"""Verification test suite for Level 2 Web Auth & Session Management (`core/auth_web.py`).

Covers:
1. Browser Isolation & DBSC Avoidance (ADR-03):
   - Rejecting Chromium-based browser names and file path markers with ``ChromiumDBSCUnsupportedError``.
   - Extracting session cookies from Firefox ``cookies.sqlite`` profiles with direct SQLite fallback.
   - Resilient handling of locked SQLite files and alternative table schemas.
   - Raising ``ValueError`` when ``__Secure-1PSID`` is missing or empty.
2. Manual Incognito Cookie Parsing & Saved Info Hygiene:
   - Sanitizing token strings, stripping surrounding quotes, prefixes, and trailing semicolons.
   - Parsing dictionary, JSON object, browser-extension array, and HTTP ``Cookie:`` header formats.
   - Returning structured hygiene instructions for disabling Gemini Web ``Saved Info``.
3. Cookie Cache Isolation & Persistence:
   - Sanitizing ``GEMINI_COOKIE_PATH`` from the process environment.
   - Enforcing safe account IDs and preventing path traversal in cookie cache paths.
   - Atomically persisting isolated cookie caches under ``account.cookie_write_lock``.
   - Loading isolated cookies and graceful handling of missing or corrupted cache files.
4. Concurrency Guard & GeminiClient Lifecycle (`WebSessionManager`):
   - Initializing independent ``GeminiClient`` instances under ``account.reinit_lock``.
   - Reusing active running clients without re-initialization.
   - Raising ``ActiveRequestsConcurrencyError`` when attempting to close a client with ``active_requests > 0``.
   - Safely re-initializing or closing idle clients (``active_requests == 0``) while persisting latest cookies.
   - Flushing cookies across all active accounts during maintenance or shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    COOKIES_CACHE_DIR,
    AccountsConfig,
    WebAccount,
    WebCookies,
    save_accounts_snapshot,
    state_registry,
)
from core.auth_web import (
    DEFAULT_GOOGLE_COOKIE_DOMAIN,
    SECURE_1PSID_COOKIE_NAME,
    SECURE_1PSIDTS_COOKIE_NAME,
    SHARED_COOKIE_ENV_VAR,
    ActiveRequestsConcurrencyError,
    ChromiumDBSCUnsupportedError,
    WebSessionManager,
    auto_discover_firefox_cookies,
    extract_client_web_cookies,
    extract_firefox_cookies,
    get_account_cookie_cache_path,
    get_saved_info_hygiene_notice,
    load_account_cookies_isolated,
    parse_cookie_header_or_dict,
    parse_manual_cookies,
    reject_chromium_cookie_source,
    sanitize_shared_cookie_env,
    save_account_cookies_isolated,
    login_web_via_isolated_firefox,
    _read_moz_cookies_with_temp_fallback,
)


# ============================================================================
# Helpers & Fixtures
# ============================================================================


def create_mock_firefox_sqlite(
    db_path: Path,
    psid: str = "test_psid_val_123",
    psidts: str = "test_psidts_val_456",
    include_last_accessed: bool = True,
    domain: str = ".google.com",
) -> Path:
    """Create a minimal SQLite database mirroring Firefox's ``moz_cookies`` table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    with conn:
        if include_last_accessed:
            conn.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT, lastAccessed INTEGER)")
            conn.execute("INSERT INTO moz_cookies (name, value, host, lastAccessed) VALUES (?, ?, ?, ?)", (SECURE_1PSID_COOKIE_NAME, psid, domain, 1700000000))
            if psidts:
                conn.execute("INSERT INTO moz_cookies (name, value, host, lastAccessed) VALUES (?, ?, ?, ?)", (SECURE_1PSIDTS_COOKIE_NAME, psidts, domain, 1700000001))
        else:
            conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)")
            conn.execute("INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)", (SECURE_1PSID_COOKIE_NAME, psid, domain))
            if psidts:
                conn.execute("INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)", (SECURE_1PSIDTS_COOKIE_NAME, psidts, domain))
    conn.close()
    return db_path


