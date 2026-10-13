"""Tests for initial_program.py in prompt_cache_evolve."""

from initial_program import EVAL_INPUTS, evaluate, propose_cache_friendly_rewrite, solve


def test_propose_cache_friendly_rewrite_returns_contract():
  """propose_cache_friendly_rewrite() returns expected keys and types."""
  sample = (
      "Request-ID: 11111111-2222-3333-4444-555555555555\n"
      "Timestamp: 2026-10-06T22:00:00Z\n"
      '{"z_key": 1, "a_key": 2}\n'
      "You are the enterprise SRE triage assistant. Follow all runbooks."
  )
  res = propose_cache_friendly_rewrite(sample)
  assert isinstance(res, dict)
  assert "rewritten_prompt" in res
  assert "changed" in res
  assert "moved_lines" in res
  assert "json_lines_key_sorted" in res
  assert isinstance(res["rewritten_prompt"], str)
  assert res["changed"] is True
  # Lossless check: key static and dynamic tokens remain present
  assert "11111111-2222-3333-4444-555555555555" in res["rewritten_prompt"]
  assert "You are the enterprise SRE triage assistant." in res["rewritten_prompt"]


def test_solve_returns_valid_output():
  """solve() returns per-case benchmark outputs."""
  out = solve(EVAL_INPUTS)
  assert isinstance(out, dict)
  assert "cases" in out
  assert len(out["cases"]) == EVAL_INPUTS["benchmark_cases"]


def test_evaluate_returns_dict_with_metric():
  """evaluate() returns a dict containing composite_cache_savings_score."""
  result = evaluate(EVAL_INPUTS)
  assert "composite_cache_savings_score" in result


def test_evaluate_returns_finite_score():
  """evaluate() returns a finite numeric score in (0, 1] for the program."""
  result = evaluate(EVAL_INPUTS)
  score = result["composite_cache_savings_score"]
  assert isinstance(score, (int, float))
  assert 0.0 < score <= 1.0
