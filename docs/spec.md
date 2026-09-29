# VibeLift — Technical Spec

VibeLift is a Cloud Run web app and MCP App that shows how the Gemini Enterprise (GE)
agents in one Google Cloud project are used and what they cost. In live mode every
number comes from Google Cloud APIs or BigQuery. Anything that is not measured is shown
as "—" / "Not measured", or is labelled as simulator data.

## Modes

| Mode | When | Data |
| :--- | :--- | :--- |
| Live | `GOOGLE_CLOUD_PROJECT` is a real project | Google Cloud APIs + BigQuery only |
| Simulator | project is `test-project` / unconfigured (unit tests, local demos) | Seed data, labelled "simulator" |

## Dashboard

Simple mode (default) shows four tabs. **Advanced ▾** reveals three more.

| Tab (id) | Mode | Content |
| :--- | :--- | :--- |
| Overview (6) | Simple | Headline, KPIs, requests over time, requests by agent, spend by model, fleet mix, top users, "Needs attention" |
| Agents (0) | Simple | Every agent in every GE app, with runtime metrics |
| Cost (3) | Simple | Model spend by model; Advanced adds billing-export SKUs and modelled cost panels |
| Users (4) | Simple | Users seen in audit logs; Advanced adds per-app user groups and alerts |
| Goals & Metrics (1), Optimizer & Testing (2), Tools & SDK (5) | Advanced | Optimizer and what-if simulators (labelled as such) |

The **Gemini Enterprise app** dropdown filters by region (`loc:global`, `loc:us`) or by one
app (`eng:<location>/<engine_id>`). It filters agents, requests, the trend chart and users.
Model spend is project-wide because Vertex AI usage metrics are not tagged by GE app.

## Data sources

| Metric | Source | Window |
| :--- | :--- | :--- |
| GE apps and agents | Discovery Engine API; auto-discovers `APP_TYPE_INTRANET` engines in `global`, `us`, `eu` (`VIBELIFT_GE_ENGINES`, `VIBELIFT_GE_LOCATIONS`) | current |
| Requests, 4xx, 5xx, latency | Cloud Monitoring `run.googleapis.com/request_count`, `aiplatform.googleapis.com/reasoning_engine/request_count` | selected (default 24h) |
| Requests over time | Same metrics, bucketed (5 min to 6 h depending on the window) | selected |
| Model tokens and estimated spend | Vertex AI publisher token metrics × list price (an estimate, not an invoice) | selected |
| Users, sessions, per-app attribution | BigQuery `ds_ge_audit_raw` (audit-log `resourceName` carries the engine) + agent OTel spans | 7 days |
| Billed cost per SKU | Cloud Billing BigQuery export, when `VIBELIFT_BILLING_EXPORT_TABLE` is set; otherwise "not connected" | 30 days |
| Alerts | Derived from observed 4xx/5xx rates and zero cache reads | selected |

Runtimes registered in several GE apps (same Cloud Run service or Agent Engine) are
counted once in totals (`vibelift.fleet.runtime_backend_key`).

## Data check

`POST /api/validate_telemetry` runs rule-based checks on `/api/state` (provenance of every
user row, request totals match per-runtime sums, no fake URIs, and so on). With
`run_llm_judge: true` it also asks Vertex AI Gemini (`VIBELIFT_JUDGE_MODEL`, default
`gemini-2.5-flash`) to review a digest. The LLM can only lower the verdict. If it is
unavailable, the reason is returned in `llm_judge.judge_error` and shown in the UI.

## Configuration

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GOOGLE_CLOUD_PROJECT` | gcloud config | Project to monitor |
| `VIBELIFT_GE_ENGINES` | `auto` | GE apps (`auto` or `loc/engine_id,...`) |
| `VIBELIFT_GE_LOCATIONS` | `global,us,eu` | Regions scanned by auto-discovery |
| `VIBELIFT_BILLING_EXPORT_TABLE` | unset | `project.dataset.table` of the billing export |
| `VIBELIFT_JUDGE_MODEL` / `_LOCATION` / `_TIMEOUT_S` | `gemini-2.5-flash` / `us-central1` / `25` | LLM judge |

Runtime service account roles: Discovery Engine viewer, Monitoring viewer, BigQuery data
viewer + job user, `roles/aiplatform.user` (judge), and BigQuery data viewer on the
billing export dataset if one is configured.

## Endpoints

- `GET /` dashboard, `GET /api/state`, `GET /api/ge_fleet?window_hours=N`
- `POST /api/validate_telemetry`
- `POST /mcp` MCP (streamable HTTP): `open_dashboard(focus_tab=6)`, `query_ge_agent_fleet`, `query_project_telemetry`, and others

## Security

API data is rendered with `textContent` / `createElement`, never `innerHTML`. BigQuery
identifiers taken from configuration are validated before being put into SQL. The Cloud Run
service is private (`--no-allow-unauthenticated`).
