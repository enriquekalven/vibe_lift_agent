"""Tests for the Gemini Enterprise reporting mart: provisioner, SQL templates, readers and cost join.

The provisioner lives in deploy/ (not shipped in the image), so it is loaded by path.
"""

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock

from vibelift import billing_export, gcp_telemetry, ge_mart

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'provision_ge_mart', _ROOT / 'deploy' / 'bigquery' / 'provision_ge_mart.py')
provision = importlib.util.module_from_spec(_SPEC)
sys.modules['provision_ge_mart'] = provision  # dataclasses resolve types via sys.modules
_SPEC.loader.exec_module(provision)  # type: ignore[union-attr]


def _sql_code(sql: str) -> str:
  """SQL without full-line `--` comments (the comments name the stage bugs on purpose)."""
  return '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))


def _describe_all_found(ref: str):
  """Fake table describer: every source exists in US with record columns."""
  del ref
  return {
      '__location__': 'US', 'timestamp': 'TIMESTAMP', 'trace': 'STRING', 'insertId': 'STRING',
      'severity': 'STRING', 'jsonPayload': 'RECORD', 'resource': 'RECORD', 'labels': 'RECORD',
      'protopayload_auditlog': 'RECORD', 'jsonpayload_v1_sanitizeoperationlogentry': 'RECORD',
  }


