"""Verification Gate 3 official test suite for Level 2 of Gemini-Hybrid-Pool-Gateway.

Covers:
1. OAuth 2.0 PKCE, Scope Bundle & Endpoint Protection (ADR-01):
   - Cryptographic S256 PKCE challenge generation, RFC 7636 verifier length bounds [43, 128].
   - Mandatory 5-scope bundle enforcement and authorization URL construction.
   - Strict rejection of forbidden sandbox endpoints (daily-cloudcode-pa.googleapis.com).
   - Manual callback URL, query string, and raw authorization code extraction with state validation.
   - Cloud Code v1internal headers shield and agy subprocess command generation.
2. Proactive Token Refresh & In-Flight Deduplication (ADR-01):
   - Boundary checks for proactive 5-minute (300s) token refresh margin.
   - Concurrent token refresh under token_refresh_lock with in-flight deduplication (single HTTP exchange).
3. Web Cookie Pool, Isolated Cache & Concurrency Guard (ADR-03):
   - Parsing __Secure-1PSID and __Secure-1PSIDTS from raw tokens, dicts, and HTTP Cookie headers.
   - Sanitizing GEMINI_COOKIE_PATH from process environment and surfacing Saved Info hygiene guidance.
   - Isolated cookie cache persistence under cookie_write_lock with path traversal rejection.
   - WebSessionManager Concurrency Guard: refusing re-init or close while active_requests > 0 (C_web,i > 0).
4. Model Discovery, 403 Fallback to Quota, and Reasoning Effort Translation (ADR-07):
   - OAuth model discovery with automatic fallback from :fetchAvailableModels to :retrieveUserQuota on 403 Forbidden.
   - Reasoning effort suffix parsing (-minimal, -low, -medium, -high), routine task downgrade to 'low'.
   - OAuth thinkingConfig generation and injection.
   - UnifiedModelRegistry aggregation across OAuth and Web accounts into OpenAI-compatible format.
5. 3-Tier Context Rehydration Engine & Deterministic Cleanup (ADR-08):
   - Sequential prefix hashing (SHA-256) and longest-prefix session matching in LMDB db_sessions.
   - Adaptive session mode (temporary=True for single-turn tool-less queries; False for multi-turn / tools).
   - 60-minute idle chat garbage collection via cleanup_expired_chats.
   - Tier 1: Same-account prefix hit with delta turns and <tool_response> tags.
   - Tier 2: Inline history formatting in <conversation_history> for contexts <= 60,000 characters.
   - Tier 3: Temporary history file passthrough in TEMP_HISTORY_DIR for contexts > 60,000 characters.
   - Deterministic temp file unlinking in finally: via managed_rehydration_plan and startup orphan pruning.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    COOKIES_CACHE_DIR,
    REHYDRATION_TIER2_MAX_CHARS,
    TEMP_HISTORY_DIR,
    AccountsConfig,
    AccountsStateRegistry,
    LMDBStore,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    save_accounts_snapshot,
    state_registry,
)
from core import (
    OAUTH_SCOPES,
    ActiveRequestsConcurrencyError,
    AgySubprocessTransport,
    PKCEChallenge,
    PrefixMatchResult,
    RehydrationPlan,
    ResolvedModelSpec,
    UnifiedModelRegistry,
    WebSessionManager,
    build_agy_subprocess_command,
    build_authorization_url,
    build_oauth_thinking_config,
    build_rehydration_plan,
    build_v1internal_headers,
    cleanup_expired_chats,
    cleanup_orphaned_temp_files,
    compute_messages_char_length,
    compute_prefix_hashes,
    discover_oauth_models,
    ensure_valid_access_token,
    extract_code_from_manual_input,
    find_longest_prefix_session,
    format_messages_as_xml_history,
    format_tool_response_block,
    generate_pkce_pair,
    get_account_cookie_cache_path,
    get_saved_info_hygiene_notice,
    inject_oauth_thinking_config,
    load_account_cookies_isolated,
    managed_rehydration_plan,
    needs_token_refresh,
    normalize_message_for_hash,
    parse_cookie_header_or_dict,
    parse_manual_cookies,
    parse_model_and_effort,
    refresh_account_token,
    resolve_web_model,
    sanitize_shared_cookie_env,
    save_account_cookies_isolated,
    should_use_temporary_session,
    validate_production_endpoint,
)
from core.model_registry import (
    FETCH_AVAILABLE_MODELS_URL,
    RETRIEVE_USER_QUOTA_URL,
)
from core.prefix_differ import (
    IDLE_CHAT_TTL_SECONDS,
    TIER1_PREFIX_HIT,
    TIER2_INLINE_HISTORY,
    TIER3_TEMP_FILE_PASSTHROUGH,
    clear_tracked_chats,
    get_tracked_chats,
    register_chat_activity,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset in-memory state, tracked chats, and isolate environment variables."""
    clear_tracked_chats()
    state_registry.config = AccountsConfig()
    yield
    clear_tracked_chats()
    state_registry.config = AccountsConfig()


