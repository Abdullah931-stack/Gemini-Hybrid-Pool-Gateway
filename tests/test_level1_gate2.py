"""Verification Gate 2 test suite for Level 1 (`core/schema_sanitizer.py` & `core/token_saver.py`).

Covers:
1. JSON Schema Sanitizer (`core/schema_sanitizer.py`):
   - Recursive stripping of all 20 `UNSUPPORTED_SCHEMA_KEYS` at arbitrary nesting depths while
     strictly preserving tool parameters inside `properties` named after restricted keys
     (e.g., `"format"`, `"pattern"`, `"examples"`).
   - Recursive flattening of `anyOf`, `oneOf`, and `type: ["...", "null"]` unions into concrete
     Gemini-compatible types with `nullable: True`, plus object-branch property/required merging.
   - Dual Zero-Required & History Replay Fix (ADR-05):
     * Case 1 (zero properties): injects `_step` into `properties` and `required: ["_step"]`,
       paired with `strip_step_from_args` and `reinject_step_to_args` for history replay consistency.
     * Case 2 (optional properties with `required: []`): never injects `_step`; promotes the first
       optional property to `required` with `nullable: True` and default `""` in description.
   - Strict immutability (no in-place mutation of caller schemas, tools, or messages).
2. WAF System Prompt Shield (ADR-04):
   - Neutralizes `<system-conventions>` / `</system-conventions>` into `<conventions>` / `</conventions>`
     and isolates `RFC 2119` directive lines in `system` and `developer` messages (`str` and `list[dict]`).
   - Leaves `user`, `assistant`, and `tool` messages completely untouched.
3. RTK-Lite Lossless Tool Output Compressor (`core/token_saver.py`):
   - Compresses `git diff` outputs by removing `index <hash>..<hash>` headers and trailing/blank padding
     while preserving file paths, `@@` hunk headers, and `+`/`-`/context lines with 20%–40%+ reduction.
   - Compresses `grep`, `find`, and `ls -l` outputs (column padding collapse, leading `./` stripping,
     consecutive duplicate line collapsing).
   - Compresses repetitive test logs, progress bars, spinners, and ANSI escape sequences while
     preserving 100% of error/exception/traceback lines (`Traceback`, `AssertionError`, `ERROR`, `FAIL`).
   - Immediate bypass with 100% byte-for-byte identity when `X-Token-Saver: off` (or `enabled=False`).
"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import (
    UNSUPPORTED_SCHEMA_KEYS,
    apply_zero_required_fix,
    clean_json_schema,
    compress_git_diff,
    compress_grep_find_ls,
    compress_test_logs,
    compress_tool_output,
    flatten_unions,
    reinject_step_to_args,
    sanitize_messages_waf,
    sanitize_parameters_schema,
    sanitize_tool_messages,
    sanitize_tools,
    should_enable_token_saver,
    strip_step_from_args,
)


# ============================================================================
# 1. JSON Schema Sanitizer (`core/schema_sanitizer.py`)
# ============================================================================


def test_recursive_schema_cleaner_strips_all_20_unsupported_keys_and_preserves_named_properties() -> None:
    """All 20 `cloudcode-pa` banned keys must be stripped deeply without deleting properties named `format` or `pattern`."""
    expected_20_keys = {
        "additionalProperties",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        "examples",
        "patternProperties",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "multipleOf",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
    assert UNSUPPORTED_SCHEMA_KEYS == expected_20_keys
    assert len(UNSUPPORTED_SCHEMA_KEYS) == 20

    raw_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.com/tool.schema.json",
        "$defs": {"SubNode": {"type": "string", "minLength": 1}},
        "definitions": {"LegacyNode": {"type": "integer", "minimum": 0}},
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 10,
        "patternProperties": {"^x-": {"type": "string"}},
        "examples": [{"format": "json"}],
        "properties": {
            # User-defined tool parameters whose names collide with banned meta-keys:
            "format": {
                "type": "string",
                "description": "Output serialization format",
                "format": "uri-reference",
                "minLength": 2,
                "maxLength": 64,
                "pattern": "^[a-z]+$",
            },
            "pattern": {
                "type": "string",
                "description": "Search regex pattern",
                "examples": ["^def "],
                "minLength": 1,
            },
            "minimum": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 100.0,
                "multipleOf": 0.5,
            },
            "nested_filter": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "tags": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "$ref": "#/$defs/SubNode",
                            "pattern": "^[a-z0-9_-]+$",
                            "format": "slug",
                        },
                    }
                },
            },
        },
        "required": ["format", "pattern"],
    }

    cleaned = clean_json_schema(raw_schema)

    # Top-level banned keys stripped
    for key in expected_20_keys:
        assert key not in cleaned

    # User-defined properties named "format", "pattern", "minimum" MUST survive
    props = cleaned["properties"]
    assert set(props.keys()) == {"format", "pattern", "minimum", "nested_filter"}

    # Meta-keys inside those property definitions MUST be stripped
    assert props["format"] == {
        "type": "string",
        "description": "Output serialization format",
    }
    assert props["pattern"] == {
        "type": "string",
        "description": "Search regex pattern",
    }
    assert props["minimum"] == {"type": "number"}

    # Deeply nested array items meta-keys stripped
    tags_schema = props["nested_filter"]["properties"]["tags"]
    assert "minItems" not in tags_schema
    assert "maxItems" not in tags_schema
    assert "uniqueItems" not in tags_schema
    assert tags_schema["items"] == {"type": "string"}


@pytest.mark.parametrize(
    ("raw_fragment", "expected_flattened"),
    [
        # 1. Type array with null -> concrete scalar + nullable: True
        (
            {"type": ["string", "null"], "description": "Optional path"},
            {"type": "string", "nullable": True, "description": "Optional path"},
        ),
        # 2. anyOf scalar + null branch -> concrete scalar + nullable: True + merged description
        (
            {
                "description": "Timeout setting",
                "anyOf": [
                    {"type": "integer", "minimum": 1, "description": "Seconds"},
                    {"type": "null"},
                ],
            },
            {
                "type": "integer",
                "nullable": True,
                "description": "Timeout setting | Seconds",
            },
        ),
        # 3. oneOf multi-scalar + null -> picks first concrete type + nullable: True
        (
            {
                "oneOf": [
                    {"type": "null"},
                    {"type": "number", "maximum": 10},
                    {"type": "string", "format": "date"},
                ]
            },
            {"type": "number", "nullable": True},
        ),
        # 4. Object union branches -> merges properties and intersects required sets
        (
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "mode": {"type": "string"},
                        },
                        "required": ["id", "mode"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1},
                        },
                        "required": ["id"],
                    },
                    {"type": "null"},
                ]
            },
            {
                "type": "object",
                "nullable": True,
                "properties": {
                    "id": {"type": "string"},
                    "mode": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["id"],
            },
        ),
        # 5. Tuple-validation array where `items` is a `list[dict]` containing unions
        (
            {
                "type": "array",
                "items": [
                    {"type": ["string", "null"], "minLength": 1},
                    {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                ],
            },
            {
                "type": "array",
                "items": [
                    {"type": "string", "nullable": True},
                    {"type": "integer", "nullable": True},
                ],
            },
        ),
    ],
)
def test_union_and_type_array_flattener_produces_concrete_nullable_types(
    raw_fragment: dict[str, Any],
    expected_flattened: dict[str, Any],
) -> None:
    """`flatten_unions` must collapse `type: [...]`, `anyOf`, and `oneOf` into concrete schemas."""
    assert flatten_unions(raw_fragment) == expected_flattened


def test_zero_required_case_1_injects_step_and_maintains_history_replay_consistency() -> None:
    """ADR-05 Case 1: Tools with `properties: {}` receive `_step`, which is stripped for client and reinjected for history."""
    zero_prop_params = {"type": "object", "properties": {}, "required": []}

    fixed_params, injected_step = apply_zero_required_fix(zero_prop_params)
    assert injected_step is True
    assert fixed_params["required"] == ["_step"]
    assert fixed_params["properties"] == {
        "_step": {
            "type": "string",
            "description": "Internal step tracker",
        }
    }

    # End-to-end tool wrapper check
    openai_tool = {
        "type": "function",
        "function": {
            "name": "take_snapshot",
            "description": "Capture current state",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    sanitized_tool = sanitize_tools([openai_tool])[0]
    assert sanitized_tool["function"]["parameters"]["required"] == ["_step"]
    assert "_step" in sanitized_tool["function"]["parameters"]["properties"]

    # When Gemini emits functionCall.args with synthetic `_step`, strip_step_from_args removes it for the client
    model_emitted_args = {"_step": "executing snapshot"}
    client_visible_args = strip_step_from_args(model_emitted_args)
    assert client_visible_args == {}
    assert model_emitted_args == {"_step": "executing snapshot"}  # input not mutated

    # On subsequent turns, reinject_step_to_args restores `{"_step": "done"}` for Case 1 tools only
    rehydrated_case1 = reinject_step_to_args(client_visible_args, tool_had_zero_properties=True)
    assert rehydrated_case1 == {"_step": "done"}

    rehydrated_case2 = reinject_step_to_args(
        {"path": "src"}, tool_had_zero_properties=False
    )
    assert rehydrated_case2 == {"path": "src"}
    assert "_step" not in rehydrated_case2


def test_zero_required_case_2_promotes_first_optional_property_without_injecting_step() -> None:
    """ADR-05 Case 2: Tools with optional properties and `required: []` promote the first property instead of injecting `_step`."""
    optional_only_params = {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Target directory to list"},
            "recursive": {"type": "boolean", "description": "Recurse into subdirectories"},
        },
        "required": [],
    }

    fixed_params, injected_step = apply_zero_required_fix(optional_only_params)
    assert injected_step is False
    assert "_step" not in fixed_params["properties"]
    assert fixed_params["required"] == ["directory"]

    promoted = fixed_params["properties"]["directory"]
    assert promoted["nullable"] is True
    assert '""' in promoted["description"]
    assert promoted["description"].startswith("Target directory to list")

    # Second property remains untouched
    assert fixed_params["properties"]["recursive"] == {
        "type": "boolean",
        "description": "Recurse into subdirectories",
    }


def test_schema_and_waf_sanitizers_never_mutate_caller_inputs_in_place() -> None:
    """All functions in `core/schema_sanitizer.py` must leave input dicts and lists 100% unmutated."""
    original_tools = [
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "parameters": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "format": {"type": ["string", "null"], "minLength": 1},
                        "query": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": [],
                },
            },
        },
        {
            "functionDeclarations": [
                {
                    "name": "ping_server",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        },
    ]
    tools_snapshot = copy.deepcopy(original_tools)

    original_messages = [
        {
            "role": "system",
            "content": "<system-conventions>RFC 2119: MUST, REQUIRED</system-conventions>",
        },
        {
            "role": "developer",
            "content": [{"type": "text", "text": "<system-conventions>Follow rules</system-conventions>"}],
        },
    ]
    messages_snapshot = copy.deepcopy(original_messages)

    _ = sanitize_tools(original_tools)
    _ = sanitize_parameters_schema(original_tools[0]["function"]["parameters"])
    _ = sanitize_messages_waf(original_messages)

    assert original_tools == tools_snapshot
    assert original_messages == messages_snapshot


# ============================================================================
# 2. WAF System Prompt Shield (`WAF System Prompt Shield` — ADR-04)
# ============================================================================


def test_waf_shield_rewrites_system_and_developer_messages_and_isolates_rfc2119() -> None:
    """ADR-04: `<system-conventions>` becomes `<conventions>` and `RFC 2119` is isolated in `system`/`developer` roles."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "<system-conventions>\n"
                "RFC 2119: MUST, REQUIRED, SHALL, SHOULD, MAY.\n"
                "Always follow project coding standards.\n"
                "</system-conventions>"
            ),
        },
        {
            "role": "developer",
            "content": [
                {
                    "type": "text",
                    "text": "<system-conventions>RFC 2119: MUST NOT violate safety.</system-conventions>",
                },
                {
                    "type": "text",
                    "text": (
                        "<system-conventions>"
                        "Keywords are interpreted as described in RFC 2119: MUST, REQUIRED"
                        "</system-conventions>"
                    ),
                },
            ],
        },
        {
            "role": "user",
            "content": "Why does <system-conventions>RFC 2119: MUST</system-conventions> trigger WAF?",
        },
        {
            "role": "assistant",
            "content": "Because <system-conventions> with RFC 2119 matches an ESF rule.",
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "<system-conventions>RFC 2119: MUST</system-conventions>",
        },
    ]

    shielded = sanitize_messages_waf(messages)

    # 1. System message (`str` content)
    sys_text = shielded[0]["content"]
    assert "<system-conventions" not in sys_text
    assert "</system-conventions>" not in sys_text
    assert "<conventions>" in sys_text
    assert "</conventions>" in sys_text
    # Opening tag and RFC 2119 line must be separated by a blank line (`\n\n`)
    assert "<conventions>\n\nRFC 2119: MUST, REQUIRED, SHALL, SHOULD, MAY.\n\n" in sys_text

    # 2. Developer message (`list[dict]` multipart content)
    dev_part_text = shielded[1]["content"][0]["text"]
    assert "<system-conventions" not in dev_part_text
    assert "</system-conventions>" not in dev_part_text
    assert "<conventions>\n\nRFC 2119: MUST NOT violate safety.\n\n</conventions>" in dev_part_text

    # Regression check: natural English prose ("in RFC 2119: MUST, REQUIRED") must stay intact on its line
    dev_prose_text = shielded[1]["content"][1]["text"]
    assert (
        dev_prose_text
        == "<conventions>\n\nKeywords are interpreted as described in RFC 2119: MUST, REQUIRED\n\n</conventions>"
    )

    # 3. User, assistant, and tool messages MUST remain byte-for-byte untouched
    assert shielded[2]["content"] == messages[2]["content"]
    assert shielded[3]["content"] == messages[3]["content"]
    assert shielded[4]["content"] == messages[4]["content"]


