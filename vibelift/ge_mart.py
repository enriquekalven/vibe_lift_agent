"""Readers for VibeLift's Gemini Enterprise reporting mart.

The mart is provisioned by `deploy/bigquery/provision_ge_mart.py`:

  <curated dataset> (default `ds_ge_curated_staging`): cleaned views over the raw GE log sinks.
  <mart dataset>    (default `vibelift_mart`): `fct_turns`, `fct_sessions`, `agg_daily_usage`.

This module only builds validated SQL and maps rows to dashboard payloads. Every number comes
from a mart row; when the mart does not know a value (tokens, model, latency, rating) the
payload carries None, never a default.
"""

import os
import re
from collections.abc import Mapping

CURATED_DATASET_ENV = 'VIBELIFT_GE_CURATED_DATASET'
MART_DATASET_ENV = 'VIBELIFT_GE_MART_DATASET'
DEFAULT_CURATED_DATASET = 'ds_ge_curated_staging'
DEFAULT_MART_DATASET = 'vibelift_mart'

_PROJECT_RE = re.compile(r'^[a-z][a-z0-9\-]{4,61}[a-z0-9]$')
_DATASET_RE = re.compile(r'^[A-Za-z0-9_]{1,1024}$')

# Turn statuses that are not failures (see fct_turns.is_failed).
NON_FAILURE_STATUSES = ('SUCCESS', 'SKIPPED', 'UNKNOWN', 'CANCELLED')


def _dataset_from_env(env: str, default: str) -> str:
  value = (os.environ.get(env) or '').strip() or default
  if not _DATASET_RE.match(value):
    raise ValueError(f'Invalid {env}: {value!r}')
  return value


def curated_ref(project_id: str) -> str:
  _check_project(project_id)
  return f'{project_id}.{_dataset_from_env(CURATED_DATASET_ENV, DEFAULT_CURATED_DATASET)}'


def mart_ref(project_id: str) -> str:
  _check_project(project_id)
  return f'{project_id}.{_dataset_from_env(MART_DATASET_ENV, DEFAULT_MART_DATASET)}'


def _check_project(project_id: str) -> None:
  if not _PROJECT_RE.match(project_id or ''):
    raise ValueError(f'Invalid project id: {project_id!r}')


def _clamp(value: object, low: int, high: int) -> int:
  try:
    n = int(value)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    n = low
  return max(low, min(n, high))


# ---------------------------------------------------------------------------------------------
# SQL builders (all interpolated values are validated or clamped integers)
# ---------------------------------------------------------------------------------------------


def build_refresh_fct_turns_ddl(project_id: str) -> str:
  """DDL to atomically rebuild the materialized fct_turns table from v_fct_turns."""
  mart = mart_ref(project_id)
  return (
      f'CREATE OR REPLACE TABLE `{mart}.fct_turns`\n'
      'PARTITION BY event_date\n'
      'CLUSTER BY engine_key, user_email\n'
      'OPTIONS(description="VibeLift turn fact: one row per user interaction (materialized; see refreshed_at).",'
      ' labels=[("app", "vibelift")])\n'
      'AS\n'
      'SELECT\n'
      '  *,\n'
      '  CURRENT_TIMESTAMP() AS refreshed_at\n'
      f'FROM `{mart}.v_fct_turns`'
  )


def build_support_turns_sql(project_id: str, hours: int = 168, limit: int = 15) -> str:
  """Recent turns, actionable issues first (failures, quota, permission, guardrail blocks)."""
  return f"""
    SELECT
      turn_id, turn_source, turn_kind, CAST(event_timestamp AS STRING) AS ts, trace_id, session_id,
      user_email, engine_key, agent_name, model_name, api_method, turn_status, is_actionable_issue,
      status_code, status_message, error_reason, audit_match_method, is_guardrail_blocked,
      guardrail_categories
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_clamp(hours, 1, 24 * 365)} HOUR)
    ORDER BY is_actionable_issue DESC, event_timestamp DESC
    LIMIT {_clamp(limit, 1, 500)}
  """


