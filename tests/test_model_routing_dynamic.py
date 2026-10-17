"""Tests for Dynamic Model Routing & Compute Allocation (Unit & Integration).

Verifies Level 4/5 dynamic model routing functionality:
1. Persistence of custom model routes in config/model_routes.json.
2. ModelRouteConfig schema, aliases, properties, and serialization.
3. get_model_channel_preference matching:
   - Default fallbacks (argon -> web only, pro -> web then oauth, flash -> oauth then web).
   - Exact custom route overrides.
   - Longest prefix / reasoning-effort suffix stripping.
   - Custom alias routes (main_agent, sub_agent, gpt-4o).
   - Exclusive primary channels (failover=None).
4. select_optimal_account integration with custom routes.
5. Dashboard API endpoints:
   - GET /api/model-routes
   - POST /api/model-routes
   - DELETE /api/model-routes/{model}
   - Fallback behavior after deletion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.dashboard_routes import router as dashboard_router
from config.settings import (
    DEFAULT_MODEL_ROUTES,
    ModelRouteConfig,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    ModelQuota,
    WebAccount,
    WebCookies,
    async_save_custom_model_routes,
    get_custom_model_routes,
    save_custom_model_routes,
    state_registry,
)
from core.hybrid_scheduler import (
    get_model_channel_preference,
    select_optimal_account,
)


@pytest.fixture(autouse=True)
def isolate_model_routes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure all tests operate on an isolated temporary model_routes.json and clean state."""
    state_registry.reset()
    test_routes_file = tmp_path / "model_routes.json"
    test_accounts_file = tmp_path / "accounts.json"
    test_cookies_dir = tmp_path / "cookies_cache"
    test_cookies_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("config.settings.MODEL_ROUTES_FILE_PATH", test_routes_file)
    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", test_accounts_file)
    monkeypatch.setattr("config.settings.COOKIES_CACHE_DIR", test_cookies_dir)
    monkeypatch.setattr("core.hybrid_scheduler.get_custom_model_routes", lambda *args, **kwargs: get_custom_model_routes(test_routes_file))

    yield test_routes_file

    state_registry.reset()
    if test_routes_file.exists():
        try:
            test_routes_file.unlink()
        except OSError:
            pass


@pytest.fixture
def test_app() -> FastAPI:
    """Create FastAPI test application."""
    app = FastAPI(title="Dynamic Model Routing Gateway Test")
    app.include_router(dashboard_router)
    return app


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    """Test client for FastAPI test app."""
    return TestClient(test_app)


# ============================================================================
# 1. ModelRouteConfig & Storage Unit Tests
# ============================================================================


def test_model_route_config_instantiation_and_aliases() -> None:
    """Verify ModelRouteConfig handles field names and aliases seamlessly."""
    # Standard field names
    cfg1 = ModelRouteConfig(primary="web", failover="oauth")
    assert cfg1.primary == "web"
    assert cfg1.failover == "oauth"
    assert cfg1.primary_channel == "web"
    assert cfg1.failover_channel == "oauth"
    assert cfg1.to_dict()["primary_channel"] == "web"

    # Alias field names
    cfg2 = ModelRouteConfig(primary_channel="oauth", failover_channel=None)
    assert cfg2.primary == "oauth"
    assert cfg2.failover is None
    assert cfg2.primary_channel == "oauth"
    assert cfg2.failover_channel is None

    # Exclusive primary (no failover)
    cfg3 = ModelRouteConfig(primary="web")
    assert cfg3.primary == "web"
    assert cfg3.failover is None


def test_get_custom_model_routes_nonexistent_file(tmp_path: Path) -> None:
    """Reading from nonexistent or empty file returns empty dict."""
    missing = tmp_path / "missing.json"
    assert get_custom_model_routes(missing) == {}

    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert get_custom_model_routes(empty) == {}


def test_save_and_get_custom_model_routes_roundtrip(isolate_model_routes_file: Path) -> None:
    """Verify custom routes persist atomically and reload accurately."""
    routes_to_save = {
        "gemini-3.1-pro": {"primary": "oauth", "failover": "web"},
        "gemini-4-argon": ModelRouteConfig(primary="web", failover=None),
        "MAIN_AGENT": {"primary_channel": "web", "failover_channel": "oauth"},
    }

    save_custom_model_routes(routes_to_save, isolate_model_routes_file)
    assert isolate_model_routes_file.exists()

    loaded = get_custom_model_routes(isolate_model_routes_file)
    assert "gemini-3.1-pro" in loaded
    assert loaded["gemini-3.1-pro"]["primary"] == "oauth"
    assert loaded["gemini-3.1-pro"]["failover"] == "web"

    assert "gemini-4-argon" in loaded
    assert loaded["gemini-4-argon"]["primary"] == "web"
    assert loaded["gemini-4-argon"]["failover"] is None

    # Keys are normalized to lowercase
    assert "main_agent" in loaded
    assert loaded["main_agent"]["primary"] == "web"
    assert loaded["main_agent"]["failover"] == "oauth"


