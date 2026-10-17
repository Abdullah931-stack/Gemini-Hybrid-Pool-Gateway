"""Unit and integration test suite for core/usage_monitor.py.

Verifies:
1. Context-scaled optimistic delta calculation against mathematical specification.
2. QuotaResetDelay parsing across various duration string formats.
3. Intelligent 429 classification (quota exhaustion cooldown vs burst limit handling).
4. Account suspension on 403 / Terms of Service violation.
5. RPC jSf9Qc XSSI extraction, tier mapping, and usage parsing.
6. Drift-safe reset detection zeroing optimistic deltas upon expiry or live query.
7. Record successful request updating EMA latency and context deltas.
8. Full central sync pass across OAuth and Web accounts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config.settings import (
    AccountsConfig,
    AccountsStateRegistry,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    WebUsage,
    state_registry,
)
from core.usage_monitor import (
    JSF9QC_RPC_ID,
    OAuth429Classification,
    UsageMonitor,
    WebUsageData,
    calculate_optimistic_5h_delta,
    calculate_optimistic_weekly_delta,
    check_and_apply_drift_safe_resets,
    classify_and_handle_oauth_429,
    extract_quota_reset_delay_from_error,
    handle_account_suspension,
    is_suspension_error,
    parse_jsf9qc_usage_response,
    parse_quota_reset_delay,
    record_successful_request,
    strip_xssi_prefix,
    sync_all_registered_accounts,
    sync_oauth_account_quotas,
    sync_web_account_usage,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_in_memory_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate and reset central in-memory state before each test."""
    dummy_accounts_file = tmp_path / "accounts.json"
    dummy_accounts_file.write_text(json.dumps({"oauth_accounts": [], "web_accounts": []}), encoding="utf-8")
    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", dummy_accounts_file)
    state_registry.config = AccountsConfig()
    yield
    state_registry.config = AccountsConfig()


@pytest.fixture
def sample_oauth_account() -> OAuthAccount:
    """Fixture providing a standard test OAuthAccount."""
    return OAuthAccount(
        id="oauth-test-1",
        label="Test OAuth CLI Account",
        credentials=OAuthCredentials(
            access_token="valid-token-123",
            refresh_token="valid-refresh-123",
            expires_at=int(time.time() + 3600),
            project_id="test-project-1",
        ),
        status="ACTIVE",
        discovered_models=["gemini-3.8-flash", "gemini-3.1-pro"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-3.8-flash": ModelQuota(remaining_fraction=1.0, is_exhausted=False),
                "gemini-3.1-pro": ModelQuota(remaining_fraction=1.0, is_exhausted=False),
            }
        ),
    )


@pytest.fixture
def sample_web_account() -> WebAccount:
    """Fixture providing a standard test WebAccount."""
    return WebAccount(
        id="web-test-1",
        label="Test Web Cookie Account",
        cookies=WebCookies(secure_1psid="psid_test", secure_1psidts="psidts_test"),
        status="ACTIVE",
        discovered_models=["gemini-4-argon", "gemini-3.1-pro"],
        usage=WebUsage(
            five_hour_pct=0.20,
            five_hour_reset_at=int(time.time() + 7200),
            weekly_pct=0.10,
            weekly_reset_at=int(time.time() + 86400 * 5),
            tier="PRO",
        ),
    )


# ============================================================================
# 1. Context-Scaled Optimistic Delta Tests
# ============================================================================


def test_calculate_optimistic_5h_delta_exact_formula() -> None:
    """Verify Delta_5h(m, L_chars) matches the mathematical specification exactly."""
    # Argon base is 0.10
    assert calculate_optimistic_5h_delta("gemini-4-argon", 0) == pytest.approx(0.10)
    # At 25,000 chars: factor is 1.0 + 0.25 * (25000 / 25000) = 1.25 -> 0.10 * 1.25 = 0.125
    assert calculate_optimistic_5h_delta("gemini-4-argon", 25000) == pytest.approx(0.125)
    # At 50,000 chars: factor is 1.0 + 0.25 * (50000 / 25000) = 1.50 -> 0.10 * 1.50 = 0.150
    assert calculate_optimistic_5h_delta("gemini-4-argon", 50000) == pytest.approx(0.150)

    # Pro base is 0.03
    assert calculate_optimistic_5h_delta("gemini-3.1-pro", 0) == pytest.approx(0.03)
    assert calculate_optimistic_5h_delta("gemini-3.1-pro", 25000) == pytest.approx(0.0375)

    # Flash base is 0.002
    assert calculate_optimistic_5h_delta("gemini-3.8-flash", 0) == pytest.approx(0.002)

    # Flash-lite base is 0.000 (free compute)
    assert calculate_optimistic_5h_delta("gemini-3.1-flash-lite", 100000) == pytest.approx(0.0)

    # Negative chars clamped to 0
    assert calculate_optimistic_5h_delta("gemini-4-argon", -500) == pytest.approx(0.10)


