"""Isolated Firefox browser launcher and session manager.

Provides capabilities for launching isolated, ephemeral Mozilla Firefox sessions
with custom temporary profiles for harvesting Gemini web session cookies and
completing interactive OAuth flows without polluting the user's main profile.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Awaitable

from config.settings import WebCookies

logger = logging.getLogger(__name__)

# Constants
SECURE_1PSID_COOKIE_NAME: str = "__Secure-1PSID"
SECURE_1PSIDTS_COOKIE_NAME: str = "__Secure-1PSIDTS"
DEFAULT_GOOGLE_COOKIE_DOMAIN: str = ".google.com"
GEMINI_APP_URL: str = "https://gemini.google.com/app"

USER_JS_PREFS_CONTENT: str = """\
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("startup.homepage_welcome_url", "");
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("signon.rememberSignons", false);
user_pref("browser.sessionstore.resume_from_crash", false);
"""


class FirefoxNotFoundError(RuntimeError):
    """Raised when Mozilla Firefox executable cannot be resolved on the system."""


def _is_executable_file(path_str: str | Path | None) -> bool:
    """Check whether a path points to an existing executable file."""
    if not path_str:
        return False
    path = Path(path_str)
    return path.is_file() and os.access(path, os.X_OK)


def _get_standard_firefox_paths() -> list[str]:
    """Return standard platform-specific installation paths for Firefox."""
    candidates: list[str] = []
    # Windows standard paths and environment variables
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        val = os.environ.get(env_var)
        if val:
            candidates.append(str(Path(val) / "Mozilla Firefox" / "firefox.exe"))
    candidates.extend([
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ])
    # Linux and Unix derivatives
    candidates.extend([
        "/usr/bin/firefox",
        "/snap/bin/firefox",
        "/var/lib/flatpak/exports/bin/org.mozilla.firefox",
    ])
    # macOS standard path
    candidates.append("/Applications/Firefox.app/Contents/MacOS/firefox")
    return candidates


def resolve_firefox_binary() -> str:
    """Resolve the Firefox executable binary path across platforms.

    Priority order:
    1. FIREFOX_BINARY_PATH environment variable (if set and executable).
    2. PATH resolution via shutil.which('firefox') or shutil.which('firefox.exe').
    3. Standard operating system installation paths.

    Raises:
        FirefoxNotFoundError: If no valid Firefox binary could be found.
    """
    env_bin = os.environ.get("FIREFOX_BINARY_PATH")
    if env_bin and _is_executable_file(env_bin):
        return env_bin

    for binary_name in ("firefox", "firefox.exe"):
        which_path = shutil.which(binary_name)
        if which_path and _is_executable_file(which_path):
            return which_path

    for candidate in _get_standard_firefox_paths():
        if _is_executable_file(candidate):
            return candidate

    raise FirefoxNotFoundError(
        "Mozilla Firefox binary was not found. Please install Firefox "
        "(https://www.mozilla.org/firefox/) or set the FIREFOX_BINARY_PATH "
        "environment variable pointing to the firefox executable."
    )


def create_isolated_firefox_profile() -> Path:
    """Create a temporary isolated Firefox profile directory with customized preferences.

    The profile writes a user.js file disabling default browser checks, first-run
    welcome tabs, password saving prompts, crash recovery, and telemetry notifications.
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="ghpg_ff_isolated_"))
    user_js_file = profile_dir / "user.js"
    user_js_file.write_text(USER_JS_PREFS_CONTENT, encoding="utf-8")
    return profile_dir


