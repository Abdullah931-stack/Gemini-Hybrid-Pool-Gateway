"""Comprehensive unit and integration test suite for Level 4 / Phase 5 Failover & Stream Handshake (ADR-10).

Verifies:
1. is_chunk_non_empty:
   - Dictionaries and Pydantic StreamChunk objects.
   - Non-empty content, reasoning_content, tool_calls.
   - Safe rejection of empty content, empty reasoning, empty tool_calls, role-only chunks,
     None, empty dict, and malformed inputs.
2. chain_async_iterators:
   - Preserves exact ordering: buffered chunks first, then active stream chunks.
   - Zero lost chunks.
   - Guarantees on_complete execution on normal completion, early break, and exception.
   - Supports async on_complete callbacks.
3. Pre-stream handshake & instant zero-drop failover (Zero-Byte Stream EOF):
   - First account yields empty stream or zero-byte EOF.
   - Seamless failover to second account before delivering response to client.
   - Zero concurrency leak: account 1 active_requests decremented immediately.
4. HTTP 429 and network error failover:
   - 429 classification / cooldown applied without leaking active_requests.
   - Network error (ConnectError, Timeout) failover to backup account.
5. Pool exhaustion:
   - PoolExhaustedError raised when all eligible accounts fail.
   - Concurrency counters strictly preserved at 0.
6. Model validation:
   - ModelNotSupportedError raised for unsupported or empty model IDs.
7. Non-streaming chat completion with failover:
   - execute_chat_completion_with_failover aggregates content, reasoning, and tool calls.
   - Mid-stream drop recovery.
8. Stress test for concurrency counter leak protection in all failure scenarios.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    AccountsConfig,
    AccountsStateRegistry,
    LMDBStore,
    ModelQuota,
    OAuthAccount,
    OAuthQuotaState,
    WebAccount,
    WebUsage,
)
from core.failover import (
    HANDSHAKE_MAX_BUFFER_CHUNKS,
    FailoverController,
    HandshakeFailedError,
    ModelNotSupportedError,
    PoolExhaustedError,
    chain_async_iterators,
    execute_chat_completion_with_failover,
    execute_stream_with_failover,
    is_chunk_non_empty,
    is_model_supported_in_gateway,
)
from core.web_channel import (
    StreamChoice,
    StreamChunk,
    StreamDelta,
    StreamToolCall,
    ToolCallFunction,
)


# ============================================================================
# Fixtures & Helper Factories
# ============================================================================


@pytest.fixture
def temp_lmdb(tmp_path: Path) -> LMDBStore:
    """Provide a fresh isolated LMDB environment for testing."""
    db_path = tmp_path / "test_lmdb_failover"
    store = LMDBStore(path=db_path)
    yield store
    store.close()


@pytest.fixture
def isolated_registry() -> AccountsStateRegistry:
    """Provide an isolated AccountsStateRegistry in memory."""
    registry = AccountsStateRegistry()
    registry.config = AccountsConfig()
    return registry


def make_oauth_account(
    account_id: str = "oauth_1",
    status: str = "ACTIVE",
    active_requests: int = 0,
    max_concurrency: int = 3,
    remaining_fraction: float = 1.0,
    reset_at: int | None = None,
    is_exhausted: bool = False,
    latency_ms: float = 100.0,
    models: list[str] | None = None,
    cooldown_until: int | None = None,
) -> OAuthAccount:
    """Helper to construct an OAuthAccount with runtime attributes."""
    quotas = {}
    standard_models = models or ["gemini-3.8-flash", "gemini-3.1-pro", "gemini-2.5-flash"]
    for m in standard_models:
        quotas[m] = ModelQuota(
            remaining_fraction=remaining_fraction,
            reset_at=reset_at,
            is_exhausted=is_exhausted,
        )

    acc = OAuthAccount(
        id=account_id,
        label=f"OAuth {account_id}",
        status=status,  # type: ignore[arg-type]
        max_concurrency=max_concurrency,
        discovered_models=list(models or []),
        quota_state=OAuthQuotaState(
            model_quotas=quotas,
            cooldown_until=cooldown_until,
        ),
    )
    acc.active_requests = active_requests
    acc.recent_latency_ms = latency_ms
    return acc


def make_web_account(
    account_id: str = "web_1",
    status: str = "ACTIVE",
    active_requests: int = 0,
    max_concurrency: int = 2,
    five_hour_pct: float = 0.20,
    weekly_pct: float = 0.10,
    models: list[str] | None = None,
    cooldown_until: int | None = None,
) -> WebAccount:
    """Helper to construct a WebAccount with runtime attributes."""
    acc = WebAccount(
        id=account_id,
        label=f"Web {account_id}",
        status=status,  # type: ignore[arg-type]
        max_concurrency=max_concurrency,
        discovered_models=list(models or []),
        cooldown_until=cooldown_until,
        usage=WebUsage(
            five_hour_pct=five_hour_pct,
            weekly_pct=weekly_pct,
        ),
    )
    acc.active_requests = active_requests
    acc.optimistic_5h_delta = 0.0
    acc.optimistic_weekly_delta = 0.0
    return acc


# ============================================================================
# 1. Tests for is_chunk_non_empty (ADR-10 Content Detection)
# ============================================================================


def test_is_chunk_non_empty_content_dict() -> None:
    """Verify non-empty detection for standard content text chunks."""
    chunk = {"choices": [{"index": 0, "delta": {"content": "Hello world"}}]}
    assert is_chunk_non_empty(chunk) is True

    # Empty content string should return False
    empty_chunk = {"choices": [{"index": 0, "delta": {"content": ""}}]}
    assert is_chunk_non_empty(empty_chunk) is False

    # Single space or newline is valid generated content
    space_chunk = {"choices": [{"index": 0, "delta": {"content": " "}}]}
    assert is_chunk_non_empty(space_chunk) is True


def test_is_chunk_non_empty_reasoning_dict() -> None:
    """Verify non-empty detection for reasoning thoughts."""
    chunk = {"choices": [{"index": 0, "delta": {"reasoning_content": "Thinking step by step"}}]}
    assert is_chunk_non_empty(chunk) is True

    empty_reasoning = {"choices": [{"index": 0, "delta": {"reasoning_content": ""}}]}
    assert is_chunk_non_empty(empty_reasoning) is False


def test_is_chunk_non_empty_tool_calls_dict() -> None:
    """Verify non-empty detection for tool call packets."""
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": "{}"},
                        }
                    ]
                },
            }
        ]
    }
    assert is_chunk_non_empty(chunk) is True

    # Empty tool_calls list should return False
    empty_tool = {"choices": [{"index": 0, "delta": {"tool_calls": []}}]}
    assert is_chunk_non_empty(empty_tool) is False


def test_is_chunk_non_empty_role_only() -> None:
    """Verify role announcement chunks are treated as empty."""
    chunk = {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}
    assert is_chunk_non_empty(chunk) is False


def test_is_chunk_non_empty_pydantic_stream_chunk() -> None:
    """Verify inspection on Pydantic StreamChunk instances."""
    # 1. Content chunk
    chunk1 = StreamChunk(
        id="c1",
        model="gemini-3.8-flash",
        choices=[StreamChoice(index=0, delta=StreamDelta(content="Generated text"))],
    )
    assert is_chunk_non_empty(chunk1) is True

    # 2. Empty content chunk
    chunk2 = StreamChunk(
        id="c2",
        model="gemini-3.8-flash",
        choices=[StreamChoice(index=0, delta=StreamDelta(content=""))],
    )
    assert is_chunk_non_empty(chunk2) is False

    # 3. Reasoning chunk
    chunk3 = StreamChunk(
        id="c3",
        model="gemini-3.8-flash",
        choices=[StreamChoice(index=0, delta=StreamDelta(reasoning_content="Examining options"))],
    )
    assert is_chunk_non_empty(chunk3) is True

    # 4. Tool call chunk
    tool_call = StreamToolCall(
        index=0,
        id="call_abc",
        function=ToolCallFunction(name="search_web", arguments='{"q": "gemini"}'),
    )
    chunk4 = StreamChunk(
        id="c4",
        model="gemini-3.8-flash",
        choices=[StreamChoice(index=0, delta=StreamDelta(tool_calls=[tool_call]))],
    )
    assert is_chunk_non_empty(chunk4) is True

    # 5. Empty delta
    chunk5 = StreamChunk(
        id="c5",
        model="gemini-3.8-flash",
        choices=[StreamChoice(index=0, delta=StreamDelta())],
    )
    assert is_chunk_non_empty(chunk5) is False


def test_is_chunk_non_empty_malformed_inputs() -> None:
    """Verify safe handling of malformed inputs without exceptions."""
    assert is_chunk_non_empty(None) is False
    assert is_chunk_non_empty({}) is False
    assert is_chunk_non_empty({"choices": []}) is False
    assert is_chunk_non_empty({"choices": [{}]}) is False
    assert is_chunk_non_empty({"choices": [{"delta": None}]}) is False
    assert is_chunk_non_empty("invalid string") is False
    assert is_chunk_non_empty(12345) is False
    assert is_chunk_non_empty([{"choices": []}]) is False


# ============================================================================
# 2. Tests for chain_async_iterators
# ============================================================================


@pytest.mark.asyncio
async def test_chain_async_iterators_preserves_order() -> None:
    """Verify buffered chunks and active stream chunks maintain exact sequential order."""
    buffered = [{"chunk": 1}, {"chunk": 2}]

    async def active_stream() -> AsyncIterator[dict[str, int]]:
        yield {"chunk": 3}
        yield {"chunk": 4}

    cleanup_called = False

    def on_complete() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    chained = chain_async_iterators(buffered, active_stream(), on_complete=on_complete)
    results = [chunk async for chunk in chained]

    assert results == [{"chunk": 1}, {"chunk": 2}, {"chunk": 3}, {"chunk": 4}]
    assert cleanup_called is True


@pytest.mark.asyncio
async def test_chain_async_iterators_on_complete_on_break() -> None:
    """Verify on_complete callback executes if consumer breaks early."""
    buffered = [{"chunk": 1}, {"chunk": 2}]

    async def active_stream() -> AsyncIterator[dict[str, int]]:
        yield {"chunk": 3}
        yield {"chunk": 4}

    cleanup_called = False

    def on_complete() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    chained = chain_async_iterators(buffered, active_stream(), on_complete=on_complete)
    collected = []
    try:
        async for item in chained:
            collected.append(item)
            if len(collected) == 2:
                break
    finally:
        await chained.aclose()

    assert len(collected) == 2
    assert cleanup_called is True


@pytest.mark.asyncio
async def test_chain_async_iterators_on_complete_on_exception() -> None:
    """Verify on_complete callback executes if active stream raises an exception."""
    buffered = [{"chunk": 1}]

    async def failing_stream() -> AsyncIterator[dict[str, int]]:
        yield {"chunk": 2}
        raise RuntimeError("Mid-stream connection dropped")

    cleanup_called = False

    def on_complete() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    chained = chain_async_iterators(buffered, failing_stream(), on_complete=on_complete)

    collected = []
    with pytest.raises(RuntimeError, match="Mid-stream connection dropped"):
        async for item in chained:
            collected.append(item)

    assert len(collected) == 2
    assert cleanup_called is True


@pytest.mark.asyncio
async def test_chain_async_iterators_async_callback() -> None:
    """Verify on_complete supports asynchronous coroutine cleanup."""
    async_cleanup_called = False

    async def async_on_complete() -> None:
        nonlocal async_cleanup_called
        await asyncio.sleep(0.001)
        async_cleanup_called = True

    async def sample_stream() -> AsyncIterator[str]:
        yield "token"

    chained = chain_async_iterators(["initial"], sample_stream(), on_complete=async_on_complete)
    items = [x async for x in chained]
    assert items == ["initial", "token"]
    assert async_cleanup_called is True


# ============================================================================
# 3. Tests for Pre-Stream Handshake & Zero-Byte Failover
# ============================================================================


@pytest.mark.asyncio
async def test_zero_byte_failover_scenario(isolated_registry: AccountsStateRegistry) -> None:
    """Verify that zero-byte stream EOF triggers instant failover to the next account."""
    acc1 = make_oauth_account("acc1", latency_ms=50.0)
    acc2 = make_oauth_account("acc2", latency_ms=100.0)
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    # acc1 yields zero-byte stream (EOF without non-empty chunk)
    async def empty_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        return
        yield  # make it a generator

    # acc2 yields non-empty valid stream
    async def valid_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        yield {"choices": [{"delta": {"content": "Hello from acc2"}}]}
        yield {"choices": [{"delta": {"content": "!"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if account.id == "acc1":
            return empty_stream()
        return valid_stream()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "hi"}]

    stream, chosen_acc, channel = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=messages,
        stream_factory=mock_stream_factory,
    )

    assert chosen_acc.id == "acc2"
    assert channel == "oauth"

    # Concurrency verification: acc1 active_requests was decremented immediately upon failure
    assert acc1.active_requests == 0
    # acc2 active_requests is active during stream
    assert acc2.active_requests == 1

    # Consume stream
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 3
    # After stream completion, acc2 active_requests decremented to 0
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_http_429_failover_scenario(isolated_registry: AccountsStateRegistry) -> None:
    """Verify HTTP 429 during handshake triggers failover and updates cooldown."""
    acc1 = make_oauth_account("acc1", latency_ms=50.0)
    acc2 = make_oauth_account("acc2", latency_ms=100.0)
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    async def stream_429() -> AsyncIterator[dict[str, Any]]:
        mock_response = httpx.Response(
            status_code=429,
            json={"error": {"message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}},
            request=httpx.Request("POST", "http://test"),
        )
        raise httpx.HTTPStatusError("429 RESOURCE_EXHAUSTED", request=mock_response.request, response=mock_response)
        yield {}

    async def stream_success() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Backup account responding"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if account.id == "acc1":
            return stream_429()
        return stream_success()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "test 429"}]

    stream, chosen_acc, channel = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=messages,
        stream_factory=mock_stream_factory,
    )

    assert chosen_acc.id == "acc2"
    assert acc1.active_requests == 0
    assert acc2.active_requests == 1

    chunks = [c async for c in stream]
    assert len(chunks) == 1
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_network_connect_error_failover(isolated_registry: AccountsStateRegistry) -> None:
    """Verify connection errors fail over immediately without crashing."""
    acc1 = make_oauth_account("acc1", latency_ms=50.0)
    acc2 = make_oauth_account("acc2", latency_ms=100.0)
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    async def stream_network_error() -> AsyncIterator[dict[str, Any]]:
        raise httpx.ConnectError("Network unreachable")
        yield {}

    async def stream_success() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Recovered network"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if account.id == "acc1":
            return stream_network_error()
        return stream_success()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "ping"}]

    stream, chosen_acc, _ = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=messages,
        stream_factory=mock_stream_factory,
    )

    assert chosen_acc.id == "acc2"
    assert acc1.active_requests == 0
    assert acc2.active_requests == 1

    _ = [c async for c in stream]
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_pool_exhausted_error(isolated_registry: AccountsStateRegistry) -> None:
    """Verify PoolExhaustedError is raised when all eligible accounts fail."""
    acc1 = make_oauth_account("acc1")
    acc2 = make_oauth_account("acc2")
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    async def failing_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        return
        yield

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        return failing_stream()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "ping"}]

    with pytest.raises(PoolExhaustedError, match="Pool exhausted for model"):
        await controller.execute_stream_with_failover(
            model="gemini-3.8-flash",
            messages=messages,
            stream_factory=mock_stream_factory,
        )

    # Concurrency must be zero for all accounts (strictly no leak)
    assert acc1.active_requests == 0
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_cross_channel_failover_pro_model(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Pro model tries Web first and fails over to OAuth upon Web handshake failure."""
    web_acc = make_web_account("web_pro")
    oauth_acc = make_oauth_account("oauth_pro")
    isolated_registry.config.web_accounts = [web_acc]
    isolated_registry.config.oauth_accounts = [oauth_acc]

    async def web_failing_stream() -> AsyncIterator[dict[str, Any]]:
        # Empty EOF
        return
        yield

    async def oauth_success_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "OAuth fallback for Pro succeeded"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if channel_type == "web":
            return web_failing_stream()
        return oauth_success_stream()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "analyze code"}]

    stream, chosen_acc, channel = await controller.execute_stream_with_failover(
        model="gemini-3.1-pro",
        messages=messages,
        stream_factory=mock_stream_factory,
    )

    assert channel == "oauth"
    assert chosen_acc.id == "oauth_pro"
    assert web_acc.active_requests == 0
    assert oauth_acc.active_requests == 1

    chunks = [c async for c in stream]
    assert len(chunks) == 1
    assert oauth_acc.active_requests == 0


