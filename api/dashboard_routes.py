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
    DEFAULT_MODEL_ROUTES,
    ModelRouteConfig,
    OAuthAccount,
    OAuthCredentials,
    WebAccount,
    WebCookies,
    get_custom_model_routes,
    save_accounts_snapshot,
    save_custom_model_routes,
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
    auto_discover_firefox_cookies,
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
    get_model_channel_preference,
)
from core.model_registry import discover_oauth_models, unified_model_registry
from core.usage_monitor import sync_all_registered_accounts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Dashboard"])
callback_router = APIRouter(tags=["OAuth Callback"])

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
    label: str = ""
    max_concurrency: int = Field(default=3, ge=1, le=10)
    concurrency: int | None = Field(default=None)


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


class WebAutoDetectRequest(BaseModel):
    """Payload for auto-detecting Firefox cookies and linking web account."""

    model_config = ConfigDict(populate_by_name=True)

    account_id: str | None = None
    label: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=8)
    auto_link: bool = True
    open_browser_if_missing: bool = False


class WebBrowserLoginRequest(BaseModel):
    """Payload for opening browser to gemini.google.com and optional polling."""

    model_config = ConfigDict(populate_by_name=True)

    account_id: str | None = None
    label: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=8)
    poll_seconds: float = Field(default=0.0, ge=0.0, le=60.0)


