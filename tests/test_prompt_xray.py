"""Tests for the Prompt Cache X-Ray (engine, controller, live GCS path, HTTP, MCP and ADK surfaces)."""

import asyncio
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app import agent as adk_agent_module
from vibelift import gcp_telemetry, mcp_server, prompt_xray, server
from vibelift.ui import template as ui_template


def _heatmap_length(segments):
  return sum(int(s['chars']) if s.get('collapsed') else len(s['text']) for s in segments)


class PromptXrayEngineTest(unittest.TestCase):

  def test_example_breaks_on_timestamp_line_and_rewrite_is_remeasured(self) -> None:
    r = prompt_xray.analyze(prompt_xray.EXAMPLE_PREVIOUS, prompt_xray.EXAMPLE_CURRENT, monthly_requests=100_000)
    self.assertEqual(r['status'], prompt_xray.STATUS_BUSTED)
    self.assertEqual(r['breakpoint']['line'], 2)
    self.assertIn('Current time:', r['breakpoint']['current_line'])
    self.assertIn('timestamp', {c['kind'] for c in r['causes']})
    self.assertLess(r['cached_prefix_pct'], 5.0)
    self.assertGreater(r['recoverable_tokens_per_request'], 2000)
    # Below the documented 2,048-token minimum today; above it after the rewrite.
    self.assertEqual(r['cache_minimum_tokens'], 2048)
    self.assertTrue(r['below_cache_minimum'])
    rw = r['rewrite']
    self.assertTrue(rw['changed'])
    self.assertGreater(rw['projected_cached_prefix_pct'], 95.0)
    self.assertFalse(rw['projected_below_cache_minimum'])
    self.assertEqual(rw['json_lines_key_sorted'], 1)
    self.assertEqual({m['line'] for m in rw['moved_lines']}, {2, 3})
    self.assertTrue(rw['rewritten_prompt'].rstrip().endswith('User: sam.chen@example.com'))
    self.assertLess(rw['projected_stranded_cost_usd_per_1k_requests'], r['stranded_cost_usd_per_1k_requests'])

  def test_cost_is_rate_card_arithmetic_and_monthly_needs_volume(self) -> None:
    card = prompt_xray.telemetry.RATE_CARDS['gemini-2.5-flash']
    with_volume = prompt_xray.analyze(prompt_xray.EXAMPLE_PREVIOUS, prompt_xray.EXAMPLE_CURRENT, monthly_requests=1000)
    expected = with_volume['recoverable_tokens_per_request'] * (
        card.input_per_million_usd - card.cached_read_per_million_usd) / 1e6
    self.assertAlmostEqual(with_volume['stranded_cost_usd_per_request'], expected, places=8)
    self.assertAlmostEqual(with_volume['stranded_cost_usd_per_month'], round(expected * 1000, 2))
    no_volume = prompt_xray.analyze(prompt_xray.EXAMPLE_PREVIOUS, prompt_xray.EXAMPLE_CURRENT)
    self.assertIsNone(no_volume['stranded_cost_usd_per_month'])
    self.assertTrue(any('request volume' in n for n in no_volume['notes']))

  def test_unknown_model_omits_dollars(self) -> None:
    r = prompt_xray.analyze('a\nb', 'a\nc', model='not-a-model')
    self.assertIsNone(r['rate_card'])
    self.assertIsNone(r['stranded_cost_usd_per_1k_requests'])
    self.assertIsNone(r['cache_minimum_tokens'])
    self.assertTrue(any('No rate card' in n for n in r['notes']))

  def test_append_only_identical_and_shorter(self) -> None:
    grow = prompt_xray.analyze('static\nhistory 1', 'static\nhistory 1\nhistory 2')
    self.assertEqual(grow['status'], prompt_xray.STATUS_APPEND_ONLY)
    self.assertIsNone(grow['breakpoint'])
    self.assertEqual(grow['recoverable_tokens_per_request'], 0)
    self.assertEqual([s['state'] for s in grow['heatmap']], ['cached', 'appended'])
    same = prompt_xray.analyze('abc', 'abc')
    self.assertEqual(same['status'], prompt_xray.STATUS_IDENTICAL)
    self.assertEqual(same['cached_prefix_pct'], 100.0)
    self.assertEqual(prompt_xray.analyze('abcdef', 'abc')['status'], prompt_xray.STATUS_SHORTER)

  def test_classifies_json_key_order_whitespace_and_content_edit(self) -> None:
    tail = '\nstatic tail that stays the same'
    js = prompt_xray.analyze('{"a": 1, "b": 2}' + tail, '{"b": 2, "a": 1}' + tail)
    self.assertEqual(js['causes'][0]['kind'], 'json_key_order')
    ws = prompt_xray.analyze('x\n  indented' + tail, 'x\n    indented' + tail)
    self.assertEqual(ws['causes'][0]['kind'], 'whitespace')
    edit = prompt_xray.analyze('Be terse.' + tail, 'Be verbose.' + tail)
    self.assertEqual(edit['causes'][0]['kind'], 'content_edit')

  def test_detects_uuid_email_and_epoch(self) -> None:
    kinds = {f['kind'] for f in prompt_xray.scan_volatile_spans(
        'req 123e4567-e89b-12d3-a456-426614174000 by bob@example.com at 1759300000')}
    self.assertTrue({'uuid', 'email', 'epoch'} <= kinds)

  def test_heatmap_covers_prompt_and_collapses_long_runs(self) -> None:
    long_static = 'policy line\n' * 900
    r = prompt_xray.analyze('Time: 2026-10-01T09:00:00Z\n' + long_static, 'Time: 2026-10-01T09:05:00Z\n' + long_static)
    self.assertEqual(_heatmap_length(r['heatmap']), r['current_chars'])
    self.assertTrue(any(s.get('collapsed') for s in r['heatmap']))
    self.assertIn('buster', {s['state'] for s in r['heatmap']})

  def test_logged_token_count_scales_tokens(self) -> None:
    curr = 'x' * 1000
    r = prompt_xray.analyze('y' + curr[1:], curr, current_input_tokens=500)
    self.assertEqual(r['token_basis'], 'scaled_from_logged_input_tokens')
    self.assertEqual(r['current_tokens'], 500)

  def test_rejects_empty_and_oversized(self) -> None:
    with self.assertRaises(ValueError):
      prompt_xray.analyze('', 'x')
    with self.assertRaises(ValueError):
      prompt_xray.analyze('x' * (prompt_xray.MAX_PROMPT_CHARS + 1), 'x')

  def test_render_logged_prompt_matches_otel_jsonl_shapes(self) -> None:
    sys_jsonl = '{"content":"You are an SRE triage agent.\\nBe precise."}\n'
    in_jsonl = ('{"role":"user","parts":[{"content":"What logs do we have?","type":"text"}],"index":0}\n'
                '{"role":"model","parts":[{"type":"tool_call","name":"list_log_files","arguments":{}}]}\n')
    text = prompt_xray.render_logged_prompt(sys_jsonl, in_jsonl)
    self.assertTrue(text.startswith('[system]\nYou are an SRE triage agent.\nBe precise.'))
    self.assertIn('[user]\nWhat logs do we have?', text)
    self.assertIn('"name": "list_log_files"', text)


