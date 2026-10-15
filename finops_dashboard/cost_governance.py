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
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9._:/-]{1,256}\Z')
_SAFE_PROJECT_RE = re.compile(r'^[A-Za-z0-9.:_-]{1,128}\Z')
_SAFE_DATASET_RE = re.compile(r'^[A-Za-z0-9_]{1,128}\Z')

CLEARANCE_TABLE = 'zombie_clearance_queue'
DEFAULT_ANALYTICS_DATASET = 'finops_analytics'
DEFAULT_AUDIT_DATASET = 'ds_ge_audit_raw'
DEFAULT_GRACE_PERIOD_HOURS = 48
DEFAULT_IDLE_DAYS_THRESHOLD = 7
DEFAULT_TOKEN_BUDGET_USD = 500.0
DEFAULT_CACHE_HIT_TARGET_PCT = 50.0


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
  """Masks an email address for safe UI display (e.g. 'a***e@example.com') per PII guidelines."""
  raw = str(email_str or '').strip()
  if not raw or '@' not in raw:
    return raw or 'unattributed'
  local, domain = raw.split('@', 1)
  if len(local) <= 2:
    masked_local = local[:1] + '***'
  else:
    masked_local = local[0] + '***' + local[-1]
  return f'{masked_local}@{domain}'


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

  def _base_url(self) -> str:
    if self._public_url_fn is not None:
      url = self._public_url_fn()
      if url:
        return url.rstrip('/')
    return (
        os.environ.get('FINOPS_PUBLIC_URL', '').strip().rstrip('/')
        or 'http://localhost:8080'
    )

  def _is_live_bq(self) -> bool:
    return (
        self._bq_query_fn is not None
        and bool(self.project_id)
        and self.project_id not in ('test-project', 'UNCONFIGURED-PROJECT')
        and bool(_SAFE_PROJECT_RE.match(self.project_id))
        and bool(_SAFE_DATASET_RE.match(self.analytics_dataset))
    )

  def _load_queue_from_bq_if_needed(self) -> None:
    with self._lock:
      if self._bq_loaded:
        return
      self._bq_loaded = True
    if not self._is_live_bq() or self._bq_query_fn is None:
      return
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
      if info.get('error') or not rows:
        return
      with self._lock:
        for r in rows:
          rid = str(r.get('runtime_id') or '').strip()
          if rid and rid not in self._queue:
            self._queue[rid] = dict(r)
    except Exception:
      logger.debug('Skipping initial BigQuery load of %s', CLEARANCE_TABLE, exc_info=True)

  def _persist_queue_items_to_bq(self, items: list[dict[str, Any]]) -> bool:
    """Appends updated clearance queue states to BigQuery (best-effort, non-blocking on error)."""
    if not items or not self._is_live_bq() or self._bq_query_fn is None:
      return False
    # Build parameterized query values for up to 50 items
    batch = items[:50]
    select_clauses: list[str] = []
    query_params: list[dict[str, Any]] = []

    def _add_param(name: str, ptype: str, val: Any) -> str:
      query_params.append({
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
        # Fallback if a custom test mock bq_query_fn doesn't accept query_parameters
        _, info = self._bq_query_fn(sql, timeout_s=5.0)
      return not bool(info.get('error'))
    except Exception:
      logger.debug('BigQuery insert into %s skipped', CLEARANCE_TABLE, exc_info=True)
      return False

  def resolve_creator_emails(self, runtime_ids: list[str]) -> dict[str, str]:
    """Looks up resource creator emails from Cloud Audit Logs in `ds_ge_audit_raw`."""
    default_owner = os.environ.get('FINOPS_DEFAULT_OWNER_EMAIL', '').strip()
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
            email = str(row.get('principal_email') or '').strip()
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

  def _resolve_gmail_access_token(self) -> str | None:
    """Resolves a bearer token for the Gmail REST API (direct token, OAuth refresh token, or ADC)."""
    explicit_token = os.environ.get('FINOPS_GMAIL_ACCESS_TOKEN', '').strip()
    if explicit_token:
      return explicit_token

    refresh_token = os.environ.get('FINOPS_GMAIL_REFRESH_TOKEN', '').strip()
    client_id = os.environ.get('FINOPS_GMAIL_CLIENT_ID', '').strip()
    client_secret = os.environ.get('FINOPS_GMAIL_CLIENT_SECRET', '').strip()
    if refresh_token and client_id and client_secret:
      form_data = urllib.parse.urlencode({
          'client_id': client_id,
          'client_secret': client_secret,
          'refresh_token': refresh_token,
          'grant_type': 'refresh_token',
      }).encode('utf-8')
      req = urllib.request.Request(
          'https://oauth2.googleapis.com/token',
          data=form_data,
          headers={'Content-Type': 'application/x-www-form-urlencoded'},
          method='POST',
      )
      with urllib.request.urlopen(req, timeout=8.0) as resp:
        token_resp = json.loads(resp.read().decode('utf-8'))
        tok = str(token_resp.get('access_token') or '').strip()
        if tok:
          return tok

    gmail_enabled = os.environ.get('FINOPS_GMAIL_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if gmail_enabled and self._token_fn is not None:
      return self._token_fn()
    return None

  def _send_via_gmail_api(
      self,
      access_token: str,
      sender_user: str,
      from_email: str,
      recipient: str,
      subject: str,
      text_body: str,
      html_body: str,
  ) -> dict[str, Any]:
    """Sends a multipart RFC 2822 email via the Gmail REST API (`users.messages.send`)."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype='html')
    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
    safe_user = urllib.parse.quote(sender_user or 'me', safe='@._-')
    url = f'https://gmail.googleapis.com/gmail/v1/users/{safe_user}/messages/send'
    payload_bytes = json.dumps({'raw': raw_b64}).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
      body = resp.read().decode('utf-8', errors='replace')
      parsed = json.loads(body) if body.strip().startswith('{') else {}
      return {
          'delivered': 200 <= resp.status < 300,
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
    gmail_sender = os.environ.get('FINOPS_GMAIL_SENDER', 'me').strip() or 'me'
    from_email = os.environ.get(
        'FINOPS_ALERT_FROM_EMAIL',
        gmail_sender if '@' in gmail_sender else 'finops-governance@googlecloud.internal',
    ).strip()

    delivery_channel = 'DRY_RUN_PREVIEW'
    delivered = False
    delivery_error: str | None = None
    gmail_message_id: str | None = None

    gmail_configured = bool(
        os.environ.get('FINOPS_GMAIL_ACCESS_TOKEN', '').strip()
        or os.environ.get('FINOPS_GMAIL_REFRESH_TOKEN', '').strip()
        or os.environ.get('FINOPS_GMAIL_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on')
    )

    if not dry_run and recipient and '@' in recipient and gmail_configured:
      delivery_channel = 'GMAIL_API'
      try:
        access_token = self._resolve_gmail_access_token()
        if not access_token:
          raise RuntimeError('Gmail API enabled but no OAuth/ADC access token could be resolved.')
        gmail_res = self._send_via_gmail_api(
            access_token=access_token,
            sender_user=gmail_sender,
            from_email=from_email,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        delivered = bool(gmail_res.get('delivered'))
        gmail_message_id = str(gmail_res.get('message_id') or '') or None
      except Exception as exc:
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

  def evaluate_and_warn_idle_runtimes(
      self,
      fleet_payload: Mapping[str, Any] | None,
      *,
      grace_period_hours: int = DEFAULT_GRACE_PERIOD_HOURS,
      idle_days_threshold: int = DEFAULT_IDLE_DAYS_THRESHOLD,
      token_budget_usd_threshold: float = DEFAULT_TOKEN_BUDGET_USD,
      send_warnings: bool = True,
      dry_run_email: bool = False,
      lookup_bq: bool = True,
      persist_to_bq: bool = True,
  ) -> dict[str, Any]:
    """Stage 1: Evaluates fleet for zombie/always-on/over-budget runtimes and issues clearance warnings."""
    if lookup_bq:
      self._load_queue_from_bq_if_needed()
    now = _utcnow()
    now_iso = _iso(now) or ''
    grace_hours = max(1, min(int(grace_period_hours or DEFAULT_GRACE_PERIOD_HOURS), 720))
    fleet = fleet_payload if isinstance(fleet_payload, Mapping) else {}
    unregistered = list(fleet.get('unregistered_runtimes') or [])
    registered = list(fleet.get('agents') or [])

    candidates: list[dict[str, Any]] = []
    active_ids: set[str] = set()

    for rt in unregistered + registered:
      if not isinstance(rt, Mapping):
        continue
      rid = str(rt.get('agent_id') or '').strip()
      if not rid:
        continue
      fstat = str(rt.get('finops_status') or '')
      metrics = rt.get('metrics') if isinstance(rt.get('metrics'), Mapping) else {}
      backend = rt.get('backend') if isinstance(rt.get('backend'), Mapping) else {}
      req_cnt = int(metrics.get('requests') or 0)
      llm_cnt = int(metrics.get('llm_calls') or 0)
      est_cost = float(metrics.get('est_token_cost_usd') or 0.0)
      kind = str(backend.get('kind') or '')

      is_zombie = fstat in ('ZOMBIE_IDLE_ENGINE', 'ZOMBIE_IDLE_SERVICE', 'ALWAYS_ON_UNREGISTERED')
      is_over_budget = token_budget_usd_threshold > 0 and est_cost >= token_budget_usd_threshold

      if is_zombie or is_over_budget:
        candidates.append({
            'runtime_id': rid,
            'resource_name': str(rt.get('resource_name') or ''),
            'runtime_kind': kind or ('agent_engine' if rid.startswith('re-') else 'cloud_run'),
            'display_name': str(rt.get('display_name') or rid),
            'project_id': str(backend.get('project') or self.project_id),
            'location': str(rt.get('location') or backend.get('location') or backend.get('region') or 'us-central1'),
            'finops_status': 'BUDGET_EXCEEDED' if (is_over_budget and not is_zombie) else fstat,
            'recommended_action': (
                'delete_reasoning_engine'
                if kind == 'agent_engine' and is_zombie
                else ('scale_to_zero' if kind == 'cloud_run' else 'review_budget')
            ),
            'action_command': str(rt.get('finops_action_command') or ''),
            'vcpu_hours': float(metrics.get('vcpu_hours') or 0.0),
            'memory_gib_hours': float(metrics.get('memory_gib_hours') or 0.0),
            'billable_instance_hours': float(metrics.get('billable_instance_hours') or 0.0),
            'est_token_cost_usd': est_cost,
        })
      elif req_cnt > 0 or llm_cnt > 0:
        active_ids.add(rid)

    creator_map = self.resolve_creator_emails([c['runtime_id'] for c in candidates]) if lookup_bq else {}
    updated_items: list[dict[str, Any]] = []
    warnings_sent: list[dict[str, Any]] = []

    with self._lock:
      # Mark any previously queued runtime that now has traffic as RECOVERED_ACTIVE
      for rid in active_ids:
        existing = self._queue.get(rid)
        if existing and existing.get('status') in ('PENDING_WARNING', 'WARNED_GRACE_PERIOD'):
          existing['status'] = 'RECOVERED_ACTIVE'
          existing['updated_at'] = now_iso
          updated_items.append(dict(existing))

      for cand in candidates:
        rid = cand['runtime_id']
        existing = self._queue.get(rid)
        first_idle = (existing or {}).get('first_flagged_idle_at') or now_iso
        snoozed_until_str = (existing or {}).get('snoozed_until')
        snoozed_dt = _parse_iso(snoozed_until_str)
        is_snoozed = snoozed_dt is not None and snoozed_dt > now

        creator_email = (
            (existing or {}).get('creator_email')
            or creator_map.get(rid)
            or creator_map.get(cand['resource_name'])
            or os.environ.get('FINOPS_DEFAULT_OWNER_EMAIL', 'finops-owners@example.com')
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
            f"{self._base_url()}/api/governance/snooze?"
            f"runtime_id={urllib.parse.quote(rid)}&days=7&token={token}"
        )

        if is_snoozed:
          status = 'SNOOZED'
          warn_sent_at = (existing or {}).get('warning_sent_at')
          sched_at = snoozed_until_str
        elif existing and existing.get('status') in ('CLEARED_ENFORCED', 'CLEARED_DRY_RUN'):
          status = str(existing['status'])
          warn_sent_at = existing.get('warning_sent_at')
          sched_at = existing.get('scheduled_clearance_at')
        else:
          warn_sent_at = (existing or {}).get('warning_sent_at')
          sched_at = (existing or {}).get('scheduled_clearance_at')
          if send_warnings and not warn_sent_at:
            warn_sent_at = now_iso
            sched_at = _iso(now + datetime.timedelta(hours=grace_hours))
            status = 'WARNED_GRACE_PERIOD'
          elif warn_sent_at:
            status = 'WARNED_GRACE_PERIOD'
          else:
            status = 'PENDING_WARNING'

        item = {
            **cand,
            'creator_email': creator_email,
            'creator_email_masked': mask_email_for_ui(creator_email),
            'first_flagged_idle_at': first_idle,
            'warning_sent_at': warn_sent_at,
            'scheduled_clearance_at': sched_at,
            'snoozed_until': snoozed_until_str,
            'status': status,
            'est_monthly_waste_usd': est_waste,
            'snooze_7d_url': snooze_url,
            'snooze_token': token,
            'idle_days_threshold': idle_days_threshold,
            'updated_at': now_iso,
        }
        was_already_warned = bool(existing and existing.get('warning_sent_at'))
        self._queue[rid] = item
        updated_items.append(dict(item))

        if send_warnings and not is_snoozed and not was_already_warned and status == 'WARNED_GRACE_PERIOD':
          subj = (
              f"[FinOps Scheduled Clearance Warning] {item['display_name']} ({item['runtime_id']}) "
              f"scheduled for {item['recommended_action']} in {grace_hours}h"
          )
          text_body = (
              f"Google Cloud FinOps Automated Cost Governance Alert\n"
              f"====================================================\n"
              f"Runtime: {item['display_name']} ({item['runtime_id']})\n"
              f"Status: {item['finops_status']}\n"
              f"Project / Location: {item['project_id']} / {item['location']}\n"
              f"Attributed Creator: {creator_email}\n"
              f"First Flagged Idle: {item['first_flagged_idle_at']}\n"
              f"Scheduled Clearance At: {item['scheduled_clearance_at']} ({grace_hours}h grace period)\n"
              f"Planned Action: {item['recommended_action']}\n"
              f"Command: {item['action_command']}\n\n"
              f"To keep this runtime alive and snooze clearance for 7 days, click or POST:\n"
              f"  {snooze_url}\n"
          )
          safe_disp = html.escape(item['display_name'])
          safe_rid = html.escape(item['runtime_id'])
          safe_fstat = html.escape(item['finops_status'])
          safe_sched = html.escape(str(item['scheduled_clearance_at'] or ''))
          safe_cmd = html.escape(item['action_command'])
          safe_url = html.escape(snooze_url)
          html_body = (
              f"<div style='font-family:Inter,sans-serif;max-width:640px;padding:16px;border:1px solid #e2e8f0;border-radius:8px;'>"
              f"<h3 style='margin-top:0;color:#b45309;'>Scheduled Clearance Warning: {safe_disp}</h3>"
              f"<p>Runtime <code>{safe_rid}</code> was flagged as <strong>{safe_fstat}</strong> with 0 requests and 0 LLM calls.</p>"
              f"<ul>"
              f"<li><strong>Scheduled Clearance:</strong> {safe_sched} ({grace_hours}h grace window)</li>"
              f"<li><strong>Planned Action:</strong> <code>{safe_cmd}</code></li>"
              f"</ul>"
              f"<p><a href='{safe_url}' style='display:inline-block;padding:8px 14px;background:#1a73e8;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;'>Keep Alive / Snooze 7 Days</a></p>"
              f"</div>"
          )
          notif = self._deliver_email_notification(
              recipient=creator_email,
              subject=subj,
              text_body=text_body,
              html_body=html_body,
              metadata={'runtime_id': rid, 'scheduled_clearance_at': sched_at, 'snooze_7d_url': snooze_url},
              dry_run=dry_run_email,
          )
          warnings_sent.append(notif)

    if persist_to_bq and updated_items:
      self._persist_queue_items_to_bq(updated_items)

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
    if lookup_bq:
      self._load_queue_from_bq_if_needed()
    rid = str(runtime_id or runtime_key or '').strip()
    if not rid or not _SAFE_ID_RE.match(rid):
      return {'status': 'INVALID_REQUEST', 'error': 'Invalid runtime_id.'}
    eff_days = snooze_days if snooze_days is not None else days
    clean_days = max(1, min(int(eff_days or 7), 90))
    if verify_token and not verify_snooze_token(rid, clean_days, str(token or '')):
      return {'status': 'FORBIDDEN', 'error': 'Invalid or expired snooze token.'}

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
    """Stage 2: Executes (or dry-runs) scale-to-zero / teardown for runtimes past their grace period."""
    del fleet_payload
    if lookup_bq:
      self._load_queue_from_bq_if_needed()
    if dry_run is not None:
      clean_mode = 'dry_run' if dry_run else 'enforced'
    else:
      clean_mode = str(mode or 'dry_run').strip().lower()
    if clean_mode not in ('dry_run', 'enforced'):
      clean_mode = 'dry_run'
    enforced_allowed = os.environ.get('FINOPS_ALLOW_AUTOMATED_CLEARANCE', '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    )
    effective_enforced = clean_mode == 'enforced' and enforced_allowed
    now = _utcnow()
    now_iso = _iso(now) or ''

    actions_executed: list[dict[str, Any]] = []
    updated_items: list[dict[str, Any]] = []

    with self._lock:
      items = list(self._queue.values())

    for item in items:
      rid = str(item.get('runtime_id') or '')
      if target_runtime_id and rid != target_runtime_id:
        continue
      status = str(item.get('status') or '')
      if status not in ('WARNED_GRACE_PERIOD', 'PENDING_WARNING'):
        continue
      snoozed_dt = _parse_iso(item.get('snoozed_until'))
      if snoozed_dt is not None and snoozed_dt > now:
        continue
      sched_dt = _parse_iso(item.get('scheduled_clearance_at'))
      is_due = force_due or (sched_dt is not None and now >= sched_dt)
      if not is_due:
        continue

      kind = str(item.get('runtime_kind') or '')
      proj = str(item.get('project_id') or self.project_id)
      loc = str(item.get('location') or 'us-central1')
      res_name = str(item.get('resource_name') or '')
      rec_action = str(item.get('recommended_action') or 'scale_to_zero')

      api_result = 'DRY_RUN_ONLY'
      api_error: str | None = None
      if effective_enforced and self._api_call_fn is not None:
        try:
          if kind == 'cloud_run':
            svc_name = rid[4:] if rid.startswith('run-') else rid
            url = (
                f'https://run.googleapis.com/v2/projects/{proj}/locations/{loc}/services/{svc_name}'
                '?updateMask=scaling.minInstanceCount'
            )
            self._api_call_fn('PATCH', url, {'scaling': {'minInstanceCount': 0}})
            api_result = 'SCALED_TO_ZERO'
          elif kind == 'agent_engine' and res_name.startswith('projects/'):
            url = f'https://{loc}-aiplatform.googleapis.com/v1/{res_name}'
            self._api_call_fn('DELETE', url, None)
            api_result = 'DELETED_REASONING_ENGINE'
          else:
            api_result = 'SKIPPED_UNSUPPORTED_KIND'
        except Exception as exc:
          api_error = f'{type(exc).__name__}: {exc}'
          api_result = 'API_ERROR'

      new_status = 'CLEARED_ENFORCED' if (effective_enforced and not api_error) else 'CLEARED_DRY_RUN'
      with self._lock:
        item['status'] = new_status
        item['updated_at'] = now_iso
        self._queue[rid] = item
        updated_items.append(dict(item))

      action_entry = {
          'timestamp': now_iso,
          'runtime_id': rid,
          'display_name': item.get('display_name'),
          'runtime_kind': kind,
          'recommended_action': rec_action,
          'action_command': item.get('action_command'),
          'mode_requested': clean_mode,
          'mode_executed': 'enforced' if effective_enforced else 'dry_run',
          'result': api_result,
          'status': new_status,
          'error': api_error,
      }
      actions_executed.append(action_entry)
      with self._lock:
        self._clearance_history.insert(0, action_entry)
        del self._clearance_history[50:]

    if persist_to_bq and updated_items:
      self._persist_queue_items_to_bq(updated_items)

    note = None
    if clean_mode == 'enforced' and not enforced_allowed:
      note = (
          'Enforced clearance requested, but FINOPS_ALLOW_AUTOMATED_CLEARANCE=1 is not set on this '
          'service; executed in safe dry_run mode instead.'
      )

    return {
        'status': 'OK',
        'mode_requested': clean_mode,
        'mode_executed': 'enforced' if effective_enforced else 'dry_run',
        'cleared_count': len(actions_executed),
        'actions': actions_executed,
        'note': note,
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
    total_waste = round(
        sum(float(i.get('est_monthly_waste_usd') or 0.0) for i in items if i.get('status') in ('PENDING_WARNING', 'WARNED_GRACE_PERIOD')),
        2,
    )
    return {
        'status': 'OK',
        'project_id': self.project_id,
        'table': f'{self.project_id}.{self.analytics_dataset}.{CLEARANCE_TABLE}',
        'summary': {
            'total_tracked': len(items),
            'pending_clearance_count': pending_cnt,
            'snoozed_count': snoozed_cnt,
            'cleared_count': cleared_cnt,
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
      self._load_queue_from_bq_if_needed()
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
