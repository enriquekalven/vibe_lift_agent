"""No guessed prices, no invented calculator inputs, no simulated values shown as live, no hardcoded UI fallbacks."""

import asyncio
import json
import re
import time
import unittest
from unittest import mock

from app import agent as adk_agent
from tests import test_fleet as ge_fleet_test
from vibelift import billing_export, gcp_telemetry, mcp_server, optimizer, server, telemetry
from vibelift.ui import template as ui_template

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()


def _turn(model: str, prompt: int = 1_000_000, cached: int = 400_000, out: int = 10_000) -> telemetry.TurnUsageLog:
  return telemetry.TurnUsageLog(
      timestamp='2026-10-01T00:00:00Z', agent_name='agent', model=model, turn_index=1,
      prompt_prefix_hash='h', cache_breakpoint_line=None, cache_breakpoint_reason='logged',
      prompt_token_count=prompt, cached_content_token_count=cached, cache_creation_input_tokens=0,
      uncached_input_tokens=prompt - cached, candidates_token_count=out, thoughts_token_count=0,
      status_code=200, tool_called='inference', evolution_generation=0,
  )


class UnpricedTurnTest(unittest.TestCase):

  def test_model_without_rate_card_is_unpriced_not_priced_with_another_card(self) -> None:
    for model in ('unknown', '', 'gemini-9.9-imaginary'):
      t = _turn(model)
      self.assertFalse(t.priced, model)
      self.assertEqual(t.compute_costs(), (None, None, None))
      ba = t.to_dict()['billing_attribution']
      self.assertFalse(ba['priced'])
      self.assertIsNone(ba['rate_card'])
      self.assertIsNone(ba['naive_count_tokens_usd'])
      self.assertIsNone(ba['actual_log_cached_usd'])

  def test_publisher_resource_name_uses_its_own_card(self) -> None:
    card = telemetry.RATE_CARDS['gemini-2.5-flash']
    t = _turn('publishers/google/models/gemini-2.5-flash')
    naive, actual, _ = t.compute_costs()
    self.assertEqual(naive, round((1_000_000 * card.input_per_million_usd + 10_000 * card.output_per_million_usd) / 1e6, 6))
    self.assertEqual(actual, round((400_000 * card.cached_read_per_million_usd + 600_000 * card.input_per_million_usd
                                    + 10_000 * card.output_per_million_usd) / 1e6, 6))
    self.assertEqual(t.to_dict()['billing_attribution']['rate_card'], 'gemini-2.5-flash')

  def test_summary_dollars_cover_priced_turns_only(self) -> None:
    priced, unpriced = _turn('gemini-2.5-flash'), _turn('unknown')
    both = telemetry.summarize_log_stream([priced, unpriced])
    only = telemetry.summarize_log_stream([priced])
    self.assertEqual((both['priced_turns'], both['unpriced_turns']), (1, 1))
    for key in ('total_naive_usd', 'total_actual_usd', 'total_saved_usd'):
      self.assertEqual(both[key], only[key], key)
    self.assertEqual(both['total_prompt_tokens'], 2_000_000)
    self.assertEqual(both['total_cached_read_tokens'], 800_000)
    self.assertEqual(both['total_uncached_input_tokens'], 1_200_000)
    self.assertEqual(telemetry.summarize_log_stream([])['unpriced_turns'], 0)


