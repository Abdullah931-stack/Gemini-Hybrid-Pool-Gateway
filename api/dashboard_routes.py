"""Dashboard API routes for live telemetry, account pool management, and quota synchronization.

Implements Level 5 (Control Plane & Dashboard) of Gemini-Hybrid-Pool-Gateway:
1. Real-time telemetry report (GET /api/status):
   - Pool-wide KPIs (active accounts, total concurrent requests, pool health, available models).
   - OAuth accounts (id, status, concurrency C, priority score P, per-model quota buckets, tau).
   - Web accounts (id, status, concurrency C, priority score P, 5h & weekly usage %, resets, tau, Si, D_conc).
2. Live OAuth account management (POST /api/oauth-accounts/init, POST /api/oauth-accounts/complete, DELETE /api/oauth-accounts/{id}).
3. Live Web account management (POST /api/web-accounts/add, DELETE /api/web-accounts/{id}).
4. Immediate quota synchronization (POST /api/accounts/sync-quotas).
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config.settings import (
    BASE_DIR,
    COOKIES_CACHE_DIR,
    OAuthAccount,
    OAuthCredentials,
    WebAccount,
    WebCookies,
    save_accounts_snapshot,
    state_registry,
)
from core.auth_oauth_cli import (
    build_authorization_url,
    complete_oauth_login,
    extract_code_from_manual_input,
    generate_pkce_pair,
)
from core.auth_web import (
    ChromiumDBSCUnsupportedError,
    extract_firefox_cookies,
    get_account_cookie_cache_path,
    get_saved_info_hygiene_notice,
    parse_cookie_header_or_dict,
    parse_manual_cookies,
    save_account_cookies_isolated,
)
from core.hybrid_scheduler import (
    calculate_oauth_account_score,
    calculate_soonest_reset_urgency,
    calculate_web_account_score,
    calculate_web_concurrency_denominator,
)
from core.model_registry import discover_oauth_models, unified_model_registry
from core.usage_monitor import sync_all_registered_accounts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Dashboard"])

STATIC_DIR: Path = BASE_DIR / "static"
INDEX_HTML_PATH: Path = STATIC_DIR / "index.html"
_SAFE_ACCOUNT_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

_pending_oauth_sessions: dict[str, dict[str, Any]] = {}
_PENDING_SESSION_TTL_SECONDS: float = 3600.0


# ============================================================================
# 1. Pydantic Request & Response DTOs
# ============================================================================


class OAuthInitRequest(BaseModel):
    """Payload for initializing an OAuth PKCE authentication flow."""

    model_config = ConfigDict(populate_by_name=True)

    redirect_uri: str | None = None
    client_id: str | None = None
    verifier_length: int = Field(default=64, ge=43, le=128)


class OAuthInitResponse(BaseModel):
    """Result of OAuth PKCE initialization."""

    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


class OAuthCompleteRequest(BaseModel):
    """Payload for completing an OAuth login exchange."""

    model_config = ConfigDict(populate_by_name=True)

    code: str
    account_id: str | None = None
    label: str = ""
    state: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None
    transport_mode: Literal["http_v1internal", "agy_subprocess"] = "http_v1internal"
    max_concurrency: int = Field(default=3, ge=1, le=10)
    client_id: str | None = None
    client_secret: str | None = None


class WebAccountAddRequest(BaseModel):
    """Payload for adding a new Web cookie pool account."""

    model_config = ConfigDict(populate_by_name=True)

    account_id: str
    label: str = ""
    firefox_profile_path: str | None = None
    cookies: str | dict[str, Any] | None = None
    secure_1psid: str | None = None
    secure_1psidts: str = ""
    proxy: str | None = None
    max_concurrency: int = Field(default=2, ge=1, le=8)


# ============================================================================
# 2. Session Management & Telemetry Helpers
# ============================================================================


def _prune_expired_oauth_sessions(now: float) -> None:
    """Purge pending OAuth sessions older than TTL."""
    expired_keys = [
        key
        for key, session in _pending_oauth_sessions.items()
        if now - session.get("created_at", 0.0) > _PENDING_SESSION_TTL_SECONDS
    ]
    for key in expired_keys:
        _pending_oauth_sessions.pop(key, None)


def _store_pending_oauth_session(
    state: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str | None,
    now: float,
) -> None:
    """Store generated PKCE challenge parameters for verification during completion."""
    _prune_expired_oauth_sessions(now)
    _pending_oauth_sessions[state] = {
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "created_at": now,
    }


def _determine_pool_status(total: int, active: int, cooldown: int) -> str:
    """Compute pool status badge according to account availability."""
    if total == 0:
        return "EMPTY"
    if active > 0:
        return "DEGRADED" if (cooldown > 0 or active < total) else "HEALTHY"
    if cooldown > 0:
        return "DEGRADED"
    return "DOWN"


def _format_oauth_quotas(account: OAuthAccount) -> dict[str, dict[str, Any]]:
    """Format per-model quota metrics for an OAuth account."""
    quotas_map: dict[str, dict[str, Any]] = {}
    for model_name, quota in account.quota_state.model_quotas.items():
        quotas_map[model_name] = {
            "remaining_fraction": round(quota.remaining_fraction, 4),
            "reset_at": quota.reset_at,
            "is_exhausted": quota.is_exhausted,
        }
    return quotas_map


def _find_soonest_oauth_reset(account: OAuthAccount) -> int | None:
    """Return the nearest future reset timestamp across all model quotas."""
    candidates = [
        quota.reset_at
        for quota in account.quota_state.model_quotas.values()
        if quota.reset_at is not None and quota.reset_at > 0
    ]
    return min(candidates) if candidates else None


def _build_oauth_account_telemetry(
    account: OAuthAccount,
    now: float,
) -> dict[str, Any]:
    """Compile detailed telemetry object for a registered OAuth account."""
    soonest_reset = _find_soonest_oauth_reset(account)
    tau = calculate_soonest_reset_urgency(soonest_reset, now=now)
    primary_model = account.discovered_models[0] if account.discovered_models else "gemini-3.8-flash"
    score = calculate_oauth_account_score(account, primary_model, now=now)

    return {
        "id": account.id,
        "label": account.label,
        "status": account.status,
        "transport_mode": account.transport_mode,
        "active_requests": account.active_requests,
        "max_concurrency": account.max_concurrency,
        "recent_latency_ms": round(account.recent_latency_ms, 2),
        "priority_score": round(score, 4),
        "tau": round(tau, 3),
        "cooldown_until": account.quota_state.cooldown_until,
        "consecutive_errors": account.quota_state.consecutive_errors,
        "last_error_reason": account.quota_state.last_error_reason,
        "discovered_models": account.discovered_models,
        "model_quotas": _format_oauth_quotas(account),
    }


def _build_web_account_telemetry(
    account: WebAccount,
    now: float,
) -> dict[str, Any]:
    """Compile detailed telemetry object for a registered Web account."""
    eff_5h = round(min(1.0, account.usage.five_hour_pct + account.optimistic_5h_delta), 4)
    eff_w = round(min(1.0, account.usage.weekly_pct + account.optimistic_weekly_delta), 4)
    tau = calculate_soonest_reset_urgency(account.usage.five_hour_reset_at, now=now)
    affinity = 1.0
    d_conc = calculate_web_concurrency_denominator(account.active_requests, affinity)
    score = calculate_web_account_score(account, "gemini-4-argon", now=now)

    return {
        "id": account.id,
        "label": account.label,
        "status": account.status,
        "tier": account.tier,
        "active_requests": account.active_requests,
        "max_concurrency": account.max_concurrency,
        "priority_score": round(score, 4),
        "cooldown_until": account.cooldown_until,
        "consecutive_errors": account.consecutive_errors,
        "last_error_reason": account.last_error_reason,
        "discovered_models": account.discovered_models,
        "usage": {
            "five_hour_pct": round(account.usage.five_hour_pct, 4),
            "optimistic_5h_delta": round(account.optimistic_5h_delta, 4),
            "effective_5h_pct": eff_5h,
            "five_hour_reset_at": account.usage.five_hour_reset_at,
            "weekly_pct": round(account.usage.weekly_pct, 4),
            "optimistic_weekly_delta": round(account.optimistic_weekly_delta, 4),
            "effective_weekly_pct": eff_w,
            "weekly_reset_at": account.usage.weekly_reset_at,
            "last_synced_at": account.usage.last_synced_at,
        },
        "dynamic_metrics": {
            "tau": round(tau, 3),
            "Si": round(affinity, 2),
            "D_conc": round(d_conc, 2),
            "priority_score": round(score, 4),
        },
    }


def _validate_account_id(account_id: str) -> str:
    """Validate and sanitize account identifier to prevent path traversal."""
    cleaned = account_id.strip()
    if not cleaned or not _SAFE_ACCOUNT_ID_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid account ID '{account_id}'. Must be alphanumeric and "
                "safe for file paths without dots or slashes."
            ),
        )
    return cleaned


def _extract_web_cookies_from_request(request: WebAccountAddRequest) -> WebCookies:
    """Resolve and parse WebCookies from the incoming request payload."""
    try:
        if request.firefox_profile_path and request.firefox_profile_path.strip():
            return extract_firefox_cookies(request.firefox_profile_path.strip())
        if request.cookies:
            return parse_cookie_header_or_dict(request.cookies)
        if request.secure_1psid and request.secure_1psid.strip():
            return parse_manual_cookies(request.secure_1psid, request.secure_1psidts)
    except (FileNotFoundError, ChromiumDBSCUnsupportedError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Must provide either 'firefox_profile_path', 'cookies', or 'secure_1psid'.",
    )


async def _discover_models_for_new_oauth(account: OAuthAccount) -> list[str]:
    """Attempt dynamic model discovery for newly authorized OAuth account."""
    try:
        discovered = await discover_oauth_models(account)
        if discovered:
            return discovered
    except Exception as exc:
        logger.warning("Dynamic model discovery skipped for %s: %s", account.id, exc)

    return ["gemini-3.8-flash", "gemini-3.1-pro", "gemini-3.1-flash-lite"]


# ============================================================================
# 3. HTTP API Endpoints
# ============================================================================


@router.get("/status")
async def get_gateway_status() -> dict[str, Any]:
    """Return real-time telemetry, accounts list, and dynamic metrics."""
    now = time.time()
    oauth_list = [_build_oauth_account_telemetry(acc, now) for acc in state_registry.oauth_accounts]
    web_list = [_build_web_account_telemetry(acc, now) for acc in state_registry.web_accounts]

    total_accs = len(oauth_list) + len(web_list)
    active_accs = sum(1 for item in (oauth_list + web_list) if item["status"] == "ACTIVE")
    cooldown_accs = sum(1 for item in (oauth_list + web_list) if item["status"] == "COOLDOWN")
    suspended_accs = sum(1 for item in (oauth_list + web_list) if item["status"] == "SUSPENDED")
    total_concurrency = sum(int(item["active_requests"]) for item in (oauth_list + web_list))

    pool_status = _determine_pool_status(total_accs, active_accs, cooldown_accs)
    available_models = unified_model_registry.get_base_models()

    return {
        "timestamp": int(now),
        "pool_status": pool_status,
        "general_stats": {
            "total_accounts": total_accs,
            "active_accounts": active_accs,
            "cooldown_accounts": cooldown_accs,
            "suspended_accounts": suspended_accs,
            "total_concurrency": total_concurrency,
            "available_models_count": len(available_models),
        },
        "available_models": available_models,
        "oauth_accounts": oauth_list,
        "web_accounts": web_list,
    }


@router.post("/oauth-accounts/init", response_model=OAuthInitResponse)
async def init_oauth_account(payload: OAuthInitRequest | None = None) -> OAuthInitResponse:
    """Generate PKCE challenge and return Google authorization URL."""
    now = time.time()
    req = payload or OAuthInitRequest()
    redirect_uri = (req.redirect_uri or "http://localhost:8000/oauth/callback").strip()

    pkce = generate_pkce_pair(verifier_length=req.verifier_length)
    auth_url = build_authorization_url(
        redirect_uri=redirect_uri,
        pkce=pkce,
        client_id=req.client_id,
    )
    _store_pending_oauth_session(pkce.state, pkce.code_verifier, redirect_uri, req.client_id, now)

    return OAuthInitResponse(
        auth_url=auth_url,
        state=pkce.state,
        code_verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
    )


@router.post("/oauth-accounts/complete")
async def complete_oauth_account(payload: OAuthCompleteRequest) -> dict[str, Any]:
    """Exchange authorization code, register OAuth account, and persist atomically."""
    now = time.time()
    code_raw = payload.code.strip()
    if not code_raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Code cannot be empty.")

    saved_session = _pending_oauth_sessions.pop(payload.state, {}) if payload.state else {}
    code_verifier = (payload.code_verifier or saved_session.get("code_verifier") or "").strip()
    if not code_verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code_verifier. Provide code_verifier or state from /api/oauth-accounts/init.",
        )

    redirect_uri = (payload.redirect_uri or saved_session.get("redirect_uri") or "http://localhost:8000/oauth/callback").strip()
    client_id = payload.client_id or saved_session.get("client_id")

    try:
        clean_code = extract_code_from_manual_input(code_raw, expected_state=payload.state)
        credentials = await complete_oauth_login(
            code=clean_code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            client_id=client_id,
            client_secret=payload.client_secret,
        )
    except Exception as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    acc_id = (payload.account_id or "").strip() or f"oauth-{credentials.project_id or secrets.token_hex(4)}"
    validated_id = _validate_account_id(acc_id)

    account = OAuthAccount(
        id=validated_id,
        label=payload.label.strip(),
        transport_mode=payload.transport_mode,
        max_concurrency=payload.max_concurrency,
        credentials=credentials,
        status="ACTIVE",
    )
    account.discovered_models = await _discover_models_for_new_oauth(account)

    state_registry.upsert_oauth_account(account)
    await save_accounts_snapshot()

    return {
        "success": True,
        "message": f"OAuth account '{validated_id}' registered successfully.",
        "account": _build_oauth_account_telemetry(account, now),
    }


@router.delete("/oauth-accounts/{account_id}")
async def delete_oauth_account(account_id: str) -> dict[str, Any]:
    """Delete an OAuth account from memory registry and persist update to disk."""
    validated_id = _validate_account_id(account_id)
    if state_registry.get_oauth_account(validated_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"OAuth account '{validated_id}' not found.")

    state_registry.remove_oauth_account(validated_id)
    await save_accounts_snapshot()
    return {"success": True, "message": f"OAuth account '{validated_id}' deleted successfully."}


@router.post("/web-accounts/add")
async def add_web_account(payload: WebAccountAddRequest) -> dict[str, Any]:
    """Add a Web Cookie pool account, save isolated cookies file, and persist snapshot."""
    now = time.time()
    validated_id = _validate_account_id(payload.account_id)
    parsed_cookies = _extract_web_cookies_from_request(payload)

    cookie_file_path = get_account_cookie_cache_path(validated_id)
    account = WebAccount(
        id=validated_id,
        label=payload.label.strip(),
        max_concurrency=payload.max_concurrency,
        proxy=payload.proxy.strip() if payload.proxy else None,
        cookie_file=str(cookie_file_path),
        cookies=parsed_cookies,
        status="ACTIVE",
        discovered_models=["gemini-4-argon", "gemini-3.1-pro", "gemini-3.8-flash", "gemini-3.1-flash-lite"],
    )

    save_account_cookies_isolated(account, sync_snapshot=False)
    state_registry.upsert_web_account(account)
    await save_accounts_snapshot()

    return {
        "success": True,
        "message": f"Web account '{validated_id}' registered successfully.",
        "account": _build_web_account_telemetry(account, now),
        "hygiene_notice": get_saved_info_hygiene_notice(),
    }


@router.delete("/web-accounts/{account_id}")
async def delete_web_account(account_id: str) -> dict[str, Any]:
    """Delete a Web account, remove its isolated cookies file, and update snapshot."""
    validated_id = _validate_account_id(account_id)
    if state_registry.get_web_account(validated_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Web account '{validated_id}' not found.")

    state_registry.remove_web_account(validated_id)
    cookie_path = get_account_cookie_cache_path(validated_id)
    if cookie_path.exists():
        try:
            cookie_path.unlink()
        except OSError as exc:
            logger.warning("Could not unlink cookies file for %s: %s", validated_id, exc)

    await save_accounts_snapshot()
    return {"success": True, "message": f"Web account '{validated_id}' deleted successfully."}


@router.post("/accounts/sync-quotas")
async def sync_accounts_quotas() -> dict[str, Any]:
    """Trigger immediate quota and usage synchronization across all registered accounts."""
    report = await sync_all_registered_accounts(state_registry, persist=True)
    return {
        "success": True,
        "report": report,
    }


# ============================================================================
# 4. Dashboard Web UI Route
# ============================================================================


@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard_ui() -> Any:
    """Serve the static/index.html visual dashboard UI."""
    if INDEX_HTML_PATH.exists():
        return FileResponse(INDEX_HTML_PATH, media_type="text/html")
    return HTMLResponse(
        content="<h1>Gemini Hybrid Pool Gateway Dashboard</h1><p>UI loading or static/index.html missing.</p>",
        status_code=status.HTTP_200_OK,
    )
