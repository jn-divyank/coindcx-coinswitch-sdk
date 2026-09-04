"""A last line of defence against committing a credential.

The pre-commit hooks are the real protection, but they only run for someone who
installed them. This runs in CI and for anyone who types ``pytest``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Ed25519 keys are 64 or 128 hex chars; CoinDCX secrets are long hex too.
LONG_HEX = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])")

# Values that are legitimately long hex and must not trip the scan.
ALLOWED = {
    # Pinned CoinDCX HMAC test vector for the payload {"timestamp":1700000000000}
    # signed with the literal secret "secret". Not a credential.
    "62eb7dcbcda62b6f07fd37f0a5efb8de204e2700505235aec6b79a40f9d5b295",
}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [ROOT / name for name in out]


def test_no_long_hex_strings_in_tracked_files():
    """Catches a pasted API secret before it reaches a commit."""
    offenders: list[str] = []
    for path in tracked_files():
        if not path.is_file() or path.suffix in {".png", ".jpg", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in LONG_HEX.findall(text):
            if match not in ALLOWED:
                offenders.append(f"{path.relative_to(ROOT)}: {match[:12]}...")
    assert not offenders, "possible credentials committed:\n" + "\n".join(offenders)


def test_env_file_is_not_tracked():
    names = {p.name for p in tracked_files()}
    assert ".env" not in names, ".env must never be committed"


def test_env_example_has_no_filled_values():
    """The template must ship empty, or someone will commit their keys in it."""
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() in {"DCX_ALLOW_LIVE_TRADING", "DCX_MAX_NOTIONAL"}:
            continue  # numeric defaults, not credentials
        assert value.strip() == "", f".env.example has a value for {key}"


def test_gitignore_covers_env():
    ignored = (ROOT / ".gitignore").read_text()
    assert ".env" in ignored
