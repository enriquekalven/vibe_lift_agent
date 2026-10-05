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
    for bad in ('proj.ds.t`; DROP TABLE x; --', 'proj-ect.ds', 'Project-Maui.ds.t', 'example-project.d s.t'):
      with self.assertRaises(ValueError, msg=bad):
        provision.validate_table_ref(bad)
    with self.assertRaises(ValueError):
      provision.validate_dataset('ds`; --')
    self.assertEqual(provision.validate_table_ref('`example-project.ds_a.tbl_1`'), 'example-project.ds_a.tbl_1')

  def test_source_sql_missing_table_is_empty_stub(self):
    sql = provision.source_sql(None, None, ('jsonPayload',), 30)
    self.assertIn('WHERE FALSE', sql)
    self.assertIn('CAST(NULL AS JSON) AS jsonPayload', sql)

  def test_source_sql_adapts_column_types(self):
    schema = {'timestamp': 'TIMESTAMP', 'jsonPayload': 'RECORD', 'labels': 'STRING', 'resource': 'JSON'}
    sql = provision.source_sql('example-project.ds.t', schema, ('jsonPayload', 'labels', 'resource', 'absent'), 999)
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

    out = provision.resolve_sources('example-project', 'US', 30, describe)
    self.assertEqual(out['inference'].status, 'MISSING')
    self.assertEqual(out['search'].status, 'WRONG_LOCATION')
    self.assertIn('WHERE FALSE', out['search'].sql)
    self.assertEqual(out['assistant'].status, 'FOUND')
    self.assertEqual(out['armor'].status, 'FOUND')
    with self.assertRaises(ValueError):
      provision.resolve_sources('example-project', 'US', 30, describe, overrides={'nope': 'a-proj.b.c'})

  def test_model_armor_enabled_and_alllogs_fallback_supported(self):
    self.assertIn('armor', provision.SOURCES)
    self.assertIn('v_model_armor_curated', [name for _, name, _ in provision.OBJECTS])
    self.assertEqual(provision.DISABLED_OBJECTS, ())
    turns_code = _sql_code(provision.load_template('mart', 'v_fct_turns'))
    self.assertIn('v_model_armor_curated', turns_code)
    self.assertIn('armor_by_token', turns_code)
    self.assertIn('armor_by_trace', turns_code)
    self.assertNotIn('CAST(NULL AS BOOL) AS is_guardrail_blocked', turns_code)

    # Verify _AllLogs schema adaptation when dedicated sanitize_operations table is absent.
    def describe_only_alllogs(ref):
      if ref.endswith('._AllLogs'):
        return {
            '__location__': 'US', 'timestamp': 'TIMESTAMP', 'insert_id': 'STRING',
            'log_id': 'STRING', 'severity': 'STRING', 'json_payload': 'JSON',
            'proto_payload': 'JSON', 'resource': 'RECORD', 'labels': 'JSON', 'trace': 'STRING',
        }
      return None

    out = provision.resolve_sources('example-project', 'US', 30, describe_only_alllogs)
    self.assertEqual(out['armor'].status, 'FOUND')
    self.assertTrue(out['armor'].table_ref.endswith('._AllLogs'))
    self.assertIn('`json_payload` AS jsonpayload_v1_sanitizeoperationlogentry', out['armor'].sql)
    self.assertIn("`log_id` = 'modelarmor.googleapis.com/sanitize_operations'", out['armor'].sql)

  def test_build_view_bodies_renders_all_views(self):
    sources = provision.resolve_sources('example-project', 'US', 30, _describe_all_found)
    bodies = provision.build_view_bodies('example-project', 'ds_ge_curated_staging', 'vibelift_mart', sources)
    self.assertEqual(len(bodies), len(provision.OBJECTS))
    for name, body in bodies.items():
      self.assertNotIn('{{', body, name)
      self.assertNotIn('}}', body, name)
      self.assertNotIn('gemini-enterprise-stage', _sql_code(body), name)  # no hard-coded project ids
    turns = bodies['vibelift_mart.v_fct_turns']
    self.assertIn('`example-project.ds_ge_curated_staging.v_user_activity_curated`', turns)
    self.assertIn('`example-project.ds_ge_curated_staging.v_model_armor_curated`', turns)
    self.assertIn('FROM `example-project.vibelift_mart.v_fct_turns`', bodies['vibelift_mart.fct_turns'])
    self.assertIn('refreshed_at', bodies['vibelift_mart.fct_turns'])
    self.assertIn('`example-project.vibelift_mart.fct_turns`', bodies['vibelift_mart.agg_daily_usage'])
    # Audit window: 1800 s lag (covers 20-minute StreamAssist timeouts) / 30 s lead in ms.
    self.assertIn('1800000', turns)
    self.assertIn('30000', turns)

  def test_fct_turns_is_a_partitioned_table_and_the_rest_are_views(self):
    ddl = provision.object_ddl('example-project', 'vibelift_mart.fct_turns', 'SELECT 1', 'd')
    self.assertTrue(ddl.startswith('CREATE OR REPLACE TABLE `example-project.vibelift_mart.fct_turns`'))
    self.assertIn('PARTITION BY event_date', ddl)
    self.assertIn('CLUSTER BY engine_key, user_email', ddl)
    for _, name, _ in provision.OBJECTS:
      if name != 'fct_turns':
        self.assertNotIn(name, provision.TABLES)
        self.assertIn('CREATE OR REPLACE VIEW', provision.object_ddl('example-project', f'd.{name}', 'SELECT 1', 'd'))

  def test_view_ddl_escapes_description(self):
    ddl = provision.view_ddl('example-project', 'vibelift_mart.fct_turns', 'SELECT 1', 'say "hi"')
    self.assertIn('description="say \\"hi\\""', ddl)
    self.assertTrue(ddl.startswith('CREATE OR REPLACE VIEW `example-project.vibelift_mart.fct_turns`'))

  def test_inline_view_refs_for_dry_run(self):
    bodies = {'m.a': 'SELECT 1 AS x', 'm.b': 'SELECT x FROM `p-proj1.m.a`'}
    sql = provision.inline_view_refs('SELECT * FROM `p-proj1.m.b`', 'p-proj1', bodies)
    self.assertNotIn('`p-proj1.m.', sql)
    self.assertIn('SELECT 1 AS x', sql)


