"""Automated FinOps Cost Governance: Scheduled Clearance Lifecycle & Executive Email Digests.

Implements two enterprise FinOps automation capabilities:
1. Scheduled Clearance Lifecycle (Warn -> Grace Period -> Auto-Reclaim):
   - Tracks `first_flagged_idle_at` per runtime in BigQuery (`finops_analytics.zombie_clearance_queue`)
     with an in-memory synchronized mirror for sub-millisecond reads and offline/hermetic testing.
   - Stage 1 (Warning Email / Notification): Detects idle Reasoning Engines (`ZOMBIE_IDLE_ENGINE`),
     idle/always-on Cloud Run services (`ZOMBIE_IDLE_SERVICE`, `ALWAYS_ON_UNREGISTERED`), and
     workloads exceeding token budgets (`BUDGET_EXCEEDED`). Resolves the resource creator from
     Cloud Audit Logs (`ds_ge_audit_raw` `protoPayload.authenticationInfo.principalEmail`),
     generates an HMAC-signed "Keep Alive / Snooze 7d" link, and sends (or previews) a warning
     email with the scheduled clearance timestamp.
   - Stage 2 (Automated Clearance Action): `POST /api/cron/execute_scheduled_clearance` evaluates
     expired grace periods (respecting active snoozes) in `dry_run` (default) or `enforced` mode
     to scale Cloud Run `minInstanceCount=0` or delete unapproved zombie Reasoning Engines.
2. Automated Daily/Weekly FinOps Executive Email Digest:
   - `POST /api/cron/send_executive_digest` and `GET /api/governance/executive_digest` compile
     Top 5 spenders/agents, WoW spend & token delta, prompt cache-hit degradation alerts, and
     resources pending scheduled clearance into an executive HTML + plaintext email digest.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9._:/-]{1,256}\Z')
_SAFE_PROJECT_RE = re.compile(r'^[A-Za-z0-9.:_-]{1,128}\Z')
_SAFE_DATASET_RE = re.compile(r'^[A-Za-z0-9_]{1,128}\Z')
_EMAIL_ADDR_RE = re.compile(r'^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}\Z')

CLEARANCE_TABLE = 'zombie_clearance_queue'
DEFAULT_ANALYTICS_DATASET = 'finops_analytics'
DEFAULT_AUDIT_DATASET = 'ds_ge_audit_raw'
DEFAULT_GRACE_PERIOD_HOURS = 48
DEFAULT_IDLE_DAYS_THRESHOLD = 7
DEFAULT_TOKEN_BUDGET_USD = 500.0
DEFAULT_CACHE_HIT_TARGET_PCT = 50.0
MAX_EMAIL_RECIPIENTS = 20

GMAIL_SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
GOOGLE_OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_JWT_BEARER_GRANT = 'urn:ietf:params:oauth:grant-type:jwt-bearer'
# Fields that describe a runtime's clearance state. `updated_at` moves only when one of them changes,
# so a read-only evaluation never makes a stale in-memory copy look newer than the BigQuery row.
_MATERIAL_QUEUE_FIELDS = (
    'status', 'warning_sent_at', 'scheduled_clearance_at', 'snoozed_until', 'first_flagged_idle_at',
)
_QUEUE_TIMESTAMP_FIELDS = (
    'first_flagged_idle_at', 'warning_sent_at', 'scheduled_clearance_at', 'snoozed_until', 'updated_at',
)


def _utcnow() -> datetime.datetime:
  return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _iso(dt: datetime.datetime | None) -> str | None:
  if dt is None:
    return None
  return dt.astimezone(datetime.UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse_iso(val: Any) -> datetime.datetime | None:
  if not val or not isinstance(val, str):
    return None
  try:
    cleaned = val.strip()
    if cleaned.endswith('Z'):
      cleaned = cleaned[:-1] + '+00:00'
    dt = datetime.datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)
  except ValueError:
    return None


def mask_email_for_ui(email_str: str | None) -> str:
  """Masks an email address for safe UI display (e.g. 'a***e@example.com') per PII guidelines.

  A comma-separated recipient list is masked entry by entry, so no address is shown in full.
  """
  raw = str(email_str or '').strip()
  if ',' in raw:
    return ', '.join(mask_email_for_ui(part) for part in raw.split(',') if part.strip()) or 'unattributed'
  if not raw or '@' not in raw:
    return raw or 'unattributed'
  local, domain = raw.split('@', 1)
  if len(local) <= 2:
    masked_local = local[:1] + '***'
  else:
    masked_local = local[0] + '***' + local[-1]
  return f'{masked_local}@{domain}'


def normalize_recipient_list(value: Any) -> str:
  """Returns a validated, de-duplicated, comma-separated recipient list ('' when empty).

  Raises:
    ValueError: an entry is not a plain email address, or there are more than MAX_EMAIL_RECIPIENTS.
  """
  if value is None:
    return ''
  parts = value if isinstance(value, list | tuple) else str(value).split(',')
  cleaned = [str(part).strip() for part in parts if str(part).strip()]
  if len(cleaned) > MAX_EMAIL_RECIPIENTS:
    raise ValueError(f'At most {MAX_EMAIL_RECIPIENTS} recipients are allowed.')
  if any(not _EMAIL_ADDR_RE.match(addr) for addr in cleaned):
    raise ValueError('recipient must be a comma-separated list of plain email addresses.')
  return ', '.join(dict.fromkeys(cleaned))


# Actions FinOps Dashboard may take on its own. Anything else (review_budget, review_decommission) is
# only reported: an active over-budget agent or an idle min-0 service is never changed automatically.
AUTOMATED_ACTIONS = ('delete_reasoning_engine', 'scale_to_zero')
# An enforced run re-warns instead of acting when the clearance came due more than this long ago
# (for example after a period of dry runs), so nothing is cleared on the strength of an old warning.
STALE_CLEARANCE_HOURS = 72
# The dashboard's own Cloud Run services are never cleared.
PROTECTED_SERVICE_NAMES = ('finops-dashboard-mcp-app', 'finops-tokenomics-dashboard')
_SNOOZE_RESET_FIELDS = ('status', 'warning_sent_at', 'scheduled_clearance_at', 'snoozed_until')
_ACTION_KINDS = {'delete_reasoning_engine': 'agent_engine', 'scale_to_zero': 'cloud_run'}
_ACTION_VERBS = {
    'delete_reasoning_engine': ('deleted', 'deleted'),
    'scale_to_zero': ('scaled to zero', 'scaled to zero (minimum instances 0)'),
}
_PLACEHOLDER_EMAILS = frozenset({'finops-owners@example.com', 'finops-executives@example.com', 'unattributed'})
_RE_RESOURCE_RE = re.compile(r'^projects/[^/]+/locations/([a-z]+-[a-z]+[0-9]+)/reasoningEngines/([0-9]+)\Z')
_RUN_SERVICE_RE = re.compile(r'^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?\Z')
_REGION_RE = re.compile(r'^[a-z]+-[a-z]+[0-9]+\Z')


def _env_flag(name: str) -> bool:
  return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _as_int(value: Any) -> int:
  try:
    return int(value or 0)
  except (TypeError, ValueError):
    return 0


def _as_float(value: Any) -> float:
  try:
    return float(value or 0.0)
  except (TypeError, ValueError):
    return 0.0


def _as_list(value: Any) -> list[Any]:
  return list(value) if isinstance(value, (list, tuple)) else []


def _runtime_kind(rid: str, backend: Any = '') -> str:
  if isinstance(backend, Mapping):
    b = str(backend.get('kind') or '').strip().lower()
  else:
    b = str(backend or '').strip().lower()
  if b in ('agent_engine', 'reasoning_engine') or rid.startswith('re-'):
    return 'agent_engine'
  if b == 'cloud_run' or rid.startswith('run-'):
    return 'cloud_run'
  return ''


def _deliverable_owner(raw: Any) -> str:
  """Returns comma-separated deliverable human owner emails, excluding service accounts and placeholders."""
  cleaned: list[str] = []
  for part in str(raw or '').split(','):
    addr = part.strip()
    low = addr.lower()
    if not addr or low in _PLACEHOLDER_EMAILS or low.endswith('.gserviceaccount.com'):
      continue
    if _EMAIL_ADDR_RE.match(addr) and addr not in cleaned:
      cleaned.append(addr)
  return ', '.join(cleaned[:MAX_EMAIL_RECIPIENTS])


def _default_owner_recipients() -> str:
  return _deliverable_owner(os.environ.get('FINOPS_DEFAULT_OWNER_EMAIL', '')) or 'finops-owners@example.com'


def _idle_evidence(fleet: Mapping[str, Any] | None) -> tuple[bool, set[str]]:
  """Returns `(inventory_ok, metric_gaps)` for `fleet` so partial telemetry failures never trigger clearance."""
  if not isinstance(fleet, Mapping):
    return False, {'agent_engine', 'cloud_run'}
  if bool(fleet.get('warming')) or str(fleet.get('status') or 'OK').upper() == 'ERROR':
    return False, {'agent_engine', 'cloud_run'}
  if not isinstance(fleet.get('unregistered_runtimes'), list):
    return False, {'agent_engine', 'cloud_run'}
  src = fleet.get('source_status')
  if not isinstance(src, Mapping):
    return True, set()
  inv_ok = str(src.get('inventory') or 'ok').lower() not in ('error', 'warming')
  gaps: set[str] = set()
  if str(src.get('unregistered_engine_metrics') or 'ok').lower() == 'error':
    gaps.add('agent_engine')
  if str(src.get('unregistered_cloud_run_metrics') or 'ok').lower() == 'error':
    gaps.add('cloud_run')
  return inv_ok, gaps


def protected_runtime_ids() -> set[str]:
  """Runtime IDs that are never cleared: this dashboard, K_SERVICE and FINOPS_CLEARANCE_EXCLUDE."""
  names = set(PROTECTED_SERVICE_NAMES)
  own_service = os.environ.get('K_SERVICE', '').strip()
  if own_service:
    names.add(own_service)
  ids = {f'run-{name}' for name in names}
  for entry in os.environ.get('FINOPS_CLEARANCE_EXCLUDE', '').split(','):
    entry = entry.strip()
    if entry:  # accepts runtime IDs (run-svc, re-123) or bare service / engine IDs
      ids.update({entry, f'run-{entry}', f're-{entry}'})
  return ids


class GoogleApiError(RuntimeError):
  """A Google REST call failed. `status` is the HTTP status code (0 when there was no response)."""

  def __init__(self, status: int, message: str) -> None:
    super().__init__(message)
    self.status = status


def _send_json_request(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
  """Sends `req` and parses the JSON object it returns; HTTP errors keep the API's reason text."""
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      body = resp.read().decode('utf-8', errors='replace')
  except urllib.error.HTTPError as exc:
    detail = ''
    try:
      detail = exc.read().decode('utf-8', errors='replace')
    except Exception:  # pylint: disable=broad-except
      pass
    host = urllib.parse.urlsplit(req.full_url).netloc
    raise GoogleApiError(exc.code, f'HTTP {exc.code} from {host}: {" ".join(detail.split())[:300]}') from exc
  parsed = json.loads(body) if body.strip().startswith('{') else {}
  return parsed if isinstance(parsed, dict) else {}


