# Gemini Enterprise reporting mart

VibeLift reads Gemini Enterprise (GE) usage from two BigQuery layers built over the GE log sinks.
Everything is a view except `fct_turns`. That table is a materialized copy of the turn logic view
`v_fct_turns`, rebuilt on a schedule. Each row carries `refreshed_at`, so the dashboard can show
how old the data is.

```mermaid
flowchart LR
  subgraph raw["Raw log sinks (US)"]
    A[ds_ge_assistant_raw]
    S[ds_ge_search_raw]
    I[ds_vertex_agents_raw]
    U[ds_ge_audit_raw]
    M[ds_security_guardrails_raw]
  end
  subgraph cur["ds_ge_curated_staging"]
    VA[v_user_activity_curated]
    VO[v_agentic_operations_curated]
    VU[v_consolidated_audit_log]
    VM[v_model_armor_curated]
  end
  subgraph mart["vibelift_mart"]
    VT[v_fct_turns]
    FT[("fct_turns table")]
    FS[fct_sessions]
    AD[agg_daily_usage]
  end
  A --> VA
  S --> VA
  I --> VO
  U --> VU
  M --> VM
  VA --> VT
  VO --> VT
  VU --> VT
  VM --> VT
  VT -- "--refresh" --> FT
  FT --> FS
  FT --> AD
  AD --> APP[VibeLift dashboard]
  B[Cloud Billing export] --> APP
```

| Layer | Objects | Purpose |
|---|---|---|
| Curated | `v_user_activity_curated`, `v_agentic_operations_curated`, `v_consolidated_audit_log` (`v_model_armor_curated` commented out until Model Armor is enabled) | Cleaned, typed views over each raw sink. Ported from `gemini-enterprise-stage.ds_ge_curated_staging`, with its defects fixed. |
| Mart | `v_fct_turns` (view), `fct_turns` (table), `fct_sessions`, `agg_daily_usage` (views over the table) | VibeLift's own model: one row per turn, per session, and per day × app × agent × model. |
| Cost | `vibelift/billing_export.py` | Per-day AI spend from the Cloud Billing export, joined to daily usage in the app. It is not stored in BigQuery. |

The stage support mart (`ds_ge_support_analytics`) is not ported. It was built for troubleshooting:
it has no cached or reasoning tokens, no inference model names and no cost.

## Provisioning

```bash
# Preview the SQL / validate it with a BigQuery dry run (no changes)
python3 deploy/bigquery/provision_ge_mart.py --project PROJECT --gcloud-auth --print
python3 deploy/bigquery/provision_ge_mart.py --project PROJECT --gcloud-auth --dry-run

# Create or replace the views and build fct_turns (creates the two datasets in --location if absent)
python3 deploy/bigquery/provision_ge_mart.py --project PROJECT --gcloud-auth --apply

# Rebuild only fct_turns (run this on a schedule)
python3 deploy/bigquery/provision_ge_mart.py --project PROJECT --gcloud-auth --refresh

# Print only the standalone CREATE OR REPLACE TABLE ... fct_turns DDL for BigQuery Scheduled Queries
python3 deploy/bigquery/provision_ge_mart.py --project PROJECT --print-refresh
```

### Why `fct_turns` is a table

`v_fct_turns` joins three curated views that parse raw JSON log entries. Reading it takes minutes of slot
time even for a few rows. The dashboard reads turns several times per refresh, with a 6-second
REST timeout. Reading a table is fast, and the heavy work runs once per refresh.

Each rebuild covers the whole lookback window, so audit matching stays correct when late log
entries arrive. `CREATE OR REPLACE TABLE` swaps the table atomically, so readers never see a
half-built table.

To schedule the rebuild as a BigQuery scheduled query, print the DDL with `--print-refresh` and schedule
the `CREATE OR REPLACE TABLE … fct_turns` statement, for example hourly. You can also trigger an
on-demand rebuild from the dashboard (**Cost & Billing → Refresh Mart** button, which calls
`POST /api/ge_mart/refresh`). The dashboard reports the last rebuild as `ge_mart_refreshed_at`.

