locals {
  use_org_sink    = length(trimspace(var.organization_id)) > 0
  use_folder_sink = !local.use_org_sink && length(trimspace(var.folder_id)) > 0
  use_project_sink = !local.use_org_sink && !local.use_folder_sink

  # When using org or folder aggregated sinks (include_children = true), omit the single-project
  # logName prefix so logs from child projects (GE app project, Agent Engine project, Cloud Run/GKE
  # project) are all captured into the central BigQuery datasets.
  log_prefix_regex = (
    local.use_project_sink
    ? "projects/${var.project_id}/logs/"
    : "projects/[^/]+/logs/"
  )

  service_clause = join(" OR ", [for s in var.monitored_cloud_run_services : "\"${s}\""])

  raw_datasets = {
    ds_ge_assistant_raw        = "Gemini Enterprise Assistant raw user activity log sink"
    ds_ge_search_raw           = "Gemini Enterprise Search raw user activity log sink"
    ds_vertex_agents_raw       = "Vertex AI Reasoning Engine and Model inference log sink"
    ds_ge_audit_raw            = "Gemini Enterprise and Cloud Audit data access & activity log sink"
    ds_security_guardrails_raw = "Model Armor and Sensitive Data Protection guardrail log sink"
    ds_antigravity_raw         = "Google Antigravity IDE inference_response metadata log sink"
    finops_analytics           = "FinOps Dashboard agent runtime turns, evaluations, and optimizer snapshots"
  }

  modeling_datasets = {
    (var.curated_dataset_id) = "Curated typed views over raw Gemini Enterprise log sinks"
    (var.mart_dataset_id)    = "FinOps Dashboard turn, session, and daily usage reporting mart"
  }

  sinks = {
    "sink-ge-assistant-activity" = {
      dataset = "ds_ge_assistant_raw"
      filter  = "logName=~\"^${local.log_prefix_regex}discoveryengine\\.googleapis\\.com%2Fgemini_enterprise_user_activity$\" AND jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.AssistantService\""
    }
    "sink-ge-search-activity" = {
      dataset = "ds_ge_search_raw"
      filter  = "logName=~\"^${local.log_prefix_regex}discoveryengine\\.googleapis\\.com%2Fgemini_enterprise_user_activity$\" AND (jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.SearchService\" OR jsonPayload.logMetadata.serviceName=\"google.cloud.discoveryengine.v1main.ConversationSearchService\")"
    }
    "sink-vertex-reasoning-engine" = {
      dataset = "ds_vertex_agents_raw"
      filter  = "(protoPayload.serviceName=\"aiplatform.googleapis.com\" AND (protoPayload.methodName=~\"(ReasoningEngineExecutionService|ReasoningEngineService)\" OR ((protoPayload.methodName=\"Predict\" OR protoPayload.methodName=~\"GenerateContent\") AND protoPayload.resourceName=~\"publishers/(google/models/gemini|anthropic/models)\"))) OR logName=~\"^${local.log_prefix_regex}gen_ai\\.client\\.inference\\.operation\\.details$\""
    }
    "sink-ge-inference-tokens" = {
      dataset = "ds_vertex_agents_raw"
      filter  = "logName=~\"^${local.log_prefix_regex}discoveryengine\\.googleapis\\.com%2Fgen_ai\\.client\\.inference\\.operation\\.details$\""
    }
    "sink-platform-audit" = {
      dataset = "ds_ge_audit_raw"
      filter  = "(logName=~\"^${local.log_prefix_regex}cloudaudit\\.googleapis\\.com%2F(activity|data_access)$\" AND protoPayload.serviceName=\"discoveryengine.googleapis.com\") OR logName=~\"^${local.log_prefix_regex}discoveryengine\\.googleapis\\.com%2F(connector_activity|api_errors|data_connectors)$\""
    }
    "sink-model-armor-sdp" = {
      dataset = "ds_security_guardrails_raw"
      filter  = "protoPayload.serviceName=\"modelarmor.googleapis.com\" OR jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.modelarmor.logging.v1.SanitizeOperationLogEntry\" OR (protoPayload.serviceName=\"dlp.googleapis.com\" AND protoPayload.methodName=~\"(InspectContent|DeidentifyContent)\")"
    }
    "finops-telemetry-sink" = {
      dataset = "finops_analytics"
      filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=(${local.service_clause})"
    }
    "sink-antigravity-inference" = {
      dataset = "ds_antigravity_raw"
      filter  = "logName=~\"^${local.log_prefix_regex}businessaicode\\.googleapis\\.com%2Finference_response$\""
    }
  }
}

resource "google_bigquery_dataset" "raw" {
  for_each = local.raw_datasets

  project                     = var.project_id
  dataset_id                  = each.key
  location                    = var.bq_location
  description                 = each.value
  default_partition_expiration_ms = var.retention_days * 24 * 3600 * 1000
  labels                      = var.labels
}

resource "google_bigquery_dataset" "modeling" {
  for_each = local.modeling_datasets

  project     = var.project_id
  dataset_id  = each.key
  location    = var.bq_location
  description = each.value
  labels      = var.labels
}

resource "google_logging_project_sink" "project_sinks" {
  for_each = local.use_project_sink ? local.sinks : {}

  project                = var.project_id
  name                   = each.key
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.raw[each.value.dataset].dataset_id}"
  filter                 = each.value.filter
  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_logging_folder_sink" "folder_sinks" {
  for_each = local.use_folder_sink ? local.sinks : {}

  folder           = var.folder_id
  name             = each.key
  destination      = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.raw[each.value.dataset].dataset_id}"
  filter           = each.value.filter
  include_children = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_logging_organization_sink" "org_sinks" {
  for_each = local.use_org_sink ? local.sinks : {}

  org_id           = var.organization_id
  name             = each.key
  destination      = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.raw[each.value.dataset].dataset_id}"
  filter           = each.value.filter
  include_children = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

locals {
  sink_writer_identities = {
    for k, v in local.sinks : k => {
      dataset_id = v.dataset
      writer_identity = (
        local.use_org_sink
        ? google_logging_organization_sink.org_sinks[k].writer_identity
        : (
          local.use_folder_sink
          ? google_logging_folder_sink.folder_sinks[k].writer_identity
          : google_logging_project_sink.project_sinks[k].writer_identity
        )
      )
    }
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writers" {
  for_each = local.sink_writer_identities

  project    = var.project_id
  dataset_id = google_bigquery_dataset.raw[each.value.dataset_id].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = each.value.writer_identity
}