class ProvisionerTest(unittest.TestCase):

  def test_render_fails_on_missing_placeholder(self):
    with self.assertRaises(KeyError):
      provision.render('SELECT * FROM `{{curated}}.x` WHERE {{other}}', {'curated': 'p.d'})

  def test_render_fails_on_malformed_placeholder(self):
    with self.assertRaises(ValueError):
      provision.render('SELECT {{ 1bad }}', {})

  def test_identifier_validation_rejects_injection(self):
    for bad in ('proj.ds.t`; DROP TABLE x; --', 'proj-ect.ds', 'Project-Maui.ds.t', 'project-maui.d s.t'):
      with self.assertRaises(ValueError, msg=bad):
        provision.validate_table_ref(bad)
    with self.assertRaises(ValueError):
      provision.validate_dataset('ds`; --')
    self.assertEqual(provision.validate_table_ref('`project-maui.ds_a.tbl_1`'), 'project-maui.ds_a.tbl_1')

  def test_source_sql_missing_table_is_empty_stub(self):
    sql = provision.source_sql(None, None, ('jsonPayload',), 30)
    self.assertIn('WHERE FALSE', sql)
    self.assertIn('CAST(NULL AS JSON) AS jsonPayload', sql)

  def test_source_sql_adapts_column_types(self):
    schema = {'timestamp': 'TIMESTAMP', 'jsonPayload': 'RECORD', 'labels': 'STRING', 'resource': 'JSON'}
    sql = provision.source_sql('project-maui.ds.t', schema, ('jsonPayload', 'labels', 'resource', 'absent'), 999)
    self.assertIn('TO_JSON(`jsonPayload`) AS jsonPayload', sql)
    self.assertIn('SAFE.PARSE_JSON(`labels`) AS labels', sql)
    self.assertIn('`resource` AS resource', sql)
    self.assertIn('CAST(NULL AS JSON) AS absent', sql)
    self.assertIn('CAST(NULL AS STRING) AS trace', sql)  # missing scalar column
    self.assertIn('INTERVAL 400 DAY', sql)  # lookback is clamped

  def test_resolve_sources_missing_and_wrong_location(self):
    def describe(ref):
      if 'ds_vertex_agents_raw' in ref:
        return None
      if 'ds_ge_search_raw' in ref:
        return dict(_describe_all_found(ref), __location__='us-east1')
      return _describe_all_found(ref)

    out = provision.resolve_sources('project-maui', 'US', 30, describe)
    self.assertEqual(out['inference'].status, 'MISSING')
    self.assertEqual(out['search'].status, 'WRONG_LOCATION')
    self.assertIn('WHERE FALSE', out['search'].sql)
    self.assertEqual(out['assistant'].status, 'FOUND')
    self.assertNotIn('armor', out)
    with self.assertRaises(ValueError):
      provision.resolve_sources('project-maui', 'US', 30, describe, overrides={'nope': 'a-proj.b.c'})

  def test_model_armor_commented_out_until_enabled(self):
    self.assertNotIn('armor', provision.SOURCES)
    self.assertNotIn('v_model_armor_curated', [name for _, name, _ in provision.OBJECTS])
    self.assertIn(('curated', 'v_model_armor_curated'), provision.DISABLED_OBJECTS)
    turns_code = _sql_code(provision.load_template('mart', 'v_fct_turns'))
    self.assertNotIn('v_model_armor_curated', turns_code)
    self.assertIn('CAST(NULL AS BOOL) AS is_guardrail_blocked', turns_code)

  def test_build_view_bodies_renders_all_views(self):
    sources = provision.resolve_sources('project-maui', 'US', 30, _describe_all_found)
    bodies = provision.build_view_bodies('project-maui', 'ds_ge_curated_staging', 'vibelift_mart', sources)
    self.assertEqual(len(bodies), len(provision.OBJECTS))
    for name, body in bodies.items():
      self.assertNotIn('{{', body, name)
      self.assertNotIn('}}', body, name)
      self.assertNotIn('gemini-enterprise-stage', _sql_code(body), name)  # no hard-coded project ids
    turns = bodies['vibelift_mart.v_fct_turns']
    self.assertIn('`project-maui.ds_ge_curated_staging.v_user_activity_curated`', turns)
    self.assertIn('FROM `project-maui.vibelift_mart.v_fct_turns`', bodies['vibelift_mart.fct_turns'])
    self.assertIn('refreshed_at', bodies['vibelift_mart.fct_turns'])
    self.assertIn('`project-maui.vibelift_mart.fct_turns`', bodies['vibelift_mart.agg_daily_usage'])
    # Audit window: 300 s lag / 30 s lead in ms, AUDIT_ONLY successes excluded by default.
    self.assertIn('300000', turns)
    self.assertIn('30000', turns)

  def test_fct_turns_is_a_partitioned_table_and_the_rest_are_views(self):
    ddl = provision.object_ddl('project-maui', 'vibelift_mart.fct_turns', 'SELECT 1', 'd')
    self.assertTrue(ddl.startswith('CREATE OR REPLACE TABLE `project-maui.vibelift_mart.fct_turns`'))
    self.assertIn('PARTITION BY event_date', ddl)
    self.assertIn('CLUSTER BY engine_key, user_email', ddl)
    for _, name, _ in provision.OBJECTS:
      if name != 'fct_turns':
        self.assertNotIn(name, provision.TABLES)
        self.assertIn('CREATE OR REPLACE VIEW', provision.object_ddl('project-maui', f'd.{name}', 'SELECT 1', 'd'))

  def test_view_ddl_escapes_description(self):
    ddl = provision.view_ddl('project-maui', 'vibelift_mart.fct_turns', 'SELECT 1', 'say "hi"')
    self.assertIn('description="say \\"hi\\""', ddl)
    self.assertTrue(ddl.startswith('CREATE OR REPLACE VIEW `project-maui.vibelift_mart.fct_turns`'))

  def test_inline_view_refs_for_dry_run(self):
    bodies = {'m.a': 'SELECT 1 AS x', 'm.b': 'SELECT x FROM `p-proj1.m.a`'}
    sql = provision.inline_view_refs('SELECT * FROM `p-proj1.m.b`', 'p-proj1', bodies)
    self.assertNotIn('`p-proj1.m.', sql)
    self.assertIn('SELECT 1 AS x', sql)