class FakeMockGeminiClient:
    """Mock GeminiClient for lifecycle and Concurrency Guard verification."""

    def __init__(
        self,
        secure_1psid: str | None = None,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.secure_1psid = secure_1psid
        self.secure_1psidts = secure_1psidts
        self.proxy = proxy
        self.init_calls: list[dict[str, Any]] = []
        self.close_calls: int = 0
        self._running: bool = False
        self.cookies: dict[str, str] = {
            SECURE_1PSID_COOKIE_NAME: secure_1psid or "",
            SECURE_1PSIDTS_COOKIE_NAME: secure_1psidts or "",
        }

    async def init(
        self,
        timeout: float = 30.0,
        auto_close: bool = False,
        auto_refresh: bool = True,
    ) -> None:
        self.init_calls.append(
            {
                "timeout": timeout,
                "auto_close": auto_close,
                "auto_refresh": auto_refresh,
            }
        )
        self._running = True

    async def close(self) -> None:
        self.close_calls += 1
        self._running = False


# ============================================================================
# 1. Browser Isolation & Chromium DBSC Rejection (ADR-03)
# ============================================================================


def test_reject_chromium_cookie_source_names() -> None:
    """Verify that common Chromium browser names are rejected."""
    for forbidden in ("chrome", "chromium", "edge", "brave", "opera", "vivaldi", "arc"):
        with pytest.raises(ChromiumDBSCUnsupportedError):
            reject_chromium_cookie_source(forbidden)
        with pytest.raises(ChromiumDBSCUnsupportedError):
            reject_chromium_cookie_source(forbidden.upper())


def test_reject_chromium_cookie_source_paths() -> None:
    """Verify that file paths matching Chromium directory markers are rejected."""
    chromium_paths = (
        r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Default\Cookies",
        "/home/user/.config/google-chrome/Default/Cookies",
        r"C:\Users\User\AppData\Local\Microsoft\Edge\User Data\Default\Cookies",
        r"C:\Users\User\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Cookies",
    )
    for path_str in chromium_paths:
        with pytest.raises(ChromiumDBSCUnsupportedError):
            reject_chromium_cookie_source(path_str)


def test_allow_firefox_cookie_paths() -> None:
    """Verify that Firefox paths pass chromium rejection checks."""
    valid_paths = (
        r"C:\Users\User\AppData\Roaming\Mozilla\Firefox\Profiles\xyz.default-release\cookies.sqlite",
        "/home/user/.mozilla/firefox/xyz.default/cookies.sqlite",
    )
    for path_str in valid_paths:
        reject_chromium_cookie_source(path_str)


# ============================================================================
# 2. Firefox SQLite Extraction
# ============================================================================


def test_extract_firefox_cookies_file_not_found(tmp_path: Path) -> None:
    """Verify FileNotFoundError when sqlite file does not exist."""
    non_existent = tmp_path / "non_existent.sqlite"
    with pytest.raises(FileNotFoundError):
        extract_firefox_cookies(non_existent)


def test_extract_firefox_cookies_success(tmp_path: Path) -> None:
    """Verify successful extraction of both cookies from a SQLite database."""
    db_file = tmp_path / "cookies.sqlite"
    create_mock_firefox_sqlite(
        db_file,
        psid="psid_valid_value_999",
        psidts="psidts_valid_value_888",
        include_last_accessed=True,
    )

    cookies = extract_firefox_cookies(db_file)
    assert cookies.secure_1psid == "psid_valid_value_999"
    assert cookies.secure_1psidts == "psidts_valid_value_888"


def test_extract_firefox_cookies_directory_input(tmp_path: Path) -> None:
    """Verify that passing profile directory automatically resolves cookies.sqlite."""
    profile_dir = tmp_path / "profile_xyz"
    db_file = profile_dir / "cookies.sqlite"
    create_mock_firefox_sqlite(db_file, psid="psid_dir_test", psidts="psidts_dir_test")

    cookies = extract_firefox_cookies(profile_dir)
    assert cookies.secure_1psid == "psid_dir_test"
    assert cookies.secure_1psidts == "psidts_dir_test"


def test_extract_firefox_cookies_without_last_accessed(tmp_path: Path) -> None:
    """Verify fallback query when lastAccessed column is absent."""
    db_file = tmp_path / "cookies.sqlite"
    create_mock_firefox_sqlite(
        db_file,
        psid="psid_no_last_acc",
        psidts="psidts_no_last_acc",
        include_last_accessed=False,
    )

    cookies = extract_firefox_cookies(db_file)
    assert cookies.secure_1psid == "psid_no_last_acc"
    assert cookies.secure_1psidts == "psidts_no_last_acc"


def test_extract_firefox_cookies_missing_psid_raises_value_error(tmp_path: Path) -> None:
    """Verify ValueError when __Secure-1PSID is absent from database."""
    db_file = tmp_path / "cookies.sqlite"
    create_mock_firefox_sqlite(db_file, psid="", psidts="only_psidts")

    with pytest.raises(ValueError, match="Required cookie '__Secure-1PSID' not found"):
        extract_firefox_cookies(db_file)


# ============================================================================
# 3. Manual Cookie Parsing & Saved Info Hygiene
# ============================================================================


def test_parse_manual_cookies_clean_and_stripped() -> None:
    """Verify whitespace, quotes, prefixes, and trailing semicolons are stripped."""
    cookies = parse_manual_cookies(
        secure_1psid=' "__Secure-1PSID=token_abc123=="; ',
        secure_1psidts=' "__Secure-1PSIDTS=ts_token_xyz=="; ',
    )
    assert cookies.secure_1psid == "token_abc123=="
    assert cookies.secure_1psidts == "ts_token_xyz=="


def test_parse_manual_cookies_missing_psid_raises() -> None:
    """Verify ValueError when secure_1psid is empty."""
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_manual_cookies("   ")


def test_parse_cookie_header_or_dict_mapping() -> None:
    """Verify parsing from a dictionary mapping."""
    raw_dict = {
        "__Secure-1PSID": "dict_psid",
        "__Secure-1PSIDTS": "dict_psidts",
    }
    cookies = parse_cookie_header_or_dict(raw_dict)
    assert cookies.secure_1psid == "dict_psid"
    assert cookies.secure_1psidts == "dict_psidts"


def test_parse_cookie_header_or_dict_json_string() -> None:
    """Verify parsing from a JSON string."""
    json_text = json.dumps({"__Secure-1PSID": "json_psid", "__Secure-1PSIDTS": "json_psidts"})
    cookies = parse_cookie_header_or_dict(json_text)
    assert cookies.secure_1psid == "json_psid"
    assert cookies.secure_1psidts == "json_psidts"


def test_parse_cookie_header_or_dict_json_array() -> None:
    """Verify parsing from a browser-extension cookie array JSON string."""
    array_text = json.dumps(
        [
            {"name": "OTHER_COOKIE", "value": "other"},
            {"name": "__Secure-1PSID", "value": "arr_psid"},
            {"name": "__Secure-1PSIDTS", "value": "arr_psidts"},
        ]
    )
    cookies = parse_cookie_header_or_dict(array_text)
    assert cookies.secure_1psid == "arr_psid"
    assert cookies.secure_1psidts == "arr_psidts"


def test_parse_cookie_header_or_dict_raw_header() -> None:
    """Verify parsing from an HTTP Cookie header string."""
    header = "Cookie: __Secure-1PSID=header_psid; OTHER=123; __Secure-1PSIDTS=header_psidts"
    cookies = parse_cookie_header_or_dict(header)
    assert cookies.secure_1psid == "header_psid"
    assert cookies.secure_1psidts == "header_psidts"


def test_get_saved_info_hygiene_notice() -> None:
    """Verify structure and content of Saved Info hygiene notice."""
    notice = get_saved_info_hygiene_notice()
    assert notice["url"] == "https://gemini.google.com/saved-info"
    assert notice["action_required"] == "DISABLE_SAVED_INFO_AND_CLOSE_INCOGNITO"
    assert "https://gemini.google.com/saved-info" in notice["reason"]
    assert "المعلومات المحفوظة" in notice["message_ar"]


# ============================================================================
# 4. Cookie Cache Isolation & Path Sanitization
# ============================================================================


def test_sanitize_shared_cookie_env() -> None:
    """Verify that GEMINI_COOKIE_PATH is removed from os.environ."""
    os.environ[SHARED_COOKIE_ENV_VAR] = "/tmp/shared_cookies"
    removed = sanitize_shared_cookie_env()
    assert removed == "/tmp/shared_cookies"
    assert SHARED_COOKIE_ENV_VAR not in os.environ

    assert sanitize_shared_cookie_env() is None


def test_get_account_cookie_cache_path_valid(tmp_path: Path) -> None:
    """Verify valid account IDs produce paths under cache directory."""
    path = get_account_cookie_cache_path("acc_web_01", cache_dir=tmp_path)
    assert path == (tmp_path / "acc_web_01.json").resolve()


def test_get_account_cookie_cache_path_traversal_rejection(tmp_path: Path) -> None:
    """Verify path traversal characters raise ValueError."""
    for malicious in ("../hack", r"..\hack", "sub/acc", r"sub\acc", "acc:stream", ""):
        with pytest.raises(ValueError):
            get_account_cookie_cache_path(malicious, cache_dir=tmp_path)


# ============================================================================
# 5. Isolated Cookie Cache Persistence & Loading
# ============================================================================


@pytest.mark.asyncio
async def test_save_and_load_account_cookies_isolated(tmp_path: Path) -> None:
    """Verify atomic saving and loading of account cookies."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_persist_01",
        label="Test Web Account",
        cookies=WebCookies(
            secure_1psid="init_psid_val",
            secure_1psidts="init_psidts_val",
        ),
    )

    saved_path = await save_account_cookies_isolated(
        account,
        cache_dir=cache_dir,
        sync_snapshot=False,
    )
    assert saved_path.exists()
    assert account.cookie_file == str(saved_path)

    loaded = load_account_cookies_isolated(account.id, cache_dir=cache_dir)
    assert loaded is not None
    assert loaded.secure_1psid == "init_psid_val"
    assert loaded.secure_1psidts == "init_psidts_val"


@pytest.mark.asyncio
async def test_save_account_cookies_isolated_with_client_rotation(tmp_path: Path) -> None:
    """Verify that rotated cookies from GeminiClient are extracted and saved."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_client_rot",
        label="Test Account",
        cookies=WebCookies(secure_1psid="old_psid", secure_1psidts="old_psidts"),
    )

    client = FakeMockGeminiClient(
        secure_1psid="rotated_psid_new",
        secure_1psidts="rotated_psidts_new",
    )

    await save_account_cookies_isolated(
        account,
        client=client,  # type: ignore[arg-type]
        cache_dir=cache_dir,
        sync_snapshot=False,
    )

    assert account.cookies.secure_1psid == "rotated_psid_new"
    assert account.cookies.secure_1psidts == "rotated_psidts_new"

    loaded = load_account_cookies_isolated(account.id, cache_dir=cache_dir)
    assert loaded is not None
    assert loaded.secure_1psid == "rotated_psid_new"
    assert loaded.secure_1psidts == "rotated_psidts_new"


