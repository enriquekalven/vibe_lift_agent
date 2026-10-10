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
from typing import Any

from vibelift.jsonutil import as_list, as_mapping

try:
  from absl import app as absl_app
  from absl import flags
except ImportError:
  absl_app = None  # type: ignore[assignment]
  flags = None  # type: ignore[assignment]

try:
  import fastapi
  from fastapi.concurrency import run_in_threadpool
  from fastapi.middleware.cors import CORSMiddleware
  from fastapi.responses import HTMLResponse, JSONResponse, Response
except ImportError:
  fastapi = None  # type: ignore[assignment]

from vibelift import billing_export, gcp_telemetry, long_running_agent, mcp_server, prompt_xray, sme_eval, telemetry
from vibelift import finops as live_finops
from vibelift import fleet as ge_fleet
from vibelift import optimizer as alpha_evolve_optimizer
from vibelift import validator as telemetry_validator
from vibelift.ui import logo_asset
from vibelift.ui import template as ui_template

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Opt-in for the Prompt Cache X-Ray live mode, which reads logged (raw) user prompts from Cloud Storage.
PROMPT_XRAY_LIVE_ENV = 'VIBELIFT_PROMPT_XRAY_LIVE'

# How long a new Cloud Run instance waits at startup for the BigQuery insights before it takes traffic
# (VibeLiftRuntimeController.prime_live_caches). A fetch normally takes 2-3 s; this stays well inside
# the startup probe budget in deploy/cloud_run_service.yaml (2 s + 6 x 5 s).
STARTUP_INSIGHTS_TIMEOUT_S = 15.0

# HTTP headers for the dashboard HTML (/, /ui, /app). The page is self-contained: inline scripts (the
# enhancement module, then the main script), inline onclick handlers and inline styles, no external
# scripts, styles, fonts or images, and every fetch goes to the same origin. 'unsafe-inline' is needed
# for the inline scripts and handlers. The Gemini Enterprise side panel gets the HTML through MCP
# resources/read, so its CSP is the MCP resource's _meta.ui.csp (mcp_server.py), not these headers.
# TODO(security): drop 'unsafe-inline' (script hashes or nonces) once the inline onclick handlers are removed.
DASHBOARD_SECURITY_HEADERS: Mapping[str, str] = {
    'Content-Security-Policy': (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    ),
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
}

VALID_SURFACES = ('all', 'mcp', 'dashboard')


def resolve_surface(configured: str | None = None) -> str:
  """Returns the active service surface ('all', 'mcp', or 'dashboard') from VIBELIFT_SURFACE."""
  raw = (configured if configured is not None else os.environ.get('VIBELIFT_SURFACE', 'all')).strip().lower()
  return raw if raw in VALID_SURFACES else 'all'


def dashboard_surface_enabled() -> bool:
  """True when the browser dashboard and REST API routes are enabled."""
  return resolve_surface() in ('all', 'dashboard')


import threading


def _first_query_value(query: Mapping[str, list[str]], name: str) -> str | None:
  """Returns the first value of a parsed query-string parameter, or None when it is absent."""
  values = query.get(name)
  return values[0] if values else None


def _label_nl2sql_simulated(res: dict[str, Any]) -> dict[str, Any]:
  """Marks a demo-mode NL2SQL result as simulator output: nothing ran on BigQuery."""
  res['execution_mode'] = 'SIMULATOR'
  summary = str(res.get('executive_summary') or '')
  if not summary.startswith(('Simulator result', 'Blocked')):
    res['executive_summary'] = 'Simulator result (example rows; no BigQuery query was run): ' + summary
  return res


def _nl2sql_not_run(res: dict[str, Any]) -> dict[str, Any]:
  """Live-mode default for the search drawer: shows the SQL, runs nothing until the user asks."""
  res['rows'] = []
  res['columns'] = []
  res['execution_mode'] = 'NOT_RUN'
  cap_mb = alpha_evolve_optimizer.NL2SQL_MAX_BYTES_BILLED // (1024 * 1024)
  res['executive_summary'] = f'Not run yet. Press Run Search to run this SQL on BigQuery (cap {cap_mb} MB billed).'
  return res


