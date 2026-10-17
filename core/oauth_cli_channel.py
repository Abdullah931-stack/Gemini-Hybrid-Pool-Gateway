"""Production v1internal OAuth CLI channel, SSE stream demuxer, and protocol bridge.

Implements Level 3 (OAuth CLI Channel & Protocol Gateway) of the Gemini-Hybrid-Pool-Gateway (v3.2):
1. Production v1internal Request Envelope (ADR-04):
   Connects to ``https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse``
   with strict prohibition of ``"requestType": "agent"`` to avoid false 429 RESOURCE_EXHAUSTED errors.
2. CloudCode Header Shield:
   Applies official headers via ``build_v1internal_headers`` from ``core/auth_oauth_cli.py``.
3. Schema Sanitization & WAF System Prompt Shield (ADR-04):
   Neutralizes ESF/WAF triggers via ``sanitize_system_prompt_text`` and strips unsupported schema keys
   via ``sanitize_tools`` from ``core/schema_sanitizer.py``.
4. Multi-Turn Role Transformation & History Replay Consistency (ADR-05 & ADR-06):
   - Maps ``system`` / ``developer`` to ``systemInstruction.parts``.
   - Maps ``assistant`` to ``model``, ``user`` to ``user``, and ``tool`` to ``functionResponse``.
   - Re-injects synthetic ``_step: "done"`` into historical ``functionCall.args`` for zero-property tools.
   - Injects original ``thoughtSignature`` from ``LMDBStore`` or falls back to
     ``"skip_thought_signature_validator"``.
   - Nests tool output images inside ``functionResponse.parts`` as ``inlineData``.
   - Appends a lightweight ``{"role": "user", "parts": [{"text": "Continue."}]}`` turn if the sequence
     ends with a ``model`` turn (Post-Assembly Model-Turn Guard).
5. SSE Stream Unpacker & OpenAI Chunk Demuxer:
   - Streams ``thought`` parts as ``delta.reasoning_content``.
   - Streams standard text as ``delta.content``.
   - Streams ``functionCall`` as ``delta.tool_calls`` with ``_step`` stripped, saving newly emitted
     ``thoughtSignature`` to ``LMDBStore`` (table ``db_signatures``) immediately under ``tool_call_id``.
6. Dual Transport Support:
   Full HTTP/2 streaming execution via ``httpx.AsyncClient`` with optional support for
   ``AgySubprocessTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
import copy
import json
import logging
import os
import re
import time
from typing import Any
import uuid

import httpx

from config.settings import LMDBStore, OAuthAccount, get_lmdb_store
from core.auth_oauth_cli import (
    AgySubprocessTransport,
    build_v1internal_headers,
    ensure_valid_access_token,
    validate_production_endpoint,
)
from core.model_registry import inject_oauth_thinking_config, parse_model_and_effort
import core.schema_sanitizer as _schema_sanitizer
from core.schema_sanitizer import (
    apply_zero_required_fix,
    clean_json_schema,
    flatten_unions,
    reinject_step_to_args,
    sanitize_parameters_schema,
    sanitize_system_prompt_text,
    sanitize_tools,
    strip_step_from_args,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Constants & Production Endpoints
# ============================================================================

V1INTERNAL_STREAM_URL: str = (
    "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
)

SKIP_THOUGHT_SIGNATURE_VALIDATOR: str = "skip_thought_signature_validator"
DEFAULT_USER_AGENT: str = "antigravity"
CONTINUE_PROMPT_TEXT: str = "Continue."
DEFAULT_TIMEOUT_SECONDS: float = 60.0

_BASE64_DATA_URL_RE: re.Pattern[str] = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9\-\+\.]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    flags=re.DOTALL | re.IGNORECASE,
)

_RAW_BASE64_IMAGE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("iVBORw0KGgo", "image/png"),
    ("/9j/", "image/jpeg"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)

_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
}


# ============================================================================
# 2. Input Sanitization & Preparation Helpers
# ============================================================================


def _extract_base64_image_data(image_input: str) -> tuple[str, str] | None:
    """Parse a data:image URI or validated raw base64 image string, returning (mime_type, base64_data)."""
    if not isinstance(image_input, str):
        return None
    cleaned = image_input.strip()
    if not cleaned:
        return None

    match = _BASE64_DATA_URL_RE.match(cleaned)
    if match:
        compact_data = re.sub(r"\s+", "", match.group("data"))
        return match.group("mime").lower(), compact_data

    # Raw string (without data:image/): ONLY treat as base64 image if it starts with a
    # known image magic header, has valid length/padding, and decodes cleanly.
    if len(cleaned) >= 16 and len(cleaned) % 4 == 0:
        for magic_prefix, mime_type in _RAW_BASE64_IMAGE_PREFIXES:
            if cleaned.startswith(magic_prefix):
                try:
                    base64.b64decode(cleaned, validate=True)
                    return mime_type, cleaned
                except (binascii.Error, ValueError):
                    return None

    return None


def _parse_image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Extract an inlineData Gemini part from an OpenAI or Gemini image part dict."""
    if not isinstance(part, dict):
        return None

    # Already Gemini inlineData format
    if "inlineData" in part and isinstance(part["inlineData"], dict):
        return {"inlineData": copy.deepcopy(part["inlineData"])}
    if "inline_data" in part and isinstance(part["inline_data"], dict):
        in_data = part["inline_data"]
        return {
            "inlineData": {
                "mimeType": in_data.get("mime_type") or in_data.get("mimeType", "image/png"),
                "data": in_data.get("data", ""),
            }
        }

    # OpenAI format: {"type": "image_url", "image_url": {"url": "..."}}
    if part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
        url = str(part["image_url"].get("url", "")).strip()
        parsed = _extract_base64_image_data(url)
        if parsed:
            mime_type, b64_data = parsed
            return {"inlineData": {"mimeType": mime_type, "data": b64_data}}

    # Direct image type: {"type": "image", "image": "..."}
    if part.get("type") == "image":
        img_val = part.get("image") or (
            part.get("source", {}).get("data") if isinstance(part.get("source"), dict) else None
        )
        if isinstance(img_val, str):
            parsed = _extract_base64_image_data(img_val)
            if parsed:
                mime_type, b64_data = parsed
                return {"inlineData": {"mimeType": mime_type, "data": b64_data}}

    return None


