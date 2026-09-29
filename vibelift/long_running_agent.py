"""Long-running ADK + Google Cloud agent runner emitting structured turn logs."""

from collections.abc import Sequence
import datetime

from vibelift import optimizer as alpha_evolve_optimizer
from vibelift import telemetry


GCP_ADK_TOOL_SEQUENCE: tuple[tuple[str, str], ...] = (
    (
        'adk.agent_engine.it_service_desk',
        'Triaged IT support ticket & checked runbooks via service-desk-agent with static prefix cache hit.',
    ),
    (
        'adk.agent_engine.service_desk_escalation',
        'Reviewed resolution plan against IT policy & escalated to tier-2 service-desk-escalation.',
    ),
    (
        'mcp.vibelift_analytics.open_dashboard',
        'Aggregated live Gemini Enterprise fleet telemetry & prompt cache FinOps economics.',
    ),
    (
        'gemini_enterprise.deep_research',
        'Synthesized multi-source research plan and citation-grounded report with deduplicated snippets.',
    ),
    (
        'gcp.cloud_storage_checkpoint_writer',
        'Persisted long-running agent state to GCS & pruned N-2 turn tool outputs.',
    ),
)

# Backward compatibility alias
ORCAS_TOOL_SEQUENCE = GCP_ADK_TOOL_SEQUENCE


class LongRunningVibeLiftAgent:
  """Simulates and executes a long-running ADK multi-turn agent with live telemetry."""

  def __init__(
      self,
      optimizer: alpha_evolve_optimizer.VibeLiftAlphaEvolveOptimizer,
      agent_name: str | None = None,
      model: str | None = None,
      max_turns: int = 35,
  ) -> None:
    """Initializes the long-running agent runtime and seed log trajectory."""
    self._optimizer = optimizer
    self.agent_name = agent_name or optimizer.active_agent.agent_id
    self.model = model or optimizer.active_agent.model
    self.max_turns = max_turns
    self._turns: list[telemetry.TurnUsageLog] = []
    self._step_descriptions: list[dict[str, object]] = []
    self.reset_with_seed_turns()

  @property
  def turns(self) -> list[telemetry.TurnUsageLog]:
    """Returns the recorded structured turn logs."""
    return list(self._turns)

  @property
  def step_descriptions(self) -> list[dict[str, object]]:
    """Returns human-readable trajectory steps."""
    return list(self._step_descriptions)

  def _current_generation(self) -> int:
    """Returns the current generation of the active agent profile."""
    profile = self._optimizer.active_agent
    if profile.timeline:
      return profile.timeline[-1].generation
    return 14

  def reset_with_seed_turns(self) -> None:
    """Populates realistic initial turns showing Gen 0 spike -> Gen 14 fix."""
    self._turns.clear()
    self._step_descriptions.clear()
    self._append_turn(
        turn_idx=1,
        gen=0,
        cached_tokens=2400,
        write_tokens=14000,
        uncached_tokens=1800,
        out_tokens=390,
        thought_tokens=160,
        breakpoint_line=None,
        breakpoint_reason='Initial cold-start prefix cache creation',
        status_code=200,
    )
    self._append_turn(
        turn_idx=2,
        gen=0,
        cached_tokens=2048,
        write_tokens=1200,
        uncached_tokens=15800,
        out_tokens=420,
        thought_tokens=210,
        breakpoint_line=1,
        breakpoint_reason=(
            'Dynamic timestamp injected at Line 1 before 16.4k static schemas'
        ),
        status_code=200,
    )
    for idx in range(3, 7):
      self._append_turn(
          turn_idx=idx,
          gen=self._current_generation(),
          cached_tokens=16400 + (idx * 120),
          write_tokens=0,
          uncached_tokens=1150,
          out_tokens=380,
          thought_tokens=170,
          breakpoint_line=None,
          breakpoint_reason='100% static prefix match across turns',
          status_code=200,
      )

  def step_turn(self) -> telemetry.TurnUsageLog:
    """Executes the next turn of the long-running agent and logs telemetry."""
    gen = self._current_generation()
    next_idx = len(self._turns) + 1
    cached_tok = int(16400 + (next_idx * 140))
    uncached_tok = max(680, int(18000 - cached_tok))
    return self._append_turn(
        turn_idx=next_idx,
        gen=gen,
        cached_tokens=cached_tok,
        write_tokens=0,
        uncached_tokens=uncached_tok,
        out_tokens=395,
        thought_tokens=180,
        breakpoint_line=None,
        breakpoint_reason='100% static prefix match across turns',
        status_code=200,
    )

  def ingest_gcp_cloud_turns(
      self,
      cloud_turns: Sequence[telemetry.TurnUsageLog],
  ) -> None:
    """Ingests live turns fetched from Google Cloud Logging and Gemini Enterprise."""
    for turn in cloud_turns:
      self._turns.append(turn)
      naive_usd, actual_usd, saved_usd = turn.compute_costs()
      self._step_descriptions.insert(
          0,
          {
              'turn_index': turn.turn_index,
              'tool_called': turn.tool_called,
              'description': (
                  f'Live Google Cloud turn from {turn.agent_name} '
                  f'[{turn.model}] ({turn.cached_content_token_count} cached / '
                  f'{turn.prompt_token_count} prompt tokens)'
              ),
              'cache_hit_ratio': turn.cache_hit_ratio,
              'naive_usd': naive_usd,
              'actual_usd': actual_usd,
              'saved_usd': saved_usd,
              'generation': turn.evolution_generation,
              'breakpoint_reason': turn.cache_breakpoint_reason,
          },
      )

  def _append_turn(
      self,
      turn_idx: int,
      gen: int,
      cached_tokens: int,
      write_tokens: int,
      uncached_tokens: int,
      out_tokens: int,
      thought_tokens: int,
      breakpoint_line: int | None,
      breakpoint_reason: str,
      status_code: int,
  ) -> telemetry.TurnUsageLog:
    """Creates and records a TurnUsageLog and trajectory step entry."""
    tool_name, tool_desc = GCP_ADK_TOOL_SEQUENCE[
        (turn_idx - 1) % len(GCP_ADK_TOOL_SEQUENCE)
    ]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ'
    )
    total_prompt = cached_tokens + write_tokens + uncached_tokens
    prefix_hash = (
        f'gen{gen}_static_8f91a2c'
        if breakpoint_line is None
        else f'gen{gen}_bust_line{breakpoint_line}'
    )
    log_entry = telemetry.TurnUsageLog(
        timestamp=now_iso,
        agent_name=self.agent_name,
        model=self.model,
        turn_index=turn_idx,
        prompt_prefix_hash=prefix_hash,
        cache_breakpoint_line=breakpoint_line,
        cache_breakpoint_reason=breakpoint_reason,
        prompt_token_count=total_prompt,
        cached_content_token_count=cached_tokens,
        cache_creation_input_tokens=write_tokens,
        uncached_input_tokens=uncached_tokens,
        candidates_token_count=out_tokens,
        thoughts_token_count=thought_tokens,
        status_code=status_code,
        tool_called=tool_name,
        evolution_generation=gen,
    )
    self._turns.append(log_entry)
    naive_usd, actual_usd, saved_usd = log_entry.compute_costs()
    self._step_descriptions.insert(
        0,
        {
            'turn_index': turn_idx,
            'tool_called': tool_name,
            'description': tool_desc,
            'cache_hit_ratio': log_entry.cache_hit_ratio,
            'naive_usd': naive_usd,
            'actual_usd': actual_usd,
            'saved_usd': saved_usd,
            'generation': gen,
            'breakpoint_reason': breakpoint_reason,
        },
    )
    return log_entry