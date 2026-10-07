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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

try:
  import google.auth as google_auth
  from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover - google-auth ships with the Cloud client libraries.
  google_auth = None  # type: ignore[assignment]
  GoogleAuthRequest = None  # type: ignore[assignment,misc]

from vibelift import finops as live_finops
from vibelift import gcp_telemetry, telemetry

logger = logging.getLogger(__name__)

_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
_MONITORING = 'https://monitoring.googleapis.com/v3'
_TRACE = 'https://cloudtrace.googleapis.com/v1'
_LOGGING = 'https://logging.googleapis.com/v2'
_AIPLATFORM = 'https://{location}-aiplatform.googleapis.com/v1'
_GKE = 'https://container.googleapis.com/v1'
_HTTP_TIMEOUT_S = 25.0
_MAX_PAGES = 10

# VIBELIFT_GE_ENGINES defaults to 'auto', which discovers every Gemini Enterprise app in these locations.
DEFAULT_DISCOVERY_LOCATIONS = ('global', 'us', 'eu')
# Locations scanned for standalone/unregistered Vertex AI Reasoning Engines.
DEFAULT_RE_LOCATIONS = ('us-central1', 'us-west1', 'us-east4', 'europe-west1')
SYSTEM_K8S_NAMESPACES = frozenset({
    'kube-system', 'gmp-system', 'gke-managed-cim', 'gke-managed-system',
    'gke-managed-volumepopulator', 'istio-system', 'config-management-system', 'asm-system',
})
ALLOWED_WINDOWS_HOURS = (1, 6, 24, 168, 720, 2160, 4320, 8760)

AGENT_TYPE_LABELS = {
    'ADK': 'ADK agent on Vertex AI Agent Engine',
    'A2A': 'A2A agent',
    'MCP_CONNECTOR': 'BYO MCP Server Connector (Registered in GE)',
    'MANAGED': 'Google-managed agent',
    'LOW_CODE': 'No-code agent (Agent Designer)',
    'DIALOGFLOW': 'Dialogflow agent',
    'UNKNOWN': 'Agent',
    'ADK_STANDALONE': 'Standalone Vertex AI Agent Engine (Not in GE)',
    'MCP_SERVER': 'Cloud Run MCP Server (Not in GE)',
    'CLOUD_RUN_AGENT': 'Standalone Cloud Run Agent / App (Not in GE)',
    'SKILL_BACKEND': 'Cloud Run Skill / Tool Backend (Not in GE)',
    'GKE_WORKLOAD': 'GKE Agent / Inference Workload (Not in GE)',
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
  return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _iso(dt: datetime.datetime) -> str:
  return dt.isoformat().replace('+00:00', 'Z')


def _to_int(value: Any) -> int:
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return 0


# OTel GenAI cache-READ token attributes, in preference order. Only the first present key is used so
# alias keys emitted side by side are never summed, and cache-creation/write counts (which are billed
# differently and are not a subset of input) are never folded into "cached" tokens.
_CACHE_READ_KEYS = (
    'gen_ai.usage.cache_read.input_tokens',
    'gen_ai.usage.cache_read_input_tokens',
    'gen_ai.usage.cached_input_tokens',
    'gen_ai.usage.cached_tokens',
)


def _cache_read_tokens(labels: dict[str, Any]) -> int:
  """Returns cache-read input tokens from one span/log label set (0 when absent)."""
  for key in _CACHE_READ_KEYS:
    if labels.get(key) not in (None, ''):
      return _to_int(labels.get(key))
  return 0


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
  raw = os.environ.get(name, '')
  values = [v.strip() for v in raw.split(',') if v.strip()]
  return values or list(default)


MAX_WINDOW_HOURS = 8760  # 365 days (1 year)
MAX_ALIGNMENT_PERIOD_S = 30 * 24 * 3600  # 30 days: Cloud Monitoring alignmentPeriod cap


def _clamp_window(hours: Any, default: int) -> int:
  try:
    value = int(hours)
  except (TypeError, ValueError):
    return default
  return max(1, min(value, MAX_WINDOW_HOURS))


def _future_result_keys(fut: concurrent.futures.Future[Any]) -> set[str]:
  """Key set of a dict-valued future's result (empty when it resolves to None). Blocks on the future."""
  return set((fut.result() or {}).keys())


def parse_window_hours(value: Any) -> int | None:
  """Validates an untrusted window value: an integer number of hours in [1, 8760], else None."""
  if value is None or isinstance(value, bool):
    return None
  try:
    hours = int(str(value).strip())
  except (TypeError, ValueError):
    return None
  return hours if 1 <= hours <= MAX_WINDOW_HOURS else None


# Up to this window the trend bins raw per-minute points itself (exact); beyond it, server-side alignment.
RAW_SERIES_MAX_WINDOW_S = 7 * 24 * 3600


def trend_bucket_seconds(hours: int) -> int:
  """Bucket size for the requests-over-time chart (about 24–90 buckets per window)."""
  if hours <= 1:
    return 300
  if hours <= 6:
    return 900
  if hours <= 24:
    return 3600
  if hours <= 72:
    return 3 * 3600
  if hours <= 720:
    return 6 * 3600
  if hours <= 2160:
    return 24 * 3600
  if hours <= 4320:
    return 2 * 24 * 3600
  return 4 * 24 * 3600


def build_request_trend(
    agents: list[dict[str, Any]],
    results: dict[tuple[Any, ...], Any],
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
      'bucket_ends': [_iso(datetime.datetime.fromtimestamp(t, datetime.UTC)) for t in grid],
      'by_runtime': by_runtime,
      'status': status,
      'source': 'Cloud Monitoring request_count (Cloud Run + Agent Engine)',
  }


def parse_bool(value: Any) -> bool:
  """Parses an untrusted boolean flag ('1', 'true', 'yes', 'on' or a real bool)."""
  if isinstance(value, bool):
    return value
  return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def runtime_backend_key(agent: Mapping[str, Any]) -> str:
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
      st['cached_tokens'] += _cache_read_tokens(labels)
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


def aggregate_trace_usage_all(traces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  """Sums gen_ai token usage across ALL Agent Engines found in Cloud Trace spans (including unregistered)."""
  engine_ids: set[str] = set()
  agent_names_by_engine: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
  for trace in traces:
    spans = trace.get('spans') or []
    trace_engine = next((e for e in (_span_engine_id(sp.get('labels') or {}) for sp in spans) if e), None)
    for sp in spans:
      labels = sp.get('labels') or {}
      eid = _span_engine_id(labels) or trace_engine
      if eid:
        engine_ids.add(eid)
        name = str(sp.get('name') or '').strip()
        if name.startswith('invoke_agent '):
          aname = str(labels.get('gen_ai.agent.name') or name[len('invoke_agent '):]).strip()
          if aname:
            agent_names_by_engine[eid][aname] += 1
  stats = aggregate_trace_usage(traces, sorted(engine_ids))
  for eid, st in stats.items():
    st['agent_names'] = [n for n, _ in agent_names_by_engine[eid].most_common(3)]
  return stats


def _span_duration_ms(span: dict[str, Any]) -> float | None:
  start_s = span.get('startTime')
  end_s = span.get('endTime')
  if not start_s or not end_s:
    return None
  try:
    t0 = datetime.datetime.fromisoformat(str(start_s).replace('Z', '+00:00'))
    t1 = datetime.datetime.fromisoformat(str(end_s).replace('Z', '+00:00'))
    return max(0.0, (t1 - t0).total_seconds() * 1000.0)
  except ValueError:
    return None


def aggregate_trace_skills_and_mcp(
    traces: list[dict[str, Any]],
    registered_engine_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
  """Extracts Skills (execute_tool), Sub-Agents (invoke_agent), and MCP tool spans from Cloud Trace."""
  reg_ids = registered_engine_ids or set()
  buckets: dict[tuple[str, str], dict[str, Any]] = {}
  for trace in traces:
    spans = trace.get('spans') or []
    trace_engine = next((e for e in (_span_engine_id(sp.get('labels') or sp.get('attributes') or {}) for sp in spans) if e), None)
    # Sum trace-level token usage so we can attribute token amplification to tools/skills used in the trace.
    usage_spans = [
        sp for sp in spans
        if any(k in (sp.get('labels') or sp.get('attributes') or {}) for k in ('gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens'))
    ]
    wrappers = {sp.get('parentSpanId') for sp in usage_spans if sp.get('parentSpanId')}
    trace_in = 0
    trace_out = 0
    trace_models: set[str] = set()
    for usp in usage_spans:
      if usp.get('spanId') in wrappers:
        continue
      ulabels = usp.get('labels') or usp.get('attributes') or {}
      trace_in += _to_int(ulabels.get('gen_ai.usage.input_tokens'))
      trace_out += _to_int(ulabels.get('gen_ai.usage.output_tokens'))
      m = ulabels.get('gen_ai.response.model') or ulabels.get('gen_ai.request.model')
      if m:
        trace_models.add(str(m))

    seen_in_trace: set[tuple[str, str]] = set()
    for sp in spans:
      name = str(sp.get('name') or '').strip()
      labels = sp.get('labels') or sp.get('attributes') or {}
      engine = _span_engine_id(labels) or trace_engine
      tool_name = str(labels.get('gen_ai.tool.name') or '').strip()
      op_name = str(labels.get('gen_ai.operation.name') or '').strip()
      kind: str | None = None
      item_name: str | None = None
      if name.startswith('execute_tool ') or op_name == 'execute_tool' or tool_name:
        item_name = tool_name or (name[len('execute_tool '):].strip() if name.startswith('execute_tool ') else name)
        if item_name in ('transfer_to_agent', 'call_remote_agent'):
          kind = 'AGENT_HANDOFF_SKILL'
        elif 'mcp' in item_name.lower():
          kind = 'MCP_TOOL'
        else:
          kind = 'SKILL_TOOL'
      elif name.startswith('invoke_agent ') or op_name == 'invoke_agent':
        item_name = (
            str(labels.get('gen_ai.agent.name') or '').strip()
            or (name[len('invoke_agent '):].strip() if name.startswith('invoke_agent ') else name)
        )
        kind = 'SUB_AGENT'
      elif '/mcp' in name.lower() or '/mcp' in str(labels.get('http.route') or labels.get('http.url') or '').lower():
        item_name = name or 'mcp_endpoint'
        kind = 'MCP_TOOL'

      if not kind or not item_name:
        continue
      key = (kind, item_name)
      entry = buckets.setdefault(key, {
          'name': item_name,
          'kind': kind,
          'calls': 0,
          'traces_count': 0,
          'co_occurring_input_tokens': 0,
          'co_occurring_output_tokens': 0,
          'last_activity': None,
          '_latencies': [],
          '_engines': collections.Counter(),
          '_models': collections.Counter(),
      })
      entry['calls'] += 1
      dur = _span_duration_ms(sp)
      if dur is not None:
        entry['_latencies'].append(dur)
      if engine:
        entry['_engines'][engine] += 1
      for tm in trace_models:
        entry['_models'][tm] += 1
      stamp = sp.get('endTime')
      if stamp and (entry['last_activity'] is None or stamp > entry['last_activity']):
        entry['last_activity'] = stamp
      if key not in seen_in_trace:
        seen_in_trace.add(key)
        entry['traces_count'] += 1
        entry['co_occurring_input_tokens'] += trace_in
        entry['co_occurring_output_tokens'] += trace_out

  out: list[dict[str, Any]] = []
  for entry in buckets.values():
    lats = entry.pop('_latencies')
    eng_counter = entry.pop('_engines')
    mod_counter = entry.pop('_models')
    engines_list = [e for e, _ in eng_counter.most_common(5)]
    entry['avg_latency_ms'] = round(sum(lats) / len(lats), 1) if lats else None
    entry['engines'] = engines_list
    entry['models'] = [m for m, _ in mod_counter.most_common(3)]
    if engines_list:
      in_ge = sum(1 for e in engines_list if e in reg_ids)
      if in_ge == len(engines_list):
        entry['registration_scope'] = 'GE_REGISTERED'
      elif in_ge == 0:
        entry['registration_scope'] = 'UNREGISTERED_STANDALONE'
      else:
        entry['registration_scope'] = 'SHARED_GE_AND_STANDALONE'
    else:
      entry['registration_scope'] = 'PROJECT_WIDE'
    out.append(entry)
  out.sort(key=lambda r: (-r['calls'], r['name']))
  return out


def classify_cloud_run_service(raw_svc: dict[str, Any], default_project: str) -> dict[str, Any]:
  """Normalizes a Cloud Run v2 Service resource for FinOps inventory."""
  full_name = str(raw_svc.get('name') or (raw_svc.get('metadata') or {}).get('name') or '')
  parts = full_name.split('/')
  service_name = parts[-1] if parts else ''
  project = parts[1] if len(parts) >= 2 and parts[0] == 'projects' else default_project
  region = parts[3] if len(parts) >= 4 and parts[2] == 'locations' else 'us-central1'
  template = raw_svc.get('template') or (raw_svc.get('spec') or {}).get('template') or {}
  scaling = template.get('scaling') or {}
  min_inst = _to_int(scaling.get('minInstanceCount'))
  max_inst = _to_int(scaling.get('maxInstanceCount')) if scaling.get('maxInstanceCount') is not None else None
  containers = template.get('containers') or (template.get('spec') or {}).get('containers') or [{}]
  limits = ((containers[0] if containers else {}).get('resources') or {}).get('limits') or {}
  lower_name = service_name.lower()
  if 'mcp' in lower_name:
    category = 'MCP_SERVER'
  elif any(tok in lower_name for tok in (
      'agent', 'assistant', 'copilot', 'bot', 'orchestrator', 'proxy', 'ui-engine', 'ui-starter', 'ops-backend'
  )):
    category = 'CLOUD_RUN_AGENT'
  elif any(tok in lower_name for tok in ('skill', 'tool', 'function', 'plugin', 'action')):
    category = 'SKILL_BACKEND'
  else:
    category = 'CLOUD_RUN_SERVICE'
  return {
      'service_name': service_name,
      'resource_name': full_name,
      'project': project,
      'region': region,
      'uri': raw_svc.get('uri'),
      'ingress': raw_svc.get('ingress'),
      'created': raw_svc.get('createTime'),
      'updated': raw_svc.get('updateTime'),
      'min_instances': min_inst,
      'max_instances': max_inst,
      'cpu': limits.get('cpu'),
      'memory': limits.get('memory'),
      'type': category,
      'category': category,
      'category_label': AGENT_TYPE_LABELS.get(category, 'Cloud Run Service'),
  }


def _estimate_token_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    models: list[str] | None,
    rate_cards: dict[str, dict[str, float]],
) -> float | None:
  """List-price cost of a runtime's measured tokens; None when none of its models has a rate card.

  Uses the first of ``models`` with a card ('publishers/google/models/x' uses card 'x') and the same
  pricing as the per-user estimate (finops.tokens_cost_usd). There is no fallback price: a model
  without a card stays unpriced instead of being priced as some other model.
  """
  # OTel gen_ai.usage.input_tokens INCLUDES cache-read tokens, so the cached share is billed once at
  # the cached rate and only the remainder at the full input rate (never input + cached on top).
  if not (int(input_tokens or 0) or int(output_tokens or 0) or int(cached_tokens or 0)):
    return 0.0
  for m in models or []:
    _, card = live_finops.rate_card_for(m, rate_cards)
    if card is not None:
      return round(live_finops.tokens_cost_usd(input_tokens, output_tokens, cached_tokens, card), 4)
  return None


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
      st['cached_tokens'] += _cache_read_tokens(labels)
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
  try:
    base = _de_base(loc_match.group(1) if loc_match else 'global')
  except InvalidAgentRequestError:
    base = _de_base('global')
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


class InvalidAgentRequestError(ValueError):
  """Raised when a trace-logging request names a malformed agent, engine, project or location."""


# Discovery Engine locations are short lowercase IDs (global, us, eu, ...). They become part of an API
# hostname, so anything else is rejected before a URL is built.
_DE_LOCATION_RE = re.compile(r'^[a-z0-9-]{1,63}\Z')
_GOOGLE_API_HOST_RE = re.compile(r'^(?:[a-z0-9-]+\.)*googleapis\.com\Z')


def _is_google_api_url(url: str) -> bool:
  """True only for https URLs on *.googleapis.com (default port, no userinfo)."""
  try:
    parsed = urllib.parse.urlsplit(str(url))
    port = parsed.port
  except ValueError:
    return False
  return (
      parsed.scheme == 'https'
      and '@' not in parsed.netloc
      and port in (None, 443)
      and bool(_GOOGLE_API_HOST_RE.match(parsed.hostname or ''))
  )


def _de_base(location: str = 'global') -> str:
  """Returns the regional or global Discovery Engine v1alpha base URL."""
  loc = (location or 'global').strip().lower()
  if not _DE_LOCATION_RE.match(loc):
    raise InvalidAgentRequestError('Invalid Discovery Engine location.')
  if loc == 'global':
    return 'https://discoveryengine.googleapis.com/v1alpha'
  return f'https://{loc}-discoveryengine.googleapis.com/v1alpha'


def classify_agent(engine_id: str, assistant_id: str, raw: dict[str, Any]) -> dict[str, Any]:
  """Normalizes a Discovery Engine Agent resource into the fleet schema (no telemetry yet)."""
  kind = 'UNKNOWN'
  backend: dict[str, Any] = {'kind': 'unknown'}
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
  obs_raw = raw.get('observabilityConfig') or {}
  observability_config = {
      'observability_enabled': bool(obs_raw.get('observabilityEnabled')),
      'sensitive_logging_enabled': bool(obs_raw.get('sensitiveLoggingEnabled')),
  }
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
      'observability_config': observability_config,
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


def classify_mcp_connector(engine_id: str, collection_id: str, raw: dict[str, Any]) -> dict[str, Any]:
  """Normalizes a Gemini Enterprise BYO_MCP DataConnector resource into the fleet schema."""
  action_params = ((raw.get('actionConfig') or {}).get('actionParams') or {})
  params = raw.get('params') or {}
  url = str(action_params.get('instance_uri') or params.get('instance_uri') or '')
  run = cloud_run_service_from_url(url) if url else None
  enabled_actions = [str(a) for a in ((raw.get('bapConfig') or {}).get('enabledActions') or []) if a]
  dynamic_tools = [
      str(t.get('name'))
      for t in (raw.get('dynamicTools') or [])
      if isinstance(t, dict) and t.get('name')
  ]
  tools = list(dict.fromkeys(enabled_actions + dynamic_tools))
  backend: dict[str, Any] = {
      'kind': 'cloud_run' if run else 'external_endpoint',
      'url': url or None,
      'collection_id': collection_id,
      'data_source': raw.get('dataSource') or 'custom_mcp',
      'connector_state': raw.get('state'),
      'action_state': raw.get('actionState'),
      'mcp_tools': tools,
  }
  if run:
    backend.update(run)

  raw_state = str(raw.get('state') or 'STATE_UNSPECIFIED')
  state = 'ENABLED' if raw_state == 'ACTIVE' else raw_state
  tools_preview = f" [{', '.join(tools[:6])}{'...' if len(tools) > 6 else ''}]" if tools else ''
  default_desc = f"Registered BYO MCP Connector ({collection_id}) -> {url or 'external'}{tools_preview}"
  description = str(raw.get('description') or default_desc)
  return {
      'agent_id': f'mcp-{collection_id}',
      'resource_name': raw.get('name'),
      'display_name': raw.get('displayName') or collection_id,
      'description': description[:280] + ('...' if len(description) > 280 else ''),
      'engine_id': engine_id,
      'assistant_id': 'mcp_connector',
      'type': 'MCP_CONNECTOR',
      'type_label': AGENT_TYPE_LABELS['MCP_CONNECTOR'],
      'state': state,
      'sharing_scope': 'GE_MCP_CONNECTOR',
      'created': raw.get('createTime'),
      'updated': raw.get('updateTime'),
      'starter_prompts': len(tools),
      'backend': backend,
      'observability_config': {
          'observability_enabled': False,
          'sensitive_logging_enabled': False,
      },
      'telemetry_scope': 'service' if backend.get('kind') == 'cloud_run' else 'none',
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
      'data_sources': ['Discovery Engine DataConnector API (BYO_MCP)'],
      'notes': [],
  }


# Each segment allows only the characters Google resource IDs use: no '/', '?', '#', '@', quotes or
# whitespace, so a resource name can neither change the API host nor break out of the shell snippets.
_GE_AGENT_RESOURCE_RE = re.compile(
    r'^projects/(?P<project>[A-Za-z0-9][A-Za-z0-9.:_-]{0,127})/locations/(?P<location>[a-z0-9-]{1,63})/'
    r'collections/(?P<collection>[A-Za-z0-9_-]{1,128})/engines/(?P<engine>[A-Za-z0-9_-]{1,128})/'
    r'assistants/(?P<assistant>[A-Za-z0-9_-]{1,128})/agents/(?P<agent>[A-Za-z0-9_-]{1,128})\Z'
)
_PROJECT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.:_-]{0,127}\Z')
_RESOURCE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}\Z')
_ENGINE_SPEC_RE = re.compile(r'^(?:[a-z0-9-]{1,63}/)?[A-Za-z0-9_-]{1,128}\Z')
# Concurrent observabilityConfig PATCHes for bulk enable/disable (each call has its own HTTP timeout).
_TRACE_PATCH_WORKERS = 8


