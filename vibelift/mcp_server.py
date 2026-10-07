"""Google Cloud Streamable HTTP Model Context Protocol (MCP) Server for VibeLift.

Enables Gemini Enterprise and AI clients to connect to VibeLift as a BYO MCP App:
- Serves the streamable HTTP JSON-RPC 2.0 endpoint at `/mcp`
- Publishes interactive MCP UI resource `ui://vibelift-analytics/dashboard`
- Implements MCP Tools: `open_dashboard`, `query_ge_agent_fleet`, `query_project_telemetry`,
  `calculate_prompt_cache_economics`, `run_alpha_evolve_generation` (an optimizer simulator with synthetic
  numbers; the name is kept so existing connectors keep working), `get_vibelift_state`, `xray_prompt_cache`,
  `list_prompt_snapshot_turns` and `set_agent_trace_logging`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from starlette.responses import JSONResponse, Response

from vibelift import fleet as ge_fleet
from vibelift import optimizer as alpha_evolve_optimizer
from vibelift import telemetry
from vibelift.ui import template as ui_template

logger = logging.getLogger('vibelift-mcp')

# ---------------------------------------------------------------------------
# MCP Protocol Constants & Gemini Enterprise UI Specs
# ---------------------------------------------------------------------------

SUPPORTED_PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26')


def resolve_default_protocol_version(configured: str | None) -> str:
  """Version offered when a client requests one we do not support.

  MCP_PROTOCOL_VERSION may override the default, but only with a supported version; anything else
  is ignored so a typo cannot make the server advertise a protocol it does not implement.
  """
  value = (configured or '').strip()
  return value if value in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]


MCP_PROTOCOL_VERSION = resolve_default_protocol_version(os.environ.get('MCP_PROTOCOL_VERSION'))


def mcp_app_enabled() -> bool:
  """ENABLE_MCP_APP=0/false/no/off or VIBELIFT_SURFACE=dashboard disables the /mcp endpoint."""
  if os.environ.get('VIBELIFT_SURFACE', 'all').strip().lower() == 'dashboard':
    return False
  return os.environ.get('ENABLE_MCP_APP', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _error_ref() -> str:
  """Short ID tying a sanitized client-facing error to the full server-side log entry."""
  return uuid.uuid4().hex[:12]


UI_META_KEY = 'io.modelcontextprotocol/ui'
WIDGET_URI = 'ui://vibelift-analytics/dashboard'
WIDGET_MIME = 'text/html;profile=mcp-app'

# JSON-RPC standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Content Security Policy (CSP) metadata for Gemini Enterprise sandboxed iframe
RESOURCE_META: dict[str, Any] = {
    'ui': {
        'csp': {
            'resourceDomains': [
                'https://cdn.jsdelivr.net',
                'https://fonts.googleapis.com',
                'https://fonts.gstatic.com',
                'https://*.gstatic.com',
            ],
            'connectDomains': [
                'https://*.run.app',
                'https://*.googleapis.com',
            ],
        },
        'prefersBorder': True,
        'preferredMode': 'pip',
        'displayMode': 'pip',
        'availableDisplayModes': ['pip', 'fullscreen', 'inline'],
    },
    UI_META_KEY: {
        'csp': {
            'resourceDomains': [
                'https://cdn.jsdelivr.net',
                'https://fonts.googleapis.com',
                'https://fonts.gstatic.com',
                'https://*.gstatic.com',
            ],
            'connectDomains': [
                'https://*.run.app',
                'https://*.googleapis.com',
            ],
        },
        'prefersBorder': True,
        'preferredMode': 'pip',
        'displayMode': 'pip',
        'availableDisplayModes': ['pip', 'fullscreen', 'inline'],
    },
}


def _build_ui_meta(
    visibility: list[str] | None = None,
    is_widget: bool = False,
    preferred_mode: str = 'pip',
) -> dict[str, Any]:
  """Constructs multi-dialect UI metadata for tool declarations and results."""
  ui_meta: dict[str, Any] = {}
  if visibility:
    ui_meta['visibility'] = list(visibility)
  if is_widget:
    ui_meta['resourceUri'] = WIDGET_URI
    ui_meta['preferredMode'] = preferred_mode
    ui_meta['displayMode'] = preferred_mode
    ui_meta['availableDisplayModes'] = ['pip', 'fullscreen', 'inline']
    ui_meta['icon'] = 'analytics'
    ui_meta['iconUrl'] = (
        'https://fonts.gstatic.com/s/i/short-term/release/'
        'googlesymbols/analytics/default/24px.svg'
    )

  meta: dict[str, Any] = {
      'ui': dict(ui_meta),
      UI_META_KEY: dict(ui_meta),
  }
  if is_widget:
    meta['ui/resourceUri'] = WIDGET_URI
  return meta


def _widget_html() -> str:
  """Renders the VibeLift interactive dashboard HTML bundle with AppBridge and initial state."""
  try:
    ctrl = _get_controller()
    initial_state = ctrl.get_state_payload(include_fleet=True, fast_mcp=True)
  except Exception:
    initial_state = None
  return ui_template.render_dashboard_html(initial_state=initial_state)


def _get_controller():
  """Lazy helper to get VibeLiftRuntimeController without circular import."""
  from vibelift import server
  return server._global_controller


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

# Dashboard tab ids (match switchTab() in ui_template.py). Overview is the default landing tab.
# Goals & Metrics (1), Optimizer & Testing (2) and Tools & SDK (5) are Advanced-mode tabs; asking
# for one turns Advanced mode on in the UI.
FLEET_TAB = 0
GOALS_METRICS_TAB = 1
OPTIMIZER_TESTING_TAB = 2
COST_BILLING_TAB = 3
USERS_FEEDBACK_TAB = 4
TOOLS_SDK_TAB = 5
OVERVIEW_TAB = 6
VALID_TABS = frozenset(range(7))


def _parse_focus_tab(value: Any) -> int:
  """Validates the untrusted focus_tab argument; defaults to the Overview tab."""
  try:
    tab = int(value)
  except (TypeError, ValueError):
    return OVERVIEW_TAB
  return tab if tab in VALID_TABS else OVERVIEW_TAB


async def _tool_open_dashboard(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Gateway tool: opens the VibeLift dashboard in Gemini Enterprise side panel or full screen."""
  ctrl = _get_controller()
  initial_agent = args.get('initial_agent')
  if initial_agent:
    await asyncio.to_thread(ctrl.select_agent, str(initial_agent))
  focus_tab = _parse_focus_tab(args.get('focus_tab'))
  window_hours = ge_fleet.parse_window_hours(args.get('window_hours'))
  if ge_fleet.parse_bool(args.get('force_refresh')):
    ctrl.ge_fleet._trigger_async_refresh(window_hours or ctrl.ge_fleet.default_window_hours)

  state = await asyncio.to_thread(ctrl.get_state_payload, True, window_hours, True)
  project_id = state.get('gcp_project') or 'unconfigured'
  active_agent = state.get('active_agent', {}).get('display_name', 'IT Service Desk')
  fleet = state.get('ge_fleet') or {}

  msg = (
      f'The VibeLift Analytics & FinOps Dashboard is now open for Google Cloud project `{project_id}` '
      f'(tab {focus_tab}). It shows every agent deployed on the Gemini Enterprise app with live telemetry '
      'and refreshes automatically.\n\n' + ge_fleet.summarize_fleet(fleet)
  )
  structured = {
      'session_id': session_key,
      'project_id': project_id,
      'active_agent': active_agent,
      'focus_tab': focus_tab,
      'state': state,
  }
  ui_meta = _build_ui_meta(is_widget=True, preferred_mode='pip')
  return {
      'content': [{'type': 'text', 'text': msg}],
      'structuredContent': structured,
      '_meta': ui_meta,
      'meta': ui_meta,
  }


