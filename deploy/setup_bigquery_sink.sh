#!/usr/bin/env bash
# setup_bigquery_sink.sh — Turnkey BigQuery Datasets, Curated Views, Mart, and Cloud Logging Sinks
#
# Provisions the complete telemetry & FinOps data plane used by VibeLift and Gemini Enterprise:
#   1. Raw Cloud Logging BigQuery Datasets (US multi-region):
#      - ds_ge_assistant_raw
#      - ds_ge_search_raw
#      - ds_vertex_agents_raw
#      - ds_ge_audit_raw
#      - ds_security_guardrails_raw
#      - vibelift_analytics
#   2. Cloud Logging Sinks (with --use-partitioned-tables and IAM writerIdentity bindings):
#      - sink-ge-assistant-activity
#      - sink-ge-search-activity
#      - sink-vertex-reasoning-engine
#      - sink-platform-audit
#      - sink-model-armor-sdp
#      - vibelift-telemetry-sink
#   3. VibeLift Analytics Tables & Summary View:
#      - agent_turns, agent_eval_runs, alpha_evolve_generations, agent_registry_snapshots
#      - vw_fleet_finops_summary
#   2b. Raw log retention: VIBELIFT_RETENTION_DAYS (default 90) via deploy/set_log_retention.sh
#   4. Curated Staging Views & Reporting Mart (via deploy/bigquery/provision_ge_mart.py):
#      - ds_ge_curated_staging (v_user_activity_curated, v_agentic_operations_curated, v_consolidated_audit_log, v_model_armor_curated)
#      - vibelift_mart (v_fct_turns, fct_turns, fct_sessions, agg_daily_usage, v_looker_l1_l2_support)
#
# Idempotent and safe to run on existing or clean GCP projects.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
BQ_LOCATION="${BQ_LOCATION:-US}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
DATASET_ID="vibelift_analytics"
CURATED_DATASET="${VIBELIFT_GE_CURATED_DATASET:-ds_ge_curated_staging}"
MART_DATASET="${VIBELIFT_GE_MART_DATASET:-vibelift_mart}"
# Days of raw log history kept in BigQuery and read by the mart (see deploy/set_log_retention.sh).
RETENTION_DAYS="${VIBELIFT_RETENTION_DAYS:-90}"
SINK_NAME="vibelift-telemetry-sink"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

echo "=========================================================="
echo " Provisioning Complete BigQuery Telemetry Engine & Sinks"
echo " Project:     ${PROJECT_ID}"
echo " BQ Location: ${BQ_LOCATION}"
echo " Default Reg: ${REGION}"
echo "=========================================================="

# 1. Enable BigQuery and Cloud Logging APIs
echo "Ensuring required APIs are enabled..."
gcloud services enable \
    bigquery.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    --project="${PROJECT_ID}"

# 2. Helper to create BigQuery dataset idempotently
create_dataset_if_missing() {
  local ds="$1"
  local desc="$2"
  echo "Checking dataset: ${PROJECT_ID}:${ds}..."
  if ! bq show --dataset "${PROJECT_ID}:${ds}" &>/dev/null; then
    echo "  Creating dataset ${ds} (location: ${BQ_LOCATION})..."
    bq --location="${BQ_LOCATION}" mk --dataset \
        --label=app:vibelift \
        --description="${desc}" \
        "${PROJECT_ID}:${ds}"
  else
    echo "  Dataset ${ds} already exists."
  fi
}

echo ""
echo "--- Step 1: Provisioning Raw & Reporting Datasets ---"
create_dataset_if_missing "ds_ge_assistant_raw" "Gemini Enterprise Assistant raw user activity log sink"
create_dataset_if_missing "ds_ge_search_raw" "Gemini Enterprise Search raw user activity log sink"
create_dataset_if_missing "ds_vertex_agents_raw" "Vertex AI Reasoning Engine and Model inference log sink"
create_dataset_if_missing "ds_ge_audit_raw" "Gemini Enterprise and Cloud Audit data access & activity log sink"
create_dataset_if_missing "ds_security_guardrails_raw" "Model Armor and Sensitive Data Protection guardrail log sink"
create_dataset_if_missing "${DATASET_ID}" "VibeLift agent runtime turns, evaluations, and optimizer snapshots"
create_dataset_if_missing "${CURATED_DATASET}" "Curated typed views over raw Gemini Enterprise log sinks"
create_dataset_if_missing "${MART_DATASET}" "VibeLift turn, session, and daily usage reporting mart"

