# VibeLift Platform Specification (`spec.md`)

## 1. Executive Overview & System Topology

**VibeLift** is an enterprise-grade analytics, prompt cache economics, and autonomous multi-objective optimization platform built for Google Cloud. It connects to live Google Cloud telemetry (Cloud Logging, Cloud Run, BigQuery) and the live **Gemini Enterprise agent fleet** (Discovery Engine inventory joined with Cloud Monitoring and Cloud Logging telemetry), serving as a **Bring-Your-Own Model Context Protocol (BYO MCP) App** in **Gemini Enterprise**.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   Gemini Enterprise                                    │
│  ┌───────────────────────────────┐                  ┌────────────────────────────────┐ │
│  │     Chat Agent & LLM Loop     │                  │  Sandboxed IFrame (BYO MCP)    │ │
│  │ (Calls open_dashboard, etc.)  │                  │  ui://vibelift-analytics/...   │ │
│  └──────────────┬────────────────┘                  └──────────────▲─────────────────┘ │
└─────────────────┼──────────────────────────────────────────────────┼───────────────────┘
                  │ Streamable HTTP JSON-RPC 2.0                     │ AppBridge postMessage
                  ▼                                                  ▼ Handshake (pip/full)
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        Google Cloud Run Service (`vibe-lift-agent`)                    │
│                                                                                        │
│  ┌────────────────────────┐  ┌─────────────────────────┐  ┌──────────────────────────┐ │
│  │    MCP Server Endpoint │  │    ADK Framework Layer  │  │   Interactive UI Server  │ │
│  │    (/mcp JSON-RPC 2.0) │  │    (app/agent.py root)  │  │   (/, /ui, /app, logo)   │ │
│  └───────────┬────────────┘  └───────────┬─────────────┘  └─────────────▲────────────┘ │
│              │                           │                              │              │
│              ▼                           ▼                              │              │
│  ┌──────────────────────────────────────────────────────────────────────┴────────────┐ │
│  │                            VibeLift Runtime Controller                            │ │
│  │                                                                                   │ │
│  │  ┌───────────────────────┐  ┌───────────────────────┐  ┌────────────────────────┐ │ │
│  │  │  Google Cloud Logging │  │  GE Agent Fleet       │  │  AlphaEvolve Optimizer │ │ │
│  │  │  & BigQuery Telemetry │  │  Live Telemetry Join  │  │  Multi-Objective Loop  │ │ │
│  │  └───────────────────────┘  └───────────────────────┘  └────────────────────────┘ │ │
│  └───────────────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Model Context Protocol (MCP) Interface Specification

### 2.1 Transport & Session Protocol
- **Transport**: Streamable HTTP (`application/json`)
- **Path**: `/mcp`
- **Supported Methods**:
  - `POST`: JSON-RPC 2.0 batch or single message payload.
  - `GET`: Returns HTTP 405 Method Not Allowed with header `Allow: POST, DELETE`.
  - `DELETE`: Explicit session termination returning HTTP 204 No Content.
- **Protocol Versions**: `2025-06-18` (default), `2025-03-26`.
- **Session Header**: `Mcp-Session-Id` (UUIDv4) issued during `initialize` and persisted across calls.

### 2.2 Core JSON-RPC Methods
1. `initialize`:
   - Returns protocol version, server info (`vibelift-analytics-mcp`), instructions for Gemini Enterprise (`open_dashboard` prioritized), and capabilities (`tools`, `resources`).
2. `ping`:
   - Heartbeat check returning empty object `{}`.
3. `tools/list`:
   - Returns registered tool definitions with metadata annotations:
     - `_meta.io.modelcontextprotocol/ui.resourceUri`: `ui://vibelift-analytics/dashboard`
     - `_meta.io.modelcontextprotocol/ui.visibility`: `['model', 'app']`
4. `resources/list`:
   - Returns available interactive UI resources:
     - `uri`: `ui://vibelift-analytics/dashboard`
     - `mimeType`: `text/html;profile=mcp-app`
     - `_meta.ui.csp`: Resource domain and connect domain whitelists.
5. `resources/read`:
   - Returns the embedded interactive HTML UI template with AppBridge postMessage client.
6. `tools/call`:
   - Executes registered tools and returns `content`, `structuredContent`, and `isError`.

