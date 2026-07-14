"""Locate the `claude`, `codex`, `gemini` binaries across platforms."""

from __future__ import annotations

import glob
import os
import shutil
from typing import Optional


# Well-known install locations to probe when the binary isn't on PATH. This
# matters under launchd/cron/systemd, whose default PATH is just
# /usr/bin:/bin:/usr/sbin:/sbin — so `shutil.which` misses Homebrew, npm-global,
# ~/.local/bin, etc. even though the CLI is installed. Checked only after PATH.
_CLAUDE_FALLBACK_BINS = [
    "~/.local/bin/claude", "~/.claude/local/claude",
    "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
    "~/.npm-global/bin/claude", "/usr/local/lib/node_modules/.bin/claude",
]
_CODEX_FALLBACK_BINS = [
    "~/.local/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
    "~/.npm-global/bin/codex", "~/.cargo/bin/codex",
]


def _first_executable(candidates: list[str]) -> Optional[str]:
    """Return the first candidate path that exists and is executable."""
    for p in candidates:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


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
    """Search: $CLAUDE_CLI_PATH → PATH → well-known bins → macOS app bundle."""
    env = os.environ.get("CLAUDE_CLI_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    on_path = shutil.which("claude")
    if on_path:
        return on_path

    fallback = _first_executable(_CLAUDE_FALLBACK_BINS)
    if fallback:
        return fallback

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
    """Search: $CODEX_CLI_PATH → PATH → well-known install locations."""
    env = os.environ.get("CODEX_CLI_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    return _first_executable(_CODEX_FALLBACK_BINS)


def find_agy_bin() -> Optional[str]:
    """Locate the Antigravity ``agy`` CLI.

    The provider named ``gemini`` wraps Antigravity, not the retired Gemini
    CLI. Its command-line flags, session format, and plain-text output parser
    are agy-specific, so selecting a legacy ``gemini`` executable would fail
    unpredictably rather than provide a usable fallback.

    Search order: $AGY_CLI_PATH → ``agy`` on PATH → ~/.local/bin/agy.
    """
    env = os.environ.get("AGY_CLI_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env

    on_path = shutil.which("agy")
    if on_path:
        return on_path

    local = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local

    return None


# The public provider key remains "gemini" for compatibility. Keep this
# function name as an import compatibility alias, but never probe the legacy
# Gemini CLI or its GEMINI_CLI_PATH variable from the agy-only provider.
find_gemini_bin = find_agy_bin


FINDERS = {
    "claude": find_claude_bin,
    "codex": find_codex_bin,
    "gemini": find_gemini_bin,
}
