"""Tests: per-agent token sources (logs vs traces), dead-registration evidence, live FinOps math."""

import json
import unittest

from finops_dashboard import finops as live_finops
from finops_dashboard import fleet as ge_fleet

RE = '4895941110288875520'
RESOURCE = f'projects/example-project/locations/us-central1/reasoningEngines/{RE}'


def _span(span_id, parent, name, labels):
  return {'spanId': span_id, 'parentSpanId': parent, 'name': name, 'labels': labels,
          'endTime': '2026-09-29T22:00:00Z'}


def _trace(input_tokens, output_tokens, engine=RE):
  usage = {'gen_ai.usage.input_tokens': str(input_tokens), 'gen_ai.usage.output_tokens': str(output_tokens),
           'gen_ai.request.model': 'gemini-2.5-flash', 'gcp.vertex.agent.session_id': 's1'}
  return {'spans': [
      _span('1', None, 'invocation', {'cloud.resource_id': f'//aiplatform.googleapis.com/{RESOURCE[:-len(RE)]}{engine}'}),
      _span('2', '1', 'call_llm', dict(usage)),                       # ADK wrapper span
      _span('3', '2', 'generate_content gemini-2.5-flash', dict(usage)),  # SDK span: same call
  ]}


class TraceTokensTest(unittest.TestCase):

  def test_each_llm_call_counted_once(self):
    st = ge_fleet.aggregate_trace_usage([_trace(100, 10), _trace(50, 5)], [RE])[RE]
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens']), (2, 150, 15))
    self.assertEqual(st['models'], ['gemini-2.5-flash'])
    self.assertEqual(st['conversations'], 1)

  def test_other_engines_ignored(self):
    st = ge_fleet.aggregate_trace_usage([_trace(100, 10, engine='999')], [RE])[RE]
    self.assertEqual(st['llm_calls'], 0)


GE_RES = ('//discoveryengine.googleapis.com/projects/123456789012/locations/us/collections/default_collection/'
          'engines/ge-app-1/assistants/default_assistant/agents/core_assistant')


def _ge_trace(inp, out, platform='gcp.gemini_enterprise'):
  labels = {'cloud.platform': platform, 'cloud.resource.id': GE_RES, 'gen_ai.request.model': 'gemini-2.5-flash',
            'gen_ai.usage.input_tokens': str(inp), 'gen_ai.usage.output_tokens': str(out),
            'gen_ai.conversation.id': 'sessions/1'}
  return {'spans': [_span('1', None, 'AssistantService/StreamAssist', {'cloud.platform': platform}),
                    _span('2', '1', 'generate_content gemini-2.5-flash', labels)]}


class GeAssistantTraceTokensTest(unittest.TestCase):

  def test_per_app_totals(self):
    out = ge_fleet.aggregate_ge_assistant_usage([_ge_trace(1000, 20), _ge_trace(500, 5)])
    st = out['by_engine']['us/ge-app-1']
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens']), (2, 1500, 25))
    self.assertEqual(st['assistant_agents'], {'core_assistant': 2})
    self.assertEqual(st['models'], {'gemini-2.5-flash': 2})
    self.assertEqual(out['totals']['input_tokens'], 1500)

  def test_agent_engine_spans_not_counted_as_ge(self):
    out = ge_fleet.aggregate_ge_assistant_usage([_ge_trace(1000, 20, platform='gcp.agent_engine'), _trace(9, 9)])
    self.assertEqual(out['by_engine'], {})
    self.assertEqual(out['totals']['llm_calls'], 0)


_CARD = {'m': {'input': 1.0, 'output': 10.0, 'cached_read': 0.25}}