class IsolatedFirefoxSession:
    """Manages the lifecycle of an isolated Firefox browser process and profile."""

    def __init__(
        self,
        target_url: str,
        firefox_bin: str | None = None,
        profile_dir: Path | None = None,
    ) -> None:
        self.target_url: str = target_url
        self.firefox_bin: str = firefox_bin or resolve_firefox_binary()
        self.profile_dir: Path = profile_dir or create_isolated_firefox_profile()
        self._cleaned_up: bool = False
        try:
            launch_args = [
                self.firefox_bin,
                "-no-remote",
                "-new-instance",
                "-profile",
                str(self.profile_dir),
            ]
            if sys.platform == "win32":
                launch_args.append("--wait-for-browser")
            launch_args.append(self.target_url)

            self.proc: subprocess.Popen[Any] = subprocess.Popen(
                launch_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._cleanup_profile_directory()
            raise

    def is_alive(self) -> bool:
        """Check if the Firefox browser process is actively running."""
        proc = getattr(self, "proc", None)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        p_dir = getattr(self, "profile_dir", None)
        if p_dir and hasattr(p_dir, "exists") and p_dir.exists():
            lock_file = p_dir / "parent.lock"
            if lock_file.exists():
                try:
                    with open(lock_file, "r+b"):
                        return False
                except (PermissionError, OSError):
                    return True
        return False

    def close_and_cleanup(self) -> None:
        """Terminate the browser process and purge the isolated profile directory."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._terminate_process()
        self._cleanup_profile_directory()

    def _terminate_process(self) -> None:
        """Terminate or kill the browser process if it is still running."""
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except (subprocess.TimeoutExpired, TimeoutError):
                    proc.kill()
                    proc.wait(timeout=2.0)
            except (ProcessLookupError, OSError):
                pass
        if sys.platform == "win32" and isinstance(getattr(proc, "pid", None), int) and proc.pid > 0:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                )
            except Exception:
                pass

    def _cleanup_profile_directory(
        self, max_attempts: int = 5, retry_delay: float = 0.2
    ) -> None:
        """Recursively remove the profile directory with retry handling for file locks."""
        p_dir = getattr(self, "profile_dir", None)
        if not p_dir or not p_dir.exists():
            return
        for attempt in range(max_attempts):
            shutil.rmtree(p_dir, ignore_errors=True)
            if not p_dir.exists():
                return
            if attempt < max_attempts - 1:
                time.sleep(retry_delay)

    def __enter__(self) -> IsolatedFirefoxSession:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_and_cleanup()

    async def __aenter__(self) -> IsolatedFirefoxSession:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_and_cleanup()


def _is_session_alive(session: Any) -> bool:
    """Determine whether an IsolatedFirefoxSession or mock session is actively running."""
    if isinstance(session, IsolatedFirefoxSession):
        return session.is_alive()
    proc = getattr(session, "proc", None)
    if proc is not None:
        try:
            return proc.poll() is None
        except Exception:
            pass
    return False


def _query_moz_cookies_db(db_path: Path, domain_name: str) -> dict[str, str]:
    """Read Google session cookies from a moz_cookies SQLite database."""
    normalized_domain = domain_name.lstrip(".").lower()
    found: dict[str, str] = {}
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        try:
            conn = sqlite3.connect(str(db_path.resolve()), timeout=5.0)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return {}

    try:
        with conn:
            cursor = conn.cursor()
            params = (
                SECURE_1PSID_COOKIE_NAME,
                SECURE_1PSIDTS_COOKIE_NAME,
                f"%{normalized_domain}",
                normalized_domain,
            )
            try:
                cursor.execute(
                    "SELECT name, value FROM moz_cookies "
                    "WHERE name IN (?, ?) AND (host LIKE ? OR host = ?) "
                    "ORDER BY lastAccessed DESC",
                    params,
                )
            except sqlite3.OperationalError:
                cursor.execute(
                    "SELECT name, value FROM moz_cookies "
                    "WHERE name IN (?, ?) AND (host LIKE ? OR host = ?)",
                    params,
                )
            for name, val in cursor.fetchall():
                if name and val and str(name) not in found:
                    found[str(name)] = str(val).strip()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return {}
    finally:
        conn.close()
    return found


def _read_profile_cookies(profile_dir: Path, domain_name: str) -> dict[str, str]:
    """Copy cookies.sqlite and WAL files to a temp directory and query them."""
    db_path = profile_dir / "cookies.sqlite"
    if not db_path.exists():
        return {}

    with tempfile.TemporaryDirectory(prefix="ghpg_read_cookies_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        tmp_db = tmp_dir / "cookies.sqlite"
        try:
            shutil.copy2(db_path, tmp_db)
            for suffix in ("-wal", "-shm"):
                sidecar = profile_dir / f"cookies.sqlite{suffix}"
                if sidecar.exists():
                    shutil.copy2(sidecar, tmp_dir / f"cookies.sqlite{suffix}")
        except (OSError, PermissionError):
            return {}

        return _query_moz_cookies_db(tmp_db, domain_name)


async def harvest_gemini_cookies_via_isolated_firefox(
    timeout_seconds: float = 180.0,
    domain_name: str = DEFAULT_GOOGLE_COOKIE_DOMAIN,
    poll_interval: float = 1.0,
    session: IsolatedFirefoxSession | None = None,
) -> WebCookies:
    """Launch an isolated Firefox instance to harvest Gemini session cookies.

    Polls the profile's SQLite database until both __Secure-1PSID and
    __Secure-1PSIDTS cookies are captured.
    """
    active_session = session or IsolatedFirefoxSession(target_url=GEMINI_APP_URL)
    start_time = time.monotonic()
    try:
        while True:
            cookies = _read_profile_cookies(active_session.profile_dir, domain_name)
            psid = cookies.get(SECURE_1PSID_COOKIE_NAME, "")
            psidts = cookies.get(SECURE_1PSIDTS_COOKIE_NAME, "")

            if psid and psidts:
                await asyncio.sleep(0.5)
                stable = _read_profile_cookies(active_session.profile_dir, domain_name)
                final_psid = stable.get(SECURE_1PSID_COOKIE_NAME) or psid
                final_psidts = stable.get(SECURE_1PSIDTS_COOKIE_NAME) or psidts
                active_session.close_and_cleanup()
                return WebCookies(secure_1psid=final_psid, secure_1psidts=final_psidts)

            if not _is_session_alive(active_session):
                active_session.close_and_cleanup()
                raise RuntimeError(
                    "Firefox was closed by user before capturing Gemini cookies."
                )

            if time.monotonic() - start_time >= timeout_seconds:
                active_session.close_and_cleanup()
                raise TimeoutError(
                    f"Timed out after {timeout_seconds}s waiting for Gemini cookies."
                )

            await asyncio.sleep(poll_interval)
    finally:
        active_session.close_and_cleanup()


async def run_oauth_in_isolated_firefox(
    auth_url: str,
    awaitable_code_task: Awaitable[Any],
    poll_interval: float = 0.5,
    session: IsolatedFirefoxSession | None = None,
) -> Any:
    """Open an OAuth authorization URL in an isolated Firefox session.

    Monitors the incoming authorization code task while verifying that the
    browser process is still running. Ensures session resources are closed
    and removed in a finally block upon completion, error, or early exit.
    """
    active_session = session or IsolatedFirefoxSession(target_url=auth_url)
    task = asyncio.ensure_future(awaitable_code_task)
    try:
        while not task.done():
            if not _is_session_alive(active_session):
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
                except (asyncio.TimeoutError, TimeoutError):
                    task.cancel()
                    raise RuntimeError(
                        "Firefox was closed by user before OAuth authorization completed."
                    )
            done, _ = await asyncio.wait([task], timeout=poll_interval)
            if done:
                break
        return await task
    finally:
        active_session.close_and_cleanup()
