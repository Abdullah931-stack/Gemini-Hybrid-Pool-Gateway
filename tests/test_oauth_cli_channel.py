"""Unit and integration tests for Level 3 OAuth CLI Channel (core/oauth_cli_channel.py).

Verifies:
1. Production Endpoint & CloudCode Header Shield:
   - Validates endpoint is strictly `cloudcode-pa.googleapis.com` (blocking daily sandbox).
   - Verifies official Header Shield via `build_v1internal_headers`.
2. ADR-04 Strict Request Envelope Enforcement:
   - Guarantees `requestType: "agent"` is NEVER present anywhere in the envelope.
   - Raises ValueError if `requestType` is forcibly injected.
3. WAF & Schema Sanitization:
   - Extracts and sanitizes system prompts with `sanitize_system_prompt_text`.
   - Sanitizes tool parameter schemas, strips unsupported keys, and detects zero-property tools.
4. Contents Conversion & Role Mapping:
   - Maps `system` / `developer` to `systemInstruction.parts` (omitted from `contents`).
   - Maps `user` to `user` (with text and image `inlineData` extraction).
   - Maps `assistant` to `model`.
   - Maps `tool` to `functionResponse`.
5. ADR-05 History Replay Consistency:
   - Re-injects `_step: "done"` into historical `functionCall.args` for zero-property tools.
   - Leaves normal tools untouched.
6. ADR-06 Thought Signature & Tool Image Nesting & Model-Turn Guard:
   - Retrieves `thoughtSignature` from LMDBStore `db_signatures` when available.
   - Injects `skip_thought_signature_validator` when signature is absent from LMDB.
   - Nests tool output images inside `functionResponse.parts` as `inlineData`.
   - Appends `{"role": "user", "parts": [{"text": "Continue."}]}` if contents ends with `model`.
7. SSE Stream Unpacking & OpenAI Chunk Demuxer:
   - Streams `thought: true` parts as `delta.reasoning_content`.
   - Streams normal text parts as `delta.content`.
   - Streams `functionCall` as `delta.tool_calls` with `_step` stripped.
   - Automatically saves emitted `thoughtSignature` to `LMDBStore.db_signatures` under `tool_call_id`.
8. Dual Transport Support:
   - HTTP/2 execution via `httpx.AsyncClient`.
   - Subprocess execution via `AgySubprocessTransport`.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import LMDBStore, OAuthAccount, OAuthCredentials
from core.oauth_cli_channel import (
    CONTINUE_PROMPT_TEXT,
    DEFAULT_USER_AGENT,
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    V1INTERNAL_STREAM_URL,
    OAuthCLIChannel,
    build_v1internal_envelope,
    convert_messages_to_v1internal_contents,
    extract_and_sanitize_system_instruction,
    format_openai_sse_event,
    parse_v1internal_sse_line,
    sanitize_and_prepare_tools,
    transform_candidate_to_openai_chunks,
)


@pytest.fixture
def temp_lmdb_store() -> Any:
    """Provide an isolated ephemeral LMDBStore for thoughtSignature testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = LMDBStore(path=Path(tmp_dir), map_size=10 * 1024 * 1024)
        yield store
        store.close()


@pytest.fixture
def sample_oauth_account() -> OAuthAccount:
    """Create a sample active OAuthAccount instance."""
    return OAuthAccount(
        id="acc-test-oauth",
        label="Test OAuth Account",
        transport_mode="http_v1internal",
        credentials=OAuthCredentials(
            access_token="ya29.test-access-token-12345",
            refresh_token="1//test-refresh-token",
            expires_at=9999999999,
            project_id="test-cloud-project-123",
            ide_type="VSCODE",
        ),
        status="ACTIVE",
    )


# ============================================================================
# 1. Production Endpoint & CloudCode Header Shield
# ============================================================================


def test_v1internal_endpoint_production_validation() -> None:
    """Endpoint must point to cloudcode-pa.googleapis.com and never to daily sandbox."""
    assert "cloudcode-pa.googleapis.com" in V1INTERNAL_STREAM_URL
    assert "daily-cloudcode-pa.googleapis.com" not in V1INTERNAL_STREAM_URL
    assert "alt=sse" in V1INTERNAL_STREAM_URL

    channel = OAuthCLIChannel()
    assert channel.endpoint_url == V1INTERNAL_STREAM_URL


