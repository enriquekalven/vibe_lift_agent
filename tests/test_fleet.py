"""Offline tests for ge_fleet: inventory, telemetry joins, aggregation, degradation, and caching.

`FakeGoogleApi` stands in for `ge_fleet._GoogleApi`. It serves canned Discovery Engine, Cloud
Monitoring, Cloud Logging, and Vertex AI responses shaped like the real APIs, mirroring the
`agent-platform-demo` app (one ADK agent, one A2A agent on Cloud Run, one Google-managed agent).
"""

import json
import re
import threading
import unittest
import urllib.parse

from finops_dashboard import finops as live_finops
from finops_dashboard import fleet as ge_fleet
from finops_dashboard import telemetry

PROJECT = 'test-project'
ENGINE = 'agent-platform-demo'
RE_PROJECT = '123456789'
RE_ID = '1588369340892184576'
RE_RESOURCE = f'projects/{RE_PROJECT}/locations/us-central1/reasoningEngines/{RE_ID}'
ENGINE_PATH = f'/v1alpha/projects/{PROJECT}/locations/global/collections/default_collection/engines/{ENGINE}'
AGENT_PREFIX = f'{ENGINE_PATH[len("/v1alpha/"):]}/assistants/default_assistant/agents'
SECRET = 'SECRET PROMPT TEXT'

ADK_AGENT = {
    'name': f'{AGENT_PREFIX}/13791105764600209034',
    'displayName': 'IT Service Desk',
    'description': 'Resolves IT tickets.',
    'state': 'ENABLED',
    'sharingConfig': {'scope': 'ALL_USERS'},
    'adkAgentDefinition': {'provisionedReasoningEngine': {'reasoningEngine': RE_RESOURCE}},
}
A2A_AGENT = {
    'name': f'{AGENT_PREFIX}/1234567890123456789',
    'displayName': 'FinOps & Tokenomics Analytics',
    'state': 'ENABLED',
    'a2aAgentDefinition': {
        'jsonAgentCard': json.dumps({'name': 'finops_dashboard', 'url': 'https://finops-dashboard-mcp-app-abcde12345-uc.a.run.app/a2a/app'}),
    },
}
MANAGED_AGENT = {
    'name': f'{AGENT_PREFIX}/deep_research',
    'displayName': 'Deep Research',
    'state': 'ENABLED',
    'managedAgentDefinition': {},
}


def _series(resource_labels, metric_labels, value):
  return {'resource': {'labels': resource_labels}, 'metric': {'labels': metric_labels}, 'points': [{'value': value}]}


def _log_entry(engine_id, input_tokens, output_tokens, conversation, stamp, cached=None):
  labels = {
      'gen_ai.usage.input_tokens': str(input_tokens),
      'gen_ai.usage.output_tokens': str(output_tokens),
      'gen_ai.conversation.id': conversation,
      'gen_ai.response.model': 'gemini-2.5-flash',
      'gen_ai.input.messages': SECRET,  # Message content must never reach the payload.
  }
  if cached is not None:
    labels['gen_ai.usage.cache_read_input_tokens'] = str(cached)
  return {
      'timestamp': stamp,
      'resource': {'type': 'aiplatform.googleapis.com/ReasoningEngine', 'labels': {'reasoning_engine_id': engine_id}},
      'labels': labels,
  }


class FakeGoogleApi:
  """Routes Google REST URLs to canned responses; fails any call whose decoded URL/body matches."""

  def __init__(self, fail_on=()):
    self.fail_on = tuple(fail_on)
    self.calls = []
    self.alignment_periods = []
    self._lock = threading.Lock()

  def call(self, method, url, body=None):
    with self._lock:
      self.calls.append((method, url, body))
    target = urllib.parse.unquote_plus(url) + json.dumps(body or {})
    for needle in self.fail_on:
      if needle in target:
        raise ge_fleet.FleetSourceError(403, f'Permission denied (fake) for {needle or "all calls"}')
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    if parts.netloc == 'discoveryengine.googleapis.com':
      return self._discovery(parts.path, query)
    if parts.netloc == 'monitoring.googleapis.com':
      return self._monitoring(parts.path, query)
    if parts.netloc == 'logging.googleapis.com':
      return self._logging(body or {})
    if parts.netloc == 'us-central1-aiplatform.googleapis.com':
      return {'displayName': 'service-desk-agent', 'spec': {'agentFramework': 'google-adk'}}
    if parts.netloc == 'cloudtrace.googleapis.com':
      return {'traces': []}  # Agents in this fixture report tokens through Cloud Logging.
    if parts.netloc == 'run.googleapis.com':
      return {'services': [{'name': f'projects/{PROJECT}/locations/us-central1/services/finops-dashboard-mcp-app'}]}
    raise ge_fleet.FleetSourceError(404, f'Unexpected URL {url}')

  def count(self, fragment):
    with self._lock:
      return sum(1 for _, url, _ in self.calls if fragment in urllib.parse.unquote_plus(url))

  def _discovery(self, path, query):
    if path == ENGINE_PATH:
      return {'name': ENGINE_PATH, 'displayName': 'GB Agent Platform Demo', 'appType': 'APP_TYPE_INTRANET'}
    if path == f'{ENGINE_PATH}/assistants':
      return {'assistants': [{'name': f'{ENGINE_PATH[len("/v1alpha/"):]}/assistants/default_assistant'}]}
    if path == f'{ENGINE_PATH}/assistants/default_assistant/agents':
      if query.get('pageToken') == ['page-2']:
        return {'agents': [MANAGED_AGENT]}
      return {'agents': [ADK_AGENT, A2A_AGENT], 'nextPageToken': 'page-2'}
    raise ge_fleet.FleetSourceError(404, f'Unexpected Discovery Engine path {path}')

  def _monitoring(self, path, query):
    with self._lock:
      self.alignment_periods.append(query.get('aggregation.alignmentPeriod', ['raw'])[0])
    project = path.split('/')[3]
    metric = re.search(r'metric\.type="([^"]+)"', query['filter'][0]).group(1)
    p50 = query.get('aggregation.crossSeriesReducer', [''])[0].endswith('_50')
    engine, service = {'reasoning_engine_id': RE_ID}, {'service_name': 'finops-dashboard-mcp-app'}
    series = []
    if project == RE_PROJECT and metric.startswith('aiplatform.googleapis.com/reasoning_engine/'):
      suffix = metric.rsplit('reasoning_engine/', 1)[1]
      if suffix == 'request_count':
        series = [_series(engine, {'response_code_class': c}, {'int64Value': n}) for c, n in (('2xx', '40'), ('4xx', '3'), ('5xx', '2'))]
      elif suffix == 'request_latencies':
        series = [_series(engine, {}, {'doubleValue': 1500.0 if p50 else 9000.0})]
      elif suffix == 'cpu/allocation_time':
        series = [_series(engine, {}, {'doubleValue': 7200.0})]
      elif suffix == 'memory/allocation_time':
        series = [_series(engine, {}, {'doubleValue': 3600.0})]
    elif project == PROJECT and metric == 'run.googleapis.com/request_count':
      series = [_series(service, {'response_code_class': c}, {'int64Value': n}) for c, n in (('2xx', '10'), ('4xx', '5'))]
    elif project == PROJECT and metric == 'run.googleapis.com/request_latencies':
      series = [_series(service, {}, {'doubleValue': 120.0 if p50 else 800.0})]
    elif project == PROJECT and metric == 'run.googleapis.com/container/billable_instance_time':
      series = [_series(service, {}, {'doubleValue': 1800.0})]
    elif project == PROJECT and metric == 'aiplatform.googleapis.com/publisher/online_serving/token_count':
      series = [
          _series({'model_user_id': 'gemini-2.5-flash'}, {'type': 'input'}, {'int64Value': '1000000'}),
          _series({'model_user_id': 'gemini-2.5-flash'}, {'type': 'output'}, {'int64Value': '100000'}),
          _series({'model_user_id': 'gemini-2.5-flash'}, {'type': 'cached_input'}, {'int64Value': '200000'}),
          _series({'model_user_id': 'claude-opus-5'}, {'type': 'input'}, {'int64Value': '1000'}),
          _series({'model_user_id': 'claude-opus-5'}, {'type': 'output'}, {'int64Value': '100'}),
      ]
    elif project == PROJECT and metric == 'aiplatform.googleapis.com/publisher/online_serving/model_invocation_count':
      series = [_series({'model_user_id': 'gemini-2.5-flash'}, {}, {'int64Value': '50'}),
                _series({'model_user_id': 'claude-opus-5'}, {}, {'int64Value': '2'})]
    elif project == PROJECT and metric == 'serviceruntime.googleapis.com/api/request_count':
      series = [
          _series({'method': 'google.cloud.discoveryengine.v1alpha.AssistantService.StreamAssist'}, {}, {'int64Value': '7'}),
          _series({'method': 'google.cloud.discoveryengine.v1alpha.EngineService.GetEngine'}, {}, {'int64Value': '3'}),
      ]
    return {'timeSeries': series} if series else {}

  def _logging(self, body):
    if body.get('pageToken') == 'logs-2':
      return {'entries': [_log_entry(RE_ID, 300, 30, 'conv-2', '2026-09-25T10:00:00Z')]}
    return {
        'entries': [
            _log_entry(RE_ID, 100, 10, 'conv-1', '2026-09-25T09:00:00Z'),
            _log_entry(RE_ID, 200, 20, 'conv-1', '2026-09-25T11:00:00Z', cached=50),
            _log_entry('999', 5000, 500, 'conv-x', '2026-09-25T12:00:00Z'),  # Not in the app: ignored.
        ],
        'nextPageToken': 'logs-2',
    }


