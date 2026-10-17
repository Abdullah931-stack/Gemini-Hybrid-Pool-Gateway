"""Web Cookie Pool streaming channel, multi-tool lookahead state-machine parser, and context-scaled delta accounting.

Implements Level 3 (Task 2 of Phase 4) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. Multi-Tool 64-Char Lookahead State Machine (ADR-09):
   - Phase 1 (``TEXT_STREAMING``): Streams regular text chunks as ``delta.content``.
     Critical noise suppression rule (ADR-09): Once ``has_emitted_tool_call == True``,
     drops and suppresses all whitespace, newlines (``\\n``), and markdown fences
     (````` ``` `````) between consecutive tool calls so text never contaminates
     tool call packets.
   - Phase 2 (``LOOKAHEAD_BUFFERING``, capacity 64 chars): Buffers characters upon
     encountering ``<`` or ````` ` ````` to inspect for ``<tool_call>`` or
     ````` ```xml\\n<tool_call> ````` prefixes. Flushes as ``delta.content`` only if
     no tool call has been emitted yet when 64 chars are reached without a tool tag.
   - Phase 3 (``TOOL_CAPTURING``): Accumulates JSON payload until ``</tool_call>``,
     parses JSON object or array of objects, and emits each tool call as
     ``delta.tool_calls`` with unique ``call_{uuid}`` and sequential ``index``
     (0, 1, 2...), setting ``has_emitted_tool_call = True`` and returning to await
     further tool calls.
   - Stream Termination: Emits final chunk with ``finish_reason: "tool_calls"`` if
     ``has_emitted_tool_call == True``, else ``finish_reason: "stop"``.
2. Context Rehydration Integration: Connects with the 3-tier rehydration engine from
   ``core/prefix_differ.py`` (``execute_rehydrated_stream``).
3. Concurrent Live Thought Streaming: Detects ``chunk.thoughts`` during streaming
   and dispatches immediately as ``delta.reasoning_content`` to avoid TTFT timeout.
4. Optimistic 5h Usage Delta: Upon successful completion, computes context-scaled
   delta ``Delta_5h(m, L) = Delta_base(m) * (1.0 + 0.25 * (L / 25000))`` and increments
   ``account.optimistic_5h_delta``, updating ``_last_completed_timestamp``.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from enum import Enum
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from config.settings import (
    LMDBStore,
    WebAccount,
    get_lmdb_store,
    get_model_base_delta,
)
import core.auth_web as _auth_web
from core.auth_web import WebSessionManager
from core.model_registry import parse_model_and_effort
from core.prefix_differ import (
    compute_messages_char_length,
    execute_rehydrated_stream,
)
import core.usage_monitor as _usage_monitor
from core.usage_monitor import check_and_apply_drift_safe_resets

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Engineering Constants & Regex Patterns
# ============================================================================

LOOKAHEAD_BUFFER_CAPACITY: Final[int] = 64
TOOL_CALL_OPEN_RE: Final[re.Pattern[str]] = re.compile(
    r"<tool_call\b([^>]*)>", re.IGNORECASE
)
TOOL_CALL_CLOSE_TAG: Final[str] = "</tool_call>"
_OPENING_FENCE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"```[a-zA-Z0-9_-]*\s*$"
)
_UNCLOSED_TOOL_TAG_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:```[a-zA-Z0-9_-]*\s*)?<tool_call\b[^>]*$", re.IGNORECASE
)

# Whitespace and newline characters dropped between consecutive tool calls (ADR-09)
_WHITESPACE_CHARS: Final[frozenset[str]] = frozenset({" ", "\t", "\r", "\n"})

_POTENTIAL_TAG_PREFIXES: Final[tuple[str, ...]] = (
    "<tool_call",
    "<tool_cal",
    "<tool_ca",
    "<tool_c",
    "<tool_",
    "<tool",
    "<too",
    "<to",
    "<t",
    "<",
)


# ============================================================================
# 2. Pydantic v2 Streaming Data Contracts (OpenAI Compatible)
# ============================================================================


class ToolCallFunction(BaseModel):
    """Function specification payload inside a streaming tool call delta."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str = ""
    arguments: str = "{}"

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item, None)

    def get(self, item: str, default: Any = None) -> Any:
        val = getattr(self, item, None)
        return val if val is not None else default

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class StreamToolCall(BaseModel):
    """Individual tool call delta emitted to agent clients (OpenAI format)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    index: int
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item, None)

    def get(self, item: str, default: Any = None) -> Any:
        val = getattr(self, item, None)
        return val if val is not None else default

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class StreamDelta(BaseModel):
    """Delta payload containing content, reasoning thoughts, or tool calls."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[StreamToolCall] | None = None
    role: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item, None)

    def get(self, item: str, default: Any = None) -> Any:
        val = getattr(self, item, None)
        return val if val is not None else default

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class StreamChoice(BaseModel):
    """Choice wrapper for a single streaming delta and finish reason."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    index: int = 0
    delta: StreamDelta
    finish_reason: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item, None)

    def get(self, item: str, default: Any = None) -> Any:
        val = getattr(self, item, None)
        return val if val is not None else default

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class StreamChunk(BaseModel):
    """Standard OpenAI-compatible streaming completion chunk."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[StreamChoice]

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item, None)

    def get(self, item: str, default: Any = None) -> Any:
        val = getattr(self, item, None)
        return val if val is not None else default

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)

    def keys(self) -> list[str]:
        return list(self.model_dump(exclude_none=True).keys())

    def values(self) -> list[Any]:
        return list(self.model_dump(exclude_none=True).values())

    def items(self) -> list[tuple[str, Any]]:
        return list(self.model_dump(exclude_none=True).items())

    def to_dict(self) -> dict[str, Any]:
        """Convert chunk into an OpenAI-compliant dictionary structure."""
        return self.model_dump(exclude_none=True)

    def to_sse_event(self) -> str:
        """Serialize chunk to a standard Server-Sent Events (SSE) data line."""
        dumped = self.model_dump(exclude_none=True)
        return f"data: {json.dumps(dumped, ensure_ascii=False)}\n\n"


