import json
import os
import stat
import subprocess

import pytest

from agent_observability import SANDBOX_FLAGS, FakeClaude, build_claude_cmd, parse_claude_json, run_claude_p
from agent_observability.claude import Completed


def test_parse_success_fixture(fixture_text):
    r = parse_claude_json(fixture_text("claude_success.json"), requested_model="sonnet", latency_s=4.3)
    assert r.ok and r.structured_output == {"score": 82, "verdict": "apply"} and r.json()["score"] == 82
    assert (r.input_tokens, r.output_tokens) == (1520, 210)
    assert (r.cache_read_input_tokens, r.cache_creation_input_tokens) == (3100, 0)
    assert r.total_cost_usd == 0.01234 and r.duration_ms == 4210 and r.duration_api_ms == 3980
    assert r.num_turns == 1 and r.stop_reason == "end_turn" and r.latency_s == 4.3


def test_actual_model_from_model_usage(fixture_text):
    r = parse_claude_json(fixture_text("claude_success.json"), requested_model="sonnet")
    assert r.model == "claude-sonnet-synthetic" and r.requested_model == "sonnet"


def test_model_falls_back_to_requested():
    r = parse_claude_json(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "hi"}), requested_model="haiku")
    assert r.model == "haiku" and r.json is not None and r.result == "hi"


def test_parse_is_error_fixture(fixture_text):
    r = parse_claude_json(fixture_text("claude_error.json"), returncode=1)
    assert r.is_error and r.subtype == "error_max_turns" and "Reached max turns" in r.error
    assert r.input_tokens == 900 and r.total_cost_usd == 0.004  # usage is kept on failures too


def test_parse_malformed_json(fixture_text):
    r = parse_claude_json(fixture_text("claude_malformed.txt"))
    assert r.is_error and r.error.startswith("malformed JSON") and r.input_tokens is None


def test_parse_event_list(fixture_text):
    r = parse_claude_json(fixture_text("claude_events_list.json"))
    assert r.ok and r.json() == {"ok": True} and r.output_tokens == 8


@pytest.mark.parametrize("stdout,code", [("", 1), ("   ", 0), ("[]", 0), ("42", 0)])
def test_parse_degenerate_outputs_never_raise(stdout, code):
    r = parse_claude_json(stdout, returncode=code, stderr="Not logged in")
    assert r.is_error and r.error


def test_nonzero_exit_with_success_envelope_is_error():
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x"})
    assert parse_claude_json(out, returncode=2).is_error


def test_bad_usage_types_tolerated():
    out = json.dumps({"type": "result", "subtype": "success", "usage": {"input_tokens": "n/a", "output_tokens": True}, "total_cost_usd": "free"})
    r = parse_claude_json(out)
    assert r.ok and r.input_tokens is None and r.output_tokens is None and r.total_cost_usd is None


def test_build_cmd_sandboxed_never_bare():
    schema = {"type": "object"}
    cmd = build_claude_cmd("/opt/claude", model="sonnet", json_schema=schema, max_turns=3)
    assert cmd[:4] == ["/opt/claude", "-p", "--output-format", "json"]
    assert "--bare" not in cmd
    for flag, value in [("--permission-mode", "dontAsk"), ("--tools", ""), ("--allowedTools", ""), ("--setting-sources", ""), ("--model", "sonnet"), ("--max-turns", "3")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema
    assert set(SANDBOX_FLAGS) <= set(cmd)


def test_prompt_on_stdin_and_empty_temp_cwd():
    fake = FakeClaude()
    r = run_claude_p("SECRET PROMPT TEXT", executor=fake, model="sonnet")
    call = fake.calls[0]
    assert r.ok and call["prompt"] == "SECRET PROMPT TEXT"
    assert not any("SECRET" in a for a in call["cmd"])
    assert call["cwd_listing"] == [] and "agent-obs-claude-" in call["cwd"] and not os.path.exists(call["cwd"])


def test_explicit_cwd_used(tmp_path):
    fake = FakeClaude()
    run_claude_p("x", executor=fake, cwd=str(tmp_path))
    assert fake.calls[0]["cwd"] == str(tmp_path)


def test_strip_api_key_env():
    fake = FakeClaude()
    run_claude_p("x", executor=fake, env={"ANTHROPIC_API_KEY": "a", "ANTHROPIC_AUTH_TOKEN": "b", "KEEP": "1"}, strip_api_key_env=True)
    assert fake.calls[0]["env"] == {"KEEP": "1"}


def test_timeout_becomes_error_result():
    fake = FakeClaude(lambda p: subprocess.TimeoutExpired("claude", 1))
    r = run_claude_p("x", executor=fake, timeout_s=1, model="sonnet")
    assert r.is_error and "timed out" in r.error and r.model == "sonnet"


def test_binary_not_found():
    r = run_claude_p("x", binary="/nonexistent/claude-xyz")
    assert r.is_error and "not found" in r.error


def _fake_cli(tmp_path, envelope: dict) -> str:
    log = tmp_path / "argv.log"
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'cat > "{tmp_path}/stdin.txt"\n'
        f'echo "$(pwd)|$#|$*" > "{log}"\n'
        f"echo '{json.dumps(envelope)}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_real_subprocess_with_fake_cli_script(tmp_path):
    env = {"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": {"ok": True},
           "usage": {"input_tokens": 5, "output_tokens": 2}, "total_cost_usd": 0.0001}
    binary = _fake_cli(tmp_path, env)
    r = run_claude_p("hello via stdin", binary=binary, model="haiku", json_schema={"type": "object"})
    assert r.ok and r.json() == {"ok": True} and r.input_tokens == 5 and r.latency_s > 0
    assert (tmp_path / "stdin.txt").read_text() == "hello via stdin"
    cwd, _argc, argv = (tmp_path / "argv.log").read_text().strip().split("|", 2)
    assert "agent-obs-claude-" in cwd and "--bare" not in argv and "hello" not in argv


def test_fake_claude_text_and_structured():
    fake = FakeClaude(lambda p: "plain text" if "text" in p else {"a": 1})
    assert run_claude_p("give text", executor=fake).result == "plain text"
    assert run_claude_p("give json", executor=fake).structured_output == {"a": 1}


def test_custom_executor_protocol():
    class Canned:
        def run(self, cmd, *, input, cwd, timeout, env):
            return Completed(0, json.dumps({"type": "result", "subtype": "success", "result": "```json\n{\"v\": 2}\n```"}))
    assert run_claude_p("x", executor=Canned()).json() == {"v": 2}
