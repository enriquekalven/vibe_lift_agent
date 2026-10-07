"""Checks that the documentation's facts match the code, so the docs cannot silently drift.

Every number, link, file path, endpoint and environment variable that the README and the docs
state is recomputed here from the code or the repository. A failing test names the doc and the
claim to fix.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from deploy.bigquery import verify_ge_mart_invariants
from vibelift import mcp_server, sme_eval, telemetry
from vibelift.ui import template as ui_template

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / 'README.md'
ARCH = ROOT / 'docs' / 'ARCHITECTURE.md'
SUBMISSION = ROOT / 'docs' / 'HACKATHON_SUBMISSION.md'
TESTING = ROOT / 'docs' / 'TESTING.md'
DOCS = (README, ARCH, SUBMISSION, TESTING)
REPO_URL = 'https://github.com/enriquekalven/vibe_lift_agent'


def _read(path: Path) -> str:
  return path.read_text(encoding='utf-8')


def _github_slug(heading: str) -> str:
  """GitHub's heading anchor: lowercase, drop punctuation except '-' and '_', spaces become '-'."""
  text = re.sub(r'[`*_]{1,2}(.+?)[`*_]{1,2}', r'\1', heading.strip())  # inline markup
  text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)  # links keep their text
  text = text.lower()
  text = re.sub(r'[^\w\- ]', '', text)
  return text.replace(' ', '-')


def _anchors(path: Path) -> set[str]:
  """All heading anchors of a markdown file, with GitHub's -1, -2 suffixes for duplicates."""
  seen: dict[str, int] = {}
  out: set[str] = set()
  in_code = False
  for line in _read(path).splitlines():
    if line.startswith('```'):
      in_code = not in_code
      continue
    m = re.match(r'^(#{1,6})\s+(.*)$', line)
    if in_code or not m:
      continue
    slug = _github_slug(m.group(2))
    n = seen.get(slug, 0)
    out.add(slug if n == 0 else f'{slug}-{n}')
    seen[slug] = n + 1
  return out


def _links(text: str) -> list[str]:
  """Markdown link targets outside fenced code blocks."""
  text = re.sub(r'```.*?```', '', text, flags=re.S)
  return re.findall(r'\]\(([^)\s]+)\)', text)


def _mermaid(text: str) -> str:
  m = re.search(r'```mermaid\n.*?\n```\n', text, flags=re.S)
  assert m, 'no mermaid block'
  return m.group(0)


class DiagramTest(unittest.TestCase):

  def test_readme_diagram_is_the_architecture_diagram(self) -> None:
    readme = _read(README)
    begin = readme.index('<!-- BEGIN ARCHITECTURE DIAGRAM')
    end = readme.index('<!-- END ARCHITECTURE DIAGRAM -->')
    self.assertEqual(_mermaid(readme[begin:end]), _mermaid(_read(ARCH)),
                     'README diagram differs from docs/ARCHITECTURE.md; copy the mermaid block across')

  def test_diagram_names_real_files(self) -> None:
    for path in re.findall(r'((?:app|vibelift|deploy)/[\w/.-]+\.(?:py|sh))', _mermaid(_read(ARCH))):
      with self.subTest(path=path):
        self.assertTrue((ROOT / path).is_file(), f'diagram names {path}, which does not exist')


class LinkTest(unittest.TestCase):

  def test_relative_links_and_anchors_resolve(self) -> None:
    for doc in DOCS:
      for target in _links(_read(doc)):
        if target.startswith(('http://', 'https://', 'mailto:')):
          continue
        path_part, _, anchor = target.partition('#')
        dest = (doc.parent / path_part).resolve() if path_part else doc
        with self.subTest(doc=doc.name, target=target):
          self.assertTrue(dest.exists(), f'{doc.name} links to missing {path_part}')
          if anchor and dest.suffix == '.md':
            self.assertIn(anchor, _anchors(dest), f'{doc.name}: no heading for #{anchor} in {dest.name}')

  def test_submission_urls_point_at_this_repository(self) -> None:
    text = _read(SUBMISSION)
    blob_paths = re.findall(re.escape(REPO_URL) + r'/blob/main/([\w/.-]+)', text)
    self.assertTrue(blob_paths, 'arch_url should be an absolute GitHub URL')
    for path in blob_paths:
      with self.subTest(path=path):
        self.assertTrue((ROOT / path).is_file(), f'{path} does not exist')
    readme_anchors = _anchors(README)
    repo_anchors = re.findall(re.escape(REPO_URL) + r'#([\w-]+)', text)
    self.assertTrue(repo_anchors, 'code_base_url should link to the README deploy section')
    for anchor in repo_anchors:
      with self.subTest(anchor=anchor):
        self.assertIn(anchor, readme_anchors)


class RepoTreeTest(unittest.TestCase):

  def test_every_path_in_the_readme_tree_exists(self) -> None:
    readme = _read(README)
    block = readme[readme.index('## Repository Structure'):]
    block = block[block.index('```') + 3:]
    block = block[:block.index('```')]
    stack: list[str] = []
    checked = 0
    for line in block.splitlines():
      m = re.match(r'^((?:│   |    )*)(?:├── |└── )([^#]+?)\s*(?:#.*)?$', line)
      if not m:
        continue
      depth = len(m.group(1)) // 4
      names = [n.strip().rstrip('/') for n in m.group(2).split(',') if n.strip()]
      for name in names:
        rel = '/'.join([*stack[:depth], name])
        with self.subTest(path=rel):
          self.assertTrue((ROOT / rel).exists(), f'README tree lists {rel}, which does not exist')
        checked += 1
      stack = [*stack[:depth], names[-1]]
    self.assertGreater(checked, 20)


