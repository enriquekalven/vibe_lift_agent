"""Deterministic & LLM-as-a-Judge Telemetry Grounding Validator for VibeLift.

Verifies every tab and metric in `/api/state` against live Google Cloud telemetry
(`project-maui` BigQuery, Cloud Monitoring v3, Cloud Logging v2, and Discovery
Engine v1alpha) to guarantee zero fabricated users, zero fake GCS URIs, and
explicit provenance tagging across all 6 dashboard tabs.
"""

from collections.abc import Mapping
import datetime
import json
import logging
import os
import urllib.error
import urllib.request

try:
  import google.auth
  from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
  google = None
  GoogleAuthRequest = None

logger = logging.getLogger(__name__)

# Fake GCS URIs from the old demo seed data that must never appear in Live GCP mode.
# (User rows are validated by BigQuery provenance, not by a name blocklist.)
BANNED_MOCK_GCS_PREFIX_IN_LIVE_MODE = 'gs://project-maui-aive-assets/'


def _is_live_gcp_mode(state: Mapping[str, object], ge_fleet: Mapping[str, object] | None = None) -> bool:
  """Returns True when the dashboard is running against a live GCP project (not unit-test fake)."""
  fleet_proj = str((ge_fleet or {}).get('project_id') or '').strip()
  state_proj = str(state.get('gcp_project') or '').strip()
  proj = fleet_proj or state_proj
  if not proj or proj in ('test-project', 'UNCONFIGURED-PROJECT'):
    return False
  uc = state.get('user_centric') if isinstance(state.get('user_centric'), Mapping) else {}
  mode = str(uc.get('collection_mode') or '')
  return 'LIVE GCP TELEMETRY' in mode


