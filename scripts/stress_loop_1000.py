#!/usr/bin/env python3
"""10,000+ Iteration Deep In-Memory Chaos, Property, Concurrency & Error-Contract Verification Loop.

Hermetic by construction: a socket guard records (and blocks) any attempted network connection for
the whole run, and the controller's GCP collaborators are stubbed in memory. A run with a recorded
network attempt FAILS, so the loop can never silently become credential-dependent or billable.

Fuzz values are drawn from per-slot shuffled decks (never from `i % n`), so every fuzzed slot is
guaranteed to see every edge value once per cycle; the report publishes the achieved coverage and a
slot that completed a cycle without seeing every value FAILS the run.

Every Python iteration runs ONE top-level bucket check and ONE deep pillar check:
  Bucket 1: normal happy paths through the production FastAPI app (same registration order as Cloud Run)
  Bucket 2: edge-case / boundary fuzzing with deck-drawn values
  Bucket 3: user / input validation errors, including MISSING fields (nothing may default to a passing score)
  Bucket 4: live-mode policy table - every mutating POST route is classified guarded/allowed and checked
  Bucket 5: VL-CODE-500 fault injection through the production app + the Cloud Logging correlation record

  Pillar 1: FinOps mathematical conservation & invariant fuzzing (spend drift, session drilldown, user costs)
  Pillar 2: Prompt Cache X-Ray property-based testing (heatmap char conservation, rewrite monotonicity)
  Pillar 3: MCP JSON-RPC 2.0: all 9 tools, VL-MCP-500, and the REAL POST /mcp HTTP route (parse errors,
            batches, unknown method/tool, session header, DELETE 204 / GET 405)
  Pillar 4: ADK agent (app/agent.py): all 10 tool functions, direct & edge-case fuzzing
  Pillar 5: NL2SQL AST verifier, adversarial SQL corpus, FinOps AST scanner & BigQuery/billing error translation
  Pillar 6: BigQuery ge_mart row mappers, PII redaction & billing export join conservation
  Pillar 7: Telemetry grounding validator (deterministic checks; an LLM judge verdict can never override a
            failed deterministic check) & 30-check SME rubric
  Pillar 8: REAL HTTP-level concurrency (8 threads of interleaved mutations/reads) + DLQ race conditions
  Pillar 9: Node.js: VL.* pure-function fuzzing (CSV formula / shell / URL sanitizers, classifier) AND the
            real renderState / triggerApi / isNonStateErrorPayload executed in a ghost DOM (tests/ui_dom_harness.js)
  Pillar 10: Full-stack internal code error contracts (VL-CODE-500, VL-MCP-500, VL-UI-RUNTIME) & state preservation
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import math
import os
import pathlib
import random
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent import futures
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from app import agent as adk_agent
from finops_dashboard import (
    billing_export,
    finops,
    ge_mart,
    mcp_server,
    optimizer,
    prompt_xray,
    server,
    sme_eval,
    telemetry,
)
from finops_dashboard import validator as telemetry_validator

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENHANCEMENTS_JS_PATH = REPO_ROOT / 'finops_dashboard' / 'ui' / 'static' / 'dashboard_enhancements.js'
GHOST_DOM_HARNESS_PATH = REPO_ROOT / 'tests' / 'ui_dom_harness.js'

WEIRD_NUMBERS: list[Any] = [
    0, -1, -999999.99, 1e12, 'nan', 'inf', '-inf', 'abc', '', None, True, False, [], {}, '0.00001',
]

# Every mutating POST route registered on the app MUST be classified here. 'guarded' routes must answer
# DISABLED_IN_LIVE_MODE when connected to a live GCP project; 'allowed' routes must never answer that.
# A new POST route that is missing from this table fails the loop at startup.
LIVE_MODE_POLICY: dict[str, str] = {
    # Synthetic simulator mutations: must fail closed on live telemetry.
    '/api/inject_anomaly': 'guarded',
    '/api/step_turn': 'guarded',
    '/api/evolve_generation': 'guarded',
    '/api/what_if_simulate': 'guarded',
    '/api/recompute_finops': 'guarded',
    '/api/reset': 'guarded',
    '/api/add_parameter': 'guarded',
    # Legitimate in live mode (view switches, real ingestion, live projections, admin / refresh actions).
    '/api/select_agent': 'allowed',
    '/api/select_optimizer': 'allowed',
    '/api/decorator_ingest': 'allowed',
    '/api/aive_log': 'allowed',
    '/api/csat_rating': 'allowed',
    '/api/what_if_live': 'allowed',
    '/api/prompt_xray': 'allowed',
    '/api/nl2sql': 'allowed',
    '/api/sme_eval/run': 'allowed',
    '/api/sme_eval/rate': 'allowed',
    '/api/validate_telemetry': 'allowed',
    '/api/ge_mart/refresh': 'allowed',
    '/api/sync_gcp_telemetry': 'allowed',
    '/api/sync_ge_fleet': 'allowed',
    '/api/agent_observability': 'allowed',
    '/api/enable_agent_observability': 'allowed',
    '/a2a/app': 'allowed',
    '/mcp': 'allowed',
}
LIVE_POLICY_BODIES: dict[str, Any] = {
    '/api/add_parameter': {'label': 'LiveAdd', 'baseline_val': 1, 'target_val': 2, 'weight_pct': 5},
    '/api/what_if_simulate': {'traffic_canary_pct': 20},
    '/api/recompute_finops': {'calls_per_hr': 500},
    '/api/select_agent': {'agent_id': 'it_service_desk'},
    '/api/select_optimizer': {'platform_id': 'alpha_evolve'},
    '/api/validate_telemetry': {'run_llm_judge': False},
    '/api/nl2sql': {'question': 'Show cost by agent'},
    '/api/sme_eval/rate': {'persona_id': 'cfo_exec', 'overall_rating': 4, 'verdict': 'APPROVED'},
    '/a2a/app': {'jsonrpc': '2.0', 'id': 'live-1', 'method': 'message/send', 'params': {'message': {'parts': [{'text': 'hi'}]}}},
    '/mcp': {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
}


class FuzzDeck:
  """Per-slot shuffled decks of edge values.

  `draw(slot)` pops from a slot-specific permutation and reshuffles when exhausted, so every slot sees
  every value exactly once per `len(values)` visits regardless of how the caller schedules iterations.
  This replaces `values[i % len(values)]`, which aliased with the bucket/sub-case modulo arithmetic and
  silently limited some endpoints to 3 of 15 values.
  """

  def __init__(self, values: list[Any], rng: random.Random) -> None:
    self._values = list(values)
    self._rng = rng
    self._decks: dict[str, list[Any]] = {}
    self.visits: collections.Counter[str] = collections.Counter()
    self.seen: dict[str, set[str]] = collections.defaultdict(set)

  def draw(self, slot: str) -> Any:
    deck = self._decks.get(slot)
    if not deck:
      deck = self._rng.sample(self._values, len(self._values))
      self._decks[slot] = deck
    value = deck.pop()
    self.visits[slot] += 1
    self.seen[slot].add(repr(value))
    return value

  def coverage_report(self) -> dict[str, dict[str, int]]:
    total = len(self._values)
    return {
        slot: {'visits': self.visits[slot], 'distinct': len(self.seen[slot]), 'of': total}
        for slot in sorted(self.visits)
    }

  def gaps(self) -> list[str]:
    """Slots that completed at least one full cycle without seeing every value (must be empty)."""
    total = len(self._values)
    return [
        f'{slot}: saw {len(self.seen[slot])}/{total} values in {self.visits[slot]} visits'
        for slot in sorted(self.visits)
        if self.visits[slot] >= total and len(self.seen[slot]) < total
    ]


class NetworkGuard:
  """Records and blocks every attempt to open a network connection while the loop runs."""

  def __init__(self) -> None:
    self.attempts: list[str] = []
    self._patchers: list[Any] = []

  def _blocker(self, name: str):
    is_method = name.startswith('socket.socket.')

    def blocked(*args: Any, **kwargs: Any) -> Any:
      if is_method:
        target = args[1] if len(args) > 1 else kwargs.get('address')
      else:
        target = args[0] if args else (kwargs.get('address') or kwargs.get('host'))
      frames = [
          f'{pathlib.Path(fr.filename).name}:{fr.lineno}:{fr.name}'
          for fr in traceback.extract_stack(limit=40)
          if '/finops_dashboard/' in fr.filename or '/app/' in fr.filename or 'site-packages/google' in fr.filename
      ]
      self.attempts.append(f'{name}({target!r}) via {" <- ".join(frames[-6:]) or "?"}'[:400])
      raise OSError(f'[hermetic stress loop] network access blocked: {name}')
    return blocked

  def __enter__(self) -> NetworkGuard:
    for name in ('connect', 'connect_ex'):
      patcher = mock.patch.object(socket.socket, name, self._blocker(f'socket.socket.{name}'))
      patcher.start()
      self._patchers.append(patcher)
    for name in ('create_connection', 'getaddrinfo'):
      patcher = mock.patch.object(socket, name, self._blocker(f'socket.{name}'))
      patcher.start()
      self._patchers.append(patcher)
    return self

  def __exit__(self, *exc: Any) -> bool:
    for patcher in reversed(self._patchers):
      patcher.stop()
    self._patchers.clear()
    return False


class _RecordCapture(logging.Handler):
  """Captures ERROR records so the VL-CODE-500 -> Cloud Logging correlation promise can be asserted."""

  def __init__(self) -> None:
    super().__init__(level=logging.ERROR)
    self.records: list[logging.LogRecord] = []

  def emit(self, record: logging.LogRecord) -> None:
    self.records.append(record)


def _fake_llm_judge(**_kwargs: Any) -> dict[str, Any]:
  """Deterministic stand-in for the Vertex AI judge: always claims everything is grounded.

  Used to prove the judge can never override failed deterministic checks, and to keep the loop offline.
  """
  return {
      'executed': True,
      'judge_model': 'stub-judge (stress loop)',
      'judge_model_used': 'stub-judge (stress loop)',
      'grounding_score_100': 100,
      'verdict': 'VERIFIED_GROUNDED',
      'executive_finding': 'Stub judge claims everything is grounded.',
      'executive_summary': 'Stub judge claims everything is grounded.',
      'tab_findings': [],
  }


def _configure_in_memory_controller() -> server.FinOpsRuntimeController:
  """Creates a FinOpsRuntimeController isolated from live GCP network calls for fast in-memory testing.

  The project/region env vars are set for the constructor: without them `GoogleCloudTelemetryService`
  probes the GCE metadata server (two 1 s timeouts) before any mock can be applied.
  """
  with mock.patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': 'demo-offline-project', 'GOOGLE_CLOUD_REGION': 'us-central1'}):
    ctrl = server.FinOpsRuntimeController()
  ctrl._is_live_gcp = mock.MagicMock(return_value=False)  # type: ignore[method-assign]
  ctrl.gcp_telemetry.project_id = 'demo-offline-project'
  ctrl.gcp_telemetry.is_live_connected = False
  # Live NL2SQL executes through the BigQuery REST API; in the loop it answers an empty, well-formed result.
  ctrl.gcp_telemetry.run_bigquery_query = mock.MagicMock(
      return_value=([], {'job_id': 'stress-loop-job', 'total_bytes_processed': 0, 'cache_hit': True})
  )
  ctrl.gcp_telemetry.list_cloud_run_agent_services = mock.MagicMock(
      return_value=[{
          'service_name': 'finops-analytics-agent',
          'region': 'us-central1',
          'status': 'READY',
          'latest_revision': 'finops-analytics-agent-00012-xyz',
      }]
  )
  ctrl.gcp_telemetry.fetch_gemini_enterprise_support_telemetry = mock.MagicMock(
      return_value=[{
          'ticket_id': 'SUP-101',
          'event_id': 'evt-101',
          'timestamp': '2026-10-11T00:00:00Z',
          'tier': 'L1 Observed',
          'agent_id': 'it_service_desk',
          'status': 'SUCCESS',
      }]
  )
  ctrl.gcp_telemetry.fetch_live_bigquery_project_insights = mock.MagicMock(return_value=None)
  ctrl.gcp_telemetry.refresh_ge_mart_turns = mock.MagicMock(
      return_value={'status': 'OK', 'mode': 'incremental', 'rows_affected': 42}
  )
  ctrl.gcp_telemetry.get_telemetry_summary_payload = mock.MagicMock(
      return_value={
          'status': 'DEMO',
          'project_id': 'demo-offline-project',
          'region': 'us-central1',
          'services_count': 2,
          'live_turns_ingested': 12,
          'services': [],
          'gemini_enterprise_support_events': [],
          'summary_stats': {'total_prompt_tokens': 120000, 'overall_cache_hit_ratio': 68.5},
      }
  )
  demo_fleet = {
      'status': 'DEMO',
      'project_id': 'demo-offline-project',
      'window_hours': 24,
      'agents': [
          {
              'agent_id': 'it_service_desk',
              'display_name': 'IT Service Desk',
              'engine_id': 'eng-1',
              'type': 'ADK',
              'backend': {'models': ['gemini-2.5-pro']},
              'metrics': {
                  'requests': 120,
                  'llm_calls': 120,
                  'input_tokens': 500_000,
                  'output_tokens': 80_000,
                  'cached_tokens': 200_000,
                  'reasoning_tokens': 20_000,
                  'est_token_cost_usd': 1.65,
              },
          }
      ],
      'models': [],
      'model_usage': {
          'window_hours': 24,
          'rate_cards': ctrl.ge_fleet.rate_cards(),
          'totals': {'est_cost_usd': 1.65},
          'models': [
              {
                  'model': 'gemini-2.5-pro',
                  'invocations': 120,
                  'input_tokens': 500_000,
                  'output_tokens': 80_000,
                  'reasoning_tokens': 20_000,
                  'cache_read_tokens': 200_000,
                  'cache_write_tokens': 0,
                  'est_cost_usd': 1.65,
              }
          ],
      },
      'totals': {'agents': 1, 'requests': 120, 'llm_calls': 120, 'input_tokens': 500_000, 'output_tokens': 80_000},
  }
  ctrl.ge_fleet.get_fleet_snapshot = mock.MagicMock(return_value=demo_fleet)
  ctrl.ge_fleet.collect = mock.MagicMock(return_value=demo_fleet)
  ctrl.billing_export.get_daily_ai_costs = mock.MagicMock(
      return_value={'status': 'NOT_CONFIGURED', 'days': {}}
  )
  ctrl.billing_export.get = mock.MagicMock(
      return_value={'status': 'NOT_CONFIGURED', 'days': {}}
  )
  # Behind POST /api/sync_gcp_telemetry: previously unmocked, i.e. a real Cloud Logging call.
  ctrl.gcp_telemetry.fetch_live_cloud_turns = mock.MagicMock(return_value=[])
  return ctrl


def run_ghost_dom_real_renderer_suite(
    rendered_html: str,
    good_state: dict[str, Any],
    iterations: int = 3,
) -> dict[str, Any]:
  """Executes the REAL dashboard scripts (renderState / triggerApi / isNonStateErrorPayload) in a ghost DOM."""
  with tempfile.TemporaryDirectory(prefix='finops-ghost-dom-') as tmp:
    html_path = pathlib.Path(tmp) / 'dashboard.html'
    state_path = pathlib.Path(tmp) / 'good_state.json'
    html_path.write_text(rendered_html, encoding='utf-8')
    state_path.write_text(json.dumps(good_state), encoding='utf-8')
    proc = subprocess.run(
        ['node', str(GHOST_DOM_HARNESS_PATH), str(html_path), str(state_path), str(max(1, int(iterations)))],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
  lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip().startswith('{')]
  if not lines:
    raise RuntimeError(f'ghost-DOM harness produced no report (exit {proc.returncode}): {proc.stderr[-1500:]}')
  report = json.loads(lines[-1])
  if proc.returncode != 0 or not report.get('ok'):
    raise RuntimeError(f"ghost-DOM harness failed: {report.get('failures')}")
  return report


def run_node_ui_error_and_fuzz_suite(iterations: int = 2000) -> dict[str, Any]:
  """Runs `iterations` of pure VL.* fuzzing in Node.js (calculators, sanitizers, and the real error classifier).

  State-preservation of the page itself is NOT simulated here any more; that is verified against the real
  `renderState` / `triggerApi` by `run_ghost_dom_real_renderer_suite`.
  """
  js_path_str = json.dumps(str(ENHANCEMENTS_JS_PATH))
  node_harness = f"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync({js_path_str}, 'utf8');
vm.runInThisContext(code, {{ filename: 'dashboard_enhancements.js' }});
assert(typeof VL === 'object' && VL !== null, 'VL must be defined');
assert(typeof VL.classifyError === 'function', 'VL.classifyError must be exported');

const TOTAL = {int(iterations)};
let passed = 0;
const counts = {{
  ui_happy: 0,
  ui_edge_fuzz: 0,
  ui_validation_err: 0,
  ui_live_guard: 0,
  ui_code_bug: 0,
  ui_security_sanitizers: 0,
}};
let lastBanner = null;
let lastInfo = null;

function classify(endpoint, httpStatus, data, jsErr) {{
  const info = VL.classifyError(endpoint, httpStatus, data, jsErr);
  lastBanner = info.bannerText;
  lastInfo = info;
  return info;
}}

const edgeValues = [null, undefined, NaN, Infinity, -Infinity, true, false, '', 'abc', 'NaN', 'Infinity', -999999, 0, 1e15, {{}}, []];
const maliciousProjects = [
  "my-proj'; rm -rf /; echo '",
  "proj$(whoami)",
  "proj`id`",
  "proj\\nFINOPS_EOF\\nmalicious_cmd",
  "proj\\nFINOPS_EOF_1\\nFINOPS_EOF_2",
];
const badUrls = [
  "javascript:alert(document.cookie)",
  "JAVASCRIPT:eval('1')",
  "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)",
  "file:///etc/passwd",
  "ftp://internal.corp/secret",
  "",
  null,
  undefined,
];

for (let i = 0; i < TOTAL; i++) {{
  const mode = i % 5;
  if (mode === 0) {{
    // 1. Happy path pure calculators + VL.health + VL.alertPolicies + VL.toCsv + VL.safeExternalUrl
    const cs = VL.costSummary(
      [
        {{day: '2026-10-06', ai_net_usd: 110, interactions: 380}},
        {{day: '2026-10-07', ai_net_usd: 115, interactions: 390}},
        {{day: '2026-10-08', ai_net_usd: 120, interactions: 400}},
        {{day: '2026-10-09', ai_net_usd: 140, interactions: 500}},
      ],
      10000
    );
    const br = VL.budgetRunway(cs);
    assert(br && typeof br.projection === 'number' && isFinite(br.projection));
    const th = VL.thinkingTax({{
      window_hours: 24,
      models: [{{model: 'gemini-2.5-pro', invocations: 100, input_tokens: 500000, output_tokens: 55000, reasoning_tokens: 45000, est_cost_usd: 2.5}}],
    }}, 24);
    assert(th && th.reasoningTokens === 45000 && th.thinkingShareOfOutputPct === 45);
    const hl = VL.health({{
      state: 'ENABLED',
      display_name: 'A1',
      metrics: {{requests: 200, errors_5xx: 1, errors_4xx: 2, latency_p95_ms: 850, llm_calls: 240}},
    }});
    assert(hl && (hl.id === 'healthy' || hl.id === 'degraded' || hl.id === 'failing'));
    assert(VL.safeExternalUrl('https://console.cloud.google.com/logs') === 'https://console.cloud.google.com/logs');
    counts.ui_happy++;
    passed++;
  }} else if (mode === 1) {{
    // 2. Edge-case fuzzing + security sanitizers (CSV formula injection, shell/heredoc breakout, URL sanitizer)
    const v1 = edgeValues[i % edgeValues.length];
    const v2 = edgeValues[(i * 3 + 1) % edgeValues.length];
    const wf = VL.driftWaterfall({{
      status: 'LIVE',
      cost_before_usd: 100,
      cost_now_usd: 100,
      change_pct: 0,
      drivers: [
        {{driver_id: 'volume', name: 'Traffic Volume', change_usd: 0, share_of_change_pct: null}},
        {{driver_id: 'mix', name: 'Model Mix', change_usd: 0, share_of_change_pct: null}},
      ],
      by_model: [
        {{model: 'gemini-2.5-pro', cost_before_usd: 60, cost_now_usd: 30, drivers_usd: {{volume: -30, mix: 0}}}},
        {{model: 'gemini-2.5-flash', cost_before_usd: 40, cost_now_usd: 70, drivers_usd: {{volume: 30, mix: 0}}}},
      ]
    }});
    assert(wf && Array.isArray(wf.steps) && wf.steps.length === 4);
    assert(wf.change === 0 && isFinite(wf.maxUsd));

    const fuzzMu = {{
      window_hours: v1,
      models: [{{model: String(v2), invocations: v1, input_tokens: v1, output_tokens: v1, reasoning_tokens: v2, est_cost_usd: v1}}],
    }};
    const fuzzTh = VL.thinkingTax(fuzzMu, v1);
    assert(fuzzTh && isFinite(fuzzTh.thinkingShareOfOutputPct));
    const fuzzFleet = {{
      window_hours: v1,
      model_usage: fuzzMu,
      agents: [{{display_name: 'FuzzAgent', backend: {{models: ['gemini-2.5-pro']}}, metrics: {{input_tokens: v2, cached_tokens: v1, output_tokens: v2, reasoning_tokens: 0}}}}],
    }};
    const fuzzRp = VL.modelRepricing(fuzzFleet);
    assert(fuzzRp && Array.isArray(fuzzRp.byModel) && Array.isArray(fuzzRp.byAgent));
    const fuzzSr = VL.sessionReceipt({{session_id: 's1', input_tokens: v1, output_tokens: v2, cached_input_tokens: 999999999, reasoning_tokens: 999999999}}, [{{input_tokens: v1, output_tokens: v2}}]);
    assert(fuzzSr && fuzzSr.cachedInputTokens <= fuzzSr.totalInputTokens);
    assert(fuzzSr.reasoningTokens <= fuzzSr.totalOutputTokens);

    // Security sanitizer checks on every edge iteration
    const malProj = maliciousProjects[i % maliciousProjects.length];
    const sampleAgent = {{
      display_name: 'Agent\\nFINOPS_POLICY\\nmalicious',
      backend: {{kind: 'cloud_run', service: 'finops-svc', region: 'us-central1'}},
      metrics: {{requests: 200}},
    }};
    const rejectedPol = VL.alertPolicies(sampleAgent, {{project: malProj}});
    assert(rejectedPol && rejectedPol.ok === false, 'Malicious project ID must be rejected by PROJECT_ID_RE');
    const validPol = VL.alertPolicies(sampleAgent, {{project: 'valid-project-123', include: {{errors: true, latency: true, absence: true}}}});
    assert(validPol && validPol.ok === true && Array.isArray(validPol.policies) && validPol.policies.length === 3);
    assert(!validPol.script.includes('\\nFINOPS_POLICY\\nmalicious'), 'Newlines in agent name must be stripped by oneLine');

    const csvOut = VL.toCsv(['c1', 'c2', 'c3', 'c4', 'c5', 'c6'], [['=cmd|calc', '+SUM(1,1)', '-2+3', '@import', '\\t=evil', 'normal']]);
    assert(csvOut.includes("'=cmd|calc") && csvOut.includes("'+SUM(1,1)") && csvOut.includes("'@import"));
    const badUrl = badUrls[i % badUrls.length];
    assert(VL.safeExternalUrl(badUrl) === null);

    counts.ui_edge_fuzz++;
    counts.ui_security_sanitizers++;
    passed++;
  }} else if (mode === 2) {{
    // 3. User validation error classification (real VL.classifyError)
    const info = classify('/api/add_parameter', 422, {{
      status: 'ERROR',
      error_kind: 'VALIDATION_ERROR',
      error_code: 'VL-INPUT-422',
      is_code_bug: false,
      state_preserved: true,
      errors: ['baseline_val: must be a finite number'],
      what_to_do: 'Provide finite numbers for baseline_val, target_val, and weight_pct.'
    }}, null);
    assert(info && info.kind === 'VALIDATION_ERROR');
    assert(info.isCodeBug === false);
    assert(info.statePreserved === true);
    assert(info.bannerText.includes('VL-INPUT-422'));
    assert(info.bannerText.includes('baseline_val: must be a finite number'));
    // Dead-letter ingest rejections are user errors too, never code bugs.
    const dl = classify('/api/decorator_ingest', 422, {{error: 'invalid_payload', errors: ['agent_name: required'], dead_letter: {{source: '/api/decorator_ingest', values_logged: false}}}}, null);
    assert(dl && dl.isCodeBug === false && typeof dl.code === 'string' && dl.code.length > 0);
    counts.ui_validation_err++;
    passed++;
  }} else if (mode === 3) {{
    // 4. Live-mode safety guardrail + upstream GCP notices (real VL.classifyError)
    const info = classify('/api/inject_anomaly', 200, {{
      status: 'DISABLED_IN_LIVE_MODE',
      reason: 'Anomaly injection is part of the demo simulator and is disabled when viewing live GCP telemetry.'
    }}, null);
    assert(info && info.kind === 'DISABLED_IN_LIVE_MODE');
    assert(info.code === 'VL-LIVE-GUARD');
    assert(info.isCodeBug === false);
    assert(lastBanner.includes('VL-LIVE-GUARD'));
    const gcp = classify('/api/ge_fleet', 200, {{status: 'PERMISSION_DENIED', user_message: 'BigQuery access denied', what_to_do: 'Grant roles/bigquery.dataViewer'}}, null);
    assert(gcp.kind === 'UPSTREAM_GCP_STATUS' && gcp.code === 'VL-GCP-PERMISSION_DENIED' && gcp.isCodeBug === false);
    const net = classify('/api/state', 0, null, new TypeError('Failed to fetch'));
    assert(net.kind === 'NETWORK_ERROR' && net.code === 'VL-NET-ERR' && net.isCodeBug === false);
    const http = classify('/api/state', 503, null, null);
    assert(http.code === 'VL-HTTP-503' && http.isCodeBug === false);
    counts.ui_live_guard++;
    passed++;
  }} else {{
    // 5. Internal code error classification (VL-CODE-500 and VL-UI-RUNTIME)
    const reqId = 'req-test-' + i;
    const srvInfo = classify('/api/recompute_finops', 500, {{
      status: 'ERROR',
      error_kind: 'INTERNAL_CODE_ERROR',
      error_code: 'VL-CODE-500',
      is_code_bug: true,
      state_preserved: true,
      where: 'POST /api/recompute_finops',
      request_id: reqId,
      exception_type: 'ZeroDivisionError',
      user_message: 'FinOps Dashboard hit an internal code error (ZeroDivisionError) while handling POST /api/recompute_finops.',
      what_to_do: 'Share Request ID ' + reqId + ' with the platform engineering team.',
      diagnostic_cmd: 'gcloud logging read ...'
    }}, null);
    assert(srvInfo && srvInfo.kind === 'INTERNAL_CODE_ERROR');
    assert(srvInfo.code === 'VL-CODE-500');
    assert(srvInfo.isCodeBug === true);
    assert(srvInfo.statePreserved === true);
    assert(srvInfo.requestId === reqId);
    assert(srvInfo.exceptionType === 'ZeroDivisionError');
    assert(srvInfo.bannerText.includes('Code Bug'));
    assert(srvInfo.bannerText.includes(reqId));

    const jsInfo = VL.classifyError('VL.onState.spend', 0, null, new TypeError('Cannot read properties of null'));
    assert(jsInfo.kind === 'INTERNAL_CODE_ERROR');
    assert(jsInfo.code === 'VL-UI-RUNTIME');
    assert(jsInfo.isCodeBug === true);
    assert(jsInfo.exceptionType === 'TypeError');
    counts.ui_code_bug++;
    passed++;
  }}
}}

console.log(JSON.stringify({{ total: TOTAL, passed, counts }}));
"""
  proc = subprocess.run(
      ['node', '-'],
      input=node_harness,
      capture_output=True,
      text=True,
      check=False,
      timeout=30,
  )
  if proc.returncode != 0:
    raise RuntimeError(f'Node UI stress harness failed (exit {proc.returncode}): {proc.stderr}')
  return json.loads(proc.stdout.strip().splitlines()[-1])

