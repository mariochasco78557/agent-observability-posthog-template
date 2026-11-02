"""No key -> no-op. Telemetry failures never reach the pipeline. Flush is bounded."""

import logging
import sys
import time

import pytest

from agent_observability import FakeClaude, MemoryTransport, NullTransport, Pipeline, PostHogTransport


def full_run(**kw):
    with Pipeline("p", claude_executor=FakeClaude(), **kw) as run:
        with run.stage("s") as span:
            res = span.claude_p("x", model="sonnet")
            span.set(n=1)
            span.capture_exception(ValueError("handled"))
        run.counter("n", 1)
        run.incr("m")
        run.mark_error("alert")
    return run, res


def test_no_key_is_noop(monkeypatch):
    run, res = full_run()
    assert isinstance(run.sink.transport, NullTransport) and not run.enabled
    assert res.ok and res.json() == {"ok": True, "echo_chars": 1}
    assert run.telemetry_errors == 0


def test_no_key_never_imports_or_builds_sdk(monkeypatch):
    import agent_observability.transport as tr

    def explode(*a, **k):
        raise AssertionError("PostHogTransport must not be built without a key")
    monkeypatch.setattr(tr.PostHogTransport, "__init__", explode)
    full_run()


def test_blank_key_is_noop(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "   ")
    run, _ = full_run()
    assert not run.enabled


def test_kill_switch_beats_key(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setenv("AGENT_OBS_DISABLED", "1")
    run, _ = full_run()
    assert isinstance(run.sink.transport, NullTransport)


def test_env_key_builds_posthog_transport(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")
    seen = {}

    class FakePosthog:
        def __init__(self, project_api_key, **kw):
            seen.update(kw, key=project_api_key)
        def capture(self, *a, **k): pass
        def capture_exception(self, *a, **k): pass
        def flush(self, timeout_seconds=None): pass
    monkeypatch.setattr("posthog.Posthog", FakePosthog)
    run = Pipeline("p")
    assert isinstance(run.sink.transport, PostHogTransport) and run.enabled
    assert seen["key"] == "phc_test" and seen["host"] == "https://eu.i.posthog.com"
    assert seen["enable_exception_autocapture"] is False and callable(seen["before_send"])


def test_sdk_import_failure_degrades_to_noop(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setitem(sys.modules, "posthog", None)  # import posthog -> ImportError
    run, res = full_run()
    assert not run.enabled and res.ok and run.telemetry_errors == 1


def test_broken_transport_never_raises(boom):
    run, res = full_run(transport=boom)
    assert res.ok and boom.calls >= 5
    assert run.telemetry_errors == boom.calls


def test_broken_transport_logged_once(boom, caplog):
    caplog.set_level(logging.WARNING, logger="agent_observability")
    full_run(transport=boom)
    warnings = [r for r in caplog.records if r.name == "agent_observability"]
    assert len(warnings) == 1 and "network down" in warnings[0].getMessage()


def test_stage_exception_still_propagates_with_broken_transport(boom):
    with pytest.raises(ZeroDivisionError):
        with Pipeline("p", transport=boom) as run:
            with run.stage("s"):
                1 / 0


def test_broken_redactor_never_raises(mem):
    class BadRedactor:
        def redact(self, text): raise RuntimeError("regex bug")
        def redact_obj(self, obj): raise RuntimeError("regex bug")
        __call__ = redact
    with Pipeline("p", transport=mem, redactor=BadRedactor(), claude_executor=FakeClaude()) as run:
        run.set(limit=1)
        with run.stage("s", a=1) as span:
            res = span.claude_p("x", check=False)
            span.set(b=2)
    assert res.ok and run.telemetry_errors == 2  # span and trace failed to build, the run completed
    assert mem.flushes == 1  # flush still happened


def test_finish_is_idempotent(mem):
    run = Pipeline("p", transport=mem)
    run.finish()
    run.finish()
    run.close()
    assert mem.names.count("pipeline_run_finished") == 1 and mem.flushes == 1


def test_flush_called_on_exit(mem):
    with Pipeline("p", transport=mem):
        pass
    assert mem.flushes == 1


def test_flush_timeout_is_bounded():
    class Slow(MemoryTransport):
        def flush(self, timeout_s):
            from agent_observability.transport import bounded_call
            return bounded_call(lambda: time.sleep(5), timeout_s)
    started = time.monotonic()
    with Pipeline("p", transport=Slow(), flush_timeout_s=0.2) as run:
        pass
    assert time.monotonic() - started < 1.5
    assert run.sink.flush_timeouts == 1 and run.telemetry_errors == 1


def test_posthog_transport_flush_bounded_with_hung_client():
    class Hung:
        def flush(self, timeout_seconds=None):
            time.sleep(5)
    t = PostHogTransport("k", "h", client=Hung())
    started = time.monotonic()
    assert t.flush(0.2) is False
    assert time.monotonic() - started < 1.5


def test_delivery_errors_from_sdk_are_counted(mem):
    run = Pipeline("p", transport=mem)
    t = PostHogTransport("k", "h", client=object(), on_delivery_error=lambda e: run.sink.record_error(e, "delivery"))
    t._on_error(ConnectionError("503"), batch=[])
    assert run.telemetry_errors == 1
