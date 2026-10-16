"""Verification Gate 5 official integration and QA test suite for Level 4 of Gemini-Hybrid-Pool-Gateway.

Topological Level 4 Gate 5 Verifications:
1. Mathematical Routing Proofs (ADR-07):
   - Invariant Proof: Session affinity account at C=1, S=1.35 strictly beats competing idle account
     at C=0, S=1.00 by 1.08 > 1.00.
   - Non-affinity penalty: Busy account for new conversation at C=1, S=1.00 drops to 0.25 (D=(1+C)^2=4.0)
     favoring idle accounts.
   - Continuous urgency factor tau in [1.0, 1.5] scaling smoothly over 18,000s window.
   - Strict eligibility gate zeroing (G=0):
     * Web: 5-hour usage >= 0.95, weekly usage >= 0.98, concurrency >= max_concurrency, cooldown, suspension.
     * OAuth: remaining fraction < 0.02, quota exhausted, cooldown, concurrency >= max_concurrency, suspension.
   - Context rotation limit at 120,000 characters neutralizes affinity multiplier back to 1.0.

2. Smart Pacing & Rate Guard:
   - Safe start interval: 0.5s minimum gap enforced between consecutive starts.
   - Fast sub-second request cooldown: 0.6s delay after < 1.0s requests.
   - Random jitter range [0.4s, 0.8s] applied without zeroing eligibility gate.
   - Zero cooldown after normal long-running streams (>= 1.0s).
   - apply_smart_pacing updates timestamps asynchronously.

3. Cross-Channel Routing & Priority Rules:
   - Flash (gemini-3.8-flash, gemini-3.1-flash-lite): OAuth first -> Web fallback upon OAuth exhaustion.
   - Argon (gemini-4-argon): Web strictly exclusive; never falls back to OAuth, raising NoEligibleAccountError.
   - Pro (gemini-3.1-pro, gemini-2.5-pro): Web first -> OAuth fallback upon Web exhaustion.
   - Excluded accounts bypass and allow_cross_channel=False isolation.

4. Zero-Drop Pre-Stream Handshake & Instant Failover (ADR-10):
   - is_chunk_non_empty semantic validation for dict and Pydantic chunks.
   - chain_async_iterators integrity: flawless replay of buffered handshake chunks + active stream
     with guaranteed cleanup callback on completion, early break, or exception.
   - Zero-Byte Stream EOF interception before client response, instant failover to secondary account
     with complete stream delivery.
   - HTTP 429 & server error interception during handshake: immediate concurrency decrement (zero leak),
     cooldown enforcement, seamless failover.
   - Strict pool exhaustion: raising PoolExhaustedError when all eligible accounts fail, maintaining active_requests=0.
   - Concurrency leak protection across all operational scenarios.
   - Non-streaming chat completion failover (execute_chat_completion_with_failover) aggregating text, reasoning,
     and tool calls.
   - Model validation: raising ModelNotSupportedError for unknown models.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    AccountsConfig,
    AccountsStateRegistry,
    LMDBStore,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    WebUsage,
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
    state_registry,
)
from core import (
    HANDSHAKE_MAX_BUFFER_CHUNKS,
    FailoverController,
    HandshakeFailedError,
    ModelNotSupportedError,
    NoEligibleAccountError,
    PoolExhaustedError,
    StreamChoice,
    StreamChunk,
    StreamDelta,
    StreamToolCall,
    ToolCallFunction,
    UsageMonitor,
    apply_smart_pacing,
    calculate_oauth_account_score,
    calculate_session_affinity,
    calculate_smart_pacing_delay,
    calculate_soonest_reset_urgency,
    calculate_web_account_score,
    calculate_web_concurrency_denominator,
    chain_async_iterators,
    default_failover_controller,
    execute_chat_completion_with_failover,
    execute_stream_with_failover,
    get_model_channel_preference,
    is_chunk_non_empty,
    is_model_supported_in_gateway,
    is_oauth_account_eligible,
    is_web_account_eligible,
    select_optimal_account,
)
from core.prefix_differ import compute_prefix_hashes


# ============================================================================
# Fixtures & Factories
# ============================================================================


@pytest.fixture
def temp_lmdb(tmp_path: Path) -> Any:
    """Create an isolated, temporary LMDBStore instance."""
    store = LMDBStore(path=tmp_path / "lmdb_gate5", map_size=10 * 1024 * 1024)
    yield store
    store.close()


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AccountsStateRegistry:
    """Provide an isolated AccountsStateRegistry in memory."""
    dummy_accounts_file = tmp_path / "accounts_gate5.json"
    dummy_accounts_file.write_text(
        json.dumps({"oauth_accounts": [], "web_accounts": []}), encoding="utf-8"
    )
    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", dummy_accounts_file)
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()
    return registry


def make_oauth_account(
    account_id: str = "gate5-oauth-1",
    status: str = "ACTIVE",
    remaining_fraction: float = 1.0,
    is_exhausted: bool = False,
    cooldown_until: int | None = None,
    active_requests: int = 0,
    max_concurrency: int = 5,
    discovered_models: list[str] | None = None,
    recent_latency_ms: float = 0.0,
    reset_at: int | None = None,
) -> OAuthAccount:
    """Helper factory for OAuth accounts in Gate 5 tests."""
    models = discovered_models if discovered_models is not None else [
        "gemini-2.5-pro",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
    ]
    model_quotas: dict[str, ModelQuota] = {}
    for m in models:
        model_quotas[m] = ModelQuota(
            remaining_fraction=remaining_fraction,
            is_exhausted=is_exhausted,
            reset_at=reset_at,
        )

    acc = OAuthAccount(
        id=account_id,
        label=f"Label {account_id}",
        transport_mode="http_v1internal",
        credentials=OAuthCredentials(
            access_token="ya29.gate5-token",
            refresh_token="1//gate5-refresh",
            expires_at=int(time.time() + 3600),
            project_id="gate5-project",
        ),
        status=status,
        discovered_models=models,
        max_concurrency=max_concurrency,
        quota_state=OAuthQuotaState(
            cooldown_until=cooldown_until,
            model_quotas=model_quotas,
        ),
    )
    acc.active_requests = active_requests
    acc.recent_latency_ms = recent_latency_ms
    return acc


def make_web_account(
    account_id: str = "gate5-web-1",
    status: str = "ACTIVE",
    five_hour_pct: float = 0.20,
    weekly_pct: float = 0.10,
    optimistic_5h_delta: float = 0.0,
    optimistic_weekly_delta: float = 0.0,
    cooldown_until: int | None = None,
    active_requests: int = 0,
    max_concurrency: int = 3,
    discovered_models: list[str] | None = None,
    five_hour_reset_at: int | None = None,
    weekly_reset_at: int | None = None,
    last_started_timestamp: float = 0.0,
    last_completed_timestamp: float = 0.0,
) -> WebAccount:
    """Helper factory for Web accounts in Gate 5 tests."""
    models = discovered_models if discovered_models is not None else [
        "gemini-4-argon",
        "gemini-2.5-pro",
        "gemini-3.8-flash",
    ]
    now_ts = int(time.time())
    acc = WebAccount(
        id=account_id,
        label=f"Label {account_id}",
        cookies=WebCookies(
            secure_1psid="test_secure_1psid_gate5",
            secure_1psidts="test_secure_1psidts_gate5",
        ),
        status=status,
        discovered_models=models,
        max_concurrency=max_concurrency,
        cooldown_until=cooldown_until,
        usage=WebUsage(
            five_hour_pct=five_hour_pct,
            five_hour_reset_at=five_hour_reset_at or (now_ts + 14400),
            weekly_pct=weekly_pct,
            weekly_reset_at=weekly_reset_at or (now_ts + 500000),
            tier="ADVANCED",
        ),
    )
    acc.active_requests = active_requests
    acc.optimistic_5h_delta = optimistic_5h_delta
    acc.optimistic_weekly_delta = optimistic_weekly_delta
    acc.last_started_timestamp = last_started_timestamp
    acc.last_completed_timestamp = last_completed_timestamp
    return acc


# ============================================================================
# Section 1: Mathematical Governing Proofs & Affinity Calculations (ADR-07)
# ============================================================================


def test_math_proof_session_affinity_surpasses_idle_competing_account(
    temp_lmdb: LMDBStore,
) -> None:
    """ADR-07 Proof 1: Account with active session affinity at C=1 (S=1.35)

    must strictly outperform competing idle account at C=0 (S=1.0) by exact ratio:
        S / D_conc = 1.35 / (1.0 + 0.25*1) = 1.35 / 1.25 = 1.08 > 1.00.
    """
    history_prefix = [
        {"role": "user", "content": "Let us initialize the codebase architecture."},
        {"role": "assistant", "content": "Architecture plan initialized."},
    ]
    hashes = compute_prefix_hashes(history_prefix)
    target_hash = hashes[-1]

    # Account A hosts the ongoing session (C=1, S=1.35)
    account_a = make_web_account(
        account_id="web-session-host",
        active_requests=1,
        five_hour_pct=0.30,
        weekly_pct=0.20,
    )
    temp_lmdb.put_session(target_hash, account_a.id)

    # Account B is completely idle (C=0, S=1.0) with identical usage margins
    account_b = make_web_account(
        account_id="web-idle-competitor",
        active_requests=0,
        five_hour_pct=0.30,
        weekly_pct=0.20,
    )

    current_turn = history_prefix + [{"role": "user", "content": "Next step please."}]

    # 1. Denominator mathematical verification
    d_affinity = calculate_web_concurrency_denominator(active_requests=1, session_affinity=1.35)
    d_idle = calculate_web_concurrency_denominator(active_requests=0, session_affinity=1.0)
    assert d_affinity == pytest.approx(1.25)
    assert d_idle == pytest.approx(1.00)

    # 2. Score calculation with identical baseline margins
    score_a = calculate_web_account_score(
        account_a,
        model="gemini-4-argon",
        messages=current_turn,
        lmdb_store=temp_lmdb,
    )
    score_b = calculate_web_account_score(
        account_b,
        model="gemini-4-argon",
        messages=current_turn,
        lmdb_store=temp_lmdb,
    )

    assert score_a > 0.0
    assert score_b > 0.0

    ratio = score_a / score_b
    assert ratio == pytest.approx(1.08, rel=1e-3)
    assert ratio > 1.00

    # 3. Selection confirmation: Account A chosen despite C=1
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()
    registry.upsert_web_account(account_a)
    registry.upsert_web_account(account_b)

    chosen, channel = select_optimal_account(
        model="gemini-4-argon",
        messages=current_turn,
        registry=registry,
        lmdb_store=temp_lmdb,
    )
    assert chosen.id == account_a.id
    assert channel == "web"


def test_math_proof_busy_account_penalized_for_new_conversation(
    temp_lmdb: LMDBStore,
) -> None:
    """ADR-07 Proof 2: For a new conversation (no session affinity, S=1.0),

    a busy account at C=1 is penalized quadratically:
        D_conc = (1.0 + C)^2 = (1 + 1)^2 = 4.0
        Score ratio = 1.0 / 4.0 = 0.25 vs idle account at C=0 (D=1.0).
    """
    account_busy = make_web_account(
        account_id="web-busy-no-affinity",
        active_requests=1,
        five_hour_pct=0.25,
        weekly_pct=0.15,
    )
    account_idle = make_web_account(
        account_id="web-idle-new-chat",
        active_requests=0,
        five_hour_pct=0.25,
        weekly_pct=0.15,
    )

    new_messages = [{"role": "user", "content": "Brand new isolated prompt with no prior history."}]

    # 1. Denominator mathematical verification
    d_busy = calculate_web_concurrency_denominator(active_requests=1, session_affinity=1.0)
    d_idle = calculate_web_concurrency_denominator(active_requests=0, session_affinity=1.0)
    assert d_busy == pytest.approx(4.0)
    assert d_idle == pytest.approx(1.0)

    # 2. Score ratio verification
    score_busy = calculate_web_account_score(
        account_busy,
        model="gemini-4-argon",
        messages=new_messages,
        lmdb_store=temp_lmdb,
    )
    score_idle = calculate_web_account_score(
        account_idle,
        model="gemini-4-argon",
        messages=new_messages,
        lmdb_store=temp_lmdb,
    )

    ratio = score_busy / score_idle
    assert ratio == pytest.approx(0.25, rel=1e-3)

    # 3. Idle account must be selected
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()
    registry.upsert_web_account(account_busy)
    registry.upsert_web_account(account_idle)

    chosen, _ = select_optimal_account(
        model="gemini-4-argon",
        messages=new_messages,
        registry=registry,
        lmdb_store=temp_lmdb,
    )
    assert chosen.id == account_idle.id


def test_continuous_urgency_factor_tau_behavior_and_window_scaling() -> None:
    """Continuous urgency factor tau in [1.0, 1.5] scaling across 18000s window.

    Formula:
        tau = 1.0 + 0.5 * (1.0 - min(window_s, max(0, T_reset - now)) / window_s)
    """
    window = WEB_URGENCY_WINDOW_S
    assert window == 18000
    now = 1700000000.0

    # 1. No reset timestamp or non-positive -> 1.0
    assert calculate_soonest_reset_urgency(None, now=now, window_s=window) == 1.0
    assert calculate_soonest_reset_urgency(0, now=now, window_s=window) == 1.0
    assert calculate_soonest_reset_urgency(-100, now=now, window_s=window) == 1.0

    # 2. Reset far in the future (>= 18,000s away) -> minimum urgency (1.0)
    assert calculate_soonest_reset_urgency(int(now + 18000), now=now, window_s=window) == pytest.approx(1.0)
    assert calculate_soonest_reset_urgency(int(now + 25000), now=now, window_s=window) == pytest.approx(1.0)

    # 3. Reset halfway (9,000s away) -> 1.0 + 0.5 * (1.0 - 0.5) = 1.25
    assert calculate_soonest_reset_urgency(int(now + 9000), now=now, window_s=window) == pytest.approx(1.25)

    # 4. Reset 75% elapsed (4,500s remaining) -> 1.0 + 0.5 * (1.0 - 0.25) = 1.375
    assert calculate_soonest_reset_urgency(int(now + 4500), now=now, window_s=window) == pytest.approx(1.375)

    # 5. Reset immediate (0s remaining) -> maximum urgency (1.5)
    assert calculate_soonest_reset_urgency(int(now), now=now, window_s=window) == pytest.approx(1.5)

    # 6. Reset overdue (T_reset < now) -> clamped to 1.5
    assert calculate_soonest_reset_urgency(int(now - 120), now=now, window_s=window) == pytest.approx(1.5)


def test_web_eligibility_gate_zeroing_boundary_conditions() -> None:
    """Strict G_web = 0 zeroing when usage ceilings, concurrency, or status constraints are violated."""
    now_ts = 1700000000.0
    model = "gemini-4-argon"

    # Base valid account
    acc = make_web_account(
        five_hour_pct=0.50,
        weekly_pct=0.50,
        active_requests=0,
        max_concurrency=3,
        status="ACTIVE",
    )
    assert is_web_account_eligible(acc, model, now=now_ts) is True
    assert calculate_web_account_score(acc, model, now=now_ts) > 0.0

    # Case 1: 5-hour ceiling >= 0.95 (scraped usage)
    acc.usage.five_hour_pct = WEB_5H_CEILING  # 0.95
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    assert calculate_web_account_score(acc, model, now=now_ts) == 0.0

    # Case 2: 5-hour ceiling reached via optimistic delta
    acc.usage.five_hour_pct = 0.90
    acc.optimistic_5h_delta = 0.05  # 0.90 + 0.05 = 0.95
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    assert calculate_web_account_score(acc, model, now=now_ts) == 0.0
    acc.optimistic_5h_delta = 0.0
    acc.usage.five_hour_pct = 0.50

    # Case 3: Weekly ceiling >= 0.98 (scraped usage)
    acc.usage.weekly_pct = WEB_WEEKLY_CEILING  # 0.98
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    assert calculate_web_account_score(acc, model, now=now_ts) == 0.0

    # Case 4: Weekly ceiling reached via optimistic delta
    acc.usage.weekly_pct = 0.95
    acc.optimistic_weekly_delta = 0.03  # 0.95 + 0.03 = 0.98
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    assert calculate_web_account_score(acc, model, now=now_ts) == 0.0
    acc.optimistic_weekly_delta = 0.0
    acc.usage.weekly_pct = 0.50

    # Case 5: Concurrency ceiling reached (C >= max_concurrency)
    acc.active_requests = 3
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    assert calculate_web_account_score(acc, model, now=now_ts) == 0.0
    acc.active_requests = 0

    # Case 6: Status not ACTIVE
    acc.status = "COOLDOWN"
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    acc.status = "SUSPENDED"
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    acc.status = "ACTIVE"

    # Case 7: Active cooldown window
    acc.cooldown_until = int(now_ts + 60)
    assert is_web_account_eligible(acc, model, now=now_ts) is False
    acc.cooldown_until = None

    # Case 8: Model not in discovered_models
    acc.discovered_models = ["gemini-2.5-flash"]
    assert is_web_account_eligible(acc, "gemini-4-argon", now=now_ts) is False


def test_oauth_eligibility_gate_zeroing_boundary_conditions() -> None:
    """Strict G_oauth = 0 zeroing when quota, Argon, concurrency, or status constraints are violated."""
    now_ts = 1700000000.0
    model = "gemini-2.5-pro"

    # Base valid account
    acc = make_oauth_account(
        remaining_fraction=1.0,
        is_exhausted=False,
        active_requests=0,
        max_concurrency=5,
        status="ACTIVE",
    )
    assert is_oauth_account_eligible(acc, model, now=now_ts) is True
    assert calculate_oauth_account_score(acc, model, now=now_ts) > 0.0

    # Case 1: Argon model requested on OAuth channel -> ALWAYS rejected (Argon is Web-only)
    assert is_oauth_account_eligible(acc, "gemini-4-argon", now=now_ts) is False
    assert calculate_oauth_account_score(acc, "gemini-4-argon", now=now_ts) == 0.0

    # Case 2: Model quota exhausted flag
    acc.quota_state.model_quotas[model].is_exhausted = True
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    assert calculate_oauth_account_score(acc, model, now=now_ts) == 0.0
    acc.quota_state.model_quotas[model].is_exhausted = False

    # Case 3: Remaining fraction below OAUTH_MIN_REMAINING_FRACTION (0.02)
    acc.quota_state.model_quotas[model].remaining_fraction = 0.019
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    assert calculate_oauth_account_score(acc, model, now=now_ts) == 0.0
    acc.quota_state.model_quotas[model].remaining_fraction = 0.02
    assert is_oauth_account_eligible(acc, model, now=now_ts) is True
    acc.quota_state.model_quotas[model].remaining_fraction = 1.0

    # Case 4: Concurrency cap reached
    acc.active_requests = 5
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    assert calculate_oauth_account_score(acc, model, now=now_ts) == 0.0
    acc.active_requests = 0

    # Case 5: Status not ACTIVE
    acc.status = "COOLDOWN"
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    acc.status = "SUSPENDED"
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    acc.status = "ACTIVE"

    # Case 6: Active cooldown window
    acc.quota_state.cooldown_until = int(now_ts + 120)
    assert is_oauth_account_eligible(acc, model, now=now_ts) is False
    acc.quota_state.cooldown_until = None

    # Case 7: Expired reset_at restores eligibility even if remaining_fraction was low
    acc.quota_state.model_quotas[model].remaining_fraction = 0.00
    acc.quota_state.model_quotas[model].reset_at = int(now_ts - 5)  # in the past!
    assert is_oauth_account_eligible(acc, model, now=now_ts) is True


def test_session_affinity_context_rotation_limit_at_120k_chars(temp_lmdb: LMDBStore) -> None:
    """Session affinity must drop from 1.35 to 1.0 when conversation length reaches 120,000 characters."""
    assert SESSION_ROTATION_CHAR_LIMIT == 120000

    account = make_web_account(account_id="web-rotation-test")
    # Base message setup
    base_messages = [
        {"role": "user", "content": "Short intro"},
        {"role": "assistant", "content": "Short response"},
    ]
    hashes = compute_prefix_hashes(base_messages)
    temp_lmdb.put_session(hashes[-1], account.id)

    # 1. Under rotation limit (100k chars) -> S = 1.35
    under_limit_messages = base_messages + [{"role": "user", "content": "x" * 100_000}]
    affinity_under = calculate_session_affinity(account, under_limit_messages, lmdb_store=temp_lmdb)
    assert affinity_under == pytest.approx(SESSION_AFFINITY_MULTIPLIER)  # 1.35

    # 2. At or over rotation limit (125k chars) -> S = 1.0 (neutralized to prevent session bloat)
    over_limit_messages = base_messages + [{"role": "user", "content": "x" * 125_000}]
    affinity_over = calculate_session_affinity(account, over_limit_messages, lmdb_store=temp_lmdb)
    assert affinity_over == pytest.approx(1.0)


# ============================================================================
# Section 2: Smart Pacing & Rate Guard
# ============================================================================


def test_smart_pacing_minimum_start_interval_enforcement() -> None:
    """Enforces 0.5s safe start interval between consecutive Web request launches."""
    assert WEB_MIN_START_INTERVAL_S == 0.5
    acc = make_web_account(last_started_timestamp=1000.0, last_completed_timestamp=0.0)

    # At t = 1000.2 (0.2s elapsed) -> requires 0.3s delay
    delay_02 = calculate_smart_pacing_delay(acc, now=1000.2)
    assert delay_02 == pytest.approx(0.3)

    # At t = 1000.45 (0.45s elapsed) -> requires 0.05s delay
    delay_045 = calculate_smart_pacing_delay(acc, now=1000.45)
    assert delay_045 == pytest.approx(0.05)

    # At t = 1000.5 (exactly 0.5s elapsed) -> 0 delay
    assert calculate_smart_pacing_delay(acc, now=1000.5) == 0.0

    # At t = 1001.0 (1.0s elapsed) -> 0 delay
    assert calculate_smart_pacing_delay(acc, now=1001.0) == 0.0


def test_smart_pacing_fast_request_cooldown_and_jitter() -> None:
    """Enforces 0.6s cooldown (or jitter range [0.4s, 0.8s]) after sub-second (< 1.0s) requests."""
    assert WEB_FAST_REQUEST_THRESHOLD_S == 1.0
    assert WEB_MIN_INTERVAL_S == 0.6
    assert WEB_JITTER_RANGE_S == (0.4, 0.8)

    # Fast request: started at 1000.0, completed at 1000.4 (duration = 0.4s < 1.0s)
    acc = make_web_account(last_started_timestamp=1000.0, last_completed_timestamp=1000.4)

    # 1. Fixed cooldown (use_jitter=False)
    # At t = 1000.5 (0.1s after completion) -> requires 0.6 - 0.1 = 0.5s delay
    delay_fixed = calculate_smart_pacing_delay(acc, now=1000.5, use_jitter=False)
    assert delay_fixed == pytest.approx(0.5)

    # At t = 1001.1 (0.7s after completion) -> requires 0.0 delay
    assert calculate_smart_pacing_delay(acc, now=1001.1, use_jitter=False) == 0.0

    # 2. Jitter cooldown (use_jitter=True)
    # At t = 1000.4 (immediate completion) -> delay must be within [0.4s, 0.8s]
    jitter_delays = [
        calculate_smart_pacing_delay(acc, now=1000.4, use_jitter=True)
        for _ in range(30)
    ]
    for d in jitter_delays:
        assert WEB_JITTER_RANGE_S[0] <= d <= WEB_JITTER_RANGE_S[1]

    # Verify distribution is non-constant
    assert len(set(jitter_delays)) > 1


def test_smart_pacing_does_not_zero_eligibility_gate() -> None:
    """Smart pacing cooldown must NOT zero the account eligibility gate G_web."""
    # Fast request in cooldown window
    acc = make_web_account(
        last_started_timestamp=1000.0,
        last_completed_timestamp=1000.3,
        status="ACTIVE",
    )
    now_in_cooldown = 1000.4  # Only 0.1s after completion

    # Smart pacing returns a delay
    delay = calculate_smart_pacing_delay(acc, now=now_in_cooldown)
    assert delay > 0.0

    # BUT eligibility gate G_web remains strictly eligible (1.0)!
    assert is_web_account_eligible(acc, "gemini-4-argon", now=now_in_cooldown) is True
    assert calculate_web_account_score(acc, "gemini-4-argon", now=now_in_cooldown) > 0.0


def test_smart_pacing_normal_long_request_zero_cooldown() -> None:
    """Normal long-running streams (duration >= 1.0s) impose zero completion cooldown."""
    # Long stream: started at 1000.0, completed at 1003.5 (duration = 3.5s >= 1.0s)
    acc = make_web_account(last_started_timestamp=1000.0, last_completed_timestamp=1003.5)

    # At t = 1003.6 (0.1s after completion, but 3.6s after start >= 0.5s)
    delay = calculate_smart_pacing_delay(acc, now=1003.6)
    assert delay == 0.0


@pytest.mark.asyncio
async def test_apply_smart_pacing_updates_timestamps_asynchronously() -> None:
    """apply_smart_pacing records last_started_timestamp and applies delay when needed."""
    acc = make_web_account(last_started_timestamp=0.0)

    t_launch = 1700000000.0
    applied_delay = await apply_smart_pacing(acc, now=t_launch)
    assert applied_delay == 0.0
    assert acc.last_started_timestamp == t_launch

    # Second launch shortly after
    t_second = t_launch + 0.2
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        applied_delay_2 = await apply_smart_pacing(acc, now=t_second)
        assert applied_delay_2 == pytest.approx(0.3)
        mock_sleep.assert_awaited_once_with(pytest.approx(0.3))


# ============================================================================
# Section 3: Channel Routing & Preference Policies
# ============================================================================


def test_routing_preference_flash_routes_oauth_first_and_fails_over_to_web() -> None:
    """Flash model preference: primary channel OAuth -> fallback channel Web."""
    pref_primary, pref_secondary = get_model_channel_preference("gemini-3.8-flash")
    assert pref_primary == "oauth"
    assert pref_secondary == "web"

    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    oauth_acc = make_oauth_account(account_id="oauth-flash", remaining_fraction=0.8)
    web_acc = make_web_account(account_id="web-flash", five_hour_pct=0.2)
    registry.upsert_oauth_account(oauth_acc)
    registry.upsert_web_account(web_acc)

    # 1. Both channels eligible -> routes to OAuth first
    chosen, channel = select_optimal_account("gemini-3.8-flash", registry=registry)
    assert chosen.id == oauth_acc.id
    assert channel == "oauth"

    # 2. OAuth exhausted -> falls over to Web channel
    oauth_acc.quota_state.model_quotas["gemini-3.8-flash"].is_exhausted = True
    chosen_fb, channel_fb = select_optimal_account("gemini-3.8-flash", registry=registry)
    assert chosen_fb.id == web_acc.id
    assert channel_fb == "web"


def test_routing_preference_argon_exclusively_web_and_strictly_rejects_oauth() -> None:
    """Argon model is strictly Web-only; never falls back to OAuth, raising NoEligibleAccountError."""
    pref_primary, pref_secondary = get_model_channel_preference("gemini-4-argon")
    assert pref_primary == "web"
    assert pref_secondary is None  # Strictly no secondary channel!

    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    oauth_acc = make_oauth_account(account_id="oauth-idle-rich", remaining_fraction=1.0)
    web_acc = make_web_account(account_id="web-argon", five_hour_pct=0.3)
    registry.upsert_oauth_account(oauth_acc)
    registry.upsert_web_account(web_acc)

    # 1. Available Web account -> selects Web
    chosen, channel = select_optimal_account("gemini-4-argon", registry=registry)
    assert chosen.id == web_acc.id
    assert channel == "web"

    # 2. Web pool exhausted -> raises NoEligibleAccountError despite abundant OAuth accounts
    web_acc.usage.five_hour_pct = 0.99
    with pytest.raises(NoEligibleAccountError) as exc_info:
        select_optimal_account("gemini-4-argon", registry=registry)
    assert "No eligible account found" in str(exc_info.value)
    assert "gemini-4-argon" in str(exc_info.value)


def test_routing_preference_pro_routes_web_first_and_fails_over_to_oauth() -> None:
    """Pro model preference: primary channel Web -> fallback channel OAuth."""
    pref_primary, pref_secondary = get_model_channel_preference("gemini-2.5-pro")
    assert pref_primary == "web"
    assert pref_secondary == "oauth"

    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    oauth_acc = make_oauth_account(account_id="oauth-pro", remaining_fraction=0.9)
    web_acc = make_web_account(account_id="web-pro", five_hour_pct=0.2)
    registry.upsert_oauth_account(oauth_acc)
    registry.upsert_web_account(web_acc)

    # 1. Both eligible -> routes to Web first (economizing OAuth quota)
    chosen, channel = select_optimal_account("gemini-2.5-pro", registry=registry)
    assert chosen.id == web_acc.id
    assert channel == "web"

    # 2. Web ceiling reached -> seamlessly falls over to OAuth
    web_acc.usage.five_hour_pct = 0.96
    chosen_fb, channel_fb = select_optimal_account("gemini-2.5-pro", registry=registry)
    assert chosen_fb.id == oauth_acc.id
    assert channel_fb == "oauth"


def test_routing_with_excluded_ids_and_cross_channel_suppression() -> None:
    """Verification of excluded_ids bypass and allow_cross_channel=False isolation."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    oauth_acc = make_oauth_account(account_id="oauth-1")
    web_acc = make_web_account(account_id="web-1")
    registry.upsert_oauth_account(oauth_acc)
    registry.upsert_web_account(web_acc)

    # 1. excluded_ids bypasses primary account
    chosen, channel = select_optimal_account(
        "gemini-2.5-pro",
        excluded_ids={"web-1"},
        registry=registry,
    )
    assert chosen.id == oauth_acc.id
    assert channel == "oauth"

    # 2. allow_cross_channel=False suppresses fallback
    web_acc.usage.five_hour_pct = 0.96  # Ineligible
    with pytest.raises(NoEligibleAccountError):
        select_optimal_account(
            "gemini-2.5-pro",
            allow_cross_channel=False,
            registry=registry,
        )


