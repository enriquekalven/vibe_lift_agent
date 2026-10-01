#!/usr/bin/env bash
# register_ge_agent.sh — Register VibeLift Agent and MCP Server in Gemini Enterprise
#
# Connects the deployed VibeLift Cloud Run service to a Gemini Enterprise app:
#   1. Authenticated health check of the private Cloud Run service
#   2. Discovery Engine service agent run.invoker binding (so GE can call /mcp and the A2A card)
#   3. A2A agent registration via agents-cli
#   4. Printed values for attaching the BYO MCP connector in the console
#
# Usage:
#   ./deploy/register_ge_agent.sh <GE_APP_ID>        # the engine ID of your Gemini Enterprise app
#   GE_LOCATION=us ./deploy/register_ge_agent.sh <GE_APP_ID>
# Find the app ID in Console > Gemini Enterprise > Apps (the "ID" column), or with:
#   curl -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "X-Goog-User-Project: PROJECT_ID" \
#     "https://discoveryengine.googleapis.com/v1/projects/PROJECT_ID/locations/global/collections/default_collection/engines"
# (for GE_LOCATION=us or eu use https://us-discoveryengine.googleapis.com / https://eu-discoveryengine.googleapis.com)
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-vibe-lift-agent}"
GE_ENGINE_ID="${1:-${GE_ENGINE_ID:-}}"
GE_LOCATION="${GE_LOCATION:-global}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT or run gcloud config set project <PROJECT_ID>." >&2
  exit 1
fi
if [[ -z "${GE_ENGINE_ID}" ]]; then
  echo "ERROR: pass your Gemini Enterprise app ID: ./deploy/register_ge_agent.sh <GE_APP_ID>" >&2
  echo "       (or export GE_ENGINE_ID). See the header of this script for how to find it." >&2
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
PUBLIC_URL="${VIBELIFT_PUBLIC_URL:-https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app}"
CARD_URL="${PUBLIC_URL}/a2a/app/.well-known/agent-card.json"
MCP_URL="${PUBLIC_URL}/mcp"

echo "=========================================================="
echo " Registering VibeLift in Gemini Enterprise"
echo " Project:       ${PROJECT_ID} (${PROJECT_NUMBER})"
echo " Region:        ${REGION}"
echo " Service URL:   ${PUBLIC_URL}"
echo " A2A Card URL:  ${CARD_URL}"
echo " MCP Endpoint:  ${MCP_URL}"
echo " GE App Engine: ${GE_ENGINE_ID} (Location: ${GE_LOCATION})"
echo "=========================================================="

# 1. The service is private (--no-allow-unauthenticated), so check it with your identity token.
echo ""
echo "--- Step 1: Validating Cloud Run Service Health ---"
ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -n "${ID_TOKEN}" ]] && curl -fsS -m 15 -H "Authorization: Bearer ${ID_TOKEN}" "${PUBLIC_URL}/health" >/dev/null 2>&1; then
  echo "Cloud Run service is online and healthy at ${PUBLIC_URL}."
else
  echo "WARNING: authenticated health check failed. Check that ./deploy/deploy_cloud_run.sh finished and" >&2
  echo "         that your account has roles/run.invoker on ${SERVICE_NAME} (Owners/Run Admins do)." >&2
fi

# 2. Gemini Enterprise calls the private service as the Discovery Engine service agent.
echo ""
echo "--- Step 2: Verifying Discovery Engine IAM Invoker ---"
DE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
echo "Ensuring ${DE_SA} has roles/run.invoker on ${SERVICE_NAME}..."
gcloud beta services identity create --service=discoveryengine.googleapis.com \
    --project="${PROJECT_ID}" &>/dev/null || true
if ! gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${DE_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null; then
  echo "WARNING: could not grant run.invoker. If the service agent does not exist yet, create it and re-run:" >&2
  echo "  gcloud beta services identity create --service=discoveryengine.googleapis.com --project=${PROJECT_ID}" >&2
fi

# 3. Register the A2A agent (the Cloud Run service publishes its card at /a2a/app/.well-known/agent-card.json).
echo ""
echo "--- Step 3: Registering A2A Agent with Gemini Enterprise ---"
echo "Needs the Gemini Enterprise Admin role. No OAuth authorization is attached: VibeLift reads telemetry with"
echo "its own runtime service account, not the end user's token (in the console, choose 'Skip & Finish')."
GE_APP_RESOURCE="projects/${PROJECT_NUMBER}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}"

if command -v agents-cli &>/dev/null; then
  echo "Using agents-cli to publish the A2A agent to ${GE_APP_RESOURCE}..."
  if ! agents-cli publish gemini-enterprise \
      --registration-type a2a \
      --agent-card-url "${CARD_URL}" \
      --gemini-enterprise-app-id "${GE_APP_RESOURCE}" \
      --display-name "VibeLift Analytics & FinOps" \
      --description "Observability, Prompt Cache FinOps, and Autonomous Multi-Objective Agent Fleet Optimization Studio"; then
    echo "WARNING: agents-cli publish failed. Re-run it by hand to see the error, or register the agent in" >&2
    echo "         Console > Gemini Enterprise > Apps > ${GE_ENGINE_ID} > Agents using the card URL above." >&2
  fi
else
  echo "agents-cli not found in PATH. Install it (see README prerequisites) or register the agent in"
  echo "Console > Gemini Enterprise > Apps > ${GE_ENGINE_ID} > Agents using the card URL above."
fi

# 4. The custom MCP server (side-panel dashboard) is a Gemini Enterprise *data store*, created in the console.
#    Official steps: https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server
echo ""
echo "--- Step 4: Custom MCP server data store ---"
echo "Scripted (needs Discovery Engine Editor; uses undocumented v1alpha methods):"
echo "  GE_LOCATION=${GE_LOCATION} python3 deploy/setup_mcp_connector.py ${GE_ENGINE_ID}   # add --dry-run to preview"
echo ""
echo "Console fallback. Before you start (once per project):"
echo "  - Org policy: some orgs block custom MCP servers with 'Disable custom MCP server connector for"
echo "    Gemini Enterprise'. If creation fails with a policy error, an Organization Policy Administrator"
echo "    turns it off for this project: Console > IAM & Admin > Organization Policies > filter that name >"
echo "    Manage policy > Override parent's policy > rule OFF."
echo "  - Your account needs Discovery Engine Editor (roles/discoveryengine.editor)."
echo "Then: Console > Gemini Enterprise > Data stores > Create data store > search 'Custom MCP Server' >"
echo "Add MCP server, and use:"
echo "     - Authentication:  No authentication  (no OAuth client needed; see below)"
echo "     - MCP Server URL:  ${MCP_URL}"
echo "     - Location:        the same multi-region as your app (${GE_LOCATION})"
echo "     - Data store name: vibelift-analytics-mcp"
echo "  After it shows Active: open it > Actions > Reload custom actions > select the actions > Enable actions,"
echo "  and connect the data store to app ${GE_ENGINE_ID} if it is not connected yet."
echo "  Auth: the service stays private. For default *.run.app URLs Gemini Enterprise sends a Google-signed"
echo "  ID token for ${DE_SA} (X-Serverless-Authorization), which Step 2 granted roles/run.invoker."
echo "  This does not work with a custom domain. Do not make the service public."
echo "  Read-only tools declare readOnlyHint, so they run without a confirmation prompt;"
echo "  run_alpha_evolve_generation changes state and asks the user to confirm."
echo "  Tools exposed by the server:"
echo "     - open_dashboard (opens Right Side Panel with Fullscreen toggle)"
echo "     - query_ge_agent_fleet (live multi-engine agent inventory and telemetry)"
echo "     - query_project_telemetry (service requests, errors, and Cloud Run stats)"
echo "     - calculate_prompt_cache_economics (prefix cache hit ratios and savings)"
echo "     - run_alpha_evolve_generation (closed-loop Pareto optimization)"
echo "     - get_vibelift_state (runtime state, parameters, and turns)"
echo ""
echo "Smoke test of the MCP endpoint with your identity token:"
echo "  curl -s -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \\"
echo "    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' ${MCP_URL}"
echo ""
echo "=========================================================="
echo " VibeLift Gemini Enterprise registration steps finished"
echo " Dashboard: ${PUBLIC_URL}/ui"
echo " MCP App:   ui://vibelift-analytics/dashboard"
echo "=========================================================="
