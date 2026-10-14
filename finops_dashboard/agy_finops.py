"""Google Antigravity FinOps collector, WTU allowance meter, and PAYG chargeback engine.

Integrates the `cloud-gtm/agy-finops` telemetry and metering model into FinOps Dashboard:
- Reads Antigravity model calls from Cloud Logging (`businessaicode.googleapis.com/inference_response`)
  or from BigQuery (`ds_antigravity_raw` / `agy_finops.calls`) using a strict metadata allowlist so
  prompt and response text never leaves Cloud Logging.
- Reads Gemini Enterprise weekly seat allowance meters from Cloud Monitoring
  (`discoveryengine.googleapis.com/quota/ratev2/agentic_wtu_tier_*_pp_weekly_regional`) and seat
  configurations/assignments from the Discovery Engine License API (`licenseConfigs`, `userLicenses`).
- Computes Weighted Token Units (WTU, $2 per million, 50-WTU minimum pre-check per call), model
  meter shares (`0.5` for Gemini 3.6/3.7/3.8 Flash and Gemini 4 Argon; `1.0` for Pro/Lite),
  floating 7-day allowance windows, and per-user PAYG chargeback (`fair_share` vs. `when_used`).
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from finops_dashboard import ge_mart

logger = logging.getLogger(__name__)

LOG_ID = 'businessaicode.googleapis.com/inference_response'
MONITORING_API = 'https://monitoring.googleapis.com/v3'
LOGGING_API = 'https://logging.googleapis.com/v2'
WTU_METER_PREFIX = 'discoveryengine.googleapis.com/quota/ratev2/'
WTU_USD = 2e-6  # 1 WTU = $0.000002 ($2 per 1M WTU; 500,000 WTU per $1)
WTU_PER_USD = 500_000
MIN_CALL_WTU = 50
WINDOW_DAYS = 7
WINDOW_SECONDS = 7 * 24 * 3600
LONG_CONTEXT_THRESHOLD = 200_000

# Strict allowlists ported from cloud-gtm/agy-finops (finops/stream/strip_text.js & to_call.js).
# Prompt and response text is stripped before any processing or storage.
ENTRY_FIELDS: tuple[str, ...] = (
    'insertId',
    'timestamp',
    'receiveTimestamp',
    'logName',
    'severity',
    'labels',
    'resource',
    'jsonPayload',
)
PAYLOAD_FIELDS: tuple[str, ...] = ('@type', 'experience', 'metadata')
METADATA_FIELDS: tuple[str, ...] = (
    'promptTokenCount',
    'cachedContentTokenCount',
    'candidatesTokenCount',
    'thoughtsTokenCount',
    'toolUsePromptTokenCount',
    'totalTokenCount',
    'quotaType',
    'trafficType',
    'promptTokensDetails',
    'cacheTokensDetails',
    'candidatesTokensDetails',
    'toolUsePromptTokensDetails',
)
LABEL_FIELDS: tuple[str, ...] = (
    'user_id',
    'model',
    'model_provider',
    'client_name',
    'client_version',
    'trajectory_id',
    'request_id',
)

# Upstream default (cloud-gtm/agy-finops config.env.example TIER_ALLOWANCES): weekly included usage per seat
# as enum:name:usd (monthly per-seat amount / 4: Plus $15 -> 3.75, Standard $10 -> 2.50). Enums sharing a
# name share one pool. Override with FINOPS_AGY_TIER_ALLOWANCES; a tier not listed has no allowance ($0).
DEFAULT_TIER_ALLOWANCES = (
    'SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT:Plus:3.75,'
    'SUBSCRIPTION_TIER_ENTERPRISE:Standard:2.50,'
    'SUBSCRIPTION_TIER_PLUS:Plus:3.75,'
    'SUBSCRIPTION_TIER_STANDARD:Standard:2.50'
)
# Discovery Engine v1 LicenseConfig.state values that cannot cover today's pool. Upstream applies each
# config's start/end dates; EXPIRED and NOT_STARTED are the state-level equivalent for "now".
INACTIVE_CONFIG_STATES = frozenset({'WITHDRAWN', 'EXPIRED', 'NOT_STARTED'})
# Only this UserLicense.licenseAssignmentState holds a seat (not UNASSIGNED, NO_LICENSE,
# NO_LICENSE_ATTEMPTED_LOGIN or BLOCKED), as in upstream's `assignment_state = 'ASSIGNED'`.
ASSIGNED_STATE = 'ASSIGNED'
UNKNOWN_TIER = 'Unknown tier'
LICENSED_UNKNOWN_TIER = 'Licensed (tier unknown)'

_POOL_METRIC_RE = re.compile(r'agentic_wtu_tier_(?P<tier>[a-z_]+?)_pp_weekly_regional$')
_METER_TIER_NAMES: dict[str, str] = {
    'enterprise_plus': 'Plus',
    'enterprise_standard': 'Standard',
    'plus': 'Plus',
    'standard': 'Standard',
}
_EFFORT_RE = re.compile(r'-(minimal|low|medium|high|xhigh|max)$', re.IGNORECASE)
_MODEL_SUFFIX_RE = re.compile(r'(-preview.*|-exp.*|-\d{3})$', re.IGNORECASE)
_LOG_PROJECT_RE = re.compile(r'^projects/([^/]+)/logs/')
_SUBJECT_RE = re.compile(r'/subject/([^/]+)$')


@dataclasses.dataclass(frozen=True)
class AgyModelRate:
  """Per-1M token list prices (USD) and Gemini Enterprise WTU meter share for an Antigravity model."""

  model: str
  provider: str  # 'Google' | 'Anthropic'
  input_per_m: float
  cached_per_m: float
  output_per_m: float
  list_share: float = 1.0
  long_threshold: int | None = None
  long_input_per_m: float | None = None
  long_cached_per_m: float | None = None
  long_output_per_m: float | None = None
  meter_long_threshold: int | None = None
  long_input_share: float | None = None
  long_cached_share: float | None = None
  long_output_share: float | None = None


# List prices (USD per 1M tokens) and WTU meter shares from cloud-gtm/agy-finops (meter_rates.csv & catalog).
# Note: Anthropic models never draw on the weekly allowance pool and price uncached input at the
# 5-minute cache-write rate (1.25x input) because Antigravity caches prompts without logging cache-write counts.
DEFAULT_AGY_RATES: dict[str, AgyModelRate] = {
    'gemini-3.8-flash': AgyModelRate(
        model='gemini-3.8-flash',
        provider='Google',
        input_per_m=0.30,
        cached_per_m=0.03,
        output_per_m=2.50,
        list_share=0.5,
    ),
    'gemini-3.7-flash': AgyModelRate(
        model='gemini-3.7-flash',
        provider='Google',
        input_per_m=0.30,
        cached_per_m=0.03,
        output_per_m=2.50,
        list_share=0.5,
    ),
    'gemini-3.6-flash': AgyModelRate(
        model='gemini-3.6-flash',
        provider='Google',
        input_per_m=0.30,
        cached_per_m=0.03,
        output_per_m=2.50,
        list_share=0.5,
    ),
    'gemini-3.5-flash-lite': AgyModelRate(
        model='gemini-3.5-flash-lite',
        provider='Google',
        input_per_m=0.10,
        cached_per_m=0.01,
        output_per_m=0.40,
        list_share=1.0,
    ),
    'gemini-3.1-pro': AgyModelRate(
        model='gemini-3.1-pro',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=1.0,
        long_threshold=200_000,
        long_input_per_m=2.50,
        long_cached_per_m=0.25,
        long_output_per_m=15.00,
    ),
    'gemini-3.0-pro': AgyModelRate(
        model='gemini-3.0-pro',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=1.0,
        long_threshold=200_000,
        long_input_per_m=2.50,
        long_cached_per_m=0.25,
        long_output_per_m=15.00,
    ),
    'gemini-3-pro': AgyModelRate(
        model='gemini-3-pro',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=1.0,
        long_threshold=200_000,
        long_input_per_m=2.50,
        long_cached_per_m=0.25,
        long_output_per_m=15.00,
    ),
    'gemini-3.1-flash': AgyModelRate(
        model='gemini-3.1-flash',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=5.00,
        list_share=1.0,
    ),
    'gemini-3.1-flash-lite': AgyModelRate(
        model='gemini-3.1-flash-lite',
        provider='Google',
        input_per_m=0.50,
        cached_per_m=0.05,
        output_per_m=2.00,
        list_share=1.0,
    ),
    'gemini-2.5-pro': AgyModelRate(
        model='gemini-2.5-pro',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=1.0,
        long_threshold=200_000,
        long_input_per_m=2.50,
        long_cached_per_m=0.25,
        long_output_per_m=15.00,
    ),
    'gemini-2.5-flash': AgyModelRate(
        model='gemini-2.5-flash',
        provider='Google',
        input_per_m=0.30,
        cached_per_m=0.03,
        output_per_m=2.50,
        list_share=1.0,
    ),
    'gemini-2.5-flash-lite': AgyModelRate(
        model='gemini-2.5-flash-lite',
        provider='Google',
        input_per_m=0.10,
        cached_per_m=0.01,
        output_per_m=0.40,
        list_share=1.0,
    ),
    'gemini-4-argon': AgyModelRate(
        model='gemini-4-argon',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=0.5,
        meter_long_threshold=200_000,
        long_input_share=1.0,
        long_cached_share=1.0,
        long_output_share=0.75,
    ),
    'gemini-4.0-argon': AgyModelRate(
        model='gemini-4.0-argon',
        provider='Google',
        input_per_m=1.25,
        cached_per_m=0.125,
        output_per_m=10.00,
        list_share=0.5,
        meter_long_threshold=200_000,
        long_input_share=1.0,
        long_cached_share=1.0,
        long_output_share=0.75,
    ),
    # Anthropic models on Agent Platform (uncached input uses 5-min cache-write rate = 1.25x input)
    'claude-opus-4-6': AgyModelRate(
        model='claude-opus-4-6',
        provider='Anthropic',
        input_per_m=6.25,  # 5.00 * 1.25 cache-write rate
        cached_per_m=0.50,
        output_per_m=25.00,
        list_share=1.0,
    ),
    'claude-sonnet-4-6': AgyModelRate(
        model='claude-sonnet-4-6',
        provider='Anthropic',
        input_per_m=3.75,  # 3.00 * 1.25 cache-write rate
        cached_per_m=0.30,
        output_per_m=15.00,
        list_share=1.0,
    ),
    'claude-opus-5-5': AgyModelRate(
        model='claude-opus-5-5',
        provider='Anthropic',
        input_per_m=6.25,
        cached_per_m=0.50,
        output_per_m=25.00,
        list_share=1.0,
    ),
    'claude-sonnet-5-5': AgyModelRate(
        model='claude-sonnet-5-5',
        provider='Anthropic',
        input_per_m=3.75,
        cached_per_m=0.30,
        output_per_m=15.00,
        list_share=1.0,
    ),
}


def strip_content(entry: Mapping[str, Any]) -> dict[str, Any]:
  """Keeps only allowlisted metadata fields from a Cloud Logging LogEntry so no prompt/response text remains."""
  kept: dict[str, Any] = {k: entry[k] for k in ENTRY_FIELDS if k in entry}
  payload = entry.get('jsonPayload')
  if isinstance(payload, Mapping):
    kept_payload: dict[str, Any] = {k: payload[k] for k in PAYLOAD_FIELDS if k in payload}
    meta = payload.get('metadata')
    if isinstance(meta, Mapping):
      kept_payload['metadata'] = {k: meta[k] for k in METADATA_FIELDS if k in meta}
    kept['jsonPayload'] = kept_payload
  else:
    kept['jsonPayload'] = {}
  labels = entry.get('labels')
  if isinstance(labels, Mapping):
    kept['labels'] = {k: labels[k] for k in LABEL_FIELDS if k in labels}
  return kept


def email_of(user_id: str | None) -> str | None:
  """Normalizes `user:alice@x.com` or `principal://.../subject/alice@x.com` to `alice@x.com`."""
  if not user_id:
    return None
  raw = str(user_id).strip()
  if not raw:
    return None
  m = _SUBJECT_RE.search(raw)
  cleaned = urllib.parse.unquote(m.group(1)) if m else re.sub(r'^[a-zA-Z]+:', '', raw)
  cleaned = cleaned.strip().lower()
  if not cleaned:
    return None
  return ge_mart.redact_email_if_configured(cleaned)


def normalize_pricing_model(model: str | None) -> str:
  """Strips `-preview*`, `-exp*`, and `-NNN` suffixes to match the pricing table."""
  if not model:
    return ''
  base = str(model).strip().rsplit('/', 1)[-1].lower()
  return _MODEL_SUFFIX_RE.sub('', base)


def extract_effort(experience: str | None) -> str:
  """Extracts effort level suffix (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) or `default`."""
  if not experience:
    return 'default'
  m = _EFFORT_RE.search(str(experience).strip())
  return m.group(1).lower() if m else 'default'


def _opt_int(val: Any) -> int | None:
  if val is None or val == '' or isinstance(val, bool):
    return None
  try:
    f = float(val)
    if not math.isfinite(f):
      return None
    return int(f)
  except (TypeError, ValueError, OverflowError):
    return None


def _parse_ts(ts: Any) -> datetime.datetime | None:
  if isinstance(ts, datetime.datetime):
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.UTC)
  if not ts:
    return None
  s = str(ts).strip()
  if not s:
    return None
  s = re.sub(r'(\.\d{6})\d+', r'\1', s)
  try:
    dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)
  except ValueError:
    return None


def normalize_inference_entry(entry: Mapping[str, Any], default_project: str = '') -> dict[str, Any]:
  """Converts one `businessaicode.googleapis.com/inference_response` log entry into a normalized call dict."""
  safe = strip_content(entry)

  def as_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

  labels = as_map(safe.get('labels'))
  payload = as_map(safe.get('jsonPayload'))
  meta = as_map(payload.get('metadata'))
  res = as_map(safe.get('resource'))
  res_labels = as_map(res.get('labels'))

  user_id = str(labels.get('user_id') or '').strip() or None
  user_email = email_of(user_id)
  model = str(labels.get('model') or '').strip() or None
  pricing_model = normalize_pricing_model(model)
  experience = str(payload.get('experience') or '').strip() or None
  effort = extract_effort(experience)
  raw_provider = str(labels.get('model_provider') or '').strip()
  provider = raw_provider or ('Anthropic' if (pricing_model.startswith('claude') or (model or '').startswith('claude')) else 'Google')

  prompt_tok = _opt_int(meta.get('promptTokenCount'))
  cached_tok = _opt_int(meta.get('cachedContentTokenCount'))
  output_tok = _opt_int(meta.get('candidatesTokenCount'))
  thoughts_tok = _opt_int(meta.get('thoughtsTokenCount'))
  total_tok = _opt_int(meta.get('totalTokenCount'))
  parts = [t for t in (prompt_tok, output_tok, thoughts_tok) if t is not None]
  has_usage = (total_tok is not None) or bool(parts)
  if total_tok is None and parts:
    total_tok = sum(parts)

  log_name = str(safe.get('logName') or '')
  proj_match = _LOG_PROJECT_RE.match(log_name)
  project_id = proj_match.group(1) if proj_match else (str(res_labels.get('project_id') or '') or default_project)
  ts_raw = str(safe.get('timestamp') or '')
  ts_clean = re.sub(r'(\.\d{6})\d+', r'\1', ts_raw) if ts_raw else ''

  return {
      'insert_id': str(safe.get('insertId') or ''),
      'ts': ts_clean,
      'user_id': user_id,
      'user_email': user_email,
      'model': model,
      'pricing_model': pricing_model,
      'experience': experience,
      'effort': effort,
      'provider': provider,
      'client_name': str(labels.get('client_name') or '').strip() or None,
      'client_version': str(labels.get('client_version') or '').strip() or None,
      'trajectory_id': str(labels.get('trajectory_id') or '').strip() or None,
      'request_id': str(labels.get('request_id') or '').strip() or None,
      'location': str(res_labels.get('location') or 'global').strip(),
      'quota_type': str(meta.get('quotaType') or '').strip() or None,
      'prompt_tokens': prompt_tok,
      'cached_tokens': cached_tok,
      'output_tokens': output_tok,
      'thoughts_tokens': thoughts_tok,
      'total_tokens': total_tok,
      'has_usage': has_usage,
      'project_id': project_id,
  }


def parse_tier_allowances(spec: str | None = None) -> dict[str, tuple[str, float]]:
  """Parses `ENUM:Name:weekly_usd,...` into `{enum_or_name: (display_name, weekly_usd_per_seat)}`."""
  raw = (spec or os.environ.get('FINOPS_AGY_TIER_ALLOWANCES') or DEFAULT_TIER_ALLOWANCES).strip()
  out: dict[str, tuple[str, float]] = {}
  for item in raw.split(','):
    parts = [p.strip() for p in item.split(':')]
    if len(parts) != 3:
      continue
    enum_key, display_name, usd_str = parts
    try:
      usd = float(usd_str)
    except ValueError:
      continue
    if enum_key and display_name and math.isfinite(usd) and usd >= 0:
      out[enum_key] = (display_name, usd)
      out.setdefault(display_name, (display_name, usd))
  return out


def meter_call(
    call: Mapping[str, Any],
    rates: Mapping[str, AgyModelRate] | None = None,
) -> dict[str, Any]:
  """Computes list value ($), WTU, and metered component costs for a single Antigravity call.

  Follows `finops/sql/views.sql` (`f_calls_metered`) from `cloud-gtm/agy-finops`:
  - `uncached_tokens = max(prompt_tokens - cached_tokens, 0)`
  - `list_usd = (uncached * input_rate + cached * cached_rate + (output + thoughts) * output_rate) / 1e6`
  - `wtu`: each token's weight is `rate * share / 2`, summed and floored to whole units, minimum 50 WTU
    (pre-check). A call with `has_usage=True` and only thinking tokens (or 0 tokens) is charged 50 WTU.
  - `NOOP_QUOTA` calls have 0 WTU and $0 cost.
  """
  rate_map = rates or DEFAULT_AGY_RATES
  pricing_model = normalize_pricing_model(call.get('pricing_model') or call.get('model'))
  rate = rate_map.get(pricing_model)
  quota_type = str(call.get('quota_type') or '').strip() or None

  prompt_tok = max(0, _opt_int(call.get('prompt_tokens')) or 0)
  cached_tok = max(0, _opt_int(call.get('cached_tokens')) or 0)
  output_tok = max(0, _opt_int(call.get('output_tokens')) or 0)
  thoughts_tok = max(0, _opt_int(call.get('thoughts_tokens')) or 0)
  uncached_tok = max(prompt_tok - cached_tok, 0)
  has_usage = bool(call.get('has_usage')) or any(
      call.get(k) is not None for k in ('prompt_tokens', 'output_tokens', 'thoughts_tokens', 'total_tokens')
  )

  if quota_type == 'NOOP_QUOTA' or rate is None:
    return {
        **dict(call),
        'pricing_model': pricing_model,
        'priced': rate is not None,
        'uncached_tokens': uncached_tok,
        'list_share': rate.list_share if rate else 1.0,
        'list_usd': 0.0,
        'wtu': 0,
        'cost_input_usd': 0.0,
        'cost_cached_usd': 0.0,
        'cost_output_usd': 0.0,
        'cost_thinking_usd': 0.0,
    }

  long_ctx = bool(rate.long_threshold and prompt_tok > rate.long_threshold)
  in_rate = (rate.long_input_per_m or rate.input_per_m) if long_ctx else rate.input_per_m
  ca_rate = (
      (rate.long_cached_per_m or rate.long_input_per_m or rate.cached_per_m or rate.input_per_m)
      if long_ctx
      else (rate.cached_per_m if rate.cached_per_m is not None else rate.input_per_m)
  )
  out_rate = (rate.long_output_per_m or rate.output_per_m) if long_ctx else rate.output_per_m

  meter_long = bool(rate.meter_long_threshold and prompt_tok > rate.meter_long_threshold)
  in_share = (rate.long_input_share or rate.list_share) if meter_long else rate.list_share
  ca_share = (rate.long_cached_share or rate.list_share) if meter_long else rate.list_share
  out_share = (rate.long_output_share or rate.list_share) if meter_long else rate.list_share

  list_usd = (uncached_tok * in_rate + cached_tok * ca_rate + (output_tok + thoughts_tok) * out_rate) / 1e6
  if not has_usage:
    wtu = 0
  elif uncached_tok == 0 and cached_tok == 0 and output_tok == 0:
    wtu = MIN_CALL_WTU
  else:
    weighted = (
        uncached_tok * round(in_rate * in_share / 2.0, 9)
        + cached_tok * round(ca_rate * ca_share / 2.0, 9)
        + (output_tok + thoughts_tok) * round(out_rate * out_share / 2.0, 9)
    )
    wtu = max(MIN_CALL_WTU, math.floor(weighted))

  return {
      **dict(call),
      'pricing_model': pricing_model,
      'provider': call.get('provider') or rate.provider,
      'priced': True,
      'uncached_tokens': uncached_tok,
      'list_share': rate.list_share,
      'list_usd': round(list_usd, 6),
      'wtu': wtu,
      'cost_input_usd': round(uncached_tok * in_rate / 1e6 * rate.list_share, 6),
      'cost_cached_usd': round(cached_tok * ca_rate / 1e6 * rate.list_share, 6),
      'cost_output_usd': round(output_tok * out_rate / 1e6 * rate.list_share, 6),
      'cost_thinking_usd': round(thoughts_tok * out_rate / 1e6 * rate.list_share, 6),
  }


def _license_config_key(ref: Any) -> str:
  """`locations/{loc}/licenseConfigs/{id}` suffix of a config name (project number and ID forms match)."""
  raw = str(ref or '').strip()
  m = re.search(r'(locations/[^/]+/licenseConfigs/[^/]+)$', raw)
  return m.group(1) if m else raw


def evaluate_calls_and_chargeback(
    raw_calls: Sequence[Mapping[str, Any]],
    license_configs: Sequence[Mapping[str, Any]] | None = None,
    user_licenses: Sequence[Mapping[str, Any]] | None = None,
    meter_pools: Mapping[str, Mapping[str, Any]] | None = None,
    project_id: str = '',
    rates: Mapping[str, AgyModelRate] | None = None,
    window_hours: int = 168,
) -> dict[str, Any]:
  """Evaluates Antigravity calls against weekly seat allowance pools and computes PAYG chargeback.

  Implements both PAYG chargeback methods from `cloud-gtm/agy-finops`:
  - `fair_share`: spreads a pool window's overage PAYG across licensed users who drew on that window
    in proportion to their drawn WTU in the window; Anthropic and unlicensed PAYG stays with the caller.
  - `when_used`: assigns PAYG to whoever called while the pool was exhausted.
  """
  tier_map = parse_tier_allowances()
  configs = list(license_configs or [])
  assignments = list(user_licenses or [])

  def allowance_of(tier: str | None) -> float:
    entry = tier_map.get(tier or '')
    return entry[1] if entry else 0.0

  # 1. Build per-tier seat counts and weekly pool budgets
  config_tier_by_key: dict[str, str] = {}
  seats_by_tier: dict[str, int] = collections.Counter()
  purchased_usd_by_tier: dict[str, float] = collections.defaultdict(float)
  unknown_tiers: set[str] = set()
  for cfg in configs:
    if str(cfg.get('state') or 'ACTIVE').upper() in INACTIVE_CONFIG_STATES:
      continue
    raw_tier = str(cfg.get('tier') or cfg.get('subscriptionTier') or '')
    if raw_tier in tier_map:
      tier_name, weekly_usd = tier_map[raw_tier]
    else:
      # Upstream parity: a tier with no configured allowance keeps its enum name and a $0 allowance.
      tier_name, weekly_usd = (raw_tier or UNKNOWN_TIER), 0.0
      unknown_tiers.add(tier_name)
    count = max(0, _opt_int(cfg.get('license_count') if 'license_count' in cfg else cfg.get('licenseCount')) or 0)
    cfg_key = _license_config_key(cfg.get('name'))
    if cfg_key:
      config_tier_by_key[cfg_key] = tier_name
    seats_by_tier[tier_name] += count
    purchased_usd_by_tier[tier_name] += count * weekly_usd

  user_tier: dict[str, str] = {}
  user_allowance_usd: dict[str, float] = {}
  assigned_by_tier: dict[str, set[str]] = collections.defaultdict(set)
  for ul in assignments:
    state = str(ul.get('assignment_state') or ul.get('licenseAssignmentState') or '').upper()
    if state != ASSIGNED_STATE:
      continue
    u_email = email_of(str(ul.get('user_email') or ul.get('userPrincipal') or ''))
    if not u_email:
      continue
    # Upstream joins an assignment to its active config by name; one that does not resolve holds no seat.
    ul_tier = config_tier_by_key.get(_license_config_key(ul.get('license_config') or ul.get('licenseConfig')))
    if not ul_tier:
      continue
    weekly_usd = allowance_of(ul_tier)
    if u_email not in user_allowance_usd or weekly_usd >= user_allowance_usd[u_email]:
      user_tier[u_email] = ul_tier
      user_allowance_usd[u_email] = weekly_usd
    assigned_by_tier[ul_tier].add(u_email)

  # Pool for calls Gemini Enterprise logged against a pool when the caller has no assignment: the project's
  # largest tier by purchased weekly amount (upstream `known_tier`, ties by name); none without license data.
  largest_tier = (
      min(purchased_usd_by_tier.items(), key=lambda kv: (-kv[1], kv[0]))[0]
      if any(v > 0 for v in purchased_usd_by_tier.values())
      else None
  )

  # Pool cap in WTU per tier (from purchased seats or Cloud Monitoring meter limit); 0 = unknown.
  meter_pools_map = meter_pools or {}
  cap_wtu_by_tier: dict[str, int] = {}
  for tname in set(seats_by_tier) | set(assigned_by_tier) | set(meter_pools_map):
    m_info = meter_pools_map.get(tname) or {}
    m_limit = _opt_int(m_info.get('limit_wtu'))
    if purchased_usd_by_tier.get(tname, 0.0) > 0:
      cap_wtu_by_tier[tname] = round(purchased_usd_by_tier[tname] * WTU_PER_USD)
    elif m_limit is not None and m_limit > 0:
      cap_wtu_by_tier[tname] = m_limit
    elif assigned_by_tier.get(tname) and allowance_of(tname) > 0:
      cap_wtu_by_tier[tname] = round(len(assigned_by_tier[tname]) * allowance_of(tname) * WTU_PER_USD)
    else:
      cap_wtu_by_tier[tname] = 0

  # 2. Deduplicate on (project_id, insert_id), sort chronologically, and meter each call
  seen_ids: set[tuple[str, str]] = set()
  metered_calls: list[dict[str, Any]] = []
  for raw in raw_calls:
    norm = (
        dict(raw)
        if ('pricing_model' in raw and 'prompt_tokens' in raw)
        else normalize_inference_entry(raw, default_project=project_id)
    )
    if norm.get('user_email'):
      norm['user_email'] = ge_mart.redact_email_if_configured(str(norm['user_email']))
    ins_id = str(norm.get('insert_id') or '')
    proj = str(norm.get('project_id') or project_id)
    if ins_id:
      dedup_key = (proj, ins_id)
      if dedup_key in seen_ids:
        continue
      seen_ids.add(dedup_key)
    metered_calls.append(meter_call(norm, rates=rates))

  metered_calls.sort(key=lambda c: (str(c.get('ts') or ''), str(c.get('insert_id') or '')))

  # 3. Replay floating 7-day windows per pool (project/tier) and classify included vs PAYG
  pool_window_start: dict[str, datetime.datetime] = {}
  pool_window_used_wtu: dict[tuple[str, str], int] = collections.defaultdict(int)
  costed_calls: list[dict[str, Any]] = []

  for mc in metered_calls:
    u_email = str(mc.get('user_email') or 'unknown')
    provider = str(mc.get('provider') or 'Google')
    q_type = str(mc.get('quota_type') or '').strip() or None
    t_assigned = user_tier.get(u_email) if user_allowance_usd.get(u_email, 0.0) > 0 else None
    is_licensed = bool(t_assigned) or q_type == 'REGULAR_QUOTA'
    eff_tier = t_assigned or (largest_tier if q_type in ('REGULAR_QUOTA', 'OVERAGE_QUOTA') else None)
    proj = str(mc.get('project_id') or project_id or 'default')

    if provider == 'Anthropic' or q_type == 'NOOP_QUOTA' or not eff_tier:
      draw_key: str | None = None
    else:
      draw_key = f'{proj}/{eff_tier}'

    dt = _parse_ts(mc.get('ts'))
    win_key = 'default_window'
    if draw_key and dt is not None:
      cur_start = pool_window_start.get(draw_key)
      if cur_start is None or dt >= cur_start + datetime.timedelta(days=WINDOW_DAYS):
        if q_type != 'OVERAGE_QUOTA':
          cur_start = dt
          pool_window_start[draw_key] = cur_start
      if cur_start is not None:
        win_key = cur_start.isoformat()

    cap_wtu = cap_wtu_by_tier.get(eff_tier or '', 0)
    used_before = pool_window_used_wtu[(draw_key or '', win_key)] if draw_key else 0

    if q_type == 'REGULAR_QUOTA':
      included = True
    elif q_type == 'OVERAGE_QUOTA':
      included = False
    elif q_type == 'NOOP_QUOTA':
      included = True
    else:
      # No logged decision: included whole while >= 50 WTU are left in a known pool (upstream f_calls_costed).
      included = bool(draw_key) and cap_wtu > 0 and used_before <= cap_wtu - MIN_CALL_WTU

    wtu = int(mc.get('wtu') or 0)
    list_usd = float(mc.get('list_usd') or 0.0)
    list_share = float(mc.get('list_share') or 1.0)

    if included and draw_key and q_type != 'NOOP_QUOTA':
      drawn_wtu = min(wtu, max(cap_wtu - used_before, 0)) if cap_wtu > 0 else wtu
      pool_window_used_wtu[(draw_key, win_key)] += wtu
    else:
      drawn_wtu = 0

    drawn_usd = drawn_wtu * WTU_USD
    included_usd = (wtu * WTU_USD) if (included and q_type != 'NOOP_QUOTA') else 0.0
    payg_usd = 0.0 if included else (list_usd * list_share)
    cost_usd = included_usd if included else payg_usd

    costed_calls.append({
        **mc,
        'licensed': is_licensed,
        'tier': eff_tier,
        'draw_key': draw_key,
        'window_key': win_key,
        'included': included,
        'drawn_wtu': drawn_wtu,
        'drawn_usd': round(drawn_usd, 6),
        'included_usd': round(included_usd, 6),
        'payg_usd': round(payg_usd, 6),
        'cost_usd': round(cost_usd, 6),
    })

  # 4. Compute Fair Share vs When-Used PAYG per user
  # Pool-window PAYG (where draw_key is not None) is spread across licensed users in proportion to drawn_wtu
  # in that window; direct PAYG (Anthropic or unlicensed calls where draw_key is None) stays with the caller.
  window_drawn_by_user: dict[tuple[str, str], dict[str, int]] = collections.defaultdict(
      lambda: collections.defaultdict(int)
  )
  window_pool_payg: dict[tuple[str, str], float] = collections.defaultdict(float)
  direct_payg_by_user: dict[str, float] = collections.defaultdict(float)

  for cc in costed_calls:
    u_email = str(cc.get('user_email') or 'unknown')
    dk = cc.get('draw_key')
    wk = str(cc.get('window_key') or 'default_window')
    if dk:
      if cc['included'] and cc['drawn_wtu'] > 0:
        window_drawn_by_user[(dk, wk)][u_email] += int(cc['drawn_wtu'])
      elif not cc['included'] and cc['payg_usd'] > 0:
        window_pool_payg[(dk, wk)] += float(cc['payg_usd'])
    elif not cc['included'] and cc['payg_usd'] > 0:
      direct_payg_by_user[u_email] += float(cc['payg_usd'])

  fair_share_payg_by_user: dict[str, float] = collections.defaultdict(float, direct_payg_by_user)
  for (dk, wk), pool_payg in window_pool_payg.items():
    drawn_map = window_drawn_by_user.get((dk, wk)) or {}
    total_drawn = sum(drawn_map.values())
    if total_drawn > 0:
      for u_email, u_drawn in drawn_map.items():
        fair_share_payg_by_user[u_email] += pool_payg * (u_drawn / total_drawn)
    else:
      # If no one drew on the window before overage, keep with callers in that window
      for cc in costed_calls:
        if cc.get('draw_key') == dk and cc.get('window_key') == wk and not cc['included']:
          fair_share_payg_by_user[str(cc.get('user_email') or 'unknown')] += float(cc['payg_usd'])

  # 5. Aggregate KPIs, Users, Models x Effort, Clients, and Weekly Pools
  users_agg: dict[str, dict[str, Any]] = {}
  models_agg: dict[tuple[str, str, str], dict[str, Any]] = {}
  clients_agg: dict[tuple[str, str], dict[str, Any]] = {}
  all_trajectories: set[str] = set()
  log_wtu_by_tier: dict[str, int] = collections.Counter()
  payg_usd_by_tier: dict[str, float] = collections.defaultdict(float)

  tot_prompt = 0
  tot_cached = 0
  tot_output = 0
  tot_thoughts = 0
  tot_tokens = 0
  tot_wtu = 0
  tot_list_usd = 0.0
  tot_included_usd = 0.0
  tot_payg_usd = 0.0
  unpriced_calls = 0
  unpriced_models: set[str] = set()

  for cc in costed_calls:
    u_email = str(cc.get('user_email') or 'unknown')
    p_tok = int(cc.get('prompt_tokens') or 0)
    c_tok = int(cc.get('cached_tokens') or 0)
    o_tok = int(cc.get('output_tokens') or 0)
    th_tok = int(cc.get('thoughts_tokens') or 0)
    t_tok = int(cc.get('total_tokens') or (p_tok + o_tok + th_tok))
    wtu = int(cc.get('wtu') or 0)
    l_usd = float(cc.get('list_usd') or 0.0)
    inc_usd = float(cc.get('included_usd') or 0.0)
    py_usd = float(cc.get('payg_usd') or 0.0)
    # A model with no rate card has no WTU or $ value: count it so totals say what they leave out.
    is_unpriced = not cc.get('priced') and cc.get('quota_type') != 'NOOP_QUOTA'
    if is_unpriced:
      unpriced_calls += 1
      unpriced_models.add(str(cc.get('pricing_model') or cc.get('model') or 'unknown'))
    traj = cc.get('trajectory_id')
    if traj:
      all_trajectories.add(str(traj))

    tot_prompt += p_tok
    tot_cached += c_tok
    tot_output += o_tok
    tot_thoughts += th_tok
    tot_tokens += t_tok
    tot_wtu += wtu
    tot_list_usd += l_usd
    tot_included_usd += inc_usd
    tot_payg_usd += py_usd

    call_tier = cc.get('tier')
    if call_tier and cc['included']:
      log_wtu_by_tier[call_tier] += wtu
    if call_tier and not cc['included']:
      payg_usd_by_tier[call_tier] += py_usd

    u = users_agg.setdefault(u_email, {
        'user_email': u_email,
        'licensed': bool(cc.get('licensed')),
        'tier': (
            cc.get('tier') or user_tier.get(u_email)
            or (LICENSED_UNKNOWN_TIER if cc.get('licensed') else 'Unlicensed (PAYG)')
        ),
        'calls': 0,
        '_trajectories': set(),
        '_models': collections.Counter(),
        'prompt_tokens': 0,
        'cached_tokens': 0,
        'output_tokens': 0,
        'thoughts_tokens': 0,
        'total_tokens': 0,
        'wtu': 0,
        'list_usd': 0.0,
        'included_usd': 0.0,
        'payg_when_used_usd': 0.0,
        'last_activity': None,
    })
    u['calls'] += 1
    if traj:
      u['_trajectories'].add(str(traj))
    if cc.get('model'):
      u['_models'][str(cc['model'])] += 1
    u['prompt_tokens'] += p_tok
    u['cached_tokens'] += c_tok
    u['output_tokens'] += o_tok
    u['thoughts_tokens'] += th_tok
    u['total_tokens'] += t_tok
    u['wtu'] += wtu
    u['list_usd'] += l_usd
    u['included_usd'] += inc_usd
    u['payg_when_used_usd'] += py_usd
    ts_str = str(cc.get('ts') or '')
    if ts_str and (u['last_activity'] is None or ts_str > u['last_activity']):
      u['last_activity'] = ts_str

    m_key = (
        str(cc.get('pricing_model') or cc.get('model') or 'unknown'),
        str(cc.get('effort') or 'default'),
        str(cc.get('provider') or 'Google'),
    )
    m_row = models_agg.setdefault(m_key, {
        'model': m_key[0],
        'effort': m_key[1],
        'provider': m_key[2],
        'calls': 0,
        'unpriced_calls': 0,
        'prompt_tokens': 0,
        'cached_tokens': 0,
        'output_tokens': 0,
        'thoughts_tokens': 0,
        'total_tokens': 0,
        'wtu': 0,
        'list_usd': 0.0,
        'included_usd': 0.0,
        'payg_usd': 0.0,
    })
    m_row['calls'] += 1
    m_row['unpriced_calls'] += int(is_unpriced)
    m_row['prompt_tokens'] += p_tok
    m_row['cached_tokens'] += c_tok
    m_row['output_tokens'] += o_tok
    m_row['thoughts_tokens'] += th_tok
    m_row['total_tokens'] += t_tok
    m_row['wtu'] += wtu
    m_row['list_usd'] += l_usd
    m_row['included_usd'] += inc_usd
    m_row['payg_usd'] += py_usd

    c_key = (str(cc.get('client_name') or 'unknown'), str(cc.get('client_version') or 'unknown'))
    c_row = clients_agg.setdefault(c_key, {
        'client_name': c_key[0],
        'client_version': c_key[1],
        'calls': 0,
        'users': set(),
        'total_tokens': 0,
        'wtu': 0,
    })
    c_row['calls'] += 1
    c_row['users'].add(u_email)
    c_row['total_tokens'] += t_tok
    c_row['wtu'] += wtu

  user_rows: list[dict[str, Any]] = []
  for u_email, u in users_agg.items():
    trajs = u.pop('_trajectories')
    mods = u.pop('_models')
    fs_payg = round(fair_share_payg_by_user.get(u_email, 0.0), 4)
    wu_payg = round(u['payg_when_used_usd'], 4)
    inc_usd = round(u['included_usd'], 4)
    p_tok = u['prompt_tokens']
    c_tok = u['cached_tokens']
    user_rows.append({
        **u,
        'trajectories': len(trajs) if trajs else None,  # unknown when calls carry no trajectory_id
        'top_model': mods.most_common(1)[0][0] if mods else None,
        'cache_hit_pct': round(c_tok / p_tok * 100.0, 1) if p_tok > 0 else None,
        'list_usd': round(u['list_usd'], 4),
        'included_usd': inc_usd,
        'payg_when_used_usd': wu_payg,
        'payg_fair_share_usd': fs_payg,
        'total_cost_usd': round(inc_usd + fs_payg, 4),
    })
  user_rows.sort(key=lambda r: (-r['total_cost_usd'], -r['wtu'], r['user_email']))

  model_rows = [
      {
          **m,
          'cache_hit_pct': round(m['cached_tokens'] / m['prompt_tokens'] * 100.0, 1) if m['prompt_tokens'] > 0 else None,
          'list_usd': round(m['list_usd'], 4),
          'included_usd': round(m['included_usd'], 4),
          'payg_usd': round(m['payg_usd'], 4),
      }
      for m in models_agg.values()
  ]
  model_rows.sort(key=lambda r: (-(r['included_usd'] + r['payg_usd']), -r['wtu'], r['model']))

  client_rows = [
      {
          'client_name': c['client_name'],
          'client_version': c['client_version'],
          'calls': c['calls'],
          'active_users': len(c['users']),
          'total_tokens': c['total_tokens'],
          'wtu': c['wtu'],
      }
      for c in sorted(clients_agg.values(), key=lambda r: -r['calls'])
  ]

  # 6. Build Pool cards per tier
  active_emails = set(users_agg.keys())
  pool_rows: list[dict[str, Any]] = []
  all_tiers = sorted(
      set(seats_by_tier) | set(assigned_by_tier) | set(meter_pools_map) | set(log_wtu_by_tier),
      key=lambda t: (0 if t == 'Plus' else 1 if t == 'Standard' else 2, t),
  )
  for tname in all_tiers:
    m_info = meter_pools_map.get(tname) or {}
    m_used = _opt_int(m_info.get('used_wtu'))
    l_used = int(log_wtu_by_tier.get(tname, 0))
    eff_used = m_used if m_used is not None else l_used
    limit_wtu = cap_wtu_by_tier.get(tname, 0) or None  # None: no configured allowance and no meter limit
    limit_usd = round(limit_wtu * WTU_USD, 2) if limit_wtu else None
    used_usd = round(eff_used * WTU_USD, 2)
    assigned_set = assigned_by_tier.get(tname, set())
    active_licensed = len(assigned_set & active_emails)
    idle_licensed = max(0, len(assigned_set) - active_licensed)
    util_pct = round(min(100.0, eff_used / limit_wtu * 100.0), 1) if limit_wtu else None
    seat_usd = allowance_of(tname) or None
    pool_rows.append({
        'tier': tname,
        'seat_allowance_usd': seat_usd,
        'seat_allowance_wtu': round(seat_usd * WTU_PER_USD) if seat_usd else None,
        'purchased_seats': int(seats_by_tier.get(tname, 0)),
        'assigned_seats': len(assigned_set),
        'active_licensed_users': active_licensed,
        'idle_seats': idle_licensed,
        'limit_wtu': limit_wtu,
        'limit_usd': limit_usd,
        'meter_used_wtu': m_used,
        'log_used_wtu': l_used,
        'effective_used_wtu': eff_used,
        'used_usd': used_usd,
        'remaining_wtu': max(0, limit_wtu - eff_used) if limit_wtu else None,
        'remaining_usd': round(max(0.0, limit_usd - used_usd), 2) if limit_usd is not None else None,
        'utilization_pct': util_pct,
        'payg_overage_usd': round(payg_usd_by_tier.get(tname, 0.0), 4),
        'window_start': m_info.get('window_start'),
        'window_end': m_info.get('window_end'),
        'meter_source': 'Cloud Monitoring (agentic_wtu_tier_*_pp_weekly_regional)' if m_used is not None else 'Log replay estimate',
    })

  total_purchased_seats = sum(p['purchased_seats'] for p in pool_rows)
  total_assigned_seats = sum(p['assigned_seats'] for p in pool_rows)
  total_idle_seats = sum(p['idle_seats'] for p in pool_rows)
  known_pools = [p for p in pool_rows if p['limit_wtu']]
  total_pool_limit_wtu = sum(p['limit_wtu'] for p in known_pools) if known_pools else None
  total_pool_used_wtu = sum(p['effective_used_wtu'] for p in pool_rows)
  known_pool_used_wtu = sum(p['effective_used_wtu'] for p in known_pools)

  has_any = bool(costed_calls or configs or assignments or meter_pools_map)
  return {
      'status': 'LIVE' if has_any else 'NO_DATA',
      'simulated': False,
      'project_id': project_id,
      'window_hours': int(window_hours or 168),
      'log_id': LOG_ID,
      'unknown_tiers': sorted(unknown_tiers),
      'kpis': {
          'total_calls': len(costed_calls),
          'total_trajectories': len(all_trajectories),
          'active_users': len(user_rows),
          'purchased_seats': total_purchased_seats,
          'assigned_seats': total_assigned_seats,
          'idle_seats': total_idle_seats,
          'prompt_tokens': tot_prompt,
          'cached_tokens': tot_cached,
          'output_tokens': tot_output,
          'thoughts_tokens': tot_thoughts,
          'total_tokens': tot_tokens,
          'cache_hit_pct': round(tot_cached / tot_prompt * 100.0, 1) if tot_prompt > 0 else None,
          'total_wtu': tot_wtu,
          'pool_limit_wtu': total_pool_limit_wtu,
          'pool_used_wtu': total_pool_used_wtu,
          'pool_utilization_pct': (
              round(min(100.0, known_pool_used_wtu / total_pool_limit_wtu * 100.0), 1)
              if total_pool_limit_wtu
              else None
          ),
          'list_value_usd': round(tot_list_usd, 4),
          'included_usd': round(tot_included_usd, 4),
          'payg_usd': round(tot_payg_usd, 4),
          'total_cost_usd': round(tot_included_usd + tot_payg_usd, 4),
          'unpriced_calls': unpriced_calls,
          'unpriced_models': sorted(unpriced_models),
      },
      'pools': pool_rows,
      'by_user': user_rows,
      'by_model_effort': model_rows,
      'by_client': client_rows,
  }


# KPIs each live source feeds: when a source fails they become None (unknown), never 0.
LOG_KPIS: tuple[str, ...] = (
    'total_calls', 'total_trajectories', 'active_users', 'prompt_tokens', 'cached_tokens', 'output_tokens',
    'thoughts_tokens', 'total_tokens', 'cache_hit_pct', 'total_wtu', 'list_value_usd', 'included_usd',
    'payg_usd', 'total_cost_usd', 'unpriced_calls', 'unpriced_models',
)
SEAT_KPIS: tuple[str, ...] = ('purchased_seats', 'assigned_seats', 'idle_seats')
POOL_KPIS: tuple[str, ...] = ('pool_limit_wtu', 'pool_used_wtu', 'pool_utilization_pct')


def build_pending_antigravity_payload(project_id: str, window_hours: int | None, note: str) -> dict[str, Any]:
  """Live-mode payload before the first collection: every figure is unknown (None), never zero."""
  return {
      'status': 'PENDING',
      'simulated': False,
      'project_id': project_id,
      'window_hours': int(window_hours or 168),
      'log_id': LOG_ID,
      'note': note,
      'errors': {},
      'unknown_tiers': [],
      'kpis': {k: None for k in LOG_KPIS + SEAT_KPIS + POOL_KPIS},
      'pools': [],
      'by_user': [],
      'by_model_effort': [],
      'by_client': [],
  }


def build_demo_antigravity_payload(project_id: str = 'demo-project', window_hours: int = 168) -> dict[str, Any]:
  """Deterministic demo Antigravity FinOps payload (used ONLY in DEMO mode, never when connected to live GCP)."""
  now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
  win_start = (now - datetime.timedelta(days=4)).isoformat().replace('+00:00', 'Z')
  win_end = (now + datetime.timedelta(days=3)).isoformat().replace('+00:00', 'Z')

  demo_configs = [
      {
          'name': f'projects/{project_id}/locations/us/licenseConfigs/ge-plus',
          'tier': 'SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT',
          'license_count': 20,
          'state': 'ACTIVE',
      },
      {
          'name': f'projects/{project_id}/locations/us/licenseConfigs/ge-standard',
          'tier': 'SUBSCRIPTION_TIER_ENTERPRISE',
          'license_count': 10,
          'state': 'ACTIVE',
      },
  ]
  plus_users = [f'dev-{i:02d}@example.com' for i in range(1, 19)]  # 18 assigned (2 unassigned, 2 idle)
  std_users = [f'analyst-{i:02d}@example.com' for i in range(1, 10)]  # 9 assigned (1 idle)
  demo_assignments = [
      {
          'user_email': u,
          'license_config': f'projects/{project_id}/locations/us/licenseConfigs/ge-plus',
          'assignment_state': 'ASSIGNED',
      }
      for u in plus_users
  ] + [
      {
          'user_email': u,
          'license_config': f'projects/{project_id}/locations/us/licenseConfigs/ge-standard',
          'assignment_state': 'ASSIGNED',
      }
      for u in std_users
  ]

  demo_calls: list[dict[str, Any]] = []
  specs = [
      ('dev-01@example.com', 'gemini-3.8-flash', 'gemini-3.8-flash-high', 'Google', 'antigravity_hub', '2.21.1', 'REGULAR_QUOTA', 28, 42000, 38000, 950, 1200),
      ('dev-02@example.com', 'gemini-3.1-pro', 'gemini-3.1-pro-high', 'Google', 'antigravity_hub', '2.21.1', 'REGULAR_QUOTA', 18, 64000, 56000, 1400, 1800),
      ('dev-03@example.com', 'claude-sonnet-4-6', 'claude-sonnet-4-6-high', 'Anthropic', 'antigravity_hub', '2.21.1', 'OVERAGE_QUOTA', 12, 35000, 29000, 1100, 0),
      ('dev-04@example.com', 'claude-opus-4-6', 'claude-opus-4-6-max', 'Anthropic', 'antigravity_cli', '2.21.1', 'OVERAGE_QUOTA', 6, 48000, 41000, 1600, 0),
      ('dev-05@example.com', 'gemini-3.8-flash', 'gemini-3.8-flash-medium', 'Google', 'antigravity_hub', '2.19.1', 'REGULAR_QUOTA', 22, 31000, 27500, 720, 450),
      ('analyst-01@example.com', 'gemini-3.8-flash', 'gemini-3.8-flash-low', 'Google', 'antigravity_hub', '2.21.1', 'REGULAR_QUOTA', 16, 24000, 21000, 580, 120),
      ('analyst-02@example.com', 'gemini-3.1-pro', 'gemini-3.1-pro-high', 'Google', 'antigravity_hub', '2.21.1', 'OVERAGE_QUOTA', 10, 52000, 44000, 1250, 1500),
      ('contractor-payg@example.com', 'gemini-3.8-flash', 'gemini-3.8-flash-high', 'Google', 'antigravity_cli', '2.21.1', 'OVERAGE_QUOTA', 9, 29000, 24000, 810, 900),
  ]
  idx = 0
  for u_email, model, exp, prov, client, ver, qtype, n_calls, p_tok, c_tok, o_tok, th_tok in specs:
    traj_id = f'traj-{u_email.split("@")[0]}-01'
    for step in range(n_calls):
      idx += 1
      ts = (now - datetime.timedelta(minutes=(n_calls - step) * 15)).isoformat().replace('+00:00', 'Z')
      demo_calls.append({
          'insert_id': f'demo-agy-{idx:04d}',
          'ts': ts,
          'user_id': f'user:{u_email}',
          'user_email': u_email,
          'model': model,
          'pricing_model': normalize_pricing_model(model),
          'experience': exp,
          'effort': extract_effort(exp),
          'provider': prov,
          'client_name': client,
          'client_version': ver,
          'trajectory_id': traj_id,
          'request_id': f'{traj_id}-{step}',
          'location': 'us',
          'quota_type': qtype,
          'prompt_tokens': p_tok,
          'cached_tokens': c_tok,
          'output_tokens': o_tok,
          'thoughts_tokens': th_tok,
          'total_tokens': p_tok + o_tok + th_tok,
          'has_usage': True,
          'project_id': project_id,
      })

  evaluated = evaluate_calls_and_chargeback(
      demo_calls,
      license_configs=demo_configs,
      user_licenses=demo_assignments,
      meter_pools={
          'Plus': {'limit_wtu': 20 * 1_875_000, 'used_wtu': 14_250_000, 'window_start': win_start, 'window_end': win_end},
          'Standard': {'limit_wtu': 10 * 1_250_000, 'used_wtu': 12_500_000, 'window_start': win_start, 'window_end': win_end},
      },
      project_id=project_id,
  )
  return {
      **evaluated,
      'status': 'SIMULATED_DEMO',
      'simulated': True,
      'project_id': project_id,
      'window_hours': window_hours,
      'log_id': LOG_ID,
      'source': 'Synthetic Antigravity Demo (modeled on cloud-gtm/agy-finops)',
      'compliance_note': (
          'In live GCP mode, enable Gemini Enterprise -> Settings -> AI developer tools -> '
          'Compliance -> Enable Metadata logging to stream businessaicode.googleapis.com/inference_response.'
      ),
  }


class AntigravityFinOpsCollector:
  """Collects live Antigravity calls, Gemini Enterprise seat licenses, and weekly WTU quota meters."""

  def __init__(
      self,
      project_id: str,
      api_caller: Callable[..., dict[str, Any]] | None = None,
      locations: Sequence[str] = ('global', 'us', 'eu'),
      max_log_entries: int = 2000,
      api: Any | None = None,
  ):
    self.project_id = project_id
    if api_caller is not None:
      self._call = api_caller
    elif api is not None and callable(getattr(api, 'call', None)):
      self._call = api.call
    else:
      raise ValueError('Provide api or api_caller to AntigravityFinOpsCollector')
    self.locations = tuple(locations)
    self.max_log_entries = max_log_entries
    self._cache_lock = threading.Lock()
    self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
    self.ttl_seconds = float(os.environ.get('FINOPS_FLEET_TTL_SECONDS', '60'))

  def _fetch_logs(self, window_s: int) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Reads `businessaicode.googleapis.com/inference_response` from Cloud Logging and strips text."""
    log_window_s = min(window_s, 30 * 24 * 3600)
    start_iso = (
        (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=log_window_s))
        .replace(microsecond=0)
        .isoformat()
        .replace('+00:00', 'Z')
    )
    body: dict[str, Any] = {
        'resourceNames': [f'projects/{self.project_id}'],
        'filter': f'logName="projects/{self.project_id}/logs/businessaicode.googleapis.com%2Finference_response" AND timestamp>="{start_iso}"',
        'orderBy': 'timestamp desc',
        'pageSize': min(1000, self.max_log_entries),
    }
    calls: list[dict[str, Any]] = []
    truncated = False
    try:
      for _ in range(5):
        page = self._call('POST', f'{LOGGING_API}/entries:list', body)
        for entry in page.get('entries') or []:
          if isinstance(entry, Mapping):
            calls.append(normalize_inference_entry(entry, default_project=self.project_id))
        token = page.get('nextPageToken')
        if not token:
          break
        if len(calls) >= self.max_log_entries:
          truncated = True
          break
        body['pageToken'] = token
      return calls, truncated, None
    except Exception as exc:  # pylint: disable=broad-except
      return calls, truncated, str(exc)[:280]

  def _list_all(self, url: str, key: str, max_pages: int = 50) -> list[Mapping[str, Any]]:
    """Every item of a paginated Discovery Engine list call (raises on API errors)."""
    items: list[Mapping[str, Any]] = []
    token = ''
    for _ in range(max_pages):
      sep = '&' if '?' in url else '?'
      page_url = f'{url}{sep}pageSize=1000' + (f'&pageToken={urllib.parse.quote(token)}' if token else '')
      resp = self._call('GET', page_url)
      items.extend(i for i in (resp.get(key) or []) if isinstance(i, Mapping))
      token = str(resp.get('nextPageToken') or '')
      if not token:
        break
    return items

  def _fetch_licenses(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, bool, bool]:
    """Reads `licenseConfigs` and `userLicenses` across configured Gemini Enterprise locations.

    Returns (configs, licenses, error, configs_answered, assignments_answered). A 404 means no Gemini
    Enterprise app (or no user store yet) in that location, as upstream treats it. When no location
    answered, seat counts are unknown; when a user store failed, assigned seats are unknown.
    """
    configs: list[dict[str, Any]] = []
    licenses: list[dict[str, Any]] = []
    errors: list[str] = []
    configs_answered = False
    assignments_answered = True

    def is_404(exc: Exception) -> bool:
      return getattr(exc, 'status', None) == 404 or 'HTTP 404' in str(exc)

    for loc in self.locations:
      host = 'https://discoveryengine.googleapis.com' if loc == 'global' else f'https://{loc}-discoveryengine.googleapis.com'
      base = f'{host}/v1/projects/{self.project_id}/locations/{loc}'
      try:
        found_cfgs = self._list_all(f'{base}/licenseConfigs', 'licenseConfigs')
        configs_answered = True
      except Exception as exc:  # pylint: disable=broad-except
        if is_404(exc):
          configs_answered = True
        else:
          errors.append(f'{loc}/licenseConfigs: {str(exc)[:200]}')
        continue
      for c in found_cfgs:
        configs.append({
            'project_id': self.project_id,
            'location': loc,
            'name': c.get('name'),
            'tier': c.get('subscriptionTier'),
            'license_count': _opt_int(c.get('licenseCount')) or 0,
            'free_trial': bool(c.get('freeTrial', False)),
            'state': c.get('state') or 'ACTIVE',
        })
      if not found_cfgs:
        continue
      try:
        found_uls = self._list_all(f'{base}/userStores/default_user_store/userLicenses', 'userLicenses')
      except Exception as exc:  # pylint: disable=broad-except
        if not is_404(exc):  # 404: seats bought but never assigned (no user store yet)
          assignments_answered = False
          errors.append(f'{loc}/userLicenses: {str(exc)[:200]}')
        continue
      for u in found_uls:
        licenses.append({
            'project_id': self.project_id,
            'location': loc,
            'user_email': email_of(str(u.get('userPrincipal') or '')),
            'license_config': u.get('licenseConfig'),
            'assignment_state': u.get('licenseAssignmentState'),
        })
    return configs, licenses, ('; '.join(errors) if errors else None), configs_answered, assignments_answered

  def _fetch_wtu_meters(self) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Reads Gemini Enterprise weekly WTU allowance meters (`agentic_wtu_tier_*_pp_weekly_regional`)."""
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    start_iso = (now - datetime.timedelta(days=8)).isoformat().replace('+00:00', 'Z')
    end_iso = now.isoformat().replace('+00:00', 'Z')
    pools: dict[str, dict[str, Any]] = {}
    try:
      desc_filter = urllib.parse.quote(f'metric.type = starts_with("{WTU_METER_PREFIX}agentic_wtu_")')
      desc_resp = self._call('GET', f'{MONITORING_API}/projects/{self.project_id}/metricDescriptors?filter={desc_filter}')
      bases: set[str] = set()
      for d in desc_resp.get('metricDescriptors') or []:
        mtype = str(d.get('type') or '')
        base = mtype.rsplit('/', 1)[0] if '/' in mtype else mtype
        if _POOL_METRIC_RE.search(base):
          bases.add(base)

      for base in sorted(bases):
        m = _POOL_METRIC_RE.search(base)
        if not m:
          continue
        tier_key = m.group('tier')
        tier_name = _METER_TIER_NAMES.get(tier_key, tier_key.replace('_', ' ').title())
        limit_wtu: int | None = None
        used_wtu: int = 0
        win_start_iso: str | None = None
        win_end_iso: str | None = None

        for suffix in ('limit', 'net_usage'):
          q = urllib.parse.urlencode({
              'filter': f'metric.type = "{base}/{suffix}"',
              'interval.startTime': start_iso,
              'interval.endTime': end_iso,
              'aggregation.alignmentPeriod': '3600s',
              'aggregation.perSeriesAligner': 'ALIGN_MAX',
          })
          ts_resp = self._call('GET', f'{MONITORING_API}/projects/{self.project_id}/timeSeries?{q}')
          for series in ts_resp.get('timeSeries') or []:
            pts = series.get('points') or []
            if not pts:
              continue
            val = _opt_int((pts[0].get('value') or {}).get('int64Value')) or 0
            if suffix == 'limit':
              limit_wtu = max(limit_wtu or 0, val)
            else:
              win_epoch = _opt_int((series.get('metric') or {}).get('labels', {}).get('window_start_time'))
              if win_epoch:
                w_start = datetime.datetime.fromtimestamp(win_epoch, datetime.UTC)
                w_end = w_start + datetime.timedelta(days=WINDOW_DAYS)
                if w_end > now and val >= used_wtu:
                  used_wtu = val
                  win_start_iso = w_start.isoformat().replace('+00:00', 'Z')
                  win_end_iso = w_end.isoformat().replace('+00:00', 'Z')
              elif val > used_wtu:
                used_wtu = val
        pools[tier_name] = {
            'tier': tier_name,
            'metric_base': base,
            'limit_wtu': limit_wtu,
            'used_wtu': used_wtu,
            'window_start': win_start_iso,
            'window_end': win_end_iso,
        }
      return pools, None
    except Exception as exc:  # pylint: disable=broad-except
      return pools, str(exc)[:280]

  def collect(self, window_hours: int = 168, force_refresh: bool = False) -> dict[str, Any]:
    """Collects live Antigravity FinOps metrics for `self.project_id`."""
    hours = max(1, min(int(window_hours or 168), 8760))
    now_mono = time.monotonic()
    with self._cache_lock:
      cached = self._cache.get(hours)
      if cached and not force_refresh and (now_mono - cached[0]) < self.ttl_seconds:
        return json.loads(json.dumps(cached[1]))

    window_s = hours * 3600
    calls, truncated, log_err = self._fetch_logs(window_s)
    configs, licenses, lic_err, configs_ok, assignments_ok = self._fetch_licenses()
    meter_pools, meter_err = self._fetch_wtu_meters()

    evaluated = evaluate_calls_and_chargeback(
        calls,
        license_configs=configs,
        user_licenses=licenses,
        meter_pools=meter_pools,
        project_id=self.project_id,
        window_hours=hours,
    )
    errors = {k: v for k, v in (('logs', log_err), ('licenses', lic_err), ('wtu_meter', meter_err)) if v}
    kpis = dict(evaluated.get('kpis') or {})
    pools = [dict(p) for p in evaluated.get('pools') or []]
    # A source that failed is unknown (None), never zero.
    if log_err:
      for k in LOG_KPIS:
        kpis[k] = None
      kpis['idle_seats'] = None  # idle = assigned with no calls: unknown without the call log
      for p in pools:
        p.update(log_used_wtu=None, active_licensed_users=None, idle_seats=None, payg_overage_usd=None)
        if p.get('meter_used_wtu') is None:
          p.update(effective_used_wtu=None, used_usd=None, utilization_pct=None, remaining_wtu=None, remaining_usd=None)
      if not pools or any(p.get('meter_used_wtu') is None for p in pools):
        kpis['pool_used_wtu'] = None
        kpis['pool_utilization_pct'] = None
    if not configs_ok:
      for k in SEAT_KPIS:
        kpis[k] = None
      for p in pools:
        p.update(purchased_seats=None, assigned_seats=None, active_licensed_users=None, idle_seats=None)
    elif not assignments_ok:
      kpis['assigned_seats'] = None
      kpis['idle_seats'] = None
      for p in pools:
        p.update(assigned_seats=None, active_licensed_users=None, idle_seats=None)

    has_any = bool(calls or configs or licenses or meter_pools)
    payload: dict[str, Any] = {
        **evaluated,
        'kpis': kpis,
        'pools': pools,
        'status': 'LIVE' if has_any else ('ERROR' if errors else 'NO_DATA'),
        'partial': bool(errors) and has_any,
        'simulated': False,
        'project_id': self.project_id,
        'window_hours': hours,
        'log_id': LOG_ID,
        'entries_scanned': len(calls),
        'truncated': truncated,
        'source': (
            'Cloud Logging (businessaicode.googleapis.com/inference_response) + '
            'Cloud Monitoring (agentic_wtu_tier_*_pp_weekly_regional) + '
            'Discovery Engine License API'
        ),
        'compliance_note': (
            'Antigravity metadata logging is enabled per Gemini Enterprise app under '
            'Settings -> AI developer tools -> Compliance -> Enable Metadata logging. '
            'Prompt and response text is never read or stored.'
        ),
        'errors': errors,
    }
    with self._cache_lock:
      self._cache[hours] = (now_mono, json.loads(json.dumps(payload)))
    return payload
