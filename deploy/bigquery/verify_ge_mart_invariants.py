#!/usr/bin/env python3
"""Automated conservation and reconciliation invariant verifier for the Gemini Enterprise mart.

Runs 10 mathematical zero-loss invariants across the curated views (`v_user_activity_curated`,
`v_agentic_operations_curated`, `v_consolidated_audit_log`, `v_model_armor_curated`) and mart
objects (`v_fct_turns`, `fct_turns`, `fct_sessions`, `agg_daily_usage`). Any non-zero violation
causes a non-zero exit code so CI/CD or post-provision checks fail immediately on data loss,
fan-out, or schema drift.

Usage:
  python3 deploy/bigquery/verify_ge_mart_invariants.py --project project-maui
  python3 deploy/bigquery/verify_ge_mart_invariants.py \\
    --project project-maui \\
    --curated-dataset ge_stage_support_curated \\
    --mart-dataset ge_stage_support_mart
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

_THIS_DIR = pathlib.Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
  sys.path.insert(0, str(_THIS_DIR))

import provision_ge_mart as provision  # noqa: E402


@dataclass(frozen=True)
class InvariantResult:
  name: str
  violations: int
  detail: str

  @property
  def passed(self) -> bool:
    return self.violations == 0


def build_invariant_sql(project: str, curated_dataset: str, mart_dataset: str) -> str:
  """Builds a single BigQuery SQL query returning (name, violations, detail) for all 10 invariants."""
  provision.validate_project(project)
  provision.validate_dataset(curated_dataset)
  provision.validate_dataset(mart_dataset)
  curated = f'{project}.{curated_dataset}'
  mart = f'{project}.{mart_dataset}'
  return f"""
WITH
  turns AS (
    SELECT
      COUNT(1) AS total_turns,
      COUNTIF(session_id IS NULL) AS sessionless_turns,
      COALESCE(SUM(total_tokens), 0) AS total_tokens,
      COALESCE(SUM(IF(session_id IS NULL, total_tokens, 0)), 0) AS sessionless_tokens,
      COALESCE(SUM(tool_call_count), 0) AS tool_calls,
      COALESCE(SUM(tool_failure_count), 0) AS tool_failures,
      COUNTIF(COALESCE(tool_failure_count, 0) > COALESCE(tool_call_count, 0)) AS bad_turn_failure_bounds,
      COUNTIF(is_guardrail_blocked) AS guardrail_blocks
    FROM `{mart}.fct_turns`
  ),
  live_view_turns AS (
    SELECT
      COUNT(1) AS total_turns,
      COALESCE(SUM(total_tokens), 0) AS total_tokens
    FROM `{mart}.v_fct_turns`
  ),
  sessions AS (
    SELECT
      COUNT(1) AS row_count,
      COUNT(DISTINCT session_id) AS distinct_sessions,
      COALESCE(SUM(turns), 0) AS total_turns,
      COALESCE(SUM(total_tokens), 0) AS total_tokens
    FROM `{mart}.fct_sessions`
  ),
  daily AS (
    SELECT
      COALESCE(SUM(interactions), 0) AS total_turns,
      COALESCE(SUM(total_tokens), 0) AS total_tokens
    FROM `{mart}.agg_daily_usage`
  ),
  inference_by_trace AS (
    SELECT
      COALESCE(SUM(trace_tokens), 0) AS total_tokens,
      COUNTIF(bad_failure_bounds > 0) AS bad_failure_rows
    FROM (
      -- Within a trace, LLM call N emits tool_call in output_messages and LLM call N+1 receives
      -- the tool response (and any failure) in input_messages[last], so tool_failure_count <= tool_call_count
      -- is a trace-level and turn-level invariant.
      SELECT
        COALESCE(trace_id, insert_id) AS trace_key,
        SUM(total_tokens) AS trace_tokens,
        IF(COALESCE(SUM(tool_failure_count), 0) > COALESCE(SUM(tool_call_count), 0), 1, 0) AS bad_failure_bounds
      FROM `{curated}.v_agentic_operations_curated`
      GROUP BY 1
    )
  ),
  upload_sessions AS (
    SELECT
      COUNTIF(REGEXP_CONTAINS( COALESCE(api_method, ''), r'UploadSessionFile') AND session_id IS NULL) AS missing_session_id,
      COUNTIF(REGEXP_CONTAINS( COALESCE(api_method, ''), r'UploadSessionFile') AND NOT COALESCE(has_uploaded_file, FALSE)) AS missing_upload_flag
    FROM `{curated}.v_user_activity_curated`
  ),
  armor_checks AS (
    SELECT
      COUNTIF(
        is_blocked
        AND (
          REGEXP_CONTAINS(LOWER(COALESCE(verdict_reason, '')), r'not blocked|enforcement type is inspect only')
          OR UPPER(COALESCE(execution_state, '')) IN ('SANITIZATION_EXECUTION_SKIPPED', 'SANITZATION_EXECUTION_SKIPPED')
        )
      ) AS false_positive_blocks
    FROM `{curated}.v_model_armor_curated`
  ),
  audit_checks AS (
    SELECT
      COUNTIF(
        session_id IS NULL
        AND REGEXP_CONTAINS(COALESCE(resource_name, ''), r'sessions/[^/\"\\s-]+')
      ) AS unextracted_audit_sessions
    FROM `{curated}.v_consolidated_audit_log`
  )