# ============================================================================
# Section 4: Zero-Drop Pre-Stream Handshake & Instant Failover (ADR-10)
# ============================================================================


def test_is_chunk_non_empty_semantic_evaluation_dict_and_pydantic() -> None:
    """Semantic inspection of streaming chunks (dict & Pydantic StreamChunk) under ADR-10."""
    # 1. Dict checks
    assert is_chunk_non_empty({"choices": [{"delta": {"content": "Real tokens"}}]}) is True
    assert is_chunk_non_empty({"choices": [{"delta": {"reasoning_content": "Deep thought"}}]}) is True
    assert is_chunk_non_empty({"choices": [{"delta": {"tool_calls": [{"id": "call_1"}]}}]}) is True

    # Empty / metadata-only chunks -> False
    assert is_chunk_non_empty({"choices": [{"delta": {"content": ""}}]}) is False
    assert is_chunk_non_empty({"choices": [{"delta": {"role": "assistant"}}]}) is False
    assert is_chunk_non_empty({"choices": [{"delta": {}}]}) is False
    assert is_chunk_non_empty({"choices": []}) is False
    assert is_chunk_non_empty({}) is False
    assert is_chunk_non_empty(None) is False

    # 2. Pydantic StreamChunk checks
    pydantic_content = StreamChunk(
        id="c1",
        model="gemini-4-argon",
        choices=[StreamChoice(delta=StreamDelta(content="Hello world"))],
    )
    assert is_chunk_non_empty(pydantic_content) is True

    pydantic_tool = StreamChunk(
        id="c2",
        model="gemini-4-argon",
        choices=[
            StreamChoice(
                delta=StreamDelta(
                    tool_calls=[
                        StreamToolCall(
                            index=0,
                            id="tc-1",
                            function=ToolCallFunction(name="test_fn", arguments="{}"),
                        )
                    ]
                )
            )
        ],
    )
    assert is_chunk_non_empty(pydantic_tool) is True

    pydantic_empty = StreamChunk(
        id="c3",
        model="gemini-4-argon",
        choices=[StreamChoice(delta=StreamDelta(role="assistant", content=""))],
    )
    assert is_chunk_non_empty(pydantic_empty) is False


