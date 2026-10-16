"""Comprehensive tests for Level 3 Web Cookie Pool channel, MultiToolLookaheadParser, and delta-5h accounting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import LMDBStore, WebAccount, WebCookies, get_model_base_delta
from core.web_channel import (
    LOOKAHEAD_BUFFER_CAPACITY,
    MultiToolLookaheadParser,
    ParserPhase,
    StreamChoice,
    StreamChunk,
    StreamDelta,
    StreamToolCall,
    ToolCallFunction,
    WebChannel,
    compute_context_scaled_delta_5h,
    execute_web_channel_stream,
    format_openai_sse_event,
)


# ============================================================================
# 1. MultiToolLookaheadParser Unit Tests
# ============================================================================


def test_parser_normal_text_streaming_without_tools() -> None:
    """Parser must stream regular text chunks as delta.content without triggering tool states."""
    parser = MultiToolLookaheadParser()
    assert parser.phase == ParserPhase.TEXT_STREAMING
    assert parser.has_emitted_tool_call is False

    deltas1 = parser.feed("Hello world! ")
    assert len(deltas1) == 1
    assert deltas1[0].content == "Hello world! "

    deltas2 = parser.feed("This is a clean stream.")
    assert len(deltas2) == 1
    assert deltas2[0].content == "This is a clean stream."

    final_deltas = parser.close()
    assert final_deltas == []
    assert parser.has_emitted_tool_call is False


def test_parser_single_tool_call_direct_tag() -> None:
    """Parser must detect <tool_call>...</tool_call>, emit delta.tool_calls, and set has_emitted_tool_call."""
    parser = MultiToolLookaheadParser()

    # Preceding text
    deltas = parser.feed("I will check the weather now: ")
    assert len(deltas) == 1
    assert deltas[0].content == "I will check the weather now: "

    # Feed opening tag + JSON + closing tag across chunks
    deltas_tool = parser.feed(
        '<tool_call>{"name": "get_weather", "arguments": {"city": "Tokyo"}}</tool_call>'
    )
    assert len(deltas_tool) == 1
    assert deltas_tool[0].tool_calls is not None
    assert len(deltas_tool[0].tool_calls) == 1

    tc = deltas_tool[0].tool_calls[0]
    assert tc.index == 0
    assert tc.id.startswith("call_")
    assert tc.type == "function"
    assert tc.function.name == "get_weather"
    assert json.loads(tc.function.arguments) == {"city": "Tokyo"}

    assert parser.has_emitted_tool_call is True
    assert parser.tool_call_index == 1

    final_deltas = parser.close()
    assert final_deltas == []


def test_parser_tool_call_inside_markdown_xml_code_block() -> None:
    """Parser must buffer ```xml\\n<tool_call> and extract tool call cleanly."""
    parser = MultiToolLookaheadParser()

    raw_output = (
        "Analyzing data.\n"
        "```xml\n"
        '<tool_call>{"name": "calculate", "arguments": {"expr": "2 + 2"}}</tool_call>\n'
        "```"
    )

    deltas = parser.feed(raw_output)
    # Check that text was emitted before the code block
    text_parts = [d.content for d in deltas if d.content]
    assert "Analyzing data.\n" in text_parts

    # Check tool call
    tool_calls_emitted = [d for d in deltas if d.tool_calls]
    assert len(tool_calls_emitted) == 1
    tc = tool_calls_emitted[0].tool_calls[0]
    assert tc.function.name == "calculate"
    assert json.loads(tc.function.arguments) == {"expr": "2 + 2"}

    final_deltas = parser.close()
    # Trailing ``` must be suppressed (ADR-09) because has_emitted_tool_call is True
    assert final_deltas == []


