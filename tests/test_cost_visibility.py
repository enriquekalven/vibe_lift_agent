"""Tests for billing error visibility and per-user list-price cost estimates."""

from __future__ import annotations

import copy
import os
import types
import unittest
from unittest import mock

from tests import test_fleet as ge_fleet_test
from vibelift import billing_export, finops, fleet, ge_mart, server
from vibelift.ui import template as ui_template

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()
_CARDS = _FAKE_FLEET.rate_cards()


class BillingErrorVisibilityTest(unittest.TestCase):

  def test_classify_billing_error_all_kinds(self) -> None:
    cases = [
        ("Invalid billing export table name: 'p:d.t'", 'invalid_table_name'),
        ('BigQuery HTTP 403: Access Denied: Table p:d.t: User does not have permission, or perhaps it does not exist.',
         'permission_denied'),
        ('BigQuery HTTP 400: Unrecognized name: credits at [7:45]', 'schema_mismatch'),
        ('BigQuery HTTP 404: Not found: Table project-maui:billing_export.gcp_billing_export_v1_X', 'not_found'),
        ('Query exceeded limit for bytes billed: 5368709120.', 'bytes_limit'),
        ('No Google Cloud credentials available.', 'auth'),
        ('Billing export query did not finish in time.', 'timeout'),
        ('Something unexpected happened.', 'unknown'),
        (None, 'unknown'),
    ]
    for msg, expected_kind in cases:
      with self.subTest(msg=msg):
        res = billing_export.classify_billing_error(msg)
        self.assertEqual(res['error_kind'], expected_kind)
        self.assertTrue(res['hint'])

  def test_payload_hints_by_status(self) -> None:
    nc = billing_export.not_connected_payload()
    self.assertEqual(nc['status'], 'NOT_CONNECTED')
    self.assertIsNone(nc['error_kind'])
    self.assertEqual(nc['hint'], billing_export.HOW_TO_CONNECT)

    d_nc = billing_export.daily_cost_not_connected()
    self.assertEqual(d_nc['hint'], billing_export.HOW_TO_CONNECT)
    self.assertIsNone(d_nc['error_kind'])

    err = billing_export.not_connected_payload('BigQuery HTTP 403: Access Denied', status='ERROR', table='p.d.t')
    self.assertEqual(err['error_kind'], 'permission_denied')
    self.assertIn('dataViewer', err['hint'])

    d_err = billing_export.daily_cost_not_connected('Not found: Table p.d.t', status='ERROR', table='p.d.t')
    self.assertEqual(d_err['error_kind'], 'not_found')

    loading = billing_export.not_connected_payload('Loading…', status='LOADING', table='p.d.t')
    self.assertIsNone(loading['error_kind'])
    self.assertIsNone(loading['hint'])

    live = billing_export.summarize_rows([], 'example-project.ds.gcp_billing_export_v1_X')
    self.assertNotIn('hint', live)
    self.assertNotIn('error_kind', live)

  def test_reader_logs_warning_when_run_returns_error(self) -> None:
    table = 'example-project.ds.gcp_billing_export_v1_X'
    with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: table}):
      reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
      with mock.patch.object(reader, '_run', return_value=([], 'BigQuery HTTP 403: Access Denied')), \
           self.assertLogs(billing_export.logger, level='WARNING') as cm:
        res = reader.get(non_blocking=False)
        daily = reader.get_daily_ai_costs(non_blocking=False)
    self.assertEqual(res['status'], 'ERROR')
    self.assertEqual(res['error_kind'], 'permission_denied')
    self.assertEqual(daily['status'], 'ERROR')
    self.assertEqual(daily['error_kind'], 'permission_denied')
    joined = '\n'.join(cm.output)
    self.assertIn('Billing export query failed: BigQuery HTTP 403', joined)
    self.assertIn('Billing export daily cost query failed: BigQuery HTTP 403', joined)

  def test_ge_daily_usage_forwards_billing_export_table_and_hint(self) -> None:
    cost_payload = billing_export.daily_cost_not_connected(
        'BigQuery HTTP 404: Not found', status='ERROR', table='example-project.billing.gcp_billing_export_v1_123'
    )
    fake_self = types.SimpleNamespace(
        billing_export=types.SimpleNamespace(get_daily_ai_costs=lambda non_blocking=True: cost_payload),
        gcp_telemetry=types.SimpleNamespace(project_id='example-project'),
    )
    bq_insights = {
        'ge_mart_dataset': 'vibelift_mart',
        'ge_curated_dataset': 'ds_ge_curated_staging',
        'ge_mart_refreshed_at': '2026-10-06T00:00:00Z',
        'ge_daily_totals': [{'day': '2026-10-05', 'interactions': 4, 'total_tokens': 1000}],
    }
    out = server.VibeLiftRuntimeController._ge_daily_usage(fake_self, bq_insights)  # type: ignore[arg-type]
    assert out is not None
    self.assertEqual(out['billing_table'], 'example-project.billing.gcp_billing_export_v1_123')
    self.assertEqual(out['billing_status'], 'ERROR')
    self.assertEqual(out['billing_error_kind'], 'not_found')
    self.assertIn('not found', out['billing_hint'].lower())


