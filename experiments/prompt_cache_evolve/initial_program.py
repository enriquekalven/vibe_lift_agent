"""Initial program for Evolutionary Prompt Cache Prefix Canonicalization & Reordering.

Given multi-turn enterprise agent prompts containing interleaved static system instructions,
tool schemas, multi-line JSON payloads, RAG context, and volatile per-turn tokens (ISO-8601
timestamps, UUIDs/request IDs, user emails, unsorted JSON keys, mixed JSON objects with both
static schemas and volatile fields, and trailing whitespace variance), evolve
`propose_cache_friendly_rewrite(prompt: str) -> dict[str, Any]` to maximize the shared
byte-identical cached prefix across consecutive turns while preserving 100% of semantic content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

DYNAMIC_SECTION_HEADER = '### Dynamic context (moved last so the static prefix above stays cacheable)'

_MONTHS = 'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'

# ORIGIN: vibelift/prompt_xray.py::_DETECTORS (lines 53-81)
_DETECTORS: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    ('timestamp', 'Timestamp',
     re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?'),
     'Move the timestamp out of the system prompt into the final user turn, or round it to the day.'),
    ('uuid', 'UUID / request id',
     re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'),
     'Per-request ids belong in request metadata or the last turn, never in the prefix.'),
    ('email', 'User e-mail',
     re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
     'Per-user identity splits the cache per user; pass it after the static instructions.'),
    ('date', 'Date',
     re.compile(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(?:' + _MONTHS + r')[a-z]*\.? \d{1,2},? \d{4}\b'),
     'A date changes daily and busts the cache at midnight; move it to the end of the prompt.'),
    ('clock_time', 'Clock time',
     re.compile(r'\b\d{1,2}:\d{2}:\d{2}\b|\b\d{1,2}:\d{2}\s?(?:AM|PM|am|pm|UTC|GMT|Z)\b'),
     'Clock times change every request; move them to the final user turn.'),
    ('epoch', 'Unix epoch',
     re.compile(r'\b1[5-9]\d{8}(?:\d{3})?\b'),
     'Epoch timestamps change every request; move them to the final user turn.'),
    ('hex_id', 'Hex id / hash',
     re.compile(r'\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{16,}\b'),
     'Session or trace hashes are unique per request; keep them out of the prefix.'),
    ('dynamic_label', 'Dynamic field label',
     re.compile(
         r'(?i)\b(?:current[ _-]?(?:time|date|datetime)|today(?:\'s date)?|timestamp|now|'
         r'request[ _-]?id|session[ _-]?id|trace[ _-]?id|correlation[ _-]?id|'
         r'user[ _-]?(?:name|id|email)|customer[ _-]?(?:name|id))\s*[:=]'),
     'This field looks per-request; render it after the static instructions.'),
)
_DETECTOR_BY_KIND = {d[0]: d for d in _DETECTORS}
_EXTRA_CAUSES = {
    'json_key_order': ('JSON key order',
                       'Same JSON data, different key order. Serialize with sort_keys=True '
                       '(json.dumps(obj, sort_keys=True)) so the bytes are stable.'),
    'whitespace': ('Whitespace only',
                   'Only whitespace changed. Normalize indentation and trailing spaces before sending.'),
    'content_edit': ('Content edit',
                     'The text itself changed. If this is an intentional instruction update the cache '
                     're-warms on the next requests; if it varies per request, move it to the end.'),
}


def _finding(kind: str, line_no: int, start: int, end: int, text: str) -> dict[str, Any]:
  label, advice = (_DETECTOR_BY_KIND[kind][1], _DETECTOR_BY_KIND[kind][3]) if kind in _DETECTOR_BY_KIND \
      else _EXTRA_CAUSES[kind]
  return {'kind': kind, 'label': label, 'line': line_no, 'start': start, 'end': end,
          'match': text[start:end][:120], 'advice': advice}


def scan_volatile_spans(text: str) -> list[dict[str, Any]]:
  """Returns non-overlapping volatile-value matches (timestamps, ids, e-mails, ...) in ``text``."""
  findings: list[dict[str, Any]] = []
  offset = 0
  for line_no, line in enumerate(text.split('\n'), start=1):
    taken: list[tuple[int, int]] = []
    for kind, _label, pattern, _advice in _DETECTORS:
      for m in pattern.finditer(line):
        s, e = m.start(), m.end()
        if any(s < te and ts < e for ts, te in taken):
          continue
        taken.append((s, e))
        findings.append(_finding(kind, line_no, offset + s, offset + e, text))
    offset += len(line) + 1
  findings.sort(key=lambda f: f['start'])
  return findings


def _parse_json_fragment(fragment: str) -> Any:
  frag = fragment.strip().rstrip(',')
  if not frag or frag[0] not in '{[':
    return None
  try:
    return json.loads(frag)
  except ValueError:
    return None


# ORIGIN: vibelift/prompt_xray.py::propose_cache_friendly_rewrite (lines 243-281)
# EVOLVE-BLOCK-START
_VOLATILE_JSON_KEY_RE = re.compile(
    r'^(?:current[ _-]?(?:time|date|datetime)|today|timestamp|now|'
    r'request[ _-]?id|session[ _-]?id|trace[ _-]?id|correlation[ _-]?id|'
    r'user[ _-]?(?:name|id|email)|customer[ _-]?(?:name|id)|nonce|span[ _-]?id)$',
    re.IGNORECASE,
)


def _is_volatile_json_value(key: str, val: Any) -> bool:
  """Returns True when a JSON key or scalar value carries per-turn volatile state."""
  if _VOLATILE_JSON_KEY_RE.match(str(key)):
    return True
  if isinstance(val, str):
    return bool(scan_volatile_spans(val))
  if isinstance(val, int) and 1_500_000_000 <= val <= 2_500_000_000_000:
    return True
  return False


def _partition_json_dict(obj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
  """Splits a JSON dict into (static_dict, dynamic_dict) recursively while preserving all keys."""
  static_part: dict[str, Any] = {}
  dynamic_part: dict[str, Any] = {}
  for k, v in obj.items():
    if _is_volatile_json_value(k, v):
      dynamic_part[k] = v
    elif isinstance(v, dict):
      s_sub, d_sub = _partition_json_dict(v)
      if s_sub:
        static_part[k] = s_sub
      if d_sub:
        dynamic_part[k] = d_sub
    else:
      static_part[k] = v
  return static_part, dynamic_part


def _canonicalize_multiline_blocks(prompt: str) -> tuple[str, int]:
  """Canonicalizes fenced ```json blocks and multi-line JSON objects into deterministic sorted lines."""
  lines = prompt.split('\n')
  out_lines: list[str] = []
  sorted_blocks = 0
  i = 0
  n = len(lines)
  while i < n:
    line = lines[i].rstrip()
    stripped = line.strip()
    # Case 1: Fenced ```json ... ``` block
    if stripped.startswith('```json'):
      j = i + 1
      block_lines: list[str] = []
      while j < n and not lines[j].strip().startswith('```'):
        block_lines.append(lines[j])
        j += 1
      if j < n:
        raw_block = '\n'.join(block_lines).strip()
        parsed = _parse_json_fragment(raw_block)
        if isinstance(parsed, (dict, list)):
          canonical = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
          if canonical != raw_block:
            sorted_blocks += 1
          out_lines.append(line)
          out_lines.append(canonical)
          out_lines.append(lines[j].rstrip())
          i = j + 1
          continue
    # Case 2: Unfenced multi-line JSON object starting with '{' and not closing on the same line
    if stripped.startswith('{') and not stripped.rstrip(',').endswith('}'):
      acc = [lines[i]]
      j = i + 1
      parsed_multi = None
      while j < n and (j - i) <= 60:
        acc.append(lines[j])
        candidate = '\n'.join(acc).strip()
        if candidate.rstrip(',').endswith('}'):
          parsed_multi = _parse_json_fragment(candidate)
          if isinstance(parsed_multi, dict):
            break
        j += 1
      if isinstance(parsed_multi, dict):
        indent = line[:len(line) - len(line.lstrip())]
        trailing = ',' if acc[-1].rstrip().endswith(',') else ''
        canonical = indent + json.dumps(parsed_multi, sort_keys=True, ensure_ascii=False) + trailing
        sorted_blocks += 1
        out_lines.append(canonical)
        i = j + 1
        continue
    out_lines.append(line)
    i += 1
  return '\n'.join(out_lines), sorted_blocks


