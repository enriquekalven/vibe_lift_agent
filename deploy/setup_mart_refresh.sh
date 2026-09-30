#!/usr/bin/env bash
# setup_mart_refresh.sh — Hourly BigQuery scheduled query that rebuilds vibelift_mart.fct_turns.
#
# fct_turns is a materialized snapshot (the live view takes 15-40 s), so it only changes when it is
# rebuilt. This creates (or updates, if one with the same display name exists) a BigQuery Data Transfer
# Service scheduled query that runs the rebuild DDL from `provision_ge_mart.py --print-refresh`.
#
# The query runs as a service account, not as your user, so it keeps working if you leave the project.
# Default: the VibeLift runtime service account created by deploy_cloud_run.sh, which already has
# BigQuery Job User, Data Viewer (reads raw + curated) and WRITER on vibelift_mart.
#
# Prerequisites: setup_bigquery_sink.sh (curated views + mart exist) and deploy_cloud_run.sh (service
# account + mart grant). You need iam.serviceAccounts.actAs on the service account.
#
# Usage:
#   GOOGLE_CLOUD_PROJECT=my-project ./deploy/setup_mart_refresh.sh
# Optional env:
#   VIBELIFT_REFRESH_SCHEDULE  default "every 1 hours" (Data Transfer schedule syntax)
#   VIBELIFT_REFRESH_SA        default vibe-lift-runtime-sa@PROJECT.iam.gserviceaccount.com
#   BQ_LOCATION                default US (must match the vibelift_mart dataset)
#   VIBELIFT_REFRESH_RUN_NOW   default 1: start one run immediately to prove it works
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
BQ_LOCATION="${BQ_LOCATION:-US}"
SCHEDULE="${VIBELIFT_REFRESH_SCHEDULE:-every 1 hours}"
SA_EMAIL="${VIBELIFT_REFRESH_SA:-vibe-lift-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
RUN_NOW="${VIBELIFT_REFRESH_RUN_NOW:-1}"
DISPLAY_NAME="VibeLift fct_turns refresh"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

echo "Project: ${PROJECT_ID}  Location: ${BQ_LOCATION}  Schedule: ${SCHEDULE}"
echo "Runs as: ${SA_EMAIL}"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "ERROR: service account ${SA_EMAIL} not found. Run ./deploy/deploy_cloud_run.sh first," >&2
  echo "  or set VIBELIFT_REFRESH_SA to an account with BigQuery Job User, Data Viewer and" >&2
  echo "  Data Editor on vibelift_mart." >&2
  exit 1
fi
if ! bq show --format=none "${PROJECT_ID}:vibelift_mart.v_fct_turns" &>/dev/null; then
  echo "ERROR: ${PROJECT_ID}:vibelift_mart.v_fct_turns not found. Run ./deploy/setup_bigquery_sink.sh first." >&2
  exit 1
fi

echo "Enabling the BigQuery Data Transfer API..."
gcloud services enable bigquerydatatransfer.googleapis.com --project="${PROJECT_ID}"

# The Data Transfer service agent mints tokens for the service account when a run starts.
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
DTS_AGENT="service-${PROJECT_NUMBER}@gcp-sa-bigquerydatatransfer.iam.gserviceaccount.com"
gcloud beta services identity create --service=bigquerydatatransfer.googleapis.com \
    --project="${PROJECT_ID}" &>/dev/null || true
echo "Granting roles/iam.serviceAccountTokenCreator on ${SA_EMAIL} to ${DTS_AGENT}..."
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" --project="${PROJECT_ID}" \
    --member="serviceAccount:${DTS_AGENT}" --role="roles/iam.serviceAccountTokenCreator" \
    --condition=None --quiet >/dev/null

QUERY_FILE="$(mktemp)"
trap 'rm -f "${QUERY_FILE}"' EXIT
python3 deploy/bigquery/provision_ge_mart.py --project="${PROJECT_ID}" --print-refresh > "${QUERY_FILE}"
PARAMS=$(python3 -c 'import json,sys; print(json.dumps({"query": open(sys.argv[1]).read()}))' "${QUERY_FILE}")

EXISTING=$(bq ls --transfer_config --transfer_location="${BQ_LOCATION}" --project_id="${PROJECT_ID}" \
    --format=json 2>/dev/null \
  | python3 -c 'import json,sys
raw = sys.stdin.read().strip() or "[]"
names = [c["name"] for c in json.loads(raw) if c.get("displayName") == sys.argv[1]]
print(names[0] if names else "")' "${DISPLAY_NAME}")

if [[ -n "${EXISTING}" ]]; then
  echo "Updating existing scheduled query ${EXISTING}..."
  bq update --transfer_config --params="${PARAMS}" --schedule="${SCHEDULE}" \
      --service_account_name="${SA_EMAIL}" "${EXISTING}"
  CONFIG="${EXISTING}"
else
  echo "Creating scheduled query '${DISPLAY_NAME}'..."
  bq mk --transfer_config --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" \
      --data_source=scheduled_query --display_name="${DISPLAY_NAME}" \
      --schedule="${SCHEDULE}" --service_account_name="${SA_EMAIL}" --params="${PARAMS}"
  CONFIG=$(bq ls --transfer_config --transfer_location="${BQ_LOCATION}" --project_id="${PROJECT_ID}" \
      --format=json | python3 -c 'import json,sys
names = [c["name"] for c in json.load(sys.stdin) if c.get("displayName") == sys.argv[1]]
print(names[0] if names else "")' "${DISPLAY_NAME}")
fi

if [[ "${RUN_NOW}" == "1" && -n "${CONFIG}" ]]; then
  NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "Starting one run now (${NOW})..."
  bq mk --transfer_run --run_time="${NOW}" "${CONFIG}" >/dev/null || \
    echo "WARNING: could not start a manual run; the schedule still applies." >&2
fi

echo ""
echo "Scheduled query: ${CONFIG:-<not found>}"
echo "Check runs:  bq ls --transfer_run --max_results=5 ${CONFIG:-<config>}"
echo "Console:     BigQuery > Scheduled queries > ${DISPLAY_NAME}"
echo "Remove:      bq rm -f --transfer_config ${CONFIG:-<config>}"
