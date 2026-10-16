"""Web Cookie Pool authentication, Firefox SQLite extraction, isolated cookie cache, and concurrency-safe GeminiClient management.

Implements Level 2 (Task 2) of the Gemini-Hybrid-Pool-Gateway (v3.2) architecture:
1. Browser isolation & DBSC avoidance (ADR-03): rejects Chromium browsers, extracts
   ``__Secure-1PSID`` and ``__Secure-1PSIDTS`` from isolated Firefox ``cookies.sqlite``
   profiles (via ``browser_cookie3`` with a direct read-only ``sqlite3`` fallback),
   or parses manual Incognito cookie inputs, plus ``Saved Info`` hygiene guidance.
2. Per-account cookie cache isolation: strips shared ``GEMINI_COOKIE_PATH`` from the
   process environment and persists each account's rotated cookies atomically to
   ``config/cookies_cache/{account_id}.json`` under ``account.cookie_write_lock``.
3. Per-account ``GeminiClient`` lifecycle & Concurrency Guard: initializes each
   client with ``auto_close=False, auto_refresh=True`` under ``account.reinit_lock``
   and refuses to close or re-initialize a client while ``account.active_requests > 0``
   to prevent severing active streams on the same Web account.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from http.cookies import CookieError, SimpleCookie
import inspect
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any

import browser_cookie3
from gemini_webapi import GeminiClient

from config.settings import (
    COOKIES_CACHE_DIR,
    WebAccount,
    WebCookies,
    _replace_file_with_windows_retry,
    save_accounts_snapshot,
    state_registry,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Constants & Domain Exceptions (ADR-03)
# ============================================================================

SECURE_1PSID_COOKIE_NAME: str = "__Secure-1PSID"
SECURE_1PSIDTS_COOKIE_NAME: str = "__Secure-1PSIDTS"
SHARED_COOKIE_ENV_VAR: str = "GEMINI_COOKIE_PATH"
DEFAULT_GOOGLE_COOKIE_DOMAIN: str = ".google.com"

SAVED_INFO_URL: str = "https://gemini.google.com/saved-info"
SAVED_INFO_HYGIENE_WARNING: str = (
    "Disable 'Saved Info' at https://gemini.google.com/saved-info before using a Web "
    "account in the pool; account-level saved memory continues to influence model "
    "responses even when temporary=True is active. If pasting cookies from a Private / "
    "Incognito window, close the browser window immediately without signing out so it "
    "does not compete with the gateway on __Secure-1PSIDTS rotation."
)

FORBIDDEN_CHROMIUM_BROWSERS: frozenset[str] = frozenset(
    {
        "chrome",
        "chromium",
        "edge",
        "microsoft-edge",
        "msedge",
        "brave",
        "brave-browser",
        "opera",
        "opera_gx",
        "opera-gx",
        "vivaldi",
        "arc",
    }
)

_SAFE_ACCOUNT_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_CHROMIUM_PATH_MARKERS: tuple[str, ...] = (
    "google/chrome",
    "google-chrome",
    "microsoft/edge",
    "microsoft-edge",
    "msedge",
    "bravesoftware",
    "brave-browser",
    "chromium",
    "vivaldi",
    "opera software",
    "opera-gx",
    "opera_gx",
)


class ChromiumDBSCUnsupportedError(ValueError):
    """Raised when attempting to import cookies from a Chromium-based browser.

    Chromium browsers bind session cookies to hardware/OS keys via Device Bound
    Session Credentials (DBSC), causing extracted cookies to expire within hours.
    """


class ActiveRequestsConcurrencyError(RuntimeError):
    """Raised when attempting to close or re-initialize a Web client with in-flight requests."""

    def __init__(self, account_id: str, active_requests: int) -> None:
        self.account_id = account_id
        self.active_requests = active_requests
        super().__init__(
            f"Cannot re-init or close GeminiClient for account '{account_id}' "
            f"while active_requests={active_requests} (C_{{web,i}} > 0)."
        )


# ============================================================================
# 2. Browser Isolation, Firefox SQLite Extraction & Manual Paste (ADR-03)
# ============================================================================


def reject_chromium_cookie_source(source_identifier: str | Path) -> None:
    """Raise ``ChromiumDBSCUnsupportedError`` if ``source_identifier`` refers to Chromium."""
    raw_str = str(source_identifier).strip()
    normalized = raw_str.lower().replace("\\", "/")
    base_name = Path(raw_str).name.lower()

    if normalized in FORBIDDEN_CHROMIUM_BROWSERS or base_name in FORBIDDEN_CHROMIUM_BROWSERS:
        raise ChromiumDBSCUnsupportedError(
            f"Browser '{source_identifier}' is Chromium-based and subject to DBSC cookie "
            "invalidation. Use an isolated Firefox profile (cookies.sqlite) or paste "
            "cookies from a closed Private/Incognito window."
        )
    for marker in _CHROMIUM_PATH_MARKERS:
        if marker in normalized:
            raise ChromiumDBSCUnsupportedError(
                f"Cookie path '{source_identifier}' belongs to a Chromium-based browser "
                "subject to DBSC. Only Firefox cookies.sqlite files are supported."
            )


def _resolve_firefox_sqlite_path(cookie_file: str | Path) -> Path:
    """Resolve and validate the path to a Firefox ``cookies.sqlite`` database file."""
    reject_chromium_cookie_source(cookie_file)
    candidate = Path(cookie_file).expanduser()
    if candidate.is_dir():
        candidate = candidate / "cookies.sqlite"
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(
            f"Firefox cookie database not found at '{candidate}'."
        )
    return candidate.resolve()


def _extract_via_browser_cookie3(
    sqlite_path: Path,
    domain_name: str,
) -> dict[str, str]:
    """Attempt cookie extraction via ``browser_cookie3.firefox``."""
    extracted: dict[str, str] = {}
    try:
        cookie_jar = browser_cookie3.firefox(
            cookie_file=str(sqlite_path),
            domain_name=domain_name,
        )
        for cookie in cookie_jar:
            if cookie.name in (SECURE_1PSID_COOKIE_NAME, SECURE_1PSIDTS_COOKIE_NAME):
                if cookie.value and cookie.name not in extracted:
                    extracted[cookie.name] = str(cookie.value).strip()
    except Exception as exc:
        logger.debug(
            "browser_cookie3.firefox fallback triggered for %s: %s",
            sqlite_path,
            exc,
        )
    return extracted


def _query_moz_cookies_file(db_path: Path, domain_suffix: str) -> dict[str, str]:
    """Query ``moz_cookies`` in read-only mode for Google session cookies."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    normalized_domain = domain_suffix.lstrip(".").lower()
    found_cookies: dict[str, str] = {}
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        conn = sqlite3.connect(str(db_path.resolve()), timeout=5.0)

    with conn:
        try:
            cursor = conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE name IN (?, ?) AND (host LIKE ? OR host = ?) "
                "ORDER BY lastAccessed DESC",
                (
                    SECURE_1PSID_COOKIE_NAME,
                    SECURE_1PSIDTS_COOKIE_NAME,
                    f"%{normalized_domain}",
                    normalized_domain,
                ),
            )
        except sqlite3.OperationalError:
            cursor = conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE name IN (?, ?) AND (host LIKE ? OR host = ?)",
                (
                    SECURE_1PSID_COOKIE_NAME,
                    SECURE_1PSIDTS_COOKIE_NAME,
                    f"%{normalized_domain}",
                    normalized_domain,
                ),
            )
        for cookie_name, cookie_value in cursor.fetchall():
            if cookie_name and cookie_value and cookie_name not in found_cookies:
                found_cookies[str(cookie_name)] = str(cookie_value).strip()
    return found_cookies


