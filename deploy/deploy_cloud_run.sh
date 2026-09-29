#!/usr/bin/env bash
# Deploy VibeLift Analytics Platform & ADK Agent to Google Cloud Run (private, IAM-authenticated).
#
# Safe to re-run: roles and bindings are idempotent, and env vars are merged (--update-env-vars), so
# variables you set on the service by hand (for example VIBELIFT_MONITORED_SERVICES) are kept.
set -euo pipefail

# `--source .` below uploads the repo root, so always run from there regardless of the caller's cwd.
cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-vibe-lift-agent}"

warn() { echo "WARNING: $*" >&2; }

# Gate: never deploy a build that fails the test suite. Tests run offline (no live API calls).
if [[ "${VIBELIFT_SKIP_TESTS:-0}" == "1" ]]; then
  warn "VIBELIFT_SKIP_TESTS=1: deploying WITHOUT running the test suite."
else
  echo "Running test suite before deploy..."
  if ! GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline-test-credentials.json \
      python3 -m unittest discover -s tests -t . ; then
    echo "ERROR: tests failed; aborting deploy. Fix the failures (or set VIBELIFT_SKIP_TESTS=1 to override)." >&2
    exit 1
  fi
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <project-id>'." >&2
  exit 1
fi
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
# Deterministic Cloud Run URL. Published in the A2A agent card and returned by the ADK open_dashboard
# tool. Override by exporting VIBELIFT_PUBLIC_URL (for example when fronting the service with a domain).
PUBLIC_URL="${VIBELIFT_PUBLIC_URL:-https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app}"

echo "=========================================================="
echo " Deploying VibeLift Agent to Google Cloud Run"
echo " Project:  ${PROJECT_ID} (${PROJECT_NUMBER})"
echo " Region:   ${REGION}"
echo " Service:  ${SERVICE_NAME}"
echo " URL:      ${PUBLIC_URL}"
echo "=========================================================="

# Ensure required APIs are enabled
echo "Enabling necessary Google Cloud APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    discoveryengine.googleapis.com \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    --project="${PROJECT_ID}"

# Configure Dedicated Least-Privilege Service Account for Cloud Run
SA_NAME="vibe-lift-runtime-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Configuring dedicated Cloud Run Service Account: ${SA_EMAIL}..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "Creating service account ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
      --description="Dedicated runtime service account for VibeLift Agent and FinOps telemetry collector" \
      --display-name="VibeLift Runtime Service Account" \
      --project="${PROJECT_ID}"
else
  echo "Service account ${SA_NAME} already exists."
fi

# Gemini Enterprise fleet inventory: the assistants/*/agents list only returns agents created by the
# caller unless the caller also holds discoveryengine.agents.manage, which roles/discoveryengine.viewer
# does not include. This custom role adds just that permission to the read path, so the runtime SA
# does not need roles/discoveryengine.agentAdmin (no create/update/delete/setIamPolicy).
FLEET_ROLE_ID="vibeLiftGeFleetReader"
FLEET_ROLE_PERMISSIONS="discoveryengine.engines.get,discoveryengine.assistants.list,discoveryengine.agents.list,discoveryengine.agents.get,discoveryengine.agents.manage"
FLEET_ROLE_STATE="$(gcloud iam roles describe "${FLEET_ROLE_ID}" --project="${PROJECT_ID}" --format='value(deleted)' 2>/dev/null || echo missing)"
if [[ "${FLEET_ROLE_STATE}" == "missing" ]]; then
  echo "Creating custom role ${FLEET_ROLE_ID}..."
  gcloud iam roles create "${FLEET_ROLE_ID}" \
      --project="${PROJECT_ID}" \
      --title="VibeLift GE Fleet Reader" \
      --description="Least-privilege Gemini Enterprise agent inventory for the VibeLift dashboard runtime SA. agents.manage is required for agents.list to return agents created by other users. No create, update, delete or setIamPolicy." \
      --permissions="${FLEET_ROLE_PERMISSIONS}" \
      --stage=GA \
      --quiet > /dev/null
