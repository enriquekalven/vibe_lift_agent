"""Tests for billing error visibility and per-user list-price cost estimates."""

from __future__ import annotations

import copy
import datetime
import math
import os
import re
import statistics
import threading
import types
import unittest
from unittest import mock

from finops_dashboard import billing_export, finops, fleet, ge_mart, reference_data, server, throughput
from finops_dashboard.ui import template as ui_template
from tests import test_fleet as ge_fleet_test

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()
_CARDS = _FAKE_FLEET.rate_cards()


class BillingErrorVisibilityTest(unittest.TestCase):

  def test_classify_billing_error_all_kinds(self) -> None:
    cases = [
        ("Invalid billing export table name: 'p:d.t'", 'invalid_table_name'),
        ('BigQuery HTTP 403: Access Denied: Table p:d.t: User does not have permission, or perhaps it does not exist.',
         'permission_denied'),
        ('BigQuery HTTP 400: Unrecognized name: credits at [7:45]', 'schema_mismatch'),
        ('BigQuery HTTP 404: Not found: Table example-gcp-project:billing_export.gcp_billing_export_v1_X', 'not_found'),
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
           mock.patch.object(reader, '_table_columns', return_value=None), \
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
        'ge_mart_dataset': 'finops_mart',
        'ge_curated_dataset': 'ds_ge_curated_staging',
        'ge_mart_refreshed_at': '2026-10-06T00:00:00Z',
        'ge_daily_totals': [{'day': '2026-10-05', 'interactions': 4, 'total_tokens': 1000}],
    }
    out = server.FinOpsRuntimeController._ge_daily_usage(fake_self, bq_insights)  # type: ignore[arg-type]
    assert out is not None
    self.assertEqual(out['billing_table'], 'example-project.billing.gcp_billing_export_v1_123')
    self.assertEqual(out['billing_status'], 'ERROR')
    self.assertEqual(out['billing_error_kind'], 'not_found')
    self.assertIn('not found', out['billing_hint'].lower())