class TokenAccountingNoDoubleCountTest(unittest.TestCase):
  """OTel input_tokens already include cache reads; cached tokens must never be counted twice."""

  def test_cost_bills_cached_share_once(self):
    # 1M input of which 400k were cache reads, 100k output:
    # 600k * $1 + 400k * $0.25 + 100k * $10 = 0.6 + 0.1 + 1.0 = $1.70 (not $2.20 = input billed in full + cache).
    self.assertEqual(ge_fleet._estimate_token_cost_usd(1_000_000, 100_000, 400_000, ['m'], _CARD), 1.7)

  def test_cost_without_cache_and_with_bad_cache_value(self):
    self.assertEqual(ge_fleet._estimate_token_cost_usd(1_000_000, 0, 0, ['m'], _CARD), 1.0)
    # A cached count larger than input is clamped to input (never negative uncached tokens).
    self.assertEqual(ge_fleet._estimate_token_cost_usd(1_000_000, 0, 5_000_000, ['m'], _CARD), 0.25)
    self.assertEqual(ge_fleet._estimate_token_cost_usd(0, 0, 0, ['m'], _CARD), 0.0)

  def test_cache_read_uses_one_key_and_ignores_cache_writes(self):
    labels = {'gen_ai.usage.input_tokens': '14146',
              'gen_ai.usage.cache_read.input_tokens': '9932',
              'gen_ai.usage.cache_read_input_tokens': '9932',       # alias of the same count
              'gen_ai.usage.cache_creation.input_tokens': '4000'}   # cache WRITE, not a cache hit
    self.assertEqual(ge_fleet._cache_read_tokens(labels), 9932)
    self.assertEqual(ge_fleet._cache_read_tokens({'gen_ai.usage.cached_tokens': '7'}), 7)
    self.assertEqual(ge_fleet._cache_read_tokens({'gen_ai.usage.cache_creation.input_tokens': '4000'}), 0)
    self.assertEqual(ge_fleet._cache_read_tokens({}), 0)

  def test_trace_and_ge_aggregators_count_cache_reads_once(self):
    extra = {'gen_ai.usage.cache_read.input_tokens': '60', 'gen_ai.usage.cache_read_input_tokens': '60',
             'gen_ai.usage.cache_creation.input_tokens': '25'}
    tr = _trace(100, 10)
    for sp in tr['spans'][1:]:
      sp['labels'].update(extra)
    st = ge_fleet.aggregate_trace_usage([tr], [RE])[RE]
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['cached_tokens']), (1, 100, 60))
    ge = _ge_trace(1000, 20)
    ge['spans'][1]['labels'].update(extra)
    out = ge_fleet.aggregate_ge_assistant_usage([ge])
    self.assertEqual(out['by_engine']['us/ge-app-1']['cached_tokens'], 60)
    self.assertEqual(out['totals']['cached_tokens'], 60)


def _agent(requests):
  return {'type': 'ADK', 'display_name': 'my-capital-agent', 'data_sources': [], 'notes': [],
          'backend': {'kind': 'agent_engine', 'resource': RESOURCE, 'reasoning_engine_id': RE,
                      'project': 'example-project', 'location': 'us-central1'},
          'metrics': {'requests': None, 'errors_4xx': None, 'errors_5xx': None, 'llm_calls': None,
                      'input_tokens': None, 'output_tokens': None, 'cached_tokens': None,
                      'conversations': None, 'last_activity': None, '_req': requests}}


def _usage(calls, inp):
  return {'llm_calls': calls, 'input_tokens': inp, 'output_tokens': 1, 'cached_tokens': 0,
          'conversations': 1, 'last_activity': None, 'models': ['m']}


class TokenSourceJoinTest(unittest.TestCase):

  def _apply(self, logs, traces, requests=4):
    svc = ge_fleet.GeminiEnterpriseFleetService.__new__(ge_fleet.GeminiEnterpriseFleetService)
    svc.project_id = 'example-project'
    agent = _agent(requests)
    results = {('re_requests', 'example-project'): {RE: {'requests': requests, 'errors_4xx': 0, 'errors_5xx': 0}}}
    if logs is not None:
      results[('re_tokens', 'example-project')] = {'by_engine': {RE: logs}}
    if traces is not None:
      results[('re_trace_tokens', 'example-project')] = {'by_engine': {RE: traces}}
    svc._apply_telemetry(agent, results)
    return agent

  def test_trace_used_when_logs_have_no_usage(self):
    agent = self._apply(_usage(0, 0), _usage(2, 3648))
    self.assertEqual(agent['metrics']['input_tokens'], 3648)
    self.assertIn('Cloud Trace', agent['metrics']['token_source'])

  def test_logs_preferred_and_never_summed_with_traces(self):
    agent = self._apply(_usage(41, 46000), _usage(41, 46000))
    self.assertEqual(agent['metrics']['input_tokens'], 46000)
    self.assertIn('Cloud Logging', agent['metrics']['token_source'])

  def test_traffic_without_telemetry_is_unknown_not_zero(self):
    agent = self._apply(_usage(0, 0), _usage(0, 0))
    self.assertIsNone(agent['metrics']['input_tokens'])
    self.assertTrue(any('no gen_ai token telemetry' in n for n in agent['notes']))

  def test_source_failure_is_unknown_not_zero(self):
    agent = self._apply(_usage(0, 0), None)
    self.assertIsNone(agent['metrics']['input_tokens'])


