"""Tests for the dashboard enhancements (vibelift/ui/static/dashboard_enhancements.{js,css}).

Covers how the assets are inlined into the self-contained dashboard HTML, the frontend safety rules the
module follows (no HTML-string sinks, no blocking dialogs, https-only console links, CSV formula guard,
users shown by LDAP), and the pure helpers (fleet health, sorting, spend math, adoption, request trend)
run in Node against the real asset.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from vibelift import fleet as ge_fleet
from vibelift.ui import template as ui_template

_STATIC = ui_template._STATIC_DIR  # pylint: disable=protected-access
_JS = (_STATIC / ui_template.ENHANCEMENTS_JS_FILE).read_text(encoding='utf-8')
_CSS = (_STATIC / ui_template.ENHANCEMENTS_CSS_FILE).read_text(encoding='utf-8')


class DashboardAssetInliningTest(unittest.TestCase):
  """The enhancement assets ship inside the one HTML document (the MCP App needs a single file)."""

  @classmethod
  def setUpClass(cls):
    cls.html = ui_template.render_dashboard_html()

  def test_assets_are_inlined_and_no_placeholder_is_left(self):
    self.assertNotIn('__VIBELIFT_ENHANCEMENTS_JS__', self.html)
    self.assertNotIn('__VIBELIFT_ENHANCEMENTS_CSS__', self.html)
    self.assertNotIn('__VIBELIFT_GOOGLEY_LOGO_DATA_URI__', self.html)
    self.assertNotIn('__VIBELIFT_INITIAL_STATE_JSON__', self.html)
    self.assertEqual(self.html.count('var VL = (function() {'), 1)
    self.assertIn('.vl-tt {', self.html)

  def test_enhancement_script_runs_before_the_main_script(self):
    # The main script calls VL.* during its first render, so VL must already exist.
    self.assertLess(self.html.index('var VL = (function() {'), self.html.index('const ADVANCED_ONLY_TABS'))

  def test_assets_cannot_break_out_of_their_tags_or_hit_placeholders(self):
    self.assertNotIn('</script', _JS.lower())
    self.assertNotIn('</style', _CSS.lower())
    self.assertNotIn('__VIBELIFT_', _JS)
    self.assertNotIn('__VIBELIFT_', _CSS)

  def test_closing_tag_in_an_asset_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      with open(os.path.join(tmp, ui_template.ENHANCEMENTS_JS_FILE), 'w', encoding='utf-8') as f:
        f.write('var x = "</script><script>alert(1)</script>";')
      with open(os.path.join(tmp, ui_template.ENHANCEMENTS_CSS_FILE), 'w', encoding='utf-8') as f:
        f.write('')
      ui_template._base_dashboard_html.cache_clear()  # pylint: disable=protected-access
      try:
        with mock.patch.object(ui_template, '_STATIC_DIR', ui_template.pathlib.Path(tmp)):
          with self.assertRaises(ValueError):
            ui_template.render_dashboard_html()
      finally:
        ui_template._base_dashboard_html.cache_clear()  # pylint: disable=protected-access

  def test_state_is_substituted_last(self):
    html = ui_template.render_dashboard_html({'note': '__VIBELIFT_ENHANCEMENTS_JS__ </script>'})
    self.assertEqual(html.count('var VL = (function() {'), 1)
    self.assertIn('"note": "__VIBELIFT_ENHANCEMENTS_JS__ \\u003c/script\\u003e"', html)

  def test_new_dashboard_elements_exist(self):
    for element_id in ('vlFreshChip', 'vlCopyLinkBtn', 'vlHealthStrip', 'vlAttentionBadge', 'vlSpendPanel',
                       'vlSpendKpis', 'vlSpendKpisEst', 'vlSpendChart', 'vlAdoptionPanel', 'vlAdoptionKpis',
                       'vlAdoptionChart', 'vlDrawer', 'vlDrawerBackdrop', 'vlThemeBtn', 'vlPaletteBtn', 'vlPalette',
                       'vlPaletteBackdrop'):
      self.assertIn(f'id="{element_id}"', self.html, element_id)

  def test_live_only_panels_stay_hidden_in_demo_mode(self):
    self.assertRegex(self.html, r'class="panel live-only" id="vlAdoptionPanel"')


class DashboardEnhancementSafetyTest(unittest.TestCase):
  """Frontend safety rules for the enhancement module."""

  def test_no_html_string_sinks_or_dynamic_code(self):
    for pattern in (r'\.innerHTML\b', r'\.outerHTML\b', r'insertAdjacentHTML', r'document\.write',
                    r'\beval\s*\(', r'new Function\b'):
      self.assertIsNone(re.search(pattern, _JS), pattern)

  def test_no_blocking_dialogs(self):
    self.assertIsNone(re.search(r'(?<![\w.])(alert|confirm|prompt)\s*\(', _JS))

  def test_new_tabs_are_opened_without_opener(self):
    self.assertIn("window.open(url, '_blank', 'noopener,noreferrer')", _JS)

  def test_local_storage_holds_only_preferences(self):
    # Only the monthly budget (per project), the compact-rows toggle and the theme; never tokens or user data.
    keys = set(re.findall(r"'(vibelift\.[A-Za-z_.]+)", _JS))
    self.assertEqual(keys, {'vibelift.budget.', 'vibelift.agentsCompact', 'vibelift.theme'})


def _block(css: str, opener: str) -> str:
  """Returns the body of the first `opener { ... }` block, matching nested braces."""
  start = css.index(opener) + len(opener)
  depth = 1
  for i in range(start, len(css)):
    depth += {'{': 1, '}': -1}.get(css[i], 0)
    if depth == 0:
      return css[start:i]
  raise AssertionError(f'unbalanced block: {opener}')


class DashboardThemeTest(unittest.TestCase):
  """Dark mode: applied before first paint, screen-only, follows the MCP host."""

  @classmethod
  def setUpClass(cls):
    cls.html = ui_template.render_dashboard_html()

  def test_theme_is_applied_before_first_paint(self):
    head = self.html[:self.html.index('<body>')]
    self.assertIn('<script>', head)
    self.assertIn("localStorage.getItem('vibelift.theme')", head)
    self.assertIn("setAttribute('data-theme'", head)

  def test_dark_theme_is_screen_only(self):
    css = re.sub(r'/\*.*?\*/', '', _CSS, flags=re.S)
    screen = _block(css, '@media screen {')
    outside = css
    while '@media screen {' in outside:  # drop every screen-only block; no dark rule may remain
      body = _block(outside, '@media screen {')
      outside = outside.replace('@media screen {' + body + '}', '', 1)
    self.assertNotIn('data-theme="dark"', outside)
    self.assertIn('color-scheme: dark;', screen)
    for var in ('--bg', '--surface', '--surface-2', '--border', '--input-border', '--text-primary', '--text-secondary'):
      self.assertIn(f'{var}:', _block(screen, 'html[data-theme="dark"] {'), var)

  def test_light_values_of_new_theme_variables(self):
    root = _block(self.html, ':root {')
    for decl in ('--surface-2: #f8fafc;', '--input-border: #cbd5e1;', '--chip-bg: #f1f5f9;'):
      self.assertIn(decl, root)

  def test_follows_the_mcp_host_theme(self):
    self.assertIn('VL.setHostTheme(res && res.hostContext && res.hostContext.theme);', self.html)
    self.assertIn('if (data.params.theme) VL.setHostTheme(data.params.theme);', self.html)

  def test_static_markup_uses_theme_variables(self):
    body = self.html[self.html.index('<body>'):self.html.index('var VL = (function() {')]
    for literal in ('background:#f8fafc', 'background:#ffffff', 'border:1px solid #cbd5e1', 'color:#475569'):
      self.assertNotIn(literal, body, literal)


def _run_node(script: str) -> dict:
  with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
    f.write(script)
    path = f.name
  try:
    proc = subprocess.run([shutil.which('node'), path], capture_output=True, text=True, check=False, timeout=60)
  finally:
    os.unlink(path)
  if proc.returncode != 0:
    raise AssertionError(f'Node harness failed: {proc.stderr}')
  return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class DashboardScriptsParseTest(unittest.TestCase):

  def test_every_inline_script_is_valid_javascript(self):
    html = ui_template.render_dashboard_html({'mode': 'demo'})
    scripts = re.findall(r'<script>(.*?)</script>', html, flags=re.S)
    self.assertGreaterEqual(len(scripts), 2)
    for i, body in enumerate(scripts):
      with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(body)
        path = f.name
      try:
        proc = subprocess.run([shutil.which('node'), '--check', path], capture_output=True, text=True,
                              check=False, timeout=60)
      finally:
        os.unlink(path)
      self.assertEqual(proc.returncode, 0, f'script #{i}: {proc.stderr}')


_DAY = dt.date(2026, 10, 1)


def _days(n, **fields):
  return [dict({'day': (_DAY + dt.timedelta(days=i)).isoformat()}, **fields) for i in range(n)]


_CASES = {
    'agents': {
        'healthy': {'state': 'ENABLED', 'metrics': {'requests': 1000, 'errors_5xx': 2, 'errors_4xx': 10,
                                                    'latency_p95_ms': 3000, 'llm_calls': 2500}},
        'failing_5xx': {'state': 'ENABLED', 'metrics': {'requests': 610, 'errors_5xx': 41, 'errors_4xx': 22}},
        'failing_loop': {'state': 'ENABLED', 'metrics': {'requests': 220, 'errors_5xx': 1, 'llm_calls': 9790}},
        'degraded_5xx': {'state': 'ENABLED', 'metrics': {'requests': 1000, 'errors_5xx': 20}},
        'degraded_4xx': {'state': 'ENABLED', 'metrics': {'requests': 100, 'errors_4xx': 25}},
        'degraded_p95': {'state': 'ENABLED', 'metrics': {'requests': 100, 'latency_p95_ms': 16000}},
        'degraded_low_traffic': {'state': 'ENABLED', 'metrics': {'requests': 10, 'errors_5xx': 1}},
        'idle': {'state': 'ENABLED', 'metrics': {'requests': 0}},
        'disabled': {'state': 'DISABLED', 'metrics': {'requests': 50}},
        'broken': {'state': 'DISABLED', 'registration': {'status': 'BACKEND_NOT_FOUND'}, 'metrics': {}},
        'unknown': {'state': 'ENABLED', 'metrics': {}},
    },
    'severity': [
        {'metrics': {'requests': 1000, 'errors_5xx': 0}},
        {'metrics': {'requests': 1000, 'errors_5xx': 5}},
        {'metrics': {'requests': 1000, 'errors_5xx': 20}},
        {'metrics': {'requests': 610, 'errors_5xx': 41}},
        {'metrics': {'requests': 10, 'errors_5xx': 1}},
    ],
    'sort': ['1.2M', '$1,234.56', '1.4 s / 6.2 s', '380 ms / 2.4 s', '15 d ago', '30 min ago', '<0.1%', '-$5.00',
             '36.3M / 2.1M', 'No-code agent', 'no rate card', '\u2014', 'Not measured (Cloud Run reports requests only)',
             '2026-10-09'],
    'csv': {'headers': ['Agent', 'Cost'],
            'rows': [['=HYPERLINK("x")', '-5'], ['a,b', '+1'], ['@cmd', '-cmd'], ['say "hi"', None]]},
    'billing_days': [
        {'day': '2026-09-29', 'ai_net_usd': 50, 'interactions': 500},
        {'day': '2026-09-30', 'ai_net_usd': 50, 'interactions': 500},
        *_days(9, ai_net_usd=100, interactions=1000),
        {'day': '2026-10-10', 'ai_net_usd': None, 'interactions': 400},  # not billed yet
    ],
    'usage_days': [dict(d, active_users=100 + i, sessions=200, interactions=800, failed_turns=8)
                   for i, d in enumerate(_days(10))],
    'model_usage': {
        'models': [{'model': 'gemini-2.5-flash', 'input_tokens': 1000000, 'cache_read_tokens': 1000000},
                   {'model': 'unpriced-model', 'input_tokens': 500000, 'cache_read_tokens': 0}],
        'rate_cards': {'gemini-2.5-flash': {'input': 0.30, 'output': 2.5, 'cached_read': 0.03}},
    },
    'trend': {'bucket_ends': ['t1', 't2', 't3'], 'bucket_seconds': 3600,
              'by_runtime': {'a': {'requests': [1, 2, 3], 'errors_4xx': [0, 1, 0], 'errors_5xx': [0, 0, 1]},
                             'b': {'requests': [10, 20, 30]}}},
    'runtime_agents': [
        {'backend': {'kind': 'cloud_run', 'service': 'svc', 'url': 'https://svc-123.a.run.app'}},
        {'backend': {'kind': 'agent_engine', 'resource': 'projects/p/locations/us-central1/reasoningEngines/1'}},
        {'backend': {'kind': 'a2a', 'url': 'https://agent.example/a2a'}},
        {'backend': {'url': 'https://no-kind.example'}},
        {'resource_name': 'projects/p/locations/global/collections/c/engines/e/assistants/a/agents/42'},
    ],
    'urls': ['https://console.cloud.google.com/logs/query;query=x?project=p', 'javascript:alert(1)',
             'https://evil.example/', 'http://console.cloud.google.com/run',
             'https://console.cloud.google.com.evil.example/', 'not a url'],
    'users': ['dev.patel@acme.example', 'maria.lopez', '', None, '@x'],
    'alert_agents': {
        'engine': {'display_name': 'Code Review Copilot', 'metrics': {'requests': 610},
                   'backend': {'kind': 'agent_engine', 'project': '123456789012', 'location': 'us-central1',
                               'resource': 'projects/123456789012/locations/us-central1/reasoningEngines/1588369340892498763'}},
        'run': {'display_name': 'Sales Deal Desk', 'metrics': {'requests': 2310},
                'backend': {'kind': 'cloud_run', 'service': 'deal-desk-a2a', 'region': 'us-central1'}},
        'nocode': {'display_name': 'Knowledge Search', 'backend': {'kind': 'gemini_enterprise_hosted'}},
        'evil': {'display_name': "Evil'; rm -rf ~ $(whoami) `id`\nVIBELIFT_POLICY\nrm -rf /", 'metrics': {'requests': 5000},
                 'backend': {'kind': 'cloud_run', 'service': 'evil-svc'}},
    },
}

_HARNESS = r"""
const CASES = __CASES__;
const out = {};
out.health = {};
Object.keys(CASES.agents).forEach(function(k) {
  const r = VL.health(CASES.agents[k]);
  out.health[k] = {id: r.id, reasons: r.reasons};
});
out.healthCounts = VL.healthCounts(Object.values(CASES.agents));
out.severity = CASES.severity.map(VL.serverErrorSeverity);
out.sort = CASES.sort.map(VL.parseSortValue);
out.sortedAsc = [3, null, 1, 'b', 'a'].sort(function(a, b) { return VL.compare(a, b, 'asc'); });
out.sortedDesc = [3, null, 1].sort(function(a, b) { return VL.compare(a, b, 'desc'); });
out.csv = VL.toCsv(CASES.csv.headers, CASES.csv.rows);
out.cost = VL.costSummary(CASES.billing_days, null);
out.costOver = VL.costSummary(CASES.billing_days, 3000);
out.costAtRisk = VL.costSummary(CASES.billing_days, 3400);
out.costOnTrack = VL.costSummary(CASES.billing_days, 5000);
out.costNoBilling = VL.costSummary([{day: '2026-10-01', ai_net_usd: null}], 1000);
out.costTwoDays = VL.costSummary(CASES.billing_days.slice(0, 2), null);
out.cache = VL.cacheSavings(CASES.model_usage);
out.cacheNoCard = VL.cacheSavings({models: CASES.model_usage.models, rate_cards: {}});
out.delta = VL.spendDelta({status: 'LIVE', cost_before_usd: 104.13, cost_now_usd: 126.33, change_pct: 21.3});
out.deltaNotLive = VL.spendDelta({status: 'NOT_CONNECTED', cost_before_usd: 1, cost_now_usd: 2});
out.adoption = VL.adoption(CASES.usage_days, '2026-10-10');
out.adoptionEmpty = VL.adoption([], '2026-10-10');
out.trend = VL.trendTotals(CASES.trend, ['a', 'b', 'missing']);
out.trendMissing = VL.trendTotals(CASES.trend, ['missing']);
out.runtimeKeys = CASES.runtime_agents.map(VL.runtimeKey);
out.urls = CASES.urls.map(VL.safeExternalUrl);
out.users = CASES.users.map(VL.userLabel);
const palItems = [{title: 'Code Review Copilot', keywords: ['ADK']}, {title: 'Warranty Claims', keywords: ['No-code']},
                  {title: 'Contract Analyzer'}, {title: 'dev.patel', hideWhenEmpty: true}];