# ============================================================================
# 3. RTK-Lite Lossless Tool Output Compressor (`core/token_saver.py`)
# ============================================================================


def test_git_diff_compression_strips_index_headers_and_achieves_20_to_40_pct_savings() -> None:
    """`compress_git_diff` must drop `index` hashes and padding while preserving paths, `@@` hunks, blank hunk context, and `+`/`-` lines."""
    raw_diff_blocks: list[str] = []
    for idx in range(1, 7):
        raw_diff_blocks.append(
            f"diff --git a/core/module_{idx}.py b/core/module_{idx}.py      \n"
            f"index 8f9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a..1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b 100644   \n"
            f"--- a/core/module_{idx}.py    \n"
            f"+++ b/core/module_{idx}.py    \n"
            "\n\n\n"
            f"@@ -10,9 +10,9 @@ def compute_score_{idx}(x: float) -> float:    \n"
            f"     \x1b[32m# context line before change\x1b[0m               \n"
            "\n"
            "\n"
            f"-    return x * 0.{idx}0000000000000000                     \n"
            f"+    return round(x * 0.{idx}, 4)                           \n"
            f"     # trailing context line                                \n"
        )
    raw_diff = "\n\n\n".join(raw_diff_blocks)

    compressed = compress_tool_output(raw_diff, enabled=True)

    # 1. Non-semantic `index <hash>..<hash>` lines and ANSI codes must be completely removed
    assert "index 8f9a1b2c" not in compressed
    assert "\x1b[" not in compressed

    # 2. All semantic file headers, hunk markers, consecutive blank context lines inside hunks, and code +/- lines must be preserved
    for idx in range(1, 7):
        assert f"diff --git a/core/module_{idx}.py b/core/module_{idx}.py" in compressed
        assert f"--- a/core/module_{idx}.py" in compressed
        assert f"+++ b/core/module_{idx}.py" in compressed
        assert (
            f"@@ -10,9 +10,9 @@ def compute_score_{idx}(x: float) -> float:\n"
            "     # context line before change\n"
            "\n"
            "\n"
            f"-    return x * 0.{idx}0000000000000000\n"
            f"+    return round(x * 0.{idx}, 4)\n"
            "     # trailing context line"
        ) in compressed

    # 3. Verify 20%+ lossless token/character reduction on realistic padded git diff output
    reduction_ratio = 1.0 - (len(compressed) / len(raw_diff))
    assert reduction_ratio >= 0.20, f"Expected >= 20% reduction, got {reduction_ratio:.2%}"