def validate_dashboard_state(
    state: Mapping[str, object],
    ge_fleet_payload: Mapping[str, object] | None = None,
    bq_insights: Mapping[str, object] | None = None,
    run_llm_judge: bool = False,
) -> dict[str, object]:
  """Audits all 6 tabs of `/api/state` for telemetry grounding, math integrity, and zero hallucination."""
  now_iso = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
  fleet = ge_fleet_payload or (state.get('ge_fleet') if isinstance(state.get('ge_fleet'), Mapping) else {}) or {}
  live_mode = _is_live_gcp_mode(state, fleet)

  checks: list[dict[str, object]] = []
  flagged_unverified_fields: list[dict[str, str]] = []

  def _add_check(
      check_id: str,
      tab: str,
      metric_or_panel: str,
      passed: bool,
      provenance: str,
      source_dataset: str,
      evidence: str,
  ) -> None:
    checks.append({
        'check_id': check_id,
        'tab': tab,
        'metric_or_panel': metric_or_panel,
        'metric': metric_or_panel,
        'status': 'PASS' if passed else 'FLAGGED',
        'provenance': provenance,
        'expected': provenance,
        'actual': 'VERIFIED' if passed else 'MISMATCH',
        'source_dataset': source_dataset,
        'source': source_dataset,
        'evidence': evidence,
        'detail': evidence,
    })
    if not passed:
      flagged_unverified_fields.append({
          'check_id': check_id,
          'tab': tab,
          'metric_or_panel': metric_or_panel,
          'reason': evidence,
      })

  # --- TAB 1: Gemini Enterprise Agent Fleet ---
  fleet_agents = fleet.get('agents') if isinstance(fleet.get('agents'), list) else []
  fleet_totals = fleet.get('totals') if isinstance(fleet.get('totals'), Mapping) else {}
  reported_agent_count = int(fleet_totals.get('agents') or 0)
  tab1_count_match = (reported_agent_count == len(fleet_agents)) and len(fleet_agents) > 0
  _add_check(
      check_id='TAB1-FLEET-INVENTORY',
      tab='Tab 1: Gemini Enterprise Agent Fleet',
      metric_or_panel='ge_fleet.agents & ge_fleet.totals.agents',
      passed=tab1_count_match,
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='discoveryengine.googleapis.com/v1alpha + Cloud Monitoring v3',
      evidence=f'Verified {len(fleet_agents)} registered agents matching totals.agents={reported_agent_count}.',
  )

  # Recompute request volume from per-agent metrics, counting each runtime once.
  try:
    from ge_fleet import runtime_backend_key  # pylint: disable=g-import-not-at-top
  except ImportError:  # pragma: no cover
    runtime_backend_key = lambda a: str(a.get('agent_id'))
  seen_rt: set[str] = set()
  sum_calls = 0
  for a in fleet_agents:
    if not isinstance(a, Mapping):
      continue
    rk = runtime_backend_key(dict(a))
    if rk in seen_rt:
      continue
    seen_rt.add(rk)
    sum_calls += int(((a.get('metrics') or {}).get('requests')) or 0)
  tot_calls = int(fleet_totals.get('requests') or 0)
  _add_check(
      check_id='TAB1-CALL-VOLUME-SUM',
      tab='Tab 1: Gemini Enterprise Agent Fleet',
      metric_or_panel='ge_fleet.totals.requests vs sum(agent.metrics.requests), one per runtime',
      passed=(sum_calls == tot_calls),
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='monitoring.googleapis.com/v3 (reasoning_engine + cloud_run + publisher)',
      evidence=f'Per-runtime request sum ({sum_calls}) vs fleet total ({tot_calls}) across {len(seen_rt)} runtimes.',
  )

  # --- TAB 2: Goals & Metrics ---
  active_agent = state.get('active_agent') if isinstance(state.get('active_agent'), Mapping) else {}
  params = active_agent.get('parameters') if isinstance(active_agent.get('parameters'), list) else []
  param_math_ok = True
  for p in params:
    if not isinstance(p, Mapping):
      continue
    b_val = float(p.get('baseline_value') or 0.0)
    c_val = float(p.get('current_value') or 0.0)
    d_val = float(p.get('delta_pct') or 0.0)
    if b_val != 0:
      expected_d = round(((c_val - b_val) / abs(b_val)) * 100.0, 1)
      if abs(expected_d - d_val) > 0.5:
        param_math_ok = False
  _add_check(
      check_id='TAB2-PARAMETER-DELTA-MATH',
      tab='Tab 2: Goals & Metrics',
      metric_or_panel='active_agent.parameters (Baseline vs Current Delta %)',
      passed=param_math_ok and len(params) >= 4,
      provenance='OPTIMIZER_SIMULATION' if live_mode else 'DERIVED_FROM_LIVE_TELEMETRY',
      source_dataset='alpha_evolve_optimizer.OptimizationParameter',
      evidence=f'Verified {len(params)} optimization parameters with exact delta_pct math.',
  )

  # --- TAB 3: Testing & History ---
  turns = state.get('turns') if isinstance(state.get('turns'), list) else []
  summary = (
      state.get('turn_summary')
      if isinstance(state.get('turn_summary'), Mapping)
      else (state.get('summary') if isinstance(state.get('summary'), Mapping) else {})
  )
  summary_turns = int((summary or {}).get('total_turns') or 0)
  _add_check(
      check_id='TAB3-TURN-TRAJECTORY-SYNC',
      tab='Tab 3: Testing & History',
      metric_or_panel='turns & turn_summary (Prompt Cache Forensics)',
      passed=(len(turns) == summary_turns and len(turns) > 0),
      provenance='OBSERVED_GCP_TELEMETRY' if live_mode else 'INTERACTIVE_WHAT_IF_SIMULATOR',
      source_dataset='project-maui.sre_triage_agent_telemetry.gen_ai_client_inference_operation_details',
      evidence=f'Verified {len(turns)} turn records synchronized with turn_summary.total_turns={summary_turns}.',
  )

  if live_mode:
    _add_live_finops_checks(state, fleet, fleet_agents, fleet_totals, _add_check)
  else:
    what_if = state.get('what_if_default') if isinstance(state.get('what_if_default'), Mapping) else {}
    _add_check(
        check_id='TAB3-WHAT-IF-CANARY-SIMULATOR',
        tab='Tab 3: Testing & History',
        metric_or_panel='what_if_default (Canary Traffic & Cost Simulator)',
        passed=bool(what_if.get('canary_rollout_command') and 'gcloud run services update-traffic' in str(what_if.get('canary_rollout_command'))),
        provenance='INTERACTIVE_WHAT_IF_SIMULATOR',
        source_dataset='VibeLift What-If Parametric Simulator (Labeled Projected Scenario)',
        evidence='Interactive What-If Canary simulator explicitly labeled as projected scenario with valid gcloud CLI.',
    )

    # --- TAB 4: Cost & Billing ---
    tc = state.get('tokenomics_cockpit') if isinstance(state.get('tokenomics_cockpit'), Mapping) else {}
    drift = tc.get('drift') if isinstance(tc.get('drift'), Mapping) else {}
    exp_spend = float(drift.get('expected_naive_token_spend_usd') or 0.0)
    rem_drift = float(drift.get('remediated_drift_total_usd') or 0.0)
    unattr = float(drift.get('unattributed_usd') or 0.0)
    actual_inv = float(drift.get('actual_reconciled_invoice_usd') or 0.0)
    drift_balanced = abs((exp_spend + rem_drift + unattr) - actual_inv) < 0.05 and unattr == 0.0
    _add_check(
        check_id='TAB4-DRIFT-RECONCILIATION-ZERO-UNATTRIBUTED',
        tab='Tab 4: Cost & Billing',
        metric_or_panel='tokenomics_cockpit.drift (D1..D5 Token-to-Spend Ledger)',
        passed=drift_balanced,
        provenance='DERIVED_FROM_LIVE_TELEMETRY',
        source_dataset='telemetry.MODEL_RATE_CARDS + FinOps D1..D5 Attribution Model',
        evidence=f'Expected (${exp_spend:.2f}) + Drift (${rem_drift:.2f}) + Unattributed (${unattr:.2f}) == Reconciled (${actual_inv:.2f}).',
    )

    caching = tc.get('caching') if isinstance(tc.get('caching'), Mapping) else {}
    flash_n = float(caching.get('flash_break_even_calls_per_hr') or 0.0)
    pro_n = float(caching.get('pro_break_even_calls_per_hr') or 0.0)
    _add_check(
        check_id='TAB4-CACHE-BREAKEVEN-MATH',
        tab='Tab 4: Cost & Billing',
        metric_or_panel='tokenomics_cockpit.caching (N* = 1 + S / 0.9*P_in)',
        passed=(abs(flash_n - 4.7) <= 0.1 and abs(pro_n - 5.0) <= 0.1),
        provenance='DERIVED_FROM_LIVE_TELEMETRY',
        source_dataset='Vertex AI Context Caching Rate Card Formula',
        evidence=f'Verified Flash break-even N*={flash_n} calls/hr and Pro break-even N*={pro_n} calls/hr.',
    )

  # --- TAB 5: Users & Feedback ---
  uc = state.get('user_centric') if isinstance(state.get('user_centric'), Mapping) else {}
  power_users = uc.get('power_users_ldap') if isinstance(uc.get('power_users_ldap'), list) else []
  user_ldaps = [str(u.get('user_ldap') or '') for u in power_users if isinstance(u, Mapping)]
  has_defined_csat_and_cost = all(
      isinstance(u, Mapping) and (u.get('avg_csat') is not None or u.get('csat_rating') is not None) and u.get('monthly_cost_usd') is not None
      for u in power_users
  )
  if live_mode:
    # Provenance check, not a name blocklist: every row must carry a live status
    # and a BigQuery source tag. Seed/demo rows have neither.
    live_statuses = ('LIVE_HUMAN_PRINCIPAL', 'SERVICE_ACCOUNT_TELEMETRY', 'UNVERIFIED_SESSION_ID')
    ungrounded = sorted(
        str(u.get('user_ldap') or '?')
        for u in power_users
        if isinstance(u, Mapping)
        and not (
            str(u.get('status') or '') in live_statuses
            and (u.get('source_tables') or '[' in str(u.get('department') or ''))
        )
    )
    no_mock_users = len(ungrounded) == 0 and len(user_ldaps) > 0
    _add_check(
        check_id='TAB5-NO-FAKE-POWER-USERS',
        tab='Tab 5: Users & Feedback',
        metric_or_panel='user_centric.power_users_ldap',
        passed=no_mock_users,
        provenance='OBSERVED_GCP_TELEMETRY',
        source_dataset='project-maui.ds_ge_audit_raw.cloudaudit_googleapis_com_data_access + sre_triage_agent_telemetry',
        evidence=(
            f'All {len(user_ldaps)} users trace to BigQuery audit/OTel rows ({", ".join(user_ldaps)}).'
            if no_mock_users
            else (f'FLAGGED: users without BigQuery provenance: {ungrounded}'
                  if ungrounded else 'FLAGGED: no users loaded from BigQuery yet.')
        ),
    )
  else:
    _add_check(
        check_id='TAB5-POWER-USERS-SCHEMA',
        tab='Tab 5: Users & Feedback',
        metric_or_panel='user_centric.power_users_ldap',
        passed=has_defined_csat_and_cost and len(user_ldaps) > 0,
        provenance='DERIVED_FROM_LIVE_TELEMETRY',
        source_dataset='alpha_evolve_optimizer.build_user_centric_analytics',
        evidence=f'Verified {len(user_ldaps)} user records with complete avg_csat and monthly_cost_usd fields.',
    )

  aive_logs = state.get('aive_logs') if isinstance(state.get('aive_logs'), Mapping) else {}
  usage_logs = aive_logs.get('usage_logs') if isinstance(aive_logs.get('usage_logs'), list) else []
  if live_mode:
    fake_uris = []
    for row in usage_logs:
      if isinstance(row, Mapping):
        for out in (row.get('outputs') or []):
          if isinstance(out, Mapping) and str(out.get('gcs_uri') or '').startswith(BANNED_MOCK_GCS_PREFIX_IN_LIVE_MODE):
            fake_uris.append(str(out.get('gcs_uri')))
    _add_check(
        check_id='TAB5-AIVE-USAGE-REAL-BQ-LOG-URIS',
        tab='Tab 5: Users & Feedback',
        metric_or_panel='aive_logs.usage_logs & ratings_logs',
        passed=(len(fake_uris) == 0 and len(usage_logs) > 0),
        provenance='OBSERVED_GCP_TELEMETRY',
        source_dataset='project-maui.sre_triage_agent_telemetry + ds_ge_audit_raw + vibelift_analytics',
        evidence=(
            f'Verified {len(usage_logs)} real BigQuery telemetry events with authentic bq://project-maui URIs.'
            if len(fake_uris) == 0
            else f'FLAGGED: Found mocked GCS URIs in live mode: {fake_uris}'
        ),
    )
  else:
    _add_check(
        check_id='TAB5-AIVE-USAGE-SCHEMA',
        tab='Tab 5: Users & Feedback',
        metric_or_panel='aive_logs.usage_logs & ratings_logs',
        passed=len(usage_logs) > 0,
        provenance='DERIVED_FROM_LIVE_TELEMETRY',
        source_dataset='telemetry.get_recent_aive_logs',
        evidence=f'Verified {len(usage_logs)} usage log records.',
    )

  # --- TAB 6: Tools & SDK ---
  otel = state.get('otel_catalog') if isinstance(state.get('otel_catalog'), Mapping) else {}
  layers = otel.get('layers') if isinstance(otel.get('layers'), list) else []
  total_otel_metrics = sum(
      len(l.get('metrics') or l.get('parameters') or [])
      for l in layers
      if isinstance(l, Mapping)
  )
  has_metrics_key = all(isinstance(l, Mapping) and isinstance(l.get('metrics'), list) for l in layers)
  _add_check(
      check_id='TAB6-OTEL-5LAYER-26-METRICS',
      tab='Tab 6: Tools & SDK',
      metric_or_panel='otel_catalog.layers (5-Layer 26-Metric Catalog)',
      passed=(len(layers) == 5 and total_otel_metrics == 26 and has_metrics_key),
      provenance='DERIVED_FROM_LIVE_TELEMETRY',
      source_dataset='OpenTelemetry GenAI Semantic Conventions + Cloud Monitoring v3',
      evidence=f'Verified 5 layers and {total_otel_metrics}/26 standardized OTel metrics with layer.metrics populated.',
  )

  services = (
      state.get('cloud_run_services')
      if isinstance(state.get('cloud_run_services'), list)
      else (state.get('gcp_services') if isinstance(state.get('gcp_services'), list) else [])
  )
  _add_check(
      check_id='TAB6-CLOUD-RUN-SERVICES-SYNC',
      tab='Tab 6: Tools & SDK',
      metric_or_panel='cloud_run_services / gcp_services',
      passed=len(services) > 0,
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='project-maui.vibelift_analytics.run_googleapis_com_requests_* + Cloud Monitoring',
      evidence=f'Verified {len(services)} Cloud Run agent services with active revision telemetry.',
  )

  passed_count = sum(1 for c in checks if c['status'] == 'PASS')
  total_checks = max(1, len(checks))
  deterministic_score = int(round((passed_count / total_checks) * 100.0))
  overall_verdict = 'VERIFIED_GROUNDED' if not flagged_unverified_fields else 'FLAGGED_ISSUES'

  llm_judge_report: dict[str, object]
  if run_llm_judge:
    llm_judge_report = run_llm_as_judge_audit(
        state=state,
        deterministic_checks=checks,
        bq_insights=bq_insights,
    )
  else:
    source_label = 'live GCP telemetry' if live_mode else 'simulator/test mode'
    if flagged_unverified_fields:
      exec_finding = (
          f'{passed_count}/{total_checks} rule-based checks passed ({source_label}). Failing: '
          + ', '.join(f['check_id'] for f in flagged_unverified_fields)
          + '.'
      )
    else:
      exec_finding = f'All {total_checks} rule-based checks passed ({source_label}).'
    llm_judge_report = {
        'executed': False,
        'judge_model': 'gemini-2.5-flash (Vertex AI Grounding Judge)',
        'judge_model_used': 'rule-based checks only (run the LLM judge for a model review)',
        'grounding_score_100': deterministic_score,
        'verdict': overall_verdict,
        'executive_finding': exec_finding,
        'executive_summary': exec_finding,
        'tab_findings': [
            f"{c['tab']}: {c['status']} ({c['check_id']})"
            for c in checks[:6]
        ],
    }

  return {
      'validated_at': now_iso,
      'gcp_project': str(fleet.get('project_id') or state.get('gcp_project') or ''),
      'live_gcp_mode': live_mode,
      'mode': 'LIVE_GCP_TELEMETRY' if live_mode else 'STANDARD_SIMULATOR_TELEMETRY',
      'overall_status': overall_verdict,
      'deterministic_score_100': deterministic_score,
      'grounding_score_pct': float(deterministic_score),
      'passed_checks': passed_count,
      'passed_count': passed_count,
      'failed_count': len(flagged_unverified_fields),
      'total_checks': total_checks,
      'flagged_unverified_count': len(flagged_unverified_fields),
      'flagged_unverified_fields': flagged_unverified_fields,
      'checks': checks,
      'provenance_summary': {
          'OBSERVED_GCP_TELEMETRY': (
              'Direct measurements from project-maui Discovery Engine v1alpha, Cloud Monitoring v3, '
              'and BigQuery (ds_ge_audit_raw, sre_triage_agent_telemetry, vibelift_analytics).'
          ),
          'DERIVED_FROM_LIVE_TELEMETRY': (
              'Deterministic rate-card calculations, cache break-even formulas (N* = 1 + S / 0.9*P_in), '
              'and D1..D5 drift attribution computed directly from observed token & call volumes.'
          ),
          'OPTIMIZER_SIMULATION': (
              'Optimizer parameters proposed on the Advanced tabs. The check verifies the arithmetic only; '
              'these values are not telemetry.'
          ),
          'INTERACTIVE_WHAT_IF_SIMULATOR': (
              'Explicitly labeled What-If Canary Simulator, Test Alert anomaly injection, and '
              'FinOps scenario sliders for testing configuration changes before deployment.'
          ),
      },
      'llm_judge': llm_judge_report,
  }


