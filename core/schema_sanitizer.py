"""Stateless JSON Schema sanitizer, WAF system prompt shield, and zero-required tool fixer.

Implements Level 1 (Data & Protocol Plane) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. WAF System Prompt Shield (ADR-04): Neutralizes ``<system-conventions>`` tags and
   isolates ``RFC 2119`` directive lines in ``system`` and ``developer`` messages to
   prevent false ``429 RESOURCE_EXHAUSTED`` responses from Google Cloud ESF/WAF.
2. Recursive JSON Schema Cleaner: Strips all 20 unsupported JSON Schema meta-keys
   rejected by ``cloudcode-pa`` while preserving user-defined tool parameter names
   inside ``properties``.
3. Recursive Union & Type-Array Flattener: Converts ``type`` arrays and ``anyOf`` /
   ``oneOf`` unions into concrete Gemini-compatible types with ``nullable: True``.
4. Dual Zero-Required & History Replay Fix (ADR-05): Injects ``_step`` only for
   zero-property tools (with helpers to strip and reinject ``_step`` across turns)
   and promotes the first optional property for tools with non-empty ``properties``
   but empty ``required``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Exact 20 JSON Schema meta-keys rejected by cloudcode-pa (v1internal)
UNSUPPORTED_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
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
)

_SYSTEM_CONVENTIONS_TAG_RE: re.Pattern[str] = re.compile(
    r"<\s*(/?)\s*system-conventions\b([^>]*)>",
    flags=re.IGNORECASE,
)
_OPEN_CONVENTIONS_ADJACENT_RE: re.Pattern[str] = re.compile(
    r"(<\s*conventions\b[^>]*>)[ \t]*\n?[ \t]*(?=\S)",
    flags=re.IGNORECASE,
)
_CLOSE_CONVENTIONS_ADJACENT_RE: re.Pattern[str] = re.compile(
    r"(?<=\S)[ \t]*\n?[ \t]*(<\s*/\s*conventions\s*>)",
    flags=re.IGNORECASE,
)
_INLINE_RFC2119_BEFORE_RE: re.Pattern[str] = re.compile(
    r"(>|[:.;!])[ \t]*(RFC[ \t]*2119\b)",
    flags=re.IGNORECASE,
)
_RFC2119_LINE_ISOLATION_RE: re.Pattern[str] = re.compile(
    r"[ \t]*\n?[ \t]*(^[^\n]*\bRFC[ \t]*2119\b[^\n]*$)[ \t]*\n?[ \t]*",
    flags=re.IGNORECASE | re.MULTILINE,
)
_EXCESS_NEWLINES_RE: re.Pattern[str] = re.compile(r"\n{3,}")

_WAF_TARGET_ROLES: frozenset[str] = frozenset({"system", "developer"})
_OPTIONAL_DEFAULT_SUFFIX: str = ' (Optional, default: "")'
_STEP_PROPERTY_NAME: str = "_step"
_STEP_REPLAY_VALUE: str = "done"


# ============================================================================
# 1. WAF System Prompt Shield (ADR-04)
# ============================================================================


def sanitize_system_prompt_text(text: str) -> str:
    """Neutralize ESF/WAF signatures in system prompt text while preserving semantics.

    Replaces ``<system-conventions>`` / ``</system-conventions>`` with
    ``<conventions>`` / ``</conventions>`` and isolates any ``RFC 2119`` clause
    from the opening tag and surrounding text with blank lines.
    """
    if not text or not isinstance(text, str):
        return text

    sanitized = _SYSTEM_CONVENTIONS_TAG_RE.sub(r"<\1conventions\2>", text)
    sanitized = _OPEN_CONVENTIONS_ADJACENT_RE.sub(r"\1\n\n", sanitized)
    sanitized = _CLOSE_CONVENTIONS_ADJACENT_RE.sub(r"\n\n\1", sanitized)
    sanitized = _INLINE_RFC2119_BEFORE_RE.sub(r"\1\n\n\2", sanitized)
    if re.search(r"\bRFC[ \t]*2119\b", sanitized, flags=re.IGNORECASE):
        sanitized = _RFC2119_LINE_ISOLATION_RE.sub(r"\n\n\1\n\n", sanitized)
        sanitized = _EXCESS_NEWLINES_RE.sub("\n\n", sanitized)
    return sanitized.strip("\n") if not text.startswith("\n") else sanitized


def _sanitize_part_dict(part: dict[str, Any]) -> dict[str, Any]:
    """Sanitize ``text`` and ``content`` fields inside a single message part dict."""
    part_copy = copy.deepcopy(part)
    if isinstance(part_copy.get("text"), str):
        part_copy["text"] = sanitize_system_prompt_text(part_copy["text"])
    if isinstance(part_copy.get("content"), str):
        part_copy["content"] = sanitize_system_prompt_text(part_copy["content"])
    return part_copy


def _sanitize_message_content(content: Any) -> Any:
    """Sanitize either a string content payload or a list of content part dicts."""
    if isinstance(content, str):
        return sanitize_system_prompt_text(content)
    if not isinstance(content, list):
        return copy.deepcopy(content)

    sanitized_parts: list[Any] = []
    for part in content:
        if isinstance(part, dict):
            sanitized_parts.append(_sanitize_part_dict(part))
        elif isinstance(part, str):
            sanitized_parts.append(sanitize_system_prompt_text(part))
        else:
            sanitized_parts.append(copy.deepcopy(part))
    return sanitized_parts


def sanitize_messages_waf(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a new message list with WAF-shielded ``system`` and ``developer`` prompts."""
    if not messages:
        return []

    sanitized_messages: list[dict[str, Any]] = []
    for message in messages:
        msg_copy = copy.deepcopy(message)
        role = str(msg_copy.get("role", "")).strip().lower()
        if role in _WAF_TARGET_ROLES and "content" in msg_copy:
            msg_copy["content"] = _sanitize_message_content(msg_copy["content"])
        sanitized_messages.append(msg_copy)
    return sanitized_messages


