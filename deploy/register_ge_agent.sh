#!/usr/bin/env bash
# register_ge_agent.sh — Deploy VibeLift to a Gemini Enterprise Data Store & Register to a GE App Instance
#
# IMPORTANT: VibeLift's MCP server (/mcp) and side-panel UI (ui://vibelift-analytics/dashboard) must be
# deployed as a **Gemini Enterprise Custom MCP Server Data Store** (mcp_server_source="BYO_MCP") —
# NOT in the Vertex AI / Cloud API Registry ("Agent Registry / MCP Registry").
# Once deployed to the Gemini Enterprise Data Store, the data store (vibelift-analytics-mcp_mcp_data)
# must be **registered (linked via dataStoreIds) to your Gemini Enterprise App instance (<GE_APP_ID>)**.
#
# Connects the deployed VibeLift Cloud Run service to a Gemini Enterprise app instance:
#   1. Authenticated health check of the private Cloud Run service
#   2. Discovery Engine service agent run.invoker binding (so GE can call /mcp and the A2A card)
#   3. Deploys Custom MCP Server to the Gemini Enterprise Data Store (vibelift-analytics-mcp, BYO_MCP),
#      enables all 9 MCP actions, and registers (links) the GE Data Store (vibelift-analytics-mcp_mcp_data)
#      to the Gemini Enterprise App instance (<GE_APP_ID>) via deploy/setup_mcp_connector.py
#   4. Registers the A2A agent on the Gemini Enterprise App instance (if agents-cli is installed)
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
echo " Deploying VibeLift to GE Data Store & Registering to GE App"
echo " Project:       ${PROJECT_ID} (${PROJECT_NUMBER})"
echo " Region:        ${REGION}"
echo " Service URL:   ${PUBLIC_URL}"
echo " MCP Endpoint:  ${MCP_URL} (Target: Gemini Enterprise Data Store, BYO_MCP)"
echo " A2A Card URL:  ${CARD_URL}"
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

# 3. Deploy the Custom MCP Server to a Gemini Enterprise Data Store (NOT Agent Registry / MCP Registry),
#    enable all 9 MCP actions, and register (link) the GE Data Store to the Gemini Enterprise App instance.
#    Official docs: https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server
echo ""
echo "--- Step 3: Deploying to Gemini Enterprise Data Store & Registering to GE App Instance ---"
echo "IMPORTANT: VibeLift deploys as a Gemini Enterprise Custom MCP Server Data Store (source=BYO_MCP),"
echo "           NOT in the Agent Registry / MCP Registry."
echo "           Once the GE Data Store (vibelift-analytics-mcp_mcp_data) is ACTIVE and its 9 actions are enabled,"
echo "           it is registered (linked via dataStoreIds) to Gemini Enterprise App instance '${GE_ENGINE_ID}'."

if [[ "${VIBELIFT_SKIP_MCP_CONNECTOR:-0}" == "1" ]]; then
  echo "VIBELIFT_SKIP_MCP_CONNECTOR=1: skipping automated setup_mcp_connector.py run."
elif GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" GOOGLE_CLOUD_REGION="${REGION}" SERVICE_NAME="${SERVICE_NAME}" \
    GE_LOCATION="${GE_LOCATION}" python3 deploy/setup_mcp_connector.py --location="${GE_LOCATION}" "${GE_ENGINE_ID}"; then
  echo "Successfully deployed to Gemini Enterprise Data Store (vibelift-analytics-mcp_mcp_data) and registered to GE App instance (${GE_ENGINE_ID})."
else
  echo "" >&2
  echo "WARNING: scripted GE Data Store deployment / app registration failed (see error above)." >&2
  echo "         Use the two-stage Console fallback below (do NOT use Agent Registry / MCP Registry):" >&2
  echo "" >&2
  echo "  Stage 3a (Console) — Deploy to Gemini Enterprise Data Store (NOT Agent Registry / MCP Registry):" >&2
  echo "    - Org policy (if blocked): IAM & Admin > Organization Policies > 'Disable custom MCP server" >&2
  echo "      connector for Gemini Enterprise' > Manage policy > Override parent's policy > Enforcement OFF." >&2
  echo "    - Role required: Discovery Engine Editor (roles/discoveryengine.editor)." >&2
  echo "    - Open: Console > Gemini Enterprise > Data stores > Create data store > search 'Custom MCP Server'" >&2
  echo "      (choose Bring Your Own MCP Server URL — do NOT select Agent Registry / MCP Registry):" >&2
  echo "        * Authentication:  No authentication (Discovery Engine SA uses X-Serverless-Authorization)" >&2
  echo "        * MCP Server URL:  ${MCP_URL}" >&2
  echo "        * Location:        ${GE_LOCATION} (must match your GE App instance location)" >&2
  echo "        * Data store name: vibelift-analytics-mcp" >&2
  echo "    - Once Active: open the data store > Actions > Reload custom actions > select all 9 actions > Enable actions." >&2
  echo "" >&2
  echo "  Stage 3b (Console) — Register the GE Data Store to the Gemini Enterprise App Instance:" >&2
  echo "    - Open: Console > Gemini Enterprise > Apps > ${GE_ENGINE_ID} > Data stores (Connected data stores)" >&2
  echo "    - Click 'Connect existing data store' (or 'Edit data stores'), select 'VibeLift Analytics'" >&2
  echo "      (vibelift-analytics-mcp_mcp_data), and click Save." >&2
