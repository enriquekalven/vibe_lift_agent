# VibeLift — Gemini Enterprise Agent Fleet Observability, FinOps & Optimization Studio

[![tests](https://github.com/enriquekalven/vibe_lift_agent/actions/workflows/tests.yml/badge.svg)](https://github.com/enriquekalven/vibe_lift_agent/actions/workflows/tests.yml)

**VibeLift** is an enterprise observability, prompt cache FinOps, and autonomous multi-objective optimization platform built on Google Cloud with the **Google Agent Development Kit (ADK)**, **FastAPI**, and the **Model Context Protocol (MCP)**.

Designed for production deployment on **Google Cloud Run** and native embedding inside **Gemini Enterprise**, VibeLift enumerates registered agents across global and regional Gemini Enterprise instances, joins inventory with real-time Google Cloud Monitoring and OpenTelemetry `gen_ai` logs, measures prompt cache and skill/MCP token economics, and runs closed-loop prompt optimization across **AlphaEvolve**, **Opus Frontier Critic**, **Google Vizier**, and **Hybrid Ensemble**.

---

## Architecture Overview

```mermaid
flowchart TB
  subgraph GE["Gemini Enterprise (Global & Regional US/EU Engines)"]
    Chat["Gemini Enterprise Assistant & StreamAssist"]
    SidePanel["Embedded MCP App Host (Right Side Panel & Fullscreen)"]
  end

  subgraph CR["Google Cloud Run — vibe-lift-agent (Private IAM)"]
    MCP["Streamable HTTP MCP Server (/mcp)\nJSON-RPC 2.0 • Protocol 2025-06-18"]
    UI["4-Tab Interactive MCP UI App\nui://vibelift-analytics/dashboard"]
    ADK["ADK Root Agent (app/agent.py)\nGemini 2.5 Flash • Vertex AI"]
    REST["FastAPI Telemetry & Optimization API (/api/*)"]
  end

  subgraph GCP["Google Cloud Telemetry & Agent Runtimes"]
    DE["Discovery Engine API\n(Multi-Region Agent Inventory)"]
    RE["Vertex AI Agent Engine\n(ADK Reasoning Engines)"]
    Mon["Cloud Monitoring API\n(Requests, Latency, vCPU/GiB, Publisher Tokens)"]
    Log["Cloud Logging & BigQuery\n(OpenTelemetry gen_ai Events & Audit Views)"]
    Dec["@vibelift_telemetry Python Decorator\n(Real-Time Function & Tool Spans)"]
  end

  Chat <-->|"BYO MCP Connector (vibelift-analytics-mcp)"| MCP
  MCP -->|"open_dashboard + embedded state"| SidePanel
  SidePanel <-->|"AppBridge postMessage / REST"| UI
  UI <--> REST
  ADK --> REST
  REST --> DE
  REST --> RE
  REST --> Mon
  REST --> Log
  Dec -->|"In-Memory & POST /api/decorator_ingest"| REST
```

---

## Core Capabilities

### 1. Live Gemini Enterprise Agent Fleet (`Tab 0`)
- **Multi-Region Discovery Engine Inventory**: Enumerates every agent deployed across configured Gemini Enterprise engines (`VIBELIFT_GE_ENGINES`, supporting both `global/engine-id` and regional `us/engine-id` or `eu/engine-id` specifications) across all assistants and pages.
- **Joined Cloud Telemetry**:
  - **ADK Agents on Vertex AI Agent Engine**: Joins Cloud Monitoring `reasoning_engine` metrics (request volume, 4xx/5xx error rates, p50/p95 latency, vCPU and memory allocation) with per-agent OpenTelemetry `gen_ai` log events (LLM invocations, input/output tokens, cached tokens, active conversations, and last activity timestamp). Prompt message bodies are never read into the payload.
  - **A2A Agents on Cloud Run**: Joins service-level `run.googleapis.com` request, error, latency, and container allocation metrics from the agent card endpoint.
  - **Low-Code / Workflow & Google-Managed Agents**: Automatically classifies Agent Designer (`lowCodeAgentDefinition`, `workflowAgentDefinition`), Dialogflow, and Google-managed agents (`Deep Research`).
- **Project-Wide Model Economics**: Aggregates Vertex AI publisher model invocations and token counts (`publisher/online_serving/*`) priced against list rate cards alongside Gemini Enterprise `StreamAssist` traffic.
- **Zero Synthetic Fallbacks**: Unavailable telemetry sources return `null` metrics with explicit diagnostics in `source_status` and `errors`. Fleet snapshots are cached in-memory (`VIBELIFT_FLEET_TTL_SECONDS=60`) with rate-limited forced refreshes (`VIBELIFT_FLEET_MIN_REFRESH_SECONDS=10`).

### 2. Prompt Cache Economics & Prefix Breakpoint Diagnostics (`Tab 1`)
- **Turn-by-Turn Token Valuation**: Tracks total input tokens, cached prefix tokens, uncached input tokens, and output tokens across multi-turn agent trajectories using official model rate cards (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash`, `claude-3-7-sonnet`, `claude-3-5-sonnet`).
- **Prefix Breakpoint Detection**: Pinpoints the exact line and character offset where dynamic tokens (such as timestamps, session UUIDs, or unpinned tool schemas) invalidate static prefix caching.
- **Custom Multi-Objective Parameters**: Allows operators to register and weight custom optimization parameters (`minimize` or `maximize`) with live baseline-to-current delta tracking.

### 3. Multi-Platform Optimization Studio (`Tab 2`)
- **Pluggable Optimizer Backends**: Supports live switching and side-by-side comparison across four optimization engines:
  - **AlphaEvolve** (`alpha_evolve`) — Evolutionary Pareto-frontier prompt prefix, schema, and context optimization (`DEFAULT`).
  - **Opus Frontier Critic** (`opus_critic`) — Frontier LLM structural prompt refactoring, instruction synthesis, and schema compaction.
  - **Google Vizier** (`vertex_vizier`) — Distributed black-box Bayesian hyperparameter and context-window tuner.
  - **Hybrid Ensemble** (`hybrid_ensemble`) — Combined pipeline uniting Opus structural critique, Google Vizier numerical tuning, and AlphaEvolve Pareto selection.
- **Closed-Loop Safety Guardrails**: Evaluates candidate prompt mutations against accuracy, safety, and latency guardrails—automatically committing Pareto improvements or rolling back regressions when anomalies occur.

### 4. User-Centric FinOps & `@vibelift_telemetry` Decorator (`Tab 3`)
- **Zero-Boilerplate Python Decorator**: Instrument any ADK tool, agent turn, or Python function (sync or async) with `@vibelift_telemetry(agent_id=..., skill_name=..., mcp_tool=...)` to capture real-time wall-clock latency, token consumption, context bloat, and idle ratio metrics.
- **Context Bloat & Idle Cost Attribution**: Separates wasted uncached context bloat (`context_bloat_pct`) and idle compute provisioning overhead (`idle_ratio_pct`) from productive token spend.
- **Per-User & Skill/MCP Token Economics**: Surfaces user-level cache hit rates, waste attribution, and token cost per call across agent skills and MCP tools, alongside prioritized optimization recommendations.

### 5. Native Gemini Enterprise BYO MCP App (`ui://vibelift-analytics/dashboard`)
- **Streamable HTTP MCP Server**: Compliant with MCP specification versions `2025-06-18` and `2025-03-26` at `/mcp`.
- **Responsive Side-Panel & Fullscreen Workspace**: Opens natively in the Gemini Enterprise **Right Side Panel (`pip`)** by default for side-by-side chat analysis, with a one-click **Fullscreen (`fullscreen`)** toggle powered by the MCP AppBridge (`ui/request-display-mode`).
- **Embedded Live State**: Every `open_dashboard` invocation and `resources/read` response embeds a fresh server-side snapshot of `state`, `gcp_telemetry`, and `ge_fleet` directly into the HTML payload so the dashboard renders live fleet data immediately inside sandboxed iframes without waiting on cross-origin XHR calls.

---

## Repository Structure

```text
vibe_lift_agent/
├── app/                         # ADK entry points (agents-cli agent_directory)
│   ├── agent.py                 # ADK root_agent and tools
│   └── fast_api_app.py          # Production ASGI app (Cloud Run CMD)
├── vibelift/                    # Application package
│   ├── server.py                # REST routes + standalone HTTP server
│   ├── mcp_server.py            # MCP JSON-RPC 2.0 server and MCP App bridge
│   ├── fleet.py                 # Gemini Enterprise inventory joined with Monitoring, Logging and Trace
│   ├── finops.py                # Live token economics, spend drift, what-if projections
│   ├── billing_export.py        # Cloud Billing export (BigQuery) reader
│   ├── gcp_telemetry.py         # Cloud Run, Logging and BigQuery collectors
│   ├── telemetry.py             # Rate cards, cache economics, @vibelift_telemetry
│   ├── validator.py             # Deterministic checks + LLM-as-judge audit
│   ├── optimizer.py             # Optimization engine and user analytics
│   ├── long_running_agent.py    # Multi-turn trajectory simulator
│   └── ui/
│       ├── template.py          # Dashboard HTML/JS
│       ├── logo_asset.py        # Embedded brand assets
│       └── static/              # Source logo images
├── tests/                       # 84 offline tests (see docs/TESTING.md)
├── deploy/
│   ├── deploy_cloud_run.sh      # Tests, then idempotent Cloud Run + IAM deploy
│   ├── cloudbuild.yaml          # Build -> test -> push -> deploy
│   ├── cloud_run_service.yaml   # Declarative service reference
│   └── setup_bigquery_sink.sh   # BigQuery dataset, views and Logging sink
├── docs/
│   ├── spec.md                  # Architecture and data-source specification
│   ├── TESTING.md               # Requirement -> test map, CI gates
│   ├── SKILL.md                 # Agent skill reference
│   └── mcp_spec.json            # MCP tool catalog
├── .github/workflows/tests.yml  # CI: full test suite on push and PR
├── Dockerfile
├── pyproject.toml, requirements.txt, constraints.txt
└── agents-cli-manifest.yaml
```

---

## Configuration & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GOOGLE_CLOUD_PROJECT` | Metadata server / active `gcloud` project | Target GCP Project ID. Falls back to `UNCONFIGURED-PROJECT` if unset so it never queries an unintended project. |
| `GOOGLE_CLOUD_REGION` | `us-central1` | Primary region for Cloud Run and Vertex AI. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` (when no API key is set) | Routes ADK `root_agent` model calls through Vertex AI using the runtime service account. |
| `GOOGLE_CLOUD_LOCATION` | `GOOGLE_CLOUD_REGION` | Vertex AI location for ADK model calls. |
| `VIBELIFT_GE_ENGINES` | `agent-platform-demo` | Comma-separated Gemini Enterprise engine IDs to inventory. Supports `location/engine_id` syntax (e.g., `us/gemini-enterprise-17649552_1764955289529,global/agent-platform-demo`). |
| `VIBELIFT_GE_LOCATION` | `global` | Default Discovery Engine location (`global`, `us`, `eu`) when not prefixed in `VIBELIFT_GE_ENGINES`. |
| `VIBELIFT_GE_COLLECTION` | `default_collection` | Discovery Engine collection ID. |
| `VIBELIFT_FLEET_TTL_SECONDS` | `60` | In-memory cache TTL (seconds) per telemetry time window. |
| `VIBELIFT_FLEET_MIN_REFRESH_SECONDS` | `10` | Minimum cooldown interval (seconds) between forced fleet refreshes. |
| `VIBELIFT_FLEET_WINDOW_HOURS` | `24` | Default telemetry aggregation window in hours (`1`, `6`, `24`, or `168`). |
| `VIBELIFT_TOKEN_LOG_MAX_ENTRIES` | `3000` | Maximum OpenTelemetry `gen_ai` log entries scanned per fleet collection. |
| `VIBELIFT_RATE_CARDS_JSON` | *(empty)* | Optional JSON override for model token pricing rate cards. |
| `VIBELIFT_MONITORED_SERVICES` | *(empty)* | Optional comma-separated Cloud Run services to include in `/api/gcp_telemetry`. |
| `VIBELIFT_PUBLIC_URL` | `https://SERVICE-PROJECT_NUMBER.REGION.run.app` | Canonical public URL published in the A2A agent card and MCP metadata. |
| `PUBLIC_A2A_URL` | `${VIBELIFT_PUBLIC_URL}/a2a/app` | Explicit override for the `url` field in the A2A agent card. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origin allowlist. |
| `ENABLE_MCP_APP` | `1` | Enables (`1`) or disables (`0`) the `/mcp` JSON-RPC 2.0 server. |
| `MCP_PROTOCOL_VERSION` | `2025-06-18` | Negotiated MCP protocol version (`2025-06-18` or `2025-03-26`). |

---

## Local Development & Testing

### 1. Environment Setup
Use Python 3.12+ and install locked dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
```

### 2. Run the Offline Test Suite
The test suite validates all telemetry calculations, `@vibelift_telemetry` decorator instrumentation, multi-platform optimization mutations, REST routes, MCP JSON-RPC methods, and regional/global Discovery Engine fleet joins without requiring live network calls:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline.json \
GOOGLE_CLOUD_PROJECT=test-project \
python -m unittest discover -s tests -t . -v
```
See [docs/TESTING.md](docs/TESTING.md) for which test enforces which requirement.

### 3. Run the Local Server
```bash
python -m vibelift.server --port=8080
```
Open [http://localhost:8080](http://localhost:8080) to inspect the 4-Tab interactive dashboard locally.

---

## Instrumenting Functions with `@vibelift_telemetry`

Decorate any synchronous or asynchronous agent function, skill, or tool to stream real-time execution metrics into **Tab 3 (User-Centric FinOps & Decorator)**:

```python
from vibelift.telemetry import vibelift_telemetry

@vibelift_telemetry(
    agent_id="adk_service_desk",
    user_id="enriq@google.com",
    skill_name="it-ticket-triage",
    mcp_tool="query_ge_agent_fleet",
    model_name="gemini-2.5-flash",
)
def handle_support_request(query: str) -> dict:
    # Return dict or object with optional token usage metadata
    return {
        "status": "resolved",
        "input_tokens": 8200,
        "cached_tokens": 6800,
        "output_tokens": 420,
    }
```

External services can also push telemetry spans over HTTP via `POST /api/decorator_ingest`.

---

## Production Deployment to Google Cloud Run

### Option A: Automated Deployment Script (`deploy/deploy_cloud_run.sh`)
```bash
./deploy/deploy_cloud_run.sh
```
This script runs the full test suite first and aborts on any failure (`VIBELIFT_SKIP_TESTS=1` overrides). It is idempotent and performs the following steps:
1. Enables required Google Cloud APIs (`run`, `cloudbuild`, `artifactregistry`, `logging`, `monitoring`, `discoveryengine`, `aiplatform`, `bigquery`).
2. Provisions the dedicated runtime service account `vibe-lift-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com`.
3. Creates/updates the custom least-privilege role `vibeLiftGeFleetReader` (`discoveryengine.engines.get`, `assistants.list`, `agents.list`, `agents.get`, `agents.manage`) so `agents.list` returns all agents across the Gemini Enterprise instance without granting destructive admin permissions.
4. Deploys `vibe-lift-agent` to Cloud Run (`--no-allow-unauthenticated`, `min-instances=1`, `max-instances=10`, `1 vCPU`, `1 GiB` RAM) with `VIBELIFT_GE_ENGINES` configured for both US and Global Gemini Enterprise instances.
5. Grants `roles/run.invoker` to the Discovery Engine service agent (`service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com`) so Gemini Enterprise can invoke `/mcp` securely.

### Option B: Cloud Build CI/CD Pipeline (`deploy/cloudbuild.yaml`)
```bash
# Build image, execute full offline unit test suite, push to Artifact Registry, and deploy:
gcloud builds submit --config=deploy/cloudbuild.yaml .

# Build, test, and push image only (skip Cloud Run deployment step):
gcloud builds submit --config=deploy/cloudbuild.yaml --substitutions=_DEPLOY=false .
```

### BigQuery Analytics Sink (`deploy/setup_bigquery_sink.sh`)
To provision the partitioned BigQuery dataset (`vibelift_analytics`), reporting views (`vw_fleet_finops_summary`), and Cloud Logging sink (`vibelift-telemetry-sink`):
```bash
./deploy/setup_bigquery_sink.sh
```

---

## Gemini Enterprise BYO MCP Integration

VibeLift registers in Gemini Enterprise as a **Custom MCP Data Connector** (`vibelift-analytics-mcp`) and attaches its federated `mcp_data` data store (`vibelift-analytics-mcp_mcp_data`) to any Global (`locations/global`) or Regional (`locations/us`, `locations/eu`) Gemini Enterprise engine.

### Connection Details
- **MCP Endpoint URL**: `https://vibe-lift-agent-<PROJECT_NUMBER>.<REGION>.run.app/mcp`
- **Connector Type**: `custom_mcp` (`THIRD_PARTY_FEDERATED`, modes `["ACTIONS", "FEDERATED"]`)
- **UI Resource URI**: `ui://vibelift-analytics/dashboard` (`text/html;profile=mcp-app`)
- **Exposed MCP Tools**:
  - `open_dashboard`: Renders the 4-Tab interactive VibeLift dashboard in the Gemini Enterprise Right Side Panel (with one-click Fullscreen expansion).
  - `query_ge_agent_fleet`: Returns live inventory and telemetry across all agents deployed on Gemini Enterprise.
  - `query_project_telemetry`: Returns live Cloud Run service metrics and BigQuery triage log summaries.
  - `calculate_prompt_cache_economics`: Evaluates prompt prefix cache hit rates and dollar savings.
  - `run_alpha_evolve_generation`: Executes a closed-loop optimization generation on the selected platform (`alpha_evolve`, `opus_critic`, `vertex_vizier`, `hybrid_ensemble`).
  - `get_vibelift_state`: Returns the complete active agent state, parameters, and turn trajectory.

---

## REST API & MCP Endpoint Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` , `/ui` , `/app` | `GET` | Interactive 4-Tab VibeLift Analytics, FinOps & Optimization Dashboard |
| `/mcp` | `POST` | Streamable JSON-RPC 2.0 MCP server (`initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/read`) |
| `/mcp` | `DELETE` | Terminates an MCP session (`HTTP 204`) |
| `/health` | `GET` | External Cloud Run health check (`{"status": "ok"}`) |
| `/healthz` | `GET` | Container-internal & local health probe (`{"ok": true}`) |
| `/.well-known/agent-card.json` | `GET` | A2A protocol metadata card (also served at `/a2a/app/.well-known/agent-card.json`) |
| `/api/state` | `GET` | Full runtime state including optimization platforms, decorator telemetry, and User-Centric FinOps |
| `/api/ge_fleet` | `GET` | Live Gemini Enterprise agent fleet inventory & telemetry (`?window_hours=24&force_refresh=false`) |
| `/api/sync_ge_fleet` | `POST` | Triggers an immediate Gemini Enterprise fleet collection (`window_hours`) |
| `/api/gcp_telemetry` | `GET` | Live Google Cloud Run service metrics and BigQuery triage log telemetry |
| `/api/sync_gcp_telemetry` | `POST` | Syncs live Google Cloud Logging turns into the active agent profile |
| `/api/user_centric_finops` | `GET` | Per-user token efficiency, Context Bloat %, Skill/MCP token breakdown, and `@vibelift_telemetry` stream |
| `/api/select_optimizer` | `POST` | Switches active optimization platform (`platform_id`: `alpha_evolve`, `opus_critic`, `vertex_vizier`, `hybrid_ensemble`) |
| `/api/decorator_ingest` | `POST` | Ingests a real-time `@vibelift_telemetry` span from an instrumented function or tool |
| `/api/select_agent` | `POST` | Switches the active optimization agent profile (`agent_id`) |
| `/api/add_parameter` | `POST` | Registers or updates a weighted multi-objective optimization parameter |
| `/api/inject_anomaly` | `POST` | Simulates a production prefix-cache or latency regression for guardrail testing |
| `/api/evolve_generation` | `POST` | Runs a closed-loop optimization generation (`platform_id` optional) |
| `/api/reset` | `POST` | Resets optimizer trajectories, parameters, and decorator telemetry to baseline |

---

## Security & Operational Guardrails

- **Private IAM Authentication**: Cloud Run is deployed with `--no-allow-unauthenticated`. Only principals granted `roles/run.invoker` (including the Discovery Engine service agent) can invoke `/`, `/api/*`, or `/mcp`.
- **Strict Content Security Policy (CSP)**: Every HTML response sets `Content-Security-Policy` headers permitting framing by `*.cloud.google.com`, `*.corp.google.com`, and `*.pantheon.corp.google.com` while blocking `object-src` and untrusted scripts.
- **Privacy-Preserving Log Ingestion**: `vibelift/fleet.py` extracts only numeric token counters, model names, and anonymized conversation identifiers from OpenTelemetry `gen_ai` logs; user prompts and model completions are never stored or returned.
- **Reference-ID Error Sanitization**: Unhandled exceptions in MCP tool calls and JSON-RPC handlers log full stack traces to Cloud Logging with a correlation `ref=<id>` while returning only the sanitized exception class and reference ID to the caller.