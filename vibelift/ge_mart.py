"""Readers for VibeLift's Gemini Enterprise reporting mart.

The mart is provisioned by `deploy/bigquery/provision_ge_mart.py`:

  <curated dataset> (default `ds_ge_curated_staging`): cleaned views over the raw GE log sinks.
  <mart dataset>    (default `vibelift_mart`): `fct_turns`, `fct_sessions`, `agg_daily_usage`.

This module only builds validated SQL and maps rows to dashboard payloads. Every number comes
from a mart row; when the mart does not know a value (tokens, model, latency, rating) the
payload carries None, never a default.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

CURATED_DATASET_ENV = 'VIBELIFT_GE_CURATED_DATASET'
MART_DATASET_ENV = 'VIBELIFT_GE_MART_DATASET'
OTEL_DATASET_ENV = 'VIBELIFT_OTEL_DATASET'
PII_REDACT_ENV = 'VIBELIFT_PII_REDACT'
POLICY_TAG_PII_ENV = 'VIBELIFT_POLICY_TAG_PII'
DEFAULT_CURATED_DATASET = 'ds_ge_curated_staging'
DEFAULT_MART_DATASET = 'vibelift_mart'
# Dataset holding the OTel GenAI table `gen_ai_client_inference_operation_details`. The default
# matches the original reference deployment; set VIBELIFT_OTEL_DATASET to your own dataset.
DEFAULT_OTEL_DATASET = 'sre_triage_agent_telemetry'

_PROJECT_RE = re.compile(r'^(?:[a-z][a-z0-9\-]{1,61}[a-z0-9]\.[a-z]{2,}:)?[a-z][a-z0-9\-]{4,61}[a-z0-9]$')
_DATASET_RE = re.compile(r'^[A-Za-z0-9_]{1,1024}$')
_POLICY_TAG_RE = re.compile(
    r'^projects/[a-z][a-z0-9\-]{4,61}[a-z0-9]/locations/[a-z0-9\-]+/taxonomies/[0-9]+/policyTags/[0-9]+$'
)

# Turn statuses that are not failures (see fct_turns.is_failed).
NON_FAILURE_STATUSES = ('SUCCESS', 'SKIPPED', 'UNKNOWN', 'CANCELLED')


def pii_redaction_enabled() -> bool:
  """Returns True when runtime SHA-256 PII pseudonymization is enabled via VIBELIFT_PII_REDACT=1."""
  return (os.environ.get(PII_REDACT_ENV) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def redact_email_if_configured(email: str | None) -> str | None:
  """Pseudonymizes user_email with a deterministic SHA-256 prefix when VIBELIFT_PII_REDACT=1."""
  if not email:
    return None
  if not pii_redaction_enabled():
    return email
  digest = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:12]
  domain = email.split('@', 1)[1] if '@' in email else 'redacted.local'
  return f'user-{digest}@{domain}'


def _dataset_from_env(env: str, default: str) -> str:
  value = (os.environ.get(env) or '').strip() or default
  if not _DATASET_RE.match(value):
    raise ValueError(f'Invalid {env}: {value!r}')
  return value


def curated_dataset_name() -> str:
  return _dataset_from_env(CURATED_DATASET_ENV, DEFAULT_CURATED_DATASET)


def mart_dataset_name() -> str:
  return _dataset_from_env(MART_DATASET_ENV, DEFAULT_MART_DATASET)


def otel_dataset_name() -> str:
  return _dataset_from_env(OTEL_DATASET_ENV, DEFAULT_OTEL_DATASET)


def curated_ref(project_id: str) -> str:
  _check_project(project_id)
  return f'{project_id}.{curated_dataset_name()}'


def mart_ref(project_id: str) -> str:
  _check_project(project_id)
  return f'{project_id}.{mart_dataset_name()}'


def _check_project(project_id: str) -> None:
  if not _PROJECT_RE.match(project_id or ''):
    raise ValueError(f'Invalid project id: {project_id!r}')


def _clamp(value: object, low: int, high: int) -> int:
  try:
    n = int(value)  # type: ignore[call-overload]
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


def build_incremental_merge_fct_turns_dml(project_id: str, lookback_days: int = 3) -> str:
  """Partition-pruned incremental MERGE into fct_turns over the recent lookback window."""
  mart = mart_ref(project_id)
  days = _clamp(lookback_days, 1, 90)
  return (
      f'MERGE `{mart}.fct_turns` AS T\n'
      'USING (\n'
      '  SELECT\n'
      '    *,\n'
      '    CURRENT_TIMESTAMP() AS refreshed_at\n'
      f'  FROM `{mart}.v_fct_turns`\n'
      f'  WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)\n'
      ') AS S\n'
      'ON T.turn_id = S.turn_id\n'
      f'  AND T.event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)\n'
      'WHEN MATCHED THEN\n'
      '  UPDATE SET\n'
      '    T.turn_status = S.turn_status,\n'
      '    T.is_failed = S.is_failed,\n'
      '    T.is_actionable_issue = S.is_actionable_issue,\n'
      '    T.status_code = S.status_code,\n'
      '    T.status_message = S.status_message,\n'
      '    T.error_reason = S.error_reason,\n'
      '    T.audit_match_method = S.audit_match_method,\n'
      '    T.is_guardrail_blocked = S.is_guardrail_blocked,\n'
      '    T.guardrail_categories = S.guardrail_categories,\n'
      '    T.latency_ms = S.latency_ms,\n'
      '    T.input_tokens = S.input_tokens,\n'
      '    T.output_tokens = S.output_tokens,\n'
      '    T.cached_input_tokens = S.cached_input_tokens,\n'
      '    T.reasoning_tokens = S.reasoning_tokens,\n'
      '    T.total_tokens = S.total_tokens,\n'
      '    T.llm_calls = S.llm_calls,\n'
      '    T.tool_call_count = S.tool_call_count,\n'
      '    T.tool_failure_count = S.tool_failure_count,\n'
      '    T.tool_names = S.tool_names,\n'
      '    T.mcp_server_name = S.mcp_server_name,\n'
      '    T.refreshed_at = S.refreshed_at\n'
      'WHEN NOT MATCHED THEN\n'
      '  INSERT ROW'
  )


def build_pii_policy_tag_ddl(project_id: str, policy_tag: str | None = None) -> str:
  """Builds ALTER TABLE DDL attaching a Data Catalog Policy Tag to fct_turns.user_email."""
  mart = mart_ref(project_id)
  tag = (policy_tag or os.environ.get(POLICY_TAG_PII_ENV) or '').strip()
  if not _POLICY_TAG_RE.match(tag):
    raise ValueError(f'Invalid BigQuery Data Catalog policy tag resource name: {tag!r}')
  return (
      f'ALTER TABLE `{mart}.fct_turns`\n'
      f'ALTER COLUMN user_email SET OPTIONS (policy_tags = ["{tag}"])'
  )


def build_support_turns_sql(project_id: str, hours: int = 168, limit: int = 15) -> str:
  """Recent turns, actionable issues first (failures, quota, permission, guardrail blocks)."""
  h = _clamp(hours, 1, 24 * 365)
  return f"""
    SELECT
      turn_id, turn_source, turn_kind, CAST(event_timestamp AS STRING) AS ts, trace_id, session_id,
      user_email, engine_key, agent_name, model_name, api_method, turn_status, is_actionable_issue,
      status_code, status_message, error_reason, audit_match_method, is_guardrail_blocked,
      guardrail_categories
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_date >= DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR))
      AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR)
    ORDER BY is_actionable_issue DESC, event_timestamp DESC
    LIMIT {_clamp(limit, 1, 500)}
  """


def build_recent_turns_sql(project_id: str, hours: int = 168, limit: int = 20, tokens_only: bool = False) -> str:
  """Recent turns with their measured usage (tokens NULL when not logged)."""
  h = _clamp(hours, 1, 24 * 365)
  where_tokens = 'AND total_tokens IS NOT NULL' if tokens_only else ''
  return f"""
    SELECT
      turn_id, turn_source, turn_kind, CAST(event_timestamp AS STRING) AS ts, session_id, user_email,
      engine_key, agent_name, model_name, api_method, turn_status, status_message, latency_ms,
      input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens, llm_calls,
      tool_call_count, tool_failure_count, tool_names, mcp_server_name
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_date >= DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR))
      AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR)
      AND turn_kind != 'WIDGET_ACTION'
      {where_tokens}
    ORDER BY event_timestamp DESC
    LIMIT {_clamp(limit, 1, 500)}
  """


def build_user_engine_rollup_sql(project_id: str, hours: int = 168) -> str:
  """Per user and GE app: interactions, real sessions and measured tokens."""
  h = _clamp(hours, 1, 24 * 365)
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
    WHERE event_date >= DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR))
      AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR)
      AND user_email IS NOT NULL
    GROUP BY user_email, engine_key
    ORDER BY interactions DESC
    LIMIT 1000
  """