# 3. Helper to create or update Cloud Logging Sinks idempotently
configure_sink() {
  local name="$1"
  local dataset="$2"
  local filter="$3"

  echo ""
  echo "Configuring Sink: ${name} -> ${dataset}..."
  local destination="bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${dataset}"

  local writer_sa=""
  if ! gcloud logging sinks describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "  Creating sink ${name}..."
    writer_sa=$(gcloud logging sinks create "${name}" "${destination}" \
        --log-filter="${filter}" \
        --use-partitioned-tables \
        --project="${PROJECT_ID}" \
        --format="value(writerIdentity)")
  else
    echo "  Sink ${name} exists; updating filter and destination..."
    gcloud logging sinks update "${name}" "${destination}" \
        --log-filter="${filter}" \
        --use-partitioned-tables \
        --project="${PROJECT_ID}" \
        --quiet >/dev/null
    writer_sa=$(gcloud logging sinks describe "${name}" --project="${PROJECT_ID}" --format="value(writerIdentity)")
  fi

  if [[ -n "${writer_sa}" ]]; then
    echo "  Granting roles/bigquery.dataEditor to ${writer_sa}..."
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="${writer_sa}" \
        --role="roles/bigquery.dataEditor" \
        --condition=None \
        --quiet >/dev/null || true
  fi
}

echo ""
echo "--- Step 2: Provisioning Cloud Logging Sinks ---"

# Sink 1: GE Assistant Activity
SINK_GE_ASSISTANT_FILTER="logName=\"projects/${PROJECT_ID}/logs/discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity\" AND jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.AssistantService\""
configure_sink "sink-ge-assistant-activity" "ds_ge_assistant_raw" "${SINK_GE_ASSISTANT_FILTER}"

# Sink 2: GE Search Activity
SINK_GE_SEARCH_FILTER="logName=\"projects/${PROJECT_ID}/logs/discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity\" AND (jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.SearchService\" OR jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.ConversationSearchService\")"
configure_sink "sink-ge-search-activity" "ds_ge_search_raw" "${SINK_GE_SEARCH_FILTER}"

# Sink 3: Vertex AI Agents & Reasoning Engines (plus standalone Vertex Agent Engine OTel logs;
# Discovery Engine gen_ai.client.inference.operation.details is routed separately by Sink 3b so
# the two sinks never write duplicate rows into ds_vertex_agents_raw).
SINK_VERTEX_FILTER="(protoPayload.serviceName=\"aiplatform.googleapis.com\" AND (protoPayload.methodName=~\"(ReasoningEngineExecutionService|ReasoningEngineService)\" OR ((protoPayload.methodName=\"Predict\" OR protoPayload.methodName=~\"GenerateContent\") AND protoPayload.resourceName=~\"publishers/(google/models/gemini|anthropic/models)\"))) OR logName=\"projects/${PROJECT_ID}/logs/gen_ai.client.inference.operation.details\""
configure_sink "sink-vertex-reasoning-engine" "ds_vertex_agents_raw" "${SINK_VERTEX_FILTER}"

# Sink 3b: Gemini Enterprise gen_ai inference logs. This is the ONLY source of per-turn input/output/
# cached token counts in vibelift_mart (fct_turns, fct_sessions, the session token drilldown). BigQuery
# names the table discoveryengine_googleapis_com_gen_ai_client_inference_operation_details, which is
# what deploy/bigquery/provision_ge_mart.py reads. Without this sink the mart has sessions but no tokens.
SINK_INFERENCE_FILTER="logName=\"projects/${PROJECT_ID}/logs/discoveryengine.googleapis.com%2Fgen_ai.client.inference.operation.details\""
configure_sink "sink-ge-inference-tokens" "ds_vertex_agents_raw" "${SINK_INFERENCE_FILTER}"

# Sink 4: Platform Audit Logs (plus Discovery Engine connector_activity, api_errors, and data_connectors)
SINK_AUDIT_FILTER=$(cat <<EOF
(
  logName=~"projects/${PROJECT_ID}/logs/cloudaudit.googleapis.com%2F(activity|data_access)"
  AND protoPayload.serviceName="discoveryengine.googleapis.com"
  AND (
    protoPayload.methodName=~"\.(CreateAgent|UpdateAgent|DeleteAgent|GetIamPolicy|SetIamPolicy)$"
    OR protoPayload.methodName=~"\.(CreateDataConnector|UpdateDataConnector|DeleteDataConnector|SyncDataConnector|RunDataConnector|GetDataConnector|AcquireAccessToken|AcquireAndStoreRefreshToken|ExchangeAuthCredentials|BuildAuthorizationUrl)$"
    OR protoPayload.methodName=~"\.(CreateEngine|UpdateEngine|DeleteEngine|ImportDocuments|PurgeDocuments)$"
    OR protoPayload.methodName=~"\.(StreamAssist|Assist|AsyncAssist|ReadAsyncAssist|AddContextFile|UploadSessionFile|DownloadSessionFile|ListSessionFileMetadata|ExecuteUiWidgetAction|AnswerQuery|Search)$"
  )
)
OR logName=~"projects/${PROJECT_ID}/logs/discoveryengine.googleapis.com%2F(connector_activity|api_errors|data_connectors)"
EOF
)
configure_sink "sink-platform-audit" "ds_ge_audit_raw" "${SINK_AUDIT_FILTER}"

