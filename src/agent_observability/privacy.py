"""Privacy helpers: content fingerprints and a regex redactor for secrets and PII.

Content (prompts, outputs) is NOT sent by default. Only ``sha256`` and length leave
the process unless ``capture_content=True``. When content is sent, every string
passes through a :class:`Redactor` first. Error messages are always redacted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Pattern

# Order matters: specific token formats before the generic ones.
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("posthog_key", r"\bph[cxs]_[A-Za-z0-9]{20,}"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("telegram_bot_token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}"),
    ("secret_assignment", r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']?[^\s\"']{6,}"),
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # International format only (leading +): avoids eating dates, ids and prices.
    ("phone", r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}\d{2,4}\b"),
    ("iban", r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"),
]


def fingerprint(text: str | None) -> dict[str, Any]:
    """sha256 + length of a string: lets you correlate and dedupe without sending content."""
    data = (text or "").encode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(data).hexdigest(), "chars": len(text or "")}


@dataclass
class Redactor:
    """Replaces matches with ``[REDACTED:<name>]``. Extend with :meth:`add` or ``extra=``.

    Regexes are a safety net, not a guarantee: keep ``capture_content=False`` for
    anything you would not paste into a public issue.
    """

    patterns: list[tuple[str, str]] | None = None
    extra: Iterable[tuple[str, str]] = ()
    _compiled: list[tuple[str, Pattern[str]]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        for name, rx in list(self.patterns if self.patterns is not None else DEFAULT_PATTERNS) + list(self.extra):
            self.add(name, rx)

    def add(self, name: str, pattern: str | Pattern[str]) -> "Redactor":
        self._compiled.append((name, re.compile(pattern) if isinstance(pattern, str) else pattern))
        return self

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self._compiled]

    def redact(self, text: str) -> str:
        for name, rx in self._compiled:
            text = rx.sub(f"[REDACTED:{name}]", text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Redact every string inside nested dicts/lists/tuples (keys are kept)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    __call__ = redact


def redact_exception_event(msg: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """``before_send`` helper: redact exception messages on ``$exception`` events.

    The SDK builds ``$exception_list`` from the live exception, so this is the only
    place where its message can be cleaned before upload.
    """
    props = msg.get("properties") or {}
    if msg.get("event") == "$exception":
        for item in props.get("$exception_list") or []:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                item["value"] = redactor.redact(item["value"])
        for key in ("$exception_message", "$exception_values"):
            if key in props:
                props[key] = redactor.redact_obj(props[key])
    for key in ("$ai_error", "error_message"):
        if isinstance(props.get(key), str):
            props[key] = redactor.redact(props[key])
    return msg