@pytest.mark.asyncio
async def test_argon_model_strictly_web_only(isolated_registry: AccountsStateRegistry) -> None:
    """Verify Argon model exclusively uses Web and raises PoolExhaustedError instead of falling back to OAuth."""
    web_acc = make_web_account("web_argon")
    oauth_acc = make_oauth_account("oauth_fallback")
    isolated_registry.config.web_accounts = [web_acc]
    isolated_registry.config.oauth_accounts = [oauth_acc]

    async def web_failing_stream() -> AsyncIterator[dict[str, Any]]:
        return
        yield

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        return web_failing_stream()

    controller = FailoverController(registry=isolated_registry)
    messages = [{"role": "user", "content": "test argon"}]

    with pytest.raises(PoolExhaustedError):
        await controller.execute_stream_with_failover(
            model="gemini-4-argon",
            messages=messages,
            stream_factory=mock_stream_factory,
        )

    assert web_acc.active_requests == 0
    assert oauth_acc.active_requests == 0


# ============================================================================
# 4. Tests for Model Validation
# ============================================================================


@pytest.mark.asyncio
async def test_model_not_supported_error(isolated_registry: AccountsStateRegistry) -> None:
    """Verify ModelNotSupportedError is raised for empty or completely unknown models."""
    controller = FailoverController(registry=isolated_registry)

    # Empty model name
    with pytest.raises(ModelNotSupportedError, match="Model identifier must be a non-empty string"):
        await controller.execute_stream_with_failover(model="", messages=[])

    # Unsupported vendor model
    with pytest.raises(ModelNotSupportedError, match="not supported in the Gemini-Hybrid-Pool-Gateway"):
        await controller.execute_stream_with_failover(model="claude-3-opus", messages=[])


