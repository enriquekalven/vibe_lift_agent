"""CLI-compatible evaluator for Evolutionary Prompt Cache Prefix Canonicalization & Reordering."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import signal
import sys
import traceback
from typing import Any

METRIC_NAME = "composite_cache_savings_score"
EVAL_INPUTS: dict[str, Any] = {
    "benchmark_cases": 12,
    "model": "gemini-3.5-flash",
}


class _TimeoutError(Exception):
  """Raised when candidate program execution exceeds timeout_seconds."""


def _failure(message: str, tb: str | None = None, insights: list[dict[str, str]] | None = None) -> dict[str, Any]:
  out_insights = list(insights or [])
  out_insights.append({"label": "error", "text": str(message)})
  if tb:
    out_insights.append({"label": "traceback", "text": str(tb)})
  return {"score": None, "insights": out_insights}


def evaluate_program(code: str, timeout_seconds: int = 30) -> dict[str, Any]:
  """Execute candidate code and return the evaluation result.

  Args:
    code: Python source code of the candidate program.
    timeout_seconds: Max seconds before kill.

  Returns:
    A dict with keys:
      - "score": float on success, None on failure.
      - "insights": list of {"label": str, "text": str} dicts.
  """
  stdout_buf = io.StringIO()
  stderr_buf = io.StringIO()
  insights: list[dict[str, str]] = []

  def _alarm_handler(_signum: int, _frame: Any) -> None:
    raise _TimeoutError(f"Execution timed out after {timeout_seconds}s")

  prev_handler = None
  if hasattr(signal, "alarm") and hasattr(signal, "SIGALRM"):
    prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(max(1, int(timeout_seconds)))

  try:
    namespace: dict[str, Any] = {"__name__": "__evolve_candidate__"}
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
      exec(compile(code, "<candidate_program>", "exec"), namespace)
      evaluate_fn = namespace.get("evaluate")
      if not callable(evaluate_fn):
        return _failure("Candidate program does not define a callable evaluate(eval_inputs)")
      raw_result = evaluate_fn( dict(EVAL_INPUTS) )
      solve_fn = namespace.get("solve")
      solve_summary = solve_fn(dict(EVAL_INPUTS)) if callable(solve_fn) else None
  except Exception as exc:  # pylint: disable=broad-except
    out_txt = stdout_buf.getvalue()
    err_txt = stderr_buf.getvalue()
    if out_txt:
      insights.append({"label": "stdout", "text": out_txt})
    if err_txt:
      insights.append({"label": "stderr", "text": err_txt})
    return _failure(str(exc), tb=traceback.format_exc(), insights=insights)
  finally:
    if hasattr(signal, "alarm") and hasattr(signal, "SIGALRM"):
      signal.alarm(0)
      if prev_handler is not None:
        signal.signal(signal.SIGALRM, prev_handler)

  out_txt = stdout_buf.getvalue()
  err_txt = stderr_buf.getvalue()
  if out_txt:
    insights.append({"label": "stdout", "text": out_txt})
  if err_txt:
    insights.append({"label": "stderr", "text": err_txt})

  if not isinstance(raw_result, dict) or METRIC_NAME not in raw_result:
    return _failure(f"evaluate() did not return dict with key {METRIC_NAME!r}", insights=insights)

  raw_score = raw_result.get(METRIC_NAME)
  if raw_score is None or isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
    return _failure(f"Invalid score value: {raw_score!r}", insights=insights)

  score_float = float(raw_score)
  if math.isnan(score_float) or math.isinf(score_float):
    return _failure(f"Non-finite score: {raw_score}", insights=insights)

  if isinstance(solve_summary, dict) and isinstance(solve_summary.get("cases"), list):
    insights.append({
        "label": "benchmark_cases",
        "text": json.dumps(solve_summary["cases"], indent=2),
    })

  return {"score": score_float, "insights": insights}


def main(argv: list[str] | None = None) -> int:
  """CLI entry point called by the ae CLI."""
  parser = argparse.ArgumentParser(description="AlphaEvolve evaluator for prompt_cache_evolve")
  parser.add_argument("--output-file", required=True, help="Path to write JSON evaluation result")
  parser.add_argument("--program-dir", required=True, help="Directory containing initial_program.py")
  args = parser.parse_args(argv)

  program_dir = os.path.abspath(args.program_dir)
  if program_dir not in sys.path:
    sys.path.insert(0, program_dir)

  program_path = os.path.join(program_dir, "initial_program.py")
  if not os.path.exists(program_path):
    res = _failure(f"initial_program.py not found in {program_dir}")
  else:
    with open(program_path, encoding="utf-8") as fh:
      code = fh.read()
    res = evaluate_program(code)

  with open(args.output_file, "w", encoding="utf-8") as out_fh:
    json.dump(res, out_fh, indent=2)
  return 0


if __name__ == "__main__":
  sys.exit(main())
