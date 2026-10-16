"""Comprehensive unit and integration test suite for Level 4 Hybrid Scheduler.

Verifies:
1. Mathematical proof of session affinity over idle accounts:
   - S_i / D_conc = 1.35 / 1.25 = 1.08 > 1.00 at C = 1 vs idle account at C = 0 (1.00).
   - Quadratic denominator D = (1 + C)^2 = 4.0 for new conversations at C = 1 (1.0 / 4.0 = 0.25).
2. Eligibility gates zeroing (G = 0):
   - Web 5-hour ceiling >= 0.95, weekly ceiling >= 0.98, concurrency >= max_concurrency.
   - OAuth Argon exclusion, remaining fraction < 0.02, exhaustion, cooldown, suspension.
3. Continuous urgency multiplier tau in [1.0, 1.5].
4. Session affinity with LMDB prefix match and context length rotation threshold (120,000 chars).
5. Smart pacing calculation and async enforcement:
   - 0.5s minimum start-to-start interval.
   - 0.6s cooldown after sub-second fast requests (< 1.0s).
   - 0ms cooldown after normal long-running streams (>= 1.0s).
6. Multi-channel optimal routing and failover:
   - Flash: OAuth first -> Web failover.
   - Argon: Web exclusively (strictly no OAuth fallback).
   - Pro: Web first -> OAuth failover.
   - Failover suppression when allow_cross_channel=False.
   - Proper bypassing of excluded_ids.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    LMDBStore,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    WebUsage,
    AccountsConfig,
    AccountsStateRegistry,
    SESSION_AFFINITY_MULTIPLIER,
    SESSION_ROTATION_CHAR_LIMIT,
    WEB_5H_CEILING,
    WEB_WEEKLY_CEILING,
    WEB_MIN_START_INTERVAL_S,
    WEB_MIN_INTERVAL_S,
    WEB_FAST_REQUEST_THRESHOLD_S,
    OAUTH_MIN_REMAINING_FRACTION,
)
from core.hybrid_scheduler import (
    NoEligibleAccountError,
    apply_smart_pacing,
    calculate_oauth_account_score,
    calculate_session_affinity,
    calculate_smart_pacing_delay,
    calculate_soonest_reset_urgency,
    calculate_web_account_score,
    calculate_web_concurrency_denominator,
    get_model_channel_preference,
    is_oauth_account_eligible,
    is_web_account_eligible,
    select_optimal_account,
)
from core.prefix_differ import compute_prefix_hashes


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_lmdb(tmp_path: Path) -> LMDBStore:
    """Provide a fresh isolated LMDB environment for testing."""
    db_path = tmp_path / "test_lmdb_scheduler"
    store = LMDBStore(path=db_path)
    yield store
    store.close()


@pytest.fixture
def isolated_registry() -> AccountsStateRegistry:
    """Provide an isolated AccountsStateRegistry in memory."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()
    return registry


def make_oauth_account(
    account_id: str = "oauth_1",
    status: str = "ACTIVE",
    active_requests: int = 0,
    max_concurrency: int = 3,
    remaining_fraction: float = 1.0,
    reset_at: int | None = None,
    is_exhausted: bool = False,
    latency_ms: float = 100.0,
    models: list[str] | None = None,
    cooldown_until: int | None = None,
) -> OAuthAccount:
    """Helper to construct an OAuthAccount with runtime attributes."""
    quotas = {}
    if models:
        for m in models:
            quotas[m] = ModelQuota(
                remaining_fraction=remaining_fraction,
                reset_at=reset_at,
                is_exhausted=is_exhausted,
            )
    else:
        quotas["gemini-3.8-flash"] = ModelQuota(
            remaining_fraction=remaining_fraction,
            reset_at=reset_at,
            is_exhausted=is_exhausted,
        )
        quotas["gemini-3.1-pro"] = ModelQuota(
            remaining_fraction=remaining_fraction,
            reset_at=reset_at,
            is_exhausted=is_exhausted,
        )

    acc = OAuthAccount(
        id=account_id,
        label=f"OAuth {account_id}",
        status=status,  # type: ignore[arg-type]
        max_concurrency=max_concurrency,
        discovered_models=list(models or []),
        quota_state=OAuthQuotaState(
            model_quotas=quotas,
            cooldown_until=cooldown_until,
        ),
    )
    acc.active_requests = active_requests
    acc.recent_latency_ms = latency_ms
    return acc


