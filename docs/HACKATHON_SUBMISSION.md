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
3. **Cost and cache analysis** ([vibelift/finops.py](../vibelift/finops.py), [vibelift/prompt_xray.py](../vibelift/prompt_xray.py), [vibelift/billing_export.py](../vibelift/billing_export.py)): measured token counts times the list prices in `RATE_CARDS`, per model, user and session. A model without a rate card is left unpriced and counted, never priced with another model's card. The Prompt Cache X-Ray shows the line and column where a prompt prefix stops being cacheable. The Cloud Billing export reader is built but not configured here, so the Cost tab says so and labels every number as a list-price estimate.
4. **Grounding validator** ([vibelift/validator.py](../vibelift/validator.py)): deterministic checks on the dashboard numbers, plus an optional Vertex AI Gemini judge.
5. **Validated ingest** ([vibelift/telemetry.py](../vibelift/telemetry.py)): `@vibelift_telemetry` and `POST /api/decorator_ingest`. Invalid bodies get HTTP 422 and a values-free dead-letter record.
6. **Plain-English search** ([vibelift/optimizer.py](../vibelift/optimizer.py), [vibelift/server.py](../vibelift/server.py)): a question is matched by keyword to one of 5 fixed read-only SELECT templates over `vibelift_mart`; the question text never enters the SQL. `verify_nl2sql_sql` re-checks the SQL and fails closed, and the query runs on BigQuery with a 100 MB `maximumBytesBilled` cap. A question asking to change data returns `BLOCKED`.
7. **Optimizer simulator** ([vibelift/optimizer.py](../vibelift/optimizer.py)): labeled as a simulator everywhere. It applies fixed factors to demo profiles, calls no model and changes no agent.

## 5. `How it saves effort`
**Effort replaced.** One view and one MCP tool call replace separate queries to 8 Google Cloud APIs: Discovery Engine (per location, engine and assistant), Vertex AI, Cloud Monitoring, Cloud Logging, Cloud Trace, Cloud Run, GKE and BigQuery. Today an operator joins those by hand. We have not timed the manual process, so we don't claim hours saved.

**Measured in `project-maui`** (`vibelift_mart.fct_turns`, the 30 days ending 2026-10-07 03:42 UTC):
- 194 turns; 20,177,951 input tokens, 7,564,372 of them served from the prompt cache, so the **cache hit rate is 37.5%**.
- `gemini-3.5-flash` accounts for 95% of input tokens. At its list price, **uncached input makes up about 92.5% of that model's estimated token cost**, and output is under 2%.
- Estimated 30-day token cost at list price: about **$20.05** (the 114 turns with no logged model are left unpriced). This is a small demo project.

**Projection, not a result:** raising `gemini-3.5-flash`'s cache hit rate from 37.5% to 90% would cut its list-price token cost by about **70%** ($19.49 to $5.85 for the same tokens). The Prompt Cache X-Ray shows which prompt lines block caching. No rewrite has been deployed or measured.

## 6. `How it was happening previously?`
1. **Inventory:** each Gemini Enterprise location (`global`, `us`, `eu`), app and assistant has to be listed separately in the console or through the Discovery Engine API. Agent Engine regions, Cloud Run services and GKE workloads are listed in other consoles.
2. **Cost:** Cloud Billing reports cost by project and SKU, not by Gemini Enterprise agent, session or user. Token counts sit in `gen_ai` log entries and Monitoring metrics that nobody joins to prices.
3. **Caching:** nothing shows *why* a prompt prefix misses the cache. Engineers compare prompts by eye.

## 7. `Deployment Readiness`
**Working prototype, deployed in one project.** It is not production-hardened; see the gaps below.

Deployed and verified in `project-maui`:
- `deploy/deploy_cloud_run.sh` runs `compileall`, `ruff` and the offline test suite, and aborts on failure. It then creates the runtime SA `vibe-lift-runtime-sa` and the custom role `vibeLiftGeFleetReader`, and deploys the private service (`--no-allow-unauthenticated`, 1 vCPU / 1 GiB, 1–10 instances). Finally it grants the Discovery Engine service agent `roles/run.invoker`. A request without a token gets HTTP 403.
- `deploy/register_ge_agent.sh` and `deploy/setup_mcp_connector.py` create the Custom MCP Server data store, enable its 9 tools and link it to a Gemini Enterprise app.
- `deploy/setup_bigquery_sink.sh` and `deploy/setup_mart_refresh.sh` create the sinks, the mart and the hourly refresh.
- The dashboard is served with a Content-Security-Policy (`default-src 'self'`, `object-src 'none'`, `frame-ancestors 'self'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. Its inline script still needs `'unsafe-inline'`.
- CI (`.github/workflows/tests.yml`) runs `ruff`, `codespell`, `mypy`, a secret scan, the offline test suite with a 90% coverage gate, and a smoke test on every push.

Known gaps (also in [docs/ARCHITECTURE.md](ARCHITECTURE.md#known-gaps)):
- No VPC Service Controls perimeter (needs organization-level Access Context Manager rights).
- No Cloud Armor in front of Cloud Run (Gemini Enterprise's MCP auth requires the `*.run.app` URL, and Cloud Armor needs an external Application Load Balancer with a custom domain and certificate). `VIBELIFT_SURFACE` (`mcp` vs `dashboard`) and `VIBELIFT_SPLIT_EDGE=1` in `deploy/deploy_cloud_run.sh` split the MCP endpoint from the `--iap` browser dashboard service.
- No Cloud Billing export (no access to billing export settings); costs are list-price estimates.
- Dead letters and decorator events are kept in memory per instance; there is no Pub/Sub dead-letter topic.
- The optimizer is a simulator. The service serves an A2A agent card, but not the A2A endpoint the card names (`/a2a/app`), so `deploy/register_ge_agent.sh` registers the A2A agent with Gemini Enterprise only when `VIBELIFT_PUBLISH_A2A=1` (not set here).
- The mart keeps `user_email` and prompt and response previews of up to 2,000 characters from Gemini Enterprise activity logs for a Looker support view, with no column masking yet.