class CountsTest(unittest.TestCase):
  """Numbers stated in the docs, recomputed from the code."""

  def _assert_all(self, pattern: str, expected: int, docs=DOCS) -> None:
    found = 0
    for doc in docs:
      for value in re.findall(pattern, _read(doc)):
        found += 1
        with self.subTest(doc=doc.name, pattern=pattern):
          self.assertEqual(int(value), expected, f'{doc.name} says {value}, the code has {expected}')
    self.assertGreater(found, 0, f'no doc states {pattern!r}; update the test or the docs')

  def test_mcp_tool_count(self) -> None:
    self._assert_all(r'\b(\d+) tools\b(?! that call)', len(mcp_server._TOOLS), docs=(README, ARCH, SUBMISSION))

  def test_adk_tool_count(self) -> None:
    from app import agent as adk_agent
    self._assert_all(r'\b(\d+) tools that call the same controller', len(adk_agent.root_agent.tools), docs=(ARCH,))

  def test_invariant_count(self) -> None:
    sql = verify_ge_mart_invariants.build_invariant_sql('proj-a', 'curated_ds', 'mart_ds')
    names = re.findall(r"'(\w+)' AS name", sql)
    self.assertEqual(len(names), len(set(names)))
    self._assert_all(r'\b(\d+) conservation', len(names), docs=(README, ARCH))

  def test_sme_check_count(self) -> None:
    personas, dims = len(sme_eval.PERSONA_SPECS), len(sme_eval.RUBRIC_DIMENSIONS)
    self._assert_all(r'\b(\d+)-Persona', personas, docs=(README,))
    self._assert_all(r'\b(\d+)-Dimension', dims, docs=(README,))
    self._assert_all(r'\((\d+)-check\)', personas * dims, docs=(README,))

  def test_tab_counts(self) -> None:
    html = ui_template.render_dashboard_html()
    buttons = re.findall(r'<button id="tabBtn\d+" class="([^"]*)"', html)
    advanced = sum(1 for cls in buttons if 'adv-only' in cls.split())
    self._assert_all(r'\b(\d+) basic tabs', len(buttons) - advanced, docs=(README, ARCH))
    self._assert_all(r'\b(\d+) advanced tabs', advanced, docs=(README, ARCH))

  def test_rate_card_models_listed_in_readme(self) -> None:
    readme = _read(README)
    line = next(ln for ln in readme.splitlines() if ln.startswith('- **Token cost estimates**'))
    listed = set(re.findall(r'`((?:gemini|claude)-[\w.-]+)`', line))
    self.assertEqual(listed, set(telemetry.RATE_CARDS), 'README rate-card list differs from RATE_CARDS')

  def test_testing_doc_counts(self) -> None:
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    files = sorted((ROOT / 'tests').glob('test_*.py'))
    m = re.search(r'^(\d+) tests across (\d+) files', _read(TESTING), flags=re.M)
    self.assertIsNotNone(m, 'docs/TESTING.md should start with "N tests across M files"')
    self.assertEqual(int(m.group(1)), suite.countTestCases(), 'update the test count in docs/TESTING.md')
    self.assertEqual(int(m.group(2)), len(files), 'update the file count in docs/TESTING.md')


class ConfigAndEndpointTest(unittest.TestCase):

  def _code_text(self) -> str:
    parts = []
    for folder in ('vibelift', 'app', 'deploy'):
      for path in (ROOT / folder).rglob('*'):
        if path.suffix in ('.py', '.sh', '.yaml', '.yml') and '__pycache__' not in path.parts:
          parts.append(path.read_text(encoding='utf-8', errors='replace'))
    parts.append(_read(ROOT / 'Makefile'))
    return '\n'.join(parts)

  def test_every_documented_env_var_is_read_by_the_code(self) -> None:
    code = self._code_text()
    for doc in DOCS:
      for var in sorted(set(re.findall(r'\b(VIBELIFT_[A-Z0-9_]+)\b', _read(doc)))):
        with self.subTest(doc=doc.name, var=var):
          self.assertIn(var, code, f'{doc.name} documents {var}, but no code reads it')

  def test_every_documented_endpoint_is_routed(self) -> None:
    from app import fast_api_app
    routes = {getattr(r, 'path', None) for r in fast_api_app.app.routes}
    readme = _read(README)
    table = readme[readme.index('## REST API & MCP Endpoint Reference'):readme.index('## Security controls and known gaps')]
    paths = set(re.findall(r'^\| `(/[\w/.\-]+)`', table, flags=re.M))
    paths |= set(re.findall(r'^\| `[^`]+`, `(/[\w/.\-]+)`', table, flags=re.M))
    self.assertGreater(len(paths), 15)
    for path in sorted(paths):
      with self.subTest(path=path):
        self.assertIn(path, routes, f'README lists {path}, which the production app (app/fast_api_app.py) does not route')


if __name__ == '__main__':
  unittest.main()
