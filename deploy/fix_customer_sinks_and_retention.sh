#!/usr/bin/env bash
# fix_customer_sinks_and_retention.sh — Patch existing customer Cloud Logging sinks & raw BQ retention
#
# Use this script when deploying VibeLift into an existing customer project (such as
# gemini-enterprise-stage) where the 5 ds_*_raw datasets and Generation-2 sinks already exist:
#   1. Patches `sink-platform-audit` so it captures File Uploads (UploadSessionFile,
#      DownloadSessionFile, ListSessionFileMetadata), Widget actions (ExecuteUiWidgetAction),
#      Async Assist calls (AsyncAssist, ReadAsyncAssist), Connector OAuth token calls
#      (AcquireAccessToken, AcquireAndStoreRefreshToken, ExchangeAuthCredentials,
#      BuildAuthorizationUrl, GetDataConnector), and IAM checks (GetIamPolicy).
#   2. Updates `sink-vertex-reasoning-engine` and configures `sink-ge-inference-tokens` so
#      Discovery Engine inference token logs (discoveryengine.googleapis.com/gen_ai.client.inference.operation.details)
#      and Vertex Agent Engine OTel logs (gen_ai.client.inference.operation.details) are both routed
#      to `ds_vertex_agents_raw` without double-writing rows across the two sinks.
#   3. Standardizes partition expiration across all 5 `ds_*_raw` datasets and existing tables to
#      VIBELIFT_RETENTION_DAYS (default 90 days; fixes the 30d/60d/90d mismatch in stage).
#
# Usage:
#   ./deploy/fix_customer_sinks_and_retention.sh [PROJECT_ID]
#   VIBELIFT_RETENTION_DAYS=90 ./deploy/fix_customer_sinks_and_retention.sh gemini-enterprise-stage
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}"
RETENTION_DAYS="${VIBELIFT_RETENTION_DAYS:-90}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Pass PROJECT_ID as the first argument or set GOOGLE_CLOUD_PROJECT." >&2
  exit 1
fi

echo "=========================================================="
echo " Patching Cloud Logging Sinks & Raw BQ Retention"
echo " Project:        ${PROJECT_ID}"
echo " Retention Days: ${RETENTION_DAYS}"
echo "=========================================================="

upsert_sink() {
  local name="$1"
  local dataset="$2"
  local filter="$3"
  local destination="bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${dataset}"
  local writer_sa=""

  echo ""
  echo "Configuring sink: ${name} -> ${dataset}..."
  if ! gcloud logging sinks describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "  Creating sink ${name}..."
    writer_sa=$(gcloud logging sinks create "${name}" "${destination}" \
        --log-filter="${filter}" \
        --use-partitioned-tables \
        --project="${PROJECT_ID}" \
        --format="value(writerIdentity)")
  else
    echo "  Updating sink ${name}..."
    gcloud logging sinks update "${name}" "${destination}" \
        --log-filter="${filter}" \
        --use-partitioned-tables \
        --project="${PROJECT_ID}" \
        --quiet >/dev/null
    writer_sa=$(gcloud logging sinks describe "${name}" --project="${PROJECT_ID}" --format="value(writerIdentity)")
  fi

  if [[ -n "${writer_sa}" ]]; then
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="${writer_sa}" \
        --role="roles/bigquery.dataEditor" \
        --condition=None \
        --quiet &>/dev/null || true
  fi
}

# 1. Patch sink-platform-audit with complete Discovery Engine method & connector/API error coverage
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
upsert_sink "sink-platform-audit" "ds_ge_audit_raw" "${SINK_AUDIT_FILTER}"

# 2. Separate Vertex AI Reasoning Engine / Agent Engine OTel logs from GE inference token logs
#    so both land in ds_vertex_agents_raw without duplicate delivery.
SINK_VERTEX_FILTER="(protoPayload.serviceName=\"aiplatform.googleapis.com\" AND (protoPayload.methodName=~\"(ReasoningEngineExecutionService|ReasoningEngineService)\" OR ((protoPayload.methodName=\"Predict\" OR protoPayload.methodName=~\"GenerateContent\") AND protoPayload.resourceName=~\"publishers/(google/models/gemini|anthropic/models)\"))) OR logName=\"projects/${PROJECT_ID}/logs/gen_ai.client.inference.operation.details\""
upsert_sink "sink-vertex-reasoning-engine" "ds_vertex_agents_raw" "${SINK_VERTEX_FILTER}"

SINK_INFERENCE_FILTER="logName=\"projects/${PROJECT_ID}/logs/discoveryengine.googleapis.com%2Fgen_ai.client.inference.operation.details\""
upsert_sink "sink-ge-inference-tokens" "ds_vertex_agents_raw" "${SINK_INFERENCE_FILTER}"

# 3. Standardize raw dataset and table partition expirations to RETENTION_DAYS (default 90 days)
echo ""
VIBELIFT_RETENTION_DAYS="${RETENTION_DAYS}" GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" ./deploy/set_log_retention.sh