const titles = function(list) { return list.map(function(i) { return i.title; }); };
out.palette = {
  scores: [VL.paletteScore('code', 'Code Review Copilot'), VL.paletteScore('code', 'No-code'),
           VL.paletteScore('crc', 'Code Review Copilot'), VL.paletteScore('xyz', 'Code Review Copilot'),
           VL.paletteScore('review code', 'Code Review Copilot'), VL.paletteScore('', 'anything')],
  code: titles(VL.paletteFilter(palItems, 'code')),
  dev: titles(VL.paletteFilter(palItems, 'dev')),
  typo: titles(VL.paletteFilter(palItems, 'cntrct')),
  empty: titles(VL.paletteFilter(palItems, '')),
  limit: VL.paletteFilter(palItems, '', 2).length,
};
const A = CASES.alert_agents;
const pol = function(r, key) { return r.policies.filter(function(p) { return p.key === key; })[0].policy; };
const engine = VL.alertPolicies(A.engine, {project: 'acme-ge-preview', windowHours: 24});
const run = VL.alertPolicies(A.run, {project: 'acme-ge-preview', windowHours: 24, include: {errors: true, latency: false, absence: true},
                                     absenceMin: 30, channel: '987654'});
const evil = VL.alertPolicies(A.evil, {project: 'acme-ge-preview', windowHours: 24});
out.alerts = {
  engine: {ok: engine.ok, keys: engine.policies.map(function(p) { return p.key; }), windowS: engine.windowS, script: engine.script,
           e5: pol(engine, '5xx'), p95: pol(engine, 'p95'), files: engine.policies.map(function(p) { return p.file; })},
  run: {ok: run.ok, keys: run.policies.map(function(p) { return p.key; }), windowS: run.windowS, script: run.script,
        e5: pol(run, '5xx'), absent: pol(run, 'no-traffic')},
  nocode: VL.alertPolicies(A.nocode, {project: 'acme-ge-preview', windowHours: 24}),
  badProject: VL.alertPolicies(A.run, {project: 'Bad Project!', windowHours: 24}).ok,
  badChannel: VL.alertPolicies(A.run, {project: 'acme-ge-preview', windowHours: 24, channel: 'not-a-channel'}).ok,
  fullChannel: VL.alertPolicies(A.run, {project: 'acme-ge-preview', windowHours: 24,
                                        channel: 'projects/other-proj-1/notificationChannels/42'}).policies[0].policy.notificationChannels,
  badAbsence: VL.alertPolicies(A.run, {project: 'acme-ge-preview', include: {absence: true}, absenceMin: 3}).ok,
  nothing: VL.alertPolicies(A.run, {project: 'acme-ge-preview', include: {}}).ok,
  forcedWindow: VL.alertPolicies(A.engine, {project: 'x-project', windowHours: 24, windowS: 300}).windowS,
  evilScript: evil.script, evilCount: evil.policies.length,
  evilJson: evil.policies.map(function(p) {
    var lines = evil.script.split('\n');
    var start = lines.indexOf('cat > ' + p.file + " <<'VIBELIFT_POLICY'");
    var end = lines.indexOf('VIBELIFT_POLICY', start + 1);
    return JSON.parse(lines.slice(start + 1, end).join('\n')).displayName;
  }),
  auto: [VL.autoAlertWindow(4820, 24, 20), VL.autoAlertWindow(0, 24, 20), VL.autoAlertWindow(null, 24, 20),
         VL.autoAlertWindow(100000, 24, 20)],
};
out.theme = [VL.resolveTheme('dark', 'light', 'light'), VL.resolveTheme(null, 'dark', 'light'),
             VL.resolveTheme(null, null, 'dark'), VL.resolveTheme(null, null, 'light'), VL.resolveTheme('x', 'y', 'z')];