def build_user_model_tokens_sql(project_id: str, hours: int = 168) -> str:
  """Per user, model and GE app: measured token sums, for list-price cost estimates.

  Same window and user filter as build_user_engine_rollup_sql, so estimates cover the turns behind
  the Top users table. Only turns that logged tokens are read; a sum is NULL when no turn logged it.
  """
  h = _clamp(hours, 1, 24 * 365)
  return f"""
    SELECT
      user_email,
      model_name,
      engine_key,
      COUNT(1) AS turns,
      SUM(input_tokens) AS input_tokens,
      SUM(output_tokens) AS output_tokens,
      SUM(cached_input_tokens) AS cached_input_tokens,
      SUM(reasoning_tokens) AS reasoning_tokens,
      SUM(total_tokens) AS total_tokens
    FROM `{mart_ref(project_id)}.fct_turns`
    WHERE event_date >= DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR))
      AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {h} HOUR)
      AND user_email IS NOT NULL
      AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR cached_input_tokens IS NOT NULL)
    GROUP BY user_email, model_name, engine_key
    ORDER BY turns DESC, user_email, model_name, engine_key
    LIMIT 5000
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


def build_recent_sessions_sql(project_id: str, days: int = 30, limit: int = 500) -> str:
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
    session_limit: int = 500,
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
      SELECT engine_key, session_id, session_end
      FROM `{mart}.fct_sessions`
      WHERE session_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {window} DAY)
      ORDER BY session_end DESC
      LIMIT {_clamp(session_limit, 1, 500)}
    )
    SELECT
      COALESCE(r.engine_key, t.engine_key) AS engine_key, t.session_id, t.turn_id, t.turn_kind,
      t.turn_status, CAST(t.event_timestamp AS STRING) AS ts, t.user_email, t.agent_name,
      t.model_name, t.latency_ms, t.input_tokens, t.output_tokens, t.cached_input_tokens,
      t.reasoning_tokens, t.total_tokens, t.llm_calls, t.tool_call_count, t.tool_failure_count,
      t.tool_names, t.mcp_server_name
    FROM `{mart}.fct_turns` AS t
    JOIN recent_sessions AS r
      ON t.session_id = r.session_id
    WHERE t.session_id IS NOT NULL
      AND t.event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {window} DAY)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY t.session_id ORDER BY t.event_timestamp DESC
    ) <= {_clamp(turns_per_session, 1, 500)}
    ORDER BY r.session_end DESC, t.session_id, t.event_timestamp
    LIMIT {_clamp(limit, 1, 5000)}
  """


