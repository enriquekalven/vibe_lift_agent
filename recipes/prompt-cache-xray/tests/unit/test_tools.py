# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the stateless tools, the instruction provider and the agent wiring."""

import os
from unittest import mock

from google.adk.agents.readonly_context import ReadonlyContext

from app import agent, callbacks, pricing, prompt_xray


def test_builtin_example_runs_when_both_prompts_are_empty() -> None:
    report = agent.xray_prompt_cache()
    assert report["status"] == prompt_xray.STATUS_BUSTED
    assert report["source"] == {"kind": "builtin_example"}
    assert report["breakpoint"]["line"] == 2
    assert report["model"] == os.environ["MODEL_NAME"]
    assert "heatmap" not in report
    assert len(report["volatile_findings"]) <= 10


def test_pasted_prompts_and_monthly_volume() -> None:
    report = agent.xray_prompt_cache(
        "a\nb\nc", "a\nX\nc", monthly_requests=1000
    )
    assert report["status"] == prompt_xray.STATUS_BUSTED
    assert report["breakpoint"]["line"] == 2
    assert report["source"] == {"kind": "pasted"}
    assert report["monthly_requests"] == 1000
    assert agent.xray_prompt_cache("same", "same")["status"] == (
        prompt_xray.STATUS_IDENTICAL
    )


def test_invalid_input_is_an_error_payload_not_an_exception() -> None:
    assert agent.xray_prompt_cache("", "only one side")["status"] == "ERROR"
    too_long = "x" * (prompt_xray.MAX_PROMPT_CHARS + 1)
    assert agent.xray_prompt_cache(too_long, "x")["status"] == "ERROR"


def test_long_rewrites_are_truncated_for_the_model_context() -> None:
    body = "static policy line\n" * 2000
    report = agent.xray_prompt_cache(
        "Time: 2026-10-01T09:00:00Z\n" + body,
        "Time: 2026-10-01T09:05:00Z\n" + body,
    )
    assert report["rewrite"]["rewritten_prompt_truncated"] is True
    assert len(report["rewrite"]["rewritten_prompt"]) == 12_000


def test_calculate_cache_economics_defaults_to_configured_model() -> None:
    priced = agent.calculate_cache_economics(100_000, 80_000)
    assert priced["model"] == os.environ["MODEL_NAME"]
    assert priced["priced"] is True
    assert priced["cache_hit_pct"] == 80.0
    assert priced["saved_usd"] > 0
    unpriced = agent.calculate_cache_economics(100_000, 80_000, model="nope-1")
    assert unpriced["priced"] is False
    assert unpriced["saved_usd"] is None


def test_instruction_provider_is_static_unless_demo_buster_is_on() -> None:
    ctx = mock.create_autospec(ReadonlyContext, instance=True)
    with mock.patch.dict(os.environ, {agent.DEMO_CACHE_BUSTER_ENV: "false"}):
        assert agent.build_instruction(ctx) == agent.build_instruction(ctx)
        assert agent.build_instruction(ctx) == agent.INSTRUCTION
    with mock.patch.dict(os.environ, {agent.DEMO_CACHE_BUSTER_ENV: "true"}):
        first, second = (
            agent.build_instruction(ctx),
            agent.build_instruction(ctx),
        )
    assert first.startswith("Current time: ")
    assert first != second  # a fresh request id every call
    assert first.endswith(agent.INSTRUCTION)
    # The sabotaged header is exactly what the engine flags.
    kinds = {
        f["kind"]
        for f in prompt_xray.scan_volatile_spans(first.split("\n\n")[0])
    }
    assert {"timestamp", "uuid"} <= kinds


def test_agent_wiring() -> None:
    tool_names = {getattr(t, "__name__", None) for t in agent.root_agent.tools}
    assert tool_names == {
        "xray_prompt_cache",
        "xray_my_session",
        "get_session_token_usage",
        "calculate_cache_economics",
    }
    assert (
        agent.root_agent.before_model_callback
        is callbacks.record_prompt_snapshot
    )
    assert agent.root_agent.after_model_callback is callbacks.record_usage
    assert agent.root_agent.instruction is agent.build_instruction
    assert agent.app.name == "app"
    assert pricing.rate_card_for(os.environ["MODEL_NAME"]) is not None, (
        "MODEL_NAME in .env.example should have a rate card so the demo prices"
    )


def test_create_agent_requires_model_name() -> None:
    with (
        mock.patch.dict(os.environ, {"MODEL_NAME": ""}),
        mock.patch.object(agent, "_model_name", return_value=""),
    ):
        try:
            agent.create_agent()
        except RuntimeError as exc:
            assert "MODEL_NAME" in str(exc)
        else:
            raise AssertionError(
                "create_agent() should refuse an empty MODEL_NAME"
            )
