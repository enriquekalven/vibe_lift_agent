"""Unit tests for Google Antigravity IDE FinOps (agy_finops.py) and dashboard integration.

Tests:
- Privacy allowlist (strip_content): prompt/response bodies never leave Cloud Logging.
- WTU & PAYG metering (meter_call): 0.5 list shares, >200k long-context shares, 50-WTU floor,
  NOOP_QUOTA zero-charge, and Anthropic 100% PAYG + 1.25x cache-write input rate.
- Floating 7-day seat allowance pool replay and Fair Share vs When-Used PAYG chargeback conservation.
- Live AntigravityFinOpsCollector against mocked Cloud Logging, Discovery Engine License API, and
  Cloud Monitoring WTU quota meters.
- Dashboard state (/api/state, /api/antigravity) and UI panel (#antigravityFinopsPanel).
"""

from __future__ import annotations

import json
import unittest
import urllib.parse
from unittest import mock

from finops_dashboard import agy_finops, server
from finops_dashboard import fleet as ge_fleet
from finops_dashboard.ui import template as ui_template
from tests import test_fleet as ge_fleet_test

SECRET_PROMPT = 'SUPER_SECRET_DEVELOPER_PROMPT_CODE_SNIPPET'
SECRET_RESPONSE = 'SUPER_SECRET_MODEL_CANDIDATE_OUTPUT'


def _raw_agy_log_entry(
    insert_id: str = 'call-1',
    ts: str = '2026-09-28T14:00:00Z',
    user: str = 'alice@example.com',
    trajectory_id: str = 'traj-100',
    model: str = 'gemini-3.8-flash',
    experience: str = 'gemini-3.8-flash-high',
    provider: str = 'Google',
    quota_type: str | None = 'REGULAR_QUOTA',
    prompt_tokens: int = 100_000,
    cached_tokens: int = 60_000,
    output_tokens: int = 10_000,
    thoughts_tokens: int = 5_000,
) -> dict:
  meta: dict = {
      'promptTokenCount': prompt_tokens,
      'cachedContentTokenCount': cached_tokens,
      'candidatesTokenCount': output_tokens,
      'thoughtsTokenCount': thoughts_tokens,
      'totalTokenCount': prompt_tokens + output_tokens + thoughts_tokens,
      'secretPromptEcho': SECRET_PROMPT,
  }
  if quota_type is not None:
    meta['quotaType'] = quota_type
  return {
      'insertId': insert_id,
      'timestamp': ts,
      'logName': 'projects/live-project/logs/businessaicode.googleapis.com%2Finference_response',
      'labels': {
          'user_id': f'user:{user}',
          'model': model,
          'model_provider': provider,
          'client_name': 'antigravity_hub',
          'client_version': '2.21.1',
          'trajectory_id': trajectory_id,
          'request_id': f'{trajectory_id}-req-1',
          'extraSecretLabel': SECRET_PROMPT,
      },
      'jsonPayload': {
          'experience': experience,
          'modelRequest': {'contents': [{'parts': [{'text': SECRET_PROMPT}]}]},
          'modelResponse': {
              'candidates': [{'content': {'parts': [{'text': SECRET_RESPONSE}]}}],
          },
          'metadata': meta,
      },
  }


