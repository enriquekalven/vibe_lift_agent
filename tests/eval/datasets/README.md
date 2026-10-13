# FinOps Dashboard Agent Evaluation Suite (`agents-cli eval`)

This directory contains the end-to-end ADK behavioral and trajectory evaluation suite for `app/agent.py` (`finops_agent`), built for `google-agents-cli` (`agents-cli eval`).

## Evaluation Cases (`datasets/basic-dataset.json` — 11 Cases, 10/10 ADK Tools)

| `eval_case_id` | Expected ADK Tool | What It Verifies |
|---|---|---|
| `open_finops_dashboard` | `open_dashboard` | Calls `open_dashboard` first when asked to open the FinOps dashboard and focuses Tab 3 (`Cost`) for `it_service_desk`. |
| `cache_economics_priced` | `calculate_cache_economics` | Computes list-price token cache savings for `gemini-2.5-flash` (`100,000` prompt / `80,000` cached) and quotes exact figures (`80%` hit ratio, `$0.0216` saved). |
| `cache_economics_unpriced_model` | `calculate_cache_economics` | Passes an unknown model (`gemini-99-experimental`), receives `priced: false`, and refuses to invent any dollar cost. |
| `prompt_cache_xray_breakpoint` | `xray_prompt_cache` | Runs Prompt Cache X-Ray across two turn snapshots and identifies the dynamic timestamp cache-buster at line 2. |
| `detect_breakpoint_line` | `detect_prompt_breakpoint` | Pinpoints the exact line and reason where prefix caching broke (`Request ID: req-101` vs `req-202` at line 3). |
| `optimizer_simulator_honesty` | `trigger_alpha_evolve_cycle` | Runs one optimizer simulator generation and explicitly discloses that the numbers are synthetic/simulated and nothing is deployed. |
| `telemetry_grounding_audit` | `validate_telemetry_grounding` | Executes deterministic telemetry grounding checks (`run_llm_judge=False`) and summarizes provenance status. |
| `fleet_inventory_24h` | `query_ge_agent_fleet` | Queries the 24-hour Gemini Enterprise agent fleet inventory and unregistered runtimes and reports observed counts. |
| `live_finops_mart_reconciliation` | `query_live_finops_and_mart` | Queries the BigQuery `finops_mart` reporting mart and Cloud Billing reconciliation for a 7-day window. |
| `discover_cloud_run_services` | `list_cloud_run_agents` | Discovers and lists deployed Cloud Run AI agent services in the hosting GCP project. |
| `gcp_telemetry_summary_24h` | `query_gcp_telemetry` | Fetches the last 24 hours of GCP telemetry logs and reports project ID, active services, and turn statistics. |

## Metrics (`eval_config.yaml`)

1. **`finops_tool_trajectory`** (`finops_metrics.py`): Deterministic `0.0–1.0` score verifying that the expected ADK tool(s) were called in the right order without tool error payloads.
2. **`finops_grounding_contract`** (`grounding_metric.py`): Deterministic `0.0–1.0` score enforcing FinOps Dashboard's zero-invented-numbers rule, exact cache-math quoting, breakpoint line citation, and mandatory simulator disclosure.
3. **`custom_response_quality`** (`response_quality.py`): `1–5` LLM-as-a-Judge score (`gemini-3.8-flash` at `temperature=0`) grading accuracy, relevance, clarity, and agreement with the reference answer and tool trace.
4. **`agent_turn_count`**: Diagnostic metric reporting turn count per case.

## Running Evaluations

```bash
# Run against the deployed Cloud Run ADK service (generate traces + grade)
make eval

# Or run the two stages explicitly:
TOKEN=$(gcloud auth print-identity-token)
agents-cli eval generate \
  --url https://finops-dashboard-mcp-app-697625214430.us-central1.run.app \
  --app-name app \
  -H "Authorization: Bearer $TOKEN"

agents-cli eval grade --config tests/eval/eval_config.yaml

# Compare two graded runs:
agents-cli eval compare artifacts/grade_results/results_<prev>.json artifacts/grade_results/results_<new>.json
```
