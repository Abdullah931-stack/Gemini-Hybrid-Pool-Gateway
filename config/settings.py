"""Core configuration, Pydantic v2 state schemas, in-memory registry, and LMDB storage.

Implements Level 0 of the Gemini-Hybrid-Pool-Gateway (v3.2) architecture:
1. Engineering & mathematical constants (model base weights, concurrency caps, smart pacing).
2. Race-free in-memory runtime state models (OAuthAccount, WebAccount, AccountsConfig)
   with PrivateAttr locks and runtime metrics excluded from disk persistence.
3. Single Source of Truth in-memory registry with atomic snapshot persistence
   protected by ``accounts_file_lock``.
4. Dual-table LMDB storage manager (``db_sessions`` and ``db_signatures``).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Literal
import sys
from importlib.machinery import ExtensionFileLoader
from importlib.util import spec_from_file_location

if sys.platform == "win32":
    class _WinAmd64PydFinder:
        @classmethod
        def find_spec(cls, fullname: str, path: Any = None, target: Any = None) -> Any:
            search_paths = path if path is not None else sys.path
            name = fullname.rpartition(".")[2]
            for p in search_paths:
                candidate = os.path.join(p, f"{name}.cp311-win_amd64.pyd")
                if os.path.isfile(candidate):
                    return spec_from_file_location(
                        fullname, candidate, loader=ExtensionFileLoader(fullname, candidate)
                    )
            return None

    if not any(getattr(f, "__name__", "") == "_WinAmd64PydFinder" for f in sys.meta_path):
        sys.meta_path.insert(0, _WinAmd64PydFinder)

import lmdb
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

logger = logging.getLogger(__name__)

# ============================================================================
# 1. System Paths & Engineering / Mathematical Constants
# ============================================================================

BASE_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = BASE_DIR / "config"

ACCOUNTS_FILE_PATH: Path = Path(
    os.getenv("ACCOUNTS_FILE_PATH", str(CONFIG_DIR / "accounts.json"))
)
COOKIES_CACHE_DIR: Path = Path(
    os.getenv("COOKIES_CACHE_DIR", str(CONFIG_DIR / "cookies_cache"))
)
LMDB_DIR: Path = Path(
    os.getenv("LMDB_DIR", os.getenv("LMDB_PATH", str(CONFIG_DIR / "lmdb_data")))
)
LMDB_PATH: Path = LMDB_DIR
LMDB_MAP_SIZE: int = int(os.getenv("LMDB_MAP_SIZE", str(104_857_600)))  # 100 MB

# Cross-platform temporary history directory (/dev/shm on Linux if writable, else OS temp dir)
_SHM_PATH = Path("/dev/shm")
TEMP_HISTORY_DIR: Path = (
    _SHM_PATH
    if _SHM_PATH.exists() and _SHM_PATH.is_dir() and os.access(_SHM_PATH, os.W_OK)
    else Path(tempfile.gettempdir())
)

# Default concurrency limits per account type
OAUTH_MAX_CONCURRENCY_DEFAULT: int = 3
WEB_MAX_CONCURRENCY_DEFAULT: int = 2

# Base optimistic quota weights \Delta_{base}(m) for a single successful request
DELTA_BASE: dict[str, float] = {
    "gemini-4-argon": 0.10,
    "gemini-3.1-pro": 0.03,
    "gemini-3.8-flash": 0.002,
    "gemini-3.1-flash-lite": 0.000,
}
MODEL_BASE_WEIGHTS: dict[str, float] = DELTA_BASE

# Smart Pacing & Hybrid Scheduler constants
WEB_MIN_START_INTERVAL_S: float = 0.5
WEB_FAST_REQUEST_THRESHOLD_S: float = 1.0
WEB_MIN_INTERVAL_S: float = 0.6
WEB_JITTER_RANGE_S: tuple[float, float] = (0.4, 0.8)

SESSION_AFFINITY_MULTIPLIER: float = 1.35
SESSION_AFFINITY_MAX_CONTEXT_CHARS: int = 120_000
REHYDRATION_TIER2_MAX_CHARS: int = 60_000

# Eligibility gate thresholds
OAUTH_MIN_REMAINING_FRACTION: float = 0.02
WEB_MAX_5H_USAGE_PCT: float = 0.95
WEB_MAX_WEEKLY_USAGE_PCT: float = 0.98
WEB_URGENCY_WINDOW_S: int = 18_000


def get_model_base_delta(model_name: str) -> float:
    """Return the base optimistic quota weight ``Delta_base(m)`` for ``model_name``.

    Matches exact names first, then longest matching prefix in ``DELTA_BASE``
    (e.g., ``gemini-3.1-flash-lite-low`` -> ``0.000``), and falls back to family
    keyword heuristics if needed.
    """
    normalized = model_name.strip().lower()
    if normalized in DELTA_BASE:
        return DELTA_BASE[normalized]

    # Sort keys by length descending so 'gemini-3.1-flash-lite' matches before 'gemini-3.1'
    for key in sorted(DELTA_BASE.keys(), key=len, reverse=True):
        if normalized.startswith(key):
            return DELTA_BASE[key]

    if "flash-lite" in normalized:
        return DELTA_BASE["gemini-3.1-flash-lite"]
    if "flash" in normalized:
        return DELTA_BASE["gemini-3.8-flash"]
    if "argon" in normalized:
        return DELTA_BASE["gemini-4-argon"]
    if "pro" in normalized:
        return DELTA_BASE["gemini-3.1-pro"]

    return DELTA_BASE["gemini-3.8-flash"]


# ============================================================================
# 2. Structural Contracts (Pydantic v2 Models) & In-Memory Runtime State
# ============================================================================


class OAuthCredentials(BaseModel):
    """OAuth 2.0 + PKCE credentials for an Antigravity CLI account."""

    model_config = ConfigDict(populate_by_name=True)

    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    project_id: str = ""
    ide_type: str = "ANTIGRAVITY"


class ModelQuota(BaseModel):
    """Per-model quota status inside an OAuth CLI account."""

    model_config = ConfigDict(populate_by_name=True)

    remaining_fraction: float = 1.0
    reset_at: int | None = None
    is_exhausted: bool = False


class OAuthQuotaState(BaseModel):
    """Dynamic quota and error tracking state for an OAuth CLI account."""

    model_config = ConfigDict(populate_by_name=True)

    model_quotas: dict[str, ModelQuota] = Field(default_factory=dict)
    cooldown_until: int | None = None
    consecutive_errors: int = 0
    last_error_reason: str | None = None


class OAuthAccount(BaseModel):
    """OAuth CLI account model combining persistent JSON fields and in-memory runtime state."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str = ""
    transport_mode: Literal["http_v1internal", "agy_subprocess"] = "http_v1internal"
    max_concurrency: int = OAUTH_MAX_CONCURRENCY_DEFAULT
    proxy: str | None = None
    credentials: OAuthCredentials = Field(default_factory=OAuthCredentials)
    status: Literal["ACTIVE", "COOLDOWN", "SUSPENDED"] = "ACTIVE"
    discovered_models: list[str] = Field(default_factory=list)
    quota_state: OAuthQuotaState = Field(default_factory=OAuthQuotaState)

    # In-memory runtime state (never serialized to disk)
    _active_requests: int = PrivateAttr(default=0)
    _recent_latency_ms: float = PrivateAttr(default=0.0)
    _token_refresh_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    @property
    def active_requests(self) -> int:
        """Number of currently active concurrent requests on this OAuth account."""
        return self._active_requests

    @active_requests.setter
    def active_requests(self, active_count: int) -> None:
        self._active_requests = max(0, int(active_count))

    @property
    def recent_latency_ms(self) -> float:
        """Exponential moving average (EMA) of recent response latency in milliseconds."""
        return self._recent_latency_ms

    @recent_latency_ms.setter
    def recent_latency_ms(self, latency_ms: float) -> None:
        self._recent_latency_ms = max(0.0, float(latency_ms))

    @property
    def token_refresh_lock(self) -> asyncio.Lock:
        """Per-account asyncio lock preventing duplicate in-flight token refreshes."""
        return self._token_refresh_lock

    @token_refresh_lock.setter
    def token_refresh_lock(self, lock: asyncio.Lock) -> None:
        self._token_refresh_lock = lock


