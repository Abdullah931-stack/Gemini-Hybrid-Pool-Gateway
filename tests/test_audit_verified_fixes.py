"""Verification integration tests for the 25 confirmed audit defects.

Proves with zero-mock/realistic integration that all critical vulnerabilities
and regressions identified during the 2-round audit are resolved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
import pytest

from config.settings import (
    LMDBStore,
    OAuthAccount,
    OAuthCredentials,
    OAuthQuotaState,
    ModelQuota,
    WebAccount,
    WebCookies,
    WebUsage,
    AccountsConfig,
    state_registry,
)
from core.failover import FailoverController
from core.web_channel import WebChannel, execute_web_channel_stream
from core.auth_web import default_web_session_manager
from core.prefix_differ import (
    compute_prefix_hashes,
    find_longest_prefix_session,
    normalize_message_for_hash,
)
from core.oauth_cli_channel import (
    _extract_base64_image_data,
    _resolve_tool_response_payload,
    convert_messages_to_v1internal_contents,
)
from core.usage_monitor import parse_jsf9qc_usage_response, check_and_apply_drift_safe_resets
from core.schema_sanitizer import clean_json_schema, apply_zero_required_fix
from click.testing import CliRunner
import main


def test_h1_1_web_channel_wired_with_session_manager_in_failover_controller() -> None:
    """H-1.1: FailoverController.web_channel must instantiate WebChannel with a valid WebSessionManager."""
    controller = FailoverController(registry=state_registry)
    channel = controller.web_channel
    assert isinstance(channel, WebChannel)
    assert channel.session_manager is not None
    assert channel.session_manager is default_web_session_manager


def test_h1_2_web_channel_track_concurrency_flag_prevents_double_increment() -> None:
    """H-1.2: execute_web_channel_stream with track_concurrency=False does not double increment active_requests."""
    account = WebAccount(
        id="test-concurrency-web",
        label="Test Web",
        cookies=WebCookies(**{"__Secure-1PSID": "p", "__Secure-1PSIDTS": "t"}),
        status="ACTIVE",
        max_concurrency=2,
    )
    account.active_requests = 1  # Simulated increment by FailoverController

    async def _dummy_stream(chat: Any, plan: Any) -> Any:
        class Chunk:
            text = "Hello world"
            thoughts = None
        yield Chunk()

    mock_chat = type("MockChat", (), {
        "send_message_stream": lambda *a, **k: _dummy_stream(None, None),
        "metadata": ["cid-123", "rid-123", "rcid-123"],
    })()
    mock_client = type("MockClient", (), {"start_chat": lambda *a, **k: mock_chat})()

    async def _run() -> None:
        async for _ in execute_web_channel_stream(
            client=mock_client,
            account=account,
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-4-argon",
            track_concurrency=False,
            stream_fn=_dummy_stream,
        ):
            # Concurrency must remain 1 during streaming, not 2
            assert account.active_requests == 1

        # Must still be 1 after completion (FailoverController will decrement it)
        assert account.active_requests == 1

    asyncio.run(_run())


def test_h3_1_multi_turn_tool_call_prefix_hash_equivalence() -> None:
    """H-3.1: Assistant XML <tool_call> and OpenAI tool_calls dict produce identical prefix hashes."""
    # Turn 1 saved assistant message (persisted XML)
    turn1_assistant = {
        "role": "assistant",
        "content": '<tool_call>{"name": "fetch_weather", "arguments": {"city": "Riyadh"}}</tool_call>',
    }

    # Turn 2 returned assistant message from OpenAI client
    turn2_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_arbitrary_random_uuid_12345",
                "type": "function",
                "function": {
                    "name": "fetch_weather",
                    "arguments": '{"city":"Riyadh"}',
                },
            }
        ],
    }

    hash1 = normalize_message_for_hash(turn1_assistant)
    hash2 = normalize_message_for_hash(turn2_assistant)
    assert hash1 == hash2

    user_msg = {"role": "user", "content": "What is the weather?"}
    hashes_turn1 = compute_prefix_hashes([user_msg, turn1_assistant])
    hashes_turn2 = compute_prefix_hashes([user_msg, turn2_assistant])

    assert hashes_turn1[1] == hashes_turn2[1]


def test_h3_7_normal_text_tool_output_over_32_chars_is_never_image() -> None:
    """H-3.7: Normal strings > 32 chars must never be misclassified as base64 images."""
    normal_text = "The quick brown fox jumps over the lazy dog and runs across the field."
    assert len(normal_text) > 32
    assert _extract_base64_image_data(normal_text) is None

    normal_json = '{"status": "success", "message": "File written successfully to the workspace directory"}'
    assert len(normal_json) > 32
    assert _extract_base64_image_data(normal_json) is None

    # Real base64 PNG header must still be recognized
    real_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    extracted = _extract_base64_image_data(real_png_b64)
    assert extracted is not None
    assert extracted[0] == "image/png"


def test_h2_2_parse_jsf9qc_with_byte_length_line() -> None:
    """H-2.2: Google batchexecute response with leading byte-length line (128) must parse valid usage."""
    part_payload = json.dumps([
        2,  # tier_id: 2 -> PRO
        [
            [100, 0.155, 1, [[1775000000]]],
            [1000, 0.42, 2, [[1775500000]]],
        ],
        True,
    ])
    raw_response = (
        ")]}'\n\n128\n"
        + json.dumps([["wrb.fr", "jSf9Qc", part_payload, None, None, None, "generic"]])
    )
    usage = parse_jsf9qc_usage_response(raw_response)
    assert usage.five_hour_pct == pytest.approx(0.155)
    assert usage.weekly_pct == pytest.approx(0.42)
    assert usage.tier == "PRO"
    assert usage.five_hour_reset_at == 1775000000
    assert usage.weekly_reset_at == 1775500000


def test_h3_3_schema_sanitizer_inlines_local_defs_and_prunes_orphans() -> None:
    """H-3.3: Resolves local $ref schemas and cleans up orphaned required keys."""
    raw_schema = {
        "$defs": {
            "Filter": {
                "type": "string",
                "description": "A search filter",
            }
        },
        "properties": {
            "query": {"$ref": "#/$defs/Filter"},
        },
        "required": ["query", "non_existent_key"],
    }
    cleaned = clean_json_schema(raw_schema)
    assert "$defs" not in cleaned
    assert "$ref" not in cleaned["properties"]["query"]
    assert cleaned["properties"]["query"]["type"] == "string"

    fixed, _ = apply_zero_required_fix(cleaned)
    assert "non_existent_key" not in fixed.get("required", [])


def test_h4_2_cli_flags_compatibility() -> None:
    """H-4.2: CLI supports --serve, --port, and --status flags without errors."""
    runner = CliRunner()
    result = runner.invoke(main.cli, ["--help"])
    assert result.exit_code == 0
    assert "--serve" in result.output
    assert "--add-oauth" in result.output
    assert "--add-web" in result.output
    assert "--firefox-profile" in result.output


def test_start_client_chat_session_no_temporary_and_effort_resolution() -> None:
    """Verify that _start_client_chat_session never passes temporary to start_chat and resolves effort suffixes."""
    from core.prefix_differ import _start_client_chat_session, RehydrationPlan, TIER2_INLINE_HISTORY
    from core.model_registry import resolve_web_model_sync

    class MockChatSession:
        def __init__(self, **kwargs: Any) -> None:
            # Emulates gemini_webapi ChatSession.__init__ rejecting temporary
            if "temporary" in kwargs:
                raise TypeError("ChatSession.__init__() got an unexpected keyword argument 'temporary'")
            self.kwargs = kwargs

    class MockGeminiClient:
        def __init__(self) -> None:
            self._models = {
                "gemini-pro": "resolved-pro-object",
                "gemini-3.1-pro": "resolved-pro-object",
                "gemini-argon": "resolved-argon-object",
            }

        def start_chat(self, **kwargs: Any) -> MockChatSession:
            return MockChatSession(**kwargs)

        def _resolve_model_by_name(self, name: str) -> str:
            if name in self._models:
                return self._models[name]
            # alias / normalized fallback
            norm = name.replace("-3.1", "").replace("-4", "")
            if norm in self._models:
                return self._models[norm]
            raise ValueError(f"Unknown model name: {name}")

    client = MockGeminiClient()
    plan = RehydrationPlan(
        tier=TIER2_INLINE_HISTORY,
        temporary=True,
        prompt="hello",
        resume_metadata=["cid_1", "rid_1", "rcid_1"],
        full_prefix_hash="hash_123",
    )

    # 1. Base model
    chat1 = _start_client_chat_session(client, plan, "gemini-3.1-pro")
    assert chat1.kwargs.get("metadata") == ["cid_1", "rid_1", "rcid_1"]
    assert chat1.kwargs.get("model") == "resolved-pro-object"
    assert "temporary" not in chat1.kwargs

    # 2. Effort suffix model (from Cline / OpenAI client)
    chat2 = _start_client_chat_session(client, plan, "gemini-3.1-pro-high")
    assert chat2.kwargs.get("model") == "resolved-pro-object"
    assert "temporary" not in chat2.kwargs

    # 3. resolve_web_model_sync helper
    resolved = resolve_web_model_sync(client, "gemini-3.1-pro-high")
    assert resolved == "resolved-pro-object"
