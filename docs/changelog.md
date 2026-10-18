# Changelog

All notable changes to the **Gemini-Hybrid-Pool-Gateway** (`v3.2` architecture) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-10-18

### Added

- **Isolated & Ephemeral Firefox Session Management ([`core/firefox_launcher.py`](core/firefox_launcher.py), [`tests/test_firefox_launcher.py`](tests/test_firefox_launcher.py)):**
  - Implemented `IsolatedFirefoxSession` providing isolated Firefox execution using temporary profiles (`create_isolated_firefox_profile`) with custom `user.js` preferences disabling default browser prompts, crash recovery dialogs, and telemetry notifications.
  - Added multi-platform binary resolution (`resolve_firefox_binary`) honoring `FIREFOX_BINARY_PATH`, `shutil.which`, and standard OS installation directories.
  - Implemented automated cookie harvesting (`harvest_gemini_cookies_via_isolated_firefox`) with WAL/SHM sidecar synchronization (`_read_profile_cookies`) and SQLite connection fallback (`_query_moz_cookies_db`), capturing `__Secure-1PSID` and `__Secure-1PSIDTS` with stabilization validation.
  - Added Windows detached process handling via `--wait-for-browser` flag and reliable process-tree termination using `taskkill /F /T /PID` alongside profile directory cleanup retries with lock backoff (`_cleanup_profile_directory`).
  - Added OAuth monitoring flow (`run_oauth_in_isolated_firefox`) tracking browser window liveliness against authorization code task completion.
- **Built-in Public Antigravity CLI OAuth Credentials ([`core/auth_oauth_cli.py`](core/auth_oauth_cli.py), [`.env.example`](.env.example)):**
  - Added base64-encoded public Antigravity CLI OAuth client credentials (`_BUILTIN_CLI_CLIENT_ID_B64`, `_BUILTIN_CLI_CLIENT_SECRET_B64`), making manual OAuth client creation and `.env` setup optional.
  - Added `GHPG_DISABLE_BUILTIN_OAUTH_CLIENT` environment variable toggle to disable built-in credentials when custom client isolation is required.
  - Added `authorize_oauth_via_isolated_firefox` coordinating ephemeral port binding and isolated browser launch.
- **Sidecar-Aware Cookie Extraction & Web Session Lifecycle Management ([`core/auth_web.py`](core/auth_web.py)):**
  - Added `login_web_via_isolated_firefox` for zero-interaction Gemini web cookie acquisition.
  - Hardened `extract_firefox_cookies` with uncheckpointed SQLite write-ahead log (`-wal`) and shared memory (`-shm`) sidecar cloning prior to database extraction.
  - Added `WebSessionManager.close_client` and `WebSessionManager.close_all` providing safe client shutdown, cache flushing, and cookie persistence.

### Fixed

- **Protobuf Schema Sanitization & HTTP 400 Prevention ([`core/schema_sanitizer.py`](core/schema_sanitizer.py), [`tests/test_level1_gate2.py`](tests/test_level1_gate2.py)):**
  - Resolved Google CloudCode PA `HTTP 400 INVALID_ARGUMENT` errors ("Cannot find field 'exclusiveMinimum'") by expanding `UNSUPPORTED_SCHEMA_KEYS` from 20 to 35 Protobuf/OpenAPI-incompatible meta-keys (including `exclusiveMinimum`, `exclusiveMaximum`, `const`, `propertyNames`, `dependencies`, `dependentRequired`, `dependentSchemas`, `unevaluatedProperties`, `unevaluatedItems`, `prefixItems`, `contentMediaType`, `contentEncoding`, `readOnly`, `writeOnly`, `deprecated`).
  - Implemented `const` to single-item `enum` normalization in `clean_json_schema` for Protobuf schema compliance.
  - Extended union flattening in `flatten_unions` to support `allOf` intersection schema blocks alongside `anyOf` and `oneOf`.
- **Purge of Fake Models & Fabricated Quotas ([`core/model_registry.py`](core/model_registry.py), [`static/index.html`](static/index.html)):**
  - Eliminated synthetic `remaining_fraction=1.0` quota fabrication in `_apply_fetch_models_quotas` and `_fallback_default_oauth_models`; model quotas remain empty until genuine quota buckets are returned by Google API.
  - Prevented malformed JSON request payloads (`{"project": ""}`) in `fetch_available_models` and `retrieve_user_quota` when account project IDs are absent.
  - Added Header Shield routing in `_build_oauth_headers` via `build_v1internal_headers`.
- **ChatSession Compatibility & Reasoning Effort Parameter Handling ([`core/prefix_differ.py`](core/prefix_differ.py), [`tests/test_audit_verified_fixes.py`](tests/test_audit_verified_fixes.py)):**
  - Fixed `TypeError: ChatSession.__init__() got an unexpected keyword argument 'temporary'` in `_start_client_chat_session` by directing `temporary` to `chat.send_message_stream`.
  - Added automatic `extended_thinking=True` activation in `_open_chat_message_stream` for `reasoning_effort in ("high", "medium")`.
- **Web Usage Synchronization Resilience ([`core/usage_monitor.py`](core/usage_monitor.py)):**
  - Hardened `sync_web_account_usage` to accept optional `session_manager` parameter, supporting `inspect.isawaitable` in `_fetch_usage_from_gemini_client` with seamless fallback to direct `jSf9Qc` batchexecute RPC calls.
- **Interactive UI & CLI Browser Decoupling ([`api/dashboard_routes.py`](api/dashboard_routes.py), [`main.py`](main.py), [`static/index.html`](static/index.html)):**
  - Replaced legacy `webbrowser.open` calls across `main.py`, `api/dashboard_routes.py`, and `static/index.html` with isolated Firefox workflows.
  - Updated `POST /api/web-accounts/browser-login` to asynchronously harvest credentials and link accounts via isolated Firefox.

### Changed

- **Version Control & Credentials Hygiene ([`.gitignore`](.gitignore)):**
  - Added `config/accounts.json`, `config/accounts.json.*`, and `config/model_routes.json` to `.gitignore` to prevent inadvertent tracking of runtime credentials and dynamic routes.
- **Architectural Documentation ([`docs/RELEASE_NOTES_v0.2.2.md`](docs/RELEASE_NOTES_v0.2.2.md)):**
  - Published comprehensive technical release notes detailing isolated browser architecture, schema sanitization mechanics, and anti-mocking verification.

## [0.2.1] - 2026-10-17

### Added

- **Automated Zero-Copy Browser OAuth Authentication ([`api/dashboard_routes.py`](../api/dashboard_routes.py), [`main.py`](../main.py), [`static/index.html`](../static/index.html)):**
  - Added `@router.get("/oauth/callback")` endpoint in `api/dashboard_routes.py` with automatic authorization code capture, PKCE challenge completion, live model discovery, and auto-closing success popup with cross-window `postMessage` synchronization.
  - Added automatic browser launching (`webbrowser.open`) on CLI invocation `python main.py --add-oauth` with an ephemeral loopback HTTP callback listener, removing manual copy-paste of OAuth redirect URLs.
  - Integrated 1-Click "Sign in with Google" button in the Web Dashboard UI (`static/index.html`) opening a popup OAuth window with real-time account status updates.
- **Automated Zero-Input Web Cookie Login & Firefox Profile Auto-Detection ([`core/auth_web.py`](../core/auth_web.py), [`api/dashboard_routes.py`](../api/dashboard_routes.py), [`main.py`](../main.py), [`static/index.html`](../static/index.html)):**
  - Implemented `auto_discover_firefox_cookies()` in `core/auth_web.py` auto-detecting Firefox user profiles across Windows (`%APPDATA%/Mozilla/Firefox/Profiles/*.default-release`), Linux, and macOS, with WAL/SHM sidecar synchronization and fallback to `browser_cookie3`.
  - Added REST endpoints `POST /api/web-accounts/auto-detect` and `POST /api/web-accounts/browser-login` for headless and automated session cookie extraction.
  - Integrated 1-Click "Auto-Detect Firefox" button in Web Dashboard UI and CLI auto-discovery on `python main.py --add-web --mode auto`.
- **Test Coverage Expansion ([`tests/test_auth_web.py`](../tests/test_auth_web.py), [`tests/test_dashboard_routes.py`](../tests/test_dashboard_routes.py), [`tests/test_main_cli.py`](../tests/test_main_cli.py), [`tests/test_openai_routes.py`](../tests/test_openai_routes.py)):**
  - Added 18 new unit and integration tests across web auth auto-discovery, OAuth callback workflows, CLI argument combinations, and live model discovery routes.
  - Total test suite coverage expanded to **386 passing tests** (up from 368 in v0.2.0).

