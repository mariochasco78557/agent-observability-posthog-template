"""Exact shape of every event the library emits (see docs/EVENTS.md)."""

from agent_observability import FINISH_EVENT, __version__
from tests.conftest import props

AI_COMMON = {"$ai_trace_id", "$ai_session_id", "pipeline", "agent_obs_version", "$process_person_profile"}


def simple_run(make_run, **kw):
    with make_run(**kw) as run:
        with run.stage("score", batch=1) as span:
            span.claude_p("score this", model="sonnet", json_schema={"type": "object"}, name="score_item")
            span.set(scored=1)
        run.counter("scored", 1)
    return run


def test_event_order_and_names(make_run, mem):
    simple_run(make_run)
    assert mem.names == ["$ai_generation", "$ai_span", "$ai_trace", FINISH_EVENT]


def test_all_events_share_trace_id_and_distinct_id(make_run, mem):
    run = simple_run(make_run)
    assert {e["properties"]["$ai_trace_id"] for e in mem.events} == {run.trace_id}
    assert {e["distinct_id"] for e in mem.events} == {"host-1"}


def test_trace_event_schema(make_run, mem):
    run = simple_run(make_run)
    p = props(mem, "$ai_trace")
    assert AI_COMMON <= set(p)
    assert p["$ai_span_name"] == "test-pipe"
    assert p["$ai_is_error"] is False and "$ai_error" not in p
    assert isinstance(p["$ai_latency"], float) and p["$ai_latency"] >= 0
    assert p["$ai_output_state"] == {"counters": {"scored": 1}, "failed_stages": []}
    assert p["$ai_session_id"] is None
    assert p["agent_obs_version"] == __version__ and run.ok


def test_span_event_schema(make_run, mem):
    run = simple_run(make_run)
    p = props(mem, "$ai_span")
    assert AI_COMMON <= set(p)
    assert p["$ai_parent_id"] == run.trace_id
    assert p["$ai_span_name"] == p["stage"] == "score"
    assert p["$ai_input_state"] == {"batch": 1} and p["$ai_output_state"] == {"scored": 1}
    assert p["$ai_is_error"] is False and p["handled_errors"] == 0
    assert len(p["$ai_span_id"]) == 36


def test_generation_event_schema(make_run, mem):
    simple_run(make_run)
    g, s = props(mem, "$ai_generation"), props(mem, "$ai_span")
    assert AI_COMMON <= set(g)
    assert g["$ai_parent_id"] == s["$ai_span_id"]
    assert g["$ai_span_name"] == "score_item" and g["stage"] == "score"
    assert g["$ai_provider"] == "anthropic" and g["$ai_model"] == "claude-sonnet-fake"
    assert g["requested_model"] == "sonnet"
    assert g["$ai_input_tokens"] > 0 and g["$ai_output_tokens"] > 0
    assert g["$ai_total_cost_usd"] > 0 and g["$ai_cost_passthrough"] is True
    assert g["cost_source"] == "claude_cli_estimate"
    assert g["$ai_is_error"] is False and "$ai_error" not in g
    assert g["$ai_stop_reason"] == "end_turn"


def test_generation_has_no_content_by_default(make_run, mem):
    simple_run(make_run)
    g = props(mem, "$ai_generation")
    assert "$ai_input" not in g and "$ai_output_choices" not in g
    assert len(g["input_sha256"]) == 64 and g["input_chars"] == len("score this")
    assert len(g["output_sha256"]) == 64 and g["output_chars"] > 0


def test_capture_content_sends_redacted_messages(make_run, mem):
    with make_run(capture_content=True) as run:
        with run.stage("s") as span:
            span.claude_p("mail me at someone@example.com", model="sonnet")
    g = props(mem, "$ai_generation")
    assert g["$ai_input"] == [{"role": "user", "content": "mail me at [REDACTED:email]"}]
    assert g["$ai_output_choices"][0]["role"] == "assistant"


