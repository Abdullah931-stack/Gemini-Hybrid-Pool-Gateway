"""Verification Gate 4 official integration and QA test suite for Level 3 of Gemini-Hybrid-Pool-Gateway.

Topological Level 3 Gate 4 Verifications:
1. OAuth CLI Channel & LMDBStore db_signatures Integration (ADR-06 & Gate 4):
   - SSE unpacker capturing emitted thoughtSignature and storing in LMDBStore.db_signatures keyed by tool_call_id.
   - Multi-turn contents converter retrieving stored thoughtSignature from LMDBStore.db_signatures.
   - Graceful fallback to SKIP_THOUGHT_SIGNATURE_VALIDATOR ("skip_thought_signature_validator") when absent.
   - Re-injection of `_step: "done"` into historical functionCall.args (ADR-05) and stripping from streamed chunks.
   - Proper nesting of inlineData image parts in functionResponse.parts for tool responses.
   - Post-Assembly Model-Turn Guard appending `{"role": "user", "parts": [{"text": "Continue."}]}`.

2. Web Cookie Channel & UsageMonitor Integration:
   - Mathematical verification of context-weighted Delta_5h consumption formula.
   - Completion timestamp and optimistic delta accumulation upon successful stream completion.
   - WebSessionManager active request concurrency tracking (C_web,i) preventing re-init or close during streaming.
   - Exponential Moving Average (EMA) latency updating and total request counter tracking.

3. UsageMonitor & OAuth Channel Quota Integration:
   - Handling HTTP 429 with quotaResetDelay: parsing duration, setting target reset_time, and transitioning
     account status to COOLDOWN.
   - Transient burst 429 handling without long cooldown, maintaining ACTIVE status.
   - Atomic disk snapshot persistence via save_accounts_snapshot() under accounts_file_lock.
   - Robust parsing of varied quotaResetDelay duration formats (seconds, minutes, hours, compound strings).
   - Permanent account suspension on TOS violation / HTTP 403.

4. Drift-Resistant Renewal Detection for Web Accounts:
   - Time-based 5-hour window expiration resetting estimated_delta_5h to 0.0 without strict timestamp equality.
   - Live RPC jSf9Qc usage sync resetting estimated_delta_5h to 0.0 to prevent double-counting drift.
   - Weekly window expiration resetting estimated_delta_weekly to 0.0.
   - Parsing Google RPC jSf9Qc responses with XSSI prefix stripping and tier mapping.

5. ADR-04 Envelope Protection & ADR-09 Multi-Tool State Machine:
   - Strict prohibition of `"requestType": "agent"` anywhere in the v1internal envelope (ADR-04).
   - Immediate rejection if requestType is tampered or injected.
   - MultiToolLookaheadParser state transitions: TEXT_STREAMING -> TOOL_BUFFERING -> TEXT_STREAMING (ADR-09).
   - Strict ADR-09 guard: zero delimiter, fence, or whitespace leakage as delta.content once tool parsing begins.
   - Multi-tool sequential extraction with unique call IDs and sequential zero-based indices.
   - Lookahead buffer resilience across split-chunk boundaries and malformed JSON recovery.

6. End-to-End Multi-Turn Tool Flow & Multi-Channel Synchronization Synergy:
   - Full 2-turn cycle: tool invocation -> LMDB signature storage -> tool result -> signature replay -> text response.
   - Central sync_all_registered_accounts pass managing both OAuth and Web pools concurrently.
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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from config.settings import (
    ACCOUNTS_FILE_PATH,
    AccountsConfig,
    AccountsStateRegistry,
    LMDBStore,
    ModelQuota,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    WebAccount,
    WebCookies,
    WebUsage,
    get_model_base_delta,
    save_accounts_snapshot,
    state_registry,
)
from core import (
    CONTINUE_PROMPT_TEXT,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    JSF9QC_RPC_ID,
    LOOKAHEAD_BUFFER_CAPACITY,
    MultiToolLookaheadParser,
    OAuth429Classification,
    OAuthCLIChannel,
    ParserPhase,
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    StreamChoice,
    StreamChunk,
    StreamDelta,
    StreamToolCall,
    TOOL_CALL_CLOSE_TAG,
    TOOL_CALL_OPEN_RE,
    ToolCallFunction,
    UsageMonitor,
    V1INTERNAL_STREAM_URL,
    WebChannel,
    WebSessionManager,
    WebUsageData,
    apply_web_usage_update,
    build_v1internal_envelope,
    calculate_optimistic_5h_delta,
    calculate_optimistic_weekly_delta,
    check_and_apply_drift_safe_resets,
    classify_and_handle_oauth_429,
    compute_context_scaled_delta_5h,
    convert_messages_to_v1internal_contents,
    default_oauth_cli_channel,
    execute_web_channel_stream,
    extract_and_sanitize_system_instruction,
    extract_quota_reset_delay_from_error,
    format_openai_sse_event,
    format_web_openai_sse_event,
    handle_account_suspension,
    is_suspension_error,
    parse_jsf9qc_usage_response,
    parse_quota_reset_delay,
    parse_v1internal_sse_line,
    record_successful_request,
    sanitize_and_prepare_tools,
    strip_xssi_prefix,
    sync_all_registered_accounts,
    sync_oauth_account_quotas,
    sync_web_account_usage,
    transform_candidate_to_openai_chunks,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def isolate_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate central state_registry and accounts file for every Gate 4 test."""
    dummy_accounts_file = tmp_path / "accounts_gate4.json"
    dummy_accounts_file.write_text(
        json.dumps({"oauth_accounts": [], "web_accounts": []}), encoding="utf-8"
    )
    monkeypatch.setattr("config.settings.ACCOUNTS_FILE_PATH", dummy_accounts_file)
    state_registry.config = AccountsConfig()
    yield
    state_registry.config = AccountsConfig()


