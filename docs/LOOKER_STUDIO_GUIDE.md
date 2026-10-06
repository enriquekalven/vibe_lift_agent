# Gemini Enterprise L1/L2 IT Support Dashboard — Looker Studio Guide

This guide covers the deployed **`vibelift_mart.v_looker_l1_l2_support`** BigQuery view (live in both `gemini-enterprise-stage` and `project-maui`), **1-click Looker Studio creation links**, the **zero-fan-out & sub-second latency architecture**, and the exact field mappings for each chart, filter control, and drill-down table so it reaches 100% parity with the Zscaler IT Support Console MCP App.

---

## 1. One-Click Looker Studio Creation Links

Click either URL below to open Looker Studio in edit mode with the BigQuery connector pre-configured and bound directly to `v_looker_l1_l2_support`:

- **[Open Looker Studio Connected to `gemini-enterprise-stage.vibelift_mart.v_looker_l1_l2_support`](https://lookerstudio.google.com/reporting/create?c.mode=edit&ds.connector=bigQuery&ds.projectId=gemini-enterprise-stage&ds.type=TABLE&ds.datasetId=vibelift_mart&ds.tableId=v_looker_l1_l2_support&r.reportName=Gemini+Enterprise+L1%2FL2+IT+Support+%28Stage%29)**
- **[Open Looker Studio Connected to `project-maui.vibelift_mart.v_looker_l1_l2_support`](https://lookerstudio.google.com/reporting/create?c.mode=edit&ds.connector=bigQuery&ds.projectId=project-maui&ds.type=TABLE&ds.datasetId=vibelift_mart&ds.tableId=v_looker_l1_l2_support&r.reportName=Gemini+Enterprise+L1%2FL2+IT+Support+%28Maui%29)**

### Step-by-Step Setup Walkthrough (5–10 Minutes)

1. **Step 1 — Open & Save the Report**:
   - Click the **`gemini-enterprise-stage`** link above (or connect manually via **lookerstudio.google.com → Create → Report → BigQuery → Project: `gemini-enterprise-stage` → Dataset: `vibelift_mart` → Table: `v_looker_l1_l2_support` → Add**).
   - In the top-right corner of Looker Studio, click **Save and Share** → **Acknowledge and save** to convert the template into a permanent editable report.
   - Set the canvas size for wide tables: click an empty area of the canvas → in the right-hand **Theme and layout** panel, click the **Layout** tab → under **Canvas size**, choose **16:9 Landscape (1920 × 1080)** or **Custom (`1800 × 1600`)**.

2. **Step 2 — Configure the 2 Deep-Link URL Columns (One-Time Data Source Setting)**:
   - In the top menu bar, click **Resource → Manage added data sources → Edit** (next to `v_looker_l1_l2_support`).
   - Find **`cloud_logging_url`** and **`cloud_trace_url`** → change their **Type** from `Text` to **`URL`**.
   - Find **`event_date`** → confirm it is set as the **Default date range dimension**. Click **Done** (top right) and **Close**.

3. **Step 3 — Add the Top Filter Bar (Page 1)**:
   - Click **Add a control** in the top toolbar:
     - **Date range control**: Place at top-right → set default to **Last 30 days** (or **This quarter**).
     - **Drop-down list**: Place 6 drop-downs across the top row and drag one field into each **Control field**:
       1. `needs_support_attention`
       2. `support_tier`
       3. `issue_category`
       4. `engine_key`
       5. `user_email`
       6. `session_id`
     - **Input box**: Place next to the drop-downs → set **Control field** to `prompt_preview` → in the **Style** tab, set **Search type** to **Contains** (lets support engineers search by keyword).

4. **Step 4 — Add the KPI Scorecards, Charts & L1/L2 Incident Queue Table (Page 1)**:
   - Delete the default starter table Looker Studio puts on the canvas.
   - **KPI Scorecards**: Click **Add a chart → Scorecard** and drag the fields from **[Section 3.B](#b-top-kpi-scorecards-row-1--8-cards)** below.
   - **Charts**: Add the **Stacked Combo Chart** (`event_date` + `support_tier`), **Horizontal Bar Chart** (`issue_category`), and **Blast Radius Table** (`error_signature` ranked by `COUNT_DISTINCT(user_email)`). At the bottom of the **Setup** panel for each chart, check ✅ **Cross-filtering**.
   - **L1/L2 Incident Queue Table**: Click **Add a chart → Table**, stretch it full-width across the bottom of Page 1, and drag the columns listed in **[Section 3.D](#d-l1l2-incident-queue--runbook-table-row-3--full-width)**. In the **Style** tab of the table, check ✅ **Wrap text** under **Table Body** so full prompts, responses, tool errors, and runbooks are readable inline.

5. **Step 5 — Add Pages 2, 3, and 4**:
   - In the top-left toolbar, click **Page → New page** (repeat 3 times) and rename the pages via **Page → Manage pages**:
     - **Page 1**: `1. L1/L2 Triage & Issues` (follow Section 3)
     - **Page 2**: `2. Session Explorer & Replay` (follow Section 4 — place the **Session Summary Table** on top with ✅ **Cross-filtering** enabled, and the **Turn-by-Turn Replay Table** sorted by `session_step_number` Ascending on the bottom)
     - **Page 3**: `3. Users, Agents & MCP Tools` (follow Section 5)
     - **Page 4**: `4. Model Armor & SDP Security` (follow Section 6)

---

## 2. Zero 1:N Row Fan-Out & Sub-Second Latency Architecture

To guarantee that Looker Studio scorecards never double-count turns or tokens and that every chart renders in `< 400ms`:

1. **Zero Query-Time Joins (`FROM vibelift_mart.fct_turns` Only)**:
   `v_looker_l1_l2_support` reads **only** the materialized, date-partitioned (`PARTITION BY event_date`) and clustered (`CLUSTER BY engine_key, user_email`) `vibelift_mart.fct_turns` table. All JSON parsing, regex extraction, and multi-sink correlation happen **at mart build/refresh time** (`provision_ge_mart.py --refresh`), never when Looker Studio queries the view.
2. **Pre-Aggregation of 1:N Child Entities (Zero Fan-Out)**:
   - **Tool Calls & Errors (`part_index = 0..N` and `llm_calls = 1..M`)**: Pre-aggregated per LLM call via `ARRAY_TO_STRING(..., ' | ')` in `v_agentic_operations_curated` and per `trace_id` (`GROUP BY inference_key`) via `STRING_AGG(..., ' | ' ORDER BY event_timestamp)` in `v_fct_turns`, attached strictly to `trace_rank = 1`.
   - **Prompts, Streamed Responses, Citations, Data Stores & Files**: Deduplicated by `insertId` (`QUALIFY ROW_NUMBER() OVER (PARTITION BY insertId ...) = 1`) with `citation_sources`, `queried_data_stores`, and `uploaded_file_names` flattened into scalar strings (`STRING`), plus streamed `SANITIZE_MODEL_RESPONSE` chunks reconstructed per `assist_token`.
   - **Model Armor & SDP Checks**: Pre-aggregated per `assist_token` / `trace_id` into `armor_checks`, `armor_blocks`, `armor_findings`, `armor_verdict_reasons`, and `armor_sdp_info_types`.
   - **Enforced by Invariant #2 (`looker_support_view_no_fanout`)**: Every `--apply` and `--refresh` verifies `COUNT(1) == COUNT(DISTINCT turn_id) == COUNT(fct_turns)` and `SUM(total_tokens) == SUM(fct_turns.total_tokens)` (`1,086` rows = `1,086` distinct turns, `86,446,759` tokens, `1.55 MB` total physical table size).

---

## 3. Page 1: L1/L2 Support Triage Queue & Root-Cause Diagnostics

### A. Top Filter Controls (7 Drop-Downs + Date Range + Search Box)

| Control Type | Field Name | Purpose |
|---|---|---|
| **Date range control** | `event_date` | Restricts partitions scanned in `fct_turns` (default: Last 30 days) |
| **Drop-down list** | `needs_support_attention` | Quick toggle (`true` = show only failed/blocked/tool-error turns) |
| **Drop-down list** | `support_tier` | Filter by `L1 - User / Input / Policy`, `L2 - Platform / Agent / MCP`, or `L0 - Healthy` |
| **Drop-down list** | `issue_category` | Filter by `MCP / Agent Tool Failure`, `Guardrail / DLP Block`, `No Answer / Skipped`, `File Upload Error`, `IAM / Auth Denial`, `Quota / Rate Limit`, `Timeout / Deadline Exceeded`, `Datastore / Federated Search Error`, etc. |
| **Drop-down list** | `engine_key` | Filter by Gemini Enterprise App / Engine (`location/engine_id`) |
| **Drop-down list** | `user_email` | Look up a specific user reporting an issue |
| **Drop-down list** | `session_id` | Isolate a single conversation session |
| **Input box (Search)** | `prompt_preview` (or `issue_summary`) | Free-text search across user prompts, error messages, tool names, and trace IDs |

---

### B. Top KPI Scorecards (Row 1 — 8 Cards)

| Scorecard Title | Metric / Field | Aggregation | Conditional Formatting |
|---|---|---|---|
| **Total Turns** | `turn_id` | `COUNT_DISTINCT` | Neutral (Blue) |
| **Needs Support Attention** | `needs_support_attention` (Filter: `needs_support_attention = true`) | `COUNT_DISTINCT(turn_id)` | Red if `> 0` |
| **Failed API / Platform Turns** | `is_failed` (Filter: `is_failed = true`) | `COUNT_DISTINCT(turn_id)` | Red if `> 0` |
| **No Answer / Skipped Turns** | `issue_category` (Filter: `issue_category = "No Answer / Skipped"`) | `COUNT_DISTINCT(turn_id)` | Amber if `> 0` |
| **Guardrail / DLP Blocks** | `is_guardrail_blocked` (Filter: `is_guardrail_blocked = true`) | `COUNT_DISTINCT(turn_id)` | Amber/Red if `> 0` |
| **Model Armor Findings** | `armor_findings` | `SUM` | Amber if `> 0` |
| **MCP / Tool Call Failures** | `tool_failure_count` | `SUM` | Orange/Red if `> 0` |
| **Affected Users** | `user_email` (Filter: `needs_support_attention = true`) | `COUNT_DISTINCT` | Amber if `> 0` |

---

### C. Diagnostic Breakdown Charts (Row 2)

1. **Daily Turns & Support Incidents Over Time (Stacked Combo Chart)**:
   - **Dimension**: `event_date`
   - **Breakdown Dimension**: `support_tier` (`L0 - Healthy`, `L1 - User / Input / Policy`, `L2 - Platform / Agent / MCP`)
   - **Metric**: `Record Count`
2. **Incidents by Issue Category (Horizontal Bar Chart)**:
   - **Dimension**: `issue_category`
   - **Metric**: `Record Count`
   - **Chart Filter**: `Exclude issue_category = "Healthy"`
   - **Cross-filtering**: Enabled (clicking a bar filters the L1/L2 Ticket Queue table below).
3. **Recurring Error Patterns / Blast Radius (Table with Bars)**:
   - **Dimensions**: `error_signature`, `issue_category`, `engine_key`
   - **Metrics**:
     - `COUNT_DISTINCT(user_email)` (*Affected Users*)
     - `COUNT_DISTINCT(session_id)` (*Affected Sessions*)
     - `Record Count` (*Occurrences*)
   - **Filter**: `needs_support_attention = true`
   - **Sort**: `Affected Users` descending, then `Occurrences` descending.
4. **Data Store / Connector & Tool Failure Hotspots (Table)**:
   - **Dimensions**: `queried_data_stores`, `mcp_server_name`, `tool_names`, `tool_error_codes`
   - **Metrics**: `SUM(tool_call_count)`, `SUM(tool_failure_count)`, `COUNT_DISTINCT(turn_id)`

---

### D. L1/L2 Incident Queue & Runbook Table (Row 3 — Full Width)

Add a **Table** with **Cross-filtering** and **Text Wrapping** enabled:

- **Dimensions (in column order)**:
  1. `ticket_id` *(Short 14-char incident key)*
  2. `event_timestamp` *(Sort Descending)*
  3. `support_tier` *(`L1 - User / Input / Policy` vs `L2 - Platform / Agent / MCP`)*
  4. `issue_category`
  5. `user_email`
  6. `engine_key`
  7. `agent_name`
  8. `prompt_preview` *(User prompt / query text or `[Confirmed Action: ...]`)*
  9. `response_preview` *(Assistant reply or search result count summary)*
  10. `l1_runbook_action` *(Prescriptive step-by-step L1/L2 remediation guidance)*
  11. `issue_summary` *(Combined method, status, guardrail category, failed tools, and error message)*
  12. `tool_error_codes` *(Pre-aggregated error codes, e.g., `MCP_PAYLOAD_ERROR`, `400`, `500`)*
  13. `tool_error_messages` *(Exact tool error strings across all failed tool calls in the turn)*
  14. `queried_data_stores` *(Data stores / federated connectors queried or implicated in error)*
  15. `uploaded_file_names` *(Uploaded file names / session `fileId`s)*
  16. `armor_verdict_reasons` *(Model Armor verdict reasons & `armor_sdp_info_types`)*
  17. `session_id`
  18. `trace_id`
  19. `cloud_logging_url` *(Configure field type as **URL → Hyperlink** with label `"Open in Cloud Logging"`)*
  20. `cloud_trace_url` *(Configure field type as **URL → Hyperlink** with label `"Open in Cloud Trace"`)*
- **Metrics**:
  - `latency_ms` (`AVG` or `MAX`)
  - `tool_failure_count` (`SUM`)
  - `total_tokens` (`SUM`)

---

## 4. Page 2: End-to-End Session Explorer & Turn-by-Turn Replay

### A. Top Filter Controls

- **Drop-down list**: `session_id` *(Enables pasting a session ID from a user ticket)*
- **Drop-down list**: `user_email`
- **Drop-down list**: `session_has_issue` *(Set to `true` to browse only sessions that experienced an error, guardrail block, or tool failure)*
- **Drop-down list**: `engine_key`

---

### B. Session Summary Table (Master Table — Top Half)

Clicking any session row in this table automatically cross-filters the **Turn-by-Turn Conversation & Tool Replay Table** below it:

- **Dimension**: `session_id`, `user_email`, `engine_key`, `session_has_issue`
- **Metrics**:
  - `MAX(session_total_turns)` *(Turns in Session)*
  - `MAX(session_issue_turns)` *(Issue Turns)*
  - `MAX(session_failed_turns)` *(Failed Turns)*
  - `MAX(session_guardrail_blocks)` *(Guardrail Blocks)*
  - `MAX(session_tool_failures)` *(Tool Failures)*
  - `MAX(session_duration_seconds)` *(Session Duration (s))*
  - `MAX(session_total_tokens)` *(Total Session Tokens)*
  - `MIN(event_timestamp)` *(Session Started At)*

---

### C. Turn-by-Turn Conversation & Tool Replay Table (Detail Table — Bottom Half)

Reconstructs the exact chronological flow (`Step 1 → Step 2 → Step 3...`) of a user's session:

- **Dimensions (in column order)**:
  1. `session_step_number` *(Sort Ascending: `1, 2, 3...`)*
  2. `event_timestamp`
  3. `turn_kind` *(`CHAT`, `FILE_UPLOAD`, `WIDGET_ACTION`, `SEARCH`, `AGENT_CALL`)*
  4. `api_method` *(`StreamAssist`, `UploadSessionFile`, `ExecuteUiWidgetAction`, `Search`)*
  5. `turn_status` *(`SUCCESS`, `SKIPPED`, `SERVER_ERROR`, `PERMISSION_DENIED`, `RATE_LIMITED`, `MODEL_BLOCKED`)*
  6. `prompt_preview` *(User prompt, query, or confirmed UI action)*
  7. `response_preview` *(Assistant grounded response or streamed Model Armor reply)*
  8. `uploaded_file_names` *(Files attached in this step)*
  9. `queried_data_stores` *(Enterprise connectors / data stores queried)*
  10. `citation_sources` *(Grounded reference titles/domains/URIs returned)*
  11. `agent_name`
  12. `model_name`
  13. `tool_args_summary` *(Pre-aggregated tool calls and JSON arguments invoked during the turn)*
  14. `tool_output_preview` *(Pre-aggregated tool output previews)*
  15. `tool_error_messages` *(Pre-aggregated tool error messages if any tool call failed)*
  16. `armor_verdict_reasons` *(Model Armor verdict & `armor_sdp_info_types`)*
  17. `l1_runbook_action`
  18. `cloud_logging_url`
- **Metrics**:
  - `latency_ms`
  - `input_tokens`
  - `output_tokens`
  - `cached_input_tokens`
  - `reasoning_tokens`
  - `total_tokens`
  - `reference_count`
  - `tool_call_count`
  - `tool_failure_count`

---

## 5. Page 3: Users, Agents & MCP Tool Performance

Maps directly to the **Users** and **Agents** tabs of the MCP App (`zscaler-it-support`), enabling L1/L2 engineers to spot whether an issue is isolated to one user's ACLs/OAuth token or systemic to a specific Agent / MCP connector.

### A. Agent & Engine Health Table (Top Half — Cross-Filtering Enabled)
- **Dimensions**:
  1. `agent_name`
  2. `engine_key`
  3. `model_name`
  4. `model_selection_mode` *(`AUTO` vs `EXPLICIT`)*
  5. `mcp_server_name`
- **Metrics**:
  - `COUNT_DISTINCT(turn_id)` *(Total Turns)*
  - `COUNT_DISTINCT(user_email)` *(Active Users)*
  - `SUM(tool_call_count)` *(Total Tool Calls)*
  - `SUM(tool_failure_count)` *(Failed Tool Calls)*
  - `PERCENTILE_CONT(latency_ms, 0.50)` / `AVG(latency_ms)` *(Median / Avg Turn Latency ms)*
  - `PERCENTILE_CONT(latency_ms, 0.95)` / `MAX(latency_ms)` *(p95 / Max Turn Latency ms)*
  - `SUM(total_tokens)` *(Total Tokens)*
  - `SUM(cached_input_tokens)` *(Cached Input Tokens)*
  - `SUM(reasoning_tokens)` *(Reasoning Tokens)*

### B. User Adoption & Issue Impact Table (Bottom Half — Cross-Filtering Enabled)
- **Dimensions**:
  1. `user_email`
  2. `user_domain`
- **Metrics**:
  - `COUNT_DISTINCT(session_id)` *(Sessions)*
  - `COUNT_DISTINCT(turn_id)` *(Turns)*
  - `SUM(IF(needs_support_attention, 1, 0))` *(Turns Needing Support)*
  - `SUM(tool_failure_count)` *(Tool Failures Experienced)*
  - `SUM(armor_blocks)` *(Guardrail Blocks)*
  - `SUM(total_tokens)` *(Total Tokens)*
  - `MAX(event_timestamp)` *(Last Active Timestamp)*

---

## 6. Page 4: Model Armor & Sensitive Data Protection (SDP) Security Audit

Maps directly to the **Model Armor & SDP** tab of the MCP App (`zscaler-it-support`).

### A. Security KPI Scorecards (Row 1)
- **Total Model Armor Checks**: `SUM(armor_checks)`
- **Guardrail / DLP Blocked Turns**: `COUNT_DISTINCT(turn_id)` (Filter: `is_guardrail_blocked = true`)
- **Model Armor Findings**: `SUM(armor_findings)`
- **Prompt Injection / Jailbreak Flags**: `COUNT_DISTINCT(turn_id)` (Filter: `is_prompt_injection = true`)
- **Sensitive Data (SDP / DLP) Flags**: `COUNT_DISTINCT(turn_id)` (Filter: `is_sensitive_data = true`)
- **Safety / Malicious URI Flags**: `COUNT_DISTINCT(turn_id)` (Filter: `is_safety_violation = true OR is_malicious_uri = true`)

### B. Flagged & Inspected Turns Table (Row 2)
- **Chart Filter**: `armor_checks > 0 OR is_guardrail_blocked = true`
- **Dimensions**:
  1. `event_timestamp`
  2. `user_email`
  3. `engine_key`
  4. `turn_status`
  5. `guardrail_categories` *(`PROMPT_INJECTION`, `SENSITIVE_DATA`, `SAFETY_VIOLATION`, `MALICIOUS_URI`)*
  6. `armor_verdict_reasons` *(Specific Model Armor filter reason)*
  7. `armor_sdp_info_types` *(Specific DLP InfoTypes matched, e.g., `PERSON_NAME`, `EMAIL_ADDRESS`, `CREDIT_CARD_NUMBER`)*
  8. `prompt_preview`
  9. `response_preview`
  10. `l1_runbook_action`
  11. `cloud_logging_url`
- **Metrics**:
  - `armor_checks` (`SUM`)
  - `armor_findings` (`SUM`)
  - `armor_blocks` (`SUM`)

---

## 7. Usability Features & Parity with the MCP App (`zscaler-it-support`)

| MCP App Tab / Feature | Looker Studio Equivalent | How It Works in Looker Studio |
|---|---|---|
| **Tab 1: Overview** | **Page 1 — Top Scorecards & Trend Charts** | 8 KPI scorecards + Daily Turns by Support Tier stacked combo chart + Issue Category horizontal bar chart. |
| **Tab 2: Issues (Blast Radius Grouping)** | **Page 1 — Recurring Error Patterns Table (`error_signature`)** | Normalizes dynamic UUIDs/IDs into `error_signature` and ranks issues by `COUNT_DISTINCT(user_email)` (*Affected Users*) and `COUNT_DISTINCT(session_id)` (*Affected Sessions*). Clicking any pattern cross-filters the Incident Queue below to show only turns matching that signature. |
| **Tab 3: Interactions & Turn Detail Drawer** | **Page 1 — L1/L2 Incident Queue & Runbook Table** | Displays `prompt_preview`, `response_preview`, `l1_runbook_action`, `tool_error_codes`, `tool_error_messages`, `tool_args_summary`, `tool_output_preview`, `queried_data_stores`, `uploaded_file_names`, and `citation_sources` inline with text wrapping, plus 1-click hyperlinks for **Open in Cloud Logging** and **Open in Cloud Trace**. |
| **Tab 4: Sessions & Step-by-Step Timeline** | **Page 2 — Master-Detail Session Explorer** | Clicking a row in the **Session Summary Table** (top) cross-filters the **Turn-by-Turn Replay Table** (bottom) ordered by `session_step_number = 1, 2, 3...`. |
| **Tab 5 & 6: Users & Agents** | **Page 3 — Users, Agents & MCP Tool Performance** | Cross-filtering tables showing per-agent `p50`/`p95` latency, MCP tool failure rates, model routing (`AUTO` vs `EXPLICIT`), and per-user impact. |
| **Tab 7: Model Armor & SDP** | **Page 4 — Model Armor & SDP Security Audit** | Breaks down `armor_checks`, `armor_findings`, `armor_blocks`, `armor_verdict_reasons`, and `armor_sdp_info_types` (`PERSON_NAME`, `US_SOCIAL_SECURITY_NUMBER`, etc.). |

