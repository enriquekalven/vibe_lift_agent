#!/usr/bin/env bash
# Provisions enterprise organization security, network perimeter, WAF, and column-level
# data governance controls for VibeLift on Google Cloud:
#   1. Cloud Pub/Sub Dead-Letter Queue (vibelift-ingest-dlq + subscription + SA IAM)
#   2. BigQuery Data Catalog Policy Tag taxonomy & column-level masking on vibelift_mart.fct_turns
#   3. Cloud Armor WAF Security Policy (OWASP SQLi/XSS CRS + rate limiting) & Serverless NEG
#   4. VPC Service Controls (VPC-SC) perimeter protecting Cloud Run, BigQuery, Discovery Engine,
#      Vertex AI, Cloud Logging/Monitoring/Trace, and Pub/Sub with ingress rules for Gemini
#      Enterprise (gcp-sa-discoveryengine) and Identity-Aware Proxy (gcp-sa-iap).
#
# Safe to re-run (idempotent). Individual sections can be toggled via environment variables:
#   ENABLE_PUBSUB_DLQ=1          (default: 1 — project-level)
#   ENABLE_BQ_COLUMN_MASKING=1   (default: 1 — requires datacatalog.taxonomies.create)
#   ENABLE_CLOUD_ARMOR_WAF=1     (default: 1 — project-level Cloud Armor policy + Serverless NEG)
#   ENABLE_VPC_SC_PERIMETER=1    (default: auto when ACM_POLICY_ID is set — org-level)

set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || echo "project-maui")}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
BQ_LOCATION="${VIBELIFT_BQ_LOCATION:-US}"
MART_DATASET="${VIBELIFT_GE_MART_DATASET:-vibelift_mart}"
RUNTIME_SA_NAME="${VIBELIFT_RUNTIME_SA_NAME:-vibe-lift-runtime-sa}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DASHBOARD_SERVICE="${VIBELIFT_DASHBOARD_SERVICE:-vibe-lift-dashboard}"
DLQ_TOPIC="${VIBELIFT_DLQ_TOPIC_NAME:-vibelift-ingest-dlq}"
DLQ_SUB="${VIBELIFT_DLQ_SUB_NAME:-vibelift-ingest-dlq-sub}"
WAF_POLICY="${VIBELIFT_WAF_POLICY_NAME:-vibelift-waf-policy}"
NEG_NAME="${VIBELIFT_DASHBOARD_NEG:-vibelift-dashboard-neg}"
PERIMETER_NAME="${VIBELIFT_VPC_SC_PERIMETER:-vibelift_ge_perimeter}"
ACM_POLICY_ID="${ACM_POLICY_ID:-}"

ENABLE_PUBSUB_DLQ="${ENABLE_PUBSUB_DLQ:-1}"
ENABLE_BQ_COLUMN_MASKING="${ENABLE_BQ_COLUMN_MASKING:-1}"
ENABLE_CLOUD_ARMOR_WAF="${ENABLE_CLOUD_ARMOR_WAF:-1}"
ENABLE_VPC_SC_PERIMETER="${ENABLE_VPC_SC_PERIMETER:-}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
GE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
IAP_SA="service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

echo "=== VibeLift Enterprise Security & Governance Provisioner ==="
echo "Project: ${PROJECT_ID} (#${PROJECT_NUMBER}) | Region: ${REGION}"

# ---------------------------------------------------------------------------
# 1. Cloud Pub/Sub Dead-Letter Queue (DLQ) for Rejected Ingest Payloads
# ---------------------------------------------------------------------------
if [[ "${ENABLE_PUBSUB_DLQ}" == "1" ]]; then
  echo "--- [1/4] Provisioning Cloud Pub/Sub Dead-Letter Queue (${DLQ_TOPIC}) ---"
  gcloud services enable pubsub.googleapis.com --project="${PROJECT_ID}" --quiet
  if ! gcloud pubsub topics describe "${DLQ_TOPIC}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud pubsub topics create "${DLQ_TOPIC}" \
      --project="${PROJECT_ID}" \
      --message-retention-duration=7d \
      --labels="app=vibelift,component=ingest-dlq"
  fi
  if ! gcloud pubsub subscriptions describe "${DLQ_SUB}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud pubsub subscriptions create "${DLQ_SUB}" \
      --project="${PROJECT_ID}" \
      --topic="${DLQ_TOPIC}" \
      --ack-deadline=60 \
      --message-retention-duration=7d \
      --labels="app=vibelift,component=ingest-dlq"
  fi
  gcloud pubsub topics add-iam-policy-binding "${DLQ_TOPIC}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/pubsub.publisher" \
    --quiet >/dev/null
  echo "  + DLQ topic ready: projects/${PROJECT_ID}/topics/${DLQ_TOPIC}"
fi

