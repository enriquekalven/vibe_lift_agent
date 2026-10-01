"""Tests for the VibeLift Multi-Persona SME Evaluation & Rating Framework (vibelift/sme_eval.py)."""

from __future__ import annotations

import unittest

from tests import test_fleet as ge_fleet_test
from vibelift import fleet as ge_fleet
from vibelift import server, sme_eval
from vibelift import validator as telemetry_validator
from vibelift.ui import template as ui_template

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()


def setUpModule() -> None:
  server._global_controller.ge_fleet = _FAKE_FLEET
  ge_fleet._SERVICE = _FAKE_FLEET


class SmeEvalFrameworkTest(unittest.TestCase):
  """Verifies the 30-check (6 personas x 5 dimensions) rubric and human SME rating store."""

  def setUp(self) -> None:
    super().setUp()
    sme_eval.get_sme_evaluation_store().clear()

  def tearDown(self) -> None:
    sme_eval.get_sme_evaluation_store().clear()
    super().tearDown()

  def test_simulator_mode_passes_all_30_persona_rubric_checks(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    state = ctrl.get_state_payload(include_fleet=True)
    ev = state.get('sme_evaluation')
    self.assertIsInstance(ev, dict)
    self.assertEqual(
        ev['overall_compliance_status'],
        'COMPLIANT',
        f"Failed checks: {[c for c in ev['checks'] if not c['passed']]}",
    )
    self.assertEqual(ev['total_checks'], 30)
    self.assertEqual(ev['passed_checks'], 30)
    self.assertEqual(ev['composite_automated_score_100'], 100.0)
    # Honest zero-fabrication: no SME ratings submitted yet
    self.assertEqual(ev['sme_total_ratings_submitted'], 0)
    self.assertIsNone(ev['sme_overall_avg_rating_5'])
    for p in ev['personas']:
      self.assertEqual(p['automated_score_100'], 100)
      self.assertEqual(p['compliance_status'], 'COMPLIANT')
      self.assertEqual(p['sme_rating_count'], 0)
      self.assertIsNone(p['avg_sme_rating_5'])
      self.assertIsNone(p['sme_task_completion_pct'])

  def test_live_mode_passes_all_30_persona_rubric_checks(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    base = ctrl.get_state_payload(include_fleet=True)
    live_fleet = dict(base['ge_fleet'])
    live_fleet['project_id'] = 'example-project'
    live_fleet['live_finops'] = ctrl._live_finops(live_fleet)

    bq_insights = {
        'ge_mart_dataset': 'vibelift_mart',
        'ge_curated_dataset': 'ds_ge_curated_staging',
        'ge_mart_refreshed_at': '2026-09-29T22:00:00Z',
        'ge_daily_totals': [
            {
                'event_date': '2026-09-29',
                'turns': 30,
                'sessions': 8,
                'distinct_users': 4,
                'input_tokens': 14200,
                'output_tokens': 1900,
                'cached_tokens': 4100,
                'thoughts_tokens': 300,
                'total_tokens': 16400,
                'error_turns': 0,
                'p95_latency_ms': 620.0,
            }
        ],
        'ge_daily_by_app': [
            {
                'event_date': '2026-09-29',
                'engine_id': 'gemini-enterprise',
                'agent_id': '101',
                'model_name': 'gemini-2.5-flash',
                'turns': 30,
                'sessions': 8,
                'distinct_users': 4,
                'total_tokens': 16400,
            }
        ],
        'ge_sessions': [
            {
                'session_id': 'sess-001',
                'engine_id': 'gemini-enterprise',
                'user_pseudo_id': 'user-a@example.com',
                'turns': 5,
                'total_tokens': 4200,
            }
        ],
    }
    live_state = dict(base)
    live_state['gcp_project'] = 'example-project'
    live_state['live_data'] = True
    live_state['ge_fleet'] = live_fleet
    live_state['tokenomics_cockpit'] = None
    live_state['what_if_default'] = None
    live_state['live_finops'] = live_fleet['live_finops']
    live_state['ge_daily_usage'] = {
        'source': 'vibelift_mart.fct_turns + Cloud Billing export',
        'mart_dataset': 'vibelift_mart',
        'curated_dataset': 'ds_ge_curated_staging',
        'refreshed_at': '2026-09-29T22:00:00Z',
        'refresh_cli': 'python3 deploy/bigquery/provision_ge_mart.py --project example-project --refresh',
        'billing_status': 'NOT_CONNECTED',
        'days': [
            {
                **bq_insights['ge_daily_totals'][0],
                'ai_net_usd': None,
                'cost_per_turn_usd': None,
                'cost_per_1k_tokens_usd': None,
            }
        ],
        'by_app_agent_model': bq_insights['ge_daily_by_app'],
        'sessions': bq_insights['ge_sessions'],
    }
    live_state['billing_reconciliation'] = {
        'source': 'Cloud Billing BigQuery standard usage export',
        'status': 'NOT_CONNECTED',
    }
    live_state['user_centric'] = {
        'collection_mode': 'LIVE GCP TELEMETRY (example-project BigQuery vibelift_mart)',
        'source': 'BigQuery vibelift_mart (fct_turns, fct_sessions, agg_daily_usage)',
        'power_users_ldap': [
            {
                'user_ldap': 'user-a@example.com',
                'status': 'LIVE_HUMAN_PRINCIPAL',
                'source_tables': ['vibelift_mart.fct_turns'],
                'department': 'GCP Principal [vibelift_mart.fct_turns]',
                'primary_agent': 'gemini-enterprise',
                'turns_30d': 18,
                'total_tokens_m': 0.0164,
                'monthly_cost_usd': None,
                'cache_hit_pct': 28.9,
                'csat_score': None,
                'optimization_recommendation': 'Observed 18 real events',
            }
        ],
        'cohorts': [
            {
                'cohort_name': 'gemini-enterprise',
                'active_users': 4,
                'turns_30d': 30,
                'baseline_monthly_usd': 0.0,
                'optimized_monthly_usd': 0.0,
                'monthly_savings_usd': 0.0,
                'cache_hit_pct': None,
                'p95_latency_ms': 620,
                'adoption_velocity_pct': None,
                'csat_score': None,
                'top_feature': 'DISCOVERYENGINE_STREAM_ASSIST (30 events)',
            }
        ],
        'ge_sessions': bq_insights['ge_sessions'],
        'total_monthly_savings_usd': None,
        'annualized_savings_usd': None,
    }
    live_state['aive_logs'] = {
        'usage_logs': [
            {
                'event_id': 'evt-live-1',
                'session_id': 'sess-001',
                'timestamp': '2026-09-29T21:55:00Z',
                'user_email': 'user-a@example.com',
                'task_type': 'STREAM_ASSIST',
                'prompts': [],
                'outputs': [{'gcs_uri': 'bq://example-project.vibelift_mart.fct_turns#evt-live-1'}],
                'total_tokens': 1200,
                'model_name': 'gemini-2.5-flash',
                'latency_ms': 510.0,
                'status': 'SUCCESS',
                'csat_rating': None,
            }
        ],
        'ratings_log': [],
    }
    live_state['telemetry_validation'] = telemetry_validator.validate_dashboard_state(
        live_state,
        ge_fleet_payload=live_fleet,
        bq_insights=bq_insights,
        run_llm_judge=False,
    )

    ev = sme_eval.evaluate_persona_rubric(
        live_state,
        ge_fleet_payload=live_fleet,
        rendered_html=ui_template.render_dashboard_html(),
    )
    self.assertEqual(ev['mode'], 'LIVE_GCP_TELEMETRY')
    self.assertEqual(
        ev['overall_compliance_status'],
        'COMPLIANT',
        f"Failed live checks: {[c for c in ev['checks'] if not c['passed']]}",
    )
    self.assertEqual(ev['passed_checks'], 30)
    self.assertEqual(ev['total_checks'], 30)
    self.assertEqual(ev['composite_automated_score_100'], 100.0)

  def test_sme_rating_submission_and_aggregation(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    res1 = ctrl.submit_sme_rating({
        'persona_id': 'finops_lead',
        'reviewer': 'finops-sme@example.com',
        'overall_rating': 5,
        'verdict': 'APPROVED',
        'task_completed': True,
        'notes': 'Daily usage mart + billing join is crystal clear.',
    })
    self.assertEqual(res1['status'], 'recorded')
    res2 = ctrl.submit_sme_rating({
        'persona_id': 'finops_lead',
        'reviewer': 'billing-auditor@example.com',
        'overall_rating': 4,
        'verdict': 'APPROVED_WITH_NOTES',
        'task_completed': True,
        'notes': 'Waiting on Billing export table env var.',
    })
    ev = res2['sme_evaluation']
    self.assertEqual(ev['sme_total_ratings_submitted'], 2)
    self.assertEqual(ev['sme_rated_personas_count'], 1)
    self.assertEqual(ev['sme_overall_avg_rating_5'], 4.5)

    finops_p = next(p for p in ev['personas'] if p['persona_id'] == 'finops_lead')
    self.assertEqual(finops_p['sme_rating_count'], 2)
    self.assertEqual(finops_p['avg_sme_rating_5'], 4.5)
    self.assertEqual(finops_p['sme_task_completion_pct'], 100.0)
    self.assertEqual(finops_p['latest_sme_verdict'], 'APPROVED_WITH_NOTES')

    # Unrated personas still report None (never fabricated)
    sre_p = next(p for p in ev['personas'] if p['persona_id'] == 'sre_platform')
    self.assertEqual(sre_p['sme_rating_count'], 0)
    self.assertIsNone(sre_p['avg_sme_rating_5'])

  def test_missing_dom_id_flags_persona_ergonomics_check(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    state = ctrl.get_state_payload(include_fleet=True)
    broken_html = ui_template.render_dashboard_html().replace('id="liveGeMartDailyPanel"', 'id="removedCard"')
    ev = sme_eval.evaluate_persona_rubric(state, rendered_html=broken_html)
    self.assertEqual(ev['overall_compliance_status'], 'FLAGGED')
    finops_p = next(p for p in ev['personas'] if p['persona_id'] == 'finops_lead')
    self.assertEqual(finops_p['automated_score_100'], 80)
    self.assertEqual(finops_p['compliance_status'], 'NEEDS_ATTENTION')

  def test_fastapi_sme_eval_routes(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    client = TestClient(fast_api_app.app)
    get_res = client.get('/api/sme_eval')
    self.assertEqual(get_res.status_code, 200)
    data = get_res.json()
    self.assertEqual(data['total_checks'], 30)
    self.assertEqual(data['passed_checks'], 30)

    run_res = client.post('/api/sme_eval/run', json={})
    self.assertEqual(run_res.status_code, 200)
    self.assertEqual(run_res.json()['overall_compliance_status'], 'COMPLIANT')

    rate_res = client.post('/api/sme_eval/rate', json={
        'persona_id': 'sre_platform',
        'reviewer': 'sre-oncall@example.com',
        'overall_rating': 5,
        'verdict': 'APPROVED',
        'notes': 'Cloud Run + GE fleet latency p95 and L1/L2 support stream verified.',
    })
    self.assertEqual(rate_res.status_code, 200)
    self.assertEqual(rate_res.json()['sme_evaluation']['sme_total_ratings_submitted'], 1)


if __name__ == '__main__':
  unittest.main()
