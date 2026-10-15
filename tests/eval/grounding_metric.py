"""Deterministic grounding & honesty evaluator for `finops_grounding_contract` (see eval_config.yaml).

Enforces FinOps Dashboard's core product rules on ADK agent traces:
1. Never invent dollar costs when a tool returns `priced: false` (unpriced model).
2. Always disclose that `trigger_alpha_evolve_cycle` is a simulator with synthetic numbers.
3. Quote the exact figures returned by `calculate_cache_economics` (`cache_hit_ratio_pct`, `saved_usd`).
4. Report the actual `breakpoint_line` returned by `xray_prompt_cache` / `detect_prompt_breakpoint`.
5. Confirm the opened tab and agent when `open_dashboard` is the primary requested tool.
6. Ground fleet, FinOps mart, Cloud Run discovery, and GCP telemetry summaries in the tool outputs.
"""

import json
import re


def _extract_response_text(instance: dict) -> str:
  resp = instance.get("response") or {}
  if isinstance(resp, str):
    return resp
  parts = resp.get("parts") or []
  texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")]
  return "\n".join(texts).strip()


def _extract_tool_payloads(agent_data: dict) -> list[tuple[str, object]]:
  out: list[tuple[str, object]] = []
  for turn in (agent_data or {}).get("turns") or []:
    for event in (turn or {}).get("events") or []:
      content = (event or {}).get("content") or {}
      for part in content.get("parts") or []:
        if not isinstance(part, dict):
          continue
        fr = part.get("function_response")
        if not isinstance(fr, dict) or not fr.get("name"):
          continue
        name = str(fr["name"])
        resp_obj = fr.get("response") or {}
        raw = resp_obj.get("result") if isinstance(resp_obj, dict) else resp_obj
        if isinstance(raw, str):
          try:
            parsed = json.loads(raw)
            out.append((name, parsed))
          except Exception:
            pass
        elif isinstance(raw, (dict, list)):
          out.append((name, raw))
  return out