def format_openai_sse_event(chunk: StreamChunk | Mapping[str, Any]) -> str:
    """Format a StreamChunk or dictionary as an OpenAI Server-Sent Events line."""
    if isinstance(chunk, StreamChunk):
        payload = chunk.to_dict()
    elif isinstance(chunk, BaseModel):
        payload = chunk.model_dump(exclude_none=True)
    elif isinstance(chunk, Mapping):
        payload = dict(chunk)
    else:
        payload = dict(chunk)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ============================================================================
# 3. Multi-Tool 64-Character Lookahead State Machine Parser (ADR-09)
# ============================================================================


class ParserPhase(str, Enum):
    """Operational phases of the multi-tool lookahead stream parser."""

    TEXT_STREAMING = "TEXT_STREAMING"
    LOOKAHEAD_BUFFERING = "LOOKAHEAD_BUFFERING"
    TOOL_CAPTURING = "TOOL_CAPTURING"


def _clean_json_code_block(raw_text: str) -> str:
    """Strip markdown code fence blocks (```json ... ```) wrapping JSON payloads."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_raw_tool_payload(raw_payload: str, fallback_name: str = "") -> list[dict[str, Any]]:
    """Parse raw XML inner text into a list of normalized tool call dictionary specifications."""
    cleaned = _clean_json_code_block(raw_payload)
    if not cleaned:
        return []

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        sanitized = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(cleaned)
            except (ValueError, SyntaxError):
                logger.warning("Failed to parse tool call JSON payload: %s", raw_payload[:120])
                return []

    items: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = [item for item in parsed if isinstance(item, dict)]

    results: list[dict[str, Any]] = []
    for item in items:
        name = (
            item.get("name")
            or item.get("tool_name")
            or (item.get("function", {}).get("name") if isinstance(item.get("function"), dict) else None)
            or fallback_name
            or "unnamed_tool"
        )

        raw_args = item.get("arguments")
        if raw_args is None:
            raw_args = item.get("parameters")
        if raw_args is None:
            raw_args = item.get("args")
        if raw_args is None and isinstance(item.get("function"), dict):
            raw_args = item["function"].get("arguments")

        if isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        elif isinstance(raw_args, str):
            args_str = raw_args
        else:
            args_str = "{}"

        results.append({"name": str(name), "arguments": args_str})

    return results


class MultiToolLookaheadParser:
    """3-phase lookahead parser supporting multiple and consecutive XML tool calls.

    Implements ADR-09:
    1. Phase 1 (``TEXT_STREAMING``): Emits plain text as ``delta.content``.
       When ``has_emitted_tool_call`` is True, all whitespace, newlines, and markdown
       code fences are suppressed and dropped between consecutive tool calls.
    2. Phase 2 (``LOOKAHEAD_BUFFERING``): 64-char lookahead buffer triggered by ``<``
       or ````` ` `````. Flushes as text only if no tool call has been emitted yet.
    3. Phase 3 (``TOOL_CAPTURING``): Captures JSON within ``<tool_call>...</tool_call>``,
       parses single objects or arrays, assigns sequential indices, sets
       ``has_emitted_tool_call = True``, and returns to stream or await next tool calls.
    """

    def __init__(self, capacity: int = LOOKAHEAD_BUFFER_CAPACITY) -> None:
        self.capacity: int = capacity
        self.phase: ParserPhase = ParserPhase.TEXT_STREAMING
        self.has_emitted_tool_call: bool = False
        self.tool_call_index: int = 0
        self._lookahead_buffer: list[str] = []
        self._tool_capture_buffer: list[str] = []
        self._active_tag_name_attr: str = ""

    def reset(self) -> None:
        """Reset parser state for a new conversation turn."""
        self.phase = ParserPhase.TEXT_STREAMING
        self.has_emitted_tool_call = False
        self.tool_call_index = 0
        self._lookahead_buffer.clear()
        self._tool_capture_buffer.clear()
        self._active_tag_name_attr = ""

    def _create_stream_tool_calls(
        self,
        raw_payload: str,
        tag_attr: str = "",
    ) -> list[StreamToolCall]:
        """Convert raw captured JSON into sequential StreamToolCall instances."""
        parsed_tools = _parse_raw_tool_payload(raw_payload, fallback_name=tag_attr)
        emitted_calls: list[StreamToolCall] = []
        for tool_spec in parsed_tools:
            call_id = f"call_{uuid4().hex[:24]}"
            idx = self.tool_call_index
            self.tool_call_index += 1
            self.has_emitted_tool_call = True
            emitted_calls.append(
                StreamToolCall(
                    index=idx,
                    id=call_id,
                    type="function",
                    function=ToolCallFunction(
                        name=tool_spec["name"],
                        arguments=tool_spec["arguments"],
                    ),
                )
            )
        return emitted_calls

    def feed(self, chunk: str) -> list[StreamDelta]:
        """Ingest an incoming text chunk and yield parsed StreamDelta objects."""
        if not chunk:
            return []

        deltas: list[StreamDelta] = []
        chars = list(chunk)
        idx = 0
        total_len = len(chars)

        while idx < total_len:
            # ================================================================
            # Phase 1: TEXT_STREAMING
            # ================================================================
            if self.phase == ParserPhase.TEXT_STREAMING:
                if self.has_emitted_tool_call:
                    # ADR-09 noise suppression rule:
                    # Suppress whitespace, newlines, and markdown backticks between tool calls
                    curr_char = chars[idx]
                    if curr_char in _WHITESPACE_CHARS:
                        idx += 1
                        continue
                    if curr_char in ("<", "`"):
                        self.phase = ParserPhase.LOOKAHEAD_BUFFERING
                        self._lookahead_buffer = [curr_char]
                        idx += 1
                        continue
                    # Any other character encountered after tools: buffer into lookahead
                    self.phase = ParserPhase.LOOKAHEAD_BUFFERING
                    self._lookahead_buffer = [curr_char]
                    idx += 1
                    continue
                else:
                    # Scan ahead for '<' or '`'
                    text_acc: list[str] = []
                    while idx < total_len:
                        c = chars[idx]
                        if c in ("<", "`"):
                            self.phase = ParserPhase.LOOKAHEAD_BUFFERING
                            self._lookahead_buffer = [c]
                            idx += 1
                            break
                        text_acc.append(c)
                        idx += 1

                    if text_acc:
                        deltas.append(StreamDelta(content="".join(text_acc), role="assistant"))

            # ================================================================
            # Phase 2: LOOKAHEAD_BUFFERING
            # ================================================================
            elif self.phase == ParserPhase.LOOKAHEAD_BUFFERING:
                while idx < total_len and self.phase == ParserPhase.LOOKAHEAD_BUFFERING:
                    self._lookahead_buffer.append(chars[idx])
                    idx += 1

                    current_buf_str = "".join(self._lookahead_buffer)
                    match = TOOL_CALL_OPEN_RE.search(current_buf_str)
                    if match:
                        # Emit any non-fence text preceding the opening <tool_call...> tag
                        raw_prefix = current_buf_str[: match.start()]
                        if raw_prefix and not self.has_emitted_tool_call:
                            fence_match = _OPENING_FENCE_SUFFIX_RE.search(raw_prefix)
                            prefix_before_fence = (
                                raw_prefix[: fence_match.start()]
                                if fence_match
                                else raw_prefix
                            )
                            if prefix_before_fence:
                                deltas.append(
                                    StreamDelta(
                                        content=prefix_before_fence, role="assistant"
                                    )
                                )

                        # Opening tag found! Transition to TOOL_CAPTURING
                        self._active_tag_name_attr = ""
                        attr_match = re.search(r'name=["\']([^"\']+)["\']', match.group(1))
                        if attr_match:
                            self._active_tag_name_attr = attr_match.group(1)

                        tag_end = match.end()
                        remainder = current_buf_str[tag_end:]
                        self._lookahead_buffer.clear()
                        self._tool_capture_buffer = [remainder] if remainder else []
                        self.phase = ParserPhase.TOOL_CAPTURING
                        break

                    # Check if 64-character buffer capacity reached
                    if len(self._lookahead_buffer) >= self.capacity:
                        unclosed_match = _UNCLOSED_TOOL_TAG_SUFFIX_RE.search(current_buf_str)
                        if unclosed_match:
                            if unclosed_match.start() > 0:
                                to_flush = current_buf_str[: unclosed_match.start()]
                                self._lookahead_buffer = list(
                                    current_buf_str[unclosed_match.start() :]
                                )
                                if to_flush and not self.has_emitted_tool_call:
                                    deltas.append(
                                        StreamDelta(content=to_flush, role="assistant")
                                    )
                                continue
                            if len(self._lookahead_buffer) < self.capacity * 4:
                                # Allow <tool_call ...> with attributes to finish closing '>'
                                continue

                        matched_sfx_len = 0
                        for sfx in _POTENTIAL_TAG_PREFIXES:
                            if current_buf_str.endswith(sfx):
                                matched_sfx_len = len(sfx)
                                break

                        if matched_sfx_len > 0:
                            to_flush = current_buf_str[:-matched_sfx_len]
                            self._lookahead_buffer = list(current_buf_str[-matched_sfx_len:])
                        else:
                            to_flush = current_buf_str
                            self._lookahead_buffer.clear()
                            self.phase = ParserPhase.TEXT_STREAMING

                        # ADR-09: flush as content ONLY if no tool call has been emitted yet
                        if to_flush and not self.has_emitted_tool_call:
                            deltas.append(StreamDelta(content=to_flush, role="assistant"))

            # ================================================================
            # Phase 3: TOOL_CAPTURING
            # ================================================================
            elif self.phase == ParserPhase.TOOL_CAPTURING:
                while idx < total_len:
                    self._tool_capture_buffer.append(chars[idx])
                    idx += 1
                    current_capture = "".join(self._tool_capture_buffer)
                    close_pos = current_capture.find(TOOL_CALL_CLOSE_TAG)
                    if close_pos != -1:
                        raw_payload = current_capture[:close_pos]
                        after_close = current_capture[close_pos + len(TOOL_CALL_CLOSE_TAG):]
                        self._tool_capture_buffer.clear()
                        self.phase = ParserPhase.TEXT_STREAMING

                        tool_calls = self._create_stream_tool_calls(
                            raw_payload, tag_attr=self._active_tag_name_attr
                        )
                        self._active_tag_name_attr = ""
                        if tool_calls:
                            deltas.append(StreamDelta(tool_calls=tool_calls, role="assistant"))

                        # Process remaining characters after </tool_call>
                        if after_close:
                            sub_deltas = self.feed(after_close)
                            deltas.extend(sub_deltas)
                        break

        # Edge-case: In Phase 3, check if complete tool call arrived inside remainder
        if self.phase == ParserPhase.TOOL_CAPTURING and self._tool_capture_buffer:
            current_capture = "".join(self._tool_capture_buffer)
            close_pos = current_capture.find(TOOL_CALL_CLOSE_TAG)
            if close_pos != -1:
                raw_payload = current_capture[:close_pos]
                after_close = current_capture[close_pos + len(TOOL_CALL_CLOSE_TAG):]
                self._tool_capture_buffer.clear()
                self.phase = ParserPhase.TEXT_STREAMING

                tool_calls = self._create_stream_tool_calls(
                    raw_payload, tag_attr=self._active_tag_name_attr
                )
                self._active_tag_name_attr = ""
                if tool_calls:
                    deltas.append(StreamDelta(tool_calls=tool_calls, role="assistant"))

                if after_close:
                    sub_deltas = self.feed(after_close)
                    deltas.extend(sub_deltas)

        return deltas

    def close(self) -> list[StreamDelta]:
        """Flush any pending buffered content at end of stream."""
        deltas: list[StreamDelta] = []

        # Flush Phase 2 lookahead buffer
        if self._lookahead_buffer:
            flush_text = "".join(self._lookahead_buffer)
            self._lookahead_buffer.clear()
            if flush_text and not self.has_emitted_tool_call:
                deltas.append(StreamDelta(content=flush_text, role="assistant"))

        # Flush Phase 3 capture buffer if model omitted closing tag at EOF
        if self._tool_capture_buffer:
            capture_text = "".join(self._tool_capture_buffer)
            self._tool_capture_buffer.clear()
            tool_calls = self._create_stream_tool_calls(
                capture_text, tag_attr=self._active_tag_name_attr
            )
            self._active_tag_name_attr = ""
            if tool_calls:
                deltas.append(StreamDelta(tool_calls=tool_calls, role="assistant"))
            elif capture_text and not self.has_emitted_tool_call:
                deltas.append(StreamDelta(content=f"<tool_call>{capture_text}", role="assistant"))

        self.phase = ParserPhase.TEXT_STREAMING
        return deltas


