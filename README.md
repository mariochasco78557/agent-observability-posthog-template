# agent-observability-posthog-template

Trace headless Claude Code pipelines (`claude -p --output-format json`) and other scheduled
agent jobs into PostHog [AI observability](https://posthog.com/docs/ai-observability) (formerly LLM analytics)
and [error tracking](https://posthog.com/docs/error-tracking), so the agent that maintains a pipeline
can query the pipeline's own runs through the [PostHog MCP server](https://posthog.com/docs/model-context-protocol).

**Why:** agents are users too. A batch job that scores and drafts on a server at 05:00 has no UI and nobody
watching. If every run lands in PostHog as a trace with spans, generations, exceptions and counters, then
"which stage fails most?", "what does one drafted output cost?" or "did it run today?" become questions
your coding agent can answer itself, instead of you grepping journald.

**Status:** v0.1, built in a day. Wired into one private batch pipeline (daily systemd timer, several
`claude -p` stages) behind an opt-in flag. The offline tests exercise the real `posthog` SDK with
`send=False`; the SQL in [docs/AGENT_QUERIES.md](docs/AGENT_QUERIES.md) has not yet been run against a live project.

## 60-second quick start

```bash
git clone https://github.com/mariochasco78557/agent-observability-posthog-template
cd agent-observability-posthog-template
uv sync

uv run agent-obs-demo --print                 # no key: print the exact events as JSON lines

export POSTHOG_API_KEY=phc_...                # PostHog > Project settings > Project API key
export POSTHOG_HOST=https://eu.i.posthog.com  # or https://us.i.posthog.com
uv run agent-obs-demo --fail                  # fake pipeline, no Claude CLI needed
```

Then open **AI observability > Traces** (one trace, 4 spans, ~8 generations) and **Error tracking**
(one handled model error in `score`, one crash in `publish`). The demo uses `FakeClaude`: synthetic
tokens and made-up costs, no model calls.

## Use it

```python
from agent_observability import ClaudeCLIError, Pipeline

with Pipeline("nightly-triage", distinct_id="vps-01") as run:          # -> $ai_trace + pipeline_run_finished
    with run.stage("fetch") as span:                                   # -> $ai_span
        items = fetch_items()
        run.counter("fetched", len(items))
    with run.stage("score") as span:
        for item in items:
            try:
                res = span.claude_p(prompt_for(item), model="sonnet",  # -> $ai_generation
                                    json_schema=SCHEMA, name="score_item")
            except ClaudeCLIError as exc:                              # generation already recorded
                span.capture_exception(exc, item_id=item.id)           # -> $exception (handled)
                continue
            save(item, res.json())
            run.incr("scored")
# an exception escaping a stage -> $exception + span/trace marked as error, then re-raised
```

- `span.claude_p(...)` runs `claude -p --output-format json` with `--permission-mode dontAsk --tools "" --allowedTools ""
  --setting-sources "" --strict-mcp-config --no-session-persistence` (never `--bare`), prompt on **stdin**,
  in a fresh empty temp dir unless you pass `cwd=`. Binary: `claude_binary=` or `CLAUDE_BIN` (use an absolute path under systemd).
  `strip_api_key_env=True` drops `ANTHROPIC_API_KEY` so the CLI uses its subscription login.
- Already have your own runner? Parse its stdout with `parse_claude_json(stdout)` and call
  `span.record_claude_result(result, prompt=prompt)`.
- Other providers: `span.generation(model=..., provider="openai", input_tokens=..., output_tokens=..., latency_s=...)`.
- Nested work: `with span.child("research") as sub:` (shows as a tree in the trace view).

## Architecture

```
 scheduled job (systemd timer / cron / CI)
 +--------------------------------------------------+
 | with Pipeline("nightly") as run:   --------------+--> $ai_trace, pipeline_run_finished
 |   with run.stage("score") as span: --------------+--> $ai_span   (+ $exception on failure)
 |     span.claude_p(prompt)          --------------+--> $ai_generation (tokens, cost, latency)
 |        | stdin                                   |
 |        v                                         |
 |   claude -p --output-format json  (no tools,     |
 |   no MCP, no settings, empty cwd)                |
 +--------------------------------------------------+
          |  SafeSink: never raises, counts errors, logs once, bounded flush
          |  Redactor: secrets/PII scrubbed; content only with capture_content=True
          v
   posthog SDK  --->  PostHog: AI observability | Error tracking | SQL
                                      ^
                                      |  "which stage fails most?"
                      PostHog MCP / PostHog AI  <---  the agent that maintains the job
```

## Events

One table with every event and property: [docs/EVENTS.md](docs/EVENTS.md).
In short: `$ai_trace` per run, `$ai_span` per stage, `$ai_generation` per model call (model, tokens, cache tokens,
latency, CLI cost estimate, `$ai_is_error`/`$ai_error`), `$exception` with `$ai_trace_id` and `stage`, and
`pipeline_run_finished` with `ok`, `failed_stage`, totals and every counter as `counter_<name>`.

Questions and SQL for agents: [docs/AGENT_QUERIES.md](docs/AGENT_QUERIES.md).

## Privacy and safety

- **Content stays local by default.** Prompts and outputs are sent only as `sha256` + length. `capture_content=True`
  adds `$ai_input` / `$ai_output_choices`, always passed through the redactor and truncated (`max_content_chars`).
- **Redaction** (regex, extendable with `Redactor(extra=[...])` or `.add(name, regex)`): private keys, Anthropic/OpenAI/
  PostHog/GitHub/AWS/Slack/Telegram tokens, JWTs, bearer tokens, `password=`-style assignments, emails, international
  phone numbers, IBANs. Error strings and exception messages (via the SDK's `before_send`) are always redacted.
  Regexes are a safety net, not a guarantee: keep content capture off for anything you would not paste in a public issue.
- **No key, no cost:** without `POSTHOG_API_KEY` every call is a no-op and the SDK client is never created.
  `AGENT_OBS_DISABLED=1` is a kill switch.
- **Telemetry never breaks the job:** every send is wrapped; failures are counted (`run.telemetry_errors`,
  also sent as `telemetry_errors`) and logged once. Your own exceptions are always re-raised.
- **Bounded exit:** flush waits at most `flush_timeout_s` (default 5 s). Exception autocapture is off: no global hooks.
- **Costs are estimates:** `total_cost_usd` from Claude Code is a client-side estimate (an API-equivalent figure on
  subscription logins), sent with `$ai_cost_passthrough` and `cost_source = claude_cli_estimate`.
- Events set `$process_person_profile: false` (server jobs are not people).

## Development

```bash
uv run pytest -q                               # offline: MemoryTransport + FakeClaude, no network
uv run python examples/claude_p_pipeline.py    # REAL claude -p calls (needs Claude Code on PATH)
```

Requires Python 3.11+ and `posthog>=7,<8`. Agent instructions: [CLAUDE.md](CLAUDE.md). Machine-readable index: [llms.txt](llms.txt).

## License

MIT