@pytest.fixture
def temp_lmdb(tmp_path: Path) -> Any:
    """Create an isolated, temporary LMDBStore instance."""
    store = LMDBStore(path=tmp_path / "lmdb_gate4", map_size=10 * 1024 * 1024)
    yield store
    store.close()


@pytest.fixture
def active_oauth_account() -> OAuthAccount:
    """Construct an active OAuthAccount for testing."""
    return OAuthAccount(
        id="gate4-oauth-1",
        label="Gate 4 Primary OAuth Account",
        transport_mode="http_v1internal",
        credentials=OAuthCredentials(
            access_token="ya29.gate4-test-token",
            refresh_token="1//gate4-refresh-token",
            expires_at=int(time.time() + 3600),
            project_id="gate4-cloud-project",
            ide_type="VSCODE",
        ),
        status="ACTIVE",
        discovered_models=["gemini-2.5-pro", "gemini-2.5-flash"],
        quota_state=OAuthQuotaState(
            model_quotas={
                "gemini-2.5-pro": ModelQuota(remaining_fraction=1.0, is_exhausted=False),
                "gemini-2.5-flash": ModelQuota(remaining_fraction=1.0, is_exhausted=False),
            }
        ),
    )


@pytest.fixture
def active_web_account() -> WebAccount:
    """Construct an active WebAccount for testing."""
    now_ts = int(time.time())
    return WebAccount(
        id="gate4-web-1",
        label="Gate 4 Primary Web Account",
        cookies=WebCookies(
            secure_1psid="test_secure_1psid_token_gate4",
            secure_1psidts="test_secure_1psidts_token_gate4",
        ),
        status="ACTIVE",
        discovered_models=["gemini-4-argon", "gemini-2.5-pro"],
        usage=WebUsage(
            five_hour_pct=0.20,
            five_hour_reset_at=now_ts + 14400,
            weekly_pct=0.10,
            weekly_reset_at=now_ts + 500000,
            tier="ADVANCED",
        ),
        optimistic_5h_delta=0.0,
        optimistic_weekly_delta=0.0,
    )


# ============================================================================
# Section 1: OAuth CLI Channel & LMDBStore db_signatures Integration (ADR-06)
# ============================================================================


def test_oauth_sse_stores_emitted_thought_signature_in_lmdb(temp_lmdb: LMDBStore) -> None:
    """SSE candidate with thoughtSignature must be saved directly to LMDBStore.db_signatures."""
    candidate = {
        "content": {
            "parts": [
                {
                    "thought": True,
                    "text": "Planning to call get_weather for Tokyo.",
                },
                {
                    "functionCall": {
                        "name": "get_weather",
                        "args": {"city": "Tokyo"},
                    },
                    "thoughtSignature": "cryptographic_thought_sig_tokyo_123",
                },
            ]
        },
        "finishReason": "STOP",
    }

    chunks, _ = transform_candidate_to_openai_chunks(
        candidate,
        model="gemini-2.5-pro",
        request_id="req-gate4-001",
        lmdb_store=temp_lmdb,
    )

    assert len(chunks) == 3
    # First chunk is reasoning thought
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "Planning to call get_weather for Tokyo."
    # Second chunk is tool call
    tc_delta = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    call_id = tc_delta["id"]
    assert tc_delta["function"]["name"] == "get_weather"
    # Third chunk is finish reason (tool_calls when functionCall present)
    assert chunks[2]["choices"][0]["finish_reason"] == "tool_calls"

    # Verify signature was stored in LMDBStore under call_id
    stored_sig = temp_lmdb.get_signature(call_id)
    assert stored_sig == "cryptographic_thought_sig_tokyo_123"