else
  if [[ "${FLEET_ROLE_STATE}" == "True" ]]; then
    echo "Restoring soft-deleted custom role ${FLEET_ROLE_ID}..."
    gcloud iam roles undelete "${FLEET_ROLE_ID}" --project="${PROJECT_ID}" --quiet > /dev/null
  fi
  echo "Updating custom role ${FLEET_ROLE_ID}..."
  gcloud iam roles update "${FLEET_ROLE_ID}" \
      --project="${PROJECT_ID}" \
      --permissions="${FLEET_ROLE_PERMISSIONS}" \
      --quiet > /dev/null
fi

echo "Granting least-privilege IAM roles to ${SA_EMAIL}..."
ROLES=(
    "roles/discoveryengine.viewer"
    "projects/${PROJECT_ID}/roles/${FLEET_ROLE_ID}"
    "roles/aiplatform.viewer"
    "roles/logging.viewer"
    "roles/monitoring.viewer"
    "roles/cloudtrace.user"
    "roles/run.viewer"
    "roles/bigquery.dataViewer"
    "roles/bigquery.jobUser"
)

IAM_FAILURES=0
for ROLE in "${ROLES[@]}"; do
  echo "Assigning ${ROLE}..."
  if ! gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="${ROLE}" \
      --condition=None \
      --quiet > /dev/null; then
    warn "could not grant ${ROLE} to ${SA_EMAIL}. Dashboard sources that need it will report errors."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
done

# Deploy container directly from source to Cloud Run. Sizing matches the verified production service.
# "auto" discovers every Gemini Enterprise app in global/us/eu; set an explicit list to pin apps.
GE_ENGINES="${VIBELIFT_GE_ENGINES:-auto}"
# Optional: Cloud Billing BigQuery export (project.dataset.table) for real invoice figures.
# The runtime service account needs roles/bigquery.dataViewer on that dataset.
EXTRA_ENV=""
if [[ -n "${VIBELIFT_BILLING_EXPORT_TABLE:-}" ]]; then
  EXTRA_ENV=";VIBELIFT_BILLING_EXPORT_TABLE=${VIBELIFT_BILLING_EXPORT_TABLE}"
fi
echo "Building and deploying container to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --platform=managed \
    --no-allow-unauthenticated \
    --port=8080 \
    --memory=1Gi \
    --cpu=1 \
    --concurrency=80 \
    --timeout=300 \
    --cpu-boost \
    --min-instances=1 \
    --max-instances=10 \
    --update-env-vars="^;^GOOGLE_CLOUD_PROJECT=${PROJECT_ID};GOOGLE_CLOUD_REGION=${REGION};GOOGLE_CLOUD_LOCATION=${REGION};GOOGLE_GENAI_USE_VERTEXAI=TRUE;USE_UVICORN=1;ENABLE_MCP_APP=1;MCP_PROTOCOL_VERSION=2025-06-18;VIBELIFT_PUBLIC_URL=${PUBLIC_URL};VIBELIFT_GE_ENGINES=${GE_ENGINES}${EXTRA_ENV}"

# The service is private, so Gemini Enterprise needs permission to call /mcp. The Discovery Engine
# service agent is the identity granted roles/run.invoker on the verified deployment.
DE_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
echo "Granting roles/run.invoker on ${SERVICE_NAME} to ${DE_SERVICE_AGENT}..."
if ! gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${DE_SERVICE_AGENT}" \
    --role="roles/run.invoker" \
    --quiet > /dev/null; then
  warn "could not grant roles/run.invoker to ${DE_SERVICE_AGENT}; Gemini Enterprise will get HTTP 403 from /mcp."
  warn "If the service agent does not exist yet, create it and re-run this script:"
  warn "  gcloud beta services identity create --service=discoveryengine.googleapis.com --project=${PROJECT_ID}"
  IAM_FAILURES=$((IAM_FAILURES + 1))
fi

echo "Deployment complete!"
echo "VibeLift Dashboard live at: ${PUBLIC_URL}"
echo "BYO MCP App Endpoint:       ${PUBLIC_URL}/mcp"
echo "Interactive MCP UI App:     ${PUBLIC_URL}/ui (or ui://vibelift-analytics/dashboard)"
echo "Health check:               ${PUBLIC_URL}/health (/healthz is reserved by Cloud Run's front end)"
if [[ "${IAM_FAILURES}" -gt 0 ]]; then
  warn "${IAM_FAILURES} IAM grant(s) failed; see the warnings above."
fi