def _or_placeholder(value: Any, pattern: re.Pattern[str], placeholder: str) -> str:
  """Returns value if it fully matches pattern, else the placeholder (for shell snippet fields)."""
  text = str(value or '')
  return text if pattern.match(text) else placeholder


def _trace_error_message(exc: FleetSourceError) -> str:
  """API error text for a failed observabilityConfig PATCH, naming the missing permission on 403."""
  text = str(exc)
  if exc.status == 403:
    text += ' (the runtime service account needs discoveryengine.agents.update)'
  return text[:400]


def build_enable_agent_logging_script(
    project_id: str,
    location: str = 'global',
    collection: str = 'default_collection',
    engine_id: str = '<ENGINE_ID>',
    agent_resource: str | None = None,
    agent_resource_name: str | None = None,
    only_low_code: bool = False,
    enabled: bool = True,
) -> dict[str, str]:
  """Builds the enable/disable_agent_logging.sh bulk script and single-agent PATCH curl command.

  Raises InvalidAgentRequestError if any value that would be written into the shell text is malformed.
  """
  proj = project_id or '<PROJECT_ID>'
  loc = (location or 'global').strip().lower()
  coll = collection or 'default_collection'
  eng = engine_id or '<ENGINE_ID>'
  target_res = (agent_resource or agent_resource_name or '').strip() or None
  if proj != '<PROJECT_ID>' and not _PROJECT_ID_RE.match(proj):
    raise InvalidAgentRequestError('Invalid Google Cloud project ID.')
  if not _RESOURCE_ID_RE.match(coll):
    raise InvalidAgentRequestError('Invalid Discovery Engine collection ID.')
  if eng != '<ENGINE_ID>' and not _RESOURCE_ID_RE.match(eng):
    raise InvalidAgentRequestError('Invalid Gemini Enterprise engine ID.')
  if target_res and not _GE_AGENT_RESOURCE_RE.match(target_res):
    raise InvalidAgentRequestError('Invalid Gemini Enterprise agent resource name.')
  base = _de_base(loc)
  bool_str = 'true' if enabled else 'false'
  action_verb = 'Enables' if enabled else 'Disables'
  action_ing = 'Enabling' if enabled else 'Disabling'
  script_name = 'enable_agent_logging.sh' if enabled else 'disable_agent_logging.sh'
  single_target = target_res or (
      f'projects/{proj}/locations/{loc}/collections/{coll}/engines/{eng}/assistants/default_assistant/agents/<AGENT_ID>'
  )
  jq_expr = (
      '.agents[]? | select(.lowCodeAgentDefinition != null or .workflowAgentDefinition != null) | .name // empty'
      if only_low_code
      else '.agents[].name // empty'
  )
  curl_cmd = (
      'curl -s -X PATCH '
      '-H "Authorization: Bearer $(gcloud auth print-access-token)" '
      f'-H "X-Goog-User-Project: {proj}" '
      '-H "Content-Type: application/json" '
      f'"{base}/{single_target}?updateMask=observabilityConfig" '
      f'-d \'{{"observabilityConfig":{{"observabilityEnabled":{bool_str},"sensitiveLoggingEnabled":{bool_str}}}}}\''
  )
  script = f"""#!/bin/bash
# -------------------------------------------------------------
# Script: {script_name}
# Purpose: {action_verb} trace logging across all registered agents
# -------------------------------------------------------------

set -e

# Configuration
PROJECT_ID="{proj}"
LOCATION="{loc}"
COLLECTION="{coll}"
ENGINE_ID="{eng}"

echo "Fetching active agents for Engine: ${{ENGINE_ID}}..."
ACCESS_TOKEN=$(gcloud auth print-access-token)

# 1. Retrieve list of registered agents
RESPONSE=$(curl -s -X GET \\
  -H "Authorization: Bearer ${{ACCESS_TOKEN}}" \\
  -H "X-Goog-User-Project: ${{PROJECT_ID}}" \\
  "{base}/projects/${{PROJECT_ID}}/locations/${{LOCATION}}/collections/${{COLLECTION}}/engines/${{ENGINE_ID}}/assistants/default_assistant/agents")

# Extract agent resource paths
AGENTS=$(echo "$RESPONSE" | jq -r '{jq_expr}')

if [ -z "$AGENTS" ]; then
  echo "⚠️ No agents found under 'default_assistant'. Raw response:"
  echo "$RESPONSE"
  exit 0
fi

# 2. Iterate and patch observabilityConfig on each agent
for AGENT in $AGENTS; do
  echo "--------------------------------------------------------"
  echo "{action_ing} trace logging for: ${{AGENT}}"

  RESULT=$(curl -s -X PATCH \\
    -H "Authorization: Bearer ${{ACCESS_TOKEN}}" \\
    -H "X-Goog-User-Project: ${{PROJECT_ID}}" \\
    -H "Content-Type: application/json" \\
    "{base}/${{AGENT}}?updateMask=observabilityConfig" \\
    -d '{{
      "observabilityConfig": {{
        "observabilityEnabled": {bool_str},
        "sensitiveLoggingEnabled": {bool_str}
      }}
    }}')

  echo "Status: $(echo "$RESULT" | jq -r '.observabilityConfig // .error.message')"
done
echo "--------------------------------------------------------"
echo "✅ All agents in ${{ENGINE_ID}} updated successfully (observabilityEnabled={bool_str})."
"""
  return {'script': script, 'curl_command': curl_cmd, 'script_name': script_name}