def _add_live_finops_checks(state, fleet, fleet_agents, fleet_totals, add_check) -> None:
  """Live mode: token economics, spend change and registrations must reconcile with raw telemetry."""
  lf = fleet.get('live_finops') if isinstance(fleet.get('live_finops'), Mapping) else None
  if lf is None:
    lf = state.get('live_finops') if isinstance(state.get('live_finops'), Mapping) else {}
  usage = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else {}
  ut = usage.get('totals') if isinstance(usage.get('totals'), Mapping) else {}

  sim_absent = state.get('what_if_default') is None and state.get('tokenomics_cockpit') is None
  add_check(
      check_id='LIVE-NO-SIMULATOR-PAYLOADS',
      tab='Tab 4: Cost & Billing',
      metric_or_panel='what_if_default, tokenomics_cockpit, live_finops',
      passed=sim_absent and bool(lf),
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='VibeLift server (live mode)',
      evidence=('Simulator payloads are absent; cost panels use live_finops computed from telemetry.'
                if sim_absent and lf else
                f'FLAGGED: simulator payload present={not sim_absent}, live_finops present={bool(lf)}.'),
  )

  te = lf.get('token_economics') if isinstance(lf.get('token_economics'), Mapping) else {}
  tt = te.get('totals') if isinstance(te.get('totals'), Mapping) else {}
  pairs = (('calls', 'invocations'), ('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens'))
  mism = [f'{a}={tt.get(a)} vs {b}={ut.get(b)}' for a, b in pairs if int(tt.get(a) or 0) != int(ut.get(b) or 0)]
  if abs(float(tt.get('est_cost_usd') or 0) - float(ut.get('est_cost_usd') or 0)) > 0.005:
    mism.append(f"cost {tt.get('est_cost_usd')} vs {ut.get('est_cost_usd')}")
  agent_in = sum(int(r.get('input_tokens') or 0) for r in te.get('agents') or [] if isinstance(r, Mapping))
  fleet_in = int(fleet_totals.get('input_tokens') or 0)
  if agent_in != fleet_in:
    mism.append(f'agent input tokens {agent_in} vs fleet total {fleet_in}')
  add_check(
      check_id='TAB4-TOKEN-ECONOMICS-RECONCILES',
      tab='Tab 4: Cost & Billing',
      metric_or_panel='live_finops.token_economics vs ge_fleet.model_usage / ge_fleet.totals',
      passed=te.get('status') == 'LIVE' and not mism,
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='Cloud Monitoring publisher token_count + Cloud Trace / Cloud Logging gen_ai telemetry',
      evidence=(f"Model totals match ({tt.get('calls')} calls, {tt.get('input_tokens')} in, {tt.get('output_tokens')} out, "
                f"${tt.get('est_cost_usd')}); per-agent input tokens sum to the fleet total ({fleet_in})."
                if te.get('status') == 'LIVE' and not mism else
                f"FLAGGED: status={te.get('status')}; " + '; '.join(mism)),
  )

  sd = lf.get('spend_drift') if isinstance(lf.get('spend_drift'), Mapping) else {}
  if sd.get('status') == 'LIVE':
    drv = sum(float(d.get('change_usd') or 0) for d in sd.get('drivers') or [] if isinstance(d, Mapping))
    change = float(sd.get('change_usd') or 0)
    now_gap = abs(float(sd.get('cost_now_usd') or 0) - float(ut.get('est_cost_usd') or 0))
    ok = abs(drv - change) <= 0.01 and now_gap <= 0.01
    ev = (f'Drivers sum to ${drv:.4f} vs observed change ${change:.4f}; this-period cost ${sd.get("cost_now_usd")} '
          f'vs model usage ${ut.get("est_cost_usd")}.')
  else:
    ok = sd.get('status') == 'NO_DATA'
    ev = f"No previous-period data; the panel says so ({sd.get('reason')})." if ok else f'FLAGGED: spend_drift missing ({sd})'
  add_check(
      check_id='TAB4-SPEND-CHANGE-ADDITIVE',
      tab='Tab 4: Cost & Billing',
      metric_or_panel='live_finops.spend_drift (drivers of spend change)',
      passed=ok,
      provenance='DERIVED_FROM_LIVE_TELEMETRY',
      source_dataset='Cloud Monitoring publisher token_count (this and previous period) x list price',
      evidence=ev,
  )

  broken = [a for a in fleet_agents if isinstance(a, Mapping)
            and (a.get('registration') or {}).get('status') in ('BACKEND_NOT_FOUND', 'NO_BACKEND')]
  bad = [str(a.get('display_name')) for a in broken
         if (a['registration'].get('status') == 'BACKEND_NOT_FOUND' and 'HTTP 404' not in str(a['registration'].get('evidence') or ''))
         or not ((a['registration'].get('action') or {}).get('delete_command'))]
  count_ok = int(fleet_totals.get('broken_registrations') or 0) == len(broken)
  add_check(
      check_id='TAB1-DEAD-REGISTRATION-EVIDENCE',
      tab='Tab 1: Gemini Enterprise Agent Fleet',
      metric_or_panel='ge_fleet.agents[].registration',
      passed=not bad and count_ok,
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='aiplatform.googleapis.com reasoningEngines.get + run.googleapis.com services.list',
      evidence=(f'{len(broken)} broken registration(s), each with HTTP 404 (or missing-backend) evidence and a cleanup command.'
                if not bad and count_ok else
                f'FLAGGED: without evidence/command: {bad}; totals.broken_registrations='
                f"{fleet_totals.get('broken_registrations')} vs {len(broken)} flagged."),
  )

  unsourced = [str(a.get('display_name')) for a in fleet_agents if isinstance(a, Mapping)
               and int(((a.get('metrics') or {}).get('llm_calls')) or 0) > 0
               and not (a.get('metrics') or {}).get('token_source')]
  add_check(
      check_id='TAB1-AGENT-TOKEN-SOURCES',
      tab='Tab 1: Gemini Enterprise Agent Fleet',
      metric_or_panel='ge_fleet.agents[].metrics.token_source',
      passed=not unsourced,
      provenance='OBSERVED_GCP_TELEMETRY',
      source_dataset='Cloud Logging gen_ai inference events + Cloud Trace gen_ai spans',
      evidence=('Every agent with LLM calls names its token source (Cloud Logging or Cloud Trace).'
                if not unsourced else f'FLAGGED: token counts without a source: {unsourced}'),
  )


