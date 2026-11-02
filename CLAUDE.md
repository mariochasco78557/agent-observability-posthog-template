# CLAUDE.md: how an agent should work in this repo

- What this is: a small library that turns pipeline runs into PostHog events. Read `llms.txt`, then `docs/EVENTS.md`.
- Tests are offline and fast: `uv run pytest -q`. Never add a test that needs network, a PostHog key or the real `claude` CLI; use `MemoryTransport` and `FakeClaude`.
- The event schema is a contract. Any change to event names or properties must update, in the same commit: `src/agent_observability/pipeline.py`, `docs/EVENTS.md`, `tests/test_events_schema.py`, and any affected SQL in `docs/AGENT_QUERIES.md`.
- Invariants (each has tests; do not weaken them):
  1. No `POSTHOG_API_KEY` -> no-op, the `posthog` SDK is not even constructed.
  2. Telemetry never raises into the caller; failures go through `SafeSink.record_error` (counted, logged once).
  3. Caller exceptions are never swallowed: stages and pipelines re-raise.
  4. Prompt/output content is only sent with `capture_content=True`, always after redaction. Error strings are always redacted.
  5. Flush is bounded by `flush_timeout_s`.
  6. `claude -p` runs with the sandbox flags in `SANDBOX_FLAGS`, prompt on stdin, never `--bare`.
- Property names for `$ai_*` events must match PostHog's manual-capture docs (https://posthog.com/docs/ai-observability/installation/manual-capture). Check the docs before adding one; custom properties get no `$` prefix.
- Never commit keys, `.env`, real prompts or real customer data. Fixtures are synthetic.
- Keep the dependency list at one runtime dependency (`posthog`).