class PromptXrayControllerTest(unittest.TestCase):

  def setUp(self) -> None:
    self.ctrl = server._global_controller

  def test_paste_mode_and_input_validation(self) -> None:
    ok = self.ctrl.prompt_xray({'previous_prompt': prompt_xray.EXAMPLE_PREVIOUS,
                                'current_prompt': prompt_xray.EXAMPLE_CURRENT,
                                'monthly_requests': '250000', 'model': 'gemini-2.5-pro'})
    self.assertEqual(ok['status'], prompt_xray.STATUS_BUSTED)
    self.assertEqual(ok['model'], 'gemini-2.5-pro')
    self.assertEqual(ok['monthly_requests'], 250000)
    self.assertEqual(ok['source'], {'kind': 'pasted'})
    bad = self.ctrl.prompt_xray({'previous_prompt': 'x', 'current_prompt': {'not': 'text'}})
    self.assertEqual(bad['status'], 'ERROR')

  def test_live_turns_not_connected_and_disabled(self) -> None:
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=False):
      listing = self.ctrl.prompt_xray_live_turns()
    self.assertEqual(listing['status'], 'NOT_CONNECTED')
    self.assertIn('gemini-2.5-flash', listing['available_models'])
    self.assertEqual(listing['example']['current_prompt'], prompt_xray.EXAMPLE_CURRENT)
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.dict(os.environ, {server.PROMPT_XRAY_LIVE_ENV: ''}):
      self.assertEqual(self.ctrl.prompt_xray_live_turns()['status'], 'DISABLED')
      res = self.ctrl.prompt_xray({'previous_event_id': 'a', 'current_event_id': 'b'})
    self.assertEqual(res['status'], 'DISABLED')

  def _live_rows(self):
    base = {'agent_name': 'root_agent', 'user_id': 'u1', 'conversation_id': 'c1',
            'source_table': 'p.otel.gen_ai_client_inference_operation_details'}
    return [
        {**base, 'event_id': 'e2', 'timestamp': '2026-06-18 21:50:36', 'input_tokens': 120,
         'sys_gcs_uri': 'gs://logs/completions/sys.jsonl', 'input_gcs_uri': 'gs://logs/completions/e2_inputs.jsonl'},
        {**base, 'event_id': 'e1', 'timestamp': '2026-06-18 21:49:10', 'input_tokens': 90,
         'sys_gcs_uri': 'gs://logs/completions/sys.jsonl', 'input_gcs_uri': 'gs://logs/completions/e1_inputs.jsonl'},
    ]

  def test_live_mode_reads_only_allowlisted_refs(self) -> None:
    objects = {
        'gs://logs/completions/sys.jsonl': '{"content":"You are an SRE triage agent."}\n',
        'gs://logs/completions/e1_inputs.jsonl': '{"role":"user","parts":[{"content":"hi","type":"text"}]}\n',
        'gs://logs/completions/e2_inputs.jsonl': ('{"role":"user","parts":[{"content":"hi","type":"text"}]}\n'
                                                  '{"role":"user","parts":[{"content":"next","type":"text"}]}\n'),
    }
    svc = self.ctrl.gcp_telemetry
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.dict(os.environ, {server.PROMPT_XRAY_LIVE_ENV: '1'}), \
         mock.patch.object(svc, 'fetch_prompt_snapshot_turns', return_value=self._live_rows()), \
         mock.patch.object(svc, 'read_gcs_text', side_effect=lambda uri: objects[uri]) as reader:
      listing = self.ctrl.prompt_xray_live_turns()
      self.assertEqual(listing['status'], 'OK')
      self.assertNotIn('sys_gcs_uri', listing['turns'][0])
      res = self.ctrl.prompt_xray({'previous_event_id': 'e1', 'current_event_id': 'e2'})
      self.assertEqual(res['status'], prompt_xray.STATUS_APPEND_ONLY)
      self.assertEqual(res['source']['kind'], 'live_otel_gcs')
      self.assertEqual(res['token_basis'], 'scaled_from_logged_input_tokens')
      self.assertEqual(res['current_tokens'], 120)
      self.assertEqual({c.args[0] for c in reader.call_args_list}, set(objects))
      unknown = self.ctrl.prompt_xray({'previous_event_id': 'e1', 'current_event_id': 'gs://evil/x'})
      self.assertEqual(unknown['status'], 'ERROR')

  def test_live_mode_surfaces_gcs_errors(self) -> None:
    svc = self.ctrl.gcp_telemetry
    err = gcp_telemetry.GcsReadError('FORBIDDEN', 'cannot read gs://logs')
    with mock.patch.object(self.ctrl, '_is_live_gcp', return_value=True), \
         mock.patch.dict(os.environ, {server.PROMPT_XRAY_LIVE_ENV: 'true'}), \
         mock.patch.object(svc, 'fetch_prompt_snapshot_turns', return_value=self._live_rows()), \
         mock.patch.object(svc, 'read_gcs_text', side_effect=err):
      res = self.ctrl.prompt_xray({'previous_event_id': 'e1', 'current_event_id': 'e2'})
    self.assertEqual(res['status'], 'SNAPSHOT_UNAVAILABLE')
    self.assertEqual(res['reason'], 'FORBIDDEN')


