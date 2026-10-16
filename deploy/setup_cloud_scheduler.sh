#!/usr/bin/env bash
# Provisions the Cloud Scheduler jobs that drive the cost-governance lifecycle
# (idle warning -> 48h grace period -> scheduled clearance) and the FinOps executive
# email digest. Each job calls the private Cloud Run service with an OIDC token.
#
# Usage:
#   PROJECT_ID=gcc-delta-tdl REGION=us-central1 ./deploy/setup_cloud_scheduler.sh
#
# Optional environment variables:
#   SERVICE_NAME       (default: finops-dashboard-mcp-app)
#   SERVICE_URL        (auto-discovered from Cloud Run if omitted)
#   SCHEDULER_SA_EMAIL (default: finops-dashboard-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com,
#                       or the Compute Engine default service account if that does not exist)
#   CLEARANCE_MODE     (default: dry_run; or enforced. Enforced also needs FINOPS_ALLOW_AUTOMATED_CLEARANCE=1
#                       on the service; the job response `note` explains any fallback to dry_run)
#   DIGEST_CADENCE     (default: daily at 08:30; or weekly, Mondays at 08:30)
#   DIGEST_RECIPIENTS  (default: the service's FINOPS_DIGEST_RECIPIENTS; comma-separated addresses)
#   TIME_ZONE          (default: America/Los_Angeles)
#
# The jobs only trigger the service. The service sends the email itself, through the
# Gmail API (FINOPS_GMAIL_*), SMTP (FINOPS_SMTP_HOST) or a webhook (FINOPS_ALERT_WEBHOOK_URL).
# With none of them configured, every run records a dry-run preview and sends nothing.
#
# Whoever runs this script needs roles/iam.serviceAccountUser on SCHEDULER_SA_EMAIL
# (to attach it to the jobs) and permission to edit the Cloud Run service's IAM policy.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
REGION="${REGION:-${GOOGLE_CLOUD_REGION:-us-central1}}"
SERVICE_NAME="${SERVICE_NAME:-finops-dashboard-mcp-app}"
CLEARANCE_MODE="${CLEARANCE_MODE:-dry_run}"
DIGEST_CADENCE="${DIGEST_CADENCE:-daily}"
TIME_ZONE="${TIME_ZONE:-America/Los_Angeles}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is required. Export PROJECT_ID=<gcp-project-id> and re-run." >&2
  exit 1
fi

case "${CLEARANCE_MODE}" in
  dry_run|enforced) ;;
  *)
    echo "ERROR: CLEARANCE_MODE must be dry_run or enforced (got '${CLEARANCE_MODE}')." >&2
    exit 1
    ;;
esac

case "${DIGEST_CADENCE}" in
  daily) DIGEST_SCHEDULE="30 8 * * *" ;;
  weekly) DIGEST_SCHEDULE="30 8 * * 1" ;;
  *)
    echo "ERROR: DIGEST_CADENCE must be daily or weekly (got '${DIGEST_CADENCE}')." >&2
    exit 1
    ;;
esac

DIGEST_BODY="{\"cadence\":\"${DIGEST_CADENCE}\",\"dry_run\":false}"
if [[ -n "${DIGEST_RECIPIENTS:-}" ]]; then
  # Plain addresses only, so the value is safe to embed in the JSON body. The service validates it again.
  RECIPIENTS_RE='^[A-Za-z0-9._%+@, -]+$'
  if [[ ! "${DIGEST_RECIPIENTS}" =~ ${RECIPIENTS_RE} ]]; then
    echo "ERROR: DIGEST_RECIPIENTS must be a comma-separated list of plain email addresses." >&2
    exit 1
  fi
  DIGEST_BODY="{\"cadence\":\"${DIGEST_CADENCE}\",\"dry_run\":false,\"recipient\":\"${DIGEST_RECIPIENTS}\"}"
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
if ! gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${SCHEDULER_SA_EMAIL}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null; then
  echo "WARNING: could not grant roles/run.invoker on ${SERVICE_NAME} to ${SCHEDULER_SA_EMAIL};" >&2
  echo "         the jobs below will get HTTP 403 from Cloud Run until that role is granted." >&2
