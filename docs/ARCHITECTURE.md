# FinOps Dashboard architecture

This page describes what the code in this repository does today. Every box in the diagram names the file or Google Cloud resource behind it. The legend says which parts read live data, which part is a simulator, and which pieces are not configured.

## Diagram

```mermaid
flowchart LR
  subgraph GE["Gemini Enterprise app (Discovery Engine)"]
    direction TB
    Chat["Assistant chat"]
    DS["Custom MCP Server data store<br/>finops-analytics-mcp_mcp_data<br/>deploy/setup_mcp_connector.py"]
    Panel["MCP App side panel<br/>sandboxed iframe + AppBridge"]
  end

  Browser["Operator browser / IAP & Cloud Armor WAF<br/>finops-tokenomics-dashboard (surface=dashboard)<br/>deploy/setup_enterprise_perimeter.sh"]

  subgraph RUN["Cloud Run services: finops-dashboard-mcp-app (MCP) & finops-tokenomics-dashboard (IAP)"]
    direction TB
    ASGI["app/fast_api_app.py<br/>ASGI app served by uvicorn"]
    MCP["finops_dashboard/mcp_server.py<br/>/mcp JSON-RPC 2.0, 9 tools<br/>ui://finops-analytics/dashboard"]
    REST["finops_dashboard/server.py<br/>/api/* + /a2a/app + dashboard HTML<br/>finops_dashboard/ui/template.py"]
    ADK["app/agent.py<br/>ADK root_agent, gemini-3.8-flash"]
    Collect["Collectors<br/>finops_dashboard/fleet.py, gcp_telemetry.py<br/>ge_mart.py, billing_export.py"]
    Econ["Cost & AlphaEvolve cache rewriter<br/>finops_dashboard/finops.py, prompt_xray.py<br/>telemetry.py RATE_CARDS"]
    Judge["finops_dashboard/validator.py<br/>deterministic checks + Vertex AI judge"]
    Ingest["finops_dashboard/telemetry.py<br/>ingest validation: HTTP 422 + Pub/Sub DLQ"]
    Sim["Demo simulator (disabled in live mode)<br/>finops_dashboard/optimizer.py<br/>finops_dashboard/long_running_agent.py"]
  end

  subgraph APIS["Google Cloud APIs, called as finops-dashboard-runtime-sa"]
    direction TB
    DEAPI["Discovery Engine API<br/>engines, assistants, agents"]
    OBS["Cloud Monitoring, Cloud Logging, Cloud Trace"]
    RUNTIMES["Vertex AI Agent Engine, Cloud Run, GKE<br/>unregistered agent discovery"]
    GEMINI["Vertex AI Gemini<br/>ADK model and judge"]
    PUBSUB["Cloud Pub/Sub DLQ<br/>topics/finops-ingest-dlq"]
  end

  subgraph BQ["BigQuery, created by deploy/setup_bigquery_sink.sh"]
    direction TB
    Sinks["7 Cloud Logging sinks into ds_*_raw datasets"]
    Curated["ds_ge_curated_staging views<br/>deploy/bigquery/ge_mart/curated/"]
    Mart["finops_mart: fct_turns, fct_sessions, agg_daily_usage<br/>deploy/bigquery/ge_mart/mart/"]
    DTS["Hourly scheduled query + incremental MERGE<br/>deploy/setup_mart_refresh.sh"]
    Billing["Cloud Billing export table<br/>finops_dashboard/billing_export.py"]
  end

  Chat -->|"tools/call"| DS
  DS -->|"HTTPS, ID token of the Discovery Engine service agent"| MCP
  MCP -->|"open_dashboard: HTML + state snapshot"| Panel
  Panel -.->|"tools/call through the host bridge"| MCP
  Browser -->|"HTTPS + IAP / identity token"| REST
  ASGI --- MCP
  ASGI --- REST
  ASGI --- ADK
  MCP --> Collect
  MCP --> Sim
  REST --> Collect
  REST --> Econ
  REST --> Judge
  REST --> Ingest
  REST --> Sim
  Ingest --> PUBSUB
  ADK --> Collect
  ADK --> GEMINI
  Judge --> GEMINI
  Collect --> DEAPI
  Collect --> OBS
  Collect --> RUNTIMES
  Collect --> Mart
  Collect -.-> Billing
  Sinks --> Curated
  Curated --> Mart
  DTS --> Mart
```

## Legend