def _call_mcp(body: Any, session_id: str | None = 's1') -> tuple[dict[str, Any], dict[str, str]]:
  """Helper to invoke mcp_server.handle_jsonrpc_sync and parse JSON response."""
  status_code, headers, raw_body = mcp_server.handle_jsonrpc_sync(body, session_id=session_id)
  assert status_code == 200
  return json.loads(raw_body), headers


class _LoopContext:
  """Everything a bucket / pillar check needs (built once per run)."""

  def __init__(
      self,
      rng: random.Random,
      ctrl: server.FinOpsRuntimeController,
      base_state: dict[str, Any],
      client: TestClient,
      thread_clients: list[TestClient],
      deck: FuzzDeck,
      log_capture: _RecordCapture,
  ) -> None:
    self.rng = rng
    self.ctrl = ctrl
    self.base_state = base_state
    self.client = client
    self.thread_clients = thread_clients
    self.deck = deck
    self.log_capture = log_capture
    self.agents = [a['agent_id'] for a in ctrl.optimizer.list_agents_summary()]
    self.platforms = ['alpha_evolve', 'opus_critic', 'hybrid_ensemble']
    self.models = sorted(m for m in telemetry.RATE_CARDS if m.startswith('gemini'))
    self.personas = ['finops_lead', 'sre_platform', 'ai_engineer', 'product_quality', 'security_governance', 'cfo_exec']
    self.pillar_counts: dict[str, int] = {
        'p1_finops_math_conservation': 0,
        'p2_prompt_xray_properties': 0,
        'p3_mcp_protocol_and_tools': 0,
        'p4_adk_agent_tools': 0,
        'p5_nl2sql_ast_and_safety': 0,
        'p6_ge_mart_and_billing_join': 0,
        'p7_grounding_and_sme_rubric': 0,
        'p8_concurrency_thread_safety': 0,
        'p9_node_ui_deep_fuzz': 0,
        'p10_full_stack_fault_injection': 0,
    }
    self.extra_counts: dict[str, int] = collections.defaultdict(int)


