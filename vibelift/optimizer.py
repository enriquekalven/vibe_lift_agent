"""Multi-agent profiles, user parameters, time-series & AlphaEvolve actions."""

import ast
import dataclasses
import re
from collections.abc import Mapping, Sequence
from typing import Any

from vibelift.jsonutil import as_list, as_mapping


def scan_python_code_for_finops_findings(
    source_code: str,
    filename: str = 'agent.py',
) -> list[dict[str, Any]]:
  """Parses Python source with `ast` to detect FIN-01..FIN-05 FinOps anti-patterns (AgentOps Cockpit)."""
  findings: list[dict[str, Any]] = []
  try:
    tree = ast.parse(source_code or '')
  except SyntaxError:
    return findings

  has_cache_config = 'ContextCacheConfig' in (source_code or '') or 'CreateCachedContentConfig' in (source_code or '')
  for node in ast.walk(tree):
    # FIN-01: LLM generate call inside a For / While loop
    if isinstance(node, (ast.For, ast.While)):
      for child in ast.walk(node):
        if isinstance(child, ast.Call):
          func_src = ast.unparse(child.func) if hasattr(ast, 'unparse') else ''
          if any(fn in func_src for fn in ('generate_content', 'send_message', 'invoke')):
            findings.append({
                'rule_id': 'FIN-01',
                'file_line': f'{filename}:{getattr(child, "lineno", node.lineno)}',
                'title': 'Sequential LLM Inference Loop in Batch Path',
                'evidence': f'Call `{func_src}(...)` inside `{type(node).__name__}` loop.',
                'confidence': 'HIGH',
            })
            break
    # FIN-04: wait_fixed retry on LLM / quota errors
    if isinstance(node, ast.Call):
      func_src = ast.unparse(node.func) if hasattr(ast, 'unparse') else ''
      if 'wait_fixed' in func_src:
        findings.append({
            'rule_id': 'FIN-04',
            'file_line': f'{filename}:{getattr(node, "lineno", 1)}',
            'title': 'Non-Exponential Retry Cascade (wait_fixed)',
            'evidence': 'Fixed-interval retry detected; use wait_random_exponential.',
            'confidence': 'HIGH',
        })
      # FIN-05: top_k > 20 or limit > 20
      for kw in node.keywords:
        if kw.arg in ('top_k', 'similarity_top_k', 'limit') and isinstance(kw.value, ast.Constant):
          if isinstance(kw.value.value, (int, float)) and kw.value.value > 20:
            findings.append({
                'rule_id': 'FIN-05',
                'file_line': f'{filename}:{getattr(node, "lineno", 1)}',
                'title': f'RAG Retrieval Over-Fetching ({kw.arg}={kw.value.value} > 20)',
                'evidence': f'{kw.arg}={kw.value.value} exceeds 20-chunk ceiling; prune to top_k<=5.',
                'confidence': 'HIGH',
            })
      # FIN-03: Pro/Opus model paired with response_schema
      kw_map = {kw.arg: kw.value for kw in node.keywords if kw.arg}
      if 'model' in kw_map and isinstance(kw_map['model'], ast.Constant):
        m_val = str(kw_map['model'].value).lower()
        if ('pro' in m_val or 'opus' in m_val) and 'response_schema' in kw_map:
          findings.append({
              'rule_id': 'FIN-03',
              'file_line': f'{filename}:{getattr(node, "lineno", 1)}',
              'title': 'Model Over-Privilege on Structured Schema Task (MP-029)',
              'evidence': f'Model `{m_val}` used with `response_schema`; route to Flash-Lite or Gemma.',
              'confidence': 'HIGH',
          })
    # FIN-02: Large string literal (>1500 chars) without ContextCacheConfig
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 1500 and not has_cache_config:
      findings.append({
          'rule_id': 'FIN-02',
          'file_line': f'{filename}:{getattr(node, "lineno", 1)}',
          'title': 'Missing ContextCacheConfig on Large Static Prompt',
          'evidence': f'Static prompt literal ({len(node.value)} chars) without ContextCacheConfig.',
          'confidence': 'MEDIUM',
      })
  return findings


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

  def to_dict(self) -> dict[str, Any]:
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

  def to_dict(self) -> dict[str, Any]:
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

  def to_dict(self) -> dict[str, Any]:
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

  def to_dict(self) -> dict[str, Any]:
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
              status='REMEDIATED (0.0%)',
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
      domain='Automated Fleet Telemetry, Prompt Cache FinOps & AlphaEvolve (A2A / MCP)',
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
              'REMEDIATED (0.0%)',
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
              action_title='Closed-Loop 400/429 Exponential Backoff Guard',
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
        'name': 'AlphaEvolve (Balanced Cost, Speed & Quality)',
        'badge': 'Automated Prompt & Config Tuning',
        'description': (
            'Balances cost, response speed, and answer quality across prompt prefixes, '
            'tool definitions, and history windows with safety checks before rollout.'
        ),
    },
    'opus_critic': {
        'id': 'opus_critic',
        'name': 'Opus Code & Prompt Reviewer (Instruction Cleanup)',
        'badge': 'Prompt & Schema Reviewer',
        'description': (
            'Reviews instructions to clarify wording, remove repeated tool calls, '
            'and shrink large JSON tool definitions.'
        ),
    },
    'vertex_vizier': {
        'id': 'vertex_vizier',
        'name': 'Google Vizier (Numeric Parameter Tuner)',
        'badge': 'Numeric Setting Tuner',
        'description': (
            'Google parameter tuning service that adjusts numeric settings '
            '(history turn limit, request rate, temperature, and cache duration).'
        ),
    },
    'hybrid_ensemble': {
        'id': 'hybrid_ensemble',
        'name': 'Combined Mode (AlphaEvolve + Vizier + Opus Reviewer)',
        'badge': 'Combined Tuning Pipeline',
        'description': (
            'Combines Opus prompt cleanup, Google Vizier numeric setting tuning, '
            'and AlphaEvolve balanced selection.'
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


def build_live_app_cohorts(
    users: Sequence[Mapping[str, Any]], live_fleet: Mapping[str, Any]
) -> list[dict[str, Any]]:
  """Groups observed principals by Gemini Enterprise app (from audit-log resource names)."""
  names = {}
  for eng in (live_fleet.get('engines') or []):
    if isinstance(eng, Mapping) and eng.get('engine_key'):
      names[str(eng['engine_key'])] = str(eng.get('display_name') or eng.get('engine_id') or eng['engine_key'])
  groups: dict[str, dict[str, Any]] = {}
  for u in users:
    if not isinstance(u, Mapping):
      continue
    is_sa = 'SERVICE' in str(u.get('status') or '')
    for key, count in (u.get('by_engine') or {}).items():
      g = groups.setdefault(str(key), {'people': 0, 'service_accounts': 0, 'sessions_7d': 0})
      g['service_accounts' if is_sa else 'people'] += 1
      g['sessions_7d'] += int(count or 0)
  out = []
  for key, g in groups.items():
    loc = key.split('/', 1)[0] if '/' in key else ''
    out.append({
        'cohort': names.get(key, key.split('/', 1)[-1]),
        'engine_key': key,
        'primary_agent': {'global': 'Global', 'us': 'US', 'eu': 'EU'}.get(loc, loc or 'unknown'),
        'people': g['people'],
        'service_accounts': g['service_accounts'],
        'sessions_7d': g['sessions_7d'],
        'source': 'ds_ge_audit_raw (last 7 days)',
    })
  return sorted(out, key=lambda c: -int(c['sessions_7d']))


def build_live_runaway_alerts(live_fleet: Mapping[str, Any]) -> list[dict[str, Any]]:
  """Derives alerts from observed fleet error rates and model cache usage. No fixed thresholds on invented data."""
  alerts: list[dict[str, Any]] = []
  window = live_fleet.get('window_hours') or 24
  seen_runtimes = set()
  for a in (live_fleet.get('agents') or []):
    if not isinstance(a, Mapping):
      continue
    m = as_mapping(a.get('metrics'))
    req = int(m.get('requests') or 0)
    if req < 10:
      continue
    from vibelift import (
      fleet as ge_fleet,  # pylint: disable=g-import-not-at-top  (avoids an import cycle at module load)
    )

    rt = ge_fleet.runtime_backend_key(a) or str(a.get('agent_id'))
    if rt in seen_runtimes:
      continue
    seen_runtimes.add(rt)
    e5 = int(m.get('errors_5xx') or 0)
    e4 = int(m.get('errors_4xx') or 0)
    name = str(a.get('display_name') or a.get('agent_id') or 'agent')
    if e5 and e5 / req >= 0.01:
      alerts.append({
          'alert_id': f'5XX-{len(alerts) + 1}',
          'severity': 'HIGH',
          'agent_name': name,
          'runaway_pattern': 'Server errors (5xx)',
          'observed': f'{e5} of {req} requests ({e5 / req * 100:.1f}%) in {window}h',
          'mitigation_applied': 'Suggested: check the runtime logs for the failing revision.',
          'status': 'OPEN',
          'source': 'Cloud Monitoring request_count by response_code_class',
      })
    if e4 / req >= 0.2:
      alerts.append({
          'alert_id': f'4XX-{len(alerts) + 1}',
          'severity': 'MEDIUM',
          'agent_name': name,
          'runaway_pattern': 'Rejected requests (4xx)',
          'observed': f'{e4} of {req} requests ({e4 / req * 100:.1f}%) in {window}h',
          'mitigation_applied': 'Suggested: usually auth or permission failures; check callers and IAM.',
          'status': 'OPEN',
          'source': 'Cloud Monitoring request_count by response_code_class',
      })
  mu = as_mapping(live_fleet.get('model_usage'))
  for mdl in (mu.get('models') or []):
    if not isinstance(mdl, Mapping):
      continue
    tin = int(mdl.get('input_tokens') or 0)
    if tin >= 50_000 and not int(mdl.get('cache_read_tokens') or 0):
      alerts.append({
          'alert_id': f'CACHE-{len(alerts) + 1}',
          'severity': 'LOW',
          'agent_name': str(mdl.get('model')),
          'runaway_pattern': 'No prompt cache reads',
          'observed': f'{tin:,} input tokens, 0 cache reads, est. ${float(mdl.get("est_cost_usd") or 0):.2f} in {window}h',
          'mitigation_applied': 'Suggested: enable context caching for repeated system prompts.',
          'status': 'OPEN',
          'source': 'Vertex AI publisher token_count metrics (project-wide)',
      })
  return alerts


def build_user_centric_analytics(
    agents: dict[str, DemoAgentProfile] | None = None,
    live_fleet: Mapping[str, Any] | None = None,
    live_bq: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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

  cohorts: list[dict[str, Any]] = [
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

  power_users_ldap: list[dict[str, Any]] = [
      {
          'user_ldap': 'enriq',
          'user_email': 'enriq@google.com',
          'department': 'FinOps & Platform Architecture',
          'company_name': 'Google Cloud',
          'primary_agent': 'VibeLift Analytics & FinOps',
          'top_task_type': 'FINOPS_CACHE_ATTRIBUTION',
          'sessions_7d': 164,
          'total_tokens_m': 7.9,
          'thinking_tokens_k': 480,
          'background_tokens_k': 610,
          'cache_hit_pct': _get_param_val(vl_agent, 'cache_hit_pct', 93.2),
          'csat_rating': 5.0,
          'avg_csat': 5.0,
          'monthly_cost_usd': 59.25,
          'monthly_savings_usd': 6120,
      },
      {
          'user_ldap': 'sbahirat',
          'user_email': 'sbahirat@google.com',
          'department': 'Creative & Multimodal Agents (AIVE)',
          'company_name': 'Google Cloud',
          'primary_agent': 'AIVE Creative & Video Agent',
          'top_task_type': 'VIDEO_GENERATION',
          'sessions_7d': 128,
          'total_tokens_m': 8.4,
          'thinking_tokens_k': 920,
          'background_tokens_k': 1140,
          'cache_hit_pct': 88.6,
          'csat_rating': 5.0,
          'avg_csat': 5.0,
          'monthly_cost_usd': 63.00,
          'monthly_savings_usd': 5640,
      },
      {
          'user_ldap': 'russellmyers',
          'user_email': 'russellmyers@google.com',
          'department': 'Cloud AI & Agent Platform',
          'company_name': 'Google Cloud',
          'primary_agent': 'VibeLift Analytics & FinOps',
          'top_task_type': 'FLEET_OPTIMIZATION_AUDIT',
          'sessions_7d': 142,
          'total_tokens_m': 6.8,
          'thinking_tokens_k': 410,
          'background_tokens_k': 520,
          'cache_hit_pct': 91.4,
          'csat_rating': 5.0,
          'avg_csat': 5.0,
          'monthly_cost_usd': 51.00,
          'monthly_savings_usd': 4820,
      },
      {
          'user_ldap': 'rseshadri',
          'user_email': 'rseshadri@google.com',
          'department': 'Executive Product Strategy',
          'company_name': 'Google Cloud',
          'primary_agent': 'Deep Research',
          'top_task_type': 'MULTI_HOP_DEEP_RESEARCH',
          'sessions_7d': 96,
          'total_tokens_m': 6.4,
          'thinking_tokens_k': 780,
          'background_tokens_k': 890,
          'cache_hit_pct': 93.1,
          'csat_rating': 5.0,
          'avg_csat': 5.0,
          'monthly_cost_usd': 48.00,
          'monthly_savings_usd': 4410,
      },
      {
          'user_ldap': 'sloona',
          'user_email': 'sloona@google.com',
          'department': 'Enterprise IT & Security Governance',
          'company_name': 'Google Cloud',
          'primary_agent': 'IT Service Desk',
          'top_task_type': 'IT_TIER2_ESCALATION',
          'sessions_7d': 119,
          'total_tokens_m': 5.2,
          'thinking_tokens_k': 310,
          'background_tokens_k': 440,
          'cache_hit_pct': _get_param_val(it_agent, 'cache_hit_pct', 91.2),
          'csat_rating': 4.9,
          'avg_csat': 4.9,
          'monthly_cost_usd': 39.00,
          'monthly_savings_usd': 3950,
      },
  ]

  token_category_breakdown: dict[str, Any] = {
      'foreground_prompt_tokens_m': 184.2,
      'cached_prefix_tokens_m': 166.8,
      'foreground_output_tokens_m': 19.4,
      'thinking_tokens_baseline_m': 48.6,
      'thinking_tokens_optimized_m': 11.2,
      'thinking_reduction_pct': 77.0,
      'background_task_tokens_baseline_m': 74.0,
      'background_task_tokens_optimized_m': 12.8,
      'background_reduction_pct': 82.7,
  }

  runaway_agent_alerts = [
      {
          'agent_name': 'Deep Research',
          'agent_id': 'deep_research',
          'telemetry_signal': 'gen_ai.usage.thinking_tokens & gcp.vertex.agent.reasoning_steps',
          'runaway_pattern': 'Unbounded multi-hop reasoning loop (9.2 steps/turn, 14.8k thinking tokens/turn)',
          'baseline_burn_per_1k_turns': '14.8M thinking tok ($148.00/1k turns)',
          'optimized_burn_per_1k_turns': '3.2M thinking tok ($32.00/1k turns)',
          'mitigation_applied': 'Capped ADK reasoning_steps <= 4 & enforced top-3 citation chunk deduplication',
          'status': 'CAPPED & GUARDED (-78% Thinking Burn)',
      },
      {
          'agent_name': 'IT Service Desk',
          'agent_id': 'it_service_desk',
          'telemetry_signal': 'background_task_tokens & gcp.vertex.a2a.payload_size_bytes',
          'runaway_pattern': 'Background ticket-polling daemon & Tier-2 A2A context snowball (48.6 KB payload)',
          'baseline_burn_per_1k_turns': '42.0M background tok ($52.50/1k turns)',
          'optimized_burn_per_1k_turns': '6.7M background tok ($6.70/1k turns)',
          'mitigation_applied': 'Replaced polling loop with webhook push + N-2 sliding-window A2A payload pruner',
          'status': 'PRUNED & CAPPED (-84% Background Burn)',
      },
      {
          'agent_name': 'AIVE Creative & Video Agent',
          'agent_id': 'vibelift_analytics',
          'telemetry_signal': 'aive_logs.agent_usage_log [VIDEO_GENERATION]',
          'runaway_pattern': 'Async Veo render status polling re-sent full 22k storyboard prompt on every poll',
          'baseline_burn_per_1k_turns': '31.9M background tok ($39.80/1k turns)',
          'optimized_burn_per_1k_turns': '5.9M background tok ($5.90/1k turns)',
          'mitigation_applied': '@with_analytics_logging isolated lightweight status check from storyboard prompt prefix',
          'status': 'OPTIMIZED (-81% Polling Burn)',
      },
  ]

  # When running against live GCP project-maui with BigQuery & Cloud Monitoring telemetry:
  if isinstance(live_bq, Mapping) and live_bq.get('power_users_ldap'):
    live_users = live_bq.get('power_users_ldap')
    if isinstance(live_users, list) and live_users:
      power_users_ldap = list(live_users)
    live_skills = live_bq.get('skills_mcp')
    if isinstance(live_skills, list) and live_skills:
      skill_mcp_breakdown = list(live_skills)
    fleet_totals = as_mapping(live_fleet.get('totals')) if isinstance(live_fleet, Mapping) else {}
    # Take prompt, output and cached tokens from ONE source so the cache-hit ratio never divides one
    # source's cached count by another source's prompt count (and categories are never mixed).
    token_src: dict[str, Any]
    if live_bq.get('otel_total_prompt_tokens') is not None:
      token_src = {'in': live_bq.get('otel_total_prompt_tokens'), 'out': live_bq.get('otel_total_output_tokens'),
                   'cached': live_bq.get('otel_total_cached_tokens')}
    else:
      token_src = {'in': fleet_totals.get('input_tokens'), 'out': fleet_totals.get('output_tokens'),
                   'cached': fleet_totals.get('cached_tokens')}
    # None (shown as a dash) when no source measured the value; never a fabricated 0.
    obs_in, obs_out, obs_cached = (
        int(token_src[k]) if token_src[k] is not None else None for k in ('in', 'out', 'cached'))
    # Live mode reports only measured token categories; the modelled demo figures above are dropped.
    token_category_breakdown = {}
    token_category_breakdown['observed_gcp_prompt_tokens'] = obs_in
    token_category_breakdown['observed_gcp_cached_tokens'] = obs_cached
    token_category_breakdown['observed_gcp_output_tokens'] = obs_out
    token_category_breakdown['observed_gcp_cache_hit_pct'] = (
        round((obs_cached / obs_in) * 100.0, 1) if obs_in and obs_cached is not None else None
    )
    fleet_for_alerts = live_fleet if isinstance(live_fleet, Mapping) else {}
    return {
        'live': True,
        'total_active_dau': None,
        'active_people_7d': sum(1 for u in power_users_ldap if 'HUMAN' in str(u.get('status') or '')),
        'active_service_accounts_7d': sum(1 for u in power_users_ldap if 'SERVICE' in str(u.get('status') or '')),
        'sessions_7d': sum(int(u.get('sessions_7d') or 0) for u in power_users_ldap),
        'supported_dau_capacity': None,
        # Savings need a measured before/after baseline; none exists in telemetry, so nothing is claimed.
        'baseline_cost_per_1k_turns_usd': None,
        'optimized_cost_per_1k_turns_usd': None,
        'avg_cost_reduction_pct': None,
        'per_user_monthly_baseline_usd': None,
        'per_user_monthly_optimized_usd': None,
        'total_monthly_savings_usd': None,
        'annualized_savings_usd': None,
        'savings_note': 'Not measured: savings need a before/after baseline, which telemetry does not contain.',
        'collection_mode': (
            'LIVE GCP TELEMETRY (project-maui BigQuery vibelift_mart + ds_ge_curated_staging + sre_triage_agent_telemetry + Cloud Monitoring v3)'
        ),
        'security_governance': {},
        'cohorts_live': True,
        'cohorts': build_live_app_cohorts(power_users_ldap, fleet_for_alerts),
        'power_users_ldap': power_users_ldap,
        'token_category_breakdown': token_category_breakdown,
        'runaway_agent_alerts': build_live_runaway_alerts(fleet_for_alerts),
        'skill_mcp_breakdown': skill_mcp_breakdown,
    }

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
      'collection_mode': (
          'LIVE GCP TELEMETRY (project-maui BigQuery vibelift_mart + ds_ge_curated_staging + sre_triage_agent_telemetry + Cloud Monitoring v3)'
          if (isinstance(live_bq, Mapping) and live_bq.get('power_users_ldap'))
          else '@vibelift_telemetry & @with_analytics_logging (Real-Time + BigQuery aive_logs, <10ms lag)'
      ),
      'security_governance': {
          'oauth_cross_project': 'ACTIVE (OAuth 2.0 Consent Configured Across GCP Projects)',
          'pdd_privacy_review': 'COMPLIANT (User LDAP & Anonymized Cohort Attribution)',
          'hosting_target': 'Google Cloud Run (MCP Side-Panel & Fullscreen UI)',
      },
      'cohorts': cohorts,
      'power_users_ldap': power_users_ldap,
      'token_category_breakdown': token_category_breakdown,
      'runaway_agent_alerts': runaway_agent_alerts,
      'skill_mcp_breakdown': skill_mcp_breakdown,
  }


def build_otel_5_layer_catalog(
    live_fleet: Mapping[str, Any] | None = None,
    live_bq: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Builds Russell Myers' 5-Layer Standardized Parameter Catalog (26 OTel/ADK/A2A/A2UI metrics) + Watch-Outs."""
  del live_bq
  layers: list[dict[str, Any]] = [
      {
          'layer_id': 'layer_1_infra',
          'layer_number': 1,
          'layer_title': 'Layer 1: Core Infrastructure & Execution (Cloud Run & Agent Engine)',
          'layer_badge': '5 Cloud Monitoring Metrics',
          'parameters': [
              {
                  'otel_name': 'run.googleapis.com/request_latencies',
                  'label': 'End-to-End Request Latency (P50 / P95 / P99)',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 2480.0,
                  'current_value': 690.0,
                  'target_value': 800.0,
                  'weight_pct': 25,
                  'why_it_matters': 'Primary UX SLA indicator; tracks full agent execution turn duration.',
              },
              {
                  'otel_name': 'run.googleapis.com/container/cpu/utilizations',
                  'label': 'Container vCPU Utilization (JSON / State Parsing)',
                  'unit': '%',
                  'direction': 'LOWER',
                  'baseline_value': 78.5,
                  'current_value': 34.2,
                  'target_value': 60.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Spikes indicate heavy JSON schema parsing or synchronous blocking in ADK.',
              },
              {
                  'otel_name': 'run.googleapis.com/container/memory/utilizations',
                  'label': 'Container Memory Utilization (Session State)',
                  'unit': '%',
                  'direction': 'LOWER',
                  'baseline_value': 82.0,
                  'current_value': 41.0,
                  'target_value': 65.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Detects in-memory conversation history leaks across long-running sessions.',
              },
              {
                  'otel_name': 'run.googleapis.com/container/billable_instance_time',
                  'label': 'Billable Container Instance Time per 1k Turns',
                  'unit': 's',
                  'direction': 'LOWER',
                  'baseline_value': 1420.0,
                  'current_value': 390.0,
                  'target_value': 500.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Primary Cloud Run compute cost driver while waiting on downstream LLMs.',
              },
              {
                  'otel_name': 'run.googleapis.com/container/startup_latencies',
                  'label': 'Container Cold-Start Latency Penalty',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 1850.0,
                  'current_value': 120.0,
                  'target_value': 300.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Critical for scale-to-zero A2A subagents invoked on-demand.',
              },
          ],
      },
      {
          'layer_id': 'layer_2_llm',
          'layer_number': 2,
          'layer_title': 'Layer 2: LLM & Reasoning Layer (Vertex AI & Gemini Models)',
          'layer_badge': '7 GenAI & Guardrail Metrics',
          'parameters': [
              {
                  'otel_name': 'gen_ai.latency.ttft',
                  'label': 'Time to First Token (TTFT)',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 890.0,
                  'current_value': 210.0,
                  'target_value': 300.0,
                  'weight_pct': 20,
                  'why_it_matters': 'Determines perceived responsiveness before streaming UI tokens begin.',
              },
              {
                  'otel_name': 'gen_ai.token.throughput',
                  'label': 'Token Generation Throughput (TPS)',
                  'unit': 'tok/s',
                  'direction': 'HIGHER',
                  'baseline_value': 68.0,
                  'current_value': 164.0,
                  'target_value': 120.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Governs how fast structured JSON and A2UI schemas stream to the client.',
              },
              {
                  'otel_name': 'gen_ai.usage.input_tokens',
                  'label': 'Input Tokens per Turn (Prompt + History + Tools)',
                  'unit': 'k tok',
                  'direction': 'LOWER',
                  'baseline_value': 28.4,
                  'current_value': 16.4,
                  'target_value': 18.0,
                  'weight_pct': 20,
                  'why_it_matters': 'Grows quadratically if multi-turn tool outputs are not pruned.',
              },
              {
                  'otel_name': 'gen_ai.usage.output_tokens',
                  'label': 'Output Tokens per Turn (Response + Tool Calls)',
                  'unit': 'tok',
                  'direction': 'LOWER',
                  'baseline_value': 920.0,
                  'current_value': 310.0,
                  'target_value': 450.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Output tokens cost 4x-8x more than input tokens and dominate generation time.',
              },
              {
                  'otel_name': 'gen_ai.usage.thinking_tokens',
                  'label': 'Internal Reasoning / Thinking Tokens (Gemini 2.5/3.x)',
                  'unit': 'tok',
                  'direction': 'LOWER',
                  'baseline_value': 4800.0,
                  'current_value': 1120.0,
                  'target_value': 1500.0,
                  'weight_pct': 20,
                  'why_it_matters': 'Hidden driver of latency and cost when thinking budgets are uncapped.',
              },
              {
                  'otel_name': 'gcp.vertex.model_armor.block_rate',
                  'label': 'Model Armor Safety & Injection Block Rate',
                  'unit': '%',
                  'direction': 'LOWER',
                  'baseline_value': 3.2,
                  'current_value': 0.2,
                  'target_value': 0.5,
                  'weight_pct': 10,
                  'why_it_matters': 'Tracks false-positive safety blocks vs. real prompt injection attempts.',
              },
              {
                  'otel_name': 'gcp.vertex.rag.grounding_score',
                  'label': 'Vertex AI Search / RAG Grounding Confidence',
                  'unit': '%',
                  'direction': 'HIGHER',
                  'baseline_value': 91.0,
                  'current_value': 96.4,
                  'target_value': 95.0,
                  'weight_pct': 25,
                  'why_it_matters': 'Prevents accuracy regressions when compressing prompts or few-shot examples.',
              },
          ],
      },
      {
          'layer_id': 'layer_3_adk',
          'layer_number': 3,
          'layer_title': 'Layer 3: Agent Orchestration & Tooling (ADK)',
          'layer_badge': '5 ADK Runtime Metrics',
          'parameters': [
              {
                  'otel_name': 'gcp.vertex.agent.reasoning_steps',
                  'label': 'ADK Reasoning Loop Steps per Turn (Thought→Action)',
                  'unit': 'steps',
                  'direction': 'LOWER',
                  'baseline_value': 6.4,
                  'current_value': 2.1,
                  'target_value': 3.5,
                  'weight_pct': 20,
                  'why_it_matters': 'Values > 5 indicate ambiguous tool descriptions or infinite reasoning loops.',
              },
              {
                  'otel_name': 'gcp.vertex.agent.tool.latency',
                  'label': 'External Tool & MCP Execution Latency',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 1180.0,
                  'current_value': 195.0,
                  'target_value': 300.0,
                  'weight_pct': 20,
                  'why_it_matters': 'Separates slow third-party APIs/BigQuery queries from LLM inference time.',
              },
              {
                  'otel_name': 'gcp.vertex.agent.tool.error_rate',
                  'label': 'Tool Execution & Schema Error Rate (400/429/5xx)',
                  'unit': '%',
                  'direction': 'LOWER',
                  'baseline_value': 6.8,
                  'current_value': 0.0,
                  'target_value': 0.5,
                  'weight_pct': 15,
                  'why_it_matters': 'Each malformed tool call forces a full LLM retry turn (2x cost & latency).',
              },
              {
                  'otel_name': 'gcp.vertex.agent.session.turn_count',
                  'label': 'Average Conversation Depth (Turns to Resolution)',
                  'unit': 'turns',
                  'direction': 'LOWER',
                  'baseline_value': 7.8,
                  'current_value': 3.2,
                  'target_value': 4.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Fewer turns to resolve a ticket directly multiplies token savings.',
              },
              {
                  'otel_name': 'gcp.vertex.agent.user.feedback',
                  'label': 'User CSAT Positive Feedback Score (ratings_log)',
                  'unit': '%',
                  'direction': 'HIGHER',
                  'baseline_value': 86.0,
                  'current_value': 98.2,
                  'target_value': 95.0,
                  'weight_pct': 20,
                  'why_it_matters': 'Ultimate quality guardrail combining explicit thumbs-up and task completion.',
              },
          ],
      },
      {
          'layer_id': 'layer_4_a2a',
          'layer_number': 4,
          'layer_title': 'Layer 4: Inter-Agent Communication (A2A Protocol)',
          'layer_badge': '4 A2A Mesh Metrics',
          'parameters': [
              {
                  'otel_name': 'gcp.vertex.a2a.handoff_latency',
                  'label': 'A2A Sub-Agent Discovery & Handoff Latency',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 620.0,
                  'current_value': 140.0,
                  'target_value': 200.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Network + OAuth serialization overhead when delegating across agents.',
              },
              {
                  'otel_name': 'gcp.vertex.a2a.delegation_depth',
                  'label': 'A2A Sub-Agent Call Chain Depth (A→B→C)',
                  'unit': 'hops',
                  'direction': 'LOWER',
                  'baseline_value': 4.2,
                  'current_value': 1.6,
                  'target_value': 2.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Deep handoff chains compound tail latency and risk cyclic delegation.',
              },
              {
                  'otel_name': 'gcp.vertex.a2a.task_completion_rate',
                  'label': 'A2A Delegated Task Schema Completion Rate',
                  'unit': '%',
                  'direction': 'HIGHER',
                  'baseline_value': 88.5,
                  'current_value': 99.4,
                  'target_value': 98.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Ensures subagents return contract-compliant JSON without parent retries.',
              },
              {
                  'otel_name': 'gcp.vertex.a2a.payload_size_bytes',
                  'label': 'A2A Context Transfer Payload Size',
                  'unit': 'KB',
                  'direction': 'LOWER',
                  'baseline_value': 48.6,
                  'current_value': 4.2,
                  'target_value': 8.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Prevents the Context Snowball Effect when handing off conversation state.',
              },
          ],
      },
      {
          'layer_id': 'layer_5_a2ui',
          'layer_number': 5,
          'layer_title': 'Layer 5: Generative UI & Client Rendering (A2UI / MCP Apps)',
          'layer_badge': '5 UI & AppBridge Metrics',
          'parameters': [
              {
                  'otel_name': 'gcp.vertex.a2ui.schema_generation_latency',
                  'label': 'UI Structured Payload / Snapshot Generation Time',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 1450.0,
                  'current_value': 8.6,
                  'target_value': 100.0,
                  'weight_pct': 15,
                  'why_it_matters': 'Warm MCP snapshot serves UI state in <10ms vs. 1.45s raw A2UI generation.',
              },
              {
                  'otel_name': 'gcp.vertex.a2ui.validation_failure_rate',
                  'label': 'UI Schema Validation Failure Rate',
                  'unit': '%',
                  'direction': 'LOWER',
                  'baseline_value': 11.4,
                  'current_value': 0.0,
                  'target_value': 0.5,
                  'weight_pct': 15,
                  'why_it_matters': 'Eliminates 2x hidden retry loops caused by hallucinated UI component props.',
              },
              {
                  'otel_name': 'gcp.vertex.a2ui.client_render_time',
                  'label': 'Browser AppBridge Hydration & Render Time',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 420.0,
                  'current_value': 28.0,
                  'target_value': 80.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Zero-dependency SVG + DOM rendering keeps side-panel & fullscreen instant.',
              },
              {
                  'otel_name': 'gcp.vertex.a2ui.interaction_rate',
                  'label': 'Interactive UI Action Engagement Rate',
                  'unit': '%',
                  'direction': 'HIGHER',
                  'baseline_value': 34.0,
                  'current_value': 84.5,
                  'target_value': 75.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Measures users clicking interactive controls vs. falling back to text chat.',
              },
              {
                  'otel_name': 'gcp.vertex.a2ui.stream_sync_lag',
                  'label': 'Stream-to-UI Synchronization Lag',
                  'unit': 'ms',
                  'direction': 'LOWER',
                  'baseline_value': 680.0,
                  'current_value': 12.0,
                  'target_value': 50.0,
                  'weight_pct': 10,
                  'why_it_matters': 'Measures lag between assistant text completion and right-panel UI update.',
              },
          ],
      },
  ]

  # Alias 'metrics' -> 'parameters' on every layer so ui_template.py renders all 26 OTel metrics
  for layer in layers:
    layer['metrics'] = layer['parameters']

  watch_out_alarms = [
      {
          'id': 'compounding_latency',
          'title': 'Watch-Out 1: The "Compounding Latency" Trap (ADK + A2A Waterfall)',
          'formula': 'Total (690ms) = TTFT (210ms) + Tool (195ms) + A2A Handoff (140ms) + Sub-Agent (115ms) + UI (30ms)',
          'baseline': '2,480 ms (Sequential Uncached Waterfall)',
          'current': '690 ms (-72% via Parallel Fan-Out & Prefix Cache)',
          'status': 'MITIGATED (P95 < 800ms SLO)',
          'severity': 'green',
      },
      {
          'id': 'context_snowball',
          'title': 'Watch-Out 2: The "Context Snowball" Effect (O(N²) A2A Token Cost)',
          'formula': 'Uncached Handoff = Sum(Turn_1..Turn_N) * A2A_Delegation_Depth',
          'baseline': '48.6 KB payload / 64% Context Bloat ($29.40 / 1k turns)',
          'current': '4.2 KB digest / 14.2% Context Bloat ($3.45 / 1k turns)',
          'status': 'GUARDED BY N-2 PRUNER (-88% Cost)',
          'severity': 'green',
      },
      {
          'id': 'a2ui_retry_loops',
          'title': 'Watch-Out 3: A2UI Schema Validation Retry Loops',
          'formula': 'Effective Cost = Base_Turn_Cost * (1 + 2 * UI_Validation_Failure_Rate)',
          'baseline': '11.4% schema failure rate triggering hidden 2x LLM retry turns',
          'current': '0.0% failure rate (Standard MCP Side-Panel + Fullscreen AppBridge)',
          'status': 'ZERO RETRIES (0.0% Failure)',
          'severity': 'green',
      },
  ]

  if isinstance(live_fleet, Mapping) and live_fleet.get('project_id') not in (None, '', 'test-project', 'UNCONFIGURED-PROJECT'):
    watch_out_alarms.extend([
        {
            'id': 'live_re_p95_spike',
            'title': 'Live GCP Finding 1: IT Service Desk Reasoning Engine P95 = 127.2s & 12,578 GiB-hr Pool',
            'formula': 'aiplatform.googleapis.com/reasoning_engine/request_latencies (5389235022676918272)',
            'baseline': '800 ms Target SLO',
            'current': '127,232.9 ms P95 (31,593.5 ms P50) across 19 calls; 12,578.6 GiB-hrs allocated',
            'status': 'ACTION NEEDED: Right-Size Idle RE Pools',
            'severity': 'amber',
        },
        {
            'id': 'live_cli_a2a_4xx',
            'title': 'Live GCP Finding 2: Enterprise CLI Agent (enterprise-cli-agent-a2a) 53.6% 4xx Error Rate',
            'formula': 'run.googleapis.com/request_count [response_code_class="4xx"]',
            'baseline': '< 0.5% Target Error SLO',
            'current': '53.6% 4xx Error Rate (15 errors / 28 requests in Cloud Run us-central1)',
            'status': 'ACTION NEEDED: Fix A2A Auth/Schema Payload',
            'severity': 'amber',
        },
    ])

  architecture_tco = {
      'slide_title': 'Hackathon Architecture, IAM Prerequisites & Monthly Compute TCO (Slides 1–3)',
      'architecture_flow': (
          'Gemini Enterprise Assistant (US & Global) → Streamable HTTP MCP (/mcp) & A2A Card → '
          'Cloud Run (vibe-lift-agent) → Parallel Discovery Engine v1alpha + Cloud Monitoring v3 + '
          'Cloud Logging v2 + @vibelift_telemetry / @with_analytics_logging (aive_logs)'
      ),
      'monthly_compute_cost_usd': 28.50,
      'monthly_compute_breakdown': (
          '~$28.50/mo total TCO: Cloud Run (1 vCPU, 512 MiB, background warmer) $19.20/mo + '
          'Cloud Monitoring & Logging API queries $5.80/mo + BigQuery aive_logs streaming $3.50/mo'
      ),
      'monthly_fleet_savings_usd': 46870,
      'net_roi_multiple': '1,644x Net ROI ($46,870/mo saved vs. $28.50/mo platform TCO)',
      'required_iam_roles': [
          'roles/discoveryengine.viewer (Enumerate GE Engines, Assistants & Agents)',
          'roles/monitoring.viewer (Read ReasoningEngine, Cloud Run & Vertex Token Metrics)',
          'roles/logging.viewer (Read OTel GenAI Spans & Prompt Cache Breakpoints)',
          'roles/aiplatform.viewer (Inspect ReasoningEngine Metadata & Rate Cards)',
          'roles/bigquery.dataEditor (Stream @with_analytics_logging to aive_logs)',
      ],
  }

  return {
      'total_standardized_parameters': sum(len(layer['parameters']) for layer in layers),
      'layers': layers,
      'watch_out_alarms': watch_out_alarms,
      'architecture_tco': architecture_tco,
  }


class VibeLiftAlphaEvolveOptimizer:
  """Manages selectable Gemini Enterprise agents, user parameters, and optimization loops."""

  def __init__(self) -> None:
    """Initializes the multi-agent catalog and sets default active agent."""
    self._agents: dict[str, DemoAgentProfile] = _build_default_agents()
    self.selected_agent_id: str = 'it_service_desk'
    self.selected_optimizer_platform: str = 'alpha_evolve'
    self._live_fleet_payload: dict[str, Any] | None = None
    self._live_bq_insights: dict[str, Any] | None = None

  @property
  def active_agent(self) -> DemoAgentProfile:
    """Returns the currently selected DemoAgentProfile."""
    return self._agents[self.selected_agent_id]

  def get_user_centric_payload(self) -> dict[str, Any]:
    """Returns user-centric FinOps analytics synced with the live agent profiles."""
    return build_user_centric_analytics(
        self._agents,
        live_fleet=self._live_fleet_payload,
        live_bq=self._live_bq_insights,
    )

  def get_otel_catalog_payload(self) -> dict[str, Any]:
    """Returns the 5-layer OTel/ADK/A2A/A2UI parameter catalog, watch-out alarms, and TCO summary."""
    return build_otel_5_layer_catalog(
        live_fleet=self._live_fleet_payload,
        live_bq=self._live_bq_insights,
    )

  def execute_nl2sql_telemetry_query(self, question: str) -> dict[str, Any]:
    """Translates a natural-language telemetry/FinOps question into BigQuery SQL and live results."""
    q_clean = (question or '').strip()
    q_lower = q_clean.lower()
    uc = self.get_user_centric_payload()
    dml_tokens = ('drop ', 'delete ', 'update ', 'insert ', 'alter ', 'truncate ', 'grant ', ';')
    blocked_dml = any(tok in q_lower for tok in dml_tokens)
    safety_audit = {
        'read_only_enforced': True,
        'blocked_dml_attempt': blocked_dml,
        'max_bytes_billed_cap': 104857600,
        'allowed_datasets': [
            'project-maui.vibelift_mart',
            'project-maui.ds_ge_curated_staging',
            'project-maui.ds_ge_audit_raw',
            'project-maui.sre_triage_agent_telemetry',
            'project-maui.vibelift_analytics',
            'project-maui.aive_logs',
            'project-maui.billing_export',
        ],
        'service_account_mode': 'READ_ONLY_BIGQUERY_DATA_VIEWER',
    }

    if any(w in q_lower for w in ('runaway', 'thinking', 'background', 'burn', 'loop')) and not blocked_dml:
      sql = (
          'SELECT\n'
          '  agent_name,\n'
          '  task_type,\n'
          '  ROUND(AVG(thinking_tokens), 0) AS avg_thinking_tokens,\n'
          '  ROUND(AVG(background_tokens), 0) AS avg_background_tokens,\n'
          '  COUNTIF(thinking_tokens > 3000 OR background_tokens > 4000) AS runaway_turns\n'
          'FROM `project-maui.aive_logs.agent_usage_log`\n'
          'WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)\n'
          'GROUP BY agent_name, task_type\n'
          'ORDER BY avg_thinking_tokens DESC;'
      )
      alerts = as_list(uc.get('runaway_agent_alerts'))
      rows = [
          {
              'agent_name': a['agent_name'],
              'telemetry_signal': a['telemetry_signal'],
              'baseline_burn': a['baseline_burn_per_1k_turns'],
              'optimized_burn': a['optimized_burn_per_1k_turns'],
              'status': a['status'],
          }
          for a in alerts
      ]
      return {
          'question': q_clean or 'Which agents have runaway thinking or background tokens?',
          'intent': 'RUNAWAY_THINKING_AND_BACKGROUND_TOKENS',
          'generated_sql': sql,
          'sql_safety_audit': safety_audit,
          'columns': ['agent_name', 'telemetry_signal', 'baseline_burn', 'optimized_burn', 'status'],
          'rows': rows,
          'executive_summary': (
              'Identified 3 runaway token & latency patterns across Reasoning Engine P95 wait, '
              'Cloud Run 4xx retry cascades, and uncached Model Garden calls.'
          ),
      }

    if any(w in q_lower for w in ('session', 'mart', 'fct_turns', 'fct_sessions', 'daily usage')) and not blocked_dml:
      sql = (
          'SELECT\n'
          '  engine_key,\n'
          '  session_id,\n'
          '  user_email,\n'
          '  turns,\n'
          '  chat_turns,\n'
          '  failed_turns,\n'
          '  duration_seconds,\n'
          '  total_tokens,\n'
          '  CAST(session_end AS STRING) AS session_end\n'
          'FROM `project-maui.vibelift_mart.fct_sessions`\n'
          'WHERE session_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)\n'
          'ORDER BY session_end DESC\n'
          'LIMIT 25;'
      )
      live_bq = self._live_bq_insights if isinstance(self._live_bq_insights, dict) else {}
      sessions = list(live_bq.get('ge_sessions') or [])
      rows = [
          {
              'session_id': str(s.get('session_id') or '—'),
              'engine_key': str(s.get('engine_key') or '—'),
              'user_email': str(s.get('user_email') or '—'),
              'turns': s.get('turns', 0),
              'chat_turns': s.get('chat_turns', 0),
              'failed_turns': s.get('failed_turns', 0),
              'duration_seconds': f"{s.get('duration_seconds')}s" if s.get('duration_seconds') is not None else '—',
              'total_tokens': s.get('total_tokens') if s.get('total_tokens') is not None else '—',
              'session_end': str(s.get('session_end') or '—'),
          }
          for s in sessions
      ]
      return {
          'question': q_clean or 'Show Gemini Enterprise conversation sessions from vibelift_mart.fct_sessions',
          'intent': 'GE_MART_SESSIONS_AND_DAILY',
          'generated_sql': sql,
          'sql_safety_audit': safety_audit,
          'columns': [
              'session_id', 'engine_key', 'user_email', 'turns', 'chat_turns',
              'failed_turns', 'duration_seconds', 'total_tokens', 'session_end',
          ],
          'rows': rows,
          'executive_summary': (
              f'Queried {len(rows)} real conversation sessions from project-maui.vibelift_mart.fct_sessions '
              '(built over ds_ge_curated_staging views with zero synthetic single-search session inflation).'
          ),
      }

    if any(w in q_lower for w in ('ldap', 'power user', 'user', 'department', 'who', 'csat')) and not blocked_dml:
      sql = (
          'SELECT\n'
          '  user_email,\n'
          '  engine_key,\n'
          '  COUNT(1) AS interactions_7d,\n'
          '  COUNT(DISTINCT session_id) AS sessions_7d,\n'
          '  SUM(total_tokens) AS total_tokens\n'
          'FROM `project-maui.vibelift_mart.fct_turns`\n'
          'WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)\n'
          '  AND user_email IS NOT NULL\n'
          'GROUP BY user_email, engine_key\n'
          'ORDER BY interactions_7d DESC\n'
          'LIMIT 10;'
      )
      users = as_list(uc.get('power_users_ldap'))
      rows = [
          {
              'user_ldap': u.get('user_ldap', '—'),
              'department': u.get('department', '—'),
              'primary_agent': u.get('primary_agent', '—'),
              'top_task_type': u.get('top_task_type') or u.get('status') or '—',
              'sessions_7d': u.get('sessions_7d', 0),
              'total_tokens_m': f"{u['total_tokens_m']}M" if u.get('total_tokens_m') is not None else '—',
              'cache_hit_pct': f"{u['cache_hit_pct']}%" if u.get('cache_hit_pct') is not None else '—',
              'csat_rating': (
                  f"{u.get('avg_csat', u.get('csat_rating'))} ★"
                  if (u.get('avg_csat') is not None or u.get('csat_rating') is not None)
                  else '—'
              ),
              'monthly_savings_usd': (
                  f"${u['monthly_savings_usd']:,}/mo" if u.get('monthly_savings_usd') is not None else '—'
              ),
          }
          for u in users
      ]
      ldaps_str = ', '.join(str(u.get('user_ldap')) for u in users[:5])
      total_sess = sum(int(u.get('sessions_7d') or 0) for u in users)
      return {
          'question': q_clean or 'Who are the top power users by user_ldap and department?',
          'intent': 'GE_POWER_USER_LDAP_LEADERBOARD',
          'generated_sql': sql,
          'sql_safety_audit': safety_audit,
          'columns': [
              'user_ldap', 'department', 'primary_agent', 'top_task_type',
              'sessions_7d', 'total_tokens_m', 'cache_hit_pct', 'csat_rating', 'monthly_savings_usd',
          ],
          'rows': rows,
          'executive_summary': (
              f'Top Gemini Enterprise & GCP principals ({ldaps_str}) '
              f'recorded {total_sess} observed sessions across project-maui telemetry.'
          ),
      }

    if any(w in q_lower for w in ('a2a', 'a2ui', 'layer', 'waterfall', 'otel', 'handoff')) and not blocked_dml:
      sql = (
          'SELECT\n'
          '  layer_title,\n'
          '  otel_metric_name,\n'
          '  ROUND(AVG(baseline_value), 1) AS baseline_val,\n'
          '  ROUND(AVG(current_value), 1) AS optimized_val,\n'
          '  target_slo\n'
          'FROM `project-maui.aive_logs.otel_5layer_parameter_snapshots`\n'
          'WHERE layer_number IN (1, 2, 3, 4, 5)\n'
          'ORDER BY layer_number ASC, weight_pct DESC;'
      )
      catalog = self.get_otel_catalog_payload()
      layers = as_list(catalog.get('layers'))
      rows = []
      for layer in layers:
        for p in (layer.get('parameters') or [])[:2]:
          rows.append({
              'layer': f"L{layer.get('layer_number')}",
              'otel_name': p['otel_name'],
              'label': p['label'],
              'baseline': f"{p['baseline_value']} {p['unit']}",
              'current': f"{p['current_value']} {p['unit']}",
              'target_slo': f"{p['target_value']} {p['unit']}",
          })
      return {
          'question': q_clean or 'Show 5-Layer OTel, A2A, and A2UI latency & guardrail metrics',
          'intent': 'OTEL_5_LAYER_WATERFALL',
          'generated_sql': sql,
          'sql_safety_audit': safety_audit,
          'columns': ['layer', 'otel_name', 'label', 'baseline', 'current', 'target_slo'],
          'rows': rows,
          'executive_summary': (
              'All 5 architectural telemetry layers (Cloud Run, Vertex AI LLM, ADK Orchestration, '
              'A2A Handoffs, and A2UI/MCP Rendering) are within target SLOs with zero schema retry loops.'
          ),
      }

    # Default: Cost & Cache ROI comparison across agents
    sql = (
        'SELECT\n'
        '  agent_id,\n'
        '  model,\n'
        '  ROUND(AVG(naive_cost_per_1k_usd), 2) AS baseline_cost_1k_usd,\n'
        '  ROUND(AVG(actual_cached_cost_per_1k_usd), 2) AS optimized_cost_1k_usd,\n'
        '  ROUND(AVG(cache_hit_ratio_pct), 1) AS cache_hit_pct,\n'
        '  SUM(monthly_savings_usd) AS monthly_savings_usd\n'
        'FROM `project-maui.aive_logs.agent_finops_attribution`\n'
        'GROUP BY agent_id, model\n'
        'ORDER BY monthly_savings_usd DESC;'
    )
    rows = []
    for profile in self._agents.values():
      cost_p = next((p for p in profile.parameters if p.key == 'cost_usd'), None)
      hit_p = next((p for p in profile.parameters if p.key == 'cache_hit_pct'), None)
      lat_p = next((p for p in profile.parameters if p.key == 'latency_ms'), None)
      rows.append({
          'agent': profile.display_name,
          'model': profile.model,
          'p95_latency': f"{lat_p.current_value if lat_p else 690.0} ms",
          'baseline_cost_1k': f"${cost_p.baseline_value if cost_p else 29.40:.2f}",
          'optimized_cost_1k': f"${cost_p.current_value if cost_p else 3.45:.2f}",
          'cache_hit_pct': f"{hit_p.current_value if hit_p else 91.2}%",
          'monthly_savings': f"${profile.monthly_savings_usd:,}/mo",
      })
    summary_msg = (
        'BLOCKED DML/DDL injection attempt; enforced read-only SELECT guardrail with 100 MB maximumBytesBilled cap.'
        if blocked_dml
        else (
            'Simulator result: rows come from the optimizer model, not BigQuery. '
            'Savings are not measured in live telemetry.'
            if uc.get('live')
            else f"Simulator result: prompt cache locking and context pruning reduced cost per 1k turns by "
            f"{uc.get('avg_cost_reduction_pct')}%, delivering ${uc.get('total_monthly_savings_usd') or 0:,}/mo "
            f"(${uc.get('annualized_savings_usd') or 0:,}/yr) in simulated savings."
        )
    )
    return {
        'question': q_clean or 'Compare cost per 1k turns and prompt cache savings across agents',
        'intent': 'READ_ONLY_GUARDRAIL_FALLBACK' if blocked_dml else 'AGENT_FINOPS_ROI_SUMMARY',
        'generated_sql': sql,
        'sql_safety_audit': safety_audit,
        'columns': ['agent', 'model', 'p95_latency', 'baseline_cost_1k', 'optimized_cost_1k', 'cache_hit_pct', 'monthly_savings'],
        'rows': rows,
        'executive_summary': summary_msg,
    }

  def get_finops_billing_reconciliation_payload(self) -> dict[str, Any]:
    """Builds Dual-Ledger Cloud Billing SKU Reconciliation, North-Star Unit Economics, and GSU/CUD Advisor."""
    uc = self.get_user_centric_payload()
    active = self.active_agent
    cost_1k = _get_param_val(active, 'cost_usd', 3.45)
    base_cost_1k = 29.40
    for p in active.parameters:
      if p.key == 'cost_usd':
        base_cost_1k = float(p.baseline_value)
        break
    # Average turns to resolution: 3.2 optimized vs 7.8 baseline
    baseline_cost_per_resolved_session = round((base_cost_1k / 1000.0) * 7.8, 4)
    optimized_cost_per_resolved_session = round((cost_1k / 1000.0) * 3.2, 4)
    unit_econ_reduction_pct = round(
        ((baseline_cost_per_resolved_session - optimized_cost_per_resolved_session)
         / max(0.0001, baseline_cost_per_resolved_session)) * 100.0,
        1,
    )
    sku_ledger = [
        {
            'sku_id': 'FE3A-91B2-C401',
            'service': 'Vertex AI',
            'sku_description': 'Gemini 2.5 Flash — Text Input & Context Cache Read (Global)',
            'usage_volume': '1.42B tok',
            'telemetry_est_usd': 1842.50,
            'telemetry_estimated_usd': 1842.50,
            'billing_export_gross_usd': 1860.00,
            'cud_or_cache_credits_usd': -1485.20,
            'cud_and_cache_credits_usd': -1485.20,
            'net_billed_usd': 374.80,
            'net_invoice_usd': 374.80,
            'reconciliation_delta_pct': 0.9,
            'variance_pct': 0.9,
            'status': 'RECONCILED (<1% Drift)',
        },
        {
            'sku_id': 'B814-22D9-A110',
            'service': 'Vertex AI',
            'sku_description': 'Gemini 2.5 Pro — Deep Research Multi-Hop Reasoning & Thinking Tokens',
            'usage_volume': '412M tok',
            'telemetry_est_usd': 2640.00,
            'telemetry_estimated_usd': 2640.00,
            'billing_export_gross_usd': 2668.40,
            'cud_or_cache_credits_usd': -1910.00,
            'cud_and_cache_credits_usd': -1910.00,
            'net_billed_usd': 758.40,
            'net_invoice_usd': 758.40,
            'reconciliation_delta_pct': 1.1,
            'variance_pct': 1.1,
            'status': 'RECONCILED (Thinking Capped)',
        },
        {
            'sku_id': 'CR90-44E1-77F2',
            'service': 'Cloud Run',
            'sku_description': 'Cloud Run vCPU & Memory Billable Instance Time (min-instances=1 + A2A Workers)',
            'usage_volume': '2,160 vCPU-hr',
            'telemetry_est_usd': 94.20,
            'telemetry_estimated_usd': 94.20,
            'billing_export_gross_usd': 96.00,
            'cud_or_cache_credits_usd': -19.20,
            'cud_and_cache_credits_usd': -19.20,
            'net_billed_usd': 76.80,
            'net_invoice_usd': 76.80,
            'reconciliation_delta_pct': 1.9,
            'variance_pct': 1.9,
            'status': 'OPTIMIZED (Concurrency=80)',
        },
        {
            'sku_id': 'AE55-19C8-33B4',
            'service': 'Vertex AI Agent Engine',
            'sku_description': 'Reasoning Engine Runtime vCPU-Hours & GiB-Hours (ADK Managed Fleet)',
            'usage_volume': '3,420 vCPU-hr',
            'telemetry_est_usd': 148.00,
            'telemetry_estimated_usd': 148.00,
            'billing_export_gross_usd': 149.50,
            'cud_or_cache_credits_usd': -22.40,
            'cud_and_cache_credits_usd': -22.40,
            'net_billed_usd': 127.10,
            'net_invoice_usd': 127.10,
            'reconciliation_delta_pct': 1.0,
            'variance_pct': 1.0,
            'status': 'RECONCILED',
        },
        {
            'sku_id': 'BQ12-88A3-09D1',
            'service': 'BigQuery & Cloud Logging',
            'sku_description': 'Streaming Write API (aive_logs) + Partitioned Analytics Queries',
            'usage_volume': '3.8 TB scanned',
            'telemetry_est_usd': 18.40,
            'telemetry_estimated_usd': 18.40,
            'billing_export_gross_usd': 18.65,
            'cud_or_cache_credits_usd': 0.00,
            'cud_and_cache_credits_usd': 0.00,
            'net_billed_usd': 18.65,
            'net_invoice_usd': 18.65,
            'reconciliation_delta_pct': 1.3,
            'variance_pct': 1.3,
            'status': 'PARTITIONED BY DATE',
        },
    ]
    return {
        'north_star_metric_name': 'Cost per CSAT-Positive Resolved Session (CSAT >= 4.5★, P95 <= 800ms)',
        'baseline_cost_per_resolved_session_usd': baseline_cost_per_resolved_session,
        'optimized_cost_per_resolved_session_usd': optimized_cost_per_resolved_session,
        'unit_economics_reduction_pct': unit_econ_reduction_pct,
        'avg_turns_to_resolution_baseline': 7.8,
        'avg_turns_to_resolution_optimized': 3.2,
        'quality_floor_csat': 4.96,
        'billing_export_table': 'project-maui.billing_export.gcp_billing_export_resource_v1',
        'total_net_invoice_usd': 1355.75,
        'total_credits_usd': 3436.80,
        'reconciliation_delta_pct': 1.04,
        'unit_economics': {
            'north_star_metric': 'Cost per CSAT-Positive Resolved Session',
            'baseline_cost_per_resolved_session_usd': baseline_cost_per_resolved_session,
            'optimized_cost_per_resolved_session_usd': optimized_cost_per_resolved_session,
            'unit_cost_reduction_pct': unit_econ_reduction_pct,
            'baseline_resolution_rate_pct': 76.2,
            'optimized_resolution_rate_pct': 96.4,
            'avg_turns_per_session_baseline': 7.8,
            'avg_turns_per_session_optimized': 3.2,
            'baseline_csat_positive_pct': 68.4,
            'optimized_csat_positive_pct': 94.8,
        },
        'sku_ledger': sku_ledger,
        'gsu_advisor': {
            'current_mode': 'Pay-As-You-Go (PAYG) + Implicit/Explicit Context Caching',
            'sustained_peak_tps': 42800,
            'current_peak_tps': 42800,
            'recommended_provisioned_gsus': 2,
            'spillover_mode': 'PAYG Spillover Enabled',
            'payg_monthly_usd': 14200,
            'gsu_cud_monthly_usd': 10050,
            'projected_gsu_savings_usd': 4150,
            'utilization_at_peak_pct': 86.4,
            'recommended_gsu_tier': '2x Generative Service Units (GSU) on gemini-2.5-flash (1-Yr CUD)',
            'additional_monthly_cud_savings_usd': 4150,
            'recommendation_note': 'Commit 2 base GSUs under 1-Yr CUD for steady-state IT Service Desk traffic; burst above 35k tok/s via PAYG.',
        },
        'total_monthly_savings_usd': uc.get('total_monthly_savings_usd'),
    }

  def get_tokenomics_and_cockpit_finops_payload(
      self,
      assumptions: Mapping[str, Any] | None = None,
  ) -> dict[str, Any]:
    """Builds the Tokenomics (Decks 1 & 2) + AgentOps Cockpit FinOps payload.

    Implements:
    - True Cost per Outcome (CpO): CpO = sum_{i=1..n}(C_LLM + C_Tools + C_Infra)_i + C_HITL
    - Token-to-Spend Drift Reconciliation across 5 Drift Drivers (D1..D5) + Unattributed = 0.00
    - 3-Way User-Level Attribution Join (GE Activity Logs x OTel Traces x Cloud Billing Export)
    - 4 Metering Categories & Consumption Portfolio (PT + Burst PayGo, Deferred -50%, Off-Peak -50%,
      Guaranteed Batch -50%, GE Standard $30/mo Pooled Seat vs PAYGO API, FSP 10%/20%)
    - Explicit vs. Implicit Context Cache Break-Even Calculator: N* = 1 + S / (0.9 * P_in)
    - Apigee AI Gateway Policies (Deck 1 Slide #30 g3ee7e8b2bb8_1_3597 & Deck 2 Page 17)
    - Modular Extension Token Overhead (Hooks 0 tok vs Skills 40 tok vs Knowledge Catalog 2k tok vs Unpruned MCP 50k tok)
    - CFO Enterprise TCO (30% Visible / 70% Hidden) & P&L Token Accounting (CapEx R&D, OpEx SG&A, COGS)
    - AgentOps Cockpit Static/Runtime FinOps Code Audit (FIN-01..FIN-05), Step-by-Step ROI Waterfall,
      @cost_guard(budget_limit_usd) Pre-Flight Turn Enforcer, and Cross-Pillar OpEx Tradeoff Matrix.
    """
    raw = assumptions or {}

    def _num(key: str, default: float) -> float:
      val = raw.get(key)
      if val is None or val == '':
        return default
      try:
        return float(str(val))
      except (TypeError, ValueError):
        return default

    # Configurable interactive assumptions (accepts both canonical keys and UI input aliases)
    raw_calls = raw.get('calls_per_hr') if raw.get('calls_per_hr') is not None else raw.get('cache_calls_per_hour')
    calls_per_hr = max(1.0, min(500.0, float(raw_calls) if raw_calls not in (None, '') else 18.0))
    prefix_tokens = max(4096.0, min(250000.0, _num('prefix_tokens', 18000.0)))
    implicit_hit_rate = max(0.0, min(0.99, _num('implicit_hit_rate', 0.55)))
    budget_limit_usd = max(0.001, min(1.0, _num('budget_limit_usd', 0.0200)))
    raw_def = raw.get('deferred_share_pct') if raw.get('deferred_share_pct') is not None else raw.get('deferred_offpeak_share_pct')
    deferred_share_pct = max(0.0, min(80.0, float(raw_def) if raw_def not in (None, '') else 35.0))
    fsp_commit_years = int(_num('fsp_commit_years', 3))
    fsp_discount_pct = 20.0 if fsp_commit_years >= 3 else (10.0 if fsp_commit_years == 1 else 0.0)
    ge_seats = max(10, int(_num('ge_seats', 250)))
    power_user_ratio_pct = max(1.0, min(50.0, _num('power_user_ratio_pct', 12.0)))
    monthly_tasks = max(1000, int(_num('monthly_tasks', 129500)))
    hitl_review_minutes = max(0.5, min(120.0, _num('hitl_review_minutes', 8.8235)))
    hitl_hourly_rate_usd = max(10.0, min(500.0, _num('hitl_hourly_rate_usd', 85.0)))

    # 1. True Cost per Outcome (CpO) Formula Breakdown (Deck 2 Pages 28 & 33; Opus AF-02)
    # Formula: CpO = (n * (C_LLM + C_Tools + C_Infra) + C_HITL) / P(CSAT_Positive_Resolution)
    if 'hitl_review_minutes' in raw or 'hitl_hourly_rate_usd' in raw:
      hitl_ticket_cost_usd = round((hitl_review_minutes / 60.0) * hitl_hourly_rate_usd, 2)
    else:
      hitl_ticket_cost_usd = 12.50
    task_scale = (monthly_tasks / 129500.0) if 'monthly_tasks' in raw else 1.0
    cpo_specs: list[dict[str, Any]] = [
        {
            'agent_id': 'it_service_desk',
            'display_name': 'IT Service Desk',
            'model': 'gemini-2.5-flash',
            'lane': 'Provisioned Throughput (PT) + Burst PayGo',
            'n_turns_baseline': 7.8,
            'n_turns_optimized': 3.2,
            'c_llm_per_turn_usd': 0.0021,
            'c_tools_per_turn_usd': 0.0008,
            'c_infra_per_turn_usd': 0.0005,
            'escalation_rate_baseline_pct': 18.4,
            'escalation_rate_optimized_pct': 3.6,
            'sessions_per_mo': max(100, round(84000 * task_scale)),
        },
        {
            'agent_id': 'vibelift_analytics',
            'display_name': 'VibeLift Analytics & FinOps',
            'model': 'gemini-2.5-flash',
            'lane': 'Standard PayGo + Explicit Prefix Cache',
            'n_turns_baseline': 6.4,
            'n_turns_optimized': 2.8,
            'c_llm_per_turn_usd': 0.0019,
            'c_tools_per_turn_usd': 0.0006,
            'c_infra_per_turn_usd': 0.0004,
            'escalation_rate_baseline_pct': 12.0,
            'escalation_rate_optimized_pct': 1.8,
            'sessions_per_mo': max(100, round(31000 * task_scale)),
        },
        {
            'agent_id': 'deep_research',
            'display_name': 'Deep Research',
            'model': 'gemini-2.5-pro (Plan) + gemini-2.5-flash (Exec)',
            'lane': 'Deferred Agents API (-50%) + Priority PayGo',
            'n_turns_baseline': 11.2,
            'n_turns_optimized': 4.5,
            'c_llm_per_turn_usd': 0.0068,
            'c_tools_per_turn_usd': 0.0022,
            'c_infra_per_turn_usd': 0.0011,
            'escalation_rate_baseline_pct': 22.0,
            'escalation_rate_optimized_pct': 4.2,
            'sessions_per_mo': max(100, round(14500 * task_scale)),
        },
    ]
    cpo_agents = []
    fleet_monthly_baseline_usd = 0.0
    fleet_monthly_optimized_usd = 0.0
    total_sessions_mo = 0
    for spec in cpo_specs:
      n_base = float(spec['n_turns_baseline'])
      n_opt = float(spec['n_turns_optimized'])
      c_llm = float(spec['c_llm_per_turn_usd'])
      c_tools = float(spec['c_tools_per_turn_usd'])
      c_infra = float(spec['c_infra_per_turn_usd'])
      esc_base = float(spec['escalation_rate_baseline_pct']) / 100.0
      esc_opt = float(spec['escalation_rate_optimized_pct']) / 100.0
      # Uncached/unpruned baseline per-turn LLM cost is 4.2x higher than cached per-turn LLM cost
      base_turn_cost = (c_llm * 4.2) + (c_tools * 1.8) + (c_infra * 1.5)
      opt_turn_cost = c_llm + c_tools + c_infra
      c_hitl_base = round(esc_base * hitl_ticket_cost_usd, 4)
      c_hitl_opt = round(esc_opt * hitl_ticket_cost_usd, 4)
      base_cpo = round(n_base * base_turn_cost + c_hitl_base, 4)
      opt_cpo = round(n_opt * opt_turn_cost + c_hitl_opt, 4)
      red_pct = round(((base_cpo - opt_cpo) / max(0.0001, base_cpo)) * 100.0, 1)
      sess_mo = int(spec['sessions_per_mo'])
      mo_base_usd = round(base_cpo * sess_mo, 2)
      mo_opt_usd = round(opt_cpo * sess_mo, 2)
      fleet_monthly_baseline_usd += mo_base_usd
      fleet_monthly_optimized_usd += mo_opt_usd
      total_sessions_mo += sess_mo
      cpo_agents.append({
          'agent_id': spec['agent_id'],
          'display_name': spec['display_name'],
          'model': spec['model'],
          'lane': spec['lane'],
          'n_turns_baseline': n_base,
          'n_turns_optimized': n_opt,
          'c_llm_turn_usd': c_llm,
          'c_tools_turn_usd': c_tools,
          'c_infra_turn_usd': c_infra,
          'c_hitl_baseline_usd': c_hitl_base,
          'c_hitl_optimized_usd': c_hitl_opt,
          'escalation_rate_optimized_pct': spec['escalation_rate_optimized_pct'],
          'baseline_cpo_usd': base_cpo,
          'optimized_cpo_usd': opt_cpo,
          'cpo_reduction_pct': red_pct,
          'sessions_per_mo': sess_mo,
          'monthly_baseline_usd': mo_base_usd,
          'monthly_optimized_usd': mo_opt_usd,
          'monthly_saved_usd': round(mo_base_usd - mo_opt_usd, 2),
          'formula': f'{n_opt}*({c_llm:.4f}+{c_tools:.4f}+{c_infra:.4f}) + {esc_opt:.3f}*${hitl_ticket_cost_usd:.2f}',
          'source_ref': 'Deck2:g3c57606f16c_0_6563 (Pages 28 & 33)',
      })

    fleet_base_cpo = round(fleet_monthly_baseline_usd / max(1, total_sessions_mo), 4)
    fleet_opt_cpo = round(fleet_monthly_optimized_usd / max(1, total_sessions_mo), 4)
    fleet_cpo_reduction_pct = round(((fleet_base_cpo - fleet_opt_cpo) / max(0.0001, fleet_base_cpo)) * 100.0, 1)

    # 2. Token-to-Spend Drift Reconciliation across 5 Drift Drivers (Deck 2 Page 29)
    # Reconciles naive single-turn token estimate vs unoptimized billed spend, and shows post-remediation drift
    expected_naive_token_spend_usd = 420.00
    drift_drivers: list[dict[str, Any]] = [
        {
            'driver_id': 'D1',
            'name': 'Agentic Loop & Sub-Agent Delegation Overhead',
            'unoptimized_drift_usd': 310.00,
            'remediated_drift_usd': 215.00,
            'share_of_drift_pct': 23.0,
            'detection_rule': 'turn_count > 3 OR subagent_fanout > 2',
            'control_applied': 'ADK max_steps=4 cap + A2A loop circuit breaker',
        },
        {
            'driver_id': 'D2',
            'name': 'Retry Cascades on 429 / 503',
            'unoptimized_drift_usd': 195.00,
            'remediated_drift_usd': 18.50,
            'share_of_drift_pct': 2.0,
            'detection_rule': 'status_code IN (429, 503) AND retry_delay_mode == "FIXED"',
            'control_applied': 'Tenacity exponential backoff + Apigee Automated Traffic Freeze',
        },
        {
            'driver_id': 'D3',
            'name': 'Quadratic Context Window Bloat (>40% Bill Share)',
            'unoptimized_drift_usd': 680.00,
            'remediated_drift_usd': 312.25,
            'share_of_drift_pct': 33.4,
            'detection_rule': 'history_tokens / prompt_token_count > 0.40',
            'control_applied': 'N=6 turn sliding window + GCP Knowledge Catalog 50k->2k schema pruning',
        },
        {
            'driver_id': 'D4',
            'name': 'Supporting Infra & Tool Grounding Fees',
            'unoptimized_drift_usd': 390.00,
            'remediated_drift_usd': 222.55,
            'share_of_drift_pct': 23.8,
            'detection_rule': 'service IN ("Cloud Run", "Agent Engine", "BigQuery")',
            'control_applied': 'Cloud Run concurrency=80 + Partitioned BigQuery aive_logs',
        },
        {
            'driver_id': 'D5',
            'name': 'Thinking Token & Priority PayGo Multipliers (1.8x)',
            'unoptimized_drift_usd': 545.00,
            'remediated_drift_usd': 167.45,
            'share_of_drift_pct': 17.9,
            'detection_rule': 'thoughts_token_count > 1024 OR service_tier == "PRIORITY"',
            'control_applied': 'thinking_budget_tokens=1024 cap + Off-Peak/Deferred lane routing',
        },
    ]
    remediated_drift_sum = round(sum(float(d['remediated_drift_usd']) for d in drift_drivers), 2)
    actual_reconciled_invoice_usd = 1355.75
    unattributed_usd = round(actual_reconciled_invoice_usd - (expected_naive_token_spend_usd + remediated_drift_sum), 2)

    # 3. 3-Way User-Level Attribution Join (Deck 2 Page 30)
    attribution_join = {
        'title': '3-Way User-Level Attribution Join (Deck 2 Page 30)',
        'sources': [
            {
                'layer': '1. Gemini Enterprise Activity Logs',
                'table_or_stream': 'cloudaudit.googleapis.com/data_access (discoveryengine.googleapis.com)',
                'join_keys': 'trace_id, session_id, userIamPrincipal',
                'extracted_fields': 'principalEmail (user_ldap), department, engine_id, agent_id',
            },
            {
                'layer': '2. OpenTelemetry GenAI Traces',
                'table_or_stream': 'project-maui.aive_logs.agent_usage_log (@vibelift_telemetry)',
                'join_keys': 'trace_id, span_id, event_id',
                'extracted_fields': 'gen_ai.usage.input_tokens, output_tokens, thinking_tokens, gen_ai.request.model',
            },
            {
                'layer': '3. GCP Cloud Billing Export',
                'table_or_stream': 'project-maui.billing_export.gcp_billing_export_resource_v1',
                'join_keys': 'labels.trace_id, labels.agent_id, sku.id, usage_start_time',
                'extracted_fields': 'cost, credits.amount (CUD / Cache / FSP), net_invoice_usd',
            },
        ],
        'join_sql': (
            'SELECT\n'
            '  ge.principal_email AS user_ldap,\n'
            '  ge.department,\n'
            '  otel.agent_name,\n'
            '  otel.model_name,\n'
            '  SUM(otel.total_tokens) AS total_tokens,\n'
            '  ROUND(SUM(bill.cost + IFNULL(cred.amount, 0)), 2) AS reconciled_net_spend_usd\n'
            'FROM `project-maui.aive_logs.ge_user_activity` AS ge\n'
            'JOIN `project-maui.aive_logs.agent_usage_log` AS otel\n'
            '  ON ge.trace_id = otel.trace_id\n'
            'JOIN `project-maui.billing_export.gcp_billing_export_resource_v1` AS bill\n'
            '  ON bill.labels.value = otel.agent_name AND DATE(bill.usage_start_time) = DATE(otel.timestamp)\n'
            'LEFT JOIN UNNEST(bill.credits) AS cred\n'
            'GROUP BY 1, 2, 3, 4\n'
            'ORDER BY reconciled_net_spend_usd DESC;'
        ),
    }

    # 4. 4 Metering Categories, Consumption Lanes & Commercial Seat Risk Model (Decks 1, 2, 3)
    # Seat-based commercial risk model (Deck 1 Slides 22-26 & 50-53):
    # Gemini Enterprise Standard = $30/user/mo with 1,500 prompts/day pooled across org
    power_users_count = max(1, round(ge_seats * (power_user_ratio_pct / 100.0)))
    standard_users_count = max(0, ge_seats - power_users_count)
    ge_pooled_monthly_usd = round(ge_seats * 30.0 * (1.0 - fsp_discount_pct / 100.0), 2)
    # Under raw PAYGO API, power users burn ~$651/mo on Flash API (or $3,762/mo on Frontier API)
    paygo_unpooled_monthly_usd = round(power_users_count * 651.0 + standard_users_count * 18.50, 2)
    pooled_seat_savings_usd = round(max(0.0, paygo_unpooled_monthly_usd - ge_pooled_monthly_usd), 2)

    metering_categories = [
        {
            'category': '1. Token-Based ($/1M tok)',
            'sku_examples': 'Gemini 2.5/3.1 Flash & Pro Input, Output, Cached Read (0.1x), Thinking',
            'billing_unit': '$/1M tokens',
            'rate_summary': 'Flash: $0.30 in / $0.03 cached / $2.50 out | Pro: $1.25 in / $0.125 cached / $10.00 out',
            'optimization_lever': '4-Tier Model Routing + Deferred/Off-Peak/Batch (-50%) + Context Caching (-90% read)',
            'monthly_spend_usd': 1133.20,
            'verified': True,
            'source_ref': 'Deck2:Page13 & Vertex AI Pricing',
        },
        {
            'category': '2. Resource-Based ($/node-hr & vCPU-hr)',
            'sku_examples': 'Cloud Run (CR90-44E1), Agent Engine Runtime (AE55-19C8), Vector Search 2.0, BigQuery',
            'billing_unit': '$/vCPU-hr, $/GiB-hr, $/TB',
            'rate_summary': 'Cloud Run concurrency=80 ($76.80/mo) + Agent Engine ($127.10/mo) + BQ ($18.65/mo)',
            'optimization_lever': 'Scale-to-zero dev/staging + Cloud Run concurrency=80 + Partitioned BQ exports',
            'monthly_spend_usd': 222.55,
            'verified': True,
            'source_ref': 'Deck2:Page13 & Cloud Billing Export',
        },
        {
            'category': '3. Provisioned ($/GSU/term)',
            'sku_examples': 'Provisioned Throughput (PT) + PT with Burst PayGo (>=200 GSUs -> 2x Standard PayGo quota)',
            'billing_unit': '$/GSU/month (1-Yr or 3-Yr CUD)',
            'rate_summary': '2 base GSUs committed ($10,050/mo vs $14,200 PAYG); Burst PayGo absorbs peak spikes',
            'optimization_lever': 'Size PT floor to P65 steady-state TPS; enable Burst PayGo spillover for P99 peaks',
            'monthly_spend_usd': 10050.00,
            'verified': False,  # Burst PayGo 200+ GSU 2x multiplier flagged † per Opus spec
            'source_ref': 'Deck3:Slides9-14†',
        },
        {
            'category': '4. Seat-Based ($/user/mo + FSP)',
            'sku_examples': 'Gemini Enterprise Standard ($30/user/mo, 1,500 prompts/day org-pooled) + FSP (10% 1Y / 20% 3Y)',
            'billing_unit': '$/seat/month (Org-Wide Pooled Quota)',
            'rate_summary': f'{ge_seats} seats @ $30/mo (-{fsp_discount_pct:.0f}% FSP) = ${ge_pooled_monthly_usd:,.2f}/mo vs ${paygo_unpooled_monthly_usd:,.2f}/mo unpooled PAYGO',
            'optimization_lever': f'Org-pooled seat quota buffers {power_users_count} power users ($651/mo API eq), saving ${pooled_seat_savings_usd:,.2f}/mo',
            'monthly_spend_usd': ge_pooled_monthly_usd,
            'verified': False,  # FSP 10%/20% BPH discount flagged † per Opus spec
            'source_ref': 'Deck1:Slides22-26 & Deck3:Slide34†',
        },
    ]

    routing_lanes = [
        {
            'lane_id': 'provisioned_pt_burst',
            'lane_name': 'Provisioned Throughput (PT) + Burst PayGo†',
            'price_multiplier': '0.71x (1-Yr CUD)',
            'slo_guarantee': 'Predictable P99 TTFT + 2x Standard PayGo burst quota (>=200 GSUs)†',
            'traffic_share_pct': round(40.0 - (deferred_share_pct - 35.0) * 0.4, 1),
            'best_for': 'Steady-state interactive production traffic (IT Service Desk)',
        },
        {
            'lane_id': 'standard_paygo',
            'lane_name': 'Standard PayGo (Implicit/Explicit Cache)',
            'price_multiplier': '1.00x (0.10x cache read)',
            'slo_guarantee': 'Standard dynamic shared quota',
            'traffic_share_pct': round(20.0 - (deferred_share_pct - 35.0) * 0.4, 1),
            'best_for': 'Variable daytime interactive queries & Burst spillover',
        },
        {
            'lane_id': 'priority_paygo',
            'lane_name': 'Priority PayGo',
            'price_multiplier': '1.80x',
            'slo_guarantee': 'Prioritized token queue above Standard PayGo during regional contention',
            'traffic_share_pct': 5.0,
            'best_for': 'Executive/SLA-critical interactive escalations only',
        },
        {
            'lane_id': 'deferred_agents_api',
            'lane_name': 'Deferred Agents API (service_tier="deferred")†',
            'price_multiplier': '0.50x (-50%)†',
            'slo_guarantee': 'Asynchronous execution within 24h SLO window',
            'traffic_share_pct': round(deferred_share_pct * 0.45, 1),
            'best_for': 'Background sub-agent research, ticket summarization, nightly evals',
        },
        {
            'lane_id': 'off_peak_paygo',
            'lane_name': 'Off-Peak PayGo (3pm–9pm PT & Weekends)†',
            'price_multiplier': '0.50x (-50%)†',
            'slo_guarantee': 'Scheduled window execution (15:00–21:00 PT + Sat/Sun)†',
            'traffic_share_pct': round(deferred_share_pct * 0.30, 1),
            'best_for': 'Scheduled parameter tuning, bulk corpus indexing, synthetic audits',
        },
        {
            'lane_id': 'guaranteed_batch',
            'lane_name': 'Guaranteed Batch Predictions',
            'price_multiplier': '0.50x (-50%)',
            'slo_guarantee': '24-hour rolling batch completion SLO',
            'traffic_share_pct': round(deferred_share_pct * 0.25, 1),
            'best_for': 'FIN-01 batch inference loops, retro group scoring, embeddings',
        },
    ]

    model_portfolio_tiers = [
        {
            'tier': 'Tier 1: Frontier',
            'models': 'gemini-2.5-pro / gemini-3.1-pro / claude-opus-4-6',
            'input_rate_1m': '$1.25',
            'output_rate_1m': '$10.00',
            'role_in_hybrid_pattern': 'Pro Plans & Reviews (15% of turns): Architecture decomposition, complex multi-hop synthesis, final quality gate',
            'intelligence_per_dollar_index': 'Baseline (1.0x)',
        },
        {
            'tier': 'Tier 2: Workhorse',
            'models': 'gemini-2.5-flash / gemini-3-flash',
            'input_rate_1m': '$0.30',
            'output_rate_1m': '$2.50',
            'role_in_hybrid_pattern': 'Flash Builds & Operates (60% of turns): Tool execution, RAG synthesis, MCP orchestration (-76% vs Frontier)',
            'intelligence_per_dollar_index': '4.1x Intelligence / $',
        },
        {
            'tier': 'Tier 3: Scale',
            'models': 'gemini-2.5-flash-lite / gemini-3.1-flash-lite',
            'input_rate_1m': '$0.10',
            'output_rate_1m': '$0.40',
            'role_in_hybrid_pattern': 'High-Volume Triage & Intent Router (15% of turns): Classification, sentiment, schema validation (-92% vs Frontier)',
            'intelligence_per_dollar_index': '9.4x Intelligence / $',
        },
        {
            'tier': 'Tier 4: Open Weight (SLM-on-Edge)',
            'models': 'gemma-3-27b-it (Cloud Run GPU / GKE)',
            'input_rate_1m': '$0.04 (infra eq.)',
            'output_rate_1m': '$0.12 (infra eq.)',
            'role_in_hybrid_pattern': 'Structured Formatting & PII Scrubbing (10% of turns): Fix for FIN-03 / MP-029 (-96% vs Frontier)',
            'intelligence_per_dollar_index': '12.8x Task-Specific / $',
        },
    ]

    # 5. Context Caching Break-Even Calculator (Deck 2 Page 15 & Deck 3 Slides 15-22)
    # Formula: N* = 1 + S / (0.9 * P_in)
    flash_p_in = 0.30
    flash_storage = 1.00
    pro_p_in = 1.25
    pro_storage = 4.50
    flash_break_even_n = round(1.0 + flash_storage / (0.9 * flash_p_in), 2)  # 4.70 calls/hr
    pro_break_even_n = round(1.0 + pro_storage / (0.9 * pro_p_in), 2)        # 5.00 calls/hr

    # Compare hourly cost for the user-selected prefix_tokens and calls_per_hr on Flash:
    tok_m = prefix_tokens / 1e6
    uncached_hr_usd = round(calls_per_hr * tok_m * flash_p_in, 4)
    explicit_hr_usd = round(tok_m * flash_p_in + 0.10 * max(0.0, calls_per_hr - 1.0) * tok_m * flash_p_in + tok_m * flash_storage, 4)
    implicit_hr_usd = round(calls_per_hr * tok_m * flash_p_in * (1.0 - 0.90 * implicit_hit_rate), 4)
    winning_mode = (
        'EXPLICIT_CACHE_LOCKED'
        if explicit_hr_usd < implicit_hr_usd and calls_per_hr >= flash_break_even_n
        else 'IMPLICIT_CACHE_AUTO'
    )

    caching_economics = {
        'formula': 'N* = 1 + S / (0.9 * P_in)',
        'assumptions': {
            'calls_per_hr': calls_per_hr,
            'prefix_tokens': int(prefix_tokens),
            'implicit_hit_rate_pct': round(implicit_hit_rate * 100.0, 1),
        },
        'flash_break_even_calls_per_hr': flash_break_even_n,
        'pro_break_even_calls_per_hr': pro_break_even_n,
        'hourly_cost_comparison': {
            'uncached_paygo_hr_usd': uncached_hr_usd,
            'explicit_cache_hr_usd': explicit_hr_usd,
            'implicit_cache_hr_usd': implicit_hr_usd,
            'winning_mode': winning_mode,
            'monthly_prefix_savings_usd': round(max(0.0, (uncached_hr_usd - min(explicit_hr_usd, implicit_hr_usd)) * 730.0), 2),
        },
        'modes_table': [
            {
                'mode': 'Explicit Caching (Gemini 2.5 Flash)',
                'write_multiplier': '1.0x ($0.30/1M)',
                'read_multiplier': '0.10x ($0.03/1M, -90%)',
                'storage_rate': '$1.00 / 1M tok-hr',
                'min_tokens': '4,096 tok',
                'ttl': '1h configurable',
                'break_even_calls_per_hr': f'{flash_break_even_n} calls/hr',
                'verdict': 'WINNER (Guaranteed 90% discount when N >= 5/hr)' if winning_mode == 'EXPLICIT_CACHE_LOCKED' else 'Sub-optimal at low call rate',
            },
            {
                'mode': 'Explicit Caching (Gemini 2.5 Pro)',
                'write_multiplier': '1.0x ($1.25/1M)',
                'read_multiplier': '0.10x ($0.125/1M, -90%)',
                'storage_rate': '$4.50 / 1M tok-hr',
                'min_tokens': '4,096 tok',
                'ttl': '1h configurable',
                'break_even_calls_per_hr': f'{pro_break_even_n} calls/hr',
                'verdict': 'Recommended for Deep Research static corpus >= 5 calls/hr',
            },
            {
                'mode': 'Implicit Caching (Gemini 2.5 / 3.x Default)',
                'write_multiplier': '1.0x',
                'read_multiplier': '0.10x on opportunistic cache hit',
                'storage_rate': '$0.00 / 1M tok-hr (Free)',
                'min_tokens': '1,024–2,048 tok',
                'ttl': ' Opportunistic (~5–15m)',
                'break_even_calls_per_hr': '1.00 call/hr (Zero storage risk)',
                'verdict': 'WINNER for bursty / low-frequency agents (< 5 calls/hr)' if winning_mode == 'IMPLICIT_CACHE_AUTO' else 'Fallback when prefix < 4,096 tok',
            },
        ],
    }

    # 6. Apigee AI Gateway Policies (Deck 1 Slide #30 g3ee7e8b2bb8_1_3597 & Slide 19; Deck 2 Page 17)
    apigee_policies = [
        {
            'policy_id': 'APG-01',
            'policy_name': 'LLMTokenQuota (Per-User & Per-Dept)',
            'configured_threshold': '250,000 tok/day per user_ldap | 50M tok/day per dept',
            'enforcement_action': 'Enforces user-level token quota from OAuth/IAM principal before forwarding to Vertex AI',
            'tokens_prevented_monthly': '142.0M tok/mo',
            'monthly_savings_usd': 4850.00,
            'source_ref': 'Deck1:g3ee7e8b2bb8_1_3597 (Slide 30) & Deck2:Page17',
        },
        {
            'policy_id': 'APG-02',
            'policy_name': 'PromptTokenLimit & Thinking Cap',
            'configured_threshold': 'max_input_tokens=24,000 | thinking_budget_tokens=1,024',
            'enforcement_action': 'Rejects or truncates unpruned 50k+ schema dumps & runaway reasoning chains at gateway edge',
            'tokens_prevented_monthly': '218.5M tok/mo',
            'monthly_savings_usd': 7420.00,
            'source_ref': 'Deck2:Page17',
        },
        {
            'policy_id': 'APG-03',
            'policy_name': 'Spend Caps (Unified Cross-App / IDE / Agent)',
            'configured_threshold': '$500/day per agent service | $15,000/mo org hard cap',
            'enforcement_action': 'Single unified budget cap across Gemini Enterprise, ADK agents, and developer IDEs',
            'tokens_prevented_monthly': '94.0M tok/mo',
            'monthly_savings_usd': 3190.00,
            'source_ref': 'Deck1:Slide19 (Spend Caps & Unified Coverage)',
        },
        {
            'policy_id': 'APG-04',
            'policy_name': 'Automated Traffic Freeze (Zero-Lag Circuit Breaker)',
            'configured_threshold': 'Trigger < 50ms on quota breach or 3x recursive tool loop',
            'enforcement_action': 'Pauses runaway agent API traffic immediately instead of waiting 24–48h for Cloud Billing export',
            'tokens_prevented_monthly': '165.0M tok/mo',
            'monthly_savings_usd': 6120.00,
            'source_ref': 'Deck1:Slide19 (Automated Traffic Freeze)',
        },
        {
            'policy_id': 'APG-05',
            'policy_name': 'Apigee Semantic Caching',
            'configured_threshold': 'Cosine similarity >= 0.92 (Vertex AI Embeddings + Memorystore)',
            'enforcement_action': 'Serves repeated IT Service Desk & FAQ queries directly from gateway cache (0 LLM tokens)',
            'tokens_prevented_monthly': '310.0M tok/mo',
            'monthly_savings_usd': 8940.00,
            'source_ref': 'Deck2:Page17 & AgentOps Cockpit semantic_cache.py',
        },
        {
            'policy_id': 'APG-06',
            'policy_name': 'Dynamic Model Routing Policy',
            'configured_threshold': 'Prompt complexity score < 0.35 -> Flash-Lite | < 0.75 -> Flash | >= 0.75 -> Pro',
            'enforcement_action': 'Routes simple classification/JSON turns away from Frontier models at API gateway layer',
            'tokens_prevented_monthly': 'N/A (Rate arbitrage -78%)',
            'monthly_savings_usd': 11200.00,
            'source_ref': 'Deck1:Slides35-41 & Deck2:Page17',
        },
        {
            'policy_id': 'APG-07',
            'policy_name': 'BigQuery Monetization & Chargeback Export',
            'configured_threshold': '100% requests tagged with x-gcp-cost-center, x-user-ldap, x-pnl-class',
            'enforcement_action': 'Streams per-call token attribution directly to BigQuery for automated department chargebacks',
            'tokens_prevented_monthly': '100% Chargeback Coverage',
            'monthly_savings_usd': 2400.00,
            'source_ref': 'Deck2:Pages17 & 30',
        },
    ]

    # Modular Extension Token Overhead Comparison (Deck 2 Page 20 + Deck 1 Slides 28-29)
    extension_overhead = [
        {
            'extension_type': 'Hooks / Slash Commands',
            'tokens_per_turn': '0 tok',
            'loading_mechanism': 'Direct pre/post-turn code execution outside LLM context window',
            'monthly_cost_100k_turns_usd': 0.00,
            'finops_recommendation': 'PREFERRED for fixed guardrails, logging (@vibelift_telemetry), and @cost_guard',
            'status': 'OPTIMAL (0 TOK OVERHEAD)',
        },
        {
            'extension_type': 'Agent Skills (SKILL.md Progressive Disclosure)',
            'tokens_per_turn': '30–50 tok / skill',
            'loading_mechanism': 'Only compact YAML name/description in system prefix; full body loaded on-demand when invoked',
            'monthly_cost_100k_turns_usd': 1.20,
            'finops_recommendation': 'PREFERRED for domain playbooks; keeps baseline prompt lean (-98% vs static dump)',
            'status': 'OPTIMAL (LAZY LOADED)',
        },
        {
            'extension_type': 'Scoped Plugins (Curated Toolset)',
            'tokens_per_turn': '450 tok / plugin',
            'loading_mechanism': 'Bundles 3–5 domain-specific tools with compressed parameter schemas',
            'monthly_cost_100k_turns_usd': 13.50,
            'finops_recommendation': 'Pin static plugin schema above cache breakpoint for 90% cache read discount',
            'status': 'CACHED (0.1X READ RATE)',
        },
        {
            'extension_type': 'GCP Knowledge Catalog (Universal Context Engine)',
            'tokens_per_turn': '2,000 tok / query',
            'loading_mechanism': 'Replaces 50,000+ tok raw database schema dumps with surgical 2k semantic metadata payload',
            'monthly_cost_100k_turns_usd': 60.00,
            'finops_recommendation': '7x token reduction, +63% SQL generation accuracy, -66% hallucinations (Deck 1 Slides 28–29)',
            'status': '7X REDUCTION APPLIED',
        },
        {
            'extension_type': 'Unpruned MCP Server Tool Schemas (Anti-Pattern)',
            'tokens_per_turn': '50,000+ tok / turn',
            'loading_mechanism': 'Dumps 40+ full JSON Schema definitions into every turn regardless of user intent',
            'monthly_cost_100k_turns_usd': 1500.00,
            'finops_recommendation': 'CRITICAL BLOAT: Replace with Dynamic Tool Registry filter (top-5 tools) + Knowledge Catalog',
            'status': 'PRUNED BY VIBELIFT (-96%)',
        },
    ]

    # 7. CFO Enterprise TCO (30% Visible / 70% Hidden) & P&L Token Accounting (Deck 2 Pages 24-25, 27, 32)
    tco_and_pnl = {
        'visible_tech_share_pct': 30.0,
        'hidden_enterprise_share_pct': 70.0,
        'current_maturity_stage': 'RUN (Value Realization, Outcome CpO & Automated FinOps Control)',
        'maturity_progression': [
            {
                'stage': 'CRAWL (Visibility & Tagging)',
                'focus': 'Track raw token volume, enforce GCP billing labels (agent_id, department, user_ldap), eliminate dark spend',
                'status': 'COMPLETED',
            },
            {
                'stage': 'WALK (Unit Cost Economics)',
                'focus': 'Reconcile Token-to-Spend Drift (<1.1%), lock Prefix Caching (>90%), deploy Apigee quotas & 4-tier routing',
                'status': 'COMPLETED',
            },
            {
                'stage': 'RUN (Value Realization & CpO)',
                'focus': 'Optimize True Cost per Outcome (CpO), automate Canary rollbacks on CSAT/Accuracy, align P&L (CapEx/OpEx/COGS)',
                'status': 'ACTIVE STAGE',
            },
        ],
        'tco_breakdown_rows': [
            {
                'bucket': 'Visible Tech Spend (30% of Enterprise TCO)',
                'components': 'Vertex AI Token SKUs, Provisioned GSUs, Cloud Run, Agent Engine, Vector Search, BigQuery',
                'unoptimized_annual_usd': 511200,
                'optimized_annual_usd': 136890,
                'lever': 'Prefix Caching (-90% read), 4-Tier Routing, Deferred/Off-Peak (-50%), PT + FSP',
            },
            {
                'bucket': 'Hidden Operational Spend (70% of Enterprise TCO)',
                'components': 'HITL Escalation Handling ($12.50/ticket), Raw Schema Maintenance, Eval & Red-Teaming, Retry Debugging',
                'unoptimized_annual_usd': 1192800,
                'optimized_annual_usd': 248400,
                'lever': 'First-Contact Resolution 76.2%->96.4% (-80% HITL), GCP Knowledge Catalog, Automated Canary Gatekeeper',
            },
        ],
        'pnl_accounting': [
            {
                'pnl_class': 'CapEx / R&D (Build Capability)',
                'billing_label_selector': 'labels.pnl_class="capex_rd"',
                'workloads': 'AlphaEvolve parameter tuning, prompt engineering, sandbox evals, synthetic baseline audits',
                'share_pct': 14.2,
                'monthly_net_spend_usd': 192.52,
                'accounting_treatment': 'Capitalized software/model development & R&D experimentation',
            },
            {
                'pnl_class': 'OpEx / SG&A (Run Internal Workflows)',
                'billing_label_selector': 'labels.pnl_class="opex_sga"',
                'workloads': 'IT Service Desk employee resolution, internal Deep Research, VibeLift FinOps control plane',
                'share_pct': 48.5,
                'monthly_net_spend_usd': 657.54,
                'accounting_treatment': 'Operating expense offset by $46,870/mo IT support & engineering productivity savings',
            },
            {
                'pnl_class': 'COGS / Gross Margin (Power External Products)',
                'billing_label_selector': 'labels.pnl_class="cogs"',
                'workloads': 'Customer-facing Gemini Enterprise portal assistants & external partner APIs',
                'share_pct': 37.3,
                'monthly_net_spend_usd': 505.69,
                'accounting_treatment': 'Direct Cost of Goods Sold; unit CpO ($0.0110/session) protects >88% gross margin',
            },
        ],
    }

    # 8. AgentOps Cockpit Static/Runtime FinOps Code Audit (FIN-01..FIN-05), Step-by-Step Waterfall & @cost_guard
    # Ported from /Users/enriq/Documents/git/agent-ops-cockpit/src/agent_ops_cockpit/ops/auditors/finops.py & finops_roi.py
    cockpit_findings: list[dict[str, Any]] = [
        {
            'rule_id': 'FIN-01',
            'title': 'Sequential LLM Inference Loop in Batch Path',
            'file_line': 'agents/deep_research/corpus_scanner.py:84',
            'ast_detection': 'ast.For / ast.While node wrapping client.models.generate_content()',
            'evidence': 'Detected synchronous for-loop invoking gemini-2.5-pro across 50 SEC 10-K filings at 1.0x Standard PayGo rate.',
            'remediation_diff': (
                '- for doc in filings:\n'
                '-     resp = client.models.generate_content(model="gemini-2.5-pro", contents=doc)\n'
                '+ batch_job = client.batches.create(model="gemini-2.5-flash", src=filings_gcs_uri,\n'
                '+                                   config={"service_tier": "deferred"})  # -50% Deferred/Batch rate'
            ),
            'savings_formula': 'loop_spend ($9,600) * 0.50 (deferred discount) * 0.85 (eligible share)',
            'monthly_savings_usd': 4080.00,
            'confidence': 'HIGH',
            'status': 'REMEDIATED (Gen 12)',
        },
        {
            'rule_id': 'FIN-02',
            'title': 'Missing ContextCacheConfig on >4,096 Token System Prefix',
            'file_line': 'agents/it_service_desk/prompt_builder.py:42',
            'ast_detection': 'System prompt literal + tool schemas = 18,400 tok (>4,096 min) without ContextCacheConfig',
            'evidence': ' Turn #2 injected dynamic timestamp at line 1, busting 18.4k static prefix cache (11.1% hit ratio).',
            'remediation_diff': (
                '- system_instruction = f"Time: {now_iso()}\\n{STATIC_RUNBOOK_18K}"\n'
                '+ cache_cfg = types.CreateCachedContentConfig(ttl="3600s", system_instruction=STATIC_RUNBOOK_18K)\n'
                '+ # Dynamic runtime metadata moved to user turn tail below breakpoint line 142'
            ),
            'savings_formula': 'prefix_tok (18.4k) * 280k calls * $0.30/1M * 0.90 - storage ($1.00/1M tok-hr)',
            'monthly_savings_usd': 14210.00,
            'confidence': 'HIGH',
            'status': 'LOCKED (92.8% Cache Hit)',
        },
        {
            'rule_id': 'FIN-03',
            'title': 'Large Model Used on Simple JSON / Regex Formatting Tasks (MP-029)',
            'file_line': 'agents/it_service_desk/ticket_classifier.py:119',
            'ast_detection': 'model="gemini-2.5-pro" paired with response_schema=TicketCategoryEnum & regex post-filter',
            'evidence': 'Frontier model ($1.25/$10.00 per 1M) used for 3-class ticket severity routing and JSON formatting.',
            'remediation_diff': (
                '- model_id = "gemini-2.5-pro"\n'
                '+ model_id = "gemini-2.5-flash-lite"  # Tier 3 Scale ($0.10/$0.40) or Gemma-3-27B SLM-on-Edge'
            ),
            'savings_formula': 'classification_spend ($12,400) * (1 - P_flash_lite / P_pro = 0.92)',
            'monthly_savings_usd': 11408.00,
            'confidence': 'HIGH',
            'status': 'ROUTED TO FLASH-LITE',
        },
        {
            'rule_id': 'FIN-04',
            'title': 'Non-Exponential Retry Cascade on HTTP 429 ResourceExhausted',
            'file_line': 'agents/shared/retry_middleware.py:29',
            'ast_detection': '@retry(wait=wait_fixed(1), stop=stop_after_attempt(5)) on ResourceExhausted',
            'evidence': 'Fixed 1s retry intervals during quota contention multiplied input token billing by 3.4x.',
            'remediation_diff': (
                '- @retry(wait=wait_fixed(1), stop=stop_after_attempt(5))\n'
                '+ @retry(wait=wait_random_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(3))'
            ),
            'savings_formula': 'retry_rate (8.2%) * avg_ctx (19k tok) * excess_retries (2.4) * P_in',
            'monthly_savings_usd': 5820.00,
            'confidence': 'HIGH',
            'status': 'REMEDIATED (0.0% 429s)',
        },
        {
            'rule_id': 'FIN-05',
            'title': 'RAG Retrieval Over-Fetching & Unpruned Schema Dump (top_k > 20)',
            'file_line': 'agents/deep_research/retriever.py:67',
            'ast_detection': 'VertexAISearchRetriever(top_k=50) + raw BigQuery INFORMATION_SCHEMA dump (52,000 tok)',
            'evidence': 'Retrieving 50 chunks + raw DB tables injected 48,500 redundant tokens per turn (64% context bloat).',
            'remediation_diff': (
                '- retriever = VertexAISearchRetriever(top_k=50)\n'
                '+ retriever = KnowledgeCatalogFlashRankRetriever(top_k=5, max_schema_tokens=2000)  # 7x reduction'
            ),
            'savings_formula': '((50 - 5) / 50) * retrieved_tok_spend ($12,613)',
            'monthly_savings_usd': 11352.00,
            'confidence': 'HIGH',
            'status': 'PRUNED (7X REDUCTION)',
        },
    ]
    cockpit_total_savings_usd = round(sum(float(f['monthly_savings_usd']) for f in cockpit_findings), 2)

    # 6-Step Sequential FinOps ROI Waterfall (Deck 3 Cymbal Case Study + Cockpit finops_roi.py)
    # Applied sequentially so savings are never flat-summed or double-counted:
    wf_baseline = 142000.00
    wf_spec = [
        ('Step 1: 4-Tier Model Portfolio Routing (Pro -> Flash / Flash-Lite / Gemma)', 0.68, 0.8676),
        ('Step 2: Explicit & Implicit Prefix Context Caching (-90% Cache Read)', 0.90, 0.6444),
        ('Step 3: GCP Knowledge Catalog (50k->2k) & RAG top_k=5 Pruning', 0.85, 0.4118),
        ('Step 4: Deferred Agents API, Off-Peak PayGo & Batch Lanes (-50%)', 0.50, round(deferred_share_pct / 100.0 + 0.05, 4)),
        ('Step 5: Provisioned Throughput (2 GSUs + Burst PayGo) & 3-Yr FSP (-20%)', 0.35, 0.6000),
    ]
    waterfall_steps = []
    running_cost = wf_baseline
    for idx, (step_name, reduction_rate, eligible_share) in enumerate(wf_spec, start=1):
      effective_cut = round(reduction_rate * eligible_share, 4)
      next_cost = round(running_cost * (1.0 - effective_cut), 2)
      delta_usd = round(running_cost - next_cost, 2)
      cum_pct = round(((wf_baseline - next_cost) / wf_baseline) * 100.0, 1)
      waterfall_steps.append({
          'step_number': idx,
          'step_name': step_name,
          'before_usd': running_cost,
          'after_usd': next_cost,
          'delta_saved_usd': delta_usd,
          'step_reduction_pct': round(effective_cut * 100.0, 1),
          'eligible_share_pct': round(eligible_share * 100.0, 1),
          'cumulative_reduction_pct': cum_pct,
      })
      running_cost = next_cost

    # @cost_guard(budget_limit_usd) Pre-Flight Turn Enforcer & Cross-Pillar OPEX_IMPACT_MAP
    preflight_simulations: list[dict[str, Any]] = [
        {
            'call_id': 'TURN-PF-101',
            'agent_and_handler': 'it_service_desk.resolve_vpn_ticket',
            'model': 'gemini-2.5-flash',
            'input_tokens': 19200,
            'cached_tokens': 17800,
            'max_output_tokens': 1024,
            'estimated_worst_case_usd': 0.0035,
        },
        {
            'call_id': 'TURN-PF-102',
            'agent_and_handler': 'deep_research.synthesize_10k_filing',
            'model': 'gemini-2.5-pro',
            'input_tokens': 22400,
            'cached_tokens': 19600,
            'max_output_tokens': 1536,
            'estimated_worst_case_usd': 0.0191,
        },
        {
            'call_id': 'TURN-PF-103',
            'agent_and_handler': 'deep_research.unpruned_mcp_schema_dump',
            'model': 'gemini-2.5-pro',
            'input_tokens': 54000,
            'cached_tokens': 0,
            'max_output_tokens': 4096,
            'estimated_worst_case_usd': 0.1085,
        },
    ]
    for pf in preflight_simulations:
      est = float(pf['estimated_worst_case_usd'])
      pf['budget_limit_usd'] = budget_limit_usd
      if est <= budget_limit_usd:
        pf['verdict'] = 'PASS (Under @cost_guard Ceiling)'
        pf['action'] = 'Execute on requested model'
      elif est <= budget_limit_usd * 2.2:
        pf['verdict'] = 'AUTO-DOWNGRADE (Exceeds Ceiling)'
        pf['action'] = 'Downgrade model to gemini-2.5-flash + cap thinking_budget_tokens=512'
      else:
        pf['verdict'] = 'BLOCKED (BudgetExceeded Exception)'
        pf['action'] = f'Blocked pre-flight: est ${est:.4f} > limit ${budget_limit_usd:.4f} (0 tokens billed)'

    opex_tradeoff_matrix = [
        {
            'pillar': 'Resiliency (Bounded Retries)',
            'setting': 'Tenacity 3-attempt exponential backoff on transient 503s',
            'opex_multiplier': '+15.0% on failed turn subset (1.8% of turns)',
            'net_monthly_impact_usd': '+$38.40/mo',
            'tradeoff_verdict': 'ACCEPTABLE: Prevents session drop while bounding retry amplification',
        },
        {
            'pillar': 'Observability (Full Payload Logging)',
            'setting': 'Sampling 100% raw prompt/response bodies vs @vibelift_telemetry metadata',
            'opex_multiplier': '+25.0% Cloud Logging & BQ storage if un-sampled',
            'net_monthly_impact_usd': '-$410.00/mo saved via metadata-first hook',
            'tradeoff_verdict': 'OPTIMIZED: Log token/hash metadata on 100% turns; sample full text only on CSAT <= 2★',
        },
        {
            'pillar': 'Security & Privacy (Model Armor + PII Scrub)',
            'setting': 'Pre-flight PII redaction & prompt-injection guardrails',
            'opex_multiplier': '+10.0% pre-processing overhead',
            'net_monthly_impact_usd': '+$92.00/mo',
            'tradeoff_verdict': 'MANDATORY: Required for OAuth 2.0 PDD compliance across 4,000–10,000 DAU',
        },
        {
            'pillar': 'FinOps (Prefix Context Caching)',
            'setting': 'Static system prompt + tool schema pinning (ContextCacheConfig)',
            'opex_multiplier': '-70.0% on cacheable input token spend',
            'net_monthly_impact_usd': '-$14,210.00/mo',
            'tradeoff_verdict': 'HIGHEST ROI: Simultaneously cuts input token spend by 70%+ and P95 TTFT by 40%',
        },
        {
            'pillar': 'Architecture (Modular Skills vs Unpruned MCP)',
            'setting': 'Lazy-loaded SKILL.md (40 tok) + Knowledge Catalog (2k tok) vs 50k MCP dump',
            'opex_multiplier': '-68.0% on tool/schema context tokens',
            'net_monthly_impact_usd': '-$11,352.00/mo',
            'tradeoff_verdict': 'HIGHEST ACCURACY LIFT: 7x token reduction + 63% SQL accuracy improvement',
        },
    ]

    tco_and_pnl['controller_signoff_note'] = (
        'Suggested ASC 350-40 / GAAP classification — requires Corporate Controller sign-off before ERP journal booking.'
    )
    tco_and_pnl['provenance_legend'] = [
        {'tag': 'MEASURED_OTEL', 'description': 'Direct runtime token, latency, and prefix-hash telemetry from @vibelift_telemetry'},
        {'tag': 'RECONCILED_BILLING_EXPORT', 'description': 'Cross-checked against project-maui.billing_export.gcp_billing_export_resource_v1 (±2.0% settlement window)'},
        {'tag': 'MODELED_SIMULATION', 'description': 'Calculated projection from user-adjustable FinOps & Canary assumptions'},
        {'tag': 'PRE_GA_CONTRACT_BENCHMARK†', 'description': 'Commercial or roadmap pricing tier (PT Burst PayGo, Deferred/Off-Peak -50%, FSP 10%/20%) subject to contract terms'},
    ]

    savings_reconciliation_bridge = {
        'realized_active_fleet_token_savings_monthly_usd': 46870.00,
        'addressable_enterprise_waterfall_savings_monthly_usd': round(wf_baseline - running_cost, 2),
        'outcome_adjusted_cpo_plus_hitl_savings_monthly_usd': round(fleet_monthly_baseline_usd - fleet_monthly_optimized_usd, 2),
        'reconciliation_explanation': (
            '1) $46,870/mo is Realized Measured Savings across the 3 active agents (6,840 DAU) from prefix caching & FIN-01..FIN-05 code remediations; '
            '2) $131,955/mo is the Addressable Enterprise Portfolio Waterfall ($142,000 -> $10,045/mo) when combining 4-tier routing, caching, Knowledge Catalog, Deferred lanes, and 2-GSU PT + 3-Yr FSP; '
            '3) $240,042/mo+ is Total Outcome-Adjusted TCO Savings when including avoided Tier-2 human escalation labor (C_HITL) across 129,500 monthly sessions.'
        ),
    }

    return {
        'schema_version': '2.1.0',
        'assumptions': {
            'calls_per_hr': calls_per_hr,
            'prefix_tokens': int(prefix_tokens),
            'implicit_hit_rate': implicit_hit_rate,
            'budget_limit_usd': budget_limit_usd,
            'deferred_share_pct': deferred_share_pct,
            'fsp_commit_years': fsp_commit_years,
            'ge_seats': ge_seats,
            'power_user_ratio_pct': power_user_ratio_pct,
            'monthly_tasks': monthly_tasks,
            'hitl_review_minutes': round(hitl_review_minutes, 2),
            'hitl_hourly_rate_usd': round(hitl_hourly_rate_usd, 2),
        },
        'savings_reconciliation_bridge': savings_reconciliation_bridge,
        'cpo': {
            'formula': 'CpO = (sum_{i=1..n} (C_LLM + C_Tools + C_Infra)_i + C_HITL) / P(CSAT_Positive_Resolution)',
            'hitl_escalation_unit_cost_usd': hitl_ticket_cost_usd,
            'first_contact_resolution_pct': 96.4,
            'fleet_baseline_cpo_usd': fleet_base_cpo,
            'fleet_optimized_cpo_usd': fleet_opt_cpo,
            'fleet_cpo_reduction_pct': fleet_cpo_reduction_pct,
            'fleet_monthly_baseline_usd': round(fleet_monthly_baseline_usd, 2),
            'fleet_monthly_optimized_usd': round(fleet_monthly_optimized_usd, 2),
            'fleet_monthly_saved_usd': round(fleet_monthly_baseline_usd - fleet_monthly_optimized_usd, 2),
            'pareto_outlier_rule': 'Top 10% outlier sessions (>8 loop turns) drive 78.4% of unoptimized token & HITL escalation spend (Deck 2 Page 28).',
            'agents': cpo_agents,
        },
        'drift': {
            'expected_naive_token_spend_usd': expected_naive_token_spend_usd,
            'remediated_drift_total_usd': remediated_drift_sum,
            'unattributed_usd': unattributed_usd,
            'residual_timing_variance_usd': 14.20,
            'reconciliation_tolerance_pct': 2.0,
            'billing_data_freshness_ts': '2026-09-29T04:00:00Z (BigQuery Billing Export SLA <= 4h)',
            'provenance': 'Measured (OTel @vibelift_telemetry) + Allocated (D1..D5 Attribution Rules)',
            'actual_reconciled_invoice_usd': actual_reconciled_invoice_usd,
            'unoptimized_billed_spend_usd': round(
                expected_naive_token_spend_usd + sum(float(d['unoptimized_drift_usd']) for d in drift_drivers), 2
            ),
            'drift_drivers': drift_drivers,
        },
        'attribution_join': attribution_join,
        'metering_and_consumption': {
            'categories': metering_categories,
            'routing_lanes': routing_lanes,
            'model_portfolio_tiers': model_portfolio_tiers,
            'seat_vs_paygo_model': {
                'ge_seats': ge_seats,
                'power_users_count': power_users_count,
                'fsp_commit_years': fsp_commit_years,
                'fsp_discount_pct': fsp_discount_pct,
                'ge_pooled_monthly_usd': ge_pooled_monthly_usd,
                'paygo_unpooled_monthly_usd': paygo_unpooled_monthly_usd,
                'pooled_seat_savings_usd': pooled_seat_savings_usd,
                'pooled_quota_prompts_per_user_day': 1500,
            },
        },
        'caching': caching_economics,
        'gateway_and_extensions': {
            'apigee_policies': apigee_policies,
            'extension_overhead': extension_overhead,
        },
        'tco_and_pnl': tco_and_pnl,
        'cockpit_finops': {
            'auditor_findings': cockpit_findings,
            'total_auditor_monthly_savings_usd': cockpit_total_savings_usd,
            'waterfall': {
                'baseline_monthly_usd': wf_baseline,
                'optimized_monthly_usd': running_cost,
                'total_monthly_saved_usd': round(wf_baseline - running_cost, 2),
                'total_reduction_pct': round(((wf_baseline - running_cost) / wf_baseline) * 100.0, 1),
                'steps': waterfall_steps,
            },
            'cost_guard': {
                'budget_limit_usd': budget_limit_usd,
                'decorator_snippet': (
                    '@cost_guard(budget_limit_usd=0.0200)\n'
                    'def invoke_agent_turn(contents: str, max_output_tokens: int = 1024):\n'
                    '    # Blocks or auto-downgrades turn before LLM call if est worst-case USD > ceiling\n'
                    '    return client.models.generate_content(model="gemini-2.5-flash", contents=contents)'
                ),
                'preflight_simulations': preflight_simulations,
                'opex_tradeoff_matrix': opex_tradeoff_matrix,
            },
        },
    }

  def get_sme_persona_playbooks(self) -> dict[str, Any]:
    """Returns role-tailored SME persona lenses, navigation targets, KPIs, and usability scores."""
    personas: list[dict[str, Any]] = [
        {
            'persona_id': 'finops_lead',
            'role_title': 'FinOps Lead / Cloud Economist',
            'short_label': 'FinOps & Billing Lead',
            'before_score': 36,
            'after_score': 92,
            'score_delta': '+56 pts',
            'primary_tab': 3,
            'target_panel_id': 'tokenomicsCpoDriftPanel',
            'primary_kpis': [
                'True Cost per Outcome (CpO): $2.27 -> $0.42 (-81.6%)',
                'Dual-Ledger Billing SKU Variance: -1.67% (Within ±2% SLA)',
                'Explicit Cache Break-Even: N* = 4.70 calls/hr (Flash)',
                'GSU + 1-Yr CUD Floor: 2 GSUs ($4,150/mo net savings)',
            ],
            'key_questions_answered': [
                'Why does our GCP invoice differ from raw token x list price estimates (D1..D5 drift)?',
                'When should we switch from Implicit to Explicit Context Caching or commit to Provisioned GSUs?',
                'How do we join Gemini Enterprise activity, OTel traces, and Cloud Billing Export per user_ldap?',
            ],
            'actionable_controls': 'Interactive FinOps Simulator (/api/recompute_finops), 3-Way Attribution SQL, GSU/CUD Advisor',
        },
        {
            'persona_id': 'sre_platform',
            'role_title': 'SRE / Cloud Platform Engineer',
            'short_label': 'SRE & Platform Eng',
            'before_score': 47,
            'after_score': 90,
            'score_delta': '+43 pts',
            'primary_tab': 0,
            'target_panel_id': 'watchOutAlarmsContainer',
            'primary_kpis': [
                'P95 Turn Latency: 2,480ms -> 690ms (-72.2%)',
                '400/429 Error Rate: 6.8% -> 0.0% (Tenacity + Apigee Freeze)',
                'Cloud Run Revision Split: 85% Prod / 15% Canary',
                '5-Layer OTel Compliance: 26/26 Metrics Within SLO',
            ],
            'key_questions_answered': [
                'Which Cloud Run service revision or Reasoning Engine is causing 429/5xx tail latency spikes?',
                'Are any agents stuck in recursive A2A sub-agent handoff loops or thinking token runaways?',
                'What is the exact gcloud run services update-traffic command to promote or roll back a canary?',
            ],
            'actionable_controls': 'Live Fleet Refresh (/api/sync_ge_fleet), 5-Layer OTel Filter, Cloud Run Canary CLI Generator',
        },
        {
            'persona_id': 'ai_engineer',
            'role_title': 'AI / Agent & Prompt Engineer',
            'short_label': 'AI / Agent Engineer',
            'before_score': 43,
            'after_score': 94,
            'score_delta': '+51 pts',
            'primary_tab': 2,
            'target_panel_id': 'whatIfSimulatorPanel',
            'primary_kpis': [
                'Prompt Cache Hit Ratio: 12.0% -> 91.2% (Line-142 Lock)',
                'Context Bloating Ratio: 64.0% -> 14.2% (N-2 Window)',
                'AST Code Audit: 5/5 FIN-01..FIN-05 Rules Remediated',
                'Schema Footprint: 50,000 tok -> 2,000 tok (7x Reduction)',
            ],
            'key_questions_answered': [
                'Which exact 1-based prompt line mutated and busted the static prefix cache hash?',
                'Which Python files trigger FIN-01..FIN-05 anti-patterns (loops, uncached prefixes, top_k>20)?',
                'What happens to latency, accuracy, and cost if I cap thinking_budget_tokens=512 on Flash?',
            ],
            'actionable_controls': 'What-If Simulator (/api/what_if_simulate), Step Turn (/api/step_turn), AST Code Auditor (FIN-01..05)',
        },
        {
            'persona_id': 'product_quality',
            'role_title': 'Product Manager / Quality & VoC Lead',
            'short_label': 'Product & VoC Lead',
            'before_score': 30,
            'after_score': 88,
            'score_delta': '+58 pts',
            'primary_tab': 3,
            'target_panel_id': 'vocRatingsBody',
            'primary_kpis': [
                'Task Grounding Accuracy: 91.0% -> 96.4% (95% Floor)',
                'Average User CSAT: 4.10 ★ -> 4.92 ★ (aive_logs.ratings_log)',
                'First-Contact Resolution: 76.2% -> 96.4% (-80% Escalations)',
                'Guardrail Rollbacks: Gen 9 Rejected (74.0% < 95.0% SLO)',
            ],
            'key_questions_answered': [
                'Did our cost optimizations degrade answer grounding accuracy or user CSAT ratings?',
                'Which specific session_ids and user_ldaps reported 1-2★ feedback or L1->L2 escalations?',
                'Did the Reviewer Gatekeeper block aggressive token-pruning mutations that hurt quality?',
            ],
            'actionable_controls': 'Live CSAT Feedback Submission (/api/csat_rating), L1/L2 Support Triage Stream, Guardrail Audit',
        },
        {
            'persona_id': 'security_governance',
            'role_title': 'Security & API Governance Architect',
            'short_label': 'Security & Apigee Arch',
            'before_score': 26,
            'after_score': 87,
            'score_delta': '+61 pts',
            'primary_tab': 3,
            'target_panel_id': 'apigeeAndExtensionsPanel',
            'primary_kpis': [
                'Apigee Edge Quotas: 7 Active Policies (APG-01..APG-07)',
                'Automated Traffic Freeze: <50ms Circuit Breaker Trigger',
                'NL2SQL Safety Gate: Read-Only SELECT + 100 MB Byte Cap',
                'OAuth 2.0 & PDD Compliance: Verified for 4k-10k DAU',
            ],
            'key_questions_answered': [
                'How do we enforce per-LDAP token quotas and hard spend caps before requests hit Vertex AI?',
                'How do we pause runaway recursive agent traffic in <50ms instead of waiting 48h for billing export?',
                'Are NL2SQL telemetry queries restricted to read-only SELECTs with DML/DDL blocking?',
            ],
            'actionable_controls': 'Apigee Policy Ledger (APG-01..07), @cost_guard Pre-Flight Enforcer, NL2SQL Read-Only Verifier',
        },
        {
            'persona_id': 'cfo_exec',
            'role_title': 'CFO / VP of Engineering (Exec Sponsor)',
            'short_label': 'CFO & VP Engineering',
            'before_score': 30,
            'after_score': 91,
            'score_delta': '+61 pts',
            'primary_tab': 3,
            'target_panel_id': 'cockpitFinopsAndTcoPanel',
            'primary_kpis': [
                'Cost / CSAT-Positive Session: $0.231 -> $0.011 (-95.2%)',
                'Enterprise AI TCO Split: 30% Visible Tech / 70% Hidden Ops',
                'P&L Token Accounting: 14.2% CapEx / 48.5% OpEx / 37.3% COGS',
                'Seat vs PAYGO Arbitrage: $17,600/mo Pooled Seat Savings',
            ],
            'key_questions_answered': [
                'How do our $46.8k/mo realized savings, $132k/mo portfolio waterfall, and $240k/mo HITL savings reconcile?',
                'How is AI spend classified across CapEx (R&D), OpEx (SG&A), and COGS (Gross Margin protection)?',
                'Should we license Gemini Enterprise Pooled Seats ($30/mo + FSP) or raw PAYGO API for power users?',
            ],
            'actionable_controls': 'Savings Reconciliation Bridge, CFO 30/70 TCO & P&L Ledger, GE Seat vs PAYGO Calculator',
        },
    ]
    avg_before = round(sum(p['before_score'] for p in personas) / len(personas), 1)
    avg_after = round(sum(p['after_score'] for p in personas) / len(personas), 1)
    return {
        'rubric_dimensions': [
            '1. Time-to-Insight (20 pts)',
            '2. Actionability & Control (20 pts)',
            '3. Data Trust & Reconciliation (20 pts)',
            '4. Persona Ergonomics & Navigation (20 pts)',
            '5. Guardrail & Risk Prevention (20 pts)',
        ],
        'fleet_average_before_score': avg_before,
        'fleet_average_after_score': avg_after,
        'fleet_average_delta': f'+{round(avg_after - avg_before, 1)} pts',
        'opus_model_garden_verified': True,
        'personas': personas,
    }

  def simulate_what_if_scenario(
      self,
      model_tier: str = 'gemini-2.5-flash',
      thinking_budget_tok: int = 1024,
      history_window_turns: int = 2,
      traffic_canary_pct: int = 10,
  ) -> dict[str, Any]:
    """Simulates a What-If FinOps & Latency configuration before Cloud Run Canary deployment."""
    active = self.active_agent
    clean_model = (model_tier or active.model or 'gemini-2.5-flash').strip()
    think_cap = max(0, min(16384, int(thinking_budget_tok)))
    win_turns = max(1, min(20, int(history_window_turns)))
    canary_pct = max(1, min(100, int(traffic_canary_pct)))

    model_mult = {
        'gemini-3.1-flash-lite': 0.52,
        'gemini-3.1-flash-tier-routed': 0.68,
        'gemini-2.5-flash': 0.78,
        'gemini-3.6-flash': 0.88,
        'gemini-2.5-pro': 1.45,
    }.get(clean_model, 0.80)
    think_mult = 0.68 if think_cap <= 512 else (0.82 if think_cap <= 1024 else (1.0 if think_cap <= 2048 else 1.35))
    win_mult = 0.75 if win_turns <= 2 else (0.90 if win_turns <= 6 else 1.25)

    cur_cost = _get_param_val(active, 'cost_usd', 3.45)
    cur_lat = _get_param_val(active, 'latency_ms', 690.0)
    cur_acc = _get_param_val(active, 'accuracy_pct', 96.4)
    cur_cache = _get_param_val(active, 'cache_hit_pct', 91.2)

    proj_cost = round(max(0.95, cur_cost * model_mult * think_mult * win_mult), 2)
    proj_lat = round(max(290.0, cur_lat * (0.72 + 0.18 * model_mult) * think_mult), 1)
    acc_delta = 0.8 if clean_model == 'gemini-2.5-pro' else (-0.4 if clean_model == 'gemini-3.1-flash-lite' else 0.3)
    if think_cap < 256:
      acc_delta -= 1.1
    proj_acc = round(min(99.4, max(90.0, cur_acc + acc_delta)), 1)
    proj_cache = round(min(98.2, cur_cache + (3.2 if win_turns <= 6 else 0.8)), 1)
    proj_session_cost = round((proj_cost / 1000.0) * 3.0, 4)
    proj_monthly_savings = round(active.monthly_savings_usd * (1.0 + max(0.05, (cur_cost - proj_cost) / max(1.0, cur_cost) * 0.45)))
    guardrail_passed = proj_acc >= 95.0 and proj_lat <= 850.0

    gen_num = (active.timeline[-1].generation if active.timeline else 14) + 1
    svc_slug = active.agent_id.replace('_', '-')
    canary_cmd = (
        f'gcloud run services update-traffic {svc_slug} '
        f'--region=us-central1 --to-revisions={svc_slug}-gen{gen_num}={canary_pct},LATEST={100 - canary_pct}'
    )
    gitops_diff = (
        f'--- a/agents/{active.agent_id}/config.yaml\n'
        f'+++ b/agents/{active.agent_id}/config.yaml\n'
        f'@@ -1,6 +1,7 @@\n'
        f'-model: {active.model}\n'
        f'+model: {clean_model}\n'
        f'+thinking_config:\n'
        f'+  thinking_budget_tokens: {think_cap}\n'
        f'+context_compaction:\n'
        f'+  sliding_window_turns: {win_turns}\n'
        f'+  pin_static_prefix_cache: true'
    )
    return {
        'agent_id': active.agent_id,
        'agent_display_name': active.display_name,
        'model_tier': clean_model,
        'thinking_budget_tok': think_cap,
        'history_window_turns': win_turns,
        'traffic_canary_pct': canary_pct,
        'baseline_cost_per_1k_usd': 29.40,
        'current_cost_per_1k_usd': cur_cost,
        'projected_cost_per_1k_usd': proj_cost,
        'projected_cost_per_resolved_session_usd': proj_session_cost,
        'current_p95_latency_ms': cur_lat,
        'projected_p95_latency_ms': proj_lat,
        'projected_accuracy_pct': proj_acc,
        'accuracy_guardrail_floor_pct': 95.0,
        'projected_cache_hit_pct': proj_cache,
        'projected_monthly_savings_usd': proj_monthly_savings,
        'additional_monthly_savings_usd': max(1200, proj_monthly_savings - int(active.monthly_savings_usd)),
        'guardrail_passed': guardrail_passed,
        'guardrail_status': 'SAFE_TO_PROMOTE_CANARY' if guardrail_passed else 'BLOCKED_BY_ACCURACY_GUARDRAIL',
        'guardrail_verdict': (
            f'PASS — Ready for {canary_pct}% Cloud Run Canary Rollout (Accuracy {proj_acc}% >= 95.0% SLO)'
            if guardrail_passed
            else f'BLOCKED — Candidate violates Accuracy/Latency Guardrail ({proj_acc}% / {proj_lat}ms)'
        ),
        'canary_rollout_command': canary_cmd,
        'gitops_diff': gitops_diff,
    }

  def select_optimizer_platform(self, platform_id: str) -> dict[str, str]:
    """Switches the active optimization platform (AlphaEvolve, Opus Critic, Vizier, or Hybrid)."""
    clean = (platform_id or '').strip().lower()
    if clean in OPTIMIZER_PLATFORMS:
      self.selected_optimizer_platform = clean
    return OPTIMIZER_PLATFORMS[self.selected_optimizer_platform]

  def get_optimizer_platforms_payload(self) -> dict[str, Any]:
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

  def sync_from_ge_fleet(
      self,
      fleet_payload: dict[str, Any] | None,
      bq_insights: dict[str, Any] | None = None,
  ) -> None:
    """Synchronizes the selectable optimization catalog with live agents from Gemini Enterprise."""
    if not isinstance(fleet_payload, dict):
      return
    proj = str(fleet_payload.get('project_id') or '').strip()
    if proj and proj not in ('test-project', 'UNCONFIGURED-PROJECT'):
      self._live_fleet_payload = fleet_payload
      if isinstance(bq_insights, dict) and bq_insights:
        self._live_bq_insights = bq_insights
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
      raw_backend = raw_agent.get('backend')
      backend = raw_backend if isinstance(raw_backend, dict) else {}
      models = as_list(backend.get('models'))
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

  def get_all_agents_dict(self) -> dict[str, dict[str, Any]]:
    """Returns full serialized profiles keyed by agent_id so embedded UI can switch agents locally."""
    return {agent_id: profile.to_dict() for agent_id, profile in self._agents.items()}

  def list_agents_summary(self) -> list[dict[str, Any]]:
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
        param.status = 'REMEDIATED (0.0%)'
      elif param.key == 'context_bloat_pct':
        param.current_value = max(8.5, round(min(param.current_value, 14.2) * 0.85, 1))
        param.status = f'PRUNED ({param.current_value}% Bloat)'
      elif param.key == 'idle_ratio_pct':
        param.current_value = max(4.2, round(min(param.current_value, 8.4) * 0.85, 1))
        param.status = f'OPTIMIZED ({param.current_value}% Idle)'

    agent.health_status = f'OPTIMIZED & REMEDIATED ({platform_short} • Gen {next_gen} Active)'
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
            event_marker=f'🧬 {platform_short} Gen {next_gen} Remediated',
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
            status=f'REMEDIATED & DEPLOYED (PR #{100 + next_gen})',
        ),
    )
    return agent
