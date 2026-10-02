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
#
# Enabling also sets sensitiveLoggingEnabled, so prompts and responses are written to Cloud Logging.
# Requires gcloud, curl and jq (all preinstalled in Cloud Shell). Exits non-zero if any engine
# could not be listed or any agent could not be updated.
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
    -*)
      echo "ERROR: Unknown option '${arg}'. See the usage notes at the top of $0." >&2
      exit 2
      ;;
    *)
      if [[ -z "${POSITIONAL_ENGINE}" ]]; then
        POSITIONAL_ENGINE="${arg}"
      fi
      ;;
  esac
done

for bin in gcloud curl jq; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: '${bin}' is required but not installed (Cloud Shell includes it)." >&2
    exit 1
  fi
done

# Configuration (auto-detects from environment / gcloud if not explicitly set)
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
LOCATION="${LOCATION:-${FINOPS_MCP_GE_LOCATION:-${VIBELIFT_GE_LOCATION:-global}}}"
COLLECTION="${COLLECTION:-${FINOPS_MCP_GE_COLLECTION:-${VIBELIFT_GE_COLLECTION:-default_collection}}}"
ENGINE_ID="${POSITIONAL_ENGINE:-${ENGINE_ID:-${GE_ENGINE_ID:-}}}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set PROJECT_ID or GOOGLE_CLOUD_PROJECT, or run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

AGENT_RESOURCE_RE='^projects/[^/]+/locations/([a-z0-9-]+)/collections/[^/]+/engines/[^/]+/assistants/[^/]+/agents/[^/]+$'
if [[ -n "${TARGET_AGENT}" && ! "${TARGET_AGENT}" =~ ${AGENT_RESOURCE_RE} ]]; then
  echo "ERROR: --agent must be a full resource name:" >&2
  echo "  projects/<PROJECT>/locations/<LOCATION>/collections/<COLLECTION>/engines/<ENGINE>/assistants/<ASSISTANT>/agents/<AGENT_ID>" >&2
  exit 2
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
UPDATED=0
FAILED=0

de_host() {
  local loc="$1"
  if [[ "${loc}" == "global" ]]; then
    echo "discoveryengine.googleapis.com"
  else
    echo "${loc}-discoveryengine.googleapis.com"
  fi
}

# Prints the response body followed by the HTTP status code on its own last line (000 = no response).
api_call() {
  local method="$1" url="$2" data="${3:-}"
  local args=(-sS -X "${method}" -w '\n%{http_code}'
    -H "Authorization: Bearer ${ACCESS_TOKEN}"
    -H "X-Goog-User-Project: ${PROJECT_ID}")
  if [[ -n "${data}" ]]; then
    args+=(-H "Content-Type: application/json" -d "${data}")
  fi
  curl "${args[@]}" "${url}" || printf '\n000'
}

# Extracts the API error message from a response body, falling back to the HTTP status.
api_error() {
  local msg
  msg="$(printf '%s' "$1" | jq -r '.error.message // empty' 2>/dev/null || true)"
  echo "${msg:-HTTP $2}"
}

patch_agent_observability() {
  local host="$1"
  local agent_name="$2"
  local out code body
  echo "--------------------------------------------------------"
  echo "${ACTION_WORD} trace logging for: ${agent_name}"
  out="$(api_call PATCH "https://${host}/v1alpha/${agent_name}?updateMask=observabilityConfig" \
    "{\"observabilityConfig\":{\"observabilityEnabled\":${OBS_BOOL},\"sensitiveLoggingEnabled\":${OBS_BOOL}}}")"
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"
  if [[ "${code}" == 2* ]]; then
    UPDATED=$((UPDATED + 1))
    echo "Status: $(printf '%s' "${body}" | jq -c '.observabilityConfig // {}' 2>/dev/null || echo updated)"
  else
    FAILED=$((FAILED + 1))
    echo "❌ Failed: $(api_error "${body}" "${code}")" >&2
  fi
}