| Flag | Default | Notes |
|---|---|---|
| `--raw-project` | `--project` | Project that holds the raw sink datasets. |
| `--location` | `US` | Must match the raw datasets. BigQuery cannot join across locations. |
| `--curated-dataset` / `--mart-dataset` | `ds_ge_curated_staging` / `vibelift_mart` | Set `VIBELIFT_GE_CURATED_DATASET` / `VIBELIFT_GE_MART_DATASET` to match. |
| `--lookback-days` | 30 | One window for every source (the stage views used different windows). |
| `--audit-lag-seconds` / `--audit-lead-seconds` | 300 / 30 | Audit-to-activity matching window (see below). |
| `--audit-only` | `errors` | `all` also creates turns from unmatched successful audit calls. |
| `--source key=project.dataset.table` | | Overrides a source table. Keys: `assistant`, `search`, `inference`, `audit_activity`, `audit_data_access` (`armor` commented out until enabled). |

A missing source table, or one in another location, is replaced with an empty stub. Its views then
return no rows instead of failing. The provisioner reports each source as `FOUND`, `MISSING` or
`WRONG_LOCATION`.

For cost, set `VIBELIFT_BILLING_EXPORT_TABLE` to the detailed billing export table. Without it, cost
fields are `null` and the dashboard shows them as `—`.

## Dashboard Integration Across User Journeys

All four primary user journeys read from `vibelift_mart` and respect the top-bar Gemini Enterprise
scope selector (`#geScopeSelect`):

1. **Overview (Tab 6)**: Reads per-engine turns, sessions, users, and model calls alongside `user_centric` people/cohort summaries.
2. **Agents (Tab 0)**: Streams turn & audit diagnostics (`gemini_enterprise_support_events`) from `vibelift_mart.fct_turns` and filters by `#geScopeSelect`.
3. **Cost & Billing (Tab 3)**: Renders daily turns, chat turns, failed turns, sessions, active users, tokens, and joined Cloud Billing AI net spend (`ai_net_usd`, `usd_per_1k_turns`, `usd_per_1m_tokens`) plus the per-app/agent/model breakdown from `vibelift_mart.agg_daily_usage`, with an on-demand **Refresh Mart** button (`POST /api/ge_mart/refresh`).
4. **Users & Feedback (Tab 4)**: Renders conversation sessions from `vibelift_mart.fct_sessions` (clicking a session pre-fills the CSAT feedback form), top users (`power_users_ldap` with null-safe token/cost display), and recent turn activity (`aive_logs.usage_logs`) from `vibelift_mart.fct_turns`.

## Stage defects fixed in the port

1. Model Armor verdicts arrive as `MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK`. Stage compared them with
   `BLOCK`, so no block was ever detected. The prefix is now stripped.
2. `answer_state` is `SUCCEEDED` in the sink. Stage expected `COMPLETED`, so every chat was marked failed.
3. The stage `tbl_*` snapshot tables had gone stale. Everything here is a view.
4. Invented defaults removed: `ANONYMOUS_USER`, `Core Assistant`, `STANDALONE_SESSION`, 0 documents.
   Unknown stays NULL.
5. Searches no longer count as sessions. `session_id` comes from the answer resource name
   (`…/sessions/<id>/assistAnswers/<id>`).
6. The Model Armor `trace` is null. Armor joins on the assist token, then falls back to trace.
7. Token fan-out: stage `FULL OUTER JOIN`ed inference to activity on trace, which copied the same
   tokens onto every row in the trace. Tokens are now aggregated per trace and attached to one turn
   (`trace_rank = 1`). Model name, cached tokens and reasoning tokens were added.
8. One lookback window for all sources.
9. Targeted JSON paths replace regexes over the whole serialized payload.
10. No caller IPs, prompt or response text, or file names. Only sizes, counts and MIME types are kept.
11. No hard-coded project IDs.
12. JSON paths use the sink's lowercase names (`logmetadata.methodname`). The camelCase paths were
    always NULL.
