# Changelog

All notable changes to the **Gemini-Hybrid-Pool-Gateway** (`v3.2` architecture) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.1] - 2026-10-16

### Added

- **RTK-Lite Lossless Tool Output Compressor ([`core/token_saver.py`](core/token_saver.py)):**
  - Implemented deterministic, stateless lossless compression for `role == "tool"` messages via [`compress_tool_output(content: str, *, enabled: bool = True) -> str`](core/token_saver.py#L457-L475) and [`sanitize_tool_messages(messages: list[dict[str, Any]], *, enabled: bool = True, headers: Mapping[str, str] | None = None) -> list[dict[str, Any]]`](core/token_saver.py#L478-L494) (aliased as [`filter_tool_messages`](core/token_saver.py#L498)).
  - Added [`should_enable_token_saver(headers: Mapping[str, str] | None = None) -> bool`](core/token_saver.py#L355-L372) to support case-insensitive per-request opt-out via the `X-Token-Saver` HTTP header (`"off"`, `"false"`, `"0"`, `"disabled"`, `"no"`), guaranteeing 100% byte-for-byte identity when disabled.
  - Added [`compress_git_diff(text: str) -> str`](core/token_saver.py#L391-L421) to strip non-semantic `index <hash>..<hash>` headers and trailing horizontal padding from unified and `git diff` blocks while preserving file headers (`diff --git`, `--- a/...`, `+++ b/...`), `@@ ... @@` hunk headers, `+`/`-`/context lines, and unprefixed blank lines inside hunks so hunk line counts remain exact.
  - Added [`compress_grep_find_ls(text: str) -> str`](core/token_saver.py#L424-L431) to collapse multi-space column padding in `ls -l` and `total <N>` lines, strip redundant leading `./` prefixes from `find` and `grep` paths, and collapse consecutive duplicate lines.
  - Added [`compress_test_logs(text: str) -> str`](core/token_saver.py#L447-L454), [`deduplicate_lines(text: str) -> str`](core/token_saver.py#L434-L439) (aliased as [`deduplicate_consecutive_lines`](core/token_saver.py#L442-L444)), [`normalize_whitespace_and_blank_lines(text: str) -> str`](core/token_saver.py#L382-L388), and [`strip_ansi_codes(text: str) -> str`](core/token_saver.py#L375-L379) to strip terminal ANSI escape sequences, collapse spinner frames, progress bars, and consecutive duplicate log lines while preserving 100% of error, failure, and stack traceback lines (`Traceback (most recent call last):`, `AssertionError`, `ERROR`, `FAIL`, `FAILED`, `FATAL`, `CRITICAL`, `PANIC`, `Exception`, `File "...", line N`, and `E   ...`).
- **Recursive JSON Schema Sanitizer & Union Flattener ([`core/schema_sanitizer.py`](core/schema_sanitizer.py)):**
  - Defined [`UNSUPPORTED_SCHEMA_KEYS`](core/schema_sanitizer.py#L25-L48) (`frozenset` of the exact 20 JSON Schema meta-keys rejected by `cloudcode-pa` `v1internal`: `additionalProperties`, `$schema`, `$id`, `$ref`, `$defs`, `definitions`, `examples`, `patternProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, `pattern`, `format`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`).
  - Implemented [`clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]`](core/schema_sanitizer.py#L173-L194) to recursively strip all 20 unsupported meta-keys at arbitrary nesting depths while preserving user-defined tool parameter names inside `properties` mappings even when named `format`, `pattern`, `minimum`, or `examples`.
  - Implemented [`flatten_unions(schema: dict[str, Any]) -> dict[str, Any]`](core/schema_sanitizer.py#L308-L335) to convert `type: [...]` arrays and `anyOf` / `oneOf` union branches into concrete Gemini-compatible types with `nullable: True`, merging `properties` and intersecting `required` sets across object union branches, and recursing into both `dict` and `list[dict]` tuple-validation `items`.
- **Dual Zero-Required & History Replay Fix — ADR-05 ([`core/schema_sanitizer.py`](core/schema_sanitizer.py)):**
  - Implemented [`apply_zero_required_fix(parameters: dict[str, Any]) -> tuple[dict[str, Any], bool]`](core/schema_sanitizer.py#L363-L392) to resolve `cloudcode-pa` post-thought `functionCall` stalls when `required` is empty (`[]`):
    - **Case 1 (Zero properties, `properties: {}`):** Injects `"_step": {"type": "string", "description": "Internal step tracker"}` and sets `required = ["_step"]`, returning `(params, True)`.
    - **Case 2 (Optional properties exist, `required: []`):** Promotes the first existing property in `properties` to `required` with `nullable: True` and appends `' (Optional, default: "")'` to its description without injecting `_step`, returning `(params, False)`.
  - Added [`strip_step_from_args(args: dict[str, Any] | None) -> dict[str, Any]`](core/schema_sanitizer.py#L395-L401) and [`reinject_step_to_args(args: dict[str, Any] | None, *, tool_had_zero_properties: bool = True) -> dict[str, Any]`](core/schema_sanitizer.py#L404-L417) to strip synthetic `_step` arguments before returning tool calls to the client and re-hydrate `{"_step": "done"}` in historical `functionCall.args` during multi-turn replay.
  - Added end-to-end tool wrappers [`sanitize_parameters_schema(parameters: dict[str, Any] | None) -> dict[str, Any]`](core/schema_sanitizer.py#L425-L431), [`sanitize_tool(tool: dict[str, Any]) -> dict[str, Any]`](core/schema_sanitizer.py#L434-L455), and [`sanitize_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]`](core/schema_sanitizer.py#L458-L462) supporting both OpenAI `{"type": "function", "function": {...}}` and Gemini `{"functionDeclarations": [...]}` formats without mutating caller inputs.
- **Public Package Exports ([`core/__init__.py`](core/__init__.py)):**
  - Exported all 23 Level 1 constants and functions from [`core/__init__.py`](core/__init__.py#L31-L55) via `__all__`.

### Security

- **WAF System Prompt Shield — ADR-04 ([`core/schema_sanitizer.py`](core/schema_sanitizer.py)):**
  - Implemented [`sanitize_system_prompt_text(text: str) -> str`](core/schema_sanitizer.py#L83-L100) and [`sanitize_messages_waf(messages: list[dict[str, Any]]) -> list[dict[str, Any]]`](core/schema_sanitizer.py#L131-L143) (aliased as [`sanitize_system_prompts`](core/schema_sanitizer.py#L146)) to neutralize Google Cloud ESF/WAF signatures in `system` and `developer` messages (`str` and multipart `list[dict]`) that trigger false `429 RESOURCE_EXHAUSTED` rejections.
  - Rewrites `<system-conventions>` / `</system-conventions>` tags to `<conventions>` / `</conventions>` and isolates `RFC 2119` directive lines with blank lines (`\n\n`) while leaving `user`, `assistant`, and `tool` messages byte-for-byte untouched.

### Fixed

- Prevented false-positive `git diff` classification in [`core/token_saver.py`](core/token_saver.py#L37-L40) when pytest failure logs contain incidental `@@ -1,2 +1,2 @@` assertion diff markers by requiring authentic `diff --git a/... b/...` or `--- a/...\n+++ b/...` headers in [`_is_git_diff_text`](core/token_saver.py#L117-L119).
- Preserved unprefixed blank lines inside unified diff hunks in [`_format_git_diff_line`](core/token_saver.py#L167-L169) so consecutive empty context lines are not collapsed, preventing hunk line-count corruption.
- Eliminated orphaned `[... repeated N times ...]` tags in [`_collapse_consecutive_identical_lines`](core/token_saver.py#L288-L291) when preceding spinner or progress bar lines are filtered out.
- Preserved natural English prose containing `RFC 2119` (such as `"Keywords are interpreted as described in RFC 2119: MUST, REQUIRED"`) on a single line in [`sanitize_system_prompt_text`](core/schema_sanitizer.py#L62-L65) by only splitting inline `RFC 2119` clauses when immediately preceded by a tag boundary (`>`) or sentence/clause punctuation (`:`, `.`, `;`, `!`).

### Verified

- **Verification Gate 2 ([`tests/test_level1_gate2.py`](tests/test_level1_gate2.py)):**
  - Added 20 unit and parametrized test cases in [`tests/test_level1_gate2.py`](tests/test_level1_gate2.py) verifying Level 1 schema sanitization, union flattening, ADR-05 Case 1 & Case 2 zero-required handling, ADR-04 WAF shielding, RTK-Lite lossless compression (`>= 20%` reduction on padded `git diff` blocks), 100% error/traceback preservation, caller input immutability, and `X-Token-Saver: off` byte-for-byte identity.
  - Combined test suite execution across [`tests/test_level0_gate1.py`](tests/test_level0_gate1.py) (19 tests) and [`tests/test_level1_gate2.py`](tests/test_level1_gate2.py) (20 tests): **39 passed**.

---

## [0.1.0] - 2026-10-16

### Added

- **Level 0 Configuration, State Models, and Storage ([`config/settings.py`](config/settings.py), [`config/accounts.json`](config/accounts.json), [`requirements.txt`](requirements.txt)):**
  - Configured mathematical scheduling constants (`DELTA_BASE`, `OAUTH_MAX_CONCURRENCY_DEFAULT = 3`, `WEB_MAX_CONCURRENCY_DEFAULT = 2`, `WEB_MIN_START_INTERVAL_S = 0.5`, `WEB_FAST_REQUEST_THRESHOLD_S = 1.0`, `WEB_MIN_INTERVAL_S = 0.6`, `WEB_JITTER_RANGE_S = (0.4, 0.8)`, `SESSION_AFFINITY_MULTIPLIER = 1.35`, `SESSION_AFFINITY_MAX_CONTEXT_CHARS = 120_000`, `REHYDRATION_TIER2_MAX_CHARS = 60_000`) and longest-prefix model weight lookup [`get_model_base_delta(model_name: str) -> float`](config/settings.py#L85-L110) as specified in [`docs/founditon/01_data_schemas_and_state.md`](docs/founditon/01_data_schemas_and_state.md) and [`docs/founditon/03_scheduling_and_control_spec.md`](docs/founditon/03_scheduling_and_control_spec.md).
  - Implemented Pydantic v2 structural contracts ([`OAuthAccount`](config/settings.py#L151-L196), [`WebAccount`](config/settings.py#L249-L353), [`AccountsConfig`](config/settings.py#L356-L362)) with isolated `PrivateAttr` runtime counters and `asyncio.Lock` instances excluded from JSON serialization.
  - Implemented [`AccountsStateRegistry`](config/settings.py#L372-L488) as the in-memory Single Source of Truth (ADR-02) and [`save_accounts_snapshot`](config/settings.py#L521-L546) protected by `accounts_file_lock` for atomic `.tmp` + `os.replace()` persistence.
  - Implemented [`LMDBStore`](config/settings.py#L561-L683) managing `db_sessions` and `db_signatures` tables with account-scoped session pruning.
- **Verification Gate 1 ([`tests/test_level0_gate1.py`](tests/test_level0_gate1.py)):**
  - Added 19 unit, parametrized, and 50-task concurrency stress tests verifying Level 0 state management, atomic persistence, and LMDB durability.
