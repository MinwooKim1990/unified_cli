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
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel

from .core import ProviderName
from .errors import UnifiedError
from .factory import create, route
from .models import DEFAULT_MODELS, list_models
from .onboarding import run_setup
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

    console.print(banner("unified-cli doctor"))
    console.print(status_table(states))

    # Suggestions
    needs_setup = [s for s in states if s.health != "ok"]
    if needs_setup:
        console.print()
        console.print(
            "[yellow]⚠ setup 이 필요한 provider 가 있습니다. "
            "`unified-cli setup` 을 실행하세요.[/yellow]"
        )
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

def _recent_table(limit: int = 10) -> Table:
    t = Table(title=f"Recent calls (last {limit})", show_lines=False,
              header_style="bold magenta")
    t.add_column("time", style="dim")
    t.add_column("provider")
    t.add_column("model")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("latency", justify="right")
    t.add_column("prompt", style="dim")
    t.add_column("error", style="red")
    for r in tracker.recent(limit):
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(r.ts)),
            r.provider,
            r.model,
            str(r.input_tokens),
            str(r.output_tokens),
            f"{r.latency_ms} ms",
            r.prompt_preview[:40],
            r.error_kind or "",
        )
    return t


def _aggregate_table() -> Table:
    t = Table(title="Usage totals (this process)", show_lines=False,
              header_style="bold green")
    t.add_column("provider", style="bold")
    t.add_column("calls", justify="right")
    t.add_column("errors", justify="right", style="red")
    t.add_column("tokens in/out", justify="right")
    t.add_column("cached", justify="right", style="dim")
    t.add_column("avg latency", justify="right")
    for a in tracker.aggregates():
        t.add_row(
            a.provider,
            str(a.calls),
            str(a.errors),
            f"{a.input_tokens}/{a.output_tokens}",
            str(a.cached_tokens),
            f"{a.avg_latency_ms:.0f} ms",
        )
    if not tracker.aggregates():
        t.add_row("-", "0", "0", "-", "-", "-")
    return t


def _status_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="head", size=1),
        Layout(name="providers", ratio=2),
        Layout(name="agg", ratio=2),
        Layout(name="recent", ratio=3),
    )
    layout["head"].update(Panel("unified-cli status", border_style="cyan",
                                style="bold", padding=(0, 1)))
    layout["providers"].update(status_table(collect_states(), title="Providers"))
    layout["agg"].update(_aggregate_table())
    layout["recent"].update(_recent_table(10))
    return layout


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
        console.print("\n[dim]종료[/dim]")
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
    t = Table(title="Available models", show_lines=False, header_style="bold cyan")
    t.add_column("provider", style="bold")
    t.add_column("id")
    t.add_column("display")
    t.add_column("default", justify="center")
    t.add_column("source", style="dim")
    for m in mods:
        t.add_row(
            m.provider, m.id, m.display_name or "-",
            "✓" if m.default else "", m.source,
        )
    console.print(t)
    return 0


# ----- chat -----

def _cmd_chat(args: argparse.Namespace) -> int:
    provider, model = (None, args.model)
    if args.model:
        try:
            provider, model = route(args.model)
        except UnifiedError as e:
            console.print(f"[red]모델 라우팅 실패:[/red] {e}")
            return 2

    try:
        client = create(
            provider or "claude",
            model=model,
            web_search=args.web_search,
            cwd=args.cwd,
        )
    except UnifiedError as e:
        console.print(f"[red]{e}[/red]")
        return 3

    prompt = args.prompt or sys.stdin.read()

    try:
        if args.stream:
            for msg in client.stream(prompt):
                if msg.kind == "text" and msg.text:
                    print(msg.text, end="", flush=True)
                elif msg.kind == "tool_use":
                    name = (msg.tool or {}).get("name")
                    console.print(f"\n[dim][tool_use: {name}][/dim]")
            print()
        else:
            resp = client.chat(prompt)
            print(resp.text)
            console.print(
                f"\n[dim]provider={resp.provider}  model={resp.model}  "
                f"session_id={resp.session_id}  "
                f"in/out={resp.usage.input_tokens}/{resp.usage.output_tokens}[/dim]",
                highlight=False,
            )
    except UnifiedError as e:
        console.print(f"[red]{e}[/red]")
        return 4
    return 0


# ----- entrypoint -----

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unified-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="check binaries, auth, and models")
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=_cmd_doctor)

    p_setup = sub.add_parser("setup", help="interactive onboarding wizard")
    p_setup.add_argument("--provider", choices=["claude", "codex", "gemini"])
    p_setup.add_argument("--skip-install", action="store_true")
    p_setup.add_argument("--skip-verify", action="store_true")
    p_setup.set_defaults(func=_cmd_setup)

    p_stat = sub.add_parser("status", help="live status dashboard in terminal")
    p_stat.add_argument("--watch", action="store_true",
                        help="refresh every --watch-interval seconds")
    p_stat.add_argument("--watch-interval", default=5,
                        help="refresh interval in seconds (default 5)")
    p_stat.add_argument("--json", action="store_true")
    p_stat.set_defaults(func=_cmd_status)

    p_mod = sub.add_parser("models", help="list available models")
    p_mod.add_argument("provider", nargs="?", choices=["claude", "codex", "gemini"])
    p_mod.add_argument("--refresh", action="store_true")
    p_mod.add_argument("--json", action="store_true")
    p_mod.set_defaults(func=_cmd_models)

    p_chat = sub.add_parser("chat", help="single-turn chat")
    p_chat.add_argument("prompt", nargs="?", help="prompt (or stdin)")
    p_chat.add_argument("-m", "--model", help="provider/model or model name")
    p_chat.add_argument("--stream", action="store_true")
    p_chat.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True)
    p_chat.add_argument("--cwd")
    p_chat.set_defaults(func=_cmd_chat)

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
