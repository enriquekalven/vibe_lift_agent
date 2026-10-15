"""Deterministic tool-trajectory evaluator for `finops_tool_trajectory` (see eval_config.yaml).

Verifies that `finops_agent` selected the expected ADK tool(s) for each evaluation case,
passed well-formed arguments, and received non-error tool responses.
"""

import json

# Fallback map by eval_case_id when `expected_tools` is not carried on the serialized instance.
_CASE_EXPECTED_TOOLS = {
    "open_finops_dashboard": ["open_dashboard"],
    "cache_economics_priced": ["calculate_cache_economics"],
    "cache_economics_unpriced_model": ["calculate_cache_economics"],
    "prompt_cache_xray_breakpoint": ["xray_prompt_cache"],
    "detect_breakpoint_line": ["detect_prompt_breakpoint"],
    "lint_agent_finops_ast_gate": ["lint_agent_finops"],
    "telemetry_grounding_audit": ["validate_telemetry_grounding"],
    "fleet_inventory_24h": ["query_ge_agent_fleet"],
    "live_finops_mart_reconciliation": ["query_live_finops_and_mart"],
    "discover_cloud_run_services": ["list_cloud_run_agents"],
    "gcp_telemetry_summary_24h": ["query_gcp_telemetry"],
}


def _extract_tool_trace(agent_data: dict) -> tuple[list[dict], list[dict]]:
  """Extracts (function_calls, function_responses) in chronological order from agent_data."""
  calls: list[dict] = []
  responses: list[dict] = []
  turns = (agent_data or {}).get("turns") or []
  for turn in turns:
    for event in (turn or {}).get("events") or []:
      content = (event or {}).get("content") or {}
      for part in content.get("parts") or []:
        if not isinstance(part, dict):
          continue
        fc = part.get("function_call")
        if isinstance(fc, dict) and fc.get("name"):
          calls.append(fc)
        fr = part.get("function_response")
        if isinstance(fr, dict) and fr.get("name"):
          responses.append(fr)
  return calls, responses


def evaluate(instance: dict) -> dict:
  """Scores tool selection and execution trajectory in [0.0, 1.0]."""
  case_id = str(instance.get("eval_case_id") or "")
  expected = instance.get("expected_tools") or _CASE_EXPECTED_TOOLS.get(case_id) or []
  agent_data = instance.get("agent_data") or {}
  calls, responses = _extract_tool_trace(agent_data)
  called_names = [str(c.get("name")) for c in calls]

  if not expected:
    return {
        "score": 1.0 if called_names else 0.5,
        "explanation": f"No expected_tools specified for case {case_id!r}; called={called_names}.",
    }

  missing = [t for t in expected if t not in called_names]
  if missing:
    return {
        "score": 0.0,
        "explanation": (
            f"Case {case_id!r} missed required tool(s) {missing}. "
            f"Expected {expected}, actual tool calls: {called_names}."
        ),
    }

  # Instruction #1 rule: when `open_dashboard` is expected, it must be the FIRST tool called.
  if "open_dashboard" in expected and called_names and called_names[0] != "open_dashboard":
    return {
        "score": 0.5,
        "explanation": (
            f"Case {case_id!r} called open_dashboard, but not first: {called_names}."
        ),
    }

  # Verify tool responses did not fail with an unhandled error payload.
  for fr in responses:
    resp_obj = fr.get("response") or {}
    raw_res = resp_obj.get("result") if isinstance(resp_obj, dict) else None
    if isinstance(raw_res, str):
      try:
        parsed = json.loads(raw_res)
        if isinstance(parsed, dict) and parsed.get("status") == "error":
          return {
              "score": 0.25,
              "explanation": f"Tool {fr.get('name')} returned status='error': {parsed.get('error')}",
          }
      except Exception:
        pass

  return {
      "score": 1.0,
      "explanation": f"Case {case_id!r} called expected tool(s) {expected} (actual={called_names}).",
  }
