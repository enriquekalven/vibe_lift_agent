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

"""Unit tests for the model callbacks and the tools that read their state.

Real ADK context objects are used (InvocationContext, CallbackContext,
ToolContext) so the tests also pin the ADK API surface the recipe relies on.
No model is called.
"""

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event_actions import EventActions
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools import ToolContext
from google.genai import types

from app import agent, callbacks, prompt_xray


def _invocation(invocation_id: str = "inv-1") -> InvocationContext:
    session = Session(id="s1", app_name="app", user_id="u1", state={})
    return InvocationContext(
        session_service=InMemorySessionService(),
        invocation_id=invocation_id,
        agent=agent.root_agent,
        session=session,
    )


def _commit(ctx: CallbackContext, invocation: InvocationContext) -> None:
    """Applies the callback's state_delta the way the runner would."""
    invocation.session.state.update(ctx._event_actions.state_delta)


def _request(*turns: str, system: str = "You are a test agent.") -> LlmRequest:
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=t)])
        for t in turns
    ]
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=[
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="lookup", description="Looks up."
                    )
                ]
            )
        ],
    )
    return LlmRequest(model="gemini-test", contents=contents, config=config)


def _usage(
    prompt: int, cached: int
) -> types.GenerateContentResponseUsageMetadata:
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=12,
        thoughts_token_count=3,
        total_token_count=prompt + 15,
    )


def test_render_orders_system_tools_then_contents() -> None:
    req = _request("hello")
    req.contents.append(
        types.Content(
            role="model",
            parts=[
                types.Part.from_function_call(name="lookup", args={"q": "x"})
            ],
        )
    )
    req.contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="lookup", response={"result": 1}
                )
            ],
        )
    )
    text = callbacks.render_llm_request(req)
    assert text.startswith("[system]\nYou are a test agent.")
    assert text.index("[tools]") < text.index("[user]")
    assert '"name":"lookup"' in text
    assert '"function_call":{"name":"lookup","args":{"q":"x"}}' in text
    assert (
        '"function_response":{"name":"lookup","response":{"result":1}}' in text
    )


def test_snapshots_are_capped_and_usage_grounds_the_latest_one() -> None:
    invocation = _invocation()
    for i in range(3):
        ctx = CallbackContext(invocation, event_actions=EventActions())
        assert (
            callbacks.record_prompt_snapshot(ctx, _request(*["m"] * (i + 1)))
            is None
        )
        _commit(ctx, invocation)
    snaps = invocation.session.state[callbacks.STATE_SNAPSHOTS]
    assert len(snaps) == callbacks.SNAPSHOT_LIMIT
    assert snaps[-1]["chars"] > snaps[-2]["chars"]
    assert snaps[-1]["prompt_token_count"] is None
    assert snaps[-1]["invocation_id"] == "inv-1"
    assert snaps[-1]["model"] == "gemini-test"

    ctx = CallbackContext(invocation, event_actions=EventActions())
    partial = LlmResponse(partial=True, usage_metadata=_usage(999, 0))
    assert callbacks.record_usage(ctx, partial) is None
    assert callbacks.STATE_USAGE_LOG not in ctx._event_actions.state_delta

    final = LlmResponse(
        usage_metadata=_usage(1000, 800), model_version="gemini-test-001"
    )
    assert callbacks.record_usage(ctx, final) is None
    _commit(ctx, invocation)
    log = invocation.session.state[callbacks.STATE_USAGE_LOG]
    assert len(log) == 1
    assert log[0]["cache_hit_pct"] == 80.0
    assert log[0]["uncached_input_tokens"] == 200
    assert log[0]["model_version"] == "gemini-test-001"
    snaps = invocation.session.state[callbacks.STATE_SNAPSHOTS]
    assert snaps[-1]["prompt_token_count"] == 1000
    assert snaps[-2]["prompt_token_count"] is None


def test_usage_log_is_capped() -> None:
    invocation = _invocation()
    for _ in range(callbacks.USAGE_LOG_LIMIT + 5):
        ctx = CallbackContext(invocation, event_actions=EventActions())
        callbacks.record_usage(ctx, LlmResponse(usage_metadata=_usage(10, 0)))
        _commit(ctx, invocation)
    assert (
        len(invocation.session.state[callbacks.STATE_USAGE_LOG])
        == callbacks.USAGE_LOG_LIMIT
    )


def test_session_tools_before_and_after_two_model_calls() -> None:
    invocation = _invocation()
    tool_ctx = ToolContext(invocation, event_actions=EventActions())
    assert agent.xray_my_session(tool_ctx)["status"] == "NOT_ENOUGH_SNAPSHOTS"
    assert agent.get_session_token_usage(tool_ctx)["status"] == "EMPTY"

    # Call 1: the user says hi. Call 2: the conversation grew by one turn.
    for turns, usage in (
        (("hi",), _usage(2500, 0)),
        (("hi", "x-ray my session"), _usage(2600, 2400)),
    ):
        ctx = CallbackContext(invocation, event_actions=EventActions())
        callbacks.record_prompt_snapshot(ctx, _request(*turns))
        callbacks.record_usage(ctx, LlmResponse(usage_metadata=usage))
        _commit(ctx, invocation)

    tool_ctx = ToolContext(invocation, event_actions=EventActions())
    report = agent.xray_my_session(tool_ctx)
    assert report["status"] == prompt_xray.STATUS_APPEND_ONLY
    assert report["source"]["kind"] == "session_callbacks"
    assert report["token_basis"] == "scaled_from_logged_input_tokens"
    assert report["current_tokens"] == 2600
    assert report["observed"]["cached_content_token_count"] == 2400
    assert report["model_calls_recorded"] == 2
    assert "heatmap" not in report

    usage = agent.get_session_token_usage(tool_ctx)
    assert usage["status"] == "OK"
    assert usage["model_calls"] == 2
    assert usage["totals"]["prompt_token_count"] == 5100
    assert usage["totals"]["cached_content_token_count"] == 2400
    assert usage["cost_at_list_price"]["priced"] is True
    assert len(usage["recent_calls"]) == 2


def test_changed_system_instruction_is_reported_as_busted() -> None:
    invocation = _invocation()
    for system in (
        "Current time: 2026-10-01T09:14:07Z\n\nBe helpful.",
        "Current time: 2026-10-01T09:14:52Z\n\nBe helpful.",
    ):
        ctx = CallbackContext(invocation, event_actions=EventActions())
        callbacks.record_prompt_snapshot(ctx, _request("hi", system=system))
        _commit(ctx, invocation)
    report = agent.xray_my_session(
        ToolContext(invocation, event_actions=EventActions())
    )
    assert report["status"] == prompt_xray.STATUS_BUSTED
    assert report["breakpoint"]["line"] == 2  # line 1 is the [system] marker
    assert "timestamp" in {c["kind"] for c in report["causes"]}