class TemplateDefectFixTest(unittest.TestCase):
  """Guards the stage defects fixed during the port (see docs/GE_MART.md)."""

  def _tpl(self, layer, name):
    return _sql_code(provision.load_template(layer, name))

  def test_turns_join_inference_only_once_per_trace(self):
    # Stage fanned tokens out across every activity row of a trace (FULL OUTER JOIN on trace).
    turns = self._tpl('mart', 'v_fct_turns')
    self.assertIn('LEFT JOIN inference AS i ON t.trace_rank = 1', turns)
    self.assertNotIn('FULL OUTER JOIN', turns.upper())

  def test_audit_matching_does_not_re_read_ctes(self):
    # BigQuery re-evaluates a CTE at every reference; NOT IN over the round-1 matches made the
    # stage validation query take 446 slot-seconds and 102 stages for 50 rows.
    turns = self._tpl('mart', 'v_fct_turns').upper()
    self.assertNotIn('NOT IN (SELECT', turns)
    self.assertEqual(turns.count('FROM AUDIT_PAIRS'), 1)
    self.assertEqual(turns.count('JOIN AUDIT_MATCHES'), 1)

  def test_armor_verdict_prefix_stripped(self):
    self.assertIn("r'^MODEL_ARMOR_SANITIZATION_VERDICT_'", self._tpl('curated', 'v_model_armor_curated'))

  def test_activity_uses_lowercase_sink_paths(self):
    activity = self._tpl('curated', 'v_user_activity_curated')
    self.assertIn('logmetadata.methodname', activity)
    self.assertNotIn('logMetadata.methodName', activity)

  def test_audit_uses_rpc_codes_and_dedupes_streaming_ops(self):
    audit = self._tpl('curated', 'v_consolidated_audit_log')
    self.assertIn("WHEN status_code = 8 THEN 'RATE_LIMITED'", audit)
    self.assertIn('LAX_STRING(op.id)', audit)
    self.assertIn('QUALIFY ROW_NUMBER() OVER', audit)

  def test_elided_activity_principal_becomes_null_and_matches_by_engine_time(self):
    activity = self._tpl('curated', 'v_user_activity_curated')
    self.assertIn("REGEXP_CONTAINS(COALESCE(LAX_STRING(jp.useriamprincipal), ''), r'@')", activity)
    turns = self._tpl('mart', 'v_fct_turns')
    self.assertIn("'ENGINE_TIME'", turns)
    self.assertIn('ON t.user_email IS NULL', turns)

  def test_no_pii_columns(self):
    for layer, name, _ in provision.OBJECTS:
      body = self._tpl(layer, name).lower()
      self.assertNotIn('callerip', body, name)
      self.assertNotIn('caller_ip', body, name)


