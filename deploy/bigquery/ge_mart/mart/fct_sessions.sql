-- VibeLift session fact: one row per real conversation session.
-- Only turns whose logs carry a session id (GE session from the answer/session resource name,
-- or the agent's gen_ai.conversation.id) are grouped. Searches without a session are NOT
-- turned into one-turn sessions, and no session is inferred from time gaps (stage did both,
-- inflating session counts).
SELECT
  engine_key,
  session_id,
  MIN(event_timestamp) AS session_start,
  MAX(event_timestamp) AS session_end,
  TIMESTAMP_DIFF(MAX(event_timestamp), MIN(event_timestamp), SECOND) AS duration_seconds,
  DATE(MIN(event_timestamp)) AS session_date,
  ARRAY_AGG(user_email IGNORE NULLS ORDER BY event_timestamp LIMIT 1)[SAFE_OFFSET(0)] AS user_email,
  ARRAY_AGG(agent_name IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS agent_name,
  STRING_AGG(DISTINCT model_name, ',' ORDER BY model_name) AS model_names,
  COUNT(1) AS turns,
  COUNTIF(turn_kind = 'CHAT') AS chat_turns,
  COUNTIF(is_failed) AS failed_turns,
  COUNTIF(is_actionable_issue) AS actionable_issues,
  COUNTIF(is_guardrail_blocked) AS guardrail_blocks,
  COUNTIF(total_tokens IS NOT NULL) AS turns_with_tokens,
  SUM(input_tokens) AS input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(cached_input_tokens) AS cached_input_tokens,
  SUM(reasoning_tokens) AS reasoning_tokens,
  SUM(total_tokens) AS total_tokens,
  SUM(llm_calls) AS llm_calls,
  SUM(tool_call_count) AS tool_calls,
  SUM(tool_failure_count) AS tool_failures
FROM `{{mart}}.fct_turns`
WHERE session_id IS NOT NULL
GROUP BY engine_key, session_id
