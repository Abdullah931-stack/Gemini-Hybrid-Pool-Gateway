"""Cross-platform hybrid context emulator, prefix differ, and 3-tier rehydration engine.

Implements Level 2 (Task 4 of Phase 3) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. Sequential Prefix Hashing (SHA-256): Canonical message normalization and cumulative
   prefix digest computation for O(k) longest-prefix lookups in LMDB ``db_sessions``.
2. Adaptive Session Mode: Uses ``temporary=True`` for single-turn tool-less queries to
   avoid polluting the user's Google Web history, and ``temporary=False`` for multi-turn
   agent trajectories or tool-assisted requests, paired with 60-minute idle chat GC.
3. 3-Tier Cross-Platform Context Rehydration Engine (ADR-08):
   - Tier 1 (``tier1_prefix_hit``): Same-account prefix hit in ``db_sessions``; sends only
     delta messages inside the existing ``ChatSession`` under ``conversation_locks[cid]``
     with ``<tool_response call_id="..." name="...">`` formatting.
   - Tier 2 (``tier2_inline_history``): New session or account failover with context
     <= 60,000 characters; embeds ``messages[:-1]`` inside ``<conversation_history>``
     followed by the active message ``messages[-1]``.
   - Tier 3 (``tier3_temp_file_passthrough``): Account failover or uncached session with
     context > 60,000 characters; writes ``messages[:-3]`` into a temporary file inside
     ``TEMP_HISTORY_DIR`` (``/dev/shm`` on Linux if writable, else ``tempfile.gettempdir()``),
     pins ``system``, ``tools``, and ``messages[-3:]`` in ``prompt``, and deterministically
     removes the file inside ``finally:`` as well as on server startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from config.settings import (
    REHYDRATION_TIER2_MAX_CHARS,
    SESSION_AFFINITY_MAX_CONTEXT_CHARS,
    TEMP_HISTORY_DIR,
    LMDBStore,
    WebAccount,
    get_lmdb_store,
)
from core.schema_sanitizer import sanitize_messages_waf, sanitize_system_prompt_text
from core.token_saver import sanitize_tool_messages

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Module Constants & Tier Definitions
# ============================================================================

IDLE_CHAT_TTL_SECONDS: Final[int] = 3600
TEMP_FILE_PREFIX: Final[str] = "harness_history_"
TEMP_FILE_GLOB: Final[str] = "harness_history_*.txt"

TIER1_PREFIX_HIT: Final[Literal["tier1_prefix_hit"]] = "tier1_prefix_hit"
TIER2_INLINE_HISTORY: Final[Literal["tier2_inline_history"]] = "tier2_inline_history"
TIER3_TEMP_FILE_PASSTHROUGH: Final[Literal["tier3_temp_file_passthrough"]] = (
    "tier3_temp_file_passthrough"
)

RehydrationTier = Literal[
    "tier1_prefix_hit",
    "tier2_inline_history",
    "tier3_temp_file_passthrough",
]

_SYSTEM_ROLES: Final[frozenset[str]] = frozenset({"system", "developer"})
_HISTORY_ROLES_REQUIRING_PERSISTENCE: Final[frozenset[str]] = frozenset(
    {"assistant", "model", "tool", "function"}
)


# ============================================================================
# 2. Data Contracts (PrefixMatchResult, RehydrationPlan, ActiveChatSession)
# ============================================================================


class PrefixMatchResult(BaseModel):
    """Longest matching prefix session retrieved from LMDB ``db_sessions``."""

    model_config = ConfigDict(populate_by_name=True)

    prefix_hash: str
    matched_count: int
    account_id: str
    session_id: str = ""
    metadata: list[str] = Field(default_factory=list)
    delta_messages: list[dict[str, Any]] = Field(default_factory=list)


class RehydrationPlan(BaseModel):
    """Prepared execution plan produced by the 3-Tier Context Rehydration Engine."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    tier: RehydrationTier
    temporary: bool
    prompt: str
    files: list[str | Path] = Field(default_factory=list)
    temp_file_path: Path | None = None
    resume_metadata: list[str | None] | None = None
    conversation_cid: str | None = None
    full_prefix_hash: str
    delta_messages: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class ActiveChatSession:
    """In-memory activity tracker entry for non-temporary Google Web chats."""

    cid: str
    account_id: str
    last_active_at: float
    prefix_hashes: set[str] = field(default_factory=set)


_active_chats_registry: dict[str, ActiveChatSession] = {}


# ============================================================================
# 3. Sequential Prefix Hashing (SHA-256) & Context Length Calculation
# ============================================================================