### Fixed

- **CLI Compatibility & Argument Signature Alignment ([`main.py`](../main.py)):**
  - Synchronized CLI argument signatures in `main.py` for `add_oauth` and `add_web` to accept `--label`, `--mode`, `--firefox-profile`, and `--id`, eliminating `TypeError: add_oauth() got an unexpected keyword argument 'mode'`.
- **Purged Mock & Hardcoded Models from Dashboard and APIs ([`core/model_registry.py`](../core/model_registry.py), [`api/dashboard_routes.py`](../api/dashboard_routes.py), [`static/index.html`](../static/index.html)):**
  - Removed hardcoded `DEFAULT_STANDARD_MODELS` from `UnifiedModelRegistry.get_base_models()`.
  - When 0 accounts are registered, `GET /v1/models` and dashboard display `0 Models` and clean `EMPTY` pool status instead of fake placeholder models.
  - Ensured all models displayed on dashboard and returned via OpenAI API are strictly and 100% discovered from live, registered accounts.

### Changed

- **Production Documentation & Verification ([`README.md`](../README.md)):**
  - Updated test suite badge and verification status to `386 passed`.

## [0.2.0] - 2026-10-17

### Added

- **Dynamic Model Routing & Compute Allocation Engine ([`core/hybrid_scheduler.py`](../core/hybrid_scheduler.py), [`config/settings.py`](../config/settings.py)):**
  - Implemented dynamic runtime model route configuration model [`ModelRouteConfig`](../config/settings.py) supporting customizable `primary_channel` (`oauth` or `web`), optional `failover_channel` (`oauth`, `web`, or `none`), and custom aliases.
  - Implemented persistent atomic JSON storage [`save_custom_model_routes`](../config/settings.py) and [`async_save_custom_model_routes`](../config/settings.py) managing `config/model_routes.json` with file locking and atomic rename, seamlessly falling back to default built-in routing rules.
  - Updated [`get_model_channel_preference`](../core/hybrid_scheduler.py) to resolve model routes dynamically: checking explicit custom route overrides first, stripping reasoning effort suffixes (`-minimal`, `-low`, `-medium`, `-high`), resolving prefix patterns, and falling back to default channel strategies (`gemini-4-argon` -> Web only; Pro models -> Web first with OAuth failover; Flash models -> OAuth first with Web failover).
  - Enhanced [`select_optimal_account`](../core/hybrid_scheduler.py) to dynamically honor configured primary and failover channels without hardcoded assumptions.
- **REST Endpoints for Model Routing ([`api/dashboard_routes.py`](../api/dashboard_routes.py)):**
  - Implemented dynamic route query endpoint `GET /api/model-routes` returning all active dynamic model routing configurations and default fallbacks.
  - Implemented dynamic route creation and update endpoint `POST /api/model-routes` accepting runtime channel preferences (`primary`, `failover`) and persisting changes to disk.
  - Implemented dynamic route deletion endpoint `DELETE /api/model-routes/{model}` reverting the specified model to default gateway scheduling behavior.
- **Interactive Web Dashboard Section for Model Routing ([`static/index.html`](../static/index.html)):**
  - Added dedicated Dynamic Model Routing management panel with live table of configured models, primary channels, failover policies, and actions.
  - Added interactive modal dialogs for creating, modifying, and deleting model routes with real-time UI feedback.
  - Added visual channel indicator badges (`OAuth CLI Pool` in cyan, `Web Cookie Pool` in emerald, `None (Strict)` in rose) and live quota statistics.
- **Comprehensive Production Documentation ([`README.md`](../README.md)):**
  - Overhauled and expanded `README.md` into a production-grade architectural guide covering system design, mathematical scheduling proofs, zero-drop failover resilience, 3-tier context rehydration, CLI management, REST API endpoints, and agent harness configurations (Pi, Hermes, Cursor).
- **Test Suites for Audit Remediations and Routing ([`tests/test_audit_verified_fixes.py`](../tests/test_audit_verified_fixes.py), [`tests/test_model_routing_dynamic.py`](../tests/test_model_routing_dynamic.py)):**
  - Added `tests/test_audit_verified_fixes.py` (7 tests) verifying critical defect fixes across Control, Auth, Protocol, and Interface planes.
  - Added `tests/test_model_routing_dynamic.py` (15 tests) verifying dynamic model routing persistence, prefix resolution, custom aliases, failover bypass, and REST API endpoints.
  - Total test suite coverage expanded to **368 passing tests** (up from 345 in v0.1.4).

### Fixed

- **Architectural Audit Remediations Across Operational Planes ($H\text{-}1.1$ through $H\text{-}4.5$):**
  - **Control Plane ($H\text{-}1.1$ – $H\text{-}1.5$):**
    - $H\text{-}1.1$: Fixed `FailoverController` instantiation of `WebChannel` to properly wire `WebSessionManager`, ensuring valid cookie session lifecycles.
    - $H\text{-}1.2$: Added `track_concurrency=False` flag to `execute_web_channel_stream` to eliminate double-incrementing of `active_requests` counter when called by `FailoverController`.
    - $H\text{-}1.3$: Added automatic latency EWMA updates, consecutive error clearing, and context-scaled optimistic delta accounting upon stream completion in `FailoverController`.
    - $H\text{-}1.4$: Guaranteed zero concurrency counter leaks across client disconnects, timeouts, and cancellations in `execute_stream_with_failover`.
    - $H\text{-}1.5$: Fixed anti-bot smart pacing race condition by reserving future execution time slots before awaiting sleep, properly staggering concurrent requests.
  - **Auth & Quota Plane ($H\text{-}2.1$ – $H\text{-}2.5$):**
    - $H\text{-}2.1$: Hardened per-account OAuth token refresh deduplication lock and atomic state serialization.
    - $H\text{-}2.2$: Enhanced Google `batchexecute` / `jSf9Qc` response parser to handle leading byte-length framing lines (`128\n`) before JSON payload.
    - $H\text{-}2.3$: Implemented drift-safe quota reset clearing optimistic deltas immediately upon expiration of 5-hour and weekly reset timestamps.
    - $H\text{-}2.4$: Enforced isolated cookie cache persistence with atomic rename and strict path traversal protections in `config/cookies_cache/`.
    - $H\text{-}2.5$: Prevented concurrent session destruction in `WebSessionManager.safe_reinit_or_close` by raising `ActiveRequestsConcurrencyError` when active streams exist.
  - **Protocol & Data Plane ($H\text{-}3.1$ – $H\text{-}3.7$):**
    - $H\text{-}3.1$: Fixed hash normalization discrepancy between persisted assistant XML `<tool_call>` and incoming OpenAI `tool_calls` dictionary for deterministic prefix matching.
    - $H\text{-}3.2$: Hardened 3-Tier Context Rehydration to cleanly resolve and unlink temporary history files on completion or exception.
    - $H\text{-}3.3$: Enhanced recursive JSON Schema sanitizer to resolve local `$ref` / `$defs` schemas and prune orphaned keys from `required`.
    - $H\text{-}3.4$: Fixed ADR-05 zero-required tool schema handling, ensuring synthetic `_step` injection occurs only when properties are truly empty.
    - $H\text{-}3.5$: Guaranteed cryptographic `thoughtSignature` preservation and restoration across multi-turn tool calling loops.
    - $H\text{-}3.6$: Strictly filtered out `"requestType": "agent"` from `v1internal` envelopes to prevent false `429 RESOURCE_EXHAUSTED` rejections.
    - $H\text{-}3.7$: Hardened base64 image detector in tool outputs to prevent false positive classification of text strings exceeding 32 characters.
  - **Interface & Transport Plane ($H\text{-}4.1$ – $H\text{-}4.5$):**
    - $H\text{-}4.1$: Replaced rigid model-to-channel bindings with dynamic runtime route registry and fallback engine.
    - $H\text{-}4.2$: Added CLI compatibility flags (`--serve`, `--port`, `--status`, `--add-oauth`, `--add-web`, `--firefox-profile`).
    - $H\text{-}4.3$: Fixed `AgySubprocessTransport` pipe deadlock by adding background `stderr` draining task for outputs exceeding 64KB.
    - $H\text{-}4.4$: Ensured permissive Pydantic schema validation on chat completion endpoints to accept arbitrary agent harness parameters without 422 errors.
    - $H\text{-}4.5$: Added real-time model routing management and health badge telemetry to the web dashboard UI.

