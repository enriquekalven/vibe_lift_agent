"""Google Cloud Project Telemetry Service for VibeLift Analytics Platform.

Fetches live telemetry, token cache economics, and agent interaction logs
from the hosting Google Cloud Project (Cloud Run, Cloud Logging, Cloud Monitoring,
and Gemini Enterprise / BigQuery Agent Analytics).
"""

import datetime
import hashlib
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any

try:
  from google.cloud import logging as gcp_logging
except ImportError:
  gcp_logging = None  # type: ignore[assignment]

try:
  from google.cloud import bigquery as gcp_bigquery
except ImportError:
  gcp_bigquery = None  # type: ignore[assignment]

try:
  import google.auth as google_auth
  from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
  google_auth = None  # type: ignore[assignment]
  GoogleAuthRequest = None  # type: ignore[assignment,misc]

from vibelift import ge_mart, telemetry

logger = logging.getLogger(__name__)

# VibeLift's own Cloud Run service name (Cloud Run injects K_SERVICE at runtime).
DEFAULT_SERVICE_NAME = 'vibe-lift-agent'

# Returned when no project can be discovered. Uppercase is invalid in project IDs, so this can
# never resolve to a real project.
UNCONFIGURED_PROJECT_ID = 'UNCONFIGURED-PROJECT'


def get_monitored_service_names() -> list[str]:
  """Returns the Cloud Run services VibeLift reports on (explicit opt-in allowlist).

  VibeLift never enumerates unrelated workloads in the hosting project. The list
  always contains VibeLift's own service, plus any services the operator opts in
  via the comma-separated VIBELIFT_MONITORED_SERVICES environment variable.
  """
  names = [os.environ.get('K_SERVICE') or DEFAULT_SERVICE_NAME]
  for raw in os.environ.get('VIBELIFT_MONITORED_SERVICES', '').split(','):
    name = raw.strip()
    if name and name not in names:
      names.append(name)
  return names


