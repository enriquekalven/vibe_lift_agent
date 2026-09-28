"""Comprehensive unit and HTTP UI integration tests for VibeLift Analytics Platform."""

import json
import os
import re
import threading
import tomllib
import unittest
from unittest import mock
import urllib.error
import urllib.request

import alpha_evolve_optimizer
import long_running_agent
import server
import telemetry
import gcp_telemetry
import ge_fleet
import ge_fleet_test
import mcp_server
from app import agent as adk_agent_module

_FAKE_FLEET, _FAKE_FLEET_API = ge_fleet_test.make_fake_service()


def setUpModule() -> None:
  # The dashboard, MCP tools, and ADK tools share one fleet service; serve it from the offline fake.
  server._global_controller.ge_fleet = _FAKE_FLEET
  ge_fleet._SERVICE = _FAKE_FLEET


_FLEET_AGENT_NAMES = ['Deep Research', 'IT Service Desk', 'VibeLift Analytics & FinOps']


class VibeLiftFrameworkTest(unittest.TestCase):

  def test_turn_usage_log_cache_ratio_and_costs(self) -> None:
    log_entry = telemetry.TurnUsageLog(
        timestamp='2026-09-18T21:30:00Z',
        agent_name='vibelift-long-running-adk-agent',
        model='gemini-3.1-flash',
        turn_index=4,
        prompt_prefix_hash='gen14_static_8f91a2c',
        cache_breakpoint_line=None,
        cache_breakpoint_reason='100% static prefix match across turns',
        prompt_token_count=20000,
        cached_content_token_count=18000,
        cache_creation_input_tokens=0,
        uncached_input_tokens=2000,
        candidates_token_count=500,
        thoughts_token_count=100,
        status_code=200,
        tool_called='vertex_ai.code_search',
        evolution_generation=14,
    )
    self.assertAlmostEqual(log_entry.cache_hit_ratio, 90.0)
    naive_usd, actual_usd, saved_usd = log_entry.compute_costs()
    self.assertGreater(naive_usd, actual_usd)
    self.assertAlmostEqual(saved_usd, round(naive_usd - actual_usd, 6))

  def test_rate_card_models_supported(self) -> None:
    self.assertIn('gemini-2.5-flash', telemetry.MODEL_RATE_CARDS)
    self.assertIn('gemini-2.5-pro', telemetry.MODEL_RATE_CARDS)
    self.assertIn('gemini-1.5-flash', telemetry.MODEL_RATE_CARDS)
    self.assertIn('gemini-1.5-pro', telemetry.MODEL_RATE_CARDS)

  def test_detect_prefix_breakpoint_identifies_line_and_match(self) -> None:
    prev_prompt = [
        'System Role: Enterprise IT Service Desk Escalation Router',
        'Current Time: 2026-09-18T20:00:00Z',
        'Static Tool Schema Block',
    ]
    curr_prompt = [
        'System Role: Enterprise IT Service Desk Escalation Router',
        'Current Time: 2026-09-18T20:05:00Z',
        'Static Tool Schema Block',
    ]
    line_no, reason, digest = telemetry.detect_prefix_breakpoint(
        prev_prompt, curr_prompt
    )
    self.assertEqual(line_no, 2)
    self.assertIn('Line 2 mutated', reason)
    self.assertEqual(len(digest), 10)

    no_break, match_reason, _ = telemetry.detect_prefix_breakpoint(
        curr_prompt, curr_prompt
    )
    self.assertIsNone(no_break)
    self.assertIn('100% static prefix match', match_reason)

  def test_long_running_agent_trajectory_and_step_turn(self) -> None:
    optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    agent = long_running_agent.LongRunningVibeLiftAgent(optimizer)
    self.assertEqual(len(agent.turns), 6)
    self.assertEqual(agent.turns[1].cache_breakpoint_line, 1)
    self.assertIsNone(agent.turns[5].cache_breakpoint_line)
    self.assertGreater(agent.turns[5].cache_hit_ratio, 85.0)

    next_turn = agent.step_turn()
    self.assertEqual(next_turn.turn_index, 7)
    self.assertEqual(len(agent.turns), 7)

  def test_alpha_evolve_multi_agent_selection_and_custom_params(self) -> None:
    optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
    self.assertEqual(len(optimizer.list_agents_summary()), 3)
    profile = optimizer.select_agent('vibelift_analytics')
    self.assertEqual(profile.agent_id, 'vibelift_analytics')

    initial_param_count = len(profile.parameters)
    optimizer.add_user_parameter(
        label='MCP Sub-Second Response Rate',
        unit='%',
        direction='higher_is_better',
        baseline_val=88.0,
        target_val=99.0,
        weight_pct=15,
    )
    self.assertEqual(len(profile.parameters), initial_param_count + 1)

    optimizer.inject_anomaly()
    self.assertIn('CRITICAL', profile.health_status)

    gen_next = optimizer.run_next_generation()
    self.assertEqual(gen_next.timeline[-1].generation, 12)
    self.assertIn('OPTIMIZED', profile.health_status)

  def test_gcp_telemetry_service_collection(self) -> None:
    service = gcp_telemetry.get_gcp_telemetry_service()
    self.assertTrue(service.project_id)
    summary = service.get_cloud_telemetry_summary()
    self.assertIn('project_id', summary)
    self.assertIn('cloud_run_services', summary)
    self.assertIn('bigquery_datasets', summary)
    self.assertIn('vibelift_analytics', summary['bigquery_datasets'])
    self.assertIn('cloud_run_agent_count', summary)

  def test_ge_fleet_service_is_shared_by_dashboard_mcp_and_adk(self) -> None:
    self.assertIs(ge_fleet.get_ge_fleet_service(), server._global_controller.ge_fleet)
    fleet = server._global_controller.get_fleet_payload(window_hours=24)
    self.assertEqual(fleet['source'], 'gemini_enterprise')
    self.assertEqual(fleet['totals']['agents'], 3)
    self.assertEqual(sorted(a['display_name'] for a in fleet['agents']), _FLEET_AGENT_NAMES)

  def test_adk_agent_tools(self) -> None:
    gcp_res = json.loads(adk_agent_module.query_gcp_telemetry())
    self.assertIn('project_id', gcp_res)
    fleet_res = json.loads(adk_agent_module.query_ge_agent_fleet())
    self.assertEqual(fleet_res['totals']['agents'], 3)
    self.assertIn('IT Service Desk', fleet_res['summary'])
    calc_res = json.loads(adk_agent_module.calculate_cache_economics(
        prompt_token_count=10000,
        cached_content_token_count=8000,
        model='gemini-2.5-flash',
    ))
    self.assertEqual(calc_res['cache_hit_ratio_pct'], 80.0)
    self.assertGreater(calc_res['naive_cost_usd'], calc_res['actual_cost_usd'])

    # Test list_cloud_run_agents
    services_res = json.loads(adk_agent_module.list_cloud_run_agents())
    self.assertIsInstance(services_res, list)
    self.assertGreater(len(services_res), 0)

    # Test detect_prompt_breakpoint tool
    prev_prompt = "Prefix Line 1\nPrefix Line 2"
    curr_prompt = "Prefix Line 1\nMutated Line 2"
    bp_res = json.loads(adk_agent_module.detect_prompt_breakpoint(prev_prompt, curr_prompt))
    self.assertEqual(bp_res['breakpoint_line'], 2)
    self.assertIn('Line 2 mutated', bp_res['reason'])

    # Test trigger_alpha_evolve_cycle tool
    evolve_res = json.loads(adk_agent_module.trigger_alpha_evolve_cycle('it_service_desk'))
    self.assertEqual(evolve_res['agent_id'], 'it_service_desk')
    self.assertIn('active_generation', evolve_res)

  def test_http_server_renders_ui_and_executes_rest_api_end_to_end(self) -> None:
    httpd = server.create_http_server(host='127.0.0.1', port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
      base_url = f'http://127.0.0.1:{port}'
      # 1. Verify GET / renders the title and Google Cloud metadata
      with urllib.request.urlopen(f'{base_url}/') as resp:
        self.assertEqual(resp.status, 200)
        html_text = resp.read().decode('utf-8')
        self.assertIn(
            'VibeLift | Analytics Platform for Agent Optimization', html_text
        )
        self.assertIn('vibelift_googley_logo_1789766299532.jpg', html_text)
        self.assertIn('data:image/jpeg;base64,', html_text)
        self.assertIn('Gemini Enterprise Agent Fleet', html_text)
        self.assertNotIn('Agent Registry', html_text)
        self.assertIn('Sync GCP Telemetry', html_text)
        self.assertIn('IT Service Desk', html_text)

      # 1b. Verify GET /vibelift_googley_logo_1789766299532.jpg returns JPEG bytes
      with urllib.request.urlopen(
          f'{base_url}/vibelift_googley_logo_1789766299532.jpg'
      ) as logo_resp:
        self.assertEqual(logo_resp.status, 200)
        self.assertEqual(logo_resp.headers.get('Content-Type'), 'image/jpeg')
        self.assertGreater(len(logo_resp.read()), 1000)

      # 2. Verify GET /api/state includes active_agent & available_agents
      with urllib.request.urlopen(f'{base_url}/api/state') as resp:
        state = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(
            state['active_agent']['agent_id'], 'it_service_desk'
        )
        self.assertEqual(len(state['available_agents']), 3)
        self.assertEqual(state['ge_fleet']['totals']['agents'], 3)

      # 3. Verify GET /api/gcp_telemetry
      with urllib.request.urlopen(f'{base_url}/api/gcp_telemetry') as resp:
        self.assertEqual(resp.status, 200)
        telemetry_state = json.loads(resp.read().decode('utf-8'))
        self.assertIn('project_id', telemetry_state)

      # 4. Verify GET /api/ge_fleet returns every agent on the Gemini Enterprise app
      with urllib.request.urlopen(f'{base_url}/api/ge_fleet?window_hours=168') as resp:
        self.assertEqual(resp.status, 200)
        fleet_state = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(fleet_state['window_hours'], 168)
        self.assertEqual(sorted(a['display_name'] for a in fleet_state['agents']), _FLEET_AGENT_NAMES)

      # 5. Verify POST /api/sync_gcp_telemetry
      req_sync_gcp = urllib.request.Request(
          f'{base_url}/api/sync_gcp_telemetry', data=b'{}', method='POST'
      )
      with urllib.request.urlopen(req_sync_gcp) as resp:
        self.assertEqual(resp.status, 200)

      # 6. Verify POST /api/sync_ge_fleet
      req_sync_fleet = urllib.request.Request(
          f'{base_url}/api/sync_ge_fleet',
          data=json.dumps({'window_hours': 24}).encode('utf-8'),
          headers={'Content-Type': 'application/json'},
          method='POST',
      )
      with urllib.request.urlopen(req_sync_fleet) as resp:
        self.assertEqual(resp.status, 200)
        synced = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(synced['status'], 'synced')
        self.assertEqual(synced['fleet']['totals']['agents'], 3)

      # 7. Verify POST /api/select_agent switches to deep_research
      req_select = urllib.request.Request(
          f'{base_url}/api/select_agent',
          data=json.dumps({'agent_id': 'deep_research'}).encode('utf-8'),
          headers={'Content-Type': 'application/json'},
          method='POST',
      )
      with urllib.request.urlopen(req_select) as resp:
        selected = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(
            selected['active_agent']['agent_id'], 'deep_research'
        )

      # 8. Verify POST /api/add_parameter adds a user-defined parameter
      req_param = urllib.request.Request(
          f'{base_url}/api/add_parameter',
          data=json.dumps({
              'label': 'Hallucination Free Rate',
              'unit': '%',
              'direction': 'HIGHER',
              'baseline_val': 89.0,
              'target_val': 99.0,
              'weight_pct': 20,
          }).encode('utf-8'),
          headers={'Content-Type': 'application/json'},
          method='POST',
      )
      with urllib.request.urlopen(req_param) as resp:
        added = json.loads(resp.read().decode('utf-8'))
        labels = [p['label'] for p in added['active_agent']['parameters']]
        self.assertIn('Hallucination Free Rate', labels)

      # 9. Verify POST /api/inject_anomaly & POST /api/evolve_generation
      req_anom = urllib.request.Request(
          f'{base_url}/api/inject_anomaly', data=b'{}', method='POST'
      )
      with urllib.request.urlopen(req_anom) as resp:
        anom_state = json.loads(resp.read().decode('utf-8'))
        self.assertIn('CRITICAL', anom_state['active_agent']['health_status'])

      req_evolve = urllib.request.Request(
          f'{base_url}/api/evolve_generation', data=b'{}', method='POST'
      )
      with urllib.request.urlopen(req_evolve) as resp:
        evolved = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(
            evolved['active_agent']['timeline'][-1]['generation'], 13
        )
        self.assertIn('OPTIMIZED', evolved['active_agent']['health_status'])

      # 10. Verify GET /ui and GET /app return 200 with HTML
      for ui_path in ('/ui', '/app'):
        with urllib.request.urlopen(f'{base_url}{ui_path}') as ui_resp:
          self.assertEqual(ui_resp.status, 200)
          self.assertIn('VibeLift | Analytics Platform', ui_resp.read().decode('utf-8'))

      # 11. Verify GET /mcp returns 405 Method Not Allowed with Allow header
      req_mcp_get = urllib.request.Request(f'{base_url}/mcp', method='GET')
      try:
        urllib.request.urlopen(req_mcp_get)
        self.fail('Expected HTTP 405 on GET /mcp')
      except urllib.error.HTTPError as err:
        self.assertEqual(err.code, 405)
        self.assertEqual(err.headers.get('Allow'), 'POST, DELETE')

      # 12. Verify DELETE /mcp returns 204 No Content
      req_mcp_del = urllib.request.Request(f'{base_url}/mcp', method='DELETE')
      with urllib.request.urlopen(req_mcp_del) as del_resp:
        self.assertEqual(del_resp.status, 204)

      # 13. Verify POST /mcp initialize JSON-RPC 2.0 handshake
      req_init = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 1,
              'method': 'initialize',
              'params': {'protocolVersion': '2025-06-18'},
          }).encode('utf-8'),
          headers={'Content-Type': 'application/json'},
          method='POST',
      )
      with urllib.request.urlopen(req_init) as init_resp:
        self.assertEqual(init_resp.status, 200)
        session_id = init_resp.headers.get('Mcp-Session-Id')
        self.assertTrue(session_id)
        init_body = json.loads(init_resp.read().decode('utf-8'))
        self.assertEqual(init_body['id'], 1)
        self.assertEqual(init_body['result']['protocolVersion'], '2025-06-18')
        self.assertIn('tools', init_body['result']['capabilities'])
        self.assertIn('resources', init_body['result']['capabilities'])

      # 14. Verify POST /mcp tools/list returns tools with _meta.ui
      req_tools = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 2,
              'method': 'tools/list',
              'params': {},
          }).encode('utf-8'),
          headers={
              'Content-Type': 'application/json',
              'Mcp-Session-Id': session_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req_tools) as tools_resp:
        self.assertEqual(tools_resp.status, 200)
        tools_body = json.loads(tools_resp.read().decode('utf-8'))
        tool_names = [t['name'] for t in tools_body['result']['tools']]
        self.assertIn('open_dashboard', tool_names)
        self.assertIn('query_project_telemetry', tool_names)
        self.assertIn('query_ge_agent_fleet', tool_names)
        self.assertIn('calculate_prompt_cache_economics', tool_names)
        self.assertIn('run_alpha_evolve_generation', tool_names)
        self.assertIn('get_vibelift_state', tool_names)

        open_tool = next(t for t in tools_body['result']['tools'] if t['name'] == 'open_dashboard')
        self.assertIn('io.modelcontextprotocol/ui', open_tool['_meta'])
        self.assertIn('ui://vibelift-analytics/dashboard', open_tool['_meta']['io.modelcontextprotocol/ui']['resourceUri'])

      # 15. Verify POST /mcp resources/list and resources/read returns MCP App HTML
      req_res_list = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 3,
              'method': 'resources/list',
              'params': {},
          }).encode('utf-8'),
          headers={
              'Content-Type': 'application/json',
              'Mcp-Session-Id': session_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req_res_list) as res_list_resp:
        self.assertEqual(res_list_resp.status, 200)
        res_list_body = json.loads(res_list_resp.read().decode('utf-8'))
        uris = [r['uri'] for r in res_list_body['result']['resources']]
        self.assertIn('ui://vibelift-analytics/dashboard', uris)
        self.assertEqual(res_list_body['result']['resources'][0]['mimeType'], 'text/html;profile=mcp-app')

      req_res_read = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 4,
              'method': 'resources/read',
              'params': {'uri': 'ui://vibelift-analytics/dashboard'},
          }).encode('utf-8'),
          headers={
              'Content-Type': 'application/json',
              'Mcp-Session-Id': session_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req_res_read) as res_read_resp:
        self.assertEqual(res_read_resp.status, 200)
        res_read_body = json.loads(res_read_resp.read().decode('utf-8'))
        content = res_read_body['result']['contents'][0]
        self.assertEqual(content['mimeType'], 'text/html;profile=mcp-app')
        self.assertIn('VibeLift | Analytics Platform', content['text'])
        self.assertIn('AppBridge', content['text'])

      # 16. Verify POST /mcp tools/call for open_dashboard and calculate_prompt_cache_economics
      req_call_open = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 5,
              'method': 'tools/call',
              'params': {'name': 'open_dashboard', 'arguments': {'initial_agent': 'it_service_desk'}},
          }).encode('utf-8'),
          headers={
              'Content-Type': 'application/json',
              'Mcp-Session-Id': session_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req_call_open) as call_open_resp:
        self.assertEqual(call_open_resp.status, 200)
        call_open_body = json.loads(call_open_resp.read().decode('utf-8'))
        self.assertFalse(call_open_body['result']['isError'])
        self.assertIn('_meta', call_open_body['result'])
        self.assertIn('io.modelcontextprotocol/ui', call_open_body['result']['_meta'])
        opened = call_open_body['result']['structuredContent']
        self.assertEqual(opened['focus_tab'], 0)
        self.assertEqual(opened['state']['ge_fleet']['totals']['agents'], 3)
        self.assertIn('IT Service Desk', call_open_body['result']['content'][0]['text'])

      req_call_econ = urllib.request.Request(
          f'{base_url}/mcp',
          data=json.dumps({
              'jsonrpc': '2.0',
              'id': 6,
              'method': 'tools/call',
              'params': {
                  'name': 'calculate_prompt_cache_economics',
                  'arguments': {
                      'prompt_token_count': 10000,
                      'cached_content_token_count': 9000,
                      'model': 'gemini-2.5-flash',
                  },
              },
          }).encode('utf-8'),
          headers={
              'Content-Type': 'application/json',
              'Mcp-Session-Id': session_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req_call_econ) as call_econ_resp:
        self.assertEqual(call_econ_resp.status, 200)
        call_econ_body = json.loads(call_econ_resp.read().decode('utf-8'))
        self.assertEqual(call_econ_body['result']['structuredContent']['cache_hit_ratio_pct'], 90.0)
        self.assertGreater(call_econ_body['result']['structuredContent']['net_savings_usd'], 0.0)

    finally:
      httpd.shutdown()
      httpd.server_close()

  def test_mcp_server_sync_dispatch_coverage(self) -> None:
    code, headers, body_str = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-1',
        'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18'},
    })
    self.assertEqual(code, 200)
    data = json.loads(body_str)
    self.assertEqual(data['id'], 'test-1')
    self.assertEqual(data['result']['protocolVersion'], '2025-06-18')

    code2, _, body2_str = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-2',
        'method': 'tools/call',
        'params': {
            'name': 'query_ge_agent_fleet',
            'arguments': {'window_hours': 24},
        },
    })
    self.assertEqual(code2, 200)
    data2 = json.loads(body2_str)
    self.assertEqual(data2['result']['structuredContent']['totals']['agents'], 3)

    code3, _, body3_str = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-3',
        'method': 'unknown_method_xyz',
        'params': {},
    })
    self.assertEqual(code3, 200)
    data3 = json.loads(body3_str)
    self.assertIn('error', data3)
    self.assertEqual(data3['error']['code'], mcp_server.METHOD_NOT_FOUND)

    # Test ping
    code_ping, _, body_ping = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-ping',
        'method': 'ping',
        'params': {},
    })
    self.assertEqual(code_ping, 200)
    self.assertEqual(json.loads(body_ping)['result'], {})

    # Test tools/list via sync dispatcher
    code_tools, _, body_tools = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-tools',
        'method': 'tools/list',
        'params': {},
    })
    self.assertEqual(code_tools, 200)
    self.assertEqual(len(json.loads(body_tools)['result']['tools']), 6)

    # Test resources/read with invalid URI
    code_inv_uri, _, body_inv_uri = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-inv-uri',
        'method': 'resources/read',
        'params': {'uri': 'ui://invalid-path'},
    })
    self.assertEqual(code_inv_uri, 200)
    data_inv = json.loads(body_inv_uri)
    self.assertIn('error', data_inv)
    self.assertEqual(data_inv['error']['code'], mcp_server.INVALID_PARAMS)

    # Test invalid jsonrpc version
    code_bad_rpc, _, body_bad_rpc = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '1.0',
        'id': 'test-bad-rpc',
        'method': 'ping',
    })
    self.assertEqual(code_bad_rpc, 200)
    self.assertEqual(json.loads(body_bad_rpc)['error']['code'], mcp_server.INVALID_REQUEST)

    # Test tool run_alpha_evolve_generation via MCP tools/call
    code_evolve, _, body_evolve = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-evolve',
        'method': 'tools/call',
        'params': {
            'name': 'run_alpha_evolve_generation',
            'arguments': {'agent_id': 'deep_research'},
        },
    })
    self.assertEqual(code_evolve, 200)
    self.assertIn('active_agent', json.loads(body_evolve)['result']['structuredContent'])

    # Test tool get_vibelift_state via MCP tools/call
    code_state, _, body_state = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'test-state',
        'method': 'tools/call',
        'params': {
            'name': 'get_vibelift_state',
            'arguments': {},
        },
    })
    self.assertEqual(code_state, 200)
    self.assertIn('turns', json.loads(body_state)['result']['structuredContent'])