def make_web_account(
    account_id: str = "web_1",
    status: str = "ACTIVE",
    active_requests: int = 0,
    max_concurrency: int = 2,
    five_hour_pct: float = 0.20,
    weekly_pct: float = 0.10,
    optimistic_5h: float = 0.0,
    optimistic_weekly: float = 0.0,
    five_hour_reset_at: int | None = None,
    models: list[str] | None = None,
    cooldown_until: int | None = None,
    last_started: float = 0.0,
    last_completed: float = 0.0,
) -> WebAccount:
    """Helper to construct a WebAccount with runtime attributes."""
    acc = WebAccount(
        id=account_id,
        label=f"Web {account_id}",
        status=status,  # type: ignore[arg-type]
        max_concurrency=max_concurrency,
        discovered_models=list(models or []),
        cooldown_until=cooldown_until,
        usage=WebUsage(
            five_hour_pct=five_hour_pct,
            weekly_pct=weekly_pct,
            five_hour_reset_at=five_hour_reset_at,
        ),
    )
    acc.active_requests = active_requests
    acc.optimistic_5h_delta = optimistic_5h
    acc.optimistic_weekly_delta = optimistic_weekly
    acc.last_started_timestamp = last_started
    acc.last_completed_timestamp = last_completed
    return acc


# ============================================================================
# 1. Reset Urgency Multiplier Tests
# ============================================================================


def test_urgency_multiplier_bounds() -> None:
    """Test calculate_soonest_reset_urgency under various time differences."""
    now = 1000.0

    # No timestamp or non-positive -> baseline 1.0
    assert calculate_soonest_reset_urgency(None, now=now) == 1.0
    assert calculate_soonest_reset_urgency(0, now=now) == 1.0
    assert calculate_soonest_reset_urgency(-100, now=now) == 1.0

    # Reset in the past or right now -> maximum urgency 1.5
    assert calculate_soonest_reset_urgency(int(now), now=now) == 1.5
    assert calculate_soonest_reset_urgency(int(now - 500), now=now) == 1.5

    # Reset far away (>= 18,000s) -> minimum urgency 1.0
    assert calculate_soonest_reset_urgency(int(now + 18000), now=now) == 1.0
    assert calculate_soonest_reset_urgency(int(now + 36000), now=now) == 1.0

    # Halfway through 18,000s window (9,000s remaining) -> 1.25
    halfway_tau = calculate_soonest_reset_urgency(int(now + 9000), now=now)
    assert pytest.approx(halfway_tau, rel=1e-5) == 1.25

    # 1/4 of window remaining (4,500s remaining) -> 1.0 + 0.5 * (1 - 0.25) = 1.375
    quarter_tau = calculate_soonest_reset_urgency(int(now + 4500), now=now)
    assert pytest.approx(quarter_tau, rel=1e-5) == 1.375


# ============================================================================
# 2. Concurrency Denominator & Mathematical Proof of Session Affinity
# ============================================================================


def test_concurrency_denominator_linear_vs_quadratic() -> None:
    """Verify conditional concurrency denominator D_conc."""
    # When session affinity > 1.0 (continuation): D = 1.0 + 0.25 * C
    assert calculate_web_concurrency_denominator(0, session_affinity=1.35) == 1.0
    assert calculate_web_concurrency_denominator(1, session_affinity=1.35) == 1.25
    assert calculate_web_concurrency_denominator(2, session_affinity=1.35) == 1.50
    assert calculate_web_concurrency_denominator(3, session_affinity=1.35) == 1.75

    # When session affinity <= 1.0 (new chat): D = (1.0 + C) ** 2
    assert calculate_web_concurrency_denominator(0, session_affinity=1.0) == 1.0
    assert calculate_web_concurrency_denominator(1, session_affinity=1.0) == 4.0
    assert calculate_web_concurrency_denominator(2, session_affinity=1.0) == 9.0
    assert calculate_web_concurrency_denominator(3, session_affinity=1.0) == 16.0


