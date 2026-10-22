"""Tests for AQuA (Ambient Quality Agent) readiness.

AQuA reviews this agent's production conversations from the BigQuery Agent Analytics plugin's table and
selects them by the root agent's ADK name. A wrong name or missing logging raises no error anywhere: every
investigation just reviews an empty window. These tests pin both, plus the deploy script's opt-in and the
rule that AQuA's Terraform state (.aqua/) is never committed or baked into the image.
"""

import os
import pathlib
import re
import subprocess
import unittest
from unittest import mock

import yaml

from app import agent as adk_agent

ROOT = pathlib.Path(__file__).resolve().parents[1]
ON = {'FINOPS_AGENT_ANALYTICS_DATASET': 'finops_agent_analytics', 'GOOGLE_CLOUD_PROJECT': 'test-project'}


def _read(path: pathlib.Path) -> str:
  return path.read_text(encoding='utf-8')


class RootAgentNameTest(unittest.TestCase):

  def test_manifest_root_agent_name_matches_the_code(self) -> None:
    # agents-cli would otherwise derive finops_dashboard_mcp_app from the project name.
    manifest = yaml.safe_load(_read(ROOT / 'agents-cli-manifest.yaml'))
    self.assertEqual(manifest['create_params']['root_agent_name'], adk_agent.root_agent.name)


class ConversationLoggingTest(unittest.TestCase):

  def test_logging_is_off_unless_a_dataset_is_named(self) -> None:
    self.assertIsNone(adk_agent.agent_analytics_settings({}))
    self.assertIsNone(adk_agent.agent_analytics_settings({'FINOPS_AGENT_ANALYTICS_DATASET': '  '}))
    self.assertEqual(adk_agent.build_agent_analytics_plugins({}), [])
    expected = ['BigQueryAgentAnalyticsPlugin'] if os.environ.get('FINOPS_AGENT_ANALYTICS_DATASET') else []
    self.assertEqual([type(p).__name__ for p in adk_agent.app.plugins], expected)

  def test_dataset_turns_logging_on_with_defaults(self) -> None:
    self.assertEqual(adk_agent.agent_analytics_settings(ON), {
        'project_id': 'test-project',
        'dataset_id': 'finops_agent_analytics',
        'table_id': 'agent_events',
        'location': 'us-central1',
    })

  def test_table_and_location_can_be_overridden(self) -> None:
    settings = adk_agent.agent_analytics_settings(
        {**ON, 'FINOPS_AGENT_ANALYTICS_TABLE': 'turns', 'FINOPS_AGENT_ANALYTICS_LOCATION': 'US'})
    assert settings is not None
    self.assertEqual((settings['table_id'], settings['location']), ('turns', 'US'))

  def test_a_dataset_without_a_project_keeps_logging_off(self) -> None:
    with self.assertLogs(adk_agent._logger, level='WARNING'):
      self.assertIsNone(adk_agent.agent_analytics_settings({'FINOPS_AGENT_ANALYTICS_DATASET': 'ds'}))

  def test_logging_uses_the_bigquery_agent_analytics_plugin(self) -> None:
    plugins = adk_agent.build_agent_analytics_plugins(ON)
    self.assertEqual(len(plugins), 1)
    plugin = plugins[0]
    self.assertEqual(type(plugin).__name__, 'BigQueryAgentAnalyticsPlugin')
    self.assertEqual((plugin.project_id, plugin.dataset_id, plugin.table_id, plugin.location),
                     ('test-project', 'finops_agent_analytics', 'agent_events', 'us-central1'))
    self.assertIs(plugin.config.content_formatter, adk_agent.format_agent_analytics_content)

  def test_content_formatter_unwraps_adk_text_syntax_on_responses(self) -> None:
    # Regression for AQuA insight 8d7fc17f: ADK wraps AGENT_RESPONSE/LLM_RESPONSE text in "text: '...'".
    self.assertEqual(
        adk_agent.format_agent_analytics_content(
            {'response': "text: 'Prompt caching saves $0.864 on FinOps's 1000 requests.'"},
            'AGENT_RESPONSE',
        ),
        {'response': "Prompt caching saves $0.864 on FinOps's 1000 requests."},
    )
    self.assertEqual(
        adk_agent.format_agent_analytics_content(
            {'response': "text: 'First part' | text: 'Second part'", 'usage': {'total': 42}},
            'LLM_RESPONSE',
        ),
        {'response': 'First part\nSecond part', 'usage': {'total': 42}},
    )
    # Plain text and non-response events are left untouched.
    self.assertEqual(
        adk_agent.format_agent_analytics_content({'response': 'Already plain.'}, 'AGENT_RESPONSE'),
        {'response': 'Already plain.'},
    )
    user_payload = {'text_summary': "text: 'Literal user quote'"}
    self.assertIs(adk_agent.format_agent_analytics_content(user_payload, 'USER_MESSAGE_RECEIVED'), user_payload)

  def test_cache_economics_includes_all_token_buckets_and_prompt_mandates_them(self) -> None:
    # Regression for AQuA insight 070a3bdb: tool output and instruction must include all token buckets.
    import json
    res = json.loads(adk_agent.calculate_cache_economics(4000, 3200, model='gemini-2.5-flash'))
    self.assertEqual(res['prompt_tokens'], 4000)
    self.assertEqual(res['cached_tokens'], 3200)
    self.assertEqual(res['uncached_tokens'], 800)
    self.assertEqual(res['cache_write_tokens'], 0)
    self.assertEqual(res['cache_hit_ratio_pct'], 80.0)
    for field in ('prompt_tokens', 'cached_tokens', 'uncached_tokens', 'cache_write_tokens',
                  'cache_hit_ratio_pct', 'naive_cost_usd', 'actual_cost_usd', 'saved_usd'):
      self.assertIn(field, str(adk_agent.root_agent.instruction))

  def test_adk_serves_the_app_so_its_plugins_run(self) -> None:
    # Plugins live on App; if the loader served root_agent instead, logging would silently never run.
    # The loader also loads the repo's .env with override=True (USE_UVICORN, PORT, the real project),
    # which would leak into every later test, so dotenv loading is off and the environment must not change.
    from google.adk.cli.utils.agent_loader import AgentLoader
    before = dict(os.environ)
    with mock.patch.dict(os.environ, {'ADK_DISABLE_LOAD_DOTENV': '1'}):
      self.assertIs(AgentLoader(str(ROOT)).load_agent('app'), adk_agent.app)
    self.assertEqual(dict(os.environ), before)


