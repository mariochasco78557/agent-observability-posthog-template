"""Real headless Claude Code pipeline: classify, then summarise synthetic support notes.

Makes REAL `claude -p` calls (3-4 small ones) with your CLI login. Requires Claude Code
on PATH (or CLAUDE_BIN=/abs/path/claude). Telemetry is a no-op without POSTHOG_API_KEY.

    uv run python examples/claude_p_pipeline.py --model haiku
"""

from __future__ import annotations

import argparse
import sys

from agent_observability import ClaudeCLIError, Pipeline

NOTES = [
    "Customer says the nightly export has been empty since Tuesday.",
    "Feature request: dark mode for the dashboard.",
    "Invoice PDF shows the wrong VAT rate for one line item.",
]
LABEL_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["bug", "feature", "billing"]}},
    "required": ["label"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--capture-content", action="store_true")
    args = ap.parse_args()

    # strip_api_key_env=True: use the CLI's subscription login, never metered API billing by accident.
    with Pipeline("claude-p-example", distinct_id="example-host", capture_content=args.capture_content,
                  strip_api_key_env=True) as run:
        labels = {}
        with run.stage("classify") as span:
            for i, note in enumerate(NOTES):
                try:
                    res = span.claude_p(f"Classify this support note:\n\n{note}", model=args.model,
                                        json_schema=LABEL_SCHEMA, max_turns=2, timeout_s=120, name="classify_note")
                    labels[i] = res.json()["label"]
                except (ClaudeCLIError, KeyError, ValueError) as exc:
                    span.capture_exception(exc, note_index=i)
                    run.incr("errors")
            run.counter("classified", len(labels))
        with run.stage("summarise") as span:
            bugs = [NOTES[i] for i, label in labels.items() if label == "bug"]
            if bugs:
                res = span.claude_p("Summarise these bug reports in one sentence:\n" + "\n".join(bugs),
                                    model=args.model, max_turns=1, timeout_s=120, name="summarise_bugs")
                print(res.result)
            run.counter("summarised", len(bugs))
    print(f"trace {run.trace_id} ok={run.ok} counters={run.counters} telemetry={'on' if run.enabled else 'off'}", file=sys.stderr)
    return 0 if run.ok else 1


if __name__ == "__main__":
    sys.exit(main())