@pytest.mark.asyncio
async def test_chain_async_iterators_preserves_order_and_guarantees_cleanup() -> None:
    """chain_async_iterators yields buffered chunks then active stream, guaranteeing on_complete."""
    buffered = [{"chunk": 1}, {"chunk": 2}]

    async def _active_gen() -> AsyncIterator[dict[str, Any]]:
        yield {"chunk": 3}
        yield {"chunk": 4}

    callback_called = False

    def _cleanup() -> None:
        nonlocal callback_called
        callback_called = True

    # 1. Full stream consumption
    collected: list[dict[str, Any]] = []
    async for item in chain_async_iterators(buffered, _active_gen(), on_complete=_cleanup):
        collected.append(item)

    assert [c["chunk"] for c in collected] == [1, 2, 3, 4]
    assert callback_called is True

    # 2. Early break consumption guarantees cleanup via aclose
    callback_called_break = False

    def _cleanup_break() -> None:
        nonlocal callback_called_break
        callback_called_break = True

    chained = chain_async_iterators(buffered, _active_gen(), on_complete=_cleanup_break)
    try:
        async for item in chained:
            if item["chunk"] == 2:
                break
    finally:
        await chained.aclose()

    assert callback_called_break is True

    # 3. Exception in stream guarantees cleanup
    callback_called_err = False

    def _cleanup_err() -> None:
        nonlocal callback_called_err
        callback_called_err = True

    async def _failing_gen() -> AsyncIterator[dict[str, Any]]:
        yield {"chunk": 3}
        raise RuntimeError("Mid-stream connection abort")

    with pytest.raises(RuntimeError):
        async for item in chain_async_iterators(buffered, _failing_gen(), on_complete=_cleanup_err):
            pass

    assert callback_called_err is True


