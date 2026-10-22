#!/usr/bin/env bash
# Deploy FinOps Analytics Platform & ADK Agent to Google Cloud Run (private, IAM-authenticated).
#
# Safe to re-run: roles and bindings are idempotent, and env vars are merged (--update-env-vars), so
# variables you set on the service by hand (for example FINOPS_MONITORED_SERVICES) are kept.
set -euo pipefail

# `--source .` below uploads the repo root, so always run from there regardless of the caller's cwd.
cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-finops-dashboard-mcp-app}"

warn() { echo "WARNING: $*" >&2; }

# Post-deploy smoke test against a revision URL using the deployer's identity token.
# Returns 0 on pass, 1 on a failed check, 2 when no identity token is available (cannot test).
smoke_test_url() {
  local base="$1" surface="${2:-all}" token health tools n init
  token="$(gcloud auth print-identity-token 2>/dev/null || true)"
  if [[ -z "${token}" ]]; then
    warn "no identity token available (gcloud auth login); cannot smoke-test ${base}."
    return 2
  fi
  if [[ "${surface}" != "mcp" ]]; then
    health="$(curl -fsS -m 30 -H "Authorization: Bearer ${token}" "${base}/api/health" 2>/dev/null || true)"
    if [[ "${health}" != *'"status":"ok"'* ]]; then
      echo "SMOKE FAIL: ${base}/api/health -> ${health:0:300}" >&2
      return 1
    fi
  fi
  tools="$(curl -fsS -m 45 -X POST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' "${base}/mcp" 2>/dev/null || true)"
  n="$(printf '%s' "${tools}" | python3 -c 'import sys, json; d = json.load(sys.stdin); print(len(d.get("result", {}).get("tools", [])))' 2>/dev/null || echo 0)"
  if [[ "${n}" != "9" ]]; then
    echo "SMOKE FAIL: ${base}/mcp tools/list returned ${n} tools (expected 9): ${tools:0:300}" >&2
    return 1
  fi
  init="$(curl -fsS -m 45 -X POST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"deploy-smoke"}}}' \
      "${base}/mcp" 2>/dev/null || true)"
  if [[ "${init}" != *'"protocolVersion"'* ]]; then
    echo "SMOKE FAIL: ${base}/mcp initialize -> ${init:0:300}" >&2
    return 1
  fi
  echo "Smoke test passed on ${base}: /api/health ok, /mcp tools/list = 9, initialize ok."
  return 0
}

# Gate: never deploy a build that fails lint or the test suite. Tests run offline (no live API calls).
if [[ "${FINOPS_SKIP_TESTS:-0}" == "1" ]]; then
  warn "FINOPS_SKIP_TESTS=1: deploying WITHOUT running the test suite."
else
  echo "Running bytecode, lint, and test suite before deploy..."
  python3 -m compileall -q app finops_dashboard tests deploy/bigquery
  if command -v ruff &>/dev/null; then
    ruff check .
  elif [[ -x "${HOME}/.local/bin/ruff" ]]; then
    "${HOME}/.local/bin/ruff" check .
  fi
  if ! GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline-test-credentials.json \
      GOOGLE_CLOUD_PROJECT=test-project \
      python3 -m unittest discover -s tests -t . ; then
    echo "ERROR: tests failed; aborting deploy. Fix the failures (or set FINOPS_SKIP_TESTS=1 to override)." >&2
    exit 1
  fi
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <project-id>'." >&2
  exit 1
fi
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
# Deterministic Cloud Run URL. Published in the A2A agent card and returned by the ADK open_dashboard
# tool. Override by exporting FINOPS_PUBLIC_URL (for example when fronting the service with a domain).
PUBLIC_URL="${FINOPS_PUBLIC_URL:-https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app}"

echo "=========================================================="
echo " Deploying FinOps Dashboard Agent to Google Cloud Run"
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
SA_NAME="finops-dashboard-runtime-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Configuring dedicated Cloud Run Service Account: ${SA_EMAIL}..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "Creating service account ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
      --description="Dedicated runtime service account for FinOps Dashboard Agent and FinOps telemetry collector" \
      --display-name="FinOps Dashboard Runtime Service Account" \
      --project="${PROJECT_ID}"