# Sink 5: Model Armor & Sensitive Data Protection
SINK_ARMOR_FILTER="protoPayload.serviceName=\"modelarmor.googleapis.com\" OR jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.modelarmor.logging.v1.SanitizeOperationLogEntry\" OR (protoPayload.serviceName=\"dlp.googleapis.com\" AND protoPayload.methodName=~\"(InspectContent|DeidentifyContent)\")"
configure_sink "sink-model-armor-sdp" "ds_security_guardrails_raw" "${SINK_ARMOR_FILTER}"

# Sink 6: VibeLift Cloud Run Telemetry
MONITORED_SERVICES="vibe-lift-agent${VIBELIFT_MONITORED_SERVICES:+,${VIBELIFT_MONITORED_SERVICES}}"
SERVICE_CLAUSE=$(echo "${MONITORED_SERVICES}" | tr ',' '\n' | sed '/^ *$/d; s/^ *//; s/ *$//; s/.*/"&"/' | paste -sd ' ' - | sed 's/ / OR /g')
SINK_VIBELIFT_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=(${SERVICE_CLAUSE})"
configure_sink "vibelift-telemetry-sink" "${DATASET_ID}" "${SINK_VIBELIFT_FILTER}"

echo ""
echo "--- Step 2b: Raw log retention (${RETENTION_DAYS} days) ---"
VIBELIFT_RETENTION_DAYS="${RETENTION_DAYS}" GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" ./deploy/set_log_retention.sh

echo ""
echo "--- Step 3: Provisioning VibeLift Analytics Tables & Views ---"

