"""Terminal CLI: `unified-cli {doctor,setup,status,chat,models}`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from . import i18n
from .core import ProviderName
from .errors import UnifiedError
from .factory import create, route
from .i18n import t
from .models import DEFAULT_MODELS, list_models
from .onboarding import run_setup
from .repl import run_repl
from .state import (
    clear_last_session, load_last_session, save_last_session,
)
from .ui import banner, collect_states, health_cell, status_table
from .usage import tracker


console = Console()


# ----- doctor -----

def _cmd_doctor(args: argparse.Namespace) -> int:
    states = collect_states()

    if args.json:
        payload = [
            {
                "provider": s.name,
                "bin_path": s.bin_path,
                "has_oauth": s.has_oauth,
                "has_api_key": s.has_api_key,
                "api_key_env": s.api_key_env,
                "model_count": s.model_count,
                "model_source": s.model_source,
                "default_model": s.default_model,
                "health": s.health,
            }
            for s in states
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console.print(banner(t("cli.doctor.title")))
    console.print(status_table(states))

    # Suggestions
    needs_setup = [s for s in states if s.health != "ok"]
    if needs_setup:
        console.print()
        console.print(f"[yellow]{t('cli.doctor.needs_setup')}[/yellow]")
    return 0 if not needs_setup else 1


# ----- setup -----

def _cmd_setup(args: argparse.Namespace) -> int:
    providers: Optional[list[ProviderName]] = None
    if args.provider:
        providers = [args.provider]
    return run_setup(
        providers=providers,
        skip_install=args.skip_install,
        skip_verify=args.skip_verify,
    )


# ----- status -----

# Status-layout helpers were relocated to ui.py so the REPL can reuse them
# without importing cli.py. Re-exported here under their old private names.
from .ui import status_layout as _status_layout  # noqa: E402


def _cmd_status(args: argparse.Namespace) -> int:
    if args.json:
        payload = {
            "providers": [
                {
                    "provider": s.name,
                    "health": s.health,
                    "bin_path": s.bin_path,
                    "has_oauth": s.has_oauth,
                    "has_api_key": s.has_api_key,
                    "model_count": s.model_count,
                    "default_model": s.default_model,
                }
                for s in collect_states()
            ],
            "usage": tracker.snapshot(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if not args.watch:
        console.print(_status_layout())
        return 0

    interval = max(1.0, float(args.watch_interval))
    try:
        with Live(_status_layout(), console=console, refresh_per_second=2,
                  screen=False) as live:
            while True:
                time.sleep(interval)
                live.update(_status_layout())
    except KeyboardInterrupt:
        console.print(f"\n[dim]{t('cli.status.stopped')}[/dim]")
        return 0


# ----- models -----

def _cmd_models(args: argparse.Namespace) -> int:
    mods = list_models(args.provider, force_refresh=args.refresh)
    if args.json:
        print(json.dumps(
            [{"id": m.id, "provider": m.provider, "display_name": m.display_name,
              "default": m.default, "source": m.source} for m in mods],
            ensure_ascii=False, indent=2,
        ))
        return 0
    tbl = Table(title=t("cli.models.title"), show_lines=False, header_style="bold cyan")
    tbl.add_column(t("cli.models.col.provider"), style="bold")
    tbl.add_column(t("cli.models.col.id"))
    tbl.add_column(t("cli.models.col.display"))
    tbl.add_column(t("cli.models.col.default"), justify="center")
    tbl.add_column(t("cli.models.col.source"), style="dim")
    for m in mods:
        tbl.add_row(
            m.provider, escape(m.id), escape(m.display_name or "-"),
            "✓" if m.default else "", m.source,
        )
    console.print(tbl)
    return 0


# ----- chat -----

def _resolve_session_flags(
    args: argparse.Namespace,
    routed_provider: Optional[ProviderName],
    routed_model: Optional[str],
) -> tuple[Optional[ProviderName], Optional[str], Optional[str]]:
    """Inspect --resume/--continue/--new and return (provider, model, session_id).

    Rules:
      --resume <id>  → use as-is. provider/model from -m (or default).
      --continue     → load state file. If -m overrides to a different provider,
                       warn and start new session (provider/model from -m).
                       If -m is same provider, keep saved session_id; use -m's model.
      --new          → clear state, start fresh.
      (none)         → start fresh (no state read/clear).
    """
    if args.resume:
        return routed_provider, routed_model, args.resume

    if getattr(args, "continue_", False):
        saved = load_last_session()
        if saved is None:
            console.print(f"[yellow]{t('cli.chat.no_saved_session')}[/yellow]")
            return routed_provider, routed_model, None
        # If user didn't override -m, reuse saved (provider, model).
        if routed_provider is None and routed_model is None:
            return saved.provider, saved.model, saved.session_id
        # If user overrode to a different provider, the saved session_id is invalid.
        if routed_provider and routed_provider != saved.provider:
            console.print(
                "[yellow]"
                + t("cli.chat.continue_wrong_provider",
                    saved=saved.provider, routed=routed_provider)
                + "[/yellow]"
            )
            return routed_provider, routed_model, None
        # Same provider, different model override: keep session_id.
        return saved.provider, routed_model or saved.model, saved.session_id

    if args.new:
        clear_last_session()
        return routed_provider, routed_model, None

    return routed_provider, routed_model, None


def _print_session_panel(
    resp, resumed_from: Optional[str], latency_ms: int
) -> None:
    """Info panel after a successful chat turn — session_id + resume hint."""
    from rich.panel import Panel
    sid = resp.session_id or "(none)"
    sid_short = sid[:12] + "…" if sid and len(sid) > 12 else sid
    sid, sid_short = escape(sid), escape(sid_short)
    model = escape(str(resp.model))
    lines = [
        f"[bold]provider[/bold]={resp.provider} · "
        f"[bold]model[/bold]={model}",
        f"[bold]session_id[/bold]={sid} [dim]{t('cli.panel.saved')}[/dim]",
        f"[dim]{t('cli.panel.resume')}[/dim] [cyan]unified-cli chat \"...\" --continue[/cyan]",
        f"[dim]{t('cli.panel.or')}[/dim] [cyan]unified-cli chat \"...\" --resume {sid_short}[/cyan]",
        f"[dim]tokens in/out={resp.usage.input_tokens}/{resp.usage.output_tokens}  "
        f"latency={latency_ms} ms[/dim]",
    ]
    console.print(Panel("\n".join(lines), title=t("cli.panel.title"), border_style="cyan",
                        expand=False, padding=(0, 1)))


def _cmd_chat(args: argparse.Namespace) -> int:
    import time as _t

    # Route -m flag first.
    provider, model = (None, args.model)
    if args.model:
        try:
            provider, model = route(args.model)
        except UnifiedError as e:
            console.print(f"[red]{t('cli.chat.route_failed')}[/red] {escape(str(e))}")
            return 2

    # Resolve session flags (may override provider/model from state file).
    try:
        provider, model, session_id = _resolve_session_flags(args, provider, model)
    except UnifiedError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 2

    create_kwargs: dict = {
        "model": model,
        "web_search": args.web_search,
        "cwd": args.cwd,
    }
    # --terse is Claude-specific (others already default to concise replies).
    if args.terse and (provider or "claude") == "claude":
        create_kwargs["terse"] = True

    try:
        client = create(provider or "claude", **create_kwargs)
    except UnifiedError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 3

    prompt = args.prompt or sys.stdin.read()

    images = getattr(args, "images", None) or None
    t0 = _t.time()
    try:
        if args.stream:
            resp_session, resp_model, text_tokens = _run_stream_with_spinner(
                client, prompt, session_id=session_id, images=images,
            )
            # Build a lightweight Response-like object for the info panel.
            class _R:
                pass
            resp = _R()
            resp.provider = client.name
            resp.model = resp_model or client.model
            resp.session_id = resp_session or ""
            from .core import Usage as _U
            # Coerce missing counts to 0 — providers like agy report no usage,
            # otherwise the session panel would render "tokens in/out=None/None".
            resp.usage = _U(
                input_tokens=text_tokens[0] or 0, output_tokens=text_tokens[1] or 0,
            )
        else:
            resp = client.chat(prompt, session_id=session_id, images=images)
            print(resp.text)
    except UnifiedError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 4

    latency_ms = int((_t.time() - t0) * 1000)

    # Save last session unless user was resuming something that doesn't match.
    if resp.session_id:
        try:
            save_last_session(
                provider=resp.provider,  # type: ignore[arg-type]
                model=resp.model or client.model,
                session_id=resp.session_id,
            )
        except OSError:
            pass  # state save is best-effort

    _print_session_panel(resp, resumed_from=session_id, latency_ms=latency_ms)
    return 0


def _run_stream_with_spinner(
    client, prompt: str, *,
    session_id: Optional[str] = None,
    images: Optional[list] = None,
) -> tuple[Optional[str], Optional[str], tuple[Optional[int], Optional[int]]]:
    """Show a rich spinner until the first text event arrives.

    Claude's TTFT is often 3–6 seconds on subscription auth; without a spinner
    users can't tell if the CLI is stuck. Spinner switches to 'tool: X' label
    when tool calls fire before the first text chunk.

    Returns (resolved_session_id, resolved_model, (input_tokens, output_tokens))
    captured from the stream so the caller can render a session panel.
    """
    from rich.status import Status

    status = Status(f"[cyan]{t('cli.chat.waiting')}[/cyan]", console=console, spinner="dots")
    status.start()
    started = False
    resolved_sid: Optional[str] = None
    resolved_model: Optional[str] = None
    in_tok: Optional[int] = None
    out_tok: Optional[int] = None
    try:
        for msg in client.stream(prompt, session_id=session_id, images=images):
            if msg.kind == "session" and msg.session_id:
                resolved_sid = msg.session_id
                init_model = (msg.raw or {}).get("model")
                if init_model:
                    resolved_model = init_model
            if msg.kind == "usage" and msg.usage:
                in_tok = msg.usage.input_tokens
                out_tok = msg.usage.output_tokens
            if msg.kind == "text" and msg.text:
                if not started:
                    status.stop()
                    started = True
                print(msg.text, end="", flush=True)
            elif msg.kind == "tool_use":
                name = (msg.tool or {}).get("name")
                if not started:
                    status.update(f"[cyan]{t('cli.chat.using_tool', name=escape(str(name)))}[/cyan]")
                else:
                    console.print(f"\n[dim][tool_use: {escape(str(name))}][/dim]")
    finally:
        status.stop()
    if started:
        print()
    return resolved_sid, resolved_model, (in_tok, out_tok)


# ----- repl -----

def _cmd_repl(args: argparse.Namespace) -> int:
    return run_repl(
        provider=args.provider,
        model=args.model,
        web_search=args.web_search,
        terse=args.terse,
        cwd=args.cwd,
    )


# ----- entrypoint -----

def _print_no_arg_hint() -> None:
    console.print(f"[bold cyan]unified-cli[/bold cyan] — {t('cli.tagline')}")
    console.print()
    console.print(t("cli.hint.first_time"))
    console.print(t("cli.hint.status"))
    console.print(t("cli.hint.oneshot"))
    console.print(t("cli.hint.continue"))
    console.print(t("cli.hint.repl"))
    console.print(t("cli.hint.models"))
    console.print()
    console.print(t("cli.hint.full_help"))


def _prescan_lang(raw: list[str]) -> None:
    """Resolve the UI language BEFORE the argparse parser is built.

    argparse `help=`/`description=` text is localized when the parser is
    constructed, so the language must be set first. We honor `--lang <code>`
    / `--lang=<code>` from argv if present (highest priority), otherwise fall
    back to i18n's normal resolution (settings.json → $UNIFIED_CLI_LANG → en).
    Invalid/unknown values are ignored here — the real parser reports them.
    """
    code: Optional[str] = None
    for i, tok in enumerate(raw):
        if tok == "--lang" and i + 1 < len(raw):
            code = raw[i + 1]
            break
        if tok.startswith("--lang="):
            code = tok.split("=", 1)[1]
            break
    if code:
        try:
            i18n.set_lang(code)
        except ValueError:
            pass  # let the parser surface the invalid-choice error


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # Resolve language early so all localized help/description text is correct.
    _prescan_lang(raw)

    if not raw:
        _print_no_arg_hint()
        return 0

    # `--lang` lives on a shared parent parser so it's accepted both before the
    # subcommand (`unified-cli --lang ko doctor`) and after it
    # (`unified-cli doctor --lang ko`). The actual language was already resolved
    # by _prescan_lang() above; this keeps argparse from rejecting it as unknown
    # and lists it in every --help.
    lang_parent = argparse.ArgumentParser(add_help=False)
    lang_parent.add_argument("--lang", choices=["en", "ko"], help=t("cli.help.lang"))

    parser = argparse.ArgumentParser(
        prog="unified-cli",
        description=t("cli.app.desc"),
        parents=[lang_parent],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add(name: str, **kw):
        return sub.add_parser(name, parents=[lang_parent], **kw)

    p_doc = _add("doctor", help=t("cli.help.doctor"))
    p_doc.add_argument("--json", action="store_true",
                       help=t("cli.help.doctor.json"))
    p_doc.set_defaults(func=_cmd_doctor)

    p_setup = _add("setup", help=t("cli.help.setup"))
    p_setup.add_argument("--provider", choices=["claude", "codex", "gemini"],
                         help=t("cli.help.setup.provider"))
    p_setup.add_argument("--skip-install", action="store_true",
                         help=t("cli.help.setup.skip_install"))
    p_setup.add_argument("--skip-verify", action="store_true",
                         help=t("cli.help.setup.skip_verify"))
    p_setup.set_defaults(func=_cmd_setup)

    p_stat = _add("status", help=t("cli.help.status"))
    p_stat.add_argument("--watch", action="store_true",
                        help=t("cli.help.status.watch"))
    p_stat.add_argument("--watch-interval", type=float, default=5,
                        help=t("cli.help.status.watch_interval"))
    p_stat.add_argument("--json", action="store_true",
                        help=t("cli.help.status.json"))
    p_stat.set_defaults(func=_cmd_status)

    p_mod = _add("models", help=t("cli.help.models"))
    p_mod.add_argument("provider", nargs="?", choices=["claude", "codex", "gemini"],
                       help=t("cli.help.models.provider"))
    p_mod.add_argument("--refresh", action="store_true",
                       help=t("cli.help.models.refresh"))
    p_mod.add_argument("--json", action="store_true", help=t("cli.help.models.json"))
    p_mod.set_defaults(func=_cmd_models)

    p_chat = _add("chat", help=t("cli.help.chat"))
    p_chat.add_argument("prompt", nargs="?",
                        help=t("cli.help.chat.prompt"))
    p_chat.add_argument("-m", "--model",
                        help=t("cli.help.chat.model"))
    p_chat.add_argument("--stream", action="store_true",
                        help=t("cli.help.chat.stream"))
    p_chat.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True,
                        help=t("cli.help.chat.no_web_search"))
    p_chat.add_argument("--terse", action="store_true",
                        help=t("cli.help.chat.terse"))
    p_chat.add_argument("--cwd",
                        help=t("cli.help.chat.cwd"))
    p_chat.add_argument("--image", action="append", dest="images",
                        metavar="PATH",
                        help=t("cli.help.chat.image"))

    # Session continuity flags (mutually exclusive).
    session_grp = p_chat.add_mutually_exclusive_group()
    session_grp.add_argument("-r", "--resume", metavar="SESSION_ID",
                             help=t("cli.help.chat.resume"))
    session_grp.add_argument("-c", "--continue", dest="continue_",
                             action="store_true",
                             help=t("cli.help.chat.continue"))
    session_grp.add_argument("--new", action="store_true",
                             help=t("cli.help.chat.new"))

    p_chat.set_defaults(func=_cmd_chat)

    p_repl = _add("repl", help=t("cli.help.repl"))
    p_repl.add_argument("--provider", choices=["claude", "codex", "gemini"],
                        default="claude", help=t("cli.help.repl.provider"))
    p_repl.add_argument("-m", "--model",
                        help=t("cli.help.repl.model"))
    p_repl.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True,
                        help=t("cli.help.repl.no_web_search"))
    p_repl.add_argument("--terse", action="store_true",
                        help=t("cli.help.repl.terse"))
    p_repl.add_argument("--cwd", help=t("cli.help.repl.cwd"))
    p_repl.set_defaults(func=_cmd_repl)

    ns = parser.parse_args(raw)
    # Apply --lang from the parsed namespace too (covers `--lang=ko` placed
    # after the subcommand, which the prescan above still catches, plus keeps
    # the override authoritative for the command body).
    if getattr(ns, "lang", None):
        i18n.set_lang(ns.lang)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