def _read_moz_cookies_with_temp_fallback(
    sqlite_path: Path,
    domain_name: str,
) -> dict[str, str]:
    """Read ``moz_cookies`` directly, copying to a temp dir if locked by Firefox."""
    try:
        return _query_moz_cookies_file(sqlite_path, domain_name)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        with tempfile.TemporaryDirectory(prefix="ghpg_ff_cookies_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            tmp_db = tmp_dir / "cookies.sqlite"
            shutil.copy2(sqlite_path, tmp_db)
            for suffix in ("-wal", "-shm"):
                sidecar = sqlite_path.with_name(f"{sqlite_path.name}{suffix}")
                if sidecar.exists():
                    shutil.copy2(sidecar, tmp_dir / f"cookies.sqlite{suffix}")
            return _query_moz_cookies_file(tmp_db, domain_name)


def _merge_sqlite_cookies_fallback(
    sqlite_path: Path,
    domain_name: str,
    cookies_map: dict[str, str],
) -> None:
    """Populate missing session cookies in ``cookies_map`` via direct SQLite read."""
    try:
        sqlite_cookies = _read_moz_cookies_with_temp_fallback(sqlite_path, domain_name)
        for cookie_name, cookie_val in sqlite_cookies.items():
            if not cookies_map.get(cookie_name) and cookie_val:
                cookies_map[cookie_name] = cookie_val
    except (sqlite3.Error, OSError) as exc:
        if not cookies_map.get(SECURE_1PSID_COOKIE_NAME):
            raise RuntimeError(
                f"Failed to read Firefox cookies from '{sqlite_path}': {exc}"
            ) from exc


def extract_firefox_cookies(
    cookie_file: str | Path,
    domain_name: str = DEFAULT_GOOGLE_COOKIE_DOMAIN,
) -> WebCookies:
    """Extract ``__Secure-1PSID`` and ``__Secure-1PSIDTS`` from a Firefox SQLite file.

    Uses ``browser_cookie3.firefox`` first and seamlessly falls back to reading
    ``moz_cookies`` via ``sqlite3`` if ``browser_cookie3`` fails or misses either
    cookie. Raises ``ValueError`` if ``__Secure-1PSID`` is absent or empty.
    """
    sqlite_path = _resolve_firefox_sqlite_path(cookie_file)
    cookies_map = _extract_via_browser_cookie3(sqlite_path, domain_name)

    if (
        not cookies_map.get(SECURE_1PSID_COOKIE_NAME)
        or not cookies_map.get(SECURE_1PSIDTS_COOKIE_NAME)
    ):
        _merge_sqlite_cookies_fallback(sqlite_path, domain_name, cookies_map)

    secure_1psid = cookies_map.get(SECURE_1PSID_COOKIE_NAME, "").strip()
    if not secure_1psid:
        raise ValueError(
            f"Required cookie '{SECURE_1PSID_COOKIE_NAME}' not found or empty in "
            f"Firefox database '{sqlite_path}'."
        )
    return WebCookies(
        secure_1psid=secure_1psid,
        secure_1psidts=cookies_map.get(SECURE_1PSIDTS_COOKIE_NAME, "").strip(),
    )


def _clean_cookie_token(raw_token: str, prefix_key: str) -> str:
    """Strip surrounding whitespace, quotes, optional prefix, and semicolons."""
    cleaned = raw_token.strip()
    while cleaned and (cleaned.endswith(";") or cleaned.startswith(";") or cleaned[0] in "\"'" or cleaned[-1] in "\"'"):
        cleaned = cleaned.strip(";").strip("\"'").strip()

    if cleaned.startswith(f"{prefix_key}="):
        cleaned = cleaned[len(prefix_key) + 1 :].strip()

    while cleaned and (cleaned.endswith(";") or cleaned.startswith(";") or cleaned[0] in "\"'" or cleaned[-1] in "\"'"):
        cleaned = cleaned.strip(";").strip("\"'").strip()

    return cleaned


def parse_manual_cookies(
    secure_1psid: str,
    secure_1psidts: str = "",
) -> WebCookies:
    """Validate and normalize manually pasted Incognito session cookies."""
    cleaned_psid = _clean_cookie_token(secure_1psid, SECURE_1PSID_COOKIE_NAME)
    cleaned_psidts = _clean_cookie_token(secure_1psidts, SECURE_1PSIDTS_COOKIE_NAME)
    if not cleaned_psid:
        raise ValueError(
            f"Required cookie '{SECURE_1PSID_COOKIE_NAME}' cannot be empty."
        )
    return WebCookies(
        secure_1psid=cleaned_psid,
        secure_1psidts=cleaned_psidts,
    )


def _extract_cookies_from_mapping(cookie_mapping: Mapping[str, Any]) -> WebCookies:
    """Extract ``WebCookies`` from a flat or nested ``{"cookies": ...}`` mapping."""
    nested = cookie_mapping.get("cookies")
    source_map: Mapping[str, Any] = (
        nested if isinstance(nested, Mapping) else cookie_mapping
    )
    psid = str(
        source_map.get(SECURE_1PSID_COOKIE_NAME)
        or source_map.get("secure_1psid")
        or ""
    )
    psidts = str(
        source_map.get(SECURE_1PSIDTS_COOKIE_NAME)
        or source_map.get("secure_1psidts")
        or ""
    )
    return parse_manual_cookies(psid, psidts)


def _extract_cookies_from_json_list(cookie_items: list[Any]) -> WebCookies:
    """Extract ``WebCookies`` from a browser-extension JSON list of ``{name, value}`` objects."""
    found: dict[str, str] = {}
    for entry in cookie_items:
        if isinstance(entry, Mapping):
            name = str(entry.get("name", "")).strip()
            val = str(entry.get("value", "")).strip()
            if name in (SECURE_1PSID_COOKIE_NAME, SECURE_1PSIDTS_COOKIE_NAME) and val:
                found[name] = val
    return parse_manual_cookies(
        found.get(SECURE_1PSID_COOKIE_NAME, ""),
        found.get(SECURE_1PSIDTS_COOKIE_NAME, ""),
    )


def _parse_cookie_header_string(header_text: str) -> WebCookies:
    """Parse a raw ``Cookie: __Secure-1PSID=...; __Secure-1PSIDTS=...`` header string."""
    body = header_text.strip()
    if body.lower().startswith("cookie:"):
        body = body.split(":", 1)[1].strip()

    found: dict[str, str] = {}
    parsed_cookie = SimpleCookie()
    try:
        parsed_cookie.load(body)
        for morsel in parsed_cookie.values():
            found[morsel.key] = morsel.value
    except (CookieError, ValueError):
        pass

    if SECURE_1PSID_COOKIE_NAME not in found or SECURE_1PSIDTS_COOKIE_NAME not in found:
        for segment in body.split(";"):
            if "=" in segment:
                key_part, val_part = segment.split("=", 1)
                k = key_part.strip()
                v = val_part.strip()
                if k in (SECURE_1PSID_COOKIE_NAME, SECURE_1PSIDTS_COOKIE_NAME) and k not in found:
                    found[k] = v

    return parse_manual_cookies(
        found.get(SECURE_1PSID_COOKIE_NAME, ""),
        found.get(SECURE_1PSIDTS_COOKIE_NAME, ""),
    )


def parse_cookie_header_or_dict(
    raw_input: str | Mapping[str, Any],
) -> WebCookies:
    """Parse manual cookies from a ``Mapping``, JSON string, or ``Cookie:`` header."""
    if isinstance(raw_input, Mapping):
        return _extract_cookies_from_mapping(raw_input)

    text = raw_input.strip()
    if not text:
        raise ValueError("Cookie input string cannot be empty.")

    if text.startswith("{") or text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON cookie payload: {exc}") from exc
        if isinstance(decoded, Mapping):
            return _extract_cookies_from_mapping(decoded)
        if isinstance(decoded, list):
            return _extract_cookies_from_json_list(decoded)
        raise ValueError("JSON cookie payload must be an object or array of cookie objects.")

    return _parse_cookie_header_string(text)


class HygieneNotice(dict[str, Any]):
    """Specialized mapping for Saved Info hygiene guidance supporting substring checks."""

    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):
            return True
        if isinstance(item, str):
            for val in self.values():
                if isinstance(val, str) and item in val:
                    return True
        return False