13. Audit status codes are `google.rpc.Code` values, not HTTP codes (8 = `RATE_LIMITED`).
14. Tool diagnostics read only the last input message, not the whole conversation history.
15. Cloud Audit Logs writes two `DATA_ACCESS` rows (`operation.first=true` and `operation.last=true`
    with the same `operation.id` at the same timestamp) for every streaming RPC (`StreamAssist`).
    `v_consolidated_audit_log` deduplicates by `operation.id` so each call is counted once.
16. Some GE apps write `"<elided>"` in `jsonPayload.useriamprincipal` in the activity sink while
    still logging the real `principalEmail` in Cloud Audit Logs. Non-email values become `NULL` in
    `v_user_activity_curated`, and `v_fct_turns` falls back to engine + method + time matching
    (`ENGINE_TIME`) so `COALESCE(user_email, audit_principal_email)` recovers the real principal.

## Audit matching

GE writes the audit entry when a request starts and the activity entry when it finishes. For a
long agent reply the gap can exceed two minutes (171 s was observed on stage). `fct_turns` matches
1:1 in two mutual-best rounds:

- Tier 0 (`TRACE`): by trace when both sides have one.
- Tier 1 (`PRINCIPAL_TIME`): by principal, engine and method, with the audit entry up to
  `audit_lag` before or `audit_lead` after the activity entry.
- Tier 2 (`ENGINE_TIME`): when the activity sink elided `user_email`, by engine and method within
  the same window.

Unmatched audit calls become `AUDIT_ONLY` turns only when they failed. Those failures (quota,
permission, agent errors) may exist only in the audit log.

Validation results (example runs in two reference projects; your numbers will differ):

| Check | `gemini-enterprise-stage` (90d) | `project-maui` (30d) |
|---|---|---|
| Chats matched to audit | 9 of 9 | 11 of 11 (4 `PRINCIPAL_TIME`, 7 `ENGINE_TIME`) |
| Searches matched to audit | 33 of 41 | 10 of 10 (4 `PRINCIPAL_TIME`, 6 `ENGINE_TIME`) |
| Elided / NULL `user_email` after join | 0 | 0 (all 13 `<elided>` rows recovered from audit) |
| `AUDIT_ONLY` turns | 0 | 9 errors (1 failed `StreamAssist` on an engine without activity logs, 8 `ExecuteUiWidgetAction` deadline-expired) |
| Inference tokens | 38,520 in both source and `fct_turns` (no fan-out) | `NULL` (no inference sink in US) |
| Guardrail block | Detected (`SENSITIVE_DATA`, `GE_GUARDRAIL_BLOCK`) | `NULL` (no Model Armor sink in US) |
| Sessions | 7 real sessions for 9 chat turns | 8 real sessions for 11 chat turns |

## Known gaps

- **Tokens need the inference sink.** Per-turn tokens come only from the
  `discoveryengine.googleapis.com/gen_ai.client.inference.operation.details` log, routed to
  `ds_vertex_agents_raw` by the `sink-ge-inference-tokens` sink in `deploy/setup_bigquery_sink.sh`.
  Projects set up before that sink existed (project-maui at the time of the table above) have
  sessions but `NULL` tokens until the sink is created; sinks only capture new entries.
- Model Armor rows need the `sink-model-armor-sdp` sink and Model Armor logging enabled. The
  `armor` source is commented out in `provision_ge_mart.py` until that table exists.
- `sre_triage_agent_telemetry` is in `us-east1` and cannot be joined from the US views. VibeLift
  still reads it separately.
- Cost is project-level AI spend (every AI service in the project). The billing export has no
  per-agent or per-user split, so cost is never allocated below project × day.
- `fct_turns` is rebuilt in full over the lookback window. At much larger volumes, switch to an
  incremental `MERGE` over recent partitions.