class VibeLiftRuntimeController:
  """Coordinates live GCP telemetry and the GE fleet, plus the optimizer simulator's demo agents."""

  def __init__(self, fleet_service: ge_fleet.GeminiEnterpriseFleetService | None = None) -> None:
    """Initializes the optimizer, long-running agent, GCP telemetry client, and fleet service."""
    self._lock = threading.Lock()
    self._warmer_started = False
    self._last_fleet_payload: dict[str, Any] | None = None
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

  def prime_live_caches(self, timeout_s: float = STARTUP_INSIGHTS_TIMEOUT_S) -> bool:
    """Loads the BigQuery insights before this instance takes traffic; returns True if they loaded.

    Runs at import time on Cloud Run, before uvicorn opens the port, so the fetch gets full (boosted)
    startup CPU. MCP app tool calls only read cached insights (Gemini Enterprise gives them about 2 s),
    and once CPU is only allocated during requests a background refresh can take minutes. Without this
    step a new instance shows the Users tab empty after every deploy.
    """
    if not self._is_live_gcp():
      return False
    import time
    started = time.monotonic()
    insights = self.gcp_telemetry.prime_bigquery_insights(timeout_s)
    elapsed = time.monotonic() - started
    if insights:
      logger.info(
          'Loaded BigQuery insights in %.1fs before serving: %d sessions, %d users',
          elapsed,
          len(insights.get('ge_sessions') or []),
          len(insights.get('power_users_ldap') or []),
      )
      return True
    logger.warning(
        'BigQuery insights were not ready %.1fs after startup; the Users tab stays empty until a '
        'background refresh finishes',
        elapsed,
    )
    return False

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
  ) -> dict[str, Any]:
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
  ) -> dict[str, Any]:
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

    live_gcp = self._is_live_gcp()
    with self._lock:
      # Live mode shows logged turns only; the simulator's synthetic turns stay in demo mode.
      turns = [t for t in self.agent.turns if not (live_gcp and t.simulated)]
      active_agent_dict = self.optimizer.active_agent.to_dict()
      available_agents = self.optimizer.list_agents_summary()
      all_agents = self.optimizer.get_all_agents_dict()
      optimizer_platforms = self.optimizer.get_optimizer_platforms_payload()
      user_centric = self.optimizer.get_user_centric_payload()
      otel_catalog = self.optimizer.get_otel_catalog_payload()
      billing_reconciliation = self.optimizer.get_finops_billing_reconciliation_payload()
      tokenomics_cockpit: dict[str, Any] | None = self.optimizer.get_tokenomics_and_cockpit_finops_payload()
      persona_playbooks = self.optimizer.get_sme_persona_playbooks()
      what_if_default: dict[str, Any] | None = self.optimizer.simulate_what_if_scenario()
      nl2sql_default = self.optimizer.execute_nl2sql_telemetry_query(
          'Compare cost per 1k turns and prompt cache savings across agents',
          project_id=self.gcp_telemetry.project_id,
      )
      nl2sql_default = _nl2sql_not_run(nl2sql_default) if live_gcp else _label_nl2sql_simulated(nl2sql_default)
      steps = [d for d in self.agent.step_descriptions if not (live_gcp and d.get('simulated'))]

    services = self.gcp_telemetry.list_cloud_run_agent_services(non_blocking=fast_mcp)
    support_events = self.gcp_telemetry.fetch_gemini_enterprise_support_telemetry(
        limit=6, non_blocking=fast_mcp
    )
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
      # The OTel catalog stays as a reference list; its example baseline/current values are simulator data.
      otel_catalog = alpha_evolve_optimizer.catalog_reference_only(otel_catalog)
      live_finops_payload = self._live_finops(fleet_payload or self.optimizer._live_fleet_payload)
      # Never show demo personas in live mode: if BigQuery principals have not
      # loaded yet, show an empty table with an explicit loading status.
      live_bq = bq_insights if isinstance(bq_insights, dict) else self.optimizer._live_bq_insights
      user_centric = dict(user_centric)
      if isinstance(live_bq, dict):
        user_centric['ge_sessions'] = list(live_bq.get('ge_sessions') or [])
        user_centric['ge_session_turns'] = dict(live_bq.get('ge_session_turns') or {})
        user_centric['ge_sessionless_token_turns'] = list(live_bq.get('ge_sessionless_token_turns') or [])
        user_centric['ge_mart_refreshed_at'] = live_bq.get('ge_mart_refreshed_at')
        user_centric['ge_mart_dataset'] = live_bq.get('ge_mart_dataset')
      if not (isinstance(live_bq, dict) and live_bq.get('power_users_ldap')):
        user_centric['power_users_ldap'] = []
        user_centric['collection_mode'] = (
            'LIVE GCP: BigQuery principals still loading (no demo data shown)'
        )
      else:
        # Estimated model cost per user (tokens x list price). Billed cost stays project-level:
        # monthly_cost_usd remains None and nothing from the billing export is split per user.
        priced_users, cost_estimate = live_finops.estimate_user_costs(
            as_list(live_bq.get('power_users_ldap')), live_bq.get('ge_user_model_tokens'), self._rate_cards())
        user_centric['power_users_ldap'] = priced_users
        user_centric['user_cost_estimate'] = {**cost_estimate, 'window_hours': live_bq.get('window_hours')}
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
    payload: dict[str, Any] = {
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
        'ingest_dead_letters': telemetry.get_ingest_dlq_events(),
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

  def get_sme_evaluation(self, run_fresh: bool = False) -> dict[str, Any]:
    """Returns the 6-persona SME evaluation scorecard (automated rubric + human SME ratings)."""
    state = self.get_state_payload(include_fleet=True, fast_mcp=False)
    fleet = state.get('ge_fleet') if isinstance(state.get('ge_fleet'), Mapping) else self._last_fleet_payload
    return sme_eval.evaluate_persona_rubric(
        state,
        ge_fleet_payload=fleet,
        rendered_html=ui_template.render_dashboard_html(),
    )

  def submit_sme_rating(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Records a human SME evaluation rating for a persona and returns updated state."""
    entry = sme_eval.get_sme_evaluation_store().submit_rating(body or {})
    state = self.get_state_payload(include_fleet=False)
    return {
        'status': 'recorded',
        'rating': entry,
        'sme_evaluation': state.get('sme_evaluation'),
        'state': state,
    }

  def validate_telemetry(self, run_llm_judge: bool = True) -> dict[str, Any]:
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

  def _ge_daily_usage(self, bq_insights: object) -> dict[str, Any] | None:
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
        'billing_table': cost.get('billing_export_table'),
        'billing_hint': cost.get('hint'),
        'billing_error_kind': cost.get('error_kind'),
        'currency': cost.get('currency'),
        'cost_scope': 'Project-level spend on AI services; not allocated to agents or users.',
        'days': billing_export.join_daily_usage_with_cost(list(bq_insights['ge_daily_totals']), cost),
        'by_app_agent_model': list(bq_insights.get('ge_daily_by_app') or []),
        'sessions': list(bq_insights.get('ge_sessions') or []),
    }

  def get_user_centric_finops_payload(self, window_hours: int | None = None) -> dict[str, Any]:
    """User-centric FinOps plus per-user session token rollup and per-session turn token drilldown."""
    state = self.get_state_payload(include_fleet=False, window_hours=window_hours)
    uc = as_mapping(state.get('user_centric'))
    return {
        'project_id': self.gcp_telemetry.project_id,
        'mode': 'LIVE_GCP' if self._is_live_gcp() else 'DEMO',
        'user_centric': uc,
        'session_drilldown': live_finops.build_session_token_drilldown(
            as_list(uc.get('ge_sessions')),
            as_mapping(uc.get('ge_session_turns')),
            as_list(uc.get('ge_sessionless_token_turns')),
        ),
    }

  def refresh_ge_mart(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Refreshes vibelift_mart.fct_turns (full rebuild or incremental MERGE) and returns refreshed state."""
    raw = body or {}
    incremental = ge_fleet.parse_bool(raw.get('incremental'))
    try:
      lookback_days = max(1, int(float(str(raw.get('lookback_days') or 3))))
    except (TypeError, ValueError):
      lookback_days = 3
    result = self.gcp_telemetry.refresh_ge_mart_turns(
        incremental=incremental, lookback_days=lookback_days
    )
    if self._is_live_gcp():
      self.gcp_telemetry.fetch_live_bigquery_project_insights(force_refresh=True, non_blocking=False)
    return {'refresh': result, 'state': self.get_state_payload(include_fleet=False)}

  def enable_agent_observability(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Enables or disables trace logging (observabilityConfig) on one or all Gemini Enterprise agents.

    Malformed input returns status INVALID_REQUEST (with a message) before any API call is made.
    """
    raw = body or {}
    resource_name = str(raw.get('resource_name') or raw.get('agent_name') or '').strip() or None
    engine_id = str(raw.get('engine_id') or '').strip() or None
    location = str(raw.get('location') or '').strip() or None
    raw_enabled = raw.get('enabled')
    if raw_enabled is None:
      enabled = str(raw.get('action') or 'enable').strip().lower() not in ('disable', 'off', 'false', '0')
    elif isinstance(raw_enabled, bool):
      enabled = raw_enabled
    else:
      enabled = ge_fleet.parse_bool(raw_enabled)
    raw_sens = raw.get('sensitive_logging')
    if raw_sens is None:
      sensitive_logging = enabled
    elif isinstance(raw_sens, bool):
      sensitive_logging = raw_sens
    else:
      sensitive_logging = ge_fleet.parse_bool(raw_sens)
    only_low_code = ge_fleet.parse_bool(raw.get('only_low_code'))
    try:
      if ge_fleet.parse_bool(raw.get('script_only')):
        default_spec = self.ge_fleet.engine_ids[0] if self.ge_fleet.engine_ids else '<ENGINE_ID>'
        spec_loc, spec_engine = ge_fleet._parse_engine_spec(  # pylint: disable=protected-access
            engine_id or default_spec, location or self.ge_fleet.location)
        script_info = ge_fleet.build_enable_agent_logging_script(
            project_id=self.ge_fleet.project_id,
            location=spec_loc,
            collection=self.ge_fleet.collection,
            engine_id='<ENGINE_ID>' if spec_engine.lower() == 'auto' else spec_engine,
            agent_resource_name=resource_name,
            only_low_code=only_low_code,
            enabled=enabled,
        )
        return {
            'status': 'SCRIPT_ONLY',
            'action': 'ENABLED' if enabled else 'DISABLED',
            'enabled': enabled,
            **script_info,
        }
      return self.ge_fleet.enable_agent_observability(
          resource_name=resource_name,
          engine_id=engine_id,
          location=location,
          enabled=enabled,
          sensitive_logging=sensitive_logging,
          only_low_code=only_low_code,
      )
    except ge_fleet.InvalidAgentRequestError as exc:
      return {
          'status': 'INVALID_REQUEST',
          'message': str(exc),
          'action': 'ENABLED' if enabled else 'DISABLED',
          'enabled': enabled,
          'targeted_count': 0,
          'updated_count': 0,
          'results': [],
      }

  def _live_finops(self, fleet: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = live_finops.build_live_finops(fleet)
    payload['rate_card_models'] = sorted(self._rate_cards())
    return payload

  def what_if_live(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
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

  @staticmethod
  def _prompt_xray_live_enabled() -> bool:
    return os.environ.get(PROMPT_XRAY_LIVE_ENV, '').strip().lower() in ('1', 'true', 'yes', 'on')

  @staticmethod
  def _public_snapshot_turn(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'event_id': row.get('event_id'),
        'timestamp': row.get('timestamp'),
        'agent_name': row.get('agent_name'),
        'user_id': row.get('user_id'),
        'conversation_id': row.get('conversation_id'),
        'input_tokens': row.get('input_tokens'),
        'has_system_instruction': bool(row.get('sys_gcs_uri')),
        'has_input_messages': bool(row.get('input_gcs_uri')),
    }

  def prompt_xray_live_turns(self) -> dict[str, Any]:
    """Recent logged turns the Prompt Cache X-Ray can compare, plus models and a labeled example."""
    base: dict[str, Any] = {
        'available_models': sorted(m for m in telemetry.RATE_CARDS if m.startswith('gemini')),
        'default_model': prompt_xray.DEFAULT_MODEL,
        'example': {'previous_prompt': prompt_xray.EXAMPLE_PREVIOUS, 'current_prompt': prompt_xray.EXAMPLE_CURRENT},
        'turns': [],
    }
    if not self._is_live_gcp():
      return {**base, 'status': 'NOT_CONNECTED',
              'message': 'No live Google Cloud project is connected; paste two prompt snapshots instead.'}
    if not self._prompt_xray_live_enabled():
      return {**base, 'status': 'DISABLED',
              'message': (f'Reading logged prompts from Cloud Storage is off. It shows raw user prompts, so it '
                          f'is opt-in: set {PROMPT_XRAY_LIVE_ENV}=1 and grant the runtime service account '
                          'roles/storage.objectViewer on the prompt-log bucket (redeploy with '
                          'VIBELIFT_PROMPT_LOG_BUCKET=<bucket> to do both).')}
    rows = self.gcp_telemetry.fetch_prompt_snapshot_turns()
    turns = [self._public_snapshot_turn(r) for r in rows]
    check = self._snapshot_availability(rows)
    for turn, row in zip(turns, rows, strict=True):
      turn['snapshot_available'] = check['by_event'].get(str(row.get('event_id')))
    available = sum(1 for t in turns if t['snapshot_available'] is True)
    missing = sum(1 for t in turns if t['snapshot_available'] is False)
    message = None if rows else 'No OTel GenAI turns with logged prompt content refs were found.'
    if rows and check['error']:
      message = f'Could not check which prompt snapshots still exist: {check["error"]}'
    elif rows and available < 2:
      message = (f'{missing} of {len(turns)} logged turns reference prompt snapshots that are no longer in '
                 f'Cloud Storage, so fewer than two turns can be compared. Paste two prompts instead, or '
                 f'compare again once the agent logs new turns.')
    return {
        **base,
        'status': 'OK' if rows else 'EMPTY',
        'source_table': rows[0].get('source_table') if rows else None,
        'turns': turns,
        'snapshot_check': {
            'checked': check['error'] is None and bool(rows),
            'available_turns': available if check['error'] is None else None,
            'missing_turns': missing if check['error'] is None else None,
            'error': check['error'],
        },
        'message': message,
    }

  def _snapshot_availability(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """{'by_event': {event_id: True/False/None}, 'error': str|None} from one listing per referenced folder."""
    import re

    def _split(uri: Any) -> tuple[str, str] | None:
      m = re.fullmatch(r'gs://([a-z0-9][a-z0-9._-]{1,220})/(.+)', str(uri or ''))
      return (m.group(1), m.group(2)) if m else None

    folders: dict[tuple[str, str], set[str] | None] = {}
    for row in rows:
      for key in ('sys_gcs_uri', 'input_gcs_uri'):
        parts = _split(row.get(key))
        if parts:
          folders.setdefault((parts[0], parts[1].rsplit('/', 1)[0] + '/' if '/' in parts[1] else ''), None)
    error = None
    for bucket, prefix in list(folders)[:5]:
      try:
        folders[(bucket, prefix)] = self.gcp_telemetry.list_gcs_object_names(bucket, prefix)
      except gcp_telemetry.GcsReadError as exc:
        error = str(exc)
    by_event: dict[str, bool | None] = {}
    for row in rows:
      verdict: bool | None = True
      for key in ('sys_gcs_uri', 'input_gcs_uri'):
        parts = _split(row.get(key))
        if not row.get(key):
          continue
        if parts is None:
          verdict = False
          break
        prefix = parts[1].rsplit('/', 1)[0] + '/' if '/' in parts[1] else ''
        names = folders.get((parts[0], prefix))
        if names is None:
          verdict = None
        elif parts[1] not in names:
          verdict = False
          break
      by_event[str(row.get('event_id'))] = verdict
    return {'by_event': by_event, 'error': error}

  def prompt_xray(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Prompt Cache X-Ray on two pasted snapshots, or on two logged turns (previous/current_event_id)."""
    raw = body or {}

    def _pos_int(key: str) -> int | None:
      try:
        val = int(float(str(raw.get(key))))
      except (TypeError, ValueError):
        return None
      return val if val > 0 else None

    model = str(raw.get('model') or prompt_xray.DEFAULT_MODEL)[:80]
    monthly = _pos_int('monthly_requests')
    try:
      if raw.get('previous_event_id') or raw.get('current_event_id'):
        return self._prompt_xray_live(
            str(raw.get('previous_event_id') or ''), str(raw.get('current_event_id') or ''), model, monthly)
      prev = raw.get('previous_prompt')
      curr = raw.get('current_prompt')
      return prompt_xray.analyze(
          prev if isinstance(prev, str) else '',
          curr if isinstance(curr, str) else '',
          model=model,
          monthly_requests=monthly,
          current_input_tokens=_pos_int('current_input_tokens'),
      )
    except ValueError as exc:
      return {'status': 'ERROR', 'error': str(exc)}

  def _prompt_xray_live(self, prev_id: str, curr_id: str, model: str, monthly: int | None) -> dict[str, Any]:
    if not self._is_live_gcp() or not self._prompt_xray_live_enabled():
      listing = self.prompt_xray_live_turns()
      return {'status': listing['status'], 'error': listing.get('message')}
    # Only GCS objects referenced by this project's own OTel rows are read; callers pass event ids, not URIs.
    by_id = {str(r.get('event_id')): r for r in self.gcp_telemetry.fetch_prompt_snapshot_turns()}
    snapshots: list[tuple[Mapping[str, Any], str]] = []
    for label, event_id in (('previous', prev_id), ('current', curr_id)):
      row = by_id.get(event_id)
      if row is None:
        return {'status': 'ERROR', 'error': f'The {label} turn {event_id!r} is not among the recent logged turns.'}
      try:
        sys_text = self.gcp_telemetry.read_gcs_text(str(row['sys_gcs_uri'])) if row.get('sys_gcs_uri') else None
        in_text = self.gcp_telemetry.read_gcs_text(str(row['input_gcs_uri'])) if row.get('input_gcs_uri') else None
      except gcp_telemetry.GcsReadError as exc:
        return {'status': 'SNAPSHOT_UNAVAILABLE', 'reason': exc.status, 'error': f'{label} turn: {exc}'}
      snapshots.append((row, prompt_xray.render_logged_prompt(sys_text, in_text)))
    (prev_row, prev_text), (curr_row, curr_text) = snapshots
    source = {
        'kind': 'live_otel_gcs',
        'source_table': curr_row.get('source_table'),
        'previous': self._public_snapshot_turn(prev_row),
        'current': self._public_snapshot_turn(curr_row),
        'rendering': ('System instruction and input messages as logged, rendered as text in order. The logged '
                      'input token count also covers tool definitions, so token figures run slightly high.'),
    }
    logged = curr_row.get('input_tokens')
    return prompt_xray.analyze(
        prev_text, curr_text, model=model, monthly_requests=monthly,
        current_input_tokens=int(logged) if isinstance(logged, int) and logged > 0 else None,
        source=source,
    )

  def recompute_finops(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Recomputes the deterministic Tokenomics & AgentOps Cockpit FinOps payload for given assumptions."""
    if self._is_live_gcp():
      return {
        'status': 'DISABLED_IN_LIVE_MODE',
        'reason': ('This parametric simulator uses example inputs, so it is off when connected to a real '
                   'project. Use /api/what_if_live for projections from observed usage.'),
    }
    with self._lock:
      return self.optimizer.get_tokenomics_and_cockpit_finops_payload(body)

  def query_nl2sql(self, question: str) -> dict[str, Any]:
    """Matches a question to a fixed SELECT template and, on a real project, runs it on BigQuery.

    Demo mode returns the simulator's example rows, labeled as such. On a real
    project the simulator rows are discarded: the SQL runs on BigQuery with a
    ``NL2SQL_MAX_BYTES_BILLED`` cap, and the response carries BigQuery's rows
    or its error. A query is sent only when ``verify_nl2sql_sql`` passed and
    the question held no write keyword.
    """
    with self._lock:
      res = self.optimizer.execute_nl2sql_telemetry_query(
          question,
          project_id=self.gcp_telemetry.project_id,
      )
    if not self._is_live_gcp():
      return _label_nl2sql_simulated(res)
    return self._run_nl2sql_live(res)

  def _run_nl2sql_live(self, res: dict[str, Any]) -> dict[str, Any]:
    """Replaces the simulator rows in an NL2SQL result with the BigQuery result."""
    safety = res.get('sql_safety_audit') or {}
    cap = alpha_evolve_optimizer.NL2SQL_MAX_BYTES_BILLED
    res['rows'] = []
    res['columns'] = []
    if safety.get('blocked_dml_attempt') or not res.get('generated_sql'):
      res['execution_mode'] = 'BLOCKED'
      return res
    if not safety.get('read_only_enforced'):
      res['execution_mode'] = 'BLOCKED'
      errors = '; '.join(str(e) for e in (safety.get('verification_errors') or [])) or 'unknown'
      res['executive_summary'] = f'Not run: the SQL failed the read-only check ({errors}).'
      return res
    rows, info = self.gcp_telemetry.run_bigquery_query(
        str(res['generated_sql']), timeout_s=6.0, max_bytes_billed=cap,
    )
    res['execution_mode'] = 'LIVE_BIGQUERY_REST'
    res['bigquery_job'] = info
    res['rows'] = rows
    res['columns'] = list(rows[0].keys()) if rows else []
    datasets = ', '.join(safety.get('datasets_queried') or []) or 'BigQuery'
    if info.get('error'):
      res['executive_summary'] = f'BigQuery did not return rows. {info["error"]}'
    else:
      scanned = info.get('total_bytes_processed')
      if not isinstance(scanned, int):
        scanned_txt = 'bytes scanned not reported'
      elif scanned < 1024 * 1024:
        scanned_txt = f'{scanned / 1024:.1f} KB scanned'
      else:
        scanned_txt = f'{scanned / (1024 * 1024):.1f} MB scanned'
      res['executive_summary'] = (
          f'{len(rows)} row(s) from {datasets} on BigQuery ({scanned_txt}; '
          f'cap {cap // (1024 * 1024)} MB billed).'
      )
    return res

  def simulate_what_if(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
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

  def submit_csat_rating(self, body: Any = None) -> dict[str, Any]:
    """Records a user CSAT rating in memory and in the audit log, and returns updated state.

    Rows use the BigQuery `aive_logs.ratings_log` shape, but this service never writes to BigQuery.

    Raises telemetry.IngestValidationError (HTTP 422) when the rating or what it rates is missing.
    """
    fields, errors = telemetry.validate_csat_payload(body)
    telemetry.raise_if_invalid('/api/csat_rating', body, errors)
    telemetry.log_csat_rating(**fields)
    return self.get_state_payload(include_fleet=False)

  def ingest_aive_log(self, body: Any = None) -> dict[str, Any]:
    """Ingests one usage event into the in-memory `aive_logs` view and the decorator stream (not BigQuery).

    Missing optional values stay None and prompt text is fingerprinted. Raises
    telemetry.IngestValidationError (HTTP 422) for invalid bodies.
    """
    fields, errors = telemetry.validate_aive_payload(body)
    telemetry.raise_if_invalid('/api/aive_log', body, errors)
    telemetry.log_agent_generation_event(**fields)
    return self.get_state_payload(include_fleet=False)

  def sync_gcp_telemetry(self, window_hours: int | None = None) -> dict[str, Any]:
    """Fetches live Cloud Logging turns from GCP and ingests them into the active runtime."""
    live_turns = self.gcp_telemetry.fetch_live_cloud_turns(hours_ago=window_hours or 48, max_results=15)
    with self._lock:
      if live_turns:
        self.agent.ingest_gcp_cloud_turns(live_turns)
    return self.get_state_payload(include_fleet=False, window_hours=window_hours)

  def select_agent(self, agent_id: str) -> dict[str, Any]:
    """Switches the active agent being analyzed and optimized."""
    with self._lock:
      self.optimizer.select_agent(agent_id)
      self.agent = long_running_agent.LongRunningVibeLiftAgent(
          self.optimizer,
          agent_name=self.optimizer.active_agent.agent_id,
          model=self.optimizer.active_agent.model,
      )
    return self.get_state_payload(include_fleet=False)

  def select_optimizer(self, platform_id: str) -> dict[str, Any]:
    """Switches the active optimization platform (AlphaEvolve, Opus Critic, or Hybrid)."""
    with self._lock:
      self.optimizer.select_optimizer_platform(platform_id)
    return self.get_state_payload(include_fleet=False)

  def ingest_decorator_event(self, body: Any = None) -> dict[str, Any]:
    """Records one @vibelift_telemetry event posted by an agent's message-passing hook.

    Fields the caller leaves out stay None; nothing is filled in. Raises
    telemetry.IngestValidationError (HTTP 422) for invalid bodies and keeps a dead-letter record.
    """
    fields, errors = telemetry.validate_decorator_payload(body)
    telemetry.raise_if_invalid('/api/decorator_ingest', body, errors)
    telemetry.record_decorator_event(telemetry.DecoratorTelemetryEvent(**fields))
    return self.get_state_payload(include_fleet=False)

  def add_parameter(
      self,
      label: str,
      unit: str,
      direction: str,
      baseline_val: float,
      target_val: float,
      weight_pct: int,
  ) -> dict[str, Any]:
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

  @staticmethod
  def _simulator_enabled_in_live_mode() -> bool:
    return os.environ.get('VIBELIFT_ENABLE_SIMULATOR', '').strip().lower() in ('1', 'true', 'yes', 'on')

  def inject_anomaly(self) -> dict[str, Any]:
    """Simulator: applies the fixed cache-bust scenario to the active demo agent."""
    if self._is_live_gcp() and not self._simulator_enabled_in_live_mode():
      return {
          'status': 'DISABLED_IN_LIVE_MODE',
          'reason': 'Synthetic anomaly injection is disabled when connected to a live Google Cloud project.',
      }
    with self._lock:
      self.optimizer.inject_anomaly()
    return self.get_state_payload(include_fleet=False)

  def step_turn(self) -> dict[str, Any]:
    """Simulator: appends one synthetic turn (hidden in live mode) and returns updated state."""
    if self._is_live_gcp() and not self._simulator_enabled_in_live_mode():
      return {
          'status': 'DISABLED_IN_LIVE_MODE',
          'reason': 'Synthetic turn stepping is disabled when connected to a live Google Cloud project.',
      }
    with self._lock:
      self.agent.step_turn()
    return self.get_state_payload(include_fleet=False)

  def evolve_generation(self) -> dict[str, Any]:
    """Simulator: runs one optimizer generation on the demo agent and appends a synthetic turn."""
    if self._is_live_gcp() and not self._simulator_enabled_in_live_mode():
      return {
          'status': 'DISABLED_IN_LIVE_MODE',
          'reason': (
              'The synthetic demo optimizer is disabled when connected to a live Google Cloud project. '
              'Use /api/prompt_xray (evolved via AlphaEvolve in experiments/prompt_cache_evolve/) or '
              '/api/what_if_live for live optimization.'
          ),
      }
    with self._lock:
      self.optimizer.run_next_generation()
      self.agent.step_turn()
    return self.get_state_payload(include_fleet=False)

  def dead_letters_payload(self) -> dict[str, Any]:
    """Returns rejected ingest dead-letter events and configured Cloud Pub/Sub DLQ topic."""
    events = telemetry.get_ingest_dlq_events()
    return {
        'status': 'OK',
        'count': len(events),
        'dlq_pubsub_topic': telemetry.configured_dlq_topic() or None,
        'dead_letters': events,
    }

  def handle_a2a_task(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Executes an A2A JSON-RPC 2.0 task request (/a2a/app) against live VibeLift skills."""
    raw = body or {}
    rpc_id = raw.get('id', 1)
    method = str(raw.get('method') or 'tasks/send').strip()
    params = as_mapping(raw.get('params'))
    task_id = str(params.get('id') or 'vibelift-a2a-task-1')
    skill = str(params.get('skill') or '').strip()
    message = as_mapping(params.get('message'))
    text_parts: list[str] = []
    for part in as_list(message.get('parts')):
      part_map = as_mapping(part)
      if isinstance(part_map.get('text'), str):
        text_parts.append(part_map['text'])
    prompt_text = '\n'.join(text_parts).strip()

    if not skill:
      lower = prompt_text.lower()
      if 'xray' in lower or ('cache' in lower and 'previous' in lower):
        skill = 'xray_prompt_cache'
      elif 'telemetry' in lower or 'cloud run' in lower:
        skill = 'query_project_telemetry'
      elif 'economics' in lower or 'savings' in lower:
        skill = 'calculate_prompt_cache_economics'
      else:
        skill = 'query_ge_agent_fleet'

    if method not in ('tasks/send', 'message/send', 'tasks/get'):
      return {
          'jsonrpc': '2.0',
          'id': rpc_id,
          'error': {'code': -32601, 'message': f'Unsupported A2A method: {method}'},
      }

    if skill == 'query_project_telemetry':
      artifact_data: dict[str, Any] = self.gcp_telemetry.get_telemetry_summary_payload()
    elif skill == 'xray_prompt_cache':
      prev = str(params.get('previous_prompt') or prompt_xray.EXAMPLE_PREVIOUS)
      curr = str(params.get('current_prompt') or prompt_xray.EXAMPLE_CURRENT)
      artifact_data = prompt_xray.analyze(prev, curr)
    elif skill == 'calculate_prompt_cache_economics':
      uncached_tok = max(0, int(params.get('uncached_input_tokens') or 2000))
      cached_tok = max(0, int(params.get('cached_input_tokens') or 18000))
      output_tok = max(0, int(params.get('output_tokens') or 500))
      log_entry = telemetry.TurnUsageLog(
          timestamp=telemetry._utc_now_iso(),  # pylint: disable=protected-access
          agent_name='a2a-client',
          model=str(params.get('model') or 'gemini-2.5-pro'),
          turn_index=1,
          prompt_prefix_hash='a2a_calc',
          cache_breakpoint_line=None,
          cache_breakpoint_reason='a2a_eval',
          prompt_token_count=uncached_tok + cached_tok,
          cached_content_token_count=cached_tok,
          cache_creation_input_tokens=0,
          uncached_input_tokens=uncached_tok,
          candidates_token_count=output_tok,
          thoughts_token_count=0,
          status_code=200,
          tool_called='calculate_prompt_cache_economics',
          evolution_generation=0,
      )
      artifact_data = log_entry.to_dict()
    else:
      skill = 'query_ge_agent_fleet'
      artifact_data = self.get_fleet_payload(allow_stale=True, max_wait_s=5.0)

    return {
        'jsonrpc': '2.0',
        'id': rpc_id,
        'result': {
            'id': task_id,
            'status': {'state': 'completed'},
            'skill': skill,
            'artifacts': [
                {
                    'name': skill,
                    'parts': [{'type': 'data', 'data': artifact_data}],
                }
            ],
        },
    }

  def reset(self) -> dict[str, Any]:
    """Resets the optimizer and agent trajectory to the initial seed state."""
    with self._lock:
      self.optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
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
if os.environ.get('K_SERVICE'):
  # Cloud Run imports this module before uvicorn opens the port, so this blocks traffic until loaded.
  _global_controller.prime_live_caches()
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

  def _read_json_payload(self) -> tuple[Any, bool]:
    """Reads the request body. Returns (parsed JSON value or None when empty, body_was_valid_json)."""
    length_str = self.headers.get('Content-Length', '0')
    length = int(length_str) if length_str.isdigit() else 0
    if length <= 0:
      return None, True
    raw = self.rfile.read(length).decode('utf-8', errors='replace')
    if not raw.strip():
      return None, True
    try:
      return json.loads(raw), True
    except ValueError:
      return raw, False

  def _send_ingest(self, source: str, handler: Any, payload: Any, valid_json: bool) -> None:
    """Runs an ingest handler; an invalid body gets HTTP 422 plus its dead-letter record."""
    try:
      if not valid_json:
        telemetry.raise_if_invalid(source, payload, ['body: not valid JSON'])
      self._send_json(handler(payload))
    except telemetry.IngestValidationError as exc:
      self._send_json(exc.to_response(), status=422)

  def do_GET(self) -> None:  # pylint: disable=invalid-name
    """Serves the dashboard UI, logo, agent card, /healthz, and the JSON APIs."""
    srv = self.server
    assert isinstance(srv, VibeLiftHttpServer)
    parsed = urllib.parse.urlsplit(self.path)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    if path == '/mcp' and mcp_server.mcp_app_enabled():
      self.send_response(405)
      self.send_header('Allow', 'POST, DELETE')
      self.send_header('Content-Length', '0')
      self.end_headers()
      return
    if path in ('/.well-known/agent-card.json', '/a2a/app/.well-known/agent-card.json'):
      self._send_json(build_agent_card(base_url_from_headers(self.headers, default_scheme='http')))
      return
    # /healthz is for container probes; Cloud Run's front end reserves paths ending in "z", so browsers
    # and external checks use /api/health instead.
    if path in ('/healthz', '/api/health'):
      self._send_json({'status': 'ok', 'service': 'vibelift', 'runtime': 'cloud_run', 'surface': resolve_surface()})
      return
    if not dashboard_surface_enabled():
      self.send_error(404, 'Not Found')
      return
    if path in ('/', '/ui', '/app'):
      html_bytes = ui_template.render_dashboard_html().encode('utf-8')
      self.send_response(200)
      self.send_header('Content-Type', 'text/html; charset=utf-8')
      self.send_header('Content-Length', str(len(html_bytes)))
      for name, value in DASHBOARD_SECURITY_HEADERS.items():
        self.send_header(name, value)
      self.end_headers()
      self.wfile.write(html_bytes)
      return
    if path == '/api/ge_fleet':
      self._send_json(srv.controller.get_fleet_payload(
          window_hours=ge_fleet.parse_window_hours(_first_query_value(query, 'window_hours')),
          force_refresh=ge_fleet.parse_bool((query.get('force_refresh') or [''])[0]),
      ))
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
    if path == '/api/state':
      self._send_json(srv.controller.get_state_payload(
          window_hours=ge_fleet.parse_window_hours(_first_query_value(query, 'window_hours')),
      ))
      return
    if path == '/api/gcp_telemetry':
      self._send_json(srv.controller.gcp_telemetry.get_telemetry_summary_payload())
      return
    if path == '/api/user_centric_finops':
      self._send_json(srv.controller.get_user_centric_finops_payload(
          window_hours=ge_fleet.parse_window_hours(_first_query_value(query, 'window_hours')),
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
    if path == '/api/prompt_xray/live_turns':
      self._send_json(srv.controller.prompt_xray_live_turns())
      return
    if path in ('/api/dead_letters', '/api/dead-letters'):
      self._send_json(srv.controller.dead_letters_payload())
      return
    self.send_error(404, 'Not Found')

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    """Serves POST routes for ingest, agent selection, custom parameters, GCP sync, and the simulator."""
    srv = self.server
    assert isinstance(srv, VibeLiftHttpServer)
    payload, valid_json = self._read_json_payload()
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}
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
    if self.path == '/a2a/app':
      self._send_json(srv.controller.handle_a2a_task(body))
      return
    if not dashboard_surface_enabled():
      self.send_error(404, 'Not Found')
      return
    ingest_routes = {
        '/api/decorator_ingest': srv.controller.ingest_decorator_event,
        '/api/aive_log': srv.controller.ingest_aive_log,
        '/api/csat_rating': srv.controller.submit_csat_rating,
    }
    if self.path in ingest_routes:
      self._send_ingest(self.path, ingest_routes[self.path], payload, valid_json)
      return
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
    if self.path == '/api/nl2sql':
      question = str(body.get('question', 'Compare cost per 1k turns and prompt cache savings across agents'))
      self._send_json(srv.controller.query_nl2sql(question))
      return
    if self.path == '/api/what_if_simulate':
      self._send_json(srv.controller.simulate_what_if(body))
      return
    if self.path == '/api/what_if_live':
      self._send_json(srv.controller.what_if_live(body))
      return
    if self.path == '/api/prompt_xray':
      self._send_json(srv.controller.prompt_xray(body))
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
    if self.path == '/api/sync_ge_fleet':
      self._send_json({
          'status': 'synced',
          'fleet': srv.controller.get_fleet_payload(
              window_hours=ge_fleet.parse_window_hours(body.get('window_hours')), force_refresh=True),
      })
      return
    if self.path == '/api/ge_mart/refresh':
      self._send_json(srv.controller.refresh_ge_mart(body))
      return
    if self.path in ('/api/enable_agent_observability', '/api/agent_observability'):
      self._send_json(srv.controller.enable_agent_observability(body))
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

  def _send_json(self, payload: Mapping[str, Any] | dict[str, Any], status: int = 200) -> None:
    """Serializes payload as JSON and writes the response (HTTP 200 unless `status` says otherwise)."""
    body = json.dumps(payload).encode('utf-8')
    self.send_response(status)
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


def build_agent_card(request_base_url: str | None = None) -> dict[str, Any]:
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
          'Monitoring and Cloud Logging telemetry), prompt cache FinOps economics, and an optimizer '
          'simulator (synthetic numbers), with an interactive MCP App dashboard.'
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
                  'agent telemetry, prompt cache economics, or the optimizer simulator in the interactive UI.'
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
              'description': (
                  'Optimizer simulator: one generation of fixed improvement factors on a demo agent profile '
                  '(synthetic numbers; no model is called and no agent is changed).'
              ),
              'tags': ['simulator', 'optimization'],
          },
      ],
  }


def register_api_routes(app: Any, controller: VibeLiftRuntimeController) -> None:
  """Registers the dashboard UI, agent card, and JSON API routes on a FastAPI app.

  Handlers are plain ``def`` so FastAPI runs the blocking Google Cloud calls in its threadpool.
  """

  def _ingest_error_response(_request: Any, exc: Exception) -> Any:
    content = exc.to_response() if isinstance(exc, telemetry.IngestValidationError) else {'error': 'invalid_payload'}
    return JSONResponse(status_code=422, content=content)

  app.add_exception_handler(telemetry.IngestValidationError, _ingest_error_response)

  async def _ingest(request: Any, source: str, handler: Any) -> Any:
    """Parses the raw body itself so malformed JSON gets the same 422 and dead-letter record."""
    raw = await request.body()
    payload: Any = None
    if raw.strip():
      try:
        payload = json.loads(raw)
      except ValueError:
        telemetry.raise_if_invalid(source, raw.decode('utf-8', errors='replace'), ['body: not valid JSON'])
    return await run_in_threadpool(handler, payload)

  @app.get('/.well-known/agent-card.json')
  @app.get('/a2a/app/.well-known/agent-card.json')
  def get_agent_card(request: fastapi.Request):
    return JSONResponse(content=build_agent_card(base_url_from_headers(request.headers)), status_code=200)

  @app.post('/a2a/app')
  def post_a2a_task(payload: dict = fastapi.Body(default={})):
    return controller.handle_a2a_task(payload)

  # /healthz is for container probes; Cloud Run's front end reserves paths ending in "z", so browsers
  # and external checks use /api/health instead.
  @app.get('/healthz')
  @app.get('/api/health')
  def get_healthz():
    return {
        'status': 'ok',
        'service': 'vibelift',
        'runtime': 'cloud_run',
        'surface': resolve_surface(),
        'project': controller.gcp_telemetry.project_id,
        'region': controller.gcp_telemetry.region,
    }

  if not dashboard_surface_enabled():
    logger.info('VIBELIFT_SURFACE=%s: dashboard UI and /api/* routes are not registered', resolve_surface())
    return

  @app.get('/', response_class=HTMLResponse)
  @app.get('/ui', response_class=HTMLResponse)
  @app.get('/app', response_class=HTMLResponse)
  def get_dashboard():
    return HTMLResponse(content=ui_template.render_dashboard_html(), status_code=200,
                        headers=dict(DASHBOARD_SECURITY_HEADERS))

  @app.get('/vibelift_googley_logo_1789766299532.jpg')
  @app.get('/logo.jpg')
  def get_logo():
    b64_part = logo_asset.VIBELIFT_GOOGLEY_LOGO_DATA_URI.split(',', 1)[1]
    return Response(content=base64.b64decode(b64_part), media_type='image/jpeg')

  @app.get('/api/state')
  def get_state(window_hours: str | None = None):
    return controller.get_state_payload(window_hours=ge_fleet.parse_window_hours(window_hours))

  @app.get('/api/dead_letters')
  @app.get('/api/dead-letters')
  def get_dead_letters():
    return controller.dead_letters_payload()

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
  def post_refresh_ge_mart(payload: dict = fastapi.Body(default={})):
    return controller.refresh_ge_mart(payload)

  @app.post('/api/enable_agent_observability')
  @app.post('/api/agent_observability')
  def post_enable_agent_observability(payload: dict = fastapi.Body(default={})):
    return controller.enable_agent_observability(payload)

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
  async def post_decorator_ingest(request: fastapi.Request):
    return await _ingest(request, '/api/decorator_ingest', controller.ingest_decorator_event)

  @app.post('/api/nl2sql')
  def post_nl2sql(payload: dict = fastapi.Body(default={})):
    question = str(payload.get('question', 'Compare cost per 1k turns and prompt cache savings across agents'))
    return controller.query_nl2sql(question)

  @app.post('/api/aive_log')
  async def post_aive_log(request: fastapi.Request):
    return await _ingest(request, '/api/aive_log', controller.ingest_aive_log)

  @app.post('/api/csat_rating')
  async def post_csat_rating(request: fastapi.Request):
    return await _ingest(request, '/api/csat_rating', controller.submit_csat_rating)

  @app.post('/api/what_if_simulate')
  def post_what_if_simulate(payload: dict = fastapi.Body(default={})):
    return controller.simulate_what_if(payload)

  @app.post('/api/what_if_live')
  def post_what_if_live(payload: dict = fastapi.Body(default={})):
    return controller.what_if_live(payload)

  @app.post('/api/prompt_xray')
  def post_prompt_xray(payload: dict = fastapi.Body(default={})):
    return controller.prompt_xray(payload)

  @app.get('/api/prompt_xray/live_turns')
  def get_prompt_xray_live_turns():
    return controller.prompt_xray_live_turns()

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


def resolve_allowed_origins() -> list[str]:
  """Resolves allowed CORS origins from ALLOWED_ORIGINS env var (defaults to ['*'] when unset)."""
  raw = os.environ.get('ALLOWED_ORIGINS', '*').strip()
  if not raw or raw == '*':
    return ['*']
  return [origin.strip() for origin in raw.split(',') if origin.strip()] or ['*']


# ---------------------------------------------------------------------------
# FastAPI Application for Cloud Run Production Deployment & ADK Framework
# ---------------------------------------------------------------------------
if fastapi is not None:
  app = fastapi.FastAPI(
      title='VibeLift | Analytics Platform for Agent Optimization',
      description='Cloud Run & ADK runtime for agent telemetry, prompt cache FinOps and an optimizer simulator',
      version='1.1.0',
  )

  app.add_middleware(
      CORSMiddleware,
      allow_origins=resolve_allowed_origins(),
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
