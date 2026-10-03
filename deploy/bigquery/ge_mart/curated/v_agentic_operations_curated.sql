-- Agent inference operations (gen_ai.client.inference.operation.details), one row per LLM call.
-- Ported from gemini-enterprise-stage ds_ge_curated_staging.v_agentic_operations_curated with fixes:
--   * Tokens stay NULL when the log does not report them (stage coalesced to 0).
--   * Adds cached-input and reasoning tokens and the model name when the log carries them.
--   * Reads the OTel attributes from jsonPayload (Discovery Engine sink) or from labels
--     (Agent Engine sink, where every attribute is a string label).
--   * Tool calls are counted from this call's output messages; tool failures and MCP metadata
--     come only from the newest input message. Stage scanned the whole input history, so every
--     earlier tool call and failure was counted again on each later turn.
--   * No invented defaults ('STANDALONE_INFERENCE', 'High Code Agent', 'STOP').
--   * No message content, tool inputs, error text or authorize URLs.
WITH ops AS (
  SELECT
    timestamp AS event_timestamp,
    LOWER(REGEXP_EXTRACT(trace, r'([0-9a-fA-F]{32})$')) AS trace_id,
    insertId AS insert_id,
    jsonPayload AS jp,
    labels AS lb,
    resource AS res
  FROM ({{src_inference}})
),
fields AS (
  SELECT
    *,
    COALESCE(LAX_INT64(jp.gen_ai_usage_input_tokens), LAX_INT64(lb.gen_ai_usage_input_tokens)) AS input_tokens,
    COALESCE(LAX_INT64(jp.gen_ai_usage_output_tokens), LAX_INT64(lb.gen_ai_usage_output_tokens)) AS output_tokens,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_cache_read_input_tokens),
      LAX_INT64(lb.gen_ai_usage_cache_read_input_tokens)
    ) AS cached_input_tokens,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_reasoning_tokens),
      LAX_INT64(jp.gen_ai_usage_thoughts_tokens),
      LAX_INT64(lb.gen_ai_usage_reasoning_tokens),
      LAX_INT64(lb.gen_ai_usage_thoughts_tokens)
    ) AS reasoning_tokens,
    JSON_QUERY_ARRAY(jp.gen_ai_input_messages) AS input_messages,
    JSON_QUERY_ARRAY(jp.gen_ai_output_messages) AS output_messages
  FROM ops
)
SELECT
  event_timestamp,
  DATE(event_timestamp) AS event_date,
  CONCAT('INFERENCE:', COALESCE(insert_id, CAST(UNIX_MICROS(event_timestamp) AS STRING))) AS event_id,
  trace_id,
  insert_id,
  NULLIF(COALESCE(LAX_STRING(jp.gen_ai_conversation_id), LAX_STRING(lb.gen_ai_conversation_id)), '') AS conversation_id,
  NULLIF(COALESCE(LAX_STRING(jp.user_id), LAX_STRING(lb.user_id)), '') AS user_id,
  NULLIF(COALESCE(LAX_STRING(jp.gen_ai_agent_name), LAX_STRING(lb.gen_ai_agent_name)), '') AS agent_name,
  COALESCE(
    LAX_STRING(res.labels.agent_id),
    LAX_STRING(res.labels.reasoning_engine_id),
    LAX_STRING(res.labels.assistant_id)
  ) AS agent_resource_id,
  LAX_STRING(res.labels.engine_id) AS engine_id,
  LAX_STRING(res.labels.location) AS location,
  COALESCE(
    LAX_STRING(jp.gen_ai_response_model),
    LAX_STRING(jp.gen_ai_request_model),
    LAX_STRING(lb.gen_ai_response_model),
    LAX_STRING(lb.gen_ai_request_model)
  ) AS model_name,
  input_tokens,
  output_tokens,
  cached_input_tokens,
  reasoning_tokens,
  IF(input_tokens IS NULL OR output_tokens IS NULL, NULL, input_tokens + output_tokens) AS total_tokens,
  COALESCE(
    LAX_STRING(jp.gen_ai_response_finish_reasons[0]),
    LAX_STRING(jp.gen_ai_output_messages[0].finish_reason),
    REGEXP_EXTRACT(LAX_STRING(lb.gen_ai_response_finish_reasons), r'([A-Za-z_]+)')
  ) AS finish_reason,
  ARRAY_LENGTH(input_messages) AS input_message_count,
  IF(jp.gen_ai_output_messages IS NULL, NULL, (
    SELECT COUNT(1)
    FROM UNNEST(output_messages) AS msg, UNNEST(JSON_QUERY_ARRAY(msg.parts)) AS part
    WHERE LAX_STRING(part.type) IN ('tool_call', 'function_call')
  )) AS tool_call_count,
  ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT LAX_STRING(part.name)
    FROM UNNEST(output_messages) AS msg, UNNEST(JSON_QUERY_ARRAY(msg.parts)) AS part
    WHERE LAX_STRING(part.type) IN ('tool_call', 'function_call') AND LAX_STRING(part.name) IS NOT NULL
    ORDER BY 1
  ), ',') AS tool_names,
  IF(jp.gen_ai_input_messages IS NULL, NULL, (
    SELECT COUNT(1)
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    LEFT JOIN UNNEST([SAFE.PARSE_JSON(COALESCE(
      NULLIF(ARRAY_TO_STRING(ARRAY(
        SELECT LAX_STRING(x.text)
        FROM UNNEST(JSON_QUERY_ARRAY(COALESCE(part.response.content, part.response.structuredcontent.content))) AS x
        WHERE LAX_STRING(x.text) IS NOT NULL
      ), '\n'), ''),
      LAX_STRING(part.response.result)
    ))]) AS cj
    WHERE JSON_TYPE(part.response) IS NOT NULL
      AND JSON_TYPE(part.response) != 'null'
      AND (
        LAX_BOOL(part.response.iserror) = TRUE
        OR NULLIF(LAX_STRING(part.response.error), '') IS NOT NULL
        OR (JSON_TYPE(part.response.error) NOT IN ('null') AND COALESCE(LAX_STRING(part.response.error), TO_JSON_STRING(part.response.error)) NOT IN ('', '""'))
        OR (JSON_TYPE(part.response.error_message) NOT IN ('null') AND COALESCE(LAX_STRING(part.response.error_message), '') != '')
        OR (JSON_TYPE(part.response.error_code) NOT IN ('null') AND COALESCE(LAX_STRING(part.response.error_code), TO_JSON_STRING(part.response.error_code)) NOT IN ('', '""'))
        OR JSON_TYPE(part.response._dolphin_error_handling) NOT IN ('null')
        OR COALESCE(
          NULLIF(LAX_STRING(cj.result.Error), ''),
          NULLIF(LAX_STRING(cj.error.message), ''),
          NULLIF(LAX_STRING(cj.error), ''),
          NULLIF(LAX_STRING(cj.Error), '')
        ) IS NOT NULL
      )
  )) AS tool_failure_count,
  NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT LAX_STRING(part.response.structuredcontent.mcpservername)
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    WHERE NULLIF(LAX_STRING(part.response.structuredcontent.mcpservername), '') IS NOT NULL
    ORDER BY 1
  ), ','), '') AS mcp_server_name,
  NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT LAX_STRING(part.response.structuredcontent.authkind)
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    WHERE NULLIF(LAX_STRING(part.response.structuredcontent.authkind), '') IS NOT NULL
    ORDER BY 1
  ), ','), '') AS mcp_auth_kind
FROM fields
