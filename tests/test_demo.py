import json

from agent_observability import MemoryTransport
from agent_observability.demo import main, run_demo


def test_demo_emits_full_trace():
    mem = MemoryTransport()
    run = run_demo(items=4, transport=mem, latency_s=0)
    assert run.ok and run.counters["fetched"] == 4 and run.counters["scored"] == 4
    assert {"$ai_trace", "$ai_span", "$ai_generation", "pipeline_run_finished"} <= set(mem.names)
    assert [e["properties"]["stage"] for e in mem.of("$ai_span")] == ["fetch", "score", "draft"]


def test_demo_fail_mode_exercises_error_tracking():
    mem = MemoryTransport()
    try:
        run_demo(items=3, fail=True, transport=mem, latency_s=0)
    except RuntimeError:
        pass
    excs = mem.of("$exception")
    assert [e["properties"]["stage"] for e in excs] == ["score", "publish"]
    assert [e["properties"]["handled"] for e in excs] == [True, False]
    fin = mem.of("pipeline_run_finished")[0]["properties"]
    assert fin["ok"] is False and fin["failed_stage"] == "publish" and fin["counter_errors"] == 1


def test_cli_print_mode_outputs_json_lines(capsys):
    assert main(["--print", "--items", "2"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert lines[-1]["event"] == "pipeline_run_finished"


def test_cli_without_key_is_noop(capsys):
    assert main(["--items", "1"]) == 0
    assert "no-op" in capsys.readouterr().err
