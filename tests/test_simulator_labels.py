"""Pins the optimizer-simulator labels so fabricated "real optimization" wording can't creep back in."""

import os
import re
import unittest

os.environ.setdefault('GOOGLE_APPLICATION_CREDENTIALS', '/nonexistent/offline-test-credentials.json')
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'test-project')

from vibelift import optimizer
from vibelift.ui import template


def _js_function(html: str, name: str) -> str:
  """Returns the source of one top-level `function name(...) {...}` in the dashboard script."""
  start = html.index(f'function {name}(')
  depth = 0
  body_start = html.index('{', start)
  for idx in range(body_start, len(html)):
    if html[idx] == '{':
      depth += 1
    elif html[idx] == '}':
      depth -= 1
      if depth == 0:
        return html[start:idx + 1]
  raise AssertionError(f'unterminated function {name}')


class DashboardSimulatorLabelsTest(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.html = template.render_dashboard_html(None)

  def test_header_buttons_say_simulated(self):
    self.assertIn('Run Optimizer (simulated)', self.html)
    self.assertIn('Simulate Alert', self.html)
    self.assertIn('id="simulatorNoteText"', self.html)

  def test_honesty_panels_are_present(self):
    for element_id in ('apiNotice', 'ingestDeadLetterBox', 'ingestDeadLetterBody', 'ingestDeadLetterCount'):
      self.assertIn(f'id="{element_id}"', self.html)

  def test_old_fabricated_labels_are_gone(self):
    for stale in (
        'Estimated Monthly AlphaEvolve Savings',
        'CRITICAL LOG ANOMALY (Cache Bust + 429 Spike)',
        'ANOMALY ACTIVE — RUN OPTIMIZER',
        'LIVE ALERT: Production Log Anomaly Detected',
        '(&lt;10ms',
    ):
      self.assertNotIn(stale, self.html)

  def test_embedded_mode_only_switches_views(self):
    source = _js_function(self.html, 'applyEmbeddedMutation')
    endpoints = set(re.findall(r"endpoint === '(/api/[a-z_]+)'", source))
    self.assertEqual(endpoints, {'/api/select_agent', '/api/select_optimizer', '/api/reset'})
    self.assertNotIn('lastPt', source)

  def test_synthetic_sample_button_is_demo_only(self):
    button = re.search(r'<button[^>]*onclick="emitLiveAiveUsageEvent\(\)"[^>]*>[^<]*</button>', self.html)
    self.assertIsNotNone(button)
    self.assertIn('sim-panel', button.group(0))
    self.assertIn('Synthetic', button.group(0))
    source = _js_function(self.html, 'emitLiveAiveUsageEvent')
    self.assertIn("classList.contains('live-data')", source)
    self.assertIn("task_type: 'SYNTHETIC_SAMPLE'", source)

  def test_probe_sends_a_measured_latency(self):
    source = _js_function(self.html, 'emitLiveDecoratorEvent')
    self.assertIn("fetch('/api/health'", source)
    self.assertNotIn("fetch('/healthz'", source)  # Cloud Run's front end reserves paths ending in "z".
    self.assertIn('performance.now()', source)
    self.assertNotIn('Math.random', source)


class OptimizerSimulatorLabelsTest(unittest.TestCase):

  def setUp(self):
    self.sim = optimizer.VibeLiftAlphaEvolveOptimizer()

  def test_platform_payload_flags_simulator(self):
    payload = self.sim.get_optimizer_platforms_payload()
    self.assertIs(payload['simulator'], True)
    self.assertIn('Simulator', payload['simulator_note'])

  def test_demo_profiles_are_labeled_simulated(self):
    for agent in self.sim._agents.values():
      self.assertTrue(agent.health_status.startswith('SIMULATED'), agent.health_status)

  def test_anomaly_and_generation_report_simulated_status(self):
    self.sim.select_agent(next(iter(self.sim._agents)))
    active = self.sim.inject_anomaly()
    self.assertTrue(active.health_status.startswith('SIMULATED'), active.health_status)
    active = self.sim.run_next_generation()
    self.assertTrue(active.health_status.startswith('OPTIMIZED IN SIMULATION'), active.health_status)
    latest = active.actions[0]
    self.assertEqual(latest.status, 'SIMULATED (nothing deployed)')
    self.assertNotRegex(f'{latest.action_title} {latest.diff_snippet}', r'PR #\d')


if __name__ == '__main__':
  unittest.main()