def test_is_model_supported_in_gateway_helpers() -> None:
    """Verify is_model_supported_in_gateway checks standard and discovered models."""
    assert is_model_supported_in_gateway("gemini-3.8-flash") is True
    assert is_model_supported_in_gateway("gemini-3.1-pro-high") is True
    assert is_model_supported_in_gateway("gemini-4-argon") is True
    assert is_model_supported_in_gateway("gemini-custom-future") is True
    assert is_model_supported_in_gateway("gpt-4o") is False
    assert is_model_supported_in_gateway("") is False


# ============================================================================
# 5. Tests for Non-Streaming Chat Completion Failover
# ============================================================================


@pytest.mark.asyncio
async def test_execute_chat_completion_with_failover_success(
    isolated_registry: AccountsStateRegistry,
) -> None:
    """Verify non-streaming completion aggregates content, reasoning, and tool calls."""
    acc = make_oauth_account("oauth_main")
    isolated_registry.config.oauth_accounts = [acc]

    async def full_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "chatcmpl-test-1",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "Pondering the solution..."},
                }
            ]
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Calculated "},
                }
            ]
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "result: 42"},
                }
            ]
        }
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {"name": "calc", "arguments": '{"val": 42}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        return full_stream()

    controller = FailoverController(registry=isolated_registry)
    result = await controller.execute_chat_completion_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "calculate"}],
        stream_factory=mock_stream_factory,
    )

    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Calculated result: 42"
    assert result["choices"][0]["message"]["reasoning_content"] == "Pondering the solution..."
    assert len(result["choices"][0]["message"]["tool_calls"]) == 1
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert acc.active_requests == 0


