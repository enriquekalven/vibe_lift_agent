"""Per-minute Vertex AI token throughput profiles and Provisioned Throughput (GSU) sizing.

Provisioned Throughput (GSUs) caps tokens per second at the moment requests arrive; tokens above the
purchased GSU capacity spill over to pay-as-you-go. Window averages hide diurnal peaks and short
bursts, over-stating how much traffic a 1-GSU commitment actually covers. This module bins raw
60-second Cloud Monitoring `token_count` points into per-minute burndown-token rates and sizes GSUs
against the full minute distribution (evaluating base GSU commitment + pay-as-you-go spillover).
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping, Sequence
from typing import Any

from finops_dashboard import reference_data

BUCKET_S = 60
THROUGHPUT_WINDOW_MAX_S = 24 * 3600  # 1,440 one-minute buckets (one full diurnal cycle)


def classify_token_type(raw_type: object) -> str:
  """Maps a Cloud Monitoring `token_count` metric.label.type value to the canonical token key."""
  token_type = str(raw_type or '').lower()
  if token_type == 'input':
    return 'input_tokens'
  if token_type == 'output':
    return 'output_tokens'
  if token_type in ('thought', 'thoughts', 'reasoning') or token_type.startswith(('thought', 'reasoning')):
    return 'reasoning_tokens'
  if 'cache_read' in token_type or token_type.startswith('cached'):
    return 'cache_read_tokens'
  if token_type.startswith('cache_write'):
    return 'cache_write_tokens'
  return 'other_tokens'


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
  """Linearly interpolated quantile (0 <= q <= 1) of a non-empty sorted list."""
  n = len(sorted_vals)
  if n == 1:
    return float(sorted_vals[0])
  pos = max(0.0, min(1.0, q)) * (n - 1)
  lo = math.floor(pos)
  hi = min(n - 1, lo + 1)
  frac = pos - lo
  return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def _parse_epoch(ts: object) -> int | None:
  if isinstance(ts, (int, float)):
    return int(ts)
  text = str(ts or '').strip()
  if not text:
    return None
  try:
    return int(datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp())
  except ValueError:
    return None


def build_model_throughput_profiles(
    series_list: Sequence[Mapping[str, Any]] | None,
    *,
    window_s: int,
    end_epoch: int | None = None,
) -> dict[str, dict[str, Any]]:
  """Bins raw 60 s `token_count` timeSeries into per-minute burndown-TPS profiles per model.

  Series points without `interval.endTime` (such as pre-aggregated test stubs) are ignored so callers
  automatically fall back to window-average sizing when per-minute timestamps are unavailable.
  """
  if not series_list:
    return {}
  profile_window_s = max(BUCKET_S, min(int(window_s or THROUGHPUT_WINDOW_MAX_S), THROUGHPUT_WINDOW_MAX_S))
  window_minutes = max(1, profile_window_s // BUCKET_S)

  by_model_slot: dict[str, dict[int, dict[str, int]]] = {}
  latest_slot_seen: int | None = (int(end_epoch) // BUCKET_S) if end_epoch is not None else None

  for series in series_list:
    if not isinstance(series, Mapping):
      continue
    res_labels = (series.get('resource') or {}).get('labels') or {}
    met_labels = (series.get('metric') or {}).get('labels') or {}
    model = str(res_labels.get('model_user_id') or '').strip()
    if not model:
      continue
    tok_key = classify_token_type(met_labels.get('type'))
    for pt in series.get('points') or []:
      if not isinstance(pt, Mapping):
        continue
      interval = pt.get('interval')
      end_time = interval.get('endTime') if isinstance(interval, Mapping) else None
      epoch = _parse_epoch(end_time)
      if epoch is None:
        continue
      raw_val = pt.get('value')
      val = raw_val if isinstance(raw_val, Mapping) else {}
      raw_num = val.get('int64Value', val.get('doubleValue'))
      if raw_num is None:
        continue
      count = round(float(raw_num))
      if count <= 0:
        continue
      slot = epoch // BUCKET_S
      if latest_slot_seen is None or slot > latest_slot_seen:
        latest_slot_seen = slot
      slot_bucket = by_model_slot.setdefault(model, {}).setdefault(
          slot,
          {
              'input_tokens': 0,
              'output_tokens': 0,
              'reasoning_tokens': 0,
              'cache_read_tokens': 0,
              'cache_write_tokens': 0,
              'other_tokens': 0,
          },
      )
      slot_bucket[tok_key] = slot_bucket.get(tok_key, 0) + count

  if not by_model_slot or latest_slot_seen is None:
    return {}

  pt_specs = reference_data.pt_specs()
  end_slot = (int(end_epoch) // BUCKET_S) if end_epoch is not None else latest_slot_seen
  start_slot = end_slot - window_minutes + 1

  profiles: dict[str, dict[str, Any]] = {}
  for model, slots in by_model_slot.items():
    spec = pt_specs.get(reference_data.model_key(model))
    out_mult = float(spec[1]) if spec is not None else 1.0
    cache_mult = float(spec[2]) if spec is not None else 1.0

    active_tps: list[float] = []
    for slot, counts in slots.items():
      if slot < start_slot or slot > end_slot:
        continue
      in_tok = max(0, int(counts.get('input_tokens') or 0))
      cw_tok = max(0, int(counts.get('cache_write_tokens') or 0))
      cr_tok = max(0, int(counts.get('cache_read_tokens') or 0))
      out_tok = max(0, int(counts.get('output_tokens') or 0))
      reason_tok = max(0, int(counts.get('reasoning_tokens') or 0))
      burndown = in_tok + cw_tok + cache_mult * cr_tok + out_mult * (out_tok + reason_tok)
      if burndown > 0:
        active_tps.append(burndown / float(BUCKET_S))

    if not active_tps:
      continue
    active_tps.sort()
    inactive_count = max(0, window_minutes - len(active_tps))
    dense_tps = ([0.0] * inactive_count) + active_tps
    total_minutes = len(dense_tps)
    mean_tps = sum(active_tps) / float(total_minutes)
    profiles[model] = {
        'source': 'Cloud Monitoring v3 aiplatform.googleapis.com/publisher/online_serving/token_count (60s raw points)',
        'resolution_s': BUCKET_S,
        'window_minutes': total_minutes,
        'active_minutes': len(active_tps),
        'peak_burndown_tps': round(dense_tps[-1], 2),
        'p99_burndown_tps': round(_quantile(dense_tps, 0.99), 2),
        'p95_burndown_tps': round(_quantile(dense_tps, 0.95), 2),
        'p50_burndown_tps': round(_quantile(dense_tps, 0.50), 2),
        'mean_burndown_tps': round(mean_tps, 2),
        'quantiles_tps': [round(_quantile(dense_tps, i / 100.0), 4) for i in range(101)],
        'active_minute_tps': [round(v, 4) for v in active_tps],
    }
  return profiles


def _samples_from_profile(profile: Mapping[str, Any]) -> tuple[list[float], int]:
  """Returns (non-zero minute burndown TPS samples, total window minutes) from a profile dict."""
  window_minutes = max(1, int(profile.get('window_minutes') or 1440))
  active = profile.get('active_minute_tps')
  if isinstance(active, Sequence) and not isinstance(active, (str, bytes)):
    vals = [max(0.0, float(v)) for v in active if v is not None and float(v) > 0]
    if vals:
      return sorted(vals), max(window_minutes, len(vals))
  quantiles = profile.get('quantiles_tps')
  if isinstance(quantiles, Sequence) and not isinstance(quantiles, (str, bytes)):
    vals = [max(0.0, float(v)) for v in quantiles if v is not None and float(v) > 0]
    if vals:
      return sorted(vals), max(len(quantiles), len(vals))
  peak = float(profile.get('peak_burndown_tps') or 0.0)
  if peak > 0:
    return [peak], window_minutes
  return [], window_minutes


def optimize_gsu_count(
    profile: Mapping[str, Any],
    *,
    tps_per_gsu: float,
    gsu_1mo_usd: float,
    gsu_1yr_usd: float,
    payg_30d_usd: float | None,
) -> dict[str, Any] | None:
  """Sizes GSUs against a per-minute burndown-TPS profile, accounting for PAYG spillover above cap.

  For each candidate commitment N in 1..peak_gsu_units, minutes below N * tps_per_gsu are covered by
  Provisioned Throughput and excess tokens in burst minutes spill over to pay-as-you-go:
    combined_30d_usd(N) = N * gsu_1yr_usd + payg_30d_usd * spillover_share(N)
  """
  if not isinstance(profile, Mapping) or tps_per_gsu <= 0:
    return None
  active_samples, window_minutes = _samples_from_profile(profile)
  if not active_samples:
    return None
  total_sample_tps = sum(active_samples)
  if total_sample_tps <= 0:
    return None

  inactive_count = max(0, window_minutes - len(active_samples))
  dense = ([0.0] * inactive_count) + active_samples
  peak_tps = float(profile.get('peak_burndown_tps') or dense[-1])
  p99_tps = float(profile.get('p99_burndown_tps') or _quantile(dense, 0.99))
  p95_tps = float(profile.get('p95_burndown_tps') or _quantile(dense, 0.95))
  mean_tps = float(profile.get('mean_burndown_tps') or (total_sample_tps / len(dense)))

  peak_gsus = max(1, math.ceil(peak_tps / tps_per_gsu - 1e-9))
  p99_gsus = max(1, math.ceil(p99_tps / tps_per_gsu - 1e-9))
  avg_gsus = max(1, math.ceil(mean_tps / tps_per_gsu - 1e-9))

  def spillover_share(units: int) -> float:
    cap = units * tps_per_gsu
    if cap <= 0:
      return 1.0
    excess = sum(max(0.0, v - cap) for v in active_samples)
    return max(0.0, min(1.0, excess / total_sample_tps))

  if payg_30d_usd is None:
    chosen_units = p99_gsus
    spill = spillover_share(chosen_units)
    return {
        'sizing_basis': 'PER_MINUTE_PROFILE',
        'window_minutes': window_minutes,
        'active_minutes': int(profile.get('active_minutes') or len(active_samples)),
        'peak_burndown_tps': round(peak_tps, 2),
        'p99_burndown_tps': round(p99_tps, 2),
        'p95_burndown_tps': round(p95_tps, 2),
        'mean_burndown_tps': round(mean_tps, 2),
        'peak_gsu_units': peak_gsus,
        'p99_gsu_units': p99_gsus,
        'avg_gsu_units': avg_gsus,
        'recommended_gsu_units': chosen_units,
        'covered_tokens_share_pct': round((1.0 - spill) * 100.0, 1),
        'spillover_tokens_share_pct': round(spill * 100.0, 1),
        'gsu_1mo_commitment_usd': round(chosen_units * gsu_1mo_usd, 2),
        'gsu_1yr_commitment_usd': round(chosen_units * gsu_1yr_usd, 2),
        'spillover_payg_30d_usd': None,
        'combined_30d_usd': None,
        'break_even_progress_pct': None,
        'net_monthly_savings_vs_1yr_gsu_usd': None,
        'recommendation': 'UNPRICED_MODEL',
        'recommendation_note': 'No list-price rate card on file, so pay-as-you-go cost is unknown.',
    }

  # Evaluate N in 1..peak_gsus to find the lowest-cost GSU + PAYG-spillover plan.
  max_search = max(1, min(peak_gsus, 500))
  best_units = 1
  best_spill = spillover_share(1)
  best_combined = gsu_1yr_usd + payg_30d_usd * best_spill
  for units in range(2, max_search + 1):
    spill = spillover_share(units)
    combined = units * gsu_1yr_usd + payg_30d_usd * spill
    if combined + 1e-6 < best_combined:
      best_units = units
      best_spill = spill
      best_combined = combined

  covered_pct = round((1.0 - best_spill) * 100.0, 1)
  spill_pct = round(best_spill * 100.0, 1)
  gsu_1yr_cost = round(best_units * gsu_1yr_usd, 2)
  gsu_1mo_cost = round(best_units * gsu_1mo_usd, 2)
  spill_usd = round(payg_30d_usd * best_spill, 2)
  combined_usd = round(gsu_1yr_cost + spill_usd, 2)
  net_savings = round(payg_30d_usd - combined_usd, 2)
  break_even_pct = round(payg_30d_usd / combined_usd * 100.0, 1) if combined_usd > 0 else None

  if net_savings > 0:
    rec = 'COMMIT_GSU_1YR'
    spill_clause = (
        f' + ${spill_usd:,.0f}/month PAYG spillover ({spill_pct}% of tokens in bursts above {best_units} GSU)'
        if spill_usd >= 1.0
        else ' (covers ~100% of minute peaks)'
    )
    note = (
        f'Per-minute sizing: {best_units} GSU (${gsu_1yr_cost:,.0f}/month 1-yr){spill_clause} = '
        f'${combined_usd:,.0f}/month vs ${payg_30d_usd:,.0f}/month on PAYG (p99 load {p99_gsus} GSU, peak {peak_gsus} GSU).'
    )
  else:
    rec = 'STAY_ON_PAYG'
    if best_spill >= 0.05:
      note = (
          f'Per-minute sizing: traffic bursts to {p99_gsus} GSU at p99 ({peak_gsus} GSU peak vs {avg_gsus} GSU avg). '
          f'{best_units} GSU (${gsu_1yr_cost:,.0f}/month) still leaves {spill_pct}% of tokens spilling to PAYG '
          f'(+${spill_usd:,.0f}/month = ${combined_usd:,.0f}/month total vs ${payg_30d_usd:,.0f}/month on PAYG).'
      )
    else:
      note = (
          f'30-day pay-as-you-go run-rate ${payg_30d_usd:,.0f} is {break_even_pct}% of '
          f'${combined_usd:,.0f}/month for {best_units} GSU on a 1-year term (p99 {p99_gsus} GSU, peak {peak_gsus} GSU).'
      )

  return {
      'sizing_basis': 'PER_MINUTE_PROFILE',
      'window_minutes': window_minutes,
      'active_minutes': int(profile.get('active_minutes') or len(active_samples)),
      'peak_burndown_tps': round(peak_tps, 2),
      'p99_burndown_tps': round(p99_tps, 2),
      'p95_burndown_tps': round(p95_tps, 2),
      'mean_burndown_tps': round(mean_tps, 2),
      'peak_gsu_units': peak_gsus,
      'p99_gsu_units': p99_gsus,
      'avg_gsu_units': avg_gsus,
      'recommended_gsu_units': best_units,
      'covered_tokens_share_pct': covered_pct,
      'spillover_tokens_share_pct': spill_pct,
      'gsu_1mo_commitment_usd': gsu_1mo_cost,
      'gsu_1yr_commitment_usd': gsu_1yr_cost,
      'spillover_payg_30d_usd': spill_usd,
      'combined_30d_usd': combined_usd,
      'break_even_progress_pct': break_even_pct,
      'net_monthly_savings_vs_1yr_gsu_usd': net_savings,
      'recommendation': rec,
      'recommendation_note': note,
  }