def _sanitize_params_with_step_flag(
    raw_params: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Sanitize tool parameter schema and return (sanitized_schema, injected_step)."""
    meta_fn = getattr(_schema_sanitizer, "sanitize_parameters_schema_with_meta", None)
    if callable(meta_fn):
        return meta_fn(raw_params)  # type: ignore[no-any-return]

    params_dict = raw_params if isinstance(raw_params, dict) else {}
    cleaned = clean_json_schema(params_dict)
    flattened = flatten_unions(cleaned)
    return apply_zero_required_fix(flattened)


def sanitize_and_prepare_tools(
    tools: Sequence[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Sanitize tool schemas and format them for the Gemini v1internal envelope.

    Returns:
        A tuple of (gemini_tools, zero_property_tool_names) where:
        - gemini_tools is a list of [{"functionDeclarations": [...]}]
        - zero_property_tool_names is a set of tool names where Case-1 _step was injected.
    """
    if not tools:
        return [], set()

    function_declarations: list[dict[str, Any]] = []
    zero_property_tools: set[str] = set()

    for raw_tool in tools:
        if not isinstance(raw_tool, dict):
            continue

        if "function" in raw_tool and isinstance(raw_tool["function"], dict):
            decl = copy.deepcopy(raw_tool["function"])
            name = str(decl.get("name", "")).strip()
            sanitized_params, injected_step = _sanitize_params_with_step_flag(
                decl.get("parameters")
            )
            decl["parameters"] = sanitized_params
            if injected_step and name:
                zero_property_tools.add(name)
            function_declarations.append(decl)

        elif "functionDeclarations" in raw_tool and isinstance(
            raw_tool["functionDeclarations"], list
        ):
            for raw_sub_decl in raw_tool["functionDeclarations"]:
                if isinstance(raw_sub_decl, dict):
                    sub_decl = copy.deepcopy(raw_sub_decl)
                    sub_name = str(sub_decl.get("name", "")).strip()
                    sanitized_params, injected_step = _sanitize_params_with_step_flag(
                        sub_decl.get("parameters")
                    )
                    sub_decl["parameters"] = sanitized_params
                    if injected_step and sub_name:
                        zero_property_tools.add(sub_name)
                    function_declarations.append(sub_decl)

        elif "name" in raw_tool:
            decl = copy.deepcopy(raw_tool)
            name = str(decl.get("name", "")).strip()
            sanitized_params, injected_step = _sanitize_params_with_step_flag(
                decl.get("parameters")
            )
            decl["parameters"] = sanitized_params
            if injected_step and name:
                zero_property_tools.add(name)
            function_declarations.append(decl)

    if not function_declarations:
        return [], zero_property_tools

    gemini_tools = [{"functionDeclarations": function_declarations}]
    return gemini_tools, zero_property_tools


def extract_and_sanitize_system_instruction(
    messages: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Extract, WAF-sanitize, and format system and developer messages into systemInstruction.parts."""
    system_parts: list[dict[str, str]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).strip().lower()
        if role not in ("system", "developer"):
            continue

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            sanitized = sanitize_system_prompt_text(content)
            if sanitized:
                system_parts.append({"text": sanitized})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_val = str(part.get("text", "")).strip()
                    if text_val:
                        sanitized = sanitize_system_prompt_text(text_val)
                        if sanitized:
                            system_parts.append({"text": sanitized})
                elif isinstance(part, str) and part.strip():
                    sanitized = sanitize_system_prompt_text(part)
                    if sanitized:
                        system_parts.append({"text": sanitized})

    if not system_parts:
        return None

    return {"parts": system_parts}


# ============================================================================
# 3. Message Transformation & History Replay Assembly (ADR-05 & ADR-06)
# ============================================================================


def _parse_tool_call_arguments(raw_args: Any) -> dict[str, Any]:
    """Parse JSON string arguments or normalize dictionary arguments safely."""
    if isinstance(raw_args, dict):
        return copy.deepcopy(raw_args)
    if isinstance(raw_args, str):
        cleaned = raw_args.strip()
        if not cleaned:
            return {}
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            return {"_value": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"_raw": cleaned}
    return {}


def _extract_images_from_tool_dict(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract embedded base64 images from structured tool output dicts while preserving non-image fields."""
    cleaned_dict = copy.deepcopy(payload)
    images: list[dict[str, Any]] = []

    for key in ("inlineData", "inline_data", "image", "image_url", "b64_json", "data"):
        if key not in cleaned_dict:
            continue
        val = cleaned_dict[key]
        if key in ("inlineData", "inline_data") and isinstance(val, dict):
            mime = val.get("mimeType") or val.get("mime_type") or "image/png"
            data_str = val.get("data", "")
            if isinstance(data_str, str) and data_str.strip():
                parsed_b64 = _extract_base64_image_data(data_str)
                b64_payload = parsed_b64[1] if parsed_b64 else data_str.strip()
                eff_mime = parsed_b64[0] if parsed_b64 else str(mime)
                images.append({"inlineData": {"mimeType": eff_mime, "data": b64_payload}})
                cleaned_dict.pop(key, None)
        elif key == "image_url":
            if isinstance(val, dict) and isinstance(val.get("url"), str):
                parsed_img = _extract_base64_image_data(val["url"])
                if parsed_img:
                    images.append(
                        {"inlineData": {"mimeType": parsed_img[0], "data": parsed_img[1]}}
                    )
                    cleaned_dict.pop(key, None)
            elif isinstance(val, str):
                parsed_img = _extract_base64_image_data(val)
                if parsed_img:
                    images.append(
                        {"inlineData": {"mimeType": parsed_img[0], "data": parsed_img[1]}}
                    )
                    cleaned_dict.pop(key, None)
        elif isinstance(val, str):
            parsed_img = _extract_base64_image_data(val)
            if parsed_img:
                images.append(
                    {"inlineData": {"mimeType": parsed_img[0], "data": parsed_img[1]}}
                )
                cleaned_dict.pop(key, None)
        elif isinstance(val, dict):
            img_part = _parse_image_part(val) or _parse_image_part(
                {"type": "image", "image": val}
            )
            if img_part:
                images.append(img_part)
                cleaned_dict.pop(key, None)

    if images and not cleaned_dict:
        cleaned_dict = {"output": "[Image Output]"}

    return cleaned_dict, images


def _resolve_tool_response_payload(raw_content: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract response data and any nested inlineData image parts from tool message content.

    Returns:
        (response_payload_dict, nested_image_parts_list)
    """
    nested_image_parts: list[dict[str, Any]] = []

    if isinstance(raw_content, dict):
        return _extract_images_from_tool_dict(raw_content)

    if isinstance(raw_content, str):
        cleaned = raw_content.strip()
        parsed_img = _extract_base64_image_data(cleaned)
        if parsed_img:
            mime_type, b64_data = parsed_img
            nested_image_parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})
            return {"output": "[Image Output]"}, nested_image_parts

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return _extract_images_from_tool_dict(parsed)
            return {"output": parsed}, nested_image_parts
        except (json.JSONDecodeError, ValueError):
            return {"output": raw_content}, nested_image_parts

    if isinstance(raw_content, list):
        text_accum: list[str] = []
        for item in raw_content:
            if isinstance(item, dict):
                img_part = _parse_image_part(item)
                if img_part:
                    nested_image_parts.append(img_part)
                elif item.get("type") == "text" and "text" in item:
                    text_accum.append(str(item["text"]))
                else:
                    sub_dict, sub_imgs = _extract_images_from_tool_dict(item)
                    if sub_imgs:
                        nested_image_parts.extend(sub_imgs)
                    if sub_dict and sub_dict != {"output": "[Image Output]"}:
                        text_accum.append(json.dumps(sub_dict, ensure_ascii=False))
            elif isinstance(item, str):
                parsed_img = _extract_base64_image_data(item)
                if parsed_img:
                    nested_image_parts.append(
                        {"inlineData": {"mimeType": parsed_img[0], "data": parsed_img[1]}}
                    )
                else:
                    text_accum.append(item)

        output_str = "\n".join(text_accum) if text_accum else "[Multimodal Tool Output]"
        return {"output": output_str}, nested_image_parts

    return {"output": str(raw_content)}, nested_image_parts


def _is_compaction_trigger_item(item: Any) -> bool:
    """Return True if a message or content part represents a non-standard compaction_trigger."""
    if isinstance(item, Mapping):
        role = str(item.get("role", "")).strip().lower()
        item_type = str(item.get("type", "")).strip().lower()
        if (
            role == "compaction_trigger"
            or item_type == "compaction_trigger"
            or item.get("compaction_trigger") is True
            or "compaction_trigger" in item
        ):
            return True
    elif isinstance(item, str) and item.strip().lower() == "compaction_trigger":
        return True
    return False


def _convert_assistant_message_to_model_turn(
    message: dict[str, Any],
    *,
    zero_tool_names: set[str],
    tool_call_id_to_name: dict[str, str],
    lmdb_store: LMDBStore | None,
) -> dict[str, Any] | None:
    """Convert an assistant/model message into a Gemini v1internal 'model' content turn."""
    model_parts: list[dict[str, Any]] = []
    content = message.get("content")
    if content is None and isinstance(message.get("parts"), list):
        content = message.get("parts")

    if isinstance(content, str) and content.strip():
        if not _is_compaction_trigger_item(content):
            model_parts.append({"text": content})
    elif isinstance(content, list):
        for part in content:
            if _is_compaction_trigger_item(part):
                continue
            if isinstance(part, dict) and "text" in part:
                model_parts.append({"text": str(part["text"])})
            elif isinstance(part, str) and part.strip():
                model_parts.append({"text": part})

    # Check for tool calls
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            call_id = str(tc.get("id", "")).strip()
            func_info = tc.get("function", {}) if isinstance(tc.get("function"), dict) else tc
            func_name = str(func_info.get("name", "")).strip()
            if call_id and func_name:
                tool_call_id_to_name[call_id] = func_name

            args = _parse_tool_call_arguments(func_info.get("arguments", {}))

            # ADR-05: Reinject _step: "done" ONLY for Case-1 zero-property tools
            is_zero_property = func_name in zero_tool_names
            args = reinject_step_to_args(args, tool_had_zero_properties=is_zero_property)

            # ADR-06: Thought signature injection
            signature: str | None = None
            if lmdb_store and call_id:
                signature = lmdb_store.get_signature(call_id)
            if not signature:
                signature = SKIP_THOUGHT_SIGNATURE_VALIDATOR

            model_parts.append(
                {
                    "functionCall": {
                        "name": func_name,
                        "args": args,
                    },
                    "thoughtSignature": signature,
                }
            )

    # Single legacy function_call support
    single_fc = message.get("function_call")
    if isinstance(single_fc, dict):
        func_name = str(single_fc.get("name", "")).strip()
        args = _parse_tool_call_arguments(single_fc.get("arguments", {}))
        is_zero_property = func_name in zero_tool_names
        args = reinject_step_to_args(args, tool_had_zero_properties=is_zero_property)

        model_parts.append(
            {
                "functionCall": {
                    "name": func_name,
                    "args": args,
                },
                "thoughtSignature": SKIP_THOUGHT_SIGNATURE_VALIDATOR,
            }
        )

    if not model_parts:
        return None
    return {"role": "model", "parts": model_parts}


def convert_messages_to_v1internal_contents(
    messages: Sequence[dict[str, Any]],
    *,
    lmdb_store: LMDBStore | None = None,
    zero_property_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert OpenAI-style messages into Gemini v1internal contents list.

    Enforces:
    - Exclusion of ``system`` and ``developer`` messages (transferred to ``systemInstruction``).
    - Filtering of ``compaction_trigger`` messages/parts before the Post-Assembly Model-Turn Guard.
    - Mapping ``user`` -> ``user``, ``assistant`` -> ``model``, ``tool`` -> ``functionResponse``.
    - Coalescing consecutive ``role == "tool"`` turns into a single ``functionResponse`` turn.
    - Re-injecting ``_step: "done"`` into historical ``functionCall.args`` ONLY for zero-property tools.
    - Injecting ``thoughtSignature`` from LMDB or defaulting to ``skip_thought_signature_validator``.
    - Tool output image nesting inside ``functionResponse.parts`` as ``inlineData``.
    - Post-Assembly Model-Turn Guard: appends ``{"role": "user", "parts": [{"text": "Continue."}]}``
      if the sequence ends with a ``model`` turn.
    """
    zero_tool_names = set(zero_property_tool_names) if zero_property_tool_names else set()
    tool_call_id_to_name: dict[str, str] = {}
    contents: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict) or _is_compaction_trigger_item(message):
            continue

        raw_role = str(message.get("role", "")).strip().lower()

        # 1. Skip system/developer roles (placed in systemInstruction)
        if raw_role in ("system", "developer"):
            continue

        # 2. User Message
        if raw_role == "user":
            user_parts: list[dict[str, Any]] = []
            content = message.get("content")
            if content is None and isinstance(message.get("parts"), list):
                content = message.get("parts")

            if isinstance(content, str):
                if not _is_compaction_trigger_item(content):
                    user_parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if _is_compaction_trigger_item(part):
                        continue
                    if isinstance(part, dict):
                        img_part = _parse_image_part(part)
                        if img_part:
                            user_parts.append(img_part)
                        elif part.get("type") == "text" and "text" in part:
                            user_parts.append({"text": str(part["text"])})
                        elif "text" in part:
                            user_parts.append({"text": str(part["text"])})
                    elif isinstance(part, str):
                        user_parts.append({"text": part})
            elif content is not None and not _is_compaction_trigger_item(content):
                user_parts.append({"text": str(content)})

            if user_parts:
                contents.append({"role": "user", "parts": user_parts})

        # 3. Assistant / Model Message
        elif raw_role in ("assistant", "model"):
            model_turn = _convert_assistant_message_to_model_turn(
                message,
                zero_tool_names=zero_tool_names,
                tool_call_id_to_name=tool_call_id_to_name,
                lmdb_store=lmdb_store,
            )
            if model_turn is not None:
                contents.append(model_turn)

        # 4. Tool Output Message -> role: "functionResponse" (coalesced across consecutive tool turns)
        elif raw_role == "tool":
            call_id = str(message.get("tool_call_id", "")).strip()
            tool_name = str(message.get("name", "")).strip()
            if not tool_name and call_id in tool_call_id_to_name:
                tool_name = tool_call_id_to_name[call_id]
            if not tool_name:
                tool_name = "tool_response"

            payload, nested_images = _resolve_tool_response_payload(message.get("content"))
            func_response_obj: dict[str, Any] = {
                "name": tool_name,
                "response": payload,
            }

            # ADR-06: Tool output images MUST be nested inside functionResponse.parts
            if nested_images:
                func_response_obj["parts"] = nested_images

            new_part = {"functionResponse": func_response_obj}
            if contents and contents[-1].get("role") == "functionResponse":
                contents[-1].setdefault("parts", []).append(new_part)
            else:
                contents.append(
                    {
                        "role": "functionResponse",
                        "parts": [new_part],
                    }
                )

    # 5. Post-Assembly Model-Turn Guard:
    # If contents ends with a 'model' turn, append a lightweight user 'Continue.' message
    if contents and contents[-1].get("role") == "model":
        contents.append(
            {
                "role": "user",
                "parts": [{"text": CONTINUE_PROMPT_TEXT}],
            }
        )

    return contents


# ============================================================================
# 4. Production v1internal Request Envelope (ADR-04)
# ============================================================================


def _build_tool_config(tool_choice: Any) -> dict[str, Any] | None:
    """Translate OpenAI tool_choice into Gemini v1internal toolConfig.functionCallingConfig."""
    if tool_choice is None:
        return None

    if isinstance(tool_choice, str):
        mode = tool_choice.strip().lower()
        if mode == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
        if mode == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        if mode in ("required", "any"):
            return {"functionCallingConfig": {"mode": "ANY"}}
        return None

    if isinstance(tool_choice, Mapping):
        if "functionCallingConfig" in tool_choice:
            return copy.deepcopy(dict(tool_choice))
        tc_type = str(tool_choice.get("type", "")).strip().lower()
        if tc_type == "function" and isinstance(tool_choice.get("function"), Mapping):
            fn_name = str(tool_choice["function"].get("name", "")).strip()
            if fn_name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [fn_name],
                    }
                }
            return {"functionCallingConfig": {"mode": "ANY"}}
        if "name" in tool_choice:
            fn_name = str(tool_choice.get("name", "")).strip()
            if fn_name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [fn_name],
                    }
                }
        if "mode" in tool_choice:
            cfg: dict[str, Any] = {"mode": str(tool_choice["mode"]).strip().upper()}
            if "allowedFunctionNames" in tool_choice:
                cfg["allowedFunctionNames"] = list(tool_choice["allowedFunctionNames"])
            return {"functionCallingConfig": cfg}

    return None


