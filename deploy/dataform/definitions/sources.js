// Raw Cloud Logging sink declarations routed by deploy/terraform/
declare({
  schema: "ds_ge_assistant_raw",
  name: "discoveryengine_googleapis_com_gemini_enterprise_user_activity",
  description: "Gemini Enterprise Assistant raw user activity log sink"
});

declare({
  schema: "ds_ge_search_raw",
  name: "discoveryengine_googleapis_com_gemini_enterprise_user_activity",
  description: "Gemini Enterprise Search raw user activity log sink"
});

declare({
  schema: "ds_vertex_agents_raw",
  name: "discoveryengine_googleapis_com_gen_ai_client_inference_operation_details",
  description: "Gemini Enterprise OpenTelemetry gen_ai inference token logs"
});

declare({
  schema: "ds_vertex_agents_raw",
  name: "gen_ai_client_inference_operation_details",
  description: "Standalone Vertex AI Agent Engine OpenTelemetry gen_ai inference token logs"
});

declare({
  schema: "ds_ge_audit_raw",
  name: "cloudaudit_googleapis_com_data_access",
  description: "Discovery Engine Cloud Audit data_access logs"
});

declare({
  schema: "ds_ge_audit_raw",
  name: "cloudaudit_googleapis_com_activity",
  description: "Discovery Engine Cloud Audit admin activity logs"
});

declare({
  schema: "ds_security_guardrails_raw",
  name: "cloudaudit_googleapis_com_data_access",
  description: "Model Armor & Sensitive Data Protection guardrail logs"
});
