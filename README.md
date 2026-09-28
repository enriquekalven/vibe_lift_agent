# VibeLift — Gemini Enterprise Agent Fleet Observability, FinOps & AlphaEvolve Studio

VibeLift is an enterprise-grade analytics, prompt cache economics, and autonomous multi-objective optimization platform built on Google Cloud with the **Google Agent Development Kit (ADK)** and `agents-cli`.

Designed for production deployment on **Google Cloud Run**, VibeLift continuously monitors live agent fleets, ingests Google Cloud Logging and BigQuery telemetry from Gemini Enterprise applications, enumerates every agent deployed on the Gemini Enterprise app and joins it with real Cloud Monitoring and Cloud Logging telemetry, and executes closed-loop AlphaEvolve optimization.

---

## Key Capabilities

1. **Google Agent Development Kit (ADK) Runtime**:
   - Built around the ADK standard (`google-adk`, `google-genai`).
   - Defined in [`app/agent.py`](app/agent.py) with `root_agent` using `gemini-2.5-flash` and automated function calling tools:
     - `open_dashboard`: Opens the interactive 3-Tab VibeLift dashboard in Gemini Enterprise.
     - `query_gcp_telemetry`: Ingests live telemetry from Google Cloud Logging and Cloud Run.
     - `query_ge_agent_fleet`: Lists every agent on the Gemini Enterprise app with live, aggregated telemetry.
     - `list_cloud_run_agents`: Discovers active AI agents in the project.
     - `calculate_cache_economics`: Computes token hit ratios and dollar savings vs naive rate cards.
     - `detect_prompt_breakpoint`: Identifies exact prompt mutations breaking prefix cache hits.
     - `trigger_alpha_evolve_cycle`: Drives automated evolutionary optimization iterations.

2. **Live Gemini Enterprise Agent Fleet** ([`ge_fleet.py`](ge_fleet.py)):
   - Enumerates every agent on the configured Gemini Enterprise app(s) (`VIBELIFT_GE_ENGINES`, default `agent-platform-demo`) through the Discovery Engine API, across all assistants and pages.
   - ADK agents on Vertex AI Agent Engine: Cloud Monitoring `reasoning_engine` metrics (requests, 4xx/5xx, p50/p95 latency, vCPU and memory) plus per-agent LLM calls, input/output tokens, conversations, and last activity from the agent's OpenTelemetry `gen_ai` log events (message content is never read into the payload).
   - A2A agents on Cloud Run: service-level `run.googleapis.com` metrics for the service named in the agent card URL. Google-managed agents are listed as inventory only.
   - Project-wide Vertex AI model token usage and invocations (`publisher/online_serving/*`) priced at list rates, plus Gemini Enterprise `StreamAssist` call volume.
   - Nothing is synthesized: an unavailable source yields `null` values and an entry in `errors` / `source_status`. Results are cached for 60 s (`VIBELIFT_FLEET_TTL_SECONDS`); forced refreshes are limited to one per 10 s.

3. **Gemini Enterprise & Cloud Logging Telemetry**:
   - Reports on VibeLift's own Cloud Run service plus any services you opt in via `VIBELIFT_MONITORED_SERVICES` (comma-separated), resolved through the Cloud Run Admin API.
   - Integrates with Gemini Enterprise BigQuery views (`gemini_enterprise_support_views.vw_l1_l2_unified_triage_logs`) and Cloud Logging audit streams.

4. **AlphaEvolve Studio & Prompt Cache Economics**:
   - Interactive **3-Tab Dashboard** powered by FastAPI and Uvicorn:
     - **Tab 0 — Gemini Enterprise Agent Fleet**: Real-time inventory, requests, error rates, latency percentiles, LLM calls, token usage, and model spend across every deployed agent.
     - **Tab 1 — Overview & Prompt Cache Economics**: Turn-by-turn prefix cache hit ratio, breakpoint diagnostics, and multi-objective parameter configuration.
     - **Tab 2 — AlphaEvolve Studio**: Closed-loop Pareto evolutionary optimization, anomaly injection, and guardrail rollback verification.
   - Multi-agent optimization profiles synchronized with Gemini Enterprise (`IT Service Desk` on Vertex AI Agent Engine, `VibeLift Analytics & FinOps` on Cloud Run A2A/MCP, and `Deep Research` Google-managed research agent).