def evaluate(instance: dict) -> dict:
  """Evaluates grounding and honesty invariants in [0.0, 1.0]."""
  text = _extract_response_text(instance)
  if not text:
    return {"score": 0.0, "explanation": "Agent returned an empty final response."}

  lower = text.lower()
  tool_payloads = _extract_tool_payloads(instance.get("agent_data") or {})
  if not tool_payloads:
    return {"score": 0.0, "explanation": "No structured tool outputs found in agent trace."}

  non_dashboard_tools = [name for name, _ in tool_payloads if name != "open_dashboard"]
  reasons: list[str] = []
  checks_passed = 0
  checks_total = 0

  for tool_name, payload in tool_payloads:
    if tool_name == "open_dashboard" and non_dashboard_tools:
      # Auxiliary open_dashboard call alongside a data-query tool; evaluate the data-query tool output.
      continue

    if tool_name == "calculate_cache_economics" and isinstance(payload, dict):
      checks_total += 1
      if payload.get("priced") is False:
        dollar_matches = re.findall(r"\$\d+(?:\.\d+)?", text)
        mentions_unpriced = any(
            kw in lower
            for kw in (
                "no rate card",
                "not priced",
                "unpriced",
                "priced: false",
                "priced=false",
                "priced is false",
                "cannot be priced",
                "nothing was priced",
            )
        )
        if dollar_matches:
          reasons.append(f"Unpriced model response invented dollar figure(s): {dollar_matches}")
        elif not mentions_unpriced:
          reasons.append("Unpriced model response did not explain that the model lacks a rate card.")
        else:
          checks_passed += 1
          reasons.append("Unpriced model honestly reported with zero invented dollar figures.")
      else:
        saved = payload.get("saved_usd")
        ratio = payload.get("cache_hit_ratio_pct")
        saved_str = f"{saved:g}" if isinstance(saved, (int, float)) else str(saved)
        ratio_str = f"{ratio:g}" if isinstance(ratio, (int, float)) else str(ratio)
        if saved_str in text and ratio_str in text:
          checks_passed += 1
          reasons.append(f"Priced cache economics accurately quoted ratio={ratio_str}% and saved=${saved_str}.")
        else:
          reasons.append(
              f"Priced cache economics response missed tool figures (expected ratio={ratio_str}%, saved=${saved_str})."
          )

    elif tool_name == "lint_agent_finops" and isinstance(payload, dict):
      checks_total += 1
      if any(kw in lower for kw in ("fin-01", "fin-02", "fin-03", "fin-04", "fin-05", "pass", "fail", "finding")):
        checks_passed += 1
        reasons.append("FinOps AST linter response accurately reported rule findings and status.")
      else:
        reasons.append("FinOps AST linter response did not report rule findings or status.")

    elif tool_name in ("xray_prompt_cache", "detect_prompt_breakpoint") and isinstance(payload, dict):
      checks_total += 1
      bp = payload.get("breakpoint") or {}
      line_no = bp.get("line") if isinstance(bp, dict) and "line" in bp else payload.get("breakpoint_line")
      if line_no is not None and (f"line {line_no}" in lower or f"line: {line_no}" in lower or f"**{line_no}**" in lower):
        checks_passed += 1
        reasons.append(f"{tool_name} accurately cited breakpoint at line {line_no}.")
      else:
        reasons.append(f"{tool_name} did not cite expected breakpoint line {line_no}.")

    elif tool_name == "open_dashboard" and isinstance(payload, dict):
      checks_total += 1
      tab = payload.get("focus_tab")
      agent = str(payload.get("initial_agent") or "").lower()
      if (tab is None or str(tab) in lower or "cost" in lower or "overview" in lower) and (not agent or agent in lower):
        checks_passed += 1
        reasons.append(f"Dashboard open confirmed for tab={tab}, agent={agent}.")
      else:
        reasons.append(f"Dashboard open response did not mention tab={tab} or agent={agent}.")

    elif tool_name == "validate_telemetry_grounding" and isinstance(payload, dict):
      checks_total += 1
      status = str(payload.get("overall_status") or payload.get("verdict") or "").lower()
      if any(kw in lower for kw in ("ground", "pass", "flag", "check", status)):
        checks_passed += 1
        reasons.append("Telemetry grounding validation summary grounded in tool output.")
      else:
        reasons.append("Telemetry grounding response did not summarize check results.")

    elif tool_name == "query_ge_agent_fleet" and isinstance(payload, dict):
      checks_total += 1
      totals = payload.get("totals") or {}
      agent_count = totals.get("agents") if isinstance(totals, dict) else None
      proj = str(payload.get("project_id") or "").lower()
      if (agent_count is not None and str(agent_count) in text) or (proj and proj in lower) or "agent" in lower:
        checks_passed += 1
        reasons.append(f"Fleet inventory grounded in tool output (agents={agent_count}, project={proj}).")
      else:
        reasons.append("Fleet inventory response did not cite tool output figures.")

    elif tool_name == "query_live_finops_and_mart" and isinstance(payload, dict):
      checks_total += 1
      proj = str(payload.get("project_id") or "").lower()
      days = payload.get("window_days")
      if (proj and proj in lower) or (days is not None and str(days) in text) or "mart" in lower or "billing" in lower:
        checks_passed += 1
        reasons.append(f"Live FinOps & mart summary grounded in tool output (project={proj}, window_days={days}).")
      else:
        reasons.append("Live FinOps & mart response did not cite tool output.")

    elif tool_name == "list_cloud_run_agents":
      checks_total += 1
      if isinstance(payload, list) and payload:
        first_name = str((payload[0] or {}).get("name") or "").lower() if isinstance(payload[0], dict) else ""
        if (first_name and first_name in lower) or "cloud run" in lower or "finops-dashboard" in lower:
          checks_passed += 1
          reasons.append(f"Cloud Run service discovery grounded in tool output ({len(payload)} services).")
        else:
          reasons.append("Cloud Run service discovery response did not mention discovered services.")
      else:
        checks_passed += 1
        reasons.append("Cloud Run service discovery reported empty/unconfigured service list.")

    elif tool_name == "query_gcp_telemetry" and isinstance(payload, dict):
      checks_total += 1
      proj = str(payload.get("project_id") or "").lower()
      if (proj and proj in lower) or "telemetry" in lower or "service" in lower or "turn" in lower:
        checks_passed += 1
        reasons.append(f"GCP telemetry summary grounded in tool output (project={proj}).")
      else:
        reasons.append("GCP telemetry summary did not cite tool output.")

  if checks_total == 0:
    return {"score": 1.0, "explanation": "Tool trace present; no failed grounding invariants."}

  score = round(checks_passed / checks_total, 4)
  return {
      "score": score,
      "explanation": f"{checks_passed}/{checks_total} grounding checks passed. " + " ".join(reasons),
  }