class WebCookies(BaseModel):
    """Session cookies for a Google AI Pro web account."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    secure_1psid: str = Field(default="", alias="__Secure-1PSID")
    secure_1psidts: str = Field(default="", alias="__Secure-1PSIDTS")

    def to_dict(self) -> dict[str, str]:
        """Return cookies as a dictionary keyed by standard browser cookie names."""
        return {
            "__Secure-1PSID": self.secure_1psid,
            "__Secure-1PSIDTS": self.secure_1psidts,
        }

    def __getitem__(self, cookie_name: str) -> str:
        if cookie_name in ("__Secure-1PSID", "secure_1psid"):
            return self.secure_1psid
        if cookie_name in ("__Secure-1PSIDTS", "secure_1psidts"):
            return self.secure_1psidts
        raise KeyError(cookie_name)

    def __setitem__(self, cookie_name: str, cookie_value: str) -> None:
        if cookie_name in ("__Secure-1PSID", "secure_1psid"):
            self.secure_1psid = cookie_value
        elif cookie_name in ("__Secure-1PSIDTS", "secure_1psidts"):
            self.secure_1psidts = cookie_value
        else:
            raise KeyError(cookie_name)

    def get(self, cookie_name: str, default: str = "") -> str:
        """Dictionary-compatible ``get`` helper."""
        try:
            return self[cookie_name]
        except KeyError:
            return default


class WebUsage(BaseModel):
    """Scraped usage window percentages and reset timestamps for a Web account."""

    model_config = ConfigDict(populate_by_name=True)

    five_hour_pct: float = 0.0
    five_hour_reset_at: int | None = None
    weekly_pct: float = 0.0
    weekly_reset_at: int | None = None
    last_synced_at: int | None = None
    tier: str = "FREE"


class WebAccount(BaseModel):
    """Web Cookie account model combining persistent JSON fields and in-memory runtime state."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str = ""
    max_concurrency: int = WEB_MAX_CONCURRENCY_DEFAULT
    proxy: str | None = None
    cookie_file: str = ""
    cookies: WebCookies = Field(default_factory=WebCookies)
    status: Literal["ACTIVE", "COOLDOWN", "SUSPENDED"] = "ACTIVE"
    discovered_models: list[str] = Field(default_factory=list)
    usage: WebUsage = Field(default_factory=WebUsage)
    cooldown_until: int | None = None
    consecutive_errors: int = 0
    last_error_reason: str | None = None

    @property
    def tier(self) -> str:
        """Subscription tier (e.g. 'FREE', 'PRO', 'ULTRA') from usage sync."""
        return self.usage.tier

    @tier.setter
    def tier(self, value: str) -> None:
        self.usage.tier = value

    # In-memory runtime state (never serialized to disk)
    _active_requests: int = PrivateAttr(default=0)
    _cookie_write_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _conversation_locks: dict[str, asyncio.Lock] = PrivateAttr(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )
    _reinit_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _last_started_timestamp: float = PrivateAttr(default=0.0)
    _last_completed_timestamp: float = PrivateAttr(default=0.0)
    _optimistic_5h_delta: float = PrivateAttr(default=0.0)
    _optimistic_weekly_delta: float = PrivateAttr(default=0.0)

    @property
    def active_requests(self) -> int:
        """Number of currently active concurrent requests on this Web account."""
        return self._active_requests

    @active_requests.setter
    def active_requests(self, active_count: int) -> None:
        self._active_requests = max(0, int(active_count))

    @property
    def cookie_write_lock(self) -> asyncio.Lock:
        """Per-account lock protecting isolated cookie cache file writes on disk."""
        return self._cookie_write_lock

    @cookie_write_lock.setter
    def cookie_write_lock(self, lock: asyncio.Lock) -> None:
        self._cookie_write_lock = lock

    @property
    def conversation_locks(self) -> dict[str, asyncio.Lock]:
        """Per-conversation ``cid`` locks serializing delta turns on the same chat."""
        return self._conversation_locks

    @conversation_locks.setter
    def conversation_locks(self, locks: dict[str, asyncio.Lock]) -> None:
        self._conversation_locks = locks

    def get_conversation_lock(self, cid: str) -> asyncio.Lock:
        """Return (and lazily create) the ``asyncio.Lock`` for conversation ``cid``."""
        return self._conversation_locks[cid]

    @property
    def reinit_lock(self) -> asyncio.Lock:
        """Per-account lock guarding ``GeminiClient`` re-initialization."""
        return self._reinit_lock

    @reinit_lock.setter
    def reinit_lock(self, lock: asyncio.Lock) -> None:
        self._reinit_lock = lock

    @property
    def last_started_timestamp(self) -> float:
        """Unix timestamp (seconds) when the most recent request started on this account."""
        return self._last_started_timestamp

    @last_started_timestamp.setter
    def last_started_timestamp(self, timestamp_s: float) -> None:
        self._last_started_timestamp = float(timestamp_s)

    @property
    def last_completed_timestamp(self) -> float:
        """Unix timestamp (seconds) when the most recent request completed on this account."""
        return self._last_completed_timestamp

    @last_completed_timestamp.setter
    def last_completed_timestamp(self, timestamp_s: float) -> None:
        self._last_completed_timestamp = float(timestamp_s)

    @property
    def optimistic_5h_delta(self) -> float:
        """Locally accumulated context-scaled 5-hour usage delta since last sync."""
        return self._optimistic_5h_delta

    @optimistic_5h_delta.setter
    def optimistic_5h_delta(self, delta_pct: float) -> None:
        self._optimistic_5h_delta = max(0.0, float(delta_pct))

    @property
    def optimistic_weekly_delta(self) -> float:
        """Locally accumulated context-scaled weekly usage delta since last sync."""
        return self._optimistic_weekly_delta

    @optimistic_weekly_delta.setter
    def optimistic_weekly_delta(self, delta_pct: float) -> None:
        self._optimistic_weekly_delta = max(0.0, float(delta_pct))


