"""Ingest validation, the dead-letter record, and the no-raw-prompt-text guarantee.

Covers POST /api/decorator_ingest, /api/aive_log and /api/csat_rating on both the FastAPI app (production)
and the controller, plus the prompt fingerprinting in vibelift.telemetry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

import fastapi
from fastapi.testclient import TestClient

from tests import test_fleet as ge_fleet_test
from vibelift import server, telemetry

_FAKE_FLEET, _ = ge_fleet_test.make_fake_service()
_NO_MEASUREMENTS = (
    'no measurements: send at least one of latency_ms, prompt_tokens, cached_tokens, output_tokens, '
    'context_bloat_pct, idle_ratio_pct'
)
_SECRET_PROMPT = 'Reset the VPN password for jane.doe@example.com, ticket 4471'


def _reset() -> None:
  telemetry.reset_decorator_events()
  telemetry.reset_aive_logs()


class DecoratorValidationTest(unittest.TestCase):

  def test_valid_body_leaves_unreported_fields_none(self) -> None:
    fields, errors = telemetry.validate_decorator_payload(
        {'agent_name': ' it_desk ', 'prompt_tokens': '1200', 'cached_tokens': 900}
    )
    self.assertEqual(errors, [])
    self.assertEqual(fields['agent_name'], 'it_desk')
    self.assertEqual((fields['prompt_tokens'], fields['cached_tokens']), (1200, 900))
    for key in ('latency_ms', 'output_tokens', 'context_bloat_pct', 'idle_ratio_pct', 'model', 'status', 'protocol'):
      self.assertIsNone(fields[key], key)

  def test_every_rule_reports_a_precise_error(self) -> None:
    cases = [
        ({'prompt_tokens': 10}, 'agent_name: required'),
        ({'agent_name': 'a'}, _NO_MEASUREMENTS),
        ({'agent_name': 'a', 'prompt_tokens': 10, 'cached_tokens': 11}, 'cached_tokens: cannot exceed prompt_tokens'),
        ({'agent_name': 'a', 'prompt_tokens': -1}, 'prompt_tokens: must be a finite number >= 0'),
        ({'agent_name': 'a', 'prompt_tokens': True}, 'prompt_tokens: must be a number'),
        ({'agent_name': 'a', 'prompt_tokens': 1.5}, 'prompt_tokens: must be a whole number'),
        ({'agent_name': 'a', 'prompt_tokens': 'bad'}, 'prompt_tokens: must be a number'),
        ({'agent_name': 'a', 'latency_ms': 'nan'}, 'latency_ms: must be a finite number >= 0'),
        ({'agent_name': 'a', 'context_bloat_pct': 101}, 'context_bloat_pct: must be <= 100'),
        ({'agent_name': 'x' * 129, 'latency_ms': 1}, 'agent_name: longer than 128 characters'),
        ({'agent_name': 7, 'latency_ms': 1}, 'agent_name: must be a string'),
    ]
    for body, expected in cases:
      with self.subTest(body=body):
        _, errors = telemetry.validate_decorator_payload(body)
        self.assertIn(expected, errors)

  def test_non_object_bodies(self) -> None:
    for body in (None, [], [1, 2], 'text', 42):
      with self.subTest(body=body):
        self.assertEqual(telemetry.validate_decorator_payload(body), ({}, ['body: must be a JSON object']))


class AiveAndCsatValidationTest(unittest.TestCase):

  def test_aive_rules(self) -> None:
    _, errors = telemetry.validate_aive_payload({})
    self.assertIn('user_email: required', errors)
    self.assertIn('task_type: required', errors)
    self.assertIn('no measurements: send at least one of total_tokens, prompt_tokens, output_tokens, latency_ms', errors)
    base = {'user_email': 'a@example.com', 'task_type': 'CHAT', 'total_tokens': 5}
    _, errors = telemetry.validate_aive_payload({**base, 'prompts': 'not a list'})
    self.assertIn('prompts: must be a list of at most 100 strings', errors)
    _, errors = telemetry.validate_aive_payload({**base, 'outputs': [{'gcs_uri': 'https://x/y'}]})
    self.assertIn('outputs[0].gcs_uri: must start with gs://', errors)
    fields, errors = telemetry.validate_aive_payload(
        {**base, 'prompt': 'one', 'prompts': ['two'], 'outputs': [{'gcs_uri': 'gs://b/o', 'mime_type': 'text/plain'}]}
    )
    self.assertEqual(errors, [])
    self.assertEqual(fields['prompts'], ['one', 'two'])
    self.assertEqual(fields['outputs'], [{'gcs_uri': 'gs://b/o', 'media_type': None, 'mime_type': 'text/plain'}])
    self.assertIsNone(fields['latency_ms'])

  def test_csat_rules(self) -> None:
    for rating in (0, 6, 2.5, 'bad', True, None):
      with self.subTest(rating=rating):
        _, errors = telemetry.validate_csat_payload({'rating': rating, 'session_id': 's'})
        self.assertIn('rating: required, a whole number from 1 to 5', errors)
    _, errors = telemetry.validate_csat_payload({'rating': 5})
    self.assertEqual(errors, ['session_id or event_id: required (the session or event being rated)'])
    fields, errors = telemetry.validate_csat_payload({'rating': '4', 'event_id': 'evt-1'})
    self.assertEqual(errors, [])
    self.assertEqual(fields['rating'], 4)


class ControllerDeadLetterTest(unittest.TestCase):

  def setUp(self) -> None:
    _reset()
    self.addCleanup(_reset)
    self.ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)

  def test_invalid_body_is_rejected_and_dead_lettered_without_values(self) -> None:
    before = telemetry.get_recent_decorator_events()
    body = {'agent_name': 'leaky', 'prompt_tokens': 'bad', 'user_cohort': 'jane.doe@example.com'}
    with self.assertLogs('vibelift.audit_sink', level='WARNING') as logs:
      with self.assertRaises(telemetry.IngestValidationError) as ctx:
        self.ctrl.ingest_decorator_event(body)
    self.assertEqual(ctx.exception.errors, ['prompt_tokens: must be a number'])
    dead = ctx.exception.dead_letter
    canonical = json.dumps(body, sort_keys=True).encode('utf-8')
    self.assertEqual(dead['source'], '/api/decorator_ingest')
    self.assertEqual(dead['payload_keys'], ['agent_name', 'prompt_tokens', 'user_cohort'])
    self.assertEqual(dead['payload_bytes'], len(canonical))
    self.assertEqual(dead['payload_sha256'], hashlib.sha256(canonical).hexdigest())
    self.assertNotIn('jane.doe', json.dumps(dead))
    self.assertNotIn('jane.doe', '\n'.join(logs.output))
    self.assertIn('ingest_dead_letter', logs.output[0])
    # Nothing was recorded, and the state payload exposes the dead letter.
    self.assertEqual(telemetry.get_recent_decorator_events(), before)
    state = self.ctrl.get_state_payload(include_fleet=False)
    self.assertEqual([d['payload_sha256'] for d in state['ingest_dead_letters']], [dead['payload_sha256']])
    self.assertEqual(ctx.exception.to_response()['error'], 'invalid_payload')
    telemetry.reset_decorator_events()
    self.assertEqual(telemetry.get_ingest_dlq_events(), [])

  def test_dead_letter_list_is_bounded_newest_first(self) -> None:
    for i in range(telemetry._INGEST_DLQ_MAX + 5):
      with self.assertRaises(telemetry.IngestValidationError):
        self.ctrl.submit_csat_rating({'rating': 0, 'session_id': f's-{i}'})
    dead = telemetry.get_ingest_dlq_events()
    self.assertEqual(len(dead), telemetry._INGEST_DLQ_MAX)
    newest = json.dumps({'rating': 0, 'session_id': f's-{telemetry._INGEST_DLQ_MAX + 4}'}, sort_keys=True)
    self.assertEqual(dead[0]['payload_sha256'], hashlib.sha256(newest.encode('utf-8')).hexdigest())

  def test_valid_decorator_event_is_recorded_as_reported(self) -> None:
    self.ctrl.ingest_decorator_event({'agent_name': 'it_desk', 'prompt_tokens': 1200, 'cached_tokens': 900})
    event = telemetry.get_recent_decorator_events()[0]
    self.assertEqual(event['agent_name'], 'it_desk')
    self.assertEqual(event['cache_hit_pct'], 75.0)
    for key in ('latency_ms', 'output_tokens', 'context_bloat_pct', 'idle_ratio_pct', 'model', 'status'):
      self.assertIsNone(event[key], key)
    self.assertEqual(telemetry.get_ingest_dlq_events(), [])


class FastApiIngestTest(unittest.TestCase):
  """The production app (uvicorn app.fast_api_app:app) registers these same routes."""

  def setUp(self) -> None:
    _reset()
    self.addCleanup(_reset)
    app = fastapi.FastAPI()
    server.register_api_routes(app, server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET))
    self.client = TestClient(app)

  def test_invalid_bodies_get_422_with_reasons(self) -> None:
    cases = [
        ('/api/decorator_ingest', {'json': {'latency_ms': 5}}, 'agent_name: required'),
        ('/api/decorator_ingest', {'content': b'{not json', 'headers': {'Content-Type': 'application/json'}},
         'body: not valid JSON'),
        ('/api/decorator_ingest', {}, 'body: must be a JSON object'),
        ('/api/aive_log', {'json': [1, 2]}, 'body: must be a JSON object'),
        ('/api/aive_log', {'json': {'user_email': 'a@example.com', 'task_type': 'CHAT', 'prompt': _SECRET_PROMPT}},
         'no measurements: send at least one of total_tokens, prompt_tokens, output_tokens, latency_ms'),
        ('/api/csat_rating', {'json': {'rating': 9, 'session_id': 's'}}, 'rating: required, a whole number from 1 to 5'),
    ]
    for route, kwargs, expected in cases:
      with self.subTest(route=route, expected=expected):
        resp = self.client.post(route, **kwargs)
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body['error'], 'invalid_payload')
        self.assertIn(expected, body['errors'])
        self.assertEqual(body['dead_letter']['source'], route)
        self.assertNotIn(_SECRET_PROMPT, resp.text)
    self.assertEqual(len(telemetry.get_ingest_dlq_events()), len(cases))

  def test_valid_bodies_return_state(self) -> None:
    resp = self.client.post('/api/decorator_ingest', json={'agent_name': 'probe', 'latency_ms': 12.5})
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.json()['decorator_events'][0]['latency_ms'], 12.5)
    resp = self.client.post('/api/csat_rating', json={'rating': 5, 'session_id': 's-1'})
    self.assertEqual(resp.status_code, 200)
    resp = self.client.post(
        '/api/aive_log',
        json={'user_email': 'a@example.com', 'task_type': 'CHAT', 'total_tokens': 42, 'prompt': _SECRET_PROMPT},
    )
    self.assertEqual(resp.status_code, 200)
    self.assertNotIn(_SECRET_PROMPT, resp.text)
    self.assertEqual(telemetry.get_ingest_dlq_events(), [])


class NoRawPromptTextTest(unittest.TestCase):
  """Prompt text is reduced to count / characters / digest before it is stored, logged or served."""

  def setUp(self) -> None:
    _reset()
    self.addCleanup(_reset)

  def test_usage_row_keeps_only_a_fingerprint(self) -> None:
    with mock.patch.dict(os.environ, {'VIBELIFT_PROMPT_HASH_KEY': ''}):
      with self.assertLogs('vibelift.audit_sink', level='INFO') as logs:
        row = telemetry.log_agent_generation_event(
            session_id='s', user_email='jane.doe@example.com', company_name=None, department=None,
            task_type='CHAT', prompts=[_SECRET_PROMPT, 'second'], outputs=None, total_tokens=10,
        )
    self.assertNotIn('prompts', row)
    self.assertEqual(row['prompt_count'], 2)
    self.assertEqual(row['prompt_chars'], len(_SECRET_PROMPT) + len('second'))
    self.assertEqual(row['prompt_hash'], 'sha256')
    self.assertEqual(row['prompt_sha256'][0], hashlib.sha256(_SECRET_PROMPT.encode('utf-8')).hexdigest())
    self.assertNotIn(_SECRET_PROMPT, '\n'.join(logs.output))
    served = json.dumps(telemetry.get_recent_aive_logs())
    self.assertNotIn(_SECRET_PROMPT, served)

  def test_hmac_key_is_used_when_configured(self) -> None:
    with mock.patch.dict(os.environ, {'VIBELIFT_PROMPT_HASH_KEY': 'k3y'}):
      out = telemetry._fingerprint_prompts([_SECRET_PROMPT])
    expected = hmac.new(b'k3y', _SECRET_PROMPT.encode('utf-8'), hashlib.sha256).hexdigest()
    self.assertEqual(out, {'prompt_count': 1, 'prompt_chars': len(_SECRET_PROMPT),
                           'prompt_sha256': [expected], 'prompt_hash': 'hmac-sha256'})
    self.assertNotEqual(expected, hashlib.sha256(_SECRET_PROMPT.encode('utf-8')).hexdigest())

  def test_decorator_and_seed_rows_never_carry_prompt_text(self) -> None:
    @telemetry.with_analytics_logging(task_type='CHAT', agent_name='desk')
    def handler(prompt: str, **_: object) -> dict[str, int]:
      return {'prompt_tokens': 30, 'cached_tokens': 10, 'output_tokens': 5}

    handler(_SECRET_PROMPT, session_id='s-9', user_email='jane.doe@example.com')
    logs = telemetry.get_recent_aive_logs()
    self.assertTrue(all('prompts' not in row for row in logs['usage_logs']))
    self.assertEqual(logs['usage_logs'][0]['prompt_count'], 1)
    self.assertEqual((logs['usage_logs'][0]['prompt_tokens'], logs['usage_logs'][0]['cached_tokens']), (30, 10))
    self.assertNotIn(_SECRET_PROMPT, json.dumps(logs))
    self.assertNotIn(_SECRET_PROMPT, json.dumps(telemetry.get_recent_decorator_events()))
    # The demo seed rows were fingerprinted at import time as well.
    self.assertNotIn('Open the VibeLift dashboard', json.dumps(telemetry._SEED_AIVE_USAGE_LOGS))

  def test_state_payload_never_serves_prompt_text(self) -> None:
    ctrl = server.VibeLiftRuntimeController(fleet_service=_FAKE_FLEET)
    state = ctrl.ingest_aive_log(
        {'user_email': 'a@example.com', 'task_type': 'CHAT', 'latency_ms': 120, 'prompts': [_SECRET_PROMPT]}
    )
    self.assertNotIn(_SECRET_PROMPT, json.dumps(state, default=str))


if __name__ == '__main__':
  unittest.main()
