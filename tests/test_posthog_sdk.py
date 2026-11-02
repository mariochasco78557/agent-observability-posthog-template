"""Real posthog SDK, offline (send=False): checks the wire event after SDK enrichment + before_send."""

import pytest

from agent_observability import FakeClaude, Pipeline, PostHogTransport


@pytest.fixture
def wire():
    sent = []
    transport = PostHogTransport("phc_offline_test", "https://eu.i.posthog.com", client_kwargs={"send": False})
    original = transport._before_send

    def spy(msg):
        out = original(msg)
        sent.append(out)
        return out
    transport.client.before_send = spy
    yield transport, sent
    transport.client.shutdown()


def test_events_pass_through_real_sdk(wire):
    transport, sent = wire
    with Pipeline("p", transport=transport, claude_executor=FakeClaude()) as run:
        with run.stage("score") as span:
            span.claude_p("x", model="sonnet")
    names = [m["event"] for m in sent]
    assert names == ["$ai_generation", "$ai_span", "$ai_trace", "pipeline_run_finished"]
    gen = sent[0]["properties"]
    assert gen["$ai_trace_id"] == run.trace_id and gen["$ai_model"] == "claude-sonnet-fake"
    assert sent[0]["distinct_id"] == "p" and run.telemetry_errors == 0


def test_exception_message_redacted_on_the_wire(wire):
    transport, sent = wire
    with pytest.raises(PermissionError):
        with Pipeline("p", transport=transport) as run:
            with run.stage("gmail"):
                raise PermissionError("token rejected for someone@example.com: ghp_" + "a" * 30)
    exc = next(m for m in sent if m["event"] == "$exception")
    value = exc["properties"]["$exception_list"][0]["value"]
    assert "example.com" not in value and "ghp_" not in value
    assert "[REDACTED:email]" in value and "[REDACTED:github_token]" in value
    assert exc["properties"]["stage"] == "gmail" and exc["properties"]["$ai_trace_id"] == run.trace_id