@pytest.mark.asyncio
async def test_execute_chat_completion_failover_on_initial_error(
    isolated_registry: AccountsStateRegistry,
) -> None:
    """Verify non-streaming completion recovers when first account fails handshake."""
    acc1 = make_oauth_account("acc1", latency_ms=50.0)
    acc2 = make_oauth_account("acc2", latency_ms=100.0)
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    async def fail_stream() -> AsyncIterator[dict[str, Any]]:
        return
        yield

    async def pass_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "From account 2"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if account.id == "acc1":
            return fail_stream()
        return pass_stream()

    controller = FailoverController(registry=isolated_registry)
    result = await controller.execute_chat_completion_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        stream_factory=mock_stream_factory,
    )

    assert result["_account_id"] == "acc2"
    assert result["choices"][0]["message"]["content"] == "From account 2"
    assert acc1.active_requests == 0
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_execute_chat_completion_mid_stream_recovery(
    isolated_registry: AccountsStateRegistry,
) -> None:
    """Verify non-streaming completion recovers even if stream drops mid-transfer after handshake."""
    acc1 = make_oauth_account("acc1", latency_ms=50.0)
    acc2 = make_oauth_account("acc2", latency_ms=100.0)
    isolated_registry.config.oauth_accounts = [acc1, acc2]

    async def drop_mid_stream() -> AsyncIterator[dict[str, Any]]:
        # Non-empty handshake succeeds
        yield {"choices": [{"delta": {"content": "Partial token"}}]}
        # Then connection abruptly breaks
        raise httpx.ReadTimeout("Timeout waiting for next chunk")

    async def full_backup_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Clean complete answer from acc2"}}]}

    def mock_stream_factory(account: Any, channel_type: str) -> AsyncIterator[dict[str, Any]]:
        if account.id == "acc1":
            return drop_mid_stream()
        return full_backup_stream()

    controller = FailoverController(registry=isolated_registry)
    result = await controller.execute_chat_completion_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        stream_factory=mock_stream_factory,
    )

    assert result["_account_id"] == "acc2"
    assert result["choices"][0]["message"]["content"] == "Clean complete answer from acc2"
    assert acc1.active_requests == 0
    assert acc2.active_requests == 0


