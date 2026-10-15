"""Tests for Workstream A (SWR, Lite Boot, BQ Fleet Snapshot) and Workstream B (Scheduled Clearance & Executive Digest)."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from finops_dashboard.cost_governance import (
    CostGovernanceService,
    mask_email_for_ui,
    sign_snooze_token,
    verify_snooze_token,
)
from finops_dashboard.fleet import GeminiEnterpriseFleetService
from finops_dashboard.server import FinOpsRuntimeController
from finops_dashboard.ui import template as ui_template


class CostGovernanceAndSwrTest(unittest.TestCase):
    def test_mask_email_and_hmac_snooze_token(self) -> None:
        self.assertEqual(mask_email_for_ui("alice@google.com"), "a***e@google.com")
        self.assertEqual(mask_email_for_ui("a@example.org"), "a***@example.org")
        token = sign_snooze_token("re-12345", days=7)
        self.assertTrue(verify_snooze_token("re-12345", 7, token))
        self.assertFalse(verify_snooze_token("re-99999", 7, token))
        self.assertFalse(verify_snooze_token("re-12345", 7, "forged_token"))

    def test_scheduled_clearance_warn_snooze_and_reclaim_lifecycle(self) -> None:
        gov = CostGovernanceService(project_id="test-proj")
        fleet_snapshot = {
            "unregistered_runtimes": [
                {
                    "agent_id": "re-99887766",
                    "resource_name": "projects/test-proj/locations/us-central1/reasoningEngines/99887766",
                    "display_name": "Idle Prototype Engine",
                    "location": "us-central1",
                    "finops_status": "ZOMBIE_IDLE_ENGINE",
                    "finops_action_command": "gcloud ai reasoning-engines delete 99887766",
                    "backend": {"kind": "agent_engine", "project": "test-proj", "location": "us-central1"},
                    "metrics": {
                        "requests": 0,
                        "llm_calls": 0,
                        "vcpu_hours": 24.0,
                        "memory_gib_hours": 48.0,
                        "billable_instance_hours": 24.0,
                        "est_token_cost_usd": 0.0,
                    },
                },
                {
                    "agent_id": "run-idle-agent-svc",
                    "display_name": "idle-agent-svc",
                    "location": "us-central1",
                    "finops_status": "ZOMBIE_IDLE_SERVICE",
                    "finops_action_command": "gcloud run services update idle-agent-svc --min-instances=0",
                    "backend": {"kind": "cloud_run", "project": "test-proj", "region": "us-central1"},
                    "metrics": {
                        "requests": 0,
                        "llm_calls": 0,
                        "vcpu_hours": 12.0,
                        "memory_gib_hours": 24.0,
                        "billable_instance_hours": 24.0,
                        "est_token_cost_usd": 0.0,
                    },
                },
            ],
            "agents": [],
        }

        # Stage 1: Evaluate and warn idle runtimes
        stage1 = gov.evaluate_and_warn_idle_runtimes(
            fleet_snapshot,
            grace_period_hours=48,
            send_warnings=True,
            dry_run_email=True,
            lookup_bq=False,
            persist_to_bq=False,
        )
        self.assertEqual(stage1["status"], "OK")
        queue_summary = stage1["summary"]
        self.assertEqual(queue_summary["total_tracked"], 2)
        self.assertEqual(queue_summary["pending_clearance_count"], 2)
        self.assertEqual(len(stage1["recent_warnings"]), 2)

        # Verify snooze link and token are present
        first_item = stage1["queue"][0]
        self.assertIn("/api/governance/snooze?runtime_id=", first_item["snooze_7d_url"])
        rid = first_item["runtime_id"]
        tok = sign_snooze_token(rid, 7)

        # Stage 2 before grace period expires: 0 cleared
        stage2_early = gov.execute_scheduled_clearance(mode="dry_run")
        self.assertEqual(stage2_early["cleared_count"], 0)

        # Snooze the first runtime for 7 days with token verification
        with patch.object(gov, "_persist_queue_items_to_bq"):
            snooze_res = gov.snooze_runtime(runtime_id=rid, days=7, token=tok, verify_token=True)
        self.assertEqual(snooze_res["status"], "OK")
        self.assertEqual(snooze_res["governance_status"], "SNOOZED")

        # Fast-forward the second runtime's scheduled_clearance_at into the past so it is ready for reclaim
        other_rid = next(k for k in gov._queue if k != rid)
        gov._queue[other_rid]["scheduled_clearance_at"] = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat()

        with patch.object(gov, "_persist_queue_items_to_bq"):
            stage2_ready = gov.execute_scheduled_clearance(mode="dry_run")
        self.assertEqual(stage2_ready["cleared_count"], 1)
        self.assertEqual(stage2_ready["actions"][0]["runtime_id"], other_rid)
        self.assertEqual(stage2_ready["actions"][0]["status"], "CLEARED_DRY_RUN")

    def test_executive_digest_payload_structure(self) -> None:
        gov = CostGovernanceService(project_id="test-proj")
        fleet_payload = {
            "agents": [
                {
                    "agent_id": "agent-1",
                    "display_name": "Customer Support Copilot",
                    "type": "ADK",
                    "metrics": {
                        "requests": 120,
                        "llm_calls": 240,
                        "input_tokens": 300000,
                        "output_tokens": 45000,
                        "cached_tokens": 120000,
                        "est_token_cost_usd": 28.0,
                    },
                }
            ],
            "unregistered_runtimes": [],
            "model_usage": {"totals": {"estimated_cost_usd": 42.5, "input_tokens": 300000}},
            "model_usage_previous": {"totals": {"estimated_cost_usd": 35.0, "input_tokens": 250000}},
        }
        user_centric_payload = {
            "power_users_ldap": [
                {"ldap": "engineer@google.com", "total_prompts": 44, "estimated_token_cost_usd": 19.25}
            ]
        }
        digest = gov.build_and_send_executive_digest(
            fleet_payload,
            user_centric_payload,
            cadence="daily",
            dry_run=True,
        )
        self.assertEqual(digest["status"], "OK")
        self.assertIn("top_5_agents", digest)
        self.assertEqual(len(digest["top_5_agents"]), 1)
        self.assertEqual(len(digest["cache_degradation_alerts"]), 1)
        self.assertEqual(digest["wow_delta"]["spend_delta_pct"], 21.4)
        self.assertIn("WoW Delta", digest["delivery"]["text_preview"])
        self.assertTrue(digest["delivery"]["dry_run"])

    def test_swr_lite_boot_and_embedded_ui_state(self) -> None:
        ctrl = FinOpsRuntimeController()
        with patch.object(
            ctrl,
            "get_fleet_payload",
            return_value={
                "status": "OK",
                "project_id": "test-project",
                "window_hours": 24,
                "warming": True,
                "cache_age_seconds": 0.0,
                "agents": [],
                "unregistered_runtimes": [],
            },
        ):
            lite_state = ctrl.get_state_payload(
                fast_mcp=True,
                allow_stale=True,
                max_wait_s=0.05,
                lite_boot=True,
            )
        self.assertTrue(lite_state.get("lite_boot"))
        self.assertIn("swr", lite_state)
        self.assertIn("cost_governance", lite_state)
        self.assertIsNone(lite_state.get("antigravity_finops"))
        self.assertIsNone(lite_state.get("user_centric_finops"))

        html = ui_template.render_dashboard_html(initial_state=lite_state)
        self.assertIn("swrRefreshBadge", html)
        self.assertIn("scheduledClearancePanel", html)
        self.assertIn('"lite_boot": true', html)

    def test_bq_fleet_snapshot_round_trip(self) -> None:
        sample_snapshot = {
            "generated_at": datetime.now(UTC).isoformat(),
            "project_id": "test-proj",
            "window_hours": 24,
            "totals": {
                "agents": 3,
                "input_tokens": 12000,
                "cached_tokens": 9000,
            },
            "agents": [{"id": "a1", "display_name": "Agent 1"}],
            "skills_and_mcp": [{"id": "m1", "display_name": "MCP 1"}],
            "engines": [],
            "reasoning_engines": [],
            "cloud_run": {"services": []},
        }
        captured_params = {}

        def fake_bq_write(sql: str, project_id=None, max_bytes_billed=0, query_parameters=None):
            captured_params["sql"] = sql
            captured_params["params"] = query_parameters
            return ([], {})

        collector = GeminiEnterpriseFleetService(
            project_id="test-proj",
            bq_query_fn=fake_bq_write,
            bq_snapshot_enabled=True,
        )
        ok = collector._write_bq_fleet_snapshot(sample_snapshot)
        self.assertTrue(ok)
        self.assertIn("agent_registry_snapshots", captured_params["sql"])
        self.assertTrue(len(captured_params["params"]) >= 2)

        def fake_bq_read(sql: str, project_id=None, max_bytes_billed=0, query_parameters=None):
            return (
                [
                    {
                        "snapshot_timestamp": datetime.now(UTC).isoformat(),
                        "age_seconds": 12.0,
                        "agent_breakdown_json": json.dumps(sample_snapshot),
                    }
                ],
                {},
            )

        collector._bq_query_fn = fake_bq_read
        loaded = collector._read_bq_fleet_snapshot(hours=24)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["project_id"], "test-proj")
        self.assertEqual(loaded["snapshot_source"], "bigquery_agent_registry_snapshots")

    def test_http_routes_end_to_end(self) -> None:
        import fastapi
        from fastapi.testclient import TestClient

        from finops_dashboard.server import register_api_routes

        ctrl = FinOpsRuntimeController()
        fake_fleet = {
            "status": "OK",
            "project_id": "test-project",
            "window_hours": 24,
            "warming": False,
            "cache_age_seconds": 1.0,
            "agents": [],
            "unregistered_runtimes": [
                {
                    "agent_id": "re-55443322",
                    "display_name": "Idle Eval Engine",
                    "location": "us-central1",
                    "finops_status": "ZOMBIE_IDLE_ENGINE",
                    "finops_action_command": "gcloud ai reasoning-engines delete 55443322",
                    "backend": {"kind": "agent_engine", "project": "test-project", "location": "us-central1"},
                    "metrics": {"requests": 0, "llm_calls": 0, "billable_instance_hours": 24.0},
                }
            ],
        }
        app = fastapi.FastAPI()
        register_api_routes(app, ctrl)
        client = TestClient(app)

        with patch.object(ctrl, "get_fleet_payload", return_value=fake_fleet):
            r_ui = client.get("/ui")
            self.assertEqual(r_ui.status_code, 200)
            self.assertIn("swrRefreshBadge", r_ui.text)
            self.assertIn("scheduledClearancePanel", r_ui.text)

            r_warn = client.post("/api/governance/evaluate_idle", json={"send_warnings": True, "dry_run_email": True})
            self.assertEqual(r_warn.status_code, 200)
            warn_data = r_warn.json()
            self.assertEqual(warn_data["summary"]["pending_clearance_count"], 1)

            tok = sign_snooze_token("re-55443322", 7)
            r_snooze_bad = client.get("/api/governance/snooze?runtime_id=re-55443322&days=7&token=bad_token")
            self.assertEqual(r_snooze_bad.status_code, 403)

            r_snooze_ok = client.get(f"/api/governance/snooze?runtime_id=re-55443322&days=7&token={tok}")
            self.assertEqual(r_snooze_ok.status_code, 200)
            self.assertEqual(r_snooze_ok.json()["governance_status"], "SNOOZED")

            r_clear = client.post("/api/cron/execute_scheduled_clearance", json={"mode": "dry_run"})
            self.assertEqual(r_clear.status_code, 200)
            self.assertEqual(r_clear.json()["cleared_count"], 0)

            r_digest = client.get("/api/governance/executive_digest")
            self.assertEqual(r_digest.status_code, 200)
            self.assertEqual(r_digest.json()["status"], "OK")

    def test_gmail_api_delivery_channel(self) -> None:
        gov = CostGovernanceService(
            project_id="test-proj",
            token_fn=lambda: "ya29.fake-oauth-token",
        )
        captured_req = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return json.dumps({"id": "msg-12345", "threadId": "thr-12345"}).encode("utf-8")

        def _fake_urlopen(req, timeout=10.0):
            captured_req["url"] = req.full_url
            captured_req["auth"] = req.headers.get("Authorization")
            captured_req["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp()

        with (
            patch.dict("os.environ", {"FINOPS_GMAIL_ENABLED": "true"}, clear=False),
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        ):
            res = gov._deliver_email_notification(
                recipient="owner@google.com",
                subject="[FinOps Alert] Idle Runtime",
                html_body="<p>Runtime idle</p>",
                text_body="Runtime idle",
                dry_run=False,
                metadata={"runtime_id": "re-123"},
            )

        self.assertTrue(res["delivered"])
        self.assertEqual(res["delivery_channel"], "GMAIL_API")
        self.assertEqual(res["gmail_message_id"], "msg-12345")
        self.assertIn("gmail.googleapis.com/gmail/v1/users/me/messages/send", captured_req["url"])
        self.assertEqual(captured_req["auth"], "Bearer ya29.fake-oauth-token")
        self.assertIn("raw", captured_req["body"])


if __name__ == "__main__":
    unittest.main()