def test_mathematical_proof_session_affinity_beats_idle_account() -> None:
    """Definitive mathematical proof that existing session with C=1 beats empty account C=0.

    Mathematical derivation:
    Account A (Open session): S_A = 1.35, C_A = 1 -> D_A = 1.0 + 0.25 * 1 = 1.25.
    Ratio_A = S_A / D_A = 1.35 / 1.25 = 1.08.

    Account B (Idle account): S_B = 1.00, C_B = 0 -> D_B = (1.0 + 0)^2 = 1.00.
    Ratio_B = S_B / D_B = 1.00 / 1.00 = 1.00.

    1.08 > 1.00 confirms that sticky session affinity is preserved!
    """
    s_affinity = 1.35
    d_affinity_c1 = calculate_web_concurrency_denominator(1, session_affinity=s_affinity)
    factor_affinity = s_affinity / d_affinity_c1
    assert pytest.approx(factor_affinity, rel=1e-5) == 1.08

    s_idle = 1.00
    d_idle_c0 = calculate_web_concurrency_denominator(0, session_affinity=s_idle)
    factor_idle = s_idle / d_idle_c0
    assert pytest.approx(factor_idle, rel=1e-5) == 1.00

    assert factor_affinity > factor_idle, "Affinity account with C=1 must beat idle account with C=0!"

    # Now verify end-to-end scores with equal usage and reset times
    now = 1000.0
    acc_open_session = make_web_account(
        account_id="web_open",
        active_requests=1,
        five_hour_pct=0.10,
        weekly_pct=0.05,
    )
    acc_idle = make_web_account(
        account_id="web_idle",
        active_requests=0,
        five_hour_pct=0.10,
        weekly_pct=0.05,
    )

    # Mock session affinity calculation to return 1.35 for acc_open_session and 1.0 for acc_idle
    with patch("core.hybrid_scheduler.calculate_session_affinity") as mock_affinity:
        def affinity_side_effect(account: WebAccount, *args: Any, **kwargs: Any) -> float:
            return 1.35 if account.id == "web_open" else 1.0

        mock_affinity.side_effect = affinity_side_effect

        score_open = calculate_web_account_score(acc_open_session, "gemini-4-argon", now=now)
        score_idle = calculate_web_account_score(acc_idle, "gemini-4-argon", now=now)

        # Ratio of scores must equal 1.08
        assert score_open > score_idle
        assert pytest.approx(score_open / score_idle, rel=1e-4) == 1.08


def test_quadratic_penalty_distributes_new_conversations() -> None:
    """Verify that new conversations (S = 1.0) strongly prefer idle accounts over C=1."""
    now = 1000.0
    acc_busy = make_web_account("web_busy", active_requests=1, five_hour_pct=0.10)
    acc_idle = make_web_account("web_idle", active_requests=0, five_hour_pct=0.10)

    # For a new conversation with S = 1.0
    score_busy = calculate_web_account_score(acc_busy, "gemini-4-argon", now=now)
    score_idle = calculate_web_account_score(acc_idle, "gemini-4-argon", now=now)

    # score_idle should be 4x higher than score_busy because D_busy = 4.0 vs D_idle = 1.0
    assert pytest.approx(score_idle / score_busy, rel=1e-4) == 4.0


# ============================================================================
# 3. Session Affinity & LMDB Integration
# ============================================================================


