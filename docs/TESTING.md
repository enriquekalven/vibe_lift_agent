# Testing

339 tests across 16 files. They run offline: `GOOGLE_APPLICATION_CREDENTIALS` points at a file that does not
exist, and every Google Cloud API is replaced by an in-process fake (`FakeGoogleApi` in
[`tests/test_fleet.py`](../tests/test_fleet.py)), so no live project is touched.

```bash
python -m unittest discover -s tests -t .        # full suite, ~5 s
python -m unittest tests.test_live_data_sources  # one file
```

## Where the suite runs

| Gate | What runs | Blocks |
|---|---|---|
| [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) | full suite, Python 3.12, pinned deps | merge (PR check) |
| [`deploy/cloudbuild.yaml`](../deploy/cloudbuild.yaml) `test` step | full suite inside the built image | push and deploy, when you run `gcloud builds submit --config deploy/cloudbuild.yaml` (no trigger is configured) |
| [`deploy/deploy_cloud_run.sh`](../deploy/deploy_cloud_run.sh) | full suite before `gcloud run deploy` | deploy (`VIBELIFT_SKIP_TESTS=1` overrides, with a warning) |

## Requirement → tests

The product rule is: every number on the dashboard comes from telemetry, and anything that cannot be measured
is shown as unknown, never as an invented value. These tests enforce it.

| Requirement | Tests |
|---|---|
| **No invented numbers.** Missing telemetry shows as unknown, not zero | `test_live_data_sources.TokenSourceJoinTest` (`test_traffic_without_telemetry_is_unknown_not_zero`, `test_source_failure_is_unknown_not_zero`); `test_honest_metrics.JudgeFallbackTest.test_missing_score_is_not_invented` |
| No modelled savings or simulator payloads in live mode (including cold-start 0-turn live projects) | `test_honest_metrics.LiveUserAnalyticsTest` (`test_live_payload_has_no_modelled_savings`, `test_cold_start_live_gcp_mode_detected_in_validator`); `test_live_data_sources.LiveValidatorChecksTest.test_simulator_payload_and_missing_evidence_flagged` |
| Token counts: each LLM call counted once; logs and traces never double-counted; cloud turns deduplicated & bounded | `TraceTokensTest`, `GeAssistantTraceTokensTest`, `TokenSourceJoinTest.test_logs_preferred_and_never_summed_with_traces`, `test_long_running_agent_enforces_max_turns_and_deduplicates_cloud_turns` |
| **Dead agents need evidence.** Only an HTTP 404 marks a registration dead; a failed lookup is "unverified" | `RegistrationTest` (`test_404_is_backend_not_found_with_cleanup_command`, `test_lookup_failure_is_unverified_not_dead`) |
| FinOps figures reconcile exactly | `LiveFinopsTest` (drift drivers sum to the observed change; token economics match model usage; projections use observed tokens) |
| Validator and LLM judge: the judge cannot override a failed deterministic check | `test_honest_metrics.JudgeFallbackTest`; `LiveValidatorChecksTest.test_live_payload_passes`; `test_vibelift...test_telemetry_grounding_validator_and_live_gcp_no_fake_users` |
| Fleet inventory is complete; unregistered runtimes & MCP/Skill spans discovered; one failing source does not break the rest | `test_fleet.GeFleetCollectionTest` (`test_failed_sources_degrade_independently`, `test_total_outage_still_returns_payload`); `GeFleetDiscoveryTest` |
| Shared runtimes counted once in totals | `GeFleetRuntimeDedupeTest`; `test_honest_metrics.LiveUserAnalyticsTest.test_alerts_derived_from_fleet_once_per_runtime` |
| Time window is clamped and applied to every query; partition pruning (`_TABLE_SUFFIX`, `event_date`) & `maximumBytesBilled` enforced | `GeFleetCollectionTest.test_window_is_clamped_and_applied_to_queries`; `RequestTrendTest`; `test_domain_scoped_project_ids_and_fct_turns_partition_pruning`; `test_refresh_ge_mart_invalidates_all_caches_and_bq_rest_safeguards` |
| **Privacy.** Prompt and response text never reaches the payload (the opt-in Prompt Cache X-Ray live mode is the documented exception); ingested and rejected bodies keep no prompt text | `GeFleetCollectionTest.test_log_message_content_never_reaches_payload`; `test_session_drilldown.py`; `test_ingest_validation.py` |
| **Security.** SQL injection rejected (including domain-scoped GCP project IDs); errors don't leak internals; hosts validated | `BillingExportTest.test_sql_rejects_injection`; `test_domain_scoped_project_ids_and_fct_turns_partition_pruning`; `DeploymentHardeningTest` (`test_mcp_errors_do_not_leak_exception_details`, `test_base_url_from_headers_rejects_malformed_hosts`) |
| Deploy stays private (no `allUsers`, no `:latest`, env vars merged) | `DeploymentHardeningTest.test_deploy_paths_stay_private_and_merge_env_vars` |
| No hardcoded project, URL or fake agents | `DeploymentHardeningTest` (`test_unconfigured_project_never_resolves_to_a_real_project`, `test_open_dashboard_does_not_publish_a_hardcoded_url`, `test_real_ge_agents_and_sub_second_mcp_open_dashboard`) |
| Dependency pins consistent across requirements, constraints and pyproject | `DeploymentHardeningTest.test_dependency_pins_are_consistent` |
| Dashboard renders; JS is valid; every DOM id the JS uses exists | `VibeLiftFrameworkTest.test_http_server_renders_ui_and_executes_rest_api_end_to_end`, `test_dashboard_javascript_syntax_is_valid_and_pip_configured`, `test_all_js_dom_ids_exist_in_rendered_html_and_all_state_keys_surfaced` |
| MCP and ADK surfaces work (A2A is an agent card only) | `test_mcp_server_sync_dispatch_coverage`, `ProductionAppTest`, `test_adk_agent_tools`, `test_trigger_alpha_evolve_returns_newest_action_and_telemetry_hours` |
| **Invalid ingest is rejected, not stored.** HTTP 422 with reasons; values-free dead letters | `test_ingest_validation.py` |
| **The optimizer is labeled a simulator** everywhere it surfaces (UI, MCP, ADK, agent card, docs) | `test_simulator_labels.py` |
| Billing errors are visible with a fix hint; per-user cost is a labeled list-price estimate | `test_cost_visibility.py` |
| **Plain-English search is real and read-only.** The cap is sent to BigQuery; write or unverified SQL is never sent; the question never enters the SQL; demo rows are labeled | `test_nl2sql_and_pricing.py` (`Nl2SqlVerifierTest`, `RunBigQueryQueryCapTest`, `Nl2SqlControllerTest`) |
| **No guessed prices.** A model without a rate card is unpriced (`None`), never priced with another model's card; runtime compute is usage, not dollars | `FleetTokenPricingTest`; `GeFleetDiscoveryTest`; `test_no_invented_values.UnpricedTurnTest`; `LiveTurnHonestyTest.test_mart_turn_without_model_is_unpriced` |
| **Calculators price only what they are given.** The MCP and ADK cache calculators add no invented prompt, output or thinking tokens; an unknown model returns `priced: false` | `test_no_invented_values.CalculatorToolsTest` |
| **Logged turns are not dressed up.** A Cloud Logging turn with no model stays `unknown`; no cache breakpoint or evolution generation is invented | `test_no_invented_values.LiveTurnHonestyTest` |
| **The metric catalog is a reference list in live mode.** Simulated baseline/current values and example alarms are dropped; each metric key says whether it exists; VibeLift's hosting cost is not estimated | `test_no_invented_values.CatalogHonestyTest` |
| **No hardcoded dashboard fallbacks.** The rendered script has no `\|\| N` / `?? N` number defaults for data, no invented status or verification labels, and logged turns are never shown as static matches or priced when unpriced | `test_no_invented_values.DashboardFallbackTest` |
| Dashboard HTML carries the CSP and hardening headers on both server stacks; `VIBELIFT_SURFACE` (`mcp` vs `dashboard`) isolates the MCP edge from the browser dashboard | `DashboardSecurityHeadersTest`; `SurfaceIsolationTest` |
| **Docs match the code.** Counts, links, anchors, repo tree, env vars and endpoints in the docs are recomputed from the code | `test_docs_integrity.py` |
| Prompt Cache X-Ray & 30-Check SME Evaluation | `test_prompt_xray.py`, `test_sme_eval.py` |
| GE mart: unknown tokens, model, latency and rating stay `None`; spend is never modelled | `test_ge_mart.GeMartReaderTest` (`test_usage_log_keeps_unknowns_as_none`, `test_daily_usage_tokens_none_counts_zero`); `FleetSummaryFromMartTest.test_spend_is_never_modelled` |
| GE mart: inference tokens attached to one turn per trace (no fan-out); stage defects stay fixed | `test_ge_mart.TemplateDefectFixTest` |
| GE mart: provisioner renders every view, stubs missing or wrong-location sources, rejects bad identifiers | `test_ge_mart.ProvisionerTest` |
| Daily cost: `None` when billing is not connected or the day is outside the exported range; unit costs `None` on a 0 or unknown denominator | `test_ge_mart.DailyCostJoinTest` |

