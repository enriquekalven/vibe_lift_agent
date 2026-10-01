"""Prompt Cache X-Ray: where two snapshots of a prompt stop sharing a cacheable prefix.

Gemini implicit and explicit context caching only reuse an identical *leading* prefix. One dynamic
value near the top of a prompt (a timestamp, a per-user id, JSON keys serialized in a different
order) makes everything after it uncacheable on every request. This module compares two snapshots
of the same prompt and reports:

* the first diverging character (line, column, approximate token index);
* the likely cache-buster on that line (timestamp, UUID, e-mail, JSON key order, whitespace, ...);
* a heatmap of the current prompt: cached prefix, busted region, and the static tail stranded
  behind the break (recoverable if the dynamic part moves to the end);
* the dollar cost of the stranded tokens, per request and per 1,000 requests, from the rate card;
  a monthly figure only when the caller supplies a request volume;
* a reordered prompt (volatile lines moved last, JSON keys sorted) whose cached share is
  re-measured by running the same comparison on the rewritten snapshots, not asserted.

Token counts are estimates. When the caller passes the turn's logged input token count, the
logged tokens-per-character ratio is used; otherwise the common 4 characters/token heuristic. The
basis is always returned as ``token_basis``.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from vibelift import telemetry

MAX_PROMPT_CHARS = 400_000
CHARS_PER_TOKEN_HEURISTIC = 4.0
DEFAULT_MODEL = 'gemini-2.5-flash'
# Heatmap segments longer than this are shown as head + collapsed marker + tail.
_COLLAPSE_OVER_CHARS = 2400
_COLLAPSE_KEEP_CHARS = 700
# Minimum cache token count for implicit and explicit caching, per the Vertex AI context caching
# overview (cloud.google.com/vertex-ai/generative-ai/docs/context-cache/context-cache-overview),
# checked 2026-10-01. Below it nothing is cached.
CACHE_MINIMUM_TOKENS_BY_FAMILY = (('gemini-3', 4096), ('gemini-2', 2048))
DYNAMIC_SECTION_HEADER = '### Dynamic context (moved last so the static prefix above stays cacheable)'

STATUS_IDENTICAL = 'IDENTICAL'
STATUS_APPEND_ONLY = 'APPEND_ONLY'
STATUS_SHORTER = 'SHORTER_PREFIX'
STATUS_BUSTED = 'PREFIX_BUSTED'

_MONTHS = 'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'

# (kind, label, pattern, advice). Order matters: earlier detectors win overlapping spans, so a full
# ISO timestamp is reported once as a timestamp rather than as a date plus a clock time.
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


def _common_prefix_len(a: str, b: str) -> int:
  limit = min(len(a), len(b))
  lo, hi = 0, limit
  # Binary search on slice equality: C-speed comparisons instead of a per-character Python loop.
  while lo < hi:
    mid = (lo + hi + 1) // 2
    if a[:mid] == b[:mid]:
      lo = mid
    else:
      hi = mid - 1
  return lo


def _common_suffix_len(a: str, b: str, prefix_len: int) -> int:
  limit = min(len(a), len(b)) - prefix_len
  lo, hi = 0, max(0, limit)
  while lo < hi:
    mid = (lo + hi + 1) // 2
    if a[len(a) - mid:] == b[len(b) - mid:]:
      lo = mid
    else:
      hi = mid - 1
  return lo


def _line_col(text: str, offset: int) -> tuple[int, int]:
  line = text.count('\n', 0, offset) + 1
  col = offset - (text.rfind('\n', 0, offset) + 1) + 1
  return line, col


def _line_at(text: str, offset: int) -> tuple[int, int, str]:
  start = text.rfind('\n', 0, offset) + 1
  end = text.find('\n', offset)
  end = len(text) if end < 0 else end
  return start, end, text[start:end]


def _parse_json_fragment(fragment: str) -> Any:
  frag = fragment.strip().rstrip(',')
  if not frag or frag[0] not in '{[':
    return None
  try:
    return json.loads(frag)
  except ValueError:
    return None


def _classify_break(previous: str, current: str, offset: int,
                    findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  """Explains the divergence: volatile values on the breaking line, JSON key order, or whitespace."""
  c_start, c_end, c_line = _line_at(current, offset)
  _p_start, _p_end, p_line = _line_at(previous, min(offset, len(previous)))
  line_no = _line_col(current, offset)[0]
  on_line = [dict(f) for f in findings if c_start <= int(f['start']) < c_end or (
      int(f['start']) <= offset < int(f['end']))]
  if on_line:
    return on_line
  if p_line.split() == c_line.split() and p_line != c_line:
    return [_finding('whitespace', line_no, c_start, c_end, current)]
  p_json, c_json = _parse_json_fragment(p_line), _parse_json_fragment(c_line)
  if p_json is not None and p_json == c_json:
    return [_finding('json_key_order', line_no, c_start, c_end, current)]
  whole_prev, whole_curr = _parse_json_fragment(previous), _parse_json_fragment(current)
  if whole_prev is not None and whole_prev == whole_curr:
    return [_finding('json_key_order', line_no, c_start, c_end, current)]
  return [_finding('content_edit', line_no, c_start, c_end, current)]


def _segments(text: str, regions: Sequence[tuple[int, int, str]],
              findings: Sequence[Mapping[str, Any]], scale: float,
              hot_span: tuple[int, int] | None = None) -> list[dict[str, Any]]:
  """Heatmap segments: base regions overlaid with volatile matches, long runs collapsed."""
  marks = {0, len(text)}
  for s, e, _ in regions:
    marks.update((s, e))
  spans = [(int(f['start']), int(f['end'])) for f in findings]
  for s, e in spans:
    marks.update((s, e))
  points = sorted(m for m in marks if 0 <= m <= len(text))
  dynamic = [(s, e) for s, e, state in regions if state == 'dynamic']
  if hot_span is not None:
    dynamic.append(hot_span)
  hot = [(fs, fe) for fs, fe in spans if any(fs < de and ds < fe for ds, de in dynamic)]

  def base_state(pos: int) -> str:
    for s, e, state in regions:
      if s <= pos < e:
        return state
    return 'cached'

  merged: list[list[Any]] = []
  for s, e in itertools.pairwise(points):
    if s == e:
      continue
    state = base_state(s)
    if any(fs <= s < fe for fs, fe in hot):
      state = 'buster'
    elif any(fs <= s < fe for fs, fe in spans) and state in ('cached', 'recoverable'):
      state = 'latent'
    if merged and merged[-1][2] == state and merged[-1][1] == s:
      merged[-1][1] = e
    else:
      merged.append([s, e, state])

  out: list[dict[str, Any]] = []
  for s, e, state in merged:
    tok_s, tok_e = round(s * scale), round(e * scale)
    if e - s > _COLLAPSE_OVER_CHARS:
      head_end, tail_start = s + _COLLAPSE_KEEP_CHARS, e - _COLLAPSE_KEEP_CHARS
      out.append({'state': state, 'text': text[s:head_end], 'start': s, 'end': head_end,
                  'token_start': tok_s, 'token_end': round(head_end * scale)})
      out.append({'state': state, 'collapsed': True, 'text': '', 'start': head_end, 'end': tail_start,
                  'chars': tail_start - head_end,
                  'tokens': round((tail_start - head_end) * scale)})
      out.append({'state': state, 'text': text[tail_start:e], 'start': tail_start, 'end': e,
                  'token_start': round(tail_start * scale), 'token_end': tok_e})
    else:
      out.append({'state': state, 'text': text[s:e], 'start': s, 'end': e,
                  'token_start': tok_s, 'token_end': tok_e})
  return out


def propose_cache_friendly_rewrite(prompt: str) -> dict[str, Any]:
  """Moves lines holding volatile values to the end and sorts JSON object keys line by line."""
  kept: list[str] = []
  moved: list[dict[str, Any]] = []
  sorted_json = 0
  findings = scan_volatile_spans(prompt)
  kinds_by_line: dict[int, list[str]] = {}
  for f in findings:
    kinds_by_line.setdefault(int(f['line']), []).append(str(f['kind']))
  for line_no, line in enumerate(prompt.split('\n'), start=1):
    parsed = _parse_json_fragment(line)
    if isinstance(parsed, dict):
      indent = line[:len(line) - len(line.lstrip())]
      trailing = ',' if line.rstrip().endswith(',') else ''
      normalized = indent + json.dumps(parsed, sort_keys=True, ensure_ascii=False) + trailing
      if normalized != line:
        sorted_json += 1
        line = normalized
    if line_no in kinds_by_line and line.strip() != DYNAMIC_SECTION_HEADER:
      moved.append({'line': line_no, 'text': line[:200], 'kinds': sorted(set(kinds_by_line[line_no]))})
    else:
      kept.append(line)
  if moved:
    while kept and not kept[-1].strip():
      kept.pop()
    rewritten = '\n'.join(kept + ['', DYNAMIC_SECTION_HEADER] + [m_line for m_line in _moved_lines(prompt, moved)])
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
  return [lines[int(m['line']) - 1] for m in moved]


def _rate_card(model: str) -> telemetry.ModelRateCard | None:
  return telemetry.RATE_CARDS.get(model)


def cache_minimum_tokens(model: str) -> int | None:
  """Documented minimum cacheable prompt size for a Gemini model family, or None if unknown."""
  name = model.lower()
  for family, minimum in CACHE_MINIMUM_TOKENS_BY_FAMILY:
    if name.startswith(family):
      return minimum
  return None


def _measure(previous: str, current: str) -> dict[str, int | str]:
  prefix = _common_prefix_len(previous, current)
  if previous == current:
    return {'status': STATUS_IDENTICAL, 'prefix': prefix, 'suffix': 0}
  if prefix == len(previous):
    return {'status': STATUS_APPEND_ONLY, 'prefix': prefix, 'suffix': 0}
  if prefix == len(current):
    return {'status': STATUS_SHORTER, 'prefix': prefix, 'suffix': 0}
  return {'status': STATUS_BUSTED, 'prefix': prefix, 'suffix': _common_suffix_len(previous, current, prefix)}


def analyze(
    previous_prompt: str,
    current_prompt: str,
    model: str = DEFAULT_MODEL,
    monthly_requests: int | None = None,
    current_input_tokens: int | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Compares two prompt snapshots and returns the X-Ray report (see module docstring).

  Raises:
    ValueError: if either prompt is empty or longer than MAX_PROMPT_CHARS.
  """
  for name, value in (('previous_prompt', previous_prompt), ('current_prompt', current_prompt)):
    if not isinstance(value, str) or not value:
      raise ValueError(f'{name} is empty.')
    if len(value) > MAX_PROMPT_CHARS:
      raise ValueError(f'{name} is {len(value):,} characters; the X-Ray accepts up to {MAX_PROMPT_CHARS:,}.')

  prev, curr = previous_prompt, current_prompt
  if current_input_tokens and current_input_tokens > 0:
    scale, basis = current_input_tokens / len(curr), 'scaled_from_logged_input_tokens'
  else:
    scale, basis = 1.0 / CHARS_PER_TOKEN_HEURISTIC, 'heuristic_4_chars_per_token'

  def tok(chars: int) -> int:
    return round(chars * scale)

  m = _measure(prev, curr)
  status, prefix, suffix = str(m['status']), int(m['prefix']), int(m['suffix'])
  findings = scan_volatile_spans(curr)

  if status == STATUS_BUSTED:
    dyn_end = len(curr) - suffix
    regions = [(0, prefix, 'cached'), (prefix, dyn_end, 'dynamic'), (dyn_end, len(curr), 'recoverable')]
    cached_chars, recoverable_chars = prefix, suffix
  elif status == STATUS_APPEND_ONLY:
    regions = [(0, prefix, 'cached'), (prefix, len(curr), 'appended')]
    cached_chars, recoverable_chars = prefix, 0
  else:
    regions = [(0, len(curr), 'cached')]
    cached_chars, recoverable_chars = len(curr), 0

  breakpoint_info: dict[str, Any] | None = None
  causes: list[dict[str, Any]] = []
  hot_span: tuple[int, int] | None = None
  if status == STATUS_BUSTED:
    line, col = _line_col(curr, prefix)
    causes = _classify_break(prev, curr, prefix, findings)
    line_start, line_end, cur_line = _line_at(curr, prefix)
    hot_span = (line_start, max(line_end, line_start + 1))
    _ps, _pe, prev_line = _line_at(prev, prefix)
    breakpoint_info = {
        'char_offset': prefix,
        'line': line,
        'column': col,
        'token_index': tok(prefix),
        'previous_line': prev_line[:240],
        'current_line': cur_line[:240],
    }

  card = _rate_card(model)
  delta_per_m = (card.input_per_million_usd - card.cached_read_per_million_usd) if card else None
  recoverable_tokens = tok(recoverable_chars)
  per_request = recoverable_tokens * delta_per_m / 1e6 if delta_per_m is not None else None
  monthly = per_request * monthly_requests if per_request is not None and monthly_requests else None

  rewrite_prev = propose_cache_friendly_rewrite(prev)
  rewrite_curr = propose_cache_friendly_rewrite(curr)
  after = _measure(str(rewrite_prev['rewritten_prompt']), str(rewrite_curr['rewritten_prompt']))
  rewritten_curr = str(rewrite_curr['rewritten_prompt'])
  if after['status'] == STATUS_BUSTED:
    after_cached_chars, after_recoverable = int(after['prefix']), int(after['suffix'])
  elif after['status'] == STATUS_APPEND_ONLY:
    after_cached_chars, after_recoverable = int(after['prefix']), 0
  else:
    after_cached_chars, after_recoverable = len(rewritten_curr), 0
  after_scale_len = max(1, len(rewritten_curr))
  after_recoverable_tokens = tok(after_recoverable)
  after_per_request = after_recoverable_tokens * delta_per_m / 1e6 if delta_per_m is not None else None

  dyn_lo, dyn_hi = (prefix, len(curr) - suffix) if status == STATUS_BUSTED else (len(curr), len(curr))
  hot_lo, hot_hi = hot_span or (dyn_lo, dyn_hi)
  latent = [f for f in findings
            if not (int(f['start']) < dyn_hi and dyn_lo < int(f['end']))
            and not (int(f['start']) < hot_hi and hot_lo < int(f['end']))
            and (int(f['end']) <= cached_chars or int(f['start']) >= dyn_hi)]
  min_tokens = cache_minimum_tokens(model)
  below_now = min_tokens is not None and tok(cached_chars) < min_tokens
  below_after = min_tokens is not None and tok(after_cached_chars) < min_tokens
  notes = [
      'Implicit caching is best-effort: this shows which tokens are eligible for reuse, not '
      'guaranteed cache hits.',
      'Token counts are ' + ('scaled from the turn\'s logged input token count.' if basis.startswith('scaled')
                             else 'estimated at 4 characters per token; pass a logged token count for '
                                  'a grounded figure.'),
  ]
  if card is None:
    notes.append(f'No rate card for model {model!r}; dollar figures are omitted.')
  if monthly_requests is None:
    notes.append('Monthly cost is shown only when you enter a request volume; per-1,000-request '
                 'cost needs no assumption.')
  if below_after:
    notes.append(f'Even after the fix the cacheable prefix is about {tok(after_cached_chars):,} tokens, below '
                 f'the {min_tokens:,}-token cache minimum for {model}; Gemini will not cache it, so the '
                 'stranded cost is not recoverable until the static prefix grows.')
  elif below_now:
    notes.append(f'Today\'s cached prefix (about {tok(cached_chars):,} tokens) is below the {min_tokens:,}-token '
                 f'cache minimum for {model}, so none of it is cached; the fix lifts it above the minimum.')
  if latent:
    notes.append(f'{len(latent)} volatile value(s) sit inside text that matched this time (dates, ids). '
                 'They will bust the cache when they change; the rewrite moves them last.')

  return {
      'status': status,
      'model': model,
      'rate_card': None if card is None else {
          'input_per_million_usd': card.input_per_million_usd,
          'cached_read_per_million_usd': card.cached_read_per_million_usd,
      },
      'token_basis': basis,
      'source': dict(source or {'kind': 'pasted'}),
      'previous_chars': len(prev),
      'current_chars': len(curr),
      'current_tokens': tok(len(curr)),
      'cached_prefix_chars': cached_chars,
      'cached_prefix_tokens': tok(cached_chars),
      'cached_prefix_pct': round(cached_chars / len(curr) * 100.0, 1),
      'cache_minimum_tokens': min_tokens,
      'below_cache_minimum': below_now,
      'breakpoint': breakpoint_info,
      'causes': causes,
      'recoverable_tokens_per_request': recoverable_tokens,
      'stranded_cost_usd_per_request': None if per_request is None else round(per_request, 8),
      'stranded_cost_usd_per_1k_requests': None if per_request is None else round(per_request * 1000, 4),
      'monthly_requests': monthly_requests,
      'stranded_cost_usd_per_month': None if monthly is None else round(monthly, 2),
      'heatmap': _segments(curr, regions, findings, scale, hot_span),
      'volatile_findings': findings[:50],
      'latent_busters': len(latent),
      'rewrite': {
          'changed': bool(rewrite_curr['changed']),
          'rewritten_prompt': rewritten_curr,
          'moved_lines': rewrite_curr['moved_lines'],
          'json_lines_key_sorted': rewrite_curr['json_lines_key_sorted'],
          'projected_status': after['status'],
          'projected_cached_prefix_pct': round(after_cached_chars / after_scale_len * 100.0, 1),
          'projected_cached_prefix_tokens': tok(after_cached_chars),
          'projected_below_cache_minimum': below_after,
          'projected_recoverable_tokens_per_request': after_recoverable_tokens,
          'projected_stranded_cost_usd_per_1k_requests': (
              None if after_per_request is None else round(after_per_request * 1000, 4)),
          'method': 'Re-measured by running the same comparison on both rewritten snapshots.',
      },
      'notes': notes,
  }


