"""Tests for the no-invented-numbers paths: billing export, LLM judge fallback, live cohorts/alerts, trend."""

import os
import unittest
from unittest import mock

from vibelift import billing_export
from vibelift import fleet as ge_fleet
from vibelift import optimizer as alpha_evolve_optimizer
from vibelift import validator as telemetry_validator


class BillingExportTest(unittest.TestCase):

  def test_not_connected_without_table(self):
    with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: ''}):
      reader = billing_export.BillingExportReader('project-maui', lambda: 'tok')
      out = reader.get()
    self.assertEqual(out['status'], 'NOT_CONNECTED')
    self.assertEqual(out['sku_ledger'], [])
    self.assertIsNone(out['total_net_invoice_usd'])
    self.assertIsNone(out['reconciliation_delta_pct'])

  def test_sql_rejects_injection(self):
    with self.assertRaises(ValueError):
      billing_export.build_billing_sql('p-roject.ds.t`; DROP TABLE x; --', 'project-maui')
    with self.assertRaises(ValueError):
      billing_export.build_billing_sql('proj-ect.ds.tbl', "x' OR 1=1 --")
    sql = billing_export.build_billing_sql('billing-proj.billing.gcp_billing_export_v1_ABC', 'project-maui')
    self.assertIn("project.id = 'project-maui'", sql)

  def test_summarize_rows_totals(self):
    rows = [
        {'service': 'Vertex AI', 'sku_id': 'A', 'sku_description': 'Gemini input', 'gross_usd': '10.5',
         'credits_usd': '-2.5', 'usage_amount': '1000', 'usage_unit': 'count', 'currency': 'USD'},
        {'service': 'Cloud Run', 'sku_id': 'B', 'sku_description': 'CPU', 'gross_usd': '3',
         'credits_usd': '0', 'usage_amount': None, 'usage_unit': None, 'currency': 'USD'},
    ]
    out = billing_export.summarize_rows(rows, 'a-proj.b.c')
    self.assertEqual(out['status'], 'LIVE')
    self.assertEqual(out['total_gross_usd'], 13.5)
    self.assertEqual(out['total_credits_usd'], -2.5)
    self.assertEqual(out['total_net_invoice_usd'], 11.0)
    self.assertEqual(out['net_by_service'], {'Vertex AI': 8.0, 'Cloud Run': 3.0})
    self.assertIsNone(out['sku_ledger'][0]['telemetry_estimated_usd'])
    self.assertIsNone(out['sku_ledger'][0]['variance_pct'])


class JudgeFallbackTest(unittest.TestCase):

  def _checks(self, fail=False):
    return [
        {'check_id': 'A', 'tab': 'T', 'status': 'PASS', 'evidence': ''},
        {'check_id': 'B', 'tab': 'T', 'status': 'FAIL' if fail else 'PASS', 'evidence': ''},
    ]

  def test_fallback_reports_error_and_failures(self):
    state = {'gcp_project': 'project-maui'}
    with mock.patch.object(telemetry_validator, '_get_access_token', return_value=(None, 'ADC: no creds')):
      out = telemetry_validator.run_llm_as_judge_audit(state, self._checks(fail=True))
    self.assertEqual(out['judge_error'], 'ADC: no creds')
    self.assertEqual(out['verdict'], 'FLAGGED_ISSUES')
    self.assertIn('B', out['executive_finding'])
    self.assertNotIn('All observed', out['executive_finding'])

  def test_llm_cannot_override_failed_check(self):
    state = {'gcp_project': 'project-maui'}
    with mock.patch.object(telemetry_validator, '_get_access_token', return_value=('tok', None)), \
         mock.patch.object(telemetry_validator, '_call_vertex_judge',
                           return_value=({'verdict': 'VERIFIED_GROUNDED', 'grounding_score_100': 97,
                                          'executive_finding': 'ok'}, None)):
      out = telemetry_validator.run_llm_as_judge_audit(state, self._checks(fail=True))
    self.assertEqual(out['verdict'], 'FLAGGED_ISSUES')
    self.assertEqual(out['grounding_score_100'], 97)
    self.assertIsNone(out['judge_error'])

  def test_missing_score_is_not_invented(self):
    state = {'gcp_project': 'project-maui'}
    with mock.patch.object(telemetry_validator, '_get_access_token', return_value=('tok', None)), \
         mock.patch.object(telemetry_validator, '_call_vertex_judge',
                           return_value=({'verdict': 'VERIFIED_GROUNDED', 'executive_finding': 'x'}, None)):
      out = telemetry_validator.run_llm_as_judge_audit(state, self._checks())
    self.assertIsNone(out['grounding_score_100'])