def propose_cache_friendly_rewrite(prompt: str) -> dict[str, Any]:
  """Moves volatile values/keys to the end, normalizes trailing spaces, and sorts JSON keys."""
  normalized_prompt, block_sorts = _canonicalize_multiline_blocks(prompt)
  kept: list[str] = []
  moved: list[dict[str, Any]] = []
  moved_texts: list[str] = []
  sorted_json = block_sorts
  findings = scan_volatile_spans(normalized_prompt)
  kinds_by_line: dict[int, list[str]] = {}
  for f in findings:
    kinds_by_line.setdefault(int(f['line']), []).append(str(f['kind']))

  in_dynamic_footer = False
  for line_no, raw_line in enumerate(normalized_prompt.split('\n'), start=1):
    line = raw_line.rstrip()
    if line.strip() == DYNAMIC_SECTION_HEADER:
      in_dynamic_footer = True
      continue
    parsed = _parse_json_fragment(line)
    if isinstance(parsed, dict):
      indent = line[:len(line) - len(line.lstrip())]
      trailing = ',' if line.rstrip().endswith(',') else ''
      static_dict, dynamic_dict = _partition_json_dict(parsed)
      if static_dict and dynamic_dict and not in_dynamic_footer:
        static_line = indent + json.dumps(static_dict, sort_keys=True, ensure_ascii=False) + trailing
        dynamic_line = indent + json.dumps(dynamic_dict, sort_keys=True, ensure_ascii=False)
        sorted_json += 1
        kept.append(static_line)
        moved.append({
            'line': line_no,
            'text': dynamic_line[:200],
            'kinds': sorted(set(kinds_by_line.get(line_no) or ['dynamic_label'])),
        })
        moved_texts.append(dynamic_line)
        continue
      normalized = indent + json.dumps(parsed, sort_keys=True, ensure_ascii=False) + trailing
      if normalized != line:
        sorted_json += 1
        line = normalized
    if in_dynamic_footer:
      if line.strip():
        moved.append({
            'line': line_no,
            'text': line[:200],
            'kinds': sorted(set(kinds_by_line.get(line_no) or ['dynamic_label'])),
        })
        moved_texts.append(line)
      continue
    if line_no in kinds_by_line:
      moved.append({'line': line_no, 'text': line[:200], 'kinds': sorted(set(kinds_by_line[line_no]))})
      moved_texts.append(line)
    else:
      kept.append(line)

  if moved_texts:
    while kept and not kept[-1].strip():
      kept.pop()
    rewritten = '\n'.join([*kept, '', DYNAMIC_SECTION_HEADER, *moved_texts])
  else:
    rewritten = '\n'.join(kept)
  return {
      'rewritten_prompt': rewritten,
      'changed': rewritten != prompt,
      'moved_lines': moved,
      'json_lines_key_sorted': sorted_json,
  }


