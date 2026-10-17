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
from collections.abc import Callable, Sequence
import logging
import random
import time
from typing import Any, Literal

from config.settings import (
    DELTA_BASE,
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
    ModelQuota,
    OAuthAccount,
    WebAccount,
    get_custom_model_routes,
    get_lmdb_store,
    get_model_base_delta,
    state_registry,
)
from core.model_registry import (
    parse_model_and_effort,
    split_model_effort_suffix,
)
from core.prefix_differ import (
    calculate_messages_char_length,
    compute_messages_char_length,
    compute_prefix_hashes,
    find_longest_prefix_session,
    get_preferred_session_account,
)

logger = logging.getLogger(__name__)

# Constants & aliases for scheduling and delta calculation
BASE_MODEL_WEIGHTS_5H: dict[str, float] = DELTA_BASE
OAUTH_MIN_QUOTA_FRACTION: float = OAUTH_MIN_REMAINING_FRACTION
SESSION_AFFINITY_MAX_CHARS: int = SESSION_ROTATION_CHAR_LIMIT
WEB_PRO_SINGLE_REQUEST_CAP: float = 0.09
estimate_messages_char_length = compute_messages_char_length


# ============================================================================
# 1. Custom Exceptions & Model Classification
# ============================================================================


class NoEligibleAccountError(RuntimeError):
    """Raised when no accounts in the pool are eligible to serve a request."""


def _normalize_base_model(model: str) -> str:
    """Strip reasoning effort suffix and normalize model name to lowercase."""
    if not model:
        return ""
    base_model, _ = split_model_effort_suffix(model.strip())
    return base_model.lower()


def compute_context_scaled_delta_5h(
    model_name: str,
    context_chars: int = 0,
) -> float:
    """Calculate context-weighted optimistic 5-hour usage delta with Pro cap.

    Formula (from 03_SCHEDULING_AND_CONTROL_SPEC.md):
        Delta_5h(m, L_chars) = Delta_base(m) * (1.0 + 0.25 * (L_chars / 25000))
    Applies WEB_PRO_SINGLE_REQUEST_CAP (0.09) when the base model is gemini-3.1-pro.
    """
    norm_base = _normalize_base_model(model_name)
    base_delta = BASE_MODEL_WEIGHTS_5H.get(norm_base)
    if base_delta is None:
        base_delta = get_model_base_delta(norm_base)
    if base_delta <= 0.0:
        return 0.0

    safe_chars = max(0, int(context_chars))
    scaled_delta = float(base_delta * (1.0 + 0.25 * (safe_chars / 25000.0)))
    if norm_base == "gemini-3.1-pro" or (
        "pro" in norm_base and "argon" not in norm_base and "flash" not in norm_base
    ):
        scaled_delta = min(scaled_delta, WEB_PRO_SINGLE_REQUEST_CAP)
    return float(scaled_delta)


def _route_dict_to_channel_preference(
    route: dict[str, Any],
) -> tuple[Literal["oauth", "web"], Literal["oauth", "web"] | None]:
    """Helper to convert a route configuration dictionary to channel preference tuple."""
    p = route.get("primary") or route.get("primary_channel")
    f_val = route.get("failover")
    if f_val is None and "failover_channel" in route:
        f_val = route.get("failover_channel")

    primary: Literal["oauth", "web"] = "web" if str(p).lower() == "web" else "oauth"
    failover: Literal["oauth", "web"] | None = None
    if f_val is not None:
        f_str = str(f_val).lower()
        if f_str == "web":
            failover = "web"
        elif f_str == "oauth":
            failover = "oauth"

    if failover == primary:
        failover = None
    return (primary, failover)