def test_calculate_optimistic_weekly_delta() -> None:
    """Verify weekly optimistic delta scaling."""
    assert calculate_optimistic_weekly_delta("gemini-4-argon", 0) == pytest.approx(0.025)
    assert calculate_optimistic_weekly_delta("gemini-3.1-pro", 0) == pytest.approx(0.0075)


# ============================================================================
# 2. Quota Reset Delay Parsing Tests
# ============================================================================


def test_parse_quota_reset_delay_various_formats() -> None:
    """Verify parse_quota_reset_delay handles standard Google duration strings."""
    # Hours, minutes, seconds combination: "4h30m28s"
    # 4*3600 + 30*60 + 28 = 14400 + 1800 + 28 = 16228s
    assert parse_quota_reset_delay("4h30m28s") == pytest.approx(16228.0)

    # Pure seconds: "3600s"
    assert parse_quota_reset_delay("3600s") == pytest.approx(3600.0)

    # Pure minutes: "15m"
    assert parse_quota_reset_delay("15m") == pytest.approx(900.0)

    # Float seconds: "1.5s"
    assert parse_quota_reset_delay("1.5s") == pytest.approx(1.5)

    # Milliseconds: "500ms"
    assert parse_quota_reset_delay("500ms") == pytest.approx(0.5)

    # Days and hours: "1d2h" -> 86400 + 7200 = 93600s
    assert parse_quota_reset_delay("1d2h") == pytest.approx(93600.0)

    # Numeric int and float
    assert parse_quota_reset_delay(120) == pytest.approx(120.0)
    assert parse_quota_reset_delay(45.5) == pytest.approx(45.5)
    assert parse_quota_reset_delay("300") == pytest.approx(300.0)

    # Invalid / empty
    assert parse_quota_reset_delay("") is None
    assert parse_quota_reset_delay(None) is None
    assert parse_quota_reset_delay("invalid-delay") is None


def test_extract_quota_reset_delay_from_error_nested() -> None:
    """Verify recursive extraction from complex nested Google error structures."""
    nested_error = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "subject": "project:test-project-1",
                            "quotaResetDelay": "4h30m28s",
                        }
                    ],
                }
            ],
        }
    }
    assert extract_quota_reset_delay_from_error(nested_error) == pytest.approx(16228.0)

    # ErrorInfo format
    metadata_error = {
        "error": {
            "details": [
                {
                    "metadata": {
                        "quotaResetDelay": "3600s",
                    }
                }
            ]
        }
    }
    assert extract_quota_reset_delay_from_error(metadata_error) == pytest.approx(3600.0)

    # Raw string containing JSON or key-value
    raw_str = '{"status": 429, "error": "rate limit", "quotaResetDelay": "15m"}'
    assert extract_quota_reset_delay_from_error(raw_str) == pytest.approx(900.0)

    # Missing delay returns None
    assert extract_quota_reset_delay_from_error({"error": {"code": 429}}) is None


# ============================================================================
# 3. Intelligent 429 Classifier Tests
# ============================================================================


