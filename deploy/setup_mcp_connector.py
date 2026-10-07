#!/usr/bin/env python3
"""Deploys VibeLift to a Gemini Enterprise Data Store and registers it to a Gemini Enterprise App instance.

IMPORTANT: VibeLift's MCP server (/mcp) and embedded side-panel UI (ui://vibelift-analytics/dashboard)
must be deployed as a **Gemini Enterprise Custom MCP Server Data Store** (`dataSource="custom_mcp"`,
`mcp_server_source="BYO_MCP"`, `use_agent_gateway_egress=False`) — **NOT** the Vertex AI / Cloud API
Registry ("Agent Registry / MCP Registry").

Once deployed to the Gemini Enterprise Data Store, the data store (`vibelift-analytics-mcp_mcp_data`)
must be **registered (linked via `dataStoreIds`) to the target Gemini Enterprise App instance**.

This script automates both stages (Console > Gemini Enterprise > Data stores > Create data store >
Custom MCP Server, followed by Console > Gemini Enterprise > Apps > <App> > Data stores):

  1. Deploys the Custom MCP Server connector to Gemini Enterprise Data Stores (collection
     `vibelift-analytics-mcp`, `mcp_server_source="BYO_MCP"`, `auth_type="NO_AUTH"`) pointing at
     `<service URL>/mcp`. Skipped if the BYO_MCP connector already exists.
  2. Waits for the Gemini Enterprise Data Store connector to become ACTIVE.
  3. Re-imports the tool list from /mcp (`refreshDataConnectorTools`).
  4. Enables the MCP tools as Gemini Enterprise actions on the Data Store (all 9 by default).
  5. Renames the Gemini Enterprise Data Store (`vibelift-analytics-mcp_mcp_data`) and **registers
     (links) it to the Gemini Enterprise App instance** (`engines/<GE_APP_ID>`), preserving the app
     instance's existing connected data stores and verifying the registration.

WARNING: this uses Discovery Engine v1alpha methods (setUpDataConnectorV2,
refreshDataConnectorTools) that are not in Google's public documentation. They were found by
inspecting the console's requests and may change. If a step fails, use the console steps in
the README instead.

Authentication: Gemini Enterprise calls the private Cloud Run service with a Google-signed ID
token for its Discovery Engine service agent (deploy_cloud_run.sh grants it roles/run.invoker).
The API still requires a non-empty `oauth_access_token` parameter for custom MCP connectors, so
this script sends a random throwaway value. It is not stored anywhere and grants access to nothing.

Usage (from the repo root):
  python3 deploy/setup_mcp_connector.py GE_APP_ID
  GE_LOCATION=us python3 deploy/setup_mcp_connector.py GE_APP_ID
  python3 deploy/setup_mcp_connector.py GE_APP_ID --dry-run
  python3 deploy/setup_mcp_connector.py --no-attach            # create the GE Data Store only
  python3 deploy/setup_mcp_connector.py --delete --collection-id=ID   # remove a connector

Needs: Discovery Engine Editor (roles/discoveryengine.editor) or higher, and gcloud logged in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_COLLECTION_ID = 'vibelift-analytics-mcp'
DEFAULT_DISPLAY_NAME = 'VibeLift Analytics'
ALL_TOOLS = (
    'open_dashboard',
    'query_ge_agent_fleet',
    'query_project_telemetry',
    'calculate_prompt_cache_economics',
    'get_vibelift_state',
    'run_alpha_evolve_generation',
    'xray_prompt_cache',
    'list_prompt_snapshot_turns',
    'set_agent_trace_logging',
)
_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,127}$')
_LOCATIONS = ('global', 'us', 'eu')


def _gcloud(*args: str) -> str:
  return subprocess.check_output(['gcloud', *args], text=True).strip()


def _check_id(value: str, what: str) -> str:
  if not _ID_RE.match(value):
    raise SystemExit(f'ERROR: invalid {what}: {value!r}')
  return value


class Api:
  """Minimal Discovery Engine v1alpha REST client using the caller's gcloud credentials."""

  def __init__(self, project_id: str, project_number: str, location: str, dry_run: bool):
    host = 'discoveryengine.googleapis.com' if location == 'global' else f'{location}-discoveryengine.googleapis.com'
    self.root = f'https://{host}/v1alpha'
    self.base = f'{self.root}/projects/{project_number}/locations/{location}'
    self.project_id = project_id
    self.dry_run = dry_run
    self._token: str | None = None

  def _auth(self) -> str:
    if self._token is None:
      self._token = _gcloud('auth', 'print-access-token')
    return self._token

  def call(self, method: str, url: str, body: dict[str, Any] | None = None, mutate: bool = False) -> tuple[int, Any]:
    if mutate and self.dry_run:
      shown = json.dumps(body, indent=1) if body is not None else ''
      print(f'[dry-run] {method} {url}\n{shown}'.rstrip())
      return 200, {}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            'Authorization': f'Bearer {self._auth()}',
            'X-Goog-User-Project': self.project_id,
            'Content-Type': 'application/json',
        },
    )
    try:
      with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
      raw = exc.read()
      try:
        return exc.code, json.loads(raw)
      except ValueError:
        return exc.code, {'raw': raw.decode(errors='replace')}


