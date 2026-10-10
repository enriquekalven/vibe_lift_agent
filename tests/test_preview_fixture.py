"""Tests for the `make preview` fixture (scripts/preview_dashboard.py).

The fixture must drive the real dashboard in live mode without any Google Cloud call, cover every
fleet health state, keep its numbers internally consistent, mark every page as fictional, listen
on 127.0.0.1 only, and leave no patches behind.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from unittest import mock

from vibelift import fleet as ge_fleet
from vibelift import server
from vibelift.ui import template as ui_template

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location('preview_dashboard', ROOT / 'scripts' / 'preview_dashboard.py')
assert _SPEC and _SPEC.loader
preview = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preview)


class PreviewFixtureDataTest(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.fleet = preview.FixtureFleet().collect(window_hours=24)

  def test_fleet_payload_shape(self):
    self.assertEqual(self.fleet['project_id'], preview.PROJECT)
    self.assertEqual(len(self.fleet['agents']), 14)
    self.assertEqual(len(self.fleet['engines']), 3)
    self.assertEqual(sum(e['agents_count'] for e in self.fleet['engines']), 14)
    for key in ('trend', 'model_usage', 'model_usage_previous', 'unregistered_runtimes', 'skills_and_mcp', 'source_status'):
      self.assertIn(key, self.fleet)

  def test_totals_count_shared_runtimes_once(self):
    unique = {}
    for a in self.fleet['agents']:
      unique.setdefault(ge_fleet.runtime_backend_key(a), a)
    expected = sum(a['metrics']['requests'] for a in unique.values() if a['metrics']['requests'] is not None)
    self.assertEqual(self.fleet['totals']['requests'], expected)

  def test_trend_matches_agents(self):
    trend = self.fleet['trend']
    keys = {ge_fleet.runtime_backend_key(a): a for a in self.fleet['agents']}
    self.assertTrue(trend['by_runtime'])
    for key, row in trend['by_runtime'].items():
      self.assertIn(key, keys)
      self.assertEqual(len(row['requests']), len(trend['bucket_ends']))
      self.assertAlmostEqual(sum(row['requests']), keys[key]['metrics']['requests'], delta=len(row['requests']))
      self.assertEqual(sum(row['errors_5xx']), keys[key]['metrics']['errors_5xx'])

  def test_unpriced_model_stays_unpriced(self):
    usage = {m['model']: m for m in self.fleet['model_usage']['models']}
    self.assertIsNone(usage['text-embedding-005']['est_cost_usd'])
    self.assertIn('text-embedding-005', self.fleet['model_usage']['totals']['models_without_rate_card'])

  def test_fixture_is_deterministic(self):
    again = preview.FixtureFleet().collect(window_hours=24)
    strip = lambda f: [(a['agent_id'], a['metrics']['requests'], a['metrics']['errors_5xx']) for a in f['agents']]  # noqa: E731
    self.assertEqual(strip(again), strip(self.fleet))
    self.assertEqual(again['trend']['by_runtime'], self.fleet['trend']['by_runtime'])

  def test_trace_logging_is_refused(self):
    with self.assertRaises(ge_fleet.InvalidAgentRequestError):
      preview.FixtureFleet().enable_agent_observability(enabled=True)

  @unittest.skipUnless(shutil.which('node'), 'node is not installed')
  def test_every_health_state_is_represented(self):
    js = (ROOT / 'vibelift' / 'ui' / 'static' / 'dashboard_enhancements.js').read_text(encoding='utf-8')
    script = js + '\nprocess.stdout.write(JSON.stringify(VL.healthCounts(' + json.dumps(self.fleet['agents']) + ')));\n'
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
      f.write(script)
      path = f.name
    try:
      proc = subprocess.run([shutil.which('node'), path], capture_output=True, text=True, check=False, timeout=60)
    finally:
      os.unlink(path)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    counts = json.loads(proc.stdout)
    for level in ('failing', 'broken', 'degraded', 'healthy', 'idle', 'disabled', 'unknown'):
      self.assertGreater(counts[level], 0, level)


class PreviewWiringTest(unittest.TestCase):

  def test_banner_is_added_once_right_after_body(self):
    html = preview.add_banner('<html><body class="x"><main></main></body></html>')
    self.assertEqual(html.count(preview.BANNER_ID), 1)
    self.assertTrue(html.startswith('<html><body class="x"><div id="vlPreviewBanner" role="status"'))
    self.assertEqual(preview.add_banner(html), html)
    self.assertIn('fictional', preview.BANNER_TEXT)

  def test_fixture_drives_a_live_dashboard_without_network(self):
    with mock.patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': preview.PROJECT}):
      controller = server.VibeLiftRuntimeController()
    real_render = ui_template.render_dashboard_html
    with mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network call in preview')) as urlopen:
      with preview.fixture_installed(controller):
        state = controller.get_state_payload(window_hours=24)
        html = ui_template.render_dashboard_html()
    self.assertEqual(urlopen.call_count, 0)  # even a swallowed call would show up here
    self.assertTrue(state['live_data'])
    self.assertEqual(len(state['ge_fleet']['agents']), 14)
    self.assertEqual(len(state['ge_daily_usage']['days']), 30)
    self.assertEqual(state['ge_daily_usage']['billing_status'], 'LIVE')
    self.assertEqual(len(state['user_centric']['ge_sessions']), 40)
    self.assertEqual(state['live_finops']['spend_drift']['status'], 'LIVE')
    self.assertIn(preview.BANNER_ID, html)
    # Everything is restored on exit.
    self.assertIs(ui_template.render_dashboard_html, real_render)
    self.assertNotIn(preview.BANNER_ID, ui_template.render_dashboard_html())
    self.assertNotIsInstance(controller.ge_fleet, preview.FixtureFleet)
    self.assertNotIn('_query_bigquery_rest', vars(controller.gcp_telemetry))

  def test_server_listens_on_localhost_only(self):
    with mock.patch.dict(os.environ), mock.patch.object(server, 'run_standalone_server') as run:
      with contextlib.redirect_stdout(io.StringIO()):
        preview.main(['--port', '8799'])
    run.assert_called_once_with(port=8799, host='127.0.0.1')
    self.assertNotIsInstance(server._global_controller.ge_fleet, preview.FixtureFleet)  # pylint: disable=protected-access


if __name__ == '__main__':
  unittest.main()