@pytest.mark.asyncio
async def test_classify_and_handle_oauth_429_genuine_exhaustion(
    sample_oauth_account: OAuthAccount,
) -> None:
    """Verify genuine quota exhaustion triggers COOLDOWN and sets cooldown_until."""
    now = 1000000.0
    error_payload = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"quotaResetDelay": "3600s"}],
        }
    }

    classification = await classify_and_handle_oauth_429(
        sample_oauth_account,
        error_payload,
        now=now,
        persist=True,
    )

    assert classification.is_exhaustion is True
    assert classification.delay_seconds == pytest.approx(3600.0)
    assert classification.cooldown_until == 1003600
    assert sample_oauth_account.status == "COOLDOWN"
    assert sample_oauth_account.quota_state.cooldown_until == 1003600
    assert "Quota exhausted" in sample_oauth_account.quota_state.last_error_reason


@pytest.mark.asyncio
async def test_classify_and_handle_oauth_429_transient_burst(
    sample_oauth_account: OAuthAccount,
) -> None:
    """Verify transient burst limit does NOT place account into long COOLDOWN."""
    now = 1000000.0
    # No quotaResetDelay
    transient_error = {
        "error": {
            "code": 429,
            "message": "Too Many Requests",
            "status": "RESOURCE_EXHAUSTED",
        }
    }

    classification = await classify_and_handle_oauth_429(
        sample_oauth_account,
        transient_error,
        now=now,
        persist=True,
    )

    assert classification.is_exhaustion is False
    assert classification.delay_seconds is None
    assert classification.cooldown_until is None
    # Account status must remain ACTIVE to avoid penalizing it with a long freeze
    assert sample_oauth_account.status == "ACTIVE"
    assert sample_oauth_account.quota_state.cooldown_until is None
    assert sample_oauth_account.quota_state.consecutive_errors == 1
    assert "Transient burst" in sample_oauth_account.quota_state.last_error_reason


# ============================================================================
# 4. Account Suspension Tests
# ============================================================================


@pytest.mark.asyncio
async def test_handle_account_suspension(sample_oauth_account: OAuthAccount) -> None:
    """Verify permanent suspension sets status to SUSPENDED immediately."""
    await handle_account_suspension(
        sample_oauth_account,
        "Terms of Service violation detected",
        persist=True,
    )

    assert sample_oauth_account.status == "SUSPENDED"
    assert "Terms of Service" in sample_oauth_account.quota_state.last_error_reason


def test_is_suspension_error() -> None:
    """Verify identification of suspension patterns."""
    assert is_suspension_error("User has violated Terms of Service") is True
    assert is_suspension_error({"error": {"message": "Account disabled due to abuse"}}) is True
    assert is_suspension_error("Temporary 500 Internal Error") is False


# ============================================================================
# 5. RPC jSf9Qc XSSI Response Parsing Tests
# ============================================================================


def test_strip_xssi_prefix() -> None:
    """Verify XSSI prefix stripping."""
    assert strip_xssi_prefix(")]}'\n[1, 2, 3]") == "[1, 2, 3]"
    assert strip_xssi_prefix(")]}'[1, 2, 3]") == "[1, 2, 3]"
    assert strip_xssi_prefix("[1, 2, 3]") == "[1, 2, 3]"


def test_parse_jsf9qc_usage_response_full_batchexecute() -> None:
    """Verify parsing of raw Google batchexecute response containing jSf9Qc."""
    part_payload = json.dumps([
        2,  # tier_id: 2 -> PRO
        [
            [100, 0.35, 1, [[1700003600]]],  # 5h window: 35%, reset at 1700003600
            [1000, 0.12, 2, [[1700500000]]],  # Weekly window: 12%, reset at 1700500000
            [50, 0, 3, None],  # AI Credits: 50 remaining
        ],
        True,  # use_overage_credits
    ])
    raw_batchexecute_text = (
        ")]}'\n"
        + json.dumps([
            ["wrb.fr", "jSf9Qc", part_payload, None, None, None, "generic"]
        ])
    )

    usage = parse_jsf9qc_usage_response(raw_batchexecute_text)

    assert usage.tier == "PRO"
    assert usage.five_hour_pct == pytest.approx(0.35)
    assert usage.five_hour_reset_at == 1700003600
    assert usage.weekly_pct == pytest.approx(0.12)
    assert usage.weekly_reset_at == 1700500000
    assert usage.ai_credits_remaining == pytest.approx(50.0)
    assert usage.use_overage_credits is True