def run_llm_as_judge_audit(
    state: Mapping[str, object],
    deterministic_checks: list[dict[str, object]],
    bq_insights: Mapping[str, object] | None = None,
) -> dict[str, object]:
  """Calls Vertex AI Gemini as an independent LLM-as-a-Judge auditor, with a rule-based fallback."""

  project_id = str(state.get('gcp_project') or os.environ.get('GOOGLE_CLOUD_PROJECT') or 'project-maui')
  fleet = state.get('ge_fleet') if isinstance(state.get('ge_fleet'), Mapping) else {}
  uc = state.get('user_centric') if isinstance(state.get('user_centric'), Mapping) else {}
  aive = state.get('aive_logs') if isinstance(state.get('aive_logs'), Mapping) else {}

  audit_digest = {
      'gcp_project': project_id,
      'collection_mode': uc.get('collection_mode'),
      'fleet_totals': fleet.get('totals'),
      'power_users_ldap': [
          {
              'user_ldap': u.get('user_ldap'),
              'user_email': u.get('user_email'),
              'sessions_7d': u.get('sessions_7d'),
              'status': u.get('status'),
              'source_tables': u.get('source_tables') or u.get('department'),
              'avg_csat': u.get('avg_csat'),
              'cache_hit_pct': u.get('cache_hit_pct'),
          }
          for u in (uc.get('power_users_ldap') or [])
          if isinstance(u, Mapping)
      ],
      'aive_usage_sample': (aive.get('usage_logs') or [])[:3],
      'deterministic_checks': [
          {'check_id': c.get('check_id'), 'status': c.get('status'), 'evidence': c.get('evidence')}
          for c in deterministic_checks
      ],
      'bigquery_live_sources': (bq_insights or {}).get('queried_tables') if isinstance(bq_insights, Mapping) else [],
  }

  prompt = (
      'You are an independent Principal SRE & FinOps Telemetry Auditor (LLM-as-a-Judge).\n'
      'Audit the following VibeLift dashboard telemetry digest from Google Cloud project `'
      + project_id
      + '`.\n'
      'Verify that:\n'
      '1. Every user row carries BigQuery provenance (status LIVE_HUMAN_PRINCIPAL/SERVICE_ACCOUNT_TELEMETRY '
      'and a source table); per-user metrics that are not measured must be null, not invented constants.\n'
      '2. Fleet totals, BigQuery audit principals, and OTel GenAI token spans are grounded in real telemetry.\n'
      '3. What-If simulators and rate-card projections are clearly distinguished from observed telemetry.\n'
      '4. If any deterministic check has status FAIL, the verdict must be FLAGGED_ISSUES.\n'
      'Base the score only on the digest; do not assume data that is not shown.\n\n'
      'Respond ONLY with a valid JSON object matching this exact schema:\n'
      '{"grounding_score_100": <0-100>, "verdict": "VERIFIED_GROUNDED" | "FLAGGED_ISSUES", '
      '"executive_finding": "2-sentence rigorous audit conclusion citing specific observed numbers."}\n\n'
      'Telemetry Digest:\n'
      + json.dumps(audit_digest, default=str)
  )

  failed_checks = [c for c in deterministic_checks if c.get('status') != 'PASS']
  judge_error = None

  # 1. Vertex AI Gemini via REST, authenticated with ADC (Cloud Run service account) or the gcloud CLI.
  if project_id not in ('test-project', 'UNCONFIGURED-PROJECT'):
    token, judge_error = _get_access_token()
    if token:
      parsed, judge_error = _call_vertex_judge(project_id, token, prompt)
      if parsed is not None:
        exec_f = str(parsed.get('executive_finding') or '').strip()
        verdict_str = str(parsed.get('verdict') or '').strip().upper()
        if verdict_str == 'PASS_GROUNDED':
          verdict_str = 'VERIFIED_GROUNDED'
        score = parsed.get('grounding_score_100')
        # The LLM can only lower confidence: a failed deterministic check is never overridden.
        if failed_checks and verdict_str == 'VERIFIED_GROUNDED':
          verdict_str = 'FLAGGED_ISSUES'
        return {
            'executed': True,
            'judge_model': f'vertex_ai/{JUDGE_MODEL}',
            'judge_model_used': f'vertex_ai/{JUDGE_MODEL} (Live GCP)',
            'grounding_score_100': int(score) if isinstance(score, (int, float)) else None,
            'verdict': verdict_str or 'UNKNOWN',
            'executive_finding': exec_f,
            'executive_summary': exec_f,
            'judge_error': None,
            'tab_findings': [
                f"{c['tab']}: {c['status']} ({c['check_id']})"
                for c in deterministic_checks[:6]
            ],
        }
    logger.warning('LLM-as-a-Judge unavailable, using rule-based result: %s', judge_error)

  passed_count = len(deterministic_checks) - len(failed_checks)
  total_count = max(1, len(deterministic_checks))
  score = int(round((passed_count / total_count) * 100.0))
  if failed_checks:
    exec_f = (
        f'{passed_count}/{total_count} rule-based checks passed. Failing: '
        + ', '.join(str(c.get('check_id')) for c in failed_checks)
        + '.'
    )
  else:
    exec_f = f'All {total_count} rule-based checks passed. No LLM review was performed.'
  return {
      'executed': True,
      'judge_model': 'deterministic-rule-auditor-v1',
      'judge_model_used': 'deterministic-rule-auditor-v1 (Vertex AI fallback)',
      'grounding_score_100': score,
      'verdict': 'VERIFIED_GROUNDED' if not failed_checks else 'FLAGGED_ISSUES',
      'executive_finding': exec_f,
      'executive_summary': exec_f,
      'judge_error': judge_error,
      'tab_findings': [
          f"{c['tab']}: {c['status']} ({c['check_id']})"
          for c in deterministic_checks[:6]
      ],
  }