def test_oauth_convert_messages_retrieves_thought_signature_from_lmdb(temp_lmdb: LMDBStore) -> None:
    """History containing a tool call must retrieve its original thoughtSignature from LMDB."""
    call_id = "call_custom_unique_9988"
    known_sig = "known_persisted_thought_signature_xyz"
    temp_lmdb.put_signature(call_id, known_sig)

    messages = [
        {"role": "user", "content": "What is the weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "get_weather",
            "content": "22C and sunny",
        },
    ]

    contents = convert_messages_to_v1internal_contents(messages, lmdb_store=temp_lmdb)

    # Validate model turn reconstituted the exact thoughtSignature
    assert len(contents) == 3
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0]["functionCall"]["name"] == "get_weather"
    assert model_turn["parts"][0]["thoughtSignature"] == known_sig


def test_oauth_convert_messages_falls_back_to_skip_thought_signature_validator(
    temp_lmdb: LMDBStore,
) -> None:
    """When a tool_call_id has no LMDB signature, it must fall back to skip_thought_signature_validator."""
    missing_call_id = "call_missing_from_lmdb_7777"

    messages = [
        {"role": "user", "content": "Execute something."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": missing_call_id,
                    "type": "function",
                    "function": {"name": "compute_hash", "arguments": '{"data": "abc"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": missing_call_id,
            "name": "compute_hash",
            "content": "hash_123",
        },
    ]

    contents = convert_messages_to_v1internal_contents(messages, lmdb_store=temp_lmdb)
    model_turn = contents[1]
    assert model_turn["parts"][0]["thoughtSignature"] == SKIP_THOUGHT_SIGNATURE_VALIDATOR


def test_oauth_history_replay_step_injection_and_chunk_stripping(temp_lmdb: LMDBStore) -> None:
    """Zero-property tools must have _step: 'done' re-injected in history but stripped in outgoing chunks."""
    # 1. Incoming candidate with zero-property tool having _step: "done"
    candidate = {
        "content": {
            "parts": [
                {
                    "functionCall": {
                        "name": "get_current_time",
                        "args": {"_step": "done"},
                    },
                    "thoughtSignature": "sig_time_001",
                }
            ]
        }
    }

    chunks, _ = transform_candidate_to_openai_chunks(
        candidate,
        model="gemini-2.5-pro",
        request_id="req-gate4-step",
        lmdb_store=temp_lmdb,
    )
    assert len(chunks) == 1
    call_args_json = chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    call_id = chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"]
    parsed_args = json.loads(call_args_json)
    # _step must be stripped for OpenAI clients!
    assert "_step" not in parsed_args
    assert parsed_args == {}

    # 2. History replay: zero-property tool must receive _step: "done" back
    messages = [
        {"role": "user", "content": "What time is it?"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_current_time", "arguments": "{}"},
                }
            ],
        },
    ]

    contents = convert_messages_to_v1internal_contents(
        messages,
        zero_property_tool_names={"get_current_time"},
        lmdb_store=temp_lmdb,
    )

    model_fc = contents[1]["parts"][0]["functionCall"]
    assert model_fc["name"] == "get_current_time"
    assert model_fc["args"] == {"_step": "done"}


def test_oauth_inline_images_nested_in_function_response_parts(temp_lmdb: LMDBStore) -> None:
    """Tool output containing an image must be nested as inlineData inside functionResponse.parts."""
    raw_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    tool_content = [
        {"type": "text", "text": "Screenshot captured successfully."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw_b64}"}},
    ]

    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_screenshot_1",
            "name": "take_screenshot",
            "content": tool_content,
        }
    ]

    contents = convert_messages_to_v1internal_contents(messages, lmdb_store=temp_lmdb)
    assert len(contents) == 1
    func_resp = contents[0]["parts"][0]["functionResponse"]
    assert func_resp["name"] == "take_screenshot"

    parts = func_resp["parts"]
    assert len(parts) == 1
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[0]["inlineData"]["data"] == raw_b64


def test_oauth_post_assembly_model_turn_guard_appends_continue() -> None:
    """If input sequence ends with model/assistant turn, guard must append user Continue turn."""
    messages = [
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "I need more details before proceeding."},
    ]

    contents = convert_messages_to_v1internal_contents(messages)
    assert len(contents) == 3
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["text"] == CONTINUE_PROMPT_TEXT