out.prompt = VL.agentPrompt({display_name: 'Code Review Copilot', engine_display_name: 'Acme Intranet Assistant',
                             metrics: CASES.agents.failing_5xx.metrics},
                            VL.health(CASES.agents.failing_5xx), 'last 24 hours');
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class DashboardEnhancementLogicTest(unittest.TestCase):
  """Runs the real enhancement module's pure helpers in Node (no DOM, so VL.mount() does not run)."""

  @classmethod
  def setUpClass(cls):
    cls.out = _run_node(_JS + '\n' + _HARNESS.replace('__CASES__', json.dumps(_CASES)))

  def test_health_levels(self):
    levels = {k: v['id'] for k, v in self.out['health'].items()}
    self.assertEqual(levels, {
        'healthy': 'healthy', 'failing_5xx': 'failing', 'failing_loop': 'failing', 'degraded_5xx': 'degraded',
        'degraded_4xx': 'degraded', 'degraded_p95': 'degraded', 'degraded_low_traffic': 'degraded',
        'idle': 'idle', 'disabled': 'disabled', 'broken': 'broken', 'unknown': 'unknown'})
    self.assertEqual(self.out['healthCounts'], {'failing': 2, 'broken': 1, 'degraded': 4, 'healthy': 1, 'idle': 1,
                                                'disabled': 1, 'unknown': 1})

  def test_health_explains_itself_with_measured_numbers(self):
    self.assertIn('6.7%', ' '.join(self.out['health']['failing_5xx']['reasons']))
    self.assertIn('44.5 LLM calls per request', ' '.join(self.out['health']['failing_loop']['reasons']))
    self.assertIn('low traffic', ' '.join(self.out['health']['degraded_low_traffic']['reasons']))
    self.assertIn('Within thresholds', ' '.join(self.out['health']['healthy']['reasons']))

  def test_server_error_severity_is_rate_based(self):
    # 0 = info, 1 = warning, 2 = critical. Low traffic with any 5xx is a warning, never critical.
    self.assertEqual(self.out['severity'], [0, 0, 1, 2, 1])

  def test_sort_values(self):
    expected_day = int(dt.datetime(2026, 10, 9, tzinfo=dt.UTC).timestamp() * 1000)
    self.assertEqual(self.out['sort'], [
        1200000, 1234.56, 1400, 380, -1296000, -1800, 0.1, -5, 36300000, 'no-code agent', None, None, None,
        expected_day])

  def test_missing_values_sort_last_in_both_directions(self):
    self.assertEqual(self.out['sortedAsc'], [1, 3, 'a', 'b', None])
    self.assertEqual(self.out['sortedDesc'], [3, 1, None])

  def test_csv_escaping_and_formula_guard(self):
    self.assertEqual(self.out['csv'],
                     'Agent,Cost\r\n'
                     '"\'=HYPERLINK(""x"")",-5\r\n'
                     '"a,b",\'+1\r\n'
                     "'@cmd,'-cmd\r\n"
                     '"say ""hi""",\r\n')

  def test_cost_summary_month_to_date_and_projection(self):
    c = self.out['cost']
    self.assertEqual(c['status'], 'OK')
    self.assertEqual((c['billedDays'], c['firstDay'], c['lastDay'], c['month']), (11, '2026-09-29', '2026-10-09', '2026-10'))
    self.assertEqual((c['total'], c['mtd'], c['mtdDays']), (1000, 900, 9))
    self.assertEqual((c['avg7'], c['avg7Days'], c['daysInMonth'], c['daysRemaining']), (100, 7, 31, 22))
    self.assertEqual(c['projection'], 900 + 100 * 22)
    self.assertAlmostEqual(c['per1kTurns7d'], 100)
    self.assertIsNone(c['budget'])
    self.assertNotIn('budgetStatus', c)

  def test_cost_summary_budget_status(self):
    over = self.out['costOver']
    self.assertAlmostEqual(over['budgetUsedPct'], 30)
    self.assertAlmostEqual(over['projectedPct'], 3100 / 3000 * 100)
    self.assertAlmostEqual(over['dailyPace'], 3000 / 31)
    self.assertEqual(over['budgetStatus'], 'over')
    self.assertEqual(self.out['costAtRisk']['budgetStatus'], 'at_risk')
    self.assertEqual(self.out['costOnTrack']['budgetStatus'], 'on_track')

  def test_cost_summary_never_invents_billed_values(self):
    self.assertEqual(self.out['costNoBilling'], {'status': 'NO_BILLING', 'billedDays': 0, 'budget': 1000})
    two = self.out['costTwoDays']
    self.assertIsNone(two['avg7'])
    self.assertIsNone(two['projection'])

  def test_cache_savings(self):
    cache = self.out['cache']
    self.assertAlmostEqual(cache['savedUsd'], 0.27)
    self.assertEqual((cache['cacheReadTokens'], cache['promptTokens']), (1000000, 2500000))
    self.assertAlmostEqual(cache['sharePct'], 40)
    self.assertIsNone(self.out['cacheNoCard']['savedUsd'])

  def test_spend_delta(self):
    self.assertAlmostEqual(self.out['delta']['change'], 22.2)
    self.assertEqual(self.out['delta']['pct'], 21.3)
    self.assertIsNone(self.out['deltaNotLive'])

  def test_adoption_uses_complete_days(self):
    a = self.out['adoption']
    self.assertEqual(a['latestDay'], '2026-10-09')
    self.assertEqual(a['activeUsersLatest'], 108)
    self.assertAlmostEqual(a['activeUsersAvg7'], 105)
    self.assertAlmostEqual(a['sessionsPerDay7'], 200)
    self.assertAlmostEqual(a['turnsPerSession7'], 4)
    self.assertAlmostEqual(a['failedRatePct7'], 1)
    self.assertEqual(a['daysCounted'], 7)
    self.assertEqual(len(a['series']), 10)
    self.assertEqual([s['partial'] for s in a['series']], [False] * 9 + [True])
    self.assertIsNone(self.out['adoptionEmpty'])

  def test_trend_totals(self):
    t = self.out['trend']
    self.assertEqual((t['total'], t['errors_4xx'], t['errors_5xx']), ([11, 22, 33], [0, 1, 0], [0, 0, 1]))
    self.assertEqual(t['bucket_seconds'], 3600)
    self.assertIsNone(self.out['trendMissing'])

  def test_runtime_key_matches_the_server(self):
    expected = [ge_fleet.runtime_backend_key(a) for a in _CASES['runtime_agents']]
    self.assertEqual(self.out['runtimeKeys'], expected)

  def test_only_https_cloud_console_links_open(self):
    self.assertEqual(self.out['urls'], [_CASES['urls'][0], None, None, None, None, None])

  def test_users_are_shown_by_ldap(self):
    self.assertEqual(self.out['users'], ['dev.patel', 'maria.lopez', '\u2014', '\u2014', '@x'])

  def test_palette_ranking(self):
    p = self.out['palette']
    prefix, word, letters, miss, words, empty = p['scores']
    self.assertGreater(prefix, word)
    self.assertGreater(word, words)
    self.assertGreater(words, letters)
    self.assertIsNone(miss)
    self.assertEqual(empty, 0)
    self.assertEqual(p['code'], ['Code Review Copilot', 'Warranty Claims'])
    self.assertEqual(p['dev'], ['dev.patel'])  # no letters-in-order noise when something matches directly
    self.assertEqual(p['typo'], ['Contract Analyzer'])
    self.assertEqual(p['empty'], ['Code Review Copilot', 'Warranty Claims', 'Contract Analyzer'])
    self.assertEqual(p['limit'], 2)

  def test_alert_policies_for_agent_engine(self):
    e = self.out['alerts']['engine']
    self.assertTrue(e['ok'])
    self.assertEqual(e['keys'], ['5xx', 'p95'])
    self.assertEqual(e['windowS'], 3600)  # 610 requests a day: only a 1-hour window reaches 20 requests
    ratio, minimum = e['e5']['conditions']
    base = ('resource.type="aiplatform.googleapis.com/ReasoningEngine" AND '
            'resource.label.reasoning_engine_id="1588369340892498763"')
    self.assertEqual(ratio['conditionThreshold']['denominatorFilter'],
                     'metric.type="aiplatform.googleapis.com/reasoning_engine/request_count" AND ' + base)
    self.assertEqual(ratio['conditionThreshold']['filter'], ratio['conditionThreshold']['denominatorFilter'] +
                     ' AND metric.label.response_code_class=starts_with("5")')
    self.assertEqual(ratio['conditionThreshold']['thresholdValue'], 0.05)
    self.assertEqual(ratio['conditionThreshold']['aggregations'],
                     [{'alignmentPeriod': '3600s', 'perSeriesAligner': 'ALIGN_DELTA', 'crossSeriesReducer': 'REDUCE_SUM',
                       'groupByFields': ['resource.label.reasoning_engine_id']}])
    self.assertEqual(ratio['conditionThreshold']['aggregations'], ratio['conditionThreshold']['denominatorAggregations'])
    self.assertEqual((minimum['conditionThreshold']['comparison'], minimum['conditionThreshold']['thresholdValue']),
                     ('COMPARISON_GE', 20))
    self.assertEqual((e['e5']['combiner'], e['e5']['severity']), ('AND', 'CRITICAL'))
    p95 = e['p95']['conditions'][0]['conditionThreshold']
    self.assertEqual(p95['filter'], 'metric.type="aiplatform.googleapis.com/reasoning_engine/request_latencies" AND ' + base)
    self.assertEqual(p95['aggregations'][0]['crossSeriesReducer'], 'REDUCE_PERCENTILE_95')
    self.assertEqual(p95['thresholdValue'], 15000)
    self.assertEqual(e['files'], ['vibelift-code-review-copilot-5xx.json', 'vibelift-code-review-copilot-p95.json'])
    self.assertIn("gcloud monitoring policies create --project='123456789012' "
                  '--policy-from-file=vibelift-code-review-copilot-5xx.json', e['script'])
    self.assertIn('# No notification channel', e['script'])

  def test_alert_policies_for_cloud_run(self):
    r = self.out['alerts']['run']
    self.assertTrue(r['ok'])
    self.assertEqual(r['keys'], ['5xx', 'no-traffic'])
    self.assertEqual(r['windowS'], 900)  # about 24 requests per 15 minutes at the observed rate
    self.assertIn('resource.type="cloud_run_revision" AND resource.label.service_name="deal-desk-a2a"',
                  r['e5']['conditions'][0]['conditionThreshold']['filter'])
    self.assertEqual(r['e5']['notificationChannels'], ['projects/acme-ge-preview/notificationChannels/987654'])
    absent = r['absent']['conditions'][0]['conditionAbsent']
    self.assertEqual(absent['duration'], '1800s')
    self.assertIn("--project='acme-ge-preview'", r['script'])
    self.assertIn('Cloud Run metrics are per service', r['script'])

  def test_alert_policies_refuse_bad_input(self):
    a = self.out['alerts']
    self.assertFalse(a['nocode']['ok'])
    self.assertIn('no per-project runtime metrics', a['nocode']['reason'])
    self.assertFalse(a['badProject'])
    self.assertFalse(a['badChannel'])
    self.assertEqual(a['fullChannel'], ['projects/other-proj-1/notificationChannels/42'])
    self.assertFalse(a['badAbsence'])
    self.assertFalse(a['nothing'])
    self.assertEqual(a['forcedWindow'], 300)

  def test_alert_script_is_shell_safe(self):
    a = self.out['alerts']
    lines = a['evilScript'].split('\n')
    # The hostile name never starts a shell line: it only appears inside comments and JSON strings.
    self.assertEqual(lines.count('VIBELIFT_POLICY'), a['evilCount'])
    for line in lines:
      self.assertFalse(line.startswith('rm'), line)
      self.assertTrue(line == '' or line.startswith(('#', 'cat > vibelift-', 'gcloud monitoring policies create', ' ', '{', '}'))
                      or line == 'VIBELIFT_POLICY', line)
    for name in a['evilJson']:
      self.assertIn("Evil'; rm -rf ~ $(whoami) `id` VIBELIFT_POLICY rm -rf /", name)

  def test_auto_alert_window(self):
    busy, idle, unknown, flood = self.out['alerts']['auto']
    self.assertEqual(busy['seconds'], 900)
    self.assertEqual((idle['seconds'], idle['perWindow']), (3600, 0))
    self.assertEqual((unknown['seconds'], unknown['perWindow']), (3600, None))
    self.assertEqual(flood['seconds'], 300)

  def test_theme_resolution_order(self):
    # Saved choice, then the MCP host theme, then the OS setting; anything unknown falls back to light.
    self.assertEqual(self.out['theme'], ['dark', 'dark', 'dark', 'light', 'light'])

  def test_investigation_prompt_quotes_measured_facts(self):
    prompt = self.out['prompt']
    self.assertIn('"Code Review Copilot" (Acme Intranet Assistant)', prompt)
    self.assertIn('VibeLift rates it Failing', prompt)
    self.assertIn('610 requests, 41 server errors (5xx), 22 rejected requests (4xx)', prompt)
    self.assertIn('last 24 hours', prompt)


if __name__ == '__main__':
  unittest.main()
