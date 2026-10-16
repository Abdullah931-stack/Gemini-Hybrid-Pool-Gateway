# Phase 2 (Level 1) Closure Report — Stateless Filters, Token Saver, Tool Schema Sanitizer & WAF Shield

| Field | Value |
| :--- | :--- |
| **Phase / Topological Level** | Phase 2 / Level 1 — Stateless Protocol Filters, Schema Sanitizers & WAF Shield |
| **Release Version** | `0.1.1` ([`changelog.md`](changelog.md)) |
| **Date** | `2026-10-16` |
| **Verification Gate** | **Verification Gate 2 — PASSED** (`20/20` Gate 2 tests passed; `39/39` cumulative tests passed) |
| **Delivered Modules** | [`core/token_saver.py`](../core/token_saver.py), [`core/schema_sanitizer.py`](../core/schema_sanitizer.py), [`core/__init__.py`](../core/__init__.py) |
| **Verification Suite** | [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py) (alongside [`tests/test_level0_gate1.py`](../tests/test_level0_gate1.py)) |
| **Governing Specifications** | [`founditon/04_protocol_and_data_plane.md`](founditon/04_protocol_and_data_plane.md), [`founditon/06_architecture_decisions_adr.md`](founditon/06_architecture_decisions_adr.md) (`ADR-04`, `ADR-05`), [`founditon/07_execution_plan.md`](founditon/07_execution_plan.md) |

---

## 1. Executive Summary