class CalculatorToolsTest(unittest.TestCase):

  def _mcp(self, args: dict[str, object]) -> dict[str, object]:
    return asyncio.run(mcp_server._tool_calculate_cache_economics('s', args))['structuredContent']

  def test_mcp_calculator_prices_only_what_was_passed(self) -> None:
    card = telemetry.RATE_CARDS['gemini-2.5-flash']
    r = self._mcp({'prompt_token_count': 2_000_000, 'cached_content_token_count': 0, 'model': 'gemini-2.5-flash'})
    self.assertTrue(r['priced'])
    self.assertEqual((r['output_tokens'], r['thoughts_tokens']), (0, 0))
    self.assertEqual(r['naive_cost_usd'], round(2 * card.input_per_million_usd, 6))

  def test_mcp_calculator_leaves_unknown_model_unpriced(self) -> None:
    r = self._mcp({'prompt_token_count': 1000, 'cached_content_token_count': 0, 'model': 'not-a-model'})
    self.assertFalse(r['priced'])
    self.assertIsNone(r['naive_cost_usd'])
    self.assertIsNone(r['net_savings_usd'])
    self.assertIn('gemini-2.5-flash', str(r['note']))

  def test_mcp_tool_description_lists_the_real_rate_cards(self) -> None:
    tool = next(t for t in mcp_server._TOOLS if t['name'] == 'calculate_prompt_cache_economics')
    for name in telemetry.RATE_CARDS:
      self.assertIn(name, tool['description'])

  def test_adk_calculator_prices_only_what_was_passed(self) -> None:
    card = telemetry.RATE_CARDS['gemini-2.5-flash']
    r = json.loads(adk_agent.calculate_cache_economics(1_000_000, 0, model='gemini-2.5-flash'))
    self.assertTrue(r['priced'])
    self.assertEqual(r['naive_cost_usd'], round(card.input_per_million_usd, 6))
    unknown = json.loads(adk_agent.calculate_cache_economics(1000, 0, model='not-a-model'))
    self.assertFalse(unknown['priced'])
    self.assertIsNone(unknown['saved_usd'])


class LiveTurnHonestyTest(unittest.TestCase):

  def _svc(self) -> gcp_telemetry.GoogleCloudTelemetryService:
    svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='example-project', region='us-central1')
    svc._cli_token = 'valid-token'
    svc._cli_token_ts = time.monotonic()
    return svc

  def test_mart_turn_without_model_is_unpriced(self) -> None:
    svc = self._svc()
    rows = [{'ts': '2026-10-01T00:00:00Z', 'agent_name': 'ge', 'model_name': None, 'turn_id': 't1',
             'input_tokens': '1000', 'output_tokens': '10', 'cached_input_tokens': None,
             'turn_status': 'SUCCESS', 'turn_source': 'GE'}]
    with mock.patch.object(svc, '_query_bigquery_rest', return_value=rows):
      turns = svc.fetch_live_cloud_turns(force_refresh=True)
    self.assertEqual(len(turns), 1)
    self.assertFalse(turns[0].priced)
    self.assertEqual(turns[0].evolution_generation, 0)
    self.assertIsNone(turns[0].cache_breakpoint_line)

  def test_cloud_logging_entry_without_model_gets_no_guessed_model_or_breakpoint(self) -> None:
    svc = self._svc()
    entry = mock.MagicMock(
        payload={'usage_metadata': {'prompt_token_count': 500, 'cached_content_token_count': 0,
                                    'candidates_token_count': 5}},
        timestamp=None, resource=mock.MagicMock(labels={'service_name': 'svc'}))
    client = mock.MagicMock()
    client.list_entries.return_value = [entry]
    svc._logging_client = client
    with mock.patch.object(svc, '_query_bigquery_rest', return_value=[]):
      turns = svc.fetch_live_cloud_turns(force_refresh=True)
    self.assertEqual(len(turns), 1)
    t = turns[0]
    self.assertEqual(t.model, 'unknown')
    self.assertFalse(t.priced)
    self.assertIsNone(t.cache_breakpoint_line)
    self.assertEqual(t.evolution_generation, 0)


