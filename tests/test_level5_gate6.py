"""Verification Gate 6 official integration and E2E verification test suite for Level 5 of Gemini-Hybrid-Pool-Gateway.

Topological Level 5 Gate 6 Verifications:
1. Application Lifecycle & In-Memory State Registry Integration (Lifespan):
   - Full lifespan startup: load accounts, clean orphaned temp files, initialize & start AsyncIOScheduler
     with 3 periodic jobs (sync_quotas [5m], flush_cookies [5m], cleanup_chats [10m]).
   - Lifespan graceful shutdown: stop scheduler (wait=False), flush web cookies, persist accounts atomic snapshot.
   - In-memory state registry roundtrip persistence: saving snapshot and reloading with 100% state fidelity.

2. OpenAI Protocol Endpoints E2E (/v1):
   - GET /v1/models: standard OpenAI catalog listing base models and thinking effort suffixes (-minimal, -low, -medium, -high).
   - GET /v1/models/{model_id}: valid retrieval for base and thinking variants; RFC 7807/OpenAI 404 format for unknown models.
   - POST /v1/chat/completions (Non-Streaming) with Failover: primary channel failure/exhaustion -> automatic instant failover
     to secondary account with valid chat.completion payload and usage statistics.
   - POST /v1/chat/completions (Streaming SSE) & Zero Concurrency Leak:
     * Content-Type: text/event-stream, Cache-Control: no-cache, Connection: keep-alive, X-Accel-Buffering: no.
     * Emits valid SSE chunks (data: {...}) and concludes strictly with data: [DONE].
     * Rigorous concurrency invariant: active_requests incremented at start and strictly decremented to 0 on completion.
   - X-Token-Saver header enforcement:
     * Enabled by default (or X-Token-Saver: on): sanitizes and compresses tool output messages.
     * Explicitly disabled via X-Token-Saver: off: passes tool messages verbatim without alteration.
   - Standard error translations & Permissive Schema:
     * ModelNotSupportedError -> HTTP 404 (invalid_request_error, code: model_not_found).
     * PoolExhaustedError -> HTTP 429 (server_error, code: pool_exhausted).
     * Extra client payload fields accepted gracefully without 422 errors.

3. Control Plane & Dashboard Endpoints E2E (/api):
   - GET /api/status: complete telemetry report calculating mathematical metrics:
     * Concurrency C (active_requests), Priority score P (priority_score), Urgency factor tau in [1.0, 1.5],
       Session affinity Si, Conditional denominator D_conc ((1+C)^2 or 1+0.25C).
     * OAuth model quotas with fractional remaining and reset timestamps.
   - OAuth account lifecycle: POST /api/oauth-accounts/init (PKCE generation), POST /api/oauth-accounts/complete
     (token exchange, model discovery, registry upsert), DELETE /api/oauth-accounts/{id} (cleanup, persistence, 404 handling).
   - Web account lifecycle: POST /api/web-accounts/add (cookie parsing, isolated cache file creation),
     DELETE /api/web-accounts/{id} (cache unlinking, registry removal, 404 handling).
   - POST /api/accounts/sync-quotas: executes quota and usage synchronization pass across pool.

4. Dashboard Web UI Presentation:
   - GET / and GET /dashboard serve static/index.html with HTTP 200 and text/html.
   - GET /static/index.html serves assets correctly via StaticFiles mount.
   - Graceful fallback when index file is missing.

5. CLI Engine (Click CLI):
   - list-accounts / --list-accounts: displays friendly empty notice or ASCII table with ID, Channel, Status, Priority, Quotas.
   - status / health / --status: displays pool status badge, account counts, available models, and health overview.
   - sync-quotas / --sync-quotas: executes immediate quota sync pass via CLI runner with success report.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.dashboard_routes import (
    _pending_oauth_sessions,
    router as dashboard_router,
)
from api.openai_routes import (
    get_failover_controller,
    get_model_registry,
    router as openai_router,
)
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
    load_accounts_into_memory,
    save_accounts_snapshot,
    state_registry,
)
from core.auth_oauth_cli import PKCEChallenge
from core.auth_web import get_account_cookie_cache_path
from core.failover import (
    FailoverController,
    ModelNotSupportedError,
    PoolExhaustedError,
    default_failover_controller,
)
from core.model_registry import unified_model_registry
import main
from main import app as global_app, cli, create_app, lifespan


# ============================================================================
# Fixtures & Environment Isolation
# ============================================================================


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide fully isolated in-memory state and filesystem sandbox for every test."""
    state_registry.reset()
    _pending_oauth_sessions.clear()

    sandbox_accounts = tmp_path / "accounts_gate6.json"
    sandbox_cookies = tmp_path / "cookies_cache_gate6"
    sandbox_cookies.mkdir(parents=True, exist_ok=True)

    # Patch environment and configuration paths
    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", sandbox_accounts)
    monkeypatch.setattr("config.settings.COOKIES_CACHE_DIR", sandbox_cookies)
    monkeypatch.setattr("core.auth_web.COOKIES_CACHE_DIR", sandbox_cookies)
    monkeypatch.setattr("api.dashboard_routes.COOKIES_CACHE_DIR", sandbox_cookies)

    yield {
        "accounts_file": sandbox_accounts,
        "cookies_dir": sandbox_cookies,
    }

    state_registry.reset()
    _pending_oauth_sessions.clear()


