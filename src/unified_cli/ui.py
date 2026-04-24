"""Rich-based rendering helpers shared across doctor / status / setup."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .core import ProviderName
from .discovery import FINDERS
from .models import DEFAULT_MODELS, list_models


console = Console()


# ---- provider state snapshot (used by doctor / status / setup) ----

@dataclass
class ProviderState:
    name: ProviderName
    bin_path: Optional[str]
    has_oauth: bool
    has_api_key: bool
    api_key_env: str
    model_count: int
    model_source: str               # "api" / "cache" / "hardcoded" / "mixed"
    default_model: str

    @property
    def health(self) -> str:
        if not self.bin_path:
            return "missing_binary"
        if not (self.has_oauth or self.has_api_key):
            return "setup_needed"
        return "ok"


_AUTH_FILES = {
    "claude": Path.home() / ".claude" / ".credentials.json",
    "codex":  Path.home() / ".codex"  / "auth.json",
    "gemini": Path.home() / ".gemini" / "oauth_creds.json",
}

# macOS Keychain service names — Claude Code stores OAuth creds there by default
# on macOS, so the credentials.json file may not exist even when logged in.
_KEYCHAIN_SERVICES = {
    "claude": "Claude Code-credentials",
    # codex/gemini appear to use files on macOS, so no keychain fallback needed.
}

_API_KEY_ENVS = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _has_keychain_creds(provider: ProviderName) -> bool:
    """Check macOS Keychain for a provider's OAuth credentials.

    Only probes — does NOT read the secret (no auth prompt triggered).
    Returns False on non-macOS or when `security` command is unavailable.
    """
    if sys.platform != "darwin":
        return False
    service = _KEYCHAIN_SERVICES.get(provider)
    if not service:
        return False
    try:
        # `find-generic-password` without -w returns metadata only (no secret).
        # Exit 0 = entry exists, 44 = not found.
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def collect_states() -> list[ProviderState]:
    out: list[ProviderState] = []
    for name, finder in FINDERS.items():
        bin_path = finder()
        api_env = _API_KEY_ENVS[name]
        try:
            mods = list_models(name)  # type: ignore[arg-type]
            srcs = {m.source for m in mods}
            source = next(iter(srcs)) if len(srcs) == 1 else "mixed"
            count = len(mods)
        except Exception:
            count, source = 0, "error"
        has_oauth = (_AUTH_FILES[name].exists()
                     or _has_keychain_creds(name))  # type: ignore[arg-type]
        out.append(ProviderState(
            name=name,  # type: ignore[arg-type]
            bin_path=bin_path,
            has_oauth=has_oauth,
            has_api_key=api_env in os.environ,
            api_key_env=api_env,
            model_count=count,
            model_source=source,
            default_model=DEFAULT_MODELS[name],  # type: ignore[index]
        ))
    return out


# ---- rendering ----

_HEALTH_STYLE = {
    "ok":              ("🟢", "green", "OK"),
    "setup_needed":    ("🟡", "yellow", "setup needed"),
    "missing_binary":  ("🔴", "red", "missing binary"),
}


def health_cell(state: ProviderState) -> Text:
    icon, color, label = _HEALTH_STYLE[state.health]
    return Text(f"{icon} {label}", style=color)


def auth_cell(state: ProviderState) -> Text:
    parts: list[str] = []
    if state.has_oauth:
        parts.append("OAuth")
    if state.has_api_key:
        parts.append(f"${state.api_key_env}")
    if not parts:
        return Text("(none)", style="red")
    return Text(" + ".join(parts), style="green" if state.has_oauth else "yellow")


def bin_cell(state: ProviderState) -> Text:
    if not state.bin_path:
        return Text("(not found)", style="red")
    short = state.bin_path
    if len(short) > 48:
        short = "…" + short[-45:]
    return Text(short, style="dim")


def status_table(states: list[ProviderState], *, title: str = "Provider status") -> Table:
    t = Table(title=title, show_lines=False, header_style="bold cyan")
    t.add_column("Provider", style="bold")
    t.add_column("Health")
    t.add_column("Binary")
    t.add_column("Auth")
    t.add_column("Models", justify="right")
    t.add_column("Default model")
    for s in states:
        t.add_row(
            s.name,
            health_cell(s),
            bin_cell(s),
            auth_cell(s),
            f"{s.model_count} ({s.model_source})",
            s.default_model,
        )
    return t


def panel(title: str, body: str, *, style: str = "cyan") -> Panel:
    return Panel(body, title=title, border_style=style, expand=False)


def banner(text: str) -> Panel:
    return Panel.fit(
        Text(text, style="bold white"),
        border_style="cyan",
        padding=(0, 2),
    )
