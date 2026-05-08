"""Interactive REPL — `unified-cli repl`.

한 프로세스 안에서 multi-turn 대화 + provider 교체 + 슬래시 명령.

내부적으로 `UnifiedConversation(sticky=False)` 를 쓰기 때문에 provider 를
바꾸면 직전 8턴 컨텍스트가 새 provider 의 prompt 에 자동 주입된다.
"""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.table import Table

from .conversation import UnifiedConversation
from .core import ProviderName
from .errors import UnifiedError
from .factory import PROVIDERS, route
from .models import DEFAULT_MODELS
from .state import save_last_session
from .usage import tracker


console = Console()
_HISTORY_FILE = Path.home() / ".unified-cli" / "repl_history"


def _setup_readline() -> None:
    """Enable arrow-key history + line editing.

    stdlib `readline` gives free arrow history + left/right editing when
    imported. We additionally persist history across REPL sessions.
    """
    try:
        import readline  # noqa: F401
    except ImportError:
        return  # Windows default Python — user can install pyreadline3
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        import readline
        if _HISTORY_FILE.exists():
            readline.read_history_file(str(_HISTORY_FILE))
        readline.set_history_length(500)
        atexit.register(lambda: _save_history_silent(readline))
    except Exception:
        pass


def _save_history_silent(readline_mod) -> None:
    try:
        readline_mod.write_history_file(str(_HISTORY_FILE))
        try:
            os.chmod(_HISTORY_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        pass


# ---------- REPL entry ----------

def run_repl(
    *,
    provider: ProviderName = "claude",
    model: Optional[str] = None,
    web_search: bool = True,
    terse: bool = False,
    cwd: Optional[str] = None,
) -> int:
    _setup_readline()

    provider_opts: dict = {"web_search": web_search, "cwd": cwd}
    if terse:
        provider_opts["terse"] = True

    conv = UnifiedConversation(
        default_provider=provider,
        default_model=model,
        sticky=False,  # allow /provider switching with auto context injection
        provider_opts=provider_opts,
    )

    # Each turn tracks current (provider, model) for the prompt label +
    # state-file write at exit.
    current = {"provider": provider, "model": model or DEFAULT_MODELS[provider]}

    # Pending images attached for the next user prompt.
    pending_images: list[str] = []

    _banner(current)

    while True:
        try:
            line = input(_prompt(current))
        except EOFError:
            console.print()
            _on_exit(conv, current)
            return 0
        except KeyboardInterrupt:
            console.print("\n[dim]/exit 로 종료하거나 Ctrl+D 눌러.[/dim]")
            continue

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            stop = _handle_slash(line, conv, current, provider_opts, pending_images)
            if stop:
                _on_exit(conv, current)
                return 0
            continue

        # Normal chat turn — consumes pending images (one-shot per turn).
        imgs = pending_images[:] if pending_images else None
        pending_images.clear()
        _run_turn(conv, current, line, images=imgs)


# ---------- slash commands ----------

_SLASH_HELP = [
    ("/help", "이 목록"),
    ("/model <name>", "같은 provider 에서 모델 변경"),
    ("/provider <claude|codex|gemini>", "provider 전환 (컨텍스트 자동 주입)"),
    ("/image <path>", "다음 prompt 에 이미지 첨부 (Codex/Gemini, 반복 가능)"),
    ("/images", "현재 첨부된 이미지 목록"),
    ("/clear-images", "첨부된 이미지 지우기"),
    ("/new", "대화 초기화 (컨텍스트 버리기)"),
    ("/save", "현재 session_id + 이어쓰기 명령 표시"),
    ("/history [N]", "최근 N (기본 10) 턴 표시"),
    ("/tokens", "이번 프로세스 누적 토큰/호출"),
    ("/doctor", "provider 헬스 한 줄 체크"),
    ("/exit, /quit", "종료 (Ctrl+D 와 동일)"),
]


def _handle_slash(
    line: str,
    conv: UnifiedConversation,
    current: dict,
    provider_opts: dict,
    pending_images: list[str],
) -> bool:
    """Return True if REPL should exit."""
    parts = line.split()
    cmd, rest = parts[0], parts[1:]

    if cmd in ("/exit", "/quit"):
        return True

    if cmd == "/image":
        if not rest:
            console.print("[red]/image <path>[/red]")
            return False
        path = " ".join(rest)
        if not Path(path).expanduser().exists():
            console.print(f"[red]파일을 찾을 수 없음: {path}[/red]")
            return False
        pending_images.append(str(Path(path).expanduser().resolve()))
        console.print(
            f"[dim]이미지 첨부됨 ({len(pending_images)}개 대기 중). "
            f"다음 메시지에 같이 보냄.[/dim]"
        )
        return False

    if cmd == "/images":
        if not pending_images:
            console.print("[dim](첨부된 이미지 없음)[/dim]")
        else:
            for i, p in enumerate(pending_images, 1):
                console.print(f"  {i}. {p}")
        return False

    if cmd == "/clear-images":
        n = len(pending_images)
        pending_images.clear()
        console.print(f"[dim]{n}개 첨부 지움.[/dim]")
        return False

    if cmd == "/help":
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style="cyan bold"); t.add_column()
        for c, d in _SLASH_HELP:
            t.add_row(c, d)
        console.print(t)
        return False

    if cmd == "/model":
        if not rest:
            console.print("[red]/model <name> 형식.[/red]")
            return False
        new_model = rest[0]
        current["model"] = new_model
        console.print(f"[dim]모델 변경: {new_model} (같은 provider 유지)[/dim]")
        return False

    if cmd == "/provider":
        if not rest or rest[0] not in PROVIDERS:
            console.print(f"[red]/provider <claude|codex|gemini>.[/red]")
            return False
        new_provider = rest[0]  # type: ignore[assignment]
        old = current["provider"]
        current["provider"] = new_provider
        current["model"] = DEFAULT_MODELS[new_provider]
        console.print(
            f"[dim]provider 전환: {old} → {new_provider}  "
            f"(다음 턴에 직전 8턴 컨텍스트 자동 주입)[/dim]"
        )
        return False

    if cmd == "/new":
        # Replace conversation with a fresh one.
        conv.turns.clear()
        conv.sessions.clear()
        conv._clients.clear()  # type: ignore[attr-defined]
        conv._locked_provider = None  # type: ignore[attr-defined]
        console.print("[dim]대화 초기화됨.[/dim]")
        return False

    if cmd == "/save":
        sid = conv.sessions.get(current["provider"])
        if not sid:
            console.print("[yellow]아직 저장할 세션이 없음 (첫 턴 후에 /save 쓰기).[/yellow]")
        else:
            console.print(Panel(
                f"[bold]session_id[/bold]={sid}\n"
                f"[dim]이어쓰기:[/dim] [cyan]unified-cli chat \"...\" --resume {sid}[/cyan]\n"
                f"[dim]또는:[/dim] [cyan]unified-cli chat \"...\" --continue[/cyan]  "
                f"(현재 저장된 마지막 세션)",
                title="save", border_style="cyan",
            ))
        return False

    if cmd == "/history":
        limit = 10
        if rest:
            try:
                limit = max(1, int(rest[0]))
            except ValueError:
                pass
        turns = conv.turns[-limit:]
        if not turns:
            console.print("[dim](아직 히스토리 없음)[/dim]")
            return False
        t = Table(show_lines=False, header_style="bold magenta")
        t.add_column("#", justify="right", style="dim")
        t.add_column("provider")
        t.add_column("prompt", overflow="ellipsis", max_width=30)
        t.add_column("reply", overflow="ellipsis", max_width=40)
        for i, turn in enumerate(turns, start=len(conv.turns) - len(turns) + 1):
            t.add_row(str(i), turn.provider, turn.prompt, turn.text)
        console.print(t)
        return False

    if cmd == "/tokens":
        aggs = tracker.aggregates()
        if not aggs:
            console.print("[dim](이번 프로세스에서 호출 없음)[/dim]")
            return False
        t = Table(header_style="bold green")
        t.add_column("provider", style="bold")
        t.add_column("calls", justify="right")
        t.add_column("in/out", justify="right")
        t.add_column("avg latency", justify="right")
        for a in aggs:
            t.add_row(a.provider, str(a.calls),
                      f"{a.input_tokens}/{a.output_tokens}",
                      f"{a.avg_latency_ms:.0f} ms")
        console.print(t)
        return False

    if cmd == "/doctor":
        from .ui import collect_states
        for s in collect_states():
            icon = {"ok": "🟢", "setup_needed": "🟡", "missing_binary": "🔴"}[s.health]
            console.print(f"  {icon} {s.provider}: {s.health}")
        return False

    console.print(f"[red]모르는 명령: {cmd}[/red]  — /help")
    return False


