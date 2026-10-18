"""Cloud Billing export reader for FinOps Dashboard.

Reads real billed cost for this project from a Cloud Billing BigQuery export
(standard `gcp_billing_export_v1_*` or detailed `gcp_billing_export_resource_v1_*`).

The export table is configured with `FINOPS_BILLING_EXPORT_TABLE`
(`project.dataset.table`). When it is not configured, or the query fails, the
result says so explicitly (`NOT_CONNECTED` / `ERROR`); no numbers are invented.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from finops_dashboard import reference_data

logger = logging.getLogger(__name__)

BILLING_TABLE_ENV = 'FINOPS_BILLING_EXPORT_TABLE'
WINDOW_DAYS = 30
CACHE_TTL_S = 900.0
# The export schema only changes when Google adds columns, so one metadata read a day is plenty.
SCHEMA_CACHE_TTL_S = 86400.0
# List price per line item, exported since 2023-06-29. Older or custom tables may not have it.
LIST_PRICE_COLUMN = 'cost_at_list'
_LIST_COLUMN_ERROR_RE = re.compile(r'\bcost_at_list\b', re.IGNORECASE)

_TABLE_RE = re.compile(
    r'^(?:[a-z][a-z0-9\-]{1,61}[a-z0-9]\.[a-z]{2,}:)?[a-z][a-z0-9\-]{4,61}[a-z0-9]\.[A-Za-z0-9_]{1,1024}\.[A-Za-z0-9_]{1,1024}$'
)
_PROJECT_RE = re.compile(r'^(?:[a-z][a-z0-9\-]{1,61}[a-z0-9]\.[a-z]{2,}:)?[a-z][a-z0-9\-]{4,61}[a-z0-9]$')

HOW_TO_CONNECT = (
    'Enable Cloud Billing export to BigQuery (Billing > Billing export), then set '
    f'{BILLING_TABLE_ENV}=<project>.<dataset>.gcp_billing_export_v1_<ID> on the service.'
)

# (error_kind, pattern, hint) for failed billing queries; the first pattern found in the error text
# wins. Permission comes before not-found because BigQuery 403s also say "or perhaps it does not exist".
_ERROR_HINTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ('invalid_table_name', re.compile(r'invalid billing export table name', re.IGNORECASE),
     f'{BILLING_TABLE_ENV} must be <project>.<dataset>.<table>, separated by dots only (no colon, no backticks).'),
    ('permission_denied', re.compile(r'access denied|permission|http 403', re.IGNORECASE),
     'The service account cannot read the export. Grant it roles/bigquery.dataViewer on the export dataset '
     'and roles/bigquery.jobUser on this project.'),
    ('schema_mismatch', re.compile(r'unrecognized name|no such field|does not exist in struct', re.IGNORECASE),
     'The table is not a Cloud Billing export (expected gcp_billing_export_v1_* or gcp_billing_export_resource_v1_*).'),
    ('not_found', re.compile(r'not found|http 404', re.IGNORECASE),
     'The table or dataset was not found. Check the name and that Billing export to BigQuery is enabled '
     '(the first rows can take about a day to appear).'),
    ('bytes_limit', re.compile(r'bytes billed', re.IGNORECASE),
     'The query would scan more than its 5 GB limit. Use the standard export (gcp_billing_export_v1_*), '
     'which is smaller than the detailed one.'),
    ('auth', re.compile(r'credentials|http 401', re.IGNORECASE),
     'The service has no valid Google Cloud credentials.'),
    ('timeout', re.compile(r'did not finish in time|timed out|timeout', re.IGNORECASE),
     'The query timed out. It runs again at the next refresh (every 15 minutes).'),
)


def classify_billing_error(message: str | None) -> dict[str, str]:
  """Returns {'error_kind', 'hint'} for the error text of a failed billing query."""
  text = message or ''
  for kind, pattern, hint in _ERROR_HINTS:
    if pattern.search(text):
      return {'error_kind': kind, 'hint': hint}
  return {'error_kind': 'unknown', 'hint': 'See the error text; it is also written to the service logs.'}


def _status_detail(status: str, message: str | None) -> dict[str, Any]:
  """error_kind and hint for a payload that has no billed numbers, so the dashboard can say what to fix."""
  if status == 'ERROR':
    return classify_billing_error(message)
  return {'error_kind': None, 'hint': HOW_TO_CONNECT if status == 'NOT_CONNECTED' else None}


def configured_table() -> str | None:
  table = os.environ.get(BILLING_TABLE_ENV, '').strip().strip('`')
  return table or None


# Cloud Billing export `credits.type` groups come from the versioned reference file (section
# billing_credit_types, checked against the export schema page). Commitment, subscription and sustained-use
# discounts plus contractual spending-based DISCOUNT credits count toward the Effective Savings Rate;
# promotional / free-tier credits are a form of payment, not a rate optimization, and are excluded.
COMMITMENT_CREDIT_TYPES = reference_data.credit_types('commitment')
CONTRACT_DISCOUNT_CREDIT_TYPES = reference_data.credit_types('contract_discount')
PROMOTIONAL_CREDIT_TYPES = reference_data.credit_types('promotional')
# Every other type is reported as "other" and excluded from ESR: FEE_UTILIZATION_OFFSET (offsets a spend-based
# CUD fee that is itself in gross cost, so it is not a saving on its own), RESELLER_MARGIN and new/unknown types.
_CREDIT_TYPE_RE = re.compile(r'^[A-Z_]+$')


def _sql_in(values: tuple[str, ...]) -> str:
  """Quoted IN-list for module constants (never user input); the check keeps it injection-proof anyway."""
  if not values or not all(_CREDIT_TYPE_RE.match(v) for v in values):
    raise ValueError(f'Invalid credit type list: {values!r}')
  return ', '.join(f"'{v}'" for v in values)


def build_billing_sql(table: str, project_id: str, window_days: int = WINDOW_DAYS,
                      include_list_price: bool = False) -> str:
  """Builds the per-SKU billed cost query. Inputs are validated because they are interpolated.

  include_list_price adds list_usd (SUM of cost_at_list) and listed_gross_usd (cost of the line items that
  have a list price). Only request it when the table has cost_at_list (BillingExportReader probes for it).
  """
  if not _TABLE_RE.match(table):
    raise ValueError(f'Invalid billing export table name: {table!r}')
  if not _PROJECT_RE.match(project_id):
    raise ValueError(f'Invalid project id: {project_id!r}')
  days = max(1, min(int(window_days), 400))
  list_cols = (
      f'ROUND(SUM({LIST_PRICE_COLUMN}), 6) AS list_usd,\n'
      f'      ROUND(SUM(IF({LIST_PRICE_COLUMN} IS NULL, 0, cost)), 6) AS listed_gross_usd,\n      '
      if include_list_price else ''
  )
  return f"""
    SELECT
      service.description AS service,
      sku.id AS sku_id,
      sku.description AS sku_description,
      ROUND(SUM(cost), 6) AS gross_usd,
      {list_cols}ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS credits_usd,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c
        WHERE c.type IN ({_sql_in(COMMITMENT_CREDIT_TYPES)})), 0)), 6) AS cud_credits_usd,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c
        WHERE c.type IN ({_sql_in(CONTRACT_DISCOUNT_CREDIT_TYPES)})), 0)), 6) AS contract_discount_credits_usd,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c
        WHERE c.type IN ({_sql_in(PROMOTIONAL_CREDIT_TYPES)})), 0)), 6) AS promo_credits_usd,
      ROUND(SUM(usage.amount_in_pricing_units), 6) AS usage_amount,
      ANY_VALUE(usage.pricing_unit) AS usage_unit,
      ANY_VALUE(currency) AS currency,
      CAST(MAX(usage_end_time) AS STRING) AS last_usage
    FROM `{table}`
    WHERE project.id = '{project_id}'
      AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    GROUP BY service, sku_id, sku_description
    HAVING gross_usd != 0 OR credits_usd != 0
    ORDER BY gross_usd DESC
    LIMIT 40
  """


ESR_METHOD = (
    'Effective Savings Rate = commitment, subscription and sustained-use discount credits plus contractual '
    'spending-based DISCOUNT credits, divided by gross cost. Promotional / free-tier credits, '
    'FEE_UTILIZATION_OFFSET, reseller margin and unknown credit types are excluded. Discounts already built into '
    'the unit price (negotiated or new spend-based CUD pricing, i.e. cost_at_list - cost) are not included, so '
    'this is a lower bound.'
)

ESR_METHOD_LIST = (
    'Effective Savings Rate = savings against list price, divided by list price (cost_at_list), over the line '
    'items that have a list price. Savings = discounts built into the unit price (negotiated or spend-based CUD '
    'pricing: cost_at_list - cost) plus commitment, subscription and sustained-use discount credits and contractual '
    'spending-based DISCOUNT credits. Promotional / free-tier credits, FEE_UTILIZATION_OFFSET, reseller margin and '
    'unknown credit types are excluded. Line items without a list price (such as taxes and some fees) are left '
    'out, so a commitment fee without a list price, including any fee paid for unused commitment, is not deducted.'
)


def _pct_of(part: float | None, whole: float) -> float | None:
  """Savings share of gross as a positive percentage (credits are negative in the export)."""
  if part is None or whole <= 0:
    return None
  return round(-part / whole * 100.0, 1)


def summarize_rows(rows: list[dict[str, Any]], table: str, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
  """Turns BigQuery rows into the dashboard's billing payload.

  When the rows carry list_usd (build_billing_sql(include_list_price=True)), the Effective Savings Rate is
  measured against list price, so discounts built into the unit price count too (esr_basis LIST_PRICE).
  Otherwise it is the credits-only lower bound (esr_basis CREDITS_ONLY).
  """

  def _f(v: object) -> float:
    try:
      return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
      return 0.0

  ledger: list[dict[str, Any]] = []
  split_rows = 0
  list_queried = any('list_usd' in r for r in rows)
  # List-price basis, over the SKUs that have a list price: list, the gross of their listed line items,
  # and their commitment + contract credits (negative in the export).
  list_total = 0.0
  listed_gross_total = 0.0
  listed_esr_credits = 0.0
  unlisted_rows = 0
  unlisted_gross = 0.0
  for r in rows:
    gross = _f(r.get('gross_usd'))
    credits = _f(r.get('credits_usd'))  # negative in the export
    split_keys = ('cud_credits_usd', 'contract_discount_credits_usd', 'promo_credits_usd')
    # Rows from an older query (or fixture) carry no per-type split: report it as unknown, never guess.
    if any(r.get(k) is not None for k in split_keys):
      split_rows += 1
      cud_cred: float | None = _f(r.get('cud_credits_usd'))
      contract_cred: float | None = _f(r.get('contract_discount_credits_usd'))
      promo_cred: float | None = _f(r.get('promo_credits_usd'))
      other_cred: float | None = credits - (cud_cred or 0.0) - (contract_cred or 0.0) - (promo_cred or 0.0)
      esr_part: float | None = (cud_cred or 0.0) + (contract_cred or 0.0)
    else:
      cud_cred = contract_cred = promo_cred = other_cred = esr_part = None
    list_usd = _f(r.get('list_usd')) if r.get('list_usd') is not None else None
    price_discount: float | None = None
    if list_usd is not None and list_usd > 0:
      listed_raw = r.get('listed_gross_usd')
      listed_gross = _f(listed_raw) if listed_raw is not None else gross
      price_discount = list_usd - listed_gross
      list_total += list_usd
      listed_gross_total += listed_gross
      listed_esr_credits += esr_part or 0.0
    elif list_queried and gross != 0:
      unlisted_rows += 1  # taxes, some fees: no list price, so no ESR basis
      unlisted_gross += gross
    usage = r.get('usage_amount')
    unit = r.get('usage_unit') or ''
    ledger.append({
        'sku_id': r.get('sku_id') or '',
        'service': r.get('service') or '',
        'sku_description': r.get('sku_description') or '',
        'usage_volume': (f'{_f(usage):,.2f} {unit}'.strip() if usage is not None else None),
        'telemetry_estimated_usd': None,
        'billing_export_gross_usd': round(gross, 2),
        'list_price_usd': round(list_usd, 2) if list_usd is not None else None,
        'price_discount_usd': round(price_discount, 2) if price_discount is not None else None,
        'cud_and_cache_credits_usd': round(credits, 2),
        'cud_credits_usd': round(cud_cred, 2) if cud_cred is not None else None,
        'contract_discount_credits_usd': round(contract_cred, 2) if contract_cred is not None else None,
        'promo_credits_usd': round(promo_cred, 2) if promo_cred is not None else None,
        'other_credits_usd': round(other_cred, 2) if other_cred is not None else None,
        'effective_savings_rate_pct': _pct_of(esr_part, gross),
        'net_invoice_usd': round(gross + credits, 2),
        'variance_pct': None,
        'currency': r.get('currency') or 'USD',
        'last_usage': r.get('last_usage'),
    })
  total_gross = round(sum(x['billing_export_gross_usd'] for x in ledger), 2)
  total_credits = round(sum(x['cud_and_cache_credits_usd'] for x in ledger), 2)
  split_known = bool(ledger) and split_rows == len(ledger)

  def _total(key: str) -> float | None:
    return round(sum(x[key] for x in ledger), 2) if split_known else None

  total_cud = _total('cud_credits_usd')
  total_contract = _total('contract_discount_credits_usd')
  credit_esr = _pct_of((total_cud or 0.0) + (total_contract or 0.0), total_gross) if split_known else None
  # Promotions can only be excluded when the credit split is known, so the list basis needs it too.
  list_basis = split_known and list_total > 0
  price_discount_total = list_total - listed_gross_total
  list_esr = (
      round((price_discount_total - listed_esr_credits) / list_total * 100.0, 1) if list_basis else None
  )
  by_service: dict[str, float] = {}
  for x in ledger:
    by_service[x['service']] = round(by_service.get(x['service'], 0.0) + x['net_invoice_usd'], 2)
  return {
      'status': 'LIVE',
      'source': 'Cloud Billing export (BigQuery)',
      'billing_export_table': table,
      'window_days': window_days,
      'sku_ledger': ledger,
      'net_by_service': dict(sorted(by_service.items(), key=lambda kv: -kv[1])),
      'total_gross_usd': total_gross,
      'total_credits_usd': total_credits,
      'credit_split_available': split_known,
      'total_cud_credits_usd': total_cud,
      'total_contract_discount_credits_usd': total_contract,
      'total_promo_credits_usd': _total('promo_credits_usd'),
      'total_other_credits_usd': _total('other_credits_usd'),
      'effective_savings_rate_pct': list_esr if list_basis else credit_esr,
      'esr_basis': 'LIST_PRICE' if list_basis else ('CREDITS_ONLY' if split_known else None),
      # Credits only (the lower bound), kept so the two bases can be compared.
      'credit_esr_pct': credit_esr,
      'total_list_usd': round(list_total, 2) if list_total > 0 else None,
      'price_discount_usd': round(price_discount_total, 2) if list_total > 0 else None,
      'list_price_rows_excluded': unlisted_rows if list_queried else None,
      'list_price_gross_excluded_usd': round(unlisted_gross, 2) if list_queried else None,
      'cud_savings_rate_pct': _pct_of(total_cud, total_gross),
      # All credits, promotions included: the "distorted" headline rate the ESR filters out.
      'blended_credit_rate_pct': _pct_of(total_credits, total_gross),
      'esr_method': ESR_METHOD_LIST if list_basis else ESR_METHOD,
      'total_net_invoice_usd': round(total_gross + total_credits, 2),
      'currency': ledger[0]['currency'] if ledger else 'USD',
      # Token estimates cover a different window (24h list price), so no invoice "match" is claimed.
      'reconciliation_delta_pct': None,
      'message': (
          f'{len(ledger)} billed SKUs in the last {window_days} days.'
          if ledger else f'No billed usage for this project in the last {window_days} days.'
      ),
  }


def not_connected_payload(message: str | None = None, status: str = 'NOT_CONNECTED', table: str | None = None) -> dict[str, Any]:
  return {
      'status': status,
      'source': 'Cloud Billing export (BigQuery)',
      'billing_export_table': table,
      'window_days': WINDOW_DAYS,
      'sku_ledger': [],
      'net_by_service': {},
      'total_gross_usd': None,
      'total_credits_usd': None,
      'credit_split_available': False,
      'total_cud_credits_usd': None,
      'total_contract_discount_credits_usd': None,
      'total_promo_credits_usd': None,
      'total_other_credits_usd': None,
      'effective_savings_rate_pct': None,
      'esr_basis': None,
      'credit_esr_pct': None,
      'total_list_usd': None,
      'price_discount_usd': None,
      'list_price_rows_excluded': None,
      'list_price_gross_excluded_usd': None,
      'list_price_column': None,
      'cud_savings_rate_pct': None,
      'blended_credit_rate_pct': None,
      'total_net_invoice_usd': None,
      'reconciliation_delta_pct': None,
      'message': message or HOW_TO_CONNECT,
      **_status_detail(status, message),
  }


class BillingExportReader:
  """Cached, non-blocking reader for the billing export."""

  def __init__(self, project_id: str, token_provider: Callable[[], str | None]):
    self.project_id = project_id
    self._token_provider = token_provider
    self._cache: dict[str, Any] | None = None
    self._cache_ts = 0.0
    self._lock = threading.Lock()
    self._inflight = False
    self._daily_cache: dict[str, Any] | None = None
    self._daily_cache_ts = 0.0
    self._daily_inflight = False
    # table -> (monotonic time, top-level column names), from the tables.get schema probe.
    self._schema_cache: dict[str, tuple[float, frozenset[str]]] = {}

  def get(self, non_blocking: bool = False) -> dict[str, Any]:
    table = configured_table()
    if not table:
      return not_connected_payload()
    now = time.monotonic()
    with self._lock:
      fresh = self._cache is not None and (now - self._cache_ts) < CACHE_TTL_S
      if fresh:
        return self._cache  # type: ignore[return-value]
      if non_blocking:
        if not self._inflight:
          self._inflight = True
          threading.Thread(target=self._refresh, args=(table,), daemon=True).start()
        return self._cache or not_connected_payload('Loading billing export…', status='LOADING', table=table)
    self._refresh(table)
    return self._cache or not_connected_payload(status='ERROR', table=table)

  def get_daily_ai_costs(self, non_blocking: bool = False, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Per-day billed AI spend for this project (see summarize_daily_cost_rows)."""
    table = configured_table()
    if not table:
      return daily_cost_not_connected()
    now = time.monotonic()
    with self._lock:
      if self._daily_cache is not None and (now - self._daily_cache_ts) < CACHE_TTL_S:
        return self._daily_cache
      if non_blocking:
        if not self._daily_inflight:
          self._daily_inflight = True
          threading.Thread(target=self._refresh_daily, args=(table, window_days), daemon=True).start()
        return self._daily_cache or daily_cost_not_connected('Loading billing export…', status='LOADING', table=table)
    self._refresh_daily(table, window_days)
    return self._daily_cache or daily_cost_not_connected(status='ERROR', table=table)

  def _refresh(self, table: str) -> None:
    try:
      result = self._query(table)
    except Exception as exc:  # pylint: disable=broad-except
      logger.warning('Billing export query failed: %s', exc)
      result = not_connected_payload(f'Billing export query failed: {exc}'[:300], status='ERROR', table=table)
    with self._lock:
      self._cache = result
      self._cache_ts = time.monotonic()
      self._inflight = False

  def _refresh_daily(self, table: str, window_days: int) -> None:
    try:
      rows, error = self._run(build_daily_cost_sql(table, self.project_id, window_days))
      if error:
        logger.warning('Billing export daily cost query failed: %s', error)
        result = daily_cost_not_connected(error, status='ERROR', table=table)
      else:
        result = summarize_daily_cost_rows(rows, table, window_days)
    except Exception as exc:  # pylint: disable=broad-except
      logger.warning('Billing export daily cost query failed: %s', exc)
      result = daily_cost_not_connected(f'Billing export query failed: {exc}'[:300], status='ERROR', table=table)
    with self._lock:
      self._daily_cache = result
      self._daily_cache_ts = time.monotonic()
      self._daily_inflight = False

  def _query(self, table: str) -> dict[str, Any]:
    columns = self._table_columns(table)
    # An unknown schema (metadata read failed) still tries the list-price column, then falls back.
    with_list = columns is None or LIST_PRICE_COLUMN in columns
    rows, error = self._run(build_billing_sql(table, self.project_id, include_list_price=with_list))
    if error and with_list and _LIST_COLUMN_ERROR_RE.search(error):
      logger.info('Billing export has no %s column; using the credits-only savings rate.', LIST_PRICE_COLUMN)
      with_list = False
      rows, error = self._run(build_billing_sql(table, self.project_id))
    if error:
      # _run returns HTTP, credential and timeout failures instead of raising: log them here.
      logger.warning('Billing export query failed: %s', error)
      return not_connected_payload(error, status='ERROR', table=table)
    out = summarize_rows(rows, table)
    out['list_price_column'] = 'present' if with_list else 'absent'
    return out

  def _table_columns(self, table: str) -> frozenset[str] | None:
    """Top-level column names of the export table, read with BigQuery tables.get (metadata, no query cost).

    Cached for SCHEMA_CACHE_TTL_S. Returns None when the metadata cannot be read; nothing is cached then.
    """
    now = time.monotonic()
    with self._lock:
      hit = self._schema_cache.get(table)
      if hit is not None and (now - hit[0]) < SCHEMA_CACHE_TTL_S:
        return hit[1]
    token = self._token_provider()
    if not token or not _TABLE_RE.match(table):
      return None
    # Dataset and table ids cannot contain dots, so this also keeps a domain-scoped project
    # (example.com:project) whole.
    project, dataset, name = table.rsplit('.', 2)
    url = (
        'https://bigquery.googleapis.com/bigquery/v2/projects/'
        f'{urllib.parse.quote(project, safe=":")}/datasets/{urllib.parse.quote(dataset, safe="")}'
        f'/tables/{urllib.parse.quote(name, safe="")}'
    )
    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {token}', 'x-goog-user-project': self.project_id},
        method='GET',
    )
    try:
      with urllib.request.urlopen(req, timeout=10.0) as resp:
        res = json.loads(resp.read().decode('utf-8'))
      fields = res['schema']['fields']
      columns = frozenset(str(f['name']) for f in fields if isinstance(f, dict) and f.get('name'))
    except Exception as exc:  # pylint: disable=broad-except
      logger.info('Billing export schema read failed for %s: %s', table, exc)
      return None
    if not columns:
      return None
    with self._lock:
      self._schema_cache[table] = (time.monotonic(), columns)
    return columns

  def _run(self, sql: str) -> tuple[list[dict[str, Any]], str | None]:
    """Runs a query over REST. Returns (rows, None) or ([], error message)."""
    token = self._token_provider()
    if not token:
      return [], 'No Google Cloud credentials available.'
    req = urllib.request.Request(
        f'https://bigquery.googleapis.com/bigquery/v2/projects/{self.project_id}/queries',
        data=json.dumps({
            'query': sql,
            'useLegacySql': False,
            'timeoutMs': 20000,
            'maximumBytesBilled': str(5 * 1024**3),
            'labels': {'datacloud': 'jetski', 'app': 'finops_dashboard'},
        }).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'x-goog-user-project': self.project_id,
        },
        method='POST',
    )
    try:
      with urllib.request.urlopen(req, timeout=25.0) as resp:
        res = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
      detail = ''
      try:
        detail = json.loads(exc.read().decode('utf-8')).get('error', {}).get('message', '')
      except Exception:  # pylint: disable=broad-except
        pass
      return [], f'BigQuery HTTP {exc.code}: {detail}'[:300]
    if not res.get('jobComplete', True):
      return [], 'Billing export query did not finish in time.'
    fields = [f.get('name') for f in res.get('schema', {}).get('fields', [])]
    return [dict(zip(fields, [c.get('v') for c in row.get('f', [])], strict=False)) for row in res.get('rows', [])], None


