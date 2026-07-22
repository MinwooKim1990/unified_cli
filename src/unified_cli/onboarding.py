"""Interactive setup wizard — `unified-cli setup`.

Walks a new user through:
  1. Environment detection (binaries / OAuth / API keys)
  2. Install missing CLIs (brew or npm, with consent)
  3. Launch OAuth login for each CLI that has no auth (with consent)
  4. Verify with a tiny test call per provider
  5. Summary report

Destructive actions (running installers, spawning login) always require
explicit Confirm. Refusal just prints the command and skips.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.rule import Rule

from .core import ProviderName
from .errors import UnifiedError
from .factory import create
from .i18n import t
from .providers.gemini import gemini_enabled
from .ui import ProviderState, banner, collect_states, panel, status_table


@dataclass
class InstallCommand:
    """A candidate install command for a provider."""

    label: str            # "Homebrew" / "npm"
    argv: list[str]       # e.g., ["brew", "install", "codex"]


# Install-command preferences per provider, ordered by preference.
_INSTALL: dict[ProviderName, list[InstallCommand]] = {
    "claude": [
        InstallCommand("npm (Claude Code CLI)",
                       ["npm", "install", "-g", "@anthropic-ai/claude-code"]),
    ],
    "codex": [
        InstallCommand("Homebrew", ["brew", "install", "codex"]),
        InstallCommand("npm", ["npm", "install", "-g", "@openai/codex"]),
    ],
    # The "gemini" provider now wraps the Antigravity `agy` CLI (the old
    # `gemini` CLI is blocked for individual accounts). `agy` isn't installed
    # via npm/brew — it ships with the Antigravity suite. If `agy` isn't on
    # PATH the wizard prints this as manual guidance.
    "gemini": [
        InstallCommand("Antigravity (manual: https://antigravity.google)", ["agy"]),
    ],
}


# Login commands: each spawns the CLI in interactive mode (TTY passthrough).
_LOGIN: dict[ProviderName, list[str]] = {
    # Claude Code requires entering the TUI and using /login — we just launch
    # the TUI and instruct the user. We can't script /login because it's
    # an in-TUI slash command.
    "claude": ["claude"],
    "codex":  ["codex", "login"],
    # First-run `agy` opens the browser OAuth flow (Antigravity).
    "gemini": ["agy"],
}


@dataclass
class StepResult:
    provider: ProviderName
    ok: bool
    note: str = ""


def run_setup(
    *,
    providers: Optional[list[ProviderName]] = None,
    skip_install: bool = False,
    skip_verify: bool = False,
) -> int:
    """Interactive setup wizard. Returns exit code (0=all good, 1=some skipped)."""
    console = Console()
    console.print(banner(t("setup.banner")))
    console.print()

    # agy/gemini is ToS-gated (opt-in via UNIFIED_CLI_ENABLE_GEMINI). When not
    # enabled it is excluded from EVERY step — install, login, verify — so the
    # wizard never spawns the `agy` OAuth flow behind the gate.
    gem_ok = gemini_enabled()

    def _snapshot() -> list[ProviderState]:
        s = collect_states()
        if providers:
            s = [x for x in s if x.name in providers]
        if not gem_ok:
            s = [x for x in s if x.name != "gemini"]
        return s

    if not gem_ok:
        console.print(f"[dim]{t('setup.gemini.gated')}[/dim]")

    states = _snapshot()

    # Step 1: snapshot
    console.print(Rule(t("setup.rule.env")))
    console.print(status_table(states))
    console.print()

    # Step 2: install
    install_results: list[StepResult] = []
    if not skip_install:
        console.print(Rule(t("setup.rule.install")))
        missing = [s for s in states if not s.bin_path]
        if not missing:
            console.print(f"[green]{t('setup.install.all_detected')}[/green]")
        for s in missing:
            install_results.append(_install_one(s, console))
        console.print()

    # Step 3: login
    console.print(Rule(t("setup.rule.login")))
    # Re-detect after install
    states = _snapshot()
    login_results: list[StepResult] = []
    for s in states:
        if s.bin_path and not s.has_oauth:
            login_results.append(_login_one(s, console))
    if not login_results:
        console.print(f"[green]{t('setup.login.all_authed')}[/green]")
    console.print()

    # Step 4: verify
    verify_results: list[StepResult] = []
    if not skip_verify:
        console.print(Rule(t("setup.rule.verify")))
        verify_results = _verify_all(_snapshot(), console)
        console.print()

    # Step 5: summary
    console.print(Rule(t("setup.rule.summary")))
    console.print(status_table(_snapshot(), title=t("setup.summary.final_status")))
    _summary(console, install_results, login_results, verify_results, skip_verify)

    # Exit code: 0 if nothing was skipped, 1 if some steps were skipped/failed
    any_failed = (
        any(not r.ok for r in install_results) or
        any(not r.ok for r in login_results) or
        any(not r.ok for r in verify_results)
    )
    return 1 if any_failed else 0


# ---- per-step helpers ----

def _install_one(state: ProviderState, console: Console) -> StepResult:
    name = state.name
    console.print(
        panel(t("setup.install.no_binary_title", name=name),
              t("setup.install.no_binary_body"),
              style="yellow")
    )
    candidates = [c for c in _INSTALL[name] if shutil.which(c.argv[0])]
    if not candidates:
        # No package manager available
        all_opts = "\n".join(f"  - {c.label}: {' '.join(c.argv)}" for c in _INSTALL[name])
        console.print(f"[red]{t('setup.install.no_pkg_mgr')}[/red]")
        console.print(t("setup.install.manual", opts=all_opts))
        return StepResult(name, False, "no package manager")

    chosen = candidates[0]
    console.print(f"  → [bold]{chosen.label}[/bold]: `{' '.join(chosen.argv)}`")
    if not Confirm.ask(t("setup.install.run_prompt", name=name), default=True):
        console.print(t("setup.install.skipped", cmd=" ".join(chosen.argv)))
        return StepResult(name, False, "user declined")

    console.print(t("setup.install.running"))
    result = subprocess.run(chosen.argv)
    if result.returncode == 0:
        console.print(t("setup.install.done"))
        return StepResult(name, True, chosen.label)
    console.print(t("setup.install.failed", code=result.returncode))
    return StepResult(name, False, f"exit {result.returncode}")


def _login_one(state: ProviderState, console: Console) -> StepResult:
    name = state.name
    login_cmd = _LOGIN[name]
    note = (t("setup.login.claude_note") if name == "claude"
            else t("setup.login.generic_note"))
    console.print(
        panel(t("setup.login.needed_title", name=name),
              t("setup.login.needed_body", cmd=" ".join(login_cmd)) + note,
              style="yellow")
    )
    if not Confirm.ask(t("setup.login.prompt", name=name), default=True):
        console.print(t("setup.login.skipped", cmd=" ".join(login_cmd)))
        console.print(t("setup.login.skipped_env", env=state.api_key_env))
        return StepResult(name, False, "user declined")

    # TTY passthrough — child process owns stdin/stdout/stderr.
    result = subprocess.run(login_cmd)
    if result.returncode == 0:
        console.print(t("setup.login.spawned"))
        return StepResult(name, True, "login spawned")
    console.print(t("setup.login.exit_maybe_cancelled", code=result.returncode))
    return StepResult(name, False, f"exit {result.returncode}")


def _verify_all(states: list[ProviderState], console: Console) -> list[StepResult]:
    results: list[StepResult] = []
    with Progress(SpinnerColumn(), TextColumn("[bold]{task.description}"),
                  console=console, transient=True) as prog:
        for s in states:
            task = prog.add_task(t("setup.verify.testing", name=s.name), total=None)
            if not s.bin_path:
                prog.remove_task(task)
                console.print(t("setup.verify.no_binary", name=s.name))
                results.append(StepResult(s.name, False, "no binary"))
                continue
            if not (s.has_oauth or s.has_api_key):
                prog.remove_task(task)
                console.print(t("setup.verify.no_auth", name=s.name))
                results.append(StepResult(s.name, False, "no auth"))
                continue
            try:
                cli = create(s.name, web_search=False)
                r = cli.chat("say just: ok")
                prog.remove_task(task)
                console.print(
                    f"  [green]✓ {s.name}: {escape(r.text.strip()[:30])}[/green]  "
                    f"(tokens {r.usage.input_tokens}/{r.usage.output_tokens})"
                )
                results.append(StepResult(s.name, True, "verified"))
            except UnifiedError as e:
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: {e.kind}[/red] — {escape(e.message)}")
                console.print(t("setup.verify.hint_label", hint=escape(e.hint or "")))
                results.append(StepResult(s.name, False, e.kind))
            except Exception as e:
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: {type(e).__name__}: {escape(str(e))}[/red]")
                results.append(StepResult(s.name, False, type(e).__name__))
    return results


def _summary(
    console: Console,
    install_results: list[StepResult],
    login_results: list[StepResult],
    verify_results: list[StepResult],
    skip_verify: bool,
) -> None:
    console.print()
    all_steps = install_results + login_results + verify_results
    failures = [r for r in all_steps if not r.ok]

    if not failures:
        console.print(f"[bold green]{t('setup.summary.all_ready')}[/bold green]")
        if skip_verify:
            console.print(t("setup.summary.skip_verify_note"))
        console.print(t("setup.summary.next_step"))
        return

    console.print(f"[bold yellow]{t('setup.summary.some_manual')}[/bold yellow]")
    for r in failures:
        # `\[` keeps the literal bracket from being parsed as a Rich tag and dropped.
        console.print(f"  • \\[{escape(r.provider)}] {escape(r.note)}")
    console.print()
    console.print(t("setup.summary.retry"))
    console.print(t("setup.summary.details"))