def _err(resp: Any) -> str:
  if isinstance(resp, dict):
    return str(resp.get('error', {}).get('message') or resp)[:500]
  return str(resp)[:500]


def get_connector(api: Api, collection: str) -> dict[str, Any] | None:
  status, resp = api.call('GET', f'{api.base}/collections/{collection}/dataConnector')
  if status == 404:
    return None
  if status != 200:
    raise SystemExit(f'ERROR: reading connector failed (HTTP {status}): {_err(resp)}')
  return resp


def create_connector(api: Api, collection: str, display: str, mcp_url: str, tools: list[str]) -> None:
  qs = urllib.parse.urlencode({'collectionId': collection, 'collectionDisplayName': display})
  body = {
      'dataSource': 'custom_mcp',
      'refreshInterval': '0s',
      'entities': [{'entityName': 'mcp_data'}],
      # Required by the API for custom MCP connectors even with NO_AUTH. Random, unstored, grants nothing.
      'params': {'oauth_access_token': secrets.token_urlsafe(32)},
      'actionConfig': {
          'actionParams': {
              'instance_uri': mcp_url,
              'mcp_server_source': 'BYO_MCP',
              'use_agent_gateway_egress': False,
              'tool_list': ','.join(tools),
              'auth_type': 'NO_AUTH',
          },
          'createBapConnection': True,
      },
      'connectorModes': ['ACTIONS', 'FEDERATED'],
  }
  if api.dry_run:
    body['params']['oauth_access_token'] = '<random placeholder>'
  status, resp = api.call('POST', f'{api.base}:setUpDataConnectorV2?{qs}', body, mutate=True)
  if status != 200:
    raise SystemExit(f'ERROR: creating connector failed (HTTP {status}): {_err(resp)}')


def wait_active(api: Api, collection: str, timeout_s: int = 300) -> dict[str, Any]:
  deadline = time.monotonic() + timeout_s
  while True:
    conn = get_connector(api, collection)
    state = (conn or {}).get('state')
    action_state = (conn or {}).get('actionState')
    if conn and state == 'ACTIVE' and action_state in (None, 'ACTIVE'):
      return conn
    if state == 'FAILED' or action_state == 'FAILED':
      raise SystemExit(f'ERROR: connector failed: {json.dumps((conn or {}).get("errors") or conn)[:800]}')
    if time.monotonic() > deadline:
      raise SystemExit(f'ERROR: connector not ACTIVE after {timeout_s}s (state={state}, actionState={action_state}).')
    print(f'  waiting: state={state} actionState={action_state}')
    time.sleep(10)


def refresh_tools(api: Api, collection: str) -> None:
  status, resp = api.call(
      'POST', f'{api.base}/collections/{collection}/dataConnector:refreshDataConnectorTools', {}, mutate=True)
  if status != 200:
    raise SystemExit(f'ERROR: refreshing tools failed (HTTP {status}): {_err(resp)}. Is {collection} reachable at /mcp?')


def enable_actions(api: Api, collection: str, tools: list[str]) -> None:
  body = {'bapConfig': {'supportedConnectorModes': ['ACTIONS'], 'enabledActions': tools}}
  status, resp = api.call(
      'PATCH', f'{api.base}/collections/{collection}/dataConnector?updateMask=bapConfig', body, mutate=True)
  if status != 200:
    raise SystemExit(f'ERROR: enabling actions failed (HTTP {status}): {_err(resp)}')


