"""Tests for Workstream A (SWR, Lite Boot, BQ Fleet Snapshot) and Workstream B (Scheduled Clearance & Executive Digest)."""

from __future__ import annotations

import base64
import email
import email.policy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from finops_dashboard.cost_governance import (
    CostGovernanceService,
    mask_email_for_ui,
    normalize_recipient_list,
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
        self.assertEqual(stage2_ready["cleared_count"], 0)
        self.assertEqual(len(stage2_ready["actions"]), 1)
        self.assertEqual(stage2_ready["actions"][0]["runtime_id"], other_rid)
        self.assertEqual(stage2_ready["actions"][0]["status"], "DRY_RUN")
        self.assertEqual(gov._queue[other_rid]["status"], "WARNED_GRACE_PERIOD")

    def test_stage1_warning_updates_each_runtimes_own_queue_row(self) -> None:
        # Regression: after a warning goes out, the update must land on that runtime's queue item, not
        # on the last candidate of the evaluation loop (one runtime alone cannot tell the two apart).
        gov = CostGovernanceService(project_id="test-proj")

        def idle_service(rid: str) -> dict:
            return {
                "agent_id": rid,
                "display_name": rid,
                "location": "us-central1",
                "finops_status": "ZOMBIE_IDLE_SERVICE",
                "backend": {"kind": "cloud_run", "project": "test-proj", "region": "us-central1"},
                "metrics": {
                    "requests": 0,
                    "llm_calls": 0,
                    "vcpu_hours": 1.0,
                    "memory_gib_hours": 2.0,
                    "billable_instance_hours": 1.0,
                    "est_token_cost_usd": 0.0,
                },
            }

        rids = [f"run-svc-{i}" for i in range(3)]
        fleet = {"unregistered_runtimes": [idle_service(r) for r in rids], "agents": []}
        persisted: list[dict] = []
        with patch.object(gov, "_persist_queue_items_to_bq", side_effect=persisted.extend):
            gov.evaluate_and_warn_idle_runtimes(
                fleet,
                grace_period_hours=48,
                send_warnings=True,
                dry_run_email=True,
                lookup_bq=False,
                persist_to_bq=True,
            )
        for rid in rids:
            item = gov._queue[rid]
            self.assertEqual(item["runtime_id"], rid)
            self.assertEqual(item["status"], "WARNED_GRACE_PERIOD")
            self.assertTrue(item["warning_sent_at"])
            self.assertTrue(item["scheduled_clearance_at"], rid + " has no clearance time")
        rows = {row["runtime_id"]: row for row in persisted}
        self.assertEqual(set(rows), set(rids))
        for rid in rids:
            self.assertEqual(rows[rid]["status"], "WARNED_GRACE_PERIOD")
            self.assertEqual(rows[rid]["scheduled_clearance_at"], gov._queue[rid]["scheduled_clearance_at"])

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
        self.assertIn("governance_score", digest)
        self.assertIn("Enterprise AI Governance Score:", digest["delivery"]["text_preview"])
        self.assertTrue(0 <= digest["governance_score"]["score"] <= 100)
        self.assertEqual(len(digest["governance_score"]["pillars"]), 5)
        self.assertTrue(digest["delivery"]["dry_run"])

    def test_compute_governance_score_backend_pillars(self) -> None:
        gov = CostGovernanceService(project_id="test-proj")
        fleet_payload = {
            "agents": [
                {
                    "agent_id": "agent-1",
                    "display_name": "Support Copilot",
                    "state": "ENABLED",
                    "backend": {"kind": "agent_engine"},
                    "trace_logging": {"supported": True, "enabled": True},
                    "metrics": {
                        "requests": 100,
                        "input_tokens": 200000,
                        "output_tokens": 20000,
                        "cached_tokens": 120000,
                        "token_source": "otel",
                    },
                }
            ],
            "unregistered_runtimes": [],
            "model_usage": {
                "models": [
                    {
                        "model": "gemini-2.5-flash",
                        "invocations": 100,
                        "input_tokens": 200000,
                        "cache_read_tokens": 120000,
                        "output_tokens": 18000,
                        "reasoning_tokens": 2000,
                    }
                ],
                "totals": {"estimated_cost_usd": 10.0, "input_tokens": 200000},
            },
            "model_usage_previous": {"totals": {"estimated_cost_usd": 9.8, "input_tokens": 195000}},
        }
        user_centric_payload = {
            "ge_sessions": [{"session_id": "s1", "user_email": "alice@google.com"}],
        }
        antigravity_payload = {
            "seats": {"assigned_seats": 50, "active_seats_7d": 46},
        }
        score = gov.compute_governance_score(
            fleet_payload=fleet_payload,
            user_centric_payload=user_centric_payload,
            antigravity_payload=antigravity_payload,
        )
        self.assertGreaterEqual(score["score"], 90)
        self.assertEqual(score["grade"], "A")
        self.assertEqual(score["status"], "OPTIMAL")
        self.assertEqual(len(score["pillars"]), 5)

    def test_trace_pillar_counts_the_fleet_observability_config(self) -> None:
        # Regression: Pillar 1 read trace_logging.enabled, which classify_agent never sets, so a live
        # fleet scored 0/N even with trace logging on (the Agents tab badge reads observability_config).
        from finops_dashboard.cost_governance import compute_governance_score
        from finops_dashboard.fleet import classify_agent

        def raw(i: int, on: bool) -> dict:
            return {
                "name": (
                    "projects/p/locations/global/collections/default_collection/engines/e1/"
                    f"assistants/default_assistant/agents/a{i}"
                ),
                "displayName": f"Agent {i}",
                "state": "ENABLED",
                "observabilityConfig": {"observabilityEnabled": on},
            }

        agents = [classify_agent("e1", "default_assistant", raw(i, i < 3)) for i in range(5)]
        score = compute_governance_score(fleet_payload={"agents": agents})
        components = [c for p in score["pillars"] for c in p["components"]]
        trace = next(c for c in components if c["id"] == "trace_logging")
        self.assertEqual(trace["measured_value"], 3)
        self.assertEqual(trace["target_value"], 5)
        self.assertEqual(trace["points"], 7)  # round(12 * 3 / 5)

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

    def test_default_bigquery_path_uses_the_real_query_signature(self) -> None:
        # Regression: both readers passed maximum_bytes_billed=, a TypeError that `except Exception`
        # swallowed, so without an injected fake the snapshot read and token pushdown never ran.
        from finops_dashboard import gcp_telemetry

        snapshot = {"project_id": "test-proj", "window_hours": 24, "agents": [{"id": "a1"}]}
        rows = [
            {
                "snapshot_timestamp": datetime.now(UTC).isoformat(),
                "age_seconds": 5.0,
                "agent_breakdown_json": json.dumps(snapshot),
            }
        ]
        collector = GeminiEnterpriseFleetService(project_id="test-proj", bq_snapshot_enabled=True)
        collector._bq_query_fn = None
        collector.bq_token_pushdown = True
        with patch.object(gcp_telemetry, "GoogleCloudTelemetryService", autospec=True) as svc_cls:
            svc_cls.return_value.run_bigquery_query.return_value = (rows, {})
            self.assertIsNotNone(collector._read_bq_fleet_snapshot(hours=24))
            self.assertEqual(collector._run_bq_token_query("test-proj", "SELECT 1"), rows)
        calls = svc_cls.return_value.run_bigquery_query.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("max_bytes_billed", call.kwargs)

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


_EMAIL_ENV_KEYS = (
    "FINOPS_GMAIL_ACCESS_TOKEN",
    "FINOPS_GMAIL_REFRESH_TOKEN",
    "FINOPS_GMAIL_CLIENT_ID",
    "FINOPS_GMAIL_CLIENT_SECRET",
    "FINOPS_GMAIL_DWD_SERVICE_ACCOUNT",
    "FINOPS_GMAIL_SENDER",
    "FINOPS_ALERT_FROM_EMAIL",
    "FINOPS_SMTP_HOST",
    "FINOPS_ALERT_WEBHOOK_URL",
    "FINOPS_ALLOW_AUTOMATED_CLEARANCE",
    "FINOPS_SNOOZE_BASE_URL",
)

# Stand-in for gcloud: records every call and, like the real CLI, rejects --headers on `jobs update http`.
_FAKE_GCLOUD = '''#!{python}
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
state = os.environ["FAKE_GCLOUD_STATE"]
if args[:3] == ["run", "services", "describe"]:
    print("https://finops-dashboard-mcp-app-123.us-central1.run.app")
elif args[:3] == ["scheduler", "jobs", "describe"]:
    sys.exit(0 if os.path.exists(os.path.join(state, args[3])) else 1)
elif args[:4] == ["scheduler", "jobs", "create", "http"]:
    with open(os.path.join(state, args[4]), "w", encoding="utf-8"):
        pass
elif args[:4] == ["scheduler", "jobs", "update", "http"]:
    if any(a.startswith("--headers") for a in args):
        sys.stderr.write("ERROR: (gcloud.scheduler.jobs.update.http) unrecognized arguments: --headers\\n")
        sys.exit(2)
'''


def _email_env(**overrides: str):
    """patch.dict for os.environ with every email/clearance variable blanked, then `overrides` applied."""
    env = dict.fromkeys(_EMAIL_ENV_KEYS, "")
    env.update(overrides)
    return patch.dict("os.environ", env)


class _FakeHttpResponse:
    """What urllib.request.urlopen returns: a context manager with a JSON body."""

    status = 200

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _send(gov: CostGovernanceService, recipient: str = "owner@example.com") -> dict:
    return gov._deliver_email_notification(
        recipient=recipient,
        subject="[FinOps] Idle runtime",
        text_body="Runtime idle",
        html_body="<p>Runtime idle</p>",
        metadata={"runtime_id": "re-123"},
        dry_run=False,
    )


def _idle_engine(rid: str) -> dict:
    return {
        "agent_id": rid,
        "resource_name": f"projects/acme-prod/locations/us-central1/reasoningEngines/{rid[3:]}",
        "display_name": "Idle Engine",
        "location": "us-central1",
        "finops_status": "ZOMBIE_IDLE_ENGINE",
        "backend": {"kind": "agent_engine", "project": "acme-prod", "location": "us-central1"},
        "metrics": {"requests": 0, "llm_calls": 0, "billable_instance_hours": 24.0},
    }


def _due_item(rid: str) -> dict:
    past = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    return {
        "runtime_id": rid,
        "runtime_kind": "agent_engine",
        "resource_name": f"projects/test-proj/locations/us-central1/reasoningEngines/{rid[3:]}",
        "display_name": "Idle Engine",
        "project_id": "test-proj",
        "location": "us-central1",
        "status": "WARNED_GRACE_PERIOD",
        "warning_sent_at": past,
        "scheduled_clearance_at": past,
        "recommended_action": "delete_reasoning_engine",
        "updated_at": past,
    }


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GovernanceEmailAndSchedulingTest(unittest.TestCase):
    """Gmail delivery, scheduled-clearance safety, and the Cloud Scheduler setup script."""

    def test_gmail_domain_wide_delegation_signs_a_jwt_and_caches_the_token(self) -> None:
        calls: dict[str, list] = {"sign": [], "token": [], "send": []}

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            if url.startswith("https://iamcredentials.googleapis.com/"):
                calls["sign"].append(req)
                return _FakeHttpResponse({"keyId": "k1", "signedJwt": "signed.jwt.value"})
            if url == "https://oauth2.googleapis.com/token":
                calls["token"].append(req)
                return _FakeHttpResponse({"access_token": "ya29.gmail-send", "expires_in": 3599})
            if url.startswith("https://gmail.googleapis.com/"):
                calls["send"].append(req)
                return _FakeHttpResponse({"id": f"msg-{len(calls['send'])}", "threadId": "thr-1"})
            raise AssertionError(f"unexpected request to {url}")

        gov = CostGovernanceService(project_id="test-proj", token_fn=lambda: "ya29.runtime-adc")
        with (
            _email_env(
                FINOPS_GMAIL_DWD_SERVICE_ACCOUNT="finops-mailer@test-proj.iam.gserviceaccount.com",
                FINOPS_GMAIL_SENDER="finops-alerts@example.com",
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            first = _send(gov, "owner@example.com")
            second = _send(gov, "cfo@example.com")

        self.assertEqual((len(calls["sign"]), len(calls["token"]), len(calls["send"])), (1, 1, 2))
        sign_req = calls["sign"][0]
        self.assertTrue(sign_req.full_url.endswith(
            "/v1/projects/-/serviceAccounts/finops-mailer@test-proj.iam.gserviceaccount.com:signJwt"
        ))
        self.assertEqual(sign_req.get_header("Authorization"), "Bearer ya29.runtime-adc")
        claims = json.loads(json.loads(sign_req.data.decode("utf-8"))["payload"])
        self.assertEqual(claims["iss"], "finops-mailer@test-proj.iam.gserviceaccount.com")
        self.assertEqual(claims["sub"], "finops-alerts@example.com")
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/gmail.send")
        self.assertEqual(claims["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual(claims["exp"] - claims["iat"], 3600)

        form = urllib.parse.parse_qs(calls["token"][0].data.decode("utf-8"))
        self.assertEqual(form["grant_type"], ["urn:ietf:params:oauth:grant-type:jwt-bearer"])
        self.assertEqual(form["assertion"], ["signed.jwt.value"])

        for send_req, recipient in zip(calls["send"], ("owner@example.com", "cfo@example.com"), strict=True):
            self.assertEqual(send_req.full_url, "https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
            # Gmail only ever sees the gmail.send token, never the runtime's cloud-platform token.
            self.assertEqual(send_req.get_header("Authorization"), "Bearer ya29.gmail-send")
            raw = json.loads(send_req.data.decode("utf-8"))["raw"]
            msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
            self.assertEqual(msg["To"], recipient)
            self.assertEqual(msg["From"], "finops-alerts@example.com")

        for res, msg_id in ((first, "msg-1"), (second, "msg-2")):
            self.assertTrue(res["delivered"], res["error"])
            self.assertEqual(res["delivery_channel"], "GMAIL_API")
            self.assertEqual(res["gmail_auth_mode"], "domain_wide_delegation")
            self.assertEqual(res["gmail_message_id"], msg_id)

    def test_gmail_refresh_token_flow_never_uses_the_runtime_token(self) -> None:
        token_forms: list[dict] = []
        send_auth: list[str | None] = []

        def fake_urlopen(req, timeout=None):
            if req.full_url == "https://oauth2.googleapis.com/token":
                token_forms.append(urllib.parse.parse_qs(req.data.decode("utf-8")))
                return _FakeHttpResponse({"access_token": "ya29.user-grant", "expires_in": 3599})
            if req.full_url.startswith("https://gmail.googleapis.com/"):
                send_auth.append(req.get_header("Authorization"))
                return _FakeHttpResponse({"id": "msg-1"})
            raise AssertionError(f"unexpected request to {req.full_url}")

        def runtime_token() -> str:
            raise AssertionError("the runtime ADC token must not be used by the refresh-token flow")

        gov = CostGovernanceService(project_id="test-proj", token_fn=runtime_token)
        with (
            _email_env(
                FINOPS_GMAIL_REFRESH_TOKEN="1//refresh",
                FINOPS_GMAIL_CLIENT_ID="cid.apps.googleusercontent.com",
                FINOPS_GMAIL_CLIENT_SECRET="client-secret",
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            results = [_send(gov), _send(gov)]

        self.assertEqual(len(token_forms), 1, "the access token is cached between sends")
        self.assertEqual(token_forms[0]["grant_type"], ["refresh_token"])
        self.assertEqual(token_forms[0]["refresh_token"], ["1//refresh"])
        self.assertEqual(token_forms[0]["client_id"], ["cid.apps.googleusercontent.com"])
        self.assertEqual(send_auth, ["Bearer ya29.user-grant", "Bearer ya29.user-grant"])
        for res in results:
            self.assertTrue(res["delivered"], res["error"])
            self.assertEqual(res["gmail_auth_mode"], "refresh_token")

    def test_gmail_error_keeps_the_api_reason_and_drops_the_cached_token(self) -> None:
        def fake_urlopen(req, timeout=None):
            if req.full_url.startswith("https://iamcredentials.googleapis.com/"):
                return _FakeHttpResponse({"signedJwt": "signed.jwt.value"})
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", hdrs=None,
                fp=io.BytesIO(b'{"error": "unauthorized_client", "error_description": "Client is unauthorized"}'),
            )

        gov = CostGovernanceService(project_id="test-proj", token_fn=lambda: "ya29.runtime-adc")
        gov._gmail_token_cache = ("other-mailbox", "ya29.other", float("inf"))
        with (
            _email_env(
                FINOPS_GMAIL_DWD_SERVICE_ACCOUNT="finops-mailer@test-proj.iam.gserviceaccount.com",
                FINOPS_GMAIL_SENDER="finops-alerts@example.com",
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            res = _send(gov)

        self.assertFalse(res["delivered"])
        self.assertEqual(res["delivery_channel"], "GMAIL_API")
        self.assertIn("HTTP 401 from oauth2.googleapis.com", res["error"])
        self.assertIn("unauthorized_client", res["error"])
        self.assertIsNone(gov._gmail_token_cache)

    def test_runtime_adc_token_alone_never_reaches_gmail(self) -> None:
        gov = CostGovernanceService(project_id="test-proj", token_fn=lambda: "ya29.runtime-adc")
        with _email_env(), patch("urllib.request.urlopen") as urlopen:
            res = _send(gov)
        urlopen.assert_not_called()
        self.assertEqual(res["delivery_channel"], "DRY_RUN_PREVIEW")
        self.assertFalse(res["delivered"])

    def test_incomplete_gmail_config_says_what_is_missing(self) -> None:
        gov = CostGovernanceService(project_id="test-proj", token_fn=lambda: "ya29.runtime-adc")
        cases = (
            ({"FINOPS_GMAIL_REFRESH_TOKEN": "1//refresh"}, "FINOPS_GMAIL_CLIENT_ID"),
            ({"FINOPS_GMAIL_DWD_SERVICE_ACCOUNT": "finops-mailer@test-proj.iam.gserviceaccount.com"},
             "FINOPS_GMAIL_SENDER"),
        )
        for env, missing in cases:
            with self.subTest(missing=missing), _email_env(**env), patch("urllib.request.urlopen") as urlopen:
                res = _send(gov)
                urlopen.assert_not_called()
                self.assertEqual(res["delivery_channel"], "GMAIL_API")
                self.assertFalse(res["delivered"])
                self.assertIn(missing, res["error"])

    def test_recipient_lists_are_masked_and_validated(self) -> None:
        self.assertEqual(
            mask_email_for_ui("alice@google.com, bob@example.org"), "a***e@google.com, b***b@example.org"
        )
        self.assertEqual(
            normalize_recipient_list(" cfo@example.com,cto@example.com, cfo@example.com "),
            "cfo@example.com, cto@example.com",
        )
        self.assertEqual(normalize_recipient_list(["cfo@example.com"]), "cfo@example.com")
        self.assertEqual(normalize_recipient_list(None), "")
        too_many = ",".join(f"user{i}@example.com" for i in range(21))
        for bad in ("not-an-email", "cfo@example.com\r\nBcc: x@evil.example", "CFO <cfo@example.com>", too_many):
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                normalize_recipient_list(bad)

    def test_cron_endpoints_honor_the_scheduler_job_bodies(self) -> None:
        import fastapi
        from fastapi.testclient import TestClient

        from finops_dashboard.server import register_api_routes

        ctrl = FinOpsRuntimeController()
        app = fastapi.FastAPI()
        register_api_routes(app, ctrl)
        client = TestClient(app)
        fleet = {"status": "OK", "project_id": "test-project", "agents": [],
                 "unregistered_runtimes": [_idle_engine("re-77")]}
        with _email_env(), patch.object(ctrl, "get_fleet_payload", return_value=fleet):
            warn = client.post(
                "/api/cron/warn_idle_runtimes",
                json={"grace_period_hours": 72, "send_warnings": True, "dry_run_email": True},
            )
            digest = client.post(
                "/api/cron/send_executive_digest",
                json={"cadence": "daily", "dry_run": True, "recipient": "cfo@example.com, cto@example.com"},
            )
            bad = client.post(
                "/api/cron/send_executive_digest",
                json={"cadence": "daily", "dry_run": True, "recipient": "cfo@example.com\nBcc: x@evil.example"},
            )

        self.assertEqual(warn.status_code, 200)
        deadline = _parse_ts(warn.json()["queue"][0]["scheduled_clearance_at"])
        self.assertGreater(deadline, datetime.now(UTC) + timedelta(hours=71))
        self.assertEqual(digest.status_code, 200)
        self.assertEqual(digest.json()["recipient_masked"], "c***o@example.com, c***o@example.com")
        self.assertEqual(digest.json()["delivery"]["delivery_channel"], "DRY_RUN_PREVIEW")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["status"], "INVALID_REQUEST")

    def test_enforced_clearance_reports_what_actually_happened(self) -> None:
        # Production default: no clearance client is wired, so an enforced request runs as a dry run
        # and leaves the runtime pending in WARNED_GRACE_PERIOD so a later enforced run can act.
        gov = CostGovernanceService(project_id="test-proj")
        gov._queue["re-111"] = _due_item("re-111")
        with _email_env(FINOPS_ALLOW_AUTOMATED_CLEARANCE="1"):
            res = gov.execute_scheduled_clearance(mode="enforced", lookup_bq=False, persist_to_bq=False)
        self.assertEqual(res["mode_executed"], "dry_run")
        self.assertEqual(res["would_clear_count"], 1)
        self.assertEqual(res["cleared_count"], 0)
        self.assertEqual(res["actions"][0]["status"], "DRY_RUN")
        self.assertEqual(gov._queue["re-111"]["status"], "WARNED_GRACE_PERIOD")
        self.assertIn("No resources were changed", res["note"])

        # With a wired client, success is CLEARED_ENFORCED and an API error is CLEARANCE_FAILED, not "cleared".
        calls: list[tuple] = []

        def client_ok(method, url, body):
            calls.append((method, url, body))
            return {}

        def client_fails(method, url, body):
            raise RuntimeError("HTTP 400 FAILED_PRECONDITION: engine has sessions")

        for api_call_fn, expected in ((client_ok, "CLEARED_ENFORCED"), (client_fails, "CLEARANCE_FAILED")):
            gov = CostGovernanceService(project_id="test-proj", api_call_fn=api_call_fn)
            gov._queue["re-111"] = _due_item("re-111")
            with self.subTest(expected=expected), _email_env(FINOPS_ALLOW_AUTOMATED_CLEARANCE="1"):
                res = gov.execute_scheduled_clearance(mode="enforced", lookup_bq=False, persist_to_bq=False)
                self.assertEqual(res["mode_executed"], "enforced")
                self.assertIsNone(res["note"])
                self.assertEqual(res["actions"][0]["status"], expected)
                self.assertEqual(res["cleared_count"], int(expected == "CLEARED_ENFORCED"))
                self.assertEqual(res["failed_count"], int(expected == "CLEARANCE_FAILED"))
        self.assertEqual(calls, [(
            "DELETE",
            "https://us-central1-aiplatform.googleapis.com/v1/projects/test-proj/locations/us-central1/"
            "reasoningEngines/111",
            None,
        )])

    def test_cloud_run_scale_to_zero_and_mint_refresh_token_helper(self) -> None:
        from scripts import mint_gmail_refresh_token as mint_helper

        calls: list[tuple] = []

        def fake_api(method, url, body):
            calls.append((method, url, body))
            if method == "GET":
                return {
                    "scaling": {"minInstanceCount": 2},
                    "template": {"scaling": {"minInstanceCount": 1}},
                    "traffic": [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}],
                }
            return {}

        gov = CostGovernanceService(project_id="test-proj", api_call_fn=fake_api)
        past = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
        gov._queue["run-idle-worker"] = {
            "runtime_id": "run-idle-worker",
            "runtime_kind": "cloud_run",
            "resource_name": "projects/test-proj/locations/us-central1/services/idle-worker",
            "display_name": "idle-worker",
            "project_id": "test-proj",
            "location": "us-central1",
            "status": "WARNED_GRACE_PERIOD",
            "warning_sent_at": past,
            "scheduled_clearance_at": past,
            "recommended_action": "scale_to_zero",
            "updated_at": past,
        }
        # First a dry run: leaves item pending in WARNED_GRACE_PERIOD
        with _email_env(FINOPS_ALLOW_AUTOMATED_CLEARANCE="1"):
            dry_res = gov.execute_scheduled_clearance(mode="dry_run", lookup_bq=False, persist_to_bq=False)
            self.assertEqual(dry_res["would_clear_count"], 1)
            self.assertEqual(gov._queue["run-idle-worker"]["status"], "WARNED_GRACE_PERIOD")
            # Now enforced run clears the still-pending item
            enf_res = gov.execute_scheduled_clearance(mode="enforced", lookup_bq=False, persist_to_bq=False)
        self.assertEqual(enf_res["cleared_count"], 1)
        self.assertEqual(gov._queue["run-idle-worker"]["status"], "CLEARED_ENFORCED")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "PATCH")
        self.assertIn("scaling.minInstanceCount", calls[1][1])
        self.assertIn("template.scaling.minInstanceCount", calls[1][1])

        verifier, challenge = mint_helper.make_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        auth_url = mint_helper.build_authorization_url(
            "cid.apps.googleusercontent.com",
            "http://127.0.0.1:54321/",
            "state-123",
            challenge,
            login_hint="finops@example.com",
        )
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.send", auth_url)
        self.assertIn("access_type=offline", auth_url)

    def test_clearance_job_honors_a_snooze_recorded_by_another_instance(self) -> None:
        bq_rows: list[dict] = []

        def fake_bq(sql, timeout_s=8.0, query_parameters=None):
            if sql.lstrip().upper().startswith("SELECT RUNTIME_ID"):
                return [dict(r) for r in bq_rows], {}
            return [], {}

        def bq_ts(dt: datetime) -> str:  # how BigQuery renders CAST(timestamp AS STRING)
            return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00")

        fleet = {"agents": [], "unregistered_runtimes": [_idle_engine("re-77")]}
        for snooze_is_newer, expected_would_clear in ((True, 0), (False, 1)):
            with self.subTest(snooze_is_newer=snooze_is_newer), _email_env():
                bq_rows.clear()
                instance_a = CostGovernanceService(project_id="acme-prod", bq_query_fn=fake_bq)
                instance_a.evaluate_and_warn_idle_runtimes(fleet, send_warnings=True, dry_run_email=True)
                local = instance_a._queue["re-77"]
                self.assertEqual(local["status"], "WARNED_GRACE_PERIOD")
                # On instance A's copy the grace period has passed...
                local["scheduled_clearance_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
                # ...but another instance recorded a snooze in BigQuery (newer, or older in the control case).
                snooze_until = datetime.now(UTC) + timedelta(days=7)
                shift = timedelta(minutes=1 if snooze_is_newer else -1)
                bq_rows.append({
                    "runtime_id": "re-77",
                    "status": "SNOOZED",
                    "snoozed_until": bq_ts(snooze_until),
                    "scheduled_clearance_at": bq_ts(snooze_until),
                    "warning_sent_at": local["warning_sent_at"],
                    "updated_at": bq_ts(_parse_ts(local["updated_at"]) + shift),
                })
                res = instance_a.execute_scheduled_clearance(mode="dry_run", persist_to_bq=False)
                self.assertEqual(res["would_clear_count"], expected_would_clear)
                expected_status = "SNOOZED" if snooze_is_newer else "WARNED_GRACE_PERIOD"
                self.assertEqual(instance_a._queue["re-77"]["status"], expected_status)

    def test_read_only_evaluation_does_not_hide_a_newer_snooze(self) -> None:
        t0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
        bq_rows: list[dict] = []

        def fake_bq(sql, timeout_s=8.0, query_parameters=None):
            if sql.lstrip().upper().startswith("SELECT RUNTIME_ID"):
                return [dict(r) for r in bq_rows], {}
            return [], {}

        def at(hours: float):
            return patch("finops_dashboard.cost_governance._utcnow", return_value=t0 + timedelta(hours=hours))

        fleet = {"agents": [], "unregistered_runtimes": [_idle_engine("re-77")]}
        instance_a = CostGovernanceService(project_id="acme-prod", bq_query_fn=fake_bq)
        with _email_env():
            with at(0):  # the scheduled warn run on instance A
                instance_a.evaluate_and_warn_idle_runtimes(fleet, send_warnings=True, dry_run_email=True)
            bq_rows.append({  # T0+1h: the owner's Keep-Alive click lands on another instance
                "runtime_id": "re-77",
                "status": "SNOOZED",
                "snoozed_until": "2026-10-08 09:00:00+00",
                "scheduled_clearance_at": "2026-10-08 09:00:00+00",
                "warning_sent_at": "2026-10-01 08:00:00+00",
                "updated_at": "2026-10-01 09:00:00+00",
            })
            with at(2):  # a dashboard refresh on instance A (read-only, no BigQuery refresh)
                instance_a.evaluate_and_warn_idle_runtimes(fleet, send_warnings=False, persist_to_bq=False)
            with at(50):  # the clearance job after the 48h grace period, inside the 7-day snooze
                res = instance_a.execute_scheduled_clearance(mode="dry_run", persist_to_bq=False)
        self.assertEqual(res["cleared_count"], 0)
        self.assertEqual(instance_a._queue["re-77"]["status"], "SNOOZED")

    def test_recovered_runtime_gets_a_fresh_warning_when_it_goes_idle_again(self) -> None:
        gov = CostGovernanceService(project_id="test-proj")
        idle = {"agents": [], "unregistered_runtimes": [_idle_engine("re-77")]}
        busy = {"agents": [], "unregistered_runtimes": [{
            "agent_id": "re-77",
            "finops_status": "HEALTHY",
            "backend": {"kind": "agent_engine"},
            "metrics": {"requests": 12, "llm_calls": 4},
        }]}
        kwargs = {"send_warnings": True, "dry_run_email": True, "lookup_bq": False, "persist_to_bq": False}

        first = gov.evaluate_and_warn_idle_runtimes(idle, **kwargs)
        self.assertEqual(len(first["recent_warnings"]), 1)
        gov.evaluate_and_warn_idle_runtimes(busy, **kwargs)
        self.assertEqual(gov._queue["re-77"]["status"], "RECOVERED_ACTIVE")
        # Weeks later the old warning's clearance time is long past, and the runtime is idle again.
        gov._queue["re-77"]["scheduled_clearance_at"] = (datetime.now(UTC) - timedelta(days=20)).isoformat()

        again = gov.evaluate_and_warn_idle_runtimes(idle, **kwargs)
        self.assertEqual(len(again["recent_warnings"]), 1, "a new idle period sends a new warning")
        new_deadline = _parse_ts(gov._queue["re-77"]["scheduled_clearance_at"])
        self.assertGreater(new_deadline, datetime.now(UTC) + timedelta(hours=47))
        res = gov.execute_scheduled_clearance(mode="dry_run", lookup_bq=False, persist_to_bq=False)
        self.assertEqual(res["cleared_count"], 0, "nothing is cleared before the new grace period ends")

    def test_snooze_links_use_the_browser_reachable_base_url(self) -> None:
        idle = {"agents": [], "unregistered_runtimes": [_idle_engine("re-77")]}
        public = "https://finops-dashboard-mcp-app-123.us-central1.run.app"
        dashboard = "https://finops-tokenomics-dashboard-123.us-central1.run.app"
        for override, expected in ((f"{dashboard}/", dashboard), ("http://attacker.example.com", public)):
            with (
                self.subTest(override=override),
                _email_env(FINOPS_SNOOZE_BASE_URL=override, FINOPS_PUBLIC_URL=public),
            ):
                gov = CostGovernanceService(project_id="test-proj")
                payload = gov.evaluate_and_warn_idle_runtimes(
                    idle, send_warnings=False, lookup_bq=False, persist_to_bq=False
                )
                self.assertTrue(payload["queue"][0]["snooze_7d_url"].startswith(
                    f"{expected}/api/governance/snooze?runtime_id=re-77&days=7&token="
                ))

    def test_warning_emails_are_sent_without_holding_the_governance_lock(self) -> None:
        gov = CostGovernanceService(project_id="test-proj")
        lock_free_during_send: list[bool] = []

        def fake_urlopen(req, timeout=None):
            # /api/state takes the same lock from another request thread while a send is in flight.
            probe_result: list[bool] = []

            def probe() -> None:
                acquired = gov._lock.acquire(timeout=2)
                if acquired:
                    gov._lock.release()
                probe_result.append(acquired)

            worker = threading.Thread(target=probe)
            worker.start()
            worker.join()
            lock_free_during_send.append(probe_result[0])
            return _FakeHttpResponse({"id": "msg-1"})

        idle = {"agents": [], "unregistered_runtimes": [_idle_engine("re-77")]}
        with (
            _email_env(FINOPS_GMAIL_ACCESS_TOKEN="ya29.manual-test-token"),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            payload = gov.evaluate_and_warn_idle_runtimes(
                idle, send_warnings=True, dry_run_email=False, lookup_bq=False, persist_to_bq=False
            )
        self.assertEqual(lock_free_during_send, [True])
        self.assertTrue(payload["recent_warnings"][0]["delivered"], payload["recent_warnings"][0]["error"])

    def test_cloud_scheduler_script_creates_then_updates_jobs(self) -> None:
        script = Path(__file__).resolve().parent.parent / "deploy" / "setup_cloud_scheduler.sh"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = tmp_path / "gcloud_calls.jsonl"
            state = tmp_path / "jobs"
            state.mkdir()
            fake_gcloud = tmp_path / "gcloud"
            fake_gcloud.write_text(_FAKE_GCLOUD.replace("{python}", sys.executable), encoding="utf-8")
            fake_gcloud.chmod(0o755)
            env = {
                key: value for key, value in os.environ.items()
                if key not in ("SERVICE_URL", "SCHEDULER_SA_EMAIL", "CLEARANCE_MODE", "DIGEST_CADENCE")
            }
            env.update({
                "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}",
                "PROJECT_ID": "acme-prod",
                "REGION": "us-central1",
                "DIGEST_RECIPIENTS": "cfo@example.com,cto@example.com",
                "FAKE_GCLOUD_LOG": str(log),
                "FAKE_GCLOUD_STATE": str(state),
            })

            def run(**extra: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["bash", str(script)], env={**env, **extra},
                    capture_output=True, text=True, timeout=60, check=False,
                )

            def scheduler_calls(verb: str) -> list[list[str]]:
                rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                return [r for r in rows if r[:4] == ["scheduler", "jobs", verb, "http"]]

            created_run = run()
            self.assertEqual(created_run.returncode, 0, created_run.stderr)
            created = scheduler_calls("create")
            self.assertEqual(len(created), 3)
            bodies = {}
            for args in created:
                self.assertIn("--headers=Content-Type=application/json", args)
                body = next(a.split("=", 1)[1] for a in args if a.startswith("--message-body="))
                bodies[args[4]] = json.loads(body)
            self.assertEqual(bodies["finops-warn-idle-runtimes"]["grace_period_hours"], 48)
            self.assertEqual(bodies["finops-execute-scheduled-clearance"], {"mode": "dry_run"})
            self.assertEqual(bodies["finops-send-executive-digest"]["recipient"], "cfo@example.com,cto@example.com")

            # Re-running is idempotent: existing jobs are updated with the flags `jobs update http` accepts.
            updated_run = run(CLEARANCE_MODE="enforced", DIGEST_CADENCE="weekly")
            self.assertEqual(updated_run.returncode, 0, updated_run.stderr)
            updated = scheduler_calls("update")
            self.assertEqual(len(updated), 3)
            for args in updated:
                self.assertIn("--update-headers=Content-Type=application/json", args)
            digest_job = next(a for a in updated if a[4] == "finops-send-executive-digest")
            self.assertIn("--schedule=30 8 * * 1", digest_job)
            clearance_job = next(a for a in updated if a[4] == "finops-execute-scheduled-clearance")
            self.assertIn('--message-body={"mode":"enforced"}', clearance_job)

            for bad_env in ({"CLEARANCE_MODE": "execute"}, {"DIGEST_RECIPIENTS": 'cfo@example.com","x":"y'}):
                with self.subTest(bad_env=bad_env):
                    bad = run(**bad_env)
                    self.assertNotEqual(bad.returncode, 0)
                    self.assertIn("ERROR", bad.stderr)

    def test_governance_score_accountability_ledger_and_configurable_thresholds(self) -> None:
        from finops_dashboard.cost_governance import compute_governance_score

        fleet = {
            "agents": [
                {
                    "agent_id": "ag-1",
                    "trace_logging": {"enabled": True},
                    "registration": {"status": "REGISTERED"},
                    "metrics": {"input_tokens": 6000, "cached_tokens": 3600, "output_tokens": 900, "llm_calls": 10},
                },
            ],
            "model_usage": {
                "models": [
                    {
                        "model": "gemini-3.8-flash",
                        "input_tokens": 2400,
                        "cache_read_tokens": 3600,
                        "output_tokens": 900,
                        "reasoning_tokens": 100,
                    }
                ],
                "totals": {"est_cost_usd": 12.0},
            },
            "unregistered_runtimes": [],
            "live_finops": {"spend_drift": {"status": "LIVE", "change_pct": 4.0}},
        }
        uc = {"ge_sessions": [{"session_id": "s1", "user_email": "alice@example.com"}]}
        agy = {"kpis": {"purchased_seats": 100, "assigned_seats": 100, "active_seats_7d": 90}}
        gdu = {
            "days": [
                {"day": f"2026-10-{d:02d}", "ai_net_usd": 10.0}
                for d in range(1, 16)
            ]
        }

        score = compute_governance_score(
            fleet_payload=fleet,
            user_centric_payload=uc,
            antigravity_payload=agy,
            ge_daily_usage=gdu,
            monthly_budget_usd=500.0,
            cache_hit_target_pct=60.0,
            seat_utilization_target_pct=90.0,
        )
        self.assertEqual(score["score"], 100)
        self.assertEqual(score["grade"], "A")
        acct = score["accountability"]
        self.assertTrue(acct["sum_check_ok"])
        self.assertEqual(acct["total_points_earned"], 100)
        self.assertEqual(acct["total_points_possible"], 100)
        self.assertEqual(acct["measured_points_earned"], 100)
        self.assertEqual(acct["measured_points_possible"], 100)
        self.assertEqual(acct["unconfigured_neutral_points"], 0)
        self.assertEqual(acct["unconfigured_points_possible"], 0)
        self.assertEqual(acct["unconfigured_components"], [])
        self.assertEqual(acct["thresholds"]["cache_hit_target_pct"], 60.0)
        self.assertEqual(acct["thresholds"]["seat_utilization_target_pct"], 90.0)
        self.assertEqual(acct["thresholds"]["monthly_budget_usd"], 500.0)

        for p in score["pillars"]:
            comp_pts = sum(c["points"] for c in p["components"])
            comp_max = sum(c["max_points"] for c in p["components"])
            self.assertEqual(comp_pts, p["score"], p["id"])
            self.assertEqual(comp_max, p["max_score"], p["id"])

        # Verify evaluate_and_warn_idle_runtimes forwards all payloads so governance_score is never evaluated against None
        gov = CostGovernanceService(project_id="test-proj", token_fn=lambda: None)
        eval_res = gov.evaluate_and_warn_idle_runtimes(
            fleet,
            send_warnings=False,
            lookup_bq=False,
            persist_to_bq=False,
            user_centric_payload=uc,
            antigravity_payload=agy,
            ge_daily_usage=gdu,
        )
        embedded_gov = eval_res["governance_score"]
        self.assertTrue(embedded_gov["accountability"]["sum_check_ok"])
        self.assertGreaterEqual(embedded_gov["accountability"]["measured_points_possible"], 88)

    def test_snooze_token_redaction_expiry_and_route_guards(self) -> None:
        # 1. Timestamped token expiry verification
        exp_future = int((datetime.now(UTC) + timedelta(days=7)).timestamp())
        exp_past = int((datetime.now(UTC) - timedelta(seconds=10)).timestamp())
        tok_future = sign_snooze_token("re-exp-1", days=7, expires_at=exp_future)
        tok_past = sign_snooze_token("re-exp-1", days=7, expires_at=exp_past)
        self.assertTrue(verify_snooze_token("re-exp-1", 7, tok_future))
        self.assertFalse(verify_snooze_token("re-exp-1", 7, tok_past))

        # 2. Public queue & recent_warnings redact HMAC token query param
        import fastapi
        from fastapi.testclient import TestClient

        from finops_dashboard.server import register_api_routes

        ctrl = FinOpsRuntimeController()
        fake_fleet = {
            "status": "OK",
            "unregistered_runtimes": [_idle_engine("re-55443322")],
            "agents": [],
        }
        app = fastapi.FastAPI()
        register_api_routes(app, ctrl)
        client = TestClient(app)

        with patch.object(ctrl, "get_fleet_payload", return_value=fake_fleet):
            r_warn = client.post(
                "/api/governance/evaluate_idle",
                json={"send_warnings": True, "dry_run_email": True},
            )
            self.assertEqual(r_warn.status_code, 200)
            warn_data = r_warn.json()
            q_item = warn_data["queue"][0]
            self.assertNotIn("snooze_token", q_item)
            self.assertTrue(q_item["snooze_7d_url"].endswith("token=REDACTED"))
            notif = warn_data["recent_warnings"][0]
            self.assertNotIn(sign_snooze_token("re-55443322", 7), notif["text_preview"])
            self.assertIn("token=REDACTED", notif["text_preview"])
            self.assertIn("token=REDACTED", notif["html_preview"])
            self.assertTrue(notif["metadata"]["snooze_7d_url"].endswith("token=REDACTED"))

            # 3. GET /api/governance/snooze without token is rejected with 403
            r_get_no_tok = client.get("/api/governance/snooze?runtime_id=re-55443322&days=7")
            self.assertEqual(r_get_no_tok.status_code, 403)

            # 4. Tokenless POST /api/governance/snooze on unqueued phantom ID is rejected with 404
            r_post_phantom = client.post(
                "/api/governance/snooze",
                json={"runtime_id": "re-phantom-999", "days": 90},
            )
            self.assertEqual(r_post_phantom.status_code, 404)
            self.assertNotIn("re-phantom-999", ctrl.cost_governance._queue)

            # 5. Tokenless POST /api/governance/snooze on queued runtime succeeds and caps days at 7
            r_post_queued = client.post(
                "/api/governance/snooze",
                json={"runtime_id": "re-55443322", "days": 90},
            )
            self.assertEqual(r_post_queued.status_code, 200)
            self.assertEqual(r_post_queued.json()["days"], 7)
            self.assertEqual(r_post_queued.json()["governance_status"], "SNOOZED")

    def test_mcp_compact_state_and_embedded_governance_bridge(self) -> None:
        from finops_dashboard import mcp_server
        from finops_dashboard.ui.template import render_dashboard_html

        ctrl = FinOpsRuntimeController()
        fake_state = {
            "window_hours": 24,
            "project_id": "test-proj",
            "live_finops": {"spend_drift": {"cost_now_usd": 12.5}},
            "cost_governance": {
                "status": "OK",
                "automated_clearance_enabled": False,
                "summary": {
                    "idle_candidates": 2,
                    "pending_clearance": 1,
                    "snoozed": 0,
                    "cleared": 0,
                    "estimated_monthly_savings_usd": 195.0,
                },
                "governance_score": {
                    "score": 82,
                    "grade": "B",
                    "status": "STRONG",
                    "pillars": [{"id": "runtime_hygiene", "score": 15, "max": 20}],
                },
            },
        }
        compact = mcp_server._compact_state_for_mcp_open(fake_state)
        self.assertIn("cost_governance", compact)
        self.assertEqual(compact["cost_governance"]["governance_score"]["score"], 82)
        self.assertEqual(compact["cost_governance"]["summary"]["idle_candidates"], 2)

        import asyncio

        with (
            patch.object(mcp_server, "_get_controller", return_value=ctrl),
            patch.object(ctrl, "get_state_payload", return_value=fake_state),
            patch.object(
                ctrl,
                "executive_digest_payload",
                return_value={"status": "OK", "subject": "[FinOps Weekly Digest]", "delivery": {"sent": False}},
            ),
        ):
            res = asyncio.run(
                mcp_server._tool_get_finops_state(
                    "test-session",
                    {"window_hours": 24, "include_executive_digest": True},
                )
            )
            text = res["content"][0]["text"]
            self.assertIn("Governance score: 82/100 (B)", text)
            self.assertIn("executive_digest_preview", res["structuredContent"])
            self.assertEqual(
                res["structuredContent"]["executive_digest_preview"]["subject"],
                "[FinOps Weekly Digest]",
            )

        html = render_dashboard_html()
        self.assertIn("finops.costSubView", html)
        self.assertIn("finops.otelLayerFilter", html)
        self.assertIn("include_executive_digest: true", html)


if __name__ == "__main__":
    unittest.main()