# ============================================================================
# 4. Context-Scaled Delta-5h Accounting
# ============================================================================


def compute_context_scaled_delta_5h(
    model_name: str,
    context_chars: int,
) -> float:
    """Calculate context-weighted optimistic 5-hour usage delta.

    Formula (from 03_SCHEDULING_AND_CONTROL_SPEC.md):
    ``Delta_5h(m, L_chars) = Delta_base(m) * (1.0 + 0.25 * (L_chars / 25000))``
    Strips any reasoning effort suffix and caps ``gemini-3.1-pro`` at ``0.09``.
    """
    base_model = parse_model_and_effort(str(model_name)).base_model
    base_delta = get_model_base_delta(base_model)
    if base_delta <= 0.0:
        return 0.0
    safe_chars = max(0, context_chars)
    scaled_delta = base_delta * (1.0 + 0.25 * (safe_chars / 25000.0))
    if base_model == "gemini-3.1-pro":
        scaled_delta = min(0.09, scaled_delta)
    return scaled_delta


def _build_chunk(
    request_id: str,
    model: str,
    delta: StreamDelta,
    finish_reason: str | None = None,
) -> StreamChunk:
    """Construct an OpenAI-compatible StreamChunk."""
    return StreamChunk(
        id=request_id,
        model=model,
        choices=[
            StreamChoice(
                index=0,
                delta=delta,
                finish_reason=finish_reason,
            )
        ],
    )


