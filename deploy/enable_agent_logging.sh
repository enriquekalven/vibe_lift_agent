#!/usr/bin/env bash
# -------------------------------------------------------------
# Script: enable_agent_logging.sh
# Purpose: Enables or disables trace logging (observabilityConfig)
#          across registered Gemini Enterprise agents (including
#          No-Code / Low-Code Agent Designer agents), or for a
#          single agent resource.
#
# Usage:
#   ./deploy/enable_agent_logging.sh                       # Enable all agents (auto-discovers engines)
#   ./deploy/enable_agent_logging.sh <ENGINE_ID>           # Enable all agents on <ENGINE_ID>
#   ./deploy/enable_agent_logging.sh --disable             # Disable all agents
#   ./deploy/enable_agent_logging.sh --disable <ENGINE_ID> # Disable all agents on <ENGINE_ID>
#   ./deploy/enable_agent_logging.sh --only-low-code       # Enable only No-Code / Low-Code agents
#   ./deploy/enable_agent_logging.sh --agent=<RESOURCE>    # Enable a single agent resource name
#   ./deploy/enable_agent_logging.sh --disable --agent=<RESOURCE> # Disable a single agent
# -------------------------------------------------------------

set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

ENABLE_LOGGING="${ENABLE_LOGGING:-true}"
ONLY_LOW_CODE="${ONLY_LOW_CODE:-0}"
TARGET_AGENT="${TARGET_AGENT:-}"
POSITIONAL_ENGINE=""

for arg in "$@"; do
  case "${arg}" in
    --disable|--off)
      ENABLE_LOGGING="false"
      ;;
    --enable|--on)
      ENABLE_LOGGING="true"
      ;;
    --only-low-code|--low-code)
      ONLY_LOW_CODE="1"
      ;;
    --agent=*)
      TARGET_AGENT="${arg#*=}"
      ;;
    *)
      if [[ -z "${POSITIONAL_ENGINE}" ]]; then
        POSITIONAL_ENGINE="${arg}"
      fi
      ;;
  esac
done

# Configuration (auto-detects from environment / gcloud if not explicitly set)
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}"
LOCATION="${LOCATION:-${FINOPS_MCP_GE_LOCATION:-${VIBELIFT_GE_LOCATION:-global}}}"
COLLECTION="${COLLECTION:-${FINOPS_MCP_GE_COLLECTION:-${VIBELIFT_GE_COLLECTION:-default_collection}}}"
ENGINE_ID="${POSITIONAL_ENGINE:-${ENGINE_ID:-${GE_ENGINE_ID:-}}}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set PROJECT_ID or GOOGLE_CLOUD_PROJECT, or run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

if [[ "${ENABLE_LOGGING}" == "false" || "${ENABLE_LOGGING}" == "0" ]]; then
  OBS_BOOL="false"
  ACTION_WORD="Disabling"
  ACTION_PAST="disabled"
else
  OBS_BOOL="true"
  ACTION_WORD="Enabling"
  ACTION_PAST="enabled"
fi

ACCESS_TOKEN="$(gcloud auth print-access-token)"

de_host() {
  local loc="$1"
  if [[ "${loc}" == "global" ]]; then
    echo "discoveryengine.googleapis.com"
  else
    echo "${loc}-discoveryengine.googleapis.com"
  fi
}

patch_agent_observability() {
  local host="$1"
  local agent_name="$2"
  echo "--------------------------------------------------------"
  echo "${ACTION_WORD} trace logging for: ${agent_name}"

  local result
  result=$(curl -s -X PATCH \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    -H "Content-Type: application/json" \
    "https://${host}/v1alpha/${agent_name}?updateMask=observabilityConfig" \
    -d "{
      \"observabilityConfig\": {
        \"observabilityEnabled\": ${OBS_BOOL},
        \"sensitiveLoggingEnabled\": ${OBS_BOOL}
      }
    }")

  echo "Status: $(echo "${result}" | jq -c '.observabilityConfig // .error.message')"
}

if [[ -n "${TARGET_AGENT}" ]]; then
  # Extract location from resource name if present
  AGENT_LOC="${LOCATION}"
  if [[ "${TARGET_AGENT}" =~ locations/([^/]+)/ ]]; then
    AGENT_LOC="${BASH_REMATCH[1]}"
  fi
  patch_agent_observability "$(de_host "${AGENT_LOC}")" "${TARGET_AGENT}"
  echo "--------------------------------------------------------"
  echo "✅ Trace logging ${ACTION_PAST} for ${TARGET_AGENT}."
  exit 0
fi

update_for_engine() {
  local loc="$1"
  local eng="$2"
  local host
  host="$(de_host "${loc}")"

  echo "========================================================"
  echo "Fetching active agents for Engine: ${eng} (location: ${loc})..."
  local response
  response=$(curl -s -X GET \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "https://${host}/v1alpha/projects/${PROJECT_ID}/locations/${loc}/collections/${COLLECTION}/engines/${eng}/assistants/default_assistant/agents")

  local jq_filter='.agents[]?.name // empty'
  if [[ "${ONLY_LOW_CODE}" == "1" ]]; then
    jq_filter='.agents[]? | select(.lowCodeAgentDefinition != null or .workflowAgentDefinition != null) | .name // empty'
  fi

  local agents
  agents=$(echo "${response}" | jq -r "${jq_filter}")

  if [[ -z "${agents}" ]]; then
    echo "⚠️ No matching agents found under 'default_assistant' in ${loc}/${eng}."
    return 0
  fi

  for AGENT in ${agents}; do
    patch_agent_observability "${host}" "${AGENT}"
  done
  echo "--------------------------------------------------------"
  echo "✅ Trace logging ${ACTION_PAST} for all matching agents in ${eng} (${loc})."
}

if [[ -n "${ENGINE_ID}" && "${ENGINE_ID}" != "auto" ]]; then
  update_for_engine "${LOCATION}" "${ENGINE_ID}"
else
  echo "No explicit ENGINE_ID provided; auto-discovering Gemini Enterprise engines in ${PROJECT_ID}..."
  FOUND_ANY=0
  for LOC in global us eu; do
    HOST="$(de_host "${LOC}")"
    ENG_RESP=$(curl -s -X GET \
      -H "Authorization: Bearer ${ACCESS_TOKEN}" \
      -H "X-Goog-User-Project: ${PROJECT_ID}" \
      "https://${HOST}/v1alpha/projects/${PROJECT_ID}/locations/${LOC}/collections/${COLLECTION}/engines" || true)
    ENGINES=$(echo "${ENG_RESP}" | jq -r '.engines[]?.name // empty' 2>/dev/null || true)
    for ENG_PATH in ${ENGINES}; do
      ENG_ID="${ENG_PATH##*/}"
      if [[ -n "${ENG_ID}" ]]; then
        FOUND_ANY=1
        update_for_engine "${LOC}" "${ENG_ID}"
      fi
    done
  done
  if [[ "${FOUND_ANY}" == "0" ]]; then
    echo "⚠️ No Gemini Enterprise engines discovered in ${PROJECT_ID}. Pass ENGINE_ID explicitly:"
    echo "   ./deploy/enable_agent_logging.sh <ENGINE_ID>"
  fi
fi