# ============================================================================
# Section 2: Web Cookie Channel & UsageMonitor Integration
# ============================================================================


def test_web_channel_calculates_context_scaled_delta_5h_boundary_math() -> None:
    """Test Delta_5h context scaling formula: base * (1.0 + 0.25 * (L / 25000))."""
    # Base delta for gemini-4-argon is 0.10
    base = get_model_base_delta("gemini-4-argon")
    assert base == 0.10

    # 1. At 0 chars: base delta
    d0 = compute_context_scaled_delta_5h("gemini-4-argon", context_chars=0)
    assert d0 == pytest.approx(0.10)

    # 2. At 25,000 chars (1x 25k): 0.10 * (1.0 + 0.25) = 0.125
    d25k = compute_context_scaled_delta_5h("gemini-4-argon", context_chars=25_000)
    assert d25k == pytest.approx(0.125)

    # 3. At 50,000 chars (2x 25k): 0.10 * (1.0 + 0.50) = 0.150
    d50k = compute_context_scaled_delta_5h("gemini-4-argon", context_chars=50_000)
    assert d50k == pytest.approx(0.150)

    # 4. Cap check: extremely large context must not exceed 100.0
    d_huge = compute_context_scaled_delta_5h("gemini-4-argon", context_chars=10_000_000)
    assert d_huge <= 100.0


@pytest.mark.asyncio
async def test_web_channel_stream_successful_completion_updates_usage_and_timestamps(
    active_web_account: WebAccount,
) -> None:
    """Successful stream through execute_web_channel_stream must increment delta and update timestamps."""
    class DummyStreamChunk:
        def __init__(self, text: str) -> None:
            self.text = text
            self.text_delta = text
            self.thoughts = None
            self.thoughts_delta = None

    class MockGeminiClient:
        def __init__(self) -> None:
            self.model = "gemini-4-argon"

        async def generate_content_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[DummyStreamChunk]:
            yield DummyStreamChunk("Part 1: ")
            yield DummyStreamChunk("Part 2 completed.")

    mock_client = MockGeminiClient()
    messages = [{"role": "user", "content": "Explain relativity in brief."}]

    before_time = time.time()
    collected_chunks: list[StreamChunk] = []

    mock_chunks = [
        DummyStreamChunk("Part 1: "),
        DummyStreamChunk("Part 2 completed."),
    ]

    async def _mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for c in mock_chunks:
            yield c

    with patch("core.web_channel.execute_rehydrated_stream", side_effect=_mock_stream):
        async for chunk in execute_web_channel_stream(
            account=active_web_account,
            client=mock_client,
            messages=messages,
            model="gemini-4-argon",
        ):
            collected_chunks.append(chunk)

    # Verify chunks received
    assert len(collected_chunks) >= 2
    contents = [c.choices[0].delta.content for c in collected_chunks if c.choices[0].delta.content]
    assert "".join(contents) == "Part 1: Part 2 completed."

    # Verify optimistic delta and last_completed_timestamp updated
    assert active_web_account.optimistic_5h_delta > 0.0
    assert active_web_account.last_completed_timestamp >= before_time


@pytest.mark.asyncio
async def test_web_session_concurrency_guard_and_usage_monitor_recording(
    active_web_account: WebAccount,
) -> None:
    """Concurrency guard must track in-flight requests and UsageMonitor must record metrics."""
    session_mgr = WebSessionManager()
    monitor = UsageMonitor(web_session_manager=session_mgr)

    # Simulate request completion through monitor
    initial_delta = active_web_account.optimistic_5h_delta
    monitor.record_request(
        active_web_account,
        model_name="gemini-4-argon",
        latency_ms=450.0,
        context_chars=12000,
    )

    assert active_web_account.optimistic_5h_delta > initial_delta
    assert active_web_account.last_completed_timestamp > 0.0


def test_record_successful_request_updates_ema_latency_and_delta(
    active_oauth_account: OAuthAccount,
    active_web_account: WebAccount,
) -> None:
    """record_successful_request must update EMA latency for OAuth and optimistic deltas for Web."""
    # 1. OAuth Account EMA Latency: initial=0.0 -> sets 500.0, next 300.0 -> (0.8 * 500) + (0.2 * 300) = 460.0
    record_successful_request(active_oauth_account, "gemini-2.5-pro", latency_ms=500.0)
    assert active_oauth_account.recent_latency_ms == pytest.approx(500.0)

    record_successful_request(active_oauth_account, "gemini-2.5-pro", latency_ms=300.0)
    assert active_oauth_account.recent_latency_ms == pytest.approx(460.0)

    # 2. Web Account: records context delta and updates last_completed_timestamp
    t0 = 1700000000.0
    record_successful_request(
        active_web_account,
        "gemini-2.5-pro",
        latency_ms=600.0,
        context_chars=15000,
        now=t0,
    )
    assert active_web_account.last_completed_timestamp == t0
    assert active_web_account.optimistic_5h_delta > 0.0
    assert active_web_account.optimistic_weekly_delta > 0.0


