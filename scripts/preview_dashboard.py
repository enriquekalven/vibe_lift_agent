#!/usr/bin/env python3
"""Serves the dashboard on fictional fixture data: run `make preview`, then open http://127.0.0.1:8766.

Use it for UI work and reviews without a Google Cloud project. Nothing here contacts Google Cloud:
the fleet service, the BigQuery REST calls and the Cloud Billing export reader are replaced by
deterministic fixture data shaped like the real payloads. The real server code (live FinOps,
insights aggregation, cost estimates, the validator and the whole dashboard) runs on top of it.

Every page carries a banner saying the data is fictional, and the server only listens on 127.0.0.1.
The made-up project is "acme-ge-preview": 14 agents in 3 Gemini Enterprise apps, with failing, degraded,
broken, idle, disabled and inventory-only agents so every dashboard state shows.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import math
import os
import pathlib
import random
import sys
import zlib
from collections.abc import Iterator
from typing import Any
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

PROJECT = 'acme-ge-preview'
PROJECT_NUMBER = '123456789012'
DOMAIN = 'acme.example'
DEFAULT_PORT = 8766
BANNER_ID = 'vlPreviewBanner'
BANNER_TEXT = (f'Preview: fictional fixture data for a made-up project ({PROJECT}). '
               'Nothing on this page comes from Google Cloud.')

ENGINES = [
    {'engine_id': 'acme-intranet', 'location': 'global', 'display_name': 'Acme Intranet Assistant'},
    {'engine_id': 'acme-support', 'location': 'us', 'display_name': 'Acme Customer Support'},
    {'engine_id': 'acme-eu-people', 'location': 'eu', 'display_name': 'Acme EU People Hub'},
]
for _e in ENGINES:
  _e['app_type'] = 'APP_TYPE_INTRANET'
  _e['engine_key'] = f"{_e['location']}/{_e['engine_id']}"

# name, engine index, type, backend kind, model, requests/24h, 4xx, 5xx, p50 ms, p95 ms,
# LLM calls per request, input tokens per call, output tokens per call, cache share, extras
AGENTS: list[tuple[Any, ...]] = [
    ('IT Service Desk', 0, 'ADK', 'agent_engine', 'gemini-2.5-flash', 4820, 61, 12, 1400, 6200, 2.9, 2600, 150, 0.55, {}),
    ('Expense Policy Helper', 0, 'ADK', 'agent_engine', 'gemini-2.5-flash', 1240, 9, 3, 1100, 4100, 2.1, 2100, 120, 0.62, {}),
    ('Sales Deal Desk', 0, 'A2A', 'cloud_run', None, 2310, 520, 9, 380, 2400, 0, 0, 0, 0, {'service': 'deal-desk-a2a'}),
    ('Code Review Copilot', 0, 'ADK', 'agent_engine', 'gemini-2.5-pro', 610, 22, 41, 6400, 21000, 3.4, 9800, 900, 0.21, {}),
    ('Knowledge Search', 0, 'LOW_CODE', 'gemini_enterprise_hosted', None, None, None, None, None, None, 0, 0, 0, 0, {}),
    ('Deep Research', 0, 'MANAGED', None, None, None, None, None, None, None, 0, 0, 0, 0, {}),
    ('Contract Analyzer', 0, 'ADK', 'agent_engine', 'gemini-2.5-flash', 220, 2, 1, 9800, 48000, 44.5, 3100, 210, 0.08, {}),
    ('Support Triage', 1, 'ADK', 'agent_engine', 'gemini-2.5-flash', 3950, 40, 6, 900, 3600, 2.2, 1900, 140, 0.71, {}),
    ('Refund Assistant', 1, 'A2A', 'cloud_run', None, 880, 31, 2, 520, 1900, 0, 0, 0, 0, {'service': 'refund-assistant'}),
    ('Order Status Bot', 1, 'ADK', 'agent_engine', 'gemini-2.5-flash', 0, 0, 0, None, None, 0, 0, 0, 0, {'broken': True}),
    ('Warranty Claims', 1, 'LOW_CODE', 'gemini_enterprise_hosted', None, None, None, None, None, None, 0, 0, 0, 0,
     {'state': 'DISABLED'}),
    ('HR Policy Q&A', 2, 'ADK', 'agent_engine', 'gemini-3.1-flash-lite', 1480, 12, 2, 700, 2600, 1.8, 1500, 110, 0.64,
     {'re_loc': 'europe-west1'}),
    ('Leave Planner', 2, 'A2A', 'cloud_run', None, 0, 0, 0, None, None, 0, 0, 0, 0,
     {'service': 'leave-planner', 'region': 'europe-west1'}),
    ('Benefits Navigator', 2, 'MANAGED', None, None, None, None, None, None, None, 0, 0, 0, 0, {}),
]
TYPE_LABEL = {'ADK': 'ADK agent on Vertex AI Agent Engine', 'A2A': 'A2A agent', 'LOW_CODE': 'No-code agent (Agent Designer)',
              'MANAGED': 'Google-managed agent'}
DESCRIPTIONS = {
    'IT Service Desk': 'Resolves IT tickets and resets access.',
    'Code Review Copilot': 'Reviews pull requests against Acme style guides.',
    'Contract Analyzer': 'Extracts clauses and risks from vendor contracts.',
    'Support Triage': 'Routes inbound customer cases to the right queue.',
}
USERS = [
    ('maria.lopez', 'global/acme-intranet', 42, 'IT Service Desk'),
    ('dev.patel', 'global/acme-intranet', 37, 'Code Review Copilot'),
    ('sam.okafor', 'us/acme-support', 33, 'Support Triage'),
    ('lena.schmidt', 'eu/acme-eu-people', 29, 'HR Policy Q&A'),
    ('chen.wei', 'global/acme-intranet', 26, 'Contract Analyzer'),
    ('ana.silva', 'us/acme-support', 21, 'Refund Assistant'),
    ('tom.becker', 'eu/acme-eu-people', 18, 'HR Policy Q&A'),
    ('priya.n', 'global/acme-intranet', 15, 'Expense Policy Helper'),
    ('jordan.lee', 'us/acme-support', 12, 'Support Triage'),
    ('fatima.k', 'global/acme-intranet', 9, 'IT Service Desk'),
]
BILLING_TABLE = 'acme-billing.billing_export.gcp_billing_export_v1_0A1B2C'
MART_SQL_BUILDERS = (
    'build_audit_principals_sql', 'build_user_engine_rollup_sql', 'build_user_model_tokens_sql', 'build_recent_turns_sql',
    'build_daily_totals_sql', 'build_daily_usage_sql', 'build_recent_sessions_sql', 'build_session_turns_sql',
    'build_sessionless_token_turns_sql',
)
_SQL_MARKER = '--vibelift-preview:'


def _utcnow() -> dt.datetime:
  return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
  return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _scale(window_hours: int) -> float:
  return max(window_hours, 1) / 24.0


def _rng(name: str) -> random.Random:
  """Deterministic per-dataset randomness (str hash() is salted per process, crc32 is not)."""
  return random.Random(zlib.crc32(name.encode('utf-8')))


# ---------------------------------------------------------------------------------------------------
# Fleet fixture (shaped like GeminiEnterpriseFleetService.collect())
# ---------------------------------------------------------------------------------------------------
def build_agents(window_hours: int, now: dt.datetime) -> list[dict[str, Any]]:
  rng = _rng('agents')
  s = _scale(window_hours)
  agents = []
  for idx, (name, ei, typ, kind, model, req, e4, e5, p50, p95, lpr, tin, tout, cshare, extra) in enumerate(AGENTS):
    eng = ENGINES[ei]
    aid = str(13791105764600209034 + idx * 7919)
    rn = (f"projects/{PROJECT}/locations/{eng['location']}/collections/default_collection/engines/{eng['engine_id']}"
          f'/assistants/default_assistant/agents/{aid}')
    backend: dict[str, Any] = {}
    if kind == 'agent_engine':
      re_id = str(1588369340892184576 + idx * 104729)
      loc = extra.get('re_loc', 'us-central1')
      backend = {'kind': 'agent_engine', 'resource': f'projects/{PROJECT_NUMBER}/locations/{loc}/reasoningEngines/{re_id}',
                 'project': PROJECT_NUMBER, 'location': loc, 'reasoning_engine_id': re_id, 'models': [model],
                 'display_name': name.lower().replace(' ', '-').replace('&', 'and'), 'framework': 'google-adk'}
    elif kind == 'cloud_run':
      region = extra.get('region', 'us-central1')
      backend = {'kind': 'cloud_run', 'service': extra['service'], 'region': region,
                 'url': f"https://{extra['service']}-{PROJECT_NUMBER}.{region}.run.app"}
    elif kind == 'gemini_enterprise_hosted':
      backend = {'kind': 'gemini_enterprise_hosted'}
    scope = 'agent' if kind == 'agent_engine' else ('service' if kind == 'cloud_run' else 'none')
    m: dict[str, Any] = {k: None for k in (
        'requests', 'errors_4xx', 'errors_5xx', 'error_rate_pct', 'latency_p50_ms', 'latency_p95_ms', 'llm_calls',
        'input_tokens', 'output_tokens', 'cached_tokens', 'conversations', 'last_activity', 'vcpu_hours',
        'memory_gib_hours', 'billable_instance_hours', 'token_source')}
    if req is not None:
      r = round(req * s)
      m.update({'requests': r, 'errors_4xx': round(e4 * s), 'errors_5xx': round(e5 * s),
                'latency_p50_ms': float(p50) if p50 else None, 'latency_p95_ms': float(p95) if p95 else None})
      m['error_rate_pct'] = round(m['errors_5xx'] / r * 100, 2) if r else 0.0
      if kind == 'agent_engine':
        calls = round(r * lpr)
        m.update({'llm_calls': calls, 'input_tokens': calls * tin, 'output_tokens': calls * tout,
                  'cached_tokens': int(calls * tin * cshare), 'conversations': int(r * 0.41),
                  'vcpu_hours': round(4.0 * min(window_hours, 720) / 24 * (1 if r else 0.25), 2),
                  'memory_gib_hours': round(8.0 * min(window_hours, 720) / 24 * (1 if r else 0.25), 2),
                  'token_source': 'Cloud Logging: OpenTelemetry gen_ai inference events'})
      elif kind == 'cloud_run':
        m['billable_instance_hours'] = round(window_hours * (0.6 if r else 1.0), 1)
      if r:
        m['last_activity'] = _iso(now - dt.timedelta(minutes=rng.randint(2, 50)))
    reg: dict[str, Any] = {'status': 'OK', 'evidence': 'Backend found.', 'checked_at': _iso(now)}
    if extra.get('broken'):
      host = ('' if eng['location'] == 'global' else eng['location'] + '-') + 'discoveryengine.googleapis.com'
      reg = {'status': 'BACKEND_NOT_FOUND',
             'evidence': f"GET .../reasoningEngines/{backend.get('reasoning_engine_id')} returned HTTP 404.",
             'checked_at': _iso(now),
             'action': {'summary': 'Delete this registration or point it at a live Agent Engine.',
                        'delete_command': ('curl -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" '
                                           f'"https://{host}/v1alpha/{rn}"')}}
    agents.append({
        'agent_id': aid, 'resource_name': rn, 'display_name': name, 'description': DESCRIPTIONS.get(name, ''),
        'engine_id': eng['engine_id'], 'assistant_id': 'default_assistant', 'type': typ, 'type_label': TYPE_LABEL[typ],
        'state': extra.get('state', 'ENABLED'), 'sharing_scope': 'ALL_USERS', 'created': None, 'updated': None,
        'starter_prompts': 0, 'backend': backend,
        'observability_config': {'observability_enabled': idx % 3 == 0, 'sensitive_logging_enabled': False},
        'telemetry_scope': scope, 'metrics': m, 'data_sources': [], 'notes': [],
        'location': eng['location'], 'engine_key': eng['engine_key'], 'engine_display_name': eng['display_name'],
        'registration': reg,
    })
  return agents


def _diurnal(i: int, n: int, bucket_s: int, now: dt.datetime) -> float:
  """Business-hours shaped weight for trend bucket i."""
  t = now - dt.timedelta(seconds=bucket_s * (n - 1 - i))
  if bucket_s >= 86400:
    return 0.35 if t.weekday() >= 5 else 1.0
  hour = (t.hour - 7) % 24
  return 0.15 + 0.85 * max(0.0, math.sin(math.pi * (hour - 7) / 12.0)) if 7 <= hour <= 19 else 0.12


def build_trend(agents: list[dict[str, Any]], window_hours: int, now: dt.datetime) -> dict[str, Any]:
  from vibelift import fleet as ge_fleet  # pylint: disable=import-outside-toplevel
  bucket_s = ge_fleet.trend_bucket_seconds(window_hours)
  n = max(1, int(window_hours * 3600 // bucket_s))
  end = now.replace(minute=0, second=0)
  ends = [_iso(end - dt.timedelta(seconds=bucket_s * (n - 1 - i))) for i in range(n)]
  by_runtime = {}
  rng = _rng('trend')
  for a in agents:
    m = a['metrics']
    if not m.get('requests'):
      continue
    w = [_diurnal(i, n, bucket_s, now) * (0.85 + 0.3 * rng.random()) for i in range(n)]
    total = sum(w)
    e5 = [0] * n
    for k in range(m['errors_5xx'] or 0):  # server errors cluster in an incident
      e5[(n * 2 // 3 + (k % 3)) % n] += 1
    by_runtime[ge_fleet.runtime_backend_key(a)] = {
        'requests': [round(m['requests'] * x / total) for x in w],
        'errors_4xx': [round(m['errors_4xx'] * x / total) for x in w],
        'errors_5xx': e5,
    }
  return {'bucket_seconds': bucket_s, 'bucket_ends': ends, 'by_runtime': by_runtime, 'status': 'ok',
          'source': 'Cloud Monitoring request_count (Cloud Run + Agent Engine)'}


def model_usage(window_hours: int, now: dt.datetime, rate_cards: dict[str, dict[str, float]],
                previous: bool = False) -> dict[str, Any]:
  s = _scale(window_hours) * (0.86 if previous else 1.0)
  rows = [
      ('gemini-2.5-flash', 31200, 88e6, 4.9e6, 2.2e6, 46e6, 1.8e6),
      ('gemini-2.5-pro', 2950, 21e6 * (0.8 if previous else 1), 1.9e6, 3.1e6, 5.5e6, 0.4e6),
      ('gemini-3.1-flash-lite', 2660, 4.1e6, 0.29e6, 0, 2.6e6, 0.1e6),
      ('text-embedding-005', 9100, 3.3e6, 0, 0, 0, 0),  # no rate card: stays unpriced
  ]
  models = []
  totals: dict[str, Any] = {'input_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 0, 'cache_read_tokens': 0,
                            'cache_write_tokens': 0, 'invocations': 0, 'est_cost_usd': 0.0, 'models_without_rate_card': []}
  for name, inv, inp, out, rea, cr, cw in rows:
    r: dict[str, Any] = {
        'model': name, 'input_tokens': int(inp * s), 'output_tokens': int(out * s), 'reasoning_tokens': int(rea * s),
        'cache_read_tokens': int(cr * s), 'cache_write_tokens': int(cw * s), 'other_tokens': 0, 'invocations': int(inv * s)}
    card = rate_cards.get(name)
    if card:
      r['est_cost_usd'] = round((r['input_tokens'] * card['input'] + (r['output_tokens'] + r['reasoning_tokens']) * card['output']
                                 + r['cache_read_tokens'] * card['cached_read'] + r['cache_write_tokens'] * card['cache_write'])
                                / 1e6, 4)
      totals['est_cost_usd'] += r['est_cost_usd']
    else:
      r['est_cost_usd'] = None
      totals['models_without_rate_card'].append(name)
    prompt = r['input_tokens'] + r['cache_read_tokens']
    r['cache_read_share_pct'] = round(r['cache_read_tokens'] / prompt * 100, 1) if prompt else 0.0
    for k in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'invocations'):
      totals[k] += r[k]
    models.append(r)
  totals['est_cost_usd'] = round(totals['est_cost_usd'], 4)
  start = now - dt.timedelta(hours=window_hours * (2 if previous else 1))
  end = now - dt.timedelta(hours=window_hours if previous else 0)
  return {'scope': f'Project-wide Vertex AI model usage in {PROJECT}', 'models': models, 'totals': totals,
          'interval': {'start': _iso(start), 'end': _iso(end)},
          'rate_cards': {m['model']: rate_cards[m['model']] for m in models if m['model'] in rate_cards}}


def unregistered(window_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
  s = _scale(window_hours)
  runtimes = [
      {'agent_id': 'pricing-experiment', 'display_name': 'pricing-experiment-agent', 'type': 'ADK',
       'type_label': 'Standalone Agent Engine', 'location': 'us-central1',
       'description': 'ReasoningEngine not registered in any Gemini Enterprise app.',
       'backend': {'kind': 'agent_engine', 'reasoning_engine_id': '7716253540012345678', 'location': 'us-central1',
                   'resource': f'projects/{PROJECT_NUMBER}/locations/us-central1/reasoningEngines/7716253540012345678'},
       'metrics': {'requests': 0, 'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'vcpu_hours': round(24 * s, 1),
                   'memory_gib_hours': round(48 * s, 1), 'est_token_cost_usd': 0},
       'registration': {'status': 'UNREGISTERED_STANDALONE', 'finops_status': 'ZOMBIE_IDLE',
                        'evidence': '0 requests in window; still allocated.',
                        'action': {'summary': 'Delete if no longer needed.',
                                   'delete_command': ('gcloud ai reasoning-engines delete 7716253540012345678 '
                                                      f'--region=us-central1 --project={PROJECT}')}}},
      {'agent_id': 'jira-mcp', 'display_name': 'jira-mcp', 'type': 'MCP', 'type_label': 'Cloud Run MCP server',
       'location': 'us-central1', 'description': 'MCP server used by IT Service Desk.',
       'backend': {'kind': 'cloud_run', 'service': 'jira-mcp', 'region': 'us-central1', 'min_instances': 0, 'max_instances': 10},
       'metrics': {'requests': int(1120 * s), 'errors_5xx': int(4 * s), 'latency_p95_ms': 840.0, 'llm_calls': None,
                   'billable_instance_hours': round(6.2 * s, 1)},
       'registration': {'status': 'UNREGISTERED_STANDALONE', 'finops_status': 'ACTIVE_UNREGISTERED',
                        'evidence': 'Serving traffic; called as a tool by registered agents.',
                        'action': {'summary': 'Healthy dependency; no action needed.'}}},
      {'agent_id': 'legacy-chatbot', 'display_name': 'legacy-chatbot', 'type': 'A2A', 'type_label': 'Cloud Run service',
       'location': 'us-east4', 'description': 'min-instances=1 with no traffic.',
       'backend': {'kind': 'cloud_run', 'service': 'legacy-chatbot', 'region': 'us-east4', 'min_instances': 1, 'max_instances': 3},
       'metrics': {'requests': 0, 'billable_instance_hours': round(24 * s, 1)},
       'registration': {'status': 'UNREGISTERED_STANDALONE', 'finops_status': 'ALWAYS_ON_IDLE',
                        'evidence': 'min-instances=1 keeps an instance billed with 0 requests.',
                        'action': {'summary': 'Set min-instances to 0 or delete.',
                                   'command': ('gcloud run services update legacy-chatbot --min-instances=0 '
                                               f'--region=us-east4 --project={PROJECT}')}}},
  ]
  summary = {
      'total_unregistered_runtimes': 3, 'unregistered_reasoning_engines': 1, 'active_unregistered_reasoning_engines': 0,
      'zombie_reasoning_engines': 1, 'zombie_runtimes_count': 2, 'unregistered_cloud_run_services': 2,
      'mcp_cloud_run_services': 1, 'always_on_cloud_run_services': 1, 'gke_clusters_count': 0, 'gke_workloads_count': 0,
      'discovered_skills_and_mcp_count': 4, 'unregistered_requests': int(1120 * s), 'unregistered_llm_calls': 0,
      'unregistered_input_tokens': 0, 'unregistered_output_tokens': 0, 'unregistered_cached_tokens': 0,
      'unregistered_est_token_cost_usd': 0, 'unregistered_unpriced_runtimes': 0, 'zombie_vcpu_hours': round(24 * s, 1),
      'zombie_memory_gib_hours': round(48 * s, 1), 'unregistered_vcpu_hours': round(24 * s, 1),
      'unregistered_memory_gib_hours': round(48 * s, 1), 'unregistered_billable_instance_hours': round(30.2 * s, 1)}
  skills = [
      {'name': 'search_kb', 'kind': 'SKILL_OR_TOOL', 'parent_agent_name': 'IT Service Desk', 'calls': int(5100 * s),
       'errors': 3, 'avg_latency_ms': 410, 'co_occurring_input_tokens': int(11e6 * s),
       'co_occurring_output_tokens': int(0.6e6 * s), 'models': ['gemini-2.5-flash'], 'source': 'Cloud Trace execute_tool spans'},
      {'name': 'jira-mcp', 'kind': 'MCP_SERVER', 'parent_agent_name': 'IT Service Desk', 'calls': int(1120 * s), 'errors': 4,
       'avg_latency_ms': 620, 'endpoint_uri': f'https://jira-mcp-{PROJECT_NUMBER}.us-central1.run.app/mcp', 'models': [],
       'finops_note': 'Standalone Cloud Run service (not in GE).'},
      {'name': 'clause_extractor', 'kind': 'SUB_AGENT', 'parent_agent_name': 'Contract Analyzer', 'calls': int(9100 * s),
       'avg_latency_ms': 3900, 'co_occurring_input_tokens': int(28e6 * s), 'co_occurring_output_tokens': int(1.9e6 * s),
       'models': ['gemini-2.5-flash'], 'finops_note': 'Invoked ~41x per request: check for a loop.'},
      {'name': 'lookup_order', 'kind': 'SKILL_OR_TOOL', 'parent_agent_name': 'Support Triage', 'calls': int(3100 * s),
       'avg_latency_ms': 210, 'models': ['gemini-2.5-flash']},
  ]
  return runtimes, summary, skills


class FixtureFleet:
  """Stands in for GeminiEnterpriseFleetService: same collect() payload, fixture data, no API calls."""

  project_id = PROJECT
  location = 'global'
  collection = 'default_collection'
  default_window_hours = 24

  def __init__(self) -> None:
    self.engine_ids = [e['engine_key'] for e in ENGINES]
    from vibelift import fleet as ge_fleet  # pylint: disable=import-outside-toplevel
    self._ge_fleet = ge_fleet
    # Only used for its list-price rate cards; api=None means it never calls Google Cloud.
    self._pricing = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT, engine_ids=['acme-intranet'], location='global', collection='default_collection', api=None)

  def rate_cards(self) -> dict[str, dict[str, float]]:
    return self._pricing.rate_cards()

  def _trigger_async_refresh(self, hours: int) -> None:  # pylint: disable=unused-argument
    return None

  def enable_agent_observability(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
    raise self._ge_fleet.InvalidAgentRequestError('Preview mode: trace logging is not changed on fixture data.')

  def collect(self, window_hours: Any = None, force_refresh: bool = False, allow_stale: bool = False,
              max_wait_s: float | None = None) -> dict[str, Any]:
    del force_refresh, allow_stale, max_wait_s
    now = _utcnow()
    hours = self._ge_fleet.parse_window_hours(window_hours) or self.default_window_hours
    agents = build_agents(hours, now)
    runtimes, unregistered_summary, skills = unregistered(hours)
    engines = [dict(e, agents_count=sum(1 for a in agents if a['engine_key'] == e['engine_key'])) for e in ENGINES]
    unique, seen = [], set()
    for a in agents:  # shared runtimes are counted once, like the real collector
      key = self._ge_fleet.runtime_backend_key(a)
      if key not in seen:
        seen.add(key)
        unique.append(a)
    totals: dict[str, Any] = {
        'agents': len(agents), 'enabled': sum(1 for a in agents if a['state'] == 'ENABLED'), 'by_type': {},
        'with_runtime_telemetry': sum(1 for a in agents if a['metrics']['requests'] is not None),
        'unique_runtimes': len(unique),
        'broken_registrations': sum(1 for a in agents if a['registration']['status'] in ('BACKEND_NOT_FOUND', 'NO_BACKEND')),
        'unverified_registrations': 0}
    for a in agents:
      totals['by_type'][a['type']] = totals['by_type'].get(a['type'], 0) + 1
    for k in ('requests', 'errors_4xx', 'errors_5xx', 'llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens',
              'conversations'):
      vals = [a['metrics'][k] for a in unique if a['metrics'].get(k) is not None]
      totals[k] = sum(vals) if vals else None
    for k in ('vcpu_hours', 'memory_gib_hours', 'billable_instance_hours'):
      vals = [a['metrics'][k] for a in unique if a['metrics'].get(k) is not None]
      totals[k] = round(sum(vals), 4) if vals else None
    totals['error_rate_pct'] = round(totals['errors_5xx'] / totals['requests'] * 100, 2) if totals['requests'] else None
    totals['last_activity'] = max(a['metrics']['last_activity'] for a in agents if a['metrics']['last_activity'])
    s = _scale(hours)
    by_engine = {
        'global/acme-intranet': {'engine_id': 'acme-intranet', 'llm_calls': int(6100 * s), 'input_tokens': int(31e6 * s),
                                 'output_tokens': int(1.7e6 * s), 'cached_tokens': int(12e6 * s),
                                 'conversations': int(2300 * s), 'models': ['gemini-2.5-flash'],
                                 'assistant_agents': ['default_assistant']},
        'us/acme-support': {'engine_id': 'acme-support', 'llm_calls': int(2900 * s), 'input_tokens': int(12e6 * s),
                            'output_tokens': int(0.8e6 * s), 'cached_tokens': int(4e6 * s), 'conversations': int(1100 * s),
                            'models': ['gemini-2.5-flash'], 'assistant_agents': ['default_assistant']},
    }
    assistant_usage = {
        'by_engine': by_engine, 'traces_scanned': 1800, 'truncated': False,
        'scope': f'Gemini Enterprise assistant model calls exported to Cloud Trace in {PROJECT}',
        'totals': {f: sum(v[f] for v in by_engine.values())
                   for f in ('llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens', 'conversations')},
    }
    cards = self.rate_cards()
    return {
        'source': 'gemini_enterprise', 'project_id': PROJECT, 'location': 'global', 'engines': engines,
        'window_hours': hours, 'generated_at': _iso(now), 'collection_ms': 2140, 'cache_ttl_seconds': 60,
        'agents': agents, 'totals': totals, 'unregistered_runtimes': runtimes,
        'gke_workloads': {'clusters': [], 'workloads': []}, 'skills_and_mcp': skills,
        'unregistered_summary': unregistered_summary,
        'model_usage': model_usage(hours, now, cards), 'model_usage_previous': model_usage(hours, now, cards, previous=True),
        'ge_traffic': {'assistant_requests': int(6420 * s),
                       'scope': f'Project-wide Gemini Enterprise assistant (StreamAssist) calls in {PROJECT}'},
        'ge_assistant_usage': assistant_usage, 'trend': build_trend(agents, hours, now),
        'token_log_scan': {'entries_scanned': 2400, 'truncated': False, 'traces_scanned': 1800, 'traces_truncated': False},
        'source_status': {'inventory': 'ok', 'reasoning_engines_inventory': 'ok', 'cloud_run_inventory': 'ok',
                          'gke_inventory': 'n/a', 'agent_engine_metrics': 'ok', 'agent_token_logs': 'ok',
                          'agent_token_traces': 'ok', 'skills_and_mcp_traces': 'ok', 'ge_assistant_tokens': 'ok',
                          'cloud_run_metrics': 'ok', 'model_usage': 'ok', 'ge_traffic': 'ok'},
        'errors': [],
        'notes': ['Cloud Monitoring data points typically appear 1-3 minutes after the request.',
                  'A2A agents on Cloud Run report service-level metrics (all traffic to the service).'],
    }


# ---------------------------------------------------------------------------------------------------
# BigQuery (vibelift_mart) fixture rows, keyed by the SQL builder that would have produced the query
# ---------------------------------------------------------------------------------------------------
def bq_rows(kind: str, now: dt.datetime) -> list[dict[str, Any]]:
  rng = _rng(kind)
  if kind == 'build_user_engine_rollup_sql':
    rows: list[dict[str, Any]] = []
    for u, ek, sess, _ in USERS:
      rows.append({'user_email': f'{u}@{DOMAIN}', 'engine_key': ek, 'sessions': str(sess), 'interactions': str(sess * 4),
                   'total_tokens': str(sess * rng.randint(38000, 120000)),
                   'reasoning_tokens': str(sess * rng.randint(900, 4000)),
                   'last_seen': _iso(now - dt.timedelta(minutes=rng.randint(3, 600)))})
    rows.append({'user_email': f'vibe-lift-runtime-sa@{PROJECT}.iam.gserviceaccount.com', 'engine_key': 'global/acme-intranet',
                 'sessions': '0', 'interactions': '0', 'total_tokens': None, 'reasoning_tokens': None, 'last_seen': _iso(now)})
    return rows
  if kind == 'build_audit_principals_sql':
    return [{'principal': f'{u}@{DOMAIN}', 'method_name': 'google.cloud.discoveryengine.v1alpha.AssistantService.StreamAssist',
             'call_count': str(sess * 5), 'last_seen': _iso(now), 'engine_key': ek} for u, ek, sess, _ in USERS]
  if kind == 'build_user_model_tokens_sql':
    rows = []
    for u, ek, sess, agent in USERS:
      model = 'gemini-2.5-pro' if agent == 'Code Review Copilot' else (
          'gemini-3.1-flash-lite' if ek.startswith('eu') else 'gemini-2.5-flash')
      inp = sess * rng.randint(30000, 90000)
      rows.append({'user_email': f'{u}@{DOMAIN}', 'model_name': model, 'engine_key': ek, 'turns': str(sess * 4),
                   'input_tokens': str(inp), 'output_tokens': str(inp // 18), 'cached_input_tokens': str(inp // 2),
                   'reasoning_tokens': str(inp // 40), 'total_tokens': str(inp + inp // 18)})
    return rows
  if kind in ('build_daily_totals_sql', 'build_daily_usage_sql'):
    rows = []
    for d in range(30, 0, -1):
      day = (now - dt.timedelta(days=d - 1)).date()
      weekday_factor = 0.35 if day.weekday() >= 5 else 1.0
      inter = int(9800 * weekday_factor * (1 + (30 - d) * 0.012) * (0.9 + 0.2 * rng.random()))
      base = {'day': str(day), 'interactions': str(inter), 'chat_turns': str(int(inter * 0.72)),
              'searches': str(int(inter * 0.2)), 'agent_calls': str(int(inter * 0.08)), 'sessions': str(int(inter / 4.1)),
              'active_users': str(int(inter / 19)), 'failed_turns': str(int(inter * (0.006 + (0.02 if d == 6 else 0)))),
              'guardrail_blocks': str(int(inter * 0.001)), 'turns_with_tokens': str(int(inter * 0.7)),
              'llm_calls': str(int(inter * 2.4)), 'input_tokens': str(inter * 9100), 'output_tokens': str(inter * 520),
              'cached_input_tokens': str(inter * 4300), 'reasoning_tokens': str(inter * 210),
              'total_tokens': str(inter * 9620), 'refreshed_at': _iso(now - dt.timedelta(minutes=37))}
      if kind == 'build_daily_usage_sql':
        for ek, agent, model, share in (('global/acme-intranet', 'IT Service Desk', 'gemini-2.5-flash', 0.45),
                                        ('us/acme-support', 'Support Triage', 'gemini-2.5-flash', 0.35),
                                        ('eu/acme-eu-people', 'HR Policy Q&A', 'gemini-3.1-flash-lite', 0.2)):
          r = {k: (v if k in ('day', 'refreshed_at') else str(int(int(v) * share))) for k, v in base.items()}
          r.update({'engine_key': ek, 'agent_name': agent, 'model_name': model})
          rows.append(r)
      else:
        rows.append(base)
    return rows
  if kind == 'build_recent_sessions_sql':
    rows = []
    for i in range(40):
      u, ek, _, agent = USERS[i % len(USERS)]
      end = now - dt.timedelta(minutes=17 * i + rng.randint(0, 9))
      dur = rng.randint(40, 1900)
      turns = rng.randint(1, 9)
      inp = turns * rng.randint(5000, 21000)
      rows.append({'engine_key': ek, 'session_id': str(6446120131357637190 + i * 7741),
                   'session_start': _iso(end - dt.timedelta(seconds=dur)), 'session_end': _iso(end),
                   'duration_seconds': str(dur), 'session_date': str(end.date()), 'user_email': f'{u}@{DOMAIN}',
                   'agent_name': agent, 'model_names': 'gemini-2.5-flash', 'turns': str(turns), 'chat_turns': str(turns),
                   'failed_turns': str(1 if i % 11 == 3 else 0), 'actionable_issues': '0', 'guardrail_blocks': '0',
                   'turns_with_tokens': str(turns), 'input_tokens': str(inp), 'output_tokens': str(inp // 16),
                   'cached_input_tokens': str(inp // 2), 'reasoning_tokens': str(inp // 50),
                   'total_tokens': str(inp + inp // 16), 'llm_calls': str(turns * 2), 'tool_calls': str(turns),
                   'tool_failures': '0'})
    return rows
  if kind == 'build_session_turns_sql':
    rows = []
    for i in range(40):
      u, ek, _, agent = USERS[i % len(USERS)]
      sid = str(6446120131357637190 + i * 7741)
      for t in range(3):
        inp = rng.randint(4000, 16000)
        rows.append({'engine_key': ek, 'session_id': sid, 'turn_id': f'{sid}-{t}', 'turn_kind': 'CHAT',
                     'turn_status': 'SUCCESS', 'ts': _iso(now - dt.timedelta(minutes=17 * i, seconds=-60 * t)),
                     'user_email': f'{u}@{DOMAIN}', 'agent_name': agent, 'model_name': 'gemini-2.5-flash',
                     'latency_ms': str(rng.randint(600, 4200)), 'input_tokens': str(inp), 'output_tokens': str(inp // 15),
                     'cached_input_tokens': str(inp // 2), 'reasoning_tokens': str(inp // 60),
                     'total_tokens': str(inp + inp // 15), 'llm_calls': '2', 'tool_call_count': '1',
                     'tool_names': 'search_kb'})
    return rows
  if kind == 'build_recent_turns_sql':
    return [{'turn_id': f't-{i}', 'ts': _iso(now - dt.timedelta(minutes=3 * i)), 'session_id': str(6446120131357637190 + i),
             'user_email': f'{USERS[i % 10][0]}@{DOMAIN}', 'turn_status': 'SUCCESS', 'turn_source': 'GE',
             'engine_key': USERS[i % 10][1], 'api_method': 'StreamAssist', 'agent_name': USERS[i % 10][3],
             'model_name': 'gemini-2.5-flash', 'latency_ms': str(800 + 90 * i), 'total_tokens': str(9000 + 400 * i),
             'input_tokens': str(8500 + 380 * i), 'output_tokens': str(500 + 20 * i), 'cached_input_tokens': '4000',
             'reasoning_tokens': '120', 'tool_names': 'search_kb', 'tool_call_count': '1'} for i in range(20)]
  return []


def _sql_marker(name: str):
  def build(*args: Any, **kwargs: Any) -> str:
    del args, kwargs
    return _SQL_MARKER + name
  return build


def fake_bigquery(sql: Any, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
  """Replaces GoogleCloudTelemetryService._query_bigquery_rest: answers the marked mart queries only."""
  del args, kwargs
  text = str(sql)
  return bq_rows(text[len(_SQL_MARKER):], _utcnow()) if text.startswith(_SQL_MARKER) else []


# ---------------------------------------------------------------------------------------------------
# Cloud Billing export fixture
# ---------------------------------------------------------------------------------------------------
def billing_summary(non_blocking: bool = False) -> dict[str, Any]:
  del non_blocking
  from vibelift import billing_export  # pylint: disable=import-outside-toplevel
  rows = [
      {'sku_id': 'A1B2-C3D4', 'service': 'Vertex AI', 'sku_description': 'Gemini 2.5 Flash input tokens',
       'usage_amount': 2.6e9, 'usage_unit': 'tokens', 'gross_usd': 790.12, 'credits_usd': -41.0, 'currency': 'USD'},
      {'sku_id': 'A1B2-C3D5', 'service': 'Vertex AI', 'sku_description': 'Gemini 2.5 Flash output tokens',
       'usage_amount': 1.5e8, 'usage_unit': 'tokens', 'gross_usd': 371.9, 'credits_usd': 0, 'currency': 'USD'},
      {'sku_id': 'B9F0-1122', 'service': 'Vertex AI', 'sku_description': 'Gemini 2.5 Pro input tokens',
       'usage_amount': 6.3e8, 'usage_unit': 'tokens', 'gross_usd': 788.4, 'credits_usd': 0, 'currency': 'USD'},
      {'sku_id': 'C7D8-3344', 'service': 'Discovery Engine', 'sku_description': 'Gemini Enterprise Standard seats',
       'usage_amount': 120, 'usage_unit': 'seat-month', 'gross_usd': 3600.0, 'credits_usd': 0, 'currency': 'USD'},
      {'sku_id': 'D1E2-5566', 'service': 'Cloud Run', 'sku_description': 'CPU allocation time',
       'usage_amount': 410000, 'usage_unit': 'vCPU-s', 'gross_usd': 9.84, 'credits_usd': 0, 'currency': 'USD'},
  ]
  return billing_export.summarize_rows(rows, BILLING_TABLE)


def billing_daily(non_blocking: bool = False, window_days: int = 30) -> dict[str, Any]:
  del non_blocking
  from vibelift import billing_export  # pylint: disable=import-outside-toplevel
  rng = _rng('billing_daily')
  now = _utcnow()
  rows = []
  for d in range(30, 1, -1):  # the export lags about a day
    day = (now - dt.timedelta(days=d - 1)).date()
    weekday_factor = 0.4 if day.weekday() >= 5 else 1.0
    gross = round(64 * weekday_factor * (1 + (30 - d) * 0.012) * (0.9 + 0.2 * rng.random()), 4)
    rows.append({'day': str(day), 'service': 'Vertex AI', 'gross_usd': gross, 'credits_usd': -1.2, 'currency': 'USD'})
    rows.append({'day': str(day), 'service': 'Discovery Engine', 'gross_usd': 120.0, 'credits_usd': 0, 'currency': 'USD'})
    rows.append({'day': str(day), 'service': 'Cloud Run', 'gross_usd': 0.33, 'credits_usd': 0, 'currency': 'USD'})
  return billing_export.summarize_daily_cost_rows(rows, BILLING_TABLE, window_days)


# ---------------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------------
def add_banner(html: str) -> str:
  """Puts the "fictional data" banner right after <body ...>, once."""
  if BANNER_ID in html:
    return html
  start = html.find('<body')
  end = html.find('>', start) if start >= 0 else -1
  if end < 0:
    return html
  banner = (f'<div id="{BANNER_ID}" role="status" style="position:sticky;top:0;z-index:2000;background:#7c2d12;'
            'color:#fff;padding:7px 14px;font:600 12.5px/1.4 system-ui,sans-serif;text-align:center;">'
            f'{BANNER_TEXT}</div>')
  return html[:end + 1] + banner + html[end + 1:]


@contextlib.contextmanager
def fixture_installed(controller: Any) -> Iterator[Any]:
  """Points a VibeLiftRuntimeController at the fixtures; everything is restored on exit."""
  from vibelift import ge_mart  # pylint: disable=import-outside-toplevel
  from vibelift.ui import template as ui_template  # pylint: disable=import-outside-toplevel
  telemetry_service = controller.gcp_telemetry
  real_fetch = telemetry_service.fetch_live_bigquery_project_insights
  real_render = ui_template.render_dashboard_html

  def fetch_insights(force_refresh: bool = False, non_blocking: bool = False, window_hours: Any = None) -> Any:
    del non_blocking  # the fixture answers instantly, so always block (no "loading" state)
    return real_fetch(force_refresh=force_refresh, non_blocking=False, window_hours=window_hours)

  def render_with_banner(initial_state: Any = None) -> str:
    return add_banner(real_render(initial_state))

  with contextlib.ExitStack() as stack:
    for name in MART_SQL_BUILDERS:
      stack.enter_context(mock.patch.object(ge_mart, name, _sql_marker(name)))
    stack.enter_context(mock.patch.object(controller, 'ge_fleet', FixtureFleet()))
    stack.enter_context(mock.patch.object(telemetry_service, '_query_bigquery_rest', fake_bigquery))
    stack.enter_context(mock.patch.object(telemetry_service, '_get_access_token', lambda *a, **k: 'preview-fixture'))
    stack.enter_context(mock.patch.object(telemetry_service, 'fetch_live_bigquery_project_insights', fetch_insights))
    stack.enter_context(mock.patch.object(telemetry_service, 'list_cloud_run_agent_services', lambda *a, **k: []))
    stack.enter_context(mock.patch.object(telemetry_service, 'fetch_gemini_enterprise_support_telemetry',
                                          lambda *a, **k: []))
    stack.enter_context(mock.patch.object(controller.billing_export, 'get', billing_summary))
    stack.enter_context(mock.patch.object(controller.billing_export, 'get_daily_ai_costs', billing_daily))
    stack.enter_context(mock.patch.object(ui_template, 'render_dashboard_html', render_with_banner))
    yield controller


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'port on 127.0.0.1 (default {DEFAULT_PORT})')
  args = parser.parse_args(argv)
  # Before vibelift.server is imported: never pick up real credentials, and name the fictional project.
  os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/nonexistent/vibelift-preview-credentials.json'
  os.environ['GOOGLE_CLOUD_PROJECT'] = PROJECT
  os.environ.setdefault('VIBELIFT_FLEET_TTL_SECONDS', '600')
  from vibelift import server  # pylint: disable=import-outside-toplevel
  with fixture_installed(server._global_controller):  # pylint: disable=protected-access
    print(f'VibeLift preview (fictional fixture data) on http://127.0.0.1:{args.port}/  (Ctrl+C to stop)', flush=True)
    server.run_standalone_server(port=args.port, host='127.0.0.1')


if __name__ == '__main__':
  main()
