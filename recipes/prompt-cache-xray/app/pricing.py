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

"""List-price rate cards and the cache-aware cost arithmetic built on them.

Every dollar figure in this recipe is `tokens x list price`. Nothing is
estimated from a model's "typical" cost: a model without a rate card is
reported as unpriced (``priced=False``, ``None`` dollars) rather than priced
with another model's card.

Rate cards go out of date. Override or extend them without touching code by
setting ``RATE_CARDS_JSON`` (see ``.env.example``), for example::

    RATE_CARDS_JSON={"gemini-3.8-flash": {"input": 0.30, "cached_read": 0.03,
                     "cache_write": 0.30, "output": 2.50}}

All prices are USD per 1M tokens.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Prices below are the Vertex AI list prices per 1M tokens (prompts up to
# 200K tokens, global endpoint) published at
# https://cloud.google.com/vertex-ai/generative-ai/pricing when this recipe
# was written. Verify them against that page before relying on a figure, or
# override them with RATE_CARDS_JSON.
PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"

# Documented minimum prompt size for implicit and explicit caching, per the
# Vertex AI context caching overview. A cacheable prefix below this size is
# never cached, however static it is.
CACHE_MINIMUM_TOKENS_BY_FAMILY: tuple[tuple[str, int], ...] = (
    ("gemini-3.8-flash", 2048),
    ("gemini-3", 4096),
    ("gemini-2", 2048),
)


@dataclasses.dataclass(frozen=True)
class RateCard:
    """USD per 1M tokens for one model."""

    model_name: str
    input_per_million_usd: float
    cached_read_per_million_usd: float
    cache_write_per_million_usd: float
    output_per_million_usd: float

    @property
    def cache_discount_per_million_usd(self) -> float:
        """What one million cached tokens save versus uncached input."""
        return self.input_per_million_usd - self.cached_read_per_million_usd


def _card(
    name: str, input_: float, cached: float, write: float, output: float
) -> RateCard:
    return RateCard(name, input_, cached, write, output)


BUILTIN_RATE_CARDS: dict[str, RateCard] = {
    "gemini-3.8-flash": _card("gemini-3.8-flash", 0.30, 0.03, 0.30, 2.50),
    "gemini-3.1-flash": _card("gemini-3.1-flash", 1.25, 0.125, 1.56, 5.00),
    "gemini-3.1-flash-lite": _card(
        "gemini-3.1-flash-lite", 0.50, 0.05, 0.625, 2.00
    ),
    "gemini-2.5-pro": _card("gemini-2.5-pro", 1.25, 0.125, 1.25, 10.00),
    "gemini-2.5-flash": _card("gemini-2.5-flash", 0.30, 0.03, 0.30, 2.50),
    "gemini-2.5-flash-lite": _card(
        "gemini-2.5-flash-lite", 0.10, 0.01, 0.10, 0.40
    ),
}

_JSON_KEYS = {
    "input": "input_per_million_usd",
    "cached_read": "cached_read_per_million_usd",
    "cache_write": "cache_write_per_million_usd",
    "output": "output_per_million_usd",
}


def _parse_override(raw: str) -> dict[str, RateCard]:
    """Parses RATE_CARDS_JSON. A malformed value is logged and ignored."""
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("RATE_CARDS_JSON is not valid JSON; ignoring it.")
        return {}
    if not isinstance(data, Mapping):
        logger.warning("RATE_CARDS_JSON must be a JSON object; ignoring it.")
        return {}
    cards: dict[str, RateCard] = {}
    for model, prices in data.items():
        if not isinstance(prices, Mapping):
            continue
        try:
            fields = {
                attr: float(prices[key]) for key, attr in _JSON_KEYS.items()
            }
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "RATE_CARDS_JSON entry %r needs numeric input, cached_read, "
                "cache_write and output; ignoring it.",
                model,
            )
            continue
        if any(not math.isfinite(v) or v < 0 for v in fields.values()):
            continue
        cards[str(model)] = RateCard(model_name=str(model), **fields)
    return cards


def load_rate_cards(
    environ: Mapping[str, str] | None = None,
) -> dict[str, RateCard]:
    """Built-in cards, with RATE_CARDS_JSON entries added on top."""
    env = os.environ if environ is None else environ
    cards = dict(BUILTIN_RATE_CARDS)
    raw = (env.get("RATE_CARDS_JSON") or "").strip()
    if raw:
        cards.update(_parse_override(raw))
    return cards


RATE_CARDS: dict[str, RateCard] = load_rate_cards()


def normalize_model_name(model: str | None) -> str:
    """'publishers/google/models/gemini-x' and 'models/gemini-x' -> 'gemini-x'."""
    return str(model or "").strip().rsplit("/", 1)[-1]


def rate_card_for(model: str | None) -> RateCard | None:
    """The model's own rate card, or None. Never another model's card."""
    name = normalize_model_name(model)
    return RATE_CARDS.get(name) if name else None


def cache_minimum_tokens(model: str | None) -> int | None:
    """Documented minimum cacheable prompt size for the model family."""
    name = normalize_model_name(model).lower()
    for family, minimum in CACHE_MINIMUM_TOKENS_BY_FAMILY:
        if name.startswith(family):
            return minimum
    return None


def _nonneg_int(value: object, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(0, int(number))


def price_turn(
    model: str | None,
    prompt_token_count: object,
    cached_content_token_count: object = 0,
    candidates_token_count: object = 0,
    thoughts_token_count: object = 0,
    cache_write_token_count: object = 0,
) -> dict[str, Any]:
    """Prices one model call at list price, with and without the cache.

    ``prompt_token_count`` is the full input (it already includes the cached
    tokens, as in Gemini's ``usage_metadata``). Thinking tokens are billed as
    output. Returns a JSON-friendly dict; dollar fields are ``None`` when the
    model has no rate card.
    """
    prompt = _nonneg_int(prompt_token_count)
    cached = min(prompt, _nonneg_int(cached_content_token_count))
    output = _nonneg_int(candidates_token_count) + _nonneg_int(
        thoughts_token_count
    )
    written = _nonneg_int(cache_write_token_count)
    hit_pct = round(cached / prompt * 100.0, 2) if prompt else 0.0
    card = rate_card_for(model)
    result: dict[str, Any] = {
        "model": normalize_model_name(model),
        "priced": card is not None,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "uncached_tokens": prompt - cached,
        "output_tokens": output,
        "cache_hit_pct": hit_pct,
        "naive_cost_usd": None,
        "actual_cost_usd": None,
        "saved_usd": None,
    }
    if card is None:
        result["note"] = (
            f"No rate card for {normalize_model_name(model)!r}, so nothing was"
            " priced. Models with a rate card: "
            + ", ".join(sorted(RATE_CARDS))
            + ". Add one with RATE_CARDS_JSON."
        )
        return result
    per_m = 1_000_000.0
    out_usd = output / per_m * card.output_per_million_usd
    naive = prompt / per_m * card.input_per_million_usd + out_usd
    actual = (
        cached / per_m * card.cached_read_per_million_usd
        + (prompt - cached) / per_m * card.input_per_million_usd
        + written / per_m * card.cache_write_per_million_usd
        + out_usd
    )
    result.update(
        naive_cost_usd=round(naive, 6),
        actual_cost_usd=round(actual, 6),
        saved_usd=round(max(0.0, naive - actual), 6),
        rate_card={
            "input_per_million_usd": card.input_per_million_usd,
            "cached_read_per_million_usd": card.cached_read_per_million_usd,
            "output_per_million_usd": card.output_per_million_usd,
            "source": PRICING_URL,
        },
    )
    return result