class GcsReaderTest(unittest.TestCase):

  def setUp(self) -> None:
    self.svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='proj-x', region='us-central1')

  def test_invalid_uri_and_missing_credentials(self) -> None:
    with self.assertRaises(gcp_telemetry.GcsReadError) as ctx:
      self.svc.read_gcs_text('https://example.com/x')
    self.assertEqual(ctx.exception.status, 'INVALID_URI')
    with mock.patch.object(self.svc, '_get_access_token', return_value=None):
      with self.assertRaises(gcp_telemetry.GcsReadError) as ctx:
        self.svc.read_gcs_text('gs://bucket-a/obj.jsonl')
    self.assertEqual(ctx.exception.status, 'NOT_CONNECTED')

  def test_http_errors_map_to_statuses_and_object_path_is_encoded(self) -> None:
    for code, status in ((403, 'FORBIDDEN'), (404, 'NOT_FOUND'), (500, 'ERROR')):
      exc = urllib.error.HTTPError('u', code, 'x', {}, io.BytesIO(b''))
      with mock.patch.object(self.svc, '_get_access_token', return_value='tok'), \
           mock.patch.object(urllib.request, 'urlopen', side_effect=exc):
        with self.assertRaises(gcp_telemetry.GcsReadError) as ctx:
          self.svc.read_gcs_text('gs://bucket-a/dir/obj.jsonl')
      self.assertEqual(ctx.exception.status, status)
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = b'{"content":"ok"}'
    with mock.patch.object(self.svc, '_get_access_token', return_value='tok'), \
         mock.patch.object(urllib.request, 'urlopen', return_value=resp) as opener:
      self.assertEqual(self.svc.read_gcs_text('gs://bucket-a/dir/obj.jsonl'), '{"content":"ok"}')
    self.assertIn('/b/bucket-a/o/dir%2Fobj.jsonl?alt=media', opener.call_args.args[0].full_url)


