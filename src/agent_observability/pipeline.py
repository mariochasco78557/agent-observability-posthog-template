"""Pipeline (one ``$ai_trace``) -> stages (``$ai_span``) -> generations (``$ai_generation``).

Guarantees:
  * no ``POSTHOG_API_KEY`` -> every call is a cheap no-op and the SDK is never imported;
  * telemetry never raises into your code: failures are counted and logged once;
  * your exceptions are never swallowed: stages record them, then re-raise;
  * prompt/output content stays local unless ``capture_content=True``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Sequence

from . import __version__
from .claude import ClaudeCLIError, ClaudeResult, Executor, run_claude_p
from .privacy import Redactor, fingerprint
from .transport import NullTransport, PostHogTransport, SafeSink

log = logging.getLogger("agent_observability")

DEFAULT_HOST = "https://us.i.posthog.com"  # EU Cloud: https://eu.i.posthog.com
FINISH_EVENT = "pipeline_run_finished"
_CAPTURED = "__agent_obs_captured__"


def _new_id() -> str:
    return str(uuid.uuid4())


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _safe(default: Any = None) -> Callable:
    """Decorator for telemetry-only methods: any exception is counted, never raised."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(self, *args, **kwargs)
            except Exception as exc:
                try:
                    self._pipeline.sink.record_error(exc, fn.__name__)
                except Exception:
                    pass
                return default

        return wrapper

    return deco