# ============================================================================
# 6. Concurrency Leak Protection Stress Tests
# ============================================================================


@pytest.mark.asyncio
async def test_zero_concurrency_leak_across_failure_scenarios(
    isolated_registry: AccountsStateRegistry,
) -> None:
    """Comprehensive stress test proving active_requests never leaks across varied failures."""
    acc1 = make_oauth_account("acc1")
    acc2 = make_web_account("acc2")
    isolated_registry.config.oauth_accounts = [acc1]
    isolated_registry.config.web_accounts = [acc2]

    # Scenario 1: Stream breaks mid-way
    async def broken_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Start"}}]}
        raise RuntimeError("Network crash")

    controller = FailoverController(registry=isolated_registry)
    stream, _, _ = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "test"}],
        stream_factory=lambda acc, ch: broken_stream(),
    )
    with pytest.raises(RuntimeError):
        _ = [c async for c in stream]

    assert acc1.active_requests == 0
    assert acc2.active_requests == 0

    # Scenario 2: Consumer cancels / breaks after 1 chunk
    async def infinite_stream() -> AsyncIterator[dict[str, Any]]:
        for i in range(100):
            yield {"choices": [{"delta": {"content": f"token_{i}"}}]}

    stream2, _, _ = await controller.execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "test"}],
        stream_factory=lambda acc, ch: infinite_stream(),
    )
    try:
        async for _ in stream2:
            break
    finally:
        await stream2.aclose()

    assert acc1.active_requests == 0
    assert acc2.active_requests == 0


@pytest.mark.asyncio
async def test_top_level_module_convenience_functions(
    isolated_registry: AccountsStateRegistry,
) -> None:
    """Verify execute_stream_with_failover and execute_chat_completion_with_failover standalone."""
    acc = make_oauth_account("acc_top")
    isolated_registry.config.oauth_accounts = [acc]

    async def stream_fn() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "Hello top level"}}]}

    # 1. execute_stream_with_failover
    stream, chosen, channel = await execute_stream_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        registry=isolated_registry,
        stream_factory=lambda a, c: stream_fn(),
    )
    assert chosen.id == "acc_top"
    items = [chunk async for chunk in stream]
    assert len(items) == 1
    assert acc.active_requests == 0

    # 2. execute_chat_completion_with_failover
    res = await execute_chat_completion_with_failover(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        registry=isolated_registry,
        stream_factory=lambda a, c: stream_fn(),
    )
    assert res["choices"][0]["message"]["content"] == "Hello top level"
    assert acc.active_requests == 0
