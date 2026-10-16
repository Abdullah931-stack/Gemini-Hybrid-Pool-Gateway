"""Proactive usage monitoring, drift-safe quota tracking, and intelligent error handling.

Implements Level 3 (Usage Monitor Plane) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. Authenticated RPC `jSf9Qc` Usage Scraping & Drift-Safe Reset Detection:
   - Queries Google's `/usage` endpoint via batchexecute, unwrapping XSSI frames.
   - Extracts and updates `five_hour_pct`, `five_hour_reset_at`, `weekly_pct`, `weekly_reset_at`,
     and subscription `tier` ("FREE", "PRO", "ULTRA", "PLUS").
   - Drift-Safe Reset: resets accumulated `account.optimistic_5h_delta = 0.0` and
     `account.optimistic_weekly_delta = 0.0` upon successful RPC scraping or whenever
     current time crosses `five_hour_reset_at` / `weekly_reset_at`.
2. Context-Scaled Optimistic Delta Function:
   - Computes Delta_5h(m, L_chars) = Delta_base(m) * (1.0 + 0.25 * (L_chars / 25000)).
3. OAuth CLI Quota Synchronization:
   - Queries `:fetchAvailableModels` with seamless 403 fallback to `:retrieveUserQuota`,
     updating per-model `remaining_fraction` and `reset_at`.
4. Intelligent 429 Classifier (`classify_and_handle_oauth_429`):
   - Parses `quotaResetDelay` duration strings (e.g., "4h30m28s", "3600s", "15m", "1.5s").
   - If `quotaResetDelay` exists: identifies genuine quota exhaustion, places account in
     `COOLDOWN`, sets `cooldown_until = now + delay_seconds`, and persists via `accounts_file_lock`.
   - If absent: identifies transient / burst rate limiting, prevents long cooldown,
     and logs transient error to allow immediate failover to another pool account.
5. Permanent Account Suspension (`handle_account_suspension`):
   - Transitions account to `SUSPENDED` upon permanent 403 / Terms of Service violation.
6. Recording & Central Orchestration:
   - `record_successful_request`: updates latency EMA and accumulated optimistic deltas.
   - `sync_all_registered_accounts`: executes a complete sync pass across all registered accounts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Mapping, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from config.settings import (
    AccountsStateRegistry,
    ModelQuota,
    OAuthAccount,
    WebAccount,
    WebUsage,
    get_model_base_delta,
    save_accounts_snapshot,
    state_registry,
)
from core.auth_oauth_cli import ensure_valid_access_token
from core.model_registry import discover_oauth_models

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Constants & Endpoints
# ============================================================================

JSF9QC_RPC_ID: str = "jSf9Qc"
GEMINI_BATCH_EXECUTE_URL: str = (
    "https://gemini.google.com/_/BardChatUi/data/batchexecute"
)
USAGE_SOURCE_PATH: str = "/usage"

XSSI_PREFIX: str = ")]}'\n"

# Standard Tier IDs mapped to human-readable subscription labels
TIER_LABELS: dict[int, str] = {
    1: "FREE",
    2: "PRO",
    3: "ULTRA",
    4: "PLUS",
    6: "ULTRA",
}

# Regex for parsing duration strings like "4h30m28s", "3600s", "15m", "1.5s", "1d2h"
_DURATION_REGEX = re.compile(
    r"^(?:(?P<days>\d+(?:\.\d+)?)\s*d)?"
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*s)?"
    r"(?:(?P<milliseconds>\d+(?:\.\d+)?)\s*ms)?$",
    re.IGNORECASE,
)

# Common regex markers for permanent suspension / TOS violations
SUSPENSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"terms\s+of\s+service", re.IGNORECASE),
    re.compile(r"account\s+disabled", re.IGNORECASE),
    re.compile(r"user\s+suspended", re.IGNORECASE),
    re.compile(r"permission_denied", re.IGNORECASE),
    re.compile(r"fraud", re.IGNORECASE),
    re.compile(r"abuse", re.IGNORECASE),
)


# ============================================================================
# 2. Data Transfer Models & Classification Results
# ============================================================================


@dataclass(frozen=True, slots=True)
class WebUsageData:
    """Parsed usage information extracted from RPC jSf9Qc."""

    five_hour_pct: float = 0.0
    five_hour_reset_at: int | None = None
    weekly_pct: float = 0.0
    weekly_reset_at: int | None = None
    tier: str = "FREE"
    use_overage_credits: bool | None = None
    ai_credits_remaining: float | None = None


@dataclass(frozen=True, slots=True)
class OAuth429Classification:
    """Result of classifying an HTTP 429 response on an OAuth account."""

    is_exhaustion: bool
    delay_seconds: float | None
    cooldown_until: int | None
    reason: str


# ============================================================================
# 3. Context-Scaled Optimistic Delta Function
# ============================================================================


def calculate_optimistic_5h_delta(model_name: str, context_chars: int) -> float:
    r"""Calculate the estimated 5-hour quota consumption delta scaled by context length.

    Formula:
        \Delta_{5h}(m, L_{chars}) = \Delta_{base}(m) \times (1.0 + 0.25 \times (L_{chars} / 25000))

    Parameters:
        model_name: Canonical model name (e.g., 'gemini-4-argon', 'gemini-3.1-pro').
        context_chars: Total character length of the conversation context.

    Returns:
        Optimistic quota consumption percentage as a float (e.g., 0.10 for 10%).
    """
    base_delta = get_model_base_delta(model_name)
    safe_chars = max(0, int(context_chars))
    scaling_factor = 1.0 + 0.25 * (safe_chars / 25000.0)
    return float(base_delta * scaling_factor)


def calculate_optimistic_weekly_delta(model_name: str, context_chars: int) -> float:
    """Calculate the estimated weekly quota consumption delta.

    Weekly quota capacity in Gemini Web is significantly larger than the 5-hour
    window; local estimation scales the 5-hour delta by 0.25.
    """
    return calculate_optimistic_5h_delta(model_name, context_chars) * 0.25


# ============================================================================
# 4. Quota Reset Delay & Intelligent 429 Analyzer
# ============================================================================


def parse_quota_reset_delay(raw_delay: Any) -> float | None:
    """Parse a `quotaResetDelay` value into total seconds as a float.

    Handles duration strings such as "4h30m28s", "3600s", "15m", "1.5s", "100ms",
    or numeric values (e.g., 3600 or 3600.0). Returns None if format is unparseable.
    """
    if raw_delay is None:
        return None

    if isinstance(raw_delay, (int, float)):
        return max(0.0, float(raw_delay))

    text = str(raw_delay).strip()
    if not text:
        return None

    # Check for direct numeric string (e.g. "3600", "1.5")
    try:
        return max(0.0, float(text))
    except ValueError:
        pass

    match = _DURATION_REGEX.match(text)
    if not match:
        return None

    parts = match.groupdict()
    if not any(parts.values()):
        return None

    total_seconds = 0.0
    if parts.get("days"):
        total_seconds += float(parts["days"]) * 86400.0
    if parts.get("hours"):
        total_seconds += float(parts["hours"]) * 3600.0
    if parts.get("minutes"):
        total_seconds += float(parts["minutes"]) * 60.0
    if parts.get("seconds"):
        total_seconds += float(parts["seconds"])
    if parts.get("milliseconds"):
        total_seconds += float(parts["milliseconds"]) / 1000.0

    return max(0.0, total_seconds)


def extract_quota_reset_delay_from_error(error_payload: Any) -> float | None:
    """Recursively search for `quotaResetDelay` in dictionaries, lists, or text."""
    if error_payload is None:
        return None

    if isinstance(error_payload, dict):
        # Direct lookup in top-level or known sub-keys
        for key in ("quotaResetDelay", "resetDelay", "quota_reset_delay", "retryAfter"):
            if key in error_payload:
                val = parse_quota_reset_delay(error_payload[key])
                if val is not None:
                    return val

        # Recursive search in all dict values
        for val in error_payload.values():
            extracted = extract_quota_reset_delay_from_error(val)
            if extracted is not None:
                return extracted

    elif isinstance(error_payload, (list, tuple)):
        for item in error_payload:
            extracted = extract_quota_reset_delay_from_error(item)
            if extracted is not None:
                return extracted

    elif isinstance(error_payload, str):
        # Fallback regex search within raw JSON or error message string
        match = re.search(
            r'["\']?(?:quotaResetDelay|resetDelay|retryAfter)["\']?\s*[:=]\s*["\']?([^"\'\s,}]+)["\']?',
            error_payload,
            re.IGNORECASE,
        )
        if match:
            return parse_quota_reset_delay(match.group(1))

    elif hasattr(error_payload, "response") and error_payload.response is not None:
        # httpx.HTTPStatusError or similar exception with attached response
        resp = error_payload.response
        # Check Retry-After header first
        retry_header = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
        if retry_header:
            parsed = parse_quota_reset_delay(retry_header)
            if parsed is not None:
                return parsed

        try:
            body = resp.json()
            return extract_quota_reset_delay_from_error(body)
        except Exception:
            text = getattr(resp, "text", "")
            return extract_quota_reset_delay_from_error(text)

    return None


async def classify_and_handle_oauth_429(
    account: OAuthAccount,
    error_payload: Any,
    *,
    now: float | None = None,
    persist: bool = True,
) -> OAuth429Classification:
    """Classify an HTTP 429 response on an OAuth account and enforce appropriate cooldown.

    - Genuine Quota Exhaustion (has `quotaResetDelay`): puts account into `COOLDOWN`
      until `now + delay_seconds` and atomically persists changes via `accounts_file_lock`.
    - Transient / Burst Rejection (no `quotaResetDelay`): increments transient error count
      without imposing a long cooldown, allowing immediate failover.
    """
    current_time = time.time() if now is None else float(now)
    delay_seconds = extract_quota_reset_delay_from_error(error_payload)

    if delay_seconds is not None and delay_seconds > 0.0:
        cooldown_until = int(current_time + delay_seconds)
        account.status = "COOLDOWN"
        account.quota_state.cooldown_until = cooldown_until
        account.quota_state.consecutive_errors += 1
        account.quota_state.last_error_reason = (
            f"Quota exhausted (quotaResetDelay: {delay_seconds:.1f}s)"
        )

        logger.warning(
            "Account '%s' placed in COOLDOWN for %.1fs (until %s). Genuine quota exhaustion.",
            account.id,
            delay_seconds,
            cooldown_until,
        )

        if persist:
            await save_accounts_snapshot()

        return OAuth429Classification(
            is_exhaustion=True,
            delay_seconds=delay_seconds,
            cooldown_until=cooldown_until,
            reason=account.quota_state.last_error_reason,
        )

    # Transient / Burst rejection without quotaResetDelay
    account.quota_state.consecutive_errors += 1
    reason = "Transient burst rate limit (429 without quotaResetDelay)"
    account.quota_state.last_error_reason = reason
    logger.info(
        "Account '%s' received transient 429 burst limit; long cooldown avoided.",
        account.id,
    )

    return OAuth429Classification(
        is_exhaustion=False,
        delay_seconds=None,
        cooldown_until=None,
        reason=reason,
    )


# ============================================================================
# 5. Permanent Account Suspension Handler
# ============================================================================


def is_suspension_error(error_payload: Any) -> bool:
    """Return True if ``error_payload`` matches permanent suspension or TOS violations."""
    raw_text = str(error_payload)
    if isinstance(error_payload, dict):
        raw_text = json.dumps(error_payload)
    elif hasattr(error_payload, "response") and error_payload.response is not None:
        raw_text = getattr(error_payload.response, "text", "")

    return any(pattern.search(raw_text) for pattern in SUSPENSION_PATTERNS)


async def handle_account_suspension(
    account: OAuthAccount | WebAccount,
    reason: str,
    *,
    persist: bool = True,
) -> None:
    """Mark an account as SUSPENDED permanently and persist immediately."""
    account.status = "SUSPENDED"
    suspension_msg = f"SUSPENDED: {reason.strip()}"
    if isinstance(account, OAuthAccount):
        account.quota_state.last_error_reason = suspension_msg
    else:
        account.last_error_reason = suspension_msg

    logger.critical(
        "Account '%s' (%s) transitioned to SUSPENDED. Reason: %s",
        account.id,
        account.__class__.__name__,
        reason,
    )

    if persist:
        await save_accounts_snapshot()


# ============================================================================
# 6. Web RPC `jSf9Qc` & XSSI Response Extraction
# ============================================================================


def strip_xssi_prefix(raw_text: str) -> str:
    """Remove Google XSSI security protection prefixes (')]}\\'' or ')]}\\'\\n')."""
    stripped = raw_text.strip()
    if stripped.startswith(")]}'"):
        stripped = stripped[4:].lstrip()
    return stripped


def _find_nested_val(data: Any, path: Sequence[int | str]) -> Any:
    """Safely traverse nested lists/dicts following index/key sequence."""
    current = data
    for key in path:
        if isinstance(key, int):
            if isinstance(current, (list, tuple)) and 0 <= key < len(current):
                current = current[key]
            else:
                return None
        elif isinstance(key, str):
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        else:
            return None
    return current


def _clamp_usage_fraction(raw_val: Any) -> float:
    """Normalize raw usage value to a clamped fraction in [0.0, 1.0]."""
    try:
        val = float(raw_val)
        if 1.0 < val <= 100.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
    except (ValueError, TypeError):
        return 0.0


def parse_jsf9qc_usage_response(raw_text_or_json: Any) -> WebUsageData:
    """Parse Google's RPC `jSf9Qc` response payload from /usage into WebUsageData.

    Accepts raw XSSI response text, parsed JSON list, or internal dictionary.
    Extracts:
    - `tier`: 1="FREE", 2="PRO", 3="ULTRA", 4="PLUS", 6="ULTRA"
    - `current_5h` (metric_type 1): usage fraction, reset epoch timestamp
    - `weekly` (metric_type 2): usage fraction, reset epoch timestamp
    - `ai_credits_remaining` (metric_type 3)
    """
    if isinstance(raw_text_or_json, str):
        clean_text = strip_xssi_prefix(raw_text_or_json)
        try:
            data = json.loads(clean_text)
        except json.JSONDecodeError:
            # Attempt line-by-line / ndjson parsing
            for line in clean_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                return WebUsageData()
    else:
        data = raw_text_or_json

    # If it is a dictionary (e.g. from gemini-webapi client._usage_info)
    if isinstance(data, dict):
        return _parse_usage_info_dict(data)

    if not isinstance(data, list):
        return WebUsageData()

    # Locate the jSf9Qc payload inside batchexecute response structure
    # Google batchexecute format:
    # [ [ "wrb.fr", "jSf9Qc", "JSON_STRING", null, null, null, "generic" ] ]
    # or direct part_body: [tier_id, [items...], use_overage]
    part_body: Any = None
    for item in data:
        if isinstance(item, list):
            rpc_id = _find_nested_val(item, [1])
            if rpc_id == JSF9QC_RPC_ID:
                payload_str = _find_nested_val(item, [2])
                if isinstance(payload_str, str):
                    try:
                        part_body = json.loads(payload_str)
                        break
                    except json.JSONDecodeError:
                        continue
            elif len(item) >= 2 and isinstance(item[0], int) and isinstance(item[1], list):
                # Direct inner part_body passed
                part_body = item
                break

    if part_body is None and len(data) >= 2 and isinstance(data[0], int) and isinstance(data[1], list):
        part_body = data

    if not isinstance(part_body, list):
        return WebUsageData()

    raw_tier_id = _find_nested_val(part_body, [0])
    tier_label = TIER_LABELS.get(int(raw_tier_id), "FREE") if isinstance(raw_tier_id, (int, float)) else "FREE"
    usage_items = _find_nested_val(part_body, [1]) or []
    use_overage = _find_nested_val(part_body, [2])

    five_hour_pct: float = 0.0
    five_hour_reset_at: int | None = None
    weekly_pct: float = 0.0
    weekly_reset_at: int | None = None
    ai_credits: float | None = None

    if isinstance(usage_items, list):
        for metric_item in usage_items:
            if not isinstance(metric_item, list):
                continue
            # metric_item structure: [remaining, usage_level, metric_type, [[reset_ts, ...]]]
            remaining = _find_nested_val(metric_item, [0])
            usage_level = _find_nested_val(metric_item, [1])
            metric_type = _find_nested_val(metric_item, [2])
            reset_ts = _find_nested_val(metric_item, [3, 0, 0])

            parsed_reset_ts: int | None = None
            if isinstance(reset_ts, (int, float)) and reset_ts > 0:
                parsed_reset_ts = int(reset_ts)

            if metric_type == 1:
                # 5-hour window
                five_hour_pct = _clamp_usage_fraction(usage_level)
                if parsed_reset_ts is not None:
                    five_hour_reset_at = parsed_reset_ts
            elif metric_type == 2:
                # Weekly window
                weekly_pct = _clamp_usage_fraction(usage_level)
                if parsed_reset_ts is not None:
                    weekly_reset_at = parsed_reset_ts
            elif metric_type == 3:
                # AI Credits
                if isinstance(remaining, (int, float)):
                    ai_credits = float(remaining)

    return WebUsageData(
        five_hour_pct=five_hour_pct,
        five_hour_reset_at=five_hour_reset_at,
        weekly_pct=weekly_pct,
        weekly_reset_at=weekly_reset_at,
        tier=tier_label,
        use_overage_credits=bool(use_overage) if use_overage is not None else None,
        ai_credits_remaining=ai_credits,
    )


def _parse_usage_info_dict(data: dict[str, Any]) -> WebUsageData:
    """Helper to parse a high-level usage dictionary into WebUsageData."""
    tier_info = data.get("tier")
    tier_label = "FREE"
    if isinstance(tier_info, dict):
        tier_label = str(tier_info.get("label", tier_info.get("name", "FREE"))).upper()
    elif isinstance(tier_info, str):
        tier_label = tier_info.upper()

    def _extract_metric(metric_dict: Any) -> tuple[float, int | None]:
        if not isinstance(metric_dict, dict):
            return 0.0, None
        level = metric_dict.get("usage_level", metric_dict.get("usage_percentage", 0.0))
        pct = _clamp_usage_fraction(level)
        reset = metric_dict.get("reset_at", metric_dict.get("reset_ts"))
        reset_ts: int | None = None
        if isinstance(reset, (int, float)):
            reset_ts = int(reset)
        elif isinstance(reset, str) and reset.strip():
            try:
                # Try parsing ISO timestamp
                dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                reset_ts = int(dt.timestamp())
            except ValueError:
                if reset.isdigit():
                    reset_ts = int(reset)
        return pct, reset_ts

    h5_pct, h5_reset = _extract_metric(data.get("current_5h") or data.get("5h"))
    w_pct, w_reset = _extract_metric(data.get("weekly"))
    ai_credits = data.get("ai_credits_remaining")
    use_overage = data.get("use_overage_ai_credits")

    return WebUsageData(
        five_hour_pct=h5_pct,
        five_hour_reset_at=h5_reset,
        weekly_pct=w_pct,
        weekly_reset_at=w_reset,
        tier=tier_label,
        use_overage_credits=bool(use_overage) if use_overage is not None else None,
        ai_credits_remaining=float(ai_credits) if isinstance(ai_credits, (int, float)) else None,
    )


# ============================================================================
# 7. Drift-Safe Reset Detection & Web Usage Sync
# ============================================================================


def check_and_apply_drift_safe_resets(
    account: WebAccount,
    *,
    now: float | None = None,
) -> bool:
    """Zero optimistic deltas if current time crossed the recorded reset time.

    Drift-Safe Reset Detection (ADR-02):
    Because Google's reset timestamp continuously drifts forward on idle accounts,
    we never compare timestamps with strict equality (==). Instead, whenever
    `now >= five_hour_reset_at`, the 5-hour optimistic delta is immediately zeroed.
    Likewise, when `now >= weekly_reset_at`, the weekly optimistic delta is zeroed.
    """
    current_time = time.time() if now is None else float(now)
    changed = False

    if (
        account.usage.five_hour_reset_at is not None
        and current_time >= account.usage.five_hour_reset_at
        and account.optimistic_5h_delta > 0.0
    ):
        logger.debug(
            "Account '%s' crossed 5h reset threshold (now=%.0f >= reset=%s); zeroing optimistic_5h_delta",
            account.id,
            current_time,
            account.usage.five_hour_reset_at,
        )
        account.optimistic_5h_delta = 0.0
        changed = True

    if (
        account.usage.weekly_reset_at is not None
        and current_time >= account.usage.weekly_reset_at
        and account.optimistic_weekly_delta > 0.0
    ):
        logger.debug(
            "Account '%s' crossed weekly reset threshold (now=%.0f >= reset=%s); zeroing optimistic_weekly_delta",
            account.id,
            current_time,
            account.usage.weekly_reset_at,
        )
        account.optimistic_weekly_delta = 0.0
        changed = True

    return changed


def apply_web_usage_update(
    account: WebAccount,
    usage_data: WebUsageData,
    *,
    now: float | None = None,
) -> None:
    """Apply newly scraped WebUsageData to ``account`` and reset optimistic deltas."""
    current_time = time.time() if now is None else float(now)

    account.usage.five_hour_pct = usage_data.five_hour_pct
    account.usage.five_hour_reset_at = usage_data.five_hour_reset_at
    account.usage.weekly_pct = usage_data.weekly_pct
    account.usage.weekly_reset_at = usage_data.weekly_reset_at
    account.usage.last_synced_at = int(current_time)
    account.usage.tier = usage_data.tier

    # Drift-Safe Reset: Successful live query resets accumulated local deltas
    account.optimistic_5h_delta = 0.0
    account.optimistic_weekly_delta = 0.0
    account.consecutive_errors = 0
    account.last_error_reason = None

    logger.info(
        "Account '%s' usage updated: 5h=%.1f%% (reset: %s), weekly=%.1f%%, tier=%s",
        account.id,
        usage_data.five_hour_pct * 100.0,
        usage_data.five_hour_reset_at,
        usage_data.weekly_pct * 100.0,
        usage_data.tier,
    )


async def sync_web_account_usage(
    account: WebAccount,
    *,
    client: Any = None,
    http_client: httpx.AsyncClient | None = None,
    persist: bool = True,
    now: float | None = None,
) -> WebUsageData:
    """Execute authenticated RPC jSf9Qc on /usage and update ``account`` state."""
    # First apply passive drift-safe reset check in case network query fails
    check_and_apply_drift_safe_resets(account, now=now)

    usage_data: WebUsageData | None = None

    # Path 1: Via GeminiClient instance (standard runtime path)
    if client is not None:
        try:
            if hasattr(client, "_fetch_usage_info") and callable(client._fetch_usage_info):
                await client._fetch_usage_info()
                raw_info = getattr(client, "_usage_info", None)
                if raw_info and isinstance(raw_info, dict):
                    usage_data = _parse_usage_info_dict(raw_info)
        except Exception as exc:
            logger.warning(
                "GeminiClient._fetch_usage_info failed for account '%s': %s",
                account.id,
                exc,
            )

    # Path 2: Direct HTTP batchexecute call (fallback or headless CLI sync)
    if usage_data is None:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
            ),
            "X-Same-Domain": "1",
        }
        cookies = account.cookies.to_dict()
        post_data = {
            "f.req": json.dumps([[[JSF9QC_RPC_ID, "[]", None, "generic"]]]),
        }
        params = {
            "rpcids": JSF9QC_RPC_ID,
            "source-path": USAGE_SOURCE_PATH,
        }

        async def _do_req(cl: httpx.AsyncClient) -> httpx.Response:
            return await cl.post(
                GEMINI_BATCH_EXECUTE_URL,
                params=params,
                data=post_data,
                headers=headers,
                cookies=cookies,
            )

        try:
            if http_client is not None:
                resp = await _do_req(http_client)
            else:
                async with httpx.AsyncClient(timeout=30.0, proxy=account.proxy) as owned_client:
                    resp = await _do_req(owned_client)

            if resp.status_code == 200:
                usage_data = parse_jsf9qc_usage_response(resp.text)
            elif resp.status_code == 403 or is_suspension_error(resp.text):
                await handle_account_suspension(account, f"HTTP {resp.status_code} on /usage", persist=persist)
                return WebUsageData()
            else:
                logger.warning(
                    "Direct /usage RPC returned status %d for account '%s'",
                    resp.status_code,
                    account.id,
                )
        except Exception as exc:
            logger.warning("Direct /usage RPC network error for account '%s': %s", account.id, exc)

    if usage_data is not None:
        apply_web_usage_update(account, usage_data, now=now)
        if persist:
            await save_accounts_snapshot()
        return usage_data

    return WebUsageData(
        five_hour_pct=account.usage.five_hour_pct,
        five_hour_reset_at=account.usage.five_hour_reset_at,
        weekly_pct=account.usage.weekly_pct,
        weekly_reset_at=account.usage.weekly_reset_at,
        tier=account.usage.tier,
    )


# ============================================================================
# 8. OAuth CLI Quota Synchronization
# ============================================================================


async def sync_oauth_account_quotas(
    account: OAuthAccount,
    *,
    http_client: httpx.AsyncClient | None = None,
    persist: bool = True,
    now: float | None = None,
) -> list[str]:
    """Synchronize per-model quotas for an OAuth account via `:fetchAvailableModels` / `:retrieveUserQuota`."""
    current_time = time.time() if now is None else float(now)

    # Check and clear expired cooldown
    if (
        account.status == "COOLDOWN"
        and account.quota_state.cooldown_until is not None
        and current_time >= account.quota_state.cooldown_until
    ):
        logger.info("Account '%s' cooldown expired; returning to ACTIVE status.", account.id)
        account.status = "ACTIVE"
        account.quota_state.cooldown_until = None
        account.quota_state.consecutive_errors = 0
        account.quota_state.last_error_reason = None

    if account.status == "SUSPENDED":
        logger.debug("Skipping quota sync for SUSPENDED OAuth account '%s'.", account.id)
        return list(account.discovered_models)

    try:
        # Refresh access token proactively if near expiry
        await ensure_valid_access_token(account, http_client=http_client, persist=persist)

        # discover_oauth_models queries fetchAvailableModels with seamless 403 fallback to retrieveUserQuota
        models = await discover_oauth_models(account, http_client=http_client)
        account.quota_state.consecutive_errors = 0
        account.quota_state.last_error_reason = None

        if persist:
            await save_accounts_snapshot()

        return models

    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            await classify_and_handle_oauth_429(account, exc, now=now, persist=persist)
        elif exc.response is not None and (exc.response.status_code == 403 or is_suspension_error(exc)):
            await handle_account_suspension(account, str(exc), persist=persist)
        else:
            account.quota_state.consecutive_errors += 1
            account.quota_state.last_error_reason = str(exc)
        return list(account.discovered_models)

    except Exception as exc:
        account.quota_state.consecutive_errors += 1
        account.quota_state.last_error_reason = str(exc)
        logger.warning("Error syncing quotas for OAuth account '%s': %s", account.id, exc)
        return list(account.discovered_models)


# ============================================================================
# 9. Request Recording & Central Sync Pass
# ============================================================================


def record_successful_request(
    account: OAuthAccount | WebAccount,
    model_name: str,
    latency_ms: float,
    *,
    context_chars: int = 0,
    now: float | None = None,
) -> None:
    """Record metrics for a successful request on either an OAuth or Web account.

    Updates:
    - Recent latency Exponential Moving Average (EMA).
    - Local context-scaled optimistic usage deltas on Web accounts.
    - Clears consecutive errors and error reasons.
    """
    current_time = time.time() if now is None else float(now)
    safe_latency = max(0.0, float(latency_ms))

    if isinstance(account, OAuthAccount):
        account.quota_state.consecutive_errors = 0
        account.quota_state.last_error_reason = None
        # EMA update: alpha = 0.2
        if account.recent_latency_ms <= 0.0:
            account.recent_latency_ms = safe_latency
        else:
            account.recent_latency_ms = (0.8 * account.recent_latency_ms) + (0.2 * safe_latency)

    elif isinstance(account, WebAccount):
        account.consecutive_errors = 0
        account.last_error_reason = None
        account.last_completed_timestamp = current_time

        # Accumulate context-scaled optimistic quota consumption
        delta_5h = calculate_optimistic_5h_delta(model_name, context_chars)
        account.optimistic_5h_delta += delta_5h

        delta_w = calculate_optimistic_weekly_delta(model_name, context_chars)
        account.optimistic_weekly_delta += delta_w

        # Check drift-safe reset threshold
        check_and_apply_drift_safe_resets(account, now=current_time)


async def sync_all_registered_accounts(
    registry: AccountsStateRegistry | None = None,
    *,
    web_session_manager: Any = None,
    http_client: httpx.AsyncClient | None = None,
    persist: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Execute a complete synchronization cycle for all registered OAuth and Web accounts.

    Performs:
    1. OAuth quota synchronization, token maintenance, and cooldown expiry recovery.
    2. Web RPC jSf9Qc usage updates, drift-safe reset zeroing, and cooldown recovery.
    3. Atomic snapshot persistence via `save_accounts_snapshot()` under `accounts_file_lock`.
    """
    reg = registry if registry is not None else state_registry
    current_time = time.time() if now is None else float(now)

    synced_oauth: list[str] = []
    synced_web: list[str] = []
    errors: dict[str, str] = {}

    # 1. Sync OAuth Accounts
    for oauth_acc in reg.oauth_accounts:
        try:
            # Check cooldown recovery
            if (
                oauth_acc.status == "COOLDOWN"
                and oauth_acc.quota_state.cooldown_until is not None
                and current_time >= oauth_acc.quota_state.cooldown_until
            ):
                logger.info("OAuthAccount '%s' cooldown expired; returning to ACTIVE.", oauth_acc.id)
                oauth_acc.status = "ACTIVE"
                oauth_acc.quota_state.cooldown_until = None
                oauth_acc.quota_state.consecutive_errors = 0
                oauth_acc.quota_state.last_error_reason = None

            await sync_oauth_account_quotas(
                oauth_acc,
                http_client=http_client,
                persist=False,
                now=current_time,
            )
            synced_oauth.append(oauth_acc.id)
        except Exception as exc:
            errors[oauth_acc.id] = str(exc)

    # 2. Sync Web Accounts
    for web_acc in reg.web_accounts:
        try:
            # Check cooldown recovery
            if (
                web_acc.status == "COOLDOWN"
                and web_acc.cooldown_until is not None
                and current_time >= web_acc.cooldown_until
            ):
                logger.info("WebAccount '%s' cooldown expired; returning to ACTIVE.", web_acc.id)
                web_acc.status = "ACTIVE"
                web_acc.cooldown_until = None
                web_acc.consecutive_errors = 0
                web_acc.last_error_reason = None

            # Get managed client if web_session_manager supplied
            active_client = None
            if web_session_manager is not None and hasattr(web_session_manager, "get_client"):
                active_client = web_session_manager.get_client(web_acc.id)

            await sync_web_account_usage(
                web_acc,
                client=active_client,
                http_client=http_client,
                persist=False,
                now=current_time,
            )
            synced_web.append(web_acc.id)
        except Exception as exc:
            errors[web_acc.id] = str(exc)

    if persist:
        await save_accounts_snapshot()

    return {
        "timestamp": int(current_time),
        "synced_oauth_count": len(synced_oauth),
        "synced_web_count": len(synced_web),
        "oauth_accounts": synced_oauth,
        "web_accounts": synced_web,
        "errors": errors,
    }