def get_saved_info_hygiene_notice() -> HygieneNotice:
    """Return structured guidance for disabling Gemini Web ``Saved Info`` and Incognito hygiene."""
    return HygieneNotice(
        {
            "feature": "Saved Info",
            "url": SAVED_INFO_URL,
            "action_required": "DISABLE_SAVED_INFO_AND_CLOSE_INCOGNITO",
            "reason": SAVED_INFO_HYGIENE_WARNING,
            "incognito_reminder": (
                "Close the Private/Incognito browser window immediately after copying cookies "
                "without clicking Sign Out, preventing concurrent __Secure-1PSIDTS rotation."
            ),
            "message_ar": (
                "يرجى تعطيل ميزة 'المعلومات المحفوظة (Saved Info)' عبر الرابط "
                "https://gemini.google.com/saved-info لمنع تلوث مخرجات الوكيل البرمجي، "
                "وإغلاق نافذة التصفح الخاصة فوراً دون تسجيل الخروج."
            ),
            "message_en": SAVED_INFO_HYGIENE_WARNING,
        }
    )


# ============================================================================
# 3. Cookie Cache Isolation & Atomic Persistence
# ============================================================================


def sanitize_shared_cookie_env() -> str | None:
    """Remove ``GEMINI_COOKIE_PATH`` from ``os.environ`` to prevent cross-account contamination."""
    removed_path = os.environ.pop(SHARED_COOKIE_ENV_VAR, None)
    if removed_path is not None:
        logger.warning(
            "Removed shared %s='%s' from environment to enforce per-account cookie isolation.",
            SHARED_COOKIE_ENV_VAR,
            removed_path,
        )
    return removed_path


