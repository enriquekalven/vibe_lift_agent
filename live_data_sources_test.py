"""Tests: per-agent token sources (logs vs traces), dead-registration evidence, live FinOps math."""

import unittest

import ge_fleet
import live_finops

RE = '4895941110288875520'
RESOURCE = f'projects/project-maui/locations/us-central1/reasoningEngines/{RE}'


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


GE_RES = ('//discoveryengine.googleapis.com/projects/697625214430/locations/us/collections/default_collection/'
          'engines/ge-app-1/assistants/default_assistant/agents/core_assistant')


def _ge_trace(inp, out, platform='gcp.gemini_enterprise'):
  labels = {'cloud.platform': platform, 'cloud.resource.id': GE_RES, 'gen_ai.request.model': 'gemini-3.5-flash',
            'gen_ai.usage.input_tokens': str(inp), 'gen_ai.usage.output_tokens': str(out),
            'gen_ai.conversation.id': 'sessions/1'}
  return {'spans': [_span('1', None, 'AssistantService/StreamAssist', {'cloud.platform': platform}),
                    _span('2', '1', 'generate_content gemini-3.5-flash', labels)]}


class GeAssistantTraceTokensTest(unittest.TestCase):

  def test_per_app_totals(self):
    out = ge_fleet.aggregate_ge_assistant_usage([_ge_trace(1000, 20), _ge_trace(500, 5)])
    st = out['by_engine']['us/ge-app-1']
    self.assertEqual((st['llm_calls'], st['input_tokens'], st['output_tokens']), (2, 1500, 25))
    self.assertEqual(st['assistant_agents'], {'core_assistant': 2})
    self.assertEqual(st['models'], {'gemini-3.5-flash': 2})
    self.assertEqual(out['totals']['input_tokens'], 1500)

  def test_agent_engine_spans_not_counted_as_ge(self):
    out = ge_fleet.aggregate_ge_assistant_usage([_ge_trace(1000, 20, platform='gcp.agent_engine'), _trace(9, 9)])
    self.assertEqual(out['by_engine'], {})
    self.assertEqual(out['totals']['llm_calls'], 0)


def _agent(requests):
  return {'type': 'ADK', 'display_name': 'my-capital-agent', 'data_sources': [], 'notes': [],
          'backend': {'kind': 'agent_engine', 'resource': RESOURCE, 'reasoning_engine_id': RE,
                      'project': 'project-maui', 'location': 'us-central1'},
          'metrics': {'requests': None, 'errors_4xx': None, 'errors_5xx': None, 'llm_calls': None,
                      'input_tokens': None, 'output_tokens': None, 'cached_tokens': None,
                      'conversations': None, 'last_activity': None, '_req': requests}}


def _usage(calls, inp):
  return {'llm_calls': calls, 'input_tokens': inp, 'output_tokens': 1, 'cached_tokens': 0,
          'conversations': 1, 'last_activity': None, 'models': ['m']}


class TokenSourceJoinTest(unittest.TestCase):

  def _apply(self, logs, traces, requests=4):
    svc = ge_fleet.GeminiEnterpriseFleetService.__new__(ge_fleet.GeminiEnterpriseFleetService)
    svc.project_id = 'project-maui'
    agent = _agent(requests)
    results = {('re_requests', 'project-maui'): {RE: {'requests': requests, 'errors_4xx': 0, 'errors_5xx': 0}}}
    if logs is not None:
      results[('re_tokens', 'project-maui')] = {'by_engine': {RE: logs}}
    if traces is not None:
      results[('re_trace_tokens', 'project-maui')] = {'by_engine': {RE: traces}}
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
    return {'type': 'ADK', 'resource_name': 'projects/697625214430/locations/global/collections/default_collection/engines/e/assistants/default_assistant/agents/123',
            'backend': dict({'kind': 'agent_engine', 'resource': RESOURCE, 'location': 'us-central1'}, **backend)}

  def test_404_is_backend_not_found_with_cleanup_command(self):
    reg = ge_fleet.assess_registration(self._agent(), {('re_meta', RESOURCE): {'exists': False, 'http_status': 404}}, 't')
    self.assertEqual(reg['status'], 'BACKEND_NOT_FOUND')
    self.assertIn('HTTP 404', reg['evidence'])
    self.assertIn('curl -X DELETE', reg['action']['delete_command'])
    self.assertIn('discoveryengine.googleapis.com/v1alpha/projects/697625214430/locations/global', reg['action']['delete_command'])

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
    import telemetry_validator  # pylint: disable=g-import-not-at-top
    checks = []
    add = lambda **kw: checks.append(dict(kw))
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


if __name__ == '__main__':
  unittest.main()