# ---------- turn execution ----------

def _run_turn(
    conv: UnifiedConversation,
    current: dict,
    prompt: str,
    *,
    images: Optional[list] = None,
) -> None:
    """Stream a single user-prompt turn with spinner + tool indicators."""
    status = Status("[cyan]응답 대기 중…[/cyan]", console=console, spinner="dots")
    status.start()
    started = False
    try:
        for msg in conv.stream(
            prompt,
            provider=current["provider"],
            model=current["model"],
            images=images,
        ):
            if msg.kind == "text" and msg.text:
                if not started:
                    status.stop()
                    started = True
                print(msg.text, end="", flush=True)
            elif msg.kind == "tool_use":
                name = (msg.tool or {}).get("name")
                if not started:
                    status.update(f"[cyan]도구 사용 중: {name}[/cyan]")
                else:
                    console.print(f"\n[dim][tool: {name}][/dim]")
    except KeyboardInterrupt:
        status.stop()
        print()
        console.print("[yellow]취소됨.[/yellow]")
        return
    except UnifiedError as e:
        status.stop()
        print()
        console.print(f"[red]{e}[/red]")
        return
    finally:
        status.stop()
    if started:
        print()


# ---------- helpers ----------

def _prompt(current: dict) -> str:
    # Plain prompt (no rich markup) so readline can measure width correctly.
    return f"[{current['provider']}/{_short(current['model'])}] > "


def _short(model: str) -> str:
    # "claude-haiku-4-5" → "haiku" ; "gpt-5.4-mini" → "gpt-5.4-mini"
    if model.startswith("claude-"):
        return model.split("-")[1] if "-" in model[7:] else model
    return model


def _banner(current: dict) -> None:
    console.print(Panel.fit(
        f"[bold]unified-cli repl[/bold] — 대화형 모드\n"
        f"[dim]슬래시 명령: /help · 종료: /exit 또는 Ctrl+D[/dim]\n"
        f"[dim]시작 provider: [cyan]{current['provider']}[/cyan] / "
        f"model: [cyan]{current['model']}[/cyan][/dim]",
        border_style="cyan",
    ))


def _on_exit(conv: UnifiedConversation, current: dict) -> None:
    # Save the most recent session so `unified-cli chat --continue` can pick up.
    sid = conv.sessions.get(current["provider"])
    if sid:
        try:
            save_last_session(
                provider=current["provider"],
                model=current["model"],
                session_id=sid,
            )
            console.print(
                f"[dim]저장됨: 다음 호출에서 "
                f"[cyan]unified-cli chat \"...\" --continue[/cyan] 로 이어쓰기[/dim]"
            )
        except OSError:
            pass
    console.print("[dim]bye.[/dim]")
