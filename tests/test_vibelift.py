"""Comprehensive unit and HTTP UI integration tests for VibeLift Analytics Platform."""

import json
import os
import re
import threading
import tomllib
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app import agent as adk_agent_module
from tests import test_fleet as ge_fleet_test
from vibelift import fleet as ge_fleet
from vibelift import gcp_telemetry, long_running_agent, mcp_server, server, telemetry
from vibelift import optimizer as alpha_evolve_optimizer

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
    self.assertTrue(evolve_res['simulator'])
    self.assertIn('simulated_generation', evolve_res)
    self.assertEqual(evolve_res['latest_action']['status'], 'SIMULATED (nothing deployed)')

    # validate_telemetry_grounding must run the real validator (deterministic, no LLM judge by default)
    with mock.patch.object(
        server._global_controller, 'validate_telemetry', wraps=server._global_controller.validate_telemetry
    ) as spy:
      grounding_res = json.loads(adk_agent_module.validate_telemetry_grounding())
    spy.assert_called_once_with(run_llm_judge=False)
    self.assertIn('overall_status', grounding_res)
    self.assertIsInstance(grounding_res['checks'], list)
    self.assertGreater(grounding_res['total_checks'], 0)
    server._global_controller.reset()

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
        self.assertEqual(opened['focus_tab'], 6)
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
    code, _headers, body_str = mcp_server.handle_jsonrpc_sync({
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
    self.assertEqual(len(json.loads(body_tools)['result']['tools']), len(mcp_server._TOOLS))
    self.assertEqual(len(mcp_server._TOOLS), 9)

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
    self.assertEqual(self.client.get('/api/health').json()['status'], 'ok')
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
        'calculate_prompt_cache_economics', 'run_alpha_evolve_generation', 'get_vibelift_state',
        'xray_prompt_cache', 'list_prompt_snapshot_turns', 'set_agent_trace_logging']))
    self.assertTrue(tools['open_dashboard']['annotations']['readOnlyHint'])
    self.assertTrue(tools['query_ge_agent_fleet']['annotations']['readOnlyHint'])
    # Changes live agent settings, so it must never claim to be read-only.
    self.assertFalse(tools['set_agent_trace_logging']['annotations']['readOnlyHint'])

    opened = self._rpc('tools/call', {'name': 'open_dashboard', 'arguments': {}}, rpc_id=3, session=session).json()['result']
    self.assertFalse(opened['isError'])
    self.assertEqual(opened['structuredContent']['focus_tab'], 6)
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


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _element_ancestry(html: str) -> dict[str, tuple[set[str], set[str]]]:
  """Maps every element id to (ids of it and its ancestors, classes of it and its ancestors)."""
  from html.parser import HTMLParser

  void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}

  class _Walker(HTMLParser):

    def __init__(self) -> None:
      super().__init__()
      self.stack: list[tuple[str, str | None, list[str]]] = []
      self.found: dict[str, tuple[set[str], set[str]]] = {}

    def handle_starttag(self, tag, attrs):
      a = dict(attrs)
      entry = (tag, a.get('id'), (a.get('class') or '').split())
      chain = [*self.stack, entry]
      if entry[1]:
        self.found[entry[1]] = ({i for _, i, _ in chain if i}, {c for _, _, cs in chain for c in cs})
      if tag not in void:
        self.stack.append(entry)

    def handle_endtag(self, tag):
      for i in range(len(self.stack) - 1, -1, -1):
        if self.stack[i][0] == tag:
          del self.stack[i:]
          break

  walker = _Walker()
  walker.feed(html.split('<script', 1)[0])  # markup only; the inline script builds no static ids
  return walker.found


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
        self.assertLogs('vibelift.gcp_telemetry', level='WARNING'):
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
    for name in ('deploy/deploy_cloud_run.sh', 'deploy/cloudbuild.yaml'):
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
    self.assertNotIn(':latest', _read_repo_file('deploy/cloudbuild.yaml'))
    self.assertIn('gcp-sa-discoveryengine', _read_repo_file('deploy/deploy_cloud_run.sh'))

  def test_ge_datastore_deployment_and_app_registration(self) -> None:
    import deploy.setup_mcp_connector as smc

    reg_script = _read_repo_file('deploy/register_ge_agent.sh')
    self.assertIn('python3 deploy/setup_mcp_connector.py --location="${GE_LOCATION}" "${GE_ENGINE_ID}"', reg_script)

    calls: list[tuple[str, str, object]] = []
    engine_stores = ['existing_store']

    class FakeConnectorApi:
      base = 'https://discoveryengine.googleapis.com/v1alpha/projects/123/locations/global'
      root = 'https://discoveryengine.googleapis.com/v1alpha'
      dry_run = False

      def call(self, method: str, url: str, body: object = None, mutate: bool = False):
        del mutate
        calls.append((method, url, body))
        if method == 'POST' and ':setUpDataConnectorV2' in url:
          return 200, {'state': 'ACTIVE'}
        if method == 'GET' and url.endswith('/engines/my-ge-app'):
          return 200, {'name': 'engines/my-ge-app', 'dataStoreIds': list(engine_stores)}
        if method == 'PATCH' and '/engines/my-ge-app?updateMask=dataStoreIds' in url:
          engine_stores[:] = list((body or {}).get('dataStoreIds') or [])
          return 200, {'name': 'engines/my-ge-app', 'dataStoreIds': list(engine_stores)}
        return 200, {}

    fake_api = FakeConnectorApi()
    smc.create_connector(fake_api, 'vibelift-analytics-mcp', 'VibeLift Analytics', 'https://svc.run.app/mcp', ['open_dashboard'])
    create_body = calls[0][2]
    self.assertEqual(create_body['dataSource'], 'custom_mcp')
    self.assertEqual(create_body['actionConfig']['actionParams']['mcp_server_source'], 'BYO_MCP')
    self.assertFalse(create_body['actionConfig']['actionParams']['use_agent_gateway_egress'])

    smc.attach_to_app(fake_api, 'my-ge-app', 'vibelift-analytics-mcp_mcp_data')
    self.assertEqual(engine_stores, ['existing_store', 'vibelift-analytics-mcp_mcp_data'])


  def test_real_ge_agents_and_sub_second_mcp_open_dashboard(self) -> None:
    import time
    for fname in (
        'vibelift/optimizer.py',
        'vibelift/ui/template.py',
        'vibelift/server.py',
        'vibelift/mcp_server.py',
        'app/agent.py',
        'docs/mcp_spec.json',
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

  def test_dashboard_javascript_syntax_is_valid_and_pip_configured(self) -> None:
    import shutil
    import subprocess
    import tempfile

    from vibelift.ui import template as ui_template

    state = server._global_controller.get_state_payload(fast_mcp=True)
    html = ui_template.render_dashboard_html(initial_state=state)
    self.assertIn('btnModeFullscreen', html)
    self.assertIn("availableDisplayModes: ['pip', 'fullscreen', 'inline']", html)
    self.assertIn("setDisplayMode('pip')", html)
    self.assertIn('tabPanel3', html)

    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, flags=re.DOTALL)
    self.assertGreaterEqual(len(scripts), 1)
    node_bin = shutil.which('node')
    if node_bin:
      for idx, script_body in enumerate(scripts):
        with tempfile.NamedTemporaryFile('w', suffix=f'_{idx}.js', delete=False, encoding='utf-8') as f:
          f.write(script_body)
          tmp_path = f.name
        try:
          proc = subprocess.run([node_bin, '--check', tmp_path], capture_output=True, text=True, check=False)
          self.assertEqual(proc.returncode, 0, f'SyntaxError in <script> #{idx}: {proc.stderr}')
        finally:
          os.unlink(tmp_path)

    # Verify MCP resource metadata and open_dashboard _meta declare right-panel pip mode
    ui_meta = mcp_server.RESOURCE_META['ui']
    self.assertEqual(ui_meta['preferredMode'], 'pip')
    self.assertEqual(ui_meta['displayMode'], 'pip')
    self.assertEqual(ui_meta['availableDisplayModes'], ['pip', 'fullscreen', 'inline'])

  def test_weekly_sync_enhancements_decorator_optimizer_and_user_centric_finops(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    @telemetry.vibelift_telemetry(
        agent_name='it_service_desk',
        protocol='MCP',
        model='gemini-2.5-flash',
        skill_or_mcp='mcp.servicenow_iam',
    )
    def sample_decorated_tool(ticket_id: str) -> dict:
      return {'ticket_id': ticket_id, 'resolved': True, 'cached': True}

    res = sample_decorated_tool('INC-99412')
    self.assertTrue(res['resolved'])
    events = telemetry.get_recent_decorator_events()
    self.assertGreaterEqual(len(events), 1)
    self.assertEqual(events[0]['skill_or_mcp'], 'mcp.servicenow_iam')

    client = TestClient(fast_api_app.app)
    try:
      state = client.get('/api/state').json()
      self.assertIn('optimizer_platforms', state)
      self.assertIn('user_centric', state)
      self.assertIn('decorator_events', state)
      self.assertEqual(state['user_centric']['supported_dau_capacity'], '4,000 – 10,000 DAU')
      self.assertEqual(state['user_centric']['baseline_cost_per_1k_turns_usd'], 29.40)
      self.assertEqual(state['user_centric']['optimized_cost_per_1k_turns_usd'], 3.45)

      # Verify context_bloat_pct and idle_ratio_pct parameters exist on active agent
      param_keys = [p['key'] for p in state['active_agent']['parameters']]
      self.assertIn('context_bloat_pct', param_keys)
      self.assertIn('idle_ratio_pct', param_keys)

      # Test switching optimization platform via /api/select_optimizer
      opt_resp = client.post('/api/select_optimizer', json={'platform_id': 'hybrid_ensemble'})
      self.assertEqual(opt_resp.status_code, 200)
      opt_state = opt_resp.json()
      self.assertEqual(opt_state['optimizer_platforms']['active_platform_id'], 'hybrid_ensemble')

      # Test ingesting a real-time decorator event via /api/decorator_ingest
      ing_resp = client.post('/api/decorator_ingest', json={
          'agent_name': 'deep_research',
          'protocol': 'A2A',
          'handler_name': 'synthesize_sec_10k_corpus',
          'model': 'gemini-2.5-pro',
          'prompt_tokens': 24800,
          'cached_tokens': 23100,
          'output_tokens': 640,
          'latency_ms': 410.5,
          'idle_ratio_pct': 6.8,
          'context_bloat_pct': 6.4,
          'skill_or_mcp': 'mcp.vertex_ai_search',
      })
      self.assertEqual(ing_resp.status_code, 200)
      ing_state = ing_resp.json()
      self.assertEqual(ing_state['decorator_events'][0]['handler_name'], 'synthesize_sec_10k_corpus')
    finally:
      server._global_controller.reset()

  def test_sme_one_pane_control_plane_endpoints_and_ui_panels(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app
    from vibelift.ui import template as ui_template

    client = TestClient(fast_api_app.app)
    try:
      state = client.get('/api/state').json()
      self.assertIn('billing_reconciliation', state)
      self.assertIn('what_if_default', state)
      self.assertIn('otel_catalog', state)
      self.assertIn('aive_logs', state)
      self.assertIn('nl2sql_default', state)

      br = state['billing_reconciliation']
      self.assertEqual(br['unit_economics']['north_star_metric'], 'Cost per CSAT-Positive Resolved Session')
      self.assertEqual(br['unit_economics']['unit_cost_reduction_pct'], 95.2)
      self.assertGreaterEqual(len(br['sku_ledger']), 5)
      self.assertIn('gsu_advisor', br)

      # Test /api/nl2sql endpoint
      nl_resp = client.post('/api/nl2sql', json={'question': 'Show token category breakdown for thinking vs context bloat'})
      self.assertEqual(nl_resp.status_code, 200)
      nl_data = nl_resp.json()
      self.assertIn('SELECT', nl_data['generated_sql'])
      self.assertGreaterEqual(len(nl_data['rows']), 1)

      # Test /api/what_if_simulate endpoint
      sim_resp = client.post('/api/what_if_simulate', json={
          'model_tier': 'gemini-2.5-flash',
          'thinking_budget_tok': 512,
          'history_window_turns': 4,
          'traffic_canary_pct': 25,
      })
      self.assertEqual(sim_resp.status_code, 200)
      sim_data = sim_resp.json()
      self.assertEqual(sim_data['traffic_canary_pct'], 25)
      self.assertIn('gcloud run services update-traffic', sim_data['canary_rollout_command'])
      self.assertIn('thinking_budget_tokens: 512', sim_data['gitops_diff'])

      # Test /api/csat_rating endpoint
      csat_resp = client.post('/api/csat_rating', json={
          'user_email': 'sme_reviewer@example.com',
          'session_id': '6446120131357637190',
          'rating': 5,
          'feedback_text': 'SME one-pane control plane verified.',
      })
      self.assertEqual(csat_resp.status_code, 200)
      csat_state = csat_resp.json()
      self.assertEqual(csat_state['aive_logs']['ratings_logs'][0]['user_ldap'], 'sme_reviewer')
      self.assertEqual(csat_state['aive_logs']['ratings_logs'][0]['rating'], 5)

      # Test /api/aive_log endpoint
      aive_resp = client.post('/api/aive_log', json={
          'user_email': 'sme_reviewer@example.com',
          'task_type': 'CANARY_TRAFFIC_SPLIT_AUDIT',
          'total_tokens': 16200,
          'latency_ms': 495.0,
      })
      self.assertEqual(aive_resp.status_code, 200)
      aive_state = aive_resp.json()
      self.assertEqual(aive_state['aive_logs']['usage_logs'][0]['task_type'], 'CANARY_TRAFFIC_SPLIT_AUDIT')

      # Verify UI HTML includes all SME One-Pane-of-Glass panels
      html = ui_template.render_dashboard_html(initial_state=state)
      for panel_id in (
          'smeExecutivePulseBar',
          'nl2sqlCopilotDrawer',
          'watchOutAlarmsContainer',
          'otelCatalogTableBody',
          'whatIfSimulatorPanel',
          'cacheForensicsBody',
          'tokenCategoryBody',
          'runawayAlertsBody',
          'billingSkuBody',
          'gsuAdvisorBox',
          'powerUsersBody',
          'vocRatingsBody',
          'aiveUsageBody',
          'tokenomicsCpoDriftPanel',
          'consumptionAndCachingPanel',
          'apigeeAndExtensionsPanel',
          'cockpitFinopsAndTcoPanel',
          'cpoDriftKpis',
          'cpoByAgentBody',
          'driftDriversBody',
          'attributionJoinBody',
          'meteringCategoriesBody',
          'routingLanesBody',
          'modelPortfolioBody',
          'cacheModelBreakEvenBody',
          'apigeePoliciesBody',
          'extensionOverheadBody',
          'cockpitFindingsBody',
          'cockpitWaterfallBody',
          'costGuardBox',
          'cfoTcoKpiBox',
          'maturityProgressionBody',
          'pnlAllocationBody',
          'opexTradeoffBody',
      ):
        self.assertIn(panel_id, html, f'Missing SME Control Plane element #{panel_id} in rendered HTML')
    finally:
      server._global_controller.reset()

  def test_tokenomics_2026_and_cockpit_finops_ledger_and_recompute(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    client = TestClient(fast_api_app.app)
    try:
      resp = client.get('/api/tokenomics_cockpit')
      self.assertEqual(resp.status_code, 200)
      tc = resp.json()

      # 1. Verify True Cost per Outcome (CpO) Ledger
      cpo = tc['cpo']
      self.assertIn('CpO =', cpo['formula'])
      self.assertEqual(len(cpo['agents']), 3)
      self.assertGreater(cpo['fleet_baseline_cpo_usd'], cpo['fleet_optimized_cpo_usd'])
      self.assertGreater(cpo['fleet_cpo_reduction_pct'], 75.0)
      self.assertIn('78.4%', cpo['pareto_outlier_rule'])

      # 2. Verify Token-to-Spend Drift & Zero Unattributed Variance
      drift = tc['drift']
      self.assertEqual(len(drift['drift_drivers']), 5)
      self.assertEqual(
          [d['driver_id'] for d in drift['drift_drivers']],
          ['D1', 'D2', 'D3', 'D4', 'D5'],
      )
      self.assertEqual(drift['unattributed_usd'], 0.0)
      self.assertAlmostEqual(
          drift['expected_naive_token_spend_usd']
          + drift['remediated_drift_total_usd']
          + drift['unattributed_usd'],
          drift['actual_reconciled_invoice_usd'],
          places=2,
      )

      # 3. Verify 3-Way User-Level Attribution Join
      attr = tc['attribution_join']
      self.assertEqual(len(attr['sources']), 3)
      self.assertIn('JOIN', attr['join_sql'])
      self.assertIn('gcp_billing_export_resource_v1', attr['join_sql'])

      # 4. Verify 4 Metering Categories, 6 Routing Lanes (with † Pre-GA markers) & 4-Tier Portfolio
      mc = tc['metering_and_consumption']
      self.assertEqual(len(mc['categories']), 4)
      self.assertEqual(len(mc['routing_lanes']), 6)
      pre_ga_lanes = [lane['lane_name'] for lane in mc['routing_lanes'] if '†' in lane['lane_name']]
      self.assertGreaterEqual(len(pre_ga_lanes), 2)
      self.assertEqual(len(mc['model_portfolio_tiers']), 4)

      # 5. Verify Explicit vs. Implicit Context Cache Storage Break-Even Formula
      caching = tc['caching']
      self.assertAlmostEqual(caching['flash_break_even_calls_per_hr'], 4.7, places=1)
      self.assertAlmostEqual(caching['pro_break_even_calls_per_hr'], 5.0, places=1)
      self.assertEqual(len(caching['modes_table']), 3)

      # 6. Verify Apigee AI Gateway (Deck 1 Slide #30 g3ee7e8b2bb8_1_3597) & Extension Overhead
      ge = tc['gateway_and_extensions']
      self.assertEqual(len(ge['apigee_policies']), 7)
      self.assertEqual(len(ge['extension_overhead']), 5)
      kc_patterns = [p for p in ge['extension_overhead'] if 'Knowledge Catalog' in p['extension_type']]
      self.assertEqual(len(kc_patterns), 1)
      self.assertIn('7x token reduction', kc_patterns[0]['finops_recommendation'])

      # 7. Verify CFO Enterprise AI TCO (30/70 Split), Maturity (CRAWL->WALK->RUN) & P&L Allocation
      tco_pnl = tc['tco_and_pnl']
      self.assertEqual(tco_pnl['visible_tech_share_pct'], 30.0)
      self.assertEqual(tco_pnl['hidden_enterprise_share_pct'], 70.0)
      self.assertEqual(len(tco_pnl['maturity_progression']), 3)
      self.assertEqual(len(tco_pnl['pnl_accounting']), 3)

      # 8. Verify AgentOps Cockpit FinOps Findings (FIN-01..FIN-05), Multiplicative Waterfall & @cost_guard
      cf = tc['cockpit_finops']
      self.assertEqual(
          [f['rule_id'] for f in cf['auditor_findings']],
          ['FIN-01', 'FIN-02', 'FIN-03', 'FIN-04', 'FIN-05'],
      )
      wf = cf['waterfall']
      self.assertEqual(wf['baseline_monthly_usd'], 142000.0)
      self.assertLess(wf['optimized_monthly_usd'], 15000.0)
      self.assertGreater(wf['total_reduction_pct'], 90.0)
      self.assertEqual(len(wf['steps']), 5)
      self.assertIn('@cost_guard', cf['cost_guard']['decorator_snippet'])
      self.assertEqual(len(cf['cost_guard']['opex_tradeoff_matrix']), 5)

      # 9. Verify POST /api/recompute_finops dynamically updates Caching & Routing Lanes
      recomp_resp = client.post('/api/recompute_finops', json={
          'calls_per_hr': 36.0,
          'prefix_tokens': 32000,
          'deferred_share_pct': 50.0,
          'fsp_commit_years': 3,
      })
      self.assertEqual(recomp_resp.status_code, 200)
      recomp_body = recomp_resp.json()
      recomp_data = recomp_body.get('tokenomics_cockpit') or recomp_body.get('result') or recomp_body
      self.assertEqual(recomp_data['assumptions']['calls_per_hr'], 36.0)
      self.assertGreater(
          recomp_data['caching']['hourly_cost_comparison']['monthly_prefix_savings_usd'],
          caching['hourly_cost_comparison']['monthly_prefix_savings_usd'],
      )
    finally:
      server._global_controller.reset()

  def test_ast_finops_scanner_detects_fin_01_through_fin_05(self) -> None:
    from vibelift import optimizer as alpha_evolve_optimizer

    long_prompt = 'STATIC_ENTERPRISE_SYSTEM_PROMPT_' * 60  # > 1500 chars without ContextCacheConfig -> FIN-02
    sample_unoptimized_code = f'''
from tenacity import retry, wait_fixed

SYSTEM_PROMPT = "{long_prompt}"

@retry(wait=wait_fixed(1))
def handle_request(client, filings, retriever):
    # FIN-05: top_k > 20
    chunks = retriever.retrieve(query="sec", top_k=50)
    # FIN-03: Pro model with response_schema
    cls_resp = client.models.generate_content(model="gemini-2.5-pro", contents="classify", response_schema= dict)
    # FIN-01: generate_content inside loop
    for doc in filings:
        resp = client.models.generate_content(model="gemini-2.5-pro", contents=doc)
    return resp
'''
    findings = alpha_evolve_optimizer.scan_python_code_for_finops_findings(
        sample_unoptimized_code, filename='unoptimized_agent.py'
    )
    detected_codes = {f['rule_id'] for f in findings}
    for expected_code in ('FIN-01', 'FIN-02', 'FIN-03', 'FIN-04', 'FIN-05'):
      self.assertIn(
          expected_code,
          detected_codes,
          f'AST scanner failed to detect {expected_code} in sample unoptimized agent code',
      )

  def test_zero_ai_fluff_buzzwords_across_ui_and_optimizer(self) -> None:
    import pathlib
    import re

    banned_buzzwords = (
        'synergy',
        'magic',
        'self-healing',
        'self-healed',
        'auto-healed',
        'autonomous',
        'hyper-scale',
        'seamless',
        'revolutionary',
        'genome',
    )
    pattern = re.compile(r'\b(' + '|'.join(re.escape(w) for w in banned_buzzwords) + r')\b', re.IGNORECASE)
    repo_dir = pathlib.Path(__file__).resolve().parent.parent
    for fname in ('vibelift/ui/template.py', 'vibelift/optimizer.py'):
      text = (repo_dir / fname).read_text(encoding='utf-8')
      matches = pattern.findall(text)
      self.assertEqual(
          matches,
          [],
          f'Found banned AI fluff buzzwords {matches} in {fname}',
      )

  def test_all_js_dom_ids_exist_in_rendered_html_and_all_state_keys_surfaced(self) -> None:
    from vibelift.ui import template as ui_template

    state = server._global_controller.get_state_payload(fast_mcp=True)
    html = ui_template.render_dashboard_html(initial_state=state)
    html_Without_scripts = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html_ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', html_Without_scripts))

    scripts = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', html, flags=re.DOTALL))
    js_referenced_ids = set(re.findall(r'getElementById\(\s*[\'"]([^\'"]+)[\'"]\s*\)', scripts))

    missing_in_html = sorted(js_referenced_ids - html_ids)
    self.assertEqual(
        missing_in_html,
        [],
        f'JS getElementById() references DOM IDs missing from HTML markup: {missing_in_html}',
    )

    # Verify previously unrendered /api/state keys and interactive controls have dedicated DOM containers
    for required_dom_id in (
        'smePersonaLensBar',
        'smePersonaPlaybookCard',
        'workflowStepsRibbon',
        'turnTrajectorySummaryKpis',
        'stepTurnBtn',
        'cloudRunServicesBody',
        'geSupportEventsBody',
    ):
      self.assertIn(
          required_dom_id,
          html_ids,
          f'Missing required SME Control Plane DOM container id="{required_dom_id}" in HTML',
      )

  def test_recompute_finops_dynamic_hitl_monthly_tasks_and_provenance_reconciliation(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    client = TestClient(fast_api_app.app)
    try:
      base_tc = client.get('/api/tokenomics_cockpit').json()
      self.assertEqual(base_tc['cpo']['hitl_escalation_unit_cost_usd'], 12.5)
      self.assertIn('savings_reconciliation_bridge', base_tc)
      self.assertIn('billing_data_freshness_ts', base_tc['drift'])
      self.assertIn('controller_signoff_note', base_tc['tco_and_pnl'])
      self.assertIn('provenance_legend', base_tc['tco_and_pnl'])

      # Recompute with custom monthly_tasks, hitl_review_minutes=12, hitl_hourly_rate_usd=90 ($18.00/escalation)
      resp = client.post('/api/recompute_finops', json={
          'monthly_tasks': 259000,
          'hitl_review_minutes': 12.0,
          'hitl_hourly_rate_usd': 90.0,
          'cache_calls_per_hour': 42.0,
          'deferred_offpeak_share_pct': 55.0,
      })
      self.assertEqual(resp.status_code, 200)
      body = resp.json()
      tc = body.get('tokenomics_cockpit') or body.get('result') or body
      self.assertAlmostEqual(tc['cpo']['hitl_escalation_unit_cost_usd'], 18.0, places=2)
      self.assertEqual(tc['assumptions']['calls_per_hr'], 42.0)
      self.assertEqual(tc['assumptions']['deferred_share_pct'], 55.0)
      self.assertGreater(
          tc['cpo']['fleet_monthly_saved_usd'],
          base_tc['cpo']['fleet_monthly_saved_usd'],
      )
    finally:
      server._global_controller.reset()

  def test_sme_persona_playbooks_and_nl2sql_dml_guardrail_and_step_turn_reset(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    client = TestClient(fast_api_app.app)
    try:
      state = client.get('/api/state').json()
      self.assertIn('persona_playbooks', state)
      playbooks = state['persona_playbooks']
      self.assertEqual(len(playbooks['personas']), 6)
      persona_ids = [p['persona_id'] for p in playbooks['personas']]
      self.assertEqual(
          persona_ids,
          ['finops_lead', 'sre_platform', 'ai_engineer', 'product_quality', 'security_governance', 'cfo_exec'],
      )
      html = client.get('/ui').text
      ancestry = _element_ancestry(html)
      for p in playbooks['personas']:
        # A role guide, not a scorecard: no invented before/after scores.
        self.assertNotIn('after_score', p)
        self.assertNotIn('before_score', p)
        self.assertTrue(p['key_questions_answered'])
        self.assertTrue(p['primary_kpis'])
        target = p['target_panel_id']
        self.assertIn(target, ancestry, f'{p["persona_id"]}: #{target} is not in the dashboard')
        ids, classes = ancestry[target]
        self.assertIn(f'tabPanel{p["primary_tab"]}', ids, f'{p["persona_id"]}: #{target} is not on its start tab')
        self.assertNotIn('sim-panel', classes, f'{p["persona_id"]}: #{target} is a simulator panel')

      # Verify NL2SQL read-only guardrail blocks DML/DDL injection attempts (Opus AF-08)
      nl_dml = client.post('/api/nl2sql', json={'question': 'DROP TABLE aive_logs.ratings_log; DELETE FROM billing'})
      self.assertEqual(nl_dml.status_code, 200)
      nl_dml_body = nl_dml.json()
      self.assertTrue(nl_dml_body['sql_safety_audit']['read_only_enforced'])
      self.assertTrue(nl_dml_body['sql_safety_audit']['blocked_dml_attempt'])
      self.assertNotIn('DROP TABLE', nl_dml_body['generated_sql'].upper())
      self.assertTrue(nl_dml_body['generated_sql'].strip().upper().startswith('SELECT'))

      # Verify /api/step_turn and /api/reset endpoints work end-to-end
      step_resp = client.post('/api/step_turn', json={})
      self.assertEqual(step_resp.status_code, 200)
      step_state = step_resp.json()
      self.assertEqual(len(step_state['turns']), 7)
      self.assertEqual(step_state['summary']['total_turns'], 7)

      reset_resp = client.post('/api/reset', json={})
      self.assertEqual(reset_resp.status_code, 200)
      reset_state = reset_resp.json()
      self.assertEqual(len(reset_state['turns']), 6)
      self.assertEqual(reset_state['summary']['total_turns'], 6)
    finally:
      server._global_controller.reset()

  def test_ux_refinement_six_tabs_calm_palette_and_plain_english_readability(self) -> None:
    import pathlib

    from vibelift.ui import template as ui_template

    state = server._global_controller.get_state_payload(fast_mcp=True)
    html = ui_template.render_dashboard_html(initial_state=state)

    # 1. Verify 6 step-by-step tabs and panels exist
    expected_tabs = (
        ('tabBtn0', 'tabPanel0', 'Gemini Enterprise Agent Fleet'),
        ('tabBtn1', 'tabPanel1', 'Goals &amp; Metrics'),
        ('tabBtn2', 'tabPanel2', 'Testing &amp; History'),
        ('tabBtn3', 'tabPanel3', 'Cost &amp; Billing'),
        ('tabBtn4', 'tabPanel4', 'Users &amp; Feedback'),
        ('tabBtn5', 'tabPanel5', 'Tools &amp; SDK'),
    )
    for btn_id, panel_id, label in expected_tabs:
      self.assertIn(f'id="{btn_id}"', html)
      self.assertIn(f'id="{panel_id}"', html)
      self.assertIn(label, html)

    # 2. Verify Cost & Billing segmented sub-views exist to prevent vertical information overload
    for sub_id in (
        'costSubBtn_summary',
        'costSubBtn_calculator',
        'costSubBtn_code_audit',
        'costSubBtn_limits',
        'costSubBtn_all',
    ):
      self.assertIn(f'id="{sub_id}"', html)
    self.assertIn('switchCostSubView(', html)

    # 3. Verify progressive disclosure above the fold (collapsible role guide + hidden SQL drawer by default)
    self.assertIn('id="roleGuideDisclosure"', html)
    self.assertRegex(html, r'id="nl2sqlCopilotDrawer"[^>]*class="[^"]*hidden[^"]*"')

    # 4. Verify Calm Neutral Slate palette in CSS root and absence of neon purple/orange chart colors
    self.assertIn('--text-primary: #0f172a;', html)
    self.assertIn('--bg: #f8fafc;', html)
    self.assertIn('--g-blue: #334155;', html)
    self.assertNotIn('#9334e6', html)
    self.assertNotIn('#e37400', html)

    # 5. Verify zero academic "AI big words" across ui_template.py and alpha_evolve_optimizer.py
    banned_big_phrases = (
        'Pareto Frontier',
        'Pareto Loop',
        'Bayesian Tuner',
        'Forensic Inspector',
        'Forensic Mutation Reason',
        'Multiplicative ROI Waterfall',
        'Multiplicative compounding',
        'Deterministic engine',
        'Deterministic FinOps Engine',
    )
    repo_dir = pathlib.Path(__file__).resolve().parent.parent
    for fname in ('vibelift/ui/template.py', 'vibelift/optimizer.py'):
      source_text = (repo_dir / fname).read_text(encoding='utf-8')
      for phrase in banned_big_phrases:
        self.assertNotIn(
            phrase.lower(),
            source_text.lower(),
            f'Found academic/AI big phrase "{phrase}" in {fname}',
        )

  def test_telemetry_grounding_validator_and_live_gcp_no_fake_users(self) -> None:
    from fastapi.testclient import TestClient

    from app import fast_api_app

    client = TestClient(fast_api_app.app)
    try:
      state = client.get('/api/state').json()
      self.assertIn('telemetry_validation', state)
      tv = state['telemetry_validation']
      self.assertEqual(tv['overall_status'], 'VERIFIED_GROUNDED')
      self.assertEqual(tv['failed_count'], 0)
      self.assertEqual(tv['grounding_score_pct'], 100.0)
      self.assertGreaterEqual(tv['total_checks'], 10)
      self.assertIn('llm_judge', tv)
      self.assertEqual(tv['llm_judge']['verdict'], 'VERIFIED_GROUNDED')

      # Verify /api/validate_telemetry GET and POST endpoints
      val_get = client.get('/api/validate_telemetry').json()
      self.assertEqual(val_get['overall_status'], 'VERIFIED_GROUNDED')
      val_post = client.post('/api/validate_telemetry', json={'llm_judge': False}).json()
      self.assertEqual(val_post['overall_status'], 'VERIFIED_GROUNDED')

      # Simulate live example-project BigQuery telemetry sync and verify zero fake users or fake GCS URIs
      live_bq_mock = {
          'project_id': 'example-project',
          'datasets_queried': ['ds_ge_audit_raw', 'sre_triage_agent_telemetry', 'vibelift_analytics'],
          'power_users_ldap': [
              {
                  'user_ldap': 'live-user',
                  'user_email': 'live-user@example.com',
                  'department': 'GCP Agent Platform & FinOps (example-project)',
                  'primary_agent': 'VibeLift Analytics & FinOps',
                  'sessions_7d': 35,
                  'total_tokens_m': 0.16,
                  'thinking_tokens_k': 24.0,
                  'background_tokens_k': 67.8,
                  'cache_hit_pct': 42.9,
                  'avg_csat': 5.0,
                  'monthly_cost_usd': 0.24,
                  'cost_saved_usd': 0.19,
                  'anomaly_status': 'LIVE_BIGQUERY_AUDIT',
              },
          ],
          'ratings_logs': [
              {
                  'rating_id': 'bq-audit-1',
                  'timestamp': '2026-04-17T06:03:00Z',
                  'session_id': '1405660395354341226',
                  'user_email': 'live-user@example.com',
                  'user_ldap': 'live-user',
                  'rating': 5,
                  'feedback_text': 'Real BigQuery StreamAssist session in example-project.',
              },
          ],
          'usage_logs': [
              {
                  'event_id': 'bq-span-1',
                  'timestamp': '2026-04-17T06:03:00Z',
                  'session_id': '1405660395354341226',
                  'user_email': 'live-user@example.com',
                  'user_ldap': 'live-user',
                  'department': 'GCP Agent Platform & FinOps (example-project)',
                  'task_type': 'GE_STREAM_ASSIST',
                  'model_name': 'gemini-2.5-flash',
                  'prompt_length_chars': 240,
                  'total_tokens': 16114,
                  'input_tokens': 15200,
                  'output_tokens': 914,
                  'thinking_tokens': 512,
                  'background_tokens': 6778,
                  'latency_ms': 256.8,
                  'status': 'OK',
                  'outputs': [{'gcs_uri': 'bq://example-project.sre_triage_agent_telemetry._AllSpans/1405660395354341226'}],
                  'csat_rating': 5,
              },
          ],
          'decorator_events': [
              {
                  'event_id': 'dec-live-1',
                  'timestamp_utc': '2026-04-17T06:03:00Z',
                  'agent_id': 'vibelift_analytics',
                  'tool_name': 'invoke_agent',
                  'model': 'gemini-2.5-flash',
                  'latency_ms': 256.8,
                  'input_tokens': 15200,
                  'cached_input_tokens': 6778,
                  'output_tokens': 914,
                  'thinking_tokens': 512,
                  'total_tokens': 16114,
                  'cache_hit_pct': 42.9,
                  'estimated_cost_usd': 0.0034,
                  'status': 'OK',
                  'trace_id': '1405660395354341226',
              },
          ],
          'otel_genai_summary': {
              'span_count': 13,
              'total_input_tokens': 158101,
              'total_output_tokens': 3036,
              'total_cached_tokens': 67783,
              'avg_latency_ms': 256.8,
              'observed_cache_hit_pct': 42.9,
          },
          'runaway_alerts': [],
      }
      live_fleet = dict(state['ge_fleet'])
      live_fleet['project_id'] = 'example-project'
      server._global_controller.optimizer.sync_from_ge_fleet(live_fleet, bq_insights=live_bq_mock)
      telemetry.set_live_aive_logs(
          usage_logs=live_bq_mock['usage_logs'],
          ratings_logs=live_bq_mock['ratings_logs'],
      )
      telemetry.set_live_decorator_events(live_bq_mock['decorator_events'])

      uc_live = server._global_controller.optimizer.get_user_centric_payload()
      live_ldaps = [u['user_ldap'] for u in uc_live['power_users_ldap']]
      self.assertEqual(live_ldaps, ['live-user'])
      for fake_ldap in ('user-a', 'user-b', 'user-c', 'user-d', 'user-e'):
        self.assertNotIn(fake_ldap, live_ldaps)
    finally:
      telemetry.set_live_aive_logs(None, None)
      telemetry.set_live_decorator_events(None)
      server._global_controller.optimizer._live_bq_insights = None
      server._global_controller.reset()


if __name__ == '__main__':
  unittest.main()
