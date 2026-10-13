"""Table-driven live-mode policy: every mutating POST route is explicitly guarded or allowed.

Background: the stress loop used to spot-check five hand-picked endpoints, so `/api/reset` (which also
wipes SDK events, AIVE logs and SME ratings) and `/api/add_parameter` shipped unguarded in live mode.
This test enumerates the routes registered on the app and fails for any route missing from the table,
so a new mutating route cannot ship unclassified.
"""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from finops_dashboard import mcp_server, server
from scripts import stress_loop_1000


class TestLiveModePolicyTable(unittest.TestCase):
  """Guarded routes answer DISABLED_IN_LIVE_MODE; allowed routes never do; the table covers every POST route."""

  @classmethod
  def setUpClass(cls) -> None:
    for name in ('httpx', 'finops_dashboard.server', 'finops_dashboard.telemetry', 'finops_dashboard.audit_sink', 'finops-mcp', 'finops_dashboard.fleet'):
      logging.getLogger(name).setLevel(logging.CRITICAL)
    cls.ctrl = stress_loop_1000._configure_in_memory_controller()
    cls.app = server.fastapi.FastAPI()
    mcp_server.register_mcp_routes(cls.app)
    server.register_api_routes(cls.app, cls.ctrl)
    cls.client = TestClient(cls.app, raise_server_exceptions=False)

  def test_every_post_route_is_classified(self) -> None:
    post_routes = stress_loop_1000._assert_live_policy_covers_all_post_routes(self.app)
    self.assertGreaterEqual(len(post_routes), 25)
    self.assertEqual(sorted(post_routes), sorted(stress_loop_1000.LIVE_MODE_POLICY))

  def test_guarded_routes_fail_closed_in_live_mode(self) -> None:
    guarded = sorted(p for p, pol in stress_loop_1000.LIVE_MODE_POLICY.items() if pol == 'guarded')
    self.assertIn('/api/reset', guarded)
    self.assertIn('/api/add_parameter', guarded)
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), mock.patch.dict('os.environ', {'FINOPS_ENABLE_SIMULATOR': ''}):
      for path in guarded:
        with self.subTest(path=path):
          r = self.client.post(path, json=stress_loop_1000.LIVE_POLICY_BODIES.get(path, {}))
          self.assertEqual(r.status_code, 200, r.text[:200])
          body = r.json()
          self.assertEqual(body.get('status'), 'DISABLED_IN_LIVE_MODE', body)
          self.assertGreater(len(body.get('reason', '')), 10)

  def test_allowed_routes_are_not_blocked_in_live_mode(self) -> None:
    allowed = sorted(p for p, pol in stress_loop_1000.LIVE_MODE_POLICY.items() if pol == 'allowed')
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True):
      for path in allowed:
        with self.subTest(path=path):
          r = self.client.post(path, json=stress_loop_1000.LIVE_POLICY_BODIES.get(path, {}))
          self.assertIn(r.status_code, (200, 400, 422), (path, r.text[:200]))
          body = r.json()
          status = body.get('status') if isinstance(body, dict) else None
          self.assertNotEqual(status, 'DISABLED_IN_LIVE_MODE', path)

  def test_reset_in_live_mode_keeps_sme_ratings_and_sdk_events(self) -> None:
    """The concrete harm the guard prevents: a live-mode reset must not discard recorded evidence."""
    from finops_dashboard import sme_eval, telemetry

    store = sme_eval.get_sme_evaluation_store()
    store.submit_rating({'persona_id': 'cfo_exec', 'overall_rating': 4, 'verdict': 'APPROVED_WITH_NOTES'})
    before_ratings = len(store.list_ratings())
    self.assertGreaterEqual(before_ratings, 1)
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), mock.patch.dict('os.environ', {'FINOPS_ENABLE_SIMULATOR': ''}):
      r = self.client.post('/api/reset', json={})
    self.assertEqual(r.json().get('status'), 'DISABLED_IN_LIVE_MODE')
    self.assertEqual(len(store.list_ratings()), before_ratings, 'live-mode reset must not wipe SME ratings')
    self.assertIsNotNone(telemetry.get_ingest_dlq_events())

  def test_simulator_override_env_var_re_enables_guarded_routes(self) -> None:
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), mock.patch.dict('os.environ', {'FINOPS_ENABLE_SIMULATOR': '1'}):
      r = self.client.post('/api/add_parameter', json=stress_loop_1000.LIVE_POLICY_BODIES['/api/add_parameter'])
    self.assertEqual(r.status_code, 200)
    self.assertNotEqual(r.json().get('status'), 'DISABLED_IN_LIVE_MODE')


if __name__ == '__main__':
  unittest.main()
