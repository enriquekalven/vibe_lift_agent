# VibeLift: hackathon submission

The 7 fields the evaluator expects. Every file, route and resource named here exists in this repository; `tests/test_docs_integrity.py` checks that the docs' links, anchors, file paths and URLs resolve, that the tool, test and tab counts match the code, and that every documented endpoint and `VIBELIFT_*` variable exists. `tests/test_no_invented_values.py` fails if the dashboard substitutes a hardcoded number for missing data. The numbers under "How it saves effort" were measured in `project-maui`, and each one is labeled as either a measurement or a projection.

---

## 1. `solution_name`
**VibeLift: Gemini Enterprise agent fleet observability and prompt-cache FinOps**

## 2. `arch_url`
https://github.com/enriquekalven/vibe_lift_agent/blob/main/docs/ARCHITECTURE.md

The diagram is Mermaid source (diagram as code). Every box names the file or Google Cloud resource behind it. The legend marks each part as live, simulated or not configured, and the page ends with a known-gaps table.

## 3. `code_base_url`
https://github.com/enriquekalven/vibe_lift_agent#deploy-in-your-own-gcp-project

---

## 4. `About the solution`
Teams that run agents in **Gemini Enterprise** can't easily see which agents exist across apps and regions, what they cost in tokens, or why prompt caching isn't saving money. VibeLift answers those questions from live Google Cloud data.

It is a private **Cloud Run** service (`vibe-lift-agent`, IAM `run.invoker` only) with two front ends. One is an **MCP server** (`/mcp`, Streamable HTTP, JSON-RPC 2.0, MCP `2025-06-18`, 9 tools) that Gemini Enterprise calls through a Custom MCP Server data store; it renders the dashboard in the side panel as an MCP App. The other is an **ADK agent** (`app/agent.py`, Gemini 2.5 Flash on Vertex AI). Both call the same in-process controller.

What it does, by source file:
1. **Fleet inventory and telemetry** ([vibelift/fleet.py](../vibelift/fleet.py), [vibelift/gcp_telemetry.py](../vibelift/gcp_telemetry.py)): lists agents in every Gemini Enterprise app in `global`, `us` and `eu` through the Discovery Engine API, finds unregistered Agent Engine, Cloud Run and GKE workloads, and joins them with Cloud Monitoring, OpenTelemetry `gen_ai` logs and Cloud Trace. A source that fails returns `null` and appears in `source_status`; no numbers are made up.
2. **BigQuery mart** ([deploy/setup_bigquery_sink.sh](../deploy/setup_bigquery_sink.sh), [deploy/bigquery/provision_ge_mart.py](../deploy/bigquery/provision_ge_mart.py), [deploy/setup_mart_refresh.sh](../deploy/setup_mart_refresh.sh)): 7 Cloud Logging sinks feed raw datasets, then curated views, then `vibelift_mart` (`fct_turns`, `fct_sessions`, `agg_daily_usage`). An hourly BigQuery Data Transfer scheduled query fully rebuilds `fct_turns`.
3. **Cost, cache analysis and AlphaEvolve rewriter** ([vibelift/finops.py](../vibelift/finops.py), [vibelift/prompt_xray.py](../vibelift/prompt_xray.py), [vibelift/billing_export.py](../vibelift/billing_export.py), [experiments/prompt_cache_evolve/](../experiments/prompt_cache_evolve/)): measured token counts times the list prices in `RATE_CARDS`, per model, user and session. A model without a rate card is left unpriced and counted, never priced with another model's card. The Prompt Cache X-Ray shows the exact line and column where a prompt prefix stops being cacheable and runs the **AlphaEvolve-evolved `propose_cache_friendly_rewrite` algorithm** (`0.7321 -> 0.9957` composite score across 12 multi-turn enterprise prompt benchmarks, handling multi-line JSON objects, fenced ````json` blocks, and mixed static/volatile key partitioning). The Cloud Billing export reader is built (`VIBELIFT_BILLING_EXPORT_TABLE`) and clearly labels every number as a list-price estimate when unconfigured.
4. **Grounding validator** ([vibelift/validator.py](../vibelift/validator.py)): deterministic checks on the dashboard numbers, plus an optional Vertex AI Gemini judge.
5. **Validated ingest and Cloud Pub/Sub DLQ** ([vibelift/telemetry.py](../vibelift/telemetry.py)): `@vibelift_telemetry` and `POST /api/decorator_ingest`. Invalid bodies get HTTP 422, a values-free dead-letter record (`GET /api/dead_letters`), and durable publishing to the Cloud Pub/Sub dead-letter topic (`VIBELIFT_DLQ_PUBSUB_TOPIC`, `projects/project-maui/topics/vibelift-ingest-dlq`).
6. **Plain-English search** ([vibelift/optimizer.py](../vibelift/optimizer.py), [vibelift/server.py](../vibelift/server.py)): a question is matched by keyword to one of 5 fixed read-only SELECT templates over `vibelift_mart`; the question text never enters the SQL. `verify_nl2sql_sql` re-checks the SQL and fails closed, and the query runs on BigQuery with a 100 MB `maximumBytesBilled` cap. A question asking to change data returns `BLOCKED`.
7. **Live A2A task execution and live-mode simulator gating** ([vibelift/server.py](../vibelift/server.py), [vibelift/ui/template.py](../vibelift/ui/template.py)): serves both the A2A agent card (`GET /.well-known/agent.json`) and live A2A JSON-RPC 2.0 task execution (`POST /a2a/app` for `message/send` and `tasks/send`). In live GCP mode on `main`, simulator UI panels and endpoints (`inject_anomaly`, `step_turn`, `evolve_generation`, `simulate_what_if`, `recompute_finops`) are hidden and blocked with `DISABLED_IN_LIVE_MODE` (`HTTP 403`), while the full interactive What-If Simulator is preserved on `feature/what-if-simulator`.