@pytest.mark.asyncio
async def test_zero_byte_stream_handshake_instant_failover_success(
    temp_lmdb: LMDBStore,
) -> None:
    """ADR-10 Zero-Byte Stream EOF: Intercepts empty stream before client response,

    decrements active_requests on failed account, and seamlessly delivers secondary account stream.
    """
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    account_a = make_web_account(account_id="web-zero-byte-fail", five_hour_pct=0.10)
    account_b = make_web_account(account_id="web-backup-success", five_hour_pct=0.20)
    registry.upsert_web_account(account_a)
    registry.upsert_web_account(account_b)

    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    # Account A returns 3 empty metadata chunks then EOF (Zero-Byte stream)
    async def _empty_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        yield {"choices": [{"delta": {"content": ""}}]}
        # Zero-Byte EOF!

    # Account B returns real tokens
    async def _success_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Resilient "}}]}
        yield {"choices": [{"delta": {"content": "Zero-Drop Stream!"}}]}

    def _stream_factory(acc: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if acc.id == "web-zero-byte-fail":
            return _empty_stream()
        return _success_stream()

    stream, chosen_acc, channel = await controller.execute_stream_with_failover(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "Test zero-drop stream"}],
        stream_factory=_stream_factory,
    )

    # 1. Chosen account must be account B
    assert chosen_acc.id == account_b.id
    assert channel == "web"

    # 2. Account A active_requests decremented immediately upon handshake failure
    assert account_a.active_requests == 0
    # Account B currently streaming (active_requests == 1)
    assert account_b.active_requests == 1

    # 3. Stream consumed completely
    tokens: list[str] = []
    async for chunk in stream:
        c = chunk["choices"][0]["delta"].get("content")
        if c:
            tokens.append(c)

    assert "".join(tokens) == "Resilient Zero-Drop Stream!"

    # 4. Concurrency counter on Account B decremented back to 0 via on_complete
    assert account_b.active_requests == 0