class RegistrationTest(unittest.TestCase):

  def _agent(self, **backend):
    return {'type': 'ADK', 'resource_name': 'projects/123456789012/locations/global/collections/default_collection/engines/e/assistants/default_assistant/agents/123',
            'backend': dict({'kind': 'agent_engine', 'resource': RESOURCE, 'location': 'us-central1'}, **backend)}

  def test_404_is_backend_not_found_with_cleanup_command(self):
    reg = ge_fleet.assess_registration(self._agent(), {('re_meta', RESOURCE): {'exists': False, 'http_status': 404}}, 't')
    self.assertEqual(reg['status'], 'BACKEND_NOT_FOUND')
    self.assertIn('HTTP 404', reg['evidence'])
    self.assertIn('curl -X DELETE', reg['action']['delete_command'])
    self.assertIn('discoveryengine.googleapis.com/v1alpha/projects/123456789012/locations/global', reg['action']['delete_command'])

  def test_lookup_failure_is_unverified_not_dead(self):
    reg = ge_fleet.assess_registration(self._agent(), {('re_meta', RESOURCE): None}, 't')
    self.assertEqual(reg['status'], 'UNVERIFIED')
    self.assertNotIn('action', reg)

  def test_existing_engine_ok_and_missing_reference(self):
    self.assertEqual(ge_fleet.assess_registration(self._agent(), {('re_meta', RESOURCE): {'exists': True}}, 't')['status'], 'OK')
    self.assertEqual(ge_fleet.assess_registration(self._agent(resource=None), {}, 't')['status'], 'NO_BACKEND')

  def test_cloud_run(self):
    a2a = {'type': 'A2A', 'resource_name': 'projects/p/locations/global/x', 'backend': {'kind': 'cloud_run', 'service': 'svc'}}
    inv = {('run_inventory', 'p'): {'other'}}
    # Hashed URL (project not encoded): absent from the default project is only UNVERIFIED.
    self.assertEqual(ge_fleet.assess_registration(a2a, inv, 't', 'p')['status'], 'UNVERIFIED')
    a2a['backend']['project'] = 'p'
    self.assertEqual(ge_fleet.assess_registration(a2a, inv, 't', 'p')['status'], 'BACKEND_NOT_FOUND')
    self.assertEqual(ge_fleet.assess_registration(a2a, {('run_inventory', 'p'): {'svc'}}, 't', 'p')['status'], 'OK')


CARD = {'input': 0.30, 'output': 2.50, 'cached_read': 0.03, 'cache_write': 0.0}
CARDS = {'flash': CARD, 'lite': {'input': 0.10, 'output': 0.40, 'cached_read': 0.01, 'cache_write': 0.0}}


def _mu(rows):
  return {'models': rows, 'totals': {'est_cost_usd': sum(live_finops._cost(r, CARDS[r['model']]) for r in rows)},
          'rate_cards': CARDS, 'interval': {'start': 'a', 'end': 'b'}}


