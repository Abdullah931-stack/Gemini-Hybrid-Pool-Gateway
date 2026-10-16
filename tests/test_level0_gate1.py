"""Verification Gate 1 test suite for Level 0 (`config/settings.py`).

Covers:
1. Mathematical & engineering constants and longest-prefix model weight resolution.
2. Pydantic v2 structural contracts, per-account lock isolation, and zero runtime state leakage.
3. In-memory Single Source of Truth (`AccountsStateRegistry`) and lock-preserving hot-plug upserts.
4. High-concurrency (50-task) atomic snapshot persistence (`save_accounts_snapshot`) under `accounts_file_lock`.
5. Dual-table LMDB (`db_sessions` and `db_signatures`) CRUD, account-scoped pruning, and disk persistence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    ACCOUNTS_FILE_PATH,
    DELTA_BASE,
    OAUTH_MAX_CONCURRENCY_DEFAULT,
    OAUTH_MIN_REMAINING_FRACTION,
    REHYDRATION_TIER2_MAX_CHARS,
    SESSION_AFFINITY_MAX_CONTEXT_CHARS,
    SESSION_AFFINITY_MULTIPLIER,
    WEB_FAST_REQUEST_THRESHOLD_S,
    WEB_JITTER_RANGE_S,
    WEB_MAX_5H_USAGE_PCT,
    WEB_MAX_CONCURRENCY_DEFAULT,
    WEB_MAX_WEEKLY_USAGE_PCT,
    WEB_MIN_INTERVAL_S,
    WEB_MIN_START_INTERVAL_S,
    WEB_URGENCY_WINDOW_S,
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
    get_model_base_delta,
    load_accounts_into_memory,
    save_accounts_snapshot,
    state_registry,
)


# ============================================================================
# 1. Mathematical Constants & Longest-Prefix Model Delta Resolution
# ============================================================================


def test_architectural_constants_match_specification() -> None:
    """Verify Level 0 mathematical and scheduling constants match docs 01 & 03."""
    assert DELTA_BASE == {
        "gemini-4-argon": 0.10,
        "gemini-3.1-pro": 0.03,
        "gemini-3.8-flash": 0.002,
        "gemini-3.1-flash-lite": 0.000,
    }
    assert OAUTH_MAX_CONCURRENCY_DEFAULT == 3
    assert WEB_MAX_CONCURRENCY_DEFAULT == 2
    assert WEB_MIN_START_INTERVAL_S == 0.5
    assert WEB_FAST_REQUEST_THRESHOLD_S == 1.0
    assert WEB_MIN_INTERVAL_S == 0.6
    assert WEB_JITTER_RANGE_S == (0.4, 0.8)
    assert SESSION_AFFINITY_MULTIPLIER == 1.35
    assert SESSION_AFFINITY_MAX_CONTEXT_CHARS == 120_000
    assert REHYDRATION_TIER2_MAX_CHARS == 60_000
    assert OAUTH_MIN_REMAINING_FRACTION == 0.02
    assert WEB_MAX_5H_USAGE_PCT == 0.95
    assert WEB_MAX_WEEKLY_USAGE_PCT == 0.98
    assert WEB_URGENCY_WINDOW_S == 18_000


@pytest.mark.parametrize(
    ("model_name", "expected_delta"),
    [
        ("gemini-4-argon", 0.10),
        ("gemini-4-argon-thinking", 0.10),
        ("gemini-3.1-pro", 0.03),
        ("gemini-3.1-pro-high", 0.03),
        ("gemini-3.8-flash", 0.002),
        ("gemini-3.8-flash-medium", 0.002),
        ("gemini-3.1-flash-lite", 0.000),
        ("gemini-3.1-flash-lite-low", 0.000),
        ("  GEMINI-3.1-FLASH-LITE-HIGH  ", 0.000),
        ("custom-flash-lite-experimental", 0.000),
        ("custom-argon-preview", 0.10),
        ("custom-pro-preview", 0.03),
        ("unknown-future-model", 0.002),
    ],
)
def test_model_base_delta_resolves_longest_prefix_and_variants(
    model_name: str,
    expected_delta: float,
) -> None:
    """Ensure longest-prefix matching distinguishes `gemini-3.1-flash-lite` from `gemini-3.1-pro`."""
    assert get_model_base_delta(model_name) == expected_delta


# ============================================================================
# 2. Pydantic v2 Contracts, Runtime Lock Isolation, and Zero State Leakage
# ============================================================================


def test_per_account_and_per_conversation_locks_are_isolated() -> None:
    """Each OAuth/Web account and conversation `cid` must own an independent asyncio.Lock."""
    oauth_a = OAuthAccount(id="oauth-a", label="OAuth A")
    oauth_b = OAuthAccount(id="oauth-b", label="OAuth B")
    assert isinstance(oauth_a.token_refresh_lock, asyncio.Lock)
    assert oauth_a.token_refresh_lock is not oauth_b.token_refresh_lock

    web_a = WebAccount(id="web-a", label="Web A")
    web_b = WebAccount(id="web-b", label="Web B")
    assert isinstance(web_a.cookie_write_lock, asyncio.Lock)
    assert isinstance(web_a.reinit_lock, asyncio.Lock)
    assert web_a.cookie_write_lock is not web_b.cookie_write_lock
    assert web_a.reinit_lock is not web_b.reinit_lock
    assert web_a.cookie_write_lock is not web_a.reinit_lock

    cid_1_lock = web_a.get_conversation_lock("cid-100")
    cid_2_lock = web_a.get_conversation_lock("cid-200")
    assert isinstance(cid_1_lock, asyncio.Lock)
    assert cid_1_lock is web_a.get_conversation_lock("cid-100")
    assert cid_1_lock is not cid_2_lock
    assert cid_1_lock is not web_b.get_conversation_lock("cid-100")


def test_serialized_accounts_match_schema_and_exclude_runtime_state() -> None:
    """`model_dump(mode='json', by_alias=True)` must match doc 01 and omit all PrivateAttr fields."""
    oauth_acc = OAuthAccount(
        id="oauth-1",
        label="Primary CLI",
        transport_mode="http_v1internal",
        max_concurrency=3,
        proxy="http://127.0.0.1:8080",
        credentials=OAuthCredentials(
            access_token="ya29.tok",
            refresh_token="1//ref",
            expires_at=1_750_000_000,
            project_id="cloud-proj-1",
            ide_type="ANTIGRAVITY",
        ),
        status="ACTIVE",
        discovered_models=["gemini-3.8-flash", "gemini-3.1-pro"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-3.8-flash": ModelQuota(
                    remaining_fraction=0.85,
                    reset_at=1_750_003_600,
                    is_exhausted=False,
                )
            },
            cooldown_until=None,
            consecutive_errors=0,
            last_error_reason=None,
        ),
    )
    oauth_acc.active_requests = 2
    oauth_acc.recent_latency_ms = 145.5

    web_acc = WebAccount(
        id="web-1",
        label="Pro Web",
        max_concurrency=2,
        proxy=None,
        cookie_file="config/cookies_cache/web-1.json",
        cookies=WebCookies(
            **{
                "__Secure-1PSID": "psid-val",
                "__Secure-1PSIDTS": "psidts-val",
            }
        ),
        status="ACTIVE",
        discovered_models=["gemini-4-argon"],
        usage=WebUsage(
            five_hour_pct=0.25,
            five_hour_reset_at=1_750_010_000,
            weekly_pct=0.12,
            weekly_reset_at=1_750_500_000,
            last_synced_at=1_750_000_100,
        ),
    )
    web_acc.active_requests = 1
    web_acc.last_started_timestamp = 1_750_000_050.0
    web_acc.last_completed_timestamp = 1_750_000_055.0
    web_acc.optimistic_5h_delta = 0.10
    web_acc.optimistic_weekly_delta = 0.02
    _ = web_acc.get_conversation_lock("cid-xyz")

    config = AccountsConfig(oauth_accounts=[oauth_acc], web_accounts=[web_acc])
    dumped = config.model_dump(mode="json", by_alias=True)

    oauth_dumped = dumped["oauth_accounts"][0]
    assert set(oauth_dumped.keys()) == {
        "id",
        "label",
        "transport_mode",
        "max_concurrency",
        "proxy",
        "credentials",
        "status",
        "discovered_models",
        "quota_state",
    }
    for forbidden_key in (
        "active_requests",
        "_active_requests",
        "recent_latency_ms",
        "_recent_latency_ms",
        "token_refresh_lock",
        "_token_refresh_lock",
    ):
        assert forbidden_key not in oauth_dumped

    web_dumped = dumped["web_accounts"][0]
    assert set(web_dumped.keys()) == {
        "id",
        "label",
        "max_concurrency",
        "proxy",
        "cookie_file",
        "cookies",
        "status",
        "discovered_models",
        "usage",
        "cooldown_until",
        "consecutive_errors",
        "last_error_reason",
    }
    assert web_dumped["cookies"] == {
        "__Secure-1PSID": "psid-val",
        "__Secure-1PSIDTS": "psidts-val",
    }
    for forbidden_key in (
        "active_requests",
        "_active_requests",
        "cookie_write_lock",
        "conversation_locks",
        "reinit_lock",
        "last_started_timestamp",
        "last_completed_timestamp",
        "optimistic_5h_delta",
        "optimistic_weekly_delta",
    ):
        assert forbidden_key not in web_dumped


# ============================================================================
# 3. In-Memory Single Source of Truth & Hot-Plug Upsert Preservation
# ============================================================================


def test_load_project_accounts_json_and_hot_plug_upsert_preserves_runtime_state() -> None:
    """Loading `config/accounts.json` and upserting accounts must preserve live locks and counters."""
    loaded_cfg = load_accounts_into_memory(ACCOUNTS_FILE_PATH)
    assert isinstance(loaded_cfg, AccountsConfig)

    registry = AccountsStateRegistry()
    initial_oauth = OAuthAccount(id="oauth-hot", label="Initial Label")
    initial_oauth.active_requests = 2
    initial_oauth.recent_latency_ms = 88.0
    original_refresh_lock = initial_oauth.token_refresh_lock
    registry.upsert_oauth_account(initial_oauth)

    updated_oauth = OAuthAccount(
        id="oauth-hot",
        label="Updated Label",
        discovered_models=["gemini-3.8-flash"],
    )
    registry.upsert_oauth_account(updated_oauth)

    stored_oauth = registry.get_oauth_account("oauth-hot")
    assert stored_oauth is not None
    assert stored_oauth.label == "Updated Label"
    assert stored_oauth.active_requests == 2
    assert stored_oauth.recent_latency_ms == 88.0
    assert stored_oauth.token_refresh_lock is original_refresh_lock

    initial_web = WebAccount(id="web-hot", label="Initial Web")
    initial_web.active_requests = 1
    initial_web.optimistic_5h_delta = 0.06
    initial_web.optimistic_weekly_delta = 0.01
    initial_web.last_started_timestamp = 100.5
    initial_web.last_completed_timestamp = 102.0
    original_cookie_lock = initial_web.cookie_write_lock
    original_reinit_lock = initial_web.reinit_lock
    original_cid_lock = initial_web.get_conversation_lock("cid-keep")
    registry.upsert_web_account(initial_web)

    updated_web = WebAccount(
        id="web-hot",
        label="Updated Web",
        cookies=WebCookies(**{"__Secure-1PSID": "new-psid", "__Secure-1PSIDTS": "new-ts"}),
    )
    registry.upsert_web_account(updated_web)

    stored_web = registry.get_web_account("web-hot")
    assert stored_web is not None
    assert stored_web.label == "Updated Web"
    assert stored_web.cookies["__Secure-1PSID"] == "new-psid"
    assert stored_web.active_requests == 1
    assert stored_web.optimistic_5h_delta == 0.06
    assert stored_web.optimistic_weekly_delta == 0.01
    assert stored_web.last_started_timestamp == 100.5
    assert stored_web.last_completed_timestamp == 102.0
    assert stored_web.cookie_write_lock is original_cookie_lock
    assert stored_web.reinit_lock is original_reinit_lock
    assert stored_web.get_conversation_lock("cid-keep") is original_cid_lock


# ============================================================================
# 4. High-Concurrency Stress Test (50 Tasks) for `save_accounts_snapshot()`
# ============================================================================


@pytest.mark.asyncio
async def test_50_concurrent_save_accounts_snapshot_calls_are_atomic_and_race_free(
    tmp_path: Path,
) -> None:
    """50 concurrent mutations + snapshots must not raise PermissionError, leave .tmp, or corrupt JSON."""
    snapshot_file = tmp_path / "accounts.json"
    tmp_file = tmp_path / "accounts.json.tmp"
    original_config = state_registry.config

    try:
        state_registry.config = AccountsConfig(
            oauth_accounts=[OAuthAccount(id="oauth-stress", label="Stress OAuth")],
            web_accounts=[
                WebAccount(
                    id="web-stress",
                    label="Stress Web",
                    cookies=WebCookies(
                        **{"__Secure-1PSID": "init-psid", "__Secure-1PSIDTS": "init-ts"}
                    ),
                )
            ],
        )

        async def mutate_and_persist(worker_id: int) -> None:
            oauth_acc = state_registry.get_oauth_account("oauth-stress")
            web_acc = state_registry.get_web_account("web-stress")
            assert oauth_acc is not None and web_acc is not None

            oauth_acc.quota_state.consecutive_errors = worker_id
            web_acc.cookies["__Secure-1PSIDTS"] = f"rotated-ts-{worker_id}"
            web_acc.usage.five_hour_pct = round((worker_id % 50) * 0.01, 4)

            if worker_id % 10 == 0:
                state_registry.upsert_web_account(
                    WebAccount(
                        id=f"web-extra-{worker_id}",
                        label=f"Extra {worker_id}",
                        cookies=WebCookies(
                            **{
                                "__Secure-1PSID": f"psid-{worker_id}",
                                "__Secure-1PSIDTS": f"ts-{worker_id}",
                            }
                        ),
                    )
                )

            await save_accounts_snapshot(file_path=snapshot_file)

        await asyncio.gather(*(mutate_and_persist(i) for i in range(1, 51)))

        assert snapshot_file.exists()
        assert not tmp_file.exists()

        persisted_raw = json.loads(snapshot_file.read_text(encoding="utf-8"))
        expected_memory_dump = state_registry.config.model_dump(mode="json", by_alias=True)
        assert persisted_raw == expected_memory_dump
    finally:
        state_registry.config = original_config


# ============================================================================
# 5. Dual-Table LMDB (`db_sessions` & `db_signatures`) CRUD & Persistence
# ============================================================================


def test_lmdb_sessions_and_signatures_crud_account_pruning_and_persistence(
    tmp_path: Path,
) -> None:
    """Verify `db_sessions` and `db_signatures` operations and durability across reopen."""
    lmdb_dir = tmp_path / "lmdb_gate1"
    store = LMDBStore(path=lmdb_dir)

    try:
        # 1. Put & Get sessions
        store.put_session("hash_a1", "web-1", ["cid-1", "rid-1", "rcid-1"])
        store.put_session("hash_a2", "web-1", ["cid-2", "rid-2", "rcid-2"])
        store.put_session("hash_b1", "web-2", ["cid-9", "rid-9", "rcid-9"])

        assert store.get_session("hash_a1") == {
            "account_id": "web-1",
            "metadata": ["cid-1", "rid-1", "rcid-1"],
        }

        # 2. Update existing session metadata
        store.put_session("hash_a1", "web-1", ["cid-1", "rid-1-new", "rcid-1-new"])
        assert store.get_session("hash_a1") == {
            "account_id": "web-1",
            "metadata": ["cid-1", "rid-1-new", "rcid-1-new"],
        }

        # 3. Put & Get thought signatures
        store.put_signature("call_001", "sig_alpha_base64")
        store.put_signature("call_002", "sig_beta_base64")
        assert store.get_signature("call_001") == "sig_alpha_base64"
        assert store.get_signature("nonexistent_call") is None
    finally:
        store.close()

    # 4. Re-open LMDB environment to verify disk persistence
    reopened_store = LMDBStore(path=lmdb_dir)
    try:
        assert reopened_store.get_session("hash_a1") == {
            "account_id": "web-1",
            "metadata": ["cid-1", "rid-1-new", "rcid-1-new"],
        }
        assert reopened_store.get_session("hash_b1") == {
            "account_id": "web-2",
            "metadata": ["cid-9", "rid-9", "rcid-9"],
        }
        assert reopened_store.get_signature("call_001") == "sig_alpha_base64"
        assert reopened_store.get_signature("call_002") == "sig_beta_base64"

        # 5. Delete sessions for a specific account (`web-1`) without touching `web-2`
        deleted_count = reopened_store.delete_sessions_for_account("web-1")
        assert deleted_count == 2
        assert reopened_store.get_session("hash_a1") is None
        assert reopened_store.get_session("hash_a2") is None
        assert reopened_store.get_session("hash_b1") is not None

        # 6. Delete individual session and signature
        assert reopened_store.delete_session("hash_b1") is True
        assert reopened_store.delete_session("hash_b1") is False
        assert reopened_store.get_session("hash_b1") is None

        assert reopened_store.delete_signature("call_001") is True
        assert reopened_store.delete_signature("call_001") is False
        assert reopened_store.get_signature("call_001") is None
        assert reopened_store.get_signature("call_002") == "sig_beta_base64"
    finally:
        reopened_store.close()
