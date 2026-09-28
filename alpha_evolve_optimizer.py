"""Multi-agent profiles, user parameters, time-series & AlphaEvolve actions."""

import dataclasses


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


def _build_default_agents() -> dict[str, DemoAgentProfile]:
  """Creates the selectable demo agents with realistic log telemetry."""
  mortgage = DemoAgentProfile(
      agent_id='mortgage_assistant',
      display_name='Mortgage Underwriting Assistant',
      domain='Consumer Lending & Rate Qualification',
      model='gemini-3.1-flash-lite',
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
              label='Task Accuracy & Grounding Score',
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
              label='400/429 Tool & Quota Error Rate',
              unit='%',
              direction='LOWER',
              baseline_value=6.8,
              current_value=0.0,
              target_value=0.5,
              weight_pct=5,
              status='SELF-HEALED (0.0%)',
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
              'Subagent Tier + Schema Fix',
          ),
      ],
      actions=[
          AlphaEvolveActionRecord(
              generation=4,
              timestamp='Day 2 • 14:20 UTC',
              action_title='Prompt Prefix Reordering (Static-First Cache Lock)',
              parameter_targeted='Cost ($) & Cache Hit Ratio (%)',
              root_cause_from_logs=(
                  'Log diff revealed dynamic timestamp & live mortgage rate '
                  'table at line 1 invalidated 16,400 static schema tokens.'
              ),
              action_taken=(
                  'AlphaEvolve moved static underwriting rules & MCP schemas '
                  'to top of SystemPrompt and pushed clock/rates to suffix.'
              ),
              impact_summary='Cache Hit: 12% -> 82.4% | Cost: $29.40 -> $6.10',
              diff_snippet=(
                  '- [Line 1] Current Time: {{now_iso}}, Rate: {{live_rate}}\n'
                  '+ [Line 1] STATIC_SYSTEM_SCHEMAS (16,400 tokens cached)\n'
                  '+ [Suffix] Context Delta: Time={{now_iso}}'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=8,
              timestamp='Day 3 • 09:15 UTC',
              action_title='Sliding-Window Tool History Compression (N-2)',
              parameter_targeted='Latency (ms) & Cost ($)',
              root_cause_from_logs=(
                  'Multi-turn logs showed 9,400-token JSON credit tables '
                  're-sent verbatim across turns 3..12, slowing TTFT.'
              ),
              action_taken=(
                  'Synthesized N-2 turn summarizer that compacts historical '
                  'tool outputs into 180-token structured digests.'
              ),
              impact_summary='Latency: 1,420ms -> 980ms | Cost: $6.10 -> $4.80',
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
                  'Novel Strategist attempted deleting all 6 underwriting '
                  'few-shot examples to shave 1,800 tokens.'
              ),
              action_taken=(
                  'Reviewer Gatekeeper automatically REJECTED candidate after '
                  'shadow log replay showed Accuracy dropping 94.1% -> 74.0%.'
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
              action_title='Strict JSON Schema Guard + Flash-Lite Subagent Routing',
              parameter_targeted='Accuracy (%), Latency (ms) & Error Rate (%)',
              root_cause_from_logs=(
                  'Logs captured 6.8% HTTP 400 malformed tool calls and 429 '
                  'quota retries on subagent calls.'
              ),
              action_taken=(
                  'Enforced strict enum/type constraints on tool parameters '
                  'and routed lookup subagents to gemini-3.1-flash-lite.'
              ),
              impact_summary=(
                  'Latency: 980ms -> 690ms | Accuracy: 96.4% | Errors: 0.0%'
              ),
              diff_snippet=(
                  '+ STRICT_JSON_SCHEMA_VALIDATION = True\n'
                  '+ SUBAGENT_MODEL_ROUTE = "gemini-3.1-flash-lite"'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  forecast = DemoAgentProfile(
      agent_id='forecast_engine',
      display_name='Financial Grid & Rate Forecast Engine',
      domain='Energy Load & Multi-Horizon Financial Solver',
      model='gemini-3.1-flash',
      health_status='OPTIMIZED (Gen 11 Active)',
      monthly_savings_usd=22400,
      parameters=[
          OptimizationParameter(
              'latency_ms',
              'P95 Solver Turn Latency',
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
              'Solver Convergence Accuracy',
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
              'Inline countTokens Baseline',
          ),
          TimeSeriesPoint(
              'Day 3 (Gen 6)',
              6,
              1890,
              9.40,
              94.8,
              81.0,
              1.4,
              'OOXML AST Cache Lock',
          ),
          TimeSeriesPoint(
              'Day 5 (Gen 11)',
              11,
              1120,
              5.80,
              97.2,
              89.4,
              0.0,
              'Gauss-Seidel Prompt Pruning',
          ),
      ],
      actions=[
          AlphaEvolveActionRecord(
              generation=6,
              timestamp='Day 3 • 10:12 UTC',
              action_title='OOXML Parser & Solver AST Prefix Caching',
              parameter_targeted='Cost ($) & Cache Hit Ratio (%)',
              root_cause_from_logs=(
                  'Directory tree and solver formulas were re-tokenized on '
                  'every batch task without shared context caching.'
              ),
              action_taken=(
                  'Pinned 22k-token repository map & formula definitions into '
                  'a shared 1-hour explicit cache block.'
              ),
              impact_summary='Cache Hit: 18% -> 81.0% | Cost: $42.00 -> $9.40',
              diff_snippet=(
                  '+ EXPLICIT_CONTEXT_CACHE_TTL = "3600s"\n'
                  '+ PIN_REPO_AST_IN_SYSTEM_PREFIX = True'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=11,
              timestamp='Day 5 • 15:30 UTC',
              action_title='Parallel Tool Batching & Solver Verification Anchor',
              parameter_targeted='Latency (ms) & Accuracy (%)',
              root_cause_from_logs=(
                  'Sequential tool calls across 6 workbook tabs added 2,100ms '
                  'of serial round-trip overhead per turn.'
              ),
              action_taken=(
                  'Evolved prompt directive to emit parallel tool calls in a '
                  'single turn + added debt-service goal-seek check.'
              ),
              impact_summary=(
                  'Latency: 1,890ms -> 1,120ms | Accuracy: 89.5% -> 97.2%'
              ),
              diff_snippet=(
                  '+ PARALLEL_TOOL_DISPATCH = True\n'
                  '+ VERIFY_GOAL_SEEK_CONVERGENCE = 1e-6'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  stock = DemoAgentProfile(
      agent_id='stock_market_updates',
      display_name='Stock Market & Portfolio Intelligence Agent',
      domain='Multi-Subagent Real-Time Equity & News Synthesis',
      model='gemini-3.1-flash-lite',
      health_status='OPTIMIZED (Gen 12 Active)',
      monthly_savings_usd=9650,
      parameters=[
          OptimizationParameter(
              'latency_ms',
              'Multi-Subagent P95 Latency',
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
              'Citation & Market Data Accuracy',
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
              'Subagent 400/429 Error Rate',
              '%',
              'LOWER',
              13.4,
              0.0,
              0.5,
              15,
              'SELF-HEALED (0.0%)',
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
              '429 Quota & 400 Schema Errors',
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
              action_title='Subagent Search Snippet Deduplication & Prefix Share',
              parameter_targeted='Cost ($) & Latency (ms)',
              root_cause_from_logs=(
                  'google_search_agent appended raw HTML snippets across '
                  'subagent hops, causing 429 token-per-minute bursts.'
              ),
              action_taken=(
                  'Shared static search schema across subagents and capped '
                  'search results to top-3 structured summaries.'
              ),
              impact_summary='Latency: 3,100ms -> 1,290ms | Cost: $19.80 -> $4.50',
              diff_snippet=(
                  '+ MAX_SEARCH_SNIPPETS_PER_HOP = 3\n'
                  '+ STRIP_RAW_HTML_BOILERPLATE = True'
              ),
              status='APPLIED & VERIFIED',
          ),
          AlphaEvolveActionRecord(
              generation=12,
              timestamp='Day 5 • 18:10 UTC',
              action_title='Closed-Loop 400/429 Self-Healing & Backoff Guard',
              parameter_targeted='Error Rate (%) & Accuracy (%)',
              root_cause_from_logs=(
                  'Runtime logs recorded 13.4% error rate (400 malformed '
                  'ticker symbols + 429 burst failures).'
              ),
              action_taken=(
                  'Added regex ticker sanitizer and adaptive token-bucket '
                  'pacing between parallel search subagents.'
              ),
              impact_summary='Error Rate: 13.4% -> 0.0% | Accuracy: 97.8%',
              diff_snippet=(
                  '+ TICKER_REGEX_VALIDATOR = "^[A-Z]{1,5}$"\n'
                  '+ SUBAGENT_QPS_PACING_MS = 120'
              ),
              status='ACTIVE PRODUCTION ELITE',
          ),
      ],
  )

  return {
      mortgage.agent_id: mortgage,
      forecast.agent_id: forecast,
      stock.agent_id: stock,
  }


class VibeLiftAlphaEvolveOptimizer:
  """Manages selectable demo agents, user parameters, and AlphaEvolve loops."""

  def __init__(self) -> None:
    """Initializes the multi-agent catalog and sets default active agent."""
    self._agents: dict[str, DemoAgentProfile] = _build_default_agents()
    self.selected_agent_id: str = 'mortgage_assistant'

  @property
  def active_agent(self) -> DemoAgentProfile:
    """Returns the currently selected DemoAgentProfile."""
    return self._agents[self.selected_agent_id]

  def select_agent(self, agent_id: str) -> DemoAgentProfile:
    """Switches the currently selected agent by ID."""
    if agent_id in self._agents:
      self.selected_agent_id = agent_id
    return self.active_agent

  def list_agents_summary(self) -> list[dict[str, object]]:
    """Returns summary metadata for all available demo agents in dropdown."""
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
    """Adds a custom user-defined optimization parameter to active agent."""
    agent = self.active_agent
    key = label.lower().replace(' ', '_')[:24]
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
    agent.parameters.append(
        OptimizationParameter(
            key=key,
            label=label,
            unit=unit,
            direction=norm_dir,
            baseline_value=baseline_val,
            current_value=current_val,
            target_value=target_val,
            weight_pct=weight_pct,
            status='TRACKING IN LOGS (AlphaEvolve Active)',
        )
    )
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
    """Runs AlphaEvolve to heal anomalies and improve all agent parameters."""
    agent = self.active_agent
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

    agent.health_status = f'OPTIMIZED & HEALED (Gen {next_gen} Active)'
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
            event_marker=f'🧬 AlphaEvolve Gen {next_gen} Auto-Healed',
        )
    )
    agent.actions.insert(
        0,
        AlphaEvolveActionRecord(
            generation=next_gen,
            timestamp=f'Live Run • Gen {next_gen}',
            action_title=(
                f'Gen {next_gen}: Cross-Turn Context Deduplication & '
                'Speculative Prefix Warming'
            ),
            parameter_targeted='Latency (ms), Cost ($), Accuracy (%) & Cache %',
            root_cause_from_logs=(
                'Detected uncached context growth and subagent serialization '
                'overhead in recent log window.'
            ),
            action_taken=(
                'Locked static prompt prefix hash, enabled speculative '
                'subagent cache key sharing, and pruned redundant tool JSON.'
            ),
            impact_summary=(
                f'Latency: -> {new_lat}ms | Cost: -> ${new_cost} | '
                f'Accuracy: -> {new_acc}% | Cache Hit: -> {new_hit}%'
            ),
            diff_snippet=(
                f'+ GENERATION_ID = {next_gen}\n'
                '+ SPECULATIVE_PREFIX_WARMING = True\n'
                '+ CROSS_SUBAGENT_CACHE_KEY = "vibelift_global_v4"'
            ),
            status=f'AUTO-HEALED & DEPLOYED (PR #{100 + next_gen})',
        ),
    )
    return agent