class ModelRouteUpdateRequest(BaseModel):
    """Payload for updating or creating a model channel route."""

    model_config = ConfigDict(populate_by_name=True)

    model: str
    primary_channel: Literal["oauth", "web"] = Field(alias="primary")
    failover_channel: Literal["oauth", "web", "none", "None"] | None = Field(
        default=None, alias="failover"
    )


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
    label: str = "",
    concurrency: int = 3,
) -> None:
    """Store generated PKCE challenge parameters for verification during completion."""
    _prune_expired_oauth_sessions(now)
    _pending_oauth_sessions[state] = {
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "label": label,
        "concurrency": concurrency,
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

    return []


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
    effective_concurrency = req.concurrency if req.concurrency is not None else req.max_concurrency

    pkce = generate_pkce_pair(verifier_length=req.verifier_length)
    auth_url = build_authorization_url(
        redirect_uri=redirect_uri,
        pkce=pkce,
        client_id=req.client_id,
    )
    _store_pending_oauth_session(
        state=pkce.state,
        code_verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
        client_id=req.client_id,
        now=now,
        label=req.label.strip(),
        concurrency=effective_concurrency,
    )

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


@callback_router.get("/oauth/callback", response_class=HTMLResponse)
@router.get("/oauth/callback", response_class=HTMLResponse)
async def handle_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Automated browser OAuth redirect handler completing authorization without manual copy-paste."""
    now = time.time()
    if error:
        err_msg = error_description or error
        return HTMLResponse(
            content=f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>OAuth Error</title>
<style>
body {{ background: #070b12; color: #f8fafc; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
.card {{ background: #0f172a; border: 1px solid #f43f5e; padding: 2rem; border-radius: 12px; text-align: center; max-width: 450px; }}
h2 {{ color: #f43f5e; }}
p {{ color: #94a3b8; font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="card">
  <h2>Google Authorization Denied</h2>
  <p>{err_msg}</p>
  <p>You can close this window and try again.</p>
</div>
<script>
if (window.opener) {{
  window.opener.postMessage({{ type: 'OAUTH_ERROR', error: '{err_msg}' }}, '*');
}}
</script>
</body>
</html>""",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not code:
        return HTMLResponse(
            content="""<!DOCTYPE html><html><body style="background:#070b12;color:#f8fafc;font-family:sans-serif;text-align:center;padding:3rem;"><h3>Missing authorization code.</h3></body></html>""",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    saved_session = _pending_oauth_sessions.pop(state, {}) if state else {}
    code_verifier = (saved_session.get("code_verifier") or "").strip()
    redirect_uri = (saved_session.get("redirect_uri") or "http://localhost:8000/oauth/callback").strip()
    client_id = saved_session.get("client_id")
    session_label = saved_session.get("label", "")
    session_concurrency = saved_session.get("concurrency", 3)

    if not code_verifier and not state and len(_pending_oauth_sessions) == 1:
        first_key = next(iter(_pending_oauth_sessions))
        fallback_session = _pending_oauth_sessions.pop(first_key)
        code_verifier = fallback_session.get("code_verifier", "")
        redirect_uri = fallback_session.get("redirect_uri", redirect_uri)
        client_id = fallback_session.get("client_id", client_id)
        session_label = fallback_session.get("label", session_label)
        session_concurrency = fallback_session.get("concurrency", session_concurrency)

    if not code_verifier:
        return HTMLResponse(
            content="""<!DOCTYPE html><html><body style="background:#070b12;color:#f8fafc;font-family:sans-serif;text-align:center;padding:3rem;">
<h3 style="color:#f43f5e;">Authorization Session Expired or Not Found</h3>
<p style="color:#94a3b8;">Please return to the dashboard and initiate login again.</p>
<a href="/dashboard" style="color:#38bdf8;">Return to Dashboard</a>
</body></html>""",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        clean_code = extract_code_from_manual_input(code, expected_state=state)
        credentials = await complete_oauth_login(
            code=clean_code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            client_id=client_id,
        )
    except Exception as exc:
        logger.error("OAuth token exchange failed during callback: %s", exc)
        return HTMLResponse(
            content=f"""<!DOCTYPE html><html><body style="background:#070b12;color:#f8fafc;font-family:sans-serif;text-align:center;padding:3rem;">
<h3 style="color:#f43f5e;">Token Exchange Failed</h3>
<p style="color:#94a3b8;">{exc}</p>
<a href="/dashboard" style="color:#38bdf8;">Return to Dashboard</a>
</body></html>""",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    acc_id = f"oauth-{credentials.project_id or secrets.token_hex(4)}"
    validated_id = _validate_account_id(acc_id)
    account_label = session_label if session_label else f"OAuth CLI ({credentials.project_id or 'CloudCode'})"

    account = OAuthAccount(
        id=validated_id,
        label=account_label,
        transport_mode="http_v1internal",
        max_concurrency=session_concurrency,
        credentials=credentials,
        status="ACTIVE",
    )
    account.discovered_models = await _discover_models_for_new_oauth(account)

    state_registry.upsert_oauth_account(account)
    await save_accounts_snapshot()

    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Google Authorization & Sign-In Successful</title>
  <style>
    body {{
      background: #070b12;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      text-align: center;
    }}
    .card {{
      background: #0f172a;
      border: 1px solid #10b981;
      border-radius: 14px;
      padding: 2.5rem 2rem;
      max-width: 480px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.6);
    }}
    h2 {{ color: #10b981; margin-bottom: 0.5rem; font-size: 1.4rem; }}
    p {{ color: #94a3b8; font-size: 0.95rem; margin-top: 0.5rem; }}
    .badge {{ display: inline-block; padding: 0.25rem 0.75rem; background: rgba(16,185,129,0.15); color: #10b981; border-radius: 20px; font-weight: 600; font-size: 0.85rem; margin-top: 0.75rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>✓ Google Sign-In Successful!</h2>
    <div class="badge">{validated_id}</div>
    <p>Google Authorization Successful: account linked with {len(account.discovered_models)} discovered models.</p>
    <p style="color: #64748b; font-size: 0.8rem; margin-top: 1rem;">Returning to Dashboard...</p>
  </div>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({{ type: 'OAUTH_SUCCESS', account_id: '{validated_id}', success: true }}, '*');
        window.opener.postMessage({{ type: 'OAUTH_COMPLETE', account_id: '{validated_id}', success: true }}, '*');
        setTimeout(() => {{ window.close(); }}, 800);
      }} else {{
        setTimeout(() => {{ window.location.href = '/dashboard'; }}, 1200);
      }}
    }} catch (e) {{
      setTimeout(() => {{ window.location.href = '/dashboard'; }}, 1200);
    }}
  </script>
</body>
</html>""",
        status_code=status.HTTP_200_OK,
    )


@router.delete("/oauth-accounts/{account_id}")
async def delete_oauth_account(account_id: str) -> dict[str, Any]:
    """Delete an OAuth account from memory registry and persist update to disk."""
    validated_id = _validate_account_id(account_id)
    if state_registry.get_oauth_account(validated_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"OAuth account '{validated_id}' not found.")

    state_registry.remove_oauth_account(validated_id)
    try:
        from config.settings import get_lmdb_store
        get_lmdb_store().delete_sessions_for_account(validated_id)
    except Exception:
        pass
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
        discovered_models=[],
    )

    save_account_cookies_isolated(account, sync_snapshot=False)
    state_registry.upsert_web_account(account)

    try:
        from core.auth_web import default_web_session_manager
        from core.model_registry import discover_web_models
        from core.usage_monitor import sync_web_account_usage
        await sync_web_account_usage(account, session_manager=default_web_session_manager)
        client = await default_web_session_manager.get_client(account)
        if client is not None:
            discovered = await discover_web_models(account, client)
            if discovered:
                account.discovered_models = discovered
    except Exception as exc:
        logger.warning("Web account setup or model discovery skipped for %s: %s", validated_id, exc)

    await save_accounts_snapshot()

    return {
        "success": True,
        "message": f"Web account '{validated_id}' registered successfully.",
        "account": _build_web_account_telemetry(account, now),
        "hygiene_notice": get_saved_info_hygiene_notice(),
    }


@router.post("/web-accounts/auto-detect")
async def auto_detect_web_account(payload: WebAutoDetectRequest | None = None) -> dict[str, Any]:
    """Auto-detect active Gemini session from Firefox/browser and link account immediately."""
    req = payload or WebAutoDetectRequest()
    sqlite_path, cookies = auto_discover_firefox_cookies()

    if not cookies or not cookies.secure_1psid:
        if req.open_browser_if_missing:
            try:
                import webbrowser
                webbrowser.open("https://gemini.google.com")
            except Exception as exc:
                logger.warning("Failed to open browser: %s", exc)
        return {
            "success": False,
            "logged_in": False,
            "login_url": "https://gemini.google.com",
            "message": "No active Gemini session found in Firefox. Please sign in to https://gemini.google.com.",
        }

    if not req.auto_link:
        return {
            "success": True,
            "logged_in": True,
            "source_path": str(sqlite_path) if sqlite_path else None,
            "cookies": {
                "secure_1psid": cookies.secure_1psid,
                "secure_1psidts": cookies.secure_1psidts,
            },
        }

    now = time.time()
    raw_id = (req.account_id or "").strip() or f"web-{secrets.token_hex(4)}"
    validated_id = _validate_account_id(raw_id)
    cookie_file_path = get_account_cookie_cache_path(validated_id)
    account_label = req.label.strip() or f"Firefox Web ({validated_id})"

    account = WebAccount(
        id=validated_id,
        label=account_label,
        max_concurrency=req.max_concurrency,
        cookie_file=str(cookie_file_path),
        cookies=cookies,
        status="ACTIVE",
        discovered_models=[
            "gemini-4-argon",
            "gemini-3.1-pro",
            "gemini-3.8-flash",
            "gemini-3.1-flash-lite",
        ],
    )

    save_account_cookies_isolated(account, sync_snapshot=False)
    state_registry.upsert_web_account(account)

    try:
        from core.auth_web import default_web_session_manager
        from core.model_registry import discover_web_models
        from core.usage_monitor import sync_web_account_usage
        await sync_web_account_usage(account, session_manager=default_web_session_manager)
        client = await default_web_session_manager.get_client(account)
        if client is not None:
            discovered = await discover_web_models(account, client)
            if discovered:
                account.discovered_models = discovered
    except Exception as exc:
        logger.warning("Auto-detected web account discovery warning: %s", exc)

    await save_accounts_snapshot()

    return {
        "success": True,
        "logged_in": True,
        "account_id": validated_id,
        "source_path": str(sqlite_path) if sqlite_path else None,
        "message": f"Successfully auto-detected and linked Web account '{validated_id}'!",
        "account": _build_web_account_telemetry(account, now),
        "hygiene_notice": get_saved_info_hygiene_notice(),
    }


@router.post("/web-accounts/browser-login")
async def trigger_web_browser_login(payload: WebBrowserLoginRequest | None = None) -> dict[str, Any]:
    """Open https://gemini.google.com in browser and optionally poll for cookies."""
    req = payload or WebBrowserLoginRequest()
    login_url = "https://gemini.google.com"

    try:
        import webbrowser
        webbrowser.open(login_url)
    except Exception as exc:
        logger.warning("Failed to open browser: %s", exc)

    if req.poll_seconds > 0:
        start_t = time.time()
        while time.time() - start_t < req.poll_seconds:
            await asyncio.sleep(1.0)
            path, cookies = auto_discover_firefox_cookies()
            if cookies and cookies.secure_1psid:
                return await auto_detect_web_account(
                    WebAutoDetectRequest(
                        account_id=req.account_id,
                        label=req.label,
                        max_concurrency=req.max_concurrency,
                        auto_link=True,
                    )
                )

    return {
        "success": True,
        "logged_in": False,
        "login_url": login_url,
        "message": "Opened https://gemini.google.com in browser. Sign in and then click Auto-Detect.",
    }


@router.delete("/web-accounts/{account_id}")
async def delete_web_account(account_id: str) -> dict[str, Any]:
    """Delete a Web account, remove its isolated cookies file, and update snapshot."""
    validated_id = _validate_account_id(account_id)
    if state_registry.get_web_account(validated_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Web account '{validated_id}' not found.")

    state_registry.remove_web_account(validated_id)
    try:
        from core.auth_web import default_web_session_manager
        await default_web_session_manager.close_client(validated_id)
    except Exception:
        pass
    try:
        from config.settings import get_lmdb_store
        get_lmdb_store().delete_sessions_for_account(validated_id)
    except Exception:
        pass

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
# 3. Dynamic Model Routing & Compute Allocation
# ============================================================================


@router.get("/model-routes")
async def get_model_routes() -> dict[str, Any]:
    """Return all discovered models and custom alias routes with effective channel preferences."""
    custom_routes = get_custom_model_routes()
    base_models = unified_model_registry.get_base_models()

    routes: list[dict[str, Any]] = []
    seen_models: set[str] = set()

    # 1. Models dynamically discovered by UnifiedModelRegistry
    for model_id in base_models:
        clean_id = model_id.strip().lower()
        if clean_id in seen_models:
            continue
        seen_models.add(clean_id)
        primary, failover = get_model_channel_preference(clean_id)
        is_custom = clean_id in custom_routes
        support = unified_model_registry.get_supporting_accounts(clean_id, active_only=False)
        oauth_count = len(support.get("oauth", []))
        web_count = len(support.get("web", []))
        routes.append({
            "model": clean_id,
            "primary_channel": primary,
            "failover_channel": failover,
            "primary": primary,
            "failover": failover,
            "is_custom": is_custom,
            "is_discovered": (oauth_count > 0 or web_count > 0),
            "supporting_accounts": {
                "oauth": oauth_count,
                "web": web_count,
            },
        })

    # 2. Custom aliases not included in base_models (e.g. main_agent, sub_agent, gpt-4o)
    for alias_name, alias_route in custom_routes.items():
        clean_alias = alias_name.strip().lower()
        if clean_alias in seen_models:
            continue
        seen_models.add(clean_alias)
        primary, failover = get_model_channel_preference(clean_alias)
        support = unified_model_registry.get_supporting_accounts(clean_alias, active_only=False)
        oauth_count = len(support.get("oauth", []))
        web_count = len(support.get("web", []))
        routes.append({
            "model": clean_alias,
            "primary_channel": primary,
            "failover_channel": failover,
            "primary": primary,
            "failover": failover,
            "is_custom": True,
            "is_discovered": (oauth_count > 0 or web_count > 0),
            "supporting_accounts": {
                "oauth": oauth_count,
                "web": web_count,
            },
        })

    return {
        "success": True,
        "routes": routes,
        "custom_routes": custom_routes,
        "default_routes": DEFAULT_MODEL_ROUTES,
    }


@router.post("/model-routes")
async def update_model_route(payload: ModelRouteUpdateRequest) -> dict[str, Any]:
    """Create or update a model routing configuration."""
    model_name = payload.model.strip().lower()
    if not model_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model identifier cannot be empty.",
        )

    primary = payload.primary_channel
    failover_raw = payload.failover_channel
    failover: Literal["oauth", "web"] | None = None
    if failover_raw is not None:
        f_str = str(failover_raw).strip().lower()
        if f_str in ("oauth", "web"):
            failover = f_str  # type: ignore[assignment]

    if failover == primary:
        failover = None

    custom_routes = get_custom_model_routes()
    custom_routes[model_name] = {
        "primary": primary,
        "failover": failover,
        "primary_channel": primary,
        "failover_channel": failover,
    }
    save_custom_model_routes(custom_routes)

    return {
        "success": True,
        "message": f"Route for model '{model_name}' updated successfully.",
        "route": {
            "model": model_name,
            "primary_channel": primary,
            "failover_channel": failover,
            "primary": primary,
            "failover": failover,
            "is_custom": True,
        },
        "custom_routes": custom_routes,
    }


@router.delete("/model-routes/{model:path}")
async def delete_model_route(model: str) -> dict[str, Any]:
    """Remove a custom model route override."""
    model_name = model.strip().lower()
    custom_routes = get_custom_model_routes()

    target_key = None
    if model_name in custom_routes:
        target_key = model_name
    else:
        for k in custom_routes:
            if k.lower() == model_name:
                target_key = k
                break

    if target_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom route for model '{model}' not found.",
        )

    custom_routes.pop(target_key, None)
    save_custom_model_routes(custom_routes)

    eff_primary, eff_failover = get_model_channel_preference(model_name)
    return {
        "success": True,
        "message": f"Custom route for model '{model_name}' deleted successfully.",
        "model": model_name,
        "effective_primary_channel": eff_primary,
        "effective_failover_channel": eff_failover,
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