class PerUserEstimatedCostTest(unittest.TestCase):

  def test_sql_builder_and_mapper(self) -> None:
    sql = ge_mart.build_user_model_tokens_sql('example-project', hours=720)
    self.assertIn('FROM `example-project.vibelift_mart.fct_turns`', sql)
    self.assertIn('INTERVAL 720 HOUR', sql)
    self.assertIn('GROUP BY user_email, model_name, engine_key', sql)
    self.assertNotIn('GROUP BY user_email, engine_key', sql)
    with self.assertRaises(ValueError):
      ge_mart.build_user_model_tokens_sql("bad'; DROP TABLE x; --")

    mapped = ge_mart.user_model_tokens_from_row({
        'user_email': 'alice@example.com',
        'model_name': None,
        'engine_key': 'global/app-1',
        'turns': '5',
        'input_tokens': '1000',
        'output_tokens': None,
        'cached_input_tokens': '200',
        'reasoning_tokens': None,
        'total_tokens': '1000',
    })
    self.assertEqual(mapped['user_email'], 'alice@example.com')
    self.assertIsNone(mapped['model_name'])
    self.assertEqual(mapped['turns'], 5)
    self.assertEqual(mapped['input_tokens'], 1000)
    self.assertIsNone(mapped['output_tokens'])

  def test_estimate_user_costs_math_and_immutability(self) -> None:
    users = [
        {'user_ldap': 'alice', 'user_email': 'alice@example.com', 'monthly_cost_usd': None},
        {'user_ldap': 'bob', 'user_email': 'bob@example.com', 'monthly_cost_usd': None},
        {'user_ldap': 'carol', 'user_email': 'carol@example.com', 'monthly_cost_usd': None},
    ]
    users_snapshot = copy.deepcopy(users)
    model_rows = [
        # alice on gemini-2.5-pro: in=1M (400k cached -> 600k uncached * 1.25 = 0.75; 400k * 0.125 = 0.05) + out=100k * 10.0 = 1.00 -> $1.80
        {'user_email': 'alice@example.com', 'model_name': 'gemini-2.5-pro', 'engine_key': 'global/app1',
         'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cached_input_tokens': 400_000},
        # alice on publishers/google/models/gemini-3.5-flash in us/app2: in=2M * 1.50 = $3.00
        {'user_email': 'alice@example.com', 'model_name': 'publishers/google/models/gemini-3.5-flash',
         'engine_key': 'us/app2', 'input_tokens': 2_000_000, 'output_tokens': 0, 'cached_input_tokens': 0},
        # alice also has unlogged model tokens (not priced, counted in unpriced_tokens)
        {'user_email': 'alice@example.com', 'model_name': None, 'engine_key': 'global/app1',
         'input_tokens': 50_000, 'output_tokens': 134, 'cached_input_tokens': 10_000},
        # bob has only unknown model tokens -> est_cost_usd stays None
        {'user_email': 'bob@example.com', 'model_name': 'custom-unknown-v1', 'engine_key': 'global/app1',
         'input_tokens': 10_000, 'output_tokens': 500, 'cached_input_tokens': 0},
        # all-None token row is ignored
        {'user_email': 'carol@example.com', 'model_name': 'gemini-2.5-flash', 'engine_key': 'global/app1',
         'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': None},
    ]
    out_users, summary = finops.estimate_user_costs(users, model_rows, _CARDS)
    self.assertEqual(users, users_snapshot, 'input user dicts must not be mutated')

    by_ldap = {u['user_ldap']: u for u in out_users}
    self.assertAlmostEqual(by_ldap['alice']['est_cost_usd'], 4.80, places=4)
    self.assertEqual(by_ldap['alice']['est_cost_by_engine'], {'global/app1': 1.8, 'us/app2': 3.0})
    self.assertEqual(by_ldap['alice']['est_cost_models'], ['gemini-2.5-pro', 'gemini-3.5-flash'])
    self.assertEqual(by_ldap['alice']['est_cost_unpriced_tokens'], 50_134)
    self.assertIsNone(by_ldap['alice']['monthly_cost_usd'])

    self.assertIsNone(by_ldap['bob']['est_cost_usd'])
    self.assertEqual(by_ldap['bob']['est_cost_unpriced_tokens'], 10_500)
    self.assertIsNone(by_ldap['carol']['est_cost_usd'])
    self.assertEqual(by_ldap['carol']['est_cost_unpriced_tokens'], 0)

    self.assertEqual(summary['status'], 'LIVE')
    self.assertEqual(summary['priced_users'], 1)
    self.assertAlmostEqual(summary['total_est_cost_usd'], 4.80, places=4)
    self.assertEqual(summary['models_priced'], ['gemini-2.5-pro', 'gemini-3.5-flash'])
    self.assertEqual(summary['models_without_rate_card'], ['(model not logged)', 'custom-unknown-v1'])
    self.assertEqual(summary['unpriced_tokens'], 60_634)

    # Cached > input clamps to input.
    clamped = finops.tokens_cost_usd(1000, 0, 5000, _CARDS['gemini-2.5-pro'])
    self.assertAlmostEqual(clamped, 1000 * 0.125 / 1e6, places=8)

    # Parity with fleet._estimate_token_cost_usd for a known model.
    fleet_est = fleet._estimate_token_cost_usd(1_000_000, 100_000, 400_000, ['gemini-2.5-pro'], _CARDS)
    self.assertEqual(round(finops.tokens_cost_usd(1_000_000, 100_000, 400_000, _CARDS['gemini-2.5-pro']), 4), fleet_est)

    # Empty rows -> NO_DATA.
    _, empty_summary = finops.estimate_user_costs([], [], _CARDS)
    self.assertEqual(empty_summary['status'], 'NO_DATA')
    self.assertIsNone(empty_summary['total_est_cost_usd'])

  def test_live_controller_populates_user_cost_estimate(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    bq = {
        'ge_mart_dataset': 'vibelift_mart',
        'window_hours': 168,
        'power_users_ldap': [
            {'user_ldap': 'enriq', 'user_email': 'enriq@example.com', 'department': 'Eng',
             'primary_agent': 'ge', 'sessions_7d': 2, 'monthly_cost_usd': None,
             'source_tables': ['vibelift_mart'], 'status': 'LIVE_HUMAN_PRINCIPAL',
             'anomaly_status': 'LIVE_HUMAN_PRINCIPAL', 'by_engine': {'global/app1': 2}},
        ],
        'ge_user_model_tokens': [
            {'user_email': 'enriq@example.com', 'model_name': 'gemini-3.5-flash', 'engine_key': 'global/app1',
             'turns': 10, 'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cached_input_tokens': 0,
             'reasoning_tokens': None, 'total_tokens': 1_100_000},
        ],
    }
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_live_bigquery_project_insights', return_value=bq), \
         mock.patch.object(ctrl.gcp_telemetry, 'list_cloud_run_agent_services', return_value=[]), \
         mock.patch.object(ctrl.gcp_telemetry, 'fetch_gemini_enterprise_support_telemetry', return_value=[]), \
         mock.patch.object(ctrl.billing_export, 'get', return_value=billing_export.not_connected_payload()), \
         mock.patch.object(ctrl.billing_export, 'get_daily_ai_costs', return_value=billing_export.daily_cost_not_connected()):
      state = ctrl.get_state_payload(include_fleet=False, window_hours=168)
    uc = state['user_centric']
    user = uc['power_users_ldap'][0]
    self.assertAlmostEqual(user['est_cost_usd'], 2.40, places=4)
    self.assertIsNone(user['monthly_cost_usd'])
    self.assertEqual(uc['user_cost_estimate']['window_hours'], 168)
    self.assertEqual(uc['user_cost_estimate']['status'], 'LIVE')

  def test_ui_template_markers_and_no_inner_html(self) -> None:
    html = ui_template.render_dashboard_html()
    for marker in (
        'id="billingStatusPanel"',
        'id="billingStatusBadge"',
        'id="powerUsersCostNote"',
        'function renderBillingStatus(',
        'function estCostCell(',
        'function renderUserCostNote(',
        'Est. Cost (list price)',
        'd.reasoning_tokens',
        'd.cached_input_tokens',
    ):
      self.assertIn(marker, html)
    self.assertNotIn('d.thought_tokens', html)
    # XSS guard: renderBillingStatus must not use innerHTML.
    start = html.index('function renderBillingStatus')
    end = html.index('function renderGeMartDaily')
    self.assertNotIn('innerHTML', html[start:end])


if __name__ == '__main__':
  unittest.main()
