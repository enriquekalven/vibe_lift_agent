#!/usr/bin/env bash
# setup_bigquery_sink.sh — Provision BigQuery dataset, tables, views, and Cloud Logging sink for VibeLift
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
DATASET_ID="vibelift_analytics"
SINK_NAME="vibelift-telemetry-sink"

# Cloud Run services whose logs are exported (VibeLift itself + explicit opt-ins).
# Mirrors the runtime allowlist in gcp_telemetry.get_monitored_service_names().
MONITORED_SERVICES="vibe-lift-agent${VIBELIFT_MONITORED_SERVICES:+,${VIBELIFT_MONITORED_SERVICES}}"
SERVICE_CLAUSE=$(echo "${MONITORED_SERVICES}" | tr ',' '\n' | sed '/^ *$/d; s/^ *//; s/ *$//; s/.*/"&"/' | paste -sd ' ' - | sed 's/ / OR /g')
SINK_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=(${SERVICE_CLAUSE})"

echo "=========================================================="
echo " Provisioning BigQuery Analytics Engine for VibeLift"
echo " Project:  ${PROJECT_ID}"
echo " Region:   ${REGION}"
echo " Dataset:  ${DATASET_ID}"
echo "=========================================================="

# 1. Enable BigQuery API
echo "Ensuring BigQuery API is enabled..."
gcloud services enable bigquery.googleapis.com --project="${PROJECT_ID}"

# 2. Create Dataset if not exists
echo "Creating BigQuery dataset ${DATASET_ID}..."
if ! bq show --dataset "${PROJECT_ID}:${DATASET_ID}" &>/dev/null; then
  bq --location="${REGION}" mk --dataset --label=datacloud:jetski "${PROJECT_ID}:${DATASET_ID}"
fi

# 3. Create Table: agent_turns (Partitioned by DATE(timestamp), Clustered by agent_id, model)
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

# 4. Create Table: agent_eval_runs (Accuracy, Guardrail Verification & Hallucination)
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

# 5. Create Table: alpha_evolve_generations (Evolution actions, diffs, Pareto choices)
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

# 6. Create Table: agent_registry_snapshots (Fleet FinOps tokens & spend from Agent Registry)
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

# 7. Create View: vw_fleet_finops_summary
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

# 8. Create Cloud Logging Sink to stream the monitored services' logs into BigQuery
echo "Configuring Cloud Logging Sink: ${SINK_NAME} (filter: ${SINK_FILTER})..."
if ! gcloud logging sinks describe "${SINK_NAME}" --project="${PROJECT_ID}" &>/dev/null; then
  SINK_IDENTITY=$(gcloud logging sinks create "${SINK_NAME}"       "bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${DATASET_ID}"       --log-filter="${SINK_FILTER}"       --project="${PROJECT_ID}"       --format="value(writerIdentity)")
  echo "Created sink with identity: ${SINK_IDENTITY}"
  # Grant writerIdentity DataEditor access on BigQuery dataset
  bq show --format=prettyjson "${PROJECT_ID}:${DATASET_ID}" > /tmp/ds.json
  # Assign access to logging service account
  gcloud projects add-iam-policy-binding "${PROJECT_ID}"       --member="${SINK_IDENTITY}"       --role="roles/bigquery.dataEditor"       --quiet > /dev/null || true
else
  echo "Cloud Logging sink ${SINK_NAME} already exists; reconciling its filter..."
  gcloud logging sinks update "${SINK_NAME}" --log-filter="${SINK_FILTER}" --project="${PROJECT_ID}" --quiet
fi

echo "=========================================================="
echo " BigQuery Analytics Engine & Telemetry Sink Successfully Configured!"
echo " Dataset: \`${PROJECT_ID}.${DATASET_ID}\`"
echo " View:    \`${PROJECT_ID}.${DATASET_ID}.vw_fleet_finops_summary\`"
echo "=========================================================="