@pytest.mark.asyncio
async def test_handshake_http_429_classification_and_instant_failover(
    temp_lmdb: LMDBStore,
) -> None:
    """HTTP 429 during handshake: classifies rate limit, puts account in COOLDOWN,

    decrements active_requests immediately, and fails over to secondary account.
    """
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    oauth_primary = make_oauth_account(account_id="oauth-primary-429", remaining_fraction=1.0)
    oauth_backup = make_oauth_account(account_id="oauth-backup-win", remaining_fraction=1.0)
    registry.upsert_oauth_account(oauth_primary)
    registry.upsert_oauth_account(oauth_backup)

    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    async def _rate_limited_stream() -> AsyncIterator[dict[str, Any]]:
        # Simulate 429 error with quotaResetDelay payload raised during handshake
        resp = httpx.Response(
            status_code=429,
            json={
                "error": {
                    "code": 429,
                    "message": "Resource exhausted",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"metadata": {"quotaResetDelay": "60s"}}],
                }
            },
            request=httpx.Request("POST", "http://test"),
        )
        raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        yield {}  # make it an async generator

    async def _healthy_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Healthy response from backup"}}]}

    def _stream_factory(acc: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if acc.id == "oauth-primary-429":
            return _rate_limited_stream()
        return _healthy_stream()

    stream, chosen_acc, channel = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Test 429 failover"}],
        stream_factory=_stream_factory,
    )

    # 1. Chosen account is backup
    assert chosen_acc.id == oauth_backup.id
    assert channel == "oauth"

    # 2. Primary account transitioned to COOLDOWN and active_requests decremented to 0
    assert oauth_primary.status == "COOLDOWN"
    assert oauth_primary.active_requests == 0
    assert oauth_primary.quota_state.cooldown_until is not None

    # 3. Stream consumed
    tokens: list[str] = []
    async for chunk in stream:
        c = chunk["choices"][0]["delta"].get("content")
        if c:
            tokens.append(c)

    assert "".join(tokens) == "Healthy response from backup"
    assert oauth_backup.active_requests == 0