# ---------------------------------------------------------------------------------------------
# Daily AI spend, joined to the GE mart's daily usage (finops_mart.agg_daily_usage / fct_turns)
# ---------------------------------------------------------------------------------------------

# Billing services counted as AI spend. Project-level only: the export has no per-agent split,
# so cost is never allocated to individual agents or users.
AI_SERVICE_RE = re.compile(
    r'(vertex ai|gemini|discovery engine|agentspace|agent builder|generative language|agent platform)',
    re.IGNORECASE,
)


def build_daily_cost_sql(table: str, project_id: str, window_days: int = WINDOW_DAYS) -> str:
  """Per-day, per-service billed cost. Inputs are validated because they are interpolated."""
  if not _TABLE_RE.match(table):
    raise ValueError(f'Invalid billing export table name: {table!r}')
  if not _PROJECT_RE.match(project_id):
    raise ValueError(f'Invalid project id: {project_id!r}')
  days = max(1, min(int(window_days), 400))
  return f"""
    SELECT
      CAST(DATE(usage_start_time) AS STRING) AS day,
      service.description AS service,
      ROUND(SUM(cost), 6) AS gross_usd,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS credits_usd,
      ANY_VALUE(currency) AS currency
    FROM `{table}`
    WHERE project.id = '{project_id}'
      AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    GROUP BY day, service
    HAVING gross_usd != 0 OR credits_usd != 0
    ORDER BY day
  """