else
  echo "Service account ${SA_NAME} already exists."
fi

# Gemini Enterprise fleet inventory: the assistants/*/agents list only returns agents created by the
# caller unless the caller also holds discoveryengine.agents.manage, which roles/discoveryengine.viewer
# does not include. This custom role adds just that permission to the read path, plus
# discoveryengine.agents.update, which the dashboard's trace logging buttons use to set an agent's
# observabilityConfig. The runtime SA does not need roles/discoveryengine.agentAdmin
# (no create/delete/setIamPolicy). The Antigravity seat panel also reads license configs and user
# assignments (licenseConfigs.list, userStores.listUserLicenses), which the viewer role lacks.
FLEET_ROLE_ID="finopsGeFleetReader"
FLEET_ROLE_PERMISSIONS="discoveryengine.engines.get,discoveryengine.assistants.list,discoveryengine.agents.list,discoveryengine.agents.get,discoveryengine.agents.manage,discoveryengine.agents.update,discoveryengine.licenseConfigs.list,discoveryengine.userStores.listUserLicenses"
FLEET_ROLE_DESCRIPTION="GE agent inventory and seat licenses (read) for the FinOps Dashboard runtime SA. agents.manage lets agents.list return agents created by other users; agents.update is used only to toggle trace logging. No create, delete or setIamPolicy."
FLEET_ROLE_STATE="$(gcloud iam roles describe "${FLEET_ROLE_ID}" --project="${PROJECT_ID}" --format='value(deleted)' 2>/dev/null || echo missing)"
if [[ "${FLEET_ROLE_STATE}" == "missing" ]]; then
  echo "Creating custom role ${FLEET_ROLE_ID}..."
  gcloud iam roles create "${FLEET_ROLE_ID}" \
      --project="${PROJECT_ID}" \
      --title="FinOps Dashboard GE Fleet Reader" \
      --description="${FLEET_ROLE_DESCRIPTION}" \
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
      --description="${FLEET_ROLE_DESCRIPTION}" \
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

# A service account created seconds ago can take up to a minute to become visible to IAM policy updates
# ("Service account ... does not exist"). Retry only that error, with backoff; other errors fail at once.
grant_sa_project_role() {
  local role="$1" out="" attempt
  for attempt in 1 2 3 4 5 6; do
    if out="$(gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${role}" \
        --condition=None \
        --quiet 2>&1 >/dev/null)"; then
      return 0
    fi
    [[ "${out}" == *"does not exist"* ]] || break
    echo "  service account not visible to IAM yet; retrying in $((attempt * 5))s..."
    sleep $((attempt * 5))
  done
  echo "${out}" >&2
  return 1
}

IAM_FAILURES=0
for ROLE in "${ROLES[@]}"; do
  echo "Assigning ${ROLE}..."
  if ! grant_sa_project_role "${ROLE}"; then
    warn "could not grant ${ROLE} to ${SA_EMAIL}. Dashboard sources that need it will report errors."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
done

# The runtime SA writes to two datasets, so it gets WRITER on each of them (not project-wide):
#   - finops_mart: the dashboard's "Refresh Mart" button (POST /api/ge_mart/refresh) runs
#     CREATE OR REPLACE TABLE finops_mart.fct_turns.
#   - finops_analytics: INSERTs into agent_registry_snapshots (fleet snapshots) and
#     zombie_clearance_queue (warning history and Keep-Alive snoozes shared by every instance).
grant_dataset_writer() {
  local dataset="$1"
  local purpose="$2"
  local acl_file
  if ! bq --project_id="${PROJECT_ID}" show --format=none "${PROJECT_ID}:${dataset}" &>/dev/null; then
    echo "Dataset ${dataset} not found; run deploy/setup_bigquery_sink.sh, then re-run this script"
    echo "so the runtime SA can write it (${purpose})."
    return 0
  fi
  echo "Granting WRITER (BigQuery Data Editor) on dataset ${dataset} to ${SA_EMAIL}..."
  # Dataset-level IAM (bq add-iam-policy-binding --dataset) needs allowlisting, so edit the dataset's
  # access list instead: WRITER on a dataset is equivalent to roles/bigquery.dataEditor on it.
  acl_file="$(mktemp)"
  if bq --project_id="${PROJECT_ID}" show --format=prettyjson "${PROJECT_ID}:${dataset}" > "${acl_file}" \
      && python3 - "${acl_file}" "${SA_EMAIL}" <<'PY' \
      && bq --project_id="${PROJECT_ID}" update --source "${acl_file}" "${PROJECT_ID}:${dataset}" > /dev/null; then
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
    warn "could not grant WRITER on ${dataset}; ${purpose} will fail."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
  rm -f "${acl_file}"
}