class DeployScriptOptInTest(unittest.TestCase):

  def setUp(self) -> None:
    self.script = _read(ROOT / 'deploy' / 'deploy_cloud_run.sh')
    start = self.script.index('if [[ -n "${FINOPS_AGENT_ANALYTICS_DATASET:-}" ]]; then')
    self.block = self.script[start:self.script.index('\nfi\n', start) + 4]

  def test_logging_env_is_set_only_for_an_existing_dataset(self) -> None:
    self.assertIn('show --format=json', self.block)
    self.assertIn('grant_dataset_writer "${FINOPS_AGENT_ANALYTICS_DATASET}"', self.block)
    for var in ('FINOPS_AGENT_ANALYTICS_DATASET=', 'FINOPS_AGENT_ANALYTICS_TABLE=',
                'FINOPS_AGENT_ANALYTICS_LOCATION=${ANALYTICS_LOCATION}'):
      self.assertIn(var, self.block)
    self.assertNotIn('FINOPS_AGENT_ANALYTICS_DATASET=', self.script.replace(self.block, ''))

  def test_deploy_script_parses(self) -> None:
    result = subprocess.run(['bash', '-n', str(ROOT / 'deploy' / 'deploy_cloud_run.sh')],
                            capture_output=True, text=True, check=False)
    self.assertEqual(result.returncode, 0, result.stderr)


class AquaFilesTest(unittest.TestCase):

  def test_aqua_state_is_never_committed_or_shipped(self) -> None:
    self.assertIn('.aqua/', _read(ROOT / '.gitignore').splitlines())
    dockerignore = _read(ROOT / '.dockerignore').splitlines()
    self.assertIn('.aqua/', dockerignore)
    self.assertIn('extensions/', dockerignore)
    gcloudignore = _read(ROOT / '.gcloudignore').splitlines()
    self.assertIn('#!include:.gitignore', gcloudignore)
    self.assertIn('.aqua/', gcloudignore)
    self.assertIn('extensions/', gcloudignore)

  def test_deployment_metadata_enables_aqua_source_snapshots(self) -> None:
    import json
    metadata = json.loads(_read(ROOT / 'deployment_metadata.json'))
    self.assertTrue(metadata.get('remote_agent_runtime_id'))
    self.assertEqual(metadata.get('agent_directory'), 'app')
    self.assertTrue(metadata.get('cloud_run_revision'))

  def test_extension_is_pinned_and_outside_this_repos_linters(self) -> None:
    extensions = yaml.safe_load(_read(ROOT / 'agents-cli-extensions.yaml'))['extensions']
    aqua = next(e for e in extensions if e['name'] == 'aqua')
    self.assertRegex(aqua['sha'], r'^[0-9a-f]{40}$')
    self.assertTrue(aqua['ref'].startswith('ambient-quality-agent/v'))
    pyproject = _read(ROOT / 'pyproject.toml')
    self.assertRegex(pyproject, r'extend-exclude = \[[^\]]*"extensions"')
    self.assertRegex(pyproject, r'skip = "[^"]*\bextensions\b')

  def test_plugin_dependencies_are_pinned_for_the_image(self) -> None:
    self.assertIn('google-adk[bigquery-analytics]==', _read(ROOT / 'requirements.txt'))
    self.assertIn('"google-adk[bigquery-analytics]==', _read(ROOT / 'pyproject.toml'))
    constraints = _read(ROOT / 'constraints.txt')
    for pin in ('google-cloud-bigquery-storage', 'google-cloud-storage', 'pyarrow'):
      self.assertRegex(constraints, rf'(?m)^{re.escape(pin)}==\d')


if __name__ == '__main__':
  unittest.main()