fi

# 4. Optional: Register the A2A agent on the Gemini Enterprise App instance (not Agent Registry).
echo ""
echo "--- Step 4: Registering A2A Agent on Gemini Enterprise App Instance (Optional) ---"
echo "Needs the Gemini Enterprise Admin role. No OAuth authorization is attached: VibeLift reads telemetry with"
echo "its own runtime service account, not the end user's token (in the console, choose 'Skip & Finish')."
GE_APP_RESOURCE="projects/${PROJECT_NUMBER}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}"

# The agent card is served, but the A2A JSON-RPC endpoint it names (/a2a/app) is not: app/fast_api_app.py
# builds the ADK app without A2A. Publishing is therefore opt-in; Gemini Enterprise uses the MCP data store.
if [[ "${VIBELIFT_PUBLISH_A2A:-0}" != "1" ]]; then
  echo "Skipped (set VIBELIFT_PUBLISH_A2A=1 to publish). The A2A endpoint named in the agent card (/a2a/app) is not"
  echo "served by this build, so a published A2A agent could not answer. The MCP data store from Step 3 provides"
  echo "all 9 MCP tools and the side-panel dashboard in app ${GE_ENGINE_ID}."
elif command -v agents-cli &>/dev/null; then
  echo "Using agents-cli to publish the A2A agent to ${GE_APP_RESOURCE}..."
  if ! agents-cli publish gemini-enterprise \
      --registration-type a2a \
      --agent-card-url "${CARD_URL}" \
      --gemini-enterprise-app-id "${GE_APP_RESOURCE}" \
      --display-name "VibeLift Analytics & FinOps" \
      --description "Gemini Enterprise agent fleet observability, prompt cache FinOps, and an optimizer simulator"; then
    echo "WARNING: agents-cli publish failed. Re-run it by hand to see the error, or register the agent in" >&2
    echo "         Console > Gemini Enterprise > Apps > ${GE_ENGINE_ID} > Agents using the card URL above." >&2
  fi
else
  echo "agents-cli not found in PATH (optional). The Gemini Enterprise Custom MCP Server Data Store registered in"
  echo "Step 3 already provides all 9 MCP tools and the side-panel dashboard in app ${GE_ENGINE_ID}."
  echo "To also add the standalone A2A agent in the console: Console > Gemini Enterprise > Apps > ${GE_ENGINE_ID} > Agents."
fi

echo ""
echo "Exposed MCP tools in GE Data Store (vibelift-analytics-mcp_mcp_data -> app ${GE_ENGINE_ID}):"
echo "  - open_dashboard (opens Right Side Panel with Fullscreen toggle)"
echo "  - query_ge_agent_fleet (live multi-engine agent inventory and telemetry)"
echo "  - query_project_telemetry (service requests, errors, and Cloud Run stats)"
echo "  - calculate_prompt_cache_economics (prefix cache hit ratios and savings)"
echo "  - run_alpha_evolve_generation (optimizer simulator: synthetic numbers, nothing deployed)"
echo "  - get_vibelift_state (runtime state, parameters, and turns)"
echo ""
echo "Smoke test of the MCP endpoint with your identity token:"
echo "  curl -s -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \\"
echo "    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' ${MCP_URL}"
echo ""
echo "=========================================================="
echo " VibeLift Gemini Enterprise registration steps finished"
echo " GE Data Store: vibelift-analytics-mcp_mcp_data (${GE_LOCATION}, BYO_MCP)"
echo " GE App Linked: ${GE_ENGINE_ID} (${GE_LOCATION})"
echo " Dashboard:     ${PUBLIC_URL}/ui"
echo " MCP App:       ui://vibelift-analytics/dashboard"
echo "=========================================================="

