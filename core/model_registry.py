"""Dynamic model discovery, reasoning effort translation, and unified model registry.

Implements Level 2 (Discovery & Effort Plane) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. OAuth CLI Model Discovery & Automatic Fallback on 403 Forbidden:
   Queries ``v1internal:fetchAvailableModels`` and seamlessly falls back to
   ``v1internal:retrieveUserQuota`` for standard Pro accounts, updating both
   ``account.discovered_models`` and ``account.quota_state.model_quotas``.
2. Web Pool Dynamic Model Discovery & Model Target Resolution:
   Discovers available models via ``client.list_models()`` and resolves canonical
   model headers via ``client.resolve_model(base_model)``.
3. Reasoning Effort & Suffix Translation:
   Normalizes ``-minimal``, ``-low``, ``-medium``, and ``-high`` suffixes or explicit
   ``reasoning_effort`` arguments into ``generationConfig.thinkingConfig`` budgets
   and Web ``extended_thinking`` flags, defaulting routine tasks to ``low``.
4. Unified Model Registry:
   Aggregates baseline, discovered OAuth/Web models, and reasoning effort variants
   into an OpenAI-compatible ``GET /v1/models`` response and resolves supporting accounts.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import inspect
import logging
import re
from typing import Any, Literal

from gemini_webapi import GeminiClient
import httpx
from pydantic import BaseModel, ConfigDict

from config.settings import (
    DELTA_BASE,
    AccountsStateRegistry,
    ModelQuota,
    OAuthAccount,
    WebAccount,
    state_registry,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Production Endpoints & Reasoning Effort Constants
# ============================================================================

FETCH_AVAILABLE_MODELS_URL: str = (
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
)
RETRIEVE_USER_QUOTA_URL: str = (
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
)

ReasoningEffort = Literal["minimal", "low", "medium", "high"]

REASONING_EFFORT_LEVELS: tuple[ReasoningEffort, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
)

EFFORT_SUFFIXES: tuple[str, ...] = ("-minimal", "-low", "-medium", "-high")

_THINKING_CONFIG_BY_EFFORT: dict[ReasoningEffort, dict[str, Any]] = {
    "minimal": {
        "includeThoughts": False,
        "thinkingBudget": 1024,
        "thinkingLevel": "MINIMAL",
    },
    "low": {
        "includeThoughts": True,
        "thinkingBudget": 4096,
        "thinkingLevel": "LOW",
    },
    "medium": {
        "includeThoughts": True,
        "thinkingBudget": 16384,
        "thinkingLevel": "MEDIUM",
    },
    "high": {
        "includeThoughts": True,
        "thinkingBudget": 32768,
        "thinkingLevel": "HIGH",
    },
}

DEFAULT_STANDARD_MODELS: tuple[str, ...] = tuple(DELTA_BASE.keys()) + (
    "gemini-2.5-pro",
    "gemini-2.5-flash",
)

_MODEL_PREFIX_RE: re.Pattern[str] = re.compile(
    r"^(?:projects/[^/]+/locations/[^/]+/)?(?:models/|publishers/google/models/)?",
    flags=re.IGNORECASE,
)


class ResolvedModelSpec(BaseModel):
    """Immutable specification of a parsed model identifier and reasoning effort."""

    model_config = ConfigDict(frozen=True)

    requested_model: str
    base_model: str
    reasoning_effort: ReasoningEffort
    oauth_thinking_config: dict[str, Any]
    web_extended_thinking: bool
    web_model_target: str

    @property
    def effort(self) -> ReasoningEffort:
        """Alias for ``reasoning_effort``."""
        return self.reasoning_effort


# ============================================================================
# 2. Model Name Normalization & Reasoning Effort Translation
# ============================================================================


def normalize_model_id(raw_name: str) -> str:
    """Strip resource path prefixes and normalize a Gemini model name."""
    cleaned = _MODEL_PREFIX_RE.sub("", raw_name.strip())
    return cleaned.strip().lower()


def split_model_effort_suffix(
    model_name: str,
) -> tuple[str, ReasoningEffort | None]:
    """Separate a trailing reasoning effort suffix from ``model_name`` if present."""
    normalized = normalize_model_id(model_name)
    for effort in REASONING_EFFORT_LEVELS:
        suffix = f"-{effort}"
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)], effort
    return normalized, None


def _coerce_reasoning_effort(
    effort: str | None,
    fallback: ReasoningEffort = "low",
) -> ReasoningEffort:
    """Validate and normalize an effort string into a valid ``ReasoningEffort``."""
    if not effort:
        return fallback
    cleaned = effort.strip().lower()
    if cleaned in ("minimal", "low", "medium", "high"):
        return cleaned  # type: ignore[return-value]
    return fallback


def build_oauth_thinking_config(
    effort: ReasoningEffort | str = "low",
) -> dict[str, Any]:
    """Build a fresh ``thinkingConfig`` dictionary for ``cloudcode-pa``."""
    resolved_effort = _coerce_reasoning_effort(str(effort), fallback="low")
    return copy.deepcopy(_THINKING_CONFIG_BY_EFFORT[resolved_effort])


def inject_oauth_thinking_config(
    generation_config: dict[str, Any] | None,
    effort: ReasoningEffort | str = "low",
) -> dict[str, Any]:
    """Return a copy of ``generation_config`` with ``thinkingConfig`` populated."""
    updated_config = copy.deepcopy(generation_config) if generation_config else {}
    updated_config["thinkingConfig"] = build_oauth_thinking_config(effort)
    return updated_config


def _select_effective_effort(
    reasoning_effort: str | None,
    suffix_effort: ReasoningEffort | None,
    fallback_effort: ReasoningEffort,
) -> ReasoningEffort:
    """Choose explicit ``reasoning_effort``, then ``suffix_effort``, else ``fallback_effort``."""
    if reasoning_effort is not None and reasoning_effort.strip():
        return _coerce_reasoning_effort(reasoning_effort, fallback=fallback_effort)
    if suffix_effort is not None:
        return suffix_effort
    return fallback_effort


def parse_model_and_effort(
    model_name: str,
    reasoning_effort: str | None = None,
    *,
    explicit_effort: str | None = None,
    is_routine_task: bool = False,
    default_effort: ReasoningEffort = "low",
) -> ResolvedModelSpec:
    """Resolve ``model_name`` and optional ``reasoning_effort`` into ``ResolvedModelSpec``."""
    requested_name = model_name.strip()
    base_model, suffix_effort = split_model_effort_suffix(requested_name)
    fallback: ReasoningEffort = "low" if is_routine_task else default_effort
    effective_effort_input = (
        explicit_effort if explicit_effort is not None else reasoning_effort
    )
    resolved_effort = _select_effective_effort(
        effective_effort_input, suffix_effort, fallback
    )
    return ResolvedModelSpec(
        requested_model=requested_name,
        base_model=base_model,
        reasoning_effort=resolved_effort,
        oauth_thinking_config=build_oauth_thinking_config(resolved_effort),
        web_extended_thinking=(resolved_effort != "minimal"),
        web_model_target=base_model,
    )


# ============================================================================
# 3. OAuth CLI Model Discovery & Automatic 403 Fallback
# ============================================================================


def _build_oauth_headers(access_token: str) -> dict[str, str]:
    """Construct authorization and JSON content headers for ``cloudcode-pa``."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_model_name_from_entry(entry: Any) -> str | None:
    """Extract a normalized model identifier from a string or dict entry."""
    if isinstance(entry, str):
        normalized = normalize_model_id(entry)
        return normalized or None
    if isinstance(entry, dict):
        for key in ("model", "name", "id", "modelId", "baseModelId", "model_id", "model_name"):
            raw_name = entry.get(key)
            if isinstance(raw_name, str) and raw_name.strip():
                return normalize_model_id(raw_name)
    return None


