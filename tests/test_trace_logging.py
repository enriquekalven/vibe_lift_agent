"""Tests for the trace logging (observabilityConfig) controls.

Covers request validation (no credentialed request can leave *.googleapis.com), per-agent PATCH
results, the fleet cache (only API-confirmed changes), the set_agent_trace_logging MCP tool, and the
dashboard's confirm-then-apply flow, run in Node against the real template JavaScript.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from vibelift import fleet as ge_fleet
from vibelift import mcp_server, server
from vibelift.ui import template as ui_template

_ENGINE = 'projects/p1/locations/global/collections/default_collection/engines/eng1'
_LC = _ENGINE + '/assistants/default_assistant/agents/lc1'
_ADK = _ENGINE + '/assistants/default_assistant/agents/adk1'


def _raw_agents():
  return [
      {
          'name': _LC,
          'displayName': 'No-Code Support Agent',
          'state': 'ENABLED',
          'lowCodeAgentDefinition': {'nodes': []},
          'observabilityConfig': {'observabilityEnabled': False, 'sensitiveLoggingEnabled': False},
      },
      {
          'name': _ADK,
          'displayName': 'ADK Agent',
          'state': 'ENABLED',
          'adkAgentDefinition': {
              'provisionedReasoningEngine': {'reasoningEngine': 'projects/p1/locations/us-central1/reasoningEngines/123'},
          },
          'observabilityConfig': {'observabilityEnabled': True, 'sensitiveLoggingEnabled': True},
      },
  ]


class _FakeApi:
  """Serves one engine with two agents; PATCH echoes the config unless an error is configured."""

  def __init__(self):
    self.calls = []
    self.raw_agents = _raw_agents()
    self.patch_errors = {}
    self.list_errors = {}  # URL substring -> exception raised by matching GETs.
    self.patch_response = None
    self._lock = threading.Lock()

  def call(self, method, url, body=None):
    with self._lock:
      self.calls.append((method, url, body))
    if method == 'GET':
      for fragment, error in self.list_errors.items():
        if fragment in url:
          raise error
    if method == 'GET' and url.endswith('/engines/eng1'):
      return {'name': _ENGINE, 'displayName': 'Engine 1'}
    if method == 'GET' and '/agents' in url:
      return {'agents': self.raw_agents}
    if method == 'PATCH':
      res = url.split('/v1alpha/', 1)[-1].split('?', 1)[0]
      if res in self.patch_errors:
        raise self.patch_errors[res]
      if self.patch_response is not None:
        return self.patch_response
      return {'name': res, 'observabilityConfig': (body or {}).get('observabilityConfig') or {}}
    return {}

  def patches(self):
    return [c for c in self.calls if c[0] == 'PATCH']


def _cfg(enabled, sensitive=None):
  return {
      'observability_enabled': enabled,
      'sensitive_logging_enabled': enabled if sensitive is None else sensitive,
  }


class TraceLoggingServiceTest(unittest.TestCase):

  def setUp(self):
    self.api = _FakeApi()
    self.svc = ge_fleet.GeminiEnterpriseFleetService(
        project_id='p1', engine_ids=['eng1'], location='global', collection='default_collection',
        api=self.api, discover_unregistered=False)

  def _cached_configs(self):
    fleet = self.svc.collect(allow_stale=True)
    return {a['resource_name']: a.get('observability_config') for a in fleet['agents']}

  def test_rejects_inputs_that_could_redirect_the_request_before_any_api_call(self):
    bad_resources = [
        'projects/p1/locations/attacker.example?x=/collections/c/engines/e/assistants/a/agents/x',
        'projects/p1/locations/attacker.example#/collections/c/engines/e/assistants/a/agents/x',
        _LC + '?updateMask=displayName',
        _LC + '/../../../../x',
        'projects/p1"; curl evil; "/locations/global/collections/c/engines/e/assistants/a/agents/x',
        'projects/p1/locations/global/collections/c/engines/e/assistants/a/agents/x y',
    ]
    for bad in bad_resources:
      with self.subTest(resource=bad), self.assertRaises(ge_fleet.InvalidAgentRequestError):
        self.svc.enable_agent_observability(resource_name=bad, enabled=True)
    with self.assertRaises(ge_fleet.InvalidAgentRequestError):
      self.svc.enable_agent_observability(engine_id='eng1?x=1', enabled=True)
    with self.assertRaises(ge_fleet.InvalidAgentRequestError):
      self.svc.enable_agent_observability(location='attacker.example', enabled=True)
    self.assertEqual(self.api.calls, [])

  def test_google_api_url_allowlist(self):
    for url in ('https://discoveryengine.googleapis.com/v1alpha/x',
                'https://us-discoveryengine.googleapis.com/v1alpha/x',
                'https://logging.googleapis.com:443/v2/entries:list'):
      self.assertTrue(ge_fleet._is_google_api_url(url), url)
    for url in ('http://discoveryengine.googleapis.com/v1alpha/x',
                'https://attacker.example/x',
                'https://attacker.example?.googleapis.com/x',
                'https://googleapis.com.attacker.example/x',
                'https://user@discoveryengine.googleapis.com/x',
                'https://discoveryengine.googleapis.com:8443/x',
                'https://discoveryengine.googleapis.com:bad/x'):
      self.assertFalse(ge_fleet._is_google_api_url(url), url)

  def test_client_never_fetches_a_token_for_a_non_google_url(self):
    api = ge_fleet._GoogleApi('p1')
    with mock.patch.object(api, '_token') as token:
      with self.assertRaises(ge_fleet.FleetSourceError):
        api.call('PATCH', 'https://attacker.example/v1alpha/x', {})
    token.assert_not_called()

  def test_single_agent_enable_patches_and_updates_the_cache(self):
    res = self.svc.enable_agent_observability(resource_name=_LC, enabled=True)
    self.assertEqual((res['status'], res['updated_count'], res['failed_count']), ('OK', 1, 0))
    self.assertEqual(res['message'], 'Trace logging enabled for No-Code Support Agent.')
    self.assertEqual(res['results'][0]['observability_config'], _cfg(True))
    self.assertTrue(res['updated_at'].endswith('Z'))
    (_, url, body), = self.api.patches()
    self.assertEqual(url, f'https://discoveryengine.googleapis.com/v1alpha/{_LC}?updateMask=observabilityConfig')
    self.assertEqual(body, {'observabilityConfig': {'observabilityEnabled': True, 'sensitiveLoggingEnabled': True}})
    self.assertEqual(self._cached_configs()[_LC], _cfg(True))

  def test_failed_patch_reports_the_error_and_keeps_the_cached_state(self):
    self.api.patch_errors[_LC] = ge_fleet.FleetSourceError(403, 'Permission denied')
    res = self.svc.enable_agent_observability(resource_name=_LC, enabled=True)
    self.assertEqual((res['status'], res['updated_count'], res['failed_count']), ('FAILED', 0, 1))
    result = res['results'][0]
    self.assertEqual((result['status'], result['http_status']), ('ERROR', 403))
    self.assertIn('discoveryengine.agents.update', result['error'])
    self.assertEqual(result['observability_config'], _cfg(False))  # Last known state, not the request.
    self.assertTrue(res['message'].startswith('Could not enable trace logging for No-Code Support Agent: HTTP 403'))
    self.assertEqual(self._cached_configs()[_LC], _cfg(False))

  def test_partial_bulk_update_only_marks_confirmed_agents(self):
    self.api.patch_errors[_ADK] = ge_fleet.FleetSourceError(500, 'backend error')
    res = self.svc.enable_agent_observability(enabled=False)
    self.assertEqual((res['status'], res['targeted_count'], res['updated_count']), ('PARTIAL', 2, 1))
    by_res = {r['resource_name']: r for r in res['results']}
    self.assertEqual(by_res[_LC]['status'], 'OK')
    self.assertEqual(by_res[_ADK]['status'], 'ERROR')
    self.assertIn('ADK Agent', res['message'])
    cached = self._cached_configs()
    self.assertEqual(cached[_LC], _cfg(False))
    self.assertEqual(cached[_ADK], _cfg(True))  # The failed agent still shows its real (unchanged) state.

  def test_bulk_results_keep_target_order(self):
    self.api.raw_agents = [
        dict(_raw_agents()[0], name=f'{_ENGINE}/assistants/default_assistant/agents/lc{i}', displayName=f'LC {i}')
        for i in range(12)
    ]
    res = self.svc.enable_agent_observability(enabled=True, only_low_code=True)
    self.assertEqual((res['status'], res['updated_count']), ('OK', 12))
    self.assertEqual(res['message'], 'Trace logging enabled on 12 agents.')
    self.assertEqual([r['display_name'] for r in res['results']], [f'LC {i}' for i in range(12)])

  def test_omitted_false_fields_mean_disabled(self):
    self.api.patch_response = {'name': _ADK, 'observabilityConfig': {}}  # Proto3 JSON omits false values.
    res = self.svc.enable_agent_observability(resource_name=_ADK, enabled=False)
    self.assertEqual(res['status'], 'OK')
    self.assertEqual(res['results'][0]['observability_config'], _cfg(False))
    self.assertEqual(self._cached_configs()[_ADK], _cfg(False))

  def test_agent_missing_from_snapshot_uses_its_regional_host(self):
    regional = 'projects/p1/locations/us/collections/default_collection/engines/eng9/assistants/default_assistant/agents/a9'
    res = self.svc.enable_agent_observability(resource_name=regional, enabled=True)
    self.assertEqual(res['status'], 'OK')
    self.assertIsNone(res['results'][0]['type'])  # Unknown, not guessed.
    self.assertEqual(
        self.api.patches()[0][1],
        f'https://us-discoveryengine.googleapis.com/v1alpha/{regional}?updateMask=observabilityConfig')
    self.assertIn('us-discoveryengine.googleapis.com', res['curl_command'])

  def test_no_matching_agents(self):
    self.api.raw_agents = [_raw_agents()[1]]
    res = self.svc.enable_agent_observability(enabled=True, only_low_code=True)
    self.assertEqual((res['status'], res['targeted_count']), ('NO_AGENTS_MATCHED', 0))
    self.assertEqual(res['message'], 'No No-Code / Low-Code agents found to update.')
    self.assertEqual(self.api.patches(), [])

  def test_cold_cache_bulk_update_lists_the_agents_directly(self):
    # A new instance whose telemetry collection is still warming has no agents in its snapshot.
    with mock.patch.object(self.svc, 'collect', return_value=self.svc._warming_placeholder(24)):
      res = self.svc.enable_agent_observability(enabled=True)
    self.assertEqual((res['status'], res['targeted_count'], res['updated_count']), ('OK', 2, 2))
    self.assertEqual(res['message'], 'Trace logging enabled on 2 agents.')
    patched = sorted(c[1].split('/v1alpha/', 1)[-1].split('?', 1)[0] for c in self.api.patches())
    self.assertEqual(patched, sorted([_LC, _ADK]))

  def test_cold_cache_listing_failure_reports_the_reason(self):
    self.api.list_errors['/agents'] = ge_fleet.FleetSourceError(403, 'Permission denied')
    with mock.patch.object(self.svc, 'collect', return_value=self.svc._warming_placeholder(24)):
      res = self.svc.enable_agent_observability(enabled=True, only_low_code=True)
    self.assertEqual((res['status'], res['targeted_count']), ('FAILED', 0))
    self.assertEqual(res['message'], 'Could not list the Gemini Enterprise agents: HTTP 403: Permission denied')
    self.assertEqual(self.api.patches(), [])

  def test_cold_cache_partial_listing_is_not_reported_as_ok(self):
    self.svc.engine_ids = ['eng1', 'eng2']
    self.api.list_errors['/engines/eng2'] = ge_fleet.FleetSourceError(403, 'Permission denied')
    with mock.patch.object(self.svc, 'collect', return_value=self.svc._warming_placeholder(24)):
      res = self.svc.enable_agent_observability(enabled=False)
    self.assertEqual((res['status'], res['updated_count'], res['failed_count']), ('PARTIAL', 2, 0))
    self.assertEqual(res['message'], 'Trace logging disabled on 2 agents. 1 app(s) could not be listed '
                     '(HTTP 403: Permission denied).')

  def test_warm_snapshot_is_used_without_extra_listing(self):
    self.svc.collect(allow_stale=True)  # Warm the cache.
    gets_before = sum(1 for c in self.api.calls if c[0] == 'GET')
    res = self.svc.enable_agent_observability(enabled=True)
    self.assertEqual(res['status'], 'OK')
    self.assertEqual(sum(1 for c in self.api.calls if c[0] == 'GET'), gets_before)

  def test_sensitive_logging_never_outlives_trace_logging(self):
    self.svc.enable_agent_observability(resource_name=_LC, enabled=True, sensitive_logging=False)
    self.svc.enable_agent_observability(resource_name=_LC, enabled=False, sensitive_logging=True)
    bodies = [c[2]['observabilityConfig'] for c in self.api.patches()]
    self.assertEqual(bodies, [
        {'observabilityEnabled': True, 'sensitiveLoggingEnabled': False},
        {'observabilityEnabled': False, 'sensitiveLoggingEnabled': False},
    ])

  def test_script_builder_rejects_values_that_would_break_out_of_the_shell_text(self):
    base = {'project_id': 'p1', 'location': 'global', 'collection': 'default_collection', 'engine_id': 'eng1'}
    for field, value in (('project_id', 'p1"; rm -rf ~; "'), ('collection', 'c$(id)'),
                         ('engine_id', 'e`id`'), ('agent_resource_name', _LC + ';id'), ('location', 'us;id')):
      with self.subTest(field=field), self.assertRaises(ge_fleet.InvalidAgentRequestError):
        ge_fleet.build_enable_agent_logging_script(**dict(base, **{field: value}))
    placeholders = ge_fleet.build_enable_agent_logging_script(project_id=None, engine_id=None)
    self.assertIn('<PROJECT_ID>', placeholders['script'])
    self.assertIn('<ENGINE_ID>', placeholders['script'])


class TraceLoggingControllerTest(unittest.TestCase):

  def setUp(self):
    self.api = _FakeApi()
    self.svc = ge_fleet.GeminiEnterpriseFleetService(
        project_id='p1', engine_ids=['eng1'], location='global', collection='default_collection',
        api=self.api, discover_unregistered=False)
    self.ctrl = server.VibeLiftRuntimeController(fleet_service=self.svc)

  def test_malformed_request_returns_invalid_request_without_api_calls(self):
    res = self.ctrl.enable_agent_observability({
        'resource_name': 'projects/p1/locations/evil.example?/collections/c/engines/e/assistants/a/agents/x',
        'enabled': True,
    })
    self.assertEqual(res['status'], 'INVALID_REQUEST')
    self.assertEqual(res['message'], 'Invalid Gemini Enterprise agent resource name.')
    self.assertEqual((res['results'], res['updated_count']), ([], 0))
    script_res = self.ctrl.enable_agent_observability({'engine_id': 'e$(id)', 'enabled': True, 'script_only': True})
    self.assertEqual(script_res['status'], 'INVALID_REQUEST')
    self.assertEqual(self.api.calls, [])

  def test_string_booleans_and_disable(self):
    res = self.ctrl.enable_agent_observability({'resource_name': _ADK, 'enabled': 'false'})
    self.assertEqual((res['status'], res['action'], res['enabled']), ('OK', 'DISABLED', False))
    self.assertEqual(self.api.patches()[0][2]['observabilityConfig'],
                     {'observabilityEnabled': False, 'sensitiveLoggingEnabled': False})

  def test_script_only_uses_the_engine_location_and_a_placeholder_for_auto(self):
    self.svc.engine_ids = ['us/auto']
    res = self.ctrl.enable_agent_observability({'enabled': True, 'script_only': True})
    self.assertEqual(res['status'], 'SCRIPT_ONLY')
    self.assertIn('ENGINE_ID="<ENGINE_ID>"', res['script'])
    self.assertIn('us-discoveryengine.googleapis.com', res['script'])
    self.assertEqual(self.api.calls, [])


class TraceLoggingMcpToolTest(unittest.TestCase):

  def _call(self, name, arguments):
    code, _, body = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0', 'id': 'trace', 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments},
    })
    self.assertEqual(code, 200)
    return json.loads(body)['result']

  def test_tool_declaration(self):
    _, _, body = mcp_server.handle_jsonrpc_sync({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    tool = {t['name']: t for t in json.loads(body)['result']['tools']}['set_agent_trace_logging']
    self.assertEqual(tool['inputSchema']['required'], ['enabled'])
    self.assertEqual(tool['annotations']['readOnlyHint'], False)
    self.assertEqual(tool['annotations']['idempotentHint'], True)
    self.assertEqual(tool['_meta']['ui']['visibility'], ['model', 'app'])

  def test_missing_enabled_is_rejected_without_calling_the_controller(self):
    with mock.patch.object(server._global_controller, 'enable_agent_observability') as ctrl_call:
      result = self._call('set_agent_trace_logging', {'resource_name': _LC})
    ctrl_call.assert_not_called()
    self.assertEqual(result['structuredContent']['status'], 'INVALID_REQUEST')

  def test_prefixed_call_forwards_known_arguments_and_hides_shell_text(self):
    reply = {
        'status': 'OK', 'message': 'Trace logging enabled for No-Code Support Agent.', 'results': [],
        'script': '#!/bin/bash', 'curl_command': 'curl -s -X PATCH',
    }
    with mock.patch.object(server._global_controller, 'enable_agent_observability', return_value=reply) as ctrl_call:
      result = self._call('custom_mcp_1_agent__set_agent_trace_logging',
                          {'enabled': True, 'resource_name': _LC, 'only_low_code': None, 'unexpected': 'x'})
    ctrl_call.assert_called_once_with({'enabled': True, 'resource_name': _LC})
    self.assertFalse(result.get('isError'))
    self.assertEqual(result['content'][0]['text'], 'Trace logging enabled for No-Code Support Agent.')
    self.assertNotIn('script', result['structuredContent'])
    self.assertNotIn('curl_command', result['structuredContent'])


# Minimal DOM for running the dashboard's real trace logging functions in Node.
_JS_PRELUDE = r"""
class Node {
  constructor(tag) {
    this.tagName = tag; this.childNodes = []; this.className = ''; this.listeners = {};
    this.disabled = false; this.title = ''; this.type = '';
    const self = this;
    const names = function() { return new Set(String(self.className).split(/\s+/).filter(Boolean)); };
    const save = function(s) { self.className = Array.from(s).join(' '); };
    this.classList = {
      add: function(c) { const s = names(); s.add(c); save(s); },
      remove: function(c) { const s = names(); s.delete(c); save(s); },
      toggle: function(c, force) {
        const s = names(); const on = force === undefined ? !s.has(c) : !!force;
        if (on) s.add(c); else s.delete(c); save(s); return on;
      },
      contains: function(c) { return names().has(c); },
    };
  }
  appendChild(c) { this.childNodes.push(c); return c; }
  replaceChildren() {
    this.childNodes = Array.prototype.slice.call(arguments).map(function(c) {
      return c instanceof Node ? c : new TextNode(String(c));
    });
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { if (!this.disabled) (this.listeners.click || []).forEach(function(fn) { fn({}); }); }
  get textContent() { return this.childNodes.map(function(c) { return c.textContent; }).join(''); }
}
class TextNode extends Node {
  constructor(text) { super('#text'); this.text = text; }
  get textContent() { return this.text; }
}
const elements = {};
['traceBulkBar', 'enableLowCodeObsBtn', 'disableLowCodeObsBtn', 'enableAllObsBtn', 'disableAllObsBtn']
  .forEach(function(id) { elements[id] = new Node(id === 'traceBulkBar' ? 'div' : 'button'); });
elements.traceBulkBar.className = 'trace-bar hidden';
const document = {
  createElement: function(tag) { return new Node(tag); },
  createTextNode: function(text) { return new TextNode(text); },
  getElementById: function(id) { return elements[id] || null; },
};
const timers = [];
function setTimeout(fn, ms) { timers.push({fn: fn, ms: ms}); return timers.length; }
let embedded = true;
function isEmbedded() { return embedded; }
const hostCalls = [];
let hostReply = null;
function callHost(method, params, timeoutMs) {
  hostCalls.push({method: method, params: params, timeoutMs: timeoutMs});
  return Promise.resolve().then(function() { return hostReply(params); });
}
const fetchCalls = [];
let fetchReply = null;
function fetch(url, opts) { fetchCalls.push({url: url, opts: opts}); return Promise.resolve(fetchReply(url, opts)); }
function notifyHostSizeChanged() {}
let renderCount = 0;
function renderFleet() { renderCount += 1; }
var geRawFleet = null;
var lastFleet = null;
"""

_JS_SCENARIO = r"""
const flush = function() { return new Promise(function(r) { setImmediate(r); }); };
const buttons = function(node) {
  const found = [];
  (function walk(n) { if (n.tagName === 'button') found.push(n); n.childNodes.forEach(walk); })(node);
  return found;
};
const btn = function(node, label) { return buttons(node).find(function(b) { return b.textContent === label; }); };
const labels = function(node) { return buttons(node).map(function(b) { return b.textContent; }); };
const RES = 'projects/p1/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/agents/lc1';
const ADK = 'projects/p1/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/agents/adk1';
const okReply = function(enabled, res) {
  return {isError: false, structuredContent: {
    status: 'OK', message: 'Trace logging ' + (enabled ? 'enabled' : 'disabled') + ' for X.',
    updated_at: '2026-10-02T10:00:05Z',
    results: [{status: 'OK', resource_name: res,
               observability_config: {observability_enabled: enabled, sensitive_logging_enabled: enabled}}],
  }};
};

(async function() {
  const out = {};
  const agent = {display_name: 'No-Code Support Agent', resource_name: RES, type: 'LOW_CODE',
                 observability_config: {observability_enabled: false, sensitive_logging_enabled: false}};
  const adk = {display_name: 'ADK Agent', resource_name: ADK, type: 'ADK',
               observability_config: {observability_enabled: true, sensitive_logging_enabled: true}};
  geRawFleet = {generated_at: '2026-10-02T10:00:00Z', agents: [agent, adk]};
  lastFleet = geRawFleet;
  let wrap = el('div', 'fleet-agent-tags trace-row', []);
  traceRowEls[RES] = {wrap: wrap, agent: agent};
  fillTraceRow(wrap, agent);
  out.initial = wrap.textContent;

  // 1. First click only asks; nothing is sent.
  btn(wrap, 'Enable trace').click();
  out.confirm = wrap.textContent;
  out.confirmLabels = labels(wrap);
  out.callsBeforeYes = hostCalls.length;

  // 2. An auto-refresh re-render keeps the pending confirmation.
  wrap = el('div', 'fleet-agent-tags trace-row', []);
  traceRowEls = {};
  traceRowEls[RES] = {wrap: wrap, agent: agent};
  fillTraceRow(wrap, agent);
  out.confirmAfterRefresh = wrap.textContent;

  // 3. Cancel returns to the toggle; asking again and confirming applies it through the host bridge.
  btn(wrap, 'Cancel').click();
  out.afterCancel = labels(wrap);
  btn(wrap, 'Enable trace').click();
  hostReply = function() { return okReply(true, RES); };
  btn(wrap, 'Yes, enable').click();
  out.busy = labels(wrap);
  out.busyDisabled = btn(wrap, 'Enabling…').disabled;
  await flush();
  out.hostCall = hostCalls[0];
  out.done = wrap.textContent;
  out.agentConfig = agent.observability_config;
  timers.forEach(function(t) { t.fn(); });
  out.afterTimer = wrap.textContent;

  // 4. A snapshot older than the confirmed change cannot flip the badge back; a newer one wins.
  const stale = {generated_at: '2026-10-02T10:00:01Z', agents: [{resource_name: RES,
    observability_config: {observability_enabled: false, sensitive_logging_enabled: false}}]};
  applyTraceOverrides(stale);
  out.staleSnapshot = stale.agents[0].observability_config.observability_enabled;
  const fresh = {generated_at: '2026-10-02T10:05:00Z', agents: [{resource_name: RES,
    observability_config: {observability_enabled: false, sensitive_logging_enabled: false}}]};
  applyTraceOverrides(fresh);
  out.freshSnapshot = fresh.agents[0].observability_config.observability_enabled;
  out.overrideCleared = !(RES in traceOverrides);

  // 5. Errors are shown inline and leave the state unchanged.
  agent.observability_config = {observability_enabled: true, sensitive_logging_enabled: true};
  fillTraceRow(wrap, agent);
  const errorCases = [
    ['failed', function() { return {isError: false, structuredContent: {status: 'FAILED',
      message: 'Could not disable trace logging for X: HTTP 403: denied', results: [{status: 'ERROR', resource_name: RES}]}}; }],
    ['isError', function() { return {isError: true, content: [{type: 'text', text: 'Tool failed (ref abc)'}]}; }],
    ['rpcError', function() { throw new Error('Unknown tool'); }],
    ['timeout', function() { throw new Error('Timeout waiting for tools/call'); }],
  ];
  out.errors = {};
  for (const [name, reply] of errorCases) {
    hostReply = reply;
    btn(wrap, 'Disable trace').click();
    btn(wrap, 'Yes, disable').click();
    await flush();
    out.errors[name] = wrap.textContent;
  }
  out.configAfterErrors = agent.observability_config.observability_enabled;

  // 6. Bulk: confirm bar, buttons disabled while running, partial failures named.
  askBulkTrace(true, true);
  const bar = elements.traceBulkBar;
  out.bulkConfirm = bar.textContent;
  out.bulkVisible = !bar.classList.contains('hidden');
  hostCalls.length = 0;
  let releaseBulk = null;
  hostReply = function() {
    return new Promise(function(resolve) { releaseBulk = resolve; });
  };
  btn(bar, 'Yes, enable').click();
  await flush();
  out.bulkBusy = bar.textContent;
  out.bulkButtonsDisabled = elements.enableAllObsBtn.disabled;
  releaseBulk({isError: false, structuredContent: {status: 'PARTIAL', message: 'Trace logging enabled on 1 of 2 agents.',
    updated_at: '2026-10-02T10:06:00Z', results: [
      {status: 'OK', resource_name: RES, display_name: 'No-Code Support Agent',
       observability_config: {observability_enabled: true, sensitive_logging_enabled: true}},
      {status: 'ERROR', resource_name: ADK, display_name: 'ADK Agent', error: 'HTTP 500'}]}});
  await flush();
  await flush();
  out.bulkCall = hostCalls[0];
  out.bulkDone = bar.textContent;
  out.bulkButtonsEnabledAgain = !elements.enableAllObsBtn.disabled;
  out.renderCount = renderCount;
  btn(bar, 'Close').click();
  out.bulkClosed = bar.classList.contains('hidden');

  // 7. Nothing to update: no Yes button.
  geRawFleet = {generated_at: '2026-10-02T10:00:00Z', agents: [adk]};
  askBulkTrace(true, true);
  out.bulkEmpty = bar.textContent;
  out.bulkEmptyLabels = labels(bar);

  // 8. Standalone (direct Cloud Run URL): POST /api, server error message shown.
  embedded = false;
  fetchReply = function() { return {ok: false, status: 500, json: function() { return Promise.resolve({message: 'Server says no'}); }}; };
  btn(wrap, 'Disable trace').click();
  btn(wrap, 'Yes, disable').click();
  await flush();
  await flush();
  out.fetchCall = {url: fetchCalls[0].url, method: fetchCalls[0].opts.method, body: JSON.parse(fetchCalls[0].opts.body)};
  out.fetchError = wrap.textContent;

  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})().catch(function(err) { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


def _extract(html: str, start: str, end: str) -> str:
  i = html.index(start)
  return html[i:html.index(end, i)]


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TraceLoggingDashboardFlowTest(unittest.TestCase):
  """Runs the template's real trace logging JavaScript in Node with a minimal DOM."""

  @classmethod
  def setUpClass(cls):
    html = ui_template.render_dashboard_html()
    cls.html = html
    helpers = _extract(html, '    function el(tag, className, children) {', '    function orDash(')
    trace_block = _extract(html, '    // ---------------- Trace logging (observabilityConfig)', '    let fleetRequestSeq = 0;')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
      f.write(_JS_PRELUDE + helpers + trace_block + _JS_SCENARIO)
      path = f.name
    try:
      proc = subprocess.run([shutil.which('node'), path], capture_output=True, text=True, check=False, timeout=60)
    finally:
      os.unlink(path)
    if proc.returncode != 0:
      raise AssertionError(f'Node harness failed: {proc.stderr}')
    cls.out = json.loads(proc.stdout)

  def test_first_click_only_asks_for_confirmation(self):
    out = self.out
    self.assertEqual(out['initial'], 'Trace: OFFEnable trace')
    self.assertIn('Are you sure? Enable trace logging for No-Code Support Agent', out['confirm'])
    self.assertEqual(out['confirmLabels'], ['Yes, enable', 'Cancel'])
    self.assertEqual(out['callsBeforeYes'], 0)
    self.assertIn('Are you sure?', out['confirmAfterRefresh'])
    self.assertEqual(out['afterCancel'], ['Enable trace'])

  def test_yes_applies_the_change_through_the_host_bridge(self):
    out = self.out
    self.assertEqual(out['busy'], ['Enabling…'])
    self.assertTrue(out['busyDisabled'])
    self.assertEqual(out['hostCall']['method'], 'tools/call')
    self.assertEqual(out['hostCall']['params'], {
        'name': 'set_agent_trace_logging',
        'arguments': {'resource_name': _LC, 'enabled': True, 'sensitive_logging': True},
    })
    self.assertIn('set_agent_trace_logging', mcp_server._TOOLS_BY_NAME)
    self.assertEqual(out['done'], 'Trace: ONDisable trace✓ Trace logging enabled.')
    self.assertEqual(out['agentConfig'], _cfg(True))
    self.assertEqual(out['afterTimer'], 'Trace: ONDisable trace')

  def test_confirmed_state_survives_stale_snapshots_only(self):
    self.assertTrue(self.out['staleSnapshot'])
    self.assertFalse(self.out['freshSnapshot'])
    self.assertTrue(self.out['overrideCleared'])

  def test_errors_are_shown_inline_and_change_nothing(self):
    errors = self.out['errors']
    self.assertIn('Could not disable trace logging for X: HTTP 403: denied', errors['failed'])
    self.assertIn('Could not disable trace logging: Tool failed (ref abc)', errors['isError'])
    self.assertIn('Could not disable trace logging: Unknown tool', errors['rpcError'])
    self.assertIn('Gemini Enterprise did not answer in time', errors['timeout'])
    for text in errors.values():
      self.assertTrue(text.startswith('Trace: ONDisable trace'), text)
    self.assertTrue(self.out['configAfterErrors'])

  def test_bulk_confirm_run_and_partial_result(self):
    out = self.out
    self.assertIn('Are you sure? Enable trace logging on 1 No-Code / Low-Code agent across all Gemini Enterprise apps',
                  out['bulkConfirm'])
    self.assertTrue(out['bulkVisible'])
    self.assertEqual(out['bulkBusy'], 'Enabling trace logging on 1 No-Code / Low-Code agent…')
    self.assertTrue(out['bulkButtonsDisabled'])
    self.assertEqual(out['bulkCall']['params'], {
        'name': 'set_agent_trace_logging',
        'arguments': {'enabled': True, 'sensitive_logging': True, 'only_low_code': True},
    })
    self.assertEqual(out['bulkCall']['timeoutMs'], 120000)
    self.assertIn('Trace logging enabled on 1 of 2 agents. Not updated: ADK Agent.', out['bulkDone'])
    self.assertTrue(out['bulkButtonsEnabledAgain'])
    self.assertGreaterEqual(out['renderCount'], 1)
    self.assertTrue(out['bulkClosed'])
    self.assertIn('No No-Code / Low-Code agents found to update.', out['bulkEmpty'])
    self.assertEqual(out['bulkEmptyLabels'], ['Close'])

  def test_standalone_mode_posts_to_the_api_and_shows_server_errors(self):
    out = self.out
    self.assertEqual(out['fetchCall'], {
        'url': '/api/enable_agent_observability', 'method': 'POST',
        'body': {'resource_name': _LC, 'enabled': False, 'sensitive_logging': False},
    })
    self.assertIn('Could not disable trace logging: Server says no', out['fetchError'])

  def test_row_controls_never_open_the_script_drawer(self):
    row_code = _extract(self.html, '    function fillTraceRow(', '    function traceCandidates(')
    self.assertNotIn('agentObsDrawer', row_code)
    self.assertEqual(len(re.findall(r'toggleAgentObsDrawer\(\)', self.html)), 2)  # Definition + Trace Script button.


if __name__ == '__main__':
  unittest.main()