class LiveFinopsTest(unittest.TestCase):

  def test_drift_drivers_sum_exactly_to_observed_change(self):
    prev = _mu([{'model': 'flash', 'input_tokens': 1_000_000, 'output_tokens': 50_000, 'cache_read_tokens': 0, 'invocations': 100}])
    cur = _mu([{'model': 'flash', 'input_tokens': 3_000_000, 'output_tokens': 80_000, 'cache_read_tokens': 500_000, 'invocations': 150},
               {'model': 'lite', 'input_tokens': 200_000, 'output_tokens': 10_000, 'cache_read_tokens': 0, 'invocations': 40}])
    d = live_finops.build_spend_drift(cur, prev)
    self.assertEqual(d['status'], 'LIVE')
    total = sum(x['change_usd'] for x in d['drivers'])
    self.assertAlmostEqual(total, d['change_usd'], places=3)
    self.assertAlmostEqual(d['unexplained_usd'], 0.0, places=6)
    by = {x['driver_id']: x['change_usd'] for x in d['drivers']}
    self.assertGreater(by['volume'], 0)
    self.assertGreater(by['prompt_size'], 0)
    self.assertAlmostEqual(by['model_mix'], live_finops._cost(cur['models'][1], CARDS['lite']), places=4)

  def test_no_previous_period_is_no_data(self):
    self.assertEqual(live_finops.build_spend_drift(_mu([]), None)['status'], 'NO_DATA')

  def test_model_switch_projection_uses_observed_tokens(self):
    usage = _mu([{'model': 'flash', 'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cache_read_tokens': 0, 'invocations': 10}])
    p = live_finops.project_model_switch(usage, 'flash', 'lite', 50, CARDS)
    self.assertEqual(p['status'], 'PROJECTION')
    self.assertAlmostEqual(p['moved_cost_before_usd'], 0.5 * (0.30 + 0.25), places=4)
    self.assertAlmostEqual(p['moved_cost_after_usd'], 0.5 * (0.10 + 0.04), places=4)
    self.assertAlmostEqual(p['projected_total_usd'], usage['totals']['est_cost_usd'] + p['change_usd'], places=4)
    self.assertEqual(live_finops.project_model_switch(usage, 'nope', 'lite', 50, CARDS)['status'], 'ERROR')

  def test_cache_projection(self):
    usage = _mu([{'model': 'flash', 'input_tokens': 1_000_000, 'output_tokens': 0, 'cache_read_tokens': 0, 'invocations': 10}])
    p = live_finops.project_cache_share(usage, 'flash', 50, CARDS)
    self.assertAlmostEqual(p['change_usd'], (-500_000 * 0.30 + 500_000 * 0.03) / 1e6, places=6)

  def test_token_economics_matches_model_usage(self):
    usage = _mu([{'model': 'flash', 'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 0,
                  'invocations': 4, 'est_cost_usd': 0.00055}])
    usage['totals'].update(invocations=4, input_tokens=1000, output_tokens=100)
    te = live_finops.build_token_economics({'model_usage': usage, 'agents': [], 'window_hours': 24})
    self.assertEqual(te['status'], 'LIVE')
    self.assertEqual(te['models'][0]['avg_prompt_tokens_per_call'], 250)
    self.assertEqual(te['totals']['calls'], 4)
    self.assertEqual(live_finops.build_token_economics({})['status'], 'NO_DATA')