# ---------------------------------------------------------------------------
# Logged prompt snapshots (OTel GenAI content refs in GCS)
# ---------------------------------------------------------------------------

def _text_of(obj: Any) -> str:
  if isinstance(obj, str):
    return obj
  if isinstance(obj, Mapping):
    for key in ('content', 'text'):
      if isinstance(obj.get(key), str):
        return str(obj[key])
    parts = obj.get('parts')
    if isinstance(parts, list):
      return '\n'.join(_text_of(p) for p in parts)
    return json.dumps(obj, ensure_ascii=False)
  if isinstance(obj, list):
    return '\n'.join(_text_of(p) for p in obj)
  return json.dumps(obj, ensure_ascii=False)


def _jsonl_records(raw: str) -> list[Any]:
  out: list[Any] = []
  for line in raw.splitlines():
    if not line.strip():
      continue
    try:
      out.append(json.loads(line))
    except ValueError:
      out.append(line)
  return out


def render_logged_prompt(system_instruction_jsonl: str | None, input_messages_jsonl: str | None) -> str:
  """Renders an OTel GenAI turn (system instruction + input messages JSONL) as one prompt text.

  This is a faithful text rendering of what was logged, in order; non-text parts (tool calls and
  results) are kept as compact JSON with their original key order.
  """
  blocks: list[str] = []
  if system_instruction_jsonl:
    sys_text = '\n'.join(_text_of(r) for r in _jsonl_records(system_instruction_jsonl))
    if sys_text:
      blocks.append('[system]\n' + sys_text)
  for rec in _jsonl_records(input_messages_jsonl or ''):
    if isinstance(rec, Mapping):
      role = str(rec.get('role') or 'message')
      parts = rec.get('parts')
      body = '\n'.join(_text_of(p) for p in parts) if isinstance(parts, list) else _text_of(rec)
      blocks.append(f'[{role}]\n{body}')
    else:
      blocks.append(_text_of(rec))
  return '\n\n'.join(blocks)