5. **Gemini Enterprise BYO MCP App Integration**:
   - Implements a streamable HTTP JSON-RPC 2.0 MCP server at `/mcp` compliant with protocol versions `2025-06-18` and `2025-03-26`.
   - Publishes the interactive MCP UI App resource `ui://vibelift-analytics/dashboard` (`text/html;profile=mcp-app`) with full Content Security Policy (CSP) headers.
   - Built-in AppBridge supporting bidirectional postMessage communication (`ui/initialize`, `ui/notifications/initialized`, `ui/request-display-mode` for side-panel and fullscreen toggling).
   - MCP Tool taxonomy with widget metadata:
     - `open_dashboard`: Opens the full glassmorphic interactive workspace directly in Gemini Enterprise.
     - `query_project_telemetry`: Live Cloud Run and Gemini Enterprise support telemetry.
     - `query_ge_agent_fleet`: Every agent on the Gemini Enterprise app with live requests, errors, latency, tokens, and model spend.
     - `calculate_prompt_cache_economics`: Real-time token economics and dollar savings.
     - `run_alpha_evolve_generation`: AlphaEvolve multi-objective mutation cycle.
     - `get_vibelift_state`: Full active agent state and trajectory.

---

## Project Structure

```
vibe_lift_agent/
├── app/                              # ADK Framework Application
│   ├── __init__.py
│   ├── agent.py                      # ADK root_agent and tool definitions
│   └── fast_api_app.py               # Production FastAPI ASGI entrypoint
├── agents-cli-manifest.yaml          # Google Agents CLI deployment manifest
├── ge_fleet.py                       # Live Gemini Enterprise agent fleet: inventory + real telemetry
├── ge_fleet_test.py                  # Unit tests for Gemini Enterprise fleet inventory & telemetry
├── gcp_telemetry.py                  # Google Cloud Logging & BigQuery telemetry service
├── telemetry.py                      # Turn usage schema, rate cards & cache economics
├── alpha_evolve_optimizer.py         # Multi-objective AlphaEvolve optimization engine
├── long_running_agent.py             # ADK multi-turn simulation runtime
├── mcp_server.py                     # Streamable HTTP MCP server & BYO MCP App bridge
├── server.py                         # Dual-mode server (FastAPI ASGI + HTTP server)
├── ui_template.py                    # 3-Tab Glassmorphic Web UI dashboard & AppBridge
├── logo_asset.py                     # Embedded Google Cloud styled logo asset
├── vibelift_mcp_spec_clean.json      # Clean MCP tool catalog specification
├── SKILL.md                          # Agent skill definition & operating workflows
├── spec.md                           # Architecture & protocol specification
├── Dockerfile                        # Production Cloud Run container (Python 3.12-slim)
├── cloud_run_service.yaml            # Knative Cloud Run service specification
├── cloudbuild.yaml                   # Google Cloud Build automated deployment pipeline
├── deploy_cloud_run.sh               # Executable Cloud Run deployment script
├── setup_bigquery_sink.sh            # BigQuery dataset, tables, views & Logging sink setup
├── pyproject.toml                    # Standard Python packaging configuration (same pins as requirements.txt)
├── requirements.txt                  # Production dependencies, pinned to the versions verified in the image
├── constraints.txt                   # Full transitive dependency lock captured from the production image
├── vibe_lift_test.py                 # Comprehensive unit and integration test suite
└── README.md                         # Platform documentation and operations guide
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Metadata server, then active `gcloud` project | Target Google Cloud project ID. If none is found, VibeLift uses the invalid placeholder `UNCONFIGURED-PROJECT` and logs a warning, so it never queries someone else's project |
| `GOOGLE_CLOUD_REGION` | `us-central1` | Cloud Run and Vertex AI region |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` when no `GOOGLE_API_KEY`/`GEMINI_API_KEY` is set | Routes the ADK agent's Gemini calls through Vertex AI with the runtime service account |
| `GOOGLE_CLOUD_LOCATION` | `GOOGLE_CLOUD_REGION` | Vertex AI location for the ADK agent's model calls |
| `VIBELIFT_GE_ENGINES` | `agent-platform-demo` | Comma-separated Gemini Enterprise engine ID(s) to inventory |
| `VIBELIFT_GE_LOCATION` | `global` | Discovery Engine location (`global`, `us`, `eu`) |
| `VIBELIFT_GE_COLLECTION` | `default_collection` | Discovery Engine collection ID |
| `VIBELIFT_FLEET_TTL_SECONDS` | `60` | Per-window in-memory cache TTL in seconds |
| `VIBELIFT_FLEET_MIN_REFRESH_SECONDS` | `10` | Minimum interval between forced fleet refreshes |
| `VIBELIFT_FLEET_WINDOW_HOURS` | `24` | Default telemetry window for the fleet view |
| `VIBELIFT_TOKEN_LOG_MAX_ENTRIES` | `3000` | Maximum `gen_ai` log entries read per fleet collection |
| `VIBELIFT_RATE_CARDS_JSON` | *(empty)* | Optional JSON override for the model list-price rate cards |
| `VIBELIFT_MONITORED_SERVICES` | *(empty)* | Optional comma-separated Cloud Run services to include in `/api/gcp_telemetry` |
| `VIBELIFT_PUBLIC_URL` | *(unset)* | Base URL published in the A2A agent card and returned by the ADK `open_dashboard` tool. When unset, the card uses the request's host and `open_dashboard` returns only the MCP resource URI. The deploy scripts set it to `https://SERVICE-PROJECT_NUMBER.REGION.run.app` |
| `PUBLIC_A2A_URL` | `VIBELIFT_PUBLIC_URL` + `/a2a/app` | Overrides the `url` field of the A2A agent card |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS allowlist for the ADK and VibeLift routes |
| `ENABLE_MCP_APP` | `1` | `0`, `false`, `no` or `off` removes the `/mcp` JSON-RPC endpoint |
| `MCP_PROTOCOL_VERSION` | `2025-06-18` | Version offered when a client requests an unsupported one. Only `2025-06-18` and `2025-03-26` are accepted; other values are ignored |

