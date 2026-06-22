"""Locate the `claude`, `codex`, `gemini` binaries across platforms."""

from __future__ import annotations

import glob
import os
import shutil
from typing import Optional


def _version_key(path: str) -> tuple:
    """Sort key for version directories like '2.1.111'. Numeric parts first."""
    parent = os.path.basename(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    )
    parts: list[tuple[int, object]] = []
    for p in parent.split("."):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))
    return tuple(parts)


def find_claude_bin() -> Optional[str]:
    """Search order: $CLAUDE_CLI_PATH → PATH → macOS Desktop app bundle."""
    env = os.environ.get("CLAUDE_CLI_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    on_path = shutil.which("claude")
    if on_path:
        return on_path

    home = os.path.expanduser("~")
    pattern = os.path.join(
        home, "Library", "Application Support", "Claude",
        "claude-code", "*", "claude.app", "Contents", "MacOS", "claude",
    )
    candidates = [p for p in glob.glob(pattern) if os.access(p, os.X_OK)]
    if candidates:
        candidates.sort(key=_version_key, reverse=True)
        return candidates[0]
    return None


def find_codex_bin() -> Optional[str]:
    env = os.environ.get("CODEX_CLI_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    return shutil.which("codex")


def find_gemini_bin() -> Optional[str]:
    """Locate the Antigravity `agy` CLI (the successor to the now-blocked
    `gemini` CLI for individual accounts — see migration notes in
    providers/gemini.py).

    Search order: $AGY_CLI_PATH → $GEMINI_CLI_PATH (legacy) → `agy` on PATH →
    ~/.local/bin/agy. As a last resort, falls back to a `gemini` binary on
    PATH for users who still have a working paid-API-key gemini CLI.
    """
    for var in ("AGY_CLI_PATH", "GEMINI_CLI_PATH"):
        env = os.environ.get(var)
        if env and os.path.isfile(env) and os.access(env, os.X_OK):
            return env

    on_path = shutil.which("agy")
    if on_path:
        return on_path

    local = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local

    # Legacy fallback (likely blocked by IneligibleTierError for individuals).
    return shutil.which("gemini")


# Backwards/forwards-compatible alias.
find_agy_bin = find_gemini_bin


FINDERS = {
    "claude": find_claude_bin,
    "codex": find_codex_bin,
    "gemini": find_gemini_bin,
}
