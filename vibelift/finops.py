"""Live token economics, spend-change breakdown and what-if projections.

Every number here is computed from observed Cloud Monitoring telemetry (the ge_fleet payload) and the
list-price rate cards used elsewhere in VibeLift. Nothing is assumed or seeded:

* Token economics: per-model and per-agent token, call and cost figures, exactly as observed.
* Spend change: this period vs the previous period of equal length, split into drivers with an
  exact additive decomposition, so the drivers always sum to the observed change.
* What-if: arithmetic projections on the observed baseline (switch a share of one model's tokens to
  another model's list price, or change the cache-read share). Stated assumptions: token counts stay
  the same; quality and latency are not modelled.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
_PRICE_KEY = {
    'input_tokens': 'input',
    'output_tokens': 'output',
    'cache_read_tokens': 'cached_read',
    'cache_write_tokens': 'cache_write',
}
DRIVERS = (
    ('volume', 'Call volume', 'More or fewer model calls at the previous cost per call.'),
    ('prompt_size', 'Prompt size', 'Change in uncached input tokens per call.'),
    ('output_length', 'Output length', 'Change in output tokens per call.'),
    ('caching', 'Caching', 'Change in cache-read and cache-write tokens per call.'),
    ('model_mix', 'Models started or stopped', 'Models used in only one of the two periods.'),
)


def _cost(row: Mapping[str, Any], card: Mapping[str, float]) -> float:
  return sum(float(row.get(k) or 0) / 1e6 * float(card.get(_PRICE_KEY[k]) or 0) for k in _TOKEN_KEYS)


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
  from vibelift.fleet import runtime_backend_key  # pylint: disable=g-import-not-at-top
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
  model used in both periods with calls N and per-call tokens a (input), b (output), c (cache read),
  w (cache write):
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
    card = cards.get(name)
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
      parts['output_length'] = n1 * (per(r1, 'output_tokens', n1) - per(r0, 'output_tokens', n0)) * card['output'] / 1e6
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
    rate_cards: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
  """Projects spend if share_pct of from_model's observed tokens were billed at to_model's list price."""
  models = _models_by_name(usage)
  src = models.get(from_model)
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
    rate_cards: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
  """Projects spend if model's cache-read share of prompt tokens were target_cache_share_pct."""
  src = _models_by_name(usage).get(model)
  card = rate_cards.get(model)
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


def build_live_finops(fleet: Mapping[str, Any] | None) -> dict[str, Any]:
  """Bundle for /api/state in live mode."""
  fleet = fleet if isinstance(fleet, Mapping) else {}
  usage = fleet.get('model_usage')
  return {
      'token_economics': build_token_economics(fleet),
      'spend_drift': build_spend_drift(usage, fleet.get('model_usage_previous')),
      'what_if_models': [m.get('model') for m in (usage or {}).get('models') or [] if m.get('est_cost_usd') is not None],
      'rate_card_models': sorted(((usage or {}).get('rate_cards') or {}).keys()),
      'unregistered_summary': fleet.get('unregistered_summary') or {},
      'unregistered_runtimes': fleet.get('unregistered_runtimes') or [],
      'gke_workloads': fleet.get('gke_workloads') or {'clusters': [], 'workloads': []},
      'skills_and_mcp': fleet.get('skills_and_mcp') or [],
  }