def test_v1internal_headers_shield() -> None:
    """Header shield must inject Bearer auth, correct User-Agent, and Accept sse."""
    from core.auth_oauth_cli import build_v1internal_headers

    headers = build_v1internal_headers("my-access-token", ide_type="VSCODE")
    assert headers["Authorization"] == "Bearer my-access-token"
    assert "CloudCode-PA/2.0 (VSCODE; GEMINI)" in headers["User-Agent"]
    assert "text/event-stream" in headers["Accept"]
    assert headers["X-Goog-Api-Client"] == "gl-python/3.11 grpc-gcp/1.0"


# ============================================================================
# 2. ADR-04 Strict Request Envelope Enforcement
# ============================================================================


def test_build_v1internal_envelope_adr04_strict_omission() -> None:
    """ADR-04: Envelope must never contain 'requestType': 'agent' anywhere."""
    contents = [{"role": "user", "parts": [{"text": "Hello"}]}]
    envelope = build_v1internal_envelope(
        contents=contents,
        model="gemini-3.8-flash",
        project_id="proj-456",
        reasoning_effort="low",
    )

    assert "requestType" not in envelope
    assert "requestType" not in envelope["request"]
    assert envelope["project"] == "proj-456"
    assert envelope["model"] == "gemini-3.8-flash"
    assert envelope["userAgent"] == DEFAULT_USER_AGENT
    assert envelope["requestId"].startswith("req-")
    assert envelope["request"]["contents"] == contents
    assert "thinkingConfig" in envelope["request"]["generationConfig"]


def test_build_v1internal_envelope_adr04_rejects_injected_request_type() -> None:
    """ADR-04: Explicit validation raises ValueError if requestType is present."""
    contents = [{"role": "user", "parts": [{"text": "Hello"}]}]
    envelope = build_v1internal_envelope(
        contents=contents,
        model="gemini-3.8-flash",
    )
    assert "requestType" not in envelope

    # If someone tampers with request dictionary before validation
    with patch("core.oauth_cli_channel.parse_model_and_effort") as mock_spec:
        mock_spec.return_value.base_model = "gemini-3.8-flash"
        mock_spec.return_value.reasoning_effort = "low"
        mock_spec.return_value.oauth_thinking_config = {}

        # Verify that attempting to inject requestType triggers ADR-04 validation error
        with pytest.raises(ValueError, match="ADR-04"):
            build_v1internal_envelope(
                contents=contents, model="gemini-3.8-flash", requestType="agent"
            )


# ============================================================================
# 3. WAF & Schema Sanitization
# ============================================================================