### 2.3 MCP Tool Catalog
| Tool Name | Visibility | Widget | Description |
|---|---|---|---|
| `open_dashboard` | `model`, `app` | `True` | Gateway tool opening the interactive glassmorphic workspace in Gemini Enterprise. |
| `query_project_telemetry` | `model`, `app` | `False` | Fetches live Cloud Run agent services and BigQuery support logs. |
| `query_ge_agent_fleet` | `model`, `app` | `False` | Lists every agent on the Gemini Enterprise app with live Cloud Monitoring / Cloud Logging telemetry and model spend. |
| `calculate_prompt_cache_economics` | `model`, `app` | `False` | Computes token hit ratios and dollar savings vs naive pricing. |
| `run_alpha_evolve_generation` | `model`, `app` | `False` | Triggers Pareto genetic mutation on prompt prefixes and parameters. |
| `get_vibelift_state` | `model`, `app` | `False` | Retrieves active agent state, trajectory turns, and parameters. |

---

## 3. Gemini Enterprise UI Apps & AppBridge Integration

### 3.1 Content Security Policy (CSP)
Gemini Enterprise enforces strict sandboxing on interactive iframes. The VibeLift resource declares:
- **Resource Domains**: `https://cdn.jsdelivr.net`, `https://fonts.googleapis.com`, `https://fonts.gstatic.com`, `https://*.gstatic.com`
- **Connect Domains**: `https://*.run.app`, `https://*.googleapis.com`, `https://*.cloud.google.com`

### 3.2 AppBridge PostMessage Protocol
The client embedded in `ui_template.py` implements the standard postMessage protocol:
1. **Host Discovery & Notification**:
   - `ui/notifications/initialized` / `notifications/initialized`: Sent on page load.
2. **Handshake**:
   - `callHost('ui/initialize', ...)` with fallback to `initialize`.
3. **Display Modes**:
   - `callHost('ui/request-display-mode', { mode: 'fullscreen' | 'pip' })`.
   - Listens for `ui/notifications/host-context-changed` to synchronize UI controls.
4. **Tool Result Reception**:
   - Listens for `ui/notifications/tool-result` to dynamically re-render metrics and charts without full reloads.

---

## 4. Google Cloud Agent Development Kit (ADK) Architecture

Defined in `app/agent.py`:
- **Model**: `gemini-2.5-flash`
- **Root Agent**: `root_agent` equipped with native ADK tool bindings:
  - `open_dashboard`
  - `query_gcp_telemetry`
  - `query_ge_agent_fleet`
  - `list_cloud_run_agents`
  - `calculate_cache_economics`
  - `detect_prompt_breakpoint`
  - `trigger_alpha_evolve_cycle`
- **CLI Configuration**: Fully compatible with `agents-cli` manifest (`agents-cli-manifest.yaml`).

---

## 5. Gemini Enterprise Agent Fleet & Telemetry Engine (`ge_fleet.py`)

- **Inventory**: Discovery Engine API `v1alpha` engines → assistants → agents for each app in `VIBELIFT_GE_ENGINES` (default `agent-platform-demo`), paginated.
- **ADK agents (Vertex AI Agent Engine)**: Cloud Monitoring `aiplatform.googleapis.com/reasoning_engine/{request_count,request_latencies,cpu/allocation_time,memory/allocation_time}`; per-agent LLM calls, tokens, conversations, and last activity from OpenTelemetry `gen_ai.*` labels in Cloud Logging (message content is never copied into the payload).
- **A2A agents (Cloud Run)**: `run.googleapis.com/{request_count,request_latencies,container/billable_instance_time}` for the service in the agent card URL (service-level).
- **Project-wide**: Vertex AI `publisher/online_serving/{token_count,model_invocation_count}` priced with `telemetry.RATE_CARDS` (list prices), and Gemini Enterprise `AssistantService.StreamAssist` call counts.
- **Auth**: Application Default Credentials (the Cloud Run runtime service account in production).
- **Semantics**: no synthesized values; unavailable sources return `null` plus `errors` / `source_status`. 60 s TTL cache per window; forced refreshes limited to one per 10 s.

---

## 6. Multi-Objective AlphaEvolve Optimizer

- **Multi-Objective Pareto Frontiers**:
  - Target 1: Prompt Cache Hit Ratio (% &uarr;)
  - Target 2: Cost per 1k Turns ($ &darr;)
  - Target 3: Accuracy / Hallucination-Free Rate (% &uarr;)
  - Target 4: P95 End-to-End Latency (ms &darr;)
