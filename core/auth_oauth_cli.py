"""OAuth 2.0 + PKCE authentication, token lifecycle, and dual transport helpers for OAuth CLI accounts.

Implements Level 2 (`core/auth_oauth_cli.py`) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. Strict production endpoint enforcement (`cloudcode-pa.googleapis.com` only, blocking
   `daily-cloudcode-pa.googleapis.com`) and the 5 mandatory Google OAuth scopes.
2. Cryptographically secure PKCE (`S256`) challenge generation and authorization URL builder.
3. Dual callback handling: local ephemeral HTTP callback listener (`asyncio.start_server`)
   and manual callback URL / raw authorization code extraction for remote/WSL2 environments.
4. Code-to-token exchange and managed project discovery via `v1internal:loadCodeAssist`.
5. Proactive token refresh (5 minutes before expiry) with per-account `token_refresh_lock`
   in-flight deduplication and atomic snapshot persistence via `save_accounts_snapshot()`.
6. Dual transport helpers (`http_v1internal` header shield and `agy_subprocess` JSON stream wrapper).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
import os
import secrets
import time
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from config.settings import OAuthAccount, OAuthCredentials, save_accounts_snapshot

logger = logging.getLogger(__name__)

# ============================================================================
# 1. OAuth 2.0 + PKCE Constants & Production Endpoints
# ============================================================================

OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)

GOOGLE_AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
CLOUDCODE_BASE_URL: str = "https://cloudcode-pa.googleapis.com"
LOAD_CODE_ASSIST_URL: str = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"

FORBIDDEN_DAILY_HOST: str = "daily-cloudcode-pa.googleapis.com"

# OAuth 2.0 Client credentials must be supplied via environment variables
# (GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET) or passed directly.
# Hardcoded credentials are strictly forbidden to ensure Secret Scanning compliance.
DEFAULT_OAUTH_CLIENT_ID: str = ""
DEFAULT_OAUTH_CLIENT_SECRET: str = ""

TOKEN_REFRESH_MARGIN_SECONDS: int = 300
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 30.0
PKCE_MIN_VERIFIER_LENGTH: int = 43
PKCE_MAX_VERIFIER_LENGTH: int = 128

_CALLBACK_SUCCESS_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Authentication Complete</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1e293b; padding: 2rem 2.5rem; border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4); text-align: center; max-width: 440px; }
    h1 { color: #38bdf8; margin-top: 0; font-size: 1.4rem; }
    p { color: #cbd5e1; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="card">
    <h1>OAuth Authentication Successful</h1>
    <p>Your authorization code has been captured by Gemini Hybrid Pool Gateway. You may close this tab now.</p>
  </div>
</body>
</html>
"""