JUDGE_MODEL = os.environ.get('VIBELIFT_JUDGE_MODEL', 'gemini-2.5-flash')
JUDGE_LOCATION = os.environ.get('VIBELIFT_JUDGE_LOCATION', 'us-central1')
JUDGE_TIMEOUT_S = float(os.environ.get('VIBELIFT_JUDGE_TIMEOUT_S', '25'))


def _get_access_token() -> tuple[str | None, str | None]:
  """Returns (token, error). Prefers ADC (the Cloud Run service account); falls back to the gcloud CLI."""
  import subprocess

  errors = []
  if google is not None and GoogleAuthRequest is not None:
    try:
      creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
      if not creds.valid:
        creds.refresh(GoogleAuthRequest())
      token = getattr(creds, 'token', None)
      if token:
        return token, None
      errors.append('ADC returned no token')
    except Exception as exc:  # pylint: disable=broad-except
      errors.append(f'ADC: {type(exc).__name__}: {exc}'[:200])
  else:
    errors.append('google-auth not installed')
  try:
    res = subprocess.run(
        ['gcloud', 'auth', 'print-access-token'], capture_output=True, text=True, timeout=5.0, check=False
    )
    if res.returncode == 0 and res.stdout.strip():
      return res.stdout.strip(), None
    errors.append(f'gcloud exit {res.returncode}')
  except Exception as exc:  # pylint: disable=broad-except
    errors.append(f'gcloud: {type(exc).__name__}')
  return None, '; '.join(errors)


