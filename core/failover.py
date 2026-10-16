"""Non-empty pre-stream handshake and zero-drop failover engine (ADR-10).

Implements Level 4 / Phase 5 of the Gemini-Hybrid-Pool-Gateway:
1. Non-Empty Chunk Detection (ADR-10):
   Inspects streaming chunks (dict or Pydantic StreamChunk) for genuine
   semantic content: choices[0].delta carrying non-empty content, reasoning_content,
   or tool_calls, safely ignoring empty / role-only packets.
2. Chained Async Generator with Cleanup Callback:
   ``chain_async_iterators`` yields buffered handshake chunks first, then streams
   remaining chunks, guaranteeing execution of ``on_complete`` (decrementing active_requests
   and recording timestamps) in a ``finally:`` block across normal completion,
   exceptions, or client disconnections.
3. Pre-Stream Handshake & Instant Failover:
   Consumes initial stream chunks up to ``HANDSHAKE_MAX_BUFFER_CHUNKS = 15``.
   If zero-byte EOF or an error (429, 403, Network error, Timeout) occurs before
   any non-empty chunk is received:
   - Decrements active_requests immediately.
   - Enforces cooldown / records 429 classification via UsageMonitor.
   - Excludes the failed account (``excluded_ids``) and seamlessly fails over
     to the next optimal account before delivering any response to the client.
4. Pool Exhaustion & Model Validation:
   Raises ``PoolExhaustedError`` when all eligible accounts in the pool are exhausted.
   Raises ``ModelNotSupportedError`` for unsupported model requests.
5. Non-Streaming Support:
   ``execute_chat_completion_with_failover`` provides complete failover resilience
   for standard non-streaming requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
import inspect
import logging
import time
from typing import Any, Final, Literal
import uuid

import httpx

from config.settings import (
    AccountsStateRegistry,
    LMDBStore,
    OAuthAccount,
    WebAccount,
    get_lmdb_store,
    state_registry,
)
from core.hybrid_scheduler import (
    NoEligibleAccountError,
    apply_smart_pacing,
    select_optimal_account,
)
from core.model_registry import (
    DEFAULT_STANDARD_MODELS,
    UnifiedModelRegistry,
    split_model_effort_suffix,
    unified_model_registry,
)
from core.oauth_cli_channel import OAuthCLIChannel
from core.usage_monitor import (
    UsageMonitor,
    classify_and_handle_oauth_429,
    handle_account_suspension,
    is_suspension_error,
)
from core.web_channel import WebChannel

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Engineering Constants & Exceptions
# ============================================================================

HANDSHAKE_MAX_BUFFER_CHUNKS: Final[int] = 15


class PoolExhaustedError(Exception):
    """Raised when all eligible accounts in the pool have been exhausted."""


class HandshakeFailedError(Exception):
    """Raised when pre-stream handshake fails to receive non-empty content on an account."""


class ModelNotSupportedError(Exception):
    """Raised when the requested model is not supported by any account or channel."""


# ============================================================================
# 2. Content Inspection & Helper Functions
# ============================================================================


def _get_field(obj: Any, name: str) -> Any:
    """Safely extract field or attribute from dict or object."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def is_chunk_non_empty(chunk: Any) -> bool:
    """Determine whether a streaming chunk carries real semantic content (ADR-10).

    A chunk is considered non-empty if ``choices[0].delta`` contains:
    - Non-empty ``content`` (str with len > 0).
    - Non-empty ``reasoning_content`` (str with len > 0).
    - Non-empty ``tool_calls`` (non-empty collection).

    Safely returns False for role announcements, metadata-only chunks,
    empty strings, None, or malformed objects without raising exceptions.
    """
    if chunk is None:
        return False

    try:
        choices = _get_field(chunk, "choices")
        if not choices or not isinstance(choices, (list, tuple)):
            return False

        first_choice = choices[0]
        delta = _get_field(first_choice, "delta")
        if delta is None:
            return False

        # 1. Content check
        content = _get_field(delta, "content")
        if isinstance(content, str) and len(content) > 0:
            return True

        # 2. Reasoning / thoughts content check
        reasoning = _get_field(delta, "reasoning_content")
        if isinstance(reasoning, str) and len(reasoning) > 0:
            return True

        # 3. Tool calls check
        tool_calls = _get_field(delta, "tool_calls")
        if tool_calls and isinstance(tool_calls, (list, tuple)) and len(tool_calls) > 0:
            return True

        return False
    except Exception:
        return False


