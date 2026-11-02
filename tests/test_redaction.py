import pytest

from agent_observability import Redactor, fingerprint
from agent_observability.privacy import redact_exception_event

R = Redactor()


# Secret-shaped fixtures are assembled at runtime. Secret scanners (e.g. GitHub's)
# flag secret-shaped literals even when they are documented example values;
# assembling them keeps the tests identical while keeping the repo scanner-clean.
def _join(*parts) -> str:
    return "".join(parts)


def fake_anthropic_key() -> str:
    return _join("sk-", "ant-", "api03-", "abcdefghijklmnop")


def fake_openai_key() -> str:
    return _join("sk-", "proj-", "abcdefghijklmnopqrstuvwx")


def fake_posthog_key() -> str:
    return _join("ph", "c_", "abcdefghijklmnopqrstuvwxyz0123")


def fake_github_token() -> str:
    return _join("gh", "p_", "abcdefghijklmnopqrstuvwxyz012345")


def fake_aws_key() -> str:
    return _join("AK", "IA", "ABCDEFGHIJKLMNOP")


def fake_bearer() -> str:
    return _join("Bear", "er ", "abcdefghijklmnop1234")


def fake_telegram_token() -> str:
    return _join("123456789", ":", "AAHdqTcvCH1vGWJx", "fSeofSAs0K5PALDsawq")


def fake_private_key() -> str:
    begin = _join("-----BEG", "IN RSA PRIV", "ATE KEY-----")
    end = _join("-----EN", "D RSA PRIV", "ATE KEY-----")
    return _join(begin, "\nMIIabc\n", end)


def fake_iban() -> str:
    return _join("IB", "AN ES91 ", "2100 0418 ", "4502 0005 ", "1332")


DEFAULT_CASES = [
    (_join("key ", fake_anthropic_key()), "anthropic_key"),
    (_join("key ", fake_openai_key()), "openai_key"),
    (fake_posthog_key(), "posthog_key"),
    (_join("token ", fake_github_token()), "github_token"),
    (fake_aws_key(), "aws_access_key"),
    (_join("Authorization: ", fake_bearer()), "bearer"),
    ("password = hunter2hunter2", "secret_assignment"),
    ("write to a.person+jobs@example.org", "email"),
    ("call +34 600 123 456 now", "phone"),
    (_join("bot ", fake_telegram_token()), "telegram_bot_token"),
    (fake_private_key(), "private_key"),
    (fake_iban(), "iban"),
]


@pytest.mark.parametrize("text,name", DEFAULT_CASES)
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
    msg = {"event": "$exception", "properties": {"$exception_list": [{"type": "E", "value": "who@corp.example"}], "$ai_error": _join("sk-", "ant-", "abcdefghijklmn")}}
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