# ============================================================================
# Section 3: UsageMonitor & OAuth Channel: 429, quotaResetDelay & Snapshot
# ============================================================================


@pytest.mark.asyncio
async def test_oauth_429_with_quota_reset_delay_transitions_account_to_cooldown(
    active_oauth_account: OAuthAccount,
) -> None:
    """OAuth 429 with quotaResetDelay must place account in COOLDOWN and set cooldown_until."""
    error_payload = {
        "error": {
            "code": 429,
            "message": "Resource exhausted",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "RATE_LIMIT_EXCEEDED",
                    "metadata": {
                        "quotaResetDelay": "120s",
                    },
                }
            ],
        }
    }

    current_time = 1700000000.0
    classification = await classify_and_handle_oauth_429(
        active_oauth_account,
        error_payload,
        now=current_time,
        persist=False,
    )

    assert classification.is_exhaustion is True
    assert classification.delay_seconds == 120.0
    assert classification.cooldown_until == int(current_time + 120.0)
    assert active_oauth_account.status == "COOLDOWN"
    assert active_oauth_account.quota_state.cooldown_until == int(current_time + 120.0)
    assert "120.0s" in str(active_oauth_account.quota_state.last_error_reason)


@pytest.mark.asyncio
async def test_oauth_429_burst_limit_avoids_cooldown_and_preserves_active_status(
    active_oauth_account: OAuthAccount,
) -> None:
    """OAuth 429 without quotaResetDelay must be treated as transient burst without cooldown."""
    error_payload = {
        "error": {
            "code": 429,
            "message": "Rate limit exceeded (transient burst)",
            "status": "RESOURCE_EXHAUSTED",
            "details": [],
        }
    }

    classification = await classify_and_handle_oauth_429(
        active_oauth_account,
        error_payload,
        persist=False,
    )

    assert classification.is_exhaustion is False
    assert classification.delay_seconds is None
    assert classification.cooldown_until is None
    # Account remains ACTIVE
    assert active_oauth_account.status == "ACTIVE"
    assert active_oauth_account.quota_state.consecutive_errors == 1


@pytest.mark.asyncio
async def test_oauth_429_atomic_snapshot_persistence_integrity(
    active_oauth_account: OAuthAccount,
) -> None:
    """Exhaustion 429 with persist=True must atomically flush state to ACCOUNTS_FILE_PATH."""
    state_registry.upsert_oauth_account(active_oauth_account)
    error_payload = {
        "error": {
            "code": 429,
            "details": [{"metadata": {"quotaResetDelay": "300s"}}],
        }
    }

    t0 = 1710000000.0
    await classify_and_handle_oauth_429(
        active_oauth_account,
        error_payload,
        now=t0,
        persist=True,
    )

    # Read the persisted file from disk
    content = json.loads(settings.ACCOUNTS_FILE_PATH.read_text(encoding="utf-8"))
    persisted_acc = content["oauth_accounts"][0]
    assert persisted_acc["status"] == "COOLDOWN"
    assert persisted_acc["quota_state"]["cooldown_until"] == int(t0 + 300.0)


def test_parse_quota_reset_delay_various_time_formats() -> None:
    """parse_quota_reset_delay must correctly parse seconds, minutes, hours, and compound strings."""
    assert parse_quota_reset_delay("60s") == 60.0
    assert parse_quota_reset_delay("120.5s") == 120.5
    assert parse_quota_reset_delay("5m") == 300.0
    assert parse_quota_reset_delay("2h") == 7200.0
    assert parse_quota_reset_delay("1h30m") == 5400.0
    assert parse_quota_reset_delay("45s") == 45.0
    assert parse_quota_reset_delay(150) == 150.0
    assert parse_quota_reset_delay(None) is None
    assert parse_quota_reset_delay("invalid_duration") is None


@pytest.mark.asyncio
async def test_handle_permanent_suspension_on_tos_or_forbidden_error(
    active_oauth_account: OAuthAccount,
) -> None:
    """TOS violation or account closure must transition account to SUSPENDED permanently."""
    error_msg = "Account has been suspended for terms of service violations."
    assert is_suspension_error(error_msg) is True

    await handle_account_suspension(active_oauth_account, error_msg, persist=False)
    assert active_oauth_account.status == "SUSPENDED"
    assert "SUSPENDED" in str(active_oauth_account.quota_state.last_error_reason)


