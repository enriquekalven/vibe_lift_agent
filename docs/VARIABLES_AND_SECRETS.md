# Variables and Secrets Reference

Complete inventory of environment variables and secrets read by the FinOps & Tokenomics Analytics Platform (`finops_dashboard/`, `app/`, `deploy/`). Every variable listed below is verified against the codebase by `tests/test_docs_integrity.py`.

## 1. Core GCP & Topology Configuration

| Variable | Read By | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT` | `finops_dashboard/gcp_telemetry.py`, `finops_dashboard/server.py` | Metadata server / gcloud config | Active Google Cloud project ID for Cloud Monitoring, Cloud Logging, BigQuery, and Discovery Engine queries. |
| `GOOGLE_CLOUD_REGION` / `GOOGLE_CLOUD_LOCATION` | `app/fast_api_app.py`, `finops_dashboard/fleet.py` | `us-central1` / `global` | Default Cloud Run region and Vertex AI / Discovery Engine location. |
| `FINOPS_PUBLIC_URL` | `finops_dashboard/server.py`, `finops_dashboard/cost_governance.py` | Auto-detected from request host | Canonical HTTPS URL for the MCP App and A2A agent card. |
| `FINOPS_SURFACE` | `finops_dashboard/server.py` | `all` (`mcp` or `dashboard`) | Isolates `/mcp` and `/a2a/app` (`FINOPS_SURFACE=mcp`) from the browser UI and `/api/*` (`FINOPS_SURFACE=dashboard`). |
| `FINOPS_SPLIT_EDGE` | `deploy/deploy_cloud_run.sh` | `0` | When `1`, deploys both `finops-dashboard-mcp-app` (`FINOPS_SURFACE=mcp`) and IAP-protected `finops-tokenomics-dashboard` (`FINOPS_SURFACE=dashboard`). |
| `ENABLE_MCP_APP` / `MCP_PROTOCOL_VERSION` | `finops_dashboard/mcp_server.py` | `1` / `2025-06-18` | Enables the Streamable HTTP MCP server at `/mcp`. |

## 2. Fleet Discovery, BigQuery Mart & Billing

| Variable | Read By | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `FINOPS_GE_ENGINES` / `FINOPS_GE_LOCATIONS` / `FINOPS_GE_LOCATION` / `FINOPS_GE_COLLECTION` | `finops_dashboard/fleet.py` | `auto` / `global,us,eu` / `global` / `default_collection` | Gemini Enterprise app engine IDs, locations, and Discovery Engine collection to inventory. |
| `FINOPS_RE_LOCATIONS` | `finops_dashboard/fleet.py` | `us-central1,us-west1,us-east4,europe-west1` | Regions scanned for standalone Vertex AI Reasoning Engines. |
| `FINOPS_DISCOVER_UNREGISTERED` / `FINOPS_MONITORED_SERVICES` | `finops_dashboard/fleet.py`, `finops_dashboard/gcp_telemetry.py` | `1` / empty | Controls discovery of unregistered Agent Engines, Cloud Run services, and GKE workloads. |
| `FINOPS_GE_MART_DATASET` / `FINOPS_GE_CURATED_DATASET` / `FINOPS_ANALYTICS_DATASET` / `FINOPS_AUDIT_DATASET` / `FINOPS_OTEL_DATASET` | `finops_dashboard/ge_mart.py`, `finops_dashboard/cost_governance.py` | `finops_mart` / `ds_ge_curated_staging` / `finops_analytics` / `ds_ge_audit_raw` / `otel_genai` | BigQuery datasets for mart queries, snapshots, governance queue, audit logs, and OTel traces. |
| `FINOPS_BILLING_EXPORT_TABLE` / `FINOPS_BQ_MAX_BYTES_BILLED` | `finops_dashboard/billing_export.py`, `finops_dashboard/gcp_telemetry.py` | Empty / `10737418240` (10 GiB) | Cloud Billing BigQuery export table for invoice reconciliation and default `maximumBytesBilled` cap. |
| `FINOPS_RATE_CARDS_JSON` | `finops_dashboard/fleet.py` | Built-in `RATE_CARDS` | Optional JSON override for model token rate cards. |
| `FINOPS_MONTHLY_BUDGET_USD` / `FINOPS_CACHE_HIT_TARGET_PCT` / `FINOPS_SEAT_UTILIZATION_TARGET_PCT` | `finops_dashboard/cost_governance.py` | `0.0` / `50.0` / `85.0` | Governance Score thresholds for monthly budget (`Pillar 4`), cache hit target (`Pillar 1`), and Antigravity seat utilization (`Pillar 5`). |
| `FINOPS_AGY_TIER_ALLOWANCES` | `finops_dashboard/agy_finops.py` | Built-in tier defaults | Weekly dollar allowance pool overrides per Antigravity license tier. |

## 3. Privacy, Observability & Guardrails

| Variable | Read By | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `FINOPS_PII_REDACT` / `FINOPS_POLICY_TAG_PII` / `FINOPS_PROMPT_HASH_KEY` | `finops_dashboard/ge_mart.py`, `finops_dashboard/telemetry.py` | `0` / empty / empty | Enables runtime SHA-256 email pseudonymization (`user-<12hex>@domain`), BigQuery Data Catalog Policy Tag DDL generation, and keyed HMAC prompt hashing. |
| `FINOPS_DEFAULT_SENSITIVE_LOGGING` | `finops_dashboard/server.py` | Follows `enabled` | Optional boolean override (`true`/`false`) for default `sensitiveLoggingEnabled` when toggling agent trace logging. |
| `FINOPS_PROMPT_XRAY_LIVE` / `FINOPS_PROMPT_LOG_BUCKET` | `finops_dashboard/server.py`, `deploy/deploy_cloud_run.sh` | `0` / empty | Opt-in live Prompt Cache X-Ray reading logged GenAI prompt snapshots from the configured GCS bucket. |
| `FINOPS_AGENT_ANALYTICS_DATASET` / `FINOPS_AGENT_ANALYTICS_TABLE` / `FINOPS_AGENT_ANALYTICS_LOCATION` | `app/agent.py`, `deploy/deploy_cloud_run.sh` | empty / `agent_events` / `us-central1` (the deploy script uses the dataset's own location) | Opt-in conversation logging through ADK's BigQuery Agent Analytics plugin, read by the Ambient Quality Agent (AQuA). The table stores user prompts and agent responses verbatim, so keep it in a dedicated, access-controlled dataset. |
| `FINOPS_DLQ_PUBSUB_TOPIC` | `finops_dashboard/telemetry.py` | Empty | Cloud Pub/Sub topic (`projects/PROJECT_ID/topics/finops-ingest-dlq`) for rejected ingest dead-letter events. |
| `FINOPS_NL2SQL_MODEL` / `FINOPS_NL2SQL_LOCATION` / `FINOPS_JUDGE_MODEL` / `FINOPS_JUDGE_LOCATION` | `finops_dashboard/optimizer.py`, `finops_dashboard/validator.py` | `gemini-2.5-flash` / `us-central1` | Vertex AI Gemini model IDs and regions used for read-only NL2SQL synthesis and telemetry grounding validation. |
| `FINOPS_ENABLE_SIMULATOR` | `finops_dashboard/server.py` | `0` in live GCP | Enables simulator mutation routes on a live GCP project when explicitly set to `1`. |

## 4. Cost Governance & Email Secrets (Secret Manager Recommended)

| Variable | Read By | Storage Tier | Purpose |
| :--- | :--- | :--- | :--- |
| `FINOPS_GOVERNANCE_SECRET` / `FINOPS_GOVERNANCE_SECRET_FILE` | `finops_dashboard/cost_governance.py` | **Secret Manager** | Shared HMAC-SHA256 secret signing Keep-Alive snooze tokens across Cloud Run instances. |
| `FINOPS_SNOOZE_BASE_URL` | `finops_dashboard/cost_governance.py` | Env var | Browser-reachable base URL (e.g. IAP-protected `finops-tokenomics-dashboard`) for warning email snooze links. |
| `FINOPS_ALLOW_AUTOMATED_CLEARANCE` / `FINOPS_CLEARANCE_EXCLUDE` | `finops_dashboard/cost_governance.py` | Env var (`0` default) | Enables Stage 2 live Cloud Run scale-to-zero / Reasoning Engine deletion and defines excluded runtime IDs. |
| `FINOPS_GMAIL_DWD_SERVICE_ACCOUNT` / `FINOPS_GMAIL_SENDER` | `finops_dashboard/cost_governance.py` | Env var | Keyless Google Workspace Domain-Wide Delegation service account and sender mailbox (`signJwt`). |
| `FINOPS_GMAIL_REFRESH_TOKEN` / `FINOPS_GMAIL_CLIENT_ID` / `FINOPS_GMAIL_CLIENT_SECRET` / `FINOPS_GMAIL_ACCESS_TOKEN` | `finops_dashboard/cost_governance.py` | **Secret Manager** | OAuth 2.0 Gmail API credentials when not using keyless Domain-Wide Delegation. |
| `FINOPS_SMTP_HOST` / `FINOPS_SMTP_PORT` / `FINOPS_SMTP_USER` / `FINOPS_SMTP_PASSWORD` / `FINOPS_SMTP_STARTTLS` | `finops_dashboard/cost_governance.py` | **Secret Manager** (`FINOPS_SMTP_PASSWORD`) | Fallback SMTP relay configuration for warning emails and executive digests. |
| `FINOPS_ALERT_WEBHOOK_URL` / `FINOPS_ALERT_FROM_EMAIL` / `FINOPS_DEFAULT_OWNER_EMAIL` / `FINOPS_DIGEST_RECIPIENTS` | `finops_dashboard/cost_governance.py` | Env var | HTTPS webhook endpoint, sender address, fallback runtime owner email, and executive digest recipient list. |