class TemplateDefectFixTest(unittest.TestCase):
  """Guards the stage defects and 8 blind spots fixed in the mart SQL templates."""

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

  def test_armor_verdict_prefix_stripped_and_inspect_only_excluded(self):
    armor = self._tpl('curated', 'v_model_armor_curated')
    self.assertIn("r'^MODEL_ARMOR_SANITIZATION_VERDICT_'", armor)
    self.assertIn('assist_token', armor)
    self.assertIn('NMwK|M8gK', armor)
    self.assertIn('not blocked as the enforcement type is inspect only', armor)
    self.assertIn('SANITIZATION_EXECUTION_SKIPPED', armor)

  def test_activity_uses_lowercase_sink_paths_and_extracts_upload_session_id(self):
    activity = self._tpl('curated', 'v_user_activity_curated')
    self.assertIn('logmetadata.methodname', activity)
    self.assertNotIn('logMetadata.methodName', activity)
    self.assertIn("REGEXP_EXTRACT(resource_path, r'sessions/([^/]+)')", activity)
    self.assertIn("r'(?i)(upload|file)'", activity)

  def test_audit_uses_rpc_codes_dedupes_streaming_ops_and_falls_back_to_request_json(self):
    audit = self._tpl('curated', 'v_consolidated_audit_log')
    self.assertIn("WHEN status_code = 8 THEN 'RATE_LIMITED'", audit)
    self.assertIn('LAX_STRING(op.id)', audit)
    self.assertIn('QUALIFY ROW_NUMBER() OVER', audit)
    self.assertIn('request_json_str', audit)
    self.assertIn('response_json_str', audit)
    self.assertIn('pp.requestJson', audit)
    self.assertIn('pp.responseJson', audit)

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

  def test_tool_failure_catches_objects_error_message_and_wrapped_payload_errors(self):
    ops = self._tpl('curated', 'v_agentic_operations_curated')
    self.assertIn("JSON_TYPE(part.response.error) NOT IN ('null')", ops)
    self.assertIn('part.response.error_message', ops)
    self.assertIn('part.response._dolphin_error_handling', ops)
    self.assertIn('cj.result.Error', ops)
    self.assertIn('cj.error.message', ops)
    self.assertNotIn('OR part.response.error IS NOT NULL', ops)
    # Multi-MCP aggregation across all tool parts (both lowercase sink and camelCase export_errors)
    self.assertIn('LAX_STRING(part.response.structuredcontent.mcpservername)', ops)
    self.assertIn('LAX_STRING(part.response.structuredContent.mcpServerName)', ops)
    self.assertIn('LAX_STRING(part.response.structuredcontent.authkind)', ops)
    self.assertIn('LAX_STRING(part.response.structuredContent.authKind)', ops)
    self.assertIn("IF(LAX_STRING(part.name) = 'invalid_tool_call_notifier'", ops)

  def test_reasoning_output_tokens_and_dotted_export_error_paths_supported(self):
    ops = self._tpl('curated', 'v_agentic_operations_curated')
    self.assertIn('gen_ai_usage_reasoning_output_tokens', ops)
    self.assertIn('$."gen_ai.usage.reasoning_output_tokens"', ops)
    self.assertIn('$."gen_ai.usage.input_tokens"', ops)
    self.assertIn('$."gen_ai.usage.output_tokens"', ops)
    self.assertIn('$."gen_ai.usage.cache_read.input_tokens"', ops)
    self.assertIn('$."gen_ai.input.messages"', ops)
    self.assertIn('$."gen_ai.output.messages"', ops)

  def test_export_errors_recovery_unioned_when_present(self):
    def describe_with_export_errors(ref):
      if ref.endswith('.export_errors'):
        return {
            '__location__': 'US', 'timestamp': 'TIMESTAMP', 'trace': 'STRING',
            'insertId': 'STRING', 'severity': 'STRING', 'logName': 'STRING', 'logEntry': 'STRING',
        }
      return _describe_all_found(ref)

    out = provision.resolve_sources('example-project', 'US', 30, describe_with_export_errors)
    self.assertEqual(out['inference'].status, 'FOUND')
    self.assertIn('export_errors recovery', out['inference'].detail)
    self.assertIn('UNION ALL', out['inference'].sql)
    self.assertIn('`example-project.ds_vertex_agents_raw.export_errors`', out['inference'].sql)
    self.assertIn('UNNEST([SAFE.PARSE_JSON(`logEntry`)]) AS _le', out['inference'].sql)
    self.assertIn('_le.jsonPayload AS jsonPayload', out['inference'].sql)
    self.assertIn(r"REGEXP_CONTAINS(`logName`, r'gen_ai\.client\.inference\.operation\.details$')", out['inference'].sql)

  def test_armor_execution_state_falls_back_to_invocation_result(self):
    armor = self._tpl('curated', 'v_model_armor_curated')
    self.assertIn('ma.sanitizationresult.invocationresult', armor)
    self.assertIn('res.labels.location', self._tpl('curated', 'v_user_activity_curated'))
    self.assertIn('jp.response.modelinfo.requestedmodel', self._tpl('curated', 'v_user_activity_curated'))

  def test_sessions_never_split_by_engine_key_and_turns_compute_latency(self):
    sessions = self._tpl('mart', 'fct_sessions')
    self.assertIn('GROUP BY session_id', sessions)
    self.assertNotIn('GROUP BY engine_key, session_id', sessions)
    turns = self._tpl('mart', 'v_fct_turns')
    self.assertIn('OVER (PARTITION BY session_id)', turns)
    self.assertIn('MILLISECOND\n    ), 0) AS latency_ms', turns)

  def test_status_message_not_truncated_at_300_chars(self):
    for name in ('v_user_activity_curated', 'v_consolidated_audit_log'):
      body = self._tpl('curated', name)
      self.assertNotIn('1, 300', body, name)
      self.assertIn("status.message), '') AS status_message", body, name)

  def test_audit_permission_denial_catches_null_granted_and_rpc_code_7(self):
    audit = self._tpl('curated', 'v_consolidated_audit_log')
    self.assertIn('NOT COALESCE(LAX_BOOL(auth.granted), FALSE)', audit)
    self.assertIn('COALESCE(status_code, 0) != 0 OR has_permission_denial AS is_error', audit)

  def test_all_curated_views_deduplicate_by_insert_id(self):
    for name in (
        'v_user_activity_curated',
        'v_agentic_operations_curated',
        'v_model_armor_curated',
        'v_consolidated_audit_log',
    ):
      body = self._tpl('curated', name)
      self.assertIn('QUALIFY ROW_NUMBER() OVER', body, name)
      self.assertIn('insertId', body, name)
    # v_user_activity_curated unions assistant + search and must deduplicate both CTEs.
    self.assertEqual(self._tpl('curated', 'v_user_activity_curated').count('QUALIFY ROW_NUMBER() OVER'), 2)

  def test_audit_classifies_async_assist_and_session_file_methods_as_interactive(self):
    audit = self._tpl('curated', 'v_consolidated_audit_log')
    for method in ('AsyncAssist', 'ReadAsyncAssist', 'DownloadSessionFile', 'ListSessionFileMetadata'):
      self.assertEqual(audit.count(f"'{method}'"), 2, method)

  def test_looker_l1_l2_support_view_reads_materialized_turns_and_exposes_triage_fields(self):
    looker = self._tpl('mart', 'v_looker_l1_l2_support')
    turns = self._tpl('mart', 'v_fct_turns')
    self.assertIn('FROM `{{mart}}.fct_turns`', looker)
    self.assertNotIn('v_fct_turns', looker)
    for col in (
        'AS ticket_id',
        'AS needs_support_attention',
        'AS support_tier',
        'AS issue_category',
        'AS l1_runbook_action',
        'AS issue_summary',
        'AS error_signature',
        'AS cloud_logging_url',
        'AS cloud_trace_url',
        'AS session_step_number',
        'AS session_total_turns',
        'AS session_has_issue',
        'No Answer / Skipped',
        'prompt_preview',
        'response_preview',
        'uploaded_file_names',
        'queried_data_stores',
        'citation_sources',
        'tool_error_codes',
        'tool_error_messages',
        'tool_call_summary',
        'AS tool_args_summary',
        'tool_output_preview',
        'armor_findings',
        'armor_verdict_reasons',
        'armor_sdp_info_types',
    ):
      self.assertIn(col, looker)
    for col in (
        'prompt_preview',
        'response_preview',
        'uploaded_file_names',
        'queried_data_stores',
        'citation_sources',
        'tool_error_codes',
        'tool_error_messages',
        'tool_call_summary',
        'tool_output_preview',
        'armor_findings',
        'armor_verdict_reasons',
        'armor_sdp_info_types',
    ):
      self.assertIn(col, turns)


