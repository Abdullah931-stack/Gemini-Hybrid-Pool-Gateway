"""Unit tests for core/model_registry.py.

Verifies:
1. Model normalization, reasoning effort suffix splitting, and effort coercion.
2. Routine task effort downgrading, OAuth thinkingConfig generation, and injection.
3. OAuth CLI model discovery via fetchAvailableModels and automatic 403 fallback to retrieveUserQuota.
4. Web dynamic model discovery and model target resolution (sync and async).
5. UnifiedModelRegistry aggregation, OpenAI /v1/models format, and supporting account lookup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from config.settings import (
    AccountsConfig,
    AccountsStateRegistry,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
)
from core.model_registry import (
    FETCH_AVAILABLE_MODELS_URL,
    RETRIEVE_USER_QUOTA_URL,
    ResolvedModelSpec,
    UnifiedModelRegistry,
    build_oauth_thinking_config,
    discover_oauth_models,
    discover_web_models,
    discover_web_models_sync,
    get_supporting_account_objects,
    get_supporting_accounts,
    inject_oauth_thinking_config,
    normalize_model_id,
    parse_model_and_effort,
    resolve_web_model,
    resolve_web_model_sync,
    split_model_effort_suffix,
)


# ============================================================================
# 1. Normalization & Reasoning Suffix Tests
# ============================================================================


def test_normalize_model_id() -> None:
    assert normalize_model_id("models/gemini-2.5-pro") == "gemini-2.5-pro"
    assert (
        normalize_model_id("publishers/google/models/gemini-2.5-flash")
        == "gemini-2.5-flash"
    )
    assert (
        normalize_model_id("projects/p1/locations/global/models/gemini-4-argon")
        == "gemini-4-argon"
    )
    assert normalize_model_id("  GEMINI-3.1-PRO  ") == "gemini-3.1-pro"


def test_split_model_effort_suffix() -> None:
    base, effort = split_model_effort_suffix("gemini-2.5-pro-high")
    assert base == "gemini-2.5-pro"
    assert effort == "high"

    base, effort = split_model_effort_suffix("models/gemini-2.5-flash-minimal")
    assert base == "gemini-2.5-flash"
    assert effort == "minimal"

    base, effort = split_model_effort_suffix("gemini-3.1-pro-low")
    assert base == "gemini-3.1-pro"
    assert effort == "low"

    base, effort = split_model_effort_suffix("gemini-4-argon-medium")
    assert base == "gemini-4-argon"
    assert effort == "medium"

    base, effort = split_model_effort_suffix("gemini-2.5-pro")
    assert base == "gemini-2.5-pro"
    assert effort is None

    base, effort = split_model_effort_suffix("gemini-2.5-pro-unknown")
    assert base == "gemini-2.5-pro-unknown"
    assert effort is None


# ============================================================================
# 2. Reasoning Effort & ThinkingConfig Translation Tests
# ============================================================================


def test_build_oauth_thinking_config() -> None:
    cfg_minimal = build_oauth_thinking_config("minimal")
    assert cfg_minimal == {
        "includeThoughts": False,
        "thinkingBudget": 1024,
        "thinkingLevel": "MINIMAL",
    }

    cfg_low = build_oauth_thinking_config("low")
    assert cfg_low == {
        "includeThoughts": True,
        "thinkingBudget": 4096,
        "thinkingLevel": "LOW",
    }

    cfg_medium = build_oauth_thinking_config("medium")
    assert cfg_medium == {
        "includeThoughts": True,
        "thinkingBudget": 16384,
        "thinkingLevel": "MEDIUM",
    }

    cfg_high = build_oauth_thinking_config("high")
    assert cfg_high == {
        "includeThoughts": True,
        "thinkingBudget": 32768,
        "thinkingLevel": "HIGH",
    }

    # Fallback to low on invalid effort string
    cfg_invalid = build_oauth_thinking_config("extreme")
    assert cfg_invalid["thinkingLevel"] == "LOW"


def test_inject_oauth_thinking_config() -> None:
    base_gen = {"temperature": 0.7, "maxOutputTokens": 2048}
    injected = inject_oauth_thinking_config(base_gen, "high")
    assert injected["temperature"] == 0.7
    assert injected["thinkingConfig"]["thinkingBudget"] == 32768
    # Original should be untouched (immutability check)
    assert "thinkingConfig" not in base_gen

    injected_none = inject_oauth_thinking_config(None, "minimal")
    assert injected_none["thinkingConfig"]["includeThoughts"] is False


def test_parse_model_and_effort_routine_downgrade() -> None:
    # Explicit effort parameter overrides suffix
    spec = parse_model_and_effort("gemini-2.5-pro-low", reasoning_effort="high")
    assert spec.base_model == "gemini-2.5-pro"
    assert spec.reasoning_effort == "high"
    assert spec.oauth_thinking_config["thinkingBudget"] == 32768
    assert spec.web_extended_thinking is True

    # Suffix takes precedence when parameter is omitted
    spec_suffix = parse_model_and_effort("gemini-2.5-pro-high")
    assert spec_suffix.reasoning_effort == "high"

    # Routine task downgrades to 'low' when no effort specified
    spec_routine = parse_model_and_effort(
        "gemini-2.5-flash",
        is_routine_task=True,
        default_effort="medium",
    )
    assert spec_routine.reasoning_effort == "low"
    assert spec_routine.oauth_thinking_config["thinkingLevel"] == "LOW"

    # Minimal disables web_extended_thinking
    spec_minimal = parse_model_and_effort("gemini-2.5-flash-minimal")
    assert spec_minimal.reasoning_effort == "minimal"
    assert spec_minimal.web_extended_thinking is False


# ============================================================================
# 3. OAuth CLI Discovery & Automatic 403 Fallback Tests
# ============================================================================


@pytest.mark.asyncio
async def test_discover_oauth_models_success() -> None:
    account = OAuthAccount(
        id="oauth-test-1",
        label="OAuth Primary",
        credentials=OAuthCredentials(
            access_token="test-access-token",
            project_id="test-proj-123",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FETCH_AVAILABLE_MODELS_URL
        assert request.headers["Authorization"] == "Bearer test-access-token"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "quotaInfo": {
                            "remainingFraction": 0.95,
                            "resetTime": "2026-10-17T04:00:00Z",
                        },
                    },
                    {
                        "name": "models/gemini-2.5-flash",
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        models = await discover_oauth_models(account, http_client=mock_client)

    assert "gemini-2.5-pro" in models
    assert "gemini-2.5-flash" in models
    assert account.discovered_models == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert account.quota_state.model_quotas["gemini-2.5-pro"].remaining_fraction == 0.95
    assert account.quota_state.model_quotas["gemini-2.5-flash"].remaining_fraction == 1.0


@pytest.mark.asyncio
async def test_discover_oauth_models_fallback_on_403() -> None:
    account = OAuthAccount(
        id="oauth-test-fallback",
        label="OAuth Fallback Pro",
        credentials=OAuthCredentials(
            access_token="pro-token",
            project_id="pro-proj-999",
        ),
    )

    called_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        called_urls.append(url_str)
        if url_str == FETCH_AVAILABLE_MODELS_URL:
            # Standard Pro account receiving 403 Forbidden
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "The caller does not have permission",
                        "status": "PERMISSION_DENIED",
                    }
                },
            )
        if url_str == RETRIEVE_USER_QUOTA_URL:
            return httpx.Response(
                200,
                json={
                    "buckets": [
                        {
                            "modelId": "gemini-3.1-pro",
                            "remainingFraction": 0.70,
                            "resetTime": "2026-10-17T06:00:00Z",
                        },
                        {
                            "modelId": "gemini-4-argon",
                            "remainingFraction": 0.0,
                            "isExhausted": True,
                        },
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        models = await discover_oauth_models(account, http_client=mock_client)

    assert called_urls == [FETCH_AVAILABLE_MODELS_URL, RETRIEVE_USER_QUOTA_URL]
    assert "gemini-3.1-pro" in models
    assert "gemini-4-argon" in models
    assert account.discovered_models == ["gemini-3.1-pro", "gemini-4-argon"]
    assert account.quota_state.model_quotas["gemini-3.1-pro"].remaining_fraction == 0.70
    assert account.quota_state.model_quotas["gemini-3.1-pro"].is_exhausted is False
    assert account.quota_state.model_quotas["gemini-4-argon"].remaining_fraction == 0.0
    assert account.quota_state.model_quotas["gemini-4-argon"].is_exhausted is True


# ============================================================================
# 4. Web Dynamic Model Discovery & Resolution Tests
# ============================================================================


class MockWebClient:
    def __init__(self, async_mode: bool = True) -> None:
        self.async_mode = async_mode

    def list_models(self) -> Any:
        models_data = [
            {"model_name": "gemini-2.5-pro", "is_available": True},
            {"model_name": "gemini-2.5-flash", "is_available": True},
        ]
        if self.async_mode:
            async def _coro() -> list[dict[str, Any]]:
                return models_data
            return _coro()
        return models_data

    def resolve_model(self, name: str) -> Any:
        return f"resolved_header_for_{name}"


@pytest.mark.asyncio
async def test_discover_web_models_async_and_sync_client() -> None:
    account = WebAccount(
        id="web-test-1",
        label="Web Account 1",
        cookies=WebCookies(secure_1psid="sid", secure_1psidts="ts"),
    )

    # Async list_models
    async_client = MockWebClient(async_mode=True)
    models = await discover_web_models(account, async_client)
    assert models == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert account.discovered_models == ["gemini-2.5-pro", "gemini-2.5-flash"]

    # Sync list_models
    sync_client = MockWebClient(async_mode=False)
    account.discovered_models = []
    models_sync = await discover_web_models(account, sync_client)
    assert models_sync == ["gemini-2.5-pro", "gemini-2.5-flash"]

    # Synchronous function call
    account.discovered_models = []
    sync_run_models = discover_web_models_sync(account, sync_client)
    assert sync_run_models == ["gemini-2.5-pro", "gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_resolve_web_model() -> None:
    client = MockWebClient()
    resolved = await resolve_web_model(client, "gemini-2.5-pro-high")
    assert resolved == "resolved_header_for_gemini-2.5-pro"

    # Sync resolver
    sync_resolved = resolve_web_model_sync(client, "gemini-2.5-flash-low")
    assert sync_resolved == "resolved_header_for_gemini-2.5-flash"


# ============================================================================
# 5. UnifiedModelRegistry & Account Lookup Tests
# ============================================================================


def test_unified_model_registry_aggregation() -> None:
    reg = AccountsStateRegistry()
    oauth_acc = OAuthAccount(
        id="oauth-active",
        label="OAuth Active",
        status="ACTIVE",
        discovered_models=["gemini-custom-pro"],
    )
    oauth_suspended = OAuthAccount(
        id="oauth-suspended",
        label="OAuth Suspended",
        status="SUSPENDED",
        discovered_models=["gemini-banned-model"],
    )
    web_acc = WebAccount(
        id="web-active",
        label="Web Active",
        status="ACTIVE",
        discovered_models=["gemini-web-exclusive"],
    )
    reg.config = AccountsConfig(
        oauth_accounts=[oauth_acc, oauth_suspended],
        web_accounts=[web_acc],
    )

    model_registry = UnifiedModelRegistry(reg)
    base_models = model_registry.get_base_models()

    assert "gemini-custom-pro" in base_models
    assert "gemini-web-exclusive" in base_models
    # Pure discovered models: defaults are NOT included by default
    assert "gemini-2.5-pro" not in base_models
    assert "gemini-4-argon" not in base_models

    # When include_defaults=True, defaults are included
    base_models_with_defaults = model_registry.get_base_models(include_defaults=True)
    assert "gemini-2.5-pro" in base_models_with_defaults
    assert "gemini-4-argon" in base_models_with_defaults

    all_ids = model_registry.get_all_model_ids()
    assert "gemini-custom-pro-minimal" in all_ids
    assert "gemini-custom-pro-low" in all_ids
    assert "gemini-custom-pro-medium" in all_ids
    assert "gemini-custom-pro-high" in all_ids

    openai_response = model_registry.list_openai_models()
    assert openai_response["object"] == "list"
    ids = [entry["id"] for entry in openai_response["data"]]
    assert "gemini-custom-pro-high" in ids


def test_get_supporting_accounts() -> None:
    reg = AccountsStateRegistry()
    oauth_acc = OAuthAccount(
        id="oauth-1",
        label="OAuth 1",
        status="ACTIVE",
        discovered_models=["gemini-2.5-pro"],
    )
    oauth_cooldown = OAuthAccount(
        id="oauth-cd",
        label="OAuth Cooldown",
        status="COOLDOWN",
        discovered_models=["gemini-2.5-pro"],
    )
    web_acc = WebAccount(
        id="web-1",
        label="Web 1",
        status="ACTIVE",
        discovered_models=["gemini-2.5-pro", "gemini-2.5-flash"],
    )
    reg.config = AccountsConfig(
        oauth_accounts=[oauth_acc, oauth_cooldown],
        web_accounts=[web_acc],
    )

    model_registry = UnifiedModelRegistry(reg)

    # By default, active_only=False includes ACTIVE and COOLDOWN (excludes SUSPENDED)
    supporting = model_registry.get_supporting_accounts("gemini-2.5-pro")
    assert isinstance(supporting, dict)
    assert supporting["oauth_accounts"] == ["oauth-1", "oauth-cd"]
    assert supporting["web_accounts"] == ["web-1"]
    assert supporting["oauth"] == ["oauth-1", "oauth-cd"]
    assert supporting["web"] == ["web-1"]

    # When active_only=True
    active_supporting = model_registry.get_supporting_accounts(
        "gemini-2.5-pro", active_only=True
    )
    assert active_supporting["oauth_accounts"] == ["oauth-1"]
    assert active_supporting["web_accounts"] == ["web-1"]

    # Suffix in model name is stripped before matching
    supporting_suffix = model_registry.get_supporting_accounts("gemini-2.5-pro-high")
    assert "oauth-1" in supporting_suffix["oauth_accounts"]

    # Object query helper
    supporting_objs = get_supporting_account_objects(
        "gemini-2.5-pro", registry=reg, active_only=True
    )
    assert supporting_objs["oauth_accounts"][0].id == "oauth-1"
    assert supporting_objs["web_accounts"][0].id == "web-1"
