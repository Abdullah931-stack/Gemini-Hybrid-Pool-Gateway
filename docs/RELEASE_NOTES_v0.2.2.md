# Release Notes: Gemini-Hybrid-Pool-Gateway v0.2.2

**Release Date:** 2026-10-18  
**Architecture Version:** v3.2  
**Previous Version:** [v0.2.1](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/CHANGELOG.md#L7-L36)

---

## Executive Summary

Version **v0.2.2** delivers fundamental architectural hardening across four operational planes:
1. **Isolated Firefox Browser Execution & Automated Lifecycle**: Elimination of host profile pollution, recursive process spawning, and detached process leaks via an isolated, ephemeral browser manager (`core/firefox_launcher.py`).
2. **Unified Authentication Modernization**: Zero-interaction credential harvesting for both Web Cookie and Antigravity OAuth CLI channels, replacing legacy `webbrowser.open` calls with isolated browser sessions and built-in public CLI credentials.
3. **Protobuf Schema Sanitization for Agent Harnesses**: Complete elimination of Google CloudCode PA `HTTP 400 INVALID_ARGUMENT` errors through the expansion of `UNSUPPORTED_SCHEMA_KEYS` to 35 schema keys, single-element `enum` normalization for `const`, and `allOf` schema flattening.
4. **Strict Anti-Mocking & Quota Realism**: Elimination of synthetic models and fabricated 100% quota fractions; models and quotas reflect genuine Google API state.
5. **Session Stability & ChatSession Alignment**: Remediation of `gemini_webapi` parameter mismatches (`temporary` keyword rejection in `ChatSession.__init__`), safe client shutdown (`close_client`, `close_all`), and robust WAL sidecar extraction.

---

## Architectural Changes & Root Cause Analysis

### 1. Isolated & Ephemeral Firefox Launcher (`core/firefox_launcher.py`)

#### Problem Statement & Root Cause
Prior releases utilized Python's built-in `webbrowser.open` or accessed the user's primary Mozilla Firefox profile directly (`%APPDATA%/Mozilla/Firefox/Profiles/*.default-release`). This caused three critical architectural failure modes:
1. **Process Detachment & Windows Orphan Spawning**: Calling the Firefox binary without process synchronization on Windows caused the initial executable to delegate to an existing background Firefox broker and terminate immediately, leaving the gateway unable to track browser exit or user cancellation.
2. **Profile Lock Contention (`parent.lock`)**: When the user had Firefox actively open, attempting to query `cookies.sqlite` directly resulted in `sqlite3.OperationalError: database is locked` or uncheckpointed write-ahead logs (`cookies.sqlite-wal`).
3. **User Profile Pollution**: Authentication flows polluted the user's personal browser cache, session history, and open tabs.

#### Architectural Solution
A dedicated isolation module [`core/firefox_launcher.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/firefox_launcher.py) was introduced:
- **Custom Ephemeral Profile Factory (`create_isolated_firefox_profile`)**: Creates a clean directory via `tempfile.mkdtemp(prefix="ghpg_ff_isolated_")` and writes a customized `user.js` configuration containing `USER_JS_PREFS_CONTENT`:
  * `browser.shell.checkDefaultBrowser = false`
  * `startup.homepage_welcome_url = ""`
  * `browser.startup.homepage_override.mstone = "ignore"`
  * `datareporting.policy.dataSubmissionPolicyBypassNotification = true`
  * `signon.rememberSignons = false`
  * `browser.sessionstore.resume_from_crash = false`
- **Isolated Process Orchestration (`IsolatedFirefoxSession`)**:
  * Executed with flags `["-no-remote", "-new-instance", "-profile", str(profile_dir)]`.
  * On `win32`, appends `--wait-for-browser` to prevent premature detachment.
  * Implements both synchronous (`__enter__` / `__exit__`) and asynchronous (`__aenter__` / `__aexit__`) context management protocols.
- **Guaranteed Teardown & Process Tree Termination**:
  * Graceful termination via `proc.terminate()`, bounded by a 5.0-second wait before escalating to `proc.kill()`.
  * On Windows, invokes `taskkill /F /T /PID <pid>` to terminate detached child processes.
  * Profile cleanup (`_cleanup_profile_directory`) implements retry loops with backoff (`max_attempts=5`, `retry_delay=0.2s`) to handle transient Windows filesystem locks.
- **Sidecar-Aware SQLite Harvesting**:
  * `_read_profile_cookies`: Copies `cookies.sqlite` alongside `-wal` and `-shm` sidecars to an isolated temporary directory before reading.
  * `_query_moz_cookies_db`: Connects via URI read-only mode (`?mode=ro`) with standard path fallback, sorting by `lastAccessed DESC` to retrieve the latest `__Secure-1PSID` and `__Secure-1PSIDTS`.
- **High-Level Harvesting Coroutines**:
  * `harvest_gemini_cookies_via_isolated_firefox`: Polls the SQLite profile until credentials appear, validates credential stability after a 0.5s settling delay, and cleans up resources.
  * `run_oauth_in_isolated_firefox`: Coordinates OAuth loopback code capture with browser window liveliness checks, cancelling the listener if the user manually closes the browser.

---

### 2. Multi-Channel Authentication Modernization

#### CLI & Web Pool Integration
- **OAuth CLI Channel ([`core/auth_oauth_cli.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/auth_oauth_cli.py)):**
  * Added `authorize_oauth_via_isolated_firefox`: Binds a local `OAuthCallbackServer` to preferred port `8000` (falling back to ephemeral port `0`), launches isolated Firefox to Google's OAuth 2.0 authorization endpoint, and returns `(code, redirect_uri)`.
  * Public Antigravity CLI Credentials: Built-in base64-encoded client credentials (`_BUILTIN_CLI_CLIENT_ID_B64`, `_BUILTIN_CLI_CLIENT_SECRET_B64`) eliminate mandatory `.env` credential setup out-of-the-box. Users can override credentials via `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET`, or enforce strict manual credential requirement by setting `GHPG_DISABLE_BUILTIN_OAUTH_CLIENT=1`.
