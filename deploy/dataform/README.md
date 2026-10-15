# FinOps Reporting Mart Dataform Package (`deploy/dataform`)

Declarative Dataform SQLX/JS pipeline for `ds_ge_curated_staging` and `finops_mart`, paired with the Terraform data plane module in [`deploy/terraform/`](../terraform/README.md) and the schema-resilient compiler in [`deploy/bigquery/provision_ge_mart.py`](../bigquery/provision_ge_mart.py).

## DAG Structure

1. **Sources (`definitions/sources.js`)**:
   - `ds_ge_assistant_raw.discoveryengine_googleapis_com_gemini_enterprise_user_activity`
   - `ds_ge_search_raw.discoveryengine_googleapis_com_gemini_enterprise_user_activity`
   - `ds_vertex_agents_raw.discoveryengine_googleapis_com_gen_ai_client_inference_operation_details`
   - `ds_vertex_agents_raw.gen_ai_client_inference_operation_details`
   - `ds_ge_audit_raw.cloudaudit_googleapis_com_data_access`
   - `ds_security_guardrails_raw.cloudaudit_googleapis_com_data_access`
2. **Curated Staging (`definitions/curated/curated_staging_views.js`)**:
   - `ds_ge_curated_staging.v_user_activity_curated`
   - `ds_ge_curated_staging.v_agentic_operations_curated`
   - `ds_ge_curated_staging.v_consolidated_audit_log`
   - `ds_ge_curated_staging.v_model_armor_curated`
3. **Reporting Mart (`definitions/mart/*.sqlx`)**:
   - `finops_mart.v_fct_turns` (View)
   - `finops_mart.fct_turns` (Partitioned & Clustered Materialized Table)
   - `finops_mart.fct_sessions` (Session Rollup View)
   - `finops_mart.agg_daily_usage` (Daily Engine/Agent/Model Usage View)
   - `finops_mart.v_looker_l1_l2_support` (Looker Studio L1/L2 Support Triage View)