def build_sessionless_token_turns_sql(project_id: str, days: int = 30, limit: int = 500) -> str:
  """Token-bearing turns without a session_id (e.g. standalone Vertex Agent Engine traces)."""
  mart = mart_ref(project_id)
  window = _clamp(days, 1, 400)
  return f"""
    SELECT
      engine_key, session_id, turn_id, turn_kind, turn_status,
      CAST(event_timestamp AS STRING) AS ts, user_email, agent_name, model_name,
      latency_ms, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens,
      llm_calls, tool_call_count, tool_failure_count, tool_names, mcp_server_name
    FROM `{mart}.fct_turns`
    WHERE session_id IS NULL
      AND total_tokens IS NOT NULL
      AND event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {window} DAY)
    ORDER BY event_timestamp DESC
    LIMIT {_clamp(limit, 1, 500)}
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


def support_event_from_row(row: Mapping[str, Any], project_id: str) -> dict[str, Any]:
  """Maps a fct_turns row to the dashboard's support-event dict (same keys as before)."""
  source_table = f'{mart_ref(project_id)}.fct_turns'
  turn_id = str(row.get('turn_id') or '')
  status = str(row.get('turn_status') or 'UNKNOWN')
  actionable = bool_or_none(row.get('is_actionable_issue')) is True
  blocked = bool_or_none(row.get('is_guardrail_blocked')) is True
  method = _text(row.get('api_method')) or _text(row.get('turn_kind')) or 'turn'
  user = redact_email_if_configured(_text(row.get('user_email')))
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