def _collect_models_from_container(
    container: Any,
    discovered: list[str],
) -> None:
    """Append unique normalized model names from a dict or list ``container``."""
    if isinstance(container, dict):
        for model_key, model_val in container.items():
            candidate = _extract_model_name_from_entry(model_val) or normalize_model_id(
                str(model_key)
            )
            if candidate and candidate not in discovered:
                discovered.append(candidate)
    elif isinstance(container, list):
        for model_entry in container:
            candidate = _extract_model_name_from_entry(model_entry)
            if candidate and candidate not in discovered:
                discovered.append(candidate)


def _parse_models_from_fetch_payload(payload: Any) -> list[str]:
    """Extract unique normalized model names from ``:fetchAvailableModels`` payload."""
    if not isinstance(payload, dict):
        return []
    discovered: list[str] = []
    for key in ("models", "availableModels", "modelIds", "data", "validModels", "items"):
        _collect_models_from_container(payload.get(key), discovered)
    return discovered


def _parse_iso_timestamp_string(timestamp_text: str) -> int | None:
    """Parse an ISO-8601 or integer string into Unix epoch seconds."""
    if timestamp_text.isdigit():
        return int(timestamp_text)
    try:
        iso_text = (
            timestamp_text[:-1] + "+00:00"
            if timestamp_text.endswith("Z")
            else timestamp_text
        )
        parsed_dt = datetime.fromisoformat(iso_text)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return int(parsed_dt.timestamp())
    except (ValueError, TypeError):
        return None


