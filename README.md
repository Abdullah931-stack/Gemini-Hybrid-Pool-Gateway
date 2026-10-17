# Gemini Hybrid Pool Gateway

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20v1%20Compatible-orange.svg)](https://platform.openai.com/docs/api-reference)
[![Tests Passing](https://img.shields.io/badge/tests-386%20passed-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Gemini Hybrid Pool Gateway** is a high-concurrency, fault-tolerant proxy gateway that pools **Google Cloud Code Assist (OAuth CLI Pool)** and **Gemini Web (Cookie Pool)** accounts into a single, unified, OpenAI-compatible HTTP interface (`/v1/chat/completions`, `/v1/models`).

The gateway is purpose-built to drive autonomous agent harnesses (such as [Pi](https://github.com/can1357/oh-my-pi), Hermes, Cline, Roo-Code, and Cursor) across multiple Google AI Pro subscription quotas without manual token rotation or dropped connections.

---

## Table of Contents

- [Architectural Overview](#architectural-overview)
  - [Dual-Pool Channel Design](#dual-pool-channel-design)
  - [System Architecture Diagram](#system-architecture-diagram)
  - [Operational Planes](#operational-planes)
- [Mathematical Dynamic Scheduler](#mathematical-dynamic-scheduler)
  - [Dynamic Channel Routing and Model Discovery](#dynamic-channel-routing-and-model-discovery)
  - [OAuth Account Priority Scoring](#oauth-account-priority-scoring)
  - [Web Account Priority Scoring](#web-account-priority-scoring)
  - [Conditional Concurrency Denominator](#conditional-concurrency-denominator)
  - [Soonest-Reset Urgency Multiplier](#soonest-reset-urgency-multiplier)
  - [Context-Scaled Optimistic Delta](#context-scaled-optimistic-delta)
  - [Smart Pacing Engine](#smart-pacing-engine)
- [Zero-Drop Resilience & Protocol Transformation](#zero-drop-resilience--protocol-transformation)
  - [Non-Empty Pre-Stream Handshake](#non-empty-pre-stream-handshake)
  - [3-Tier Context Rehydration](#3-tier-context-rehydration)
  - [Dual Zero-Required Schema Sanitizer](#dual-zero-required-schema-sanitizer)
  - [WAF System Prompt Shield](#waf-system-prompt-shield)
  - [Thought Signature & Model-Turn Guards](#thought-signature--model-turn-guards)
  - [Multi-Tool Lookahead Parser](#multi-tool-lookahead-parser)
  - [RTK-Lite Lossless Token Saver](#rtk-lite-lossless-token-saver)
- [Prerequisites & Installation](#prerequisites--installation)
  - [System Requirements](#system-requirements)
  - [Installation Steps](#installation-steps)
  - [Environment Configuration](#environment-configuration)
- [CLI Reference & Account Management](#cli-reference--account-management)
  - [Starting the Gateway Server](#starting-the-gateway-server)
  - [Adding OAuth CLI Accounts](#adding-oauth-cli-accounts)
  - [Adding Web Cookie Accounts](#adding-web-cookie-accounts)
  - [Checking Pool Status & Quotas](#checking-pool-status--quotas)
  - [Immediate Quota Synchronization](#immediate-quota-synchronization)
- [API Usage & Client Integration](#api-usage--client-integration)
  - [Endpoints Reference](#endpoints-reference)
  - [Dynamic Model Routing REST API and Persistence](#dynamic-model-routing-rest-api-and-persistence)
  - [Python OpenAI SDK Sample](#python-openai-sdk-sample)
  - [cURL Request Sample](#curl-request-sample)
  - [Agent Harness Configuration](#agent-harness-configuration)
- [Real-Time Web Dashboard](#real-time-web-dashboard)
  - [Dashboard Overview and Health Telemetry](#dashboard-overview-and-health-telemetry)
  - [Dynamic Model Routing and Compute Allocation UI](#dynamic-model-routing-and-compute-allocation-ui)
- [Verification & Testing](#verification--testing)
- [Security & Cookie Hygiene](#security--cookie-hygiene)

---

## Architectural Overview

### Dual-Pool Channel Design

The gateway decouples traffic into two complementary channels based on model latency, quota mechanics, and tool support:

1. **OAuth CLI Pool (`cloudcode-pa.googleapis.com/v1internal`)**:
   - Backed by Google Cloud Code Assist (Antigravity 2.0 CLI) OAuth 2.0 PKCE credentials.
   - Dedicated to sub-agents and high-throughput coding tasks using lightweight models (`gemini-3.8-flash`, `gemini-3.1-flash-lite`).
   - Native support for tool calls (`functionDeclarations`), structured schemas, and `thoughtSignature` persistence.
   - Requests omit `"requestType": "agent"` to eliminate artificial `429 RESOURCE_EXHAUSTED` errors on Pro tiers.
   - Default concurrency cap: $C_{\text{oauth}, k} \le 3$.

2. **Web Cookie Pool (`gemini-webapi v2.1.1+`)**:
   - Backed by Google AI Pro web sessions authenticated via `__Secure-1PSID` and `__Secure-1PSIDTS` cookies.
   - Dedicated to flagship reasoning models (`gemini-4-argon`, `gemini-3.1-pro`).
   - Dual-seat concurrent requests ($C_{\text{web}, i} \le 2$) per web account with strict session affinity.
   - Automatic cross-channel failover fallback for flash requests when OAuth pools enter cooldown.

> [!NOTE]
> **Dynamic Discovery & Role-Agnostic Model Selection**:
> Model names are **not rigid constraints** on which models can be selected for an agent harness. Rather, the gateway dynamically discovers all supported models from registered accounts across both pools (`v1internal:fetchAvailableModels`, `retrieveUserQuota`, and `list_models()`) and publishes them via `GET /v1/models`. Clients and agent harnesses can assign any discovered model or custom alias to any agent role (Main Agent, Sub Agent, Fast Worker, Reviewer, etc.). A model name primarily specifies which **Primary Channel** (`oauth` or `web`) to try first before automatically failing over to the alternative channel (`web` or `oauth`), and this routing preference is **100% dynamic, configurable, and persistent**.

### System Architecture Diagram

```mermaid
flowchart TD
    Client["Client / Agent Harness (Pi, Hermes, OpenAI SDK)"] -->|POST /v1/chat/completions| API["FastAPI Protocol Layer (api/openai_routes.py)"]
    API --> TokenSaver["RTK-Lite Token Saver (core/token_saver.py)"]
    TokenSaver --> Failover["Failover Controller & Handshake (core/failover.py)"]
    
    Failover --> Scheduler{"Hybrid Scheduler (core/hybrid_scheduler.py)"}
    
    Scheduler -->|"Flash Models (P_oauth,k)"| OAuthChannel["OAuth CLI Channel (core/oauth_cli_channel.py)"]
    Scheduler -->|"Pro / Argon Models (P_web,i)"| WebChannel["Web Channel (core/web_channel.py)"]
    Scheduler -.->|"Cross-Channel Failover"| WebChannel
    
    subgraph OAuthPool ["OAuth CLI Pool (C_oauth <= 3)"]
        OAuthChannel --> Sanitizer["Schema Sanitizer & WAF Shield (core/schema_sanitizer.py)"]
        Sanitizer --> CloudCode["Google Cloud Code Assist API (v1internal)"]
    end
    
    subgraph WebPool ["Web Cookie Pool (C_web <= 2)"]
        WebChannel --> Rehydrator["3-Tier Context Rehydration (core/prefix_differ.py)"]
        Rehydrator --> Lookahead["Multi-Tool Lookahead Parser (core/web_channel.py)"]
        Lookahead --> GeminiWeb["Gemini Web Client (gemini-webapi)"]
    end
    
    subgraph StateStorage ["State & Storage Plane (Single Source of Truth)"]
        StateReg["In-Memory State Registry (AccountsStateRegistry)"]
        LMDB["LMDB Store (db_sessions & db_signatures)"]
        DiskSnapshot["Atomic Snapshot (config/accounts.json)"]
        CookieCache["Isolated Cookie Cache (config/cookies_cache/)"]
        StateReg <-->|Atomic Flush| DiskSnapshot
    end
```

### Operational Planes

The implementation follows a 6-tier topological architecture:

| Operational Plane | Modules | Responsibilities |
| :--- | :--- | :--- |
| **Level 0: State & Storage** | `config/settings.py`, `config/accounts.json`, `config/cookies_cache/`, `lmdb` | In-memory authority, atomic disk persistence with `accounts_file_lock`, isolated cookies, prefix & signature stores. |
| **Level 1: Filters & Sanitizers** | `core/token_saver.py`, `core/schema_sanitizer.py` | RTK-Lite lossless compression, 20-keyword schema stripper, dual zero-required fixer, WAF header shield. |
| **Level 2: Auth & Rehydration** | `core/auth_oauth_cli.py`, `core/auth_web.py`, `core/model_registry.py`, `core/prefix_differ.py` | OAuth 2.0 PKCE, Firefox cookie extraction, model discovery, 3-tier prefix diffing & RAM/temp passthrough. |
| **Level 3: Channel Execution** | `core/oauth_cli_channel.py`, `core/web_channel.py`, `core/usage_monitor.py` | `v1internal` client, 64-char lookahead parser, reasoning stream dispatcher, batch quota scraper (`jSf9Qc`). |
| **Level 4: Control & Routing** | `core/hybrid_scheduler.py`, `core/failover.py` | Mathematical priority scoring, soonest-reset balancing, smart pacing, non-empty pre-stream handshake. |
| **Level 5: Public Interfaces** | `api/openai_routes.py`, `api/dashboard_routes.py`, `static/index.html`, `main.py` | OpenAI `/v1` endpoints, telemetry `/api/status`, management UI, and Click CLI engine. |

---

## Mathematical Dynamic Scheduler

### Dynamic Channel Routing and Model Discovery

The scheduler dynamically queries registered accounts and evaluates channel routing rules for every incoming request.

#### Dynamic Model Discovery & Role Agnosticism
Model names are **not rigid constraints** defining what can be selected. Rather, the gateway dynamically exposes all discovered models across both OAuth CLI accounts (`v1internal:fetchAvailableModels` and `v1internal:retrieveUserQuota`) and Web accounts (`client.list_models()`) through the OpenAI-compatible `GET /v1/models` endpoint. Autonomous harnesses and users can select **any discovered model or custom alias for any agent role** (Main Agent, Sub Agent, Fast Worker, Code Reviewer, etc.).

#### Primary Channel & Failover Mechanics
A requested model identifier primarily designates the **Primary Channel** (`oauth` or `web`) to evaluate first. If all accounts on the primary channel are exhausted, saturated with active concurrent requests ($C \ge C_{\max}$), or in cooldown, the scheduler transparently fails over to the designated **Failover Channel** (`web` or `oauth`) without dropping the client stream.

#### Default System Routing Table

The baseline system defaults (`DEFAULT_MODEL_ROUTES`) balance token quotas and latency:

| Model Identifier / Family | Primary Channel | Failover Channel | Architectural Rationale |
| :--- | :--- | :--- | :--- |
| `gemini-4-argon` | `web` | `None` (Exclusive) | Exclusive to Google AI Pro web subscribers; Antigravity CLI does not offer Argon. |
| `gemini-3.1-pro` | `web` | `oauth` | Prioritizes flagship web sessions; seamlessly falls back to OAuth CLI when web accounts saturate. |
| `gemini-3.8-flash` | `oauth` | `web` | Offloads high-throughput agent tasks to OAuth CLI to preserve 5-hour web quotas; fails over to web if OAuth cools down. |
| `gemini-3.1-flash-lite` | `oauth` | `web` | High-speed zero-deduction lightweight calls on OAuth CLI; fails over to web as needed. |
| *Other / Discovered Models* | `oauth` | `web` | Default fallback for newly discovered models and unconfigured identifiers. |

#### 100% Dynamic & Configurable Routing
Routing preferences are completely dynamic and can be modified at runtime without restarting the server:
- **Custom Overrides & Aliases**: Reassign primary and failover channels for any model, or create custom aliases (e.g., `main_agent`, `sub_agent`, `gpt-4o`) via the Web Dashboard or REST API (`POST /api/model-routes`).
- **Resolution Order**: The scheduler resolves channel preference via `get_model_channel_preference()`:
  1. *Exact match* against custom routes in `config/model_routes.json` (raw name or normalized base model).
  2. *Longest prefix match* after stripping reasoning effort suffixes (`-minimal`, `-low`, `-medium`, `-high`).
  3. *Default system routing* rules from `DEFAULT_MODEL_ROUTES`.
- **Atomic Persistence**: Custom routes persist atomically to `config/model_routes.json` protected by `accounts_file_lock`.

### OAuth Account Priority Scoring

For an OAuth account $k$ and requested model $m$, priority score $P_{\text{oauth}, k}$ is defined as:

$$P_{\text{oauth}, k} = G_{\text{oauth}, k} \times \tau_{\text{oauth}, k, m} \times \frac{Q_{\text{oauth}, k, m}}{(1 + C_{\text{oauth}, k}) \times (1 + 0.001 \times L_{\text{oauth}, k})}$$

Where:
- $G_{\text{oauth}, k} \in \{0, 1\}$: Strict eligibility gate. Evaluates to $1$ only if account status is `ACTIVE`, active requests $C_{\text{oauth}, k} < 3$, cooldown timestamp has passed, model $m$ is supported, and remaining quota fraction $Q_{\text{oauth}, k, m} \ge 0.02$. Otherwise $0$.
- $\tau_{\text{oauth}, k, m}$: Reset urgency multiplier in $[1.0, 1.5]$.
- $Q_{\text{oauth}, k, m}$: Remaining quota fraction in $(0, 1.0]$.
- $C_{\text{oauth}, k}$: Active concurrent requests on account $k$.
- $L_{\text{oauth}, k}$: Exponential moving average of latency in milliseconds.

### Web Account Priority Scoring

For a Web account $i$, priority score $P_{\text{web}, i}$ balances rolling limits, concurrency, and session stickiness:

$$P_{\text{web}, i} = G_{\text{web}, i} \times S_i \times \tau_{\text{web}, i} \times \frac{(0.95 - U_{5h, i}) \times (0.98 - U_{w, i})}{D_{\text{conc}, i}}$$

Where:
- $G_{\text{web}, i} \in \{0, 1\}$: Strict eligibility gate. Evaluates to $1$ only if account status is `ACTIVE`, active requests $C_{\text{web}, i} < 2$, effective 5-hour usage $U_{5h, i} < 0.95$, and effective weekly usage $U_{w, i} < 0.98$. Otherwise $0$.
- $S_i$: Bounded session affinity multiplier ($1.35$ or $1.0$).
- $\tau_{\text{web}, i}$: Soonest-reset urgency multiplier in $[1.0, 1.5]$.
- $U_{5h, i} = \text{scraped\_5h}_i + \text{optimistic\_5h\_delta}_i$: Total effective 5-hour usage.
- $U_{w, i} = \text{scraped\_weekly}_i + \text{optimistic\_weekly\_delta}_i$: Total effective weekly usage.
- $D_{\text{conc}, i}$: Conditional concurrency denominator.

### Conditional Concurrency Denominator

To prevent concurrent turns within an existing agent conversation from being pushed to a clean account (which forces re-uploading the entire prompt history), the gateway decouples affinity requests from new conversations:

$$D_{\text{conc}, i} = \begin{cases} 1 + 0.25 \times C_{\text{web}, i} & \text{if } S_i = 1.35 \text{ (conversation turn continuing on account } i \text{)} \\ (1 + C_{\text{web}, i})^2 & \text{if } S_i = 1.0 \text{ (new session or unassigned request)} \end{cases}$$

> [!NOTE]
> When an account has one in-flight request ($C_{\text{web}, i} = 1$), an affinity match receives $D_{\text{conc}, i} = 1.25$. The ratio evaluates to $\frac{S_i}{D_{\text{conc}, i}} = \frac{1.35}{1.25} = \mathbf{1.08 > 1.00}$, retaining affinity priority over an idle account without violating the safety cap ($C \le 2$).

### Soonest-Reset Urgency Multiplier

To exhaust an account whose 5-hour rolling window will reset shortly, $\tau_{\text{web}, i}$ scales up over an 18,000-second (5-hour) horizon:

$$\tau_{\text{web}, i} = 1.0 + 0.5 \times \left(1.0 - \frac{\min(18000, \max(0, T_{\text{reset}, 5h, i} - T_{\text{now}}))}{18000}\right)$$

### Context-Scaled Optimistic Delta

Because web usage stats from Google's `/usage` RPC (`jSf9Qc`) sync periodically, the gateway tracks optimistic quota deductions locally immediately upon request completion:

$$\Delta_{5h}(m, L_{\text{chars}}) = \Delta_{\text{base}}(m) \times \left(1.0 + 0.25 \times \frac{L_{\text{chars}}}{25000}\right)$$

Where $\Delta_{\text{base}}(m)$ represents base quota weights:
- `gemini-4-argon`: `+0.10` (10% of 5h window)
- `gemini-3.1-pro`: `+0.03` (3% of 5h window, capped at `0.09` maximum per request)
- `gemini-3.8-flash`: `+0.002` (0.2% of 5h window)
- `gemini-3.1-flash-lite`: `0.000` (Free tier / zero computational deduction)

### Smart Pacing Engine

The Smart Pacing engine protects web cookie accounts from anti-bot challenges and CAPTCHA tripping:
- **Start-Interval Throttling**: Enforces a minimum interval of `WEB_MIN_START_INTERVAL_S = 0.5s` between request start timestamps ($T_{\text{start}}$) on the same account.
- **Fast Request Safety Cooldown**: If a request completes in under $1.0\text{s}$, a non-blocking jittered pause (`WEB_MIN_INTERVAL_S = 0.6s`, randomized between `0.4s` and `0.8s`) is scheduled via `asyncio.sleep()` without zeroing the account's eligibility gate.
- Long streaming requests that naturally span several seconds incur `0ms` extra latency upon completion.

---

## Zero-Drop Resilience & Protocol Transformation

### Non-Empty Pre-Stream Handshake

In ASGI frameworks like FastAPI, emitting a `StreamingResponse` commits `HTTP 200 OK` to the network socket immediately. If an upstream channel fails after the response has begun, the client experiences a dropped connection and fails.

To guarantee zero dropped requests:
1. `core/failover.py` opens the upstream generator inside a retry loop and collects initial chunks into an in-memory buffer (`buffered_chunks`).
2. Buffering continues until the gateway receives the **first non-empty chunk** containing actual text (`delta.content`), reasoning thoughts (`delta.reasoning_content`), or tool calls (`delta.tool_calls`).
3. If the selected account raises an error, exhausts its quota, or terminates prematurely with an empty response, the controller releases the account's concurrency slot, marks it in `excluded_ids`, and transparently routes the request to the next highest-scoring account across either pool.
4. Once the handshake succeeds, the buffered chunks are chained with the active stream and returned to the client: `StreamingResponse(chain(buffered_chunks, stream_gen))`.

### 3-Tier Context Rehydration

To conserve bandwidth and avoid context duplication on multi-turn conversations, `core/prefix_differ.py` hashes prefix messages into an LMDB database (`db_sessions`):

- **Tier 1: Same-Account Prefix Hit**: If the selected account holds an active `ChatSession` matching prefix $[m_1 \dots m_{k-d}]$, the gateway acquires the per-conversation lock (`conversation_locks[cid]`) and sends only the delta turn $[m_{k-d+1} \dots m_k]$.
- **Tier 2: Account Handoff ($\le 60,000$ characters)**: When a session fails over to a new account and total context is $\le 60,000$ characters, previous turns are consolidated into `<conversation_history>` XML blocks and uploaded to initiate a new session.
- **Tier 3: Cross-Platform File/RAM Passthrough ($> 60,000$ characters)**: For massive contexts:
  - The gateway detects `/dev/shm` (Linux shared memory) or automatically falls back to `tempfile.gettempdir()` on Windows/macOS.
  - Older turns (`messages[:-3]`) are serialized to `harness_history_{uuid}.txt` and mounted as an input file.
  - System instructions, tool definitions, and the last 3 turns remain pinned in the prompt text.
  - Temporary files are guaranteed to be unlinked in a `finally:` block and cleaned during application startup.

### Dual Zero-Required Schema Sanitizer

Google's Cloud Code Assist endpoint (`cloudcode-pa`) rejects standard JSON Schema keywords and experiences empty-response bugs when function declarations have no `required` fields.

The schema sanitizer (`core/schema_sanitizer.py`) executes:
1. **Keyword Stripping**: Recursively removes 20 unsupported JSON Schema keys: `additionalProperties`, `$schema`, `$id`, `$ref`, `$defs`, `definitions`, `examples`, `patternProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, `pattern`, `format`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`.
2. **Union Flattening**: Flattens `anyOf` and `oneOf` blocks.
3. **Dual Zero-Required Parameter Fix**:
   - *Case 1 (Empty Properties `properties: {}`)*: Injects synthetic parameter `"_step": {"type": "string"}` with `required: ["_step"]`. Strips `_step` from model output before returning to client. Upon subsequent conversation turns, re-injects `"_step": "done"` into historical `functionCall.args` to ensure history replay consistency.
   - *Case 2 (Optional Properties Present, `required: []`)*: Promotes the first optional property to `required` (annotating its description with `nullable: true` or default `""`) without adding artificial parameters.

### WAF System Prompt Shield

Google's Enterprise Security Firewall (ESF) and WAF will drop or return false `429` errors on system prompts containing certain agent harness conventions.

The shield automatically sanitizes outgoing prompts:
- Transforms `<system-conventions>` to `<conventions>`.
- Decouples `RFC 2119: MUST, REQUIRED...` directives from opening XML tags.

### Thought Signature & Model-Turn Guards

- **`thoughtSignature` Injection**: Gemini 3 and 4 require valid thought signatures on multi-turn function call replies. The gateway restores original signatures from LMDB (`db_signatures`) matching `tool_call_id`, or injects `"thoughtSignature": "skip_thought_signature_validator"`.
- **Multimodal Tool Nesting**: Images returned by tool executions are nested as `inlineData` directly inside `functionResponse.parts` (preventing `400 Invalid Argument` errors).
- **Post-Assembly Model-Turn Guard**: If conversation history terminates on an assistant/model turn, the gateway appends a synthetic `{"role": "user", "parts": [{"text": "Continue."}]}` message to satisfy API structure requirements.

### Multi-Tool Lookahead Parser

In Web Cookie channels, models like `gemini-4-argon` emit multiple consecutive `<tool_call>` blocks in a single streaming turn.

The 64-character lookahead parser (`core/web_channel.py`) operates a 3-phase state machine:
1. `TEXT_STREAMING`: Streams text as `delta.content`. Once a tool call begins (`has_emitted_tool_call = True`), suppresses all whitespace and markdown fences between consecutive calls.
2. `LOOKAHEAD_BUFFERING` (64 chars): Buffers upcoming tokens upon encountering `<` or ```` ``` ```` to detect `<tool_call>` tags without leaking XML syntax to the client.
3. `TOOL_CAPTURING`: Accumulates JSON arguments until `</tool_call>`, emitting each tool call with a unique `call_{uuid}` and sequential index (`0, 1, 2...`), then loops to capture subsequent tools in the same response.

### RTK-Lite Lossless Token Saver

Autonomous agents repeatedly invoke commands like `git diff`, `grep`, `find`, and `ls` that produce extensive whitespace, repeating status lines, and redundant log outputs.

The `RTK-Lite` engine (`core/token_saver.py`):
- Sanitizes `role: "tool"` outputs losslessly without modifying code or line content.
- Collapses consecutive duplicate lines and strips trailing spaces, reducing token usage by 20% to 40%.
- Can be disabled on any request by supplying the header:
  ```http
  X-Token-Saver: off
  ```

---

## Prerequisites & Installation

### System Requirements

- **Python**: Version `3.11` or higher.
- **Operating System**: Linux, macOS, or Windows (Windows AMD64 `.pyd` dynamic library loading is handled automatically).
- **Browser**: [Mozilla Firefox](https://www.mozilla.org/firefox/) (strongly recommended for Web Cookie extraction to avoid Chromium DBSC encryption).

### Installation Steps

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-org/Gemini-Hybrid-Pool-Gateway.git
   cd Gemini-Hybrid-Pool-Gateway
   ```

2. **Create and activate a virtual environment**:
   ```bash
   # Linux / macOS
   python3 -m venv venv
   source venv/bin/activate

   # Windows (PowerShell)
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

### Environment Configuration

Copy `.env.example` to `.env` and adjust settings as required:

```bash
cp .env.example .env
```

```ini
# ==============================================================================
# Gemini-Hybrid-Pool-Gateway Environment Configuration
# ==============================================================================

# Google OAuth 2.0 Client credentials (required for OAuth CLI pool)
# Create a Desktop Application client at: https://console.cloud.google.com/apis/credentials
GEMINI_OAUTH_CLIENT_ID=your_client_id.apps.googleusercontent.com
GEMINI_OAUTH_CLIENT_SECRET=your_client_secret

# Storage Paths & LMDB Map Size (in bytes, default 100 MB)
ACCOUNTS_FILE_PATH=config/accounts.json
MODEL_ROUTES_FILE_PATH=config/model_routes.json
COOKIES_CACHE_DIR=config/cookies_cache
LMDB_DIR=config/lmdb_data
LMDB_MAP_SIZE=104857600
```

> [!IMPORTANT]
> `config/accounts.json` and `config/model_routes.json` are maintained in-memory during server execution. All runtime changes are persisted atomically using temporary files (`*.tmp`), flushed via `os.fsync()`, and swapped via `os.replace()` under `accounts_file_lock`. Do not manually edit these JSON files while the gateway is running.

---

## CLI Reference & Account Management

The gateway exposes a command-line interface via `main.py`:

```
Usage: main.py [OPTIONS] [COMMAND] [ARGS]...
```

### Starting the Gateway Server

Start the ASGI server with default binding (`127.0.0.1:8000`):

```bash
# Using subcommand
python main.py serve --host 127.0.0.1 --port 8000

# Using top-level flag
python main.py --serve --port 8000

# With hot reload enabled (development)
python main.py serve --port 8000 --reload
```

### Adding OAuth CLI Accounts

Launch the interactive PKCE authorization wizard to add Google Cloud Code Assist accounts:

```bash
# Interactive wizard
python main.py add-oauth

# With predefined label and transport mode
python main.py --add-oauth --label "Work Account" --mode "http_v1internal"

# Subprocess transport mode (Antigravity CLI wrapper)
python main.py add-oauth --label "Secondary Account" --mode "agy_subprocess"
```

The wizard will:
1. Generate an RFC 7636 PKCE pair (`S256`).
2. Provide an authorization URL for Google login.
3. Prompt for the redirected callback URL or code.
4. Exchange tokens, discover the associated Google Cloud project ID, query available models, and register the account.

### Adding Web Cookie Accounts

Register Google AI Pro web accounts using cookies extracted from Firefox or supplied manually:

```bash
# Import cookies directly from a Firefox profile or cookies.sqlite file
python main.py --add-web --firefox-profile "C:\Users\<user>\AppData\Roaming\Mozilla\Firefox\Profiles\<profile>\cookies.sqlite" --label "Web Pro 1"

# Interactive manual prompt (enter __Secure-1PSID and __Secure-1PSIDTS)
python main.py add-web --label "Web Pro 2"
```

> [!TIP]
> Each web account's cookies are maintained in an isolated file at `config/cookies_cache/{account_id}.json` protected by a dedicated file lock (`cookie_write_lock`). The server periodically refreshes session cookies in the background.

### Checking Pool Status & Quotas

Print an overview of pool health, active concurrency, and supported models:

```bash
# Detailed telemetry status
python main.py status
# Or alias
python main.py health
# Or top-level flag
python main.py --status
```

Print a formatted table of all registered accounts, priority scores, and quota percentages:

```bash
python main.py list-accounts
# Or
python main.py --list-accounts
```

### Immediate Quota Synchronization

Trigger an immediate quota synchronization pass across all OAuth and Web accounts:

```bash
python main.py sync-quotas
# Or
python main.py --sync-quotas
```

---

## API Usage & Client Integration

### Endpoints Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/v1/models` | List all available base models and reasoning effort variants in OpenAI schema. |
| `GET` | `/v1/models/{model_id}` | Retrieve metadata for a specific model ID. |
| `POST` | `/v1/chat/completions` | Create streaming or non-streaming chat completions with failover. |
| `GET` | `/api/status` | Live JSON telemetry report for dashboard and health probes. |
| `POST` | `/api/accounts/sync-quotas` | Force immediate pool-wide quota refresh. |
| `GET` | `/api/model-routes` | List all discovered models, active channel preferences, custom overrides, and supporting accounts. |
| `POST` | `/api/model-routes` | Dynamically create or update primary and failover channel routing for a model or alias. |
| `DELETE` | `/api/model-routes/{model}` | Remove custom route override and revert model/alias to default system routing. |
| `GET` | `/` or `/dashboard` | Serve the administrative web UI. |

### Dynamic Model Routing REST API and Persistence

The gateway exposes REST endpoints allowing agent harnesses, CI/CD pipelines, and operators to dynamically reconfigure compute channels and model aliases at runtime without restarting the server.

#### 1. Retrieve Dynamic Route Table (`GET /api/model-routes`)

Returns all discovered models and user-defined aliases along with effective primary/failover channels, custom status, and supporting account counts across OAuth and Web pools:

```bash
curl http://127.0.0.1:8000/api/model-routes
```

```json
{
  "success": true,
  "routes": [
    {
      "model": "gemini-4-argon",
      "primary_channel": "web",
      "failover_channel": null,
      "primary": "web",
      "failover": null,
      "is_custom": false,
      "is_discovered": true,
      "supporting_accounts": {
        "oauth": 0,
        "web": 2
      }
    },
    {
      "model": "gemini-3.1-pro",
      "primary_channel": "web",
      "failover_channel": "oauth",
      "primary": "web",
      "failover": "oauth",
      "is_custom": false,
      "is_discovered": true,
      "supporting_accounts": {
        "oauth": 2,
        "web": 2
      }
    },
    {
      "model": "main_agent",
      "primary_channel": "web",
      "failover_channel": "oauth",
      "primary": "web",
      "failover": "oauth",
      "is_custom": true,
      "is_discovered": false,
      "supporting_accounts": {
        "oauth": 2,
        "web": 2
      }
    }
  ],
  "custom_routes": {
    "main_agent": {
      "primary": "web",
      "failover": "oauth"
    }
  },
  "default_routes": {
    "gemini-4-argon": { "primary": "web", "failover": null },
    "gemini-3.1-pro": { "primary": "web", "failover": "oauth" },
    "gemini-3.8-flash": { "primary": "oauth", "failover": "web" },
    "gemini-3.1-flash-lite": { "primary": "oauth", "failover": "web" }
  }
}
```

#### 2. Create or Update Route (`POST /api/model-routes`)

Create a custom alias or override channel preferences for an existing model. Set `failover_channel` to `null` (or `"none"`) to enforce exclusive channel execution:

```bash
# Define custom alias 'main_agent' (Web primary with OAuth failover)
curl -X POST http://127.0.0.1:8000/api/model-routes \
  -H "Content-Type: application/json" \
  -d '{
    "model": "main_agent",
    "primary_channel": "web",
    "failover_channel": "oauth"
  }'

# Override 'gemini-3.1-pro' to strictly use Web channel only (no failover)
curl -X POST http://127.0.0.1:8000/api/model-routes \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "primary_channel": "web",
    "failover_channel": null
  }'
```

```json
{
  "success": true,
  "message": "Route for model 'main_agent' updated successfully.",
  "route": {
    "model": "main_agent",
    "primary_channel": "web",
    "failover_channel": "oauth",
    "primary": "web",
    "failover": "oauth",
    "is_custom": true
  }
}
```

#### 3. Reset Route to Default (`DELETE /api/model-routes/{model}`)

Remove a custom override or alias. The model immediately reverts to its baseline system routing:

```bash
curl -X DELETE http://127.0.0.1:8000/api/model-routes/main_agent
```

```json
{
  "success": true,
  "message": "Custom route for model 'main_agent' deleted successfully.",
  "model": "main_agent",
  "effective_primary_channel": "oauth",
  "effective_failover_channel": "web"
}
```

#### 4. Disk Persistence (`config/model_routes.json`)

All route updates are atomically persisted to disk:
- **Storage File**: `config/model_routes.json` (configurable via `MODEL_ROUTES_FILE_PATH`).
- **Atomic Concurrency**: Written to `model_routes.json.tmp`, flushed with `os.fsync()`, and swapped into place using `os.replace()` protected by `accounts_file_lock`.
- **JSON Schema**:
  ```json
  {
    "main_agent": {
      "primary": "web",
      "failover": "oauth"
    },
    "sub_agent": {
      "primary": "oauth",
      "failover": "web"
    },
    "gemini-4-argon": {
      "primary": "web",
      "failover": null
    }
  }
  ```

### Python OpenAI SDK Sample

The gateway functions as a drop-in replacement for any OpenAI-compatible client library (`openai>=1.0.0`):

```python
from openai import OpenAI

# Initialize client pointing to local gateway
client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-gemini-hybrid-pool",  # Any non-empty string
)

# 1. Flagship Reasoning Model with Streaming & Thoughts
response = client.chat.completions.create(
    model="gemini-4-argon",
    messages=[
        {"role": "system", "content": "You are a distributed systems architect."},
        {"role": "user", "content": "Explain raft leader election in detail."},
    ],
    stream=True,
    reasoning_effort="high",
)

print("--- Streaming Thoughts & Response ---")
for chunk in response:
    delta = chunk.choices[0].delta
    # Live streaming reasoning tokens (emitted as delta.reasoning_content)
    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
        print(f"\033[90m{delta.reasoning_content}\033[0m", end="", flush=True)
    # Output response tokens
    if delta.content:
        print(delta.content, end="", flush=True)
print()

# 2. Sub-Agent Coding Model with Function / Tool Calling
tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents from local filesystem",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Target file path"}
                },
                "required": ["path"],
            },
        },
    }
]

completion = client.chat.completions.create(
    model="gemini-3.8-flash",
    messages=[
        {"role": "user", "content": "Inspect the configuration in config/settings.py"}
    ],
    tools=tools,
    tool_choice="auto",
    stream=False,
)

message = completion.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        print(f"Tool: {tool_call.function.name}")
        print(f"Args: {tool_call.function.arguments}")
```

### cURL Request Sample

#### Streaming Request with Reasoning

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini-hybrid-pool" \
  -d '{
    "model": "gemini-4-argon",
    "messages": [
      {"role": "user", "content": "Write a short proof that sqrt(2) is irrational."}
    ],
    "stream": true,
    "reasoning_effort": "high"
  }'
```

#### Non-Streaming Request with Tool Call

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini-hybrid-pool" \
  -d '{
    "model": "gemini-3.8-flash",
    "messages": [
      {"role": "user", "content": "List files in the current directory."}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "list_directory",
          "description": "List files in a given directory path",
          "parameters": {
            "type": "object",
            "properties": {
              "path": {"type": "string"}
            },
            "required": ["path"]
          }
        }
      }
    ]
  }'
```

### Agent Harness Configuration

The JSON snippet below illustrates client-side configuration within autonomous agent harnesses (such as [Pi](https://github.com/can1357/oh-my-pi), Hermes, Cline, Roo-Code, or Cursor):

```json
{
  "provider": "openai-compatible",
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "sk-gemini-hybrid-pool",
  "main_agent_model": "gemini-4-argon",
  "sub_agent_model": "gemini-3.8-flash",
  "fallback_model": "gemini-3.1-flash-lite",
  "max_parallel_sub_agents": 6
}
```

#### Seamless Mapping to Gateway Dynamic Routing
The agent harness's model choices map directly to the gateway's dynamic routing table without requiring custom client code:
1. **Main Reasoning Agent (`main_agent_model: "gemini-4-argon"` or alias `"main_agent"`)**:
   - The harness dispatches high-level planning and architectural reasoning tasks.
   - The gateway routes requests to the **Web Cookie Pool** (Google AI Pro sessions) for extended context and superior multi-turn reasoning capabilities.
2. **High-Concurrency Sub-Agents (`sub_agent_model: "gemini-3.8-flash"` or alias `"sub_agent"`)**:
   - The harness spawns parallel sub-agents to inspect files, execute grep operations, run tests, and perform iterative refactoring.
   - The gateway routes these requests to the **OAuth CLI Pool** (CloudCode Assist), preventing quota depletion on primary web subscriptions while delivering sub-second TTFT.
3. **Fallback & Free-Tier Quota Shield (`fallback_model: "gemini-3.1-flash-lite"`)**:
   - Used for simple auxiliary checks and classification tasks.
   - Evaluated as zero-deduction lightweight calls on OAuth CLI before falling back to Web.
4. **Universal Standardized Aliases (e.g., `gpt-4o`, `claude-3-7-sonnet`)**:
   - For agent harnesses hardcoded to standard OpenAI model names (such as `gpt-4o`), simply register a custom alias `gpt-4o` in the Web Dashboard or `config/model_routes.json` with Primary `web` and Failover `oauth`.
   - The gateway dynamically publishes `gpt-4o` in `GET /v1/models`, translating requests seamlessly to your chosen backend channels.

---

## Real-Time Web Dashboard

The gateway includes an embedded single-page monitoring dashboard accessible at:

```
http://localhost:8000/
# or
http://localhost:8000/dashboard
```

### Dashboard Overview and Health Telemetry

Key monitoring features:
- **Pool-Wide Health Indicators**: Live status badges (`HEALTHY`, `DEGRADED`, `EXHAUSTED`, `EMPTY`), overall account count, and total active concurrency.
- **Quota Tracking & Reset Countdowns**: Visual progress bars representing 5-hour rolling quotas, weekly limits, and countdown timers to the next reset window.
- **Account Score Telemetry**: Real-time display of mathematical priority scores ($P$), concurrency counts ($C$), and soonest-reset multipliers ($\tau$).
- **Live Onboarding Controls**: Interactive forms to initiate OAuth 2.0 PKCE login flows or register web accounts directly from the browser.
- **Instant Quota Refresh**: One-click button triggering the `POST /api/accounts/sync-quotas` endpoint.

### Dynamic Model Routing and Compute Allocation UI

The dashboard features a visual management section dedicated to compute allocation:
**"🧭 Dynamic Model Routing & Compute Allocation | توجيه النماذج وقنوات الحوسبة"**

#### Key Dashboard Capabilities:
- **Instant Channel Reassignment**: Reassign Primary Channel (`OAuth CLI` vs `Web Cookie`) and Failover Channel (`Web Cookie`, `OAuth CLI`, or `None / Exclusive`) directly through responsive inline dropdowns. Changes auto-save via AJAX to `POST /api/model-routes` and take effect immediately.
- **Custom Model & Alias Creation**: Register custom harness aliases (`main_agent`, `sub_agent`, `gpt-4o`) directly from the browser with designated compute channels.
- **Live Supporting Account Badges**: Displays the exact number of active registered accounts supporting each model across both channels (`OAuth: N | Web: M`), giving immediate visibility into pool redundancy.
- **One-Click Route Reset**: Instantly revert any customized route back to its baseline default configuration (`DELETE /api/model-routes/{model}`).

#### Visual Interface Representation:

```text
+-----------------------------------------------------------------------------------------------------------------------------+
| 🧭 Dynamic Model Routing & Compute Allocation | توجيه النماذج وقنوات الحوسبة                                [🔄 Refresh Routes] |
| Configure primary compute channel and automatic failover per model or custom agent alias. Route preferences take            |
| effect immediately for all harness sub-agent requests and OpenAI completions, persisting to config/model_routes.json.      |
+-----------------------------------------------------------------------------------------------------------------------------+
| Model Name / Custom Alias         Primary Channel              Failover Channel             Action                          |
| [ main_agent, gpt-4o, ...       ] [ Web Cookie (Google AI) ▼ ] [ OAuth CLI (CloudCode) ▼  ] [ + Add / Override Route     ] |
+-----------------------------------------------------------------------------------------------------------------------------+
| MODEL / ALIAS           STATUS          PRIMARY CHANNEL              FAILOVER CHANNEL             ACCOUNTS   ACTION         |
| gemini-4-argon          [Discovered]    [Web Cookie (Google AI)  ▼]  [None (Exclusive)         ▼] OAuth:0 W:2  [Reset Route] |
| gemini-3.1-pro          [Discovered]    [Web Cookie (Google AI)  ▼]  [OAuth CLI (CloudCode)    ▼] OAuth:2 W:2  [Reset Route] |
| gemini-3.8-flash        [Discovered]    [OAuth CLI (CloudCode)   ▼]  [Web Cookie (Google AI)   ▼] OAuth:2 W:2  [Reset Route] |
| gemini-3.1-flash-lite   [Discovered]    [OAuth CLI (CloudCode)   ▼]  [Web Cookie (Google AI)   ▼] OAuth:2 W:2  [Reset Route] |
| main_agent              [Custom Alias]  [Web Cookie (Google AI)  ▼]  [OAuth CLI (CloudCode)    ▼] OAuth:2 W:2  [Delete Alias]|
| sub_agent               [Custom Alias]  [OAuth CLI (CloudCode)   ▼]  [Web Cookie (Google AI)   ▼] OAuth:2 W:2  [Delete Alias]|
| gpt-4o                  [Custom Alias]  [Web Cookie (Google AI)  ▼]  [OAuth CLI (CloudCode)    ▼] OAuth:2 W:2  [Delete Alias]|
+-----------------------------------------------------------------------------------------------------------------------------+
```

---

## Verification & Testing

The gateway is backed by a 6-phase test suite verifying every component from Level 0 to Level 5.

Run the test suite via `pytest`:

```bash
# Run all tests
pytest -v

# Run with concise output
pytest -q

# Run specific verification gates & test suites
pytest tests/test_level0_gate1.py         # Gate 1: State schemas, atomic snapshots, LMDB stores
pytest tests/test_level1_gate2.py         # Gate 2: Schema sanitizer, zero-required fix, WAF shield
pytest tests/test_level2_gate3.py         # Gate 3: OAuth PKCE, Firefox cookies, 3-tier rehydration
pytest tests/test_level3_gate4.py         # Gate 4: Channels, lookahead parser, quota scraper
pytest tests/test_level4_gate5.py         # Gate 5: Scheduler, mathematical scoring, pre-stream handshake
pytest tests/test_level5_gate6.py         # Gate 6: OpenAI routes, dashboard APIs, Click CLI
pytest tests/test_model_routing_dynamic.py # Dynamic model routing, persistence, and REST endpoints
```

**Test Verification Status**:
```text
============================== 386 passed in ~12s ==============================
```

---

## Security & Cookie Hygiene

> [!WARNING]
> **Chromium DBSC Encryption Notice**:
> Modern Chromium-based browsers (Google Chrome, Microsoft Edge, Brave) enforce **Device Bound Session Credentials (DBSC)**, encrypting session tokens with device-bound TPM keys. Extracted cookies from Chromium browsers will be invalidated by Google within hours.
> **Always use Mozilla Firefox** to extract web cookies for the gateway. Firefox cookies remain valid indefinitely until manually logged out.

> [!IMPORTANT]
> **Disable "Saved Info" in Gemini Web**:
> In your Google account's Gemini settings, disable the **"Saved Info" (Personal Context)** feature. If enabled, Google injects persistent user background facts into model context, which can distort agent instructions and unit test executions.