# Prints the matching agent resource names on an engine (all pages); returns 1 if the list call fails.
list_agents() {
  local host="$1" loc="$2" eng="$3"
  local token="" out code body url
  local jq_filter='.agents[]? | .name // empty'
  if [[ "${ONLY_LOW_CODE}" == "1" ]]; then
    jq_filter='.agents[]? | select(.lowCodeAgentDefinition != null or .workflowAgentDefinition != null) | .name // empty'
  fi
  while :; do
    url="https://${host}/v1alpha/projects/${PROJECT_ID}/locations/${loc}/collections/${COLLECTION}/engines/${eng}/assistants/default_assistant/agents?pageSize=100"
    if [[ -n "${token}" ]]; then
      url+="&pageToken=$(jq -rn --arg t "${token}" '$t|@uri')"
    fi
    out="$(api_call GET "${url}")"
    code="${out##*$'\n'}"
    body="${out%$'\n'*}"
    if [[ "${code}" != 2* ]]; then
      echo "❌ Could not list agents for ${loc}/${eng}: $(api_error "${body}" "${code}")" >&2
      return 1
    fi
    printf '%s' "${body}" | jq -r "${jq_filter}"
    token="$(printf '%s' "${body}" | jq -r '.nextPageToken // empty')"
    if [[ -z "${token}" ]]; then
      break
    fi
  done
}

# Prints the IDs of Gemini Enterprise apps (APP_TYPE_INTRANET engines) in a location; returns 1 on error.
discover_engines() {
  local loc="$1"
  local host token="" out code body url
  host="$(de_host "${loc}")"
  while :; do
    url="https://${host}/v1alpha/projects/${PROJECT_ID}/locations/${loc}/collections/${COLLECTION}/engines?pageSize=100"
    if [[ -n "${token}" ]]; then
      url+="&pageToken=$(jq -rn --arg t "${token}" '$t|@uri')"
    fi
    out="$(api_call GET "${url}")"
    code="${out##*$'\n'}"
    body="${out%$'\n'*}"
    if [[ "${code}" != 2* ]]; then
      echo "⚠️ Could not list engines in ${loc}: $(api_error "${body}" "${code}")" >&2
      return 1
    fi
    printf '%s' "${body}" | jq -r '.engines[]? | select(.appType == "APP_TYPE_INTRANET") | .name // empty | split("/") | last'
    token="$(printf '%s' "${body}" | jq -r '.nextPageToken // empty')"
    if [[ -z "${token}" ]]; then
      break
    fi
  done
}

update_for_engine() {
  local loc="$1"
  local eng="$2"
  local host agents agent
  host="$(de_host "${loc}")"

  echo "========================================================"
  echo "Fetching agents for Engine: ${eng} (location: ${loc})..."
  if ! agents="$(list_agents "${host}" "${loc}" "${eng}")"; then
    FAILED=$((FAILED + 1))
    return 0
  fi
  if [[ -z "${agents//[[:space:]]/}" ]]; then
    echo "⚠️ No matching agents found under 'default_assistant' in ${loc}/${eng}."
    return 0
  fi
  for agent in ${agents}; do
    patch_agent_observability "${host}" "${agent}"
  done
}

finish() {
  echo "========================================================"
  echo "Trace logging ${ACTION_PAST} on ${UPDATED} agent(s); ${FAILED} failure(s)."
  if (( FAILED > 0 )); then
    exit 1
  fi
  echo "✅ Done."
  exit 0
}

if [[ -n "${TARGET_AGENT}" ]]; then
  [[ "${TARGET_AGENT}" =~ ${AGENT_RESOURCE_RE} ]]
  patch_agent_observability "$(de_host "${BASH_REMATCH[1]}")" "${TARGET_AGENT}"
  finish
fi

if [[ -n "${ENGINE_ID}" && "${ENGINE_ID}" != "auto" ]]; then
  update_for_engine "${LOCATION}" "${ENGINE_ID}"
else
  echo "No explicit ENGINE_ID provided; auto-discovering Gemini Enterprise apps in ${PROJECT_ID}..."
  FOUND_ANY=0
  DISCOVERY_ERRORS=0
  for LOC in global us eu; do
    if ! ENGINES="$(discover_engines "${LOC}")"; then
      DISCOVERY_ERRORS=$((DISCOVERY_ERRORS + 1))
      continue
    fi
    for ENG_ID in ${ENGINES}; do
      FOUND_ANY=1
      update_for_engine "${LOC}" "${ENG_ID}"
    done
  done
  if [[ "${FOUND_ANY}" == "0" ]]; then
    echo "⚠️ No Gemini Enterprise apps discovered in ${PROJECT_ID}. Pass ENGINE_ID explicitly:"
    echo "   ./deploy/enable_agent_logging.sh <ENGINE_ID>"
    if (( DISCOVERY_ERRORS > 0 )); then
      FAILED=$((FAILED + DISCOVERY_ERRORS))
    fi
  fi
fi
finish
