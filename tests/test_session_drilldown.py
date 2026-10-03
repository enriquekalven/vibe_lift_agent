"""Tests for the per-user session token rollup and per-session turn token drilldown."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import unittest
import urllib.request
from unittest import mock

import fastapi
from fastapi.testclient import TestClient

from tests import test_fleet as ge_fleet_test
from vibelift import finops, gcp_telemetry, ge_mart, server
from vibelift.ui import template as ui_template

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()

_SESSION_ROWS = [
    {'engine_key': 'global/eng-1', 'session_id': 's-1', 'session_end': '2026-09-29T12:00:00Z',
     'user_email': 'user-a@example.com', 'agent_name': 'sre_agent', 'turns': '3', 'chat_turns': '3',
     'input_tokens': '12000', 'output_tokens': '1500', 'cached_input_tokens': '9000',
     'reasoning_tokens': '300', 'total_tokens': '13500'},
    {'engine_key': 'global/eng-1', 'session_id': 's-2', 'session_end': '2026-09-29T13:00:00Z',
     'user_email': 'user-a@example.com', 'agent_name': 'sre_agent', 'turns': '1',
     'input_tokens': '1000', 'output_tokens': '100', 'cached_input_tokens': None,
     'reasoning_tokens': None, 'total_tokens': '1100'},
    {'engine_key': 'global/eng-2', 'session_id': 's-9', 'session_end': '2026-09-28T09:00:00Z',
     'user_email': 'ops@example.com', 'agent_name': 'search', 'turns': '2',
     'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': None,
     'reasoning_tokens': None, 'total_tokens': None},
]

_TURN_ROWS = [
    {'engine_key': 'global/eng-1', 'session_id': 's-1', 'turn_id': 't-1', 'turn_kind': 'CHAT',
     'turn_status': 'SUCCESS', 'ts': '2026-09-29T11:50:00Z', 'user_email': 'user-a@example.com',
     'model_name': 'gemini-2.5-flash', 'input_tokens': '4000', 'output_tokens': '500',
     'cached_input_tokens': '3000', 'reasoning_tokens': '100', 'total_tokens': '4500',
     'llm_calls': '1', 'tool_call_count': '1', 'tool_names': 'fetch_logs'},
    {'engine_key': 'global/eng-1', 'session_id': 's-1', 'turn_id': 't-2', 'turn_kind': 'CHAT',
     'turn_status': 'SUCCESS', 'ts': '2026-09-29T11:55:00Z', 'input_tokens': '8000',
     'output_tokens': '1000', 'cached_input_tokens': '6000', 'reasoning_tokens': '200',
     'total_tokens': '9000', 'llm_calls': '2', 'tool_call_count': '1', 'tool_names': None},
    {'engine_key': 'global/eng-1', 'session_id': 's-1', 'turn_id': 't-3', 'turn_kind': 'SEARCH',
     'turn_status': 'SUCCESS', 'ts': '2026-09-29T12:00:00Z', 'input_tokens': None,
     'output_tokens': None, 'total_tokens': None},
    {'engine_key': 'global/eng-1', 'session_id': None, 'turn_id': 't-x'},  # no session: dropped
]


class SessionTurnsSqlTest(unittest.TestCase):

  def test_builder_targets_mart_and_clamps(self):
    sql = ge_mart.build_session_turns_sql(
        'example-project', days=10**9, session_limit=10**9, turns_per_session=10**9, limit=10**9)
    self.assertIn('FROM `example-project.vibelift_mart.fct_sessions`', sql)
    self.assertIn('FROM `example-project.vibelift_mart.fct_turns` AS t', sql)
    self.assertIn('INTERVAL 400 DAY', sql)
    self.assertIn('LIMIT 500', sql)       # session limit clamp
    self.assertIn('<= 500', sql)          # turns-per-session clamp
    self.assertIn('LIMIT 5000', sql)      # overall row clamp
    self.assertIn('ORDER BY session_end DESC', sql)
    # Token counts only: the mart's prompt/response columns are never selected.
    for forbidden in ('prompt', 'response_text', 'SELECT *'):
      self.assertNotIn(forbidden, sql)

  def test_builder_uses_same_session_selection_as_recent_sessions(self):
    sess_sql = ge_mart.build_recent_sessions_sql('example-project', days=30, limit=50)
    turn_sql = ge_mart.build_session_turns_sql('example-project', days=30, session_limit=50)
    for clause in ('INTERVAL 30 DAY', 'ORDER BY session_end DESC', 'LIMIT 50'):
      self.assertIn(clause, sess_sql)
      self.assertIn(clause, turn_sql)

  def test_builder_rejects_bad_project(self):
    with self.assertRaises(ValueError):
      ge_mart.build_session_turns_sql('bad project; DROP')


class SessionTurnMapperTest(unittest.TestCase):

  def test_mapper_keeps_unknown_as_none(self):
    turn = ge_mart.session_turn_from_row(_TURN_ROWS[2])
    self.assertIsNone(turn['input_tokens'])
    self.assertIsNone(turn['output_tokens'])
    self.assertIsNone(turn['tool_calls'])
    first = ge_mart.session_turn_from_row(_TURN_ROWS[0])
    self.assertEqual(first['input_tokens'], 4000)
    self.assertEqual(first['output_tokens'], 500)
    self.assertEqual(first['cached_input_tokens'], 3000)
    self.assertEqual(first['tool_calls'], 1)
    self.assertEqual(first['tool_names'], 'fetch_logs')
    self.assertNotIn('prompts', first)

  def test_grouping_and_session_key(self):
    grouped = ge_mart.group_session_turns(_TURN_ROWS)
    self.assertEqual(list(grouped), ['global/eng-1|s-1'])
    self.assertEqual([t['turn_id'] for t in grouped['global/eng-1|s-1']], ['t-1', 't-2', 't-3'])
    sess = ge_mart.session_from_row(_SESSION_ROWS[0], 'example-project')
    self.assertEqual(sess['session_key'], 'global/eng-1|s-1')
    self.assertEqual(ge_mart.session_key(None, 's-1'), '|s-1')


class SessionTokenDrilldownTest(unittest.TestCase):

  def _drilldown(self):
    sessions = [ge_mart.session_from_row(r, 'example-project') for r in _SESSION_ROWS]
    return finops.build_session_token_drilldown(sessions, ge_mart.group_session_turns(_TURN_ROWS))

  def test_user_rollup_sums_known_values_only(self):
    dd = self._drilldown()
    self.assertEqual(dd['session_count'], 3)
    self.assertEqual(dd['user_count'], 2)
    users = {u['user_email']: u for u in dd['users']}
    user_a = users['user-a@example.com']
    self.assertEqual(user_a['sessions'], 2)
    self.assertEqual(user_a['turns'], 4)
    self.assertEqual(user_a['input_tokens'], 13000)
    self.assertEqual(user_a['output_tokens'], 1600)
    self.assertEqual(user_a['cached_input_tokens'], 9000)   # s-2 unknown is skipped, not zero
    self.assertEqual(user_a['total_tokens'], 14600)
    self.assertEqual(user_a['avg_total_tokens_per_session'], 7300)
    self.assertEqual(user_a['last_activity'], '2026-09-29T13:00:00Z')
    ops = users['ops@example.com']
    self.assertIsNone(ops['total_tokens'])                  # nothing measured stays None
    self.assertIsNone(ops['avg_total_tokens_per_session'])
    self.assertEqual(dd['users'][0]['user_email'], 'user-a@example.com')  # sorted by tokens
    self.assertEqual(dd['totals']['total_tokens'], 14600)

  def test_session_turn_detail_and_completeness(self):
    dd = self._drilldown()
    s1 = dd['sessions']['global/eng-1|s-1']
    self.assertEqual(s1['turn_detail_count'], 3)
    self.assertTrue(s1['turn_detail_complete'])
    self.assertEqual(sum(t['input_tokens'] or 0 for t in s1['turn_detail']), s1['input_tokens'])
    self.assertEqual(sum(t['output_tokens'] or 0 for t in s1['turn_detail']), s1['output_tokens'])
    s2 = dd['sessions']['global/eng-1|s-2']
    self.assertEqual(s2['turn_detail'], [])
    self.assertFalse(s2['turn_detail_complete'])

  def test_duplicate_session_rows_are_not_double_counted(self):
    sessions = [ge_mart.session_from_row(r, 'example-project') for r in _SESSION_ROWS]
    turns = ge_mart.group_session_turns(_TURN_ROWS)
    single = finops.build_session_token_drilldown(sessions, turns)
    doubled = finops.build_session_token_drilldown(sessions + [dict(s) for s in sessions], turns)
    self.assertEqual(doubled['session_count'], single['session_count'])
    self.assertEqual(doubled['totals'], single['totals'])
    self.assertEqual(doubled['users'], single['users'])
    # Totals always equal the sum of the per-user rollups.
    self.assertEqual(doubled['totals']['total_tokens'], sum(u['total_tokens'] or 0 for u in doubled['users']))

  def test_empty_and_bad_inputs(self):
    dd = finops.build_session_token_drilldown(None, None)
    self.assertEqual(dd['session_count'], 0)
    self.assertEqual(dd['users'], [])
    self.assertIsNone(dd['totals']['total_tokens'])
    dd = finops.build_session_token_drilldown(['junk', {'session_id': 'x', 'turns': 0}], {'|x': ['junk']})
    self.assertEqual(dd['sessions']['|x']['turn_detail'], [])
    self.assertEqual(dd['users'][0]['user_email'], 'unknown')


class TelemetryWiringTest(unittest.TestCase):

  def test_insights_include_grouped_session_turns(self):
    svc = gcp_telemetry.GoogleCloudTelemetryService('example-project', region='us-central1')

    def _router(sql, *_args, **_kwargs):
      if 'recent_sessions' in sql:
        return list(_TURN_ROWS)
      if 'fct_sessions' in sql and 'fct_turns' not in sql:
        return list(_SESSION_ROWS)
      return []

    with mock.patch.object(svc, '_get_access_token', return_value='tok'), \
         mock.patch.object(svc, '_query_bigquery_rest', side_effect=_router):
      insights = svc.fetch_live_bigquery_project_insights(force_refresh=True)
    self.assertIn('ge_session_turns', insights)
    self.assertEqual(len(insights['ge_session_turns']['global/eng-1|s-1']), 3)
    self.assertEqual(len(insights['ge_sessions']), 3)
    self.assertEqual(insights['ge_sessions'][0]['session_key'], 'global/eng-1|s-1')


def _uc_with_sessions() -> dict[str, object]:
  sessions = [ge_mart.session_from_row(r, 'example-project') for r in _SESSION_ROWS]
  return {'ge_sessions': sessions, 'ge_session_turns': ge_mart.group_session_turns(_TURN_ROWS)}


class UserCentricFinopsEndpointTest(unittest.TestCase):

  def test_live_state_injects_session_turns(self):
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    uc = _uc_with_sessions()
    bq = {'ge_sessions': uc['ge_sessions'], 'ge_session_turns': uc['ge_session_turns'],
          'ge_mart_dataset': 'example-project.vibelift_mart', 'power_users_ldap': []}
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_live_bigquery_project_insights', return_value=bq), \
         mock.patch.object(ctrl.gcp_telemetry, 'list_cloud_run_agent_services', return_value=[]), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_gemini_enterprise_support_telemetry', return_value=[]), \
         mock.patch.object(ctrl.billing_export, 'get', return_value={'status': 'NOT_CONFIGURED'}), \
         mock.patch.object(ctrl.billing_export, 'get_daily_ai_costs', return_value={'status': 'NOT_CONFIGURED'}):
      payload = ctrl.get_user_centric_finops_payload(window_hours=168)
    self.assertEqual(payload['mode'], 'LIVE_GCP')
    self.assertEqual(len(payload['user_centric']['ge_session_turns']['global/eng-1|s-1']), 3)
    dd = payload['session_drilldown']
    self.assertEqual(dd['session_count'], 3)
    self.assertEqual(dd['sessions']['global/eng-1|s-1']['turn_detail'][0]['input_tokens'], 4000)

  def test_fastapi_and_stdlib_routes(self):
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    state = {'user_centric': _uc_with_sessions()}
    app = fastapi.FastAPI()
    server.register_api_routes(app, ctrl)
    with mock.patch.object(ctrl, 'get_state_payload', return_value=state) as m_state:
      body = TestClient(app).get('/api/user_centric_finops', params={'window_hours': '24'}).json()
      m_state.assert_called_once_with(include_fleet=False, window_hours=24)
    self.assertEqual(body['session_drilldown']['user_count'], 2)
    self.assertIn('global/eng-1|s-1', body['session_drilldown']['sessions'])
    # Stdlib HTTP server (standalone mode) serves the same route.
    httpd = server.create_http_server('127.0.0.1', 0, ctrl)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
      with mock.patch.object(ctrl, 'get_state_payload', return_value=state):
        url = f'http://127.0.0.1:{httpd.server_address[1]}/api/user_centric_finops?window_hours=abc'
        with urllib.request.urlopen(url, timeout=10) as resp:
          std_body = json.loads(resp.read().decode('utf-8'))
    finally:
      httpd.shutdown()
      httpd.server_close()
    self.assertEqual(std_body['session_drilldown']['session_count'], 3)
    # Demo mode (no live data): endpoint still answers with an empty drilldown.
    demo = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET).get_user_centric_finops_payload()
    self.assertEqual(demo['mode'], 'DEMO')
    self.assertIn('session_drilldown', demo)

  def test_ui_has_drilldown_controls_without_inner_html(self):
    html = ui_template.render_dashboard_html()
    for marker in ('id="geSessionsUserFilter"', 'id="geSessionsUserFilterClear"', 'function renderGeSessionsTable',
                   'function renderGeSessionTurns', 'function setGeSessionUserFilter', 'Input Tok', 'Output Tok',
                   'Cached Tok', 'ge_session_turns'):
      self.assertIn(marker, html)
    start = html.index('function renderGeSessionTurns')
    end = html.index('function renderGeMartDaily')
    self.assertNotIn('innerHTML', html[start:end])


def _is_sessions_query(sql: str) -> bool:
  """True for build_recent_sessions_sql (build_session_turns_sql reads fct_sessions in a recent_sessions CTE)."""
  return 'fct_sessions' in sql and 'recent_sessions' not in sql


class ColdStartInsightsTest(unittest.TestCase):
  """A new Cloud Run instance must not serve the MCP app an empty Users tab, or make its tool calls wait."""

  def _service(self) -> gcp_telemetry.GoogleCloudTelemetryService:
    service = gcp_telemetry.GoogleCloudTelemetryService('example-project', region='us-central1')
    service._cli_token = 'token'  # skip ADC and the gcloud CLI
    service._cli_token_ts = time.monotonic()
    return service

  @staticmethod
  def _fake_query(calls: list[str], release: threading.Event | None = None):
    """Fake _query_bigquery_rest: only the sessions query returns rows, held until `release` is set."""
    def fake(sql: str, timeout_s: float = 7.0) -> list[dict[str, str | None]]:
      del timeout_s
      if not _is_sessions_query(sql):
        return []
      calls.append(sql)
      if release is not None:
        release.wait(5.0)
      return [dict(r) for r in _SESSION_ROWS]
    return fake

  def test_prime_waits_for_the_first_fetch(self):
    service = self._service()
    calls: list[str] = []
    with mock.patch.object(service, '_query_bigquery_rest', side_effect=self._fake_query(calls)):
      insights = service.prime_bigquery_insights(timeout_s=5.0)
    self.assertIsNotNone(insights)
    self.assertEqual([s['session_id'] for s in insights['ge_sessions']], ['s-1', 's-2', 's-9'])
    self.assertEqual(len(calls), 1)

  def test_prime_skips_offline_projects(self):
    service = gcp_telemetry.GoogleCloudTelemetryService('test-project', region='us-central1')
    with mock.patch.object(service, '_query_bigquery_rest') as query:
      self.assertIsNone(service.prime_bigquery_insights(timeout_s=5.0))
    query.assert_not_called()

  def test_cold_non_blocking_calls_answer_fast_and_share_one_refresh(self):
    service = self._service()
    calls: list[str] = []
    release = threading.Event()
    with mock.patch.object(service, '_query_bigquery_rest', side_effect=self._fake_query(calls, release)):
      started = time.monotonic()
      with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: service.fetch_live_bigquery_project_insights(non_blocking=True), range(4)))
      elapsed = time.monotonic() - started
      refresh = service._bq_refresh_event
      release.set()
      self.assertIsNotNone(refresh)
      self.assertTrue(refresh.wait(5.0))
      warm = service.fetch_live_bigquery_project_insights(non_blocking=True)
    self.assertEqual(results, [None] * 4)
    self.assertLess(elapsed, 1.0)  # Gemini Enterprise fails MCP app tool calls after about 2 s
    self.assertEqual(len(calls), 1)  # one shared refresh, not one per call
    self.assertEqual(len(warm['ge_sessions']), 3)

  def test_stale_cache_is_served_while_the_refresh_runs(self):
    service = self._service()
    stale = {'ge_sessions': [{'session_id': 'old'}]}
    service._cached_bq_insights = stale
    service._cached_bq_insights_ts = time.monotonic() - 3600.0
    calls: list[str] = []
    release = threading.Event()
    with mock.patch.object(service, '_query_bigquery_rest', side_effect=self._fake_query(calls, release)):
      started = time.monotonic()
      out = service.fetch_live_bigquery_project_insights(non_blocking=True)
      elapsed = time.monotonic() - started
      refresh = service._bq_refresh_event
      release.set()
      self.assertIsNotNone(refresh)
      self.assertTrue(refresh.wait(5.0))
    self.assertIs(out, stale)
    self.assertLess(elapsed, 1.0)
    self.assertEqual(len(service._cached_bq_insights['ge_sessions']), 3)
    self.assertIsNone(service._bq_refresh_event)

  def test_empty_fetch_logs_a_warning(self):
    service = self._service()
    with mock.patch.object(service, '_query_bigquery_rest', return_value=[]), \
         self.assertLogs(gcp_telemetry.logger, level='WARNING') as logs:
      self.assertIsNone(service.fetch_live_bigquery_project_insights(force_refresh=True))
    self.assertIn('every query returned no rows', '\n'.join(logs.output))

  def test_controller_prime_reports_the_outcome(self):
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    insights = {'ge_sessions': _uc_with_sessions()['ge_sessions'], 'power_users_ldap': [{'ldap': 'user-a'}]}
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.object(ctrl.gcp_telemetry, 'prime_bigquery_insights', return_value=insights) as prime, \
         self.assertLogs(server.logger, level='INFO') as logs:
      self.assertTrue(ctrl.prime_live_caches(timeout_s=3.0))
    prime.assert_called_once_with(3.0)
    self.assertIn('3 sessions, 1 users', '\n'.join(logs.output))
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.object(ctrl.gcp_telemetry, 'prime_bigquery_insights', return_value=None), \
         self.assertLogs(server.logger, level='WARNING') as logs:
      self.assertFalse(ctrl.prime_live_caches(timeout_s=3.0))
    self.assertIn('not ready', '\n'.join(logs.output))
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=False), \
         mock.patch.object(ctrl.gcp_telemetry, 'prime_bigquery_insights') as prime:
      self.assertFalse(ctrl.prime_live_caches())
    prime.assert_not_called()


if __name__ == '__main__':
  unittest.main()