| Box | Source | Status |
| :--- | :--- | :--- |
| ASGI app | [app/fast_api_app.py](../app/fast_api_app.py) | Builds the ADK FastAPI app, then registers the MCP, A2A (`/a2a/app`), and REST routes. If ADK fails to load, it serves the FinOps Dashboard routes without the ADK API. |
| MCP server | [finops_dashboard/mcp_server.py](../finops_dashboard/mcp_server.py) | Live. Streamable HTTP, JSON-RPC 2.0, MCP `2025-06-18` (also `2025-03-26`). 9 tools; read-only tools set `readOnlyHint`, so Gemini Enterprise asks the user to confirm the two that change state (`run_alpha_evolve_generation`, `set_agent_trace_logging`). |
| REST API, A2A and dashboard | [finops_dashboard/server.py](../finops_dashboard/server.py), [finops_dashboard/ui/template.py](../finops_dashboard/ui/template.py), [finops_dashboard/ui/static/dashboard_enhancements.js](../finops_dashboard/ui/static/dashboard_enhancements.js) | Live. Self-contained HTML/JS dashboard with 4 basic tabs and 3 advanced tabs, served with a `Content-Security-Policy` header (`DASHBOARD_SECURITY_HEADERS`), plus a live A2A JSON-RPC 2.0 endpoint (`POST /a2a/app`). The enhancement script and stylesheet are inlined into the same HTML at render time; fleet health, spend projections and adoption figures are computed in the browser from the payload the dashboard already receives (no extra data sources or endpoints). On a real project, simulator buttons and panels are hidden (`sim-panel`) and simulator endpoints return `DISABLED_IN_LIVE_MODE`. |
| ADK agent | [app/agent.py](../app/agent.py) | Live. 10 tools that call the same controller in-process. |
| Collectors | [finops_dashboard/fleet.py](../finops_dashboard/fleet.py), [finops_dashboard/gcp_telemetry.py](../finops_dashboard/gcp_telemetry.py), [finops_dashboard/ge_mart.py](../finops_dashboard/ge_mart.py), [finops_dashboard/billing_export.py](../finops_dashboard/billing_export.py) | Live reads. A source that fails shows up in `source_status` / `errors`, with metrics as `null` and no made-up numbers. Supports runtime SHA-256 PII email pseudonymization (`FINOPS_PII_REDACT=1`) and BigQuery Data Catalog Policy Tag column masking (`build_pii_policy_tag_ddl`). |
| Cost & AlphaEvolve cache rewriter | [finops_dashboard/finops.py](../finops_dashboard/finops.py), [finops_dashboard/prompt_xray.py](../finops_dashboard/prompt_xray.py), [finops_dashboard/telemetry.py](../finops_dashboard/telemetry.py), [experiments/prompt_cache_evolve/](../experiments/prompt_cache_evolve/) | Measured token counts multiplied by **list prices in `RATE_CARDS`**. `propose_cache_friendly_rewrite` in `finops_dashboard/prompt_xray.py` was evolved via the AlphaEvolve harness (`experiments/prompt_cache_evolve/`, lifting `composite_cache_savings_score` from `0.7321` to `0.9957` across 12 multi-turn enterprise prompt benchmarks). |
| Plain-English search | [finops_dashboard/optimizer.py](../finops_dashboard/optimizer.py) (`verify_nl2sql_sql`, `NL2SQL_MAX_BYTES_BILLED`), [finops_dashboard/server.py](../finops_dashboard/server.py) (`query_nl2sql`) | Live on a real project. The question picks one of 5 fixed `SELECT` templates over `finops_mart`; its text never enters the SQL. Each query is checked (one read-only statement, backticked tables in the mart only) and sent with a 100 MB `maximumBytesBilled` cap. Demo mode returns example rows labeled as simulator output. |
| Validator | [finops_dashboard/validator.py](../finops_dashboard/validator.py) | Live. Deterministic grounding checks, plus an optional Vertex AI Gemini judge (`FINOPS_JUDGE_MODEL`, default `gemini-3.8-flash`). |
| Ingest validation & Pub/Sub DLQ | [finops_dashboard/telemetry.py](../finops_dashboard/telemetry.py), [deploy/setup_enterprise_perimeter.sh](../deploy/setup_enterprise_perimeter.sh) | Live. Invalid bodies to `/api/decorator_ingest`, `/api/aive_log` and `/api/csat_rating` get HTTP 422, are stored as values-free dead letters (`GET /api/dead_letters`), and are published to the Cloud Pub/Sub Dead-Letter Queue topic (`projects/project-maui/topics/finops-ingest-dlq`, configured via `FINOPS_DLQ_PUBSUB_TOPIC`). |
| Demo simulator (branch-isolated & live-gated) | [finops_dashboard/optimizer.py](../finops_dashboard/optimizer.py), [finops_dashboard/long_running_agent.py](../finops_dashboard/long_running_agent.py) | **Disabled in live mode** (`DISABLED_IN_LIVE_MODE` unless `FINOPS_ENABLE_SIMULATOR=1`); full interactive simulator preserved on `feature/what-if-simulator`. |
| BigQuery sinks, curated views, mart | [deploy/setup_bigquery_sink.sh](../deploy/setup_bigquery_sink.sh), [deploy/bigquery/provision_ge_mart.py](../deploy/bigquery/provision_ge_mart.py), [deploy/bigquery/ge_mart/](../deploy/bigquery/ge_mart/) | Live in `project-maui`. Raw logs, then curated views, then the mart (`fct_turns`, `fct_sessions`, `agg_daily_usage`). |
| Scheduled & incremental refresh | [deploy/setup_mart_refresh.sh](../deploy/setup_mart_refresh.sh), [finops_dashboard/ge_mart.py](../finops_dashboard/ge_mart.py) | Live. Supports both hourly scheduled rebuilds and partition-pruned incremental `MERGE INTO` (`build_incremental_merge_fct_turns_dml`, `POST /api/ge_mart/refresh` with `{"incremental": true, "lookback_days": 3}`). |
| Invariant checks | [deploy/bigquery/verify_ge_mart_invariants.py](../deploy/bigquery/verify_ge_mart_invariants.py) | 13 conservation and reconciliation checks across raw, curated and mart layers. |
| Billing export | [finops_dashboard/billing_export.py](../finops_dashboard/billing_export.py) | Reads real invoice spend when `FINOPS_BILLING_EXPORT_TABLE` is connected; otherwise the Cost tab shows an explicit `not configured` status and labels figures as list-price estimates. |