class FakeAgyGoogleApi:
  """Fake _GoogleApi serving Antigravity Cloud Logging, License API, and Cloud Monitoring quota meters."""

  def __init__(self, entries: list[dict] | None = None):
    self.entries = entries if entries is not None else [
        _raw_agy_log_entry(
            insert_id='call-1',
            ts='2026-09-28T12:00:00Z',
            user='alice@example.com',
            trajectory_id='traj-1',
            model='gemini-3.8-flash',
            experience='gemini-3.8-flash-high',
            provider='Google',
            quota_type='REGULAR_QUOTA',
            prompt_tokens=200_000,
            cached_tokens=100_000,
            output_tokens=20_000,
            thoughts_tokens=10_000,
        ),
        _raw_agy_log_entry(
            insert_id='call-2',
            ts='2026-09-28T13:00:00Z',
            user='bob@example.com',
            trajectory_id='traj-2',
            model='claude-sonnet-4-6',
            experience='claude-sonnet-4-6-high',
            provider='Anthropic',
            quota_type='OVERAGE_QUOTA',
            prompt_tokens=50_000,
            cached_tokens=20_000,
            output_tokens=5_000,
            thoughts_tokens=0,
        ),
    ]

  def call(self, method: str, url: str, body: dict | None = None) -> dict:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    if parts.netloc == 'logging.googleapis.com' and method == 'POST':
      return {'entries': self.entries}
    if parts.netloc.endswith('discoveryengine.googleapis.com'):
      if parts.path.endswith('/licenseConfigs'):
        return {
            'licenseConfigs': [
                {
                    'name': 'projects/live-project/locations/global/licenseConfigs/cfg-plus',
                    'subscriptionTier': 'SUBSCRIPTION_TIER_PLUS',
                    'licenseCount': 5,
                    'state': 'ACTIVE',
                }
            ]
        }
      if parts.path.endswith('/userLicenses'):
        return {
            'userLicenses': [
                {
                    'userPrincipal': 'alice@example.com',
                    'licenseConfig': 'projects/live-project/locations/global/licenseConfigs/cfg-plus',
                    'licenseAssignmentState': 'ASSIGNED',
                },
                {
                    'userPrincipal': 'bob@example.com',
                    'licenseConfig': 'projects/live-project/locations/global/licenseConfigs/cfg-plus',
                    'licenseAssignmentState': 'ASSIGNED',
                },
                {
                    'userPrincipal': 'idle.dev@example.com',
                    'licenseConfig': 'projects/live-project/locations/global/licenseConfigs/cfg-plus',
                    'licenseAssignmentState': 'ASSIGNED',
                },
            ]
        }
    if parts.netloc == 'monitoring.googleapis.com':
      if parts.path.endswith('/metricDescriptors'):
        return {
            'metricDescriptors': [
                {'type': 'discoveryengine.googleapis.com/quota/ratev2/agentic_wtu_tier_plus_pp_weekly_regional/limit'},
                {'type': 'discoveryengine.googleapis.com/quota/ratev2/agentic_wtu_tier_plus_pp_weekly_regional/net_usage'},
            ]
        }
      flt = urllib.parse.unquote_plus((query.get('filter') or [''])[0])
      if 'agentic_wtu_tier_plus_pp_weekly_regional/limit' in flt:
        return {
            'timeSeries': [{
                'resource': {'labels': {'location': 'global'}},
                'metric': {'labels': {}},
                'points': [{'value': {'int64Value': str(5 * 1_875_000)}}],
            }]
        }
      if 'agentic_wtu_tier_plus_pp_weekly_regional/net_usage' in flt:
        return {
            'timeSeries': [{
                'resource': {'labels': {'location': 'global'}},
                'metric': {'labels': {'window_start_time': '1790400000'}},
                'points': [{'value': {'int64Value': '125000'}}],
            }]
        }
      return {}
    raise AssertionError(f'Unexpected URL {url}')