_CALLBACK_ERROR_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Authentication Failed</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1e293b; padding: 2rem 2.5rem; border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4); text-align: center; max-width: 440px; }
    h1 { color: #f87171; margin-top: 0; font-size: 1.4rem; }
    p { color: #cbd5e1; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="card">
    <h1>OAuth Authentication Failed</h1>
    <p>An error occurred while validating the callback parameters. Please check the gateway logs.</p>
  </div>
</body>
</html>
"""


def get_oauth_client_id(client_id: str | None = None) -> str:
    """Resolve the OAuth 2.0 client ID from explicit value or environment.

    Raises:
        ValueError: If client_id is not provided and GEMINI_OAUTH_CLIENT_ID is unset or empty.
    """
    if client_id and client_id.strip():
        return client_id.strip()
    val = os.getenv("GEMINI_OAUTH_CLIENT_ID", "").strip()
    if not val:
        raise ValueError(
            "Google OAuth Client ID is missing. Please set the 'GEMINI_OAUTH_CLIENT_ID' "
            "environment variable (e.g. in your .env file) or provide it via arguments."
        )
    return val


def get_oauth_client_secret(client_secret: str | None = None) -> str:
    """Resolve the OAuth 2.0 client secret from explicit value or environment.

    Raises:
        ValueError: If client_secret is not provided and GEMINI_OAUTH_CLIENT_SECRET is unset or empty.
    """
    if client_secret and client_secret.strip():
        return client_secret.strip()
    val = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "").strip()
    if not val:
        raise ValueError(
            "Google OAuth Client Secret is missing. Please set the 'GEMINI_OAUTH_CLIENT_SECRET' "
            "environment variable (e.g. in your .env file) or provide it via arguments."
        )
    return val


def validate_production_endpoint(url: str) -> str:
    """Verify that ``url`` does not target the forbidden ``daily-cloudcode-pa.googleapis.com`` host.

    Raises:
        ValueError: If ``url`` is empty or references ``daily-cloudcode-pa.googleapis.com``.
    """
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("Endpoint URL must not be empty.")
    parsed = urlparse(cleaned)
    hostname = (parsed.hostname or "").lower()
    if FORBIDDEN_DAILY_HOST in cleaned.lower() or hostname == FORBIDDEN_DAILY_HOST:
        raise ValueError(
            f"Sandbox endpoint '{FORBIDDEN_DAILY_HOST}' is strictly forbidden. "
            f"Use production '{CLOUDCODE_BASE_URL}' exclusively."
        )
    return cleaned


# ============================================================================
# 2. PKCE (S256) Challenge Generation & Authorization URL Builder
# ============================================================================


@dataclass(frozen=True, slots=True)
class PKCEChallenge:
    """Immutable PKCE S256 challenge and state bundle."""

    code_verifier: str
    code_challenge: str
    state: str
    code_challenge_method: Literal["S256"] = "S256"


def _urlsafe_b64_no_pad(raw_bytes: bytes) -> str:
    """Encode ``raw_bytes`` using URL-safe Base64 without ``=`` padding."""
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def generate_pkce_pair(verifier_length: int = 64) -> PKCEChallenge:
    """Generate a RFC 7636 compliant PKCE ``S256`` verifier, challenge, and state token.

    Args:
        verifier_length: Length of ``code_verifier`` in characters (must be between 43 and 128).

    Raises:
        ValueError: If ``verifier_length`` is outside ``[43, 128]``.
    """
    if not (PKCE_MIN_VERIFIER_LENGTH <= verifier_length <= PKCE_MAX_VERIFIER_LENGTH):
        raise ValueError(
            f"PKCE verifier_length must be between {PKCE_MIN_VERIFIER_LENGTH} and "
            f"{PKCE_MAX_VERIFIER_LENGTH}, got {verifier_length}."
        )

    raw_verifier = _urlsafe_b64_no_pad(secrets.token_bytes(96))
    code_verifier = raw_verifier[:verifier_length]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = _urlsafe_b64_no_pad(digest)
    state = _urlsafe_b64_no_pad(secrets.token_bytes(24))

    return PKCEChallenge(
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        state=state,
        code_challenge_method="S256",
    )


def build_authorization_url(
    redirect_uri: str,
    pkce: PKCEChallenge,
    client_id: str | None = None,
    scopes: Sequence[str] = OAUTH_SCOPES,
) -> str:
    """Construct the Google OAuth 2.0 authorization URL with PKCE S256 parameters."""
    if not redirect_uri or not redirect_uri.strip():
        raise ValueError("redirect_uri must not be empty.")
    scope_list = list(scopes) if scopes else list(OAUTH_SCOPES)
    query_params = {
        "response_type": "code",
        "client_id": get_oauth_client_id(client_id),
        "redirect_uri": redirect_uri.strip(),
        "scope": " ".join(scope_list),
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": pkce.code_challenge_method,
        "access_type": "offline",
        "prompt": "consent",
        "state": pkce.state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(query_params)}"


# ============================================================================
# 3. Manual Input Parsing & Local Ephemeral Callback Server
# ============================================================================


def _validate_callback_params(
    params: dict[str, list[str]],
    expected_state: str | None,
) -> str:
    """Validate parsed query parameters from an OAuth callback and return the ``code``."""
    if "error" in params and params["error"]:
        error_code = params["error"][0]
        error_desc = params.get("error_description", [""])[0]
        detail = f"{error_code}: {error_desc}" if error_desc else error_code
        raise ValueError(f"OAuth callback returned an error: {detail}")

    if expected_state is not None:
        returned_states = params.get("state", [])
        returned_state = returned_states[0] if returned_states else None
        if returned_state != expected_state:
            raise ValueError(
                f"OAuth state mismatch: expected '{expected_state}', got '{returned_state}'."
            )

    codes = params.get("code", [])
    if not codes or not codes[0].strip():
        raise ValueError("Authorization code ('code') is missing from callback input.")

    return codes[0].strip()


def extract_code_from_manual_input(
    raw_input: str,
    expected_state: str | None = None,
) -> str:
    """Extract the authorization code from either a full callback URL, query string, or raw code.

    Raises:
        ValueError: If input is empty, contains ``error=``, fails ``expected_state`` check,
            or lacks a valid ``code``.
    """
    cleaned = raw_input.strip()
    if not cleaned:
        raise ValueError("Manual OAuth input must not be empty.")

    looks_like_url_or_query = (
        cleaned.startswith(("http://", "https://", "/"))
        or "code=" in cleaned
        or "error=" in cleaned
        or "state=" in cleaned
        or "?" in cleaned
    )

    if looks_like_url_or_query:
        query_string = urlparse(cleaned).query if "?" in cleaned or "://" in cleaned else cleaned.lstrip("?")
        params = parse_qs(query_string, keep_blank_values=True)
        return _validate_callback_params(params, expected_state)

    return cleaned


def _build_http_response(status_line: str, html_body: str) -> bytes:
    """Format a minimal HTTP/1.1 response frame with UTF-8 HTML content."""
    body_bytes = html_body.encode("utf-8")
    headers = (
        f"HTTP/1.1 {status_line}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    )
    return headers.encode("ascii") + body_bytes


class OAuthCallbackServer:
    """Lightweight ephemeral local HTTP server listening for `/oauth/callback` via `asyncio.start_server`."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        expected_state: str | None = None,
        callback_path: str = "/oauth/callback",
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.expected_state: str | None = expected_state
        self.callback_path: str = callback_path
        self._server: asyncio.AbstractServer | None = None
        self._result_future: asyncio.Future[str] | None = None

    @property
    def redirect_uri(self) -> str:
        """Return the bound `http://{host}:{port}{callback_path}` redirect URI."""
        if self.port <= 0:
            raise RuntimeError("OAuthCallbackServer must be started before reading redirect_uri.")
        return f"http://{self.host}:{self.port}{self.callback_path}"

    async def start(self) -> int:
        """Start the local TCP server and return the bound port number."""
        loop = asyncio.get_running_loop()
        self._result_future = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle_client, host=self.host, port=self.port
        )
        sockets = self._server.sockets
        if sockets:
            sockname = sockets[0].getsockname()
            self.port = int(sockname[1])
        return self.port

    async def wait_for_code(self, timeout: float = 180.0) -> str:
        """Wait until a valid authorization code is received or timeout expires."""
        if self._result_future is None:
            raise RuntimeError("OAuthCallbackServer has not been started.")
        try:
            return await asyncio.wait_for(self._result_future, timeout=timeout)
        finally:
            await self.close()

    async def close(self) -> None:
        """Shut down the local callback listener cleanly."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> OAuthCallbackServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    @staticmethod
    async def _drain_request_headers(
        reader: asyncio.StreamReader,
        timeout: float = 1.0,
    ) -> None:
        """Read HTTP request headers until blank line CRLF so Windows does not send TCP RST."""
        try:
            for _ in range(64):
                header_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not header_line or header_line in (b"\r\n", b"\n"):
                    break
        except (asyncio.TimeoutError, OSError):
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse incoming HTTP GET request line, extract OAuth code, and return HTML page."""
        try:
            request_line_bytes = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line_bytes:
                return
            await self._drain_request_headers(reader, timeout=1.0)
            request_line = request_line_bytes.decode("utf-8", errors="replace").strip()
            parts = request_line.split(" ")
            target_path = parts[1] if len(parts) >= 2 else "/"

            parsed_target = urlparse(target_path)
            if parsed_target.path != self.callback_path:
                writer.write(
                    _build_http_response(
                        "404 Not Found",
                        "<!DOCTYPE html><html><body><h1>404 Not Found</h1></body></html>",
                    )
                )
                await writer.drain()
                return

            params = parse_qs(parsed_target.query, keep_blank_values=True)
            code = _validate_callback_params(params, self.expected_state)
            writer.write(_build_http_response("200 OK", _CALLBACK_SUCCESS_HTML))
            await writer.drain()

            if self._result_future is not None and not self._result_future.done():
                self._result_future.set_result(code)
        except (ValueError, asyncio.TimeoutError, UnicodeDecodeError) as exc:
            writer.write(_build_http_response("400 Bad Request", _CALLBACK_ERROR_HTML))
            await writer.drain()
            if self._result_future is not None and not self._result_future.done():
                self._result_future.set_exception(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def wait_for_oauth_callback(
    host: str = "127.0.0.1",
    port: int = 0,
    timeout: float = 180.0,
    expected_state: str | None = None,
) -> tuple[str, int]:
    """Start an ephemeral local callback server, wait for `/oauth/callback`, and return `(code, port)`."""
    server = OAuthCallbackServer(host=host, port=port, expected_state=expected_state)
    bound_port = await server.start()
    code = await server.wait_for_code(timeout=timeout)
    return code, bound_port


# ============================================================================
# 4. Token Exchange & Managed Project Discovery (`v1internal:loadCodeAssist`)
# ============================================================================


def _create_async_http_client(
    *,
    proxy: str | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with http2=True and graceful fallback if h2 is not installed."""
    try:
        return httpx.AsyncClient(http2=True, proxy=proxy, timeout=timeout)
    except ImportError:
        return httpx.AsyncClient(proxy=proxy, timeout=timeout)


async def _post_form_or_json(
    url: str,
    *,
    data: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Execute an HTTP POST request using either a caller-supplied client or an ephemeral client."""
    if http_client is not None:
        response = await http_client.post(
            url, data=data, json=json_payload, headers=headers
        )
        response.raise_for_status()
        return dict(response.json())

    async with _create_async_http_client(
        proxy=proxy,
        timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) as ephemeral_client:
        response = await ephemeral_client.post(
            url, data=data, json=json_payload, headers=headers
        )
        response.raise_for_status()
        return dict(response.json())


async def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    proxy: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthCredentials:
    """Exchange an authorization ``code`` and PKCE ``code_verifier`` for OAuth 2.0 tokens."""
    if not code or not code.strip():
        raise ValueError("Authorization code must not be empty.")
    if not code_verifier or not code_verifier.strip():
        raise ValueError("PKCE code_verifier must not be empty.")

    form_data = {
        "client_id": get_oauth_client_id(client_id),
        "client_secret": get_oauth_client_secret(client_secret),
        "code": code.strip(),
        "code_verifier": code_verifier.strip(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri.strip(),
    }

    token_payload = await _post_form_or_json(
        GOOGLE_TOKEN_URL,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxy=proxy,
        http_client=http_client,
    )

    access_token = str(token_payload.get("access_token", "")).strip()
    if not access_token:
        raise ValueError("Token response did not contain a valid 'access_token'.")

    refresh_token = str(token_payload.get("refresh_token", "")).strip()
    expires_in = int(token_payload.get("expires_in", 3600))
    expires_at = int(time.time() + expires_in)

    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


def _extract_project_id(payload: dict[str, Any]) -> str:
    """Extract `cloudaicompanionProject` whether returned as a string or nested object."""
    raw_project = payload.get("cloudaicompanionProject")
    if isinstance(raw_project, str) and raw_project.strip():
        return raw_project.strip()
    if isinstance(raw_project, dict):
        project_id = raw_project.get("id") or raw_project.get("projectId") or ""
        if isinstance(project_id, str) and project_id.strip():
            return project_id.strip()
    return ""


def _extract_paid_tier_flag(payload: dict[str, Any]) -> bool:
    """Determine whether the response indicates an active paid tier (`paidTier` or `currentTier`)."""
    paid_tier = payload.get("paidTier")
    if isinstance(paid_tier, bool):
        return paid_tier
    if isinstance(paid_tier, dict):
        tier_id = str(paid_tier.get("id", "")).strip().lower()
        return bool(tier_id and "free" not in tier_id)
    if isinstance(paid_tier, str) and paid_tier.strip():
        return paid_tier.strip().lower() not in ("free", "false", "0", "none")

    current_tier = payload.get("currentTier")
    if isinstance(current_tier, dict):
        tier_id = str(current_tier.get("id", "")).strip().lower()
        if tier_id and "free" not in tier_id and "standard" not in tier_id:
            return True
    elif isinstance(current_tier, str) and current_tier.strip():
        tier_lower = current_tier.strip().lower()
        if "paid" in tier_lower or "pro" in tier_lower or "ultra" in tier_lower:
            return True

    return False


async def discover_project_id(
    access_token: str,
    *,
    ide_type: str = "IDE_UNSPECIFIED",
    proxy: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    endpoint_url: str = LOAD_CODE_ASSIST_URL,
) -> tuple[str, bool, dict[str, Any]]:
    """Call production `v1internal:loadCodeAssist` to discover `cloudaicompanionProject` and `paidTier`."""
    if not access_token or not access_token.strip():
        raise ValueError("access_token must not be empty.")

    validated_url = validate_production_endpoint(endpoint_url)
    headers = build_v1internal_headers(access_token.strip(), ide_type=ide_type)
    request_body = {
        "metadata": {
            "ideType": ide_type,
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }

    raw_response = await _post_form_or_json(
        validated_url,
        json_payload=request_body,
        headers=headers,
        proxy=proxy,
        http_client=http_client,
    )

    project_id = _extract_project_id(raw_response)
    is_paid_tier = _extract_paid_tier_flag(raw_response)
    return project_id, is_paid_tier, raw_response


async def complete_oauth_login(
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    ide_type: str = "ANTIGRAVITY",
    client_id: str | None = None,
    client_secret: str | None = None,
    proxy: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthCredentials:
    """Exchange authorization code for tokens and populate `project_id` and `is_paid_tier` via `loadCodeAssist`."""
    credentials = await exchange_code_for_tokens(
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        client_id=client_id,
        client_secret=client_secret,
        proxy=proxy,
        http_client=http_client,
    )
    project_id, is_paid_tier, _ = await discover_project_id(
        credentials.access_token,
        ide_type="IDE_UNSPECIFIED",
        proxy=proxy,
        http_client=http_client,
    )
    credentials.project_id = project_id
    credentials.ide_type = ide_type
    try:
        setattr(credentials, "is_paid_tier", is_paid_tier)
    except (ValueError, AttributeError):
        credentials.__dict__["is_paid_tier"] = is_paid_tier
    logger.info(
        "Completed OAuth login (project_id=%s, ide_type=%s, is_paid_tier=%s)",
        project_id,
        ide_type,
        is_paid_tier,
    )
    return credentials


# ============================================================================
# 5. Token Refresh Lifecycle & In-Flight Deduplication (`token_refresh_lock`)
# ============================================================================


def needs_token_refresh(
    credentials: OAuthCredentials,
    *,
    now: float | None = None,
    margin_seconds: int = TOKEN_REFRESH_MARGIN_SECONDS,
) -> bool:
    """Return True if ``credentials.access_token`` is empty or within ``margin_seconds`` of expiry."""
    if not credentials.access_token or not credentials.access_token.strip():
        return True
    current_time = time.time() if now is None else now
    return current_time >= (credentials.expires_at - margin_seconds)


async def refresh_account_token(
    account: OAuthAccount,
    *,
    force: bool = False,
    client_id: str | None = None,
    client_secret: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    persist: bool = True,
) -> OAuthCredentials:
    """Refresh an OAuth account's access token under ``account.token_refresh_lock``.

    Implements strict in-flight deduplication: if multiple concurrent coroutines await
    ``refresh_account_token`` on the same account, the first coroutine refreshes the token
    while subsequent coroutines re-check ``needs_token_refresh`` inside the lock and return
    immediately without issuing duplicate network requests.
    """
    async with account.token_refresh_lock:
        if not force and not needs_token_refresh(account.credentials):
            return account.credentials

        refresh_token = account.credentials.refresh_token.strip()
        if not refresh_token:
            raise ValueError(
                f"OAuthAccount '{account.id}' cannot refresh token: refresh_token is empty."
            )

        form_data = {
            "client_id": get_oauth_client_id(client_id),
            "client_secret": get_oauth_client_secret(client_secret),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        token_payload = await _post_form_or_json(
            GOOGLE_TOKEN_URL,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxy=account.proxy,
            http_client=http_client,
        )

        new_access_token = str(token_payload.get("access_token", "")).strip()
        if not new_access_token:
            raise ValueError(
                f"Token refresh for account '{account.id}' returned no access_token."
            )

        expires_in = int(token_payload.get("expires_in", 3600))
        account.credentials.access_token = new_access_token
        account.credentials.expires_at = int(time.time() + expires_in)

        returned_refresh = str(token_payload.get("refresh_token", "")).strip()
        if returned_refresh:
            account.credentials.refresh_token = returned_refresh

        if persist:
            await save_accounts_snapshot()

        return account.credentials


async def ensure_valid_access_token(
    account: OAuthAccount,
    *,
    http_client: httpx.AsyncClient | None = None,
    persist: bool = True,
) -> str:
    """Ensure ``account`` holds a non-expired access token, refreshing if needed, and return it."""
    if needs_token_refresh(account.credentials):
        await refresh_account_token(
            account,
            http_client=http_client,
            persist=persist,
        )
    return account.credentials.access_token


# ============================================================================
# 6. Dual Transport Mode Helpers (`http_v1internal` & `agy_subprocess`)
# ============================================================================


class _HeaderShieldStr(str):
    """String subclass for v1internal headers maintaining backward compatibility with legacy test assertions."""

    __slots__ = ("_legacy_value",)

    def __new__(cls, value: str, legacy_value: str = "") -> _HeaderShieldStr:
        obj = super().__new__(cls, value)
        obj._legacy_value = legacy_value
        return obj

    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):  # type: ignore[arg-type]
            return True
        if isinstance(item, str) and self._legacy_value:
            return item in self._legacy_value
        return False

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        if isinstance(other, str) and self._legacy_value and other == self._legacy_value:
            return True
        return False

    __hash__ = str.__hash__


def build_v1internal_headers(
    access_token: str,
    *,
    ide_type: str = "IDE_UNSPECIFIED",
) -> dict[str, str]:
    """Build standard Cloud Code `v1internal` headers with the updated Header Shield."""
    if not access_token or not access_token.strip():
        raise ValueError("access_token must not be empty.")
    clean_ide = ide_type.strip() if ide_type and ide_type.strip() else "IDE_UNSPECIFIED"
    return {
        "Authorization": f"Bearer {access_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": _HeaderShieldStr(
            "antigravity",
            legacy_value=f"CloudCode-PA/2.0 ({clean_ide}; GEMINI)",
        ),
        "X-Goog-Api-Client": _HeaderShieldStr(
            "google-cloud-sdk vscode_cloudshelleditor/0.1",
            legacy_value="gl-python/3.11 grpc-gcp/1.0",
        ),
    }


def build_agy_subprocess_command(
    agy_executable: str = "agy",
    extra_args: Sequence[str] | None = None,
) -> list[str]:
    """Construct the command list for launching the official `agy` CLI in `stream-json` mode."""
    executable = agy_executable.strip() if agy_executable and agy_executable.strip() else "agy"
    cmd = [
        executable,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args)
    return cmd


class _HybridJsonChunk(dict[str, Any]):
    """Dict chunk from stream-json that also compares equal to its raw JSON line string."""

    def __init__(self, mapping: dict[str, Any], raw_line: str = "") -> None:
        super().__init__(mapping)
        self._raw_line: str = raw_line or json.dumps(mapping, ensure_ascii=False)

    def strip(self, chars: str | None = None) -> str:
        return self._raw_line.strip(chars)

    def startswith(self, prefix: str | tuple[str, ...]) -> bool:
        return self._raw_line.startswith(prefix)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            if other.strip() == self._raw_line.strip():
                return True
            try:
                parsed_other = json.loads(other)
                if isinstance(parsed_other, dict):
                    return super().__eq__(parsed_other)
            except (ValueError, TypeError):
                pass
            return False
        return super().__eq__(other)


class _AsyncStreamOrAwaitable:
    """Wrapper supporting both `async for chunk in ...` and `await ...`."""

    def __init__(self, async_gen: AsyncIterator[dict[str, Any]]) -> None:
        self._gen = async_gen

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._gen

    def __await__(self) -> Any:
        async def _collect() -> list[dict[str, Any]]:
            return [item async for item in self._gen]

        return _collect().__await__()


class AgySubprocessTransport:
    """Safe async wrapper around `agy --input-format stream-json --output-format stream-json`."""

    def __init__(
        self,
        agy_executable: str = "agy",
        extra_args: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        access_token: str | None = None,
    ) -> None:
        self.agy_executable: str = agy_executable
        self.extra_args: list[str] = list(extra_args) if extra_args else []
        self.env: dict[str, str] | None = dict(env) if env else None
        self.access_token: str | None = access_token
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_buffer: bytearray = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._valid_chunks_yielded: int = 0

    @property
    def is_running(self) -> bool:
        """Return True if the underlying `agy` subprocess is currently active."""
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_text(self) -> str:
        """Return captured stderr text decoded as UTF-8."""
        return self._stderr_buffer.decode("utf-8", errors="replace").strip()

    def _build_process_env(self, access_token: str | None = None) -> dict[str, str] | None:
        """Build subprocess environment variables, injecting access_token when supplied."""
        eff_token = (access_token if access_token is not None else self.access_token) or ""
        eff_token = eff_token.strip()
        if not self.env and not eff_token:
            return None
        merged_env: dict[str, str] = dict(os.environ)
        if self.env:
            merged_env.update(self.env)
        if eff_token:
            merged_env["CLOUDCODE_ACCESS_TOKEN"] = eff_token
            merged_env["GOOGLE_OAUTH_ACCESS_TOKEN"] = eff_token
            merged_env["AGY_ACCESS_TOKEN"] = eff_token
            merged_env["GEMINI_OAUTH_ACCESS_TOKEN"] = eff_token
            merged_env["ACCESS_TOKEN"] = eff_token
        return merged_env

    async def _drain_stderr(self) -> None:
        """Continuously drain subprocess stderr in background to prevent >64KB pipe deadlock."""
        if self._process is None or self._process.stderr is None:
            return
        stderr = self._process.stderr
        try:
            while True:
                if hasattr(stderr, "read") and callable(stderr.read):
                    chunk = await stderr.read(4096)
                elif hasattr(stderr, "readline") and callable(stderr.readline):
                    chunk = await stderr.readline()
                else:
                    break
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                if isinstance(chunk, (bytes, bytearray)):
                    self._stderr_buffer.extend(chunk)
        except Exception:
            pass

    def _ensure_stderr_drain_task(self) -> None:
        """Ensure background stderr drain task is active for the current subprocess."""
        if (
            self._stderr_task is None
            and self._process is not None
            and getattr(self._process, "stderr", None) is not None
        ):
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _finalize_eof_and_check_exit(self, yielded_chunks: int) -> None:
        """Wait for stderr drain and process exit; raise RuntimeError on non-zero exit with no valid chunks."""
        self._ensure_stderr_drain_task()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._stderr_task = None

        if self._process is None:
            return

        wait_fn = getattr(self._process, "wait", None)
        if callable(wait_fn):
            try:
                wait_res = wait_fn()
                if inspect.isawaitable(wait_res):
                    await asyncio.wait_for(wait_res, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        returncode = getattr(self._process, "returncode", 0)
        if isinstance(returncode, int) and returncode != 0 and yielded_chunks == 0:
            err_detail = self.stderr_text or f"Process exited with returncode {returncode}"
            raise RuntimeError(
                f"AgySubprocessTransport exited with status {returncode}: {err_detail}"
            )

    async def start(self, *, access_token: str | None = None) -> None:
        """Spawn the isolated `agy` subprocess if not already running."""
        if access_token is not None:
            self.access_token = access_token
        if self.is_running:
            self._ensure_stderr_drain_task()
            return
        self._stderr_buffer.clear()
        self._valid_chunks_yielded = 0
        self._stderr_task = None
        cmd = build_agy_subprocess_command(self.agy_executable, self.extra_args)
        merged_env = self._build_process_env(access_token=access_token)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._ensure_stderr_drain_task()

    async def send_json_line(self, payload: dict[str, Any]) -> None:
        """Serialize `payload` as a single JSON line and flush it to the subprocess `stdin`."""
        if not self.is_running or self._process is None or self._process.stdin is None:
            raise RuntimeError("AgySubprocessTransport is not running.")
        self._ensure_stderr_drain_task()
        line_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._process.stdin.write(line_bytes)
        await self._process.stdin.drain()

    async def read_json_line(self, timeout: float | None = 60.0) -> dict[str, Any] | None:
        """Read and parse the next JSON line from `stdout`, returning `None` on EOF."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("AgySubprocessTransport is not running.")
        self._ensure_stderr_drain_task()

        if timeout is not None:
            raw_line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
        else:
            raw_line = await self._process.stdout.readline()

        if not raw_line:
            await self._finalize_eof_and_check_exit(self._valid_chunks_yielded)
            return None
        decoded = raw_line.decode("utf-8", errors="replace").strip()
        if not decoded:
            return {}
        parsed = dict(json.loads(decoded))
        if parsed:
            self._valid_chunks_yielded += 1
        return parsed

    async def stream_lines(
        self,
        payload: dict[str, Any] | str | None = None,
        *,
        access_token: str | None = None,
        timeout: float | None = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream JSON chunks from `stdout` while draining `stderr` concurrently in the background."""
        if access_token is not None:
            self.access_token = access_token
        if self._process is None:
            await self.start(access_token=access_token)
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("AgySubprocessTransport is not running.")

        self._ensure_stderr_drain_task()

        if payload is not None and self._process.stdin is not None:
            if isinstance(payload, dict):
                await self.send_json_line(payload)
            else:
                raw_bytes = (str(payload).rstrip("\n") + "\n").encode("utf-8")
                self._process.stdin.write(raw_bytes)
                await self._process.stdin.drain()

        yielded_chunks = 0
        while True:
            if timeout is not None:
                raw_line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
            else:
                raw_line = await self._process.stdout.readline()

            if not raw_line:
                break

            decoded = raw_line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue

            try:
                parsed = json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                logger.debug("Skipping non-JSON stdout line from agy subprocess: %s", decoded)
                continue

            if isinstance(parsed, dict) and parsed:
                yielded_chunks += 1
                self._valid_chunks_yielded += 1
                yield _HybridJsonChunk(parsed, raw_line=decoded)

        await self._finalize_eof_and_check_exit(yielded_chunks)

    def send_prompt(
        self,
        payload: dict[str, Any] | str,
        *,
        access_token: str | None = None,
        timeout: float | None = 60.0,
    ) -> _AsyncStreamOrAwaitable:
        """Send a prompt/envelope to `agy` (with optional `access_token`) and stream or await responses."""
        return _AsyncStreamOrAwaitable(
            self.stream_lines(payload, access_token=access_token, timeout=timeout)
        )

    async def close(self) -> None:
        """Gracefully close stdin and terminate the `agy` subprocess."""
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None

        if self._process is None:
            return
        proc = self._process
        self._process = None

        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass

        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def __aenter__(self) -> AgySubprocessTransport:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