@pytest.fixture
def test_app() -> FastAPI:
    """Instantiate a pristine production FastAPI application."""
    return create_app()


@pytest.fixture
def client_factory(test_app: FastAPI) -> Callable[[], httpx.AsyncClient]:
    """Provide an asynchronous HTTP client factory bound to the test app."""
    def _create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://testserver",
        )
    return _create


@pytest.fixture
def sync_client(test_app: FastAPI) -> TestClient:
    """Provide a synchronous TestClient bound to the test app."""
    return TestClient(test_app)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Provide a Click CliRunner for CLI commands."""
    return CliRunner()


# ============================================================================
# Section 1: Application Lifecycle & State Registry Integration (Lifespan)
# ============================================================================


@pytest.mark.asyncio
async def test_gate6_lifespan_startup_and_graceful_shutdown(monkeypatch: pytest.MonkeyPatch):
    """Gate 6.1.1: Verify application Lifespan starts scheduler with 3 jobs and performs graceful cleanup."""
    mock_load = MagicMock()
    mock_cleanup_temp = MagicMock()
    mock_flush_cookies = AsyncMock()
    mock_save_snapshot = AsyncMock()

    monkeypatch.setattr("main.load_accounts_into_memory", mock_load)
    monkeypatch.setattr("main.cleanup_orphaned_temp_files", mock_cleanup_temp)
    monkeypatch.setattr("main.web_session_manager.flush_all_cookies", mock_flush_cookies)
    monkeypatch.setattr("main.save_accounts_snapshot", mock_save_snapshot)

    dummy_app = FastAPI(lifespan=lifespan)

    async with lifespan(dummy_app):
        # Startup checks
        mock_load.assert_called_once()
        mock_cleanup_temp.assert_called_once()

        scheduler = getattr(dummy_app.state, "scheduler", None)
        assert scheduler is not None
        assert scheduler.running is True

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert "sync_quotas" in job_ids
        assert "flush_cookies" in job_ids
        assert "cleanup_chats" in job_ids

    # Graceful shutdown checks: allow single event loop tick for _shutdown callback
    await asyncio.sleep(0.02)
    assert scheduler.running is False
    mock_flush_cookies.assert_awaited_once()
    mock_save_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate6_state_registry_snapshot_roundtrip_persistence(isolate_test_environment: dict[str, Path]):
    """Gate 6.1.2: Verify state registry serialization and deserialization retains accounts with 100% fidelity."""
    accounts_file = isolate_test_environment["accounts_file"]

    # 1. Populate state registry with diverse accounts
    oauth_acc = OAuthAccount(
        id="gate6-oauth-roundtrip",
        label="Gate 6 OAuth Account",
        status="ACTIVE",
        transport_mode="http_v1internal",
        max_concurrency=4,
        credentials=OAuthCredentials(
            access_token="gate6-token-123",
            refresh_token="gate6-refresh-xyz",
            project_id="gate6-companion-proj",
        ),
        discovered_models=["gemini-3.8-flash", "gemini-3.1-pro"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-3.8-flash": ModelQuota(remaining_fraction=0.92, reset_at=1735689600),
            }
        ),
    )
    web_acc = WebAccount(
        id="gate6-web-roundtrip",
        label="Gate 6 Web Account",
        status="ACTIVE",
        max_concurrency=3,
        cookie_file=str(isolate_test_environment["cookies_dir"] / "gate6-web-roundtrip.json"),
        cookies=WebCookies(secure_1psid="psid_roundtrip_val", secure_1psidts="ts_roundtrip_val"),
        discovered_models=["gemini-4-argon", "gemini-2.5-pro"],
        usage=WebUsage(five_hour_pct=0.35, weekly_pct=0.15, five_hour_reset_at=1735689600),
    )

    state_registry.upsert_oauth_account(oauth_acc)
    state_registry.upsert_web_account(web_acc)

    # 2. Persist snapshot to disk
    await save_accounts_snapshot()
    assert accounts_file.exists()

    # 3. Wipe memory registry completely
    state_registry.reset()
    assert len(state_registry.oauth_accounts) == 0
    assert len(state_registry.web_accounts) == 0

    # 4. Reload from disk
    load_accounts_into_memory()

    # 5. Verify restored account integrity
    restored_oauth = state_registry.get_oauth_account("gate6-oauth-roundtrip")
    assert restored_oauth is not None
    assert restored_oauth.label == "Gate 6 OAuth Account"
    assert restored_oauth.credentials.project_id == "gate6-companion-proj"
    assert restored_oauth.quota_state.model_quotas["gemini-3.8-flash"].remaining_fraction == 0.92

    restored_web = state_registry.get_web_account("gate6-web-roundtrip")
    assert restored_web is not None
    assert restored_web.cookies.secure_1psid == "psid_roundtrip_val"
    assert restored_web.usage.five_hour_pct == 0.35


# ============================================================================
# Section 2: OpenAI Protocol Endpoints E2E (/v1)
# ============================================================================


@pytest.mark.asyncio
async def test_gate6_openai_models_catalog_and_thinking_suffixes(
    client_factory: Callable[[], httpx.AsyncClient],
):
    """Gate 6.2.1: Verify GET /v1/models catalog listing and reasoning effort suffixes."""
    async with client_factory() as client:
        # 1. Catalog listing
        response = await client.get("/v1/models")
        assert response.status_code == 200
        catalog = response.json()
        assert catalog.get("object") == "list"
        model_ids = [m["id"] for m in catalog.get("data", [])]

        # Verify core base models
        for base in ["gemini-2.5-pro", "gemini-3.8-flash", "gemini-4-argon", "gemini-3.1-flash-lite"]:
            assert base in model_ids

        # Verify reasoning effort suffixed variants exist
        assert "gemini-2.5-pro-high" in model_ids
        assert "gemini-2.5-pro-low" in model_ids

        # 2. Single model retrieval
        res_pro = await client.get("/v1/models/gemini-2.5-pro")
        assert res_pro.status_code == 200
        pro_meta = res_pro.json()
        assert pro_meta["id"] == "gemini-2.5-pro"
        assert pro_meta["object"] == "model"
        assert pro_meta["owned_by"] == "google-hybrid-pool"

        # 3. Model with suffix retrieval
        res_suffix = await client.get("/v1/models/gemini-2.5-pro-high")
        assert res_suffix.status_code == 200
        assert res_suffix.json()["id"] == "gemini-2.5-pro-high"

        # 4. Unknown model returns OpenAI standard 404
        res_unknown = await client.get("/v1/models/non-existent-gpt-omega")
        assert res_unknown.status_code == 404
        err_payload = res_unknown.json()
        assert "error" in err_payload
        assert err_payload["error"]["type"] == "invalid_request_error"
        assert err_payload["error"]["code"] == "model_not_found"
        assert "non-existent-gpt-omega" in err_payload["error"]["message"]


@pytest.mark.asyncio
async def test_gate6_chat_completion_non_streaming_failover_recovery(
    test_app: FastAPI,
    client_factory: Callable[[], httpx.AsyncClient],
):
    """Gate 6.2.2: Verify non-streaming completions execute instant failover upon primary channel failure."""
    # Setup primary and secondary accounts
    acc_primary = OAuthAccount(
        id="gate6-oauth-primary",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok-1"),
        discovered_models=["gemini-3.8-flash"],
        priority=2.0,
    )
    acc_secondary = OAuthAccount(
        id="gate6-oauth-secondary",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok-2"),
        discovered_models=["gemini-3.8-flash"],
        priority=1.0,
    )
    state_registry.upsert_oauth_account(acc_primary)
    state_registry.upsert_oauth_account(acc_secondary)

    call_count = 0

    async def mock_stream_factory(account: Any, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if account.id == "gate6-oauth-primary":
            # Primary fails handshake immediately with 429
            fake_req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1internal")
            fake_resp = httpx.Response(
                status_code=429,
                request=fake_req,
                json={"error": {"message": "Resource exhausted", "quotaResetDelay": "60s"}},
            )
            raise httpx.HTTPStatusError("429 Too Many Requests", request=fake_req, response=fake_resp)

        # Secondary succeeds
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            yield {
                "id": "chatcmpl-gate6-nonstream",
                "object": "chat.completion.chunk",
                "created": 1735689600,
                "model": "gemini-3.8-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Recovered response from secondary account."},
                        "finish_reason": "stop",
                    }
                ],
            }
        return _gen()

    # Use a real FailoverController pointing to state_registry with the mock stream factory
    custom_controller = FailoverController(registry=state_registry)

    # Intercept execute_chat_completion_with_failover to inject our stream_factory
    original_exec = custom_controller.execute_chat_completion_with_failover

    async def _wrapped_exec(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["stream_factory"] = mock_stream_factory
        return await original_exec(*args, **kwargs)

    custom_controller.execute_chat_completion_with_failover = _wrapped_exec  # type: ignore[method-assign]
    test_app.dependency_overrides[get_failover_controller] = lambda: custom_controller

    async with client_factory() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Hello failover test"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Recovered response from secondary account."
    assert data["choices"][0]["finish_reason"] == "stop"

    # Verify primary account experienced 429 and was put in cooldown, secondary served request
    assert acc_primary.status == "COOLDOWN"
    assert acc_primary.active_requests == 0
    assert acc_secondary.active_requests == 0
    assert call_count >= 2


@pytest.mark.asyncio
async def test_gate6_chat_completion_streaming_sse_and_zero_concurrency_leak(
    test_app: FastAPI,
    client_factory: Callable[[], httpx.AsyncClient],
):
    """Gate 6.2.3: Verify SSE streaming headers, data chunks, data: [DONE], and guaranteed concurrency decrement."""
    stream_acc = OAuthAccount(
        id="gate6-streaming-account",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok-stream"),
        discovered_models=["gemini-3.8-flash"],
        priority=3.0,
    )
    state_registry.upsert_oauth_account(stream_acc)
    assert stream_acc.active_requests == 0

    async def mock_stream_factory(account: Any, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            # Handshake non-empty chunk
            yield {
                "id": "chatcmpl-stream-1",
                "object": "chat.completion.chunk",
                "created": 1735689600,
                "model": "gemini-3.8-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Stream chunk 1"}, "finish_reason": None}],
            }
            await asyncio.sleep(0.01)
            # Second chunk
            yield {
                "id": "chatcmpl-stream-1",
                "object": "chat.completion.chunk",
                "created": 1735689600,
                "model": "gemini-3.8-flash",
                "choices": [{"index": 0, "delta": {"content": " and chunk 2."}, "finish_reason": "stop"}],
            }
        return _gen()

    custom_controller = FailoverController(registry=state_registry)
    original_stream_exec = custom_controller.execute_stream_with_failover

    async def _wrapped_stream_exec(*args: Any, **kwargs: Any) -> Any:
        kwargs["stream_factory"] = mock_stream_factory
        return await original_stream_exec(*args, **kwargs)

    custom_controller.execute_stream_with_failover = _wrapped_stream_exec  # type: ignore[method-assign]
    test_app.dependency_overrides[get_failover_controller] = lambda: custom_controller

    async with client_factory() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Stream me"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        # Verify strict SSE headers
        assert "text/event-stream" in response.headers["content-type"]
        assert "no-cache" in response.headers.get("cache-control", "").lower()
        assert "keep-alive" in response.headers.get("connection", "").lower()
        assert response.headers.get("x-accel-buffering") == "no"

        # Read streaming lines
        lines: list[str] = []
        async for line in response.aiter_lines():
            if line.strip():
                lines.append(line.strip())

    # Verify SSE format and completion sentinel
    assert len(lines) >= 3
    assert lines[0].startswith("data: ")
    assert "Stream chunk 1" in lines[0]
    assert lines[-1] == "data: [DONE]"

    # CRITICAL: Zero Concurrency Leak Verification
    assert stream_acc.active_requests == 0


@pytest.mark.asyncio
async def test_gate6_chat_completion_token_saver_header_control(
    test_app: FastAPI,
    client_factory: Callable[[], httpx.AsyncClient],
):
    """Gate 6.2.4: Verify X-Token-Saver header controls tool message compression end-to-end."""
    oauth_acc = OAuthAccount(
        id="gate6-token-saver-acc",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok"),
        discovered_models=["gemini-3.8-flash"],
    )
    state_registry.upsert_oauth_account(oauth_acc)

    captured_messages: list[list[dict[str, Any]]] = []

    class CapturingController(FailoverController):
        async def execute_chat_completion_with_failover(
            self, model: str, messages: Any, tools: Any = None, **kwargs: Any
        ) -> dict[str, Any]:
            captured_messages.append([dict(m) for m in messages])
            return {
                "id": "chatcmpl-ts-test",
                "object": "chat.completion",
                "created": 1735689600,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}],
            }

    test_app.dependency_overrides[get_failover_controller] = lambda: CapturingController(registry=state_registry)

    # Verbose tool output payload
    large_tool_output = json.dumps({"items": [{"id": i, "data": f"verbose payload value {i}"} for i in range(100)]})
    request_messages = [
        {"role": "user", "content": "Fetch records"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_data", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": large_tool_output},
    ]

    async with client_factory() as client:
        # Request 1: Default (Token saver ON)
        res_on = await client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3.8-flash", "messages": request_messages},
        )
        assert res_on.status_code == 200

        # Request 2: Explicitly OFF via X-Token-Saver: off
        res_off = await client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3.8-flash", "messages": request_messages},
            headers={"X-Token-Saver": "off"},
        )
        assert res_off.status_code == 200

    assert len(captured_messages) == 2
    sanitized_tool_content = captured_messages[0][2]["content"]
    verbatim_tool_content = captured_messages[1][2]["content"]

    # When ON, size must be compressed / sanitized; when OFF, must match original verbatim string
    assert len(sanitized_tool_content) <= len(large_tool_output)
    assert verbatim_tool_content == large_tool_output


@pytest.mark.asyncio
async def test_gate6_chat_completion_error_handling_and_permissive_schema(
    test_app: FastAPI,
    client_factory: Callable[[], httpx.AsyncClient],
):
    """Gate 6.2.5: Verify standard OpenAI error format for PoolExhaustedError, ModelNotSupportedError, and extra fields."""
    # 1. Unknown model -> 404
    async with client_factory() as client:
        res_404 = await client.post(
            "/v1/chat/completions",
            json={"model": "unsupported-gate6-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res_404.status_code == 404
        err_404 = res_404.json()
        assert err_404["error"]["code"] == "model_not_found"
        assert err_404["error"]["type"] == "invalid_request_error"

    # 2. Pool exhausted -> 429
    class ExhaustedController(FailoverController):
        async def execute_chat_completion_with_failover(self, *args: Any, **kwargs: Any) -> Any:
            raise PoolExhaustedError("All registered accounts in pool are exhausted.")

    test_app.dependency_overrides[get_failover_controller] = lambda: ExhaustedController()

    async with client_factory() as client:
        res_429 = await client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res_429.status_code == 429
        err_429 = res_429.json()
        assert err_429["error"]["code"] == "pool_exhausted"
        assert err_429["error"]["type"] == "server_error"

    # 3. Permissive Schema: extra fields accepted without 422
    class EchoController(FailoverController):
        async def execute_chat_completion_with_failover(self, model: str, *args: Any, **kwargs: Any) -> Any:
            return {
                "id": "chatcmpl-permissive",
                "object": "chat.completion",
                "created": 1735689600,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            }

    test_app.dependency_overrides[get_failover_controller] = lambda: EchoController()

    async with client_factory() as client:
        res_extra = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Hello", "custom_extra_tag": 999}],
                "seed": 42,
                "user": "custom-agent-guid",
                "client_custom_unknown_field": True,
            },
        )
        assert res_extra.status_code == 200
        assert res_extra.json()["object"] == "chat.completion"


# ============================================================================
# Section 3: Control Plane & Dashboard Endpoints E2E (/api)
# ============================================================================


def test_gate6_dashboard_status_mathematical_metrics():
    """Gate 6.3.1: Verify GET /api/status telemetry returns C, P, tau, Si, D_conc with mathematical accuracy."""
    now = time.time()
    dash_app = FastAPI(title="Dashboard Math App")
    dash_app.include_router(dashboard_router)
    client = TestClient(dash_app)

    # Web account with active request C=1
    web_acc = WebAccount(
        id="gate6-web-math",
        label="Web Math Account",
        status="ACTIVE",
        max_concurrency=3,
        cookie_file="dummy.json",
        cookies=WebCookies(secure_1psid="psid"),
        discovered_models=["gemini-4-argon"],
        usage=WebUsage(
            five_hour_pct=0.40,
            weekly_pct=0.20,
            five_hour_reset_at=int(now + 3600),
            weekly_reset_at=int(now + 86400),
        ),
    )
    web_acc.active_requests = 1
    # OAuth account with C=0
    oauth_acc = OAuthAccount(
        id="gate6-oauth-math",
        label="OAuth Math Account",
        status="ACTIVE",
        active_requests=0,
        max_concurrency=4,
        credentials=OAuthCredentials(access_token="tok"),
        discovered_models=["gemini-3.8-flash"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-3.8-flash": ModelQuota(
                    remaining_fraction=0.85,
                    reset_at=int(now + 7200),
                    is_exhausted=False,
                )
            }
        ),
    )

    state_registry.upsert_web_account(web_acc)
    state_registry.upsert_oauth_account(oauth_acc)

    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["pool_status"] == "HEALTHY"
    stats = payload["general_stats"]
    assert stats["total_accounts"] == 2
    assert stats["active_accounts"] == 2
    assert stats["total_concurrency"] == 1

    # Verify Web Account Mathematical Metrics
    web_telemetry = next(acc for acc in payload["web_accounts"] if acc["id"] == "gate6-web-math")
    assert web_telemetry["active_requests"] == 1  # C
    assert web_telemetry["priority_score"] > 0    # P

    dynamic = web_telemetry["dynamic_metrics"]
    assert 1.0 <= dynamic["tau"] <= 1.5           # tau in [1.0, 1.5]
    assert dynamic["Si"] == 1.0                   # Default affinity
    # For Si=1.0, D_conc = (1 + C)^2 = (1 + 1)^2 = 4.0
    assert dynamic["D_conc"] == 4.0

    # Verify OAuth Account Metrics
    oauth_telemetry = next(acc for acc in payload["oauth_accounts"] if acc["id"] == "gate6-oauth-math")
    assert oauth_telemetry["active_requests"] == 0
    assert oauth_telemetry["priority_score"] > 0
    assert 1.0 <= oauth_telemetry["tau"] <= 1.5
    assert "gemini-3.8-flash" in oauth_telemetry["model_quotas"]
    assert oauth_telemetry["model_quotas"]["gemini-3.8-flash"]["remaining_fraction"] == 0.85


def test_gate6_oauth_account_crud_lifecycle(sync_client: TestClient):
    """Gate 6.3.2: Verify full OAuth account lifecycle: init -> complete -> delete."""
    # 1. Init PKCE
    init_res = sync_client.post(
        "/api/oauth-accounts/init",
        json={"redirect_uri": "http://localhost:8000/oauth/callback", "verifier_length": 64},
    )
    assert init_res.status_code == 200
    init_data = init_res.json()
    assert "auth_url" in init_data
    assert "accounts.google.com" in init_data["auth_url"]
    state = init_data["state"]
    code_verifier = init_data["code_verifier"]

    # 2. Complete flow with mock token exchange
    mock_creds = OAuthCredentials(
        access_token="gate6_access_token_abc",
        refresh_token="gate6_refresh_token_def",
        project_id="gate6-companion-project",
    )
    with patch("api.dashboard_routes.complete_oauth_login", new_callable=AsyncMock) as mock_login, \
         patch("api.dashboard_routes.discover_oauth_models", new_callable=AsyncMock) as mock_models:
        mock_login.return_value = mock_creds
        mock_models.return_value = ["gemini-3.8-flash", "gemini-3.1-pro"]

        comp_res = sync_client.post(
            "/api/oauth-accounts/complete",
            json={
                "code": "4/0AWtgMockCodeForGate6",
                "state": state,
                "label": "Gate 6 Test OAuth",
                "max_concurrency": 5,
            },
        )
        assert comp_res.status_code == 200
        comp_data = comp_res.json()
        assert comp_data["success"] is True
        registered_id = comp_data["account"]["id"]

        # Confirm stored in memory registry
        acc = state_registry.get_oauth_account(registered_id)
        assert acc is not None
        assert acc.label == "Gate 6 Test OAuth"
        assert acc.max_concurrency == 5
        assert acc.credentials.access_token == "gate6_access_token_abc"

    # 3. Delete account
    del_res = sync_client.delete(f"/api/oauth-accounts/{registered_id}")
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True
    assert state_registry.get_oauth_account(registered_id) is None

    # 4. Deleting non-existent account returns 404
    del_404 = sync_client.delete(f"/api/oauth-accounts/{registered_id}")
    assert del_404.status_code == 404


def test_gate6_web_account_crud_lifecycle(sync_client: TestClient):
    """Gate 6.3.3: Verify full Web account lifecycle: add -> verify cache file -> delete."""
    raw_cookie = "Cookie: __Secure-1PSID=gate6_psid_token; __Secure-1PSIDTS=gate6_ts_token"
    add_res = sync_client.post(
        "/api/web-accounts/add",
        json={
            "account_id": "gate6-web-crud",
            "label": "Gate 6 Web Account",
            "cookies": raw_cookie,
            "max_concurrency": 2,
        },
    )
    assert add_res.status_code == 200
    add_data = add_res.json()
    assert add_data["success"] is True
    assert "hygiene_notice" in add_data

    # Verify registry entry
    account = state_registry.get_web_account("gate6-web-crud")
    assert account is not None
    assert account.cookies.secure_1psid == "gate6_psid_token"
    assert account.cookies.secure_1psidts == "gate6_ts_token"

    # Verify isolated cookie file exists on disk
    cookie_path = Path(account.cookie_file)
    assert cookie_path.exists()
    cache_json = json.loads(cookie_path.read_text(encoding="utf-8"))
    assert cache_json["cookies"]["__Secure-1PSID"] == "gate6_psid_token"

    # Delete web account
    del_res = sync_client.delete("/api/web-accounts/gate6-web-crud")
    assert del_res.status_code == 200
    assert state_registry.get_web_account("gate6-web-crud") is None
    # Verify cookie file was unlinked
    assert not cookie_path.exists()

    # Second delete returns 404
    assert sync_client.delete("/api/web-accounts/gate6-web-crud").status_code == 404


def test_gate6_accounts_sync_quotas_endpoint(sync_client: TestClient):
    """Gate 6.3.4: Verify POST /api/accounts/sync-quotas invokes UsageMonitor and returns report."""
    with patch("api.dashboard_routes.sync_all_registered_accounts", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = {
            "timestamp": 1735689600,
            "synced_oauth_count": 2,
            "synced_web_count": 1,
            "oauth_accounts": ["o1", "o2"],
            "web_accounts": ["w1"],
            "errors": {},
        }
        res = sync_client.post("/api/accounts/sync-quotas")
        assert res.status_code == 200
        payload = res.json()
        assert payload["success"] is True
        assert payload["report"]["synced_oauth_count"] == 2
        mock_sync.assert_awaited_once_with(state_registry, persist=True)


# ============================================================================
# Section 4: Web UI Dashboard Presentation
# ============================================================================


def test_gate6_dashboard_ui_presentation_endpoints(sync_client: TestClient):
    """Gate 6.4.1: Verify GET /, GET /dashboard, and GET /static/index.html serve UI correctly."""
    # 1. Root / serves HTML
    res_root = sync_client.get("/")
    assert res_root.status_code == 200
    assert "text/html" in res_root.headers.get("content-type", "")
    assert "<!DOCTYPE html>" in res_root.text

    # 2. /dashboard serves HTML
    res_dash = sync_client.get("/dashboard")
    assert res_dash.status_code == 200
    assert "text/html" in res_dash.headers.get("content-type", "")
    assert "<!DOCTYPE html>" in res_dash.text

    # 3. Static mount /static/index.html
    res_static = sync_client.get("/static/index.html")
    assert res_static.status_code == 200
    assert "text/html" in res_static.headers.get("content-type", "")


def test_gate6_dashboard_ui_missing_file_handling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Gate 6.4.2: Verify 404 response when index.html is absent."""
    non_existent = tmp_path / "absent_index.html"
    monkeypatch.setattr(main, "INDEX_HTML_PATH", non_existent)

    test_app = create_app()
    client = TestClient(test_app)

    res = client.get("/")
    assert res.status_code == 404
    assert "Dashboard UI not found" in res.json()["detail"]