sanitize_system_prompts = sanitize_messages_waf


# ============================================================================
# 2. Recursive JSON Schema Cleaner (20 Unsupported Keys)
# ============================================================================


def _clean_properties_map(properties: dict[str, Any]) -> dict[str, Any]:
    """Preserve user-defined parameter names in ``properties`` while cleaning their schemas."""
    cleaned_props: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        if isinstance(prop_schema, dict):
            cleaned_props[prop_name] = clean_json_schema(prop_schema)
        else:
            cleaned_props[prop_name] = copy.deepcopy(prop_schema)
    return cleaned_props


def _clean_schema_list(items: list[Any]) -> list[Any]:
    """Recursively clean all dictionary elements inside a schema list."""
    return [
        clean_json_schema(element) if isinstance(element, dict) else copy.deepcopy(element)
        for element in items
    ]


def clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove the 20 unsupported ``cloudcode-pa`` meta-keys from ``schema``.

    Never deletes parameter names inside a ``properties`` mapping even if a parameter
    is named after a restricted keyword (such as ``format`` or ``pattern``).
    """
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    cleaned: dict[str, Any] = {}
    for key, val in schema.items():
        if key in UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(val, dict):
            cleaned[key] = _clean_properties_map(val)
        elif isinstance(val, dict):
            cleaned[key] = clean_json_schema(val)
        elif isinstance(val, list):
            cleaned[key] = _clean_schema_list(val)
        else:
            cleaned[key] = copy.deepcopy(val)
    return cleaned


# ============================================================================
# 3. Recursive Union & Type-Array Flattener (`anyOf`, `oneOf`, `type: [...]`)
# ============================================================================


def _flatten_type_list(schema: dict[str, Any]) -> None:
    """Convert ``type: [...]`` in ``schema`` to a concrete scalar type plus ``nullable``."""
    raw_types = schema.get("type")
    if not isinstance(raw_types, list):
        return

    has_null = any(t == "null" or t is None for t in raw_types)
    non_null_types = [
        t for t in raw_types if isinstance(t, str) and t.strip() and t != "null"
    ]
    if non_null_types:
        schema["type"] = non_null_types[0]
        if has_null:
            schema["nullable"] = True
    else:
        schema["type"] = "string"
        schema["nullable"] = True


def _is_null_branch(branch: dict[str, Any]) -> bool:
    """Return True if ``branch`` represents a JSON Schema ``null`` type variant."""
    branch_type = branch.get("type")
    if branch_type == "null" or branch_type == ["null"]:
        return True
    return branch_type is None and "properties" not in branch and not branch


def _merge_branch_descriptions(
    parent_description: str | None,
    branches: list[dict[str, Any]],
) -> str | None:
    """Combine distinct descriptions from ``parent_description`` and union ``branches``."""
    collected: list[str] = []
    if isinstance(parent_description, str) and parent_description.strip():
        collected.append(parent_description.strip())
    for branch in branches:
        desc = branch.get("description")
        if isinstance(desc, str) and desc.strip() and desc.strip() not in collected:
            collected.append(desc.strip())
    return " | ".join(collected) if collected else None


def _collect_merged_properties(
    existing_props: Any,
    branches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collect unified ``properties`` across all object union branches."""
    merged_props: dict[str, Any] = (
        copy.deepcopy(existing_props) if isinstance(existing_props, dict) else {}
    )
    for branch in branches:
        branch_props = branch.get("properties")
        if isinstance(branch_props, dict):
            for prop_name, prop_def in branch_props.items():
                if prop_name not in merged_props:
                    merged_props[prop_name] = prop_def
    return merged_props