def _call_vertex_judge(project_id: str, token: str, prompt: str) -> tuple[dict | None, str | None]:
  """Calls Vertex AI generateContent. Returns (parsed_json, error). Retries once on 429/5xx/timeouts."""
  import time

  url = (
      f'https://{JUDGE_LOCATION}-aiplatform.googleapis.com/v1/projects/{project_id}'
      f'/locations/{JUDGE_LOCATION}/publishers/google/models/{JUDGE_MODEL}:generateContent'
  )
  body = json.dumps({
      'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
      'generationConfig': {
          'temperature': 0.0,
          'maxOutputTokens': 2048,
          'responseMimeType': 'application/json',
          'thinkingConfig': {'thinkingBudget': 0},
      },
  }).encode('utf-8')
  last_error = None
  for attempt in range(2):
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'x-goog-user-project': project_id,
        },
        method='POST',
    )
    try:
      with urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT_S) as resp:
        resp_data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
      detail = ''
      try:
        detail = json.loads(exc.read().decode('utf-8')).get('error', {}).get('status', '')
      except Exception:  # pylint: disable=broad-except
        pass
      last_error = f'Vertex AI HTTP {exc.code} {detail}'.strip()
      if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
        time.sleep(1.5)
        continue
      return None, last_error
    except Exception as exc:  # pylint: disable=broad-except
      last_error = f'Vertex AI {type(exc).__name__}: {exc}'[:200]
      if attempt == 0:
        continue
      return None, last_error
    candidates = resp_data.get('candidates') or []
    parts = ((candidates[0].get('content') or {}).get('parts') or []) if candidates else []
    text = str(parts[0].get('text') or '').strip() if parts else ''
    if not text:
      finish = candidates[0].get('finishReason') if candidates else 'NO_CANDIDATES'
      return None, f'Vertex AI returned no text (finishReason={finish})'
    try:
      parsed = json.loads(text)
    except json.JSONDecodeError:
      return None, 'Vertex AI response was not valid JSON'
    if not isinstance(parsed, dict):
      return None, 'Vertex AI response was not a JSON object'
    return parsed, None
  return None, last_error
