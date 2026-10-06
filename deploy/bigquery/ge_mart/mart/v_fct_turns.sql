-- VibeLift turn fact: one row per user interaction with Gemini Enterprise or a deployed agent.
-- Built on the curated views. Not a port of the stage support mart (vw_support_master_events):
--   * Inference tokens are aggregated per trace and attached to ONE turn per trace (the first
--     chat row). Stage FULL OUTER JOINed on trace_id, copying the same tokens onto every
--     activity row in the trace.
--   * Inference calls with no activity row become AGENT_INFERENCE turns (deployed ADK agents).
--   * Audit calls are matched 1:1 (two mutual-best rounds) by trace, else by principal + method
--     with the audit entry up to audit_lag before (request start) or audit_lead after the
--     activity entry. Unmatched FAILED interactive audit calls become AUDIT_ONLY turns, so
--     failures only the audit log records (quota, permission, agent errors) are not lost.
--     Unmatched successful calls are not turns by default: GE issues several StreamAssist calls
--     per visible chat turn (stage: 28 audit calls for 9 activity turns).
--   * Model Armor checks join by assist token, else by trace. ModelArmorAudit activity rows
--     (GE's own guardrail audit entries) join by trace as evidence; they are never turns.
--   * Unknown stays NULL: tokens, model, agent, guardrail outcome. turn_status is UNKNOWN when
--     no source says how the turn ended.
WITH activity AS (
  SELECT * FROM `{{curated}}.v_user_activity_curated`
),
turn_events AS (
  SELECT
    *,
    IF(trace_id IS NULL, 1, ROW_NUMBER() OVER (
      PARTITION BY trace_id
      ORDER BY IF(activity_category = 'CHAT', 0, 1), event_timestamp, event_id
    )) AS trace_rank
  FROM activity
  WHERE is_user_turn
),
guardrail_audit AS (
  SELECT
    trace_id,
    COUNT(1) AS guardrail_audit_events,
    LOGICAL_OR(COALESCE(is_guardrail_block, FALSE)) AS guardrail_audit_blocked
  FROM activity
  WHERE activity_category = 'GUARDRAIL_AUDIT' AND trace_id IS NOT NULL
  GROUP BY trace_id
),
inference_raw AS (
  SELECT
    COALESCE(trace_id, event_id) AS inference_key,
    ANY_VALUE(trace_id) AS trace_id,
    ARRAY_AGG(span_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS span_id,
    MIN(event_timestamp) AS first_call_at,
    MAX(event_timestamp) AS last_call_at,
    COUNT(1) AS llm_calls,
    COUNTIF(total_tokens IS NOT NULL) AS llm_calls_with_tokens,
    SUM(input_tokens) AS input_tokens,
    SUM(output_tokens) AS output_tokens,
    SUM(cached_input_tokens) AS cached_input_tokens,
    SUM(reasoning_tokens) AS reasoning_tokens,
    SUM(total_tokens) AS total_tokens,
    SUM(tool_call_count) AS tool_call_count,
    SUM(tool_failure_count) AS tool_failure_count,
    STRING_AGG(tool_names, ',') AS raw_tool_names,
    ARRAY_AGG(model_name IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS model_name,
    ARRAY_AGG(agent_name IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS agent_name,
    ARRAY_AGG(agent_resource_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS agent_resource_id,
    ARRAY_AGG(conversation_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS conversation_id,
    ARRAY_AGG(user_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS user_id,
    ARRAY_AGG(engine_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS engine_id,
    ARRAY_AGG(location IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS location,
    STRING_AGG(mcp_server_name, ',') AS raw_mcp_server_name,
    STRING_AGG(tool_error_codes, ', ') AS raw_tool_error_codes,
    SUBSTR(STRING_AGG(tool_error_messages, ' | ' ORDER BY event_timestamp), 1, 2000) AS tool_error_messages,
    SUBSTR(STRING_AGG(tool_call_summary, ' | ' ORDER BY event_timestamp), 1, 2000) AS tool_call_summary,
    SUBSTR(STRING_AGG(tool_output_preview, ' | ' ORDER BY event_timestamp), 1, 2000) AS tool_output_preview,
    ARRAY_AGG(finish_reason IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS finish_reason
  FROM `{{curated}}.v_agentic_operations_curated`
  GROUP BY inference_key
),
inference AS (
  SELECT
    * EXCEPT (raw_tool_names, raw_mcp_server_name, raw_tool_error_codes),
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT tn
      FROM UNNEST(SPLIT(raw_tool_names, ',')) AS tn
      WHERE tn != ''
      ORDER BY tn
    ), ','), '') AS tool_names,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT srv
      FROM UNNEST(SPLIT(raw_mcp_server_name, ',')) AS srv
      WHERE srv != ''
      ORDER BY srv
    ), ','), '') AS mcp_server_name,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT TRIM(ec)
      FROM UNNEST(SPLIT(raw_tool_error_codes, ',')) AS ec
      WHERE TRIM(ec) != ''
      ORDER BY 1
    ), ', '), '') AS tool_error_codes
  FROM inference_raw
),
audit AS (
  SELECT * FROM `{{curated}}.v_consolidated_audit_log` WHERE is_interactive
),
audit_pairs AS (
  SELECT
    t.event_id AS turn_event_id,
    a.event_id AS audit_event_id,
    'TRACE' AS match_method,
    0 AS tier,
    IF((COALESCE(t.status_code, 0) != 0) = a.is_error, 0, 1) AS status_mismatch,
    ABS(TIMESTAMP_DIFF(a.event_timestamp, t.event_timestamp, MILLISECOND)) AS gap_ms
  FROM turn_events AS t
  JOIN audit AS a
    ON t.trace_id = a.trace_id AND t.activity_category = a.interaction_kind
  UNION ALL
  -- The audit entry is written when the request starts and the activity entry when it
  -- finishes, so the audit row may precede the activity row by a long agent response.
  SELECT
    t.event_id,
    a.event_id,
    'PRINCIPAL_TIME',
    1,
    IF((COALESCE(t.status_code, 0) != 0) = a.is_error, 0, 1),
    ABS(TIMESTAMP_DIFF(a.event_timestamp, t.event_timestamp, MILLISECOND))
  FROM turn_events AS t
  JOIN audit AS a
    ON LOWER(t.user_email) = LOWER(a.principal_email)
   AND t.activity_category = a.interaction_kind
   AND COALESCE(t.engine_id, a.engine_id, '') = COALESCE(a.engine_id, t.engine_id, '')
  WHERE TIMESTAMP_DIFF(t.event_timestamp, a.event_timestamp, MILLISECOND) BETWEEN -{{audit_lead_ms}} AND {{audit_lag_ms}}
  UNION ALL
  -- When a GE app elides useriamprincipal in the activity sink ("<elided>"), t.user_email is NULL;
  -- pair by engine + interaction kind + time window so COALESCE(user_email, audit_principal_email)
  -- recovers the real principal from the audit log.
  SELECT
    t.event_id,
    a.event_id,
    'ENGINE_TIME',
    2,
    IF((COALESCE(t.status_code, 0) != 0) = a.is_error, 0, 1),
    ABS(TIMESTAMP_DIFF(a.event_timestamp, t.event_timestamp, MILLISECOND))
  FROM turn_events AS t
  JOIN audit AS a
    ON t.user_email IS NULL
   AND t.engine_id = a.engine_id
   AND t.activity_category = a.interaction_kind
  WHERE TIMESTAMP_DIFF(t.event_timestamp, a.event_timestamp, MILLISECOND) BETWEEN -{{audit_lead_ms}} AND {{audit_lag_ms}}
),
-- Two mutual-best rounds in one chain of window functions. BigQuery re-evaluates a CTE at every
-- reference, so the rounds must not re-read audit_pairs (NOT IN / self-joins multiply the plan).
ranked_audit_pairs AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY turn_event_id ORDER BY tier, status_mismatch, gap_ms, audit_event_id) AS rn_turn,
    ROW_NUMBER() OVER (PARTITION BY audit_event_id ORDER BY tier, status_mismatch, gap_ms, turn_event_id) AS rn_audit
  FROM audit_pairs
),
flagged_audit_pairs AS (
  SELECT
    *,
    rn_turn = 1 AND rn_audit = 1 AS is_round1,
    LOGICAL_OR(rn_turn = 1 AND rn_audit = 1) OVER (PARTITION BY turn_event_id) AS turn_taken,
    LOGICAL_OR(rn_turn = 1 AND rn_audit = 1) OVER (PARTITION BY audit_event_id) AS audit_taken
  FROM ranked_audit_pairs
),
-- Second round over what round 1 left, so bursts of near-simultaneous calls (several searches
-- within a second) still pair up 1:1. Ineligible pairs sit in their own window partition.
ranked_audit_pairs_round2 AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY turn_event_id, turn_taken OR audit_taken
      ORDER BY tier, status_mismatch, gap_ms, audit_event_id
    ) AS rn_turn2,
    ROW_NUMBER() OVER (
      PARTITION BY audit_event_id, turn_taken OR audit_taken
      ORDER BY tier, status_mismatch, gap_ms, turn_event_id
    ) AS rn_audit2
  FROM flagged_audit_pairs
),
audit_matches AS (
  SELECT turn_event_id, audit_event_id, match_method
  FROM ranked_audit_pairs_round2
  WHERE is_round1 OR (NOT turn_taken AND NOT audit_taken AND rn_turn2 = 1 AND rn_audit2 = 1)
),
-- Every interactive audit call with the turn it matched (NULL when unmatched). Read once by the
-- activity join and once for AUDIT_ONLY turns.
audit_resolved AS (
  SELECT a.*, m.turn_event_id AS matched_turn_event_id, m.match_method
  FROM audit AS a
  LEFT JOIN audit_matches AS m ON m.audit_event_id = a.event_id
),
armor AS (
  SELECT * FROM `{{curated}}.v_model_armor_curated`
),
armor_by_token AS (
  SELECT
    assist_token,
    COUNT(1) AS armor_checks,
    COUNTIF(is_blocked) AS armor_blocks,
    COUNTIF(is_finding) AS armor_findings,
    LOGICAL_OR(is_prompt_injection) AS is_prompt_injection,
    LOGICAL_OR(is_sensitive_data) AS is_sensitive_data,
    LOGICAL_OR(is_safety_violation) AS is_safety_violation,
    LOGICAL_OR(is_malicious_uri) AS is_malicious_uri,
    STRING_AGG(DISTINCT violation_category, ',' ORDER BY violation_category) AS guardrail_categories,
    STRING_AGG(DISTINCT NULLIF(verdict_reason, ''), ' | ' ORDER BY NULLIF(verdict_reason, '')) AS armor_verdict_reasons,
    STRING_AGG(DISTINCT sdp_info_types, ', ' ORDER BY sdp_info_types) AS armor_sdp_info_types,
    SUBSTR(NULLIF(TRIM(REGEXP_REPLACE(
      STRING_AGG(
        IF(operation_type = 'SANITIZE_MODEL_RESPONSE', sanitized_text, NULL),
        '' ORDER BY event_timestamp, insert_id
      ),
      r'\[\[section:[^\]]*\]\]\s*',
      ''
    )), ''), 1, 2000) AS armor_response_preview
  FROM armor
  WHERE assist_token IS NOT NULL
  GROUP BY assist_token
),
armor_by_trace AS (
  SELECT
    trace_id,
    COUNT(1) AS armor_checks,
    COUNTIF(is_blocked) AS armor_blocks,
    COUNTIF(is_finding) AS armor_findings,
    LOGICAL_OR(is_prompt_injection) AS is_prompt_injection,
    LOGICAL_OR(is_sensitive_data) AS is_sensitive_data,
    LOGICAL_OR(is_safety_violation) AS is_safety_violation,
    LOGICAL_OR(is_malicious_uri) AS is_malicious_uri,
    STRING_AGG(DISTINCT violation_category, ',' ORDER BY violation_category) AS guardrail_categories,
    STRING_AGG(DISTINCT NULLIF(verdict_reason, ''), ' | ' ORDER BY NULLIF(verdict_reason, '')) AS armor_verdict_reasons,
    STRING_AGG(DISTINCT sdp_info_types, ', ' ORDER BY sdp_info_types) AS armor_sdp_info_types,
    SUBSTR(NULLIF(TRIM(REGEXP_REPLACE(
      STRING_AGG(
        IF(operation_type = 'SANITIZE_MODEL_RESPONSE', sanitized_text, NULL),
        '' ORDER BY event_timestamp, insert_id
      ),
      r'\[\[section:[^\]]*\]\]\s*',
      ''
    )), ''), 1, 2000) AS armor_response_preview
  FROM armor
  WHERE assist_token IS NULL AND trace_id IS NOT NULL
  GROUP BY trace_id
),
armor_file_pairs AS (
  SELECT
    t.event_id AS turn_event_id,
    m.*
  FROM turn_events AS t
  JOIN armor AS m
    ON t.activity_category = 'FILE_UPLOAD'
   AND t.uploaded_file_names IS NOT NULL
   AND t.assist_token IS NULL
   AND STARTS_WITH(COALESCE(m.sanitized_text, ''), 'Sanitized file content')
   AND ABS(TIMESTAMP_DIFF(m.event_timestamp, t.event_timestamp, MILLISECOND)) <= 10000
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY m.event_id
    ORDER BY ABS(TIMESTAMP_DIFF(m.event_timestamp, t.event_timestamp, MILLISECOND)), t.event_id
  ) = 1
),
armor_by_file_upload AS (
  SELECT
    turn_event_id,
    COUNT(1) AS armor_checks,
    COUNTIF(is_blocked) AS armor_blocks,
    COUNTIF(is_finding) AS armor_findings,
    LOGICAL_OR(is_prompt_injection) AS is_prompt_injection,
    LOGICAL_OR(is_sensitive_data) AS is_sensitive_data,
    LOGICAL_OR(is_safety_violation) AS is_safety_violation,
    LOGICAL_OR(is_malicious_uri) AS is_malicious_uri,
    STRING_AGG(DISTINCT violation_category, ',' ORDER BY violation_category) AS guardrail_categories,
    STRING_AGG(DISTINCT NULLIF(verdict_reason, ''), ' | ' ORDER BY NULLIF(verdict_reason, '')) AS armor_verdict_reasons,
    STRING_AGG(DISTINCT sdp_info_types, ', ' ORDER BY sdp_info_types) AS armor_sdp_info_types
  FROM armor_file_pairs
  GROUP BY turn_event_id
),
activity_joined AS (
  SELECT
    t.*,
    a.event_timestamp AS audit_timestamp,
    a.span_id AS audit_span_id,
    a.status_class AS audit_status_class,
    a.status_code AS audit_status_code,
    a.status_message AS audit_status_message,
    a.error_reason AS audit_error_reason,
    a.principal_email AS audit_principal_email,
    a.session_id AS audit_session_id,
    a.project_ref AS audit_project_ref,
    a.location AS audit_location,
    a.engine_id AS audit_engine_id,
    a.agent_id AS audit_agent_id,
    a.match_method AS audit_match_method,
    i.first_call_at, i.last_call_at, i.span_id AS inference_span_id,
    i.llm_calls, i.llm_calls_with_tokens, i.input_tokens, i.output_tokens, i.cached_input_tokens,
    i.reasoning_tokens, i.total_tokens, i.tool_call_count, i.tool_failure_count, i.tool_names,
    i.tool_error_codes, i.tool_error_messages, i.tool_call_summary, i.tool_output_preview,
    i.model_name AS inference_model_name, i.agent_name AS inference_agent_name,
    i.conversation_id, i.location AS inference_location, i.engine_id AS inference_engine_id,
    i.mcp_server_name, i.finish_reason,
    COALESCE(tok.armor_checks, tr.armor_checks, fu.armor_checks) AS armor_checks,
    COALESCE(tok.armor_blocks, tr.armor_blocks, fu.armor_blocks) AS armor_blocks,
    COALESCE(tok.armor_findings, tr.armor_findings, fu.armor_findings) AS armor_findings,
    COALESCE(tok.is_prompt_injection, tr.is_prompt_injection, fu.is_prompt_injection) AS armor_prompt_injection,
    COALESCE(tok.is_sensitive_data, tr.is_sensitive_data, fu.is_sensitive_data) AS armor_sensitive_data,
    COALESCE(tok.is_safety_violation, tr.is_safety_violation, fu.is_safety_violation) AS armor_safety_violation,
    COALESCE(tok.is_malicious_uri, tr.is_malicious_uri, fu.is_malicious_uri) AS armor_malicious_uri,
    COALESCE(tok.guardrail_categories, tr.guardrail_categories, fu.guardrail_categories) AS armor_categories,
    COALESCE(tok.armor_verdict_reasons, tr.armor_verdict_reasons, fu.armor_verdict_reasons) AS armor_verdict_reasons,
    COALESCE(tok.armor_sdp_info_types, tr.armor_sdp_info_types, fu.armor_sdp_info_types) AS armor_sdp_info_types,
    COALESCE(tok.armor_response_preview, tr.armor_response_preview) AS armor_response_preview,
    g.guardrail_audit_events,
    g.guardrail_audit_blocked
  FROM turn_events AS t
  LEFT JOIN audit_resolved AS a ON a.matched_turn_event_id = t.event_id
  LEFT JOIN inference AS i ON t.trace_rank = 1 AND i.trace_id = t.trace_id
  LEFT JOIN armor_by_token AS tok ON tok.assist_token = t.assist_token
  LEFT JOIN armor_by_trace AS tr ON t.assist_token IS NULL AND t.trace_rank = 1 AND tr.trace_id = t.trace_id
  LEFT JOIN armor_by_file_upload AS fu ON t.assist_token IS NULL AND fu.turn_event_id = t.event_id
  LEFT JOIN guardrail_audit AS g ON t.trace_rank = 1 AND g.trace_id = t.trace_id
),
activity_turns AS (
  SELECT
    event_id AS turn_id,
    'ACTIVITY' AS turn_source,
    activity_category AS turn_kind,
    event_timestamp,
    trace_id,
    COALESCE(span_id, inference_span_id, audit_span_id) AS span_id,
    COALESCE(session_id, audit_session_id, conversation_id) AS session_id,
    COALESCE(user_email, audit_principal_email) AS user_email,
    api_method,
    COALESCE(project_ref, audit_project_ref) AS project_ref,
    COALESCE(location, audit_location, inference_location) AS location,
    COALESCE(engine_id, audit_engine_id, inference_engine_id) AS engine_id,
    -- 'Core assistant' is derived from the log's coreassistant=true flag, not a default.
    COALESCE(agent_display_name, inference_agent_name, IF(is_core_assistant, 'Core assistant', NULL)) AS agent_name,
    COALESCE(agent_id, audit_agent_id) AS agent_id,
    is_core_assistant,
    COALESCE(inference_model_name, model_name) AS model_name,
    model_selection_mode,
    answer_state,
    skipped_reasons,
    CASE
      WHEN status_class IS NOT NULL THEN status_class
      WHEN audit_status_class IS NOT NULL AND audit_status_class != 'OK' THEN audit_status_class
      WHEN answer_state IN ('SUCCEEDED', 'COMPLETED') THEN 'SUCCESS'
      WHEN answer_state = 'SKIPPED' THEN 'SKIPPED'
      WHEN answer_state = 'FAILED' THEN 'FAILED'
      WHEN audit_status_class = 'OK' THEN 'SUCCESS'
      WHEN activity_category = 'SEARCH' AND attribution_token IS NOT NULL THEN 'SUCCESS'
      ELSE 'UNKNOWN'
    END AS turn_status,
    COALESCE(NULLIF(status_code, 0), NULLIF(audit_status_code, 0)) AS status_code,
    COALESCE(status_message, audit_status_message) AS status_message,
    audit_error_reason AS error_reason,
    audit_match_method IS NOT NULL AS audit_matched,
    audit_match_method,
    NULLIF(TIMESTAMP_DIFF(
      GREATEST(
        event_timestamp,
        COALESCE(audit_timestamp, event_timestamp),
        COALESCE(first_call_at, event_timestamp),
        COALESCE(last_call_at, event_timestamp)
      ),
      LEAST(
        event_timestamp,
        COALESCE(audit_timestamp, event_timestamp),
        COALESCE(first_call_at, event_timestamp),
        COALESCE(last_call_at, event_timestamp)
      ),
      MILLISECOND
    ), 0) AS latency_ms,
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
    finish_reason,
    reference_count,
    query_chars,
    has_uploaded_file,
    prompt_preview,
    COALESCE(response_preview, armor_response_preview) AS response_preview,
    uploaded_file_names,
    COALESCE(
      queried_data_stores,
      NULLIF(ARRAY_TO_STRING(ARRAY(
        SELECT DISTINCT ds FROM UNNEST(ARRAY_CONCAT(
          IFNULL(REGEXP_EXTRACT_ALL(COALESCE(status_message, audit_status_message, ''), r'collections/([A-Za-z0-9_-]+)/dataConnector'), CAST([] AS ARRAY<STRING>)),
          IFNULL(REGEXP_EXTRACT_ALL(COALESCE(status_message, audit_status_message, ''), r'dataStores/([A-Za-z0-9_-]+)'), CAST([] AS ARRAY<STRING>))
        )) AS ds
        WHERE ds IS NOT NULL AND ds != ''
        ORDER BY ds
      ), ', '), '')
    ) AS queried_data_stores,
    citation_sources,
    tool_error_codes,
    tool_error_messages,
    tool_call_summary,
    tool_output_preview,
    armor_checks,
    armor_blocks,
    IF(
      armor_checks IS NULL AND sanitization_verdict IS NULL AND guardrail_audit_events IS NULL
        AND NOT REGEXP_CONTAINS(COALESCE(skipped_reasons, ''), r'CUSTOMER_POLICY_VIOLATION'),
      NULL,
      COALESCE(armor_blocks, 0) > 0
        OR COALESCE(is_guardrail_block, FALSE)
        OR COALESCE(guardrail_audit_blocked, FALSE)
        OR REGEXP_CONTAINS(COALESCE(skipped_reasons, ''), r'CUSTOMER_POLICY_VIOLATION')
    ) AS is_guardrail_blocked,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT c FROM UNNEST(SPLIT(CONCAT(
        COALESCE(armor_categories, ''), ',',
        IF(
          COALESCE(is_guardrail_block, FALSE)
            OR COALESCE(guardrail_audit_blocked, FALSE)
            OR REGEXP_CONTAINS(COALESCE(skipped_reasons, ''), r'CUSTOMER_POLICY_VIOLATION'),
          'GE_GUARDRAIL_BLOCK', ''
        )
      ), ',')) AS c
      WHERE c != ''
      ORDER BY c
    ), ','), '') AS guardrail_categories,
    armor_prompt_injection AS is_prompt_injection,
    armor_sensitive_data AS is_sensitive_data,
    armor_safety_violation AS is_safety_violation,
    armor_malicious_uri AS is_malicious_uri,
    armor_findings,
    armor_verdict_reasons,
    armor_sdp_info_types
  FROM activity_joined
),
inference_turns AS (
  SELECT
    CONCAT('INFERENCE:', i.inference_key) AS turn_id,
    'AGENT_INFERENCE' AS turn_source,
    'AGENT_CALL' AS turn_kind,
    i.first_call_at AS event_timestamp,
    i.trace_id,
    i.span_id,
    i.conversation_id AS session_id,
    IF(REGEXP_CONTAINS(COALESCE(i.user_id, ''), r'@'), i.user_id, NULL) AS user_email,
    CAST(NULL AS STRING) AS api_method,
    CAST(NULL AS STRING) AS project_ref,
    i.location,
    i.engine_id,
    i.agent_name,
    i.agent_resource_id AS agent_id,
    CAST(NULL AS BOOL) AS is_core_assistant,
    i.model_name,
    CAST(NULL AS STRING) AS model_selection_mode,
    CAST(NULL AS STRING) AS answer_state,
    CAST(NULL AS STRING) AS skipped_reasons,
    CASE
      WHEN i.finish_reason IS NULL THEN 'UNKNOWN'
      WHEN UPPER(i.finish_reason) IN ('STOP', 'END_TURN', 'MAX_TOKENS', 'LENGTH', 'TOOL_CALLS', 'FUNCTION_CALL', 'TOOL_CALL') THEN 'SUCCESS'
      WHEN UPPER(i.finish_reason) IN ('SAFETY', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'RECITATION', 'IMAGE_SAFETY', 'CONTENT_FILTER') THEN 'MODEL_BLOCKED'
      WHEN UPPER(i.finish_reason) IN ('ERROR', 'MALFORMED_FUNCTION_CALL', 'OTHER') THEN 'MODEL_ERROR'
      ELSE 'UNKNOWN'
    END AS turn_status,
    CAST(NULL AS INT64) AS status_code,
    CAST(NULL AS STRING) AS status_message,
    CAST(NULL AS STRING) AS error_reason,
    FALSE AS audit_matched,
    CAST(NULL AS STRING) AS audit_match_method,
    NULLIF(TIMESTAMP_DIFF(i.last_call_at, i.first_call_at, MILLISECOND), 0) AS latency_ms,
    i.llm_calls,
    i.llm_calls_with_tokens,
    i.input_tokens,
    i.output_tokens,
    i.cached_input_tokens,
    i.reasoning_tokens,
    i.total_tokens,
    i.tool_call_count,
    i.tool_failure_count,
    i.tool_names,
    i.mcp_server_name,
    i.finish_reason,
    CAST(NULL AS INT64) AS reference_count,
    CAST(NULL AS INT64) AS query_chars,
    CAST(NULL AS BOOL) AS has_uploaded_file,
    CAST(NULL AS STRING) AS prompt_preview,
    CAST(NULL AS STRING) AS response_preview,
    CAST(NULL AS STRING) AS uploaded_file_names,
    CAST(NULL AS STRING) AS queried_data_stores,
    CAST(NULL AS STRING) AS citation_sources,
    i.tool_error_codes,
    i.tool_error_messages,
    i.tool_call_summary,
    i.tool_output_preview,
    tr.armor_checks,
    tr.armor_blocks,
    IF(tr.armor_checks IS NULL, NULL, tr.armor_blocks > 0) AS is_guardrail_blocked,
    tr.guardrail_categories,
    tr.is_prompt_injection,
    tr.is_sensitive_data,
    tr.is_safety_violation,
    tr.is_malicious_uri,
    tr.armor_findings,
    tr.armor_verdict_reasons,
    tr.armor_sdp_info_types
  FROM inference AS i
  LEFT JOIN turn_events AS t ON t.trace_rank = 1 AND t.trace_id = i.trace_id
  LEFT JOIN armor_by_trace AS tr ON tr.trace_id = i.trace_id
  WHERE t.event_id IS NULL
),
audit_only_turns AS (
  SELECT
    a.event_id AS turn_id,
    'AUDIT_ONLY' AS turn_source,
    a.interaction_kind AS turn_kind,
    a.event_timestamp,
    a.trace_id,
    a.span_id,
    a.session_id,
    a.principal_email AS user_email,
    a.method_short_name AS api_method,
    a.project_ref,
    a.location,
    a.engine_id,
    CAST(NULL AS STRING) AS agent_name,
    a.agent_id,
    CAST(NULL AS BOOL) AS is_core_assistant,
    CAST(NULL AS STRING) AS model_name,
    CAST(NULL AS STRING) AS model_selection_mode,
    CAST(NULL AS STRING) AS answer_state,
    CAST(NULL AS STRING) AS skipped_reasons,
    IF(a.status_class = 'OK', 'SUCCESS', a.status_class) AS turn_status,
    NULLIF(a.status_code, 0) AS status_code,
    a.status_message,
    a.error_reason,
    TRUE AS audit_matched,
    'AUDIT_ONLY' AS audit_match_method,
    CAST(NULL AS INT64) AS latency_ms,
    CAST(NULL AS INT64) AS llm_calls,
    CAST(NULL AS INT64) AS llm_calls_with_tokens,
    CAST(NULL AS INT64) AS input_tokens,
    CAST(NULL AS INT64) AS output_tokens,
    CAST(NULL AS INT64) AS cached_input_tokens,
    CAST(NULL AS INT64) AS reasoning_tokens,
    CAST(NULL AS INT64) AS total_tokens,
    CAST(NULL AS INT64) AS tool_call_count,
    CAST(NULL AS INT64) AS tool_failure_count,
    CAST(NULL AS STRING) AS tool_names,
    CAST(NULL AS STRING) AS mcp_server_name,
    CAST(NULL AS STRING) AS finish_reason,
    CAST(NULL AS INT64) AS reference_count,
    CAST(NULL AS INT64) AS query_chars,
    CAST(NULL AS BOOL) AS has_uploaded_file,
    CONCAT(
      '[Audit-only ',
      COALESCE(a.method_short_name, 'API call'),
      IF(
        NULLIF(a.resource_name, '') IS NOT NULL,
        CONCAT(' on ', REGEXP_REPLACE(a.resource_name, r'^projects/[^/]+/locations/[^/]+/collections/', '')),
        ''
      ),
      ']'
    ) AS prompt_preview,
    CAST(NULL AS STRING) AS response_preview,
    CAST(NULL AS STRING) AS uploaded_file_names,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT ds FROM UNNEST(ARRAY_CONCAT(
        IFNULL(REGEXP_EXTRACT_ALL(COALESCE(a.status_message, ''), r'collections/([A-Za-z0-9_-]+)/dataConnector'), CAST([] AS ARRAY<STRING>)),
        IFNULL(REGEXP_EXTRACT_ALL(COALESCE(a.status_message, ''), r'dataStores/([A-Za-z0-9_-]+)'), CAST([] AS ARRAY<STRING>))
      )) AS ds
      WHERE ds IS NOT NULL AND ds != ''
      ORDER BY ds
    ), ', '), '') AS queried_data_stores,
    CAST(NULL AS STRING) AS citation_sources,
    CAST(NULL AS STRING) AS tool_error_codes,
    CAST(NULL AS STRING) AS tool_error_messages,
    CAST(NULL AS STRING) AS tool_call_summary,
    CAST(NULL AS STRING) AS tool_output_preview,
    CAST(NULL AS INT64) AS armor_checks,
    CAST(NULL AS INT64) AS armor_blocks,
    IF(
      REGEXP_CONTAINS(COALESCE(a.status_message, ''), r'(?i)(Sensitive Data Protection|Model Armor|guardrail)'),
      TRUE,
      CAST(NULL AS BOOL)
    ) AS is_guardrail_blocked,
    IF(
      REGEXP_CONTAINS(COALESCE(a.status_message, ''), r'(?i)(Sensitive Data Protection|Model Armor|guardrail)'),
      'GE_GUARDRAIL_BLOCK',
      CAST(NULL AS STRING)
    ) AS guardrail_categories,
    CAST(NULL AS BOOL) AS is_prompt_injection,
    IF(
      REGEXP_CONTAINS(COALESCE(a.status_message, ''), r'(?i)Sensitive Data Protection'),
      TRUE,
      CAST(NULL AS BOOL)
    ) AS is_sensitive_data,
    CAST(NULL AS BOOL) AS is_safety_violation,
    CAST(NULL AS BOOL) AS is_malicious_uri,
    CAST(NULL AS INT64) AS armor_findings,
    CAST(NULL AS STRING) AS armor_verdict_reasons,
    CAST(NULL AS STRING) AS armor_sdp_info_types
  FROM audit_resolved AS a
  WHERE a.matched_turn_event_id IS NULL
    AND ({{include_audit_only_successes}} OR a.is_error)
),
all_turns_raw AS (
  SELECT * FROM activity_turns
  UNION ALL
  SELECT * FROM inference_turns
  UNION ALL
  SELECT * FROM audit_only_turns
),
-- Normalize engine_id, location, and user_email across turns that share a non-null session_id so
-- a session with a FILE_UPLOAD / AUDIT_ONLY / AGENT_INFERENCE turn missing engine_id never splits.
all_turns AS (
  SELECT
    * EXCEPT (location, engine_id, user_email),
    IF(
      session_id IS NULL,
      location,
      COALESCE(location, MAX(location) OVER (PARTITION BY session_id))
    ) AS location,
    IF(
      session_id IS NULL,
      engine_id,
      COALESCE(engine_id, MAX(engine_id) OVER (PARTITION BY session_id))
    ) AS engine_id,
    IF(
      session_id IS NULL,
      user_email,
      COALESCE(user_email, MAX(user_email) OVER (PARTITION BY session_id))
    ) AS user_email
  FROM all_turns_raw
)
SELECT
  turn_id,
  turn_source,
  turn_kind,
  event_timestamp,
  DATE(event_timestamp) AS event_date,
  trace_id,
  span_id,
  session_id,
  user_email,
  location,
  engine_id,
  IF(engine_id IS NULL, NULL, CONCAT(COALESCE(location, '?'), '/', engine_id)) AS engine_key,
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
  turn_status NOT IN ('SUCCESS', 'SKIPPED', 'UNKNOWN', 'CANCELLED') AS is_failed,
  turn_status NOT IN ('SUCCESS', 'SKIPPED', 'UNKNOWN', 'CANCELLED') OR COALESCE(is_guardrail_blocked, FALSE) AS is_actionable_issue,
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
  finish_reason,
  reference_count,
  query_chars,
  has_uploaded_file,
  prompt_preview,
  response_preview,
  uploaded_file_names,
  queried_data_stores,
  citation_sources,
  tool_error_codes,
  tool_error_messages,
  tool_call_summary,
  tool_output_preview,
  armor_checks,
  armor_blocks,
  is_guardrail_blocked,
  guardrail_categories,
  is_prompt_injection,
  is_sensitive_data,
  is_safety_violation,
  is_malicious_uri,
  armor_findings,
  armor_verdict_reasons,
  armor_sdp_info_types
FROM all_turns