def test_grep_find_and_ls_compression_normalizes_columns_paths_and_duplicates() -> None:
    """`compress_grep_find_ls` must collapse `ls -l` padding, strip leading `./` in `find`/`grep`, and deduplicate repeats."""
    raw_output = (
        "total   128   \n"
        "-rw-r--r--    1   root    staff      4096   Oct  16   20:15   config/settings.py   \n"
        "drwxr-xr-x    5   root    staff       160   Oct  16   20:16   core   \n"
        "./core/token_saver.py\n"
        "./core/schema_sanitizer.py\n"
        "./core/token_saver.py:42:def compress_tool_output(\n"
        "./core/token_saver.py:42:def compress_tool_output(\n"
        "./core/token_saver.py:42:def compress_tool_output(\n"
    )

    compressed = compress_grep_find_ls(raw_output)
    lines = compressed.split("\n")

    assert lines[0] == "total 128"
    assert lines[1] == "-rw-r--r-- 1 root staff 4096 Oct 16 20:15 config/settings.py"
    assert lines[2] == "drwxr-xr-x 5 root staff 160 Oct 16 20:16 core"
    assert lines[3] == "core/token_saver.py"
    assert lines[4] == "core/schema_sanitizer.py"
    assert lines[5] == "core/token_saver.py:42:def compress_tool_output("
    assert lines[6] == "  [... repeated 3 times ...]"