def make_fake_service(fail_on=()):
  """Returns (service, fake_api) wired to the offline fake."""
  api = FakeGoogleApi(fail_on=fail_on)
  service = ge_fleet.GeminiEnterpriseFleetService(
      project_id=PROJECT, engine_ids=[ENGINE], location='global', collection='default_collection', api=api)
  return service, api


def _by_name(payload):
  return {a['display_name']: a for a in payload['agents']}


class GeFleetCollectionTest(unittest.TestCase):

  def test_inventory_lists_every_agent_across_pages(self):
    service, api = make_fake_service()
    payload = service.collect(window_hours=24)
    agents = _by_name(payload)
    self.assertEqual(sorted(agents), ['Deep Research', 'FinOps & Tokenomics Analytics', 'IT Service Desk'])
    self.assertEqual(api.count('pageToken=page-2'), 1)
    self.assertEqual(payload['engines'], [{
        'engine_id': ENGINE, 'engine_key': f'global/{ENGINE}', 'location': 'global',
        'display_name': 'GB Agent Platform Demo', 'app_type': 'APP_TYPE_INTRANET', 'agents_count': 3}])
    self.assertEqual({a['engine_key'] for a in payload['agents']}, {f'global/{ENGINE}'})
    self.assertEqual({a['location'] for a in payload['agents']}, {'global'})
    self.assertEqual(agents['IT Service Desk']['type'], 'ADK')
    self.assertEqual(agents['FinOps & Tokenomics Analytics']['type'], 'A2A')
    self.assertEqual(agents['Deep Research']['type'], 'MANAGED')
    self.assertEqual(payload['source'], 'gemini_enterprise')
    self.assertEqual(payload['window_hours'], 24)

  def test_adk_agent_joined_with_engine_metrics_and_token_logs(self):
    service, api = make_fake_service()
    agent = _by_name(service.collect(window_hours=24))['IT Service Desk']
    m = agent['metrics']
    self.assertEqual(agent['telemetry_scope'], 'agent')
    self.assertEqual((m['requests'], m['errors_4xx'], m['errors_5xx']), (45, 3, 2))
    self.assertEqual(m['error_rate_pct'], 4.44)
    self.assertEqual((m['latency_p50_ms'], m['latency_p95_ms']), (1500.0, 9000.0))
    self.assertEqual((m['vcpu_hours'], m['memory_gib_hours']), (2.0, 1.0))
    self.assertEqual((m['llm_calls'], m['input_tokens'], m['output_tokens'], m['cached_tokens']), (3, 600, 60, 50))
    self.assertEqual(m['conversations'], 2)
    self.assertEqual(m['last_activity'], '2026-09-25T11:00:00Z')
    self.assertEqual(agent['backend']['framework'], 'google-adk')
    self.assertEqual(agent['backend']['display_name'], 'service-desk-agent')
    self.assertEqual(agent['backend']['models'], ['gemini-2.5-flash'])
    # Agent Engine telemetry is read from the project that owns the reasoning engine.
    self.assertGreater(api.count(f'/v3/projects/{RE_PROJECT}/timeSeries'), 0)

  def test_a2a_agent_joined_with_cloud_run_service_metrics(self):
    service, _ = make_fake_service()
    agent = _by_name(service.collect(window_hours=24))['FinOps & Tokenomics Analytics']
    m = agent['metrics']
    self.assertEqual(agent['backend']['kind'], 'cloud_run')
    self.assertEqual(agent['backend']['service'], 'finops-dashboard-mcp-app')
    self.assertEqual(agent['telemetry_scope'], 'service')
    self.assertEqual((m['requests'], m['errors_4xx'], m['errors_5xx'], m['error_rate_pct']), (15, 5, 0, 0.0))
    self.assertEqual((m['latency_p50_ms'], m['latency_p95_ms']), (120.0, 800.0))
    self.assertEqual(m['billable_instance_hours'], 0.5)
    self.assertIsNone(m['llm_calls'])

  def test_managed_agent_is_inventory_only(self):
    service, _ = make_fake_service()
    agent = _by_name(service.collect(window_hours=24))['Deep Research']
    self.assertEqual(agent['telemetry_scope'], 'none')
    self.assertTrue(all(v is None for v in agent['metrics'].values()))

  def test_fleet_totals_model_usage_and_ge_traffic(self):
    service, _ = make_fake_service()
    payload = service.collect(window_hours=24)
    t = payload['totals']
    self.assertEqual((t['agents'], t['enabled'], t['with_runtime_telemetry']), (3, 3, 2))
    self.assertEqual(t['by_type'], {'ADK': 1, 'A2A': 1, 'MANAGED': 1})
    self.assertEqual((t['requests'], t['errors_4xx'], t['errors_5xx'], t['error_rate_pct']), (60, 8, 2, 3.33))
    self.assertEqual((t['llm_calls'], t['input_tokens'], t['output_tokens'], t['conversations']), (3, 600, 60, 2))
    self.assertEqual(t['last_activity'], '2026-09-25T11:00:00Z')

    usage = payload['model_usage']
    flash, opus = usage['models']
    card = telemetry.RATE_CARDS['gemini-2.5-flash']
    expected = round(1.0 * card.input_per_million_usd + 0.1 * card.output_per_million_usd
                     + 0.2 * card.cached_read_per_million_usd, 4)
    self.assertEqual(flash['model'], 'gemini-2.5-flash')
    self.assertEqual((flash['input_tokens'], flash['output_tokens'], flash['cache_read_tokens'], flash['invocations']),
                     (1000000, 100000, 200000, 50))
    self.assertAlmostEqual(flash['est_cost_usd'], expected)
    self.assertEqual(flash['cache_read_share_pct'], 16.7)
    self.assertEqual(opus['model'], 'claude-opus-5')
    self.assertIsNone(opus['est_cost_usd'])
    self.assertEqual(usage['totals']['models_without_rate_card'], ['claude-opus-5'])
    self.assertAlmostEqual(usage['totals']['est_cost_usd'], expected)
    self.assertEqual(usage['totals']['invocations'], 52)
    self.assertEqual(payload['ge_traffic']['assistant_requests'], 7)
    self.assertEqual(payload['token_log_scan'], {'entries_scanned': 4, 'truncated': False,
                                                 'traces_scanned': 0, 'traces_truncated': False})
    agents = _by_name(payload)
    self.assertEqual(agents['IT Service Desk']['registration']['status'], 'OK')
    self.assertEqual(agents['FinOps & Tokenomics Analytics']['registration']['status'], 'OK')
    self.assertEqual(agents['Deep Research']['registration']['status'], 'NOT_CHECKED')
    self.assertEqual(payload['totals']['broken_registrations'], 0)
    self.assertIn('model_usage_previous', payload)
    self.assertEqual(payload['errors'], [])
    self.assertEqual(set(payload['source_status'].values()), {'ok'})

  def test_log_message_content_never_reaches_payload(self):
    service, _ = make_fake_service()
    self.assertNotIn(SECRET, json.dumps(service.collect(window_hours=24)))

  def test_failed_sources_degrade_independently(self):
    service, _ = make_fake_service(fail_on=['run.googleapis.com/request_count', 'entries:list'])
    payload = service.collect(window_hours=24)
    agents = _by_name(payload)
    self.assertEqual(payload['source_status']['cloud_run_metrics'], 'error')
    self.assertEqual(payload['source_status']['agent_token_logs'], 'error')
    # The Cloud Run / GKE workload token logs also use entries:list, so they degrade too; traces do not.
    self.assertEqual(payload['source_status']['workload_token_logs'], 'error')
    self.assertEqual(payload['source_status']['workload_token_traces'], 'ok')
    self.assertEqual(payload['source_status']['agent_engine_metrics'], 'ok')
    self.assertEqual(payload['source_status']['inventory'], 'ok')
    self.assertIsNone(agents['FinOps & Tokenomics Analytics']['metrics']['requests'])
    self.assertEqual(agents['IT Service Desk']['metrics']['requests'], 45)
    self.assertIsNone(agents['IT Service Desk']['metrics']['input_tokens'])
    self.assertIsNone(payload['totals']['llm_calls'])
    self.assertEqual(payload['totals']['requests'], 45)
    sources = {e['source'] for e in payload['errors']}
    # The request-history query uses the same Cloud Run metric, so it degrades with it.
    self.assertEqual(sources, {'Cloud Run request metrics', 'Cloud Run request history', 'Agent GenAI token logs',
                               'Cloud Run / GKE GenAI token logs'})
    self.assertNotIn('cloud_run:finops-dashboard-mcp-app', payload['trend']['by_runtime'])

  def test_inventory_failure_is_reported_not_raised(self):
    service, _ = make_fake_service(fail_on=['discoveryengine.googleapis.com'])
    payload = service.collect(window_hours=24)
    self.assertEqual(payload['agents'], [])
    self.assertEqual(payload['source_status']['inventory'], 'error')
    self.assertEqual(payload['source_status']['agent_engine_metrics'], 'not_applicable')
    self.assertEqual(payload['engines'][0]['agents_count'], None)
    self.assertIn('Discovery Engine agents (agent-platform-demo)', {e['source'] for e in payload['errors']})

  def test_total_outage_still_returns_payload(self):
    service, _ = make_fake_service(fail_on=[''])
    payload = service.collect(window_hours=24)
    self.assertEqual(payload['agents'], [])
    self.assertIsNone(payload['model_usage'])
    self.assertIsNone(payload['ge_traffic'])
    self.assertEqual(payload['source_status']['model_usage'], 'error')

  def test_cache_ttl_and_force_refresh_rate_limit(self):
    service, api = make_fake_service()
    service.collect(window_hours=24)
    calls = len(api.calls)
    cached = service.collect(window_hours=24)
    self.assertEqual(len(api.calls), calls)
    self.assertGreaterEqual(cached['cache_age_seconds'], 0.0)
    service.collect(window_hours=24, force_refresh=True)  # Inside the minimum refresh interval.
    self.assertEqual(len(api.calls), calls)
    service.min_refresh_interval_s = 0
    refreshed = service.collect(window_hours=24, force_refresh=True)
    self.assertGreater(len(api.calls), calls)
    self.assertEqual(refreshed['cache_age_seconds'], 0.0)
    calls = len(api.calls)
    service.collect(window_hours=6)  # A different window is a separate cache entry.
    self.assertGreater(len(api.calls), calls)
    service.ttl_seconds = 0
    calls = len(api.calls)
    service.collect(window_hours=6)  # Expired entries are re-collected.
    self.assertGreater(len(api.calls), calls)

  def test_concurrent_requests_share_one_collection(self):
    service, api = make_fake_service()
    threads = [threading.Thread(target=service.collect, kwargs={'window_hours': 24}) for _ in range(6)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()
    self.assertEqual(api.count('/assistants/default_assistant/agents?pageSize=100&pageToken=page-2'), 1)

  def test_window_is_clamped_and_applied_to_queries(self):
    service, api = make_fake_service()
    self.assertEqual(service.collect(window_hours=100000)['window_hours'], 8760)
    # 1-year window caps Cloud Monitoring alignmentPeriod at 30d (2592000s) and uses 4d (345600s) trend buckets.
    self.assertEqual(set(api.alignment_periods), {'2592000s', '345600s'})
    api.alignment_periods.clear()
    self.assertEqual(service.collect(window_hours=720)['window_hours'], 720)
    # 1-month (30-day) window uses 30d (2592000s) aggregate and 6h (21600s) trend buckets.
    self.assertEqual(set(api.alignment_periods), {'2592000s', '21600s'})
    api.alignment_periods.clear()
    self.assertEqual(service.collect(window_hours=2160)['window_hours'], 2160)
    # 3-month (90-day) window uses 30d (2592000s) aggregate and 1d (86400s) trend buckets.
    self.assertEqual(set(api.alignment_periods), {'2592000s', '86400s'})
    api.alignment_periods.clear()
    self.assertEqual(service.collect(window_hours=4320)['window_hours'], 4320)
    # 6-month (180-day) window uses 30d (2592000s) aggregate and 2d (172800s) trend buckets.
    self.assertEqual(set(api.alignment_periods), {'2592000s', '172800s'})
    self.assertEqual(service.collect(window_hours=0)['window_hours'], 1)
    # Up to 7 days the trend fetches raw per-minute points (no server-side alignment double counting).
    api.alignment_periods.clear()
    service.collect(window_hours=24)
    self.assertEqual(set(api.alignment_periods), {'86400s', 'raw'})

  def test_allow_stale_collects_requested_window_instead_of_snapping_to_24h(self):
    service, _ = make_fake_service()
    service.collect(window_hours=24)
    for hrs in (1, 6, 168, 720, 2160, 4320, 8760):
      payload = service.collect(window_hours=hrs, allow_stale=True, max_wait_s=5.0)
      self.assertEqual(payload['window_hours'], hrs)


class GeFleetDiscoveryTest(unittest.TestCase):

  def test_auto_discovers_ge_apps_and_tolerates_a_failing_region(self):
    api = FakeGoogleApi()
    listing_path = ENGINE_PATH.rsplit('/', 1)[0]
    original = api._discovery

    def discovery(path, query):
      if path == listing_path:
        return {'engines': [
            {'name': ENGINE_PATH[len('/v1alpha/'):], 'appType': 'APP_TYPE_INTRANET'},
            {'name': listing_path[len('/v1alpha/'):] + '/plain-search-app', 'solutionType': 'SOLUTION_TYPE_SEARCH'},
        ]}
      return original(path, query)

    api._discovery = discovery
    service = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT, engine_ids=['auto'], location='global', collection='default_collection', api=api)
    service.discovery_locations = ['global', 'us']  # 'us' is not served by the fake -> error, not fatal.
    payload = service.collect(window_hours=24)
    self.assertEqual([e['engine_key'] for e in payload['engines']], [f'global/{ENGINE}'])
    self.assertEqual(len(payload['agents']), 3)
    self.assertTrue(any('app discovery (us)' in e['source'] for e in payload['errors']))

  def test_auto_with_no_apps_reports_error_instead_of_hardcoded_engines(self):
    api = FakeGoogleApi()
    listing_path = ENGINE_PATH.rsplit('/', 1)[0]
    original = api._discovery

    def discovery(path, query):
      if path == listing_path:
        return {'engines': []}
      return original(path, query)

    api._discovery = discovery
    service = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT, engine_ids=['auto'], location='global', collection='default_collection', api=api)
    service.discovery_locations = ['global']
    payload = service.collect(window_hours=24)
    self.assertEqual(payload['engines'], [])
    self.assertEqual(payload['agents'], [])
    self.assertTrue(any('No Gemini Enterprise apps found' in e['detail'] for e in payload['errors']))