class PerUserEstimatedCostTest(unittest.TestCase):

  def test_sql_builder_and_mapper(self) -> None:
    sql = ge_mart.build_user_model_tokens_sql('example-project', hours=720)
    self.assertIn('FROM `example-project.finops_mart.fct_turns`', sql)
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
        # alice on publishers/google/models/gemini-2.5-flash in us/app2: in=2M * 0.30 = $0.60
        {'user_email': 'alice@example.com', 'model_name': 'publishers/google/models/gemini-2.5-flash',
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
    self.assertAlmostEqual(by_ldap['alice']['est_cost_usd'], 2.40, places=4)
    self.assertEqual(by_ldap['alice']['est_cost_by_engine'], {'global/app1': 1.8, 'us/app2': 0.6})
    self.assertEqual(by_ldap['alice']['est_cost_models'], ['gemini-2.5-flash', 'gemini-2.5-pro'])
    self.assertEqual(by_ldap['alice']['est_cost_unpriced_tokens'], 50_134)
    self.assertIsNone(by_ldap['alice']['monthly_cost_usd'])

    self.assertIsNone(by_ldap['bob']['est_cost_usd'])
    self.assertEqual(by_ldap['bob']['est_cost_unpriced_tokens'], 10_500)
    self.assertIsNone(by_ldap['carol']['est_cost_usd'])
    self.assertEqual(by_ldap['carol']['est_cost_unpriced_tokens'], 0)

    self.assertEqual(summary['status'], 'LIVE')
    self.assertEqual(summary['priced_users'], 1)
    self.assertAlmostEqual(summary['total_est_cost_usd'], 2.40, places=4)
    self.assertEqual(summary['models_priced'], ['gemini-2.5-flash', 'gemini-2.5-pro'])
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
    ctrl = server.FinOpsRuntimeController(fleet_service=_FAKE_FLEET)
    bq = {
        'ge_mart_dataset': 'finops_mart',
        'window_hours': 168,
        'power_users_ldap': [
            {'user_ldap': 'alex', 'user_email': 'alex@example.com', 'department': 'Eng',
             'primary_agent': 'ge', 'sessions_7d': 2, 'monthly_cost_usd': None,
             'source_tables': ['finops_mart'], 'status': 'LIVE_HUMAN_PRINCIPAL',
             'anomaly_status': 'LIVE_HUMAN_PRINCIPAL', 'by_engine': {'global/app1': 2}},
        ],
        'ge_user_model_tokens': [
            {'user_email': 'alex@example.com', 'model_name': 'gemini-2.5-flash', 'engine_key': 'global/app1',
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
    self.assertAlmostEqual(user['est_cost_usd'], 0.55, places=4)
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
    for tok_id in (
        'id="vlValueRealizationBox"',
        'id="vlLoopCircuitBreakerBox"',
        'id="vlGsuForecastBox"',
        'id="vlModernizationRadarBox"',
    ):
      self.assertIn(tok_id, html)


class TokenomicsFiveFeaturesTest(unittest.TestCase):
  """The 5 adopted FinOps & tokenomics features, including regression tests for the issues found in review."""

  TODAY = datetime.date(2026, 10, 16)

  def _sample_fleet(self) -> dict[str, object]:
    return {
        'project_id': 'example-project',
        'window_hours': 24,
        'model_usage': {
            'interval': {'start': '2026-10-15T00:00:00Z', 'end': '2026-10-16T00:00:00Z'},
            'rate_cards': _CARDS,
            'totals': {'invocations': 12_000, 'est_cost_usd': 42.265, 'models_without_rate_card': []},
            'models': [
                {'model': 'gemini-2.5-pro', 'invocations': 2_000, 'input_tokens': 10_000_000,
                 'output_tokens': 200_000, 'reasoning_tokens': 0, 'cache_read_tokens': 10_000_000,
                 'cache_write_tokens': 0, 'est_cost_usd': 15.75},
                {'model': 'gemini-1.5-pro', 'invocations': 1_000, 'input_tokens': 4_000_000,
                 'output_tokens': 400_000, 'reasoning_tokens': 0, 'cache_read_tokens': 2_000_000,
                 'cache_write_tokens': 0, 'est_cost_usd': 7.625},
                {'model': 'gemini-2.5-flash', 'invocations': 9_000, 'input_tokens': 36_000_000,
                 'output_tokens': 2_400_000, 'reasoning_tokens': 500_000, 'cache_read_tokens': 18_000_000,
                 'cache_write_tokens': 1_000_000, 'est_cost_usd': 18.89},
            ],
        },
        'agents': [
            self._agent('loop-agent-1', 'Contract Loop Agent', 're-111', 'gemini-2.5-flash',
                        requests=10, errors_4xx=1, errors_5xx=1, llm_calls=500, input_tokens=2_000_000,
                        output_tokens=200_000, cached_tokens=500_000, est_token_cost_usd=0.965),
            self._agent('healthy-pro-agent', 'Code Copilot', 're-222', 'gemini-2.5-pro',
                        requests=100, errors_4xx=0, errors_5xx=0, llm_calls=200, input_tokens=1_000_000,
                        output_tokens=100_000, cached_tokens=400_000, est_token_cost_usd=1.80),
        ],
        'skills_and_mcp': [
            # Live producer fields (fleet.aggregate_trace_skills_and_mcp): calls + traces_count, no notes.
            {'name': 'clause_extractor', 'kind': 'SUB_AGENT', 'calls': 450, 'traces_count': 10},
            {'name': 'router', 'kind': 'SUB_AGENT', 'calls': 1_200, 'traces_count': 1_200},
            {'name': 'checker', 'kind': 'SKILL_TOOL', 'calls': 30, 'finops_note': 'No loop detected.'},
        ],
    }

  @staticmethod
  def _agent(agent_id: str, name: str, engine: str, model: str, **metrics: object) -> dict[str, object]:
    return {'agent_id': agent_id, 'display_name': name, 'engine_id': 'app-1', 'type': 'ADK',
            'backend': {'kind': 'agent_engine', 'reasoning_engine_id': engine, 'models': [model]},
            'metrics': dict(metrics)}

  @staticmethod
  def _usage_fleet(models: list[dict[str, object]], cards: dict[str, object] | None = None,
                   agents: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {'window_hours': 24, 'agents': agents or [],
            'model_usage': {'rate_cards': {**_CARDS, **(cards or {})}, 'models': models, 'totals': {}}}

  # ---- Feature 1: cache-read savings & failure token tax ------------------------------------------
  def test_feature1_cache_savings_and_5xx_based_failure_tax(self) -> None:
    vr = finops.build_value_realization_ledger(self._sample_fleet())
    self.assertEqual(vr['status'], 'LIVE')
    # 10M x (1.25 - 0.125) + 2M x (1.25 - 0.3125) + 18M x (0.30 - 0.03), per 1M tokens.
    self.assertAlmostEqual(vr['realized_cache_savings_window_usd'], 17.985, places=3)
    self.assertEqual(vr['cache_savings_basis'], 'GROSS_LIST_PRICE_EXCLUDING_CACHE_STORAGE')
    # Contract Loop Agent: 1 of the 9 requests that ran failed with 5xx; the 4xx is rejected, not taxed.
    self.assertEqual(vr['agent_requests_total'], 110)
    self.assertEqual(vr['agent_failed_requests'], 1)
    self.assertEqual(vr['agent_rejected_requests'], 1)
    self.assertEqual(vr['agent_successful_requests'], 108)
    self.assertAlmostEqual(vr['failure_token_tax_window_usd'], round(0.965 / 9, 4), places=4)
    self.assertAlmostEqual(vr['productive_agent_spend_usd'], 2.765 - round(0.965 / 9, 4), places=4)
    self.assertAlmostEqual(vr['agent_failure_rate_pct'], 0.92, places=2)
    self.assertAlmostEqual(vr['cost_per_successful_request_usd'], 2.765 / 108, places=4)
    self.assertEqual(vr['failure_tax_basis'], 'ESTIMATE')

  def test_feature1_rejected_only_requests_carry_no_tax(self) -> None:
    ag = self._agent('a', 'Rejector', 're-1', 'gemini-2.5-flash', requests=100, errors_4xx=50, errors_5xx=0,
                     input_tokens=1_000_000, output_tokens=50_000, est_token_cost_usd=1.0)
    vr = finops.build_value_realization_ledger(self._usage_fleet([], agents=[ag]))
    row = vr['by_agent'][0]
    self.assertEqual(row['failure_token_tax_usd'], 0.0)
    self.assertEqual(row['rejected_requests'], 50)
    self.assertEqual(row['failure_rate_pct'], 0.0)
    self.assertAlmostEqual(row['cost_per_successful_request_usd'], 1.0 / 50, places=5)

  def test_feature1_unpriced_agent_is_unknown_not_zero(self) -> None:
    ag = {'agent_id': 'u', 'display_name': 'No tokens', 'metrics': {'requests': 50, 'errors_5xx': 5}}
    vr = finops.build_value_realization_ledger(self._usage_fleet([], agents=[ag]))
    self.assertIsNone(vr['by_agent'][0]['failure_token_tax_usd'])
    self.assertIsNone(vr['failure_token_tax_window_usd'])
    self.assertIsNone(vr['productive_agent_spend_usd'])
    self.assertEqual(vr['unpriced_agent_count'], 1)

  def test_feature1_mart_failure_rate_reports_its_own_window(self) -> None:
    daily = {'days': [{'interactions': 100, 'failed_turns': 2}, {'interactions': 300, 'failed_turns': 6},
                      {'interactions': None}]}
    vr = finops.build_value_realization_ledger(self._sample_fleet(), daily_usage=daily)
    self.assertEqual(vr['mart_window_days'], 2)
    self.assertEqual(vr['mart_turns_total'], 400)
    self.assertEqual(vr['mart_failure_rate_pct'], 2.0)
    self.assertEqual(vr['mart_source'], 'finops_mart daily usage')

  # ---- Feature 2: loop / fan-out audit -------------------------------------------------------------
  def test_feature2_threshold_matches_dashboard_health_rule(self) -> None:
    js = (ui_template._STATIC_DIR / ui_template.ENHANCEMENTS_JS_FILE).read_text(encoding='utf-8')  # pylint: disable=protected-access
    m = re.search(r'loopCallsPerRequest:\s*(\d+)', js)
    self.assertIsNotNone(m)
    self.assertEqual(finops.DEFAULT_LOOP_CALLS_THRESHOLD, int(m.group(1)))

  def test_feature2_offender_spend_and_snippet(self) -> None:
    lcb = finops.build_loop_circuit_breaker_audit(self._sample_fleet())
    self.assertEqual(lcb['offender_count'], 1)
    off = lcb['offenders'][0]
    self.assertEqual(off['display_name'], 'Contract Loop Agent')
    self.assertEqual(off['excess_llm_calls'], 500 - 10 * 20)
    self.assertAlmostEqual(off['excess_loop_cost_window_usd'], 0.965 * 0.6, places=4)
    snippet = off['adk_circuit_breaker_snippet']
    for needle in ('RunConfig(max_llm_calls=20)', 'LlmCallsLimitExceededError', 'run_config=run_config',
                   'ADK_MAX_LLM_CALLS=20'):
      self.assertIn(needle, snippet)

  def test_feature2_boundaries(self) -> None:
    def agent(cpr: int) -> dict[str, object]:
      return self._agent(f'a{cpr}', f'A{cpr}', f're-{cpr}', 'gemini-2.5-flash', requests=100,
                         llm_calls=100 * cpr, input_tokens=1_000_000, output_tokens=10_000, est_token_cost_usd=0.5)
    lcb = finops.build_loop_circuit_breaker_audit(self._usage_fleet([], agents=[agent(17), agent(20)]))
    self.assertEqual([o['display_name'] for o in lcb['offenders']], ['A20'])
    self.assertEqual(lcb['offenders'][0]['excess_llm_calls'], 0)

  def test_feature2_sub_agent_loops_use_calls_per_trace(self) -> None:
    lcb = finops.build_loop_circuit_breaker_audit(self._sample_fleet())
    self.assertEqual([s['name'] for s in lcb['looping_sub_agents']], ['clause_extractor'])
    self.assertEqual(lcb['looping_sub_agents'][0]['calls_per_trace'], 45.0)

  def test_feature2_snippet_cannot_inject_code_via_agent_name(self) -> None:
    ag = self._agent('x', 'Evil"\nimport os  # pwn', 're-x', 'gemini-2.5-flash', requests=1, llm_calls=50,
                     input_tokens=1_000, output_tokens=100, est_token_cost_usd=0.01)
    lcb = finops.build_loop_circuit_breaker_audit(self._usage_fleet([], agents=[ag]))
    lines = lcb['offenders'][0]['adk_circuit_breaker_snippet'].splitlines()
    self.assertFalse(any(line.startswith('import os') for line in lines))

  # ---- F2: per-agent loop baselines from session history ---------------------------------------------
  @staticmethod
  def _f2_sessions(agent: str, runs: list[list[int | None]]) -> tuple[list[dict], dict[str, list[dict]]]:
    sessions, turns = [], {}
    for i, vals in enumerate(runs):
      key = f'eng|{agent[:3]}-{i}'
      sessions.append({'session_key': key, 'session_id': f'{agent[:3]}-{i}', 'agent_name': agent,
                       'user_email': f'user{i}@acme.example', 'turns': len(vals),
                       'llm_calls': sum(v or 0 for v in vals), 'session_end': '2026-10-15T10:00:00Z'})
      turns[key] = [{'llm_calls': v, 'agent_name': agent} for v in vals]
    return sessions, turns

  def test_f2_baseline_uses_the_agents_own_run_distribution(self) -> None:
    runs = [[20 + i, 30 + i] for i in range(10)] + [[25, 200]]  # an orchestrator, plus one runaway turn
    sessions, turns = self._f2_sessions('Contract Analyzer', runs)
    out = finops.build_session_loop_baselines(sessions, turns)
    self.assertEqual(out['status'], 'LIVE')
    b = out['agents'][0]
    # Oracle: statistics' inclusive method is the same linear interpolation as numpy's default.
    samples = sorted(v for r in runs for v in r)
    q1, _, q3 = statistics.quantiles(samples, n=4, method='inclusive')
    fence = max(q3 + 3 * (q3 - q1), 2 * q3, 10)
    normal = [v for v in samples if v <= fence]
    p99 = statistics.quantiles(normal, n=100, method='inclusive')[98]
    self.assertEqual((b['basis'], b['samples'], b['sessions']), ('TURN', 22, 11))
    self.assertEqual(b['fence'], round(fence, 1))                  # 69.5 = 2 x Q3 (34.75)
    self.assertEqual(b['p99_within_fence'], round(p99, 1))          # 38.8
    self.assertEqual(b['recommended_cap'], max(10, math.ceil(max(1.5 * p99, normal[-1]))))  # 59
    self.assertEqual(b['recommended_cap'], 59)
    self.assertEqual(b['runaway_sessions'], 1)
    self.assertEqual(b['runaways'][0]['session_id'], 'Con-10')
    self.assertEqual(b['runaways'][0]['calls_per_run'], 200.0)
    self.assertEqual(b['runaways'][0]['run_basis'], 'BUSIEST_TURN')

  def test_f2_short_history_and_search_turns_get_no_cap(self) -> None:
    sessions, turns = self._f2_sessions('HR Policy Q&A', [[2, 0], [3, None], [1]])
    out = finops.build_session_loop_baselines(sessions, turns)
    self.assertEqual(out['agents'], [])
    self.assertEqual(out['insufficient'], [{'agent_name': 'HR Policy Q&A', 'agent_key': 'hrpolicyqa', 'samples': 3}])
    self.assertEqual(finops.build_session_loop_baselines([], {})['status'], 'NO_DATA')

  def test_f2_without_turn_detail_session_averages_stand_in(self) -> None:
    sessions = [{'session_key': f'e|s{i}', 'session_id': f's{i}', 'agent_name': 'Support Triage',
                 'turns': 4, 'llm_calls': 8 + i} for i in range(11)]
    sessions.append({'session_key': 'e|loop', 'session_id': 'loop', 'agent_name': 'Support Triage',
                     'turns': 2, 'llm_calls': 400})
    b = finops.build_session_loop_baselines(sessions, {})['agents'][0]
    self.assertEqual((b['basis'], b['samples']), ('SESSION_AVERAGE', 12))
    self.assertEqual(b['recommended_cap'], 10)  # normal runs average 2-4.5 calls: the floor applies
    self.assertEqual(b['runaway_sessions'], 1)
    self.assertEqual((b['runaways'][0]['session_id'], b['runaways'][0]['run_basis']), ('loop', 'SESSION_AVERAGE'))

  def test_f2_audit_judges_each_runtime_against_its_own_cap(self) -> None:
    orchestrator = self._agent('o', 'Contract Analyzer', 're-o', 'gemini-2.5-flash', requests=220, llm_calls=9790,
                               input_tokens=30_000_000, output_tokens=2_000_000, est_token_cost_usd=13.0)
    simple = self._agent('s', 'Simple Bot', 're-s', 'gemini-2.5-flash', requests=100, llm_calls=1200,
                         input_tokens=1_000_000, output_tokens=50_000, est_token_cost_usd=0.5)
    fleet = self._usage_fleet([], agents=[orchestrator, simple])
    baselines = {'status': 'LIVE', 'agents': [
        {'agent_key': 'contractanalyzer', 'recommended_cap': 59, 'p99_within_fence': 38.8, 'samples': 22},
        {'agent_key': 'simplebot', 'recommended_cap': 10, 'p99_within_fence': 4.0, 'samples': 40}]}
    fixed = finops.build_loop_circuit_breaker_audit(fleet)
    self.assertEqual([o['display_name'] for o in fixed['offenders']], ['Contract Analyzer'])  # 44.5 >= 20, 12 < 20
    own = finops.build_loop_circuit_breaker_audit(fleet, baselines=baselines)
    self.assertEqual((own['threshold_basis'], own['agents_with_baseline']), ('PER_AGENT', 2))
    self.assertEqual([o['display_name'] for o in own['offenders']], ['Simple Bot'])  # 44.5 < 59, 12 >= 10
    off = own['offenders'][0]
    self.assertEqual((off['threshold_calls_per_request'], off['threshold_basis']), (10, 'SESSION_P99'))
    self.assertEqual(off['excess_llm_calls'], 1200 - 100 * 10)
    self.assertIn('RunConfig(max_llm_calls=10)', off['adk_circuit_breaker_snippet'])
    self.assertIn('p99 4.0 LLM calls per run over 40 runs', off['adk_circuit_breaker_snippet'])

  def test_f2_thresholds_reach_the_health_card_by_normalized_name(self) -> None:
    agents = [{'display_name': 'Contract  Analyzer!', 'metrics': {'requests': 1}}, {'display_name': 'Other'}, 'junk']
    baselines = {'agents': [{'agent_key': finops.normalize_agent_key('contract-analyzer'), 'recommended_cap': 59}]}
    out = finops.apply_loop_thresholds(agents, baselines)
    self.assertEqual(out[0]['metrics'], {'requests': 1, 'loop_threshold_calls_per_request': 59,
                                         'loop_threshold_basis': 'SESSION_P99',
                                         'loop_threshold_source': 'finops_mart.fct_sessions + fct_turns'})
    self.assertEqual(agents[0]['metrics'], {'requests': 1})  # the cached fleet dict is not mutated
    self.assertIs(out[1], agents[1])
    self.assertEqual(out[2], 'junk')

  # ---- Feature 3: credit split & Effective Savings Rate ---------------------------------------------
  def test_feature3_sql_uses_exact_credit_types(self) -> None:
    sql = billing_export.build_billing_sql('example-project.ds.gcp_billing_export_v1_X', 'example-project')
    self.assertIn("c.type IN ('COMMITTED_USAGE_DISCOUNT', 'COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE', "
                  "'SUBSCRIPTION_BENEFIT', 'SUSTAINED_USAGE_DISCOUNT')", sql)
    self.assertIn("c.type IN ('DISCOUNT')", sql)
    self.assertIn("c.type IN ('PROMOTION', 'FREE_TIER')", sql)
    self.assertNotIn('LIKE', sql)
    self.assertNotIn('FEE_UTILIZATION_OFFSET', sql)  # offsets a fee inside gross cost: "other", never ESR

  def test_feature3_esr_excludes_promotions_and_other_credits(self) -> None:
    rows = [{'sku_id': 'S1', 'service': 'Vertex AI', 'sku_description': 'Gemini input', 'gross_usd': 200.0,
             'credits_usd': -60.0, 'cud_credits_usd': -30.0, 'contract_discount_credits_usd': -10.0,
             'promo_credits_usd': -15.0, 'currency': 'USD'}]
    s = billing_export.summarize_rows(rows, 'example-project.ds.gcp_billing_export_v1_X')
    self.assertTrue(s['credit_split_available'])
    self.assertEqual(s['total_other_credits_usd'], -5.0)
    self.assertEqual(s['effective_savings_rate_pct'], 20.0)   # (30 + 10) / 200
    self.assertEqual(s['blended_credit_rate_pct'], 30.0)      # 60 / 200, promotions included
    self.assertEqual(s['cud_savings_rate_pct'], 15.0)
    self.assertEqual(s['sku_ledger'][0]['effective_savings_rate_pct'], 20.0)
    self.assertEqual(s['total_net_invoice_usd'], 140.0)

  def test_feature3_unknown_or_mixed_split_is_none_not_cud(self) -> None:
    legacy = {'service': 'Vertex AI', 'sku_id': 'A', 'sku_description': 'x', 'gross_usd': 100.0,
              'credits_usd': -30.0, 'currency': 'USD'}
    split = dict(legacy, sku_id='B', cud_credits_usd=-10.0, contract_discount_credits_usd=0.0,
                 promo_credits_usd=0.0)
    for rows in ([legacy], [legacy, split]):
      with self.subTest(n=len(rows)):
        s = billing_export.summarize_rows(rows, 'a-proj.b.c')
        self.assertFalse(s['credit_split_available'])
        self.assertIsNone(s['total_cud_credits_usd'])
        self.assertIsNone(s['effective_savings_rate_pct'])
        self.assertIsNotNone(s['blended_credit_rate_pct'])
    self.assertIsNone(billing_export.summarize_rows([legacy], 'a-proj.b.c')['sku_ledger'][0]['cud_credits_usd'])

  def test_feature3_not_connected_payload_has_no_numbers(self) -> None:
    p = billing_export.not_connected_payload()
    for key in ('total_cud_credits_usd', 'total_contract_discount_credits_usd', 'total_promo_credits_usd',
                'total_other_credits_usd', 'effective_savings_rate_pct', 'cud_savings_rate_pct',
                'blended_credit_rate_pct', 'esr_basis', 'credit_esr_pct', 'total_list_usd', 'price_discount_usd',
                'list_price_rows_excluded', 'list_price_gross_excluded_usd', 'list_price_column'):
      self.assertIsNone(p[key], key)

  # ---- F3: Effective Savings Rate against list price (cost_at_list) ---------------------------------
  _F3_TABLE = 'example-project.ds.gcp_billing_export_v1_X'

  def test_f3_list_price_columns_are_opt_in(self) -> None:
    plain = billing_export.build_billing_sql(self._F3_TABLE, 'example-project')
    self.assertNotIn('cost_at_list', plain)
    listed = billing_export.build_billing_sql(self._F3_TABLE, 'example-project', include_list_price=True)
    self.assertIn('ROUND(SUM(cost_at_list), 6) AS list_usd', listed)
    self.assertIn('ROUND(SUM(IF(cost_at_list IS NULL, 0, cost)), 6) AS listed_gross_usd', listed)

  def test_f3_list_basis_counts_discounts_built_into_the_price(self) -> None:
    rows = [
        # List $250, billed $200 after negotiated pricing, plus $40 of commitment + contract credits.
        {'sku_id': 'A', 'service': 'Vertex AI', 'sku_description': 'Gemini input', 'gross_usd': 200.0,
         'list_usd': 250.0, 'listed_gross_usd': 200.0, 'credits_usd': -60.0, 'cud_credits_usd': -30.0,
         'contract_discount_credits_usd': -10.0, 'promo_credits_usd': -15.0, 'currency': 'USD'},
        # A tax line has no list price: left out of the list basis and counted.
        {'sku_id': 'T', 'service': 'Tax', 'sku_description': 'Sales tax', 'gross_usd': 12.0, 'list_usd': None,
         'listed_gross_usd': 0.0, 'credits_usd': 0.0, 'cud_credits_usd': 0.0,
         'contract_discount_credits_usd': 0.0, 'promo_credits_usd': 0.0, 'currency': 'USD'},
    ]
    s = billing_export.summarize_rows(rows, self._F3_TABLE)
    self.assertEqual(s['esr_basis'], 'LIST_PRICE')
    self.assertEqual(s['effective_savings_rate_pct'], 36.0)  # (250 - 200 + 30 + 10) / 250
    self.assertEqual(s['credit_esr_pct'], 18.9)              # (30 + 10) / 212, the credits-only lower bound
    self.assertEqual(s['total_list_usd'], 250.0)
    self.assertEqual(s['price_discount_usd'], 50.0)
    self.assertEqual(s['list_price_rows_excluded'], 1)
    self.assertEqual(s['list_price_gross_excluded_usd'], 12.0)
    self.assertEqual(s['esr_method'], billing_export.ESR_METHOD_LIST)
    self.assertEqual(s['sku_ledger'][0]['price_discount_usd'], 50.0)
    self.assertIsNone(s['sku_ledger'][1]['list_price_usd'])

  def test_f3_list_basis_needs_the_credit_split(self) -> None:
    rows = [{'sku_id': 'A', 'service': 'Vertex AI', 'sku_description': 'x', 'gross_usd': 100.0,
             'list_usd': 120.0, 'credits_usd': -30.0, 'currency': 'USD'}]
    s = billing_export.summarize_rows(rows, self._F3_TABLE)
    self.assertIsNone(s['effective_savings_rate_pct'])  # promotions cannot be told apart from savings
    self.assertIsNone(s['esr_basis'])
    self.assertEqual(s['total_list_usd'], 120.0)

  def test_f3_without_list_prices_the_basis_is_credits_only(self) -> None:
    rows = [{'sku_id': 'A', 'service': 'Vertex AI', 'sku_description': 'x', 'gross_usd': 200.0,
             'credits_usd': -40.0, 'cud_credits_usd': -30.0, 'contract_discount_credits_usd': -10.0,
             'promo_credits_usd': 0.0, 'currency': 'USD'}]
    s = billing_export.summarize_rows(rows, self._F3_TABLE)
    self.assertEqual(s['esr_basis'], 'CREDITS_ONLY')
    self.assertEqual(s['effective_savings_rate_pct'], 20.0)
    self.assertEqual(s['credit_esr_pct'], 20.0)
    self.assertIsNone(s['list_price_rows_excluded'])
    self.assertEqual(s['esr_method'], billing_export.ESR_METHOD)

  def test_f3_schema_probe_reads_tables_get_and_caches(self) -> None:
    reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
    body = b'{"schema": {"fields": [{"name": "cost"}, {"name": "cost_at_list"}, {"name": "credits"}]}}'
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = body
    table = 'example.com:billing-proj.ds.gcp_billing_export_v1_X'
    with mock.patch('urllib.request.urlopen', return_value=resp) as urlopen:
      self.assertEqual(reader._table_columns(table), frozenset({'cost', 'cost_at_list', 'credits'}))
      self.assertIn('cost_at_list', reader._table_columns(table))  # served from the cache
    self.assertEqual(urlopen.call_count, 1)
    req = urlopen.call_args.args[0]
    self.assertEqual(req.get_method(), 'GET')
    self.assertEqual(req.full_url, 'https://bigquery.googleapis.com/bigquery/v2/projects/example.com:billing-proj'
                                   '/datasets/ds/tables/gcp_billing_export_v1_X')
    self.assertEqual(req.get_header('X-goog-user-project'), 'example-project')

  def test_f3_schema_probe_failure_is_not_cached(self) -> None:
    reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = b'{"schema": {"fields": [{"name": "cost"}]}}'
    with mock.patch('urllib.request.urlopen', side_effect=[OSError('network down'), ok]) as urlopen:
      self.assertIsNone(reader._table_columns(self._F3_TABLE))
      self.assertEqual(reader._table_columns(self._F3_TABLE), frozenset({'cost'}))
    self.assertEqual(urlopen.call_count, 2)
    self.assertIsNone(billing_export.BillingExportReader('p', lambda: None)._table_columns(self._F3_TABLE))

  def test_f3_query_asks_for_list_prices_only_when_the_column_exists(self) -> None:
    row = {'sku_id': 'A', 'service': 'Vertex AI', 'sku_description': 'x', 'gross_usd': 200.0,
           'credits_usd': -40.0, 'cud_credits_usd': -30.0, 'contract_discount_credits_usd': -10.0,
           'promo_credits_usd': 0.0, 'currency': 'USD'}
    for columns, expect_list in ((frozenset({'cost', 'credits'}), False),
                                 (frozenset({'cost', 'credits', 'cost_at_list'}), True)):
      with self.subTest(expect_list=expect_list):
        reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
        listed = dict(row, list_usd=250.0, listed_gross_usd=200.0) if expect_list else row
        with mock.patch.object(reader, '_table_columns', return_value=columns), \
             mock.patch.object(reader, '_run', return_value=([listed], None)) as run:
          out = reader._query(self._F3_TABLE)
        self.assertEqual(run.call_count, 1)
        self.assertEqual('cost_at_list' in run.call_args.args[0], expect_list)
        self.assertEqual(out['list_price_column'], 'present' if expect_list else 'absent')
        self.assertEqual(out['esr_basis'], 'LIST_PRICE' if expect_list else 'CREDITS_ONLY')

  def test_f3_unknown_schema_retries_without_list_prices(self) -> None:
    reader = billing_export.BillingExportReader('example-project', lambda: 'tok')
    row = {'sku_id': 'A', 'service': 'Vertex AI', 'sku_description': 'x', 'gross_usd': 10.0,
           'credits_usd': 0.0, 'cud_credits_usd': 0.0, 'contract_discount_credits_usd': 0.0,
           'promo_credits_usd': 0.0, 'currency': 'USD'}
    missing = 'BigQuery HTTP 400: Unrecognized name: cost_at_list at [7:18]'
    with mock.patch.object(reader, '_table_columns', return_value=None), \
         mock.patch.object(reader, '_run', side_effect=[([], missing), ([row], None)]) as run:
      out = reader._query(self._F3_TABLE)
    self.assertEqual(run.call_count, 2)
    self.assertIn('cost_at_list', run.call_args_list[0].args[0])
    self.assertNotIn('cost_at_list', run.call_args_list[1].args[0])
    self.assertEqual(out['status'], 'LIVE')
    self.assertEqual(out['list_price_column'], 'absent')
    # Any other failure is reported as is, without a second query.
    with mock.patch.object(reader, '_table_columns', return_value=None), \
         mock.patch.object(reader, '_run', return_value=([], 'BigQuery HTTP 403: Access Denied')) as run:
      self.assertEqual(reader._query(self._F3_TABLE)['status'], 'ERROR')
    self.assertEqual(run.call_count, 1)

  # ---- Feature 4: Provisioned Throughput break-even & projection ----------------------------------
  def test_feature4_reference_values_match_published_tables(self) -> None:
    self.assertEqual((finops.GSU_MONTHLY_1MO_USD, finops.GSU_MONTHLY_1YR_USD), (2700.0, 2000.0))
    self.assertEqual(finops.PT_GSU_SPECS['gemini-2.5-flash'], (2690.0, 9.0, 1.0))
    self.assertEqual(finops.PT_GSU_SPECS['gemini-2.5-pro'], (650.0, 8.0, 1.0))

  def test_feature4_sizing_uses_each_models_burndown(self) -> None:
    day = 86_400
    cases = [
        ({'model': 'publishers/google/models/gemini-2.5-flash-001', 'input_tokens': 2690 * day}, 1),
        ({'model': 'gemini-2.5-flash', 'input_tokens': 2690 * day + 1}, 2),
        ({'model': 'gemini-2.5-pro', 'output_tokens': 650 * day // 8}, 1),      # output burns 8x
        ({'model': 'gemini-2.5-pro', 'output_tokens': 650 * day // 8 + 1}, 2),
    ]
    for model, gsus in cases:
      with self.subTest(model=model):
        g = finops.build_gsu_and_quota_forecast(self._usage_fleet([dict(model, est_cost_usd=10.0)]))
        self.assertEqual(g['by_model'][0]['recommended_gsu_units'], gsus)

  def test_feature4_recommendations_never_invent_a_hybrid_or_sizing(self) -> None:
    models = [
        {'model': 'gemini-2.5-flash', 'input_tokens': 1_000, 'est_cost_usd': 1400.0 / 30},   # 70% of 1 GSU
        {'model': 'gemini-2.5-flash-lite', 'input_tokens': 1_000, 'est_cost_usd': 2100.0 / 30},
        {'model': 'claude-sonnet-4-6', 'input_tokens': 1_000, 'est_cost_usd': 1.0},
    ]
    rows = {r['model']: r for r in finops.build_gsu_and_quota_forecast(self._usage_fleet(models))['by_model']}
    self.assertEqual(rows['gemini-2.5-flash']['recommendation'], 'STAY_ON_PAYG')
    self.assertEqual(rows['gemini-2.5-flash-lite']['recommendation'], 'COMMIT_GSU_1YR')
    self.assertEqual(rows['claude-sonnet-4-6']['recommendation'], 'NO_PT_REFERENCE')
    self.assertIsNone(rows['claude-sonnet-4-6']['recommended_gsu_units'])
    self.assertIsNone(rows['claude-sonnet-4-6']['gsu_1yr_commitment_usd'])

  def test_feature4_projection_prefers_billing_burn(self) -> None:
    daily = {'days': [{'ai_net_usd': 10.0}, {'ai_net_usd': 20.0}, {'ai_net_usd': 30.0}, {'ai_net_usd': None}]}
    g = finops.build_gsu_and_quota_forecast(self._sample_fleet(), daily_usage=daily)
    self.assertEqual(g['status'], 'PROJECTION')
    self.assertEqual((g['forecast_30d_usd'], g['forecast_60d_usd'], g['forecast_90d_usd']), (600.0, 1200.0, 1800.0))
    self.assertTrue(g['burn_rate_basis'].startswith('Cloud Billing export'))
    self.assertNotIn('days_to_budget_exhaustion', g)
    self.assertEqual(g['sizing_basis'], 'WINDOW_AVERAGE')

  def test_f1_per_minute_profile_distinguishes_steady_vs_bursty_with_identical_totals(self) -> None:
    # Both workloads consume 190,080,000 input tokens in 24h (2,200 TPS window average, 81.8% of 1 GSU on
    # gemini-2.5-flash at 2,690 TPS/GSU) and cost $2,500/30d on pay-as-you-go.
    total_tokens = 2200 * 86_400
    est_cost_24h = 2500.0 / 30.0
    t0 = 1_760_000_000

    def make_series(active_minutes: int, tokens_per_active_minute: int) -> list[dict[str, object]]:
      pts = [
          {
              'interval': {'endTime': datetime.datetime.fromtimestamp(t0 - i * 60, tz=datetime.UTC).isoformat()},
              'value': {'int64Value': str(tokens_per_active_minute)},
          }
          for i in range(active_minutes)
      ]
      return [{
          'resource': {'labels': {'model_user_id': 'gemini-2.5-flash'}},
          'metric': {'labels': {'type': 'input'}},
          'points': pts,
      }]

    # 1. Steady traffic: 2,200 TPS every minute for 1,440 minutes -> 1 GSU covers 100% of tokens ($2,000/mo vs $2,500 PAYG).
    steady_prof = throughput.build_model_throughput_profiles(
        make_series(1440, 2200 * 60), window_s=86_400, end_epoch=t0
    )['gemini-2.5-flash']
    steady_forecast = finops.build_gsu_and_quota_forecast(self._usage_fleet([{
        'model': 'gemini-2.5-flash',
        'input_tokens': total_tokens,
        'est_cost_usd': est_cost_24h,
        'throughput_profile': steady_prof,
    }]))
    steady_row = steady_forecast['by_model'][0]
    self.assertEqual(steady_forecast['sizing_basis'], 'PER_MINUTE_PROFILE')
    self.assertEqual(steady_row['recommendation'], 'COMMIT_GSU_1YR')
    self.assertEqual(steady_row['recommended_gsu_units'], 1)
    self.assertEqual(steady_row['p99_gsu_units'], 1)
    self.assertEqual(steady_row['peak_gsu_units'], 1)
    self.assertEqual(steady_row['covered_tokens_share_pct'], 100.0)
    self.assertEqual(steady_row['spillover_payg_30d_usd'], 0.0)
    self.assertEqual(steady_row['net_monthly_savings_vs_1yr_gsu_usd'], 500.0)

    # 2. Bursty traffic: exact same 190,080,000 tokens and $2,500/30d PAYG cost, but arriving in 144 active
    # minutes at 22,000 TPS (9 GSUs during bursts, idle the other 90% of the day).
    # Window-average sizing would falsely recommend 1 GSU; per-minute sizing sees 87.8% spillover at 1 GSU
    # ($2,000 GSU + $2,194.32 spillover = $4,194.32 > $2,500 PAYG) and recommends STAY_ON_PAYG.
    bursty_prof = throughput.build_model_throughput_profiles(
        make_series(144, 22_000 * 60), window_s=86_400, end_epoch=t0
    )['gemini-2.5-flash']
    bursty_forecast = finops.build_gsu_and_quota_forecast(self._usage_fleet([{
        'model': 'gemini-2.5-flash',
        'input_tokens': total_tokens,
        'est_cost_usd': est_cost_24h,
        'throughput_profile': bursty_prof,
    }]))
    bursty_row = bursty_forecast['by_model'][0]
    self.assertEqual(bursty_row['recommendation'], 'STAY_ON_PAYG')
    self.assertEqual(bursty_row['avg_gsu_units'], 1)
    self.assertEqual(bursty_row['p99_gsu_units'], 9)
    self.assertEqual(bursty_row['peak_gsu_units'], 9)
    self.assertAlmostEqual(bursty_row['spillover_tokens_share_pct'], (22_000 - 2690) / 22_000 * 100.0, places=1)
    self.assertLess(bursty_row['net_monthly_savings_vs_1yr_gsu_usd'], 0.0)

  def test_f1_diurnal_base_gsu_with_peak_spillover_minimizes_combined_cost(self) -> None:
    # 700 minutes at 2 GSUs (5,000 TPS on gemini-2.5-flash) + 20 spike minutes at 5 GSUs (13,000 TPS);
    # 30-day PAYG cost = $9,000. Buying 5 GSUs for the 20-minute spike would cost $10,000/mo; buying 2 GSUs
    # ($4,000/mo) covers 95.8% of tokens and lets 4.2% spill over to PAYG ($378.15/mo) = $4,378.15/mo total.
    profile = {
        'resolution_s': 60,
        'window_minutes': 1440,
        'active_minutes': 720,
        'active_minute_tps': ([5000.0] * 700) + ([13_000.0] * 20),
    }
    opt = throughput.optimize_gsu_count(
        profile, tps_per_gsu=2690.0, gsu_1mo_usd=2700.0, gsu_1yr_usd=2000.0, payg_30d_usd=9000.0
    )
    assert opt is not None
    self.assertEqual(opt['recommendation'], 'COMMIT_GSU_1YR')
    self.assertEqual(opt['recommended_gsu_units'], 2)
    self.assertEqual(opt['peak_gsu_units'], 5)
    expected_spill = (20 * (13_000.0 - 2 * 2690.0)) / (700 * 5000.0 + 20 * 13_000.0)
    self.assertAlmostEqual(opt['spillover_payg_30d_usd'], round(9000.0 * expected_spill, 2), places=2)
    self.assertAlmostEqual(opt['combined_30d_usd'], round(4000.0 + 9000.0 * expected_spill, 2), places=2)

  def test_f1_profiles_skip_malformed_points_and_unpriced_models_get_no_dollars(self) -> None:
    self.assertEqual(
        [throughput.classify_token_type(t) for t in ('input', 'output', 'thoughts', 'cache_read', 'cache_write', 'image', None)],
        ['input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens',
         'other_tokens', 'other_tokens'])
    t0 = 1_760_000_000
    iso = datetime.datetime.fromtimestamp(t0, tz=datetime.UTC).isoformat()
    series = [
        'junk',                                                                     # not a series
        {'resource': {'labels': {}}, 'points': [{'interval': {'endTime': iso}}]},  # no model label
        {
            'resource': {'labels': {'model_user_id': 'gemini-2.5-flash'}},
            'metric': {'labels': {'type': 'output'}},
            'points': [
                'junk',                                                                # not a point
                {'interval': {'endTime': 'not-a-timestamp'}, 'value': {'int64Value': '600'}},
                {'interval': {'endTime': iso}, 'value': {}},                           # no value
                {'interval': {'endTime': iso}, 'value': {'int64Value': '0'}},          # zero tokens
                {'interval': {'endTime': t0 - 7200}, 'value': {'int64Value': '900'}},  # outside the window
                {'interval': {'endTime': t0 - 60}, 'value': {'doubleValue': 600.0}},   # epoch seconds
                {'interval': {'endTime': iso}, 'value': {'int64Value': '1200'}},
            ],
        },
        {  # only points older than the window: no profile at all (never a zero-filled one)
            'resource': {'labels': {'model_user_id': 'gemini-2.5-pro'}},
            'metric': {'labels': {'type': 'input'}},
            'points': [{'interval': {'endTime': t0 - 7200}, 'value': {'int64Value': '5000'}}],
        },
    ]
    _, out_mult, _ = reference_data.pt_specs()[reference_data.model_key('gemini-2.5-flash')]
    profiles = throughput.build_model_throughput_profiles(series, window_s=3600)
    self.assertEqual(sorted(profiles), ['gemini-2.5-flash'])
    prof = profiles['gemini-2.5-flash']
    self.assertEqual((prof['window_minutes'], prof['active_minutes']), (60, 2))
    self.assertEqual(prof['active_minute_tps'], [round(600 * out_mult / 60, 4), round(1200 * out_mult / 60, 4)])
    self.assertEqual(prof['peak_burndown_tps'], round(1200 * out_mult / 60, 2))
    one = throughput.build_model_throughput_profiles(series, window_s=60)['gemini-2.5-flash']
    self.assertEqual((one['window_minutes'], one['active_minutes']), (1, 1))
    self.assertEqual(one['p50_burndown_tps'], one['peak_burndown_tps'])

    kw = {'tps_per_gsu': 2690.0, 'gsu_1mo_usd': 2700.0, 'gsu_1yr_usd': 2000.0}
    self.assertIsNone(throughput.optimize_gsu_count({}, payg_30d_usd=100.0, **kw))
    self.assertIsNone(throughput.optimize_gsu_count({'peak_burndown_tps': 10.0}, tps_per_gsu=0.0, gsu_1mo_usd=1.0,
                                                    gsu_1yr_usd=1.0, payg_30d_usd=1.0))
    # Without a rate card the GSU count is still sized (p99) but no pay-as-you-go dollars are invented.
    unpriced = throughput.optimize_gsu_count({'quantiles_tps': [0.0] * 90 + [3000.0] * 11}, payg_30d_usd=None, **kw)
    assert unpriced is not None
    self.assertEqual(unpriced['recommendation'], 'UNPRICED_MODEL')
    self.assertEqual((unpriced['p99_gsu_units'], unpriced['recommended_gsu_units']), (2, 2))
    for key in ('spillover_payg_30d_usd', 'combined_30d_usd', 'net_monthly_savings_vs_1yr_gsu_usd',
                'break_even_progress_pct'):
      self.assertIsNone(unpriced[key], key)
    # A profile with only its peak still sizes the peak; one burst minute is not worth a commitment.
    peak_only = throughput.optimize_gsu_count({'peak_burndown_tps': 6000.0}, payg_30d_usd=1000.0, **kw)
    assert peak_only is not None
    self.assertEqual((peak_only['peak_gsu_units'], peak_only['recommendation']), (3, 'STAY_ON_PAYG'))


  # ---- Feature 5: model retirement & right-sizing radar ---------------------------------------------
  def test_feature5_flags_retiring_models_and_never_recommends_one(self) -> None:
    mmr = finops.build_model_modernization_radar(self._sample_fleet(), today=self.TODAY)
    project = [f for f in mmr['findings'] if f['scope'] == 'PROJECT_MODEL']
    self.assertEqual(sorted((f['subject'], f['finding_kind']) for f in project), [
        ('gemini-1.5-pro', 'MODEL_RETIRED'),
        ('gemini-2.5-flash', 'MODEL_RETIRING_SOON'),
        ('gemini-2.5-pro', 'MODEL_RETIRING_SOON'),
    ])
    self.assertEqual(mmr['retiring_model_count'], 3)
    for f in mmr['findings']:
      with self.subTest(subject=f['subject'], kind=f['finding_kind']):
        self.assertIsNotNone(f['recommended_model'])
        self.assertIsNone(finops._lifecycle(finops._model_key(f['recommended_model']), self.TODAY))  # pylint: disable=protected-access
    # gemini-1.5-pro's listed replacement (gemini-2.5-flash) is itself retiring, so the chain is followed.
    by_subject = {f['subject']: f for f in project}
    self.assertEqual(by_subject['gemini-1.5-pro']['recommended_model'], 'gemini-3.8-flash')
    self.assertEqual({f['finding_kind'] for f in mmr['findings'] if f['scope'] == 'AGENT_RUNTIME'},
                     {'AGENT_MODEL_RETIRING_SOON'})

  def test_feature5_one_finding_per_model_and_no_double_counting(self) -> None:
    mmr = finops.build_model_modernization_radar(self._sample_fleet(), today=self.TODAY)
    subjects = [f['subject'] for f in mmr['findings'] if f['scope'] == 'PROJECT_MODEL']
    self.assertEqual(len(subjects), len(set(subjects)))
    self.assertEqual(mmr['project_model_savings_30d_usd'], 0.0)  # retirement deltas are not "savings"

  def test_feature5_short_output_pro_rightsizing_is_computed_and_gated(self) -> None:
    card = {'gemini-3.1-pro-preview': {'input': 2.0, 'output': 12.0, 'cached_read': 0.2, 'cache_write': 2.0}}
    short = {'model': 'gemini-3.1-pro-preview', 'invocations': 1_000, 'input_tokens': 4_000_000, 'output_tokens': 100_000}
    mmr = finops.build_model_modernization_radar(self._usage_fleet([short], cards=card), today=self.TODAY)
    f = mmr['findings'][0]
    self.assertEqual(f['finding_kind'], 'OVER_TIERED_SHORT_OUTPUT')
    self.assertEqual(f['recommended_model'], finops.TIER_DOWNGRADE_TARGET)
    self.assertTrue(f['requires_quality_eval'])
    cur = 4 * 2.0 + 0.1 * 12.0
    tgt = finops._cost(short, finops._resolve_rate_cards_map({'rate_cards': {**_CARDS, **card}})[f['recommended_model']])  # pylint: disable=protected-access
    self.assertAlmostEqual(mmr['project_model_savings_window_usd'], cur - tgt, places=4)
    self.assertIn(f'{round((cur - tgt) / cur * 100)}% less', f['rationale'])
    long_out = dict(short, output_tokens=500_000)
    self.assertEqual(finops.build_model_modernization_radar(
        self._usage_fleet([long_out], cards=card), today=self.TODAY)['findings'], [])

  def test_feature5_is_date_aware(self) -> None:
    flash = [{'model': 'gemini-2.5-flash', 'invocations': 10, 'input_tokens': 1_000, 'output_tokens': 100}]
    after = finops.build_model_modernization_radar(self._usage_fleet(flash), today=datetime.date(2026, 10, 25))
    self.assertEqual(after['findings'][0]['finding_kind'], 'MODEL_RETIRED')
    early = finops.build_model_modernization_radar(self._usage_fleet(flash), today=datetime.date(2026, 1, 1))
    self.assertEqual(early['findings'], [])

  def test_feature5_migration_snippet_cannot_inject_code(self) -> None:
    ag = self._agent('x', 'Bad "name"\nimport os', 're-x', 'gemini-2.5-flash', requests=1, llm_calls=1,
                     input_tokens=1_000, output_tokens=100)
    mmr = finops.build_model_modernization_radar(self._usage_fleet([], agents=[ag]), today=self.TODAY)
    lines = mmr['findings'][0]['migration_snippet'].splitlines()
    self.assertFalse(any(line.startswith('import os') for line in lines))
    self.assertRegex(lines[-1], r'^agent = Agent\(name="[a-z_][a-z0-9_]*", model="gemini-3\.8-flash"\)$')

  def test_live_bundle_has_all_feature_keys(self) -> None:
    bundle = finops.build_live_finops(self._sample_fleet())
    for k in ('value_realization', 'loop_circuit_breaker', 'loop_baselines', 'gsu_and_quota_forecast',
              'model_modernization_radar'):
      self.assertIn(k, bundle)


class LiveFinopsWiringTest(unittest.TestCase):
  """fleet.live_finops (what the UI reads first) must get the same billing / mart inputs as state.live_finops."""

  def test_fleet_bundle_receives_daily_usage_and_sessions(self) -> None:
    turns = {'e|s1': [{'llm_calls': 3}]}
    insights = {'ge_daily_totals': [], 'ge_sessions': [{'turns': 4, 'failed_turns': 1}], 'ge_session_turns': turns}
    daily = {'days': [{'ai_net_usd': 10.0, 'interactions': 100, 'failed_turns': 1}] * 3}
    seen: dict[str, object] = {}
    cached_agent = {'display_name': 'Contract Analyzer', 'metrics': {'requests': 10, 'llm_calls': 300}}
    other_agent = {'display_name': 'IT Service Desk', 'metrics': {'requests': 10, 'llm_calls': 20}}
    baselines = {'status': 'LIVE', 'agents': [{'agent_key': 'contractanalyzer', 'recommended_cap': 48}]}

    def fake_live_finops(fleet_arg, *, daily_usage=None, sessions=None, session_turns=None):
      del fleet_arg
      seen.update(daily_usage=daily_usage, sessions=sessions, session_turns=session_turns)
      return {'bundle': True, 'loop_baselines': baselines}

    fake = types.SimpleNamespace(
        ge_fleet=types.SimpleNamespace(collect=lambda **kw: {'project_id': 'p', 'agents': [cached_agent, other_agent]},
                                       project_id='p'),
        gcp_telemetry=types.SimpleNamespace(fetch_live_bigquery_project_insights=lambda **kw: insights),
        optimizer=types.SimpleNamespace(sync_from_ge_fleet=lambda *a, **kw: None),
        _lock=threading.Lock(),
        _is_live_gcp=lambda: True,
        _ge_daily_usage=lambda bq: daily if bq is insights else None,
        _live_finops=fake_live_finops,
    )
    out = server.FinOpsRuntimeController.get_fleet_payload(fake, window_hours=24)
    self.assertEqual(out['live_finops'], {'bundle': True, 'loop_baselines': baselines})
    self.assertIs(seen['daily_usage'], daily)
    self.assertEqual(seen['sessions'], insights['ge_sessions'])
    self.assertIs(seen['session_turns'], turns)
    enriched = out['agents'][0]['metrics']
    self.assertEqual(enriched['loop_threshold_calls_per_request'], 48)
    self.assertEqual(enriched['loop_threshold_basis'], 'SESSION_P99')
    self.assertNotIn('loop_threshold_calls_per_request', cached_agent['metrics'])  # cached dict untouched
    self.assertIs(out['agents'][1], other_agent)  # no baseline: the health card keeps the default rule

  def test_ui_regressions_stay_fixed(self) -> None:
    js = (ui_template._STATIC_DIR / ui_template.ENHANCEMENTS_JS_FILE).read_text(encoding='utf-8')  # pylint: disable=protected-access
    self.assertIn("kpi('Re-price to 3.1 Flash-Lite'", js)  # card removed by mistake, restored
    self.assertIn("'vl-kpi-whatif'", js)
    self.assertIn('if (t === null) return DASH;', js)                       # unpriced tax is a dash, not $0.00
    self.assertNotIn('HYBRID_BASE_GSU_PLUS_PAYG', js)


if __name__ == '__main__':
  unittest.main()
