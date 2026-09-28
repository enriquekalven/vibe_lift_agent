"""Multi-agent profiles, user parameters, time-series & AlphaEvolve actions."""

import dataclasses
import re


@dataclasses.dataclass
class OptimizationParameter:
  """Represents a tracked or user-defined optimization parameter."""

  key: str
  label: str
  unit: str
  direction: str
  baseline_value: float
  current_value: float
  target_value: float
  weight_pct: int
  status: str

  def to_dict(self) -> dict[str, object]:
    """Serializes parameter state for the UI."""
    if self.baseline_value != 0:
      raw_delta = (
          (self.current_value - self.baseline_value)
          / abs(self.baseline_value)
          * 100.0
      )
    else:
      raw_delta = 0.0
    return {
        'key': self.key,
        'label': self.label,
        'unit': self.unit,
        'direction': self.direction,
        'baseline_value': self.baseline_value,
        'current_value': self.current_value,
        'target_value': self.target_value,
        'weight_pct': self.weight_pct,
        'status': self.status,
        'delta_pct': round(raw_delta, 1),
    }


@dataclasses.dataclass(frozen=True)
class TimeSeriesPoint:
  """Single time/generation snapshot across all optimization parameters."""

  timestamp_label: str
  generation: int
  latency_ms: float
  cost_usd: float
  accuracy_pct: float
  cache_hit_pct: float
  error_rate_pct: float
  event_marker: str

  def to_dict(self) -> dict[str, object]:
    """Serializes time-series snapshot to dict."""
    return {
        'timestamp_label': self.timestamp_label,
        'generation': self.generation,
        'latency_ms': self.latency_ms,
        'cost_usd': self.cost_usd,
        'accuracy_pct': self.accuracy_pct,
        'cache_hit_pct': self.cache_hit_pct,
        'error_rate_pct': self.error_rate_pct,
        'event_marker': self.event_marker,
    }


@dataclasses.dataclass(frozen=True)
class AlphaEvolveActionRecord:
  """Describes a concrete optimization action taken by AlphaEvolve."""

  generation: int
  timestamp: str
  action_title: str
  parameter_targeted: str
  root_cause_from_logs: str
  action_taken: str
  impact_summary: str
  diff_snippet: str
  status: str

  def to_dict(self) -> dict[str, object]:
    """Serializes action record to dict."""
    return {
        'generation': self.generation,
        'timestamp': self.timestamp,
        'action_title': self.action_title,
        'parameter_targeted': self.parameter_targeted,
        'root_cause_from_logs': self.root_cause_from_logs,
        'action_taken': self.action_taken,
        'impact_summary': self.impact_summary,
        'diff_snippet': self.diff_snippet,
        'status': self.status,
    }


@dataclasses.dataclass
class DemoAgentProfile:
  """Holds state, parameters, time-series, and AlphaEvolve actions per agent."""

  agent_id: str
  display_name: str
  domain: str
  model: str
  health_status: str
  monthly_savings_usd: int
  parameters: list[OptimizationParameter]
  timeline: list[TimeSeriesPoint]
  actions: list[AlphaEvolveActionRecord]

  def to_dict(self) -> dict[str, object]:
    """Serializes full agent optimization profile."""
    return {
        'agent_id': self.agent_id,
        'display_name': self.display_name,
        'domain': self.domain,
        'model': self.model,
        'health_status': self.health_status,
        'monthly_savings_usd': self.monthly_savings_usd,
        'parameters': [p.to_dict() for p in self.parameters],
        'timeline': [t.to_dict() for t in self.timeline],
        'actions': [a.to_dict() for a in self.actions],
    }


def _slugify_agent_name(display_name: str, fallback_id: str = '') -> str:
  """Maps a Gemini Enterprise agent display name or resource ID to a canonical profile slug."""
  raw_name = (display_name or '').strip().lower()
  raw_id = (fallback_id or '').strip().lower()
  if 'it service desk' in raw_name or raw_id in ('it_service_desk', '13791105764600209034', 'service-desk-agent'):
    return 'it_service_desk'
  if 'vibelift' in raw_name or raw_id in ('vibelift_analytics', '9854735213116306727', 'vibe-lift-agent'):
    return 'vibelift_analytics'
  if 'deep research' in raw_name or raw_id == 'deep_research':
    return 'deep_research'
  slug = ''.join(ch if ch.isalnum() else '_' for ch in (raw_name or raw_id)).strip('_')
  while '__' in slug:
    slug = slug.replace('__', '_')
  return slug or 'ge_agent'


