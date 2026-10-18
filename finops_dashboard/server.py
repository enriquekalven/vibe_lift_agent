"""HTTP server, FastAPI app, and REST API for FinOps Analytics Platform on Google Cloud."""

import http.server
import json
import logging
import os
import socket
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from finops_dashboard.jsonutil import as_list, as_mapping

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
  from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:
  fastapi = None  # type: ignore[assignment]

from finops_dashboard import (
  agy_finops,
  billing_export,
  cost_governance,
  gcp_telemetry,
  mcp_server,
  prompt_xray,
  telemetry,
)
from finops_dashboard import finops as live_finops
from finops_dashboard import fleet as ge_fleet
from finops_dashboard import optimizer as alpha_evolve_optimizer
from finops_dashboard import validator as telemetry_validator
from finops_dashboard.ui import template as ui_template

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Opt-in for the Prompt Cache X-Ray live mode, which reads logged (raw) user prompts from Cloud Storage.
PROMPT_XRAY_LIVE_ENV = 'FINOPS_PROMPT_XRAY_LIVE'

# How long a new Cloud Run instance waits at startup for the BigQuery insights before it takes traffic
# (FinOpsRuntimeController.prime_live_caches). A fetch normally takes 2-3 s; this stays well inside
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
  """Returns the active service surface ('all', 'mcp', or 'dashboard') from FINOPS_SURFACE."""
  raw = (configured if configured is not None else os.environ.get('FINOPS_SURFACE', 'all')).strip().lower()
  return raw if raw in VALID_SURFACES else 'all'


def dashboard_surface_enabled() -> bool:
  """True when the browser dashboard and REST API routes are enabled."""
  return resolve_surface() in ('all', 'dashboard')


import math
import threading
import uuid


def build_code_error_response(where: str, exc: BaseException) -> dict[str, Any]:
  """Structured diagnostic contract for unexpected application code errors (HTTP 500).

  Explicitly tells the user this is an internal FinOps Dashboard bug (not bad input or GCP IAM),
  confirms dashboard state was preserved, and provides a correlation Request ID without
  leaking stack traces or file paths to the browser.
  """
  req_id = f'req-{uuid.uuid4().hex[:8]}'
  exc_type = type(exc).__name__
  logger.error(
      'Unhandled application code error [%s] in %s (%s): %s',
      req_id,
      where,
      exc_type,
      exc,
      exc_info=True,
  )
  diag_cmd = (
      f'gcloud logging read \'jsonPayload.request_id="{req_id}" '
      f'OR textPayload:"{req_id}"\' --limit=10'
  )
  return {
      'status': 'ERROR',
      'error_kind': 'INTERNAL_CODE_ERROR',
      'error_code': 'VL-CODE-500',
      'is_code_bug': True,
      'state_preserved': True,
      'request_id': req_id,
      'where': where,
      'exception_type': exc_type,
      'error': f'Internal application code error in FinOps Dashboard ({exc_type} at {where}).',
      'user_message': (
          f'Internal application code error in FinOps Dashboard ({exc_type} at {where}). '
          'This is a bug in FinOps Dashboard code — NOT an issue with your input or GCP permissions. '
          'Your current dashboard view and state were preserved.'
      ),
      'what_to_do': (
          f'Copy Request ID {req_id} to report this code bug, or run the diagnostic command below '
          'to inspect the server traceback in Cloud Logging.'
      ),
      'diagnostic_cmd': diag_cmd,
  }


def build_validation_error_response(
    where: str,
    errors: Sequence[str],
    what_to_do: str = 'Check the highlighted input fields and try again. Your current dashboard state was not modified.',
) -> dict[str, Any]:
  """Structured response for user input / validation errors (HTTP 422 / 400)."""
  err_list = [str(e) for e in errors if e] or ['Invalid request parameters.']
  return {
      'status': 'ERROR',
      'error_kind': 'VALIDATION_ERROR',
      'error_code': 'VL-INPUT-422',
      'is_code_bug': False,
      'state_preserved': True,
      'where': where,
      'error': '; '.join(err_list),
      'errors': err_list,
      'user_message': f'Invalid input for {where}: {"; ".join(err_list)}',
      'what_to_do': what_to_do,
  }


def _parse_finite_float(val: Any, default: float | None = None) -> tuple[float | None, bool]:
  """Returns (finite_float, is_valid). Empty/None returns (default, True) when default is provided."""
  if val is None or val == '':
    return default, (default is not None)
  if isinstance(val, bool):
    return default, False
  try:
    parsed = float(str(val))
  except (TypeError, ValueError):
    return default, False
  if not math.isfinite(parsed):
    return default, False
  return parsed, True


def _parse_finite_int(val: Any, default: int | None = None) -> tuple[int | None, bool]:
  """Returns (finite_int, is_valid)."""
  fval, ok = _parse_finite_float(val, float(default) if default is not None else None)
  if not ok or fval is None:
    return default, ok
  try:
    return int(fval), True
  except (TypeError, ValueError, OverflowError):
    return default, False


