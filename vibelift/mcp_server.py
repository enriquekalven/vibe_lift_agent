"""Google Cloud Streamable HTTP Model Context Protocol (MCP) Server for VibeLift.

Enables Gemini Enterprise and AI clients to connect to VibeLift as a BYO MCP App:
- Serves the streamable HTTP JSON-RPC 2.0 endpoint at `/mcp`
- Publishes interactive MCP UI resource `ui://vibelift-analytics/dashboard`
- Implements MCP Tools: `open_dashboard`, `query_ge_agent_fleet`, `query_project_telemetry`,
  `calculate_prompt_cache_economics`, `run_alpha_evolve_generation`, and `get_vibelift_state`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
import logging
import os
import sys
import uuid
from typing import Any

from starlette.responses import JSONResponse, Response

from vibelift import gcp_telemetry
from vibelift import fleet as ge_fleet
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
  """ENABLE_MCP_APP=0/false/no/off disables the /mcp endpoint; it is enabled by default."""
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
  fleet = await asyncio.to_thread(
      ctrl.get_fleet_payload,
      ge_fleet.parse_window_hours(args.get('window_hours')),
      ge_fleet.parse_bool(args.get('force_refresh')),
      True,
      0.45,
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
  """Calculates prompt cache hit ratio and dollar savings against Google Cloud rate cards."""
  model = str(args.get('model') or 'gemini-2.5-flash')
  prompt_tok = int(args.get('prompt_token_count') or 10000)
  cached_tok = int(args.get('cached_content_token_count') or 0)
  output_tok = int(args.get('output_token_count') or 500)

  card = telemetry.RATE_CARDS.get(model, telemetry.RATE_CARDS['gemini-2.5-flash'])
  uncached_tok = max(0, prompt_tok - cached_tok)

  log_entry = telemetry.TurnUsageLog(
      timestamp='2026-09-25T15:00:00Z',
      agent_name='mcp-eval-client',
      model=model,
      turn_index=1,
      prompt_prefix_hash='eval_calc',
      cache_breakpoint_line=None if cached_tok > 0 else 1,
      cache_breakpoint_reason='Evaluated via MCP tool',
      prompt_token_count=prompt_tok,
      cached_content_token_count=cached_tok,
      cache_creation_input_tokens=0,
      uncached_input_tokens=uncached_tok,
      candidates_token_count=output_tok,
      thoughts_token_count=100,
      status_code=200,
      tool_called='mcp.calculate_cache_economics',
      evolution_generation=14,
  )
  naive_usd, actual_usd, saved_usd = log_entry.compute_costs()
  result_payload = {
      'model': model,
      'prompt_tokens': prompt_tok,
      'cached_tokens': cached_tok,
      'uncached_tokens': uncached_tok,
      'output_tokens': output_tok,
      'cache_hit_ratio_pct': log_entry.cache_hit_ratio,
      'naive_cost_usd': naive_usd,
      'actual_cost_usd': actual_usd,
      'net_savings_usd': saved_usd,
  }
  return {
      'content': [{'type': 'text', 'text': json.dumps(result_payload, indent=2)}],
      'structuredContent': result_payload,
  }


async def _tool_run_alpha_evolve_generation(session_key: str, args: dict[str, Any]) -> dict[str, Any]:
  """Executes an optimization cycle on the active agent."""
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
      f"Optimization Generation {latest_gen.get('generation', 13)} complete for `{active.get('display_name') or active.get('agent_id')}`.\n"
      f"- Status: {active.get('health_status')}\n"
      f"- Action: {latest_action.get('action_title')}\n"
      f"- Impact: {latest_action.get('impact_summary')}\n"
      f"- Monthly Savings: ${active.get('monthly_savings_usd', 0):,}/mo"
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
            'agent fleet, agent telemetry, prompt cache economics, user-centric FinOps, or AlphaEvolve '
            'optimization in the interactive UI. Opens the interactive dashboard in the right side panel '
            '(with a Fullscreen button): every agent deployed on the Gemini Enterprise app with live Cloud '
            'Monitoring and Cloud Logging telemetry, plus the optimization studio and @vibelift_telemetry stream.'
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
                    'description': 'Telemetry window for the live fleet in hours (1-720, default 24).',
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
        'title': 'Query Gemini Enterprise Agent Fleet',
        'description': (
            'Lists every agent deployed on the Gemini Enterprise app (ADK agents on Vertex AI Agent Engine, '
            'A2A agents, and Google-managed agents) with live telemetry aggregated from Cloud Monitoring and '
            'Cloud Logging: requests, 4xx/5xx errors, p50/p95 latency, LLM calls, input/output tokens, '
            'conversations, and last activity, plus project-wide Vertex AI model token usage and estimated cost.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'window_hours': {
                    'type': 'integer',
                    'description': 'Telemetry window in hours (1-720, default 24; e.g. 1, 6, 24, 168).',
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
            'Calculates prompt cache hit ratio and net dollar savings vs naive rate cards for Google Cloud Gemini models '
            '(gemini-2.5-flash, gemini-2.5-pro, gemini-1.5-flash, gemini-1.5-pro, gemini-3.1-flash).'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'prompt_token_count': {
                    'type': 'integer',
                    'description': 'Total prompt token count.',
                },
                'cached_content_token_count': {
                    'type': 'integer',
                    'description': 'Number of tokens read from Gemini prompt cache.',
                },
                'output_token_count': {
                    'type': 'integer',
                    'description': 'Number of generated candidate tokens (defaults to 500).',
                },
                'model': {
                    'type': 'string',
                    'description': "Gemini model identifier (e.g. 'gemini-2.5-flash').",
                },
            },
            'required': ['prompt_token_count', 'cached_content_token_count'],
        },
        'annotations': _READ_ONLY_LOCAL,
        'handler': _tool_calculate_cache_economics,
    },
    {
        'name': 'run_alpha_evolve_generation',
        'title': 'Run AlphaEvolve Optimization Cycle',
        'description': (
            'Executes the next AlphaEvolve evolutionary optimization cycle on prompt prefixes, '
            'schema pruning, and multi-objective performance parameters for the active agent.'
        ),
        'visibility': ['model', 'app'],
        'inputSchema': {
            'type': 'object',
            'properties': {
                'agent_id': {
                    'type': 'string',
                    'description': "Optional Gemini Enterprise agent identifier: 'it_service_desk', 'vibelift_analytics', or 'deep_research'.",
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
agent telemetry, FinOps prompt cache economics, or AlphaEvolve optimization for their Google Cloud \
project. Do NOT skip `open_dashboard`. Calling `open_dashboard` opens the interactive dashboard \
(live agent fleet telemetry and AlphaEvolve studio) directly in Gemini Enterprise's side panel. \
Tell the user the VibeLift dashboard is open and summarize the fleet numbers it returns.
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
                    'and the AlphaEvolve optimization studio.'
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