def _merge_object_branches(
    schema: dict[str, Any],
    non_null_branches: list[dict[str, Any]],
) -> None:
    """Merge multiple object union branches into a unified ``type: "object"`` schema."""
    merged_props = _collect_merged_properties(schema.get("properties"), non_null_branches)
    branch_required_sets = [
        set(branch["required"])
        for branch in non_null_branches
        if isinstance(branch.get("required"), list) and branch["required"]
    ]
    schema["type"] = "object"
    schema["properties"] = merged_props
    if branch_required_sets and "required" not in schema:
        common_req = set.intersection(*branch_required_sets)
        if common_req:
            schema["required"] = [k for k in merged_props if k in common_req]


def _apply_union_branches(
    schema: dict[str, Any],
    raw_branches: list[dict[str, Any]],
) -> None:
    """Collapse union ``raw_branches`` into ``schema``."""
    non_null_raw = [b for b in raw_branches if not _is_null_branch(b)]
    non_null_branches = [flatten_unions(b) for b in non_null_raw]
    has_null = len(non_null_raw) < len(raw_branches) or any(
        b.get("nullable") is True for b in non_null_branches
    )
    merged_desc = _merge_branch_descriptions(schema.get("description"), raw_branches)

    if not non_null_branches:
        schema.setdefault("type", "string")
        schema["nullable"] = True
    elif all(b.get("type") == "object" or "properties" in b for b in non_null_branches):
        _merge_object_branches(schema, non_null_branches)
    else:
        for key, val in copy.deepcopy(non_null_branches[0]).items():
            if not (key == "description" and "description" in schema):
                schema[key] = val

    if has_null:
        schema["nullable"] = True
    if merged_desc:
        schema["description"] = merged_desc