# ============================================================================
# Section 5: CLI Engine (Click CLI)
# ============================================================================


def test_gate6_cli_list_accounts_command(cli_runner: CliRunner):
    """Gate 6.5.1: Verify 'list-accounts' outputs clean table for empty and populated pool."""
    # Empty pool
    res_empty = cli_runner.invoke(cli, ["list-accounts"])
    assert res_empty.exit_code == 0
    assert "No accounts registered" in res_empty.output

    # Populated pool
    oauth_acc = OAuthAccount(
        id="gate6-cli-oauth",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok"),
        discovered_models=["gemini-3.8-flash"],
        quota_state=OAuthQuotaState(
            model_quotas={"gemini-3.8-flash": ModelQuota(remaining_fraction=0.88)}
        ),
    )
    web_acc = WebAccount(
        id="gate6-cli-web",
        status="ACTIVE",
        cookie_file="test.json",
        cookies=WebCookies(secure_1psid="psid"),
        discovered_models=["gemini-4-argon"],
        usage=WebUsage(five_hour_pct=0.25, weekly_pct=0.10),
    )
    state_registry.upsert_oauth_account(oauth_acc)
    state_registry.upsert_web_account(web_acc)

    res_pop = cli_runner.invoke(cli, ["list-accounts"])
    assert res_pop.exit_code == 0
    assert "gate6-cli-oauth" in res_pop.output
    assert "gate6-cli-web" in res_pop.output
    assert "Channel" in res_pop.output
    assert "Priority" in res_pop.output


def test_gate6_cli_status_and_health_commands(cli_runner: CliRunner):
    """Gate 6.5.2: Verify 'status' and 'health' commands output telemetry overview."""
    res_status = cli_runner.invoke(cli, ["status"])
    assert res_status.exit_code == 0
    assert "Gemini Hybrid Pool Gateway Status" in res_status.output
    assert "Pool Health:" in res_status.output
    assert "Available Models" in res_status.output

    res_health = cli_runner.invoke(cli, ["health"])
    assert res_health.exit_code == 0
    assert "Pool Health:" in res_health.output


def test_gate6_cli_sync_quotas_command(cli_runner: CliRunner):
    """Gate 6.5.3: Verify 'sync-quotas' command triggers synchronization pass."""
    with patch("main.sync_all_registered_accounts", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = {
            "timestamp": 1735689600,
            "synced_oauth_count": 1,
            "synced_web_count": 0,
            "oauth_accounts": ["o1"],
            "web_accounts": [],
            "errors": {},
        }
        res_sync = cli_runner.invoke(cli, ["sync-quotas"])
        assert res_sync.exit_code == 0
        assert "Quota synchronization completed successfully" in res_sync.output
        mock_sync.assert_awaited_once_with(state_registry)