def test_session_affinity_lmdb_match(temp_lmdb: LMDBStore) -> None:
    """Verify calculate_session_affinity with LMDB prefix hit and character threshold."""
    messages = [
        {"role": "user", "content": "Hello, world!"},
        {"role": "assistant", "content": "Hi there! How can I help you today?"},
    ]
    prefix_hashes = compute_prefix_hashes(messages)
    # Store in LMDB for web_1
    temp_lmdb.put_session(prefix_hashes[-1], "web_1", metadata=["cid_456"])

    acc1 = make_web_account("web_1")
    acc2 = make_web_account("web_2")

    # Matched account receives 1.35
    aff1 = calculate_session_affinity(acc1, messages=messages, lmdb_store=temp_lmdb)
    assert aff1 == SESSION_AFFINITY_MULTIPLIER

    # Different account receives 1.0
    aff2 = calculate_session_affinity(acc2, messages=messages, lmdb_store=temp_lmdb)
    assert aff2 == 1.0

    # Empty messages receives 1.0
    assert calculate_session_affinity(acc1, messages=[], lmdb_store=temp_lmdb) == 1.0
    assert calculate_session_affinity(acc1, messages=None, lmdb_store=temp_lmdb) == 1.0


def test_session_affinity_rotation_on_oversized_context(temp_lmdb: LMDBStore) -> None:
    """Verify session affinity drops to 1.0 when context exceeds 120,000 characters."""
    large_turn = "x" * 125_000
    messages = [
        {"role": "user", "content": "Start turn"},
        {"role": "assistant", "content": large_turn},
    ]
    prefix_hashes = compute_prefix_hashes(messages)
    temp_lmdb.put_session(prefix_hashes[-1], "web_1", metadata=["cid_large"])

    acc1 = make_web_account("web_1")
    # Even though there is a match in LMDB, char length > 120,000 triggers rotation (S = 1.0)
    aff = calculate_session_affinity(acc1, messages=messages, lmdb_store=temp_lmdb)
    assert aff == 1.0


# ============================================================================
# 4. Eligibility Gates Tests
# ============================================================================


def test_is_oauth_account_eligible_all_gates() -> None:
    """Test all rejection conditions for is_oauth_account_eligible."""
    now = 1000.0
    base_acc = make_oauth_account("oauth_ok", max_concurrency=3)
    assert is_oauth_account_eligible(base_acc, "gemini-3.8-flash", now=now) is True

    # Gate 1: Non-ACTIVE status
    acc_suspended = make_oauth_account("oauth_susp", status="SUSPENDED")
    assert is_oauth_account_eligible(acc_suspended, "gemini-3.8-flash", now=now) is False
    acc_cooldown = make_oauth_account("oauth_cd", status="COOLDOWN")
    assert is_oauth_account_eligible(acc_cooldown, "gemini-3.8-flash", now=now) is False

    # Gate 2: Active cooldown timestamp
    acc_future_cd = make_oauth_account("oauth_fcd", cooldown_until=int(now + 60))
    assert is_oauth_account_eligible(acc_future_cd, "gemini-3.8-flash", now=now) is False
    acc_past_cd = make_oauth_account("oauth_pcd", cooldown_until=int(now - 10))
    assert is_oauth_account_eligible(acc_past_cd, "gemini-3.8-flash", now=now) is True

    # Gate 3: Concurrency cap C >= max_concurrency
    acc_max_c = make_oauth_account("oauth_c", active_requests=3, max_concurrency=3)
    assert is_oauth_account_eligible(acc_max_c, "gemini-3.8-flash", now=now) is False

    # Gate 4: Argon prohibition
    assert is_oauth_account_eligible(base_acc, "gemini-4-argon", now=now) is False
    assert is_oauth_account_eligible(base_acc, "gemini-4-argon-high", now=now) is False

    # Gate 5: Model support in discovered_models
    acc_filtered = make_oauth_account(
        "oauth_filt", models=["gemini-3.8-flash"]
    )
    assert is_oauth_account_eligible(acc_filtered, "gemini-3.8-flash", now=now) is True
    assert is_oauth_account_eligible(acc_filtered, "gemini-3.1-pro", now=now) is False

    # Gate 6: Quota exhaustion and remaining fraction < 0.02
    acc_exhausted = make_oauth_account("oauth_exh", is_exhausted=True, reset_at=int(now + 300))
    assert is_oauth_account_eligible(acc_exhausted, "gemini-3.8-flash", now=now) is False

    acc_low_quota = make_oauth_account("oauth_low", remaining_fraction=0.015, reset_at=int(now + 300))
    assert is_oauth_account_eligible(acc_low_quota, "gemini-3.8-flash", now=now) is False

    # But if reset time has passed, account becomes eligible
    acc_reset_passed = make_oauth_account("oauth_rst", remaining_fraction=0.01, reset_at=int(now - 10))
    assert is_oauth_account_eligible(acc_reset_passed, "gemini-3.8-flash", now=now) is True