class _GoogleApi:
  """Minimal authenticated JSON client for Google REST APIs using ADC or gcloud CLI fallback."""

  def __init__(self, quota_project: str):
    self._quota_project = quota_project
    self._credentials: Any = None
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
    if not _is_google_api_url(url):
      # Never attach the runtime service account's bearer token to a non-Google host.
      raise FleetSourceError(0, 'Refusing to send credentials to a non-Google API URL')
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
      discover_unregistered: bool | None = None,
  ):
    self.project_id = project_id or gcp_telemetry.get_current_gcp_project()
    self.engine_ids = engine_ids or _env_list('VIBELIFT_GE_ENGINES', ('auto',))
    self.discovery_locations = _env_list('VIBELIFT_GE_LOCATIONS', DEFAULT_DISCOVERY_LOCATIONS)
    self.reasoning_engine_locations = _env_list('VIBELIFT_RE_LOCATIONS', DEFAULT_RE_LOCATIONS)
    self.location = location or os.environ.get('VIBELIFT_GE_LOCATION', 'global')
    self.collection = collection or os.environ.get('VIBELIFT_GE_COLLECTION', 'default_collection')
    if discover_unregistered is None:
      default_unreg = self.project_id not in ('test-project', gcp_telemetry.UNCONFIGURED_PROJECT_ID)
      self.discover_unregistered = parse_bool(
          os.environ.get('VIBELIFT_DISCOVER_UNREGISTERED', 'true' if default_unreg else 'false')
      )
    else:
      self.discover_unregistered = bool(discover_unregistered)
    self.ttl_seconds = float(os.environ.get('VIBELIFT_FLEET_TTL_SECONDS', '60'))
    # Forced refreshes inside this interval are served from cache (protects API quotas).
    self.min_refresh_interval_s = float(os.environ.get('VIBELIFT_FLEET_MIN_REFRESH_SECONDS', '10'))
    self.max_trace_pages = int(os.environ.get('VIBELIFT_TRACE_MAX_PAGES', '6'))
    self.default_window_hours = _clamp_window(os.environ.get('VIBELIFT_FLEET_WINDOW_HOURS', '24'), 24)
    self.max_log_entries = int(os.environ.get('VIBELIFT_TOKEN_LOG_MAX_ENTRIES', '3000'))
    self._api = api or _GoogleApi(quota_project=self.project_id)
    self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
    self._project_aliases: set[str] = {self.project_id}
    self._trace_cache: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]], bool]] = {}
    self._log_cache: dict[tuple[str, tuple[str, ...], int], tuple[float, dict[str, Any]]] = {}
    self._cache_lock = threading.Lock()
    self._refresh_lock = threading.Lock()
    self._inflight_events: dict[int, threading.Event] = {}

  def _record_project_alias(self, resource_name: str | None) -> None:
    """Records a GCP project number/ID discovered from resources belonging to self.project_id."""
    if not resource_name:
      return
    match = re.search(r'(?:^|/)projects/([^/]+)/', str(resource_name))
    if match and match.group(1):
      with self._cache_lock:
        self._project_aliases.add(match.group(1))

  def _canonical_project(self, project: str | None) -> str:
    """Normalizes a project number or alias back to self.project_id so metrics/traces are queried once."""
    aliases = getattr(self, '_project_aliases', None) or {self.project_id}
    if not project or project in aliases:
      return self.project_id
    return project

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
    if cached is not None and usable(cached):
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
        fallback = self._fallback_cache(hours)
        if fallback is not None:
          return fallback
        return self._warming_placeholder(hours)

    return self._refresh_sync(hours, force_refresh=force_refresh)

  def _list_trace_inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Lists the Gemini Enterprise apps and their agents directly (no telemetry), plus listing errors."""
    errors: list[dict[str, str]] = []
    engines: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    for spec in self._resolve_engine_specs(errors):
      try:
        meta, engine_agents = self._list_engine_agents(spec)
      except FleetSourceError as exc:
        errors.append({'source': f'Discovery Engine agents ({spec})', 'detail': str(exc)})
        continue
      engines.append(meta)
      agents.extend(engine_agents)
    return engines, agents, [str(e.get('detail') or e.get('source')) for e in errors]

  def enable_agent_observability(
      self,
      resource_name: str | None = None,
      engine_id: str | None = None,
      location: str | None = None,
      enabled: bool = True,
      sensitive_logging: bool | None = None,
      only_low_code: bool = False,
  ) -> dict[str, Any]:
    """Enables or disables Discovery Engine trace logging (observabilityConfig) on one or all registered GE agents.

    All request inputs are validated before any API call (InvalidAgentRequestError). PATCHes run in
    parallel, and only agents the API confirmed are marked as updated in the fleet cache.
    """
    obs_enabled = bool(enabled)
    sens_enabled = obs_enabled if sensitive_logging is None else (bool(sensitive_logging) and obs_enabled)
    res_clean = str(resource_name or '').strip() or None
    engine_filter = str(engine_id or '').strip() or None
    loc_arg = str(location or '').strip().lower() or None
    res_match = _GE_AGENT_RESOURCE_RE.match(res_clean) if res_clean else None
    if res_clean and not res_match:
      raise InvalidAgentRequestError('Invalid Gemini Enterprise agent resource name.')
    if engine_filter and not _ENGINE_SPEC_RE.match(engine_filter):
      raise InvalidAgentRequestError('Invalid Gemini Enterprise engine ID.')
    if loc_arg and not _DE_LOCATION_RE.match(loc_arg):
      raise InvalidAgentRequestError('Invalid Discovery Engine location.')

    fleet_snapshot = self.collect(allow_stale=True, max_wait_s=5.0)
    agents = list(fleet_snapshot.get('agents') or [])
    engines = list(fleet_snapshot.get('engines') or [])
    listing_errors: list[str] = []
    if not res_match and (fleet_snapshot.get('source_status') or {}).get('inventory') == 'warming':
      # Cold cache (e.g. a new instance still collecting telemetry): list the agents directly instead
      # of reporting that there are none.
      engines, agents, listing_errors = self._list_trace_inventory()
    first_engine = engines[0] if engines else {}

    targets: list[dict[str, Any]] = []
    if res_match:
      script_loc, script_engine = res_match['location'], res_match['engine']
      existing = next((a for a in agents if a.get('resource_name') == res_clean), None)
      targets.append(existing or {
          'agent_id': res_match['agent'],
          'display_name': res_match['agent'],
          'resource_name': res_clean,
          'engine_id': res_match['engine'],
          'location': res_match['location'],
          'type': None,  # Not in the current snapshot, so the agent type is unknown.
      })
    else:
      default_loc = loc_arg or str(first_engine.get('location') or self.location or 'global')
      script_loc, script_engine = _parse_engine_spec(
          engine_filter or str(first_engine.get('engine_id') or '<ENGINE_ID>'), default_loc)
      for a in agents:
        res_name = str(a.get('resource_name') or '')
        if not _GE_AGENT_RESOURCE_RE.match(res_name):
          continue
        if engine_filter and a.get('engine_id') != engine_filter and f"{a.get('location')}/{a.get('engine_id')}" != engine_filter:
          continue
        if only_low_code and a.get('type') != 'LOW_CODE':
          continue
        targets.append(a)

    # Built before any PATCH. Request inputs were validated above; snapshot/config values that do not
    # fit the shell-safe patterns fall back to placeholders instead of failing the request.
    snippets = build_enable_agent_logging_script(
        project_id=_or_placeholder(self.project_id, _PROJECT_ID_RE, '<PROJECT_ID>'),
        location=_or_placeholder(str(script_loc).lower(), _DE_LOCATION_RE, 'global'),
        collection=_or_placeholder(self.collection, _RESOURCE_ID_RE, 'default_collection'),
        engine_id=_or_placeholder(script_engine, _RESOURCE_ID_RE, '<ENGINE_ID>'),
        agent_resource=res_clean or (
            str(targets[0].get('resource_name')) if len(targets) == 1 else None),
        only_low_code=only_low_code,
        enabled=obs_enabled,
    )

    patch_body = {
        'observabilityConfig': {
            'observabilityEnabled': obs_enabled,
            'sensitiveLoggingEnabled': sens_enabled,
        }
    }

    def _patch(target: dict[str, Any]) -> dict[str, Any]:
      res_name = str(target.get('resource_name') or '')
      entry: dict[str, Any] = {
          'agent_id': target.get('agent_id'),
          'display_name': target.get('display_name') or target.get('agent_id'),
          'resource_name': res_name,
          'type': target.get('type'),
      }
      match = _GE_AGENT_RESOURCE_RE.match(res_name)
      try:
        if not match:
          raise FleetSourceError(0, 'Invalid agent resource name')
        resp = self._api.call(
            'PATCH', f"{_de_base(match['location'])}/{res_name}?updateMask=observabilityConfig", patch_body)
      except FleetSourceError as exc:
        previous = target.get('observability_config')
        entry.update({
            'status': 'ERROR',
            'error': _trace_error_message(exc),
            'http_status': exc.status or None,
            # Nothing changed, so report the agent's last known state.
            'observability_config': dict(previous) if isinstance(previous, Mapping) else None,
        })
        return entry
      returned = (resp or {}).get('observabilityConfig')
      cfg = returned if isinstance(returned, Mapping) else patch_body['observabilityConfig']
      entry.update({
          'status': 'OK',
          'observability_config': {
              # Proto3 JSON omits false fields, so a missing key means false.
              'observability_enabled': bool(cfg.get('observabilityEnabled', False)),
              'sensitive_logging_enabled': bool(cfg.get('sensitiveLoggingEnabled', False)),
          },
      })
      return entry

    results: list[dict[str, Any]] = []
    if len(targets) == 1:
      results = [_patch(targets[0])]
    elif targets:
      with concurrent.futures.ThreadPoolExecutor(
          max_workers=min(_TRACE_PATCH_WORKERS, len(targets)), thread_name_prefix='vibelift-trace') as pool:
        results = list(pool.map(_patch, targets))

    # Only agents the API confirmed change in the cache, so the next refresh cannot show a state that
    # was never applied.
    confirmed = {r['resource_name']: r['observability_config'] for r in results if r['status'] == 'OK'}
    if confirmed:
      with self._cache_lock:
        for _, (_, cached_payload) in self._cache.items():
          for cached_agent in cached_payload.get('agents') or []:
            cfg_now = confirmed.get(str(cached_agent.get('resource_name') or ''))
            if cfg_now is not None:
              cached_agent['observability_config'] = dict(cfg_now)

    updated_count = sum(1 for r in results if r['status'] == 'OK')
    failed = [r for r in results if r['status'] != 'OK']
    verb = 'enabled' if obs_enabled else 'disabled'
    action = 'enable' if obs_enabled else 'disable'
    if not targets and listing_errors:
      status = 'FAILED'
      message = f'Could not list the Gemini Enterprise agents: {listing_errors[0]}'[:400]
    elif not targets:
      status = 'NO_AGENTS_MATCHED'
      message = ('No No-Code / Low-Code agents found to update.' if only_low_code
                 else 'No registered agents found to update.')
    elif not failed:
      status = 'OK'
      message = (f"Trace logging {verb} for {results[0]['display_name']}." if len(results) == 1
                 else f'Trace logging {verb} on {updated_count} agents.')
    elif updated_count:
      status = 'PARTIAL'
      message = (f'Trace logging {verb} on {updated_count} of {len(results)} agents; {len(failed)} failed '
                 f"({failed[0]['display_name']}: {failed[0]['error']}).")
    else:
      status = 'FAILED'
      message = (f"Could not {action} trace logging for {failed[0]['display_name']}: {failed[0]['error']}"
                 if len(failed) == 1 else
                 f"Could not {action} trace logging on any of {len(failed)} agents ({failed[0]['error']}).")
    if targets and listing_errors:
      # Agents on apps that could not be listed were never attempted.
      status = 'PARTIAL' if status == 'OK' else status
      message += f' {len(listing_errors)} app(s) could not be listed ({listing_errors[0]}).'
    logger.info(
        'Trace logging %s: status=%s targeted=%d updated=%d failed=%d resource=%s only_low_code=%s',
        verb, status, len(targets), updated_count, len(failed), res_clean or '-', bool(only_low_code))
    return {
        'status': status,
        'message': message,
        'action': 'ENABLED' if obs_enabled else 'DISABLED',
        'enabled': obs_enabled,
        'sensitive_logging': sens_enabled,
        'project_id': self.project_id,
        'location': script_loc,
        'collection': self.collection,
        'engine_id': script_engine,
        'only_low_code': bool(only_low_code),
        'targeted_count': len(targets),
        'total_targeted': len(targets),
        'updated_count': updated_count,
        'failed_count': len(failed),
        'updated_at': _iso(_utcnow()),
        'results': results,
        'script_name': snippets['script_name'],
        'script': snippets['script'],
        'curl_command': snippets['curl_command'],
    }

  # ------------------------------------------------------------------ internals

  def _refresh_sync(self, hours: int, force_refresh: bool = False) -> dict[str, Any]:
    with self._refresh_lock:
      cached = self._fresh_cache(hours)
      if cached is not None and (
          not force_refresh or cached['cache_age_seconds'] < self.min_refresh_interval_s
      ):
        return cached
      if force_refresh:
        with self._cache_lock:
          self._trace_cache.clear()
          self._log_cache.clear()
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
    if entry is None:
      return None
    age = time.monotonic() - entry[0]
    return dict(entry[1], cache_age_seconds=round(age, 1))

  def _fallback_cache(self, hours: int) -> dict[str, Any] | None:
    """Falls back to the most recently collected window if the requested window timed out warming, preserving window_hours."""
    with self._cache_lock:
      if not self._cache:
        return None
      entry = max(self._cache.values(), key=lambda item: item[0])
    age = time.monotonic() - entry[0]
    source_hours = entry[1].get('window_hours')
    out = dict(entry[1], window_hours=hours, cache_age_seconds=round(age, 1))
    if source_hours is not None and int(source_hours) != int(hours):
      out['warming'] = True
      out['fallback_from_window_hours'] = int(source_hours)
      notes = list(out.get('notes') or [])
      notes.append(
          f'Warming {hours}h telemetry in the background (currently showing cached {source_hours}h snapshot).'
      )
      out['notes'] = notes
    return out

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
        'unregistered_runtimes': [],
        'gke_workloads': {'clusters': [], 'workloads': []},
        'skills_and_mcp': [],
        'unregistered_summary': {
            'total_unregistered_runtimes': 0,
            'unregistered_reasoning_engines': 0,
            'active_unregistered_reasoning_engines': 0,
            'zombie_reasoning_engines': 0,
            'zombie_runtimes_count': 0,
            'unregistered_cloud_run_services': 0,
            'mcp_cloud_run_services': 0,
            'always_on_cloud_run_services': 0,
            'gke_clusters_count': 0,
            'gke_workloads_count': 0,
            'discovered_skills_and_mcp_count': 0,
            'unregistered_requests': 0,
            'unregistered_llm_calls': 0,
            'unregistered_input_tokens': 0,
            'unregistered_output_tokens': 0,
            'unregistered_cached_tokens': 0,
            'unregistered_est_token_cost_usd': 0.0,
            'unregistered_unpriced_runtimes': 0,
            'zombie_vcpu_hours': 0.0,
            'zombie_memory_gib_hours': 0.0,
            'unregistered_vcpu_hours': 0.0,
            'unregistered_memory_gib_hours': 0.0,
            'unregistered_billable_instance_hours': 0.0,
            'gke_cpu_core_hours': 0.0,
            'gke_memory_gib': 0.0,
            'discovery_status': 'warming',
        },
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

  def _de_collection_base(self, location: str | None = None, collection_id: str | None = None) -> str:
    loc = location or self.location
    coll = collection_id or self.collection
    host = 'discoveryengine.googleapis.com'
    if loc != 'global':
      host = f'{loc}-{host}'
    return f'https://{host}/v1alpha/projects/{self.project_id}/locations/{loc}/collections/{coll}'

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
    if not specs:
      # Never fall back to hard-coded engine IDs: they belong to another project.
      errors.append({
          'source': 'Discovery Engine app discovery',
          'detail': ('No Gemini Enterprise apps found in locations '
                     f'{", ".join(self.discovery_locations)}. Set VIBELIFT_GE_ENGINES to '
                     'location/engine_id values, or check that the runtime service account '
                     'has discoveryengine.engines.list.'),
      })
    return specs

  def _list_engine_agents(self, engine_spec: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if '/' in engine_spec:
      loc, engine_id = engine_spec.split('/', 1)
    else:
      loc, engine_id = self.location, engine_spec
    base = f'{self._de_base(loc)}/{engine_id}'
    engine = self._api.call('GET', base)
    self._record_project_alias(engine.get('name'))
    try:
      assistants = [a['name'].rsplit('/', 1)[-1] for a in self._paged(f'{base}/assistants?pageSize=100', 'assistants')]
    except FleetSourceError:
      assistants = []
    agents = []
    display = engine.get('displayName') or engine_id
    for assistant_id in assistants or ['default_assistant']:
      for raw in self._paged(f'{base}/assistants/{assistant_id}/agents?pageSize=100', 'agents'):
        self._record_project_alias(raw.get('name'))
        agent = classify_agent(engine_id, assistant_id, raw)
        agent['location'] = loc
        agent['engine_key'] = f'{loc}/{engine_id}'
        agent['engine_display_name'] = display
        agents.append(agent)
    for ds_id in engine.get('dataStoreIds') or []:
      ds_str = str(ds_id or '').strip()
      if not ds_str.endswith('_mcp_data'):
        continue
      cid = ds_str[:-len('_mcp_data')]
      if not cid or not _RESOURCE_ID_RE.match(cid):
        continue
      try:
        raw_conn = self._api.call('GET', f'{self._de_collection_base(loc, cid)}/dataConnector')
      except FleetSourceError:
        continue
      if not isinstance(raw_conn, dict) or not raw_conn:
        continue
      self._record_project_alias(raw_conn.get('name'))
      conn_agent = classify_mcp_connector(engine_id, cid, raw_conn)
      conn_agent['location'] = loc
      conn_agent['engine_key'] = f'{loc}/{engine_id}'
      conn_agent['engine_display_name'] = display
      agents.append(conn_agent)
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
    align_s = min(window_s, MAX_ALIGNMENT_PERIOD_S)
    params = [
        ('filter', metric_filter),
        ('interval.startTime', _iso(end - datetime.timedelta(seconds=window_s))),
        ('interval.endTime', _iso(end)),
        ('aggregation.alignmentPeriod', f'{align_s}s'),
        ('aggregation.perSeriesAligner', aligner),
        ('aggregation.crossSeriesReducer', reducer),
    ] + [('aggregation.groupByFields', field) for field in group_by]
    url = f'{_MONITORING}/projects/{project}/timeSeries?{urllib.parse.urlencode(params)}'
    rows = []
    for series in self._paged(url, 'timeSeries'):
      points = series.get('points') or []
      if not points:
        continue
      nums: list[float] = []
      for pt in points:
        value = pt.get('value', {})
        number = value.get('int64Value', value.get('doubleValue'))
        if number is None and 'distributionValue' in value:
          number = value['distributionValue'].get('mean')
        if number is not None:
          nums.append(float(number))
      if not nums:
        continue
      agg_val = (
          sum(nums)
          if (aligner == 'ALIGN_SUM' or (aligner == 'ALIGN_DELTA' and reducer == 'REDUCE_SUM'))
          else (sum(nums) / len(nums))
      )
      labels = dict(series.get('resource', {}).get('labels', {}))
      labels.update(series.get('metric', {}).get('labels', {}))
      rows.append((labels, float(agg_val)))
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
      count = round(value)
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
        count = round(float(val.get('int64Value', val.get('doubleValue', 0)) or 0))
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
    log_window_s = min(window_s, 30 * 24 * 3600)  # Cloud Logging _Default retains 30 days max
    cache_key = (project, tuple(sorted(set(engine_ids))), log_window_s)
    log_cache = getattr(self, '_log_cache', None)
    if log_cache is not None and self.ttl_seconds > 0:
      with self._cache_lock:
        cached = log_cache.get(cache_key)
      if cached is not None and (time.monotonic() - cached[0]) < self.ttl_seconds:
        return json.loads(json.dumps(cached[1]))
    start = _iso(_utcnow() - datetime.timedelta(seconds=log_window_s))
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
        s['cached_tokens'] += _cache_read_tokens(labels)
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
    out = {'by_engine': stats, 'entries_scanned': scanned, 'truncated': truncated}
    if log_cache is not None and self.ttl_seconds > 0:
      with self._cache_lock:
        log_cache[cache_key] = (time.monotonic(), json.loads(json.dumps(out)))
    return out

  def _trace_usage(self, project: str, engine_ids: list[str], window_s: int) -> dict[str, Any]:
    """Per-engine gen_ai token usage from Cloud Trace spans exported by Agent Engine telemetry."""
    traces, truncated = self._list_traces(project, '+cloud.platform:gcp.agent_engine', window_s)
    return {
        'by_engine': aggregate_trace_usage(traces, engine_ids),
        'all_engines': aggregate_trace_usage_all(traces),
        'skills_and_mcp': aggregate_trace_skills_and_mcp(traces, set(engine_ids)),
        'traces_scanned': len(traces),
        'truncated': truncated,
    }

  def _ge_assistant_usage(self, window_s: int) -> dict[str, Any]:
    """Gemini Enterprise assistant token usage per app, from Cloud Trace (gcp.gemini_enterprise spans)."""
    traces, truncated = self._list_traces(self.project_id, '+cloud.platform:gcp.gemini_enterprise', window_s)
    out = aggregate_ge_assistant_usage(traces)
    out.update({
        'traces_scanned': len(traces), 'truncated': truncated,
        'scope': f'Gemini Enterprise assistant model calls exported to Cloud Trace in {self.project_id}',
    })
    return out

  @staticmethod
  def _filter_traces_by_start(traces: list[dict[str, Any]], start_iso: str) -> list[dict[str, Any]]:
    """Filters a cached trace list down to traces with span activity at or after start_iso."""
    filtered: list[dict[str, Any]] = []
    for tr in traces:
      spans = tr.get('spans') or []
      if not spans:
        filtered.append(tr)
        continue
      stamps = [
          str(sp.get('endTime') or sp.get('startTime') or '')
          for sp in spans
          if sp.get('endTime') or sp.get('startTime')
      ]
      if not stamps or max(stamps) >= start_iso:
        filtered.append(tr)
    return filtered

  def _list_traces(self, project: str, trace_filter: str, window_s: int) -> tuple[list[dict[str, Any]], bool]:
    end = _utcnow()
    trace_window_s = min(window_s, 30 * 24 * 3600)  # Cloud Trace retains 30 days max
    start_iso = _iso(end - datetime.timedelta(seconds=trace_window_s))
    trace_cache = getattr(self, '_trace_cache', None)
    now_mono = time.monotonic()
    if trace_cache is not None and self.ttl_seconds > 0:
      with self._cache_lock:
        exact = trace_cache.get((project, trace_filter, trace_window_s))
        if exact is not None and (now_mono - exact[0]) < self.ttl_seconds:
          return list(exact[1]), exact[2]
        # Reuse a fresh, non-truncated superset window (e.g., 7d or 30d) for smaller windows without extra API calls.
        for (c_proj, c_filt, c_win_s), (c_ts, c_traces, c_trunc) in trace_cache.items():
          if (
              c_proj == project
              and c_filt == trace_filter
              and c_win_s >= trace_window_s
              and not c_trunc
              and (now_mono - c_ts) < self.ttl_seconds
          ):
            return self._filter_traces_by_start(c_traces, start_iso), False

    params = {
        'startTime': start_iso,
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
      url = f'{_TRACE}/projects/{project}/traces?{urllib.parse.urlencode(query)}'
      page = None
      last_exc: FleetSourceError | None = None
      for attempt in range(3):
        try:
          page = self._api.call('GET', url)
          last_exc = None
          break
        except FleetSourceError as exc:
          last_exc = exc
          if exc.status == 429 and attempt < 2:
            time.sleep(0.6 * (attempt + 1))
            continue
          break
      if last_exc is not None:
        if last_exc.status == 429:
          if traces:
            truncated = True
            break
          if trace_cache is not None:
            with self._cache_lock:
              candidates = [
                  (c_win_s, c_traces, c_trunc)
                  for (c_proj, c_filt, c_win_s), (_, c_traces, c_trunc) in trace_cache.items()
                  if c_proj == project and c_filt == trace_filter
              ]
            if candidates:
              best_win_s, best_traces, best_trunc = max(candidates, key=lambda item: item[0])
              return self._filter_traces_by_start(best_traces, start_iso), (best_trunc or best_win_s < trace_window_s)
        raise last_exc
      traces.extend((page or {}).get('traces', []))
      token = (page or {}).get('nextPageToken', '')
      if not token:
        break
      if page_no == self.max_trace_pages - 1:
        truncated = True
    if trace_cache is not None and self.ttl_seconds > 0:
      with self._cache_lock:
        trace_cache[(project, trace_filter, trace_window_s)] = (time.monotonic(), list(traces), truncated)
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

  def _cloud_run_services_catalog(self, project: str) -> dict[str, dict[str, Any]]:
    """Catalog of every Cloud Run service in the project (all regions). Raises if the list is incomplete."""
    catalog: dict[str, dict[str, Any]] = {}
    token = ''
    for _ in range(_MAX_PAGES):
      url = f'https://run.googleapis.com/v2/projects/{project}/locations/-/services?pageSize=500'
      if token:
        url += f'&pageToken={urllib.parse.quote(token)}'
      page = self._api.call('GET', url)
      for svc in page.get('services', []):
        if project == self.project_id:
          self._record_project_alias(svc.get('name'))
        info = classify_cloud_run_service(svc, project)
        if info['service_name']:
          catalog[info['service_name']] = info
      token = page.get('nextPageToken', '')
      if not token:
        return catalog
    raise FleetSourceError(0, 'Cloud Run service list exceeded the page limit; existence not verified.')

  def _cloud_run_services(self, project: str) -> set[str]:
    """Names of every Cloud Run service in the project (all regions). Raises if the list is incomplete."""
    return set(self._cloud_run_services_catalog(project).keys())

  def _discover_reasoning_engines(self, project: str) -> list[dict[str, Any]]:
    """Lists every Vertex AI Agent Engine (ReasoningEngine) across configured regional endpoints."""
    discovered: list[dict[str, Any]] = []
    for loc in self.reasoning_engine_locations:
      url = f'{_AIPLATFORM.format(location=loc)}/projects/{project}/locations/{loc}/reasoningEngines?pageSize=100'
      try:
        items = self._paged(url, 'reasoningEngines')
      except FleetSourceError:
        continue
      for raw in items:
        resource = str(raw.get('name') or '')
        if project == self.project_id:
          self._record_project_alias(resource)
        match = _REASONING_ENGINE_RE.match(resource)
        rid = match['id'] if match else resource.rsplit('/', 1)[-1]
        if not rid:
          continue
        spec = raw.get('spec') or {}
        discovered.append({
            'reasoning_engine_id': rid,
            'resource': resource or f'projects/{project}/locations/{loc}/reasoningEngines/{rid}',
            'project': match['project'] if match else project,
            'location': match['location'] if match else loc,
            'display_name': raw.get('displayName') or f'ReasoningEngine {rid}',
            'description': str(raw.get('description') or '')[:280],
            'framework': spec.get('agentFramework') or 'google-adk',
            'created': raw.get('createTime'),
            'updated': raw.get('updateTime'),
        })
    return discovered

  def _discover_gke_workloads(self, project: str, window_s: int) -> dict[str, Any]:
    """Discovers GKE clusters and non-system agent/inference container workloads via GKE & Cloud Monitoring APIs."""
    clusters_resp = self._api.call('GET', f'{_GKE}/projects/{project}/locations/-/clusters')
    clusters: list[dict[str, Any]] = []
    cluster_by_name: dict[str, dict[str, Any]] = {}
    for c in clusters_resp.get('clusters') or []:
      cname = str(c.get('name') or '')
      if not cname:
        continue
      autopilot = bool((c.get('autopilot') or {}).get('enabled'))
      c_obj = {
          'name': cname,
          'location': c.get('location') or 'us-central1',
          'status': c.get('status') or 'RUNNING',
          'autopilot': autopilot,
          'mode': 'Autopilot' if autopilot else 'Standard',
          'node_count': _to_int(c.get('currentNodeCount')),
          'version': c.get('currentMasterVersion'),
          'endpoint': c.get('endpoint'),
          'created': c.get('createTime'),
          'user_cpu_core_hours': 0.0,
          'system_cpu_core_hours': 0.0,
          'user_memory_gib': 0.0,
      }
      clusters.append(c_obj)
      cluster_by_name[cname] = c_obj

    workloads_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
      cpu_rows = self._timeseries(
          project,
          'metric.type="kubernetes.io/container/cpu/core_usage_time"',
          window_s,
          ['resource.label.cluster_name', 'resource.label.namespace_name', 'resource.label.container_name'],
          aligner='ALIGN_DELTA',
          reducer='REDUCE_SUM',
      )
    except FleetSourceError:
      cpu_rows = []
    try:
      mem_rows = self._timeseries(
          project,
          'metric.type="kubernetes.io/container/memory/used_bytes"',
          window_s,
          ['resource.label.cluster_name', 'resource.label.namespace_name', 'resource.label.container_name'],
          aligner='ALIGN_MEAN',
          reducer='REDUCE_SUM',
      )
    except FleetSourceError:
      mem_rows = []

    for labels, core_seconds in cpu_rows:
      cname = str(labels.get('cluster_name') or '')
      ns = str(labels.get('namespace_name') or 'default')
      cont = str(labels.get('container_name') or 'container')
      core_hours = round(float(core_seconds) / 3600.0, 4)
      if ns in SYSTEM_K8S_NAMESPACES:
        if cname in cluster_by_name:
          cluster_by_name[cname]['system_cpu_core_hours'] = round(
              cluster_by_name[cname]['system_cpu_core_hours'] + core_hours, 4
          )
        continue
      if cname in cluster_by_name:
        cluster_by_name[cname]['user_cpu_core_hours'] = round(
            cluster_by_name[cname]['user_cpu_core_hours'] + core_hours, 4
        )
      w = workloads_map.setdefault((cname, ns, cont), {
          'cluster_name': cname,
          'location': (cluster_by_name.get(cname) or {}).get('location') or 'us-central1',
          'cluster_mode': (cluster_by_name.get(cname) or {}).get('mode') or 'Standard',
          'node_count': (cluster_by_name.get(cname) or {}).get('node_count') or 0,
          'namespace': ns,
          'container_name': cont,
          'cpu_core_hours': 0.0,
          'memory_gib': 0.0,
      })
      w['cpu_core_hours'] = round(w['cpu_core_hours'] + core_hours, 4)

    for labels, mem_bytes in mem_rows:
      cname = str(labels.get('cluster_name') or '')
      ns = str(labels.get('namespace_name') or 'default')
      cont = str(labels.get('container_name') or 'container')
      if ns in SYSTEM_K8S_NAMESPACES:
        continue
      gib = round(float(mem_bytes) / (1024.0 ** 3), 4)
      if cname in cluster_by_name:
        cluster_by_name[cname]['user_memory_gib'] = round(
            cluster_by_name[cname]['user_memory_gib'] + gib, 4
        )
      w = workloads_map.setdefault((cname, ns, cont), {
          'cluster_name': cname,
          'location': (cluster_by_name.get(cname) or {}).get('location') or 'us-central1',
          'cluster_mode': (cluster_by_name.get(cname) or {}).get('mode') or 'Standard',
          'node_count': (cluster_by_name.get(cname) or {}).get('node_count') or 0,
          'namespace': ns,
          'container_name': cont,
          'cpu_core_hours': 0.0,
          'memory_gib': 0.0,
      })
      w['memory_gib'] = round(w['memory_gib'] + gib, 4)

    workloads = sorted(
        workloads_map.values(),
        key=lambda r: (-(r['cpu_core_hours'] + r['memory_gib']), r['cluster_name'], r['container_name']),
    )
    return {'clusters': clusters, 'workloads': workloads}

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
          'model': model, 'input_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 0,
          'cache_read_tokens': 0, 'cache_write_tokens': 0, 'other_tokens': 0, 'invocations': 0})

    for labels, value in token_rows:
      row = bucket(labels.get('model_user_id') or 'unknown')
      token_type = str(labels.get('type') or '').lower()
      count = round(value)
      if token_type == 'input':
        row['input_tokens'] += count
      elif token_type == 'output':
        row['output_tokens'] += count
      elif token_type in ('thought', 'thoughts', 'reasoning') or token_type.startswith(('thought', 'reasoning')):
        row['reasoning_tokens'] += count
      elif 'cache_read' in token_type or token_type.startswith('cached'):
        row['cache_read_tokens'] += count
      elif token_type.startswith('cache_write'):
        row['cache_write_tokens'] += count
      else:
        row['other_tokens'] += count
    for labels, value in invocation_rows:
      bucket(labels.get('model_user_id') or 'unknown')['invocations'] += round(value)

    cards = self._rate_cards()
    totals: dict[str, Any] = {'input_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 0,
              'cache_read_tokens': 0, 'cache_write_tokens': 0,
              'invocations': 0, 'est_cost_usd': 0.0, 'models_without_rate_card': []}
    rows = []
    for row in sorted(models.values(), key=lambda r: -(r['input_tokens'] + r['output_tokens'] + r['reasoning_tokens'])):
      if not any(row[k] for k in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'invocations')):
        continue
      card = cards.get(row['model'])
      if card:
        row['est_cost_usd'] = round(
            row['input_tokens'] / 1e6 * card['input']
            + (row['output_tokens'] + row['reasoning_tokens']) / 1e6 * card['output']
            + row['cache_read_tokens'] / 1e6 * card['cached_read']
            + row['cache_write_tokens'] / 1e6 * card['cache_write'], 4)
        totals['est_cost_usd'] += row['est_cost_usd']
      else:
        row['est_cost_usd'] = None
        totals['models_without_rate_card'].append(row['model'])
      denominator = row['input_tokens'] + row['cache_read_tokens'] + row['cache_write_tokens']
      row['cache_read_share_pct'] = round(row['cache_read_tokens'] / denominator * 100, 1) if denominator else None
      for key in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'invocations'):
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
        'assistant_requests': round(assist),
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
        engines_by_project[self._canonical_project(backend.get('project'))].add(backend['reasoning_engine_id'])
        if backend.get('resource') and backend.get('location'):
          engine_meta_jobs[backend['resource']] = backend['location']
      elif backend.get('kind') == 'cloud_run':
        services_by_project[self._canonical_project(backend.get('project'))].add(backend['service'])

    re_metric = 'aiplatform.googleapis.com/reasoning_engine'
    bucket_s = trend_bucket_seconds(hours)
    jobs: dict[tuple[str, ...], Any] = {}
    unreg_jobs: dict[tuple[str, ...], Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
      if self.discover_unregistered:
        unreg_jobs[('discover_re', self.project_id)] = pool.submit(self._discover_reasoning_engines, self.project_id)
        unreg_jobs[('run_catalog', self.project_id)] = pool.submit(self._cloud_run_services_catalog, self.project_id)
        unreg_jobs[('discover_gke', self.project_id)] = pool.submit(self._discover_gke_workloads, self.project_id, window_s)

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
      run_projects = {self._canonical_project(a['backend'].get('project')) for a in agents
                      if a['backend'].get('kind') == 'cloud_run' and a['backend'].get('service')}
      for project in sorted(run_projects):
        if self.discover_unregistered and project == self.project_id and ('run_catalog', self.project_id) in unreg_jobs:
          cat_fut = unreg_jobs[('run_catalog', self.project_id)]
          jobs[('run_inventory', project)] = pool.submit(_future_result_keys, cat_fut)
        else:
          jobs[('run_inventory', project)] = pool.submit(self._cloud_run_services, project)
      jobs[('model_usage',)] = pool.submit(self._model_usage, window_s)
      jobs[('model_usage_prev',)] = pool.submit(self._model_usage, window_s, window_s)
      jobs[('ge_traffic',)] = pool.submit(self._ge_traffic, window_s)
      jobs[('ge_assistant_tokens',)] = pool.submit(self._ge_assistant_usage, window_s)

      if self.discover_unregistered:
        if ('re_trace_tokens', self.project_id) not in jobs:
          unreg_jobs[('unreg_trace_tokens', self.project_id)] = pool.submit(
              self._trace_usage, self.project_id, [], window_s
          )
        try:
          disc_re = unreg_jobs[('discover_re', self.project_id)].result() or []
        except Exception:
          disc_re = []
        reg_re_ids = engines_by_project.get(self.project_id, set())
        unreg_re_ids = sorted({r['reasoning_engine_id'] for r in disc_re if r.get('reasoning_engine_id')} - reg_re_ids)
        if unreg_re_ids:
          unreg_jobs[('unreg_re_requests', self.project_id)] = pool.submit(
              self._requests_by, self.project_id, f'{re_metric}/request_count', 'reasoning_engine_id', unreg_re_ids, window_s
          )
          unreg_jobs[('unreg_re_latency', self.project_id)] = pool.submit(
              self._latency_by, self.project_id, f'{re_metric}/request_latencies', 'reasoning_engine_id', unreg_re_ids, window_s
          )
          unreg_jobs[('unreg_re_cpu', self.project_id)] = pool.submit(
              self._sum_by, self.project_id, f'{re_metric}/cpu/allocation_time', 'reasoning_engine_id', unreg_re_ids, window_s
          )
          unreg_jobs[('unreg_re_mem', self.project_id)] = pool.submit(
              self._sum_by, self.project_id, f'{re_metric}/memory/allocation_time', 'reasoning_engine_id', unreg_re_ids, window_s
          )
          unreg_jobs[('unreg_re_tokens', self.project_id)] = pool.submit(
              self._genai_usage, self.project_id, unreg_re_ids, window_s
          )

        try:
          run_cat = unreg_jobs[('run_catalog', self.project_id)].result() or {}
        except Exception:
          run_cat = {}
        reg_run_svcs = services_by_project.get(self.project_id, set())
        unreg_run_svcs = sorted(set(run_cat.keys()) - reg_run_svcs)
        if unreg_run_svcs:
          unreg_jobs[('unreg_run_requests', self.project_id)] = pool.submit(
              self._requests_by, self.project_id, 'run.googleapis.com/request_count', 'service_name', unreg_run_svcs, window_s
          )
          unreg_jobs[('unreg_run_latency', self.project_id)] = pool.submit(
              self._latency_by, self.project_id, 'run.googleapis.com/request_latencies', 'service_name', unreg_run_svcs, window_s
          )
          unreg_jobs[('unreg_run_billable', self.project_id)] = pool.submit(
              self._sum_by, self.project_id, 'run.googleapis.com/container/billable_instance_time', 'service_name', unreg_run_svcs, window_s
          )

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

      unreg_results: dict[tuple[str, ...], Any] = {}
      for key, future in unreg_jobs.items():
        try:
          unreg_results[key] = future.result()
        except Exception:
          unreg_results[key] = None

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

    token_results = [results[k] for k in results if k[0] == 're_tokens' and results[k]]
    trace_results = [results[k] for k in results if k[0] == 're_trace_tokens' and results[k]]

    # Mirror canonical project results to any discovered project number/ID aliases so
    # assess_registration, _apply_telemetry, and build_request_trend resolve both forms.
    aliases = (getattr(self, '_project_aliases', None) or {self.project_id}) - {self.project_id}
    if aliases:
      for key, val in list(results.items()):
        if len(key) == 2 and key[1] == self.project_id:
          for alias in aliases:
            results.setdefault((key[0], alias), val)
        elif len(key) == 2 and key[1] in aliases:
          results.setdefault((key[0], self.project_id), val)
          for alias in aliases:
            results.setdefault((key[0], alias), val)

    checked_at = _iso(_utcnow())
    for agent in agents:
      self._apply_telemetry(agent, results)
      agent['registration'] = assess_registration(agent, results, checked_at, self.project_id)

    unreg_bundle = self._build_unregistered_inventory(agents, results, unreg_results, checked_at)

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
    for metric_key in ('requests', 'errors_4xx', 'errors_5xx', 'llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens', 'conversations'):
      values = [a['metrics'][metric_key] for a in unique_runtime_agents if a['metrics'].get(metric_key) is not None]
      totals[metric_key] = sum(values) if values else None
    for fkey in ('vcpu_hours', 'memory_gib_hours', 'billable_instance_hours'):
      fvals = [float(a['metrics'][fkey]) for a in unique_runtime_agents if a['metrics'].get(fkey) is not None]
      totals[fkey] = round(sum(fvals), 4) if fvals else None
    totals['error_rate_pct'] = (
        round((totals['errors_5xx'] or 0) / totals['requests'] * 100, 2) if totals.get('requests') else None)
    stamps = [a['metrics']['last_activity'] for a in agents if a['metrics']['last_activity']]
    totals['last_activity'] = max(stamps) if stamps else None

    notes = [
        'Cloud Monitoring data points typically appear 1-3 minutes after the request.',
        'A2A agents on Cloud Run report service-level metrics (all traffic to the service).',
        'Google-managed and no-code agents expose no per-project runtime telemetry.',
        'Standalone Agent Engine, Cloud Run, GKE, and MCP runtimes not registered in GE are tracked in unregistered_runtimes.',
    ]
    if hours > 720:
      notes.append(
          'Per-agent token logs (Cloud Logging _Default) and traces (Cloud Trace) retain 30 days; '
          'windows over 1 month reflect 30-day agent token history while Requests and Project Model Spend cover the full window.'
      )
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
        'unregistered_runtimes': unreg_bundle['unregistered_runtimes'],
        'gke_workloads': unreg_bundle['gke_workloads'],
        'skills_and_mcp': unreg_bundle['skills_and_mcp'],
        'unregistered_summary': unreg_bundle['unregistered_summary'],
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
        'notes': notes,
    }

  def _build_unregistered_inventory(
      self,
      registered_agents: list[dict[str, Any]],
      results: dict[tuple[str, ...], Any],
      unreg_results: dict[tuple[str, ...], Any],
      checked_at: str,
  ) -> dict[str, Any]:
    """Builds the FinOps inventory of standalone Agent Engines, Cloud Run services, GKE workloads, and Skills/MCP."""
    project = self.project_id
    rate_cards = self._rate_cards()
    reg_re_ids: set[str] = set()
    reg_run_svcs: set[str] = set()
    engine_name_by_id: dict[str, str] = {}
    for a in registered_agents:
      b = a.get('backend') or {}
      if b.get('kind') == 'agent_engine' and b.get('reasoning_engine_id'):
        rid = str(b['reasoning_engine_id'])
        reg_re_ids.add(rid)
        engine_name_by_id[rid] = a.get('display_name') or rid
      elif b.get('kind') == 'cloud_run' and b.get('service'):
        reg_run_svcs.add(str(b['service']))

    trace_bundle = (
        results.get(('re_trace_tokens', project))
        or unreg_results.get(('unreg_trace_tokens', project))
        or {}
    )
    all_trace_engines: dict[str, dict[str, Any]] = dict(trace_bundle.get('all_engines') or {})
    trace_skills_mcp: list[dict[str, Any]] = [dict(item) for item in (trace_bundle.get('skills_and_mcp') or [])]

    disc_re_list: list[dict[str, Any]] = list(unreg_results.get(('discover_re', project)) or [])
    disc_re_by_id: dict[str, dict[str, Any]] = {}
    for r in disc_re_list:
      rid = str(r.get('reasoning_engine_id') or '')
      if rid:
        disc_re_by_id[rid] = r
        if rid not in engine_name_by_id and r.get('display_name'):
          engine_name_by_id[rid] = str(r['display_name'])

    # Also include any standalone Agent Engine that emitted Cloud Trace spans in the window
    # even if it was deleted before reasoningEngines.list ran or lives in another region.
    for rid, t_usage in all_trace_engines.items():
      if rid not in reg_re_ids and rid not in disc_re_by_id:
        inferred_names = t_usage.get('agent_names') or []
        display = inferred_names[0] if inferred_names else f'Standalone Agent Engine {rid}'
        disc_re_by_id[rid] = {
            'reasoning_engine_id': rid,
            'resource': f'projects/{project}/locations/us-central1/reasoningEngines/{rid}',
            'project': project,
            'location': 'us-central1',
            'display_name': display,
            'description': 'Discovered via Cloud Trace gen_ai spans (not registered in any Gemini Enterprise app).',
            'framework': 'google-adk',
            'created': None,
            'updated': t_usage.get('last_activity'),
        }
        engine_name_by_id[rid] = display

    re_req = unreg_results.get(('unreg_re_requests', project)) or {}
    re_lat = unreg_results.get(('unreg_re_latency', project)) or {}
    re_cpu = unreg_results.get(('unreg_re_cpu', project)) or {}
    re_mem = unreg_results.get(('unreg_re_mem', project)) or {}
    re_tok = (unreg_results.get(('unreg_re_tokens', project)) or {}).get('by_engine') or {}

    unregistered_runtimes: list[dict[str, Any]] = []
    count_re = 0
    count_active_re = 0
    count_zombie_re = 0
    zombie_vcpu_hours = 0.0
    zombie_mem_gib_hours = 0.0

    for rid, info in disc_re_by_id.items():
      if rid in reg_re_ids:
        continue
      count_re += 1
      loc = info.get('location') or 'us-central1'
      req_info = re_req.get(rid) or {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0}
      lat_info = re_lat.get(rid) or {}
      vcpu_h = round(float(re_cpu.get(rid, 0.0)) / 3600.0, 4) if rid in re_cpu else 0.0
      mem_h = round(float(re_mem.get(rid, 0.0)) / 3600.0, 4) if rid in re_mem else 0.0
      log_u = re_tok.get(rid)
      tr_u = all_trace_engines.get(rid)
      usage = log_u if (log_u and log_u.get('llm_calls')) else (tr_u if (tr_u and tr_u.get('llm_calls')) else (log_u or tr_u))
      llm_calls = int((usage or {}).get('llm_calls') or 0)
      in_tok = int((usage or {}).get('input_tokens') or 0)
      out_tok = int((usage or {}).get('output_tokens') or 0)
      cache_tok = int((usage or {}).get('cached_tokens') or 0)
      convs = int((usage or {}).get('conversations') or 0)
      last_act = (usage or {}).get('last_activity')
      models = list((usage or {}).get('models') or [])
      inferred_names = (tr_u or {}).get('agent_names') or []
      display_name = info.get('display_name') or f'ReasoningEngine {rid}'
      if inferred_names and (display_name.startswith('ReasoningEngine ') or display_name.startswith('Standalone ')):
        display_name = inferred_names[0]
      elif inferred_names and inferred_names[0] not in display_name:
        display_name = f'{display_name} ({inferred_names[0]})'
      engine_name_by_id[rid] = display_name

      requests_cnt = int(req_info.get('requests') or 0)
      err_4xx = int(req_info.get('errors_4xx') or 0)
      err_5xx = int(req_info.get('errors_5xx') or 0)
      est_cost = _estimate_token_cost_usd(in_tok, out_tok, cache_tok, models, rate_cards)

      is_active = (requests_cnt > 0) or (llm_calls > 0)
      if is_active:
        count_active_re += 1
        finops_status = 'ACTIVE_UNREGISTERED'
        state_str = 'ACTIVE_UNREGISTERED'
        finops_rec = (
            'Active standalone Agent Engine consuming tokens/compute outside Gemini Enterprise governance. '
            'Register in GE App or enforce standalone FinOps budget.'
        )
        finops_cmd = (
            f'gcloud alpha discovery-engine engines assistants agents create --project={project} '
            f'--reasoning-engine={info["resource"]}'
        )
      else:
        count_zombie_re += 1
        zombie_vcpu_hours += vcpu_h
        zombie_mem_gib_hours += mem_h
        finops_status = 'ZOMBIE_IDLE_ENGINE'
        state_str = 'ZOMBIE_IDLE'
        finops_rec = (
            f'Zombie/idle Reasoning Engine holding {mem_h:,.1f} GiB-hrs ({vcpu_h:,.1f} vCPU-hrs) with 0 requests '
            'and 0 LLM calls. Delete to reclaim idle runtime footprint.'
        )
        finops_cmd = f'gcloud ai reasoning-engines delete {rid} --project={project} --region={loc} --quiet'

      unregistered_runtimes.append({
          'agent_id': f're-{rid}',
          'resource_name': info['resource'],
          'display_name': display_name,
          'description': info.get('description') or f'Standalone Vertex AI ReasoningEngine ({loc})',
          'engine_id': 'unregistered',
          'engine_key': 'unregistered',
          'engine_display_name': 'Standalone / Not in GE App',
          'assistant_id': 'standalone',
          'location': loc,
          'type': 'ADK_STANDALONE',
          'type_label': AGENT_TYPE_LABELS['ADK_STANDALONE'],
          'state': state_str,
          'sharing_scope': 'UNREGISTERED_STANDALONE',
          'created': info.get('created'),
          'updated': info.get('updated'),
          'starter_prompts': 0,
          'backend': {
              'kind': 'agent_engine',
              'resource': info['resource'],
              'project': project,
              'location': loc,
              'reasoning_engine_id': rid,
              'display_name': display_name,
              'framework': info.get('framework') or 'google-adk',
              'models': models,
          },
          'telemetry_scope': 'agent',
          'metrics': {
              'requests': requests_cnt,
              'errors_4xx': err_4xx,
              'errors_5xx': err_5xx,
              'error_rate_pct': round(err_5xx / requests_cnt * 100, 2) if requests_cnt else None,
              'latency_p50_ms': lat_info.get('p50'),
              'latency_p95_ms': lat_info.get('p95'),
              'llm_calls': llm_calls,
              'input_tokens': in_tok,
              'output_tokens': out_tok,
              'cached_tokens': cache_tok,
              'conversations': convs,
              'last_activity': last_act,
              'vcpu_hours': vcpu_h,
              'memory_gib_hours': mem_h,
              'billable_instance_hours': None,
              'est_token_cost_usd': est_cost,
              # Compute is reported as measured usage (vCPU-h, GiB-h), not dollars: no price is assumed.
              'est_infra_cost_usd': None,
          },
          'registration': {
              'status': 'UNREGISTERED_STANDALONE',
              'finops_status': finops_status,
              'evidence': f'Provisioned in Vertex AI Agent Engine ({loc}/{rid}) but not registered in any Gemini Enterprise app.',
              'checked_at': checked_at,
              'action': {'summary': finops_rec, 'delete_command': finops_cmd},
          },
          'finops_status': finops_status,
          'finops_recommendation': finops_rec,
          'finops_action_command': finops_cmd,
          'data_sources': ['Vertex AI ReasoningEngines API', 'Cloud Monitoring: reasoning_engine/*', 'Cloud Trace / Logging: gen_ai.*'],
          'notes': [finops_rec],
      })

    run_catalog: dict[str, dict[str, Any]] = dict(unreg_results.get(('run_catalog', project)) or {})
    run_req = unreg_results.get(('unreg_run_requests', project)) or {}
    run_lat = unreg_results.get(('unreg_run_latency', project)) or {}
    run_bill = unreg_results.get(('unreg_run_billable', project)) or {}

    count_run = 0
    count_mcp_run = 0
    count_always_on_run = 0
    for svc_name, sinfo in run_catalog.items():
      if svc_name in reg_run_svcs:
        continue
      count_run += 1
      cat = sinfo.get('category') or 'CLOUD_RUN_AGENT'
      if cat == 'MCP_SERVER':
        count_mcp_run += 1
      min_inst = int(sinfo.get('min_instances') or 0)
      if min_inst >= 1:
        count_always_on_run += 1
      region = sinfo.get('region') or 'us-central1'
      s_req = run_req.get(svc_name) or {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0}
      s_lat = run_lat.get(svc_name) or {}
      bill_h = round(float(run_bill.get(svc_name, 0.0)) / 3600.0, 4) if svc_name in run_bill else 0.0
      req_cnt = int(s_req.get('requests') or 0)
      err_4xx = int(s_req.get('errors_4xx') or 0)
      err_5xx = int(s_req.get('errors_5xx') or 0)

      if min_inst >= 1:
        finops_status = 'ALWAYS_ON_UNREGISTERED'
        state_str = f'ALWAYS_ON (min={min_inst})'
        finops_rec = (
            f'Cloud Run {sinfo.get("category_label")} configured with min-instances={min_inst} '
            f'({bill_h:,.1f} billable instance-hrs). Scale to min-instances=0 if cold-start budget permits.'
        )
        finops_cmd = f'gcloud run services update {svc_name} --project={project} --region={region} --min-instances=0'
      elif req_cnt == 0:
        finops_status = 'ZOMBIE_IDLE_SERVICE'
        state_str = 'ZOMBIE_IDLE'
        finops_rec = f'Idle Cloud Run service ({region}) with 0 requests in window. Review for decommission.'
        finops_cmd = f'gcloud run services delete {svc_name} --project={project} --region={region} --quiet'
      else:
        finops_status = 'ACTIVE_UNREGISTERED'
        state_str = 'ACTIVE_UNREGISTERED'
        finops_rec = (
            f'Active {sinfo.get("category_label")} ({req_cnt:,} requests, {bill_h:,.1f} billable instance-hrs) '
            'not registered in Gemini Enterprise.'
        )
        finops_cmd = f'gcloud run services describe {svc_name} --project={project} --region={region}'

      unregistered_runtimes.append({
          'agent_id': f'run-{svc_name}',
          'resource_name': sinfo.get('resource_name') or f'projects/{project}/locations/{region}/services/{svc_name}',
          'display_name': svc_name,
          'description': f'{sinfo.get("category_label")} ({sinfo.get("cpu") or "1"} vCPU, {sinfo.get("memory") or "512Mi"}, min={min_inst})',
          'engine_id': 'unregistered',
          'engine_key': 'unregistered',
          'engine_display_name': 'Standalone / Not in GE App',
          'assistant_id': 'standalone',
          'location': region,
          'type': cat,
          'type_label': AGENT_TYPE_LABELS.get(cat, 'Cloud Run Service (Not in GE)'),
          'state': state_str,
          'sharing_scope': sinfo.get('ingress') or 'UNREGISTERED_STANDALONE',
          'created': sinfo.get('created'),
          'updated': sinfo.get('updated'),
          'starter_prompts': 0,
          'backend': {
              'kind': 'cloud_run',
              'service': svc_name,
              'project': project,
              'region': region,
              'url': sinfo.get('uri'),
              'min_instances': min_inst,
              'max_instances': sinfo.get('max_instances'),
              'cpu': sinfo.get('cpu'),
              'memory': sinfo.get('memory'),
          },
          'telemetry_scope': 'service',
          'metrics': {
              'requests': req_cnt,
              'errors_4xx': err_4xx,
              'errors_5xx': err_5xx,
              'error_rate_pct': round(err_5xx / req_cnt * 100, 2) if req_cnt else None,
              'latency_p50_ms': s_lat.get('p50'),
              'latency_p95_ms': s_lat.get('p95'),
              'llm_calls': None,
              'input_tokens': None,
              'output_tokens': None,
              'cached_tokens': None,
              'conversations': None,
              'last_activity': sinfo.get('updated'),
              'vcpu_hours': None,
              'memory_gib_hours': None,
              'billable_instance_hours': bill_h,
              'est_token_cost_usd': None,
              'est_infra_cost_usd': None,  # usage only: billable_instance_hours above
          },
          'registration': {
              'status': 'UNREGISTERED_STANDALONE',
              'finops_status': finops_status,
              'evidence': f"Cloud Run service '{svc_name}' ({region}) is not registered as a Gemini Enterprise agent.",
              'checked_at': checked_at,
              'action': {'summary': finops_rec, 'delete_command': finops_cmd},
          },
          'finops_status': finops_status,
          'finops_recommendation': finops_rec,
          'finops_action_command': finops_cmd,
          'data_sources': ['Cloud Run Admin API v2', 'Cloud Monitoring: run.googleapis.com/*'],
          'notes': [finops_rec],
      })

      # Also surface MCP Server and Skill Backend Cloud Run services in the Skills & MCP inventory
      if cat in ('MCP_SERVER', 'SKILL_BACKEND'):
        trace_skills_mcp.append({
            'name': svc_name,
            'kind': 'MCP_SERVER_RUNTIME' if cat == 'MCP_SERVER' else 'SKILL_BACKEND_RUNTIME',
            'calls': req_cnt,
            'traces_count': req_cnt,
            'co_occurring_input_tokens': 0,
            'co_occurring_output_tokens': 0,
            'avg_latency_ms': s_lat.get('p50'),
            'latency_p95_ms': s_lat.get('p95'),
            'billable_instance_hours': bill_h,
            'min_instances': min_inst,
            'uri': sinfo.get('uri'),
            'engines': [f'cloud_run:{region}/{svc_name}'],
            'engine_names': [f'Cloud Run ({region})'],
            'models': [],
            'registration_scope': 'UNREGISTERED_STANDALONE',
            'last_activity': sinfo.get('updated'),
        })

    # Also surface registered BYO_MCP DataConnectors in the Skills & MCP inventory as GE_REGISTERED
    for a in registered_agents:
      if a.get('type') != 'MCP_CONNECTOR':
        continue
      b = a.get('backend') or {}
      m = a.get('metrics') or {}
      svc_name = str(b.get('service') or '')
      sinfo = run_catalog.get(svc_name) if svc_name else None
      if sinfo:
        b.setdefault('min_instances', int(sinfo.get('min_instances') or 0))
        b.setdefault('max_instances', sinfo.get('max_instances'))
        b.setdefault('cpu', sinfo.get('cpu'))
        b.setdefault('memory', sinfo.get('memory'))
        if not m.get('last_activity'):
          m['last_activity'] = sinfo.get('updated') or a.get('updated')
      req_cnt = int(m.get('requests') or 0)
      trace_skills_mcp.append({
          'name': a.get('display_name') or b.get('collection_id') or svc_name or a.get('agent_id'),
          'kind': 'MCP_CONNECTOR_RUNTIME',
          'calls': req_cnt,
          'traces_count': req_cnt,
          'co_occurring_input_tokens': 0,
          'co_occurring_output_tokens': 0,
          'avg_latency_ms': m.get('latency_p50_ms'),
          'latency_p95_ms': m.get('latency_p95_ms'),
          'billable_instance_hours': m.get('billable_instance_hours'),
          'min_instances': b.get('min_instances'),
          'uri': b.get('url'),
          'mcp_tools': list(b.get('mcp_tools') or []),
          'engines': [str(a.get('engine_key') or a.get('engine_id') or 'global')],
          'engine_names': [str(a.get('engine_display_name') or a.get('engine_id') or 'GE App')],
          'models': [],
          'registration_scope': 'GE_REGISTERED',
          'last_activity': m.get('last_activity') or a.get('updated'),
      })

    gke_raw = unreg_results.get(('discover_gke', project)) or unreg_results.get(('gke_workloads', project)) or {'clusters': [], 'workloads': []}
    if isinstance(gke_raw, list):
      gke_data = {'clusters': [], 'workloads': gke_raw}
    else:
      gke_data = gke_raw
    gke_clusters = list(gke_data.get('clusters') or [])
    gke_workloads = list(gke_data.get('workloads') or [])
    for gw in gke_workloads:
      cname = gw.get('cluster_name') or 'gke-cluster'
      ns = gw.get('namespace') or 'default'
      cont = gw.get('container_name') or 'workload'
      loc = gw.get('location') or 'us-central1'
      mode = gw.get('cluster_mode') or 'Standard'
      core_h = float(gw.get('cpu_core_hours') or 0.0)
      mem_g = float(gw.get('memory_gib') or 0.0)
      finops_rec = (
          f'GKE {mode} container workload {ns}/{cont} on cluster {cname} ({loc}): '
          f'{core_h:,.2f} CPU core-hrs, {mem_g:,.2f} GiB avg memory. Not registered in GE App.'
      )
      finops_cmd = (
          f'gcloud container clusters get-credentials {cname} --project={project} --location={loc} '
          f'&& kubectl top pods -n {ns}'
      )
      unregistered_runtimes.append({
          'agent_id': f'gke-{cname}-{ns}-{cont}',
          'resource_name': f'projects/{project}/locations/{loc}/clusters/{cname}/namespaces/{ns}/containers/{cont}',
          'display_name': f'{cname}/{cont}',
          'description': f'GKE {mode} Workload ({ns}/{cont} on {cname}, {gw.get("node_count") or 0} nodes)',
          'engine_id': 'unregistered',
          'engine_key': 'unregistered',
          'engine_display_name': 'Standalone / Not in GE App',
          'assistant_id': 'standalone',
          'location': loc,
          'type': 'GKE_WORKLOAD',
          'type_label': AGENT_TYPE_LABELS['GKE_WORKLOAD'],
          'state': 'ACTIVE_GKE_WORKLOAD',
          'sharing_scope': 'GKE_CLUSTER',
          'created': None,
          'updated': checked_at,
          'starter_prompts': 0,
          'backend': {
              'kind': 'gke_workload',
              'cluster_name': cname,
              'cluster_mode': mode,
              'namespace': ns,
              'container_name': cont,
              'project': project,
              'location': loc,
          },
          'telemetry_scope': 'container',
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
              'last_activity': checked_at,
              'vcpu_hours': core_h,
              'memory_gib_hours': None,
              'avg_memory_gib': mem_g,
              'billable_instance_hours': None,
              'est_token_cost_usd': None,
              'est_infra_cost_usd': None,  # usage only: CPU core-hours and average GiB above
          },
          'registration': {
              'status': 'UNREGISTERED_STANDALONE',
              'finops_status': 'ACTIVE_GKE_WORKLOAD',
              'evidence': f'Running on GKE cluster {cname} ({ns}/{cont}); not registered in Gemini Enterprise.',
              'checked_at': checked_at,
              'action': {'summary': finops_rec, 'delete_command': finops_cmd},
          },
          'finops_status': 'ACTIVE_GKE_WORKLOAD',
          'finops_recommendation': finops_rec,
          'finops_action_command': finops_cmd,
          'data_sources': ['GKE Clusters API', 'Cloud Monitoring: kubernetes.io/container/*'],
          'notes': [finops_rec],
      })

    # Enrich trace_skills_mcp with human-readable engine_names
    for item in trace_skills_mcp:
      if 'engine_names' not in item:
        item['engine_names'] = [engine_name_by_id.get(eid, eid) for eid in (item.get('engines') or [])]
    trace_skills_mcp.sort(key=lambda r: (-int(r.get('calls') or 0), str(r.get('name') or '')))

    def _sort_key(rt: dict[str, Any]) -> tuple[Any, ...]:
      m = rt.get('metrics') or {}
      req = int(m.get('requests') or 0)
      llm = int(m.get('llm_calls') or 0)
      tok = int(m.get('input_tokens') or 0) + int(m.get('output_tokens') or 0)
      is_gke = 1 if rt.get('type') == 'GKE_WORKLOAD' else 0
      alloc = float(m.get('billable_instance_hours') or 0.0) + float(m.get('vcpu_hours') or 0.0)
      active_tier = 2 if (req > 0 or llm > 0) else (1 if is_gke else 0)
      return (-active_tier, -tok, -req, -llm, -alloc, str(rt.get('display_name') or ''))

    unregistered_runtimes.sort(key=_sort_key)

    total_unreg_requests = sum(int(r['metrics'].get('requests') or 0) for r in unregistered_runtimes)
    total_unreg_llm_calls = sum(int(r['metrics'].get('llm_calls') or 0) for r in unregistered_runtimes)
    total_unreg_in_tok = sum(int(r['metrics'].get('input_tokens') or 0) for r in unregistered_runtimes)
    total_unreg_out_tok = sum(int(r['metrics'].get('output_tokens') or 0) for r in unregistered_runtimes)
    total_unreg_cache_tok = sum(int(r['metrics'].get('cached_tokens') or 0) for r in unregistered_runtimes)
    total_unreg_cost = sum(float(r['metrics'].get('est_token_cost_usd') or 0.0) for r in unregistered_runtimes)
    zombie_runtimes = sum(1 for r in unregistered_runtimes if str(r.get('finops_status') or '').startswith('ZOMBIE'))
    # Runtimes with token usage but no rate card for their model(s): left out of the total, not guessed.
    unpriced_unreg = sum(
        1 for r in unregistered_runtimes
        if r['metrics'].get('est_token_cost_usd') is None
        and (int(r['metrics'].get('input_tokens') or 0) or int(r['metrics'].get('output_tokens') or 0))
    )
    total_unreg_vcpu = sum(
        float(r['metrics'].get('vcpu_hours') or 0.0)
        for r in unregistered_runtimes
        if r.get('type') == 'ADK_STANDALONE'
    )
    total_unreg_mem = sum(
        float(r['metrics'].get('memory_gib_hours') or 0.0)
        for r in unregistered_runtimes
        if r.get('type') == 'ADK_STANDALONE'
    )
    total_unreg_billable = sum(
        float(r['metrics'].get('billable_instance_hours') or 0.0)
        for r in unregistered_runtimes
    )
    total_gke_cpu = sum(float(w.get('cpu_core_hours') or 0.0) for w in gke_workloads)
    total_gke_mem = sum(float(w.get('memory_gib') or 0.0) for w in gke_workloads)

    unregistered_summary = {
        'total_unregistered_runtimes': len(unregistered_runtimes),
        'unregistered_reasoning_engines': count_re,
        'active_unregistered_reasoning_engines': count_active_re,
        'zombie_reasoning_engines': count_zombie_re,
        'zombie_runtimes_count': zombie_runtimes,
        'unregistered_cloud_run_services': count_run,
        'mcp_cloud_run_services': count_mcp_run,
        'always_on_cloud_run_services': count_always_on_run,
        'gke_clusters_count': len(gke_clusters),
        'gke_workloads_count': len(gke_workloads),
        'discovered_skills_and_mcp_count': len(trace_skills_mcp),
        'unregistered_requests': total_unreg_requests,
        'unregistered_llm_calls': total_unreg_llm_calls,
        'unregistered_input_tokens': total_unreg_in_tok,
        'unregistered_output_tokens': total_unreg_out_tok,
        'unregistered_cached_tokens': total_unreg_cache_tok,
        'unregistered_est_token_cost_usd': round(total_unreg_cost, 4),
        'unregistered_unpriced_runtimes': unpriced_unreg,
        'zombie_vcpu_hours': round(zombie_vcpu_hours, 2),
        'zombie_memory_gib_hours': round(zombie_mem_gib_hours, 2),
        'unregistered_vcpu_hours': round(total_unreg_vcpu, 2),
        'unregistered_memory_gib_hours': round(total_unreg_mem, 2),
        'unregistered_billable_instance_hours': round(total_unreg_billable, 2),
        'gke_cpu_core_hours': round(total_gke_cpu, 2),
        'gke_memory_gib': round(total_gke_mem, 2),
        'discovery_status': 'ok' if self.discover_unregistered else 'trace_only',
    }
    return {
        'unregistered_runtimes': unregistered_runtimes,
        'gke_workloads': {'clusters': gke_clusters, 'workloads': gke_workloads},
        'skills_and_mcp': trace_skills_mcp,
        'unregistered_summary': unregistered_summary,
    }

  def _apply_telemetry(self, agent: dict[str, Any], results: dict[tuple[str, ...], Any]) -> None:
    """Joins one agent with the telemetry fetched for its backend (exact project match)."""
    backend, metrics = agent['backend'], agent['metrics']
    project = (
        self._canonical_project(backend.get('project'))
        if hasattr(self, '_canonical_project')
        else (backend.get('project') or self.project_id)
    )
    def pick(name):
      return results.get((name, project))
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
  unreg = payload.get('unregistered_summary') or {}
  if unreg.get('total_unregistered_runtimes'):
    lines.append(
        f"- Unregistered / Standalone Runtimes (NOT in GE App): {unreg.get('total_unregistered_runtimes', 0)} total "
        f"({unreg.get('unregistered_reasoning_engines', 0)} Agent Engines [{unreg.get('zombie_reasoning_engines', 0)} zombie/idle "
        f"holding {unreg.get('zombie_memory_gib_hours', 0.0):,.1f} GiB-hrs], "
        f"{unreg.get('unregistered_cloud_run_services', 0)} Cloud Run services [{unreg.get('mcp_cloud_run_services', 0)} MCP], "
        f"{unreg.get('gke_workloads_count', 0)} GKE workloads across {unreg.get('gke_clusters_count', 0)} clusters); "
        f"{unreg.get('unregistered_requests', 0):,} requests, {_fmt_tokens(unreg.get('unregistered_input_tokens'))} in / "
        f"{_fmt_tokens(unreg.get('unregistered_output_tokens'))} out tokens ({unreg.get('unregistered_llm_calls', 0):,} LLM calls)"
    )
  skills_mcp = payload.get('skills_and_mcp') or []
  if skills_mcp:
    top_tools = ', '.join(f"{s.get('name')} ({s.get('calls', 0):,})" for s in skills_mcp[:5])
    lines.append(f"- Discovered Skills, Sub-Agents & MCP Tools ({len(skills_mcp)}): {top_tools}")
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

