"""Comprehensive tests for OpenAI protocol routes (Level 5).

Verifies:
1. GET /v1/models:
   - Dynamic catalog listing adhering to standard OpenAI {"object": "list", "data": [...]}.
   - Contains discovered models and reasoning effort variants.
2. GET /v1/models/{model_id}:
   - Successful retrieval for registered models.
   - Standard 404 OpenAI error payload for non-existent models.
3. POST /v1/chat/completions non-streaming:
   - Returns complete chat.completion JSON object with choices, message, and usage.
4. POST /v1/chat/completions streaming:
   - Returns text/event-stream with no-cache, keep-alive, and X-Accel-Buffering headers.
   - Emits valid Server-Sent Events (SSE) data lines.
   - Concludes with the mandatory 'data: [DONE]\\n\\n' sentinel event.
5. X-Token-Saver header evaluation:
   - Enabled by default: compresses tool outputs in request messages.
   - Disabled via 'X-Token-Saver: off': leaves tool outputs uncompressed verbatim.
6. Standard OpenAI error handling:
   - ModelNotSupportedError -> HTTP 404 (invalid_request_error, code: model_not_found).
   - PoolExhaustedError -> HTTP 429 (server_error, code: pool_exhausted).
   - Unexpected exceptions -> HTTP 500 (server_error, code: internal_server_error).
7. Permissive schema validation:
   - Accepts unknown OpenAI/client fields (extra="allow") without throwing 422 errors.
   - Translates explicit reasoning_effort into suffixed model identifier.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
import json
from typing import Any

from fastapi import FastAPI
import httpx
import pytest

from api.openai_routes import (
    get_failover_controller,
    get_model_registry,
    router as openai_router,
)
from config.settings import OAuthAccount, OAuthCredentials, state_registry
from core.failover import (
    FailoverController,
    ModelNotSupportedError,
    PoolExhaustedError,
)
from core.model_registry import UnifiedModelRegistry


# ============================================================================
# Test Doubles & Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clean_state() -> Any:
    """Ensure clean registry state between tests."""
    state_registry.reset()
    yield
    state_registry.reset()


class StubFailoverController:
    """Configurable test double for FailoverController."""

    def __init__(self) -> None:
        self.stream_chunks: list[dict[str, Any]] = [
            {
                "id": "chatcmpl-test-stream",
                "object": "chat.completion.chunk",
                "created": 1735689600,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-test-stream",
                "object": "chat.completion.chunk",
                "created": 1735689600,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world!"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        self.completion_result: dict[str, Any] = {
            "id": "chatcmpl-test-non-stream",
            "object": "chat.completion",
            "created": 1735689600,
            "model": "gemini-2.5-pro",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello from non-streaming gateway!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "total_tokens": 18,
            },
            "_account_id": "oauth-test-account",
            "_channel": "oauth",
        }
        self.last_stream_call: dict[str, Any] = {}
        self.last_completion_call: dict[str, Any] = {}
        self.error_to_raise: Exception | None = None

    async def execute_stream_with_failover(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> tuple[AsyncIterator[Any], OAuthAccount, str]:
        if self.error_to_raise:
            raise self.error_to_raise

        self.last_stream_call = {
            "model": model,
            "messages": list(messages),
            "tools": tools,
            "kwargs": kwargs,
        }

        async def _generator() -> AsyncIterator[dict[str, Any]]:
            for chunk in self.stream_chunks:
                yield chunk

        real_account = OAuthAccount(
            id="oauth-test-account",
            credentials=OAuthCredentials(access_token="valid-token"),
            status="ACTIVE",
        )
        return _generator(), real_account, "oauth"

    async def execute_chat_completion_with_failover(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.error_to_raise:
            raise self.error_to_raise

        self.last_completion_call = {
            "model": model,
            "messages": list(messages),
            "tools": tools,
            "kwargs": kwargs,
        }
        return self.completion_result


@pytest.fixture
def stub_controller() -> StubFailoverController:
    """Fixture providing a configurable failover controller stub."""
    return StubFailoverController()


@pytest.fixture
def app(stub_controller: StubFailoverController) -> FastAPI:
    """FastAPI application configured with OpenAI router and dependency overrides."""
    application = FastAPI(title="OpenAI Routes Test App")
    application.include_router(openai_router)
    application.dependency_overrides[get_failover_controller] = lambda: stub_controller
    return application


@pytest.fixture
def client_factory(app: FastAPI) -> Callable[[], httpx.AsyncClient]:
    """Sync fixture yielding an AsyncClient factory connected to ASGI app."""
    def _create_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )
    return _create_client


# ============================================================================
# 1. Models Endpoints Tests
# ============================================================================


@pytest.mark.asyncio
async def test_list_models_empty_pool_returns_empty_list(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """GET /v1/models must return empty list when no accounts are registered."""
    async with client_factory() as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"] == []


@pytest.mark.asyncio
async def test_list_models_returns_openai_compliant_catalog(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """GET /v1/models must return a list object containing discovered model entries."""
    test_acc = OAuthAccount(
        id="test-discovered-account",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok"),
        discovered_models=["gemini-2.5-pro"],
    )
    state_registry.upsert_oauth_account(test_acc)

    async with client_factory() as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200

    payload = response.json()
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) > 0

    model_ids = [item["id"] for item in payload["data"]]
    assert "gemini-2.5-pro" in model_ids
    assert "gemini-2.5-pro-high" in model_ids

    first_item = payload["data"][0]
    assert first_item["object"] == "model"
    assert "id" in first_item
    assert "created" in first_item
    assert "owned_by" in first_item


@pytest.mark.asyncio
async def test_get_model_existing_returns_metadata(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """GET /v1/models/{model_id} must return metadata for a known model."""
    test_acc = OAuthAccount(
        id="test-discovered-account",
        status="ACTIVE",
        credentials=OAuthCredentials(access_token="tok"),
        discovered_models=["gemini-2.5-pro"],
    )
    state_registry.upsert_oauth_account(test_acc)

    async with client_factory() as client:
        response = await client.get("/v1/models/gemini-2.5-pro")
    assert response.status_code == 200

    payload = response.json()
    assert payload["id"] == "gemini-2.5-pro"
    assert payload["object"] == "model"
    assert payload["owned_by"] == "google-hybrid-pool"


@pytest.mark.asyncio
async def test_get_model_not_found_returns_404_openai_error(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """GET /v1/models/{model_id} must return HTTP 404 in standard OpenAI error format."""
    async with client_factory() as client:
        response = await client.get("/v1/models/unsupported-dummy-model")
    assert response.status_code == 404

    payload = response.json()
    assert "error" in payload
    error = payload["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"
    assert "unsupported-dummy-model" in error["message"]


# ============================================================================
# 2. Non-Streaming Chat Completion Tests
# ============================================================================


@pytest.mark.asyncio
async def test_chat_completion_non_streaming_success(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """POST /v1/chat/completions (stream=False) returns complete OpenAI completion payload."""
    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [
            {"role": "user", "content": "Hello, gateway!"},
        ],
        "stream": False,
        "temperature": 0.7,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 200

    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["id"] == "chatcmpl-test-non-stream"
    assert len(payload["choices"]) == 1
    assert payload["choices"][0]["message"]["content"] == "Hello from non-streaming gateway!"
    assert payload["choices"][0]["finish_reason"] == "stop"

    assert stub_controller.last_completion_call["model"] == "gemini-2.5-pro"
    assert len(stub_controller.last_completion_call["messages"]) == 1


# ============================================================================
# 3. Streaming Chat Completion Tests (SSE & data: [DONE])
# ============================================================================


@pytest.mark.asyncio
async def test_chat_completion_streaming_sse_success_with_done_sentinel(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """POST /v1/chat/completions (stream=True) streams SSE chunks and ends with data: [DONE]."""
    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [
            {"role": "user", "content": "Stream me a message."},
        ],
        "stream": True,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("connection") == "keep-alive"
    assert response.headers.get("x-accel-buffering") == "no"

    raw_text = response.text
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    # Collect data lines
    data_lines = [line for line in lines if line.startswith("data: ")]
    assert len(data_lines) >= 3

    # Last data line must be the standard OpenAI EOF sentinel
    assert data_lines[-1] == "data: [DONE]"

    # Verify preceding lines parse as valid JSON chunks
    content_parts: list[str] = []
    for line in data_lines[:-1]:
        chunk_json = json.loads(line[6:])
        assert chunk_json["object"] == "chat.completion.chunk"
        assert "choices" in chunk_json
        delta = chunk_json["choices"][0]["delta"]
        if "content" in delta:
            content_parts.append(delta["content"])

    assert "".join(content_parts) == "Hello world!"


# ============================================================================
# 4. Token Saver Header (X-Token-Saver) Tests
# ============================================================================


@pytest.mark.asyncio
async def test_chat_completion_token_saver_active_by_default(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """Tool outputs are compressed by default when X-Token-Saver is omitted."""
    repeated_content = "identical error log line\n" * 10
    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [
            {"role": "user", "content": "Check log"},
            {"role": "tool", "content": repeated_content, "tool_call_id": "call_1"},
        ],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 200

    received_messages = stub_controller.last_completion_call["messages"]
    tool_msg = received_messages[1]
    # Default compression collapses repeated lines
    assert "[... repeated" in tool_msg["content"]


@pytest.mark.asyncio
async def test_chat_completion_token_saver_disabled_via_header(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """When X-Token-Saver: off is provided, tool outputs are kept uncompressed."""
    repeated_content = "identical error log line\n" * 10
    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [
            {"role": "user", "content": "Check log"},
            {"role": "tool", "content": repeated_content, "tool_call_id": "call_1"},
        ],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers={"X-Token-Saver": "off"},
        )
    assert response.status_code == 200

    received_messages = stub_controller.last_completion_call["messages"]
    tool_msg = received_messages[1]
    # Uncompressed: exact repeat count retained without collapse tag
    assert "[... repeated" not in tool_msg["content"]
    assert tool_msg["content"].count("identical error log line") == 10


# ============================================================================
# 5. Standard Error Handling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_chat_completion_model_not_supported_returns_404(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """ModelNotSupportedError raises standard 404 invalid_request_error with model_not_found code."""
    stub_controller.error_to_raise = ModelNotSupportedError("Requested model 'gpt-fake' is unsupported.")

    request_body = {
        "model": "gpt-fake",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 404

    payload = response.json()
    assert "error" in payload
    error = payload["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"
    assert "gpt-fake" in error["message"]


@pytest.mark.asyncio
async def test_chat_completion_pool_exhausted_returns_429(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """PoolExhaustedError raises standard 429 server_error with pool_exhausted code."""
    stub_controller.error_to_raise = PoolExhaustedError("All pool accounts have exceeded daily/hourly limits.")

    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 429

    payload = response.json()
    assert "error" in payload
    error = payload["error"]
    assert error["type"] == "server_error"
    assert error["code"] == "pool_exhausted"
    assert "limits" in error["message"]


@pytest.mark.asyncio
async def test_chat_completion_unexpected_exception_returns_500(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """Unexpected exception in gateway raises standard 500 server_error."""
    stub_controller.error_to_raise = RuntimeError("Unexpected gateway internal fault.")

    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 500

    payload = response.json()
    assert "error" in payload
    error = payload["error"]
    assert error["type"] == "server_error"
    assert error["code"] == "internal_server_error"
    assert "Unexpected gateway internal fault" in error["message"]


# ============================================================================
# 6. Permissive Validation & Reasoning Effort Resolution Tests
# ============================================================================


@pytest.mark.asyncio
async def test_chat_completion_permissive_schema_allows_extra_fields(
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """ChatCompletionRequest must accept extra OpenAI fields without Pydantic validation errors."""
    request_body = {
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": False,
        "custom_user_id": "usr_998877",
        "parallel_tool_calls": True,
        "service_tier": "auto",
        "metadata": {"session_id": "sess_123"},
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"


@pytest.mark.asyncio
async def test_chat_completion_reasoning_effort_suffix_translation(
    client_factory: Callable[[], httpx.AsyncClient],
    stub_controller: StubFailoverController,
) -> None:
    """Explicit reasoning_effort parameter is translated into canonical model suffix."""
    request_body = {
        "model": "gemini-2.5-pro",
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "Solve hard problem"}],
        "stream": False,
    }

    async with client_factory() as client:
        response = await client.post("/v1/chat/completions", json=request_body)
    assert response.status_code == 200

    assert stub_controller.last_completion_call["model"] == "gemini-2.5-pro-high"