class GeMartReaderTest(unittest.TestCase):

  def test_refs_default_and_env_override(self):
    with mock.patch.dict(os.environ, {ge_mart.MART_DATASET_ENV: '', ge_mart.CURATED_DATASET_ENV: ''}):
      self.assertEqual(ge_mart.mart_ref('example-project'), 'example-project.vibelift_mart')
      self.assertEqual(ge_mart.curated_ref('example-project'), 'example-project.ds_ge_curated_staging')
    with mock.patch.dict(os.environ, {ge_mart.MART_DATASET_ENV: 'bad`; --'}):
      with self.assertRaises(ValueError):
        ge_mart.mart_ref('example-project')
    with self.assertRaises(ValueError):
      ge_mart.build_support_turns_sql("x' OR 1=1")

  def test_sql_limits_are_clamped(self):
    sql = ge_mart.build_recent_turns_sql('example-project', hours=10**9, limit=10**9, tokens_only=True)
    self.assertIn('INTERVAL 8760 HOUR', sql)
    self.assertIn('LIMIT 500', sql)
    self.assertIn('total_tokens IS NOT NULL', sql)
    for hrs in (720, 2160, 4320, 8760):
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_support_turns_sql('example-project', hours=hrs))
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_user_engine_rollup_sql('example-project', hours=hrs))
      self.assertIn(f'INTERVAL {hrs} HOUR', ge_mart.build_audit_principals_sql('example-project', hours=hrs))

  def test_sql_to_python_reader_column_contract(self):
    # Every turn query and row mapper must select and preserve latency_ms, tool_failure_count,
    # and mcp_server_name rather than hardcoding None or dropping them.
    recent_sql = ge_mart.build_recent_turns_sql('example-project')
    session_turns_sql = ge_mart.build_session_turns_sql('example-project')
    sessionless_sql = ge_mart.build_sessionless_token_turns_sql('example-project')
    for sql in (recent_sql, session_turns_sql, sessionless_sql):
      for col in ('latency_ms', 'tool_call_count', 'tool_failure_count', 'tool_names', 'mcp_server_name'):
        self.assertIn(col, sql)
    log = ge_mart.usage_log_from_row({'turn_id': 't1', 'latency_ms': '1420', 'total_tokens': '500'}, 'example-project')
    self.assertEqual(log['latency_ms'], 1420)
    st = ge_mart.session_turn_from_row({
        'turn_id': 't1', 'latency_ms': '1420', 'tool_call_count': '3',
        'tool_failure_count': '1', 'mcp_server_name': 'github,jira',
    })
    self.assertEqual(st['latency_ms'], 1420)
    self.assertEqual(st['tool_calls'], 3)
    self.assertEqual(st['tool_failures'], 1)
    self.assertEqual(st['mcp_server_name'], 'github,jira')

  def test_usage_log_keeps_unknowns_as_none(self):
    row = {'turn_id': 'ACTIVITY:abc', 'ts': '2026-09-29 10:00:00', 'turn_status': 'SUCCESS',
           'user_email': 'a@example.com', 'total_tokens': None, 'input_tokens': '', 'model_name': None}
    log = ge_mart.usage_log_from_row(row, 'example-project')
    self.assertEqual(log['prompts'], [])
    self.assertIsNone(log['latency_ms'])
    self.assertIsNone(log['csat_rating'])
    self.assertIsNone(log['total_tokens'])
    self.assertIsNone(log['input_tokens'])
    self.assertIsNone(log['model_name'])
    self.assertTrue(log['outputs'][0]['gcs_uri'].startswith('bq://example-project.'))

  def test_usage_log_parses_rest_strings(self):
    log = ge_mart.usage_log_from_row({'turn_id': 't', 'total_tokens': '38520', 'reasoning_tokens': '0'},
                                     'example-project')
    self.assertEqual(log['total_tokens'], 38520)
    self.assertEqual(log['thinking_tokens'], 0)

  def test_support_event_guardrail_block_is_actionable(self):
    row = {'turn_id': 'ACTIVITY:xyz', 'turn_status': 'SUCCESS', 'is_actionable_issue': 'true',
           'is_guardrail_blocked': 'true', 'guardrail_categories': 'SENSITIVE_DATA', 'status_code': '0'}
    ev = ge_mart.support_event_from_row(row, 'example-project')
    self.assertEqual(ev['category'], 'GUARDRAIL_BLOCK')
    self.assertEqual(ev['tier'], 'L2 Actionable')
    self.assertEqual(ev['resolution_status'], 'NEEDS_ATTENTION')
    self.assertIn('SENSITIVE_DATA', ev['issue_summary'])
    self.assertEqual(ev['status_code'], 0)
    self.assertIsNone(ev['model_name'])

  def test_support_event_success_not_actionable(self):
    ev = ge_mart.support_event_from_row({'turn_id': 'a', 'turn_status': 'SUCCESS',
                                         'is_actionable_issue': 'false'}, 'example-project')
    self.assertFalse(ev['is_actionable_issue'])
    self.assertEqual(ev['resolution_status'], 'NO_ACTION')
    self.assertEqual(ev['resolution_action'], 'no audit record matched')

  def test_mart_refreshed_at_unknown_without_rows(self):
    self.assertIsNone(ge_mart.mart_refreshed_at([]))
    self.assertIsNone(ge_mart.mart_refreshed_at([{'day': '2026-09-29', 'refreshed_at': None}]))
    rows = [{'refreshed_at': '2026-09-29 10:00:00+00'}, {'refreshed_at': '2026-09-29 11:00:00+00'}]
    self.assertEqual(ge_mart.mart_refreshed_at(rows), '2026-09-29 11:00:00+00')
    self.assertIn('MAX(refreshed_at)', ge_mart.build_daily_totals_sql('example-project'))

  def test_daily_usage_tokens_none_counts_zero(self):
    out = ge_mart.daily_usage_from_row({'day': '2026-09-29', 'interactions': '5', 'failed_turns': None,
                                        'total_tokens': None, 'input_tokens': '12'})
    self.assertEqual(out['interactions'], 5)
    self.assertEqual(out['failed_turns'], 0)
    self.assertIsNone(out['total_tokens'])
    self.assertEqual(out['input_tokens'], 12)

  def test_sessions_and_refresh_ddl(self):
    ddl = ge_mart.build_refresh_fct_turns_ddl('example-project')
    self.assertIn('CREATE OR REPLACE TABLE `example-project.vibelift_mart.fct_turns`', ddl)
    self.assertIn('CURRENT_TIMESTAMP() AS refreshed_at', ddl)
    self.assertIn('FROM `example-project.vibelift_mart.v_fct_turns`', ddl)
    sql = ge_mart.build_recent_sessions_sql('example-project', days=10**9, limit=10**9)
    self.assertIn('FROM `example-project.vibelift_mart.fct_sessions`', sql)
    self.assertIn('INTERVAL 400 DAY', sql)
    self.assertIn('LIMIT 500', sql)
    sess = ge_mart.session_from_row(
        {'session_id': 's1', 'engine_key': 'global/us-1', 'user_email': 'u@example.com',
         'turns': '4', 'chat_turns': '3', 'failed_turns': '1', 'duration_seconds': '42',
         'total_tokens': None, 'input_tokens': '100', 'agent_name': 'Helper'},
        'example-project',
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
    service = gcp_telemetry.GoogleCloudTelemetryService('example-project', region='us-central1')
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
    service = gcp_telemetry.GoogleCloudTelemetryService('example-project', region='us-central1')
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
    service = gcp_telemetry.GoogleCloudTelemetryService('example-project', region='us-central1')
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
      billing_export.build_daily_cost_sql('p-roject.ds.t`; DROP TABLE x; --', 'example-project')
    with self.assertRaises(ValueError):
      billing_export.build_daily_cost_sql('billing-proj.billing.t', "x' OR 1=1 --")
    sql = billing_export.build_daily_cost_sql('billing-proj.billing.t', 'example-project', window_days=10**6)
    self.assertIn('INTERVAL 400 DAY', sql)

  def test_reader_not_connected_without_table(self):
    with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: ''}):
      out = billing_export.BillingExportReader('example-project', lambda: 'tok').get_daily_ai_costs()
    self.assertEqual(out['status'], 'NOT_CONNECTED')
    self.assertEqual(out['days'], [])
    self.assertIsNone(out['last_billed_day'])


