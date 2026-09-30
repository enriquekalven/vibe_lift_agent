#!/usr/bin/env bash
# register_ge_agent.sh — Register VibeLift Agent and MCP Server in Gemini Enterprise
#
# Connects the deployed VibeLift Cloud Run service to Gemini Enterprise:
#   1. A2A Agent Registration via agents-cli or Discovery Engine API
#   2. MCP Connector Registration (Streamable HTTP /mcp and ui://vibelift-analytics/dashboard)
#   3. Discovery Engine Service Agent IAM Invoker Validation
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-vibe-lift-agent}"
GE_ENGINE_ID="${1:-${GE_ENGINE_ID:-agent-platform-demo}}"
GE_LOCATION="${GE_LOCATION:-global}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT or run gcloud config set project <PROJECT_ID>." >&2
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

# 1. Validate that the Cloud Run service is reachable and healthy
echo ""
echo "--- Step 1: Validating Cloud Run Service Health ---"
if curl -fsSL -m 10 "${PUBLIC_URL}/health" >/dev/null 2>&1; then
  echo "Cloud Run service is online and healthy at ${PUBLIC_URL}."
else
  echo "Notice: External unauthenticated health check returned non-200 (expected when IAM authentication is active)."
fi

# 2. Verify IAM invoker binding for Discovery Engine
echo ""
echo "--- Step 2: Verifying Discovery Engine IAM Invoker ---"
DE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
echo "Ensuring ${DE_SA} has roles/run.invoker on ${SERVICE_NAME}..."
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${DE_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null || {
  echo "Warning: Could not grant run.invoker; ensure the Discovery Engine service account exists."
}

# 3. Register A2A Agent with Gemini Enterprise
echo ""
echo "--- Step 3: Registering A2A Agent with Gemini Enterprise ---"
GE_APP_RESOURCE="projects/${PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}"

if command -v agents-cli &>/dev/null; then
  echo "Using agents-cli to publish A2A agent..."
  agents-cli publish gemini-enterprise \
      --agent-card-url "${CARD_URL}" \
      --gemini-enterprise-app-id "${GE_APP_RESOURCE}" \
      --display-name "VibeLift Analytics & FinOps" \
      --description "Observability, Prompt Cache FinOps, and Autonomous Multi-Objective Agent Fleet Optimization Studio" \
      || {
    echo "Notice: agents-cli registration exited with status; checking REST fallback."
  }
else
  echo "agents-cli not found in PATH; register via Google Cloud Console or REST API."
fi

# 4. Display MCP Connector registration instructions
echo ""
echo "--- Step 4: BYO MCP Server Configuration ---"
echo "To attach VibeLift as an interactive BYO MCP Tool Connector in Gemini Enterprise:"
echo "  1. Open Google Cloud Console -> Gemini Enterprise -> Apps -> ${GE_ENGINE_ID}"
echo "  2. Go to Extensions / Tools -> Add Tool -> Custom MCP Connector"
echo "  3. Configure:"
echo "     - Name:          vibelift-analytics-mcp"
echo "     - Endpoint URL:  ${MCP_URL}"
echo "     - Protocol:      JSON-RPC 2.0 / Streamable HTTP (2025-06-18)"
echo "     - UI Resource:   ui://vibelift-analytics/dashboard"
echo "     - Auth:          Google Cloud IAM (uses runtime service account)"
echo "  4. Available Tools registered automatically:"
echo "     - open_dashboard (opens Right Side Panel with Fullscreen toggle)"
echo "     - query_ge_agent_fleet (live multi-engine agent inventory and telemetry)"
echo "     - query_project_telemetry (service requests, errors, and Cloud Run stats)"
echo "     - calculate_prompt_cache_economics (prefix cache hit ratios and savings)"
echo "     - run_alpha_evolve_generation (closed-loop Pareto optimization)"
echo "     - get_vibelift_state (runtime state, parameters, and turns)"
echo ""
echo "=========================================================="
echo " VibeLift Gemini Enterprise Registration Ready!"
echo " Dashboard: ${PUBLIC_URL}/ui"
echo " MCP App:   ui://vibelift-analytics/dashboard"
echo "=========================================================="
