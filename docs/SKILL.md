---
name: finops-analytics
description: Gemini Enterprise agent fleet observability, prompt cache economics, and static AST FinOps linting. Use this skill to query project telemetry, inspect fleet prompt cache token economics, interact with the FinOps Dashboard MCP App in Gemini Enterprise, or lint Python agent code and prompt pairs.
allowed-tools:
  - open_dashboard
  - query_project_telemetry
  - query_ge_agent_fleet
  - calculate_prompt_cache_economics
  - lint_agent_finops
  - get_finops_state
  - xray_prompt_cache
  - list_prompt_snapshot_turns
  - set_agent_trace_logging
---

# FinOps Dashboard Agent Optimization & FinOps Skill (`finops-analytics`)

## 🛠️ Tool Catalog & Pipeline Stages

| Pipeline Stage | Tool | Description & Trigger Context |
| :--- | :--- | :--- |
| **Interactive UI** | `open_dashboard` | **CRITICAL: Call FIRST** whenever the user asks to see, open, launch, or inspect the FinOps dashboard, FinOps and Tokenomics dashboard, or FinOps Analytics Platform, or asks for interactive agent telemetry in Gemini Enterprise. |
| **GCP Observability** | `query_project_telemetry` | Ingests live Cloud Run agent microservices, BigQuery support logs, and Google Cloud Logging usage metrics. |
| **GE Agent Fleet** | `query_ge_agent_fleet` | Lists every agent deployed on the Gemini Enterprise app PLUS standalone/unregistered runtimes with live requests, errors, latency, LLM calls, tokens, conversations, and last activity, plus project-wide model spend. |
| **Cost & Token Economics** | `calculate_prompt_cache_economics` | Computes prompt cache hit ratio (%) and net dollar savings ($) at list prices from the rate cards in `finops_dashboard/telemetry.py` (`RATE_CARDS`). |
| **Static AST & Prompt Cache Linter** | `lint_agent_finops` | Runs static AST FinOps rules (`FIN-01`..`FIN-05`) on Python agent code (`code_snippet` or workspace `file_path`) and optional Prompt Cache X-Ray diffing on consecutive prompts. |
| **Runtime State** | `get_finops_state` | Retrieves the complete runtime state, turn trajectories (`cache_breakpoint_line`, `cache_breakpoint_reason`), and multi-objective optimization parameters. |
| **Prompt Cache X-Ray** | `xray_prompt_cache` | Compares two prompt snapshots (text or logged turn event IDs) to pinpoint the exact character and line where the cacheable prefix breaks, stranded tokens, and an AlphaEvolve-reordered prompt. |
| **Logged Prompt Snapshots** | `list_prompt_snapshot_turns` | Lists recent OTel GenAI turns whose system instruction and input messages were logged to Cloud Storage (`FINOPS_PROMPT_XRAY_LIVE=1`). |
| **Trace Logging Control** | `set_agent_trace_logging` | Enables or disables Discovery Engine `observabilityConfig` trace logging on a single Gemini Enterprise agent or across the registered fleet (requires user confirmation). |

---

## 🏛️ Operating Workflows

### 1. Gemini Enterprise BYO MCP App Workflow
1. When asked to open or view the dashboard:
   - Call `open_dashboard()`; it opens on the live Gemini Enterprise agent fleet (`focus_tab=1` or `2` opens the optimization tabs).
   - The interactive glassmorphic UI loads inside Gemini Enterprise's side panel or expands to fullscreen via AppBridge.
2. When the user requests a live telemetry refresh:
   - Call `query_project_telemetry(force_refresh=True)`.
   - Updated logs and turns flow into the AppBridge via `ui/notifications/tool-result`.

### 2. Gemini Enterprise Agent Fleet Audit Workflow
1. Call `query_ge_agent_fleet(window_hours=24)` (use `168` for a weekly view).
2. Report per agent: requests, 4xx/5xx errors, p50/p95 latency, LLM calls, input/output tokens, conversations, and last activity. Treat `null` as "source unavailable", never as zero.
3. Report project-wide Vertex AI model usage and estimated list-price spend, naming any models without a rate card.
4. Flag agents with 5xx errors or high p95 latency and recommend prompt-cache optimizations where cache reads are low.

### 3. Prompt cache fixes and static FinOps linting
1. For real findings, call `get_finops_state` and read each turn's `cache_breakpoint_line` / `cache_breakpoint_reason` to find the prompt prefix that changes between turns, or call `list_prompt_snapshot_turns` + `xray_prompt_cache` to inspect two logged turns.
2. Recommend the fix yourself (for example, move the changing text after the static prefix) or run `xray_prompt_cache` / `lint_agent_finops`.
3. Call `lint_agent_finops` to check Python agent code for `FIN-01`..`FIN-05` anti-patterns and prompt cache busting before deployment.
