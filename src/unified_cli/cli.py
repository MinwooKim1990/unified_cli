"""Terminal CLI: `unified-cli {doctor,setup,status,chat,models}`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from typing import Optional
from urllib.parse import quote

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from . import i18n, settings
from .core import ProviderId, ProviderName
from .errors import UnifiedError
from .factory import create, route
from .i18n import t
from .models import DEFAULT_MODELS, list_models
# NOTE: `.repl` (pulls prompt_toolkit) and `.onboarding` (pulls rich.progress)
# are imported lazily inside _cmd_repl / _cmd_setup so that fast paths like
# `--version`, `--help`, `doctor`, `chat` don't pay their import cost.
from .state import (
    clear_last_session, load_last_session, resolve_cwd, save_last_session,
)
from .ui import banner, collect_states, health_cell, status_table
from .usage import tracker


console = Console()
# Diagnostics / spinners / panels go to stderr so `unified-cli chat "..." | jq`
# keeps stdout as pure model output. Only `print(resp.text)` / streamed text
# is written to stdout.
err_console = Console(stderr=True)


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
                "has_token_env": s.has_token_env,
                "keychain": s.keychain,
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

    if getattr(args, "headless", False):
        return _doctor_headless(states)

    console.print(banner(t("cli.doctor.title")))
    console.print(status_table(states))

    # Suggestions
    needs_setup = [s for s in states if s.health != "ok"]
    if needs_setup:
        console.print()
        console.print(f"[yellow]{t('cli.doctor.needs_setup')}[/yellow]")
    return 0 if not needs_setup else 1


def _doctor_headless(states) -> int:
    """Real per-provider preflight: make a tiny call with a short timeout and a
    closed stdin, proving THIS process context can actually reach auth.

    Run it *from your service context* (launchd/cron/systemd) — that is where a
    Keychain-blocked `claude` hangs. In an interactive terminal it will usually
    pass, because the terminal can unlock the Keychain.
    """
    from .providers.gemini import gemini_enabled

    console.print(banner(t("cli.doctor.headless.title")))
    console.print(f"[dim]{t('cli.doctor.headless.intro')}[/dim]")
    console.print()
    rc = 0
    for s in states:
        if s.name == "gemini" and not gemini_enabled():
            console.print(f"  [dim]• gemini: {t('cli.doctor.headless.skipped_gate')}[/dim]")
            continue
        if not s.bin_path:
            console.print(f"  [red]✗ {s.name}: {t('cli.doctor.headless.no_binary')}[/red]")
            rc = 1
            continue
        try:
            client = create(s.name, web_search=False, timeout=15)
            resp = client.chat("ping")
            reply = escape((resp.text or "").strip()[:30])
            console.print(f"  [green]✓ {s.name}: {t('cli.doctor.headless.ok')}[/green] "
                          f"[dim]{reply}[/dim]")
        except UnifiedError as e:
            console.print(f"  [red]✗ {s.name}: {e.kind}[/red] — {escape(e.message)}")
            if e.hint:
                console.print(f"     [dim]→ {escape(e.hint)}[/dim]")
            rc = 1
        except Exception as e:  # noqa: BLE001 - preflight must not crash
            console.print(f"  [red]✗ {s.name}: {type(e).__name__}: {escape(str(e))}[/red]")
            rc = 1
    console.print()
    console.print(f"[{'green' if rc == 0 else 'yellow'}]"
                  f"{t('cli.doctor.headless.done_ok') if rc == 0 else t('cli.doctor.headless.done_fail')}"
                  f"[/]")
    return rc


# ----- setup -----

def _cmd_setup(args: argparse.Namespace) -> int:
    from .onboarding import run_setup  # lazy (rich.progress / rich.prompt)
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
    try:
        mods = list_models(args.provider, force_refresh=args.refresh)
    except UnifiedError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        return 2
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


# ----- providers -----

def _cmd_providers(args: argparse.Namespace) -> int:
    # Registry import is intentionally command-local.  Parser construction,
    # --help, and --version therefore cannot trigger entry-point discovery.
    from .registry import list_providers

    try:
        descriptors = list_providers(include_ext=args.include_ext)
    except UnifiedError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        return 2
    if args.json:
        print(json.dumps([
            {
                "id": item.id,
                "source": item.source,
                "status": item.status,
                "default_model": item.default_model,
                "capabilities": sorted(item.capabilities),
                "route_prefixes": list(item.route_prefixes),
                "server_policy": (
                    asdict(item.server_policy) if item.server_policy else None
                ),
                "error": item.error,
            }
            for item in descriptors
        ], ensure_ascii=False, indent=2))
        return 0

    tbl = Table(title=t("cli.providers.title"), show_lines=False,
                header_style="bold cyan")
    tbl.add_column(t("cli.providers.col.id"), style="bold")
    tbl.add_column(t("cli.providers.col.source"))
    tbl.add_column(t("cli.providers.col.status"))
    tbl.add_column(t("cli.providers.col.default"))
    for item in descriptors:
        tbl.add_row(
            escape(item.id), item.source, item.status,
            escape(item.default_model or "-"),
        )
    console.print(tbl)
    return 0


# ----- chat -----

def _resolve_session_flags(
    args: argparse.Namespace,
    routed_provider: Optional[ProviderId],
    routed_model: Optional[str],
) -> tuple[Optional[ProviderId], Optional[str], Optional[str], Optional[str]]:
    """Resolve session flags to (provider, model, session_id, saved_cwd).

    Rules:
      --resume <id>  → use as-is. provider/model from -m (or default).
      --continue     → load state file. If -m overrides to a different provider,
                       warn and start new session (provider/model from -m).
                       If -m is same provider, keep saved session_id; use -m's model.
      --new          → clear state, start fresh.
      (none)         → start fresh (no state read/clear).
    """
    if args.resume:
        return routed_provider, routed_model, args.resume, None

    if getattr(args, "continue_", False):
        saved = load_last_session()
        if saved is None:
            console.print(f"[yellow]{t('cli.chat.no_saved_session')}[/yellow]")
            return routed_provider, routed_model, None, None
        # If user didn't override -m, reuse saved (provider, model).
        if routed_provider is None and routed_model is None:
            return saved.provider, saved.model, saved.session_id, saved.cwd
        # If user overrode to a different provider, the saved session_id is invalid.
        if routed_provider and routed_provider != saved.provider:
            console.print(
                "[yellow]"
                + t("cli.chat.continue_wrong_provider",
                    saved=saved.provider, routed=routed_provider)
                + "[/yellow]"
            )
            return routed_provider, routed_model, None, None
        # Same provider, different model override: keep session_id.
        return saved.provider, routed_model or saved.model, saved.session_id, saved.cwd

    if args.new:
        clear_last_session()
        return routed_provider, routed_model, None, None

    return routed_provider, routed_model, None, None


def _configured_default_provider() -> ProviderName:
    """Read the validated user preference, retaining Claude compatibility."""
    provider = settings.get("default_provider")
    return (provider if isinstance(provider, str)
            and provider in {"claude", "codex", "gemini"} else "claude")


def _effective_cwd(explicit_cwd: Optional[str], saved_cwd: Optional[str]) -> str:
    """Resolve the documented CWD precedence for a CLI chat invocation."""
    if explicit_cwd is not None:
        resolved = resolve_cwd(explicit_cwd)
        if resolved is None:
            raise ValueError(t("cli.chat.invalid_cwd", cwd=explicit_cwd))
        return resolved
    if saved_cwd:
        resolved = resolve_cwd(saved_cwd)
        if resolved is not None:
            return resolved
        err_console.print(
            f"[yellow]{t('cli.chat.saved_cwd_missing', cwd=escape(saved_cwd))}[/yellow]"
        )
    # Passing the effective cwd explicitly keeps the persisted state truthful
    # and has the same subprocess semantics as BaseProvider's inherited cwd.
    return resolve_cwd(os.getcwd()) or os.getcwd()


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
    err_console.print(Panel("\n".join(lines), title=t("cli.panel.title"), border_style="cyan",
                            expand=False, padding=(0, 1)))


def _cmd_chat(args: argparse.Namespace) -> int:
    import time as _t

    # Route -m flag first.
    provider, model = (None, args.model)
    if args.model:
        try:
            provider, model = route(args.model)
        except UnifiedError as e:
            err_console.print(f"[red]{t('cli.chat.route_failed')}[/red] {escape(str(e))}")
            return 2

    # Resolve session flags (may override provider/model from state file).
    try:
        provider, model, session_id, saved_cwd = _resolve_session_flags(
            args, provider, model
        )
    except UnifiedError as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        return 2

    try:
        effective_cwd = _effective_cwd(args.cwd, saved_cwd)
    except ValueError as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        return 2

    provider = provider or _configured_default_provider()

    create_kwargs: dict = {
        "model": model,
        "web_search": args.web_search,
        "cwd": effective_cwd,
    }
    # --terse is Claude-specific (others already default to concise replies).
    if args.terse and provider == "claude":
        create_kwargs["terse"] = True

    try:
        client = create(provider, **create_kwargs)
    except UnifiedError as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        return 3

    # Prompt from arg, or piped stdin. If neither (interactive TTY with no
    # prompt), don't block forever on stdin.read() — show usage and exit.
    if args.prompt is not None:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        err_console.print(f"[yellow]{t('cli.chat.need_prompt')}[/yellow]")
        return 2

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
            # Spinner on stderr while the (blocking) call runs; payload to stdout.
            with err_console.status(f"[cyan]{t('cli.chat.waiting')}[/cyan]", spinner="dots"):
                resp = client.chat(prompt, session_id=session_id, images=images)
            print(resp.text)
    except UnifiedError as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        return 4

    latency_ms = int((_t.time() - t0) * 1000)

    # Save last session unless user was resuming something that doesn't match.
    if resp.session_id:
        try:
            save_last_session(
                provider=resp.provider,  # type: ignore[arg-type]
                model=resp.model or client.model,
                session_id=resp.session_id,
                cwd=effective_cwd,
            )
        except OSError:
            pass  # state save is best-effort

    # Session metadata is diagnostics — only render it when stderr is a terminal,
    # so `... | jq` (stdout piped) or a fully-captured run stays clean.
    if sys.stderr.isatty():
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

    status = Status(f"[cyan]{t('cli.chat.waiting')}[/cyan]", console=err_console, spinner="dots")
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
                    err_console.print(f"\n[dim][tool_use: {escape(str(name))}][/dim]")
    finally:
        status.stop()
    if started:
        print()
    return resolved_sid, resolved_model, (in_tok, out_tok)


# ----- repl -----

def _cmd_repl(args: argparse.Namespace) -> int:
    from .repl import run_repl  # lazy (prompt_toolkit)
    return run_repl(
        provider=args.provider or _configured_default_provider(),
        model=args.model,
        web_search=args.web_search,
        terse=args.terse,
        cwd=args.cwd,
        continue_session=getattr(args, "continue_", False),
    )


# ----- config -----

def _cmd_config(args: argparse.Namespace) -> int:
    """Read or update durable, non-secret CLI preferences."""
    if args.config_cmd != "default-provider":  # pragma: no cover - parser owns this
        return 2
    if args.reset and args.provider:
        err_console.print(f"[red]{t('cli.config.default_provider.conflict')}[/red]")
        return 2
    try:
        if args.reset:
            settings.set("default_provider", None)
            console.print(t("cli.config.default_provider.reset"))
        elif args.provider:
            settings.set("default_provider", args.provider)
            console.print(t("cli.config.default_provider.set", provider=args.provider))
        else:
            provider = settings.get("default_provider")
            console.print(
                t("cli.config.default_provider.current", provider=provider or "claude")
            )
    except (OSError, ValueError) as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        return 2
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Launch the localhost dashboard + OpenAI-compatible server."""
    try:
        from .server import run  # lazy (fastapi/uvicorn — optional extra)
        if getattr(args, "manage", False):
            from .server import prepare_manage
    except ImportError:
        console.print(f"[red]{t('cli.serve.missing_deps')}[/red]")
        console.print(t("cli.serve.install_hint"))
        return 3

    manage = getattr(args, "manage", False)
    workspaces = tuple(getattr(args, "workspace", ()) or ())
    if workspaces and not manage:
        err_console.print(f"[red]{t('cli.serve.workspace_requires_manage')}[/red]")
        return 2

    url = f"http://127.0.0.1:{args.port}/dashboard"
    if manage:
        try:
            token = prepare_manage(workspaces)
        except UnifiedError as e:
            err_console.print(f"[red]{escape(str(e))}[/red]")
            return 2
        except (OSError, TypeError, ValueError):
            # The backend validates paths as its final authority.  Keep a
            # malformed workspace from becoming a CLI traceback if a backend
            # implementation raises a built-in validation error instead.
            err_console.print(f"[red]{t('cli.serve.invalid_workspace')}[/red]")
            return 2
        url = f"{url}#bootstrap={quote(token, safe='')}"

    console.print(t("cli.serve.starting", url=url))
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - opening a browser is best-effort
            pass
    try:
        if manage:
            run(host="127.0.0.1", port=args.port,
                manage=True, workspaces=workspaces)
        else:
            run(host="127.0.0.1", port=args.port)
    except UnifiedError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 2
    except OSError:
        # `run()` failures occur after workspace preparation; report them as
        # bind/start failures instead of incorrectly blaming a workspace.
        err_console.print(f"[red]{t('cli.serve.server_start_failed')}[/red]")
        return 2
    return 0


