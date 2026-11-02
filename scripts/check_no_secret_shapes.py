#!/usr/bin/env python3
"""Fail if any text file in the repo contains a secret-shaped string.

Usage: check_no_secret_shapes.py [ROOT]   (default: repo root, parent of scripts/)
Prints `path:line: <pattern name>` per hit and exits 1; prints one OK line and exits 0 when clean.
Patterns are built from pieces so this file does not match itself.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_B = r"\b"
PATTERNS = [
    ("telegram_bot_token", re.compile(_B + r"\d{8,10}:[A-Za-z0-9_-]{35}" + _B)),
    ("github_token", re.compile(_B + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_access_key", re.compile(_B + "AK" + "IA" + r"[0-9A-Z]{16}" + _B)),
    ("private_key", re.compile("-----BEGIN" + r" [A-Z ]*PRIVATE" + " KEY-----")),
    ("bearer", re.compile(r"(?i)" + _B + "bearer" + r"\s+[A-Za-z0-9._\-~+/=]{16,}")),
    ("iban", re.compile(_B + r"[A-Z]{2}\d{2}(?: ?[0-9A-Z]{4}){4,7}" + _B)),
    ("anthropic_key", re.compile(_B + "sk-" + "ant-" + r"[A-Za-z0-9_-]{12,}")),
    ("openai_key", re.compile(_B + "sk-" + r"(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("posthog_key", re.compile(_B + "ph" + r"c_[A-Za-z0-9]{20,}")),
]

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".cfg", ".ini", ".env", ".example", ".sh", ""}


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or not path.is_file():
            continue
        if path.suffix == ".lock" or path.suffix not in TEXT_SUFFIXES:
            continue
        yield path


def scan(root: Path) -> tuple[list[str], int]:
    hits: list[str] = []
    count = 0
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        count += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS:
                if rx.search(line):
                    hits.append(f"{path.relative_to(root)}:{lineno}: {name}")
    return hits, count


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    hits, count = scan(root.resolve())
    if hits:
        print("\n".join(hits))
        return 1
    print(f"OK: no secret-shaped strings in {count} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