class GeMartReaderTest(unittest.TestCase):

  def test_refs_default_and_env_override(self):
    with mock.patch.dict(os.environ, {ge_mart.MART_DATASET_ENV: '', ge_mart.CURATED_DATASET_ENV: ''}):
      self.assertEqual(ge_mart.mart_ref('project-maui'), 'project-maui.vibelift_mart')
      self.assertEqual(ge_mart.curated_ref('project-maui'), 'project-maui.ds_ge_curated_staging')
    with mock.patch.dict(os.environ, {ge_mart.MART_DATASET_ENV: 'bad`; --'}):
      with self.assertRaises(ValueError):
        ge_mart.mart_ref('project-maui')
    with self.assertRaises(ValueError):
      ge_mart.build_support_turns_sql("x' OR 1=1")

  def test_sql_limits_are_clamped(self):
    sql = ge_mart.build_recent_turns_sql('project-maui', hours=10**9, limit=10**9, tokens_only=True)
    self.assertIn('INTERVAL 8760 HOUR', sql)
    self.assertIn('LIMIT 500', sql)
    self.assertIn('total_tokens IS NOT NULL', sql)
    for hrs in (720, 2160, 4320, 8760):
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_support_turns_sql('project-maui', hours=hrs))
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_user_engine_rollup_sql('project-maui', hours=hrs))
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_audit_principals_sql('project-maui', hours=hrs))

  def test_usage_log_keeps_unknowns_as_none(self):
    row = {'turn_id': 'ACTIVITY:abc', 'ts': '2026-09-29 10:00:00', 'turn_status': 'SUCCESS',
           'user_email': 'a@example.com', 'total_tokens': None, 'input_tokens': '', 'model_name': None}
    log = ge_mart.usage_log_from_row(row, 'project-maui')
    self.assertEqual(log['prompts'], [])
    self.assertIsNone(log['latency_ms'])
    self.assertIsNone(log['csat_rating'])
    self.assertIsNone(log['total_tokens'])
    self.assertIsNone(log['input_tokens'])
    self.assertIsNone(log['model_name'])
    self.assertTrue(log['outputs'][0]['gcs_uri'].startswith('bq://project-maui.'))

  def test_usage_log_parses_rest_strings(self):
    log = ge_mart.usage_log_from_row({'turn_id': 't', 'total_tokens': '38520', 'reasoning_tokens': '0'},
                                     'project-maui')
    self.assertEqual(log['total_tokens'], 38520)
    self.assertEqual(log['thinking_tokens'], 0)

  def test_support_event_guardrail_block_is_actionable(self):
    row = {'turn_id': 'ACTIVITY:xyz', 'turn_status': 'SUCCESS', 'is_actionable_issue': 'true',
           'is_guardrail_blocked': 'true', 'guardrail_categories': 'SENSITIVE_DATA', 'status_code': '0'}
    ev = ge_mart.support_event_from_row(row, 'project-maui')
    self.assertEqual(ev['category'], 'GUARDRAIL_BLOCK')
    self.assertEqual(ev['tier'], 'L2 Actionable')
    self.assertEqual(ev['resolution_status'], 'NEEDS_ATTENTION')
    self.assertIn('SENSITIVE_DATA', ev['issue_summary'])
    self.assertEqual(ev['status_code'], 0)
    self.assertIsNone(ev['model_name'])

  def test_support_event_success_not_actionable(self):
    ev = ge_mart.support_event_from_row({'turn_id': 'a', 'turn_status': 'SUCCESS',
                                         'is_actionable_issue': 'false'}, 'project-maui')
    self.assertFalse(ev['is_actionable_issue'])
    self.assertEqual(ev['resolution_status'], 'NO_ACTION')
    self.assertEqual(ev['resolution_action'], 'no audit record matched')

  def test_mart_refreshed_at_unknown_without_rows(self):
    self.assertIsNone(ge_mart.mart_refreshed_at([]))
    self.assertIsNone(ge_mart.mart_refreshed_at([{'day': '2026-09-29', 'refreshed_at': None}]))
    rows = [{'refreshed_at': '2026-09-29 10:00:00+00'}, {'refreshed_at': '2026-09-29 11:00:00+00'}]
    self.assertEqual(ge_mart.mart_refreshed_at(rows), '2026-09-29 11:00:00+00')
    self.assertIn('MAX(refreshed_at)', ge_mart.build_daily_totals_sql('project-maui'))

  def test_daily_usage_tokens_none_counts_zero(self):
    out = ge_mart.daily_usage_from_row({'day': '2026-09-29', 'interactions': '5', 'failed_turns': None,
                                        'total_tokens': None, 'input_tokens': '12'})
    self.assertEqual(out['interactions'], 5)
    self.assertEqual(out['failed_turns'], 0)
    self.assertIsNone(out['total_tokens'])
    self.assertEqual(out['input_tokens'], 12)

  def test_sessions_and_refresh_ddl(self):
    ddl = ge_mart.build_refresh_fct_turns_ddl('project-maui')
    self.assertIn('CREATE OR REPLACE TABLE `project-maui.vibelift_mart.fct_turns`', ddl)
    self.assertIn('CURRENT_TIMESTAMP() AS refreshed_at', ddl)
    self.assertIn('FROM `project-maui.vibelift_mart.v_fct_turns`', ddl)
    sql = ge_mart.build_recent_sessions_sql('project-maui', days=10**9, limit=10**9)
    self.assertIn('FROM `project-maui.vibelift_mart.fct_sessions`', sql)
    self.assertIn('INTERVAL 400 DAY', sql)
    self.assertIn('LIMIT 500', sql)
    sess = ge_mart.session_from_row(
        {'session_id': 's1', 'engine_key': 'global/us-1', 'user_email': 'u@example.com',
         'turns': '4', 'chat_turns': '3', 'failed_turns': '1', 'duration_seconds': '42',
         'total_tokens': None, 'input_tokens': '100', 'agent_name': 'Helper'},
        'project-maui',
    )
    self.assertEqual(sess['session_id'], 's1')
    self.assertEqual(sess['turns'], 4)
    self.assertEqual(sess['chat_turns'], 3)
    self.assertEqual(sess['failed_turns'], 1)
    self.assertEqual(sess['duration_seconds'], 42)
    self.assertIsNone(sess['total_tokens'])
    self.assertEqual(sess['input_tokens'], 100)
    self.assertEqual(sess['agent_name'], 'Helper')

  def test_refresh_ge_mart_turns_executes_ddl_and_clears_caches(self):
    service = gcp_telemetry.GoogleCloudTelemetryService('project-maui', region='us-central1')
    service._cached_bq_insights = {'cached': True}
    service._cached_bq_insights_ts = 123.0
    with mock.patch.object(service, '_query_bigquery_rest',
                           side_effect=[[], [{'row_count': '30', 'refreshed_at': '2026-09-30 02:14:04+00'}]]):
      res = service.refresh_ge_mart_turns()
    self.assertEqual(res['status'], 'REFRESHED')
    self.assertEqual(res['row_count'], 30)
    self.assertEqual(res['refreshed_at'], '2026-09-30 02:14:04+00')
    self.assertIsNone(service._cached_bq_insights)