_EXAMPLE_SERVICES = (
    ('payments-api', 'card authorization and refunds'), ('checkout-web', 'storefront checkout flow'),
    ('identity', 'SSO, MFA and session issuance'), ('search', 'catalog search and ranking'),
    ('inventory', 'stock levels and reservations'), ('notifications', 'e-mail, SMS and push delivery'),
    ('billing-batch', 'nightly invoicing jobs'), ('data-pipeline', 'streaming ingestion to BigQuery'),
    ('vpn-gateway', 'remote employee access'), ('email-relay', 'outbound corporate mail'),
    ('hr-portal', 'employee self-service'), ('wiki', 'internal documentation'),
    ('ci-runners', 'build and test infrastructure'), ('artifact-registry', 'container images'),
    ('feature-flags', 'runtime configuration'), ('support-chat', 'customer chat widget'),
)


def _example_static_body() -> str:
  lines = [
      '## Escalation policy',
      '1. Classify every ticket as P1, P2 or P3 using the severity matrix below before taking action.',
      '2. P1 tickets page the on-call SRE for the affected service within 5 minutes and open a bridge.',
      '3. P2 tickets get an owner within 30 minutes; attach the most relevant runbook excerpt.',
      '4. P3 tickets are answered from the knowledge base; escalate only if the requester is blocked.',
      '5. Never share credentials, tokens, internal hostnames or customer data with the requester.',
      '6. Restarts and rollbacks need explicit approval from the service owner for P2 and P3.',
      '7. Summarize every action you took in the ticket history in plain language.',
      '',
      '## Severity matrix',
      '- P1: production outage, confirmed data loss, or an active security incident.',
      '- P2: degraded service with a documented workaround, or a single-region failure.',
      '- P3: how-to questions, access requests and feature requests.',
      '',
      '## Tool catalog',
  ]
  for name, purpose in _EXAMPLE_SERVICES:
    fn = name.replace('-', '_')
    lines += [
        f'get_{fn}_status(region) -> health, open incidents and last deploy for {name} ({purpose})',
        f'get_{fn}_recent_errors(minutes) -> top error signatures and counts for {name}',
        f'restart_{fn}(region, approval_id) -> rollout id; requires owner approval below P1',
        f'rollback_{fn}(region, release, approval_id) -> rollout id; previous release of {name}',
        f'search_{fn}_runbooks(query) -> up to 5 runbook excerpts for {name}',
    ]
  lines += [
      '',
      '## Response format',
      'Reply with: severity, affected service, actions taken, next step, and who owns it.',
  ]
  return '\n'.join(lines) + '\n'


EXAMPLE_PREVIOUS = (
    'You are the IT Service Desk escalation agent for Example Corp.\n'
    'Current time: 2026-10-01T09:14:07Z\n'
    'User: alex.rivera@example.com\n'
    '{"tier": "gold", "region": "us-central1", "sla_minutes": 30}\n'
    '\n'
    + _example_static_body()
)
EXAMPLE_CURRENT = (
    EXAMPLE_PREVIOUS
    .replace('2026-10-01T09:14:07Z', '2026-10-01T09:14:52Z')
    .replace('alex.rivera@example.com', 'sam.chen@example.com')
    .replace('{"tier": "gold", "region": "us-central1", "sla_minutes": 30}',
             '{"region": "us-central1", "tier": "gold", "sla_minutes": 30}')
)