def test_is_web_account_eligible_all_gates() -> None:
    """Test all rejection conditions for is_web_account_eligible."""
    now = 1000.0
    base_acc = make_web_account("web_ok", max_concurrency=2, five_hour_pct=0.20, weekly_pct=0.10)
    assert is_web_account_eligible(base_acc, "gemini-4-argon", now=now) is True

    # Gate 1: Non-ACTIVE status
    acc_suspended = make_web_account("web_susp", status="SUSPENDED")
    assert is_web_account_eligible(acc_suspended, "gemini-4-argon", now=now) is False
    acc_cooldown = make_web_account("web_cd", status="COOLDOWN")
    assert is_web_account_eligible(acc_cooldown, "gemini-4-argon", now=now) is False

    # Gate 2: Cooldown until
    acc_future_cd = make_web_account("web_fcd", cooldown_until=int(now + 60))
    assert is_web_account_eligible(acc_future_cd, "gemini-4-argon", now=now) is False

    # Gate 3: Concurrency cap C >= max_concurrency
    acc_max_c = make_web_account("web_max_c", active_requests=2, max_concurrency=2)
    assert is_web_account_eligible(acc_max_c, "gemini-4-argon", now=now) is False

    # Gate 4: 5-Hour usage ceiling (0.95)
    acc_5h_limit = make_web_account("web_5h", five_hour_pct=0.95)
    assert is_web_account_eligible(acc_5h_limit, "gemini-4-argon", now=now) is False

    acc_5h_optimistic_limit = make_web_account("web_5h_opt", five_hour_pct=0.90, optimistic_5h=0.06)
    assert is_web_account_eligible(acc_5h_optimistic_limit, "gemini-4-argon", now=now) is False

    # Gate 5: Weekly usage ceiling (0.98)
    acc_w_limit = make_web_account("web_w", weekly_pct=0.98)
    assert is_web_account_eligible(acc_w_limit, "gemini-4-argon", now=now) is False

    acc_w_opt_limit = make_web_account("web_w_opt", weekly_pct=0.95, optimistic_weekly=0.04)
    assert is_web_account_eligible(acc_w_opt_limit, "gemini-4-argon", now=now) is False

    # Gate 6: Discovered models filtering
    acc_models = make_web_account("web_m", models=["gemini-4-argon"])
    assert is_web_account_eligible(acc_models, "gemini-4-argon", now=now) is True
    assert is_web_account_eligible(acc_models, "gemini-3.8-flash", now=now) is False


# ============================================================================
# 5. Account Scoring Tests
# ============================================================================