class GeFleetRuntimeDedupeTest(unittest.TestCase):

  def test_hashed_and_numbered_run_urls_share_a_runtime_key(self):
    hashed = ge_fleet.classify_agent('e1', 'default_assistant', A2A_AGENT)
    numbered_raw = dict(A2A_AGENT, name=A2A_AGENT['name'] + '0', a2aAgentDefinition={
        'jsonAgentCard': json.dumps({'url': 'https://finops-dashboard-mcp-app-123456789.us-central1.run.app/a2a/app'})})
    numbered = ge_fleet.classify_agent('e2', 'default_assistant', numbered_raw)
    self.assertEqual(ge_fleet.runtime_backend_key(hashed), ge_fleet.runtime_backend_key(numbered))
    managed = ge_fleet.classify_agent('e1', 'default_assistant', MANAGED_AGENT)
    self.assertNotEqual(ge_fleet.runtime_backend_key(hashed), ge_fleet.runtime_backend_key(managed))


class GeFleetHelpersTest(unittest.TestCase):

  def test_parse_window_hours(self):
    for valid in (1, 6, 24, 168, 720, 2160, 4320, 8760):
      self.assertEqual(ge_fleet.parse_window_hours(str(valid)), valid)
      self.assertEqual(ge_fleet.parse_window_hours(valid), valid)
    for bad in (None, True, 0, 8761, 'abc', '', '1; DROP'):
      self.assertIsNone(ge_fleet.parse_window_hours(bad), bad)

  def test_parse_bool(self):
    for truthy in (True, 'true', '1', 'YES', 'on'):
      self.assertTrue(ge_fleet.parse_bool(truthy), truthy)
    for falsy in (False, None, '', '0', 'false', 'no'):
      self.assertFalse(ge_fleet.parse_bool(falsy), falsy)

  def test_cloud_run_service_from_url(self):
    self.assertEqual(
        ge_fleet.cloud_run_service_from_url('https://finops-dashboard-mcp-app-123456789012.us-central1.run.app/a2a/app'),
        {'service': 'finops-dashboard-mcp-app', 'project': '123456789012', 'region': 'us-central1'})
    self.assertEqual(
        ge_fleet.cloud_run_service_from_url('https://finops-dashboard-mcp-app-abcde12345-uc.a.run.app/a2a/app'),
        {'service': 'finops-dashboard-mcp-app'})
    self.assertIsNone(ge_fleet.cloud_run_service_from_url('https://agents.example.com/a2a'))

  def test_classify_other_agent_kinds(self):
    low_code = ge_fleet.classify_agent(ENGINE, 'default_assistant', {'name': 'a/b/agents/x', 'lowCodeAgentDefinition': {}})
    self.assertEqual((low_code['type'], low_code['telemetry_scope']), ('LOW_CODE', 'none'))
    workflow = ge_fleet.classify_agent(ENGINE, 'default_assistant', {'name': 'a/b/agents/w', 'workflowAgentDefinition': {}})
    self.assertEqual((workflow['type'], workflow['telemetry_scope']), ('LOW_CODE', 'none'))
    self.assertEqual(ge_fleet._parse_engine_spec('us/gemini-enterprise-17649552_1764955289529'), ('us', 'gemini-enterprise-17649552_1764955289529'))
    self.assertEqual(ge_fleet._parse_engine_spec('agent-platform-demo'), ('global', 'agent-platform-demo'))
    self.assertEqual(ge_fleet._de_base('us'), 'https://us-discoveryengine.googleapis.com/v1alpha')
    self.assertEqual(ge_fleet._de_base('global'), 'https://discoveryengine.googleapis.com/v1alpha')
    external = ge_fleet.classify_agent(ENGINE, 'default_assistant', {
        'name': 'a/b/agents/y', 'a2aAgentDefinition': {'jsonAgentCard': json.dumps({'url': 'https://agents.example.com/a2a'})}})
    self.assertEqual((external['backend']['kind'], external['telemetry_scope']), ('external_endpoint', 'none'))
    broken = ge_fleet.classify_agent(ENGINE, 'default_assistant', {
        'name': 'a/b/agents/z', 'a2aAgentDefinition': {'jsonAgentCard': 'not json'}})
    self.assertEqual(broken['backend']['kind'], 'external_endpoint')

  def test_classify_cloud_run_service(self):
    mcp_svc = {'metadata': {'name': 'slideforge-mcp', 'labels': {'purpose': 'mcp-server'}}, 'spec': {'template': {'spec': {'containers': [{'name': 'mcp'}]}}}}
    agent_svc = {'metadata': {'name': 'customer-support-agent'}, 'spec': {'template': {'spec': {'containers': [{'name': 'agent'}]}}}}
    skill_svc = {'metadata': {'name': 'corporate-email-tool'}, 'spec': {'template': {'spec': {'containers': [{'name': 'backend'}]}}}}
    std_svc = {'metadata': {'name': 'payment-gateway'}, 'spec': {'template': {'spec': {'containers': [{'name': 'app'}]}}}}
    self.assertEqual(ge_fleet.classify_cloud_run_service(mcp_svc, 'example-project')['type'], 'MCP_SERVER')
    self.assertEqual(ge_fleet.classify_cloud_run_service(agent_svc, 'example-project')['type'], 'CLOUD_RUN_AGENT')
    self.assertEqual(ge_fleet.classify_cloud_run_service(skill_svc, 'example-project')['type'], 'SKILL_BACKEND')
    self.assertEqual(ge_fleet.classify_cloud_run_service(std_svc, 'example-project')['type'], 'CLOUD_RUN_SERVICE')

  def test_aggregate_trace_skills_and_mcp(self):
    fake_traces = [
        {'spans': [
            {'name': 'execute_tool slideforge_render', 'attributes': {'gen_ai.tool.name': 'slideforge_render', 'gcp.vertex.reasoning_engine.id': '1111111111111111111', 'gen_ai.usage.input_tokens': '1200', 'gen_ai.usage.output_tokens': '450', 'gen_ai.request.model': 'gemini-2.5-flash'}, 'startTime': '2026-09-30T00:00:00Z', 'endTime': '2026-09-30T00:00:01.500Z'},
        ]},
        {'spans': [
            {'name': 'invoke_agent deep_researcher', 'attributes': {'gen_ai.agent.name': 'deep_researcher', 'gen_ai.usage.input_tokens': '8000', 'gen_ai.usage.output_tokens': '2000', 'gen_ai.response.model': 'gemini-2.5-pro'}, 'startTime': '2026-09-30T00:00:00Z', 'endTime': '2026-09-30T00:00:04.000Z'},
        ]},
        {'spans': [
            {'name': 'POST /mcp/tools/call', 'attributes': {'http.target': '/mcp/tools/call', 'gen_ai.usage.input_tokens': '500', 'gen_ai.usage.output_tokens': '150'}, 'startTime': '2026-09-30T00:00:00Z', 'endTime': '2026-09-30T00:00:00.800Z'},
        ]},
    ]
    items = ge_fleet.aggregate_trace_skills_and_mcp(fake_traces)
    self.assertEqual(len(items), 3)
    names = [x['name'] for x in items]
    self.assertIn('slideforge_render', names)
    self.assertIn('deep_researcher', names)
    self.assertIn('POST /mcp/tools/call', names)
    tool_item = next(x for x in items if x['name'] == 'slideforge_render')
    self.assertEqual(tool_item['kind'], 'SKILL_TOOL')
    self.assertEqual(tool_item['calls'], 1)
    self.assertEqual(tool_item['co_occurring_input_tokens'], 1200)
    self.assertEqual(tool_item['co_occurring_output_tokens'], 450)
    self.assertIn('gemini-2.5-flash', tool_item['models'])

  def test_unregistered_inventory_and_zombie_detection(self):
    service, _ = make_fake_service()
    project = service.project_id
    registered_agents = [{'backend': {'kind': 'agent_engine', 'reasoning_engine_id': '7384666355462537216'}}]
    disc_re = [{
        'reasoning_engine_id': '9999999999999999999',
        'resource': f'projects/{project}/locations/us-central1/reasoningEngines/9999999999999999999',
        'location': 'us-central1',
        'display_name': 'zombie-idle-engine',
        'framework': 'google-adk',
    }]
    run_catalog = {
        'slideforge-mcp': {
            'service_name': 'slideforge-mcp',
            'resource_name': f'projects/{project}/locations/us-central1/services/slideforge-mcp',
            'region': 'us-central1',
            'category': 'MCP_SERVER',
            'category_label': 'MCP Server',
            'min_instances': 1,
            'max_instances': 10,
            'uri': 'https://slideforge-mcp-uc.a.run.app',
        }
    }
    gke_workloads = [{
        'cluster_name': 'agent-autopilot-cluster',
        'location': 'us-central1',
        'cluster_mode': 'Autopilot',
        'node_count': 3,
        'namespace': 'default',
        'container_name': 'agent-worker',
        'cpu_core_hours': 48.0,
        'memory_gib': 8.0,
    }]
    unreg_results = {
        ('discover_re', project): disc_re,
        ('unreg_re_requests', project): {'9999999999999999999': {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0}},
        ('unreg_re_cpu', project): {'9999999999999999999': 86400.0},
        ('unreg_re_mem', project): {'9999999999999999999': 345600.0},
        ('run_catalog', project): run_catalog,
        ('unreg_run_requests', project): {'slideforge-mcp': {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0}},
        ('unreg_run_billable', project): {'slideforge-mcp': 86400.0},
        ('gke_workloads', project): gke_workloads,
    }

    inv = service._build_unregistered_inventory(
        registered_agents=registered_agents,
        results={},
        unreg_results=unreg_results,
        checked_at='2026-09-30T00:00:00Z',
    )
    unreg = inv['unregistered_runtimes']
    self.assertEqual(len(unreg), 3)
    re_unreg = next(x for x in unreg if x['backend']['kind'] == 'agent_engine')
    self.assertEqual(re_unreg['registration']['finops_status'], 'ZOMBIE_IDLE_ENGINE')
    self.assertIn('gcloud ai reasoning-engines delete', re_unreg['registration']['action']['delete_command'])
    # Compute is reported as measured usage, never converted to dollars with an assumed price.
    self.assertIsNone(re_unreg['metrics']['est_infra_cost_usd'])

    cr_unreg = next(x for x in unreg if x['backend']['kind'] == 'cloud_run')
    self.assertEqual(cr_unreg['type'], 'MCP_SERVER')
    self.assertEqual(cr_unreg['registration']['finops_status'], 'ALWAYS_ON_UNREGISTERED')
    self.assertIn('--min-instances=0', cr_unreg['registration']['action']['delete_command'])

    gke_unreg = next(x for x in unreg if x['backend']['kind'] == 'gke_workload')
    self.assertEqual(gke_unreg['type'], 'GKE_WORKLOAD')
    self.assertIsNone(gke_unreg['metrics']['est_infra_cost_usd'])
    self.assertIsNone(gke_unreg['metrics']['memory_gib_hours'])  # avg GiB is not GiB-hours
    self.assertIn('avg_memory_gib', gke_unreg['metrics'])
    self.assertIsNone(cr_unreg['metrics']['est_infra_cost_usd'])
    summary = inv['unregistered_summary']
    # The dashboard reads these keys (it used to read keys that were never produced).
    self.assertEqual(summary['zombie_runtimes_count'], 1)
    self.assertEqual(summary['unregistered_unpriced_runtimes'], 0)

  def test_project_alias_normalization_deduplicates_project_number(self):
    service, _ = make_fake_service()
    service._record_project_alias('projects/123456789012/locations/global/collections/default_collection/engines/my-app')
    self.assertEqual(service._canonical_project('123456789012'), service.project_id)
    self.assertEqual(service._canonical_project('other-project-999'), 'other-project-999')

  def test_trace_cache_shares_30d_cap_and_falls_back_on_429(self):
    calls = {'trace_list': 0, 'fail_429': False}

    class TraceApi:
      def call(self, method, url, body=None):
        if '/v1/projects/' in url and '/traces' in url and method == 'GET':
          calls['trace_list'] += 1
          if calls['fail_429']:
            raise ge_fleet.FleetSourceError(429, 'HTTP 429: Quota exceeded')
          return {
              'traces': [{
                  'traceId': 't1',
                  'spans': [{
                      'spanId': '1',
                      'name': 'call_llm',
                      'startTime': '2099-09-30T10:00:00Z',
                      'endTime': '2099-09-30T10:00:01Z',
                      'attributes': {
                          'gen_ai.usage.input_tokens': '500',
                          'gen_ai.usage.output_tokens': '200',
                      },
                  }],
              }]
          }
        return {}

    service = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT,
        engine_ids=[ENGINE],
        api=TraceApi(),
    )
    traces_1m, trunc_1m = service._list_traces(
        PROJECT,
        window_s=720 * 3600,
        trace_filter='+span:call_llm',
    )
    self.assertEqual(len(traces_1m), 1)
    self.assertFalse(trunc_1m)
    self.assertEqual(calls['trace_list'], 1)

    # 3m (2160h) and 1y (8760h) cap to the same 30-day trace window and reuse cache
    traces_3m, _ = service._list_traces(
        PROJECT,
        window_s=2160 * 3600,
        trace_filter='+span:call_llm',
    )
    traces_1y, _ = service._list_traces(
        PROJECT,
        window_s=8760 * 3600,
        trace_filter='+span:call_llm',
    )
    self.assertEqual(len(traces_3m), 1)
    self.assertEqual(len(traces_1y), 1)
    self.assertEqual(calls['trace_list'], 1)

    # If a new window hits HTTP 429 on page 0, it falls back to cached trace scan
    calls['fail_429'] = True
    from unittest import mock
    with mock.patch('finops_dashboard.fleet.time.sleep', return_value=None):
      traces_fallback, _ = service._list_traces(
          PROJECT,
          window_s=168 * 3600,
          trace_filter='+span:call_llm',
      )
    self.assertEqual(len(traces_fallback), 1)

  def test_fallback_cache_marks_warming_and_window_metadata(self):
    service, _ = make_fake_service()
    service._cache[24] = (
        ge_fleet.time.monotonic(),
        {
            'window_hours': 24,
            'notes': ['Existing note'],
            'totals': {'agent_input_tokens_window': 0},
        },
    )
    fallback = service._fallback_cache(720)
    self.assertIsNotNone(fallback)
    self.assertTrue(fallback['warming'])
    self.assertEqual(fallback['window_hours'], 720)
    self.assertEqual(fallback['fallback_from_window_hours'], 24)
    self.assertTrue(any('showing cached 24h snapshot' in n for n in fallback['notes']))

  def test_byo_mcp_connector_registered_not_unregistered(self):
    api = FakeGoogleApi()
    original_discovery = api._discovery
    original_call = api.call

    def discovery(path, query):
      if path == ENGINE_PATH:
        return {
            'name': ENGINE_PATH,
            'displayName': 'GB Agent Platform Demo',
            'appType': 'APP_TYPE_INTRANET',
            'dataStoreIds': ['it-support-dashboard-mcp_mcp_data'],
        }
      if path == f'/v1alpha/projects/{PROJECT}/locations/global/collections/it-support-dashboard-mcp/dataConnector':
        return {
            'name': f'projects/{RE_PROJECT}/locations/global/collections/it-support-dashboard-mcp/dataConnector',
            'dataSource': 'custom_mcp',
            'state': 'ACTIVE',
            'actionState': 'ACTIVE',
            'actionConfig': {
                'actionParams': {
                    'instance_uri': f'https://it-support-dashboard-mcp-{RE_PROJECT}.us-central1.run.app/mcp',
                }
            },
            'bapConfig': {
                'enabledActions': ['open_support_dashboard', 'get_support_kpis'],
            },
        }
      return original_discovery(path, query)

    def call(method, url, body=None):
      parts = urllib.parse.urlsplit(url)
      if parts.netloc == 'run.googleapis.com':
        return {
            'services': [
                {'name': f'projects/{PROJECT}/locations/us-central1/services/finops-dashboard-mcp-app'},
                {
                    'name': f'projects/{PROJECT}/locations/us-central1/services/it-support-dashboard-mcp',
                    'uri': f'https://it-support-dashboard-mcp-{RE_PROJECT}.us-central1.run.app',
                    'template': {'scaling': {'minInstanceCount': 0}, 'containers': [{'resources': {'limits': {'cpu': '1', 'memory': '1Gi'}}}]},
                },
            ]
        }
      return original_call(method, url, body)

    api._discovery = discovery
    api.call = call
    service = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT, engine_ids=[ENGINE], location='global', collection='default_collection', api=api)
    payload = service.collect(window_hours=24)
    agents = _by_name(payload)
    self.assertIn('it-support-dashboard-mcp', agents)
    mcp_conn = agents['it-support-dashboard-mcp']
    self.assertEqual(mcp_conn['type'], 'MCP_CONNECTOR')
    self.assertEqual(mcp_conn['state'], 'ENABLED')
    self.assertEqual(mcp_conn['backend']['kind'], 'cloud_run')
    self.assertEqual(mcp_conn['backend']['service'], 'it-support-dashboard-mcp')
    self.assertEqual(mcp_conn['backend']['mcp_tools'], ['open_support_dashboard', 'get_support_kpis'])
    self.assertEqual(mcp_conn['registration']['status'], 'OK')
    unreg_names = [r['display_name'] for r in payload['unregistered_runtimes']]
    self.assertNotIn('it-support-dashboard-mcp', unreg_names)
    mcp_items = [s for s in payload['skills_and_mcp'] if s.get('name') == 'it-support-dashboard-mcp']
    self.assertEqual(len(mcp_items), 1)
    self.assertEqual(mcp_items[0]['registration_scope'], 'GE_REGISTERED')