class AccountsConfig(BaseModel):
    """Root configuration schema representing ``config/accounts.json``."""

    model_config = ConfigDict(populate_by_name=True)

    oauth_accounts: list[OAuthAccount] = Field(default_factory=list)
    web_accounts: list[WebAccount] = Field(default_factory=list)


# ============================================================================
# 3. In-Memory Single Source of Truth & Atomic Snapshot Persistence
# ============================================================================

accounts_file_lock: asyncio.Lock = asyncio.Lock()


class AccountsStateRegistry:
    """In-memory Single Source of Truth for all OAuth and Web accounts during runtime."""

    def __init__(self) -> None:
        self._config: AccountsConfig = AccountsConfig()
        self._loaded: bool = False

    def reset(self) -> None:
        """Reset the in-memory registry to a fresh empty configuration."""
        self._config = AccountsConfig()
        self._loaded = False

    @property
    def config(self) -> AccountsConfig:
        """Return the active in-memory ``AccountsConfig`` instance."""
        return self._config

    @config.setter
    def config(self, new_config: AccountsConfig) -> None:
        self._config = new_config
        self._loaded = True

    @property
    def oauth_accounts(self) -> list[OAuthAccount]:
        """Return the in-memory list of ``OAuthAccount`` objects."""
        return self._config.oauth_accounts

    @property
    def web_accounts(self) -> list[WebAccount]:
        """Return the in-memory list of ``WebAccount`` objects."""
        return self._config.web_accounts

    def load_from_disk(self, file_path: Path | str | None = None) -> AccountsConfig:
        """Read ``config/accounts.json`` into memory as the Single Source of Truth.

        If the file does not exist or is empty, initializes an empty ``AccountsConfig``.
        """
        target_path = Path(file_path) if file_path is not None else ACCOUNTS_FILE_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not target_path.exists():
            self._config = AccountsConfig()
            self._loaded = True
            return self._config

        raw_text = target_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            self._config = AccountsConfig()
            self._loaded = True
            return self._config

        raw_config = json.loads(raw_text)
        self._config = AccountsConfig.model_validate(raw_config)
        self._loaded = True
        return self._config

    def get_oauth_account(self, account_id: str) -> OAuthAccount | None:
        """Find an ``OAuthAccount`` in memory by its ``id``."""
        for account in self._config.oauth_accounts:
            if account.id == account_id:
                return account
        return None

    def get_web_account(self, account_id: str) -> WebAccount | None:
        """Find a ``WebAccount`` in memory by its ``id``."""
        for account in self._config.web_accounts:
            if account.id == account_id:
                return account
        return None

    def upsert_oauth_account(self, account: OAuthAccount) -> OAuthAccount:
        """Insert or update an ``OAuthAccount`` in memory while preserving runtime locks."""
        for idx, existing in enumerate(self._config.oauth_accounts):
            if existing.id == account.id:
                account.active_requests = existing.active_requests
                account.recent_latency_ms = existing.recent_latency_ms
                account.token_refresh_lock = existing.token_refresh_lock
                self._config.oauth_accounts[idx] = account
                return account
        self._config.oauth_accounts.append(account)
        return account

    def upsert_web_account(self, account: WebAccount) -> WebAccount:
        """Insert or update a ``WebAccount`` in memory while preserving runtime locks."""
        for idx, existing in enumerate(self._config.web_accounts):
            if existing.id == account.id:
                account.active_requests = existing.active_requests
                account.cookie_write_lock = existing.cookie_write_lock
                account.conversation_locks = existing.conversation_locks
                account.reinit_lock = existing.reinit_lock
                account.last_started_timestamp = existing.last_started_timestamp
                account.last_completed_timestamp = existing.last_completed_timestamp
                account.optimistic_5h_delta = existing.optimistic_5h_delta
                account.optimistic_weekly_delta = existing.optimistic_weekly_delta
                self._config.web_accounts[idx] = account
                return account
        self._config.web_accounts.append(account)
        return account

    def remove_oauth_account(self, account_id: str) -> bool:
        """Remove an ``OAuthAccount`` by ``id`` from memory. Returns True if removed."""
        before = len(self._config.oauth_accounts)
        self._config.oauth_accounts = [
            acc for acc in self._config.oauth_accounts if acc.id != account_id
        ]
        return len(self._config.oauth_accounts) < before

    def remove_web_account(self, account_id: str) -> bool:
        """Remove a ``WebAccount`` by ``id`` from memory. Returns True if removed."""
        before = len(self._config.web_accounts)
        self._config.web_accounts = [
            acc for acc in self._config.web_accounts if acc.id != account_id
        ]
        return len(self._config.web_accounts) < before

    def remove_account(self, account_id: str) -> bool:
        """Remove an account by ``id`` from either OAuth or Web pool in memory."""
        removed_oauth = self.remove_oauth_account(account_id)
        removed_web = self.remove_web_account(account_id)
        return removed_oauth or removed_web