def test_content_truncated(make_run, mem):
    with make_run(capture_content=True, max_content_chars=10) as run:
        run.generation(model="m", provider="p", input="x" * 50)
    assert props(mem, "$ai_generation")["$ai_input"][0]["content"].startswith("x" * 10 + "...[truncated 40")


def test_finish_event_carries_counters_and_totals(make_run, mem):
    with make_run() as run:
        with run.stage("a") as span:
            span.claude_p("one", model="sonnet")
            span.claude_p("two", model="sonnet")
        run.counter("fetched", 7)
        run.incr("drafted")
        run.incr("drafted", 2)
    f = props(mem, FINISH_EVENT)
    assert f["counter_fetched"] == 7 and f["counter_drafted"] == 3
    assert f["ok"] is True and f["stages"] == ["a"] and f["failed_stages"] == [] and f["failed_stage"] is None
    gens = mem.of("$ai_generation")
    assert f["generations"] == 2
    assert f["input_tokens_total"] == sum(g["properties"]["$ai_input_tokens"] for g in gens)
    assert f["cost_usd_total"] > 0 and f["telemetry_errors"] == 0
    assert "$ai_session_id" not in f  # custom event, not an AI event


def test_nested_child_span_parent(make_run, mem):
    with make_run() as run:
        with run.stage("draft") as span:
            with span.child("research", job=3) as child:
                child.claude_p("x", model="sonnet")
    child_span, parent_span = mem.of("$ai_span")
    assert child_span["properties"]["$ai_parent_id"] == parent_span["properties"]["$ai_span_id"]
    assert props(mem, "$ai_generation")["$ai_parent_id"] == child_span["properties"]["$ai_span_id"]


def test_trace_level_generation_parent_is_trace(make_run, mem):
    with make_run() as run:
        run.generation(model="gpt-x", provider="openai", input_tokens=3, output_tokens=4, latency_s=0.5)
    g = props(mem, "$ai_generation")
    assert g["$ai_parent_id"] == run.trace_id and "stage" not in g
    assert g["$ai_provider"] == "openai" and g["$ai_latency"] == 0.5


def test_generic_generation_with_message_list(make_run, mem):
    msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    with make_run(capture_content=True) as run:
        with run.stage("s") as span:
            span.generation(model="m", provider="p", input=msgs, output="hello", cost_usd=0.1, cost_passthrough=False)
    g = props(mem, "$ai_generation")
    assert g["$ai_input"] == msgs and "$ai_cost_passthrough" not in g and g["$ai_total_cost_usd"] == 0.1


def test_static_properties_on_every_event(make_run, mem):
    simple_run(make_run, properties={"env": "ci"})
    assert all(e["properties"]["env"] == "ci" for e in mem.events)


def test_person_profiles_flag(make_run, mem):
    simple_run(make_run, person_profiles=True)
    assert all("$process_person_profile" not in e["properties"] for e in mem.events)


def test_distinct_id_defaults_to_name(mem, fake):
    from agent_observability import Pipeline
    with Pipeline("nightly", transport=mem):
        pass
    assert {e["distinct_id"] for e in mem.events} == {"nightly"}


def test_latency_measured(mem):
    from agent_observability import FakeClaude, Pipeline
    with Pipeline("p", transport=mem, claude_executor=FakeClaude(latency_s=0.05)) as run:
        with run.stage("s") as span:
            span.claude_p("x")
    assert props(mem, "$ai_generation")["$ai_latency"] >= 0.05
    assert props(mem, "$ai_span")["$ai_latency"] >= 0.05


def test_run_set_becomes_trace_input_state(make_run, mem):
    with make_run() as run:
        run.set(limit=10, dry_run=True)
    assert props(mem, "$ai_trace")["$ai_input_state"] == {"limit": 10, "dry_run": True}


def test_events_are_json_serialisable(make_run, mem):
    with make_run() as run:
        with run.stage("s", odd=object()) as span:
            span.set(path=__import__("pathlib").Path("/x"))
    assert mem.of("$ai_span")  # MemoryTransport round-trips through json.dumps
