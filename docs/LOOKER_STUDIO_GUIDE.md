# Gemini Enterprise L1/L2 IT Support Dashboard — Looker Studio Guide

This guide covers the deployed **`vibelift_mart.v_looker_l1_l2_support`** BigQuery view (live in both `gemini-enterprise-stage` and `project-maui`), **1-click Looker Studio creation links**, the **zero-fan-out & sub-second latency architecture**, and the exact field mappings for each chart, filter control, and drill-down table so it reaches 100% parity with the Zscaler IT Support Console MCP App.

---

## 1. Fastest Way to Open in Looker Studio (Zero Link Errors)

> **Why `/reporting/create` URLs fail without a template ID:** Looker Studio's `/reporting/create` Linking API throws *"Something went wrong when building your report"* when called without an existing `c.reportId` template or `ds.billingProjectId` on a BigQuery View. Use **Option A (BigQuery Console 1-Click Handoff)** or **Option B (Looker Studio Home)** below — both work 100% of the time.

### Option A (Recommended — 2 Clicks via BigQuery Console):
1. Click the BigQuery Console link for your environment to open `v_looker_l1_l2_support` directly:
   - **[Open `gemini-enterprise-stage.vibelift_mart.v_looker_l1_l2_support` in BigQuery Console](https://console.cloud.google.com/bigquery?project=gemini-enterprise-stage&ws=!1m5!1m4!4m3!1sgemini-enterprise-stage!2svibelift_mart!3sv_looker_l1_l2_support)**
   - **[Open `project-maui.vibelift_mart.v_looker_l1_l2_support` in BigQuery Console](https://console.cloud.google.com/bigquery?project=project-maui&ws=!1m5!1m4!4m3!1sproject-maui!2svibelift_mart!3sv_looker_l1_l2_support)**
2. In the BigQuery Console toolbar at the top of the view tab, click **Export → Explore with Looker Studio**.
3. Once Looker Studio opens with the view loaded, click **Save and Share** (top right) → **Acknowledge and save** to unlock full multi-page report editing.

### Option B (Directly in Looker Studio — 3 Clicks):
1. Open **[Looker Studio Home (`lookerstudio.google.com`)](https://lookerstudio.google.com/)**.
2. Click **Create** (top-left) → **Report**.
3. In the *Add data to report* panel, select **BigQuery** → choose:
   - **Project**: `gemini-enterprise-stage` *(or `project-maui`)*
   - **Dataset**: `vibelift_mart`
   - **Table / View**: `v_looker_l1_l2_support`
   - Click **Add** → **Add to report**.

### Step-by-Step Layout Walkthrough (5–10 Minutes)

1. **Step 1 — Widen the Canvas**:
   - Once your report is open in Edit mode, click an empty area of the canvas → in the right-hand **Theme and layout** panel, click the **Layout** tab → under **Canvas size**, choose **16:9 Landscape (1920 × 1080)** or **Custom (`1800 × 1600`)**.

2. **Step 2 — Configure the 2 Deep-Link URL Columns (One-Time Data Source Setting)**:
   - In the top menu bar, click **Resource → Manage added data sources → Edit** (next to `v_looker_l1_l2_support`).
   - Find **`cloud_logging_url`** and **`cloud_trace_url`** → change their **Type** from `Text` to **`URL`**.
   - Find **`event_date`** → confirm it is set as the **Default date range dimension**. Click **Done** (top right) and **Close**.

3. **Step 3 — Add the Top Filter Bar (Page 1)**:
   - Click **Add a control** in the top toolbar:
     - **Date range control**: Place at top-right → set default to **Last 30 days** (or **This quarter**).
     - **Drop-down list**: Place 7 drop-downs across the top row and drag one field into each **Control field**:
        1. `needs_support_attention`
        2. `support_tier`
        3. `issue_type` *(5 high-level MCP App badges: `Guardrail block`, `Access denied`, `Platform error`, `Tool error`, `No answer`, `Healthy`)*
        4. `issue_category` *(11 granular root-cause categories)*
        5. `engine_key`
        6. `user_email`
        7. `session_id`
      - **Input box (Omni-Search)**: Place next to the drop-downs → set **Control field** to **`search_text`** → in the **Style** tab, set **Search type** to **Contains** (lets support engineers search across prompts, responses, trace IDs, session IDs, ticket IDs, user emails, tool names, tool error messages, data stores, file names, and guardrail verdicts in a single box).

4. **Step 4 — Add the KPI Scorecards, Charts & L1/L2 Incident Queue Table (Page 1)**:
   - Delete the default starter table Looker Studio puts on the canvas.
   - **KPI Scorecards**: Click **Add a chart → Scorecard** and drag the fields from **[Section 3.B](#b-top-kpi-scorecards-row-1--8-cards)** below (plus a `MAX(refreshed_at)` **Data Freshness** indicator in the top header).
   - **Charts**: Add the **Stacked Combo Chart** (`event_date` + `support_tier`), **Horizontal Bar Chart** (`issue_category` or `issue_type`), and **Blast Radius Table** (`error_signature` ranked by `COUNT_DISTINCT(user_email)`). At the bottom of the **Setup** panel for each chart, check ✅ **Cross-filtering**.
   - **L1/L2 Incident Queue Table**: Click **Add a chart → Table**, stretch it full-width across the bottom of Page 1, and drag the columns listed in **[Section 3.D](#d-l1l2-incident-queue--runbook-table-row-3--full-width)**. In the **Style** tab of the table, check ✅ **Wrap text** under **Table Body** so full prompts, responses, tool errors, escalation summaries, and runbooks are readable inline.

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
   - **Model Armor & SDP Checks (Including `UploadSessionFile` Image Scans)**: Pre-aggregated per `assist_token`, `trace_id`, and 1:1 timestamp-matched `UploadSessionFile` check into `armor_checks`, `armor_blocks`, `armor_findings`, `armor_verdict_reasons`, and `armor_sdp_info_types`.
   - **Enforced by Invariant #2 (`looker_support_view_no_fanout`)**: Every `--apply` and `--refresh` verifies `COUNT(1) == COUNT(DISTINCT turn_id) == COUNT(fct_turns)` and `SUM(total_tokens) == SUM(fct_turns.total_tokens)` (`1,086` rows = `1,086` distinct turns, `86,446,759` tokens, `1.55 MB` total physical table size).

---

## 3. Page 1: L1/L2 Support Triage Queue & Root-Cause Diagnostics

### A. Top Filter Controls (7 Drop-Downs + Date Range + Omni-Search Box)

| Control Type | Field Name | Purpose |
|---|---|---|
| **Date range control** | `event_date` | Restricts partitions scanned in `fct_turns` (default: Last 30 days) |
| **Drop-down list** | `needs_support_attention` | Quick toggle (`true` = show only failed/blocked/tool-error turns) |
| **Drop-down list** | `support_tier` | Filter by `L1 - User / Input / Policy`, `L2 - Platform / Agent / MCP`, or `L0 - Healthy` |
| **Drop-down list** | `issue_type` | High-level 5-badge filter matching the MCP App (`Guardrail block`, `Access denied`, `Platform error`, `Tool error`, `No answer`, `Healthy`) |
| **Drop-down list** | `issue_category` | Granular 11-category filter (`MCP / Agent Tool Failure`, `Guardrail / DLP Block`, `No Answer / Skipped`, `File Upload Error`, `IAM / Auth Denial`, `Quota / Rate Limit`, `Timeout / Deadline Exceeded`, `Datastore / Federated Search Error`, etc.) |
| **Drop-down list** | `engine_key` | Filter by Gemini Enterprise App / Engine (`location/engine_id`) |
| **Drop-down list** | `user_email` | Look up a specific user reporting an issue |
| **Drop-down list** | `session_id` | Isolate a single conversation session |
| **Input box (Omni-Search)** | `search_text` | Single free-text search box (set to **Contains**) that searches across 26 fields simultaneously: prompts, responses, `turn_id`, `ticket_id`, `trace_id`, `session_id`, `user_email`, `agent_name`, `tool_names`, `tool_error_messages`, `queried_data_stores`, `uploaded_file_names`, `citation_sources`, and `armor_verdict_reasons` |

---

### B. Top KPI Scorecards (Row 1 — 8 Cards + Data Freshness Header)

> **1-Click Shortcut for Scorecards:** Because `v_looker_l1_l2_support` is strictly **1 row per `turn_id`**, Looker Studio's default **`Record Count`** metric gives the exact same result as `Count Distinct(turn_id)`. To rename any scorecard or change its aggregation, hover over the left side of the metric pill (`AUT` / `CTD` / `SUM`) in the **Setup** panel until the **pencil icon (`✏️`)** appears, click it, and type the scorecard title.

| Scorecard Title | Metric / Field | Aggregation | Chart Filter (Setup → Add a filter) |
|---|---|---|---|
| **Total Turns** | `Record Count` *(or `turn_id`)* | `AUT` *(or `Count Distinct`)* | *None* |
| **Needs Support Attention** | `Record Count` *(or `turn_id`)* | `AUT` *(or `Count Distinct`)* | `Include needs_support_attention = true` |
| **Failed API / Platform Turns** | `Record Count` *(or `turn_id`)* | `AUT` *(or `Count Distinct`)* | `Include is_failed = true` |
| **No Answer / Skipped Turns** | `Record Count` *(or `turn_id`)* | `AUT` *(or `Count Distinct`)* | `Include issue_category = No Answer / Skipped` |
| **Guardrail / DLP Blocks** | `Record Count` *(or `turn_id`)* | `AUT` *(or `Count Distinct`)* | `Include is_guardrail_blocked = true` |
| **Model Armor Findings** | `armor_findings` | `SUM` | *None* |
| **MCP / Tool Call Failures** | `tool_failure_count` | `SUM` | *None* |
| **Affected Users** | `user_email` | `Count Distinct (CTD)` | `Include needs_support_attention = true` |
| **Mart Last Refreshed (Header)** | `refreshed_at` | `MAX` | *None* |

---

### C. Diagnostic Breakdown Charts (Row 2)

1. **Daily Turns & Support Incidents Over Time (Stacked Combo Chart)**:
   - **Dimension**: `event_date`
   - **Breakdown Dimension**: `support_tier` (`L0 - Healthy`, `L1 - User / Input / Policy`, `L2 - Platform / Agent / MCP`)
   - **Metric**: `Record Count`
2. **Incidents by Issue Category (Horizontal Bar Chart)**:
   - **Dimension**: `issue_category` *(or `issue_type` for the 5-badge view)*
   - **Metric**: `Record Count`
   - **Chart Filter**: `Exclude issue_category = "Healthy"`
   - **Cross-filtering**: Enabled (clicking a bar filters the L1/L2 Ticket Queue table below).
3. **Recurring Error Patterns / Blast Radius (Table with Bars)**:
   - **Dimensions**: `error_signature`, `issue_type`, `issue_category`, `engine_key`
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
  4. `issue_type` *(`Guardrail block`, `Access denied`, `Platform error`, `Tool error`, `No answer`, `Healthy`)*
  5. `issue_category`
  6. `user_email`
  7. `engine_key`
  8. `agent_name` *(Never NULL — automatically falls back to `Search`, `File upload`, `Widget action`, or `Core assistant`)*
  9. `prompt_preview` *(User prompt / query text or `[Confirmed Action: ...]`)*
  10. `response_preview` *(Assistant reply or search result count summary)*
  11. `l1_runbook_action` *(Prescriptive step-by-step L1/L2 remediation guidance)*
  12. `escalation_ticket_text` *(Pre-formatted 1-click copyable L1→L2 Jira/ServiceNow ticket payload with trace, session, user, latency, tool errors, and runbook)*
  13. `issue_summary` *(Combined method, status, guardrail category, failed tools, and error message)*
  14. `tool_error_codes` *(Pre-aggregated error codes, e.g., `MCP_PAYLOAD_ERROR`, `400`, `500`)*
  15. `tool_error_messages` *(Exact tool error strings across all failed tool calls in the turn)*
  16. `queried_data_stores` *(Data stores / federated connectors queried or implicated in error)*
  17. `uploaded_file_names` *(Uploaded file names / session `fileId`s)*
  18. `armor_verdict_reasons` *(Model Armor verdict reasons & `armor_sdp_info_types`)*
  19. `session_id`
  20. `trace_id`
  21. `cloud_logging_url` *(Configure field type as **URL → Hyperlink** with label `"Open in Cloud Logging"`)*
  22. `cloud_trace_url` *(Configure field type as **URL → Hyperlink** with label `"Open in Cloud Trace"`)*
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

Clicking any session row in this table automatically cross-filters the **Turn-by-Turn Conversation & Tool Replay Table** below it.
*(Tip: Add a chart filter `is_session_first_turn = true` OR use `MAX(...)` on the `session_*` metrics below so session-level window totals are never summed across turns.)*

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
  1. `agent_name` *(Includes stand-in labels `Search`, `File upload`, `Widget action`, and `Core assistant` so no rows appear as `null`)*
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
  4. `turn_kind` *(`CHAT`, `FILE_UPLOAD`, etc. — includes `UploadSessionFile` image/file scan findings)*
  5. `turn_status`
  6. `guardrail_categories` *(`PROMPT_INJECTION`, `SENSITIVE_DATA`, `SAFETY_VIOLATION`, `MALICIOUS_URI`)*
  7. `armor_verdict_reasons` *(Specific Model Armor filter reason)*
  8. `armor_sdp_info_types` *(Specific DLP InfoTypes matched, e.g., `PERSON_NAME`, `EMAIL_ADDRESS`, `CREDIT_CARD_NUMBER`)*
  9. `prompt_preview`
  10. `response_preview`
  11. `l1_runbook_action`
  12. `cloud_logging_url`
- **Metrics**:
  - `armor_checks` (`SUM`)
  - `armor_findings` (`SUM`)
  - `armor_blocks` (`SUM`)

---

## 7. Usability Features & Parity with the MCP App (`zscaler-it-support`)

| MCP App Tab / Feature | Looker Studio Equivalent | How It Works in Looker Studio |
|---|---|---|
| **Tab 1: Overview** | **Page 1 — Top Scorecards & Trend Charts** | 8 KPI scorecards + `MAX(refreshed_at)` freshness badge + Daily Turns by Support Tier stacked combo chart + Issue Category horizontal bar chart. |
| **Tab 2: Issues (Blast Radius Grouping)** | **Page 1 — Recurring Error Patterns Table (`error_signature`)** | Normalizes dynamic emails, URLs, ISO timestamps, UUIDs/hex IDs, and 5+ digit numbers into `error_signature` and ranks issues by `COUNT_DISTINCT(user_email)` (*Affected Users*) and `COUNT_DISTINCT(session_id)` (*Affected Sessions*). Also provides both 5-badge `issue_type` and 11-category `issue_category`. |
| **Tab 3: Interactions & Turn Detail Drawer** | **Page 1 — L1/L2 Incident Queue & Runbook Table** | Displays `prompt_preview`, `response_preview`, `l1_runbook_action`, `escalation_ticket_text` (1-click copyable L1→L2 ticket summary), `tool_error_codes`, `tool_error_messages`, `tool_args_summary`, `tool_output_preview`, `queried_data_stores`, `uploaded_file_names`, and `citation_sources` inline with text wrapping, plus 1-click hyperlinks for **Open in Cloud Logging** and **Open in Cloud Trace**. |
| **Tab 4: Sessions & Step-by-Step Timeline** | **Page 2 — Master-Detail Session Explorer** | Clicking a row in the **Session Summary Table** (top) cross-filters the **Turn-by-Turn Replay Table** (bottom) ordered by `session_step_number = 1, 2, 3...`. |
| **Tab 5 & 6: Users & Agents** | **Page 3 — Users, Agents & MCP Tool Performance** | Cross-filtering tables showing per-agent `p50`/`p95` latency, MCP tool failure rates, model routing (`AUTO` vs `EXPLICIT`), fallback stand-in agent names (`Search`, `File upload`), and per-user impact. |
| **Tab 7: Model Armor & SDP** | **Page 4 — Model Armor & SDP Security Audit** | Breaks down `armor_checks`, `armor_findings`, `armor_blocks`, `armor_verdict_reasons`, and `armor_sdp_info_types` (`PERSON_NAME`, `US_SOCIAL_SECURITY_NUMBER`, etc.), including `UploadSessionFile` file-upload image scan findings. |

---

## 8. Operational Safeguards & How to Prevent Support / Looker Studio Pitfalls

1. **Refresh Fields in Looker Studio After Schema Updates**:
   - Looker Studio caches the column list of a BigQuery view when you first add it. Whenever new columns are added to `v_looker_l1_l2_support`, open the report in Edit mode and click **Resource → Manage added data sources → Edit → Refresh fields** (bottom left) → **Apply**.
2. **Use `search_text` (Not `prompt_preview`) for the Search Input Box**:
   - Looker Studio's **Input box** control can only bind to a single column. Binding it to **`search_text`** (with Search type = **Contains**) allows L1/L2 engineers to paste a `trace_id`, `session_id`, `ticket_id`, `user_email`, `tool_name`, or `tool_error_message` and find the exact turn immediately.
3. **Use `MAX(...)` or `is_session_first_turn = true` on Session Window Columns**:
   - Columns prefixed with `session_*` (`session_total_turns`, `session_total_tokens`, `session_duration_seconds`, etc.) are window-function totals repeated on every turn of that session. In the **Session Summary Table** on Page 2, either aggregate them with `MAX(...)` or add a chart filter `is_session_first_turn = true` so Looker Studio does not `SUM` them across multiple turns in the same session.
4. **Monitor Snapshot Freshness via `MAX(refreshed_at)`**:
   - Because `v_looker_l1_l2_support` queries the materialized `fct_turns` table for sub-second response times, new logs appear after `provision_ge_mart.py --refresh` (or the hourly BigQuery Scheduled Query from `deploy/setup_mart_refresh.sh`) runs. Displaying `MAX(refreshed_at)` in the top header ensures support engineers always know the exact snapshot timestamp.
5. **Unlinked Vertex AI Floor-Setting Model Armor Checks & Historical Connector SDP Logs**:
   - `v_looker_l1_l2_support` is strictly 1 row per user turn (`turn_id`). It captures **100% of turn-linked Model Armor blocks** and **7 of 8 Model Armor findings** (including `UploadSessionFile` image scan errors).
   - A small subset of Model Armor logs (`client_name = 'VERTEX_AI'` floor-setting checks with no `assist_token` or `trace_id`, plus aborted stream checks with no user activity log) have no user turn to attach to. Similarly, the 89 legacy connector DLP scan rows in `Stage_SDP_Default_Logging.v2_Stage_Default_Logging_table` are historical (`2026-08-14` to `2026-09-08`, prior to the `2026-09-29` activity sink cutover). If L2 security auditors ever want to inspect unlinked raw Model Armor checks alongside turn-linked checks on Page 4, they can add `vibelift_curated.v_model_armor_curated` as a secondary data source for a standalone raw-checks table.

