"""Interactive REPL — `unified-cli repl`.

Multi-turn chat + provider switching + slash commands in one process. Uses
`UnifiedConversation(sticky=False)`, so switching provider auto-injects the
last 8 turns into the new provider's prompt.

When prompt_toolkit is available and stdout is a TTY, the REPL offers a live
slash-command menu (type `/`) and model pickers; otherwise it falls back to a
plain `input()` loop with readline history. All user-facing text is localized
(English default; `--lang ko` / `/lang ko` for Korean).
"""

from __future__ import annotations

import atexit
import os
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status
from rich.table import Table

from . import settings
from .conversation import UnifiedConversation
from .core import ProviderName
from .errors import UnifiedError
from .factory import PROVIDERS
from .i18n import current_lang, set_lang, t
from .models import DEFAULT_MODELS
from .providers.gemini import gemini_enabled
from .repl_completion import (
    SLASH_COMMANDS, arg_candidates, build_session, has_prompt_toolkit,
    pick_model, pick_provider, warm_models_async,
)
from .state import save_last_session
from .ui import status_layout
from .usage import tracker


console = Console()
_HISTORY_FILE = Path.home() / ".unified-cli" / "repl_history"          # readline
_PTK_HISTORY_FILE = Path.home() / ".unified-cli" / "repl_history.ptk"   # prompt_toolkit


def _setup_readline() -> None:
    """Arrow-key history + line editing for the input() fallback path."""
    try:
        import readline  # noqa: F401
    except ImportError:
        return
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