## Request paths

1. **Gemini Enterprise chat to MCP.** The assistant calls a tool on the Custom MCP Server data store. Gemini Enterprise sends the request to `https://finops-dashboard-mcp-app-PROJECT_NUMBER.REGION.run.app/mcp` with a Google-signed ID token for its Discovery Engine service agent in `X-Serverless-Authorization`. Cloud Run IAM checks `roles/run.invoker` before the container sees the request.
2. **Side panel.** `open_dashboard` returns the dashboard HTML and a server-side state snapshot. Gemini Enterprise renders it in a sandboxed iframe, with the CSP domains from the MCP resource `_meta.ui.csp`. Refreshes are MCP `tools/call` messages sent through the host bridge, not browser HTTP calls.
3. **Standalone dashboard (`finops-tokenomics-dashboard`).** Protected by Cloud Run native Identity-Aware Proxy (`--iap`) and Cloud Armor WAF (`finops-waf-policy` + Serverless NEG `finops-dashboard-neg`). Operators can also connect via `gcloud run services proxy`.
4. **A2A JSON-RPC 2.0 (`POST /a2a/app`).** Executes `tasks/send` and `message/send` requests against live FinOps Dashboard skills (`query_ge_agent_fleet`, `query_project_telemetry`, `xray_prompt_cache`, `calculate_prompt_cache_economics`).
5. **Instrumented agents.** `@finops_telemetry` records events in the agent's own process. Other services can POST the same event to `/api/decorator_ingest`, which validates it first and routes rejected payloads to the Cloud Pub/Sub DLQ (`finops-ingest-dlq`).
6. **Mart.** Cloud Logging sinks copy Gemini Enterprise and Vertex AI logs into `ds_*_raw`. Curated views read those tables, and `finops_mart.fct_turns` is updated via hourly scheduled query or partition-pruned incremental `MERGE INTO`.

## Security controls in place

