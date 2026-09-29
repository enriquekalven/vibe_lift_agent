"""Gemini Enterprise agent fleet: live inventory joined with real telemetry.

Enumerates every agent deployed on the configured Gemini Enterprise app(s) through the
Discovery Engine API and joins each agent with telemetry read from Google Cloud:

* ADK agents on Vertex AI Agent Engine: Cloud Monitoring ``reasoning_engine`` metrics
  (requests, non-2xx responses, latency percentiles, vCPU / memory allocation) plus the
  OpenTelemetry GenAI events the agent writes to Cloud Logging (per-agent input/output
  tokens, LLM calls, conversations, last activity).
* A2A agents served from Cloud Run: Cloud Monitoring ``run.googleapis.com`` metrics for the
  service named in the agent card URL (service-level: includes all traffic to that service).
* Google-managed and no-code agents: inventory only (no per-project runtime telemetry).

Project-wide model token usage comes from the Vertex AI publisher ``token_count`` and
``model_invocation_count`` metrics. Nothing is synthesized: when a source is unavailable the
value is ``None`` and the failure is reported in ``errors`` / ``source_status``.
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

try:
  import google.auth as google_auth
  from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover - google-auth ships with the Cloud client libraries.
  google_auth = None
  GoogleAuthRequest = None

import gcp_telemetry
import telemetry

logger = logging.getLogger(__name__)

_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
_MONITORING = 'https://monitoring.googleapis.com/v3'
_TRACE = 'https://cloudtrace.googleapis.com/v1'
_LOGGING = 'https://logging.googleapis.com/v2'
_AIPLATFORM = 'https://{location}-aiplatform.googleapis.com/v1'
_HTTP_TIMEOUT_S = 25.0
_MAX_PAGES = 10

DEFAULT_ENGINE_IDS = ('agent-platform-demo', 'us/gemini-enterprise-17649552_1764955289529')
# 'auto' discovers every Gemini Enterprise app in these locations.
DEFAULT_DISCOVERY_LOCATIONS = ('global', 'us', 'eu')
ALLOWED_WINDOWS_HOURS = (1, 6, 24, 168)

AGENT_TYPE_LABELS = {
    'ADK': 'ADK agent on Vertex AI Agent Engine',
    'A2A': 'A2A agent',
    'MANAGED': 'Google-managed agent',
    'LOW_CODE': 'No-code agent (Agent Designer)',
    'DIALOGFLOW': 'Dialogflow agent',
    'UNKNOWN': 'Agent',
}

_REASONING_ENGINE_RE = re.compile(
    r'^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/reasoningEngines/(?P<id>[^/]+)$'
)
# <service>-<project-number>.<region>.run.app
_RUN_NUMBERED_HOST_RE = re.compile(
    r'^(?P<service>[a-z0-9-]+)-(?P<project>\d+)\.(?P<region>[a-z0-9-]+)\.run\.app$'
)
# <service>-<10 char hash>-<region code>.a.run.app
_RUN_HASHED_HOST_RE = re.compile(r'^(?P<service>[a-z0-9-]+?)-[a-z0-9]{10}-[a-z]{2,4}\.a\.run\.app$')


class FleetSourceError(Exception):
  """Raised when a Google Cloud API call fails."""

  def __init__(self, status: int, message: str):
    super().__init__(f'HTTP {status}: {message}' if status else message)
    self.status = status


def _utcnow() -> datetime.datetime:
  return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(dt: datetime.datetime) -> str:
  return dt.isoformat().replace('+00:00', 'Z')


def _to_int(value: Any) -> int:
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return 0


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
  raw = os.environ.get(name, '')
  values = [v.strip() for v in raw.split(',') if v.strip()]
  return values or list(default)


def _clamp_window(hours: Any, default: int) -> int:
  try:
    value = int(hours)
  except (TypeError, ValueError):
    return default
  return max(1, min(value, 720))


def parse_window_hours(value: Any) -> int | None:
  """Validates an untrusted window value: an integer number of hours in [1, 720], else None."""
  if value is None or isinstance(value, bool):
    return None
  try:
    hours = int(str(value).strip())
  except (TypeError, ValueError):
    return None
  return hours if 1 <= hours <= 720 else None


# Up to this window the trend bins raw per-minute points itself (exact); beyond it, server-side alignment.
RAW_SERIES_MAX_WINDOW_S = 7 * 24 * 3600


def trend_bucket_seconds(hours: int) -> int:
  """Bucket size for the requests-over-time chart (about 24 buckets per window)."""
  if hours <= 1:
    return 300
  if hours <= 6:
    return 900
  if hours <= 24:
    return 3600
  if hours <= 72:
    return 3 * 3600
  return 6 * 3600


def build_request_trend(
    agents: list[dict[str, Any]],
    results: dict[tuple[str, ...], Any],
    window_s: int,
    bucket_s: int,
    default_project: str | None = None,
) -> dict[str, Any]:
  """Aligns per-runtime request series onto one bucket grid, keyed like runtime_backend_key()."""
  now = int(_utcnow().timestamp())
  start = now - window_s
  last = now - (now % bucket_s) + bucket_s
  # Enough buckets that the first one starts at or before the window start.
  n = max(1, -(-(last - start) // bucket_s))
  grid = [last - bucket_s * (n - 1 - i) for i in range(n)]
  index = {t: i for i, t in enumerate(grid)}
  by_runtime: dict[str, dict[str, list[int]]] = {}
  ok = False
  for agent in agents:
    backend = agent.get('backend') or {}
    kind = backend.get('kind')
    project = backend.get('project') or default_project
    if kind == 'cloud_run':
      series_map = results.get(('run_series', project))
      rid = backend.get('service')
    elif kind == 'agent_engine':
      series_map = results.get(('re_series', project))
      rid = backend.get('reasoning_engine_id')
    else:
      continue
    if series_map is None or not rid:
      continue
    ok = True
    key = runtime_backend_key(agent)
    if key in by_runtime or rid not in series_map:
      continue
    row = {'requests': [0] * n, 'errors_4xx': [0] * n, 'errors_5xx': [0] * n}
    for epoch, vals in series_map[rid].items():
      if epoch <= start:
        continue
      # Snap each point's end time up to the bucket end that contains it.
      snapped = epoch + ((bucket_s - epoch % bucket_s) % bucket_s)
      i = index.get(snapped)
      if i is None:
        continue
      for k in row:
        row[k][i] += int(vals.get(k) or 0)
    by_runtime[key] = row
  if not ok and not any(k[0] in ('run_series', 're_series') for k in results):
    status = 'not_applicable'
  else:
    status = 'ok' if all(results.get(k) is not None for k in results if k[0] in ('run_series', 're_series')) else 'partial'
  return {
      'bucket_seconds': bucket_s,
      'bucket_ends': [_iso(datetime.datetime.fromtimestamp(t, datetime.timezone.utc)) for t in grid],
      'by_runtime': by_runtime,
      'status': status,
      'source': 'Cloud Monitoring request_count (Cloud Run + Agent Engine)',
  }


def parse_bool(value: Any) -> bool:
  """Parses an untrusted boolean flag ('1', 'true', 'yes', 'on' or a real bool)."""
  if isinstance(value, bool):
    return value
  return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def runtime_backend_key(agent: dict[str, Any]) -> str:
  """Returns a key identifying the runtime serving an agent (shared runtimes share a key)."""
  backend = agent.get('backend') or {}
  kind = str(backend.get('kind') or 'unknown')
  if kind == 'cloud_run' and backend.get('service'):
    # Metrics are queried per service name in the monitored project, and the same
    # service can be referenced by both its hashed and numbered run.app URLs.
    return f"cloud_run:{backend['service']}"
  if kind == 'agent_engine' and backend.get('resource'):
    return f"agent_engine:{backend['resource']}"
  if backend.get('resource') or backend.get('url'):
    return f"{kind}:{backend.get('resource') or backend.get('url')}"
  return f"agent:{agent.get('resource_name') or agent.get('agent_id')}"


_TRACE_ENGINE_RE = re.compile(r'reasoningEngines/(\d+)')


def _span_engine_id(labels: dict[str, Any]) -> str | None:
  for key in ('cloud.resource_id', 'cloud.resource.id', 'g.co/r/aiplatform.googleapis.com/ReasoningEngine/reasoning_engine_id'):
    match = _TRACE_ENGINE_RE.search(str(labels.get(key) or ''))
    if match:
      return match.group(1)
  service = str(labels.get('service.name') or '')
  return service if service.isdigit() else None


def aggregate_trace_usage(traces: list[dict[str, Any]], engine_ids: list[str]) -> dict[str, dict[str, Any]]:
  """Sums gen_ai token usage per Agent Engine from Cloud Trace spans, counting each model call once.

  ADK's call_llm span and the GenAI SDK's generate_content child span both carry the same usage
  attributes; only the innermost span with usage is counted.
  """
  stats: dict[str, dict[str, Any]] = {
      i: {'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'last_activity': None,
          '_conversations': set(), '_models': collections.Counter()}
      for i in engine_ids
  }
  for trace in traces:
    spans = trace.get('spans') or []
    trace_engine = next((e for e in (_span_engine_id(sp.get('labels') or {}) for sp in spans) if e), None)
    usage = [sp for sp in spans if any(k in (sp.get('labels') or {}) for k in ('gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens'))]
    wrappers = {sp.get('parentSpanId') for sp in usage}
    for sp in usage:
      if sp.get('spanId') in wrappers:
        continue  # A child span reports the same call.
      labels = sp.get('labels') or {}
      engine = _span_engine_id(labels) or trace_engine
      if engine not in stats:
        continue
      st = stats[engine]
      st['llm_calls'] += 1
      st['input_tokens'] += _to_int(labels.get('gen_ai.usage.input_tokens'))
      st['output_tokens'] += _to_int(labels.get('gen_ai.usage.output_tokens'))
      for key, value in labels.items():
        if key.startswith('gen_ai.usage.') and 'cache' in key:
          st['cached_tokens'] += _to_int(value)
      conv = labels.get('gen_ai.conversation.id') or labels.get('gcp.vertex.agent.session_id')
      if conv:
        st['_conversations'].add(conv)
      model = labels.get('gen_ai.response.model') or labels.get('gen_ai.request.model')
      if model:
        st['_models'][model] += 1
      stamp = sp.get('endTime')
      if stamp and (st['last_activity'] is None or stamp > st['last_activity']):
        st['last_activity'] = stamp
  for st in stats.values():
    st['conversations'] = len(st.pop('_conversations'))
    st['models'] = [m for m, _ in st.pop('_models').most_common(3)]
  return stats


_GE_RESOURCE_RE = re.compile(r'locations/([^/]+)/collections/[^/]+/engines/([^/]+)(?:/assistants/[^/]+/agents/([^/]+))?')


def aggregate_ge_assistant_usage(traces: list[dict[str, Any]]) -> dict[str, Any]:
  """Sums gen_ai token usage of Gemini Enterprise's own assistant, per GE app and assistant agent.

  Gemini Enterprise exports generate_content spans (cloud.platform=gcp.gemini_enterprise) whose
  cloud.resource.id names the app (engine) and the assistant agent (e.g. core_assistant).
  """
  by_engine: dict[str, dict[str, Any]] = {}
  for trace in traces:
    spans = trace.get('spans') or []
    usage = [sp for sp in spans if any(k in (sp.get('labels') or {}) for k in ('gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens'))]
    wrappers = {sp.get('parentSpanId') for sp in usage}
    for sp in usage:
      if sp.get('spanId') in wrappers:
        continue
      labels = sp.get('labels') or {}
      if labels.get('cloud.platform') != 'gcp.gemini_enterprise':
        continue
      match = _GE_RESOURCE_RE.search(str(labels.get('cloud.resource.id') or labels.get('cloud.resource_id') or ''))
      if not match:
        continue
      key = f'{match.group(1)}/{match.group(2)}'
      st = by_engine.setdefault(key, {
          'engine_key': key, 'location': match.group(1), 'engine_id': match.group(2),
          'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0,
          'last_activity': None, '_conversations': set(), '_models': collections.Counter(),
          '_agents': collections.Counter()})
      st['llm_calls'] += 1
      st['input_tokens'] += _to_int(labels.get('gen_ai.usage.input_tokens'))
      st['output_tokens'] += _to_int(labels.get('gen_ai.usage.output_tokens'))
      for k, value in labels.items():
        if k.startswith('gen_ai.usage.') and 'cache' in k:
          st['cached_tokens'] += _to_int(value)
      if labels.get('gen_ai.conversation.id'):
        st['_conversations'].add(labels['gen_ai.conversation.id'])
      model = labels.get('gen_ai.response.model') or labels.get('gen_ai.request.model')
      if model:
        st['_models'][model] += 1
      st['_agents'][match.group(3) or 'assistant'] += 1
      stamp = sp.get('endTime')
      if stamp and (st['last_activity'] is None or stamp > st['last_activity']):
        st['last_activity'] = stamp
  totals = {'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'conversations': 0}
  for st in by_engine.values():
    st['conversations'] = len(st.pop('_conversations'))
    st['models'] = dict(st.pop('_models'))
    st['assistant_agents'] = dict(st.pop('_agents'))
    for k in totals:
      totals[k] += st[k]
  return {'by_engine': by_engine, 'totals': totals}


_RESOURCE_LOCATION_RE = re.compile(r'locations/([^/]+)/')
_RESOURCE_PROJECT_RE = re.compile(r'^projects/([^/]+)/')


def _registration_action(agent: dict[str, Any]) -> dict[str, str]:
  """Cleanup steps for a GE registration whose backend is gone (never executed automatically)."""
  name = str(agent.get('resource_name') or '')
  loc_match = _RESOURCE_LOCATION_RE.search(name)
  proj_match = _RESOURCE_PROJECT_RE.match(name)
  base = _de_base(loc_match.group(1) if loc_match else 'global')
  project = proj_match.group(1) if proj_match else ''
  return {
      'summary': ('Remove this agent from Gemini Enterprise, or redeploy its backend and update the '
                  'registration to point at the new one.'),
      'delete_command': (
          'curl -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" '
          f'-H "X-Goog-User-Project: {project}" "{base}/{name}"'
      ) if name else '',
  }


def assess_registration(
    agent: dict[str, Any],
    results: dict[tuple[str, ...], Any],
    checked_at: str,
    default_project: str | None = None,
) -> dict[str, Any]:
  """Classifies whether a GE agent's backend still exists, using only API responses as evidence.

  Statuses: OK, BACKEND_NOT_FOUND (API returned 404 / service absent from the project's list),
  NO_BACKEND (ADK registration without an Agent Engine reference), UNVERIFIED (lookup failed or the
  backend's project is unknown) and NOT_CHECKED (agent types hosted by Gemini Enterprise itself).
  """
  backend = agent.get('backend') or {}
  kind = backend.get('kind')
  out: dict[str, Any] = {'status': 'NOT_CHECKED', 'evidence': None, 'checked_at': checked_at}
  if agent.get('type') == 'ADK':
    resource = backend.get('resource')
    if not resource:
      out.update(status='NO_BACKEND', evidence='The Gemini Enterprise registration has no Agent Engine (reasoningEngine) reference.')
    else:
      meta = results.get(('re_meta', resource))
      url = f"{_AIPLATFORM.format(location=backend.get('location') or 'us-central1')}/{resource}"
      if meta is None:
        out.update(status='UNVERIFIED', evidence=f'Agent Engine lookup failed (not a 404): GET {url}')
      elif meta.get('exists') is False:
        out.update(status='BACKEND_NOT_FOUND', evidence=f"GET {url} returned HTTP {meta.get('http_status', 404)} (Agent Engine does not exist).")
      else:
        out.update(status='OK', evidence=f'GET {url} returned the Agent Engine.')
  elif kind == 'cloud_run' and backend.get('service'):
    service = backend['service']
    explicit_project = backend.get('project')
    project = explicit_project or default_project
    services = results.get(('run_inventory', project)) if project else None
    if services is None:
      out.update(status='UNVERIFIED', evidence=f'Could not list Cloud Run services in project {project}.')
    elif service in services:
      out.update(status='OK', evidence=f"Cloud Run service '{service}' exists in project {project}.")
    elif not explicit_project:
      # The hashed run.app URL does not say which project hosts the service; it may live elsewhere.
      out.update(status='UNVERIFIED', evidence=(
          f"Cloud Run service '{service}' is not in project {project}, and its run.app URL does not "
          'encode the hosting project.'))
    else:
      out.update(status='BACKEND_NOT_FOUND', evidence=(
          f"Cloud Run service '{service}' is not in project {project} "
          f'(services.list across all regions returned {len(services)} services).'))
  if out['status'] in ('BACKEND_NOT_FOUND', 'NO_BACKEND'):
    out['action'] = _registration_action(agent)
  return out


def cloud_run_service_from_url(url: str) -> dict[str, str] | None:
  """Derives the Cloud Run service (and project/region when encoded) from a run.app URL."""
  host = (urllib.parse.urlparse(url).hostname or '').lower()
  match = _RUN_NUMBERED_HOST_RE.match(host)
  if match:
    return {'service': match['service'], 'project': match['project'], 'region': match['region']}
  match = _RUN_HASHED_HOST_RE.match(host)
  if match:
    return {'service': match['service']}
  return None


def _parse_engine_spec(spec: str, default_location: str = 'global') -> tuple[str, str]:
  """Parses '<location>/<engine_id>' or '<engine_id>' into (location, engine_id)."""
  cleaned = str(spec or '').strip()
  if '/' in cleaned:
    loc, eid = cleaned.split('/', 1)
    return (loc.strip() or default_location, eid.strip())
  return (default_location, cleaned)


def _de_base(location: str = 'global') -> str:
  """Returns the regional or global Discovery Engine v1alpha base URL."""
  loc = (location or 'global').strip().lower()
  if loc == 'global':
    return 'https://discoveryengine.googleapis.com/v1alpha'
  return f'https://{loc}-discoveryengine.googleapis.com/v1alpha'


def classify_agent(engine_id: str, assistant_id: str, raw: dict[str, Any]) -> dict[str, Any]:
  """Normalizes a Discovery Engine Agent resource into the fleet schema (no telemetry yet)."""
  kind, backend = 'UNKNOWN', {'kind': 'unknown'}
  if 'adkAgentDefinition' in raw:
    kind = 'ADK'
    resource = ((raw['adkAgentDefinition'].get('provisionedReasoningEngine') or {}).get('reasoningEngine') or '')
    match = _REASONING_ENGINE_RE.match(resource)
    backend = {
        'kind': 'agent_engine',
        'resource': resource or None,
        'project': match['project'] if match else None,
        'location': match['location'] if match else None,
        'reasoning_engine_id': match['id'] if match else None,
    }
  elif 'a2aAgentDefinition' in raw:
    kind = 'A2A'
    try:
      card = json.loads(raw['a2aAgentDefinition'].get('jsonAgentCard') or '{}')
    except ValueError:
      card = {}
    url = str(card.get('url') or '')
    run = cloud_run_service_from_url(url) if url else None
    backend = {'kind': 'cloud_run' if run else 'external_endpoint', 'url': url or None}
    if run:
      backend.update(run)
  elif 'managedAgentDefinition' in raw:
    kind, backend = 'MANAGED', {'kind': 'google_managed'}
  elif 'lowCodeAgentDefinition' in raw or 'workflowAgentDefinition' in raw:
    kind, backend = 'LOW_CODE', {'kind': 'gemini_enterprise_hosted'}
  elif 'dialogflowAgentDefinition' in raw:
    kind = 'DIALOGFLOW'
    backend = {'kind': 'dialogflow', 'resource': raw['dialogflowAgentDefinition'].get('dialogflowAgent')}

  description = str(raw.get('description') or '')
  return {
      'agent_id': str(raw.get('name', '')).rsplit('/', 1)[-1],
      'resource_name': raw.get('name'),
      'display_name': raw.get('displayName') or str(raw.get('name', '')).rsplit('/', 1)[-1],
      'description': description[:280] + ('...' if len(description) > 280 else ''),
      'engine_id': engine_id,
      'assistant_id': assistant_id,
      'type': kind,
      'type_label': AGENT_TYPE_LABELS[kind],
      'state': raw.get('state') or 'STATE_UNSPECIFIED',
      'sharing_scope': (raw.get('sharingConfig') or {}).get('scope'),
      'created': raw.get('createTime'),
      'updated': raw.get('updateTime'),
      'starter_prompts': len(raw.get('starterPrompts') or []),
      'backend': backend,
      'telemetry_scope': {'ADK': 'agent', 'A2A': 'service' if backend.get('kind') == 'cloud_run' else 'none'}.get(kind, 'none'),
      'metrics': {
          'requests': None,
          'errors_4xx': None,
          'errors_5xx': None,
          'error_rate_pct': None,
          'latency_p50_ms': None,
          'latency_p95_ms': None,
          'llm_calls': None,
          'input_tokens': None,
          'output_tokens': None,
          'cached_tokens': None,
          'conversations': None,
          'last_activity': None,
          'vcpu_hours': None,
          'memory_gib_hours': None,
          'billable_instance_hours': None,
      },
      'data_sources': [],
      'notes': [],
  }


class _GoogleApi:
  """Minimal authenticated JSON client for Google REST APIs using ADC or gcloud CLI fallback."""

  def __init__(self, quota_project: str):
    self._quota_project = quota_project
    self._credentials = None
    self._is_user_credentials = False
    self._cli_token: str | None = None
    self._cli_token_ts: float = 0.0
    self._lock = threading.Lock()

  def _token(self) -> str:
    if google_auth is None or GoogleAuthRequest is None:
      raise FleetSourceError(0, 'google-auth is not installed; cannot obtain ADC credentials')
    with self._lock:
      try:
        if self._credentials is None:
          self._credentials, _ = google_auth.default(scopes=[_SCOPE])
          self._is_user_credentials = type(self._credentials).__module__.startswith('google.oauth2.credentials')
        if not self._credentials.valid:
          self._credentials.refresh(GoogleAuthRequest())
        if self._credentials.token:
          return self._credentials.token
      except Exception as exc:  # DefaultCredentialsError, RefreshError, transport errors.
        if self._quota_project and self._quota_project not in ('test-project', gcp_telemetry.UNCONFIGURED_PROJECT_ID):
          now_mono = time.monotonic()
          if self._cli_token and (now_mono - self._cli_token_ts) < 300.0:
            self._is_user_credentials = True
            return self._cli_token
          try:
            import subprocess
            res = subprocess.run(
                ['gcloud', 'auth', 'print-access-token'],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
              self._cli_token = res.stdout.strip()
              self._cli_token_ts = now_mono
              self._is_user_credentials = True
              return self._cli_token
          except Exception:
            pass
        raise FleetSourceError(0, f'Could not obtain Google Cloud credentials: {type(exc).__name__}') from exc
      return self._credentials.token

  def call(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {'Authorization': f'Bearer {self._token()}', 'Content-Type': 'application/json'}
    if self._is_user_credentials and self._quota_project:
      # End-user ADC (local development) needs an explicit quota project.
      headers['x-goog-user-project'] = self._quota_project
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
      with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as resp:
        raw = resp.read().decode('utf-8')
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
      detail = exc.read().decode('utf-8', 'replace')
      try:
        detail = json.loads(detail).get('error', {}).get('message', detail)
      except ValueError:
        pass
      raise FleetSourceError(exc.code, str(detail)[:300]) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
      raise FleetSourceError(0, f'{type(exc).__name__}: {exc}') from exc


class GeminiEnterpriseFleetService:
  """Collects the live agent inventory and telemetry for Gemini Enterprise app(s)."""

  def __init__(
      self,
      project_id: str | None = None,
      engine_ids: list[str] | None = None,
      location: str | None = None,
      collection: str | None = None,
      api: Any | None = None,
  ):
    self.project_id = project_id or gcp_telemetry.get_current_gcp_project()
    self.engine_ids = engine_ids or _env_list('VIBELIFT_GE_ENGINES', ('auto',))
    self.discovery_locations = _env_list('VIBELIFT_GE_LOCATIONS', DEFAULT_DISCOVERY_LOCATIONS)
    self.location = location or os.environ.get('VIBELIFT_GE_LOCATION', 'global')
    self.collection = collection or os.environ.get('VIBELIFT_GE_COLLECTION', 'default_collection')
    self.ttl_seconds = float(os.environ.get('VIBELIFT_FLEET_TTL_SECONDS', '60'))
    # Forced refreshes inside this interval are served from cache (protects API quotas).
    self.min_refresh_interval_s = float(os.environ.get('VIBELIFT_FLEET_MIN_REFRESH_SECONDS', '10'))
    self.max_trace_pages = int(os.environ.get('VIBELIFT_TRACE_MAX_PAGES', '6'))
    self.default_window_hours = _clamp_window(os.environ.get('VIBELIFT_FLEET_WINDOW_HOURS', '24'), 24)
    self.max_log_entries = int(os.environ.get('VIBELIFT_TOKEN_LOG_MAX_ENTRIES', '3000'))
    self._api = api or _GoogleApi(quota_project=self.project_id)
    self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
    self._cache_lock = threading.Lock()
    self._refresh_lock = threading.Lock()
    self._inflight_events: dict[int, threading.Event] = {}

  # ------------------------------------------------------------------ public API

  def collect(
      self,
      window_hours: int | None = None,
      force_refresh: bool = False,
      allow_stale: bool = False,
      max_wait_s: float | None = None,
  ) -> dict[str, Any]:
    """Returns the fleet payload for the window, served from cache or refreshed."""
    hours = _clamp_window(window_hours, self.default_window_hours)

    def usable(entry: dict[str, Any] | None) -> bool:
      if entry is None:
        return False
      return not force_refresh or entry['cache_age_seconds'] < self.min_refresh_interval_s

    cached = self._fresh_cache(hours)
    if usable(cached):
      return cached

    if allow_stale and not force_refresh:
      stale = self._any_cache(hours)
      if stale is not None:
        self._trigger_async_refresh(hours)
        return stale
      if max_wait_s is not None and max_wait_s > 0:
        evt = self._trigger_async_refresh(hours)
        evt.wait(timeout=max_wait_s)
        after_wait = self._any_cache(hours)
        if after_wait is not None:
          return after_wait
        return self._warming_placeholder(hours)

    return self._refresh_sync(hours, force_refresh=force_refresh)

  # ------------------------------------------------------------------ internals

  def _refresh_sync(self, hours: int, force_refresh: bool = False) -> dict[str, Any]:
    with self._refresh_lock:
      cached = self._fresh_cache(hours)
      if cached is not None and (
          not force_refresh or cached['cache_age_seconds'] < self.min_refresh_interval_s
      ):
        return cached
      payload = self._collect_uncached(hours)
      with self._cache_lock:
        self._cache[hours] = (time.monotonic(), payload)
      return dict(payload, cache_age_seconds=0.0)

  def _trigger_async_refresh(self, hours: int) -> threading.Event:
    with self._cache_lock:
      existing = self._inflight_events.get(hours)
      if existing is not None and not existing.is_set():
        return existing
      evt = threading.Event()
      self._inflight_events[hours] = evt

    def _worker() -> None:
      try:
        self._refresh_sync(hours, force_refresh=False)
      except Exception:
        logger.debug('Background fleet refresh failed for window %sh', hours, exc_info=True)
      finally:
        evt.set()

    threading.Thread(target=_worker, daemon=True).start()
    return evt

  def _fresh_cache(self, hours: int) -> dict[str, Any] | None:
    with self._cache_lock:
      entry = self._cache.get(hours)
    if entry is None:
      return None
    age = time.monotonic() - entry[0]
    if age >= self.ttl_seconds:
      return None
    return dict(entry[1], cache_age_seconds=round(age, 1))

  def _any_cache(self, hours: int) -> dict[str, Any] | None:
    with self._cache_lock:
      entry = self._cache.get(hours)
      if entry is None and self._cache:
        # Fall back to the most recently collected window while the requested window refreshes.
        entry = max(self._cache.values(), key=lambda item: item[0])
    if entry is None:
      return None
    age = time.monotonic() - entry[0]
    return dict(entry[1], cache_age_seconds=round(age, 1))

  def _warming_placeholder(self, hours: int) -> dict[str, Any]:
    return {
        'source': 'gemini_enterprise',
        'project_id': self.project_id,
        'location': self.location,
        'engines': [{'engine_id': eid, 'display_name': eid, 'agents_count': None} for eid in self.engine_ids],
        'window_hours': hours,
        'generated_at': _iso(_utcnow()),
        'collection_ms': 0,
        'cache_ttl_seconds': self.ttl_seconds,
        'cache_age_seconds': 0.0,
        'agents': [],
        'totals': {'agents': 0, 'enabled': 0, 'by_type': {}, 'with_runtime_telemetry': 0},
        'model_usage': None,
        'ge_traffic': None,
        'token_log_scan': {'entries_scanned': 0, 'truncated': False},
        'source_status': {'inventory': 'warming'},
        'errors': [],
        'notes': ['Live Gemini Enterprise telemetry collection is warming in the background; refresh in a moment.'],
    }

  def _de_base(self, location: str | None = None) -> str:
    loc = location or self.location
    host = 'discoveryengine.googleapis.com'
    if loc != 'global':
      host = f'{loc}-{host}'
    return (
        f'https://{host}/v1alpha/projects/{self.project_id}/locations/{loc}'
        f'/collections/{self.collection}/engines'
    )

  def _paged(self, url: str, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token = ''
    for _ in range(_MAX_PAGES):
      sep = '&' if '?' in url else '?'
      page = self._api.call('GET', url + (f'{sep}pageToken={urllib.parse.quote(token)}' if token else ''))
      items.extend(page.get(key, []))
      token = page.get('nextPageToken', '')
      if not token:
        break
    return items

  def _discover_engine_specs(self, errors: list[dict[str, str]]) -> list[str]:
    """Lists every Gemini Enterprise app (APP_TYPE_INTRANET engine) across the discovery locations."""
    specs: list[str] = []
    for loc in self.discovery_locations:
      try:
        engines = self._paged(f'{self._de_base(loc)}?pageSize=100', 'engines')
      except FleetSourceError as exc:
        errors.append({'source': f'Discovery Engine app discovery ({loc})', 'detail': str(exc)})
        continue
      for eng in engines:
        if eng.get('appType') == 'APP_TYPE_INTRANET':
          specs.append(f"{loc}/{str(eng.get('name', '')).rsplit('/', 1)[-1]}")
    return specs

  def _resolve_engine_specs(self, errors: list[dict[str, str]]) -> list[str]:
    """Expands 'auto' into the discovered GE apps; explicit specs are kept as configured."""
    explicit = [e for e in self.engine_ids if e.strip().lower() != 'auto']
    if len(explicit) == len(self.engine_ids):
      return list(self.engine_ids)
    discovered = self._discover_engine_specs(errors)
    specs = list(dict.fromkeys(explicit + discovered))
    return specs or list(DEFAULT_ENGINE_IDS)

  def _list_engine_agents(self, engine_spec: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if '/' in engine_spec:
      loc, engine_id = engine_spec.split('/', 1)
    else:
      loc, engine_id = self.location, engine_spec
    base = f'{self._de_base(loc)}/{engine_id}'
    engine = self._api.call('GET', base)
    try:
      assistants = [a['name'].rsplit('/', 1)[-1] for a in self._paged(f'{base}/assistants?pageSize=100', 'assistants')]
    except FleetSourceError:
      assistants = []
    agents = []
    display = engine.get('displayName') or engine_id
    for assistant_id in assistants or ['default_assistant']:
      for raw in self._paged(f'{base}/assistants/{assistant_id}/agents?pageSize=100', 'agents'):
        agent = classify_agent(engine_id, assistant_id, raw)
        agent['location'] = loc
        agent['engine_key'] = f'{loc}/{engine_id}'
        agent['engine_display_name'] = display
        agents.append(agent)
    meta = {
        'engine_id': engine_id,
        'engine_key': f'{loc}/{engine_id}',
        'location': loc,
        'display_name': display,
        'app_type': engine.get('appType'),
        'agents_count': len(agents),
    }
    return meta, agents

  def _timeseries(
      self,
      project: str,
      metric_filter: str,
      window_s: int,
      group_by: list[str],
      aligner: str = 'ALIGN_SUM',
      reducer: str = 'REDUCE_SUM',
      end_offset_s: int = 0,
  ) -> list[tuple[dict[str, str], float]]:
    end = _utcnow() - datetime.timedelta(seconds=end_offset_s)
    params = [
        ('filter', metric_filter),
        ('interval.startTime', _iso(end - datetime.timedelta(seconds=window_s))),
        ('interval.endTime', _iso(end)),
        ('aggregation.alignmentPeriod', f'{window_s}s'),
        ('aggregation.perSeriesAligner', aligner),
        ('aggregation.crossSeriesReducer', reducer),
    ] + [('aggregation.groupByFields', field) for field in group_by]
    url = f'{_MONITORING}/projects/{project}/timeSeries?{urllib.parse.urlencode(params)}'
    rows = []
    for series in self._paged(url, 'timeSeries'):
      points = series.get('points') or []
      if not points:
        continue
      value = points[0].get('value', {})  # Newest point covers exactly the requested window.
      number = value.get('int64Value', value.get('doubleValue'))
      if number is None and 'distributionValue' in value:
        number = value['distributionValue'].get('mean')
      labels = dict(series.get('resource', {}).get('labels', {}))
      labels.update(series.get('metric', {}).get('labels', {}))
      rows.append((labels, float(number or 0.0)))
    return rows

  @staticmethod
  def _one_of(values: list[str]) -> str:
    return 'one_of(' + ','.join(json.dumps(v) for v in sorted(set(values))) + ')'

  def _requests_by(self, project: str, metric: str, label: str, ids: list[str], window_s: int) -> dict[str, dict[str, int]]:
    rows = self._timeseries(
        project,
        f'metric.type="{metric}" AND resource.label.{label} = {self._one_of(ids)}',
        window_s,
        [f'resource.label.{label}', 'metric.label.response_code_class'],
    )
    out: dict[str, dict[str, int]] = {i: {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0} for i in ids}
    for labels, value in rows:
      key = labels.get(label)
      if key not in out:
        continue
      count = int(round(value))
      out[key]['requests'] += count
      code_class = str(labels.get('response_code_class') or '')
      if code_class.startswith('4'):
        out[key]['errors_4xx'] += count
      elif code_class.startswith('5'):
        out[key]['errors_5xx'] += count
    return out

  def _request_series(
      self, project: str, metric: str, label: str, ids: list[str], window_s: int, bucket_s: int
  ) -> dict[str, dict[int, dict[str, int]]]:
    """Request counts per runtime id per point end time: {id: {end_epoch: {requests, errors_4xx, errors_5xx}}}.

    For windows up to RAW_SERIES_MAX_WINDOW_S the raw 60 s DELTA points are fetched and binned by the
    caller. Server-side alignment is avoided there because Cloud Monitoring adds a raw point that
    straddles an alignment boundary to both adjacent buckets, which double-counts requests.
    """
    end = _utcnow()
    params = [
        ('filter', f'metric.type="{metric}" AND resource.label.{label} = {self._one_of(ids)}'),
        ('interval.startTime', _iso(end - datetime.timedelta(seconds=window_s))),
        ('interval.endTime', _iso(end)),
    ]
    if window_s > RAW_SERIES_MAX_WINDOW_S:
      params += [
          ('aggregation.alignmentPeriod', f'{bucket_s}s'),
          ('aggregation.perSeriesAligner', 'ALIGN_SUM'),
          ('aggregation.crossSeriesReducer', 'REDUCE_SUM'),
          ('aggregation.groupByFields', f'resource.label.{label}'),
          ('aggregation.groupByFields', 'metric.label.response_code_class'),
      ]
    url = f'{_MONITORING}/projects/{project}/timeSeries?{urllib.parse.urlencode(params)}'
    out: dict[str, dict[int, dict[str, int]]] = {}
    for series in self._paged(url, 'timeSeries'):
      labels = dict(series.get('resource', {}).get('labels', {}))
      labels.update(series.get('metric', {}).get('labels', {}))
      key = labels.get(label)
      if key not in ids:
        continue
      code_class = str(labels.get('response_code_class') or '')
      for pt in series.get('points') or []:
        t_end = pt.get('interval', {}).get('endTime')
        if not t_end:
          continue
        epoch = int(datetime.datetime.fromisoformat(t_end.replace('Z', '+00:00')).timestamp())
        val = pt.get('value', {})
        count = int(round(float(val.get('int64Value', val.get('doubleValue', 0)) or 0)))
        slot = out.setdefault(key, {}).setdefault(epoch, {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0})
        slot['requests'] += count
        if code_class.startswith('4'):
          slot['errors_4xx'] += count
        elif code_class.startswith('5'):
          slot['errors_5xx'] += count
    return out

  def _latency_by(self, project: str, metric: str, label: str, ids: list[str], window_s: int) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pct in ('50', '95'):
      rows = self._timeseries(
          project,
          f'metric.type="{metric}" AND resource.label.{label} = {self._one_of(ids)}',
          window_s,
          [f'resource.label.{label}'],
          aligner='ALIGN_DELTA',
          reducer=f'REDUCE_PERCENTILE_{pct}',
      )
      for labels, value in rows:
        if labels.get(label):
          out.setdefault(labels[label], {})[f'p{pct}'] = round(value, 1)
    return out

  def _sum_by(self, project: str, metric: str, label: str, ids: list[str], window_s: int) -> dict[str, float]:
    rows = self._timeseries(
        project,
        f'metric.type="{metric}" AND resource.label.{label} = {self._one_of(ids)}',
        window_s,
        [f'resource.label.{label}'],
    )
    out = {i: 0.0 for i in ids}
    for labels, value in rows:
      if labels.get(label) in out:
        out[labels[label]] += value
    return out

  def _genai_usage(self, project: str, engine_ids: list[str], window_s: int) -> dict[str, Any]:
    """Aggregates OpenTelemetry GenAI inference events written by ADK agents to Cloud Logging."""
    start = _iso(_utcnow() - datetime.timedelta(seconds=window_s))
    ids = ' OR '.join(json.dumps(i) for i in sorted(set(engine_ids)))
    body: dict[str, Any] = {
        'resourceNames': [f'projects/{project}'],
        'filter': (
            'resource.type="aiplatform.googleapis.com/ReasoningEngine" '
            f'AND resource.labels.reasoning_engine_id=({ids}) '
            'AND labels."gen_ai.usage.input_tokens":* '
            f'AND timestamp>="{start}"'
        ),
        'orderBy': 'timestamp desc',
        'pageSize': min(1000, self.max_log_entries),
    }
    stats: dict[str, dict[str, Any]] = {
        i: {'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'last_activity': None,
            '_conversations': set(), '_models': collections.Counter()}
        for i in engine_ids
    }
    scanned, truncated = 0, False
    for _ in range(_MAX_PAGES):
      page = self._api.call('POST', f'{_LOGGING}/entries:list', body)
      for entry in page.get('entries', []):
        scanned += 1
        engine = entry.get('resource', {}).get('labels', {}).get('reasoning_engine_id')
        if engine not in stats:
          continue
        labels = entry.get('labels', {})
        s = stats[engine]
        s['llm_calls'] += 1
        s['input_tokens'] += _to_int(labels.get('gen_ai.usage.input_tokens'))
        s['output_tokens'] += _to_int(labels.get('gen_ai.usage.output_tokens'))
        for key, value in labels.items():
          if key.startswith('gen_ai.usage.') and 'cache' in key:
            s['cached_tokens'] += _to_int(value)
        if labels.get('gen_ai.conversation.id'):
          s['_conversations'].add(labels['gen_ai.conversation.id'])
        model = labels.get('gen_ai.response.model') or labels.get('gen_ai.request.model')
        if model:
          s['_models'][model] += 1
        stamp = entry.get('timestamp')
        if stamp and (s['last_activity'] is None or stamp > s['last_activity']):
          s['last_activity'] = stamp
      token = page.get('nextPageToken')
      if not token:
        break
      if scanned >= self.max_log_entries:
        truncated = True
        break
      body['pageToken'] = token
    for s in stats.values():
      s['conversations'] = len(s.pop('_conversations'))
      s['models'] = [m for m, _ in s.pop('_models').most_common(3)]
    return {'by_engine': stats, 'entries_scanned': scanned, 'truncated': truncated}

  def _trace_usage(self, project: str, engine_ids: list[str], window_s: int) -> dict[str, Any]:
    """Per-engine gen_ai token usage from Cloud Trace spans exported by Agent Engine telemetry."""
    traces, truncated = self._list_traces(project, '+cloud.platform:gcp.agent_engine', window_s)
    return {'by_engine': aggregate_trace_usage(traces, engine_ids), 'traces_scanned': len(traces), 'truncated': truncated}

  def _ge_assistant_usage(self, window_s: int) -> dict[str, Any]:
    """Gemini Enterprise assistant token usage per app, from Cloud Trace (gcp.gemini_enterprise spans)."""
    traces, truncated = self._list_traces(self.project_id, '+cloud.platform:gcp.gemini_enterprise', window_s)
    out = aggregate_ge_assistant_usage(traces)
    out.update({
        'traces_scanned': len(traces), 'truncated': truncated,
        'scope': f'Gemini Enterprise assistant model calls exported to Cloud Trace in {self.project_id}',
    })
    return out

  def _list_traces(self, project: str, trace_filter: str, window_s: int) -> tuple[list[dict[str, Any]], bool]:
    end = _utcnow()
    params = {
        'startTime': _iso(end - datetime.timedelta(seconds=window_s)),
        'endTime': _iso(end),
        'view': 'COMPLETE',
        'pageSize': '500',
        'filter': trace_filter,
    }
    traces: list[dict[str, Any]] = []
    truncated = False
    token = ''
    for page_no in range(self.max_trace_pages):
      query = dict(params, **({'pageToken': token} if token else {}))
      page = self._api.call('GET', f'{_TRACE}/projects/{project}/traces?{urllib.parse.urlencode(query)}')
      traces.extend(page.get('traces', []))
      token = page.get('nextPageToken', '')
      if not token:
        break
      if page_no == self.max_trace_pages - 1:
        truncated = True
    return traces, truncated

  def _reasoning_engine_meta(self, resource: str, location: str) -> dict[str, Any]:
    try:
      data = self._api.call('GET', f'{_AIPLATFORM.format(location=location)}/{resource}')
    except FleetSourceError as exc:
      if exc.status == 404:
        return {'exists': False, 'http_status': 404}
      raise  # Permission or transient errors are not evidence that the engine is gone.
    spec = data.get('spec') or {}
    return {'exists': True, 'display_name': data.get('displayName'), 'framework': spec.get('agentFramework')}

  def _cloud_run_services(self, project: str) -> set[str]:
    """Names of every Cloud Run service in the project (all regions). Raises if the list is incomplete."""
    names: set[str] = set()
    token = ''
    for _ in range(_MAX_PAGES):
      url = f'https://run.googleapis.com/v2/projects/{project}/locations/-/services?pageSize=500'
      if token:
        url += f'&pageToken={urllib.parse.quote(token)}'
      page = self._api.call('GET', url)
      for svc in page.get('services', []):
        names.add(str(svc.get('name', '')).rsplit('/', 1)[-1])
      token = page.get('nextPageToken', '')
      if not token:
        return names
    raise FleetSourceError(0, 'Cloud Run service list exceeded the page limit; existence not verified.')

  def _rate_cards(self) -> dict[str, dict[str, float]]:
    cards = {
        name: {
            'input': card.input_per_million_usd,
            'output': card.output_per_million_usd,
            'cached_read': card.cached_read_per_million_usd,
            'cache_write': card.cache_write_per_million_usd,
        }
        for name, card in telemetry.RATE_CARDS.items()
    }
    override = os.environ.get('VIBELIFT_RATE_CARDS_JSON', '').strip()
    if override:
      try:
        for name, card in json.loads(override).items():
          cards[name] = {k: float(card.get(k, 0.0)) for k in ('input', 'output', 'cached_read', 'cache_write')}
      except (ValueError, AttributeError, TypeError) as exc:
        logger.warning('Ignoring invalid VIBELIFT_RATE_CARDS_JSON: %s', exc)
    return cards

  def rate_cards(self) -> dict[str, dict[str, float]]:
    """List-price rate cards (USD per 1M tokens) used for every cost estimate."""
    return self._rate_cards()

  def _model_usage(self, window_s: int, end_offset_s: int = 0) -> dict[str, Any]:
    token_rows = self._timeseries(
        self.project_id,
        'metric.type="aiplatform.googleapis.com/publisher/online_serving/token_count"',
        window_s,
        ['resource.label.model_user_id', 'metric.label.type'],
        end_offset_s=end_offset_s,
    )
    invocation_rows = self._timeseries(
        self.project_id,
        'metric.type="aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"',
        window_s,
        ['resource.label.model_user_id'],
        end_offset_s=end_offset_s,
    )
    models: dict[str, dict[str, Any]] = {}

    def bucket(model: str) -> dict[str, Any]:
      return models.setdefault(model, {
          'model': model, 'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0,
          'cache_write_tokens': 0, 'other_tokens': 0, 'invocations': 0})

    for labels, value in token_rows:
      row = bucket(labels.get('model_user_id') or 'unknown')
      token_type = str(labels.get('type') or '').lower()
      count = int(round(value))
      if token_type == 'input':
        row['input_tokens'] += count
      elif token_type == 'output':
        row['output_tokens'] += count
      elif 'cache_read' in token_type or token_type.startswith('cached'):
        row['cache_read_tokens'] += count
      elif token_type.startswith('cache_write'):
        row['cache_write_tokens'] += count
      else:
        row['other_tokens'] += count
    for labels, value in invocation_rows:
      bucket(labels.get('model_user_id') or 'unknown')['invocations'] += int(round(value))

    cards = self._rate_cards()
    totals = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0, 'cache_write_tokens': 0,
              'invocations': 0, 'est_cost_usd': 0.0, 'models_without_rate_card': []}
    rows = []
    for row in sorted(models.values(), key=lambda r: -(r['input_tokens'] + r['output_tokens'])):
      if not any(row[k] for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'invocations')):
        continue
      card = cards.get(row['model'])
      if card:
        row['est_cost_usd'] = round(
            row['input_tokens'] / 1e6 * card['input'] + row['output_tokens'] / 1e6 * card['output']
            + row['cache_read_tokens'] / 1e6 * card['cached_read']
            + row['cache_write_tokens'] / 1e6 * card['cache_write'], 4)
        totals['est_cost_usd'] += row['est_cost_usd']
      else:
        row['est_cost_usd'] = None
        totals['models_without_rate_card'].append(row['model'])
      denominator = row['input_tokens'] + row['cache_read_tokens'] + row['cache_write_tokens']
      row['cache_read_share_pct'] = round(row['cache_read_tokens'] / denominator * 100, 1) if denominator else None
      for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'invocations'):
        totals[key] += row[key]
      rows.append(row)
    totals['est_cost_usd'] = round(totals['est_cost_usd'], 4)
    end = _utcnow() - datetime.timedelta(seconds=end_offset_s)
    return {
        'scope': f'Project-wide Vertex AI model usage in {self.project_id}',
        'models': rows,
        'totals': totals,
        'interval': {'start': _iso(end - datetime.timedelta(seconds=window_s)), 'end': _iso(end)},
        'rate_cards': {r['model']: cards[r['model']] for r in rows if r['model'] in cards},
    }

  def _ge_traffic(self, window_s: int) -> dict[str, Any]:
    rows = self._timeseries(
        self.project_id,
        'metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.type="consumed_api" '
        'AND resource.label.service="discoveryengine.googleapis.com"',
        window_s,
        ['resource.label.method'],
    )
    assist = sum(v for labels, v in rows if str(labels.get('method', '')).endswith('AssistantService.StreamAssist'))
    return {
        'assistant_requests': int(round(assist)),
        'scope': f'Project-wide Gemini Enterprise assistant (StreamAssist) calls in {self.project_id}',
    }

  def _collect_uncached(self, hours: int) -> dict[str, Any]:
    started = time.monotonic()
    window_s = hours * 3600
    errors: list[dict[str, str]] = []
    status: dict[str, str] = {}
    engines, agents = [], []

    for engine_id in self._resolve_engine_specs(errors):
      try:
        meta, engine_agents = self._list_engine_agents(engine_id)
        engines.append(meta)
        agents.extend(engine_agents)
      except FleetSourceError as exc:
        errors.append({'source': f'Discovery Engine agents ({engine_id})', 'detail': str(exc)})
        loc, _, eid = engine_id.rpartition('/')
        engines.append({'engine_id': eid, 'engine_key': f"{loc or self.location}/{eid}",
                        'location': loc or self.location, 'display_name': eid, 'agents_count': None})
    status['inventory'] = 'error' if errors else 'ok'

    engines_by_project: dict[str, set[str]] = collections.defaultdict(set)
    services_by_project: dict[str, set[str]] = collections.defaultdict(set)
    engine_meta_jobs: dict[str, str] = {}
    for agent in agents:
      backend = agent['backend']
      if backend.get('kind') == 'agent_engine' and backend.get('reasoning_engine_id'):
        engines_by_project[backend.get('project') or self.project_id].add(backend['reasoning_engine_id'])
        if backend.get('resource') and backend.get('location'):
          engine_meta_jobs[backend['resource']] = backend['location']
      elif backend.get('kind') == 'cloud_run':
        services_by_project[backend.get('project') or self.project_id].add(backend['service'])

    re_metric = 'aiplatform.googleapis.com/reasoning_engine'
    bucket_s = trend_bucket_seconds(hours)
    jobs: dict[tuple[str, ...], Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
      for project, ids in engines_by_project.items():
        id_list = sorted(ids)
        jobs[('re_requests', project)] = pool.submit(self._requests_by, project, f'{re_metric}/request_count', 'reasoning_engine_id', id_list, window_s)
        jobs[('re_latency', project)] = pool.submit(self._latency_by, project, f'{re_metric}/request_latencies', 'reasoning_engine_id', id_list, window_s)
        jobs[('re_cpu', project)] = pool.submit(self._sum_by, project, f'{re_metric}/cpu/allocation_time', 'reasoning_engine_id', id_list, window_s)
        jobs[('re_mem', project)] = pool.submit(self._sum_by, project, f'{re_metric}/memory/allocation_time', 'reasoning_engine_id', id_list, window_s)
        jobs[('re_tokens', project)] = pool.submit(self._genai_usage, project, id_list, window_s)
        jobs[('re_trace_tokens', project)] = pool.submit(self._trace_usage, project, id_list, window_s)
        jobs[('re_series', project)] = pool.submit(self._request_series, project, f'{re_metric}/request_count', 'reasoning_engine_id', id_list, window_s, bucket_s)
      for project, services in services_by_project.items():
        svc_list = sorted(services)
        jobs[('run_requests', project)] = pool.submit(self._requests_by, project, 'run.googleapis.com/request_count', 'service_name', svc_list, window_s)
        jobs[('run_latency', project)] = pool.submit(self._latency_by, project, 'run.googleapis.com/request_latencies', 'service_name', svc_list, window_s)
        jobs[('run_billable', project)] = pool.submit(self._sum_by, project, 'run.googleapis.com/container/billable_instance_time', 'service_name', svc_list, window_s)
        jobs[('run_series', project)] = pool.submit(self._request_series, project, 'run.googleapis.com/request_count', 'service_name', svc_list, window_s, bucket_s)
      for resource, location in engine_meta_jobs.items():
        jobs[('re_meta', resource)] = pool.submit(self._reasoning_engine_meta, resource, location)
      run_projects = {a['backend'].get('project') or self.project_id for a in agents
                      if a['backend'].get('kind') == 'cloud_run' and a['backend'].get('service')}
      for project in sorted(run_projects):
        jobs[('run_inventory', project)] = pool.submit(self._cloud_run_services, project)
      jobs[('model_usage',)] = pool.submit(self._model_usage, window_s)
      jobs[('model_usage_prev',)] = pool.submit(self._model_usage, window_s, window_s)
      jobs[('ge_traffic',)] = pool.submit(self._ge_traffic, window_s)
      jobs[('ge_assistant_tokens',)] = pool.submit(self._ge_assistant_usage, window_s)

      results: dict[tuple[str, ...], Any] = {}
      source_names = {
          're_requests': 'Agent Engine request metrics', 're_latency': 'Agent Engine latency metrics',
          're_cpu': 'Agent Engine vCPU metrics', 're_mem': 'Agent Engine memory metrics',
          're_tokens': 'Agent GenAI token logs', 're_trace_tokens': 'Agent GenAI trace spans',
          'run_requests': 'Cloud Run request metrics',
          'run_latency': 'Cloud Run latency metrics', 'run_billable': 'Cloud Run billable time',
          're_meta': 'Agent Engine metadata', 'model_usage': 'Vertex AI model token metrics',
          'ge_traffic': 'Gemini Enterprise API traffic',
          'ge_assistant_tokens': 'Gemini Enterprise assistant trace spans',
          're_series': 'Agent Engine request history', 'run_series': 'Cloud Run request history',
          'run_inventory': 'Cloud Run service list', 'model_usage_prev': 'Vertex AI model token metrics (previous period)',
      }
      for key, future in jobs.items():
        try:
          results[key] = future.result()
        except Exception as exc:  # Each source degrades independently.
          results[key] = None
          if key[0] not in ('re_meta', 'run_inventory'):  # Shown per agent as UNVERIFIED instead.
            errors.append({'source': source_names.get(key[0], key[0]), 'detail': str(exc)[:300]})

    def source_state(prefix: str) -> str:
      keys = [k for k in results if k[0] == prefix]
      if not keys:
        return 'not_applicable'
      return 'ok' if all(results[k] is not None for k in keys) else 'error'

    for name, prefix in (('agent_engine_metrics', 're_requests'), ('agent_token_logs', 're_tokens'),
                         ('agent_token_traces', 're_trace_tokens'),
                         ('cloud_run_metrics', 'run_requests'), ('model_usage', 'model_usage'),
                         ('ge_traffic', 'ge_traffic'), ('ge_assistant_tokens', 'ge_assistant_tokens')):
      status[name] = source_state(prefix)

    checked_at = _iso(_utcnow())
    for agent in agents:
      self._apply_telemetry(agent, results)
      agent['registration'] = assess_registration(agent, results, checked_at, self.project_id)

    telemetry_agents = [a for a in agents if a['metrics']['requests'] is not None]
    # The same runtime (Cloud Run service / Agent Engine) can be registered as an
    # agent in several GE engines; its metrics must only be counted once.
    unique_runtime_agents = []
    seen_backends: set[str] = set()
    for a in agents:
      backend_key = runtime_backend_key(a)
      if backend_key in seen_backends:
        continue
      seen_backends.add(backend_key)
      unique_runtime_agents.append(a)
    totals: dict[str, Any] = {
        'agents': len(agents),
        'enabled': sum(1 for a in agents if a['state'] == 'ENABLED'),
        'by_type': dict(collections.Counter(a['type'] for a in agents)),
        'with_runtime_telemetry': len(telemetry_agents),
        'unique_runtimes': len(unique_runtime_agents),
        'broken_registrations': sum(
            1 for a in agents if a['registration']['status'] in ('BACKEND_NOT_FOUND', 'NO_BACKEND')),
        'unverified_registrations': sum(1 for a in agents if a['registration']['status'] == 'UNVERIFIED'),
    }
    for key in ('requests', 'errors_4xx', 'errors_5xx', 'llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens', 'conversations'):
      values = [a['metrics'][key] for a in unique_runtime_agents if a['metrics'].get(key) is not None]
      totals[key] = sum(values) if values else None
    for fkey in ('vcpu_hours', 'memory_gib_hours', 'billable_instance_hours'):
      fvals = [float(a['metrics'][fkey]) for a in unique_runtime_agents if a['metrics'].get(fkey) is not None]
      totals[fkey] = round(sum(fvals), 4) if fvals else None
    totals['error_rate_pct'] = (
        round((totals['errors_5xx'] or 0) / totals['requests'] * 100, 2) if totals.get('requests') else None)
    stamps = [a['metrics']['last_activity'] for a in agents if a['metrics']['last_activity']]
    totals['last_activity'] = max(stamps) if stamps else None

    token_results = [results[k] for k in results if k[0] == 're_tokens' and results[k]]
    trace_results = [results[k] for k in results if k[0] == 're_trace_tokens' and results[k]]
    return {
        'source': 'gemini_enterprise',
        'project_id': self.project_id,
        'location': self.location,
        'engines': engines,
        'window_hours': hours,
        'generated_at': _iso(_utcnow()),
        'collection_ms': int((time.monotonic() - started) * 1000),
        'cache_ttl_seconds': self.ttl_seconds,
        'agents': agents,
        'totals': totals,
        'model_usage': results.get(('model_usage',)),
        'model_usage_previous': results.get(('model_usage_prev',)),
        'ge_traffic': results.get(('ge_traffic',)),
        'ge_assistant_usage': results.get(('ge_assistant_tokens',)),
        'trend': build_request_trend(agents, results, window_s, bucket_s, self.project_id),
        'token_log_scan': {
            'entries_scanned': sum(r['entries_scanned'] for r in token_results),
            'truncated': any(r['truncated'] for r in token_results),
            'traces_scanned': sum(r['traces_scanned'] for r in trace_results),
            'traces_truncated': any(r['truncated'] for r in trace_results),
        },
        'source_status': status,
        'errors': errors,
        'notes': [
            'Cloud Monitoring data points typically appear 1-3 minutes after the request.',
            'A2A agents on Cloud Run report service-level metrics (all traffic to the service).',
            'Google-managed and no-code agents expose no per-project runtime telemetry.',
        ],
    }

  def _apply_telemetry(self, agent: dict[str, Any], results: dict[tuple[str, ...], Any]) -> None:
    """Joins one agent with the telemetry fetched for its backend (exact project match)."""
    backend, metrics = agent['backend'], agent['metrics']
    project = backend.get('project') or self.project_id
    pick = lambda name: results.get((name, project))
    if backend.get('kind') == 'agent_engine' and backend.get('reasoning_engine_id'):
      rid = backend['reasoning_engine_id']
      requests = pick('re_requests')
      if requests is not None and rid in requests:
        metrics.update(requests[rid])
        agent['data_sources'].append('Cloud Monitoring: reasoning_engine/request_count')
      latency = pick('re_latency')
      if latency is not None:
        metrics['latency_p50_ms'] = (latency.get(rid) or {}).get('p50')
        metrics['latency_p95_ms'] = (latency.get(rid) or {}).get('p95')
      cpu, mem = pick('re_cpu'), pick('re_mem')
      if cpu is not None and rid in cpu:
        metrics['vcpu_hours'] = round(cpu[rid] / 3600.0, 4)
      if mem is not None and rid in mem:
        metrics['memory_gib_hours'] = round(mem[rid] / 3600.0, 4)
      tokens, traced = pick('re_tokens'), pick('re_trace_tokens')
      log_usage = (tokens or {}).get('by_engine', {}).get(rid) if tokens is not None else None
      trace_usage = (traced or {}).get('by_engine', {}).get(rid) if traced is not None else None
      usage, source = None, None
      if log_usage and log_usage['llm_calls']:
        usage, source = log_usage, 'Cloud Logging: OpenTelemetry gen_ai inference events'
      elif trace_usage and trace_usage['llm_calls']:
        usage, source = trace_usage, 'Cloud Trace: OpenTelemetry gen_ai spans'
      elif tokens is not None and traced is not None and not metrics.get('requests'):
        usage = log_usage  # Both sources checked and the agent had no traffic: a true zero.
      if usage is not None:
        for key in ('llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens', 'conversations', 'last_activity'):
          metrics[key] = usage[key]
        if usage.get('models'):
          backend['models'] = usage['models']
      if source:
        metrics['token_source'] = source
        agent['data_sources'].append(source)
      elif metrics.get('requests') and tokens is not None and traced is not None:
        agent['notes'].append(
            'Served requests but emitted no gen_ai token telemetry (logs or traces). Deploy with '
            'GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true to measure tokens.')
      meta = results.get(('re_meta', backend.get('resource')))
      if meta:
        backend['display_name'] = meta.get('display_name')
        backend['framework'] = meta.get('framework')
    elif backend.get('kind') == 'cloud_run':
      service = backend['service']
      requests = pick('run_requests')
      if requests is not None and service in requests:
        metrics.update(requests[service])
        agent['data_sources'].append('Cloud Monitoring: run.googleapis.com/request_count (service-level)')
      latency = pick('run_latency')
      if latency is not None:
        metrics['latency_p50_ms'] = (latency.get(service) or {}).get('p50')
        metrics['latency_p95_ms'] = (latency.get(service) or {}).get('p95')
      billable = pick('run_billable')
      if billable is not None and service in billable:
        metrics['billable_instance_hours'] = round(billable[service] / 3600.0, 4)
      agent['notes'].append('Service-level metrics: include every request to this Cloud Run service.')
    elif agent['type'] in ('MANAGED', 'LOW_CODE'):
      agent['notes'].append('Runs inside Gemini Enterprise; no per-project runtime telemetry is exposed.')
    elif backend.get('kind') == 'external_endpoint':
      agent['notes'].append('Hosted outside Cloud Run/Agent Engine; runtime telemetry not available.')

    if metrics['requests']:
      metrics['error_rate_pct'] = round((metrics['errors_5xx'] or 0) / metrics['requests'] * 100, 2)


_SERVICE: GeminiEnterpriseFleetService | None = None
_SERVICE_LOCK = threading.Lock()


def get_ge_fleet_service() -> GeminiEnterpriseFleetService:
  """Returns the process-wide fleet service (one shared cache for UI, MCP, and ADK)."""
  global _SERVICE
  with _SERVICE_LOCK:
    if _SERVICE is None:
      _SERVICE = GeminiEnterpriseFleetService()
    return _SERVICE


def _fmt_tokens(value: Any) -> str:
  number = float(value or 0)
  if number >= 1e6:
    return f'{number / 1e6:.1f}M'
  if number >= 1e3:
    return f'{number / 1e3:.1f}k'
  return f'{int(number)}'


def _fmt_ms(value: Any) -> str:
  if value is None:
    return 'n/a'
  return f'{value / 1000:.1f} s' if value >= 1000 else f'{value:.0f} ms'


def summarize_fleet(payload: dict[str, Any]) -> str:
  """Plain-text summary of a fleet payload, used in chat replies and tool results."""
  engines = ', '.join(
      f"{e.get('display_name') or e.get('engine_id')} ({e.get('engine_id')})" for e in payload.get('engines') or []
  ) or 'no Gemini Enterprise app'
  totals = payload.get('totals') or {}
  by_type = ', '.join(f'{count} {kind}' for kind, count in sorted((totals.get('by_type') or {}).items()))
  lines = [
      f"Gemini Enterprise agent fleet for {engines} in project {payload.get('project_id')} "
      f"(last {payload.get('window_hours')}h, generated {payload.get('generated_at')}):",
      f"- Agents: {totals.get('agents', 0)} ({totals.get('enabled', 0)} enabled" + (f'; {by_type}' if by_type else '') + ')',
  ]
  for agent in payload.get('agents') or []:
    m = agent.get('metrics') or {}
    parts = [f"{agent.get('display_name')} [{agent.get('type')}, {agent.get('state')}]"]
    if m.get('requests') is not None:
      parts.append(f"{m['requests']:,} requests ({m.get('errors_4xx') or 0} 4xx, {m.get('errors_5xx') or 0} 5xx)")
    if m.get('latency_p95_ms') is not None:
      parts.append(f"p50 {_fmt_ms(m.get('latency_p50_ms'))} / p95 {_fmt_ms(m['latency_p95_ms'])}")
    if m.get('input_tokens') is not None:
      parts.append(
          f"{_fmt_tokens(m['input_tokens'])} input / {_fmt_tokens(m.get('output_tokens'))} output tokens "
          f"over {m.get('llm_calls') or 0:,} LLM calls, {m.get('conversations') or 0:,} conversations")
    if m.get('last_activity'):
      parts.append(f"last activity {m['last_activity']}")
    scope = agent.get('telemetry_scope')
    if scope == 'service':
      parts.append('service-level metrics')
    elif scope == 'none':
      parts.append('inventory only (no per-project runtime telemetry)')
    lines.append('- ' + '; '.join(parts))
  usage_totals = (payload.get('model_usage') or {}).get('totals') or {}
  if usage_totals:
    line = (
        f"- Project-wide Vertex AI model usage: {_fmt_tokens(usage_totals.get('input_tokens'))} input / "
        f"{_fmt_tokens(usage_totals.get('output_tokens'))} output tokens, {usage_totals.get('invocations', 0):,} calls, "
        f"est. ${usage_totals.get('est_cost_usd', 0.0):,.2f} at list price")
    if usage_totals.get('models_without_rate_card'):
      line += f" (no rate card for {', '.join(usage_totals['models_without_rate_card'])})"
    lines.append(line)
  traffic = payload.get('ge_traffic') or {}
  if traffic:
    lines.append(f"- Gemini Enterprise assistant calls (project-wide): {traffic.get('assistant_requests', 0):,}")
  if payload.get('errors'):
    lines.append('- Unavailable sources: ' + '; '.join(str(e.get('source')) for e in payload['errors']))
  return '\n'.join(lines)
