"""Tests for the plain-English search (NL2SQL) path and for token pricing without fallback prices."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from tests import test_fleet as ge_fleet_test
from vibelift import fleet, gcp_telemetry, optimizer, server

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()
_CARDS = _FAKE_FLEET.rate_cards()
_CAP = optimizer.NL2SQL_MAX_BYTES_BILLED

# One question per template branch in optimizer._match_nl2sql_template, plus the default branch.
_TEMPLATE_QUESTIONS = (
    'Which agents have runaway thinking loops?',
    'Show daily usage from the session mart',
    'Who are the power users by department?',
    'Show the A2A handoff waterfall by layer',
    'Compare cost per 1k turns and prompt cache savings across agents',
)


class _FakeResponse(io.BytesIO):
  """Minimal context-manager response for a patched urllib.request.urlopen."""

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False


class Nl2SqlVerifierTest(unittest.TestCase):

  def test_every_template_passes_the_read_only_check(self) -> None:
    opt = optimizer.VibeLiftAlphaEvolveOptimizer()
    for q in _TEMPLATE_QUESTIONS:
      with self.subTest(q=q):
        res = opt.execute_nl2sql_telemetry_query(q, project_id='proj-a')
        audit = res['sql_safety_audit']
        self.assertTrue(audit['read_only_enforced'], audit['verification_errors'])
        self.assertEqual(audit['datasets_queried'], ['proj-a.vibelift_mart'])
        self.assertEqual(audit['max_bytes_billed_cap'], _CAP)
        self.assertEqual(audit['sql_source'], 'FIXED_SELECT_TEMPLATES')

  def test_question_text_never_enters_the_sql(self) -> None:
    opt = optimizer.VibeLiftAlphaEvolveOptimizer()
    marker = "zq_marker' OR 1=1 --"
    for q in _TEMPLATE_QUESTIONS:
      with self.subTest(q=q):
        sql = opt.execute_nl2sql_telemetry_query(f'{q} {marker}', project_id='proj-a')['generated_sql']
        self.assertNotIn('zq_marker', sql)

  def test_verifier_rejects_unsafe_sql(self) -> None:
    allowed = ['p.vibelift_mart']
    cases = {
        'SELECT 1 FROM `p.vibelift_mart.t`; DROP TABLE `p.vibelift_mart.t`': 'more than one statement',
        'DELETE FROM `p.vibelift_mart.t` WHERE TRUE': 'not a SELECT',
        'SELECT * FROM p.vibelift_mart.t': 'unquoted table reference',
        'SELECT * FROM `p.other_ds.t`': 'dataset not allowed',
        'SELECT 1': 'no table reference',
        '': 'empty SQL',
        'WITH x AS (SELECT 1 FROM `p.vibelift_mart.t`) SELECT * FROM x': 'unquoted table reference',
    }
    for sql, expected in cases.items():
      with self.subTest(sql=sql):
        res = optimizer.verify_nl2sql_sql(sql, allowed)
        self.assertFalse(res['read_only_enforced'])
        self.assertTrue(any(expected in e for e in res['verification_errors']), res['verification_errors'])

  def test_verifier_ignores_keywords_inside_names_and_literals(self) -> None:
    sql = "SELECT 'update' AS label FROM `my-set-1.vibelift_mart.t` WHERE note != 'delete'"
    res = optimizer.verify_nl2sql_sql(sql, ['my-set-1.vibelift_mart'])
    self.assertTrue(res['read_only_enforced'], res['verification_errors'])


class RunBigQueryQueryCapTest(unittest.TestCase):

  def setUp(self) -> None:
    self.svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='proj-cap')
    patcher = mock.patch.object(self.svc, '_get_access_token', return_value='tok')
    patcher.start()
    self.addCleanup(patcher.stop)

  def _run(self, response: dict, **kwargs):
    sent = {}

    def fake_urlopen(req, timeout=None):
      sent['payload'] = json.loads(req.data.decode('utf-8'))
      sent['timeout'] = timeout
      return _FakeResponse(json.dumps(response).encode('utf-8'))

    with mock.patch.object(gcp_telemetry.urllib.request, 'urlopen', side_effect=fake_urlopen):
      rows, info = self.svc.run_bigquery_query('SELECT 1 AS n FROM `proj-cap.vibelift_mart.t`', **kwargs)
    return rows, info, sent

  def test_explicit_cap_is_sent_to_bigquery(self) -> None:
    resp = {
        'jobComplete': True,
        'totalBytesProcessed': '2048',
        'cacheHit': False,
        'schema': {'fields': [{'name': 'n', 'type': 'INTEGER'}]},
        'rows': [{'f': [{'v': '1'}]}],
    }
    rows, info, sent = self._run(resp, max_bytes_billed=_CAP)
    self.assertEqual(sent['payload']['maximumBytesBilled'], str(_CAP))
    self.assertEqual(info['maximum_bytes_billed'], str(_CAP))
    self.assertEqual(info['total_bytes_processed'], 2048)
    self.assertIs(info['cache_hit'], False)
    self.assertIsNone(info['error'])
    self.assertEqual(len(rows), 1)

  def test_default_cap_comes_from_the_environment(self) -> None:
    with mock.patch.dict(os.environ, {'VIBELIFT_BQ_MAX_BYTES_BILLED': '12345'}):
      _, info, sent = self._run({'jobComplete': True, 'rows': []})
    self.assertEqual(sent['payload']['maximumBytesBilled'], '12345')
    self.assertEqual(info['maximum_bytes_billed'], '12345')

  def test_http_error_is_reported_not_raised(self) -> None:
    body = io.BytesIO(json.dumps({'error': {'message': 'Query exceeded limit for bytes billed'}}).encode())
    err = urllib.error.HTTPError('https://bq', 400, 'Bad Request', {}, body)
    with mock.patch.object(gcp_telemetry.urllib.request, 'urlopen', side_effect=err):
      rows, info = self.svc.run_bigquery_query('SELECT 1 FROM `proj-cap.vibelift_mart.t`', max_bytes_billed=_CAP)
    self.assertEqual(rows, [])
    self.assertIn('BigQuery HTTP 400', info['error'])
    self.assertIn('bytes billed', info['error'])

  def test_unfinished_job_is_an_error(self) -> None:
    rows, info, _ = self._run({'jobComplete': False}, max_bytes_billed=_CAP)
    self.assertEqual(rows, [])
    self.assertIn('did not finish', info['error'])


class Nl2SqlControllerTest(unittest.TestCase):

  def test_demo_mode_rows_are_labeled_as_simulator_output(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    self.assertFalse(ctrl._is_live_gcp())
    res = ctrl.query_nl2sql('Who are the power users by department?')
    self.assertEqual(res['execution_mode'], 'SIMULATOR')
    self.assertTrue(res['executive_summary'].startswith('Simulator result'))

  def _live_ctrl(self):
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    patcher = mock.patch.object(server.VibeLiftRuntimeController, '_is_live_gcp', return_value=True)
    patcher.start()
    self.addCleanup(patcher.stop)
    return ctrl

  def test_live_mode_runs_the_sql_on_bigquery_with_the_cap(self) -> None:
    ctrl = self._live_ctrl()
    bq_rows = [{'user_email': 'a@example.com', 'interactions_7d': 3}]
    info = {'error': None, 'maximum_bytes_billed': str(_CAP), 'total_bytes_processed': 3 * 1024 * 1024,
            'cache_hit': False}
    with mock.patch.object(ctrl.gcp_telemetry, 'run_bigquery_query', return_value=(bq_rows, info)) as run:
      res = ctrl.query_nl2sql('Who are the power users by department?')
    run.assert_called_once()
    self.assertEqual(run.call_args.kwargs['max_bytes_billed'], _CAP)
    self.assertEqual(run.call_args.args[0], res['generated_sql'])
    self.assertEqual(res['execution_mode'], 'LIVE_BIGQUERY_REST')
    self.assertEqual(res['rows'], bq_rows)
    self.assertEqual(res['columns'], ['user_email', 'interactions_7d'])
    self.assertIn('3.0 MB scanned', res['executive_summary'])
    self.assertIn(f'cap {_CAP // (1024 * 1024)} MB billed', res['executive_summary'])

  def test_live_mode_reports_bigquery_errors_without_example_rows(self) -> None:
    ctrl = self._live_ctrl()
    info = {'error': 'BigQuery HTTP 403: Access Denied', 'maximum_bytes_billed': str(_CAP),
            'total_bytes_processed': None, 'cache_hit': None}
    with mock.patch.object(ctrl.gcp_telemetry, 'run_bigquery_query', return_value=([], info)):
      res = ctrl.query_nl2sql('Show daily usage from the session mart')
    self.assertEqual(res['rows'], [])
    self.assertIn('Access Denied', res['executive_summary'])

  def test_live_mode_never_sends_a_write_question(self) -> None:
    ctrl = self._live_ctrl()
    with mock.patch.object(ctrl.gcp_telemetry, 'run_bigquery_query') as run:
      res = ctrl.query_nl2sql('DROP TABLE users; DELETE FROM billing')
    run.assert_not_called()
    self.assertEqual(res['execution_mode'], 'BLOCKED')
    self.assertEqual(res['rows'], [])

  def test_live_mode_never_sends_sql_that_failed_the_check(self) -> None:
    ctrl = self._live_ctrl()
    bad = {'generated_sql': 'SELECT * FROM p.x.t', 'rows': [{'x': 1}],
           'sql_safety_audit': {'blocked_dml_attempt': False, 'read_only_enforced': False,
                                'verification_errors': ['unquoted table reference: p.x.t']}}
    with mock.patch.object(ctrl.gcp_telemetry, 'run_bigquery_query') as run:
      res = ctrl._run_nl2sql_live(bad)
    run.assert_not_called()
    self.assertEqual(res['execution_mode'], 'BLOCKED')
    self.assertEqual(res['rows'], [])
    self.assertIn('unquoted table reference', res['executive_summary'])

  def test_state_default_helpers(self) -> None:
    not_run = server._nl2sql_not_run({'rows': [{'x': 1}], 'columns': ['x'], 'generated_sql': 'SELECT 1'})
    self.assertEqual(not_run['execution_mode'], 'NOT_RUN')
    self.assertEqual(not_run['rows'], [])
    self.assertEqual(not_run['generated_sql'], 'SELECT 1')
    self.assertIn('Not run yet', not_run['executive_summary'])
    sim = server._label_nl2sql_simulated({'executive_summary': 'Top users.'})
    self.assertEqual(sim['execution_mode'], 'SIMULATOR')
    self.assertTrue(sim['executive_summary'].startswith('Simulator result'))
    # Labeling twice does not stack the prefix; a blocked message is left as is.
    self.assertEqual(server._label_nl2sql_simulated(dict(sim))['executive_summary'], sim['executive_summary'])
    self.assertEqual(server._label_nl2sql_simulated({'executive_summary': 'Blocked: x'})['executive_summary'],
                     'Blocked: x')


class FleetTokenPricingTest(unittest.TestCase):

  def test_unknown_model_is_unpriced_not_guessed(self) -> None:
    self.assertIsNone(fleet._estimate_token_cost_usd(1_000_000, 10_000, 0, ['custom-model-x'], _CARDS))
    self.assertIsNone(fleet._estimate_token_cost_usd(1_000_000, 10_000, 0, [], _CARDS))
    self.assertIsNone(fleet._estimate_token_cost_usd(1_000_000, 10_000, 0, None, _CARDS))

  def test_no_tokens_costs_nothing(self) -> None:
    self.assertEqual(fleet._estimate_token_cost_usd(0, 0, 0, ['custom-model-x'], _CARDS), 0.0)

  def test_first_priced_model_and_resource_path_names(self) -> None:
    plain = fleet._estimate_token_cost_usd(1_000_000, 100_000, 400_000, ['gemini-2.5-pro'], _CARDS)
    path = fleet._estimate_token_cost_usd(
        1_000_000, 100_000, 400_000, ['custom-model-x', 'publishers/google/models/gemini-2.5-pro'], _CARDS)
    self.assertIsNotNone(plain)
    self.assertEqual(plain, path)



class DashboardSecurityHeadersTest(unittest.TestCase):
  """Both server stacks send the same CSP and hardening headers with the dashboard HTML."""

  def _check(self, headers) -> None:
    for name, value in server.DASHBOARD_SECURITY_HEADERS.items():
      self.assertEqual(headers.get(name), value, name)
    csp = headers.get('Content-Security-Policy')
    self.assertIn("connect-src 'self'", csp)
    self.assertIn("object-src 'none'", csp)
    self.assertNotIn('*', csp)

  def test_fastapi_dashboard_headers(self) -> None:
    import fastapi
    from fastapi.testclient import TestClient
    app = fastapi.FastAPI()
    server.register_api_routes(app, server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET))
    client = TestClient(app)
    for path in ('/', '/ui', '/app'):
      with self.subTest(path=path):
        resp = client.get(path)
        self.assertEqual(resp.status_code, 200)
        self._check(resp.headers)
    # JSON APIs are not HTML and don't need the page CSP.
    self.assertNotIn('content-security-policy', client.get('/api/health').headers)

  def test_stdlib_dashboard_headers(self) -> None:
    import threading
    import urllib.request
    httpd = server.VibeLiftHttpServer(('127.0.0.1', 0), server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    self.addCleanup(httpd.server_close)
    self.addCleanup(httpd.shutdown)
    port = httpd.server_address[1]
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=10) as resp:
      self.assertEqual(resp.status, 200)
      self._check(resp.headers)


class SurfaceIsolationTest(unittest.TestCase):
  """VIBELIFT_SURFACE=mcp isolates the MCP edge from the browser dashboard; dashboard disables /mcp."""

  def test_resolve_surface_defaults_and_validates(self) -> None:
    self.assertEqual(server.resolve_surface(None), 'all')
    self.assertEqual(server.resolve_surface('  MCP '), 'mcp')
    self.assertEqual(server.resolve_surface('dashboard'), 'dashboard')
    self.assertEqual(server.resolve_surface('invalid'), 'all')

  def test_fastapi_mcp_surface_hides_dashboard_and_apis(self) -> None:
    import fastapi
    from fastapi.testclient import TestClient

    from vibelift import mcp_server
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    with mock.patch.dict(os.environ, {'VIBELIFT_SURFACE': 'mcp', 'ENABLE_MCP_APP': '1'}):
      app = fastapi.FastAPI()
      mcp_server.register_mcp_routes(app)
      server.register_api_routes(app, ctrl)
      client = TestClient(app)
      self.assertEqual(client.get('/api/health').status_code, 200)
      self.assertEqual(client.get('/api/health').json()['surface'], 'mcp')
      self.assertEqual(client.get('/.well-known/agent-card.json').status_code, 200)
      self.assertEqual(client.get('/mcp').status_code, 405)
      for blocked in ('/', '/ui', '/app', '/logo.jpg', '/api/state', '/api/ge_fleet'):
        self.assertEqual(client.get(blocked).status_code, 404, blocked)

  def test_fastapi_dashboard_surface_disables_mcp(self) -> None:
    import fastapi
    from fastapi.testclient import TestClient

    from vibelift import mcp_server
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    with mock.patch.dict(os.environ, {'VIBELIFT_SURFACE': 'dashboard', 'ENABLE_MCP_APP': '1'}):
      self.assertFalse(mcp_server.mcp_app_enabled())
      app = fastapi.FastAPI()
      mcp_server.register_mcp_routes(app)
      server.register_api_routes(app, ctrl)
      client = TestClient(app)
      self.assertEqual(client.get('/ui').status_code, 200)
      self.assertEqual(client.get('/api/health').json()['surface'], 'dashboard')
      self.assertEqual(client.get('/mcp').status_code, 404)

  def test_stdlib_mcp_surface_blocks_dashboard_and_allows_mcp(self) -> None:
    import threading
    import urllib.error
    import urllib.request
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    httpd = server.VibeLiftHttpServer(('127.0.0.1', 0), ctrl)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    self.addCleanup(httpd.server_close)
    self.addCleanup(httpd.shutdown)
    port = httpd.server_address[1]
    with mock.patch.dict(os.environ, {'VIBELIFT_SURFACE': 'mcp', 'ENABLE_MCP_APP': '1'}):
      with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=10) as resp:
        self.assertEqual(resp.status, 200)
      for path in ('/ui', '/api/state'):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
          urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=10)
        self.assertEqual(ctx.exception.code, 404)
      req = urllib.request.Request(
          f'http://127.0.0.1:{port}/api/reset', data=b'{}',
          headers={'Content-Type': 'application/json'}, method='POST',
      )
      with self.assertRaises(urllib.error.HTTPError) as ctx:
        urllib.request.urlopen(req, timeout=10)
      self.assertEqual(ctx.exception.code, 404)


if __name__ == '__main__':
  unittest.main()