def is_model_supported_in_gateway(
    model: str,
    registry: AccountsStateRegistry | None = None,
) -> bool:
    """Check if ``model`` is recognized and supported within the gateway.

    Supported models include:
    - Known standard models in DEFAULT_STANDARD_MODELS.
    - Discovered models across active accounts in the registry.
    - Standard 'gemini-' prefixed models.
    """
    if not model or not isinstance(model, str) or not model.strip():
        return False

    base_model, _ = split_model_effort_suffix(model.strip())
    norm = base_model.lower()

    # 1. Standard default models
    for known in DEFAULT_STANDARD_MODELS:
        if norm == known.lower():
            return True

    # 2. Registry discovered models
    target_registry = registry or state_registry
    all_accounts: list[OAuthAccount | WebAccount] = list(
        target_registry.oauth_accounts
    ) + list(target_registry.web_accounts)
    for acc in all_accounts:
        for disc in acc.discovered_models:
            d_base, _ = split_model_effort_suffix(disc)
            if d_base.lower() == norm:
                return True

    # 3. Canonical Gemini namespace check
    if norm.startswith("gemini-"):
        return True

    return False


# ============================================================================
# 3. Chained Stream Generator with Guaranteed Cleanup
# ============================================================================


async def chain_async_iterators(
    buffered_chunks: Sequence[Any],
    active_stream: AsyncIterator[Any],
    on_complete: Callable[[], Any] | None = None,
) -> AsyncIterator[Any]:
    """Yield buffered chunks first, then forward active_stream with guaranteed cleanup.

    Guarantees execution of ``on_complete`` in a ``finally:`` block regardless of
    whether the stream completes normally, breaks early, is cancelled, or raises.
    """
    callback_executed = False

    async def _safe_cleanup() -> None:
        nonlocal callback_executed
        if not callback_executed and on_complete is not None:
            callback_executed = True
            try:
                res = on_complete()
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error("Error in chain_async_iterators on_complete callback: %s", e)

    try:
        for chunk in buffered_chunks:
            yield chunk
        async for chunk in active_stream:
            yield chunk
    finally:
        await _safe_cleanup()


# ============================================================================
# 4. Failover Controller & Handshake Engine
# ============================================================================