_VERIFY_SPEC = importlib.util.spec_from_file_location(
    'verify_ge_mart_invariants', _ROOT / 'deploy' / 'bigquery' / 'verify_ge_mart_invariants.py')
verify_invariants = importlib.util.module_from_spec(_VERIFY_SPEC)
sys.modules['verify_ge_mart_invariants'] = verify_invariants
_VERIFY_SPEC.loader.exec_module(verify_invariants)  # type: ignore[union-attr]


class InvariantVerifierTest(unittest.TestCase):

  def test_build_invariant_sql_covers_all_conservation_laws(self):
    sql = verify_invariants.build_invariant_sql('example-project', 'ds_ge_curated_staging', 'vibelift_mart')
    for inv in (
        'fct_turns_unique_turn_id',
        'looker_support_view_no_fanout',
        'fct_sessions_unique_session_id',
        'session_turn_conservation',
        'session_token_conservation',
        'daily_agg_turn_conservation',
        'daily_agg_token_conservation',
        'inference_token_conservation',
        'tool_failure_bounds',
        'file_upload_session_coverage',
        'armor_inspect_only_exclusion',
        'audit_session_extraction_coverage',
        'materialized_fct_turns_sync',
    ):
      self.assertIn(inv, sql)
    with self.assertRaises(ValueError):
      verify_invariants.build_invariant_sql('bad; DROP', 'ds_ge_curated_staging', 'vibelift_mart')

  def test_verify_mart_invariants_parses_results_and_exit_code(self):
    fake_rows = [
        {'name': 'fct_sessions_unique_session_id', 'violations': '0', 'detail': 'rows=248 distinct_sessions=248'},
        {'name': 'session_token_conservation', 'violations': '0', 'detail': 'ok'},
    ]
    results = verify_invariants.verify_mart_invariants(
        'example-project', query_runner=lambda _p, _l, _s: fake_rows)
    self.assertEqual(len(results), 2)
    self.assertTrue(all(r.passed for r in results))

  def test_no_unnest_array_agg_in_sql_templates(self):
    for sql_path in (_ROOT / 'deploy' / 'bigquery' / 'ge_mart').rglob('*.sql'):
      text = sql_path.read_text(encoding='utf-8').upper()
      for forbidden in ('UNNEST(ARRAY_AGG', 'UNNEST(SPLIT(STRING_AGG'):
        self.assertNotIn(
            forbidden,
            text,
            f'{sql_path.name} uses {forbidden}(...), which BigQuery rejects inside GROUP BY scalar subqueries; aggregate in a prior CTE first.',
        )


if __name__ == '__main__':
  unittest.main()

