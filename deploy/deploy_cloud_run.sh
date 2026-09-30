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

# Gate: never deploy a build that fails lint or the test suite. Tests run offline (no live API calls).
if [[ "${VIBELIFT_SKIP_TESTS:-0}" == "1" ]]; then
  warn "VIBELIFT_SKIP_TESTS=1: deploying WITHOUT running the test suite."
else
  echo "Running bytecode, lint, and test suite before deploy..."
  python3 -m compileall -q app vibelift tests deploy/bigquery
  if command -v ruff &>/dev/null; then
    ruff check .
  elif [[ -x "${HOME}/.local/bin/ruff" ]]; then
    "${HOME}/.local/bin/ruff" check .
  fi
  if ! GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline-test-credentials.json \
      GOOGLE_CLOUD_PROJECT=test-project \
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
    # The ADK root agent and the LLM-as-judge audit call Gemini on Vertex AI (predict/generateContent).
    "roles/aiplatform.user"
    "roles/logging.viewer"
    "roles/monitoring.viewer"
    "roles/cloudtrace.user"
    "roles/run.viewer"
    # GKE cluster listing for the unregistered-workload discovery (read-only).
    "roles/container.clusterViewer"
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

# The dashboard's "Refresh Mart" button (POST /api/ge_mart/refresh) runs CREATE OR REPLACE TABLE
# vibelift_mart.fct_turns, so the runtime SA needs write access on that dataset only (not project-wide).
MART_DATASET="${VIBELIFT_GE_MART_DATASET:-vibelift_mart}"
if bq --project_id="${PROJECT_ID}" show --format=none "${PROJECT_ID}:${MART_DATASET}" &>/dev/null; then
  echo "Granting WRITER (BigQuery Data Editor) on dataset ${MART_DATASET} to ${SA_EMAIL}..."
  # Dataset-level IAM (bq add-iam-policy-binding --dataset) needs allowlisting, so edit the dataset's
  # access list instead: WRITER on a dataset is equivalent to roles/bigquery.dataEditor on it.
  ACL_FILE="$(mktemp)"
  if bq --project_id="${PROJECT_ID}" show --format=prettyjson "${PROJECT_ID}:${MART_DATASET}" > "${ACL_FILE}" \
      && python3 - "${ACL_FILE}" "${SA_EMAIL}" <<'PY' \
      && bq --project_id="${PROJECT_ID}" update --source "${ACL_FILE}" "${PROJECT_ID}:${MART_DATASET}" > /dev/null; then
import json, sys
path, email = sys.argv[1], sys.argv[2]
ds = json.load(open(path))
access = ds.get('access', [])
if not any(e.get('userByEmail') == email and e.get('role') in ('WRITER', 'OWNER') for e in access):
  access.append({'role': 'WRITER', 'userByEmail': email})
json.dump({'access': access}, open(path, 'w'))
PY
    :
  else
    warn "could not grant WRITER on ${MART_DATASET}; the dashboard's Refresh Mart button will fail."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
  rm -f "${ACL_FILE}"
else
  echo "Dataset ${MART_DATASET} not found; run deploy/setup_bigquery_sink.sh, then re-run this script"
  echo "so the runtime SA can refresh the mart from the dashboard."
fi

# Deploy container directly from source to Cloud Run. Sizing matches the verified production service.
# "auto" discovers every Gemini Enterprise app in global/us/eu; set an explicit list to pin apps.
GE_ENGINES="${VIBELIFT_GE_ENGINES:-auto}"
# Optional: Cloud Billing BigQuery export (project.dataset.table) for real invoice figures.
# The runtime service account needs roles/bigquery.dataViewer on that dataset.
EXTRA_ENV=""
if [[ -n "${VIBELIFT_BILLING_EXPORT_TABLE:-}" ]]; then
  EXTRA_ENV=";VIBELIFT_BILLING_EXPORT_TABLE=${VIBELIFT_BILLING_EXPORT_TABLE}"
