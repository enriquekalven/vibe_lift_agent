-- VibeLift daily usage by engine, agent and model.
-- Distinct counts (sessions, active_users) are per row and must NOT be summed across rows;
-- query fct_turns with COUNT(DISTINCT ...) for cross-row totals.
-- Token sums are NULL when no turn in the row reported tokens; turns_with_tokens says how
-- many turns the sums cover. Cost is joined in the application from the Cloud Billing
-- export (vibelift/billing_export.py), which may live in another location.
SELECT
  event_date,
  engine_key,
  agent_name,
  model_name,
  COUNT(1) AS interactions,
  COUNTIF(turn_kind = 'CHAT') AS chat_turns,
  COUNTIF(turn_kind = 'SEARCH') AS searches,
  COUNTIF(turn_kind = 'WIDGET_ACTION') AS widget_actions,
  COUNTIF(turn_kind = 'FILE_UPLOAD') AS file_uploads,
  COUNTIF(turn_kind = 'AGENT_CALL') AS agent_calls,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(DISTINCT user_email) AS active_users,
  COUNTIF(turn_status = 'SUCCESS') AS successful_turns,
  COUNTIF(is_failed) AS failed_turns,
  COUNTIF(turn_status = 'UNKNOWN') AS unknown_status_turns,
  COUNTIF(turn_status = 'RATE_LIMITED') AS rate_limited_turns,
  COUNTIF(is_guardrail_blocked) AS guardrail_blocks,
  COUNTIF(total_tokens IS NOT NULL) AS turns_with_tokens,
  SUM(input_tokens) AS input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(cached_input_tokens) AS cached_input_tokens,
  SUM(reasoning_tokens) AS reasoning_tokens,
  SUM(total_tokens) AS total_tokens,
  SUM(llm_calls) AS llm_calls
FROM `{{mart}}.fct_turns`
GROUP BY event_date, engine_key, agent_name, model_name