grant_dataset_writer "${FINOPS_GE_MART_DATASET:-finops_mart}" "the dashboard's Refresh Mart button"
grant_dataset_writer "${FINOPS_ANALYTICS_DATASET:-finops_analytics}" \
  "fleet snapshots and the scheduled clearance queue (warning history, Keep-Alive snoozes)"

# Deploy container directly from source to Cloud Run. Sizing matches the verified production service.
# "auto" discovers every Gemini Enterprise app in global/us/eu; set an explicit list to pin apps.
GE_ENGINES="${FINOPS_GE_ENGINES:-auto}"
# Optional: Cloud Billing BigQuery export (project.dataset.table) for real invoice figures.
# The runtime service account needs roles/bigquery.dataViewer on that dataset.
EXTRA_ENV=""
if [[ -n "${FINOPS_BILLING_EXPORT_TABLE:-}" ]]; then
  EXTRA_ENV=";FINOPS_BILLING_EXPORT_TABLE=${FINOPS_BILLING_EXPORT_TABLE}"
fi
# Optional: Cloud Pub/Sub Dead-Letter Queue topic for rejected telemetry ingest payloads (HTTP 422).
DLQ_TOPIC="${FINOPS_DLQ_PUBSUB_TOPIC:-projects/${PROJECT_ID}/topics/finops-ingest-dlq}"
if gcloud pubsub topics describe "${DLQ_TOPIC}" --project="${PROJECT_ID}" &>/dev/null; then
  EXTRA_ENV="${EXTRA_ENV};FINOPS_DLQ_PUBSUB_TOPIC=${DLQ_TOPIC}"
fi
# Optional: SHA-256 PII pseudonymization of principal_email when FINOPS_PII_REDACT=1
if [[ -n "${FINOPS_PII_REDACT:-}" ]]; then
  EXTRA_ENV="${EXTRA_ENV};FINOPS_PII_REDACT=${FINOPS_PII_REDACT}"
fi
# Optional: conversation logging for the Ambient Quality Agent (AQuA) through ADK's BigQuery Agent
# Analytics plugin. The table holds RAW USER PROMPTS and agent responses, so this is opt-in: set
# FINOPS_AGENT_ANALYTICS_DATASET to an existing dataset in this project. The plugin creates its table
# (FINOPS_AGENT_ANALYTICS_TABLE, default agent_events) on the first turn, and the runtime SA gets
# WRITER on that dataset only. To stop logging later, remove FINOPS_AGENT_ANALYTICS_DATASET from the
# service (gcloud run services update --remove-env-vars).
if [[ -n "${FINOPS_AGENT_ANALYTICS_DATASET:-}" ]]; then
  ANALYTICS_LOCATION="$(bq --project_id="${PROJECT_ID}" show --format=json "${PROJECT_ID}:${FINOPS_AGENT_ANALYTICS_DATASET}" 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("location", ""))' 2>/dev/null || true)"
  if [[ -n "${ANALYTICS_LOCATION}" ]]; then
    grant_dataset_writer "${FINOPS_AGENT_ANALYTICS_DATASET}" "conversation logging for AQuA"
    EXTRA_ENV="${EXTRA_ENV};FINOPS_AGENT_ANALYTICS_DATASET=${FINOPS_AGENT_ANALYTICS_DATASET}"
    EXTRA_ENV="${EXTRA_ENV};FINOPS_AGENT_ANALYTICS_TABLE=${FINOPS_AGENT_ANALYTICS_TABLE:-agent_events}"
    EXTRA_ENV="${EXTRA_ENV};FINOPS_AGENT_ANALYTICS_LOCATION=${ANALYTICS_LOCATION}"
  else
    warn "dataset ${FINOPS_AGENT_ANALYTICS_DATASET} not found in ${PROJECT_ID}; conversation logging stays off."
  fi
