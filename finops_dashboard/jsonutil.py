"""Typed helpers for navigating untyped JSON-like payloads (dashboard state, API responses).

`payload.get(key) if isinstance(payload.get(key), Mapping) else {}` is the idiom this codebase uses
to walk loosely-shaped JSON. Type checkers cannot narrow through the repeated `.get()` call, so these
helpers express the same check once with a precise return type. Behavior is identical: the value is
returned unchanged when it has the expected type, otherwise a fresh empty container.
"""

from collections.abc import Mapping
from typing import Any


def as_mapping(value: object) -> Mapping[str, Any]:
  """Returns `value` if it is a Mapping, else a new empty dict."""
  return value if isinstance(value, Mapping) else {}


def as_list(value: object) -> list[Any]:
  """Returns `value` if it is a list, else a new empty list."""
  return value if isinstance(value, list) else []