def get_model_channel_preference(
    model: str,
) -> tuple[Literal["oauth", "web"], Literal["oauth", "web"] | None]:
    """Determine primary and failover channel for a given model.

    Checks dynamic custom model routes first (from ``config/model_routes.json``),
    matching exact model name / normalized base model, then longest prefix / alias.
    If no custom route matches, falls back to default routing rules:
    - Argon (e.g. ``gemini-4-argon``): Web exclusively (never fall back to OAuth).
    - Pro (e.g. ``gemini-3.1-pro``, ``gemini-2.5-pro``): Web first, then OAuth.
    - Flash (e.g. ``gemini-3.8-flash``, ``gemini-3.1-flash-lite``): OAuth first, then Web.
    - Others: OAuth first, then Web.
    """
    raw_lower = model.strip().lower() if model else ""
    norm = _normalize_base_model(model)

    try:
        custom_routes = get_custom_model_routes()
    except Exception:
        custom_routes = {}

    if custom_routes:
        # 1. Exact match on raw lower name or normalized base model
        if raw_lower in custom_routes:
            return _route_dict_to_channel_preference(custom_routes[raw_lower])
        if norm and norm in custom_routes:
            return _route_dict_to_channel_preference(custom_routes[norm])

        # 2. Prefix / alias match (longest key first)
        for key in sorted(custom_routes.keys(), key=len, reverse=True):
            if not key:
                continue
            if norm and (norm.startswith(key) or key.startswith(norm)):
                return _route_dict_to_channel_preference(custom_routes[key])
            if raw_lower and (raw_lower.startswith(key) or key.startswith(raw_lower)):
                return _route_dict_to_channel_preference(custom_routes[key])
            if key in norm or key in raw_lower:
                return _route_dict_to_channel_preference(custom_routes[key])

    # 3. Default routing rules
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
    norm_target = _normalize_base_model(base_model)
    for candidate in discovered_models:
        if _normalize_base_model(candidate) == norm_target:
            return True
    try:
        custom_routes = get_custom_model_routes()
        raw_target = base_model.strip().lower()
        if raw_target in custom_routes or norm_target in custom_routes:
            return True
        for key in custom_routes:
            if key in raw_target or key in norm_target:
                return True
    except Exception:
        pass
    return False


def _lookup_oauth_model_quota(
    account: OAuthAccount,
    model: str,
) -> ModelQuota | None:
    """Resolve per-model quota on ``account`` after stripping effort suffixes."""
    base_model, _ = split_model_effort_suffix(model.strip())
    norm_base = base_model.lower()
    model_quotas = account.quota_state.model_quotas
    quota = model_quotas.get(norm_base) or model_quotas.get(base_model) or model_quotas.get(model)
    if quota is not None:
        return quota
    for key, candidate_quota in model_quotas.items():
        if _normalize_base_model(key) == norm_base:
            return candidate_quota
    return None