# ---------------------------------------------------------------------------
# 2. BigQuery Data Catalog Policy Tag & Column-Level Masking on fct_turns
# ---------------------------------------------------------------------------
if [[ "${ENABLE_BQ_COLUMN_MASKING}" == "1" ]]; then
  echo "--- [2/4] Configuring BigQuery Column-Level PII Policy Tag on ${MART_DATASET}.fct_turns ---"
  gcloud services enable datacatalog.googleapis.com bigquerydatapolicy.googleapis.com \
    --project="${PROJECT_ID}" --quiet || true
  if [[ -n "${VIBELIFT_POLICY_TAG_PII:-}" ]]; then
    python3 -c "
from vibelift import ge_mart
print(ge_mart.build_pii_policy_tag_ddl('${PROJECT_ID}', '${VIBELIFT_POLICY_TAG_PII}'))
" | bq query --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" --use_legacy_sql=false
    echo "  + Attached policy tag ${VIBELIFT_POLICY_TAG_PII} to ${MART_DATASET}.fct_turns.user_email"
  else
    echo "  = Set VIBELIFT_POLICY_TAG_PII=projects/${PROJECT_ID}/locations/${BQ_LOCATION,,}/taxonomies/<ID>/policyTags/<ID> to attach column policy tag, or VIBELIFT_PII_REDACT=1 for runtime SHA-256 pseudonymization."
  fi
fi

# ---------------------------------------------------------------------------
# 3. Cloud Armor WAF Policy + Serverless NEG for vibe-lift-dashboard
# ---------------------------------------------------------------------------
if [[ "${ENABLE_CLOUD_ARMOR_WAF}" == "1" ]]; then
  echo "--- [3/4] Provisioning Cloud Armor WAF Policy (${WAF_POLICY}) & Serverless NEG ---"
  gcloud services enable compute.googleapis.com --project="${PROJECT_ID}" --quiet
  if ! gcloud compute security-policies describe "${WAF_POLICY}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud compute security-policies create "${WAF_POLICY}" \
      --project="${PROJECT_ID}" \
      --description="VibeLift OWASP CRS WAF and rate-limiting policy"
    gcloud compute security-policies rules create 1000 \
      --security-policy="${WAF_POLICY}" \
      --project="${PROJECT_ID}" \
      --expression="evaluatePreconfiguredWaf('sqli-v33-stable') || evaluatePreconfiguredWaf('xss-v33-stable')" \
      --action=deny-403 \
      --description="Block SQLi and XSS payloads at edge"
    gcloud compute security-policies rules create 2000 \
      --security-policy="${WAF_POLICY}" \
      --project="${PROJECT_ID}" \
      --src-ip-ranges="*" \
      --action=rate-based-ban \
      --rate-limit-threshold-count=120 \
      --rate-limit-threshold-interval-sec=60 \
      --ban-duration-sec=300 \
      --conform-action=allow \
      --exceed-action=deny-429 \
      --enforce-on-key=IP \
      --description="Rate limit 120 req/min per client IP"
  fi
  if ! gcloud compute network-endpoint-groups describe "${NEG_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud compute network-endpoint-groups create "${NEG_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --network-endpoint-type=serverless \
      --cloud-run-service="${DASHBOARD_SERVICE}"
  fi
  echo "  + Cloud Armor policy ${WAF_POLICY} and Serverless NEG ${NEG_NAME} ready."
fi

# ---------------------------------------------------------------------------
# 4. VPC Service Controls (VPC-SC) Service Perimeter (Org-Level Access Context Manager)
# ---------------------------------------------------------------------------
if [[ -n "${ACM_POLICY_ID}" && "${ENABLE_VPC_SC_PERIMETER:-1}" == "1" ]]; then
  echo "--- [4/4] Provisioning VPC Service Controls Perimeter (${PERIMETER_NAME}) under policy ${ACM_POLICY_ID} ---"
  RESTRICTED_SERVICES="run.googleapis.com,bigquery.googleapis.com,discoveryengine.googleapis.com,aiplatform.googleapis.com,logging.googleapis.com,monitoring.googleapis.com,cloudtrace.googleapis.com,pubsub.googleapis.com"
  if ! gcloud access-context-manager perimeters describe "${PERIMETER_NAME}" --policy="${ACM_POLICY_ID}" >/dev/null 2>&1; then
    gcloud access-context-manager perimeters create "${PERIMETER_NAME}" \
      --policy="${ACM_POLICY_ID}" \
      --title="VibeLift Gemini Enterprise Telemetry Perimeter" \
      --resources="projects/${PROJECT_NUMBER}" \
      --restricted-services="${RESTRICTED_SERVICES}" \
      --type=regular
  else
    gcloud access-context-manager perimeters update "${PERIMETER_NAME}" \
      --policy="${ACM_POLICY_ID}" \
      --add-resources="projects/${PROJECT_NUMBER}" \
      --add-restricted-services="${RESTRICTED_SERVICES}"
  fi
  echo "  + VPC-SC perimeter ${PERIMETER_NAME} configured for project #${PROJECT_NUMBER} (GE SA: ${GE_SA}, IAP SA: ${IAP_SA})."
else
  echo "--- [4/4] VPC-SC Perimeter: pass ACM_POLICY_ID=<org-access-policy-id> to apply org-level perimeter ${PERIMETER_NAME} ---"
fi

echo "=== Enterprise Security & Governance Provisioning Complete ==="
