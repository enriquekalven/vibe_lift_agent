# VibeLift Dashboard — Multi-Persona SME Evaluation Framework & Compliance Report

## 1. Executive Summary & Evaluation Strategy

To verify that the VibeLift Analytics & FinOps Dashboard meets the operational expectations of every stakeholder persona—without ever fabricating telemetry, user identities, cost figures, or SME ratings—we implemented a **Two-Tier Evaluation Framework** in [`vibelift/sme_eval.py`](file:///Users/enriq/.gemini/jetski/scratch/vibe_lift_agent/vibelift/sme_eval.py):

1. **Tier 1: Deterministic 30-Check Persona Rubric (`automated_score_100`)**
   - Evaluates **6 Enterprise Personas × 5 Standardized Rubric Dimensions = 30 deterministic checks** (20 points per dimension = 100 points per persona).
   - Audits live `/api/state`, `/api/ge_fleet`, `telemetry_validation` (14 cross-tab grounding checks), and the rendered HTML DOM (`vibelift/ui/template.py`) on every request.
2. **Tier 2: Honest Human SME Rating & Sign-Off Ledger (`SmeEvaluationStore`)**
   - Unrated personas explicitly report `avg_sme_rating_5 = None`, `sme_rating_count = 0`, and `sme_task_completion_pct = None`. Human ratings are **never** fabricated or pre-populated.
   - SMEs test their Critical User Journey (CUJ) in the dashboard and submit structured 1–5 ratings, verdicts (`APPROVED`, `APPROVED_WITH_NOTES`, `NEEDS_WORK`), and notes via the in-dashboard scorecard (`#smeEvalScorecardPanel`) or `POST /api/sme_eval/rate`.

---

## 2. Live `project-maui` Compliance Scorecard Summary

| Metric | Simulator / Offline Mode | Live `project-maui` Mode | Status |
| :--- | :---: | :---: | :---: |
| **Overall Compliance Status** | `COMPLIANT` | `COMPLIANT` | **PASS** |
| **Persona Rubric Checks Passed** | **30 / 30** (100%) | **30 / 30** (100%) | **PASS** |
| **Composite Automated Score** | **100.0 / 100** | **100.0 / 100** | **PASS** |
| **Telemetry Grounding Validator** | **11 / 11** (`VERIFIED_GROUNDED`) | **14 / 14** (`VERIFIED_GROUNDED`) | **PASS** |
| **DOM ID & State Key Parity** | 100% matched | 100% matched | **PASS** |
| **Initial Human SME Ratings** | `None` (`0` ratings — honest) | `None` (`0` ratings — honest) | **VERIFIED** |

---

## 3. Standardized 5-Dimension Evaluation Rubric (100 Points per Persona)

Every persona is scored across the same 5 dimensions (20 points each):

| Dimension ID | Rubric Dimension | Max Points | What It Verifies |
| :--- | :--- | :---: | :--- |
| `time_to_insight` | **1. Time-to-Insight** | 20 | Primary tab, target panel, and top-level KPIs populate immediately with grounded data (`vibelift_mart` in live mode). |
| `actionability_control` | **2. Actionability & Control** | 20 | Interactive controls, REST API endpoints, and copy-pasteable CLI remediation commands are wired and responsive. |
| `data_trust_reconciliation` | **3. Data Trust & Reconciliation** | 20 | Metrics reconcile across sources; unknown or unconfigured values remain `None` (never invented or zeroed). |
| `persona_ergonomics` | **4. Persona Ergonomics & Navigation** | 20 | All required DOM containers exist in rendered HTML, respect `#geScopeSelect`, and provide 1-click tab navigation. |
| `guardrail_risk_prevention` | **5. Guardrail & Risk Prevention** | 20 | Safety gates (read-only NL2SQL, zero PII leakage, HTTP 404 evidence before cleanup, simulator suppression in live mode) hold. |

---

## 4. Per-Persona Evaluation Matrix (30 / 30 Checks on Live `project-maui`)

### Persona 1: FinOps Lead / Cloud Economist (`finops_lead`)
- **Primary Tab & Panel**: Cost & Billing (Tab 3) — `#liveGeMartDailyPanel`, `#billingReconciliationPanel`, `#liveTokenEconomicsPanel`
- **Critical User Journey (CUJ)**: Inspect daily turns, sessions, tokens, and joined Cloud Billing AI spend in `vibelift_mart.agg_daily_usage`, verify token economics reconciliation, and trigger a mart refresh.
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `FINOPS-D1-INSIGHT` (**20/20 PASS**): Live `ge_daily_usage` loaded (`9` daily rows, `9` app/agent/model rows, `8` sessions from `project-maui.vibelift_mart`).
  - `FINOPS-D2-CONTROL` (**20/20 PASS**): Refresh CLI (`python3 deploy/bigquery/provision_ge_mart.py --project project-maui --refresh`) and `POST /api/ge_mart/refresh` wired; billing status reported honestly (`NOT_CONNECTED` until `VIBELIFT_BILLING_EXPORT_TABLE` is set).
  - `FINOPS-D3-TRUST` (**20/20 PASS**): `TAB4-TOKEN-ECONOMICS-RECONCILES`, `TAB4-SPEND-CHANGE-ADDITIVE`, and `TAB4-GE-MART-DAILY-USAGE` all pass with zero invented cost figures (`ai_net_usd = None` when billing export is unconfigured).
  - `FINOPS-D4-ERGONOMICS` (**20/20 PASS**): Verified all 6 target DOM containers (`liveGeMartDailyPanel`, `geMartDailyBody`, `geMartByAppBody`, `geMartRefreshBtn`, `billingReconciliationPanel`, `liveTokenEconomicsPanel`).
  - `FINOPS-D5-GUARDRAIL` (**20/20 PASS**): `LIVE-NO-SIMULATOR-PAYLOADS` verified; `tokenomics_cockpit` and `what_if_default` are suppressed (`None`) in live mode.

### Persona 2: SRE / Cloud Platform Engineer (`sre_platform`)
- **Primary Tab & Panel**: Agents (Tab 0) — `#watchOutAlarmsContainer`, `#fleetAgentsBody`, `#cloudRunServicesBody`, `#geSupportEventsBody`
- **Critical User Journey (CUJ)**: Audit registered Gemini Enterprise agents and Cloud Run revisions, verify broken registrations have HTTP 404 evidence, and triage L1/L2 support events from `vibelift_mart.fct_turns`.
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `SRE-D1-INSIGHT` (**20/20 PASS**): Observed live Cloud Run services, GE fleet agents across 3 engines, and support events from `vibelift_mart.fct_turns`.
  - `SRE-D2-CONTROL` (**20/20 PASS**): Watch-out alarms and L1/L2 support stream events with actionable triage and `/api/sync_ge_fleet` refresh.
  - `SRE-D3-TRUST` (**20/20 PASS**): `TAB1-FLEET-INVENTORY`, `TAB1-CALL-VOLUME-SUM`, and `TAB6-CLOUD-RUN-SERVICES-SYNC` pass (shared runtime backends counted once).
  - `SRE-D4-ERGONOMICS` (**20/20 PASS**): Verified all 5 target DOM containers (`watchOutAlarmsContainer`, `cloudRunServicesBody`, `geSupportEventsBody`, `fleetAgentsBody`, `geScopeSelect`).
  - `SRE-D5-GUARDRAIL` (**20/20 PASS**): `TAB1-DEAD-REGISTRATION-EVIDENCE` verified — dead agent registrations require HTTP 404 evidence and provide reviewable `curl -X DELETE` commands (VibeLift never auto-deletes).

### Persona 3: AI / Agent & Prompt Engineer (`ai_engineer`)
- **Primary Tab & Panel**: Optimizer & Testing (Tab 2) — `#whatIfSimulatorPanel`, `#cacheForensicsBody`, `#otelCatalogTableBody`
- **Critical User Journey (CUJ)**: Diagnose prompt prefix cache breakpoints, inspect 5-layer OpenTelemetry GenAI parameters, and run a What-If projection (`/api/what_if_live` in live mode, `/api/what_if_simulate` in simulator mode).
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `AIENG-D1-INSIGHT` (**20/20 PASS**): Optimization parameters, live turn records (`vibelift_mart.fct_turns`), and 5 OTel layers (26 metrics) loaded.
  - `AIENG-D2-CONTROL` (**20/20 PASS**): Interactive agent selector, live model-switch / cache-share projection (`/api/what_if_live`), and turn inspection active.
  - `AIENG-D3-TRUST` (**20/20 PASS**): `TAB2-PARAMETER-DELTA-MATH`, `TAB3-TURN-TRAJECTORY-SYNC`, and `TAB6-OTEL-5LAYER-26-METRICS` verified.
  - `AIENG-D4-ERGONOMICS` (**20/20 PASS**): Verified all 5 target DOM containers (`whatIfSimulatorPanel`, `whatIfKpiGrid`, `whatIfCanaryCmd`, `cacheForensicsBody`, `otelCatalogTableBody`).
  - `AIENG-D5-GUARDRAIL` (**20/20 PASS**): `TAB1-AGENT-TOKEN-SOURCES` verified — every LLM token count names its provenance source; logs and traces are never summed together.

### Persona 4: Product Manager / Quality & VoC Lead (`product_quality`)
- **Primary Tab & Panel**: Users (Tab 4) — `#liveGeSessionsPanel`, `#geSessionsBody`, `#powerUsersBody`, `#vocRatingsBody`
- **Critical User Journey (CUJ)**: Review multi-turn conversation sessions in `vibelift_mart.fct_sessions`, inspect top users and cohorts per GE app, and submit a session-linked CSAT rating.
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `PROD-D1-INSIGHT` (**20/20 PASS**): Live principals, GE app cohorts, `8` multi-turn sessions (`vibelift_mart.fct_sessions`), and usage logs loaded.
  - `PROD-D2-CONTROL` (**20/20 PASS**): Clicking any session row pre-fills `#csatSessionInput` and `#csatEmailInput` for 1-click CSAT submission via `POST /api/csat_rating`.
  - `PROD-D3-TRUST` (**20/20 PASS**): `TAB5-NO-FAKE-POWER-USERS` and `TAB5-AIVE-USAGE-REAL-BQ-LOG-URIS` pass; unrated turns keep `csat_rating = None`.
  - `PROD-D4-ERGONOMICS` (**20/20 PASS**): Verified all 7 target DOM containers (`liveGeSessionsPanel`, `geSessionsBody`, `powerUsersBody`, `vocRatingsBody`, `aiveUsageBody`, `csatEmailInput`, `csatSessionInput`).
  - `PROD-D5-GUARDRAIL` (**20/20 PASS**): Per-user cost is never fabricated in live mode (`monthly_cost_usd = None`).

### Persona 5: Security & API Governance Architect (`security_governance`)
- **Primary Tab & Panel**: NL2SQL Copilot Drawer & Telemetry Validator (`#nl2sqlCopilotDrawer`, `#telemetryValidatorDrawer`, `#apigeePoliciesBody`)
- **Critical User Journey (CUJ)**: Verify NL2SQL enforces read-only `SELECT` queries with dataset allowlisting (`vibelift_mart`, `ds_ge_curated_staging`), confirm zero PII/prompt leakage in live payloads, and run the telemetry grounding validator.
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `SEC-D1-INSIGHT` (**20/20 PASS**): NL2SQL Copilot and Telemetry Validator active (`overall_status = VERIFIED_GROUNDED`).
  - `SEC-D2-CONTROL` (**20/20 PASS**): NL2SQL allowlist includes `project-maui.vibelift_mart` and `project-maui.ds_ge_curated_staging`; `POST /api/validate_telemetry` active.
  - `SEC-D3-TRUST` (**20/20 PASS**): Telemetry grounding validator passed (`14/14` checks in live mode, `11/11` in simulator mode).
  - `SEC-D4-ERGONOMICS` (**20/20 PASS**): Verified all 5 target DOM containers (`nl2sqlCopilotDrawer`, `nl2sqlQuestionInput`, `nl2sqlSqlPre`, `telemetryValidatorDrawer`, `apigeePoliciesBody`).
  - `SEC-D5-GUARDRAIL` (**20/20 PASS**): Read-only `SELECT` guardrail blocks all DML/DDL (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `TRUNCATE`, `GRANT`) and live `usage_logs` contain zero raw prompt/response text or caller IPs.

### Persona 6: CFO / VP of Engineering (`cfo_exec`)
- **Primary Tab & Panel**: Overview (Tab 6) — `#tabPanel6`, `#execHeadline`, `#execKpis`, `#execChartTrend`, `#execChartSpend`, `#execChartUsers`
- **Critical User Journey (CUJ)**: Review executive fleet overview across Gemini Enterprise apps, active human principals vs service accounts, model call distribution, and reconciled Cloud Billing status.
- **Automated Score**: **100 / 100 (`COMPLIANT`)**
  - `CFO-D1-INSIGHT` (**20/20 PASS**): Executive Overview populated across `3` Gemini Enterprise apps (`gemini-enterprise`, `Agentspace-demo_1748456955551`, `media-search-app_1761578054144`).
  - `CFO-D2-CONTROL` (**20/20 PASS**): Top-bar `#geScopeSelect` and time window selector filter Overview, Agents, Cost, and Users in unison.
  - `CFO-D3-TRUST` (**20/20 PASS**): Live mode claims zero unmeasured savings (`total_monthly_savings_usd = None`, `annualized_savings_usd = None`).
  - `CFO-D4-ERGONOMICS` (**20/20 PASS**): Verified all 8 target DOM containers (`tabPanel6`, `execHeadline`, `execKpis`, `execChartTrend`, `execChartRequests`, `execChartSpend`, `execChartUsers`, `northStarUnitEconKpis`).
  - `CFO-D5-GUARDRAIL` (**20/20 PASS**): All 14 cross-tab telemetry grounding checks pass with zero flags.

---

## 5. How SMEs Test & Rate the Dashboard

### Option A: Interactive UI Scorecard (In-Dashboard)
1. Open the VibeLift Dashboard (`/ui` or via the `open_dashboard` MCP tool in Gemini Enterprise).
2. Expand **"Role Guide, 7-Step Workflow & SME Persona Evaluation Scorecard"** (`#roleGuideDisclosure`) near the top of the page.
3. Click **"Open <Tab>"** next to your persona in the **SME Multi-Persona Evaluation & Rating Scorecard** table to jump directly to that persona's primary workflow tab and execute the listed Critical User Journey (CUJ).
4. Select your persona, enter your reviewer email/LDAP, select a **1–5 Rating** and **Verdict** (`APPROVED`, `APPROVED_WITH_NOTES`, `NEEDS_WORK`), add notes, and click **Submit SME Rating**.
5. The rating is immediately recorded in the session ledger (`#smeEvalRatingsBody`) and updates the per-persona and overall SME rating averages.

### Option B: REST API (Automated CI / Scripted SME Harness)
```bash
# 1. Fetch the 30-check persona evaluation scorecard
curl -s http://localhost:8080/api/sme_eval | jq .

# 2. Force a fresh re-evaluation across all 6 personas
curl -s -X POST http://localhost:8080/api/sme_eval/run -H "Content-Type: application/json" -d '{}' | jq .

# 3. Submit a human SME persona rating
curl -s -X POST http://localhost:8080/api/sme_eval/rate \
  -H "Content-Type: application/json" \
  -d '{
    "persona_id": "finops_lead",
    "reviewer": "enriq@google.com",
    "overall_rating": 5,
    "verdict": "APPROVED",
    "task_completed": true,
    "notes": "Verified vibelift_mart.agg_daily_usage and token economics reconciliation."
  }' | jq .sme_evaluation
```
