#!/usr/bin/env python3
"""Provisions VibeLift's Gemini Enterprise curated views and reporting mart in BigQuery.

Layers:

#   <curated dataset>  v_user_activity_curated, v_agentic_operations_curated,
#                      v_consolidated_audit_log (v_model_armor_curated commented out until enabled)
#                      (views, ported from gemini-enterprise-stage ds_ge_curated_staging, bugs fixed)
#   <mart dataset>     v_fct_turns (turn logic view), fct_turns (its materialized copy, with
#                      refreshed_at), fct_sessions and agg_daily_usage (views over fct_turns)
#
# fct_turns is a table because v_fct_turns joins the curated views over raw JSON and takes minutes to
# read. Rebuild it with --refresh on a schedule; readers use refreshed_at to show staleness.
#
# The raw log-sink tables differ between projects (maui lacks fields stage has, some tables do
# not exist at all), so every source is read through a small adapter that exposes the record
# columns as JSON. A missing column becomes NULL and a missing table becomes an empty source,
# so the views always build and simply return no rows for what is not logged.
#
# Usage:
#   python3 deploy/bigquery/provision_ge_mart.py --project project-maui --print
#   python3 deploy/bigquery/provision_ge_mart.py --project project-maui --dry-run
#   python3 deploy/bigquery/provision_ge_mart.py --project project-maui --apply
#   python3 deploy/bigquery/provision_ge_mart.py --project project-maui --refresh
#
#   # Validate the SQL against another project's raw logs, into a throwaway dataset pair:
#   python3 deploy/bigquery/provision_ge_mart.py --project project-maui \
#       --raw-project gemini-enterprise-stage --curated-dataset vibelift_port_validation \
#       --mart-dataset vibelift_port_validation --lookback-days 90 --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import sys
from collections.abc import Callable, Mapping

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / 'ge_mart'

DEFAULT_CURATED_DATASET = 'ds_ge_curated_staging'
DEFAULT_MART_DATASET = 'vibelift_mart'
DEFAULT_LOCATION = 'US'
# Matches the default raw log retention set by deploy/set_log_retention.sh (VIBELIFT_RETENTION_DAYS).
DEFAULT_LOOKBACK_DAYS = 90
# The audit entry is written at request start, the activity entry at completion.
DEFAULT_AUDIT_LAG_S = 300
DEFAULT_AUDIT_LEAD_S = 30
JOB_LABELS = {'datacloud': 'jetski', 'app': 'vibelift'}

_PROJECT_RE = re.compile(r'^(?:[a-z][a-z0-9\-]{1,61}[a-z0-9]\.[a-z]{2,}:)?[a-z][a-z0-9\-]{4,61}[a-z0-9]$')
_DATASET_RE = re.compile(r'^[A-Za-z0-9_]{1,1024}$')
_TABLE_RE = re.compile(r'^[A-Za-z0-9_\-]{1,1024}$')
_PLACEHOLDER_RE = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')
_SCALAR_COLUMNS = (('timestamp', 'TIMESTAMP'), ('trace', 'STRING'), ('insertId', 'STRING'), ('severity', 'STRING'))


@dataclasses.dataclass(frozen=True)
class SourceSpec:
  dataset: str
  table: str
  json_columns: tuple[str, ...]


SOURCES: dict[str, SourceSpec] = {
    'assistant': SourceSpec(
        'ds_ge_assistant_raw', 'discoveryengine_googleapis_com_gemini_enterprise_user_activity',
        ('jsonPayload', 'resource')),
    'search': SourceSpec(
        'ds_ge_search_raw', 'discoveryengine_googleapis_com_gemini_enterprise_user_activity',
        ('jsonPayload', 'resource')),
    'inference': SourceSpec(
        'ds_vertex_agents_raw', 'discoveryengine_googleapis_com_gen_ai_client_inference_operation_details',
        ('jsonPayload', 'labels', 'resource')),
    'audit_activity': SourceSpec(
        'ds_ge_audit_raw', 'cloudaudit_googleapis_com_activity', ('protopayload_auditlog', 'resource', 'operation')),
    'audit_data_access': SourceSpec(
        'ds_ge_audit_raw', 'cloudaudit_googleapis_com_data_access',
        ('protopayload_auditlog', 'resource', 'operation')),
    # Uncomment when Model Armor log sink (ds_security_guardrails_raw) is enabled:
    # 'armor': SourceSpec(
    #     'ds_security_guardrails_raw', 'modelarmor_googleapis_com_sanitize_operations',
    #     ('jsonpayload_v1_sanitizeoperationlogentry', 'labels', 'resource')),
}

# (layer, view name, description). Order matters: later views read earlier ones.
OBJECTS: tuple[tuple[str, str, str], ...] = (
    ('curated', 'v_user_activity_curated', 'GE assistant + search activity (VibeLift port of stage curated view).'),
    ('curated', 'v_agentic_operations_curated', 'Agent inference operations with token usage.'),
    ('curated', 'v_consolidated_audit_log', 'GE admin-activity and data-access audit calls.'),
    # Uncomment when Model Armor is enabled (along with the armor CTEs in mart/v_fct_turns.sql):
    # ('curated', 'v_model_armor_curated', 'Model Armor sanitize operations.'),
    ('mart', 'v_fct_turns', 'VibeLift turn logic (always current, slow). fct_turns is its materialized copy.'),
    ('mart', 'fct_turns', 'VibeLift turn fact: one row per user interaction (materialized; see refreshed_at).'),
    ('mart', 'fct_sessions', 'VibeLift session fact: one row per logged session.'),
    ('mart', 'agg_daily_usage', 'VibeLift daily usage by engine, agent and model.'),
)

# Objects built as tables instead of views: name -> (partition column, cluster columns).
# Rebuilt in full by --apply and --refresh.
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    'fct_turns': ('event_date', ('engine_key', 'user_email')),
}

# Describes a table: returns {column: type, '__location__': location} or None when absent.
TableDescriber = Callable[[str], 'Mapping[str, str] | None']


def validate_project(value: str) -> str:
  if not _PROJECT_RE.match(value or ''):
    raise ValueError(f'Invalid project id: {value!r}')
  return value


def validate_dataset(value: str) -> str:
  if not _DATASET_RE.match(value or ''):
    raise ValueError(f'Invalid dataset id: {value!r}')
  return value


def validate_table_ref(value: str) -> str:
  parts = (value or '').strip('`').split('.')
  if len(parts) != 3:
    raise ValueError(f'Table reference must be project.dataset.table: {value!r}')
  validate_project(parts[0])
  validate_dataset(parts[1])
  if not _TABLE_RE.match(parts[2]):
    raise ValueError(f'Invalid table id: {parts[2]!r}')
  return '.'.join(parts)


def render(template: str, values: Mapping[str, str]) -> str:
  """Fills {{name}} placeholders. Fails on any placeholder without a value.

  string.Template is not used because the SQL regexes contain '$'.
  """
  missing = sorted(set(_PLACEHOLDER_RE.findall(template)) - set(values))
  if missing:
    raise KeyError(f'Missing template values: {", ".join(missing)}')
  out = _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)
  if '{{' in out or '}}' in out:
    raise ValueError('Unrendered or malformed placeholder left in SQL')
  return out


def empty_source_sql(json_columns: tuple[str, ...]) -> str:
  cols = [f'CAST(NULL AS {t}) AS {c}' for c, t in _SCALAR_COLUMNS]
  cols += [f'CAST(NULL AS JSON) AS {c}' for c in json_columns]
  return 'SELECT ' + ', '.join(cols) + ' FROM UNNEST([1]) AS _empty WHERE FALSE'


def source_sql(table_ref: str | None, schema: Mapping[str, str] | None, json_columns: tuple[str, ...],
               lookback_days: int) -> str:
  """Adapter that exposes a log-sink table with a fixed column set.

  Record columns become JSON (TO_JSON keeps the sink's exact field names, which are lowercase
  under jsonPayload/resource/labels and camelCase under protopayload_auditlog).
  """
  if not table_ref or not schema or schema.get('timestamp', '').upper() != 'TIMESTAMP':
    return empty_source_sql(json_columns)
  table_ref = validate_table_ref(table_ref)
  days = max(1, min(int(lookback_days), 400))
  cols = []
  for name, sql_type in _SCALAR_COLUMNS:
    cols.append(f'`{name}`' if name in schema else f'CAST(NULL AS {sql_type}) AS {name}')
  for name in json_columns:
    col_type = (schema.get(name) or '').upper()
    if col_type in ('RECORD', 'STRUCT'):
      cols.append(f'TO_JSON(`{name}`) AS {name}')
    elif col_type == 'JSON':
      cols.append(f'`{name}` AS {name}')
    elif col_type == 'STRING':
      cols.append(f'SAFE.PARSE_JSON(`{name}`) AS {name}')
    else:
      cols.append(f'CAST(NULL AS JSON) AS {name}')
  return (
      'SELECT ' + ', '.join(cols) + f' FROM `{table_ref}`'
      f' WHERE `timestamp` >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)'
  )


@dataclasses.dataclass
class ResolvedSource:
  key: str
  table_ref: str
  status: str  # FOUND | MISSING | WRONG_LOCATION
  detail: str
  sql: str


def resolve_sources(raw_project: str, location: str, lookback_days: int, describe: TableDescriber,
                    overrides: Mapping[str, str] | None = None) -> dict[str, ResolvedSource]:
  overrides = dict(overrides or {})
  unknown = sorted(set(overrides) - set(SOURCES))
  if unknown:
    raise ValueError(f'Unknown source override(s): {", ".join(unknown)}; known: {", ".join(SOURCES)}')
  resolved: dict[str, ResolvedSource] = {}
  for key, spec in SOURCES.items():
    ref = validate_table_ref(overrides.get(key) or f'{raw_project}.{spec.dataset}.{spec.table}')
    schema = describe(ref)
    if schema is None:
      resolved[key] = ResolvedSource(key, ref, 'MISSING', 'table not found; view returns no rows',
                                     empty_source_sql(spec.json_columns))
      continue
    table_location = str(schema.get('__location__') or '')
    if table_location and table_location.upper() != location.upper():
      resolved[key] = ResolvedSource(
          key, ref, 'WRONG_LOCATION',
          f'table is in {table_location}, views are in {location}; BigQuery cannot join across locations',
          empty_source_sql(spec.json_columns))
      continue
    present = [c for c in spec.json_columns if c in schema]
    resolved[key] = ResolvedSource(
        key, ref, 'FOUND', f'record columns present: {", ".join(present) or "none"}',
        source_sql(ref, schema, spec.json_columns, lookback_days))
  return resolved


def load_template(layer: str, name: str) -> str:
  return (TEMPLATE_DIR / layer / f'{name}.sql').read_text(encoding='utf-8')


def build_view_bodies(project: str, curated_dataset: str, mart_dataset: str,
                      sources: Mapping[str, ResolvedSource], *,
                      audit_lag_s: int = DEFAULT_AUDIT_LAG_S,
                      audit_lead_s: int = DEFAULT_AUDIT_LEAD_S,
                      include_audit_only_successes: bool = False,
                      templates: Mapping[tuple[str, str], str] | None = None) -> dict[str, str]:
  """Returns {'dataset.view': select_sql} in creation order."""
  validate_project(project)
  validate_dataset(curated_dataset)
  validate_dataset(mart_dataset)
  values = {f'src_{k}': v.sql for k, v in sources.items()}
  values.update({
      'curated': f'{project}.{curated_dataset}',
      'mart': f'{project}.{mart_dataset}',
      'audit_lag_ms': str(max(0, min(int(audit_lag_s), 3600)) * 1000),
      'audit_lead_ms': str(max(0, min(int(audit_lead_s), 3600)) * 1000),
      'include_audit_only_successes': 'TRUE' if include_audit_only_successes else 'FALSE',
  })
  bodies: dict[str, str] = {}
  for layer, name, _ in OBJECTS:
    template = (templates or {}).get((layer, name)) or load_template(layer, name)
    dataset = curated_dataset if layer == 'curated' else mart_dataset
    bodies[f'{dataset}.{name}'] = render(template, values).strip().rstrip(';')
  return bodies


def view_ddl(project: str, qualified_name: str, body: str, description: str) -> str:
  desc = description.replace('\\', '\\\\').replace('"', '\\"')
  return (
      f'CREATE OR REPLACE VIEW `{project}.{qualified_name}`\n'
      f'OPTIONS(description="{desc}", labels=[("app", "vibelift")])\n'
      f'AS\n{body}\n'
  )


def table_ddl(project: str, qualified_name: str, body: str, description: str) -> str:
  """Full rebuild of a materialized object. CREATE OR REPLACE swaps atomically for readers."""
  partition_col, cluster_cols = TABLES[qualified_name.split('.')[-1]]
  desc = description.replace('\\', '\\\\').replace('"', '\\"')
  return (
      f'CREATE OR REPLACE TABLE `{project}.{qualified_name}`\n'
      f'PARTITION BY {partition_col}\n'
      f'CLUSTER BY {", ".join(cluster_cols)}\n'
      f'OPTIONS(description="{desc}", labels=[("app", "vibelift")])\n'
      f'AS\n{body}\n'
  )


def object_ddl(project: str, qualified_name: str, body: str, description: str) -> str:
  if qualified_name.split('.')[-1] in TABLES:
    return table_ddl(project, qualified_name, body, description)
  return view_ddl(project, qualified_name, body, description)


def inline_view_refs(sql: str, project: str, bodies: Mapping[str, str]) -> str:
  """Replaces references to not-yet-created views with their SQL, for dry-run validation."""
  for name, body in bodies.items():
    ref = f'`{project}.{name}`'
    if ref in sql:
      sql = sql.replace(ref, f'(\n{inline_view_refs(body, project, bodies)}\n)')
  return sql


def _descriptions() -> dict[str, str]:
  return {name: desc for _, name, desc in OBJECTS}


# ---------------------------------------------------------------------------------------------
# BigQuery I/O (not exercised by unit tests)
# ---------------------------------------------------------------------------------------------


def _bq_describer(client) -> TableDescriber:
  from google.api_core import exceptions as gexc  # pylint: disable=import-outside-toplevel

  def describe(ref: str):
    try:
      table = client.get_table(ref)
    except gexc.NotFound:
      return None
    except gexc.Forbidden as exc:
      print(f'  ! no access to {ref}: {exc.message}', file=sys.stderr)
      return None
    schema = {f.name: f.field_type for f in table.schema}
    schema['__location__'] = table.location or ''
    return schema

  return describe


def _ensure_dataset(client, project: str, dataset: str, location: str) -> None:
  from google.api_core import exceptions as gexc  # pylint: disable=import-outside-toplevel
  from google.cloud import bigquery  # pylint: disable=import-outside-toplevel

  ref = f'{project}.{dataset}'
  try:
    existing = client.get_dataset(ref)
  except gexc.NotFound:
    ds = bigquery.Dataset(ref)
    ds.location = location
    ds.description = 'VibeLift Gemini Enterprise reporting (managed by deploy/bigquery/provision_ge_mart.py).'
    ds.labels = {'app': 'vibelift'}
    client.create_dataset(ds)
    print(f'  + created dataset {ref} ({location})')
    return
  if (existing.location or '').upper() != location.upper():
    raise SystemExit(f'Dataset {ref} exists in {existing.location}, expected {location}.')
  print(f'  = dataset {ref} exists ({existing.location})')


# Objects that are currently disabled/commented out and should be removed if present during --apply.
DISABLED_OBJECTS: tuple[tuple[str, str], ...] = (
    ('curated', 'v_model_armor_curated'),
)


def _drop_if_view(client, ref: str) -> None:
  """CREATE OR REPLACE TABLE cannot replace a view (earlier versions built fct_turns as one)."""
  from google.api_core import exceptions as gexc  # pylint: disable=import-outside-toplevel

  try:
    existing = client.get_table(ref)
  except gexc.NotFound:
    return
  if existing.table_type == 'VIEW':
    client.delete_table(ref)
    print(f'  - dropped view {ref} (now a table)')


def _drop_disabled_objects(client, project: str, curated_dataset: str, mart_dataset: str) -> None:
  """Removes views/tables that are commented out in OBJECTS so stale objects do not linger."""
  from google.api_core import exceptions as gexc  # pylint: disable=import-outside-toplevel

  active_names = {name for _, name, _ in OBJECTS}
  for layer, name in DISABLED_OBJECTS:
    if name in active_names:
      continue
    dataset = curated_dataset if layer == 'curated' else mart_dataset
    ref = f'{project}.{dataset}.{name}'
    try:
      client.get_table(ref)
      client.delete_table(ref)
      print(f'  - dropped disabled object {ref}')
    except gexc.NotFound:
      pass


def refresh_materialized_tables(
    project: str,
    *,
    raw_project: str | None = None,
    location: str = DEFAULT_LOCATION,
    curated_dataset: str = DEFAULT_CURATED_DATASET,
    mart_dataset: str = DEFAULT_MART_DATASET,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    client=None,
) -> dict[str, int]:
  """Rebuilds materialized mart tables (fct_turns) and returns {table_name: row_count}."""
  from google.cloud import bigquery  # pylint: disable=import-outside-toplevel

  project = validate_project(project)
  raw_project = validate_project(raw_project or project)
  if client is None:
    client = bigquery.Client(project=project)
  sources = resolve_sources(raw_project, location, lookback_days, _bq_describer(client))
  bodies = build_view_bodies(project, curated_dataset, mart_dataset, sources)
  descriptions = _descriptions()
  job_config = bigquery.QueryJobConfig(labels=JOB_LABELS)
  counts: dict[str, int] = {}
  for name, body in bodies.items():
    short_name = name.split('.')[1]
    if short_name not in TABLES:
      continue
    _drop_if_view(client, f'{project}.{name}')
    client.query(object_ddl(project, name, body, descriptions[short_name]),
                 job_config=job_config, location=location).result()
    rows = list(client.query(f'SELECT COUNT(1) AS n FROM `{project}.{name}`',
                             job_config=job_config, location=location).result())
    counts[name] = int(rows[0]['n']) if rows else 0
  return counts


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument('--project', default=os.environ.get('GOOGLE_CLOUD_PROJECT'), help='Target project.')
  parser.add_argument('--raw-project', help='Project holding the raw log-sink datasets (default: --project).')
  parser.add_argument('--location', default=DEFAULT_LOCATION)
  parser.add_argument('--curated-dataset', default=DEFAULT_CURATED_DATASET)
  parser.add_argument('--mart-dataset', default=DEFAULT_MART_DATASET)
  parser.add_argument('--lookback-days', type=int, default=DEFAULT_LOOKBACK_DAYS,
                      help='One lookback window for every source (stage used 60/30/90).')
  parser.add_argument('--audit-lag-seconds', type=int, default=DEFAULT_AUDIT_LAG_S,
                      help='How long before an activity entry its audit entry may be (request start).')
  parser.add_argument('--audit-lead-seconds', type=int, default=DEFAULT_AUDIT_LEAD_S,
                      help='How long after an activity entry its audit entry may be.')
  parser.add_argument('--audit-only', choices=('errors', 'all'), default='errors',
                      help='Which unmatched interactive audit calls become AUDIT_ONLY turns. GE makes '
                           'several StreamAssist calls per chat turn, so "all" over-counts turns.')
  parser.add_argument('--source', action='append', default=[], metavar='KEY=PROJECT.DATASET.TABLE',
                      help=f'Override a raw source table. Keys: {", ".join(SOURCES)}.')
  parser.add_argument('--gcloud-auth', action='store_true',
                      help='Use `gcloud auth print-access-token` instead of Application Default Credentials.')
  mode = parser.add_mutually_exclusive_group()
  mode.add_argument('--print', dest='mode', action='store_const', const='print', help='Print the DDL only.')
  mode.add_argument('--print-refresh', dest='mode', action='store_const', const='print-refresh',
                    help='Print only the materialized-table rebuild DDL (for BigQuery Scheduled Queries).')
  mode.add_argument('--dry-run', dest='mode', action='store_const', const='dry-run', help='Validate (default).')
  mode.add_argument('--apply', dest='mode', action='store_const', const='apply',
                    help='Create datasets and views, and build the materialized tables.')
  mode.add_argument('--refresh', dest='mode', action='store_const', const='refresh',
                    help=f'Rebuild only the materialized tables ({", ".join(TABLES)}). Run on a schedule.')
  args = parser.parse_args(argv)
  mode_name = args.mode or 'dry-run'

  project = validate_project(args.project or '')
  raw_project = validate_project(args.raw_project or project)
  overrides = {}
  for item in args.source:
    key, _, ref = item.partition('=')
    overrides[key.strip()] = ref.strip()

  descriptions = _descriptions()
  if mode_name == 'print-refresh':
    mart_ds = validate_dataset(args.mart_dataset)
    cur_ds = validate_dataset(args.curated_dataset)
    ctx = {'curated': f'{project}.{cur_ds}', 'mart': f'{project}.{mart_ds}'}
    for layer, name, _ in OBJECTS:
      if name in TABLES:
        ds = cur_ds if layer == 'curated' else mart_ds
        view_name = f'{ds}.{name}'
        body = render(load_template(layer, name), ctx)
        print(f'-- Scheduled rebuild for {project}.{view_name}\n'
              + object_ddl(project, view_name, body, descriptions[name]) + ';')
    return 0

  from google.cloud import bigquery  # pylint: disable=import-outside-toplevel

  credentials = None
  if args.gcloud_auth:
    import subprocess  # pylint: disable=import-outside-toplevel

    from google.oauth2 import credentials as oauth_credentials  # pylint: disable=import-outside-toplevel

    env = dict(os.environ)
    existing_metrics = env.get('CLOUDSDK_METRICS_ENVIRONMENT', '').strip()
    env['CLOUDSDK_METRICS_ENVIRONMENT'] = (f'{existing_metrics} datacloud.antigravity'.strip()
                                           if existing_metrics else 'datacloud.antigravity')
    token = subprocess.run(['gcloud', 'auth', 'print-access-token'], check=True, capture_output=True,
                           text=True, env=env).stdout.strip()
    credentials = oauth_credentials.Credentials(token)
  client = bigquery.Client(project=project, credentials=credentials)
  print(f'Sources ({raw_project}, lookback {args.lookback_days}d):')
  sources = resolve_sources(raw_project, args.location, args.lookback_days, _bq_describer(client), overrides)
  for src in sources.values():
    print(f'  {src.key:18s} {src.status:14s} {src.table_ref}  [{src.detail}]')

  bodies = build_view_bodies(
      project, args.curated_dataset, args.mart_dataset, sources,
      audit_lag_s=args.audit_lag_seconds,
      audit_lead_s=args.audit_lead_seconds,
      include_audit_only_successes=args.audit_only == 'all')

  if mode_name == 'print':
    for name, body in bodies.items():
      print(f'\n-- {name}\n' + object_ddl(project, name, body, descriptions[name.split('.')[1]]) + ';')
    return 0

  job_config = bigquery.QueryJobConfig(labels=JOB_LABELS)
  if mode_name == 'dry-run':
    print('Dry run:')
    failed = False
    for name, body in bodies.items():
      sql = inline_view_refs(body, project, bodies)
      cfg = bigquery.QueryJobConfig(labels=JOB_LABELS, dry_run=True, use_query_cache=False)
      try:
        job = client.query(sql, job_config=cfg, location=args.location)
        print(f'  ok   {name:45s} ~{(job.total_bytes_processed or 0) / 1e6:.1f} MB')
      except Exception as exc:  # pylint: disable=broad-except
        failed = True
        print(f'  FAIL {name}: {exc}')
    return 1 if failed else 0

  import time  # pylint: disable=import-outside-toplevel

  if mode_name == 'refresh':
    print('Refresh:')
    targets = {n: b for n, b in bodies.items() if n.split('.')[1] in TABLES}
  else:
    print('Apply:')
    for dataset in dict.fromkeys((args.curated_dataset, args.mart_dataset)):
      _ensure_dataset(client, project, dataset, args.location)
    _drop_disabled_objects(client, project, args.curated_dataset, args.mart_dataset)
    targets = bodies
  for name, body in targets.items():
    started = time.monotonic()
    if name.split('.')[1] in TABLES:
      _drop_if_view(client, f'{project}.{name}')
    client.query(object_ddl(project, name, body, descriptions[name.split('.')[1]]),
                 job_config=job_config, location=args.location).result()
    kind = 'table' if name.split('.')[1] in TABLES else 'view'
    print(f'  + {kind:5s} {project}.{name}  ({time.monotonic() - started:.1f}s)')
  print('Row counts:')
  for name in targets:
    if name.split('.')[1] == 'v_fct_turns':
      continue  # same rows as fct_turns; counting it re-runs the slow logic
    rows = list(client.query(f'SELECT COUNT(1) AS n FROM `{project}.{name}`',
                             job_config=job_config, location=args.location).result())
    print(f'  {name:45s} {rows[0]["n"]}')
  return 0


if __name__ == '__main__':
  sys.exit(main())
