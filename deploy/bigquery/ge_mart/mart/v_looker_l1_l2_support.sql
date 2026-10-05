-- Denormalized L1/L2 Support & Session Triage view for Looker Studio.
-- Reads ONLY the materialized, date-partitioned `{{mart}}.fct_turns` table so Looker Studio
-- scorecards, filter controls, and drill-down tables execute in sub-second time without data blends
-- or raw JSON log scans.
SELECT
  turn_id,
  SUBSTR(REGEXP_EXTRACT(turn_id, r'([^:]+)$'), 1, 14) AS ticket_id,
  turn_source,
  turn_kind,
  event_timestamp,
  event_date,
  TIMESTAMP_TRUNC(event_timestamp, HOUR) AS event_hour,
  refreshed_at,
  trace_id,
  session_id,
  user_email,
  REGEXP_EXTRACT(user_email, r'@(.+)$') AS user_domain,
  location,
  engine_id,
  engine_key,
  project_ref,
  agent_name,
  agent_id,
  is_core_assistant,
  model_name,
  model_selection_mode,
  api_method,
  answer_state,
  skipped_reasons,
  turn_status,
  is_failed,
  is_actionable_issue,
  COALESCE(tool_failure_count, 0) > 0 AS has_tool_failure,
  COALESCE(is_actionable_issue, FALSE) OR COALESCE(tool_failure_count, 0) > 0 AS needs_support_attention,
  CASE
    WHEN COALESCE(is_guardrail_blocked, FALSE)
      OR turn_status IN ('MODEL_BLOCKED', 'INVALID_ARGUMENT', 'RATE_LIMITED')
      OR (status_code IN (3, 8) AND NOT REGEXP_CONTAINS(COALESCE(status_message, ''), r'(?i)catalog_configs'))
      OR (turn_kind = 'FILE_UPLOAD' AND COALESCE(is_failed, FALSE))
      THEN 'L1 - User / Input / Policy'
    WHEN COALESCE(tool_failure_count, 0) > 0
      OR COALESCE(is_failed, FALSE)
      THEN 'L2 - Platform / Agent / MCP'
    ELSE 'L0 - Healthy'
  END AS support_tier,
  CASE
    WHEN COALESCE(is_guardrail_blocked, FALSE) OR turn_status = 'MODEL_BLOCKED'
      THEN 'Guardrail / DLP Block'
    WHEN COALESCE(tool_failure_count, 0) > 0
      THEN 'MCP / Agent Tool Failure'
    WHEN (turn_kind = 'FILE_UPLOAD' OR COALESCE(has_uploaded_file, FALSE)) AND COALESCE(is_failed, FALSE)
      THEN 'File Upload Error'
    WHEN turn_kind = 'CONNECTOR' AND COALESCE(is_failed, FALSE)
      THEN 'Connector / OAuth Error'
    WHEN turn_status IN ('PERMISSION_DENIED', 'UNAUTHENTICATED') OR status_code IN (7, 16)
      THEN 'IAM / Auth Denial'
    WHEN turn_status = 'RATE_LIMITED' OR status_code = 8
      THEN 'Quota / Rate Limit'
    WHEN turn_status = 'DEADLINE_EXCEEDED' OR status_code = 4
      THEN 'Timeout / Deadline Exceeded'
    WHEN turn_status = 'UNAVAILABLE' OR status_code = 14
      THEN 'Upstream / Channel Unavailable'
    WHEN REGEXP_CONTAINS(COALESCE(status_message, ''), r'(?i)(datastore|federated|catalog_configs)')
      OR status_code IN (9, 12, 13)
      THEN 'Datastore / Federated Search Error'
    WHEN turn_status = 'INVALID_ARGUMENT' OR status_code = 3
      THEN 'Invalid Request / Input'
    WHEN COALESCE(is_failed, FALSE)
      THEN CONCAT('Platform Error (', COALESCE(turn_status, 'UNKNOWN'), ')')
    WHEN turn_status = 'SKIPPED' OR answer_state = 'SKIPPED'
      THEN 'No Answer / Skipped'
    ELSE 'Healthy'
  END AS issue_category,
  CASE
    WHEN COALESCE(is_guardrail_blocked, FALSE) AND COALESCE(is_prompt_injection, FALSE)
      THEN 'L1/SecOps: Blocked for Prompt Injection / Jailbreak attempt. Review guardrail_categories.'
    WHEN COALESCE(is_guardrail_blocked, FALSE) OR turn_status = 'MODEL_BLOCKED'
      THEN 'L1: Input/output blocked by Model Armor or Sensitive Data Protection. Verify user prompt against DLP policy.'
    WHEN (turn_kind = 'FILE_UPLOAD' OR COALESCE(has_uploaded_file, FALSE)) AND COALESCE(is_failed, FALSE)
      THEN 'L1: File upload rejected (check status_message for unsupported MIME type or session file quota).'
    WHEN COALESCE(tool_failure_count, 0) > 0
      THEN 'L2: MCP/Agent tool call failed (inspect tool_error_messages and tool_call_summary).'
    WHEN turn_kind = 'CONNECTOR' AND COALESCE(is_failed, FALSE)
      THEN 'L2: Data Connector / OAuth token exchange failed (check api_method and status_message).'
    WHEN turn_status IN ('PERMISSION_DENIED', 'UNAUTHENTICATED') OR status_code IN (7, 16)
      THEN 'L1/L2: Permission denied (status_code 7/16). Verify user IAM bindings and connector ACLs.'
    WHEN turn_status = 'RATE_LIMITED' OR status_code = 8
      THEN 'L2: Vertex AI / Discovery Engine quota or rate limit exceeded (status_code 8).'
    WHEN turn_status = 'DEADLINE_EXCEEDED' OR status_code = 4
      THEN 'L2: StreamAssist / Search deadline exceeded (status_code 4). Check downstream agent/connector latency.'
    WHEN turn_status = 'UNAVAILABLE' OR status_code = 14
      THEN 'L2: Upstream channel broken or service unavailable (status_code 14). Check client disconnect or backend health.'
    WHEN REGEXP_CONTAINS(COALESCE(status_message, ''), r'(?i)(datastore|federated|catalog_configs)')
      OR status_code IN (9, 12, 13)
      THEN 'L2: Federated datastore or search precondition error. Check datastore connector health and catalog_configs.'
    WHEN turn_status = 'INVALID_ARGUMENT' OR status_code = 3
      THEN 'L1: Invalid request argument (status_code 3). Check status_message for malformed parameter.'
    WHEN COALESCE(is_failed, FALSE)
      THEN 'L2: Platform or model execution failure. Escalate trace_id and status_message.'
    WHEN turn_status = 'SKIPPED' OR answer_state = 'SKIPPED'
      THEN 'L1: Assistant skipped answer generation (check skipped_reasons, e.g., NON_ASSIST_SEEKING_QUERY_IGNORED or OUT_OF_DOMAIN_QUERY_IGNORED).'
    ELSE 'No action required.'
  END AS l1_runbook_action,
  ARRAY_TO_STRING([
    COALESCE(api_method, turn_kind),
    turn_status,
    IF(COALESCE(is_guardrail_blocked, FALSE), CONCAT('guardrail:', COALESCE(guardrail_categories, 'BLOCKED')), NULL),
    IF(COALESCE(tool_failure_count, 0) > 0, CONCAT('failed_tools:', COALESCE(tool_names, 'unknown'), ' (mcp:', COALESCE(mcp_server_name, 'n/a'), ')'), NULL),
    NULLIF(tool_error_messages, ''),
    NULLIF(status_message, ''),
    NULLIF(error_reason, '')
  ], ' | ') AS issue_summary,
  IF(
    COALESCE(is_actionable_issue, FALSE) OR COALESCE(tool_failure_count, 0) > 0,
    SUBSTR(
      REGEXP_REPLACE(
        REGEXP_REPLACE(
          COALESCE(
            NULLIF(tool_error_messages, ''),
            NULLIF(status_message, ''),
            NULLIF(error_reason, ''),
            IF(COALESCE(is_guardrail_blocked, FALSE), CONCAT('Guardrail Block: ', COALESCE(armor_verdict_reasons, guardrail_categories, 'Policy Match')), NULL),
            NULLIF(skipped_reasons, ''),
            turn_status
          ),
          r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
          '<uuid>'
        ),
        r'\b[0-9]{6,}\b',
        '<id>'
      ),
      1,
      300
    ),
    CAST(NULL AS STRING)
  ) AS error_signature,
  status_code,
  status_message,
  error_reason,
  audit_matched,
  audit_match_method,
  latency_ms,
  llm_calls,
  llm_calls_with_tokens,
  input_tokens,
  output_tokens,
  cached_input_tokens,
  reasoning_tokens,
  total_tokens,
  tool_call_count,
  tool_failure_count,
  tool_names,
  mcp_server_name,
  tool_error_codes,
  tool_error_messages,
  tool_call_summary,
  tool_call_summary AS tool_args_summary,
  tool_output_preview,
  finish_reason,
  reference_count,
  query_chars,
  has_uploaded_file,
  prompt_preview,
  response_preview,
  uploaded_file_names,
  queried_data_stores,
  citation_sources,
  armor_checks,
  armor_blocks,
  armor_findings,
  is_guardrail_blocked,
  guardrail_categories,
  armor_verdict_reasons,
  armor_sdp_info_types,
  is_prompt_injection,
  is_sensitive_data,
  is_safety_violation,
  is_malicious_uri,
  CONCAT(
    'https://console.cloud.google.com/logs/query;query=',
    COALESCE(
      IF(trace_id IS NOT NULL, CONCAT('trace%3D%22', trace_id, '%22'), NULL),
      CONCAT('insertId%3D%22', REGEXP_EXTRACT(turn_id, r'([^:]+)$'), '%22')
    ),
    IF(project_ref IS NOT NULL AND NOT REGEXP_CONTAINS(project_ref, r'^[0-9]+$'), CONCAT('?project=', project_ref), '')
  ) AS cloud_logging_url,
  IF(
    trace_id IS NOT NULL,
    CONCAT(
      'https://console.cloud.google.com/traces/list?tid=',
      trace_id,
      IF(project_ref IS NOT NULL AND NOT REGEXP_CONTAINS(project_ref, r'^[0-9]+$'), CONCAT('&project=', project_ref), '')
    ),
    CAST(NULL AS STRING)
  ) AS cloud_trace_url,
  -- Session-level context pre-joined via window functions so Looker Studio needs no data blend
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_timestamp, turn_id)
  ) AS session_step_number,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    COUNT(1) OVER (PARTITION BY session_id)
  ) AS session_total_turns,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    COUNTIF(is_failed) OVER (PARTITION BY session_id)
  ) AS session_failed_turns,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    COUNTIF(COALESCE(is_actionable_issue, FALSE) OR COALESCE(tool_failure_count, 0) > 0) OVER (PARTITION BY session_id)
  ) AS session_issue_turns,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    COUNTIF(is_guardrail_blocked) OVER (PARTITION BY session_id)
  ) AS session_guardrail_blocks,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    SUM(COALESCE(tool_failure_count, 0)) OVER (PARTITION BY session_id)
  ) AS session_tool_failures,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    SUM(total_tokens) OVER (PARTITION BY session_id)
  ) AS session_total_tokens,
  IF(
    session_id IS NULL,
    CAST(NULL AS INT64),
    TIMESTAMP_DIFF(
      MAX(event_timestamp) OVER (PARTITION BY session_id),
      MIN(event_timestamp) OVER (PARTITION BY session_id),
      SECOND
    )
  ) AS session_duration_seconds,
  IF(
    session_id IS NULL,
    COALESCE(is_actionable_issue, FALSE) OR COALESCE(tool_failure_count, 0) > 0,
    LOGICAL_OR(COALESCE(is_actionable_issue, FALSE) OR COALESCE(tool_failure_count, 0) > 0) OVER (PARTITION BY session_id)
  ) AS session_has_issue
FROM `{{mart}}.fct_turns`