class AgyFinOpsTest(unittest.TestCase):

  def test_strip_content_drops_prompt_and_response_text(self):
    raw = _raw_agy_log_entry()
    stripped = agy_finops.strip_content(raw)
    serialized = json.dumps(stripped)
    self.assertNotIn(SECRET_PROMPT, serialized)
    self.assertNotIn(SECRET_RESPONSE, serialized)
    self.assertEqual(stripped['insertId'], 'call-1')
    self.assertEqual(stripped['jsonPayload']['metadata']['promptTokenCount'], 100_000)
    self.assertEqual(stripped['labels']['user_id'], 'user:alice@example.com')

  def test_meter_call_flash_half_share_and_50_wtu_floor(self):
    # Tiny call on gemini-3.8-flash should hit the 50 WTU minimum pre-check floor.
    tiny = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(prompt_tokens=10, cached_tokens=0, output_tokens=5, thoughts_tokens=0)
    )
    metered_tiny = agy_finops.meter_call(tiny)
    self.assertEqual(metered_tiny['wtu'], 50)

    # Larger call on gemini-3.8-flash: 0.5 list share means WTU cost is half of list_usd.
    normal = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(prompt_tokens=1_000_000, cached_tokens=0, output_tokens=100_000, thoughts_tokens=0)
    )
    metered = agy_finops.meter_call(normal)
    # List price: 1M * $0.30 + 0.1M * $2.50 = $0.55. Half share = $0.275 -> 137,500 WTU.
    self.assertAlmostEqual(metered['list_usd'], 0.55, places=5)
    self.assertEqual(metered['list_share'], 0.5)
    self.assertEqual(metered['wtu'], 137_500)

  def test_meter_call_argon_long_context_and_noop_and_anthropic(self):
    # gemini-4-argon > 200k prompt tokens switches from 0.5 share to 1.0/1.0/0.75 share.
    argon_long = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(
            model='gemini-4-argon',
            prompt_tokens=300_000,
            cached_tokens=100_000,
            output_tokens=20_000,
            thoughts_tokens=0,
        )
    )
    m_argon = agy_finops.meter_call(argon_long)
    # Uncached = 200k * ($1.25 * 1.0 / 2) = 125,000 WTU;
    # Cached = 100k * ($0.125 * 1.0 / 2) = 6,250 WTU;
    # Output = 20k * ($10.00 * 0.75 / 2) = 75,000 WTU -> Total = 206,250 WTU.
    self.assertEqual(m_argon['wtu'], 206_250)

    # NOOP_QUOTA charges 0 WTU and $0 list_usd.
    noop = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(quota_type='NOOP_QUOTA', prompt_tokens=500_000)
    )
    m_noop = agy_finops.meter_call(noop)
    self.assertEqual(m_noop['wtu'], 0)
    self.assertEqual(m_noop['list_usd'], 0.0)

    # Anthropic Claude uses 1.25x cache-write input rate ($3.75/M for claude-sonnet-4-6) and never draws on WTU pool.
    claude = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(
            model='claude-sonnet-4-6',
            provider='Anthropic',
            quota_type='OVERAGE_QUOTA',
            prompt_tokens=100_000,
            cached_tokens=20_000,
            output_tokens=10_000,
            thoughts_tokens=0,
        )
    )
    m_claude = agy_finops.meter_call(claude)
    self.assertEqual(m_claude['provider'], 'Anthropic')
    # Uncached = 80k * $3.75 = $0.30; Cached = 20k * $0.30 = $0.006; Output = 10k * $15.00 = $0.15 -> $0.456.
    self.assertAlmostEqual(m_claude['list_usd'], 0.456, places=5)

  def test_pool_replay_and_fair_share_vs_when_used_conservation(self):
    # 1 Plus seat = 1,875,000 WTU ($3.75/wk).
    # Alice draws 1,500,000 WTU first (included); Bob draws 375,000 WTU (included, fills pool to 1,875,000),
    # and then Bob makes an overage call worth $2.00 (1,000,000 WTU).
    configs = [{'name': 'cfg-plus', 'tier': 'SUBSCRIPTION_TIER_PLUS', 'license_count': 1, 'state': 'ACTIVE'}]
    licenses = [
        {'user_email': 'alice@example.com', 'license_config': 'cfg-plus', 'assignment_state': 'ASSIGNED'},
        {'user_email': 'bob@example.com', 'license_config': 'cfg-plus', 'assignment_state': 'ASSIGNED'},
    ]
    c1 = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(
            insert_id='c1',
            ts='2026-09-28T10:00:00Z',
            user='alice@example.com',
            model='gemini-2.5-pro',
            quota_type=None,
            prompt_tokens=0,
            cached_tokens=0,
            output_tokens=300_000,  # $3.00 = 1,500,000 WTU (80% of 1,875,000 pool)
            thoughts_tokens=0,
        )
    )
    c2 = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(
            insert_id='c2',
            ts='2026-09-28T11:00:00Z',
            user='bob@example.com',
            model='gemini-2.5-pro',
            quota_type=None,
            prompt_tokens=0,
            cached_tokens=0,
            output_tokens=75_000,  # $0.75 = 375,000 WTU (remaining 20% of pool)
            thoughts_tokens=0,
        )
    )
    c3 = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(
            insert_id='c3',
            ts='2026-09-28T12:00:00Z',
            user='bob@example.com',
            model='gemini-2.5-pro',
            quota_type=None,
            prompt_tokens=0,
            cached_tokens=0,
            output_tokens=200_000,  # $2.00 overage after pool is exhausted
            thoughts_tokens=0,
        )
    )
    res = agy_finops.evaluate_calls_and_chargeback(
        [c1, c2, c3],
        license_configs=configs,
        user_licenses=licenses,
        project_id='live-project',
    )
    kpis = res['kpis']
    self.assertAlmostEqual(kpis['included_usd'], 3.75, places=4)
    self.assertAlmostEqual(kpis['payg_usd'], 2.00, places=4)

    by_user = {u['user_email']: u for u in res['by_user']}
    # Under When-Used: Alice paid $0 PAYG; Bob triggered the $2.00 overage so Bob gets $2.00.
    self.assertAlmostEqual(by_user['alice@example.com']['payg_when_used_usd'], 0.0, places=4)
    self.assertAlmostEqual(by_user['bob@example.com']['payg_when_used_usd'], 2.00, places=4)
    # Under Fair Share: Alice drew 80% of the pool (1.5M / 1.875M) so she gets 80% of $2.00 = $1.60;
    # Bob drew 20% of the pool (375k / 1.875M) so he gets 20% of $2.00 = $0.40.
    self.assertAlmostEqual(by_user['alice@example.com']['payg_fair_share_usd'], 1.60, places=4)
    self.assertAlmostEqual(by_user['bob@example.com']['payg_fair_share_usd'], 0.40, places=4)
    # Both chargeback methods sum to the exact $2.00 PAYG total.
    self.assertAlmostEqual(
        sum(u['payg_fair_share_usd'] for u in res['by_user']),
        sum(u['payg_when_used_usd'] for u in res['by_user']),
        places=4,
    )

  def test_live_collector_and_api_endpoint(self):
    fake_api = FakeAgyGoogleApi()
    collector = agy_finops.AntigravityFinOpsCollector(
        project_id='live-project',
        api=fake_api,
        locations=['global'],
    )
    payload = collector.collect(window_hours=168, force_refresh=True)
    serialized = json.dumps(payload)
    self.assertNotIn(SECRET_PROMPT, serialized)
    self.assertNotIn(SECRET_RESPONSE, serialized)
    self.assertEqual(payload['status'], 'LIVE')
    self.assertEqual(payload['kpis']['total_calls'], 2)
    self.assertEqual(payload['kpis']['purchased_seats'], 5)
    self.assertEqual(payload['kpis']['assigned_seats'], 3)
    self.assertEqual(payload['kpis']['idle_seats'], 1)

    # Verify controller /api/state and get_antigravity_finops_payload in DEMO mode and HTML rendering.
    fake_fleet, _ = ge_fleet_test.make_fake_service()
    ctrl = server.FinOpsRuntimeController(fleet_service=fake_fleet)
    state = ctrl.get_state_payload(include_fleet=False)
    self.assertIn('antigravity_finops', state)
    self.assertEqual(state['antigravity_finops']['status'], 'SIMULATED_DEMO')
    self.assertGreater(state['antigravity_finops']['kpis']['total_calls'], 0)

    html = ui_template.render_dashboard_html()
    self.assertIn('id="antigravityFinopsPanel"', html)
    self.assertIn('renderAntigravityFinops', html)

  def test_license_states_and_unknown_tiers_follow_the_api(self):
    # Discovery Engine v1: only ASSIGNED holds a seat; EXPIRED / NOT_STARTED / WITHDRAWN configs add no pool; a
    # tier with no configured allowance gets none (upstream weekly_usd_per_seat = 0), never a guessed $3.75.
    cfg = 'projects/123/locations/global/licenseConfigs/'
    configs = [
        {'name': cfg + 'plus', 'tier': 'SUBSCRIPTION_TIER_PLUS', 'license_count': 2, 'state': 'ACTIVE'},
        {'name': cfg + 'old', 'tier': 'SUBSCRIPTION_TIER_PLUS', 'license_count': 50, 'state': 'EXPIRED'},
        {'name': cfg + 'next', 'tier': 'SUBSCRIPTION_TIER_PLUS', 'license_count': 50, 'state': 'NOT_STARTED'},
        {'name': cfg + 'gone', 'tier': 'SUBSCRIPTION_TIER_PLUS', 'license_count': 50, 'state': 'WITHDRAWN'},
        {'name': cfg + 'biz', 'tier': 'SUBSCRIPTION_TIER_AGENTSPACE_BUSINESS', 'license_count': 3, 'state': 'ACTIVE'},
    ]
    # The assignment names the config by project ID; the config itself uses the project number.
    plus_ref = 'projects/live-project/locations/global/licenseConfigs/plus'
    licenses = [
        {'user_email': 'alice@example.com', 'license_config': plus_ref, 'assignment_state': 'ASSIGNED'},
        {'user_email': 'bob@example.com', 'license_config': plus_ref, 'assignment_state': 'NO_LICENSE'},
        {'user_email': 'carol@example.com', 'license_config': plus_ref, 'assignment_state': 'BLOCKED'},
        {'user_email': 'dave@example.com', 'license_config': plus_ref, 'assignment_state': 'NO_LICENSE_ATTEMPTED_LOGIN'},
        {'user_email': 'erin@example.com', 'license_config': plus_ref, 'assignment_state': 'UNASSIGNED'},
        {'user_email': 'frank@example.com', 'license_config': plus_ref},
        {'user_email': 'gina@example.com', 'license_config': cfg + 'old', 'assignment_state': 'ASSIGNED'},
    ]
    res = agy_finops.evaluate_calls_and_chargeback(
        [], license_configs=configs, user_licenses=licenses, project_id='live-project')
    self.assertEqual(res['kpis']['purchased_seats'], 5)  # 2 Plus + 3 of the tier with no allowance
    self.assertEqual(res['kpis']['assigned_seats'], 1)  # alice only
    self.assertEqual(res['kpis']['pool_limit_wtu'], 2 * 1_875_000)
    pools = {p['tier']: p for p in res['pools']}
    self.assertEqual(pools['Plus']['seat_allowance_usd'], 3.75)
    self.assertEqual(pools['Plus']['seat_allowance_wtu'], 1_875_000)
    biz = pools['SUBSCRIPTION_TIER_AGENTSPACE_BUSINESS']
    self.assertIsNone(biz['seat_allowance_usd'])
    self.assertIsNone(biz['limit_wtu'])
    self.assertIsNone(biz['utilization_pct'])
    self.assertEqual(res['unknown_tiers'], ['SUBSCRIPTION_TIER_AGENTSPACE_BUSINESS'])

  def test_unpriced_models_are_counted_not_silently_zero(self):
    call = agy_finops.normalize_inference_entry(
        _raw_agy_log_entry(insert_id='u1', model='gemini-9-ultra', experience='gemini-9-ultra-high', trajectory_id=''))
    res = agy_finops.evaluate_calls_and_chargeback([call], project_id='live-project')
    self.assertEqual(res['kpis']['unpriced_calls'], 1)
    self.assertEqual(res['kpis']['unpriced_models'], ['gemini-9-ultra'])
    self.assertEqual(res['by_model_effort'][0]['unpriced_calls'], 1)
    user = res['by_user'][0]
    self.assertIsNone(user['trajectories'])  # no trajectory_id logged: unknown, not 1
    self.assertEqual(user['tier'], agy_finops.LICENSED_UNKNOWN_TIER)  # REGULAR_QUOTA, no license data

  def test_source_failures_are_unknown_not_zero(self):
    def denied(method, url, body=None):
      raise ge_fleet.FleetSourceError(403, 'Permission denied')

    payload = agy_finops.AntigravityFinOpsCollector(
        project_id='live-project', api_caller=denied, locations=['global']).collect(window_hours=168, force_refresh=True)
    self.assertEqual(payload['status'], 'ERROR')
    self.assertEqual(set(payload['errors']), {'logs', 'licenses', 'wtu_meter'})
    for key in ('total_calls', 'payg_usd', 'total_cost_usd', 'purchased_seats', 'assigned_seats', 'idle_seats',
                'pool_used_wtu'):
      self.assertIsNone(payload['kpis'][key], key)

    class UserStoreDenied(FakeAgyGoogleApi):
      def call(self, method, url, body=None):
        if url.split('?')[0].endswith('/userLicenses'):
          raise ge_fleet.FleetSourceError(403, 'Permission denied')
        return super().call(method, url, body)

    partial = agy_finops.AntigravityFinOpsCollector(
        project_id='live-project', api=UserStoreDenied(), locations=['global']).collect(window_hours=168, force_refresh=True)
    self.assertEqual(partial['status'], 'LIVE')
    self.assertTrue(partial['partial'])
    self.assertEqual(partial['kpis']['total_calls'], 2)
    self.assertEqual(partial['kpis']['purchased_seats'], 5)
    self.assertIsNone(partial['kpis']['assigned_seats'])
    self.assertIsNone(partial['kpis']['idle_seats'])

  def test_live_state_before_first_collection_is_pending_not_zero(self):
    fake_fleet, _ = ge_fleet_test.make_fake_service()
    ctrl = server.FinOpsRuntimeController(fleet_service=fake_fleet)
    ctrl._last_fleet_payload = None
    ctrl.optimizer._live_fleet_payload = None
    with mock.patch.object(ctrl, '_is_live_gcp', return_value=True):
      payload = ctrl.get_antigravity_finops_payload(window_hours=168)
    self.assertEqual(payload['status'], 'PENDING')
    self.assertFalse(payload['simulated'])
    self.assertTrue(payload['kpis'])
    self.assertTrue(all(v is None for v in payload['kpis'].values()))
    self.assertEqual(payload['by_user'], [])


if __name__ == '__main__':
  unittest.main()