fi
# Optional: live mode for the Prompt Cache X-Ray (diff two logged turns instead of pasted prompts).
# The OTel prompt-log bucket holds RAW USER PROMPTS, so this is opt-in: set FINOPS_PROMPT_LOG_BUCKET
# (e.g. my-project-agent-logs, no gs://) to grant the runtime SA read access on that bucket only and
# enable FINOPS_PROMPT_XRAY_LIVE. Anyone who can open the dashboard can then view those prompts.
if [[ -n "${FINOPS_PROMPT_LOG_BUCKET:-}" ]]; then
  PROMPT_BUCKET="${FINOPS_PROMPT_LOG_BUCKET#gs://}"
  PROMPT_BUCKET="${PROMPT_BUCKET%%/*}"
  echo "Granting roles/storage.objectViewer on gs://${PROMPT_BUCKET} to ${SA_EMAIL} (Prompt X-Ray live mode)..."
  if gcloud storage buckets add-iam-policy-binding "gs://${PROMPT_BUCKET}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="roles/storage.objectViewer" \
      --project="${PROJECT_ID}" \
      --quiet > /dev/null; then
    EXTRA_ENV="${EXTRA_ENV};FINOPS_PROMPT_XRAY_LIVE=1"
  else
    warn "could not grant objectViewer on gs://${PROMPT_BUCKET}; Prompt X-Ray stays in paste-only mode."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
fi
# Optional: Stage 2 automated cost governance cleanup (scale idle Cloud Run min-instances=0 and delete
# idle Vertex AI Reasoning Engines after the grace period expires without a Keep-Alive snooze).
if [[ "${FINOPS_ALLOW_AUTOMATED_CLEARANCE:-0}" =~ ^(1|true|yes|on)$ ]]; then
  CLEARANCE_ROLE_ID="finopsIdleRuntimeCleaner"
  CLEARANCE_ROLE_PERMISSIONS="run.services.get,run.services.update,aiplatform.reasoningEngines.get,aiplatform.reasoningEngines.delete"
  CLEARANCE_ROLE_DESCRIPTION="Least-privilege Stage 2 cost-governance cleanup role for FinOps Dashboard runtime SA (scale Cloud Run min-instances=0 and delete idle Reasoning Engines)."
  CLEARANCE_ROLE_STATE="$(gcloud iam roles describe "${CLEARANCE_ROLE_ID}" --project="${PROJECT_ID}" --format='value(deleted)' 2>/dev/null || echo missing)"
  if [[ "${CLEARANCE_ROLE_STATE}" == "missing" ]]; then
    echo "Creating custom role ${CLEARANCE_ROLE_ID}..."
    gcloud iam roles create "${CLEARANCE_ROLE_ID}" \
        --project="${PROJECT_ID}" \
        --title="FinOps Idle Runtime Cleaner" \
        --description="${CLEARANCE_ROLE_DESCRIPTION}" \
        --permissions="${CLEARANCE_ROLE_PERMISSIONS}" \
        --stage=GA \
        --quiet > /dev/null
  else
    if [[ "${CLEARANCE_ROLE_STATE}" == "True" ]]; then
      gcloud iam roles undelete "${CLEARANCE_ROLE_ID}" --project="${PROJECT_ID}" --quiet > /dev/null
    fi
    gcloud iam roles update "${CLEARANCE_ROLE_ID}" \
        --project="${PROJECT_ID}" \
        --description="${CLEARANCE_ROLE_DESCRIPTION}" \
        --permissions="${CLEARANCE_ROLE_PERMISSIONS}" \
        --quiet > /dev/null
  fi
  echo "Granting projects/${PROJECT_ID}/roles/${CLEARANCE_ROLE_ID} to ${SA_EMAIL}..."
  if grant_sa_project_role "projects/${PROJECT_ID}/roles/${CLEARANCE_ROLE_ID}"; then
    EXTRA_ENV="${EXTRA_ENV};FINOPS_ALLOW_AUTOMATED_CLEARANCE=1"
  else
    warn "could not grant ${CLEARANCE_ROLE_ID} to ${SA_EMAIL}; Stage 2 clearance stays in dry-run mode."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