def _run_pillar_check(pillar: int, step_idx: int, cx: _LoopContext) -> None:
  """Executes one deep architectural verification check across Pillars 1..8."""
  rng, ctrl, client, deck = cx.rng, cx.ctrl, cx.client, cx.deck
  models, agents = cx.models, cx.agents
  pillar_counts = cx.pillar_counts

  if pillar == 0:
    # -------------------------------------------------------------------------
    # Pillar 1: FinOps Mathematical Conservation & Invariant Fuzzing
    # -------------------------------------------------------------------------
    sub = step_idx % 4
    if sub == 0:
      # Random multi-model spend drift conservation: sum(drivers) == change_usd
      cur_models = []
      prev_models = []
      for m in rng.sample(models, k=min(3, len(models))):
        if rng.random() < 0.85:
          c_in = rng.randint(1000, 2_000_000)
          cur_models.append({
              'model': m,
              'invocations': rng.randint(1, 5000),
              'input_tokens': c_in,
              'output_tokens': rng.randint(100, 400_000),
              'reasoning_tokens': rng.randint(0, 100_000),
              'cache_read_tokens': rng.randint(0, c_in),
              'cache_write_tokens': 0,
          })
        if rng.random() < 0.85:
          p_in = rng.randint(1000, 2_000_000)
          prev_models.append({
              'model': m,
              'invocations': rng.randint(1, 5000),
              'input_tokens': p_in,
              'output_tokens': rng.randint(100, 400_000),
              'reasoning_tokens': rng.randint(0, 100_000),
              'cache_read_tokens': rng.randint(0, p_in),
              'cache_write_tokens': 0,
          })
      drift = finops.build_spend_drift(
          {'interval': 'now', 'rate_cards': telemetry.RATE_CARDS, 'models': cur_models},
          {'interval': 'prev', 'rate_cards': telemetry.RATE_CARDS, 'models': prev_models},
      )
      assert drift['status'] == 'LIVE'
      assert abs(drift['unexplained_usd']) <= 1e-5, f"Unexplained drift: {drift['unexplained_usd']}"
      driver_sum = sum(d['change_usd'] for d in drift['drivers'])
      assert abs(driver_sum - drift['change_usd']) <= 0.002
    elif sub == 1:
      # Session token drilldown conservation & deduplication + NaN/bool resilience
      s1_tok = rng.randint(100, 10000)
      s2_tok = rng.randint(100, 10000)
      sl_tok = rng.randint(50, 5000)
      sessions = [
          {'session_key': 'e1|s1', 'session_id': 's1', 'engine_key': 'e1', 'user_email': 'a@corp.com', 'turns': 2, 'total_tokens': s1_tok, 'input_tokens': s1_tok, 'output_tokens': None},
          {'session_key': 'e1|s1', 'session_id': 's1', 'engine_key': 'e1', 'user_email': 'a@corp.com', 'turns': 2, 'total_tokens': s1_tok, 'input_tokens': s1_tok, 'output_tokens': None},  # duplicate
          {'session_key': 'e1|s2', 'session_id': 's2', 'engine_key': 'e1', 'user_email': 'b@corp.com', 'turns': 'nan', 'total_tokens': s2_tok, 'input_tokens': True, 'output_tokens': 'inf'},
      ]
      sessionless = [
          {'turn_id': 't-sl-1', 'user_email': 'a@corp.com', 'total_tokens': sl_tok, 'input_tokens': sl_tok},
          {'turn_id': 't-sl-1', 'user_email': 'a@corp.com', 'total_tokens': sl_tok, 'input_tokens': sl_tok},  # duplicate
      ]
      dd = finops.build_session_token_drilldown(sessions, {'e1|s1': [{'turn_id': 't1'}]}, sessionless)
      assert dd['session_count'] == 2
      assert dd['sessionless_turn_count'] == 1
      assert dd['totals']['total_tokens'] == s1_tok + s2_tok + sl_tok
      assert dd['totals']['output_tokens'] is None  # None honesty preserved!
    elif sub == 2:
      # Per-user list-price cost estimation immutability & cached > input clamping
      orig_user = {'user_email': 'u1@corp.com', 'interactions': 5}
      model_rows = [
          {'user_email': 'u1@corp.com', 'engine_key': 'us/eng1', 'model_name': 'publishers/google/models/gemini-2.5-pro', 'input_tokens': 100_000, 'cached_input_tokens': 250_000, 'output_tokens': 10_000},
          {'user_email': 'u1@corp.com', 'engine_key': 'us/eng1', 'model_name': 'unknown-custom-model', 'input_tokens': 5000, 'cached_input_tokens': 0, 'output_tokens': 1000},
      ]
      enriched, summary = finops.estimate_user_costs([orig_user], model_rows, telemetry.RATE_CARDS)
      assert 'est_cost_usd' not in orig_user, 'Input user dict must never be mutated'
      assert enriched[0]['est_cost_usd'] is not None and enriched[0]['est_cost_usd'] > 0
      assert enriched[0]['est_cost_unpriced_tokens'] == 6000
      assert summary['unpriced_tokens'] == 6000
      zero_in_cost = finops.tokens_cost_usd(0, 0, 500_000, telemetry.RATE_CARDS['gemini-2.5-pro'])
      assert zero_in_cost == 0.0
    else:
      # TurnUsageLog.compute_costs across priced & unpriced models
      m = rng.choice([*models, 'unpriced-experimental-v9'])
      p_tok = rng.randint(0, 500_000)
      c_tok = rng.randint(0, p_tok + 50_000)
      tlog = telemetry.TurnUsageLog(
          timestamp='2026-10-11T00:00:00Z',
          agent_name='test',
          model=m,
          turn_index=1,
          prompt_prefix_hash='h',
          cache_breakpoint_line=None,
          cache_breakpoint_reason='ok',
          prompt_token_count=p_tok,
          cached_content_token_count=min(p_tok, c_tok),
          cache_creation_input_tokens=0,
          uncached_input_tokens=max(0, p_tok - c_tok),
          candidates_token_count=1000,
          thoughts_token_count=500,
          status_code=200,
          tool_called='none',
          evolution_generation=0,
      )
      naive, actual, saved = tlog.compute_costs()
      if m in telemetry.RATE_CARDS:
        assert naive is not None and actual is not None and saved is not None
        assert naive >= actual and saved >= -1e-9
      else:
        assert naive is None and actual is None and saved is None
    pillar_counts['p1_finops_math_conservation'] += 1

  elif pillar == 1:
    # -------------------------------------------------------------------------
    # Pillar 2: Prompt Cache X-Ray Property-Based Testing
    # -------------------------------------------------------------------------
    sub = step_idx % 4
    static_tail = '\n## Static Policy\n' + ('Rule: always verify ticket severity before paging SRE.\n' * 15)
    if sub == 0:
      prev = f'Current time: 2026-10-01T10:00:00Z\nUser: a@example.com\nRequest_id: req-{step_idx}\n{{"b": 1, "a": 2}}{static_tail}'
      curr = f'Current time: 2026-10-01T10:05:59Z\nUser: b@example.com\nRequest_id: req-{step_idx + 1}\n{{"a": 2, "b": 1}}{static_tail}'
    elif sub == 1:
      prev = f'Static system prompt v1.{static_tail}'
      curr = prev
    elif sub == 2:
      prev = f'Static system prompt v1.{static_tail}'
      curr = prev + f'\nUser turn {step_idx}: check status'
    else:
      curr = f'Static system prompt v1.{static_tail}'
      prev = curr + '\nExtra trailing text'
    bad_requests = deck.draw('p2.prompt_xray.monthly_requests')
    bad_tokens = deck.draw('p2.prompt_xray.current_input_tokens')
    rep = prompt_xray.analyze(
        prev,
        curr,
        model=rng.choice(models),
        monthly_requests=bad_requests,  # type: ignore[arg-type]
        current_input_tokens=bad_tokens,  # type: ignore[arg-type]
    )
    total_seg_chars = sum(int(seg['end'] - seg['start']) for seg in rep['heatmap'])
    assert total_seg_chars == len(curr), f'Heatmap chars {total_seg_chars} != len(curr) {len(curr)}'
    cursor = 0
    for seg in rep['heatmap']:
      assert seg['start'] == cursor
      assert seg['end'] >= seg['start']
      cursor = seg['end']
    assert cursor == len(curr)
    if rep['status'] == prompt_xray.STATUS_BUSTED and rep['rewrite']['changed']:
      assert rep['rewrite']['projected_cached_prefix_pct'] >= rep['cached_prefix_pct']
      assert rep['rewrite']['projected_recoverable_tokens_per_request'] <= rep['recoverable_tokens_per_request']
    if rep['stranded_cost_usd_per_month'] is not None:
      assert math.isfinite(rep['stranded_cost_usd_per_month']) and rep['stranded_cost_usd_per_month'] >= 0
    pillar_counts['p2_prompt_xray_properties'] += 1

  elif pillar == 2:
    # -------------------------------------------------------------------------
    # Pillar 3: MCP JSON-RPC 2.0 - all 9 tools, VL-MCP-500, and the REAL POST /mcp HTTP route
    # -------------------------------------------------------------------------
    sub = step_idx % 16
    with mock.patch.object(mcp_server, '_get_controller', return_value=ctrl):
      if sub == 0:
        out, hdrs = _call_mcp({
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'stress'}},
        }, None)
        assert out['result']['protocolVersion'] == '2025-06-18' and hdrs.get('Mcp-Session-Id')
      elif sub == 1:
        out, _ = _call_mcp({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert len(out['result']['tools']) == 9
      elif sub == 2:
        caller_args = {'focus_tab': deck.draw('p3.mcp.open_dashboard.focus_tab'), 'initial_agent': 'it_service_desk', 'session_id': 's-imm'}
        out, _ = _call_mcp({
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'open_dashboard', 'arguments': caller_args},
        })
        assert 'session_id' in caller_args, 'Caller arguments dict must not be mutated'
        assert out['result']['structuredContent']['focus_tab'] in range(8)
      elif sub == 3:
        out, _ = _call_mcp({
            'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'query_ge_agent_fleet', 'arguments': {'window_hours': deck.draw('p3.mcp.fleet.window_hours')}},
        })
        assert not out['result']['isError']
      elif sub == 4:
        with mock.patch.object(ctrl.gcp_telemetry, 'get_telemetry_summary_payload', return_value={'status': 'EMPTY'}):
          out, _ = _call_mcp({
              'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
              'params': {'name': 'query_project_telemetry', 'arguments': {}},
          })
          assert not out['result']['isError']
      elif sub == 5:
        # calculate_prompt_cache_economics with EVERY weird value over a full deck cycle (NaN, Inf, bool, str, None, list, dict)
        w = deck.draw('p3.mcp.calc.prompt_token_count')
        w2 = deck.draw('p3.mcp.calc.output_token_count')
        out, _ = _call_mcp({
            'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
            'params': {
                'name': 'calculate_prompt_cache_economics',
                'arguments': {
                    'prompt_token_count': w,
                    'cached_content_token_count': 999999,
                    'output_token_count': w2,
                    'thoughts_token_count': True,
                    'model': 'gemini-2.5-flash',
                },
            },
        })
        assert not out['result']['isError']
        sc = out['result']['structuredContent']
        assert sc['cached_tokens'] <= sc['prompt_tokens']
        assert sc['thoughts_tokens'] == 0  # True is rejected, not coerced to 1
      elif sub == 6:
        with mock.patch.object(ctrl, '_is_live_gcp', return_value=(step_idx % 2 == 0)):
          out, _ = _call_mcp({
              'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
              'params': {'name': 'run_alpha_evolve_generation', 'arguments': {'agent_id': 'it_service_desk'}},
          })
          assert not out['result']['isError']
          assert 'None' not in out['result']['content'][0]['text']
      elif sub == 7:
        out, _ = _call_mcp({'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call', 'params': {'name': 'get_finops_state', 'arguments': {}}})
        assert 'active_agent' in out['result']['structuredContent']
      elif sub == 8:
        out, _ = _call_mcp({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'xray_prompt_cache', 'arguments': {'previous_prompt': 'A', 'current_prompt': 'B'}},
        })
        assert not out['result']['isError']
      elif sub == 9:
        out, _ = _call_mcp({'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call', 'params': {'name': 'list_prompt_snapshot_turns', 'arguments': {}}})
        assert not out['result']['isError']
      elif sub == 10:
        out, _ = _call_mcp({
            'jsonrpc': '2.0', 'id': 11, 'method': 'tools/call',
            'params': {'name': 'set_agent_trace_logging', 'arguments': {'enabled': None}},
        })
        assert out['result']['structuredContent']['status'] == 'INVALID_REQUEST'
      elif sub == 11:
        # Fault injection on MCP tool -> VL-MCP-500 structured error contract (no secret leakage)
        with mock.patch.object(ctrl, 'get_state_payload', side_effect=RuntimeError('secret_mcp_crash_token=abc')):
          out, _ = _call_mcp({'jsonrpc': '2.0', 'id': 12, 'method': 'tools/call', 'params': {'name': 'get_finops_state', 'arguments': {}}})
          assert out['result']['isError'] is True
          sc = out['result']['structuredContent']
          assert sc['error_code'] == 'VL-MCP-500'
          assert sc['error_kind'] == 'INTERNAL_CODE_ERROR'
          assert sc['is_code_bug'] is True
          assert sc['state_preserved'] is True
          assert 'secret_mcp_crash_token' not in json.dumps(out)
      elif sub == 12:
        # REAL HTTP route: malformed JSON must be a JSON-RPC parse error (HTTP 400, -32700), never a 500.
        r = client.post('/mcp', content=b'{not json', headers={'content-type': 'application/json'})
        assert r.status_code == 400, r.text
        assert r.json()['error']['code'] == mcp_server.PARSE_ERROR
        r_empty = client.post('/mcp', content=b'', headers={'content-type': 'application/json'})
        assert r_empty.status_code == 400 and r_empty.json()['error']['code'] == mcp_server.PARSE_ERROR
        cx.extra_counts['mcp_http_route_checks'] += 1
      elif sub == 13:
        # REAL HTTP route: a batch with a valid call, an unknown method and a notification.
        r = client.post('/mcp', json=[
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'does/not/exist'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        ])
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and len(body) == 2, body  # notifications get no response entry
        codes = {entry.get('error', {}).get('code') for entry in body if 'error' in entry}
        assert mcp_server.METHOD_NOT_FOUND in codes
        assert any(len(entry.get('result', {}).get('tools', [])) == 9 for entry in body)
        cx.extra_counts['mcp_http_route_checks'] += 1
      elif sub == 14:
        # REAL HTTP route: initialize -> Mcp-Session-Id header, DELETE ends the session (204), GET is 405.
        r = client.post('/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'stress-http'}},
        })
        assert r.status_code == 200 and r.headers.get('mcp-session-id'), r.headers
        assert r.json()['result']['protocolVersion'] == '2025-06-18'
        assert client.delete('/mcp', headers={'Mcp-Session-Id': r.headers['mcp-session-id']}).status_code == 204
        assert client.get('/mcp').status_code == 405
        cx.extra_counts['mcp_http_route_checks'] += 1
      else:
        # REAL HTTP route: unknown tool -> -32602, wrong JSON-RPC version -> -32600, tool crash -> VL-MCP-500 over HTTP.
        r = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {'name': 'nope', 'arguments': {}}})
        assert r.status_code == 200 and r.json()['error']['code'] == mcp_server.INVALID_PARAMS
        r2 = client.post('/mcp', json={'jsonrpc': '1.0', 'id': 6, 'method': 'tools/list'})
        assert r2.status_code == 200 and r2.json()['error']['code'] == mcp_server.INVALID_REQUEST
        with mock.patch.object(ctrl, 'get_state_payload', side_effect=KeyError('http_secret_key_name')):
          r3 = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'get_finops_state', 'arguments': {}}})
        assert r3.status_code == 200
        sc3 = r3.json()['result']['structuredContent']
        assert sc3['error_code'] == 'VL-MCP-500' and sc3['is_code_bug'] is True
        assert 'http_secret_key_name' not in r3.text
        cx.extra_counts['mcp_http_route_checks'] += 1
    pillar_counts['p3_mcp_protocol_and_tools'] += 1

  elif pillar == 3:
    # -------------------------------------------------------------------------
    # Pillar 4: ADK Agent (app/agent.py) - ALL 10 tool functions
    # -------------------------------------------------------------------------
    sub = step_idx % 10
    w = deck.draw('p4.adk.weird')
    with mock.patch.object(server, '_global_controller', ctrl):
      if sub == 0:
        res = json.loads(adk_agent.calculate_cache_economics(w, 999999, model=str(w), candidates_token_count=w, thoughts_token_count=True))
        assert res['cached_tokens'] <= res['prompt_tokens']
      elif sub == 1:
        res = json.loads(adk_agent.detect_prompt_breakpoint(None if step_idx % 2 == 0 else 'line1\nline2', 'line1\nline3'))  # type: ignore[arg-type]
        assert 'breakpoint_line' in res and 'prefix_hash' in res
      elif sub == 2:
        res = json.loads(adk_agent.open_dashboard(focus_tab=w, initial_agent=''))  # type: ignore[arg-type]
        assert res['status'] == 'opened' and res['focus_tab'] in range(8)
      elif sub == 3:
        res = json.loads(adk_agent.query_live_finops_and_mart(days=w))  # type: ignore[arg-type]
        assert 1 <= res['window_days'] <= 365
      elif sub == 4:
        res = json.loads(adk_agent.query_gcp_telemetry(hours_ago=24))
        assert 'project_id' in res
      elif sub == 5:
        res = json.loads(adk_agent.list_cloud_run_agents())
        assert isinstance(res, list)
      elif sub == 6:
        res = json.loads(adk_agent.xray_prompt_cache(' Static A ', ' Static B ', monthly_requests=0))
        assert 'status' in res
      elif sub == 7:
        res = json.loads(adk_agent.trigger_alpha_evolve_cycle(agent_id='it_service_desk'))
        assert res['simulator'] is True
      elif sub == 8:
        res = json.loads(adk_agent.query_ge_agent_fleet(window_hours=w))  # type: ignore[arg-type]
        assert isinstance(res, dict) and ('agents' in res or 'status' in res)
      else:
        # With the judge on, the stubbed judge claims VERIFIED_GROUNDED: the deterministic verdict must still rule.
        res = json.loads(adk_agent.validate_telemetry_grounding(run_llm_judge=(step_idx % 2 == 0)))
        assert res['overall_status'] in ('VERIFIED_GROUNDED', 'FLAGGED_ISSUES') and 'llm_judge' in res
        assert res['total_checks'] >= 10
    pillar_counts['p4_adk_agent_tools'] += 1

  elif pillar == 4:
    # -------------------------------------------------------------------------
    # Pillar 5: NL2SQL Verifier, Adversarial SQL Corpus, AST FinOps Scanner & BigQuery error translation
    # -------------------------------------------------------------------------
    allowed_ds = ['demo-offline-project.finops_mart', 'demo-offline-project.ds_ge_curated_staging']
    adversarial_sql = [
        'DROP TABLE `demo-offline-project.finops_mart.fct_turns`',
        'DELETE FROM `demo-offline-project.finops_mart.fct_turns` WHERE 1=1',
        'SELECT * FROM `demo-offline-project.finops_mart.fct_turns` LIMIT 10; DROP TABLE x',
        'SELECT * FROM `evil-project.finops_mart.fct_turns` LIMIT 10',
        'SELECT * FROM `demo-offline-project.secret_dataset.users` LIMIT 10',
        'MERGE `demo-offline-project.finops_mart.fct_turns` T USING S ON 1=1 WHEN MATCHED THEN DELETE',
        'EXECUTE IMMEDIATE "SELECT 1"',
        'SELECT * FROM unquoted_table LIMIT 10',
    ]
    bad_sql = adversarial_sql[step_idx % len(adversarial_sql)]
    audit = optimizer.verify_nl2sql_sql(bad_sql, allowed_ds)
    assert audit['read_only_enforced'] is False, f'Adversarial SQL must be rejected: {bad_sql}'

    good_sql = (
        'SELECT engine_key, SUM(total_tokens) AS tok '
        'FROM `demo-offline-project.finops_mart.fct_turns` '
        'WHERE event_date >= CURRENT_DATE() - 7 GROUP BY 1 LIMIT 25'
    )
    good_audit = optimizer.verify_nl2sql_sql(good_sql, allowed_ds)
    assert good_audit['read_only_enforced'] is True

    if step_idx % 4 == 0:
      findings = optimizer.scan_python_code_for_finops_findings(
          'for item in batch:\n  client.models.generate_content(model="gemini-2.5-pro", response_schema=S, top_k=50)\n'
      )
      rule_ids = {f['rule_id'] for f in findings}
      assert 'FIN-01' in rule_ids and 'FIN-03' in rule_ids and 'FIN-05' in rule_ids
    if step_idx % 2 == 1:
      # BigQuery / billing export error text -> user-facing error_kind + remediation hint
      for text, kind in (
          ('Access Denied: BigQuery BigQuery: Permission denied while getting Drive credentials', 'permission_denied'),
          ('Not found: Table proj:ds.gcp_billing_export_v1_X was not found in location US', 'not_found'),
          ('Unrecognized name: cost_at_list at [3:7]', 'schema_mismatch'),
          ('Query exceeded limit for bytes billed: 5368709120', 'bytes_limit'),
          ('Operation did not finish in time', 'timeout'),
          ('HTTP 401 Unauthorized: invalid credentials', 'auth'),
          ('Invalid billing export table name: nope', 'invalid_table_name'),
          ('something nobody anticipated', 'unknown'),
          (None, 'unknown'),
      ):
        translated = billing_export.classify_billing_error(text)
        assert translated['error_kind'] == kind, (text, translated)
        assert translated['hint']
      cx.extra_counts['bigquery_error_translation_checks'] += 1
    pillar_counts['p5_nl2sql_ast_and_safety'] += 1

  elif pillar == 5:
    # -------------------------------------------------------------------------
    # Pillar 6: BigQuery ge_mart Row Mappers, PII Redaction & Billing Join
    # -------------------------------------------------------------------------
    sub = step_idx % 4
    if sub == 0:
      for bad_v in [None, '', True, False, 'nan', 'inf', '-inf', float('nan'), float('inf'), 'not-int', [], {}]:
        assert ge_mart.int_or_none(bad_v) is None
      assert ge_mart.int_or_none('42.9') == 42
      assert ge_mart._clamp(True, 1, 90) == 1
      assert ge_mart._clamp(float('inf'), 1, 90) == 1
    elif sub == 1:
      with mock.patch.dict(os.environ, {'FINOPS_PII_REDACT': '1'}):
        red = ge_mart.redact_email_if_configured('Alice.Smith@Example.com')
        assert red and red.startswith('user-') and 'alice' not in red.lower() and red.endswith('@Example.com')
    elif sub == 2:
      t_row = ge_mart.usage_log_from_row({
          'turn_id': 't-100', 'turn_status': 'SUCCESS', 'user_email': 'u@corp.com',
          'input_tokens': None, 'output_tokens': '150', 'total_tokens': 'nan', 'cached_input_tokens': True,
      }, 'demo-offline-project')
      assert t_row['input_tokens'] is None and t_row['output_tokens'] == 150
      assert t_row['total_tokens'] is None and t_row['cached_input_tokens'] is None
      assert t_row['prompts'] == []  # Prompt text never leaves BigQuery
    else:
      daily = [{'day': '2026-10-10', 'interactions': 200, 'total_tokens': 500_000}]
      billing_days = {
          'status': 'LIVE',
          'last_billed_day': '2026-10-10',
          'days': [{'day': '2026-10-10', 'ai_net_usd': 10.0, 'all_net_usd': 12.0}],
      }
      joined = billing_export.join_daily_usage_with_cost(daily, billing_days)
      assert len(joined) == 1
      assert joined[0]['ai_net_usd'] == 10.0
      assert joined[0]['usd_per_1k_turns'] == round(10.0 / 200 * 1000.0, 4)
      assert joined[0]['usd_per_1m_tokens'] == round(10.0 / 500_000 * 1_000_000.0, 4)
    pillar_counts['p6_ge_mart_and_billing_join'] += 1

  elif pillar == 6:
    # -------------------------------------------------------------------------
    # Pillar 7: Telemetry Grounding Validator (+ LLM judge cannot override) & SME Rubric
    # -------------------------------------------------------------------------
    sub = step_idx % 3
    corrupted = dict(cx.base_state)
    corrupted['live_data'] = True
    corrupted['gcp_project'] = 'prod-live-project-123'
    corrupted['ge_fleet'] = {
        'status': 'LIVE',
        'project_id': 'prod-live-project-123',
        'agents': [{'display_name': 'A1', 'metrics': {'requests': 10, 'errors': 0, 'input_tokens': 100, 'output_tokens': 50}}],
        'totals': {'agents': 99, 'requests': 99999, 'errors': 0, 'input_tokens': 100, 'output_tokens': 50},
    }
    if sub == 0:
      val = telemetry_validator.validate_dashboard_state(cx.base_state, run_llm_judge=False)
      assert val['total_checks'] >= 10 and val['overall_status'] == 'VERIFIED_GROUNDED'
      det = telemetry_validator.validate_dashboard_state(corrupted, run_llm_judge=False)
      assert det['failed_count'] >= 1 and det['overall_status'] != 'VERIFIED_GROUNDED'
    elif sub == 1:
      sme = sme_eval.evaluate_persona_rubric(cx.base_state)
      assert sme['total_checks'] == 30
      assert sme['passed_checks'] == 30, f"SME failed checks: {[c for c in sme['checks'] if not c['passed']]}"
      assert sme['overall_compliance_status'] == 'COMPLIANT'
    else:
      # The (stubbed) LLM judge swears everything is grounded; failed deterministic checks must still win.
      det = telemetry_validator.validate_dashboard_state(corrupted, run_llm_judge=True)
      assert det['llm_judge']['executed'] is True and det['llm_judge']['verdict'] == 'VERIFIED_GROUNDED'
      assert det['failed_count'] >= 1
      assert det['overall_status'] != 'VERIFIED_GROUNDED', 'an LLM judge verdict must never override failed deterministic checks'
      cx.extra_counts['llm_judge_non_override_checks'] += 1
    pillar_counts['p7_grounding_and_sme_rubric'] += 1

  else:
    # -------------------------------------------------------------------------
    # Pillar 8: REAL HTTP-level concurrency through the production app + DLQ race conditions
    # -------------------------------------------------------------------------
    if step_idx % 10 == 0:
      def _http_worker(w_id: int) -> bool:
        wrng = random.Random(step_idx * 1000 + w_id)
        c = cx.thread_clients[w_id % len(cx.thread_clients)]
        for _ in range(4):
          op = wrng.random()
          if op < 0.30:
            r = c.get('/api/state?window_hours=24')
            assert r.status_code == 200, r.text[:200]
            st = r.json()
            assert st['active_agent']['agent_id'] in agents, 'unknown active agent under concurrency'
            assert isinstance(st['active_agent'].get('parameters'), list)
          elif op < 0.50:
            r = c.post('/api/select_agent', json={'agent_id': wrng.choice(agents)})
          elif op < 0.65:
            r = c.post('/api/evolve_generation', json={})
          elif op < 0.75:
            r = c.post('/api/step_turn', json={})
          elif op < 0.85:
            r = c.post('/api/add_parameter', json={'label': f'P{w_id}', 'baseline_val': 1, 'target_val': 2, 'weight_pct': 5})
          elif op < 0.92:
            r = c.post('/api/inject_anomaly', json={})
          else:
            r = c.post('/api/reset', json={})
          assert r.status_code == 200, f'{r.request.url.path} -> HTTP {r.status_code}: {r.text[:200]}'
        return True

      with futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(_http_worker, range(8)))
      cx.extra_counts['concurrency_http_requests'] += 32
    else:
      def _worker(w_id: int) -> bool:
        if w_id % 3 == 0:
          ctrl.select_agent(agents[w_id % len(agents)])
        elif w_id % 3 == 1:
          telemetry.record_ingest_dlq('/api/concurrent_test', {'k1': w_id, 'secret': 'never-logged'}, [f'err-{w_id}'])
        else:
          dlq = ctrl.dead_letters_payload()
          assert dlq['status'] == 'OK'
        return True

      with futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_worker, range(4)))
      assert all(results)
      dlq_events = telemetry.get_ingest_dlq_events()
      assert len(dlq_events) <= 50
      assert 'never-logged' not in json.dumps(dlq_events)
    pillar_counts['p8_concurrency_thread_safety'] += 1


