"""HTTP server, FastAPI app, and REST API for VibeLift Analytics Platform on Google Cloud."""

import base64
import http.server
import json
import logging
import os
import socket
import sys
import urllib.parse
from collections.abc import Mapping, Sequence

try:
  from absl import app as absl_app
  from absl import flags
except ImportError:
  absl_app = None
  flags = None

try:
  import fastapi
  from fastapi.middleware.cors import CORSMiddleware
  from fastapi.responses import HTMLResponse, JSONResponse, Response
except ImportError:
  fastapi = None

from vibelift import billing_export, gcp_telemetry, long_running_agent, mcp_server, sme_eval, telemetry
from vibelift import finops as live_finops
from vibelift import fleet as ge_fleet
from vibelift import optimizer as alpha_evolve_optimizer
from vibelift import validator as telemetry_validator
from vibelift.ui import logo_asset
from vibelift.ui import template as ui_template

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


import threading


class VibeLiftRuntimeController:
  """Coordinates the selectable Gemini Enterprise agents, AlphaEvolve optimizer, GCP telemetry, and GE fleet."""

  def __init__(self, fleet_service: ge_fleet.GeminiEnterpriseFleetService | None = None) -> None:
    """Initializes the optimizer, long-running agent, GCP telemetry client, and fleet service."""
    self._lock = threading.Lock()
    self._warmer_started = False
    self._last_fleet_payload: dict[str, object] | None = None
    self.optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    self.agent = long_running_agent.LongRunningVibeLiftAgent(
        self.optimizer,
        agent_name=self.optimizer.active_agent.agent_id,
        model=self.optimizer.active_agent.model,
    )
    self.gcp_telemetry = gcp_telemetry.GoogleCloudTelemetryService()
    self.billing_export = billing_export.BillingExportReader(
        self.gcp_telemetry.project_id, self.gcp_telemetry._get_access_token  # pylint: disable=protected-access
    )
    self.ge_fleet = fleet_service or ge_fleet.get_ge_fleet_service()

  def _is_live_gcp(self) -> bool:
    """Returns True when connected to a real GCP project (not the offline unit-test fake)."""
    proj = str(getattr(self.ge_fleet, 'project_id', '') or '').strip()
    return bool(proj and proj not in ('test-project', gcp_telemetry.UNCONFIGURED_PROJECT_ID))

  def start_background_warmer(self, interval_s: float = 45.0) -> None:
    """Starts a daemon thread that keeps the GE fleet and GCP telemetry caches warm on Cloud Run."""
    with self._lock:
      if self._warmer_started:
        return
      self._warmer_started = True

    def _warm_loop() -> None:
      import time
      while True:
        try:
          self.get_fleet_payload(window_hours=24, force_refresh=False)
          self.gcp_telemetry.list_cloud_run_agent_services(force_refresh=True)
          self.gcp_telemetry.fetch_gemini_enterprise_support_telemetry(limit=6, force_refresh=True)
          if self._is_live_gcp():
            self.gcp_telemetry.fetch_live_bigquery_project_insights(force_refresh=True)
        except Exception:
          logger.debug('Background cache warmer iteration failed', exc_info=True)
        time.sleep(max(15.0, interval_s))

    threading.Thread(target=_warm_loop, daemon=True, name='vibelift-fleet-warmer').start()

  def get_fleet_payload(
      self,
      window_hours: int | None = None,
      force_refresh: bool = False,
      allow_stale: bool = False,
      max_wait_s: float | None = None,
  ) -> dict[str, object]:
    """Returns the live Gemini Enterprise agent fleet (inventory joined with real telemetry)."""
    try:
      fleet = self.ge_fleet.collect(
          window_hours=window_hours,
          force_refresh=force_refresh,
          allow_stale=allow_stale,
          max_wait_s=max_wait_s,
      )
      bq_insights = None
      if self._is_live_gcp():
        bq_insights = self.gcp_telemetry.fetch_live_bigquery_project_insights(
            non_blocking=allow_stale, window_hours=window_hours
        )
      with self._lock:
        self.optimizer.sync_from_ge_fleet(fleet, bq_insights=bq_insights)
      # Token economics / spend change for the same window as the fleet (shallow copy: fleet is cached).
      fleet = dict(fleet)
      fleet['live_finops'] = self._live_finops(fleet)
      self._last_fleet_payload = fleet
      return fleet
    except Exception:  # collect() degrades per source; this guards against unexpected failures.
      logger.exception('Gemini Enterprise fleet collection failed')
      return {
          'source': 'gemini_enterprise',
          'project_id': self.ge_fleet.project_id,
          'engines': [],
          'agents': [],
          'totals': {'agents': 0},
          'source_status': {'inventory': 'error'},
          'errors': [{'source': 'Fleet collector', 'detail': 'Collection failed; see server logs.'}],
          'notes': [],
      }

  def get_state_payload(
      self,
      include_fleet: bool = True,
      window_hours: int | None = None,
      fast_mcp: bool = False,
  ) -> dict[str, object]:
    """Builds the JSON payload for the dashboard (optimization tabs plus, optionally, the live GE fleet)."""
    fleet_payload = None
    if include_fleet:
      fleet_payload = self.get_fleet_payload(
          window_hours=window_hours,
          allow_stale=fast_mcp,
          max_wait_s=0.35 if fast_mcp else None,
      )

    bq_insights = None
    if self._is_live_gcp():
      bq_insights = self.gcp_telemetry.fetch_live_bigquery_project_insights(
          non_blocking=fast_mcp, window_hours=window_hours
      )
      if isinstance(bq_insights, dict) and bq_insights:
        usage_rows = bq_insights.get('aive_usage_logs')
        rating_rows = bq_insights.get('aive_ratings_logs')
        dec_rows = bq_insights.get('decorator_events')
        if isinstance(usage_rows, list) and usage_rows:
          telemetry.set_live_aive_logs(
              usage_rows,
              rating_rows if isinstance(rating_rows, list) else [],
          )
        if isinstance(dec_rows, list) and dec_rows:
          telemetry.set_live_decorator_events(dec_rows)
        with self._lock:
          self.optimizer.sync_from_ge_fleet(
              fleet_payload or self.optimizer._live_fleet_payload or {'project_id': self.ge_fleet.project_id, 'agents': []},
              bq_insights=bq_insights,
          )

    with self._lock:
      turns = list(self.agent.turns)
      active_agent_dict = self.optimizer.active_agent.to_dict()
      available_agents = self.optimizer.list_agents_summary()
      all_agents = self.optimizer.get_all_agents_dict()
      optimizer_platforms = self.optimizer.get_optimizer_platforms_payload()
      user_centric = self.optimizer.get_user_centric_payload()
      otel_catalog = self.optimizer.get_otel_catalog_payload()
      billing_reconciliation = self.optimizer.get_finops_billing_reconciliation_payload()
      tokenomics_cockpit = self.optimizer.get_tokenomics_and_cockpit_finops_payload()
      persona_playbooks = self.optimizer.get_sme_persona_playbooks()
      what_if_default = self.optimizer.simulate_what_if_scenario()
      nl2sql_default = self.optimizer.execute_nl2sql_telemetry_query(
          'Compare cost per 1k turns and prompt cache savings across agents'
      )
      steps = list(self.agent.step_descriptions)

    services = self.gcp_telemetry.list_cloud_run_agent_services(non_blocking=fast_mcp)
    support_events = self.gcp_telemetry.fetch_gemini_enterprise_support_telemetry(
        limit=6, non_blocking=fast_mcp
    )
    live_gcp = self._is_live_gcp()
    live_finops_payload = None
    ge_daily_usage = None
    if live_gcp:
      # Real invoice data comes only from a Cloud Billing export; otherwise the panel says it is not connected.
      billing_reconciliation = self.billing_export.get(non_blocking=True)
      ge_daily_usage = self._ge_daily_usage(bq_insights)
      # The tokenomics cockpit and what-if simulator are parametric models with example inputs.
      # In live mode they are replaced by figures computed from observed telemetry only.
      tokenomics_cockpit = None
      what_if_default = None
      live_finops_payload = self._live_finops(fleet_payload or self.optimizer._live_fleet_payload)
      # Never show demo personas in live mode: if BigQuery principals have not
      # loaded yet, show an empty table with an explicit loading status.
      live_bq = bq_insights if isinstance(bq_insights, dict) else self.optimizer._live_bq_insights
      user_centric = dict(user_centric)
      if isinstance(live_bq, dict):
        user_centric['ge_sessions'] = list(live_bq.get('ge_sessions') or [])
        user_centric['ge_session_turns'] = dict(live_bq.get('ge_session_turns') or {})
        user_centric['ge_mart_refreshed_at'] = live_bq.get('ge_mart_refreshed_at')
        user_centric['ge_mart_dataset'] = live_bq.get('ge_mart_dataset')
      if not (isinstance(live_bq, dict) and live_bq.get('power_users_ldap')):
        user_centric['power_users_ldap'] = []
        user_centric['collection_mode'] = (
            'LIVE GCP: BigQuery principals still loading (no demo data shown)'
        )
    if not support_events and not live_gcp:
      support_events = [
          {
              'event_timestamp': '2026-09-29T04:12:18Z',
              'session_id': '6446120131357637190',
              'triage_tier': 'L1_AUTO_RESOLVED',
              'agent_id': 'it_service_desk',
              'intent_category': 'VPN_SSO_CERT_RENEWAL',
              'latency_ms': 580,
              'cache_hit_pct': 92.4,
              'resolution_status': 'RESOLVED_FIRST_CONTACT (CSAT 5★)',
          },
          {
              'event_timestamp': '2026-09-29T03:58:04Z',
              'session_id': '7192837465102938471',
              'triage_tier': 'L2_REASONING_ENGINE_ESCALATION',
              'agent_id': 'it_service_desk',
              'intent_category': 'CORP_FIREWALL_VPC_SC_PERIMETER',
              'latency_ms': 690,
              'cache_hit_pct': 91.2,
              'resolution_status': 'HANDOFF_TO_RE_8821150342449201152 (0 Errors)',
          },
          {
              'event_timestamp': '2026-09-29T03:41:50Z',
              'session_id': '8391029384756102938',
              'triage_tier': 'L1_AUTO_RESOLVED',
              'agent_id': 'deep_research',
              'intent_category': 'SEC_10K_COMPETITIVE_SYNTHESIS',
              'latency_ms': 1120,
              'cache_hit_pct': 89.6,
              'resolution_status': 'DEFERRED_LANE_BATCH_COMPLETE (-50% Rate)',
          },
      ]

    turn_summary_dict = dict(telemetry.summarize_log_stream(turns))
    payload: dict[str, object] = {
        'agent_name': self.agent.agent_name,
        'model': self.agent.model,
        'gcp_project': self.gcp_telemetry.project_id,
        'gcp_region': self.gcp_telemetry.region,
        'gcp_services': services,
        'cloud_run_services': services,
        'gemini_enterprise_support_events': support_events,
        'active_agent': active_agent_dict,
        'available_agents': available_agents,
        'all_agents': all_agents,
        'optimizer_platforms': optimizer_platforms,
        'user_centric': user_centric,
        'otel_catalog': otel_catalog,
        'billing_reconciliation': billing_reconciliation,
        'tokenomics_cockpit': tokenomics_cockpit,
        'persona_playbooks': persona_playbooks,
        'what_if_default': what_if_default,
        'live_finops': live_finops_payload,
        'ge_daily_usage': ge_daily_usage,
        'live_data': live_gcp,
        'aive_logs': telemetry.get_recent_aive_logs(live_only=live_gcp),
        'nl2sql_default': nl2sql_default,
        'decorator_events': telemetry.get_recent_decorator_events(),
        'summary': turn_summary_dict,
        'turn_summary': turn_summary_dict,
        'turns': [t.to_dict() for t in turns],
        'steps': steps,
    }
    if include_fleet and fleet_payload is not None:
      payload['ge_fleet'] = fleet_payload
    effective_fleet = (
        fleet_payload
        or self._last_fleet_payload
        or (self.optimizer._live_fleet_payload if self._is_live_gcp() else None)
    )
    if effective_fleet is None and not self._is_live_gcp():
      effective_fleet = self.get_fleet_payload(allow_stale=False)
    payload['telemetry_validation'] = telemetry_validator.validate_dashboard_state(
        payload,
        ge_fleet_payload=effective_fleet,
        bq_insights=bq_insights,
        run_llm_judge=False,
    )
    payload['sme_evaluation'] = sme_eval.evaluate_persona_rubric(
        payload,
        ge_fleet_payload=effective_fleet,
        rendered_html=ui_template.render_dashboard_html(),
    )
    return payload

  def get_sme_evaluation(self, run_fresh: bool = False) -> dict[str, object]:
    """Returns the 6-persona SME evaluation scorecard (automated rubric + human SME ratings)."""
    state = self.get_state_payload(include_fleet=True, fast_mcp=False)
    fleet = state.get('ge_fleet') if isinstance(state.get('ge_fleet'), Mapping) else self._last_fleet_payload
    return sme_eval.evaluate_persona_rubric(
        state,
        ge_fleet_payload=fleet,
        rendered_html=ui_template.render_dashboard_html(),
    )

  def submit_sme_rating(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Records a human SME evaluation rating for a persona and returns updated state."""
    entry = sme_eval.get_sme_evaluation_store().submit_rating(body or {})
    state = self.get_state_payload(include_fleet=False)
    return {
        'status': 'recorded',
        'rating': entry,
        'sme_evaluation': state.get('sme_evaluation'),
        'state': state,
    }

  def validate_telemetry(self, run_llm_judge: bool = True) -> dict[str, object]:
    """Runs the deterministic + optional Vertex AI LLM-as-a-Judge telemetry grounding validator."""
    state = self.get_state_payload(include_fleet=True, fast_mcp=False)
    bq_insights = self.gcp_telemetry.fetch_live_bigquery_project_insights(non_blocking=True) if self._is_live_gcp() else None
    fleet = state.get('ge_fleet') if isinstance(state.get('ge_fleet'), Mapping) else None
    return telemetry_validator.validate_dashboard_state(
        state,
        ge_fleet_payload=fleet,
        bq_insights=bq_insights,
        run_llm_judge=run_llm_judge,
    )

  def _rate_cards(self) -> dict[str, dict[str, float]]:
    getter = getattr(self.ge_fleet, 'rate_cards', None)
    return getter() if callable(getter) else {}

  def _ge_daily_usage(self, bq_insights: object) -> dict[str, object] | None:
    """Per-day GE usage (vibelift_mart.fct_turns) joined with billed AI spend (billing export)."""
    if not isinstance(bq_insights, Mapping) or not isinstance(bq_insights.get('ge_daily_totals'), list):
      return None
    cost = self.billing_export.get_daily_ai_costs(non_blocking=True)
    proj = self.gcp_telemetry.project_id
    return {
        'source': f"{bq_insights.get('ge_mart_dataset')}.fct_turns + Cloud Billing export",
        'mart_dataset': bq_insights.get('ge_mart_dataset'),
        'curated_dataset': bq_insights.get('ge_curated_dataset'),
        'refreshed_at': bq_insights.get('ge_mart_refreshed_at'),
        'refresh_cli': f'python3 deploy/bigquery/provision_ge_mart.py --project {proj} --refresh',
        'billing_status': cost.get('status'),
        'billing_message': cost.get('message'),
        'billing_table': cost.get('table'),
        'currency': cost.get('currency'),
        'cost_scope': 'Project-level spend on AI services; not allocated to agents or users.',
        'days': billing_export.join_daily_usage_with_cost(list(bq_insights['ge_daily_totals']), cost),
        'by_app_agent_model': list(bq_insights.get('ge_daily_by_app') or []),
        'sessions': list(bq_insights.get('ge_sessions') or []),
    }

  def get_user_centric_finops_payload(self, window_hours: int | None = None) -> dict[str, object]:
    """User-centric FinOps plus per-user session token rollup and per-session turn token drilldown."""
    state = self.get_state_payload(include_fleet=False, window_hours=window_hours)
    uc = state.get('user_centric') if isinstance(state.get('user_centric'), Mapping) else {}
    return {
        'project_id': self.gcp_telemetry.project_id,
        'mode': 'LIVE_GCP' if self._is_live_gcp() else 'DEMO',
        'user_centric': uc,
        'session_drilldown': live_finops.build_session_token_drilldown(
            uc.get('ge_sessions') if isinstance(uc.get('ge_sessions'), list) else [],
            uc.get('ge_session_turns') if isinstance(uc.get('ge_session_turns'), Mapping) else {},
        ),
    }

  def refresh_ge_mart(self) -> dict[str, object]:
    """Rebuilds vibelift_mart.fct_turns from v_fct_turns and returns the refreshed state."""
    result = self.gcp_telemetry.refresh_ge_mart_turns()
    if self._is_live_gcp():
      self.gcp_telemetry.fetch_live_bigquery_project_insights(force_refresh=True, non_blocking=False)
    return {'refresh': result, 'state': self.get_state_payload(include_fleet=False)}

  def _live_finops(self, fleet: Mapping[str, object] | None) -> dict[str, object]:
    payload = live_finops.build_live_finops(fleet)
    payload['rate_card_models'] = sorted(self._rate_cards())
    return payload

  def what_if_live(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Projection from observed model usage (model switch or cache share); never uses example inputs."""
    raw = body or {}
    fleet = self.get_fleet_payload(
        window_hours=ge_fleet.parse_window_hours(raw.get('window_hours')), allow_stale=True, max_wait_s=20.0)
    usage = fleet.get('model_usage') if isinstance(fleet, Mapping) else None
    cards = self._rate_cards()
    try:
      if str(raw.get('kind') or 'model_switch') == 'cache_share':
        return live_finops.project_cache_share(
            usage, str(raw.get('model') or ''), float(raw.get('target_cache_share_pct') or 0), cards)
      return live_finops.project_model_switch(
          usage, str(raw.get('from_model') or ''), str(raw.get('to_model') or ''),
          float(raw.get('share_pct') or 0), cards)
    except (TypeError, ValueError):
      return {'status': 'ERROR', 'error': 'Invalid numeric input.'}

  def recompute_finops(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Recomputes the deterministic Tokenomics & AgentOps Cockpit FinOps payload for given assumptions."""
    if self._is_live_gcp():
      return {
        'status': 'DISABLED_IN_LIVE_MODE',
        'reason': ('This parametric simulator uses example inputs, so it is off when connected to a real '
                   'project. Use /api/what_if_live for projections from observed usage.'),
    }
    with self._lock:
      return self.optimizer.get_tokenomics_and_cockpit_finops_payload(body)

  def query_nl2sql(self, question: str) -> dict[str, object]:
    """Executes an interactive NL2SQL telemetry query and returns SQL + rows + executive summary."""
    with self._lock:
      return self.optimizer.execute_nl2sql_telemetry_query(question)

  def simulate_what_if(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Runs an interactive What-If FinOps & Canary scenario on the currently selected agent."""
    if self._is_live_gcp():
      return {
        'status': 'DISABLED_IN_LIVE_MODE',
        'reason': ('This parametric simulator uses example inputs, so it is off when connected to a real '
                   'project. Use /api/what_if_live for projections from observed usage.'),
    }
    raw = body or {}
    model_tier = str(raw.get('model_tier') or 'gemini-3.1-flash-tier-routed')
    try:
      thinking_budget_tok = int(float(str(raw.get('thinking_budget_tok') if raw.get('thinking_budget_tok') is not None else 1024)))
    except (TypeError, ValueError):
      thinking_budget_tok = 1024
    try:
      history_window_turns = int(float(str(raw.get('history_window_turns') if raw.get('history_window_turns') is not None else 6)))
    except (TypeError, ValueError):
      history_window_turns = 6
    try:
      traffic_canary_pct = int(float(str(raw.get('traffic_canary_pct') if raw.get('traffic_canary_pct') is not None else 15)))
    except (TypeError, ValueError):
      traffic_canary_pct = 15
    with self._lock:
      return self.optimizer.simulate_what_if_scenario(
          model_tier=model_tier,
          thinking_budget_tok=thinking_budget_tok,
          history_window_turns=history_window_turns,
          traffic_canary_pct=traffic_canary_pct,
      )

  def submit_csat_rating(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Records a Voice-of-Customer CSAT rating into `aive_logs.ratings_log` and returns updated state."""
    raw = body or {}
    try:
      rating = int(float(str(raw.get('rating') or 5)))
    except (TypeError, ValueError):
      rating = 5
    telemetry.log_csat_rating(
        session_id=str(raw.get('session_id') or '6446120131357637190'),
        event_id=str(raw.get('event_id') or 'evt-9f81c204-aive'),
        user_email=str(raw.get('user_email') or 'sme-demo@example.com'),
        rating=rating,
        feedback_text=str(raw.get('feedback_text') or 'Verified SME closed-loop optimization guardrail.'),
    )
    return self.get_state_payload(include_fleet=False)

  def ingest_aive_log(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Ingests a @with_analytics_logging event into `aive_logs` and the real-time decorator stream."""
    raw = body or {}
    active = self.optimizer.active_agent
    telemetry.log_agent_generation_event(
        session_id=str(raw.get('session_id') or '6446120131357637190'),
        user_email=str(raw.get('user_email') or 'sme-demo@example.com'),
        company_name=str(raw.get('company_name') or 'Google Cloud'),
        department=str(raw.get('department') or 'Cloud AI & Agent Platform'),
        task_type=str(raw.get('task_type') or 'FLEET_OPTIMIZATION_AUDIT'),
        prompts=[str(raw.get('prompt') or 'Execute @with_analytics_logging telemetry turn')],
        outputs=[{
            'gcs_uri': str(raw.get('gcs_uri') or f'gs://vibelift-aive-assets/{active.agent_id}_turn.json'),
            'media_type': str(raw.get('media_type') or 'APPLICATION_JSON'),
            'mime_type': str(raw.get('mime_type') or 'application/json'),
        }],
        total_tokens=int(float(str(raw.get('total_tokens') or 19400))),
        model_name=str(raw.get('model_name') or active.model),
        latency_ms=float(str(raw.get('latency_ms') or 565.0)),
        status=str(raw.get('status') or 'SUCCESS'),
        agent_name=active.agent_id,
    )
    return self.get_state_payload(include_fleet=False)

  def sync_gcp_telemetry(self, window_hours: int | None = None) -> dict[str, object]:
    """Fetches live Cloud Logging turns from GCP and ingests them into the active runtime."""
    live_turns = self.gcp_telemetry.fetch_live_cloud_turns(hours_ago=window_hours or 48, max_results=15)
    with self._lock:
      if live_turns:
        self.agent.ingest_gcp_cloud_turns(live_turns)
    return self.get_state_payload(include_fleet=False, window_hours=window_hours)

  def select_agent(self, agent_id: str) -> dict[str, object]:
    """Switches the active agent being analyzed and optimized."""
    with self._lock:
      self.optimizer.select_agent(agent_id)
      self.agent = long_running_agent.LongRunningVibeLiftAgent(
          self.optimizer,
          agent_name=self.optimizer.active_agent.agent_id,
          model=self.optimizer.active_agent.model,
      )
    return self.get_state_payload(include_fleet=False)

  def select_optimizer(self, platform_id: str) -> dict[str, object]:
    """Switches the active optimization platform (AlphaEvolve, Opus Critic, Vizier, or Hybrid)."""
    with self._lock:
      self.optimizer.select_optimizer_platform(platform_id)
    return self.get_state_payload(include_fleet=False)

  def ingest_decorator_event(self, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """Records a real-time @vibelift_telemetry decorator event from an agent message-passing hook."""
    import time
    raw = body or {}
    active = self.optimizer.active_agent

    def _safe_float(key: str, default: float) -> float:
      val = raw.get(key)
      if val is None or val == '':
        return default
      try:
        return max(0.0, float(val))
      except (TypeError, ValueError):
        return default

    def _safe_int(key: str, default: int) -> int:
      val = raw.get(key)
      if val is None or val == '':
        return default
      try:
        return max(0, int(val))
      except (TypeError, ValueError):
        return default

    prompt_tok = _safe_int('prompt_tokens', 19200)
    cached_tok = min(prompt_tok, _safe_int('cached_tokens', 17680))
    event = telemetry.DecoratorTelemetryEvent(
        timestamp=str(raw.get('timestamp') or time.strftime('%H:%M:%S UTC', time.gmtime()))[:64],
        agent_name=str(raw.get('agent_name') or active.agent_id)[:64],
        handler_name=str(raw.get('handler_name') or 'on_message_passing_turn')[:80],
        protocol=str(raw.get('protocol') or 'ADK / MCP Decorator Stream')[:80],
        model=str(raw.get('model') or active.model)[:64],
        latency_ms=_safe_float('latency_ms', 585.0),
        prompt_tokens=prompt_tok,
        cached_tokens=cached_tok,
        output_tokens=_safe_int('output_tokens', 320),
        context_bloat_pct=min(100.0, _safe_float('context_bloat_pct', 12.4)),
        idle_ratio_pct=min(100.0, _safe_float('idle_ratio_pct', 6.8)),
        skill_or_mcp=str(raw.get('skill_or_mcp') or f'mcp://{active.agent_id}/stream')[:96],
        user_cohort=str(raw.get('user_cohort') or 'Enterprise Active DAU Cohort')[:96],
        status=str(raw.get('status') or '200 OK (@vibelift_telemetry)')[:64],
    )
    telemetry.record_decorator_event(event)
    return self.get_state_payload(include_fleet=False)

  def add_parameter(
      self,
      label: str,
      unit: str,
      direction: str,
      baseline_val: float,
      target_val: float,
      weight_pct: int,
  ) -> dict[str, object]:
    """Adds a custom user-defined parameter to the currently selected agent."""
    with self._lock:
      self.optimizer.add_user_parameter(
          label=label,
          unit=unit,
          direction=direction,
          baseline_val=baseline_val,
          target_val=target_val,
          weight_pct=weight_pct,
      )
    return self.get_state_payload(include_fleet=False)

  def inject_anomaly(self) -> dict[str, object]:
    """Simulates a production log anomaly (cache bust + latency/cost spike)."""
    with self._lock:
      self.optimizer.inject_anomaly()
    return self.get_state_payload(include_fleet=False)

  def step_turn(self) -> dict[str, object]:
    """Executes one long-running agent turn and returns updated state."""
    with self._lock:
      self.agent.step_turn()
    return self.get_state_payload(include_fleet=False)

  def evolve_generation(self) -> dict[str, object]:
    """Runs the next AlphaEvolve Pareto generation and steps the agent."""
    with self._lock:
      self.optimizer.run_next_generation()
      self.agent.step_turn()
    return self.get_state_payload(include_fleet=False)

  def reset(self) -> dict[str, object]:
    """Resets the optimizer and agent trajectory to the initial seed state."""
    with self._lock:
      current_id = self.optimizer.active_agent.agent_id
      self.optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
      self.optimizer.select_agent(current_id)
      self.agent = long_running_agent.LongRunningVibeLiftAgent(
          self.optimizer,
          agent_name=self.optimizer.active_agent.agent_id,
          model=self.optimizer.active_agent.model,
      )
      telemetry.reset_decorator_events()
      telemetry.reset_aive_logs()
      sme_eval.get_sme_evaluation_store().clear()
    return self.get_state_payload(include_fleet=False)


# Shared singleton runtime controller
_global_controller = VibeLiftRuntimeController()
if os.environ.get('K_SERVICE') or os.environ.get('VIBELIFT_BACKGROUND_WARMER') == '1':
  _global_controller.start_background_warmer()


class VibeLiftHttpServer(http.server.ThreadingHTTPServer):
  """Dual-stack IPv6/IPv4 HTTP server for Cloud Run, local development, and test environments."""

  allow_reuse_address = True

  def __init__(
      self,
      server_address: tuple[str, int],
      controller: VibeLiftRuntimeController,
  ) -> None:
    """Initializes the HTTP server with IPv6 dual-stack when host is ::."""
    self.controller = controller
    if ':' in server_address[0]:
      self.address_family = socket.AF_INET6
    super().__init__(server_address, VibeLiftRequestHandler)

  def server_bind(self) -> None:
    """Enables dual-stack IPv4+IPv6 when bound to AF_INET6."""
    if self.address_family == socket.AF_INET6:
      try:
        self.socket.setsockopt(
            socket.IPPROTO_IPV6,
            socket.IPV6_V6ONLY,
            0,
        )
      except OSError:
        pass
    super().server_bind()


class VibeLiftRequestHandler(http.server.BaseHTTPRequestHandler):
  """Handles HTML UI and JSON API requests for VibeLift Analytics Platform."""

  def _read_json_body(self) -> dict[str, object]:
    """Reads and parses optional JSON request body."""
    length_str = self.headers.get('Content-Length', '0')
    length = int(length_str) if length_str.isdigit() else 0
    if length <= 0:
      return {}
    raw = self.rfile.read(length).decode('utf-8')
    if not raw.strip():
      return {}
    try:
      data = json.loads(raw)
      return data if isinstance(data, dict) else {}
    except ValueError:
      return {}

  def do_GET(self) -> None:  # pylint: disable=invalid-name
    """Serves the dashboard UI, logo, agent card, /healthz, and the JSON APIs."""
    srv = self.server
    assert isinstance(srv, VibeLiftHttpServer)
    parsed = urllib.parse.urlsplit(self.path)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    if path in ('/', '/ui', '/app'):
      html_bytes = ui_template.render_dashboard_html().encode('utf-8')
      self.send_response(200)
      self.send_header('Content-Type', 'text/html; charset=utf-8')
      self.send_header('Content-Length', str(len(html_bytes)))
      self.end_headers()
      self.wfile.write(html_bytes)
      return
    if path == '/api/ge_fleet':
      self._send_json(srv.controller.get_fleet_payload(
          window_hours=ge_fleet.parse_window_hours((query.get('window_hours') or [None])[0]),
          force_refresh=ge_fleet.parse_bool((query.get('force_refresh') or [''])[0]),
      ))
      return
    if path == '/mcp' and mcp_server.mcp_app_enabled():
      self.send_response(405)
      self.send_header('Allow', 'POST, DELETE')
      self.send_header('Content-Length', '0')
      self.end_headers()
      return
    if path in ('/vibelift_googley_logo_1789766299532.jpg', '/logo.jpg'):
      b64_part = logo_asset.VIBELIFT_GOOGLEY_LOGO_DATA_URI.split(',', 1)[1]
      img_bytes = base64.b64decode(b64_part)
      self.send_response(200)
      self.send_header('Content-Type', 'image/jpeg')
      self.send_header('Content-Length', str(len(img_bytes)))
      self.end_headers()
      self.wfile.write(img_bytes)
      return
    if path in ('/.well-known/agent-card.json', '/a2a/app/.well-known/agent-card.json'):
      self._send_json(build_agent_card(base_url_from_headers(self.headers, default_scheme='http')))
      return
    if path == '/healthz':
      self._send_json({'status': 'ok', 'service': 'vibelift', 'runtime': 'cloud_run'})
      return
    if path == '/api/state':
      self._send_json(srv.controller.get_state_payload(
          window_hours=ge_fleet.parse_window_hours((query.get('window_hours') or [None])[0]),
      ))
      return
    if path == '/api/gcp_telemetry':
      self._send_json(srv.controller.gcp_telemetry.get_telemetry_summary_payload())
      return
    if path == '/api/user_centric_finops':
      self._send_json(srv.controller.get_user_centric_finops_payload(
          window_hours=ge_fleet.parse_window_hours((query.get('window_hours') or [None])[0]),
      ))
      return
    if path == '/api/tokenomics_cockpit':
      self._send_json(srv.controller.recompute_finops({}))
      return
    if path == '/api/validate_telemetry':
      run_judge = ge_fleet.parse_bool((query.get('run_llm_judge') or [''])[0])
      self._send_json(srv.controller.validate_telemetry(run_llm_judge=run_judge))
      return
    if path == '/api/sme_eval':
      fresh = ge_fleet.parse_bool((query.get('fresh') or [''])[0])
      self._send_json(srv.controller.get_sme_evaluation(run_fresh=fresh))
      return
    self.send_error(404, 'Not Found')

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    """Serves POST routes for agent selection, custom parameters, GCP sync, and AlphaEvolve."""
    srv = self.server
    assert isinstance(srv, VibeLiftHttpServer)
    body = self._read_json_body()
    if self.path == '/api/validate_telemetry':
      run_judge = bool(body.get('run_llm_judge', body.get('llm_judge', True)))
      self._send_json(srv.controller.validate_telemetry(run_llm_judge=run_judge))
      return
    if self.path == '/api/sme_eval/run':
      self._send_json(srv.controller.get_sme_evaluation(run_fresh=True))
      return
    if self.path == '/api/sme_eval/rate':
      self._send_json(srv.controller.submit_sme_rating(body))
      return
    if self.path == '/api/select_agent':
      agent_id = str(body.get('agent_id', 'it_service_desk'))
      self._send_json(srv.controller.select_agent(agent_id))
      return
    if self.path == '/api/select_optimizer':
      platform_id = str(body.get('platform_id', 'alpha_evolve'))
      self._send_json(srv.controller.select_optimizer(platform_id))
      return
    if self.path == '/api/decorator_ingest':
      self._send_json(srv.controller.ingest_decorator_event(body))
      return
    if self.path == '/api/nl2sql':
      question = str(body.get('question', 'Compare cost per 1k turns and prompt cache savings across agents'))
      self._send_json(srv.controller.query_nl2sql(question))
      return
    if self.path == '/api/aive_log':
      self._send_json(srv.controller.ingest_aive_log(body))
      return
    if self.path == '/api/csat_rating':
      self._send_json(srv.controller.submit_csat_rating(body))
      return
    if self.path == '/api/what_if_simulate':
      self._send_json(srv.controller.simulate_what_if(body))
      return
    if self.path == '/api/what_if_live':
      self._send_json(srv.controller.what_if_live(body))
      return
    if self.path == '/api/recompute_finops':
      self._send_json(srv.controller.recompute_finops(body))
      return
    if self.path == '/api/add_parameter':
      label = str(body.get('label', 'Custom Guardrail Metric'))
      unit = str(body.get('unit', '%'))
      direction = str(body.get('direction', 'HIGHER'))
      baseline_val = float(str(body.get('baseline_val', 85.0)))
      target_val = float(str(body.get('target_val', 98.0)))
      weight_pct = int(float(str(body.get('weight_pct', 10))))
      self._send_json(
          srv.controller.add_parameter(
              label=label,
              unit=unit,
              direction=direction,
              baseline_val=baseline_val,
              target_val=target_val,
              weight_pct=weight_pct,
          )
      )
      return
    if self.path == '/api/inject_anomaly':
      self._send_json(srv.controller.inject_anomaly())
      return
    if self.path == '/api/step_turn':
      self._send_json(srv.controller.step_turn())
      return
    if self.path == '/api/evolve_generation':
      self._send_json(srv.controller.evolve_generation())
      return
    if self.path == '/api/reset':
      self._send_json(srv.controller.reset())
      return
    if self.path == '/api/sync_gcp_telemetry':
      self._send_json({
          'status': 'synced',
          'state': srv.controller.sync_gcp_telemetry(
              window_hours=ge_fleet.parse_window_hours(body.get('window_hours'))
          ),
      })
      return
    if self.path == '/mcp' and mcp_server.mcp_app_enabled():
      session_id = self.headers.get('mcp-session-id') or self.headers.get('Mcp-Session-Id') or ''
      code, headers_map, res_text = mcp_server.handle_jsonrpc_sync(body, session_id=session_id)
      self.send_response(code)
      for k, v in headers_map.items():
        self.send_header(k, v)
      res_bytes = res_text.encode('utf-8')
      self.send_header('Content-Length', str(len(res_bytes)))
      self.end_headers()
      self.wfile.write(res_bytes)
      return
    if self.path == '/api/sync_ge_fleet':
      self._send_json({
          'status': 'synced',
          'fleet': srv.controller.get_fleet_payload(
              window_hours=ge_fleet.parse_window_hours(body.get('window_hours')), force_refresh=True),
      })
      return
    if self.path == '/api/ge_mart/refresh':
      self._send_json(srv.controller.refresh_ge_mart())
      return
    self.send_error(404, 'Not Found')

  def do_DELETE(self) -> None:  # pylint: disable=invalid-name
    """Handles MCP session teardown."""
    if self.path == '/mcp' and mcp_server.mcp_app_enabled():
      self.send_response(204)
      self.send_header('Content-Length', '0')
      self.end_headers()
      return
    self.send_error(404, 'Not Found')

  def log_message(self, fmt: str, *args: object) -> None:
    """Routes HTTP access logs through standard logger."""
    logger.info('VibeLift HTTP: ' + fmt, *args)

  def _send_json(self, payload: Mapping[str, object] | dict[str, object]) -> None:
    """Serializes payload as JSON and writes HTTP 200 response."""
    body = json.dumps(payload).encode('utf-8')
    self.send_response(200)
    self.send_header('Content-Type', 'application/json; charset=utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)


def create_http_server(
    host: str = '127.0.0.1',
    port: int = 0,
    controller: VibeLiftRuntimeController | None = None,
) -> VibeLiftHttpServer:
  """Creates a bound VibeLiftHttpServer instance."""
  ctrl = controller or _global_controller
  return VibeLiftHttpServer((host, port), ctrl)


def configured_public_url() -> str:
  """Returns VIBELIFT_PUBLIC_URL (no trailing slash), or '' when it is not configured."""
  return os.environ.get('VIBELIFT_PUBLIC_URL', '').strip().rstrip('/')


def base_url_from_headers(headers: object, default_scheme: str = 'https') -> str:
  """Derives the externally visible base URL from the Host and X-Forwarded-Proto request headers.

  Cloud Run terminates TLS and forwards plain HTTP, so X-Forwarded-Proto carries the real scheme.
  """
  get = getattr(headers, 'get', None)
  if get is None:
    return ''
  host = str(get('host') or get('Host') or '').strip()
  if not host or any(c in host for c in '/\\ @'):
    return ''
  proto = str(get('x-forwarded-proto') or get('X-Forwarded-Proto') or default_scheme)
  proto = proto.split(',')[0].strip().lower()
  return f"{proto if proto in ('http', 'https') else default_scheme}://{host}"


def build_agent_card(request_base_url: str | None = None) -> dict[str, object]:
  """A2A agent card served at /.well-known/agent-card.json and /a2a/app/.well-known/agent-card.json.

  The advertised URL is VIBELIFT_PUBLIC_URL when set, otherwise the URL the request arrived on,
  so a fork or redeploy never publishes another deployment's hostname.
  """
  service_url = configured_public_url() or (request_base_url or '').rstrip('/')
  a2a_url = os.environ.get('PUBLIC_A2A_URL', '').strip() or f'{service_url}/a2a/app'
  return {
      'protocolVersion': '0.3.0',
      'name': 'vibelift-analytics-agent',
      'displayName': 'VibeLift Analytics & FinOps Platform',
      'description': (
          'Live Gemini Enterprise agent fleet observability (agent inventory joined with real Cloud '
          'Monitoring and Cloud Logging telemetry), prompt cache FinOps economics, and AlphaEvolve '
          'optimization, with an interactive MCP App dashboard.'
      ),
      'url': a2a_url,
      'version': '1.1.0',
      'capabilities': {'streaming': True},
      'defaultInputModes': ['text'],
      'defaultOutputModes': ['text'],
      'skills': [
          {
              'id': 'open_dashboard',
              'name': 'open_dashboard',
              'description': (
                  'CRITICAL: Always call this tool whenever the user asks to see, open, or inspect the '
                  'VibeLift Analytics & FinOps Dashboard, or asks for the Gemini Enterprise agent fleet, '
                  'agent telemetry, prompt cache economics, or AlphaEvolve optimization in the interactive UI.'
              ),
              'tags': ['analytics', 'dashboard', 'finops', 'optimization'],
          },
          {
              'id': 'query_ge_agent_fleet',
              'name': 'query_ge_agent_fleet',
              'description': (
                  'Lists every agent deployed on the Gemini Enterprise app with live telemetry: requests, '
                  'errors, latency, LLM calls, tokens, conversations, and last activity, plus project-wide '
                  'Vertex AI model usage and estimated cost.'
              ),
              'tags': ['gemini_enterprise', 'fleet', 'telemetry', 'finops'],
          },
          {
              'id': 'query_project_telemetry',
              'name': 'query_project_telemetry',
              'description': (
                  'Fetches Google Cloud project telemetry for the monitored Cloud Run services, Gemini '
                  'Enterprise support events, and prompt cache statistics.'
              ),
              'tags': ['telemetry', 'cloud_run', 'logging'],
          },
          {
              'id': 'calculate_prompt_cache_economics',
              'name': 'calculate_prompt_cache_economics',
              'description': 'Calculates prompt cache hit ratio and net savings against Vertex AI list prices.',
              'tags': ['economics', 'cache', 'pricing'],
          },
          {
              'id': 'run_alpha_evolve_generation',
              'name': 'run_alpha_evolve_generation',
              'description': 'Executes an AlphaEvolve optimization cycle on prompt prefixes and parameters.',
              'tags': ['alpha_evolve', 'optimization'],
          },
      ],
  }


def register_api_routes(app: object, controller: VibeLiftRuntimeController) -> None:
  """Registers the dashboard UI, agent card, and JSON API routes on a FastAPI app.

  Handlers are plain ``def`` so FastAPI runs the blocking Google Cloud calls in its threadpool.
  """
  @app.get('/', response_class=HTMLResponse)
  @app.get('/ui', response_class=HTMLResponse)
  @app.get('/app', response_class=HTMLResponse)
  def get_dashboard():
    return HTMLResponse(content=ui_template.render_dashboard_html(), status_code=200)

  @app.get('/vibelift_googley_logo_1789766299532.jpg')
  @app.get('/logo.jpg')
  def get_logo():
    b64_part = logo_asset.VIBELIFT_GOOGLEY_LOGO_DATA_URI.split(',', 1)[1]
    return Response(content=base64.b64decode(b64_part), media_type='image/jpeg')

  @app.get('/.well-known/agent-card.json')
  @app.get('/a2a/app/.well-known/agent-card.json')
  def get_agent_card(request: fastapi.Request):
    return JSONResponse(content=build_agent_card(base_url_from_headers(request.headers)), status_code=200)

  @app.get('/healthz')
  def get_healthz():
    return {
        'status': 'ok',
        'service': 'vibelift',
        'runtime': 'cloud_run',
        'project': controller.gcp_telemetry.project_id,
        'region': controller.gcp_telemetry.region,
    }

  @app.get('/api/state')
  def get_state(window_hours: str | None = None):
    return controller.get_state_payload(window_hours=ge_fleet.parse_window_hours(window_hours))

  @app.get('/api/ge_fleet')
  def get_ge_fleet(window_hours: str | None = None, force_refresh: str | None = None):
    return controller.get_fleet_payload(
        window_hours=ge_fleet.parse_window_hours(window_hours),
        force_refresh=ge_fleet.parse_bool(force_refresh),
    )

  @app.post('/api/sync_ge_fleet')
  def sync_ge_fleet(payload: dict = fastapi.Body(default={})):
    return {
        'status': 'synced',
        'fleet': controller.get_fleet_payload(
            window_hours=ge_fleet.parse_window_hours(payload.get('window_hours')), force_refresh=True),
    }

  @app.post('/api/ge_mart/refresh')
  def post_refresh_ge_mart():
    return controller.refresh_ge_mart()

  @app.get('/api/gcp_telemetry')
  def get_gcp_telemetry_endpoint():
    return controller.gcp_telemetry.get_telemetry_summary_payload()

  @app.get('/api/user_centric_finops')
  def get_user_centric_finops(window_hours: str | None = None):
    return controller.get_user_centric_finops_payload(window_hours=ge_fleet.parse_window_hours(window_hours))

  @app.post('/api/sync_gcp_telemetry')
  def sync_gcp_telemetry_endpoint(payload: dict = fastapi.Body(default={})):
    return {
        'status': 'synced',
        'state': controller.sync_gcp_telemetry(
            window_hours=ge_fleet.parse_window_hours(payload.get('window_hours'))
        ),
    }

  @app.post('/api/select_agent')
  def post_select_agent(payload: dict = fastapi.Body(default={})):
    return controller.select_agent(str(payload.get('agent_id', 'it_service_desk')))

  @app.post('/api/select_optimizer')
  def post_select_optimizer(payload: dict = fastapi.Body(default={})):
    return controller.select_optimizer(str(payload.get('platform_id', 'alpha_evolve')))

  @app.post('/api/decorator_ingest')
  def post_decorator_ingest(payload: dict = fastapi.Body(default={})):
    return controller.ingest_decorator_event(payload)

  @app.post('/api/nl2sql')
  def post_nl2sql(payload: dict = fastapi.Body(default={})):
    question = str(payload.get('question', 'Compare cost per 1k turns and prompt cache savings across agents'))
    return controller.query_nl2sql(question)

  @app.post('/api/aive_log')
  def post_aive_log(payload: dict = fastapi.Body(default={})):
    return controller.ingest_aive_log(payload)

  @app.post('/api/csat_rating')
  def post_csat_rating(payload: dict = fastapi.Body(default={})):
    return controller.submit_csat_rating(payload)

  @app.post('/api/what_if_simulate')
  def post_what_if_simulate(payload: dict = fastapi.Body(default={})):
    return controller.simulate_what_if(payload)

  @app.post('/api/what_if_live')
  def post_what_if_live(payload: dict = fastapi.Body(default={})):
    return controller.what_if_live(payload)

  @app.get('/api/tokenomics_cockpit')
  def get_tokenomics_cockpit_endpoint():
    return controller.recompute_finops({})

  @app.post('/api/recompute_finops')
  def post_recompute_finops(payload: dict = fastapi.Body(default={})):
    return controller.recompute_finops(payload)

  @app.get('/api/validate_telemetry')
  def get_validate_telemetry(run_llm_judge: str | None = None):
    return controller.validate_telemetry(run_llm_judge=ge_fleet.parse_bool(run_llm_judge))

  @app.post('/api/validate_telemetry')
  def post_validate_telemetry(payload: dict = fastapi.Body(default={})):
    run_judge = bool(payload.get('run_llm_judge', payload.get('llm_judge', True)))
    return controller.validate_telemetry(run_llm_judge=run_judge)

  @app.get('/api/sme_eval')
  def get_sme_eval(fresh: str | None = None):
    return controller.get_sme_evaluation(run_fresh=ge_fleet.parse_bool(fresh))

  @app.post('/api/sme_eval/run')
  def post_sme_eval_run():
    return controller.get_sme_evaluation(run_fresh=True)

  @app.post('/api/sme_eval/rate')
  def post_sme_eval_rate(payload: dict = fastapi.Body(default={})):
    return controller.submit_sme_rating(payload)

  @app.post('/api/add_parameter')
  def post_add_parameter(payload: dict = fastapi.Body(default={})):
    return controller.add_parameter(
        label=str(payload.get('label', 'Custom Metric')),
        unit=str(payload.get('unit', '%')),
        direction=str(payload.get('direction', 'HIGHER')),
        baseline_val=float(str(payload.get('baseline_val', 85.0))),
        target_val=float(str(payload.get('target_val', 98.0))),
        weight_pct=int(float(str(payload.get('weight_pct', 10)))),
    )

  @app.post('/api/inject_anomaly')
  def post_inject_anomaly():
    return controller.inject_anomaly()

  @app.post('/api/step_turn')
  def post_step_turn():
    return controller.step_turn()

  @app.post('/api/evolve_generation')
  def post_evolve_generation():
    return controller.evolve_generation()

  @app.post('/api/reset')
  def post_reset():
    return controller.reset()


# ---------------------------------------------------------------------------
# FastAPI Application for Cloud Run Production Deployment & ADK Framework
# ---------------------------------------------------------------------------
if fastapi is not None:
  app = fastapi.FastAPI(
      title='VibeLift | Analytics Platform for Agent Optimization',
      description='Cloud Run & ADK runtime for autonomous multi-objective agent telemetry and AlphaEvolve optimization',
      version='1.1.0',
  )

  # TODO(security): restrict CORS to known origins (ALLOWED_ORIGINS) once the dashboard's
  # consumers are fixed; the service itself is IAM-protected on Cloud Run.
  app.add_middleware(
      CORSMiddleware,
      allow_origins=['*'],
      allow_credentials=False,
      allow_methods=['GET', 'POST', 'DELETE'],
      allow_headers=['*'],
  )

  mcp_server.register_mcp_routes(app)
  register_api_routes(app, _global_controller)
else:
  app = None


def run_standalone_server(port: int = 8080, host: str = '0.0.0.0') -> None:
  """Runs the VibeLift HTTP server for local or container execution."""
  srv = create_http_server(host=host, port=port)
  actual_port = srv.server_address[1]
  logger.info(
      'VibeLift Analytics Platform active on %s:%s (GCP Project: %s)',
      host,
      actual_port,
      _global_controller.gcp_telemetry.project_id,
  )
  try:
    srv.serve_forever()
  except KeyboardInterrupt:
    logger.info('Shutting down server...')
    srv.server_close()


def main(argv: Sequence[str] | None = None) -> None:
  """Entry point honoring Cloud Run $PORT environment variable."""
  port_str = os.environ.get('PORT') or '8088'
  port = int(port_str) if port_str.isdigit() else 8088
  host = os.environ.get('HOST', '0.0.0.0')

  # If uvicorn is available and requested, run with uvicorn for Cloud Run
  use_uvicorn = os.environ.get('USE_UVICORN', '0') == '1' or '--uvicorn' in (argv or sys.argv)
  if use_uvicorn and app is not None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
  else:
    run_standalone_server(port=port, host=host)


if __name__ == '__main__':
  main(sys.argv)
