"""Cloud Billing export reader for VibeLift.

Reads real billed cost for this project from a Cloud Billing BigQuery export
(standard `gcp_billing_export_v1_*` or detailed `gcp_billing_export_resource_v1_*`).

The export table is configured with `VIBELIFT_BILLING_EXPORT_TABLE`
(`project.dataset.table`). When it is not configured, or the query fails, the
result says so explicitly (`NOT_CONNECTED` / `ERROR`); no numbers are invented.
"""

from collections.abc import Callable
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BILLING_TABLE_ENV = 'VIBELIFT_BILLING_EXPORT_TABLE'
WINDOW_DAYS = 30
CACHE_TTL_S = 900.0

_TABLE_RE = re.compile(r'^[a-z][a-z0-9\-]{4,61}[a-z0-9]\.[A-Za-z0-9_]{1,1024}\.[A-Za-z0-9_]{1,1024}$')
_PROJECT_RE = re.compile(r'^[a-z][a-z0-9\-]{4,61}[a-z0-9]$')

HOW_TO_CONNECT = (
    'Enable Cloud Billing export to BigQuery (Billing > Billing export), then set '
    f'{BILLING_TABLE_ENV}=<project>.<dataset>.gcp_billing_export_v1_<ID> on the service.'
)


def configured_table() -> str | None:
  table = os.environ.get(BILLING_TABLE_ENV, '').strip().strip('`')
  return table or None


def build_billing_sql(table: str, project_id: str, window_days: int = WINDOW_DAYS) -> str:
  """Builds the per-SKU billed cost query. Inputs are validated because they are interpolated."""
  if not _TABLE_RE.match(table):
    raise ValueError(f'Invalid billing export table name: {table!r}')
  if not _PROJECT_RE.match(project_id):
    raise ValueError(f'Invalid project id: {project_id!r}')
  days = max(1, min(int(window_days), 400))
  return f"""
    SELECT
      service.description AS service,
      sku.id AS sku_id,
      sku.description AS sku_description,
      ROUND(SUM(cost), 6) AS gross_usd,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS credits_usd,
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


def summarize_rows(rows: list[dict[str, object]], table: str, window_days: int = WINDOW_DAYS) -> dict[str, object]:
  """Turns BigQuery rows into the dashboard's billing payload."""

  def _f(v: object) -> float:
    try:
      return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
      return 0.0

  ledger = []
  for r in rows:
    gross = _f(r.get('gross_usd'))
    credits = _f(r.get('credits_usd'))  # negative in the export
    usage = r.get('usage_amount')
    unit = r.get('usage_unit') or ''
    ledger.append({
        'sku_id': r.get('sku_id') or '',
        'service': r.get('service') or '',
        'sku_description': r.get('sku_description') or '',
        'usage_volume': (f'{_f(usage):,.2f} {unit}'.strip() if usage is not None else None),
        'telemetry_estimated_usd': None,
        'billing_export_gross_usd': round(gross, 2),
        'cud_and_cache_credits_usd': round(credits, 2),
        'net_invoice_usd': round(gross + credits, 2),
        'variance_pct': None,
        'currency': r.get('currency') or 'USD',
        'last_usage': r.get('last_usage'),
    })
  total_gross = round(sum(x['billing_export_gross_usd'] for x in ledger), 2)
  total_credits = round(sum(x['cud_and_cache_credits_usd'] for x in ledger), 2)
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
      'total_net_invoice_usd': round(total_gross + total_credits, 2),
      'currency': ledger[0]['currency'] if ledger else 'USD',
      # Token estimates cover a different window (24h list price), so no invoice "match" is claimed.
      'reconciliation_delta_pct': None,
      'message': (
          f'{len(ledger)} billed SKUs in the last {window_days} days.'
          if ledger else f'No billed usage for this project in the last {window_days} days.'
      ),
  }


def not_connected_payload(message: str | None = None, status: str = 'NOT_CONNECTED', table: str | None = None) -> dict[str, object]:
  return {
      'status': status,
      'source': 'Cloud Billing export (BigQuery)',
      'billing_export_table': table,
      'window_days': WINDOW_DAYS,
      'sku_ledger': [],
      'net_by_service': {},
      'total_gross_usd': None,
      'total_credits_usd': None,
      'total_net_invoice_usd': None,
      'reconciliation_delta_pct': None,
      'message': message or HOW_TO_CONNECT,
  }


class BillingExportReader:
  """Cached, non-blocking reader for the billing export."""

  def __init__(self, project_id: str, token_provider: Callable[[], str | None]):
    self.project_id = project_id
    self._token_provider = token_provider
    self._cache: dict[str, object] | None = None
    self._cache_ts = 0.0
    self._lock = threading.Lock()
    self._inflight = False

  def get(self, non_blocking: bool = False) -> dict[str, object]:
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

  def _query(self, table: str) -> dict[str, object]:
    sql = build_billing_sql(table, self.project_id)
    token = self._token_provider()
    if not token:
      return not_connected_payload('No Google Cloud credentials available.', status='ERROR', table=table)
    req = urllib.request.Request(
        f'https://bigquery.googleapis.com/bigquery/v2/projects/{self.project_id}/queries',
        data=json.dumps({
            'query': sql,
            'useLegacySql': False,
            'timeoutMs': 20000,
            'maximumBytesBilled': str(5 * 1024**3),
            'labels': {'datacloud': 'jetski', 'app': 'vibelift'},
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
      return not_connected_payload(f'BigQuery HTTP {exc.code}: {detail}'[:300], status='ERROR', table=table)
    if not res.get('jobComplete', True):
      return not_connected_payload('Billing export query did not finish in time.', status='ERROR', table=table)
    fields = [f.get('name') for f in res.get('schema', {}).get('fields', [])]
    rows = [dict(zip(fields, [c.get('v') for c in row.get('f', [])])) for row in res.get('rows', [])]
    return summarize_rows(rows, table)