SELECT
  'fct_sessions_unique_session_id' AS name,
  CAST(s.row_count - s.distinct_sessions AS INT64) AS violations,
  CONCAT('rows=', CAST(s.row_count AS STRING), ' distinct_sessions=', CAST(s.distinct_sessions AS STRING)) AS detail
FROM sessions AS s
UNION ALL
SELECT
  'session_turn_conservation' AS name,
  CAST(ABS((s.total_turns + t.sessionless_turns) - t.total_turns) AS INT64) AS violations,
  CONCAT('session_turns=', CAST(s.total_turns AS STRING), ' sessionless_turns=', CAST(t.sessionless_turns AS STRING), ' fct_turns=', CAST(t.total_turns AS STRING)) AS detail
FROM sessions AS s, turns AS t
UNION ALL
SELECT
  'session_token_conservation' AS name,
  CAST(ABS((s.total_tokens + t.sessionless_tokens) - t.total_tokens) AS INT64) AS violations,
  CONCAT('session_tokens=', CAST(s.total_tokens AS STRING), ' sessionless_tokens=', CAST(t.sessionless_tokens AS STRING), ' fct_turns_tokens=', CAST(t.total_tokens AS STRING)) AS detail
FROM sessions AS s, turns AS t
UNION ALL
SELECT
  'daily_agg_turn_conservation' AS name,
  CAST(ABS(d.total_turns - t.total_turns) AS INT64) AS violations,
  CONCAT('agg_daily_turns=', CAST(d.total_turns AS STRING), ' fct_turns=', CAST(t.total_turns AS STRING)) AS detail
FROM daily AS d, turns AS t
UNION ALL
SELECT
  'daily_agg_token_conservation' AS name,
  CAST(ABS(d.total_tokens - t.total_tokens) AS INT64) AS violations,
  CONCAT('agg_daily_tokens=', CAST(d.total_tokens AS STRING), ' fct_turns_tokens=', CAST(t.total_tokens AS STRING)) AS detail
FROM daily AS d, turns AS t
UNION ALL
SELECT
  'inference_token_conservation' AS name,
  CAST(ABS(i.total_tokens - t.total_tokens) AS INT64) AS violations,
  CONCAT('curated_inference_tokens=', CAST(i.total_tokens AS STRING), ' fct_turns_tokens=', CAST(t.total_tokens AS STRING)) AS detail
FROM inference_by_trace AS i, turns AS t
UNION ALL
SELECT
  'tool_failure_bounds' AS name,
  CAST(i.bad_failure_rows + t.bad_turn_failure_bounds + IF(t.tool_failures > t.tool_calls, 1, 0) AS INT64) AS violations,
  CONCAT('tool_failures=', CAST(t.tool_failures AS STRING), ' tool_calls=', CAST(t.tool_calls AS STRING)) AS detail
FROM inference_by_trace AS i, turns AS t
UNION ALL
SELECT
  'file_upload_session_coverage' AS name,
  CAST(u.missing_session_id + u.missing_upload_flag AS INT64) AS violations,
  CONCAT('missing_session_id=', CAST(u.missing_session_id AS STRING), ' missing_upload_flag=', CAST(u.missing_upload_flag AS STRING)) AS detail
FROM upload_sessions AS u
UNION ALL
SELECT
  'armor_inspect_only_exclusion' AS name,
  CAST(a.false_positive_blocks AS INT64) AS violations,
  CONCAT('false_positive_blocks=', CAST(a.false_positive_blocks AS STRING)) AS detail
