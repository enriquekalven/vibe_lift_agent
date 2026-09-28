"""Log-based token cache economics, pricing, and prompt breakpoint analyzer."""

from collections.abc import Mapping, Sequence
import dataclasses
import hashlib
import json


@dataclasses.dataclass(frozen=True)
class ModelRateCard:
  """Pricing rate card per 1M tokens from Admin Console Catalog."""

  model_name: str
  input_per_million_usd: float
  cached_read_per_million_usd: float
  cache_write_per_million_usd: float
  output_per_million_usd: float


RATE_CARDS: dict[str, ModelRateCard] = {
    'gemini-3.1-flash': ModelRateCard(
        model_name='gemini-3.1-flash',
        input_per_million_usd=1.25,
        cached_read_per_million_usd=0.125,
        cache_write_per_million_usd=1.56,
        output_per_million_usd=5.00,
    ),
    'gemini-3.1-flash-lite': ModelRateCard(
        model_name='gemini-3.1-flash-lite',
        input_per_million_usd=0.50,
        cached_read_per_million_usd=0.05,
        cache_write_per_million_usd=0.625,
        output_per_million_usd=2.00,
    ),
    # Gemini 2.5 / 3.x Flash cards below: Vertex AI list prices per 1M tokens (<=200K context,
    # global endpoint) from cloud.google.com/vertex-ai/generative-ai/pricing, checked 2026-09-25.
    'gemini-2.5-flash': ModelRateCard(
        model_name='gemini-2.5-flash',
        input_per_million_usd=0.30,
        cached_read_per_million_usd=0.03,
        cache_write_per_million_usd=0.30,
        output_per_million_usd=2.50,
    ),
    'gemini-2.5-pro': ModelRateCard(
        model_name='gemini-2.5-pro',
        input_per_million_usd=1.25,
        cached_read_per_million_usd=0.125,
        cache_write_per_million_usd=1.25,
        output_per_million_usd=10.00,
    ),
    'gemini-3.5-flash': ModelRateCard(
        model_name='gemini-3.5-flash',
        input_per_million_usd=1.50,
        cached_read_per_million_usd=0.15,
        cache_write_per_million_usd=1.50,
        output_per_million_usd=9.00,
    ),
    # Gemini 3.6 / 3.7 / 3.8 Flash: introductory $0.75 input / $3.75 output through 2026-12-31
    # (standard $1.50 / $7.50 from 2027-01-01). Cached input uses the published $0.15 rate.
    **{
        name: ModelRateCard(
            model_name=name,
            input_per_million_usd=0.75,
            cached_read_per_million_usd=0.15,
            cache_write_per_million_usd=0.75,
            output_per_million_usd=3.75,
        )
        for name in ('gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash')
    },
    'gemini-1.5-flash': ModelRateCard(
        model_name='gemini-1.5-flash',
        input_per_million_usd=0.075,
        cached_read_per_million_usd=0.01875,
        cache_write_per_million_usd=0.075,
        output_per_million_usd=0.30,
    ),
    'gemini-1.5-pro': ModelRateCard(
        model_name='gemini-1.5-pro',
        input_per_million_usd=1.25,
        cached_read_per_million_usd=0.3125,
        cache_write_per_million_usd=1.25,
        output_per_million_usd=5.00,
    ),
}

MODEL_RATE_CARDS = RATE_CARDS