def test_load_account_cookies_isolated_missing_or_corrupt(tmp_path: Path) -> None:
    """Verify None is returned when cache file is missing or corrupted."""
    assert load_account_cookies_isolated("non_existent", cache_dir=tmp_path) is None

    corrupt_file = tmp_path / "corrupt_acc.json"
    corrupt_file.write_text("{invalid json", encoding="utf-8")
    assert load_account_cookies_isolated("corrupt_acc", cache_dir=tmp_path) is None


# ============================================================================
# 6. WebSessionManager Lifecycle & Concurrency Guard
# ============================================================================


@pytest.mark.asyncio
async def test_session_manager_get_or_init_client(tmp_path: Path) -> None:
    """Verify client initialization and persistence under reinit_lock."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_manager_01",
        label="Manager Account",
        proxy="http://127.0.0.1:8080",
        cookies=WebCookies(secure_1psid="mgr_psid", secure_1psidts="mgr_psidts"),
    )

    manager = WebSessionManager(
        cache_dir=cache_dir,
        sync_snapshot=False,
        client_factory=FakeMockGeminiClient,
    )

    client = await manager.get_or_init_client(account, timeout=45.0, auto_refresh=True)
    assert isinstance(client, FakeMockGeminiClient)
    assert client.init_calls == [
        {"timeout": 45.0, "auto_close": False, "auto_refresh": True}
    ]
    assert client.proxy == "http://127.0.0.1:8080"
    assert manager.get_client(account.id) is client

    # Subsequent call reuses existing running client without re-initialization
    reused = await manager.get_or_init_client(account)
    assert reused is client
    assert len(client.init_calls) == 1


@pytest.mark.asyncio
async def test_safe_reinit_or_close_concurrency_guard(tmp_path: Path) -> None:
    """Verify ActiveRequestsConcurrencyError is raised when active_requests > 0."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_concurrency_guard",
        label="Concurrency Account",
        cookies=WebCookies(secure_1psid="guard_psid", secure_1psidts="guard_psidts"),
    )

    manager = WebSessionManager(
        cache_dir=cache_dir,
        sync_snapshot=False,
        client_factory=FakeMockGeminiClient,
    )

    client = await manager.get_or_init_client(account)
    assert isinstance(client, FakeMockGeminiClient)

    # Simulate in-flight streaming request: active_requests = 1
    account.active_requests = 1

    with pytest.raises(ActiveRequestsConcurrencyError) as exc_info:
        await manager.safe_reinit_or_close(account)

    assert exc_info.value.account_id == "acc_concurrency_guard"
    assert exc_info.value.active_requests == 1
    # Verify client was NOT closed
    assert client.close_calls == 0
    assert manager.get_client(account.id) is client


