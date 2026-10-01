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
      reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
      out = reader.get()
    self.assertEqual(out['status'], 'NOT_CONNECTED')
    self.assertEqual(out['sku_ledger'], [])
    self.assertIsNone(out['total_net_invoice_usd'])
    self.assertIsNone(out['reconciliation_delta_pct'])

  def test_sql_rejects_injection(self):
    with self.assertRaises(ValueError):
      billing_export.build_billing_sql('p-roject.ds.t`; DROP TABLE x; --', 'example-project')
    with self.assertRaises(ValueError):
      billing_export.build_billing_sql('proj-ect.ds.tbl', "x' OR 1=1 --")
    sql = billing_export.build_billing_sql('billing-proj.billing.gcp_billing_export_v1_ABC', 'example-project')
    self.assertIn("project.id = 'example-project'", sql)

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
    state = {'gcp_project': 'example-project'}
    with mock.patch.object(telemetry_validator, '_get_access_token', return_value=(None, 'ADC: no creds')):
      out = telemetry_validator.run_llm_as_judge_audit(state, self._checks(fail=True))
    self.assertEqual(out['judge_error'], 'ADC: no creds')
    self.assertEqual(out['verdict'], 'FLAGGED_ISSUES')
    self.assertIn('B', out['executive_finding'])
    self.assertNotIn('All observed', out['executive_finding'])

  def test_llm_cannot_override_failed_check(self):
    state = {'gcp_project': 'example-project'}
    with mock.patch.object(telemetry_validator, '_get_access_token', return_value=('tok', None)), \
         mock.patch.object(telemetry_validator, '_call_vertex_judge',
                           return_value=({'verdict': 'VERIFIED_GROUNDED', 'grounding_score_100': 97,
                                          'executive_finding': 'ok'}, None)):
      out = telemetry_validator.run_llm_as_judge_audit(state, self._checks(fail=True))
    self.assertEqual(out['verdict'], 'FLAGGED_ISSUES')
    self.assertEqual(out['grounding_score_100'], 97)
    self.assertIsNone(out['judge_error'])

  def test_missing_score_is_not_invented(self):
    state = {'gcp_project': 'example-project'}
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


