variable "project_id" {
  description = "Central GCP project ID hosting the BigQuery FinOps datasets and reporting mart."
  type        = string
}

variable "bq_location" {
  description = "BigQuery dataset location (e.g. US, EU, us-central1)."
  type        = string
  default     = "US"
}

variable "organization_id" {
  description = "Optional GCP Organization ID for aggregated org-level Cloud Logging sinks (include_children = true). Mutually exclusive with folder_id."
  type        = string
  default     = ""
}

variable "folder_id" {
  description = "Optional GCP Folder ID for aggregated folder-level Cloud Logging sinks (include_children = true). Mutually exclusive with organization_id."
  type        = string
  default     = ""
}

variable "retention_days" {
  description = "Default partition expiration in days for raw Cloud Logging sink datasets."
  type        = number
  default     = 90
}

variable "curated_dataset_id" {
  description = "Dataset ID for curated staging views over raw Cloud Logging sinks."
  type        = string
  default     = "ds_ge_curated_staging"
}

variable "mart_dataset_id" {
  description = "Dataset ID for the FinOps reporting mart (v_fct_turns, fct_turns, fct_sessions, agg_daily_usage)."
  type        = string
  default     = "finops_mart"
}

variable "monitored_cloud_run_services" {
  description = "Cloud Run service names routed by finops-telemetry-sink."
  type        = list(string)
  default     = ["finops-dashboard-mcp-app"]
}

variable "labels" {
  description = "Resource labels applied to all provisioned BigQuery datasets."
  type        = map(string)
  default = {
    app        = "finops-dashboard"
    managed_by = "terraform"
  }
}
