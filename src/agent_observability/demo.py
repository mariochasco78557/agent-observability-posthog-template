"""``agent-obs-demo``: a fake fetch -> score -> draft pipeline using FakeClaude (no CLI, no tokens).

    POSTHOG_API_KEY=phc_... POSTHOG_HOST=https://eu.i.posthog.com agent-obs-demo
    agent-obs-demo --print          # no key needed: print the events as JSON lines instead
    agent-obs-demo --print --fail   # also inject a handled model error and a crashing stage
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

from . import ClaudeCLIError, FakeClaude, MemoryTransport, Pipeline

SCORE_SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}


def synthetic_items(n: int) -> list[dict]:
    topics = ["LLM evals", "billing webhook", "retry queue", "PDF parser", "Slack bot", "cron monitor", "RAG index", "SQL report"]
    return [{"id": i + 1, "title": f"Synthetic task #{i + 1}: {topics[i % len(topics)]}"} for i in range(n)]


def make_responder(fail: bool):
    def respond(prompt: str):
        if fail and "#2:" in prompt:
            return "!error synthetic overload, please retry"
        if prompt.startswith("Score"):
            digest = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
            return {"score": 40 + digest % 60}
        return "Draft: a short, synthetic reply written by FakeClaude."
    return respond


def run_demo(*, items: int = 5, fail: bool = False, transport=None, capture_content: bool = False, latency_s: float = 0.05) -> Pipeline:
    fake = FakeClaude(make_responder(fail), latency_s=latency_s)
    run = Pipeline("demo-pipeline", distinct_id="demo-host", transport=transport, claude_executor=fake,
                   capture_content=capture_content, properties={"env": "demo"})
    with run:
        run.set(items_requested=items, fail_injected=fail)
        with run.stage("fetch") as span:
            batch = synthetic_items(items)
            span.set(fetched=len(batch))
            run.counter("fetched", len(batch))

        scored = []
        with run.stage("score") as span:
            for item in batch:
                try:
                    res = span.claude_p(f"Score this task from 0 to 100: {item['title']}", model="sonnet",
                                        json_schema=SCORE_SCHEMA, name="score_item")
                except ClaudeCLIError as exc:  # handled: recorded, pipeline continues
                    span.capture_exception(exc, item_id=item["id"])
                    run.incr("errors")
                    continue
                scored.append((item, res.json()["score"]))
            span.set(scored=len(scored))
            run.counter("scored", len(scored))

        with run.stage("draft") as span:
            for item, score in scored:
                if score >= 70:
                    span.claude_p(f"Draft a two-line reply for: {item['title']}", model="sonnet", name="draft_reply")
                    run.incr("drafted")
            run.counter("drafted", run.counters.get("drafted", 0))

        if fail:
            with run.stage("publish"):
                raise RuntimeError("synthetic publish failure (injected by --fail)")
    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-obs-demo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print events as JSON lines, send nothing")
    ap.add_argument("--fail", action="store_true", help="inject a handled model error and a crashing stage")
    ap.add_argument("--items", type=int, default=5)
    ap.add_argument("--capture-content", action="store_true", help="also send (redacted) prompts/outputs")
    args = ap.parse_args(argv)

    transport = MemoryTransport(echo=print) if args.print else None
    if not args.print and not os.environ.get("POSTHOG_API_KEY"):
        print("POSTHOG_API_KEY is not set: telemetry is a no-op. Use --print to see the events.", file=sys.stderr)
    try:
        run = run_demo(items=args.items, fail=args.fail, transport=transport, capture_content=args.capture_content)
    except RuntimeError as exc:
        print(f"run failed as requested: {exc}", file=sys.stderr)
        return 1
    where = "printed above" if args.print else ("sent to PostHog" if run.enabled else "not sent (no key)")
    print(f"trace {run.trace_id}: ok={run.ok} counters={run.counters} events {where}; "
          f"telemetry_errors={run.telemetry_errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