@pytest.mark.asyncio
async def test_safe_reinit_or_close_when_idle(tmp_path: Path) -> None:
    """Verify safe client closure and re-initialization when active_requests == 0."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_idle_reinit",
        label="Idle Account",
        cookies=WebCookies(secure_1psid="idle_psid", secure_1psidts="idle_psidts"),
    )

    manager = WebSessionManager(
        cache_dir=cache_dir,
        sync_snapshot=False,
        client_factory=FakeMockGeminiClient,
    )

    old_client = await manager.get_or_init_client(account)
    assert isinstance(old_client, FakeMockGeminiClient)
    assert account.active_requests == 0

    # Re-initialization
    new_client = await manager.safe_reinit_or_close(account, close_only=False, timeout=25.0)
    assert isinstance(new_client, FakeMockGeminiClient)
    assert new_client is not old_client
    assert old_client.close_calls == 1
    assert manager.get_client(account.id) is new_client


@pytest.mark.asyncio
async def test_close_client_if_idle(tmp_path: Path) -> None:
    """Verify close_client_if_idle closes client and removes it from registry."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    account = WebAccount(
        id="acc_idle_close",
        label="Close Account",
        cookies=WebCookies(secure_1psid="close_psid", secure_1psidts="close_psidts"),
    )

    manager = WebSessionManager(
        cache_dir=cache_dir,
        sync_snapshot=False,
        client_factory=FakeMockGeminiClient,
    )

    client = await manager.get_or_init_client(account)
    assert isinstance(client, FakeMockGeminiClient)

    await manager.close_client_if_idle(account)
    assert client.close_calls == 1
    assert manager.get_client(account.id) is None