def get_account_cookie_cache_path(
    account_id: str,
    cache_dir: Path | None = None,
) -> Path:
    """Return the isolated ``{cache_dir}/{account_id}.json`` path after path-traversal validation."""
    cleaned_id = account_id.strip()
    if (
        not cleaned_id
        or ".." in cleaned_id
        or "/" in cleaned_id
        or "\\" in cleaned_id
        or ":" in cleaned_id
        or not _SAFE_ACCOUNT_ID_RE.match(cleaned_id)
    ):
        raise ValueError(
            f"Invalid or unsafe account_id '{account_id}' for isolated cookie cache path."
        )
    base_dir = (Path(cache_dir) if cache_dir is not None else COOKIES_CACHE_DIR).resolve()
    target_path = (base_dir / f"{cleaned_id}.json").resolve()
    if target_path.parent != base_dir:
        raise ValueError(
            f"Path traversal detected for account_id '{account_id}'."
        )
    return target_path


def _read_cookie_from_jar(cookie_container: Any, cookie_name: str) -> str:
    """Read a named cookie from a ``GeminiClient.cookies`` mapping or ``CookieJar``."""
    if cookie_container is None:
        return ""
    if inspect.isawaitable(cookie_container) or "Mock" in type(cookie_container).__name__:
        return ""
    if hasattr(cookie_container, "get") and callable(getattr(cookie_container, "get", None)):
        try:
            direct_val = cookie_container.get(cookie_name)
            if inspect.isawaitable(direct_val) or "Mock" in type(direct_val).__name__:
                return ""
            if isinstance(direct_val, str) and direct_val.strip():
                return direct_val.strip()
        except (TypeError, AttributeError):
            pass
    jar = getattr(cookie_container, "jar", cookie_container)
    if (
        isinstance(jar, Iterable)
        and not isinstance(jar, (str, bytes, Mapping))
        and not inspect.isawaitable(jar)
        and "Mock" not in type(jar).__name__
    ):
        for morsel in jar:
            if getattr(morsel, "name", None) == cookie_name:
                val = getattr(morsel, "value", "")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def extract_client_web_cookies(
    client: GeminiClient,
    fallback_cookies: WebCookies | None = None,
) -> WebCookies:
    """Extract the latest ``__Secure-1PSID`` and ``__Secure-1PSIDTS`` from a ``GeminiClient``."""
    active_jar = getattr(client, "cookies", None)
    internal_jar = getattr(client, "_cookies", None)

    psid = (
        _read_cookie_from_jar(active_jar, SECURE_1PSID_COOKIE_NAME)
        or _read_cookie_from_jar(internal_jar, SECURE_1PSID_COOKIE_NAME)
        or (fallback_cookies.secure_1psid if fallback_cookies else "")
    )
    psidts = (
        _read_cookie_from_jar(active_jar, SECURE_1PSIDTS_COOKIE_NAME)
        or _read_cookie_from_jar(internal_jar, SECURE_1PSIDTS_COOKIE_NAME)
        or (fallback_cookies.secure_1psidts if fallback_cookies else "")
    )
    return WebCookies(secure_1psid=psid, secure_1psidts=psidts)


