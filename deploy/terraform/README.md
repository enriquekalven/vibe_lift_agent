# FinOps Data Plane Terraform Module (`deploy/terraform`)

Production Infrastructure-as-Code (IaC) replacement for [`deploy/setup_bigquery_sink.sh`](../setup_bigquery_sink.sh).

## What This Module Provisions

1. **7 Raw Cloud Logging BigQuery Datasets** (partition-expired automatically via `retention_days`, default `90` days):
   - `ds_ge_assistant_raw`
   - `ds_ge_search_raw`
   - `ds_vertex_agents_raw`
   - `ds_ge_audit_raw`
   - `ds_security_guardrails_raw`
   - `ds_antigravity_raw`
   - `finops_analytics`
2. **2 Analytical Modeling Datasets**:
   - `ds_ge_curated_staging` (Curated typed views over raw JSON log sinks)
   - `finops_mart` (`v_fct_turns`, `fct_turns`, `fct_sessions`, `agg_daily_usage`, `v_looker_l1_l2_support`)
3. **8 Cloud Logging Sinks** (Project-level, Folder-level with `include_children = true`, or Organization-level with `include_children = true`):
   - `sink-ge-assistant-activity`
   - `sink-ge-search-activity`
   - `sink-vertex-reasoning-engine`
   - `sink-ge-inference-tokens`
   - `sink-platform-audit`
   - `sink-model-armor-sdp`
   - `finops-telemetry-sink`
   - `sink-antigravity-inference`
4. **Least-Privilege Dataset IAM Bindings**:
   - Grants `roles/bigquery.dataEditor` on each target raw dataset to its sink's `writer_identity` (instead of project-wide editor roles).

## Usage: Multi-Project Enterprise (Folder or Organization Aggregated Sinks)

```hcl
module "finops_data_plane" {
  source          = "./deploy/terraform"
  project_id      = "my-central-finops-prod"
  folder_id       = "123456789012" # Routes logs from all child projects (GE, Agent Engine, Cloud Run, GKE)
  bq_location     = "US"
  retention_days  = 90
}
```

## Usage: Single GCP Project

```hcl
module "finops_data_plane" {
  source         = "./deploy/terraform"
  project_id     = "my-agent-project"
  bq_location    = "US"
  retention_days = 90
}
```

After `terraform apply`, compile and execute the SQL modeling layer via the companion Dataform package in [`deploy/dataform/`](../dataform/README.md) or run:

```bash
python3 deploy/bigquery/provision_ge_mart.py \
  --project="my-central-finops-prod" \
  --location="US" \
  --gcloud-auth --apply
```
