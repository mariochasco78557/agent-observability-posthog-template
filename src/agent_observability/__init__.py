"""Trace headless Claude Code pipelines into PostHog AI observability + error tracking."""

__version__ = "0.1.0"

from .claude import (  # noqa: E402
    SANDBOX_FLAGS,
    ClaudeCLIError,
    ClaudeResult,
    FakeClaude,
    build_claude_cmd,
    parse_claude_json,
    run_claude_p,
)
from .pipeline import DEFAULT_HOST, FINISH_EVENT, Pipeline, Span  # noqa: E402
from .privacy import DEFAULT_PATTERNS, Redactor, fingerprint  # noqa: E402
from .transport import MemoryTransport, NullTransport, PostHogTransport, SafeSink  # noqa: E402

__all__ = [
    "__version__", "Pipeline", "Span", "ClaudeResult", "ClaudeCLIError", "FakeClaude",
    "build_claude_cmd", "parse_claude_json", "run_claude_p", "SANDBOX_FLAGS",
    "Redactor", "fingerprint", "DEFAULT_PATTERNS",
    "MemoryTransport", "NullTransport", "PostHogTransport", "SafeSink",
    "DEFAULT_HOST", "FINISH_EVENT",
]