## 5. `How it saves effort`
**Effort replaced.** One view and one MCP tool call replace separate queries to 8 Google Cloud APIs: Discovery Engine (per location, engine and assistant), Vertex AI, Cloud Monitoring, Cloud Logging, Cloud Trace, Cloud Run, GKE and BigQuery. Today an operator joins those by hand. We have not timed the manual process, so we don't claim hours saved.

**Measured in `project-maui`** (`vibelift_mart.fct_turns`, the 30 days ending 2026-10-07 03:42 UTC):
- 194 turns; 20,177,951 input tokens, 7,564,372 of them served from the prompt cache, so the **cache hit rate is 37.5%**.
- `gemini-3.5-flash` accounts for 95% of input tokens. At its list price, **uncached input makes up about 92.5% of that model's estimated token cost**, and output is under 2%.
- Estimated 30-day token cost at list price: about **$20.05** (the 114 turns with no logged model are left unpriced). This is a small demo project.

**Measured AlphaEvolve prompt-cache benchmark & cost projection:**
- On the 12-case multi-turn enterprise prompt benchmark in `experiments/prompt_cache_evolve/`, the evolved `propose_cache_friendly_rewrite` algorithm raises the prefix cacheable ratio from **10.9% to 99.6%** (`composite_cache_savings_score`: `0.732105` -> `0.995733`, `+36.0%` relative improvement over the single-line baseline).
- **Projection on `project-maui` traffic:** raising `gemini-3.5-flash`'s cache hit rate from 37.5% to 90% cuts its list-price token cost by about **70%** ($19.49 to $5.85 for the same tokens).

## 6. `How it was happening previously?`
1. **Inventory:** each Gemini Enterprise location (`global`, `us`, `eu`), app and assistant has to be listed separately in the console or through the Discovery Engine API. Agent Engine regions, Cloud Run services and GKE workloads are listed in other consoles.
2. **Cost:** Cloud Billing reports cost by project and SKU, not by Gemini Enterprise agent, session or user. Token counts sit in `gen_ai` log entries and Monitoring metrics that nobody joins to prices.
3. **Caching:** nothing shows *why* a prompt prefix misses the cache. Engineers compare prompts by eye.

## 7. `Deployment Readiness`
**Enterprise-ready architecture deployed in `project-maui` with automated customer-org security and governance scripts.**

Deployed and verified in `project-maui`:
- `deploy/deploy_cloud_run.sh` runs `compileall`, `ruff` and the offline test suite, and aborts on failure. It then creates the runtime SA `vibe-lift-runtime-sa` and the custom role `vibeLiftGeFleetReader`, and deploys the private service (`--no-allow-unauthenticated`, 1 vCPU / 1 GiB, 1–10 instances) with `VIBELIFT_DLQ_PUBSUB_TOPIC` and `VIBELIFT_PII_REDACT`. Finally it grants the Discovery Engine service agent `roles/run.invoker`. A request without a token gets HTTP 403.
- `deploy/setup_enterprise_perimeter.sh` provisions:
  - **Cloud Armor WAF policy `vibelift-waf-policy`** (Rule 1000: OWASP SQLi + XSS `deny(403)`; Rule 2000: `120 req/min` IP rate-based ban) and **Serverless NEG `vibelift-dashboard-neg`** for the `vibe-lift-dashboard` surface (`VIBELIFT_SURFACE` / `VIBELIFT_SPLIT_EDGE=1`).
  - **Cloud Pub/Sub dead-letter topic `vibelift-ingest-dlq`** and subscription `vibelift-ingest-dlq-sub` with least-privilege `roles/pubsub.publisher` for `vibe-lift-runtime-sa`.
  - **BigQuery Data Catalog Policy Tag column masking** (`build_pii_policy_tag_ddl`, `VIBELIFT_POLICY_TAG_PII`) and runtime SHA-256 PII email pseudonymization (`VIBELIFT_PII_REDACT=1`).
  - **VPC Service Controls perimeter `vibelift_enterprise_perimeter`** when `ORG_ID` and `ACCESS_POLICY_ID` are provided by a customer organization admin.
- `deploy/register_ge_agent.sh` and `deploy/setup_mcp_connector.py` create the Custom MCP Server data store, enable its 9 tools, link it to a Gemini Enterprise app, and optionally register the A2A agent (`VIBELIFT_PUBLISH_A2A=1`) backed by `POST /a2a/app`.
- `deploy/setup_bigquery_sink.sh`, `deploy/setup_mart_refresh.sh`, and `POST /api/ge_mart/refresh` create the 7 sinks, the mart, and partition-pruned incremental `MERGE INTO` refreshes (`build_incremental_merge_fct_turns_dml`).
- The dashboard is served with a Content-Security-Policy (`default-src 'self'`, `object-src 'none'`, `frame-ancestors 'self'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- CI (`.github/workflows/tests.yml`) runs `ruff`, `codespell`, `mypy`, a secret scan, the 344-test offline suite with a >90% coverage gate, and a smoke test on every push.

Known gaps (also in [docs/ARCHITECTURE.md](ARCHITECTURE.md#known-gaps)):
- In `project-maui`, organization-level `accesscontextmanager` permissions and Cloud Billing export dataset configuration are not enabled on the shared demo billing account, so `deploy/setup_enterprise_perimeter.sh` skips the org-level VPC-SC creation step unless `ACCESS_POLICY_ID` is passed, and FinOps uses `RATE_CARDS` list prices unless `VIBELIFT_BILLING_EXPORT_TABLE` is set.