fi
# `gcloud run deploy --source` builds with Cloud Build, which runs as the Compute Engine default service
# account. Orgs that enforce iam.automaticIamGrantsForDefaultServiceAccounts (Argolis, and every org
# created after May 3, 2024) give that account no roles, so the build fails unless it holds
# roles/run.builder (https://cloud.google.com/run/docs/deploying-source-code).
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${BUILD_SA}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "Enabling the Compute Engine API so the default service account used by Cloud Build exists..."
  gcloud services enable compute.googleapis.com --project="${PROJECT_ID}"
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    gcloud iam service-accounts describe "${BUILD_SA}" --project="${PROJECT_ID}" &>/dev/null && break
    sleep 5
  done
fi
HAS_BUILDER="$(gcloud projects get-iam-policy "${PROJECT_ID}" --flatten='bindings[].members' \
    --filter="bindings.role=roles/run.builder AND bindings.members=serviceAccount:${BUILD_SA}" \
    --format='value(bindings.role)' 2>/dev/null || true)"
if [[ -z "${HAS_BUILDER}" ]]; then
  echo "Granting roles/run.builder to the Cloud Build service account ${BUILD_SA}..."
  if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${BUILD_SA}" --role="roles/run.builder" \
      --condition=None --quiet >/dev/null; then
    echo "Waiting 60s for the new build permission to propagate..."
    sleep 60
  else
    warn "could not grant roles/run.builder to ${BUILD_SA}; the source build may fail with PERMISSION_DENIED."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
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
# The service agent is created lazily; make sure it exists before binding it (no-op when it does).
gcloud beta services identity create --service=discoveryengine.googleapis.com \
    --project="${PROJECT_ID}" &>/dev/null || true
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

# Optional: let teammates open the private dashboard. Comma-separated IAM members, for example
#   VIBELIFT_INVOKERS="user:alice@example.com,group:finops@example.com"
if [[ -n "${VIBELIFT_INVOKERS:-}" ]]; then
  IFS=',' read -r -a INVOKERS <<< "${VIBELIFT_INVOKERS}"
  for MEMBER in "${INVOKERS[@]}"; do
    MEMBER="$(echo "${MEMBER}" | xargs)"
    [[ -z "${MEMBER}" ]] && continue
    echo "Granting roles/run.invoker on ${SERVICE_NAME} to ${MEMBER}..."
    if ! gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --member="${MEMBER}" \
        --role="roles/run.invoker" \
        --quiet > /dev/null; then
      warn "could not grant roles/run.invoker to ${MEMBER}."
      IAM_FAILURES=$((IAM_FAILURES + 1))
    fi
  done
fi

echo "Deployment complete!"
echo "VibeLift Dashboard live at: ${PUBLIC_URL}"
echo "BYO MCP App Endpoint:       ${PUBLIC_URL}/mcp"
echo "Interactive MCP UI App:     ${PUBLIC_URL}/ui (or ui://vibelift-analytics/dashboard)"
echo "Health check:               ${PUBLIC_URL}/health (/healthz is reserved by Cloud Run's front end)"
echo ""
echo "The service is private. To open the dashboard from your machine:"
echo "  gcloud run services proxy ${SERVICE_NAME} --project=${PROJECT_ID} --region=${REGION} --port=8080"
echo "  then browse http://localhost:8080"
echo "Quick check with an identity token:"
echo "  curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${PUBLIC_URL}/health"
echo ""
echo "Next steps (see README 'Deploy in your own GCP project'):"
echo "  1. If not done yet: ./deploy/setup_bigquery_sink.sh, then re-run this script for the mart grant."
echo "  2. Register in Gemini Enterprise: ./deploy/register_ge_agent.sh <GE_APP_ID>"
echo "  3. Hourly mart refresh (scheduled query): GOOGLE_CLOUD_PROJECT=${PROJECT_ID} ./deploy/setup_mart_refresh.sh"
if [[ "${IAM_FAILURES}" -gt 0 ]]; then
  warn "${IAM_FAILURES} IAM grant(s) failed; see the warnings above."
fi
