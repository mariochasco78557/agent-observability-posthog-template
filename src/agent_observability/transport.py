"""Transports: where events go. PostHog in production, memory in tests, nothing when unconfigured.

Every transport method may raise; :class:`SafeSink` is the layer that guarantees
the pipeline never sees those exceptions.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Protocol

from .privacy import Redactor, redact_exception_event

log = logging.getLogger("agent_observability")


class Transport(Protocol):
    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None: ...
    def capture_exception(self, exc: BaseException, distinct_id: str, properties: dict[str, Any]) -> None: ...
    def flush(self, timeout_s: float) -> bool: ...


class NullTransport:
    """No key, no network, no cost."""

    enabled = False

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        return None

    def capture_exception(self, exc: BaseException, distinct_id: str, properties: dict[str, Any]) -> None:
        return None

    def flush(self, timeout_s: float) -> bool:
        return True


class MemoryTransport:
    """Keeps events in a list (tests, ``agent-obs-demo --print``). Optionally echoes JSON lines."""

    enabled = True

    def __init__(self, echo: Callable[[str], None] | None = None):
        self.events: list[dict[str, Any]] = []
        self.flushes = 0
        self.echo = echo

    def _add(self, record: dict[str, Any]) -> None:
        # Round-trip through JSON: proves every event is serialisable, as it must be on the wire.
        record = json.loads(json.dumps(record, default=str))
        self.events.append(record)
        if self.echo:
            self.echo(json.dumps(record, sort_keys=True))

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        self._add({"event": event, "distinct_id": distinct_id, "properties": dict(properties)})

    def capture_exception(self, exc: BaseException, distinct_id: str, properties: dict[str, Any]) -> None:
        props = dict(properties)
        props["$exception_list"] = [{"type": type(exc).__name__, "value": str(exc)}]
        self._add({"event": "$exception", "distinct_id": distinct_id, "properties": props})

    def flush(self, timeout_s: float) -> bool:
        self.flushes += 1
        return True

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    @property
    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


def bounded_call(fn: Callable[[], Any], timeout_s: float) -> bool:
    """Run ``fn`` in a daemon thread; give up after ``timeout_s``. True if it finished in time."""
    done = threading.Event()
    failure: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # reported to the caller below
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=target, name="agent-obs-flush", daemon=True).start()
    finished = done.wait(max(0.0, timeout_s))
    if failure:
        raise failure[0]
    return finished


class PostHogTransport:
    """Thin wrapper over the official ``posthog`` SDK (>= 7).

    * ``before_send`` redacts exception messages and error strings before upload.
    * ``on_error`` reports background delivery failures to ``on_delivery_error``.
    * Exception autocapture stays OFF: we never install global hooks in your process.
    """

    enabled = True

    def __init__(
        self,
        api_key: str,
        host: str,
        *,
        redactor: Redactor | None = None,
        on_delivery_error: Callable[[BaseException], None] | None = None,
        client_kwargs: dict[str, Any] | None = None,
        client: Any = None,
    ):
        self.redactor = redactor or Redactor()
        self._on_delivery_error = on_delivery_error
        if client is not None:
            self.client = client
            return
        from posthog import Posthog  # imported lazily: no-op mode never imports the SDK

        kwargs: dict[str, Any] = dict(
            host=host,
            max_retries=2,
            timeout=10,
            enable_exception_autocapture=False,
            disable_geoip=True,
            before_send=self._before_send,
            on_error=self._on_error,
        )
        kwargs.update(client_kwargs or {})
        self.client = Posthog(project_api_key=api_key, **kwargs)

    def _before_send(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return redact_exception_event(msg, self.redactor)
        except Exception:
            return None  # privacy first: drop rather than send unredacted

    def _on_error(self, exc: BaseException, batch: Any = None) -> None:
        if self._on_delivery_error:
            self._on_delivery_error(exc)

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        self.client.capture(event, distinct_id=distinct_id, properties=properties)

    def capture_exception(self, exc: BaseException, distinct_id: str, properties: dict[str, Any]) -> None:
        self.client.capture_exception(exc, distinct_id=distinct_id, properties=properties)

    def flush(self, timeout_s: float) -> bool:
        # The SDK's own timeout is "not a strict wall-clock limit"; the thread join is.
        return bounded_call(lambda: self.client.flush(timeout_seconds=timeout_s), timeout_s + 0.25)


class SafeSink:
    """Wraps a transport: every call is try/except, errors are counted and logged ONCE."""

    def __init__(self, transport: Any, logger: logging.Logger = log):
        self.transport = transport
        self.errors = 0
        self.flush_timeouts = 0
        self._logged = False
        self._lock = threading.Lock()
        self.log = logger

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.transport, "enabled", True))

    def record_error(self, exc: BaseException, where: str) -> None:
        with self._lock:
            self.errors += 1
            first = not self._logged
            self._logged = True
        if first:
            self.log.warning(
                "telemetry %s failed (%s: %s); further telemetry errors are counted, not logged",
                where, type(exc).__name__, str(exc)[:200],
            )

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        try:
            self.transport.capture(event, distinct_id, properties)
        except Exception as exc:
            self.record_error(exc, f"capture({event})")

    def capture_exception(self, exc: BaseException, distinct_id: str, properties: dict[str, Any]) -> None:
        try:
            self.transport.capture_exception(exc, distinct_id, properties)
        except Exception as err:
            self.record_error(err, "capture_exception")

    def flush(self, timeout_s: float) -> bool:
        try:
            ok = bool(self.transport.flush(timeout_s))
        except Exception as exc:
            self.record_error(exc, "flush")
            return False
        if not ok:
            self.flush_timeouts += 1
            self.record_error(TimeoutError(f"flush exceeded {timeout_s}s"), "flush")
        return ok