class FailoverController:
    """Pre-stream handshake and instant zero-drop failover controller (ADR-10)."""

    def __init__(
        self,
        registry: AccountsStateRegistry | None = None,
        lmdb_store: LMDBStore | None = None,
        usage_monitor: UsageMonitor | None = None,
        oauth_channel: OAuthCLIChannel | None = None,
        web_channel: WebChannel | None = None,
        max_buffer_chunks: int = HANDSHAKE_MAX_BUFFER_CHUNKS,
    ) -> None:
        self.registry: AccountsStateRegistry = registry or state_registry
        self._lmdb_store: LMDBStore | None = lmdb_store
        self.usage_monitor: UsageMonitor = usage_monitor or UsageMonitor(self.registry)
        self._oauth_channel: OAuthCLIChannel | None = oauth_channel
        self._web_channel: WebChannel | None = web_channel
        self.max_buffer_chunks: int = max_buffer_chunks

    @property
    def lmdb_store(self) -> LMDBStore:
        """Resolve active LMDBStore instance."""
        if self._lmdb_store is None:
            self._lmdb_store = get_lmdb_store()
        return self._lmdb_store

    @property
    def oauth_channel(self) -> OAuthCLIChannel:
        """Resolve active OAuthCLIChannel instance."""
        if self._oauth_channel is None:
            self._oauth_channel = OAuthCLIChannel(lmdb_store=self.lmdb_store)
        return self._oauth_channel

    @property
    def web_channel(self) -> WebChannel:
        """Resolve active WebChannel instance."""
        if self._web_channel is None:
            self._web_channel = WebChannel(lmdb_store=self.lmdb_store)
        return self._web_channel

    async def _handle_handshake_failure(
        self,
        account: OAuthAccount | WebAccount,
        channel_type: Literal["oauth", "web"],
        error: Exception | Any,
        *,
        now: float | None = None,
    ) -> None:
        """Perform immediate concurrency decrement and classification upon handshake failure."""
        now_ts = time.time() if now is None else float(now)

        # 1. Immediately decrement active_requests (zero concurrency leak)
        account.active_requests = max(0, account.active_requests - 1)

        # 2. Update completion timestamps
        if hasattr(account, "last_completed_timestamp"):
            account.last_completed_timestamp = now_ts
        if hasattr(account, "last_request_end_time"):
            account.last_request_end_time = now_ts

        err_str = str(error)
        status_code = getattr(error, "status_code", None)
        if status_code is None and hasattr(error, "response") and error.response is not None:
            status_code = getattr(error.response, "status_code", None)

        is_429 = (
            status_code == 429
            or "429" in err_str
            or "RESOURCE_EXHAUSTED" in err_str
        )

        if is_429:
            if isinstance(account, OAuthAccount):
                try:
                    await self.usage_monitor.classify_429(account, error, now=now_ts)
                except Exception as e:
                    logger.error("Failed to classify 429 for account %s: %s", account.id, e)
                    account.status = "COOLDOWN"
                    account.quota_state.cooldown_until = int(now_ts + 60.0)
            elif isinstance(account, WebAccount):
                account.status = "COOLDOWN"
                account.cooldown_until = int(now_ts + 60.0)
                account.consecutive_errors += 1
                account.last_error_reason = "HTTP 429 rate limit exceeded"
        elif is_suspension_error(error):
            try:
                await handle_account_suspension(account, err_str)
            except Exception as e:
                logger.error("Failed to suspend account %s: %s", account.id, e)
        else:
            if isinstance(account, OAuthAccount):
                account.quota_state.consecutive_errors += 1
                account.quota_state.last_error_reason = err_str[:200]
            elif isinstance(account, WebAccount):
                account.consecutive_errors += 1
                account.last_error_reason = err_str[:200]

    async def execute_stream_with_failover(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
        stream_factory: Callable[..., AsyncIterator[Any]] | None = None,
        *,
        excluded_ids: set[str] | None = None,
        allow_cross_channel: bool = True,
        max_buffer_chunks: int | None = None,
        max_retries: int | None = None,
        now: float | None = None,
        validate_model: bool = True,
        **stream_kwargs: Any,
    ) -> tuple[AsyncIterator[Any], OAuthAccount | WebAccount, Literal["oauth", "web"]]:
        """Execute chat completion stream with pre-stream handshake and instant failover.

        Steps per attempt:
        a. Select optimal account via ``select_optimal_account`` (passing ``excluded_ids``).
        b. Increment concurrency counter ``account.active_requests += 1``.
        c. Apply ``apply_smart_pacing(account)`` if web account.
        d. Open stream generator via channel streaming function or ``stream_factory``.
        e. Consume initial chunks into ``buffered_chunks`` (up to ``HANDSHAKE_MAX_BUFFER_CHUNKS``).
        f. Upon first non-empty chunk (``is_chunk_non_empty(chunk) == True``):
           - Handshake succeeds!
           - Returns ``(chain_async_iterators(buffered_chunks, stream), account, channel_type)``.
        g. If generator ends without non-empty content (Zero-Byte EOF) or raises an error:
           - Handshake fails before delivering response to client.
           - Decrements active_requests immediately.
           - Enforces cooldown / 429 classification.
           - Adds account to ``excluded_ids`` and retries on next optimal account.
        h. If pool is exhausted, raises ``PoolExhaustedError``.
        """
        # Validate model
        if not model or not isinstance(model, str) or not model.strip():
            raise ModelNotSupportedError("Model identifier must be a non-empty string.")

        if validate_model and not is_model_supported_in_gateway(model, self.registry):
            raise ModelNotSupportedError(
                f"Model '{model}' is not supported in the Gemini-Hybrid-Pool-Gateway."
            )

        excluded: set[str] = set(excluded_ids) if excluded_ids is not None else set()
        eff_buffer_limit = max_buffer_chunks or self.max_buffer_chunks
        attempts = 0

        while True:
            if max_retries is not None and attempts >= max_retries:
                raise PoolExhaustedError(
                    f"Maximum retries ({max_retries}) reached for model '{model}' without successful handshake. "
                    f"Excluded accounts: {sorted(excluded)}"
                )

            # a. Select optimal account
            try:
                account, channel_type = select_optimal_account(
                    model=model,
                    messages=messages,
                    tools=tools,
                    excluded_ids=excluded,
                    allow_cross_channel=allow_cross_channel,
                    registry=self.registry,
                    lmdb_store=self.lmdb_store,
                    now=now,
                )
            except NoEligibleAccountError as exc:
                raise PoolExhaustedError(
                    f"Pool exhausted for model '{model}'. No eligible accounts available. "
                    f"Excluded accounts: {sorted(excluded)}"
                ) from exc

            account_id = getattr(account, "id", getattr(account, "account_id", str(account)))
            attempts += 1

            # b. Increment concurrency counter
            account.active_requests += 1

            # c. Apply smart pacing for Web account
            if channel_type == "web" and isinstance(account, WebAccount):
                try:
                    await apply_smart_pacing(account, now=now)
                except Exception as pace_err:
                    logger.warning("Pacing error on account %s: %s", account_id, pace_err)

            # d. Open stream generator
            try:
                if stream_factory is not None:
                    # Support stream_factory(account, channel_type, **kwargs),
                    # stream_factory(account, **kwargs), or stream_factory(account)
                    try:
                        stream = stream_factory(account, channel_type, **stream_kwargs)
                    except TypeError:
                        try:
                            stream = stream_factory(account, **stream_kwargs)
                        except TypeError:
                            stream = stream_factory(account)
                elif channel_type == "oauth":
                    stream = self.oauth_channel.stream_chat(
                        messages=messages,
                        account=account if isinstance(account, OAuthAccount) else None,
                        model=model,
                        tools=tools,
                        **stream_kwargs,
                    )
                else:
                    stream = self.web_channel.stream_chat(
                        account=account if isinstance(account, WebAccount) else account,
                        messages=messages,
                        model=model,
                        tools=tools,
                        **stream_kwargs,
                    )

                if inspect.iscoroutine(stream):
                    stream = await stream

                stream_iter = aiter(stream)
            except Exception as setup_err:
                logger.warning(
                    "Failed to open stream on account %s (%s): %s",
                    account_id,
                    channel_type,
                    setup_err,
                )
                await self._handle_handshake_failure(account, channel_type, setup_err, now=now)
                excluded.add(account_id)
                continue

            # e. Handshake: buffer initial chunks up to eff_buffer_limit
            buffered_chunks: list[Any] = []
            handshake_success = False
            stream_error: Exception | None = None

            try:
                while len(buffered_chunks) < eff_buffer_limit:
                    try:
                        chunk = await anext(stream_iter)
                    except StopAsyncIteration:
                        # Zero-byte or empty stream reached EOF
                        break

                    buffered_chunks.append(chunk)
                    if is_chunk_non_empty(chunk):
                        handshake_success = True
                        break
            except Exception as chunk_err:
                stream_error = chunk_err

            # f. Evaluate handshake result
            if handshake_success:
                # Handshake succeeded!
                completed = False

                def _on_complete() -> None:
                    nonlocal completed
                    if not completed:
                        completed = True
                        account.active_requests = max(0, account.active_requests - 1)
                        now_ts = time.time() if now is None else float(now)
                        if hasattr(account, "last_completed_timestamp"):
                            account.last_completed_timestamp = now_ts
                        if hasattr(account, "last_request_end_time"):
                            account.last_request_end_time = now_ts

                chained_stream = chain_async_iterators(
                    buffered_chunks=buffered_chunks,
                    active_stream=stream_iter,
                    on_complete=_on_complete,
                )
                return chained_stream, account, channel_type

            # g. Handshake failed: zero-byte stream EOF or error before non-empty chunk
            failure_reason = stream_error or HandshakeFailedError(
                f"Stream on account {account_id} ended without non-empty content "
                f"({len(buffered_chunks)} empty chunks buffered)"
            )
            logger.warning(
                "Pre-stream handshake failed on account %s (%s): %s; triggering failover",
                account_id,
                channel_type,
                failure_reason,
            )

            if hasattr(stream_iter, "aclose") and callable(stream_iter.aclose):
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass

            await self._handle_handshake_failure(account, channel_type, failure_reason, now=now)
            excluded.add(account_id)
            # Continues loop to next candidate account

    async def execute_chat_completion_with_failover(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
        stream_factory: Callable[..., AsyncIterator[Any]] | None = None,
        *,
        excluded_ids: set[str] | None = None,
        allow_cross_channel: bool = True,
        max_retries: int | None = None,
        now: float | None = None,
        validate_model: bool = True,
        **stream_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute standard non-streaming chat completion with end-to-end failover resilience."""
        excluded: set[str] = set(excluded_ids) if excluded_ids is not None else set()
        attempts = 0

        while True:
            if max_retries is not None and attempts >= max_retries:
                raise PoolExhaustedError(
                    f"Maximum retries ({max_retries}) reached for non-streaming completion."
                )

            stream, account, channel_type = await self.execute_stream_with_failover(
                model=model,
                messages=messages,
                tools=tools,
                stream_factory=stream_factory,
                excluded_ids=excluded,
                allow_cross_channel=allow_cross_channel,
                max_retries=max_retries,
                now=now,
                validate_model=validate_model,
                **stream_kwargs,
            )
            attempts += 1
            account_id = getattr(account, "id", getattr(account, "account_id", str(account)))

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_map: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            created_ts = int(time.time() if now is None else float(now))

            stream_broken = False
            try:
                async for chunk in stream:
                    c_id = _get_field(chunk, "id")
                    if c_id:
                        response_id = str(c_id)
                    c_created = _get_field(chunk, "created")
                    if c_created and isinstance(c_created, (int, float)):
                        created_ts = int(c_created)

                    choices = _get_field(chunk, "choices")
                    if choices and isinstance(choices, (list, tuple)) and len(choices) > 0:
                        first_choice = choices[0]
                        fr = _get_field(first_choice, "finish_reason")
                        if fr:
                            finish_reason = str(fr)

                        delta = _get_field(first_choice, "delta")
                        if delta:
                            c_text = _get_field(delta, "content")
                            if c_text:
                                content_parts.append(str(c_text))
                            r_text = _get_field(delta, "reasoning_content")
                            if r_text:
                                reasoning_parts.append(str(r_text))
                            t_calls = _get_field(delta, "tool_calls")
                            if t_calls and isinstance(t_calls, (list, tuple)):
                                for tc in t_calls:
                                    idx = _get_field(tc, "index") or 0
                                    if idx not in tool_calls_map:
                                        tool_calls_map[idx] = {
                                            "id": _get_field(tc, "id") or f"call_{uuid.uuid4().hex[:8]}",
                                            "type": "function",
                                            "function": {
                                                "name": "",
                                                "arguments": "",
                                            },
                                        }
                                    tc_id = _get_field(tc, "id")
                                    if tc_id:
                                        tool_calls_map[idx]["id"] = tc_id
                                    fn = _get_field(tc, "function")
                                    if fn:
                                        fn_name = _get_field(fn, "name")
                                        if fn_name:
                                            tool_calls_map[idx]["function"]["name"] = fn_name
                                        fn_args = _get_field(fn, "arguments")
                                        if fn_args:
                                            tool_calls_map[idx]["function"]["arguments"] += fn_args
            except Exception as mid_stream_err:
                logger.warning(
                    "Stream broke mid-transfer on account %s (%s): %s; failing over",
                    account_id,
                    channel_type,
                    mid_stream_err,
                )
                stream_broken = True
                excluded.add(account_id)

            if stream_broken:
                continue

            full_content = "".join(content_parts) if content_parts else None
            full_reasoning = "".join(reasoning_parts) if reasoning_parts else None
            assembled_tools = (
                [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
                if tool_calls_map
                else None
            )

            if assembled_tools and not finish_reason:
                finish_reason = "tool_calls"
            elif not finish_reason:
                finish_reason = "stop"

            message_payload: dict[str, Any] = {
                "role": "assistant",
                "content": full_content,
            }
            if full_reasoning is not None:
                message_payload["reasoning_content"] = full_reasoning
            if assembled_tools is not None:
                message_payload["tool_calls"] = assembled_tools

            return {
                "id": response_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message_payload,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": len(full_content.split()) if full_content else 0,
                    "total_tokens": len(full_content.split()) if full_content else 0,
                },
                "_account_id": account_id,
                "_channel": channel_type,
            }


# ============================================================================
# 5. Top-Level Module Functions
# ============================================================================

default_failover_controller: FailoverController = FailoverController()


async def execute_stream_with_failover(
    model: str,
    messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
    stream_factory: Callable[..., AsyncIterator[Any]] | None = None,
    *,
    controller: FailoverController | None = None,
    registry: AccountsStateRegistry | None = None,
    lmdb_store: LMDBStore | None = None,
    usage_monitor: UsageMonitor | None = None,
    **kwargs: Any,
) -> tuple[AsyncIterator[Any], OAuthAccount | WebAccount, Literal["oauth", "web"]]:
    """Execute streaming completion turn with non-empty pre-stream handshake and instant failover."""
    ctrl = controller
    if ctrl is None:
        if registry is not None or lmdb_store is not None or usage_monitor is not None:
            ctrl = FailoverController(
                registry=registry,
                lmdb_store=lmdb_store,
                usage_monitor=usage_monitor,
            )
        else:
            ctrl = default_failover_controller

    return await ctrl.execute_stream_with_failover(
        model=model,
        messages=messages,
        tools=tools,
        stream_factory=stream_factory,
        **kwargs,
    )


async def execute_chat_completion_with_failover(
    model: str,
    messages: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] | None = None,
    stream_factory: Callable[..., AsyncIterator[Any]] | None = None,
    *,
    controller: FailoverController | None = None,
    registry: AccountsStateRegistry | None = None,
    lmdb_store: LMDBStore | None = None,
    usage_monitor: UsageMonitor | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute standard non-streaming completion turn with instant failover resilience."""
    ctrl = controller
    if ctrl is None:
        if registry is not None or lmdb_store is not None or usage_monitor is not None:
            ctrl = FailoverController(
                registry=registry,
                lmdb_store=lmdb_store,
                usage_monitor=usage_monitor,
            )
        else:
            ctrl = default_failover_controller

    return await ctrl.execute_chat_completion_with_failover(
        model=model,
        messages=messages,
        tools=tools,
        stream_factory=stream_factory,
        **kwargs,
    )


__all__ = [
    "HANDSHAKE_MAX_BUFFER_CHUNKS",
    "FailoverController",
    "HandshakeFailedError",
    "ModelNotSupportedError",
    "PoolExhaustedError",
    "chain_async_iterators",
    "default_failover_controller",
    "execute_chat_completion_with_failover",
    "execute_stream_with_failover",
    "is_chunk_non_empty",
    "is_model_supported_in_gateway",
]
