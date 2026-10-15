output "raw_dataset_ids" {
  description = "Map of raw Cloud Logging BigQuery dataset IDs provisioned in the FinOps project."
  value       = { for k, v in google_bigquery_dataset.raw : k => v.dataset_id }
}

output "curated_dataset_id" {
  description = "Curated staging BigQuery dataset ID."
  value       = google_bigquery_dataset.modeling[var.curated_dataset_id].dataset_id
}

output "mart_dataset_id" {
  description = "FinOps reporting mart BigQuery dataset ID."
  value       = google_bigquery_dataset.modeling[var.mart_dataset_id].dataset_id
}

output "sink_writer_identities" {
  description = "Map of Cloud Logging sink names to their BigQuery writer service account identities."
  value       = { for k, v in local.sink_writer_identities : k => v.writer_identity }
}

output "sink_scope" {
  description = "Active Cloud Logging sink scope: ORGANIZATION, FOLDER, or PROJECT."
  value       = local.use_org_sink ? "ORGANIZATION" : (local.use_folder_sink ? "FOLDER" : "PROJECT")
}