Phase 2 implements **Level 1** of the bottom-up topological execution plan ([`founditon/07_execution_plan.md`](founditon/07_execution_plan.md#L11)): the stateless protocol filters, lossless tool-output compressor, recursive JSON Schema sanitizer, union flattener, dual zero-required parameter fixer, and WAF system prompt shield.

Because agent harnesses (**Pi** and **Hermes**) manage conversation compaction internally, the gateway is strictly prohibited from dropping or summarizing conversation turns ([`founditon/04_protocol_and_data_plane.md`](founditon/04_protocol_and_data_plane.md#L3)). Level 1 enforces protocol compatibility with `cloudcode-pa` (`v1internal`) and compresses redundant tool output tokens deterministically without mutating caller data structures or altering semantic content.

All deliverables passed static code quality audits (`clean-code-guard`), test quality audits (`test-guard`), and **Verification Gate 2** (`20 passed` in [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py); `39 passed` across the repository in `1.33s`).

---

## 2. Delivered Architecture & Components

### 2.1 RTK-Lite Lossless Tool Output Compressor ([`core/token_saver.py`](../core/token_saver.py))

[`core/token_saver.py`](../core/token_saver.py) implements the deterministic, stateless lossless filter specified in Section 1 of [`founditon/04_protocol_and_data_plane.md`](founditon/04_protocol_and_data_plane.md#L5-L8). It operates exclusively on `role == "tool"` messages and supports immediate per-request opt-out via the `X-Token-Saver: off` HTTP header.

#### Public API Signatures & Contracts

| Symbol | Signature | Contract & Behavior |
| :--- | :--- | :--- |
| [`should_enable_token_saver`](../core/token_saver.py#L355-L372) | `should_enable_token_saver(headers: Mapping[str, str] | None = None) -> bool` | Inspects `headers` case-insensitively for `X-Token-Saver`. Returns `False` if the stripped lowercase value is in `_DISABLED_HEADER_VALUES` (`{"off", "false", "0", "disabled", "no"}`), and `True` otherwise. |
| [`strip_ansi_codes`](../core/token_saver.py#L375-L379) | `strip_ansi_codes(text: str) -> str` | Removes terminal ANSI CSI/OSC escape sequences matching [`_ANSI_ESCAPE_RE`](../core/token_saver.py#L28-L30) without altering printable text. |
| [`normalize_whitespace_and_blank_lines`](../core/token_saver.py#L382-L388) | `normalize_whitespace_and_blank_lines(text: str) -> str` | Strips ANSI codes, normalizes `\r\n` and `\r` to `\n`, strips trailing horizontal spaces, and collapses consecutive blank lines to at most one. |
| [`compress_git_diff`](../core/token_saver.py#L391-L421) | `compress_git_diff(text: str) -> str` | Verifies authentic diff headers via [`_is_git_diff_text`](../core/token_saver.py#L117-L119), removes `index <hash>..<hash>` metadata lines, strips trailing horizontal whitespace, preserves `diff --git`, `---`, `+++`, `@@ ... @@` hunk headers, `+`/`-`/context lines, and preserves unprefixed blank lines inside hunks so hunk line counts remain valid. |
| [`compress_grep_find_ls`](../core/token_saver.py#L424-L431) | `compress_grep_find_ls(text: str) -> str` | Normalizes multi-space column padding in `ls -l` and `total <N>` lines, strips leading `./` prefixes from `find` and `grep` match lines, and collapses consecutive identical non-error lines. |
| [`deduplicate_lines`](../core/token_saver.py#L434-L439) | `deduplicate_lines(text: str) -> str` | Collapses consecutive identical non-empty lines into a single line followed by `  [... repeated N times ...]` while leaving error and traceback lines untouched. Aliased as [`deduplicate_consecutive_lines`](../core/token_saver.py#L442-L444). |
| [`compress_test_logs`](../core/token_saver.py#L447-L454) | `compress_test_logs(text: str) -> str` | Removes spinner frames (`⠋ Running...`), intermediate progress bars (`45%\|████\|`), collapses dot-progress runs (`[... N passed [50%] ...]`, `[... N progress dots ...]`) and inline percentage runs (`[progress: 90%]`), while preserving 100% of error, assertion, and traceback lines verbatim. |
| [`compress_tool_output`](../core/token_saver.py#L457-L475) | `compress_tool_output(content: str, *, enabled: bool = True) -> str` | End-to-end RTK-Lite pipeline. Returns `content` with 100% byte-for-byte identity when `enabled=False` or `content` is empty. Dispatches authentic diffs to [`compress_git_diff`](../core/token_saver.py#L391-L421), and runs `ls`/`find`/`grep` normalization, test log filtering, deduplication, and blank-line collapsing on general tool outputs. |
| [`sanitize_tool_messages`](../core/token_saver.py#L478-L494) | `sanitize_tool_messages(messages: list[dict[str, Any]], *, enabled: bool = True, headers: Mapping[str, str] | None = None) -> list[dict[str, Any]]` | Returns a new message list where only `role == "tool"` messages (`str` content or `type == "text"` parts in `list[dict]`) are compressed when `enabled and should_enable_token_saver(headers)` is `True`. Non-tool messages and image parts remain untouched. Aliased as [`filter_tool_messages`](../core/token_saver.py#L498). |

---

### 2.2 JSON Schema Sanitizer, Union Flattener, Zero-Required Fixer & WAF Shield ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py))

[`core/schema_sanitizer.py`](../core/schema_sanitizer.py) implements Section 2.a and Section 2.b of [`founditon/04_protocol_and_data_plane.md`](founditon/04_protocol_and_data_plane.md#L13-L24), enforcing architectural decisions **ADR-04** ([`founditon/06_architecture_decisions_adr.md`](founditon/06_architecture_decisions_adr.md#L26-L35)) and **ADR-05** ([`founditon/06_architecture_decisions_adr.md`](founditon/06_architecture_decisions_adr.md#L36-L43)).

#### Constant Definition

- [`UNSUPPORTED_SCHEMA_KEYS: frozenset[str]`](../core/schema_sanitizer.py#L25-L48): Contains the exact 20 JSON Schema meta-keys rejected by `cloudcode-pa` (`v1internal`):
  `additionalProperties`, `$schema`, `$id`, `$ref`, `$defs`, `definitions`, `examples`, `patternProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, `pattern`, `format`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`.

#### Public API Signatures & Architectural Contracts

| Symbol | Signature | Contract & Architectural Decision |
| :--- | :--- | :--- |
| [`sanitize_system_prompt_text`](../core/schema_sanitizer.py#L83-L100) | `sanitize_system_prompt_text(text: str) -> str` | **ADR-04 (WAF Shield):** Rewrites `<system-conventions>` / `</system-conventions>` to `<conventions>` / `</conventions>` and isolates `RFC 2119` directive lines with blank lines (`\n\n`) so Google Cloud ESF/WAF does not reject requests with false `429 RESOURCE_EXHAUSTED` errors. |
| [`sanitize_messages_waf`](../core/schema_sanitizer.py#L131-L143) | `sanitize_messages_waf(messages: list[dict[str, Any]]) -> list[dict[str, Any]]` | **ADR-04 (WAF Shield):** Deep-copies `messages` and applies [`sanitize_system_prompt_text`](../core/schema_sanitizer.py#L83-L100) exclusively to `system` and `developer` roles (`str` and `list[dict]` content parts), leaving `user`, `assistant`, and `tool` messages byte-for-byte identical. Aliased as [`sanitize_system_prompts`](../core/schema_sanitizer.py#L146). |
| [`clean_json_schema`](../core/schema_sanitizer.py#L173-L194) | `clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]` | Recursively removes all 20 keys in [`UNSUPPORTED_SCHEMA_KEYS`](../core/schema_sanitizer.py#L25-L48) at arbitrary nesting depths while routing `properties` dicts through [`_clean_properties_map`](../core/schema_sanitizer.py#L154-L162) so user-defined tool parameters named `format`, `pattern`, `minimum`, or `examples` are never deleted. |
| [`flatten_unions`](../core/schema_sanitizer.py#L308-L335) | `flatten_unions(schema: dict[str, Any]) -> dict[str, Any]` | Recursively flattens `type: [...]` arrays and `anyOf` / `oneOf` unions into concrete types with `nullable: True`, merges distinct branch descriptions with `" \| "`, merges `properties` and intersects `required` sets across object union branches, and recurses into both `dict` and `list[dict]` `items`. |
| [`apply_zero_required_fix`](../core/schema_sanitizer.py#L363-L392) | `apply_zero_required_fix(parameters: dict[str, Any]) -> tuple[dict[str, Any], bool]` | **ADR-05 (Dual Zero-Required Fix):** Prevents `cloudcode-pa` post-thought `functionCall` stalls when `required` is empty:<br>• **Case 1 (`properties: {}`):** Injects `"_step": {"type": "string", "description": "Internal step tracker"}` and sets `required = ["_step"]`, returning `(params, True)`.<br>• **Case 2 (non-empty `properties`, `required: []`):** Promotes the first property in `properties` to `required` with `nullable: True` and appends `' (Optional, default: "")'` to its description without injecting `_step`, returning `(params, False)`. |
| [`strip_step_from_args`](../core/schema_sanitizer.py#L395-L401) | `strip_step_from_args(args: dict[str, Any] | None) -> dict[str, Any]` | **ADR-05 (Outbound Cleanup):** Returns a copy of `functionCall.args` with the synthetic `_step` key removed before streaming or returning tool calls to the client agent. |
| [`reinject_step_to_args`](../core/schema_sanitizer.py#L404-L417) | `reinject_step_to_args(args: dict[str, Any] | None, *, tool_had_zero_properties: bool = True) -> dict[str, Any]` | **ADR-05 (History Replay Consistency):** Re-injects `{"_step": "done"}` into historical `functionCall.args` when rebuilding `request.contents` on subsequent turns for Case 1 tools (`tool_had_zero_properties=True`). |
| [`sanitize_parameters_schema`](../core/schema_sanitizer.py#L425-L431) | `sanitize_parameters_schema(parameters: dict[str, Any] | None) -> dict[str, Any]` | Runs [`clean_json_schema`](../core/schema_sanitizer.py#L173-L194) $\rightarrow$ [`flatten_unions`](../core/schema_sanitizer.py#L308-L335) $\rightarrow$ [`apply_zero_required_fix`](../core/schema_sanitizer.py#L363-L392) in sequence. |
| [`sanitize_tool`](../core/schema_sanitizer.py#L434-L455) | `sanitize_tool(tool: dict[str, Any]) -> dict[str, Any]` | Sanitizes a single tool definition in OpenAI format (`{"type": "function", "function": {...}}`), Gemini container format (`{"functionDeclarations": [...]}`), or raw declaration format (`{"name": ..., "parameters": ...}`). |
| [`sanitize_tools`](../core/schema_sanitizer.py#L458-L462) | `sanitize_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]` | Sanitizes a list of tool definitions without mutating the caller's list or nested dicts. |

---

### 2.3 Package Export Surface ([`core/__init__.py`](../core/__init__.py))

[`core/__init__.py`](../core/__init__.py) re-exports all 23 public symbols from [`core/schema_sanitizer.py`](../core/schema_sanitizer.py) (12 symbols) and [`core/token_saver.py`](../core/token_saver.py) (11 symbols) in a sorted `__all__` tuple so downstream Level 2–5 modules can import directly from `core`.

---

## 3. Quality Assurance & Audit Resolution Summary

Following the initial implementation of Level 1, comprehensive audits were executed using `clean-code-guard` and `test-guard`. Every identified edge case and structural finding was resolved prior to Gate 2 closure:

1. **False-Positive `git diff` Detection on Pytest Failure Logs ([`core/token_saver.py`](../core/token_saver.py#L37-L40)):**
   - *Finding:* Pytest assertion diffs frequently emit lines containing `@@ -1,2 +1,2 @@` inside standard test failure logs. Matching `^@@ .* @@` alone caused [`compress_tool_output`](../core/token_saver.py#L457-L475) to route full test logs into [`compress_git_diff`](../core/token_saver.py#L391-L421), bypassing test-log spinner/progress compression.
   - *Resolution:* Tightened [`_GIT_DIFF_HEADER_RE`](../core/token_saver.py#L37-L40) to require authentic unified diff headers (`^diff --git a/.+ b/.+` or `^--- (?:a/|\S+).*\n\+\+\+ (?:b/|\S+)`). Added an explicit regression assertion with `@@ -1,2 +1,2 @@` inside [`test_test_log_compression_collapses_noise_while_preserving_100_pct_of_errors_and_tracebacks`](../tests/test_level1_gate2.py#L551-L601).
2. **Preserving Unprefixed Blank Context Lines Inside Unified Diff Hunks ([`core/token_saver.py`](../core/token_saver.py#L167-L169)):**
   - *Finding:* Many diff generators emit empty context lines inside `@@ ... @@` hunks as completely empty lines (`""`) rather than a single space (`" "`). Collapsing consecutive empty lines inside a hunk alters the hunk line count declared in the `@@ -l,s +l,s @@` header.
   - *Resolution:* Updated [`_format_git_diff_line`](../core/token_saver.py#L147-L172) to mark `stripped == ""` when `in_hunk` is `True` as `is_hunk_body = True`, preserving all blank context lines inside hunks while still collapsing redundant blank lines outside hunks.
3. **Orphaned Repeat Tags After Filtering Spinners/Progress Bars ([`core/token_saver.py`](../core/token_saver.py#L288-L291)):**
   - *Finding:* If an input log already contained `[... repeated N times ...]` immediately following a spinner or progress bar line that was dropped by [`_filter_test_log_lines`](../core/token_saver.py#L231-L251), the repeat tag could become orphaned or attach to an unrelated preceding line.
   - *Resolution:* Reordered [`compress_tool_output`](../core/token_saver.py#L470-L474) so [`_filter_test_log_lines`](../core/token_saver.py#L231-L251) executes before [`_collapse_consecutive_identical_lines`](../core/token_saver.py#L277-L304), and added a guard in [`_collapse_consecutive_identical_lines`](../core/token_saver.py#L288-L291) to drop any orphaned repeat tags.
4. **Natural English Prose Containing `RFC 2119` ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py#L62-L65)):**
   - *Finding:* Splitting unconditionally before any inline occurrence of `RFC 2119` broke mid-sentence phrases such as `"Keywords are interpreted as described in RFC 2119: MUST, REQUIRED"` across two lines.
   - *Resolution:* Restricted [`_INLINE_RFC2119_BEFORE_RE`](../core/schema_sanitizer.py#L62-L65) (`r"(>|[:.;!])[ \t]*(RFC[ \t]*2119\b)"`) so inline line-splitting only triggers when `RFC 2119` immediately follows a tag close (`>`) or clause/sentence punctuation (`:`, `.`, `;`, `!`), while [`_RFC2119_LINE_ISOLATION_RE`](../core/schema_sanitizer.py#L66-L69) isolates the full line with `\n\n`.
5. **Recursive Union Flattening Inside Tuple-Validation Array `items` ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py#L321-L325)):**
   - *Finding:* In JSON Schema Draft 7 / 2020-12, `items` can be either a single schema `dict` or a `list[dict]` for tuple validation. [`flatten_unions`](../core/schema_sanitizer.py#L308-L335) initially only recursed when `items` was a `dict`.
   - *Resolution:* Added `elif isinstance(flattened_schema.get("items"), list):` branch in [`flatten_unions`](../core/schema_sanitizer.py#L321-L325) and verified it with a parametrized test case in [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py#L247-L263).
6. **Single-Responsibility Decomposition (`clean-code-guard`):**
   - Refactored both [`core/token_saver.py`](../core/token_saver.py) and [`core/schema_sanitizer.py`](../core/schema_sanitizer.py) into small, single-responsibility helper functions (every function $\le 35$ lines, shallow nesting $\le 2$ levels, full type annotations, and zero unused parameters).

---

## 4. Verification Gate 2 Metrics & Test Coverage

Verification Gate 2 was executed via `pytest -v` against [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py) and [`tests/test_level0_gate1.py`](../tests/test_level0_gate1.py):

```text
platform win32 -- Python 3.11.10, pytest-9.1.1, pluggy-1.7.0
collected 39 items

tests/test_level0_gate1.py (19 items) ................................... PASSED
tests/test_level1_gate2.py (20 items) ................................... PASSED

============================== 39 passed in 1.33s ==============================
```

### Gate 2 Test Matrix ([`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py))

| Test Function | Cases | Verified Requirement | Status |
| :--- | :---: | :--- | :---: |
| [`test_recursive_schema_cleaner_strips_all_20_unsupported_keys_and_preserves_named_properties`](../tests/test_level1_gate2.py#L67-L175) | 1 | Strips all 20 [`UNSUPPORTED_SCHEMA_KEYS`](../core/schema_sanitizer.py#L25-L48) at root, property, and nested array item levels while preserving user tool parameters named `format`, `pattern`, and `minimum`. | PASSED |
| [`test_union_and_type_array_flattener_produces_concrete_nullable_types`](../tests/test_level1_gate2.py#L178-L271) | 5 | Parametrized verification of `type: ["string", "null"]`, `anyOf` scalar+null with description merging, `oneOf` multi-scalar+null, `anyOf` object branch merging (`properties` union + `required` intersection), and `items: list[dict]` tuple-array unions. | PASSED |
| [`test_zero_required_case_1_injects_step_and_maintains_history_replay_consistency`](../tests/test_level1_gate2.py#L274-L315) | 1 | **ADR-05 Case 1:** Injects `_step` into `properties` and `required: ["_step"]` when `properties: {}`; verifies [`strip_step_from_args`](../core/schema_sanitizer.py#L395-L401) and [`reinject_step_to_args`](../core/schema_sanitizer.py#L404-L417) (`{"_step": "done"}`). | PASSED |
| [`test_zero_required_case_2_promotes_first_optional_property_without_injecting_step`](../tests/test_level1_gate2.py#L318-L343) | 1 | **ADR-05 Case 2:** Promotes the first optional property (`"directory"`) to `required` with `nullable: True` and default `""` hint without injecting `_step`. | PASSED |
| [`test_schema_and_waf_sanitizers_never_mutate_caller_inputs_in_place`](../tests/test_level1_gate2.py#L346-L393) | 1 | Verifies `copy.deepcopy` immutability across [`sanitize_tools`](../core/schema_sanitizer.py#L458-L462), [`sanitize_parameters_schema`](../core/schema_sanitizer.py#L425-L431), and [`sanitize_messages_waf`](../core/schema_sanitizer.py#L131-L143). | PASSED |
| [`test_waf_shield_rewrites_system_and_developer_messages_and_isolates_rfc2119`](../tests/test_level1_gate2.py#L401-L473) | 1 | **ADR-04:** Rewrites `<system-conventions>` to `<conventions>` and isolates `RFC 2119` with `\n\n` in `system` (`str`) and `developer` (`list[dict]`) messages while preserving mid-sentence `RFC 2119` prose and leaving `user`, `assistant`, and `tool` messages untouched. | PASSED |
| [`test_git_diff_compression_strips_index_headers_and_achieves_20_to_40_pct_savings`](../tests/test_level1_gate2.py#L480-L523) | 1 | Strips `index <hash>..<hash>` headers and ANSI codes, preserves file headers, `@@` hunks, consecutive blank hunk context lines, and `+`/`-` lines, achieving $\ge 20\%$ character reduction. | PASSED |
| [`test_grep_find_and_ls_compression_normalizes_columns_paths_and_duplicates`](../tests/test_level1_gate2.py#L526-L548) | 1 | Normalizes `ls -l` and `total` spacing, strips `./` from `find` and `grep` outputs, and collapses consecutive duplicates into `  [... repeated 3 times ...]`. | PASSED |
| [`test_test_log_compression_collapses_noise_while_preserving_100_pct_of_errors_and_tracebacks`](../tests/test_level1_gate2.py#L551-L601) | 1 | Collapses spinners, progress bars, dot-runs, and repeated `INFO` lines without orphan repeat tags while preserving 100% of `FAILED`, `AssertionError`, incidental `@@` diff lines, `Traceback`, and `ERROR` lines. | PASSED |
| [`test_x_token_saver_off_header_and_disabled_flag_guarantee_byte_for_byte_identity`](../tests/test_level1_gate2.py#L604-L668) | 7 | Parametrized check across `None`, `{}`, `"on"`, `"off"`, `"  OFF  "`, `"false"`, and `"0"` headers plus `enabled=False`, verifying 100% byte-for-byte identity on both `str` and multipart `list[dict]` tool messages. | PASSED |

---

## 5. Readiness for Phase 3 (Level 2)

With Level 0 ([`config/settings.py`](../config/settings.py)) and Level 1 ([`core/token_saver.py`](../core/token_saver.py), [`core/schema_sanitizer.py`](../core/schema_sanitizer.py)) complete and verified, the repository is ready to proceed to **Phase 3 (Level 2 — Authentication, Model Discovery, and Context Prefix Rehydration)** as defined in [`founditon/07_execution_plan.md`](founditon/07_execution_plan.md#L42-L51):

1. **`core/auth_oauth_cli.py`**: Can now import [`OAuthAccount`](../config/settings.py#L151), [`state_registry`](../config/settings.py#L490), and [`save_accounts_snapshot`](../config/settings.py#L521) from [`config/settings.py`](../config/settings.py) to manage OAuth 2.0 + PKCE (`S256`) credentials, `:loadCodeAssist` project discovery, and per-account `token_refresh_lock` synchronization.
2. **`core/auth_web.py`**: Can now import [`WebAccount`](../config/settings.py#L249), [`COOKIES_CACHE_DIR`](../config/settings.py#L38-L40), and [`save_accounts_snapshot`](../config/settings.py#L521) to manage isolated Firefox cookie imports and atomic cookie cache persistence under `cookie_write_lock`.
3. **`core/model_registry.py`**: Can now resolve dynamic model inventories for both channels and map reasoning effort suffixes (`-low`, `-medium`, `-high`) alongside [`get_model_base_delta`](../config/settings.py#L85-L110).
4. **`core/prefix_differ.py`**: Can now combine [`LMDBStore`](../config/settings.py#L561-L683) (`db_sessions`), [`TEMP_HISTORY_DIR`](../config/settings.py#L48-L53), [`REHYDRATION_TIER2_MAX_CHARS`](../config/settings.py#L76), and [`sanitize_tool_messages`](../core/token_saver.py#L478-L494) to implement the cross-platform 3-tier context rehydration state machine (`Same-Account Prefix Hit`, `<= 60,000` char `<conversation_history>` merge, and `> 60,000` char `harness_history_{uuid}.txt` file passthrough).
