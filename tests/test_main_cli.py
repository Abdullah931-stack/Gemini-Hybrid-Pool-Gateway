"""Integration and CLI test suite for main.py (Gemini-Hybrid-Pool-Gateway).

Verifies:
1. Application factory (create_app) configuration:
   - Registers OpenAI routes (/v1/models, /v1/chat/completions)
   - Registers Dashboard routes (/api/status, /api/accounts/sync-quotas)
   - Mounts static files at /static
   - Exposes root / and /dashboard serving static/index.html
   - Enables permissive CORS middleware
2. Lifespan lifecycle management (@asynccontextmanager):
   - Startup: loads accounts into memory, cleans orphaned temp files,
     initializes & starts APScheduler with 3 periodic tasks.
   - Shutdown: shuts down APScheduler, flushes web cookies, saves atomic snapshot.
3. Click CLI Engine:
   - serve: starts uvicorn with default or custom options (--host, --port, --reload)
   - list-accounts / --list-accounts: pretty table for empty and populated pool
   - status / health / --status: summary report of pool state, accounts, and models
   - sync-quotas / --sync-quotas: executes immediate sync and persists snapshot
   - add-oauth: interactive Google OAuth PKCE wizard
   - add-web: interactive Web Cookie account registration wizard
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest

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
from core.auth_oauth_cli import PKCEChallenge
from core.model_registry import unified_model_registry
import main
from main import app, cli, create_app, lifespan


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clean_runtime_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate registry and test paths before and after each test."""
    state_registry.reset()
    test_accounts_file = tmp_path / "accounts.json"
    test_cookies_dir = tmp_path / "cookies_cache"
    test_cookies_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", test_accounts_file)
    monkeypatch.setattr("config.settings.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr("core.auth_web.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr(main, "auto_discover_firefox_cookies", lambda: (None, None))

    yield

    state_registry.reset()


@pytest.fixture
def runner() -> CliRunner:
    """Click CLI test runner fixture."""
    return CliRunner()


# ============================================================================
# 1. Application Factory & Router Registration Tests
# ============================================================================


def test_create_app_registers_all_required_routes_and_middleware():
    """Application factory must register OpenAI, Dashboard, Static, and Root routes."""
    test_app = create_app()

    assert test_app.title == "Gemini Hybrid Pool Gateway"

    route_paths: list[str] = []
    for r in test_app.routes:
        if getattr(r, "path", None):
            route_paths.append(r.path)
        if hasattr(r, "original_router") and hasattr(r.original_router, "routes"):
            for sub in r.original_router.routes:
                if getattr(sub, "path", None):
                    route_paths.append(sub.path)

    # Required routes according to Level 5 specification
    assert "/v1/models" in route_paths
    assert "/v1/models/{model_id}" in route_paths
    assert "/v1/chat/completions" in route_paths
    assert "/api/status" in route_paths
    assert "/api/accounts/sync-quotas" in route_paths
    assert "/" in route_paths
    assert "/dashboard" in route_paths

    # Static mount check
    static_mounts = [r for r in test_app.routes if getattr(r, "name", "") == "static"]
    assert len(static_mounts) > 0
    assert static_mounts[0].path == "/static"

    # CORS middleware check
    middleware_types = [m.cls.__name__ for m in test_app.user_middleware]
    assert "CORSMiddleware" in middleware_types


def test_root_and_dashboard_endpoints_serve_index_html():
    """GET / and GET /dashboard must both return static/index.html with HTTP 200."""
    with TestClient(app) as client:
        resp_root = client.get("/")
        assert resp_root.status_code == 200
        assert "text/html" in resp_root.headers.get("content-type", "")
        assert "<!DOCTYPE html>" in resp_root.text

        resp_dash = client.get("/dashboard")
        assert resp_dash.status_code == 200
        assert "text/html" in resp_dash.headers.get("content-type", "")
        assert "<!DOCTYPE html>" in resp_dash.text


def test_dashboard_endpoint_returns_404_when_index_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """GET / and GET /dashboard must return 404 when static/index.html is absent."""
    non_existent_html = tmp_path / "absent_index.html"
    monkeypatch.setattr(main, "INDEX_HTML_PATH", non_existent_html)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 404
        assert "Dashboard UI not found" in response.json()["detail"]


# ============================================================================
# 2. Lifespan Lifecycle Tests
# ============================================================================


@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown_lifecycle(monkeypatch: pytest.MonkeyPatch):
    """Lifespan must properly invoke startup loaders & scheduler, and shutdown cleaners."""
    mock_load_accounts = MagicMock()
    mock_cleanup_orphans = MagicMock()
    mock_sync_quotas = AsyncMock()
    mock_flush_cookies = AsyncMock()
    mock_cleanup_chats = AsyncMock()
    mock_save_snapshot = AsyncMock()

    mock_scheduler = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.shutdown = MagicMock()
    mock_scheduler.add_job = MagicMock()

    monkeypatch.setattr(main, "load_accounts_into_memory", mock_load_accounts)
    monkeypatch.setattr(main, "cleanup_orphaned_temp_files", mock_cleanup_orphans)
    monkeypatch.setattr(main, "sync_all_registered_accounts", mock_sync_quotas)
    monkeypatch.setattr(main.web_session_manager, "flush_all_cookies", mock_flush_cookies)
    monkeypatch.setattr(main.web_session_manager, "cleanup_expired_chats", mock_cleanup_chats)
    monkeypatch.setattr(main, "save_accounts_snapshot", mock_save_snapshot)
    monkeypatch.setattr(main, "AsyncIOScheduler", lambda: mock_scheduler)

    test_app = create_app()

    async with lifespan(test_app):
        # Startup assertions
        mock_load_accounts.assert_called_once()
        mock_cleanup_orphans.assert_called_once()
        mock_scheduler.start.assert_called_once()
        assert mock_scheduler.add_job.call_count == 3
        assert getattr(test_app.state, "scheduler", None) is mock_scheduler

    # Shutdown assertions
    mock_scheduler.shutdown.assert_called_once_with(wait=False)
    mock_flush_cookies.assert_awaited_once()
    mock_save_snapshot.assert_awaited_once()


# ============================================================================
# 3. CLI Engine: serve Options
# ============================================================================


def test_cli_serve_invokes_uvicorn_with_custom_options(runner: CliRunner, monkeypatch: pytest.MonkeyPatch):
    """CLI serve command must pass custom host, port, and reload flag to uvicorn.run."""
    mock_uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", mock_uvicorn_run)

    result = runner.invoke(cli, ["serve", "--host", "0.0.0.0", "--port", "9090", "--reload"])

    assert result.exit_code == 0
    assert "Starting Gemini Hybrid Pool Gateway on http://0.0.0.0:9090" in result.output
    mock_uvicorn_run.assert_called_once_with(
        "main:app",
        host="0.0.0.0",
        port=9090,
        reload=True,
    )


def test_cli_serve_invokes_uvicorn_with_default_options(runner: CliRunner, monkeypatch: pytest.MonkeyPatch):
    """CLI serve command without flags must use 127.0.0.1, port 8000, reload=False."""
    mock_uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", mock_uvicorn_run)

    result = runner.invoke(cli, ["serve"])

    assert result.exit_code == 0
    assert "Starting Gemini Hybrid Pool Gateway on http://127.0.0.1:8000" in result.output
    mock_uvicorn_run.assert_called_once_with(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


# ============================================================================
# 4. CLI Engine: list-accounts & --list-accounts
# ============================================================================


def test_cli_list_accounts_empty_pool_displays_helpful_message(runner: CliRunner):
    """list-accounts command must inform user when no accounts are currently registered."""
    result = runner.invoke(cli, ["list-accounts"])

    assert result.exit_code == 0
    assert "No accounts registered" in result.output
    assert "add-oauth" in result.output


def test_cli_list_accounts_flag_displays_helpful_message(runner: CliRunner):
    """--list-accounts top-level flag must invoke list_accounts handler."""
    result = runner.invoke(cli, ["--list-accounts"])

    assert result.exit_code == 0
    assert "No accounts registered" in result.output


def test_cli_list_accounts_renders_formatted_table_with_registered_accounts(runner: CliRunner):
    """list-accounts must render an elegant table showing IDs, channels, status, and quotas."""
    oauth_acc = OAuthAccount(
        id="oauth-prod-1",
        credentials=OAuthCredentials(access_token="tok-1", project_id="test-proj"),
        status="ACTIVE",
        discovered_models=["gemini-2.5-pro", "gemini-2.5-flash"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-2.5-pro": ModelQuota(remaining_fraction=0.95),
                "gemini-2.5-flash": ModelQuota(remaining_fraction=1.0),
            }
        ),
    )
    web_acc = WebAccount(
        id="web-prod-1",
        cookies=WebCookies(secure_1psid="psid_test", secure_1psidts="psidts_test"),
        status="ACTIVE",
        usage=WebUsage(five_hour_pct=0.25, weekly_pct=0.10),
    )
    state_registry.upsert_oauth_account(oauth_acc)
    state_registry.upsert_web_account(web_acc)

    result = runner.invoke(cli, ["list-accounts"])

    assert result.exit_code == 0
    assert "oauth-prod-1" in result.output
    assert "web-prod-1" in result.output
    assert "oauth" in result.output
    assert "web" in result.output
    assert "ACTIVE" in result.output
    assert "gemini-2.5-pro: 95%" in result.output
    assert "5h: 25.0%" in result.output


# ============================================================================
# 5. CLI Engine: status, health & --status
# ============================================================================


def test_cli_status_and_health_commands_report_pool_telemetry(runner: CliRunner):
    """status, health, and --status must all display pool health and model counts."""
    oauth_acc = OAuthAccount(
        id="oauth-acc-test",
        credentials=OAuthCredentials(access_token="tok"),
        status="ACTIVE",
    )
    state_registry.upsert_oauth_account(oauth_acc)

    # 1. status command
    res1 = runner.invoke(cli, ["status"])
    assert res1.exit_code == 0
    assert "Gemini Hybrid Pool Gateway Status" in res1.output
    assert "Pool Health:        HEALTHY" in res1.output
    assert "OAuth (CLI):    1" in res1.output

    # 2. health command alias
    res2 = runner.invoke(cli, ["health"])
    assert res2.exit_code == 0
    assert "Pool Health:        HEALTHY" in res2.output

    # 3. --status top-level flag
    res3 = runner.invoke(cli, ["--status"])
    assert res3.exit_code == 0
    assert "Pool Health:        HEALTHY" in res3.output


# ============================================================================
# 6. CLI Engine: sync-quotas & --sync-quotas
# ============================================================================


def test_cli_sync_quotas_invokes_sync_and_persists_snapshot(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """sync-quotas must execute usage monitor pass and persist updated state."""
    mock_sync = AsyncMock()
    mock_save = AsyncMock()

    monkeypatch.setattr(main, "sync_all_registered_accounts", mock_sync)
    monkeypatch.setattr(main, "save_accounts_snapshot", mock_save)

    result = runner.invoke(cli, ["sync-quotas"])

    assert result.exit_code == 0
    assert "Quota synchronization completed successfully" in result.output
    mock_sync.assert_awaited_once_with(state_registry)
    mock_save.assert_awaited_once()


def test_cli_sync_quotas_flag_invokes_sync(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """--sync-quotas top-level flag must invoke sync_quotas handler."""
    mock_sync = AsyncMock()
    mock_save = AsyncMock()

    monkeypatch.setattr(main, "sync_all_registered_accounts", mock_sync)
    monkeypatch.setattr(main, "save_accounts_snapshot", mock_save)

    result = runner.invoke(cli, ["--sync-quotas"])

    assert result.exit_code == 0
    assert "Quota synchronization completed successfully" in result.output


def test_cli_sync_quotas_handles_failure_cleanly(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """sync-quotas must catch exceptions and raise ClickException with error details."""
    async def _failing_sync(registry: Any) -> None:
        raise RuntimeError("Network timeout while syncing quotas")

    monkeypatch.setattr(main, "sync_all_registered_accounts", _failing_sync)

    result = runner.invoke(cli, ["sync-quotas"])

    assert result.exit_code != 0
    assert "Quota sync failed: Network timeout while syncing quotas" in result.output


# ============================================================================
# 7. CLI Engine: add-oauth & add-web Interactive Wizards
# ============================================================================


def test_cli_add_oauth_wizard_registers_account_successfully(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """add-oauth must run interactive flow, exchange code, and persist new account."""
    mock_pkce = PKCEChallenge(
        code_verifier="test_verifier_string_of_sufficient_length_1234567890",
        code_challenge="test_challenge",
        state="test_state_123",
    )
    mock_credentials = OAuthCredentials(
        access_token="mock_access_token",
        project_id="discovered-project-123",
    )

    monkeypatch.setattr(main, "generate_pkce_pair", lambda: mock_pkce)
    monkeypatch.setattr(main, "build_authorization_url", lambda **kwargs: "https://mock.auth.url")
    monkeypatch.setattr(main, "extract_code_from_manual_input", lambda inp, expected_state: "auth_code_xyz")
    monkeypatch.setattr(main, "complete_oauth_login", AsyncMock(return_value=mock_credentials))
    monkeypatch.setattr(main, "discover_oauth_models", AsyncMock(return_value=["gemini-2.5-pro"]))
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    # User inputs: Account ID -> Redirect URL/Code
    inputs = "my-oauth-acc\nhttp://localhost:8000/oauth/callback?code=auth_code_xyz&state=test_state_123\n"
    result = runner.invoke(cli, ["add-oauth"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered OAuth account 'my-oauth-acc'" in result.output
    registered = state_registry.get_oauth_account("my-oauth-acc")
    assert registered is not None
    assert registered.credentials.project_id == "discovered-project-123"


def test_cli_add_oauth_wizard_rejects_unsafe_account_id(runner: CliRunner):
    """add-oauth must reject account IDs containing unsafe path-traversal characters."""
    result = runner.invoke(cli, ["add-oauth"], input="../evil_id\n")

    assert result.exit_code != 0
    assert "Account ID must be alphanumeric" in result.output


def test_cli_add_web_wizard_registers_account_with_manual_cookies(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """add-web with option 2 must parse manual cookies and register account."""
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    # User inputs: Account ID -> Choice 2 (Manual) -> __Secure-1PSID -> __Secure-1PSIDTS
    inputs = "my-web-cli-acc\n2\nsid_value_xyz\nts_value_abc\n"
    result = runner.invoke(cli, ["add-web"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered Web account 'my-web-cli-acc'" in result.output
    assert "Hygiene Notice" in result.output

    registered = state_registry.get_web_account("my-web-cli-acc")
    assert registered is not None
    assert registered.cookies.secure_1psid == "sid_value_xyz"
    assert registered.cookies.secure_1psidts == "ts_value_abc"


def test_cli_add_web_wizard_registers_account_with_firefox_profile(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """add-web with option 1 must extract cookies from Firefox profile and register account."""
    mock_cookies = WebCookies(secure_1psid="ff_psid_123", secure_1psidts="ff_psidts_456")
    monkeypatch.setattr(main, "extract_firefox_cookies", lambda path: mock_cookies)
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    # User inputs: Account ID -> Choice 1 (Firefox) -> Profile Path
    inputs = "my-firefox-acc\n1\nC:\\Users\\Profile\\cookies.sqlite\n"
    result = runner.invoke(cli, ["add-web"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered Web account 'my-firefox-acc'" in result.output

    registered = state_registry.get_web_account("my-firefox-acc")
    assert registered is not None
    assert registered.cookies.secure_1psid == "ff_psid_123"


def test_cli_add_oauth_flag_with_label_and_mode(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """Top-level --add-oauth flag with --label and --mode must succeed and configure account."""
    mock_pkce = PKCEChallenge(
        code_verifier="test_verifier_string_of_sufficient_length_1234567890",
        code_challenge="test_challenge",
        state="test_state_123",
    )
    mock_credentials = OAuthCredentials(
        access_token="mock_access_token",
        project_id="discovered-project-123",
    )

    monkeypatch.setattr(main, "generate_pkce_pair", lambda: mock_pkce)
    monkeypatch.setattr(main, "build_authorization_url", lambda **kwargs: "https://mock.auth.url")
    monkeypatch.setattr(main, "extract_code_from_manual_input", lambda inp, expected_state: "auth_code_xyz")
    monkeypatch.setattr(main, "complete_oauth_login", AsyncMock(return_value=mock_credentials))
    monkeypatch.setattr(main, "discover_oauth_models", AsyncMock(return_value=["gemini-2.5-pro"]))
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    inputs = "\nhttp://localhost:8000/oauth/callback?code=auth_code_xyz&state=test_state_123\n"
    result = runner.invoke(cli, ["--add-oauth", "--label", "test-label", "--mode", "http_v1internal"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered OAuth account 'test-label'" in result.output
    registered = state_registry.get_oauth_account("test-label")
    assert registered is not None
    assert registered.label == "test-label"
    assert registered.transport_mode == "http_v1internal"


def test_cli_add_oauth_subcommand_with_label_and_mode(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """Subcommand add-oauth with --label and --mode must succeed and configure account."""
    mock_pkce = PKCEChallenge(
        code_verifier="test_verifier_string_of_sufficient_length_1234567890",
        code_challenge="test_challenge",
        state="test_state_123",
    )
    mock_credentials = OAuthCredentials(
        access_token="mock_access_token",
        project_id="discovered-project-456",
    )

    monkeypatch.setattr(main, "generate_pkce_pair", lambda: mock_pkce)
    monkeypatch.setattr(main, "build_authorization_url", lambda **kwargs: "https://mock.auth.url")
    monkeypatch.setattr(main, "extract_code_from_manual_input", lambda inp, expected_state: "auth_code_xyz")
    monkeypatch.setattr(main, "complete_oauth_login", AsyncMock(return_value=mock_credentials))
    monkeypatch.setattr(main, "discover_oauth_models", AsyncMock(return_value=["gemini-2.5-flash"]))
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    inputs = "\nhttp://localhost:8000/oauth/callback?code=auth_code_xyz&state=test_state_123\n"
    result = runner.invoke(cli, ["add-oauth", "--label", "test-label", "--mode", "agy_subprocess"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered OAuth account 'test-label'" in result.output
    registered = state_registry.get_oauth_account("test-label")
    assert registered is not None
    assert registered.label == "test-label"
    assert registered.transport_mode == "agy_subprocess"


def test_cli_add_web_flag_with_label_and_firefox_profile(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """Top-level --add-web flag with --label and --firefox-profile must bypass cookie prompt."""
    mock_cookies = WebCookies(secure_1psid="ff_psid_direct", secure_1psidts="ff_psidts_direct")
    monkeypatch.setattr(main, "extract_firefox_cookies", lambda path: mock_cookies)
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    inputs = "\n"
    result = runner.invoke(cli, ["--add-web", "--label", "test-web", "--firefox-profile", "dummy"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered Web account 'test-web'" in result.output
    registered = state_registry.get_web_account("test-web")
    assert registered is not None
    assert registered.label == "test-web"
    assert registered.cookies.secure_1psid == "ff_psid_direct"
    assert registered.cookies.secure_1psidts == "ff_psidts_direct"


def test_cli_add_web_subcommand_with_label_and_firefox_profile(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """Subcommand add-web with --label and --firefox-profile must bypass cookie prompt."""
    mock_cookies = WebCookies(secure_1psid="ff_psid_sub", secure_1psidts="ff_psidts_sub")
    monkeypatch.setattr(main, "extract_firefox_cookies", lambda path: mock_cookies)
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    inputs = "\n"
    result = runner.invoke(cli, ["add-web", "--label", "test-web-sub", "--firefox-profile", "dummy"], input=inputs)

    assert result.exit_code == 0
    assert "Successfully registered Web account 'test-web-sub'" in result.output
    registered = state_registry.get_web_account("test-web-sub")
    assert registered is not None
    assert registered.label == "test-web-sub"
    assert registered.cookies.secure_1psid == "ff_psid_sub"


def test_cli_add_web_auto_detect_from_firefox_profile(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """add-web without profile must auto-detect Firefox cookies and register without prompts."""
    mock_cookies = WebCookies(secure_1psid="auto_psid_zero_input", secure_1psidts="auto_ts_zero_input")
    monkeypatch.setattr(main, "auto_discover_firefox_cookies", lambda: (Path("fake/cookies.sqlite"), mock_cookies))
    monkeypatch.setattr(main, "save_accounts_snapshot", AsyncMock())

    result = runner.invoke(cli, ["add-web", "--id", "zero-copy-web"])

    assert result.exit_code == 0
    assert "Automatically detected Gemini cookies from Firefox" in result.output
    assert "Successfully registered Web account 'zero-copy-web'" in result.output
    registered = state_registry.get_web_account("zero-copy-web")
    assert registered is not None
    assert registered.cookies.secure_1psid == "auto_psid_zero_input"


def test_cli_add_oauth_automatic_callback_success(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
):
    """add-oauth in interactive mode must capture code via OAuthCallbackServer without manual paste."""
    mock_pkce = PKCEChallenge(
        code_verifier="test_verifier_string_of_sufficient_length_1234567890",
        code_challenge="test_challenge",
        state="test_state_123",
    )
    mock_credentials = OAuthCredentials(
        access_token="mock_access_token_auto",
        project_id="auto-discovered-proj",
    )

    monkeypatch.setattr(main, "generate_pkce_pair", lambda: mock_pkce)
    monkeypatch.setattr(main, "build_authorization_url", lambda **kwargs: "https://mock.auth.url")
    monkeypatch.setattr(main, "complete_oauth_login", AsyncMock(return_value=mock_credentials))
    monkeypatch.setattr(main, "discover_oauth_models", AsyncMock(return_value=["gemini-3.8-flash"]))
    monkeypatch.setattr(main, "_is_interactive_terminal", lambda: True)

    with patch("webbrowser.open") as mock_browser, \
         patch.object(main.OAuthCallbackServer, "start", new_callable=AsyncMock) as mock_start, \
         patch.object(main.OAuthCallbackServer, "wait_for_code", new_callable=AsyncMock) as mock_wait:
        mock_start.return_value = 8000
        mock_wait.return_value = "auto_captured_code_789"

        result = runner.invoke(cli, ["add-oauth", "--id", "auto-oauth-zero-copy"])

        assert result.exit_code == 0
        assert "Opening Google authorization page in your browser" in result.output
        assert "Successfully registered OAuth account 'auto-oauth-zero-copy'" in result.output
        mock_browser.assert_called_once_with("https://mock.auth.url")
        registered = state_registry.get_oauth_account("auto-oauth-zero-copy")
        assert registered is not None
        assert registered.credentials.project_id == "auto-discovered-proj"