def build_recent_turns_sql(project_id: str, hours: int = 168, limit: int = 20, tokens_only: bool = False) -> str:
  """Recent turns with their measured usage (tokens NULL when not logged)."""
  where_tokens = 'AND total_tokens IS NOT NULL' if tokens_only else ''
  return f"""
    SELECT
      turn_id, turn_source, turn_kind, CAST(event_timestamp AS STRING) AS ts, session_id, user_email,
      engine_key, agent_name, model_name, api_method, turn_status, status_message,
      input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens, llm_calls,
      tool_call_count, tool_names
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_clamp(hours, 1, 24 * 365)} HOUR)
      AND turn_kind != 'WIDGET_ACTION'
      {where_tokens}
    ORDER BY event_timestamp DESC
    LIMIT {_clamp(limit, 1, 500)}
  """


def build_user_engine_rollup_sql(project_id: str, hours: int = 168) -> str:
  """Per user and GE app: interactions, real sessions and measured tokens."""
  return f"""
    SELECT
      user_email,
      engine_key,
      COUNT(1) AS interactions,
      COUNTIF(turn_kind = 'CHAT') AS chat_turns,
      COUNT(DISTINCT session_id) AS sessions,
      COUNTIF(total_tokens IS NOT NULL) AS turns_with_tokens,
      SUM(total_tokens) AS total_tokens,
      SUM(reasoning_tokens) AS reasoning_tokens,
      COUNTIF(is_failed) AS failed_turns,
      CAST(MAX(event_timestamp) AS STRING) AS last_seen
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_clamp(hours, 1, 24 * 365)} HOUR)
      AND user_email IS NOT NULL
    GROUP BY user_email, engine_key
    ORDER BY interactions DESC
    LIMIT 1000
  """


def build_audit_principals_sql(project_id: str, hours: int = 168) -> str:
  """Audit calls per principal, method and GE app (replaces the raw ds_ge_audit_raw query)."""
  return f"""
    SELECT
      principal_email AS principal,
      method_name,
      method_short_name,
      IF(engine_id IS NULL, NULL, CONCAT(COALESCE(location, '?'), '/', engine_id)) AS engine_key,
      COUNT(1) AS call_count,
      COUNTIF(is_error) AS error_count,
      CAST(MAX(event_timestamp) AS STRING) AS last_seen
    FROM `{curated_ref(project_id)}.v_consolidated_audit_log`
    WHERE principal_email IS NOT NULL
      AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_clamp(hours, 1, 24 * 365)} HOUR)
    GROUP BY 1, 2, 3, 4
    ORDER BY call_count DESC
    LIMIT 500
  """


def build_daily_totals_sql(project_id: str, days: int = 30) -> str:
  """Per-day totals from fct_turns (distinct users/sessions cannot be summed from agg rows)."""
  return f"""
    SELECT
      CAST(event_date AS STRING) AS day,
      COUNT(1) AS interactions,
      COUNTIF(turn_kind = 'CHAT') AS chat_turns,
      COUNTIF(turn_kind = 'SEARCH') AS searches,
      COUNTIF(turn_kind = 'AGENT_CALL') AS agent_calls,
      COUNT(DISTINCT session_id) AS sessions,
      COUNT(DISTINCT user_email) AS active_users,
      COUNTIF(is_failed) AS failed_turns,
      COUNTIF(is_guardrail_blocked) AS guardrail_blocks,
      COUNTIF(total_tokens IS NOT NULL) AS turns_with_tokens,
      SUM(input_tokens) AS input_tokens,
      SUM(output_tokens) AS output_tokens,
      SUM(cached_input_tokens) AS cached_input_tokens,
      SUM(reasoning_tokens) AS reasoning_tokens,
      SUM(total_tokens) AS total_tokens,
      CAST(MAX(refreshed_at) AS STRING) AS refreshed_at
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {_clamp(days, 1, 400)} DAY)
    GROUP BY event_date
    ORDER BY event_date
  """


def build_daily_usage_sql(project_id: str, days: int = 7) -> str:
  """Daily usage by app, agent and model from agg_daily_usage."""
  return f"""
    SELECT
      CAST(event_date AS STRING) AS day, engine_key, agent_name, model_name, interactions, chat_turns,
      searches, agent_calls, sessions, active_users, failed_turns, guardrail_blocks, turns_with_tokens,
      input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens, llm_calls
    FROM `{mart_ref(project_id)}.agg_daily_usage`
    WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {_clamp(days, 1, 400)} DAY)
    ORDER BY event_date DESC, interactions DESC
    LIMIT 500
  """


