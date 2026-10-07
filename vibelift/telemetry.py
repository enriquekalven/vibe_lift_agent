"""Log-based token cache economics, pricing, and prompt breakpoint analyzer."""

import dataclasses
import datetime
import functools
import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_LOG = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger('vibelift.audit_sink')
_STATE_FILE_LOCK = threading.Lock()


def emit_structured_audit_record(kind: str, record: Mapping[str, Any], level: int = logging.INFO) -> None:
  """Emits a structured JSON audit record to Cloud Logging and optional VIBELIFT_STATE_DIR JSONL."""
  payload = {'vibelift_event_kind': kind, **dict(record)}
  try:
    line = json.dumps(payload, sort_keys=True)
    _AUDIT_LOGGER.log(level, line)
  except Exception:
    return
  state_dir = (os.environ.get('VIBELIFT_STATE_DIR') or '').strip()
  if state_dir:
    try:
      os.makedirs(state_dir, exist_ok=True)
      safe_kind = ''.join(c for c in kind if c.isalnum() or c in ('_', '-')) or 'events'
      target_path = os.path.join(state_dir, f'{safe_kind}.jsonl')
      with _STATE_FILE_LOCK:
        with open(target_path, 'a', encoding='utf-8') as fh:
          fh.write(line + '\n')
    except OSError:
      pass



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
    # Gemini 3.6 / 3.7 / 3.8 Flash, global endpoint: introductory $0.75 input / $0.075 cached input /
    # $3.75 output through 2026-12-31 (standard $1.50 / $0.15 / $7.50 from 2027-01-01). Checked
    # 2026-10-06 against cloud.google.com/vertex-ai/generative-ai/pricing.
    **{
        name: ModelRateCard(
            model_name=name,
            input_per_million_usd=0.75,
            cached_read_per_million_usd=0.075,
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
    'gemini-3.5-pro': ModelRateCard(
        model_name='gemini-3.5-pro',
        input_per_million_usd=2.50,
        cached_read_per_million_usd=0.25,
        cache_write_per_million_usd=2.50,
        output_per_million_usd=15.00,
    ),
    # Anthropic Claude Opus on Vertex AI Model Garden (claude-opus-5-5 / claude-opus-4-6):
    # $5.00/1M input, $25.00/1M output, $0.50/1M prompt cache read, $6.25/1M cache write.
    'claude-opus-5-5': ModelRateCard(
        model_name='claude-opus-5-5',
        input_per_million_usd=5.00,
        cached_read_per_million_usd=0.50,
        cache_write_per_million_usd=6.25,
        output_per_million_usd=25.00,
    ),
    'claude-opus-4-6': ModelRateCard(
        model_name='claude-opus-4-6',
        input_per_million_usd=5.00,
        cached_read_per_million_usd=0.50,
        cache_write_per_million_usd=6.25,
        output_per_million_usd=25.00,
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
  # True for turns made up by the built-in simulator (vibelift/long_running_agent.py), never for logged turns.
  simulated: bool = False

  @property
  def cache_hit_ratio(self) -> float:
    """Returns the percentage of input tokens served from prompt cache."""
    if self.prompt_token_count <= 0:
      return 0.0
    return round(
        (self.cached_content_token_count / self.prompt_token_count) * 100.0,
        2,
    )

  @property
  def rate_card(self) -> ModelRateCard | None:
    """The model's own rate card ('publishers/google/models/x' uses card 'x'), or None. No fallback card."""
    name = str(self.model or '').strip().rsplit('/', 1)[-1]
    return RATE_CARDS.get(name) if name else None

  @property
  def priced(self) -> bool:
    """True when the turn's model has a rate card, so its tokens can be priced."""
    return self.rate_card is not None

  def compute_costs(self) -> tuple[float | None, float | None, float | None]:
    """Computes (naive_raw_usd, actual_log_cached_usd, net_savings_usd) at list price.

    A model without a rate card is left unpriced: all three values are None. It is never
    priced with another model's card.
    """
    card = self.rate_card
    if card is None:
      return None, None, None
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

  def to_dict(self) -> dict[str, Any]:
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
        'simulated': self.simulated,
        'usage_metadata': {
            'prompt_token_count': self.prompt_token_count,
            'cached_content_token_count': self.cached_content_token_count,
            'cache_creation_input_tokens': self.cache_creation_input_tokens,
            'uncached_input_tokens': self.uncached_input_tokens,
            'candidates_token_count': self.candidates_token_count,
            'thoughts_token_count': self.thoughts_token_count,
        },
        'billing_attribution': {
            'priced': naive_usd is not None,
            'rate_card': self.rate_card.model_name if self.rate_card else None,
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
  """Aggregates log stream metrics for the VibeLift Telemetry tab.

  Dollar totals cover priced turns only. Turns whose model has no rate card are counted in
  unpriced_turns and add nothing to the dollar totals.
  """
  if not turns:
    return {
        'total_turns': 0,
        'priced_turns': 0,
        'unpriced_turns': 0,
        'avg_cache_hit_ratio': 0.0,
        'total_prompt_tokens': 0,
        'total_cached_read_tokens': 0,
        'total_uncached_input_tokens': 0,
        'total_thoughts_tokens': 0,
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
  priced = 0
  errors = 0
  for turn in turns:
    n_cost, a_cost, s_cost = turn.compute_costs()
    if n_cost is not None and a_cost is not None and s_cost is not None:
      priced += 1
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
      'priced_turns': priced,
      'unpriced_turns': len(turns) - priced,
      'avg_cache_hit_ratio': hit_ratio,
      'total_prompt_tokens': total_prompt,
      'total_cached_read_tokens': total_cached,
      'total_uncached_input_tokens': sum(t.uncached_input_tokens for t in turns),
      'total_thoughts_tokens': sum(t.thoughts_token_count for t in turns),
      'total_naive_usd': round(naive_sum, 4),
      'total_actual_usd': round(actual_sum, 4),
      'total_saved_usd': round(saved_sum, 4),
      'error_rate_pct': err_pct,
  }


# ---------------------------------------------------------------------------
# Real-Time Decorator-Based Telemetry Framework (@vibelift_telemetry)
# Aligned in Sep 22, 2026 VibeLift Weekly Sync: connects directly to agent
# message-passing protocols (MCP / A2A / ADK) to avoid BigQuery log router delay.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DecoratorTelemetryEvent:
  """One event from the @vibelift_telemetry decorator or POST /api/decorator_ingest.

  Anything the caller did not report stays None; nothing is imputed.
  """

  timestamp: str
  agent_name: str | None
  handler_name: str | None
  protocol: str | None
  model: str | None
  latency_ms: float | None
  prompt_tokens: int | None
  cached_tokens: int | None
  output_tokens: int | None
  context_bloat_pct: float | None
  idle_ratio_pct: float | None
  skill_or_mcp: str | None
  user_cohort: str | None
  status: str | None

  def to_dict(self) -> dict[str, Any]:
    """Serializes the event for the dashboard. Unknown measurements are None."""
    prompt_tok = _opt_count(self.prompt_tokens)
    cached_tok = _opt_count(self.cached_tokens)
    if prompt_tok is not None and cached_tok is not None:
      cached_tok = min(prompt_tok, cached_tok)
    # Undefined (None) when prompt tokens are unknown or zero.
    cache_hit_pct = (
        round(min(100.0, (cached_tok / prompt_tok) * 100.0), 1)
        if prompt_tok and cached_tok is not None
        else None
    )
    return {
        'timestamp': self.timestamp,
        'agent_name': self.agent_name,
        'handler_name': self.handler_name,
        'protocol': self.protocol,
        'model': self.model,
        'latency_ms': None if self.latency_ms is None else round(max(0.0, float(self.latency_ms)), 1),
        'prompt_tokens': prompt_tok,
        'cached_tokens': cached_tok,
        'output_tokens': _opt_count(self.output_tokens),
        'cache_hit_pct': cache_hit_pct,
        'context_bloat_pct': _opt_pct(self.context_bloat_pct),
        'idle_ratio_pct': _opt_pct(self.idle_ratio_pct),
        'skill_or_mcp': self.skill_or_mcp,
        'user_cohort': self.user_cohort,
        'status': self.status,
    }


def _opt_count(value: Any) -> int | None:
  """Non-negative int, or None when unknown."""
  return None if value is None else max(0, int(value))


def _opt_pct(value: Any) -> float | None:
  """Percentage clamped to 0-100 and rounded to 0.1, or None when unknown."""
  return None if value is None else round(max(0.0, min(100.0, float(value))), 1)


def _utc_now_iso() -> str:
  """Current UTC time as an ISO-8601 string with a Z suffix."""
  return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


_SEED_DECORATOR_EVENTS: tuple[DecoratorTelemetryEvent, ...] = (
    DecoratorTelemetryEvent(
        timestamp='demo seed',
        agent_name='it_service_desk',
        handler_name='handle_tier2_escalation',
        protocol='ADK / A2A Message Passing',
        model='gemini-2.5-flash',
        latency_ms=640.0,
        prompt_tokens=18400,
        cached_tokens=16800,
        output_tokens=310,
        context_bloat_pct=14.2,
        idle_ratio_pct=8.4,
        skill_or_mcp='mcp://service-desk-escalation',
        user_cohort='Enterprise IT Support (2,840 DAU)',
        status='200 OK (Prefix Cached)',
    ),
    DecoratorTelemetryEvent(
        timestamp='demo seed',
        agent_name='vibelift_analytics',
        handler_name='open_dashboard',
        protocol='Streamable HTTP MCP',
        model='gemini-2.5-flash',
        latency_ms=8.6,
        prompt_tokens=22100,
        cached_tokens=19890,
        output_tokens=420,
        context_bloat_pct=11.8,
        idle_ratio_pct=5.2,
        skill_or_mcp='mcp://vibelift-analytics/dashboard',
        user_cohort='Cloud Platform & FinOps Leads (1,650 DAU)',
        status='200 OK (Warm Snapshot)',
    ),
    DecoratorTelemetryEvent(
        timestamp='demo seed',
        agent_name='deep_research',
        handler_name='synthesize_cited_brief',
        protocol='A2A Subagent Mesh',
        model='gemini-2.5-pro',
        latency_ms=725.0,
        prompt_tokens=31200,
        cached_tokens=29050,
        output_tokens=890,
        context_bloat_pct=16.5,
        idle_ratio_pct=9.1,
        skill_or_mcp='skill://multi-hop-citation-dedup',
        user_cohort='Product & Executive Research (2,350 DAU)',
        status='200 OK (Deduplicated)',
    ),
)

_DECORATOR_LOCK = threading.Lock()
_DECORATOR_EVENTS: list[DecoratorTelemetryEvent] = list(_SEED_DECORATOR_EVENTS)
_RUNTIME_DECORATOR_EVENTS: list[DecoratorTelemetryEvent] = []
_LIVE_GCP_DECORATOR_EVENTS: list[dict[str, Any]] | None = None
_INGEST_DLQ: list[dict[str, Any]] = []
_INGEST_DLQ_MAX = 50


def set_live_decorator_events(events: Sequence[Mapping[str, Any]] | None) -> None:
  """Sets live GCP telemetry events (BigQuery / Cloud Logging) to replace seed decorator rows."""
  global _LIVE_GCP_DECORATOR_EVENTS
  with _DECORATOR_LOCK:
    _LIVE_GCP_DECORATOR_EVENTS = [dict(e) for e in events] if events is not None else None


# ---------------------------------------------------------------------------
# Ingest validation and dead-letter list
# POST /api/decorator_ingest, /api/aive_log and /api/csat_rating validate their
# bodies here. Invalid bodies are rejected (HTTP 422) and recorded in a bounded
# dead-letter list; fields a caller leaves out stay None (no defaults).
# ---------------------------------------------------------------------------

_MAX_TOKENS_PER_EVENT = 100_000_000
_MAX_LATENCY_MS = 86_400_000.0  # 24 h
_MAX_PROMPT_CHARS = 1_000_000
_DECORATOR_MEASUREMENTS = (
    'latency_ms', 'prompt_tokens', 'cached_tokens', 'output_tokens', 'context_bloat_pct', 'idle_ratio_pct',
)
_AIVE_MEASUREMENTS = ('total_tokens', 'prompt_tokens', 'output_tokens', 'latency_ms')


class IngestValidationError(ValueError):
  """An ingest body failed validation. Carries the errors and the dead-letter record."""

  def __init__(self, errors: Sequence[str], dead_letter: Mapping[str, Any]) -> None:
    super().__init__('; '.join(errors))
    self.errors = list(errors)
    self.dead_letter = dict(dead_letter)

  def to_response(self) -> dict[str, Any]:
    """Body of the HTTP 422 response."""
    return {'error': 'invalid_payload', 'errors': self.errors, 'dead_letter': self.dead_letter}


def record_ingest_dlq(source: str, payload: Any, errors: Sequence[str]) -> dict[str, Any]:
  """Records a rejected ingest body in the bounded dead-letter list and as a WARNING log line.

  Only the body's key names, size and SHA-256 are kept, never its values, because rejected bodies
  can carry prompt text or e-mail addresses. Nothing is retried or replayed automatically.
  """
  try:
    canonical = json.dumps(payload, sort_keys=True, default=str)
  except (TypeError, ValueError):
    canonical = repr(payload)
  encoded = canonical.encode('utf-8', errors='replace')
  entry: dict[str, Any] = {
      'timestamp': _utc_now_iso(),
      'source': str(source)[:80],
      'errors': [str(e)[:200] for e in errors][:20],
      'payload_type': type(payload).__name__,
      'payload_keys': sorted(str(k)[:64] for k in payload)[:40] if isinstance(payload, Mapping) else [],
      'payload_bytes': len(encoded),
      'payload_sha256': hashlib.sha256(encoded).hexdigest(),
  }
  with _DECORATOR_LOCK:
    _INGEST_DLQ.insert(0, entry)
    del _INGEST_DLQ[_INGEST_DLQ_MAX:]
  emit_structured_audit_record('ingest_dead_letter', entry, level=logging.WARNING)
  return entry


def get_ingest_dlq_events() -> list[dict[str, Any]]:
  """Returns recently rejected ingest bodies, newest first."""
  with _DECORATOR_LOCK:
    return [dict(x) for x in _INGEST_DLQ]


def raise_if_invalid(source: str, payload: Any, errors: Sequence[str]) -> None:
  """Records a dead-letter entry and raises IngestValidationError when `errors` is not empty."""
  if errors:
    raise IngestValidationError(errors, record_ingest_dlq(source, payload, errors))


def _has_value(raw: Mapping[str, Any], key: str) -> bool:
  return raw.get(key) not in (None, '')


def _field_str(
    raw: Mapping[str, Any], key: str, errors: list[str], *, max_len: int, required: bool = False
) -> str | None:
  """Optional (or required) trimmed string field; type and length are checked, never truncated."""
  value = raw.get(key)
  if value is None or (isinstance(value, str) and not value.strip()):
    if required:
      errors.append(f'{key}: required')
    return None
  if not isinstance(value, str):
    errors.append(f'{key}: must be a string')
    return None
  value = value.strip()
  if len(value) > max_len:
    errors.append(f'{key}: longer than {max_len} characters')
    return None
  return value


def _field_num(
    raw: Mapping[str, Any], key: str, errors: list[str], *, integer: bool = False, max_value: float | None = None
) -> Any:
  """Optional non-negative number (numeric strings accepted). Returns None when absent or invalid."""
  value = raw.get(key)
  if value is None or (isinstance(value, str) and not value.strip()):
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float, str)):
    errors.append(f'{key}: must be a number')
    return None
  try:
    num = float(value)
  except ValueError:
    errors.append(f'{key}: must be a number')
    return None
  if not math.isfinite(num) or num < 0:
    errors.append(f'{key}: must be a finite number >= 0')
    return None
  if max_value is not None and num > max_value:
    errors.append(f'{key}: must be <= {max_value:,.0f}')
    return None
  if integer:
    if not num.is_integer():
      errors.append(f'{key}: must be a whole number')
      return None
    return int(num)
  return num


def _field_tokens(raw: Mapping[str, Any], key: str, errors: list[str]) -> int | None:
  return _field_num(raw, key, errors, integer=True, max_value=_MAX_TOKENS_PER_EVENT)


def _check_cached_le_prompt(fields: Mapping[str, Any], errors: list[str]) -> None:
  prompt_tok, cached_tok = fields.get('prompt_tokens'), fields.get('cached_tokens')
  if prompt_tok is not None and cached_tok is not None and cached_tok > prompt_tok:
    errors.append('cached_tokens: cannot exceed prompt_tokens')


def _require_measurement(raw: Mapping[str, Any], keys: Sequence[str], errors: list[str]) -> None:
  if not any(_has_value(raw, k) for k in keys):
    errors.append('no measurements: send at least one of ' + ', '.join(keys))


def validate_decorator_payload(raw: Any) -> tuple[dict[str, Any], list[str]]:
  """Validates a POST /api/decorator_ingest body. Returns (DecoratorTelemetryEvent fields, errors)."""
  if not isinstance(raw, Mapping):
    return {}, ['body: must be a JSON object']
  errors: list[str] = []
  fields: dict[str, Any] = {
      'timestamp': _field_str(raw, 'timestamp', errors, max_len=64) or time.strftime('%H:%M:%S UTC', time.gmtime()),
      'agent_name': _field_str(raw, 'agent_name', errors, max_len=128, required=True),
      'handler_name': _field_str(raw, 'handler_name', errors, max_len=128),
      'protocol': _field_str(raw, 'protocol', errors, max_len=80),
      'model': _field_str(raw, 'model', errors, max_len=128),
      'latency_ms': _field_num(raw, 'latency_ms', errors, max_value=_MAX_LATENCY_MS),
      'prompt_tokens': _field_tokens(raw, 'prompt_tokens', errors),
      'cached_tokens': _field_tokens(raw, 'cached_tokens', errors),
      'output_tokens': _field_tokens(raw, 'output_tokens', errors),
      'context_bloat_pct': _field_num(raw, 'context_bloat_pct', errors, max_value=100.0),
      'idle_ratio_pct': _field_num(raw, 'idle_ratio_pct', errors, max_value=100.0),
      'skill_or_mcp': _field_str(raw, 'skill_or_mcp', errors, max_len=256),
      'user_cohort': _field_str(raw, 'user_cohort', errors, max_len=128),
      'status': _field_str(raw, 'status', errors, max_len=64),
  }
  _check_cached_le_prompt(fields, errors)
  _require_measurement(raw, _DECORATOR_MEASUREMENTS, errors)
  return fields, errors


def _field_prompts(raw: Mapping[str, Any], errors: list[str]) -> list[str]:
  """Collects `prompt` (string) and `prompts` (list of strings). Used only for fingerprints."""
  items: list[str] = []
  single = raw.get('prompt')
  if single not in (None, ''):
    if isinstance(single, str):
      items.append(single)
    else:
      errors.append('prompt: must be a string')
  many = raw.get('prompts')
  if many not in (None, []):
    if isinstance(many, list) and len(many) <= 100 and all(isinstance(p, str) for p in many):
      items.extend(many)
    else:
      errors.append('prompts: must be a list of at most 100 strings')
  if sum(len(p) for p in items) > _MAX_PROMPT_CHARS:
    errors.append(f'prompt: longer than {_MAX_PROMPT_CHARS:,} characters in total')
    return []
  return items


def _field_outputs(raw: Mapping[str, Any], errors: list[str]) -> list[dict[str, Any]]:
  """Collects `outputs` (list of {gcs_uri, media_type, mime_type}) or a top-level `gcs_uri`."""
  entries: list[Mapping[str, Any]] = []
  outs = raw.get('outputs')
  if outs not in (None, []):
    if isinstance(outs, list) and len(outs) <= 20 and all(isinstance(o, Mapping) for o in outs):
      entries.extend(outs)
    else:
      errors.append('outputs: must be a list of at most 20 objects')
  if _has_value(raw, 'gcs_uri'):
    entries.append({k: raw.get(k) for k in ('gcs_uri', 'media_type', 'mime_type')})
  cleaned: list[dict[str, Any]] = []
  for i, out in enumerate(entries):
    sub: list[str] = []
    uri = _field_str(out, 'gcs_uri', sub, max_len=1024, required=True)
    media = _field_str(out, 'media_type', sub, max_len=64)
    mime = _field_str(out, 'mime_type', sub, max_len=128)
    if uri is not None and not uri.startswith('gs://'):
      sub.append('gcs_uri: must start with gs://')
    errors.extend(f'outputs[{i}].{e}' for e in sub)
    if not sub:
      cleaned.append({'gcs_uri': uri, 'media_type': media, 'mime_type': mime})
  return cleaned


def validate_aive_payload(raw: Any) -> tuple[dict[str, Any], list[str]]:
  """Validates a POST /api/aive_log body. Returns (log_agent_generation_event kwargs, errors)."""
  if not isinstance(raw, Mapping):
    return {}, ['body: must be a JSON object']
  errors: list[str] = []
  fields: dict[str, Any] = {
      'user_email': _field_str(raw, 'user_email', errors, max_len=254, required=True),
      'task_type': _field_str(raw, 'task_type', errors, max_len=64, required=True),
      'session_id': _field_str(raw, 'session_id', errors, max_len=128),
      'company_name': _field_str(raw, 'company_name', errors, max_len=128),
      'department': _field_str(raw, 'department', errors, max_len=128),
      'model_name': _field_str(raw, 'model_name', errors, max_len=128),
      'agent_name': _field_str(raw, 'agent_name', errors, max_len=128),
      'status': _field_str(raw, 'status', errors, max_len=32),
      'error_message': _field_str(raw, 'error_message', errors, max_len=500),
      'total_tokens': _field_tokens(raw, 'total_tokens', errors),
      'prompt_tokens': _field_tokens(raw, 'prompt_tokens', errors),
      'cached_tokens': _field_tokens(raw, 'cached_tokens', errors),
      'output_tokens': _field_tokens(raw, 'output_tokens', errors),
      'thinking_tokens': _field_tokens(raw, 'thinking_tokens', errors),
      'background_tokens': _field_tokens(raw, 'background_tokens', errors),
      'latency_ms': _field_num(raw, 'latency_ms', errors, max_value=_MAX_LATENCY_MS),
      'prompts': _field_prompts(raw, errors),
      'outputs': _field_outputs(raw, errors),
  }
  _check_cached_le_prompt(fields, errors)
  _require_measurement(raw, _AIVE_MEASUREMENTS, errors)
  return fields, errors


def validate_csat_payload(raw: Any) -> tuple[dict[str, Any], list[str]]:
  """Validates a POST /api/csat_rating body. Returns (log_csat_rating kwargs, errors)."""
  if not isinstance(raw, Mapping):
    return {}, ['body: must be a JSON object']
  errors: list[str] = []
  rating_errors: list[str] = []
  rating = _field_num(raw, 'rating', rating_errors, integer=True)
  if rating is None or not 1 <= rating <= 5:
    errors.append('rating: required, a whole number from 1 to 5')
    rating = None
  fields: dict[str, Any] = {
      'rating': rating,
      'session_id': _field_str(raw, 'session_id', errors, max_len=128),
      'event_id': _field_str(raw, 'event_id', errors, max_len=128),
      'user_email': _field_str(raw, 'user_email', errors, max_len=254),
      'feedback_text': _field_str(raw, 'feedback_text', errors, max_len=2000),
  }
  if not _has_value(raw, 'session_id') and not _has_value(raw, 'event_id'):
    errors.append('session_id or event_id: required (the session or event being rated)')
  return fields, errors


def record_decorator_event(event: DecoratorTelemetryEvent) -> dict[str, Any]:
  """Appends a real-time decorator telemetry event to the in-memory stream."""
  with _DECORATOR_LOCK:
    _RUNTIME_DECORATOR_EVENTS.insert(0, event)
    del _RUNTIME_DECORATOR_EVENTS[25:]
    _DECORATOR_EVENTS.insert(0, event)
    del _DECORATOR_EVENTS[25:]
  out = event.to_dict()
  emit_structured_audit_record('decorator_event', out)
  return out


def get_recent_decorator_events() -> list[dict[str, Any]]:
  """Returns recent real-time @vibelift_telemetry decorator events."""
  with _DECORATOR_LOCK:
    if _LIVE_GCP_DECORATOR_EVENTS is not None:
      return [e.to_dict() for e in _RUNTIME_DECORATOR_EVENTS] + [dict(x) for x in _LIVE_GCP_DECORATOR_EVENTS]
    snapshot = list(_DECORATOR_EVENTS)
  return [e.to_dict() for e in snapshot]


def reset_decorator_events() -> None:
  """Resets the in-memory decorator event stream and dead-letter list back to seed state."""
  global _LIVE_GCP_DECORATOR_EVENTS
  with _DECORATOR_LOCK:
    _RUNTIME_DECORATOR_EVENTS.clear()
    _INGEST_DLQ.clear()
    _LIVE_GCP_DECORATOR_EVENTS = None
    _DECORATOR_EVENTS[:] = list(_SEED_DECORATOR_EVENTS)


# Token-usage field names: VibeLift / OpenTelemetry-style keys first, then google-genai
# `usage_metadata` names (prompt_token_count already includes cached tokens).
_USAGE_FIELDS: dict[str, tuple[str, ...]] = {
    'prompt_tokens': ('prompt_tokens', 'prompt_token_count'),
    'cached_tokens': ('cached_tokens', 'cached_content_token_count'),
    'output_tokens': ('output_tokens', 'candidates_token_count'),
    'thinking_tokens': ('thinking_tokens', 'thoughts_token_count'),
    'total_tokens': ('total_tokens', 'total_token_count'),
}


def _get_field(obj: Any, key: str) -> Any:
  """Reads `key` from a mapping or an object attribute (e.g. a google-genai response)."""
  if obj is None:
    return None
  if isinstance(obj, Mapping):
    return obj.get(key)
  return getattr(obj, key, None)


def _usage_source(result: Any) -> Any:
  meta = _get_field(result, 'usage_metadata')
  return meta if meta is not None else result


def _count_from(source: Any, names: Sequence[str]) -> int | None:
  """First present count among `names`; None if absent, negative or not a number."""
  for name in names:
    value = _get_field(source, name)
    if value is None:
      continue
    if isinstance(value, bool):
      return None
    try:
      num = int(value)
    except (TypeError, ValueError):
      return None
    return num if num >= 0 else None
  return None


def _pct_from(source: Any, key: str) -> float | None:
  value = _get_field(source, key)
  if value is None or isinstance(value, bool):
    return None
  try:
    num = float(value)
  except (TypeError, ValueError):
    return None
  return num if math.isfinite(num) and 0.0 <= num <= 100.0 else None


def _usage_from_result(result: Any) -> dict[str, Any]:
  """Token usage reported by a handler result; anything missing or malformed is None."""
  source = _usage_source(result)
  usage: dict[str, Any] = {key: _count_from(source, names) for key, names in _USAGE_FIELDS.items()}
  if usage['prompt_tokens'] is not None and usage['cached_tokens'] is not None:
    usage['cached_tokens'] = min(usage['cached_tokens'], usage['prompt_tokens'])
  model = _get_field(result, 'model_version') or _get_field(result, 'model')
  usage['model'] = model[:128] if isinstance(model, str) and model else None
  return usage


def _extract_result_metrics(result: Any) -> tuple[int | None, int | None, int | None, float | None, float | None]:
  """Token, context-bloat and idle metrics reported by a handler result; missing values are None."""
  usage = _usage_from_result(result)
  source = _usage_source(result)
  return (
      usage['prompt_tokens'],
      usage['cached_tokens'],
      usage['output_tokens'],
      _pct_from(source, 'context_bloat_pct'),
      _pct_from(source, 'idle_ratio_pct'),
  )


def vibelift_telemetry(
    agent_name: str,
    model: str | None = None,
    protocol: str | None = None,
    skill_or_mcp: str | None = None,
    user_cohort: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """Decorator that records each call of an agent handler as an in-process telemetry event.

  Latency is measured around the call. Token counts, context bloat and idle ratio are read from the
  handler's return value when it reports them (a Gemini response's `usage_metadata`, or dict keys
  such as `prompt_tokens`); otherwise they are recorded as None. `model` defaults to the response's
  `model_version` when present.
  """

  def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
    handler_name = getattr(func, '__name__', 'agent_handler')

    def _record(result: Any, status: str, t0: float) -> None:
      try:
        p_tok, c_tok, o_tok, bloat, idle = _extract_result_metrics(result)
        record_decorator_event(
            DecoratorTelemetryEvent(
                timestamp=time.strftime('%H:%M:%S UTC', time.gmtime()),
                agent_name=agent_name,
                handler_name=handler_name,
                protocol=protocol,
                model=model or _usage_from_result(result)['model'],
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                prompt_tokens=p_tok,
                cached_tokens=c_tok,
                output_tokens=o_tok,
                context_bloat_pct=bloat,
                idle_ratio_pct=idle,
                skill_or_mcp=skill_or_mcp,
                user_cohort=user_cohort,
                status=status,
            )
        )
      except Exception:  # pylint: disable=broad-except
        # Telemetry must never break the wrapped handler.
        _LOG.warning('vibelift_telemetry could not record %s', handler_name, exc_info=True)

    if inspect.iscoroutinefunction(func):

      @functools.wraps(func)
      async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        status, result = 'OK', None
        try:
          result = await func(*args, **kwargs)
          return result
        except Exception as exc:
          status = f'ERROR ({type(exc).__name__})'
          raise
        finally:
          _record(result, status, t0)

      return _async_wrapper

    @functools.wraps(func)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
      t0 = time.perf_counter()
      status, result = 'OK', None
      try:
        result = func(*args, **kwargs)
        return result
      except Exception as exc:
        status = f'ERROR ({type(exc).__name__})'
        raise
      finally:
        _record(result, status, t0)

    return _sync_wrapper

  return _decorator


# ---------------------------------------------------------------------------
# BigQuery `aive_logs` Schema & `@with_analytics_logging` Decorator
# Implements a standardized usage-log schema (`agent_usage_log`,
# `ratings_log`, `improvement_log`) with `user_ldap` extraction and dual-write
# to the real-time `@vibelift_telemetry` stream.
# ---------------------------------------------------------------------------

_AIVE_LOGS_LOCK = threading.Lock()


def _fingerprint_prompts(prompts: Sequence[str] | None) -> dict[str, Any]:
  """Summarizes prompts without keeping their text: count, characters and one digest per prompt.

  With VIBELIFT_PROMPT_HASH_KEY set the digest is HMAC-SHA256 keyed with it, so short or common
  prompts can't be recovered by hashing guesses. Plain SHA-256 (no key) still supports dedup and
  prefix-stability checks but is not anonymization.
  """
  texts = [str(p) for p in (prompts or [])]
  key = (os.environ.get('VIBELIFT_PROMPT_HASH_KEY') or '').encode('utf-8')
  if key:
    digests = [hmac.new(key, t.encode('utf-8'), hashlib.sha256).hexdigest() for t in texts]
  else:
    digests = [hashlib.sha256(t.encode('utf-8')).hexdigest() for t in texts]
  return {
      'prompt_count': len(texts),
      'prompt_chars': sum(len(t) for t in texts),
      'prompt_sha256': digests,
      'prompt_hash': 'hmac-sha256' if key else 'sha256',
  }


def _without_prompt_text(row: Mapping[str, Any]) -> dict[str, Any]:
  """Copy of a usage row with `prompts` replaced by its fingerprint."""
  out = {k: v for k, v in row.items() if k != 'prompts'}
  out.update(_fingerprint_prompts(row.get('prompts')))
  return out


_SEED_AIVE_USAGE_LOGS: list[dict[str, Any]] = [
    {
        'event_id': 'evt-9f81c204-aive',
        'timestamp': '2026-09-28T22:15:00Z',
        'session_id': '6446120131357637190',
        'user_email': 'user-c@example.com',
        'user_ldap': 'user-c',
        'company_name': 'Example Corp',
        'department': 'AI Platform Engineering',
        'task_type': 'FLEET_OPTIMIZATION_AUDIT',
        'model_name': 'gemini-2.5-flash',
        'prompts': ['Open the VibeLift dashboard and audit 5-layer OTel metrics'],
        'outputs': [{'gcs_uri': 'gs://vibelift-demo-assets/reports/otel_5layer_audit.json', 'media_type': 'APPLICATION_JSON', 'mime_type': 'application/json'}],
        'latency_ms': 640.0,
        'total_tokens': 18710,
        'thinking_tokens': 1420,
        'background_tokens': 2100,
        'status': 'SUCCESS',
        'error_message': None,
        'csat_rating': 5,
    },
    {
        'event_id': 'evt-7b42e911-aive',
        'timestamp': '2026-09-28T22:18:30Z',
        'session_id': '8821150342449201152',
        'user_email': 'user-b@example.com',
        'user_ldap': 'user-b',
        'company_name': 'Example Corp',
        'department': 'Creative & Multimodal Agents',
        'task_type': 'VIDEO_GENERATION',
        'model_name': 'gemini-2.5-pro',
        'prompts': ['Synthesize product launch storyboard and Veo scene prompts'],
        'outputs': [{'gcs_uri': 'gs://vibelift-demo-assets/veo_launch_v3.mp4', 'media_type': 'VIDEO', 'mime_type': 'video/mp4'}],
        'latency_ms': 1120.0,
        'total_tokens': 31940,
        'thinking_tokens': 3800,
        'background_tokens': 4900,
        'status': 'SUCCESS',
        'error_message': None,
        'csat_rating': 5,
    },
    {
        'event_id': 'evt-3c19d408-aive',
        'timestamp': '2026-09-28T22:21:10Z',
        'session_id': '9854735213116306727',
        'user_email': 'user-a@example.com',
        'user_ldap': 'user-a',
        'company_name': 'Example Corp',
        'department': 'FinOps & Platform Architecture',
        'task_type': 'FINOPS_CACHE_ATTRIBUTION',
        'model_name': 'gemini-2.5-flash',
        'prompts': ['Verify prompt cache breakpoint and runaway thinking token caps'],
        'outputs': [{'gcs_uri': 'gs://vibelift-demo-assets/finops_delta_gen14.json', 'media_type': 'APPLICATION_JSON', 'mime_type': 'application/json'}],
        'latency_ms': 585.0,
        'total_tokens': 22520,
        'thinking_tokens': 960,
        'background_tokens': 1450,
        'status': 'SUCCESS',
        'error_message': None,
        'csat_rating': None,
    },
    {
        'event_id': 'evt-5a90f312-aive',
        'timestamp': '2026-09-28T22:24:45Z',
        'session_id': '13791105764600209034',
        'user_email': 'user-e@example.com',
        'user_ldap': 'user-e',
        'company_name': 'Example Corp',
        'department': 'Enterprise IT & Security Governance',
        'task_type': 'IT_TIER2_ESCALATION',
        'model_name': 'gemini-2.5-flash',
        'prompts': ['Resolve cross-project OAuth 2.0 consent & VPN runbook escalation'],
        'outputs': [{'gcs_uri': 'gs://vibelift-demo-assets/it_runbook_resolution.md', 'media_type': 'TEXT', 'mime_type': 'text/markdown'}],
        'latency_ms': 690.0,
        'total_tokens': 18710,
        'thinking_tokens': 1180,
        'background_tokens': 1820,
        'status': 'SUCCESS',
        'error_message': None,
        'csat_rating': None,
    },
    {
        'event_id': 'evt-6d12a877-aive',
        'timestamp': '2026-09-28T22:26:12Z',
        'session_id': '4419820311048291001',
        'user_email': 'user-d@example.com',
        'user_ldap': 'user-d',
        'company_name': 'Example Corp',
        'department': 'Executive Product Strategy',
        'task_type': 'MULTI_HOP_DEEP_RESEARCH',
        'model_name': 'gemini-2.5-pro',
        'prompts': ['Compile Q4 executive competitive battlecard with cited sources'],
        'outputs': [{'gcs_uri': 'gs://vibelift-demo-assets/q4_strategy_synthesis.pdf', 'media_type': 'DOCUMENT', 'mime_type': 'application/pdf'}],
        'latency_ms': 740.0,
        'total_tokens': 32090,
        'thinking_tokens': 4120,
        'background_tokens': 3600,
        'status': 'SUCCESS',
        'error_message': None,
        'csat_rating': 4,
    },
]

# Demo seed prompts are reduced to fingerprints like runtime rows, so no prompt text is ever served.
_SEED_AIVE_USAGE_LOGS = [_without_prompt_text(row) for row in _SEED_AIVE_USAGE_LOGS]
_AIVE_USAGE_LOGS: list[dict[str, Any]] = [dict(x) for x in _SEED_AIVE_USAGE_LOGS]
_SEED_AIVE_RATINGS_LOGS: list[dict[str, Any]] = [
    {
        'rating_id': 'rat-101',
        'timestamp': '2026-09-28T22:16:00Z',
        'session_id': '6446120131357637190',
        'event_id': 'evt-9f81c204-aive',
        'user_email': 'user-c@example.com',
        'user_ldap': 'user-c',
        'rating': 5,
        'feedback_text': '1-click dashboard & 5-layer OTel metrics mapped cleanly.',
    },
    {
        'rating_id': 'rat-102',
        'timestamp': '2026-09-28T22:19:00Z',
        'session_id': '8821150342449201152',
        'event_id': 'evt-7b42e911-aive',
        'user_email': 'user-b@example.com',
        'user_ldap': 'user-b',
        'rating': 5,
        'feedback_text': '@with_analytics_logging decorator captured GCS asset URIs with zero lag.',
    },
    {
        'rating_id': 'rat-103',
        'timestamp': '2026-09-28T22:28:15Z',
        'session_id': '4419820311048291001',
        'event_id': 'evt-6d12a877-aive',
        'user_email': 'user-d@example.com',
        'user_ldap': 'user-d',
        'rating': 4,
        'feedback_text': 'Deep research synthesis strong; keep thinking token budget capped at 2,048 to preserve <750ms P95.',
    },
]
_AIVE_RATINGS_LOGS: list[dict[str, Any]] = [dict(x) for x in _SEED_AIVE_RATINGS_LOGS]
_RUNTIME_AIVE_USAGE_LOGS: list[dict[str, Any]] = []
_RUNTIME_AIVE_RATINGS_LOGS: list[dict[str, Any]] = []
_LIVE_GCP_AIVE_USAGE_LOGS: list[dict[str, Any]] | None = None
_LIVE_GCP_AIVE_RATINGS_LOGS: list[dict[str, Any]] | None = None


def set_live_aive_logs(
    usage_logs: Sequence[Mapping[str, Any]] | None,
    ratings_logs: Sequence[Mapping[str, Any]] | None,
) -> None:
  """Replaces seed aive_logs with live GCP BigQuery telemetry in live mode."""
  global _LIVE_GCP_AIVE_USAGE_LOGS, _LIVE_GCP_AIVE_RATINGS_LOGS
  with _AIVE_LOGS_LOCK:
    _LIVE_GCP_AIVE_USAGE_LOGS = [dict(x) for x in usage_logs] if usage_logs is not None else None
    _LIVE_GCP_AIVE_RATINGS_LOGS = [dict(x) for x in ratings_logs] if ratings_logs is not None else None


def log_agent_generation_event(
    session_id: str | None,
    user_email: str | None,
    company_name: str | None,
    department: str | None,
    task_type: str | None,
    prompts: Sequence[str] | None,
    outputs: Sequence[Mapping[str, Any]] | None,
    total_tokens: int | None = None,
    model_name: str | None = None,
    latency_ms: float | None = None,
    status: str | None = None,
    error_message: str | None = None,
    thinking_tokens: int | None = None,
    background_tokens: int | None = None,
    agent_name: str | None = None,
    prompt_tokens: int | None = None,
    cached_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
  """Records an agent generation event in memory, in the decorator stream and in the audit log.

  Rows use the BigQuery `aive_logs.agent_usage_log` shape, but nothing here writes to BigQuery; live
  mode reads that table instead.

  Values the caller did not supply stay None: no imputed token splits, ratings or labels. Prompt
  text is reduced to a fingerprint (`_fingerprint_prompts`) before anything is stored or logged.
  """
  clean_email = (user_email or '').strip() or None
  user_ldap = (clean_email.split('@')[0] if '@' in clean_email else clean_email) if clean_email else None
  row: dict[str, Any] = {
      'event_id': f'evt-{uuid.uuid4().hex[:8]}-aive',
      'timestamp': _utc_now_iso(),
      'session_id': session_id or None,
      'user_email': clean_email,
      'user_ldap': user_ldap,
      'company_name': company_name or None,
      'department': department or None,
      'task_type': task_type or None,
      'model_name': model_name or None,
      **_fingerprint_prompts(prompts),
      'outputs': [
          {
              'gcs_uri': str(o['gcs_uri']) if o.get('gcs_uri') else None,
              'media_type': str(o['media_type']) if o.get('media_type') else None,
              'mime_type': str(o['mime_type']) if o.get('mime_type') else None,
          }
          for o in (outputs or [])
          if isinstance(o, Mapping)
      ],
      'latency_ms': None if latency_ms is None else round(max(0.0, float(latency_ms)), 1),
      'total_tokens': _opt_count(total_tokens),
      'prompt_tokens': _opt_count(prompt_tokens),
      'cached_tokens': _opt_count(cached_tokens),
      'output_tokens': _opt_count(output_tokens),
      'thinking_tokens': _opt_count(thinking_tokens),
      'background_tokens': _opt_count(background_tokens),
      'status': status or None,
      'error_message': str(error_message)[:500] if error_message else None,
      # Ratings only come from POST /api/csat_rating; nothing is assumed here.
      'csat_rating': None,
  }
  with _AIVE_LOGS_LOCK:
    _RUNTIME_AIVE_USAGE_LOGS.insert(0, row)
    del _RUNTIME_AIVE_USAGE_LOGS[30:]
    _AIVE_USAGE_LOGS.insert(0, row)
    del _AIVE_USAGE_LOGS[30:]

  # Dual-write to the in-process decorator stream with the same (possibly missing) measurements.
  cohort = ' '.join(x for x in (user_ldap, f'({department})' if department else None) if x) or None
  record_decorator_event(
      DecoratorTelemetryEvent(
          timestamp=time.strftime('%H:%M:%S UTC', time.gmtime()),
          agent_name=agent_name or None,
          handler_name=f'with_analytics_logging[{task_type}]' if task_type else 'with_analytics_logging',
          protocol='aive_logs dual-write',
          model=model_name or None,
          latency_ms=row['latency_ms'],
          prompt_tokens=row['prompt_tokens'],
          cached_tokens=row['cached_tokens'],
          output_tokens=row['output_tokens'],
          context_bloat_pct=None,
          idle_ratio_pct=None,
          skill_or_mcp=f'aive_logs://{task_type.lower()}' if task_type else None,
          user_cohort=cohort,
          status=row['status'],
      )
  )
  emit_structured_audit_record('aive_usage_log', row)
  return row


def log_csat_rating(
    session_id: str | None,
    event_id: str | None,
    user_email: str | None,
    rating: int,
    feedback_text: str | None = None,
) -> dict[str, Any]:
  """Records a user CSAT rating (a whole number from 1 to 5) in memory and in the audit log.

  Rows use the BigQuery `aive_logs.ratings_log` shape; nothing here writes to BigQuery.
  """
  value = int(rating)
  if isinstance(rating, bool) or value != rating or not 1 <= value <= 5:
    raise ValueError('rating must be a whole number from 1 to 5')
  clean_email = (user_email or '').strip() or None
  user_ldap = (clean_email.split('@')[0] if '@' in clean_email else clean_email) if clean_email else None
  entry: dict[str, Any] = {
      'rating_id': f'rat-{uuid.uuid4().hex[:6]}',
      'timestamp': _utc_now_iso(),
      'session_id': session_id or None,
      'event_id': event_id or None,
      'user_email': clean_email,
      'user_ldap': user_ldap,
      'rating': value,
      'feedback_text': feedback_text or None,
  }
  with _AIVE_LOGS_LOCK:
    _RUNTIME_AIVE_RATINGS_LOGS.insert(0, entry)
    del _RUNTIME_AIVE_RATINGS_LOGS[30:]
    _AIVE_RATINGS_LOGS.insert(0, entry)
    del _AIVE_RATINGS_LOGS[30:]
    if event_id:
      for row in _AIVE_USAGE_LOGS:
        if row.get('event_id') == event_id:
          row['csat_rating'] = value
      for row in _LIVE_GCP_AIVE_USAGE_LOGS or []:
        if row.get('event_id') == event_id:
          row['csat_rating'] = value
  emit_structured_audit_record('csat_rating', entry)
  return entry


def get_recent_aive_logs(live_only: bool = False) -> dict[str, list[dict[str, Any]]]:
  """Returns recent `aive_logs` (`agent_usage_log` and `ratings_log`) records.

  Args:
    live_only: When True (live GCP mode), never return seed/demo rows; if live
      BigQuery rows have not arrived yet, only runtime-ingested rows are returned.
  """
  with _AIVE_LOGS_LOCK:
    if live_only or _LIVE_GCP_AIVE_USAGE_LOGS is not None or _LIVE_GCP_AIVE_RATINGS_LOGS is not None:
      return {
          'usage_logs': [dict(x) for x in _RUNTIME_AIVE_USAGE_LOGS] + [dict(x) for x in (_LIVE_GCP_AIVE_USAGE_LOGS or [])],
          'ratings_logs': [dict(x) for x in _RUNTIME_AIVE_RATINGS_LOGS] + [dict(x) for x in (_LIVE_GCP_AIVE_RATINGS_LOGS or [])],
      }
    return {
        'usage_logs': [dict(x) for x in _AIVE_USAGE_LOGS],
        'ratings_logs': [dict(x) for x in _AIVE_RATINGS_LOGS],
    }


def reset_aive_logs() -> None:
  """Resets `aive_logs` in-memory stream back to seed state."""
  global _LIVE_GCP_AIVE_USAGE_LOGS, _LIVE_GCP_AIVE_RATINGS_LOGS
  with _AIVE_LOGS_LOCK:
    _RUNTIME_AIVE_USAGE_LOGS.clear()
    _RUNTIME_AIVE_RATINGS_LOGS.clear()
    _LIVE_GCP_AIVE_USAGE_LOGS = None
    _LIVE_GCP_AIVE_RATINGS_LOGS = None
    _AIVE_USAGE_LOGS[:] = [dict(x) for x in _SEED_AIVE_USAGE_LOGS]
    _AIVE_RATINGS_LOGS[:] = [dict(x) for x in _SEED_AIVE_RATINGS_LOGS]


def with_analytics_logging(
    task_type: str = 'TEXT_GENERATION',
    model_name: str | None = None,
    agent_name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """Decorator for ADK tool / generation calls that writes one `aive_logs` usage row per call.

  Token counts come from the handler's return value (a Gemini response's `usage_metadata`, or
  `total_tokens` / `prompt_tokens` / ... keys); when it reports none they stay None. Prompt text
  (the `prompt=` keyword or a leading string argument) is fingerprinted, never stored. Logging
  failures are reported as warnings and never break the wrapped call.
  """

  def _context(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    prompt_val = kwargs.get('prompt') or (args[0] if args and isinstance(args[0], str) else None)

    def _kw(key: str) -> str | None:
      value = kwargs.get(key)
      return str(value) if value not in (None, '') else None

    return {
        'session_id': _kw('session_id'),
        'user_email': _kw('user_email'),
        'company_name': _kw('company_name'),
        'department': _kw('department'),
        'prompts': [str(prompt_val)] if prompt_val else [],
    }

  def _log(ctx: Mapping[str, Any], result: Any, status: str, error_message: str | None, t0: float) -> None:
    try:
      usage = _usage_from_result(result)
      outputs: list[dict[str, Any]] = []
      if isinstance(result, Mapping) and result.get('gcs_uri'):
        outputs.append({k: result.get(k) for k in ('gcs_uri', 'media_type', 'mime_type')})
      log_agent_generation_event(
          task_type=task_type,
          outputs=outputs,
          total_tokens=usage['total_tokens'],
          prompt_tokens=usage['prompt_tokens'],
          cached_tokens=usage['cached_tokens'],
          output_tokens=usage['output_tokens'],
          thinking_tokens=usage['thinking_tokens'],
          model_name=model_name or usage['model'],
          latency_ms=(time.perf_counter() - t0) * 1000.0,
          status=status,
          error_message=error_message,
          agent_name=agent_name,
          **ctx,
      )
    except Exception:  # pylint: disable=broad-except
      _LOG.warning('with_analytics_logging could not record a usage row', exc_info=True)

  def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):

      @functools.wraps(func)
      async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        ctx = _context(args, kwargs)
        result: Any = None
        status, error_message = 'SUCCESS', None
        try:
          result = await func(*args, **kwargs)
          return result
        except Exception as exc:
          status, error_message = 'FAILED', str(exc)
          raise
        finally:
          _log(ctx, result, status, error_message, t0)

      return _async_wrapper

    @functools.wraps(func)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
      t0 = time.perf_counter()
      ctx = _context(args, kwargs)
      result: Any = None
      status, error_message = 'SUCCESS', None
      try:
        result = func(*args, **kwargs)
        return result
      except Exception as exc:
        status, error_message = 'FAILED', str(exc)
        raise
      finally:
        _log(ctx, result, status, error_message, t0)

    return _sync_wrapper

  return _decorator