def _get_effective_web_usage(
    account: WebAccount,
    now_ts: float,
) -> tuple[float, float]:
    """Return (effective_5h_usage, effective_weekly_usage) accounting for passed resets."""
    five_hour_reset_at = account.usage.five_hour_reset_at
    if five_hour_reset_at is not None and now_ts >= five_hour_reset_at:
        eff_5h = 0.0
    else:
        eff_5h = float(account.usage.five_hour_pct) + float(account.optimistic_5h_delta)

    weekly_reset_at = account.usage.weekly_reset_at
    if weekly_reset_at is not None and now_ts >= weekly_reset_at:
        eff_w = 0.0
    else:
        eff_w = float(account.usage.weekly_pct) + float(account.optimistic_weekly_delta)

    return eff_5h, eff_w


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
    - Model quota remaining_fraction <= OAUTH_MIN_QUOTA_FRACTION (0.02)
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

    norm_base = _normalize_base_model(model)

    # Argon is strictly Web-only by default; Antigravity CLI does not offer it
    if "argon" in norm_base:
        pref = get_model_channel_preference(model)
        if pref[0] != "oauth" and pref[1] != "oauth":
            return False

    if not _account_supports_model(account.discovered_models, norm_base):
        return False

    # Check model quota state
    quota = _lookup_oauth_model_quota(account, model)
    if quota is not None:
        reset_time_passed = quota.reset_at is not None and now_ts >= quota.reset_at
        if not reset_time_passed:
            if quota.is_exhausted:
                return False
            if quota.remaining_fraction < OAUTH_MIN_QUOTA_FRACTION:
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
    - 5-hour usage (scraped + optimistic delta) >= WEB_5H_CEILING (0.95) unless reset passed.
    - Weekly usage (scraped + optimistic delta) >= WEB_WEEKLY_CEILING (0.98) unless reset passed.
    """
    now_ts = time.time() if now is None else float(now)

    if account.status != "ACTIVE":
        return False

    if account.cooldown_until is not None:
        if now_ts < account.cooldown_until:
            return False

    if account.active_requests >= account.max_concurrency:
        return False

    norm_base = _normalize_base_model(model)

    if not _account_supports_model(account.discovered_models, norm_base):
        return False

    eff_5h, eff_w = _get_effective_web_usage(account, now_ts)
    if eff_5h >= WEB_5H_CEILING:
        return False

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
    *,
    preferred_account_id: str | None = None,
    lmdb_env: Any = None,
) -> float:
    """Calculate session affinity multiplier S_i.

    Returns SESSION_AFFINITY_MULTIPLIER (1.35) if a prefix match is found in LMDB
    db_sessions matching account.id and total message length < SESSION_ROTATION_CHAR_LIMIT
    (120,000 chars). Returns 1.0 otherwise.
    """
    if preferred_account_id is not None:
        if messages:
            total_chars = estimate_messages_char_length(messages, tools=tools)
            if total_chars >= SESSION_AFFINITY_MAX_CHARS:
                return 1.0
        return SESSION_AFFINITY_MULTIPLIER if preferred_account_id == account.id else 1.0

    if not messages:
        return 1.0

    total_chars = estimate_messages_char_length(messages, tools=tools)
    if total_chars >= SESSION_AFFINITY_MAX_CHARS:
        return 1.0

    store = lmdb_store if lmdb_store is not None else (lmdb_env if lmdb_env is not None else get_lmdb_store())
    try:
        pref_res = get_preferred_session_account(
            messages, lmdb_store=store, tools=tools
        )
    except TypeError:
        pref_res = get_preferred_session_account(
            messages, env=store, max_chars=SESSION_AFFINITY_MAX_CHARS
        )

    if isinstance(pref_res, tuple):
        preferred_id, pref_chars = pref_res
        if pref_chars >= SESSION_AFFINITY_MAX_CHARS:
            return 1.0
    else:
        preferred_id = pref_res

    if preferred_id is not None and preferred_id == account.id:
        return SESSION_AFFINITY_MULTIPLIER

    # Account-scoped fallback: check if this specific account has a prefix match in LMDB
    match: Any = None
    try:
        match = find_longest_prefix_session(
            messages,
            lmdb_store=store,
            target_account_id=account.id,
        )
    except TypeError:
        try:
            match = find_longest_prefix_session(
                messages,
                env=store,
                target_account_id=account.id,
            )
        except TypeError:
            try:
                match = find_longest_prefix_session(messages, lmdb_store=store)
            except TypeError:
                match = None

    if match is not None:
        match_acc_id = (
            match.get("account_id")
            if isinstance(match, dict)
            else getattr(match, "account_id", None)
        )
        if match_acc_id == account.id:
            return SESSION_AFFINITY_MULTIPLIER

    # Direct LMDBStore scan fallback if find_longest_prefix_session did not filter by target_account_id
    if hasattr(store, "get_session_record"):
        try:
            for p_hash in reversed(compute_prefix_hashes(messages)):
                rec = store.get_session_record(p_hash)
                if rec is not None and getattr(rec, "account_id", None) == account.id:
                    return SESSION_AFFINITY_MULTIPLIER
        except Exception:
            pass

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
        Q: remaining quota fraction in [0.0, 1.0] (or 1.0 if reset_at has passed)
        C: active requests count
        L: recent latency EMA in milliseconds
    """
    if not is_oauth_account_eligible(account, model, now=now):
        return 0.0

    now_ts = time.time() if now is None else float(now)
    quota = _lookup_oauth_model_quota(account, model)
    if quota is not None:
        if quota.reset_at is not None and now_ts >= quota.reset_at:
            q = 1.0
        else:
            q = float(quota.remaining_fraction)
        reset_at = quota.reset_at
    else:
        q = 1.0
        reset_at = None

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
        U_5h: scraped 5h usage + optimistic delta (or 0.0 if 5h reset passed)
        U_w: scraped weekly usage + optimistic delta (or 0.0 if weekly reset passed)
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

    u_5h, u_w = _get_effective_web_usage(account, now_ts)

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
    time_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], Any] | None = None,
) -> float:
    """Apply smart pacing delay via sleep_fn and reserve request start timestamp pre-sleep."""
    now_ts = float(time_fn()) if now is None else float(now)
    delay = calculate_smart_pacing_delay(account, now=now_ts, use_jitter=use_jitter)

    # Reserve future slot BEFORE awaiting sleep so concurrent callers stagger properly (H-1.5)
    reserved_ts = float(now_ts + delay)
    account.last_started_timestamp = reserved_ts
    if hasattr(account, "last_request_start_time"):
        account.last_request_start_time = reserved_ts

    if delay > 0.0:
        actual_sleep = sleep_fn if sleep_fn is not None else asyncio.sleep
        await actual_sleep(delay)

    current_ts = float(time_fn()) if now is None else float(now)
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