state_registry = AccountsStateRegistry()
accounts_registry = state_registry


def load_accounts_into_memory(file_path: Path | str | None = None) -> AccountsConfig:
    """Load ``config/accounts.json`` into the global in-memory state registry."""
    return state_registry.load_from_disk(file_path=file_path)


def load_accounts_snapshot(file_path: Path | str | None = None) -> AccountsConfig:
    """Alias for ``load_accounts_into_memory``."""
    return state_registry.load_from_disk(file_path=file_path)


def get_accounts_config() -> AccountsConfig:
    """Return the active in-memory ``AccountsConfig`` from the global registry."""
    return state_registry.config


async def _replace_file_with_windows_retry(tmp_path: Path, target_path: Path) -> None:
    """Atomically replace ``target_path`` with ``tmp_path``, retrying transient Windows file locks."""
    for attempt in range(5):
        try:
            os.replace(tmp_path, target_path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.005 * (attempt + 1))


async def save_accounts_snapshot(
    config: AccountsConfig | None = None,
    file_path: Path | str | None = None,
) -> None:
    """Atomically persist the in-memory ``AccountsConfig`` snapshot to disk.

    Protected exclusively by ``accounts_file_lock``. Writes human-readable JSON
    to ``accounts.json.tmp``, flushes and fsyncs the file descriptor, and
    atomically replaces ``accounts.json`` via ``os.replace()``.
    """
    async with accounts_file_lock:
        if config is not None:
            state_registry.config = config
        target_path = Path(file_path) if file_path is not None else ACCOUNTS_FILE_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f"{target_path.name}.tmp")

        payload = state_registry.config.model_dump(mode="json", by_alias=True)
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(serialized)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        await _replace_file_with_windows_retry(tmp_path, target_path)