class LiveValidatorChecksTest(unittest.TestCase):

  def _fleet(self, evidence='GET x returned HTTP 404 (Agent Engine does not exist).'):
    usage = _mu([{'model': 'flash', 'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 0,
                  'invocations': 4, 'est_cost_usd': 0.00055}])
    usage['totals'].update(invocations=4, input_tokens=1000, output_tokens=100, est_cost_usd=0.00055)
    agents = [
        {'display_name': 'live', 'backend': {'kind': 'agent_engine', 'resource': 'r1'},
         'metrics': {'requests': 2, 'llm_calls': 4, 'input_tokens': 700, 'output_tokens': 70,
                     'token_source': 'Cloud Trace: OpenTelemetry gen_ai spans'},
         'registration': {'status': 'OK'}},
        {'display_name': 'dead', 'backend': {'kind': 'agent_engine', 'resource': 'r2'},
         'metrics': {'requests': 0, 'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0},
         'registration': {'status': 'BACKEND_NOT_FOUND', 'evidence': evidence,
                          'action': {'delete_command': 'curl -X DELETE ...'}}},
    ]
    fleet = {'agents': agents, 'model_usage': usage, 'model_usage_previous': None, 'window_hours': 24,
             'totals': {'input_tokens': 700, 'broken_registrations': 1}}
    fleet['live_finops'] = live_finops.build_live_finops(fleet)
    return fleet

  def _run(self, state, fleet):
    from finops_dashboard import validator as telemetry_validator  # pylint: disable=g-import-not-at-top
    checks = []
    def add(**kw):
      return checks.append(dict(kw))
    telemetry_validator._add_live_finops_checks(state, fleet, fleet['agents'], fleet['totals'], add)
    return {c['check_id']: c for c in checks}

  def test_live_payload_passes(self):
    fleet = self._fleet()
    got = self._run({'what_if_default': None, 'tokenomics_cockpit': None}, fleet)
    self.assertTrue(all(c['passed'] for c in got.values()), {k: c['evidence'] for k, c in got.items() if not c['passed']})

  def test_simulator_payload_and_missing_evidence_flagged(self):
    fleet = self._fleet(evidence='lookup failed')
    got = self._run({'what_if_default': {'x': 1}, 'tokenomics_cockpit': None}, fleet)
    self.assertFalse(got['LIVE-NO-SIMULATOR-PAYLOADS']['passed'])
    self.assertFalse(got['TAB1-DEAD-REGISTRATION-EVIDENCE']['passed'])

  def test_standalone_runtime_tokens_do_not_break_the_registered_reconciliation(self):
    # Token economics lists standalone runtimes too; fleet.totals covers registered agents only.
    fleet = self._fleet()
    fleet['unregistered_runtimes'] = [{
        'display_name': 'support-agent', 'type': 'CLOUD_RUN_AGENT',
        'backend': {'kind': 'cloud_run', 'service': 'support-agent'},
        'metrics': {'requests': 0, 'llm_calls': 2, 'input_tokens': 3000, 'output_tokens': 300,
                    'token_source': 'Cloud Trace: OpenTelemetry gen_ai spans'},
        'registration': {'status': 'UNREGISTERED_STANDALONE'}}]
    fleet['live_finops'] = live_finops.build_live_finops(fleet)
    got = self._run({'what_if_default': None, 'tokenomics_cockpit': None}, fleet)
    check = got['TAB4-TOKEN-ECONOMICS-RECONCILES']
    self.assertTrue(check['passed'], check['evidence'])


# --- Cloud Run / GKE workload token attribution -------------------------------------------------

_W_SECRET = 'WORKLOAD SECRET PROMPT TEXT'
# Content-bearing labels that real spans carry; they must never reach records, results or summaries.
_W_CONTENT = {
    'gcp.vertex.agent.llm_request': _W_SECRET, 'gcp.vertex.agent.llm_response': _W_SECRET,
    'gcp.vertex.agent.tool_call_args': _W_SECRET, 'gcp.vertex.agent.tool_response': _W_SECRET,
    'gen_ai.input.messages': _W_SECRET, 'gen_ai.output.messages': _W_SECRET,
}
_RUN_REV = '//run.googleapis.com/projects/example-project/locations/us-central1/revisions/'
_W_SERVICES = {'support-agent', 'docs-mcp', 'docs-mcp-eu'}
_W_GKE = {('agents-cluster', 'support', 'support-worker')}
_RECORD_KEYS = {'kind', 'service', 'resource_service', 'revision', 'legacy_service', 'service_name', 'job', 'cluster',
                'namespace', 'container', 'name', 'platform', 'input_tokens', 'output_tokens', 'cached_tokens', 'model',
                'conversation', 'timestamp'}


def _wl_labels(extra, inp=1000, out=100, model='gemini-2.5-flash', conv='conv-1'):
  labels = {'gen_ai.usage.input_tokens': str(inp), 'gen_ai.usage.output_tokens': str(out),
            'gen_ai.request.model': model, 'gen_ai.conversation.id': conv, **_W_CONTENT}
  labels.update(extra)
  return labels


def _run_span(span_id, extra, parent=None, **usage):
  return _span(span_id, parent, 'generate_content', _wl_labels({'cloud.platform': 'gcp_cloud_run', **extra}, **usage))


def _resolve_traces(traces, services=_W_SERVICES, gke=_W_GKE):
  return ge_fleet.resolve_workload_usage(ge_fleet.extract_workload_trace_records(traces), services, gke)


def _calls(resolved, kind):
  return {key: st['llm_calls'] for key, st in resolved[kind].items()}


class WorkloadTraceAttributionTest(unittest.TestCase):
  """Cloud Run / GKE spans are attributed by their own labels (fixtures shaped like live Cloud Trace data)."""

  def test_cloud_run_span_attributed_by_faas_name(self):
    out = _resolve_traces([{'spans': [
        _run_span('1', {'faas.name': 'support-agent', 'cloud.resource_id': _RUN_REV + 'support-agent-00007-abc'})]}])
    st = out['cloud_run']['support-agent']
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens'], st['cached_tokens']), (1, 1000, 100, 0))
    self.assertEqual((st['models'], st['conversations'], st['last_activity']),
                     (['gemini-2.5-flash'], 1, '2026-09-29T22:00:00Z'))
    self.assertEqual((out['unattributed_llm_calls'], out['skipped_agent_engine_or_ge_calls']), (0, 0))

  def test_revision_resolution_with_prefix_sharing_service_names(self):
    # cloud.resource_id names the REVISION: generated suffixes are stripped, custom ones use the longest prefix.
    revisions = ('docs-mcp-eu-00012-x7k', 'docs-mcp-eu-blue', 'docs-mcp-00003-q2w', 'docs-mcp-canary')
    out = _resolve_traces([{'spans': [_run_span(str(i), {'cloud.resource_id': _RUN_REV + rev})
                                      for i, rev in enumerate(revisions)]}])
    self.assertEqual(_calls(out, 'cloud_run'), {'docs-mcp-eu': 2, 'docs-mcp': 2})

  def test_service_name_and_legacy_label_fallbacks(self):
    out = _resolve_traces([{'spans': [
        _run_span('1', {'service.name': 'support-agent'}),
        _span('2', None, 'generate_content', _wl_labels({'g.co/r/cloud_run_revision/service_name': 'docs-mcp'})),
        _run_span('3', {'service.name': 'unknown_service:python'}),  # OTel default name: never matched
    ]}])
    self.assertEqual(_calls(out, 'cloud_run'), {'support-agent': 1, 'docs-mcp': 1})
    self.assertEqual(out['unattributed'], {'cloud_run:unknown_service:python': 1})

  def test_gemini_enterprise_spans_in_the_same_trace_are_not_attributed(self):
    ge = {'cloud.platform': 'gcp.gemini_enterprise', 'cloud.resource.id': GE_RES}
    out = _resolve_traces([{'spans': [
        _span('1', None, 'AssistantService/StreamAssist', {'cloud.platform': 'gcp.gemini_enterprise'}),
        _span('2', '1', 'generate_content', _wl_labels(dict(ge), inp=9000, out=900)),
        _span('3', '2', 'generate_content', _wl_labels(dict(ge), inp=8000, out=800)),  # GE wrapper + child: one call
        # The Cloud Run MCP server that GE called; its parent is a GE usage span, its call is its own.
        _run_span('4', {'faas.name': 'docs-mcp'}, parent='2', inp=300, out=30),
        # A usage child on another platform does not make span 4 a wrapper.
        _span('5', '4', 'generate_content', _wl_labels(dict(ge), inp=7000, out=700)),
    ]}])
    self.assertEqual(list(out['cloud_run']), ['docs-mcp'])
    st = out['cloud_run']['docs-mcp']
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens']), (1, 300, 30))
    self.assertEqual(out['skipped'], {'gemini_enterprise': 2})

  def test_agent_engine_spans_are_skipped(self):
    out = _resolve_traces([{'spans': [
        _span('1', None, 'call_llm', _wl_labels({'cloud.platform': 'gcp.agent_engine'})),
        _run_span('2', {'service.name': '7777777777777777777'}),  # numeric service.name is an Agent Engine id
        _run_span('3', {'cloud.resource_id': f'//aiplatform.googleapis.com/{RESOURCE}'}),
        # Agent Engine runtimes that export as Cloud Run, even when a catalog service has that name.
        _run_span('4', {'faas.name': 'reasoning-engine-1234567890',
                        'cloud.resource_id': _RUN_REV + 'reasoning-engine-1234567890-00001-abc'}),
        _run_span('5', {'cloud.resource_id': _RUN_REV + 'reasoning-engine-1234567890-00002-def'}),
    ]}], services=_W_SERVICES | {'reasoning-engine-1234567890'})
    self.assertEqual((out['cloud_run'], out['unattributed']), ({}, {}))
    self.assertEqual(out['skipped'], {'agent_engine': 1, 'agent_engine:7777777777777777777': 1,
                                      f'agent_engine:{RE}': 1, 'agent_engine:1234567890': 2})

  def test_nested_call_llm_and_generate_content_counted_once(self):
    ident = {'faas.name': 'support-agent'}
    out = _resolve_traces([{'spans': [
        _span('1', None, 'invoke_agent support_agent', {'cloud.platform': 'gcp_cloud_run', 'faas.name': 'support-agent'}),
        _run_span('2', ident, parent='1'),  # ADK call_llm
        _run_span('3', ident, parent='2'),  # GenAI SDK generate_content: the same call
    ]}])
    st = out['cloud_run']['support-agent']
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens']), (1, 1000, 100))

  def test_gke_spans_by_k8s_and_legacy_labels(self):
    k8s = {'cloud.platform': 'gcp_kubernetes_engine', 'k8s.cluster.name': 'agents-cluster', 'k8s.namespace.name': 'support'}
    legacy = {'g.co/r/k8s_container/cluster_name': 'agents-cluster', 'g.co/r/k8s_container/namespace_name': 'support',
              'g.co/r/k8s_container/container_name': 'support-worker'}
    out = _resolve_traces([{'spans': [
        _span('1', None, 'generate_content', _wl_labels({**k8s, 'k8s.container.name': 'support-worker'})),
        _span('2', None, 'generate_content', _wl_labels(legacy)),
        _span('3', None, 'generate_content', _wl_labels({**k8s, 'service.name': 'support-worker'})),  # no container name
        _span('4', None, 'generate_content', _wl_labels({**k8s, 'service.name': 'billing-api'})),  # no such container
        _span('5', None, 'generate_content', _wl_labels({**k8s, 'k8s.namespace.name': 'kube-system',
                                                         'k8s.container.name': 'kube-dns'})),
    ]}])
    self.assertEqual(_calls(out, 'gke'), {'agents-cluster/support/support-worker': 3})
    self.assertEqual(out['unattributed'], {'gke:agents-cluster/support/billing-api': 1})
    self.assertEqual(out['skipped_system_namespace_calls'], 1)

  def test_unresolvable_workloads_are_counted_by_name_only(self):
    out = _resolve_traces([{'spans': [
        _run_span('1', {'faas.name': 'deleted-svc'}),
        _run_span('2', {'faas.name': 'deleted-svc'}),
        _span('3', None, 'generate_content', _wl_labels({
            'cloud.platform': 'gcp_kubernetes_engine', 'k8s.cluster.name': 'agents-cluster',
            'k8s.namespace.name': 'support', 'k8s.container.name': 'other'})),
        _span('4', None, 'generate_content', _wl_labels({'service.name': 'local-dev'})),  # no platform at all
        # service.name alone is not enough: trace attribution needs cloud.platform (resource detection),
        # even when the name matches an inventoried Cloud Run service.
        _span('5', None, 'generate_content', _wl_labels({'service.name': 'support-agent'})),
    ]}])
    self.assertEqual((out['cloud_run'], out['gke'], out['unattributed_llm_calls']), ({}, {}, 5))
    self.assertEqual(out['top_unattributed'],
                     ['cloud_run:deleted-svc', 'gke:agents-cluster/support/other', 'unknown_platform:local-dev',
                      'unknown_platform:support-agent'])

  def test_records_and_results_never_carry_content(self):
    trace = {'spans': [_run_span('1', {'faas.name': 'support-agent'}, conv='conv-raw-id-42')]}
    entries = [{'timestamp': '2026-10-01T10:00:00Z', 'jsonPayload': {'prompt': _W_SECRET},
                'resource': {'type': 'cloud_run_revision', 'labels': {'service_name': 'support-agent'}},
                'labels': _wl_labels({}, conv='conv-raw-id-42')}]
    trace_records = ge_fleet.extract_workload_trace_records([trace])
    log_records = ge_fleet.extract_workload_log_records(entries)
    for rec in trace_records + log_records:
      self.assertLessEqual(set(rec), _RECORD_KEYS)  # allowlisted keys only, never a whole label dict
    bundle = ge_fleet.build_workload_token_usage({'records': log_records}, {'records': trace_records}, _W_SERVICES, _W_GKE)
    blob = json.dumps([trace_records, log_records, bundle, ge_fleet.summarize_workload_token_attribution(bundle)])
    self.assertNotIn(_W_SECRET, blob)
    self.assertNotIn('conv-raw-id-42', blob)  # conversations are only counted, from a digest
    self.assertEqual(bundle['logs']['cloud_run']['support-agent']['conversations'], 1)


class WorkloadLogAttributionTest(unittest.TestCase):

  @staticmethod
  def _entry(rtype, rlabels, **usage):
    return {'timestamp': '2026-10-01T10:00:00Z', 'resource': {'type': rtype, 'labels': rlabels},
            'labels': _wl_labels({}, **usage), 'jsonPayload': {'message': _W_SECRET}}

  def test_log_resource_types_and_identity(self):
    entries = [
        self._entry('cloud_run_revision', {'service_name': 'support-agent', 'revision_name': 'support-agent-00007-abc'}),
        self._entry('generic_task', {'job': 'support-agent', 'location': 'us-central1', 'task_id': 'instance-1'}),
        self._entry('generic_task', {'job': 'reasoning-engine-1234567890'}),  # Agent Engine runtime
        self._entry('generic_task', {'job': 'nightly-batch'}),  # not a Cloud Run service
        self._entry('k8s_container', {'cluster_name': 'agents-cluster', 'namespace_name': 'support',
                                      'container_name': 'support-worker'}),
        self._entry('aiplatform.googleapis.com/ReasoningEngine', {'reasoning_engine_id': RE}),  # read elsewhere
        self._entry('global', {}),
        {'timestamp': '2026-10-01T10:00:00Z', 'labels': {},
         'resource': {'type': 'cloud_run_revision', 'labels': {'service_name': 'docs-mcp'}}},  # no usage labels
    ]
    records = ge_fleet.extract_workload_log_records(entries)
    self.assertEqual(len(records), 5)
    out = ge_fleet.resolve_workload_usage(records, _W_SERVICES, _W_GKE)
    self.assertEqual(_calls(out, 'cloud_run'), {'support-agent': 2})
    self.assertEqual(_calls(out, 'gke'), {'agents-cluster/support/support-worker': 1})
    self.assertEqual(out['skipped'], {'agent_engine:1234567890': 1})
    self.assertEqual(out['unattributed'], {'generic_task:nightly-batch': 1})


class WorkloadTokenCostAndSummaryTest(unittest.TestCase):

  def test_each_model_priced_with_its_own_card(self):
    cards = {'a': {'input': 1.0, 'output': 10.0, 'cached_read': 0.1}, 'b': {'input': 2.0, 'output': 20.0, 'cached_read': 0.2}}
    by_model = {'a': {'input_tokens': 1_000_000, 'output_tokens': 0, 'cached_tokens': 0},
                'publishers/google/models/b': {'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cached_tokens': 500_000}}
    # a: 1M x $1 = 1.0; b: 500k uncached x $2 + 500k cached x $0.2 + 100k x $20 = 1.0 + 0.1 + 2.0 = 3.1.
    self.assertEqual(ge_fleet._estimate_usage_cost_usd(by_model, cards), (4.1, []))

  def test_any_unpriced_model_leaves_the_runtime_unpriced(self):
    by_model = {'a': {'input_tokens': 10, 'output_tokens': 1}, 'mystery-model': {'input_tokens': 5},
                '(model not logged)': {'output_tokens': 3}, 'idle-model': {}}
    self.assertEqual(ge_fleet._estimate_usage_cost_usd(by_model, {'a': {'input': 1.0, 'output': 10.0}}),
                     (None, ['(model not logged)', 'mystery-model']))

  def test_summary_prefers_log_counts_per_name_and_never_sums(self):
    bundle = {
        'logs': {'cloud_run': {'a': {'llm_calls': 2}}, 'gke': {}, 'unattributed': {'cloud_run:x': 3}, 'skipped': {}},
        'traces': {'cloud_run': {'a': {'llm_calls': 2}, 'b': {'llm_calls': 1}}, 'gke': {'c/n/w': {'llm_calls': 1}},
                   'unattributed': {'cloud_run:x': 5, 'cloud_run:y': 1}, 'skipped': {'gemini_enterprise': 4}},
        'log_source': 'ok', 'trace_source': 'ok', 'log_entries_scanned': 7, 'traces_scanned': 3, 'truncated': True,
    }
    s = ge_fleet.summarize_workload_token_attribution(bundle)
    self.assertEqual((s['cloud_run_services_with_tokens'], s['gke_workloads_with_tokens']), (2, 1))
    self.assertEqual(s['unattributed_llm_calls'], 4)  # x: logs 3 (not 3 + 5); y: traces 1
    self.assertEqual(s['top_unattributed'], ['cloud_run:x', 'cloud_run:y'])
    self.assertEqual((s['skipped_agent_engine_or_ge_calls'], s['truncated']), (4, True))
    self.assertEqual((s['log_entries_scanned'], s['traces_scanned']), (7, 3))
    empty = ge_fleet.summarize_workload_token_attribution(None)
    self.assertEqual((empty['log_source'], empty['trace_source'], empty['unattributed_llm_calls'], empty['top_unattributed']),
                     ('not_run', 'not_run', 0, []))


if __name__ == '__main__':
  unittest.main()
