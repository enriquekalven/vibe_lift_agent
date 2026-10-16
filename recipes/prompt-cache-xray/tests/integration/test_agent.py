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

"""Integration test: a live model call through the Runner.

Excluded from CI (needs credentials and a model). Run locally with:

    uv run pytest tests/integration

It checks the part unit tests cannot: that ADK really invokes the callbacks
around a Gemini call, so after one turn the session holds a prompt snapshot
and a usage_metadata entry with a real token count.
"""

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app import callbacks
from app.agent import app as adk_app


def test_callbacks_record_a_live_turn() -> None:
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="it_user", app_name="app"
    )
    runner = Runner(app=adk_app, session_service=session_service)

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text="Run the built-in prompt cache example and tell me on"
                " which line caching breaks."
            )
        ],
    )
    events = list(
        runner.run(
            new_message=message, user_id="it_user", session_id=session.id
        )
    )
    assert events, "Expected at least one event"

    called = [
        part.function_call.name
        for event in events
        for part in (event.content.parts if event.content else [])
        if part.function_call
    ]
    assert "xray_prompt_cache" in called

    final = session_service.get_session_sync(
        app_name="app", user_id="it_user", session_id=session.id
    )
    assert final is not None
    usage_log = final.state[callbacks.STATE_USAGE_LOG]
    snapshots = final.state[callbacks.STATE_SNAPSHOTS]
    assert usage_log and usage_log[-1]["prompt_token_count"] > 0
    assert snapshots and snapshots[-1]["prompt_token_count"] is not None
    assert snapshots[-1]["prompt"].startswith("[system]\n")
