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

import ge_fleet
import telemetry

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
    'displayName': 'VibeLift Analytics & FinOps',
    'state': 'ENABLED',
    'a2aAgentDefinition': {
        'jsonAgentCard': json.dumps({'name': 'vibelift', 'url': 'https://vibe-lift-agent-abcde12345-uc.a.run.app/a2a/app'}),
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
    engine, service = {'reasoning_engine_id': RE_ID}, {'service_name': 'vibe-lift-agent'}
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
    self.assertEqual(sorted(agents), ['Deep Research', 'IT Service Desk', 'VibeLift Analytics & FinOps'])
    self.assertEqual(api.count('pageToken=page-2'), 1)
    self.assertEqual(payload['engines'], [{
        'engine_id': ENGINE, 'engine_key': f'global/{ENGINE}', 'location': 'global',
        'display_name': 'GB Agent Platform Demo', 'app_type': 'APP_TYPE_INTRANET', 'agents_count': 3}])
    self.assertEqual({a['engine_key'] for a in payload['agents']}, {f'global/{ENGINE}'})
    self.assertEqual({a['location'] for a in payload['agents']}, {'global'})
    self.assertEqual(agents['IT Service Desk']['type'], 'ADK')
    self.assertEqual(agents['VibeLift Analytics & FinOps']['type'], 'A2A')
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
    agent = _by_name(service.collect(window_hours=24))['VibeLift Analytics & FinOps']
    m = agent['metrics']
    self.assertEqual(agent['backend']['kind'], 'cloud_run')
    self.assertEqual(agent['backend']['service'], 'vibe-lift-agent')
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
    self.assertEqual(payload['token_log_scan'], {'entries_scanned': 4, 'truncated': False})
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
    self.assertEqual(payload['source_status']['agent_engine_metrics'], 'ok')
    self.assertEqual(payload['source_status']['inventory'], 'ok')
    self.assertIsNone(agents['VibeLift Analytics & FinOps']['metrics']['requests'])
    self.assertEqual(agents['IT Service Desk']['metrics']['requests'], 45)
    self.assertIsNone(agents['IT Service Desk']['metrics']['input_tokens'])
    self.assertIsNone(payload['totals']['llm_calls'])
    self.assertEqual(payload['totals']['requests'], 45)
    sources = {e['source'] for e in payload['errors']}
    # The request-history query uses the same Cloud Run metric, so it degrades with it.
    self.assertEqual(sources, {'Cloud Run request metrics', 'Cloud Run request history', 'Agent GenAI token logs'})
    self.assertNotIn('cloud_run:vibe-lift-agent', payload['trend']['by_runtime'])

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
    self.assertEqual(service.collect(window_hours=100000)['window_hours'], 720)
    # Window-level aggregates use the whole window; the trend uses 6h buckets for a 30-day window.
    self.assertEqual(set(api.alignment_periods), {'2592000s', '21600s'})
    self.assertEqual(service.collect(window_hours=0)['window_hours'], 1)
    # Up to 7 days the trend fetches raw per-minute points (no server-side alignment double counting).
    api.alignment_periods.clear()
    service.collect(window_hours=24)
    self.assertEqual(set(api.alignment_periods), {'86400s', 'raw'})


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


class GeFleetRuntimeDedupeTest(unittest.TestCase):

  def test_hashed_and_numbered_run_urls_share_a_runtime_key(self):
    hashed = ge_fleet.classify_agent('e1', 'default_assistant', A2A_AGENT)
    numbered_raw = dict(A2A_AGENT, name=A2A_AGENT['name'] + '0', a2aAgentDefinition={
        'jsonAgentCard': json.dumps({'url': 'https://vibe-lift-agent-123456789.us-central1.run.app/a2a/app'})})
    numbered = ge_fleet.classify_agent('e2', 'default_assistant', numbered_raw)
    self.assertEqual(ge_fleet.runtime_backend_key(hashed), ge_fleet.runtime_backend_key(numbered))
    managed = ge_fleet.classify_agent('e1', 'default_assistant', MANAGED_AGENT)
    self.assertNotEqual(ge_fleet.runtime_backend_key(hashed), ge_fleet.runtime_backend_key(managed))


class GeFleetHelpersTest(unittest.TestCase):

  def test_parse_window_hours(self):
    self.assertEqual(ge_fleet.parse_window_hours('24'), 24)
    self.assertEqual(ge_fleet.parse_window_hours(168), 168)
    for bad in (None, True, 0, 721, 'abc', '', '1; DROP'):
      self.assertIsNone(ge_fleet.parse_window_hours(bad), bad)

  def test_parse_bool(self):
    for truthy in (True, 'true', '1', 'YES', 'on'):
      self.assertTrue(ge_fleet.parse_bool(truthy), truthy)
    for falsy in (False, None, '', '0', 'false', 'no'):
      self.assertFalse(ge_fleet.parse_bool(falsy), falsy)

  def test_cloud_run_service_from_url(self):
    self.assertEqual(
        ge_fleet.cloud_run_service_from_url('https://vibe-lift-agent-123456789012.us-central1.run.app/a2a/app'),
        {'service': 'vibe-lift-agent', 'project': '123456789012', 'region': 'us-central1'})
    self.assertEqual(
        ge_fleet.cloud_run_service_from_url('https://vibe-lift-agent-abcde12345-uc.a.run.app/a2a/app'),
        {'service': 'vibe-lift-agent'})
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

  def test_summarize_fleet(self):
    service, _ = make_fake_service()
    text = ge_fleet.summarize_fleet(service.collect(window_hours=24))
    self.assertIn('GB Agent Platform Demo (agent-platform-demo)', text)
    self.assertIn('Agents: 3 (3 enabled; 1 A2A, 1 ADK, 1 MANAGED)', text)
    self.assertIn('IT Service Desk [ADK, ENABLED]; 45 requests (3 4xx, 2 5xx)', text)
    self.assertIn('Deep Research [MANAGED, ENABLED]; inventory only', text)
    self.assertIn('no rate card for claude-opus-5', text)
    self.assertIn('Gemini Enterprise assistant calls (project-wide): 7', text)


if __name__ == '__main__':
  unittest.main()
