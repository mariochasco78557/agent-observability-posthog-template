import pytest

from agent_observability import Redactor, fingerprint
from agent_observability.privacy import redact_exception_event

R = Redactor()


@pytest.mark.parametrize(
    "text,name",
    [
        ("key sk-ant-api03-abcdefghijklmnop", "anthropic_key"),
        ("key sk-proj-abcdefghijklmnopqrstuvwx", "openai_key"),
        ("phc_abcdefghijklmnopqrstuvwxyz0123", "posthog_key"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz012345", "github_token"),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
        ("Authorization: Bearer abcdefghijklmnop1234", "bearer"),
        ("password = hunter2hunter2", "secret_assignment"),
        ("write to a.person+jobs@example.org", "email"),
        ("call +34 600 123 456 now", "phone"),
        ("bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawq", "telegram_bot_token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "private_key"),
        ("IBAN ES91 2100 0418 4502 0005 1332", "iban"),
    ],
)
def test_default_patterns(text, name):
    out = R(text)
    assert f"[REDACTED:{name}]" in out


@pytest.mark.parametrize("text", ["run 2026-11-02 15:48:13 took 4210 ms", "score 82/100, 1520 tokens, $0.0123", "trace d9222e05-8708-41b8-98ea-d4a21849e761"])
def test_no_false_positives_on_ordinary_text(text):
    assert R(text) == text


def test_extra_and_add_patterns():
    r = Redactor(extra=[("ticket", r"TCK-\d+")]).add("internal_host", r"\b[a-z0-9]+\.corp\.internal\b")
    assert r("TCK-991 on db1.corp.internal") == "[REDACTED:ticket] on [REDACTED:internal_host]"
    assert r.names[-2:] == ["ticket", "internal_host"]


def test_custom_pattern_list_replaces_defaults():
    r = Redactor(patterns=[("digits", r"\d+")])
    assert r("a@b.co 42") == "a@b.co [REDACTED:digits]"


def test_redact_obj_nested():
    obj = {"messages": [{"role": "user", "content": "x@y.io"}], "n": 3, "t": ("ghp_" + "b" * 30,)}
    out = R.redact_obj(obj)
    assert out == {"messages": [{"role": "user", "content": "[REDACTED:email]"}], "n": 3, "t": ["[REDACTED:github_token]"]}


def test_fingerprint_stable_and_content_free():
    a, b = fingerprint("hello"), fingerprint("hello")
    assert a == b and a["chars"] == 5 and len(a["sha256"]) == 64 and "hello" not in str(a)
    assert fingerprint(None)["chars"] == 0


def test_before_send_hook_redacts_exception_list():
    msg = {"event": "$exception", "properties": {"$exception_list": [{"type": "E", "value": "who@corp.example"}], "$ai_error": "sk-ant-abcdefghijklmn"}}
    out = redact_exception_event(msg, R)
    assert out["properties"]["$exception_list"][0]["value"] == "[REDACTED:email]"
    assert out["properties"]["$ai_error"] == "[REDACTED:anthropic_key]"


def test_before_send_drops_event_if_redaction_breaks():
    from agent_observability import PostHogTransport

    class Bad(Redactor):
        def redact(self, text):
            raise RuntimeError("bug")
    t = PostHogTransport("k", "h", redactor=Bad(), client=object())
    assert t._before_send({"event": "$exception", "properties": {"$exception_list": [{"value": "x"}]}}) is None
