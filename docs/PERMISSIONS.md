# VibeLift permissions reference

All the IAM roles, APIs and org policies VibeLift uses, in one place. Sources: `deploy/deploy_cloud_run.sh`, `deploy/setup_bigquery_sink.sh`, `deploy/setup_mart_refresh.sh`, `deploy/register_ge_agent.sh` and the README.

Each row says whether a script grants it or you do it by hand. **Billing is manual**, and the MCP connector needs its own script (`deploy/setup_mcp_connector.py`), so a deploy that only ran `deploy_cloud_run.sh` will show no billing data and no side-panel connector.

## 1. Runtime service account `vibe-lift-runtime-sa@PROJECT_ID.iam.gserviceaccount.com`

The Cloud Run service reads every metric as this account, never with the end user's token.

| Role | Scope | Why | Granted by |
| :--- | :--- | :--- | :--- |
| `roles/discoveryengine.viewer` | Project | List Gemini Enterprise apps, assistants, agents | `deploy_cloud_run.sh` |
| Custom `vibeLiftGeFleetReader` (`discoveryengine.engines.get`, `assistants.list`, `agents.list`, `agents.get`, `agents.manage`) | Project | Without `agents.manage`, `agents.list` returns only the caller's own agents. No create/update/delete. | `deploy_cloud_run.sh` |
| `roles/aiplatform.viewer` | Project | Agent Engine (Reasoning Engine) metadata | `deploy_cloud_run.sh` |
| `roles/aiplatform.user` | Project | Gemini calls from the ADK agent and the LLM judge | `deploy_cloud_run.sh` |
| `roles/logging.viewer` | Project | OpenTelemetry `gen_ai` logs, audit logs | `deploy_cloud_run.sh` |
| `roles/monitoring.viewer` | Project | Request, error, latency and token metrics | `deploy_cloud_run.sh` |
| `roles/cloudtrace.user` | Project | Tool/skill spans from Cloud Trace | `deploy_cloud_run.sh` |
| `roles/run.viewer` | Project | Discover Cloud Run agents and MCP servers | `deploy_cloud_run.sh` |
| `roles/container.clusterViewer` | Project | Discover GKE workloads | `deploy_cloud_run.sh` |
| `roles/bigquery.dataViewer` | Project | Read sink tables and `vibelift_mart` | `deploy_cloud_run.sh` |
| `roles/bigquery.jobUser` | Project | Run queries (mart and billing export queries run as jobs in this project) | `deploy_cloud_run.sh` |
| `WRITER` (= BigQuery Data Editor) | Dataset `vibelift_mart` only | "Refresh Mart" button rebuilds `fct_turns` | `deploy_cloud_run.sh`, **only if the dataset already exists**. Re-run the script after `setup_bigquery_sink.sh`. |
| **BigQuery Data Viewer** | **Billing export dataset** (often in another project) | Billed cost on the Cost tab | **Manual** (see section 4) |

## 2. Google-managed service identities

| Identity | Role | Why | Granted by |
| :--- | :--- | :--- | :--- |
| Discovery Engine service agent `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com` | `roles/run.invoker` on the `vibe-lift-agent` service | Gemini Enterprise calls `/mcp` and the A2A card with this identity's ID token | `deploy_cloud_run.sh`, `register_ge_agent.sh` |
| Compute Engine default SA `PROJECT_NUMBER-compute@developer.gserviceaccount.com` | `roles/run.builder` | Cloud Build source deploys (needed in orgs created after May 2024) | `deploy_cloud_run.sh` |
| Each log sink's writer identity | `roles/bigquery.dataEditor` | Route logs into the raw datasets | `setup_bigquery_sink.sh` |
| BigQuery Data Transfer service agent | `roles/iam.serviceAccountTokenCreator` on the runtime SA | Hourly scheduled query runs as the runtime SA | `setup_mart_refresh.sh` |

## 3. People

| Who | Needs | For |
| :--- | :--- | :--- |
| Person running the scripts | Project **Owner** is simplest. Otherwise roughly: Service Usage Admin, Service Account Admin, Role Administrator, Project IAM Admin, Cloud Run Admin, Cloud Build Editor, Artifact Registry Admin, Storage Admin, Service Account User on the runtime SA, Logs Configuration Writer, BigQuery Admin, Discovery Engine Admin. Not tested as a minimal set. | Deploy |
| Person registering in Gemini Enterprise | **Gemini Enterprise Admin** (A2A agent), **Discovery Engine Editor** (`roles/discoveryengine.editor`, MCP data store via `setup_mcp_connector.py` or the console) | Step 4 |
| Org admin (only if needed) | **Organization Policy Administrator**, if creating the MCP data store fails because of *Disable custom MCP server connector for Gemini Enterprise*. In `project-maui` the policy showed as enforced but creation still worked. | MCP connector |
| Billing account admin | **Billing Account Administrator** (or Billing Account Costs Manager) on the billing account | Turning on the BigQuery billing export |
| Dashboard viewers | `roles/run.invoker` on the service (`VIBELIFT_INVOKERS` in `deploy_cloud_run.sh`) | Opening the dashboard through `gcloud run services proxy` |

## 4. Billing data (manual)

Without these steps the Cost tab shows `NOT_CONNECTED` (or `ERROR` with the BigQuery error message). No numbers are estimated in its place.

1. A billing account admin turns on *Billing > Billing export > BigQuery export* (standard or detailed). Data can take up to a day to appear.
2. Grant the runtime SA **BigQuery Data Viewer** on the export dataset: *BigQuery > dataset > Sharing > Permissions > Add principal*.
3. Point the service at the table, without a rebuild:
   ```bash
   gcloud run services update vibe-lift-agent --region=REGION \
     --update-env-vars=VIBELIFT_BILLING_EXPORT_TABLE=BILLING_PROJECT.DATASET.gcp_billing_export_v1_XXXX
   ```

Scope: cost is filtered to `project.id = GOOGLE_CLOUD_PROJECT` and reported per day, per service and per SKU. The export has no per-agent split, so VibeLift does not assign dollars to individual agents or users.

## 5. APIs

Enabled by `deploy_cloud_run.sh`: `run`, `cloudbuild`, `artifactregistry`, `logging`, `monitoring`, `discoveryengine`, `aiplatform`, `bigquery` (plus `compute` if the default SA is missing). `setup_mart_refresh.sh` enables `bigquerydatatransfer`.

## 6. Org policies that can block a deploy

| Policy | Effect | Fix |
| :--- | :--- | :--- |
| *Disable custom MCP server connector for Gemini Enterprise* | May block creating the Custom MCP Server data store (it did not in `project-maui`) | Try first; if creation fails with a policy error, override for the project with enforcement **Off** |
| `iam.automaticIamGrantsForDefaultServiceAccounts` | Source build fails | Handled by `deploy_cloud_run.sh` (`roles/run.builder`) |
| Domain restricted sharing (`iam.allowedPolicyMemberDomains`) | Grants to outside accounts fail | Use only principals from your own domain in `VIBELIFT_INVOKERS` |
| VPC-SC / data connector restrictions | MCP data store blocked | Allow `custom_mcp` and the `*.run.app` host (see README step 0) |
