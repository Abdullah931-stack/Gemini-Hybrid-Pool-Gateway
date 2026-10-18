"""Unit tests for core/firefox_launcher.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from core.firefox_launcher import (
    FirefoxNotFoundError,
    IsolatedFirefoxSession,
    _get_standard_firefox_paths,
    _is_executable_file,
    _query_moz_cookies_db,
    _read_profile_cookies,
    create_isolated_firefox_profile,
    harvest_gemini_cookies_via_isolated_firefox,
    resolve_firefox_binary,
    run_oauth_in_isolated_firefox,
)


def _populate_test_moz_cookies(
    db_path: Path,
    cookies: list[tuple[str, str, str, int]],
    with_last_accessed: bool = True,
) -> None:
    """Create a temporary moz_cookies SQLite table populated with test records."""
    conn = sqlite3.connect(str(db_path))
    with conn:
        if with_last_accessed:
            conn.execute(
                "CREATE TABLE moz_cookies ("
                "id INTEGER PRIMARY KEY, "
                "name TEXT, value TEXT, host TEXT, lastAccessed INTEGER)"
            )
            for name, val, host, la in cookies:
                conn.execute(
                    "INSERT INTO moz_cookies (name, value, host, lastAccessed) VALUES (?, ?, ?, ?)",
                    (name, val, host, la),
                )
        else:
            conn.execute(
                "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT)"
            )
            for name, val, host, _ in cookies:
                conn.execute(
                    "INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)",
                    (name, val, host),
                )
    conn.close()


class TestResolveFirefoxBinary:
    """Tests for Firefox executable discovery and resolution."""

    def test_resolve_via_env_var(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake_bin = tmp_path / "custom_firefox.exe"
        fake_bin.write_text("binary", encoding="utf-8")
        monkeypatch.setenv("FIREFOX_BINARY_PATH", str(fake_bin))

        with patch("core.firefox_launcher._is_executable_file", return_value=True):
            resolved = resolve_firefox_binary()
            assert resolved == str(fake_bin)

    def test_resolve_via_env_var_invalid_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIREFOX_BINARY_PATH", "/non/existent/firefox")
        with patch("core.firefox_launcher._is_executable_file", side_effect=lambda p: "which" in str(p)):
            with patch("shutil.which", return_value="/usr/bin/which_firefox"):
                resolved = resolve_firefox_binary()
                assert resolved == "/usr/bin/which_firefox"

    def test_resolve_via_which_firefox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIREFOX_BINARY_PATH", raising=False)
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/firefox" if name == "firefox" else None):
            with patch("core.firefox_launcher._is_executable_file", return_value=True):
                assert resolve_firefox_binary() == "/usr/bin/firefox"

    def test_resolve_via_which_firefox_exe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIREFOX_BINARY_PATH", raising=False)
        with patch(
            "shutil.which",
            side_effect=lambda name: r"C:\firefox.exe" if name == "firefox.exe" else None,
        ):
            with patch("core.firefox_launcher._is_executable_file", return_value=True):
                assert resolve_firefox_binary() == r"C:\firefox.exe"

    def test_resolve_via_standard_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIREFOX_BINARY_PATH", raising=False)
        with patch("shutil.which", return_value=None):
            with patch(
                "core.firefox_launcher._get_standard_firefox_paths",
                return_value=["/var/lib/flatpak/exports/bin/org.mozilla.firefox"],
            ):
                with patch("core.firefox_launcher._is_executable_file", return_value=True):
                    assert resolve_firefox_binary() == "/var/lib/flatpak/exports/bin/org.mozilla.firefox"

    def test_resolve_raises_firefox_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIREFOX_BINARY_PATH", raising=False)
        with patch("shutil.which", return_value=None):
            with patch("core.firefox_launcher._get_standard_firefox_paths", return_value=[]):
                with pytest.raises(FirefoxNotFoundError) as exc_info:
                    resolve_firefox_binary()
                assert "Mozilla Firefox binary was not found" in str(exc_info.value)

    def test_is_executable_file(self, tmp_path: Path) -> None:
        assert not _is_executable_file(None)
        assert not _is_executable_file(tmp_path / "missing.exe")
        real_file = tmp_path / "script.sh"
        real_file.write_text("echo hi", encoding="utf-8")
        assert _is_executable_file(real_file)


class TestCreateIsolatedFirefoxProfile:
    """Tests for temporary profile creation and user.js contents."""

    def test_profile_creation_and_preferences(self) -> None:
        profile_path = create_isolated_firefox_profile()
        try:
            assert profile_path.exists()
            assert profile_path.name.startswith("ghpg_ff_isolated_")
            user_js = profile_path / "user.js"
            assert user_js.exists()

            content = user_js.read_text(encoding="utf-8")
            assert 'user_pref("browser.shell.checkDefaultBrowser", false);' in content
            assert 'user_pref("startup.homepage_welcome_url", "");' in content
            assert 'user_pref("browser.startup.homepage_override.mstone", "ignore");' in content
            assert (
                'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);'
                in content
            )
            assert 'user_pref("signon.rememberSignons", false);' in content
            assert 'user_pref("browser.sessionstore.resume_from_crash", false);' in content
        finally:
            shutil.rmtree(profile_path, ignore_errors=True)


class TestIsolatedFirefoxSession:
    """Tests for IsolatedFirefoxSession process lifecycle and cleanup."""

    @patch("subprocess.Popen")
    def test_session_starts_process_with_correct_args(
        self, mock_popen: MagicMock, tmp_path: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        mock_popen.return_value = fake_proc

        session = IsolatedFirefoxSession(
            target_url="https://gemini.google.com/app",
            firefox_bin="firefox",
            profile_dir=tmp_path,
        )
        expected_cmd = [
            "firefox",
            "-no-remote",
            "-new-instance",
            "-profile",
            str(tmp_path),
        ]
        if sys.platform == "win32":
            expected_cmd.append("--wait-for-browser")
        expected_cmd.append("https://gemini.google.com/app")

        try:
            mock_popen.assert_called_once_with(
                expected_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert session.target_url == "https://gemini.google.com/app"
            assert session.firefox_bin == "firefox"
            assert session.profile_dir == tmp_path
        finally:
            session.close_and_cleanup()

    @patch("subprocess.Popen")
    def test_session_sync_context_manager(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        mock_popen.return_value = fake_proc

        with IsolatedFirefoxSession(
            target_url="https://test.com",
            firefox_bin="firefox",
            profile_dir=tmp_path,
        ) as session:
            assert session.proc == fake_proc
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called_once_with(timeout=5.0)

    @pytest.mark.asyncio
    async def test_session_async_context_manager(self, tmp_path: Path) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=fake_proc):
            async with IsolatedFirefoxSession(
                target_url="https://test.com",
                firefox_bin="firefox",
                profile_dir=tmp_path,
            ) as session:
                assert session.proc == fake_proc
        fake_proc.terminate.assert_called_once()

    @patch("subprocess.Popen")
    def test_session_kills_on_timeout(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="firefox", timeout=5.0),
            None,
        ]
        mock_popen.return_value = fake_proc

        session = IsolatedFirefoxSession(
            target_url="https://test.com",
            firefox_bin="firefox",
            profile_dir=tmp_path,
        )
        session.close_and_cleanup()
        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_called_once()

    @patch("subprocess.Popen")
    def test_session_cleanup_retry_on_file_lock(
        self, mock_popen: MagicMock, tmp_path: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 0
        mock_popen.return_value = fake_proc

        dummy_dir = tmp_path / "locked_profile"
        dummy_dir.mkdir()

        call_count = 0
        real_rmtree = shutil.rmtree

        def flaky_rmtree(path: Path, ignore_errors: bool = False) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return  # simulates directory remaining locked
            real_rmtree(path, ignore_errors=True)

        with patch("shutil.rmtree", side_effect=flaky_rmtree):
            with patch("time.sleep", return_value=None):
                session = IsolatedFirefoxSession(
                    target_url="https://test.com",
                    firefox_bin="firefox",
                    profile_dir=dummy_dir,
                )
                session.close_and_cleanup()
                assert call_count >= 3
                assert not dummy_dir.exists()

    @patch("subprocess.Popen", side_effect=OSError("Exec failed"))
    def test_session_init_failure_cleans_up_profile(
        self, mock_popen: MagicMock, tmp_path: Path
    ) -> None:
        dummy_dir = tmp_path / "failed_profile"
        dummy_dir.mkdir()
        with pytest.raises(OSError, match="Exec failed"):
            IsolatedFirefoxSession(
                target_url="https://test.com",
                firefox_bin="firefox",
                profile_dir=dummy_dir,
            )
        assert not dummy_dir.exists()

    @patch("subprocess.Popen")
    def test_isolated_session_is_alive_no_recursion(
        self, mock_popen: MagicMock, tmp_path: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        mock_popen.return_value = fake_proc

        session = IsolatedFirefoxSession(
            target_url="https://test.com",
            firefox_bin="firefox",
            profile_dir=tmp_path,
        )
        # Must not raise RecursionError
        assert session.is_alive() is True
        from core.firefox_launcher import _is_session_alive
        assert _is_session_alive(session) is True

        fake_proc.poll.return_value = 0
        assert session.is_alive() is False
        assert _is_session_alive(session) is False
        session.close_and_cleanup()


class TestCookiesExtractionAndHarvesting:
    """Tests for moz_cookies parsing and harvest_gemini_cookies_via_isolated_firefox."""

    def test_query_moz_cookies_db(self, tmp_path: Path) -> None:
        db_file = tmp_path / "cookies.sqlite"
        records = [
            ("__Secure-1PSID", "old_psid", ".google.com", 100),
            ("__Secure-1PSID", "latest_psid", ".google.com", 200),
            ("__Secure-1PSIDTS", "latest_ts", "accounts.google.com", 200),
            ("other_cookie", "ignored", ".google.com", 200),
        ]
        _populate_test_moz_cookies(db_file, records, with_last_accessed=True)

        found = _query_moz_cookies_db(db_file, ".google.com")
        assert found.get("__Secure-1PSID") == "latest_psid"
        assert found.get("__Secure-1PSIDTS") == "latest_ts"

    def test_query_moz_cookies_db_fallback_schema(self, tmp_path: Path) -> None:
        db_file = tmp_path / "cookies_fallback.sqlite"
        records = [
            ("__Secure-1PSID", "psid_val", ".google.com", 0),
            ("__Secure-1PSIDTS", "ts_val", "google.com", 0),
        ]
        _populate_test_moz_cookies(db_file, records, with_last_accessed=False)

        found = _query_moz_cookies_db(db_file, "google.com")
        assert found.get("__Secure-1PSID") == "psid_val"
        assert found.get("__Secure-1PSIDTS") == "ts_val"

    def test_read_profile_cookies_copies_wal_and_reads(self, tmp_path: Path) -> None:
        db_file = tmp_path / "cookies.sqlite"
        wal_file = tmp_path / "cookies.sqlite-wal"
        wal_file.write_text("wal-content", encoding="utf-8")
        records = [
            ("__Secure-1PSID", "wal_psid", ".google.com", 100),
            ("__Secure-1PSIDTS", "wal_ts", ".google.com", 100),
        ]
        _populate_test_moz_cookies(db_file, records)

        extracted = _read_profile_cookies(tmp_path, ".google.com")
        assert extracted["__Secure-1PSID"] == "wal_psid"
        assert extracted["__Secure-1PSIDTS"] == "wal_ts"

    def test_read_profile_cookies_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert _read_profile_cookies(tmp_path, ".google.com") == {}

    @pytest.mark.asyncio
    async def test_harvest_cookies_success(self, tmp_path: Path) -> None:
        db_file = tmp_path / "cookies.sqlite"
        records = [
            ("__Secure-1PSID", "harvest_psid_123", ".google.com", 100),
            ("__Secure-1PSIDTS", "harvest_ts_456", ".google.com", 100),
        ]
        _populate_test_moz_cookies(db_file, records)

        mock_session = MagicMock()
        mock_session.profile_dir = tmp_path
        mock_session.proc.poll.return_value = None

        cookies = await harvest_gemini_cookies_via_isolated_firefox(
            timeout_seconds=5.0,
            domain_name=".google.com",
            session=mock_session,
        )
        assert cookies.secure_1psid == "harvest_psid_123"
        assert cookies.secure_1psidts == "harvest_ts_456"
        mock_session.close_and_cleanup.assert_called()

    @pytest.mark.asyncio
    async def test_harvest_cookies_user_closed_window(self, tmp_path: Path) -> None:
        mock_session = MagicMock()
        mock_session.profile_dir = tmp_path
        mock_session.proc.poll.return_value = 0  # Process closed

        with pytest.raises(RuntimeError, match="Firefox was closed by user"):
            await harvest_gemini_cookies_via_isolated_firefox(
                timeout_seconds=5.0,
                domain_name=".google.com",
                session=mock_session,
            )
        mock_session.close_and_cleanup.assert_called()

    @pytest.mark.asyncio
    async def test_harvest_cookies_timeout(self, tmp_path: Path) -> None:
        mock_session = MagicMock()
        mock_session.profile_dir = tmp_path
        mock_session.proc.poll.return_value = None  # Running but no cookies

        with pytest.raises(TimeoutError, match="Timed out after"):
            await harvest_gemini_cookies_via_isolated_firefox(
                timeout_seconds=0.05,
                poll_interval=0.01,
                session=mock_session,
            )
        mock_session.close_and_cleanup.assert_called()


class TestRunOAuthInIsolatedFirefox:
    """Tests for run_oauth_in_isolated_firefox monitoring and lifecycle."""

    @pytest.mark.asyncio
    async def test_run_oauth_success(self) -> None:
        mock_session = MagicMock()
        mock_session.proc.poll.return_value = None

        async def fake_auth_coro() -> str:
            await asyncio.sleep(0.01)
            return "auth_code_xyz_789"

        result = await run_oauth_in_isolated_firefox(
            auth_url="https://accounts.google.com/o/oauth2/v2/auth",
            awaitable_code_task=fake_auth_coro(),
            poll_interval=0.01,
            session=mock_session,
        )
        assert result == "auth_code_xyz_789"
        mock_session.close_and_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_oauth_user_closed_window(self) -> None:
        mock_session = MagicMock()
        mock_session.proc.poll.return_value = 0  # Window closed

        never_completing_task = asyncio.get_running_loop().create_future()

        with pytest.raises(RuntimeError, match="Firefox was closed by user"):
            await run_oauth_in_isolated_firefox(
                auth_url="https://accounts.google.com/o/oauth2/v2/auth",
                awaitable_code_task=never_completing_task,
                poll_interval=0.01,
                session=mock_session,
            )
        assert never_completing_task.cancelled()
        mock_session.close_and_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_oauth_task_raises_error(self) -> None:
        mock_session = MagicMock()
        mock_session.proc.poll.return_value = None

        async def failing_auth_coro() -> None:
            await asyncio.sleep(0.01)
            raise ValueError("State parameter mismatch")

        with pytest.raises(ValueError, match="State parameter mismatch"):
            await run_oauth_in_isolated_firefox(
                auth_url="https://accounts.google.com/o/oauth2/v2/auth",
                awaitable_code_task=failing_auth_coro(),
                poll_interval=0.01,
                session=mock_session,
            )
        mock_session.close_and_cleanup.assert_called_once()