## Adding a test

Put it in the file for the module it covers: `test_fleet.py` (collection and inventory),
`test_live_data_sources.py` (tokens, registrations, live FinOps, validator live checks),
`test_honest_metrics.py` (billing, judge, live analytics, partition pruning & FinOps safeguards), `test_ge_mart.py` (GE mart provisioner, SQL
templates, mart readers, daily cost join), `test_prompt_xray.py` (Prompt Cache X-Ray), `test_session_drilldown.py` (session turn drilldowns), `test_sme_eval.py` (30-check SME rubric & rating ledger), `test_trace_logging.py` (trace logging switch), `test_ingest_validation.py` (ingest validation and dead letters), `test_simulator_labels.py` (simulator labels), `test_cost_visibility.py` (billing errors and per-user estimates), `test_nl2sql_and_pricing.py` (plain-English search, pricing, dashboard headers), `test_no_invented_values.py` (no guessed prices, calculator inputs or UI fallbacks), `test_docs_integrity.py` (docs match the code), `test_full_coverage.py` (unit coverage), `test_vibelift.py` (HTTP, MCP, UI, deploy config).
When you add or remove a test, update the count on the first line; `test_docs_integrity.py` checks it.
Any new dashboard number should come with a test showing it is `None` (not `0`) when its source is missing.

> Tests were written alongside the features, not strictly test-first. The requirement map above is the
> contract: a change that breaks one of these rules should fail a test.
