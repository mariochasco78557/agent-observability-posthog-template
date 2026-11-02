import json
from pathlib import Path

import pytest

from agent_observability import FakeClaude, MemoryTransport, Pipeline

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("POSTHOG_API_KEY", "POSTHOG_HOST", "AGENT_OBS_DISABLED", "CLAUDE_BIN"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def fixture_text():
    return lambda name: (FIX / name).read_text()


@pytest.fixture
def mem():
    return MemoryTransport()


@pytest.fixture
def fake():
    return FakeClaude()


@pytest.fixture
def make_run(mem, fake):
    def make(**kw):
        kw.setdefault("transport", mem)
        kw.setdefault("claude_executor", fake)
        return Pipeline("test-pipe", distinct_id="host-1", **kw)
    return make


class Boom:
    """Transport where every method explodes."""
    enabled = True

    def __init__(self):
        self.calls = 0

    def capture(self, *a, **k):
        self.calls += 1
        raise ConnectionError("network down")

    def capture_exception(self, *a, **k):
        self.calls += 1
        raise ConnectionError("network down")

    def flush(self, *a, **k):
        self.calls += 1
        raise ConnectionError("network down")


@pytest.fixture
def boom():
    return Boom()


def props(transport, event, i=0):
    return transport.of(event)[i]["properties"]


def load(name):
    return json.loads((FIX / name).read_text())