def _parse_add_parameter_body(body: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
  """Validates /api/add_parameter inputs so NaN/Inf/non-numeric strings never cause 500 errors."""
  raw = body if isinstance(body, Mapping) else {}
  errors: list[str] = []
  label = str(raw.get('label') if raw.get('label') is not None else 'Custom Metric').strip() or 'Custom Metric'
  unit = str(raw.get('unit') if raw.get('unit') is not None else '%').strip() or '%'
  direction = str(raw.get('direction') if raw.get('direction') is not None else 'HIGHER').strip().upper() or 'HIGHER'
  if direction not in ('HIGHER', 'LOWER', 'HIGHER_IS_BETTER', 'LOWER_IS_BETTER'):
    errors.append('direction: must be HIGHER or LOWER')
  base_val, base_ok = _parse_finite_float(raw.get('baseline_val'), 85.0)
  if not base_ok or base_val is None:
    errors.append('baseline_val: must be a finite number')
  targ_val, targ_ok = _parse_finite_float(raw.get('target_val'), 98.0)
  if not targ_ok or targ_val is None:
    errors.append('target_val: must be a finite number')
  weight_val, weight_ok = _parse_finite_int(raw.get('weight_pct'), 10)
  if not weight_ok or weight_val is None or weight_val < 0 or weight_val > 100:
    errors.append('weight_pct: must be a finite integer between 0 and 100')
  return {
      'label': label[:120],
      'unit': unit[:24],
      'direction': direction,
      'baseline_val': float(base_val if base_val is not None else 85.0),
      'target_val': float(targ_val if targ_val is not None else 98.0),
      'weight_pct': int(weight_val if weight_val is not None else 10),
  }, errors


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


class FinOpsRuntimeController:
  """Coordinates live GCP telemetry and the GE fleet, plus the optimizer simulator's demo agents."""

  def __init__(self, fleet_service: ge_fleet.GeminiEnterpriseFleetService | None = None) -> None:
    """Initializes the optimizer, long-running agent, GCP telemetry client, and fleet service."""
    self._lock = threading.Lock()
    self._warmer_started = False
    self._last_fleet_payload: dict[str, Any] | None = None
    self.optimizer = alpha_evolve_optimizer.FinOpsAlphaEvolveOptimizer()
    self.agent = telemetry.CloudTurnStore(
        agent_name=self.optimizer.active_agent.agent_id,
        model=self.optimizer.active_agent.model,
    )
    self.gcp_telemetry = gcp_telemetry.GoogleCloudTelemetryService()
    self.billing_export = billing_export.BillingExportReader(
        self.gcp_telemetry.project_id, self.gcp_telemetry._get_access_token  # pylint: disable=protected-access
    )
    self.ge_fleet = fleet_service or ge_fleet.get_ge_fleet_service()
    if (
        getattr(self.ge_fleet, '_bq_query_fn', None) is None
        and (
            os.environ.get('FINOPS_BQ_TOKEN_PUSHDOWN')
            or os.environ.get('FINOPS_BQ_SNAPSHOT_ENABLED')
        )
    ):
      try:
        self.ge_fleet._bq_query_fn = (  # pylint: disable=protected-access
            lambda *a, **kw: self.gcp_telemetry.run_bigquery_query(*a, **kw)
        )
      except Exception:
        pass
    self.cost_governance = cost_governance.CostGovernanceService(
        project_id=self.gcp_telemetry.project_id,
        bq_query_fn=lambda *a, **kw: self.gcp_telemetry.run_bigquery_query(*a, **kw),
        token_fn=lambda: self.gcp_telemetry._get_access_token(),  # pylint: disable=protected-access
        api_call_fn=cost_governance.google_api_caller(
            lambda: self.gcp_telemetry._get_access_token()  # pylint: disable=protected-access
        ),
    )

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

    threading.Thread(target=_warm_loop, daemon=True, name='finops-fleet-warmer').start()

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
      # The UI reads fleet.live_finops before state.live_finops, so give it the same billing / mart inputs.
      fleet = dict(fleet)
      fleet['live_finops'] = self._live_finops(
          fleet,
          daily_usage=self._ge_daily_usage(bq_insights),
          sessions=list(bq_insights.get('ge_sessions') or []) if isinstance(bq_insights, dict) else None,
          session_turns=bq_insights.get('ge_session_turns') if isinstance(bq_insights, dict) else None,
      )
      # Per-agent loop thresholds for the health card (copies; the collector's cached agents stay untouched).
      fleet['agents'] = live_finops.apply_loop_thresholds(
          fleet.get('agents'), fleet['live_finops'].get('loop_baselines'))
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
      *,
      allow_stale: bool | None = None,
      max_wait_s: float | None = None,
      lite_boot: bool = False,
  ) -> dict[str, Any]:
    """Builds the JSON payload for the dashboard (optimization tabs plus, optionally, the live GE fleet)."""
    effective_allow_stale = bool(fast_mcp) if allow_stale is None else bool(allow_stale)
    effective_max_wait = (0.35 if fast_mcp else (0.4 if effective_allow_stale else None)) if max_wait_s is None else max_wait_s
    fleet_payload = None
    if include_fleet:
      fleet_payload = self.get_fleet_payload(
          window_hours=window_hours,
          allow_stale=effective_allow_stale,
          max_wait_s=effective_max_wait,
      )

    bq_insights = None
    if self._is_live_gcp():
      bq_insights = self.gcp_telemetry.fetch_live_bigquery_project_insights(
          non_blocking=effective_allow_stale, window_hours=window_hours
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

    services = self.gcp_telemetry.list_cloud_run_agent_services(non_blocking=effective_allow_stale)
    support_events = self.gcp_telemetry.fetch_gemini_enterprise_support_telemetry(
        limit=6, non_blocking=effective_allow_stale
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
      live_bq = bq_insights if isinstance(bq_insights, dict) else self.optimizer._live_bq_insights
      live_finops_payload = self._live_finops(
          fleet_payload or self.optimizer._live_fleet_payload,
          daily_usage=ge_daily_usage,
          sessions=list(live_bq.get('ge_sessions') or []) if isinstance(live_bq, dict) else None,
          session_turns=live_bq.get('ge_session_turns') if isinstance(live_bq, dict) else None,
      )
      # Never show demo personas in live mode: if BigQuery principals have not
      # loaded yet, show an empty table with an explicit loading status.
      user_centric = dict(user_centric)
      if isinstance(live_bq, dict):
        user_centric['ge_sessions'] = list(live_bq.get('ge_sessions') or [])
        if lite_boot:
          user_centric['ge_session_turns'] = {}
          user_centric['ge_sessionless_token_turns'] = []
          user_centric['lazy_turns_deferred'] = True
        else:
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
    antigravity_finops_payload = (
        None
        if lite_boot
        else self.get_antigravity_finops_payload(
            window_hours=window_hours,
            fleet_payload=fleet_payload or self._last_fleet_payload,
            allow_live_fetch=False,
        )
    )
    effective_fleet = (
        fleet_payload
        or self._last_fleet_payload
        or (self.optimizer._live_fleet_payload if self._is_live_gcp() else None)
    )
    if effective_fleet is None and not self._is_live_gcp():
      effective_fleet = self.get_fleet_payload(allow_stale=False)
    self.cost_governance.project_id = self.gcp_telemetry.project_id
    cost_gov_summary = self.cost_governance.evaluate_and_warn_idle_runtimes(
        effective_fleet,
        send_warnings=False,
        lookup_bq=False,
        persist_to_bq=False,
        user_centric_payload=as_mapping(user_centric) if isinstance(user_centric, Mapping) else None,
        antigravity_payload=(
            as_mapping(antigravity_finops_payload)
            if isinstance(antigravity_finops_payload, Mapping)
            else None
        ),
        ge_daily_usage=as_mapping(ge_daily_usage) if isinstance(ge_daily_usage, Mapping) else None,
    )
    fleet_stale = bool(
        isinstance(fleet_payload, Mapping)
        and (
            fleet_payload.get('stale_revalidating')
            or fleet_payload.get('warming')
            or as_mapping(fleet_payload.get('source_status')).get('inventory') == 'warming'
        )
    )
    bq_stale = bool(live_gcp and not (isinstance(bq_insights, dict) and bq_insights))
    stale_revalidating = bool(fleet_stale or bq_stale)
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
        'antigravity_finops': antigravity_finops_payload,
        'cost_governance': cost_gov_summary,
        'lite_boot': bool(lite_boot),
        'swr': {
            'stale_revalidating': stale_revalidating,
            'fleet_stale': fleet_stale,
            'bq_stale': bq_stale,
            'lite_boot': bool(lite_boot),
        },
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
    payload['telemetry_validation'] = telemetry_validator.validate_dashboard_state(
        payload,
        ge_fleet_payload=effective_fleet,
        bq_insights=bq_insights,
        run_llm_judge=False,
    )
    return payload

  def evaluate_idle_governance(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluates idle/zombie runtimes and optionally dispatches Stage 1 warning notifications."""
    raw = body if isinstance(body, Mapping) else {}
    window_hours = ge_fleet.parse_window_hours(raw.get('window_hours'))
    send_warnings = ge_fleet.parse_bool(raw.get('send_warnings', True))
    dry_run_email = ge_fleet.parse_bool(raw.get('dry_run_email', False))
    grace_hours, grace_ok = _parse_finite_int(
        raw.get('grace_period_hours'), cost_governance.DEFAULT_GRACE_PERIOD_HOURS
    )
    persist_to_bq = ge_fleet.parse_bool(raw.get('persist_to_bq', True)) and self._is_live_gcp()
    lookup_bq = ge_fleet.parse_bool(raw.get('lookup_bq', True)) and self._is_live_gcp()
    fleet = self.get_fleet_payload(window_hours=window_hours, allow_stale=True, max_wait_s=5.0)
    self.cost_governance.project_id = self.gcp_telemetry.project_id
    user_centric = as_mapping(self.optimizer.get_user_centric_payload())
    return self.cost_governance.evaluate_and_warn_idle_runtimes(
        fleet,
        grace_period_hours=(
            grace_hours if grace_ok and grace_hours else cost_governance.DEFAULT_GRACE_PERIOD_HOURS
        ),
        send_warnings=send_warnings,
        dry_run_email=dry_run_email,
        lookup_bq=lookup_bq,
        persist_to_bq=persist_to_bq,
        user_centric_payload=user_centric,
    )

  def execute_scheduled_clearance(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Executes Stage 2 scheduled clearance after the grace period expires (dry_run by default)."""
    raw = body if isinstance(body, Mapping) else {}
    window_hours = ge_fleet.parse_window_hours(raw.get('window_hours'))
    raw_dry_run = raw.get('dry_run')
    if raw_dry_run is None:
      mode_str = str(raw.get('mode') or '').strip().lower()
      dry_run = mode_str != 'enforced' if mode_str else True
    elif isinstance(raw_dry_run, bool):
      dry_run = raw_dry_run
    else:
      dry_run = ge_fleet.parse_bool(raw_dry_run)
    fleet = self.get_fleet_payload(window_hours=window_hours, allow_stale=True, max_wait_s=5.0)
    self.cost_governance.project_id = self.gcp_telemetry.project_id
    return self.cost_governance.execute_scheduled_clearance(
        fleet,
        mode='dry_run' if dry_run else 'enforced',
        dry_run=dry_run,
        lookup_bq=self._is_live_gcp(),
        persist_to_bq=self._is_live_gcp(),
    )

  def snooze_governance_runtime(
      self,
      runtime_key: str,
      token: str,
      snooze_days: int | None = None,
      actor: str = 'owner_link',
  ) -> dict[str, Any]:
    """Verifies an HMAC snooze token (if provided) and snoozes scheduled clearance for a runtime."""
    self.cost_governance.project_id = self.gcp_telemetry.project_id
    return self.cost_governance.snooze_runtime(
        runtime_id=runtime_key,
        days=snooze_days or 7,
        token=token,
        verify_token=bool(token),
        lookup_bq=self._is_live_gcp(),
        persist_to_bq=self._is_live_gcp(),
        actor=actor,
    )

  def executive_digest_payload(
      self,
      body: Mapping[str, Any] | None = None,
      send_email: bool = False,
  ) -> dict[str, Any]:
    """Builds and optionally dispatches the daily/weekly FinOps executive digest."""
    raw = body if isinstance(body, Mapping) else {}
    try:
      recipient = cost_governance.normalize_recipient_list(
          raw.get('recipient', raw.get('recipients'))
      ) or None
    except ValueError as exc:
      return {'status': 'INVALID_REQUEST', 'error': str(exc)}
    window_hours = ge_fleet.parse_window_hours(raw.get('window_hours')) or 168
    cadence = str(raw.get('cadence') or 'weekly').strip().lower()
    dry_run_flag = ge_fleet.parse_bool(raw.get('dry_run', not send_email))
    fleet = self.get_fleet_payload(window_hours=window_hours, allow_stale=True, max_wait_s=5.0)
    self.cost_governance.project_id = self.gcp_telemetry.project_id
    self.cost_governance.evaluate_and_warn_idle_runtimes(
        fleet,
        send_warnings=False,
        lookup_bq=self._is_live_gcp(),
        persist_to_bq=False,
    )
    user_centric = as_mapping(self.optimizer.get_user_centric_payload())
    return self.cost_governance.build_and_send_executive_digest(
        fleet,
        user_centric_payload=user_centric,
        cadence=cadence,
        recipient=recipient,
        dry_run=dry_run_flag,
        lookup_bq=self._is_live_gcp(),
    )

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
    """Per-day GE usage (finops_mart.fct_turns) joined with billed AI spend (billing export)."""
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

  def get_antigravity_finops_payload(
      self,
      window_hours: int | None = None,
      force_refresh: bool = False,
      fleet_payload: Mapping[str, Any] | None = None,
      allow_live_fetch: bool = False,
  ) -> dict[str, Any]:
    """Returns Google Antigravity IDE FinOps telemetry (live collector on GCP, deterministic demo in DEMO mode)."""
    if self._is_live_gcp():
      effective = fleet_payload or self._last_fleet_payload or self.optimizer._live_fleet_payload
      if not force_refresh and isinstance(effective, Mapping):
        cached = effective.get('antigravity_usage')
        if isinstance(cached, Mapping):
          return dict(cached)
      if allow_live_fetch or force_refresh:
        collector = getattr(self.ge_fleet, 'collect_antigravity_finops', None)
        if callable(collector):
          return collector(window_hours=window_hours, force_refresh=force_refresh)
      return agy_finops.build_pending_antigravity_payload(
          project_id=self.gcp_telemetry.project_id,
          window_hours=window_hours or 168,
          note=(
              'Not collected yet: Antigravity usage is read with the fleet refresh (when unregistered-runtime '
              'discovery is on) or on demand from GET /api/antigravity.'
          ),
      )
    return agy_finops.build_demo_antigravity_payload(
        project_id=self.gcp_telemetry.project_id,
        window_hours=window_hours or 168,
    )

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
    """Refreshes finops_mart.fct_turns (full rebuild or incremental MERGE) and returns refreshed state."""
    raw = body if isinstance(body, Mapping) else {}
    incremental = ge_fleet.parse_bool(raw.get('incremental'))
    parsed_days, ok = _parse_finite_int(raw.get('lookback_days'), 3)
    lookback_days = max(1, min(365, parsed_days if (ok and parsed_days is not None) else 3))
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
    raw = body if isinstance(body, Mapping) else {}
    # TODO(security): with no resource_name/engine_id the change applies fleet-wide (every matched agent).
    # The dashboard and MCP tool always send an explicit target or `only_low_code`; a future revision
    # should require an explicit `confirm_all: true` for the fleet-wide form of this write.
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

  def _live_finops(
      self,
      fleet: Mapping[str, Any] | None,
      *,
      daily_usage: Mapping[str, Any] | None = None,
      sessions: Sequence[Mapping[str, Any]] | None = None,
      session_turns: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
  ) -> dict[str, Any]:
    payload = live_finops.build_live_finops(
        fleet, daily_usage=daily_usage, sessions=sessions, session_turns=session_turns
    )
    payload['rate_card_models'] = sorted(self._rate_cards())
    return payload

  def what_if_live(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Projection from observed model usage (model switch or cache share); never uses example inputs."""
    raw = body if isinstance(body, Mapping) else {}
    fleet = self.get_fleet_payload(
        window_hours=ge_fleet.parse_window_hours(raw.get('window_hours')), allow_stale=True, max_wait_s=20.0)
    usage = fleet.get('model_usage') if isinstance(fleet, Mapping) else None
    cards = self._rate_cards()
    try:
      if str(raw.get('kind') or 'model_switch') == 'cache_share':
        raw_target = raw.get('target_cache_share_pct')
        if isinstance(raw_target, bool):
          raise ValueError('Invalid numeric input.')
        return live_finops.project_cache_share(
            usage, str(raw.get('model') or ''), float(0.0 if raw_target in (None, '') else raw_target), cards)
      raw_share = raw.get('share_pct')
      if isinstance(raw_share, bool):
        raise ValueError('Invalid numeric input.')
      return live_finops.project_model_switch(
          usage, str(raw.get('from_model') or ''), str(raw.get('to_model') or ''),
          float(0.0 if raw_share in (None, '') else raw_share), cards)
    except (TypeError, ValueError, OverflowError):
      return {
          'status': 'ERROR',
          'error_kind': 'VALIDATION_ERROR',
          'error_code': 'VL-INPUT-422',
          'is_code_bug': False,
          'state_preserved': True,
          'where': '/api/what_if_live',
          'error': 'Invalid numeric input.',
          'what_to_do': 'Enter a finite percentage between 0 and 100.',
      }

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
                          'FINOPS_PROMPT_LOG_BUCKET=<bucket> to do both).')}
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
    raw = body if isinstance(body, Mapping) else {}

    def _pos_int(key: str) -> int | None:
      val, ok = _parse_finite_int(raw.get(key))
      return val if (ok and val is not None and val > 0) else None

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
      return {
          'status': 'ERROR',
          'error_kind': 'VALIDATION_ERROR',
          'error_code': 'VL-INPUT-422',
          'is_code_bug': False,
          'state_preserved': True,
          'where': '/api/prompt_xray',
          'error': str(exc),
          'what_to_do': 'Paste non-empty previous and current prompts and select a supported Gemini model.',
      }

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
    raw = body if isinstance(body, Mapping) else {}
    model_tier = str(raw.get('model_tier') or 'gemini-3.1-flash-tier-routed')
    tb, tb_ok = _parse_finite_int(raw.get('thinking_budget_tok'), 1024)
    thinking_budget_tok = tb if (tb_ok and tb is not None) else 1024
    hw, hw_ok = _parse_finite_int(raw.get('history_window_turns'), 6)
    history_window_turns = hw if (hw_ok and hw is not None) else 6
    tc, tc_ok = _parse_finite_int(raw.get('traffic_canary_pct'), 15)
    traffic_canary_pct = tc if (tc_ok and tc is not None) else 15
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
      self.agent = telemetry.CloudTurnStore(
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
    """Records one @finops_telemetry event posted by an agent's message-passing hook.

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
    """Adds a custom user-defined parameter to the currently selected agent (simulator only)."""
    if self._is_live_gcp() and not self._simulator_enabled_in_live_mode():
      return {
          'status': 'DISABLED_IN_LIVE_MODE',
          'reason': (
              'Adding synthetic KPI parameters to the demo agent is disabled when connected to a live '
              'Google Cloud project. Live agent metrics come from BigQuery / Gemini Enterprise telemetry.'
          ),
      }
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
    return os.environ.get('FINOPS_ENABLE_SIMULATOR', '').strip().lower() in ('1', 'true', 'yes', 'on')

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
    """Executes an A2A JSON-RPC 2.0 task request (/a2a/app) against live FinOps Dashboard skills."""
    raw = body if isinstance(body, Mapping) else {}
    rpc_id = raw.get('id', 1)
    method = str(raw.get('method') or 'tasks/send').strip()
    params = as_mapping(raw.get('params'))
    task_id = str(params.get('id') or 'finops-a2a-task-1')
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
      u_tok, u_ok = _parse_finite_int(params.get('uncached_input_tokens'), 2000)
      c_tok, c_ok = _parse_finite_int(params.get('cached_input_tokens'), 18000)
      o_tok, o_ok = _parse_finite_int(params.get('output_tokens'), 500)
      uncached_tok = max(0, u_tok if (u_ok and u_tok is not None) else 2000)
      cached_tok = max(0, c_tok if (c_ok and c_tok is not None) else 18000)
      output_tok = max(0, o_tok if (o_ok and o_tok is not None) else 500)
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
    """Resets the optimizer catalog and ingested SDK events (disabled in live mode)."""
    if self._is_live_gcp() and not self._simulator_enabled_in_live_mode():
      return {
          'status': 'DISABLED_IN_LIVE_MODE',
          'reason': (
              'Resetting the demo state is disabled when connected to a live Google Cloud project: '
              'it would also discard ingested SDK events and AIVE logs. '
              'Set FINOPS_ENABLE_SIMULATOR=1 to allow it explicitly.'
          ),
      }
    with self._lock:
      self.optimizer = alpha_evolve_optimizer.FinOpsAlphaEvolveOptimizer()
      self.agent = telemetry.CloudTurnStore(
          agent_name=self.optimizer.active_agent.agent_id,
          model=self.optimizer.active_agent.model,
      )
      telemetry.reset_decorator_events()
      telemetry.reset_aive_logs()
    return self.get_state_payload(include_fleet=False)


# Shared singleton runtime controller
_global_controller = FinOpsRuntimeController()
if os.environ.get('K_SERVICE'):
  # Cloud Run imports this module before uvicorn opens the port, so this blocks traffic until loaded.
  _global_controller.prime_live_caches()
if os.environ.get('K_SERVICE') or os.environ.get('FINOPS_BACKGROUND_WARMER') == '1':
  _global_controller.start_background_warmer()


class FinOpsHttpServer(http.server.ThreadingHTTPServer):
  """Dual-stack IPv6/IPv4 HTTP server for Cloud Run, local development, and test environments."""

  allow_reuse_address = True

  def __init__(
      self,
      server_address: tuple[str, int],
      controller: FinOpsRuntimeController,
  ) -> None:
    """Initializes the HTTP server with IPv6 dual-stack when host is ::."""
    self.controller = controller
    if ':' in server_address[0]:
      self.address_family = socket.AF_INET6
    super().__init__(server_address, FinOpsRequestHandler)

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


class FinOpsRequestHandler(http.server.BaseHTTPRequestHandler):
  """Handles HTML UI and JSON API requests for FinOps Analytics Platform."""

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
    parsed = urllib.parse.urlsplit(self.path)
    path = parsed.path
    try:
      self._handle_get(parsed, path)
    except Exception as exc:  # pylint: disable=broad-except
      self._send_json(build_code_error_response(f'GET {path}', exc), status=500)

  def _handle_get(self, parsed: urllib.parse.SplitResult, path: str) -> None:
    srv = self.server
    assert isinstance(srv, FinOpsHttpServer)
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
      self._send_json({'status': 'ok', 'service': 'finops_dashboard', 'runtime': 'cloud_run', 'surface': resolve_surface()})
      return
    if not dashboard_surface_enabled():
      self.send_error(404, 'Not Found')
      return
    if path in ('/', '/ui', '/app'):
      initial_state = srv.controller.get_state_payload(
          fast_mcp=True, allow_stale=True, max_wait_s=0.35, lite_boot=True
      )
      html_bytes = ui_template.render_dashboard_html(initial_state=initial_state).encode('utf-8')
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
    if path == '/api/state':
      raw_stale = _first_query_value(query, 'allow_stale')
      allow_stale_val = True if raw_stale is None or raw_stale == '' else ge_fleet.parse_bool(raw_stale)
      raw_wait = _first_query_value(query, 'max_wait_s')
      wait_val, wait_ok = _parse_finite_float(raw_wait, 0.4 if allow_stale_val else None)
      lite_val = ge_fleet.parse_bool(_first_query_value(query, 'lite') or '')
      self._send_json(srv.controller.get_state_payload(
          window_hours=ge_fleet.parse_window_hours(_first_query_value(query, 'window_hours')),
          allow_stale=allow_stale_val,
          max_wait_s=wait_val if wait_ok else (0.4 if allow_stale_val else None),
          lite_boot=lite_val,
      ))
      return
    if path == '/api/governance/clearance_queue':
      self._send_json(srv.controller.evaluate_idle_governance({
          'window_hours': _first_query_value(query, 'window_hours'),
          'send_warnings': False,
          'persist_to_bq': False,
      }))
      return
    if path == '/api/governance/executive_digest':
      self._send_json(srv.controller.executive_digest_payload(
          {'window_hours': _first_query_value(query, 'window_hours')},
          send_email=False,
      ))
      return
    if path == '/api/governance/snooze':
      r_key = str(
          _first_query_value(query, 'runtime_id')
          or _first_query_value(query, 'resource_id')
          or _first_query_value(query, 'runtime_key')
          or ''
      ).strip()
      token = str(_first_query_value(query, 'token') or '').strip()
      days_val, days_ok = _parse_finite_int(_first_query_value(query, 'days'), 7)
      res = srv.controller.snooze_governance_runtime(
          runtime_key=r_key,
          token=token,
          snooze_days=days_val if days_ok else 7,
      )
      self._send_json(res, status=403 if res.get('status') == 'FORBIDDEN' else 200)
      return
    if path == '/api/gcp_telemetry':
      self._send_json(srv.controller.gcp_telemetry.get_telemetry_summary_payload())
      return
    if path in ('/api/antigravity', '/api/antigravity_finops'):
      self._send_json(srv.controller.get_antigravity_finops_payload(
          window_hours=ge_fleet.parse_window_hours(_first_query_value(query, 'window_hours')),
          force_refresh=ge_fleet.parse_bool((query.get('force_refresh') or [''])[0]),
          allow_live_fetch=True,
      ))
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
    if path == '/api/prompt_xray/live_turns':
      self._send_json(srv.controller.prompt_xray_live_turns())
      return
    if path in ('/api/dead_letters', '/api/dead-letters'):
      self._send_json(srv.controller.dead_letters_payload())
      return
    self.send_error(404, 'Not Found')

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    """Serves POST routes for ingest, agent selection, custom parameters, and GCP sync."""
    try:
      self._handle_post()
    except Exception as exc:  # pylint: disable=broad-except
      self._send_json(build_code_error_response(f'POST {self.path}', exc), status=500)

  def _handle_post(self) -> None:
    srv = self.server
    assert isinstance(srv, FinOpsHttpServer)
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
      parsed_params, errors = _parse_add_parameter_body(body)
      if errors:
        self._send_json(build_validation_error_response('/api/add_parameter', errors), status=422)
        return
      self._send_json(srv.controller.add_parameter(**parsed_params))
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
    if self.path in ('/api/governance/evaluate_idle', '/api/cron/warn_idle_runtimes'):
      self._send_json(srv.controller.evaluate_idle_governance(body))
      return
    if self.path in ('/api/governance/execute_clearance', '/api/cron/execute_scheduled_clearance'):
      self._send_json(srv.controller.execute_scheduled_clearance(body))
      return
    if self.path == '/api/governance/snooze':
      r_key = str(
          body.get('runtime_id')
          or body.get('resource_id')
          or body.get('runtime_key')
          or ''
      ).strip()
      token = str(body.get('token') or '').strip()
      days_val, days_ok = _parse_finite_int(body.get('days'), 7)
      res = srv.controller.snooze_governance_runtime(
          runtime_key=r_key,
          token=token,
          snooze_days=days_val if days_ok else 7,
      )
      self._send_json(res, status=403 if res.get('status') == 'FORBIDDEN' else 200)
      return
    if self.path in ('/api/governance/executive_digest', '/api/cron/send_executive_digest'):
      send_flag = ge_fleet.parse_bool(body.get('send_email', True))
      res = srv.controller.executive_digest_payload(body, send_email=send_flag)
      self._send_json(res, status=400 if res.get('status') == 'INVALID_REQUEST' else 200)
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
    logger.info('FinOps Dashboard HTTP: ' + fmt, *args)

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
    controller: FinOpsRuntimeController | None = None,
) -> FinOpsHttpServer:
  """Creates a bound FinOpsHttpServer instance."""
  ctrl = controller or _global_controller
  return FinOpsHttpServer((host, port), ctrl)


def configured_public_url() -> str:
  """Returns FINOPS_PUBLIC_URL (no trailing slash), or '' when it is not configured."""
  return os.environ.get('FINOPS_PUBLIC_URL', '').strip().rstrip('/')


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

  The advertised URL is FINOPS_PUBLIC_URL when set, otherwise the URL the request arrived on,
  so a fork or redeploy never publishes another deployment's hostname.
  """
  service_url = configured_public_url() or (request_base_url or '').rstrip('/')
  a2a_url = os.environ.get('PUBLIC_A2A_URL', '').strip() or f'{service_url}/a2a/app'
  return {
      'protocolVersion': '0.3.0',
      'name': 'finops-analytics-agent',
      'displayName': 'FinOps & Tokenomics Analytics Platform',
      'description': (
          'Live Gemini Enterprise agent fleet observability (agent inventory joined with real Cloud '
          'Monitoring, Cloud Logging, and BigQuery finops_mart telemetry), prompt cache FinOps economics, '
          'and static AST FinOps linting, with an interactive MCP App dashboard.'
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
                  'FinOps & Tokenomics Analytics Dashboard, or asks for the Gemini Enterprise agent fleet, '
                  'agent telemetry, or prompt cache economics in the interactive UI.'
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
              'id': 'lint_agent_finops',
              'name': 'lint_agent_finops',
              'description': (
                  'Runs static AST FinOps linting (FIN-01..FIN-05) on Python agent code and Prompt Cache X-Ray '
                  'prefix diffing for CI/CD gating.'
              ),
              'tags': ['linter', 'ast', 'cache', 'ci_cd'],
          },
      ],
  }


def register_api_routes(app: Any, controller: FinOpsRuntimeController) -> None:
  """Registers the dashboard UI, agent card, and JSON API routes on a FastAPI app.

  Handlers are plain ``def`` so FastAPI runs the blocking Google Cloud calls in its threadpool.
  """

  def _ingest_error_response(_request: Any, exc: Exception) -> Any:
    content = exc.to_response() if isinstance(exc, telemetry.IngestValidationError) else {'error': 'invalid_payload'}
    return JSONResponse(status_code=422, content=content)

  app.add_exception_handler(telemetry.IngestValidationError, _ingest_error_response)

  @app.middleware('http')
  async def _code_error_guard_middleware(request: Any, call_next: Any) -> Any:
    try:
      return await call_next(request)
    except telemetry.IngestValidationError as exc:
      return JSONResponse(status_code=422, content=exc.to_response())
    except Exception as exc:  # pylint: disable=broad-except
      where = f'{getattr(request, "method", "HTTP")} {getattr(getattr(request, "url", None), "path", "/api")}'
      return JSONResponse(status_code=500, content=build_code_error_response(where, exc))

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

  def _as_body(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, Mapping) else {}

  @app.get('/.well-known/agent-card.json')
  @app.get('/a2a/app/.well-known/agent-card.json')
  def get_agent_card(request: fastapi.Request):
    return JSONResponse(content=build_agent_card(base_url_from_headers(request.headers)), status_code=200)

  @app.post('/a2a/app')
  def post_a2a_task(payload: Any = fastapi.Body(default=None)):
    return controller.handle_a2a_task(_as_body(payload))

  # /healthz is for container probes; Cloud Run's front end reserves paths ending in "z", so browsers
  # and external checks use /api/health instead.
  @app.get('/healthz')
  @app.get('/api/health')
  def get_healthz():
    return {
        'status': 'ok',
        'service': 'finops_dashboard',
        'runtime': 'cloud_run',
        'surface': resolve_surface(),
        'project': controller.gcp_telemetry.project_id,
        'region': controller.gcp_telemetry.region,
    }

  if not dashboard_surface_enabled():
    logger.info('FINOPS_SURFACE=%s: dashboard UI and /api/* routes are not registered', resolve_surface())
    return

  @app.get('/', response_class=HTMLResponse)
  @app.get('/ui', response_class=HTMLResponse)
  @app.get('/app', response_class=HTMLResponse)
  def get_dashboard():
    initial_state = controller.get_state_payload(
        fast_mcp=True, allow_stale=True, max_wait_s=0.35, lite_boot=True
    )
    return HTMLResponse(
        content=ui_template.render_dashboard_html(initial_state=initial_state),
        status_code=200,
        headers=dict(DASHBOARD_SECURITY_HEADERS),
    )

  @app.get('/api/state')
  def get_state(
      window_hours: str | None = None,
      allow_stale: str | None = None,
      max_wait_s: str | None = None,
      lite: str | None = None,
  ):
    allow_stale_val = True if allow_stale is None or allow_stale == '' else ge_fleet.parse_bool(allow_stale)
    wait_val, wait_ok = _parse_finite_float(max_wait_s, 0.4 if allow_stale_val else None)
    lite_val = ge_fleet.parse_bool(lite or '')
    return controller.get_state_payload(
        window_hours=ge_fleet.parse_window_hours(window_hours),
        allow_stale=allow_stale_val,
        max_wait_s=wait_val if wait_ok else (0.4 if allow_stale_val else None),
        lite_boot=lite_val,
    )

  @app.get('/api/governance/clearance_queue')
  def get_governance_clearance_queue(window_hours: str | None = None):
    return controller.evaluate_idle_governance({
        'window_hours': window_hours,
        'send_warnings': False,
        'persist_to_bq': False,
    })

  @app.post('/api/governance/evaluate_idle')
  @app.post('/api/cron/warn_idle_runtimes')
  def post_governance_evaluate_idle(payload: Any = fastapi.Body(default=None)):
    return controller.evaluate_idle_governance(_as_body(payload))

  @app.post('/api/governance/execute_clearance')
  @app.post('/api/cron/execute_scheduled_clearance')
  def post_governance_execute_clearance(payload: Any = fastapi.Body(default=None)):
    return controller.execute_scheduled_clearance(_as_body(payload))

  @app.get('/api/governance/snooze')
  def get_governance_snooze(
      runtime_id: str | None = None,
      resource_id: str | None = None,
      runtime_key: str | None = None,
      token: str | None = None,
      days: str | None = None,
  ):
    days_val, days_ok = _parse_finite_int(days, 7)
    r_key = str(runtime_id or resource_id or runtime_key or '').strip()
    res = controller.snooze_governance_runtime(
        runtime_key=r_key,
        token=str(token or '').strip(),
        snooze_days=days_val if days_ok else 7,
    )
    if res.get('status') == 'FORBIDDEN':
      return JSONResponse(status_code=403, content=res)
    return res

  @app.post('/api/governance/snooze')
  def post_governance_snooze(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    days_val, days_ok = _parse_finite_int(body.get('days'), 7)
    r_key = str(
        body.get('runtime_id')
        or body.get('resource_id')
        or body.get('runtime_key')
        or ''
    ).strip()
    res = controller.snooze_governance_runtime(
        runtime_key=r_key,
        token=str(body.get('token') or '').strip(),
        snooze_days=days_val if days_ok else 7,
    )
    if res.get('status') == 'FORBIDDEN':
      return JSONResponse(status_code=403, content=res)
    return res

  @app.get('/api/governance/executive_digest')
  def get_governance_executive_digest(window_hours: str | None = None):
    return controller.executive_digest_payload({'window_hours': window_hours}, send_email=False)

  @app.post('/api/governance/executive_digest')
  @app.post('/api/cron/send_executive_digest')
  def post_governance_executive_digest(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    send_flag = ge_fleet.parse_bool(body.get('send_email', True))
    res = controller.executive_digest_payload(body, send_email=send_flag)
    if res.get('status') == 'INVALID_REQUEST':
      return JSONResponse(status_code=400, content=res)
    return res

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
  def sync_ge_fleet(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    return {
        'status': 'synced',
        'fleet': controller.get_fleet_payload(
            window_hours=ge_fleet.parse_window_hours(body.get('window_hours')), force_refresh=True),
    }

  @app.post('/api/ge_mart/refresh')
  def post_refresh_ge_mart(payload: Any = fastapi.Body(default=None)):
    return controller.refresh_ge_mart(_as_body(payload))

  @app.post('/api/enable_agent_observability')
  @app.post('/api/agent_observability')
  def post_enable_agent_observability(payload: Any = fastapi.Body(default=None)):
    return controller.enable_agent_observability(_as_body(payload))

  @app.get('/api/gcp_telemetry')
  def get_gcp_telemetry_endpoint():
    return controller.gcp_telemetry.get_telemetry_summary_payload()

  @app.get('/api/antigravity')
  @app.get('/api/antigravity_finops')
  def get_antigravity_finops_endpoint(window_hours: str | None = None, force_refresh: str | None = None):
    return controller.get_antigravity_finops_payload(
        window_hours=ge_fleet.parse_window_hours(window_hours),
        force_refresh=ge_fleet.parse_bool(force_refresh),
        allow_live_fetch=True,
    )

  @app.get('/api/user_centric_finops')
  def get_user_centric_finops(window_hours: str | None = None):
    return controller.get_user_centric_finops_payload(window_hours=ge_fleet.parse_window_hours(window_hours))

  @app.post('/api/sync_gcp_telemetry')
  def sync_gcp_telemetry_endpoint(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    return {
        'status': 'synced',
        'state': controller.sync_gcp_telemetry(
            window_hours=ge_fleet.parse_window_hours(body.get('window_hours'))
        ),
    }

  @app.post('/api/select_agent')
  def post_select_agent(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    return controller.select_agent(str(body.get('agent_id', 'it_service_desk')))

  @app.post('/api/select_optimizer')
  def post_select_optimizer(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    return controller.select_optimizer(str(body.get('platform_id', 'alpha_evolve')))

  @app.post('/api/decorator_ingest')
  async def post_decorator_ingest(request: fastapi.Request):
    return await _ingest(request, '/api/decorator_ingest', controller.ingest_decorator_event)

  @app.post('/api/nl2sql')
  def post_nl2sql(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    question = str(body.get('question', 'Compare cost per 1k turns and prompt cache savings across agents'))
    return controller.query_nl2sql(question)

  @app.post('/api/aive_log')
  async def post_aive_log(request: fastapi.Request):
    return await _ingest(request, '/api/aive_log', controller.ingest_aive_log)

  @app.post('/api/csat_rating')
  async def post_csat_rating(request: fastapi.Request):
    return await _ingest(request, '/api/csat_rating', controller.submit_csat_rating)

  @app.post('/api/what_if_simulate')
  def post_what_if_simulate(payload: Any = fastapi.Body(default=None)):
    return controller.simulate_what_if(_as_body(payload))

  @app.post('/api/what_if_live')
  def post_what_if_live(payload: Any = fastapi.Body(default=None)):
    return controller.what_if_live(_as_body(payload))

  @app.post('/api/prompt_xray')
  def post_prompt_xray(payload: Any = fastapi.Body(default=None)):
    return controller.prompt_xray(_as_body(payload))

  @app.get('/api/prompt_xray/live_turns')
  def get_prompt_xray_live_turns():
    return controller.prompt_xray_live_turns()

  @app.get('/api/tokenomics_cockpit')
  def get_tokenomics_cockpit_endpoint():
    return controller.recompute_finops({})

  @app.post('/api/recompute_finops')
  def post_recompute_finops(payload: Any = fastapi.Body(default=None)):
    return controller.recompute_finops(_as_body(payload))

  @app.get('/api/validate_telemetry')
  def get_validate_telemetry(run_llm_judge: str | None = None):
    return controller.validate_telemetry(run_llm_judge=ge_fleet.parse_bool(run_llm_judge))

  @app.post('/api/validate_telemetry')
  def post_validate_telemetry(payload: Any = fastapi.Body(default=None)):
    body = _as_body(payload)
    run_judge = bool(body.get('run_llm_judge', body.get('llm_judge', True)))
    return controller.validate_telemetry(run_llm_judge=run_judge)

  @app.post('/api/add_parameter')
  def post_add_parameter(payload: Any = fastapi.Body(default=None)):
    parsed_params, errors = _parse_add_parameter_body(_as_body(payload))
    if errors:
      return JSONResponse(
          status_code=422,
          content=build_validation_error_response('/api/add_parameter', errors),
      )
    return controller.add_parameter(**parsed_params)

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
      title='FinOps and Tokenomics Dashboard',
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
  """Runs the FinOps Dashboard HTTP server for local or container execution."""
  srv = create_http_server(host=host, port=port)
  actual_port = srv.server_address[1]
  logger.info(
      'FinOps Analytics Platform active on %s:%s (GCP Project: %s)',
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