def test_extract_and_sanitize_system_instruction() -> None:
    """System prompts with <system-conventions> and RFC 2119 must be WAF-sanitized."""
    messages = [
        {
            "role": "system",
            "content": "<system-conventions>RFC 2119: You MUST adhere.</system-conventions>",
        },
        {"role": "developer", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Write code."},
    ]
    sys_instruction = extract_and_sanitize_system_instruction(messages)
    assert sys_instruction is not None
    parts = sys_instruction["parts"]
    assert len(parts) == 2

    # Verify WAF substitution
    assert "<conventions>" in parts[0]["text"]
    assert "<system-conventions>" not in parts[0]["text"]
    assert "RFC 2119" in parts[0]["text"]
    assert parts[1]["text"] == "You are a helpful assistant."


def test_sanitize_and_prepare_tools_zero_properties_detection() -> None:
    """Zero-property tools must be detected and given _step; normal tools sanitized."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "take_snapshot",
                "description": "Takes a system snapshot",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads file content",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    gemini_tools, zero_tool_names = sanitize_and_prepare_tools(tools)
    assert len(gemini_tools) == 1
    decls = gemini_tools[0]["functionDeclarations"]
    assert len(decls) == 2

    # Zero-property tool take_snapshot detected
    assert "take_snapshot" in zero_tool_names
    assert "read_file" not in zero_tool_names

    # Check take_snapshot parameters
    snapshot_decl = decls[0]
    assert snapshot_decl["name"] == "take_snapshot"
    assert "_step" in snapshot_decl["parameters"]["properties"]
    assert "additionalProperties" not in snapshot_decl["parameters"]

    # Check read_file parameters
    read_decl = decls[1]
    assert read_decl["name"] == "read_file"
    assert "minLength" not in read_decl["parameters"]["properties"]["path"]
    assert "additionalProperties" not in read_decl["parameters"]


# ============================================================================
# 4. Contents Conversion & Role Mapping
# ============================================================================


def test_convert_messages_role_mapping(temp_lmdb_store: LMDBStore) -> None:
    """OpenAI messages roles must be cleanly mapped to Gemini contents roles."""
    messages = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "List files"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": '{"dir": "core"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_123", "name": "list_dir", "content": "core/__init__.py"},
        {"role": "user", "content": "Now show auth.py"},
    ]

    contents = convert_messages_to_v1internal_contents(messages, lmdb_store=temp_lmdb_store)

    # System message must NOT be in contents
    for item in contents:
        assert item["role"] != "system"
        assert item["role"] != "developer"

    roles = [item["role"] for item in contents]
    assert roles == ["user", "model", "functionResponse", "user"]


# ============================================================================
# 5. ADR-05 History Replay Consistency (Zero-Property Tools)
# ============================================================================


def test_reinject_step_to_args_for_zero_property_tools(temp_lmdb_store: LMDBStore) -> None:
    """ADR-05: Historical functionCall.args for zero-property tools must have _step: 'done' reinjected."""
    messages = [
        {"role": "user", "content": "Take a snapshot"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_snap_1",
                    "type": "function",
                    "function": {"name": "take_snapshot", "arguments": "{}"},
                },
                {
                    "id": "call_read_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_snap_1", "name": "take_snapshot", "content": "snap_done"},
        {"role": "tool", "tool_call_id": "call_read_1", "name": "read_file", "content": "hello"},
        {"role": "user", "content": "Great, thanks"},
    ]

    zero_tools = {"take_snapshot"}
    contents = convert_messages_to_v1internal_contents(
        messages,
        lmdb_store=temp_lmdb_store,
        zero_property_tool_names=zero_tools,
    )

    # Find the model turn
    model_turn = next(c for c in contents if c["role"] == "model")
    parts = model_turn["parts"]
    assert len(parts) == 2

    # Part 0 is take_snapshot (zero-property): must have _step: 'done'
    snap_part = parts[0]["functionCall"]
    assert snap_part["name"] == "take_snapshot"
    assert snap_part["args"] == {"_step": "done"}

    # Part 1 is read_file (normal): must NOT have _step
    read_part = parts[1]["functionCall"]
    assert read_part["name"] == "read_file"
    assert read_part["args"] == {"path": "a.txt"}
    assert "_step" not in read_part["args"]


# ============================================================================
# 6. ADR-06 Thought Signature & Tool Image Nesting & Model-Turn Guard
# ============================================================================


def test_thought_signature_lmdb_retrieval_and_skip_fallback(temp_lmdb_store: LMDBStore) -> None:
    """ADR-06: Injects stored signature from LMDB, or skip_thought_signature_validator fallback."""
    # Pre-populate LMDB with signature for call_known
    temp_lmdb_store.put_signature("call_known", "base64-stored-signature-xyz")

    messages = [
        {"role": "user", "content": "Do two actions"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_known",
                    "type": "function",
                    "function": {"name": "action_a", "arguments": '{"x": 1}'},
                },
                {
                    "id": "call_unknown",
                    "type": "function",
                    "function": {"name": "action_b", "arguments": '{"y": 2}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_known", "name": "action_a", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_unknown", "name": "action_b", "content": "ok"},
        {"role": "user", "content": "Done"},
    ]

    contents = convert_messages_to_v1internal_contents(messages, lmdb_store=temp_lmdb_store)
    model_turn = next(c for c in contents if c["role"] == "model")
    parts = model_turn["parts"]

    # Call with stored signature
    assert parts[0]["thoughtSignature"] == "base64-stored-signature-xyz"

    # Call without stored signature gets fallback
    assert parts[1]["thoughtSignature"] == SKIP_THOUGHT_SIGNATURE_VALIDATOR


def test_tool_image_nesting_inside_function_response_parts() -> None:
    """ADR-06: Tool output images MUST be nested inside functionResponse.parts as inlineData."""
    b64_img = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    messages = [
        {"role": "user", "content": "Take screenshot"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_screen",
                    "type": "function",
                    "function": {"name": "screenshot", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_screen",
            "name": "screenshot",
            "content": [
                {"type": "text", "text": "Screenshot captured successfully."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
            ],
        },
        {"role": "user", "content": "What is in the image?"},
    ]

    contents = convert_messages_to_v1internal_contents(messages)
    tool_turn = next(c for c in contents if c["role"] == "functionResponse")
    fc_resp = tool_turn["parts"][0]["functionResponse"]

    assert fc_resp["name"] == "screenshot"
    assert "Screenshot captured successfully." in fc_resp["response"]["output"]

    # Image MUST be nested inside functionResponse.parts
    assert "parts" in fc_resp
    assert len(fc_resp["parts"]) == 1
    assert fc_resp["parts"][0]["inlineData"]["mimeType"] == "image/png"
    assert fc_resp["parts"][0]["inlineData"]["data"] == b64_img


def test_post_assembly_model_turn_guard_appends_continue() -> None:
    """ADR-06: Appends Continue. message if contents ends with a model turn."""
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "I am here to help."},
    ]

    contents = convert_messages_to_v1internal_contents(messages)
    assert len(contents) == 3
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"] == [{"text": CONTINUE_PROMPT_TEXT}]


# ============================================================================
# 7. SSE Stream Unpacking & OpenAI Chunk Transformation
# ============================================================================


def test_transform_candidate_thought_and_text_and_tool_call(temp_lmdb_store: LMDBStore) -> None:
    """Transforms SSE candidate with thought, text, and functionCall, saving thoughtSignature to LMDB."""
    candidate = {
        "content": {
            "parts": [
                {"thought": True, "text": "Let me think about this step."},
                {"text": "Here is the response."},
                {
                    "functionCall": {
                        "name": "take_snapshot",
                        "args": {"_step": "done", "target": "system"},
                    },
                    "thoughtSignature": "sig-from-gemini-server-123",
                },
            ]
        },
        "finishReason": "STOP",
    }

    chunks, next_idx = transform_candidate_to_openai_chunks(
        candidate,
        model="gemini-3.8-flash",
        request_id="req-test-99",
        lmdb_store=temp_lmdb_store,
        tool_call_start_index=0,
    )

    assert len(chunks) == 4  # thought chunk, content chunk, tool_call chunk, finish_reason chunk
    assert next_idx == 1

    # 1. Thought chunk
    c0 = chunks[0]
    assert c0["choices"][0]["delta"]["reasoning_content"] == "Let me think about this step."
    assert "content" not in c0["choices"][0]["delta"]

    # 2. Content chunk
    c1 = chunks[1]
    assert c1["choices"][0]["delta"]["content"] == "Here is the response."
    assert "reasoning_content" not in c1["choices"][0]["delta"]

    # 3. Tool call chunk
    c2 = chunks[2]
    tc = c2["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0
    assert tc["function"]["name"] == "take_snapshot"
    parsed_args = json.loads(tc["function"]["arguments"])
    # Synthetic _step MUST be stripped before emitting to client
    assert "_step" not in parsed_args
    assert parsed_args == {"target": "system"}

    # thoughtSignature MUST be stored in LMDB under the emitted tool_call_id
    emitted_call_id = tc["id"]
    stored_sig = temp_lmdb_store.get_signature(emitted_call_id)
    assert stored_sig == "sig-from-gemini-server-123"

    # 4. Finish reason chunk
    c3 = chunks[3]
    assert c3["choices"][0]["finish_reason"] == "tool_calls"


def test_parse_v1internal_sse_line() -> None:
    """Parses SSE lines and ignores comments/empty lines."""
    assert parse_v1internal_sse_line("") is None
    assert parse_v1internal_sse_line(": heartbeat") is None
    assert parse_v1internal_sse_line("data: [DONE]") is None

    line = 'data: {"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}'
    parsed = parse_v1internal_sse_line(line)
    assert parsed is not None
    assert "response" in parsed

    event_str = format_openai_sse_event({"id": "1", "choices": []})
    assert event_str.startswith("data: ")
    assert event_str.endswith("\n\n")


# ============================================================================
# 8. Dual Transport Support (HTTP/2 httpx & Subprocess)
# ============================================================================


@pytest.mark.asyncio
async def test_oauth_cli_channel_stream_chat_http_flow(
    temp_lmdb_store: LMDBStore,
    sample_oauth_account: OAuthAccount,
) -> None:
    """Simulates an end-to-end HTTP/2 SSE streaming call to v1internal."""
    channel = OAuthCLIChannel(lmdb_store=temp_lmdb_store)

    sse_data = (
        'data: {"response": {"candidates": [{"content": {"parts": [{"thought": true, "text": "Pondering..."}]}}]}}\n\n'
        'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Hello world!"}]}}]}}\n\n'
        'data: {"response": {"candidates": [{"finishReason": "STOP"}]}}\n\n'
    )

    # Mock httpx AsyncClient stream
    mock_response = AsyncMock()
    mock_response.status_code = 200

    async def mock_aiter_lines() -> Any:
        for line in sse_data.split("\n"):
            yield line

    mock_response.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self) -> Any:
            return mock_response

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_client = MagicMock()
    mock_client.stream.return_value = MockStreamContext()

    messages = [{"role": "user", "content": "Say hello"}]

    collected_chunks: list[dict[str, Any]] = []
    async for chunk in channel.stream_chat(
        messages=messages,
        account=sample_oauth_account,
        http_client=mock_client,
    ):
        collected_chunks.append(chunk)

    assert len(collected_chunks) >= 3

    # Check that reasoning and content chunks arrived
    thought_chunks = [c for c in collected_chunks if "reasoning_content" in c["choices"][0]["delta"]]
    content_chunks = [c for c in collected_chunks if "content" in c["choices"][0]["delta"]]
    finish_chunks = [c for c in collected_chunks if c["choices"][0].get("finish_reason") == "stop"]

    assert len(thought_chunks) == 1
    assert thought_chunks[0]["choices"][0]["delta"]["reasoning_content"] == "Pondering..."

    assert len(content_chunks) == 1
    assert content_chunks[0]["choices"][0]["delta"]["content"] == "Hello world!"

    assert len(finish_chunks) == 1


@pytest.mark.asyncio
async def test_oauth_cli_channel_stream_chat_subprocess_flow(
    temp_lmdb_store: LMDBStore,
    sample_oauth_account: OAuthAccount,
) -> None:
    """Simulates streaming via AgySubprocessTransport mode."""
    sample_oauth_account.transport_mode = "agy_subprocess"
    channel = OAuthCLIChannel(lmdb_store=temp_lmdb_store)

    mock_transport = AsyncMock()
    mock_transport.read_json_line.side_effect = [
        {"response": {"candidates": [{"content": {"parts": [{"text": "Line 1"}]}}]}},
        {"response": {"candidates": [{"content": {"parts": [{"text": "Line 2"}]}}]}},
        {"response": {"candidates": [{"finishReason": "STOP"}]}},
        None,  # EOF
    ]

    messages = [{"role": "user", "content": "Test subprocess"}]
    collected_chunks: list[dict[str, Any]] = []

    async for chunk in channel.stream_chat(
        messages=messages,
        account=sample_oauth_account,
        transport=mock_transport,
    ):
        collected_chunks.append(chunk)

    assert len(collected_chunks) == 3
    mock_transport.send_json_line.assert_called_once()
    assert collected_chunks[0]["choices"][0]["delta"]["content"] == "Line 1"
    assert collected_chunks[1]["choices"][0]["delta"]["content"] == "Line 2"
    assert collected_chunks[2]["choices"][0]["finish_reason"] == "stop"
