# Prompt Cache X-Ray

An ADK agent that finds where another agent's prompt stops being cacheable,
prices the tokens stranded behind the break, and shows a fix whose effect it
measures rather than asserts.

Gemini's prompt cache (implicit or explicit) only reuses an *identical
leading prefix* of a request: system instruction, then tool declarations,
then the conversation. One value that changes near the top -- a timestamp,
a request id, a user e-mail, JSON keys serialized in a different order --
makes everything after it uncacheable on every single request. It is the
most common way agents quietly pay full price for input they send unchanged
a thousand times a day, and nothing in a bill tells you *why*.

This recipe teaches two things:

1. **Observing your own requests with callbacks.** A `before_model_callback`
   renders every outgoing `LlmRequest` to text and keeps the last two in
   session state; an `after_model_callback` records each response's
   `usage_metadata` (prompt, cached, output and thinking tokens). Both
   observe only -- they return `None` -- and re-assign state instead of
   mutating it so the change lands in the event's `state_delta` with any
   session service. See [`app/callbacks.py`](app/callbacks.py).
2. **Measuring cache eligibility instead of guessing.** The pure engine in
   [`app/prompt_xray.py`](app/prompt_xray.py) compares two prompt snapshots
   with a binary search for the common prefix, classifies the cache-buster
   on the breaking line, prices the stranded tokens from a list-price rate
   card, and proposes a rewrite (volatile lines moved last, JSON keys
   sorted) whose cached share is re-measured by running the same comparison
   on the rewritten pair.

## What the agent can do

| Tool | What it answers |
| --- | --- |
| `xray_prompt_cache` | "Here are two versions of my prompt -- where does caching break and what does it cost?" Leave both prompts empty to run a built-in example. |
| `xray_my_session` | "Was *your* previous request's prefix reusable for this one?" Compares the last two requests this agent itself sent, using the snapshots its callbacks recorded, scaled from the logged token count. |
| `get_session_token_usage` | "How many tokens has this conversation used, how many were cached, what did that save?" Straight from `usage_metadata`. |
| `calculate_cache_economics` | "Price 100k prompt tokens with 80k cached." List-price arithmetic with and without the cache. |

Every dollar figure is `tokens x list price`. A model without a rate card
is reported as **unpriced** -- it is never priced with another model's
card -- and the agent is instructed to say so rather than guess. Token
counts from pasted prompts are estimates (4 characters per token) and are
labelled as such; counts from the agent's own session come from the logged
`usage_metadata`.

## Setup

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), and either a
Google Cloud project with Vertex AI enabled (Application Default Credentials
via `gcloud auth application-default login`) or a Gemini API key.

```bash
cd contrib/python/prompt-cache-xray
cp .env.example .env     # set GOOGLE_CLOUD_PROJECT (or GOOGLE_API_KEY)
uv sync
uv run pytest            # unit tests; no credentials or model needed
```

`.env.example` documents every variable the recipe reads. The ones that
matter:

| Variable | Purpose |
| --- | --- |
| `MODEL_NAME` | Model the agent calls. Pick one that has a rate card in [`app/pricing.py`](app/pricing.py) if you want dollar figures. |
| `DEMO_CACHE_BUSTER` | `true` prepends a timestamp and a request id to the system instruction on every request -- the anti-pattern this recipe detects. Default `false`. |
| `RATE_CARDS_JSON` | Optional JSON object that adds or overrides rate cards, so stale prices can be corrected without a code change. |

## Run

```bash
uv run adk web .         # web UI; pick "app"
# or
uv run adk run app       # terminal chat
```

Things to ask:

- *"Run the built-in example."* -- `PREFIX_BUSTED` on line 2 (a timestamp),
  1.1% of the prompt still cacheable, about 2,100 stranded tokens per
  request (an estimate: 4 characters per token), priced per 1,000 requests
  from the rate card, and a rewrite that moves two lines and is 99.7%
  cacheable when re-measured.
