"""Tests for the 5 enterprise production remediation workstreams."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import agent as adk_agent
from finops_dashboard import (
  cli_lint,
  mcp_server,
  optimizer,
  prompt_xray,
  validator,
)

ROOT = Path(__file__).resolve().parent.parent


class Workstream1HackathonTheaterRemovalAndNl2SqlTest(unittest.TestCase):
  """Verifies simulator/self-grading theater removal and Gemini NL2SQL + Pre-Built SQL Recipes."""

  def test_simulator_and_self_grading_files_removed_from_main(self) -> None:
    self.assertFalse((ROOT / 'finops_dashboard' / 'long_running_agent.py').exists())
    self.assertFalse((ROOT / 'finops_dashboard' / 'sme_eval.py').exists())
    self.assertFalse((ROOT / 'docs' / 'SME_EVALUATION_REPORT.md').exists())
    opt = optimizer.FinOpsAlphaEvolveOptimizer()
    self.assertFalse(hasattr(opt, 'run_next_generation'))
    self.assertFalse(hasattr(opt, 'inject_anomaly'))

  def test_nl2sql_gemini_structured_generation_and_recipe_fallback(self) -> None:
    opt = optimizer.FinOpsAlphaEvolveOptimizer()
    valid_gemini_sql = (
        'SELECT engine_id, COUNT(*) AS turns '
        'FROM `proj-live.finops_mart.fct_turns` '
        'WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) '
        'GROUP BY 1 LIMIT 10'
    )
    res = opt.execute_nl2sql_telemetry_query(
        'Count turns per engine over the last 7 days',
        project_id='proj-live',
        llm_sql_generator=lambda *_args: valid_gemini_sql,
    )
    self.assertEqual(res['sql_safety_audit']['sql_source'], 'GEMINI_NL2SQL_VERIFIED')
    self.assertTrue(res['sql_safety_audit']['read_only_enforced'])
    self.assertEqual(res['generated_sql'], valid_gemini_sql)
    self.assertEqual(res['sql_safety_audit']['max_bytes_billed_cap'], optimizer.NL2SQL_MAX_BYTES_BILLED)

    # Unsafe SQL from Gemini must be rejected by verify_nl2sql_sql and fall back to Pre-Built FinOps SQL Recipes
    unsafe_gemini_sql = 'DELETE FROM `proj-live.finops_mart.fct_turns` WHERE TRUE'
    fallback_res = opt.execute_nl2sql_telemetry_query(
        'Show daily usage from the session mart',
        project_id='proj-live',
        llm_sql_generator=lambda *_args: unsafe_gemini_sql,
    )
    self.assertEqual(fallback_res['sql_safety_audit']['sql_source'], 'PRE_BUILT_FINOPS_SQL_RECIPES')
    self.assertTrue(fallback_res['sql_safety_audit']['read_only_enforced'])
    self.assertIn('fct_sessions', fallback_res['generated_sql'])


class Workstream2GaModelDefaultsTest(unittest.TestCase):
  """Verifies GA Vertex AI model defaults (gemini-2.5-flash) and custom rate card overrides."""

  def test_ga_vertex_ai_model_defaults(self) -> None:
    self.assertEqual(adk_agent.ADK_MODEL, 'gemini-2.5-flash')
    self.assertEqual(validator.JUDGE_MODEL, 'gemini-2.5-flash')
    self.assertEqual(prompt_xray.DEFAULT_MODEL, 'gemini-2.5-flash')
    opt = optimizer.FinOpsAlphaEvolveOptimizer()
    sim = opt.simulate_what_if_scenario()
    self.assertEqual(sim['model_tier'], 'gemini-2.5-flash')

  def test_custom_rate_card_override_via_env(self) -> None:
    fake_service, _ = __import__('tests.test_fleet', fromlist=['make_fake_service']).make_fake_service()
    custom = json.dumps({
        'gemini-custom-enterprise': {
            'input': 0.25,
            'cached_read': 0.025,
            'cache_write': 0.25,
            'output': 1.25,
        }
    })
    with mock.patch.dict(os.environ, {'FINOPS_RATE_CARDS_JSON': custom}):
      cards = fake_service.rate_cards()
    self.assertIn('gemini-custom-enterprise', cards)
    self.assertEqual(cards['gemini-custom-enterprise']['input'], 0.25)


class Workstream3TerraformAndDataformModulesTest(unittest.TestCase):
  """Verifies Terraform and Dataform data plane modules for Org/Folder/Project sinks."""

  def test_terraform_module_supports_org_folder_project_scopes(self) -> None:
    tf_dir = ROOT / 'deploy' / 'terraform'
    for fname in ('versions.tf', 'variables.tf', 'main.tf', 'outputs.tf', 'README.md'):
      self.assertTrue((tf_dir / fname).is_file(), f'Missing deploy/terraform/{fname}')
    main_tf = (tf_dir / 'main.tf').read_text(encoding='utf-8')
    vars_tf = (tf_dir / 'variables.tf').read_text(encoding='utf-8')
    self.assertIn('google_logging_organization_sink', main_tf)
    self.assertIn('google_logging_folder_sink', main_tf)
    self.assertIn('google_logging_project_sink', main_tf)
    self.assertIn('include_children', main_tf)
    self.assertIn('var.mart_dataset_id', main_tf)
    self.assertIn('finops_mart', vars_tf)

  def test_dataform_package_definitions_exist(self) -> None:
    df_dir = ROOT / 'deploy' / 'dataform'
    self.assertTrue((df_dir / 'workflow_settings.yaml').is_file())
    self.assertTrue((df_dir / 'definitions' / 'sources.js').is_file())
    self.assertTrue((df_dir / 'definitions' / 'curated' / 'curated_staging_views.js').is_file())
    for mart_file in ('v_fct_turns.sqlx', 'fct_turns.sqlx', 'fct_sessions.sqlx', 'agg_daily_usage.sqlx', 'v_looker_l1_l2_support.sqlx'):
      self.assertTrue((df_dir / 'definitions' / 'mart' / mart_file).is_file(), f'Missing {mart_file}')


class Workstream4BigQueryPushdownTokenScanTest(unittest.TestCase):
  """Verifies 100% BigQuery SQL pushdown for fleet and workload token scans at 1M+ turns/day."""

  def test_bigquery_genai_usage_pushdown_handles_million_plus_turns_without_truncation(self) -> None:
    fake_service, _ = __import__('tests.test_fleet', fromlist=['make_fake_service']).make_fake_service()

    def fake_bq_query(sql: str):
      if 'fct_turns' in sql and 'GROUP BY reasoning_engine_id' in sql:
        return (
            [
                {
                    'reasoning_engine_id': '9001',
                    'llm_calls': '1250000',
                    'input_tokens': '2500000000',
                    'output_tokens': '350000000',
                    'cached_tokens': '1800000000',
                    'conversations': '95000',
                    'last_activity': '2026-10-14T23:00:00Z',
                    'models_csv': 'gemini-2.5-flash',
                    'entries_scanned': '1250000',
                }
            ],
            {'error': None, 'total_bytes_processed': 4096},
        )
      return ([], {'error': None})

    fake_service._bq_query_fn = fake_bq_query
    fake_service.bq_token_pushdown = True
    usage = fake_service._genai_usage('proj-test', ['9001'], 24 * 3600)
    self.assertEqual(usage['scan_mode'], 'BIGQUERY_PUSHDOWN')
    self.assertFalse(usage['truncated'])
    self.assertEqual(usage['entries_scanned'], 1_250_000)
    re_usage = usage['by_engine']['9001']
    self.assertEqual(re_usage['llm_calls'], 1_250_000)
    self.assertEqual(re_usage['input_tokens'], 2_500_000_000)
    self.assertEqual(re_usage['cached_tokens'], 1_800_000_000)

  def test_bigquery_workload_log_usage_pushdown(self) -> None:
    fake_service, _ = __import__('tests.test_fleet', fromlist=['make_fake_service']).make_fake_service()

    def fake_bq_query(sql: str):
      return (
          [
              {
                  'service_name': 'order-bot',
                  'model': 'gemini-2.5-flash',
                  'llm_calls': '500000',
                  'input_tokens': '800000000',
                  'output_tokens': '120000000',
                  'cached_tokens': '600000000',
                  'conversations_count': '12000',
                  'timestamp': '2026-10-14T23:00:00Z',
                  'entries_scanned': '500000',
              }
          ],
          {'error': None},
      )

    fake_service._bq_query_fn = fake_bq_query
    fake_service.bq_token_pushdown = True
    wl = fake_service._workload_log_usage('proj-test', 24 * 3600)
    self.assertEqual(wl['scan_mode'], 'BIGQUERY_PUSHDOWN')
    self.assertFalse(wl['truncated'])
    self.assertEqual(wl['entries_scanned'], 500_000)
    self.assertEqual(wl['records'][0]['llm_calls'], 500_000)
    self.assertEqual(wl['records'][0]['service'], 'order-bot')


class Workstream5CliLinterAndMcpGateTest(unittest.TestCase):
  """Verifies finops-lint CLI, GitHub Action workflow, and lint_agent_finops MCP/ADK tool."""

  def test_cli_lint_detects_findings_and_returns_nonzero_exit_code(self) -> None:
    bad_code = (
        'LARGE_PROMPT = "' + ('A' * 1600) + '"\n'
        'def run_agent(items):\n'
        '    for item in items:\n'
        '        client.generate_content(item)\n'
    )
    out = io.StringIO()
    with mock.patch('sys.stdout', out):
      rc = cli_lint.main(['--code', bad_code, '--format', 'json', '--fail-on-findings'])
    self.assertEqual(rc, 1)
    report = json.loads(out.getvalue())
    self.assertFalse(report['passed'])
    rule_ids = {f['rule_id'] for f in report['findings']}
    self.assertIn('FIN-01', rule_ids)
    self.assertIn('FIN-02', rule_ids)

  def test_cli_lint_passes_clean_code_and_supports_prompt_xray(self) -> None:
    clean_code = 'def add(a: int, b: int) -> int:\n    return a + b\n'
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8') as tf:
      tf.write(clean_code)
      temp_path = tf.name
    try:
      out = io.StringIO()
      static_prompt = 'System Role: Support\n' + ('Policy block ' * 800)
      with mock.patch('sys.stdout', out):
        rc = cli_lint.main([
            temp_path,
            '--prev-prompt', static_prompt,
            '--curr-prompt', static_prompt + '\nUser: Help 2',
            '--format', 'json',
            '--fail-on-findings',
        ])
      self.assertEqual(rc, 0)
      report = json.loads(out.getvalue())
      self.assertTrue(report['passed'])
      self.assertIsNotNone(report['prompt_xray'])
      self.assertIn('summary', report['prompt_xray'])
    finally:
      os.unlink(temp_path)

  def test_github_action_workflow_and_mcp_tool_exist(self) -> None:
    wf = ROOT / '.github' / 'workflows' / 'finops-lint.yml'
    self.assertTrue(wf.is_file())
    self.assertIn('cli_lint', wf.read_text(encoding='utf-8'))

    code, _, body = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'lint-1',
        'method': 'tools/call',
        'params': {
            'name': 'lint_agent_finops',
            'arguments': {
                'code_snippet': 'for x in items:\n    client.generate_content(x)\n',
                'previous_prompt': 'System: Static\nTime: 10:00\n' + (' Block' * 100),
                'current_prompt': 'System: Static\nTime: 10:01\n' + (' Block' * 100),
            },
        },
    })
    self.assertEqual(code, 200)
    sc = json.loads(body)['result']['structuredContent']
    self.assertGreaterEqual(sc['findings_count'], 1)
    self.assertIsNotNone(sc['prompt_xray'])

    # Verify deploy/setup_mcp_connector.py ALL_TOOLS matches MCP tools/list
    connector_mod = __import__('deploy.setup_mcp_connector', fromlist=['ALL_TOOLS'])
    tl_code, _, tl_body = mcp_server.handle_jsonrpc_sync({
        'jsonrpc': '2.0',
        'id': 'tl-1',
        'method': 'tools/list',
    })
    self.assertEqual(tl_code, 200)
    mcp_tool_names = {t['name'] for t in json.loads(tl_body)['result']['tools']}
    self.assertEqual(set(connector_mod.ALL_TOOLS), mcp_tool_names)

    # Verify tests/eval/datasets/basic-dataset.json covers all 10 ADK tools on root_agent
    eval_ds = json.loads((ROOT / 'tests' / 'eval' / 'datasets' / 'basic-dataset.json').read_text(encoding='utf-8'))
    covered_adk_tools = {
        tool_name
        for case in eval_ds.get('eval_cases', [])
        for tool_name in case.get('expected_tools', [])
    }
    root_tool_names = {getattr(fn, '__name__', str(fn)) for fn in adk_agent.root_agent.tools}
    self.assertEqual(covered_adk_tools, root_tool_names)


if __name__ == '__main__':
  unittest.main()