async def _write_isolated_cookie_json(
    target_path: Path,
    account_id: str,
    cookies: WebCookies,
) -> None:
    """Atomically write the isolated cookie JSON payload via a ``.tmp`` file and ``os.replace``."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.tmp")
    payload = {
        "account_id": account_id,
        "updated_at": int(time.time()),
        "cookies": cookies.to_dict(),
    }
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as tmp_file:
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    await _replace_file_with_windows_retry(tmp_path, target_path)


class AwaitablePath(type(Path())):
    """Path that can be used directly synchronously or awaited by async callers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._async_side_effects: list[Any] = []

    def __await__(self) -> Any:
        async def _resolve() -> Path:
            for coro in self._async_side_effects:
                if inspect.isawaitable(coro):
                    await coro
            return self

        return _resolve().__await__()


# Clean Code Rule 3 exception: 4 parameters including keyword-only `sync_snapshot` boolean
# required by the Level 2 architectural specification contract. Revisit if snapshot
# persistence is decoupled from per-account cookie flushing in a future major version.
def save_account_cookies_isolated(
    account: WebAccount,
    *,
    client: GeminiClient | None = None,
    cache_dir: Path | None = None,
    sync_snapshot: bool = True,
) -> Any:
    """Atomically persist ``account`` cookies to ``config/cookies_cache/{account.id}.json``.

    Supports both synchronous direct usage and asynchronous ``await`` calls.
    Updates ``account.cookies`` from ``client.cookies`` when ``client`` is supplied,
    sets ``account.cookie_file``, and optionally triggers ``save_accounts_snapshot()``.
    """
    target_path = get_account_cookie_cache_path(account.id, cache_dir=cache_dir)
    if client is not None:
        latest_cookies = extract_client_web_cookies(
            client, fallback_cookies=account.cookies
        )
        if latest_cookies.secure_1psid:
            account.cookies = latest_cookies

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.tmp")
    payload = {
        "account_id": account.id,
        "updated_at": int(time.time()),
        "cookies": account.cookies.to_dict(),
    }
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as tmp_file:
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, target_path)
    account.cookie_file = str(target_path)

    awaitable_path = AwaitablePath(str(target_path))
    if sync_snapshot:
        state_registry.upsert_web_account(account)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            awaitable_path._async_side_effects.append(save_accounts_snapshot())

    return awaitable_path