# ----- entrypoint -----

def _print_no_arg_hint() -> None:
    console.print(f"[bold cyan]unified-cli[/bold cyan] — {t('cli.tagline')}")
    console.print()
    console.print(t("cli.hint.first_time"))
    console.print(t("cli.hint.status"))
    console.print(t("cli.hint.oneshot"))
    console.print(t("cli.hint.continue"))
    console.print(t("cli.hint.repl"))
    console.print(t("cli.hint.serve"))
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

    # Keep version probing automation-friendly: no parser construction, Rich
    # rendering, localization output, or provider discovery on this fast path.
    if raw in (["--version"], ["-V"]):
        from . import __version__
        print(__version__)
        return 0

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
    p_doc.add_argument("--headless", action="store_true",
                       help=t("cli.help.doctor.headless"))
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
    p_mod.add_argument("provider", nargs="?",
                       help=t("cli.help.models.provider"))
    p_mod.add_argument("--refresh", action="store_true",
                       help=t("cli.help.models.refresh"))
    p_mod.add_argument("--json", action="store_true", help=t("cli.help.models.json"))
    p_mod.set_defaults(func=_cmd_models)

    p_providers = _add("providers", help=t("cli.help.providers"))
    p_providers.add_argument(
        "--include-ext", action="store_true",
        help=t("cli.help.providers.include_ext"),
    )
    p_providers.add_argument(
        "--json", action="store_true", help=t("cli.help.providers.json"),
    )
    p_providers.set_defaults(func=_cmd_providers)

    p_config = _add("config", help=t("cli.help.config"))
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)
    p_default_provider = config_sub.add_parser(
        "default-provider", parents=[lang_parent],
        help=t("cli.help.config.default_provider"),
    )
    p_default_provider.add_argument(
        "provider", nargs="?", choices=["claude", "codex", "gemini"],
        help=t("cli.help.config.default_provider.provider"),
    )
    p_default_provider.add_argument(
        "--reset", action="store_true",
        help=t("cli.help.config.default_provider.reset"),
    )
    p_default_provider.set_defaults(func=_cmd_config)

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
                        default=None, help=t("cli.help.repl.provider"))
    p_repl.add_argument("-m", "--model",
                        help=t("cli.help.repl.model"))
    p_repl.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True,
                        help=t("cli.help.repl.no_web_search"))
    p_repl.add_argument("--terse", action="store_true",
                        help=t("cli.help.repl.terse"))
    p_repl.add_argument("--cwd", help=t("cli.help.repl.cwd"))
    p_repl.add_argument("-c", "--continue", dest="continue_", action="store_true",
                        help=t("cli.help.repl.continue"))
    p_repl.set_defaults(func=_cmd_repl)

    p_serve = _add("serve", help=t("cli.help.serve"))
    p_serve.add_argument("--port", type=int, default=8000,
                         help=t("cli.help.serve.port"))
    p_serve.add_argument("--open", action="store_true",
                         help=t("cli.help.serve.open"))
    p_serve.add_argument("--manage", action="store_true",
                         help=t("cli.help.serve.manage"))
    p_serve.add_argument("--workspace", action="append", default=[], metavar="PATH",
                         help=t("cli.help.serve.workspace"))
    p_serve.set_defaults(func=_cmd_serve)

    ns = parser.parse_args(raw)
    if ns.cmd == "serve" and ns.workspace and not ns.manage:
        parser.error(t("cli.serve.workspace_requires_manage"))
    # Apply --lang from the parsed namespace too (covers `--lang=ko` placed
    # after the subcommand, which the prescan above still catches, plus keeps
    # the override authoritative for the command body).
    if getattr(ns, "lang", None):
        i18n.set_lang(ns.lang)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
