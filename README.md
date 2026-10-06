# VibeLift — Gemini Enterprise Agent Fleet Observability, FinOps & Optimization Studio

[![tests](https://github.com/enriquekalven/vibe_lift_agent/actions/workflows/tests.yml/badge.svg)](https://github.com/enriquekalven/vibe_lift_agent/actions/workflows/tests.yml)

**VibeLift** is an enterprise observability, prompt cache FinOps, and autonomous multi-objective optimization platform built on Google Cloud with the **Google Agent Development Kit (ADK)**, **FastAPI**, and the **Model Context Protocol (MCP)**.

Designed for production deployment on **Google Cloud Run** and native embedding inside **Gemini Enterprise**, VibeLift enumerates registered agents across global and regional Gemini Enterprise instances, joins inventory with real-time Google Cloud Monitoring and OpenTelemetry `gen_ai` logs, measures prompt cache and skill/MCP token economics, and runs closed-loop prompt optimization across **AlphaEvolve**, **Opus Frontier Critic**, and **Hybrid Ensemble**.

> **Deploying to your own project?**
> - **No AI tools or no local CLI installed?** Follow the **[Manual Deployment Contingency Guide (Browser-Only, Zero Local Setup)](#manual-deployment-contingency-guide-browser-only-zero-local-setup)**.
> - **Standard CLI deployment:** Follow **[Deploy in Your Own GCP Project](#deploy-in-your-own-gcp-project)** for prerequisites, logging, BigQuery, Cloud Run, Gemini Enterprise registration, verification, and [troubleshooting](#troubleshooting).

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
    UI["7-Tab Interactive MCP UI App\nui://vibelift-analytics/dashboard"]
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
- **Pluggable Optimizer Backends**: Supports live switching and side-by-side comparison across three optimization engines:
  - **AlphaEvolve** (`alpha_evolve`) — Evolutionary Pareto-frontier prompt prefix, schema, and context optimization (`DEFAULT`).
  - **Opus Frontier Critic** (`opus_critic`) — Frontier LLM structural prompt refactoring, instruction synthesis, and schema compaction.
  - **Hybrid Ensemble** (`hybrid_ensemble`) — Combined pipeline uniting Opus structural critique and AlphaEvolve Pareto selection.
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
│   ├── ge_mart.py               # BigQuery vibelift_mart SQL builders and readers
│   ├── prompt_xray.py           # Character-level Prompt Cache X-Ray & cache-friendly rewriter
│   ├── sme_eval.py              # 6-Persona x 5-Dimension (30-check) SME evaluation engine
│   ├── telemetry.py             # Rate cards, cache economics, @vibelift_telemetry
│   ├── validator.py             # Deterministic checks + LLM-as-judge audit
│   ├── optimizer.py             # Optimization engine and user analytics
│   ├── long_running_agent.py    # Multi-turn trajectory simulator
│   └── ui/
│       ├── template.py          # Dashboard HTML/JS
│       ├── logo_asset.py        # Embedded brand assets
│       └── static/              # Source logo images
├── tests/                       # Offline test suite (215 tests, no live API calls; see docs/TESTING.md)
├── deploy/
│   ├── deploy_cloud_run.sh      # Tests, then idempotent Cloud Run + IAM deploy
│   ├── cloudbuild.yaml          # Build -> test -> push -> deploy
│   ├── cloud_run_service.yaml   # Declarative service reference
│   ├── setup_bigquery_sink.sh   # BigQuery datasets, Logging sinks, analytics tables, curated views + mart
│   ├── set_log_retention.sh     # Raw log retention (partition expiration, default 90 days)
│   ├── fix_customer_sinks_and_retention.sh # Audits & remediates customer sinks, retention, and mart views
│   ├── setup_mart_refresh.sh    # Hourly BigQuery scheduled query that rebuilds fct_turns
│   ├── register_ge_agent.sh     # Deploys Custom MCP Data Store (BYO_MCP) & registers it + A2A agent to a GE App instance
│   ├── setup_mcp_connector.py   # Deploys Custom MCP Data Store (NOT Agent Registry) & links dataStoreIds to GE App instance
│   └── bigquery/
│       ├── provision_ge_mart.py # Curated views + vibelift_mart (apply / refresh / scheduled-query DDL)
│       ├── verify_ge_mart_invariants.py # 13 conservation & reconciliation invariants across raw/curated/mart
│       └── ge_mart/             # SQL templates for the curated and mart layers
├── docs/
│   ├── PERMISSIONS.md           # Every IAM role, API and org policy VibeLift needs
│   ├── spec.md                  # Architecture and data-source specification
│   ├── GE_MART.md               # Gemini Enterprise curated views and reporting mart
│   ├── LOOKER_STUDIO_GUIDE.md   # 4-page Looker Studio L1/L2 IT Support Dashboard guide
│   ├── SME_EVALUATION_REPORT.md # 6-Persona x 5-Dimension (30-check) SME evaluation report
│   ├── HACKATHON_DEMO_SCRIPT.md # 3-minute high-impact demo script & Q&A cheat sheet
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

Runtime variables are read by the service (set on Cloud Run by `deploy/deploy_cloud_run.sh`, or in `.env` locally; copy [.env.example](.env.example)). Unset variables use the defaults below.

**Core**

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GOOGLE_CLOUD_PROJECT` | Metadata server / active `gcloud` project | Target GCP Project ID (supports both standard and domain-scoped `domain:project-id`). Falls back to `UNCONFIGURED-PROJECT` if unset so it never queries an unintended project. |
| `GOOGLE_CLOUD_REGION` | `us-central1` | Primary region for Cloud Run and Vertex AI. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` (when no API key is set) | Routes ADK `root_agent` model calls through Vertex AI using the runtime service account. |
| `GOOGLE_CLOUD_LOCATION` | `GOOGLE_CLOUD_REGION` | Vertex AI location for ADK model calls. |
| `VIBELIFT_PUBLIC_URL` | `https://SERVICE-PROJECT_NUMBER.REGION.run.app` | Canonical public URL published in the A2A agent card and MCP metadata. |
| `PUBLIC_A2A_URL` | `${VIBELIFT_PUBLIC_URL}/a2a/app` | Explicit override for the `url` field in the A2A agent card. |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Listen address. |
| `USE_UVICORN` | `0` (deploy sets `1`) | Serve the FastAPI app with uvicorn instead of the stdlib server. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origin allowlist. |
| `ENABLE_MCP_APP` | `1` | Enables (`1`) or disables (`0`) the `/mcp` JSON-RPC 2.0 server. |
| `MCP_PROTOCOL_VERSION` | `2025-06-18` | Negotiated MCP protocol version (`2025-06-18` or `2025-03-26`). |

**Gemini Enterprise fleet and runtimes**

| Variable | Default | Description |
| :--- | :--- | :--- |
| `VIBELIFT_GE_ENGINES` | `auto` | `auto` discovers every Gemini Enterprise app in `VIBELIFT_GE_LOCATIONS`. Or a comma-separated list of `location/engine_id` (for example `global/my-app,us/my-us-app`); a bare ID uses `VIBELIFT_GE_LOCATION`. If `auto` finds no apps, the dashboard shows an error instead of guessing. |
| `VIBELIFT_GE_LOCATIONS` | `global,us,eu` | Discovery Engine locations scanned by `auto`. |
| `VIBELIFT_GE_LOCATION` | `global` | Default location for engine IDs given without a `location/` prefix. |
| `VIBELIFT_GE_COLLECTION` | `default_collection` | Discovery Engine collection ID. |
| `VIBELIFT_DISCOVER_UNREGISTERED` | `true` (in a real project) | Also discover standalone Agent Engine, Cloud Run and GKE workloads that are not registered in Gemini Enterprise. |
| `VIBELIFT_RE_LOCATIONS` | `us-central1,us-west1,us-east4,europe-west1` | Regions scanned for standalone Vertex AI Agent Engine (Reasoning Engine) instances. |
| `VIBELIFT_MONITORED_SERVICES` | *(empty)* | Optional comma-separated Cloud Run services to include in `/api/gcp_telemetry`. |
| `VIBELIFT_FLEET_TTL_SECONDS` | `60` | In-memory cache TTL (seconds) per telemetry time window. |
| `VIBELIFT_FLEET_MIN_REFRESH_SECONDS` | `10` | Minimum cooldown interval (seconds) between forced fleet refreshes. |
| `VIBELIFT_FLEET_WINDOW_HOURS` | `24` | Default telemetry aggregation window in hours. |
| `VIBELIFT_TOKEN_LOG_MAX_ENTRIES` | `3000` | Maximum OpenTelemetry `gen_ai` log entries scanned per fleet collection. |
| `VIBELIFT_TRACE_MAX_PAGES` | `6` | Maximum Cloud Trace pages read per collection. |
| `VIBELIFT_BACKGROUND_WARMER` | on in Cloud Run | Set to `1` to pre-warm caches in the background when running outside Cloud Run. |

**BigQuery, cost and audit**

| Variable | Default | Description |
| :--- | :--- | :--- |
| `VIBELIFT_GE_CURATED_DATASET` | `ds_ge_curated_staging` | Curated views dataset (`v_user_activity_curated`, `v_agentic_operations_curated`, `v_consolidated_audit_log`, `v_model_armor_curated`; see [docs/GE_MART.md](docs/GE_MART.md)). |
| `VIBELIFT_GE_MART_DATASET` | `vibelift_mart` | Reporting mart dataset (`v_fct_turns`, `fct_turns`, `fct_sessions`, `agg_daily_usage`, `v_looker_l1_l2_support`). |
| `VIBELIFT_BILLING_EXPORT_TABLE` | *(empty)* | Cloud Billing BigQuery export table (`project.dataset.gcp_billing_export_v1_XXXX`). Empty = billed cost shown as unknown. |
| `VIBELIFT_BQ_MAX_BYTES_BILLED` | `10737418240` (10 GB) | FinOps safeguard capping `maximumBytesBilled` per BigQuery REST query (`0` disables the cap). |
| `VIBELIFT_RATE_CARDS_JSON` | *(empty)* | Optional JSON override for model token pricing rate cards. |
| `VIBELIFT_JUDGE_MODEL` | `gemini-2.5-flash` | Model used by the LLM-as-judge audit. |
| `VIBELIFT_JUDGE_LOCATION` | `us-central1` | Vertex AI location for the judge model. |
| `VIBELIFT_JUDGE_TIMEOUT_S` | `25` | Judge call timeout in seconds. |

**Deploy-script variables** (read by the scripts in `deploy/`, not by the service)

| Variable | Used by | Default | Description |
| :--- | :--- | :--- | :--- |
| `SERVICE_NAME` | all scripts | `vibe-lift-agent` | Cloud Run service name. |
| `VIBELIFT_SKIP_TESTS` | `deploy_cloud_run.sh` | `0` | `1` deploys without running the local test gate (not recommended). |
| `VIBELIFT_INVOKERS` | `deploy_cloud_run.sh` | *(empty)* | Comma-separated IAM members granted `roles/run.invoker` so they can open the dashboard, for example `user:alice@example.com,group:finops@example.com`. |
| `BQ_LOCATION` | `setup_bigquery_sink.sh`, `setup_mart_refresh.sh` | `US` | BigQuery location for all datasets. Keep it the same as your log sinks. |
| `VIBELIFT_RETENTION_DAYS` | `setup_bigquery_sink.sh`, `set_log_retention.sh` | `90` | Days of raw log history kept in BigQuery (partition expiration on the raw sink datasets). The mart reads the same window (`provision_ge_mart.py --lookback-days`, default 90). |
| `VIBELIFT_REFRESH_SCHEDULE` | `setup_mart_refresh.sh` | `every 1 hours` | How often the scheduled query rebuilds `fct_turns` (Data Transfer schedule syntax). |
| `VIBELIFT_REFRESH_SA` | `setup_mart_refresh.sh` | `vibe-lift-runtime-sa@PROJECT.iam.gserviceaccount.com` | Service account the scheduled query runs as. |
| `GE_ENGINE_ID` | `register_ge_agent.sh`, `setup_mcp_connector.py`, `deploy_cloud_run.sh` | *(required for step 4)* | Gemini Enterprise app instance ID to register the GE Data Store to (or pass it as the first argument to `register_ge_agent.sh`). |
| `GE_LOCATION` | `register_ge_agent.sh`, `setup_mcp_connector.py` | `global` | Location of that Gemini Enterprise app instance (`global`, `us`, `eu`). |

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
Open [http://localhost:8080](http://localhost:8080) to inspect the dashboard locally (`make playground` does the same). Locally the server uses your `gcloud auth application-default login` credentials against `GOOGLE_CLOUD_PROJECT`.

---

## Instrumenting Functions with `@vibelift_telemetry`

Decorate any synchronous or asynchronous agent function, skill, or tool to stream real-time execution metrics into **Tab 3 (User-Centric FinOps & Decorator)**:

```python
from vibelift.telemetry import vibelift_telemetry

@vibelift_telemetry(
    agent_id="adk_service_desk",
    user_id="user@your-company.com",
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

## Deploy in Your Own GCP Project

This section takes a fresh Google Cloud project to a working, private VibeLift deployment deployed to a **Gemini Enterprise Data Store** and registered to your **Gemini Enterprise App instance**.

> [!TIP]
> **Non-technical operator or no AI coding tools (no Claude, Antigravity, local Python, or local `gcloud`)?**
> Start directly with the **[Manual Deployment Contingency Guide (Browser-Only, Zero Local Setup)](#manual-deployment-contingency-guide-browser-only-zero-local-setup)** below. You only need a web browser and access to the Google Cloud Console.

---

### Manual Deployment Contingency Guide (Browser-Only, Zero Local Setup)

Use this contingency guide if you **do not** have Claude, Antigravity, `git`, Python, or the Google Cloud CLI installed on your computer. Everything runs inside your web browser using **Google Cloud Shell** (a pre-configured terminal built into the Google Cloud Console) and the **Google Cloud Console UI**.

#### Before You Start: Find Your 3 Settings in the Google Cloud Console

Open [https://console.cloud.google.com](https://console.cloud.google.com) in Chrome and write down these three values:

1. **`PROJECT_ID` (Your Google Cloud Project ID)**
   - Click the **Project Picker** dropdown at the top-left of the Google Cloud Console (next to the Google Cloud logo).
   - Copy the **ID** column (for example, `my-company-ge-prod`) — *use the ID, not the display name*.
2. **`GE_APP_ID` (Your Gemini Enterprise App ID)** and **`GE_LOCATION` (`global`, `us`, or `eu`)**
   - In the top search bar of the Google Cloud Console, search for **Gemini Enterprise** (or **AI Applications**) and open **Apps**.
   - Find the Gemini Enterprise app you want to monitor and copy its **ID** column (for example, `agent-platform-demo`) and its **Location** column (`global`, `us`, or `eu`).

---

#### Plan A: 5-Minute Browser-Only Deployment via Google Cloud Shell (Recommended)

You do **not** need to install anything on your laptop. Google Cloud Shell already has `gcloud`, `bq`, `python3`, and `unzip` pre-installed.

**Step 1 — Open Google Cloud Shell in your browser**
1. In [https://console.cloud.google.com](https://console.cloud.google.com), make sure your target project is selected in the top-left dropdown.
2. Click the **Activate Cloud Shell** icon (**`>_`**) in the top-right blue navigation bar (next to the search bar and notification bell).
3. A terminal panel will open at the bottom of your browser. If prompted, click **Continue** / **Authorize**.

**Step 2 — Get the code into Cloud Shell (pick ONE of the two options below)**
- **If you have the `.zip` file (`finops-mcp-zscaler.zip` or `vibe_lift_agent.zip`) on your computer:**
  1. In the top-right corner of the Cloud Shell panel, click the **three vertical dots (`⋮`)** → **Upload** → **Choose File**.
  2. Select the `.zip` file from your computer and click **Upload**.
  3. Paste this command into the Cloud Shell window and press **Enter**:
     ```bash
     rm -rf ~/vibelift_deploy && mkdir -p ~/vibelift_deploy
     unzip -q ~/*.zip -d ~/vibelift_deploy
     cd "$(dirname "$(find ~/vibelift_deploy -name deploy_cloud_run.sh | head -n 1)")/.."
     pwd
     ```
- **Or if you are pulling directly from GitHub:**
  ```bash
  rm -rf ~/vibelift_deploy
  git clone https://github.com/enriquekalven/vibe_lift_agent.git ~/vibelift_deploy
  cd ~/vibelift_deploy
  ```

**Step 3 — Fill in your 3 values and run the one-step installer**
1. Copy the block below into Notepad/TextEdit first, replace `YOUR_PROJECT_ID`, `YOUR_GE_APP_ID`, and `global` with the 3 values you wrote down, then paste the entire block into Cloud Shell and press **Enter** (if Cloud Shell pops up an **Authorize** prompt, click **Authorize**):

```bash
# === 1. EDIT THESE 3 VALUES ===
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GE_ENGINE_ID="YOUR_GE_APP_ID"
export GE_LOCATION="global"              # global, us, or eu (must match your GE App location)
export GOOGLE_CLOUD_REGION="us-central1" # Cloud Run region
export BQ_LOCATION="US"                  # BigQuery location (US or EU)

# === 2. DO NOT EDIT BELOW THIS LINE (COPY & PASTE AS-IS) ===
gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt -c constraints.txt

# Create BigQuery datasets, Cloud Logging sinks, and reporting mart views/tables
./deploy/setup_bigquery_sink.sh

# Build & deploy the private Cloud Run service, IAM roles, and register onto Gemini Enterprise
./deploy/deploy_cloud_run.sh

# Set up the hourly BigQuery scheduled query to keep the mart fresh
./deploy/setup_mart_refresh.sh
```

**Step 4 — Open and verify the dashboard in your browser (Zero local setup)**
You can open the dashboard in two ways without installing anything on your computer:
- **Inside Gemini Enterprise Chat:** Open your Gemini Enterprise web app and type:
  > `Open the dashboard`
  The dashboard will open in the right-hand side panel.
- **Directly in your browser via Cloud Shell Web Preview:**
  1. In Cloud Shell, run:
     ```bash
     gcloud run services proxy --project="${GOOGLE_CLOUD_PROJECT}" --region="${GOOGLE_CLOUD_REGION}" --port=8080 "$(gcloud run services list --project="${GOOGLE_CLOUD_PROJECT}" --region="${GOOGLE_CLOUD_REGION}" --format='value(metadata.name)' | head -n 1)"
     ```
  2. Click the **Web Preview** icon (the square with an eye / arrow at the top-right of the Cloud Shell bar) → **Preview on port 8080**.
  3. A new browser tab will open directly to the live dashboard.

---

#### Plan B: Step-by-Step Google Cloud Console UI Walkthrough (If Any Automated Step Is Blocked)

If an organization policy or permission blocks one of the automated scripts in Plan A, a non-technical administrator can complete or fix each stage directly in the **Google Cloud Console UI**:

1. **Enable Gemini Enterprise & Audit Logs in the Console UI**
   - Go to **Gemini Enterprise → Apps**, click your app, and turn on **User activity / observability logging**.
   - Go to **IAM & Admin → Audit Logs**, filter for **Discovery Engine API**, check **Admin Read**, **Data Read**, and **Data Write**, and click **Save**.
2. **Allow Custom MCP Connectors (If Blocked by Organization Policy)**
   - Go to **IAM & Admin → Organization Policies**.
   - Filter by **Disable custom MCP server connector for Gemini Enterprise**.
   - Click it → **Manage policy** → select **Override parent's policy** → **Add a rule** → set Enforcement to **Off** → click **Set policy**.
3. **Connect the MCP Server to Gemini Enterprise Using Only the Console UI**
   - First, get your Cloud Run URL in **Cloud Run** → click the service (`vibe-lift-agent` or `finops-mcp-agent`) → copy the **URL** at the top (ending in `.run.app`), and add `/mcp` to the end (for example, `https://vibe-lift-agent-123456789.us-central1.run.app/mcp`).
   - Go to **Gemini Enterprise → Data stores → Create data store**.
   - Search for **Custom MCP Server** and click **Add MCP server** → select **Bring your own MCP server URL** (do **not** select Agent Registry).
   - Paste your `https://...run.app/mcp` URL, select **No authentication**, click **Continue**, choose the same location as your app (`global`, `us`, or `eu`), name it `vibelift-analytics-mcp`, and click **Create**.
   - Wait 1–2 minutes until the data store status shows **Active**. Click into the data store → **Actions** tab → click **Reload custom actions** → check the box to select all **9 actions** → click **Enable actions**.
   - Go to **Gemini Enterprise → Apps → [Click Your App] → Data stores** → click **Connect existing data store**, check `vibelift-analytics-mcp`, and click **Save**.
4. **Grant Teammates Access to View the Standalone Dashboard in the Console UI**
   - Go to **Cloud Run** → click the checkbox next to `vibe-lift-agent` (or `finops-mcp-agent`) → click **Permissions** (top-right) → **Add principal**.
   - Enter your teammate's email (or Google Group), select role **Cloud Run → Cloud Run Invoker**, and click **Save**.

---

### Standard CLI Deployment Walkthrough

Run every command from the repo root. Replace `PROJECT_ID`, `REGION` and `GE_APP_ID` with your values.

```mermaid
flowchart LR
  P["0. Prerequisites"] --> L["1. Turn on GE + audit logging"]
  L --> B["2. setup_bigquery_sink.sh"]
  B --> D["3. deploy_cloud_run.sh"]
  D --> R1["4a. Deploy to GE Data Store (BYO_MCP)"]
  R1 --> R2["4b. Register GE Data Store to GE App Instance"]
  R2 --> V["5. Verify"]
  V --> S["6. Schedule mart refresh"]
```

### 0. Prerequisites

**Tools on your machine**
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) with `bq` (bundled), logged in: `gcloud auth login` and `gcloud auth application-default login`. In Argolis, log in both with your Argolis account (for example `admin@YOUR_LDAP.altostrat.com`) and make it active (`gcloud config set account ...`); your `@google.com` account usually has no access to Argolis projects.
- Python 3.12+ with the project dependencies (`deploy_cloud_run.sh` runs the test suite locally before deploying, and `setup_bigquery_sink.sh` runs `provision_ge_mart.py`):
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt -c constraints.txt
  pip install ruff   # optional; the deploy gate runs it when present
  ```
- `agents-cli` (optional, only if you also want `register_ge_agent.sh` to publish the standalone A2A agent).

**Google Cloud**
- A project with billing enabled, selected with `gcloud config set project PROJECT_ID`.
- An existing **Gemini Enterprise app** in that project (VibeLift observes and registers into it; it does not create one). Note its app ID and location (`global`, `us` or `eu`).
- **Your permissions.** Project **Owner** is the simplest. Otherwise you need roughly: Service Usage Admin, Service Account Admin, Role Administrator (for the custom `vibeLiftGeFleetReader` role), Project IAM Admin, Cloud Run Admin, Cloud Build Editor, Artifact Registry Admin, Storage Admin (source upload bucket), Service Account User on the runtime SA, Logs Configuration Writer, BigQuery Admin, Discovery Engine Admin, **Discovery Engine Editor** (to deploy the Custom MCP Server data store and register it onto the Gemini Enterprise App instance) and **Gemini Enterprise Admin** (optional A2A agent registration). This list has not been tested as a minimal set; if a step fails with `PERMISSION_DENIED`, the error names the missing permission.
- **Organization Policy Administrator** (`roles/orgpolicy.policyAdmin`) on the project, or someone who has it, to allow custom MCP servers (see below). In Argolis you normally administer your own organization and can grant it to yourself.

**OAuth client: not needed.** VibeLift does not need an OAuth client ID or consent screen:
- Gemini Enterprise calls the private Cloud Run service with a Google-signed ID token for its Discovery Engine service agent (`X-Serverless-Authorization` header), for both the A2A agent and the custom MCP server. `deploy_cloud_run.sh` grants that agent `roles/run.invoker` on the service. This only works with the default `*.run.app` URL, not a custom domain.
- The dashboard reads all telemetry with its own runtime service account, never with the end user's token.
- You and your teammates open the dashboard with `gcloud` credentials (proxy or identity token), not a browser OAuth flow.

An OAuth client is only needed if you change VibeLift to act *as the signed-in user*; see [Optional: end-user OAuth](#optional-end-user-oauth-not-used-by-vibelift).

**Argolis and other new organizations.** These org defaults break a naive deploy; the scripts handle the first one, the rest need you:

| Org default | Effect | What to do |
| :--- | :--- | :--- |
| `iam.automaticIamGrantsForDefaultServiceAccounts` (enforced in orgs created after May 3, 2024) | The Compute Engine default service account, which Cloud Build uses for `gcloud run deploy --source`, has no roles, so the build fails | Handled: `deploy_cloud_run.sh` grants it `roles/run.builder` (enabling the Compute Engine API first if the account does not exist) |
| Managed constraint *Disable custom MCP server connector for Gemini Enterprise* | The *Custom MCP Server* data store cannot be created | Turn it off for your project: *IAM & Admin > Organization Policies*, filter by that name, *Manage policy > Override parent's policy*, add a rule with enforcement **Off**, *Set policy*. Only if the project is VPC Service Controls-protected or listed in the data-connector policy's `enforcedProjects`: also add `custom_mcp` to *Restrict allowed data sources for data connectors* and your `vibe-lift-agent-PROJECT_NUMBER.REGION.run.app` host to *Restrict egress domains for data connectors*. ([docs](https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/override-constraint-for-custom-mcp-data-stores)) |
| Domain restricted sharing (`iam.allowedPolicyMemberDomains`) | IAM grants to accounts outside your org fail | Put only principals from your Argolis domain in `VIBELIFT_INVOKERS` (for example `user:admin@YOUR_LDAP.altostrat.com`), not `@google.com` accounts. |

> [!NOTE]
> These scripts were run end to end in `project-maui` (a `google.com` project). The Argolis rows above come from Google's documentation and have not yet been run in an Argolis project. If a step fails there, the error names the missing permission or policy.

### 1. Turn on Gemini Enterprise and audit logging (console, one time)

The scripts create sinks, but sinks only route logs that are actually written:

1. **Gemini Enterprise user activity logging.** In the Gemini Enterprise console, open each app you want to observe and turn on its user activity / observability logging (the exact label changes between console releases). This produces the `discoveryengine.googleapis.com/gemini_enterprise_user_activity` and `discoveryengine.googleapis.com/gen_ai.client.inference.operation.details` logs. Without them the mart has no turns, sessions or tokens.
2. **Data Access audit logs (optional).** *IAM & Admin > Audit Logs >* `Discovery Engine API` *>* enable *Data Read* and *Data Write*. Admin Activity audit logs are always on; Data Access logs add read/assist calls to `ds_ge_audit_raw` and improve user attribution when activity logs elide the email.

Check the logs exist (after some GE traffic):
```bash
gcloud logging logs list --project=PROJECT_ID | grep discoveryengine
```

### 2. BigQuery datasets, log sinks and reporting mart

```bash
BQ_LOCATION=US ./deploy/setup_bigquery_sink.sh
```

Idempotent. It creates:

| Layer | Objects |
| :--- | :--- |
| Raw datasets (`BQ_LOCATION`) | `ds_ge_assistant_raw`, `ds_ge_search_raw`, `ds_vertex_agents_raw`, `ds_ge_audit_raw`, `ds_security_guardrails_raw`, `vibelift_analytics` |
| Log sinks (partitioned tables) | `sink-ge-assistant-activity`, `sink-ge-search-activity`, `sink-vertex-reasoning-engine`, **`sink-ge-inference-tokens`** (the only source of per-turn tokens), `sink-platform-audit`, `sink-model-armor-sdp`, `vibelift-telemetry-sink`. Each sink's writer identity gets `roles/bigquery.dataEditor` on the project. |
| Raw log retention | Partition expiration of `VIBELIFT_RETENTION_DAYS` (default **90 days**) on the raw sink datasets, set on each dataset (new tables) and each existing table, by `deploy/set_log_retention.sh`. `vibelift_analytics` is not expired. |
| Analytics tables | `vibelift_analytics.agent_turns`, `agent_eval_runs`, `alpha_evolve_generations`, `agent_registry_snapshots`, view `vw_fleet_finops_summary` |
| Curated views + mart | `ds_ge_curated_staging` (`v_user_activity_curated`, `v_agentic_operations_curated`, `v_consolidated_audit_log`, `v_model_armor_curated`) and `vibelift_mart` (`v_fct_turns`, table `fct_turns`, `fct_sessions`, `agg_daily_usage`, `v_looker_l1_l2_support`), built by `deploy/bigquery/provision_ge_mart.py --apply --lookback-days=$VIBELIFT_RETENTION_DAYS` |

On a new project the raw tables do not exist until the first logs arrive; the curated views then return no rows instead of failing. The script exits non-zero if the mart step fails and prints the command to re-run just that step. Details of the mart logic: [docs/GE_MART.md](docs/GE_MART.md). For connecting Looker Studio to `vibelift_mart.v_looker_l1_l2_support`, see [docs/LOOKER_STUDIO_GUIDE.md](docs/LOOKER_STUDIO_GUIDE.md). To verify all 13 data conservation & reconciliation invariants across the raw, curated, and mart layers, run `python3 deploy/bigquery/verify_ge_mart_invariants.py --project=PROJECT_ID --gcloud-auth`.

To change retention later, run `VIBELIFT_RETENTION_DAYS=180 ./deploy/set_log_retention.sh` and rebuild the views with the same window: `python3 deploy/bigquery/provision_ge_mart.py --project=PROJECT_ID --gcloud-auth --apply --lookback-days=180`. Data that already expired cannot be recovered.

> [!NOTE]
> Sinks only capture log entries written **after** they exist. There is no backfill. After real Gemini Enterprise traffic, rebuild the mart table:
> `python3 deploy/bigquery/provision_ge_mart.py --project=PROJECT_ID --location=US --gcloud-auth --refresh`

> [!WARNING]
> The raw log datasets can contain prompt and response text from Gemini Enterprise activity logs. VibeLift reads only counters and identifiers, but restrict access to the `ds_*_raw` datasets as you would the logs themselves.

### 3. Deploy the Cloud Run service

```bash
# Optional extras:
#   VIBELIFT_INVOKERS="user:alice@example.com,group:finops@example.com"  -> can open the dashboard
#   VIBELIFT_BILLING_EXPORT_TABLE="billing-proj.billing_ds.gcp_billing_export_v1_XXXX"
#   VIBELIFT_GE_ENGINES="global/my-app"   (default: auto)
#   GE_ENGINE_ID="GE_APP_ID"              (optional: automatically runs step 4 after Cloud Run deploy)
GOOGLE_CLOUD_REGION=us-central1 ./deploy/deploy_cloud_run.sh
```

The script:
1. Runs bytecode compilation, `ruff` and the offline test suite, and aborts on failure (`VIBELIFT_SKIP_TESTS=1` overrides).
2. Enables `run`, `cloudbuild`, `artifactregistry`, `logging`, `monitoring`, `discoveryengine`, `aiplatform` and `bigquery` APIs.
3. Creates the runtime service account `vibe-lift-runtime-sa@PROJECT_ID.iam.gserviceaccount.com`.
4. Creates/updates the custom role `vibeLiftGeFleetReader` (`discoveryengine.engines.get`, `assistants.list`, `agents.list`, `agents.get`, `agents.manage`, `agents.update`) so `agents.list` returns agents created by any user, without agent admin rights. `agents.update` is used only by the dashboard's trace logging buttons (`observabilityConfig`).
5. Grants the runtime SA: `discoveryengine.viewer`, `vibeLiftGeFleetReader`, `aiplatform.viewer`, `aiplatform.user` (Gemini calls from the ADK agent and LLM judge), `logging.viewer`, `monitoring.viewer`, `cloudtrace.user`, `run.viewer`, `container.clusterViewer` (GKE discovery), `bigquery.dataViewer`, `bigquery.jobUser`, plus `WRITER` (Data Editor) on the `vibelift_mart` dataset's access list only (the dashboard's *Refresh Mart* button). If the mart does not exist yet, re-run the script after step 2.
6. Grants the Cloud Build service account (Compute Engine default SA) `roles/run.builder` if it lacks it, which orgs such as Argolis require for source deploys, and waits 60 s for it to propagate.
7. Deploys the private service (`--no-allow-unauthenticated`, 1 vCPU, 1 GiB, min 1 / max 10 instances) from source.
8. Creates the Discovery Engine service agent if needed and grants it `roles/run.invoker` (`service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`) so Gemini Enterprise can call `/mcp`, and to anyone in `VIBELIFT_INVOKERS`.

It is safe to re-run. Env vars are merged, so values set by hand on the service are kept.

**Cost data (optional).** Turn on the Cloud Billing export to BigQuery for your billing account (*Billing > Billing export > BigQuery export*), then grant the runtime SA read access on the export dataset and redeploy with `VIBELIFT_BILLING_EXPORT_TABLE` set:
In the console: *BigQuery > BILLING_DATASET > Sharing > Permissions > Add principal* `vibe-lift-runtime-sa@PROJECT_ID.iam.gserviceaccount.com` with **BigQuery Data Viewer**. (`bq add-iam-policy-binding --dataset` needs allowlisting in many projects, so the console or the dataset access list is the reliable path.)
Without it, token spend is estimated from list prices and billed cost shows as unknown.

**CI/CD alternative.** After the first `deploy_cloud_run.sh` run, [deploy/cloudbuild.yaml](deploy/cloudbuild.yaml) builds, tests, pushes and rolls out new images. Read its header first: the Cloud Build service account needs `roles/run.developer` and `roles/iam.serviceAccountUser` on the runtime SA.
```bash
gcloud builds submit --config=deploy/cloudbuild.yaml .
gcloud builds submit --config=deploy/cloudbuild.yaml --substitutions=_DEPLOY=false .   # build, test, push only
```

### 4. Deploy to Gemini Enterprise Data Store & Register to GE App Instance

> [!IMPORTANT]
> **Deploy to the Gemini Enterprise Data Store — NOT the Agent Registry / MCP Registry.**
> VibeLift's MCP server (`/mcp`) and embedded side-panel UI (`ui://vibelift-analytics/dashboard`) must be deployed as a **Gemini Enterprise Custom MCP Server Data Store** (`dataSource: "custom_mcp"`, `mcp_server_source: "BYO_MCP"`, `use_agent_gateway_egress: false` in **Console > Gemini Enterprise > Data stores**), **not** in Vertex AI / Cloud API Registry (*Agent Registry > MCP Registry*).
>
> **Two mandatory stages are required:**
> 1. **Stage 4a — Deploy to Gemini Enterprise Data Store (`BYO_MCP`):** Create the `Custom MCP Server` data store (`vibelift-analytics-mcp` / `vibelift-analytics-mcp_mcp_data`) pointing to `https://vibe-lift-agent-PROJECT_NUMBER.REGION.run.app/mcp` with `No authentication`, wait for it to become `ACTIVE`, import the 9 tools from `/mcp`, and enable them as actions on the data store.
> 2. **Stage 4b — Register the GE Data Store to the Gemini Enterprise App Instance (`GE_APP_ID`):** Once deployed to the GE Data Store, link/register `vibelift-analytics-mcp_mcp_data` onto your target **Gemini Enterprise App instance** (`engines/GE_APP_ID` -> `dataStoreIds`) so the assistant in that app instance can invoke VibeLift's MCP tools and open the right side-panel dashboard.

**Automated script (recommended — runs both Stage 4a and Stage 4b):**
```bash
GE_LOCATION=global ./deploy/register_ge_agent.sh GE_APP_ID
# Or call the GE Data Store + App linker directly:
GE_LOCATION=global python3 deploy/setup_mcp_connector.py GE_APP_ID            # add --dry-run to preview
```

What `./deploy/register_ge_agent.sh GE_APP_ID` does:
1. Checks the private Cloud Run service health with your identity token and ensures the Discovery Engine service agent (`service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`) holds `roles/run.invoker`.
2. **Stage 4a — Deploys to the Gemini Enterprise Data Store (`deploy/setup_mcp_connector.py`):** Creates the `Custom MCP Server` connector (`collectionId=vibelift-analytics-mcp`, `mcp_server_source="BYO_MCP"`, `use_agent_gateway_egress=false`, `auth_type="NO_AUTH"`) in Gemini Enterprise Data Stores ([docs](https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)), waits for `ACTIVE`, imports all 9 tools from `/mcp`, and enables all 9 as actions (`--tools` to choose). If a connector with that collection ID already exists but its `mcp_server_source` is not `BYO_MCP`, the script stops instead of reusing it.
3. **Stage 4b — Registers the GE Data Store to the Gemini Enterprise App Instance:** Renames the data store (`vibelift-analytics-mcp_mcp_data`) to `VibeLift Analytics`, links it into `engines/GE_APP_ID.dataStoreIds` while keeping the app instance's existing connected data stores, and verifies the registration with a follow-up `GET`.
4. **Optional A2A Agent Registration:** If `agents-cli` is installed, also registers the A2A agent card on the Gemini Enterprise App instance (`--registration-type a2a`, no OAuth).

Safe to re-run. Remove a connector with `python3 deploy/setup_mcp_connector.py --delete --collection-id=ID`. You need **Discovery Engine Editor** (`roles/discoveryengine.editor`). Some orgs also block custom MCP servers with an org policy (see step 0); in `project-maui` creation worked without an override.

> [!WARNING]
> `setup_mcp_connector.py` uses Discovery Engine v1alpha methods (`setUpDataConnectorV2`, `refreshDataConnectorTools`) that are not publicly documented and may change. If it fails, use the two-stage Console fallback steps below.

**Console (fallback):**

**Stage 4a (Console) — Deploy to Gemini Enterprise Data Store (NOT Agent Registry / MCP Registry):**
1. Go to **Console > Gemini Enterprise > Data stores > Create data store**, search **Custom MCP Server**, and click **Add MCP server** (choose **Bring your own MCP server URL** — do **not** go to *Agent Registry / MCP Registry* or select *From Agent Registry*).
2. Authentication: **No authentication**. The service stays private: for `*.run.app` URLs Gemini Enterprise sends a Google-signed ID token for its Discovery Engine service agent (`X-Serverless-Authorization`), which step 3 granted `roles/run.invoker`.
3. Fill in the values below, click **Continue**, pick the same multi-region as your app (`global`, `us` or `eu`), name it `vibelift-analytics-mcp` and click **Create**.
4. When the data store is **Active**: open it in **Gemini Enterprise > Data stores**, click **Actions > Reload custom actions**, select all 9 actions and click **Enable actions** (all actions start disabled).

**Stage 4b (Console) — Register the GE Data Store to the Gemini Enterprise App Instance:**
5. Go to **Console > Gemini Enterprise > Apps > `GE_APP_ID` > Data stores** (or *Connected data stores*).
6. Click **Connect existing data store** (or *Edit data stores*), select **`VibeLift Analytics`** (`vibelift-analytics-mcp` / `vibelift-analytics-mcp_mcp_data`), and click **Save** to register the data store to your Gemini Enterprise App instance ([Google docs](https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)).

| Field | Value |
| :--- | :--- |
| Target destination | **Gemini Enterprise > Data stores > Custom MCP Server (`BYO_MCP`)** — *not* Agent Registry / MCP Registry |
| Data store name / ID | `vibelift-analytics-mcp` (`vibelift-analytics-mcp_mcp_data`) |
| Registered GE App instance | `Console > Gemini Enterprise > Apps > GE_APP_ID > Data stores` (`engines/GE_APP_ID.dataStoreIds`) |
| MCP Server URL | `https://vibe-lift-agent-PROJECT_NUMBER.REGION.run.app/mcp` (the default Cloud Run URL; a custom domain does not receive the ID token) |
| Authentication | No authentication (no OAuth client) |
| Location | Same multi-region as your Gemini Enterprise app (`global`, `us` or `eu`) |
| Transport | Streamable HTTP (the only transport Gemini Enterprise supports), JSON-RPC 2.0, MCP `2025-06-18` (also accepts `2025-03-26`) |
| UI resource | `ui://vibelift-analytics/dashboard` (`text/html;profile=mcp-app`) |

Read-only tools declare `readOnlyHint`, so they run without a confirmation prompt. `run_alpha_evolve_generation` and `set_agent_trace_logging` change state, so Gemini Enterprise asks the user to confirm them.

Exposed MCP tools (9):
- `open_dashboard`: renders the VibeLift dashboard in the Gemini Enterprise right side panel (with Fullscreen and PDF export).
- `query_ge_agent_fleet`: live inventory and telemetry for all agents in Gemini Enterprise.
- `query_project_telemetry`: Cloud Run service metrics and BigQuery triage log summaries.
- `calculate_prompt_cache_economics`: prompt prefix cache hit rates and savings.
- `run_alpha_evolve_generation`: one closed-loop optimization generation (`alpha_evolve`, `opus_critic`, `hybrid_ensemble`).
- `get_vibelift_state`: active agent state, parameters and turn trajectory.
- `xray_prompt_cache`: character-level Prompt Cache X-Ray diff and cache-friendly rewrite.
- `list_prompt_snapshot_turns`: lists logged OpenTelemetry prompt snapshot turns for live X-Ray comparison.
- `set_agent_trace_logging`: enables or disables Cloud Trace / BigQuery logging on Gemini Enterprise agents.


#### Optional: end-user OAuth (not used by VibeLift)

Skip this unless you change VibeLift to call Google APIs *as the signed-in user*. Today it would add a consent prompt and change no numbers, because every metric is read with the runtime service account. If you do need it:
1. *APIs & Services > OAuth consent screen* (Google Auth Platform): if prompted, configure it with user type **Internal**.
2. *APIs & Services > Credentials > Create credentials > OAuth client ID*, application type **Web application**, with these authorized redirect URIs:
   - `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`
3. Download the JSON (client ID, client secret, auth URI, token URI). Enter these values in the A2A agent's OAuth step, or choose **OAuth 2.0** instead of *No authentication* on the MCP data store, together with the scopes your code needs.

Gemini Enterprise then sends the user's token in the `Authorization` header, and the service agent's ID token still goes in `X-Serverless-Authorization`. Your code has to read the user token. See [Register and manage A2A agents](https://cloud.google.com/gemini/enterprise/docs/register-and-manage-an-a2a-agent) and [Set up a custom MCP server](https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server).

### 5. Verify

```bash
URL=https://vibe-lift-agent-$(gcloud projects describe PROJECT_ID --format='value(projectNumber)').REGION.run.app
TOKEN=$(gcloud auth print-identity-token)

curl -s -H "Authorization: Bearer $TOKEN" $URL/health                        # {"status": "ok"}
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/ge_fleet?window_hours=24" | python3 -m json.tool | head -40
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/user_centric_finops?window_hours=168" | python3 -m json.tool | head -40
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' $URL/mcp | head -c 300
```

Check `errors` and `source_status` in `/api/ge_fleet`: every unavailable source is listed there with the reason (usually a missing role or API) instead of fake numbers.

**Open the dashboard.** The service is private, so browse through the Cloud Run proxy (uses your credentials):
```bash
gcloud run services proxy vibe-lift-agent --project=PROJECT_ID --region=REGION --port=8080
# then open http://localhost:8080
```
Teammates need `roles/run.invoker` on the service (`VIBELIFT_INVOKERS` in step 3) to do the same. In Gemini Enterprise, ask the assistant to "open the VibeLift dashboard".

### 6. Keep the mart fresh (hourly scheduled query)

`fct_turns` is a materialized snapshot, so it only changes when it is rebuilt. Create an hourly BigQuery scheduled query that rebuilds it (run after steps 2 and 3):
```bash
GOOGLE_CLOUD_PROJECT=PROJECT_ID ./deploy/setup_mart_refresh.sh
```
Idempotent: it updates the existing "VibeLift fct_turns refresh" query if there is one. It enables the BigQuery Data Transfer API, runs the query as the runtime service account (not your user, so it keeps working if you leave the project), grants the Data Transfer service agent `roles/iam.serviceAccountTokenCreator` on that account, and starts one run immediately. Check runs with `bq ls --transfer_run --max_results=5 CONFIG_NAME` (printed by the script) or in **BigQuery > Scheduled queries**.

Operators can also click **Refresh Mart** in the dashboard, or run `provision_ge_mart.py --refresh`.

### Executive PDF Export

Operators can filter by Gemini Enterprise app, standalone runtimes and time window (1 hour to 1 year), then click **Export PDF** in the dashboard action bar to get a print-optimized, multi-page report. Dedicated `@media print` CSS hides interactive controls and keeps KPI cards, charts and agent tables.

---


## REST API & MCP Endpoint Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` , `/ui` , `/app` | `GET` | Interactive VibeLift Analytics, FinOps & Optimization Dashboard |
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
| `/api/user_centric_finops` | `GET` | User-centric FinOps plus `session_drilldown`: per-user session token rollup (sessions, turns, input/output/cached/reasoning/total tokens) and a per-session turn-by-turn token dictionary from `vibelift_mart.fct_sessions` / `fct_turns` (`?window_hours=168`). Token counts only, no prompt text |
| `/api/select_optimizer` | `POST` | Switches active optimization platform (`platform_id`: `alpha_evolve`, `opus_critic`, `hybrid_ensemble`) |
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
---

## Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `403` from the service in a browser or `curl` | Service is private | Use `gcloud run services proxy` or an identity token; grant `roles/run.invoker` via `VIBELIFT_INVOKERS`. |
| Gemini Enterprise gets `403` from `/mcp` | Discovery Engine service agent missing or not an invoker | `gcloud beta services identity create --service=discoveryengine.googleapis.com --project=PROJECT_ID`, then re-run `deploy_cloud_run.sh` (or `register_ge_agent.sh`). |
| Fleet tab empty; error "No Gemini Enterprise apps found" | `auto` found no apps, or the SA lacks `discoveryengine.engines.list` | Check the app exists in `global`/`us`/`eu`; set `VIBELIFT_GE_ENGINES=location/app_id`; re-run the deploy script to re-apply roles. |
| Fleet shows only agents you created | Custom role missing `discoveryengine.agents.manage` | Re-run `deploy_cloud_run.sh` (it recreates/undeletes `vibeLiftGeFleetReader`). |
| Sessions/users tab empty | No GE activity logs, or no traffic since the sinks were created | Step 1 (logging on), generate GE traffic, then `provision_ge_mart.py --refresh`. |
| Sessions present but tokens `—`/empty | Inference log not routed to BigQuery | Re-run `setup_bigquery_sink.sh` (creates `sink-ge-inference-tokens`); tokens appear for new traffic after the next refresh. |
| `provision_ge_mart.py` shows a source as `WRONG_LOCATION` | Raw dataset is in a different BigQuery location than `--location` | Keep all datasets in one location (`BQ_LOCATION`), or pass the matching `--location`. |
| *Refresh Mart* button fails | Runtime SA lacks write access on `vibelift_mart` | Re-run `deploy_cloud_run.sh` after the mart exists (adds the runtime SA as `WRITER` on that dataset). |
| Judge/ADK agent errors mentioning `aiplatform.endpoints.predict` | Runtime SA lacks `roles/aiplatform.user` | Re-run `deploy_cloud_run.sh`. |
| Billed cost shows unknown | `VIBELIFT_BILLING_EXPORT_TABLE` unset or unreadable | See "Cost data" in step 3. |
| Source build fails with `PERMISSION_DENIED` (for example on `storage.objects.get` or Artifact Registry) | Cloud Build service account has no roles (`iam.automaticIamGrantsForDefaultServiceAccounts`, Argolis) | Re-run `deploy_cloud_run.sh` (grants `roles/run.builder` to `PROJECT_NUMBER-compute@developer.gserviceaccount.com`); wait 2 minutes if the grant was just made. |
| *Custom MCP Server* is missing or creation is blocked | Managed org constraint *Disable custom MCP server connector for Gemini Enterprise* | Override it for the project (step 0, Argolis table); you need `roles/orgpolicy.policyAdmin`. |
| MCP server was registered in *Agent Registry / MCP Registry*, or the GE Data Store exists but tools do not appear in Gemini Enterprise chat | VibeLift must be deployed as a **Gemini Enterprise Custom MCP Server Data Store** (`BYO_MCP`, not Agent Registry / MCP Registry) **and** registered (linked via `dataStoreIds`) onto the target Gemini Enterprise App instance (`GE_APP_ID`) | Run `GE_LOCATION=global ./deploy/register_ge_agent.sh GE_APP_ID` (which runs `deploy/setup_mcp_connector.py GE_APP_ID` to create the GE Data Store, enable all 6 actions, and link `vibelift-analytics-mcp_mcp_data` to `engines/GE_APP_ID`). |
| MCP data store stays in error, or actions do not load | Wrong URL (custom domain, missing `/mcp`), the service agent is not an invoker, or egress is restricted | Use `https://vibe-lift-agent-PROJECT_NUMBER.REGION.run.app/mcp`; re-run `register_ge_agent.sh`; if the project is VPC-SC protected, allow `custom_mcp` and the host in the data-connector policies. Then *Actions > Reload custom actions*. |
| `VIBELIFT_INVOKERS` grant fails with a domain / organization error | Domain restricted sharing | Use principals from your own org domain (in Argolis, `@YOUR_LDAP.altostrat.com`). |
| Deploy aborts before building | Local tests or `ruff` failed, or dependencies not installed | `pip install -r requirements.txt -c constraints.txt`, then fix the failure shown. |
| `IAM grant(s) failed` warning at the end of a script | Your account cannot set IAM policy | Ask a project Owner to re-run the script, or apply the listed grants. |

---

## Teardown

Removes everything the scripts created (irreversible for the BigQuery data):
```bash
PROJECT_ID=your-project; REGION=us-central1
gcloud run services delete vibe-lift-agent --project=$PROJECT_ID --region=$REGION
for s in sink-ge-assistant-activity sink-ge-search-activity sink-vertex-reasoning-engine sink-ge-inference-tokens \
         sink-platform-audit sink-model-armor-sdp vibelift-telemetry-sink; do
  gcloud logging sinks delete $s --project=$PROJECT_ID --quiet
done
for d in vibelift_mart ds_ge_curated_staging vibelift_analytics ds_ge_assistant_raw ds_ge_search_raw \
         ds_vertex_agents_raw ds_ge_audit_raw ds_security_guardrails_raw; do
  bq rm -r -f --dataset $PROJECT_ID:$d
done
gcloud iam roles delete vibeLiftGeFleetReader --project=$PROJECT_ID
gcloud iam service-accounts delete vibe-lift-runtime-sa@$PROJECT_ID.iam.gserviceaccount.com --project=$PROJECT_ID
```
Delete the scheduled query from step 6 (before deleting the service account):
```bash
bq ls --transfer_config --transfer_location=US --project_id=$PROJECT_ID   # find 'VibeLift fct_turns refresh'
bq rm -f --transfer_config projects/PROJECT_NUMBER/locations/us/transferConfigs/CONFIG_ID
```
Remove the VibeLift agent and MCP connector from the Gemini Enterprise app in the console.