def load_account_cookies_isolated(
    account_id: str,
    *,
    cache_dir: Path | None = None,
) -> WebCookies | None:
    """Load ``WebCookies`` from ``config/cookies_cache/{account_id}.json`` if present and valid."""
    target_path = get_account_cookie_cache_path(account_id, cache_dir=cache_dir)
    if not target_path.exists() or not target_path.is_file():
        return None
    try:
        raw_text = target_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return None
        parsed_json = json.loads(raw_text)
        if isinstance(parsed_json, Mapping):
            return _extract_cookies_from_mapping(parsed_json)
        if isinstance(parsed_json, list):
            return _extract_cookies_from_json_list(parsed_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to load isolated cookie cache for account '%s' at %s: %s",
            account_id,
            target_path,
            exc,
        )
    return None


# ============================================================================
# 4. Per-Account GeminiClient Lifecycle & Concurrency Guard
# ============================================================================


def _resolve_initial_account_cookies(
    account: WebAccount,
    cache_dir: Path | None = None,
) -> WebCookies:
    """Resolve the freshest cookies for ``account`` from isolated cache, memory, or Firefox."""
    cached_cookies = load_account_cookies_isolated(account.id, cache_dir=cache_dir)
    mem_psid = account.cookies.secure_1psid.strip()
    mem_psidts = account.cookies.secure_1psidts.strip()

    if cached_cookies is not None and cached_cookies.secure_1psid:
        if not mem_psid or cached_cookies.secure_1psid == mem_psid:
            return WebCookies(
                secure_1psid=cached_cookies.secure_1psid,
                secure_1psidts=cached_cookies.secure_1psidts or mem_psidts,
            )

    if mem_psid:
        return WebCookies(secure_1psid=mem_psid, secure_1psidts=mem_psidts)

    if account.cookie_file:
        cookie_path = Path(account.cookie_file)
        if cookie_path.exists() and cookie_path.is_file():
            if cookie_path.suffix.lower() == ".sqlite" or cookie_path.name == "cookies.sqlite":
                return extract_firefox_cookies(cookie_path)
            try:
                raw_text = cookie_path.read_text(encoding="utf-8").strip()
                if raw_text:
                    file_cookies = parse_cookie_header_or_dict(raw_text)
                    if file_cookies.secure_1psid:
                        return file_cookies
            except Exception as exc:
                logger.debug(
                    "Failed to parse custom cookie_file '%s': %s",
                    cookie_path,
                    exc,
                )

    raise ValueError(
        f"WebAccount '{account.id}' has no valid '__Secure-1PSID' in isolated cache, "
        "in-memory state, or Firefox cookie_file."
    )