---

## BigQuery Analytics & Telemetry Sink Setup

To provision the high-performance BigQuery analytics sink:

```bash
./setup_bigquery_sink.sh
```
This automatically:
1. Creates the BigQuery dataset `vibelift_analytics` in your active GCP region.
2. Creates partitioned & clustered tables:
   - `agent_turns`: Granular turn-by-turn prompt & cached token logs.
   - `agent_eval_runs`: Accuracy and safety guardrail verification scores.
   - `alpha_evolve_generations`: Mutation decisions, prompt diffs, and health status.
   - `agent_registry_snapshots`: Fleet-wide spend vs naive spend snapshots.
3. Deploys the pre-aggregated SQL view `vw_fleet_finops_summary` for sub-second dashboard rendering.
4. Configures a Google Cloud Logging sink (`vibelift-telemetry-sink`) that automatically routes all Cloud Run agent turns into BigQuery.

---

## Quickstart: Local Development

### 1. Install Dependencies
Use Python 3.12, the version in the production image. The constraints file locks every transitive
dependency to the versions verified in production:
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
```
To upgrade a dependency, bump its pin in `requirements.txt` and `pyproject.toml`, rebuild, run the tests,
then regenerate `constraints.txt` from the new image with `pip freeze --exclude pip --exclude setuptools --exclude wheel`.

### 2. Run the Unit & Integration Test Suites
```bash
# Offline and deterministic (what Cloud Build runs): no live Google Cloud API calls.
GOOGLE_APPLICATION_CREDENTIALS=/nonexistent GOOGLE_CLOUD_PROJECT=test-project \
  python -m unittest vibe_lift_test ge_fleet_test