# --- Cloud Run / GKE workload token attribution -------------------------------------------------

WL_CONTENT = {  # content-bearing span labels: they must never reach the payload
    'gcp.vertex.agent.llm_request': SECRET, 'gcp.vertex.agent.llm_response': SECRET,
    'gcp.vertex.agent.tool_call_args': SECRET, 'gcp.vertex.agent.tool_response': SECRET,
    'gen_ai.input.messages': SECRET,
}
RUN_REVISION_ID = f'//run.googleapis.com/projects/{PROJECT}/locations/us-central1/revisions/'
GE_RESOURCE_ID = ('//discoveryengine.googleapis.com/projects/123456789012/locations/global/collections/'
                  'default_collection/engines/ge-app/assistants/default_assistant/agents/core_assistant')
A2A_SERVICE = 'finops-dashboard-mcp-app'
TRACE_SOURCE = 'Cloud Trace: OpenTelemetry gen_ai spans'
LOG_SOURCE = 'Cloud Logging: OpenTelemetry gen_ai inference events'
GKE_WORKLOAD = {'cluster_name': 'agents-cluster', 'location': 'us-central1', 'cluster_mode': 'Autopilot', 'node_count': 3,
                'namespace': 'support', 'container_name': 'support-worker', 'cpu_core_hours': 4.0, 'memory_gib': 2.0}