@pytest.mark.asyncio
async def test_handshake_server_error_and_network_disconnect_failover(
    temp_lmdb: LMDBStore,
) -> None:
    """Network connection error during handshake triggers instant failover without concurrency leaks."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    web_failing = make_web_account(account_id="web-net-fail", five_hour_pct=0.1)
    web_healthy = make_web_account(account_id="web-net-ok", five_hour_pct=0.2)
    registry.upsert_web_account(web_failing)
    registry.upsert_web_account(web_healthy)

    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    async def _network_error_stream() -> AsyncIterator[dict[str, Any]]:
        raise httpx.ConnectError("Connection reset by peer during handshake")
        yield {}

    async def _healthy_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Network recovery successful"}}]}

    def _stream_factory(acc: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if acc.id == "web-net-fail":
            return _network_error_stream()
        return _healthy_stream()

    stream, chosen_acc, _ = await controller.execute_stream_with_failover(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "Test net failover"}],
        stream_factory=_stream_factory,
    )

    assert chosen_acc.id == web_healthy.id
    assert web_failing.active_requests == 0
    assert web_failing.consecutive_errors >= 1

    chunks = [c async for c in stream]
    assert len(chunks) == 1
    assert web_healthy.active_requests == 0


@pytest.mark.asyncio
async def test_zero_concurrency_leak_across_all_failure_modes(
    temp_lmdb: LMDBStore,
) -> None:
    """Invariant: active_requests counter strictly returns to 0 across ALL failure and completion modes."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    acc = make_web_account(account_id="web-leak-test", five_hour_pct=0.1)
    registry.upsert_web_account(acc)
    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    # 1. Normal stream completion
    async def _stream_normal() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Token"}}]}

    stream, _, _ = await controller.execute_stream_with_failover(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "Hi"}],
        stream_factory=lambda *a, **k: _stream_normal(),
    )
    assert acc.active_requests == 1
    async for _ in stream:
        pass
    assert acc.active_requests == 0

    # 2. Mid-stream break
    async def _stream_mid_break() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Token 1"}}]}
        yield {"choices": [{"delta": {"content": "Token 2"}}]}

    stream, _, _ = await controller.execute_stream_with_failover(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "Hi"}],
        stream_factory=lambda *a, **k: _stream_mid_break(),
    )
    assert acc.active_requests == 1
    try:
        async for _ in stream:
            break
    finally:
        await stream.aclose()
    assert acc.active_requests == 0

    # 3. Handshake failure (zero byte)
    async def _stream_zero_byte() -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    with pytest.raises(PoolExhaustedError):
        await controller.execute_stream_with_failover(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "Hi"}],
            stream_factory=lambda *a, **k: _stream_zero_byte(),
        )
    assert acc.active_requests == 0


