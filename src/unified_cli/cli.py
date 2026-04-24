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
        console.print(f"[red]{e}[/red]")
        return 3

    prompt = args.prompt or sys.stdin.read()

    try:
        if args.stream:
            _run_stream_with_spinner(client, prompt)
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


def _run_stream_with_spinner(client, prompt: str) -> None:
    """Show a rich spinner until the first text event arrives.

    Claude's TTFT is often 3–6 seconds on subscription auth; without a spinner
    users can't tell if the CLI is stuck. Spinner switches to 'tool: X' label
    when tool calls fire before the first text chunk.
    """
    from rich.status import Status

    status = Status("[cyan]응답 대기 중…[/cyan]", console=console, spinner="dots")
    status.start()
    started = False
    try:
        for msg in client.stream(prompt):
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
                    console.print(f"\n[dim][tool_use: {name}][/dim]")
    finally:
        status.stop()
    if started:
        print()


# ----- entrypoint -----

def _print_no_arg_hint() -> None:
    console.print("[bold cyan]unified-cli[/bold cyan] — Claude / Codex / Gemini 통합 CLI 래퍼")
    console.print()
    console.print("처음이면: [bold]unified-cli setup[/bold]  (대화형 온보딩 마법사)")
    console.print("상태 확인: [bold]unified-cli doctor[/bold] · [bold]unified-cli status[/bold]")
    console.print("바로 쓰기: [bold]unified-cli chat \"안녕\" -m haiku[/bold]")
    console.print("모델 목록: [bold]unified-cli models[/bold]")
    console.print()
    console.print("[dim]전체 도움말: unified-cli --help[/dim]")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        _print_no_arg_hint()
        return 0

    parser = argparse.ArgumentParser(
        prog="unified-cli",
        description="3개 AI CLI (claude / codex / gemini) 통합 래퍼. "
                    "첫 실행 시 `unified-cli setup` 을 권장합니다.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="바이너리 · auth · 모델 개수 점검")
    p_doc.add_argument("--json", action="store_true",
                       help="machine-readable JSON 출력 (자동화 스크립트용)")
    p_doc.set_defaults(func=_cmd_doctor)

    p_setup = sub.add_parser("setup", help="대화형 온보딩 마법사 (설치 + 로그인 + 검증)")
    p_setup.add_argument("--provider", choices=["claude", "codex", "gemini"],
                         help="특정 provider 만 진행 (기본: 세 개 모두)")
    p_setup.add_argument("--skip-install", action="store_true",
                         help="설치 단계 건너뛰기")
    p_setup.add_argument("--skip-verify", action="store_true",
                         help="테스트 호출 건너뛰기 (토큰 절약)")
    p_setup.set_defaults(func=_cmd_setup)

    p_stat = sub.add_parser("status", help="사용량 스냅샷 + (옵션) 실시간 대시보드")
    p_stat.add_argument("--watch", action="store_true",
                        help="rich.live 로 주기 갱신 대시보드 (Ctrl+C 로 종료)")
    p_stat.add_argument("--watch-interval", default=5,
                        help="갱신 주기 초 (기본 5)")
    p_stat.add_argument("--json", action="store_true",
                        help="JSON 출력 (자동화 스크립트용)")
    p_stat.set_defaults(func=_cmd_status)

    p_mod = sub.add_parser("models", help="사용 가능한 모델 목록")
    p_mod.add_argument("provider", nargs="?", choices=["claude", "codex", "gemini"],
                       help="provider 필터 (생략 시 전부)")
    p_mod.add_argument("--refresh", action="store_true",
                       help="캐시 무시하고 API 재조회")
    p_mod.add_argument("--json", action="store_true", help="JSON 출력")
    p_mod.set_defaults(func=_cmd_models)

    p_chat = sub.add_parser("chat", help="단일 프롬프트 호출 (stdin 입력도 가능)")
    p_chat.add_argument("prompt", nargs="?",
                        help="프롬프트 텍스트. 생략 시 stdin 에서 읽음")
    p_chat.add_argument("-m", "--model",
                        help="모델명 또는 provider/model (예: haiku, claude/sonnet, gpt-5.4-mini)")
    p_chat.add_argument("--stream", action="store_true",
                        help="토큰 단위 스트리밍 출력 (첫 토큰 대기 중엔 스피너 표시)")
    p_chat.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True,
                        help="웹서치 도구 비활성화 (기본 ON)")
    p_chat.add_argument("--terse", action="store_true",
                        help="Claude 가 짧은 질문에 장황하게 답하는 걸 억제")
    p_chat.add_argument("--cwd",
                        help="하위 CLI 의 작업 디렉토리 (도구 사용 시 영향)")
    p_chat.set_defaults(func=_cmd_chat)

    ns = parser.parse_args(raw)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
