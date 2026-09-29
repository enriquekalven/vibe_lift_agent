# Testing

84 tests across 4 files. They run offline: `GOOGLE_APPLICATION_CREDENTIALS` points at a file that does not
exist, and every Google Cloud API is replaced by an in-process fake (`FakeGoogleApi` in
[`tests/test_fleet.py`](../tests/test_fleet.py)), so no live project is touched.

```bash
python -m unittest discover -s tests -t .        # full suite, ~20 s
python -m unittest tests.test_live_data_sources  # one file
```

## Where the suite runs

| Gate | What runs | Blocks |
|---|---|---|
| [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) | full suite, Python 3.12, pinned deps | merge (PR check) |
| [`deploy/cloudbuild.yaml`](../deploy/cloudbuild.yaml) `test` step | full suite inside the built image | push and deploy |
| [`deploy/deploy_cloud_run.sh`](../deploy/deploy_cloud_run.sh) | full suite before `gcloud run deploy` | deploy (`VIBELIFT_SKIP_TESTS=1` overrides, with a warning) |

## Requirement → tests

The product rule is: every number on the dashboard comes from telemetry, and anything that cannot be measured
is shown as unknown, never as an invented value. These tests enforce it.

| Requirement | Tests |
|---|---|
| **No invented numbers.** Missing telemetry shows as unknown, not zero | `test_live_data_sources.TokenSourceJoinTest` (`test_traffic_without_telemetry_is_unknown_not_zero`, `test_source_failure_is_unknown_not_zero`); `test_honest_metrics.JudgeFallbackTest.test_missing_score_is_not_invented` |
| No modelled savings or simulator payloads in live mode | `test_honest_metrics.LiveUserAnalyticsTest.test_live_payload_has_no_modelled_savings`; `test_live_data_sources.LiveValidatorChecksTest.test_simulator_payload_and_missing_evidence_flagged` |
| Token counts: each LLM call counted once; logs and traces never double-counted | `TraceTokensTest`, `GeAssistantTraceTokensTest`, `TokenSourceJoinTest.test_logs_preferred_and_never_summed_with_traces` |
| **Dead agents need evidence.** Only an HTTP 404 marks a registration dead; a failed lookup is "unverified" | `RegistrationTest` (`test_404_is_backend_not_found_with_cleanup_command`, `test_lookup_failure_is_unverified_not_dead`) |
| FinOps figures reconcile exactly | `LiveFinopsTest` (drift drivers sum to the observed change; token economics match model usage; projections use observed tokens) |
| Validator and LLM judge: the judge cannot override a failed deterministic check | `test_honest_metrics.JudgeFallbackTest`; `LiveValidatorChecksTest.test_live_payload_passes`; `test_vibelift...test_telemetry_grounding_validator_and_live_gcp_no_fake_users` |
| Fleet inventory is complete; one failing source or region does not break the rest | `test_fleet.GeFleetCollectionTest` (`test_failed_sources_degrade_independently`, `test_total_outage_still_returns_payload`); `GeFleetDiscoveryTest` |
| Shared runtimes counted once in totals | `GeFleetRuntimeDedupeTest`; `test_honest_metrics.LiveUserAnalyticsTest.test_alerts_derived_from_fleet_once_per_runtime` |
| Time window is clamped and applied to every query | `GeFleetCollectionTest.test_window_is_clamped_and_applied_to_queries`; `RequestTrendTest` |
| **Privacy.** Prompt and response text never reaches the payload | `GeFleetCollectionTest.test_log_message_content_never_reaches_payload` |
| **Security.** SQL injection rejected; errors don't leak internals; hosts validated | `BillingExportTest.test_sql_rejects_injection`; `DeploymentHardeningTest` (`test_mcp_errors_do_not_leak_exception_details`, `test_base_url_from_headers_rejects_malformed_hosts`) |
| Deploy stays private (no `allUsers`, no `:latest`, env vars merged) | `DeploymentHardeningTest.test_deploy_paths_stay_private_and_merge_env_vars` |
| No hardcoded project, URL or fake agents | `DeploymentHardeningTest` (`test_unconfigured_project_never_resolves_to_a_real_project`, `test_open_dashboard_does_not_publish_a_hardcoded_url`, `test_real_ge_agents_and_sub_second_mcp_open_dashboard`) |
| Dependency pins consistent across requirements, constraints and pyproject | `DeploymentHardeningTest.test_dependency_pins_are_consistent` |
| Dashboard renders; JS is valid; every DOM id the JS uses exists | `VibeLiftFrameworkTest.test_http_server_renders_ui_and_executes_rest_api_end_to_end`, `test_dashboard_javascript_syntax_is_valid_and_pip_configured`, `test_all_js_dom_ids_exist_in_rendered_html_and_all_state_keys_surfaced` |
| MCP / A2A / ADK surfaces work | `test_mcp_server_sync_dispatch_coverage`, `ProductionAppTest`, `test_adk_agent_tools` |

## Adding a test

Put it in the file for the module it covers: `test_fleet.py` (collection and inventory),
`test_live_data_sources.py` (tokens, registrations, live FinOps, validator live checks),
`test_honest_metrics.py` (billing, judge, live analytics), `test_vibelift.py` (HTTP, MCP, UI, deploy config).
Any new dashboard number should come with a test showing it is `None` (not `0`) when its source is missing.

> Tests were written alongside the features, not strictly test-first. The requirement map above is the
> contract: a change that breaks one of these rules should fail a test.