# ============================================================================
# 5. Core Streaming Execution Generator
# ============================================================================


async def execute_web_channel_stream(
    account: WebAccount,
    client: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str | Any = "gemini-4-argon",
    reasoning_effort: str | None = None,
    system_prompt: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
    tool_choice: Any | None = None,
    headers: Mapping[str, str] | None = None,
    request_id: str | None = None,
    enable_token_saver: bool | None = True,
    enable_waf_shield: bool = True,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    response_format: Mapping[str, Any] | None = None,
    lmdb_store: LMDBStore | None = None,
    temp_dir: Path | str | None = None,
    as_dict: bool = False,
    track_concurrency: bool = True,
    time_fn: Callable[[], float] = time.time,
    **kwargs: Any,
) -> AsyncIterator[StreamChunk | dict[str, Any]]:
    """Stream response from Google AI Pro Web Pool with 3-tier rehydration and lookahead parsing.

    Key guarantees:
    1. Active requests increment on start and decrement in ``finally:`` when ``track_concurrency=True``.
    2. Concurrent live reasoning: streams ``chunk.thoughts`` immediately as ``reasoning_content``
       with cumulative prefix deduplication.
    3. MultiToolLookaheadParser filters XML tool blocks, dropping interstitial noise (ADR-09).
    4. Upon successful completion: applies drift-safe resets, increments ``account.optimistic_5h_delta``
       and ``account.optimistic_weekly_delta``, resets consecutive errors, and persists rotated cookies.
    """
    rid = request_id or f"chatcmpl-{uuid4().hex}"
    model_str = str(model) if model else "gemini-4-argon"
    resolved_spec = parse_model_and_effort(model_str, reasoning_effort=reasoning_effort)
    base_model = resolved_spec.base_model
    eff_token_saver = True if enable_token_saver is None else bool(enable_token_saver)

    # Track request start & concurrency
    start_time = float(time_fn())
    if account is not None:
        if track_concurrency:
            account.active_requests += 1
        account.last_started_timestamp = start_time
        if hasattr(account, "_last_started_timestamp"):
            account._last_started_timestamp = start_time

    parser = MultiToolLookaheadParser(capacity=LOOKAHEAD_BUFFER_CAPACITY)
    accumulated_text_len = 0
    emitted_thoughts_prefix: str = ""
    stream_succeeded = False

    try:
        raw_stream = execute_rehydrated_stream(
            account=account,
            client=client,
            messages=messages,
            model=base_model if isinstance(model, str) else model,
            system_prompt=system_prompt,
            tools=tools,
            headers=headers,
            enable_token_saver=eff_token_saver,
            enable_waf_shield=enable_waf_shield,
            lmdb_store=lmdb_store,
            temp_dir=temp_dir,
        )

        async for chunk in raw_stream:
            # 1. Live Reasoning Stream: emit incremental thoughts immediately
            thought_delta = getattr(chunk, "thoughts_delta", None)
            if isinstance(thought_delta, str) and thought_delta:
                emitted_thoughts_prefix += thought_delta
                c = _build_chunk(
                    rid,
                    model_str,
                    StreamDelta(reasoning_content=thought_delta, role="assistant"),
                )
                yield c.to_dict() if as_dict else c
            else:
                raw_thoughts = getattr(chunk, "thoughts", None)
                if isinstance(raw_thoughts, str) and raw_thoughts:
                    if emitted_thoughts_prefix and raw_thoughts.startswith(
                        emitted_thoughts_prefix
                    ):
                        new_thought = raw_thoughts[len(emitted_thoughts_prefix) :]
                        emitted_thoughts_prefix = raw_thoughts
                    else:
                        new_thought = raw_thoughts
                        emitted_thoughts_prefix = (
                            raw_thoughts
                            if not emitted_thoughts_prefix
                            else emitted_thoughts_prefix + raw_thoughts
                        )
                    if new_thought:
                        c = _build_chunk(
                            rid,
                            model_str,
                            StreamDelta(reasoning_content=new_thought, role="assistant"),
                        )
                        yield c.to_dict() if as_dict else c

            # 2. Extract incremental text delta
            text_delta = getattr(chunk, "text_delta", None)
            if not text_delta:
                full_text = getattr(chunk, "text", None)
                if isinstance(full_text, str) and len(full_text) > accumulated_text_len:
                    text_delta = full_text[accumulated_text_len:]

            if text_delta:
                accumulated_text_len += len(text_delta)
                for parsed_delta in parser.feed(text_delta):
                    c = _build_chunk(rid, model_str, parsed_delta)
                    yield c.to_dict() if as_dict else c

        # 3. Flush lookahead parser at stream end
        for final_delta in parser.close():
            c = _build_chunk(rid, model_str, final_delta)
            yield c.to_dict() if as_dict else c

        # 4. Emit final termination chunk
        finish_reason = "tool_calls" if parser.has_emitted_tool_call else "stop"
        final_chunk = _build_chunk(
            rid,
            model_str,
            StreamDelta(role=None),
            finish_reason=finish_reason,
        )
        yield final_chunk.to_dict() if as_dict else final_chunk

        stream_succeeded = True

    finally:
        end_time = float(time_fn())
        if account is not None:
            if track_concurrency:
                account.active_requests = max(0, account.active_requests - 1)
            account.last_completed_timestamp = end_time
            if hasattr(account, "_last_completed_timestamp"):
                account._last_completed_timestamp = end_time

            if stream_succeeded:
                # 1. Drift-safe reset check before accumulating new deltas
                try:
                    check_and_apply_drift_safe_resets(
                        account, current_time=end_time  # type: ignore[call-arg]
                    )
                except TypeError:
                    check_and_apply_drift_safe_resets(account, now=end_time)

                # 2. Compute and accumulate 5h and weekly optimistic deltas
                context_chars = compute_messages_char_length(
                    messages, system_prompt=system_prompt, tools=tools
                )
                delta_5h = compute_context_scaled_delta_5h(base_model, context_chars)
                account.optimistic_5h_delta += delta_5h

                compute_weekly_fn = getattr(
                    _usage_monitor, "compute_optimistic_weekly_delta", None
                )
                if callable(compute_weekly_fn):
                    try:
                        delta_weekly = float(compute_weekly_fn(delta_5h))
                    except TypeError:
                        delta_weekly = float(compute_weekly_fn(base_model, context_chars))
                else:
                    delta_weekly = round(delta_5h * 0.25, 6)
                account.optimistic_weekly_delta += delta_weekly

                # 3. Reset error tracking state
                account.consecutive_errors = 0
                account.last_error_reason = None

                # 4. Persist rotated cookies if client carries updated cookies
                if client is not None:
                    try:
                        extract_fn = getattr(_auth_web, "extract_cookies_from_client", None)
                        if callable(extract_fn):
                            extracted = extract_fn(client)
                        else:
                            extracted = _auth_web.extract_client_web_cookies(client)

                        has_cookies = False
                        if isinstance(extracted, Mapping):
                            psid = str(
                                extracted.get("__Secure-1PSID")
                                or extracted.get("secure_1psid")
                                or ""
                            ).strip()
                            psidts = str(
                                extracted.get("__Secure-1PSIDTS")
                                or extracted.get("secure_1psidts")
                                or ""
                            ).strip()
                            if psid:
                                account.cookies.secure_1psid = psid
                                if psidts:
                                    account.cookies.secure_1psidts = psidts
                                has_cookies = True
                        elif hasattr(extracted, "secure_1psid"):
                            psid = str(getattr(extracted, "secure_1psid", "")).strip()
                            psidts = str(getattr(extracted, "secure_1psidts", "")).strip()
                            if psid:
                                account.cookies.secure_1psid = psid
                                if psidts:
                                    account.cookies.secure_1psidts = psidts
                                has_cookies = True

                        if has_cookies:
                            try:
                                save_res = _auth_web.save_account_cookies_isolated(
                                    account, client=client
                                )
                            except TypeError:
                                save_res = _auth_web.save_account_cookies_isolated(account)
                            if inspect.isawaitable(save_res):
                                await save_res
                    except Exception as cookie_exc:
                        logger.debug(
                            "Skipping post-stream cookie persistence for account '%s': %s",
                            account.id,
                            cookie_exc,
                        )

                logger.info(
                    "Web channel turn completed successfully for account '%s' "
                    "(model: %s, context: %d chars). Added delta_5h: +%.5f -> "
                    "total optimistic_5h_delta: %.5f, optimistic_weekly_delta: %.5f",
                    account.id,
                    base_model,
                    context_chars,
                    delta_5h,
                    account.optimistic_5h_delta,
                    account.optimistic_weekly_delta,
                )