class CatalogHonestyTest(unittest.TestCase):

  def test_reference_only_catalog_drops_simulated_values(self) -> None:
    cat = optimizer.build_otel_5_layer_catalog()
    live = optimizer.catalog_reference_only(cat)
    self.assertIs(live['values_measured'], False)
    self.assertEqual(live['watch_out_alarms'], [])
    live_params = [p for layer in live['layers'] for p in layer['parameters']]
    self.assertEqual([p['otel_name'] for p in live_params],
                     [p['otel_name'] for layer in cat['layers'] for p in layer['parameters']])
    for p in live_params:
      self.assertIsNone(p['baseline_value'])
      self.assertIsNone(p['current_value'])
      self.assertEqual(p['status'], 'NOT MEASURED')
    self.assertIsNotNone(cat['layers'][0]['parameters'][0]['current_value'])  # demo copy untouched

  def test_metric_keys_say_whether_they_exist(self) -> None:
    self.assertEqual(optimizer.metric_key_status('run.googleapis.com/request_latencies'), 'Cloud Monitoring metric')
    self.assertIn('semantic-convention', optimizer.metric_key_status('gen_ai.usage.input_tokens'))
    self.assertTrue(optimizer.metric_key_status('gcp.vertex.a2ui.interaction_rate').startswith('Proposed name'))
    cat = optimizer.build_otel_5_layer_catalog()
    self.assertTrue(all(p.get('key_status') for layer in cat['layers'] for p in layer['parameters']))

  def test_hosting_cost_is_not_invented(self) -> None:
    tco = optimizer.build_otel_5_layer_catalog()['architecture_tco']
    self.assertIsNone(tco['monthly_compute_cost_usd'])
    self.assertNotIn('net_roi_multiple', tco)
    self.assertNotIn('monthly_fleet_savings_usd', tco)
    self.assertIn('Billing export', tco['cost_note'])

  def test_live_state_sends_reference_only_catalog(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_live_bigquery_project_insights', return_value={}), \
         mock.patch.object(ctrl.gcp_telemetry, 'list_cloud_run_agent_services', return_value=[]), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_gemini_enterprise_support_telemetry', return_value=[]), \
         mock.patch.object(ctrl.billing_export, 'get', return_value=billing_export.not_connected_payload()), \
         mock.patch.object(ctrl.billing_export, 'get_daily_ai_costs', return_value=billing_export.daily_cost_not_connected()):
      state = ctrl.get_state_payload(include_fleet=False, window_hours=24)
    cat = state['otel_catalog']
    self.assertIs(cat['values_measured'], False)
    self.assertTrue(all(p['current_value'] is None for layer in cat['layers'] for p in layer['parameters']))
    demo = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET).get_state_payload(include_fleet=False)
    self.assertNotIn('values_measured', demo['otel_catalog'])


class DashboardFallbackTest(unittest.TestCase):
  """The dashboard script must show a dash, not an invented number, when a value is missing."""

  # Input-field defaults, query windows, chart buckets and timeouts are settings, not data.
  _ALLOWED_CONTEXT = ('getElementById(', 'window_days', 'window_hours', 'fleetWindowHours', 'bucket_seconds', 'timeoutMs')

  @classmethod
  def setUpClass(cls) -> None:
    cls.script = ui_template.render_dashboard_html().split('<script', 1)[1]

  def test_no_numeric_fallbacks_in_data_rendering(self) -> None:
    bad = []
    for line in self.script.splitlines():
      for m in re.finditer(r'(\|\||\?\?)\s*(-?\d+(?:\.\d+)?)\b', line):
        if float(m.group(2)) in (0.0, 1.0) or any(a in line for a in self._ALLOWED_CONTEXT):
          continue
        bad.append(line.strip()[:160])
    self.assertEqual(bad, [])

  def test_no_invented_status_or_verification_labels(self) -> None:
    for text in ("|| 'HIGH'", "|| 'APPROVED'", "|| 'OPTIMIZED'", "|| 'PASS'", "|| 'REMEDIATED'",
                 "|| 'SAFE_TO_PROMOTE_CANARY'", 'Verified across sequential', 'Prefix SHA-256 verification across turns',
                 '* 0.45', '|| 39.0', '?? 39.0'):
      self.assertNotIn(text, self.script, text)

  def test_logged_turns_are_not_shown_as_static_matches_or_priced_when_unpriced(self) -> None:
    self.assertIn("badge('Not checked', 'badge-blue')", self.script)
    self.assertIn("'Unpriced (no rate card for '", self.script)
    self.assertIn('ts.unpriced_turns', self.script)


if __name__ == '__main__':
  unittest.main()