@pytest.mark.asyncio
async def test_async_save_custom_model_routes(isolate_model_routes_file: Path) -> None:
    """Verify async_save_custom_model_routes atomic write with asyncio lock."""
    routes = {
        "sub_agent": {"primary": "oauth", "failover": "web"}
    }
    await async_save_custom_model_routes(routes, isolate_model_routes_file)
    loaded = get_custom_model_routes(isolate_model_routes_file)
    assert loaded["sub_agent"]["primary"] == "oauth"


# ============================================================================
# 2. get_model_channel_preference Routing Engine Tests
# ============================================================================


def test_get_model_channel_preference_defaults() -> None:
    """Verify default channel preferences when no custom route override exists."""
    assert get_model_channel_preference("gemini-4-argon") == ("web", None)
    assert get_model_channel_preference("gemini-4-argon-high") == ("web", None)
    assert get_model_channel_preference("gemini-3.1-pro") == ("web", "oauth")
    assert get_model_channel_preference("gemini-2.5-pro-low") == ("web", "oauth")
    assert get_model_channel_preference("gemini-3.8-flash") == ("oauth", "web")
    assert get_model_channel_preference("gemini-3.1-flash-lite") == ("oauth", "web")
    assert get_model_channel_preference("unknown-model-xyz") == ("oauth", "web")


def test_get_model_channel_preference_custom_exact_override(isolate_model_routes_file: Path) -> None:
    """Custom route takes precedence over default routing."""
    # Switch gemini-3.1-pro from default (web, oauth) to (oauth, web)
    save_custom_model_routes(
        {"gemini-3.1-pro": {"primary": "oauth", "failover": "web"}},
        isolate_model_routes_file,
    )

    assert get_model_channel_preference("gemini-3.1-pro") == ("oauth", "web")
    # Argon remains default
    assert get_model_channel_preference("gemini-4-argon") == ("web", None)


def test_get_model_channel_preference_strips_reasoning_effort(isolate_model_routes_file: Path) -> None:
    """Custom route for base model applies to model with reasoning effort suffix."""
    save_custom_model_routes(
        {"gemini-3.1-pro": {"primary": "oauth", "failover": None}},
        isolate_model_routes_file,
    )

    assert get_model_channel_preference("gemini-3.1-pro-high") == ("oauth", None)
    assert get_model_channel_preference("gemini-3.1-pro-low") == ("oauth", None)
    assert get_model_channel_preference("gemini-3.1-pro-minimal") == ("oauth", None)


def test_get_model_channel_preference_custom_aliases(isolate_model_routes_file: Path) -> None:
    """Custom aliases (e.g. main_agent, sub_agent, gpt-4o) route accurately."""
    save_custom_model_routes(
        {
            "main_agent": {"primary": "web", "failover": "oauth"},
            "sub_agent": {"primary": "oauth", "failover": "web"},
            "gpt-4o": {"primary": "oauth", "failover": None},
        },
        isolate_model_routes_file,
    )

    assert get_model_channel_preference("main_agent") == ("web", "oauth")
    assert get_model_channel_preference("main_agent-medium") == ("web", "oauth")
    assert get_model_channel_preference("sub_agent") == ("oauth", "web")
    assert get_model_channel_preference("gpt-4o") == ("oauth", None)


def test_select_optimal_account_honors_custom_route(isolate_model_routes_file: Path) -> None:
    """select_optimal_account uses custom channel preference for routing."""
    # Create an OAuth account and a Web account
    oauth_acc = OAuthAccount(
        id="oauth-test",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok", project_id="p1"),
        discovered_models=["gemini-3.1-pro"],
        quota_state=OAuthQuotaState(
            model_quotas={"gemini-3.1-pro": ModelQuota(remaining_fraction=1.0)}
        ),
    )
    web_acc = WebAccount(
        id="web-test",
        status="ACTIVE",
        discovered_models=["gemini-3.1-pro"],
    )
    state_registry.upsert_oauth_account(oauth_acc)
    state_registry.upsert_web_account(web_acc)

    # By default, gemini-3.1-pro prefers Web primary
    acc, channel = select_optimal_account("gemini-3.1-pro", registry=state_registry)
    assert channel == "web"
    assert acc.id == "web-test"

    # Now override gemini-3.1-pro to prefer OAuth primary
    save_custom_model_routes(
        {"gemini-3.1-pro": {"primary": "oauth", "failover": "web"}},
        isolate_model_routes_file,
    )

    acc2, channel2 = select_optimal_account("gemini-3.1-pro", registry=state_registry)
    assert channel2 == "oauth"
    assert acc2.id == "oauth-test"