def _moved_lines(prompt: str, moved: Sequence[Mapping[str, Any]]) -> list[str]:
  lines = prompt.split('\n')
  return [lines[int(m['line']) - 1] for m in moved if 1 <= int(m['line']) <= len(lines)]
# EVOLVE-BLOCK-END


def _common_prefix_len(a: str, b: str) -> int:
  limit = min(len(a), len(b))
  lo, hi = 0, limit
  while lo < hi:
    mid = (lo + hi + 1) // 2
    if a[:mid] == b[:mid]:
      lo = mid
    else:
      hi = mid - 1
  return lo


_STATIC_ENTERPRISE_BODY = (
    "You are VibeLift's Gemini Enterprise SRE & FinOps Copilot.\n"
    "Follow the production incident triage playbook strictly:\n"
    "1. Verify Discovery Engine engine health across global, us, and eu.\n"
    "2. Reconcile Cloud Monitoring token series with BigQuery vibelift_mart.fct_turns.\n"
    "3. Enforce read-only SELECT guardrails and 100 MiB maximumBytesBilled on NL2SQL.\n"
    "4. Preserve customer data residency and zero-PII dead-letter logging.\n"
) * 8

BENCHMARK_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "iso_timestamp_header",
        "current_time: 2026-10-06T10:00:00Z\n" + _STATIC_ENTERPRISE_BODY,
        "current_time: 2026-10-06T10:05:12Z\n" + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "uuid_request_header",
        "request_id: 123e4567-e89b-12d3-a456-426614174000\n" + _STATIC_ENTERPRISE_BODY,
        "request_id: 987e6543-e21b-43d3-b789-426614174999\n" + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "user_email_header",
        "user_email: alice.sre@example.com\n" + _STATIC_ENTERPRISE_BODY,
        "user_email: bob.finops@example.com\n" + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "single_line_json_key_order",
        '{"z_tool": "bq_query", "a_cap_mb": 100, "m_mode": "read_only"}\n' + _STATIC_ENTERPRISE_BODY,
        '{"a_cap_mb": 100, "m_mode": "read_only", "z_tool": "bq_query"}\n' + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "multiline_json_key_order",
        "{\n  \"z_policy\": \"strict\",\n  \"a_region\": \"us-central1\",\n  \"m_tier\": \"flash\"\n}\n"
        + _STATIC_ENTERPRISE_BODY,
        "{\n  \"a_region\": \"us-central1\",\n  \"m_tier\": \"flash\",\n  \"z_policy\": \"strict\"\n}\n"
        + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "fenced_json_schema_order",
        "```json\n{\n  \"z_schema\": {\"b\": 2, \"a\": 1},\n  \"a_version\": \"1.0\"\n}\n```\n"
        + _STATIC_ENTERPRISE_BODY,
        "```json\n{\n  \"a_version\": \"1.0\",\n  \"z_schema\": {\"a\": 1, \"b\": 2}\n}\n```\n"
        + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "mixed_static_and_volatile_json_line",
        '{"request_id": "11111111-2222-3333-4444-555555555555", "static_runbook": "'
        + ("R" * 600)
        + '"}\n'
        + _STATIC_ENTERPRISE_BODY,
        '{"request_id": "99999999-8888-7777-6666-555555555555", "static_runbook": "'
        + ("R" * 600)
        + '"}\n'
        + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "trailing_whitespace_variance",
        _STATIC_ENTERPRISE_BODY.replace("\n", "   \n") + "\nuser_id: u-101",
        _STATIC_ENTERPRISE_BODY + "\nuser_id: u-202",
    ),
    (
        "epoch_and_clock_preamble",
        "epoch: 1759789000\nclock: 14:22:01 UTC\n" + _STATIC_ENTERPRISE_BODY,
        "epoch: 1759789060\nclock: 14:23:01 UTC\n" + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "hex_trace_and_date_preamble",
        "date: Oct 6, 2026\ntrace_hash: a1b2c3d4e5f60718293a\n" + _STATIC_ENTERPRISE_BODY,
        "date: Oct 7, 2026\ntrace_hash: f0e1d2c3b4a596877869\n" + _STATIC_ENTERPRISE_BODY,
    ),
    (
        "idempotent_already_rewritten",
        _STATIC_ENTERPRISE_BODY + "\n\n" + DYNAMIC_SECTION_HEADER + "\nrequest_id: 11111111-2222-3333-4444-555555555555",
        _STATIC_ENTERPRISE_BODY + "\n\n" + DYNAMIC_SECTION_HEADER + "\nrequest_id: 22222222-3333-4444-5555-666666666666",
    ),
    (
        "pure_static_prompt",
        _STATIC_ENTERPRISE_BODY,
        _STATIC_ENTERPRISE_BODY,
    ),
)

