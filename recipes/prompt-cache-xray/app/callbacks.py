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

"""ADK callbacks that let the agent observe its own prompt-cache behaviour.

Two model callbacks keep a small, JSON-serializable record in session state:

* ``record_prompt_snapshot`` (``before_model_callback``) renders the outgoing
  ``LlmRequest`` -- system instruction, tool declarations and contents, in
  the order Gemini sees them -- to text, and keeps the last two renderings
  under ``STATE_SNAPSHOTS``. Two consecutive snapshots are exactly what the
  X-Ray needs to say whether this agent's own prefix is being reused.
* ``record_usage`` (``after_model_callback``) appends each completed
  response's ``usage_metadata`` (prompt, cached, output and thinking tokens)
  under ``STATE_USAGE_LOG``, and attaches the prompt token count to the
  latest snapshot so the X-Ray can scale its estimates from a logged figure
  instead of a characters-per-token heuristic.

Both callbacks return ``None``: they observe, they never alter the request
or the response. Streaming chunks (``llm_response.partial``) are ignored;
only the final aggregated response of a call is recorded.

State is re-assigned rather than mutated in place so the change lands in the
event's ``state_delta`` and persists with any session service, not only the
in-memory one.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app import prompt_xray

STATE_SNAPSHOTS = "prompt_cache_xray_snapshots"
STATE_USAGE_LOG = "prompt_cache_xray_usage_log"
SNAPSHOT_LIMIT = 2
USAGE_LOG_LIMIT = 50
MAX_SNAPSHOT_CHARS = prompt_xray.MAX_PROMPT_CHARS


def _system_instruction_text(instruction: Any) -> str:
    """Flattens the forms ADK and google-genai accept for system_instruction."""
    if instruction is None:
        return ""
    if isinstance(instruction, str):
        return instruction
    if isinstance(instruction, types.Content):
        return "\n".join(_part_text(p) for p in instruction.parts or [])
    if isinstance(instruction, types.Part):
        return _part_text(instruction)
    if isinstance(instruction, (list, tuple)):
        return "\n".join(_system_instruction_text(item) for item in instruction)
    return str(instruction)


def _compact(obj: Any) -> str:
    """Stable, compact JSON for non-text parts. Key order is preserved."""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _part_text(part: types.Part) -> str:
    if part.text:
        return part.text
    if part.function_call is not None:
        call = part.function_call
        return _compact(
            {"function_call": {"name": call.name, "args": call.args}}
        )
    if part.function_response is not None:
        resp = part.function_response
        return _compact(
            {
                "function_response": {
                    "name": resp.name,
                    "response": resp.response,
                }
            }
        )
    if part.inline_data is not None:
        blob = part.inline_data
        size = len(blob.data or b"")
        return f"[inline_data {blob.mime_type or 'application/octet-stream'} {size} bytes]"
    if part.file_data is not None:
        return f"[file_data {part.file_data.file_uri}]"
    return _compact(part.model_dump(exclude_none=True, mode="json"))


def _tool_declarations_text(config: types.GenerateContentConfig | None) -> str:
    if config is None or not config.tools:
        return ""
    lines: list[str] = []
    for tool in config.tools:
        if not isinstance(tool, types.Tool):
            lines.append(_compact(str(tool)))
            continue
        for decl in tool.function_declarations or []:
            lines.append(
                _compact(decl.model_dump(exclude_none=True, mode="json"))
            )
        for name in (
            "google_search",
            "code_execution",
            "retrieval",
            "url_context",
        ):
            if getattr(tool, name, None) is not None:
                lines.append(f"[builtin tool {name}]")
    return "\n".join(lines)


def render_llm_request(llm_request: LlmRequest) -> str:
    """Renders an LlmRequest as text in the order Gemini tokenizes it.

    System instruction first, then tool declarations, then the conversation
    contents. The rendering is a proxy for the token stream, which is enough
    to locate the first byte that differs between two consecutive requests.
    """
    blocks: list[str] = []
    system_text = _system_instruction_text(
        getattr(llm_request.config, "system_instruction", None)
    )
    if system_text:
        blocks.append("[system]\n" + system_text)
    tools_text = _tool_declarations_text(llm_request.config)
    if tools_text:
        blocks.append("[tools]\n" + tools_text)
    for content in llm_request.contents or []:
        role = content.role or "message"
        body = "\n".join(_part_text(p) for p in content.parts or [])
        blocks.append(f"[{role}]\n{body}")
    return "\n\n".join(blocks)


def record_prompt_snapshot(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """before_model_callback: keep the last two rendered requests in state."""
    text = render_llm_request(llm_request)
    truncated = len(text) > MAX_SNAPSHOT_CHARS
    snapshot = {
        "invocation_id": callback_context.invocation_id,
        "agent": callback_context.agent_name,
        "model": llm_request.model,
        "chars": len(text),
        "truncated": truncated,
        "prompt_token_count": None,
        "prompt": text[:MAX_SNAPSHOT_CHARS],
    }
    snapshots = list(callback_context.state.get(STATE_SNAPSHOTS) or [])
    snapshots.append(snapshot)
    callback_context.state[STATE_SNAPSHOTS] = snapshots[-SNAPSHOT_LIMIT:]
    return None


def usage_entry(
    usage: types.GenerateContentResponseUsageMetadata,
    *,
    invocation_id: str | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    """A JSON-friendly summary of one response's usage_metadata."""
    prompt = int(usage.prompt_token_count or 0)
    cached = int(usage.cached_content_token_count or 0)
    return {
        "invocation_id": invocation_id,
        "model_version": model_version,
        "prompt_token_count": prompt,
        "cached_content_token_count": cached,
        "uncached_input_tokens": max(0, prompt - cached),
        "candidates_token_count": int(usage.candidates_token_count or 0),
        "thoughts_token_count": int(usage.thoughts_token_count or 0),
        "total_token_count": int(usage.total_token_count or 0),
        "cache_hit_pct": round(cached / prompt * 100.0, 2) if prompt else 0.0,
    }


def record_usage(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """after_model_callback: log usage_metadata of each completed response."""
    if llm_response.partial or llm_response.usage_metadata is None:
        return None
    entry = usage_entry(
        llm_response.usage_metadata,
        invocation_id=callback_context.invocation_id,
        model_version=llm_response.model_version,
    )
    log = list(callback_context.state.get(STATE_USAGE_LOG) or [])
    log.append(entry)
    callback_context.state[STATE_USAGE_LOG] = log[-USAGE_LOG_LIMIT:]

    # Ground the latest snapshot's token estimate in the logged count.
    snapshots = list(callback_context.state.get(STATE_SNAPSHOTS) or [])
    if snapshots and snapshots[-1].get("prompt_token_count") is None:
        snapshots[-1] = {
            **snapshots[-1],
            "prompt_token_count": entry["prompt_token_count"],
        }
        callback_context.state[STATE_SNAPSHOTS] = snapshots
    return None