def build_v1internal_envelope(
    contents: list[dict[str, Any]],
    model: str,
    *,
    project_id: str | None = None,
    system_instruction: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    generation_config: dict[str, Any] | None = None,
    request_id: str | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: str | Sequence[str] | None = None,
    response_format: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Construct the official Cloud Code v1internal request envelope.

    Strictly satisfies ADR-04:
    - Never injects or allows ``"requestType": "agent"`` in the payload.
    - Resolves reasoning effort into ``generationConfig.thinkingConfig``.
    - Embeds standard ``userAgent: "antigravity"`` and unique ``requestId``.
    """
    clean_model = model.strip() if model else "gemini-3.8-flash"
    spec = parse_model_and_effort(clean_model, reasoning_effort=reasoning_effort)
    base_model = spec.base_model

    req_id = request_id.strip() if request_id and request_id.strip() else f"req-{uuid.uuid4().hex}"
    proj_id = project_id.strip() if project_id and project_id.strip() else ""

    if "requestType" in kwargs:
        raise ValueError(
            "ADR-04 Violation: 'requestType' must NEVER be present in the v1internal envelope."
        )

    eff_gen_config = (
        inject_oauth_thinking_config(generation_config, effort=spec.reasoning_effort)
        if generation_config is not None
        else copy.deepcopy(spec.oauth_thinking_config)
    )
    # Ensure thinkingConfig is nested inside generationConfig
    if "thinkingConfig" not in eff_gen_config and isinstance(eff_gen_config, dict):
        eff_gen_config = {"thinkingConfig": eff_gen_config}

    if temperature is not None:
        eff_gen_config["temperature"] = float(temperature)
    if top_p is not None:
        eff_gen_config["topP"] = float(top_p)
    if max_tokens is not None:
        eff_gen_config["maxOutputTokens"] = int(max_tokens)
    if stop is not None:
        eff_gen_config["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)
    if response_format is not None and isinstance(response_format, Mapping):
        rf_type = str(response_format.get("type", "")).strip().lower()
        if rf_type == "json_object":
            eff_gen_config["responseMimeType"] = "application/json"
        elif rf_type == "json_schema":
            eff_gen_config["responseMimeType"] = "application/json"
            js_obj = response_format.get("json_schema")
            if isinstance(js_obj, Mapping) and isinstance(js_obj.get("schema"), Mapping):
                eff_gen_config["responseSchema"] = clean_json_schema(dict(js_obj["schema"]))

    req_body: dict[str, Any] = {
        "contents": copy.deepcopy(contents),
    }

    if system_instruction and system_instruction.get("parts"):
        req_body["systemInstruction"] = copy.deepcopy(system_instruction)

    if eff_gen_config:
        req_body["generationConfig"] = eff_gen_config

    if tools:
        req_body["tools"] = copy.deepcopy(tools)
        tool_cfg = _build_tool_config(tool_choice)
        if tool_cfg is not None:
            req_body["toolConfig"] = tool_cfg

    envelope: dict[str, Any] = {
        "project": proj_id,
        "model": base_model,
        "request": req_body,
        "userAgent": DEFAULT_USER_AGENT,
        "requestId": req_id,
    }

    # Strict ADR-04 Validation: Absolute ban on "requestType"
    if "requestType" in envelope or "requestType" in req_body:
        raise ValueError(
            "ADR-04 Violation: 'requestType' must NEVER be present in the v1internal envelope."
        )

    return envelope


build_v1internal_request_payload = build_v1internal_envelope


# ============================================================================
# 5. SSE Stream Decoding & OpenAI Chunk Transformation
# ============================================================================


def parse_v1internal_sse_line(raw_line: str) -> dict[str, Any] | None:
    """Parse a single SSE event line from v1internal, ignoring heartbeats and comments."""
    line = raw_line.strip()
    if not line or line.startswith(":") or line == "data: [DONE]":
        return None

    if line.startswith("data:"):
        payload_str = line[5:].strip()
        if not payload_str:
            return None
        try:
            parsed = json.loads(payload_str)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Non-JSON SSE payload received: %s (%s)", line, exc)
            return None

    return None


def transform_candidate_to_openai_chunks(
    candidate: dict[str, Any],
    *,
    model: str,
    request_id: str,
    lmdb_store: LMDBStore | None = None,
    tool_call_start_index: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Transform a Gemini candidate into OpenAI-compatible chat completion chunks.

    Returns:
        A tuple of (chunks_list, updated_tool_call_index).
    """
    chunks: list[dict[str, Any]] = []
    created_ts = int(time.time())
    chunk_id = f"chatcmpl-{request_id}"
    tool_idx = tool_call_start_index

    content_obj = candidate.get("content", {})
    parts = content_obj.get("parts", []) if isinstance(content_obj, dict) else []

    candidate_signature = candidate.get("thoughtSignature")

    for part in parts:
        if not isinstance(part, dict):
            continue

        # 1. Thought / Reasoning Part
        if part.get("thought") is True and "text" in part:
            reasoning_text = str(part.get("text", ""))
            if reasoning_text:
                chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "reasoning_content": reasoning_text,
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                chunks.append(chunk)
            continue

        # 2. Regular Text Part
        if "text" in part and not part.get("thought"):
            normal_text = str(part.get("text", ""))
            if normal_text:
                chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": normal_text,
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                chunks.append(chunk)

        # 3. Tool Call Part (functionCall)
        if "functionCall" in part and isinstance(part["functionCall"], dict):
            fc = part["functionCall"]
            func_name = str(fc.get("name", "")).strip()
            raw_args = fc.get("args", {})

            # Clean synthetic _step before presenting to client
            clean_args = strip_step_from_args(raw_args)
            tool_call_id = f"call_{uuid.uuid4().hex[:12]}"

            # Extract thoughtSignature and persist in LMDBStore db_signatures
            thought_sig = (
                part.get("thoughtSignature")
                or fc.get("thoughtSignature")
                or candidate_signature
            )
            if thought_sig and lmdb_store:
                try:
                    lmdb_store.put_signature(tool_call_id, str(thought_sig))
                except Exception as exc:
                    logger.warning("Failed to store thoughtSignature for %s: %s", tool_call_id, exc)

            args_str = json.dumps(clean_args, ensure_ascii=False)
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": tool_idx,
                                    "id": tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": func_name,
                                        "arguments": args_str,
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            chunks.append(chunk)
            tool_idx += 1

    # Check for finishReason
    raw_finish = candidate.get("finishReason")
    if raw_finish:
        finish_str = str(raw_finish).upper()
        mapped_finish = _FINISH_REASON_MAP.get(finish_str, "stop")
        if tool_idx > tool_call_start_index:
            mapped_finish = "tool_calls"

        finish_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": mapped_finish,
                }
            ],
        }
        chunks.append(finish_chunk)

    return chunks, tool_idx


