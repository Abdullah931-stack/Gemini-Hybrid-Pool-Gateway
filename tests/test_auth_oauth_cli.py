"""Unit and concurrency tests for core/auth_oauth_cli.py.

Verifies Level 2 (Task 1 of Phase 3) requirements:
1. Production endpoint validation (blocks daily-cloudcode-pa.googleapis.com).
2. PKCE (S256) verifier and challenge generation (RFC 7636 compliance).
3. Authorization URL construction with mandatory scopes.
4. Manual callback extraction (raw code, full URL, query string, error detection).
5. Ephemeral local HTTP callback listener (OAuthCallbackServer).
6. Token refresh lifecycle and in-flight deduplication under token_refresh_lock.
7. Dual transport helpers (v1internal headers and agy stream-json command builder).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
import types
from unittest.mock import AsyncMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OAuthAccount, OAuthCredentials
from core.auth_oauth_cli import (
    CLOUDCODE_BASE_URL,
    FORBIDDEN_DAILY_HOST,
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    LOAD_CODE_ASSIST_URL,
    OAUTH_SCOPES,
    AgySubprocessTransport,
    OAuthCallbackServer,
    PKCEChallenge,
    authorize_oauth_via_isolated_firefox,
    build_agy_subprocess_command,
    build_authorization_url,
    build_v1internal_headers,
    complete_oauth_login,
    discover_project_id,
    ensure_valid_access_token,
    exchange_code_for_tokens,
    extract_code_from_manual_input,
    generate_pkce_pair,
    get_oauth_client_id,
    get_oauth_client_secret,
    needs_token_refresh,
    refresh_account_token,
    validate_production_endpoint,
    wait_for_oauth_callback,
)


@pytest.fixture(autouse=True)
def setup_test_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide default dummy OAuth credentials for test isolation."""
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_ID", "mock-test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_SECRET", "mock-test-client-secret")


def test_validate_production_endpoint_blocks_daily_sandbox() -> None:
    """Verifies that validate_production_endpoint rejects daily-cloudcode-pa."""
    assert validate_production_endpoint(CLOUDCODE_BASE_URL) == CLOUDCODE_BASE_URL
    assert validate_production_endpoint(LOAD_CODE_ASSIST_URL) == LOAD_CODE_ASSIST_URL

    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_production_endpoint(f"https://{FORBIDDEN_DAILY_HOST}/v1internal:loadCodeAssist")

    with pytest.raises(ValueError, match="Endpoint URL must not be empty"):
        validate_production_endpoint("   ")


def test_pkce_pair_generation_rfc7636() -> None:
    """Verifies generate_pkce_pair produces RFC 7636 compliant S256 challenge."""
    pkce = generate_pkce_pair(verifier_length=64)
    assert isinstance(pkce, PKCEChallenge)
    assert len(pkce.code_verifier) == 64
    assert pkce.code_challenge_method == "S256"
    assert len(pkce.code_challenge) > 20
    assert len(pkce.state) > 10

    # Bounds validation
    with pytest.raises(ValueError, match="verifier_length must be between"):
        generate_pkce_pair(verifier_length=30)
    with pytest.raises(ValueError, match="verifier_length must be between"):
        generate_pkce_pair(verifier_length=150)


def test_build_authorization_url() -> None:
    """Verifies build_authorization_url includes scopes, PKCE challenge, and redirect_uri."""
    pkce = generate_pkce_pair()
    url = build_authorization_url(
        redirect_uri="http://127.0.0.1:8085/oauth/callback",
        pkce=pkce,
    )
    assert url.startswith(GOOGLE_AUTH_URL)
    assert "response_type=code" in url
    assert "code_challenge=" + pkce.code_challenge in url
    assert "code_challenge_method=S256" in url
    assert "state=" + pkce.state in url
    for scope in OAUTH_SCOPES:
        assert scope.replace(":", "%3A").replace("/", "%2F") in url or scope in url


def test_extract_code_from_manual_input() -> None:
    """Verifies authorization code extraction from raw code, callback URL, and query string."""
    # Raw code
    assert extract_code_from_manual_input("4/0AeanS0...") == "4/0AeanS0..."

    # Full callback URL
    url = "http://127.0.0.1:8085/oauth/callback?code=4%2F0AeanS0XYZ&state=test_state"
    assert extract_code_from_manual_input(url, expected_state="test_state") == "4/0AeanS0XYZ"

    # Query string only
    qs = "?code=my_auth_code&state=st_123"
    assert extract_code_from_manual_input(qs, expected_state="st_123") == "my_auth_code"

    # State mismatch raises ValueError
    with pytest.raises(ValueError, match="OAuth state mismatch"):
        extract_code_from_manual_input(url, expected_state="different_state")

    # Error param raises ValueError
    err_url = "http://127.0.0.1:8085/oauth/callback?error=access_denied&error_description=User+denied"
    with pytest.raises(ValueError, match="OAuth callback returned an error"):
        extract_code_from_manual_input(err_url)

    # Empty raises
    with pytest.raises(ValueError, match="Manual OAuth input must not be empty"):
        extract_code_from_manual_input("   ")