def rename_data_store(api: Api, data_store_id: str, display: str) -> None:
  url = f'{api.base}/collections/default_collection/dataStores/{data_store_id}?updateMask=displayName'
  status, resp = api.call('PATCH', url, {'displayName': display}, mutate=True)
  if status != 200:
    print(f'  WARNING: could not rename data store (HTTP {status}): {_err(resp)}')


def attach_to_app(api: Api, engine_id: str, data_store_id: str) -> None:
  url = f'{api.base}/collections/default_collection/engines/{engine_id}'
  status, engine = api.call('GET', url)
  if status != 200:
    raise SystemExit(f'ERROR: reading Gemini Enterprise app {engine_id} failed (HTTP {status}): {_err(engine)}')
  current = list(engine.get('dataStoreIds') or [])
  if data_store_id in current:
    print(f'  already registered (linked) to Gemini Enterprise app instance {engine_id} (dataStoreId={data_store_id})')
    return
  # PATCH replaces the whole list, so send the existing data stores plus the new one.
  status, resp = api.call('PATCH', f'{url}?updateMask=dataStoreIds',
                          {'dataStoreIds': [*current, data_store_id]}, mutate=True)
  if status != 200:
    raise SystemExit(f'ERROR: linking data store to app failed (HTTP {status}): {_err(resp)}')
  if not api.dry_run:
    v_status, v_engine = api.call('GET', url)
    if v_status == 200 and data_store_id not in (v_engine.get('dataStoreIds') or []):
      raise SystemExit(
          f'ERROR: data store {data_store_id} was not persisted in dataStoreIds for Gemini Enterprise app {engine_id}.'
      )
  print(
      f'  registered GE Data Store {data_store_id} to Gemini Enterprise app instance {engine_id} '
      f'(data stores: {len(current)} -> {len(current) + 1})'
  )