EVAL_INPUTS: dict[str, Any] = {
    "benchmark_cases": len(BENCHMARK_PAIRS),
    "model": "gemini-3.5-flash",
}


def _tokens_preserved(original: str, rewritten: str) -> bool:
  """Verifies that all alphanumeric/identifier tokens from original exist in rewritten."""
  orig_tokens = re.findall(r'[A-Za-z0-9_@.\-]+', original)
  rew_tokens = set(re.findall(r'[A-Za-z0-9_@.\-]+', rewritten))
  return all(tok in rew_tokens for tok in orig_tokens)


def solve(eval_inputs: Mapping[str, Any]) -> dict[str, Any]:
  """Runs propose_cache_friendly_rewrite across all benchmark turn pairs."""
  limit = int(eval_inputs.get("benchmark_cases", len(BENCHMARK_PAIRS)))
  cases: list[dict[str, Any]] = []
  for name, prev_prompt, curr_prompt in BENCHMARK_PAIRS[:limit]:
    raw_prefix = _common_prefix_len(prev_prompt, curr_prompt)
    raw_ratio = raw_prefix / max(1, len(curr_prompt))

    prev_rw = propose_cache_friendly_rewrite(prev_prompt)["rewritten_prompt"]
    curr_res = propose_cache_friendly_rewrite(curr_prompt)
    curr_rw = curr_res["rewritten_prompt"]
    # Second pass idempotency check
    curr_rw_twice = propose_cache_friendly_rewrite(curr_rw)["rewritten_prompt"]
    idempotent = curr_rw == curr_rw_twice

    rw_prefix = _common_prefix_len(prev_rw, curr_rw)
    rw_ratio = rw_prefix / max(1, len(curr_rw))
    preserved = _tokens_preserved(prev_prompt, prev_rw) and _tokens_preserved(curr_prompt, curr_rw)

    cases.append({
        "name": name,
        "raw_cached_ratio": round(raw_ratio, 4),
        "rewritten_cached_ratio": round(rw_ratio, 4),
        "preserved": preserved,
        "idempotent": idempotent,
    })
  return {"cases": cases}


