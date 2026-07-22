"""Rich-based rendering helpers shared across doctor / status / setup."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .core import ProviderName
from .discovery import FINDERS
from .i18n import t
from .models import DEFAULT_MODELS, list_models
from .usage import tracker


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
    has_token_env: bool = False     # e.g. CLAUDE_CODE_OAUTH_TOKEN is set
    keychain: str = "na"            # "present" / "absent" / "blocked" / "na"

    @property
    def health(self) -> str:
        if not self.bin_path:
            return "missing_binary"
        # Default provider calls deliberately strip inherited vendor API keys.
        # Only OAuth/headless-token state is usable without Python `extra_env`.
        if not self.has_oauth:
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

# Long-lived non-metered token env vars (OAuth-equivalent, not per-token API
# billing). `claude setup-token` mints CLAUDE_CODE_OAUTH_TOKEN — the officially
# supported way to authenticate Claude Code in a headless/daemon context where
# the login Keychain is unreachable.
_TOKEN_ENVS = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
}


def _keychain_status(provider: ProviderName) -> str:
    """Probe the macOS Keychain for a provider's OAuth credentials.

    Only probes metadata — does NOT read the secret (no auth prompt triggered).
    Returns "present" (entry exists), "absent" (no entry), "blocked" (query
    errored — e.g. locked/not permitted, the daemon case), or "na" (non-macOS /
    no keychain service / `security` unavailable).
    """
    if sys.platform != "darwin":
        return "na"
    service = _KEYCHAIN_SERVICES.get(provider)
    if not service:
        return "na"
    try:
        # `find-generic-password` without -w returns metadata only (no secret).
        # Exit 0 = entry exists, 44 = not found, other = query blocked/errored.
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, timeout=3,
        )
    except Exception:
        return "na"
    if r.returncode == 0:
        return "present"
    if r.returncode == 44:
        return "absent"
    return "blocked"


def _has_keychain_creds(provider: ProviderName) -> bool:
    """Back-compat boolean wrapper over `_keychain_status`."""
    return _keychain_status(provider) == "present"


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
        keychain = _keychain_status(name)  # type: ignore[arg-type]
        token_env = _TOKEN_ENVS.get(name)
        has_token = bool(token_env and os.environ.get(token_env))
        has_oauth = (_AUTH_FILES[name].exists()
                     or keychain == "present"
                     or has_token)
        out.append(ProviderState(
            name=name,  # type: ignore[arg-type]
            bin_path=bin_path,
            has_oauth=has_oauth,
            has_api_key=api_env in os.environ,
            api_key_env=api_env,
            model_count=count,
            model_source=source,
            default_model=DEFAULT_MODELS[name],  # type: ignore[index]
            has_token_env=has_token,
            keychain=keychain,
        ))
    return out


# ---- rendering ----

# (icon, color) per health state. The label is resolved via i18n at call time
# (the active language may be set after this module imports).
_HEALTH_STYLE = {
    "ok":              ("🟢", "green", "ui.health.ok"),
    "setup_needed":    ("🟡", "yellow", "ui.health.setup_needed"),
    "missing_binary":  ("🔴", "red", "ui.health.missing_binary"),
}


def health_cell(state: ProviderState) -> Text:
    icon, color, label_key = _HEALTH_STYLE[state.health]
    return Text(f"{icon} {t(label_key)}", style=color)


def auth_cell(state: ProviderState) -> Text:
    parts: list[str] = []
    if state.has_oauth:
        # Distinguish the headless token from interactive OAuth so operators
        # can see the daemon-safe path is in effect.
        parts.append("Token" if (state.has_token_env and state.keychain != "present"
                                  and not _AUTH_FILES[state.name].exists())
                     else "OAuth")
    if state.has_api_key:
        parts.append(t("ui.auth.api_key_ignored", env=state.api_key_env))
    if not parts:
        # No usable auth. If creds live only in a blocked Keychain, say so —
        # that's the launchd/daemon hang, not a missing login.
        if state.keychain == "blocked":
            return Text(t("ui.auth.keychain_blocked"), style="red")
        return Text(t("ui.auth.none"), style="red")
    return Text(" + ".join(parts), style="green" if state.has_oauth else "yellow")


def bin_cell(state: ProviderState) -> Text:
    if not state.bin_path:
        return Text(t("ui.bin.not_found"), style="red")
    short = state.bin_path
    if len(short) > 48:
        short = "…" + short[-45:]
    return Text(short, style="dim")


def status_table(states: list[ProviderState], *, title: Optional[str] = None) -> Table:
    tbl = Table(title=title if title is not None else t("ui.table.status_title"),
                show_lines=False, header_style="bold cyan")
    tbl.add_column(t("ui.table.col.provider"), style="bold")
    tbl.add_column(t("ui.table.col.health"))
    tbl.add_column(t("ui.table.col.binary"))
    tbl.add_column(t("ui.table.col.auth"))
    tbl.add_column(t("ui.table.col.models"), justify="right")
    tbl.add_column(t("ui.table.col.default_model"))
    for s in states:
        tbl.add_row(
            s.name,
            health_cell(s),
            bin_cell(s),
            auth_cell(s),
            f"{s.model_count} ({s.model_source})",
            s.default_model,
        )
    return tbl


def panel(title: str, body: str, *, style: str = "cyan") -> Panel:
    return Panel(body, title=title, border_style=style, expand=False)


def banner(text: str) -> Panel:
    return Panel.fit(
        Text(text, style="bold white"),
        border_style="cyan",
        padding=(0, 2),
    )


# ---- live status layout (shared by `status --watch` and the REPL `/status`) ----

def recent_table(limit: int = 10) -> Table:
    tbl = Table(title=t("ui.recent.title", limit=limit), show_lines=False,
                header_style="bold magenta")
    tbl.add_column(t("ui.recent.col.time"), style="dim")
    tbl.add_column(t("ui.recent.col.provider"))
    tbl.add_column(t("ui.recent.col.model"))
    tbl.add_column(t("ui.recent.col.in"), justify="right")
    tbl.add_column(t("ui.recent.col.out"), justify="right")
    tbl.add_column(t("ui.recent.col.latency"), justify="right")
    tbl.add_column(t("ui.recent.col.prompt"), style="dim")
    tbl.add_column(t("ui.recent.col.error"), style="red")
    for r in tracker.recent(limit):
        tbl.add_row(
            time.strftime("%H:%M:%S", time.localtime(r.ts)),
            r.provider, escape(r.model),
            str(r.input_tokens), str(r.output_tokens),
            f"{r.latency_ms} ms",
            escape(r.prompt_preview[:40]),
            r.error_kind or "",
        )
    return tbl


def aggregate_table() -> Table:
    tbl = Table(title=t("ui.agg.title"), show_lines=False,
                header_style="bold green")
    tbl.add_column(t("ui.agg.col.provider"), style="bold")
    tbl.add_column(t("ui.agg.col.calls"), justify="right")
    tbl.add_column(t("ui.agg.col.errors"), justify="right", style="red")
    tbl.add_column(t("ui.agg.col.tokens"), justify="right")
    tbl.add_column(t("ui.agg.col.cached"), justify="right", style="dim")
    tbl.add_column(t("ui.agg.col.avg_latency"), justify="right")
    aggs = tracker.aggregates()
    for a in aggs:
        tbl.add_row(
            a.provider, str(a.calls), str(a.errors),
            f"{a.input_tokens}/{a.output_tokens}",
            str(a.cached_tokens), f"{a.avg_latency_ms:.0f} ms",
        )
    if not aggs:
        tbl.add_row("-", "0", "0", "-", "-", "-")
    return tbl


def status_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="head", size=1),
        Layout(name="providers", ratio=2),
        Layout(name="agg", ratio=2),
        Layout(name="recent", ratio=3),
    )
    layout["head"].update(Panel(t("ui.layout.title"), border_style="cyan",
                                style="bold", padding=(0, 1)))
    layout["providers"].update(status_table(collect_states(),
                                            title=t("ui.layout.providers_title")))
    layout["agg"].update(aggregate_table())
    layout["recent"].update(recent_table(10))
    return layout