def _parse_reset_timestamp(raw_reset: Any) -> int | None:
    """Convert an ISO-8601 timestamp or numeric epoch value into Unix seconds."""
    if isinstance(raw_reset, (int, float)):
        return int(raw_reset)
    if isinstance(raw_reset, str) and raw_reset.strip():
        return _parse_iso_timestamp_string(raw_reset.strip())
    return None


def _extract_model_from_quota_bucket(bucket: dict[str, Any]) -> str | None:
    """Extract a normalized Gemini model identifier from a quota bucket dict."""
    for key in ("model", "modelId", "modelName", "name", "id", "model_id", "model_name"):
        raw_name = bucket.get(key)
        if isinstance(raw_name, str) and raw_name.strip():
            return normalize_model_id(raw_name)
        if isinstance(raw_name, dict):
            extracted = _extract_model_name_from_entry(raw_name)
            if extracted:
                return extracted

    metric_text = (
        bucket.get("metric")
        or bucket.get("tokenType")
        or bucket.get("metricType")
        or bucket.get("metricName")
        or ""
    )
    if isinstance(metric_text, str) and "gemini-" in metric_text.lower():
        match = re.search(r"(gemini-[a-z0-9.\-]+)", metric_text.lower())
        if match:
            return normalize_model_id(match.group(1))
    return None


def _parse_clamped_fraction(raw_fraction: Any) -> float:
    """Parse a quota fraction value clamped to ``[0.0, 1.0]``."""
    try:
        fraction_float = float(raw_fraction)
        if 1.0 < fraction_float <= 100.0:
            fraction_float = fraction_float / 100.0
        return max(0.0, min(1.0, fraction_float))
    except (TypeError, ValueError):
        return 1.0


def _build_model_quota_from_bucket(bucket: dict[str, Any]) -> ModelQuota:
    """Construct a ``ModelQuota`` instance from a ``:retrieveUserQuota`` bucket."""
    raw_fraction = bucket.get(
        "remainingFraction",
        bucket.get("remaining_fraction", bucket.get("remainingPct", 1.0)),
    )
    remaining_fraction = _parse_clamped_fraction(raw_fraction)
    reset_at = _parse_reset_timestamp(
        bucket.get("resetTime", bucket.get("reset_at", bucket.get("quotaResetTime")))
    )
    explicit_exhausted = bucket.get("isExhausted", bucket.get("is_exhausted"))
    is_exhausted = (
        bool(explicit_exhausted)
        if explicit_exhausted is not None
        else remaining_fraction <= 0.0
    )
    return ModelQuota(
        remaining_fraction=remaining_fraction,
        reset_at=reset_at,
        is_exhausted=is_exhausted,
    )