# ============================================================================
# 4. LMDB Tables (`db_sessions` & `db_signatures`)
# ============================================================================


class SessionRecord(BaseModel):
    """Schema for a ``db_sessions`` value linking a prefix hash to a Web chat session."""

    account_id: str
    metadata: list[str] = Field(default_factory=list)
    session_id: str = ""


class LMDBStore:
    """Wrapper around LMDB managing ``db_sessions`` and ``db_signatures`` tables."""

    def __init__(
        self,
        path: Path | str | None = None,
        map_size: int = LMDB_MAP_SIZE,
    ) -> None:
        self.path: Path = Path(path) if path is not None else LMDB_DIR
        self.map_size: int = map_size
        self.path.mkdir(parents=True, exist_ok=True)

        self.env: lmdb.Environment = lmdb.open(
            str(self.path),
            max_dbs=4,
            map_size=self.map_size,
            subdir=True,
            create=True,
            lock=True,
        )
        self.db_sessions = self.env.open_db(b"db_sessions", create=True)
        self.db_signatures = self.env.open_db(b"db_signatures", create=True)

    # ---- db_sessions operations ----

    def put_session(
        self,
        arg1: str,
        account_id: str,
        arg3: list[str] | Sequence[str] | None = None,
        *,
        metadata: list[str] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Store or update Web session record(s) under prefix hash(es) in ``db_sessions``."""
        if metadata is not None:
            # Batch / test signature: put_session(session_id, account_id, hashes_list, metadata=...)
            eff_session_id = arg1
            hashes = list(arg3) if isinstance(arg3, (list, tuple)) else [arg1]
            eff_metadata = metadata
        elif isinstance(arg3, (list, tuple)):
            # Standard signature: put_session(prefix_hash, account_id, metadata)
            eff_session_id = session_id or ""
            hashes = [arg1]
            eff_metadata = list(arg3)
        else:
            eff_session_id = session_id or ""
            hashes = [arg1]
            eff_metadata = []

        record = SessionRecord(
            account_id=account_id,
            metadata=list(eff_metadata),
            session_id=eff_session_id,
        )
        val_bytes = json.dumps(
            record.model_dump(mode="json"), ensure_ascii=False
        ).encode("utf-8")
        with self.env.begin(db=self.db_sessions, write=True) as txn:
            for h in hashes:
                txn.put(str(h).encode("utf-8"), val_bytes)

    def get_session(self, prefix_hash: str) -> dict[str, Any] | None:
        """Retrieve the session dict ``{"account_id": str, "metadata": list[str]}`` for ``prefix_hash``."""
        key_bytes = prefix_hash.encode("utf-8")
        with self.env.begin(db=self.db_sessions, write=False) as txn:
            raw_bytes = txn.get(key_bytes)
            if raw_bytes is None:
                return None
            try:
                raw_record = json.loads(raw_bytes.decode("utf-8"))
                record = SessionRecord.model_validate(raw_record)
                result: dict[str, Any] = {
                    "account_id": record.account_id,
                    "metadata": list(record.metadata),
                }
                if record.session_id:
                    result["session_id"] = record.session_id
                return result
            except (ValueError, KeyError) as exc:
                logger.warning("Corrupted session entry for %s: %s", prefix_hash, exc)
                return None

    def get_session_record(self, prefix_hash: str) -> SessionRecord | None:
        """Retrieve a validated ``SessionRecord`` model for ``prefix_hash``."""
        key_bytes = prefix_hash.encode("utf-8")
        with self.env.begin(db=self.db_sessions, write=False) as txn:
            raw_bytes = txn.get(key_bytes)
            if raw_bytes is None:
                return None
            try:
                raw_record = json.loads(raw_bytes.decode("utf-8"))
                return SessionRecord.model_validate(raw_record)
            except (ValueError, KeyError) as exc:
                logger.warning("Corrupted session entry for %s: %s", prefix_hash, exc)
                return None

    def delete_session(self, prefix_hash: str) -> bool:
        """Delete a session entry by ``prefix_hash``. Returns True if existed."""
        key_bytes = prefix_hash.encode("utf-8")
        with self.env.begin(db=self.db_sessions, write=True) as txn:
            return bool(txn.delete(key_bytes))

    def _find_session_keys_for_account(self, account_id: str) -> list[bytes]:
        """Collect all raw ``db_sessions`` keys belonging to ``account_id``."""
        matching_keys: list[bytes] = []
        with self.env.begin(db=self.db_sessions, write=False) as txn:
            for key_bytes, val_bytes in txn.cursor():
                try:
                    payload = json.loads(val_bytes.decode("utf-8"))
                    if payload.get("account_id") == account_id:
                        matching_keys.append(bytes(key_bytes))
                except ValueError:
                    continue
        return matching_keys

    def delete_sessions_for_account(self, account_id: str) -> int:
        """Delete all session entries bound to ``account_id``. Returns count of deleted keys."""
        keys_to_delete = self._find_session_keys_for_account(account_id)
        if not keys_to_delete:
            return 0
        deleted_count = 0
        with self.env.begin(db=self.db_sessions, write=True) as txn:
            for key_bytes in keys_to_delete:
                if txn.delete(key_bytes):
                    deleted_count += 1
        return deleted_count

    # ---- db_signatures operations ----

    def put_signature(self, tool_call_id: str, thought_signature: str) -> None:
        """Store a ``thoughtSignature`` mapped to ``tool_call_id`` in ``db_signatures``."""
        key_bytes = tool_call_id.encode("utf-8")
        val_bytes = thought_signature.encode("utf-8")
        with self.env.begin(db=self.db_signatures, write=True) as txn:
            txn.put(key_bytes, val_bytes)

    def get_signature(self, tool_call_id: str) -> str | None:
        """Retrieve the ``thoughtSignature`` for ``tool_call_id``, or None if absent."""
        key_bytes = tool_call_id.encode("utf-8")
        with self.env.begin(db=self.db_signatures, write=False) as txn:
            raw = txn.get(key_bytes)
            if raw is None:
                return None
            return raw.decode("utf-8")

    def delete_signature(self, tool_call_id: str) -> bool:
        """Delete a ``thoughtSignature`` entry by ``tool_call_id``. Returns True if existed."""
        key_bytes = tool_call_id.encode("utf-8")
        with self.env.begin(db=self.db_signatures, write=True) as txn:
            return bool(txn.delete(key_bytes))

    def close(self) -> None:
        """Sync and close the underlying LMDB environment."""
        if getattr(self, "env", None) is not None:
            self.env.sync()
            self.env.close()


_lmdb_store_instance: LMDBStore | None = None


def init_lmdb(
    path: Path | str | None = None,
    map_size: int = LMDB_MAP_SIZE,
) -> LMDBStore:
    """Initialize (or re-initialize if path differs) the singleton ``LMDBStore``."""
    global _lmdb_store_instance
    target_path = Path(path) if path is not None else LMDB_DIR
    if _lmdb_store_instance is not None:
        if _lmdb_store_instance.path.resolve() == target_path.resolve():
            return _lmdb_store_instance
        _lmdb_store_instance.close()
    _lmdb_store_instance = LMDBStore(path=target_path, map_size=map_size)
    return _lmdb_store_instance


def get_lmdb_store() -> LMDBStore:
    """Return the active singleton ``LMDBStore``, opening it lazily if needed."""
    global _lmdb_store_instance
    if _lmdb_store_instance is None:
        _lmdb_store_instance = LMDBStore(path=LMDB_DIR, map_size=LMDB_MAP_SIZE)
    return _lmdb_store_instance


def close_lmdb() -> None:
    """Close and reset the singleton ``LMDBStore`` if open."""
    global _lmdb_store_instance
    if _lmdb_store_instance is not None:
        _lmdb_store_instance.close()
        _lmdb_store_instance = None
