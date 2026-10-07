# Evolutionary Prompt Cache Prefix Canonicalization & Reordering (`prompt_cache_evolve`)

This AlphaEvolve experiment evolves `propose_cache_friendly_rewrite(prompt: str) -> dict[str, Any]` from [`vibelift/prompt_xray.py`](../../vibelift/prompt_xray.py) to maximize the shared byte-identical cached prefix across consecutive multi-turn Gemini Enterprise prompts while strictly preserving 100% of token content and idempotency.

Across the 12-case multi-turn enterprise benchmark suite (`BENCHMARK_PAIRS`), evolving the baseline single-line volatile-line mover (`score = 0.732105`) to include multi-line JSON block canonicalization, recursive static/volatile JSON key partitioning, and horizontal whitespace normalization raises `composite_cache_savings_score` to **`0.995733` (+36.0% relative lift)**.

## Files

| File | Purpose |
|---|---|
| `initial_program.py` | Standalone program with `# EVOLVE-BLOCK-START` / `# EVOLVE-BLOCK-END` and `# ORIGIN` provenance |
| `evaluator.py` | CLI-compatible evaluator for the `ae` CLI (`--output-file`, `--program-dir`) |
| `problem_description.md` | Formal specification and objective function used in LLM evolution prompts |
| `test_program.py` | Pytest suite verifying program contract, lossless preservation, and finite scoring |
| `test_evaluator.py` | Pytest suite verifying evaluator CLI, stdout capture, and NaN/Inf guards |
| `example_evaluation.json` | Verified evaluator output (`score = 0.995733`) |
| `.evolve/experiment_description.json` | Phase 1 `ExperimentDescription` specification |
| `.evolve/source_map.json` | Provenance mapping back to `vibelift/prompt_xray.py` |
| `pyproject.toml` | `uv` project configuration |

## Running Tests

```bash
uv sync
uv run pytest -v
```

## Metric

- **Name:** `composite_cache_savings_score`
- **Direction:** `maximize`
- **Evaluation strategy:** `COMPOSITE_MULTI_OBJECTIVE` (`0.60 * cached_prefix_ratio + 0.30 * cost_reduction_ratio + 0.10 * idempotency`)