def _wl_span(span_id, parent, identity, inp=1000, out=100, model='gemini-2.5-flash', conv='conv-1'):
  labels = {'gen_ai.usage.input_tokens': str(inp), 'gen_ai.usage.output_tokens': str(out),
            'gen_ai.request.model': model, 'gen_ai.conversation.id': conv, **WL_CONTENT, **identity}
  return {'spanId': span_id, 'parentSpanId': parent, 'name': 'generate_content', 'labels': labels,
          'startTime': '2026-10-01T10:00:00Z', 'endTime': '2026-10-01T10:00:02Z'}


def _run_identity(**labels):
  return {'cloud.platform': 'gcp_cloud_run', **labels}


def _wl_records(spans):
  return ge_fleet.extract_workload_trace_records([{'traceId': 'trace-1', 'spans': spans}])


def _wl_log(rtype, rlabels, inp, out, stamp='2026-10-01T11:00:00Z', **extra):
  labels = {'gen_ai.usage.input_tokens': str(inp), 'gen_ai.usage.output_tokens': str(out),
            'gen_ai.response.model': 'gemini-2.5-flash', 'gen_ai.input.messages': SECRET, **extra}
  return {'timestamp': stamp, 'resource': {'type': rtype, 'labels': rlabels}, 'labels': labels}