# ============================================================================
# 6. High-Level WebChannel Adapter Class
# ============================================================================


class WebChannel:
    """High-level gateway channel adapter for Google AI Pro Web Cookie Pool."""

    def __init__(
        self,
        session_manager: WebSessionManager | None = None,
        lmdb_store: LMDBStore | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.session_manager: WebSessionManager | None = session_manager
        self.lmdb_store: LMDBStore = (
            lmdb_store if lmdb_store is not None else get_lmdb_store()
        )
        self.temp_dir: Path | str | None = temp_dir

    async def stream_chat(
        self,
        account: WebAccount,
        messages: Sequence[Mapping[str, Any]],
        *,
        client: Any | None = None,
        model: str | Any = "gemini-4-argon",
        reasoning_effort: str | None = None,
        system_prompt: str = "",
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any | None = None,
        headers: Mapping[str, str] | None = None,
        request_id: str | None = None,
        enable_token_saver: bool | None = True,
        enable_waf_shield: bool = True,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        as_dict: bool = False,
        track_concurrency: bool = True,
        time_fn: Callable[[], float] = time.time,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk | dict[str, Any]]:
        """Execute a streaming chat completion turn via the Web Cookie Pool."""
        active_client = client
        if active_client is None:
            if self.session_manager is None:
                raise ValueError(
                    "WebChannel has no WebSessionManager and no client was provided."
                )
            active_client = await self.session_manager.get_or_init_client(account)

        async for chunk in execute_web_channel_stream(
            account=account,
            client=active_client,
            messages=messages,
            model=model,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            tools=tools,
            tool_choice=tool_choice,
            headers=headers,
            request_id=request_id,
            enable_token_saver=enable_token_saver,
            enable_waf_shield=enable_waf_shield,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            response_format=response_format,
            lmdb_store=self.lmdb_store,
            temp_dir=self.temp_dir,
            as_dict=as_dict,
            track_concurrency=track_concurrency,
            time_fn=time_fn,
            **kwargs,
        ):
            yield chunk


__all__ = [
    "LOOKAHEAD_BUFFER_CAPACITY",
    "TOOL_CALL_CLOSE_TAG",
    "TOOL_CALL_OPEN_RE",
    "MultiToolLookaheadParser",
    "ParserPhase",
    "StreamChoice",
    "StreamChunk",
    "StreamDelta",
    "StreamToolCall",
    "ToolCallFunction",
    "WebChannel",
    "compute_context_scaled_delta_5h",
    "execute_web_channel_stream",
    "format_openai_sse_event",
]
