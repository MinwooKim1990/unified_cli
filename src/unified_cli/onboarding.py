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
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.rule import Rule

from .core import ProviderName
from .errors import UnifiedError
from .factory import create
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
    "gemini": [
        InstallCommand("npm", ["npm", "install", "-g", "@google/gemini-cli"]),
    ],
}


# Login commands: each spawns the CLI in interactive mode (TTY passthrough).
_LOGIN: dict[ProviderName, list[str]] = {
    # Claude Code requires entering the TUI and using /login — we just launch
    # the TUI and instruct the user. We can't script /login because it's
    # an in-TUI slash command.
    "claude": ["claude"],
    "codex":  ["codex", "login"],
    # First-run `gemini` triggers OAuth automatically.
    "gemini": ["gemini"],
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
    console.print(banner("unified-cli setup — 3개 provider 온보딩"))
    console.print()

    states = collect_states()
    if providers:
        states = [s for s in states if s.name in providers]

    # Step 1: snapshot
    console.print(Rule("1. 환경 검사"))
    console.print(status_table(states))
    console.print()

    # Step 2: install
    install_results: list[StepResult] = []
    if not skip_install:
        console.print(Rule("2. 누락된 CLI 설치"))
        missing = [s for s in states if not s.bin_path]
        if not missing:
            console.print("[green]✓ 모든 CLI 바이너리 감지됨[/green]")
        for s in missing:
            install_results.append(_install_one(s, console))
        console.print()

    # Step 3: login
    console.print(Rule("3. 로그인 (OAuth) 필요한 provider"))
    # Re-detect after install
    states = collect_states()
    if providers:
        states = [s for s in states if s.name in providers]
    login_results: list[StepResult] = []
    for s in states:
        if s.bin_path and not (s.has_oauth or s.has_api_key):
            login_results.append(_login_one(s, console))
    if not login_results:
        console.print("[green]✓ 모든 provider 가 이미 인증됨 (OAuth 또는 API key)[/green]")
    console.print()

    # Step 4: verify
    verify_results: list[StepResult] = []
    if not skip_verify:
        console.print(Rule("4. 테스트 호출 검증"))
        states = collect_states()
        if providers:
            states = [s for s in states if s.name in providers]
        verify_results = _verify_all(states, console)
        console.print()

    # Step 5: summary
    console.print(Rule("5. 요약"))
    console.print(status_table(collect_states() if not providers else
                               [s for s in collect_states() if s.name in providers],
                               title="최종 상태"))
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
        panel(f"[{name}] 바이너리 없음",
              f"아래 설치 명령을 실행합니다 (Y/n). 거부하면 명령만 출력됩니다.",
              style="yellow")
    )
    candidates = [c for c in _INSTALL[name] if shutil.which(c.argv[0])]
    if not candidates:
        # No package manager available
        all_opts = "\n".join(f"  - {c.label}: {' '.join(c.argv)}" for c in _INSTALL[name])
        console.print(f"[red]사용 가능한 패키지 매니저(brew/npm) 가 없습니다.[/red]")
        console.print(f"수동으로 다음 중 하나를 실행하세요:\n{all_opts}")
        return StepResult(name, False, "no package manager")

    chosen = candidates[0]
    console.print(f"  → [bold]{chosen.label}[/bold]: `{' '.join(chosen.argv)}`")
    if not Confirm.ask(f"[{name}] 실행할까요?", default=True):
        console.print(f"  [yellow]건너뜀.[/yellow] 수동으로 실행: {' '.join(chosen.argv)}")
        return StepResult(name, False, "user declined")

    console.print(f"  실행 중... (스트림 출력)")
    result = subprocess.run(chosen.argv)
    if result.returncode == 0:
        console.print(f"  [green]✓ 설치 완료[/green]")
        return StepResult(name, True, chosen.label)
    console.print(f"  [red]✗ 설치 실패 (exit {result.returncode})[/red]")
    return StepResult(name, False, f"exit {result.returncode}")


def _login_one(state: ProviderState, console: Console) -> StepResult:
    name = state.name
    login_cmd = _LOGIN[name]
    console.print(
        panel(f"[{name}] 로그인 필요",
              (f"아래 명령으로 OAuth 로그인을 시작합니다 (브라우저가 열립니다).\n"
               f"  {' '.join(login_cmd)}\n\n"
               + ("⚠  Claude 의 경우 TUI 진입 후 `/login` 슬래시 명령을 치고 엔터, "
                  "완료되면 `/exit` 로 나오세요." if name == "claude" else
                  "완료 후 자동으로 setup 이 이어집니다.")),
              style="yellow")
    )
    if not Confirm.ask(f"[{name}] 지금 로그인할까요?", default=True):
        console.print(f"  [yellow]건너뜀.[/yellow] 수동 실행: {' '.join(login_cmd)}")
        console.print(f"  또는 환경변수 {state.api_key_env} 설정으로 API key 사용 가능.")
        return StepResult(name, False, "user declined")

    # TTY passthrough — child process owns stdin/stdout/stderr.
    result = subprocess.run(login_cmd)
    if result.returncode == 0:
        console.print(f"  [green]✓ 로그인 프로세스 종료[/green]")
        return StepResult(name, True, "login spawned")
    console.print(f"  [yellow]로그인 프로세스 exit {result.returncode} (취소되었을 수 있음)[/yellow]")
    return StepResult(name, False, f"exit {result.returncode}")


def _verify_all(states: list[ProviderState], console: Console) -> list[StepResult]:
    results: list[StepResult] = []
    with Progress(SpinnerColumn(), TextColumn("[bold]{task.description}"),
                  console=console, transient=True) as prog:
        for s in states:
            task = prog.add_task(f"{s.name} 테스트 호출...", total=None)
            if not s.bin_path:
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: 바이너리 없음 — 검증 건너뜀[/red]")
                results.append(StepResult(s.name, False, "no binary"))
                continue
            if not (s.has_oauth or s.has_api_key):
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: 인증 없음 — 검증 건너뜀[/red]")
                results.append(StepResult(s.name, False, "no auth"))
                continue
            try:
                cli = create(s.name, web_search=False)
                r = cli.chat("say just: ok")
                prog.remove_task(task)
                console.print(
                    f"  [green]✓ {s.name}: {r.text.strip()[:30]}[/green]  "
                    f"(tokens {r.usage.input_tokens}/{r.usage.output_tokens})"
                )
                results.append(StepResult(s.name, True, "verified"))
            except UnifiedError as e:
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: {e.kind}[/red] — {e.message}")
                console.print(f"     힌트: {e.hint}")
                results.append(StepResult(s.name, False, e.kind))
            except Exception as e:
                prog.remove_task(task)
                console.print(f"  [red]✗ {s.name}: {type(e).__name__}: {e}[/red]")
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
        console.print("[bold green]✓ 모든 provider 준비 완료[/bold green]")
        if skip_verify:
            console.print("[dim]--skip-verify 로 검증 호출은 생략됐습니다.[/dim]")
        console.print("다음 단계: [cyan]unified-cli chat \"안녕\" -m haiku[/cyan]")
        return

    console.print("[bold yellow]일부 provider 는 수동 처리가 필요합니다:[/bold yellow]")
    for r in failures:
        console.print(f"  • [{r.provider}] {r.note}")
    console.print()
    console.print("재시도: [cyan]unified-cli setup[/cyan]")
    console.print("상세: [cyan]unified-cli doctor[/cyan]")
