"""Standalone CLI and programmatic linter for Agent FinOps AST checks & Prompt Cache X-Ray."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from finops_dashboard import optimizer, prompt_xray

_EXCLUDED_DIRS = {
    '.git',
    '.venv',
    'venv',
    '__pycache__',
    'node_modules',
    '.mypy_cache',
    '.pytest_cache',
    '.ruff_cache',
    'build',
    'dist',
}


def discover_python_files(target_paths: list[str]) -> list[Path]:
  """Resolves files or directories into a deterministic sorted list of Python files."""
  discovered: list[Path] = []
  seen: set[Path] = set()
  for raw in target_paths:
    p = Path(raw)
    if not p.exists():
      continue
    if p.is_file() and p.suffix == '.py':
      resolved = p.resolve()
      if resolved not in seen:
        seen.add(resolved)
        discovered.append(p)
    elif p.is_dir():
      for child in sorted(p.rglob('*.py')):
        if any(part in _EXCLUDED_DIRS for part in child.parts):
          continue
        resolved = child.resolve()
        if resolved not in seen:
          seen.add(resolved)
          discovered.append(child)
  return discovered


def lint_code_and_prompts(
    *,
    paths: list[str] | None = None,
    code_snippet: str | None = None,
    code_filename: str = 'inline_agent.py',
    prev_prompt: str | None = None,
    curr_prompt: str | None = None,
    model: str = prompt_xray.DEFAULT_MODEL,
    daily_call_volume: int = 10_000,
) -> dict[str, Any]:
  """Runs AST FinOps checks (FIN-01..FIN-05) and optional Prompt Cache X-Ray diffing."""
  findings: list[dict[str, Any]] = []
  scanned_files: list[str] = []

  if code_snippet is not None and code_snippet.strip():
    scanned_files.append(code_filename)
    findings.extend(
        optimizer.scan_python_code_for_finops_findings(
            code_snippet, filename=code_filename
        )
    )

  if paths:
    for py_file in discover_python_files(paths):
      rel_str = str(py_file)
      scanned_files.append(rel_str)
      try:
        source_text = py_file.read_text(encoding='utf-8')
      except OSError:
        continue
      findings.extend(
          optimizer.scan_python_code_for_finops_findings(
              source_text, filename=rel_str
          )
      )

  xray_report: dict[str, Any] | None = None
  cache_bust_detected = False
  if prev_prompt is not None and curr_prompt is not None:
    xray_report = dict(
        prompt_xray.analyze(
            prev_prompt,
            curr_prompt,
            model=model,
            monthly_requests=max(1, int(daily_call_volume)) * 30,
        )
    )
    status = str(xray_report.get('status') or '')
    if status == prompt_xray.STATUS_BUSTED:
      cache_bust_detected = True
    xray_report.setdefault(
        'summary',
        {
            'verdict': status,
            'prefix_cache_ratio': xray_report.get('cached_prefix_pct', 0.0),
            'monthly_savings_from_fix_usd': float(
                xray_report.get('stranded_cost_usd_per_month') or 0.0
            ),
        },
    )

  high_severity_count = sum(
      1 for f in findings if str(f.get('severity') or '').upper() in ('HIGH', 'CRITICAL')
  )
  passed = len(findings) == 0 and not cache_bust_detected

  return {
      'status': 'PASS' if passed else 'FAIL',
      'passed': passed,
      'scanned_files_count': len(scanned_files),
      'scanned_files': scanned_files,
      'findings_count': len(findings),
      'high_severity_count': high_severity_count,
      'findings': findings,
      'prompt_xray': xray_report,
      'cache_bust_detected': cache_bust_detected,
  }


def _format_text_report(report: dict[str, Any]) -> str:
  lines = [
      f"FinOps Lint Verdict: {report['status']} "
      f"(files={report['scanned_files_count']}, findings={report['findings_count']})"
  ]
  for item in report.get('findings', []):
    lines.append(
        f"  [{item.get('rule_id')}] ({item.get('severity')}) "
        f"{item.get('file')}:{item.get('line')} - {item.get('title')}: {item.get('detail')}"
    )
  xray = report.get('prompt_xray')
  if isinstance(xray, dict):
    summary = xray.get('summary') or {}
    lines.append(
        f"  [PROMPT-XRAY] verdict={summary.get('verdict')} "
        f"prefix_cache_ratio={summary.get('prefix_cache_ratio')} "
        f"monthly_savings_usd=${float(summary.get('monthly_savings_from_fix_usd') or 0.0):.2f}"
    )
  return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      prog='finops-lint',
      description='Static AST FinOps Linter (FIN-01..FIN-05) & Prompt Cache X-Ray CI Gate.',
  )
  parser.add_argument(
      'paths',
      nargs='*',
      default=[],
      help='Python files or directories to scan for FinOps anti-patterns.',
  )
  parser.add_argument(
      '--code',
      default=None,
      help='Inline Python code snippet to lint.',
  )
  parser.add_argument(
      '--prev-prompt',
      default=None,
      help='Turn N-1 prompt string (or @filepath) for Prompt Cache X-Ray.',
  )
  parser.add_argument(
      '--curr-prompt',
      default=None,
      help='Turn N prompt string (or @filepath) for Prompt Cache X-Ray.',
  )
  parser.add_argument(
      '--model',
      default=prompt_xray.DEFAULT_MODEL,
      help=f'Vertex AI model for token cache pricing (default: {prompt_xray.DEFAULT_MODEL}).',
  )
  parser.add_argument(
      '--format',
      choices=('text', 'json'),
      default='text',
      help='Output format (text or json).',
  )
  parser.add_argument(
      '--fail-on-findings',
      action='store_true',
      help='Exit with code 1 if any AST finding or prompt cache bust is detected.',
  )
  args = parser.parse_args(argv)

  def _load_prompt_arg(val: str | None) -> str | None:
    if val is None:
      return None
    if val.startswith('@') and len(val) > 1:
      return Path(val[1:]).read_text(encoding='utf-8')
    return val

  report = lint_code_and_prompts(
      paths=args.paths,
      code_snippet=args.code,
      prev_prompt=_load_prompt_arg(args.prev_prompt),
      curr_prompt=_load_prompt_arg(args.curr_prompt),
      model=args.model,
  )

  if args.format == 'json':
    sys.stdout.write(json.dumps(report, indent=2) + '\n')
  else:
    sys.stdout.write(_format_text_report(report) + '\n')

  if args.fail_on_findings and not report['passed']:
    return 1
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