def test_parse_jsf9qc_usage_response_dict() -> None:
    """Verify parsing when input is a client._usage_info dictionary."""
    data = {
        "tier": {"id": 3, "label": "ULTRA"},
        "current_5h": {
            "usage_level": 0.45,
            "reset_at": "2026-10-17T05:00:00+00:00",
        },
        "weekly": {
            "usage_percentage": 20,
            "reset_at": 1700500000,
        },
        "ai_credits_remaining": 150.0,
    }

    usage = parse_jsf9qc_usage_response(data)

    assert usage.tier == "ULTRA"
    assert usage.five_hour_pct == pytest.approx(0.45)
    assert usage.weekly_pct == pytest.approx(0.20)
    assert usage.weekly_reset_at == 1700500000
    assert usage.ai_credits_remaining == pytest.approx(150.0)


# ============================================================================
# 6. Drift-Safe Reset Detection Tests
# ============================================================================


def test_check_and_apply_drift_safe_resets(sample_web_account: WebAccount) -> None:
    """Verify optimistic deltas are zeroed when current time crosses reset time."""
    sample_web_account.optimistic_5h_delta = 0.25
    sample_web_account.optimistic_weekly_delta = 0.05
    sample_web_account.usage.five_hour_reset_at = 1000
    sample_web_account.usage.weekly_reset_at = 2000

    # Before 5h reset: deltas remain intact
    changed = check_and_apply_drift_safe_resets(sample_web_account, now=999.0)
    assert changed is False
    assert sample_web_account.optimistic_5h_delta == pytest.approx(0.25)

    # After 5h reset (now=1001 >= 1000): 5h delta zeroed, weekly untouched
    changed = check_and_apply_drift_safe_resets(sample_web_account, now=1001.0)
    assert changed is True
    assert sample_web_account.optimistic_5h_delta == pytest.approx(0.0)
    assert sample_web_account.optimistic_weekly_delta == pytest.approx(0.05)

    # After weekly reset (now=2005 >= 2000): weekly delta zeroed
    changed = check_and_apply_drift_safe_resets(sample_web_account, now=2005.0)
    assert changed is True
    assert sample_web_account.optimistic_weekly_delta == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_sync_web_account_usage_live_query_resets_deltas(
    sample_web_account: WebAccount,
) -> None:
    """Verify successful live RPC query zeroes optimistic deltas immediately."""
    sample_web_account.optimistic_5h_delta = 0.30
    sample_web_account.optimistic_weekly_delta = 0.08

    mock_client = MagicMock()
    mock_client._fetch_usage_info = AsyncMock()
    mock_client._usage_info = {
        "tier": {"id": 2, "label": "PRO"},
        "current_5h": {"usage_level": 0.50, "reset_at": 1700000000},
        "weekly": {"usage_level": 0.15, "reset_at": 1700500000},
    }

    result = await sync_web_account_usage(
        sample_web_account,
        client=mock_client,
        persist=True,
    )

    assert result.five_hour_pct == pytest.approx(0.50)
    assert sample_web_account.usage.five_hour_pct == pytest.approx(0.50)
    # Drift-Safe Reset: optimistic deltas zeroed upon live RPC update
    assert sample_web_account.optimistic_5h_delta == pytest.approx(0.0)
    assert sample_web_account.optimistic_weekly_delta == pytest.approx(0.0)


# ============================================================================
# 7. Record Successful Request Tests
# ============================================================================


def test_record_successful_request_oauth_latency_ema(
    sample_oauth_account: OAuthAccount,
) -> None:
    """Verify EMA latency updating for OAuth account."""
    # First request initializes recent_latency_ms
    record_successful_request(sample_oauth_account, "gemini-3.8-flash", latency_ms=100.0)
    assert sample_oauth_account.recent_latency_ms == pytest.approx(100.0)

    # Second request updates EMA: 0.8 * 100 + 0.2 * 200 = 80 + 40 = 120.0
    record_successful_request(sample_oauth_account, "gemini-3.8-flash", latency_ms=200.0)
    assert sample_oauth_account.recent_latency_ms == pytest.approx(120.0)