FROM armor_checks AS a
UNION ALL
SELECT
  'audit_session_extraction_coverage' AS name,
  CAST(ac.unextracted_audit_sessions AS INT64) AS violations,
  CONCAT('unextracted_audit_sessions=', CAST(ac.unextracted_audit_sessions AS STRING)) AS detail
FROM audit_checks AS ac
UNION ALL
SELECT
  'materialized_fct_turns_sync' AS name,
  CAST(ABS(v.total_turns - t.total_turns) + ABS(v.total_tokens - t.total_tokens) AS INT64) AS violations,
  CONCAT('v_fct_turns=', CAST(v.total_turns AS STRING), ' fct_turns=', CAST(t.total_turns AS STRING)) AS detail
FROM live_view_turns AS v, turns AS t
ORDER BY name
""".strip()


def run_bq_query_rows(project: str, location: str, sql: str) -> list[dict[str, object]]:
  """Executes a BigQuery query via the bq CLI and returns parsed JSON rows."""
  import os  # pylint: disable=import-outside-toplevel
  import re  # pylint: disable=import-outside-toplevel
  provision.validate_project(project)
  if not re.match(r'^[A-Za-z0-9_-]{2,32}$', location or ''):
    raise ValueError(f'Invalid BigQuery location: {location!r}')
  env = dict(os.environ)
  env['PATH'] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
  existing_metrics = env.get('CLOUDSDK_METRICS_ENVIRONMENT', '').strip()
  env['CLOUDSDK_METRICS_ENVIRONMENT'] = (f'{existing_metrics} datacloud.antigravity'.strip()
                                         if existing_metrics else 'datacloud.antigravity')
  cmd = [
      'bq', f'--project_id={project}', f'--location={location}',
      'query', '--use_legacy_sql=false', '--format=json',
      '--label', 'datacloud:antigravity',
      sql,
  ]
  proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=180, env=env)
  if proc.returncode != 0:
    detail = (proc.stderr or proc.stdout).strip()
    raise RuntimeError(f'bq query failed: {detail}')
  raw = (proc.stdout or '').strip()
  if not raw:
    return []
  start = raw.find('[')
  if start < 0:
    return []
  return json.loads(raw[start:])


def verify_mart_invariants(
    project: str,
    curated_dataset: str = provision.DEFAULT_CURATED_DATASET,
    mart_dataset: str = provision.DEFAULT_MART_DATASET,
    location: str = provision.DEFAULT_LOCATION,
    query_runner: Callable[[str, str, str], list[dict[str, object]]] = run_bq_query_rows,
) -> list[InvariantResult]:
  """Runs all mart invariants and returns their structured results."""
  sql = build_invariant_sql(project, curated_dataset, mart_dataset)
  rows = query_runner(project, location, sql)
  results: list[InvariantResult] = []
  for r in rows:
    results.append(
        InvariantResult(
            name=str(r.get('name') or ''),
            violations=int(str(r.get('violations') or 0)),
            detail=str(r.get('detail') or ''),
        )
    )
  return results


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument('--project', required=True, help='GCP project ID hosting the mart datasets.')
  parser.add_argument('--curated-dataset', default=provision.DEFAULT_CURATED_DATASET)
  parser.add_argument('--mart-dataset', default=provision.DEFAULT_MART_DATASET)
  parser.add_argument('--location', default=provision.DEFAULT_LOCATION)
  parser.add_argument('--print-sql', action='store_true', help='Print the invariant SQL and exit.')
  args = parser.parse_args(argv)

  if args.print_sql:
    print(build_invariant_sql(args.project, args.curated_dataset, args.mart_dataset))
    return 0

  results = verify_mart_invariants(
      project=args.project,
      curated_dataset=args.curated_dataset,
      mart_dataset=args.mart_dataset,
      location=args.location,
  )
  failed = [r for r in results if not r.passed]
  print(f'Verified {len(results)} invariants on {args.project}.{args.mart_dataset}:')
  for r in results:
    status = 'PASS' if r.passed else 'FAIL'
    print(f'  [{status}] {r.name:<36} violations={r.violations} ({r.detail})')
  if failed:
    print(f'\nERROR: {len(failed)} invariant(s) failed.', file=sys.stderr)
    return 1
  print('\nAll invariants passed with 0 violations.')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