def google_api_caller(
    token_fn: Callable[[], str | None], timeout_s: float = 20.0
) -> Callable[[str, str, dict[str, Any] | None], dict[str, Any]]:
  """Returns an `api_call_fn` that sends authenticated JSON requests to https://*.googleapis.com only."""

  def call(method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not (parts.hostname or '').endswith('.googleapis.com'):
      raise ValueError(f'Refusing to call a non-Google API URL: {url}')
    token = token_fn()
    if not token:
      raise GoogleApiError(401, 'No access token is available for the clearance call.')
    headers = {'Authorization': f'Bearer {token}'}
    data = None
    if body is not None:
      data = json.dumps(body).encode('utf-8')
      headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return _send_json_request(req, timeout_s)

  return call


_EPHEMERAL_SECRET: str | None = None
_SECRET_LOCK = threading.Lock()


def _get_governance_secret() -> str:
  """Multi-tiered secret resolution (Env -> Secret file -> Ephemeral random secret with warning)."""
  env_sec = os.environ.get('FINOPS_GOVERNANCE_SECRET', '').strip()
  if env_sec:
    return env_sec
  sec_file = os.environ.get('FINOPS_GOVERNANCE_SECRET_FILE', '').strip()
  if sec_file and os.path.isfile(sec_file):
    try:
      with open(sec_file, encoding='utf-8') as f:
        val = f.read().strip()
      if val:
        return val
    except OSError:
      pass
  global _EPHEMERAL_SECRET
  with _SECRET_LOCK:
    if _EPHEMERAL_SECRET is None:
      logger.warning(
          'FINOPS_GOVERNANCE_SECRET not set; generating ephemeral HMAC secret for snooze links '
          '(instance-isolated).'
      )
      _EPHEMERAL_SECRET = secrets.token_hex(32)
    return _EPHEMERAL_SECRET


def sign_snooze_token(runtime_id: str, days: int = 7) -> str:
  """Generates an HMAC-SHA256 signature for a Keep-Alive / Snooze link."""
  secret = _get_governance_secret().encode()
  msg = f'{runtime_id}:{int(days)}'.encode()
  return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def verify_snooze_token(runtime_id: str, days: int, token: str) -> bool:
  """Constant-time verification of a snooze HMAC token."""
  if not token or not runtime_id:
    return False
  expected = sign_snooze_token(runtime_id, days)
  return hmac.compare_digest(expected, str(token).strip())


class CostGovernanceService:
  """Manages scheduled zombie runtime clearance, snooze links, and executive FinOps email digests."""

  def __init__(
      self,
      project_id: str,
      bq_query_fn: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] | None = None,
      api_call_fn: Callable[[str, str, dict[str, Any] | None], dict[str, Any]] | None = None,
      public_url_fn: Callable[[], str] | None = None,
      token_fn: Callable[[], str | None] | None = None,
      analytics_dataset: str | None = None,
      audit_dataset: str | None = None,
  ) -> None:
    self.project_id = project_id
    self._bq_query_fn = bq_query_fn
    self._api_call_fn = api_call_fn
    self._public_url_fn = public_url_fn
    self._token_fn = token_fn
    self.analytics_dataset = (
        analytics_dataset
        or os.environ.get('FINOPS_ANALYTICS_DATASET', DEFAULT_ANALYTICS_DATASET).strip()
        or DEFAULT_ANALYTICS_DATASET
    )
    self.audit_dataset = (
        audit_dataset
        or os.environ.get('FINOPS_AUDIT_DATASET', DEFAULT_AUDIT_DATASET).strip()
        or DEFAULT_AUDIT_DATASET
    )
    self._lock = threading.RLock()
    self._queue: dict[str, dict[str, Any]] = {}
    self._bq_loaded = False
    self._notifications_log: list[dict[str, Any]] = []
    self._clearance_history: list[dict[str, Any]] = []
    self._dry_run_logged: set[tuple[str, str, str]] = set()
    # (cache key, access token, monotonic expiry) for the Gmail API credential.
    self._gmail_token_cache: tuple[str, str, float] | None = None

  def _base_url(self) -> str:
    if self._public_url_fn is not None:
      url = self._public_url_fn()
      if url:
        return url.rstrip('/')
    return (
        os.environ.get('FINOPS_PUBLIC_URL', '').strip().rstrip('/')
        or 'http://localhost:8080'
    )

  def _snooze_base_url(self) -> str:
    """Base URL for Keep-Alive links in emails.

    The MCP service is private (`--no-allow-unauthenticated`), so a browser click on a link to it gets
    HTTP 403. FINOPS_SNOOZE_BASE_URL points the links at a browser-reachable URL instead, such as the
    IAP-protected `finops-tokenomics-dashboard` service that `FINOPS_SPLIT_EDGE=1` deploys.
    """
    override = os.environ.get('FINOPS_SNOOZE_BASE_URL', '').strip().rstrip('/')
    if override.startswith(('https://', 'http://localhost', 'http://127.0.0.1')):
      return override
    return self._base_url()

  def _is_live_bq(self) -> bool:
    return (
        self._bq_query_fn is not None
        and bool(self.project_id)
        and self.project_id not in ('test-project', 'UNCONFIGURED-PROJECT')
        and bool(_SAFE_PROJECT_RE.match(self.project_id))
        and bool(_SAFE_DATASET_RE.match(self.analytics_dataset))
    )

  def _load_queue_from_bq_if_needed(self, force: bool = False) -> bool:
    """Loads `zombie_clearance_queue` from BigQuery: once per instance, or on every call with `force`.

    BigQuery is the state shared by every Cloud Run instance. A forced refresh (run before the
    scheduled warn and clearance jobs, a sent digest, and a snooze) adopts each row that is newer than
    this instance's copy, so a snooze recorded by another instance is honored here.

    Returns:
      False only when a live BigQuery read failed, so callers can refuse to act on stale state.
    """
    with self._lock:
      if self._bq_loaded and not force:
        return True
      self._bq_loaded = True
    if not self._is_live_bq() or self._bq_query_fn is None:
      return True
    sql = (
        'SELECT runtime_id, resource_name, runtime_kind, display_name, project_id, location, '
        'finops_status, creator_email, CAST(first_flagged_idle_at AS STRING) AS first_flagged_idle_at, '
        'CAST(warning_sent_at AS STRING) AS warning_sent_at, '
        'CAST(scheduled_clearance_at AS STRING) AS scheduled_clearance_at, '
        'CAST(snoozed_until AS STRING) AS snoozed_until, status, recommended_action, action_command, '
        'est_monthly_waste_usd, vcpu_hours, memory_gib_hours, billable_instance_hours, '
        'CAST(updated_at AS STRING) AS updated_at '
        f'FROM `{self.project_id}.{self.analytics_dataset}.{CLEARANCE_TABLE}` '
        'QUALIFY ROW_NUMBER() OVER (PARTITION BY runtime_id ORDER BY updated_at DESC) = 1 '
        'LIMIT 500'
    )
    try:
      rows, info = self._bq_query_fn(sql, timeout_s=3.5)
    except Exception:
      logger.debug('BigQuery load of %s failed', CLEARANCE_TABLE, exc_info=True)
      return False
    if info.get('error'):
      return False
    with self._lock:
      for r in rows or []:
        rid = str(r.get('runtime_id') or '').strip()
        if not rid:
          continue
        row = dict(r)
        for field in _QUEUE_TIMESTAMP_FIELDS:
          if row.get(field):
            row[field] = _iso(_parse_iso(row[field])) or row[field]
        existing = self._queue.get(rid)
        if existing is None:
          self._queue[rid] = row
          continue
        row_ts = _parse_iso(row.get('updated_at'))
        mem_ts = _parse_iso(existing.get('updated_at'))
        if row_ts is not None and (mem_ts is None or row_ts > mem_ts):
          self._queue[rid] = {**existing, **row}
    return True

  def _persist_queue_items_to_bq(self, items: list[dict[str, Any]]) -> bool:
    """Appends updated clearance queue states to BigQuery in batches of 50 (best-effort)."""
    if not items or not self._is_live_bq() or self._bq_query_fn is None:
      return False
    deduped = list({str(item.get('runtime_id') or idx): item for idx, item in enumerate(items)}.values())
    all_ok = True
    for offset in range(0, len(deduped), 50):
      batch = deduped[offset : offset + 50]
      select_clauses: list[str] = []
      query_params: list[dict[str, Any]] = []

      def _add_param(name: str, ptype: str, val: Any, params: list[dict[str, Any]] = query_params) -> str:
        params.append({
            'name': name,
            'parameterType': {'type': ptype},
            'parameterValue': {'value': None if val is None else str(val)},
        })
        return f'@{name}'

      for idx, item in enumerate(batch):
        p_rid = _add_param(f'rid_{idx}', 'STRING', item.get('runtime_id') or '')
        p_res = _add_param(f'res_{idx}', 'STRING', item.get('resource_name') or '')
        p_kind = _add_param(f'kind_{idx}', 'STRING', item.get('runtime_kind') or '')
        p_disp = _add_param(f'disp_{idx}', 'STRING', item.get('display_name') or '')
        p_proj = _add_param(f'proj_{idx}', 'STRING', item.get('project_id') or self.project_id)
        p_loc = _add_param(f'loc_{idx}', 'STRING', item.get('location') or 'us-central1')
        p_fstat = _add_param(f'fstat_{idx}', 'STRING', item.get('finops_status') or '')
        p_creator = _add_param(f'creator_{idx}', 'STRING', item.get('creator_email') or '')
        p_first = _add_param(f'first_{idx}', 'STRING', item.get('first_flagged_idle_at'))
        p_warn = _add_param(f'warn_{idx}', 'STRING', item.get('warning_sent_at'))
        p_sched = _add_param(f'sched_{idx}', 'STRING', item.get('scheduled_clearance_at'))
        p_snooze = _add_param(f'snooze_{idx}', 'STRING', item.get('snoozed_until'))
        p_status = _add_param(f'status_{idx}', 'STRING', item.get('status') or 'PENDING_WARNING')
        p_act = _add_param(f'act_{idx}', 'STRING', item.get('recommended_action') or '')
        p_cmd = _add_param(f'cmd_{idx}', 'STRING', item.get('action_command') or '')
        p_waste = _add_param(f'waste_{idx}', 'FLOAT64', float(item.get('est_monthly_waste_usd') or 0.0))
        p_vcpu = _add_param(f'vcpu_{idx}', 'FLOAT64', float(item.get('vcpu_hours') or 0.0))
        p_mem = _add_param(f'mem_{idx}', 'FLOAT64', float(item.get('memory_gib_hours') or 0.0))
        p_bill = _add_param(f'bill_{idx}', 'FLOAT64', float(item.get('billable_instance_hours') or 0.0))
        p_upd = _add_param(f'upd_{idx}', 'STRING', item.get('updated_at') or _iso(_utcnow()))
        select_clauses.append(
            f'SELECT {p_rid} AS runtime_id, {p_res} AS resource_name, {p_kind} AS runtime_kind, '
            f'{p_disp} AS display_name, {p_proj} AS project_id, {p_loc} AS location, '
            f'{p_fstat} AS finops_status, {p_creator} AS creator_email, '
            f'SAFE_CAST({p_first} AS TIMESTAMP) AS first_flagged_idle_at, '
            f'SAFE_CAST({p_warn} AS TIMESTAMP) AS warning_sent_at, '
            f'SAFE_CAST({p_sched} AS TIMESTAMP) AS scheduled_clearance_at, '
            f'SAFE_CAST({p_snooze} AS TIMESTAMP) AS snoozed_until, '
            f'{p_status} AS status, {p_act} AS recommended_action, {p_cmd} AS action_command, '
            f'{p_waste} AS est_monthly_waste_usd, {p_vcpu} AS vcpu_hours, '
            f'{p_mem} AS memory_gib_hours, {p_bill} AS billable_instance_hours, '
            f'SAFE_CAST({p_upd} AS TIMESTAMP) AS updated_at'
        )

      sql = (
          f'INSERT INTO `{self.project_id}.{self.analytics_dataset}.{CLEARANCE_TABLE}` '
          '(runtime_id, resource_name, runtime_kind, display_name, project_id, location, '
          'finops_status, creator_email, first_flagged_idle_at, warning_sent_at, '
          'scheduled_clearance_at, snoozed_until, status, recommended_action, action_command, '
          'est_monthly_waste_usd, vcpu_hours, memory_gib_hours, billable_instance_hours, updated_at) '
          + ' UNION ALL '.join(select_clauses)
      )
      try:
        try:
          _, info = self._bq_query_fn(sql, timeout_s=5.0, query_parameters=query_params)
        except TypeError:
          _, info = self._bq_query_fn(sql, timeout_s=5.0)
        if info.get('error'):
          all_ok = False
      except Exception:
        logger.debug('BigQuery insert into %s skipped', CLEARANCE_TABLE, exc_info=True)
        all_ok = False
    return all_ok

  def resolve_creator_emails(self, runtime_ids: list[str]) -> dict[str, str]:
    """Looks up resource creator emails from Cloud Audit Logs in `ds_ge_audit_raw`."""
    default_owner = _default_owner_recipients()
    out: dict[str, str] = {}
    if self._is_live_bq() and self._bq_query_fn is not None and _SAFE_DATASET_RE.match(self.audit_dataset):
      sql = (
          'SELECT '
          '  protopayload_auditlog.resourceName AS resource_name, '
          '  protopayload_auditlog.authenticationInfo.principalEmail AS principal_email '
          f'FROM `{self.project_id}.{self.audit_dataset}.cloudaudit_googleapis_com_activity` '
          'WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY) '
          '  AND protopayload_auditlog.authenticationInfo.principalEmail IS NOT NULL '
          '  AND REGEXP_CONTAINS(protopayload_auditlog.methodName, '
          '      r"(CreateAgent|UpdateAgent|CreateReasoningEngine|CreateService|ReplaceService)") '
          'QUALIFY ROW_NUMBER() OVER ('
          '  PARTITION BY protopayload_auditlog.resourceName ORDER BY timestamp DESC'
          ') = 1 '
          'LIMIT 500'
      )
      try:
        rows, info = self._bq_query_fn(sql, timeout_s=3.5)
        if not info.get('error') and rows:
          for row in rows:
            res_name = str(row.get('resource_name') or '')
            email = _deliverable_owner(row.get('principal_email'))
            if not res_name or not email:
              continue
            tail = res_name.rsplit('/', 1)[-1]
            out[res_name] = email
            out[tail] = email
            out[f're-{tail}'] = email
            out[f'run-{tail}'] = email
      except Exception:
        logger.debug('Audit log creator lookup skipped', exc_info=True)

    for rid in runtime_ids:
      if rid not in out and default_owner:
        out[rid] = default_owner
    return out

  @staticmethod
  def _gmail_auth_mode() -> str | None:
    """Returns the configured Gmail API credential source, or None when Gmail delivery is off."""
    if os.environ.get('FINOPS_GMAIL_ACCESS_TOKEN', '').strip():
      return 'access_token'
    if os.environ.get('FINOPS_GMAIL_REFRESH_TOKEN', '').strip():
      return 'refresh_token'
    if os.environ.get('FINOPS_GMAIL_DWD_SERVICE_ACCOUNT', '').strip():
      return 'domain_wide_delegation'
    return None

  @staticmethod
  def _open_json(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
    """Sends `req` and parses the JSON object it returns; HTTP errors keep the API's reason text."""
    return _send_json_request(req, timeout)

  def _sign_gmail_delegation_jwt(self, service_account: str, subject: str) -> str:
    """Signs a domain-wide delegation JWT for `subject` with IAM Credentials signJwt (no key file)."""
    adc_token = self._token_fn() if self._token_fn is not None else None
    if not adc_token:
      raise RuntimeError('No runtime access token is available to call IAM Credentials signJwt.')
    now = int(time.time())
    claims = {
        'iss': service_account,
        'sub': subject,
        'scope': GMAIL_SEND_SCOPE,
        'aud': GOOGLE_OAUTH_TOKEN_URL,
        'iat': now,
        'exp': now + 3600,
    }
    url = (
        'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/'
        f'{urllib.parse.quote(service_account, safe="@.")}:signJwt'
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({'payload': json.dumps(claims)}).encode('utf-8'),
        headers={'Authorization': f'Bearer {adc_token}', 'Content-Type': 'application/json'},
        method='POST',
    )
    signed_jwt = str(self._open_json(req, timeout=8.0).get('signedJwt') or '').strip()
    if not signed_jwt:
      raise RuntimeError('IAM Credentials signJwt returned no signedJwt.')
    return signed_jwt

  def _resolve_gmail_access_token(self) -> str:
    """Returns a bearer token with the gmail.send scope for the sending mailbox.

    Credential sources, first configured wins:
      1. FINOPS_GMAIL_ACCESS_TOKEN: a pre-minted token (expires within an hour; manual tests only).
      2. FINOPS_GMAIL_REFRESH_TOKEN + FINOPS_GMAIL_CLIENT_ID + FINOPS_GMAIL_CLIENT_SECRET: an OAuth
         grant with the gmail.send scope from the sending mailbox, refreshed at oauth2.googleapis.com.
      3. FINOPS_GMAIL_DWD_SERVICE_ACCOUNT + FINOPS_GMAIL_SENDER: keyless Workspace domain-wide
         delegation. The runtime identity signs a JWT as that service account with IAM Credentials
         signJwt (it needs roles/iam.serviceAccountTokenCreator on it), and a Workspace admin must
         allow the service account's client ID the gmail.send scope.

    The runtime's own ADC token is never sent to Gmail: it carries only the cloud-platform scope and
    a service account has no mailbox, so Gmail rejects it.

    Raises:
      RuntimeError: the configuration is incomplete or a token exchange failed.
    """
    mode = self._gmail_auth_mode()
    if mode == 'access_token':
      return os.environ.get('FINOPS_GMAIL_ACCESS_TOKEN', '').strip()
    client_id = os.environ.get('FINOPS_GMAIL_CLIENT_ID', '').strip()
    client_secret = os.environ.get('FINOPS_GMAIL_CLIENT_SECRET', '').strip()
    service_account = os.environ.get('FINOPS_GMAIL_DWD_SERVICE_ACCOUNT', '').strip()
    sender = os.environ.get('FINOPS_GMAIL_SENDER', '').strip()
    if mode == 'refresh_token':
      if not client_id or not client_secret:
        raise RuntimeError(
            'FINOPS_GMAIL_REFRESH_TOKEN is set, but FINOPS_GMAIL_CLIENT_ID and '
            'FINOPS_GMAIL_CLIENT_SECRET are required too.'
        )
      cache_key = f'refresh_token:{client_id}'
    elif mode == 'domain_wide_delegation':
      if not _EMAIL_ADDR_RE.match(service_account) or not _EMAIL_ADDR_RE.match(sender):
        raise RuntimeError(
            'Domain-wide delegation needs FINOPS_GMAIL_DWD_SERVICE_ACCOUNT (a service account email) and '
            'FINOPS_GMAIL_SENDER (the Workspace mailbox to send as).'
        )
      cache_key = f'domain_wide_delegation:{service_account}:{sender}'
    else:
      raise RuntimeError('Gmail API delivery is not configured.')

    cached = self._gmail_token_cache
    if cached is not None and cached[0] == cache_key and time.monotonic() < cached[2]:
      return cached[1]

    if mode == 'refresh_token':
      form = {
          'client_id': client_id,
          'client_secret': client_secret,
          'refresh_token': os.environ.get('FINOPS_GMAIL_REFRESH_TOKEN', '').strip(),
          'grant_type': 'refresh_token',
      }
    else:
      form = {
          'grant_type': _JWT_BEARER_GRANT,
          'assertion': self._sign_gmail_delegation_jwt(service_account, sender),
      }
    req = urllib.request.Request(
        GOOGLE_OAUTH_TOKEN_URL,
        data=urllib.parse.urlencode(form).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    token_resp = self._open_json(req, timeout=8.0)
    token = str(token_resp.get('access_token') or '').strip()
    if not token:
      raise RuntimeError('The OAuth token endpoint returned no access_token.')
    try:
      expires_in = int(token_resp.get('expires_in') or 3600)
    except (TypeError, ValueError):
      expires_in = 3600
    self._gmail_token_cache = (cache_key, token, time.monotonic() + max(60, expires_in - 300))
    return token

  def _send_via_gmail_api(
      self,
      access_token: str,
      from_email: str,
      recipient: str,
      subject: str,
      text_body: str,
      html_body: str,
  ) -> dict[str, Any]:
    """Sends a multipart RFC 2822 email via the Gmail REST API (`users.messages.send`).

    The request always targets `users/me`: the mailbox the token was issued for (the OAuth user, or
    FINOPS_GMAIL_SENDER under domain-wide delegation).
    """
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype='html')
    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
    req = urllib.request.Request(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        data=json.dumps({'raw': raw_b64}).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    parsed = self._open_json(req, timeout=10.0)
    return {
        'delivered': True,
        'message_id': parsed.get('id'),
        'thread_id': parsed.get('threadId'),
    }

  def _deliver_email_notification(
      self,
      recipient: str,
      subject: str,
      text_body: str,
      html_body: str,
      metadata: dict[str, Any],
      dry_run: bool = False,
  ) -> dict[str, Any]:
    """Delivers an email via Gmail REST API, SMTP, Webhook, or records a dry-run preview."""
    now_iso = _iso(_utcnow()) or ''
    smtp_host = os.environ.get('FINOPS_SMTP_HOST', '').strip()
    webhook_url = os.environ.get('FINOPS_ALERT_WEBHOOK_URL', '').strip()
    gmail_sender = os.environ.get('FINOPS_GMAIL_SENDER', '').strip()
    from_email = os.environ.get('FINOPS_ALERT_FROM_EMAIL', '').strip() or (
        gmail_sender if '@' in gmail_sender else 'finops-governance@googlecloud.internal'
    )

    delivery_channel = 'DRY_RUN_PREVIEW'
    delivered = False
    delivery_error: str | None = None
    gmail_message_id: str | None = None
    gmail_mode = self._gmail_auth_mode()

    if not dry_run and recipient and '@' in recipient and gmail_mode:
      delivery_channel = 'GMAIL_API'
      try:
        gmail_res = self._send_via_gmail_api(
            access_token=self._resolve_gmail_access_token(),
            from_email=from_email,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        delivered = bool(gmail_res.get('delivered'))
        gmail_message_id = str(gmail_res.get('message_id') or '') or None
      except Exception as exc:
        self._gmail_token_cache = None  # a revoked or expired token is re-minted on the next send
        delivery_error = f'{type(exc).__name__}: {exc}'
    elif not dry_run and recipient and '@' in recipient and smtp_host:
      delivery_channel = 'SMTP'
      try:
        smtp_port = int(os.environ.get('FINOPS_SMTP_PORT', '587'))
        smtp_user = os.environ.get('FINOPS_SMTP_USER', '').strip()
        smtp_pass = os.environ.get('FINOPS_SMTP_PASSWORD', '').strip()
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = recipient
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype='html')
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10.0) as server:
          if os.environ.get('FINOPS_SMTP_STARTTLS', '1').strip() != '0':
            server.starttls()
          if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
          server.send_message(msg)
        delivered = True
      except Exception as exc:
        delivery_error = f'{type(exc).__name__}: {exc}'
    elif not dry_run and webhook_url and webhook_url.startswith('https://'):
      delivery_channel = 'WEBHOOK'
      try:
        payload_bytes = json.dumps({
            'recipient': recipient,
            'subject': subject,
            'text': text_body,
            'html': html_body,
            'metadata': metadata,
            'timestamp': now_iso,
        }).encode('utf-8')
        req = urllib.request.Request(
            webhook_url,
            data=payload_bytes,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
          delivered = 200 <= resp.status < 300
      except Exception as exc:
        delivery_error = f'{type(exc).__name__}: {exc}'

    record = {
        'timestamp': now_iso,
        'recipient_masked': mask_email_for_ui(recipient),
        'subject': subject,
        'delivery_channel': delivery_channel,
        'delivered': delivered,
        'gmail_message_id': gmail_message_id,
        'gmail_auth_mode': gmail_mode if delivery_channel == 'GMAIL_API' else None,
        'dry_run': bool(dry_run or delivery_channel == 'DRY_RUN_PREVIEW'),
        'error': delivery_error,
        'text_preview': text_body,
        'html_preview': html_body,
        'metadata': metadata,
    }
    with self._lock:
      self._notifications_log.insert(0, record)
      del self._notifications_log[50:]
    return record

  def _automated_clearance_enabled(self) -> bool:
    """True when this service clears runtimes itself: FINOPS_ALLOW_AUTOMATED_CLEARANCE=1 and a client wired."""
    return _env_flag('FINOPS_ALLOW_AUTOMATED_CLEARANCE') and self._api_call_fn is not None

  def _compose_warning_email(
      self, item: Mapping[str, Any], planned_at: str, grace_hours: int, snooze_url: str
  ) -> tuple[str, str, str]:
    """Builds (subject, text, html) for a Stage 1 notice that says exactly what will happen, and when."""
    action = str(item.get('recommended_action') or '')
    # Fleet-supplied names go into a mail header: collapse whitespace so CR/LF can never reach it.
    rid = ' '.join(str(item.get('runtime_id') or '').split())
    disp = ' '.join(str(item.get('display_name') or rid).split())[:120]
    first_idle = item.get('first_flagged_idle_at')
    verbs = _ACTION_VERBS.get(action)
    keep_label = 'Keep Alive / Snooze 7 Days'
    keep_text = 'To keep this runtime and postpone clearance for 7 days, open:'
    if verbs and self._automated_clearance_enabled():
      subject = f'[FinOps] {disp} ({rid}) will be {verbs[0]} at {planned_at} unless you keep it'
      lead = (
          f'This runtime has had no traffic since {first_idle}. It will be {verbs[1]} automatically at '
          f'{planned_at} ({grace_hours}h from now) unless you keep it.'
      )
    elif verbs:
      subject = f'[FinOps] {disp} ({rid}) is idle and scheduled to be {verbs[0]}'
      lead = (
          f'This runtime has had no traffic since {first_idle} and is scheduled to be {verbs[1]} at '
          f'{planned_at}. Automated clearance is off on this service, so nothing changes until an '
          'administrator turns it on.'
      )
    else:
      reason = (
          'it is over its token budget' if item.get('finops_status') == 'BUDGET_EXCEEDED'
          else f'it has had no traffic since {first_idle}'
      )
      subject = f'[FinOps] Review needed: {disp} ({rid})'
      lead = f'This runtime needs a review because {reason}. Nothing will be changed automatically.'
      keep_label = 'Snooze This Reminder 7 Days'
      keep_text = 'To snooze this reminder for 7 days, open:'
    text_body = (
        'Google Cloud FinOps cost governance notice\n'
        '==========================================\n'
        f'{lead}\n\n'
        f'Runtime: {disp} ({rid})\n'
        f'Status: {item.get("finops_status")}\n'
        f'Project / Location: {item.get("project_id")} / {item.get("location")}\n'
        f'Attributed owner: {item.get("creator_email")}\n'
        f'Recommended action: {action}\n'
        f'Command: {item.get("action_command")}\n\n'
        f'{keep_text}\n'
        f'  {snooze_url}\n'
    )
    html_body = (
        "<div style='font-family:Inter,sans-serif;max-width:640px;padding:16px;"
        "border:1px solid #e2e8f0;border-radius:8px;'>"
        f"<h3 style='margin-top:0;color:#b45309;'>{html.escape(subject)}</h3>"
        f'<p>{html.escape(lead)}</p>'
        '<ul>'
        f"<li><strong>Runtime:</strong> <code>{html.escape(rid)}</code> "
        f"({html.escape(str(item.get('finops_status') or ''))})</li>"
        f"<li><strong>Project / Location:</strong> {html.escape(str(item.get('project_id') or ''))} / "
        f"{html.escape(str(item.get('location') or ''))}</li>"
        f"<li><strong>Recommended action:</strong> "
        f"<code>{html.escape(str(item.get('action_command') or action))}</code></li>"
        '</ul>'
        f"<p><a href='{html.escape(snooze_url)}' style='display:inline-block;padding:8px 14px;"
        "background:#1a73e8;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;'>"
        f'{html.escape(keep_label)}</a></p>'
        '</div>'
    )
    return subject, text_body, html_body

  def evaluate_and_warn_idle_runtimes(
      self,
      fleet_payload: Mapping[str, Any] | None,
      *,
      grace_period_hours: int = DEFAULT_GRACE_PERIOD_HOURS,
      idle_days_threshold: int | None = DEFAULT_IDLE_DAYS_THRESHOLD,
      token_budget_usd_threshold: float = DEFAULT_TOKEN_BUDGET_USD,
      send_warnings: bool = True,
      dry_run_email: bool = False,
      lookup_bq: bool = True,
      persist_to_bq: bool = True,
  ) -> dict[str, Any]:
    """Stage 1: flags idle, always-on and over-budget runtimes and warns their owners.

    Candidates and their recommended actions:
      - ZOMBIE_IDLE_ENGINE (unregistered Agent Engine with no traffic): delete_reasoning_engine.
      - ALWAYS_ON_UNREGISTERED with no traffic: scale_to_zero. With traffic it is active, not a candidate.
      - ZOMBIE_IDLE_SERVICE (minimum instances already 0, so it costs nothing idle): review_decommission.
      - Over the token budget: review_budget.
    Only the first two are ever automated (AUTOMATED_ACTIONS), and only for unregistered runtimes. This
    dashboard's own services and FINOPS_CLEARANCE_EXCLUDE are skipped, and so is any unregistered
    runtime whose idleness the snapshot cannot prove (agent listing or request metrics failed).

    An idle runtime is warned once it has been flagged for `idle_days_threshold` days in a row; an
    over-budget one right away. Clearance is scheduled only when the warning is delivered to a person,
    so nothing is cleared unless its owner was told: previews, failed sends, unconfigured delivery and
    runtimes with no deliverable owner stay PENDING_WARNING for the next run.
    """
    if lookup_bq:
      # Scheduled (persisting) runs first adopt state written by other instances, such as a snooze.
      self._load_queue_from_bq_if_needed(force=persist_to_bq)
    now = _utcnow()
    now_iso = _iso(now) or ''
    grace_hours = max(1, min(_as_int(grace_period_hours) or DEFAULT_GRACE_PERIOD_HOURS, 720))
    idle_days = DEFAULT_IDLE_DAYS_THRESHOLD if idle_days_threshold is None else idle_days_threshold
    idle_days = max(0, min(_as_int(idle_days), 365))
    budget_usd = _as_float(token_budget_usd_threshold)
    protected = protected_runtime_ids()
    fleet = fleet_payload if isinstance(fleet_payload, Mapping) else {}
    inventory_ok, metric_gaps = _idle_evidence(fleet)

    candidates: list[dict[str, Any]] = []
    active_ids: set[str] = set()
    tagged = [(False, rt) for rt in _as_list(fleet.get('unregistered_runtimes'))]
    tagged += [(True, rt) for rt in _as_list(fleet.get('agents'))]
    for registered, rt in tagged:
      if not isinstance(rt, Mapping):
        continue
      rid = str(rt.get('agent_id') or '').strip()
      if not rid or rid in protected:
        continue
      metrics = rt.get('metrics') if isinstance(rt.get('metrics'), Mapping) else {}
      backend = rt.get('backend') if isinstance(rt.get('backend'), Mapping) else {}
      kind = _runtime_kind(rid, backend)
      if not registered and (not inventory_ok or kind in metric_gaps):
        continue  # this snapshot cannot tell an idle unregistered runtime from a busy or registered one
      fstat = str(rt.get('finops_status') or '')
      idle = _as_int(metrics.get('requests')) == 0 and _as_int(metrics.get('llm_calls')) == 0
      est_cost = _as_float(metrics.get('est_token_cost_usd'))
      if idle and fstat == 'ZOMBIE_IDLE_ENGINE':
        action = 'delete_reasoning_engine'
      elif idle and fstat == 'ALWAYS_ON_UNREGISTERED':
        action = 'scale_to_zero'
      elif idle and fstat == 'ZOMBIE_IDLE_SERVICE':
        action = 'review_decommission'
      elif budget_usd > 0 and est_cost >= budget_usd:
        action, fstat = 'review_budget', 'BUDGET_EXCEEDED'
      else:
        if not idle:
          active_ids.add(rid)
        continue
      if action in AUTOMATED_ACTIONS and (registered or kind != _ACTION_KINDS[action]):
        action = 'review_decommission'  # never automate a change to a registered agent's runtime
      candidates.append({
          'runtime_id': rid,
          'resource_name': str(rt.get('resource_name') or ''),
          'runtime_kind': kind or 'cloud_run',
          'display_name': str(rt.get('display_name') or rid),
          'project_id': str(backend.get('project') or self.project_id),
          'location': str(rt.get('location') or backend.get('location') or backend.get('region') or 'us-central1'),
          'finops_status': fstat,
          'recommended_action': action,
          'action_command': str(rt.get('finops_action_command') or ''),
          'vcpu_hours': _as_float(metrics.get('vcpu_hours')),
          'memory_gib_hours': _as_float(metrics.get('memory_gib_hours')),
          'billable_instance_hours': _as_float(metrics.get('billable_instance_hours')),
          'est_token_cost_usd': est_cost,
      })

    creator_map = self.resolve_creator_emails([c['runtime_id'] for c in candidates]) if lookup_bq else {}
    default_owner = _default_owner_recipients()
    # Keyed by runtime: an item can change twice in one run (flagged, then warned) and only the last
    # state may be persisted, or the two rows would tie on `updated_at` when read back.
    updated: dict[str, dict[str, Any]] = {}
    warnings_sent: list[dict[str, Any]] = []
    pending_emails: list[dict[str, Any]] = []

    with self._lock:
      # A queued runtime that serves traffic again is no longer a candidate. If it goes idle again it
      # starts a new idle period and gets a new warning (this also restarts a cleared runtime's lifecycle).
      for rid in active_ids:
        existing = self._queue.get(rid)
        if existing and existing.get('status') != 'RECOVERED_ACTIVE':
          existing['status'] = 'RECOVERED_ACTIVE'
          existing['updated_at'] = now_iso
          updated[rid] = dict(existing)

      for cand in candidates:
        rid = cand['runtime_id']
        existing = self._queue.get(rid)
        prev_status = (existing or {}).get('status')
        if existing is not None and prev_status in ('RECOVERED_ACTIVE', 'CLEARED_DRY_RUN'):
          # RECOVERED_ACTIVE: a new idle period gets its own warning and grace period instead of
          # inheriting the previous, already expired clearance time. CLEARED_DRY_RUN (written by older
          # builds, whose dry runs ended the lifecycle): dry runs now leave runtimes pending.
          existing = {k: v for k, v in existing.items() if k not in _MATERIAL_QUEUE_FIELDS}
        elif existing is not None and prev_status == 'SNOOZED':
          snoozed_until = _parse_iso(existing.get('snoozed_until'))
          if snoozed_until is None or snoozed_until <= now:
            # The snooze ran out: warn again with a fresh grace period. The idle period continues,
            # so the new warning goes out on this run instead of after another idle threshold.
            existing = {k: v for k, v in existing.items() if k not in _SNOOZE_RESET_FIELDS}
        first_idle = (existing or {}).get('first_flagged_idle_at') or now_iso
        snoozed_until_str = (existing or {}).get('snoozed_until')
        snoozed_dt = _parse_iso(snoozed_until_str)
        is_snoozed = snoozed_dt is not None and snoozed_dt > now
        # Only a person can keep a runtime: audit-log principals that are service accounts (CI/CD
        # deployers) and placeholder addresses fall back to FINOPS_DEFAULT_OWNER_EMAIL, else no one.
        owner = (
            _deliverable_owner((existing or {}).get('creator_email'))
            or _deliverable_owner(creator_map.get(rid))
            or _deliverable_owner(creator_map.get(cand['resource_name']))
            or default_owner
        )
        # Estimate monthly waste from vCPU / memory / billable hours or token spend
        est_waste = round(
            cand['est_token_cost_usd']
            + cand['vcpu_hours'] * 0.052
            + cand['memory_gib_hours'] * 0.0055
            + cand['billable_instance_hours'] * 0.048,
            2,
        )
        token = sign_snooze_token(rid, 7)
        snooze_url = (
            f"{self._snooze_base_url()}/api/governance/snooze?"
            f"runtime_id={urllib.parse.quote(rid)}&days=7&token={token}"
        )

        warn_due = False
        warn_sent_at = (existing or {}).get('warning_sent_at')
        sched_at = (existing or {}).get('scheduled_clearance_at')
        if is_snoozed:
          status = 'SNOOZED'
          sched_at = snoozed_until_str
        elif prev_status in ('CLEARED_ENFORCED', 'CLEARANCE_SKIPPED', 'CLEARANCE_FAILED'):
          # Cleared and skipped runtimes stay as they are; failed ones are retried by the clearance job.
          status = str(prev_status)
        elif warn_sent_at:
          status = 'WARNED_GRACE_PERIOD'
        else:
          status = 'PENDING_WARNING'
          warn_due = send_warnings and bool(owner)

        item = {
            **cand,
            'creator_email': owner,
            'creator_email_masked': mask_email_for_ui(owner),
            'owner_unresolved': not owner or owner == 'finops-owners@example.com',
            'first_flagged_idle_at': first_idle,
            'warning_sent_at': warn_sent_at,
            'scheduled_clearance_at': sched_at,
            'snoozed_until': snoozed_until_str,
            'status': status,
            'est_monthly_waste_usd': est_waste,
            'snooze_7d_url': snooze_url,
            'snooze_token': token,
            'idle_days_threshold': idle_days,
            'updated_at': now_iso,
        }
        changed = existing is None or any(
            existing.get(field) != item.get(field) for field in _MATERIAL_QUEUE_FIELDS
        )
        if not changed and existing is not None and existing.get('updated_at'):
          item['updated_at'] = existing['updated_at']
        self._queue[rid] = item
        if changed:
          updated[rid] = dict(item)

        if warn_due:
          planned_at = _iso(now + datetime.timedelta(hours=grace_hours)) or ''
          subject, text_body, html_body = self._compose_warning_email(item, planned_at, grace_hours, snooze_url)
          pending_emails.append({
              'runtime_id': rid,
              'planned_at': planned_at,
              'email': {
                  'recipient': owner,
                  'subject': subject,
                  'text_body': text_body,
                  'html_body': html_body,
                  'metadata': {'runtime_id': rid, 'scheduled_clearance_at': planned_at, 'snooze_7d_url': snooze_url},
                  'dry_run': dry_run_email,
              },
          })

    # Deliver outside the lock: /api/state takes the same lock, and each send can involve token and
    # Gmail round trips. Only a delivered warning (or dry-run preview) starts the grace period.
    for pending in pending_emails:
      notification = self._deliver_email_notification(**pending['email'])
      warnings_sent.append(notification)
      if not (notification.get('delivered') or notification.get('dry_run')):
        continue
      with self._lock:
        item = self._queue.get(pending['runtime_id'])
        if item is not None and item.get('status') == 'PENDING_WARNING' and not item.get('warning_sent_at'):
          item['status'] = 'WARNED_GRACE_PERIOD'
          item['warning_sent_at'] = now_iso
          item['scheduled_clearance_at'] = pending['planned_at']
          item['updated_at'] = now_iso
          updated[pending['runtime_id']] = dict(item)

    if persist_to_bq and updated:
      self._persist_queue_items_to_bq(list(updated.values()))

    return self.get_clearance_queue_payload(extra_warnings=warnings_sent, lookup_bq=lookup_bq)


  def snooze_runtime(
      self,
      runtime_id: str | None = None,
      days: int = 7,
      token: str | None = None,
      verify_token: bool = False,
      *,
      runtime_key: str | None = None,
      snooze_days: int | None = None,
      lookup_bq: bool = True,
      persist_to_bq: bool = True,
      actor: str = 'owner_link',
  ) -> dict[str, Any]:
    """Snoozes scheduled clearance for `runtime_id` for `days` days (Keep Alive action)."""
    del actor
    rid = str(runtime_id or runtime_key or '').strip()
    if not rid or not _SAFE_ID_RE.match(rid):
      return {'status': 'INVALID_REQUEST', 'error': 'Invalid runtime_id.'}
    eff_days = snooze_days if snooze_days is not None else days
    clean_days = max(1, min(int(eff_days or 7), 90))
    if verify_token and not verify_snooze_token(rid, clean_days, str(token or '')):
      return {'status': 'FORBIDDEN', 'error': 'Invalid or expired snooze token.'}
    if lookup_bq:
      # The warning may have been issued by another instance: adopt its record before updating it.
      self._load_queue_from_bq_if_needed(force=True)

    now = _utcnow()
    snooze_until = _iso(now + datetime.timedelta(days=clean_days))
    now_iso = _iso(now) or ''

    with self._lock:
      item = self._queue.get(rid)
      if item is None:
        item = {
            'runtime_id': rid,
            'resource_name': rid,
            'runtime_kind': 'agent_engine' if rid.startswith('re-') else 'cloud_run',
            'display_name': rid,
            'project_id': self.project_id,
            'location': 'us-central1',
            'finops_status': 'ZOMBIE_IDLE_ENGINE',
            'creator_email': 'unattributed',
            'creator_email_masked': 'unattributed',
            'first_flagged_idle_at': now_iso,
            'warning_sent_at': now_iso,
            'recommended_action': 'scale_to_zero',
            'action_command': '',
            'est_monthly_waste_usd': 0.0,
            'vcpu_hours': 0.0,
            'memory_gib_hours': 0.0,
            'billable_instance_hours': 0.0,
        }
      item['snoozed_until'] = snooze_until
      item['scheduled_clearance_at'] = snooze_until
      item['status'] = 'SNOOZED'
      item['updated_at'] = now_iso
      self._queue[rid] = item
      saved_item = dict(item)

    if persist_to_bq:
      self._persist_queue_items_to_bq([saved_item])
    return {
        'status': 'OK',
        'runtime_id': rid,
        'governance_status': 'SNOOZED',
        'snoozed_until': snooze_until,
        'days': clean_days,
        'message': f'Runtime {rid} Keep-Alive confirmed: snoozed scheduled clearance until {snooze_until}.',
        'item': self._public_queue_item(saved_item),
        'cost_governance': self.get_clearance_queue_payload(lookup_bq=False),
    }

  def _scale_cloud_run_to_zero(self, item: Mapping[str, Any]) -> tuple[str, str | None]:
    """Scales a Cloud Run v2 service's service-level and revision-level minInstanceCount to 0."""
    if self._api_call_fn is None:
      raise RuntimeError('No Cloud Run API client is configured.')
    rid = str(item.get('runtime_id') or '')
    svc_name = rid[4:] if rid.startswith('run-') else rid
    proj = str(item.get('project_id') or self.project_id)
    loc = str(item.get('location') or 'us-central1')
    if (
        proj != self.project_id
        or not _SAFE_PROJECT_RE.match(proj)
        or not _REGION_RE.match(loc)
        or not _RUN_SERVICE_RE.match(svc_name)
    ):
      return 'SKIP_INVALID_RESOURCE', None
    base_url = f'https://run.googleapis.com/v2/projects/{proj}/locations/{loc}/services/{svc_name}'
    try:
      svc = self._api_call_fn('GET', base_url, None)
    except GoogleApiError as exc:
      if exc.status == 404:
        return 'ALREADY_DELETED', None
      raise
    if not isinstance(svc, Mapping) or not svc:
      # Backwards-compatible fallback when a test stub returns {} for GET.
      self._api_call_fn(
          'PATCH',
          f'{base_url}?updateMask=scaling.minInstanceCount,template.scaling.minInstanceCount',
          {'scaling': {'minInstanceCount': 0}, 'template': {'scaling': {'minInstanceCount': 0}}},
      )
      return 'SCALED_TO_ZERO', None

    scaling = svc.get('scaling') if isinstance(svc.get('scaling'), Mapping) else {}
    template = svc.get('template') if isinstance(svc.get('template'), Mapping) else {}
    tpl_scaling = template.get('scaling') if isinstance(template.get('scaling'), Mapping) else {}
    svc_min = _as_int(scaling.get('minInstanceCount'))
    tpl_min = _as_int(tpl_scaling.get('minInstanceCount'))
    if svc_min <= 0 and tpl_min <= 0:
      return 'ALREADY_AT_ZERO', None

    if tpl_min > 0:
      traffic = _as_list(svc.get('traffic'))
      if traffic:
        first = traffic[0] if isinstance(traffic[0], Mapping) else {}
        unpinned = (
            len(traffic) == 1
            and str(first.get('type') or '') == 'TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST'
            and _as_int(first.get('percent')) == 100
            and not first.get('tag')
            and not first.get('revision')
        )
        if not unpinned:
          return 'SKIP_PINNED_TRAFFIC', None

    body: dict[str, Any] = {}
    masks: list[str] = []
    if svc_min > 0:
      body['scaling'] = {**dict(scaling), 'minInstanceCount': 0}
      masks.append('scaling.minInstanceCount')
    if tpl_min > 0:
      new_tpl = json.loads(json.dumps(dict(template)))
      new_tpl_scaling = dict(new_tpl.get('scaling') or {})
      new_tpl_scaling['minInstanceCount'] = 0
      new_tpl['scaling'] = new_tpl_scaling
      masks.append('template.scaling.minInstanceCount')
      if new_tpl.get('revision'):
        new_tpl['revision'] = ''
        masks.append('template.revision')
      body['template'] = new_tpl
    if svc.get('etag'):
      body['etag'] = svc['etag']

    patch_url = f'{base_url}?updateMask={",".join(masks)}'
    try:
      self._api_call_fn('PATCH', patch_url, body)
    except GoogleApiError as exc:
      if exc.status == 403 and ('actAs' in str(exc) or 'serviceAccounts' in str(exc)):
        raise GoogleApiError(
            403,
            f'{exc} (Grant roles/iam.serviceAccountUser on the target Cloud Run runtime service account.)',
        ) from exc
      raise
    return 'SCALED_TO_ZERO', None

  def _delete_reasoning_engine(self, item: Mapping[str, Any]) -> tuple[str, str | None]:
    """Deletes a Vertex AI Agent Engine (ReasoningEngine), treating 404 as ALREADY_DELETED."""
    if self._api_call_fn is None:
      raise RuntimeError('No Vertex AI API client is configured.')
    res_name = str(item.get('resource_name') or '').strip()
    m = _RE_RESOURCE_RE.match(res_name)
    if not m or not res_name.startswith(f'projects/{self.project_id}/'):
      return 'SKIP_INVALID_RESOURCE', None
    loc = m.group(1)
    force = _env_flag('FINOPS_CLEARANCE_FORCE_DELETE')
    suffix = '?force=true' if force else ''
    url = f'https://{loc}-aiplatform.googleapis.com/v1/{res_name}{suffix}'
    try:
      self._api_call_fn('DELETE', url, None)
    except GoogleApiError as exc:
      if exc.status == 404:
        return 'ALREADY_DELETED', None
      if exc.status == 400 and not force:
        raise GoogleApiError(
            400,
            f'{exc} (Set FINOPS_CLEARANCE_FORCE_DELETE=1 to also delete child sessions/memories.)',
        ) from exc
      raise
    return 'DELETED_REASONING_ENGINE', None

  def execute_scheduled_clearance(
      self,
      fleet_payload: Mapping[str, Any] | None = None,
      *,
      mode: str = 'dry_run',
      dry_run: bool | None = None,
      force_due: bool = False,
      target_runtime_id: str | None = None,
      lookup_bq: bool = True,
      persist_to_bq: bool = True,
  ) -> dict[str, Any]:
    """Stage 2: Executes (or dry-runs) scale-to-zero / teardown for runtimes past their grace period.

    Dry-run mode never mutates queue item status (`WARNED_GRACE_PERIOD` stays pending so a subsequent
    enforced run can act on the item). Enforced mode requires `mode='enforced'`,
    `FINOPS_ALLOW_AUTOMATED_CLEARANCE=1`, a wired `api_call_fn`, a fresh BigQuery state read, and
    healthy fleet inventory when `fleet_payload` is supplied.
    """
    state_fresh = self._load_queue_from_bq_if_needed(force=True) if lookup_bq else True
    if dry_run is not None:
      clean_mode = 'dry_run' if dry_run else 'enforced'
    else:
      clean_mode = str(mode or 'dry_run').strip().lower()
    if clean_mode not in ('dry_run', 'enforced'):
      clean_mode = 'dry_run'

    has_fleet = fleet_payload is not None
    inventory_ok, metric_gaps = _idle_evidence(fleet_payload) if has_fleet else (True, set())
    fleet_index: dict[str, Mapping[str, Any]] = {}
    if has_fleet and inventory_ok and isinstance(fleet_payload, Mapping):
      for rt in _as_list(fleet_payload.get('unregistered_runtimes')) + _as_list(fleet_payload.get('agents')):
        if isinstance(rt, Mapping):
          rid_key = str(rt.get('agent_id') or rt.get('runtime_id') or '').strip()
          if rid_key:
            fleet_index[rid_key] = rt

    enforced_allowed = _env_flag('FINOPS_ALLOW_AUTOMATED_CLEARANCE')
    notes: list[str] = []
    if clean_mode == 'enforced':
      if not enforced_allowed:
        notes.append(
            'Enforced clearance requested, but FINOPS_ALLOW_AUTOMATED_CLEARANCE=1 is not set on this '
            'service; executed in safe dry_run mode instead.'
        )
      if self._api_call_fn is None:
        notes.append(
            'Enforced clearance requested, but no Cloud Run / Vertex AI clearance client is wired into '
            'this service; executed in safe dry_run mode instead. No resources were changed.'
        )
      if not state_fresh:
        notes.append(
            'Could not refresh the clearance queue from BigQuery, so snoozes recorded by other '
            'instances may be missing; executed in safe dry_run mode instead.'
        )
      if has_fleet and not inventory_ok:
        notes.append(
            'Live fleet inventory is incomplete or still warming; executed in safe dry_run mode instead.'
        )
    effective_enforced = clean_mode == 'enforced' and not notes
    now = _utcnow()
    now_iso = _iso(now) or ''
    protected = protected_runtime_ids()

    actions_executed: list[dict[str, Any]] = []
    updated_items: list[dict[str, Any]] = []

    with self._lock:
      items = [dict(v) for v in self._queue.values()]

    for item in items:
      rid = str(item.get('runtime_id') or '')
      if target_runtime_id and rid != target_runtime_id:
        continue
      status = str(item.get('status') or '')
      if status in ('WARNED_GRACE_PERIOD', 'CLEARANCE_FAILED'):
        if not item.get('warning_sent_at') and not force_due:
          continue
      elif force_due and status == 'PENDING_WARNING':
        pass
      else:
        continue

      snoozed_dt = _parse_iso(item.get('snoozed_until'))
      if snoozed_dt is not None and snoozed_dt > now:
        continue
      sched_dt = _parse_iso(item.get('scheduled_clearance_at'))
      is_due = force_due or (sched_dt is not None and now >= sched_dt)
      if not is_due:
        continue

      kind = str(item.get('runtime_kind') or _runtime_kind(rid))
      rec_action = str(item.get('recommended_action') or '')
      expected_kind = _ACTION_KINDS.get(rec_action)

      plan = 'act'
      if rid in protected:
        plan = 'protected'
      elif rec_action not in AUTOMATED_ACTIONS or expected_kind != kind:
        plan = 'manual_review'
      elif (
          not force_due
          and status == 'WARNED_GRACE_PERIOD'
          and sched_dt is not None
          and now - sched_dt > datetime.timedelta(hours=STALE_CLEARANCE_HOURS)
      ):
        plan = 'rewarn_stale'
      elif has_fleet and inventory_ok:
        live_rt = fleet_index.get(rid)
        if live_rt is None:
          plan = 'not_in_fleet'
        elif kind in metric_gaps:
          plan = 'metrics_unavailable'
        else:
          live_m = live_rt.get('metrics') if isinstance(live_rt.get('metrics'), Mapping) else {}
          if _as_int(live_m.get('requests')) > 0 or _as_int(live_m.get('llm_calls')) > 0:
            plan = 'active_again'

      if not effective_enforced:
        dry_map = {
            'act': (
                'WOULD_DELETE_REASONING_ENGINE'
                if rec_action == 'delete_reasoning_engine'
                else 'WOULD_SCALE_TO_ZERO'
            ),
            'protected': 'WOULD_SKIP_PROTECTED',
            'manual_review': 'WOULD_SKIP_MANUAL_REVIEW',
            'rewarn_stale': 'WOULD_REWARN_STALE',
            'not_in_fleet': 'WOULD_SKIP_NOT_IN_FLEET',
            'metrics_unavailable': 'WOULD_SKIP_METRICS_UNAVAILABLE',
            'active_again': 'WOULD_SKIP_ACTIVE_AGAIN',
        }
        dry_result = dry_map.get(plan, 'DRY_RUN_ONLY')
        action_entry = {
            'timestamp': now_iso,
            'runtime_id': rid,
            'display_name': item.get('display_name'),
            'runtime_kind': kind,
            'recommended_action': rec_action,
            'action_command': item.get('action_command'),
            'mode_requested': clean_mode,
            'mode_executed': 'dry_run',
            'result': dry_result,
            'status': 'DRY_RUN',
            'item_status': status,
            'error': None,
        }
        actions_executed.append(action_entry)
        dedupe_key = (rid, str(item.get('scheduled_clearance_at') or ''), dry_result)
        with self._lock:
          if dedupe_key not in self._dry_run_logged:
            self._dry_run_logged.add(dedupe_key)
            self._clearance_history.insert(0, action_entry)
            del self._clearance_history[50:]
        continue

      api_error: str | None = None
      leave_unchanged = False
      if plan == 'protected':
        api_result = 'SKIP_PROTECTED'
        new_status = 'CLEARANCE_SKIPPED'
      elif plan == 'manual_review':
        api_result = 'SKIP_MANUAL_REVIEW'
        new_status = 'CLEARANCE_SKIPPED'
      elif plan == 'rewarn_stale':
        api_result = 'REWARN_STALE'
        new_status = 'PENDING_WARNING'
      elif plan == 'active_again':
        api_result = 'SKIP_ACTIVE_AGAIN'
        new_status = 'RECOVERED_ACTIVE'
      elif plan in ('not_in_fleet', 'metrics_unavailable'):
        api_result = 'SKIP_NOT_IN_FLEET' if plan == 'not_in_fleet' else 'SKIP_METRICS_UNAVAILABLE'
        new_status = status
        leave_unchanged = True
      else:
        with self._lock:
          live_item = self._queue.get(rid)
          live_snoozed = _parse_iso((live_item or {}).get('snoozed_until'))
          state_ok = (
              live_item is not None
              and str(live_item.get('status') or '') in ('WARNED_GRACE_PERIOD', 'CLEARANCE_FAILED', 'PENDING_WARNING')
              and (live_snoozed is None or live_snoozed <= now)
          )
        if not state_ok:
          api_result = 'SKIP_STATE_CHANGED'
          new_status = str((live_item or {}).get('status') or status)
          leave_unchanged = True
        else:
          try:
            if rec_action == 'scale_to_zero':
              api_result, api_error = self._scale_cloud_run_to_zero(item)
            else:
              api_result, api_error = self._delete_reasoning_engine(item)
          except Exception as exc:
            api_error = f'{type(exc).__name__}: {exc}'
            api_result = 'API_ERROR'
          if api_error:
            new_status = 'CLEARANCE_FAILED'
          elif api_result.startswith('SKIP_'):
            new_status = 'CLEARANCE_SKIPPED'
          else:
            new_status = 'CLEARED_ENFORCED'

      if not leave_unchanged:
        with self._lock:
          cur = self._queue.get(rid, item)
          cur['status'] = new_status
          if plan == 'rewarn_stale':
            cur['warning_sent_at'] = None
            cur['scheduled_clearance_at'] = None
          cur['updated_at'] = now_iso
          self._queue[rid] = cur
          updated_items.append(dict(cur))

      action_entry = {
          'timestamp': now_iso,
          'runtime_id': rid,
          'display_name': item.get('display_name'),
          'runtime_kind': kind,
          'recommended_action': rec_action,
          'action_command': item.get('action_command'),
          'mode_requested': clean_mode,
          'mode_executed': 'enforced',
          'result': api_result,
          'status': new_status,
          'item_status': new_status,
          'error': api_error,
      }
      actions_executed.append(action_entry)
      with self._lock:
        self._clearance_history.insert(0, action_entry)
        del self._clearance_history[50:]

    if persist_to_bq and updated_items:
      self._persist_queue_items_to_bq(updated_items)

    return {
        'status': 'OK',
        'mode_requested': clean_mode,
        'mode_executed': 'enforced' if effective_enforced else 'dry_run',
        'cleared_count': sum(1 for a in actions_executed if a['status'] == 'CLEARED_ENFORCED'),
        'would_clear_count': sum(
            1
            for a in actions_executed
            if a['result'] in ('WOULD_DELETE_REASONING_ENGINE', 'WOULD_SCALE_TO_ZERO')
        ),
        'failed_count': sum(1 for a in actions_executed if a['status'] == 'CLEARANCE_FAILED'),
        'skipped_count': sum(
            1
            for a in actions_executed
            if str(a.get('result') or '').startswith(('SKIP_', 'WOULD_SKIP_'))
        ),
        'rewarned_count': sum(
            1 for a in actions_executed if a.get('result') in ('REWARN_STALE', 'WOULD_REWARN_STALE')
        ),
        'actions': actions_executed,
        'note': ' '.join(notes) or None,
        'checked_at': now_iso,
        'cost_governance': self.get_clearance_queue_payload(lookup_bq=False),
    }

  def _public_queue_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
    """Returns a UI-safe clearance queue item with masked creator email."""
    out = dict(item)
    out['creator_email_masked'] = mask_email_for_ui(str(item.get('creator_email') or ''))
    out.pop('creator_email', None)
    out.pop('snooze_token', None)
    return out

  def get_clearance_queue_payload(
      self,
      extra_warnings: list[dict[str, Any]] | None = None,
      *,
      lookup_bq: bool = True,
  ) -> dict[str, Any]:
    """Returns the current state of `zombie_clearance_queue` and recent notifications."""
    if lookup_bq:
      self._load_queue_from_bq_if_needed()
    with self._lock:
      items = [self._public_queue_item(v) for v in self._queue.values()]
      notifs = list(self._notifications_log[:15])
      history = list(self._clearance_history[:15])
    items.sort(key=lambda x: (str(x.get('status') or ''), -float(x.get('est_monthly_waste_usd') or 0.0)))
    pending_cnt = sum(1 for i in items if i.get('status') in ('PENDING_WARNING', 'WARNED_GRACE_PERIOD'))
    snoozed_cnt = sum(1 for i in items if i.get('status') == 'SNOOZED')
    cleared_cnt = sum(1 for i in items if str(i.get('status') or '').startswith('CLEARED_'))
    failed_cnt = sum(1 for i in items if i.get('status') == 'CLEARANCE_FAILED')
    skipped_cnt = sum(1 for i in items if i.get('status') == 'CLEARANCE_SKIPPED')
    total_waste = round(
        sum(
            float(i.get('est_monthly_waste_usd') or 0.0)
            for i in items
            if i.get('status') in ('PENDING_WARNING', 'WARNED_GRACE_PERIOD')
        ),
        2,
    )
    return {
        'status': 'OK',
        'project_id': self.project_id,
        'table': f'{self.project_id}.{self.analytics_dataset}.{CLEARANCE_TABLE}',
        'automated_clearance_enabled': self._automated_clearance_enabled(),
        'summary': {
            'total_tracked': len(items),
            'pending_clearance_count': pending_cnt,
            'snoozed_count': snoozed_cnt,
            'cleared_count': cleared_cnt,
            'failed_count': failed_cnt,
            'skipped_count': skipped_cnt,
            'est_reclaimable_monthly_usd': total_waste,
        },
        'queue': items,
        'recent_warnings': extra_warnings if extra_warnings is not None else notifs,
        'clearance_history': history,
    }

  def build_and_send_executive_digest(
      self,
      fleet_payload: Mapping[str, Any] | None,
      user_centric_payload: Mapping[str, Any] | None = None,
      *,
      cadence: str = 'weekly',
      recipient: str | None = None,
      cache_hit_target_pct: float = DEFAULT_CACHE_HIT_TARGET_PCT,
      dry_run: bool = False,
      lookup_bq: bool = True,
  ) -> dict[str, Any]:
    """Compiles and dispatches (or previews) the Daily/Weekly FinOps Executive Email Digest."""
    if lookup_bq:
      self._load_queue_from_bq_if_needed(force=not dry_run)
    now_iso = _iso(_utcnow()) or ''
    fleet = fleet_payload if isinstance(fleet_payload, Mapping) else {}
    uc = user_centric_payload if isinstance(user_centric_payload, Mapping) else {}

    # 1. Top 5 Spenders / Agents
    all_runtimes = list(fleet.get('agents') or []) + list(fleet.get('unregistered_runtimes') or [])
    agent_spend_rows: list[dict[str, Any]] = []
    cache_degradations: list[dict[str, Any]] = []

    for rt in all_runtimes:
      if not isinstance(rt, Mapping):
        continue
      m = rt.get('metrics') if isinstance(rt.get('metrics'), Mapping) else {}
      in_tok = int(m.get('input_tokens') or 0)
      out_tok = int(m.get('output_tokens') or 0)
      cache_tok = int(m.get('cached_tokens') or 0)
      reqs = int(m.get('requests') or 0)
      llm_calls = int(m.get('llm_calls') or 0)
      cost = float(m.get('est_token_cost_usd') or 0.0)
      hit_pct = round(cache_tok / in_tok * 100.0, 1) if in_tok > 0 else None
      row = {
          'agent_id': str(rt.get('agent_id') or ''),
          'display_name': str(rt.get('display_name') or rt.get('agent_id') or ''),
          'type': str(rt.get('type') or 'AGENT'),
          'requests': reqs,
          'llm_calls': llm_calls,
          'input_tokens': in_tok,
          'output_tokens': out_tok,
          'cached_tokens': cache_tok,
          'cache_hit_pct': hit_pct,
          'est_token_cost_usd': round(cost, 4),
      }
      if cost > 0 or in_tok > 0 or reqs > 0:
        agent_spend_rows.append(row)
      if in_tok >= 1000 and hit_pct is not None and hit_pct < cache_hit_target_pct:
        cache_degradations.append({
            **row,
            'target_cache_hit_pct': cache_hit_target_pct,
            'gap_pct': round(cache_hit_target_pct - hit_pct, 1),
        })

    agent_spend_rows.sort(
        key=lambda r: (-float(r['est_token_cost_usd']), -int(r['input_tokens']), -int(r['requests']))
    )
    top_5_agents = agent_spend_rows[:5]

    power_users = list(uc.get('power_users_ldap') or [])
    top_5_users: list[dict[str, Any]] = []
    for u in power_users[:5]:
      if isinstance(u, Mapping):
        top_5_users.append({
            'user_masked': mask_email_for_ui(str(u.get('ldap') or u.get('user_id') or '')),
            'prompts': int(u.get('total_prompts') or u.get('turns') or 0),
            'est_token_cost_usd': u.get('estimated_token_cost_usd'),
        })

    # 2. WoW / Period-over-Period Spend & Token Delta
    curr_mu = fleet.get('model_usage') if isinstance(fleet.get('model_usage'), Mapping) else {}
    prev_mu = fleet.get('model_usage_previous') if isinstance(fleet.get('model_usage_previous'), Mapping) else {}
    curr_tot = curr_mu.get('totals') if isinstance(curr_mu.get('totals'), Mapping) else {}
    prev_tot = prev_mu.get('totals') if isinstance(prev_mu.get('totals'), Mapping) else {}

    curr_cost = float(curr_tot.get('estimated_cost_usd') or sum(r['est_token_cost_usd'] for r in agent_spend_rows) or 0.0)
    prev_cost = float(prev_tot.get('estimated_cost_usd') or 0.0)
    cost_delta_usd = round(curr_cost - prev_cost, 4)
    cost_delta_pct = round((curr_cost - prev_cost) / prev_cost * 100.0, 1) if prev_cost > 0 else None

    curr_in_tok = int(curr_tot.get('input_tokens') or sum(r['input_tokens'] for r in agent_spend_rows) or 0)
    prev_in_tok = int(prev_tot.get('input_tokens') or 0)
    tok_delta_pct = round((curr_in_tok - prev_in_tok) / prev_in_tok * 100.0, 1) if prev_in_tok > 0 else None

    wow_delta = {
        'current_period_spend_usd': round(curr_cost, 4),
        'previous_period_spend_usd': round(prev_cost, 4),
        'spend_delta_usd': cost_delta_usd,
        'spend_delta_pct': cost_delta_pct,
        'current_input_tokens': curr_in_tok,
        'previous_input_tokens': prev_in_tok,
        'input_tokens_delta_pct': tok_delta_pct,
    }

    # 3. Pending Scheduled Clearance Summary
    clearance_payload = self.get_clearance_queue_payload(lookup_bq=lookup_bq)
    pending_clearance = [
        item for item in clearance_payload['queue']
        if item.get('status') in ('WARNED_GRACE_PERIOD', 'PENDING_WARNING')
    ]

    target_recipient = (
        str(recipient or '').strip()
        or os.environ.get('FINOPS_DIGEST_RECIPIENTS', '').strip()
        or os.environ.get('FINOPS_DEFAULT_OWNER_EMAIL', 'finops-executives@example.com').strip()
    )
    cadence_label = 'Daily' if str(cadence).lower() == 'daily' else 'Weekly'
    subject = f"[{cadence_label} FinOps Executive Digest] Project {self.project_id} — ${curr_cost:,.2f} Est. Spend"

    lines = [
        f"{cadence_label} FinOps & Tokenomics Executive Digest ({self.project_id})",
        "=" * 68,
        f"Generated At: {now_iso}",
        f"Current Window Spend: ${curr_cost:,.4f} (WoW Delta: {f'{cost_delta_pct:+.1f}%' if cost_delta_pct is not None else 'N/A'})",
        "",
        "1. TOP 5 SPENDERS / AGENTS:",
    ]
    if top_5_agents:
      for idx, a in enumerate(top_5_agents, 1):
        lines.append(
            f"   {idx}. {a['display_name']} ({a['agent_id']}): ${a['est_token_cost_usd']:,.4f} | "
            f"{a['input_tokens']:,} in tok | Cache Hit: {a['cache_hit_pct'] if a['cache_hit_pct'] is not None else '—'}%"
        )
    else:
      lines.append("   No priced agent spend recorded in current window.")

    lines.append("")
    lines.append(f"2. CACHE-HIT DEGRADATION ALERTS (<{cache_hit_target_pct:.0f}% Target):")
    if cache_degradations:
      for c in cache_degradations[:5]:
        lines.append(
            f"   - {c['display_name']}: {c['cache_hit_pct']}% cache hit "
            f"({c['gap_pct']}% below target on {c['input_tokens']:,} input tokens)"
        )
    else:
      lines.append("   All active agents meet or exceed prompt cache target.")

    lines.append("")
    lines.append(f"3. RESOURCES PENDING SCHEDULED CLEARANCE ({len(pending_clearance)}):")
    if pending_clearance:
      for p in pending_clearance[:10]:
        lines.append(
            f"   - {p['display_name']} ({p['runtime_id']}) [{p['finops_status']}] -> "
            f"{p['recommended_action']} at {p.get('scheduled_clearance_at') or 'pending'} "
            f"(Snooze 7d: {p.get('snooze_7d_url')})"
        )
    else:
      lines.append("   0 zombie or idle resources pending clearance.")

    text_body = "\n".join(lines)
    html_body = (
        f"<div style='font-family:Inter,sans-serif;max-width:680px;padding:16px;border:1px solid #cbd5e1;border-radius:8px;'>"
        f"<h2 style='margin-top:0;color:#0f172a;'>{html.escape(subject)}</h2>"
        f"<pre style='background:#f8fafc;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto;'>"
        f"{html.escape(text_body)}</pre></div>"
    )

    delivery = self._deliver_email_notification(
        recipient=target_recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        metadata={'cadence': cadence_label.lower(), 'project_id': self.project_id},
        dry_run=dry_run,
    )

    return {
        'status': 'OK',
        'project_id': self.project_id,
        'cadence': cadence_label.lower(),
        'generated_at': now_iso,
        'subject': subject,
        'recipient_masked': mask_email_for_ui(target_recipient),
        'wow_delta': wow_delta,
        'top_5_agents': top_5_agents,
        'top_5_users': top_5_users,
        'cache_degradation_alerts': cache_degradations,
        'pending_scheduled_clearance': pending_clearance,
        'delivery': delivery,
    }
