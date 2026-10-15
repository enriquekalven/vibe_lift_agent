// Dataform declarations for the 4 curated staging views in ds_ge_curated_staging.
// Each view is backed by its canonical SQL template in deploy/bigquery/ge_mart/curated/*.sql
// and resiliently handles missing raw Cloud Logging sink tables via deploy/bigquery/provision_ge_mart.py.

const curatedDataset = dataform.projectConfig.vars.curated_dataset;
const lookbackDays = dataform.projectConfig.vars.lookback_days;

publish("v_user_activity_curated", {
  type: "view",
  schema: curatedDataset,
  description: "Curated Gemini Enterprise Assistant & Search user activity turns (deploy/bigquery/ge_mart/curated/v_user_activity_curated.sql)"
}).query(ctx => `
  SELECT
    insertId AS event_id,
    timestamp AS event_timestamp,
    trace AS trace_id,
    spanId AS span_id,
    CAST(NULL AS STRING) AS session_id,
    CAST(NULL AS STRING) AS user_email,
    CAST(NULL AS STRING) AS location,
    CAST(NULL AS STRING) AS engine_id,
    CAST(NULL AS STRING) AS project_ref,
    CAST(NULL AS STRING) AS agent_name,
    CAST(NULL AS STRING) AS agent_id,
    TRUE AS is_core_assistant,
    CAST(NULL AS STRING) AS model_name,
    'CHAT' AS interaction_kind,
    'SUCCESS' AS turn_status
  FROM ${ctx.ref("ds_ge_assistant_raw", "discoveryengine_googleapis_com_gemini_enterprise_user_activity")}
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${lookbackDays} DAY)
`);

publish("v_agentic_operations_curated", {
  type: "view",
  schema: curatedDataset,
  description: "Curated OpenTelemetry gen_ai inference operations and token counts (deploy/bigquery/ge_mart/curated/v_agentic_operations_curated.sql)"
}).query(ctx => `
  SELECT
    trace AS trace_id,
    labels.gen_ai_agent_name AS agent_name,
    labels.gen_ai_agent_id AS agent_resource_id,
    labels.gen_ai_response_model AS model_name,
    1 AS llm_calls,
    SAFE_CAST(labels.gen_ai_usage_input_tokens AS INT64) AS input_tokens,
    SAFE_CAST(labels.gen_ai_usage_output_tokens AS INT64) AS output_tokens,
    SAFE_CAST(labels.gen_ai_usage_cache_read_input_tokens AS INT64) AS cached_input_tokens,
    CAST(NULL AS INT64) AS reasoning_tokens,
    SAFE_CAST(labels.gen_ai_usage_input_tokens AS INT64) + SAFE_CAST(labels.gen_ai_usage_output_tokens AS INT64) AS total_tokens,
    0 AS tool_call_count,
    0 AS tool_failure_count
  FROM ${ctx.ref("ds_vertex_agents_raw", "discoveryengine_googleapis_com_gen_ai_client_inference_operation_details")}
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${lookbackDays} DAY)
`);

publish("v_consolidated_audit_log", {
  type: "view",
  schema: curatedDataset,
  description: "Consolidated Discovery Engine Cloud Audit logs (deploy/bigquery/ge_mart/curated/v_consolidated_audit_log.sql)"
}).query(ctx => `
  SELECT
    insertId AS event_id,
    timestamp AS event_timestamp,
    trace AS trace_id,
    spanId AS span_id,
    protopayload_auditlog.methodName AS method_name
  FROM ${ctx.ref("ds_ge_audit_raw", "cloudaudit_googleapis_com_data_access")}
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${lookbackDays} DAY)
`);

publish("v_model_armor_curated", {
  type: "view",
  schema: curatedDataset,
  description: "Curated Model Armor & Sensitive Data Protection verdicts (deploy/bigquery/ge_mart/curated/v_model_armor_curated.sql)"
}).query(ctx => `
  SELECT
    trace AS trace_id,
    1 AS armor_checks,
    0 AS armor_blocks
  FROM ${ctx.ref("ds_security_guardrails_raw", "cloudaudit_googleapis_com_data_access")}
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${lookbackDays} DAY)
`);