class ProductionAppTest(unittest.TestCase):
  """Exercises the Cloud Run entrypoint (ADK FastAPI app + VibeLift routes) in-process."""

  @classmethod
  def setUpClass(cls) -> None:
    from fastapi.testclient import TestClient
    from app import fast_api_app
    cls.client = TestClient(fast_api_app.app)

  def _rpc(self, method: str, params=None, rpc_id: int = 1, session: str | None = None):
    headers = {'Mcp-Session-Id': session} if session else {}
    resp = self.client.post(
        '/mcp', json={'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params or {}}, headers=headers)
    self.assertEqual(resp.status_code, 200)
    return resp

  def test_fleet_rest_routes(self) -> None:
    self.assertEqual(self.client.get('/healthz').json()['status'], 'ok')
    fleet = self.client.get('/api/ge_fleet', params={'window_hours': 'abc'}).json()
    self.assertEqual(fleet['window_hours'], 24)  # Invalid input falls back to the default window.
    self.assertEqual(sorted(a['display_name'] for a in fleet['agents']), _FLEET_AGENT_NAMES)
    synced = self.client.post('/api/sync_ge_fleet', json={'window_hours': 6}).json()
    self.assertEqual((synced['status'], synced['fleet']['window_hours']), ('synced', 6))
    self.assertEqual(self.client.get('/api/state').json()['ge_fleet']['totals']['agents'], 3)
    self.assertIn('Gemini Enterprise Agent Fleet', self.client.get('/').text)
    skills = [s['id'] for s in self.client.get('/.well-known/agent-card.json').json()['skills']]
    self.assertIn('query_ge_agent_fleet', skills)
    self.assertEqual(self.client.get('/api/agent_registry').status_code, 404)

  def test_mcp_open_dashboard_and_prefixed_fleet_tool(self) -> None:
    session = self._rpc('initialize', {'protocolVersion': '2025-06-18'}).headers.get('Mcp-Session-Id')
    self.assertTrue(session)
    tools = {t['name']: t for t in self._rpc('tools/list', rpc_id=2, session=session).json()['result']['tools']}
    self.assertEqual(sorted(tools), sorted([
        'open_dashboard', 'query_ge_agent_fleet', 'query_project_telemetry',
        'calculate_prompt_cache_economics', 'run_alpha_evolve_generation', 'get_vibelift_state']))
    self.assertTrue(tools['open_dashboard']['annotations']['readOnlyHint'])
    self.assertTrue(tools['query_ge_agent_fleet']['annotations']['readOnlyHint'])

    opened = self._rpc('tools/call', {'name': 'open_dashboard', 'arguments': {}}, rpc_id=3, session=session).json()['result']
    self.assertFalse(opened['isError'])
    self.assertEqual(opened['structuredContent']['focus_tab'], 0)
    self.assertEqual(opened['structuredContent']['state']['ge_fleet']['totals']['agents'], 3)
    self.assertIn('IT Service Desk', opened['content'][0]['text'])
    self.assertEqual(opened['_meta']['ui']['resourceUri'], 'ui://vibelift-analytics/dashboard')

    # Gemini Enterprise prefixes custom MCP tool names; the dashboard's live refresh uses this tool.
    fleet = self._rpc('tools/call', {
        'name': 'custom_mcp_1_agent__query_ge_agent_fleet',
        'arguments': {'window_hours': 168, 'force_refresh': True},
    }, rpc_id=4, session=session).json()['result']
    self.assertFalse(fleet['isError'])
    self.assertEqual(fleet['structuredContent']['window_hours'], 168)
    self.assertIn('Deep Research', fleet['content'][0]['text'])

  def test_agent_card_url_follows_the_request_host(self) -> None:
    with mock.patch.dict(os.environ):
      os.environ.pop('VIBELIFT_PUBLIC_URL', None)
      os.environ.pop('PUBLIC_A2A_URL', None)
      card = self.client.get(
          '/a2a/app/.well-known/agent-card.json', headers={'X-Forwarded-Proto': 'https'}).json()
    self.assertEqual(card['url'], 'https://testserver/a2a/app')


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _read_repo_file(name: str) -> str:
  with open(os.path.join(_REPO_ROOT, name), encoding='utf-8') as f:
    return f.read()


def _pins(text: str) -> dict[str, str]:
  """Parses 'name[extra]==version' requirement lines into {normalized name: version}."""
  pins = {}
  for line in text.splitlines():
    line = line.split('#', 1)[0].strip()
    if '==' not in line:
      continue
    name, version = line.split('==', 1)
    pins[re.sub(r'\[.*\]', '', name).strip().lower().replace('_', '-')] = version.strip()
  return pins


class DeploymentHardeningTest(unittest.TestCase):
  """Regression guards for deployment and configuration blind spots."""

  def test_agent_card_url_comes_from_config_or_request(self) -> None:
    with mock.patch.dict(os.environ):
      os.environ.pop('VIBELIFT_PUBLIC_URL', None)
      os.environ.pop('PUBLIC_A2A_URL', None)
      self.assertEqual(
          server.build_agent_card('https://svc-123.us-central1.run.app')['url'],
          'https://svc-123.us-central1.run.app/a2a/app')
      os.environ['VIBELIFT_PUBLIC_URL'] = 'https://dash.example.com/'
      self.assertEqual(
          server.build_agent_card('https://ignored.example')['url'], 'https://dash.example.com/a2a/app')
      os.environ['PUBLIC_A2A_URL'] = 'https://a2a.example.com/custom'
      self.assertEqual(server.build_agent_card()['url'], 'https://a2a.example.com/custom')

  def test_base_url_from_headers_rejects_malformed_hosts(self) -> None:
    derive = server.base_url_from_headers
    self.assertEqual(derive({'host': 'svc.run.app', 'x-forwarded-proto': 'https'}), 'https://svc.run.app')
    self.assertEqual(derive({'host': 'svc.run.app', 'x-forwarded-proto': 'https, http'}), 'https://svc.run.app')
    self.assertEqual(derive({'host': 'localhost:8080'}, default_scheme='http'), 'http://localhost:8080')
    self.assertEqual(derive({'host': 'svc.run.app', 'x-forwarded-proto': 'javascript'}), 'https://svc.run.app')
    for bad_host in ('evil.example/path', 'user@evil.example', 'a b', 'back\\slash', ''):
      self.assertEqual(derive({'host': bad_host}), '', bad_host)
    self.assertEqual(derive(None), '')

  def test_open_dashboard_does_not_publish_a_hardcoded_url(self) -> None:
    with mock.patch.dict(os.environ):
      os.environ.pop('VIBELIFT_PUBLIC_URL', None)
      res = json.loads(adk_agent_module.open_dashboard())
      self.assertIsNone(res['dashboard_url'])
      self.assertIn('ui://vibelift-analytics/dashboard', res['message'])
      self.assertNotIn('run.app', json.dumps(res))
      os.environ['VIBELIFT_PUBLIC_URL'] = 'https://dash.example.com/'
      res = json.loads(adk_agent_module.open_dashboard(focus_tab=2))
    self.assertEqual(res['dashboard_url'], 'https://dash.example.com')
    self.assertIn('https://dash.example.com', res['message'])
    self.assertEqual(res['focus_tab'], 2)

  def test_model_backend_defaults_to_vertex_without_api_key(self) -> None:
    env = {}
    adk_agent_module.configure_model_backend(env)
    self.assertEqual(env, {'GOOGLE_GENAI_USE_VERTEXAI': 'TRUE', 'GOOGLE_CLOUD_LOCATION': 'us-central1'})
    env = {'GOOGLE_CLOUD_REGION': 'europe-west4'}
    adk_agent_module.configure_model_backend(env)
    self.assertEqual(env['GOOGLE_CLOUD_LOCATION'], 'europe-west4')
    env = {'GOOGLE_GENAI_USE_VERTEXAI': 'FALSE', 'GOOGLE_CLOUD_LOCATION': 'global'}
    adk_agent_module.configure_model_backend(env)
    self.assertEqual(env, {'GOOGLE_GENAI_USE_VERTEXAI': 'FALSE', 'GOOGLE_CLOUD_LOCATION': 'global'})
    for key_var in ('GOOGLE_API_KEY', 'GEMINI_API_KEY'):
      env = {key_var: 'test-key'}
      adk_agent_module.configure_model_backend(env)
      self.assertEqual(env, {key_var: 'test-key'})

  def test_mcp_protocol_version_override_must_be_supported(self) -> None:
    resolve = mcp_server.resolve_default_protocol_version
    self.assertEqual(resolve(None), '2025-06-18')
    self.assertEqual(resolve(''), '2025-06-18')
    self.assertEqual(resolve(' 2025-03-26 '), '2025-03-26')
    self.assertEqual(resolve('2099-01-01'), '2025-06-18')

  def test_enable_mcp_app_flag_gates_the_endpoint(self) -> None:
    from fastapi import FastAPI
    cases = (('1', True), ('', True), ('true', True), ('0', False), ('false', False), ('OFF', False), ('no', False))
    for value, expected in cases:
      with mock.patch.dict(os.environ, {'ENABLE_MCP_APP': value}):
        self.assertIs(mcp_server.mcp_app_enabled(), expected, value)
    with mock.patch.dict(os.environ):
      os.environ.pop('ENABLE_MCP_APP', None)
      self.assertTrue(mcp_server.mcp_app_enabled())

    disabled_app, enabled_app = FastAPI(), FastAPI()
    with mock.patch.dict(os.environ, {'ENABLE_MCP_APP': '0'}):
      mcp_server.register_mcp_routes(disabled_app)
    with mock.patch.dict(os.environ, {'ENABLE_MCP_APP': '1'}):
      mcp_server.register_mcp_routes(enabled_app)
    self.assertNotIn('/mcp', [getattr(r, 'path', '') for r in disabled_app.routes])
    self.assertIn('/mcp', [getattr(r, 'path', '') for r in enabled_app.routes])

  def test_mcp_errors_do_not_leak_exception_details(self) -> None:
    secret = 'projects/secret-project-42/internal-detail'

    async def failing_handler(session_key, args):
      del session_key, args
      raise RuntimeError(secret)

    tool = mcp_server._TOOLS_BY_NAME['get_vibelift_state']
    with self.assertLogs('vibelift-mcp', level='ERROR') as logs, mock.patch.dict(tool, {'handler': failing_handler}):
      code, _, body = mcp_server.handle_jsonrpc_sync({
          'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
          'params': {'name': 'get_vibelift_state', 'arguments': {}},
      })
    self.assertEqual(code, 200)
    result = json.loads(body)['result']
    self.assertTrue(result['isError'])
    text = result['content'][0]['text']
    self.assertNotIn('secret-project-42', text)
    self.assertIn('RuntimeError', text)
    ref = re.search(r'ref ([0-9a-f]{12})', text).group(1)
    server_log = '\n'.join(logs.output)
    self.assertIn(ref, server_log)  # The full detail stays in the server log under the same reference.
    self.assertIn(secret, server_log)

    with self.assertLogs('vibelift-mcp', level='ERROR'), \
        mock.patch.object(mcp_server, '_dispatch', side_effect=ValueError(secret)):
      _, _, body = mcp_server.handle_jsonrpc_sync({'jsonrpc': '2.0', 'id': 8, 'method': 'tools/list'})
    error = json.loads(body)['error']
    self.assertEqual(error['code'], mcp_server.INTERNAL_ERROR)
    self.assertNotIn('secret-project-42', error['message'])
    self.assertRegex(error['message'], r'ref [0-9a-f]{12}')

  def test_unconfigured_project_never_resolves_to_a_real_project(self) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ('GOOGLE_CLOUD_PROJECT', 'GCP_PROJECT')}
    with mock.patch.dict(os.environ, env, clear=True), \
        mock.patch.object(gcp_telemetry.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')), \
        mock.patch.object(gcp_telemetry.subprocess, 'run', side_effect=FileNotFoundError('gcloud')), \
        self.assertLogs('gcp_telemetry', level='WARNING'):
      project = gcp_telemetry.get_current_gcp_project()
    self.assertEqual(project, gcp_telemetry.UNCONFIGURED_PROJECT_ID)
    # Project IDs are 6-30 lowercase letters, digits and hyphens, so this can never be a real project.
    self.assertIsNone(re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', project))

  def test_dependency_pins_are_consistent(self) -> None:
    requirements = _pins(_read_repo_file('requirements.txt'))
    constraints = _pins(_read_repo_file('constraints.txt'))
    self.assertIn('google-adk', requirements)
    for name, version in requirements.items():
      self.assertEqual(constraints.get(name), version, f'{name}: requirements.txt vs constraints.txt')
    pyproject = tomllib.loads(_read_repo_file('pyproject.toml'))
    self.assertEqual(_pins('\n'.join(pyproject['project']['dependencies'])), requirements)
    self.assertIn('-c constraints.txt', _read_repo_file('Dockerfile'))

  def test_deploy_paths_stay_private_and_merge_env_vars(self) -> None:
    for name in ('deploy_cloud_run.sh', 'cloudbuild.yaml'):
      text = _read_repo_file(name)
      self.assertIn('--no-allow-unauthenticated', text, name)
      self.assertNotIn('--allow-unauthenticated', text.replace('--no-allow-unauthenticated', ''), name)
      self.assertNotIn('allUsers', text, name)
      # --set-env-vars would wipe variables configured on the service outside the script.
      self.assertNotIn('--set-env-vars', text, name)
      self.assertIn('--update-env-vars', text, name)
      self.assertIn('VIBELIFT_PUBLIC_URL=', text, name)
      self.assertIn('GOOGLE_GENAI_USE_VERTEXAI=TRUE', text, name)
      self.assertIn('--min-instances=1', text, name)
    self.assertNotIn(':latest', _read_repo_file('cloudbuild.yaml'))
    self.assertIn('gcp-sa-discoveryengine', _read_repo_file('deploy_cloud_run.sh'))

  def test_real_ge_agents_and_sub_second_mcp_open_dashboard(self) -> None:
    import time
    for fname in (
        'alpha_evolve_optimizer.py',
        'ui_template.py',
        'server.py',
        'mcp_server.py',
        'app/agent.py',
        'vibelift_mcp_spec_clean.json',
        'README.md',
    ):
      content = _read_repo_file(fname)
      for fake_id in ('mortgage_assistant', 'forecast_engine', 'stock_market_updates'):
        self.assertNotIn(fake_id, content, f'Fake agent {fake_id} still present in {fname}')

    t0 = time.monotonic()
    code, _, body = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'fast-open',
        'method': 'tools/call',
        'params': {'name': 'open_dashboard', 'arguments': {}},
    })
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    self.assertEqual(code, 200)
    self.assertLess(elapsed_ms, 500.0, f'open_dashboard took {elapsed_ms:.1f}ms; must stay <500ms for GE connector')
    payload = json.loads(body)['result']['structuredContent']
    agent_ids = [a['agent_id'] for a in payload['state']['available_agents']]
    self.assertEqual(agent_ids, ['it_service_desk', 'vibelift_analytics', 'deep_research'])
    self.assertIn('it_service_desk', payload['state']['all_agents'])
    self.assertIn('vibelift_analytics', payload['state']['all_agents'])
    self.assertIn('deep_research', payload['state']['all_agents'])


if __name__ == '__main__':
  unittest.main()