def flatten_unions(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively flatten ``type`` lists and ``anyOf`` / ``oneOf`` unions in ``schema``."""
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    flattened_schema = clean_json_schema(schema)
    if isinstance(flattened_schema.get("properties"), dict):
        flattened_schema["properties"] = {
            k: flatten_unions(v) if isinstance(v, dict) else v
            for k, v in flattened_schema["properties"].items()
        }
    if isinstance(flattened_schema.get("items"), dict):
        flattened_schema["items"] = flatten_unions(flattened_schema["items"])
    elif isinstance(flattened_schema.get("items"), list):
        flattened_schema["items"] = [
            flatten_unions(el) if isinstance(el, dict) else el
            for el in flattened_schema["items"]
        ]

    _flatten_type_list(flattened_schema)
    for union_key in ("anyOf", "oneOf"):
        raw_branches = flattened_schema.pop(union_key, None)
        if isinstance(raw_branches, list) and raw_branches:
            dict_branches = [b for b in raw_branches if isinstance(b, dict)]
            _apply_union_branches(flattened_schema, dict_branches)
            _flatten_type_list(flattened_schema)

    return clean_json_schema(flattened_schema)


# ============================================================================
# 4. Dual Zero-Required & History Replay Fix (ADR-05)
# ============================================================================


def _promote_first_optional_property(params: dict[str, Any]) -> None:
    """Promote the first property in ``params["properties"]`` to ``required`` with nullable default."""
    properties: dict[str, Any] = params["properties"]
    first_prop_name = next(iter(properties))
    raw_prop_schema = properties.get(first_prop_name)
    prop_schema: dict[str, Any] = (
        copy.deepcopy(raw_prop_schema) if isinstance(raw_prop_schema, dict) else {"type": "string"}
    )

    prop_schema["nullable"] = True
    existing_desc = str(prop_schema.get("description", "")).strip()
    if not existing_desc:
        prop_schema["description"] = 'Optional, default: ""'
    elif '""' not in existing_desc and "default:" not in existing_desc.lower():
        prop_schema["description"] = f"{existing_desc}{_OPTIONAL_DEFAULT_SUFFIX}"

    properties[first_prop_name] = prop_schema
    params["required"] = [first_prop_name]


def apply_zero_required_fix(parameters: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Ensure ``parameters`` has a non-empty ``required`` list to prevent post-thought stalls.

    - Case 1 (no properties): Injects ``_step`` property and sets ``required = ["_step"]``.
      Returns ``(sanitized_parameters, True)``.
    - Case 2 (optional properties exist, ``required`` empty/missing): Promotes the first
      existing property to ``required`` with ``nullable: True`` and default ``""`` hint.
      Returns ``(sanitized_parameters, False)``.
    """
    params = copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
    params.setdefault("type", "object")

    required = params.get("required")
    if isinstance(required, list) and len(required) > 0:
        return params, False

    properties = params.get("properties")
    if not isinstance(properties, dict) or len(properties) == 0:
        params["type"] = "object"
        params["properties"] = {
            _STEP_PROPERTY_NAME: {
                "type": "string",
                "description": "Internal step tracker",
            }
        }
        params["required"] = [_STEP_PROPERTY_NAME]
        return params, True

    _promote_first_optional_property(params)
    return params, False


def strip_step_from_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Remove the synthetic ``_step`` key from ``functionCall.args`` before returning to client."""
    if not isinstance(args, dict):
        return {}
    cleaned_args = copy.deepcopy(args)
    cleaned_args.pop(_STEP_PROPERTY_NAME, None)
    return cleaned_args


def reinject_step_to_args(
    args: dict[str, Any] | None,
    *,
    tool_had_zero_properties: bool = True,
) -> dict[str, Any]:
    """Re-inject ``{"_step": "done"}`` into historical ``functionCall.args`` for replay consistency.

    Note: ``tool_had_zero_properties`` is required by the Level 1/Level 3 architectural contract
    so callers can selectively re-hydrate Case 1 zero-property tools.
    """
    rehydrated = copy.deepcopy(args) if isinstance(args, dict) else {}
    if tool_had_zero_properties and _STEP_PROPERTY_NAME not in rehydrated:
        rehydrated[_STEP_PROPERTY_NAME] = _STEP_REPLAY_VALUE
    return rehydrated


# ============================================================================
# 5. End-to-End Tool Sanitization Interfaces
# ============================================================================


def sanitize_parameters_schema(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Run ``clean_json_schema``, ``flatten_unions``, and ``apply_zero_required_fix`` on ``parameters``."""
    raw_params = parameters if isinstance(parameters, dict) else {}
    cleaned = clean_json_schema(raw_params)
    flattened = flatten_unions(cleaned)
    fixed, _ = apply_zero_required_fix(flattened)
    return fixed


def sanitize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single tool definition in either OpenAI or Gemini ``functionDeclarations`` format."""
    if not isinstance(tool, dict):
        return copy.deepcopy(tool)

    tool_copy = copy.deepcopy(tool)

    if isinstance(tool_copy.get("function"), dict):
        func_def = tool_copy["function"]
        func_def["parameters"] = sanitize_parameters_schema(func_def.get("parameters"))
        return tool_copy

    if isinstance(tool_copy.get("functionDeclarations"), list):
        tool_copy["functionDeclarations"] = [
            sanitize_tool(decl) for decl in tool_copy["functionDeclarations"]
        ]
        return tool_copy

    if "name" in tool_copy or "parameters" in tool_copy:
        tool_copy["parameters"] = sanitize_parameters_schema(tool_copy.get("parameters"))

    return tool_copy


def sanitize_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Sanitize a list of tool definitions without mutating the input list."""
    if not tools:
        return []
    return [sanitize_tool(tool) for tool in tools]
