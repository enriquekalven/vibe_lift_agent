"""HTTP server, FastAPI app, and REST API for VibeLift Analytics Platform on Google Cloud."""

from collections.abc import Callable, Mapping, Sequence
import base64
import http.server
import json
import logging
import os
import socket
import sys
import urllib.parse

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

import alpha_evolve_optimizer
import logo_asset
import long_running_agent
import telemetry
import ui_template

import gcp_telemetry
import ge_fleet
import mcp_server

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


import threading


class VibeLiftRuntimeController:
  """Coordinates the selectable Gemini Enterprise agents, AlphaEvolve optimizer, GCP telemetry, and GE fleet."""

  def __init__(self, fleet_service: ge_fleet.GeminiEnterpriseFleetService | None = None) -> None:
    """Initializes the optimizer, long-running agent, GCP telemetry client, and fleet service."""
    self._lock = threading.Lock()
    self._warmer_started = False
    self.optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    self.agent = long_running_agent.LongRunningVibeLiftAgent(
        self.optimizer,
        agent_name=self.optimizer.active_agent.agent_id,
        model=self.optimizer.active_agent.model,
    )
    self.gcp_telemetry = gcp_telemetry.GoogleCloudTelemetryService()
    self.ge_fleet = fleet_service or ge_fleet.get_ge_fleet_service()

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
      with self._lock:
        self.optimizer.sync_from_ge_fleet(fleet)
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

    with self._lock:
      turns = list(self.agent.turns)
      active_agent_dict = self.optimizer.active_agent.to_dict()
      available_agents = self.optimizer.list_agents_summary()
      all_agents = self.optimizer.get_all_agents_dict()
      optimizer_platforms = self.optimizer.get_optimizer_platforms_payload()
      steps = list(self.agent.step_descriptions)

    services = self.gcp_telemetry.list_cloud_run_agent_services(non_blocking=fast_mcp)
    support_events = self.gcp_telemetry.fetch_gemini_enterprise_support_telemetry(
        limit=6, non_blocking=fast_mcp
    )

    payload: dict[str, object] = {
        'agent_name': self.agent.agent_name,
        'model': self.agent.model,
        'gcp_project': self.gcp_telemetry.project_id,
        'gcp_region': self.gcp_telemetry.region,
        'gcp_services': services,
        'gemini_enterprise_support_events': support_events,
        'active_agent': active_agent_dict,
        'available_agents': available_agents,
        'all_agents': all_agents,
        'optimizer_platforms': optimizer_platforms,
        'user_centric': alpha_evolve_optimizer.build_user_centric_analytics(),
        'decorator_events': telemetry.get_recent_decorator_events(),
        'summary': dict(telemetry.summarize_log_stream(turns)),
        'turns': [t.to_dict() for t in turns],
        'steps': steps,
    }
    if include_fleet and fleet_payload is not None:
      payload['ge_fleet'] = fleet_payload
    return payload

  def sync_gcp_telemetry(self) -> dict[str, object]:
    """Fetches live Cloud Logging turns from GCP and ingests them into the active runtime."""
    live_turns = self.gcp_telemetry.fetch_live_cloud_turns(hours_ago=48, max_results=15)
    with self._lock:
      if live_turns:
        self.agent.ingest_gcp_cloud_turns(live_turns)
    return self.get_state_payload(include_fleet=False)

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
    event = telemetry.DecoratorTelemetryEvent(
        timestamp=str(raw.get('timestamp') or time.strftime('%H:%M:%S UTC', time.gmtime())),
        agent_name=str(raw.get('agent_name') or active.agent_id),
        handler_name=str(raw.get('handler_name') or 'on_message_passing_turn'),
        protocol=str(raw.get('protocol') or 'ADK / MCP Decorator Stream'),
        model=str(raw.get('model') or active.model),
        latency_ms=float(raw.get('latency_ms') or 585.0),
        prompt_tokens=int(raw.get('prompt_tokens') or 19200),
        cached_tokens=int(raw.get('cached_tokens') or 17680),
        output_tokens=int(raw.get('output_tokens') or 320),
        context_bloat_pct=float(raw.get('context_bloat_pct') or 12.4),
        idle_ratio_pct=float(raw.get('idle_ratio_pct') or 6.8),
        skill_or_mcp=str(raw.get('skill_or_mcp') or f'mcp://{active.agent_id}/stream'),
        user_cohort=str(raw.get('user_cohort') or 'Enterprise Active DAU Cohort'),
        status=str(raw.get('status') or '200 OK (@vibelift_telemetry)'),
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
      self._send_json(srv.controller.get_state_payload())
      return
    if path == '/api/gcp_telemetry':
      self._send_json(srv.controller.gcp_telemetry.get_telemetry_summary_payload())
      return
    self.send_error(404, 'Not Found')

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    """Serves POST routes for agent selection, custom parameters, GCP sync, and AlphaEvolve."""
    srv = self.server
    assert isinstance(srv, VibeLiftHttpServer)
    body = self._read_json_body()
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
      self._send_json({'status': 'synced', 'state': srv.controller.sync_gcp_telemetry()})
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

  @app.get('/api/gcp_telemetry')
  def get_gcp_telemetry_endpoint():
    return controller.gcp_telemetry.get_telemetry_summary_payload()

  @app.post('/api/sync_gcp_telemetry')
  def sync_gcp_telemetry_endpoint():
    return {'status': 'synced', 'state': controller.sync_gcp_telemetry()}

  @app.post('/api/select_agent')
  def post_select_agent(payload: dict = fastapi.Body(default={})):
    return controller.select_agent(str(payload.get('agent_id', 'it_service_desk')))

  @app.post('/api/select_optimizer')
  def post_select_optimizer(payload: dict = fastapi.Body(default={})):
    return controller.select_optimizer(str(payload.get('platform_id', 'alpha_evolve')))

  @app.post('/api/decorator_ingest')
  def post_decorator_ingest(payload: dict = fastapi.Body(default={})):
    return controller.ingest_decorator_event(payload)

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