def build_recent_sessions_sql(project_id: str, days: int = 30, limit: int = 50) -> str:
  """Recent conversation sessions from fct_sessions."""
  return f"""
    SELECT
      engine_key,
      session_id,
      CAST(session_start AS STRING) AS session_start,
      CAST(session_end AS STRING) AS session_end,
      duration_seconds,
      CAST(session_date AS STRING) AS session_date,
      user_email,
      agent_name,
      model_names,
      turns,
      chat_turns,
      failed_turns,
      actionable_issues,
      guardrail_blocks,
      turns_with_tokens,
      input_tokens,
      output_tokens,
      cached_input_tokens,
      reasoning_tokens,
      total_tokens,
      llm_calls,
      tool_calls,
      tool_failures
    FROM `{mart_ref(project_id)}.fct_sessions`
    WHERE session_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {_clamp(days, 1, 400)} DAY)
    ORDER BY session_end DESC
    LIMIT {_clamp(limit, 1, 500)}
  """


def build_session_turns_sql(
    project_id: str,
    days: int = 30,
    session_limit: int = 50,
    turns_per_session: int = 100,
    limit: int = 2000,
) -> str:
  """Turn-by-turn token counts for the sessions listed by build_recent_sessions_sql.

  Uses the same session selection (date window, ORDER BY session_end DESC, LIMIT) so the
  drilldown covers exactly the sessions shown. Only counts and metadata are selected; prompt
  and response text never leave BigQuery. Keeps the most recent `turns_per_session` turns of
  each session and at most `limit` rows overall.
  """
  mart = mart_ref(project_id)
  window = _clamp(days, 1, 400)
  return f"""
    WITH recent_sessions AS (
      SELECT engine_key, session_id
      FROM `{mart}.fct_sessions`
      WHERE session_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {window} DAY)
      ORDER BY session_end DESC
      LIMIT {_clamp(session_limit, 1, 500)}
    )
    SELECT
      t.engine_key, t.session_id, t.turn_id, t.turn_kind, t.turn_status,
      CAST(t.event_timestamp AS STRING) AS ts, t.user_email, t.agent_name, t.model_name,
      t.input_tokens, t.output_tokens, t.cached_input_tokens, t.reasoning_tokens, t.total_tokens,
      t.llm_calls, t.tool_call_count, t.tool_names
    FROM `{mart}.fct_turns` AS t
    JOIN recent_sessions AS r
      ON t.session_id = r.session_id AND COALESCE(t.engine_key, '') = COALESCE(r.engine_key, '')
    WHERE t.session_id IS NOT NULL
      AND t.event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {window} DAY)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY t.engine_key, t.session_id ORDER BY t.event_timestamp DESC
    ) <= {_clamp(turns_per_session, 1, 500)}
    ORDER BY t.session_id, t.event_timestamp
    LIMIT {_clamp(limit, 1, 5000)}
  """


# ---------------------------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------------------------


def int_or_none(value: object) -> int | None:
  """BigQuery REST returns numbers as strings and NULL as None; keep None as None."""
  if value is None or value == '':
    return None
  try:
    return int(float(value))  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return None


def bool_or_none(value: object) -> bool | None:
  if value is None or value == '':
    return None
  if isinstance(value, bool):
    return value
  return str(value).strip().lower() == 'true'


def _text(value: object) -> str | None:
  s = str(value).strip() if value is not None else ''
  return s or None