# ============================================================================
# 3. Dashboard API Endpoints Integration Tests
# ============================================================================


def test_api_get_model_routes_empty_custom(client: TestClient) -> None:
    """GET /api/model-routes returns discovered models and system defaults."""
    response = client.get("/api/model-routes")
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert isinstance(data["routes"], list)
    assert len(data["routes"]) > 0

    # Check that standard models exist in routes
    model_names = [r["model"] for r in data["routes"]]
    assert "gemini-4-argon" in model_names
    assert "gemini-3.1-pro" in model_names
    assert "gemini-3.8-flash" in model_names

    # Check argon default
    argon_route = next(r for r in data["routes"] if r["model"] == "gemini-4-argon")
    assert argon_route["primary_channel"] == "web"
    assert argon_route["failover_channel"] is None
    assert argon_route["is_custom"] is False


def test_api_post_model_routes_update_and_persist(client: TestClient, isolate_model_routes_file: Path) -> None:
    """POST /api/model-routes updates a route and persists to model_routes.json."""
    payload = {
        "model": "gemini-3.1-pro",
        "primary_channel": "oauth",
        "failover_channel": "web",
    }
    response = client.post("/api/model-routes", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert data["route"]["model"] == "gemini-3.1-pro"
    assert data["route"]["primary_channel"] == "oauth"
    assert data["route"]["failover_channel"] == "web"
    assert data["route"]["is_custom"] is True

    # Check persistence on disk
    assert isolate_model_routes_file.exists()
    disk_routes = json.loads(isolate_model_routes_file.read_text(encoding="utf-8"))
    assert disk_routes["gemini-3.1-pro"]["primary"] == "oauth"
    assert disk_routes["gemini-3.1-pro"]["failover"] == "web"

    # GET /api/model-routes should now flag it as is_custom=True
    get_resp = client.get("/api/model-routes")
    pro_route = next(r for r in get_resp.json()["routes"] if r["model"] == "gemini-3.1-pro")
    assert pro_route["is_custom"] is True
    assert pro_route["primary_channel"] == "oauth"
    assert pro_route["failover_channel"] == "web"


def test_api_post_model_routes_add_custom_alias(client: TestClient) -> None:
    """POST /api/model-routes allows adding a custom model alias like main_agent."""
    payload = {
        "model": "main_agent",
        "primary": "web",
        "failover": "none",
    }
    response = client.post("/api/model-routes", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["route"]["model"] == "main_agent"
    assert data["route"]["primary_channel"] == "web"
    assert data["route"]["failover_channel"] is None

    # Verify alias appears in GET /api/model-routes
    get_resp = client.get("/api/model-routes")
    aliases = [r["model"] for r in get_resp.json()["routes"]]
    assert "main_agent" in aliases


def test_api_post_model_routes_validation_error(client: TestClient) -> None:
    """POST /api/model-routes rejects empty model name."""
    response = client.post("/api/model-routes", json={"model": "   ", "primary": "web"})
    assert response.status_code == 400


def test_api_delete_model_route_and_fallback(client: TestClient, isolate_model_routes_file: Path) -> None:
    """DELETE /api/model-routes/{model} removes override and reverts to system default."""
    # 1. Override gemini-3.8-flash to web primary
    client.post(
        "/api/model-routes",
        json={"model": "gemini-3.8-flash", "primary": "web", "failover": "oauth"},
    )
    assert get_model_channel_preference("gemini-3.8-flash") == ("web", "oauth")

    # 2. Delete override
    del_resp = client.delete("/api/model-routes/gemini-3.8-flash")
    assert del_resp.status_code == 200
    del_data = del_resp.json()
    assert del_data["success"] is True
    assert del_data["effective_primary_channel"] == "oauth"
    assert del_data["effective_failover_channel"] == "web"

    # 3. Preference reverts to default (oauth, web)
    assert get_model_channel_preference("gemini-3.8-flash") == ("oauth", "web")


def test_api_delete_model_route_not_found(client: TestClient) -> None:
    """DELETE /api/model-routes/{model} returns 404 when route does not exist."""
    response = client.delete("/api/model-routes/nonexistent-model-xyz")
    assert response.status_code == 404