def _interactive() -> bool:
    """True when we have a real terminal for prompt_toolkit / rich.Live."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _harden_repl_history() -> None:
    """Keep the prompt_toolkit history file owner-only (0o600), matching the
    readline history / state.json / settings.json. REPL prompts can contain
    secrets and must not be world-readable on a shared host. Best-effort."""
    try:
        _PTK_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(_PTK_HISTORY_FILE.parent, 0o700)
        _PTK_HISTORY_FILE.touch(exist_ok=True)
        os.chmod(_PTK_HISTORY_FILE, 0o600)
    except OSError:
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
    # If the user asked to start on the gated provider, fall back gracefully.
    if provider == "gemini" and not gemini_enabled():
        console.print(f"[yellow]{t('repl.gemini.locked')}[/yellow]")
        provider = "claude"
        model = None

    provider_opts: dict = {"web_search": web_search, "cwd": cwd}
    if terse:
        provider_opts["terse"] = True

    conv = UnifiedConversation(
        default_provider=provider,
        default_model=model,
        sticky=False,
        provider_opts=provider_opts,
    )

    current = {"provider": provider, "model": model or DEFAULT_MODELS[provider]}
    pending_images: list[str] = []

    # Input driver: prompt_toolkit (live menu) when interactive, else readline.
    use_ptk = has_prompt_toolkit() and _interactive()
    session = None
    if use_ptk:
        _harden_repl_history()
        session = build_session(_PTK_HISTORY_FILE, current)
        warm_models_async(("claude", "codex"))  # gemini warmed lazily
    else:
        _setup_readline()

    _banner(current, use_ptk)

    while True:
        try:
            prompt_str = _prompt(current)
            line = session.prompt(prompt_str) if session else input(prompt_str)
        except EOFError:
            console.print()
            _on_exit(conv, current)
            return 0
        except KeyboardInterrupt:
            console.print(f"\n[dim]{t('repl.interrupt_hint')}[/dim]")
            continue

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            # Defense-in-depth: a bug in any slash handler must not tear down
            # the whole REPL (losing in-memory session state).
            try:
                stop = _handle_slash(line, conv, current, provider_opts,
                                     pending_images, use_ptk)
            except KeyboardInterrupt:
                console.print()
                continue
            except Exception as e:  # noqa: BLE001 - degrade, don't crash
                # escape: the error text may contain Rich-markup-shaped tokens
                # (e.g. CLI stderr with "[/red]") which would re-raise here.
                console.print(f"[red]{escape(t('repl.slash_error', err=e))}[/red]")
                continue
            if stop:
                _on_exit(conv, current)
                return 0
            continue

        imgs = pending_images[:] if pending_images else None
        pending_images.clear()
        # Defense-in-depth (same as the slash path): an unexpected turn error
        # (e.g. the CLI binary removed mid-session, or invalid UTF-8 from the
        # child) must not tear down the REPL and lose in-memory history.
        try:
            _run_turn(conv, current, line, images=imgs)
        except Exception as e:  # noqa: BLE001 - degrade, don't crash
            console.print(f"[red]{escape(t('repl.turn.error', err=e))}[/red]")


# ---------- slash commands ----------

def _switch_provider(current: dict, new_provider: str) -> bool:
    """Switch provider with the gemini gate enforced. Returns True if switched."""
    if new_provider not in PROVIDERS:
        console.print(f"[red]{t('repl.provider.usage')}[/red]")
        return False
    if new_provider == "gemini" and not gemini_enabled():
        console.print(f"[yellow]{t('repl.gemini.locked')}[/yellow]")
        return False
    old = current["provider"]
    if new_provider == old:
        return False
    current["provider"] = new_provider
    current["model"] = DEFAULT_MODELS[new_provider]
    console.print(f"[dim]{t('repl.provider.switched', old=old, new=new_provider)}[/dim]")
    return True


def _handle_slash(
    line: str,
    conv: UnifiedConversation,
    current: dict,
    provider_opts: dict,
    pending_images: list[str],
    use_ptk: bool,
) -> bool:
    """Return True if the REPL should exit."""
    parts = line.split()
    cmd, rest = parts[0], parts[1:]

    if cmd in ("/exit", "/quit"):
        return True

    if cmd == "/help":
        _print_help(current)
        return False

    if cmd == "/lang":
        if not rest:
            console.print(f"[dim]{t('repl.lang.usage', lang=current_lang())}[/dim]")
            return False
        code = rest[0].strip().lower()
        try:
            set_lang(code)
        except ValueError:
            console.print(f"[red]{t('repl.lang.unknown', lang=escape(code))}[/red]")
            return False
        try:
            settings.set("lang", code)
        except Exception:
            pass
        console.print(f"[dim]{t('repl.lang.changed', lang=code)}[/dim]")
        _banner(current, use_ptk)
        return False

    if cmd == "/status":
        _live_status()
        return False

    if cmd == "/image":
        if not rest:
            console.print(f"[red]{t('repl.image.usage')}[/red]")
            return False
        path = " ".join(rest)
        if not Path(path).expanduser().exists():
            console.print(f"[red]{t('repl.image.not_found', path=escape(path))}[/red]")
            return False
        pending_images.append(str(Path(path).expanduser().resolve()))
        console.print(f"[dim]{t('repl.image.attached', n=len(pending_images))}[/dim]")
        return False

    if cmd == "/images":
        if not pending_images:
            console.print(f"[dim]{t('repl.images.none')}[/dim]")
        else:
            for i, p in enumerate(pending_images, 1):
                console.print(f"  {i}. {escape(p)}")
        return False

    if cmd == "/clear-images":
        n = len(pending_images)
        pending_images.clear()
        console.print(f"[dim]{t('repl.images.cleared', n=n)}[/dim]")
        return False

    if cmd == "/model":
        if rest:
            # Models can be multi-word (agy display names like "Gemini 3.5
            # Flash (Medium)"), so take the whole argument, not just rest[0].
            # split(None, 1) matches the same whitespace class as line.split()
            # used for `rest`, so a tab separator can't IndexError here.
            new_model = line.split(None, 1)[1].strip()
            known = {mid for mid, _ in arg_candidates("/model", current["provider"], "")}
            current["model"] = new_model
            if new_model not in known:
                console.print(f"[yellow]{t('repl.model.unknown', model=escape(new_model))}[/yellow]")
            console.print(f"[dim]{t('repl.model.changed', model=escape(new_model))}[/dim]")
            return False
        # no arg → picker (interactive) or numbered list (fallback)
        if use_ptk:
            chosen = pick_model(current["provider"])
            if chosen:
                current["model"] = chosen
                console.print(f"[dim]{t('repl.model.changed', model=escape(chosen))}[/dim]")
            else:
                console.print(f"[dim]{t('repl.model.cancelled')}[/dim]")
        else:
            _print_model_list(current["provider"])
        return False

    if cmd == "/provider":
        if rest:
            _switch_provider(current, rest[0])
            return False
        if use_ptk:
            chosen = pick_provider()
            if chosen:
                _switch_provider(current, chosen)
        else:
            console.print(f"[dim]{t('repl.provider.usage')}[/dim]")
        return False

    if cmd == "/new":
        conv.turns.clear()
        conv.sessions.clear()
        conv._clients.clear()  # type: ignore[attr-defined]
        conv._locked_provider = None  # type: ignore[attr-defined]
        console.print(f"[dim]{t('repl.new.done')}[/dim]")
        return False

    if cmd == "/save":
        sid = conv.sessions.get(current["provider"])
        if not sid:
            console.print(f"[yellow]{t('repl.save.none')}[/yellow]")
        else:
            console.print(Panel(
                t("repl.save.body", sid=escape(sid)),
                title=t("repl.save.title"), border_style="cyan",
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
            console.print(f"[dim]{t('repl.history.none')}[/dim]")
            return False
        tbl = Table(show_lines=False, header_style="bold magenta")
        tbl.add_column("#", justify="right", style="dim")
        tbl.add_column("provider")
        tbl.add_column("prompt", overflow="ellipsis", max_width=30)
        tbl.add_column("reply", overflow="ellipsis", max_width=40)
        for i, turn in enumerate(turns, start=len(conv.turns) - len(turns) + 1):
            tbl.add_row(str(i), turn.provider, escape(turn.prompt), escape(turn.text))
        console.print(tbl)
        return False

    if cmd == "/tokens":
        aggs = tracker.aggregates()
        if not aggs:
            console.print(f"[dim]{t('repl.tokens.none')}[/dim]")
            return False
        tbl = Table(header_style="bold green")
        tbl.add_column("provider", style="bold")
        tbl.add_column("calls", justify="right")
        tbl.add_column("in/out", justify="right")
        tbl.add_column("avg latency", justify="right")
        for a in aggs:
            tbl.add_row(a.provider, str(a.calls),
                        f"{a.input_tokens}/{a.output_tokens}",
                        f"{a.avg_latency_ms:.0f} ms")
        console.print(tbl)
        return False

    if cmd == "/doctor":
        from .ui import _HEALTH_STYLE, collect_states
        for s in collect_states():
            icon, color, label_key = _HEALTH_STYLE.get(s.health, ("•", "dim", None))
            label = t(label_key) if label_key else s.health
            console.print(f"  {icon} {s.name}: [{color}]{label}[/{color}]")
        return False

    console.print(f"[red]{t('repl.unknown', cmd=escape(cmd))}[/red]")
    return False


def _print_model_list(provider: str) -> None:
    """Fallback model display when prompt_toolkit isn't available."""
    cands = arg_candidates("/model", provider, "")
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="cyan")
    tbl.add_column(style="dim")
    for mid, meta in cands:
        tbl.add_row(escape(mid), escape(meta))
    console.print(tbl)
    console.print(f"[dim]{t('repl.model.usage')}[/dim]")


