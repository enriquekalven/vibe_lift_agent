---
name: vibelift-analytics
description: Google Cloud Agent Optimization, Prompt Cache Economics, Gemini Enterprise agent fleet observability, and AlphaEvolve Studio. Use this skill to query project telemetry, inspect fleet prompt cache token economics, interact with the VibeLift MCP App in Gemini Enterprise, or drive closed-loop evolutionary optimization for long-running ADK agents.
allowed-tools:
  - open_dashboard
  - query_project_telemetry
  - query_ge_agent_fleet
  - calculate_prompt_cache_economics
  - run_alpha_evolve_generation
  - get_vibelift_state
---

# VibeLift Agent Optimization & FinOps Skill (`vibelift-analytics`)

## 🛠️ Tool Catalog & Pipeline Stages

| Pipeline Stage | Tool | Description & Trigger Context |
| :--- | :--- | :--- |
| **Interactive UI** | `open_dashboard` | **CRITICAL: Call FIRST** whenever the user asks to see, open, or inspect the VibeLift Analytics Platform, or asks for interactive agent telemetry in Gemini Enterprise. |
| **GCP Observability** | `query_project_telemetry` | Ingests live Cloud Run agent microservices, BigQuery support logs, and Google Cloud Logging usage metrics. |
| **GE Agent Fleet** | `query_ge_agent_fleet` | Lists every agent deployed on the Gemini Enterprise app with live requests, errors, latency, LLM calls, tokens, conversations, and last activity, plus project-wide model spend. |
| **Cost & Token Economics** | `calculate_prompt_cache_economics` | Computes prompt cache hit ratio (%) and net dollar savings ($) against official Gemini 2.5/3.1 rate cards. |
| **Prompt Engineering** | `get_vibelift_state` | Each turn carries `cache_breakpoint_line` and `cache_breakpoint_reason`, the line and mutation cause breaking prompt cache prefix matching between agent turns. (`detect_prompt_breakpoint` exists only on the ADK agent API, not on the MCP server.) |
| **Evolutionary Tuning** | `run_alpha_evolve_generation` | Runs multi-objective Pareto evolutionary search (reordering static prefixes, pruning tool schemas, guardrail rollbacks). |
| **Runtime State** | `get_vibelift_state` | Retrieves the complete runtime state, turn trajectories, and multi-objective optimization parameters. |

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

### 3. AlphaEvolve Multi-Objective Optimization Loop
1. Call `get_vibelift_state` to inspect baseline composite score and Pareto parameters.
2. If an anomaly or low cache hit ratio is detected:
   - Read the per-turn `cache_breakpoint_line` / `cache_breakpoint_reason` from `get_vibelift_state` to locate non-deterministic prefixes.
   - Trigger `run_alpha_evolve_generation` to evolve the prompt prefix and schema layout.
   - Verify that safety and accuracy guardrails remain above threshold targets.