class LiveUserAnalyticsTest(unittest.TestCase):

  def _fleet(self):
    return {
        'window_hours': 24,
        'engines': [{'engine_key': 'global/app-a', 'display_name': 'App A'},
                    {'engine_key': 'us/app-b', 'display_name': 'App B'}],
        'agents': [
            {'agent_id': '1', 'display_name': 'Svc', 'backend': {'kind': 'cloud_run', 'service': 'svc'},
             'metrics': {'requests': 100, 'errors_4xx': 40, 'errors_5xx': 2}},
            {'agent_id': '2', 'display_name': 'Svc again', 'backend': {'kind': 'cloud_run', 'service': 'svc'},
             'metrics': {'requests': 100, 'errors_4xx': 40, 'errors_5xx': 2}},
            {'agent_id': '3', 'display_name': 'Quiet', 'backend': {'kind': 'cloud_run', 'service': 'quiet'},
             'metrics': {'requests': 5, 'errors_4xx': 5, 'errors_5xx': 0}},
        ],
        'model_usage': {'models': [{'model': 'm1', 'input_tokens': 60000, 'cache_read_tokens': 0, 'est_cost_usd': 1.2}]},
    }

  def test_live_payload_has_no_modelled_savings(self):
    live_bq = {'power_users_ldap': [
        {'user_ldap': 'a', 'status': 'LIVE_HUMAN_PRINCIPAL', 'sessions_7d': 10, 'by_engine': {'global/app-a': 7, 'us/app-b': 3}},
        {'user_ldap': 'sa', 'status': 'SERVICE_ACCOUNT_TELEMETRY', 'sessions_7d': 50, 'by_engine': {'global/app-a': 50}},
    ]}
    uc = alpha_evolve_optimizer.build_user_centric_analytics(None, live_fleet=self._fleet(), live_bq=live_bq)
    self.assertTrue(uc['live'])
    self.assertIsNone(uc['total_monthly_savings_usd'])
    self.assertIsNone(uc['annualized_savings_usd'])
    self.assertIsNone(uc['supported_dau_capacity'])
    self.assertEqual(uc['active_people_7d'], 1)
    self.assertEqual(uc['active_service_accounts_7d'], 1)
    cohorts = {c['engine_key']: c for c in uc['cohorts']}
    self.assertEqual(cohorts['global/app-a']['cohort'], 'App A')
    self.assertEqual(cohorts['global/app-a']['people'], 1)
    self.assertEqual(cohorts['global/app-a']['service_accounts'], 1)
    self.assertEqual(cohorts['global/app-a']['sessions_7d'], 57)
    self.assertEqual(cohorts['us/app-b']['primary_agent'], 'US')

  def test_token_breakdown_uses_one_source(self):
    fleet = dict(self._fleet(), totals={'input_tokens': 1000, 'output_tokens': 50, 'cached_tokens': 400})
    users = [{'user_ldap': 'a', 'status': 'LIVE_HUMAN_PRINCIPAL', 'sessions_7d': 1}]
    uc = alpha_evolve_optimizer.build_user_centric_analytics(None, live_fleet=fleet, live_bq={'power_users_ldap': users})
    tcb = uc['token_category_breakdown']
    self.assertEqual((tcb['observed_gcp_prompt_tokens'], tcb['observed_gcp_output_tokens'],
                      tcb['observed_gcp_cached_tokens'], tcb['observed_gcp_cache_hit_pct']), (1000, 50, 400, 40.0))
    # A BigQuery prompt total without a cached total must not borrow the fleet's cached count.
    bq = {'power_users_ldap': users, 'otel_total_prompt_tokens': 5000, 'otel_total_output_tokens': 70}
    tcb = alpha_evolve_optimizer.build_user_centric_analytics(None, live_fleet=fleet, live_bq=bq)['token_category_breakdown']
    self.assertEqual((tcb['observed_gcp_prompt_tokens'], tcb['observed_gcp_output_tokens'],
                      tcb['observed_gcp_cached_tokens'], tcb['observed_gcp_cache_hit_pct']), (5000, 70, None, None))
    self.assertNotIn('foreground_prompt_tokens_m', tcb)   # no modelled demo figures in live payloads
    self.assertNotIn('thinking_reduction_pct', tcb)
    # No fleet payload and no BigQuery totals: unmeasured stays None instead of a fabricated 0.
    tcb = alpha_evolve_optimizer.build_user_centric_analytics(
        None, live_fleet=None, live_bq={'power_users_ldap': users})['token_category_breakdown']
    self.assertIsNone(tcb['observed_gcp_prompt_tokens'])
    self.assertIsNone(tcb['observed_gcp_cache_hit_pct'])

  def test_alerts_derived_from_fleet_once_per_runtime(self):
    alerts = alpha_evolve_optimizer.build_live_runaway_alerts(self._fleet())
    kinds = [a['alert_id'].split('-')[0] for a in alerts]
    self.assertEqual(kinds.count('5XX'), 1)   # shared runtime counted once
    self.assertEqual(kinds.count('4XX'), 1)   # 'quiet' has < 10 requests
    self.assertEqual(kinds.count('CACHE'), 1)
    self.assertIn('2 of 100', alerts[0]['observed'])