def test_test_log_compression_collapses_noise_while_preserving_100_pct_of_errors_and_tracebacks() -> None:
    """`compress_tool_output` & `compress_test_logs` must drop spinners/bars without orphan repeat tags and handle incidental `@@` in pytest failures."""
    raw_test_log = (
        "\x1b[36m⠋ Running test suite...\x1b[0m\n"
        "\x1b[36m⠋ Running test suite...\x1b[0m\n"
        "\x1b[36m⠋ Running test suite...\x1b[0m\n"
        "\x1b[36m⠙ Running test suite...\x1b[0m\n"
        "10%... 25%... 50%... 75%... 90%\n"
        "  45%|████▌     | 45/100 [00:02<00:03, 18.00it/s]\n"
        "  45%|████▌     | 45/100 [00:02<00:03, 18.00it/s]\n"
        "............................ [ 50%]\n"
        "...................................\n"
        "INFO: worker heartbeat ok\n"
        "INFO: worker heartbeat ok\n"
        "INFO: worker heartbeat ok\n"
        "FAILED tests/test_scheduler.py::test_quota_gate - AssertionError: Gate expected False\n"
        "FAILED tests/test_scheduler.py::test_quota_gate - AssertionError: Gate expected False\n"
        "@@ -1,2 +1,2 @@\n"
        "E   AssertionError: diff mismatch at @@ -1,2 +1,2 @@\n"
        "Traceback (most recent call last):\n"
        '  File "/app/tests/test_scheduler.py", line 88, in test_quota_gate\n'
        "    assert gate_open is False\n"
        "AssertionError: Gate expected False\n"
        "ERROR: invocation failed with exit code 1\n"
    )

    for compressed in (compress_test_logs(raw_test_log), compress_tool_output(raw_test_log, enabled=True)):
        # 1. Noise and ANSI sequences removed or collapsed without leaving orphaned `[... repeated N times ...]` tags
        assert "\x1b" not in compressed
        assert "⠋ Running" not in compressed
        assert "⠙ Running" not in compressed
        assert "45%|████" not in compressed
        assert not compressed.lstrip().startswith("[... repeated")
        assert compressed.count("[... repeated") == 1
        assert "[progress: 90%]" in compressed
        assert "[... 28 passed [ 50%] ...]" in compressed
        assert "[... 35 progress dots ...]" in compressed
        assert "INFO: worker heartbeat ok\n  [... repeated 3 times ...]" in compressed

        # 2. 100% of error, failure, incidental `@@` pytest diff lines, and traceback lines preserved verbatim
        assert (
            "FAILED tests/test_scheduler.py::test_quota_gate - AssertionError: Gate expected False\n"
            "FAILED tests/test_scheduler.py::test_quota_gate - AssertionError: Gate expected False"
        ) in compressed
        assert "E   AssertionError: diff mismatch at @@ -1,2 +1,2 @@" in compressed
        assert "Traceback (most recent call last):" in compressed
        assert '  File "/app/tests/test_scheduler.py", line 88, in test_quota_gate' in compressed
        assert "    assert gate_open is False" in compressed
        assert "AssertionError: Gate expected False" in compressed
        assert "ERROR: invocation failed with exit code 1" in compressed