fi

echo "==> Configuring Cloud Scheduler FinOps jobs in project=${PROJECT_ID} (${REGION})"
echo "    Target Cloud Run URL : ${SERVICE_URL}"
echo "    OIDC Service Account : ${SCHEDULER_SA_EMAIL}"
echo "    Clearance Mode       : ${CLEARANCE_MODE}"
echo "    Digest Cadence       : ${DIGEST_CADENCE} (${DIGEST_SCHEDULE}, ${TIME_ZONE})"

upsert_scheduler_job() {
  local job_name="$1"
  local schedule="$2"
  local uri="$3"
  local body="$4"
  local description="$5"
  local common_flags=(
    --project="${PROJECT_ID}"
    --location="${REGION}"
    --schedule="${schedule}"
    --time-zone="${TIME_ZONE}"
    --uri="${uri}"
    --http-method=POST
    --message-body="${body}"
    --oidc-service-account-email="${SCHEDULER_SA_EMAIL}"
    --oidc-token-audience="${SERVICE_URL}"
    --attempt-deadline=300s
    --description="${description}"
  )

  if gcloud scheduler jobs describe "${job_name}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" >/dev/null 2>&1; then
    echo "--> Updating existing Cloud Scheduler job: ${job_name}"
    # `jobs update http` takes --update-headers; --headers is only accepted by `jobs create http`.
    gcloud scheduler jobs update http "${job_name}" "${common_flags[@]}" \
      --update-headers="Content-Type=application/json"
  else
    echo "--> Creating Cloud Scheduler job: ${job_name}"
    gcloud scheduler jobs create http "${job_name}" "${common_flags[@]}" \
      --headers="Content-Type=application/json"
  fi
}

# 1. Stage 1: daily 08:00. Flag idle runtimes, attribute owners, send the 48h warning email.
upsert_scheduler_job \
  "finops-warn-idle-runtimes" \
  "0 8 * * *" \
  "${SERVICE_URL}/api/cron/warn_idle_runtimes" \
  '{"grace_period_hours":48,"send_warnings":true,"dry_run_email":false}' \
  "FinOps Stage 1: warn owners of idle Reasoning Engines and Cloud Run services (48h grace period)"

# 2. Stage 2: every 6 hours. Clear runtimes whose grace period expired without a snooze.
upsert_scheduler_job \
  "finops-execute-scheduled-clearance" \
  "0 */6 * * *" \
  "${SERVICE_URL}/api/cron/execute_scheduled_clearance" \
  "{\"mode\":\"${CLEARANCE_MODE}\"}" \
  "FinOps Stage 2: clear runtimes whose grace period expired without a snooze (mode=${CLEARANCE_MODE})"

# 3. Executive digest: top agents and users by token spend, WoW delta, cache-hit alerts, pending clearance.
upsert_scheduler_job \
  "finops-send-executive-digest" \
  "${DIGEST_SCHEDULE}" \
  "${SERVICE_URL}/api/cron/send_executive_digest" \
  "${DIGEST_BODY}" \
  "FinOps executive digest (${DIGEST_CADENCE}): top agents and users by token spend, WoW delta, cache alerts"

echo "==> All 3 Cloud Scheduler FinOps jobs configured."
echo "    Run one now: gcloud scheduler jobs run finops-send-executive-digest --project=${PROJECT_ID} --location=${REGION}"
echo "    Email is sent by the service only if it has FINOPS_GMAIL_*, FINOPS_SMTP_HOST or FINOPS_ALERT_WEBHOOK_URL set;"
echo "    otherwise each run records a dry-run preview (Cost Governance panel, recent_warnings)."
echo "    Keep-Alive links need the same FINOPS_GOVERNANCE_SECRET on every service that sends or serves them."
echo "    Setup guide: docs/COST_GOVERNANCE.md"
