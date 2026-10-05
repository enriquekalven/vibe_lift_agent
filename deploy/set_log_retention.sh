#!/usr/bin/env bash
# set_log_retention.sh — Keep raw log-sink data for N days (default 90) in BigQuery.
#
# Log sinks write day-partitioned tables. Retention is enforced by partition expiration, which lives
# in two places, and both are set here:
#   1. the dataset default  -> applies to tables the sinks create later;
#   2. each existing table  -> tables keep the expiration they were created with, so they are updated too.
# Partitions that already expired are gone; a longer retention only keeps data from now on.
#
# Keep the mart lookback equal to this retention (provision_ge_mart.py --lookback-days, default 90),
# otherwise the dashboard shows less history than BigQuery keeps.
#
# Usage:
#   GOOGLE_CLOUD_PROJECT=my-project ./deploy/set_log_retention.sh            # 90 days
#   VIBELIFT_RETENTION_DAYS=180 ./deploy/set_log_retention.sh
# Idempotent. Datasets that do not exist are skipped.
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
RETENTION_DAYS="${VIBELIFT_RETENTION_DAYS:-90}"
# Raw Cloud Logging sink datasets. vibelift_analytics is excluded on purpose: it also holds
# VibeLift's own tables (agent_turns, ...), which must not lose history to a log retention policy.
RAW_DATASETS=(ds_ge_assistant_raw ds_ge_search_raw ds_vertex_agents_raw ds_ge_audit_raw ds_security_guardrails_raw)

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi
if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || (( RETENTION_DAYS < 1 || RETENTION_DAYS > 3650 )); then
  echo "ERROR: VIBELIFT_RETENTION_DAYS must be a whole number of days between 1 and 3650." >&2
  exit 1
fi
SECONDS_TTL=$(( RETENTION_DAYS * 86400 ))

echo "Setting ${RETENTION_DAYS}-day retention on raw log datasets in ${PROJECT_ID}..."
for ds in "${RAW_DATASETS[@]}"; do
  if ! bq show --dataset "${PROJECT_ID}:${ds}" &>/dev/null; then
    echo "  ${ds}: not found, skipped."
    continue
  fi
  if bq update --set_label datacloud:antigravity --default_partition_expiration="${SECONDS_TTL}" "${PROJECT_ID}:${ds}" &>/dev/null; then
    echo "  ${ds}: dataset default partition expiration = ${RETENTION_DAYS}d"
  else
    echo "  ${ds}: skipped dataset default (requires bigquery.datasets.update / dataset OWNER); updating existing tables..."
  fi
  tables=$(bq ls --format=json --max_results=1000 "${PROJECT_ID}:${ds}" \
      | python3 -c 'import json,sys; print("\n".join(t["tableReference"]["tableId"] for t in json.load(sys.stdin) if t.get("type") == "TABLE" and t.get("timePartitioning")))')
  for t in ${tables}; do
    bq update --set_label datacloud:antigravity --time_partitioning_expiration="${SECONDS_TTL}" "${PROJECT_ID}:${ds}.${t}" >/dev/null
    echo "    ${t}: partition expiration = ${RETENTION_DAYS}d"
  done
done
echo "Done. Keep provision_ge_mart.py --lookback-days at ${RETENTION_DAYS} so the mart covers the same window."