def _print_help(current: dict) -> None:
    tbl = Table(show_header=True, box=None, padding=(0, 2),
                header_style="bold cyan")
    tbl.add_column(t("repl.help.col_cmd"), style="cyan bold")
    tbl.add_column(t("repl.help.col_desc"))
    for c in SLASH_COMMANDS:
        name = f"{c.name} {c.arg_hint}".strip()
        tbl.add_row(name, t(c.desc_key))
    console.print(tbl)
    console.print(f"[dim]{t('repl.help.footer', lang=current_lang(), provider=current['provider'], model=escape(_short(current['model'])))}[/dim]")


def _live_status() -> None:
    """Auto-refreshing status panel; Ctrl+C returns to the prompt."""
    if not _interactive():
        console.print(status_layout())
        return
    console.print(f"[dim]{t('repl.status.hint')}[/dim]")
    try:
        with Live(status_layout(), console=console, refresh_per_second=2,
                  screen=False) as live:
            while True:
                time.sleep(1.5)
                live.update(status_layout())
    except KeyboardInterrupt:
        pass


# ---------- turn execution ----------

def _run_turn(
    conv: UnifiedConversation,
    current: dict,
    prompt: str,
    *,
    images: Optional[list] = None,
) -> None:
    status = Status(f"[cyan]{t('repl.turn.waiting')}[/cyan]", console=console, spinner="dots")
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
                    status.update(f"[cyan]{t('repl.turn.tool', name=escape(str(name)))}[/cyan]")
                else:
                    console.print(f"\n[dim][tool: {escape(str(name))}][/dim]")
    except KeyboardInterrupt:
        status.stop()
        print()
        console.print(f"[yellow]{t('repl.turn.cancelled')}[/yellow]")
        return
    except UnifiedError as e:
        status.stop()
        print()
        console.print(f"[red]{escape(str(e))}[/red]")
        return
    finally:
        status.stop()
    if started:
        print()


# ---------- helpers ----------

def _prompt(current: dict) -> str:
    return f"[{current['provider']}/{_short(current['model'])}] > "


def _short(model: str) -> str:
    if model.startswith("claude-"):
        return model.split("-")[1] if "-" in model[7:] else model
    return model


def _banner(current: dict, use_ptk: bool) -> None:
    # The live "/" menu only exists on the prompt_toolkit path.
    hint = t("repl.banner.hint") if use_ptk else t("repl.banner.hint_basic")
    console.print(Panel.fit(
        f"[bold]{t('repl.banner.title')}[/bold]\n"
        f"[dim]{hint}[/dim]\n"
        f"[dim]{t('repl.banner.start', provider=current['provider'], model=escape(current['model']))}[/dim]",
        border_style="cyan",
    ))


def _on_exit(conv: UnifiedConversation, current: dict) -> None:
    sid = conv.sessions.get(current["provider"])
    if sid:
        try:
            save_last_session(
                provider=current["provider"],
                model=current["model"],
                session_id=sid,
            )
            console.print(f"[dim]{t('repl.exit.saved')}[/dim]")
        except OSError:
            pass
    console.print(f"[dim]{t('repl.exit.bye')}[/dim]")
