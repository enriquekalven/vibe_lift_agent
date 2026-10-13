"""Executes the REAL dashboard scripts in a ghost DOM and verifies the UI error/state-preservation contract.

Why this exists: the earlier Node harness re-implemented `isNonStateErrorPayload` / `renderState` and
asserted properties of its own copy. That copy drifted from finops_dashboard/ui/template.py (it lacked the
`invalid_payload` / `dead_letter` gate) and could never catch a half-rendered DOM. This test loads the
rendered HTML, runs all three inline scripts in `tests/ui_dom_harness.js` (no jsdom / no npm deps) and
drives the real `renderState`, `triggerApi` and `isNonStateErrorPayload` functions.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from finops_dashboard import server
from scripts import stress_loop_1000

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / 'tests' / 'ui_dom_harness.js'


def run_ghost_dom_harness(html: str, good_state: dict, iterations: int = 1, timeout_s: int = 60) -> dict:
  """Writes the page + state to a temp dir and runs the Node ghost-DOM harness; returns its JSON report."""
  node = shutil.which('node')
  if not node:
    raise unittest.SkipTest('node is not installed; ghost-DOM UI contract test skipped')
  with tempfile.TemporaryDirectory(prefix='finops-ui-dom-') as tmp:
    html_path = pathlib.Path(tmp) / 'dashboard.html'
    state_path = pathlib.Path(tmp) / 'good_state.json'
    html_path.write_text(html, encoding='utf-8')
    state_path.write_text(json.dumps(good_state), encoding='utf-8')
    proc = subprocess.run(
        [node, str(HARNESS), str(html_path), str(state_path), str(int(iterations))],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
  lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip().startswith('{')]
  if not lines:
    raise AssertionError(f'ghost-DOM harness produced no JSON report (exit {proc.returncode}): {proc.stderr[-2000:]}')
  report = json.loads(lines[-1])
  report['exit_code'] = proc.returncode
  return report


class TestRealRenderStateInGhostDom(unittest.TestCase):
  """The real page scripts must preserve state on error payloads and roll back on mid-render exceptions."""

  @classmethod
  def setUpClass(cls) -> None:
    for name in ('httpx', 'finops_dashboard.server', 'finops_dashboard.telemetry', 'finops_dashboard.audit_sink', 'finops-mcp'):
      logging.getLogger(name).setLevel(logging.CRITICAL)
    cls.ctrl = stress_loop_1000._configure_in_memory_controller()
    cls.good_state = cls.ctrl.get_state_payload(include_fleet=False)
    cls.html = server.ui_template.render_dashboard_html()

  def test_real_dashboard_scripts_honour_state_preservation_contract(self) -> None:
    report = run_ghost_dom_harness(self.html, self.good_state, iterations=7)  # >= longest case list, so every case runs
    self.assertEqual(report['inline_scripts_loaded'], report['inline_scripts_total'], report.get('failures'))
    self.assertEqual(report['failed'], 0, f"ghost-DOM checks failed: {report['failures']}")
    self.assertTrue(report['ok'])
    self.assertEqual(report['exit_code'], 0)
    # 7 error payload shapes + 3 malformed states + 5 triggerApi cases, each with several assertions.
    self.assertGreaterEqual(report['checks'], 40)

  def test_harness_detects_a_renderer_that_lets_exceptions_escape(self) -> None:
    """Mutation check: a renderer without the commit-after-render boundary must FAIL the harness."""
    anchor = '      } catch (err) {\n        currentState = previousState;'
    self.assertIn(anchor, self.html, 'commit-after-render boundary missing from template.py')
    mutant_html = self.html.replace(anchor, '      } catch (err) {\n        throw err;\n        currentState = previousState;', 1)
    report = run_ghost_dom_harness(mutant_html, self.good_state, iterations=1)
    self.assertFalse(report['ok'])
    self.assertTrue(any('must not propagate the exception' in f for f in report['failures']), report['failures'])
    self.assertTrue(any('rolled back' in f for f in report['failures']), report['failures'])

  def test_real_gate_rejects_dead_letter_payloads_the_old_copy_missed(self) -> None:
    """The gate condition that had drifted in the old harness copy is now asserted against the real code."""
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=False):
      report = run_ghost_dom_harness(self.html, self.good_state, iterations=7)
    self.assertEqual(report['failed'], 0, report['failures'])


if __name__ == '__main__':
  unittest.main()