@pytest.mark.asyncio
async def test_flush_all_cookies(tmp_path: Path) -> None:
    """Verify flush_all_cookies persists caches for all active accounts."""
    cache_dir = tmp_path / "cookies_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    acc1 = WebAccount(
        id="acc_flush_1",
        label="Acc 1",
        cookies=WebCookies(secure_1psid="psid_1", secure_1psidts="psidts_1"),
    )
    acc2 = WebAccount(
        id="acc_flush_2",
        label="Acc 2",
        cookies=WebCookies(secure_1psid="psid_2", secure_1psidts="psidts_2"),
    )

    manager = WebSessionManager(
        cache_dir=cache_dir,
        sync_snapshot=False,
        client_factory=FakeMockGeminiClient,
    )

    client1 = await manager.get_or_init_client(acc1)
    client2 = await manager.get_or_init_client(acc2)
    assert isinstance(client1, FakeMockGeminiClient)
    assert isinstance(client2, FakeMockGeminiClient)

    client1.cookies[SECURE_1PSIDTS_COOKIE_NAME] = "rotated_ts_1"
    client2.cookies[SECURE_1PSIDTS_COOKIE_NAME] = "rotated_ts_2"

    saved_paths = await manager.flush_all_cookies([acc1, acc2])
    assert len(saved_paths) == 2

    loaded1 = load_account_cookies_isolated("acc_flush_1", cache_dir=cache_dir)
    loaded2 = load_account_cookies_isolated("acc_flush_2", cache_dir=cache_dir)
    assert loaded1 is not None and loaded1.secure_1psidts == "rotated_ts_1"
    assert loaded2 is not None and loaded2.secure_1psidts == "rotated_ts_2"