def _iter_fetch_model_dicts(container: Any) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(model_id, quota_details)`` pairs from a fetchAvailableModels container."""
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(container, dict):
        for model_key, model_details in container.items():
            if isinstance(model_details, dict):
                model_id = _extract_model_name_from_entry(
                    model_details
                ) or normalize_model_id(str(model_key))
                entries.append((model_id, model_details))
    elif isinstance(container, list):
        for model_entry in container:
            if isinstance(model_entry, dict):
                model_id = _extract_model_name_from_entry(model_entry)
                if model_id:
                    entries.append((model_id, model_entry))
    return entries


def _apply_fetch_models_quotas(
    account: OAuthAccount,
    payload: Any,
) -> dict[str, ModelQuota]:
    """Update ``account.quota_state.model_quotas`` if quota fields exist in fetch payload."""
    extracted_quotas: dict[str, ModelQuota] = {}
    if not isinstance(payload, dict):
        return extracted_quotas
    container = payload.get("models") or payload.get("availableModels")
    for model_id, quota_details in _iter_fetch_model_dicts(container):
        quota_dict = (
            quota_details.get("quotaInfo")
            or quota_details.get("quota")
            or quota_details
        )
        if isinstance(quota_dict, dict) and any(
            k in quota_dict
            for k in ("remainingFraction", "remaining_fraction", "resetTime", "reset_at")
        ):
            quota_obj = _build_model_quota_from_bucket(quota_dict)
            account.quota_state.model_quotas[model_id] = quota_obj
            extracted_quotas[model_id] = quota_obj
    return extracted_quotas


def _apply_quota_buckets(
    account: OAuthAccount,
    buckets: Any,
) -> list[str]:
    """Extract models and update ``account.quota_state.model_quotas`` from ``buckets``."""
    discovered: list[str] = []
    if isinstance(buckets, dict):
        for quota_key, quota_payload in buckets.items():
            model_id = normalize_model_id(str(quota_key))
            if model_id and model_id not in discovered:
                discovered.append(model_id)
            if model_id and isinstance(quota_payload, dict):
                account.quota_state.model_quotas[model_id] = (
                    _build_model_quota_from_bucket(quota_payload)
                )
    elif isinstance(buckets, list):
        for bucket_entry in buckets:
            if not isinstance(bucket_entry, dict):
                continue
            model_id = _extract_model_from_quota_bucket(bucket_entry)
            if not model_id:
                continue
            if model_id not in discovered:
                discovered.append(model_id)
            account.quota_state.model_quotas[model_id] = _build_model_quota_from_bucket(
                bucket_entry
            )
    return discovered


def _parse_and_apply_user_quota_payload(
    account: OAuthAccount,
    payload: Any,
) -> list[str]:
    """Parse ``:retrieveUserQuota`` response and update account models and quotas."""
    if not isinstance(payload, dict):
        return []
    buckets = (
        payload.get("buckets")
        or payload.get("quotaBuckets")
        or payload.get("models")
        or payload.get("quotas")
        or []
    )
    if not buckets and isinstance(payload.get("userQuota"), dict):
        user_quota_dict = payload["userQuota"]
        buckets = (
            user_quota_dict.get("buckets")
            or user_quota_dict.get("quotaBuckets")
            or user_quota_dict.get("models")
            or user_quota_dict.get("quotas")
            or []
        )
    discovered = _apply_quota_buckets(account, buckets)
    if discovered:
        account.discovered_models = list(discovered)
    return discovered


def _is_permission_denied_response(response: httpx.Response) -> bool:
    """Return True if ``response`` indicates 403 Forbidden or PERMISSION_DENIED."""
    if response.status_code == 403:
        return True
    try:
        body = response.json()
        if isinstance(body, dict):
            error_obj = body.get("error")
            if isinstance(error_obj, dict):
                status_text = str(error_obj.get("status", "")).upper()
                code_val = error_obj.get("code")
                if status_text == "PERMISSION_DENIED" or code_val == 403:
                    return True
    except (ValueError, TypeError):
        if "PERMISSION_DENIED" in response.text:
            return True
    return False


async def fetch_available_models(
    account: OAuthAccount,
    token: str,
    client: httpx.AsyncClient,
) -> httpx.Response:
    """Send POST request to ``v1internal:fetchAvailableModels``."""
    headers = _build_oauth_headers(token)
    request_body = {"project": account.credentials.project_id}
    return await client.post(
        FETCH_AVAILABLE_MODELS_URL,
        json=request_body,
        headers=headers,
    )


async def retrieve_user_quota(
    account: OAuthAccount,
    token: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Send POST request to ``v1internal:retrieveUserQuota`` and populate ``account`` quotas."""
    headers = _build_oauth_headers(token)
    request_body = {"project": account.credentials.project_id}
    quota_response = await client.post(
        RETRIEVE_USER_QUOTA_URL,
        json=request_body,
        headers=headers,
    )
    quota_response.raise_for_status()
    return _parse_and_apply_user_quota_payload(account, quota_response.json())


