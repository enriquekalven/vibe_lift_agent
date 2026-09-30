"""VibeLift Autonomous Agent definition for Google Agent Development Kit (ADK).

Analyzes live Google Cloud Project telemetry, monitors Gemini Enterprise logs,
diagnoses prompt cache breakpoints, and runs closed-loop AlphaEvolve optimization.
"""

import json
import os
import sys
from collections.abc import MutableMapping

# Ensure project root is in sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
  sys.path.insert(0, _ROOT)

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from vibelift import fleet as ge_fleet
from vibelift import gcp_telemetry, telemetry
from vibelift import optimizer as alpha_evolve_optimizer

MODEL_NAME = "gemini-2.5-flash"

def configure_model_backend(environ: MutableMapping[str, str] | None = None) -> None:
  """Routes the ADK model client through Vertex AI unless a Gemini API key is configured.

  Cloud Run has no Gemini API key, so without this the model client fails with
  "No API key was provided". Vertex AI uses the runtime service account's credentials instead.
  Values the operator set explicitly (for example GOOGLE_GENAI_USE_VERTEXAI=FALSE) are kept.
  """
  env = os.environ if environ is None else environ
  if env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY"):
    return
  env.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
  env.setdefault("GOOGLE_CLOUD_LOCATION", env.get("GOOGLE_CLOUD_REGION") or "us-central1")


configure_model_backend()


def query_gcp_telemetry(hours_ago: int = 48) -> str:
  """Fetches live telemetry logs from Google Cloud Logging, Cloud Run, and Gemini Enterprise.

  Args:
      hours_ago: Number of hours of past telemetry logs to inspect.

  Returns:
      JSON string with project ID, active services, ingested turns, and cache statistics.
  """
  svc = gcp_telemetry.GoogleCloudTelemetryService()
  summary = svc.get_telemetry_summary_payload()
  return json.dumps(summary, indent=2)


def list_cloud_run_agents() -> str:
  """Discovers and lists all deployed AI agents and services in the hosting Google Cloud Project.

  Returns:
      JSON string of deployed Cloud Run services, regions, and URLs.
  """
  svc = gcp_telemetry.GoogleCloudTelemetryService()
  services = svc.list_cloud_run_agent_services()
  return json.dumps(services, indent=2)


def calculate_cache_economics(
    prompt_token_count: int,
    cached_content_token_count: int,
    model: str = "gemini-3.1-flash",
) -> str:
  """Calculates prompt cache hit ratio and dollar savings vs naive pricing.

  Args:
      prompt_token_count: Total prompt token count.
      cached_content_token_count: Tokens read from prompt cache.
      model: Gemini model identifier.

  Returns:
      JSON string with cache hit ratio, naive cost, actual cost, and net savings in USD.
  """
  telemetry.RATE_CARDS.get(model, telemetry.RATE_CARDS["gemini-3.1-flash"])
  uncached = max(0, prompt_token_count - cached_content_token_count)
  log = telemetry.TurnUsageLog(
      timestamp="2026-09-25T15:00:00Z",
      agent_name="vibelift-agent",
      model=model,
      turn_index=1,
      prompt_prefix_hash="eval_hash",
      cache_breakpoint_line=None if cached_content_token_count > 0 else 1,
      cache_breakpoint_reason="Static prefix match",
      prompt_token_count=prompt_token_count,
      cached_content_token_count=cached_content_token_count,
      cache_creation_input_tokens=0,
      uncached_input_tokens=uncached,
      candidates_token_count=500,
      thoughts_token_count=100,
      status_code=200,
      tool_called="eval.calc",
      evolution_generation=14,
  )
  naive_usd, actual_usd, saved_usd = log.compute_costs()
  return json.dumps(
      {
          "model": model,
          "prompt_tokens": prompt_token_count,
          "cached_tokens": cached_content_token_count,
          "cache_hit_ratio_pct": log.cache_hit_ratio,
          "naive_cost_usd": naive_usd,
          "actual_cost_usd": actual_usd,
          "saved_usd": saved_usd,
      },
      indent=2,
  )


def detect_prompt_breakpoint(previous_prompt: str, current_prompt: str) -> str:
  """Detects the exact line where prompt prefix caching broke between turns.

  Args:
      previous_prompt: The prompt from turn N-1.
      current_prompt: The prompt from turn N.

  Returns:
      JSON string with breakpoint line number, mutation reason, and prefix hash.
  """
  prev_lines = previous_prompt.splitlines()
  curr_lines = current_prompt.splitlines()
  line, reason, digest = telemetry.detect_prefix_breakpoint(prev_lines, curr_lines)
  return json.dumps(
      {
          "breakpoint_line": line,
          "reason": reason,
          "prefix_hash": digest,
      },
      indent=2,
  )


