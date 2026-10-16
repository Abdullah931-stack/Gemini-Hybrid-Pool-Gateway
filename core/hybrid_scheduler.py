"""Mathematical routing engine, account scoring, session affinity, and smart pacing.

Implements Level 4 (Scheduling & Control Plane) of the Gemini-Hybrid-Pool-Gateway:
1. Eligibility gates with strict zero-drop boundary conditions:
   - ``is_oauth_account_eligible``: enforces active status, cooldown expiration,
     concurrency cap, Argon prohibition, and remaining fraction >= 0.02.
   - ``is_web_account_eligible``: enforces active status, concurrency cap,
     5-hour usage < 0.95, and weekly usage < 0.98.
2. Mathematical urgency & session affinity:
   - ``calculate_soonest_reset_urgency``: continuous window-bounded urgency multiplier
     scaling smoothly from 1.0 (far from reset) to 1.5 (immediate reset).
   - ``calculate_session_affinity``: 1.35x multiplier if LMDB has prefix match
     and context is under 120,000 characters.
   - ``calculate_web_concurrency_denominator``: linear (1 + 0.25*C) for affinity matches
     (preserving 1.35/1.25 = 1.08 > 1.00 at C=1) vs quadratic (1 + C)^2 for new chats (D=4.0 at C=1).
3. Account scoring functions:
   - ``calculate_oauth_account_score``: P_oauth = G * tau * Q / ((1 + C) * (1 + 0.001 * L)).
   - ``calculate_web_account_score``: P_web = G * S * tau * (0.95 - U_5h) * (0.98 - U_w) / D_conc.
4. Smart pacing:
   - ``calculate_smart_pacing_delay`` & ``apply_smart_pacing``: enforces 0.5s start interval
     and 0.6s cooldown after sub-second fast requests without zeroing eligibility.
5. Optimal account selection & failover:
   - ``select_optimal_account``: routes Flash to OAuth first (failover to Web),
     Argon exclusively to Web (no OAuth fallback), and Pro to Web first (failover to OAuth).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import random
import time
from typing import Any, Literal

from config.settings import (
    LMDBStore,
    OAUTH_MIN_REMAINING_FRACTION,
    SESSION_AFFINITY_MULTIPLIER,
    SESSION_ROTATION_CHAR_LIMIT,
    WEB_5H_CEILING,
    WEB_FAST_REQUEST_THRESHOLD_S,
    WEB_JITTER_RANGE_S,
    WEB_MIN_INTERVAL_S,
    WEB_MIN_START_INTERVAL_S,
    WEB_URGENCY_WINDOW_S,
    WEB_WEEKLY_CEILING,
    AccountsStateRegistry,
    OAuthAccount,
    WebAccount,
    get_lmdb_store,
    state_registry,
)
from core.model_registry import (
    parse_model_and_effort,
    split_model_effort_suffix,
)
from core.prefix_differ import (
    calculate_messages_char_length,
    compute_messages_char_length,
    get_preferred_session_account,
)

logger = logging.getLogger(__name__)


# ============================================================================
# 1. Custom Exceptions & Model Classification
# ============================================================================


class NoEligibleAccountError(RuntimeError):
    """Raised when no accounts in the pool are eligible to serve a request."""


def get_model_channel_preference(
    model: str,
) -> tuple[Literal["oauth", "web"], Literal["oauth", "web"] | None]:
    """Determine primary and failover channel for a given model.

    Routing rules:
    - Argon (e.g. ``gemini-4-argon``): Web exclusively (never fall back to OAuth).
    - Pro (e.g. ``gemini-3.1-pro``, ``gemini-2.5-pro``): Web first, then OAuth.
    - Flash (e.g. ``gemini-3.8-flash``, ``gemini-3.1-flash-lite``): OAuth first, then Web.
    - Others: OAuth first, then Web.
    """
    base_model, _ = split_model_effort_suffix(model)
    norm = base_model.lower()

    if "argon" in norm:
        return ("web", None)
    if "pro" in norm:
        return ("web", "oauth")
    if "flash" in norm:
        return ("oauth", "web")
    return ("oauth", "web")


def _account_supports_model(discovered_models: list[str], base_model: str) -> bool:
    """Return True if ``discovered_models`` contains or matches ``base_model``."""
    if not discovered_models:
        return True
    norm_target, _ = split_model_effort_suffix(base_model)
    norm_target = norm_target.lower()
    for candidate in discovered_models:
        cand_base, _ = split_model_effort_suffix(candidate)
        if cand_base.lower() == norm_target:
            return True
    return False


# ============================================================================
# 2. Eligibility Gates
# ============================================================================


def is_oauth_account_eligible(
    account: OAuthAccount,
    model: str,
    *,
    now: float | None = None,
) -> bool:
    """Check if ``account`` satisfies all strict OAuth gate conditions (G_oauth == 1).

    Rejection conditions:
    - Status is not 'ACTIVE' (e.g. SUSPENDED or COOLDOWN).
    - Account is in active cooldown window (cooldown_until > now).
    - Active requests concurrency C >= max_concurrency.
    - Requested model is Argon (OAuth CLI does not support gemini-4-argon).
    - Account's discovered_models is populated and does not include base_model.
    - Model quota is exhausted and reset_at is either unset or in the future.
    - Model quota remaining_fraction <= OAUTH_MIN_REMAINING_FRACTION (0.02)
      and reset_at is unset or in the future.
    """
    now_ts = time.time() if now is None else float(now)

    if account.status != "ACTIVE":
        return False

    if account.quota_state.cooldown_until is not None:
        if now_ts < account.quota_state.cooldown_until:
            return False

    if account.active_requests >= account.max_concurrency:
        return False

    base_model, _ = split_model_effort_suffix(model)
    norm_base = base_model.lower()

    # Argon is strictly Web-only; Antigravity CLI does not offer it
    if "argon" in norm_base:
        return False

    if not _account_supports_model(account.discovered_models, norm_base):
        return False

    # Check model quota state
    quota = account.quota_state.model_quotas.get(
        norm_base
    ) or account.quota_state.model_quotas.get(base_model)
    if quota is not None:
        reset_time_passed = quota.reset_at is not None and now_ts >= quota.reset_at
        if not reset_time_passed:
            if quota.is_exhausted:
                return False
            if quota.remaining_fraction < OAUTH_MIN_REMAINING_FRACTION:
                return False

    return True


def is_web_account_eligible(
    account: WebAccount,
    model: str,
    *,
    now: float | None = None,
) -> bool:
    """Check if ``account`` satisfies all strict Web gate conditions (G_web == 1).

    Rejection conditions:
    - Status is not 'ACTIVE' (e.g. SUSPENDED or COOLDOWN).
    - Account is in active cooldown window (cooldown_until > now).
    - Active requests concurrency C >= max_concurrency.
    - Account's discovered_models is populated and does not include base_model.
    - 5-hour usage (scraped + optimistic delta) >= WEB_5H_CEILING (0.95).
    - Weekly usage (scraped + optimistic delta) >= WEB_WEEKLY_CEILING (0.98).
    """
    now_ts = time.time() if now is None else float(now)

    if account.status != "ACTIVE":
        return False

    if account.cooldown_until is not None:
        if now_ts < account.cooldown_until:
            return False

    if account.active_requests >= account.max_concurrency:
        return False

    base_model, _ = split_model_effort_suffix(model)
    norm_base = base_model.lower()

    if not _account_supports_model(account.discovered_models, norm_base):
        return False

    eff_5h = account.usage.five_hour_pct + account.optimistic_5h_delta
    if eff_5h >= WEB_5H_CEILING:
        return False

    eff_w = account.usage.weekly_pct + account.optimistic_weekly_delta
    if eff_w >= WEB_WEEKLY_CEILING:
        return False

    return True


# ============================================================================
# 3. Mathematical Routing Functions
# ============================================================================


def calculate_soonest_reset_urgency(
    reset_timestamp: int | None,
    now: float | None = None,
    window_s: int = WEB_URGENCY_WINDOW_S,
) -> float:
    """Compute continuous reset urgency multiplier tau in [1.0, 1.5].

    Formula:
        tau = 1.0 + 0.5 * (1.0 - min(window_s, max(0, T_reset - now)) / window_s)

    When reset_timestamp is None or non-positive: tau = 1.0.
    When T_reset <= now (immediate or overdue reset): tau = 1.5 (maximum urgency).
    When T_reset - now >= window_s: tau = 1.0 (minimum urgency).
    """
    if reset_timestamp is None or reset_timestamp <= 0:
        return 1.0
    now_ts = time.time() if now is None else float(now)
    remaining_s = max(0.0, float(reset_timestamp) - now_ts)
    clamped_s = min(float(window_s), remaining_s)
    tau = 1.0 + 0.5 * (1.0 - clamped_s / float(window_s))
    return float(tau)


def calculate_session_affinity(
    account: WebAccount,
    messages: Sequence[dict[str, Any]] | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
    lmdb_store: LMDBStore | None = None,
) -> float:
    """Calculate session affinity multiplier S_i.

    Returns SESSION_AFFINITY_MULTIPLIER (1.35) if a prefix match is found in LMDB
    db_sessions matching account.id and total message length < SESSION_ROTATION_CHAR_LIMIT
    (120,000 chars). Returns 1.0 otherwise.
    """
    if not messages:
        return 1.0

    store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    preferred_id, total_chars = get_preferred_session_account(
        messages, lmdb_store=store, tools=tools
    )
    if (
        preferred_id is not None
        and preferred_id == account.id
        and total_chars < SESSION_ROTATION_CHAR_LIMIT
    ):
        return SESSION_AFFINITY_MULTIPLIER
    return 1.0


def calculate_web_concurrency_denominator(
    active_requests: int,
    session_affinity: float,
) -> float:
    """Compute conditional concurrency denominator D_conc.

    Formulas:
    - If session_affinity > 1.0 (continuation turn on same account):
        D_conc = 1.0 + 0.25 * C
        At C = 1: D = 1.25 -> S / D = 1.35 / 1.25 = 1.08 > 1.00 (beats empty account).
    - If session_affinity <= 1.0 (new chat or no affinity):
        D_conc = (1.0 + C) ** 2
        At C = 1: D = 4.0 -> heavily penalizes busy accounts for new chats.
    """
    c = max(0, int(active_requests))
    if session_affinity > 1.0:
        return float(1.0 + 0.25 * c)
    return float((1.0 + c) ** 2)


def calculate_oauth_account_score(
    account: OAuthAccount,
    model: str,
    *,
    now: float | None = None,
    window_s: int = WEB_URGENCY_WINDOW_S,
) -> float:
    """Compute priority score P_oauth for an OAuth CLI account.

    Formula:
        P = G * tau * Q / ((1 + C) * (1 + 0.001 * L))

    Where:
        G: eligibility gate (1.0 if eligible, else 0.0)
        tau: reset urgency for requested model
        Q: remaining quota fraction in [0.0, 1.0]
        C: active requests count
        L: recent latency EMA in milliseconds
    """
    if not is_oauth_account_eligible(account, model, now=now):
        return 0.0

    now_ts = time.time() if now is None else float(now)
    base_model, _ = split_model_effort_suffix(model)
    norm_base = base_model.lower()

    quota = account.quota_state.model_quotas.get(
        norm_base
    ) or account.quota_state.model_quotas.get(base_model)
    q = quota.remaining_fraction if quota is not None else 1.0
    reset_at = quota.reset_at if quota is not None else None

    tau = calculate_soonest_reset_urgency(reset_at, now=now_ts, window_s=window_s)
    c = float(account.active_requests)
    latency = float(account.recent_latency_ms)

    denom = (1.0 + c) * (1.0 + 0.001 * latency)
    return float((1.0 * tau * q) / denom)


def calculate_web_account_score(
    account: WebAccount,
    model: str,
    messages: Sequence[dict[str, Any]] | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
    *,
    now: float | None = None,
    lmdb_store: LMDBStore | None = None,
    window_s: int = WEB_URGENCY_WINDOW_S,
) -> float:
    """Compute priority score P_web for a Web Cookie account.

    Formula:
        P = G * S * tau * (0.95 - U_5h) * (0.98 - U_w) / D_conc

    Where:
        G: eligibility gate (1.0 if eligible, else 0.0)
        S: session affinity multiplier (1.35 if match, else 1.0)
        tau: 5-hour reset urgency in [1.0, 1.5]
        U_5h: scraped 5h usage + optimistic delta
        U_w: scraped weekly usage + optimistic delta
        D_conc: conditional concurrency denominator
    """
    if not is_web_account_eligible(account, model, now=now):
        return 0.0

    now_ts = time.time() if now is None else float(now)
    s = calculate_session_affinity(
        account, messages=messages, tools=tools, lmdb_store=lmdb_store
    )
    tau = calculate_soonest_reset_urgency(
        account.usage.five_hour_reset_at, now=now_ts, window_s=window_s
    )

    u_5h = account.usage.five_hour_pct + account.optimistic_5h_delta
    u_w = account.usage.weekly_pct + account.optimistic_weekly_delta

    margin_5h = max(0.0, WEB_5H_CEILING - u_5h)
    margin_w = max(0.0, WEB_WEEKLY_CEILING - u_w)

    if margin_5h <= 0.0 or margin_w <= 0.0:
        return 0.0

    d_conc = calculate_web_concurrency_denominator(account.active_requests, s)
    score = (1.0 * s * tau * margin_5h * margin_w) / d_conc
    return float(score)


# ============================================================================
# 4. Smart Pacing
# ============================================================================


def calculate_smart_pacing_delay(
    account: WebAccount,
    now: float | None = None,
    *,
    use_jitter: bool = False,
) -> float:
    """Calculate required sleep duration (seconds) before launching a Web request.

    Conditions:
    1. Start-to-start interval: ensures at least WEB_MIN_START_INTERVAL_S (0.5s)
       between consecutive request starts.
    2. Sub-second fast request cooldown: if the previous request completed in under
       WEB_FAST_REQUEST_THRESHOLD_S (1.0s), enforces WEB_MIN_INTERVAL_S (0.6s)
       or random jitter in WEB_JITTER_RANGE_S (0.4s-0.8s) after request completion.
    3. Normal long-running streams (duration >= 1.0s) impose 0s cooldown after completion.
    """
    now_ts = time.time() if now is None else float(now)
    t_start = float(account.last_started_timestamp)
    t_done = float(account.last_completed_timestamp)

    # 1. Start-to-start pacing
    start_delay = 0.0
    if t_start > 0.0:
        elapsed_start = now_ts - t_start
        if elapsed_start < WEB_MIN_START_INTERVAL_S:
            start_delay = max(0.0, WEB_MIN_START_INTERVAL_S - elapsed_start)

    # 2. Fast request completion pacing
    done_delay = 0.0
    if t_done >= t_start > 0.0:
        duration = t_done - t_start
        if duration < WEB_FAST_REQUEST_THRESHOLD_S:
            elapsed_done = now_ts - t_done
            required_cooldown = (
                random.uniform(WEB_JITTER_RANGE_S[0], WEB_JITTER_RANGE_S[1])
                if use_jitter
                else WEB_MIN_INTERVAL_S
            )
            if elapsed_done < required_cooldown:
                done_delay = max(0.0, required_cooldown - elapsed_done)

    return float(max(start_delay, done_delay))


async def apply_smart_pacing(
    account: WebAccount,
    *,
    now: float | None = None,
    use_jitter: bool = False,
) -> float:
    """Apply smart pacing delay via asyncio.sleep and record request start timestamp."""
    delay = calculate_smart_pacing_delay(account, now=now, use_jitter=use_jitter)
    if delay > 0.0:
        await asyncio.sleep(delay)

    current_ts = time.time() if now is None else float(now)
    account.last_started_timestamp = current_ts
    if hasattr(account, "last_request_start_time"):
        account.last_request_start_time = current_ts
    return delay


# ============================================================================
# 5. Optimal Account Selection Algorithm
# ============================================================================


def select_optimal_account(
    model: str,
    messages: Sequence[dict[str, Any]] | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
    excluded_ids: set[str] | None = None,
    allow_cross_channel: bool = True,
    registry: AccountsStateRegistry | None = None,
    lmdb_store: LMDBStore | None = None,
    *,
    now: float | None = None,
) -> tuple[OAuthAccount | WebAccount, Literal["oauth", "web"]]:
    """Select the mathematically optimal account across pools for a given request.

    Channel preference rules:
    - Flash (e.g. ``gemini-3.8-flash``): OAuth first, falls back to Web if unavailable.
    - Argon (e.g. ``gemini-4-argon``): Web exclusively (never falls back to OAuth).
    - Pro (e.g. ``gemini-3.1-pro``): Web first, falls back to OAuth if unavailable.
    - Other: OAuth first, falls back to Web if unavailable.

    Returns:
        tuple[account, "oauth" | "web"]

    Raises:
        NoEligibleAccountError: if no accounts satisfy eligibility gates.
    """
    target_registry = registry if registry is not None else state_registry
    target_store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    excluded = set(excluded_ids) if excluded_ids is not None else set()

    primary_channel, secondary_channel = get_model_channel_preference(model)
    channels_to_evaluate: list[Literal["oauth", "web"]] = [primary_channel]
    if allow_cross_channel and secondary_channel is not None:
        channels_to_evaluate.append(secondary_channel)

    for channel in channels_to_evaluate:
        if channel == "oauth":
            oauth_candidates = [
                acc
                for acc in target_registry.oauth_accounts
                if acc.id not in excluded
            ]
            scored_oauth: list[tuple[float, int, OAuthAccount]] = []
            for acc in oauth_candidates:
                score = calculate_oauth_account_score(acc, model, now=now)
                if score > 0.0:
                    scored_oauth.append((score, -acc.active_requests, acc))

            if scored_oauth:
                scored_oauth.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return scored_oauth[0][2], "oauth"

        elif channel == "web":
            web_candidates = [
                acc
                for acc in target_registry.web_accounts
                if acc.id not in excluded
            ]
            scored_web: list[tuple[float, int, WebAccount]] = []
            for acc in web_candidates:
                score = calculate_web_account_score(
                    acc,
                    model,
                    messages=messages,
                    tools=tools,
                    now=now,
                    lmdb_store=target_store,
                )
                if score > 0.0:
                    scored_web.append((score, -acc.active_requests, acc))

            if scored_web:
                scored_web.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return scored_web[0][2], "web"

    raise NoEligibleAccountError(
        f"No eligible account found for model '{model}' "
        f"(tried channels: {channels_to_evaluate}, excluded: {sorted(excluded)})"
    )
