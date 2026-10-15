#!/usr/bin/env bash
# Provisions Google Cloud Scheduler cron jobs for the Autonomous Cost Governance
# lifecycle (Idle Warning -> 48h Grace Period -> Auto-Reclaim) and Daily FinOps
# Executive Email Digest.
#
# Usage:
#   PROJECT_ID=gcc-delta-tdl REGION=us-central1 ./deploy/setup_cloud_scheduler.sh
#
# Optional environment variables:
#   SERVICE_NAME       (default: finops-dashboard-mcp-app)
#   SERVICE_URL        (auto-discovered from Cloud Run if omitted)
#   SCHEDULER_SA_EMAIL (default: finops-dashboard-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com)
#   CLEARANCE_MODE     (default: dry_run; set to execute for live scale-to-zero/delete)
#   TIME_ZONE          (default: America/Los_Angeles)

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
REGION="${REGION:-${GOOGLE_CLOUD_REGION:-us-central1}}"
SERVICE_NAME="${SERVICE_NAME:-finops-dashboard-mcp-app}"
CLEARANCE_MODE="${CLEARANCE_MODE:-dry_run}"
TIME_ZONE="${TIME_ZONE:-America/Los_Angeles}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is required. Export PROJECT_ID=<gcp-project-id> and re-run." >&2
  exit 1
fi

if [[ -z "${SERVICE_URL:-}" ]]; then
  SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format='value(status.url)' 2>/dev/null || true)"
fi

if [[ -z "${SERVICE_URL}" ]]; then
  echo "ERROR: Could not resolve Cloud Run URL for ${SERVICE_NAME}. Set SERVICE_URL=https://... explicitly." >&2
  exit 1
fi

if [[ -z "${SCHEDULER_SA_EMAIL:-}" ]]; then
  SCHEDULER_SA_EMAIL="finops-dashboard-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "${SCHEDULER_SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
    PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
    SCHEDULER_SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  fi
fi

echo "==> Enabling cloudscheduler.googleapis.com and granting roles/run.invoker to ${SCHEDULER_SA_EMAIL}..."
gcloud services enable cloudscheduler.googleapis.com --project="${PROJECT_ID}"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null || true

echo "==> Configuring Cloud Scheduler FinOps jobs in project=${PROJECT_ID} (${REGION})"
echo "    Target Cloud Run URL : ${SERVICE_URL}"
echo "    OIDC Service Account : ${SCHEDULER_SA_EMAIL}"
echo "    Clearance Mode       : ${CLEARANCE_MODE}"

upsert_scheduler_job() {
  local job_name="$1"
  local schedule="$2"
  local uri="$3"
  local body="$4"
  local description="$5"

  if gcloud scheduler jobs describe "${job_name}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" >/dev/null 2>&1; then
    echo "--> Updating existing Cloud Scheduler job: ${job_name}"
    gcloud scheduler jobs update http "${job_name}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --schedule="${schedule}" \
      --time-zone="${TIME_ZONE}" \
      --uri="${uri}" \
      --http-method=POST \
      --headers="Content-Type=application/json" \
      --message-body="${body}" \
      --oidc-service-account-email="${SCHEDULER_SA_EMAIL}" \
      --oidc-token-audience="${SERVICE_URL}" \
      --description="${description}"
  else
    echo "--> Creating Cloud Scheduler job: ${job_name}"
    gcloud scheduler jobs create http "${job_name}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --schedule="${schedule}" \
      --time-zone="${TIME_ZONE}" \
      --uri="${uri}" \
      --http-method=POST \
      --headers="Content-Type=application/json" \
      --message-body="${body}" \
      --oidc-service-account-email="${SCHEDULER_SA_EMAIL}" \
      --oidc-token-audience="${SERVICE_URL}" \
      --description="${description}"
  fi
}

# 1. Stage 1: Daily 08:00 — Scan 7-day idle runtimes, attribute owners, send 48h warning email
upsert_scheduler_job \
  "finops-warn-idle-runtimes" \
  "0 8 * * *" \
  "${SERVICE_URL}/api/cron/warn_idle_runtimes" \
  '{"grace_period_hours":48,"send_warnings":true,"dry_run_email":false}' \
  "FinOps Stage 1: Warn owners of 7d idle Reasoning Engines and Cloud Run services (48h grace period)"

# 2. Stage 2: Every 6 hours — Reclaim expired PENDING_CLEARANCE runtimes that were not snoozed
upsert_scheduler_job \
  "finops-execute-scheduled-clearance" \
  "0 */6 * * *" \
  "${SERVICE_URL}/api/cron/execute_scheduled_clearance" \
  "{\"mode\":\"${CLEARANCE_MODE}\"}" \
  "FinOps Stage 2: Execute scheduled scale-to-zero or Engine deletion after 48h grace period expires"

# 3. Daily 08:30 — Send FinOps Executive Digest email (Top 5 agents, Top 5 users, WoW delta, cache alerts)
DIGEST_BODY='{"cadence":"daily","dry_run":false}'
if [[ -n "${DIGEST_RECIPIENTS:-}" ]]; then
  DIGEST_BODY="{\"cadence\":\"daily\",\"dry_run\":false,\"recipient\":\"${DIGEST_RECIPIENTS}\"}"
fi
upsert_scheduler_job \
  "finops-send-executive-digest" \
  "30 8 * * *" \
  "${SERVICE_URL}/api/cron/send_executive_digest" \
  "${DIGEST_BODY}" \
  "FinOps Executive Digest: Daily Top-5 Agent & User Token Spend, WoW Delta, and Cache Health Alerting"

echo "==> All 3 Cloud Scheduler FinOps jobs configured."