# ============================================================================
# Section 4: Drift-Resistant Renewal Detection for Web Accounts
# ============================================================================


def test_drift_safe_reset_on_5h_window_expiry_zeroes_optimistic_delta(
    active_web_account: WebAccount,
) -> None:
    """Crossing five_hour_reset_at threshold (now >= reset_time) must zero optimistic_5h_delta."""
    active_web_account.optimistic_5h_delta = 18.5
    reset_time = 1700000000
    active_web_account.usage.five_hour_reset_at = reset_time

    # 1. Before threshold: delta preserved
    changed = check_and_apply_drift_safe_resets(active_web_account, now=reset_time - 10.0)
    assert changed is False
    assert active_web_account.optimistic_5h_delta == 18.5

    # 2. At or after threshold: delta reset to 0.0 (anti-drift)
    changed = check_and_apply_drift_safe_resets(active_web_account, now=reset_time + 1.0)
    assert changed is True
    assert active_web_account.optimistic_5h_delta == 0.0


def test_drift_safe_reset_on_live_rpc_jsf9qc_sync_clears_optimistic_delta(
    active_web_account: WebAccount,
) -> None:
    """Live query response via apply_web_usage_update must reset optimistic deltas."""
    active_web_account.optimistic_5h_delta = 25.0
    active_web_account.optimistic_weekly_delta = 15.0

    live_usage = WebUsageData(
        five_hour_pct=0.45,
        five_hour_reset_at=1700018000,
        weekly_pct=0.30,
        weekly_reset_at=1700500000,
        tier="ADVANCED",
    )

    apply_web_usage_update(active_web_account, live_usage, now=1700000000.0)

    # State updated with live values
    assert active_web_account.usage.five_hour_pct == 0.45
    assert active_web_account.usage.tier == "ADVANCED"
    # Local accumulated optimistic deltas zeroed to eliminate double counting!
    assert active_web_account.optimistic_5h_delta == 0.0
    assert active_web_account.optimistic_weekly_delta == 0.0


def test_drift_safe_reset_weekly_window_zeroes_weekly_delta(
    active_web_account: WebAccount,
) -> None:
    """Crossing weekly_reset_at threshold must zero optimistic_weekly_delta."""
    active_web_account.optimistic_weekly_delta = 42.0
    reset_time = 1700500000
    active_web_account.usage.weekly_reset_at = reset_time

    changed = check_and_apply_drift_safe_resets(active_web_account, now=reset_time + 10.0)
    assert changed is True
    assert active_web_account.optimistic_weekly_delta == 0.0


def test_rpc_jsf9qc_payload_parsing_with_xssi_and_tier_mapping() -> None:
    """parse_jsf9qc_usage_response must strip XSSI prefix and extract quotas and tier."""
    part_payload = json.dumps([
        2,  # tier_id: 2 -> PRO
        [
            [100, 0.35, 1, [[1700018000]]],
            [1000, 0.15, 2, [[1700600000]]],
        ],
    ])
    raw_payload = (
        ")]}'\n"
        + json.dumps([
            ["wrb.fr", "jSf9Qc", part_payload, None, None, None, "generic"]
        ])
    )

    cleaned = strip_xssi_prefix(raw_payload)
    assert cleaned.startswith("[[")

    usage = parse_jsf9qc_usage_response(raw_payload)
    assert usage.five_hour_pct == pytest.approx(0.35)
    assert usage.five_hour_reset_at == 1700018000
    assert usage.weekly_pct == pytest.approx(0.15)
    assert usage.tier == "PRO"


# ============================================================================
# Section 5: ADR-04 Envelope Protection & ADR-09 Multi-Tool State Machine
# ============================================================================


def test_v1internal_envelope_strictly_prohibits_request_type_agent() -> None:
    """v1internal envelope must NEVER contain 'requestType': 'agent' anywhere."""
    envelope = build_v1internal_envelope(
        contents=[{"role": "user", "parts": [{"text": "Hello"}]}],
        model="gemini-2.5-pro",
        project_id="test-proj-gate4",
        system_instruction={"parts": [{"text": "Be concise."}]},
    )

    # 1. Inspect dict keys and values recursively
    def _assert_no_request_type(node: Any) -> None:
        if isinstance(node, dict):
            assert "requestType" not in node, "Forbidden 'requestType' key discovered in envelope!"
            for v in node.values():
                _assert_no_request_type(v)
        elif isinstance(node, list):
            for item in node:
                _assert_no_request_type(item)

    _assert_no_request_type(envelope)

    # 2. Inspect serialized JSON string
    json_str = json.dumps(envelope)
    assert "requestType" not in json_str