class FleetSummaryFromMartTest(unittest.TestCase):

  def test_spend_is_never_modelled(self):
    service = gcp_telemetry.GoogleCloudTelemetryService('project-maui', region='us-central1')
    rows = [
        {'day': '2026-09-29', 'agent_name': 'helper', 'model_name': 'gemini-2.5-flash', 'interactions': '4',
         'turns_with_tokens': '1', 'input_tokens': '1000', 'cached_input_tokens': '250',
         'output_tokens': '50', 'total_tokens': '1050'},
        {'day': '2026-09-29', 'agent_name': None, 'model_name': None, 'interactions': '9',
         'turns_with_tokens': '0', 'input_tokens': None, 'cached_input_tokens': None,
         'output_tokens': None, 'total_tokens': None},
    ]
    with mock.patch.object(service, '_query_bigquery_rest', return_value=rows):
      out = service.fetch_bigquery_fleet_summary()
    self.assertIsNone(out['total_actual_spend_usd'])
    self.assertIsNone(out['total_naive_spend_usd'])
    self.assertEqual(out['total_turns'], 13)
    by_agent = {g['agent_id']: g for g in out['fleet_breakdown']}
    self.assertEqual(by_agent['helper']['cache_hit_ratio'], 25.0)
    self.assertIsNone(by_agent[None]['prompt_tokens'])
    self.assertIsNone(by_agent[None]['cache_hit_ratio'])
    self.assertIsNone(by_agent[None]['avg_latency_ms'])

  def test_no_rows_returns_none(self):
    service = gcp_telemetry.GoogleCloudTelemetryService('project-maui', region='us-central1')
    with mock.patch.object(service, '_query_bigquery_rest', return_value=[]):
      self.assertIsNone(service.fetch_bigquery_fleet_summary())