def usage_log_from_row(row: Mapping[str, Any], project_id: str) -> dict[str, Any]:
  """Maps a fct_turns row to an aive usage-log dict. No prompt text, no invented latency or rating."""
  source_table = f'{mart_ref(project_id)}.fct_turns'
  turn_id = str(row.get('turn_id') or '')
  email = redact_email_if_configured(_text(row.get('user_email')))
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
      'latency_ms': int_or_none(row.get('latency_ms')),
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


def mart_refreshed_at(rows: Sequence[Mapping[str, Any]]) -> str | None:
  """Latest fct_turns rebuild time seen in the rows (None when unknown, e.g. no rows)."""
  stamps = [str(r['refreshed_at']) for r in rows if r.get('refreshed_at')]
  return max(stamps) if stamps else None


def daily_usage_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
  out: dict[str, Any] = {'day': str(row.get('day') or '')}
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


def user_model_tokens_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
  """Maps a build_user_model_tokens_sql row. Token sums no turn reported stay None."""
  return {
      'user_email': redact_email_if_configured(_text(row.get('user_email'))),
      'model_name': _text(row.get('model_name')),
      'engine_key': _text(row.get('engine_key')),
      'turns': int_or_none(row.get('turns')) or 0,
      **{k: int_or_none(row.get(k))
         for k in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'total_tokens')},
  }


def session_from_row(row: Mapping[str, Any], project_id: str) -> dict[str, Any]:
  """Maps a fct_sessions row to a dashboard session dict. Unknown token/call values remain None."""
  return {
      'session_id': str(row.get('session_id') or ''),
      'session_key': session_key(row.get('engine_key'), row.get('session_id')),
      'engine_key': _text(row.get('engine_key')),
      'session_start': _text(row.get('session_start')),
      'session_end': _text(row.get('session_end')),
      'duration_seconds': int_or_none(row.get('duration_seconds')),
      'session_date': _text(row.get('session_date')),
      'user_email': redact_email_if_configured(_text(row.get('user_email'))),
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
  """Stable key for a fct_sessions row (the view groups by session_id with primary engine_key)."""
  return f"{_text(engine_key) or ''}|{_text(session_id) or ''}"


def session_turn_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
  """Maps a build_session_turns_sql row to a token-only turn dict (unknown counts stay None)."""
  return {
      'turn_id': str(row.get('turn_id') or ''),
      'ts': _text(row.get('ts')),
      'turn_kind': _text(row.get('turn_kind')),
      'turn_status': _text(row.get('turn_status')),
      'user_email': redact_email_if_configured(_text(row.get('user_email'))),
      'agent_name': _text(row.get('agent_name')),
      'model_name': _text(row.get('model_name')),
      'latency_ms': int_or_none(row.get('latency_ms')),
      'input_tokens': int_or_none(row.get('input_tokens')),
      'output_tokens': int_or_none(row.get('output_tokens')),
      'cached_input_tokens': int_or_none(row.get('cached_input_tokens')),
      'reasoning_tokens': int_or_none(row.get('reasoning_tokens')),
      'total_tokens': int_or_none(row.get('total_tokens')),
      'llm_calls': int_or_none(row.get('llm_calls')),
      'tool_calls': int_or_none(row.get('tool_call_count')),
      'tool_failures': int_or_none(row.get('tool_failure_count')),
      'tool_names': _text(row.get('tool_names')),
      'mcp_server_name': _text(row.get('mcp_server_name')),
  }


def group_session_turns(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
  """Groups session-turn rows by session_key(), keeping the row order (oldest turn first)."""
  grouped: dict[str, list[dict[str, Any]]] = {}
  for row in rows:
    if not _text(row.get('session_id')):
      continue
    grouped.setdefault(session_key(row.get('engine_key'), row.get('session_id')), []).append(
        session_turn_from_row(row))
  return grouped