def test_parser_consecutive_tool_calls_with_noise_suppression_adr09() -> None:
    """ADR-09: Interstitial whitespace, newlines, and code fences between tool calls must be dropped."""
    parser = MultiToolLookaheadParser()

    # Tool 1
    d1 = parser.feed('<tool_call>{"name": "tool_one", "arguments": {"arg": 1}}</tool_call>')
    assert len(d1) == 1
    assert d1[0].tool_calls[0].index == 0
    assert d1[0].tool_calls[0].function.name == "tool_one"
    assert parser.has_emitted_tool_call is True

    # Noise between tool calls: newlines, spaces, markdown backticks
    d_noise = parser.feed("\n\n  \r\n```xml\n")
    # Must NOT emit content!
    assert d_noise == []

    # Tool 2
    d2 = parser.feed('<tool_call>{"name": "tool_two", "arguments": {"arg": 2}}</tool_call>')
    assert len(d2) == 1
    assert d2[0].tool_calls[0].index == 1
    assert d2[0].tool_calls[0].function.name == "tool_two"

    # Trailing noise after tools
    d_trail = parser.feed("\n```\n   ")
    assert d_trail == []

    final_deltas = parser.close()
    assert final_deltas == []
    assert parser.tool_call_index == 2


def test_parser_array_of_tool_calls_in_single_tag() -> None:
    """Parser must support JSON array of tool specifications inside a single <tool_call> block."""
    parser = MultiToolLookaheadParser()

    payload = (
        "<tool_call>[\n"
        '  {"name": "query_users", "arguments": {"limit": 10}},\n'
        '  {"name": "query_logs", "arguments": {"filter": "error"}}\n'
        "]</tool_call>"
    )

    deltas = parser.feed(payload)
    assert len(deltas) == 1
    tool_calls = deltas[0].tool_calls
    assert tool_calls is not None
    assert len(tool_calls) == 2

    assert tool_calls[0].index == 0
    assert tool_calls[0].function.name == "query_users"
    assert json.loads(tool_calls[0].function.arguments) == {"limit": 10}

    assert tool_calls[1].index == 1
    assert tool_calls[1].function.name == "query_logs"
    assert json.loads(tool_calls[1].function.arguments) == {"filter": "error"}

    assert parser.tool_call_index == 2
    assert parser.has_emitted_tool_call is True


def test_parser_fragmented_streaming_across_char_boundaries() -> None:
    """Parser must handle tags, JSON, and text sliced into 1-3 character fragments."""
    parser = MultiToolLookaheadParser()

    full_text = 'Result: <tool_call>{"name": "calc", "arguments": {"x": 5}}</tool_call> Done.'
    emitted_content: list[str] = []
    emitted_tools: list[StreamToolCall] = []

    # Feed in tiny slices
    chunk_size = 3
    for i in range(0, len(full_text), chunk_size):
        slice_part = full_text[i : i + chunk_size]
        for delta in parser.feed(slice_part):
            if delta.content:
                emitted_content.append(delta.content)
            if delta.tool_calls:
                emitted_tools.extend(delta.tool_calls)

    for delta in parser.close():
        if delta.content:
            emitted_content.append(delta.content)

    assert "".join(emitted_content).strip() == "Result:"
    assert len(emitted_tools) == 1
    assert emitted_tools[0].function.name == "calc"
    assert json.loads(emitted_tools[0].function.arguments) == {"x": 5}


def test_parser_lookahead_buffer_flushes_when_capacity_reached() -> None:
    """When buffer reaches 64 characters without a tool tag, it must flush as delta.content."""
    parser = MultiToolLookaheadParser(capacity=64)

    # Trigger lookahead with '<'
    text_chunk = "<" + "A" * 70  # 71 characters, no tool call
    deltas = parser.feed(text_chunk)

    # Should flush because capacity 64 was reached and not a tool tag
    assert len(deltas) > 0
    flushed_content = "".join(d.content for d in deltas if d.content)
    assert "<" in flushed_content
    assert "A" in flushed_content

    final_deltas = parser.close()
    all_content = flushed_content + "".join(d.content for d in final_deltas if d.content)
    assert all_content == text_chunk


def test_parser_lookahead_buffer_suppression_when_tools_already_emitted() -> None:
    """When buffer reaches 64 characters after has_emitted_tool_call, it must drop and NOT emit content."""
    parser = MultiToolLookaheadParser(capacity=64)

    # Emit a tool call first
    parser.feed('<tool_call>{"name": "test_tool"}</tool_call>')
    assert parser.has_emitted_tool_call is True

    # Now feed text that triggers lookahead
    stray_text = "<" + "Z" * 70
    deltas = parser.feed(stray_text)
    # Must NOT emit any content!
    assert all(d.content is None for d in deltas)

    final_deltas = parser.close()
    assert all(d.content is None for d in final_deltas)