```
Without those variables, the telemetry tests also call live Google Cloud APIs with your
Application Default Credentials. That run is slower (minutes) but exercises the real clients.

### 3. Launch the Local Dashboard
```bash
python server.py --port=8080
```
Open [http://localhost:8080](http://localhost:8080) in your browser.

---

## Gemini Enterprise BYO MCP App Setup

To surface VibeLift in Gemini Enterprise as an interactive Bring-Your-Own (BYO) MCP App:

1. **Deploy to Cloud Run**:
   ```bash
   ./deploy_cloud_run.sh
   ```
   This provisions the dedicated runtime service account (`vibe-lift-runtime-sa`), grants least-privilege viewer roles plus the custom `vibeLiftGeFleetReader` role (required so Discovery Engine `agents.list` returns all agents on the app rather than only caller-created agents), and deploys `vibe-lift-agent` with private IAM authentication (`--no-allow-unauthenticated`). Because the service is private, the script also grants `roles/run.invoker` on the service to the Discovery Engine service agent (`service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`), the identity that carries Gemini Enterprise's calls to `/mcp` on the verified deployment.
2. **Register in Gemini Enterprise as a custom MCP data store** (connector `vibelift-analytics-mcp`, data store "VibeLift Analytics"):
   - **MCP Server URL**: `https://vibe-lift-agent-PROJECT_NUMBER.REGION.run.app/mcp` (printed by the deploy script)
   - **Transport**: Streamable HTTP (`POST`, `GET` 405, `DELETE` 204)
   - **UI Resource**: `ui://vibelift-analytics/dashboard` (`text/html;profile=mcp-app`)
   - Add the data store to the Gemini Enterprise app (engine `dataStoreIds`) so it appears as a chat source.
   - After changing tool names, descriptions or schemas, call `dataConnector:refreshDataConnectorTools` so Gemini Enterprise picks them up.
3. **Invoke in Chat**:
   - In a new chat, select the **VibeLift Analytics** source, then ask: *"Can you open the VibeLift Analytics dashboard for me?"*
   - Gemini calls `open_dashboard`, which opens the dashboard on the live Gemini Enterprise agent fleet tab in the side panel or full screen.

---

## Google Cloud Agents CLI (`agents-cli`)

VibeLift is configured with [`agents-cli-manifest.yaml`](agents-cli-manifest.yaml).

### Inspect Agent Metadata
```bash
agents-cli info
```

### Validate Agent Functionality
```bash
agents-cli eval run
```

---

## Deployment to Google Cloud Run

Deploy VibeLift directly to your Google Cloud project using the deployment script or Cloud Build.

### Option A: Direct Deployment Script
```bash
./deploy_cloud_run.sh
```
This builds and deploys the container to Cloud Run with:
- Service name: `vibe-lift-agent` (override with `SERVICE_NAME`)
- Region: `us-central1` (override with `GOOGLE_CLOUD_REGION`)
- Port: `8080` (or injected `$PORT`), 1 vCPU, 1 GiB, concurrency 80, timeout 300 s, 0 to 10 instances, startup CPU boost
- Runtime Service Account: `vibe-lift-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com`
- Authentication: Private Cloud Run IAM (`--no-allow-unauthenticated`), plus `roles/run.invoker` for the Discovery Engine service agent so Gemini Enterprise can reach `/mcp`
- `VIBELIFT_PUBLIC_URL` set to the deterministic `https://SERVICE-PROJECT_NUMBER.REGION.run.app` URL, and Vertex AI as the ADK model backend

The script is safe to re-run. Env vars are merged with `--update-env-vars`, so variables you set on the
service by hand (for example `VIBELIFT_MONITORED_SERVICES`) survive a redeploy. Failed IAM grants are
reported as warnings instead of being silently ignored.

### Option B: Cloud Build Automated CI/CD
```bash
gcloud builds submit --config=cloudbuild.yaml .                                # build, test, push, deploy
gcloud builds submit --config=cloudbuild.yaml --substitutions=_DEPLOY=false .   # build, test, push only
```
The pipeline builds the image, runs the full test suite inside it (offline), pushes it with the
immutable `$BUILD_ID` tag, and deploys that exact image. Prerequisites:
- Run `./deploy_cloud_run.sh` once first. It creates the runtime service account, its roles and the invoker binding.
- The deploy step runs as your Cloud Build service account, which cannot deploy Cloud Run by default.
  Grant it `roles/run.developer` on the project and `roles/iam.serviceAccountUser` on
  `vibe-lift-runtime-sa`. The exact commands are in the header of [`cloudbuild.yaml`](cloudbuild.yaml).
  Without these grants, use `_DEPLOY=false` and deploy the pushed image with `gcloud run deploy --image`.

### Option C: Knative Service Spec (fresh services only)
[`cloud_run_service.yaml`](cloud_run_service.yaml) is a template with `__PROJECT_ID__`,
`__PROJECT_NUMBER__`, `__REGION__` and `__IMAGE_URI__` placeholders. Its header shows the `sed` command
to fill them in.

