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

"""Prompt Cache X-Ray agent.

A single ADK agent with four tools and two observing callbacks:

* ``xray_prompt_cache`` compares two pasted prompt snapshots (or the built-in
  example) and reports where prefix caching broke, what it costs and a
  re-measured cache-friendly rewrite.
* ``xray_my_session`` runs the same comparison on the last two requests this
  agent itself sent to Gemini, recorded by ``before_model_callback``.
* ``get_session_token_usage`` reports the ``usage_metadata`` recorded by
  ``after_model_callback`` -- real cached-token counts, priced at list price.
* ``calculate_cache_economics`` prices a given token count with and without
  the cache.

Set ``DEMO_CACHE_BUSTER=true`` to prepend a timestamp and a request id to the
system instruction -- the two most common ways agents defeat their own cache
-- and watch ``xray_my_session`` flip from APPEND_ONLY to PREFIX_BUSTED.
"""

from __future__ import annotations

import datetime
import os
import uuid
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from app import callbacks, pricing, prompt_xray

DEMO_CACHE_BUSTER_ENV = "DEMO_CACHE_BUSTER"
_FINDINGS_LIMIT = 10
_REWRITE_CHARS_LIMIT = 12_000
_CALLS_IN_REPORT = 10

INSTRUCTION = """You are Prompt Cache X-Ray, an assistant that measures and explains Gemini prompt caching for ADK agents.

What you know:
- Gemini reuses only an identical leading prefix of a request: system instruction first, then tool declarations, then the conversation. One value that changes near the top (a timestamp, a request id, a user e-mail, JSON keys in a different order) makes everything after it uncacheable on every request.
- Implicit caching is best-effort and needs a minimum prefix size; explicit caching (ADK ContextCacheConfig) follows the same prefix rule.
- Cached input tokens are billed at a fraction of the uncached input price, so the cost of a broken prefix is (uncached - cached price) x stranded tokens x requests.

Your tools:
- xray_prompt_cache: compare two versions of a prompt the user pastes. Call it with both prompts empty to run the built-in example. It reports the status (IDENTICAL, APPEND_ONLY, SHORTER_PREFIX or PREFIX_BUSTED), the breaking line and column, the likely cause, the share of the prompt that is still cacheable, the stranded tokens and their list-price cost, and a rewritten prompt whose cached share was re-measured.
- xray_my_session: run the same comparison on the last two requests you yourself sent to the model, recorded by your callbacks. Use it whenever the user asks about this conversation, this session, or your own prompt.
- get_session_token_usage: the per-call token log recorded by your callbacks, with real cached-token counts, cache hit rates and list-price cost.
- calculate_cache_economics: price a given token count with and without the cache.

Rules:
- Quote only numbers the tools returned. Never estimate, round up or extrapolate a figure a tool did not return. If a model has no rate card, say the cost is unavailable rather than guessing.
- Say whether token figures are logged counts or estimates (the tool reports token_basis), and that implicit caching is best-effort.
- When the status is PREFIX_BUSTED, name the line, the cause and the concrete fix: move the dynamic value to the end of the prompt, serialize JSON with sorted keys, or normalize whitespace.
- Lead with the verdict, keep answers short, and offer to show the rewritten prompt instead of pasting it unasked.
"""


def _model_name() -> str:
    """The configured model id. app/__init__.py loads it from .env."""
    return (os.getenv("MODEL_NAME") or "").strip()


