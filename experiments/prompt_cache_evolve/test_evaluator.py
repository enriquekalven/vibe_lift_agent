"""Tests for evaluator.py in prompt_cache_evolve."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from evaluator import evaluate_program

INITIAL_CODE = (pathlib.Path(__file__).parent / "initial_program.py").read_text(encoding="utf-8")


def test_evaluate_program_returns_score_and_insights():
  """evaluate_program() returns a dict with finite positive score and insights."""
  result = evaluate_program(INITIAL_CODE)
  assert isinstance(result["score"], float)
  assert result["score"] > 0.9
  assert isinstance(result["insights"], list)


def test_evaluate_program_returns_error_insights_on_failure():
  """evaluate_program() returns error insights for invalid Python code."""
  result = evaluate_program("def !!!")
  assert result["score"] is None
  labels = {i["label"] for i in result["insights"]}
  assert "error" in labels
  assert "traceback" in labels


def test_evaluate_program_guards_non_finite_scores():
  """evaluate_program() rejects NaN and Inf scores."""
  nan_code = 'def evaluate(ei): return {"composite_cache_savings_score": float("nan")}'
  res_nan = evaluate_program(nan_code)
  assert res_nan["score"] is None
  labels = {i["label"] for i in res_nan["insights"]}
  assert "error" in labels


def test_evaluate_program_captures_stdout():
  """stdout from the program is captured as an insight."""
  code = 'print("hello evolve")\ndef evaluate(ei): return {"composite_cache_savings_score": 0.95}'
  result = evaluate_program(code)
  stdout = [i for i in result["insights"] if i["label"] == "stdout"]
  assert len(stdout) == 1
  assert "hello evolve" in stdout[0]["text"]


def test_cli_main_writes_output_file():
  """main() writes a valid JSON output file via --output-file and --program-dir."""
  tmpdir = tempfile.mkdtemp()
  try:
    here = pathlib.Path(__file__).parent
    shutil.copy(here / "initial_program.py", os.path.join(tmpdir, "initial_program.py"))
    shutil.copy(here / "evaluator.py", os.path.join(tmpdir, "evaluator.py"))
    output_file = os.path.join(tmpdir, "scores.json")

    cmd = [
        sys.executable,
        "evaluator.py",
        "--output-file",
        output_file,
        "--program-dir",
        tmpdir,
    ]
    result = subprocess.run(
        cmd,
        cwd=tmpdir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    with open(output_file, encoding="utf-8") as f:
      data = json.load(f)
    assert isinstance(data["score"], (int, float))
    assert "insights" in data
  finally:
    shutil.rmtree(tmpdir)