@dataclasses.dataclass(frozen=True)
class TurnUsageLog:
  """Structured log record for a single agent turn parsed from JSONL."""

  timestamp: str
  agent_name: str
  model: str
  turn_index: int
  prompt_prefix_hash: str
  cache_breakpoint_line: int | None
  cache_breakpoint_reason: str
  prompt_token_count: int
  cached_content_token_count: int
  cache_creation_input_tokens: int
  uncached_input_tokens: int
  candidates_token_count: int
  thoughts_token_count: int
  status_code: int
  tool_called: str
  evolution_generation: int

  @property
  def cache_hit_ratio(self) -> float:
    """Returns the percentage of input tokens served from prompt cache."""
    if self.prompt_token_count <= 0:
      return 0.0
    return round(
        (self.cached_content_token_count / self.prompt_token_count) * 100.0,
        2,
    )

  def compute_costs(self) -> tuple[float, float, float]:
    """Computes (naive_raw_usd, actual_log_cached_usd, net_savings_usd)."""
    card = RATE_CARDS.get(self.model, RATE_CARDS['gemini-3.1-flash'])
    total_out = self.candidates_token_count + self.thoughts_token_count
    naive_in_usd = (self.prompt_token_count / 1_000_000.0) * (
        card.input_per_million_usd
    )
    out_usd = (total_out / 1_000_000.0) * card.output_per_million_usd
    naive_total = naive_in_usd + out_usd

    cached_usd = (self.cached_content_token_count / 1_000_000.0) * (
        card.cached_read_per_million_usd
    )
    write_usd = (self.cache_creation_input_tokens / 1_000_000.0) * (
        card.cache_write_per_million_usd
    )
    uncached_usd = (self.uncached_input_tokens / 1_000_000.0) * (
        card.input_per_million_usd
    )
    actual_total = cached_usd + write_usd + uncached_usd + out_usd
    savings = max(0.0, naive_total - actual_total)
    return (
        round(naive_total, 6),
        round(actual_total, 6),
        round(savings, 6),
    )

  def to_dict(self) -> dict[str, object]:
    """Serializes the turn log with billing attribution to a dictionary."""
    naive_usd, actual_usd, saved_usd = self.compute_costs()
    return {
        'timestamp': self.timestamp,
        'agent_name': self.agent_name,
        'model': self.model,
        'turn_index': self.turn_index,
        'prompt_prefix_hash': self.prompt_prefix_hash,
        'cache_breakpoint_line': self.cache_breakpoint_line,
        'cache_breakpoint_reason': self.cache_breakpoint_reason,
        'cache_hit_ratio': self.cache_hit_ratio,
        'tool_called': self.tool_called,
        'evolution_generation': self.evolution_generation,
        'status_code': self.status_code,
        'usage_metadata': {
            'prompt_token_count': self.prompt_token_count,
            'cached_content_token_count': self.cached_content_token_count,
            'cache_creation_input_tokens': self.cache_creation_input_tokens,
            'uncached_input_tokens': self.uncached_input_tokens,
            'candidates_token_count': self.candidates_token_count,
            'thoughts_token_count': self.thoughts_token_count,
        },
        'billing_attribution': {
            'naive_count_tokens_usd': naive_usd,
            'actual_log_cached_usd': actual_usd,
            'net_savings_usd': saved_usd,
        },
    }

  def to_jsonl(self) -> str:
    """Serializes the turn log record as a compact JSONL line."""
    return json.dumps(self.to_dict(), sort_keys=True)


def detect_prefix_breakpoint(
    previous_lines: Sequence[str],
    current_lines: Sequence[str],
) -> tuple[int | None, str, str]:
  """Identifies the exact 1-based line where static prompt caching broke.

  Args:
    previous_lines: Prompt lines from turn N-1.
    current_lines: Prompt lines from turn N.

  Returns:
    Tuple of (breakpoint_line, reason, prefix_hash).
  """
  matched_lines: list[str] = []
  limit = min(len(previous_lines), len(current_lines))
  for idx in range(limit):
    if previous_lines[idx] != current_lines[idx]:
      digest = hashlib.sha256(
          '\n'.join(matched_lines).encode('utf-8')
      ).hexdigest()[:10]
      reason = (
          f'Line {idx + 1} mutated from {previous_lines[idx][:28]!r} '
          f'to {current_lines[idx][:28]!r}'
      )
      return idx + 1, reason, digest
    matched_lines.append(current_lines[idx])

  digest = hashlib.sha256('\n'.join(matched_lines).encode('utf-8')).hexdigest()[
      :10
  ]
  return None, '100% static prefix match across turns', digest


def summarize_log_stream(
    turns: Sequence[TurnUsageLog],
) -> Mapping[str, float | int]:
  """Aggregates log stream metrics for the VibeLift Telemetry tab."""
  if not turns:
    return {
        'total_turns': 0,
        'avg_cache_hit_ratio': 0.0,
        'total_naive_usd': 0.0,
        'total_actual_usd': 0.0,
        'total_saved_usd': 0.0,
        'error_rate_pct': 0.0,
    }

  total_prompt = sum(t.prompt_token_count for t in turns)
  total_cached = sum(t.cached_content_token_count for t in turns)
  naive_sum = 0.0
  actual_sum = 0.0
  saved_sum = 0.0
  errors = 0
  for turn in turns:
    n_cost, a_cost, s_cost = turn.compute_costs()
    naive_sum += n_cost
    actual_sum += a_cost
    saved_sum += s_cost
    if turn.status_code >= 400:
      errors += 1

  hit_ratio = (
      round((total_cached / total_prompt) * 100.0, 2) if total_prompt else 0.0
  )
  err_pct = round((errors / len(turns)) * 100.0, 2)
  return {
      'total_turns': len(turns),
      'avg_cache_hit_ratio': hit_ratio,
      'total_naive_usd': round(naive_sum, 4),
      'total_actual_usd': round(actual_sum, 4),
      'total_saved_usd': round(saved_sum, 4),
      'error_rate_pct': err_pct,
  }