- **Web Cookie Pool ([`core/auth_web.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/auth_web.py)):**
  * Added `login_web_via_isolated_firefox`: Launches isolated Firefox directly to `https://gemini.google.com/app` and harvests session cookies.
  * Hardened `extract_firefox_cookies`: Explicitly checks for `cookies.sqlite-wal` and clones the database and sidecars before extraction to read uncheckpointed commits.
  * Session Cleanup: Added `WebSessionManager.close_client` and `WebSessionManager.close_all` to persist cookies and cleanly close underlying HTTP clients on shutdown or account re-linking.
- **REST API Endpoints ([`api/dashboard_routes.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/api/dashboard_routes.py)):**
  * Refactored `POST /api/web-accounts/browser-login` to execute `login_web_via_isolated_firefox` asynchronously with configurable `poll_seconds` (defaulting to 180s).
  * Removed legacy `webbrowser.open` calls from `auto_detect_web_account`.
  * Standardized account initialization and model discovery via `_initialize_and_discover_web_account`.
- **Command-Line Interface ([`main.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/main.py)):**
  * Updated `add-oauth` command to automatically launch isolated Firefox.
  * Added option 3 to `add-web` wizard: *"Launch isolated Firefox to sign in"*.
  * Replaced `webbrowser.open` calls throughout CLI commands.

---

### 3. Protobuf Schema Sanitizer Hardening (`core/schema_sanitizer.py`)

#### Problem Statement & Root Cause
Agent harnesses such as Cline, Roo Code, and Cursor generate tool definitions using JSON Schema draft 7 / draft 2020-12 specifications. When passed to Google CloudCode PA (`v1internal:loadCodeAssist`), the endpoint serializes tool schemas into internal Protocol Buffer definitions (`google.protobuf.Struct` / OpenAPI 3.0 subset).

Encountering unsupported meta-schema keywords—specifically `exclusiveMinimum`, `exclusiveMaximum`, `const`, or nested `allOf` blocks—triggered immediate `HTTP 400 INVALID_ARGUMENT` rejections from Google API:
```json
{
  "error": {
    "code": 400,
    "message": "Invalid JSON payload received. Unknown name \"exclusiveMinimum\": Cannot find field.",
    "status": "INVALID_ARGUMENT"
  }
}
```

#### Architectural Solution
[`core/schema_sanitizer.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/schema_sanitizer.py) was enhanced:
1. **Expansion of `UNSUPPORTED_SCHEMA_KEYS` to 35 Keys:**
   Extended the rejected key set to encompass all OpenAPI 3.0 / Protobuf incompatible keywords:
   - Numerical bounds: `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minimum`, `maximum`.
   - Structural & composition: `allOf`, `propertyNames`, `dependencies`, `dependentRequired`, `dependentSchemas`, `unevaluatedProperties`, `unevaluatedItems`, `prefixItems`.
   - Value & Type annotations: `const`, `contentMediaType`, `contentEncoding`, `readOnly`, `writeOnly`, `deprecated`.
   - String & Array constraints: `pattern`, `format`, `minLength`, `maxLength`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`, `additionalProperties`.
2. **`const` to Single-Element `enum` Normalization:**
   Protobuf schemas lack a `const` scalar constraint. In `clean_json_schema`, schemas containing `const` without `enum` are converted into an equivalent single-item string enumeration:
   ```python
   if "const" in schema and "enum" not in schema:
       cleaned["enum"] = [str(schema["const"])]
   ```
3. **`allOf` Intersection Flattening (`flatten_unions`):**
   Added `"allOf"` to union processing (`for union_key in ("anyOf", "oneOf", "allOf"):`), flattening nested object property definitions into a single merged dictionary.

---

### 4. Strict Anti-Mocking & Quota Realism (`core/model_registry.py`)

#### Problem Statement & Remediation
- **Fabricated 100% Quotas:** Prior implementations populated missing OAuth models with synthetic `remaining_fraction=1.0` quota states. In v0.2.2, this behavior is strictly eliminated:
  * In `_apply_fetch_models_quotas`, unreturned quota buckets are not fabricated with synthetic 1.0 fractions.
  * In `_fallback_default_oauth_models`, only canonical model identifiers are populated in `discovered_models`; `quota_state.model_quotas` remains empty until actual quota buckets are returned by Google API.
- **Empty Project ID Guard:** Handled accounts with missing or blank `project_id`, preventing malformed JSON request payloads (`{"project": ""}`) to `fetchAvailableModels` and `retrieveUserQuota`.
- **Header Shielding:** Updated `_build_oauth_headers` to route through `build_v1internal_headers` in `core/auth_oauth_cli.py`, supplying proper user agent and security headers.
- **Web Model Suffix Resolution:** Enhanced `resolve_web_model` and `resolve_web_model_sync` to safely probe `client.resolve_model` or `client._resolve_model_by_name`, stripping reasoning effort suffixes (`-minimal`, `-low`, `-medium`, `-high`).

---

### 5. ChatSession Protocol Compatibility (`core/prefix_differ.py`)

#### Problem Statement & Remediation
In `gemini_webapi`, calling `client.start_chat(temporary=True)` raised:
```
TypeError: ChatSession.__init__() got an unexpected keyword argument 'temporary'
```
In `core/prefix_differ.py`:
- `_start_client_chat_session`: Stripped `temporary` from `start_chat` kwargs when `accepts_var_kw` is True.
- `_open_chat_message_stream`: Now passes `temporary` directly to `chat.send_message_stream(...)`.
- `extended_thinking`: Automatically passes `extended_thinking=True` to `send_message_stream` when `reasoning_effort in ("high", "medium")`.

---

## File Verification & Symbol Map

| Modified/Added File | Verified Symbols / Functions | Architectural Responsibility |
|---|---|---|
| [`core/firefox_launcher.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/firefox_launcher.py) | `FirefoxNotFoundError`, `_is_executable_file`, `_get_standard_firefox_paths`, `resolve_firefox_binary`, `create_isolated_firefox_profile`, `IsolatedFirefoxSession`, `_query_moz_cookies_db`, `_read_profile_cookies`, `harvest_gemini_cookies_via_isolated_firefox`, `run_oauth_in_isolated_firefox` | Isolated Firefox session lifecycle, ephemeral profile generation, Windows detachment handling, and cookie extraction. |
| [`core/auth_oauth_cli.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/auth_oauth_cli.py) | `get_oauth_client_id`, `get_oauth_client_secret`, `authorize_oauth_via_isolated_firefox`, `_BUILTIN_CLI_CLIENT_ID_B64`, `_BUILTIN_CLI_CLIENT_SECRET_B64` | Built-in public CLI credentials and loopback OAuth flow via isolated Firefox. |
| [`core/auth_web.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/auth_web.py) | `extract_firefox_cookies`, `_extract_firefox_cookies_from_path`, `login_web_via_isolated_firefox`, `WebSessionManager.close_client`, `WebSessionManager.close_all` | Sidecar-aware cookie extraction, isolated Web login, and session teardown. |
| [`core/schema_sanitizer.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/schema_sanitizer.py) | `UNSUPPORTED_SCHEMA_KEYS`, `clean_json_schema`, `flatten_unions` | Protobuf JSON Schema sanitization (35 keys, `const` -> `enum`, `allOf` flattening). |
| [`core/model_registry.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/model_registry.py) | `_fallback_default_oauth_models`, `fetch_available_models`, `retrieve_user_quota`, `resolve_web_model`, `resolve_web_model_sync` | Anti-mocking quota enforcement, Header Shield routing, and suffix-aware model resolution. |
| [`core/prefix_differ.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/prefix_differ.py) | `_start_client_chat_session`, `_open_chat_message_stream`, `execute_rehydrated_stream` | `start_chat` signature compatibility, `extended_thinking` activation, and 3-tier rehydration. |
| [`core/usage_monitor.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/core/usage_monitor.py) | `_fetch_usage_from_gemini_client`, `_fetch_usage_via_direct_batchexecute`, `sync_web_account_usage` | Resilient usage monitoring supporting `session_manager` injection and direct `jSf9Qc` fallback. |
| [`api/dashboard_routes.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/api/dashboard_routes.py) | `trigger_web_browser_login`, `auto_detect_web_account`, `_initialize_and_discover_web_account` | Web Dashboard REST endpoints wired to isolated browser flows. |
| [`main.py`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/main.py) | `add_oauth`, `add_web`, `_prompt_for_web_cookies`, `_discover_and_persist_web_account` | CLI wizards integrating isolated Firefox flows. |
| [`static/index.html`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/static/index.html) | `startGoogleOAuth`, `autoDetectWebAccount`, `openGeminiInBrowser` | Web UI status badges, one-click isolated Firefox buttons, and accurate route labeling. |
| [`.env.example`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/.env.example) | `GEMINI_OAUTH_CLIENT_ID`, `GEMINI_OAUTH_CLIENT_SECRET`, `GHPG_DISABLE_BUILTIN_OAUTH_CLIENT` | Environment variable documentation for optional built-in credentials. |
| [`.gitignore`](file:///d:/Projects/Gemini-Hybrid-Pool-Gateway/.gitignore) | `config/accounts.json`, `config/accounts.json.*`, `config/model_routes.json` | Exclusion of runtime account credentials and custom model routes from version control. |

---

## Migration & Operational Notes

1. **Environment Variables**:
   - `GEMINI_OAUTH_CLIENT_ID` and `GEMINI_OAUTH_CLIENT_SECRET` are now **optional**. The gateway defaults to built-in public CLI credentials out-of-the-box.
   - To disable built-in credentials and mandate explicit credentials, set `GHPG_DISABLE_BUILTIN_OAUTH_CLIENT=1`.
2. **Browser Prerequisite**:
   - Mozilla Firefox must be installed on the host system, or pointed to via the `FIREFOX_BINARY_PATH` environment variable.
3. **Agent Configuration Compatibility**:
   - Tool calling payloads from Cline, Roo Code, and Cursor utilizing JSON Schema `exclusiveMinimum`, `const`, or `allOf` are now automatically sanitized without requiring manual client modifications.