def test_calculate_oauth_account_score() -> None:
    """Test calculate_oauth_account_score formula and rankings."""
    now = 1000.0

    # Ineligible account scores exactly 0.0
    ineligible = make_oauth_account("oauth_0", status="SUSPENDED")
    assert calculate_oauth_account_score(ineligible, "gemini-3.8-flash", now=now) == 0.0

    # Higher remaining quota beats lower remaining quota
    acc_high_q = make_oauth_account("oauth_hq", remaining_fraction=0.90, latency_ms=100.0)
    acc_low_q = make_oauth_account("oauth_lq", remaining_fraction=0.30, latency_ms=100.0)
    assert calculate_oauth_account_score(acc_high_q, "gemini-3.8-flash", now=now) > calculate_oauth_account_score(acc_low_q, "gemini-3.8-flash", now=now)

    # Lower latency beats higher latency
    acc_fast = make_oauth_account("oauth_fast", remaining_fraction=1.0, latency_ms=50.0)
    acc_slow = make_oauth_account("oauth_slow", remaining_fraction=1.0, latency_ms=500.0)
    assert calculate_oauth_account_score(acc_fast, "gemini-3.8-flash", now=now) > calculate_oauth_account_score(acc_slow, "gemini-3.8-flash", now=now)

    # Closer reset urgency increases priority (tau multiplier)
    acc_imminent_reset = make_oauth_account("oauth_imm", remaining_fraction=0.5, reset_at=int(now))
    acc_distant_reset = make_oauth_account("oauth_dist", remaining_fraction=0.5, reset_at=int(now + 18000))
    assert calculate_oauth_account_score(acc_imminent_reset, "gemini-3.8-flash", now=now) > calculate_oauth_account_score(acc_distant_reset, "gemini-3.8-flash", now=now)


def test_calculate_web_account_score() -> None:
    """Test calculate_web_account_score formula and rankings."""
    now = 1000.0

    # Ineligible account scores 0.0
    ineligible = make_web_account("web_0", status="COOLDOWN")
    assert calculate_web_account_score(ineligible, "gemini-4-argon", now=now) == 0.0

    # Usage ceiling boundary scores 0.0
    at_ceiling = make_web_account("web_ceil", five_hour_pct=0.95)
    assert calculate_web_account_score(at_ceiling, "gemini-4-argon", now=now) == 0.0

    # Account with more remaining quota margin scores higher
    acc_fresh = make_web_account("web_fresh", five_hour_pct=0.10, weekly_pct=0.10)
    acc_used = make_web_account("web_used", five_hour_pct=0.80, weekly_pct=0.10)
    assert calculate_web_account_score(acc_fresh, "gemini-4-argon", now=now) > calculate_web_account_score(acc_used, "gemini-4-argon", now=now)

    # Imminent 5-hour reset gives higher score to exhaust quota before reset
    acc_imm = make_web_account("web_imm", five_hour_pct=0.50, five_hour_reset_at=int(now))
    acc_dist = make_web_account("web_dist", five_hour_pct=0.50, five_hour_reset_at=int(now + 18000))
    assert calculate_web_account_score(acc_imm, "gemini-4-argon", now=now) > calculate_web_account_score(acc_dist, "gemini-4-argon", now=now)


# ============================================================================
# 6. Smart Pacing Tests
# ============================================================================


def test_smart_pacing_delay_scenarios() -> None:
    """Verify calculate_smart_pacing_delay under all operational scenarios."""
    now = 2000.0

    # Scenario 1: Initial request on clean account -> 0.0s delay
    acc_clean = make_web_account("web_p1", last_started=0.0, last_completed=0.0)
    assert calculate_smart_pacing_delay(acc_clean, now=now) == 0.0

    # Scenario 2: Normal long-running stream (e.g. 5.0s) completed 2.0s ago -> 0.0s delay
    # Started at 1993, completed at 1998 (duration 5s >= 1s threshold). Now is 2000.
    acc_normal_stream = make_web_account("web_p2", last_started=1993.0, last_completed=1998.0)
    assert calculate_smart_pacing_delay(acc_normal_stream, now=now) == 0.0

    # Scenario 3: Fast request (e.g. 0.2s duration) completed 0.1s ago -> cooldown required
    # Started at 1999.7, completed at 1999.9 (duration 0.2s < 1.0s). Now is 2000.0.
    # Cooldown required is WEB_MIN_INTERVAL_S (0.6s). Elapsed since done is 0.1s.
    # Expected delay: 0.6 - 0.1 = 0.5s.
    acc_fast = make_web_account("web_p3", last_started=1999.7, last_completed=1999.9)
    delay_fast = calculate_smart_pacing_delay(acc_fast, now=now, use_jitter=False)
    assert pytest.approx(delay_fast, rel=1e-4) == 0.5

    # Scenario 4: Fast request completed 0.8s ago -> cooldown already elapsed (delay 0.0s)
    acc_fast_done = make_web_account("web_p4", last_started=1999.0, last_completed=1999.2)
    assert calculate_smart_pacing_delay(acc_fast_done, now=now, use_jitter=False) == 0.0

    # Scenario 5: Rapid start-to-start interval (started 0.2s ago, WEB_MIN_START_INTERVAL_S = 0.5s)
    # Expected delay: 0.5 - 0.2 = 0.3s.
    acc_rapid = make_web_account("web_p5", last_started=1999.8, last_completed=0.0)
    delay_rapid = calculate_smart_pacing_delay(acc_rapid, now=now)
    assert pytest.approx(delay_rapid, rel=1e-4) == 0.3