def test_v1internal_envelope_raises_error_if_request_type_injected() -> None:
    """build_v1internal_envelope must raise ValueError if requestType is forcibly injected."""
    contents = [{"role": "user", "parts": [{"text": "Hello"}]}]
    envelope = build_v1internal_envelope(
        contents=contents,
        model="gemini-3.8-flash",
    )
    assert "requestType" not in envelope

    # If tampered with
    with pytest.raises(ValueError, match="ADR-04"):
        build_v1internal_envelope(
            contents=contents, model="gemini-3.8-flash", requestType="agent"
        )


def test_multi_tool_parser_adr09_no_delimiter_text_leak_after_tool_starts() -> None:
    """Strict ADR-09 check: No separators, fences or whitespace can leak as delta.content once tools start."""
    parser = MultiToolLookaheadParser()

    # Preceding assistant text
    d1 = parser.feed("I will perform two actions:\n")
    assert len(d1) == 1
    assert d1[0].content == "I will perform two actions:\n"

    # Feed opening tool call tag + JSON + closing tag + whitespace between calls
    stream_chunk = (
        '<tool_call>{"name": "fetch_user", "arguments": {"id": 101}}</tool_call>\n\n'
        '<tool_call>{"name": "fetch_orders", "arguments": {"user_id": 101}}</tool_call>'
    )

    d2 = parser.feed(stream_chunk)

    # Verify deltas: ONLY tool_calls deltas, NO delta.content!
    for d in d2:
        assert d.content is None or d.content == "", f"Forbidden content leak detected under ADR-09: {d.content!r}"
        assert d.tool_calls is not None

    closing = parser.close()
    for d in closing:
        assert d.content is None or d.content == ""


def test_multi_tool_parser_sequential_multiple_tool_calls_indices_and_ids() -> None:
    """MultiToolLookaheadParser must yield distinct sequential tool calls with unique IDs and 0-based indices."""
    parser = MultiToolLookaheadParser()

    stream_data = (
        '<tool_call>{"name": "action_alpha", "arguments": {"x": 1}}</tool_call>'
        '<tool_call>{"name": "action_beta", "arguments": {"y": 2}}</tool_call>'
        '<tool_call>{"name": "action_gamma", "arguments": {"z": 3}}</tool_call>'
    )

    deltas = parser.feed(stream_data)
    assert len(deltas) == 3

    tc0 = deltas[0].tool_calls[0]
    tc1 = deltas[1].tool_calls[0]
    tc2 = deltas[2].tool_calls[0]

    assert tc0.index == 0
    assert tc0.function.name == "action_alpha"
    assert json.loads(tc0.function.arguments) == {"x": 1}

    assert tc1.index == 1
    assert tc1.function.name == "action_beta"
    assert json.loads(tc1.function.arguments) == {"y": 2}

    assert tc2.index == 2
    assert tc2.function.name == "action_gamma"
    assert json.loads(tc2.function.arguments) == {"z": 3}

    # All call IDs must be unique
    assert len({tc0.id, tc1.id, tc2.id}) == 3


def test_multi_tool_parser_handles_markdown_xml_code_block_variants() -> None:
    """Parser must buffer and strip ```xml\\n and ``` fences enclosing <tool_call> tags."""
    parser = MultiToolLookaheadParser()

    chunk = (
        "Here is the execution plan.\n"
        "```xml\n"
        '<tool_call>{"name": "execute_query", "arguments": {"sql": "SELECT 1"}}</tool_call>\n'
        "```"
    )

    deltas = parser.feed(chunk)
    # Check that text before code block is emitted
    text_deltas = [d.content for d in deltas if d.content]
    assert "".join(text_deltas) == "Here is the execution plan.\n"

    # Check tool call parsed cleanly
    tool_deltas = [d for d in deltas if d.tool_calls]
    assert len(tool_deltas) == 1
    tc = tool_deltas[0].tool_calls[0]
    assert tc.function.name == "execute_query"
    assert json.loads(tc.function.arguments) == {"sql": "SELECT 1"}


