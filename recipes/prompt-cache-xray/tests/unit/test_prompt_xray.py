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

"""Unit tests for the pure X-Ray engine (no model, no network)."""

import pytest

from app import pricing, prompt_xray

MODEL = "gemini-3.8-flash"


def _heatmap_length(segments: list[dict]) -> int:
    return sum(
        int(s["chars"]) if s.get("collapsed") else len(s["text"])
        for s in segments
    )


def test_example_breaks_on_timestamp_line_and_rewrite_is_remeasured() -> None:
    r = prompt_xray.analyze(
        prompt_xray.EXAMPLE_PREVIOUS,
        prompt_xray.EXAMPLE_CURRENT,
        model=MODEL,
        monthly_requests=100_000,
    )
    assert r["status"] == prompt_xray.STATUS_BUSTED
    assert r["breakpoint"]["line"] == 2
    assert "Current time:" in r["breakpoint"]["current_line"]
    assert "timestamp" in {c["kind"] for c in r["causes"]}
    assert r["cached_prefix_pct"] < 5.0
    assert r["recoverable_tokens_per_request"] > 2000
    # Below the documented 2,048-token minimum today; above it after the fix.
    assert r["cache_minimum_tokens"] == 2048
    assert r["below_cache_minimum"] is True
    rw = r["rewrite"]
    assert rw["changed"] is True
    assert rw["projected_cached_prefix_pct"] > 95.0
    assert rw["projected_below_cache_minimum"] is False
    assert rw["json_lines_key_sorted"] == 1
    assert {m["line"] for m in rw["moved_lines"]} == {2, 3}
    assert (
        rw["rewritten_prompt"].rstrip().endswith("User: sam.chen@example.com")
    )
    assert (
        rw["projected_stranded_cost_usd_per_1k_requests"]
        < r["stranded_cost_usd_per_1k_requests"]
    )


def test_cost_is_rate_card_arithmetic_and_monthly_needs_volume() -> None:
    card = pricing.rate_card_for(MODEL)
    assert card is not None
    with_volume = prompt_xray.analyze(
        prompt_xray.EXAMPLE_PREVIOUS,
        prompt_xray.EXAMPLE_CURRENT,
        model=MODEL,
        monthly_requests=1000,
    )
    expected = (
        with_volume["recoverable_tokens_per_request"]
        * card.cache_discount_per_million_usd
        / 1e6
    )
    assert with_volume["stranded_cost_usd_per_request"] == pytest.approx(
        expected, abs=1e-8
    )
    assert with_volume["stranded_cost_usd_per_month"] == pytest.approx(
        round(expected * 1000, 2)
    )
    no_volume = prompt_xray.analyze(
        prompt_xray.EXAMPLE_PREVIOUS, prompt_xray.EXAMPLE_CURRENT, model=MODEL
    )
    assert no_volume["stranded_cost_usd_per_month"] is None
    assert any("request volume" in n for n in no_volume["notes"])


def test_unknown_model_omits_dollars_instead_of_guessing() -> None:
    r = prompt_xray.analyze("a\nb", "a\nc", model="not-a-model")
    assert r["rate_card"] is None
    assert r["stranded_cost_usd_per_1k_requests"] is None
    assert r["cache_minimum_tokens"] is None
    assert any("No rate card" in n for n in r["notes"])


def test_append_only_identical_and_shorter() -> None:
    grow = prompt_xray.analyze(
        "static\nhistory 1", "static\nhistory 1\nhistory 2", model=MODEL
    )
    assert grow["status"] == prompt_xray.STATUS_APPEND_ONLY
    assert grow["breakpoint"] is None
    assert grow["recoverable_tokens_per_request"] == 0
    assert [s["state"] for s in grow["heatmap"]] == ["cached", "appended"]
    same = prompt_xray.analyze("abc", "abc", model=MODEL)
    assert same["status"] == prompt_xray.STATUS_IDENTICAL
    assert same["cached_prefix_pct"] == 100.0
    shorter = prompt_xray.analyze("abcdef", "abc", model=MODEL)
    assert shorter["status"] == prompt_xray.STATUS_SHORTER


def test_classifies_json_key_order_whitespace_and_content_edit() -> None:
    tail = "\nstatic tail that stays the same"
    js = prompt_xray.analyze(
        '{"a": 1, "b": 2}' + tail, '{"b": 2, "a": 1}' + tail, model=MODEL
    )
    assert js["causes"][0]["kind"] == "json_key_order"
    ws = prompt_xray.analyze(
        "x\n  indented" + tail, "x\n    indented" + tail, model=MODEL
    )
    assert ws["causes"][0]["kind"] == "whitespace"
    edit = prompt_xray.analyze(
        "Be terse." + tail, "Be verbose." + tail, model=MODEL
    )
    assert edit["causes"][0]["kind"] == "content_edit"


def test_detects_uuid_email_and_epoch() -> None:
    kinds = {
        f["kind"]
        for f in prompt_xray.scan_volatile_spans(
            "req 123e4567-e89b-12d3-a456-426614174000 by bob@example.com"
            " at 1759300000"
        )
    }
    assert {"uuid", "email", "epoch"} <= kinds


def test_heatmap_covers_prompt_and_collapses_long_runs() -> None:
    long_static = "policy line\n" * 900
    r = prompt_xray.analyze(
        "Time: 2026-10-01T09:00:00Z\n" + long_static,
        "Time: 2026-10-01T09:05:00Z\n" + long_static,
        model=MODEL,
    )
    assert _heatmap_length(r["heatmap"]) == r["current_chars"]
    assert any(s.get("collapsed") for s in r["heatmap"])
    assert "buster" in {s["state"] for s in r["heatmap"]}


def test_logged_token_count_scales_tokens() -> None:
    curr = "x" * 1000
    r = prompt_xray.analyze(
        "y" + curr[1:], curr, model=MODEL, current_input_tokens=500
    )
    assert r["token_basis"] == "scaled_from_logged_input_tokens"
    assert r["current_tokens"] == 500


def test_rewrite_moves_dynamic_json_keys_and_sorts_fenced_blocks() -> None:
    prompt = (
        "Policy header\n"
        '{"tenant": "acme", "request_id": "123e4567-e89b-12d3-a456-426614174000"}\n'
        "```json\n"
        '{"b": 1,\n "a": 2}\n'
        "```\n"
        "Static tail\n"
    )
    rw = prompt_xray.propose_cache_friendly_rewrite(prompt)
    assert rw["changed"] is True
    rewritten = rw["rewritten_prompt"]
    assert prompt_xray.DYNAMIC_SECTION_HEADER in rewritten
    static_part, dynamic_part = rewritten.split(
        prompt_xray.DYNAMIC_SECTION_HEADER
    )
    assert '"tenant": "acme"' in static_part
    assert "request_id" in dynamic_part
    assert '{"a": 2, "b": 1}' in static_part


def test_rejects_empty_and_oversized() -> None:
    with pytest.raises(ValueError, match="empty"):
        prompt_xray.analyze("", "x", model=MODEL)
    with pytest.raises(ValueError, match="characters"):
        prompt_xray.analyze(
            "x" * (prompt_xray.MAX_PROMPT_CHARS + 1), "x", model=MODEL
        )