def delete_connector(api: Api, collection: str) -> None:
  """Deletes the connector's collection and its data store (for tests and clean-up)."""
  data_store_id = f'{collection}_mcp_data'
  status, resp = api.call('DELETE', f'{api.base}/collections/{collection}', mutate=True)
  if status not in (200, 404):
    raise SystemExit(f'ERROR: deleting collection failed (HTTP {status}): {_err(resp)}')
  op = resp.get('name') if isinstance(resp, dict) else None
  if status == 200 and op and not resp.get('done') and not api.dry_run:
    for _ in range(60):
      time.sleep(5)
      s, o = api.call('GET', f'{api.root}/{op}')
      if s != 200 or o.get('done'):
        if o.get('error'):
          raise SystemExit(f'ERROR: delete operation failed: {_err(o)}')
        break
  # Deleting the collection normally removes the data store; delete it explicitly if it is still there.
  ds_url = f'{api.base}/collections/default_collection/dataStores/{data_store_id}'
  if not api.dry_run and api.call('GET', ds_url)[0] == 200:
    status, resp = api.call('DELETE', ds_url, mutate=True)
    if status not in (200, 404):
      raise SystemExit(f'ERROR: deleting data store {data_store_id} failed (HTTP {status}): {_err(resp)}')
  print(f'Deleted connector {collection} and data store {data_store_id}.')


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument('ge_app_id', nargs='?', default=os.environ.get('GE_ENGINE_ID', ''),
                      help='Gemini Enterprise app (engine) ID to register/link the GE Data Store to.')
  parser.add_argument('--location', default=os.environ.get('GE_LOCATION', 'global'), choices=_LOCATIONS,
                      help='Same multi-region as the Gemini Enterprise app (default: $GE_LOCATION or global).')
  parser.add_argument('--collection-id', default=DEFAULT_COLLECTION_ID)
  parser.add_argument('--display-name', default=DEFAULT_DISPLAY_NAME)
  parser.add_argument('--tools', default=','.join(ALL_TOOLS),
                      help='Comma-separated MCP tools to enable as actions (default: all 9).')
  parser.add_argument('--mcp-url', default='', help='Default: ${VIBELIFT_PUBLIC_URL or Cloud Run URL}/mcp')
  parser.add_argument('--no-attach', action='store_true', help='Create the GE Data Store only; do not link to an app.')
  parser.add_argument('--delete', action='store_true', help='Delete the connector and its data store.')
  parser.add_argument('--dry-run', action='store_true', help='Print the write requests without sending them.')
  args = parser.parse_args(argv)

  project_id = os.environ.get('GOOGLE_CLOUD_PROJECT') or _gcloud('config', 'get-value', 'project')
  if not project_id:
    raise SystemExit('ERROR: set GOOGLE_CLOUD_PROJECT or run gcloud config set project PROJECT_ID.')
  project_number = _gcloud('projects', 'describe', project_id, '--format=value(projectNumber)')
  collection = _check_id(args.collection_id, 'collection id')
  api = Api(project_id, project_number, args.location, args.dry_run)

  if args.delete:
    delete_connector(api, collection)
    return

  region = os.environ.get('GOOGLE_CLOUD_REGION', 'us-central1')
  service = os.environ.get('SERVICE_NAME', 'vibe-lift-agent')
  public_url = os.environ.get('VIBELIFT_PUBLIC_URL') or f'https://{service}-{project_number}.{region}.run.app'
  mcp_url = args.mcp_url or f'{public_url.rstrip("/")}/mcp'
  tools = [t.strip() for t in args.tools.split(',') if t.strip()]
  unknown = sorted(set(tools) - set(ALL_TOOLS))
  if unknown:
    raise SystemExit(f'ERROR: unknown tools {unknown}; choose from {", ".join(ALL_TOOLS)}')
  if not args.no_attach:
    if not args.ge_app_id:
      raise SystemExit('ERROR: pass the Gemini Enterprise app ID (or use --no-attach).')
    _check_id(args.ge_app_id, 'Gemini Enterprise app id')
  data_store_id = f'{collection}_mcp_data'

  print(f'Project {project_id} ({project_number}), location {args.location}')
  print(f'GE Data Store Connector: {collection} (dataStoreId: {data_store_id}, source: BYO_MCP) -> {mcp_url}')
  print(f'Target GE App Instance:  {args.ge_app_id or "(none, --no-attach)"}')
  print(f'Actions: {", ".join(tools)}')

  print('1. Deploy Custom MCP Server to Gemini Enterprise Data Store (BYO_MCP, NOT Agent Registry / MCP Registry)')
  existing = get_connector(api, collection)
  if existing:
    action_params = (existing.get('actionConfig') or {}).get('actionParams') or {}
    src = action_params.get('mcp_server_source', 'BYO_MCP')
    if src != 'BYO_MCP':
      raise SystemExit(
          f'ERROR: existing connector {collection} has mcp_server_source={src!r}. '
          'VibeLift must be deployed as a Gemini Enterprise Custom MCP Server Data Store (mcp_server_source="BYO_MCP"), '
          'NOT via the Agent Registry / MCP Registry. Delete and re-create it with: '
          f'python3 deploy/setup_mcp_connector.py --delete --collection-id={collection}'
      )
    print(f'  exists (state={existing.get("state")}, source={src}); not re-creating')
    uri = action_params.get('instance_uri')
    if uri and uri != mcp_url:
      print(f'  WARNING: existing connector points at {uri}, not {mcp_url}')
  else:
    create_connector(api, collection, args.display_name, mcp_url, tools)
    print('  created in Gemini Enterprise Data Stores (source=BYO_MCP)')

  if args.dry_run:
    print('2-3. [dry-run] would wait for ACTIVE and refresh tools')
  else:
    print('2. Waiting for GE Data Store connector to become ACTIVE')
    wait_active(api, collection)
    print('3. Importing tools from /mcp into GE Data Store')
    refresh_tools(api, collection)
    conn = wait_active(api, collection)
    found = {t.get('name') for t in conn.get('dynamicTools') or []}
    missing = [t for t in tools if t not in found]
    if missing:
      raise SystemExit(f'ERROR: /mcp did not report tools {missing} (found: {sorted(found)}). '
                       'Check that the service is deployed and the Discovery Engine service agent can invoke it.')

  print('4. Enabling MCP actions on GE Data Store')
  enable_actions(api, collection, tools)

  print('5. Register (link) GE Data Store to Gemini Enterprise App instance')
  rename_data_store(api, data_store_id, args.display_name)
  if args.no_attach:
    print('  --no-attach: not linking GE Data Store to an app instance')
  else:
    attach_to_app(api, args.ge_app_id, data_store_id)

  if not args.dry_run:
    conn = get_connector(api, collection) or {}
    enabled = (conn.get('bapConfig') or {}).get('enabledActions') or []
    print(f'Done. state={conn.get("state")} actionState={conn.get("actionState")} enabled={enabled}')
    print('In Gemini Enterprise, ask: "Open the VibeLift dashboard".')


if __name__ == '__main__':
  main(sys.argv[1:])