def get_current_gcp_project() -> str:
  """Resolves the current Google Cloud Project ID with multi-level discovery."""
  # 1. Environment variable (standard on Cloud Run / Cloud Functions)
  proj = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCP_PROJECT')
  if proj:
    return proj

  # 2. Compute Engine / Cloud Run Metadata Server
  try:
    req = urllib.request.Request(
        'http://metadata.google.internal/computeMetadata/v1/project/project-id',
        headers={'Metadata-Flavor': 'Google'},
    )
    with urllib.request.urlopen(req, timeout=1.0) as resp:
      if resp.status == 200:
        return resp.read().decode('utf-8').strip()
  except (urllib.error.URLError, TimeoutError, OSError):
    pass

  # 3. Local gcloud CLI active configuration
  try:
    proc = subprocess.run(
        ['gcloud', 'config', 'get-value', 'project'],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    out = proc.stdout.strip()
    if out and not out.startswith('('):
      return out
  except Exception:
    pass

  # 4. Nothing configured. Deliberately NOT a real project: uppercase makes it an invalid
  # project ID, so API calls fail fast with a clear error instead of querying someone else's project.
  logger.warning(
      'No Google Cloud project found (GOOGLE_CLOUD_PROJECT, metadata server, gcloud); using %s',
      UNCONFIGURED_PROJECT_ID,
  )
  return UNCONFIGURED_PROJECT_ID


def get_current_gcp_region() -> str:
  """Resolves the current Google Cloud Region."""
  region = os.environ.get('GOOGLE_CLOUD_REGION') or os.environ.get('REGION')
  if region:
    return region

  # Metadata server zone -> region
  try:
    req = urllib.request.Request(
        'http://metadata.google.internal/computeMetadata/v1/instance/zone',
        headers={'Metadata-Flavor': 'Google'},
    )
    with urllib.request.urlopen(req, timeout=1.0) as resp:
      if resp.status == 200:
        zone = resp.read().decode('utf-8').strip().split('/')[-1]
        return '-'.join(zone.split('-')[:2])
  except Exception:
    pass

  return 'us-central1'


class GcsReadError(Exception):
  """A Cloud Storage object could not be read; ``status`` says why."""

  def __init__(self, status: str, message: str):
    super().__init__(message)
    self.status = status


def _gcs_error_reason(exc: Any) -> str:
  """Best-effort 'Cloud Storage says: ...' from an HTTPError JSON body (bounded, never raises)."""
  try:
    body = json.loads((exc.read(4096) or b'{}').decode('utf-8', errors='replace'))
    msg = str(((body or {}).get('error') or {}).get('message') or '').strip()
  except Exception:
    return ''
  return f'Cloud Storage says: {msg[:300]}' if msg else ''


class GoogleCloudTelemetryService:
  """Service for aggregating live GCP telemetry from Cloud Logging and BigQuery."""

  def __init__(self, project_id: str | None = None, region: str | None = None):
    self.project_id = project_id or get_current_gcp_project()
    self.region = region or get_current_gcp_region()
    self._logging_client = None
    self._bigquery_client = None
    self._credentials: Any = None
    self._cached_services: list[dict[str, Any]] | None = None
    self._cached_support_events: list[dict[str, Any]] | None = None
    self._cached_live_turns: list[telemetry.TurnUsageLog] | None = None
    self._cached_bq_summary: dict[str, Any] | None = None
    self._cached_bq_summary_ts: float = 0.0
    self._cached_bq_summary_hours: int = 168
    self._cached_bq_insights: dict[str, Any] | None = None
    self._cached_bq_insights_ts: float = 0.0
    self._cli_token: str | None = None
    self._cli_token_ts: float = 0.0

  @property
  def logging_client(self):
    """Lazy initializer for Google Cloud Logging client."""
    if self._logging_client is None and gcp_logging is not None:
      try:
        self._logging_client = gcp_logging.Client(project=self.project_id)
      except Exception as exc:
        logger.warning('Failed to initialize GCP Logging Client: %s', exc)
    return self._logging_client

  @property
  def bigquery_client(self):
    """Lazy initializer for Google Cloud BigQuery client."""
    if self._bigquery_client is None and gcp_bigquery is not None:
      try:
        self._bigquery_client = gcp_bigquery.Client(project=self.project_id)
      except Exception as exc:
        logger.warning('Failed to initialize GCP BigQuery Client: %s', exc)
    return self._bigquery_client

  def _get_access_token(self) -> str | None:
    """Returns an OAuth2 access token from ADC (Cloud Run) or gcloud CLI fallback."""
    import time

    now_mono = time.monotonic()
    if self._cli_token and (now_mono - self._cli_token_ts) < 300.0:
      return self._cli_token

    if google_auth is not None and GoogleAuthRequest is not None:
      try:
        if self._credentials is None:
          self._credentials, _ = google_auth.default(
              scopes=['https://www.googleapis.com/auth/cloud-platform']
          )
        if not self._credentials.valid:
          self._credentials.refresh(GoogleAuthRequest())
        if self._credentials.token:
          return self._credentials.token
      except Exception as exc:
        logger.debug('ADC token refresh unavailable, trying gcloud CLI fallback: %s', exc)

    if self.project_id and self.project_id not in (UNCONFIGURED_PROJECT_ID, 'test-project'):
      try:
        res = subprocess.run(
            ['gcloud', 'auth', 'print-access-token'],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
          self._cli_token = res.stdout.strip()
          self._cli_token_ts = now_mono
          return self._cli_token
      except Exception as exc:
        logger.debug('gcloud CLI token fallback failed: %s', exc)
    return None

  def _query_bigquery_rest(
      self,
      sql: str,
      timeout_s: float = 8.0,
  ) -> list[dict[str, Any]]:
    """Executes a read-only BigQuery SQL query via the BigQuery REST API using ADC."""
    if not self.project_id or self.project_id in (UNCONFIGURED_PROJECT_ID, 'test-project'):
      return []
    token = self._get_access_token()
    if not token:
      return []
    url = f'https://bigquery.googleapis.com/bigquery/v2/projects/{self.project_id}/queries'
    payload: dict[str, Any] = {
        'query': sql,
        'useLegacySql': False,
        'timeoutMs': int(timeout_s * 1000),
        'labels': {'datacloud': 'jetski'},
    }
    if not sql.lstrip().upper().startswith(('CREATE ', 'DROP ', 'ALTER ')):
      max_bytes = os.environ.get('VIBELIFT_BQ_MAX_BYTES_BILLED', str(10 * 1024 * 1024 * 1024)).strip()
      if max_bytes.isdigit() and int(max_bytes) > 0:
        payload['maximumBytesBilled'] = max_bytes
    try:
      req = urllib.request.Request(
          url,
          data=json.dumps(payload).encode('utf-8'),
          headers={
              'Authorization': f'Bearer {token}',
              'Content-Type': 'application/json',
              'x-goog-user-project': self.project_id,
          },
          method='POST',
      )
      with urllib.request.urlopen(req, timeout=timeout_s + 1.0) as resp:
        res = json.loads(resp.read().decode('utf-8'))
      if not res or res.get('jobComplete') is False or 'rows' not in res:
        if res and res.get('jobComplete') is False:
          logger.debug('BigQuery REST query timed out before jobComplete (%s)', sql[:60])
        return []
      fields = [f.get('name', f'col_{i}') for i, f in enumerate(res.get('schema', {}).get('fields', []))]
      out: list[dict[str, Any]] = []
      for row in res.get('rows', []):
        vals = [cell.get('v') for cell in row.get('f', [])]
        out.append(dict(zip(fields, vals, strict=False)))
      return out
    except Exception as exc:
      logger.debug('BigQuery REST query skipped (%s): %s', sql[:60], exc)
      return []

  def list_cloud_run_agent_services(
      self,
      force_refresh: bool = False,
      non_blocking: bool = False,
  ) -> list[dict[str, Any]]:
    """Describes the allowlisted Cloud Run services via the Cloud Run Admin API.

    Only services returned by get_monitored_service_names() are reported. When the
    Admin API is unreachable the entry is returned with status UNKNOWN and an empty
    URL rather than a guessed endpoint.
    """
    if self._cached_services is not None and not force_refresh:
      return self._cached_services

    if non_blocking and not force_refresh:
      import threading
      evt = threading.Event()
      def _bg() -> None:
        try:
          self.list_cloud_run_agent_services(force_refresh=True, non_blocking=False)
        finally:
          evt.set()
      threading.Thread(target=_bg, daemon=True).start()
      evt.wait(timeout=0.05)
      if self._cached_services is not None:
        return self._cached_services
      pub_url = os.environ.get('VIBELIFT_PUBLIC_URL', '').strip().rstrip('/')
      return [
          {
              'service_name': name,
              'url': pub_url if name == (os.environ.get('K_SERVICE') or DEFAULT_SERVICE_NAME) else '',
              'region': self.region,
              'status': 'READY' if pub_url else 'UNKNOWN',
              'active_revision': f'{name}-live' if pub_url else '',
              'min_instances': 1,
              'max_instances': 10,
              'concurrency': 80,
              'cpu_utilization_pct': None,
              'memory_utilization_pct': None,
              'p95_latency_ms': None,
              'cold_starts_1h': None,
              'monthly_cost_usd': None,
          }
          for name in get_monitored_service_names()
      ]

    token = self._get_access_token()
    discovered: list[dict[str, Any]] = []
    for name in get_monitored_service_names():
      entry: dict[str, Any] = {
          'service_name': name,
          'url': '',
          'region': self.region,
          'status': 'UNKNOWN',
          'active_revision': '',
          'min_instances': 1,
          'max_instances': 10,
          'concurrency': 80,
          'cpu_utilization_pct': None,
          'memory_utilization_pct': None,
          'p95_latency_ms': None,
          'cold_starts_1h': None,
          'monthly_cost_usd': None,
      }
      if token and self.project_id not in (UNCONFIGURED_PROJECT_ID, 'test-project'):
        api_url = (
            f'https://run.googleapis.com/v2/projects/{self.project_id}'
            f'/locations/{self.region}/services/{name}'
        )
        try:
          req = urllib.request.Request(
              api_url,
              headers={
                  'Authorization': f'Bearer {token}',
                  'x-goog-user-project': self.project_id,
              },
          )
          with urllib.request.urlopen(req, timeout=3.0) as resp:
            svc = json.loads(resp.read().decode('utf-8'))
          entry['url'] = svc.get('uri', '')
          entry['active_revision'] = str(svc.get('latestReadyRevision', '')).split('/')[-1]
          tmpl = svc.get('template') or {}
          scaling = tmpl.get('scaling') or {}
          entry['min_instances'] = int(scaling.get('minInstanceCount') or 1)
          entry['max_instances'] = int(scaling.get('maxInstanceCount') or 10)
          entry['concurrency'] = int(tmpl.get('maxInstanceRequestConcurrency') or 80)
          state = (svc.get('terminalCondition') or {}).get('state', '')
          entry['status'] = 'READY' if state == 'CONDITION_SUCCEEDED' else (state or 'ACTIVE')
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
          logger.debug('Cloud Run Admin API describe failed for %s: %s', name, exc)
      discovered.append(entry)

    # Enrich with real request & latency metrics from BigQuery <analytics_ds>.run_googleapis_com_requests_*
    analytics_ds = (os.environ.get('VIBELIFT_ANALYTICS_DATASET') or 'vibelift_analytics').strip() or 'vibelift_analytics'
    bq_rev_rows = self._query_bigquery_rest(f"""
      SELECT
        resource.labels.service_name AS service_name,
        ARRAY_AGG(resource.labels.revision_name ORDER BY timestamp DESC LIMIT 1)[OFFSET(0)] AS latest_rev,
        COUNT(*) AS req_count,
        ROUND(AVG(httpRequest.latency * 1000), 1) AS avg_latency_ms,
        ROUND(APPROX_QUANTILES(httpRequest.latency * 1000, 100)[OFFSET(95)], 1) AS p95_latency_ms
      FROM `{self.project_id}.{analytics_ds}.run_googleapis_com_requests_*`
      WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY))
        AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
      GROUP BY 1
    """, timeout_s=5.0)
    rev_by_svc = {str(r.get('service_name')): r for r in bq_rev_rows}
    for d in discovered:
      sname = str(d.get('service_name'))
      if sname in rev_by_svc:
        rinfo = rev_by_svc[sname]
        if not d.get('active_revision') and rinfo.get('latest_rev'):
          d['active_revision'] = str(rinfo.get('latest_rev'))
        lat = rinfo.get('p95_latency_ms') if rinfo.get('p95_latency_ms') is not None else rinfo.get('avg_latency_ms')
        d['p95_latency_ms'] = float(lat) if lat is not None else None
        d['requests_observed'] = int(rinfo.get('req_count') or 0)
        # CPU utilization, memory utilization, and per-service monthly cost are not present
        # in Cloud Run request logs; keep them None rather than fabricating numbers.

    self._cached_services = discovered
    return discovered

  def fetch_gemini_enterprise_support_telemetry(
      self,
      limit: int = 15,
      force_refresh: bool = False,
      non_blocking: bool = False,
  ) -> list[dict[str, Any]]:
    """Fetches real Gemini Enterprise audit & user activity events from BigQuery."""
    if self._cached_support_events is not None and not force_refresh:
      return self._cached_support_events

    if non_blocking and not force_refresh:
      import threading
      evt = threading.Event()
      def _bg() -> None:
        try:
          self.fetch_gemini_enterprise_support_telemetry(limit=limit, force_refresh=True, non_blocking=False)
        finally:
          evt.set()
      threading.Thread(target=_bg, daemon=True).start()
      evt.wait(timeout=0.05)
      return self._cached_support_events if self._cached_support_events is not None else []

    # Turns from VibeLift's GE mart (deploy/bigquery/provision_ge_mart.py), actionable issues
    # first. If the mart is not provisioned the query fails and the panel stays empty.
    try:
      sql = ge_mart.build_support_turns_sql(self.project_id, hours=168, limit=limit)
    except ValueError:
      return []
    rows = self._query_bigquery_rest(sql, timeout_s=6.0)
    events = [ge_mart.support_event_from_row(r, self.project_id) for r in rows]
    self._cached_support_events = events
    return events

  def fetch_bigquery_fleet_summary(
      self,
      hours_ago: int = 168,
      force_refresh: bool = False,
  ) -> dict[str, Any] | None:
    """Per agent/model usage from vibelift_mart.agg_daily_usage (tokens None when not logged).

    Spend is not modelled here: billed cost comes only from the Cloud Billing export.
    """
    import time

    eff_hours = max(1, int(hours_ago))
    now_mono = time.monotonic()
    if (
        self._cached_bq_summary is not None
        and not force_refresh
        and getattr(self, '_cached_bq_summary_hours', 168) == eff_hours
        and (now_mono - getattr(self, '_cached_bq_summary_ts', 0.0)) < 60.0
    ):
      return self._cached_bq_summary

    try:
      sql = ge_mart.build_daily_usage_sql(self.project_id, days=max(1, eff_hours // 24))
    except ValueError:
      return None
    rows = [ge_mart.daily_usage_from_row(r) for r in self._query_bigquery_rest(sql, timeout_s=6.0)]
    if not rows:
      return None
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for r in rows:
      g = groups.setdefault((r.get('agent_name'), r.get('model_name')), {
          'agent_id': r.get('agent_name'),
          'model': r.get('model_name'),
          'total_turns': 0,
          'turns_with_tokens': 0,
          'prompt_tokens': None,
          'cached_tokens': None,
          'output_tokens': None,
      })
      g['total_turns'] = int(g['total_turns']) + int(r.get('interactions') or 0)  # type: ignore[arg-type]
      g['turns_with_tokens'] = int(g['turns_with_tokens']) + int(r.get('turns_with_tokens') or 0)  # type: ignore[arg-type]
      for src, dst in (('input_tokens', 'prompt_tokens'), ('cached_input_tokens', 'cached_tokens'),
                       ('output_tokens', 'output_tokens')):
        if r.get(src) is not None:
          g[dst] = int(g[dst] or 0) + int(r[src])  # type: ignore[arg-type]
    breakdown = []
    for g in groups.values():
      prompt, cached = g['prompt_tokens'], g['cached_tokens']
      g['cache_hit_ratio'] = round(cached / prompt * 100.0, 2) if prompt and cached is not None else None  # type: ignore[operator]
      g['naive_spend_usd'] = None
      g['actual_spend_usd'] = None
      g['saved_usd'] = None
      g['avg_latency_ms'] = None
      breakdown.append(g)
    breakdown.sort(key=lambda g: -int(g['total_turns']))  # type: ignore[arg-type]
    prompts = [g['prompt_tokens'] for g in breakdown if g['prompt_tokens'] is not None]
    cached_vals = [g['cached_tokens'] for g in breakdown if g['cached_tokens'] is not None]
    total_prompts = sum(prompts) if prompts else None  # type: ignore[arg-type]
    total_cached = sum(cached_vals) if cached_vals else None  # type: ignore[arg-type]
    summary = {
        'source': f'BigQuery ({ge_mart.mart_ref(self.project_id)}.agg_daily_usage)',
        'fleet_breakdown': breakdown,
        'total_turns': sum(int(g['total_turns']) for g in breakdown),  # type: ignore[arg-type]
        'total_prompt_tokens': total_prompts,
        'total_cached_tokens': total_cached,
        'aggregate_cache_hit_ratio': (
            round(total_cached / total_prompts * 100.0, 2) if total_prompts and total_cached is not None else None
        ),
        'total_naive_spend_usd': None,
        'total_actual_spend_usd': None,
        'total_finops_savings_usd': None,
    }
    self._cached_bq_summary = summary
    self._cached_bq_summary_ts = now_mono
    self._cached_bq_summary_hours = eff_hours
    return summary

  def fetch_live_cloud_turns(
      self,
      hours_ago: int = 48,
      max_results: int = 30,
      force_refresh: bool = False,
  ) -> list[telemetry.TurnUsageLog]:
    """Queries BigQuery or Cloud Logging for agent turns and prompt cache usage."""
    if self._cached_live_turns is not None and not force_refresh:
      return self._cached_live_turns

    turns: list[telemetry.TurnUsageLog] = []

    # 1. VibeLift GE mart: turns whose inference logs reported token usage. Only rows with both
    # input and output tokens are used; cached tokens absent from the log count as no cache read.
    try:
      mart_sql = ge_mart.build_recent_turns_sql(
          self.project_id, hours=hours_ago, limit=max_results, tokens_only=True)
    except ValueError:
      mart_sql = ''
    mart_rows = self._query_bigquery_rest(mart_sql, timeout_s=6.0) if mart_sql else []
    for idx, r in enumerate(mart_rows):
      in_tok = ge_mart.int_or_none(r.get('input_tokens'))
      out_tok = ge_mart.int_or_none(r.get('output_tokens'))
      if in_tok is None or out_tok is None:
        continue
      cached_tok = ge_mart.int_or_none(r.get('cached_input_tokens')) or 0
      turn_id = str(r.get('turn_id') or f'turn-{idx + 1}')
      turns.append(
          telemetry.TurnUsageLog(
              timestamp=str(r.get('ts') or ''),
              agent_name=str(r.get('agent_name') or r.get('engine_key') or 'unknown agent'),
              model=str(r.get('model_name') or 'unknown'),
              turn_index=idx + 1,
              prompt_prefix_hash=hashlib.sha256(turn_id.encode('utf-8')).hexdigest()[:10],
              cache_breakpoint_line=None,
              cache_breakpoint_reason=f"{r.get('turn_source')} turn ({ge_mart.mart_ref(self.project_id)}.fct_turns)",
              prompt_token_count=in_tok,
              cached_content_token_count=cached_tok,
              cache_creation_input_tokens=0,
              uncached_input_tokens=max(0, in_tok - cached_tok),
              candidates_token_count=out_tok,
              thoughts_token_count=ge_mart.int_or_none(r.get('reasoning_tokens')) or 0,
              status_code=200 if r.get('turn_status') == 'SUCCESS' else 500,
              tool_called=str(r.get('tool_names') or r.get('api_method') or 'inference'),
              evolution_generation=0,
          )
      )
    if turns:
      self._cached_live_turns = turns
      return turns

    # 1b. Secondary BigQuery Source: real OTel GenAI turns in the OTel dataset (VIBELIFT_OTEL_DATASET)
    try:
      otel_ds = ge_mart.otel_dataset_name()
    except ValueError:
      otel_ds = ''
    otel_rows = [] if not otel_ds else self._query_bigquery_rest(f"""
      SELECT
        CAST(timestamp AS STRING) AS ts,
        COALESCE(labels.gen_ai_agent_name, 'root_agent') AS agent_name,
        labels.user_id AS user_id,
        COALESCE(labels.gen_ai_conversation_id, 'conv') AS conv_id,
        COALESCE(labels.gen_ai_usage_input_tokens, '0') AS input_tokens,
        COALESCE(labels.gen_ai_usage_output_tokens, '0') AS output_tokens,
        COALESCE(labels.gen_ai_system_instructions_ref, '') AS sys_ref,
        COALESCE(labels.gen_ai_input_messages_ref, '') AS in_ref,
        COALESCE(labels.gen_ai_output_messages_ref, '') AS out_ref
      FROM `{self.project_id}.{otel_ds}.gen_ai_client_inference_operation_details`
      WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
      ORDER BY timestamp DESC
      LIMIT {int(max_results)}
    """, timeout_s=6.0)
    for idx, r in enumerate(otel_rows):
      in_tok = int(r.get('input_tokens') or 0)
      out_tok = int(r.get('output_tokens') or 0)
      if in_tok <= 0 and out_tok <= 0:
        continue
      sys_ref = str(r.get('sys_ref') or '')
      prefix_hash = sys_ref.split('/')[-1][:12] if sys_ref else f'otel_{idx + 1}'
      turns.append(
          telemetry.TurnUsageLog(
              timestamp=str(r.get('ts') or ''),
              agent_name=str(r.get('agent_name') or 'root_agent'),
              model='unknown',  # the OTel labels in this table carry no model name
              turn_index=idx + 1,
              prompt_prefix_hash=prefix_hash,
              cache_breakpoint_line=None,
              cache_breakpoint_reason=f"Live OTel turn ({r.get('user_id')}, conv {str(r.get('conv_id'))[:8]})",
              prompt_token_count=in_tok,
              cached_content_token_count=0,
              cache_creation_input_tokens=0,
              uncached_input_tokens=in_tok,
              candidates_token_count=out_tok,
              thoughts_token_count=0,
              status_code=200,
              tool_called='otel.gen_ai_inference',
              evolution_generation=14,
          )
      )
    if turns:
      self._cached_live_turns = turns
      return turns

    # 2. Secondary Ingestion Pipeline: Direct Cloud Logging
    if self.logging_client is None:
      self._cached_live_turns = turns
      return turns
    if type(self.logging_client).__module__.startswith('google.cloud') and (
        self._credentials is None or not getattr(self._credentials, 'valid', False)
    ):
      self._cached_live_turns = turns
      return turns

    try:
      # Query Cloud Run logs for the allowlisted (monitored) services only.
      monitored = ' OR '.join(f'"{name}"' for name in get_monitored_service_names())
      filter_str = (
          'resource.type="cloud_run_revision" AND '
          f'resource.labels.service_name=({monitored})'
      )
      entries = list(self.logging_client.list_entries(
          filter_=filter_str,
          order_by=gcp_logging.DESCENDING,
          max_results=max_results,
      ))

      turn_idx = 1
      for entry in entries:
        payload = entry.payload
        if not payload:
          continue

        ts_str = entry.timestamp.isoformat() if entry.timestamp else datetime.datetime.now(datetime.UTC).isoformat()
        service_name = 'gemini-enterprise-agent'
        if entry.resource and entry.resource.labels:
          service_name = entry.resource.labels.get('service_name', service_name)

        # Inspect if structured usage_metadata is present
        prompt_tokens = 0
        cached_tokens = 0
        write_tokens = 0
        uncached_tokens = 0
        candidate_tokens = 0
        thought_tokens = 0
        model = 'gemini-3.1-flash'

        if isinstance(payload, dict):
          usage = payload.get('usage_metadata') or payload.get('usageMetadata') or {}
          if usage:
            prompt_tokens = int(usage.get('prompt_token_count') or usage.get('promptTokenCount') or 0)
            cached_tokens = int(usage.get('cached_content_token_count') or usage.get('cachedContentTokenCount') or 0)
            candidate_tokens = int(usage.get('candidates_token_count') or usage.get('candidatesTokenCount') or 0)
            thought_tokens = int(usage.get('thoughts_token_count') or usage.get('thoughtsTokenCount') or 0)
            uncached_tokens = max(0, prompt_tokens - cached_tokens)
            model = payload.get('model', model)

        # Only entries that carry real usage metadata become turns; nothing is synthesized.
        if prompt_tokens <= 0:
          continue

        prefix_hash = hashlib.sha256(f'{service_name}:{turn_idx}'.encode()).hexdigest()[:10]

        turn_log = telemetry.TurnUsageLog(
            timestamp=ts_str,
            agent_name=service_name,
            model=model,
            turn_index=turn_idx,
            prompt_prefix_hash=f'gcp_{prefix_hash}',
            cache_breakpoint_line=None if cached_tokens > 0 else 1,
            cache_breakpoint_reason=(
                '100% Google Cloud prompt cache hit'
                if cached_tokens > 0
                else 'Cold start cache initialization'
            ),
            prompt_token_count=prompt_tokens,
            cached_content_token_count=cached_tokens,
            cache_creation_input_tokens=write_tokens,
            uncached_input_tokens=uncached_tokens,
            candidates_token_count=candidate_tokens,
            thoughts_token_count=thought_tokens,
            status_code=200,
            tool_called=f'{service_name}.inference',
            evolution_generation=14,
        )
        turns.append(turn_log)
        turn_idx += 1

    except Exception as exc:
      logger.warning('Error querying GCP Cloud Logging: %s', exc)

    self._cached_live_turns = turns
    return turns

  def refresh_ge_mart_turns(self) -> dict[str, Any]:
    """Rebuilds the materialized vibelift_mart.fct_turns table from v_fct_turns via BigQuery REST."""
    if not self.project_id or self.project_id in (UNCONFIGURED_PROJECT_ID, 'test-project'):
      return {'status': 'OFFLINE', 'message': 'No live Google Cloud project configured.'}
    mart = ge_mart.mart_ref(self.project_id)
    ddl = ge_mart.build_refresh_fct_turns_ddl(self.project_id)
    self._query_bigquery_rest(ddl, timeout_s=60.0)
    rows = self._query_bigquery_rest(
        f'SELECT COUNT(1) AS row_count, CAST(MAX(refreshed_at) AS STRING) AS refreshed_at FROM `{mart}.fct_turns`',
        timeout_s=15.0,
    )
    self._cached_bq_insights = None
    self._cached_bq_insights_ts = 0.0
    self._cached_bq_summary = None
    self._cached_bq_summary_ts = 0.0
    self._cached_support_events = None
    self._cached_live_turns = None
    row = rows[0] if rows else {}
    return {
        'status': 'REFRESHED',
        'table': f'{mart}.fct_turns',
        'row_count': ge_mart.int_or_none(row.get('row_count')) or 0,
        'refreshed_at': str(row.get('refreshed_at') or ''),
        'refresh_cli': f'python3 deploy/bigquery/provision_ge_mart.py --project {self.project_id} --refresh',
    }

  def fetch_prompt_snapshot_turns(self, limit: int = 60, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Recent OTel GenAI turns whose prompt content (system instruction / input messages) was logged to GCS.

    Rows come from <otel dataset>.gen_ai_client_inference_operation_details; only turns with at least
    one content ref are returned. Cached for 60 seconds.
    """
    import time

    if not self.project_id or self.project_id in (UNCONFIGURED_PROJECT_ID, 'test-project'):
      return []
    cached = getattr(self, '_cached_prompt_turns', None)
    cached_ts = float(getattr(self, '_cached_prompt_turns_ts', 0.0))
    if cached is not None and not force_refresh and time.monotonic() - cached_ts < 60.0:
      return list(cached)
    try:
      otel_ds = ge_mart.otel_dataset_name()
    except ValueError:
      return []
    rows = self._query_bigquery_rest(f"""
      SELECT
        insertId AS event_id,
        CAST(timestamp AS STRING) AS ts,
        labels.gen_ai_agent_name AS agent_name,
        labels.user_id AS user_id,
        COALESCE(labels.gen_ai_conversation_id, '') AS conversation_id,
        SAFE_CAST(labels.gen_ai_usage_input_tokens AS INT64) AS input_tokens,
        COALESCE(labels.gen_ai_system_instructions_ref, '') AS sys_gcs_uri,
        COALESCE(labels.gen_ai_input_messages_ref, '') AS input_gcs_uri
      FROM `{self.project_id}.{otel_ds}.gen_ai_client_inference_operation_details`
      WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 400 DAY)
        AND (COALESCE(labels.gen_ai_system_instructions_ref, '') != ''
             OR COALESCE(labels.gen_ai_input_messages_ref, '') != '')
      ORDER BY timestamp DESC
      LIMIT {max(1, min(int(limit), 200))}
    """, timeout_s=7.0)
    turns = [{
        'event_id': str(r.get('event_id') or ''),
        'timestamp': str(r.get('ts') or ''),
        'agent_name': str(r.get('agent_name') or '') or None,
        'user_id': str(r.get('user_id') or '') or None,
        'conversation_id': str(r.get('conversation_id') or '') or None,
        'input_tokens': int(r['input_tokens']) if r.get('input_tokens') not in (None, '') else None,
        'sys_gcs_uri': str(r.get('sys_gcs_uri') or ''),
        'input_gcs_uri': str(r.get('input_gcs_uri') or ''),
        'source_table': f'{self.project_id}.{otel_ds}.gen_ai_client_inference_operation_details',
    } for r in rows if r.get('event_id')]
    self._cached_prompt_turns = turns
    self._cached_prompt_turns_ts = time.monotonic()
    return list(turns)

  def read_gcs_text(self, gcs_uri: str, max_bytes: int = 2_000_000) -> str:
    """Reads a gs:// object as UTF-8 text via the Cloud Storage JSON API.

    Raises:
      GcsReadError: with status INVALID_URI, NOT_CONNECTED, FORBIDDEN, NOT_FOUND, TOO_LARGE or ERROR.
    """
    import re
    import urllib.parse

    m = re.fullmatch(r'gs://([a-z0-9][a-z0-9._-]{1,220})/(.+)', str(gcs_uri or ''))
    if not m:
      raise GcsReadError('INVALID_URI', f'Not a gs:// object URI: {str(gcs_uri)[:120]!r}')
    bucket, obj = m.group(1), m.group(2)
    token = self._get_access_token()
    if not token:
      raise GcsReadError('NOT_CONNECTED', 'No Google Cloud credentials available to read Cloud Storage.')
    url = (f'https://storage.googleapis.com/storage/v1/b/{bucket}/o/'
           f'{urllib.parse.quote(obj, safe="")}?alt=media')
    # No x-goog-user-project header: it makes Cloud Storage also require serviceusage.services.use on
    # the quota project, which the least-privilege runtime SA does not hold (every read 403s). The
    # prompt-log bucket is not requester-pays, so the resource project is billed as usual.
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
      with urllib.request.urlopen(req, timeout=8.0) as resp:
        data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
      reason = _gcs_error_reason(exc)
      if exc.code in (401, 403):
        raise GcsReadError('FORBIDDEN', f'The runtime identity cannot read gs://{bucket} '
                           f'(needs roles/storage.objectViewer on the bucket). {reason}'.strip()) from exc
      if exc.code == 404:
        raise GcsReadError('NOT_FOUND', f'{gcs_uri} no longer exists (a bucket lifecycle rule may '
                           'have deleted it).') from exc
      raise GcsReadError('ERROR', f'Cloud Storage returned HTTP {exc.code} for {gcs_uri}. {reason}'.strip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
      raise GcsReadError('ERROR', f'Cloud Storage read failed: {exc}') from exc
    if len(data) > max_bytes:
      raise GcsReadError('TOO_LARGE', f'{gcs_uri} is larger than {max_bytes:,} bytes.')
    return data.decode('utf-8', errors='replace')

  def list_gcs_object_names(self, bucket: str, prefix: str = '', max_objects: int = 5000) -> set[str]:
    """Object names under gs://bucket/prefix via one paginated objects.list (names only).

    Raises:
      GcsReadError: INVALID_URI, NOT_CONNECTED, FORBIDDEN, TOO_LARGE (more than max_objects) or ERROR.
    """
    import re
    import urllib.parse

    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{1,220}', str(bucket or '')):
      raise GcsReadError('INVALID_URI', f'Not a bucket name: {str(bucket)[:120]!r}')
    token = self._get_access_token()
    if not token:
      raise GcsReadError('NOT_CONNECTED', 'No Google Cloud credentials available to list Cloud Storage.')
    names: set[str] = set()
    page_token = ''
    while True:
      params = {'prefix': prefix, 'fields': 'items(name),nextPageToken', 'maxResults': '1000'}
      if page_token:
        params['pageToken'] = page_token
      url = f'https://storage.googleapis.com/storage/v1/b/{bucket}/o?{urllib.parse.urlencode(params)}'
      req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
      try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
          page = json.loads(resp.read(8_000_000).decode('utf-8', errors='replace') or '{}')
      except urllib.error.HTTPError as exc:
        reason = _gcs_error_reason(exc)
        if exc.code in (401, 403):
          raise GcsReadError('FORBIDDEN', f'The runtime identity cannot list gs://{bucket} '
                             f'(needs roles/storage.objectViewer on the bucket). {reason}'.strip()) from exc
        raise GcsReadError('ERROR', f'Cloud Storage returned HTTP {exc.code} listing gs://{bucket}. {reason}'.strip()) from exc
      except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GcsReadError('ERROR', f'Cloud Storage list failed: {exc}') from exc
      names.update(str(item.get('name')) for item in page.get('items') or [] if item.get('name'))
      if len(names) > max_objects:
        raise GcsReadError('TOO_LARGE', f'gs://{bucket}/{prefix} holds more than {max_objects:,} objects.')
      page_token = str(page.get('nextPageToken') or '')
      if not page_token:
        return names

  def fetch_live_bigquery_project_insights(
      self,
      force_refresh: bool = False,
      non_blocking: bool = False,
      window_hours: int | None = None,
  ) -> dict[str, Any] | None:
    """Queries real BigQuery telemetry datasets in the project for user, session, tool, and turn grounding."""
    import concurrent.futures
    import threading
    import time

    if not self.project_id or self.project_id in (UNCONFIGURED_PROJECT_ID, 'test-project'):
      return None

    eff_hours = max(1, min(int(window_hours), 8760)) if window_hours is not None else 168
    eff_days = max(30, min(400, -(-eff_hours // 24))) if window_hours is not None else 30
    cached_hours = getattr(self, '_cached_bq_insights_hours', 168)

    now_mono = time.monotonic()
    if (
        self._cached_bq_insights is not None
        and not force_refresh
        and (window_hours is None or eff_hours == cached_hours)
        and (now_mono - self._cached_bq_insights_ts) < 60.0
    ):
      return self._cached_bq_insights

    if non_blocking and not force_refresh:
      evt = threading.Event()
      def _bg() -> None:
        try:
          self.fetch_live_bigquery_project_insights(
              force_refresh=True, non_blocking=False, window_hours=window_hours
          )
        finally:
          evt.set()
      threading.Thread(target=_bg, daemon=True).start()
      evt.wait(timeout=0.05)
      return self._cached_bq_insights

    token = self._get_access_token()
    if not token or self.project_id == UNCONFIGURED_PROJECT_ID:
      return self._cached_bq_insights

    p = self.project_id
    try:
      ge_mart.mart_ref(p)
    except ValueError:
      return self._cached_bq_insights

    try:
      otel_ds = ge_mart.otel_dataset_name()
    except ValueError:
      return self._cached_bq_insights
    analytics_ds = (os.environ.get('VIBELIFT_ANALYTICS_DATASET') or 'vibelift_analytics').strip() or 'vibelift_analytics'

    queries = {
        # Gemini Enterprise: VibeLift's curated views and mart (provision_ge_mart.py).
        'ge_audit_principals': ge_mart.build_audit_principals_sql(p, hours=eff_hours),
        'ge_user_rollup': ge_mart.build_user_engine_rollup_sql(p, hours=eff_hours),
        'ge_recent_turns': ge_mart.build_recent_turns_sql(p, hours=eff_hours, limit=20),
        'ge_daily_totals': ge_mart.build_daily_totals_sql(p, days=eff_days),
        'ge_daily_by_app': ge_mart.build_daily_usage_sql(p, days=eff_days),
        'ge_sessions': ge_mart.build_recent_sessions_sql(p, days=eff_days, limit=50),
        # Turn-by-turn token counts for exactly the 50 sessions above (no prompt text).
        'ge_session_turns': ge_mart.build_session_turns_sql(p, days=eff_days, session_limit=50),
        'sre_triage_turns': f"""
          SELECT
            insertId AS event_id,
            CAST(timestamp AS STRING) AS ts,
            labels.user_id AS user_id,
            labels.gen_ai_agent_name AS agent_name,
            COALESCE(labels.gen_ai_conversation_id, '') AS conversation_id,
            CAST(COALESCE(labels.gen_ai_usage_input_tokens, '0') AS INT64) AS input_tokens,
            CAST(COALESCE(labels.gen_ai_usage_output_tokens, '0') AS INT64) AS output_tokens,
            COALESCE(labels.gen_ai_input_messages_ref, '') AS input_gcs_uri,
            COALESCE(labels.gen_ai_output_messages_ref, '') AS output_gcs_uri,
            COALESCE(labels.gen_ai_system_instructions_ref, '') AS sys_gcs_uri,
            COALESCE(labels.gen_ai_tool_definitions, '[]') AS tool_defs_json,
            resource.labels.reasoning_engine_id AS engine_id
          FROM `{p}.{otel_ds}.gen_ai_client_inference_operation_details`
          WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {eff_hours} HOUR)
          ORDER BY timestamp DESC
          LIMIT 2000
        """,
        'cloud_run_requests': f"""
          SELECT
            resource.labels.revision_name AS revision,
            CAST(httpRequest.status AS STRING) AS status,
            COUNT(*) AS req_count,
            ROUND(AVG(httpRequest.latency * 1000), 1) AS avg_latency_ms,
            ROUND(MAX(httpRequest.latency * 1000), 1) AS max_latency_ms
          FROM `{p}.{analytics_ds}.run_googleapis_com_requests_*`
          WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY))
            AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
          GROUP BY 1, 2
          ORDER BY revision DESC, status ASC
          LIMIT 30
        """,
        'vertex_and_run_audit': f"""
          SELECT * FROM (
            SELECT
              'ds_vertex_agents_raw' AS dataset,
              protopayload_auditlog.authenticationInfo.principalEmail AS principal,
              protopayload_auditlog.methodName AS method_name,
              COUNT(*) AS call_count,
              CAST(MAX(timestamp) AS STRING) AS last_seen
            FROM `{p}.ds_vertex_agents_raw.cloudaudit_googleapis_com_data_access`
            WHERE protopayload_auditlog.authenticationInfo.principalEmail IS NOT NULL
              AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
            GROUP BY 1, 2, 3
            UNION ALL
            SELECT
              '{analytics_ds}' AS dataset,
              protopayload_auditlog.authenticationInfo.principalEmail AS principal,
              protopayload_auditlog.methodName AS method_name,
              COUNT(*) AS call_count,
              CAST(MAX(timestamp) AS STRING) AS last_seen
            FROM `{p}.{analytics_ds}.cloudaudit_googleapis_com_data_access_*`
            WHERE protopayload_auditlog.authenticationInfo.principalEmail IS NOT NULL
              AND _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY))
              AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
            GROUP BY 1, 2, 3
          )
          ORDER BY call_count DESC
          LIMIT 25
        """,
    }

    raw_results: dict[str, list[dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
      futs = {k: pool.submit(self._query_bigquery_rest, sql, 7.0) for k, sql in queries.items()}
      for k, fut in futs.items():
        try:
          raw_results[k] = fut.result()
        except Exception:
          raw_results[k] = []

    ge_audit = raw_results.get('ge_audit_principals', [])
    ge_rollup = raw_results.get('ge_user_rollup', [])
    ge_recent = raw_results.get('ge_recent_turns', [])
    ge_daily = [ge_mart.daily_usage_from_row(r) for r in raw_results.get('ge_daily_totals', [])]
    ge_daily_by_app = [ge_mart.daily_usage_from_row(r) for r in raw_results.get('ge_daily_by_app', [])]
    ge_sessions = [ge_mart.session_from_row(r, p) for r in raw_results.get('ge_sessions', [])]
    ge_session_turns = ge_mart.group_session_turns(raw_results.get('ge_session_turns', []))
    sre_turns = raw_results.get('sre_triage_turns', [])
    cr_reqs = raw_results.get('cloud_run_requests', [])
    vx_audit = raw_results.get('vertex_and_run_audit', [])
    mart = ge_mart.mart_ref(p)
    curated = ge_mart.curated_ref(p)

    if not any((ge_audit, ge_rollup, ge_recent, ge_daily, ge_daily_by_app, ge_sessions, ge_session_turns,
                sre_turns, cr_reqs, vx_audit)):
      return self._cached_bq_insights

    # 1. Usage logs for Tab 5 (aive_logs): GE turns from the mart plus Agent Engine OTel turns.
    # No prompt text, latency or ratings exist in these sources, so none are reported.
    live_usage_logs: list[dict[str, Any]] = [ge_mart.usage_log_from_row(r, p) for r in ge_recent]
    live_ratings_logs: list[dict[str, Any]] = []

    for idx, row in enumerate(sre_turns[:15]):
      uid = str(row.get('user_id') or '') or None
      in_tok = ge_mart.int_or_none(row.get('input_tokens'))
      out_tok = ge_mart.int_or_none(row.get('output_tokens'))
      out_uri = str(row.get('output_gcs_uri') or row.get('input_gcs_uri') or row.get('sys_gcs_uri') or '')
      evt_id = str(row.get('event_id') or f'otel-{idx + 1}')
      source_table = f'{p}.{otel_ds}.gen_ai_client_inference_operation_details'
      live_usage_logs.append({
          'event_id': evt_id,
          'timestamp': str(row.get('ts') or ''),
          'session_id': str(row.get('conversation_id') or '') or None,
          'user_email': uid if uid and '@' in uid else None,
          'user_ldap': uid,
          'company_name': f'Google Cloud ({p})',
          'department': f"ReasoningEngine {row.get('engine_id')} ({otel_ds})",
          'task_type': 'OTEL_GENAI_INFERENCE',
          'agent_name': str(row.get('agent_name') or '') or None,
          'model_name': None,  # not in these OTel labels
          'prompts': [],
          'outputs': [{
              'gcs_uri': out_uri or f'bq://{source_table}/{evt_id}',
              'media_type': 'APPLICATION_JSONL' if out_uri else 'BIGQUERY_ROW',
              'mime_type': 'application/jsonl' if out_uri else 'application/x-bigquery-row',
          }],
          'latency_ms': None,
          'total_tokens': in_tok + out_tok if in_tok is not None and out_tok is not None else None,
          'thinking_tokens': None,
          'background_tokens': None,
          'status': 'UNKNOWN',
          'error_message': None,
          'csat_rating': None,
          'source_table': source_table,
      })

    # 2. Aggregate real principals across BigQuery tables for power_users_ldap
    principal_stats: dict[str, dict[str, Any]] = {}

    def _ensure_p(principal: str, dept: str, primary_agent: str, is_sa: bool = False) -> dict[str, Any]:
      ldap = principal.split('@')[0] if '@' in principal else principal
      return principal_stats.setdefault(principal, {
          'user_email': principal,
          'user_ldap': ldap,
          'department': dept,
          'primary_agent': primary_agent,
          'is_service_account': is_sa,
          'api_calls': 0,
          'interactive_sessions': 0,
          'interactions': 0,
          'observed_tokens': 0,
          'reasoning_tokens': None,
          'methods': set(),
          'source_tables': set(),
          'last_seen': '',
          'engine_calls': {},
          'engine_sessions': {},
      })

    # Real sessions, interactions and tokens per user and GE app from the mart.
    for r in ge_rollup:
      pr = str(r.get('user_email') or '')
      if not pr:
        continue
      is_sa = pr.endswith('.gserviceaccount.com')
      st = _ensure_p(
          pr,
          'Service Account Runtime (vibelift_mart)' if is_sa else 'Gemini Enterprise Interactive User (vibelift_mart)',
          'Gemini Enterprise Assistant & Search',
          is_sa=is_sa,
      )
      sess = ge_mart.int_or_none(r.get('sessions')) or 0
      st['interactive_sessions'] += sess
      st['interactions'] += ge_mart.int_or_none(r.get('interactions')) or 0
      ek = str(r.get('engine_key') or '')
      if ek:
        st['engine_sessions'][ek] = st['engine_sessions'].get(ek, 0) + sess
      tok = ge_mart.int_or_none(r.get('total_tokens'))
      if tok is not None:
        st['observed_tokens'] += tok
        st['tokens_reported'] = True
      rtok = ge_mart.int_or_none(r.get('reasoning_tokens'))
      if rtok is not None:
        st['reasoning_tokens'] = (st['reasoning_tokens'] or 0) + rtok
      st['methods'].add('fct_turns')
      st['source_tables'].add('vibelift_mart')
      ls = str(r.get('last_seen') or '')
      if ls > st['last_seen']:
        st['last_seen'] = ls

    for r in ge_audit:
      pr = str(r.get('principal') or '')
      if not pr:
        continue
      is_sa = pr.endswith('.gserviceaccount.com')
      dept = 'Service Account Runtime (ds_ge_audit_raw)' if is_sa else 'Gemini Enterprise Interactive User (ds_ge_audit_raw)'
      ag = 'vibelift_analytics (Discovery Engine Poller)' if is_sa else 'Gemini Enterprise Assistant & Search'
      st = _ensure_p(pr, dept, ag, is_sa=is_sa)
      cnt = int(r.get('call_count') or 0)
      mname = str(r.get('method_name') or '').split('.')[-1]
      st['api_calls'] += cnt
      ek = str(r.get('engine_key') or '')
      if ek and ek != '/':
        st['engine_calls'][ek] = st['engine_calls'].get(ek, 0) + cnt
      st['methods'].add(mname)
      st['source_tables'].add('ds_ge_audit_raw')
      ls = str(r.get('last_seen') or '')
      if ls > st['last_seen']:
        st['last_seen'] = ls

    for r in vx_audit:
      pr = str(r.get('principal') or '')
      if not pr:
        continue
      is_sa = pr.endswith('.gserviceaccount.com')
      ds_name = str(r.get('dataset') or 'vibelift_analytics')
      dept = f'Service Account ({ds_name})' if is_sa else f'Cloud Run & Vertex Admin ({ds_name})'
      ag = 'ReasoningEngine & Cloud Run API'
      st = _ensure_p(pr, dept, ag, is_sa=is_sa)
      cnt = int(r.get('call_count') or 0)
      mname = str(r.get('method_name') or '').split('.')[-1]
      st['api_calls'] += cnt
      st['methods'].add(mname)
      st['source_tables'].add(ds_name)
      ls = str(r.get('last_seen') or '')
      if ls > st['last_seen']:
        st['last_seen'] = ls

    # Aggregate Agent Engine OTel session user ids (app-supplied labels, not authenticated identities).
    sre_by_user: dict[str, dict[str, Any]] = {}
    extracted_tools: list[dict[str, str]] = []
    otel_engine_ids = sorted({str(r.get('engine_id')) for r in sre_turns if r.get('engine_id')})
    otel_agent_label = (
        'Agent Engine ' + ', '.join(otel_engine_ids) if otel_engine_ids else 'Agent Engine (id not logged)'
    )
    for r in sre_turns:
      if not r.get('user_id'):
        continue  # No session user id logged: do not attribute to anyone.
      uid = str(r.get('user_id'))
      u = sre_by_user.setdefault(uid, {'turns': 0, 'convs': set(), 'in_tok': 0, 'out_tok': 0, 'last_ts': ''})
      u['turns'] += 1
      if r.get('conversation_id'):
        u['convs'].add(str(r.get('conversation_id')))
      u['in_tok'] += int(r.get('input_tokens') or 0)
      u['out_tok'] += int(r.get('output_tokens') or 0)
      ts_val = str(r.get('ts') or '')
      if ts_val > u['last_ts']:
        u['last_ts'] = ts_val
      if not extracted_tools and r.get('tool_defs_json'):
        try:
          tdefs = json.loads(str(r.get('tool_defs_json')))
          if isinstance(tdefs, list):
            extracted_tools = [t for t in tdefs if isinstance(t, dict) and t.get('name')]
        except Exception:
          pass

    for uid, u in sre_by_user.items():
      st = _ensure_p(
          uid,
          'Agent session user id (OTel label, not an authenticated identity)',
          otel_agent_label,
          is_sa=(uid == 'vais-query-reasoning-engine'),
      )
      st['otel_session_id_only'] = True
      st['api_calls'] += u['turns']
      st['interactive_sessions'] += len(u['convs'])  # turns without a conversation id are not sessions
      st['observed_tokens'] += (u['in_tok'] + u['out_tok'])
      st['tokens_reported'] = True
      st['methods'].add('gen_ai.client.inference')
      st['source_tables'].add(otel_ds)
      if u['last_ts'] > st['last_seen']:
        st['last_seen'] = u['last_ts']


    live_power_users: list[dict[str, Any]] = []
    for pr, st in sorted(
        principal_stats.items(),
        key=lambda kv: (kv[1]['is_service_account'], -(kv[1]['interactive_sessions'] + kv[1]['api_calls'])),
    ):
      # Tokens are None (unknown) unless a source reported them for this principal.
      obs_tok = int(st['observed_tokens']) if st.get('tokens_reported') else None
      tok_m = round(obs_tok / 1_000_000.0, 4) if obs_tok is not None else None
      # Real sessions only (mart session ids / OTel conversations); audit call counts are
      # reported separately as api_calls_observed, never relabelled as sessions.
      sessions_val = st['interactive_sessions']
      reasoning = st.get('reasoning_tokens')
      if st['is_service_account']:
        role_tag = 'SERVICE_ACCOUNT_TELEMETRY'
      elif st.get('otel_session_id_only') and '@' not in str(pr):
        # e.g. agents-cli's default session id 'cli-user': real traffic, but not a verified person.
        role_tag = 'UNVERIFIED_SESSION_ID'
      else:
        role_tag = 'LIVE_HUMAN_PRINCIPAL'
      live_power_users.append({
          'user_ldap': st['user_ldap'],
          'user_email': st['user_email'],
          'department': f"{st['department']} [{' + '.join(sorted(st['source_tables']))}]",
          'primary_agent': st['primary_agent'],
          'sessions_7d': int(sessions_val),
          'api_calls_observed': int(st['api_calls']),
          'observed_tokens_exact': obs_tok,
          'total_tokens_m': tok_m,
          'thinking_tokens_k': round(reasoning / 1000.0, 2) if reasoning is not None else None,
          'background_tokens_k': None,
          # Not measured per principal in these sources: leave unset rather than invent values.
          'cache_hit_pct': None,
          'csat_rating': None,
          'avg_csat': None,
          # Billed cost is project-level (Cloud Billing export); it is never allocated per user.
          'monthly_cost_usd': None,
          'cost_30d_usd': None,
          'saved_30d_usd': None,
          'source_tables': sorted(st['source_tables']),
          'last_seen': st['last_seen'],
          # Per GE app ('location/engine_id'), same unit as sessions_7d. Apps a principal only
          # called (audit) appear with 0 sessions so app cohorts still count the principal.
          # Principals seen only in non-GE sources (Agent Engine OTel) have no GE app attribution.
          'by_engine': {
              **{k: 0 for k in st['engine_calls']},
              **st['engine_sessions'],
          },
          'interactions_7d': int(st['interactions']),
          'status': role_tag,
          'anomaly_status': role_tag,
      })

    # 3. Build real skill_mcp_breakdown & decorator_events from OTel tool definitions, ds_ge_audit_raw, and Cloud Run requests
    live_skills_mcp: list[dict[str, Any]] = []
    live_decorator_events: list[dict[str, Any]] = []

    total_sre_in = sum(int(r.get('input_tokens') or 0) for r in sre_turns)
    total_sre_turns = len(sre_turns)
    for tdef in extracted_tools:
      tname = str(tdef.get('name') or 'tool')
      live_skills_mcp.append({
          'resource_name': f'adk_tool://{tname}',
          'kind': 'ADK FunctionTool (OTel)',
          'attached_agent': otel_agent_label,
          # Tool definitions are attached to every inference turn; per-tool call counts are not logged.
          'calls_24h': None,
          'prompt_tokens_m': round(total_sre_in / 1_000_000.0, 4),
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'optimization_applied': f'Declared on {total_sre_turns} recent inference turns ({otel_ds})',
          'monthly_saved_usd': None,
      })
      live_decorator_events.append({
          'timestamp': str(sre_turns[0].get('ts') if sre_turns else 'Live BQ'),
          'agent_name': str(sre_turns[0].get('agent_name') or 'root_agent') if sre_turns else 'root_agent',
          'handler_name': tname,
          'protocol': 'OpenTelemetry gen_ai.client.inference',
          'model': None,
          'latency_ms': None,
          'prompt_tokens': int(sre_turns[0].get('input_tokens') or 0) if sre_turns else None,
          'cached_tokens': None,
          'output_tokens': int(sre_turns[0].get('output_tokens') or 0) if sre_turns else None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'idle_ratio_pct': None,
          'skill_or_mcp': f'adk_tool://{tname}',
          'user_cohort': f'{otel_ds} (BigQuery)',
          'status': 'OTel span (BigQuery)',
      })

    # Add real tool calls observed in vibelift_mart.fct_turns (tool_names / tool_call_count)
    mart_tool_stats: dict[str, dict[str, Any]] = {}
    for r in ge_recent:
      raw_names = str(r.get('tool_names') or '').strip()
      if not raw_names:
        continue
      for tname in [x.strip() for x in raw_names.split(',') if x.strip()]:
        st_tool = mart_tool_stats.setdefault(tname, {
            'calls': 0,
            'in_tok': 0,
            'out_tok': 0,
            'agent_name': str(r.get('agent_name') or r.get('engine_key') or 'Gemini Enterprise Agent'),
            'model_name': r.get('model_name'),
            'ts': str(r.get('ts') or 'Live BQ'),
        })
        st_tool['calls'] += max(1, int(ge_mart.int_or_none(r.get('tool_call_count')) or 1))
        st_tool['in_tok'] += int(ge_mart.int_or_none(r.get('input_tokens')) or 0)
        st_tool['out_tok'] += int(ge_mart.int_or_none(r.get('output_tokens')) or 0)

    for tname, tstat in sorted(mart_tool_stats.items(), key=lambda kv: -kv[1]['calls']):
      live_skills_mcp.append({
          'resource_name': f'ge_tool://{tname}',
          'kind': 'Gemini Enterprise Tool (vibelift_mart)',
          'attached_agent': tstat['agent_name'],
          'calls_24h': tstat['calls'],
          'prompt_tokens_m': round(tstat['in_tok'] / 1_000_000.0, 4) if tstat['in_tok'] > 0 else None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'optimization_applied': f"Observed in {mart}.fct_turns ({tstat['calls']} calls)",
          'monthly_saved_usd': None,
      })
      live_decorator_events.append({
          'timestamp': tstat['ts'],
          'agent_name': tstat['agent_name'],
          'handler_name': tname,
          'protocol': 'Gemini Enterprise Tool Span (vibelift_mart.fct_turns)',
          'model': tstat['model_name'],
          'latency_ms': None,
          'prompt_tokens': tstat['in_tok'] or None,
          'cached_tokens': None,
          'output_tokens': tstat['out_tok'] or None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'idle_ratio_pct': None,
          'skill_or_mcp': f'ge_tool://{tname}',
          'user_cohort': f'{mart}.fct_turns (BigQuery)',
          'status': 'Mart turn tool call (BigQuery)',
      })

    # Add real Discovery Engine methods from ds_ge_audit_raw
    method_totals: dict[str, int] = {}
    for r in ge_audit:
      m = str(r.get('method_name') or '').split('.')[-1]
      if m:
        method_totals[m] = method_totals.get(m, 0) + int(r.get('call_count') or 0)
    for mname, mcount in sorted(method_totals.items(), key=lambda kv: -kv[1])[:6]:
      live_skills_mcp.append({
          'resource_name': f'discoveryengine://{mname}',
          'kind': 'Gemini Enterprise API',
          'attached_agent': 'Gemini Enterprise Assistant (ds_ge_audit_raw)',
          'calls_24h': mcount,
          'prompt_tokens_m': None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'optimization_applied': 'Audit-log call count, last 7 days (ds_ge_audit_raw)',
          'monthly_saved_usd': None,
      })
      live_decorator_events.append({
          'timestamp': str(ge_audit[0].get('last_seen') if ge_audit else 'Live BQ'),
          'agent_name': 'gemini_enterprise_assistant',
          'handler_name': mname,
          'protocol': 'Discovery Engine v1alpha/v1main RPC',
          'model': None,
          'latency_ms': None,
          'prompt_tokens': None,
          'cached_tokens': None,
          'output_tokens': None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'idle_ratio_pct': None,
          'skill_or_mcp': f'discoveryengine://{mname} ({mcount} calls)',
          'user_cohort': 'ds_ge_audit_raw (BigQuery)',
          'status': 'Audit log (BigQuery)',
      })

    # Add Cloud Run request aggregates from <analytics_ds>.run_googleapis_com_requests_*
    total_cr_calls = sum(int(r.get('req_count') or 0) for r in cr_reqs)
    if total_cr_calls > 0:
      live_skills_mcp.append({
          'resource_name': 'mcp://vibelift-analytics/streamable-http',
          'kind': 'Cloud Run MCP Server',
          'attached_agent': 'VibeLift Analytics & FinOps (vibe-lift-agent)',
          'calls_24h': total_cr_calls,
          'prompt_tokens_m': None,
          'cache_hit_pct': None,
          'context_bloat_pct': None,
          'optimization_applied': f'Request log count across {len({r.get("revision") for r in cr_reqs})} Cloud Run revisions ({analytics_ds})',
          'monthly_saved_usd': None,
      })

    insights: dict[str, Any] = {
        'project_id': p,
        'fetched_at_utc': datetime.datetime.now(datetime.UTC).isoformat(),
        'queried_tables': [
            f'{curated}.v_consolidated_audit_log',
            f'{mart}.fct_turns',
            f'{mart}.fct_sessions',
            f'{mart}.agg_daily_usage',
            f'{p}.{otel_ds}.gen_ai_client_inference_operation_details',
            f'{p}.{analytics_ds}.run_googleapis_com_requests_*',
        ],
        'ge_mart_dataset': mart,
        'ge_curated_dataset': curated,
        'ge_audit_principals': ge_audit,
        # Per-day GE usage for the last 30 days (fct_turns); joined with billed cost in server.py.
        'ge_daily_totals': ge_daily,
        # Per-day usage broken down by (engine_key, agent_name, model_name) from agg_daily_usage.
        'ge_daily_by_app': ge_daily_by_app,
        # Real conversation sessions from fct_sessions.
        'ge_sessions': ge_sessions,
        # {session_key: [turns oldest-first]} for the sessions above; token counts only.
        'ge_session_turns': ge_session_turns,
        # When fct_turns was last rebuilt (None = unknown); the table is a scheduled snapshot.
        'ge_mart_refreshed_at': ge_mart.mart_refreshed_at(ge_daily),
        'ge_assistant_activity_count': sum(int(d.get('chat_turns') or 0) for d in ge_daily[-7:]),
        'ge_search_activity_count': sum(int(d.get('searches') or 0) for d in ge_daily[-7:]),
        'sre_triage_turns_count': len(sre_turns),
        'sre_triage_total_input_tokens': total_sre_in,
        'sre_triage_total_output_tokens': sum(int(r.get('output_tokens') or 0) for r in sre_turns),
        'cloud_run_requests_total': total_cr_calls,
        'cloud_run_revisions': cr_reqs,
        'vertex_and_run_audit': vx_audit,
        'live_power_users_ldap': live_power_users,
        'power_users_ldap': live_power_users,
        'live_skills_mcp': live_skills_mcp,
        'skills_mcp': live_skills_mcp,
        'live_aive_usage_logs': live_usage_logs,
        'aive_usage_logs': live_usage_logs,
        'usage_logs': live_usage_logs,
        'live_aive_ratings_logs': live_ratings_logs,
        'aive_ratings_logs': live_ratings_logs,
        'ratings_logs': live_ratings_logs,
        'live_decorator_events': live_decorator_events,
        'decorator_events': live_decorator_events,
    }
    self._cached_bq_insights = insights
    self._cached_bq_insights_ts = now_mono
    self._cached_bq_insights_hours = eff_hours
    return insights

  def get_telemetry_summary_payload(self, hours_ago: int = 48) -> dict[str, Any]:
    """Aggregates project info, deployed services, BigQuery triage, and Cloud Logging turns."""
    eff_hours = max(1, int(hours_ago))
    services = self.list_cloud_run_agent_services()
    support_events = self.fetch_gemini_enterprise_support_telemetry(limit=10)
    live_turns = self.fetch_live_cloud_turns(hours_ago=eff_hours, max_results=15)
    bq_fleet = self.fetch_bigquery_fleet_summary(hours_ago=max(168, eff_hours))

    summary_stats = telemetry.summarize_log_stream(live_turns)
    otel_ds = ge_mart.otel_dataset_name()
    analytics_ds = (os.environ.get('VIBELIFT_ANALYTICS_DATASET') or 'vibelift_analytics').strip() or 'vibelift_analytics'

    return {
        'project_id': self.project_id,
        'region': self.region,
        'window_hours': eff_hours,
        'provider': 'Google Cloud Platform (BigQuery + Cloud Run + Cloud Logging)',
        'services_count': len(services),
        'cloud_run_agent_count': len(services),
        'services': services,
        'cloud_run_services': services,
        'bigquery_datasets': [
            analytics_ds,
            ge_mart.curated_dataset_name(),
            ge_mart.mart_dataset_name(),
            otel_ds,
        ],
        'bigquery_fleet_summary': bq_fleet,
        'gemini_enterprise_support_events': support_events,
        'live_turns_ingested': len(live_turns),
        'live_turns': [t.to_dict() for t in live_turns],
        'summary_stats': dict(summary_stats),
        'timestamp_utc': datetime.datetime.now(datetime.UTC).isoformat(),
    }

  def get_cloud_telemetry_summary(self, hours_ago: int = 48) -> dict[str, Any]:
    """Alias for get_telemetry_summary_payload."""
    return self.get_telemetry_summary_payload(hours_ago=hours_ago)


def get_gcp_telemetry_service(
    project_id: str | None = None, region: str | None = None
) -> GoogleCloudTelemetryService:
  """Returns an instance of GoogleCloudTelemetryService."""
  return GoogleCloudTelemetryService(project_id=project_id, region=region)