def _messages(value: Any, role: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    return [{"role": role, "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)}]


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class _Node:
    """Shared by Pipeline (trace level) and Span (stage level): generations and exceptions."""

    _pipeline: "Pipeline"
    _node_id: str
    _node_name: str

    def _stage_name(self) -> str | None:
        return None

    @_safe()
    def generation(
        self,
        *,
        model: str | None,
        provider: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_s: float | None = None,
        cost_usd: float | None = None,
        cost_passthrough: bool = True,
        is_error: bool = False,
        error: str | None = None,
        input: Any = None,
        output: Any = None,
        name: str | None = None,
        stop_reason: str | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        http_status: int | None = None,
        properties: dict[str, Any] | None = None,
    ) -> str | None:
        """Record one LLM call from any provider as ``$ai_generation``. Returns the generation id."""
        p = self._pipeline
        gid = _new_id()
        props: dict[str, Any] = {
            **p._ai_props(),
            "$ai_span_id": gid,
            "$ai_parent_id": self._node_id,
            "$ai_span_name": name or self._node_name,
            "$ai_model": model,
            "$ai_provider": provider,
            "$ai_is_error": bool(is_error),
        }
        if self._stage_name():
            props["stage"] = self._stage_name()
        optional = {
            "$ai_input_tokens": input_tokens,
            "$ai_output_tokens": output_tokens,
            "$ai_latency": None if latency_s is None else round(float(latency_s), 4),
            "$ai_stop_reason": stop_reason,
            "$ai_cache_read_input_tokens": cache_read_input_tokens,
            "$ai_cache_creation_input_tokens": cache_creation_input_tokens,
            "$ai_http_status": http_status,
        }
        props.update({k: v for k, v in optional.items() if v is not None})
        if cost_usd is not None:
            props["$ai_total_cost_usd"] = float(cost_usd)
            if cost_passthrough:
                props["$ai_cost_passthrough"] = True
        if is_error:
            props["$ai_error"] = p._clean_error(error or "error")
        if input is not None:
            fp = fingerprint(_as_text(input))
            props["input_sha256"], props["input_chars"] = fp["sha256"], fp["chars"]
            if p.capture_content:
                props["$ai_input"] = p._content(_messages(input, "user"))
        if output is not None:
            fp = fingerprint(_as_text(output))
            props["output_sha256"], props["output_chars"] = fp["sha256"], fp["chars"]
            if p.capture_content:
                props["$ai_output_choices"] = p._content(_messages(output, "assistant"))
        if properties:
            props.update(properties)
        p._count_generation(input_tokens, output_tokens, cost_usd, is_error)
        p.sink.capture("$ai_generation", p.distinct_id, props)
        return gid

    @_safe()
    def record_claude_result(
        self, result: ClaudeResult, *, prompt: str | None = None, name: str | None = None, properties: dict[str, Any] | None = None
    ) -> str | None:
        """Emit ``$ai_generation`` for a ``claude -p`` result you ran yourself (e.g. your own runner)."""
        latency = result.latency_s if result.latency_s is not None else (
            result.duration_ms / 1000 if result.duration_ms is not None else None)
        extra = {
            "requested_model": result.requested_model,
            "claude_subtype": result.subtype,
            "claude_duration_ms": result.duration_ms,
            "claude_duration_api_ms": result.duration_api_ms,
            "claude_num_turns": result.num_turns,
            "claude_exit_code": result.returncode,
            "cost_source": "claude_cli_estimate" if result.total_cost_usd is not None else None,
        }
        extra = {k: v for k, v in extra.items() if v is not None}
        extra.update(properties or {})
        return self.generation(
            model=result.model,
            provider="anthropic",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_s=latency,
            cost_usd=result.total_cost_usd,
            is_error=result.is_error,
            error=result.error,
            input=prompt,
            output=result.output_text if (result.ok or result.result) else None,
            name=name,
            stop_reason=result.stop_reason,
            cache_read_input_tokens=result.cache_read_input_tokens,
            cache_creation_input_tokens=result.cache_creation_input_tokens,
            properties=extra,
        )

    def claude_p(
        self,
        prompt: str,
        *,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
        timeout_s: float = 300,
        extra_args: Sequence[str] = (),
        env: dict[str, str] | None = None,
        name: str | None = None,
        check: bool = True,
        properties: dict[str, Any] | None = None,
    ) -> ClaudeResult:
        """Run ``claude -p`` (sandboxed, prompt on stdin), record the generation, return the result.

        Raises :class:`ClaudeCLIError` on failure when ``check=True`` (the generation is
        recorded first, with ``$ai_is_error``). Telemetry problems never raise.
        """
        p = self._pipeline
        result = run_claude_p(
            prompt,
            binary=p.claude_binary,
            model=model,
            json_schema=json_schema,
            cwd=cwd,
            max_turns=max_turns,
            timeout_s=timeout_s,
            extra_args=extra_args,
            env=env,
            strip_api_key_env=p.strip_api_key_env,
            executor=p.claude_executor,
        )
        self.record_claude_result(result, prompt=prompt, name=name, properties=properties)
        if check and result.is_error:
            raise ClaudeCLIError(result.error or "claude call failed", result)
        return result

    @_safe()
    def capture_exception(self, exc: BaseException, **properties: Any) -> None:
        """Send a HANDLED exception to error tracking (your code keeps running)."""
        self._pipeline._capture_exception(exc, stage=self._stage_name(), span_id=self._node_id, handled=True, extra=properties)
        self._on_handled_error()

    def _on_handled_error(self) -> None:
        pass


class Span(_Node):
    """One stage of a pipeline. Use as a context manager; emits ``$ai_span`` on exit."""

    def __init__(self, pipeline: "Pipeline", name: str, parent_id: str, attrs: dict[str, Any], parent: "Span | None" = None):
        self._pipeline = pipeline
        self._node_id = _new_id()
        self._node_name = name
        self.name = name
        self.span_id = self._node_id
        self.parent_id = parent_id
        self.parent = parent
        self._input_state = dict(attrs)
        self._output_state: dict[str, Any] = {}
        self.handled_errors = 0
        self._started = time.monotonic()
        self._closed = False

    def _stage_name(self) -> str | None:
        return self.name

    def _on_handled_error(self) -> None:
        self.handled_errors += 1

    @_safe()
    def set(self, **attrs: Any) -> None:
        """Attach output state to the span (counts, ids; not content)."""
        self._output_state.update(attrs)

    def child(self, name: str, **attrs: Any) -> "Span":
        """Nested span (tree view): parent is this span."""
        return Span(self._pipeline, name, self.span_id, attrs, parent=self)

    def __enter__(self) -> "Span":
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self._finish(exc)
        return False  # never swallow the caller's exception

    @_safe()
    def _finish(self, exc: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        p = self._pipeline
        if exc is not None and isinstance(exc, Exception):
            p._capture_exception(exc, stage=self.name, span_id=self.span_id, handled=False)
        error = f"{type(exc).__name__}: {exc}" if exc is not None else None
        props: dict[str, Any] = {
            **p._ai_props(),
            "$ai_span_id": self.span_id,
            "$ai_parent_id": self.parent_id,
            "$ai_span_name": self.name,
            "$ai_latency": round(time.monotonic() - self._started, 4),
            "$ai_is_error": exc is not None,
            "stage": self.name,
            "handled_errors": self.handled_errors,
        }
        if self._input_state:
            props["$ai_input_state"] = p.redactor.redact_obj(self._input_state)
        if self._output_state:
            props["$ai_output_state"] = p.redactor.redact_obj(self._output_state)
        if error:
            props["$ai_error"] = p._clean_error(error)
        p._stage_done(self.name, failed=exc is not None)
        p.sink.capture("$ai_span", p.distinct_id, props)


class Pipeline(_Node):
    """One run of a scheduled agent job. ``with Pipeline("name") as run: with run.stage("x") as s: ...``"""

    def __init__(
        self,
        name: str,
        distinct_id: str | None = None,
        *,
        api_key: str | None = None,
        host: str | None = None,
        capture_content: bool = False,
        redactor: Redactor | None = None,
        transport: Any = None,
        flush_timeout_s: float = 5.0,
        properties: dict[str, Any] | None = None,
        person_profiles: bool = False,
        claude_binary: str | None = None,
        claude_executor: Executor | None = None,
        strip_api_key_env: bool = False,
        max_content_chars: int = 20_000,
        trace_id: str | None = None,
    ):
        self._pipeline = self
        self.name = name
        self.distinct_id = distinct_id or name
        self.trace_id = trace_id or _new_id()
        self._node_id = self.trace_id
        self._node_name = name
        self.capture_content = bool(capture_content)
        self.redactor = redactor or Redactor()
        self.flush_timeout_s = float(flush_timeout_s)
        self.static_properties = dict(properties or {})
        self.person_profiles = person_profiles
        self.claude_binary = claude_binary or os.environ.get("CLAUDE_BIN") or "claude"
        self.claude_executor = claude_executor
        self.strip_api_key_env = strip_api_key_env
        self.max_content_chars = max_content_chars
        self.counters: dict[str, float] = {}
        self.stages: list[str] = []
        self.failed_stages: list[str] = []
        self._error_message: str | None = None
        self._ok = True
        self._stats = {"generations": 0, "generation_errors": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "exceptions": 0}
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._finished = False
        self._input_state: dict[str, Any] = {}
        self.sink = SafeSink(NullTransport())
        self.sink.transport = self._make_transport(transport, api_key, host)

    # -- setup ---------------------------------------------------------------------------
    def _make_transport(self, transport: Any, api_key: str | None, host: str | None) -> Any:
        if transport is not None:
            return transport
        if _truthy(os.environ.get("AGENT_OBS_DISABLED")):
            return NullTransport()
        key = (api_key if api_key is not None else os.environ.get("POSTHOG_API_KEY", "")).strip()
        if not key:
            return NullTransport()
        try:
            return PostHogTransport(
                key,
                host or os.environ.get("POSTHOG_HOST") or DEFAULT_HOST,
                redactor=self.redactor,
                on_delivery_error=lambda exc: self.sink.record_error(exc, "delivery"),
            )
        except Exception as exc:  # SDK missing or broken: degrade to no-op, never fail the job
            self.sink.record_error(exc, "init")
            return NullTransport()

    @property
    def enabled(self) -> bool:
        return self.sink.enabled

    @property
    def telemetry_errors(self) -> int:
        return self.sink.errors

    @property
    def ok(self) -> bool:
        return self._ok

    # -- helpers ---------------------------------------------------------------------------
    def _base_props(self) -> dict[str, Any]:
        props: dict[str, Any] = {"pipeline": self.name, "agent_obs_version": __version__, **self.static_properties}
        if not self.person_profiles:
            props["$process_person_profile"] = False
        return props

    def _ai_props(self) -> dict[str, Any]:
        return {**self._base_props(), "$ai_trace_id": self.trace_id, "$ai_session_id": None}

    def _clean_error(self, text: str) -> str:
        return self.redactor.redact(str(text))[:1000]

    def _content(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = self.redactor.redact_obj(messages)
        limit = self.max_content_chars
        for m in out:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str) and len(c) > limit:
                m["content"] = c[:limit] + f"...[truncated {len(c) - limit} chars]"
        return out

    def _count_generation(self, inp: int | None, out: int | None, cost: float | None, is_error: bool) -> None:
        with self._lock:
            s = self._stats
            s["generations"] += 1
            s["generation_errors"] += int(bool(is_error))
            s["input_tokens"] += int(inp or 0)
            s["output_tokens"] += int(out or 0)
            s["cost_usd"] += float(cost or 0.0)

    def _stage_done(self, name: str, failed: bool) -> None:
        with self._lock:
            self.stages.append(name)
            if failed and name not in self.failed_stages:
                self.failed_stages.append(name)

    def _capture_exception(self, exc: BaseException, *, stage: str | None, span_id: str | None, handled: bool, extra: dict[str, Any] | None = None) -> None:
        if getattr(exc, _CAPTURED, False):
            return
        try:
            setattr(exc, _CAPTURED, True)
        except Exception:
            pass
        props: dict[str, Any] = {**self._base_props(), "$ai_trace_id": self.trace_id, "handled": handled}
        if stage:
            props["stage"] = stage
        if span_id and span_id != self.trace_id:
            props["$ai_span_id"] = span_id
        if extra:
            props.update(extra)
        with self._lock:
            self._stats["exceptions"] += 1
        self.sink.capture_exception(exc, self.distinct_id, props)

    # -- public API ------------------------------------------------------------------------
    def stage(self, name: str, **attrs: Any) -> Span:
        """A stage span. ``attrs`` become ``$ai_input_state`` (keep them small and content-free)."""
        return Span(self, name, self.trace_id, attrs)

    @_safe()
    def set(self, **attrs: Any) -> None:
        """Attach input state to the trace (run parameters, not content)."""
        self._input_state.update(attrs)

    @_safe()
    def counter(self, name: str, value: float) -> None:
        """Set a counter to an absolute value (sent as ``counter_<name>`` on the finish event)."""
        self.counters[str(name)] = int(value) if isinstance(value, bool) else value

    @_safe()
    def incr(self, name: str, by: float = 1) -> None:
        """Increment a counter."""
        with self._lock:
            self.counters[str(name)] = self.counters.get(str(name), 0) + by

    @_safe()
    def mark_error(self, message: str) -> None:
        """Mark the run as failed without an exception (e.g. an alert condition)."""
        self._ok = False
        self._error_message = self._clean_error(message)

    def __enter__(self) -> "Pipeline":
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self.finish(exc)
        return False

    @_safe(default=False)
    def finish(self, exc: BaseException | None = None) -> bool:
        """Emit ``$ai_trace`` + ``pipeline_run_finished`` and flush (bounded). Idempotent."""
        if self._finished:
            return self._ok
        self._finished = True
        try:
            self._emit_finish(exc)
        finally:
            self.sink.flush(self.flush_timeout_s)  # always flush, even if building an event failed
        return self._ok

    def _emit_finish(self, exc: BaseException | None) -> None:
        if exc is not None:
            self._ok = False
            self._error_message = self._clean_error(f"{type(exc).__name__}: {exc}")
            if isinstance(exc, Exception):
                self._capture_exception(exc, stage=None, span_id=None, handled=False)
        if self.failed_stages:
            self._ok = False
        latency = round(time.monotonic() - self._started, 4)
        trace: dict[str, Any] = {
            **self._ai_props(),
            "$ai_span_name": self.name,
            "$ai_latency": latency,
            "$ai_is_error": not self._ok,
            "$ai_output_state": {"counters": dict(self.counters), "failed_stages": list(self.failed_stages)},
        }
        if self._input_state:
            trace["$ai_input_state"] = self.redactor.redact_obj(self._input_state)
        if self._error_message:
            trace["$ai_error"] = self._error_message
        self.sink.capture("$ai_trace", self.distinct_id, trace)
        s = self._stats
        finished: dict[str, Any] = {
            **self._base_props(),
            "$ai_trace_id": self.trace_id,
            "ok": self._ok,
            "duration_s": latency,
            "stages": list(self.stages),
            "failed_stages": list(self.failed_stages),
            "failed_stage": self.failed_stages[0] if self.failed_stages else None,
            "error_message": self._error_message,
            "generations": s["generations"],
            "generation_errors": s["generation_errors"],
            "input_tokens_total": s["input_tokens"],
            "output_tokens_total": s["output_tokens"],
            "cost_usd_total": round(s["cost_usd"], 6),
            "exceptions": s["exceptions"],
            "telemetry_errors": self.sink.errors,
            **{f"counter_{k}": v for k, v in self.counters.items()},
        }
        self.sink.capture(FINISH_EVENT, self.distinct_id, finished)

    close = finish
