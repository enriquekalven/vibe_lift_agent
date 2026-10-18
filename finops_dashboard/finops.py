"""Live token economics, spend-change breakdown and what-if projections.

Every number here is computed from observed Cloud Monitoring telemetry (the ge_fleet payload) and the
list-price rate cards used elsewhere in FinOps Dashboard. Nothing is assumed or seeded:

* Token economics: per-model and per-agent token, call and cost figures, exactly as observed.
* Spend change: this period vs the previous period of equal length, split into drivers with an
  exact additive decomposition, so the drivers always sum to the observed change.
* What-if: arithmetic projections on the observed baseline (switch a share of one model's tokens to
  another model's list price, or change the cache-read share). Stated assumptions: token counts stay
  the same; quality and latency are not modelled.
"""

from __future__ import annotations

import datetime
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from finops_dashboard import reference_data, throughput

_TOKEN_KEYS = ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens')
_PRICE_KEY = {
    'input_tokens': 'input',
    'output_tokens': 'output',
    'reasoning_tokens': 'output',
    'cache_read_tokens': 'cached_read',
    'cache_write_tokens': 'cache_write',
}
DRIVERS = (
    ('volume', 'Call volume', 'More or fewer model calls at the previous cost per call.'),
    ('prompt_size', 'Prompt size', 'Change in uncached input tokens per call.'),
    ('output_length', 'Output length', 'Change in output and reasoning tokens per call.'),
    ('caching', 'Caching', 'Change in cache-read and cache-write tokens per call.'),
    ('model_mix', 'Models started or stopped', 'Models used in only one of the two periods.'),
)


def _normalize_card(card: Any) -> dict[str, float] | None:
  """Normalizes either a dict rate card or a telemetry.ModelRateCard into a standard float mapping."""
  if card is None:
    return None
  if isinstance(card, Mapping):
    return {
        'input': float(card.get('input') or 0.0),
        'output': float(card.get('output') or 0.0),
        'cached_read': float(card.get('cached_read') or 0.0),
        'cache_write': float(card.get('cache_write') or 0.0),
    }
  if hasattr(card, 'input_per_million_usd'):
    return {
        'input': float(getattr(card, 'input_per_million_usd', 0.0) or 0.0),
        'output': float(getattr(card, 'output_per_million_usd', 0.0) or 0.0),
        'cached_read': float(getattr(card, 'cached_read_per_million_usd', 0.0) or 0.0),
        'cache_write': float(getattr(card, 'cache_write_per_million_usd', 0.0) or 0.0),
    }
  return None


def _cost(row: Mapping[str, Any], card: Any) -> float:
  nc = _normalize_card(card) or {}
  return sum(float(row.get(k) or 0) / 1e6 * float(nc.get(_PRICE_KEY[k]) or 0) for k in _TOKEN_KEYS)


def _div(a: float, b: float) -> float | None:
  return a / b if b else None