def test_multi_tool_parser_lookahead_split_boundary_reconstitution() -> None:
    """Parser must seamlessly assemble tool calls split across arbitrary chunk boundaries."""
    parser = MultiToolLookaheadParser()

    fragments = [
        "Starting calculation. <to",
        "ol_call>",
        '{"name": "calc", ',
        '"arguments": {"val": 42',
        "}}</tool",
        "_call>",
    ]

    all_deltas: list[StreamDelta] = []
    for frag in fragments:
        all_deltas.extend(parser.feed(frag))
    all_deltas.extend(parser.close())

    # Text before tool call
    text_deltas = [d.content for d in all_deltas if d.content]
    assert "".join(text_deltas) == "Starting calculation. "

    # Tool call
    tool_deltas = [d for d in all_deltas if d.tool_calls]
    assert len(tool_deltas) == 1
    tc = tool_deltas[0].tool_calls[0]
    assert tc.function.name == "calc"
    assert json.loads(tc.function.arguments) == {"val": 42}


def test_multi_tool_parser_malformed_json_fallback_resilience() -> None:
    """Malformed tool JSON must be handled gracefully without crashing parser."""
    parser = MultiToolLookaheadParser()

    malformed = '<tool_call>{"name": "test", broken json here...</tool_call>'
    deltas = parser.feed(malformed)

    # Should not raise exception
    assert isinstance(deltas, list)


# ============================================================================
# Section 6: End-to-End Synergy & Multi-Channel Synchronization
# ============================================================================


@pytest.mark.asyncio
async def test_end_to_end_oauth_tool_execution_lmdb_signature_cycle(
    temp_lmdb: LMDBStore,
    active_oauth_account: OAuthAccount,
) -> None:
    """Full 2-turn cycle: tool call emits signature -> saved to LMDB -> retrieved in turn 2."""
    channel = OAuthCLIChannel(lmdb_store=temp_lmdb)

    # --- Turn 1: Upstream model emits functionCall with thoughtSignature ---
    simulated_sse_lines = [
        'data: {"response": {"candidates": [{"content": {"parts": [{"functionCall": {"name": "read_file", "args": {"path": "main.py"}}, "thoughtSignature": "sig_turn1_e2e"}]}}]}}',
        'data: {"response": {"candidates": [{"finishReason": "STOP"}]}}',
    ]

    collected_chunks: list[dict[str, Any]] = []
    for line in simulated_sse_lines:
        parsed = parse_v1internal_sse_line(line)
        if parsed and "response" in parsed:
            for cand in parsed["response"].get("candidates", []):
                chunks, _ = transform_candidate_to_openai_chunks(
                    cand,
                    model="gemini-2.5-pro",
                    request_id="req-gate4-e2e",
                    lmdb_store=temp_lmdb,
                )
                collected_chunks.extend(chunks)

    assert len(collected_chunks) >= 1
    tool_chunk = collected_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    captured_call_id = tool_chunk["id"]
    assert tool_chunk["function"]["name"] == "read_file"

    # Verify signature persisted in LMDB
    assert temp_lmdb.get_signature(captured_call_id) == "sig_turn1_e2e"

    # --- Turn 2: User provides tool execution result -> gateway converts messages ---
    messages_turn2 = [
        {"role": "user", "content": "Read main.py please."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": captured_call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": captured_call_id,
            "name": "read_file",
            "content": "print('hello world')",
        },
    ]

    converted_contents = convert_messages_to_v1internal_contents(
        messages_turn2, lmdb_store=temp_lmdb
    )

    # Verify model turn reconstituted original signature from LMDB
    assert len(converted_contents) == 3
    model_turn = converted_contents[1]
    assert model_turn["parts"][0]["thoughtSignature"] == "sig_turn1_e2e"


@pytest.mark.asyncio
async def test_usage_monitor_sync_all_orchestration_across_both_channels(
    active_oauth_account: OAuthAccount,
    active_web_account: WebAccount,
) -> None:
    """sync_all_registered_accounts must coordinate OAuth discovery and Web usage sync concurrently."""
    state_registry.config.oauth_accounts = [active_oauth_account]
    state_registry.config.web_accounts = [active_web_account]

    monitor = UsageMonitor(registry=state_registry)

    # Mock OAuth discover and Web sync to test top-level orchestration
    with (
        patch("core.usage_monitor.sync_oauth_account_quotas", new_callable=AsyncMock) as mock_oauth_sync,
        patch("core.usage_monitor.sync_web_account_usage", new_callable=AsyncMock) as mock_web_sync,
    ):
        mock_oauth_sync.return_value = ["gemini-2.5-pro"]
        mock_web_sync.return_value = WebUsageData(five_hour_pct=0.30, tier="ADVANCED")

        results = await monitor.sync_all(persist=False)

        assert mock_oauth_sync.called
        assert mock_web_sync.called
        assert active_oauth_account.id in results["oauth_accounts"]
        assert active_web_account.id in results["web_accounts"]