def _run_catalog(*names):
  return {n: {'service_name': n, 'resource_name': f'projects/{PROJECT}/locations/us-central1/services/{n}',
              'region': 'us-central1', 'category': 'CLOUD_RUN_AGENT', 'category_label': 'Cloud Run Agent',
              'min_instances': 0, 'max_instances': 10, 'uri': f'https://{n}-abcde12345-uc.a.run.app'} for n in names}


class WorkloadTokenInventoryTest(unittest.TestCase):
  """Unregistered Cloud Run / GKE rows join the resolved workload token bundle."""

  def _inventory(self, log_records=None, trace_records=None, requests=None, gke=(), log_source='ok', trace_source='ok'):
    service, _ = make_fake_service()
    catalog = _run_catalog('support-agent', 'docs-mcp')
    logs = {'records': log_records or [], 'entries_scanned': len(log_records or []), 'truncated': False}
    traces = {'records': trace_records or [], 'traces_scanned': 1, 'truncated': False}
    bundle = ge_fleet.build_workload_token_usage(
        logs if log_source == 'ok' else None, traces if trace_source == 'ok' else None,
        catalog, ge_fleet.gke_workload_keys(list(gke)), log_source=log_source, trace_source=trace_source)
    unreg_results = {
        ('run_catalog', PROJECT): catalog,
        ('unreg_run_requests', PROJECT): {
            name: {'requests': count, 'errors_4xx': 0, 'errors_5xx': 0} for name, count in (requests or {}).items()},
        ('discover_gke', PROJECT): {'clusters': [], 'workloads': list(gke)},
    }
    inv = service._build_unregistered_inventory(
        [], {('workload_tokens', PROJECT): bundle}, unreg_results, '2026-10-01T12:00:00Z')
    rows = {r['display_name']: r for r in inv['unregistered_runtimes']}
    return rows, inv['unregistered_summary'], service

  def test_cloud_run_row_gets_tokens_per_model_cost_and_stays_active_without_requests(self):
    records = _wl_records([
        _wl_span('1', None, _run_identity(**{'faas.name': 'support-agent'}), conv='c1'),
        _wl_span('2', None, _run_identity(**{'cloud.resource_id': RUN_REVISION_ID + 'support-agent-00003-k2p'}),
                 inp=2000, out=200, model='gemini-2.5-pro', conv='c2'),
    ])
    rows, summary, service = self._inventory(trace_records=records, requests={'support-agent': 0, 'docs-mcp': 12})
    row = rows['support-agent']
    m = row['metrics']
    self.assertEqual((m['llm_calls'], m['input_tokens'], m['output_tokens'], m['cached_tokens'], m['conversations']),
                     (2, 3000, 300, 0, 2))
    self.assertEqual(m['token_source'], TRACE_SOURCE)
    self.assertIn(TRACE_SOURCE, row['data_sources'])
    self.assertEqual(sorted(row['backend']['models']), ['gemini-2.5-flash', 'gemini-2.5-pro'])
    self.assertEqual(m['last_activity'], '2026-10-01T10:00:02Z')
    cards = service.rate_cards()
    expected = round(live_finops.tokens_cost_usd(1000, 100, 0, cards['gemini-2.5-flash'])
                     + live_finops.tokens_cost_usd(2000, 200, 0, cards['gemini-2.5-pro']), 4)
    self.assertEqual(m['est_token_cost_usd'], expected)
    # LLM calls with no HTTP requests are activity, never a delete recommendation.
    self.assertEqual(row['finops_status'], 'ACTIVE_UNREGISTERED')
    self.assertIn('2 LLM calls', row['finops_recommendation'])
    self.assertNotIn('delete', row['finops_action_command'])
    self.assertEqual((summary['unregistered_llm_calls'], summary['unregistered_input_tokens']), (2, 3000))
    self.assertEqual(summary['unregistered_est_token_cost_usd'], expected)
    wta = summary['workload_token_attribution']
    self.assertEqual((wta['log_source'], wta['trace_source'], wta['cloud_run_services_with_tokens']), ('ok', 'ok', 1))

  def test_service_without_token_telemetry_stays_unknown(self):
    rows, summary, _ = self._inventory(requests={'support-agent': 0, 'docs-mcp': 12})
    docs, idle = rows['docs-mcp']['metrics'], rows['support-agent']
    self.assertEqual((docs['llm_calls'], docs['input_tokens'], docs['est_token_cost_usd']), (None, None, None))
    self.assertNotIn('token_source', docs)
    # Requests but no gen_ai telemetry while both sources ran: tell the operator how to instrument.
    self.assertTrue(any('OTEL_SERVICE_NAME' in n for n in rows['docs-mcp']['notes']))
    self.assertEqual(idle['finops_status'], 'ZOMBIE_IDLE_SERVICE')
    self.assertFalse(any('OTEL_SERVICE_NAME' in n for n in idle['notes']))
    self.assertEqual(summary['unregistered_llm_calls'], 0)
    # A failed source means unknown, so no instrumentation advice.
    rows, _, _ = self._inventory(requests={'docs-mcp': 12}, trace_source='error')
    self.assertFalse(any('OTEL_SERVICE_NAME' in n for n in rows['docs-mcp']['notes']))

  def test_logs_preferred_over_traces_and_never_summed(self):
    logs = ge_fleet.extract_workload_log_records([_wl_log('cloud_run_revision', {'service_name': 'support-agent'}, 500, 50)])
    traces = _wl_records([_wl_span('1', None, _run_identity(**{'faas.name': 'support-agent'}), inp=500, out=50)])
    rows, summary, _ = self._inventory(log_records=logs, trace_records=traces, requests={'support-agent': 3})
    m = rows['support-agent']['metrics']
    self.assertEqual((m['llm_calls'], m['input_tokens'], m['output_tokens']), (1, 500, 50))
    self.assertEqual(m['token_source'], LOG_SOURCE)
    self.assertNotIn(TRACE_SOURCE, rows['support-agent']['data_sources'])
    self.assertEqual(summary['unregistered_input_tokens'], 500)

  def test_unpriced_model_leaves_cost_unknown_and_counts_the_runtime(self):
    records = _wl_records([
        _wl_span('1', None, _run_identity(**{'faas.name': 'support-agent'}), model='gemini-2.5-flash'),
        _wl_span('2', None, _run_identity(**{'faas.name': 'support-agent'}), model='claude-opus-5'),
    ])
    rows, summary, _ = self._inventory(trace_records=records, requests={'support-agent': 4})
    row = rows['support-agent']
    self.assertIsNone(row['metrics']['est_token_cost_usd'])
    self.assertTrue(any('No rate card for claude-opus-5' in n for n in row['notes']))
    self.assertEqual(summary['unregistered_unpriced_runtimes'], 1)
    self.assertEqual(summary['unregistered_est_token_cost_usd'], 0.0)

  def test_gke_row_gets_tokens_from_k8s_container_logs(self):
    logs = ge_fleet.extract_workload_log_records([_wl_log(
        'k8s_container', {'cluster_name': 'agents-cluster', 'namespace_name': 'support', 'container_name': 'support-worker'},
        700, 70, stamp='2026-10-01T11:30:00Z', **{'gen_ai.usage.cache_read.input_tokens': '200',
                                                   'gcp.vertex.agent.session_id': 'session-9'})])
    rows, summary, _ = self._inventory(log_records=logs, gke=[GKE_WORKLOAD])
    row = rows['agents-cluster/support-worker']
    m = row['metrics']
    self.assertEqual((m['llm_calls'], m['input_tokens'], m['output_tokens'], m['cached_tokens'], m['conversations']),
                     (1, 700, 70, 200, 1))
    self.assertEqual((m['token_source'], m['last_activity']), (LOG_SOURCE, '2026-10-01T11:30:00Z'))
    self.assertIsNotNone(m['est_token_cost_usd'])
    self.assertIn('LLM calls', row['finops_recommendation'])
    self.assertEqual(summary['workload_token_attribution']['gke_workloads_with_tokens'], 1)
    self.assertEqual(summary['unregistered_cached_tokens'], 200)

  def test_inventory_without_a_bundle_reports_not_run(self):
    service, _ = make_fake_service()
    inv = service._build_unregistered_inventory(
        [], {}, {('run_catalog', PROJECT): _run_catalog('docs-mcp'),
                 ('unreg_run_requests', PROJECT): {'docs-mcp': {'requests': 5, 'errors_4xx': 0, 'errors_5xx': 0}}},
        '2026-10-01T12:00:00Z')
    row = inv['unregistered_runtimes'][0]
    self.assertIsNone(row['metrics']['llm_calls'])
    self.assertFalse(any('OTEL_SERVICE_NAME' in n for n in row['notes']))
    wta = inv['unregistered_summary']['workload_token_attribution']
    self.assertEqual((wta['log_source'], wta['trace_source']), ('not_run', 'not_run'))