# ============================================================================
# 10. UsageMonitor Unified Service Class
# ============================================================================


class UsageMonitor:
    """Unified service interface coordinating proactive usage and quota monitoring."""

    def __init__(
        self,
        registry: AccountsStateRegistry | None = None,
        web_session_manager: Any = None,
    ) -> None:
        self.registry: AccountsStateRegistry = registry or state_registry
        self.web_session_manager: Any = web_session_manager

    @staticmethod
    def calculate_5h_delta(model_name: str, context_chars: int) -> float:
        """Calculate context-scaled 5-hour optimistic delta."""
        return calculate_optimistic_5h_delta(model_name, context_chars)

    @staticmethod
    def calculate_weekly_delta(model_name: str, context_chars: int) -> float:
        """Calculate context-scaled weekly optimistic delta."""
        return calculate_optimistic_weekly_delta(model_name, context_chars)

    @staticmethod
    def parse_delay(raw_delay: Any) -> float | None:
        """Parse quota reset delay duration."""
        return parse_quota_reset_delay(raw_delay)

    async def classify_429(
        self,
        account: OAuthAccount,
        error_payload: Any,
        *,
        now: float | None = None,
        persist: bool = True,
    ) -> OAuth429Classification:
        """Classify 429 response and apply cooldown if exhausted."""
        return await classify_and_handle_oauth_429(
            account, error_payload, now=now, persist=persist
        )

    async def suspend_account(
        self,
        account: OAuthAccount | WebAccount,
        reason: str,
        *,
        persist: bool = True,
    ) -> None:
        """Permanently suspend an account."""
        await handle_account_suspension(account, reason, persist=persist)

    def record_request(
        self,
        account: OAuthAccount | WebAccount,
        model_name: str,
        latency_ms: float,
        *,
        context_chars: int = 0,
        now: float | None = None,
    ) -> None:
        """Record metrics for completed request."""
        record_successful_request(
            account,
            model_name,
            latency_ms,
            context_chars=context_chars,
            now=now,
        )

    async def sync_web_usage(
        self,
        account: WebAccount,
        *,
        http_client: httpx.AsyncClient | None = None,
        persist: bool = True,
        now: float | None = None,
    ) -> WebUsageData:
        """Sync a single web account's usage via RPC jSf9Qc."""
        client = (
            self.web_session_manager.get_client(account.id)
            if self.web_session_manager is not None and hasattr(self.web_session_manager, "get_client")
            else None
        )
        return await sync_web_account_usage(
            account,
            client=client,
            http_client=http_client,
            persist=persist,
            now=now,
        )

    async def sync_oauth_quotas(
        self,
        account: OAuthAccount,
        *,
        http_client: httpx.AsyncClient | None = None,
        persist: bool = True,
        now: float | None = None,
    ) -> list[str]:
        """Sync a single OAuth account's quotas via cloudcode-pa."""
        return await sync_oauth_account_quotas(
            account,
            http_client=http_client,
            persist=persist,
            now=now,
        )

    async def sync_all(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        persist: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Run a complete synchronization cycle for all registered accounts."""
        return await sync_all_registered_accounts(
            self.registry,
            web_session_manager=self.web_session_manager,
            http_client=http_client,
            persist=persist,
            now=now,
        )