def evaluate(eval_inputs: Mapping[str, Any]) -> dict[str, float]:
  """Score the solution. Returns {'composite_cache_savings_score': score}."""
  solution = solve(eval_inputs)
  cases = solution["cases"]
  if not cases:
    return {"composite_cache_savings_score": 0.0}

  total_rw_ratio = 0.0
  total_cost_saving_ratio = 0.0
  total_integrity = 0.0
  # Gemini 3.5 Flash list pricing ratio: cached read is 0.10x of uncached input ($0.15 vs $1.50 / 1M)
  cache_discount = 0.90

  for c in cases:
    if not c["preserved"]:
      # Hard constraint violation: data loss gets 0
      return {"composite_cache_savings_score": 0.0}
    rw_ratio = float(c["rewritten_cached_ratio"])
    total_rw_ratio += rw_ratio
    total_cost_saving_ratio += (rw_ratio * cache_discount) / cache_discount
    total_integrity += 1.0 if c["idempotent"] else 0.5

  n = float(len(cases))
  avg_cached_prefix_ratio = total_rw_ratio / n
  avg_cost_reduction_ratio = total_cost_saving_ratio / n
  avg_integrity = total_integrity / n

  composite = (
      0.60 * avg_cached_prefix_ratio
      + 0.30 * avg_cost_reduction_ratio
      + 0.10 * avg_integrity
  )
  return {"composite_cache_savings_score": round(composite, 6)}
