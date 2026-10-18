"""Versioned Vertex AI and Cloud Billing reference data.

Every reference table the dashboard relies on (model list prices, Provisioned Throughput GSU prices and
burndown specs, model retirement dates and billing credit types) lives in one file,
``reference/vertex_ai_reference.json``. Each section records the date its values were last checked
against the cited Google page, and ``tests/test_reference_data.py`` fails once a section is older than
``max_age_days``, so stale prices or retirement dates cannot ship silently.

This module only reads and validates that file; it has no dependencies on the rest of the package.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import pathlib
import re
from collections.abc import Mapping
from typing import Any

REFERENCE_FILE = pathlib.Path(__file__).with_name('reference') / 'vertex_ai_reference.json'
PRICE_KEYS = ('input', 'cached_read', 'cache_write', 'output')
_PT_KEYS = ('tokens_per_second_per_gsu', 'output_burndown', 'cache_read_burndown')
_CREDIT_GROUPS = ('commitment', 'contract_discount', 'promotional')
_CREDIT_TYPE_RE = re.compile(r'^[A-Z_]+$')


def _date(value: object, where: str) -> datetime.date:
  try:
    return datetime.date.fromisoformat(str(value))
  except ValueError as exc:
    raise ValueError(f'{where}: {value!r} is not an ISO date') from exc


def _price(entry: object, where: str) -> None:
  if not isinstance(entry, Mapping):
    raise ValueError(f'{where}: expected an object')
  for key in PRICE_KEYS:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
      raise ValueError(f'{where}.{key}: expected a non-negative number, got {value!r}')


def validate(data: object) -> None:
  """Raises ValueError with the offending path when the reference file is malformed."""
  if not isinstance(data, Mapping) or data.get('schema_version') != 1:
    raise ValueError('reference data: expected schema_version 1')
  if not isinstance(data.get('max_age_days'), int) or data['max_age_days'] <= 0:
    raise ValueError('reference data: max_age_days must be a positive integer')
  sections = data.get('sections')
  if not isinstance(sections, Mapping):
    raise ValueError('reference data: missing sections')
  for name in ('model_list_prices', 'retired_model_list_prices', 'provisioned_throughput', 'model_lifecycle',
               'billing_credit_types'):
    sec = sections.get(name)
    if not isinstance(sec, Mapping):
      raise ValueError(f'reference data: missing section {name}')
    if not sec.get('static'):
      _date(sec.get('checked'), f'{name}.checked')
  for group in ('model_list_prices', 'retired_model_list_prices'):
    for model, entry in sections[group]['models'].items():
      _price(entry, f'{group}.{model}')
      last = datetime.date.min
      for i, step in enumerate(entry.get('schedule') or []):
        _price(step, f'{group}.{model}.schedule[{i}]')
        effective = _date(step.get('effective_from'), f'{group}.{model}.schedule[{i}].effective_from')
        if effective <= last:
          raise ValueError(f'{group}.{model}.schedule must be in ascending effective_from order')
        last = effective
  pt = sections['provisioned_throughput']
  for term in ('1_month', '1_year'):
    if not isinstance(pt.get('gsu_hourly_usd_global', {}).get(term), (int, float)):
      raise ValueError(f'provisioned_throughput.gsu_hourly_usd_global.{term}: expected a number')
  if not isinstance(pt.get('hours_per_month'), (int, float)) or pt['hours_per_month'] <= 0:
    raise ValueError('provisioned_throughput.hours_per_month: expected a positive number')
  for model, spec in pt['models'].items():
    for key in _PT_KEYS:
      value = spec.get(key) if isinstance(spec, Mapping) else None
      if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f'provisioned_throughput.{model}.{key}: expected a positive number')
  for model, entry in sections['model_lifecycle']['models'].items():
    _date(entry.get('retirement_date'), f'model_lifecycle.{model}.retirement_date')
    if not entry.get('replacements') or not all(isinstance(r, str) and r for r in entry['replacements']):
      raise ValueError(f'model_lifecycle.{model}.replacements: expected a non-empty list of model ids')
  credits = sections['billing_credit_types']
  for group in _CREDIT_GROUPS:
    values = credits.get(group)
    if not values or not all(isinstance(v, str) and _CREDIT_TYPE_RE.match(v) for v in values):
      raise ValueError(f'billing_credit_types.{group}: expected a non-empty list of UPPER_CASE credit types')


@functools.lru_cache(maxsize=1)
def load() -> dict[str, Any]:
  data = json.loads(REFERENCE_FILE.read_text(encoding='utf-8'))
  validate(data)
  return data


def _section(name: str) -> Mapping[str, Any]:
  return load()['sections'][name]


def version() -> str:
  return str(load()['version'])


def checked(section: str) -> str:
  """ISO date the section was last verified against its source."""
  return str(_section(section)['checked'])


def source(section: str, key: str = 'source') -> str:
  return str(_section(section)[key])


def checked_dates() -> dict[str, datetime.date]:
  """Last-checked date of every non-static section."""
  return {name: datetime.date.fromisoformat(sec['checked'])
          for name, sec in load()['sections'].items() if not sec.get('static')}


def model_key(model: object) -> str:
  """'publishers/google/models/gemini-2.0-flash-001' -> 'gemini-2.0-flash' (lifecycle / Provisioned Throughput key)."""
  name = str(model or '').strip().rsplit('/', 1)[-1].lower()
  return re.sub(r'-\d{3}$', '', name)


def list_prices(today: datetime.date | None = None, include_retired: bool = True) -> dict[str, dict[str, float]]:
  """Per-model list price in effect on `today` (UTC date): {model: {input, cached_read, cache_write, output}}."""
  day = today or datetime.datetime.now(datetime.UTC).date()
  groups = ('model_list_prices', 'retired_model_list_prices') if include_retired else ('model_list_prices',)
  out: dict[str, dict[str, float]] = {}
  for group in groups:
    for model, entry in _section(group)['models'].items():
      current = entry
      for step in entry.get('schedule') or []:
        if datetime.date.fromisoformat(step['effective_from']) <= day:
          current = step
      out[model] = {key: float(current[key]) for key in PRICE_KEYS}
  return out


def gsu_monthly_prices() -> tuple[float, float]:
  """(1-month term, 1-year term) global list price per GSU per month."""
  pt = _section('provisioned_throughput')
  hours = float(pt['hours_per_month'])
  hourly = pt['gsu_hourly_usd_global']
  return round(float(hourly['1_month']) * hours, 2), round(float(hourly['1_year']) * hours, 2)


def pt_specs() -> dict[str, tuple[float, float, float]]:
  """{model: (tokens per second per GSU, output/reasoning burndown, cache-read burndown)}."""
  return {model: (float(s['tokens_per_second_per_gsu']), float(s['output_burndown']), float(s['cache_read_burndown']))
          for model, s in _section('provisioned_throughput')['models'].items()}


def burndown_tokens(model: object, *, input_tokens: float = 0.0, output_tokens: float = 0.0,
                    reasoning_tokens: float = 0.0, cache_read_tokens: float = 0.0,
                    cache_write_tokens: float = 0.0) -> float | None:
  """Provisioned Throughput burndown-weighted tokens, or None when the model has no published spec."""
  spec = pt_specs().get(model_key(model))
  if spec is None:
    return None
  _, out_mult, cache_mult = spec
  return (max(0.0, input_tokens) + max(0.0, cache_write_tokens) + cache_mult * max(0.0, cache_read_tokens)
          + out_mult * (max(0.0, output_tokens) + max(0.0, reasoning_tokens)))


def model_lifecycle() -> dict[str, tuple[str, tuple[str, ...]]]:
  """{model: (retirement date, Google's listed replacements)}."""
  return {model: (str(e['retirement_date']), tuple(e['replacements']))
          for model, e in _section('model_lifecycle')['models'].items()}


def credit_types(group: str) -> tuple[str, ...]:
  """Billing export credits.type values of one group: commitment, contract_discount or promotional."""
  if group not in _CREDIT_GROUPS:
    raise KeyError(group)
  return tuple(_section('billing_credit_types')[group])