@pytest.mark.parametrize(
    ("headers", "expected_enabled"),
    [
        (None, True),
        ({}, True),
        ({"X-Token-Saver": "on"}, True),
        ({"X-Token-Saver": "off"}, False),
        ({"x-token-saver": "  OFF  "}, False),
        ({"X-TOKEN-SAVER": "false"}, False),
        ({"x-token-saver": "0"}, False),
    ],
)
def test_x_token_saver_off_header_and_disabled_flag_guarantee_byte_for_byte_identity(
    headers: dict[str, str] | None,
    expected_enabled: bool,
) -> None:
    """When `X-Token-Saver: off` or `enabled=False`, tool output must remain 100% byte-for-byte identical."""
    assert should_enable_token_saver(headers) is expected_enabled

    raw_uncompressed_payload = (
        "\x1b[31mdiff --git a/file.py b/file.py\x1b[0m   \r\n"
        "index 1111111..2222222 100644   \r\n"
        "--- a/file.py\r\n"
        "+++ b/file.py\r\n"
        "\r\n\r\n\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        "-old_value = 1   \r\n"
        "+new_value = 2   \r\n"
    )

    # Direct pipeline call with enabled=False
    assert compress_tool_output(raw_uncompressed_payload, enabled=False) == raw_uncompressed_payload

    # Message-level filtering with headers and multipart + string tool messages
    original_messages: list[dict[str, Any]] = [
        {"role": "user", "content": raw_uncompressed_payload},
        {"role": "tool", "tool_call_id": "call_str", "content": raw_uncompressed_payload},
        {
            "role": "tool",
            "tool_call_id": "call_parts",
            "content": [
                {"type": "text", "text": raw_uncompressed_payload},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]
    snapshot = copy.deepcopy(original_messages)

    result_msgs = sanitize_tool_messages(original_messages, enabled=True, headers=headers)

    # Caller input never mutated
    assert original_messages == snapshot
    # Non-tool message never touched even when enabled
    assert result_msgs[0]["content"] == raw_uncompressed_payload

    if not expected_enabled:
        assert result_msgs[1]["content"] == raw_uncompressed_payload
        assert result_msgs[2]["content"][0]["text"] == raw_uncompressed_payload
    else:
        assert "index 1111111..2222222" not in result_msgs[1]["content"]
        assert "index 1111111..2222222" not in result_msgs[2]["content"][0]["text"]
        assert result_msgs[2]["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
        }
