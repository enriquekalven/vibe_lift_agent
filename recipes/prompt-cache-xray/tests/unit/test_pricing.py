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

"""Unit tests for the rate cards and cost arithmetic."""

import pytest

from app import pricing

MODEL = "gemini-3.8-flash"


def test_price_turn_is_tokens_times_list_price() -> None:
    card = pricing.rate_card_for(MODEL)
    assert card is not None
    r = pricing.price_turn(MODEL, 100_000, 80_000, candidates_token_count=1_000)
    assert r["priced"] is True
    assert r["cache_hit_pct"] == 80.0
    out = 1_000 / 1e6 * card.output_per_million_usd
    naive = 100_000 / 1e6 * card.input_per_million_usd + out
    actual = (
        80_000 / 1e6 * card.cached_read_per_million_usd
        + 20_000 / 1e6 * card.input_per_million_usd
        + out
    )
    assert r["naive_cost_usd"] == pytest.approx(naive, abs=1e-6)
    assert r["actual_cost_usd"] == pytest.approx(actual, abs=1e-6)
    assert r["saved_usd"] == pytest.approx(naive - actual, abs=1e-6)
    assert r["rate_card"]["source"] == pricing.PRICING_URL


def test_unknown_model_is_unpriced_not_guessed() -> None:
    r = pricing.price_turn("gemini-99-experimental", 50_000, 25_000)
    assert r["priced"] is False
    assert r["naive_cost_usd"] is None
    assert r["actual_cost_usd"] is None
    assert r["saved_usd"] is None
    assert r["cache_hit_pct"] == 50.0
    assert "No rate card" in r["note"]


def test_inputs_are_clamped_and_long_model_names_normalized() -> None:
    r = pricing.price_turn(f"publishers/google/models/{MODEL}", "1000", 5_000)
    assert r["model"] == MODEL
    assert r["priced"] is True
    assert r["cached_tokens"] == 1000  # never more than the prompt
    bad = pricing.price_turn(MODEL, None, float("nan"))
    assert bad["prompt_tokens"] == 0
    assert bad["cache_hit_pct"] == 0.0
    assert pricing.rate_card_for(None) is None
    assert pricing.rate_card_for("") is None


def test_cache_minimum_by_family() -> None:
    assert pricing.cache_minimum_tokens("gemini-3.8-flash") == 2048
    assert pricing.cache_minimum_tokens("gemini-3.1-flash") == 4096
    assert pricing.cache_minimum_tokens("gemini-2.5-pro") == 2048
    assert pricing.cache_minimum_tokens("claude-opus-4-6") is None


def test_rate_cards_json_override_adds_and_replaces_cards() -> None:
    env = {
        "RATE_CARDS_JSON": (
            '{"my-tuned-model": {"input": 1, "cached_read": 0.1,'
            ' "cache_write": 1, "output": 4},'
            f' "{MODEL}": {{"input": 9, "cached_read": 0.9,'
            ' "cache_write": 9, "output": 9},'
            ' "broken": {"input": "x"}}'
        )
    }
    cards = pricing.load_rate_cards(env)
    assert cards["my-tuned-model"].output_per_million_usd == 4
    assert cards[MODEL].input_per_million_usd == 9
    assert "broken" not in cards
    # Built-ins stay intact; the override does not leak into the module.
    assert pricing.BUILTIN_RATE_CARDS[MODEL].input_per_million_usd != 9


def test_malformed_override_is_ignored() -> None:
    assert pricing.load_rate_cards({"RATE_CARDS_JSON": "not json"}) == (
        pricing.BUILTIN_RATE_CARDS
    )
    assert pricing.load_rate_cards({"RATE_CARDS_JSON": "[1, 2]"}) == (
        pricing.BUILTIN_RATE_CARDS
    )
    assert pricing.load_rate_cards({}) == pricing.BUILTIN_RATE_CARDS