- **Private service, IAP and surface split (`FINOPS_SURFACE` / `FINOPS_SPLIT_EDGE`).** Deployed with `--no-allow-unauthenticated`; a request without an identity token gets HTTP 403. Setting `FINOPS_SPLIT_EDGE=1` in [deploy/deploy_cloud_run.sh](../deploy/deploy_cloud_run.sh) splits the edge into `finops-dashboard-mcp-app` (`FINOPS_SURFACE=mcp`: `/mcp`, `/a2a/app`, `/api/health` and the agent card, invoked by the Discovery Engine service agent) and `finops-tokenomics-dashboard` (`FINOPS_SURFACE=dashboard` with Cloud Run native `--iap`: browser dashboard and `/api/*` only, `/mcp` disabled).
- **Cloud Armor WAF & VPC-SC Enterprise Perimeter ([deploy/setup_enterprise_perimeter.sh](../deploy/setup_enterprise_perimeter.sh)).** Provisions the live Cloud Armor security policy `finops-waf-policy` (Rule 1000: OWASP SQLi `sqli-v33-stable` + XSS `xss-v33-stable` deny-403; Rule 2000: 120 req/min rate-based ban), Serverless NEG `finops-dashboard-neg`, Cloud Pub/Sub DLQ topic `finops-ingest-dlq`, BigQuery Data Catalog Policy Tag column masking (`user_email`), and customer-org VPC Service Controls perimeter (`finops_ge_perimeter`).
- **Dedicated runtime service account** `finops-dashboard-runtime-sa` with viewer roles, plus the custom role `finopsGeFleetReader` ([deploy/deploy_cloud_run.sh](../deploy/deploy_cloud_run.sh)). The custom role includes two write permissions, `discoveryengine.agents.manage` (needed so `agents.list` returns agents created by other users) and `discoveryengine.agents.update` (used only by the trace-logging buttons). It has no create, delete or `setIamPolicy`.
- **Values-free audit and Cloud Pub/Sub dead-letter queue.** Ingest records and rejections are written as structured JSON to the `finops_dashboard.audit_sink` logger and published to `projects/PROJECT_ID/topics/finops-ingest-dlq` (`FINOPS_DLQ_PUBSUB_TOPIC`). Rejected payloads record only key names, byte size and SHA-256 digest—never raw values.
- **PII column masking and runtime pseudonymization.** `finops_dashboard/ge_mart.py` provides `build_pii_policy_tag_ddl()` to attach BigQuery Data Catalog Policy Tags (`Fine-Grained Reader` masking) to `user_email`, and `FINOPS_PII_REDACT=1` hashes user emails with SHA-256 (`user-<12hex>@domain`) across all mart readers before data leaves the server.
- **Dashboard CSP.** `/`, `/ui` and `/app` send `Content-Security-Policy` (scripts and fetches limited to the same origin, `object-src 'none'`, `base-uri 'none'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. `'unsafe-inline'` remains because the page uses inline scripts and inline handlers.
- **Read-only plain-English search.** Fixed templates, a fail-closed SQL check before every query, a 100 MB billing cap per query, and no query at all when the question contains a write keyword.
- **Error sanitization.** MCP tool and method failures log the stack trace with a `ref=<id>` and return only the reference to the caller ([finops_dashboard/mcp_server.py](../finops_dashboard/mcp_server.py)).
- **Human confirmation.** The MCP tools that change state are not marked read-only, so Gemini Enterprise asks the user before running them.
- **Guardrail logs.** The `sink-model-armor-sdp` sink copies Model Armor and Sensitive Data Protection log entries to `ds_security_guardrails_raw`. FinOps Dashboard reads those logs; it does not call Model Armor or DLP itself.

## Known gaps

| Gap | Why | Compensating control today |
| :--- | :--- | :--- |
| VPC-SC perimeter requires customer org-level `ACCESS_POLICY_ID` | Organization-level Access Context Manager policies are managed by the customer org admin | `deploy/setup_enterprise_perimeter.sh` automates `gcloud access-context-manager perimeters create finops_ge_perimeter` when `ACCESS_POLICY_ID` is supplied; private Cloud Run IAM + split edge (`--iap`) enforce authentication in every project |
| Gemini Enterprise MCP endpoint (`finops-dashboard-mcp-app`) uses `*.run.app` ingress | Gemini Enterprise's `BYO_MCP` connector only attaches `X-Serverless-Authorization` ID tokens to the default `*.run.app` domain | Edge split (`FINOPS_SPLIT_EDGE=1`) keeps `/mcp` locked to `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com` while placing `finops-tokenomics-dashboard` behind `--iap`, `finops-dashboard-neg` and Cloud Armor `finops-waf-policy` |
| Cloud Billing export is optional per billing account | Requires billing-account export to BigQuery (`FINOPS_BILLING_EXPORT_TABLE`) | When unset, the Cost tab reports `not configured` and labels token costs as list-price estimates |
| In-memory decorator ring buffer is per instance | Decorator events live in memory per Cloud Run instance while rejected ingest payloads are durability-backed by Cloud Pub/Sub (`finops-ingest-dlq`) | Structured JSON audit records are also emitted to Cloud Logging (`finops_dashboard.audit_sink`) and queried from BigQuery |
| Hardcoded list prices | No live Cloud Billing Catalog API sync | Rate cards verified against Vertex AI pricing page are documented in `finops_dashboard/telemetry.py` (`FINOPS_RATE_CARDS_JSON` overrides); unknown models stay `None` (unpriced) |
| `'unsafe-inline'` in the dashboard CSP | Self-contained single-file MCP App HTML uses inline scripts and `onclick` handlers | No external origins are allowed; every fetch is same-origin |