def _extract_part_text(part: Any) -> str:
    """Extract text from a single multipart content item."""
    if isinstance(part, str):
        return part
    if isinstance(part, Mapping):
        if isinstance(part.get("text"), str):
            return part["text"]
        if isinstance(part.get("content"), str):
            return part["content"]
        return json.dumps(
            dict(part), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return str(part)


def _extract_text_from_content(raw_content: Any) -> str:
    """Convert string, list-of-parts, or structured message content into deterministic text."""
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, Sequence) and not isinstance(raw_content, (bytes, bytearray)):
        return "\n".join(_extract_part_text(part) for part in raw_content)
    if isinstance(raw_content, Mapping):
        return json.dumps(
            dict(raw_content), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return str(raw_content)


def _canonicalize_tool_arguments(raw_args: Any) -> str:
    """Serialize tool call arguments into a deterministic JSON string."""
    if isinstance(raw_args, str):
        stripped_args = raw_args.strip()
        if not stripped_args:
            return ""
        try:
            parsed_args = json.loads(stripped_args)
            return json.dumps(
                parsed_args, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except ValueError:
            return stripped_args
    if isinstance(raw_args, Mapping):
        return json.dumps(
            dict(raw_args), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return str(raw_args or "")


_TOOL_CALL_TAG_RE = re.compile(
    r"<tool_call(?:\s+[^>]*)?>(.*?)</tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_CODE_FENCE_CLEANUP_RE = re.compile(
    r"```(?:xml|json)?\s*```",
    re.IGNORECASE,
)


def _extract_embedded_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract <tool_call> blocks from assistant message content and return (cleaned_text, tool_calls)."""
    if not isinstance(text, str) or "<tool_call" not in text.lower():
        return text, []

    tool_calls: list[dict[str, Any]] = []
    for match in _TOOL_CALL_TAG_RE.finditer(text):
        raw_payload = match.group(1).strip()
        try:
            parsed = json.loads(raw_payload)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or (
                        item.get("function", {}).get("name")
                        if isinstance(item.get("function"), dict)
                        else ""
                    )
                    args = item.get("arguments") or (
                        item.get("function", {}).get("arguments")
                        if isinstance(item.get("function"), dict)
                        else ""
                    )
                    tool_calls.append(
                        {
                            "id": "",
                            "type": "function",
                            "function": {
                                "name": str(name or ""),
                                "arguments": _canonicalize_tool_arguments(args),
                            },
                        }
                    )
        except Exception:
            continue

    cleaned = _TOOL_CALL_TAG_RE.sub("", text)
    cleaned = _CODE_FENCE_CLEANUP_RE.sub("", cleaned).strip()
    return cleaned, tool_calls


def _normalize_single_tool_call(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize a single OpenAI-style tool call mapping for deterministic hashing."""
    func_raw = tool_call.get("function")
    func_map = func_raw if isinstance(func_raw, Mapping) else {}
    raw_args = func_map.get("arguments", tool_call.get("arguments", ""))
    return {
        "id": "",
        "type": str(tool_call.get("type") or "function"),
        "function": {
            "name": str(func_map.get("name") or tool_call.get("name") or ""),
            "arguments": _canonicalize_tool_arguments(raw_args),
        },
    }


def _normalize_tool_calls_list(raw_tool_calls: Any) -> list[dict[str, Any]]:
    """Canonicalize a sequence of tool calls on an assistant message."""
    if not isinstance(raw_tool_calls, Sequence) or isinstance(
        raw_tool_calls, (str, bytes, bytearray)
    ):
        return []
    return [
        _normalize_single_tool_call(tc)
        for tc in raw_tool_calls
        if isinstance(tc, Mapping)
    ]


def normalize_message_for_hash(message: Mapping[str, Any]) -> str:
    """Normalize a chat message into a deterministic canonical JSON string for SHA-256 hashing."""
    role = str(message.get("role") or "").strip().lower()
    raw_content = message.get("content")
    if isinstance(raw_content, list):
        canonical_content: Any = [
            dict(part) if isinstance(part, Mapping) else part for part in raw_content
        ]
    elif raw_content is None:
        canonical_content = ""
    else:
        canonical_content = _extract_text_from_content(raw_content)

    tool_calls = _normalize_tool_calls_list(message.get("tool_calls"))

    # H-3.1: If assistant message has embedded <tool_call> in content, extract and unify tool_calls
    if role in ("assistant", "model") and isinstance(canonical_content, str) and "<tool_call" in canonical_content.lower():
        cleaned_content, extracted_calls = _extract_embedded_tool_calls(canonical_content)
        canonical_content = cleaned_content
        if not tool_calls and extracted_calls:
            tool_calls = extracted_calls

    canonical_payload: dict[str, Any] = {"role": role, "content": canonical_content}
    if tool_calls:
        canonical_payload["tool_calls"] = tool_calls

    tool_call_id = message.get("tool_call_id") or message.get("call_id")
    if tool_call_id is not None:
        canonical_payload["tool_call_id"] = str(tool_call_id)
    if message.get("name") is not None:
        canonical_payload["name"] = str(message.get("name"))

    return json.dumps(
        canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_prefix_hashes(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Compute cumulative SHA-256 hex digests for every prefix ``messages[:j+1]``."""
    if not messages:
        return []

    prefix_hashes: list[str] = []
    running_hasher = hashlib.sha256()
    for idx, message in enumerate(messages):
        canonical_msg = normalize_message_for_hash(message)
        frame = f"{idx}:{len(canonical_msg)}:{canonical_msg}\n".encode("utf-8")
        running_hasher.update(frame)
        prefix_hashes.append(running_hasher.hexdigest())
    return prefix_hashes


def _single_message_char_length(message: Mapping[str, Any]) -> int:
    """Return total character length of a single message including its tool calls."""
    char_count = len(_extract_text_from_content(message.get("content")))
    raw_tool_calls = message.get("tool_calls")
    if isinstance(raw_tool_calls, Sequence) and not isinstance(
        raw_tool_calls, (str, bytes, bytearray)
    ):
        for tc in raw_tool_calls:
            if isinstance(tc, Mapping):
                char_count += len(
                    json.dumps(dict(tc), separators=(",", ":"), ensure_ascii=False)
                )
    return char_count


def compute_messages_char_length(
    messages: Sequence[Mapping[str, Any]],
    system_prompt: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Calculate total character count across messages, system prompt, and tool schemas."""
    total_chars = len(system_prompt) if system_prompt else 0
    total_chars += sum(_single_message_char_length(msg) for msg in messages)
    if tools:
        total_chars += sum(
            len(json.dumps(dict(tool), separators=(",", ":"), ensure_ascii=False))
            for tool in tools
            if isinstance(tool, Mapping)
        )
    return total_chars


calculate_messages_char_length = compute_messages_char_length


# ============================================================================
# 4. Adaptive Session Mode & Idle Chat Garbage Collection (60-Min TTL)
# ============================================================================


def should_use_temporary_session(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Determine whether to use ``temporary=True`` for a Google Web chat session.

    Returns ``True`` only for simple single-turn queries without ``tools`` and without
    any prior conversation turns (at most one ``user`` message, optional ``system``
    or ``developer`` messages, and zero ``assistant`` / ``tool`` messages).
    Returns ``False`` for multi-turn agent conversations or requests containing ``tools``.
    """
    if tools and len(tools) > 0:
        return False

    user_message_count = 0
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role in _HISTORY_ROLES_REQUIRING_PERSISTENCE:
            return False
        if message.get("tool_calls") or message.get("tool_call_id"):
            return False
        if role == "user":
            user_message_count += 1
            if user_message_count > 1:
                return False

    return True


def register_chat_activity(
    cid: str,
    account_id: str,
    *,
    prefix_hash: str | None = None,
    timestamp: float | None = None,
) -> None:
    """Record or refresh activity timestamp for a persistent Web chat ``cid``."""
    if not cid:
        return
    now_ts = float(timestamp) if timestamp is not None else time.time()
    entry = _active_chats_registry.get(cid)
    if entry is None:
        entry = ActiveChatSession(cid=cid, account_id=account_id, last_active_at=now_ts)
        _active_chats_registry[cid] = entry
    else:
        entry.account_id = account_id
        entry.last_active_at = now_ts

    if prefix_hash:
        entry.prefix_hashes.add(prefix_hash)


def unregister_chat_activity(cid: str) -> bool:
    """Remove ``cid`` from the in-memory active chat registry."""
    return _active_chats_registry.pop(cid, None) is not None


def get_tracked_chats() -> dict[str, ActiveChatSession]:
    """Return a shallow copy of the tracked active Web chat sessions."""
    return dict(_active_chats_registry)


def clear_tracked_chats() -> None:
    """Clear all tracked active Web chat sessions from memory."""
    _active_chats_registry.clear()


def _find_lmdb_keys_for_cid(store: LMDBStore, cid: str) -> list[bytes]:
    """Scan ``db_sessions`` for keys whose ``metadata[0]`` matches ``cid``."""
    matching_keys: list[bytes] = []
    with store.env.begin(db=store.db_sessions, write=False) as txn:
        for key_bytes, val_bytes in txn.cursor():
            try:
                payload = json.loads(val_bytes.decode("utf-8"))
                metadata = payload.get("metadata")
                if isinstance(metadata, list) and metadata and metadata[0] == cid:
                    matching_keys.append(bytes(key_bytes))
            except ValueError:
                continue
    return matching_keys


def _purge_cid_from_lmdb(
    store: LMDBStore,
    cid: str,
    known_prefix_hashes: set[str],
) -> None:
    """Delete all ``db_sessions`` entries referencing conversation ``cid``."""
    for prefix_hash in known_prefix_hashes:
        store.delete_session(prefix_hash)

    matching_keys = _find_lmdb_keys_for_cid(store, cid)
    if matching_keys:
        with store.env.begin(db=store.db_sessions, write=True) as txn:
            for key_bytes in matching_keys:
                txn.delete(key_bytes)


async def _delete_remote_chat_safely(client: Any, cid: str) -> None:
    """Invoke ``client.delete_chat(cid)`` if available, handling sync or async methods."""
    if client is None or not hasattr(client, "delete_chat"):
        return
    try:
        outcome = client.delete_chat(cid)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete expired remote chat %s: %s", cid, exc)


async def cleanup_expired_chats(
    client_resolver: Callable[[str], Any],
    *,
    max_idle_seconds: float = IDLE_CHAT_TTL_SECONDS,
    lmdb_store: LMDBStore | None = None,
    now: float | None = None,
) -> int:
    """Delete Web chats idle for at least ``max_idle_seconds`` from Google and LMDB."""
    current_ts = float(now) if now is not None else time.time()
    expired_entries = [
        entry
        for entry in list(_active_chats_registry.values())
        if (current_ts - entry.last_active_at) >= max_idle_seconds
    ]
    if not expired_entries:
        return 0

    store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    for entry in expired_entries:
        try:
            resolved = client_resolver(entry.account_id)
            client = await resolved if inspect.isawaitable(resolved) else resolved
            await _delete_remote_chat_safely(client, entry.cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Error deleting remote chat %s for account %s: %s",
                entry.cid,
                entry.account_id,
                exc,
            )
        _purge_cid_from_lmdb(store, entry.cid, entry.prefix_hashes)
        _active_chats_registry.pop(entry.cid, None)

    return len(expired_entries)


# ============================================================================
# 5. Longest Prefix Match in LMDB (`db_sessions`)
# ============================================================================


def _copy_messages_as_dicts(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert a sequence of message mappings into a list of mutable shallow dicts."""
    return [dict(msg) for msg in messages]


def find_longest_prefix_session(
    messages: Sequence[Mapping[str, Any]],
    *,
    lmdb_store: LMDBStore | None = None,
    require_delta: bool = False,
    target_account_id: str | None = None,
    excluded_account_ids: Set[str] | Sequence[str] | None = None,
    **kwargs: Any,
) -> PrefixMatchResult | None:
    """Find the longest prefix of ``messages`` stored in LMDB ``db_sessions``.

    When ``require_delta=True``, only prefixes strictly shorter than ``len(messages)``
    are considered so that ``delta_messages`` is guaranteed to be non-empty.
    When ``target_account_id`` is supplied, only matches belonging to that account are returned.
    """
    if not messages:
        return None

    store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    prefix_hashes = compute_prefix_hashes(messages)
    max_index = len(prefix_hashes) - 2 if require_delta else len(prefix_hashes) - 1
    excluded_set = set(excluded_account_ids) if excluded_account_ids is not None else None

    for idx in range(max_index, -1, -1):
        candidate_hash = prefix_hashes[idx]
        record = store.get_session_record(candidate_hash)
        if record is not None:
            if target_account_id is not None and record.account_id != target_account_id:
                continue
            if excluded_set is not None and record.account_id in excluded_set:
                continue
            matched_count = idx + 1
            return PrefixMatchResult(
                prefix_hash=candidate_hash,
                matched_count=matched_count,
                account_id=record.account_id,
                session_id=record.session_id or (record.metadata[0] if record.metadata else ""),
                metadata=list(record.metadata),
                delta_messages=_copy_messages_as_dicts(messages[matched_count:]),
            )

    return None


def get_preferred_session_account(
    messages: Sequence[Mapping[str, Any]],
    *,
    lmdb_store: LMDBStore | None = None,
    system_prompt: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
    max_chars: int = SESSION_AFFINITY_MAX_CONTEXT_CHARS,
    excluded_account_ids: Set[str] | Sequence[str] | None = None,
    env: Any = None,
    **kwargs: Any,
) -> tuple[str | None, int]:
    """Return ``(preferred_account_id, total_chars)`` if an active session fits affinity cap.

    Enforces ``SESSION_AFFINITY_MAX_CONTEXT_CHARS`` (120,000 characters) so oversized
    contexts can rotate cleanly to a fresh account without sticky-session lock-in.
    """
    total_chars = compute_messages_char_length(
        messages, system_prompt=system_prompt, tools=tools
    )
    if total_chars >= max_chars:
        return None, total_chars

    store = lmdb_store if lmdb_store is not None else (env if env is not None else None)
    matched = find_longest_prefix_session(
        messages,
        lmdb_store=store,
        excluded_account_ids=excluded_account_ids,
    )
    if matched is None:
        return None, total_chars
    return matched.account_id, total_chars


# ============================================================================
# 6. XML History & Tool Response Formatting
# ============================================================================


def format_tool_response_block(call_id: str, name: str, content: str) -> str:
    """Format a tool execution output inside a structured ``<tool_response>`` XML block."""
    safe_call_id = str(call_id or "unknown_call")
    safe_name = str(name or "unknown_tool")
    safe_content = str(content or "")
    return (
        f'<tool_response call_id="{safe_call_id}" name="{safe_name}">\n'
        f"{safe_content}\n"
        f"</tool_response>"
    )


def _format_assistant_tool_calls_xml(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """Render historical assistant ``tool_calls`` as ``<tool_call>`` XML blocks."""
    rendered_blocks: list[str] = []
    for tool_call in tool_calls:
        normalized = _normalize_single_tool_call(tool_call)
        func_info = normalized["function"]
        raw_args = func_info["arguments"]
        try:
            parsed_args = json.loads(raw_args) if raw_args else {}
        except ValueError:
            parsed_args = raw_args
        payload = json.dumps(
            {"name": func_info["name"], "arguments": parsed_args},
            ensure_ascii=False,
        )
        rendered_blocks.append(f"<tool_call>{payload}</tool_call>")
    return "\n".join(rendered_blocks)


def _format_single_message_entry(message: Mapping[str, Any]) -> str:
    """Format a single message for inline history, delta prompts, or temp history files."""
    role = str(message.get("role") or "user").strip().lower()
    content_text = _extract_text_from_content(message.get("content"))

    if role in ("tool", "function"):
        call_id = str(
            message.get("tool_call_id") or message.get("call_id") or message.get("id") or "call_0"
        )
        tool_name = str(message.get("name") or "tool")
        return format_tool_response_block(call_id, tool_name, content_text)

    if role in ("assistant", "model"):
        parts = [content_text] if content_text else []
        raw_tool_calls = message.get("tool_calls")
        if isinstance(raw_tool_calls, Sequence) and not isinstance(
            raw_tool_calls, (str, bytes, bytearray)
        ):
            mapping_calls = [tc for tc in raw_tool_calls if isinstance(tc, Mapping)]
            if mapping_calls:
                parts.append(_format_assistant_tool_calls_xml(mapping_calls))
        return f"[assistant]\n" + "\n".join(parts)

    return f"[{role}]\n{content_text}"


def format_messages_as_xml_history(messages: Sequence[Mapping[str, Any]]) -> str:
    """Wrap a sequence of historical messages in a ``<conversation_history>`` XML block."""
    formatted_entries = [_format_single_message_entry(msg) for msg in messages]
    inner_body = "\n\n".join(formatted_entries)
    if inner_body:
        return f"<conversation_history>\n{inner_body}\n</conversation_history>"
    return "<conversation_history>\n</conversation_history>"


def _format_tools_protocol_block(tools: Sequence[Mapping[str, Any]] | None) -> str:
    """Format tool declarations and XML ``<tool_call>`` emission instructions for Web Pool."""
    if not tools:
        return ""
    serialized_tools = json.dumps(
        [dict(t) for t in tools if isinstance(t, Mapping)],
        ensure_ascii=False,
    )
    return (
        "<tools_definition>\n"
        "You have access to the following tools:\n"
        f"{serialized_tools}\n"
        "When invoking a tool, emit ONLY a valid JSON object inside XML tags:\n"
        '<tool_call>{"name": "<tool_name>", "arguments": {...}}</tool_call>\n'
        "</tools_definition>"
    )


def _extract_combined_system_prompt(
    messages: Sequence[Mapping[str, Any]],
    explicit_system_prompt: str,
    *,
    enable_waf_shield: bool,
) -> tuple[str, list[dict[str, Any]]]:
    """Separate system/developer instructions from conversation turns."""
    system_segments: list[str] = []
    if explicit_system_prompt.strip():
        cleaned_explicit = explicit_system_prompt.strip()
        system_segments.append(
            sanitize_system_prompt_text(cleaned_explicit)
            if enable_waf_shield
            else cleaned_explicit
        )

    conversation_turns: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "").strip().lower()
        if role in _SYSTEM_ROLES:
            text = _extract_text_from_content(msg.get("content")).strip()
            if text:
                system_segments.append(text)
        else:
            conversation_turns.append(dict(msg))

    return "\n\n".join(system_segments), conversation_turns


# ============================================================================
# 7. Cross-Platform 3-Tier Context Rehydration Engine (ADR-08)
# ============================================================================


def resolve_temp_history_dir(override_dir: Path | str | None = None) -> Path:
    """Resolve the cross-platform temporary directory for Tier-3 history passthrough.

    Uses ``/dev/shm`` when present and writable on Linux, otherwise falls back to
    ``tempfile.gettempdir()`` on Windows/macOS so ``FileNotFoundError`` never occurs.
    """
    if override_dir is not None:
        target = Path(override_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target

    shm_path = Path("/dev/shm")
    if shm_path.exists() and shm_path.is_dir() and os.access(shm_path, os.W_OK):
        return shm_path

    if TEMP_HISTORY_DIR.exists() and os.access(TEMP_HISTORY_DIR, os.W_OK):
        return TEMP_HISTORY_DIR

    return Path(tempfile.gettempdir())


def _prepare_sanitized_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    headers: Mapping[str, str] | None,
    enable_token_saver: bool,
    enable_waf_shield: bool,
) -> list[dict[str, Any]]:
    """Apply lossless RTK-Lite tool compression and WAF system prompt shielding."""
    prepared = _copy_messages_as_dicts(messages)
    if enable_token_saver:
        prepared = sanitize_tool_messages(prepared, enabled=True, headers=headers)
    if enable_waf_shield:
        prepared = sanitize_messages_waf(prepared)
    return prepared


def _find_best_prefix_match(
    raw_messages: Sequence[Mapping[str, Any]],
    sanitized_messages: Sequence[Mapping[str, Any]],
    *,
    lmdb_store: LMDBStore,
    target_account_id: str | None = None,
) -> PrefixMatchResult | None:
    """Locate the longest prefix match in ``db_sessions`` preferring non-empty deltas."""
    for candidate_seq in (raw_messages, sanitized_messages):
        match_with_delta = find_longest_prefix_session(
            candidate_seq,
            lmdb_store=lmdb_store,
            require_delta=True,
            target_account_id=target_account_id,
        )
        if match_with_delta is not None:
            matched_idx = match_with_delta.matched_count
            match_with_delta.delta_messages = _copy_messages_as_dicts(
                sanitized_messages[matched_idx:]
            )
            return match_with_delta

    return find_longest_prefix_session(
        raw_messages,
        lmdb_store=lmdb_store,
        require_delta=False,
        target_account_id=target_account_id,
    )


def _build_delta_prompt(delta_messages: Sequence[Mapping[str, Any]]) -> str:
    """Format delta turns for a Tier-1 same-account session continuation."""
    rendered_blocks: list[str] = []
    for msg in delta_messages:
        role = str(msg.get("role") or "user").strip().lower()
        if role in ("tool", "function"):
            call_id = str(
                msg.get("tool_call_id") or msg.get("call_id") or msg.get("id") or "call_0"
            )
            tool_name = str(msg.get("name") or "tool")
            content_text = _extract_text_from_content(msg.get("content"))
            rendered_blocks.append(format_tool_response_block(call_id, tool_name, content_text))
        elif len(delta_messages) == 1 and role == "user":
            rendered_blocks.append(_extract_text_from_content(msg.get("content")))
        else:
            rendered_blocks.append(_format_single_message_entry(msg))
    return "\n\n".join(rendered_blocks)


def _build_tier2_prompt(
    all_sanitized_messages: Sequence[Mapping[str, Any]],
    system_header: str,
    tools_block: str,
) -> str:
    """Assemble the Tier-2 inline history prompt (<= 60,000 chars)."""
    prompt_sections: list[str] = []
    if system_header:
        prompt_sections.append(f"<system_instructions>\n{system_header}\n</system_instructions>")
    if tools_block:
        prompt_sections.append(tools_block)

    non_system_turns = [
        msg
        for msg in all_sanitized_messages
        if str(msg.get("role") or "").strip().lower() not in _SYSTEM_ROLES
    ]
    prior_turns = non_system_turns[:-1] if len(non_system_turns) > 1 else []
    active_turn = (
        non_system_turns[-1]
        if non_system_turns
        else (all_sanitized_messages[-1] if all_sanitized_messages else None)
    )

    if prior_turns:
        prompt_sections.append(format_messages_as_xml_history(prior_turns))

    if active_turn is not None:
        role = str(active_turn.get("role") or "user").strip().lower()
        if role in ("tool", "function") or prior_turns:
            prompt_sections.append(_format_single_message_entry(active_turn))
        else:
            prompt_sections.append(_extract_text_from_content(active_turn.get("content")))

    return "\n\n".join(section for section in prompt_sections if section)


def _write_tier3_temp_history_file(
    older_messages: Sequence[Mapping[str, Any]],
    target_dir: Path,
) -> Path:
    """Write ``messages[:-3]`` into ``harness_history_{uuid4().hex}.txt`` in ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{TEMP_FILE_PREFIX}{uuid4().hex}.txt"
    temp_file_path = target_dir / file_name
    serialized_history = format_messages_as_xml_history(older_messages)
    temp_file_path.write_text(serialized_history, encoding="utf-8")
    return temp_file_path


def _build_tier3_prompt(
    recent_messages: Sequence[Mapping[str, Any]],
    temp_file_name: str,
    system_header: str,
    tools_block: str,
) -> str:
    """Assemble the Tier-3 prompt pinning system, tools, and ``messages[-3:]``."""
    sections: list[str] = []
    if system_header:
        sections.append(f"<system_instructions>\n{system_header}\n</system_instructions>")
    if tools_block:
        sections.append(tools_block)

    sections.append(
        f"<attached_context_notice>\n"
        f"The earlier conversation history is attached in file '{temp_file_name}'. "
        f"Read the attached file carefully before responding to the recent turns below.\n"
        f"</attached_context_notice>"
    )

    formatted_recent = [_format_single_message_entry(msg) for msg in recent_messages]
    if formatted_recent:
        sections.append("<recent_turns>\n" + "\n\n".join(formatted_recent) + "\n</recent_turns>")

    return "\n\n".join(section for section in sections if section)


def _create_tier3_plan(
    sanitized_messages: list[dict[str, Any]],
    full_prefix_hash: str,
    header_and_tools: tuple[str, str],
    temp_dir: Path | str | None,
) -> RehydrationPlan:
    """Construct a ``TIER3_TEMP_FILE_PASSTHROUGH`` plan and write the temporary history file."""
    system_header, tools_block = header_and_tools
    non_system_turns = [
        msg
        for msg in sanitized_messages
        if str(msg.get("role") or "").strip().lower() not in _SYSTEM_ROLES
    ]
    target_turns = non_system_turns if non_system_turns else sanitized_messages

    if len(target_turns) > 3:
        older_messages = target_turns[:-3]
        recent_messages = target_turns[-3:]
    elif len(target_turns) > 1:
        older_messages = target_turns[:-1]
        recent_messages = target_turns[-1:]
    else:
        older_messages = target_turns
        recent_messages = target_turns

    resolved_temp_dir = resolve_temp_history_dir(temp_dir)
    temp_file_path = _write_tier3_temp_history_file(older_messages, resolved_temp_dir)
    prompt = _build_tier3_prompt(
        recent_messages,
        temp_file_path.name,
        system_header,
        tools_block,
    )
    return RehydrationPlan(
        tier=TIER3_TEMP_FILE_PASSTHROUGH,
        temporary=False,
        prompt=prompt,
        files=[temp_file_path],
        temp_file_path=temp_file_path,
        resume_metadata=None,
        conversation_cid=None,
        full_prefix_hash=full_prefix_hash,
        delta_messages=list(recent_messages),
    )


# Clean Code Rule 3 exception: 4 positional parameters supporting dual signatures
# (target_account_id first vs messages first) for cross-phase backward compatibility.
# Revisit when upstream routing standardizes on a unified RehydrationRequest DTO.
def build_rehydration_plan(
    target_account_id: str | Sequence[Mapping[str, Any]] | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
    system_prompt: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    enable_token_saver: bool = True,
    enable_waf_shield: bool = True,
    lmdb_store: LMDBStore | None = None,
    temp_dir: Path | str | None = None,
    **kwargs: Any,
) -> RehydrationPlan:
    """Build a 3-Tier Context Rehydration Plan for the selected Web account.

    Supports both ``build_rehydration_plan(target_account_id, messages, ...)``
    and ``build_rehydration_plan(messages, target_account_id=..., ...)`` parameter orders.
    """
    effective_account_id: str | None = None
    effective_messages: Sequence[Mapping[str, Any]]

    if isinstance(target_account_id, str):
        effective_account_id = target_account_id
        effective_messages = messages if messages is not None else []
    elif isinstance(target_account_id, Sequence) and not isinstance(
        target_account_id, (str, bytes, bytearray)
    ):
        effective_messages = target_account_id
        effective_account_id = (
            kwargs.get("target_account_id")
            if "target_account_id" in kwargs
            else (messages if isinstance(messages, str) else None)
        )
    else:
        effective_account_id = kwargs.get("target_account_id")
        effective_messages = messages if messages is not None else []

    if not effective_messages:
        return RehydrationPlan(
            tier=TIER2_INLINE_HISTORY,
            temporary=should_use_temporary_session([], tools),
            prompt=sanitize_system_prompt_text(system_prompt) if enable_waf_shield else system_prompt,
            full_prefix_hash=hashlib.sha256(b"").hexdigest(),
        )

    store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    sanitized_messages = _prepare_sanitized_messages(
        effective_messages,
        headers=headers,
        enable_token_saver=enable_token_saver,
        enable_waf_shield=enable_waf_shield,
    )
    full_prefix_hash = compute_prefix_hashes(effective_messages)[-1]

    matched = _find_best_prefix_match(
        effective_messages,
        sanitized_messages,
        lmdb_store=store,
        target_account_id=effective_account_id,
    )
    if (
        matched is not None
        and effective_account_id is not None
        and matched.account_id == effective_account_id
        and len(matched.delta_messages) > 0
    ):
        cid = matched.metadata[0] if matched.metadata else None
        return RehydrationPlan(
            tier=TIER1_PREFIX_HIT,
            temporary=False,
            prompt=_build_delta_prompt(matched.delta_messages),
            resume_metadata=list(matched.metadata),
            conversation_cid=cid,
            full_prefix_hash=full_prefix_hash,
            delta_messages=matched.delta_messages,
        )

    system_header, _ = _extract_combined_system_prompt(
        sanitized_messages, system_prompt, enable_waf_shield=enable_waf_shield
    )
    tools_block = _format_tools_protocol_block(tools)
    total_chars = compute_messages_char_length(
        sanitized_messages, system_prompt=system_prompt, tools=tools
    )

    if total_chars <= REHYDRATION_TIER2_MAX_CHARS:
        return RehydrationPlan(
            tier=TIER2_INLINE_HISTORY,
            temporary=should_use_temporary_session(sanitized_messages, tools),
            prompt=_build_tier2_prompt(sanitized_messages, system_header, tools_block),
            full_prefix_hash=full_prefix_hash,
            delta_messages=sanitized_messages,
        )

    return _create_tier3_plan(
        sanitized_messages,
        full_prefix_hash,
        (system_header, tools_block),
        temp_dir,
    )


# ============================================================================
# 8. Deterministic Temp File Cleanup, Context Manager & Stream Execution
# ============================================================================


def remove_temp_history_file(temp_file_path: Path | str | None) -> bool:
    """Deterministically remove a Tier-3 temporary history file if it exists."""
    if temp_file_path is None:
        return False
    try:
        os.remove(temp_file_path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Failed to remove temp history file %s: %s", temp_file_path, exc)
        return False


def cleanup_orphaned_temp_files(temp_dir: Path | str | None = None) -> int:
    """Remove all orphaned ``harness_history_*.txt`` files on server startup."""
    target_dir = resolve_temp_history_dir(temp_dir)
    if not target_dir.exists() or not target_dir.is_dir():
        return 0

    removed_count = 0
    for candidate in target_dir.glob(TEMP_FILE_GLOB):
        if candidate.is_file() and remove_temp_history_file(candidate):
            removed_count += 1
    return removed_count


class RehydrationContextManager:
    """Sync and async context manager guaranteeing Tier-3 temp file deletion on exit."""

    def __init__(self, plan: RehydrationPlan) -> None:
        self.plan = plan

    def __enter__(self) -> RehydrationPlan:
        return self.plan

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        remove_temp_history_file(self.plan.temp_file_path)

    async def __aenter__(self) -> RehydrationPlan:
        return self.plan

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        remove_temp_history_file(self.plan.temp_file_path)


def managed_rehydration_plan(
    target_account_id: str | Sequence[Mapping[str, Any]] | RehydrationPlan | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
    **kwargs: Any,
) -> RehydrationContextManager:
    """Create a ``RehydrationPlan`` wrapped in a deterministic cleanup context manager."""
    if isinstance(target_account_id, RehydrationPlan):
        return RehydrationContextManager(target_account_id)
    plan = build_rehydration_plan(target_account_id, messages, **kwargs)
    return RehydrationContextManager(plan)


def _start_client_chat_session(
    client: Any,
    plan: RehydrationPlan,
    model: Any,
) -> Any:
    """Invoke ``client.start_chat`` adapting to supported keyword parameters."""
    start_chat_fn = client.start_chat
    sig = inspect.signature(start_chat_fn)
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    eff_model = model
    if isinstance(model, str):
        try:
            from core.model_registry import split_model_effort_suffix
            base_m, _ = split_model_effort_suffix(model)
            resolve_fn = getattr(client, "resolve_model", None) or getattr(
                client, "_resolve_model_by_name", None
            )
            if callable(resolve_fn):
                try:
                    res = resolve_fn(base_m)
                    eff_model = res if res is not None else base_m
                except Exception:
                    eff_model = base_m
            else:
                eff_model = base_m
        except Exception:
            eff_model = model

    call_kwargs: dict[str, Any] = {}
    if accepts_var_kw or "metadata" in sig.parameters:
        call_kwargs["metadata"] = plan.resume_metadata
    if eff_model is not None and (accepts_var_kw or "model" in sig.parameters):
        call_kwargs["model"] = eff_model
    # NOTE: Never pass temporary to start_chat when accepts_var_kw is True!
    # In gemini_webapi, start_chat has **kwargs but ChatSession.__init__ does not accept temporary;
    # temporary is accepted by send_message_stream.
    if "temporary" in sig.parameters and not accepts_var_kw:
        call_kwargs["temporary"] = plan.temporary

    return start_chat_fn(**call_kwargs)


def _open_chat_message_stream(
    chat: Any,
    plan: RehydrationPlan,
    reasoning_effort: str | None = None,
) -> Any:
    """Call ``chat.send_message_stream`` with ``plan.prompt``, ``temporary``, and ``extended_thinking``."""
    send_fn = chat.send_message_stream
    sig = inspect.signature(send_fn)
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    call_kwargs: dict[str, Any] = {}
    if plan.files and (accepts_var_kw or "files" in sig.parameters):
        call_kwargs["files"] = plan.files
    if accepts_var_kw or "temporary" in sig.parameters:
        call_kwargs["temporary"] = plan.temporary
    if reasoning_effort in ("high", "medium") and (
        accepts_var_kw or "extended_thinking" in sig.parameters
    ):
        call_kwargs["extended_thinking"] = True

    return send_fn(plan.prompt, **call_kwargs)


@dataclass
class _SessionCompletionContext:
    """Internal bundle for persisting chat metadata into LMDB after streaming."""

    store: LMDBStore
    account_id: str
    plan: RehydrationPlan
    messages: Sequence[Mapping[str, Any]]


def _persist_completed_session(
    ctx: _SessionCompletionContext,
    chat: Any,
    accumulated_text: str,
) -> None:
    """Persist updated ``chat.metadata`` (`[cid, rid, rcid]`) in LMDB ``db_sessions``."""
    if ctx.plan.temporary:
        return

    raw_metadata = getattr(chat, "metadata", None)
    if not isinstance(raw_metadata, list) or not raw_metadata or not raw_metadata[0]:
        return

    normalized_meta = [str(item) if item is not None else "" for item in raw_metadata]
    cid = normalized_meta[0]

    ctx.store.put_session(ctx.plan.full_prefix_hash, ctx.account_id, normalized_meta)
    register_chat_activity(cid, ctx.account_id, prefix_hash=ctx.plan.full_prefix_hash)

    if accumulated_text:
        extended_messages = _copy_messages_as_dicts(ctx.messages)
        extended_messages.append({"role": "assistant", "content": accumulated_text})
        extended_hash = compute_prefix_hashes(extended_messages)[-1]
        ctx.store.put_session(extended_hash, ctx.account_id, normalized_meta)
        register_chat_activity(cid, ctx.account_id, prefix_hash=extended_hash)


async def _iterate_chat_chunks(
    chat: Any,
    plan: RehydrationPlan,
    accumulated_parts: list[str],
    reasoning_effort: str | None = None,
) -> AsyncIterator[Any]:
    """Yield chunks from ``chat.send_message_stream`` while recording text deltas."""
    stream_or_coro = _open_chat_message_stream(
        chat, plan, reasoning_effort=reasoning_effort
    )
    stream = await stream_or_coro if inspect.isawaitable(stream_or_coro) else stream_or_coro

    async for chunk in stream:
        delta_text = getattr(chunk, "text_delta", None) or getattr(chunk, "text", None)
        if isinstance(delta_text, str) and delta_text:
            accumulated_parts.append(delta_text)
        yield chunk


async def _stream_and_persist_turn(
    client: Any,
    model: Any,
    completion_ctx: _SessionCompletionContext,
    reasoning_effort: str | None = None,
) -> AsyncIterator[Any]:
    """Run ``start_chat``, yield all stream chunks, and persist the resulting session."""
    accumulated_parts: list[str] = []
    chat = _start_client_chat_session(client, completion_ctx.plan, model)
    async for chunk in _iterate_chat_chunks(
        chat, completion_ctx.plan, accumulated_parts, reasoning_effort=reasoning_effort
    ):
        yield chunk
    _persist_completed_session(completion_ctx, chat, "".join(accumulated_parts))


async def execute_rehydrated_stream(
    account: WebAccount,
    client: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model: Any = None,
    reasoning_effort: str | None = None,
    system_prompt: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    enable_token_saver: bool = True,
    enable_waf_shield: bool = True,
    lmdb_store: LMDBStore | None = None,
    temp_dir: Path | str | None = None,
) -> AsyncIterator[Any]:
    """Execute a streaming Web request with 3-tier context rehydration and guaranteed cleanup."""
    store = lmdb_store if lmdb_store is not None else get_lmdb_store()
    effective_effort = reasoning_effort
    if effective_effort is None and isinstance(model, str):
        try:
            from core.model_registry import split_model_effort_suffix
            _, extracted_eff = split_model_effort_suffix(model)
            effective_effort = extracted_eff
        except Exception:
            pass

    plan = build_rehydration_plan(
        account.id,
        messages,
        system_prompt=system_prompt,
        tools=tools,
        headers=headers,
        enable_token_saver=enable_token_saver,
        enable_waf_shield=enable_waf_shield,
        lmdb_store=store,
        temp_dir=temp_dir,
    )
    completion_ctx = _SessionCompletionContext(
        store=store,
        account_id=account.id,
        plan=plan,
        messages=messages,
    )

    try:
        if plan.tier == TIER1_PREFIX_HIT and plan.conversation_cid:
            async with account.get_conversation_lock(plan.conversation_cid):
                async for chunk in _stream_and_persist_turn(
                    client, model, completion_ctx, reasoning_effort=effective_effort
                ):
                    yield chunk
        else:
            async for chunk in _stream_and_persist_turn(
                client, model, completion_ctx, reasoning_effort=effective_effort
            ):
                yield chunk
    finally:
        remove_temp_history_file(plan.temp_file_path)


execute_with_rehydration = execute_rehydrated_stream


__all__ = [
    "IDLE_CHAT_TTL_SECONDS",
    "REHYDRATION_TIER2_MAX_CHARS",
    "ActiveChatSession",
    "PrefixMatchResult",
    "RehydrationContextManager",
    "RehydrationPlan",
    "RehydrationTier",
    "TEMP_FILE_GLOB",
    "TEMP_FILE_PREFIX",
    "TIER1_PREFIX_HIT",
    "TIER2_INLINE_HISTORY",
    "TIER3_TEMP_FILE_PASSTHROUGH",
    "build_rehydration_plan",
    "cleanup_expired_chats",
    "cleanup_orphaned_temp_files",
    "clear_tracked_chats",
    "compute_messages_char_length",
    "compute_prefix_hashes",
    "execute_rehydrated_stream",
    "execute_with_rehydration",
    "find_longest_prefix_session",
    "format_messages_as_xml_history",
    "format_tool_response_block",
    "get_preferred_session_account",
    "get_tracked_chats",
    "managed_rehydration_plan",
    "normalize_message_for_hash",
    "register_chat_activity",
    "remove_temp_history_file",
    "resolve_temp_history_dir",
    "should_use_temporary_session",
    "unregister_chat_activity",
]
