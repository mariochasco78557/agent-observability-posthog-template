"""Headless Claude Code (``claude -p --output-format json``): command builder, runner, parser, fake.

The parser never raises: whatever the CLI prints (success, ``is_error``, a JSON event
list, garbage, nothing) becomes a :class:`ClaudeResult` so telemetry can always record it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

# Sandbox flags: no tools, no MCP servers, no user/project settings, nothing persisted.
SANDBOX_FLAGS: tuple[str, ...] = (
    "--permission-mode", "dontAsk",
    "--tools", "",
    "--allowedTools", "",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--no-session-persistence",
)


class ClaudeCLIError(RuntimeError):
    """Raised by ``claude_p(check=True)`` when the call failed. ``.result`` has the details."""

    def __init__(self, message: str, result: "ClaudeResult | None" = None):
        super().__init__(message)
        self.result = result


@dataclass
class ClaudeResult:
    is_error: bool
    result: str = ""
    structured_output: Any = None
    error: str | None = None
    subtype: str | None = None
    model: str | None = None
    requested_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    num_turns: int | None = None
    stop_reason: str | None = None
    returncode: int | None = None
    latency_s: float | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.is_error

    @property
    def output_text(self) -> str:
        if self.structured_output is not None:
            return json.dumps(self.structured_output, ensure_ascii=False, sort_keys=True)
        return self.result or ""

    def json(self) -> Any:
        """``structured_output`` if present, else the result text parsed as JSON (fences tolerated)."""
        if self.structured_output is not None:
            return self.structured_output
        text = (self.result or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        return json.loads(text)


def _int(v: Any) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _actual_model(envelope: dict[str, Any]) -> str | None:
    """The CLI reports the resolved model id in ``modelUsage``; pick the one that produced most output."""
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        def out(item: tuple[str, Any]) -> float:
            v = item[1] if isinstance(item[1], dict) else {}
            return _float(v.get("outputTokens")) or 0.0
        return max(usage.items(), key=out)[0]
    return envelope.get("model") if isinstance(envelope.get("model"), str) else None


def parse_claude_json(
    stdout: str | None,
    *,
    returncode: int | None = 0,
    stderr: str | None = "",
    requested_model: str | None = None,
    latency_s: float | None = None,
) -> ClaudeResult:
    """Parse ``claude -p --output-format json`` stdout into a :class:`ClaudeResult`. Never raises."""
    base = dict(requested_model=requested_model, model=requested_model, returncode=returncode, latency_s=latency_s)
    tail = (stderr or "").strip()[-300:]
    text = (stdout or "").strip()
    if not text:
        why = f"claude exited {returncode} with no output" + (f": {tail}" if tail else "")
        return ClaudeResult(is_error=True, error=why, **base)
    try:
        envelope: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        return ClaudeResult(is_error=True, error=f"malformed JSON from claude: {exc.msg} at char {exc.pos}", result=text[:2000], **base)
    if isinstance(envelope, list):  # stream-style array of events: the last "result" wins
        results = [e for e in envelope if isinstance(e, dict) and e.get("type") == "result"]
        if not results:
            return ClaudeResult(is_error=True, error="no result event in claude output", **base)
        envelope = results[-1]
    if not isinstance(envelope, dict):
        return ClaudeResult(is_error=True, error=f"unexpected JSON type from claude: {type(envelope).__name__}", **base)

    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    subtype = envelope.get("subtype")
    is_error = bool(envelope.get("is_error")) or subtype not in (None, "success") or (returncode not in (0, None))
    result_text = envelope.get("result") if isinstance(envelope.get("result"), str) else ""
    error = None
    if is_error:
        detail = result_text[:300] or tail
        error = f"claude error (subtype={subtype}, exit={returncode})" + (f": {detail}" if detail else "")
    base["model"] = _actual_model(envelope) or requested_model
    return ClaudeResult(
        is_error=is_error,
        result=result_text,
        structured_output=envelope.get("structured_output"),
        error=error,
        subtype=subtype,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
        total_cost_usd=_float(envelope.get("total_cost_usd")),
        duration_ms=_int(envelope.get("duration_ms")),
        duration_api_ms=_int(envelope.get("duration_api_ms")),
        num_turns=_int(envelope.get("num_turns")),
        stop_reason=envelope.get("stop_reason") if isinstance(envelope.get("stop_reason"), str) else None,
        raw=envelope,
        **base,
    )


def build_claude_cmd(
    binary: str = "claude",
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    max_turns: int | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """argv for a sandboxed print-mode call. The prompt goes on stdin, never in argv. No ``--bare``."""
    cmd = [binary, "-p", "--output-format", "json", *SANDBOX_FLAGS]
    if model:
        cmd += ["--model", model]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    cmd += list(extra_args)
    return cmd


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str = ""


class Executor(Protocol):
    def run(self, cmd: list[str], *, input: str, cwd: str, timeout: float, env: dict[str, str]) -> Completed: ...


class SubprocessExecutor:
    def run(self, cmd: list[str], *, input: str, cwd: str, timeout: float, env: dict[str, str]) -> Completed:
        proc = subprocess.run(cmd, input=input, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return Completed(proc.returncode, proc.stdout, proc.stderr)


def run_claude_p(
    prompt: str,
    *,
    binary: str = "claude",
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    cwd: str | None = None,
    max_turns: int | None = None,
    timeout_s: float = 300,
    extra_args: Sequence[str] = (),
    env: dict[str, str] | None = None,
    strip_api_key_env: bool = False,
    executor: Executor | None = None,
) -> ClaudeResult:
    """Run one ``claude -p`` call and parse it. Never raises; failures come back as ``is_error``.

    ``cwd=None`` runs in a fresh empty temp dir so no CLAUDE.md or project settings leak in.
    ``strip_api_key_env=True`` removes ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN so the CLI
    uses its subscription login instead of metered API billing.
    """
    cmd = build_claude_cmd(binary, model=model, json_schema=json_schema, max_turns=max_turns, extra_args=extra_args)
    run_env = dict(os.environ if env is None else env)
    if strip_api_key_env:
        run_env.pop("ANTHROPIC_API_KEY", None)
        run_env.pop("ANTHROPIC_AUTH_TOKEN", None)
    ex = executor or SubprocessExecutor()
    started = time.monotonic()

    def elapsed() -> float:
        return round(time.monotonic() - started, 4)

    try:
        if cwd is None:
            with tempfile.TemporaryDirectory(prefix="agent-obs-claude-") as empty:
                done = ex.run(cmd, input=prompt, cwd=empty, timeout=timeout_s, env=run_env)
        else:
            done = ex.run(cmd, input=prompt, cwd=cwd, timeout=timeout_s, env=run_env)
    except subprocess.TimeoutExpired:
        return ClaudeResult(is_error=True, error=f"claude timed out after {timeout_s}s", requested_model=model, model=model, latency_s=elapsed())
    except FileNotFoundError:
        return ClaudeResult(is_error=True, error=f"claude binary not found: {binary}", requested_model=model, model=model, latency_s=elapsed())
    except OSError as exc:
        return ClaudeResult(is_error=True, error=f"could not start claude: {exc}", requested_model=model, model=model, latency_s=elapsed())
    return parse_claude_json(done.stdout, returncode=done.returncode, stderr=done.stderr, requested_model=model, latency_s=elapsed())


class FakeClaude:
    """Drop-in :class:`Executor` that imitates ``claude -p --output-format json``. No CLI, no tokens.

    ``responder(prompt) -> dict | str | Exception``:
      * dict: returned as ``structured_output`` (as with ``--json-schema``)
      * str: returned as ``result`` text
      * ``"!error"`` prefix: an ``is_error`` envelope; an Exception is raised (e.g. TimeoutExpired)
    Token counts are synthetic (chars / 4) and the cost is a made-up rate, clearly fake.
    """

    def __init__(
        self,
        responder: Callable[[str], Any] | None = None,
        *,
        model: str = "claude-sonnet-fake",
        latency_s: float = 0.0,
        usd_per_1k_tokens: float = 0.003,
    ):
        self.responder = responder or (lambda prompt: {"ok": True, "echo_chars": len(prompt)})
        self.model = model
        self.latency_s = latency_s
        self.rate = usd_per_1k_tokens
        self.calls: list[dict[str, Any]] = []

    def run(self, cmd: list[str], *, input: str, cwd: str, timeout: float, env: dict[str, str]) -> Completed:
        self.calls.append({"cmd": list(cmd), "prompt": input, "cwd": cwd, "cwd_listing": sorted(os.listdir(cwd)), "env": env})
        if self.latency_s:
            time.sleep(self.latency_s)
        answer = self.responder(input)
        if isinstance(answer, BaseException):
            raise answer
        in_tok = max(1, len(input) // 4)
        if isinstance(answer, str) and answer.startswith("!error"):
            env_ = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                    "result": answer[6:].strip() or "fake failure", "usage": {"input_tokens": in_tok, "output_tokens": 0},
                    "total_cost_usd": round(in_tok / 1000 * self.rate, 6), "duration_ms": int(self.latency_s * 1000)}
            return Completed(1, json.dumps(env_))
        text = answer if isinstance(answer, str) else json.dumps(answer)
        out_tok = max(1, len(text) // 4)
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": answer if isinstance(answer, str) else "",
            "duration_ms": int(self.latency_s * 1000),
            "duration_api_ms": int(self.latency_s * 1000),
            "num_turns": 1,
            "stop_reason": "end_turn",
            "total_cost_usd": round((in_tok + out_tok) / 1000 * self.rate, 6),
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "modelUsage": {self.model: {"inputTokens": in_tok, "outputTokens": out_tok}},
        }
        if not isinstance(answer, str):
            envelope["structured_output"] = answer
        return Completed(0, json.dumps(envelope))