async def _execute_oauth_discovery_requests(
    account: OAuthAccount,
    token: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Perform ``:fetchAvailableModels`` with automatic 403 or missing-quota fallback to ``:retrieveUserQuota``."""
    try:
        response = await fetch_available_models(account, token, client)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and _is_permission_denied_response(exc.response):
            response = exc.response
        else:
            raise

    if _is_permission_denied_response(response):
        logger.info(
            "OAuth account %s received 403 on fetchAvailableModels; falling back to retrieveUserQuota",
            account.id,
        )
        return await retrieve_user_quota(account, token, client)

    response.raise_for_status()
    payload = response.json()
    models = _parse_models_from_fetch_payload(payload)
    quotas = _apply_fetch_models_quotas(account, payload)
    if not quotas:
        try:
            quota_models = await retrieve_user_quota(account, token, client)
            for qm in quota_models:
                if qm not in models:
                    models.append(qm)
        except Exception as exc:
            logger.debug(
                "retrieveUserQuota quota enrichment after fetchAvailableModels failed for account %s: %s",
                account.id,
                exc,
            )

    if models:
        account.discovered_models = list(models)
    for model_id in account.discovered_models:
        if model_id not in account.quota_state.model_quotas:
            account.quota_state.model_quotas[model_id] = ModelQuota(
                remaining_fraction=1.0,
                reset_at=None,
                is_exhausted=False,
            )
    return models


def _create_oauth_discovery_http_client(proxy: str | None = None) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with HTTP/2 enabled and graceful fallback if h2 is unavailable."""
    try:
        return httpx.AsyncClient(http2=True, timeout=30.0, proxy=proxy)
    except ImportError:
        return httpx.AsyncClient(timeout=30.0, proxy=proxy)


async def discover_oauth_models(
    account: OAuthAccount,
    *,
    access_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Discover available models for ``account`` via ``cloudcode-pa`` with 403 fallback."""
    token = (
        access_token if access_token is not None else account.credentials.access_token
    )
    if http_client is not None:
        return await _execute_oauth_discovery_requests(account, token, http_client)

    async with _create_oauth_discovery_http_client(proxy=account.proxy) as owned_client:
        return await _execute_oauth_discovery_requests(account, token, owned_client)


# ============================================================================
# 4. Web Pool Dynamic Model Discovery & Resolution
# ============================================================================


def _extract_web_model_name(model_item: Any) -> str | None:
    """Extract canonical model name from a ``gemini-webapi`` model object, enum, or dict."""
    if isinstance(model_item, str):
        normalized = normalize_model_id(model_item)
        return normalized or None
    if isinstance(model_item, dict):
        return _extract_model_name_from_entry(model_item)

    for attr in ("model_name", "model_id", "name", "id", "value"):
        attr_val = getattr(model_item, attr, None)
        if isinstance(attr_val, str) and attr_val.strip():
            return normalize_model_id(attr_val)
    return None


def _extract_models_from_list_result(raw_result: Any) -> list[str]:
    """Parse list of unique model names from raw result of ``client.list_models()``."""
    model_entries: list[Any]
    if isinstance(raw_result, dict):
        model_entries = list(
            raw_result.get("models")
            or raw_result.get("data")
            or raw_result.values()
        )
    elif isinstance(raw_result, (list, tuple, set, frozenset)):
        model_entries = list(raw_result)
    else:
        model_entries = []

    discovered: list[str] = []
    for model_entry in model_entries:
        model_id = _extract_web_model_name(model_entry)
        if model_id and model_id not in discovered:
            discovered.append(model_id)
    return discovered


async def discover_web_models(account: WebAccount, client: GeminiClient | Any) -> list[str]:
    """Discover available models on a Web account via ``client.list_models()``."""
    list_models_fn = getattr(client, "list_models", None)
    if not callable(list_models_fn):
        return list(account.discovered_models)

    raw_result = list_models_fn()
    if inspect.isawaitable(raw_result):
        raw_result = await raw_result

    discovered = _extract_models_from_list_result(raw_result)
    if discovered:
        account.discovered_models = list(discovered)
    return discovered


def _run_coroutine_sync(coro: Any) -> Any:
    """Safely execute an awaitable synchronously across event loop contexts."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def discover_web_models_sync(
    account: WebAccount,
    client: GeminiClient | Any,
) -> list[str]:
    """Synchronous model discovery on a Web account for synchronous callers."""
    list_models_fn = getattr(client, "list_models", None)
    if not callable(list_models_fn):
        return list(account.discovered_models)

    raw_result = list_models_fn()
    if inspect.isawaitable(raw_result):
        raw_result = _run_coroutine_sync(raw_result)

    discovered = _extract_models_from_list_result(raw_result)
    if discovered:
        account.discovered_models = list(discovered)
    return discovered


async def resolve_web_model(client: GeminiClient | Any, model_name: str) -> Any:
    """Resolve ``model_name`` via ``client.resolve_model(base_model)`` after stripping suffixes."""
    base_model, _ = split_model_effort_suffix(model_name)
    resolve_fn = getattr(client, "resolve_model", None)
    if not callable(resolve_fn):
        return base_model

    try:
        resolved = resolve_fn(base_model)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        return resolved
    except (KeyError, ValueError):
        return base_model


def resolve_web_model_sync(client: GeminiClient | Any, model_name: str) -> Any:
    """Synchronous model target resolution for synchronous callers."""
    base_model, _ = split_model_effort_suffix(model_name)
    resolve_fn = getattr(client, "resolve_model", None)
    if not callable(resolve_fn):
        return base_model

    try:
        resolved = resolve_fn(base_model)
        if inspect.isawaitable(resolved):
            resolved = _run_coroutine_sync(resolved)
        return resolved
    except (KeyError, ValueError):
        return base_model


# ============================================================================
# 5. Unified Model Registry (OpenAI /v1/models & Account Support Lookup)
# ============================================================================


def _account_supports_base_model(
    discovered_models: list[str],
    base_model: str,
) -> bool:
    """Return True if ``discovered_models`` matches ``base_model``."""
    if not discovered_models or not base_model:
        return False
    normalized_target, _ = split_model_effort_suffix(normalize_model_id(base_model))
    for candidate in discovered_models:
        cand_base, _ = split_model_effort_suffix(normalize_model_id(candidate))
        if cand_base == normalized_target:
            return True
    return False


class UnifiedModelRegistry:
    """Aggregates models across OAuth and Web pools and exposes OpenAI model lists."""

    def __init__(self, registry: AccountsStateRegistry | None = None) -> None:
        self._registry: AccountsStateRegistry = (
            registry if registry is not None else state_registry
        )

    def get_base_models(self, include_defaults: bool = False) -> list[str]:
        """Return ordered unique base model names across active accounts (and optional defaults)."""
        models: list[str] = []
        if include_defaults:
            for default_model in DEFAULT_STANDARD_MODELS:
                norm_default, _ = split_model_effort_suffix(normalize_model_id(default_model))
                if norm_default and norm_default not in models:
                    models.append(norm_default)

        for oauth_acc in self._registry.oauth_accounts:
            if oauth_acc.status == "SUSPENDED":
                continue
            for discovered in oauth_acc.discovered_models:
                base_name, _ = split_model_effort_suffix(normalize_model_id(discovered))
                if base_name and base_name not in models:
                    models.append(base_name)

        for web_acc in self._registry.web_accounts:
            if web_acc.status == "SUSPENDED":
                continue
            for discovered in web_acc.discovered_models:
                base_name, _ = split_model_effort_suffix(normalize_model_id(discovered))
                if base_name and base_name not in models:
                    models.append(base_name)

        return models

    def get_all_model_ids(
        self,
        *,
        include_effort_variants: bool = True,
        include_defaults: bool = False,
    ) -> list[str]:
        """Return all base model IDs plus reasoning effort suffix variants."""
        base_models = self.get_base_models(include_defaults=include_defaults)
        if not base_models:
            return []
        if not include_effort_variants:
            return base_models

        all_ids: list[str] = []
        for base_model in base_models:
            if base_model not in all_ids:
                all_ids.append(base_model)
            for suffix in EFFORT_SUFFIXES:
                variant = f"{base_model}{suffix}"
                if variant not in all_ids:
                    all_ids.append(variant)
        return all_ids

    def list_openai_models(self, *, include_defaults: bool = False) -> dict[str, Any]:
        """Return an OpenAI-compatible ``GET /v1/models`` response dictionary."""
        all_ids = self.get_all_model_ids(
            include_effort_variants=True,
            include_defaults=include_defaults,
        )
        if not all_ids:
            return {
                "object": "list",
                "data": [],
            }
        model_entries = [
            {
                "id": model_id,
                "object": "model",
                "created": 1735689600,
                "owned_by": "google-hybrid-pool",
            }
            for model_id in all_ids
        ]
        return {
            "object": "list",
            "data": model_entries,
        }

    def get_supporting_accounts(
        self,
        base_model: str,
        *,
        active_only: bool = False,
    ) -> dict[str, list[str]]:
        """Return dictionary mapping pool type to account IDs supporting ``base_model``."""
        oauth_ids = [
            acc.id
            for acc in self._registry.oauth_accounts
            if (acc.status == "ACTIVE" if active_only else acc.status != "SUSPENDED")
            and _account_supports_base_model(acc.discovered_models, base_model)
        ]
        web_ids = [
            acc.id
            for acc in self._registry.web_accounts
            if (acc.status == "ACTIVE" if active_only else acc.status != "SUSPENDED")
            and _account_supports_base_model(acc.discovered_models, base_model)
        ]
        return {
            "oauth_accounts": oauth_ids,
            "web_accounts": web_ids,
            "oauth": oauth_ids,
            "web": web_ids,
        }

    def get_supporting_account_objects(
        self,
        base_model: str,
        *,
        active_only: bool = False,
    ) -> dict[str, list[OAuthAccount | WebAccount]]:
        """Return dictionary mapping pool type to account objects supporting ``base_model``."""
        oauth_matches: list[OAuthAccount] = [
            acc
            for acc in self._registry.oauth_accounts
            if (acc.status == "ACTIVE" if active_only else acc.status != "SUSPENDED")
            and _account_supports_base_model(acc.discovered_models, base_model)
        ]
        web_matches: list[WebAccount] = [
            acc
            for acc in self._registry.web_accounts
            if (acc.status == "ACTIVE" if active_only else acc.status != "SUSPENDED")
            and _account_supports_base_model(acc.discovered_models, base_model)
        ]
        return {
            "oauth_accounts": oauth_matches,
            "web_accounts": web_matches,
            "oauth": oauth_matches,
            "web": web_matches,
        }


unified_model_registry = UnifiedModelRegistry()


def get_supporting_accounts(
    base_model: str,
    *,
    registry: AccountsStateRegistry | None = None,
    active_only: bool = False,
) -> dict[str, list[str]]:
    """Return dictionary mapping pool type to account IDs supporting ``base_model``."""
    target_registry = (
        unified_model_registry
        if registry is None
        else UnifiedModelRegistry(registry)
    )
    return target_registry.get_supporting_accounts(base_model, active_only=active_only)


def get_supporting_account_objects(
    base_model: str,
    *,
    registry: AccountsStateRegistry | None = None,
    active_only: bool = False,
) -> dict[str, list[OAuthAccount | WebAccount]]:
    """Return dictionary mapping pool type to account objects supporting ``base_model``."""
    target_registry = (
        unified_model_registry
        if registry is None
        else UnifiedModelRegistry(registry)
    )
    return target_registry.get_supporting_account_objects(
        base_model, active_only=active_only
    )
