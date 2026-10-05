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
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(insertId, CAST(UNIX_MICROS(timestamp) AS STRING))
    ORDER BY timestamp DESC
  ) = 1
),
fields AS (
  SELECT
    *,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_input_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.input_tokens"')),
      LAX_INT64(lb.gen_ai_usage_input_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.input_tokens"'))
    ) AS input_tokens,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_output_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.output_tokens"')),
      LAX_INT64(lb.gen_ai_usage_output_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.output_tokens"'))
    ) AS output_tokens,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_cache_read_input_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.cache_read.input_tokens"')),
      LAX_INT64(lb.gen_ai_usage_cache_read_input_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.cache_read.input_tokens"'))
    ) AS cached_input_tokens,
    COALESCE(
      LAX_INT64(jp.gen_ai_usage_reasoning_output_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.reasoning_output_tokens"')),
      LAX_INT64(jp.gen_ai_usage_reasoning_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.reasoning_tokens"')),
      LAX_INT64(jp.gen_ai_usage_thoughts_tokens),
      LAX_INT64(JSON_QUERY(jp, '$."gen_ai.usage.thoughts_tokens"')),
      LAX_INT64(lb.gen_ai_usage_reasoning_output_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.reasoning_output_tokens"')),
      LAX_INT64(lb.gen_ai_usage_reasoning_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.reasoning_tokens"')),
      LAX_INT64(lb.gen_ai_usage_thoughts_tokens),
      LAX_INT64(JSON_QUERY(lb, '$."gen_ai.usage.thoughts_tokens"'))
    ) AS reasoning_tokens,
    COALESCE(
      JSON_QUERY_ARRAY(jp.gen_ai_input_messages),
      JSON_QUERY_ARRAY(jp, '$."gen_ai.input.messages"')
    ) AS input_messages,
    COALESCE(
      JSON_QUERY_ARRAY(jp.gen_ai_output_messages),
      JSON_QUERY_ARRAY(jp, '$."gen_ai.output.messages"')
    ) AS output_messages,
    ARRAY(
      SELECT AS STRUCT
        LAX_STRING(cp.id) AS call_id,
        ANY_VALUE(COALESCE(
          IF(LAX_STRING(cp.name) = 'invalid_tool_call_notifier', NULLIF(LAX_STRING(cp.arguments.tool_name), ''), NULL),
          LAX_STRING(cp.name)
        )) AS call_tool_name
      FROM UNNEST(COALESCE(
        JSON_QUERY_ARRAY(jp.gen_ai_input_messages),
        JSON_QUERY_ARRAY(jp, '$."gen_ai.input.messages"')
      )) AS cm, UNNEST(JSON_QUERY_ARRAY(cm.parts)) AS cp
      WHERE LAX_STRING(cp.type) IN ('tool_call', 'function_call')
        AND LAX_STRING(cp.id) IS NOT NULL
        AND LAX_STRING(cp.name) IS NOT NULL
      GROUP BY 1
    ) AS call_lookup
  FROM ops
)
SELECT
  event_timestamp,
  DATE(event_timestamp) AS event_date,
  CONCAT('INFERENCE:', COALESCE(insert_id, CAST(UNIX_MICROS(event_timestamp) AS STRING))) AS event_id,
  trace_id,
  insert_id,
  NULLIF(COALESCE(
    LAX_STRING(jp.gen_ai_conversation_id),
    JSON_VALUE(jp, '$."gen_ai.conversation.id"'),
    LAX_STRING(lb.gen_ai_conversation_id),
    JSON_VALUE(lb, '$."gen_ai.conversation.id"')
  ), '') AS conversation_id,
  NULLIF(COALESCE(
    LAX_STRING(jp.user_id),
    JSON_VALUE(jp, '$."user.id"'),
    LAX_STRING(lb.user_id),
    JSON_VALUE(lb, '$."user.id"')
  ), '') AS user_id,
  NULLIF(COALESCE(
    LAX_STRING(jp.gen_ai_agent_name),
    JSON_VALUE(jp, '$."gen_ai.agent.name"'),
    LAX_STRING(lb.gen_ai_agent_name),
    JSON_VALUE(lb, '$."gen_ai.agent.name"')
  ), '') AS agent_name,
  COALESCE(
    LAX_STRING(res.labels.agent_id),
    LAX_STRING(res.labels.reasoning_engine_id),
    LAX_STRING(res.labels.assistant_id)
  ) AS agent_resource_id,
  LAX_STRING(res.labels.engine_id) AS engine_id,
  LAX_STRING(res.labels.location) AS location,
  COALESCE(
    LAX_STRING(jp.gen_ai_response_model),
    JSON_VALUE(jp, '$."gen_ai.response.model"'),
    LAX_STRING(jp.gen_ai_request_model),
    JSON_VALUE(jp, '$."gen_ai.request.model"'),
    LAX_STRING(lb.gen_ai_response_model),
    JSON_VALUE(lb, '$."gen_ai.response.model"'),
    LAX_STRING(lb.gen_ai_request_model),
    JSON_VALUE(lb, '$."gen_ai.request.model"')
  ) AS model_name,
  input_tokens,
  output_tokens,
  cached_input_tokens,
  reasoning_tokens,
  IF(input_tokens IS NULL OR output_tokens IS NULL, NULL, input_tokens + output_tokens) AS total_tokens,
  COALESCE(
    LAX_STRING(jp.gen_ai_response_finish_reasons[0]),
    LAX_STRING(JSON_QUERY(jp, '$."gen_ai.response.finish_reasons"')[0]),
    LAX_STRING(output_messages[SAFE_OFFSET(0)].finish_reason),
    REGEXP_EXTRACT(COALESCE(LAX_STRING(lb.gen_ai_response_finish_reasons), JSON_VALUE(lb, '$."gen_ai.response.finish_reasons"')), r'([A-Za-z_]+)')
  ) AS finish_reason,
  ARRAY_LENGTH(input_messages) AS input_message_count,
  IF(output_messages IS NULL, NULL, (
    SELECT COUNT(1)
    FROM UNNEST(output_messages) AS msg, UNNEST(JSON_QUERY_ARRAY(msg.parts)) AS part
    WHERE LAX_STRING(part.type) IN ('tool_call', 'function_call')
  )) AS tool_call_count,
  ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT COALESCE(
      IF(LAX_STRING(part.name) = 'invalid_tool_call_notifier', NULLIF(LAX_STRING(part.arguments.tool_name), ''), NULL),
      LAX_STRING(part.name)
    )
    FROM UNNEST(output_messages) AS msg, UNNEST(JSON_QUERY_ARRAY(msg.parts)) AS part
    WHERE LAX_STRING(part.type) IN ('tool_call', 'function_call') AND LAX_STRING(part.name) IS NOT NULL
    ORDER BY 1
  ), ',') AS tool_names,
  NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT CONCAT(
      COALESCE(
        IF(LAX_STRING(part.name) = 'invalid_tool_call_notifier', CONCAT('invalid_tool_call_notifier(', NULLIF(LAX_STRING(part.arguments.tool_name), ''), ')'), NULL),
        LAX_STRING(part.name)
      ),
      IF(
        JSON_TYPE(COALESCE(part.arguments, part.args)) NOT IN ('null'),
        CONCAT(' ', SUBSTR(TO_JSON_STRING(COALESCE(part.arguments, part.args)), 1, 240)),
        ''
      )
    )
    FROM UNNEST(output_messages) AS msg, UNNEST(JSON_QUERY_ARRAY(msg.parts)) AS part
    WHERE LAX_STRING(part.type) IN ('tool_call', 'function_call') AND LAX_STRING(part.name) IS NOT NULL
  ), ' | '), '') AS tool_call_summary,
  IF(input_messages IS NULL, NULL, (
    SELECT COUNT(1)
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    LEFT JOIN UNNEST([SAFE.PARSE_JSON(COALESCE(
      NULLIF(ARRAY_TO_STRING(ARRAY(
        SELECT LAX_STRING(x.text)
        FROM UNNEST(JSON_QUERY_ARRAY(COALESCE(
          part.response.content,
          part.response.structuredcontent.content,
          part.response.structuredContent.content
        ))) AS x
        WHERE LAX_STRING(x.text) IS NOT NULL
      ), '\n'), ''),
      LAX_STRING(part.response.result)
    ))]) AS cj
    WHERE JSON_TYPE(part.response) IS NOT NULL
      AND JSON_TYPE(part.response) != 'null'
      AND (
        COALESCE(LAX_BOOL(part.response.iserror), LAX_BOOL(part.response.isError)) = TRUE
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
  IF(input_messages IS NULL, CAST(NULL AS STRING), NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT CONCAT(
      COALESCE(cl.call_tool_name, NULLIF(LAX_STRING(part.name), ''), NULLIF(LAX_STRING(part.response.tool_name), ''), NULLIF(LAX_STRING(part.response.skill_name), ''), 'tool'),
      IF(
        COALESCE(
          NULLIF(LAX_STRING(part.response.error_code), ''),
          IF(JSON_TYPE(part.response.error_code) NOT IN ('null') AND TO_JSON_STRING(part.response.error_code) NOT IN ('', '""'), TO_JSON_STRING(part.response.error_code), NULL),
          IF(COALESCE(NULLIF(LAX_STRING(cj.result.Error), ''), NULLIF(LAX_STRING(cj.error.message), ''), NULLIF(LAX_STRING(cj.error), ''), NULLIF(LAX_STRING(cj.Error), '')) IS NOT NULL, 'MCP_PAYLOAD_ERROR', NULL)
        ) IS NOT NULL,
        CONCAT(' [', COALESCE(
          NULLIF(LAX_STRING(part.response.error_code), ''),
          IF(JSON_TYPE(part.response.error_code) NOT IN ('null') AND TO_JSON_STRING(part.response.error_code) NOT IN ('', '""'), TO_JSON_STRING(part.response.error_code), NULL),
          'MCP_PAYLOAD_ERROR'
        ), ']'),
        ''
      ),
      ': ',
      SUBSTR(COALESCE(
        NULLIF(LAX_STRING(part.response.error), ''),
        NULLIF(LAX_STRING(part.response.error.message), ''),
        IF(JSON_TYPE(part.response.error) NOT IN ('null') AND TO_JSON_STRING(part.response.error) NOT IN ('', '""'), TO_JSON_STRING(part.response.error), NULL),
        NULLIF(LAX_STRING(part.response.error_message), ''),
        NULLIF(LAX_STRING(cj.result.Error), ''),
        NULLIF(LAX_STRING(cj.error.message), ''),
        NULLIF(LAX_STRING(cj.error), ''),
        NULLIF(LAX_STRING(cj.Error), ''),
        IF(COALESCE(LAX_BOOL(part.response.iserror), LAX_BOOL(part.response.isError)) = TRUE,
          COALESCE(content_text, NULLIF(LAX_STRING(part.response.text), ''), NULLIF(LAX_STRING(part.response.result), '')),
          NULL
        ),
        NULLIF(LAX_STRING(part.response._dolphin_error_handling.message), ''),
        IF(JSON_TYPE(part.response._dolphin_error_handling) NOT IN ('null'), TO_JSON_STRING(part.response._dolphin_error_handling), NULL),
        'Tool reported an error without a message'
      ), 1, 500)
    )
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    LEFT JOIN UNNEST(call_lookup) AS cl ON cl.call_id = LAX_STRING(part.id)
    LEFT JOIN UNNEST([NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT LAX_STRING(x.text)
      FROM UNNEST(JSON_QUERY_ARRAY(COALESCE(
        part.response.content,
        part.response.structuredcontent.content,
        part.response.structuredContent.content
      ))) AS x
      WHERE LAX_STRING(x.text) IS NOT NULL
    ), '\n'), '')]) AS content_text
    LEFT JOIN UNNEST([SAFE.PARSE_JSON(COALESCE(content_text, LAX_STRING(part.response.result)))]) AS cj
    WHERE JSON_TYPE(part.response) IS NOT NULL
      AND JSON_TYPE(part.response) != 'null'
      AND (
        COALESCE(LAX_BOOL(part.response.iserror), LAX_BOOL(part.response.isError)) = TRUE
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
  ), ' | '), '')) AS tool_error_messages,
  IF(input_messages IS NULL, CAST(NULL AS STRING), NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT COALESCE(
      NULLIF(LAX_STRING(part.response.error_code), ''),
      IF(JSON_TYPE(part.response.error_code) NOT IN ('null') AND TO_JSON_STRING(part.response.error_code) NOT IN ('', '""'), TO_JSON_STRING(part.response.error_code), NULL),
      IF(COALESCE(NULLIF(LAX_STRING(cj.result.Error), ''), NULLIF(LAX_STRING(cj.error.message), ''), NULLIF(LAX_STRING(cj.error), ''), NULLIF(LAX_STRING(cj.Error), '')) IS NOT NULL, 'MCP_PAYLOAD_ERROR', NULL),
      IF(COALESCE(LAX_BOOL(part.response.iserror), LAX_BOOL(part.response.isError)) = TRUE, 'MCP_IS_ERROR', NULL),
      'TOOL_ERROR'
    )
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    LEFT JOIN UNNEST([NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT LAX_STRING(x.text)
      FROM UNNEST(JSON_QUERY_ARRAY(COALESCE(
        part.response.content,
        part.response.structuredcontent.content,
        part.response.structuredContent.content
      ))) AS x
      WHERE LAX_STRING(x.text) IS NOT NULL
    ), '\n'), '')]) AS content_text
    LEFT JOIN UNNEST([SAFE.PARSE_JSON(COALESCE(content_text, LAX_STRING(part.response.result)))]) AS cj
    WHERE JSON_TYPE(part.response) IS NOT NULL
      AND JSON_TYPE(part.response) != 'null'
      AND (
        COALESCE(LAX_BOOL(part.response.iserror), LAX_BOOL(part.response.isError)) = TRUE
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
    ORDER BY 1
  ), ', '), '')) AS tool_error_codes,
  IF(input_messages IS NULL, CAST(NULL AS STRING), NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT CONCAT(
      COALESCE(cl.call_tool_name, NULLIF(LAX_STRING(part.name), ''), NULLIF(LAX_STRING(part.response.tool_name), ''), NULLIF(LAX_STRING(part.response.skill_name), ''), 'tool'),
      ': ',
      SUBSTR(
        COALESCE(
          content_text,
          NULLIF(LAX_STRING(part.response.result), ''),
          NULLIF(LAX_STRING(part.response.instructions), ''),
          IF(JSON_TYPE(part.response.retrieval_results) NOT IN ('null'), TO_JSON_STRING(part.response.retrieval_results), NULL),
          NULLIF(LAX_STRING(part.response.error_message), ''),
          NULLIF(LAX_STRING(part.response.error), '')
        ),
        1,
        400
      )
    )
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    LEFT JOIN UNNEST(call_lookup) AS cl ON cl.call_id = LAX_STRING(part.id)
    LEFT JOIN UNNEST([NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT LAX_STRING(x.text)
      FROM UNNEST(JSON_QUERY_ARRAY(COALESCE(
        part.response.content,
        part.response.structuredcontent.content,
        part.response.structuredContent.content
      ))) AS x
      WHERE LAX_STRING(x.text) IS NOT NULL
    ), '\n'), '')]) AS content_text
    WHERE JSON_TYPE(part.response) IS NOT NULL
      AND JSON_TYPE(part.response) != 'null'
      AND COALESCE(
        content_text,
        NULLIF(LAX_STRING(part.response.result), ''),
        NULLIF(LAX_STRING(part.response.instructions), ''),
        IF(JSON_TYPE(part.response.retrieval_results) NOT IN ('null'), TO_JSON_STRING(part.response.retrieval_results), NULL),
        NULLIF(LAX_STRING(part.response.error_message), ''),
        NULLIF(LAX_STRING(part.response.error), '')
      ) IS NOT NULL
  ), ' | '), '')) AS tool_output_preview,
  NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT COALESCE(
      LAX_STRING(part.response.structuredcontent.mcpservername),
      LAX_STRING(part.response.structuredContent.mcpServerName)
    )
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    WHERE NULLIF(COALESCE(
      LAX_STRING(part.response.structuredcontent.mcpservername),
      LAX_STRING(part.response.structuredContent.mcpServerName)
    ), '') IS NOT NULL
    ORDER BY 1
  ), ','), '') AS mcp_server_name,
  NULLIF(ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT COALESCE(
      LAX_STRING(part.response.structuredcontent.authkind),
      LAX_STRING(part.response.structuredContent.authKind)
    )
    FROM UNNEST(JSON_QUERY_ARRAY(input_messages[SAFE_OFFSET(ARRAY_LENGTH(input_messages) - 1)].parts)) AS part
    WHERE NULLIF(COALESCE(
      LAX_STRING(part.response.structuredcontent.authkind),
      LAX_STRING(part.response.structuredContent.authKind)
    ), '') IS NOT NULL
    ORDER BY 1
  ), ','), '') AS mcp_auth_kind
FROM fields