def _models_by_name(usage: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
  if not isinstance(usage, Mapping):
    return {}
  return {str(m.get('model')): m for m in usage.get('models') or [] if isinstance(m, Mapping)}


def build_token_economics(fleet: Mapping[str, Any] | None) -> dict[str, Any]:
  """Per-model and per-agent token economics from the live fleet payload."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else None
  if usage is None:
    return {'status': 'NO_DATA', 'reason': 'Vertex AI model usage metrics are not available.'}
  totals = usage.get('totals') or {}
  total_cost = totals.get('est_cost_usd') or 0.0
  models = []
  for m in usage.get('models') or []:
    calls = int(m.get('invocations') or 0)
    cost = m.get('est_cost_usd')
    prompt = int(m.get('input_tokens') or 0) + int(m.get('cache_read_tokens') or 0) + int(m.get('cache_write_tokens') or 0)
    models.append({
        'model': m.get('model'),
        'calls': calls,
        'input_tokens': int(m.get('input_tokens') or 0),
        'output_tokens': int(m.get('output_tokens') or 0),
        'reasoning_tokens': int(m.get('reasoning_tokens') or 0),
        'cache_read_tokens': int(m.get('cache_read_tokens') or 0),
        'est_cost_usd': cost,
        'cost_share_pct': round(cost / total_cost * 100, 1) if cost is not None and total_cost else None,
        'avg_prompt_tokens_per_call': round(prompt / calls) if calls else None,
        'avg_output_tokens_per_call': round(int(m.get('output_tokens') or 0) / calls) if calls else None,
        'cache_read_share_pct': m.get('cache_read_share_pct'),
        'cost_per_1k_calls_usd': round(cost / calls * 1000, 4) if cost is not None and calls else None,
    })
  # Per agent: each runtime once (shared runtimes are registered in several apps),
  # plus active standalone/unregistered runtimes that emitted token telemetry.
  from finops_dashboard.fleet import runtime_backend_key  # pylint: disable=g-import-not-at-top
  seen: set[str] = set()
  agents = []
  all_agent_sources = [
      (a, 'REGISTERED_IN_GE') for a in (fleet.get('agents') or [])
  ] + [
      (a, 'UNREGISTERED_STANDALONE') for a in (fleet.get('unregistered_runtimes') or [])
  ]
  for a, reg_status in all_agent_sources:
    mt = a.get('metrics') or {}
    if not mt.get('llm_calls') and not mt.get('input_tokens'):
      continue
    key = runtime_backend_key(dict(a))
    if key in seen:
      continue
    seen.add(key)
    req = int(mt.get('requests') or 0)
    tok = int(mt.get('input_tokens') or 0) + int(mt.get('output_tokens') or 0)
    agents.append({
        'display_name': a.get('display_name'),
        'engine_id': a.get('engine_id'),
        'registration_status': reg_status,
        'runtime_type': a.get('type'),
        'requests': req,
        'llm_calls': int(mt.get('llm_calls') or 0),
        'input_tokens': int(mt.get('input_tokens') or 0),
        'output_tokens': int(mt.get('output_tokens') or 0),
        'cached_tokens': int(mt.get('cached_tokens') or 0),
        'est_token_cost_usd': mt.get('est_token_cost_usd'),
        'models': (a.get('backend') or {}).get('models') or [],
        'token_source': mt.get('token_source') or ('Cloud Trace / Logging: gen_ai.*' if reg_status == 'UNREGISTERED_STANDALONE' else None),
        'tokens_per_request': round(tok / req) if req else None,
        'llm_calls_per_request': round(int(mt.get('llm_calls') or 0) / req, 2) if req else None,
    })
  agents.sort(key=lambda r: -(r['input_tokens'] + r['output_tokens']))
  no_telemetry = []
  seen_nt: set[str] = set()
  for a, reg_status in all_agent_sources:
    mt = a.get('metrics') or {}
    key = runtime_backend_key(dict(a))
    if mt.get('requests') and not mt.get('input_tokens') and key not in seen and key not in seen_nt:
      seen_nt.add(key)
      no_telemetry.append({
          'display_name': a.get('display_name'),
          'requests': int(mt.get('requests') or 0),
          'runs_on': (a.get('backend') or {}).get('kind'),
          'registration_status': reg_status,
          'runtime_type': a.get('type'),
      })
  ge = fleet.get('ge_assistant_usage') if isinstance(fleet.get('ge_assistant_usage'), Mapping) else None
  ge_rows = []
  if ge:
    names = {e.get('engine_key'): e.get('display_name') for e in fleet.get('engines') or [] if isinstance(e, Mapping)}
    for key, st in (ge.get('by_engine') or {}).items():
      ge_rows.append({'engine_key': key, 'app': names.get(key) or st.get('engine_id'),
                      'llm_calls': st.get('llm_calls'), 'input_tokens': st.get('input_tokens'),
                      'output_tokens': st.get('output_tokens'), 'conversations': st.get('conversations'),
                      'models': st.get('models'), 'assistant_agents': st.get('assistant_agents')})
    ge_rows.sort(key=lambda r: -(int(r['input_tokens'] or 0) + int(r['output_tokens'] or 0)))
  return {
      'status': 'LIVE',
      'window_hours': fleet.get('window_hours'),
      'interval': usage.get('interval'),
      'totals': {
          'calls': int(totals.get('invocations') or 0),
          'input_tokens': int(totals.get('input_tokens') or 0),
          'output_tokens': int(totals.get('output_tokens') or 0),
          'reasoning_tokens': int(totals.get('reasoning_tokens') or 0),
          'cache_read_tokens': int(totals.get('cache_read_tokens') or 0),
          'est_cost_usd': totals.get('est_cost_usd'),
          'models_without_rate_card': list(totals.get('models_without_rate_card') or []),
      },
      'models': models,
      'agents': agents,
      'agents_without_token_telemetry': sorted(no_telemetry, key=lambda r: -r['requests']),
      'ge_assistant': ge_rows,
      'ge_assistant_status': 'LIVE' if ge else 'UNAVAILABLE',
      'unregistered_summary': fleet.get('unregistered_summary') or {},
      'skills_and_mcp': fleet.get('skills_and_mcp') or [],
      'gke_workloads': fleet.get('gke_workloads') or {'clusters': [], 'workloads': []},
      'agents_note': ('Per-agent tokens come from OpenTelemetry gen_ai data the agent exports: Cloud Logging '
                      'inference events, else Cloud Trace spans (including standalone Agent Engines not registered in GE). '
                      'Model cost is project-wide.'),
      'source': 'Cloud Monitoring: aiplatform publisher token_count + model_invocation_count; list prices',
  }


def build_spend_drift(
    current: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None,
    rate_cards: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
  """Explains the change in estimated model spend between two equal-length periods.

  Both periods are priced with the same (current) list prices, so price changes are excluded. For a
  model used in both periods with calls N and per-call tokens a (input), b (output + reasoning),
  c (cache read), w (cache write):
    volume        = (N1 - N0) * u0            (u = cost per call)
    prompt_size   = N1 * (a1 - a0) * p_in
    output_length = N1 * (b1 - b0) * p_out
    caching       = N1 * ((c1 - c0) * p_cr + (w1 - w0) * p_cw)
  which sums exactly to N1*u1 - N0*u0. A model with no calls in one period goes to model_mix.
  """
  if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
    return {'status': 'NO_DATA', 'reason': 'Model usage for this or the previous period is not available.'}
  cards = dict(previous.get('rate_cards') or {})
  cards.update(current.get('rate_cards') or {})
  cards.update(rate_cards or {})
  cur, prev = _models_by_name(current), _models_by_name(previous)
  drivers = {k: 0.0 for k, _, _ in DRIVERS}
  rows, unpriced = [], []
  cost_now = cost_before = 0.0
  for name in sorted(set(cur) | set(prev)):
    card = _normalize_card(cards.get(name))
    if not card:
      unpriced.append(name)
      continue
    r1, r0 = cur.get(name, {}), prev.get(name, {})
    c1, c0 = _cost(r1, card), _cost(r0, card)
    n1, n0 = int(r1.get('invocations') or 0), int(r0.get('invocations') or 0)
    cost_now += c1
    cost_before += c0
    row = {'model': name, 'cost_before_usd': round(c0, 4), 'cost_now_usd': round(c1, 4),
           'calls_before': n0, 'calls_now': n1, 'change_usd': round(c1 - c0, 4)}
    parts = {k: 0.0 for k, _, _ in DRIVERS}
    if n0 and n1:
      def per(r, k, n):
        return float(r.get(k) or 0) / n
      parts['volume'] = (n1 - n0) * (c0 / n0)
      parts['prompt_size'] = n1 * (per(r1, 'input_tokens', n1) - per(r0, 'input_tokens', n0)) * card['input'] / 1e6
      parts['output_length'] = n1 * (
          (per(r1, 'output_tokens', n1) - per(r0, 'output_tokens', n0))
          + (per(r1, 'reasoning_tokens', n1) - per(r0, 'reasoning_tokens', n0))
      ) * card['output'] / 1e6
      parts['caching'] = n1 * (
          (per(r1, 'cache_read_tokens', n1) - per(r0, 'cache_read_tokens', n0)) * card['cached_read']
          + (per(r1, 'cache_write_tokens', n1) - per(r0, 'cache_write_tokens', n0)) * card['cache_write']) / 1e6
    else:
      parts['model_mix'] = c1 - c0  # Started (c0 = 0), stopped (c1 = 0) or tokens without call counts.
    for k, v in parts.items():
      drivers[k] += v
    row['drivers_usd'] = {k: round(v, 4) for k, v in parts.items()}
    rows.append(row)
  change = cost_now - cost_before
  explained = sum(drivers.values())
  return {
      'status': 'LIVE',
      'current_interval': current.get('interval'),
      'previous_interval': previous.get('interval'),
      'cost_before_usd': round(cost_before, 4),
      'cost_now_usd': round(cost_now, 4),
      'change_usd': round(change, 4),
      'change_pct': round(change / cost_before * 100, 1) if cost_before else None,
      'drivers': [
          {'driver_id': k, 'name': label, 'description': desc, 'change_usd': round(drivers[k], 4),
           'share_of_change_pct': round(drivers[k] / change * 100, 1) if abs(change) > 1e-9 else None}
          for k, label, desc in DRIVERS
      ],
      # Computed, not asserted: the decomposition is exact up to floating-point rounding.
      'unexplained_usd': round(change - explained, 6),
      'by_model': sorted(rows, key=lambda r: -abs(r['change_usd'])),
      'models_without_rate_card': unpriced,
      'method': 'Both periods priced at current list prices; price changes are excluded.',
  }


def project_model_switch(
    usage: Mapping[str, Any] | None,
    from_model: str,
    to_model: str,
    share_pct: float,
    rate_cards: Mapping[str, Any],
) -> dict[str, Any]:
  """Projects spend if share_pct of from_model's observed tokens were billed at to_model's list price."""
  models = _models_by_name(usage)
  src = models.get(from_model)
  if isinstance(share_pct, bool) or not math.isfinite(float(share_pct)):
    raise ValueError('Invalid numeric input.')
  if src is None:
    return {'status': 'ERROR', 'error': f'{from_model} has no observed usage in this period.'}
  if from_model not in rate_cards or to_model not in rate_cards:
    return {'status': 'ERROR', 'error': 'No list price on file for one of the models.'}
  share = max(0.0, min(100.0, float(share_pct))) / 100.0
  moved = {k: float(src.get(k) or 0) * share for k in _TOKEN_KEYS}
  before = _cost(moved, rate_cards[from_model])
  after = _cost(moved, rate_cards[to_model])
  base_total = float(((usage or {}).get('totals') or {}).get('est_cost_usd') or 0.0)
  return {
      'status': 'PROJECTION',
      'scenario': f'Move {share * 100:.0f}% of {from_model} tokens to {to_model}',
      'observed_baseline_usd': round(base_total, 4),
      'moved_tokens': {k: int(v) for k, v in moved.items()},
      'moved_cost_before_usd': round(before, 4),
      'moved_cost_after_usd': round(after, 4),
      'change_usd': round(after - before, 4),
      'projected_total_usd': round(base_total - before + after, 4),
      'assumptions': ['Same token counts on the new model.', 'Quality and latency are not modelled.',
                      'List prices; discounts and committed use are not applied.'],
      'interval': (usage or {}).get('interval'),
  }


def project_cache_share(
    usage: Mapping[str, Any] | None,
    model: str,
    target_cache_share_pct: float,
    rate_cards: Mapping[str, Any],
) -> dict[str, Any]:
  """Projects spend if model's cache-read share of prompt tokens were target_cache_share_pct."""
  if isinstance(target_cache_share_pct, bool) or not math.isfinite(float(target_cache_share_pct)):
    raise ValueError('Invalid numeric input.')
  src = _models_by_name(usage).get(model)
  card = _normalize_card(rate_cards.get(model))
  if src is None or card is None:
    return {'status': 'ERROR', 'error': f'{model} has no observed usage or no list price.'}
  inp, cr = float(src.get('input_tokens') or 0), float(src.get('cache_read_tokens') or 0)
  prompt = inp + cr
  if not prompt:
    return {'status': 'ERROR', 'error': f'{model} has no prompt tokens in this period.'}
  t = max(0.0, min(100.0, float(target_cache_share_pct))) / 100.0
  new_cr, new_in = prompt * t, prompt * (1 - t)
  change = ((new_in - inp) * card['input'] + (new_cr - cr) * card['cached_read']) / 1e6
  base_total = float(((usage or {}).get('totals') or {}).get('est_cost_usd') or 0.0)
  return {
      'status': 'PROJECTION',
      'scenario': f'{model}: cache-read share {cr / prompt * 100:.1f}% -> {t * 100:.0f}% of prompt tokens',
      'observed_baseline_usd': round(base_total, 4),
      'observed_cache_share_pct': round(cr / prompt * 100, 1),
      'change_usd': round(change, 4),
      'projected_total_usd': round(base_total + change, 4),
      'assumptions': ['Same total prompt tokens.', 'Cache storage (per hour) is not included.',
                      'List prices; discounts are not applied.'],
      'interval': (usage or {}).get('interval'),
  }


def build_live_finops(
    fleet: Mapping[str, Any] | None,
    *,
    daily_usage: Mapping[str, Any] | None = None,
    sessions: Sequence[Mapping[str, Any]] | None = None,
    session_turns: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
  """Bundle for /api/state in live mode."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage')
  te = build_token_economics(fleet)
  loop_baselines = build_session_loop_baselines(sessions, session_turns)
  return {
      'token_economics': te,
      'spend_drift': build_spend_drift(usage, fleet.get('model_usage_previous')),
      'value_realization': build_value_realization_ledger(
          fleet, sessions=sessions, daily_usage=daily_usage
      ),
      'loop_baselines': loop_baselines,
      'loop_circuit_breaker': build_loop_circuit_breaker_audit(fleet, token_economics=te, baselines=loop_baselines),
      'gsu_and_quota_forecast': build_gsu_and_quota_forecast(fleet, daily_usage=daily_usage),
      'model_modernization_radar': build_model_modernization_radar(fleet, token_economics=te),
      'what_if_models': [m.get('model') for m in (usage or {}).get('models') or [] if m.get('est_cost_usd') is not None],
      'rate_card_models': sorted(((usage or {}).get('rate_cards') or {}).keys()),
      'unregistered_summary': fleet.get('unregistered_summary') or {},
      'unregistered_runtimes': fleet.get('unregistered_runtimes') or [],
      'gke_workloads': fleet.get('gke_workloads') or {'clusters': [], 'workloads': []},
      'skills_and_mcp': fleet.get('skills_and_mcp') or [],
      'antigravity_usage': fleet.get('antigravity_usage'),
  }


SESSION_TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'total_tokens')


def _int_or_none(value: object) -> int | None:
  if value is None or value == '' or isinstance(value, bool):
    return None
  try:
    f = float(value)  # type: ignore[arg-type]
    if not math.isfinite(f):
      return None
    return int(f)
  except (TypeError, ValueError, OverflowError):
    return None


def _sum_known(values: list[Any]) -> int | None:
  """Sum of the measured values; None when no value was measured (never treats unknown as 0)."""
  known = [n for v in values if (n := _int_or_none(v)) is not None]
  return sum(known) if known else None


def build_session_token_drilldown(
    sessions: list[Mapping[str, Any]] | None,
    session_turns: Mapping[str, list[Mapping[str, Any]]] | None,
    sessionless_turns: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
  """Per-user session token rollup plus a per-session turn-by-turn token dictionary.

  `sessions` are fct_sessions rows (ge_mart.session_from_row), `session_turns` is
  {session_key: [turn dicts]} (ge_mart.group_session_turns), and optional `sessionless_turns`
  are token-bearing turns without a session_id (e.g. standalone Vertex Agent Engine traces) so
  total tokens across all turns are conserved. Only token counts are returned; prompt and
  response text are never read.
  """
  sessions = [s for s in (sessions or []) if isinstance(s, Mapping)]
  session_turns = session_turns if isinstance(session_turns, Mapping) else {}
  kept_sessionless = [dict(t) for t in (sessionless_turns or []) if isinstance(t, Mapping)]

  by_session: dict[str, dict[str, Any]] = {}
  users: dict[str, dict[str, Any]] = {}
  kept: list[Mapping[str, Any]] = []
  for s in sessions:
    key = str(s.get('session_key') or f"{s.get('engine_key') or ''}|{s.get('session_id') or ''}")
    if key in by_session:
      # A session row seen twice would double count its tokens in the user rollup and totals.
      continue
    kept.append(s)
    turns = [dict(t) for t in session_turns.get(key) or [] if isinstance(t, Mapping)]
    turn_count = max(0, _int_or_none(s.get('turns')) or 0)
    by_session[key] = {
        'session_id': s.get('session_id'),
        'engine_key': s.get('engine_key'),
        'user_email': s.get('user_email'),
        'agent_name': s.get('agent_name'),
        'session_start': s.get('session_start'),
        'session_end': s.get('session_end'),
        'turns': turn_count,
        **{k: s.get(k) for k in SESSION_TOKEN_KEYS},
        'turn_detail': turns,
        'turn_detail_count': len(turns),
        # False when the per-session cap (100 most recent turns) or the row limit cut turns off.
        'turn_detail_complete': len(turns) >= turn_count,
    }
    email = str(s.get('user_email') or 'unknown')
    u = users.setdefault(email, {'user_email': email, 'session_keys': [], '_tok': {k: [] for k in SESSION_TOKEN_KEYS},
                                 'turns': 0, 'last_activity': None})
    u['session_keys'].append(key)
    u['turns'] += turn_count
    for k in SESSION_TOKEN_KEYS:
      u['_tok'][k].append(s.get(k))
    end = s.get('session_end') or s.get('session_start')
    if end and (u['last_activity'] is None or str(end) > str(u['last_activity'])):
      u['last_activity'] = end

  seen_sessionless_ids: set[str] = set()
  unique_sessionless: list[dict[str, Any]] = []
  for t in kept_sessionless:
    tid = str(t.get('turn_id') or '')
    if tid and tid in seen_sessionless_ids:
      continue
    if tid:
      seen_sessionless_ids.add(tid)
    unique_sessionless.append(t)
    email = str(t.get('user_email') or 'unknown')
    u = users.setdefault(email, {'user_email': email, 'session_keys': [], '_tok': {k: [] for k in SESSION_TOKEN_KEYS},
                                 'turns': 0, 'last_activity': None})
    u['turns'] += 1
    for k in SESSION_TOKEN_KEYS:
      u['_tok'][k].append(t.get(k))
    ts = t.get('ts')
    if ts and (u['last_activity'] is None or str(ts) > str(u['last_activity'])):
      u['last_activity'] = ts

  user_rows: list[dict[str, Any]] = []
  for u in users.values():
    tok = u.pop('_tok')
    row = {**u, 'sessions': len(u['session_keys'])}
    row.update({k: _sum_known(tok[k]) for k in SESSION_TOKEN_KEYS})
    row['avg_total_tokens_per_session'] = (
        round(row['total_tokens'] / row['sessions']) if row['total_tokens'] is not None and row['sessions'] else None)
    user_rows.append(row)
  user_rows.sort(key=lambda r: (r['total_tokens'] is None, -(r['total_tokens'] or 0), r['user_email']))

  all_token_sources = [*kept, *unique_sessionless]
  return {
      'source': 'finops_mart.fct_sessions + finops_mart.fct_turns (token counts only; no prompt text)',
      'session_count': len(by_session),
      'sessionless_turn_count': len(unique_sessionless),
      'user_count': len(user_rows),
      'totals': {k: _sum_known([s.get(k) for s in all_token_sources]) for k in SESSION_TOKEN_KEYS},
      'users': user_rows,
      'sessions': by_session,
      'sessionless_turns': unique_sessionless,
  }


# ---------------------------------------------------------------------------------------------
# Per-user estimated model cost (list price; never billed cost)
# ---------------------------------------------------------------------------------------------

USER_COST_METHOD = (
    'Logged input, cached-input and output tokens (finops_mart.fct_turns) x Vertex AI list price; cached '
    'input is priced at the cache-read rate. Thinking tokens count only when the logged output includes them.'
)
USER_COST_NOTE = (
    'Estimate at list price, not your invoice. Billed cost (Cloud Billing export) is project-level and is '
    'never split per user.'
)
NO_MODEL_LABEL = '(model not logged)'


def rate_card_for(model: object, rate_cards: Mapping[str, Any]) -> tuple[str, dict[str, float] | None]:
  """(card name, card) for a logged model; 'publishers/google/models/x' uses card 'x'. No fallback card."""
  name = str(model or '').strip().rsplit('/', 1)[-1]
  return name, (_normalize_card(rate_cards.get(name)) if name else None)


def tokens_cost_usd(input_tokens: int | None, output_tokens: int | None, cached_input_tokens: int | None,
                    card: Any) -> float:
  """List-price cost of measured tokens, priced like fleet._estimate_token_cost_usd.

  OTel input_tokens include cache reads, so cached tokens are priced once at the cache-read rate and
  only the rest at the input rate. Reasoning is not added on top: ADK output_tokens include thoughts.
  """
  nc = _normalize_card(card) or {}
  in_tok = max(0, _int_or_none(input_tokens) or 0)
  cached = min(in_tok, max(0, _int_or_none(cached_input_tokens) or 0))
  out_tok = max(0, _int_or_none(output_tokens) or 0)
  uncached = max(in_tok - cached, 0)
  return (uncached * float(nc.get('input') or 0)
          + cached * float(nc.get('cached_read') or 0)
          + out_tok * float(nc.get('output') or 0)) / 1e6


def estimate_user_costs(
    users: Sequence[Mapping[str, Any]] | None,
    model_rows: Sequence[Mapping[str, Any]] | None,
    rate_cards: Mapping[str, Mapping[str, float]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  """Adds a list-price cost estimate to each user from per user x model x app token sums.

  `users` are live power_users_ldap dicts; `model_rows` are ge_mart.user_model_tokens_from_row rows.
  Returns (new user dicts, summary). The input dicts are shared with the cached BigQuery insights,
  so they are copied, never mutated. Tokens whose model is not logged or has no rate card are not
  priced (there is no fallback price); they are counted in est_cost_unpriced_tokens instead.
  """
  acc: dict[str, dict[str, Any]] = {}
  for r in model_rows or []:
    if not isinstance(r, Mapping) or not r.get('user_email'):
      continue
    in_tok, out_tok, cached = r.get('input_tokens'), r.get('output_tokens'), r.get('cached_input_tokens')
    if in_tok is None and out_tok is None and cached is None:
      continue
    a = acc.setdefault(str(r['user_email']), {'usd': 0.0, 'priced': False, 'by_engine': {}, 'models': set(),
                                               'unpriced_tokens': 0, 'unpriced_models': set()})
    name, card = rate_card_for(r.get('model_name'), rate_cards)
    if card is None:
      a['unpriced_tokens'] += max(0, _int_or_none(in_tok) or 0) + max(0, _int_or_none(out_tok) or 0)
      a['unpriced_models'].add(name or NO_MODEL_LABEL)
      continue
    usd = tokens_cost_usd(in_tok, out_tok, cached, card)
    a['usd'] += usd
    a['priced'] = True
    a['models'].add(name)
    engine = str(r.get('engine_key') or '')
    if engine:
      a['by_engine'][engine] = a['by_engine'].get(engine, 0.0) + usd

  out: list[dict[str, Any]] = []
  for u in users or []:
    if not isinstance(u, Mapping):
      continue
    user_acc = acc.get(str(u.get('user_email') or ''))
    row = dict(u)
    row.update({
        # None when no priced tokens were logged for the user (unknown, not $0).
        'est_cost_usd': round(user_acc['usd'], 4) if user_acc and user_acc['priced'] else None,
        # Per GE app ('location/engine_id'), so the dashboard's app filter can scope the estimate.
        'est_cost_by_engine': {k: round(v, 4) for k, v in sorted(user_acc['by_engine'].items())} if user_acc else {},
        'est_cost_models': sorted(user_acc['models']) if user_acc else [],
        'est_cost_unpriced_tokens': user_acc['unpriced_tokens'] if user_acc else 0,
    })
    out.append(row)

  models_priced: set[str] = set()
  models_unpriced: set[str] = set()
  for a in acc.values():
    models_priced |= a['models']
    models_unpriced |= a['unpriced_models']
  priced = [a for a in acc.values() if a['priced']]
  summary = {
      'status': 'LIVE' if acc else 'NO_DATA',
      'source': 'finops_mart.fct_turns',
      'method': USER_COST_METHOD,
      'note': USER_COST_NOTE,
      'total_est_cost_usd': round(sum(a['usd'] for a in priced), 4) if priced else None,
      'priced_users': len(priced),
      'models_priced': sorted(models_priced),
      'models_without_rate_card': sorted(models_unpriced),
      'unpriced_tokens': sum(a['unpriced_tokens'] for a in acc.values()),
  }
  return out, summary


# ---------------------------------------------------------------------------------------------
# Shared helpers for the tokenomics intelligence builders below
# ---------------------------------------------------------------------------------------------
def _resolve_rate_cards_map(usage: Mapping[str, Any] | None) -> dict[str, dict[str, float]]:
  from finops_dashboard import telemetry  # pylint: disable=g-import-not-at-top
  cards: dict[str, dict[str, float]] = {}
  for k, v in telemetry.RATE_CARDS.items():
    nc = _normalize_card(v)
    if nc:
      cards[k] = nc
  if isinstance(usage, Mapping):
    for k, v in (usage.get('rate_cards') or {}).items():
      nc = _normalize_card(v)
      if nc:
        cards[str(k)] = nc
  return cards


def _model_key(model: object) -> str:
  """Lifecycle / Provisioned Throughput lookup key (see reference_data.model_key)."""
  return reference_data.model_key(model)


def _one_line(text: object, limit: int = 80) -> str:
  """Display text that is safe inside a generated code comment (no line breaks)."""
  return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(text or '')).strip()[:limit]


def _py_identifier(text: object) -> str:
  ident = re.sub(r'[^a-z0-9_]+', '_', str(text or '').lower()).strip('_')
  return ident if ident and not ident[0].isdigit() else f'agent_{ident}'


# ---------------------------------------------------------------------------------------------
# 1. Cache-read savings & failure token tax ledger (outcome unit economics)
# ---------------------------------------------------------------------------------------------
VALUE_REALIZATION_METHOD = (
    'Cache-read savings = cache_read_tokens x (input list price - cached-read list price): a gross saving that '
    'excludes explicit context-cache storage charges. Failure token tax (estimate) allocates an agent\'s observed '
    'token spend to server-side failures: spend x 5xx / (requests - 4xx). 4xx requests are rejected before model '
    'work and are assumed to use no tokens.'
)


def build_value_realization_ledger(
    fleet: Mapping[str, Any] | None,
    sessions: Sequence[Mapping[str, Any]] | None = None,
    daily_usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Cache-read savings plus a split of agent token spend into productive spend and failure token tax."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else None
  if usage is None:
    return {'status': 'NO_DATA', 'reason': 'Vertex AI model usage metrics are not available.'}

  cards = _resolve_rate_cards_map(usage)
  window_hours = max(1, int(fleet.get('window_hours') or 24))
  scale_30d = 720.0 / window_hours

  cache_by_model: list[dict[str, Any]] = []
  total_cache_saved_usd = 0.0
  total_cache_read_tokens = 0
  total_prompt_tokens = 0
  priced_cache_models = 0

  for m in usage.get('models') or []:
    if not isinstance(m, Mapping):
      continue
    model_name = str(m.get('model') or '')
    # Model-level input_tokens exclude cache reads (see _cost / project_cache_share), so prompt = input + cache read.
    cr_tok = max(0, int(m.get('cache_read_tokens') or 0))
    in_tok = max(0, int(m.get('input_tokens') or 0))
    prompt_tok = in_tok + cr_tok
    total_cache_read_tokens += cr_tok
    total_prompt_tokens += prompt_tok
    _, card = rate_card_for(model_name, cards)
    saved_usd: float | None = None
    if card is not None:
      priced_cache_models += 1
      discount_per_1m = max(0.0, float(card['input']) - float(card['cached_read']))
      saved_usd = round(cr_tok * discount_per_1m / 1e6, 4)
      total_cache_saved_usd += saved_usd
    if prompt_tok > 0:
      cache_by_model.append({
          'model': model_name,
          'cache_read_tokens': cr_tok,
          'prompt_tokens': prompt_tok,
          'cache_hit_share_pct': round(cr_tok / prompt_tok * 100.0, 1),
          'realized_savings_window_usd': saved_usd,
          'realized_savings_30d_usd': round(saved_usd * scale_30d, 2) if saved_usd is not None else None,
      })
  cache_by_model.sort(key=lambda r: -(r.get('realized_savings_window_usd') or 0.0))

  # Agent-level failure token tax from observed 4xx/5xx counts and OTel token cost.
  from finops_dashboard.fleet import runtime_backend_key  # pylint: disable=g-import-not-at-top
  seen_agents: set[str] = set()
  all_sources = [
      (a, 'REGISTERED_IN_GE') for a in (fleet.get('agents') or []) if isinstance(a, Mapping)
  ] + [
      (a, 'UNREGISTERED_STANDALONE') for a in (fleet.get('unregistered_runtimes') or []) if isinstance(a, Mapping)
  ]

  agent_tax_rows: list[dict[str, Any]] = []
  total_requests = 0
  total_rejected = 0
  total_failed = 0
  total_succeeded = 0
  total_est_spend = 0.0
  total_failure_tax_usd = 0.0
  priced_agents = 0
  unpriced_agents = 0

  for a, reg_status in all_sources:
    key = runtime_backend_key(dict(a))
    if key in seen_agents:
      continue
    seen_agents.add(key)
    raw_mt = a.get('metrics')
    mt: Mapping[str, Any] = raw_mt if isinstance(raw_mt, Mapping) else {}
    req = _int_or_none(mt.get('requests'))
    if req is None or req <= 0:
      continue
    rejected = min(req, max(0, _int_or_none(mt.get('errors_4xx')) or 0))
    worked = req - rejected
    failed = min(worked, max(0, _int_or_none(mt.get('errors_5xx')) or 0))
    succeeded = worked - failed
    total_requests += req
    total_rejected += rejected
    total_failed += failed
    total_succeeded += succeeded

    ag_in_tok = _int_or_none(mt.get('input_tokens'))
    ag_out_tok = _int_or_none(mt.get('output_tokens'))
    ag_cached_tok = _int_or_none(mt.get('cached_tokens'))
    est_usd = mt.get('est_token_cost_usd')
    if est_usd is None and (ag_in_tok is not None or ag_out_tok is not None):
      raw_be = a.get('backend')
      be_map: Mapping[str, Any] = raw_be if isinstance(raw_be, Mapping) else {}
      models_list = be_map.get('models') or []
      if models_list:
        _, card = rate_card_for(models_list[0], cards)
        if card is not None:
          est_usd = round(tokens_cost_usd(ag_in_tok, ag_out_tok, ag_cached_tok, card), 4)

    fail_share = failed / worked if worked > 0 else 0.0
    tax_usd: float | None = None
    prod_usd: float | None = None
    cost_per_succ: float | None = None
    if est_usd is not None and float(est_usd) > 0:
      priced_agents += 1
      est_f = float(est_usd)
      total_est_spend += est_f
      tax_usd = round(est_f * fail_share, 4)
      prod_usd = round(max(0.0, est_f - tax_usd), 4)
      total_failure_tax_usd += tax_usd
      cost_per_succ = round(est_f / succeeded, 5) if succeeded > 0 else None
    elif failed > 0 or rejected > 0:
      unpriced_agents += 1

    if failed > 0 or rejected > 0 or tax_usd is not None:
      agent_tax_rows.append({
          'display_name': a.get('display_name') or a.get('agent_id'),
          'agent_id': a.get('agent_id'),
          'registration_status': reg_status,
          'requests': req,
          'successful_requests': succeeded,
          'failed_requests': failed,
          'rejected_requests': rejected,
          'errors_4xx': rejected,
          'errors_5xx': failed,
          'failure_rate_pct': round(fail_share * 100.0, 2) if worked > 0 else None,
          'est_token_cost_usd': round(float(est_usd), 4) if est_usd is not None else None,
          'productive_spend_usd': prod_usd,
          'failure_token_tax_usd': tax_usd,
          'failure_token_tax_30d_usd': round(tax_usd * scale_30d, 2) if tax_usd is not None else None,
          'cost_per_successful_request_usd': cost_per_succ,
      })

  agent_tax_rows.sort(key=lambda r: (-(r.get('failure_token_tax_usd') or 0.0), -(r.get('failed_requests') or 0)))

  # GE turn failure rate from finops_mart (its own day window, reported separately from the agent window above).
  mart_turns = 0
  mart_failed_turns = 0
  mart_days = 0
  mart_source: str | None = None
  if isinstance(daily_usage, Mapping) and isinstance(daily_usage.get('days'), list):
    for d in daily_usage.get('days') or []:
      turns = _int_or_none(d.get('interactions')) if isinstance(d, Mapping) else None
      if turns is None:
        continue
      mart_days += 1
      mart_turns += max(0, turns)
      mart_failed_turns += max(0, _int_or_none(d.get('failed_turns')) or 0)
    mart_source = 'finops_mart daily usage'
  elif sessions:
    for s in sessions:
      if isinstance(s, Mapping):
        mart_turns += max(0, _int_or_none(s.get('turns')) or 0)
        mart_failed_turns += max(0, _int_or_none(s.get('failed_turns')) or 0)
    mart_source = 'finops_mart recent sessions'

  worked_total = total_requests - total_rejected
  priced = priced_agents > 0
  return {
      'status': 'LIVE',
      'window_hours': window_hours,
      'realized_cache_savings_window_usd': round(total_cache_saved_usd, 4) if priced_cache_models > 0 else None,
      'realized_cache_savings_30d_usd': round(total_cache_saved_usd * scale_30d, 2) if priced_cache_models > 0 else None,
      'cache_savings_basis': 'GROSS_LIST_PRICE_EXCLUDING_CACHE_STORAGE',
      'total_cache_read_tokens': total_cache_read_tokens,
      'total_prompt_tokens': total_prompt_tokens,
      'fleet_cache_hit_share_pct': (
          round(total_cache_read_tokens / total_prompt_tokens * 100.0, 1) if total_prompt_tokens > 0 else None
      ),
      'cache_by_model': cache_by_model,
      'agent_requests_total': total_requests,
      'agent_successful_requests': total_succeeded,
      'agent_failed_requests': total_failed,
      'agent_rejected_requests': total_rejected,
      'agent_failure_rate_pct': round(total_failed / worked_total * 100.0, 2) if worked_total > 0 else None,
      'priced_agent_count': priced_agents,
      'unpriced_agent_count': unpriced_agents,
      'failure_tax_basis': 'ESTIMATE',
      'agent_tracked_spend_usd': round(total_est_spend, 4) if priced else None,
      'productive_agent_spend_usd': round(max(0.0, total_est_spend - total_failure_tax_usd), 4) if priced else None,
      'failure_token_tax_window_usd': round(total_failure_tax_usd, 4) if priced else None,
      'failure_token_tax_30d_usd': round(total_failure_tax_usd * scale_30d, 2) if priced else None,
      'cost_per_successful_request_usd': (
          round(total_est_spend / total_succeeded, 5) if priced and total_succeeded > 0 else None
      ),
      'mart_source': mart_source if mart_turns > 0 else None,
      'mart_window_days': mart_days if mart_turns > 0 and mart_days > 0 else None,
      'mart_turns_total': mart_turns if mart_turns > 0 else None,
      'mart_failed_turns': mart_failed_turns if mart_turns > 0 else None,
      'mart_failure_rate_pct': round(mart_failed_turns / mart_turns * 100.0, 2) if mart_turns > 0 else None,
      'by_agent': agent_tax_rows,
      'method': VALUE_REALIZATION_METHOD,
  }


# ---------------------------------------------------------------------------------------------
# 2. Runaway loop & fan-out detector + ADK circuit-breaker snippet
# ---------------------------------------------------------------------------------------------
# Same threshold and comparison (>=) as the dashboard health rule RULES.loopCallsPerRequest in
# ui/static/dashboard_enhancements.js, so "loop offender" here means "possible agent loop" on the health card.
# It is the fallback: agents with enough session history get their own cap (build_session_loop_baselines).
DEFAULT_LOOP_CALLS_THRESHOLD = 20
LOOP_BASELINE_MIN_SAMPLES = 10   # runs (turns, or sessions without turn detail) needed for a per-agent baseline
LOOP_CAP_FLOOR = 10              # never recommend an ADK cap below this many LLM calls per run
LOOP_CAP_HEADROOM = 1.5          # cap = 1.5 x the p99 of normal runs
LOOP_BASELINE_METHOD = (
    'Per agent, from the last 30 days of Gemini Enterprise sessions (finops_mart, most recent 500 sessions). A run '
    'is one turn, the unit ADK max_llm_calls caps; without turn detail a session\'s llm_calls / turns stands in. '
    'Outlier fence = max(Q3 + 3 x IQR, 2 x Q3, 10) LLM calls per run. A session is a runaway when its busiest run '
    '(or its average, without turn detail) is above the fence. Recommended ADK cap = max(10, 1.5 x p99 of the runs '
    'inside the fence, the largest run inside the fence), so no normal run seen would have been cut off. Agents '
    f'with fewer than {LOOP_BASELINE_MIN_SAMPLES} runs keep the fixed {DEFAULT_LOOP_CALLS_THRESHOLD} calls/request rule.'
)


def normalize_agent_key(name: object) -> str:
  """Lowercase letters and digits only, so a mart agent_name matches a fleet display_name."""
  return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
  """Linear-interpolated quantile (numpy's default method) of an ascending, non-empty sequence."""
  if len(sorted_vals) == 1:
    return float(sorted_vals[0])
  pos = (len(sorted_vals) - 1) * q
  lo, hi = math.floor(pos), math.ceil(pos)
  return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))


def build_session_loop_baselines(
    sessions: Sequence[Mapping[str, Any]] | None,
    session_turns: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    *,
    min_samples: int = LOOP_BASELINE_MIN_SAMPLES,
) -> dict[str, Any]:
  """Per-agent LLM-calls-per-run distribution, outlier fence, runaway sessions and recommended ADK cap.

  `sessions` are fct_sessions rows (ge_mart.session_from_row) and `session_turns` is {session_key: [turns]}
  (ge_mart.group_session_turns). A turn counts for the agent that served it (turn agent_name, else the
  session's). Turns with no LLM call (searches) are not runs and are skipped. See LOOP_BASELINE_METHOD.
  """
  sessions = [s for s in (sessions or []) if isinstance(s, Mapping)]
  session_turns = session_turns if isinstance(session_turns, Mapping) else {}
  # agent key -> {'label', 'turn': {session_key: [calls per run]}, 'avg': {session_key: calls per turn}, 'meta'}
  groups: dict[str, dict[str, Any]] = {}

  def _group(name: object) -> dict[str, Any] | None:
    key = normalize_agent_key(name)
    if not key:
      return None
    return groups.setdefault(key, {'label': str(name), 'turn': {}, 'avg': {}, 'meta': {}})

  seen: set[str] = set()
  for s in sessions:
    skey = str(s.get('session_key') or f"{s.get('engine_key') or ''}|{s.get('session_id') or ''}")
    if skey in seen:
      continue
    seen.add(skey)
    meta = {'session_id': s.get('session_id'), 'user_email': s.get('user_email'),
            'session_end': s.get('session_end') or s.get('session_start'), 'turns': _int_or_none(s.get('turns')),
            'llm_calls': _int_or_none(s.get('llm_calls'))}
    turn_runs = 0
    for t in session_turns.get(skey) or []:
      if not isinstance(t, Mapping):
        continue
      calls = _int_or_none(t.get('llm_calls'))
      if calls is None or calls < 1:
        continue
      g = _group(t.get('agent_name') or s.get('agent_name'))
      if g is None:
        continue
      g['turn'].setdefault(skey, []).append(calls)
      g['meta'][skey] = meta
      turn_runs += 1
    if turn_runs:
      continue
    # No usable turn detail for this session: its average calls per turn stands in for a run.
    calls, turns = meta['llm_calls'], meta['turns']
    g = _group(s.get('agent_name'))
    if g is not None and calls and turns and calls >= 1 and turns >= 1:
      g['avg'][skey] = calls / turns
      g['meta'][skey] = meta

  agents: list[dict[str, Any]] = []
  insufficient: list[dict[str, Any]] = []
  for key, g in groups.items():
    turn_samples = [c for runs in g['turn'].values() for c in runs]
    if len(turn_samples) >= min_samples:
      basis, samples = 'TURN', sorted(float(c) for c in turn_samples)
      per_session = {sk: float(max(runs)) for sk, runs in g['turn'].items()}
    elif len(g['avg']) >= min_samples:
      basis, samples = 'SESSION_AVERAGE', sorted(g['avg'].values())
      per_session = dict(g['avg'])
    else:
      insufficient.append({'agent_name': g['label'], 'agent_key': key,
                           'samples': max(len(turn_samples), len(g['avg']))})
      continue
    q1, q3 = _quantile(samples, 0.25), _quantile(samples, 0.75)
    fence = max(q3 + 3.0 * (q3 - q1), 2.0 * q3, float(LOOP_CAP_FLOOR))
    normal = [v for v in samples if v <= fence]  # never empty: the fence is at least the median
    p99_normal = _quantile(normal, 0.99)
    cap = max(LOOP_CAP_FLOOR, math.ceil(max(LOOP_CAP_HEADROOM * p99_normal, normal[-1]) - 1e-9))
    runaway_keys = sorted((sk for sk, v in per_session.items() if v > fence), key=lambda sk: -per_session[sk])
    agents.append({
        'agent_name': g['label'],
        'agent_key': key,
        'basis': basis,
        'samples': len(samples),
        'sessions': len(per_session),
        'p50': round(_quantile(samples, 0.50), 1),
        'p95': round(_quantile(samples, 0.95), 1),
        'p99': round(_quantile(samples, 0.99), 1),
        'max': round(samples[-1], 1),
        'fence': round(fence, 1),
        'p99_within_fence': round(p99_normal, 1),
        'max_within_fence': round(normal[-1], 1),
        'recommended_cap': int(cap),
        'runaway_sessions': len(runaway_keys),
        'runaway_session_share_pct': round(100.0 * len(runaway_keys) / len(per_session), 1),
        'runaways': [
            {**g['meta'][sk], 'calls_per_run': round(per_session[sk], 1),
             'run_basis': 'BUSIEST_TURN' if basis == 'TURN' else 'SESSION_AVERAGE'}
            for sk in runaway_keys[:5]
        ],
    })
  agents.sort(key=lambda a: (-a['runaway_sessions'], a['agent_name'].lower()))
  insufficient.sort(key=lambda a: a['agent_name'].lower())
  if not agents and not insufficient:
    return {'status': 'NO_DATA', 'reason': 'No Gemini Enterprise sessions with LLM call counts in the last 30 days.',
            'agents': [], 'insufficient': [], 'method': LOOP_BASELINE_METHOD}
  return {
      'status': 'LIVE',
      'source': 'finops_mart.fct_sessions + fct_turns (last 30 days, most recent 500 sessions)',
      'min_samples': min_samples,
      'agents': agents,
      'insufficient': insufficient,
      'runaway_session_count': sum(a['runaway_sessions'] for a in agents),
      'method': LOOP_BASELINE_METHOD,
  }


def loop_caps_by_agent(baselines: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
  """{normalized agent key: baseline row} for the agents that have a recommended cap."""
  if not isinstance(baselines, Mapping):
    return {}
  return {
      str(b.get('agent_key')): b
      for b in baselines.get('agents') or []
      if isinstance(b, Mapping) and b.get('agent_key') and _int_or_none(b.get('recommended_cap'))
  }


def apply_loop_thresholds(
    agents: Sequence[Any] | None, baselines: Mapping[str, Any] | None
) -> list[Any]:
  """Copies of the fleet agents whose metrics carry their own loop threshold (the health card reads it).

  The input dicts are never mutated (the fleet payload is cached). Agents without a baseline are returned
  as they are, so the health card falls back to DEFAULT_LOOP_CALLS_THRESHOLD.
  """
  caps = loop_caps_by_agent(baselines)
  out: list[Any] = []
  for a in agents or []:
    b = caps.get(normalize_agent_key(a.get('display_name'))) if isinstance(a, Mapping) else None
    if b is None:
      out.append(a)
      continue
    metrics = dict(a.get('metrics') or {})
    metrics['loop_threshold_calls_per_request'] = int(b['recommended_cap'])
    metrics['loop_threshold_basis'] = 'SESSION_P99'
    metrics['loop_threshold_source'] = str(
        (baselines or {}).get('source') or 'finops_mart.fct_sessions + fct_turns'
    )
    out.append({**a, 'metrics': metrics})
  return out


def _adk_circuit_breaker_snippet(
    agent_name: str, calls_per_request: float, cap: int, baseline: Mapping[str, Any] | None = None
) -> str:
  if baseline:
    why = (
        f'# Cap {cap} = max(10, 1.5 x p99 of normal runs, largest normal run): p99 {baseline.get("p99_within_fence")} '
        f'LLM calls per run over {baseline.get("samples")} runs in the last 30 days.\n'
    )
    check = '# ADK raises LlmCallsLimitExceededError once a run exceeds the cap, so that request fails instead of looping.\n'
  else:
    why = ''
    check = (
        '# ADK raises LlmCallsLimitExceededError once a run exceeds the cap, so that request fails instead of\n'
        '# looping. Check the p99 LLM calls per request in your traces before rollout (ADK default cap: 500).\n'
    )
  return (
      'from google.adk.agents import RunConfig\n\n'
      f'# Circuit breaker for {_one_line(agent_name)}: observed {calls_per_request:.1f} LLM calls/request '
      f'(loop threshold {cap}).\n'
      + why + check +
      f'run_config = RunConfig(max_llm_calls={cap})\n'
      '# Per run:  runner.run_async(user_id=..., session_id=..., new_message=..., run_config=run_config)\n'
      f'# Or deployment-wide, without code changes:  ADK_MAX_LLM_CALLS={cap}\n'
  )


def build_loop_circuit_breaker_audit(
    fleet: Mapping[str, Any] | None,
    max_calls_per_request: int = DEFAULT_LOOP_CALLS_THRESHOLD,
    token_economics: Mapping[str, Any] | None = None,
    baselines: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Flags runtimes and sub-agents/tools at or above the loop threshold and estimates the spend above it.

  A runtime with a session baseline (build_session_loop_baselines) is judged against its own recommended
  cap; the others, and sub-agents/tools, against max_calls_per_request.
  """
  fleet = fleet if isinstance(fleet, Mapping) else {}
  te = token_economics if isinstance(token_economics, Mapping) else build_token_economics(fleet)
  if te.get('status') != 'LIVE':
    return {'status': 'NO_DATA', 'reason': te.get('reason') or 'Token economics unavailable.'}

  cards = _resolve_rate_cards_map(fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else None)
  window_hours = max(1, int(fleet.get('window_hours') or 24))
  scale_30d = 720.0 / window_hours
  default_threshold = max(2, int(max_calls_per_request))
  caps = loop_caps_by_agent(baselines)

  offenders: list[dict[str, Any]] = []
  total_excess_calls = 0
  total_excess_tokens = 0
  total_excess_cost_usd = 0.0
  priced_offenders = 0
  per_agent_checked = 0

  for ag in te.get('agents') or []:
    if not isinstance(ag, Mapping):
      continue
    req = int(ag.get('requests') or 0)
    calls = int(ag.get('llm_calls') or 0)
    if req <= 0 or calls <= 0:
      continue
    baseline = caps.get(normalize_agent_key(ag.get('display_name')))
    threshold = max(2, int(baseline['recommended_cap'])) if baseline else default_threshold
    per_agent_checked += 1 if baseline else 0
    cpr = calls / req
    if cpr < threshold:
      continue

    excess_calls = max(0, calls - req * threshold)
    excess_share = excess_calls / calls
    in_tok = int(ag.get('input_tokens') or 0)
    out_tok = int(ag.get('output_tokens') or 0)
    cached_tok = int(ag.get('cached_tokens') or 0)
    total_tok = in_tok + out_tok
    excess_tokens = round(total_tok * excess_share)

    est_cost = ag.get('est_token_cost_usd')
    if est_cost is None and total_tok > 0:
      models_list = ag.get('models') or []
      if models_list:
        _, card = rate_card_for(models_list[0], cards)
        if card is not None:
          est_cost = round(tokens_cost_usd(in_tok, out_tok, cached_tok, card), 4)

    excess_cost_usd: float | None = None
    if est_cost is not None:
      priced_offenders += 1
      excess_cost_usd = round(float(est_cost) * excess_share, 4)
      total_excess_cost_usd += excess_cost_usd

    total_excess_calls += excess_calls
    total_excess_tokens += excess_tokens
    agent_name = str(ag.get('display_name') or 'agent')
    offenders.append({
        'display_name': agent_name,
        'engine_id': ag.get('engine_id'),
        'registration_status': ag.get('registration_status'),
        'runtime_type': ag.get('runtime_type'),
        'requests': req,
        'llm_calls': calls,
        'llm_calls_per_request': round(cpr, 2),
        'threshold_calls_per_request': threshold,
        'threshold_basis': 'SESSION_P99' if baseline else 'DEFAULT',
        'excess_llm_calls': excess_calls,
        'excess_tokens': excess_tokens,
        'est_token_cost_usd': round(float(est_cost), 4) if est_cost is not None else None,
        'excess_loop_cost_window_usd': excess_cost_usd,
        'excess_loop_cost_30d_usd': round(excess_cost_usd * scale_30d, 2) if excess_cost_usd is not None else None,
        'models': ag.get('models') or [],
        'adk_circuit_breaker_snippet': _adk_circuit_breaker_snippet(agent_name, cpr, threshold, baseline),
    })

  # Sub-agents / tools invoked many times inside one trace (Cloud Trace sample: calls / traces it appeared in).
  looping_sub_agents: list[dict[str, Any]] = []
  for sk in fleet.get('skills_and_mcp') or []:
    if not isinstance(sk, Mapping):
      continue
    sk_calls = _int_or_none(sk.get('calls'))
    sk_traces = _int_or_none(sk.get('traces_count'))
    if not sk_calls or not sk_traces or sk_traces <= 0:
      continue
    per_trace = sk_calls / sk_traces
    if per_trace < default_threshold:
      continue
    looping_sub_agents.append({
        'name': sk.get('name'),
        'kind': sk.get('kind'),
        'calls': sk_calls,
        'traces_count': sk_traces,
        'calls_per_trace': round(per_trace, 1),
        'engines': sk.get('engines') or [],
        'co_occurring_input_tokens': sk.get('co_occurring_input_tokens'),
        'co_occurring_output_tokens': sk.get('co_occurring_output_tokens'),
    })
  looping_sub_agents.sort(key=lambda r: -r['calls_per_trace'])

  offenders.sort(key=lambda r: (-(r.get('excess_loop_cost_window_usd') or 0.0), -r['llm_calls_per_request']))
  # No offenders is a measured zero; offenders without a rate card make the total unknown, not zero.
  cost_known = priced_offenders > 0 or not offenders
  return {
      'status': 'LIVE',
      'source': 'Cloud Monitoring v3 + Cloud Trace gen_ai spans + finops_mart.fct_sessions + fct_turns',
      'window_hours': window_hours,
      'threshold_calls_per_request': default_threshold,
      'threshold_basis': 'PER_AGENT' if caps else 'DEFAULT',
      'agents_with_baseline': len(caps),
      'runtimes_on_own_cap': per_agent_checked,
      'offender_count': len(offenders),
      'priced_offender_count': priced_offenders,
      'unpriced_offender_count': len(offenders) - priced_offenders,
      'total_excess_llm_calls': total_excess_calls,
      'total_excess_tokens': total_excess_tokens,
      'excess_loop_cost_window_usd': round(total_excess_cost_usd, 4) if cost_known else None,
      'excess_loop_cost_30d_usd': round(total_excess_cost_usd * scale_30d, 2) if cost_known else None,
      'excess_basis': 'ESTIMATE',
      'offenders': offenders,
      'looping_sub_agents': looping_sub_agents,
      'method': (
          'Flags runtimes whose average LLM calls per request reach their threshold: their own recommended ADK cap '
          'when they have enough session history (see loop_baselines), else '
          f'{default_threshold} (the dashboard loop rule). Sub-agents/tools are flagged at >= {default_threshold} calls '
          'per sampled trace. Spend above the threshold = (llm_calls - requests x threshold) / llm_calls x observed '
          'token spend (assumes equal cost per call). Single runaway sessions are listed from the session history.'
      ),
  }


# ---------------------------------------------------------------------------------------------
# 4. Pay-as-you-go vs. Provisioned Throughput (GSU) break-even & 30/60/90-day spend forecast
# ---------------------------------------------------------------------------------------------
# Provisioned Throughput GSU prices and per-model burndown specs come from the versioned reference file
# (reference/vertex_ai_reference.json, section provisioned_throughput, with its last-checked date).
GSU_PRICE_SOURCE = reference_data.source('provisioned_throughput', 'price_source') + ' (Provisioned Throughput, global endpoint)'
GSU_SPEC_SOURCE = reference_data.source('provisioned_throughput', 'spec_source')
GSU_MONTHLY_1MO_USD, GSU_MONTHLY_1YR_USD = reference_data.gsu_monthly_prices()
# model: (tokens per second per GSU, output/reasoning burndown multiplier, cache-read burndown multiplier)
PT_GSU_SPECS: dict[str, tuple[float, float, float]] = reference_data.pt_specs()
GSU_SIZING_CAVEAT = (
    'Provisioned Throughput must be sized for peak, not average, throughput. These GSU counts use the window '
    'average, so bursty traffic needs more GSUs and anything above the purchased GSUs is billed pay-as-you-go. '
    'Treat COMMIT_GSU_1YR as a prompt to model peak load before buying.'
)
GSU_SIZING_CAVEAT_PROFILE = (
    'Sized from 1-minute token-count buckets: base GSU commitment is chosen to minimize 1-year GSU cost + '
    'pay-as-you-go spillover during peak minutes. 60-second Cloud Monitoring buckets still smooth sub-minute bursts.'
)


def build_gsu_and_quota_forecast(
    fleet: Mapping[str, Any] | None,
    daily_usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Per-model pay-as-you-go vs 1-year GSU break-even, plus a 30/60/90-day spend projection."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else None
  if usage is None:
    return {'status': 'NO_DATA', 'reason': 'Vertex AI model usage metrics are not available.'}

  window_hours = max(1, int(fleet.get('window_hours') or 24))
  window_seconds = window_hours * 3600.0
  scale_30d = 720.0 / window_hours

  model_rows: list[dict[str, Any]] = []
  total_payg_30d_usd = 0.0
  gsu_candidates_count = 0
  profile_sized_models = 0

  for m in usage.get('models') or []:
    if not isinstance(m, Mapping):
      continue
    name = str(m.get('model') or '')
    in_tok = max(0, int(m.get('input_tokens') or 0))
    cr_tok = max(0, int(m.get('cache_read_tokens') or 0))
    cw_tok = max(0, int(m.get('cache_write_tokens') or 0))
    out_tok = max(0, int(m.get('output_tokens') or 0))
    reason_tok = max(0, int(m.get('reasoning_tokens') or 0))
    total_tok = in_tok + cr_tok + cw_tok + out_tok + reason_tok
    if total_tok <= 0:
      continue

    est_cost = m.get('est_cost_usd')
    payg_30d_usd = round(float(est_cost) * scale_30d, 2) if est_cost is not None else None
    if payg_30d_usd is not None:
      total_payg_30d_usd += payg_30d_usd

    row: dict[str, Any] = {
        'model': name,
        'total_tokens_window': total_tok,
        'avg_tokens_per_second': round(total_tok / window_seconds, 2),
        'payg_window_usd': round(float(est_cost), 4) if est_cost is not None else None,
        'payg_30d_run_rate_usd': payg_30d_usd,
        'sizing_basis': None,
        'gsu_throughput_per_gsu': None,
        'burndown_tokens_per_second': None,
        'peak_burndown_tps': None,
        'p99_burndown_tps': None,
        'p95_burndown_tps': None,
        'est_single_gsu_utilization_pct': None,
        'peak_gsu_units': None,
        'p99_gsu_units': None,
        'avg_gsu_units': None,
        'recommended_gsu_units': None,
        'covered_tokens_share_pct': None,
        'spillover_tokens_share_pct': None,
        'gsu_1mo_commitment_usd': None,
        'gsu_1yr_commitment_usd': None,
        'spillover_payg_30d_usd': None,
        'combined_30d_usd': None,
        'break_even_progress_pct': None,
        'net_monthly_savings_vs_1yr_gsu_usd': None,
    }
    spec = PT_GSU_SPECS.get(_model_key(name))
    if spec is None:
      row['sizing_basis'] = 'NO_PT_REFERENCE'
      row['recommendation'] = 'NO_PT_REFERENCE'
      row['recommendation_note'] = 'No published Provisioned Throughput rate for this model in the reference table.'
      model_rows.append(row)
      continue

    tps_per_gsu, out_mult, cache_mult = spec
    burndown_tps = (in_tok + cw_tok + cache_mult * cr_tok + out_mult * (out_tok + reason_tok)) / window_seconds
    avg_gsus = max(1, math.ceil(burndown_tps / tps_per_gsu))
    row.update({
        'gsu_throughput_per_gsu': tps_per_gsu,
        'burndown_tokens_per_second': round(burndown_tps, 2),
        'est_single_gsu_utilization_pct': round(burndown_tps / tps_per_gsu * 100.0, 1),
        'avg_gsu_units': avg_gsus,
    })

    raw_profile = m.get('throughput_profile')
    profile_sized = (
        throughput.optimize_gsu_count(
            raw_profile,
            tps_per_gsu=tps_per_gsu,
            gsu_1mo_usd=GSU_MONTHLY_1MO_USD,
            gsu_1yr_usd=GSU_MONTHLY_1YR_USD,
            payg_30d_usd=payg_30d_usd,
        )
        if isinstance(raw_profile, Mapping)
        else None
    )
    if profile_sized is not None:
      profile_sized_models += 1
      row.update({
          'sizing_basis': 'PER_MINUTE_PROFILE',
          'peak_burndown_tps': profile_sized['peak_burndown_tps'],
          'p99_burndown_tps': profile_sized['p99_burndown_tps'],
          'p95_burndown_tps': profile_sized['p95_burndown_tps'],
          'peak_gsu_units': profile_sized['peak_gsu_units'],
          'p99_gsu_units': profile_sized['p99_gsu_units'],
          'recommended_gsu_units': profile_sized['recommended_gsu_units'],
          'covered_tokens_share_pct': profile_sized['covered_tokens_share_pct'],
          'spillover_tokens_share_pct': profile_sized['spillover_tokens_share_pct'],
          'gsu_1mo_commitment_usd': profile_sized['gsu_1mo_commitment_usd'],
          'gsu_1yr_commitment_usd': profile_sized['gsu_1yr_commitment_usd'],
          'spillover_payg_30d_usd': profile_sized['spillover_payg_30d_usd'],
          'combined_30d_usd': profile_sized['combined_30d_usd'],
          'break_even_progress_pct': profile_sized['break_even_progress_pct'],
          'net_monthly_savings_vs_1yr_gsu_usd': profile_sized['net_monthly_savings_vs_1yr_gsu_usd'],
          'recommendation': profile_sized['recommendation'],
          'recommendation_note': profile_sized['recommendation_note'],
      })
      if profile_sized['recommendation'] == 'COMMIT_GSU_1YR':
        gsu_candidates_count += 1
      model_rows.append(row)
      continue

    gsus = avg_gsus
    gsu_1yr_cost = round(gsus * GSU_MONTHLY_1YR_USD, 2)
    row.update({
        'sizing_basis': 'WINDOW_AVERAGE',
        'recommended_gsu_units': gsus,
        'gsu_1mo_commitment_usd': round(gsus * GSU_MONTHLY_1MO_USD, 2),
        'gsu_1yr_commitment_usd': gsu_1yr_cost,
        'combined_30d_usd': gsu_1yr_cost,
    })
    if payg_30d_usd is None:
      row['recommendation'] = 'UNPRICED_MODEL'
      row['recommendation_note'] = 'No list-price rate card on file, so pay-as-you-go cost is unknown.'
    else:
      row['break_even_progress_pct'] = round(payg_30d_usd / gsu_1yr_cost * 100.0, 1)
      row['net_monthly_savings_vs_1yr_gsu_usd'] = round(payg_30d_usd - gsu_1yr_cost, 2)
      if payg_30d_usd >= gsu_1yr_cost:
        gsu_candidates_count += 1
        row['recommendation'] = 'COMMIT_GSU_1YR'
        row['recommendation_note'] = (
            f'30-day pay-as-you-go run-rate ${payg_30d_usd:,.0f} vs ${gsu_1yr_cost:,.0f}/month for {gsus} GSU '
            f'on a 1-year term (average-load sizing; size for peak before buying).'
        )
      else:
        row['recommendation'] = 'STAY_ON_PAYG'
        row['recommendation_note'] = (
            f'30-day pay-as-you-go run-rate ${payg_30d_usd:,.0f} is {row["break_even_progress_pct"]}% of '
            f'${gsu_1yr_cost:,.0f}/month for {gsus} GSU on a 1-year term.'
        )
    model_rows.append(row)

  model_rows.sort(key=lambda r: -(r.get('payg_30d_run_rate_usd') or 0.0))

  # 30 / 60 / 90-day projection from the observed burn rate (billing export first, telemetry list price second).
  daily_burn_usd: float | None = None
  burn_basis = f'Observed {window_hours}h Vertex AI token telemetry at list price'
  if isinstance(daily_usage, Mapping) and isinstance(daily_usage.get('days'), list):
    billed_days = [
        float(d['ai_net_usd'])
        for d in (daily_usage.get('days') or [])
        if isinstance(d, Mapping) and d.get('ai_net_usd') is not None
    ]
    if len(billed_days) >= 3:
      recent_7 = billed_days[-7:]
      daily_burn_usd = sum(recent_7) / len(recent_7)
      burn_basis = f'Cloud Billing export, average net AI spend of the last {len(recent_7)} billed days'

  if daily_burn_usd is None and total_payg_30d_usd > 0:
    daily_burn_usd = total_payg_30d_usd / 30.0

  total_tokens_window = sum(int(r['total_tokens_window']) for r in model_rows)
  daily_tokens = total_tokens_window * (24.0 / window_hours)
  sizing_basis = 'PER_MINUTE_PROFILE' if profile_sized_models > 0 else 'WINDOW_AVERAGE'

  return {
      'status': 'PROJECTION',
      'telemetry_source': str(
          usage.get('source') or 'Cloud Monitoring v3 aiplatform.googleapis.com/publisher/online_serving/token_count'
      ),
      'window_hours': window_hours,
      'burn_rate_basis': burn_basis,
      'daily_burn_rate_usd': round(daily_burn_usd, 2) if daily_burn_usd is not None else None,
      'forecast_30d_usd': round(daily_burn_usd * 30.0, 2) if daily_burn_usd is not None else None,
      'forecast_60d_usd': round(daily_burn_usd * 60.0, 2) if daily_burn_usd is not None else None,
      'forecast_90d_usd': round(daily_burn_usd * 90.0, 2) if daily_burn_usd is not None else None,
      'forecast_30d_tokens': round(daily_tokens * 30.0),
      'forecast_60d_tokens': round(daily_tokens * 60.0),
      'forecast_90d_tokens': round(daily_tokens * 90.0),
      'sizing_basis': sizing_basis,
      'sizing_caveat': GSU_SIZING_CAVEAT_PROFILE if sizing_basis == 'PER_MINUTE_PROFILE' else GSU_SIZING_CAVEAT,
      'gsu_reference': {
          'gsu_1mo_usd': GSU_MONTHLY_1MO_USD,
          'gsu_1yr_usd': GSU_MONTHLY_1YR_USD,
          'price_source': GSU_PRICE_SOURCE,
          'throughput_source': GSU_SPEC_SOURCE,
          'checked': reference_data.checked('provisioned_throughput'),
      },
      'gsu_candidates_count': gsu_candidates_count,
      'by_model': model_rows,
  }


# ---------------------------------------------------------------------------------------------
# 5. Model retirement & right-sizing radar
# ---------------------------------------------------------------------------------------------
# Model retirement dates and Google's listed replacements come from the versioned reference file
# (section model_lifecycle). Replacements that are themselves retiring are resolved at run time.
MODEL_LIFECYCLE_SOURCE = reference_data.source('model_lifecycle')
MODEL_LIFECYCLE: dict[str, tuple[str, tuple[str, ...]]] = reference_data.model_lifecycle()
RETIREMENT_WARNING_DAYS = 90
# Pro-tier traffic with short visible outputs is a right-sizing candidate. Google lists gemini-3.8-flash as the
# replacement for gemini-2.5-pro, so it is the comparison target (resolved through MODEL_LIFECYCLE if it retires).
TIER_DOWNGRADE_TARGET = 'gemini-3.8-flash'
SHORT_OUTPUT_PRO_THRESHOLD_TOKENS = 200
_PRO_TIER_RE = re.compile(r'(^|-)pro($|-)')


def _lifecycle(model_key: str, today: datetime.date) -> tuple[str, str, int] | None:
  """(status, retirement date, days left) when the model is retired or retires within the warning window."""
  entry = MODEL_LIFECYCLE.get(model_key)
  if entry is None:
    return None
  retire = datetime.date.fromisoformat(entry[0])
  days_left = (retire - today).days
  if days_left <= 0:
    return 'RETIRED', entry[0], days_left
  if days_left <= RETIREMENT_WARNING_DAYS:
    return 'RETIRING_SOON', entry[0], days_left
  return None


def _current_replacement(model_key: str, today: datetime.date, seen: set[str] | None = None) -> str | None:
  """First listed replacement that is not itself retired or retiring soon (follows replacement chains)."""
  seen = seen if seen is not None else {model_key}
  for cand in MODEL_LIFECYCLE.get(model_key, ('', ()))[1]:
    if cand in seen:
      continue
    seen.add(cand)
    if _lifecycle(cand, today) is None:
      return cand
    deeper = _current_replacement(cand, today, seen)
    if deeper:
      return deeper
  return None


def _signed_usd(v: float) -> str:
  return f'{"+" if v >= 0 else "-"}${abs(v):,.2f}'


def build_model_modernization_radar(
    fleet: Mapping[str, Any] | None,
    today: datetime.date | None = None,
    token_economics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Flags retired / soon-retiring models (with Google's listed replacement) and short-output Pro traffic."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else None
  if usage is None:
    return {'status': 'NO_DATA', 'reason': 'Vertex AI model usage metrics are not available.'}

  today = today or datetime.datetime.now(datetime.UTC).date()
  cards = _resolve_rate_cards_map(usage)
  window_hours = max(1, int(fleet.get('window_hours') or 24))
  scale_30d = 720.0 / window_hours
  downgrade_target = (
      TIER_DOWNGRADE_TARGET if _lifecycle(TIER_DOWNGRADE_TARGET, today) is None
      else _current_replacement(TIER_DOWNGRADE_TARGET, today)
  )

  findings: list[dict[str, Any]] = []
  rightsizing_savings_window_usd = 0.0
  retirement_change_window_usd = 0.0
  priced_retirements = 0
  retiring_models = 0

  def _finding(scope: str, subject: str, current: str, calls: int, avg_out: int | None,
               cur_cost: float | None, tgt_cost: float | None, target: str | None) -> dict[str, Any]:
    change = round(tgt_cost - cur_cost, 4) if cur_cost is not None and tgt_cost is not None else None
    return {
        'scope': scope,
        'subject': subject,
        'current_model': current,
        'recommended_model': target,
        'calls': calls,
        'avg_output_tokens_per_call': avg_out,
        'current_cost_window_usd': round(cur_cost, 4) if cur_cost is not None else None,
        'target_cost_window_usd': round(tgt_cost, 4) if tgt_cost is not None else None,
        'cost_change_window_usd': change,
        'cost_change_30d_usd': round(change * scale_30d, 2) if change is not None else None,
        'savings_window_usd': round(max(0.0, -change), 4) if change is not None else None,
        'savings_30d_usd': round(max(0.0, -change) * scale_30d, 2) if change is not None else None,
    }

  def _retirement(base: dict[str, Any], life: tuple[str, str, int], key: str) -> dict[str, Any]:
    status, date, days_left = life
    target = base['recommended_model']
    change_30d = base['cost_change_30d_usd']
    when = f'retired on {date}' if status == 'RETIRED' else f'retires on {date} ({days_left} days)'
    cost_txt = (f' Same tokens on {target} at list price: {_signed_usd(change_30d)}/30d.'
                if change_30d is not None else ' No list price on file for the replacement.')
    base.update({
        'finding_kind': 'MODEL_RETIRED' if status == 'RETIRED' else 'MODEL_RETIRING_SOON',
        'severity': 'HIGH',
        'retirement_date': date,
        'days_until_retirement': days_left,
        'requires_quality_eval': True,
        'rationale': (f'{key} {when}; Google lists {target} as the replacement.{cost_txt}' if target
                      else f'{key} {when}; no current replacement is listed in the lifecycle table.'),
    })
    return base

  for m in usage.get('models') or []:
    if not isinstance(m, Mapping):
      continue
    model_name = str(m.get('model') or '')
    key = _model_key(model_name)
    _, src_card = rate_card_for(model_name, cards)
    calls = int(m.get('invocations') or 0)
    out_tok = int(m.get('output_tokens') or 0)
    if calls <= 0 and int(m.get('input_tokens') or 0) + out_tok <= 0:
      continue
    raw_est = m.get('est_cost_usd')
    cur_cost = float(raw_est) if raw_est is not None else (_cost(m, src_card) if src_card else None)
    avg_out = round(out_tok / calls) if calls > 0 else None

    life = _lifecycle(key, today)
    if life is not None:  # Retirement first; it is the only finding for this model (no double counting).
      target = _current_replacement(key, today)
      tgt_card = rate_card_for(target, cards)[1] if target else None
      tgt_cost = _cost(m, tgt_card) if tgt_card is not None else None
      f = _retirement(_finding('PROJECT_MODEL', key, key, calls, avg_out, cur_cost, tgt_cost, target), life, key)
      f['migration_snippet'] = (
          f'# {_one_line(key)} -> {target} (Vertex AI model lifecycle)\nagent = Agent(name="...", model="{target}")'
          if target else None)
      retiring_models += 1
      if f['cost_change_window_usd'] is not None:
        priced_retirements += 1
        retirement_change_window_usd += f['cost_change_window_usd']
      findings.append(f)
      continue

    if (_PRO_TIER_RE.search(key) and downgrade_target and cur_cost is not None and cur_cost > 0
        and avg_out is not None and avg_out <= SHORT_OUTPUT_PRO_THRESHOLD_TOKENS):
      tgt_card = rate_card_for(downgrade_target, cards)[1]
      if tgt_card is None:
        continue
      f = _finding('PROJECT_MODEL', key, key, calls, avg_out, cur_cost, _cost(m, tgt_card), downgrade_target)
      if not f['savings_window_usd']:
        continue
      pct = round(f['savings_window_usd'] / cur_cost * 100.0)
      rightsizing_savings_window_usd += f['savings_window_usd']
      f.update({
          'finding_kind': 'OVER_TIERED_SHORT_OUTPUT',
          'severity': 'MEDIUM',
          'retirement_date': None,
          'days_until_retirement': None,
          'requires_quality_eval': True,
          'rationale': (
              f'{key} averages {avg_out} visible output tokens per call over {calls:,} calls; the same tokens '
              f'on {downgrade_target} cost {pct}% less at list price. Run an eval on these prompts first.'
          ),
          'migration_snippet': f'# Right-size {_one_line(key)} -> {downgrade_target}\nagent = Agent(name="...", model="{downgrade_target}")',
      })
      findings.append(f)

  # Per agent runtime: the same two checks on the agent's model, priced from its own token counts.
  te = token_economics if isinstance(token_economics, Mapping) else build_token_economics(fleet)
  for ag in te.get('agents') or []:
    if not isinstance(ag, Mapping):
      continue
    models_list = ag.get('models') or []
    if not models_list:
      continue
    cur_model, src_card = rate_card_for(models_list[0], cards)
    key = _model_key(cur_model)
    calls = int(ag.get('llm_calls') or 0)
    in_tok = int(ag.get('input_tokens') or 0)
    out_tok = int(ag.get('output_tokens') or 0)
    cached_tok = int(ag.get('cached_tokens') or 0)
    if in_tok + out_tok <= 0:
      continue
    avg_out_ag = round(out_tok / calls) if calls > 0 else None
    agent_name = str(ag.get('display_name') or 'agent')
    cur_cost_ag = tokens_cost_usd(in_tok, out_tok, cached_tok, src_card) if src_card else None
    life = _lifecycle(key, today)
    if life is not None:
      target = _current_replacement(key, today)
    elif (_PRO_TIER_RE.search(key) and avg_out_ag is not None and 0 < avg_out_ag <= SHORT_OUTPUT_PRO_THRESHOLD_TOKENS):
      target = downgrade_target
    else:
      continue
    tgt_card = rate_card_for(target, cards)[1] if target else None
    tgt_cost_ag = tokens_cost_usd(in_tok, out_tok, cached_tok, tgt_card) if tgt_card else None
    f = _finding('AGENT_RUNTIME', agent_name, key, calls, avg_out_ag, cur_cost_ag, tgt_cost_ag, target)
    f['engine_id'] = ag.get('engine_id')
    if life is not None:
      f = _retirement(f, life, key)
      f['finding_kind'] = 'AGENT_MODEL_RETIRED' if life[0] == 'RETIRED' else 'AGENT_MODEL_RETIRING_SOON'
      f['rationale'] = f'Agent "{_one_line(agent_name)}": ' + f['rationale']
    else:
      if not f['savings_window_usd'] or cur_cost_ag is None:
        continue
      pct = round(f['savings_window_usd'] / cur_cost_ag * 100.0)
      f.update({
          'finding_kind': 'AGENT_OVER_TIERED_SHORT_OUTPUT',
          'severity': 'MEDIUM',
          'retirement_date': None,
          'days_until_retirement': None,
          'requires_quality_eval': True,
          'rationale': (
              f'Agent "{_one_line(agent_name)}" runs {key} with {avg_out_ag} output tokens per call over '
              f'{calls:,} LLM calls; {target} costs {pct}% less for these tokens at list price. Eval first.'
          ),
      })
    f['migration_snippet'] = (
        f'# {_one_line(agent_name)}: {_one_line(key)} -> {target}\n'
        f'agent = Agent(name="{_py_identifier(agent_name)}", model="{target}")' if target else None)
    findings.append(f)

  severity_rank = {'HIGH': 0, 'MEDIUM': 1}
  findings.sort(key=lambda r: (r['scope'] != 'PROJECT_MODEL', severity_rank.get(r['severity'], 2),
                               -(r.get('savings_30d_usd') or 0.0)))
  return {
      'status': 'LIVE',
      'window_hours': window_hours,
      'as_of': today.isoformat(),
      'finding_count': len(findings),
      'retiring_model_count': retiring_models,
      'retirement_cost_change_30d_usd': (
          round(retirement_change_window_usd * scale_30d, 2) if priced_retirements > 0 else None
      ),
      # Right-sizing only, one finding per model, so the total never double counts the same traffic.
      'project_model_savings_window_usd': round(rightsizing_savings_window_usd, 4),
      'project_model_savings_30d_usd': round(rightsizing_savings_window_usd * scale_30d, 2),
      'lifecycle_source': MODEL_LIFECYCLE_SOURCE,
      'lifecycle_checked': reference_data.checked('model_lifecycle'),
      'findings': findings,
  }