def test_auto_discover_firefox_cookies_from_profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify auto_discover_firefox_cookies discovers and extracts from .default-release profile."""
    fake_appdata = tmp_path / "AppData" / "Roaming"
    profile_dir = fake_appdata / "Mozilla" / "Firefox" / "Profiles" / "abc12345.default-release"
    db_file = profile_dir / "cookies.sqlite"
    create_mock_firefox_sqlite(db_file, psid="auto_discovered_psid", psidts="auto_discovered_ts")

    monkeypatch.setenv("APPDATA", str(fake_appdata))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    path, cookies = auto_discover_firefox_cookies()
    assert path is not None
    assert path.resolve() == db_file.resolve()
    assert cookies is not None
    assert cookies.secure_1psid == "auto_discovered_psid"
    assert cookies.secure_1psidts == "auto_discovered_ts"


def test_auto_discover_firefox_cookies_fallback_to_browser_cookie3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify auto_discover_firefox_cookies falls back to browser_cookie3 when no profile DB exists."""
    empty_appdata = tmp_path / "EmptyAppData"
    empty_appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(empty_appdata))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    mock_cookie1 = MagicMock()
    mock_cookie1.name = "__Secure-1PSID"
    mock_cookie1.value = "browser3_psid"

    mock_cookie2 = MagicMock()
    mock_cookie2.name = "__Secure-1PSIDTS"
    mock_cookie2.value = "browser3_ts"

    mock_bc3 = MagicMock()
    mock_bc3.firefox.return_value = [mock_cookie1, mock_cookie2]
    monkeypatch.setattr("core.auth_web.browser_cookie3", mock_bc3)

    path, cookies = auto_discover_firefox_cookies()
    assert path is None
    assert cookies is not None
    assert cookies.secure_1psid == "browser3_psid"
    assert cookies.secure_1psidts == "browser3_ts"