def _build_default_agents() -> dict[str, DemoAgentProfile]:
  """Creates the selectable optimization profiles for the agents registered on Gemini Enterprise."""
  it_service_desk = DemoAgentProfile(
      agent_id='it_service_desk',
      display_name='IT Service Desk',
      domain='ADK Tier-1 & Tier-2 IT Support Triage (Vertex AI Agent Engine)',
      model='gemini-2.5-flash',
      health_status='OPTIMIZED (Gen 14 Active)',
      monthly_savings_usd=14820,
      parameters=[
          OptimizationParameter(
              key='latency_ms',
              label='P95 Response Latency',
              unit='ms',
              direction='LOWER',
              baseline_value=2480.0,
              current_value=690.0,
              target_value=800.0,
              weight_pct=30,
              status='MEETING TARGET (-72%)',
          ),
          OptimizationParameter(
              key='cost_usd',
              label='Net Cost per 1k Turns (Cache-Adjusted)',
              unit='$',
              direction='LOWER',
              baseline_value=29.40,
              current_value=3.45,
              target_value=5.00,
              weight_pct=30,
              status='MEETING TARGET (-88%)',
          ),
          OptimizationParameter(
              key='accuracy_pct',
              label='IT Policy & Runbook Grounding Accuracy',
              unit='%',
              direction='HIGHER',
              baseline_value=91.0,
              current_value=96.4,
              target_value=95.0,
              weight_pct=25,
              status='EXCEEDING TARGET (+5.4%)',
          ),
          OptimizationParameter(
              key='cache_hit_pct',
              label='Prompt Cache Hit Ratio (Log-Derived)',
              unit='%',
              direction='HIGHER',
              baseline_value=12.0,
              current_value=91.2,
              target_value=85.0,
              weight_pct=10,
              status='EXCEEDING TARGET (+79.2%)',
          ),
          OptimizationParameter(
              key='error_rate_pct',
              label='400/429 Tool & Escalation Error Rate',
              unit='%',
              direction='LOWER',
              baseline_value=6.8,
              current_value=0.0,
              target_value=0.5,
              weight_pct=5,
              status='SELF-HEALED (0.0%)',
          ),
          OptimizationParameter(
              key='context_bloat_pct',
              label='Context Bloating Ratio (Stale History / Total Context)',
              unit='%',
              direction='LOWER',
              baseline_value=64.0,
              current_value=14.2,
              target_value=20.0,
              weight_pct=15,
              status='PRUNED BY N-2 WINDOW (-78%)',
          ),
          OptimizationParameter(
              key='idle_ratio_pct',
              label='Agent Idle & Tool-Wait Ratio (@vibelift_telemetry)',
              unit='%',
              direction='LOWER',
              baseline_value=41.5,
              current_value=8.4,
              target_value=15.0,
              weight_pct=10,
              status='MEETING TARGET (-80%)',
          ),
      ],
      timeline=[
          TimeSeriesPoint(
              'Day 1 (Baseline)',
              0,
              2480,
              29.40,
              91.0,
              12.0,
              6.8,
              'Baseline Log Audit',
          ),
          TimeSeriesPoint(
              'Day 2 (Gen 4)',
              4,
              1420,
              6.10,
              92.5,
              82.4,
              4.2,
              'Prefix Cache Locked',
          ),
          TimeSeriesPoint(
              'Day 3 (Gen 8)',
              8,
              980,
              4.80,
              94.1,
              86.5,
              1.1,
              'Tool Output Pruner',
          ),
          TimeSeriesPoint(
              'Day 4 (Gen 9 Rej)',
              9,
              610,
              4.20,
              74.0,
              85.0,
              11.5,
              'Blocked Quality Drop',
          ),
          TimeSeriesPoint(
              'Day 5 (Gen 14)',
              14,
              690,
              3.45,
              96.4,
              91.2,
              0.0,
              'Tier-2 Escalation Schema Fix',
          ),
      ],
      actions=[
          AlphaEvolveActionRecord(
              generation=4,
              timestamp='Day 2 • 14:20 UTC',
              action_title='Prompt Prefix Reordering (Static IT Runbook Cache Lock)',
              parameter_targeted='Cost ($) & Cache Hit Ratio (%)',
              root_cause_from_logs=(
                  'Log diff revealed dynamic ticket timestamp & incident banner '
                  'at line 1 invalidated 16,400 static IT policy & runbook tokens.'
              ),
              action_taken=(
                  'AlphaEvolve moved static IT policy rules & runbook tool schemas '
                  'to top of SystemPrompt and pushed ticket clock/metadata to suffix.'
              ),
              impact_summary='Cache Hit: 12% -> 82.4% | Cost: $29.40 -> $6.10',
              diff_snippet=(
                  '- [Line 1] Ticket Time: {{now_iso}}, Active Incidents: {{live_banner}}\n'
                  '+ [Line 1] STATIC_IT_POLICY_AND_RUNBOOK_SCHEMAS (16,400 tokens cached)\n'
                  '+ [Suffix] Ticket Context Delta: Time={{now_iso}}'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=8,
              timestamp='Day 3 • 09:15 UTC',
              action_title='Sliding-Window Tool History Compression (N-2)',
              parameter_targeted='Latency (ms), Context Bloating (%) & Cost ($)',
              root_cause_from_logs=(
                  'Multi-turn logs showed 9,400-token VPN/SSO diagnostic dumps '
                  're-sent verbatim across turns 3..12, causing 64% context bloating.'
              ),
              action_taken=(
                  'Synthesized N-2 turn summarizer that compacts historical '
                  'runbook search outputs into 180-token structured digests.'
              ),
              impact_summary='Latency: 1,420ms -> 980ms | Context Bloat: 64% -> 14.2% | Cost: $6.10 -> $4.80',
              diff_snippet=(
                  '+ PRUNE_TOOL_OUTPUT_OLDER_THAN_TURNS = 2\n'
                  '+ TOOL_DIGEST_FORMAT = "compact_key_metrics_only"'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=9,
              timestamp='Day 4 • 11:05 UTC',
              action_title='Aggressive Few-Shot Removal (Safety Gate Blocked)',
              parameter_targeted='Accuracy Guardrail (%)',
              root_cause_from_logs=(
                  'Novel Strategist attempted deleting all 6 IT policy review '
                  'few-shot examples to shave 1,800 tokens.'
              ),
              action_taken=(
                  'Reviewer Gatekeeper automatically REJECTED candidate after '
                  'shadow log replay showed Policy Accuracy dropping 94.1% -> 74.0%.'
              ),
              impact_summary='PREVENTED -20.1% Accuracy Regression',
              diff_snippet=(
                  '! REVIEWER_GATE = REJECTED (Accuracy 74.0% < 95.0% floor)\n'
                  '! Auto-reverted to Elite Gen 8'
              ),
              status='REJECTED BY GUARDRAIL',
          ),
          AlphaEvolveActionRecord(
              generation=14,
              timestamp='Day 5 • 16:40 UTC',
              action_title='Strict JSON Schema Guard + Tier-2 Escalation Routing',
              parameter_targeted='Accuracy (%), Latency (ms) & Error Rate (%)',
              root_cause_from_logs=(
                  'Logs captured 6.8% HTTP 400 malformed tool calls when escalating '
                  'to service-desk-escalation (Reasoning Engine 8821150342449201152).'
              ),
              action_taken=(
                  'Enforced strict enum/type constraints on escalation tool parameters '
                  'and shared static prefix cache across Tier-1 and Tier-2 engines.'
              ),
              impact_summary=(
                  'Latency: 980ms -> 690ms | Accuracy: 96.4% | Errors: 0.0%'
              ),
              diff_snippet=(
                  '+ STRICT_JSON_SCHEMA_VALIDATION = True\n'
                  '+ TIER2_ESCALATION_SHARED_PREFIX = "it_service_desk_v4"'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  vibelift_analytics = DemoAgentProfile(
      agent_id='vibelift_analytics',
      display_name='VibeLift Analytics & FinOps',
      domain='Autonomous Fleet Telemetry, Prompt Cache FinOps & AlphaEvolve (A2A / MCP)',
      model='gemini-2.5-flash',
      health_status='OPTIMIZED (Gen 11 Active)',
      monthly_savings_usd=22400,
      parameters=[
          OptimizationParameter(
              'latency_ms',
              'P95 MCP & Telemetry Turn Latency',
              'ms',
              'LOWER',
              3850.0,
              1120.0,
              1250.0,
              30,
              'MEETING TARGET (-71%)',
          ),
          OptimizationParameter(
              'cost_usd',
              'Net Cost per 1k Turns (Cache-Adjusted)',
              '$',
              'LOWER',
              42.00,
              5.80,
              8.00,
              30,
              'MEETING TARGET (-86%)',
          ),
          OptimizationParameter(
              'accuracy_pct',
              'FinOps Rate-Card & Breakpoint Attribution Accuracy',
              '%',
              'HIGHER',
              89.5,
              97.2,
              95.0,
              25,
              'EXCEEDING TARGET (+7.7%)',
          ),
          OptimizationParameter(
              'cache_hit_pct',
              'Prompt Cache Hit Ratio',
              '%',
              'HIGHER',
              18.0,
              89.4,
              85.0,
              15,
              'EXCEEDING TARGET (+71.4%)',
          ),
          OptimizationParameter(
              'context_bloat_pct',
              'Context Bloating Ratio (Stale Telemetry Dumps)',
              '%',
              'LOWER',
              58.0,
              11.8,
              20.0,
              15,
              'PRUNED (-80%)',
          ),
          OptimizationParameter(
              'idle_ratio_pct',
              'Agent Idle & Fan-Out Wait Ratio (@vibelift_telemetry)',
              '%',
              'LOWER',
              48.0,
              5.2,
              12.0,
              10,
              'PARALLELIZED (-89%)',
          ),
      ],
      timeline=[
          TimeSeriesPoint(
              'Day 1 (Baseline)',
              0,
              3850,
              42.00,
              89.5,
              18.0,
              5.2,
              'Uncached Fleet Scan Baseline',
          ),
          TimeSeriesPoint(
              'Day 3 (Gen 6)',
              6,
              1890,
              9.40,
              94.8,
              81.0,
              1.4,
              'MCP Schema Prefix Cache Lock',
          ),
          TimeSeriesPoint(
              'Day 5 (Gen 11)',
              11,
              1120,
              5.80,
              97.2,
              89.4,
              0.0,
              'Parallel Telemetry + Warm Snapshot',
          ),
      ],
      actions=[
          AlphaEvolveActionRecord(
              generation=6,
              timestamp='Day 3 • 10:12 UTC',
              action_title='MCP Tool Catalog & Rate-Card Prefix Caching',
              parameter_targeted='Cost ($) & Cache Hit Ratio (%)',
              root_cause_from_logs=(
                  'MCP tool schemas and BigQuery FinOps rate-card definitions were '
                  're-tokenized on every analytical turn without shared prefix caching.'
              ),
              action_taken=(
                  'Pinned 22k-token MCP schema & Vertex AI rate-card definitions into '
                  'an invariant system prefix block.'
              ),
              impact_summary='Cache Hit: 18% -> 81.0% | Cost: $42.00 -> $9.40',
              diff_snippet=(
                  '+ EXPLICIT_CONTEXT_CACHE_TTL = "3600s"\n'
                  '+ PIN_MCP_SCHEMAS_IN_SYSTEM_PREFIX = True'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=11,
              timestamp='Day 5 • 15:30 UTC',
              action_title='Parallel Cloud Monitoring Fan-Out & Warm Snapshot Cache',
              parameter_targeted='Latency (ms), Idle Ratio (%) & Accuracy (%)',
              root_cause_from_logs=(
                  'Sequential Discovery Engine, Cloud Monitoring, and Cloud Logging '
                  'calls exceeded the 1.0s Gemini Enterprise MCP streamable HTTP window.'
              ),
              action_taken=(
                  'Evolved background fleet warmer + stale-while-revalidate cache so '
                  'open_dashboard responds in <10ms with verified telemetry.'
              ),
              impact_summary=(
                  'Latency: 1,890ms -> 1,120ms | Idle Ratio: 48% -> 5.2% | Accuracy: 97.2%'
              ),
              diff_snippet=(
                  '+ FLEET_STALE_WHILE_REVALIDATE = True\n'
                  '+ BACKGROUND_WARMER_INTERVAL_S = 120'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  deep_research = DemoAgentProfile(
      agent_id='deep_research',
      display_name='Deep Research',
      domain='Google-Managed Multi-Source Research & Report Synthesis',
      model='gemini-2.5-pro',
      health_status='OPTIMIZED (Gen 12 Active)',
      monthly_savings_usd=9650,
      parameters=[
          OptimizationParameter(
              'latency_ms',
              'Multi-Hop Research P95 Latency',
              'ms',
              'LOWER',
              3100.0,
              740.0,
              900.0,
              35,
              'MEETING TARGET (-76%)',
          ),
          OptimizationParameter(
              'cost_usd',
              'Net Cost per 1k Turns',
              '$',
              'LOWER',
              19.80,
              2.60,
              4.00,
              25,
              'MEETING TARGET (-87%)',
          ),
          OptimizationParameter(
              'accuracy_pct',
              'Citation & Cross-Source Grounding Accuracy',
              '%',
              'HIGHER',
              92.0,
              97.8,
              95.0,
              25,
              'EXCEEDING TARGET (+5.8%)',
          ),
          OptimizationParameter(
              'error_rate_pct',
              'Retrieval 400/429 Error Rate',
              '%',
              'LOWER',
              13.4,
              0.0,
              0.5,
              15,
              'SELF-HEALED (0.0%)',
          ),
          OptimizationParameter(
              'context_bloat_pct',
              'Context Bloating Ratio (Unpruned Search Chunks)',
              '%',
              'LOWER',
              71.0,
              16.5,
              22.0,
              15,
              'DEDUPLICATED (-77%)',
          ),
          OptimizationParameter(
              'idle_ratio_pct',
              'Subagent Retrieval Idle / Pacing Ratio',
              '%',
              'LOWER',
              39.0,
              9.1,
              15.0,
              10,
              'MEETING TARGET (-77%)',
          ),
      ],
      timeline=[
          TimeSeriesPoint(
              'Day 1 (Baseline)',
              0,
              3100,
              19.80,
              92.0,
              22.0,
              13.4,
              '429 Quota & Unpruned Search Chunks',
          ),
          TimeSeriesPoint(
              'Day 3 (Gen 7)',
              7,
              1290,
              4.50,
              95.4,
              84.0,
              1.2,
              'Search Snippet Deduplication',
          ),
          TimeSeriesPoint(
              'Day 5 (Gen 12)',
              12,
              740,
              2.60,
              97.8,
              93.1,
              0.0,
              'Adaptive Rate-Limit Backoff',
          ),
      ],
      actions=[
          AlphaEvolveActionRecord(
              generation=7,
              timestamp='Day 3 • 12:00 UTC',
              action_title='Multi-Hop Search Snippet Deduplication & Prefix Share',
              parameter_targeted='Cost ($), Context Bloating (%) & Latency (ms)',
              root_cause_from_logs=(
                  'Retrieval subagents appended raw document chunks across '
                  'research hops, causing 71% context bloating and 429 bursts.'
              ),
              action_taken=(
                  'Shared static research plan prefix across hops and capped '
                  'retrieved chunks to top-3 deduplicated citation digests.'
              ),
              impact_summary='Latency: 3,100ms -> 1,290ms | Context Bloat: 71% -> 16.5% | Cost: $19.80 -> $4.50',
              diff_snippet=(
                  '+ MAX_RETRIEVAL_CHUNKS_PER_HOP = 3\n'
                  '+ DEDUPLICATE_CITATION_DIGESTS = True'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=12,
              timestamp='Day 5 • 18:10 UTC',
              action_title='Closed-Loop 400/429 Self-Healing & Backoff Guard',
              parameter_targeted='Error Rate (%) & Accuracy (%)',
              root_cause_from_logs=(
                  'Runtime logs recorded 13.4% error rate from parallel '
                  'retrieval quota bursts during deep report synthesis.'
              ),
              action_taken=(
                  'Added query schema validator and adaptive token-bucket '
                  'pacing between parallel research subagents.'
              ),
              impact_summary='Error Rate: 13.4% -> 0.0% | Accuracy: 97.8%',
              diff_snippet=(
                  '+ STRICT_QUERY_SCHEMA_VALIDATOR = True\n'
                  '+ SUBAGENT_QPS_PACING_MS = 120'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  return {
      it_service_desk.agent_id: it_service_desk,
      vibelift_analytics.agent_id: vibelift_analytics,
      deep_research.agent_id: deep_research,
  }


OPTIMIZER_PLATFORMS: dict[str, dict[str, str]] = {
    'alpha_evolve': {
        'id': 'alpha_evolve',
        'name': 'AlphaEvolve (Multi-Objective Pareto Loop)',
        'badge': 'Genetic Prompt & Config Evolution',
        'description': (
            'Evolutionary Pareto optimization over prompt prefixes, tool schemas, '
            'and sliding-window context rules with shadow log-replay safety gates.'
        ),
    },
    'opus_critic': {
        'id': 'opus_critic',
        'name': 'Opus Frontier Critic (Structural Prompt Refactoring)',
        'badge': 'Frontier LLM Structural Critic',
        'description': (
            'Uses frontier reasoning critique to rewrite ambiguous instructions, '
            'eliminate redundant tool calls, and synthesize compact JSON schemas.'
        ),
    },
    'vertex_vizier': {
        'id': 'vertex_vizier',
        'name': 'Google Vizier (Distributed Black-Box Bayesian Tuner)',
        'badge': 'Bayesian Hyperparameter & Context Tuner',
        'description': (
            'Google distributed black-box optimization engine tuning continuous '
            'parameters (N-turn pruning window, QPS pacing, temperature, cache TTL).'
        ),
    },
    'hybrid_ensemble': {
        'id': 'hybrid_ensemble',
        'name': 'Hybrid Ensemble (AlphaEvolve + Vizier + Opus Critic)',
        'badge': 'Combined Multi-Platform Pipeline',
        'description': (
            'Combines Opus structural prompt refactoring, Google Vizier numerical '
            'hyperparameter tuning, and AlphaEvolve Pareto selection.'
        ),
    },
}


def _get_param_val(profile: DemoAgentProfile | None, key: str, default: float) -> float:
  if not profile:
    return default
  for p in profile.parameters:
    if p.key == key:
      return float(p.current_value)
  return default


def build_user_centric_analytics(
    agents: dict[str, DemoAgentProfile] | None = None,
) -> dict[str, object]:
  """Builds the User-Centric Token Spending, Savings, Skill/MCP & Governance view (Sep 22 Sync)."""
  it_agent = agents.get('it_service_desk') if agents else None
  vl_agent = agents.get('vibelift_analytics') if agents else None
  dr_agent = agents.get('deep_research') if agents else None

  it_cost = _get_param_val(it_agent, 'cost_usd', 3.45)
  vl_cost = _get_param_val(vl_agent, 'cost_usd', 5.80)
  dr_cost = _get_param_val(dr_agent, 'cost_usd', 2.60)

  it_bloat = _get_param_val(it_agent, 'context_bloat_pct', 14.2)
  vl_bloat = _get_param_val(vl_agent, 'context_bloat_pct', 11.8)
  dr_bloat = _get_param_val(dr_agent, 'context_bloat_pct', 16.5)

  it_idle = _get_param_val(it_agent, 'idle_ratio_pct', 8.4)
  vl_idle = _get_param_val(vl_agent, 'idle_ratio_pct', 5.2)
  dr_idle = _get_param_val(dr_agent, 'idle_ratio_pct', 9.1)

  it_savings = int(it_agent.monthly_savings_usd) if it_agent else 14820
  vl_savings = int(vl_agent.monthly_savings_usd) if vl_agent else 22400
  dr_savings = int(dr_agent.monthly_savings_usd) if dr_agent else 9650

  cohorts = [
      {
          'cohort': 'Enterprise IT Support & Helpdesk Analysts',
          'primary_agent': 'IT Service Desk',
          'active_dau': 2840,
          'sessions_24h': 9420,
          'tokens_per_user_k': 48.2,
          'context_bloat_before_pct': 64.0,
          'context_bloat_after_pct': it_bloat,
          'idle_ratio_pct': it_idle,
          'baseline_cost_per_1k_usd': 29.40,
          'optimized_cost_per_1k_usd': it_cost,
          'monthly_savings_usd': it_savings,
      },
      {
          'cohort': 'Cloud Platform, SRE & FinOps Engineers',
          'primary_agent': 'VibeLift Analytics & FinOps',
          'active_dau': 1650,
          'sessions_24h': 6180,
          'tokens_per_user_k': 62.5,
          'context_bloat_before_pct': 58.0,
          'context_bloat_after_pct': vl_bloat,
          'idle_ratio_pct': vl_idle,
          'baseline_cost_per_1k_usd': 42.00,
          'optimized_cost_per_1k_usd': vl_cost,
          'monthly_savings_usd': vl_savings,
      },
      {
          'cohort': 'Product Strategy & Executive Research Teams',
          'primary_agent': 'Deep Research',
          'active_dau': 2350,
          'sessions_24h': 5140,
          'tokens_per_user_k': 84.0,
          'context_bloat_before_pct': 71.0,
          'context_bloat_after_pct': dr_bloat,
          'idle_ratio_pct': dr_idle,
          'baseline_cost_per_1k_usd': 19.80,
          'optimized_cost_per_1k_usd': dr_cost,
          'monthly_savings_usd': dr_savings,
      },
  ]
  skill_mcp_breakdown = [
      {
          'resource_name': 'mcp://vibelift-analytics/dashboard',
          'kind': 'Custom MCP Server (Cloud Run)',
          'attached_agent': 'VibeLift Analytics & FinOps',
          'calls_24h': 4120,
          'prompt_tokens_m': 82.4,
          'cache_hit_pct': _get_param_val(vl_agent, 'cache_hit_pct', 89.4),
          'context_bloat_pct': vl_bloat,
          'optimization_applied': 'Pinned 22k MCP tool schema in static prefix + <10ms warm snapshot',
          'monthly_saved_usd': 12900,
      },
      {
          'resource_name': 'mcp://service-desk-escalation',
          'kind': 'ADK Reasoning Engine Tooling',
          'attached_agent': 'IT Service Desk',
          'calls_24h': 6890,
          'prompt_tokens_m': 112.6,
          'cache_hit_pct': _get_param_val(it_agent, 'cache_hit_pct', 91.2),
          'context_bloat_pct': it_bloat,
          'optimization_applied': 'Shared Tier-1/Tier-2 static runbook prefix + N-2 history pruner',
          'monthly_saved_usd': it_savings,
      },
      {
          'resource_name': 'skill://multi-hop-citation-dedup',
          'kind': 'Agent Skill (Retrieval & Synthesis)',
          'attached_agent': 'Deep Research',
          'calls_24h': 3410,
          'prompt_tokens_m': 64.8,
          'cache_hit_pct': 93.1,
          'context_bloat_pct': dr_bloat,
          'optimization_applied': 'Top-3 chunk deduplication + adaptive 429 QPS token-bucket pacing',
          'monthly_saved_usd': dr_savings,
      },
      {
          'resource_name': 'skill://finops-rate-card-attribution',
          'kind': 'Agent Skill (Prompt Cache Economics)',
          'attached_agent': 'VibeLift Analytics & FinOps',
          'calls_24h': 2760,
          'prompt_tokens_m': 38.2,
          'cache_hit_pct': 90.8,
          'context_bloat_pct': 10.4,
          'optimization_applied': 'Static Vertex AI rate-card table locked in cached system prefix',
          'monthly_saved_usd': 9500,
      },
  ]
  total_dau = sum(int(c['active_dau']) for c in cohorts)
  total_monthly_savings = sum(int(c['monthly_savings_usd']) for c in cohorts)
  reduction_pct = round(((29.40 - it_cost) / 29.40) * 100.0, 1)
  return {
      'total_active_dau': total_dau,
      'supported_dau_capacity': '4,000 – 10,000 DAU',
      'baseline_cost_per_1k_turns_usd': 29.40,
      'optimized_cost_per_1k_turns_usd': it_cost,
      'avg_cost_reduction_pct': reduction_pct,
      'per_user_monthly_baseline_usd': 7.85,
      'per_user_monthly_optimized_usd': 0.98,
      'total_monthly_savings_usd': total_monthly_savings,
      'annualized_savings_usd': total_monthly_savings * 12,
      'collection_mode': '@vibelift_telemetry Decorator (Real-Time Message-Passing, <10ms lag)',
      'security_governance': {
          'oauth_cross_project': 'ACTIVE (OAuth 2.0 Consent Configured Across GCP Projects)',
          'pdd_privacy_review': 'COMPLIANT (Anonymized Cohort & Token Attribution)',
          'hosting_target': 'Google Cloud Run (MCP Side-Panel & Fullscreen UI)',
      },
      'cohorts': cohorts,
      'skill_mcp_breakdown': skill_mcp_breakdown,
  }


class VibeLiftAlphaEvolveOptimizer:
  """Manages selectable Gemini Enterprise agents, user parameters, and optimization loops."""

  def __init__(self) -> None:
    """Initializes the multi-agent catalog and sets default active agent."""
    self._agents: dict[str, DemoAgentProfile] = _build_default_agents()
    self.selected_agent_id: str = 'it_service_desk'
    self.selected_optimizer_platform: str = 'alpha_evolve'

  @property
  def active_agent(self) -> DemoAgentProfile:
    """Returns the currently selected DemoAgentProfile."""
    return self._agents[self.selected_agent_id]

  def get_user_centric_payload(self) -> dict[str, object]:
    """Returns user-centric FinOps analytics synced with the live agent profiles."""
    return build_user_centric_analytics(self._agents)

  def select_optimizer_platform(self, platform_id: str) -> dict[str, str]:
    """Switches the active optimization platform (AlphaEvolve, Opus Critic, Vizier, or Hybrid)."""
    clean = (platform_id or '').strip().lower()
    if clean in OPTIMIZER_PLATFORMS:
      self.selected_optimizer_platform = clean
    return OPTIMIZER_PLATFORMS[self.selected_optimizer_platform]

  def get_optimizer_platforms_payload(self) -> dict[str, object]:
    """Returns the active optimization platform and all supported platforms."""
    return {
        'active_platform_id': self.selected_optimizer_platform,
        'active_platform': OPTIMIZER_PLATFORMS[self.selected_optimizer_platform],
        'available_platforms': list(OPTIMIZER_PLATFORMS.values()),
    }

  def select_agent(self, agent_id: str) -> DemoAgentProfile:
    """Switches the currently selected agent by slug, GE resource ID, or display name."""
    raw = (agent_id or '').strip()
    if raw in self._agents:
      self.selected_agent_id = raw
      return self.active_agent
    slug = _slugify_agent_name(raw, raw)
    if slug in self._agents:
      self.selected_agent_id = slug
      return self.active_agent
    for key, profile in self._agents.items():
      if profile.display_name.lower() == raw.lower():
        self.selected_agent_id = key
        return self.active_agent
    return self.active_agent

  def sync_from_ge_fleet(self, fleet_payload: dict[str, object] | None) -> None:
    """Synchronizes the selectable optimization catalog with live agents from Gemini Enterprise."""
    if not isinstance(fleet_payload, dict):
      return
    fleet_agents = fleet_payload.get('agents')
    if not isinstance(fleet_agents, list):
      return
    for raw_agent in fleet_agents:
      if not isinstance(raw_agent, dict):
        continue
      display_name = str(raw_agent.get('display_name') or raw_agent.get('agent_id') or '').strip()
      raw_id = str(raw_agent.get('agent_id') or '').strip()
      if not display_name and not raw_id:
        continue
      slug = _slugify_agent_name(display_name, raw_id)
      backend = raw_agent.get('backend') if isinstance(raw_agent.get('backend'), dict) else {}
      models = backend.get('models') if isinstance(backend.get('models'), list) else []
      live_model = str(models[0]) if models else ''
      type_label = str(raw_agent.get('type_label') or raw_agent.get('type') or 'Gemini Enterprise Agent')
      desc = str(raw_agent.get('description') or type_label)[:90]
      if slug in self._agents:
        profile = self._agents[slug]
        if display_name:
          profile.display_name = display_name
        if live_model:
          profile.model = live_model
      else:
        self._agents[slug] = DemoAgentProfile(
            agent_id=slug,
            display_name=display_name or slug,
            domain=f'{type_label} ({desc})' if desc else type_label,
            model=live_model or 'gemini-2.5-flash',
            health_status='OPTIMIZED (Gen 10 Active)',
            monthly_savings_usd=8400,
            parameters=[
                OptimizationParameter(
                    'latency_ms', 'P95 Response Latency', 'ms', 'LOWER',
                    2100.0, 720.0, 850.0, 35, 'MEETING TARGET (-66%)',
                ),
                OptimizationParameter(
                    'cost_usd', 'Net Cost per 1k Turns', '$', 'LOWER',
                    24.00, 3.80, 5.00, 30, 'MEETING TARGET (-84%)',
                ),
                OptimizationParameter(
                    'accuracy_pct', 'Task Grounding Accuracy', '%', 'HIGHER',
                    90.0, 96.2, 95.0, 25, 'EXCEEDING TARGET (+6.2%)',
                ),
                OptimizationParameter(
                    'cache_hit_pct', 'Prompt Cache Hit Ratio', '%', 'HIGHER',
                    15.0, 88.5, 85.0, 10, 'EXCEEDING TARGET (+73.5%)',
                ),
                OptimizationParameter(
                    'context_bloat_pct', 'Context Bloating Ratio', '%', 'LOWER',
                    60.0, 15.0, 20.0, 10, 'PRUNED (-75%)',
                ),
                OptimizationParameter(
                    'idle_ratio_pct', 'Agent Idle Ratio', '%', 'LOWER',
                    38.0, 9.5, 15.0, 10, 'MEETING TARGET (-75%)',
                ),
            ],
            timeline=[
                TimeSeriesPoint('Day 1 (Baseline)', 0, 2100, 24.00, 90.0, 15.0, 4.5, 'Baseline Audit'),
                TimeSeriesPoint('Day 5 (Gen 10)', 10, 720, 3.80, 96.2, 88.5, 0.0, 'Prefix Cache Locked'),
            ],
            actions=[
                AlphaEvolveActionRecord(
                    generation=10,
                    timestamp='Live Sync • Gen 10',
                    action_title='Static Prompt Prefix Lock & Schema Compression',
                    parameter_targeted='Latency (ms), Cost ($) & Cache Hit Ratio (%)',
                    root_cause_from_logs='Detected dynamic context prefix invalidating static instruction cache.',
                    action_taken='Pinned static instructions and tool schemas to prefix block.',
                    impact_summary='Latency: 2,100ms -> 720ms | Cache Hit: 15% -> 88.5%',
                    diff_snippet='+ STATIC_SYSTEM_PREFIX_CACHE = True',
                    status='ACTIVE PRODUCTION ELITE',
                ),
            ],
        )

  def get_all_agents_dict(self) -> dict[str, dict[str, object]]:
    """Returns full serialized profiles keyed by agent_id so embedded UI can switch agents locally."""
    return {agent_id: profile.to_dict() for agent_id, profile in self._agents.items()}

  def list_agents_summary(self) -> list[dict[str, object]]:
    """Returns summary metadata for all available Gemini Enterprise agents in dropdown."""
    return [
        {
            'agent_id': a.agent_id,
            'display_name': a.display_name,
            'domain': a.domain,
            'model': a.model,
            'health_status': a.health_status,
        }
        for a in self._agents.values()
    ]

  def add_user_parameter(
      self,
      label: str,
      unit: str,
      direction: str,
      baseline_val: float,
      target_val: float,
      weight_pct: int,
  ) -> DemoAgentProfile:
    """Adds or updates a custom user-defined optimization parameter on the active agent."""
    agent = self.active_agent
    clean_label = (label or 'Custom Metric').strip()
    lower_label = clean_label.lower()
    if 'context bloat' in lower_label:
      key = 'context_bloat_pct'
    elif 'idle' in lower_label:
      key = 'idle_ratio_pct'
    else:
      key = re.sub(r'[^a-z0-9]+', '_', lower_label).strip('_')[:24] or 'custom_metric'
    norm_dir = (
        'LOWER'
        if direction.strip().upper() in ('LOWER', 'LOWER_IS_BETTER')
        else 'HIGHER'
    )
    current_val = (
        round(baseline_val * 0.65, 2)
        if norm_dir == 'LOWER'
        else round(min(99.0, baseline_val * 1.12), 2)
    )
    new_param = OptimizationParameter(
        key=key,
        label=clean_label,
        unit=unit,
        direction=norm_dir,
        baseline_value=baseline_val,
        current_value=current_val,
        target_value=target_val,
        weight_pct=weight_pct,
        status='TRACKING IN LOGS (@vibelift_telemetry)',
    )
    for idx, existing in enumerate(agent.parameters):
      if existing.key == key or existing.label.lower() == lower_label:
        agent.parameters[idx] = new_param
        return agent
    agent.parameters.append(new_param)
    return agent

  def inject_anomaly(self) -> DemoAgentProfile:
    """Simulates a live production log anomaly (Cache Bust + Latency Spike)."""
    agent = self.active_agent
    last_gen = agent.timeline[-1].generation if agent.timeline else 14
    agent.health_status = '⚠️ CRITICAL LOG ANOMALY (Cache Bust + 429 Spike)'
    for param in agent.parameters:
      if param.key == 'latency_ms':
        param.current_value = 2390.0
        param.status = '⚠️ SLA BREACH (+246%)'
      elif param.key == 'cost_usd':
        param.current_value = 24.80
        param.status = '⚠️ CACHE BUST SPIKE'
      elif param.key == 'accuracy_pct':
        param.current_value = 88.2
        param.status = '⚠️ ACCURACY REGRESSION (88.2%)'
      elif param.key == 'cache_hit_pct':
        param.current_value = 14.5
        param.status = '⚠️ PREFIX INVALIDATED'
      elif param.key == 'error_rate_pct':
        param.current_value = 9.4
        param.status = '⚠️ 429 QUOTA ERRORS'
      elif param.key == 'context_bloat_pct':
        param.current_value = 68.5
        param.status = '⚠️ CONTEXT BLOAT SPIKE'
      elif param.key == 'idle_ratio_pct':
        param.current_value = 44.0
        param.status = '⚠️ TOOL WAIT BOTTLENECK'

    agent.timeline.append(
        TimeSeriesPoint(
            timestamp_label='Live Anomaly!',
            generation=last_gen,
            latency_ms=2390.0,
            cost_usd=24.80,
            accuracy_pct=88.2,
            cache_hit_pct=14.5,
            error_rate_pct=9.4,
            event_marker='⚠️ Dynamic Prompt Regression Injected',
        )
    )
    return agent

  def run_next_generation(self) -> DemoAgentProfile:
    """Runs the selected optimization platform to heal anomalies and improve all agent parameters."""
    agent = self.active_agent
    platform = OPTIMIZER_PLATFORMS.get(
        self.selected_optimizer_platform, OPTIMIZER_PLATFORMS['alpha_evolve']
    )
    platform_short = platform['name'].split(' (')[0]
    next_gen = (agent.timeline[-1].generation if agent.timeline else 14) + 1
    new_lat = 590.0
    new_cost = 2.85
    new_acc = 97.6
    new_hit = 94.6

    for param in agent.parameters:
      if param.key == 'latency_ms':
        new_lat = max(380.0, round(min(param.current_value, 690.0) * 0.86, 1))
        param.current_value = new_lat
        param.status = f'OPTIMIZED BY GEN {next_gen} (-76%)'
      elif param.key == 'cost_usd':
        new_cost = max(1.80, round(min(param.current_value, 3.45) * 0.84, 2))
        param.current_value = new_cost
        param.status = f'OPTIMIZED BY GEN {next_gen} (-90%)'
      elif param.key == 'accuracy_pct':
        new_acc = min(99.2, round(max(param.current_value, 96.4) + 0.6, 1))
        param.current_value = new_acc
        param.status = f'EXCEEDING TARGET ({new_acc}%)'
      elif param.key == 'cache_hit_pct':
        new_hit = min(97.5, round(max(param.current_value, 91.2) + 1.8, 1))
        param.current_value = new_hit
        param.status = f'LOCKED ({new_hit}% Hit)'
      elif param.key == 'error_rate_pct':
        param.current_value = 0.0
        param.status = 'SELF-HEALED (0.0%)'
      elif param.key == 'context_bloat_pct':
        param.current_value = max(8.5, round(min(param.current_value, 14.2) * 0.85, 1))
        param.status = f'PRUNED ({param.current_value}% Bloat)'
      elif param.key == 'idle_ratio_pct':
        param.current_value = max(4.2, round(min(param.current_value, 8.4) * 0.85, 1))
        param.status = f'OPTIMIZED ({param.current_value}% Idle)'

    agent.health_status = f'OPTIMIZED & HEALED ({platform_short} • Gen {next_gen} Active)'
    agent.monthly_savings_usd += 1850
    agent.timeline.append(
        TimeSeriesPoint(
            timestamp_label=f'Gen {next_gen} (Live)',
            generation=next_gen,
            latency_ms=new_lat,
            cost_usd=new_cost,
            accuracy_pct=new_acc,
            cache_hit_pct=new_hit,
            error_rate_pct=0.0,
            event_marker=f'🧬 {platform_short} Gen {next_gen} Auto-Healed',
        )
    )
    agent.actions.insert(
        0,
        AlphaEvolveActionRecord(
            generation=next_gen,
            timestamp=f'Live Run • Gen {next_gen} ({platform_short})',
            action_title=(
                f'Gen {next_gen} [{platform_short}]: Cross-Turn Context Deduplication & '
                'Speculative Prefix Warming'
            ),
            parameter_targeted='Latency (ms), Cost ($), Context Bloat (%), Idle Ratio (%) & Cache %',
            root_cause_from_logs=(
                'Decorator telemetry (@vibelift_telemetry) detected uncached context bloating '
                'and subagent serialization idle wait in recent message-passing window.'
            ),
            action_taken=(
                f'{platform_short} locked static prompt prefix hash, tuned sliding-window '
                'context pruning, and shared cross-subagent KV cache keys.'
            ),
            impact_summary=(
                f'Latency: -> {new_lat}ms | Cost: -> ${new_cost} | '
                f'Accuracy: -> {new_acc}% | Cache Hit: -> {new_hit}%'
            ),
            diff_snippet=(
                f'+ OPTIMIZER_BACKEND = "{self.selected_optimizer_platform}"\n'
                f'+ GENERATION_ID = {next_gen}\n'
                '+ SPECULATIVE_PREFIX_WARMING = True\n'
                '+ CROSS_SUBAGENT_CACHE_KEY = "vibelift_global_v4"'
            ),
            status=f'AUTO-HEALED & DEPLOYED (PR #{100 + next_gen})',
        ),
    )
    return agent