class PromptXraySurfacesTest(unittest.TestCase):

  def test_http_routes(self) -> None:
    httpd = server.create_http_server(host='127.0.0.1', port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
      base = f'http://127.0.0.1:{httpd.server_address[1]}'
      with mock.patch.object(server._global_controller, '_is_live_gcp', return_value=False):
        with urllib.request.urlopen(f'{base}/api/prompt_xray/live_turns') as resp:
          self.assertEqual(json.loads(resp.read())['status'], 'NOT_CONNECTED')
      req = urllib.request.Request(
          f'{base}/api/prompt_xray', method='POST', headers={'Content-Type': 'application/json'},
          data=json.dumps({'previous_prompt': 'a\nb\nc', 'current_prompt': 'a\nX\nc'}).encode())
      with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
      self.assertEqual(body['status'], prompt_xray.STATUS_BUSTED)
      self.assertEqual(body['breakpoint']['line'], 2)
    finally:
      httpd.shutdown()
      httpd.server_close()

  def test_mcp_and_adk_tools(self) -> None:
    out = asyncio.run(mcp_server._tool_xray_prompt_cache('s', {
        'previous_prompt': prompt_xray.EXAMPLE_PREVIOUS, 'current_prompt': prompt_xray.EXAMPLE_CURRENT}))
    self.assertEqual(out['structuredContent']['status'], prompt_xray.STATUS_BUSTED)
    self.assertIn('line 2', out['content'][0]['text'])
    self.assertTrue(mcp_server._TOOLS_BY_NAME['xray_prompt_cache']['annotations']['readOnlyHint'])
    adk = json.loads(adk_agent_module.xray_prompt_cache(
        prompt_xray.EXAMPLE_PREVIOUS, prompt_xray.EXAMPLE_CURRENT, monthly_requests=1000))
    self.assertNotIn('heatmap', adk)
    self.assertIsNotNone(adk['stranded_cost_usd_per_month'])
    self.assertIn(adk_agent_module.xray_prompt_cache, adk_agent_module.root_agent.tools)

  def test_dashboard_panel_is_wired(self) -> None:
    html = ui_template.render_dashboard_html()
    self.assertIn('id="promptXrayPanel"', html)
    for fn in ('pxSetSource', 'pxLoadExample', 'runPromptXray', 'pxCopyRewrite', 'initPromptXray', 'renderPromptXray'):
      self.assertIn(f'function {fn}(', html)
    self.assertIn('/api/prompt_xray', html)
    self.assertIn("'xray_prompt_cache'", html)
    # XSS guard: the X-Ray renderer never assigns innerHTML.
    start = html.index('// ---------------- Prompt Cache X-Ray')
    end = html.index('async function runTelemetryValidationAudit')
    self.assertNotIn('innerHTML', html[start:end])


if __name__ == '__main__':
  unittest.main()