def test_record_successful_request_web_accumulates_deltas(
    sample_web_account: WebAccount,
) -> None:
    """Verify context-scaled delta accumulation for Web account."""
    sample_web_account.optimistic_5h_delta = 0.0
    # Argon base is 0.10, at 25,000 chars -> delta is 0.125
    record_successful_request(
        sample_web_account,
        "gemini-4-argon",
        latency_ms=1500.0,
        context_chars=25000,
        now=5000.0,
    )

    assert sample_web_account.optimistic_5h_delta == pytest.approx(0.125)
    assert sample_web_account.optimistic_weekly_delta == pytest.approx(0.125 * 0.25)
    assert sample_web_account.last_completed_timestamp == pytest.approx(5000.0)


def test_record_successful_request_web_drift_safe_reset_precedes_accumulation(
    sample_web_account: WebAccount,
) -> None:
    """Verify drift-safe reset runs BEFORE accumulating new deltas.

    If current_time >= five_hour_reset_at, prior optimistic_5h_delta is cleared first,
    and then the newly computed request delta is added (preserving the new consumption).
    """
    sample_web_account.usage.five_hour_reset_at = 1000
    sample_web_account.optimistic_5h_delta = 0.40
    sample_web_account.usage.weekly_reset_at = 5000
    sample_web_account.optimistic_weekly_delta = 0.10

    # Request executes at now=1005 (past 5h reset, but before weekly reset)
    record_successful_request(
        sample_web_account,
        "gemini-4-argon",
        latency_ms=1000.0,
        context_chars=0,
        now=1005.0,
    )

    # 5h delta was reset to 0.0 first, then argon base delta (0.10) was added -> 0.10
    assert sample_web_account.optimistic_5h_delta == pytest.approx(0.10)
    # Weekly delta was NOT reset (1005 < 5000), so 0.10 + (0.10 * 0.25) = 0.125
    assert sample_web_account.optimistic_weekly_delta == pytest.approx(0.125)


# ============================================================================
# 8. Central Sync Pass Tests
# ============================================================================


@pytest.mark.asyncio
async def test_sync_all_registered_accounts_lifecycle(
    sample_oauth_account: OAuthAccount,
    sample_web_account: WebAccount,
) -> None:
    """Verify complete central sync pass recovers expired cooldowns and updates state."""
    registry = AccountsStateRegistry()
    registry.upsert_oauth_account(sample_oauth_account)
    registry.upsert_web_account(sample_web_account)

    # Place OAuth account in COOLDOWN that has now expired
    sample_oauth_account.status = "COOLDOWN"
    sample_oauth_account.quota_state.cooldown_until = 1000

    # Place Web account in COOLDOWN that has now expired
    sample_web_account.status = "COOLDOWN"
    sample_web_account.cooldown_until = 1000

    now = 1050.0  # past 1000

    with patch("core.usage_monitor.sync_oauth_account_quotas", new_callable=AsyncMock) as mock_oauth_sync, \
         patch("core.usage_monitor.sync_web_account_usage", new_callable=AsyncMock) as mock_web_sync:

        mock_oauth_sync.return_value = ["gemini-3.8-flash"]
        mock_web_sync.return_value = WebUsageData(tier="PRO")

        summary = await sync_all_registered_accounts(
            registry,
            persist=True,
            now=now,
        )

        assert summary["synced_oauth_count"] == 1
        assert summary["synced_web_count"] == 1
        assert sample_oauth_account.status == "ACTIVE"
        assert sample_web_account.status == "ACTIVE"


# ============================================================================
# 9. UsageMonitor Service Class Tests
# ============================================================================


@pytest.mark.asyncio
async def test_usage_monitor_service_wrapper(
    sample_oauth_account: OAuthAccount,
    sample_web_account: WebAccount,
) -> None:
    """Verify UsageMonitor service class coordinates monitoring operations."""
    registry = AccountsStateRegistry()
    registry.upsert_oauth_account(sample_oauth_account)
    registry.upsert_web_account(sample_web_account)

    monitor = UsageMonitor(registry=registry)

    # Verify static methods
    assert monitor.calculate_5h_delta("gemini-4-argon", 0) == pytest.approx(0.10)
    assert monitor.parse_delay("15m") == pytest.approx(900.0)

    # Verify suspension
    await monitor.suspend_account(sample_oauth_account, "Testing suspension")
    assert sample_oauth_account.status == "SUSPENDED"
