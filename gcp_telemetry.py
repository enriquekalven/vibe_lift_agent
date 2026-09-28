"""Google Cloud Project Telemetry Service for VibeLift Analytics Platform.

Fetches live telemetry, token cache economics, and agent interaction logs
from the hosting Google Cloud Project (Cloud Run, Cloud Logging, Cloud Monitoring,
and Gemini Enterprise / BigQuery Agent Analytics).
"""

from collections.abc import Mapping, Sequence
import datetime
import hashlib
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request

try:
  from google.cloud import logging as gcp_logging
except ImportError:
  gcp_logging = None

try:
  from google.cloud import bigquery as gcp_bigquery
except ImportError:
  gcp_bigquery = None

try:
  import google.auth as google_auth
  from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
  google_auth = None
  GoogleAuthRequest = None

import telemetry

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


class GoogleCloudTelemetryService:
  """Service for aggregating live GCP telemetry from Cloud Logging and BigQuery."""

  def __init__(self, project_id: str | None = None, region: str | None = None):
    self.project_id = project_id or get_current_gcp_project()
    self.region = region or get_current_gcp_region()
    self._logging_client = None
    self._bigquery_client = None
    self._credentials = None
    self._cached_services: list[dict[str, str]] | None = None
    self._cached_support_events: list[dict[str, object]] | None = None
    self._cached_live_turns: list[telemetry.TurnUsageLog] | None = None

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
    """Returns an ADC OAuth2 access token (runtime service account on Cloud Run)."""
    if google_auth is None or GoogleAuthRequest is None:
      return None
    try:
      if self._credentials is None:
        self._credentials, _ = google_auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
      if not self._credentials.valid:
        self._credentials.refresh(GoogleAuthRequest())
      return self._credentials.token
    except Exception as exc:
      logger.debug('Could not obtain ADC access token: %s', exc)
      return None

  def list_cloud_run_agent_services(
      self,
      force_refresh: bool = False,
      non_blocking: bool = False,
  ) -> list[dict[str, str]]:
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
          }
          for name in get_monitored_service_names()
      ]

    token = self._get_access_token()
    discovered = []
    for name in get_monitored_service_names():
      entry = {'service_name': name, 'url': '', 'region': self.region, 'status': 'UNKNOWN'}
      if token:
        api_url = (
            f'https://run.googleapis.com/v2/projects/{self.project_id}'
            f'/locations/{self.region}/services/{name}'
        )
        try:
          req = urllib.request.Request(api_url, headers={'Authorization': f'Bearer {token}'})
          with urllib.request.urlopen(req, timeout=3.0) as resp:
            svc = json.loads(resp.read().decode('utf-8'))
          entry['url'] = svc.get('uri', '')
          state = (svc.get('terminalCondition') or {}).get('state', '')
          entry['status'] = 'READY' if state == 'CONDITION_SUCCEEDED' else (state or 'ACTIVE')
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
          logger.debug('Cloud Run Admin API describe failed for %s: %s', name, exc)
      discovered.append(entry)

    self._cached_services = discovered
    return discovered

  def fetch_gemini_enterprise_support_telemetry(
      self,
      limit: int = 15,
      force_refresh: bool = False,
      non_blocking: bool = False,
  ) -> list[dict[str, object]]:
    """Fetches high-level triage events from Gemini Enterprise views if available."""
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

    events = []
    if self.bigquery_client is not None:
      try:
        query = f"""
        SELECT
          event_timestamp,
          source_system,
          triage_category,
          support_tier,
          user_email
        FROM `{self.project_id}.gemini_enterprise_support_views.vw_l1_l2_unified_triage_logs`
        ORDER BY event_timestamp DESC
        LIMIT {limit}
        """
        job = self.bigquery_client.query(query)
        for row in job.result():
          events.append({
              'timestamp': str(row.get('event_timestamp')),
              'source': str(row.get('source_system')),
              'category': str(row.get('triage_category')),
              'support_tier': str(row.get('support_tier')),
              'user': str(row.get('user_email', 'system')),
          })
      except Exception as exc:
        logger.debug('Gemini Enterprise view query skipped: %s', exc)

    self._cached_support_events = events
    return events

  def fetch_bigquery_fleet_summary(self, hours_ago: int = 168) -> dict[str, object] | None:
    """Queries BigQuery vibelift_analytics.vw_fleet_finops_summary for fast pre-aggregated metrics."""
    if self.bigquery_client is None:
      return None

    try:
      query = f"""
      SELECT
        agent_id,
        model,
        total_turns,
        total_prompt_tokens,
        total_cached_tokens,
        aggregate_cache_hit_ratio,
        total_naive_spend_usd,
        total_actual_spend_usd,
        total_finops_savings_usd,
        avg_latency_ms
      FROM `{self.project_id}.vibelift_analytics.vw_fleet_finops_summary`
      LIMIT 20
      """
      job = self.bigquery_client.query(query)
      rows = list(job.result())
      if not rows:
        return None

      fleet_breakdown = []
      total_prompts = 0
      total_cached = 0
      total_naive = 0.0
      total_actual = 0.0
      total_savings = 0.0

      for row in rows:
        agent_id = str(row.get('agent_id', 'agent'))
        model = str(row.get('model', 'gemini-2.5-flash'))
        prompt_tok = int(row.get('total_prompt_tokens') or 0)
        cached_tok = int(row.get('total_cached_tokens') or 0)
        naive_usd = float(row.get('total_naive_spend_usd') or 0.0)
        actual_usd = float(row.get('total_actual_spend_usd') or 0.0)
        saved_usd = float(row.get('total_finops_savings_usd') or 0.0)

        total_prompts += prompt_tok
        total_cached += cached_tok
        total_naive += naive_usd
        total_actual += actual_usd
        total_savings += saved_usd

        fleet_breakdown.append({
            'agent_id': agent_id,
            'model': model,
            'total_turns': int(row.get('total_turns') or 0),
            'prompt_tokens': prompt_tok,
            'cached_tokens': cached_tok,
            'cache_hit_ratio': float(row.get('aggregate_cache_hit_ratio') or 0.0),
            'naive_spend_usd': round(naive_usd, 4),
            'actual_spend_usd': round(actual_usd, 4),
            'saved_usd': round(saved_usd, 4),
            'avg_latency_ms': float(row.get('avg_latency_ms') or 0.0),
        })

      overall_cache_ratio = round((total_cached / total_prompts * 100.0), 2) if total_prompts > 0 else 0.0
      return {
          'source': 'BigQuery (vibelift_analytics.vw_fleet_finops_summary)',
          'fleet_breakdown': fleet_breakdown,
          'total_prompt_tokens': total_prompts,
          'total_cached_tokens': total_cached,
          'aggregate_cache_hit_ratio': overall_cache_ratio,
          'total_naive_spend_usd': round(total_naive, 4),
          'total_actual_spend_usd': round(total_actual, 4),
          'total_finops_savings_usd': round(total_savings, 4),
      }
    except Exception as exc:
      logger.debug('BigQuery fleet summary query skipped/fallback: %s', exc)
      return None

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

    # 1. Primary High-Performance Query: BigQuery vibelift_analytics.agent_turns
    if self.bigquery_client is not None:
      try:
        query = f"""
        SELECT
          timestamp,
          agent_id,
          model,
          turn_index,
          prompt_tokens,
          cached_tokens,
          uncached_tokens,
          output_tokens,
          thoughts_tokens,
          tool_called,
          cache_breakpoint_line,
          cache_breakpoint_reason,
          prompt_prefix_hash,
          generation,
          status_code
        FROM `{self.project_id}.vibelift_analytics.agent_turns`
        WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours_ago} HOUR)
        ORDER BY timestamp DESC
        LIMIT {max_results}
        """
        job = self.bigquery_client.query(query)
        for row in job.result():
          ts_str = str(row.get('timestamp'))
          agent_name = str(row.get('agent_id', 'gemini-agent'))
          model = str(row.get('model', 'gemini-2.5-flash'))
          prompt_tok = int(row.get('prompt_tokens') or 0)
          cached_tok = int(row.get('cached_tokens') or 0)
          uncached_tok = int(row.get('uncached_tokens') or max(0, prompt_tok - cached_tok))
          output_tok = int(row.get('output_tokens') or 0)
          thoughts_tok = int(row.get('thoughts_tokens') or 0)
          turn_idx = int(row.get('turn_index') or len(turns) + 1)
          breakpoint_line = row.get('cache_breakpoint_line')
          breakpoint_reason = str(row.get('cache_breakpoint_reason') or 'BigQuery recorded turn')
          prefix_hash = str(row.get('prompt_prefix_hash') or 'bq_hash')
          gen = int(row.get('generation') or 14)
          tool_called = str(row.get('tool_called') or f'{agent_name}.inference')

          turn_log = telemetry.TurnUsageLog(
              timestamp=ts_str,
              agent_name=agent_name,
              model=model,
              turn_index=turn_idx,
              prompt_prefix_hash=prefix_hash,
              cache_breakpoint_line=int(breakpoint_line) if breakpoint_line is not None else None,
              cache_breakpoint_reason=breakpoint_reason,
              prompt_token_count=prompt_tok,
              cached_content_token_count=cached_tok,
              cache_creation_input_tokens=0,
              uncached_input_tokens=uncached_tok,
              candidates_token_count=output_tok,
              thoughts_token_count=thoughts_tok,
              status_code=int(row.get('status_code') or 200),
              tool_called=tool_called,
              evolution_generation=gen,
          )
          turns.append(turn_log)

        if turns:
          self._cached_live_turns = turns
          return turns
      except Exception as exc:
        logger.debug('BigQuery agent_turns query fallback to Cloud Logging: %s', exc)

    # 2. Secondary Ingestion Pipeline: Direct Cloud Logging (or synthetic live feed)
    if self.logging_client is None:
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

        ts_str = entry.timestamp.isoformat() if entry.timestamp else datetime.datetime.now(datetime.timezone.utc).isoformat()
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

        prefix_hash = hashlib.sha256(f'{service_name}:{turn_idx}'.encode('utf-8')).hexdigest()[:10]

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

  def get_telemetry_summary_payload(self) -> dict[str, object]:
    """Aggregates project info, deployed services, BigQuery triage, and Cloud Logging turns."""
    services = self.list_cloud_run_agent_services()
    support_events = self.fetch_gemini_enterprise_support_telemetry(limit=10)
    live_turns = self.fetch_live_cloud_turns(max_results=15)
    bq_fleet = self.fetch_bigquery_fleet_summary()

    summary_stats = telemetry.summarize_log_stream(live_turns)

    return {
        'project_id': self.project_id,
        'region': self.region,
        'provider': 'Google Cloud Platform (BigQuery + Cloud Run + Cloud Logging)',
        'services_count': len(services),
        'cloud_run_agent_count': len(services),
        'services': services,
        'cloud_run_services': services,
        'bigquery_datasets': ['vibelift_analytics', 'gemini_enterprise_support_views', 'sre_triage_agent_telemetry'],
        'bigquery_fleet_summary': bq_fleet,
        'gemini_enterprise_support_events': support_events,
        'live_turns_ingested': len(live_turns),
        'live_turns': [t.to_dict() for t in live_turns],
        'summary_stats': dict(summary_stats),
        'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

  def get_cloud_telemetry_summary(self) -> dict[str, object]:
    """Alias for get_telemetry_summary_payload."""
    return self.get_telemetry_summary_payload()


def get_gcp_telemetry_service(
    project_id: str | None = None, region: str | None = None
) -> GoogleCloudTelemetryService:
  """Returns an instance of GoogleCloudTelemetryService."""
  return GoogleCloudTelemetryService(project_id=project_id, region=region)