fi
for GOV_VAR in FINOPS_CLEARANCE_FORCE_DELETE FINOPS_CLEARANCE_EXCLUDE FINOPS_DEFAULT_OWNER_EMAIL FINOPS_SNOOZE_BASE_URL; do
  if [[ -n "${!GOV_VAR:-}" ]]; then
    EXTRA_ENV="${EXTRA_ENV};${GOV_VAR}=${!GOV_VAR}"
  fi
done
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

SPLIT_EDGE="${FINOPS_SPLIT_EDGE:-0}"
if [[ "${SPLIT_EDGE}" == "1" ]]; then
  SURFACE="${FINOPS_SURFACE:-mcp}"
else
  SURFACE="${FINOPS_SURFACE:-all}"
fi

echo "Building and deploying container to Cloud Run (${SERVICE_NAME}, surface=${SURFACE})..."
# Canary gate: an existing service gets the new revision with NO traffic under the 'candidate' tag; it is
# promoted to 100% only after the smoke test passes on its tag URL. A failed smoke test leaves traffic on
# the previous revision. FINOPS_SKIP_SMOKE=1 restores the direct (old) behaviour.
CANARY=0
if [[ "${FINOPS_SKIP_SMOKE:-0}" != "1" ]] && \
    gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" &>/dev/null; then
  CANARY=1
  echo "Canary mode: the new revision is deployed WITHOUT traffic and smoke-tested before promotion (FINOPS_SKIP_SMOKE=1 to disable)."
fi
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
    --no-cpu-throttling \
    --min-instances=1 \
    --max-instances=10 \
    $( [[ "${CANARY}" == "1" ]] && printf -- '--no-traffic --tag=candidate' ) \
    --update-env-vars="^;^GOOGLE_CLOUD_PROJECT=${PROJECT_ID};GOOGLE_CLOUD_REGION=${REGION};GOOGLE_CLOUD_LOCATION=global;GOOGLE_GENAI_USE_VERTEXAI=TRUE;USE_UVICORN=1;ENABLE_MCP_APP=1;FINOPS_SURFACE=${SURFACE};MCP_PROTOCOL_VERSION=2025-06-18;FINOPS_PUBLIC_URL=${PUBLIC_URL};FINOPS_GE_ENGINES=${GE_ENGINES};FINOPS_BQ_TOKEN_PUSHDOWN=true;FINOPS_BQ_SNAPSHOT_ENABLED=true${EXTRA_ENV}"

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

if [[ "${CANARY}" == "1" ]]; then
  CANDIDATE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format=json \
      | python3 -c 'import sys, json; d = json.load(sys.stdin); print(next((t.get("url") for t in d.get("status", {}).get("traffic", []) if t.get("tag") == "candidate" and t.get("url")), ""))' 2>/dev/null || true)"
  if [[ -z "${CANDIDATE_URL}" ]]; then
    warn "could not resolve the 'candidate' tag URL; promoting the new revision without a smoke test."
    gcloud run services update-traffic "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --to-latest --quiet >/dev/null
  else
    echo "Smoke-testing candidate revision at ${CANDIDATE_URL} ..."
    SMOKE_RC=0
    smoke_test_url "${CANDIDATE_URL}" "${SURFACE}" || SMOKE_RC=$?
    if [[ "${SMOKE_RC}" == "0" ]]; then
      echo "Promoting candidate revision to 100% of traffic..."
      gcloud run services update-traffic "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --to-latest --quiet >/dev/null
    elif [[ "${SMOKE_RC}" == "2" ]]; then
      warn "smoke test skipped (no identity token); promoting the new revision."
      gcloud run services update-traffic "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --to-latest --quiet >/dev/null
    else
      echo "ERROR: smoke test FAILED on the candidate revision. Traffic stays on the previous revision." >&2
      echo "  Candidate (0% traffic, tag 'candidate'): ${CANDIDATE_URL}" >&2
      echo "  Logs: gcloud run services logs read ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --limit 100" >&2
      exit 1
    fi
  fi