class LivePayloadBuildersTest(unittest.TestCase):

  def test_all_server_payload_builders_accept_live_user_data(self):
    opt = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    opt._live_bq_insights = {'power_users_ldap': [
        {'user_ldap': 'a', 'status': 'LIVE_HUMAN_PRINCIPAL', 'sessions_7d': 3, 'by_engine': {'global/x': 3}},
    ]}
    opt._live_fleet_payload = {'agents': [], 'engines': [], 'totals': {}}
    uc = opt.get_user_centric_payload()
    self.assertTrue(uc['live'])
    # Every builder server.get_state_payload() calls must cope with None savings fields.
    opt.get_optimizer_platforms_payload()
    opt.get_otel_catalog_payload()
    opt.get_finops_billing_reconciliation_payload()
    opt.get_tokenomics_and_cockpit_finops_payload()
    opt.get_sme_persona_playbooks()
    opt.simulate_what_if_scenario()
    res = opt.execute_nl2sql_telemetry_query('Compare cost per 1k turns and prompt cache savings across agents')
    self.assertIn('Simulator', res['executive_summary'])


class RequestTrendTest(unittest.TestCase):

  def test_trend_aligns_and_dedupes(self):
    agents = [
        {'agent_id': '1', 'backend': {'kind': 'cloud_run', 'service': 'svc', 'project': 'p'}},
        {'agent_id': '2', 'backend': {'kind': 'cloud_run', 'service': 'svc', 'project': 'p'}},
    ]
    import datetime
    fixed = datetime.datetime(2026, 9, 29, 12, 30, tzinfo=datetime.UTC)
    with mock.patch.object(ge_fleet, '_utcnow', return_value=fixed):
      end_last = int(datetime.datetime(2026, 9, 29, 13, 0, tzinfo=datetime.UTC).timestamp())
      results = {('run_series', 'p'): {'svc': {
          end_last: {'requests': 5, 'errors_4xx': 1, 'errors_5xx': 0},
          end_last - 3600: {'requests': 3, 'errors_4xx': 0, 'errors_5xx': 1},
          # Raw per-minute point inside the oldest partial hour (window starts 12:30 yesterday).
          end_last - 24 * 3600 - 600: {'requests': 2, 'errors_4xx': 0, 'errors_5xx': 0},
          # Exactly at the window start: outside the window, must be ignored.
          end_last - 24 * 3600 - 1800: {'requests': 100, 'errors_4xx': 0, 'errors_5xx': 0},
      }}}
      tr = ge_fleet.build_request_trend(agents, results, 24 * 3600, 3600, 'p')
    # 25 buckets: the first (12:00-13:00 yesterday) holds the window's first partial half hour.
    self.assertEqual(len(tr['bucket_ends']), 25)
    self.assertEqual(tr['bucket_ends'][0], '2026-09-28T13:00:00Z')
    self.assertEqual(tr['bucket_ends'][-1], '2026-09-29T13:00:00Z')
    row = tr['by_runtime']['cloud_run:svc']
    self.assertEqual(list(tr['by_runtime']), ['cloud_run:svc'])
    self.assertEqual(row['requests'][-1], 5)
    self.assertEqual(row['requests'][-2], 3)
    self.assertEqual(row['requests'][0], 2)
    self.assertEqual(sum(row['requests']), 10)
    self.assertEqual(tr['status'], 'ok')

  def test_bucket_sizes(self):
    self.assertEqual(ge_fleet.trend_bucket_seconds(1), 300)
    self.assertEqual(ge_fleet.trend_bucket_seconds(24), 3600)
    self.assertEqual(ge_fleet.trend_bucket_seconds(168), 6 * 3600)


if __name__ == '__main__':
  unittest.main()