@pytest.fixture
def temp_lmdb_store(tmp_path: Path) -> LMDBStore:
    """Provide an isolated LMDB database instance for tests."""
    store = LMDBStore(path=tmp_path / "lmdb_gate3")
    yield store
    store.close()


# ============================================================================
# 1. OAuth 2.0 PKCE, Scope Bundle & Endpoint Protection (ADR-01)
# ============================================================================


def test_pkce_generation_and_cryptographic_verifier_challenge_math() -> None:
    """PKCE pair generation must enforce verifier length bounds and correct S256 digest calculation."""
    challenge = generate_pkce_pair(verifier_length=64)

    assert len(challenge.code_verifier) == 64
    assert challenge.code_challenge_method == "S256"
    assert len(challenge.state) > 16

    # Verify cryptographic S256 transformation: base64url(sha256(verifier)) unpadded
    expected_digest = hashlib.sha256(challenge.code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
    assert challenge.code_challenge == expected_challenge

    # Bounds checking: RFC 7636 allows 43 to 128 characters
    with pytest.raises(ValueError, match="verifier_length must be between 43 and 128"):
        generate_pkce_pair(verifier_length=42)

    with pytest.raises(ValueError, match="verifier_length must be between 43 and 128"):
        generate_pkce_pair(verifier_length=129)


def test_authorization_url_contains_all_five_mandatory_scopes_and_pkce_params() -> None:
    """Google OAuth authorization URL must contain all 5 required scopes and PKCE parameters."""
    pkce = generate_pkce_pair(verifier_length=64)
    redirect_uri = "http://127.0.0.1:8080/oauth/callback"

    auth_url = build_authorization_url(
        redirect_uri=redirect_uri,
        pkce=pkce,
        client_id="test-client-id.apps.googleusercontent.com",
    )

    parsed = urlparse(auth_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"

    params = parse_qs(parsed.query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["test-client-id.apps.googleusercontent.com"]
    assert params["redirect_uri"] == [redirect_uri]
    assert params["code_challenge"] == [pkce.code_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [pkce.state]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]

    scopes = set(params["scope"][0].split(" "))
    for required_scope in OAUTH_SCOPES:
        assert required_scope in scopes, f"Missing mandatory scope: {required_scope}"
    assert len(scopes) == 5


@pytest.mark.parametrize(
    ("endpoint_url", "should_raise", "expected_match"),
    [
        (
            "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            True,
            "daily-cloudcode-pa.googleapis.com",
        ),
        (
            "http://daily-cloudcode-pa.googleapis.com",
            True,
            "daily-cloudcode-pa.googleapis.com",
        ),
        ("", True, "Endpoint URL must not be empty"),
        ("   ", True, "Endpoint URL must not be empty"),
        (
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            False,
            None,
        ),
        (
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            False,
            None,
        ),
    ],
)
def test_endpoint_validator_strictly_rejects_sandbox_daily_cloudcode(
    endpoint_url: str,
    should_raise: bool,
    expected_match: str | None,
) -> None:
    """The endpoint validator must strictly reject sandbox / daily URLs while accepting production."""
    if should_raise:
        with pytest.raises(ValueError, match=expected_match):
            validate_production_endpoint(endpoint_url)
    else:
        validated = validate_production_endpoint(endpoint_url)
        assert validated == endpoint_url


@pytest.mark.parametrize(
    ("raw_input", "expected_state", "expected_code", "should_raise"),
    [
        (
            "http://127.0.0.1:8080/oauth/callback?code=auth_code_123&state=state_abc",
            "state_abc",
            "auth_code_123",
            False,
        ),
        (
            "/?code=code_from_query&state=expected_st",
            "expected_st",
            "code_from_query",
            False,
        ),
        (
            "code=just_code_kv&state=st_val",
            "st_val",
            "just_code_kv",
            False,
        ),
        (
            "raw_verbatim_auth_code_string_from_terminal",
            None,
            "raw_verbatim_auth_code_string_from_terminal",
            False,
        ),
        (
            "http://127.0.0.1:8080/oauth/callback?code=valid_code&state=wrong_state",
            "correct_state",
            None,
            True,
        ),
        (
            "http://127.0.0.1:8080/oauth/callback?error=access_denied&error_description=User+declined",
            None,
            None,
            True,
        ),
        ("", None, None, True),
    ],
)
def test_manual_input_code_and_state_extraction(
    raw_input: str,
    expected_state: str | None,
    expected_code: str | None,
    should_raise: bool,
) -> None:
    """Manual input parsing must extract valid auth codes and enforce state security checks."""
    if should_raise:
        with pytest.raises(ValueError):
            extract_code_from_manual_input(raw_input, expected_state=expected_state)
    else:
        code = extract_code_from_manual_input(raw_input, expected_state=expected_state)
        assert code == expected_code


def test_v1internal_headers_and_subprocess_command_generation() -> None:
    """Headers shield must emit correct Bearer and User-Agent headers, and command list must match CLI spec."""
    headers = build_v1internal_headers("token_secret_123", ide_type="ANTIGRAVITY")
    assert headers["Authorization"] == "Bearer token_secret_123"
    assert headers["Content-Type"] == "application/json"
    assert "CloudCode-PA/2.0 (ANTIGRAVITY; GEMINI)" in headers["User-Agent"]
    assert "gl-python" in headers["X-Goog-Api-Client"]

    cmd = build_agy_subprocess_command("agy", extra_args=["--profile", "pool1"])
    assert cmd == [
        "agy",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--profile",
        "pool1",
    ]


# ============================================================================
# 2. Proactive Token Refresh & In-Flight Deduplication (ADR-01)
# ============================================================================


def test_needs_token_refresh_boundary_conditions() -> None:
    """Token refresh check must trigger 5 minutes (300s) before expiration or when token is missing."""
    now = 1_000_000.0

    # Token with 301 seconds remaining (> 300s margin): no refresh
    creds_fresh = OAuthCredentials(
        access_token="ya29.fresh",
        refresh_token="1//refresh",
        expires_at=int(now + 301),
    )
    assert not needs_token_refresh(creds_fresh, now=now)

    # Token with 300 seconds remaining (<= 300s margin): must refresh
    creds_boundary = OAuthCredentials(
        access_token="ya29.boundary",
        refresh_token="1//refresh",
        expires_at=int(now + 300),
    )
    assert needs_token_refresh(creds_boundary, now=now)

    # Expired token: must refresh
    creds_expired = OAuthCredentials(
        access_token="ya29.expired",
        refresh_token="1//refresh",
        expires_at=int(now - 10),
    )
    assert needs_token_refresh(creds_expired, now=now)

    # Empty access token: must refresh
    creds_empty = OAuthCredentials(
        access_token="",
        refresh_token="1//refresh",
        expires_at=int(now + 1000),
    )
    assert needs_token_refresh(creds_empty, now=now)


@pytest.mark.asyncio
async def test_concurrent_token_refresh_in_flight_deduplication() -> None:
    """Multiple concurrent refresh calls for the same account must perform only 1 network exchange."""
    now = time.time()
    account = OAuthAccount(
        id="oauth_pool_test",
        label="OAuth Pool Test",
        credentials=OAuthCredentials(
            access_token="ya29.expiring_soon",
            refresh_token="1//valid_refresh_token",
            expires_at=int(now + 100),  # Expires in 100s, within 300s margin
        ),
    )

    network_call_count = 0

    def mock_token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_call_count
        network_call_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.newly_refreshed_access_token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    mock_transport = httpx.MockTransport(mock_token_handler)
    async with httpx.AsyncClient(transport=mock_transport) as client:
        # Launch 10 concurrent tasks calling ensure_valid_access_token
        tasks = [
            asyncio.create_task(
                ensure_valid_access_token(account, http_client=client, persist=False)
            )
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

    # All 10 tasks must receive the refreshed access token
    for res in results:
        assert res == "ya29.newly_refreshed_access_token"

    # Crucial Gate 3 invariant: In-flight deduplication under token_refresh_lock
    # guarantees exactly ONE network exchange occurs
    assert network_call_count == 1
    assert account.credentials.access_token == "ya29.newly_refreshed_access_token"
    assert account.credentials.expires_at > int(now + 3000)


# ============================================================================
# 3. Web Cookie Pool, Isolated Cache & Concurrency Guard (ADR-03)
# ============================================================================


def test_cookie_parsing_sanitization_and_hygiene_notice() -> None:
    """Manual cookie parsing must handle raw cookies and headers, and hygiene guidance must be surfaced."""
    # Test manual token cleanup
    parsed = parse_manual_cookies(
        "\"__Secure-1PSID=sid_val_xyz123;\";",
        "'__Secure-1PSIDTS=ts_val_abc456;'",
    )
    assert parsed.secure_1psid == "sid_val_xyz123"
    assert parsed.secure_1psidts == "ts_val_abc456"

    # Test dictionary parsing
    dict_input = {
        "__Secure-1PSID": "dict_sid_val",
        "__Secure-1PSIDTS": "dict_ts_val",
    }
    parsed_dict = parse_cookie_header_or_dict(dict_input)
    assert parsed_dict.secure_1psid == "dict_sid_val"
    assert parsed_dict.secure_1psidts == "dict_ts_val"

    # Test HTTP Cookie header parsing
    header_val = "Cookie: __Secure-1PSID=header_sid; __Secure-1PSIDTS=header_ts; other_val=ignore"
    parsed_header = parse_cookie_header_or_dict(header_val)
    assert parsed_header.secure_1psid == "header_sid"
    assert parsed_header.secure_1psidts == "header_ts"

    # Test environment sanitization
    os.environ["GEMINI_COOKIE_PATH"] = "/tmp/shared_cookies.json"
    sanitize_shared_cookie_env()
    assert "GEMINI_COOKIE_PATH" not in os.environ

    # Test Saved Info hygiene warning
    notice = get_saved_info_hygiene_notice()
    assert notice["url"] == "https://gemini.google.com/saved-info"
    assert notice["feature"] == "Saved Info"


@pytest.mark.asyncio
async def test_isolated_cookie_cache_path_traversal_rejection_and_atomic_io(tmp_path: Path) -> None:
    """Cookie cache paths must reject traversal attempts and isolate cookies per account."""
    # Path traversal protection
    with pytest.raises(ValueError, match="Invalid or unsafe account_id"):
        get_account_cookie_cache_path("../malicious_id")

    with pytest.raises(ValueError, match="Invalid or unsafe account_id"):
        get_account_cookie_cache_path("nested/account/id")

    # Safe isolated file operations
    account = WebAccount(
        id="web_account_alpha",
        label="Web Alpha",
        cookies=WebCookies(
            secure_1psid="alpha_sid_999",
            secure_1psidts="alpha_ts_888",
        ),
    )

    cache_path = await save_account_cookies_isolated(
        account, cache_dir=tmp_path, sync_snapshot=False
    )
    assert cache_path.exists()
    assert cache_path.name == "web_account_alpha.json"

    loaded_cookies = load_account_cookies_isolated("web_account_alpha", cache_dir=tmp_path)
    assert loaded_cookies is not None
    assert loaded_cookies.secure_1psid == "alpha_sid_999"
    assert loaded_cookies.secure_1psidts == "alpha_ts_888"


@pytest.mark.asyncio
async def test_web_session_manager_concurrency_guard_protects_active_streams(
    tmp_path: Path,
) -> None:
    """WebSessionManager must refuse to close or re-initialize a client while active_requests > 0."""
    session_mgr = WebSessionManager(cache_dir=tmp_path, sync_snapshot=False)
    account = WebAccount(
        id="web_stream_test",
        label="Web Stream Test",
        cookies=WebCookies(
            secure_1psid="sid_sample",
            secure_1psidts="ts_sample",
        ),
    )
    account.active_requests = 2  # Active in-flight streams C_web,i > 0

    # Attach an async mock client
    mock_client = AsyncMock()
    mock_client.close = AsyncMock()
    mock_client.cookies = None
    session_mgr._clients[account.id] = mock_client

    # Concurrency Guard: must raise ActiveRequestsConcurrencyError when active_requests > 0
    with pytest.raises(
        ActiveRequestsConcurrencyError,
        match="Cannot re-init or close GeminiClient",
    ):
        await session_mgr.safe_reinit_or_close(account)

    # close_client_if_idle must return False and not close
    closed_idle = await session_mgr.close_client_if_idle(account)
    assert not closed_idle
    mock_client.close.assert_not_called()

    # Now simulate streams finishing (active_requests == 0)
    account.active_requests = 0
    closed_idle_success = await session_mgr.close_client_if_idle(account)
    assert closed_idle_success
    mock_client.close.assert_awaited_once()
    assert account.id not in session_mgr._clients


# ============================================================================
# 4. Model Discovery, 403 Fallback to Quota, and Reasoning Effort Translation (ADR-07)
# ============================================================================


@pytest.mark.asyncio
async def test_oauth_model_discovery_with_automatic_403_fallback_to_user_quota() -> None:
    """When :fetchAvailableModels yields 403 Forbidden, gateway must fall back to :retrieveUserQuota."""
    account = OAuthAccount(
        id="oauth_quota_fallback",
        label="OAuth Quota Fallback",
        credentials=OAuthCredentials(
            access_token="ya29.mock_token",
            expires_at=int(time.time() + 3600),
        ),
    )

    fetch_called = False
    quota_called = False

    def mock_discovery_transport(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_called, quota_called
        url_str = str(request.url)
        if FETCH_AVAILABLE_MODELS_URL in url_str:
            fetch_called = True
            # Simulate Cloud Code 403 Forbidden for standard Pro tier
            return httpx.Response(
                403,
                json={"error": {"code": 403, "message": "Method not allowed for this tier"}},
            )
        if RETRIEVE_USER_QUOTA_URL in url_str:
            quota_called = True
            return httpx.Response(
                200,
                json={
                    "buckets": [
                        {
                            "modelId": "models/gemini-2.5-pro",
                            "remainingFraction": 0.85,
                            "resetTime": "2026-10-17T12:00:00Z",
                        },
                        {
                            "modelId": "gemini-2.5-flash",
                            "remainingFraction": 0.99,
                            "resetTime": "2026-10-17T12:00:00Z",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_discovery_transport))
    discovered = await discover_oauth_models(account, http_client=mock_client)

    assert fetch_called, "Should have attempted fetchAvailableModels first"
    assert quota_called, "Must fall back to retrieveUserQuota on 403 Forbidden"
    assert "gemini-2.5-pro" in discovered
    assert "gemini-2.5-flash" in discovered
    assert "gemini-2.5-pro" in account.discovered_models
    assert "gemini-2.5-pro" in account.quota_state.model_quotas

    quota_pro = account.quota_state.model_quotas["gemini-2.5-pro"]
    assert quota_pro.remaining_fraction == 0.85


def test_model_effort_parsing_and_routine_downgrade() -> None:
    """Model strings and reasoning effort must be accurately parsed, defaulting routine tasks to 'low'."""
    # Suffix parsing
    spec_high = parse_model_and_effort("gemini-2.5-pro-high")
    assert spec_high.base_model == "gemini-2.5-pro"
    assert spec_high.reasoning_effort == "high"
    assert spec_high.oauth_thinking_config["thinkingBudget"] == 32768
    assert spec_high.web_extended_thinking is True

    spec_min = parse_model_and_effort("gemini-2.5-flash-minimal")
    assert spec_min.base_model == "gemini-2.5-flash"
    assert spec_min.reasoning_effort == "minimal"
    assert spec_min.oauth_thinking_config["includeThoughts"] is False
    assert spec_min.oauth_thinking_config["thinkingBudget"] == 1024
    assert spec_min.web_extended_thinking is False

    # Explicit argument override
    spec_override = parse_model_and_effort("gemini-2.5-pro-low", reasoning_effort="high")
    assert spec_override.reasoning_effort == "high"

    # Routine task downgrade: routine models without explicit effort default to 'low'
    spec_routine = parse_model_and_effort("gemini-2.5-flash", is_routine_task=True)
    assert spec_routine.reasoning_effort == "low"
    assert spec_routine.oauth_thinking_config["thinkingLevel"] == "LOW"

    # Verify thinking config injection
    payload: dict[str, Any] = {"temperature": 0.7}
    injected = inject_oauth_thinking_config(payload, "high")
    assert injected["temperature"] == 0.7
    assert injected["thinkingConfig"]["thinkingBudget"] == 32768


def test_unified_model_registry_aggregates_all_pool_models() -> None:
    """UnifiedModelRegistry must aggregate discovered models across accounts into OpenAI /v1/models format."""
    state_registry.config = AccountsConfig(
        oauth_accounts=[
            OAuthAccount(
                id="oauth_acc1",
                label="OAuth Acc 1",
                discovered_models=["gemini-2.5-pro"],
                credentials=OAuthCredentials(access_token="tok"),
            )
        ],
        web_accounts=[
            WebAccount(
                id="web_acc1",
                label="Web Acc 1",
                discovered_models=["gemini-2.5-flash"],
                cookies=WebCookies(secure_1psid="sid"),
            )
        ],
    )

    registry = UnifiedModelRegistry(registry=state_registry)
    models_response = registry.list_openai_models()

    assert "data" in models_response
    model_ids = {item["id"] for item in models_response["data"]}

    # Base and reasoning variants must be present
    assert "gemini-2.5-pro" in model_ids
    assert "gemini-2.5-pro-high" in model_ids
    assert "gemini-2.5-flash" in model_ids
    assert "gemini-2.5-flash-low" in model_ids

    # Supporting account lookup
    oauth_supporters = registry.get_supporting_accounts("gemini-2.5-pro")
    assert "oauth_acc1" in oauth_supporters["oauth"]

    web_supporters = registry.get_supporting_accounts("gemini-2.5-flash")
    assert "web_acc1" in web_supporters["web"]


# ============================================================================
# 5. 3-Tier Context Rehydration Engine & Deterministic Cleanup (ADR-08)
# ============================================================================


def test_prefix_hashing_and_longest_prefix_matching(temp_lmdb_store: LMDBStore) -> None:
    """Sequential prefix hashes must accumulate deterministically and resolve longest matching prefixes."""
    msg1 = {"role": "user", "content": "Initial prompt"}
    msg2 = {"role": "assistant", "content": "I can help with that."}
    msg3 = {"role": "user", "content": "Next step."}

    hashes = compute_prefix_hashes([msg1, msg2, msg3])
    assert len(hashes) == 3
    assert all(isinstance(h, str) and len(h) == 64 for h in hashes)

    # Store 2-message session under the second prefix hash in LMDB
    temp_lmdb_store.put_session(
        hashes[1],
        "web_acc_1",
        ["session_web_001", "rid_456"],
    )

    # Search with 3-message sequence: longest match is 2 messages, delta is [msg3]
    match = find_longest_prefix_session([msg1, msg2, msg3], lmdb_store=temp_lmdb_store)
    assert match is not None
    assert match.session_id == "session_web_001"
    assert match.account_id == "web_acc_1"
    assert match.matched_count == 2
    assert len(match.delta_messages) == 1
    assert match.delta_messages[0]["content"] == "Next step."


@pytest.mark.asyncio
async def test_adaptive_session_mode_and_idle_chat_cleanup(temp_lmdb_store: LMDBStore) -> None:
    """Adaptive session mode must use temporary=True for single-turn tool-less queries, and GC idle chats."""
    # Single-turn tool-less query without prior context: temporary=True
    single_turn = [{"role": "user", "content": "What is the capital of France?"}]
    assert should_use_temporary_session(single_turn, tools=[]) is True

    # Multi-turn query: temporary=False
    multi_turn = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Continue"},
    ]
    assert should_use_temporary_session(multi_turn, tools=[]) is False

    # Tool-assisted query: temporary=False
    tool_query = [{"role": "user", "content": "Run command"}]
    assert should_use_temporary_session(tool_query, tools=[{"type": "function"}]) is False

    # Idle chat GC: expired sessions (> 3600s idle) must be cleaned up
    now = time.time()
    cid_active = "cid_act_001"
    cid_expired = "cid_exp_002"

    register_chat_activity(
        cid_active, "web_acc_1", prefix_hash="hash_act", timestamp=now - 200
    )
    temp_lmdb_store.put_session("hash_act", "web_acc_1", [cid_active, "rid1"])

    register_chat_activity(
        cid_expired, "web_acc_1", prefix_hash="hash_exp", timestamp=now - 4000
    )
    temp_lmdb_store.put_session("hash_exp", "web_acc_1", [cid_expired, "rid2"])

    mock_client = AsyncMock()
    mock_client.delete_chat = AsyncMock()

    deleted_count = await cleanup_expired_chats(
        lambda account_id: mock_client,
        max_idle_seconds=IDLE_CHAT_TTL_SECONDS,
        lmdb_store=temp_lmdb_store,
        now=now,
    )

    assert deleted_count == 1
    mock_client.delete_chat.assert_called_once_with(cid_expired)
    assert temp_lmdb_store.get_session("hash_exp") is None
    assert temp_lmdb_store.get_session("hash_act") is not None
    assert cid_expired not in get_tracked_chats()
    assert cid_active in get_tracked_chats()


def test_three_tier_rehydration_plan_selection(temp_lmdb_store: LMDBStore, tmp_path: Path) -> None:
    """Rehydration engine must accurately select Tier 1 (delta), Tier 2 (inline <=60k), or Tier 3 (>60k)."""
    # ----------------------------------------------------
    # Case A: Tier 1 (Same-account prefix hit in LMDB)
    # ----------------------------------------------------
    turn1 = {"role": "user", "content": "Step 1"}
    turn2 = {"role": "assistant", "content": "Done 1"}
    turn3 = {
        "role": "tool",
        "content": "output from test tool",
        "tool_call_id": "call_123",
        "name": "run_test",
    }
    all_turns = [turn1, turn2, turn3]

    hashes = compute_prefix_hashes(all_turns)
    temp_lmdb_store.put_session(
        hashes[1],
        "web_primary",
        ["cid_abc"],
    )

    plan_t1 = build_rehydration_plan(
        "web_primary",
        all_turns,
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    )
    assert plan_t1.tier == TIER1_PREFIX_HIT
    assert plan_t1.conversation_cid == "cid_abc"
    assert '<tool_response call_id="call_123" name="run_test">' in plan_t1.prompt
    assert plan_t1.temp_file_path is None

    # ----------------------------------------------------
    # Case B: Tier 2 (Inline history <= 60,000 characters)
    # ----------------------------------------------------
    short_turns = [
        {"role": "user", "content": "Short question 1"},
        {"role": "assistant", "content": "Short answer 1"},
        {"role": "user", "content": "Short question 2"},
    ]
    plan_t2 = build_rehydration_plan(
        "web_new_account",
        short_turns,
        system_prompt="You are a helpful assistant.",
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    )
    assert plan_t2.tier == TIER2_INLINE_HISTORY
    assert "<conversation_history>" in plan_t2.prompt
    assert "Short question 1" in plan_t2.prompt
    assert plan_t2.temp_file_path is None

    # ----------------------------------------------------
    # Case C: Tier 3 (Temp file passthrough > 60,000 characters)
    # ----------------------------------------------------
    large_content = "X" * 25_000
    large_turns = [
        {"role": "user", "content": f"Turn 1: {large_content}"},
        {"role": "assistant", "content": f"Turn 2: {large_content}"},
        {"role": "user", "content": f"Turn 3: {large_content}"},
        {"role": "assistant", "content": "Turn 4: brief confirmation"},
        {"role": "user", "content": "Turn 5: final instruction"},
    ]
    assert compute_messages_char_length(large_turns) > REHYDRATION_TIER2_MAX_CHARS

    plan_t3 = build_rehydration_plan(
        "web_failover_account",
        large_turns,
        system_prompt="System Directive",
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    )
    try:
        assert plan_t3.tier == TIER3_TEMP_FILE_PASSTHROUGH
        assert plan_t3.temp_file_path is not None
        assert plan_t3.temp_file_path.exists()
        assert plan_t3.temp_file_path.name.startswith("harness_history_")
        assert "<attached_context_notice>" in plan_t3.prompt
        assert "<recent_turns>" in plan_t3.prompt
    finally:
        if plan_t3.temp_file_path and plan_t3.temp_file_path.exists():
            plan_t3.temp_file_path.unlink()


def test_deterministic_temp_file_cleanup_and_startup_orphan_pruning(
    temp_lmdb_store: LMDBStore,
    tmp_path: Path,
) -> None:
    """Tier 3 temporary history files must be deterministically unlinked in finally: and on startup."""
    large_content = "Y" * 25_000
    large_turns = [
        {"role": "user", "content": f"Turn 1: {large_content}"},
        {"role": "assistant", "content": f"Turn 2: {large_content}"},
        {"role": "user", "content": f"Turn 3: {large_content}"},
        {"role": "assistant", "content": "Turn 4: response"},
        {"role": "user", "content": "Turn 5: instruction"},
    ]

    # Context manager must guarantee deletion on normal exit
    with managed_rehydration_plan(
        "web_acc_cleanup",
        large_turns,
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    ) as plan:
        assert plan.temp_file_path is not None
        created_file = plan.temp_file_path
        assert created_file.exists()
    assert not created_file.exists(), "Temp file must be unlinked after context manager exits"

    # Context manager must guarantee deletion on exception
    with pytest.raises(RuntimeError, match="Simulated stream error"):
        with managed_rehydration_plan(
            "web_acc_cleanup",
            large_turns,
            lmdb_store=temp_lmdb_store,
            temp_dir=tmp_path,
        ) as plan_err:
            err_file = plan_err.temp_file_path
            assert err_file is not None and err_file.exists()
            raise RuntimeError("Simulated stream error")
    assert not err_file.exists(), "Temp file must be unlinked even if an exception occurs"

    # Startup orphan cleanup test: leftover harness_history_*.txt must be pruned
    orphan1 = tmp_path / "harness_history_orphan1.txt"
    orphan2 = tmp_path / "harness_history_orphan2.txt"
    non_orphan = tmp_path / "important_config.txt"
    orphan1.write_text("orphan 1")
    orphan2.write_text("orphan 2")
    non_orphan.write_text("keep me")

    pruned_count = cleanup_orphaned_temp_files(temp_dir=tmp_path)
    assert pruned_count == 2
    assert not orphan1.exists()
    assert not orphan2.exists()
    assert non_orphan.exists()