def _demo_cache_buster_enabled() -> bool:
    value = (os.getenv(DEMO_CACHE_BUSTER_ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def build_instruction(readonly_context: ReadonlyContext) -> str:
    """InstructionProvider: the static instruction, optionally sabotaged.

    With DEMO_CACHE_BUSTER on, the first two lines change on every request.
    That is the anti-pattern this recipe exists to catch: run xray_my_session
    and it reports PREFIX_BUSTED on line 2 with a timestamp as the cause.
    """
    del readonly_context  # The instruction does not depend on session state.
    if _demo_cache_buster_enabled():
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        return (
            f"Current time: {now}\nRequest id: {uuid.uuid4()}\n\n{INSTRUCTION}"
        )
    return INSTRUCTION


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Trims the X-Ray report before it goes back into the model's context.

    The heatmap repeats the whole prompt and the rewrite can be long; feeding
    both to the model would inflate the very prompt this recipe is about.
    """
    out = dict(report)
    out.pop("heatmap", None)
    out["volatile_findings"] = list(report.get("volatile_findings") or [])[
        :_FINDINGS_LIMIT
    ]
    rewrite = dict(report.get("rewrite") or {})
    text = str(rewrite.get("rewritten_prompt") or "")
    if len(text) > _REWRITE_CHARS_LIMIT:
        rewrite["rewritten_prompt"] = text[:_REWRITE_CHARS_LIMIT]
        rewrite["rewritten_prompt_truncated"] = True
    out["rewrite"] = rewrite
    return out


def xray_prompt_cache(
    previous_prompt: str = "",
    current_prompt: str = "",
    monthly_requests: int = 0,
) -> dict[str, Any]:
    """Finds where two snapshots of a prompt stop sharing a cacheable prefix.

    Compares the prompt sent on one request with the prompt sent on the next
    and reports the first differing line, the likely cache-buster on it, how
    much of the prompt is still cacheable, the stranded tokens and their
    list-price cost, and a cache-friendly rewrite with a re-measured cached
    share. Leave both prompts empty to analyze the built-in example.

    Args:
        previous_prompt: Full prompt text from the earlier request (turn N-1).
        current_prompt: Full prompt text from the later request (turn N).
        monthly_requests: Monthly request volume for a monthly cost figure.
            0 omits the monthly figure; per-1,000-request cost needs none.

    Returns:
        The X-Ray report: status, breakpoint, causes, cached_prefix_pct,
        recoverable_tokens_per_request, stranded cost fields, rewrite and
        notes. Dollar fields are null when the model has no rate card.
    """
    source: dict[str, Any] = {"kind": "pasted"}
    if not previous_prompt and not current_prompt:
        previous_prompt = prompt_xray.EXAMPLE_PREVIOUS
        current_prompt = prompt_xray.EXAMPLE_CURRENT
        source = {"kind": "builtin_example"}
    try:
        report = prompt_xray.analyze(
            previous_prompt,
            current_prompt,
            model=_model_name(),
            monthly_requests=monthly_requests or None,
            source=source,
        )
    except ValueError as exc:
        return {"status": "ERROR", "error": str(exc)}
    return _compact_report(report)


def xray_my_session(tool_context: ToolContext) -> dict[str, Any]:
    """X-rays the last two requests this agent itself sent to the model.

    Uses the request snapshots and usage_metadata that the agent's callbacks
    recorded in session state, so the result describes this very
    conversation: whether the previous request's prefix was reusable for the
    current one, and how many cached tokens the model actually reported.

    Returns:
        The X-Ray report for the agent's own last two requests, plus an
        'observed' block with the logged token counts of the latest call.
        Returns status NOT_ENOUGH_SNAPSHOTS until two model calls were made.
    """
    snapshots = list(tool_context.state.get(callbacks.STATE_SNAPSHOTS) or [])
    usage_log = list(tool_context.state.get(callbacks.STATE_USAGE_LOG) or [])
    if len(snapshots) < 2:
        return {
            "status": "NOT_ENOUGH_SNAPSHOTS",
            "snapshots_recorded": len(snapshots),
            "message": "Two model calls are needed to compare prefixes. Send one"
            " more message and ask again.",
        }
    previous, current = snapshots[-2], snapshots[-1]
    try:
        report = prompt_xray.analyze(
            str(previous.get("prompt") or ""),
            str(current.get("prompt") or ""),
            model=str(current.get("model") or _model_name()),
            current_input_tokens=current.get("prompt_token_count"),
            source={
                "kind": "session_callbacks",
                "previous_invocation_id": previous.get("invocation_id"),
                "current_invocation_id": current.get("invocation_id"),
                "snapshot_truncated": bool(
                    previous.get("truncated") or current.get("truncated")
                ),
            },
        )
    except ValueError as exc:
        return {"status": "ERROR", "error": str(exc)}
    out = _compact_report(report)
    out["observed"] = usage_log[-1] if usage_log else None
    out["model_calls_recorded"] = len(usage_log)
    return out


def get_session_token_usage(tool_context: ToolContext) -> dict[str, Any]:
    """Reports the token usage this agent's callbacks logged for the session.

    Every completed model call contributes its usage_metadata: prompt tokens,
    cached tokens, output and thinking tokens. Totals are priced at list
    price with and without the cache for the configured model.

    Returns:
        status, totals, cost_at_list_price (null dollars when the model has
        no rate card) and the most recent calls. Status EMPTY before the
        first model call completes.
    """
    log = list(tool_context.state.get(callbacks.STATE_USAGE_LOG) or [])
    if not log:
        return {
            "status": "EMPTY",
            "message": "No completed model call has been recorded in this"
            " session yet.",
        }
    keys = (
        "prompt_token_count",
        "cached_content_token_count",
        "candidates_token_count",
        "thoughts_token_count",
    )
    totals = {k: sum(int(e.get(k) or 0) for e in log) for k in keys}
    prompt = totals["prompt_token_count"]
    cached = totals["cached_content_token_count"]
    model = _model_name()
    return {
        "status": "OK",
        "model": model,
        "model_calls": len(log),
        "totals": {
            **totals,
            "uncached_input_tokens": max(0, prompt - cached),
            "cache_hit_pct": round(cached / prompt * 100.0, 2)
            if prompt
            else 0.0,
        },
        "cost_at_list_price": pricing.price_turn(
            model,
            prompt,
            cached,
            totals["candidates_token_count"],
            totals["thoughts_token_count"],
        ),
        "recent_calls": log[-_CALLS_IN_REPORT:],
        "note": "Counts come from usage_metadata on each response. Cost is"
        " tokens x list price for the configured MODEL_NAME, not a bill.",
    }


def calculate_cache_economics(
    prompt_token_count: int,
    cached_content_token_count: int,
    candidates_token_count: int = 0,
    thoughts_token_count: int = 0,
    model: str = "",
) -> dict[str, Any]:
    """Prices one model call at list price, with and without the prompt cache.

    Args:
        prompt_token_count: Total input tokens, including the cached ones.
        cached_content_token_count: Input tokens served from the cache.
        candidates_token_count: Output tokens (0 if unknown).
        thoughts_token_count: Thinking tokens, billed as output (0 if unknown).
        model: Model id to price with; defaults to the configured MODEL_NAME.

    Returns:
        cache_hit_pct, naive_cost_usd (no cache), actual_cost_usd and
        saved_usd. A model without a rate card returns priced=false and null
        dollar fields with a note listing the models that have one.
    """
    return pricing.price_turn(
        model or _model_name(),
        prompt_token_count,
        cached_content_token_count,
        candidates_token_count,
        thoughts_token_count,
    )


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the agent."""
    model_name = _model_name()
    if not model_name:
        raise RuntimeError(
            "MODEL_NAME is not set. Copy .env.example to .env (app/__init__.py"
            " loads it) or export MODEL_NAME before starting the agent."
        )
    return Agent(
        name="prompt_cache_xray",
        model=Gemini(
            model=model_name,
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description="Finds where an agent's prompt prefix stops being"
        " cacheable, what the stranded tokens cost, and how to fix it.",
        instruction=build_instruction,
        tools=[
            xray_prompt_cache,
            xray_my_session,
            get_session_token_usage,
            calculate_cache_economics,
        ],
        before_model_callback=callbacks.record_prompt_snapshot,
        after_model_callback=callbacks.record_usage,
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