async def _tool_query_ge_agent_fleet(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Lists every agent on the Gemini Enterprise app with live, aggregated telemetry."""
  ctrl = _get_controller()
  window_hours = ge_fleet.parse_window_hours(args.get('window_hours'))
  force_refresh = ge_fleet.parse_bool(args.get('force_refresh'))
  wait_s = 8.0 if (window_hours is not None or force_refresh) else 0.45
  fleet = await asyncio.to_thread(
      ctrl.get_fleet_payload,
      window_hours,
      force_refresh,
      True,
      wait_s,
  )
  return {
      'content': [{'type': 'text', 'text': ge_fleet.summarize_fleet(fleet)}],
      'structuredContent': fleet,
  }


async def _tool_query_project_telemetry(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Fetches live Google Cloud Project telemetry from Cloud Logging, Cloud Run, and BigQuery."""
  ctrl = _get_controller()
  if ge_fleet.parse_bool(args.get('force_refresh')):
    await asyncio.to_thread(ctrl.sync_gcp_telemetry)
  telemetry_data = await asyncio.to_thread(ctrl.gcp_telemetry.get_telemetry_summary_payload)

  text_summary = (
      f"GCP Telemetry for `{telemetry_data['project_id']}` ({telemetry_data['region']}):\n"
      f"- Deployed Cloud Run Agent Services: {telemetry_data['services_count']}\n"
      f"- Live Turns Ingested: {telemetry_data['live_turns_ingested']}\n"
      f"- Gemini Enterprise Support Events: {len(telemetry_data.get('gemini_enterprise_support_events', []))}\n"
      f"- Aggregate Prompt Tokens: {telemetry_data.get('summary_stats', {}).get('total_prompt_tokens', 0):,}\n"
      f"- Cache Hit Ratio: {telemetry_data.get('summary_stats', {}).get('overall_cache_hit_ratio', 0.0)}%"
  )
  return {
      'content': [{'type': 'text', 'text': text_summary}],
      'structuredContent': telemetry_data,
  }


async def _tool_calculate_cache_economics(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Prices the token counts the caller passed against the model's own list-price rate card.

  Nothing is assumed: omitted output or thought counts are 0, and a model without a rate card is
  reported as unpriced instead of being priced with another model's card.
  """
  model = str(args.get('model') or 'gemini-2.5-flash')
  prompt_tok = max(0, int(args.get('prompt_token_count') or 0))
  cached_tok = min(prompt_tok, max(0, int(args.get('cached_content_token_count') or 0)))
  output_tok = max(0, int(args.get('output_token_count') or 0))
  thoughts_tok = max(0, int(args.get('thoughts_token_count') or 0))
  uncached_tok = prompt_tok - cached_tok

  log_entry = telemetry.TurnUsageLog(
      timestamp=telemetry._utc_now_iso(),
      agent_name='mcp-eval-client',
      model=model,
      turn_index=1,
      prompt_prefix_hash='eval_calc',
      cache_breakpoint_line=None,
      cache_breakpoint_reason='Calculator input (no prompt text)',
      prompt_token_count=prompt_tok,
      cached_content_token_count=cached_tok,
      cache_creation_input_tokens=0,
      uncached_input_tokens=uncached_tok,
      candidates_token_count=output_tok,
      thoughts_token_count=thoughts_tok,
      status_code=200,
      tool_called='mcp.calculate_cache_economics',
      evolution_generation=0,
  )
  naive_usd, actual_usd, saved_usd = log_entry.compute_costs()
  result_payload: dict[str, Any] = {
      'model': model,
      'priced': log_entry.priced,
      'prompt_tokens': prompt_tok,
      'cached_tokens': cached_tok,
      'uncached_tokens': uncached_tok,
      'output_tokens': output_tok,
      'thoughts_tokens': thoughts_tok,
      'cache_hit_ratio_pct': log_entry.cache_hit_ratio,
      'naive_cost_usd': naive_usd,
      'actual_cost_usd': actual_usd,
      'net_savings_usd': saved_usd,
      'pricing_basis': 'Vertex AI list price from the hardcoded rate card; an estimate, not billed cost.',
  }
  if not log_entry.priced:
    result_payload['note'] = (
        f'No rate card for {model!r}, so nothing was priced. Models with a rate card: '
        + ', '.join(sorted(telemetry.RATE_CARDS)) + '.'
    )
  return {
      'content': [{'type': 'text', 'text': json.dumps(result_payload, indent=2)}],
      'structuredContent': result_payload,
  }


async def _tool_run_alpha_evolve_generation(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Runs one generation of the optimizer simulator (synthetic numbers) on a demo agent profile."""
  ctrl = _get_controller()
  agent_id = args.get('agent_id')
  if agent_id:
    await asyncio.to_thread(ctrl.select_agent, str(agent_id))
  platform_id = args.get('platform_id')
  if platform_id:
    await asyncio.to_thread(ctrl.select_optimizer, str(platform_id))

  evolved_state = await asyncio.to_thread(ctrl.evolve_generation)
  active = evolved_state.get('active_agent', {})
  timeline = active.get('timeline', [])
  latest_gen = timeline[-1] if timeline else {}
  actions = active.get('actions', [])
  latest_action = actions[0] if actions else {}

  text_summary = (
      f"SIMULATED generation {latest_gen.get('generation')} for demo profile "
      f"`{active.get('display_name') or active.get('agent_id')}`.\n"
      f"- Status: {active.get('health_status')}\n"
      f"- Simulated change: {latest_action.get('impact_summary')}\n"
      f"- {alpha_evolve_optimizer.SIMULATOR_NOTE}"
  )
  return {
      'content': [{'type': 'text', 'text': text_summary}],
      'structuredContent': evolved_state,
  }


async def _tool_get_vibelift_state(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Retrieves complete VibeLift runtime state (turns, active agent, telemetry, and parameters)."""
  ctrl = _get_controller()
  state = await asyncio.to_thread(ctrl.get_state_payload, True, None, True)
  return {
      'content': [{'type': 'text', 'text': f"VibeLift state: active agent `{state.get('active_agent', {}).get('agent_id')}`, {len(state.get('turns', []))} turns recorded."}],
      'structuredContent': state,
  }


async def _tool_xray_prompt_cache(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Prompt Cache X-Ray: where two prompt snapshots stop sharing a cacheable prefix, and what it costs."""
  ctrl = _get_controller()
  report = await asyncio.to_thread(ctrl.prompt_xray, args)
  if report.get('status') in (None, 'ERROR', 'NOT_CONNECTED', 'DISABLED', 'SNAPSHOT_UNAVAILABLE'):
    text = f"Prompt Cache X-Ray unavailable: {report.get('error') or report.get('status')}"
  else:
    bp = report.get('breakpoint') or {}
    cost = report.get('stranded_cost_usd_per_1k_requests')
    rw = report.get('rewrite') or {}
    text = (
        f"Prompt Cache X-Ray ({report.get('status')}): {report.get('cached_prefix_pct')}% of the current prompt is "
        f"a reusable prefix"
        + (f"; it breaks at line {bp.get('line')}, column {bp.get('column')} "
           f"({', '.join(c.get('label', '') for c in report.get('causes') or [])})" if bp else '')
        + (f". Stranded static tokens cost ${cost:,.4f} per 1,000 requests on {report.get('model')}" if cost else '')
        + (f". The proposed rewrite re-measures at {rw.get('projected_cached_prefix_pct')}% reusable."
           if rw.get('changed') else '.')
    )
  return {'content': [{'type': 'text', 'text': text}], 'structuredContent': report}


async def _tool_list_prompt_snapshot_turns(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Lists recent logged turns (OTel GenAI + GCS content refs) that the Prompt Cache X-Ray can compare."""
  ctrl = _get_controller()
  listing = await asyncio.to_thread(ctrl.prompt_xray_live_turns)
  text = (f"{len(listing.get('turns') or [])} logged turns available for the Prompt Cache X-Ray."
          if listing.get('status') == 'OK' else str(listing.get('message') or listing.get('status')))
  return {'content': [{'type': 'text', 'text': text}], 'structuredContent': listing}


_TRACE_TOOL_ARGS = ('resource_name', 'enabled', 'sensitive_logging', 'only_low_code', 'engine_id', 'location')


async def _tool_set_agent_trace_logging(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Enables or disables trace logging (observabilityConfig) on one agent or on all registered agents.

  Called by the dashboard's confirmed Enable/Disable trace buttons through the MCP App host bridge, or
  by the assistant when the user asks. The PATCH runs as the server's runtime service account.
  """
  if args.get('enabled') is None:
    result: dict[str, Any] = {
        'status': 'INVALID_REQUEST',
        'message': "'enabled' is required (true to enable trace logging, false to disable it).",
        'targeted_count': 0,
        'updated_count': 0,
        'results': [],
    }
  else:
    ctrl = _get_controller()
    body = {k: args[k] for k in _TRACE_TOOL_ARGS if args.get(k) is not None}
    result = await asyncio.to_thread(ctrl.enable_agent_observability, body)
  structured = {k: v for k, v in result.items() if k not in ('script', 'curl_command')}
  return {
      'content': [{'type': 'text', 'text': str(result.get('message') or result.get('status'))}],
      'structuredContent': structured,
  }


# ---------------------------------------------------------------------------
# MCP Tool Registry
# ---------------------------------------------------------------------------

# MCP ToolAnnotations. Gemini Enterprise asks the user to confirm every custom-MCP
# action unless the tool declares readOnlyHint=True, so read-only tools say so explicitly.
_READ_ONLY_LOCAL = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}
_READ_ONLY_CLOUD = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}
_STATEFUL_LOCAL = {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': False}

_TOOLS: list[dict[str, Any]] = [
    {
        'name': 'open_dashboard',
        'title': 'Open VibeLift Analytics Dashboard',
        'description': (
            'CRITICAL: Always call this tool whenever the user asks to see, open, '
            'or inspect the VibeLift Analytics & FinOps Dashboard, or asks for the Gemini Enterprise '
            'agent fleet, standalone/unregistered agents (Vertex AI Agent Engine, Cloud Run, GKE), '
            'MCP servers, Skills/tools, agent telemetry, prompt cache economics, user-centric FinOps, '
            'or the optimizer simulator in the interactive UI. Opens the interactive dashboard in the '
            'right side panel (with a Fullscreen button): every agent registered in Gemini Enterprise '
            'PLUS standalone/unregistered Agent Engines, Cloud Run services, GKE workloads, Skills, and MCP servers.'
        ),
        'visibility': ['model', 'app'],
        'widget': True,
        'inputSchema': {
            'type': 'object',
            'properties': {
                'initial_agent': {
                    'type': 'string',
                    'description': "Optional Gemini Enterprise agent to select: 'it_service_desk' (IT Service Desk), 'vibelift_analytics' (VibeLift Analytics & FinOps), or 'deep_research' (Deep Research).",
                },
                'focus_tab': {
                    'type': 'integer',
                    'description': (
                        'Tab to open: 6 = Overview (default), 0 = Agents, 3 = Cost, 4 = Users. '
                        'Advanced tabs: 1 = Goals & Metrics, 2 = Optimizer & Testing, 5 = Tools & SDK.'
                    ),
                },
                'window_hours': {
                    'type': 'integer',
                    'description': 'Telemetry window for the live fleet in hours (1-8760, default 24; e.g. 1, 6, 24, 168, 720, 2160, 4320, 8760).',
                },
            },
        },
        'annotations': _READ_ONLY_LOCAL,
        'handler': _tool_open_dashboard,
    },
    {
        'name': 'query_project_telemetry',
        'title': 'Query Google Cloud Project Telemetry',
        'description': (
            'Fetches live Google Cloud telemetry for the project hosting Cloud Run, including '
            'active Cloud Run agent microservices, BigQuery support logs from Gemini Enterprise apps, '
            'and structured usage_metadata prompt cache statistics from Cloud Logging.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'force_refresh': {
                    'type': 'boolean',
                    'description': 'If true, forces a re-sync from Google Cloud Logging.',
                },
            },
        },
        'annotations': _READ_ONLY_CLOUD,
        'handler': _tool_query_project_telemetry,
    },
    {
        'name': 'query_ge_agent_fleet',
        'title': 'Query Gemini Enterprise & Project-Wide Agent Fleet',
        'description': (
            'Lists every agent deployed on the Gemini Enterprise app PLUS standalone/unregistered runtimes '
            'across the GCP project (standalone Vertex AI Agent Engines, zombie/idle engines, Cloud Run agents, '
            'MCP servers, Skill backends, GKE agent/inference workloads, and Cloud Trace Skills/MCP tool spans) '
            'with live telemetry aggregated from Cloud Monitoring, Cloud Logging, and Cloud Trace.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'window_hours': {
                    'type': 'integer',
                    'description': 'Telemetry window in hours (1-8760, default 24; e.g. 1, 6, 24, 168, 720, 2160, 4320, 8760).',
                },
                'force_refresh': {
                    'type': 'boolean',
                    'description': 'If true, bypasses the 60-second cache and re-queries Google Cloud.',
                },
            },
        },
        'annotations': _READ_ONLY_CLOUD,
        'handler': _tool_query_ge_agent_fleet,
    },
    {
        'name': 'calculate_prompt_cache_economics',
        'title': 'Calculate Prompt Cache Economics',
        'description': (
            'Prices the token counts you pass at Vertex AI list price (an estimate, not billed cost) and returns the '
            'cache hit ratio and the saving versus no caching. Omitted output or thought counts are 0. A model '
            'without a rate card is returned unpriced. Models with a rate card: '
            + ', '.join(sorted(telemetry.RATE_CARDS)) + '.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'prompt_token_count': {
                    'type': 'integer',
                    'description': 'Total prompt (input) token count, including cached tokens.',
                },
                'cached_content_token_count': {
                    'type': 'integer',
                    'description': 'Number of input tokens read from the prompt cache.',
                },
                'output_token_count': {
                    'type': 'integer',
                    'description': 'Generated output tokens (0 if omitted).',
                },
                'thoughts_token_count': {
                    'type': 'integer',
                    'description': 'Thinking tokens billed as output (0 if omitted).',
                },
                'model': {
                    'type': 'string',
                    'description': "Model id with a rate card (default 'gemini-2.5-flash').",
                },
            },
            'required': ['prompt_token_count', 'cached_content_token_count'],
        },
        'annotations': _READ_ONLY_LOCAL,
        'handler': _tool_calculate_cache_economics,
    },
    {
        'name': 'run_alpha_evolve_generation',
        'title': 'Run Optimizer Simulator (one generation)',
        'description': (
            'SIMULATOR: runs one generation of the built-in optimizer simulator on a demo agent profile. '
            'It applies fixed improvement factors to synthetic parameters; it does not call AlphaEvolve or any '
            'model, read logs, or change any agent. Always tell the user the numbers are simulated.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'agent_id': {
                    'type': 'string',
                    'description': "Optional demo agent profile: 'it_service_desk', 'vibelift_analytics', or 'deep_research'.",
                },
                'platform_id': {
                    'type': 'string',
                    'description': "Optional simulated method: 'alpha_evolve', 'opus_critic', or 'hybrid_ensemble'.",
                },
            },
        },
        'annotations': _STATEFUL_LOCAL,
        'handler': _tool_run_alpha_evolve_generation,
    },
    {
        'name': 'get_vibelift_state',
        'title': 'Get Full VibeLift Runtime State',
        'description': 'Retrieves full runtime state, active agent profile, recorded turns, and telemetry.',
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {},
        },
        'annotations': _READ_ONLY_LOCAL,
        'handler': _tool_get_vibelift_state,
    },
    {
        'name': 'xray_prompt_cache',
        'title': 'Prompt Cache X-Ray',
        'description': (
            'Compares two snapshots of the same prompt (pasted text, or two logged turns by event id) and finds '
            'the exact character where the cacheable prefix breaks, the cache-buster on that line (timestamp, '
            'UUID, user e-mail, JSON key order, whitespace), the static tokens stranded behind it and their cost '
            'per 1,000 requests from the rate card, plus a reordered prompt whose cached share is re-measured.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'previous_prompt': {'type': 'string', 'description': 'Prompt text from turn N-1.'},
                'current_prompt': {'type': 'string', 'description': 'Prompt text from turn N.'},
                'previous_event_id': {
                    'type': 'string',
                    'description': 'Instead of text: event id of a logged turn from list_prompt_snapshot_turns.',
                },
                'current_event_id': {'type': 'string', 'description': 'Event id of the later logged turn.'},
                'model': {'type': 'string', 'description': "Rate card model (default 'gemini-2.5-flash')."},
                'monthly_requests': {
                    'type': 'integer',
                    'description': 'Optional request volume per month; without it no monthly figure is given.',
                },
                'current_input_tokens': {
                    'type': 'integer',
                    'description': 'Optional logged input token count of the current prompt, for grounded token figures.',
                },
            },
        },
        'annotations': _READ_ONLY_CLOUD,
        'handler': _tool_xray_prompt_cache,
    },
    {
        'name': 'list_prompt_snapshot_turns',
        'title': 'List Logged Turns for Prompt Cache X-Ray',
        'description': (
            'Lists recent OTel GenAI turns whose system instruction and input messages were logged to Cloud '
            'Storage, so xray_prompt_cache can compare two of them. Opt-in (VIBELIFT_PROMPT_XRAY_LIVE).'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': _READ_ONLY_CLOUD,
        'handler': _tool_list_prompt_snapshot_turns,
    },
    {
        'name': 'set_agent_trace_logging',
        'title': 'Enable or Disable Agent Trace Logging',
        'description': (
            'Enables or disables Discovery Engine trace logging (observabilityConfig: observabilityEnabled '
            'and sensitiveLoggingEnabled) on one Gemini Enterprise agent, or on all registered agents '
            '(optionally only No-Code / Low-Code agents). This changes live agent settings: only call it when '
            'the user explicitly asks to enable or disable trace logging, and pass resource_name for a single '
            'agent (omitting it updates every registered agent). Runs as the server service account, which '
            'needs discoveryengine.agents.update.'
        ),
        # Same visibility as the other tools the dashboard calls through the Gemini Enterprise host bridge
        # (the Enable / Disable trace buttons, after the admin confirms). It is not read-only, so Gemini
        # Enterprise asks the user to confirm before the assistant runs it.
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'enabled': {
                    'type': 'boolean',
                    'description': 'true to enable trace logging, false to disable it.',
                },
                'resource_name': {
                    'type': 'string',
                    'description': (
                        'Full agent resource name (projects/.../locations/.../collections/.../engines/.../'
                        'assistants/.../agents/...). Omit to update every registered agent.'
                    ),
                },
                'only_low_code': {
                    'type': 'boolean',
                    'description': 'With no resource_name, only update No-Code / Low-Code agents.',
                },
                'sensitive_logging': {
                    'type': 'boolean',
                    'description': 'Also log prompts and responses (defaults to the value of enabled).',
                },
                'engine_id': {
                    'type': 'string',
                    'description': "Optional 'engine_id' or 'location/engine_id' to limit a bulk update.",
                },
                'location': {'type': 'string', 'description': 'Optional Discovery Engine location.'},
            },
            'required': ['enabled'],
        },
        'annotations': {
            'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True,
        },
        'handler': _tool_set_agent_trace_logging,
    },
]