else
  SMOKE_RC=0
  smoke_test_url "${PUBLIC_URL}" "${SURFACE}" || SMOKE_RC=$?
  if [[ "${SMOKE_RC}" == "1" ]]; then
    warn "post-deploy smoke test did not pass on ${PUBLIC_URL}; this revision is already serving traffic (first deploy or FINOPS_SKIP_SMOKE=1)."
  fi
fi

# Record deployment_metadata.json for `agents-cli aqua publish-source`, and publish a source snapshot
# when AQuA is attached (.aqua/attached_aqua.json).
LATEST_REVISION="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
    --format='value(status.latestReadyRevisionName)' 2>/dev/null || true)"
if [[ -n "${LATEST_REVISION}" ]]; then
  python3 - "projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE_NAME}" "${SERVICE_NAME}" "${LATEST_REVISION}" <<'PY'
import json, sys
runtime_id, service_name, revision = sys.argv[1:4]
with open("deployment_metadata.json", "w", encoding="utf-8") as fh:
  json.dump({
      "remote_agent_runtime_id": runtime_id,
      "agent_directory": "app",
      "cloud_run_service": service_name,
      "cloud_run_revision": revision,
  }, fh, indent=2)
  fh.write("\n")
PY
  if [[ -f ".aqua/attached_aqua.json" ]] && command -v agents-cli &>/dev/null; then
    agents-cli aqua publish-source finops_agent --revision "${LATEST_REVISION}" || \
      warn "could not publish source snapshot for ${LATEST_REVISION} to AQuA."
  fi
fi

# Optional edge split: deploy a second Cloud Run service (finops-tokenomics-dashboard) for browser/API traffic
# behind Cloud Run native IAP (--iap), while keeping finops-dashboard-mcp-app on the *.run.app URL for Gemini
# Enterprise's X-Serverless-Authorization MCP calls. Reuses the image just built above.
DASHBOARD_SERVICE_NAME="${DASHBOARD_SERVICE_NAME:-finops-tokenomics-dashboard}"
DASHBOARD_PUBLIC_URL="https://${DASHBOARD_SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
if [[ "${SPLIT_EDGE}" == "1" ]]; then
  echo "Enabling Identity-Aware Proxy API for ${DASHBOARD_SERVICE_NAME}..."
  gcloud services enable iap.googleapis.com --project="${PROJECT_ID}"
  BUILT_IMAGE="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
      --format='value(spec.template.spec.containers[0].image)')"
  echo "Deploying split dashboard service ${DASHBOARD_SERVICE_NAME} (surface=dashboard, --iap)..."
  gcloud run deploy "${DASHBOARD_SERVICE_NAME}" \
      --image="${BUILT_IMAGE}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --service-account="${SA_EMAIL}" \
      --platform=managed \
      --no-allow-unauthenticated \
      --iap \
      --port=8080 \
      --memory=1Gi \
      --cpu=1 \
      --concurrency=80 \
      --timeout=300 \
      --cpu-boost \
      --no-cpu-throttling \
      --min-instances=1 \
      --max-instances=10 \
      --update-env-vars="^;^GOOGLE_CLOUD_PROJECT=${PROJECT_ID};GOOGLE_CLOUD_REGION=${REGION};GOOGLE_CLOUD_LOCATION=global;GOOGLE_GENAI_USE_VERTEXAI=TRUE;USE_UVICORN=1;ENABLE_MCP_APP=0;FINOPS_SURFACE=dashboard;FINOPS_PUBLIC_URL=${DASHBOARD_PUBLIC_URL};FINOPS_GE_ENGINES=${GE_ENGINES};FINOPS_BQ_TOKEN_PUSHDOWN=true;FINOPS_BQ_SNAPSHOT_ENABLED=true${EXTRA_ENV}"
  IAP_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
  gcloud beta services identity create --service=iap.googleapis.com --project="${PROJECT_ID}" &>/dev/null || true
  echo "Granting roles/run.invoker on ${DASHBOARD_SERVICE_NAME} to ${IAP_SERVICE_AGENT}..."
  if ! gcloud run services add-iam-policy-binding "${DASHBOARD_SERVICE_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --member="serviceAccount:${IAP_SERVICE_AGENT}" \
      --role="roles/run.invoker" \
      --quiet > /dev/null; then
    warn "could not grant roles/run.invoker to ${IAP_SERVICE_AGENT} on ${DASHBOARD_SERVICE_NAME}."
    IAM_FAILURES=$((IAM_FAILURES + 1))
  fi