class BlindspotRemediationTest(unittest.TestCase):

  def test_cloud_run_services_never_invent_cpu_memory_or_cost(self):
    from vibelift import gcp_telemetry
    svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='example-project', region='us-central1')
    with mock.patch.object(svc, '_get_access_token', return_value=None), \
         mock.patch.object(svc, '_query_bigquery_rest', return_value=[
             {'service_name': 'vibe-lift-agent', 'latest_rev': 'vibe-lift-agent-00099',
              'req_count': 42, 'avg_latency_ms': 112.4, 'p95_latency_ms': 245.8}
         ]):
      rows = svc.list_cloud_run_agent_services(force_refresh=True)
    self.assertGreaterEqual(len(rows), 1)
    row = rows[0]
    self.assertIsNone(row['cpu_utilization_pct'])
    self.assertIsNone(row['memory_utilization_pct'])
    self.assertIsNone(row['monthly_cost_usd'])
    self.assertEqual(row['p95_latency_ms'], 245.8)
    self.assertEqual(row['active_revision'], 'vibe-lift-agent-00099')

  def test_unconfigured_project_never_crashes_summary_or_insights(self):
    from vibelift import gcp_telemetry
    svc = gcp_telemetry.GoogleCloudTelemetryService(
        project_id=gcp_telemetry.UNCONFIGURED_PROJECT_ID,
        region='us-central1',
    )
    with mock.patch.object(svc, '_get_access_token', return_value='tok'), \
         mock.patch.object(svc, '_query_bigquery_rest', return_value=[]), \
         mock.patch.object(svc, 'fetch_live_cloud_turns', return_value=[]):
      summary = svc.get_telemetry_summary_payload()
      insights = svc.fetch_live_bigquery_project_insights(force_refresh=True)
    self.assertEqual(summary['project_id'], gcp_telemetry.UNCONFIGURED_PROJECT_ID)
    self.assertIn('vibelift_mart', summary['bigquery_datasets'])
    self.assertIsNone(insights)

  def test_mart_tool_names_extracted_into_live_skills_and_decorator_events(self):
    from vibelift import gcp_telemetry
    svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='example-project', region='us-central1')
    def fake_bq(sql, timeout_s=7.0):
      if 'tool_names' in sql and 'fct_turns' in sql and 'RECENT_SESSIONS' not in sql.upper():
        return [{
            'turn_id': 't-1',
            'turn_source': 'INFERENCE',
            'turn_kind': 'AGENT_CALL',
            'ts': '2026-09-30T10:00:00Z',
            'session_id': 's-1',
            'user_email': 'user-a@example.com',
            'engine_key': 'global/app-1',
            'agent_name': 'sre-agent',
            'model_name': 'gemini-2.5-flash',
            'api_method': 'StreamAssist',
            'turn_status': 'SUCCESS',
            'status_message': None,
            'input_tokens': '1200',
            'output_tokens': '300',
            'cached_input_tokens': '800',
            'reasoning_tokens': '150',
            'total_tokens': '1650',
            'llm_calls': '1',
            'tool_call_count': '2',
            'tool_names': 'fetch_logs, run_diagnostics',
        }]
      return []

    with mock.patch.object(svc, '_get_access_token', return_value='tok'), \
         mock.patch.object(svc, '_query_bigquery_rest', side_effect=fake_bq):
      insights = svc.fetch_live_bigquery_project_insights(force_refresh=True)
    self.assertIsNotNone(insights)
    skill_names = [s['resource_name'] for s in insights['live_skills_mcp']]
    self.assertIn('ge_tool://fetch_logs', skill_names)
    self.assertIn('ge_tool://run_diagnostics', skill_names)
    handlers = [d['handler_name'] for d in insights['live_decorator_events']]
    self.assertIn('fetch_logs', handlers)

  def test_reasoning_tokens_priced_and_decomposed_in_finops(self):
    from vibelift import finops as live_finops
    cards = {'gemini-2.5-flash': {'input': 0.30, 'output': 2.50, 'cached_read': 0.03, 'cache_write': 0.0}}
    prev_row = {'model': 'gemini-2.5-flash', 'input_tokens': 1_000_000, 'output_tokens': 100_000,
                'reasoning_tokens': 50_000, 'cache_read_tokens': 0, 'cache_write_tokens': 0, 'invocations': 100}
    cur_row = {'model': 'gemini-2.5-flash', 'input_tokens': 1_200_000, 'output_tokens': 120_000,
               'reasoning_tokens': 200_000, 'cache_read_tokens': 200_000, 'cache_write_tokens': 0, 'invocations': 120}
    prev_row['est_cost_usd'] = round(live_finops._cost(prev_row, cards['gemini-2.5-flash']), 4)
    cur_row['est_cost_usd'] = round(live_finops._cost(cur_row, cards['gemini-2.5-flash']), 4)
    prev = {'models': [prev_row], 'totals': {'est_cost_usd': prev_row['est_cost_usd'], 'invocations': 100,
            'input_tokens': 1_000_000, 'output_tokens': 100_000, 'reasoning_tokens': 50_000, 'cache_read_tokens': 0},
            'rate_cards': cards, 'interval': {'start': 's0', 'end': 'e0'}}
    cur = {'models': [cur_row], 'totals': {'est_cost_usd': cur_row['est_cost_usd'], 'invocations': 120,
           'input_tokens': 1_200_000, 'output_tokens': 120_000, 'reasoning_tokens': 200_000, 'cache_read_tokens': 200_000},
           'rate_cards': cards, 'interval': {'start': 's1', 'end': 'e1'}}
    drift = live_finops.build_spend_drift(cur, prev, cards)
    self.assertEqual(drift['status'], 'LIVE')
    self.assertAlmostEqual(drift['unexplained_usd'], 0.0, places=6)
    te = live_finops.build_token_economics({'model_usage': cur, 'agents': [], 'window_hours': 24})
    self.assertEqual(te['totals']['reasoning_tokens'], 200_000)
    self.assertEqual(te['models'][0]['reasoning_tokens'], 200_000)

  def test_nl2sql_uses_dynamic_project_and_vibelift_mart_tables(self):
    opt = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    for q in (
        'Which agents have runaway thinking tokens?',
        'Show session duration from fct_sessions',
        'Who are the top power users by ldap?',
        'Show 5-layer otel span metrics',
        'Compare cost per 1k turns across agents',
    ):
      out = opt.execute_nl2sql_telemetry_query(q, project_id='custom-gcp-proj')
      self.assertIn('custom-gcp-proj.vibelift_mart', out['generated_sql'])
      self.assertNotIn('aive_logs.', out['generated_sql'])

  def test_structured_audit_sink_and_sme_state_dir_persistence(self):
    import tempfile

    from vibelift import sme_eval, telemetry
    with tempfile.TemporaryDirectory() as tmpdir:
      with mock.patch.dict(os.environ, {'VIBELIFT_STATE_DIR': tmpdir}):
        store1 = sme_eval.SmeEvaluationStore()
        store1.submit_rating(persona_id='finops_lead', reviewer_ldap='user-a', overall_rating=5, notes='Persisted')
        telemetry.log_csat_rating('sess-1', 'evt-1', 'user-a@example.com', 5, 'Great')
        # New store instance should reload the persisted SME rating from VIBELIFT_STATE_DIR
        store2 = sme_eval.SmeEvaluationStore()
        ratings = store2.list_ratings('finops_lead')
        self.assertEqual(len(ratings), 1)
        self.assertEqual(ratings[0]['reviewer_ldap'], 'user-a')
        self.assertTrue(os.path.isfile(os.path.join(tmpdir, 'csat_rating.jsonl')))
        store2.clear()
        self.assertEqual(len(store2.list_ratings()), 0)

  def test_server_reset_restores_default_agent_and_cors_env(self):
    from vibelift import server
    ctrl = server.VibeLiftRuntimeController()
    ctrl.select_agent('deep_research')
    self.assertEqual(ctrl.optimizer.selected_agent_id, 'deep_research')
    ctrl.reset()
    self.assertEqual(ctrl.optimizer.selected_agent_id, 'it_service_desk')
    with mock.patch.dict(os.environ, {'ALLOWED_ORIGINS': 'https://a.example.com, https://b.example.com'}):
      self.assertEqual(server.resolve_allowed_origins(), ['https://a.example.com', 'https://b.example.com'])


if __name__ == '__main__':
  unittest.main()

