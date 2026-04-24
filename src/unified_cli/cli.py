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
            console.print("[yellow]⚠ 저장된 세션 없음 — 새 대화로 시작.[/yellow]")
            return routed_provider, routed_model, None
        # If user didn't override -m, reuse saved (provider, model).
        if routed_provider is None and routed_model is None:
            return saved.provider, saved.model, saved.session_id
        # If user overrode to a different provider, the saved session_id is invalid.
        if routed_provider and routed_provider != saved.provider:
            console.print(
                f"[yellow]⚠ --continue 는 이전 provider ({saved.provider}) 전용, "
                f"-m 로 {routed_provider} 지정 — 새 대화로 시작.[/yellow]"
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
    lines = [
        f"[bold]provider[/bold]={resp.provider} · "
        f"[bold]model[/bold]={resp.model}",
        f"[bold]session_id[/bold]={sid} [dim](저장됨)[/dim]",
        f"[dim]이어쓰기:[/dim] [cyan]unified-cli chat \"...\" --continue[/cyan]",
        f"[dim]     또는:[/dim] [cyan]unified-cli chat \"...\" --resume {sid_short}[/cyan]",
        f"[dim]tokens in/out={resp.usage.input_tokens}/{resp.usage.output_tokens}  "
        f"latency={latency_ms} ms[/dim]",
    ]
    console.print(Panel("\n".join(lines), title="session", border_style="cyan",
                        expand=False, padding=(0, 1)))


def _cmd_chat(args: argparse.Namespace) -> int:
    import time as _t

    # Route -m flag first.
    provider, model = (None, args.model)
    if args.model:
        try:
            provider, model = route(args.model)
        except UnifiedError as e:
            console.print(f"[red]모델 라우팅 실패:[/red] {e}")
            return 2

    # Resolve session flags (may override provider/model from state file).
    try:
        provider, model, session_id = _resolve_session_flags(args, provider, model)
    except UnifiedError as e:
        console.print(f"[red]{e}[/red]")
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

    t0 = _t.time()
    try:
        if args.stream:
            resp_session, resp_model, text_tokens = _run_stream_with_spinner(
                client, prompt, session_id=session_id
            )
            # Build a lightweight Response-like object for the info panel.
            class _R:
                pass
            resp = _R()
            resp.provider = client.name
            resp.model = resp_model or client.model
            resp.session_id = resp_session or ""
            from .core import Usage as _U
            resp.usage = _U(
                input_tokens=text_tokens[0], output_tokens=text_tokens[1],
            )
        else:
            resp = client.chat(prompt, session_id=session_id)
            print(resp.text)
    except UnifiedError as e:
        console.print(f"[red]{e}[/red]")
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
    client, prompt: str, *, session_id: Optional[str] = None
) -> tuple[Optional[str], Optional[str], tuple[Optional[int], Optional[int]]]:
    """Show a rich spinner until the first text event arrives.

    Claude's TTFT is often 3–6 seconds on subscription auth; without a spinner
    users can't tell if the CLI is stuck. Spinner switches to 'tool: X' label
    when tool calls fire before the first text chunk.

    Returns (resolved_session_id, resolved_model, (input_tokens, output_tokens))
    captured from the stream so the caller can render a session panel.
    """
    from rich.status import Status

    status = Status("[cyan]응답 대기 중…[/cyan]", console=console, spinner="dots")
    status.start()
    started = False
    resolved_sid: Optional[str] = None
    resolved_model: Optional[str] = None
    in_tok: Optional[int] = None
    out_tok: Optional[int] = None
    try:
        for msg in client.stream(prompt, session_id=session_id):
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
                    status.update(f"[cyan]도구 사용 중: {name}[/cyan]")
                else:
                    console.print(f"\n[dim][tool_use: {name}][/dim]")
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
    console.print("[bold cyan]unified-cli[/bold cyan] — Claude / Codex / Gemini 통합 CLI 래퍼")
    console.print()
    console.print("처음이면: [bold]unified-cli setup[/bold]  (대화형 온보딩 마법사)")
    console.print("상태 확인: [bold]unified-cli doctor[/bold] · [bold]unified-cli status[/bold]")
    console.print("단발 호출: [bold]unified-cli chat \"안녕\" -m haiku[/bold]")
    console.print("이어쓰기: [bold]unified-cli chat \"...\" --continue[/bold]  "
                  "[dim](마지막 세션)[/dim]")
    console.print("대화 모드: [bold]unified-cli repl[/bold]  "
                  "[dim](슬래시 명령 + provider 교체)[/dim]")
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

    # Session continuity flags (mutually exclusive).
    session_grp = p_chat.add_mutually_exclusive_group()
    session_grp.add_argument("-r", "--resume", metavar="SESSION_ID",
                             help="특정 session_id 이어쓰기")
    session_grp.add_argument("-c", "--continue", dest="continue_",
                             action="store_true",
                             help="마지막 저장된 세션 이어쓰기 (~/.unified-cli/state.json)")
    session_grp.add_argument("--new", action="store_true",
                             help="저장된 세션 무시하고 새 대화 시작 + 상태파일 초기화")

    p_chat.set_defaults(func=_cmd_chat)

    p_repl = sub.add_parser("repl", help="대화형 REPL 모드 (슬래시 명령 /help)")
    p_repl.add_argument("--provider", choices=["claude", "codex", "gemini"],
                        default="claude", help="시작 provider (기본 claude)")
    p_repl.add_argument("-m", "--model",
                        help="시작 모델 (생략 시 provider 기본 모델)")
    p_repl.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True,
                        help="웹서치 도구 비활성화 (기본 ON)")
    p_repl.add_argument("--terse", action="store_true",
                        help="Claude 짧은 응답 모드")
    p_repl.add_argument("--cwd", help="하위 CLI 의 작업 디렉토리")
    p_repl.set_defaults(func=_cmd_repl)

    ns = parser.parse_args(raw)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