class WorkloadTokenCollectionTest(unittest.TestCase):
  """Collector wiring: queries, source status, registered Cloud Run agents and an end-to-end run."""

  def test_workload_log_query_downloads_no_payload_and_ignores_other_resources(self):
    service, api = make_fake_service()  # the A2A agent's Cloud Run backend schedules the workload jobs
    payload = service.collect(window_hours=24)
    calls = [(url, body) for _, url, body in api.calls if 'fields=entries(timestamp,labels,resource),nextPageToken' in url]
    self.assertEqual(len(calls), 2)  # the fake has two log pages (pageToken logs-2)
    url, body = calls[0]
    self.assertTrue(url.startswith('https://logging.googleapis.com/v2/entries:list?'))
    self.assertIn('resource.type=("cloud_run_revision" OR "k8s_container" OR "generic_task")', body['filter'])
    self.assertIn('labels."gen_ai.usage.input_tokens":*', body['filter'])
    self.assertEqual(body['resourceNames'], [f'projects/{PROJECT}'])
    self.assertEqual(api.count('label:gen_ai.usage.input_tokens +cloud.platform:gcp_cloud_run'), 1)
    self.assertEqual(api.count('label:gen_ai.usage.input_tokens +cloud.platform:gcp_kubernetes_engine'), 1)
    wta = payload['unregistered_summary']['workload_token_attribution']
    self.assertEqual((wta['log_source'], wta['trace_source'], wta['log_entries_scanned']), ('ok', 'ok', 4))
    self.assertEqual(payload['source_status']['workload_token_logs'], 'ok')
    # The fake serves Agent Engine entries to every logging query; they are never attributed to Cloud Run.
    a2a = _by_name(payload)['FinOps & Tokenomics Analytics']
    self.assertIsNone(a2a['metrics']['llm_calls'])
    self.assertTrue(any('OTEL_SERVICE_NAME' in n for n in a2a['notes']))
    self.assertEqual(wta['unattributed_llm_calls'], 0)

  def test_workload_jobs_not_scheduled_without_cloud_run_or_discovery(self):
    service, api = make_fake_service(fail_on=['discoveryengine.googleapis.com'])  # no agents at all
    payload = service.collect(window_hours=24)
    self.assertEqual(api.count('fields=entries(timestamp,labels,resource)'), 0)
    self.assertEqual(payload['source_status']['workload_token_logs'], 'not_applicable')
    self.assertEqual(payload['unregistered_summary']['workload_token_attribution']['log_source'], 'not_run')

  def test_failed_trace_source_is_reported_and_tokens_stay_unknown(self):
    service, _ = make_fake_service(fail_on=['gcp_kubernetes_engine'])
    payload = service.collect(window_hours=24)
    self.assertEqual(payload['source_status']['workload_token_traces'], 'error')
    self.assertIn('Cloud Run / GKE GenAI trace spans', {e['source'] for e in payload['errors']})
    self.assertEqual(payload['unregistered_summary']['workload_token_attribution']['trace_source'], 'error')
    a2a = _by_name(payload)['FinOps & Tokenomics Analytics']
    self.assertIsNone(a2a['metrics']['input_tokens'])
    self.assertFalse(any('OTEL_SERVICE_NAME' in n for n in a2a['notes']))

  def test_workload_log_scan_caps_and_flags_truncation(self):
    entry = _wl_log('cloud_run_revision', {'service_name': 'support-agent'}, 10, 1)

    class PagedLogs:
      def __init__(self):
        self.calls = 0

      def call(self, method, url, body=None):
        self.calls += 1
        return {'entries': [entry, entry], 'nextPageToken': f'page-{self.calls}'}

    api = PagedLogs()
    service = ge_fleet.GeminiEnterpriseFleetService(project_id=PROJECT, engine_ids=[ENGINE], api=api)
    service.max_log_entries = 3
    out = service._workload_log_usage(PROJECT, 3600)
    self.assertEqual((out['entries_scanned'], out['truncated'], len(out['records']), api.calls), (4, True, 4, 2))
    self.assertEqual(service._workload_log_usage(PROJECT, 3600)['entries_scanned'], 4)  # TTL cache
    self.assertEqual(api.calls, 2)
    service._log_cache.clear()
    service.max_log_entries = 10 ** 6
    out = service._workload_log_usage(PROJECT, 3600)  # page limit reached with entries left
    self.assertTrue(out['truncated'])
    self.assertEqual(api.calls, 2 + ge_fleet._MAX_PAGES)

  def test_registered_cloud_run_agent_gets_tokens_via_apply_telemetry(self):
    service, _ = make_fake_service()
    agent = ge_fleet.classify_agent(ENGINE, 'default_assistant', A2A_AGENT)
    records = _wl_records([_wl_span('1', None, _run_identity(**{
        'cloud.resource_id': RUN_REVISION_ID + A2A_SERVICE + '-00042-abc'}))])
    bundle = ge_fleet.build_workload_token_usage(None, {'records': records}, {A2A_SERVICE}, set(), log_source='error')
    results = {('run_requests', PROJECT): {A2A_SERVICE: {'requests': 15, 'errors_4xx': 5, 'errors_5xx': 0}},
               ('workload_tokens', PROJECT): bundle}
    service._apply_telemetry(agent, results)
    m = agent['metrics']
    self.assertEqual((m['requests'], m['llm_calls'], m['input_tokens'], m['output_tokens']), (15, 1, 1000, 100))
    self.assertEqual(m['token_source'], TRACE_SOURCE)
    self.assertEqual(agent['backend']['models'], ['gemini-2.5-flash'])
    card = service.rate_cards()['gemini-2.5-flash']
    self.assertEqual(m['est_token_cost_usd'], round(live_finops.tokens_cost_usd(1000, 100, 0, card), 4))
    # A Cloud Run backend in another project is never attributed from this project's telemetry.
    other_card = json.dumps({'url': f'https://{A2A_SERVICE}-987654321098.us-central1.run.app/a2a/app'})
    other = ge_fleet.classify_agent(ENGINE, 'default_assistant',
                                    dict(A2A_AGENT, a2aAgentDefinition={'jsonAgentCard': other_card}))
    results[('workload_tokens', '987654321098')] = bundle
    service._apply_telemetry(other, results)
    self.assertIsNone(other['metrics']['llm_calls'])
    self.assertNotIn('token_source', other['metrics'])

  def test_collect_attributes_workload_tokens_end_to_end(self):
    run_identity = _run_identity(**{'cloud.resource_id': RUN_REVISION_ID + A2A_SERVICE + '-00042-abc'})
    ge_identity = {'cloud.platform': 'gcp.gemini_enterprise', 'cloud.resource.id': GE_RESOURCE_ID}
    run_trace = {'traceId': 'trace-run-1', 'spans': [
        {'spanId': '1', 'name': 'AssistantService/StreamAssist', 'labels': {'cloud.platform': 'gcp.gemini_enterprise'}},
        _wl_span('2', '1', ge_identity, inp=9000, out=900),  # Gemini Enterprise assistant calls in the same trace
        _wl_span('3', '1', ge_identity, inp=8000, out=800),
        _wl_span('4', '1', run_identity),  # registered A2A service: ADK call_llm ...
        _wl_span('5', '4', run_identity),  # ... and the GenAI SDK span for the same call
        _wl_span('6', '1', _run_identity(**{'faas.name': 'support-agent'}), inp=2000, out=200, model='gemini-2.5-pro'),
        _wl_span('7', '1', _run_identity(**{'faas.name': 'reasoning-engine-1234567890'})),  # Agent Engine runtime
    ]}
    gke_trace = {'traceId': 'trace-gke-1', 'spans': [_wl_span('1', None, {
        'cloud.platform': 'gcp_kubernetes_engine', 'k8s.cluster.name': 'agents-cluster',
        'k8s.namespace.name': 'support', 'k8s.container.name': 'support-worker'}, inp=500, out=50)]}
    shared = dict(A2A_AGENT, name=A2A_AGENT['name'] + '7', displayName='FinOps Analytics (second registration)')

    class WorkloadFakeApi(FakeGoogleApi):
      """Adds Cloud Run / GKE inventory, workload traces routed by filter, and a second registration."""

      def call(self, method, url, body=None):
        parts = urllib.parse.urlsplit(url)
        if parts.netloc == 'cloudtrace.googleapis.com':
          super().call(method, url, body)  # records the call and honours fail_on
          trace_filter = urllib.parse.parse_qs(parts.query).get('filter', [''])[0]
          if 'cloud.platform:gcp_cloud_run' in trace_filter:
            return {'traces': [run_trace]}
          if 'cloud.platform:gcp_kubernetes_engine' in trace_filter:
            return {'traces': [gke_trace, run_trace]}  # run_trace matches both queries
          return {'traces': []}
        if parts.netloc == 'run.googleapis.com':
          return {'services': [{'name': f'projects/{PROJECT}/locations/us-central1/services/{name}'}
                               for name in (A2A_SERVICE, 'support-agent', 'docs-mcp')]}
        if parts.netloc == 'container.googleapis.com':
          return {'clusters': [{'name': 'agents-cluster', 'location': 'us-central1', 'currentNodeCount': 3,
                                'autopilot': {'enabled': True}}]}
        return super().call(method, url, body)

      def _monitoring(self, path, query):
        if 'kubernetes.io/container/cpu/core_usage_time' in query['filter'][0]:
          return {'timeSeries': [
              _series({'cluster_name': 'agents-cluster', 'namespace_name': ns, 'container_name': name}, {}, {'doubleValue': 7200.0})
              for ns, name in (('support', 'support-worker'), ('kube-system', 'kube-dns'))]}
        return super()._monitoring(path, query)

      def _discovery(self, path, query):
        out = super()._discovery(path, query)
        if path.endswith('/assistants/default_assistant/agents') and query.get('pageToken') != ['page-2']:
          out = dict(out, agents=[*out['agents'], shared])
        return out

    api = WorkloadFakeApi()
    service = ge_fleet.GeminiEnterpriseFleetService(
        project_id=PROJECT, engine_ids=[ENGINE], location='global', collection='default_collection', api=api,
        discover_unregistered=True)
    payload = service.collect(window_hours=24)

    agents = _by_name(payload)
    a2a = agents['FinOps & Tokenomics Analytics']['metrics']
    self.assertEqual((a2a['requests'], a2a['llm_calls'], a2a['input_tokens'], a2a['output_tokens']), (15, 1, 1000, 100))
    self.assertEqual(a2a['token_source'], TRACE_SOURCE)
    self.assertEqual(agents['FinOps Analytics (second registration)']['metrics']['llm_calls'], 1)
    # The shared runtime counts once: the ADK agent (3 calls, 600 input from logs) + the Cloud Run service.
    self.assertEqual((payload['totals']['llm_calls'], payload['totals']['input_tokens']), (4, 1600))

    rows = {r['display_name']: r for r in payload['unregistered_runtimes']}
    self.assertNotIn(A2A_SERVICE, rows)
    support = rows['support-agent']
    self.assertEqual((support['metrics']['requests'], support['metrics']['llm_calls'], support['metrics']['input_tokens']),
                     (0, 1, 2000))
    self.assertEqual(support['finops_status'], 'ACTIVE_UNREGISTERED')
    self.assertEqual(support['backend']['models'], ['gemini-2.5-pro'])
    self.assertEqual(rows['docs-mcp']['finops_status'], 'ZOMBIE_IDLE_SERVICE')
    self.assertIsNone(rows['docs-mcp']['metrics']['input_tokens'])
    gke_row = rows['agents-cluster/support-worker']
    self.assertEqual((gke_row['metrics']['llm_calls'], gke_row['metrics']['input_tokens']), (1, 500))
    self.assertEqual(gke_row['metrics']['token_source'], TRACE_SOURCE)
    self.assertNotIn('agents-cluster/kube-dns', rows)

    summary = payload['unregistered_summary']
    self.assertEqual((summary['unregistered_llm_calls'], summary['unregistered_input_tokens']), (2, 2500))
    wta = summary['workload_token_attribution']
    self.assertEqual((wta['log_source'], wta['trace_source'], wta['truncated']), ('ok', 'ok', False))
    self.assertEqual(wta['traces_scanned'], 2)  # trace-run-1 matched both queries and is read once
    self.assertEqual(wta['log_entries_scanned'], 4)  # the fake's Agent Engine log entries: scanned, not attributed
    self.assertEqual((wta['cloud_run_services_with_tokens'], wta['gke_workloads_with_tokens']), (2, 1))
    self.assertEqual(wta['skipped_agent_engine_or_ge_calls'], 3)  # two GE assistant calls + one Agent Engine runtime
    self.assertEqual((wta['unattributed_llm_calls'], wta['top_unattributed']), (0, []))
    self.assertEqual(payload['source_status']['workload_token_traces'], 'ok')
    self.assertTrue(any('Cloud Run and GKE token usage' in n for n in payload['notes']))
    self.assertNotIn(SECRET, json.dumps(payload))


if __name__ == '__main__':
  unittest.main()
