"""Comprehensive offline unit tests to maximize statement & branch coverage across VibeLift."""

import asyncio
import io
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests import test_fleet as ge_fleet_test
from vibelift import (
    billing_export,
    finops,
    fleet,
    gcp_telemetry,
    ge_mart,
    long_running_agent,
    mcp_server,
    optimizer,
    server,
    sme_eval,
    telemetry,
    validator,
)
from vibelift.ui import template as ui_template

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()


def setUpModule() -> None:
    server._global_controller.ge_fleet = _FAKE_FLEET
    fleet._SERVICE = _FAKE_FLEET


class _MockHttpResponse:
    """Context-manager mock for urllib.request.urlopen responses."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        if isinstance(payload, (bytes, bytearray)):
            self._bytes = bytes(payload)
        elif isinstance(payload, str):
            self._bytes = payload.encode('utf-8')
        else:
            self._bytes = json.dumps(payload).encode('utf-8')

    def read(self) -> bytes:
        return self._bytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _bq_rest_payload(rows: list[dict[str, object]], job_complete: bool = True) -> dict[str, object]:
    if not rows:
        return {'jobComplete': job_complete, 'schema': {'fields': []}, 'rows': []}
    keys = list(rows[0].keys())
    return {
        'jobComplete': job_complete,
        'schema': {'fields': [{'name': k} for k in keys]},
        'rows': [{'f': [{'v': None if r.get(k) is None else str(r.get(k))} for k in keys]} for r in rows],
    }


class TestBillingExportFullCoverage(unittest.TestCase):
    """Covers BillingExportReader, _run, caching, non-blocking refresh, and edge cases."""

    def test_summarize_rows_invalid_float_and_empty(self) -> None:
        res = billing_export.summarize_rows(
            [{'gross_usd': 'not-a-float', 'credits_usd': None, 'usage_amount': 12.5, 'usage_unit': 'requests'}],
            'proj-abc123.ds.tbl',
        )
        self.assertEqual(res['status'], 'LIVE')
        self.assertEqual(res['sku_ledger'][0]['billing_export_gross_usd'], 0.0)

        empty_res = billing_export.summarize_rows([], 'proj-abc123.ds.tbl')
        self.assertIn('No billed usage', str(empty_res['message']))

    def test_billing_export_reader_get_and_daily_costs(self) -> None:
        table = 'project-maui.billing_ds.gcp_billing_export_v1_012345'
        with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: table}):
            reader = billing_export.BillingExportReader('project-maui', lambda: 'fake-token')
            bq_rows = [
                {
                    'service': 'Vertex AI',
                    'sku_id': 'SKU-1',
                    'sku_description': 'Gemini 2.5 Flash Input',
                    'gross_usd': '14.50',
                    'credits_usd': '-2.25',
                    'usage_amount': '100000',
                    'usage_unit': 'tokens',
                    'currency': 'USD',
                    'last_usage': '2026-09-29T10:00:00Z',
                    'day': '2026-09-29',
                }
            ]
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse(_bq_rest_payload(bq_rows))):
                # Blocking get() and cache hit
                data1 = reader.get(non_blocking=False)
                self.assertEqual(data1['status'], 'LIVE')
                data2 = reader.get(non_blocking=False)
                self.assertIs(data1, data2)

                # Blocking get_daily_ai_costs() and cache hit
                daily1 = reader.get_daily_ai_costs(non_blocking=False, window_days=30)
                self.assertEqual(daily1['status'], 'LIVE')
                daily2 = reader.get_daily_ai_costs(non_blocking=False, window_days=30)
                self.assertIs(daily1, daily2)

    def test_billing_export_reader_non_blocking_and_errors(self) -> None:
        table = 'project-maui.billing_ds.gcp_billing_export_v1_012345'
        with mock.patch.dict(os.environ, {billing_export.BILLING_TABLE_ENV: table}):
            # 1. No token available -> ERROR status
            reader_no_tok = billing_export.BillingExportReader('project-maui', lambda: None)
            res_no_tok = reader_no_tok.get(non_blocking=False)
            self.assertEqual(res_no_tok['status'], 'ERROR')
            self.assertIn('No Google Cloud credentials', str(res_no_tok['message']))

            daily_no_tok = reader_no_tok.get_daily_ai_costs(non_blocking=False)
            self.assertEqual(daily_no_tok['status'], 'ERROR')

            # 2. Non-blocking initial call returns LOADING and spawns background thread
            reader_nb = billing_export.BillingExportReader('project-maui', lambda: 'tok')
            with mock.patch.object(reader_nb, '_refresh'), mock.patch.object(reader_nb, '_refresh_daily'):
                loading1 = reader_nb.get(non_blocking=True)
                self.assertEqual(loading1['status'], 'LOADING')
                loading2 = reader_nb.get(non_blocking=True)
                self.assertEqual(loading2['status'], 'LOADING')

                dloading1 = reader_nb.get_daily_ai_costs(non_blocking=True)
                self.assertEqual(dloading1['status'], 'LOADING')
                dloading2 = reader_nb.get_daily_ai_costs(non_blocking=True)
                self.assertEqual(dloading2['status'], 'LOADING')

            # 3. _run with jobComplete=False
            reader_inc = billing_export.BillingExportReader('project-maui', lambda: 'tok')
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse({'jobComplete': False})):
                res_inc = reader_inc.get(non_blocking=False)
                self.assertEqual(res_inc['status'], 'ERROR')
                self.assertIn('did not finish', str(res_inc['message']))

            # 4. _run with HTTPError (JSON error body and non-JSON error body)
            err_body = io.BytesIO(json.dumps({'error': {'message': 'Quota exceeded'}}).encode('utf-8'))
            http_err = urllib.error.HTTPError('https://bq', 403, 'Forbidden', {}, err_body)
            with mock.patch('urllib.request.urlopen', side_effect=http_err):
                rows, err_msg = reader_inc._run('SELECT 1')
                self.assertEqual(rows, [])
                self.assertIn('BigQuery HTTP 403: Quota exceeded', str(err_msg))

            bad_err_body = io.BytesIO(b'not-json')
            http_err2 = urllib.error.HTTPError('https://bq', 500, 'Internal', {}, bad_err_body)
            with mock.patch('urllib.request.urlopen', side_effect=http_err2):
                rows, err_msg = reader_inc._run('SELECT 1')
                self.assertEqual(rows, [])
                self.assertIn('BigQuery HTTP 500', str(err_msg))

            # 5. Exception inside _refresh and _refresh_daily
            reader_exc = billing_export.BillingExportReader('project-maui', lambda: 'tok')
            with mock.patch.object(reader_exc, '_query', side_effect=RuntimeError('boom')):
                reader_exc._refresh(table)
                self.assertEqual(reader_exc._cache['status'], 'ERROR')
            with mock.patch.object(reader_exc, '_run', side_effect=RuntimeError('daily boom')):
                reader_exc._refresh_daily(table, 30)
                self.assertEqual(reader_exc._daily_cache['status'], 'ERROR')

    def test_summarize_daily_cost_rows_and_join_edge_cases(self) -> None:
        summary = billing_export.summarize_daily_cost_rows(
            [
                {'day': '', 'gross_usd': '1.0', 'credits_usd': '0.0', 'service': 'Vertex AI'},
                {'day': '2026-09-28', 'gross_usd': 'bad-num', 'credits_usd': '0.0', 'service': 'Vertex AI'},
                {'day': '2026-09-27', 'gross_usd': '10.0', 'credits_usd': '-1.0', 'service': 'Vertex AI', 'currency': 'USD'},
                {'day': '2026-09-29', 'gross_usd': '12.0', 'credits_usd': '-2.0', 'service': 'Gemini Enterprise', 'currency': 'USD'},
            ],
            'project-maui.ds.tbl',
        )
        self.assertEqual(len(summary['days']), 2)
        joined = billing_export.join_daily_usage_with_cost(
            [
                {'day': '2026-09-27', 'interactions': 100, 'total_tokens': 50000},
                {'day': '2026-09-28', 'interactions': 50, 'total_tokens': 20000},  # between first & last billed -> 0.0
                {'day': '2026-09-30', 'interactions': 10, 'total_tokens': 5000},   # after last_billed -> None
            ],
            summary,
        )
        self.assertEqual(joined[0]['ai_net_usd'], 9.0)
        self.assertEqual(joined[1]['ai_net_usd'], 0.0)
        self.assertIsNone(joined[2]['ai_net_usd'])


class TestGcpTelemetryFullCoverage(unittest.TestCase):
    """Covers GoogleCloudTelemetryService live branches with mocked REST/Cloud Logging calls."""

    def test_project_and_region_discovery_fallbacks(self) -> None:
        with mock.patch.dict(os.environ, {'VIBELIFT_MONITORED_SERVICES': 'svc-a, svc-b, vibe-lift-agent'}, clear=False):
            names = gcp_telemetry.get_monitored_service_names()
            self.assertIn('svc-a', names)
            self.assertIn('svc-b', names)

        # Metadata server discovery for project and region
        env_clean = {k: v for k, v in os.environ.items() if k not in ('GOOGLE_CLOUD_PROJECT', 'GCP_PROJECT', 'GOOGLE_CLOUD_REGION', 'REGION')}
        with mock.patch.dict(os.environ, env_clean, clear=True):
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse('metadata-proj-123', status=200)):
                self.assertEqual(gcp_telemetry.get_current_gcp_project(), 'metadata-proj-123')

            with mock.patch('urllib.request.urlopen', side_effect=OSError('no metadata')):
                proc_mock = mock.MagicMock(stdout='gcloud-proj-456\n', returncode=0)
                with mock.patch('subprocess.run', return_value=proc_mock):
                    self.assertEqual(gcp_telemetry.get_current_gcp_project(), 'gcloud-proj-456')

                with mock.patch('subprocess.run', side_effect=OSError('no gcloud')):
                    self.assertEqual(gcp_telemetry.get_current_gcp_project(), gcp_telemetry.UNCONFIGURED_PROJECT_ID)

            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse('projects/123/zones/us-east4-a', status=200)):
                self.assertEqual(gcp_telemetry.get_current_gcp_region(), 'us-east4')

        with mock.patch.dict(os.environ, {'GOOGLE_CLOUD_REGION': 'europe-west1'}):
            self.assertEqual(gcp_telemetry.get_current_gcp_region(), 'europe-west1')

    def test_bigquery_client_and_access_token_fallbacks(self) -> None:
        svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='project-maui', region='us-central1')
        # bigquery_client property
        _ = svc.bigquery_client

        # _get_access_token cached _cli_token
        svc._cli_token = 'cached-cli-tok'
        svc._cli_token_ts = time.monotonic()
        self.assertEqual(svc._get_access_token(), 'cached-cli-tok')

        # _get_access_token via ADC credentials
        svc._cli_token = None
        fake_creds = mock.MagicMock()
        fake_creds.valid = False
        fake_creds.token = 'adc-refreshed-token'
        with mock.patch.object(gcp_telemetry, 'google_auth') as m_auth, mock.patch.object(gcp_telemetry, 'GoogleAuthRequest', return_value='req'):
            m_auth.default.return_value = (fake_creds, 'project-maui')
            tok = svc._get_access_token()
            self.assertEqual(tok, 'adc-refreshed-token')
            fake_creds.refresh.assert_called_once()

        # _get_access_token via gcloud CLI fallback when ADC fails
        svc._credentials = None
        with mock.patch.object(gcp_telemetry, 'google_auth') as m_auth:
            m_auth.default.side_effect = RuntimeError('no adc')
            proc_ok = mock.MagicMock(returncode=0, stdout='cli-token-xyz\n')
            with mock.patch('subprocess.run', return_value=proc_ok):
                self.assertEqual(svc._get_access_token(), 'cli-token-xyz')

            svc._cli_token = None
            with mock.patch('subprocess.run', side_effect=OSError('gcloud failed')):
                self.assertIsNone(svc._get_access_token())

    def test_query_bigquery_rest_and_cloud_run_services(self) -> None:
        svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='project-maui', region='us-central1')
        svc._cli_token = 'valid-token'
        svc._cli_token_ts = time.monotonic()

        # _query_bigquery_rest: rows returned, empty rows, and exception
        payload = _bq_rest_payload([{'service_name': 'vibe-lift-agent', 'latest_rev': 'vibe-lift-agent-00018', 'req_count': '42', 'avg_latency_ms': '120.5', 'p95_latency_ms': '210.0'}])
        with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse(payload)):
            rows = svc._query_bigquery_rest('SELECT 1')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['service_name'], 'vibe-lift-agent')

        with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse({'jobComplete': True})):
            self.assertEqual(svc._query_bigquery_rest('SELECT 1'), [])

        with mock.patch('urllib.request.urlopen', side_effect=OSError('network down')):
            self.assertEqual(svc._query_bigquery_rest('SELECT 1'), [])

        # list_cloud_run_agent_services: live Admin API + BQ enrichment
        admin_resp = {
            'uri': 'https://vibe-lift-agent-xyz.a.run.app',
            'latestReadyRevision': 'projects/project-maui/locations/us-central1/services/vibe-lift-agent/revisions/vibe-lift-agent-00018',
            'template': {'scaling': {'minInstanceCount': 1, 'maxInstanceCount': 5}, 'maxInstanceRequestConcurrency': 80},
            'terminalCondition': {'state': 'CONDITION_SUCCEEDED'},
        }
        with mock.patch('urllib.request.urlopen', side_effect=[_MockHttpResponse(admin_resp), _MockHttpResponse(payload)]):
            services = svc.list_cloud_run_agent_services(force_refresh=True)
            self.assertEqual(services[0]['status'], 'READY')
            self.assertEqual(services[0]['active_revision'], 'vibe-lift-agent-00018')
            self.assertEqual(services[0]['requests_observed'], 42)

        # list_cloud_run_agent_services: non_blocking when cache is empty and slow
        svc._cached_services = None
        with mock.patch.object(svc, '_get_access_token', side_effect=lambda: (time.sleep(0.1), None)[1]):
            nb_services = svc.list_cloud_run_agent_services(force_refresh=False, non_blocking=True)
            self.assertTrue(len(nb_services) >= 1)

    def test_support_telemetry_and_fleet_summary(self) -> None:
        svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='project-maui', region='us-central1')
        mart_turn_row = {
            'turn_id': 't-100',
            'turn_source': 'ASSISTANT',
            'turn_kind': 'CHAT',
            'ts': '2026-09-29T12:00:00Z',
            'trace_id': 'tr-1',
            'session_id': 'sess-1',
            'user_email': 'enriq@google.com',
            'engine_key': 'global/eng-1',
            'agent_name': 'sre_agent',
            'model_name': 'gemini-2.5-flash',
            'api_method': 'StreamAssist',
            'turn_status': 'SUCCESS',
            'is_actionable_issue': 'false',
            'status_code': '200',
            'status_message': '',
            'error_reason': '',
            'audit_match_method': 'EXACT',
            'is_guardrail_blocked': 'true',
            'guardrail_categories': 'PII',
        }
        with mock.patch.object(svc, '_query_bigquery_rest', return_value=[mart_turn_row]):
            evts = svc.fetch_gemini_enterprise_support_telemetry(limit=5, force_refresh=True)
            self.assertEqual(len(evts), 1)
            self.assertEqual(evts[0]['category'], 'GUARDRAIL_BLOCK')
            # Cached hit
            self.assertEqual(len(svc.fetch_gemini_enterprise_support_telemetry(limit=5)), 1)

        # non_blocking support telemetry
        svc._cached_support_events = None
        with mock.patch.object(svc, '_query_bigquery_rest', return_value=[mart_turn_row]):
            nb_evts = svc.fetch_gemini_enterprise_support_telemetry(limit=5, non_blocking=True)
            self.assertIsInstance(nb_evts, list)

        # Invalid project ID in support telemetry & fleet summary
        bad_svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='INVALID_PROJ', region='us-central1')
        self.assertEqual(bad_svc.fetch_gemini_enterprise_support_telemetry(force_refresh=True), [])
        self.assertIsNone(bad_svc.fetch_bigquery_fleet_summary())

        # fetch_bigquery_fleet_summary with mart daily rows
        daily_rows = [
            {
                'day': '2026-09-29',
                'engine_key': 'global/eng-1',
                'agent_name': 'sre_agent',
                'model_name': 'gemini-2.5-flash',
                'interactions': '10',
                'chat_turns': '8',
                'searches': '2',
                'agent_calls': '8',
                'sessions': '3',
                'active_users': '2',
                'failed_turns': '0',
                'guardrail_blocks': '0',
                'turns_with_tokens': '8',
                'input_tokens': '20000',
                'output_tokens': '2500',
                'cached_input_tokens': '15000',
                'reasoning_tokens': '500',
                'total_tokens': '22500',
                'llm_calls': '10',
            }
        ]
        with mock.patch.object(svc, '_query_bigquery_rest', return_value=daily_rows):
            summary = svc.fetch_bigquery_fleet_summary(hours_ago=168)
            self.assertIsNotNone(summary)
            self.assertEqual(summary['total_turns'], 10)
            self.assertEqual(summary['aggregate_cache_hit_ratio'], 75.0)

    def test_fetch_live_cloud_turns_all_three_tiers(self) -> None:
        svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='project-maui', region='us-central1')

        # Tier 1: GE mart turns with tokens
        mart_rows = [
            {'turn_id': 't-skip', 'input_tokens': None, 'output_tokens': None},
            {
                'turn_id': 't-1',
                'turn_source': 'ASSISTANT',
                'ts': '2026-09-29T10:00:00Z',
                'agent_name': 'sre_agent',
                'model_name': 'gemini-2.5-flash',
                'input_tokens': '12000',
                'output_tokens': '800',
                'cached_input_tokens': '9000',
                'reasoning_tokens': '200',
                'turn_status': 'SUCCESS',
                'tool_names': 'search_logs',
            },
        ]
        with mock.patch.object(svc, '_query_bigquery_rest', return_value=mart_rows):
            turns1 = svc.fetch_live_cloud_turns(force_refresh=True)
            self.assertEqual(len(turns1), 1)
            self.assertEqual(turns1[0].prompt_token_count, 12000)

        # Tier 1b: OTel GenAI turns when mart returns empty
        otel_rows = [
            {'input_tokens': '0', 'output_tokens': '0'},
            {
                'ts': '2026-09-29T11:00:00Z',
                'agent_name': 'sre_triage_root_agent',
                'user_id': 'enriq@google.com',
                'conv_id': 'conv-12345678',
                'input_tokens': '8500',
                'output_tokens': '450',
                'sys_ref': 'gs://bucket/sys_hash_abc123456789',
            },
        ]
        with mock.patch.object(svc, '_query_bigquery_rest', side_effect=[[], otel_rows]):
            turns2 = svc.fetch_live_cloud_turns(force_refresh=True)
            self.assertEqual(len(turns2), 1)
            self.assertEqual(turns2[0].agent_name, 'sre_triage_root_agent')

        # Tier 2: Cloud Logging entries when BigQuery returns empty
        fake_entry_empty = mock.MagicMock(payload=None)
        fake_entry_no_tok = mock.MagicMock(payload={'message': 'hello'}, timestamp=None, resource=None)
        fake_res = mock.MagicMock(labels={'service_name': 'vibe-lift-agent'})
        fake_entry_valid = mock.MagicMock(
            payload={
                'model': 'gemini-2.5-flash',
                'usage_metadata': {
                    'prompt_token_count': 15000,
                    'cached_content_token_count': 12000,
                    'candidates_token_count': 600,
                    'thoughts_token_count': 150,
                },
            },
            timestamp=None,
            resource=fake_res,
        )
        fake_logging_client = mock.MagicMock()
        fake_logging_client.list_entries.return_value = [fake_entry_empty, fake_entry_no_tok, fake_entry_valid]
        svc._logging_client = fake_logging_client
        with mock.patch.object(svc, '_query_bigquery_rest', return_value=[]):
            turns3 = svc.fetch_live_cloud_turns(force_refresh=True)
            self.assertEqual(len(turns3), 1)
            self.assertEqual(turns3[0].prompt_token_count, 15000)

    def test_refresh_ge_mart_and_live_bigquery_project_insights(self) -> None:
        svc = gcp_telemetry.GoogleCloudTelemetryService(project_id='project-maui', region='us-central1')
        svc._cli_token = 'valid-token'
        svc._cli_token_ts = time.monotonic()

        with mock.patch.object(svc, '_query_bigquery_rest', side_effect=[[], [{'row_count': '128', 'refreshed_at': '2026-09-29T12:00:00Z'}]]):
            ref = svc.refresh_ge_mart_turns()
            self.assertEqual(ref['status'], 'REFRESHED')
            self.assertEqual(ref['row_count'], 128)

        # Full fetch_live_bigquery_project_insights synthesis across all 9 parallel queries
        def _fake_bq_router(sql: str, timeout_s: float = 7.0) -> list[dict[str, object]]:
            if 'v_consolidated_audit_log' in sql:
                return [
                    {'principal': 'enriq@google.com', 'method_name': 'ConversationalSearchService.StreamAssist', 'engine_key': 'global/eng-1', 'call_count': '15', 'last_seen': '2026-09-29T12:00:00Z'},
                    {'principal': 'sa@project-maui.iam.gserviceaccount.com', 'method_name': 'EngineService.GetEngine', 'engine_key': '/', 'call_count': '5', 'last_seen': '2026-09-29T11:00:00Z'},
                ]
            if 'GROUP BY user_email, engine_key' in sql:
                return [
                    {'user_email': 'enriq@google.com', 'engine_key': 'global/eng-1', 'interactions': '12', 'sessions': '4', 'total_tokens': '48000', 'reasoning_tokens': '1200', 'last_seen': '2026-09-29T12:05:00Z'},
                ]
            if 'FROM `project-maui.vibelift_mart.fct_turns`' in sql and 'GROUP BY event_date' in sql:
                return [
                    {'day': '2026-09-29', 'interactions': '12', 'chat_turns': '10', 'searches': '2', 'agent_calls': '5', 'sessions': '4', 'active_users': '2', 'failed_turns': '0', 'guardrail_blocks': '0', 'turns_with_tokens': '10', 'input_tokens': '40000', 'output_tokens': '8000', 'cached_input_tokens': '30000', 'reasoning_tokens': '1200', 'total_tokens': '48000', 'refreshed_at': '2026-09-29T12:00:00Z'},
                ]
            if 'FROM `project-maui.vibelift_mart.fct_turns`' in sql:
                return [
                    {'turn_id': 't-1', 'turn_source': 'ASSISTANT', 'turn_kind': 'CHAT', 'ts': '2026-09-29T12:00:00Z', 'session_id': 's-1', 'user_email': 'enriq@google.com', 'engine_key': 'global/eng-1', 'agent_name': 'sre_agent', 'model_name': 'gemini-2.5-flash', 'api_method': 'StreamAssist', 'turn_status': 'SUCCESS', 'input_tokens': '4000', 'output_tokens': '500', 'cached_input_tokens': '3000', 'reasoning_tokens': '100', 'total_tokens': '4500', 'llm_calls': '1', 'tool_call_count': '1', 'tool_names': 'fetch_logs'},
                ]
            if 'agg_daily_usage' in sql:
                return [
                    {'day': '2026-09-29', 'engine_key': 'global/eng-1', 'agent_name': 'sre_agent', 'model_name': 'gemini-2.5-flash', 'interactions': '12', 'chat_turns': '10', 'searches': '2', 'agent_calls': '5', 'sessions': '4', 'active_users': '2', 'failed_turns': '0', 'guardrail_blocks': '0', 'turns_with_tokens': '10', 'input_tokens': '40000', 'output_tokens': '8000', 'cached_input_tokens': '30000', 'reasoning_tokens': '1200', 'total_tokens': '48000', 'llm_calls': '12'},
                ]
            if 'fct_sessions' in sql:
                return [
                    {'engine_key': 'global/eng-1', 'session_id': 's-1', 'session_start': '2026-09-29T11:50:00Z', 'session_end': '2026-09-29T12:00:00Z', 'duration_seconds': '600', 'session_date': '2026-09-29', 'user_email': 'enriq@google.com', 'agent_name': 'sre_agent', 'model_names': 'gemini-2.5-flash', 'turns': '3', 'chat_turns': '3', 'failed_turns': '0', 'actionable_issues': '0', 'guardrail_blocks': '0', 'turns_with_tokens': '3', 'input_tokens': '12000', 'output_tokens': '1500', 'cached_input_tokens': '9000', 'reasoning_tokens': '300', 'total_tokens': '13500', 'llm_calls': '3', 'tool_calls': '2', 'tool_failures': '0'},
                ]
            if 'sre_triage_agent_telemetry' in sql:
                return [
                    {'event_id': 'ev-1', 'ts': '2026-09-29T11:55:00Z', 'user_id': 'cli-user', 'agent_name': 'sre_triage_root_agent', 'conversation_id': 'c-1', 'input_tokens': '5000', 'output_tokens': '600', 'output_gcs_uri': 'gs://real-bucket/out.jsonl', 'tool_defs_json': json.dumps([{'name': 'lookup_incident'}]), 'engine_id': '998877'},
                    {'event_id': 'ev-2', 'ts': '2026-09-29T11:56:00Z', 'user_id': '', 'agent_name': 'sre_triage_root_agent', 'conversation_id': '', 'input_tokens': '1000', 'output_tokens': '100', 'output_gcs_uri': '', 'tool_defs_json': 'invalid-json', 'engine_id': '998877'},
                ]
            if 'run_googleapis_com_requests_' in sql:
                return [
                    {'revision': 'vibe-lift-agent-00018', 'status': '200', 'req_count': '85', 'avg_latency_ms': '140.2', 'max_latency_ms': '450.0'},
                ]
            if 'ds_vertex_agents_raw' in sql:
                return [
                    {'dataset': 'ds_vertex_agents_raw', 'principal': 'admin@google.com', 'method_name': 'ReasoningEngineService.Query', 'call_count': '9', 'last_seen': '2026-09-29T11:30:00Z'},
                ]
            return []

        with mock.patch.object(svc, '_query_bigquery_rest', side_effect=_fake_bq_router):
            insights = svc.fetch_live_bigquery_project_insights(force_refresh=True)
            self.assertIsNotNone(insights)
            self.assertTrue(len(insights['live_power_users_ldap']) >= 3)
            self.assertTrue(len(insights['live_skills_mcp']) >= 2)
            self.assertEqual(insights['cloud_run_requests_total'], 85)
            # Cache hit
            cached_ins = svc.fetch_live_bigquery_project_insights(force_refresh=False)
            self.assertIs(insights, cached_ins)
            # Non-blocking hit
            nb_ins = svc.fetch_live_bigquery_project_insights(force_refresh=False, non_blocking=True)
            self.assertIs(insights, nb_ins)


class TestTelemetryDecoratorsFullCoverage(unittest.TestCase):
    """Covers sync/async @vibelift_telemetry and @with_analytics_logging decorators and live streams."""

    def tearDown(self) -> None:
        telemetry.reset_decorator_events()
        telemetry.reset_aive_logs()

    def test_extract_result_metrics_and_sync_async_vibelift_telemetry(self) -> None:
        m = telemetry._extract_result_metrics({
            'usage_metadata': {
                'prompt_tokens': '20000',
                'cached_tokens': '15000',
                'output_tokens': '500',
                'context_bloat_pct': '11.5',
                'idle_ratio_pct': '4.2',
            }
        })
        self.assertEqual(m[0], 20000)
        self.assertEqual(m[1], 15000)

        # Invalid numeric strings gracefully fall back
        m_bad = telemetry._extract_result_metrics({
            'prompt_tokens': 'bad',
            'cached_tokens': 'bad',
            'output_tokens': 'bad',
            'context_bloat_pct': 'bad',
            'idle_ratio_pct': 'bad',
        })
        self.assertEqual(m_bad[0], 16400)

        @telemetry.vibelift_telemetry(agent_name='test_sync_agent')
        def sync_ok() -> dict[str, int]:
            return {'prompt_tokens': 8000, 'cached_tokens': 6000, 'output_tokens': 300}

        @telemetry.vibelift_telemetry(agent_name='test_sync_err')
        def sync_fail() -> None:
            raise ValueError('sync error')

        @telemetry.vibelift_telemetry(agent_name='test_async_agent')
        async def async_ok() -> dict[str, int]:
            return {'prompt_tokens': 9000, 'cached_tokens': 7500, 'output_tokens': 250}

        @telemetry.vibelift_telemetry(agent_name='test_async_err')
        async def async_fail() -> None:
            raise RuntimeError('async error')

        self.assertEqual(sync_ok()['prompt_tokens'], 8000)
        with self.assertRaises(ValueError):
            sync_fail()
        self.assertEqual(asyncio.run(async_ok())['prompt_tokens'], 9000)
        with self.assertRaises(RuntimeError):
            asyncio.run(async_fail())

        telemetry.set_live_decorator_events([{'handler_name': 'live_bq_span', 'agent_name': 'sre'}])
        evts = telemetry.get_recent_decorator_events()
        self.assertTrue(any(e.get('handler_name') == 'live_bq_span' for e in evts))

    def test_with_analytics_logging_sync_and_async_and_live_aive(self) -> None:
        @telemetry.with_analytics_logging(task_type='REPORT_GEN', agent_name='sync_aive')
        def sync_gen(prompt: str, **kwargs) -> dict[str, object]:
            return {'gcs_uri': 'gs://bucket/report.json', 'total_tokens': 12345}

        @telemetry.with_analytics_logging(task_type='REPORT_GEN', agent_name='sync_aive')
        def sync_gen_fail(prompt: str) -> None:
            raise RuntimeError('gen failed')

        @telemetry.with_analytics_logging(task_type='VIDEO_GEN', agent_name='async_aive')
        async def async_gen(prompt: str, **kwargs) -> dict[str, object]:
            return {'gcs_uri': 'gs://bucket/video.mp4', 'total_tokens': 23456}

        @telemetry.with_analytics_logging(task_type='VIDEO_GEN', agent_name='async_aive')
        async def async_gen_fail(prompt: str) -> None:
            raise ValueError('async gen failed')

        res1 = sync_gen('generate report', session_id='s-100', user_email='enriq@google.com')
        self.assertEqual(res1['total_tokens'], 12345)
        with self.assertRaises(RuntimeError):
            sync_gen_fail('bad prompt')

        res2 = asyncio.run(async_gen('generate video', session_id='s-200', user_email='enriq@google.com'))
        self.assertEqual(res2['total_tokens'], 23456)
        with self.assertRaises(ValueError):
            asyncio.run(async_gen_fail('bad async prompt'))

        telemetry.set_live_aive_logs(
            [{'event_id': 'live-evt-1', 'csat_rating': None}],
            [{'rating_id': 'live-rat-1', 'rating': 5}],
        )
        telemetry.log_csat_rating('s-live', 'live-evt-1', 'enriq@google.com', 4, 'good')
        live_logs = telemetry.get_recent_aive_logs(live_only=True)
        self.assertTrue(any(r.get('event_id') == 'live-evt-1' and r.get('csat_rating') == 4 for r in live_logs['usage_logs']))


class TestValidatorAndSmeEvalFullCoverage(unittest.TestCase):
    """Covers Vertex AI LLM-as-a-Judge branches, flagged checks, and SME evaluation edge cases."""

    def test_validator_vertex_judge_and_retries(self) -> None:
        ctrl = server.VibeLiftRuntimeController()
        state = ctrl.get_state_payload(include_fleet=True)
        # Force live project_id on state for run_llm_as_judge_audit
        live_state = dict(state, gcp_project='project-maui')

        vertex_json = {
            'candidates': [{
                'content': {
                    'parts': [{
                        'text': json.dumps({
                            'verdict': 'PASS_GROUNDED',
                            'grounding_score_100': 98,
                            'executive_finding': 'All metrics verified against BigQuery.',
                        })
                    }]
                }
            }]
        }
        pass_checks = [{'check_id': 'C1', 'tab': 'Tab 1', 'status': 'PASS', 'evidence': 'ok'}]
        fail_checks = [{'check_id': 'C1', 'tab': 'Tab 1', 'status': 'FLAGGED', 'evidence': 'mismatch'}]

        with mock.patch.object(validator, '_get_access_token', return_value=('fake-tok', None)):
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse(vertex_json)):
                rep_pass = validator.run_llm_as_judge_audit(live_state, deterministic_checks=pass_checks)
                self.assertEqual(rep_pass['verdict'], 'VERIFIED_GROUNDED')
                self.assertEqual(rep_pass['grounding_score_100'], 98)

                # When deterministic check fails, LLM judge verdict is overridden to FLAGGED_ISSUES
                rep_fail = validator.run_llm_as_judge_audit(live_state, deterministic_checks=fail_checks)
                self.assertEqual(rep_fail['verdict'], 'FLAGGED_ISSUES')

        # Fallback when _get_access_token returns None (both pass and fail deterministic checks)
        with mock.patch.object(validator, '_get_access_token', return_value=(None, 'no token')):
            fb_pass = validator.run_llm_as_judge_audit(live_state, deterministic_checks=pass_checks)
            self.assertEqual(fb_pass['verdict'], 'VERIFIED_GROUNDED')
            fb_fail = validator.run_llm_as_judge_audit(live_state, deterministic_checks=fail_checks)
            self.assertEqual(fb_fail['verdict'], 'FLAGGED_ISSUES')

        # Test validate_dashboard_state with run_llm_judge=True on test-project
        full_val = validator.validate_dashboard_state(state, run_llm_judge=True)
        self.assertEqual(full_val['overall_status'], 'VERIFIED_GROUNDED')

        # Test _call_vertex_judge error & retry branches
        with mock.patch('time.sleep'):
            err_503 = urllib.error.HTTPError('https://vertex', 503, 'Unavailable', {}, io.BytesIO(b'{"error":{"status":"UNAVAILABLE"}}'))
            with mock.patch('urllib.request.urlopen', side_effect=[err_503, _MockHttpResponse(vertex_json)]):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNotNone(parsed)
                self.assertIsNone(err)

            err_400_bad = urllib.error.HTTPError('https://vertex', 400, 'Bad', {}, io.BytesIO(b'not-json'))
            with mock.patch('urllib.request.urlopen', side_effect=err_400_bad):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNone(parsed)

            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse({'candidates': [{'finishReason': 'SAFETY'}]})):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNone(parsed)
                self.assertIn('finishReason=SAFETY', str(err))

            bad_json_resp = {'candidates': [{'content': {'parts': [{'text': 'not-json'}]}}]}
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse(bad_json_resp)):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNone(parsed)
                self.assertIn('not valid JSON', str(err))

            list_json_resp = {'candidates': [{'content': {'parts': [{'text': '[1, 2, 3]'}]}}]}
            with mock.patch('urllib.request.urlopen', return_value=_MockHttpResponse(list_json_resp)):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNone(parsed)
                self.assertIn('not a JSON object', str(err))

            with mock.patch('urllib.request.urlopen', side_effect=OSError('timeout')):
                parsed, err = validator._call_vertex_judge('project-maui', 'tok', 'prompt')
                self.assertIsNone(parsed)
                self.assertIn('OSError', str(err))

        # Test validator._get_access_token branches
        with mock.patch.object(validator, 'google') as m_goog, mock.patch.object(validator, 'GoogleAuthRequest', return_value='req'):
            creds = mock.MagicMock(valid=False, token='adc-tok')
            m_goog.auth.default.return_value = (creds, 'project-maui')
            tok, err = validator._get_access_token()
            self.assertEqual(tok, 'adc-tok')

            # ADC returns no token -> falls back to gcloud CLI
            creds_none = mock.MagicMock(valid=True, token=None)
            m_goog.auth.default.return_value = (creds_none, 'project-maui')
            with mock.patch('subprocess.run', return_value=mock.MagicMock(returncode=0, stdout='cli-tok\n')):
                tok2, _ = validator._get_access_token()
                self.assertEqual(tok2, 'cli-tok')

            with mock.patch('subprocess.run', side_effect=OSError('no cli')):
                tok3, err3 = validator._get_access_token()
                self.assertIsNone(tok3)
                self.assertIn('gcloud', str(err3))

    def test_sme_eval_explicit_args_and_filter(self) -> None:
        store = sme_eval.SmeEvaluationStore()
        entry = store.submit_rating(
            persona_id='sre_platform',
            reviewer_ldap='sre_reviewer@google.com',
            overall_rating=5,
            verdict='invalid_verdict_defaults_to_approved',
            task_completed=True,
            dimension_ratings={'time_to_insight': 5, 'actionability_control': 'bad'},  # type: ignore[dict-item]
            notes='Tested explicit args path',
        )
        self.assertEqual(entry['verdict'], 'APPROVED')
        self.assertEqual(entry['dimension_ratings']['time_to_insight'], 5)

        # Also test dict payload with invalid overall_rating
        entry2 = store.submit_rating({'persona_id': 'finops_lead', 'overall_rating': 'bad'})
        self.assertEqual(entry2['overall_rating'], 5)

        self.assertEqual(len(store.list_ratings(persona_id='sre_platform')), 1)
        self.assertEqual(len(store.list_ratings(persona_id='cfo_exec')), 0)
        with self.assertRaises(ValueError):
            store.submit_rating(persona_id='nonexistent_persona')
        self.assertEqual(sme_eval._extract_html_ids(''), set())


class TestServerAndHttpHandlerFullCoverage(unittest.TestCase):
    """Covers VibeLiftHttpServer routes, live-mode controller branches, and helper functions."""

    def test_http_server_all_remaining_routes(self) -> None:
        ctrl = server.VibeLiftRuntimeController()
        httpd = server.create_http_server(host='127.0.0.1', port=0, controller=ctrl)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f'http://127.0.0.1:{port}'
        try:
            for path in (
                '/.well-known/agent-card.json',
                '/healthz',
                '/api/tokenomics_cockpit',
                '/api/validate_telemetry?run_llm_judge=false',
                '/api/sme_eval?fresh=true',
            ):
                with urllib.request.urlopen(f'{base}{path}', timeout=5) as resp:
                    self.assertEqual(resp.status, 200)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f'{base}/nonexistent', timeout=5)
            self.assertEqual(ctx.exception.code, 404)

            post_routes = [
                ('/api/validate_telemetry', {'run_llm_judge': False}),
                ('/api/sme_eval/run', {}),
                ('/api/sme_eval/rate', {'persona_id': 'finops_lead', 'overall_rating': 5, 'verdict': 'APPROVED'}),
                ('/api/select_optimizer', {'platform_id': 'alpha_evolve'}),
                ('/api/decorator_ingest', {'prompt_tokens': 'bad', 'latency_ms': 'bad'}),
                ('/api/nl2sql', {'question': 'Show top users'}),
                ('/api/aive_log', {'prompt': 'Test turn'}),
                ('/api/csat_rating', {'rating': 'bad'}),
                ('/api/what_if_simulate', {'thinking_budget_tok': 'bad', 'history_window_turns': 'bad', 'traffic_canary_pct': 'bad'}),
                ('/api/what_if_live', {'kind': 'cache_share', 'model': 'gemini-2.5-flash', 'target_cache_share_pct': 60}),
                ('/api/recompute_finops', {}),
                ('/api/step_turn', {}),
                ('/api/ge_mart/refresh', {}),
                ('/api/reset', {}),
            ]
            for route, body in post_routes:
                req = urllib.request.Request(
                    f'{base}{route}',
                    data=json.dumps(body).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_controller_live_gcp_branches_and_helpers(self) -> None:
        ctrl = server.VibeLiftRuntimeController()
        self.assertEqual(server.base_url_from_headers(object()), '')
        self.assertEqual(server.base_url_from_headers({'Host': 'bad/host'}), '')
        self.assertEqual(
            server.base_url_from_headers({'Host': 'vibe.run.app', 'X-Forwarded-Proto': 'https, http'}),
            'https://vibe.run.app',
        )

        fake_insights = {
            'ge_mart_dataset': 'project-maui.vibelift_mart',
            'ge_curated_dataset': 'project-maui.ds_ge_curated_staging',
            'ge_mart_refreshed_at': '2026-09-29T12:00:00Z',
            'ge_daily_totals': [{'day': '2026-09-29', 'interactions': 10, 'total_tokens': 25000}],
            'ge_daily_by_app': [],
            'ge_sessions': [{'session_id': 's-1', 'turns': 3}],
            'aive_usage_logs': [{'event_id': 'e-1', 'outputs': []}],
            'aive_ratings_logs': [],
            'decorator_events': [{'handler_name': 'h-1'}],
            'power_users_ldap': [],
        }
        with mock.patch.object(ctrl.ge_fleet, 'project_id', 'project-maui'), mock.patch.object(ctrl.gcp_telemetry, 'project_id', 'project-maui'):
            with mock.patch.object(ctrl.gcp_telemetry, 'fetch_live_bigquery_project_insights', return_value=fake_insights):
                with mock.patch.object(ctrl.gcp_telemetry, 'list_cloud_run_agent_services', return_value=[{'service_name': 'vibe-lift-agent'}]):
                    with mock.patch.object(ctrl.gcp_telemetry, 'fetch_gemini_enterprise_support_telemetry', return_value=[]):
                        with mock.patch.object(ctrl.gcp_telemetry, 'refresh_ge_mart_turns', return_value={'status': 'REFRESHED', 'row_count': 10}):
                            state = ctrl.get_state_payload(include_fleet=False)
                            self.assertTrue(state['live_data'])
                            self.assertIsNotNone(state['ge_daily_usage'])
                            self.assertEqual(ctrl.recompute_finops({})['status'], 'DISABLED_IN_LIVE_MODE')
                            self.assertEqual(ctrl.simulate_what_if({})['status'], 'DISABLED_IN_LIVE_MODE')
                            ref_res = ctrl.refresh_ge_mart()
                            self.assertIn('refresh', ref_res)

        # Test what_if_live invalid numeric input
        bad_wi = ctrl.what_if_live({'kind': 'cache_share', 'model': 'gemini-2.5-flash', 'target_cache_share_pct': 'not-a-float'})
        self.assertEqual(bad_wi['status'], 'ERROR')

        # Test sync_gcp_telemetry when live_turns are present
        fake_turn = ctrl.agent.turns[0]
        with mock.patch.object(ctrl.gcp_telemetry, 'fetch_live_cloud_turns', return_value=[fake_turn]):
            synced_state = ctrl.sync_gcp_telemetry()
            self.assertIn('turns', synced_state)

        telemetry.reset_decorator_events()
        telemetry.reset_aive_logs()
        server._global_controller.reset()


class TestFinopsGeMartAndMcpEdgeCases(unittest.TestCase):
    """Covers remaining edge cases in finops.py, ge_mart.py, mcp_server.py, telemetry.py, and template.py."""

    def test_finops_edge_cases(self) -> None:
        self.assertEqual(finops._div(10.0, 0.0), None)
        self.assertEqual(finops._div(10.0, 2.0), 5.0)
        self.assertEqual(finops._models_by_name(None), {})

        dup_agent = {
            'display_name': 'Shared Agent',
            'engine_id': 'eng-1',
            'backend': {'kind': 'ReasoningEngine', 'reasoning_engine': 'projects/p/locations/l/reasoningEngines/1'},
            'metrics': {'requests': 5, 'llm_calls': 5, 'input_tokens': 1000, 'output_tokens': 200},
        }
        fleet_with_ge = {
            'window_hours': 168,
            'model_usage': {'totals': {'invocations': 5, 'input_tokens': 1000, 'output_tokens': 200, 'est_cost_usd': 0.01}, 'models': []},
            'agents': [dup_agent, dict(dup_agent, engine_id='eng-2')],
            'engines': [{'engine_key': 'global/eng-1', 'display_name': 'App 1'}],
            'ge_assistant_usage': {
                'by_engine': {
                    'global/eng-1': {'engine_id': 'eng-1', 'llm_calls': 4, 'input_tokens': 800, 'output_tokens': 150, 'conversations': 2, 'models': ['gemini-2.5-flash'], 'assistant_agents': 1}
                }
            },
        }
        te = finops.build_token_economics(fleet_with_ge)
        self.assertEqual(len(te['agents']), 1)
        self.assertEqual(len(te['ge_assistant']), 1)

        cards = {'gemini-2.5-flash': {'input': 0.3, 'output': 2.5, 'cached_read': 0.03, 'cache_write': 0.3}}
        usage_zero_prompt = {'models': [{'model': 'gemini-2.5-flash', 'input_tokens': 0, 'cache_read_tokens': 0}]}
        res = finops.project_cache_share(usage_zero_prompt, 'gemini-2.5-flash', 50.0, cards)
        self.assertEqual(res['status'], 'ERROR')

        res_missing = finops.project_cache_share(usage_zero_prompt, 'unknown-model', 50.0, cards)
        self.assertEqual(res_missing['status'], 'ERROR')

        res_no_card = finops.project_model_switch(usage_zero_prompt, 'gemini-2.5-flash', 'unpriced-model', 50.0, cards)
        self.assertEqual(res_no_card['status'], 'ERROR')

    def test_ge_mart_and_telemetry_edge_cases(self) -> None:
        self.assertEqual(ge_mart._clamp('bad-int', 1, 100), 1)
        self.assertEqual(ge_mart.int_or_none('not-a-number'), None)
        self.assertEqual(ge_mart.bool_or_none(True), True)
        self.assertEqual(ge_mart.bool_or_none(False), False)

        zero_turn = telemetry.TurnUsageLog(
            timestamp='2026-09-29T00:00:00Z',
            agent_name='test',
            model='gemini-2.5-flash',
            turn_index=1,
            prompt_prefix_hash='h',
            cache_breakpoint_line=1,
            cache_breakpoint_reason='r',
            prompt_token_count=0,
            cached_content_token_count=0,
            cache_creation_input_tokens=0,
            uncached_input_tokens=0,
            candidates_token_count=0,
            thoughts_token_count=0,
            status_code=500,
            tool_called='t',
            evolution_generation=0,
        )
        self.assertEqual(zero_turn.cache_hit_ratio, 0.0)
        self.assertIn('"status_code": 500', zero_turn.to_jsonl())
        summary = telemetry.summarize_log_stream([zero_turn])
        self.assertEqual(summary['error_rate_pct'], 100.0)

        opt = optimizer.VibeLiftAlphaEvolveOptimizer()
        ag = long_running_agent.LongRunningVibeLiftAgent(opt)
        saved_tl = list(opt.active_agent.timeline)
        opt.active_agent.timeline.clear()
        self.assertEqual(ag._current_generation(), 14)
        opt.active_agent.timeline.extend(saved_tl)

    def test_mcp_server_and_template_edge_cases(self) -> None:
        self.assertEqual(mcp_server._parse_focus_tab('not-an-int'), mcp_server.OVERVIEW_TAB)
        self.assertEqual(mcp_server._parse_focus_tab(999), mcp_server.OVERVIEW_TAB)
        self.assertEqual(mcp_server._parse_focus_tab(3), mcp_server.COST_BILLING_TAB)

        with mock.patch.object(mcp_server, '_get_controller', side_effect=RuntimeError('no ctrl')):
            self.assertIn('<!DOCTYPE html>', mcp_server._widget_html())

        res_ui = asyncio.run(mcp_server._dispatch('sess', 'ui/initialize', {}))
        self.assertIn('protocolVersion', res_ui)

        res_tel = asyncio.run(mcp_server._dispatch('sess', 'tools/call', {'name': 'query_project_telemetry', 'arguments': {'force_refresh': True}}))
        self.assertFalse(res_tel['isError'])

        res_ev = asyncio.run(mcp_server._dispatch('sess', 'tools/call', {'name': 'run_alpha_evolve_generation', 'arguments': {'platform_id': 'alpha_evolve'}}))
        self.assertFalse(res_ev['isError'])

        with self.assertRaises(mcp_server._RpcError):
            asyncio.run(mcp_server._dispatch('sess', 'tools/call', {'name': 'nonexistent_tool'}))

        code, _, _ = mcp_server.handle_jsonrpc_sync({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(code, 202)

        code_inv, _, body_inv = mcp_server.handle_jsonrpc_sync('not-a-dict')
        self.assertEqual(code_inv, 200)
        self.assertIn('error', json.loads(body_inv))

        # Test FastAPI /mcp endpoint edge cases (DELETE, GET, invalid JSON, notification, RpcError)
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        self.assertEqual(client.delete('/mcp').status_code, 204)
        self.assertEqual(client.get('/mcp').status_code, 405)
        self.assertEqual(client.post('/mcp', content=b'not-json').status_code, 400)
        self.assertEqual(client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code, 202)
        self.assertEqual(client.post('/mcp', json=['bad-item', {'jsonrpc': '2.0', 'id': 1, 'method': 'unknown_method', 'params': 'bad'}]).status_code, 200)

        html = ui_template.render_dashboard_html(initial_state={'unserializable': object()})
        self.assertIn('<!DOCTYPE html>', html)
        server._global_controller.reset()

    def test_optimizer_and_server_extra_branches(self) -> None:
        opt = optimizer.VibeLiftAlphaEvolveOptimizer()
        # NL2SQL intents: sessions, users/ldap, 5-layer otel
        r_sess = opt.execute_nl2sql_telemetry_query('Show session duration from fct_sessions')
        self.assertIn('generated_sql', r_sess)
        r_user = opt.execute_nl2sql_telemetry_query('Show power user ldap and csat')
        self.assertIn('generated_sql', r_user)
        r_otel = opt.execute_nl2sql_telemetry_query('Show 5-layer otel span metrics')
        self.assertIn('generated_sql', r_otel)

        # select_agent by display name and unknown name
        opt.select_agent('IT Service Desk')
        self.assertEqual(opt.selected_agent_id, 'it_service_desk')
        opt.select_agent('Unregistered Custom Agent')

        # sync_from_ge_fleet with a brand-new custom agent and live_skills
        opt.sync_from_ge_fleet(
            {
                'project_id': 'project-maui',
                'agents': [
                    {
                        'agent_id': 'custom-999',
                        'display_name': 'Custom__Brand__New Agent!',
                        'kind': 'ADK Agent',
                        'state': 'ENABLED',
                        'backend': {'kind': 'ReasoningEngine', 'models': ['gemini-2.5-flash']},
                        'metrics': {'requests': 12, 'llm_calls': 12, 'input_tokens': 24000, 'output_tokens': 3000, 'latency_p95_ms': 420.0},
                    }
                ],
            },
            bq_insights={
                'live_skills_mcp': [{'resource_name': 'adk_tool://custom', 'kind': 'Tool'}],
                'ge_sessions': [{'session_id': 's-1', 'user_email': 'enriq@google.com', 'agent_name': 'Custom', 'turns': 4, 'duration_seconds': 120, 'total_tokens': 5000, 'failed_turns': 0}],
            },
        )
        r_sess_live = opt.execute_nl2sql_telemetry_query('Show session duration from fct_sessions')
        self.assertEqual(len(r_sess_live['rows']), 1)

        # add_user_parameter for context bloat, idle ratio, and updating existing parameter
        opt.add_user_parameter('Context Bloat Ceiling', '%', 'LOWER', 20.0, 8.0, 10)
        opt.add_user_parameter('Context Bloat Ceiling', '%', 'LOWER', 18.0, 6.0, 12)
        opt.add_user_parameter('Idle Ratio Target', '%', 'LOWER', 15.0, 5.0, 10)

        # OptimizationParameter zero baseline delta
        zero_param = optimizer.OptimizationParameter('k', 'Label', '%', 'HIGHER', 0.0, 10.0, 5.0, 10, 'TRACKING')
        self.assertEqual(zero_param.to_dict()['delta_pct'], 0.0)

        # Server background warmer, get_fleet_payload exception handling, and main()
        ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
        with mock.patch('threading.Thread') as m_thread:
            ctrl.start_background_warmer(interval_s=15.0)
            ctrl.start_background_warmer(interval_s=15.0)  # idempotent second call
            m_thread.assert_called_once()

        with mock.patch.object(ctrl.ge_fleet, 'collect', side_effect=RuntimeError('collect boom')):
            err_fleet = ctrl.get_fleet_payload()
            self.assertEqual(err_fleet['source_status'], {'inventory': 'error'})

        with mock.patch.object(server, 'run_standalone_server') as m_run:
            server.main(['prog'])
            m_run.assert_called_once()

        # Test app.fast_api_app fallback when get_fast_api_app fails and __main__ entrypoint
        import importlib
        import runpy

        import app.fast_api_app as fast_api_mod
        with mock.patch('google.adk.cli.fast_api.get_fast_api_app', side_effect=RuntimeError('no adk')):
            importlib.reload(fast_api_mod)
            self.assertIsNotNone(fast_api_mod.app)
        importlib.reload(fast_api_mod)
        with mock.patch('uvicorn.run') as m_uvicorn:
            runpy.run_module('app.fast_api_app', run_name='__main__')
            m_uvicorn.assert_called_once()


if __name__ == '__main__':
    unittest.main()