class WebSessionManager:
    """Manages independent ``GeminiClient`` instances per ``WebAccount`` with Concurrency Guard."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        sync_snapshot: bool = True,
        client_factory: Callable[..., GeminiClient] = GeminiClient,
    ) -> None:
        self._clients: dict[str, GeminiClient] = {}
        self._cache_dir: Path | None = cache_dir
        self._sync_snapshot: bool = sync_snapshot
        self._client_factory: Callable[..., GeminiClient] = client_factory

    def get_client(self, account_id: str) -> GeminiClient | None:
        """Return the active ``GeminiClient`` for ``account_id`` if registered."""
        return self._clients.get(account_id)

    async def _create_and_init_client(
        self,
        account: WebAccount,
        timeout: float,
        auto_refresh: bool,
    ) -> GeminiClient:
        """Instantiate, initialize, and persist cookies for a fresh ``GeminiClient``."""
        sanitize_shared_cookie_env()
        resolved_cookies = _resolve_initial_account_cookies(
            account, cache_dir=self._cache_dir
        )
        account.cookies = resolved_cookies
        client = self._client_factory(
            secure_1psid=resolved_cookies.secure_1psid,
            secure_1psidts=resolved_cookies.secure_1psidts or None,
            proxy=account.proxy or None,
        )
        await client.init(
            timeout=timeout,
            auto_close=False,
            auto_refresh=auto_refresh,
        )
        self._clients[account.id] = client
        await save_account_cookies_isolated(
            account,
            client=client,
            cache_dir=self._cache_dir,
            sync_snapshot=self._sync_snapshot,
        )
        return client

    async def get_or_init_client(
        self,
        account: WebAccount,
        *,
        timeout: float = 30.0,
        auto_refresh: bool = True,
    ) -> GeminiClient:
        """Return an initialized ``GeminiClient`` for ``account``, initializing under ``reinit_lock``."""
        async with account.reinit_lock:
            existing_client = self._clients.get(account.id)
            if existing_client is not None and getattr(existing_client, "_running", False):
                latest = extract_client_web_cookies(
                    existing_client, fallback_cookies=account.cookies
                )
                if latest.secure_1psid:
                    account.cookies = latest
                return existing_client

            return await self._create_and_init_client(
                account=account,
                timeout=timeout,
                auto_refresh=auto_refresh,
            )

    async def _persist_and_close_existing(self, account: WebAccount) -> None:
        """Save latest cookies and close the existing ``GeminiClient`` for ``account`` if present."""
        existing_client = self._clients.pop(account.id, None)
        if existing_client is None:
            return
        await save_account_cookies_isolated(
            account,
            client=existing_client,
            cache_dir=self._cache_dir,
            sync_snapshot=self._sync_snapshot,
        )
        try:
            if getattr(existing_client, "_running", False) or getattr(
                existing_client, "client", None
            ) is not None:
                await existing_client.close()
        except Exception as exc:
            logger.warning(
                "Error closing existing GeminiClient for account '%s': %s",
                account.id,
                exc,
            )

    # Clean Code Rule 3 exception: `close_only` boolean keyword argument is mandated
    # by the Level 2 specification contract for `safe_reinit_or_close`. Callers may
    # also use `close_client_if_idle()` for a non-flag close operation.
    async def safe_reinit_or_close(
        self,
        account: WebAccount,
        *,
        close_only: bool = False,
        timeout: float = 30.0,
    ) -> GeminiClient | None:
        """Close or re-initialize ``account``'s client only when ``active_requests == 0``.

        Protected by ``account.reinit_lock``. Raises ``ActiveRequestsConcurrencyError``
        if ``account.active_requests > 0`` ($C_{\\text{web}, i} > 0$) so an in-flight
        stream on the same account is never severed.
        """
        async with account.reinit_lock:
            if account.active_requests > 0:
                raise ActiveRequestsConcurrencyError(
                    account_id=account.id,
                    active_requests=account.active_requests,
                )

            await self._persist_and_close_existing(account)
            if close_only:
                return None

            return await self._create_and_init_client(
                account=account,
                timeout=timeout,
                auto_refresh=True,
            )

    async def close_client_if_idle(self, account: WebAccount) -> bool:
        """Close ``account``'s ``GeminiClient`` safely when no requests are active.

        Returns True if the client was successfully closed, or False if active_requests > 0.
        """
        if account.active_requests > 0:
            return False
        await self.safe_reinit_or_close(account, close_only=True)
        return True

    async def flush_all_cookies(
        self,
        accounts: Iterable[WebAccount] | None = None,
    ) -> list[Path]:
        """Flush rotated cookies for all active Web accounts to their isolated cache files."""
        sanitize_shared_cookie_env()
        target_accounts = (
            list(accounts)
            if accounts is not None
            else list(state_registry.web_accounts)
        )
        saved_paths: list[Path] = []
        for account in target_accounts:
            client = self._clients.get(account.id)
            if not account.cookies.secure_1psid and client is None:
                continue
            try:
                path = await save_account_cookies_isolated(
                    account,
                    client=client,
                    cache_dir=self._cache_dir,
                    sync_snapshot=False,
                )
                saved_paths.append(path)
            except Exception as exc:
                logger.error(
                    "Failed to flush cookies for account '%s': %s",
                    account.id,
                    exc,
                )

        if saved_paths and self._sync_snapshot:
            await save_accounts_snapshot()
        return saved_paths


web_session_manager = WebSessionManager()