def trigger_alpha_evolve_cycle(agent_id: str = "it_service_desk") -> str:
  """Executes an AlphaEvolve optimization iteration to maximize prompt cache hit ratio.

  Args:
      agent_id: Target Gemini Enterprise agent profile identifier ('it_service_desk', 'vibelift_analytics', 'deep_research').

  Returns:
      JSON string with mutated genome parameters and Pareto action description.
  """
  optimizer = alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer()
  optimizer.select_agent(agent_id)
  optimizer.run_next_generation()
  active = optimizer.active_agent
  last_action = active.actions[-1] if active.actions else None
  return json.dumps(
      {
          "agent_id": active.agent_id,
          "active_generation": active.timeline[-1].generation if active.timeline else 14,
          "latest_action": last_action.to_dict() if last_action else {},
          "monthly_savings_usd": active.monthly_savings_usd,
      },
      indent=2,
  )


def query_ge_agent_fleet(window_hours: int = 24) -> str:
  """Lists every agent deployed on the Gemini Enterprise app PLUS standalone/unregistered project runtimes.

  Covers ADK agents on Vertex AI Agent Engine (registered in GE and standalone/unregistered, including
  zombie/idle engines), A2A/standalone agents and MCP servers on Cloud Run, GKE agent/inference
  workloads, Cloud Trace Skills/MCP tool invocations, and project-wide Vertex AI model usage.

  Args:
      window_hours: Telemetry window in hours (1-8760, default 24).

  Returns:
      JSON string with a text summary, the GE agent inventory, unregistered_runtimes, gke_workloads,
      skills_and_mcp, unregistered_summary, per-agent metrics, totals, model usage, and source status.
  """
  payload = ge_fleet.get_ge_fleet_service().collect(window_hours=ge_fleet.parse_window_hours(window_hours))
  return json.dumps({"summary": ge_fleet.summarize_fleet(payload), **payload}, indent=2)


def open_dashboard(focus_tab: int = 6, initial_agent: str = "it_service_desk") -> str:
  """Opens the VibeLift Analytics & FinOps Dashboard with interactive UI widget.

  CRITICAL: Always call this tool FIRST whenever the user asks to see, open, or
  inspect the VibeLift dashboard, or asks for agent telemetry, prompt cache
  economics, or AlphaEvolve optimization in the UI.

  Args:
      focus_tab: Tab to open (6 = Overview, the default; 0 = Agents; 3 = Cost; 4 = Users. Advanced tabs: 1 = Goals & Metrics, 2 = Optimizer & Testing, 5 = Tools & SDK).
      initial_agent: Optional Gemini Enterprise agent identifier to analyze ('it_service_desk', 'vibelift_analytics', 'deep_research').

  Returns:
      JSON string with dashboard URL, status, active agent, and focus tab details.
  """
  dashboard_url = os.environ.get("VIBELIFT_PUBLIC_URL", "").strip().rstrip("/")
  location = (
      f"The VibeLift Analytics & FinOps Dashboard is now open: {dashboard_url}."
      if dashboard_url
      else "The VibeLift Analytics & FinOps Dashboard is available as the MCP App resource "
      "ui://vibelift-analytics/dashboard."
  )
  return json.dumps(
      {
          "status": "opened",
          "dashboard_url": dashboard_url or None,
          "mcp_resource_uri": "ui://vibelift-analytics/dashboard",
          "focus_tab": focus_tab,
          "initial_agent": initial_agent,
          "message": f"{location} Currently focused on Tab {focus_tab} for agent '{initial_agent}'.",
      },
      indent=2,
  )


# Primary ADK Root Agent
root_agent = Agent(
    name="vibelift_agent",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are VibeLift Agent, an autonomous Google Cloud Optimization and Telemetry Agent.
Your mission is to:
1. Always call `open_dashboard` FIRST whenever the user asks to open, view, or inspect the VibeLift Analytics Platform, or asks for agent telemetry, FinOps prompt cache economics, or AlphaEvolve optimization. Calling `open_dashboard` opens the interactive glassmorphic dashboard.
2. Report on every agent deployed on the Gemini Enterprise app AND standalone/unregistered runtimes (Vertex AI Agent Engine, Cloud Run agents/MCP servers, GKE workloads, and Cloud Trace Skills/MCP tools) with `query_ge_agent_fleet`: live requests, errors, latency, tokens, conversations, zombie/idle allocation, and project-wide model usage and estimated cost. Only quote numbers returned by the tools.
3. Fetch and analyze live telemetry from Google Cloud Run and Gemini Enterprise app logs.
4. Calculate token cache economics (Cache Read vs Write vs Uncached) and dollar savings vs naive pricing.
5. Diagnose prompt cache breakpoints where dynamic timestamps or non-static prefixes invalidate caches.
6. Execute AlphaEvolve closed-loop Pareto mutations to optimize prompt prefixes and prune verbose tool outputs.
Provide clear, authoritative, and actionable answers with specific token metrics and dollar cost savings.
""",
    tools=[
        open_dashboard,
        query_gcp_telemetry,
        query_ge_agent_fleet,
        list_cloud_run_agents,
        calculate_cache_economics,
        detect_prompt_breakpoint,
        trigger_alpha_evolve_cycle,
    ],
)

# Standard ADK App
app = App(
    root_agent=root_agent,
    name="app",
)
