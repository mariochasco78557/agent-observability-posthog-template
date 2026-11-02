"""Error tracking: stage failures, handled exceptions, claude failures."""

import pytest

from agent_observability import ClaudeCLIError, FakeClaude, Pipeline
from tests.conftest import props


def test_stage_exception_recorded_and_reraised(make_run, mem):
    with pytest.raises(ValueError):
        with make_run() as run:
            with run.stage("score"):
                raise ValueError("bad row")
    s = props(mem, "$ai_span")
    assert s["$ai_is_error"] is True and s["$ai_error"] == "ValueError: bad row"
    exc = props(mem, "$exception")
    assert exc["stage"] == "score" and exc["handled"] is False
    assert exc["$ai_trace_id"] == run.trace_id and exc["$ai_span_id"] == s["$ai_span_id"]
    assert exc["$exception_list"][0]["type"] == "ValueError"


def test_exception_captured_once_across_layers(make_run, mem):
    with pytest.raises(KeyError):
        with make_run() as run:
            with run.stage("a") as span:
                with span.child("b"):
                    raise KeyError("x")
    assert len(mem.of("$exception")) == 1
    assert props(mem, "$exception")["stage"] == "b"
    assert [e["properties"]["$ai_is_error"] for e in mem.of("$ai_span")] == [True, True]


def test_failed_run_trace_and_finish_event(make_run, mem):
    with pytest.raises(RuntimeError):
        with make_run() as run:
            with run.stage("fetch"):
                pass
            with run.stage("draft"):
                raise RuntimeError("smtp? never")
    t, f = props(mem, "$ai_trace"), props(mem, "pipeline_run_finished")
    assert t["$ai_is_error"] is True and "RuntimeError" in t["$ai_error"]
    assert f["ok"] is False and f["failed_stages"] == ["draft"] and f["failed_stage"] == "draft"
    assert f["exceptions"] == 1 and run.ok is False


def test_exception_outside_stage_captured_by_pipeline(make_run, mem):
    with pytest.raises(OSError):
        with make_run():
            raise OSError("disk full")
    e = props(mem, "$exception")
    assert "stage" not in e and e["handled"] is False


def test_handled_exception_does_not_fail_span(make_run, mem):
    with make_run() as run:
        with run.stage("score") as span:
            try:
                raise TimeoutError("one job timed out")
            except TimeoutError as exc:
                span.capture_exception(exc, job_id=42)
    s, e = props(mem, "$ai_span"), props(mem, "$exception")
    assert s["$ai_is_error"] is False and s["handled_errors"] == 1
    assert e["handled"] is True and e["job_id"] == 42 and e["stage"] == "score"
    assert run.ok is True


def test_mark_error_fails_run_without_exception(make_run, mem):
    with make_run() as run:
        run.mark_error("0 items fetched")
    assert props(mem, "pipeline_run_finished")["ok"] is False
    assert props(mem, "$ai_trace")["$ai_error"] == "0 items fetched"
    assert not mem.of("$exception")


def test_claude_error_raises_after_recording(mem):
    fake = FakeClaude(lambda p: "!error overloaded")
    with pytest.raises(ClaudeCLIError) as info:
        with Pipeline("p", transport=mem, claude_executor=fake) as run:
            with run.stage("score") as span:
                span.claude_p("x", model="sonnet")
    g = props(mem, "$ai_generation")
    assert g["$ai_is_error"] is True and "overloaded" in g["$ai_error"]
    assert info.value.result.is_error and info.value.result.returncode == 1
    assert props(mem, "pipeline_run_finished")["generation_errors"] == 1


def test_claude_error_check_false_returns_result(mem):
    fake = FakeClaude(lambda p: "!error nope")
    with Pipeline("p", transport=mem, claude_executor=fake) as run:
        with run.stage("s") as span:
            res = span.claude_p("x", check=False)
    assert res.is_error and run.ok


def test_error_strings_are_redacted(make_run, mem):
    with pytest.raises(RuntimeError):
        with make_run() as run:
            with run.stage("s"):
                raise RuntimeError("auth failed for key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA")
    assert "sk-ant" not in props(mem, "$ai_span")["$ai_error"]
    assert "[REDACTED:anthropic_key]" in props(mem, "$ai_trace")["$ai_error"]


def test_keyboard_interrupt_not_sent_to_error_tracking(make_run, mem):
    with pytest.raises(KeyboardInterrupt):
        with make_run() as run:
            with run.stage("s"):
                raise KeyboardInterrupt
    assert not mem.of("$exception")
    assert props(mem, "$ai_span")["$ai_is_error"] is True