# Table: agent_turns
echo "Creating table ${DATASET_ID}.agent_turns..."
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${DATASET_ID}.agent_turns\` (
  turn_id STRING,
  timestamp TIMESTAMP,
  agent_id STRING,
  model STRING,
  turn_index INT64,
  prompt_tokens INT64,
  cached_tokens INT64,
  uncached_tokens INT64,
  output_tokens INT64,
  thoughts_tokens INT64,
  cache_hit_ratio FLOAT64,
  latency_ms INT64,
  status_code INT64,
  tool_called STRING,
  cache_breakpoint_line INT64,
  cache_breakpoint_reason STRING,
  prompt_prefix_hash STRING,
  naive_cost_usd FLOAT64,
  actual_cost_usd FLOAT64,
  saved_usd FLOAT64,
  generation INT64
)
PARTITION BY DATE(timestamp)
CLUSTER BY agent_id, model;
"

# Table: agent_eval_runs
echo "Creating table ${DATASET_ID}.agent_eval_runs..."
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${DATASET_ID}.agent_eval_runs\` (
  eval_id STRING,
  timestamp TIMESTAMP,
  agent_id STRING,
  generation INT64,
  metric_name STRING,
  score_pct FLOAT64,
  passed_guardrail BOOL,
  target_threshold FLOAT64
)
PARTITION BY DATE(timestamp)
CLUSTER BY agent_id;
"

# Table: alpha_evolve_generations
echo "Creating table ${DATASET_ID}.alpha_evolve_generations..."
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${DATASET_ID}.alpha_evolve_generations\` (
  generation INT64,
  timestamp TIMESTAMP,
  agent_id STRING,
  action_title STRING,
  parameter_targeted STRING,
  root_cause_from_logs STRING,
  action_taken STRING,
  impact_summary STRING,
  diff_snippet STRING,
  composite_score FLOAT64,
  decision STRING,
  health_status STRING
);
"

# Table: agent_registry_snapshots
echo "Creating table ${DATASET_ID}.agent_registry_snapshots..."
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${DATASET_ID}.agent_registry_snapshots\` (
  snapshot_timestamp TIMESTAMP,
  project_id STRING,
  total_registered_agents INT64,
  total_mcp_servers INT64,
  total_skills INT64,
  fleet_prompt_tokens INT64,
  fleet_cached_tokens INT64,
  fleet_cache_hit_ratio FLOAT64,
  total_naive_spend_usd FLOAT64,
  total_actual_spend_usd FLOAT64,
  total_net_savings_usd FLOAT64,
  agent_breakdown_json STRING,
  mcp_breakdown_json STRING
);
"

# View: vw_fleet_finops_summary
echo "Creating view ${DATASET_ID}.vw_fleet_finops_summary..."
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
CREATE OR REPLACE VIEW \`${PROJECT_ID}.${DATASET_ID}.vw_fleet_finops_summary\` AS
SELECT
  agent_id,
  model,
  COUNT(1) AS total_turns,
  SUM(prompt_tokens) AS total_prompt_tokens,
  SUM(cached_tokens) AS total_cached_tokens,
  ROUND(SAFE_DIVIDE(SUM(cached_tokens), SUM(prompt_tokens)) * 100.0, 2) AS aggregate_cache_hit_ratio,
  ROUND(SUM(naive_cost_usd), 4) AS total_naive_spend_usd,
  ROUND(SUM(actual_cost_usd), 4) AS total_actual_spend_usd,
  ROUND(SUM(saved_usd), 4) AS total_finops_savings_usd,
  ROUND(AVG(latency_ms), 1) AS avg_latency_ms
FROM \`${PROJECT_ID}.${DATASET_ID}.agent_turns\`
GROUP BY agent_id, model;
"

echo ""
echo "--- Step 4: Provisioning Curated Staging Views & Reporting Mart ---"
# --apply creates the curated views and mart views AND builds the materialized fct_turns table.
# (--apply and --refresh are mutually exclusive; --refresh only rebuilds tables on a schedule.)
# --gcloud-auth uses your gcloud login, so Application Default Credentials are not required.
# Raw tables that do not exist yet (no logs routed so far) resolve to empty views, not errors.
MART_STATUS="ok"
if ! python3 deploy/bigquery/provision_ge_mart.py --project="${PROJECT_ID}" --location="${BQ_LOCATION}" \
    --curated-dataset="${CURATED_DATASET}" --mart-dataset="${MART_DATASET}" \
    --gcloud-auth --apply --lookback-days="${RETENTION_DAYS}"; then
  MART_STATUS="failed"
  echo "ERROR: provision_ge_mart.py failed; curated views and ${MART_DATASET} may be incomplete." >&2
  echo "  Needs: python3 with 'pip install -r requirements.txt' (google-cloud-bigquery), and BigQuery" >&2
  echo "  Data Editor + Job User for your account. Re-run just this step with:" >&2
  echo "  python3 deploy/bigquery/provision_ge_mart.py --project=${PROJECT_ID} --location=${BQ_LOCATION} --curated-dataset=${CURATED_DATASET} --mart-dataset=${MART_DATASET} --gcloud-auth --apply --lookback-days=${RETENTION_DAYS}" >&2
fi

echo ""
echo "=========================================================="
echo " BigQuery Datasets, Views, Mart, & Log Sinks"
echo " Datasets:"
echo "   - \`${PROJECT_ID}.ds_ge_assistant_raw\`"
echo "   - \`${PROJECT_ID}.ds_ge_search_raw\`"
echo "   - \`${PROJECT_ID}.ds_vertex_agents_raw\`"
echo "   - \`${PROJECT_ID}.ds_ge_audit_raw\`"
echo "   - \`${PROJECT_ID}.ds_security_guardrails_raw\`"
echo "   - \`${PROJECT_ID}.${DATASET_ID}\`"
echo "   - \`${PROJECT_ID}.${CURATED_DATASET}\`"
echo "   - \`${PROJECT_ID}.${MART_DATASET}\` (provisioning: ${MART_STATUS})"
echo ""
echo " Raw log retention: ${RETENTION_DAYS} days (mart lookback: ${RETENTION_DAYS} days)"
echo ""
echo " Sinks:"
echo "   - sink-ge-assistant-activity"
echo "   - sink-ge-search-activity"
echo "   - sink-vertex-reasoning-engine"
echo "   - sink-ge-inference-tokens"
echo "   - sink-platform-audit"
echo "   - sink-model-armor-sdp"
echo "   - vibelift-telemetry-sink"
echo "=========================================================="
echo ""
echo "Manual steps this script cannot do (see README 'Deploy in your own GCP project'):"
echo "  1. Turn on user activity logging in each Gemini Enterprise app (Console > Gemini Enterprise >"
echo "     your app > settings/observability). Without it the gemini_enterprise_user_activity and"
echo "     gen_ai.client.inference.operation.details logs are never written and the mart stays empty."
echo "  2. Enable Data Access audit logs for discoveryengine.googleapis.com (IAM & Admin > Audit Logs)"
echo "     if you want read/assist calls in ds_ge_audit_raw. Admin Activity logs are always on."
echo "  3. Sinks only capture NEW log entries. After real GE traffic, rebuild the mart table with:"
echo "     python3 deploy/bigquery/provision_ge_mart.py --project=${PROJECT_ID} --location=${BQ_LOCATION} --gcloud-auth --refresh"
echo "  4. After ./deploy/deploy_cloud_run.sh, keep fct_turns fresh with an hourly scheduled query:"
echo "     GOOGLE_CLOUD_PROJECT=${PROJECT_ID} ./deploy/setup_mart_refresh.sh"
if [[ "${MART_STATUS}" != "ok" ]]; then
  exit 1
fi
