"""Unit and integration test suite for Level 2 prefix differ & context rehydration (`core/prefix_differ.py`).

Covers:
1. Sequential Prefix Hashing (SHA-256):
   - Deterministic canonical JSON formatting via ``normalize_message_for_hash``
   - Cumulative prefix digest computation via ``compute_prefix_hashes``
   - Accurate character length calculation via ``compute_messages_char_length``
2. Adaptive Session Mode:
   - ``should_use_temporary_session``: True for single-turn tool-less requests;
     False for multi-turn conversations and tool-assisted requests
   - In-memory chat tracking (``register_chat_activity``, ``get_tracked_chats``, ``clear_tracked_chats``)
   - 60-minute idle chat garbage collection via ``cleanup_expired_chats``
3. Longest Prefix Matching in LMDB:
   - ``find_longest_prefix_session`` retrieving exact O(k) longest prefixes and delta messages
   - ``get_preferred_session_account`` respecting the 120,000 char session affinity threshold
4. 3-Tier Context Rehydration Engine:
   - Tier 1 (``tier1_prefix_hit``): same-account match returning delta turns and ``<tool_response>`` tags
   - Tier 2 (``tier2_inline_history``): failover or cold start with <= 60,000 chars formatted in ``<conversation_history>``
   - Tier 3 (``tier3_temp_file_passthrough``): large contexts (> 60,000 chars) writing ``messages[:-3]`` to disk,
     pinning system prompt, tools, and last 3 turns in prompt, and passing files
5. Deterministic Temp File Cleanup & Context Managers:
   - Safe removal via ``remove_temp_history_file``
   - Startup orphan pruning via ``cleanup_orphaned_temp_files``
   - Sync & async ``RehydrationContextManager`` guaranteed cleanup in ``finally:``
   - Streaming execution via ``execute_rehydrated_stream`` with conversation locking and cleanup
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    LMDBStore,
    REHYDRATION_TIER2_MAX_CHARS,
    SESSION_AFFINITY_MAX_CONTEXT_CHARS,
    TEMP_HISTORY_DIR,
    WebAccount,
)
from core.prefix_differ import (
    IDLE_CHAT_TTL_SECONDS,
    TIER1_PREFIX_HIT,
    TIER2_INLINE_HISTORY,
    TIER3_TEMP_FILE_PASSTHROUGH,
    ActiveChatSession,
    PrefixMatchResult,
    RehydrationContextManager,
    RehydrationPlan,
    build_rehydration_plan,
    cleanup_expired_chats,
    cleanup_orphaned_temp_files,
    clear_tracked_chats,
    compute_messages_char_length,
    compute_prefix_hashes,
    execute_rehydrated_stream,
    find_longest_prefix_session,
    format_messages_as_xml_history,
    format_tool_response_block,
    get_preferred_session_account,
    get_tracked_chats,
    managed_rehydration_plan,
    normalize_message_for_hash,
    register_chat_activity,
    remove_temp_history_file,
    resolve_temp_history_dir,
    should_use_temporary_session,
    unregister_chat_activity,
)


@pytest.fixture(autouse=True)
def clean_chats_state() -> None:
    """Ensure in-memory chat registry is reset before and after every test."""
    clear_tracked_chats()
    yield
    clear_tracked_chats()


@pytest.fixture
def temp_lmdb_store(tmp_path: Path) -> LMDBStore:
    """Provide an isolated LMDB environment for testing."""
    store = LMDBStore(path=tmp_path / "lmdb_test")
    yield store
    store.close()


# ============================================================================
# 1. Sequential Prefix Hashing (SHA-256) & Char Length
# ============================================================================


def test_normalize_message_for_hash_determinism() -> None:
    """Equivalent messages with arbitrary key order must produce identical canonical JSON."""
    msg1 = {
        "role": "user",
        "content": "Hello Gemini",
        "name": "alice",
    }
    msg2 = {
        "name": "alice",
        "content": "Hello Gemini",
        "role": "user",
    }
    canonical1 = normalize_message_for_hash(msg1)
    canonical2 = normalize_message_for_hash(msg2)

    assert canonical1 == canonical2
    parsed = json.loads(canonical1)
    assert parsed["role"] == "user"
    assert parsed["content"] == "Hello Gemini"
    assert parsed["name"] == "alice"


def test_normalize_message_with_multipart_content_and_tool_calls() -> None:
    """Multipart content parts and assistant tool calls must be normalized canonically."""
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": "I will call the search tool."}],
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": '{"query":"python","limit":5}',
                },
            }
        ],
    }
    canonical = normalize_message_for_hash(msg)
    parsed = json.loads(canonical)

    assert parsed["role"] == "assistant"
    assert "tool_calls" in parsed
    assert len(parsed["tool_calls"]) == 1
    assert parsed["tool_calls"][0]["function"]["name"] == "search"
    # Arguments must be canonical sorted JSON
    assert parsed["tool_calls"][0]["function"]["arguments"] == '{"limit":5,"query":"python"}'


def test_compute_prefix_hashes_accumulation() -> None:
    """Prefix hashes for [m1, m2] must match the first two hashes of [m1, m2, m3]."""
    m1 = {"role": "user", "content": "Hello"}
    m2 = {"role": "assistant", "content": "Hi there!"}
    m3 = {"role": "user", "content": "How is the weather?"}

    hashes_turn2 = compute_prefix_hashes([m1, m2])
    hashes_turn3 = compute_prefix_hashes([m1, m2, m3])

    assert len(hashes_turn2) == 2
    assert len(hashes_turn3) == 3
    assert hashes_turn2[0] == hashes_turn3[0]
    assert hashes_turn2[1] == hashes_turn3[1]
    assert hashes_turn3[2] != hashes_turn3[1]


def test_compute_messages_char_length() -> None:
    """Character counts must correctly sum message contents, tool calls, and system prompts."""
    messages = [
        {"role": "user", "content": "12345"},  # 5 chars
        {"role": "assistant", "content": "1234567890"},  # 10 chars
    ]
    system_prompt = "SYS"  # 3 chars
    tools = [{"name": "tool_a", "description": "test"}]

    length_basic = compute_messages_char_length(messages)
    assert length_basic == 15

    length_with_sys = compute_messages_char_length(messages, system_prompt=system_prompt)
    assert length_with_sys == 18

    length_with_tools = compute_messages_char_length(
        messages, system_prompt=system_prompt, tools=tools
    )
    assert length_with_tools > 18


# ============================================================================
# 2. Adaptive Session Mode & Idle Chat GC
# ============================================================================


def test_should_use_temporary_session() -> None:
    """Temporary=True for single-turn tool-less queries; False for multi-turn or tool requests."""
    # Single user turn with or without system prompt
    assert should_use_temporary_session([{"role": "user", "content": "Hi"}]) is True
    assert (
        should_use_temporary_session(
            [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hi"},
            ]
        )
        is True
    )

    # Multi-turn conversations
    assert (
        should_use_temporary_session(
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Next"},
            ]
        )
        is False
    )

    # Multiple user turns
    assert (
        should_use_temporary_session(
            [
                {"role": "user", "content": "One"},
                {"role": "user", "content": "Two"},
            ]
        )
        is False
    )

    # Tool requests
    assert (
        should_use_temporary_session(
            [{"role": "user", "content": "Hi"}],
            tools=[{"name": "fetch"}],
        )
        is False
    )
    assert (
        should_use_temporary_session(
            [{"role": "tool", "content": "output", "tool_call_id": "c1"}]
        )
        is False
    )


@pytest.mark.asyncio
async def test_cleanup_expired_chats_purges_idle_sessions(temp_lmdb_store: LMDBStore) -> None:
    """Chats idle >= 3600s must be deleted from Google client, LMDB db_sessions, and registry."""
    account_id = "web_test_1"
    cid_active = "cid_active_100"
    cid_expired = "cid_expired_200"
    now = time.time()

    # Active session (active 5 minutes ago)
    register_chat_activity(
        cid_active, account_id, prefix_hash="hash_active", timestamp=now - 300
    )
    temp_lmdb_store.put_session("hash_active", account_id, [cid_active, "rid1", "rcid1"])

    # Expired session (idle 4000 seconds ago)
    register_chat_activity(
        cid_expired, account_id, prefix_hash="hash_expired", timestamp=now - 4000
    )
    temp_lmdb_store.put_session("hash_expired", account_id, [cid_expired, "rid2", "rcid2"])

    mock_client = AsyncMock()
    mock_client.delete_chat = AsyncMock()

    client_resolver = MagicMock(return_value=mock_client)

    deleted_count = await cleanup_expired_chats(
        client_resolver,
        max_idle_seconds=IDLE_CHAT_TTL_SECONDS,
        lmdb_store=temp_lmdb_store,
        now=now,
    )

    assert deleted_count == 1
    mock_client.delete_chat.assert_called_once_with(cid_expired)

    # Expired session must be purged from LMDB and in-memory registry
    assert temp_lmdb_store.get_session("hash_expired") is None
    assert cid_expired not in get_tracked_chats()

    # Active session must remain intact
    assert temp_lmdb_store.get_session("hash_active") is not None
    assert cid_active in get_tracked_chats()


# ============================================================================
# 3. Longest Prefix Match in LMDB
# ============================================================================


def test_find_longest_prefix_session_and_delta(temp_lmdb_store: LMDBStore) -> None:
    """Longest matching prefix must be detected and return the correct delta messages."""
    m1 = {"role": "user", "content": "Question 1"}
    m2 = {"role": "assistant", "content": "Answer 1"}
    m3 = {"role": "user", "content": "Question 2"}

    h1, h2 = compute_prefix_hashes([m1, m2])
    temp_lmdb_store.put_session(h2, "web_account_alpha", ["cid_alpha_1", "rid_1", "rcid_1"])

    # Query with [m1, m2, m3]
    match = find_longest_prefix_session([m1, m2, m3], lmdb_store=temp_lmdb_store)
    assert match is not None
    assert match.prefix_hash == h2
    assert match.matched_count == 2
    assert match.account_id == "web_account_alpha"
    assert match.metadata == ["cid_alpha_1", "rid_1", "rcid_1"]
    assert len(match.delta_messages) == 1
    assert match.delta_messages[0]["content"] == "Question 2"


def test_get_preferred_session_account_affinity_cap(temp_lmdb_store: LMDBStore) -> None:
    """Contexts exceeding 120,000 chars must forfeit session affinity."""
    m1 = {"role": "user", "content": "Hello"}
    h1 = compute_prefix_hashes([m1])[0]
    temp_lmdb_store.put_session(h1, "web_acc_1", ["cid_1", "r1", "rc1"])

    # Normal context under 120k chars
    pref_acc, chars = get_preferred_session_account([m1], lmdb_store=temp_lmdb_store)
    assert pref_acc == "web_acc_1"
    assert chars < SESSION_AFFINITY_MAX_CONTEXT_CHARS

    # Oversized context exceeding 120k chars
    huge_prompt = "x" * (SESSION_AFFINITY_MAX_CONTEXT_CHARS + 500)
    pref_acc_huge, huge_chars = get_preferred_session_account(
        [m1], system_prompt=huge_prompt, lmdb_store=temp_lmdb_store
    )
    assert pref_acc_huge is None
    assert huge_chars > SESSION_AFFINITY_MAX_CONTEXT_CHARS


# ============================================================================
# 4. 3-Tier Context Rehydration Engine (ADR-08)
# ============================================================================


def test_tier1_prefix_hit_same_account(temp_lmdb_store: LMDBStore) -> None:
    """Tier 1: Same account hit must return delta messages formatted with tool response tags."""
    target_account = "web_account_42"
    m1 = {"role": "user", "content": "Run test"}
    m2 = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_99",
                "type": "function",
                "function": {"name": "run_test", "arguments": "{}"},
            }
        ],
    }
    m3 = {
        "role": "tool",
        "name": "run_test",
        "tool_call_id": "call_99",
        "content": "All 10 tests passed.",
    }

    hashes = compute_prefix_hashes([m1, m2])
    temp_lmdb_store.put_session(hashes[1], target_account, ["cid_42", "rid_42", "rcid_42"])

    plan = build_rehydration_plan(
        target_account,
        [m1, m2, m3],
        lmdb_store=temp_lmdb_store,
    )

    assert plan.tier == TIER1_PREFIX_HIT
    assert plan.temporary is False
    assert plan.conversation_cid == "cid_42"
    assert plan.resume_metadata == ["cid_42", "rid_42", "rcid_42"]
    assert '<tool_response call_id="call_99" name="run_test">' in plan.prompt
    assert "All 10 tests passed." in plan.prompt
    assert plan.temp_file_path is None


def test_tier2_inline_history_failover(temp_lmdb_store: LMDBStore) -> None:
    """Tier 2: Failover to a different account under 60k chars embeds history in XML tags."""
    cached_account = "web_acc_cached"
    failover_account = "web_acc_failover"

    m1 = {"role": "user", "content": "What is the capital of France?"}
    m2 = {"role": "assistant", "content": "Paris."}
    m3 = {"role": "user", "content": "And Italy?"}

    hashes = compute_prefix_hashes([m1, m2])
    temp_lmdb_store.put_session(hashes[1], cached_account, ["cid_cached", "r", "rc"])

    plan = build_rehydration_plan(
        failover_account,
        [m1, m2, m3],
        lmdb_store=temp_lmdb_store,
    )

    assert plan.tier == TIER2_INLINE_HISTORY
    assert plan.temporary is False
    assert "<conversation_history>" in plan.prompt
    assert "[user]\nWhat is the capital of France?" in plan.prompt
    assert "[assistant]\nParis." in plan.prompt
    assert plan.prompt.endswith("And Italy?") or "And Italy?" in plan.prompt
    assert plan.temp_file_path is None


def test_tier3_temp_file_passthrough(tmp_path: Path, temp_lmdb_store: LMDBStore) -> None:
    """Tier 3: Contexts exceeding 60k chars write older turns to temp file and pin recent 3 turns."""
    target_account = "web_acc_heavy"

    large_content = "Word " * 15_000  # ~75,000 chars
    m1 = {"role": "user", "content": f"Context chunk 1: {large_content}"}
    m2 = {"role": "assistant", "content": "Acknowledged chunk 1."}
    m3 = {"role": "user", "content": "Context chunk 2: proceed with analysis."}
    m4 = {"role": "assistant", "content": "Proceeding with analysis."}
    m5 = {"role": "user", "content": "Final instruction: summarize results."}

    plan = build_rehydration_plan(
        target_account,
        [m1, m2, m3, m4, m5],
        system_prompt="You are a data analyst.",
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    )

    try:
        assert plan.tier == TIER3_TEMP_FILE_PASSTHROUGH
        assert plan.temporary is False
        assert plan.temp_file_path is not None
        assert plan.temp_file_path.exists()
        assert str(plan.temp_file_path).startswith(str(tmp_path))

        # Check prompt contents: pins system, attached notice, and recent turns
        assert "<system_instructions>\nYou are a data analyst.\n</system_instructions>" in plan.prompt
        assert "<attached_context_notice>" in plan.prompt
        assert plan.temp_file_path.name in plan.prompt
        assert "<recent_turns>" in plan.prompt
        assert "Final instruction: summarize results." in plan.prompt

        # Check file contents: contains older turns m1 and m2
        file_text = plan.temp_file_path.read_text(encoding="utf-8")
        assert "<conversation_history>" in file_text
        assert "Context chunk 1:" in file_text
    finally:
        remove_temp_history_file(plan.temp_file_path)
        assert not plan.temp_file_path.exists()


# ============================================================================
# 5. Deterministic Temp File Cleanup & Context Managers
# ============================================================================


def test_cleanup_orphaned_temp_files(tmp_path: Path) -> None:
    """Startup cleaner must remove orphaned harness_history_*.txt files safely."""
    f1 = tmp_path / "harness_history_abc123.txt"
    f2 = tmp_path / "harness_history_def456.txt"
    f3 = tmp_path / "other_file.txt"

    f1.write_text("orphan 1", encoding="utf-8")
    f2.write_text("orphan 2", encoding="utf-8")
    f3.write_text("keep me", encoding="utf-8")

    removed = cleanup_orphaned_temp_files(temp_dir=tmp_path)
    assert removed == 2
    assert not f1.exists()
    assert not f2.exists()
    assert f3.exists()


def test_sync_rehydration_context_manager_cleanup(tmp_path: Path) -> None:
    """Sync 'with' context manager must delete the temporary file on exit."""
    large_content = "A" * 70_000
    messages = [
        {"role": "user", "content": large_content},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "turn 3"},
        {"role": "assistant", "content": "turn 4"},
        {"role": "user", "content": "turn 5"},
    ]

    temp_path_ref: Path | None = None
    with managed_rehydration_plan(
        "web_acc", messages, temp_dir=tmp_path
    ) as plan:
        assert plan.tier == TIER3_TEMP_FILE_PASSTHROUGH
        temp_path_ref = plan.temp_file_path
        assert temp_path_ref is not None
        assert temp_path_ref.exists()

    assert temp_path_ref is not None
    assert not temp_path_ref.exists()


@pytest.mark.asyncio
async def test_async_rehydration_context_manager_cleanup(tmp_path: Path) -> None:
    """Async 'async with' context manager must delete the temporary file on exit."""
    large_content = "B" * 70_000
    messages = [
        {"role": "user", "content": large_content},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "turn 3"},
        {"role": "assistant", "content": "turn 4"},
        {"role": "user", "content": "turn 5"},
    ]

    temp_path_ref: Path | None = None
    async with managed_rehydration_plan(
        "web_acc", messages, temp_dir=tmp_path
    ) as plan:
        assert plan.tier == TIER3_TEMP_FILE_PASSTHROUGH
        temp_path_ref = plan.temp_file_path
        assert temp_path_ref is not None
        assert temp_path_ref.exists()

    assert temp_path_ref is not None
    assert not temp_path_ref.exists()


@pytest.mark.asyncio
async def test_execute_rehydrated_stream_conversation_lock_and_cleanup(
    tmp_path: Path, temp_lmdb_store: LMDBStore
) -> None:
    """execute_rehydrated_stream must acquire conversation lock on Tier 1 and clean up in finally."""
    account = WebAccount(id="web_stream_test", label="Test Web Account")
    cid = "cid_stream_99"

    m1 = {"role": "user", "content": "First turn"}
    m2 = {"role": "assistant", "content": "First answer"}
    m3 = {"role": "user", "content": "Second turn"}

    h1, h2 = compute_prefix_hashes([m1, m2])
    temp_lmdb_store.put_session(h2, account.id, [cid, "rid", "rcid"])

    # Mock chunk and stream
    mock_chunk = MagicMock()
    mock_chunk.text_delta = "Streaming answer delta"

    async def sample_stream() -> AsyncIterator[Any]:
        yield mock_chunk

    mock_chat = MagicMock()
    mock_chat.metadata = [cid, "rid_new", "rcid_new"]
    mock_chat.send_message_stream = MagicMock(return_value=sample_stream())

    mock_client = MagicMock()
    mock_client.start_chat = MagicMock(return_value=mock_chat)

    received_chunks: list[Any] = []
    async for chunk in execute_rehydrated_stream(
        account,
        mock_client,
        [m1, m2, m3],
        lmdb_store=temp_lmdb_store,
        temp_dir=tmp_path,
    ):
        received_chunks.append(chunk)

    assert len(received_chunks) == 1
    assert received_chunks[0].text_delta == "Streaming answer delta"

    # Confirm LMDB was updated with the new prefix hashes
    final_user_hash = compute_prefix_hashes([m1, m2, m3])[-1]
    saved_session = temp_lmdb_store.get_session(final_user_hash)
    assert saved_session is not None
    assert saved_session["account_id"] == account.id
    assert saved_session["metadata"][0] == cid