- *"X-ray this conversation."* -- with the default configuration the verdict
  is `APPEND_ONLY`: an ADK conversation grows by appending, so the previous
  request is a prefix of the current one (about 95% of it after one turn).
  The result also carries the cached token count the model actually
  reported, and early in a session that is `0` even though the prefix is
  reusable: this agent's instruction plus four tool declarations is about
  1,450 tokens, below the 2,048-token minimum for implicit caching on the
  default model. The report says so (`below_cache_minimum`), and the agent
  explains it instead of claiming a saving.
- *"How many tokens has this session used and what did caching save?"*
- Paste two versions of your own system prompt and ask where they diverge.

### See the anti-pattern happen

```bash
DEMO_CACHE_BUSTER=true uv run adk run app
```

Send any two messages, then ask *"x-ray this conversation"*. The verdict
flips to `PREFIX_BUSTED` on line 2 with cause `timestamp`, the whole
instruction and every tool declaration behind it is reported as stranded,
and the rewrite moves the two volatile lines to the end. Turn the flag off
and ask again to see the prefix become reusable.

### Deploy

The recipe ships the standard serving layer (`Dockerfile`,
`app/fast_api_app.py`, A2A routes, Agent Engine adapter). Build and run the
container locally:

```bash
docker build -t prompt-cache-xray .
docker run -p 8080:8080 --env-file .env prompt-cache-xray
```

Or deploy with `agents-cli deploy` (the `agents-cli-manifest.yaml` targets
Cloud Run). Session state is in-memory unless `SESSION_SERVICE_URI` or
`GOOGLE_CLOUD_AGENT_ENGINE_ID` is set.

## Evaluate

The evalset checks tool selection deterministically and uses an LLM judge
for the honesty rules (verdict first, numbers only from tools, unpriced
means unavailable, estimates labelled):

```bash
uv run adk eval app tests/eval/evalsets/basic.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

The integration test makes one live model call and asserts that both
callbacks fired around it: `uv run pytest tests/integration`.

## How it works

```text
user turn ──► before_model_callback: render LlmRequest ─► state[snapshots] (last 2)
          ──► Gemini
          ◄── after_model_callback: usage_metadata ──────► state[usage_log]
                                                            ▲
xray_my_session ──► prompt_xray.analyze(snapshots[-2], snapshots[-1],
                                        current_input_tokens=logged) ◄─┘
```

- `prompt_xray.analyze` returns `IDENTICAL`, `APPEND_ONLY`, `SHORTER_PREFIX`
  or `PREFIX_BUSTED`, the breaking line/column, the detected cause (eight
  volatile-value detectors plus JSON key order, whitespace and content
  edit), a heatmap, the stranded cost, and the re-measured rewrite.
- Tool results are trimmed before they return to the model (no heatmap,
  capped findings, capped rewrite text) so the X-Ray does not bloat the very
  prompt it is analysing.
- Implicit caching is best-effort and has a documented minimum prefix size;
  the report says when a prefix is below it. For guaranteed reuse, pair the
  fix with explicit caching via ADK's `ContextCacheConfig` on the `App`.

## Limitations

- The rendered request is a text proxy for the token stream, good enough to
  locate the first differing byte; the model's tokenizer may place the
  boundary a few tokens away.
- Rate cards are list prices copied from the Vertex AI pricing page when the
  recipe was written. Check them before relying on a figure, or override
  them with `RATE_CARDS_JSON`.
- Snapshots are kept per session (last two requests, up to 400k characters
  each). Nothing is written outside session state.

## Where this came from

Extracted from [FinOps Dashboard](https://github.com/cloud-gtm/finops-dashboard-mcp-app),
an agent-fleet observability and FinOps dashboard for Gemini Enterprise,
where the same engine runs against prompt snapshots pulled from Cloud
Logging. This recipe keeps the part that teaches: the callbacks and the
engine, with no cloud dependencies beyond the model call.