# ============================================================================
# 2. Context-Scaled Delta 5h Accounting Tests
# ============================================================================


@pytest.mark.parametrize(
    ("model_name", "context_chars", "expected_delta"),
    [
        ("gemini-4-argon", 0, 0.10),
        ("gemini-4-argon", 25_000, 0.10 * (1.0 + 0.25 * 1.0)),  # 0.125
        ("gemini-4-argon", 50_000, 0.10 * (1.0 + 0.25 * 2.0)),  # 0.150
        ("gemini-3.1-pro", 0, 0.03),
        ("gemini-3.1-pro", 25_000, 0.03 * 1.25),  # 0.0375
        ("gemini-3.8-flash", 0, 0.002),
        ("gemini-3.8-flash", 25_000, 0.002 * 1.25),  # 0.0025
        ("gemini-3.1-flash-lite", 100_000, 0.0),  # Free model
    ],
)
def test_compute_context_scaled_delta_5h(
    model_name: str, context_chars: int, expected_delta: float
) -> None:
    """Optimistic 5h delta must scale linearly with context length per specification."""
    calculated = compute_context_scaled_delta_5h(model_name, context_chars)
    assert pytest.approx(calculated, rel=1e-5) == expected_delta


# ============================================================================
# 3. Stream Execution, Live Thoughts, and Account Accounting Tests
# ============================================================================


class MockStreamChunk:
    """Mock chunk matching gemini_webapi ModelOutput attributes."""

    def __init__(
        self,
        text: str = "",
        text_delta: str = "",
        thoughts: str = "",
        thoughts_delta: str = "",
    ) -> None:
        self.text = text
        self.text_delta = text_delta
        self.thoughts = thoughts
        self.thoughts_delta = thoughts_delta


@pytest.mark.asyncio
async def test_execute_web_channel_stream_live_thoughts_and_accounting() -> None:
    """Stream must dispatch live thoughts, update active_requests, last_completed, and delta_5h."""
    account = WebAccount(
        id="test_web_acc",
        cookies=WebCookies(secure_1psid="psid", secure_1psidts="psidts"),
    )
    assert account.active_requests == 0
    assert account.optimistic_5h_delta == 0.0

    mock_chunks = [
        # Thought chunks
        MockStreamChunk(thoughts="Thinking step 1...", thoughts_delta="Thinking step 1..."),
        MockStreamChunk(thoughts="Thinking step 1... step 2", thoughts_delta=" step 2"),
        # Text chunk with tool call
        MockStreamChunk(
            text_delta='Executing query: <tool_call>{"name": "db_lookup", "arguments": {"id": 42}}</tool_call>'
        ),
    ]

    async def _mock_rehydrated_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for c in mock_chunks:
            yield c

    messages = [
        {"role": "user", "content": "Look up user 42"}
    ]

    with patch("core.web_channel.execute_rehydrated_stream", side_effect=_mock_rehydrated_stream):
        received_chunks: list[StreamChunk] = []
        async for chunk in execute_web_channel_stream(
            account=account,
            client=MagicMock(),
            messages=messages,
            model="gemini-4-argon",
        ):
            assert isinstance(chunk, StreamChunk)
            received_chunks.append(chunk)

    # 1. Verify thoughts emitted
    thought_chunks = [
        c for c in received_chunks if c.choices[0].delta.reasoning_content
    ]
    assert len(thought_chunks) == 2
    assert thought_chunks[0].choices[0].delta.reasoning_content == "Thinking step 1..."
    assert thought_chunks[1].choices[0].delta.reasoning_content == " step 2"

    # 2. Verify content & tool calls
    content_chunks = [
        c for c in received_chunks if c.choices[0].delta.content
    ]
    assert len(content_chunks) >= 1
    assert "Executing query: " in "".join(c.choices[0].delta.content for c in content_chunks)

    tool_chunks = [
        c for c in received_chunks if c.choices[0].delta.tool_calls
    ]
    assert len(tool_chunks) == 1
    tc = tool_chunks[0].choices[0].delta.tool_calls[0]
    assert tc.function.name == "db_lookup"
    assert json.loads(tc.function.arguments) == {"id": 42}

    # 3. Verify final finish reason
    final_chunk = received_chunks[-1]
    assert final_chunk.choices[0].finish_reason == "tool_calls"

    # 4. Verify account runtime updates
    assert account.active_requests == 0
    assert account.last_completed_timestamp > 0.0
    assert account.last_started_timestamp > 0.0
    assert account.optimistic_5h_delta > 0.0  # Increased for gemini-4-argon


