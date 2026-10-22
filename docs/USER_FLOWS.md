# User and Operator Flows

End-to-end user, operator, and automated scheduler journeys across the FinOps & Tokenomics Analytics Platform (`finops_dashboard/`, `app/`). Every route, MCP tool, and DOM control referenced below is verified against the codebase.

## 1. Gemini Enterprise Assistant & Side-Panel MCP App Flow

1. **Launch:** A user in Gemini Enterprise asks to open the FinOps dashboard. The assistant invokes `open_dashboard` on `/mcp` ([finops_dashboard/mcp_server.py](../finops_dashboard/mcp_server.py)).
2. **Render:** `open_dashboard` returns the `ui://finops-analytics/dashboard` HTML resource ([finops_dashboard/ui/template.py](../finops_dashboard/ui/template.py)) and an initial state snapshot (`structuredContent`, including compact `cost_governance` and the 5-pillar `governance_score`).
3. **Host Bridge Refresh & Read-Only Governance:** Inside the sandboxed iframe, clicking **Refresh** or changing the telemetry window (`#fleetWindowSelect`, persisted to `localStorage.getItem('finops.fleetWindowHours')`) calls `query_ge_agent_fleet` or `query_project_telemetry` over the MCP AppBridge (`window.parent.postMessage`). Clicking **Evaluate Idle Runtimes (Dry-Run)** or **Preview Weekly Digest** calls `get_finops_state` (`include_executive_digest: true`) over the MCP host bridge, while state-mutating governance actions (**Warn Creators**, **Keep Alive 7d**, **Run Clearance**) display a notice directing operators to the standalone IAP-protected console (`finops-tokenomics-dashboard`).
4. **Trace Logging Toggle:** Clicking **Enable / Disable** on an agent row asks for inline confirmation and invokes `set_agent_trace_logging` (`POST /api/enable_agent_observability` in standalone mode), which updates Discovery Engine `observabilityConfig` as `finops-dashboard-runtime-sa`.

## 2. Standalone Operator Dashboard Flow (`finops-tokenomics-dashboard`)

1. **Lite Boot + SWR Hydration:** Opening `/ui` (or `/`) renders the embedded initial state immediately, then calls `GET /api/state?lite=true` and `GET /api/ge_fleet` with Stale-While-Revalidate (`#swrRefreshBadge`). Active sub-views (`localStorage.getItem('finops.costSubView')`) and OTel layer filters (`localStorage.getItem('finops.otelLayerFilter')`) restore across reloads while What-If simulator sliders always reset on load.
2. **Lazy Tab Hydration:** Switching to **Tab 4 (Users)** or **Tab 7 (Antigravity)** triggers `ensureTabHydrated(tab)`, fetching `GET /api/user_centric_finops` or `GET /api/antigravity_finops` and marking the tab hydrated only after `res.ok`.
3. **Provisioned Throughput (GSU), Governance Score & Remediation Simulator:** **Tab 6 (Overview)** and **Tab 3 (Cost)** render Peak-Aware Provisioned Throughput sizing (`finops_dashboard/throughput.py`, comparing P95/P99 60-second token bursts against Vertex AI GSU reference tables) and the 5-Pillar Governance Score (`FINOPS_MONTHLY_BUDGET_USD`, `FINOPS_CACHE_HIT_TARGET_PCT`). Operators can toggle **Simulate Remediation** (`#vlGovSimBtn`, `VL.simulateGovernanceRemediation`) to preview the projected 100/100 score and recoverable monthly savings, or click **Copy Executive Brief** (`#vlGovCopyBriefBtn`, `VL.formatGovernanceBrief`) to copy a Markdown governance summary to the clipboard.
4. **Plain-English Search (NL2SQL):** Opening `#nl2sqlCopilotDrawer` (`#toggleNl2SqlBtn`) and submitting a question posts to `POST /api/nl2sql`, which verifies the generated query with `verify_nl2sql_sql` and executes it with `NL2SQL_MAX_BYTES_BILLED` (100 MiB).
5. **Prompt Cache X-Ray & AST Linter:** Opening **Tab 2 (Optimizer & Testing)** calls `GET /api/prompt_xray/live_turns` and `POST /api/prompt_xray` (or MCP tools `list_prompt_snapshot_turns`, `xray_prompt_cache`, and `lint_agent_finops`) to pinpoint cache-busting prefixes and `FIN-01`..`FIN-05` AST findings.

## 3. Closed-Loop Idle Runtime Governance & Keep-Alive Flow

1. **Stage 1 — Evaluate & Warn (`POST /api/governance/evaluate_idle` / `POST /api/cron/warn_idle_runtimes`):**
   - Evaluates fleet idle telemetry (`_idle_evidence`), resolves creator emails from `ds_ge_audit_raw` (`resolve_creator_emails`), persists rows to `finops_analytics.zombie_clearance_queue`, and sends warning emails (`_deliver_email_notification`) containing an HMAC-signed 7-day Keep-Alive link (`GET /api/governance/snooze?runtime_id=...&days=7&token=...`).
   - Public API responses (`GET /api/state`, `GET /api/governance/clearance_queue`) mask creator emails (`mask_email_for_ui`) and redact `token=REDACTED` (`_redact_snooze_tokens`).
2. **Keep-Alive / Snooze (`GET /api/governance/snooze` or `POST /api/governance/snooze`):**
   - Clicking the email link (`GET /api/governance/snooze`) verifies the HMAC token (`verify_snooze_token`) and sets `status = 'SNOOZED'` for 7 days.
   - Clicking **Keep Alive 7d** in the dashboard (`POST /api/governance/snooze`) snoozes a runtime already present in `zombie_clearance_queue` for up to 7 days.
3. **Stage 2 — Scheduled Clearance (`POST /api/governance/execute_clearance` / `POST /api/cron/execute_scheduled_clearance`):**
   - Re-reads `zombie_clearance_queue` from BigQuery (`_load_queue_from_bq_if_needed(force=True)`), skips snoozed or active runtimes, and either logs `DRY_RUN` (`mode='dry_run'`) or executes live Cloud Run `minScale=0` / Reasoning Engine deletion when `CLEARANCE_MODE=enforced` and `FINOPS_ALLOW_AUTOMATED_CLEARANCE=1`.
4. **Executive Digest (`GET /api/governance/executive_digest` / `POST /api/cron/send_executive_digest`):**
   - Compiles week-over-week spend deltas, top agents/users, cache-hit alerts, and pending clearance items, previewing in `#govDigestPreview` or delivering to `FINOPS_DIGEST_RECIPIENTS`.

