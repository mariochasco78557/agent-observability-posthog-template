# Event schema (v0.1)

Everything this library sends, in one table. Property names follow PostHog's
[manual capture spec](https://posthog.com/docs/ai-observability/installation/manual-capture)
for AI observability (formerly "LLM analytics"); custom properties have no `$` prefix.
Source of truth: `src/agent_observability/pipeline.py`, enforced by `tests/test_events_schema.py`.

Every event also carries: `pipeline` (run name), `agent_obs_version`, any static `properties=` you passed,
and `$process_person_profile: false` (server-side jobs do not need person profiles; pass `person_profiles=True` to drop it).
`distinct_id` is the `distinct_id=` you pass (default: the pipeline name).

| Event | Property | Type | When | Meaning |
|---|---|---|---|---|
| `$ai_generation` | `$ai_trace_id` | uuid | always | run id, shared by every event of the run |
| | `$ai_session_id` | null | always | sent as `null`: batch runs are single-trace by design |
| | `$ai_span_id` | uuid | always | id of this generation |
| | `$ai_parent_id` | uuid | always | enclosing stage span id (or the trace id for `run.generation`) |
| | `$ai_span_name` | str | always | `name=` you passed, else the stage name |
| | `$ai_model` | str | always | resolved model id (`modelUsage` key with most output tokens), else the requested alias |
| | `$ai_provider` | str | always | `anthropic` for `claude_p`, whatever you pass for `generation()` |
| | `$ai_input_tokens` / `$ai_output_tokens` | int | if known | from `usage.input_tokens` / `usage.output_tokens` |
| | `$ai_cache_read_input_tokens` / `$ai_cache_creation_input_tokens` | int | if known | from `usage` |
| | `$ai_latency` | float s | if known | wall clock around the CLI call (falls back to `duration_ms`) |
| | `$ai_stop_reason` | str | if known | from the CLI envelope |
| | `$ai_total_cost_usd` | float | if known | `total_cost_usd` from the CLI: a client-side estimate, not a bill |
| | `$ai_cost_passthrough` | bool | with cost | `true`: PostHog keeps our total instead of recomputing |
| | `$ai_is_error` | bool | always | CLI `is_error`, non-success subtype, non-zero exit, timeout or unparseable output |
| | `$ai_error` | str | on error | redacted, max 1000 chars |
| | `input_sha256`, `input_chars` | str, int | if input given | fingerprint of the prompt (content stays local) |
| | `output_sha256`, `output_chars` | str, int | if output | fingerprint of the result |
| | `$ai_input` | list | `capture_content=True` | `[{"role":"user","content":...}]`, redacted, truncated to `max_content_chars` |
| | `$ai_output_choices` | list | `capture_content=True` | `[{"role":"assistant","content":...}]`, redacted, truncated |
| | `stage` | str | inside a stage | stage name |
| | `requested_model`, `claude_subtype`, `claude_duration_ms`, `claude_duration_api_ms`, `claude_num_turns`, `claude_exit_code`, `cost_source` | mixed | `claude_p` / `record_claude_result` | raw CLI facts; `cost_source = claude_cli_estimate` |
| `$ai_span` | `$ai_trace_id`, `$ai_session_id` | | always | as above |
| | `$ai_span_id` | uuid | always | stage id |
| | `$ai_parent_id` | uuid | always | trace id, or the parent span for `span.child()` |
| | `$ai_span_name`, `stage` | str | always | stage name |
| | `$ai_latency` | float s | always | stage duration |
| | `$ai_is_error` | bool | always | `true` if an exception left the stage |
| | `$ai_error` | str | on error | `Type: message`, redacted |
| | `$ai_input_state` | obj | if `stage(name, **attrs)` | your attrs, redacted (keep them content-free) |
| | `$ai_output_state` | obj | if `span.set(...)` | your attrs, redacted |
| | `handled_errors` | int | always | `span.capture_exception` calls in this stage |
| `$ai_trace` | `$ai_trace_id`, `$ai_session_id` | | always | as above |
| | `$ai_span_name` | str | always | pipeline name |
| | `$ai_latency` | float s | always | run duration |
| | `$ai_is_error` | bool | always | exception, failed stage, or `run.mark_error()` |
| | `$ai_error` | str | on error | redacted |
| | `$ai_input_state` | obj | if `run.set(...)` | run parameters |
| | `$ai_output_state` | obj | always | `{"counters": {...}, "failed_stages": [...]}` |
| `pipeline_run_finished` | `$ai_trace_id` | uuid | always | join key to the AI events |
| | `ok` | bool | always | same as `not $ai_trace.$ai_is_error` |
| | `duration_s` | float | always | run duration |
| | `stages`, `failed_stages` | list | always | stage names in completion order / those that raised |
| | `failed_stage` | str or null | always | first failed stage |
| | `error_message` | str or null | always | redacted |
| | `generations`, `generation_errors` | int | always | counts for this run |
| | `input_tokens_total`, `output_tokens_total` | int | always | sums over generations |
| | `cost_usd_total` | float | always | sum of reported costs (estimates) |
| | `exceptions` | int | always | `$exception` events sent this run |
| | `telemetry_errors` | int | always | failed telemetry calls so far (canary for the telemetry itself) |
| | `counter_<name>` | number | per counter | every `run.counter()` / `run.incr()` |
| `$exception` | (SDK) `$exception_list`, ... | | on failure | built by `posthog.capture_exception`; messages redacted in `before_send` |
| | `$ai_trace_id` | uuid | always | links the error to its run |
| | `stage` | str | if in a stage | where it happened |
| | `$ai_span_id` | uuid | if in a stage | which span |
| | `handled` | bool | always | `false` = it escaped a stage/run; `true` = `capture_exception` on a caught error |
| | your kwargs | | handled only | e.g. `item_id=42` |

Ordering: generations are sent as they happen, a span when its stage exits, then `$ai_trace`,
then `pipeline_run_finished`, then a bounded flush (`flush_timeout_s`, default 5 s).