def daily_cost_not_connected(message: str | None = None, status: str = 'NOT_CONNECTED',
                             table: str | None = None) -> dict[str, Any]:
  return {
      'status': status,
      'source': 'Cloud Billing export (BigQuery)',
      'billing_export_table': table,
      'days': [],
      'last_billed_day': None,
      'currency': None,
      'message': message or HOW_TO_CONNECT,
      **_status_detail(status, message),
  }


def summarize_daily_cost_rows(rows: list[dict[str, Any]], table: str,
                              window_days: int = WINDOW_DAYS) -> dict[str, Any]:
  """Collapses per-day, per-service rows into per-day AI and total net spend."""
  by_day: dict[str, dict[str, Any]] = {}
  currency = None
  for r in rows:
    day = str(r.get('day') or '')
    if not day:
      continue
    try:
      net = float(r.get('gross_usd') or 0.0) + float(r.get('credits_usd') or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
      continue
    service = str(r.get('service') or '')
    currency = currency or r.get('currency')
    d = by_day.setdefault(day, {'day': day, 'ai_net_usd': 0.0, 'all_net_usd': 0.0, 'ai_services': []})
    d['all_net_usd'] = round(float(d['all_net_usd']) + net, 6)  # type: ignore[arg-type]
    if AI_SERVICE_RE.search(service):
      d['ai_net_usd'] = round(float(d['ai_net_usd']) + net, 6)  # type: ignore[arg-type]
      if service not in d['ai_services']:  # type: ignore[operator]
        d['ai_services'].append(service)  # type: ignore[union-attr]
  days = [by_day[k] for k in sorted(by_day)]
  return {
      'status': 'LIVE',
      'source': 'Cloud Billing export (BigQuery)',
      'billing_export_table': table,
      'window_days': window_days,
      'days': days,
      'last_billed_day': days[-1]['day'] if days else None,
      'currency': currency or 'USD',
      'message': f'{len(days)} billed days in the last {window_days} days.' if days else
                 f'No billed usage for this project in the last {window_days} days.',
  }


def join_daily_usage_with_cost(daily_usage: list[dict[str, Any]],
                               daily_cost: dict[str, Any]) -> list[dict[str, Any]]:
  """Joins per-day usage (from the GE mart) with per-day billed AI spend.

  Cost is project-level AI spend (every AI service in the project, not only Gemini Enterprise).
  It is None when billing is not connected, for days after the last exported billing day (the
  export lags by up to a day), and for days before the first exported day (the export may not
  have existed yet). Unit costs are None when their denominator is unknown or 0.
  """
  live = daily_cost.get('status') == 'LIVE'
  cost_by_day = {str(d.get('day')): d for d in (daily_cost.get('days') or []) if isinstance(d, dict)}
  last_billed = daily_cost.get('last_billed_day')
  first_billed = min(cost_by_day) if cost_by_day else None
  out = []
  for u in daily_usage:
    day = str(u.get('day') or '')
    ai_net: float | None = None
    if live and day:
      if day in cost_by_day:
        ai_net = float(cost_by_day[day].get('ai_net_usd') or 0.0)  # type: ignore[arg-type]
      elif first_billed and last_billed and str(first_billed) <= day <= str(last_billed):
        ai_net = 0.0  # exported day with no AI charges
    interactions = u.get('interactions')
    tokens = u.get('total_tokens')
    per_1k_turns = None
    if ai_net is not None and isinstance(interactions, int) and interactions > 0:
      per_1k_turns = round(ai_net / interactions * 1000.0, 4)
    per_1m_tokens = None
    if ai_net is not None and isinstance(tokens, int) and tokens > 0:
      per_1m_tokens = round(ai_net / tokens * 1_000_000.0, 4)
    row = dict(u)
    row.update({
        'ai_net_usd': round(ai_net, 4) if ai_net is not None else None,
        'usd_per_1k_turns': per_1k_turns,
        'usd_per_1m_tokens': per_1m_tokens,
        'cost_scope': 'project_ai_services' if live else None,
    })
    out.append(row)
  return out