> [!WARNING]
> `gcloud run services replace` swaps in the entire spec. Anything not in the file, such as custom
> audiences or env vars added later, is removed from the live service. To change an existing service
> declaratively, start from `gcloud run services describe vibe-lift-agent --region=us-central1 --format=export`.

### Verify and Roll Back
```bash
# The service is private: call it with an identity token.
URL=$(gcloud run services describe vibe-lift-agent --region=us-central1 --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"

# Roll back by shifting all traffic to a previous revision.
gcloud run revisions list --service=vibe-lift-agent --region=us-central1
gcloud run services update-traffic vibe-lift-agent --region=us-central1 --to-revisions=REVISION_NAME=100
```

---

## Security & Operations Notes

- **Private by default.** Only principals with `roles/run.invoker` on the service can call it, including
  the dashboard, `/api/*` and `/mcp`. Review the list with
  `gcloud run services get-iam-policy vibe-lift-agent --region=us-central1`. Every invoker can read the
  project telemetry the runtime service account can see.
- **ADK model access.** The ADK API server (`/run`, `/run_sse`) calls Gemini on Vertex AI as the runtime
  service account. The deploy script grants only viewer roles, so model calls fail with a permission
  error until you grant `roles/aiplatform.user` (or a custom role with `aiplatform.endpoints.predict`)
  to `vibe-lift-runtime-sa`. The dashboard and the MCP App do not need it.
- **A2A.** Only the agent card is served (`/.well-known/agent-card.json`, `/a2a/app/.well-known/agent-card.json`).
  There is no A2A JSON-RPC (`message/send`) handler, so registering VibeLift as an A2A agent in Gemini
  Enterprise does not work. Use the MCP data store integration above.
- **State is per instance.** The dashboard's optimization state lives in memory. It resets on cold
  start and is not shared between instances when Cloud Run scales out.
- **Health checks.** Cloud Run's front end reserves `/healthz` on the public URL, so use `/health`
  (ADK) externally. `/healthz` still works for in-container probes and local runs.
- **Errors.** MCP tool and JSON-RPC errors return only the exception type and a reference ID. The full
  error is in Cloud Logging under the same `ref=` value.

---

## API & MCP Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Interactive 3-Tab VibeLift Analytics & FinOps Dashboard |
| `/ui` / `/app` | `GET` | Direct aliases for the interactive UI workspace |
| `/.well-known/agent-card.json` | `GET` | A2A protocol agent card (also served at `/a2a/app/.well-known/agent-card.json`). Card only: no A2A JSON-RPC handler |
| `/mcp` | `POST` | Streamable JSON-RPC 2.0 MCP endpoint (`initialize`, `tools/*`, `resources/*`) |
| `/mcp` | `DELETE` | MCP session termination (returns HTTP 204) |
| `/health` | `GET` | ADK API server health check. Use this on the Cloud Run URL |
| `/healthz` | `GET` | VibeLift health check for local runs and in-container probes (reserved by Cloud Run's front end on the public URL) |
| `/list-apps`, `/run`, `/run_sse` | `GET` / `POST` | ADK API server for the `app` agent. Model calls need Vertex AI permission (see Security & Operations Notes) |
| `/api/state` | `GET` | Complete active agent runtime state, turns, and summary |
| `/api/gcp_telemetry` | `GET` | Live Google Cloud project services and telemetry summary |
| `/api/sync_gcp_telemetry` | `POST` | Triggers a live sync from Google Cloud Logging into turns |
| `/api/ge_fleet` | `GET` | Live Gemini Enterprise agent fleet (`window_hours`, `force_refresh`) |
| `/api/sync_ge_fleet` | `POST` | Forces a fresh fleet collection (at most one per 10 s) |
| `/api/select_agent` | `POST` | Switches active optimization agent profile |
| `/api/add_parameter` | `POST` | Adds user-defined multi-objective metric parameter |
| `/api/inject_anomaly` | `POST` | Injects an anomaly to trigger optimization degradation |
| `/api/evolve_generation` | `POST` | Runs the next AlphaEvolve optimization generation |
| `/api/reset` | `POST` | Resets the active agent state to baseline |