### Changed

- **Runtime Model Routing & Compute Allocation:**
  - Transitioned from hardcoded model routing to dynamic runtime preference engine with persistent fallback in `config/model_routes.json`.
  - Enabled client and agent harnesses to configure primary and failover channels per model at runtime via REST API or Web Dashboard.

---

## [0.1.4] - 2026-10-17

### Added

- **OpenAI Protocol Engine & Public Endpoints — Level 5 ([`api/openai_routes.py`](../api/openai_routes.py)):**
  - Implemented dynamic models catalog endpoints [`list_models`](../api/openai_routes.py#L182-L188) (`GET /v1/models`) and [`get_model`](../api/openai_routes.py#L190-L208) (`GET /v1/models/{model_id}`) dynamically aggregating discovered base models and reasoning effort suffixes (`-minimal`, `-low`, `-medium`, `-high`), translating model names via [`resolve_effective_model`](../api/openai_routes.py#L132-L144), and returning standard RFC 7807/OpenAI 404 error payloads (`invalid_request_error`, `code: model_not_found`) via [`make_openai_error_response`](../api/openai_routes.py#L106-L123) for unregistered model identifiers.
  - Implemented unified chat completion route [`create_chat_completion`](../api/openai_routes.py#L210-L279) (`POST /v1/chat/completions`) supporting both non-streaming and streaming execution modes with failover orchestration and permissive Pydantic schema [`ChatCompletionRequest`](../api/openai_routes.py#L83-L99) accepting arbitrary client parameters without 422 rejections.
  - Implemented true Server-Sent Events (SSE) streaming delivery via [`sse_event_stream`](../api/openai_routes.py#L155-L175) and [`format_sse_chunk`](../api/openai_routes.py#L146-L153) returning standard HTTP streaming headers (`Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`), streaming formatted chunks (`data: {...}\n\n`), concluding with mandatory `data: [DONE]\n\n`, and guaranteeing **Zero Concurrency Leak** by strictly releasing in-flight concurrency counters ($C = 0$) upon stream completion, cancellation, or exception.
  - Integrated RTK-Lite tool output compressor [`sanitize_tool_messages`](../core/token_saver.py#L478-L494) controlled via the `X-Token-Saver` HTTP request header ([`should_enable_token_saver`](../core/token_saver.py#L355-L372)), compressing tool responses by default to conserve context window while supporting per-request bypass (`X-Token-Saver: off`) for 100% verbatim byte fidelity.
  - Implemented standard OpenAI error mapping for gateway exceptions: translating [`ModelNotSupportedError`](../core/failover.py#L86-L87) to HTTP 404 (`code: model_not_found`), and [`PoolExhaustedError`](../core/failover.py#L78-L79) to HTTP 429 (`code: pool_exhausted`).
- **Control Plane & Real-Time Telemetry API — Level 5 ([`api/dashboard_routes.py`](../api/dashboard_routes.py)):**
  - Implemented real-time gateway telemetry endpoint [`get_gateway_status`](../api/dashboard_routes.py#L315-L346) (`GET /api/status`) computing pool health badges (`HEALTHY`, `DEGRADED`, `EMPTY`, `DOWN`) via [`_determine_pool_status`](../api/dashboard_routes.py#L161-L170), tracking active accounts, cluster concurrency ($C_{\text{total}}$), available models, and per-account mathematical scheduling metrics: active requests $C$, priority score $P$, urgency factor $\tau \in [1.0, 1.5]$, session affinity $S_i \in \{1.0, 1.35\}$, and conditional concurrency denominator $D_{\text{conc}}$ via [`_build_oauth_account_telemetry`](../api/dashboard_routes.py#L194-L220) and [`_build_web_account_telemetry`](../api/dashboard_routes.py#L222-L264).
  - Implemented OAuth account management lifecycle: PKCE initialization [`init_oauth_account`](../api/dashboard_routes.py#L348-L369) (`POST /api/oauth-accounts/init`) generating cryptographic challenge pairs and pending sessions, token exchange and account registration [`complete_oauth_account`](../api/dashboard_routes.py#L371-L425) (`POST /api/oauth-accounts/complete`) with dynamic model discovery and atomic snapshot persistence, and account removal [`delete_oauth_account`](../api/dashboard_routes.py#L426-L436) (`DELETE /api/oauth-accounts/{account_id}`).
  - Implemented Web account management lifecycle: account ingestion [`add_web_account`](../api/dashboard_routes.py#L438-L467) (`POST /api/web-accounts/add`) supporting Firefox SQLite cookie extraction, raw cookie headers, or manual `__Secure-1PSID`/`__Secure-1PSIDTS` input, isolated per-account cookie caching in `config/cookies_cache/`, and account removal [`delete_web_account`](../api/dashboard_routes.py#L469-L486) (`DELETE /api/web-accounts/{account_id}`) with automatic unlinking of isolated cookie files.
  - Implemented immediate quota synchronization endpoint [`sync_accounts_quotas`](../api/dashboard_routes.py#L488-L496) (`POST /api/accounts/sync-quotas`) executing a pool-wide sync pass via `sync_all_registered_accounts`.
  - Implemented visual dashboard fallback route [`serve_dashboard_ui`](../api/dashboard_routes.py#L503-L512) (`GET /dashboard`) serving `static/index.html`.
- **Modern Dark Mode Control Dashboard UI — Level 5 ([`static/index.html`](../static/index.html)):**
  - Designed and built a responsive dark-themed dashboard UI (`--bg-dark: #070b12`, `--bg-card: #0f172a`, with cyan, emerald, amber, and rose accent glow effects) for real-time cluster monitoring and operations.
  - Implemented dynamic top-level KPI metrics grid: Pool Health status badge, Active Accounts ratio, In-Flight Concurrency ($C_{\text{total}}$), and Supported Models count.
  - Implemented **Quick Agent Connect** card providing one-click copyable configuration snippets for AI agent environments (Pi, Hermes, Cursor) with pre-configured OpenAI-compatible base URL (`http://localhost:8000/v1`), API key (`sk-gemini-hybrid-pool`), and multi-tier model routing specification JSON (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.1-flash-lite`).
  - Implemented dual visual pool grids for OAuth CLI accounts and Web Cookie accounts displaying real-time status indicators, model quotas, 5-hour and weekly quota consumption progress bars, urgency factor $\tau$, priority scores, and account deletion actions.
  - Built interactive modal wizards for adding OAuth accounts (step-by-step Google OAuth PKCE flow) and Web accounts (Firefox SQLite import, raw cookies, or manual PSID entry).
- **Gateway Server Lifespan & Click CLI Engine — Level 5 ([`main.py`](../main.py)):**
  - Implemented production FastAPI application factory [`create_app`](../main.py#L144-L178) with asynchronous [`lifespan`](../main.py#L86-L142) context manager orchestrating startup initialization (memory registry loading via `load_accounts_into_memory`, orphaned temp file pruning via `cleanup_orphaned_temp_files`) and configuring background `AsyncIOScheduler` executing 3 periodic tasks:
    1. `sync_quotas`: quota and usage synchronization across all registered accounts every 5 minutes (`sync_all_registered_accounts`).
    2. `flush_cookies`: periodic cookie persistence every 5 minutes ([`flush_all_cookies`](../core/auth_web.py#L778-L812)).
    3. `cleanup_chats`: garbage collection of idle web chat sessions and orphaned temp history files every 10 minutes ([`cleanup_expired_chats`](../core/auth_web.py#L883-L892)).
  - Implemented graceful shutdown sequence safely stopping background jobs (`wait=False`), flushing updated session cookies, and committing an atomic snapshot of accounts state to disk (`save_accounts_snapshot`).
  - Mounted static asset directory at `/static` and exposed dashboard routes at `/` and `/dashboard` via [`FileResponse`](../main.py#L168-L175).
  - Built comprehensive Click CLI management engine with 6 subcommands:
    - [`serve`](../main.py#L306-L314): launches Uvicorn ASGI server with configurable `--host`, `--port`, and `--reload`.
    - [`list-accounts`](../main.py#L316-L320): renders formatted ASCII table of registered accounts, channel types, statuses, priority scores, and quota percentages.
    - [`sync-quotas`](../main.py#L322-L335): triggers immediate manual quota synchronization across all accounts from the command line.
    - [`status`](../main.py#L337-L341) and [`health`](../main.py#L343-L348): displays pool health summary, channel breakdown, concurrency, and available model catalog.
    - [`add-oauth`](../main.py#L350-L402): interactive CLI wizard for Google OAuth PKCE authorization.
    - [`add-web`](../main.py#L404-L462): interactive CLI wizard for Web Cookie account registration from Firefox or manual input.
- **Mathematical Hybrid Routing & Scheduling Engine — ADR-07 ([`core/hybrid_scheduler.py`](../core/hybrid_scheduler.py)):**
  - Implemented strict boundary eligibility gates [`is_oauth_account_eligible`](../core/hybrid_scheduler.py#L117-L169) ($G_{\text{oauth}}$) and [`is_web_account_eligible`](../core/hybrid_scheduler.py#L172-L214) ($G_{\text{web}}$) enforcing zero priority ($G=0$) upon quota exhaustion, active cooldown windows, active concurrency saturation ($C \ge C_{\text{max}}$), or consumption threshold violations (5-hour usage $\ge 95\%$ via `WEB_5H_CEILING = 0.95`, weekly usage $\ge 98\%$ via `WEB_WEEKLY_CEILING = 0.98`, and OAuth model remaining fraction $< 2\%$ via `OAUTH_MIN_REMAINING_FRACTION = 0.02`).
  - Implemented mathematical session affinity proof (**ADR-07**) via [`calculate_session_affinity`](../core/hybrid_scheduler.py#L245-L270) ($S_i = 1.35$) and conditional concurrency denominator [`calculate_web_concurrency_denominator`](../core/hybrid_scheduler.py#L273-L291): active sessions on an in-flight account ($C=1, S_i = 1.35$) evaluate to $D_{\text{conc}} = 1 + 0.25(1) = 1.25 \implies S / D_{\text{conc}} = 1.35 / 1.25 = \mathbf{1.08}$, mathematically surpassing an idle competing account ($C=0, S=1.0 \implies S / D_{\text{conc}} = \mathbf{1.00}$) by $1.08 > 1.00$, while applying quadratic penalty $D_{\text{conc}} = (1+C)^2 = 4.0 \implies S / D_{\text{conc}} = \mathbf{0.25} \ll 1.00$ for new conversations to automatically push traffic to idle accounts.
  - Implemented continuous window-bounded urgency multiplier [`calculate_soonest_reset_urgency`](../core/hybrid_scheduler.py#L222-L243) dynamically scaling $\tau \in [1.0, 1.5]$ over `WEB_URGENCY_WINDOW_S = 18_000s` ($5\text{h}$) to prioritize accounts nearing quota refresh ($\tau = 1.0 + 0.5 \times (1.0 - \min(W, \max(0, T_{\text{reset}} - \text{now})) / W)$), and enforced session affinity rotation limit at `SESSION_ROTATION_CHAR_LIMIT = 120_000` characters to prevent context drift.
  - Implemented holistic scoring models [`calculate_oauth_account_score`](../core/hybrid_scheduler.py#L293-L330) ($P_{\text{oauth}} = G \times \tau \times Q / ((1 + C) \times (1 + 0.001 L))$) and [`calculate_web_account_score`](../core/hybrid_scheduler.py#L333-L379) ($P_{\text{web}} = G \times S \times \tau \times (0.95 - U_{5h}) \times (0.98 - U_w) / D_{\text{conc}}$).
  - Implemented anti-bot smart pacing [`calculate_smart_pacing_delay`](../core/hybrid_scheduler.py#L386-L428) and async wrapper [`apply_smart_pacing`](../core/hybrid_scheduler.py#L430-L446) enforcing a 0.5s minimum start-to-start interval (`WEB_MIN_START_INTERVAL_S = 0.5`) and a 0.6s cooldown (`WEB_MIN_INTERVAL_S = 0.6`) with optional jitter in $[0.4\text{s}, 0.8\text{s}]$ (`WEB_JITTER_RANGE_S`) following sub-second fast requests ($< 1.0\text{s}$ via `WEB_FAST_REQUEST_THRESHOLD_S`), with zero post-completion cooldown for normal long-running streams ($\ge 1.0\text{s}$), without zeroing account eligibility gates.
  - Implemented cross-channel routing preference resolver [`get_model_channel_preference`](../core/hybrid_scheduler.py#L76-L96) and pool selector [`select_optimal_account`](../core/hybrid_scheduler.py#L453-L530): routing `gemini-4-argon` strictly and exclusively to Web (raising [`NoEligibleAccountError`](../core/hybrid_scheduler.py#L72-L73) if Web accounts are unavailable, strictly prohibiting OAuth fallback), routing Flash models (`gemini-3.8-flash`, `gemini-3.1-flash-lite`) to OAuth first with automatic Web fallback, and routing Pro models (`gemini-3.1-pro`, `gemini-2.5-pro`) to Web first with OAuth fallback.
- **Zero-Drop Pre-Stream Handshake & Instant Failover Engine — ADR-10 ([`core/failover.py`](../core/failover.py)):**
  - Implemented proactive semantic content validator [`is_chunk_non_empty`](../core/failover.py#L104-L145) inspecting streaming chunks (supporting dicts, Pydantic `StreamChunk`, and raw attributes) for genuine payload in `content`, `reasoning_content`, or `tool_calls` while safely ignoring role-only, empty, or metadata chunks.
  - Implemented memory-bounded pre-stream buffer holding up to `HANDSHAKE_MAX_BUFFER_CHUNKS = 15` chunks, intercepting Zero-Byte Stream EOF, HTTP 429 rate limits, HTTP 403, and network disconnects before FastAPI commits HTTP 200 response headers to the client.
  - Implemented chained async stream generator [`chain_async_iterators`](../core/failover.py#L193-L223) ensuring seamless delivery of buffered handshake chunks followed by the active stream, with guaranteed execution of `on_complete` cleanup callback in a `finally:` block across normal completion, early client break, cancellations, and exceptions, providing **Zero Concurrency Leak** protection.
  - Implemented central failover coordinator [`FailoverController`](../core/failover.py#L230-L670) with [`execute_stream_with_failover`](../core/failover.py#L327-L519) and top-level helper [`execute_stream_with_failover`](../core/failover.py#L679-L709): instantly decrementing `account.active_requests`, recording 429 cooldowns via `UsageMonitor`, excluding the failed account via `excluded_ids`, and transparently failing over to the next optimal candidate account without client-side interruption.
  - Implemented complete non-streaming chat completion failover [`execute_chat_completion_with_failover`](../core/failover.py#L520-L670) (aliased as module function [`execute_chat_completion_with_failover`](../core/failover.py#L712-L742)) assembling text, reasoning thoughts, and multi-tool invocations with mid-stream failover resilience.
  - Added specialized gateway exceptions [`PoolExhaustedError`](../core/failover.py#L78-L79), [`HandshakeFailedError`](../core/failover.py#L82-L83), and [`ModelNotSupportedError`](../core/failover.py#L86-L87) alongside model validation [`is_model_supported_in_gateway`](../core/failover.py#L148-L185).
- **Package Re-exports ([`core/__init__.py`](../core/__init__.py)):**
  - Re-exported all 23 Level 4 public symbols in `__all__`, bringing the total exported public interface to 144 symbols across all architectural levels (0 through 4).
- **Tooling & Test Infrastructure ([`pytest.ini`](../pytest.ini)):**
  - Configured project-wide `pytest.ini` with strict asyncio execution mode (`asyncio_mode = strict`) and warning filters ignoring `StarletteDeprecationWarning` from test client imports (`ignore::starlette.exceptions.StarletteDeprecationWarning`), guaranteeing 100% clean test runner output without noise.

### Refactored

- **Defensive Error Handling ([`core/usage_monitor.py`](../core/usage_monitor.py)):**
  - Hardened [`extract_quota_reset_delay_from_error`](../core/usage_monitor.py#L247-L265) against mock objects and non-standard response payloads by verifying return types are primitive (`isinstance(body, (dict, list, str))`) before recursive invocation, eliminating recursion depth errors.
- **Web Session Management ([`core/auth_web.py`](../core/auth_web.py)):**
  - Added [`cleanup_expired_chats`](../core/auth_web.py#L883-L892) helper method to `WebSessionManager` combining idle session pruning and orphaned temporary history file cleanup for periodic scheduler execution.

### Verified

- **Verification Gate 5 ([`tests/test_level4_gate5.py`](../tests/test_level4_gate5.py), [`tests/test_hybrid_scheduler.py`](../tests/test_hybrid_scheduler.py), [`tests/test_failover.py`](../tests/test_failover.py)):**
  - Added 25 official Gate 5 integration test cases in [`tests/test_level4_gate5.py`](../tests/test_level4_gate5.py), 20 unit tests in [`tests/test_hybrid_scheduler.py`](../tests/test_hybrid_scheduler.py), and 23 tests in [`tests/test_failover.py`](../tests/test_failover.py) (**68 Level 4 tests total**).
  - Verified mathematical proof of session affinity ($1.08 > 1.00$), quadratic penalty for new conversations ($D=4.0$), continuous urgency factor $\tau$, strict boundary zeroing for $G_{\text{oauth}}$ and $G_{\text{web}}$, smart pacing intervals and jitter, cross-channel routing rules and Argon exclusivity, semantic chunk detection, chained stream generators, zero-byte EOF failover, HTTP 429 instant recovery, zero concurrency leaks, and pool exhaustion guarantees.
- **Verification Gate 6 ([`tests/test_level5_gate6.py`](../tests/test_level5_gate6.py), [`tests/test_openai_routes.py`](../tests/test_openai_routes.py), [`tests/test_dashboard_routes.py`](../tests/test_dashboard_routes.py), [`tests/test_main_cli.py`](../tests/test_main_cli.py)):**
  - Added 16 official E2E Gate 6 test cases in [`tests/test_level5_gate6.py`](../tests/test_level5_gate6.py), 12 tests in [`tests/test_openai_routes.py`](../tests/test_openai_routes.py), 21 tests in [`tests/test_dashboard_routes.py`](../tests/test_dashboard_routes.py), and 17 tests in [`tests/test_main_cli.py`](../tests/test_main_cli.py) (**66 Level 5 tests total**).
  - Verified full lifespan startup and graceful shutdown, state registry snapshot roundtrip persistence with 100% fidelity, OpenAI models catalog with thinking suffixes and 404 error formatting, chat completion non-streaming failover recovery, streaming SSE delivery with `data: [DONE]`, zero concurrency leak invariants ($C = 0$ on stream completion), `X-Token-Saver` header tool output compression control, standard error handling, real-time dashboard telemetry ($C, P, \tau, S_i, D_{\text{conc}}$), OAuth and Web account CRUD lifecycle, isolated cookie caching, web UI presentation routes, and CLI commands (`list-accounts`, `status`, `health`, `sync-quotas`).
  - Achieved cumulative 100% pass rate across entire repository: **345/345 passed in 13.15s** with 0 failures, 0 warnings, and 0 regressions.

---

## [0.1.3] - 2026-10-17

### Added

- **OAuth CLI Channel & Protocol Gateway — ADR-04, ADR-05, ADR-06 ([`core/oauth_cli_channel.py`](../core/oauth_cli_channel.py)):**
  - Implemented production `v1internal` request envelope builder [`build_v1internal_envelope`](../core/oauth_cli_channel.py#L481-L542) strictly prohibiting `"requestType": "agent"` (**ADR-04**) to eliminate false 429 quota exhaustion errors.
  - Implemented multi-turn role converter [`convert_messages_to_v1internal_contents`](../core/oauth_cli_channel.py#L307-L473) re-injecting `_step: "done"` into historical `functionCall.args` for zero-property tools (**ADR-05**), fetching cryptographic `thoughtSignature` from `LMDBStore.db_signatures` or defaulting to `skip_thought_signature_validator` (**ADR-06**), nesting tool output images inside `functionResponse.parts` as `inlineData`, and applying the Post-Assembly Model-Turn Guard.
  - Implemented SSE demuxer and OpenAI chunk stream transformer [`transform_candidate_to_openai_chunks`](../core/oauth_cli_channel.py#L571-L690) streaming reasoning thoughts as `delta.reasoning_content`, regular text as `delta.content`, and tool calls as `delta.tool_calls` (stripping synthetic `_step` and automatically persisting emitted `thoughtSignature` tokens to `LMDBStore.db_signatures` under `tool_call_id`).
  - Implemented high-level client [`OAuthCLIChannel`](../core/oauth_cli_channel.py#L820-L959) supporting both HTTP/2 streaming via `httpx.AsyncClient` and subprocess streaming via `AgySubprocessTransport`.
- **Web Cookie Pool Channel & Lookahead Parser — ADR-09 ([`core/web_channel.py`](../core/web_channel.py)):**
  - Implemented [`MultiToolLookaheadParser`](../core/web_channel.py#L305-L536) state machine enforcing **ADR-09**: zero post-tool delimiter or fence content leakage, lookahead token buffering (`capacity=64`) across fragmented chunk boundaries, and extraction of sequential multi-tool invocations with unique `call_{uuid}` IDs and 0-based indices.
  - Implemented context-weighted 5-hour quota delta accounting [`compute_context_scaled_delta_5h`](../core/web_channel.py#L542-L557): $\Delta_{5h}(m, L) = \Delta_{\text{base}}(m) \times (1.0 + 0.25 \times (L / 25000))$, capped at 100.0.
  - Implemented streaming execution wrapper [`execute_web_channel_stream`](../core/web_channel.py#L613-L715) dispatching live thoughts immediately to avoid TTFT timeouts, tracking in-flight concurrency, and updating `account.optimistic_5h_delta` and `account.last_completed_timestamp` upon stream completion.
  - Implemented channel adapter [`WebChannel`](../core/web_channel.py#L722-L777) integrating with `WebSessionManager`.
- **Proactive Usage Monitor & Anti-Drift Engine — ADR-02 ([`core/usage_monitor.py`](../core/usage_monitor.py)):**
  - Implemented duration string parser [`parse_quota_reset_delay`](../core/usage_monitor.py#L166-L209) and deep extractor [`extract_quota_reset_delay_from_error`](../core/usage_monitor.py#L211-L263) supporting diverse time units (`"120s"`, `"5m"`, `"2h"`, `"1h30m"`, numeric values).
  - Implemented intelligent HTTP 429 evaluator [`classify_and_handle_oauth_429`](../core/usage_monitor.py#L265-L324) putting accounts with reset delays into `COOLDOWN` and atomically persisting state to disk, while treating delay-less 429s as transient bursts without long cooldowns.
  - Implemented permanent suspension handler [`handle_account_suspension`](../core/usage_monitor.py#L342-L366) and pattern matcher [`is_suspension_error`](../core/usage_monitor.py#L331-L340) for Terms of Service rejections and disabled accounts.
  - Implemented Google RPC `jSf9Qc` response parser [`parse_jsf9qc_usage_response`](../core/usage_monitor.py#L385-L567) with `)]}'\n` XSSI stripping, quota percentage extraction, and subscription tier mapping (`FREE`, `PRO`, `ULTRA`).
  - Implemented **ADR-02** drift-safe reset zeroing [`check_and_apply_drift_safe_resets`](../core/usage_monitor.py#L574-L619) zeroing optimistic deltas whenever `now >= reset_time`, and [`apply_web_usage_update`](../core/usage_monitor.py#L621-L650) clearing local deltas on fresh live queries.
  - Implemented request metrics recorder [`record_successful_request`](../core/usage_monitor.py#L812-L853) tracking Exponential Moving Average (EMA) latency ($\alpha = 0.2$), and central maintenance pass [`sync_all_registered_accounts`](../core/usage_monitor.py#L855-L943).
  - Implemented service facade [`UsageMonitor`](../core/usage_monitor.py#L946-L1070).
- **Package Re-exports ([`core/__init__.py`](../core/__init__.py)):**
  - Exported all 121 public Level 1, Level 2, and Level 3 symbols in `__all__`, resolving naming collisions (`format_web_openai_sse_event`) and submodule access hygiene (`default_oauth_cli_channel`).

### Verified

- **Verification Gate 4 ([`tests/test_level3_gate4.py`](../tests/test_level3_gate4.py), [`tests/test_oauth_cli_channel.py`](../tests/test_oauth_cli_channel.py), [`tests/test_web_channel.py`](../tests/test_web_channel.py), [`tests/test_usage_monitor.py`](../tests/test_usage_monitor.py)):**
  - Added 28 comprehensive integration test cases in [`tests/test_level3_gate4.py`](../tests/test_level3_gate4.py) alongside 53 unit tests in the channel and monitor test suites (**81 Level 3 tests total**).
  - Verified 100% pass rate across entire repository: **210/210 passed in 7.54s** with zero regressions.

---

## [0.1.2] - 2026-10-17

### Added

- **OAuth 2.0 + PKCE Authentication & Lifecycle Engine ([`core/auth_oauth_cli.py`](../core/auth_oauth_cli.py)):**
  - Implemented RFC 7636 compliant PKCE challenge generation via [`generate_pkce_pair(verifier_length: int = 64) -> PKCEChallenge`](../core/auth_oauth_cli.py#L166-L193) and authorization URL constructor [`build_authorization_url(redirect_uri: str, pkce: PKCEChallenge, client_id: str | None = None, scopes: Sequence[str] = OAUTH_SCOPES) -> str`](../core/auth_oauth_cli.py#L195-L216) requesting all 5 mandatory Google OAuth scopes ([`OAUTH_SCOPES`](../core/auth_oauth_cli.py#L40-L46)).
  - Implemented dual callback handling: local ephemeral HTTP callback listener [`OAuthCallbackServer`](../core/auth_oauth_cli.py#L294-L392) / [`wait_for_oauth_callback`](../core/auth_oauth_cli.py#L394-L404) on `127.0.0.1` (`asyncio.start_server`) returning styled HTML completion/error pages, and manual callback extraction [`extract_code_from_manual_input(raw_input: str, expected_state: str | None = None) -> str`](../core/auth_oauth_cli.py#L250-L278) supporting raw authorization codes, full callback URLs, and query strings.
  - Implemented [`exchange_code_for_tokens`](../core/auth_oauth_cli.py#L440-L485) and managed project discovery [`discover_project_id(access_token: str, ...) -> tuple[str, bool, dict[str, Any]]`](../core/auth_oauth_cli.py#L524-L557) via `cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`, plus end-to-end wrapper [`complete_oauth_login`](../core/auth_oauth_cli.py#L559-L589).
  - Implemented proactive token refresh [`needs_token_refresh(credentials: OAuthCredentials, ..., margin_seconds: int = 300) -> bool`](../core/auth_oauth_cli.py#L596-L606), per-account lock synchronization [`refresh_account_token(account: OAuthAccount, ...) -> OAuthCredentials`](../core/auth_oauth_cli.py#L609-L668) under `account.token_refresh_lock` with strict in-flight request deduplication and atomic snapshot persistence, and [`ensure_valid_access_token`](../core/auth_oauth_cli.py#L670-L684).
  - Implemented dual transport helpers: [`build_v1internal_headers(access_token: str, ide_type: str = "IDE_UNSPECIFIED") -> dict[str, str]`](../core/auth_oauth_cli.py#L691-L706) with CloudCode-PA Header Shield, and [`build_agy_subprocess_command`](../core/auth_oauth_cli.py#L709-L728) with async streaming wrapper [`AgySubprocessTransport`](../core/auth_oauth_cli.py#L730-L816) for `agy --input-format stream-json --output-format stream-json`.
- **Web Pool Authentication, Firefox SQLite Extractor, Cookie Cache & Concurrency Guard — ADR-03 ([`core/auth_web.py`](../core/auth_web.py)):**
  - Implemented isolated Firefox SQLite cookie extraction [`extract_firefox_cookies(cookie_file: str | Path, ...) -> WebCookies`](../core/auth_web.py#L257-L286) extracting `__Secure-1PSID` and `__Secure-1PSIDTS` via `browser_cookie3` with direct read-only `sqlite3` fallback against `moz_cookies` and temporary WAL/SHM sidecar copying.
  - Implemented manual cookie ingestion [`parse_manual_cookies(secure_1psid: str, secure_1psidts: str = "") -> WebCookies`](../core/auth_web.py#L303-L317) and polymorphic parser [`parse_cookie_header_or_dict(raw_input: str | Mapping[str, Any]) -> WebCookies`](../core/auth_web.py#L384-L407) supporting JSON cookie objects, browser-extension JSON lists, and raw `Cookie:` header strings.
  - Added [`get_saved_info_hygiene_notice() -> dict[str, Any]`](../core/auth_web.py#L409-L427) providing bilingual guidance to disable account-level `Saved Info` memory and close private browsing windows immediately to prevent concurrent `__Secure-1PSIDTS` invalidation.
  - Implemented per-account cookie cache isolation: [`get_account_cookie_cache_path(account_id: str, ...) -> Path`](../core/auth_web.py#L446-L470) with strict regex and path-traversal rejection, [`save_account_cookies_isolated(account: WebAccount, ...) -> Path`](../core/auth_web.py#L538-L567) under `account.cookie_write_lock` with atomic `.tmp` + `os.replace` persistence, [`load_account_cookies_isolated`](../core/auth_web.py#L569-L594), and [`extract_client_web_cookies`](../core/auth_web.py#L493-L512).
  - Implemented [`WebSessionManager`](../core/auth_web.py#L645-L812) managing independent `GeminiClient` instances: [`get_or_init_client`](../core/auth_web.py#L694-L716) initialized with `auto_close=False, auto_refresh=True` under `account.reinit_lock`, [`safe_reinit_or_close`](../core/auth_web.py#L744-L772) raising [`ActiveRequestsConcurrencyError`](../core/auth_web.py#L106-L116) if `account.active_requests > 0` ($C_{\text{web}, i} > 0$) to protect active streams, [`close_client_if_idle`](../core/auth_web.py#L774-L776), and [`flush_all_cookies`](../core/auth_web.py#L778-L812).
- **Dynamic Model Discovery, Reasoning Effort Translation & Unified Registry ([`core/model_registry.py`](../core/model_registry.py)):**
  - Implemented model normalization [`normalize_model_id(raw_name: str) -> str`](../core/model_registry.py#L119-L123) and suffix splitting [`split_model_effort_suffix(model_name: str) -> tuple[str, ReasoningEffort | None]`](../core/model_registry.py#L125-L134).
  - Implemented reasoning effort translation: [`build_oauth_thinking_config(effort: ReasoningEffort | str = "low") -> dict[str, Any]`](../core/model_registry.py#L150-L156), [`inject_oauth_thinking_config`](../core/model_registry.py#L158-L165), and [`parse_model_and_effort(model_name: str, ...) -> ResolvedModelSpec`](../core/model_registry.py#L181-L203) mapping `minimal`, `low`, `medium`, and `high` to thinking budgets (`1024`, `4096`, `16384`, `32768`), setting Web `extended_thinking = (effort != "minimal")`, and defaulting routine tasks to `low`.
  - Implemented OAuth CLI model discovery [`discover_oauth_models(account: OAuthAccount, ...) -> list[str]`](../core/model_registry.py#L514-L530) querying `v1internal:fetchAvailableModels` with automatic fallback to `v1internal:retrieveUserQuota` on `403 Forbidden`, populating `account.discovered_models` and `account.quota_state.model_quotas`.
  - Implemented Web pool dynamic model discovery and target resolution [`discover_web_models`](../core/model_registry.py#L573-L586), [`discover_web_models_sync`](../core/model_registry.py#L589-L605), [`resolve_web_model`](../core/model_registry.py#L608-L622), and [`resolve_web_model_sync`](../core/model_registry.py#L624-L638).
  - Implemented [`UnifiedModelRegistry`](../core/model_registry.py#L660-L777) aggregating base models and effort suffix variants, exposing OpenAI-compatible [`list_openai_models() -> dict[str, Any]`](../core/model_registry.py#L710-L725) (`GET /v1/models`), and resolving accounts supporting a model via [`get_supporting_accounts`](../core/model_registry.py#L782-L795) and [`get_supporting_account_objects`](../core/model_registry.py#L797-L812).
- **Cross-Platform 3-Tier Context Rehydration Engine & Hybrid Prefix Differ — ADR-08 ([`core/prefix_differ.py`](../core/prefix_differ.py)):**
  - Implemented deterministic SHA-256 sequential message hashing [`normalize_message_for_hash(message: Mapping[str, Any]) -> str`](../core/prefix_differ.py#L208-L235), cumulative prefix hashing [`compute_prefix_hashes(messages: Sequence[Mapping[str, Any]]) -> list[str]`](../core/prefix_differ.py#L237-L250), and character length calculation [`compute_messages_char_length`](../core/prefix_differ.py#L267-L282).
  - Implemented adaptive session mode [`should_use_temporary_session(messages, tools) -> bool`](../core/prefix_differ.py#L289-L315) selecting `temporary=True` for single-turn tool-less queries and `temporary=False` for multi-turn agent conversations or tool requests.
  - Implemented active chat tracking and 60-minute idle chat garbage collection [`register_chat_activity`](../core/prefix_differ.py#L318-L340), [`get_tracked_chats`](../core/prefix_differ.py#L346-L349), and [`cleanup_expired_chats(client_resolver, ...) -> int`](../core/prefix_differ.py#L399-L433) safely deleting idle Web chats from Google and purging associated records from LMDB `db_sessions`.
  - Implemented $O(k)$ longest prefix matching [`find_longest_prefix_session`](../core/prefix_differ.py#L447-L479) in LMDB `db_sessions` and session affinity lookup [`get_preferred_session_account`](../core/prefix_differ.py#L481-L503) enforcing `SESSION_AFFINITY_MAX_CONTEXT_CHARS = 120_000`.
  - Implemented structured XML history and tool response rendering [`format_tool_response_block(call_id: str, name: str, content: str) -> str`](../core/prefix_differ.py#L510-L520) (`<tool_response call_id="..." name="...">`) and [`format_messages_as_xml_history`](../core/prefix_differ.py#L567-L574) (`<conversation_history>`).
  - Implemented cross-platform temporary directory resolution [`resolve_temp_history_dir(override_dir = None) -> Path`](../core/prefix_differ.py#L628-L647) prioritizing `/dev/shm` on Linux and falling back to `tempfile.gettempdir()` on Windows/macOS.
  - Implemented the 3-Tier Context Rehydration Engine via [`build_rehydration_plan(...) -> RehydrationPlan`](../core/prefix_differ.py#L834-L929):
    - **Tier 1 (`tier1_prefix_hit`):** Same-account prefix hit in `db_sessions`; sends delta messages only under `account.get_conversation_lock(cid)` with `<tool_response>` blocks.
    - **Tier 2 (`tier2_inline_history`):** New session or account failover with context $\le 60,000$ characters; inlines prior turns inside `<conversation_history>`.
    - **Tier 3 (`tier3_temp_file_passthrough`):** Context $> 60,000$ characters; writes `messages[:-3]` to `harness_history_{uuid4().hex}.txt`, pins `system`, `tools`, and `messages[-3:]` in prompt, and attaches the history file.
  - Implemented deterministic temp file cleanup [`remove_temp_history_file`](../core/prefix_differ.py#L936-L948), startup orphan sweeper [`cleanup_orphaned_temp_files(temp_dir = None) -> int`](../core/prefix_differ.py#L950-L961), sync/async context manager [`RehydrationContextManager`](../core/prefix_differ.py#L963-L980) / [`managed_rehydration_plan`](../core/prefix_differ.py#L982-L990), and stream runner [`execute_rehydrated_stream`](../core/prefix_differ.py#L1094-L1138) (aliased as [`execute_with_rehydration`](../core/prefix_differ.py#L1140)).
- **Package Re-exports ([`core/__init__.py`](../core/__init__.py)):**
  - Re-exported all Level 1 and Level 2 public symbols (68 exported symbols across schema sanitation, token compression, OAuth authentication, Web authentication, model registry, and context rehydration) in `__all__`.

### Security

- **Strict Production Endpoint Enforcement ([`core/auth_oauth_cli.py`](../core/auth_oauth_cli.py)):**
  - Implemented [`validate_production_endpoint(url: str) -> str`](../core/auth_oauth_cli.py#L127-L143) strictly blocking `daily-cloudcode-pa.googleapis.com` and requiring production `cloudcode-pa.googleapis.com` endpoints exclusively.
- **Browser Isolation & DBSC Avoidance — ADR-03 ([`core/auth_web.py`](../core/auth_web.py)):**
  - Implemented [`reject_chromium_cookie_source(source_identifier: str | Path) -> None`](../core/auth_web.py#L123-L141) raising [`ChromiumDBSCUnsupportedError`](../core/auth_web.py#L98-L104) for all Chromium browsers (`chrome`, `edge`, `brave`, `opera`, `arc`, `vivaldi`) to prevent Device Bound Session Credential invalidation.
- **Process Environment Sanitization ([`core/auth_web.py`](../core/auth_web.py)):**
  - Implemented [`sanitize_shared_cookie_env() -> str | None`](../core/auth_web.py#L434-L444) removing `GEMINI_COOKIE_PATH` from `os.environ` to eliminate cross-account cookie contamination.
- **Path-Traversal Protection ([`core/auth_web.py`](../core/auth_web.py)):**
  - Enforced strict regex matching and resolved parent directory validation in [`get_account_cookie_cache_path`](../core/auth_web.py#L446-L470) to prevent directory traversal attacks via malicious account identifiers.

### Verified

- **Verification Gate 3 ([`tests/test_level2_gate3.py`](../tests/test_level2_gate3.py), [`tests/test_auth_oauth_cli.py`](../tests/test_auth_oauth_cli.py), [`tests/test_auth_web.py`](../tests/test_auth_web.py), [`tests/test_model_registry.py`](../tests/test_model_registry.py), [`tests/test_prefix_differ.py`](../tests/test_prefix_differ.py)):**
  - Added 90 comprehensive unit, concurrency, end-to-end, and parametrized tests verifying Level 2 OAuth PKCE authentication, token refresh deduplication, Web session management and concurrency guards, dynamic model discovery, reasoning effort translation, and 3-tier context rehydration with deterministic temp file cleanup.
  - Combined test suite execution across all levels:
    - Level 0 Gate 1 ([`tests/test_level0_gate1.py`](../tests/test_level0_gate1.py)): **19 passed**
    - Level 1 Gate 2 ([`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py)): **20 passed**
    - Level 2 Gate 3 ([`tests/test_level2_gate3.py`](../tests/test_level2_gate3.py) [28 items], [`tests/test_auth_oauth_cli.py`](../tests/test_auth_oauth_cli.py) [10 items], [`tests/test_auth_web.py`](../tests/test_auth_web.py) [26 items], [`tests/test_model_registry.py`](../tests/test_model_registry.py) [11 items], [`tests/test_prefix_differ.py`](../tests/test_prefix_differ.py) [15 items]): **90 passed**
    - **Total: 129 passed in 7.32s** with zero regressions.

---

## [0.1.1] - 2026-10-16

### Added

- **RTK-Lite Lossless Tool Output Compressor ([`core/token_saver.py`](../core/token_saver.py)):**
  - Implemented deterministic, stateless lossless compression for `role == "tool"` messages via [`compress_tool_output(content: str, *, enabled: bool = True) -> str`](../core/token_saver.py#L457-L475) and [`sanitize_tool_messages(messages: list[dict[str, Any]], *, enabled: bool = True, headers: Mapping[str, str] | None = None) -> list[dict[str, Any]]`](../core/token_saver.py#L478-L494) (aliased as [`filter_tool_messages`](../core/token_saver.py#L498)).
  - Added [`should_enable_token_saver(headers: Mapping[str, str] | None = None) -> bool`](../core/token_saver.py#L355-L372) to support case-insensitive per-request opt-out via the `X-Token-Saver` HTTP header (`"off"`, `"false"`, `"0"`, `"disabled"`, `"no"`), guaranteeing 100% byte-for-byte identity when disabled.
  - Added [`compress_git_diff(text: str) -> str`](../core/token_saver.py#L391-L421) to strip non-semantic `index <hash>..<hash>` headers and trailing horizontal padding from unified and `git diff` blocks while preserving file headers (`diff --git`, `--- a/...`, `+++ b/...`), `@@ ... @@` hunk headers, `+`/`-`/context lines, and unprefixed blank lines inside hunks so hunk line counts remain exact.
  - Added [`compress_grep_find_ls(text: str) -> str`](../core/token_saver.py#L424-L431) to collapse multi-space column padding in `ls -l` and `total <N>` lines, strip redundant leading `./` prefixes from `find` and `grep` paths, and collapse consecutive duplicate lines.
  - Added [`compress_test_logs(text: str) -> str`](../core/token_saver.py#L447-L454), [`deduplicate_lines(text: str) -> str`](../core/token_saver.py#L434-L439) (aliased as [`deduplicate_consecutive_lines`](../core/token_saver.py#L442-L444)), [`normalize_whitespace_and_blank_lines(text: str) -> str`](../core/token_saver.py#L382-L388), and [`strip_ansi_codes(text: str) -> str`](../core/token_saver.py#L375-L379) to strip terminal ANSI escape sequences, collapse spinner frames, progress bars, and consecutive duplicate log lines while preserving 100% of error, failure, and stack traceback lines (`Traceback (most recent call last):`, `AssertionError`, `ERROR`, `FAIL`, `FAILED`, `FATAL`, `CRITICAL`, `PANIC`, `Exception`, `File "...", line N`, and `E   ...`).
- **Recursive JSON Schema Sanitizer & Union Flattener ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py)):**
  - Defined [`UNSUPPORTED_SCHEMA_KEYS`](../core/schema_sanitizer.py#L25-L48) (`frozenset` of the exact 20 JSON Schema meta-keys rejected by `cloudcode-pa` `v1internal`: `additionalProperties`, `$schema`, `$id`, `$ref`, `$defs`, `definitions`, `examples`, `patternProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, `pattern`, `format`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`).
  - Implemented [`clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]`](../core/schema_sanitizer.py#L173-L194) to recursively strip all 20 unsupported meta-keys at arbitrary nesting depths while preserving user-defined tool parameter names inside `properties` mappings even when named `format`, `pattern`, `minimum`, or `examples`.
  - Implemented [`flatten_unions(schema: dict[str, Any]) -> dict[str, Any]`](../core/schema_sanitizer.py#L308-L335) to convert `type: [...]` arrays and `anyOf` / `oneOf` union branches into concrete Gemini-compatible types with `nullable: True`, merging `properties` and intersecting `required` sets across object union branches, and recursing into both `dict` and `list[dict]` tuple-validation `items`.
- **Dual Zero-Required & History Replay Fix — ADR-05 ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py)):**
  - Implemented [`apply_zero_required_fix(parameters: dict[str, Any]) -> tuple[dict[str, Any], bool]`](../core/schema_sanitizer.py#L363-L392) to resolve `cloudcode-pa` post-thought `functionCall` stalls when `required` is empty (`[]`):
    - **Case 1 (Zero properties, `properties: {}`):** Injects `"_step": {"type": "string", "description": "Internal step tracker"}` and sets `required = ["_step"]`, returning `(params, True)`.
    - **Case 2 (Optional properties exist, `required: []`):** Promotes the first existing property in `properties` to `required` with `nullable: True` and appends `' (Optional, default: "")'` to its description without injecting `_step`, returning `(params, False)`.
  - Added [`strip_step_from_args(args: dict[str, Any] | None) -> dict[str, Any]`](../core/schema_sanitizer.py#L395-L401) and [`reinject_step_to_args(args: dict[str, Any] | None, *, tool_had_zero_properties: bool = True) -> dict[str, Any]`](../core/schema_sanitizer.py#L404-L417) to strip synthetic `_step` arguments before returning tool calls to the client and re-hydrate `{"_step": "done"}` in historical `functionCall.args` during multi-turn replay.
  - Added end-to-end tool wrappers [`sanitize_parameters_schema(parameters: dict[str, Any] | None) -> dict[str, Any]`](../core/schema_sanitizer.py#L425-L431), [`sanitize_tool(tool: dict[str, Any]) -> dict[str, Any]`](../core/schema_sanitizer.py#L434-L455), and [`sanitize_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]`](../core/schema_sanitizer.py#L458-L462) supporting both OpenAI `{"type": "function", "function": {...}}` and Gemini `{"functionDeclarations": [...]}` formats without mutating caller inputs.
- **Public Package Exports ([`core/__init__.py`](../core/__init__.py)):**
  - Exported all 23 Level 1 constants and functions from [`core/__init__.py`](../core/__init__.py#L31-L55) via `__all__`.

### Security

- **WAF System Prompt Shield — ADR-04 ([`core/schema_sanitizer.py`](../core/schema_sanitizer.py)):**
  - Implemented [`sanitize_system_prompt_text(text: str) -> str`](../core/schema_sanitizer.py#L83-L100) and [`sanitize_messages_waf(messages: list[dict[str, Any]]) -> list[dict[str, Any]]`](../core/schema_sanitizer.py#L131-L143) (aliased as [`sanitize_system_prompts`](../core/schema_sanitizer.py#L146)) to neutralize Google Cloud ESF/WAF signatures in `system` and `developer` messages (`str` and multipart `list[dict]`) that trigger false `429 RESOURCE_EXHAUSTED` rejections.
  - Rewrites `<system-conventions>` / `</system-conventions>` tags to `<conventions>` / `</conventions>` and isolates `RFC 2119` directive lines with blank lines (`\n\n`) while leaving `user`, `assistant`, and `tool` messages byte-for-byte untouched.

### Fixed

- Prevented false-positive `git diff` classification in [`core/token_saver.py`](../core/token_saver.py#L37-L40) when pytest failure logs contain incidental `@@ -1,2 +1,2 @@` assertion diff markers by requiring authentic `diff --git a/... b/...` or `--- a/...\n+++ b/...` headers in [`_is_git_diff_text`](../core/token_saver.py#L117-L119).
- Preserved unprefixed blank lines inside unified diff hunks in [`_format_git_diff_line`](../core/token_saver.py#L167-L169) so consecutive empty context lines are not collapsed, preventing hunk line-count corruption.
- Eliminated orphaned `[... repeated N times ...]` tags in [`_collapse_consecutive_identical_lines`](../core/token_saver.py#L288-L291) when preceding spinner or progress bar lines are filtered out.
- Preserved natural English prose containing `RFC 2119` (such as `"Keywords are interpreted as described in RFC 2119: MUST, REQUIRED"`) on a single line in [`sanitize_system_prompt_text`](../core/schema_sanitizer.py#L62-L65) by only splitting inline `RFC 2119` clauses when immediately preceded by a tag boundary (`>`) or sentence/clause punctuation (`:`, `.`, `;`, `!`).

### Verified

- **Verification Gate 2 ([`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py)):**
  - Added 20 unit and parametrized test cases in [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py) verifying Level 1 schema sanitization, union flattening, ADR-05 Case 1 & Case 2 zero-required handling, ADR-04 WAF shielding, RTK-Lite lossless compression (`>= 20%` reduction on padded `git diff` blocks), 100% error/traceback preservation, caller input immutability, and `X-Token-Saver: off` byte-for-byte identity.
  - Combined test suite execution across [`tests/test_level0_gate1.py`](../tests/test_level0_gate1.py) (19 tests) and [`tests/test_level1_gate2.py`](../tests/test_level1_gate2.py) (20 tests): **39 passed**.

---

## [0.1.0] - 2026-10-16

### Added

- **Level 0 Configuration, State Models, and Storage ([`config/settings.py`](../config/settings.py), [`config/accounts.json`](../config/accounts.json), [`requirements.txt`](../requirements.txt)):**
  - Configured mathematical scheduling constants (`DELTA_BASE`, `OAUTH_MAX_CONCURRENCY_DEFAULT = 3`, `WEB_MAX_CONCURRENCY_DEFAULT = 2`, `WEB_MIN_START_INTERVAL_S = 0.5`, `WEB_FAST_REQUEST_THRESHOLD_S = 1.0`, `WEB_MIN_INTERVAL_S = 0.6`, `WEB_JITTER_RANGE_S = (0.4, 0.8)`, `SESSION_AFFINITY_MULTIPLIER = 1.35`, `SESSION_AFFINITY_MAX_CONTEXT_CHARS = 120_000`, `REHYDRATION_TIER2_MAX_CHARS = 60_000`) and longest-prefix model weight lookup [`get_model_base_delta(model_name: str) -> float`](../config/settings.py#L85-L110) as specified in [`docs/founditon/01_data_schemas_and_state.md`](founditon/01_data_schemas_and_state.md) and [`docs/founditon/03_scheduling_and_control_spec.md`](founditon/03_scheduling_and_control_spec.md).
  - Implemented Pydantic v2 structural contracts ([`OAuthAccount`](../config/settings.py#L151-L196), [`WebAccount`](../config/settings.py#L249-L353), [`AccountsConfig`](../config/settings.py#L356-L362)) with isolated `PrivateAttr` runtime counters and `asyncio.Lock` instances excluded from JSON serialization.
  - Implemented [`AccountsStateRegistry`](../config/settings.py#L372-L488) as the in-memory Single Source of Truth (ADR-02) and [`save_accounts_snapshot`](../config/settings.py#L521-L546) protected by `accounts_file_lock` for atomic `.tmp` + `os.replace()` persistence.
  - Implemented [`LMDBStore`](../config/settings.py#L561-L683) managing `db_sessions` and `db_signatures` tables with account-scoped session pruning.
- **Verification Gate 1 ([`tests/test_level0_gate1.py`](../tests/test_level0_gate1.py)):**
  - Added 19 unit, parametrized, and 50-task concurrency stress tests verifying Level 0 state management, atomic persistence, and LMDB durability.