fi

# Optional: let teammates open the private dashboard. Comma-separated IAM members, for example
#   FINOPS_INVOKERS="user:alice@example.com,group:finops@example.com"
INVOKER_TARGET_SERVICE="${SERVICE_NAME}"
if [[ "${SPLIT_EDGE}" == "1" ]]; then
  INVOKER_TARGET_SERVICE="${DASHBOARD_SERVICE_NAME}"
fi
if [[ -n "${FINOPS_INVOKERS:-}" ]]; then
  IFS=',' read -r -a INVOKERS <<< "${FINOPS_INVOKERS}"
  for MEMBER in "${INVOKERS[@]}"; do
    MEMBER="$(echo "${MEMBER}" | xargs)"
    [[ -z "${MEMBER}" ]] && continue
    echo "Granting roles/run.invoker on ${INVOKER_TARGET_SERVICE} to ${MEMBER}..."
    if ! gcloud run services add-iam-policy-binding "${INVOKER_TARGET_SERVICE}" \
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
if [[ "${SPLIT_EDGE}" == "1" ]]; then
  echo "FinOps Dashboard MCP Service (surface=mcp):           ${PUBLIC_URL}/mcp"
  echo "FinOps Dashboard Dashboard Service (surface=dashboard, IAP): ${DASHBOARD_PUBLIC_URL}/ui"
else
  echo "FinOps Dashboard Dashboard live at: ${PUBLIC_URL}"
  echo "BYO MCP App Endpoint:       ${PUBLIC_URL}/mcp"
  echo "Interactive MCP UI App:     ${PUBLIC_URL}/ui (or ui://finops-analytics/dashboard)"
fi
echo "Health check:               ${PUBLIC_URL}/api/health (/healthz is reserved by Cloud Run's front end)"
echo ""

# Optional: if GE_ENGINE_ID is provided, automatically deploy the Custom MCP Server to the
# Gemini Enterprise Data Store (BYO_MCP, NOT Agent Registry / MCP Registry) and register
# (link) that GE Data Store onto the Gemini Enterprise App instance.
if [[ -n "${GE_ENGINE_ID:-}" ]]; then
  echo "GE_ENGINE_ID=${GE_ENGINE_ID} set: deploying to Gemini Enterprise Data Store & registering to GE App instance..."
  GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" GOOGLE_CLOUD_REGION="${REGION}" SERVICE_NAME="${SERVICE_NAME}" \
      GE_LOCATION="${GE_LOCATION:-global}" ./deploy/register_ge_agent.sh "${GE_ENGINE_ID}"
fi

echo "The service is private. To open the dashboard from your machine:"
echo "  gcloud run services proxy ${SERVICE_NAME} --project=${PROJECT_ID} --region=${REGION} --port=8080"
echo "  then browse http://localhost:8080"
echo "Quick check with an identity token:"
echo "  curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${PUBLIC_URL}/api/health"
echo ""
echo "Next steps (see README 'Deploy in your own GCP project'):"
echo "  1. If not done yet: ./deploy/setup_bigquery_sink.sh, then re-run this script for the mart grant."
echo "  2. Deploy to Gemini Enterprise Data Store (NOT Agent Registry / MCP Registry) & register to your GE App instance:"
echo "       GE_LOCATION=global ./deploy/register_ge_agent.sh <GE_APP_ID>"
echo "     (Deploys the Custom MCP Server to GE Data Store 'finops-analytics-mcp' [source=BYO_MCP], enables"
echo "      all 9 MCP actions, and registers GE Data Store 'finops-analytics-mcp_mcp_data' onto GE App <GE_APP_ID>.)"
echo "  3. Hourly mart refresh (scheduled query): GOOGLE_CLOUD_PROJECT=${PROJECT_ID} ./deploy/setup_mart_refresh.sh"
if [[ "${IAM_FAILURES}" -gt 0 ]]; then
  warn "${IAM_FAILURES} IAM grant(s) failed; see the warnings above."
fi