def support_event_from_row(row: Mapping[str, object], project_id: str) -> dict[str, object]:
  """Maps a fct_turns row to the dashboard's support-event dict (same keys as before)."""
  source_table = f'{mart_ref(project_id)}.fct_turns'
  turn_id = str(row.get('turn_id') or '')
  status = str(row.get('turn_status') or 'UNKNOWN')
  actionable = bool_or_none(row.get('is_actionable_issue')) is True
  blocked = bool_or_none(row.get('is_guardrail_blocked')) is True
  method = _text(row.get('api_method')) or _text(row.get('turn_kind')) or 'turn'
  user = _text(row.get('user_email'))
  engine = _text(row.get('engine_key'))
  if blocked and status == 'SUCCESS':
    category = 'GUARDRAIL_BLOCK'
  else:
    category = status
  detail = _text(row.get('status_message')) or _text(row.get('error_reason'))
  if blocked:
    detail = f"guardrail: {row.get('guardrail_categories') or 'blocked'}" + (f'; {detail}' if detail else '')
  summary = f'{method} {status}'
  if user:
    summary += f' by {user}'
  if engine:
    summary += f' on {engine}'
  if detail:
    summary += f' ({detail[:160]})'
  return {
      'ticket_id': turn_id.split(':')[-1][:14],
      'event_id': turn_id,
      'timestamp': str(row.get('ts') or ''),
      'tier': 'L2 Actionable' if actionable else 'L1 Observed',
      'triage_tier': 'L2 Actionable' if actionable else 'L1 Observed',
      'agent_id': engine,
      'agent_name': _text(row.get('agent_name')),
      'model_name': _text(row.get('model_name')),
      'trace_id': _text(row.get('trace_id')),
      'session_id': _text(row.get('session_id')),
      'user': user,
      'source': source_table,
      'source_dataset': source_table,
      'turn_source': _text(row.get('turn_source')),
      'category': category,
      'intent_category': f'{method} ({status})',
      'issue_summary': summary,
      'status': status,
      'is_actionable_issue': actionable,
      'status_code': int_or_none(row.get('status_code')),
      'error_reason': _text(row.get('error_reason')),
      'resolution_status': 'NEEDS_ATTENTION' if actionable else 'NO_ACTION',
      'resolution_action': (
          f"audit match: {row.get('audit_match_method')}" if row.get('audit_match_method') else 'no audit record matched'
      ),
  }


def usage_log_from_row(row: Mapping[str, object], project_id: str) -> dict[str, object]:
  """Maps a fct_turns row to an aive usage-log dict. No prompt text, no invented latency or rating."""
  source_table = f'{mart_ref(project_id)}.fct_turns'
  turn_id = str(row.get('turn_id') or '')
  email = _text(row.get('user_email'))
  status = str(row.get('turn_status') or 'UNKNOWN')
  return {
      'event_id': turn_id,
      'timestamp': str(row.get('ts') or ''),
      'session_id': _text(row.get('session_id')),
      'user_email': email,
      'user_ldap': email.split('@')[0] if email and '@' in email else email,
      'company_name': f'Google Cloud ({project_id})',
      'department': f"{row.get('turn_source') or 'turn'} · {row.get('engine_key') or 'app not logged'}",
      'task_type': _text(row.get('api_method')) or _text(row.get('turn_kind')),
      'agent_name': _text(row.get('agent_name')),
      'model_name': _text(row.get('model_name')),
      # Prompt and response text never leave BigQuery.
      'prompts': [],
      'outputs': [{
          'gcs_uri': f'bq://{source_table}/{turn_id}',
          'media_type': 'BIGQUERY_ROW',
          'mime_type': 'application/x-bigquery-row',
      }],
      'latency_ms': None,
      'total_tokens': int_or_none(row.get('total_tokens')),
      'input_tokens': int_or_none(row.get('input_tokens')),
      'output_tokens': int_or_none(row.get('output_tokens')),
      'cached_input_tokens': int_or_none(row.get('cached_input_tokens')),
      'thinking_tokens': int_or_none(row.get('reasoning_tokens')),
      'background_tokens': None,
      'status': status,
      'error_message': _text(row.get('status_message')) if status not in NON_FAILURE_STATUSES else None,
      'csat_rating': None,
      'source_table': source_table,
  }


def mart_refreshed_at(rows: list[Mapping[str, object]]) -> str | None:
  """Latest fct_turns rebuild time seen in the rows (None when unknown, e.g. no rows)."""
  stamps = [str(r['refreshed_at']) for r in rows if r.get('refreshed_at')]
  return max(stamps) if stamps else None