@pytest.mark.asyncio
async def test_pool_exhausted_error_when_all_accounts_fail(temp_lmdb: LMDBStore) -> None:
    """PoolExhaustedError raised when all accounts in pool fail handshake, maintaining 0 concurrency."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    acc1 = make_web_account(account_id="web-exhaust-1")
    acc2 = make_web_account(account_id="web-exhaust-2")
    registry.upsert_web_account(acc1)
    registry.upsert_web_account(acc2)

    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    async def _all_fail_stream() -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("Global network outage")
        yield {}

    with pytest.raises(PoolExhaustedError) as exc_info:
        await controller.execute_stream_with_failover(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "Test pool exhaust"}],
            stream_factory=lambda *a, **k: _all_fail_stream(),
        )

    assert "Pool exhausted" in str(exc_info.value)
    assert acc1.active_requests == 0
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_non_streaming_chat_completion_with_failover(temp_lmdb: LMDBStore) -> None:
    """execute_chat_completion_with_failover aggregates tokens, reasoning, and tool calls with failover."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    acc_fail = make_web_account(account_id="web-nonstream-fail", five_hour_pct=0.1)
    acc_ok = make_web_account(account_id="web-nonstream-ok", five_hour_pct=0.2)
    registry.upsert_web_account(acc_fail)
    registry.upsert_web_account(acc_ok)

    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    async def _fail_stream() -> AsyncIterator[dict[str, Any]]:
        raise httpx.ReadTimeout("Timeout on first attempt")
        yield {}

    async def _ok_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "Plan formulated.",
                    },
                }
            ]
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "The forecast in Tokyo is clear.",
                    },
                }
            ]
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_tokyo_weather",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Tokyo"}',
                                },
                            }
                        ]
                    },
                }
            ]
        }

    def _stream_factory(acc: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if acc.id == "web-nonstream-fail":
            return _fail_stream()
        return _ok_stream()

    result = await controller.execute_chat_completion_with_failover(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "Weather check"}],
        stream_factory=_stream_factory,
    )

    # Verify standard OpenAI chat completion schema
    assert result["object"] == "chat.completion"
    assert result["model"] == "gemini-4-argon"
    assert result["_account_id"] == "web-nonstream-ok"
    assert result["_channel"] == "web"

    message = result["choices"][0]["message"]
    assert message["content"] == "The forecast in Tokyo is clear."
    assert message["reasoning_content"] == "Plan formulated."
    assert len(message["tool_calls"]) == 1
    assert message["tool_calls"][0]["id"] == "call_tokyo_weather"
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"