- **Mutations Supported**:
  - `STATIC_PREFIX_REORDER`: Moves invariant system prompt blocks to the front.
  - `DYNAMIC_SCHEMA_PRUNING`: Strips redundant tool definitions.
  - `GUARDRAIL_ROLLBACK`: Reverts candidate mutations that degrade safety or accuracy below user-configured thresholds.

---

## 7. Cloud Run IAM, Service Account & Telemetry Ingestion Pipeline

### 7.1 Runtime Identity & Least-Privilege IAM Roles
VibeLift executes under a dedicated Google Cloud Service Account:
- **Service Account**: `vibe-lift-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com`
- **Assigned IAM Roles**:
  - `roles/discoveryengine.viewer`: Reads the Gemini Enterprise app, its assistants, and agent details.
  - `projects/${PROJECT_ID}/roles/vibeLiftGeFleetReader` (custom role, created by `deploy_cloud_run.sh`): `discoveryengine.engines.get`, `discoveryengine.assistants.list`, `discoveryengine.agents.list`, `discoveryengine.agents.get` and `discoveryengine.agents.manage`. The agents list API returns only agents created by the caller unless the caller holds `discoveryengine.agents.manage`, which the viewer role lacks. This role adds that one permission without the create, update, delete or setIamPolicy permissions that `roles/discoveryengine.agentAdmin` would grant.
  - `roles/aiplatform.viewer`: Reads Vertex AI Agent Engine (reasoning engine) metadata for ADK agents.
  - `roles/logging.viewer`: Allows querying Google Cloud Logging for live agent request and response turns (`protoPayload.response.usageMetadata`).
  - `roles/monitoring.viewer`: Reads Cloud Monitoring metrics (latency, invocations, CPU/memory).
  - `roles/run.viewer`: Inspects deployed Cloud Run agent microservices across the region.
  - `roles/bigquery.dataViewer` & `roles/bigquery.jobUser`: Queries Gemini Enterprise support triage views and audit logs.

### 7.2 Telemetry Ingestion Pipes
1. **Metadata Server & ADC**: Cloud Run automatically injects OAuth2 Bearer tokens for `vibe-lift-runtime-sa` via the internal metadata server (`http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token`).
2. **Fleet Pipeline**: `GeminiEnterpriseFleetService` lists agents through the Discovery Engine API and joins each one with Cloud Monitoring and Cloud Logging telemetry (see Section 5).
3. **Log Aggregation Pipeline**: `GoogleCloudTelemetryService` filters Cloud Logging for logName `cloudaudit.googleapis.com` and `run.googleapis.com/stdout` with structured `usageMetadata` (`promptTokenCount`, `cachedContentTokenCount`, `candidatesTokenCount`).
4. **Dashboard Push / AppBridge**: Aggregated metrics are synchronized into `VibeLiftRuntimeController` and streamed via `/api/state`, `/mcp` JSON-RPC tools, and the glassmorphic HTML UI.

### 7.3 Access Control & Integration Surfaces
- **Ingress auth**: The service is deployed with `--no-allow-unauthenticated`. Every route (dashboard, `/api/*`, `/mcp`, ADK) requires a caller with `roles/run.invoker`.
- **Gemini Enterprise → MCP**: The custom MCP connector (`auth_type: NO_AUTH`, BYO MCP) reaches the private `/mcp` endpoint through the Discovery Engine service agent `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`, which `deploy_cloud_run.sh` grants `roles/run.invoker` on the service. MCP sessions are stateless (the `Mcp-Session-Id` is only echoed), so requests can land on any instance.
- **ADK API server**: `/run` and `/run_sse` call `gemini-2.5-flash` on Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, location `GOOGLE_CLOUD_LOCATION`) as `vibe-lift-runtime-sa`. That account holds viewer roles only; model calls additionally need `aiplatform.endpoints.predict` (for example `roles/aiplatform.user`).
- **A2A**: Only the agent card is published. Its `url` is `PUBLIC_A2A_URL`, else `VIBELIFT_PUBLIC_URL` + `/a2a/app`, else the request's host (from `Host` and `X-Forwarded-Proto`). No A2A JSON-RPC handler exists, so the card must not be registered as a working A2A agent.
- **Error hygiene**: Tool and JSON-RPC failures return the exception type plus a 12-character reference. The full exception is logged server-side with the same `ref=`.
