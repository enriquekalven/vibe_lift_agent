# Evolutionary Prompt Cache Prefix Canonicalization & Reordering

## Problem Statement

Gemini 2.5 and Gemini 3.x implicit and explicit context caching reuse only a **byte-identical leading prefix** across consecutive turns. In enterprise agent deployments (such as `project-maui`, where measured prompt cache hit rate across 194 turns and 20.18M input tokens was `37.5%`), volatile per-request values injected near the top of a system prompt or tool schema invalidate the entire downstream static prefix.

Given an input prompt string `prompt: str`, `propose_cache_friendly_rewrite(prompt: str) -> dict[str, Any]` must canonicalize and reorder the prompt so that:
1. All static instructions, runbooks, and static JSON schema fields remain in a deterministic, byte-stable leading prefix.
2. All per-request volatile lines and volatile JSON fields (ISO-8601 timestamps, UUIDs, user emails, calendar dates, clock times, Unix epochs, hex trace IDs, and dynamic field labels) are moved to the trailing `### Dynamic context (moved last so the static prefix above stays cacheable)` section.
3. Single-line JSON dicts, unfenced multi-line JSON objects, and fenced ```` ```json ... ``` ```` blocks have their keys deterministically sorted (`sort_keys=True`), and mixed JSON objects containing both large static fields and volatile keys (`request_id`, `timestamp`, `session_id`, `trace_id`, `user_email`) are partitioned into a static leading JSON object and a dynamic trailing JSON object.
4. Trailing horizontal whitespace per line is normalized without losing any alphanumeric or identifier token.

## Formal Specification

Let $(P_{t-1}, P_t)$ be consecutive prompt snapshots for turn $t-1$ and turn $t$, and let $R(P)$ be the rewritten prompt returned by `propose_cache_friendly_rewrite(P)["rewritten_prompt"]`.

- **Cached Prefix Ratio:**
  $$\rho(P_{t-1}, P_t) = \frac{\text{LCP}\big(R(P_{t-1}),\, R(P_t)\big)}{\max\big(1,\, |R(P_t)|\big)}$$
  where $\text{LCP}(a, b)$ is the byte-identical longest common prefix length.
- **Lossless Token Constraint:**
  Every alphanumeric/identifier token $w \in \text{Tokens}(P)$ must appear in $\text{Tokens}(R(P))$. Any violation forces the score to `0.0`.
- **Idempotency Constraint:**
  $R(R(P)) = R(P)$.
- **Composite Objective Function (`maximize`):**
  $$S = 0.60 \cdot \bar{\rho} + 0.30 \cdot \bar{c} + 0.10 \cdot \bar{I} \in [0.0, 1.0]$$

## Evaluation

- **Metric:** `composite_cache_savings_score` (`maximize`)
- **Strategy:** `COMPOSITE_MULTI_OBJECTIVE` across 12 multi-turn enterprise benchmark pairs (`BENCHMARK_PAIRS`).
- **Inputs:** `{"benchmark_cases": 12, "model": "gemini-3.5-flash"}`

## Solution Guidance

- Preserve the exact return dictionary contract of `propose_cache_friendly_rewrite(prompt)`: `{"rewritten_prompt": str, "changed": bool, "moved_lines": list[dict], "json_lines_key_sorted": int}`.
- Partition mixed single-line and multi-line JSON dictionaries recursively so large static schemas are not dragged into the dynamic footer by a single `request_id` or `timestamp` field.
- Ensure `DYNAMIC_SECTION_HEADER` is recognized on re-entry so repeated calls are strictly idempotent.