def test_model_not_supported_error_for_invalid_models() -> None:
    """is_model_supported_in_gateway and controller reject unsupported models with ModelNotSupportedError."""
    # Model support validation
    assert is_model_supported_in_gateway("gemini-2.5-pro") is True
    assert is_model_supported_in_gateway("gemini-4-argon") is True
    assert is_model_supported_in_gateway("gemini-custom-future") is True
    assert is_model_supported_in_gateway("gpt-4o") is False
    assert is_model_supported_in_gateway("claude-3-opus") is False
    assert is_model_supported_in_gateway("") is False

    controller = default_failover_controller
    with pytest.raises(ModelNotSupportedError):
        asyncio.run(
            controller.execute_stream_with_failover(
                model="gpt-4o",
                messages=[{"role": "user", "content": "test"}],
            )
        )

    with pytest.raises(ModelNotSupportedError):
        asyncio.run(
            controller.execute_stream_with_failover(
                model="",
                messages=[{"role": "user", "content": "test"}],
            )
        )


@pytest.mark.asyncio
async def test_handshake_max_buffer_chunks_limit_enforced(temp_lmdb: LMDBStore) -> None:
    """Handshake buffers up to HANDSHAKE_MAX_BUFFER_CHUNKS (15) empty chunks before declaring failure."""
    assert HANDSHAKE_MAX_BUFFER_CHUNKS == 15

    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()

    acc = make_web_account(account_id="web-many-empty")
    registry.upsert_web_account(acc)
    controller = FailoverController(registry=registry, lmdb_store=temp_lmdb)

    # Stream yields 20 empty chunks without non-empty content
    async def _infinite_empty_stream() -> AsyncIterator[dict[str, Any]]:
        for _ in range(20):
            yield {"choices": [{"delta": {"role": "assistant"}}]}

    with pytest.raises(PoolExhaustedError):
        await controller.execute_stream_with_failover(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "test"}],
            stream_factory=lambda *a, **k: _infinite_empty_stream(),
        )

    assert acc.active_requests == 0
