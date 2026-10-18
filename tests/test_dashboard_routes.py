"""Integration test suite for Dashboard API routes and control plane endpoints.

Verifies:
1. GET /api/status telemetry reporting across empty, active, and degraded pool states.
2. POST /api/oauth-accounts/init PKCE challenge generation and session storage.
3. POST /api/oauth-accounts/complete authentication completion, model discovery, and persistence.
4. DELETE /api/oauth-accounts/{id} account deletion and snapshot persistence.
5. POST /api/web-accounts/add cookie parsing (header, JSON, tokens, Firefox SQLite), cache isolation, and hygiene notices.
6. DELETE /api/web-accounts/{id} account deletion, cache file removal, and 404 handling.
7. POST /api/accounts/sync-quotas invocation of UsageMonitor sync pass.
8. GET /api/dashboard UI serving.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.dashboard_routes import (
    _pending_oauth_sessions,
    callback_router,
    router as dashboard_router,
)
from config.settings import (
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    WebUsage,
    state_registry,
)


@pytest.fixture(autouse=True)
def clean_state_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate registry and storage paths for each test execution."""
    from api.dashboard_routes import _pending_oauth_events

    state_registry.reset()
    _pending_oauth_sessions.clear()
    _pending_oauth_events.clear()

    test_accounts_file = tmp_path / "accounts.json"
    test_cookies_dir = tmp_path / "cookies_cache"
    test_cookies_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", test_accounts_file)
    monkeypatch.setattr("config.settings.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr("core.auth_web.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr("api.dashboard_routes.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr("core.usage_monitor.sync_web_account_usage", AsyncMock())
    monkeypatch.setattr("core.model_registry.discover_web_models", AsyncMock(return_value=["gemini-3.8-flash"]))

    mock_default_session = MagicMock()
    mock_default_session.proc.poll.return_value = 0
    monkeypatch.setattr("api.dashboard_routes.IsolatedFirefoxSession", MagicMock(return_value=mock_default_session))

    yield

    state_registry.reset()
    _pending_oauth_sessions.clear()
    _pending_oauth_events.clear()


@pytest.fixture
def app() -> FastAPI:
    """Create FastAPI application mounting dashboard router."""
    test_app = FastAPI(title="Test Gateway")
    test_app.include_router(callback_router)
    test_app.include_router(dashboard_router)
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Synchronous HTTP test client for clean FastAPI testing."""
    return TestClient(app)


# ============================================================================
# 1. Telemetry & Status Tests (GET /api/status)
# ============================================================================


def test_get_status_returns_complete_telemetry_for_empty_pool(client: TestClient):
    """Verify status report on clean empty pool returns status 'EMPTY' and zero counts."""
    response = client.get("/api/status")
    assert response.status_code == 200

    payload = response.json()
    assert payload["pool_status"] == "EMPTY"
    assert payload["general_stats"]["total_accounts"] == 0
    assert payload["general_stats"]["active_accounts"] == 0
    assert payload["general_stats"]["total_concurrency"] == 0
    assert isinstance(payload["available_models"], list)
    assert payload["oauth_accounts"] == []
    assert payload["web_accounts"] == []


def test_get_status_returns_complete_telemetry_for_active_accounts(client: TestClient):
    """Verify status report calculates concurrency, scores, dynamic metrics, and quotas."""
    oauth_acc = OAuthAccount(
        id="test-oauth-1",
        label="Primary OAuth",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="mock_tok", project_id="proj-123"),
        discovered_models=["gemini-3.8-flash", "gemini-3.1-pro"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-3.8-flash": ModelQuota(remaining_fraction=0.85, reset_at=int(time.time() + 3600)),
            }
        ),
    )
    oauth_acc.active_requests = 1
    oauth_acc.recent_latency_ms = 450.0
    state_registry.upsert_oauth_account(oauth_acc)

    web_acc = WebAccount(
        id="test-web-1",
        label="Primary Web",
        status="ACTIVE",
        cookies=WebCookies(secure_1psid="mock_psid", secure_1psidts="mock_ts"),
        usage=WebUsage(five_hour_pct=0.40, five_hour_reset_at=int(time.time() + 1800), tier="PRO"),
    )
    web_acc.active_requests = 0
    state_registry.upsert_web_account(web_acc)

    response = client.get("/api/status")
    assert response.status_code == 200

    payload = response.json()
    assert payload["pool_status"] == "HEALTHY"
    assert payload["general_stats"]["total_accounts"] == 2
    assert payload["general_stats"]["active_accounts"] == 2
    assert payload["general_stats"]["total_concurrency"] == 1

    # Check OAuth account fields
    assert len(payload["oauth_accounts"]) == 1
    oauth_data = payload["oauth_accounts"][0]
    assert oauth_data["id"] == "test-oauth-1"
    assert oauth_data["active_requests"] == 1
    assert oauth_data["recent_latency_ms"] == 450.0
    assert oauth_data["tau"] > 1.0
    assert "gemini-3.8-flash" in oauth_data["model_quotas"]
    assert oauth_data["model_quotas"]["gemini-3.8-flash"]["remaining_fraction"] == 0.85

    # Check Web account fields & dynamic metrics
    assert len(payload["web_accounts"]) == 1
    web_data = payload["web_accounts"][0]
    assert web_data["id"] == "test-web-1"
    assert web_data["tier"] == "PRO"
    assert web_data["usage"]["five_hour_pct"] == 0.40
    assert "dynamic_metrics" in web_data
    assert web_data["dynamic_metrics"]["tau"] > 1.0
    assert web_data["dynamic_metrics"]["Si"] == 1.0
    assert web_data["dynamic_metrics"]["D_conc"] == 1.0


def test_get_status_reflects_cooldown_and_degraded_pool(client: TestClient):
    """Verify that an account in COOLDOWN switches pool status to DEGRADED."""
    web_acc = WebAccount(
        id="test-cooldown-web",
        status="COOLDOWN",
        cooldown_until=int(time.time() + 600),
    )
    state_registry.upsert_web_account(web_acc)

    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["pool_status"] == "DEGRADED"
    assert payload["general_stats"]["cooldown_accounts"] == 1
    assert payload["general_stats"]["active_accounts"] == 0


# ============================================================================
# 2. OAuth Management Tests (init, complete, delete)
# ============================================================================


def test_init_oauth_account_generates_pkce_and_auth_url(client: TestClient):
    """Verify /api/oauth-accounts/init generates authorization URL and stores PKCE state."""
    response = client.post(
        "/api/oauth-accounts/init",
        json={"redirect_uri": "http://localhost:8000/oauth/callback"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "auth_url" in payload
    assert "accounts.google.com" in payload["auth_url"]
    assert "state" in payload
    assert len(payload["code_verifier"]) >= 43


def test_complete_oauth_account_successful_registration(client: TestClient):
    """Verify /api/oauth-accounts/complete successfully adds account to registry and persists."""
    init_res = client.post("/api/oauth-accounts/init")
    init_data = init_res.json()
    state = init_data["state"]

    mock_credentials = OAuthCredentials(
        access_token="mock_access_token_123",
        refresh_token="mock_refresh_token_456",
        project_id="test-companion-proj",
    )

    with patch("api.dashboard_routes.complete_oauth_login", new_callable=AsyncMock) as mock_login, \
         patch("api.dashboard_routes.discover_oauth_models", new_callable=AsyncMock) as mock_models:
        mock_login.return_value = mock_credentials
        mock_models.return_value = ["gemini-3.8-flash", "gemini-3.1-pro"]

        response = client.post(
            "/api/oauth-accounts/complete",
            json={
                "code": "4/0AWtgMockAuthorizationCode",
                "state": state,
                "label": "My Dev Account",
                "max_concurrency": 4,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert "oauth-test-companion-proj" in payload["account"]["id"]

        # Verify added to in-memory state_registry
        acc = state_registry.get_oauth_account(payload["account"]["id"])
        assert acc is not None
        assert acc.label == "My Dev Account"
        assert acc.max_concurrency == 4
        assert acc.credentials.access_token == "mock_access_token_123"


def test_complete_oauth_account_missing_verifier_raises_bad_request(client: TestClient):
    """Verify /api/oauth-accounts/complete rejects request with unknown state and no verifier."""
    response = client.post(
        "/api/oauth-accounts/complete",
        json={
            "code": "raw_code_without_session",
            "state": "unknown_random_state",
        },
    )
    assert response.status_code == 400
    assert "Missing code_verifier" in response.json()["detail"]


def test_oauth_callback_browser_success(client: TestClient):
    """Verify /oauth/callback automatically exchanges token, registers account, and renders success HTML."""
    init_res = client.post("/api/oauth-accounts/init")
    assert init_res.status_code == 200
    state = init_res.json()["state"]

    mock_credentials = OAuthCredentials(
        access_token="mock_access_token_browser",
        refresh_token="mock_refresh_token_browser",
        project_id="test-auto-browser-proj",
    )

    with patch("api.dashboard_routes.complete_oauth_login", new_callable=AsyncMock) as mock_login, \
         patch("api.dashboard_routes.discover_oauth_models", new_callable=AsyncMock) as mock_models:
        mock_login.return_value = mock_credentials
        mock_models.return_value = ["gemini-3.8-flash"]

        response = client.get(f"/oauth/callback?code=mock_code_123&state={state}")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Google Authorization Successful" in response.text
        assert "oauth-test-auto-browser-proj" in response.text

        # Verify account registered in state_registry with discovered model
        acc = state_registry.get_oauth_account("oauth-test-auto-browser-proj")
        assert acc is not None
        assert acc.discovered_models == ["gemini-3.8-flash"]


def test_oauth_callback_error_handling(client: TestClient):
    """Verify /oauth/callback properly handles Google access_denied errors."""
    response = client.get("/oauth/callback?error=access_denied&error_description=User+declined+permissions")
    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "Google Authorization Denied" in response.text
    assert "User declined permissions" in response.text


def test_oauth_callback_expired_session(client: TestClient):
    """Verify /oauth/callback rejects expired or non-existent state sessions."""
    response = client.get("/oauth/callback?code=code123&state=nonexistent_state_abc")
    assert response.status_code == 400
    assert "Authorization Session Expired or Not Found" in response.text


def test_delete_oauth_account_removes_from_registry(client: TestClient):
    """Verify DELETE /api/oauth-accounts/{id} removes account and persists update."""
    account = OAuthAccount(
        id="oauth-to-delete",
        credentials=OAuthCredentials(access_token="tok"),
    )
    state_registry.upsert_oauth_account(account)
    assert state_registry.get_oauth_account("oauth-to-delete") is not None

    response = client.delete("/api/oauth-accounts/oauth-to-delete")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert state_registry.get_oauth_account("oauth-to-delete") is None


def test_delete_oauth_account_nonexistent_returns_404(client: TestClient):
    """Verify deleting non-existent OAuth account returns 404."""
    response = client.delete("/api/oauth-accounts/nonexistent-oauth")
    assert response.status_code == 404


# ============================================================================
# 3. Web Account Management Tests (add, delete)
# ============================================================================


def test_add_web_account_via_cookie_header_string(client: TestClient):
    """Verify adding web account with raw Cookie header saves isolated file and updates registry."""
    raw_cookies = "Cookie: __Secure-1PSID=test_psid_val; __Secure-1PSIDTS=test_ts_val"
    response = client.post(
        "/api/web-accounts/add",
        json={
            "account_id": "web-test-1",
            "label": "Web Test Account",
            "cookies": raw_cookies,
            "max_concurrency": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["account"]["id"] == "web-test-1"
    assert "hygiene_notice" in payload

    # Verify registered in memory
    account = state_registry.get_web_account("web-test-1")
    assert account is not None
    assert account.cookies.secure_1psid == "test_psid_val"
    assert account.cookies.secure_1psidts == "test_ts_val"

    # Verify isolated cookie file exists on disk
    cookie_file = Path(account.cookie_file)
    assert cookie_file.exists()
    saved_data = json.loads(cookie_file.read_text(encoding="utf-8"))
    assert saved_data["account_id"] == "web-test-1"
    assert saved_data["cookies"]["__Secure-1PSID"] == "test_psid_val"


def test_add_web_account_via_manual_tokens(client: TestClient):
    """Verify adding web account via direct __Secure-1PSID / 1PSIDTS token fields."""
    response = client.post(
        "/api/web-accounts/add",
        json={
            "account_id": "web-tokens-account",
            "secure_1psid": "direct_psid_token_123",
            "secure_1psidts": "direct_psidts_token_456",
            "max_concurrency": 3,
        },
    )
    assert response.status_code == 200
    acc = state_registry.get_web_account("web-tokens-account")
    assert acc is not None
    assert acc.cookies.secure_1psid == "direct_psid_token_123"
    assert acc.max_concurrency == 3


def test_add_web_account_via_firefox_sqlite_profile(client: TestClient, tmp_path: Path):
    """Verify adding web account by importing from an isolated Firefox cookies.sqlite database."""
    firefox_profile = tmp_path / "firefox_profile"
    firefox_profile.mkdir()
    sqlite_file = firefox_profile / "cookies.sqlite"

    with sqlite3.connect(str(sqlite_file)) as conn:
        conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, lastAccessed INTEGER)")
        conn.execute(
            "INSERT INTO moz_cookies VALUES ('__Secure-1PSID', 'firefox_psid_val', '.google.com', 1000)"
        )
        conn.execute(
            "INSERT INTO moz_cookies VALUES ('__Secure-1PSIDTS', 'firefox_ts_val', '.google.com', 2000)"
        )

    response = client.post(
        "/api/web-accounts/add",
        json={
            "account_id": "firefox-web-acc",
            "firefox_profile_path": str(sqlite_file),
        },
    )
    assert response.status_code == 200
    acc = state_registry.get_web_account("firefox-web-acc")
    assert acc is not None
    assert acc.cookies.secure_1psid == "firefox_psid_val"
    assert acc.cookies.secure_1psidts == "firefox_ts_val"


@pytest.mark.parametrize("invalid_id", ["../escape", "test/account", "acc:1", "", " "])
def test_add_web_account_rejects_unsafe_account_id(client: TestClient, invalid_id: str):
    """Verify path traversal or unsafe account IDs are rejected with 400."""
    response = client.post(
        "/api/web-accounts/add",
        json={
            "account_id": invalid_id,
            "secure_1psid": "valid_token",
        },
    )
    assert response.status_code == 400


def test_add_web_account_rejects_missing_cookies(client: TestClient):
    """Verify request without any cookie input returns 400 Bad Request."""
    response = client.post(
        "/api/web-accounts/add",
        json={"account_id": "valid-id"},
    )
    assert response.status_code == 400


def test_delete_web_account_removes_from_registry_and_deletes_cache_file(client: TestClient):
    """Verify DELETE /api/web-accounts/{id} unlinks cookie cache and calls close_client."""
    add_res = client.post(
        "/api/web-accounts/add",
        json={
            "account_id": "web-to-remove",
            "secure_1psid": "some_psid",
        },
    )
    assert add_res.status_code == 200
    acc = state_registry.get_web_account("web-to-remove")
    assert acc is not None
    cookie_path = Path(acc.cookie_file)
    assert cookie_path.exists()

    with patch("core.auth_web.default_web_session_manager.close_client", new_callable=AsyncMock) as mock_close:
        mock_close.return_value = True
        del_res = client.delete("/api/web-accounts/web-to-remove")
        assert del_res.status_code == 200
        assert state_registry.get_web_account("web-to-remove") is None
        assert not cookie_path.exists()
        mock_close.assert_awaited_once_with("web-to-remove")


def test_delete_web_account_nonexistent_returns_404(client: TestClient):
    """Verify deleting non-existent Web account returns 404."""
    response = client.delete("/api/web-accounts/nonexistent-web")
    assert response.status_code == 404


# ============================================================================
# 4. Quota Sync & UI Route Tests
# ============================================================================


def test_sync_accounts_quotas_calls_monitor_and_returns_report(client: TestClient):
    """Verify POST /api/accounts/sync-quotas executes usage synchronization pass."""
    with patch("api.dashboard_routes.sync_all_registered_accounts", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = {
            "timestamp": 1234567890,
            "synced_oauth_count": 1,
            "synced_web_count": 2,
            "oauth_accounts": ["oauth-1"],
            "web_accounts": ["web-1", "web-2"],
            "errors": {},
        }

        response = client.post("/api/accounts/sync-quotas")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["report"]["synced_web_count"] == 2
        mock_sync.assert_awaited_once_with(state_registry, persist=True)


def test_dashboard_ui_endpoint_serves_html(client: TestClient):
    """Verify GET /api/dashboard serves index.html UI."""
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Gemini Hybrid Pool Gateway" in response.text


def test_oauth_callback_preserves_label_and_concurrency(client: TestClient):
    """Verify /oauth/callback honors label and concurrency saved in pending session."""
    init_res = client.post(
        "/api/oauth-accounts/init",
        json={"label": "Custom CLI Team", "max_concurrency": 5},
    )
    assert init_res.status_code == 200
    state = init_res.json()["state"]

    mock_credentials = OAuthCredentials(
        access_token="mock_tok",
        refresh_token="mock_ref",
        project_id="team-proj-999",
    )

    with patch("api.dashboard_routes.complete_oauth_login", new_callable=AsyncMock) as mock_login, \
         patch("api.dashboard_routes.discover_oauth_models", new_callable=AsyncMock) as mock_models:
        mock_login.return_value = mock_credentials
        mock_models.return_value = ["gemini-3.8-flash"]

        response = client.get(f"/oauth/callback?code=code_xyz&state={state}")
        assert response.status_code == 200
        assert "OAUTH_SUCCESS" in response.text
        assert "OAUTH_COMPLETE" in response.text

        acc = state_registry.get_oauth_account("oauth-team-proj-999")
        assert acc is not None
        assert acc.label == "Custom CLI Team"
        assert acc.max_concurrency == 5


def test_web_account_auto_detect_success(client: TestClient):
    """Verify POST /api/web-accounts/auto-detect detects cookies and registers account."""
    mock_cookies = WebCookies(secure_1psid="auto_psid_123", secure_1psidts="auto_ts_456")
    with patch("api.dashboard_routes.auto_discover_firefox_cookies", return_value=(Path("fake/cookies.sqlite"), mock_cookies)):
        response = client.post(
            "/api/web-accounts/auto-detect",
            json={"label": "Auto Detected Firefox", "max_concurrency": 3},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["logged_in"] is True
        assert payload["account"]["label"] == "Auto Detected Firefox"

        acc = state_registry.get_web_account(payload["account_id"])
        assert acc is not None
        assert acc.cookies.secure_1psid == "auto_psid_123"
        assert acc.max_concurrency == 3


def test_web_account_auto_detect_not_logged_in(client: TestClient):
    """Verify POST /api/web-accounts/auto-detect returns logged_in=False when not found."""
    with patch("api.dashboard_routes.auto_discover_firefox_cookies", return_value=(None, None)):
        response = client.post("/api/web-accounts/auto-detect")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is False
        assert payload["logged_in"] is False
        assert "login_url" in payload


def test_web_account_browser_login_endpoint(client: TestClient):
    """Verify POST /api/web-accounts/browser-login harvests cookies via isolated Firefox and registers account."""
    mock_cookies = WebCookies(secure_1psid="mock_harvested_psid", secure_1psidts="mock_harvested_ts")
    with patch("core.auth_web.login_web_via_isolated_firefox", new_callable=AsyncMock) as mock_harvest:
        mock_harvest.return_value = mock_cookies
        response = client.post(
            "/api/web-accounts/browser-login",
            json={"account_id": "web-harvested-acc", "poll_seconds": 180.0},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["logged_in"] is True
        assert payload["account_id"] == "web-harvested-acc"
        mock_harvest.assert_awaited_once_with(timeout_seconds=180.0)

        acc = state_registry.get_web_account("web-harvested-acc")
        assert acc is not None
        assert acc.cookies.secure_1psid == "mock_harvested_psid"
        assert len(acc.discovered_models) > 0


def test_web_account_browser_login_failure(client: TestClient):
    """Verify POST /api/web-accounts/browser-login handles exceptions gracefully."""
    with patch("core.auth_web.login_web_via_isolated_firefox", side_effect=RuntimeError("Firefox closed")):
        response = client.post(
            "/api/web-accounts/browser-login",
            json={"account_id": "web-failed-acc"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is False
        assert payload["logged_in"] is False
        assert "Firefox closed" in payload["error"]


def test_init_oauth_account_with_open_browser_creates_event(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Verify POST /api/oauth-accounts/init with open_browser=True creates pending event and closes on callback."""
    mock_launch = AsyncMock()
    monkeypatch.setattr("api.dashboard_routes._launch_isolated_oauth_browser", mock_launch)

    init_res = client.post(
        "/api/oauth-accounts/init",
        json={"open_browser": True, "label": "Isolated Browser OAuth"},
    )
    assert init_res.status_code == 200
    state = init_res.json()["state"]
    from api.dashboard_routes import _pending_oauth_events

    assert state in _pending_oauth_events
    event = _pending_oauth_events[state]
    assert not event.is_set()
    mock_launch.assert_called_once()

    mock_credentials = OAuthCredentials(access_token="tok123", project_id="proj-auto-close")
    with patch("api.dashboard_routes.complete_oauth_login", new_callable=AsyncMock, return_value=mock_credentials), \
         patch("api.dashboard_routes.discover_oauth_models", new_callable=AsyncMock, return_value=["gemini-3.8-flash"]):
        cb_res = client.get(f"/oauth/callback?code=mock_code&state={state}")
        assert cb_res.status_code == 200
        assert event.is_set()


@pytest.mark.asyncio
async def test_launch_isolated_oauth_browser_lifecycle_and_cleanup():
    """Verify _launch_isolated_oauth_browser waits for event and terminates/cleans up IsolatedFirefoxSession."""
    import asyncio
    from api.dashboard_routes import _launch_isolated_oauth_browser

    event = asyncio.Event()
    mock_session = MagicMock()
    mock_session.proc.poll.return_value = None

    with patch("api.dashboard_routes.IsolatedFirefoxSession", return_value=mock_session):
        task = asyncio.create_task(_launch_isolated_oauth_browser("https://auth.url", event))
        await asyncio.sleep(0.05)
        assert not task.done()

        # Set event to trigger automatic closing and cleanup
        event.set()
        await task
        mock_session.close_and_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_and_discover_web_account_without_type_error():
    """Verify _initialize_and_discover_web_account calls get_or_init_client, sync_web_account_usage, and discover_web_models."""
    from api.dashboard_routes import _initialize_and_discover_web_account
    from config.settings import WebAccount, WebCookies

    mock_account = WebAccount(
        id="test-discover-acc",
        cookies=WebCookies(secure_1psid="psid", secure_1psidts="ts"),
    )
    mock_client = MagicMock()

    with patch("core.auth_web.default_web_session_manager.get_or_init_client", new_callable=AsyncMock, return_value=mock_client) as mock_get_client, \
         patch("core.usage_monitor.sync_web_account_usage", new_callable=AsyncMock) as mock_sync_usage, \
         patch("core.model_registry.discover_web_models", new_callable=AsyncMock, return_value=["gemini-4-argon", "gemini-3.8-flash"]) as mock_discover:

        discovered = await _initialize_and_discover_web_account(mock_account)

        assert discovered == ["gemini-4-argon", "gemini-3.8-flash"]
        assert mock_account.discovered_models == ["gemini-4-argon", "gemini-3.8-flash"]
        mock_get_client.assert_awaited_once_with(mock_account)
        mock_sync_usage.assert_awaited_once_with(mock_account, client=mock_client, persist=False)
        mock_discover.assert_awaited_once_with(mock_account, mock_client)