@pytest.mark.asyncio
async def test_oauth_callback_server_ephemeral_flow() -> None:
    """Verifies OAuthCallbackServer listens, receives GET with code, and shuts down."""
    server = OAuthCallbackServer(host="127.0.0.1", port=0, expected_state="test_state_123")
    port = await server.start()
    assert port > 0
    assert server.redirect_uri == f"http://127.0.0.1:{port}/oauth/callback"

    async def _send_callback() -> None:
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/oauth/callback?code=test_auth_code_999&state=test_state_123"
            )
            assert resp.status_code == 200
            assert "OAuth Authentication Successful" in resp.text

    send_task = asyncio.create_task(_send_callback())
    code = await server.wait_for_code(timeout=5.0)
    await send_task

    assert code == "test_auth_code_999"


@pytest.mark.asyncio
async def test_token_exchange_and_project_discovery() -> None:
    """Verifies exchange_code_for_tokens and discover_project_id."""
    fake_token_resp = {
        "access_token": "ya29.fake_access_token_123",
        "refresh_token": "1//fake_refresh_token_456",
        "expires_in": 3600,
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_post_resp = AsyncMock(spec=httpx.Response)
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = fake_token_resp
    mock_client.post.return_value = mock_post_resp

    creds = await exchange_code_for_tokens(
        code="auth_code_1",
        redirect_uri="http://localhost:8080",
        code_verifier="test_verifier",
        http_client=mock_client,
    )

    assert creds.access_token == "ya29.fake_access_token_123"
    assert creds.refresh_token == "1//fake_refresh_token_456"
    assert creds.expires_at > int(time.time())

    # Project discovery
    fake_load_resp = {
        "cloudaicompanionProject": "projects/my-genai-project-123",
        "paidTier": True,
    }
    mock_post_resp.json.return_value = fake_load_resp

    project_id, is_paid, _ = await discover_project_id(
        "ya29.fake_access_token_123",
        http_client=mock_client,
    )
    assert project_id == "projects/my-genai-project-123"
    assert is_paid is True


def test_needs_token_refresh() -> None:
    """Verifies proactive token refresh margin (5 minutes before expiry)."""
    now = 10000.0

    # Expired or within 300s margin
    creds_expiring_soon = OAuthCredentials(
        access_token="tok", refresh_token="ref", expires_at=10200
    )
    assert needs_token_refresh(creds_expiring_soon, now=now, margin_seconds=300) is True

    # Valid token well into future
    creds_fresh = OAuthCredentials(
        access_token="tok", refresh_token="ref", expires_at=10500
    )
    assert needs_token_refresh(creds_fresh, now=now, margin_seconds=300) is False

    # Empty token always needs refresh
    creds_empty = OAuthCredentials(access_token="", refresh_token="ref", expires_at=20000)
    assert needs_token_refresh(creds_empty, now=now) is True


@pytest.mark.asyncio
async def test_refresh_account_token_in_flight_deduplication() -> None:
    """Verifies that concurrent refresh calls on the same account trigger only 1 network request."""
    account = OAuthAccount(
        id="oauth-dedup-test",
        label="Test Dedup Account",
        credentials=OAuthCredentials(
            access_token="old_access_token",
            refresh_token="valid_refresh_token",
            expires_at=int(time.time() - 10),  # expired
        ),
    )

    network_call_count = 0

    async def _fake_post_form_or_json(url: str, **kwargs: any) -> dict[str, any]:
        nonlocal network_call_count
        network_call_count += 1
        await asyncio.sleep(0.05)  # simulate network delay
        return {
            "access_token": f"refreshed_tok_{network_call_count}",
            "expires_in": 3600,
        }

    with patch("core.auth_oauth_cli._post_form_or_json", side_effect=_fake_post_form_or_json), \
         patch("core.auth_oauth_cli.save_accounts_snapshot", new_callable=AsyncMock):

        # Launch 10 concurrent tasks calling refresh_account_token
        tasks = [
            asyncio.create_task(refresh_account_token(account, persist=False))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

        # In-flight deduplication guarantees exactly 1 network request
        assert network_call_count == 1
        assert account.credentials.access_token == "refreshed_tok_1"
        for cred in results:
            assert cred.access_token == "refreshed_tok_1"


def test_build_v1internal_headers() -> None:
    """Verifies build_v1internal_headers constructs CloudCode-PA headers."""
    headers = build_v1internal_headers("my_test_token", ide_type="ANTIGRAVITY")
    assert headers["Authorization"] == "Bearer my_test_token"
    assert headers["Content-Type"] == "application/json"
    assert "ANTIGRAVITY" in headers["User-Agent"]
    assert "CloudCode-PA" in headers["User-Agent"]


def test_build_agy_subprocess_command() -> None:
    """Verifies build_agy_subprocess_command builds agy CLI invocation."""
    cmd = build_agy_subprocess_command("agy", extra_args=["--verbose", "--model", "gemini-pro"])
    assert cmd[0] == "agy"
    assert "--input-format" in cmd
    assert "stream-json" in cmd
    assert "--output-format" in cmd
    assert "--verbose" in cmd
    assert "gemini-pro" in cmd


def test_get_oauth_client_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies client_id and client_secret resolution (explicit, env var, builtin, disable flag)."""
    # 1. Explicit arguments take absolute precedence
    assert get_oauth_client_id("explicit_client_id") == "explicit_client_id"
    assert get_oauth_client_secret("explicit_secret") == "explicit_secret"

    # 2. Environment variables are used when arguments are omitted
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_ID", "env_test_id")
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_SECRET", "env_test_secret")
    assert get_oauth_client_id() == "env_test_id"
    assert get_oauth_client_secret() == "env_test_secret"

    # 3. Builtin fallback when env vars are unset and disable flag is not active
    monkeypatch.delenv("GEMINI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GEMINI_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GHPG_DISABLE_BUILTIN_OAUTH_CLIENT", raising=False)
    assert "681255809395" in get_oauth_client_id()
    assert get_oauth_client_secret() == "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"

    # 4. GHPG_DISABLE_BUILTIN_OAUTH_CLIENT="1" or "true" raises ValueError if missing
    for flag in ("1", "true", "True", " 1 "):
        monkeypatch.setenv("GHPG_DISABLE_BUILTIN_OAUTH_CLIENT", flag)
        with pytest.raises(ValueError, match="Google OAuth Client ID is missing"):
            get_oauth_client_id()
        with pytest.raises(ValueError, match="Google OAuth Client Secret is missing"):
            get_oauth_client_secret()


@pytest.mark.asyncio
async def test_authorize_oauth_via_isolated_firefox_port_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies browser authorization falls back to port 0 on port conflict and returns code."""
    pkce = generate_pkce_pair()

    async def _fake_run_oauth(url: str, code_coro: any) -> str:
        if hasattr(code_coro, "close"):
            code_coro.close()
        return "mock_captured_auth_code"

    mock_run_oauth = AsyncMock(side_effect=_fake_run_oauth)
    mock_launcher = types.ModuleType("core.firefox_launcher")
    mock_launcher.run_oauth_in_isolated_firefox = mock_run_oauth
    monkeypatch.setitem(sys.modules, "core.firefox_launcher", mock_launcher)

    real_start = OAuthCallbackServer.start
    call_ports: list[int] = []

    async def _mock_start(server_self: OAuthCallbackServer) -> int:
        call_ports.append(server_self.port)
        if server_self.port == 8000:
            raise OSError("Port 8000 is occupied")
        return await real_start(server_self)

    monkeypatch.setattr(OAuthCallbackServer, "start", _mock_start)

    code, redirect_uri = await authorize_oauth_via_isolated_firefox(
        pkce=pkce,
        preferred_port=8000,
        timeout=10.0,
    )

    assert code == "mock_captured_auth_code"
    assert call_ports == [8000, 0]
    assert redirect_uri.startswith("http://127.0.0.1:")
    assert redirect_uri.endswith("/oauth/callback")
    assert not redirect_uri.startswith("http://127.0.0.1:8000")

    mock_run_oauth.assert_awaited_once()
    auth_url_arg = mock_run_oauth.call_args[0][0]
    assert auth_url_arg.startswith(GOOGLE_AUTH_URL)
    assert f"code_challenge={pkce.code_challenge}" in auth_url_arg
    assert f"state={pkce.state}" in auth_url_arg


@pytest.mark.asyncio
async def test_authorize_oauth_via_isolated_firefox_preferred_port_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies browser authorization uses preferred_port when binding succeeds."""
    pkce = generate_pkce_pair()

    async def _fake_run_oauth(url: str, code_coro: any) -> str:
        if hasattr(code_coro, "close"):
            code_coro.close()
        return "code_preferred"

    mock_run_oauth = AsyncMock(side_effect=_fake_run_oauth)
    mock_launcher = types.ModuleType("core.firefox_launcher")
    mock_launcher.run_oauth_in_isolated_firefox = mock_run_oauth
    monkeypatch.setitem(sys.modules, "core.firefox_launcher", mock_launcher)

    code, redirect_uri = await authorize_oauth_via_isolated_firefox(
        pkce=pkce,
        preferred_port=0,
        timeout=10.0,
    )

    assert code == "code_preferred"
    assert redirect_uri.startswith("http://127.0.0.1:")
    mock_run_oauth.assert_awaited_once()