def _run_bucket_check(i: int, bucket: int, cx: _LoopContext, counts: dict[str, int]) -> None:
  """Executes one top-level bucket check (happy / edge / validation / live policy / fault injection)."""
  rng, ctrl, client, deck = cx.rng, cx.ctrl, cx.client, cx.deck
  models, agents, personas, platforms = cx.models, cx.agents, cx.personas, cx.platforms

  if bucket == 0:
    # -----------------------------------------------------------------------
    # 1. NORMAL HAPPY PATHS
    # -----------------------------------------------------------------------
    sub = (i // 5) % 10
    if sub == 0:
      r = client.get('/api/health')
      assert r.status_code == 200 and r.json()['status'] == 'ok'
    elif sub == 1:
      r = client.get('/api/state?window_hours=24')
      assert r.status_code == 200
      body = r.json()
      assert 'active_agent' in body and 'tokenomics_cockpit' in body
    elif sub == 2:
      ag = rng.choice(agents)
      r = client.post('/api/select_agent', json={'agent_id': ag})
      assert r.status_code == 200 and r.json()['active_agent']['agent_id'] == ag
    elif sub == 3:
      pl = rng.choice(platforms)
      r = client.post('/api/select_optimizer', json={'platform_id': pl})
      assert r.status_code == 200
      assert r.json()['optimizer_platforms']['active_platform_id'] == pl
    elif sub == 4:
      r = client.post('/api/what_if_live', json={
          'kind': 'model_switch',
          'from_model': 'gemini-2.5-pro',
          'to_model': 'gemini-2.5-flash',
          'share_pct': rng.choice([25, 50, 75, 100]),
          'window_hours': 24,
      })
      assert r.status_code == 200 and r.json()['status'] == 'PROJECTION'
      assert math.isfinite(r.json()['change_usd']) and math.isfinite(r.json()['projected_total_usd'])
    elif sub == 5:
      r = client.post('/api/prompt_xray', json={
          'model': rng.choice(models),
          'previous_prompt': 'System prefix: corporate policy v1.\nUser: status of INC-100?\nTools: [search]',
          'current_prompt': 'System prefix: corporate policy v1.\nUser: status of INC-101?\nTools: [search]',
          'calls_per_day': 500,
      })
      assert r.status_code == 200 and 'cached_prefix_pct' in r.json() and 'rewrite' in r.json()
    elif sub == 6:
      r = client.post('/api/recompute_finops', json={
          'calls_per_hr': rng.randint(50, 2000),
          'prefix_tokens': rng.randint(1024, 8192),
          'var_tokens': rng.randint(128, 1024),
          'output_tokens': rng.randint(128, 1024),
          'ttl_sec': 3600,
          'hit_rate': 0.8,
          'model': rng.choice(models),
      })
      assert r.status_code == 200 and 'cpo' in r.json() and 'caching' in r.json()
    elif sub == 7:
      r = client.post('/api/nl2sql', json={'question': 'Show cache hit rate and cost by agent'})
      assert r.status_code == 200 and 'generated_sql' in r.json() and 'sql_safety_audit' in r.json()
    elif sub == 8:
      store = sme_eval.get_sme_evaluation_store()
      before = len(store.list_ratings())
      r = client.post('/api/sme_eval/rate', json={
          'persona_id': rng.choice(personas),
          'overall_rating': rng.choice([3, 4, 5]),
          'verdict': rng.choice(['APPROVED', 'APPROVED_WITH_NOTES', 'NEEDS_WORK']),
          'would_recommend': True,
          'reviewer': 'stress-loop-sme',
          'notes': 'Verified in automated 10000x loop',
      })
      assert r.status_code == 200 and r.json()['status'] == 'recorded'
      assert len(store.list_ratings()) == before + 1
    else:
      r = client.post('/a2a/app', json={
          'jsonrpc': '2.0',
          'id': f'rpc-{i}',
          'method': 'message/send',
          'params': {'message': {'parts': [{'text': 'calculate prompt cache economics'}]}},
      })
      assert r.status_code == 200 and r.json()['result']['status']['state'] == 'completed'
    counts['happy_path'] += 1

  elif bucket == 1:
    # -----------------------------------------------------------------------
    # 2. EDGE CASES & BOUNDARY FUZZING (deck-drawn: every slot sees every value once per cycle)
    # -----------------------------------------------------------------------
    sub = (i // 5) % 8
    if sub == 0:
      w_val = deck.draw('b2.recompute_finops.w_val')
      w_val2 = deck.draw('b2.recompute_finops.w_val2')
      r = client.post('/api/recompute_finops', json={
          'calls_per_hr': w_val,
          'prefix_tokens': w_val2,
          'var_tokens': w_val,
          'output_tokens': w_val2,
          'ttl_sec': w_val,
          'hit_rate': w_val2,
          'model': str(w_val),
          'deferred_share_pct': w_val,
      })
      assert r.status_code == 200, (w_val, w_val2, r.text[:200])
      tf = r.json()
      assert math.isfinite(tf['cpo']['fleet_monthly_baseline_usd'])
      assert math.isfinite(tf['caching']['hourly_cost_comparison']['uncached_paygo_hr_usd'])
    elif sub == 1:
      w_val = deck.draw('b2.what_if_simulate.w_val')
      w_val2 = deck.draw('b2.what_if_simulate.w_val2')
      r = client.post('/api/what_if_simulate', json={
          'model_tier': str(w_val),
          'thinking_budget_tok': w_val,
          'history_window_turns': w_val2,
          'traffic_canary_pct': w_val,
      })
      assert r.status_code == 200, (w_val, w_val2, r.text[:200])
    elif sub == 2:
      w_val = deck.draw('b2.prompt_xray.w_val')
      w_val2 = deck.draw('b2.prompt_xray.w_val2')
      r = client.post('/api/prompt_xray', json={
          'previous_prompt': '' if i % 2 == 0 else 'Hello <script>alert(1)</script> ' * 20,
          'current_prompt': '' if i % 3 == 0 else 'Hello <script>alert(2)</script> ' * 20,
          'model': str(w_val),
          'calls_per_day': w_val,
          'monthly_requests': w_val2,
          'current_input_tokens': w_val,
      })
      assert r.status_code == 200, (w_val, w_val2, r.text[:200])
      px = r.json()
      assert ('cached_prefix_pct' in px and 'rewrite' in px) or (
          px.get('status') == 'ERROR' and px.get('error_kind') == 'VALIDATION_ERROR'
      )
    elif sub == 3:
      weird_bodies: list[Any] = [
          None, [], 'plain-string-body', 12345, True, {'unexpected': ['nested', {'deep': None}]},
          json.loads('[' * 64 + '1' + ']' * 64),
      ]
      non_dict = weird_bodies[i % len(weird_bodies)]
      target_ep = rng.choice([
          '/api/recompute_finops',
          '/api/what_if_simulate',
          '/api/prompt_xray',
          '/api/select_agent',
          '/api/select_optimizer',
          '/api/nl2sql',
          '/a2a/app',
          '/api/sme_eval/rate',
          '/api/aive_log',
          '/api/reset',
      ])
      r = client.post(target_ep, json=non_dict)
      assert r.status_code in (200, 422), (target_ep, non_dict, r.status_code, r.text[:200])
    elif sub == 4:
      w_val = deck.draw('b2.state_query.w_val')
      w_val2 = deck.draw('b2.state_query.w_val2')
      r = client.get(f'/api/state?window_hours={w_val}')
      assert r.status_code == 200
      r2 = client.get(f'/api/ge_fleet?window_hours={w_val}&force_refresh={w_val2}')
      assert r2.status_code == 200
      r3 = client.get(f'/api/prompt_xray/live_turns?limit={w_val2}&engine={w_val}')
      assert r3.status_code == 200
    elif sub == 5:
      w_val = deck.draw('b2.ge_mart_refresh.w_val')
      w_val2 = deck.draw('b2.ge_mart_refresh.w_val2')
      r = client.post('/api/ge_mart/refresh', json={'incremental': w_val, 'lookback_days': w_val2})
      assert r.status_code == 200
    elif sub == 6:
      usage_sample = ctrl.ge_fleet.get_fleet_snapshot()['model_usage']
      for bad in [float('nan'), float('inf'), -float('inf'), True, False, 'nan', 'inf', 'abc']:
        try:
          finops.project_model_switch(
              usage_sample,
              'gemini-2.5-pro',
              'gemini-2.5-flash',
              bad,  # type: ignore[arg-type]
              telemetry.RATE_CARDS,
          )
          raise AssertionError(f'Expected (TypeError, ValueError) for {bad!r}')
        except (TypeError, ValueError):
          pass
    else:
      w_val = deck.draw('b2.a2a.params')
      r = client.post('/a2a/app', json={'id': None, 'method': 'unknown', 'params': w_val})
      assert r.status_code == 200 and ('result' in r.json() or 'error' in r.json())
    counts['edge_fuzz'] += 1

  elif bucket == 2:
    # -----------------------------------------------------------------------
    # 3. USER / INPUT VALIDATION ERRORS - wrong values AND missing fields (HTTP 422 / INVALID_REQUEST)
    # -----------------------------------------------------------------------
    sub = (i // 5) % 7
    if sub == 0:
      secret_pii = f'secret-ssn-{i}-999-00-1234'
      r = client.post('/api/decorator_ingest', json={
          'agent_name': '',
          'latency_ms': -50,
          'raw_user_secret': secret_pii,
      })
      assert r.status_code == 422
      body = r.json()
      assert body['error'] == 'invalid_payload'
      assert body['dead_letter']['source'] == '/api/decorator_ingest'
      assert body['dead_letter']['values_logged'] is False
      assert secret_pii not in json.dumps(body), 'PII must never be echoed in dead-letter response'
    elif sub == 1:
      bad_csat = rng.choice([
          {'agent_id': 'it_service_desk', 'rating': 99},
          {},  # nothing at all
          {'agent_id': 'it_service_desk'},  # missing rating
          {'rating': 'five', 'session_id': 's1'},
      ])
      r = client.post('/api/csat_rating', json=bad_csat)
      assert r.status_code == 422 and r.json()['error'] == 'invalid_payload', (bad_csat, r.text[:200])
    elif sub == 2:
      bad_payload = rng.choice([
          {'label': 'Test', 'baseline_val': 'not-a-number', 'target_val': 95, 'weight_pct': 10},
          {'label': 'Test', 'baseline_val': 'nan', 'target_val': 'inf', 'weight_pct': 10},
          {'label': '', 'baseline_val': 80, 'target_val': 95, 'weight_pct': 500},
          {'label': 'Test', 'direction': 'SIDEWAYS', 'baseline_val': 80, 'target_val': 95, 'weight_pct': 10},
      ])
      r = client.post('/api/add_parameter', json=bad_payload)
      assert r.status_code == 422
      body = r.json()
      assert body['error_kind'] == 'VALIDATION_ERROR'
      assert body['is_code_bug'] is False
      assert body['state_preserved'] is True
      assert isinstance(body['errors'], list) and len(body['errors']) >= 1
    elif sub == 3:
      bad_wi = rng.choice([
          {'kind': 'model_switch', 'from_model': 'gemini-2.5-pro', 'to_model': 'gemini-2.5-flash', 'share_pct': 'nan'},
          {'kind': 'model_switch', 'from_model': 'gemini-2.5-pro', 'to_model': 'gemini-2.5-flash', 'share_pct': 'inf'},
          {'kind': 'model_switch', 'from_model': 'gemini-2.5-pro', 'to_model': 'gemini-2.5-flash', 'share_pct': True},
          {'kind': 'cache_share', 'model': 'gemini-2.5-pro', 'target_cache_share_pct': 'not-a-float'},
      ])
      r = client.post('/api/what_if_live', json=bad_wi)
      assert r.status_code == 200
      body = r.json()
      assert body['status'] == 'ERROR'
      assert body['error_kind'] == 'VALIDATION_ERROR'
      assert body['is_code_bug'] is False
    elif sub == 4:
      # SME ratings: MISSING or invalid fields must be rejected and must never be recorded.
      store = sme_eval.get_sme_evaluation_store()
      before = len(store.list_ratings())
      bad_rating = rng.choice([
          {},
          {'persona_id': 'finops_lead'},
          {'persona_id': 'finops_lead', 'overall_rating': 4},
          {'persona_id': 'finops_lead', 'overall_rating': 'bad', 'verdict': 'APPROVED'},
          {'persona_id': 'finops_lead', 'overall_rating': 99, 'verdict': 'APPROVED'},
          {'persona_id': 'finops_lead', 'overall_rating': True, 'verdict': 'APPROVED'},
          {'persona_id': 'finops_lead', 'overall_rating': 4.5, 'verdict': 'APPROVED'},
          {'persona_id': 'finops_lead', 'overall_rating': 4, 'verdict': 'LGTM'},
          {'persona_id': 'not_a_real_persona', 'overall_rating': 5, 'verdict': 'APPROVED'},
      ])
      r = client.post('/api/sme_eval/rate', json=bad_rating)
      assert r.status_code == 422, (bad_rating, r.status_code, r.text[:200])
      body = r.json()
      assert body['status'] == 'ERROR'
      assert body['error_kind'] == 'VALIDATION_ERROR'
      assert body['is_code_bug'] is False
      assert len(store.list_ratings()) == before, 'a rejected SME rating must never be recorded'
      cx.extra_counts['missing_field_validation_checks'] += 1
    elif sub == 5:
      r = client.post('/api/enable_agent_observability', json={'resource_name': 'invalid/resource/path'})
      assert r.status_code == 200
      assert r.json()['status'] == 'INVALID_REQUEST'
    else:
      bad_ingest = rng.choice([
          {},
          {'agent_name': 'x', 'prompt_tokens': 10, 'cached_tokens': 50},  # cached > prompt
          {'agent_name': 'x' * 5000, 'prompt_tokens': 10},
          {'agent_name': 'x', 'prompt_tokens': 'ten'},
      ])
      r = client.post('/api/decorator_ingest', json=bad_ingest)
      assert r.status_code == 422, (bad_ingest, r.text[:200])
      assert r.json()['dead_letter']['values_logged'] is False
      cx.extra_counts['missing_field_validation_checks'] += 1
    counts['validation_error'] += 1

  elif bucket == 3:
    # -----------------------------------------------------------------------
    # 4. LIVE-MODE POLICY TABLE - every mutating POST route, guarded or allowed
    # -----------------------------------------------------------------------
    path = rng.choice(sorted(LIVE_MODE_POLICY))
    body = LIVE_POLICY_BODIES.get(path, {})
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True):
      r = client.post(path, json=body)
    assert r.status_code in (200, 400, 422), (path, r.status_code, r.text[:200])
    payload = r.json()
    status = payload.get('status') if isinstance(payload, dict) else None
    if LIVE_MODE_POLICY[path] == 'guarded':
      assert status == 'DISABLED_IN_LIVE_MODE', f'{path} must be guarded in live mode, got {status!r}: {r.text[:200]}'
      assert 'reason' in payload and len(payload['reason']) > 10
      cx.extra_counts['live_policy_guarded_checks'] += 1
    else:
      assert status != 'DISABLED_IN_LIVE_MODE', f'{path} is classified allowed but was blocked in live mode'
      cx.extra_counts['live_policy_allowed_checks'] += 1
    counts['live_mode_guardrail'] += 1

  else:
    # -----------------------------------------------------------------------
    # 5. SIMULATED INTERNAL CODE ERRORS through the PRODUCTION app (VL-CODE-500 + Cloud Logging record)
    # -----------------------------------------------------------------------
    exc_cls, exc_msg = rng.choice([
        (RuntimeError, 'simulated internal state fault containing secret_token=xyz987'),
        (KeyError, 'unexpected_missing_dict_key'),
        (ZeroDivisionError, 'float division by zero in experimental path'),
        (TypeError, 'NoneType object is not subscriptable'),
    ])
    target_method, path, method = rng.choice([
        ('query_nl2sql', '/api/nl2sql', 'POST'),
        ('recompute_finops', '/api/recompute_finops', 'POST'),
        ('prompt_xray', '/api/prompt_xray', 'POST'),
        ('get_user_centric_finops_payload', '/api/user_centric_finops', 'GET'),
        ('get_state_payload', '/api/state', 'GET'),
        ('what_if_live', '/api/what_if_live', 'POST'),
    ])
    log_before = len(cx.log_capture.records)
    with mock.patch.object(ctrl, target_method, side_effect=exc_cls(exc_msg)):
      r = client.get(path) if method == 'GET' else client.post(path, json={})
    assert r.status_code == 500, (path, r.status_code, r.text[:200])
    err_body = r.json()

    assert err_body['status'] == 'ERROR'
    assert err_body['error_kind'] == 'INTERNAL_CODE_ERROR'
    assert err_body['error_code'] == 'VL-CODE-500'
    assert err_body['is_code_bug'] is True
    assert err_body['state_preserved'] is True
    assert err_body['request_id'].startswith('req-')
    assert err_body['exception_type'] == exc_cls.__name__
    assert path in err_body['where']
    assert 'user_message' in err_body and exc_cls.__name__ in err_body['user_message']
    assert 'what_to_do' in err_body and err_body['request_id'] in err_body['what_to_do']
    assert 'diagnostic_cmd' in err_body and err_body['request_id'] in err_body['diagnostic_cmd']
    assert 'secret_token=xyz987' not in json.dumps(err_body)
    assert 'unexpected_missing_dict_key' not in json.dumps(err_body)
    assert 'Traceback' not in json.dumps(err_body)

    # The promise behind diagnostic_cmd: exactly this request_id is findable in Cloud Logging, with the traceback.
    new_records = cx.log_capture.records[log_before:]
    matching = [rec for rec in new_records if err_body['request_id'] in rec.getMessage() and rec.exc_info]
    assert matching, 'VL-CODE-500 must emit an ERROR log record that carries the request_id and the traceback'
    tb_text = ''.join(traceback.format_exception(*matching[0].exc_info))
    assert exc_cls.__name__ in tb_text
    assert exc_msg in matching[0].getMessage() or exc_msg in tb_text, 'engineer-facing detail must be in the log'
    assert 'textPayload:"' + err_body['request_id'] + '"' in err_body['diagnostic_cmd']
    cx.extra_counts['log_correlation_verified'] += 1

    health_after = client.get('/api/health')
    assert health_after.status_code == 200 and health_after.json()['status'] == 'ok'
    counts['internal_code_error'] += 1
    cx.pillar_counts['p10_full_stack_fault_injection'] += 1


def _assert_live_policy_covers_all_post_routes(app: Any) -> list[str]:
  """Every POST route on the app must be classified in LIVE_MODE_POLICY (fails loudly for new routes)."""
  post_routes = sorted({
      getattr(route, 'path', '')
      for route in app.routes
      if 'POST' in (getattr(route, 'methods', None) or set())
  })
  unclassified = [p for p in post_routes if p not in LIVE_MODE_POLICY]
  assert not unclassified, (
      f'New mutating routes must be classified guarded/allowed in LIVE_MODE_POLICY: {unclassified}'
  )
  stale = [p for p in LIVE_MODE_POLICY if p not in post_routes]
  assert not stale, f'LIVE_MODE_POLICY lists routes that no longer exist: {stale}'
  return post_routes


def run_stress_loop(total_iterations: int = 1000, seed: int = 42) -> dict[str, Any]:
  """Runs >= `total_iterations` across backend, frontend & all 10 deep verification pillars, hermetically."""
  rng = random.Random(seed)
  t0 = time.perf_counter()

  ui_iterations = max(200, total_iterations // 5)
  py_iterations = max(800, total_iterations - ui_iterations)

  for log_name in ('httpx', 'finops_dashboard.telemetry', 'finops_dashboard.audit_sink', 'finops-mcp', 'finops_dashboard.fleet', 'finops_dashboard.validator'):
    logging.getLogger(log_name).setLevel(logging.CRITICAL)
  # Capture (instead of silencing) the server's ERROR records: bucket 5 asserts the request_id correlation.
  server_logger = logging.getLogger('finops_dashboard.server')
  prev_level, prev_propagate = server_logger.level, server_logger.propagate
  log_capture = _RecordCapture()
  server_logger.setLevel(logging.ERROR)
  server_logger.propagate = False
  server_logger.addHandler(log_capture)

  cached_html = server.ui_template.render_dashboard_html()
  html_patcher = mock.patch.object(server.ui_template, 'render_dashboard_html', return_value=cached_html)
  judge_patcher = mock.patch.object(telemetry_validator, 'run_llm_as_judge_audit', side_effect=_fake_llm_judge)
  html_patcher.start()
  judge_patcher.start()

  counts = {
      'happy_path': 0,
      'edge_fuzz': 0,
      'validation_error': 0,
      'live_mode_guardrail': 0,
      'internal_code_error': 0,
  }
  latencies_ms: list[float] = []
  failures: list[str] = []
  guard = NetworkGuard()
  cx: _LoopContext | None = None
  ui_report: dict[str, Any] = {'total': 0, 'passed': 0, 'counts': {}}
  ghost_report: dict[str, Any] = {'checks': 0, 'failed': 0}
  post_routes: list[str] = []

  try:
    with guard:
      ctrl = _configure_in_memory_controller()
      base_state = ctrl.get_state_payload(include_fleet=True)
      # Same composition order as app/fast_api_app.py on Cloud Run: MCP routes first, then dashboard/API routes.
      test_app = server.fastapi.FastAPI()
      mcp_server.register_mcp_routes(test_app)
      server.register_api_routes(test_app, ctrl)
      post_routes = _assert_live_policy_covers_all_post_routes(test_app)
      client = TestClient(test_app, raise_server_exceptions=False)
      thread_clients = [TestClient(test_app, raise_server_exceptions=False) for _ in range(8)]
      deck = FuzzDeck(WEIRD_NUMBERS, rng)
      cx = _LoopContext(rng, ctrl, base_state, client, thread_clients, deck, log_capture)

      for i in range(py_iterations):
        step_t0 = time.perf_counter()
        bucket = i % 5
        try:
          _run_pillar_check(pillar=i % 8, step_idx=i // 8, cx=cx)
          _run_bucket_check(i, bucket, cx, counts)
        except Exception as exc:  # pylint: disable=broad-except
          failures.append(f'Iteration {i} (bucket {bucket}, pillar {i % 8}): {type(exc).__name__}: {exc}')
        finally:
          latencies_ms.append((time.perf_counter() - step_t0) * 1000.0)

      # Pillar 9: pure VL.* fuzz + the REAL renderer in a ghost DOM (both offline, both under the socket guard).
      try:
        ui_report = run_node_ui_error_and_fuzz_suite(iterations=ui_iterations)
      except Exception as exc:  # pylint: disable=broad-except
        failures.append(f'Node VL.* fuzz suite: {type(exc).__name__}: {exc}')
      try:
        ghost_report = run_ghost_dom_real_renderer_suite(
            cached_html, ctrl.get_state_payload(include_fleet=False), iterations=max(7, ui_iterations // 50))
      except Exception as exc:  # pylint: disable=broad-except
        failures.append(f'Ghost-DOM real renderer suite: {type(exc).__name__}: {exc}')
  finally:
    judge_patcher.stop()
    html_patcher.stop()
    server_logger.removeHandler(log_capture)
    server_logger.setLevel(prev_level)
    server_logger.propagate = prev_propagate

  # Self-checks on the loop itself: hermeticity and fuzz coverage are part of the pass/fail verdict.
  for attempt in guard.attempts:
    failures.append(f'HERMETICITY VIOLATION: network attempt {attempt}')
  fuzz_gaps = cx.deck.gaps() if cx else []
  for gap in fuzz_gaps:
    failures.append(f'FUZZ COVERAGE GAP: {gap}')

  pillar_counts = dict(cx.pillar_counts) if cx else {}
  extra_counts = dict(cx.extra_counts) if cx else {}
  pillar_counts['p9_node_ui_deep_fuzz'] = int(ui_report.get('total', 0)) + int(ghost_report.get('checks', 0))
  elapsed_s = time.perf_counter() - t0
  latencies_sorted = sorted(latencies_ms)
  p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0.0
  p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0.0
  p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)] if latencies_sorted else 0.0

  iteration_failures = [f for f in failures if f.startswith('Iteration ')]
  total_executed = py_iterations + int(ui_report.get('total', 0)) + int(ghost_report.get('checks', 0))
  total_passed = (
      (py_iterations - len(iteration_failures))
      + int(ui_report.get('passed', 0))
      + (int(ghost_report.get('checks', 0)) - int(ghost_report.get('failed', 0)))
  )

  return {
      'total_iterations': total_executed,
      'total_passed': total_passed,
      'total_failed': len(failures),
      'elapsed_seconds': round(elapsed_s, 3),
      'python_iterations': py_iterations,
      'node_ui_iterations': ui_report.get('total', 0),
      'ghost_dom_real_renderer_checks': ghost_report.get('checks', 0),
      'backend_category_counts': counts,
      'frontend_category_counts': ui_report.get('counts', {}),
      'deep_pillar_counts': pillar_counts,
      'extra_counts': extra_counts,
      'hermetic': {'network_attempts': len(guard.attempts), 'samples': guard.attempts[:5]},
      'fuzz_coverage': cx.deck.coverage_report() if cx else {},
      'fuzz_coverage_gaps': fuzz_gaps,
      'live_policy_post_routes': post_routes,
      'latency_ms': {
          'p50': round(p50, 3),
          'p95': round(p95, 3),
          'p99': round(p99, 3),
      },
      'failures': failures,
  }


def main() -> int:
  parser = argparse.ArgumentParser(description='Run 10,000+ iteration FinOps Dashboard deep stress & error-contract loop.')
  parser.add_argument('--iterations', type=int, default=10000, help='Total iterations to execute (default: 10000)')
  parser.add_argument('--seed', type=int, default=42, help='Random seed for reproducible fuzzing')
  args = parser.parse_args()

  report = run_stress_loop(total_iterations=max(1000, args.iterations), seed=args.seed)
  print(json.dumps(report, indent=2))
  return 0 if report['total_failed'] == 0 else 1


if __name__ == '__main__':
  sys.exit(main())