def test_auto_discover_firefox_cookies_returns_none_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify auto_discover_firefox_cookies returns (None, None) when no cookies found anywhere."""
    empty_appdata = tmp_path / "EmptyAppData"
    empty_appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(empty_appdata))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    mock_bc3 = MagicMock()
    mock_bc3.firefox.return_value = []
    monkeypatch.setattr("core.auth_web.browser_cookie3", mock_bc3)

    path, cookies = auto_discover_firefox_cookies()
    assert path is None
    assert cookies is None


@pytest.mark.asyncio
async def test_session_manager_close_client(tmp_path: Path) -> None:
    """Verify WebSessionManager.close_client closes client and flushes cookies."""
    cache_dir = tmp_path / "cookies_cache"
    account = WebAccount(
        id="acc_close_test",
        label="Close Account",
        cookies=WebCookies(secure_1psid="init_psid", secure_1psidts="init_ts"),
    )
    manager = WebSessionManager(
        cache_dir=cache_dir, sync_snapshot=False, client_factory=FakeMockGeminiClient
    )
    client = await manager.get_or_init_client(account)
    assert manager.get_client(account.id) is client

    closed = await manager.close_client(account)
    assert closed is True
    assert client.close_calls == 1
    assert manager.get_client(account.id) is None
    assert await manager.close_client(account) is False

    client2 = await manager.get_or_init_client(account)
    assert await manager.close_client(account.id) is True
    assert client2.close_calls == 1
    assert manager.get_client(account.id) is None


@pytest.mark.asyncio
async def test_session_manager_close_all(tmp_path: Path) -> None:
    """Verify WebSessionManager.close_all closes all managed clients."""
    cache_dir = tmp_path / "cookies_cache"
    manager = WebSessionManager(
        cache_dir=cache_dir, sync_snapshot=False, client_factory=FakeMockGeminiClient
    )
    clients: list[FakeMockGeminiClient] = []
    for i in range(3):
        acc = WebAccount(
            id=f"acc_all_{i}",
            label=f"Acc {i}",
            cookies=WebCookies(secure_1psid=f"psid_{i}"),
        )
        cl = await manager.get_or_init_client(acc)
        clients.append(cl)

    assert len(manager._clients) == 3
    await manager.close_all()
    assert len(manager._clients) == 0
    assert all(cl.close_calls == 1 for cl in clients)


def test_read_moz_cookies_with_temp_fallback_wal(tmp_path: Path) -> None:
    """Verify WAL and SHM sidecars are copied and read in _read_moz_cookies_with_temp_fallback."""
    db_file = tmp_path / "cookies.sqlite"
    conn = sqlite3.connect(str(db_file))
    with conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, lastAccessed INTEGER)"
        )
        conn.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
            (SECURE_1PSID_COOKIE_NAME, "wal_psid_val", ".google.com", 100),
        )
        conn.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
            (SECURE_1PSIDTS_COOKIE_NAME, "wal_ts_val", ".google.com", 200),
        )
    wal_file = tmp_path / "cookies.sqlite-wal"
    shm_file = tmp_path / "cookies.sqlite-shm"
    if not wal_file.exists():
        wal_file.write_bytes(b"dummy_wal_header")
    if not shm_file.exists():
        shm_file.write_bytes(b"dummy_shm_header")

    cookies = _read_moz_cookies_with_temp_fallback(db_file, ".google.com")
    assert cookies[SECURE_1PSID_COOKIE_NAME] == "wal_psid_val"
    assert cookies[SECURE_1PSIDTS_COOKIE_NAME] == "wal_ts_val"

    extracted = extract_firefox_cookies(db_file)
    assert extracted.secure_1psid == "wal_psid_val"
    assert extracted.secure_1psidts == "wal_ts_val"


@pytest.mark.asyncio
async def test_login_web_via_isolated_firefox_harvest() -> None:
    """Verify login_web_via_isolated_firefox delegates to harvest_gemini_cookies_via_isolated_firefox."""
    from unittest.mock import AsyncMock, patch

    expected = WebCookies(secure_1psid="harvest_psid", secure_1psidts="harvest_ts")
    with patch(
        "core.firefox_launcher.harvest_gemini_cookies_via_isolated_firefox",
        new_callable=AsyncMock,
    ) as mock_harvest:
        mock_harvest.return_value = expected
        result = await login_web_via_isolated_firefox(timeout_seconds=45.0)
        mock_harvest.assert_awaited_once_with(
            timeout_seconds=45.0,
            domain_name=DEFAULT_GOOGLE_COOKIE_DOMAIN,
        )
        assert result == expected