def daily_usage_from_row(row: Mapping[str, object]) -> dict[str, object]:
  out: dict[str, object] = {'day': str(row.get('day') or '')}
  for key in ('engine_key', 'agent_name', 'model_name', 'refreshed_at'):
    if key in row:
      out[key] = _text(row.get(key))
  for key in ('interactions', 'chat_turns', 'searches', 'agent_calls', 'sessions', 'active_users',
              'failed_turns', 'guardrail_blocks', 'turns_with_tokens', 'llm_calls'):
    if key in row:
      out[key] = int_or_none(row.get(key)) or 0
  for key in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'total_tokens'):
    if key in row:
      out[key] = int_or_none(row.get(key))  # None = no turn reported tokens
  return out


def session_from_row(row: Mapping[str, object], project_id: str) -> dict[str, object]:
  """Maps a fct_sessions row to a dashboard session dict. Unknown token/call values remain None."""
  return {
      'session_id': str(row.get('session_id') or ''),
      'session_key': session_key(row.get('engine_key'), row.get('session_id')),
      'engine_key': _text(row.get('engine_key')),
      'session_start': _text(row.get('session_start')),
      'session_end': _text(row.get('session_end')),
      'duration_seconds': int_or_none(row.get('duration_seconds')),
      'session_date': _text(row.get('session_date')),
      'user_email': _text(row.get('user_email')),
      'agent_name': _text(row.get('agent_name')),
      'model_names': _text(row.get('model_names')),
      'turns': int_or_none(row.get('turns')) or 0,
      'chat_turns': int_or_none(row.get('chat_turns')) or 0,
      'failed_turns': int_or_none(row.get('failed_turns')) or 0,
      'actionable_issues': int_or_none(row.get('actionable_issues')) or 0,
      'guardrail_blocks': int_or_none(row.get('guardrail_blocks')) or 0,
      'turns_with_tokens': int_or_none(row.get('turns_with_tokens')) or 0,
      'input_tokens': int_or_none(row.get('input_tokens')),
      'output_tokens': int_or_none(row.get('output_tokens')),
      'cached_input_tokens': int_or_none(row.get('cached_input_tokens')),
      'reasoning_tokens': int_or_none(row.get('reasoning_tokens')),
      'total_tokens': int_or_none(row.get('total_tokens')),
      'llm_calls': int_or_none(row.get('llm_calls')),
      'tool_calls': int_or_none(row.get('tool_calls')),
      'tool_failures': int_or_none(row.get('tool_failures')),
      'source_table': f'{mart_ref(project_id)}.fct_sessions',
  }


def session_key(engine_key: object, session_id: object) -> str:
  """Stable key for a fct_sessions row (the view groups by engine_key and session_id)."""
  return f"{_text(engine_key) or ''}|{_text(session_id) or ''}"


def session_turn_from_row(row: Mapping[str, object]) -> dict[str, object]:
  """Maps a build_session_turns_sql row to a token-only turn dict (unknown counts stay None)."""
  return {
      'turn_id': str(row.get('turn_id') or ''),
      'ts': _text(row.get('ts')),
      'turn_kind': _text(row.get('turn_kind')),
      'turn_status': _text(row.get('turn_status')),
      'user_email': _text(row.get('user_email')),
      'agent_name': _text(row.get('agent_name')),
      'model_name': _text(row.get('model_name')),
      'input_tokens': int_or_none(row.get('input_tokens')),
      'output_tokens': int_or_none(row.get('output_tokens')),
      'cached_input_tokens': int_or_none(row.get('cached_input_tokens')),
      'reasoning_tokens': int_or_none(row.get('reasoning_tokens')),
      'total_tokens': int_or_none(row.get('total_tokens')),
      'llm_calls': int_or_none(row.get('llm_calls')),
      'tool_calls': int_or_none(row.get('tool_call_count')),
      'tool_names': _text(row.get('tool_names')),
  }


def group_session_turns(rows: list[Mapping[str, object]]) -> dict[str, list[dict[str, object]]]:
  """Groups session-turn rows by session_key(), keeping the row order (oldest turn first)."""
  grouped: dict[str, list[dict[str, object]]] = {}
  for row in rows:
    if not _text(row.get('session_id')):
      continue
    grouped.setdefault(session_key(row.get('engine_key'), row.get('session_id')), []).append(
        session_turn_from_row(row))
  return grouped