def format_openai_sse_event(chunk: dict[str, Any]) -> str:
    """Format an OpenAI chunk dictionary as a valid SSE event string."""
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ============================================================================
# 6. OAuth CLI Channel & Transport Executors
# ============================================================================


class OAuthCLIChannel:
    """Production OAuth CLI channel managing v1internal streaming requests.

    Supports:
    - Direct HTTP/2 execution via ``httpx.AsyncClient``.
    - Subprocess streaming via ``AgySubprocessTransport``.
    - Automatic token refreshing and validation.
    - End-to-end WAF & Schema sanitization.
    - LMDBStore thoughtSignature persistence.
    """

    def __init__(
        self,
        *,
        endpoint_url: str = V1INTERNAL_STREAM_URL,
        lmdb_store: LMDBStore | None = None,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint_url: str = validate_production_endpoint(endpoint_url)
        self._lmdb_store: LMDBStore | None = lmdb_store
        self.default_timeout: float = default_timeout

    @property
    def lmdb_store(self) -> LMDBStore:
        """Resolve the active LMDBStore instance."""
        if self._lmdb_store is None:
            self._lmdb_store = get_lmdb_store()
        return self._lmdb_store

    async def execute_http_stream(
        self,
        envelope: dict[str, Any],
        headers: dict[str, str],
        *,
        model: str,
        request_id: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completion chunks via HTTP/2 POST to cloudcode-pa."""
        eff_timeout = timeout or self.default_timeout
        own_client = False
        client = http_client

        if client is None:
            client_kwargs: dict[str, Any] = {"http2": True, "timeout": eff_timeout}
            if proxy:
                client_kwargs["proxy"] = proxy
            client = httpx.AsyncClient(**client_kwargs)
            own_client = True

        tool_counter = 0
        try:
            async with client.stream(
                "POST",
                self.endpoint_url,
                headers=headers,
                json=envelope,
                timeout=eff_timeout,
            ) as response:
                if response.status_code != 200:
                    error_bytes = await response.aread()
                    error_msg = error_bytes.decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"CloudCode PA returned HTTP {response.status_code}: {error_msg}",
                        request=response.request,
                        response=response,
                    )

                async for line in response.aiter_lines():
                    event_dict = parse_v1internal_sse_line(line)
                    if not event_dict:
                        continue

                    # Cloud Code envelope wraps payload in event_dict.response or event_dict directly
                    response_obj = event_dict.get("response", event_dict)
                    candidates = response_obj.get("candidates", [])
                    if isinstance(candidates, list):
                        for candidate in candidates:
                            if isinstance(candidate, dict):
                                chunks, tool_counter = transform_candidate_to_openai_chunks(
                                    candidate,
                                    model=model,
                                    request_id=request_id,
                                    lmdb_store=self.lmdb_store,
                                    tool_call_start_index=tool_counter,
                                )
                                for chunk in chunks:
                                    yield chunk
        finally:
            if own_client and client is not None:
                await client.aclose()

    async def execute_subprocess_stream(
        self,
        envelope: dict[str, Any],
        *,
        model: str,
        request_id: str,
        transport: AgySubprocessTransport | None = None,
        timeout: float | None = None,
        access_token: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completion chunks via an active or ephemeral AgySubprocessTransport."""
        eff_timeout = timeout or self.default_timeout
        own_transport = False
        sub_transport = transport

        sub_env: dict[str, str] | None = None
        if access_token and access_token.strip():
            clean_tok = access_token.strip()
            sub_env = os.environ.copy()
            sub_env["CLOUDCODE_ACCESS_TOKEN"] = clean_tok
            sub_env["AGY_ACCESS_TOKEN"] = clean_tok

        if sub_transport is None:
            sub_transport = AgySubprocessTransport(env=sub_env)
            await sub_transport.start()
            own_transport = True
        elif not sub_transport.is_running:
            if sub_env is not None:
                merged = dict(sub_env)
                if sub_transport.env:
                    merged.update(sub_transport.env)
                sub_transport.env = merged
            await sub_transport.start()

        tool_counter = 0
        try:
            await sub_transport.send_json_line(envelope)
            while True:
                line_dict = await sub_transport.read_json_line(timeout=eff_timeout)
                if line_dict is None:
                    break
                if not line_dict:
                    continue

                response_obj = line_dict.get("response", line_dict)
                candidates = response_obj.get("candidates", [])
                if isinstance(candidates, list):
                    for candidate in candidates:
                        if isinstance(candidate, dict):
                            chunks, tool_counter = transform_candidate_to_openai_chunks(
                                candidate,
                                model=model,
                                request_id=request_id,
                                lmdb_store=self.lmdb_store,
                                tool_call_start_index=tool_counter,
                            )
                            for chunk in chunks:
                                yield chunk
        finally:
            if own_transport and sub_transport is not None:
                await sub_transport.close()

    async def stream_chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        account: OAuthAccount | None = None,
        access_token: str | None = None,
        project_id: str | None = None,
        model: str = "gemini-3.8-flash",
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        generation_config: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: str | Sequence[str] | None = None,
        response_format: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: AgySubprocessTransport | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
        headers: Mapping[str, str] | None = None,
        enable_token_saver: bool | None = None,
        track_concurrency: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Full pipeline: prepare, sanitize, assemble, and stream an OpenAI-compatible completion.

        Yields:
            Dictionaries representing OpenAI ``chat.completion.chunk`` objects.
        """
        eff_request_id = request_id or f"req-{uuid.uuid4().hex}"

        # 1. Resolve Access Token, Project ID, and Proxy
        eff_access_token = access_token
        eff_project_id = project_id
        eff_proxy = proxy
        ide_type = "IDE_UNSPECIFIED"

        if account is not None:
            eff_access_token = await ensure_valid_access_token(
                account,
                http_client=http_client,
                persist=True,
            )
            eff_project_id = eff_project_id or account.credentials.project_id
            ide_type = account.credentials.ide_type or "IDE_UNSPECIFIED"
            if eff_proxy is None and account.proxy:
                eff_proxy = account.proxy

        if not eff_access_token or not eff_access_token.strip():
            raise ValueError("An active access_token or OAuthAccount must be provided.")

        # 2. Sanitize and Prepare Tools
        gemini_tools, zero_tool_names = sanitize_and_prepare_tools(tools)

        # 3. Extract and Sanitize System Instructions
        system_instruction = extract_and_sanitize_system_instruction(messages)

        # 4. Convert Messages to Contents with ADR-05 & ADR-06 Guards
        contents = convert_messages_to_v1internal_contents(
            messages,
            lmdb_store=self.lmdb_store,
            zero_property_tool_names=zero_tool_names,
        )

        # 5. Build ADR-04 Compliant Envelope
        envelope = build_v1internal_envelope(
            contents=contents,
            model=model,
            project_id=eff_project_id,
            system_instruction=system_instruction,
            tools=gemini_tools,
            tool_choice=tool_choice,
            generation_config=generation_config,
            request_id=eff_request_id,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            response_format=response_format,
        )

        # 6. Build Official Header Shield
        v1_headers = build_v1internal_headers(eff_access_token, ide_type=ide_type)

        # 7. Dispatch to Selected Transport
        transport_mode = account.transport_mode if account else "http_v1internal"

        if transport_mode == "agy_subprocess" or transport is not None:
            async for chunk in self.execute_subprocess_stream(
                envelope,
                model=model,
                request_id=eff_request_id,
                transport=transport,
                timeout=timeout,
                access_token=eff_access_token,
            ):
                yield chunk
        else:
            async for chunk in self.execute_http_stream(
                envelope,
                headers=v1_headers,
                model=model,
                request_id=eff_request_id,
                http_client=http_client,
                timeout=timeout,
                proxy=eff_proxy,
            ):
                yield chunk


# Shared singleton channel instance
oauth_cli_channel = OAuthCLIChannel()
default_oauth_cli_channel = oauth_cli_channel


async def execute_v1internal_sse_stream(
    messages: Sequence[dict[str, Any]] | dict[str, Any],
    *,
    account: OAuthAccount | None = None,
    access_token: str | None = None,
    project_id: str | None = None,
    model: str = "gemini-3.8-flash",
    tools: Sequence[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    generation_config: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: str | Sequence[str] | None = None,
    response_format: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    transport: AgySubprocessTransport | None = None,
    timeout: float | None = None,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
    lmdb_store: LMDBStore | None = None,
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Module-level helper for executing a v1internal SSE stream."""
    channel = OAuthCLIChannel(lmdb_store=lmdb_store) if lmdb_store else oauth_cli_channel
    eff_proxy = proxy or (account.proxy if account and account.proxy else None)

    if isinstance(messages, dict) and "request" in messages and "model" in messages:
        req_id = request_id or str(messages.get("requestId", f"req-{uuid.uuid4().hex}"))
        eff_headers = headers
        if eff_headers is None:
            tok = access_token or (account.credentials.access_token if account else "")
            ide = account.credentials.ide_type if account else "IDE_UNSPECIFIED"
            eff_headers = build_v1internal_headers(tok, ide_type=ide)
        async for chunk in channel.execute_http_stream(
            messages,
            headers=eff_headers,
            model=model,
            request_id=req_id,
            http_client=http_client,
            timeout=timeout,
            proxy=eff_proxy,
        ):
            yield chunk
        return

    async for chunk in channel.stream_chat(
        messages=messages,  # type: ignore[arg-type]
        account=account,
        access_token=access_token,
        project_id=project_id,
        model=model,
        tools=tools,
        tool_choice=tool_choice,
        generation_config=generation_config,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        stop=stop,
        response_format=response_format,
        request_id=request_id,
        http_client=http_client,
        transport=transport,
        timeout=timeout,
        proxy=eff_proxy,
        headers=headers,
        **kwargs,
    ):
        yield chunk


async def execute_subprocess_stream(
    envelope: dict[str, Any],
    *,
    model: str = "gemini-3.8-flash",
    request_id: str | None = None,
    transport: AgySubprocessTransport | None = None,
    timeout: float | None = None,
    access_token: str | None = None,
    lmdb_store: LMDBStore | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Module-level helper for executing a subprocess stream with optional access_token injection."""
    channel = OAuthCLIChannel(lmdb_store=lmdb_store) if lmdb_store else oauth_cli_channel
    eff_req_id = request_id or str(envelope.get("requestId", f"req-{uuid.uuid4().hex}"))
    async for chunk in channel.execute_subprocess_stream(
        envelope,
        model=model,
        request_id=eff_req_id,
        transport=transport,
        timeout=timeout,
        access_token=access_token,
    ):
        yield chunk