_TOOLS_BY_NAME = {t['name']: t for t in _TOOLS}


def _tool_descriptor(tool: dict[str, Any]) -> dict[str, Any]:
  """Constructs standard MCP tool descriptor with UI metadata."""
  meta = _build_ui_meta(
      visibility=tool.get('visibility') or ['model', 'app'],
      is_widget=bool(tool.get('widget')),
      preferred_mode='pip',
  )
  descriptor = {
      'name': tool['name'],
      'title': tool['title'],
      'description': tool['description'],
      'inputSchema': tool['inputSchema'],
      'icon': 'analytics',
      'icon_url': (
          'https://fonts.gstatic.com/s/i/short-term/release/'
          'googlesymbols/analytics/default/24px.svg'
      ),
      '_meta': meta,
      'meta': meta,
  }
  if tool.get('annotations'):
    descriptor['annotations'] = {'title': tool['title'], **tool['annotations']}
  return descriptor


# ---------------------------------------------------------------------------
# Routing Instruction for Gemini Enterprise
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
CRITICAL: Always call `open_dashboard` FIRST whenever the user asks to open, \
view, or inspect the VibeLift Analytics Platform, or asks for the Gemini Enterprise agent fleet, \
agent telemetry, FinOps prompt cache economics, or the optimizer simulator for their Google Cloud \
project. Do NOT skip `open_dashboard`. Calling `open_dashboard` opens the interactive dashboard \
(live agent fleet telemetry plus an optimizer simulator) directly in Gemini Enterprise's side panel. \
Tell the user the VibeLift dashboard is open and summarize the fleet numbers it returns. \
`run_alpha_evolve_generation` is a simulator: say its numbers are simulated whenever you report them.
"""


# ---------------------------------------------------------------------------
# JSON-RPC Dispatcher
# ---------------------------------------------------------------------------

class _RpcError(Exception):

  def __init__(self, code: int, message: str) -> None:
    super().__init__(message)
    self.code = code
    self.message = message


async def _dispatch(session_key: str, method: str, params: dict[str, Any]) -> Any:
  """Dispatches MCP JSON-RPC 2.0 requests."""
  if method == 'initialize':
    requested = str(params.get('protocolVersion') or '')
    version = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
    )
    return {
        'protocolVersion': version,
        'capabilities': {
            'tools': {'listChanged': False},
            'resources': {'subscribe': False, 'listChanged': False},
        },
        'serverInfo': {
            'name': 'vibelift-analytics-mcp',
            'title': 'VibeLift — Agent Analytics & FinOps Platform',
            'version': '1.0.0',
            'icon': 'analytics',
            'icon_url': (
                'https://fonts.gstatic.com/s/i/short-term/release/'
                'googlesymbols/analytics/default/24px.svg'
            ),
        },
        'instructions': _SYSTEM_INSTRUCTION,
    }

  if method in ('ping', 'notifications/initialized', 'ui/notifications/initialized'):
    return {}

  if method in ('ui/initialize', 'ui/request-display-mode'):
    return {'protocolVersion': MCP_PROTOCOL_VERSION, 'capabilities': {}}

  if method == 'tools/list':
    return {'tools': [_tool_descriptor(t) for t in _TOOLS]}

  if method == 'resources/list':
    return {
        'resources': [
            {
                'uri': WIDGET_URI,
                'name': 'VibeLift Analytics Dashboard',
                'description': (
                    'Live Gemini Enterprise agent fleet telemetry, prompt cache economics, '
                    'and an optimizer simulator (synthetic numbers).'
                ),
                'mimeType': WIDGET_MIME,
                '_meta': RESOURCE_META,
            }
        ]
    }

  if method == 'resources/read':
    raw_uri = str(params.get('uri') or '')
    base_uri = raw_uri.split('#', 1)[0].split('?', 1)[0]
    if base_uri not in (
        WIDGET_URI,
        f'{WIDGET_URI}.html',
        'ui://vibelift-analytics/dashboard',
        'ui://vibelift-analytics/dashboard.html',
        'ui://vibelift/dashboard',
        'ui://vibelift/dashboard.html',
    ):
      raise _RpcError(INVALID_PARAMS, f'Unknown resource URI: {raw_uri!r}')

    return {
        'contents': [
            {
                'uri': raw_uri or WIDGET_URI,
                'mimeType': WIDGET_MIME,
                'text': _widget_html(),
                '_meta': RESOURCE_META,
            }
        ]
    }

  if method == 'tools/call':
    name = str(params.get('name') or '')
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None and '__' in name:
      tool = _TOOLS_BY_NAME.get(name.rsplit('__', 1)[-1])
    if tool is None:
      raise _RpcError(INVALID_PARAMS, f'Unknown tool: {name!r}')

    tool_args = params.get('arguments')
    if not isinstance(tool_args, dict):
      tool_args = {}

    effective_key = str(tool_args.pop('session_id', '')).strip() or session_key
    try:
      result = await tool['handler'](effective_key, tool_args)
    except Exception as err:
      ref = _error_ref()
      logger.exception('MCP tool %s failed [ref=%s]: %s', tool['name'], ref, err)
      return {
          'content': [{
              'type': 'text',
              'text': (
                  f'Tool {name} failed ({type(err).__name__}, ref {ref}). '
                  'Details are in the VibeLift server logs.'
              ),
          }],
          'isError': True,
      }

    result.setdefault('isError', False)
    if tool.get('widget'):
      widget_meta = _build_ui_meta(is_widget=True, preferred_mode='pip')
      result.setdefault('_meta', widget_meta)
      result.setdefault('meta', widget_meta)
    return result

  raise _RpcError(METHOD_NOT_FOUND, f'Unknown method: {method!r}')


# ---------------------------------------------------------------------------
# ASGI Endpoint Registration for Starlette / FastAPI
# ---------------------------------------------------------------------------

def register_mcp_routes(app: Any) -> None:
  """Registers the `/mcp` streamable HTTP JSON-RPC endpoint on FastAPI or Starlette."""

  async def mcp_endpoint(request):
    if request.method == 'DELETE':
      return Response(status_code=204)

    if request.method == 'GET':
      return Response(status_code=405, headers={'Allow': 'POST, DELETE'})

    try:
      body = await request.json()
    except Exception:
      return JSONResponse(
          {
              'jsonrpc': '2.0',
              'id': None,
              'error': {'code': PARSE_ERROR, 'message': 'Invalid JSON'},
          },
          status_code=400,
      )

    session_key = (
        request.headers.get('mcp-session-id')
        or request.headers.get('Mcp-Session-Id')
        or ''
    )

    batch = body if isinstance(body, list) else [body]
    responses = []
    new_session_id = None

    for item in batch:
      if not isinstance(item, dict) or item.get('jsonrpc') != '2.0':
        responses.append({
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': INVALID_REQUEST,
                'message': 'Expected a JSON-RPC 2.0 request object',
            },
        })
        continue

      method = str(item.get('method') or '')
      req_id = item.get('id')
      params = item.get('params')
      if not isinstance(params, dict):
        params = {}

      if method == 'initialize' and not session_key:
        new_session_id = uuid.uuid4().hex
        session_key = new_session_id

      if req_id is None:
        continue

      try:
        result = await _dispatch(session_key, method, params)
        responses.append({'jsonrpc': '2.0', 'id': req_id, 'result': result})
      except _RpcError as err:
        responses.append({
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {'code': err.code, 'message': err.message},
        })
      except Exception:
        ref = _error_ref()
        logger.exception('MCP method %s execution failed [ref=%s]', method, ref)
        responses.append({
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {'code': INTERNAL_ERROR, 'message': f'Internal error (ref {ref}); see server logs.'},
        })

    headers = {}
    if new_session_id:
      headers['Mcp-Session-Id'] = new_session_id

    if not responses:
      return Response(status_code=202, headers=headers)

    payload = responses if isinstance(body, list) else responses[0]
    return Response(
        content=json.dumps(payload),
        media_type='application/json',
        headers=headers,
    )

  if not mcp_app_enabled():
    logger.warning('ENABLE_MCP_APP is off: the /mcp endpoint is not registered')
    return
  app.add_route('/mcp', mcp_endpoint, methods=['POST', 'GET', 'DELETE'])
  logger.info('VibeLift MCP endpoint successfully registered at /mcp')


def handle_jsonrpc_sync(
    body: Any,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], str]:
  """Synchronous entry point for JSON-RPC 2.0 processing in standard HTTP handlers.

  Returns: (status_code, headers_dict, response_body_str)
  """
  session_key = session_id or ''
  batch = body if isinstance(body, list) else [body]
  responses = []
  new_session_id = None

  for item in batch:
    if not isinstance(item, dict) or item.get('jsonrpc') != '2.0':
      responses.append({
          'jsonrpc': '2.0',
          'id': None,
          'error': {
              'code': INVALID_REQUEST,
              'message': 'Expected a JSON-RPC 2.0 request object',
          },
      })
      continue

    method = str(item.get('method') or '')
    req_id = item.get('id')
    params = item.get('params')
    if not isinstance(params, dict):
      params = {}

    if method == 'initialize' and not session_key:
      new_session_id = uuid.uuid4().hex
      session_key = new_session_id

    if req_id is None:
      continue

    try:
      result = asyncio.run(_dispatch(session_key, method, params))
      responses.append({'jsonrpc': '2.0', 'id': req_id, 'result': result})
    except _RpcError as err:
      responses.append({
          'jsonrpc': '2.0',
          'id': req_id,
          'error': {'code': err.code, 'message': err.message},
      })
    except Exception:
      ref = _error_ref()
      logger.exception('MCP method %s execution failed [ref=%s]', method, ref)
      responses.append({
          'jsonrpc': '2.0',
          'id': req_id,
          'error': {'code': INTERNAL_ERROR, 'message': f'Internal error (ref {ref}); see server logs.'},
      })

  headers = {'Content-Type': 'application/json; charset=utf-8'}
  if new_session_id:
    headers['Mcp-Session-Id'] = new_session_id

  if not responses:
    return (202, headers, '')

  payload = responses if isinstance(body, list) else responses[0]
  return (200, headers, json.dumps(payload))
