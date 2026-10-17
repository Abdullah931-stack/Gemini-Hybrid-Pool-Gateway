"""OpenAI-compatible protocol routes and Chat Completions endpoint.

Implements Level 5 (Public Protocol Plane) of the Gemini-Hybrid-Pool-Gateway:
1. Standard OpenAI Models API:
   - GET /v1/models: Dynamic aggregation of all discovered base models and reasoning effort variants.
   - GET /v1/models/{model_id}: Single model retrieval with standard OpenAI 404 error responses.
2. Unified Chat Completions API:
   - POST /v1/chat/completions: Supports both streaming (Server-Sent Events) and non-streaming modes.
   - RTK-Lite Token Saver: Lossless tool-output compression, controllable via the X-Token-Saver header.
   - Zero-Drop Failover: Non-empty pre-stream handshake ensuring complete resilience before client emission.
   - Standard Error Handling: Structured OpenAI error payloads for ModelNotSupportedError (404),
     PoolExhaustedError (429), and internal server errors (500).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
import json
import logging
from typing import Any, Literal
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.failover import (
    FailoverController,
    ModelNotSupportedError,
    PoolExhaustedError,
    default_failover_controller,
    execute_chat_completion_with_failover,
    execute_stream_with_failover,
)
from core.model_registry import (
    UnifiedModelRegistry,
    split_model_effort_suffix,
    unified_model_registry,
)
from core.token_saver import (
    sanitize_tool_messages,
    should_enable_token_saver,
)
from core.web_channel import StreamChunk, format_openai_sse_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI"])

# ============================================================================
# 1. Dependency Inversion Providers
# ============================================================================


def get_model_registry() -> UnifiedModelRegistry:
    """Provide the active UnifiedModelRegistry instance."""
    return unified_model_registry


def get_failover_controller() -> FailoverController:
    """Provide the active FailoverController instance."""
    return default_failover_controller


# ============================================================================
# 2. Pydantic v2 Request & Message Schemas
# ============================================================================


class ChatMessage(BaseModel):
    """OpenAI-compatible message schema with flexible extra attributes."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI standard chat completion request payload."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str
    messages: list[ChatMessage | dict[str, Any]]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    response_format: dict[str, Any] | None = None


# ============================================================================
# 3. Helpers & Error Formatters
# ============================================================================


def make_openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Build an OpenAI-compliant structured JSON error response."""
    payload: dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }
    return JSONResponse(status_code=status_code, content=payload)


def normalize_message_to_dict(message: ChatMessage | Mapping[str, Any]) -> dict[str, Any]:
    """Convert ChatMessage or Mapping into a clean dictionary."""
    if isinstance(message, BaseModel):
        return message.model_dump(exclude_none=True)
    return dict(message)


def resolve_effective_model(model_name: str, reasoning_effort: str | None) -> str:
    """Translate explicit reasoning effort into a canonical model identifier if needed."""
    if not model_name or not isinstance(model_name, str):
        return model_name
    base_model, existing_suffix = split_model_effort_suffix(model_name)
    if existing_suffix is not None or not reasoning_effort:
        return model_name

    normalized_effort = reasoning_effort.strip().lower()
    if normalized_effort in ("minimal", "low", "medium", "high"):
        return f"{base_model}-{normalized_effort}"
    return model_name


def format_sse_chunk(chunk: Any) -> str:
    """Format chunk into an SSE line data: <json>\\n\\n."""
    if isinstance(chunk, str):
        if chunk.startswith("data: "):
            return chunk if chunk.endswith("\n\n") else f"{chunk}\n\n"
        return f"data: {chunk}\n\n"
    return format_openai_sse_event(chunk)


async def sse_event_stream(stream_iter: AsyncIterator[Any]) -> AsyncIterator[str]:
    """Wrap async generator into Server-Sent Events, concluding with data: [DONE]."""
    try:
        async for chunk in stream_iter:
            yield format_sse_chunk(chunk)
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Error during streaming generation: %s", exc, exc_info=True)
        error_payload = {
            "error": {
                "message": str(exc),
                "type": "server_error",
                "param": None,
                "code": "stream_error",
            }
        }
        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


# ============================================================================
# 4. Route Handlers
# ============================================================================


@router.get("/models")
async def list_models(
    registry: UnifiedModelRegistry = Depends(get_model_registry),
) -> dict[str, Any]:
    """List all available models in standard OpenAI format."""
    return registry.list_openai_models()


@router.get("/models/{model_id}")
async def get_model(
    model_id: str,
    registry: UnifiedModelRegistry = Depends(get_model_registry),
) -> Response:
    """Retrieve metadata for a specific model or return 404 in OpenAI format."""
    models_list = registry.list_openai_models().get("data", [])
    for entry in models_list:
        if entry.get("id") == model_id:
            return JSONResponse(content=entry)

    return make_openai_error_response(
        status_code=status.HTTP_404_NOT_FOUND,
        message=f"The model '{model_id}' does not exist or you do not have access to it.",
        error_type="invalid_request_error",
        param="model",
        code="model_not_found",
    )


@router.post("/chat/completions")
async def create_chat_completion(
    request: Request,
    body: ChatCompletionRequest,
    controller: FailoverController = Depends(get_failover_controller),
) -> Response:
    """Create a chat completion turn with failover and optional token compression."""
    token_saver_enabled = should_enable_token_saver(request.headers)
    raw_messages = [normalize_message_to_dict(m) for m in body.messages]
    sanitized_messages = sanitize_tool_messages(
        raw_messages,
        enabled=token_saver_enabled,
        headers=request.headers,
    )

    effective_model = resolve_effective_model(body.model, body.reasoning_effort)

    try:
        if body.stream:
            stream_iter, _account, _channel = await controller.execute_stream_with_failover(
                model=effective_model,
                messages=sanitized_messages,
                tools=body.tools,
            )
            sse_headers = {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
            return StreamingResponse(
                sse_event_stream(stream_iter),
                media_type="text/event-stream",
                headers=sse_headers,
            )

        completion_result = await controller.execute_chat_completion_with_failover(
            model=effective_model,
            messages=sanitized_messages,
            tools=body.tools,
        )
        return JSONResponse(content=completion_result)

    except ModelNotSupportedError as exc:
        logger.warning("Requested model not supported: %s", exc)
        return make_openai_error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            message=str(exc),
            error_type="invalid_request_error",
            param="model",
            code="model_not_found",
        )
    except PoolExhaustedError as exc:
        logger.error("Account pool exhausted: %s", exc)
        return make_openai_error_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message=str(exc),
            error_type="server_error",
            param=None,
            code="pool_exhausted",
        )
    except Exception as exc:
        logger.exception("Unexpected error processing chat completion: %s", exc)
        return make_openai_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=f"Internal server error: {exc}",
            error_type="server_error",
            param=None,
            code="internal_server_error",
        )


__all__ = [
    "ChatCompletionRequest",
    "ChatMessage",
    "format_sse_chunk",
    "get_failover_controller",
    "get_model_registry",
    "make_openai_error_response",
    "normalize_message_to_dict",
    "resolve_effective_model",
    "router",
    "sse_event_stream",
]