class DailyCostJoinTest(unittest.TestCase):

  _USAGE = (
      {'day': '2026-09-20', 'interactions': 10, 'total_tokens': None},   # before export started
      {'day': '2026-09-25', 'interactions': 10, 'total_tokens': 2_000_000},
      {'day': '2026-09-26', 'interactions': 0, 'total_tokens': 0},       # exported, no AI charges
      {'day': '2026-09-27', 'interactions': 4, 'total_tokens': None},    # after last billed day
  )

  def _cost(self):
    rows = [
        {'day': '2026-09-24', 'service': 'Cloud Run', 'gross_usd': '1', 'credits_usd': '0', 'currency': 'USD'},
        {'day': '2026-09-25', 'service': 'Vertex AI', 'gross_usd': '3', 'credits_usd': '-1', 'currency': 'USD'},
        {'day': '2026-09-25', 'service': 'Cloud Run', 'gross_usd': '5', 'credits_usd': '0', 'currency': 'USD'},
        {'day': '2026-09-26', 'service': 'Cloud Storage', 'gross_usd': '0.1', 'credits_usd': '0'},
    ]
    return billing_export.summarize_daily_cost_rows(rows, 'billing-proj.billing.export_v1')

  def test_not_connected_keeps_cost_none(self):
    out = billing_export.join_daily_usage_with_cost(self._USAGE, billing_export.daily_cost_not_connected())
    for row in out:
      self.assertIsNone(row['ai_net_usd'])
      self.assertIsNone(row['usd_per_1k_turns'])
      self.assertIsNone(row['usd_per_1m_tokens'])
      self.assertIsNone(row['cost_scope'])

  def test_summarize_splits_ai_from_other_services(self):
    cost = self._cost()
    by_day = {d['day']: d for d in cost['days']}
    self.assertEqual(by_day['2026-09-25']['ai_net_usd'], 2.0)
    self.assertEqual(by_day['2026-09-25']['all_net_usd'], 7.0)
    self.assertEqual(cost['last_billed_day'], '2026-09-26')

  def test_join_with_live_billing(self):
    out = {r['day']: r for r in billing_export.join_daily_usage_with_cost(self._USAGE, self._cost())}
    self.assertIsNone(out['2026-09-20']['ai_net_usd'])      # before first exported day: unknown
    self.assertEqual(out['2026-09-25']['ai_net_usd'], 2.0)
    self.assertEqual(out['2026-09-25']['usd_per_1k_turns'], 200.0)
    self.assertEqual(out['2026-09-25']['usd_per_1m_tokens'], 1.0)
    self.assertEqual(out['2026-09-26']['ai_net_usd'], 0.0)   # exported, no AI spend
    self.assertIsNone(out['2026-09-26']['usd_per_1k_turns'])  # 0 interactions
    self.assertIsNone(out['2026-09-26']['usd_per_1m_tokens'])
    self.assertIsNone(out['2026-09-27']['ai_net_usd'])      # export lag: unknown, not 0
    self.assertEqual(out['2026-09-25']['cost_scope'], 'project_ai_services')

  def test_daily_cost_sql_rejects_injection(self):
    with self.assertRaises(ValueError):
      billing_export.build_daily_cost_sql('p-roject.ds.t`; DROP TABLE x; --', 'project-maui')
    with self.assertRaises(ValueError):
      billing_export.build_daily_cost_sql('billing-proj.billing.t', "x' OR 1=1 --")
    sql = billing_export.build_daily_cost_sql('billing-proj.billing.t', 'project-maui', window_days=10**6)
    self.assertIn('INTERVAL 400 DAY', sql)

  def test_reader_not_connected_without_table(self):
    with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: ''}):
      out = billing_export.BillingExportReader('project-maui', lambda: 'tok').get_daily_ai_costs()
    self.assertEqual(out['status'], 'NOT_CONNECTED')
    self.assertEqual(out['days'], [])
    self.assertIsNone(out['last_billed_day'])


if __name__ == '__main__':
  unittest.main()