@pytest.mark.asyncio
async def test_apply_smart_pacing_execution() -> None:
    """Verify apply_smart_pacing sleeps for delay and updates last_started_timestamp."""
    acc = make_web_account("web_async", last_started=100.0, last_completed=100.2)
    now = 100.3  # duration 0.2s < 1s, elapsed done = 0.1s -> delay = 0.5s

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        applied_delay = await apply_smart_pacing(acc, now=now, use_jitter=False)
        assert pytest.approx(applied_delay, rel=1e-4) == 0.5
        mock_sleep.assert_awaited_once_with(pytest.approx(0.5, rel=1e-4))
        assert acc.last_started_timestamp == now


# ============================================================================
# 7. Model Channel Preference & Optimal Account Selection Tests
# ============================================================================


def test_model_channel_preference_mapping() -> None:
    """Verify get_model_channel_preference for all model families."""
    # Argon -> Web exclusively, never OAuth
    assert get_model_channel_preference("gemini-4-argon") == ("web", None)
    assert get_model_channel_preference("gemini-4-argon-high") == ("web", None)

    # Pro -> Web first, OAuth fallback
    assert get_model_channel_preference("gemini-3.1-pro") == ("web", "oauth")
    assert get_model_channel_preference("gemini-2.5-pro-low") == ("web", "oauth")

    # Flash -> OAuth first, Web fallback
    assert get_model_channel_preference("gemini-3.8-flash") == ("oauth", "web")
    assert get_model_channel_preference("gemini-3.1-flash-lite") == ("oauth", "web")
    assert get_model_channel_preference("gemini-2.5-flash-minimal") == ("oauth", "web")


