"""Deterministic, stateless, lossless tool-output compressor (RTK-Lite Lossless Filter).

Implements Level 1 of the Gemini-Hybrid-Pool-Gateway (v3.2) Data & Protocol Plane:
- Strips terminal ANSI escape sequences without altering semantic text.
- Normalizes line endings, trailing whitespace, and consecutive blank line runs.
- Optimizes ``git diff`` outputs by removing non-semantic ``index <hash>..<hash>``
  metadata lines while preserving file paths, hunk headers (``@@ ... @@``), and
  all code addition/deletion/context lines intact without corrupting hunk line counts.
- Compresses ``grep``, ``find``, and ``ls -l`` outputs by collapsing multi-space
  column padding in ``ls -l``, stripping redundant leading ``./`` prefixes in
  ``find`` paths, and deduplicating consecutive identical output lines.
- Compresses repetitive test logs and progress bars (dot-runs, percentage progress
  updates, spinner frames, and consecutive identical non-error log lines) while
  strictly preserving all error/exception/traceback lines verbatim.
- Supports per-request opt-out via the ``X-Token-Saver: off`` HTTP header.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

# ============================================================================
# Compiled Regular Expressions (Module-Level Constants)
# ============================================================================

_ANSI_ESCAPE_RE: re.Pattern[str] = re.compile(
    r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][ab012]|[>=<])"
)

_DISABLED_HEADER_VALUES: frozenset[str] = frozenset(
    {"off", "false", "0", "disabled", "no"}
)

# Authentic git/unified diff detection (requires 'diff --git ' or '--- a/...' followed by '+++ b/...')
_GIT_DIFF_HEADER_RE: re.Pattern[str] = re.compile(
    r"(?:^diff --git a/.+ b/.+|^--- (?:a/|\S+).*\n\+\+\+ (?:b/|\S+))",
    re.MULTILINE,
)
_GIT_INDEX_LINE_RE: re.Pattern[str] = re.compile(
    r"^index [0-9a-fA-F]{4,64}\.\.[0-9a-fA-F]{4,64}(?: \d{6})?$"
)
_GIT_META_HEADER_PREFIXES: tuple[str, ...] = (
    "diff --git ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "copy from ",
    "copy to ",
    "rename from ",
    "rename to ",
    "similarity index ",
    "dissimilarity index ",
    "--- ",
    "+++ ",
    "Binary files ",
)

# ls -l line pattern: permissions (10-11 chars), link count, owner, group, size, date/time, name
_LS_LONG_LINE_RE: re.Pattern[str] = re.compile(
    r"^([bcdlsp-][rwxstST-]{9}[+@.]?)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+(?:,\s*\d+)?)\s+"
    r"([A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+)$"
)
_LS_TOTAL_LINE_RE: re.Pattern[str] = re.compile(r"^total\s+\d+\s*$")

_FIND_DOT_SLASH_LINE_RE: re.Pattern[str] = re.compile(
    r"^\./([A-Za-z0-9_.\-/]+(?:\.[A-Za-z0-9_.\-]+)?)$"
)
_GREP_DOT_SLASH_LINE_RE: re.Pattern[str] = re.compile(
    r"^\./([A-Za-z0-9_.\-/]+:\d+[:\-].*)$"
)

# Progress bars & test dot-runs
_DOT_PROGRESS_LINE_RE: re.Pattern[str] = re.compile(
    r"^[.FEsxX]{15,}(?:\s*\[\s*\d+%\s*\])?$"
)
_PURE_DOTS_LINE_RE: re.Pattern[str] = re.compile(r"^\.{5,}(?:\s+\d+%?)?$")
_INLINE_PERCENT_PROGRESS_RE: re.Pattern[str] = re.compile(
    r"^(?:\s*\d{1,3}%\s*(?:\.{2,}|[|#=\-\s>]+)\s*){2,}\d{1,3}%?.*$"
)
_BAR_PROGRESS_LINE_RE: re.Pattern[str] = re.compile(
    r"^\s*\d{1,3}%\|[█▉▊▋▌▍▎▏#=\-\s]+\|\s*\d+/\d+.*$"
)
_SPINNER_FRAME_LINE_RE: re.Pattern[str] = re.compile(
    r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/\\\-]\s+(?:Loading|Running|Building|Installing|Downloading|Waiting|Processing)\b.*$",
    re.IGNORECASE,
)

# Error/exception keywords that must never be collapsed or dropped
_ERROR_LINE_RE: re.Pattern[str] = re.compile(
    r"(?:Traceback \(most recent call last\)|"
    r"\b(?:ERROR|FAIL|FAILED|FATAL|CRITICAL|PANIC|AssertionError|Exception|"
    r"TypeError|ValueError|KeyError|RuntimeError|AttributeError|ImportError|"
    r"IndexError|ZeroDivisionError|FileNotFoundError|PermissionError|SyntaxError)\b|"
    r"^\s*File \".*\", line \d+|"
    r"^E\s+)",
    re.MULTILINE,
)

_REPEAT_TAG_RE: re.Pattern[str] = re.compile(
    r"^\s*\[\.\.\. repeated (\d+) times \.\.\.\]\s*$"
)


# ============================================================================
# Internal Line-Level Helpers
# ============================================================================


def _clean_line_endings_and_ansi(text: str) -> str:
    """Strip ANSI escape codes and normalize CRLF/CR line endings to LF."""
    return strip_ansi_codes(text).replace("\r\n", "\n").replace("\r", "\n")


def _is_git_diff_text(text: str) -> bool:
    """Return True if ``text`` contains authentic ``git diff`` or unified diff headers."""
    return bool(_GIT_DIFF_HEADER_RE.search(text))


def _is_protected_error_line(line: str) -> bool:
    """Return True if ``line`` is part of an error, failure, or stack traceback."""
    return bool(_ERROR_LINE_RE.search(line))


def _collapse_blank_line_runs(lines: list[str]) -> list[str]:
    """Collapse consecutive blank lines to at most one and trim outer blank padding."""
    collapsed: list[str] = []
    previous_blank = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            if not previous_blank:
                collapsed.append("")
                previous_blank = True
        else:
            collapsed.append(line)
            previous_blank = False

    while len(collapsed) > 1 and collapsed[0] == "":
        collapsed.pop(0)
    return collapsed


def _format_git_diff_line(
    raw_line: str,
    in_hunk: bool,
) -> tuple[str | None, bool, bool]:
    """Process a single git diff line, returning ``(formatted_line, in_hunk, is_hunk_body)``."""
    stripped = raw_line.rstrip()

    if _GIT_INDEX_LINE_RE.match(stripped):
        return None, False, False

    if stripped.startswith("@@ ") and " @@" in stripped:
        return stripped, True, False

    if stripped.startswith(_GIT_META_HEADER_PREFIXES):
        return stripped, False, False

    if in_hunk:
        if raw_line.startswith(("+", "-", " ", "\\")):
            # Preserve leading prefix marker while stripping trailing horizontal whitespace
            return raw_line.rstrip(), True, True
        if stripped == "":
            # Unprefixed empty context line inside a diff hunk must be preserved for line counts
            return "", True, True
        return stripped, False, False

    return stripped, False, False


def _normalize_single_grep_find_ls_line(raw_line: str) -> str:
    """Normalize a single line from ``ls -l``, ``find``, or ``grep`` output."""
    line = raw_line.rstrip()
    if not line:
        return ""

    ls_match = _LS_LONG_LINE_RE.match(line)
    if ls_match:
        perms, links, owner, group, size, date_str, name = ls_match.groups()
        compact_date = " ".join(date_str.split())
        compact_size = size.replace(" ", "")
        return f"{perms} {links} {owner} {group} {compact_size} {compact_date} {name}"

    if _LS_TOTAL_LINE_RE.match(line):
        return " ".join(line.split())

    find_match = _FIND_DOT_SLASH_LINE_RE.match(line)
    if find_match:
        return find_match.group(1)

    grep_match = _GREP_DOT_SLASH_LINE_RE.match(line)
    if grep_match:
        return grep_match.group(1)

    return line


def _normalize_grep_find_ls_lines(lines: list[str]) -> list[str]:
    """Normalize ``ls -l`` column spacing and ``./`` path prefixes across ``lines``."""
    return [_normalize_single_grep_find_ls_line(line) for line in lines]


def _transform_progress_or_spinner_line(line: str) -> str | None:
    """Transform or drop a single progress bar, spinner frame, or dot-run line."""
    if _SPINNER_FRAME_LINE_RE.match(line):
        return None

    if _PURE_DOTS_LINE_RE.match(line):
        return f"[... {line.count('.')} progress dots ...]"

    if _DOT_PROGRESS_LINE_RE.match(line) and "F" not in line and "E" not in line:
        pct_match = re.search(r"\[\s*\d+%\s*\]", line)
        suffix = f" {pct_match.group(0)}" if pct_match else ""
        return f"[... {line.count('.')} passed{suffix} ...]"

    if _INLINE_PERCENT_PROGRESS_RE.match(line):
        percentages = re.findall(r"\d{1,3}%", line)
        last_pct = percentages[-1] if percentages else "100%"
        return f"[progress: {last_pct}]"

    if _BAR_PROGRESS_LINE_RE.match(line) and not line.lstrip().startswith("100%"):
        return None

    return line


def _filter_test_log_lines(lines: list[str]) -> list[str]:
    """Filter progress noise from test log lines while keeping tracebacks/errors intact."""
    filtered_lines: list[str] = []
    in_traceback = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if "Traceback (most recent call last):" in line:
            in_traceback = True

        if in_traceback or _is_protected_error_line(line):
            filtered_lines.append(line)
            if in_traceback and not line:
                in_traceback = False
            continue

        transformed = _transform_progress_or_spinner_line(line)
        if transformed is not None:
            filtered_lines.append(transformed)

    return filtered_lines


def _count_consecutive_repeats(lines: list[str], start_idx: int) -> tuple[int, int]:
    """Return ``(repeat_count, next_index)`` for identical lines starting at ``start_idx``."""
    target = lines[start_idx]
    repeat_count = 1
    cursor = start_idx + 1
    total = len(lines)

    while cursor < total:
        candidate = lines[cursor]
        if candidate == target:
            repeat_count += 1
            cursor += 1
            continue
        tag_match = _REPEAT_TAG_RE.match(candidate)
        if tag_match:
            repeat_count += max(0, int(tag_match.group(1)) - 1)
            cursor += 1
            continue
        break

    return repeat_count, cursor


def _collapse_consecutive_identical_lines(lines: list[str]) -> list[str]:
    """Collapse consecutive identical non-empty lines while protecting error/traceback lines."""
    if not lines:
        return []

    collapsed_lines: list[str] = []
    idx = 0
    total = len(lines)

    while idx < total:
        current = lines[idx]
        if _REPEAT_TAG_RE.match(current):
            # Drop orphaned repeat tags whose preceding line was filtered out
            idx += 1
            continue

        if not current or _is_protected_error_line(current):
            collapsed_lines.append(current)
            idx += 1
            continue

        repeat_count, next_idx = _count_consecutive_repeats(lines, idx)
        collapsed_lines.append(current)
        if repeat_count > 1:
            collapsed_lines.append(f"  [... repeated {repeat_count} times ...]")
        idx = next_idx

    return collapsed_lines


def _sanitize_content_parts(
    parts: list[Any],
    *,
    should_compress: bool,
) -> list[Any]:
    """Copy a multipart message content list, compressing text parts when ``should_compress`` is True."""
    parts_copy: list[Any] = []
    for part in parts:
        if not isinstance(part, dict):
            parts_copy.append(part)
            continue
        part_dict = dict(part)
        if (
            should_compress
            and part_dict.get("type") in ("text", None)
            and isinstance(part_dict.get("text"), str)
        ):
            part_dict["text"] = compress_tool_output(part_dict["text"], enabled=True)
        parts_copy.append(part_dict)
    return parts_copy


def _sanitize_single_message(
    message: dict[str, Any],
    *,
    effective_enabled: bool,
) -> dict[str, Any]:
    """Return a shallow-safe copy of ``message`` with tool output compressed if enabled."""
    msg_copy: dict[str, Any] = dict(message)
    is_tool_role = msg_copy.get("role") == "tool"
    should_compress = effective_enabled and is_tool_role
    raw_content = msg_copy.get("content")

    if isinstance(raw_content, list):
        msg_copy["content"] = _sanitize_content_parts(
            raw_content, should_compress=should_compress
        )
    elif should_compress and isinstance(raw_content, str):
        msg_copy["content"] = compress_tool_output(raw_content, enabled=True)

    return msg_copy


# ============================================================================
# Public API Functions
# ============================================================================


def should_enable_token_saver(headers: Mapping[str, str] | None = None) -> bool:
    """Inspect request headers to determine whether the Token Saver filter is enabled.

    Checks the ``X-Token-Saver`` header case-insensitively. Returns ``False`` if its
    normalized value is in ``{"off", "false", "0", "disabled", "no"}``, and ``True``
    otherwise.
    """
    if not headers:
        return True

    for key, value in headers.items():
        if isinstance(key, str) and key.strip().lower() == "x-token-saver":
            return not (
                isinstance(value, str)
                and value.strip().lower() in _DISABLED_HEADER_VALUES
            )

    return True


def strip_ansi_codes(text: str) -> str:
    """Remove terminal ANSI escape sequences from ``text``."""
    if not text or "\x1b" not in text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def normalize_whitespace_and_blank_lines(text: str) -> str:
    """Normalize line endings, strip ANSI codes and trailing spaces, and collapse blank lines."""
    if not text:
        return text
    cleaned = _clean_line_endings_and_ansi(text)
    collapsed = _collapse_blank_line_runs(cleaned.split("\n"))
    return "\n".join(collapsed)


def compress_git_diff(text: str) -> str:
    """Losslessly optimize ``git diff`` output blocks while preserving hunk line counts."""
    if not text:
        return text

    normalized = _clean_line_endings_and_ansi(text)
    if not _is_git_diff_text(normalized):
        return text

    diff_lines: list[str] = []
    in_hunk = False
    previous_outside_blank = False

    for raw_line in normalized.split("\n"):
        formatted, in_hunk, is_hunk_body = _format_git_diff_line(raw_line, in_hunk)
        if formatted is None:
            continue
        if is_hunk_body:
            diff_lines.append(formatted)
            previous_outside_blank = False
        elif not formatted:
            if not previous_outside_blank:
                diff_lines.append("")
                previous_outside_blank = True
        else:
            diff_lines.append(formatted)
            previous_outside_blank = False

    while len(diff_lines) > 1 and diff_lines[0] == "":
        diff_lines.pop(0)
    return "\n".join(diff_lines)


def compress_grep_find_ls(text: str) -> str:
    """Losslessly compress ``grep``, ``find``, and ``ls -l`` command outputs."""
    if not text:
        return text
    lines = _clean_line_endings_and_ansi(text).split("\n")
    normalized_lines = _normalize_grep_find_ls_lines(lines)
    deduped_lines = _collapse_consecutive_identical_lines(normalized_lines)
    return "\n".join(deduped_lines)


def deduplicate_lines(text: str) -> str:
    """Collapse consecutive identical non-empty lines while preserving error lines."""
    if not text:
        return text
    lines = [line.rstrip() for line in _clean_line_endings_and_ansi(text).split("\n")]
    return "\n".join(_collapse_consecutive_identical_lines(lines))


def deduplicate_consecutive_lines(text: str) -> str:
    """Alias for ``deduplicate_lines``."""
    return deduplicate_lines(text)


def compress_test_logs(text: str) -> str:
    """Compress repetitive test runner logs and textual progress bars losslessly."""
    if not text:
        return text
    lines = _clean_line_endings_and_ansi(text).split("\n")
    filtered_lines = _filter_test_log_lines(lines)
    deduped_lines = _collapse_consecutive_identical_lines(filtered_lines)
    return "\n".join(deduped_lines)


def compress_tool_output(content: str, *, enabled: bool = True) -> str:
    """Run the complete deterministic RTK-Lite lossless compression pipeline on tool output.

    If ``enabled=False`` or ``content`` is not a non-empty ``str``, returns ``content``
    with 100% byte-for-byte identity.
    """
    if not enabled or not isinstance(content, str) or not content:
        return content

    cleaned = _clean_line_endings_and_ansi(content)
    if _is_git_diff_text(cleaned):
        return compress_git_diff(cleaned)

    lines = cleaned.split("\n")
    lines = _normalize_grep_find_ls_lines(lines)
    lines = _filter_test_log_lines(lines)
    lines = _collapse_consecutive_identical_lines(lines)
    lines = _collapse_blank_line_runs(lines)
    return "\n".join(lines)


def sanitize_tool_messages(
    messages: list[dict[str, Any]],
    *,
    enabled: bool = True,
    headers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return a sanitized copy of ``messages`` with ``role == "tool"`` outputs compressed."""
    if not isinstance(messages, list):
        return messages

    effective_enabled = enabled and should_enable_token_saver(headers)
    return [
        _sanitize_single_message(msg, effective_enabled=effective_enabled)
        if isinstance(msg, dict)
        else msg
        for msg in messages
    ]


# Alias for full compatibility across gateway modules
filter_tool_messages = sanitize_tool_messages


__all__ = [
    "compress_git_diff",
    "compress_grep_find_ls",
    "compress_test_logs",
    "compress_tool_output",
    "deduplicate_consecutive_lines",
    "deduplicate_lines",
    "filter_tool_messages",
    "normalize_whitespace_and_blank_lines",
    "sanitize_tool_messages",
    "should_enable_token_saver",
    "strip_ansi_codes",
]