@pytest.mark.asyncio
async def test_execute_web_channel_stream_finish_reason_stop_when_no_tools() -> None:
    """Stream must emit finish_reason: 'stop' when no tools are called."""
    account = WebAccount(id="web_plain")

    mock_chunks = [
        MockStreamChunk(text_delta="Hello! "),
        MockStreamChunk(text_delta="How can I assist you today?"),
    ]

    async def _mock_rehydrated_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for c in mock_chunks:
            yield c

    with patch("core.web_channel.execute_rehydrated_stream", side_effect=_mock_rehydrated_stream):
        received_chunks: list[StreamChunk] = []
        async for chunk in execute_web_channel_stream(
            account=account,
            client=MagicMock(),
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-3.8-flash",
        ):
            received_chunks.append(chunk)

    final_chunk = received_chunks[-1]
    assert final_chunk.choices[0].finish_reason == "stop"
    assert account.optimistic_5h_delta > 0.0  # Increased for gemini-3.8-flash


@pytest.mark.asyncio
async def test_execute_web_channel_stream_error_does_not_increment_delta() -> None:
    """If an error occurs mid-stream, active_requests must decrement but optimistic_5h_delta must NOT increment."""
    account = WebAccount(id="web_err_acc")
    initial_delta = account.optimistic_5h_delta

    async def _failing_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield MockStreamChunk(text_delta="Beginning...")
        raise RuntimeError("Web connection severed mid-stream")

    with patch("core.web_channel.execute_rehydrated_stream", side_effect=_failing_stream):
        with pytest.raises(RuntimeError, match="Web connection severed mid-stream"):
            async for _ in execute_web_channel_stream(
                account=account,
                client=MagicMock(),
                messages=[{"role": "user", "content": "Hi"}],
                model="gemini-4-argon",
            ):
                pass

    # Concurrency must be restored
    assert account.active_requests == 0
    # Delta must NOT have increased on failure
    assert account.optimistic_5h_delta == initial_delta


@pytest.mark.asyncio
async def test_web_channel_class_with_session_manager() -> None:
    """WebChannel class must initialize client automatically via session_manager when client is None."""
    mock_session_mgr = MagicMock()
    mock_client = MagicMock()
    mock_session_mgr.get_or_init_client = AsyncMock(return_value=mock_client)

    channel = WebChannel(session_manager=mock_session_mgr)
    account = WebAccount(id="web_mgr_acc")

    async def _mock_rehydrated(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield MockStreamChunk(text_delta="Answer")

    with patch("core.web_channel.execute_rehydrated_stream", side_effect=_mock_rehydrated):
        results: list[dict[str, Any]] = []
        async for chunk in channel.stream_chat(
            account=account,
            messages=[{"role": "user", "content": "Question"}],
            as_dict=True,
        ):
            results.append(chunk)

    mock_session_mgr.get_or_init_client.assert_awaited_once_with(account)
    assert len(results) >= 2  # content chunk + finish chunk
    assert isinstance(results[0], dict)
    assert results[-1]["choices"][0]["finish_reason"] == "stop"


def test_format_openai_sse_event() -> None:
    """format_openai_sse_event must serialize both StreamChunk and dict into standard SSE lines."""
    chunk = StreamChunk(
        id="chatcmpl-test",
        model="gemini-4-argon",
        choices=[
            StreamChoice(
                index=0,
                delta=StreamDelta(content="Hello"),
            )
        ],
    )

    sse_line = format_openai_sse_event(chunk)
    assert sse_line.startswith("data: ")
    assert sse_line.endswith("\n\n")

    parsed = json.loads(sse_line[6:].strip())
    assert parsed["id"] == "chatcmpl-test"
    assert parsed["choices"][0]["delta"]["content"] == "Hello"

    # Also test dict input
    dict_sse = format_openai_sse_event({"id": "custom", "test": True})
    assert json.loads(dict_sse[6:].strip()) == {"id": "custom", "test": True}