def test_select_optimal_account_flash_prefers_oauth(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Flash routes to OAuth primary, picking best score."""
    oauth1 = make_oauth_account("oauth_1", remaining_fraction=0.5)
    oauth2 = make_oauth_account("oauth_2", remaining_fraction=0.9)  # Higher score
    web1 = make_web_account("web_1")

    isolated_registry.upsert_oauth_account(oauth1)
    isolated_registry.upsert_oauth_account(oauth2)
    isolated_registry.upsert_web_account(web1)

    selected, channel = select_optimal_account(
        "gemini-3.8-flash", registry=isolated_registry
    )
    assert channel == "oauth"
    assert selected.id == "oauth_2"


def test_select_optimal_account_flash_fails_over_to_web(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Flash transparently fails over to Web when all OAuth accounts are unavailable."""
    oauth_exhausted = make_oauth_account("oauth_exh", is_exhausted=True)
    oauth_cooldown = make_oauth_account("oauth_cd", status="COOLDOWN")
    web_ok = make_web_account("web_ok", five_hour_pct=0.10)

    isolated_registry.upsert_oauth_account(oauth_exhausted)
    isolated_registry.upsert_oauth_account(oauth_cooldown)
    isolated_registry.upsert_web_account(web_ok)

    selected, channel = select_optimal_account(
        "gemini-3.8-flash", allow_cross_channel=True, registry=isolated_registry
    )
    assert channel == "web"
    assert selected.id == "web_ok"


def test_select_optimal_account_argon_exclusively_web(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Argon routes to Web and strictly NEVER falls back to OAuth."""
    oauth_ok = make_oauth_account("oauth_healthy", remaining_fraction=1.0)
    web_ok = make_web_account("web_argon", five_hour_pct=0.10)

    isolated_registry.upsert_oauth_account(oauth_ok)
    isolated_registry.upsert_web_account(web_ok)

    # Routes to Web
    selected, channel = select_optimal_account(
        "gemini-4-argon", registry=isolated_registry
    )
    assert channel == "web"
    assert selected.id == "web_argon"

    # When Web is exhausted, Argon MUST NOT fall back to OAuth, raising NoEligibleAccountError
    web_ok.usage.five_hour_pct = 0.96  # Exceeds ceiling
    with pytest.raises(NoEligibleAccountError):
        select_optimal_account(
            "gemini-4-argon",
            allow_cross_channel=True,
            registry=isolated_registry,
        )


def test_select_optimal_account_pro_prefers_web_then_oauth(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Pro routes to Web primary, falling back to OAuth if Web is at capacity."""
    oauth_ok = make_oauth_account("oauth_pro", remaining_fraction=0.8)
    web_ok = make_web_account("web_pro", five_hour_pct=0.20)

    isolated_registry.upsert_oauth_account(oauth_ok)
    isolated_registry.upsert_web_account(web_ok)

    # 1. Primary: selects Web
    selected, channel = select_optimal_account(
        "gemini-3.1-pro", registry=isolated_registry
    )
    assert channel == "web"
    assert selected.id == "web_pro"

    # 2. Web reaches max concurrency (C = 2) -> falls back to OAuth
    web_ok.active_requests = 2
    selected_failover, channel_failover = select_optimal_account(
        "gemini-3.1-pro", allow_cross_channel=True, registry=isolated_registry
    )
    assert channel_failover == "oauth"
    assert selected_failover.id == "oauth_pro"


def test_select_optimal_account_excluded_ids(isolated_registry: AccountsStateRegistry) -> None:
    """Verify excluded_ids accounts are skipped during selection."""
    oauth1 = make_oauth_account("oauth_1", remaining_fraction=0.9)
    oauth2 = make_oauth_account("oauth_2", remaining_fraction=0.7)

    isolated_registry.upsert_oauth_account(oauth1)
    isolated_registry.upsert_oauth_account(oauth2)

    selected, channel = select_optimal_account(
        "gemini-3.8-flash",
        excluded_ids={"oauth_1"},
        registry=isolated_registry,
    )
    assert channel == "oauth"
    assert selected.id == "oauth_2"


def test_select_optimal_account_disallows_cross_channel(isolated_registry: AccountsStateRegistry) -> None:
    """Verify allow_cross_channel=False suppresses failover to secondary pool."""
    oauth_exhausted = make_oauth_account("oauth_exh", is_exhausted=True)
    web_ok = make_web_account("web_ok")

    isolated_registry.upsert_oauth_account(oauth_exhausted)
    isolated_registry.upsert_web_account(web_ok)

    with pytest.raises(NoEligibleAccountError):
        select_optimal_account(
            "gemini-3.8-flash",
            allow_cross_channel=False,
            registry=isolated_registry,
        )


def test_select_optimal_account_no_accounts_raises(isolated_registry: AccountsStateRegistry) -> None:
    """Verify NoEligibleAccountError raised when pool is completely empty or exhausted."""
    with pytest.raises(NoEligibleAccountError) as exc_info:
        select_optimal_account("gemini-3.8-flash", registry=isolated_registry)
    assert "No eligible account found" in str(exc_info.value)
