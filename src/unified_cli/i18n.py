"""Tiny dict-based i18n for the CLI/REPL. No external lib, no .po files.

Usage:
    from .i18n import t
    print(t("repl.exit.bye"))
    print(t("repl.model.changed", model="claude-opus-4-7"))

Language resolution (first hit wins):
    1. process override via set_lang()  (the --lang flag / /lang command)
    2. settings.json `lang`
    3. $UNIFIED_CLI_LANG
    4. default "en"

`t()` is total: an unknown key returns the key; a bad/missing format arg
returns the raw template. It never raises — a translation gap must never
crash the CLI.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "ko")

_ACTIVE: Optional[str] = None  # set by set_lang(); takes precedence over all


def set_lang(code: Optional[str]) -> None:
    """Force the active language for this process (used by --lang / /lang)."""
    global _ACTIVE
    if code is None:
        _ACTIVE = None
        return
    code = code.strip().lower()
    if code not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported language: {code!r} (supported: {', '.join(SUPPORTED_LANGS)})")
    _ACTIVE = code


def detect_lang() -> str:
    """Resolve the active language (see module docstring for order)."""
    if _ACTIVE in SUPPORTED_LANGS:
        return _ACTIVE  # type: ignore[return-value]
    # settings.json (imported lazily to avoid a cycle and keep i18n importable
    # before settings exists)
    try:
        from .settings import get as _get
        val = _get("lang")
        if isinstance(val, str) and val.strip().lower() in SUPPORTED_LANGS:
            return val.strip().lower()
    except Exception:
        pass
    env = (os.environ.get("UNIFIED_CLI_LANG") or "").strip().lower()
    if env in SUPPORTED_LANGS:
        return env
    return DEFAULT_LANG


def current_lang() -> str:
    return detect_lang()


def t(key: str, **kw) -> str:
    """Translate `key` for the active language, then `.format(**kw)`."""
    lang = current_lang()
    template = MESSAGES.get(lang, {}).get(key)
    if template is None:
        template = MESSAGES["en"].get(key, key)
    if not kw:
        return template
    try:
        return template.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template


# ---------------------------------------------------------------------------
# Message tables. Keep `en` complete; `ko` may omit keys (falls back to en).
# Keys are namespaced by area. Every ko key MUST exist in en (enforced by test).
# ---------------------------------------------------------------------------

_EN = {
    # REPL banner / prompt
    "repl.banner.title": "unified-cli repl — interactive mode",
    "repl.banner.hint": "slash commands: /help · type / for the menu · exit: /exit or Ctrl+D",
    "repl.banner.hint_basic": "slash commands: /help · exit: /exit or Ctrl+D",
    "repl.banner.start": "start provider: {provider} / model: {model}",
    "repl.interrupt_hint": "Use /exit or press Ctrl+D to quit.",
    # images
    "repl.image.usage": "/image <path>",
    "repl.image.not_found": "file not found: {path}",
    "repl.image.attached": "image attached ({n} pending). Sent with your next message.",
    "repl.images.none": "(no images attached)",
    "repl.images.cleared": "cleared {n} attachment(s).",
    # model / provider
    "repl.model.usage": "usage: /model <name>  (or /model with no arg to pick)",
    "repl.model.changed": "model changed: {model} (same provider)",
    "repl.model.cancelled": "(model unchanged)",
    "repl.model.unknown": "unknown model '{model}' — passing it through anyway.",
    "repl.provider.usage": "/provider <claude|codex|gemini>",
    "repl.provider.switched": "provider: {old} → {new}  (last 8 turns auto-injected next turn)",
    "repl.provider.locked": "provider '{provider}' is locked.",
    # gemini gate
    "repl.gemini.locked": "The agy (Antigravity) provider is disabled by default — automating it risks a Google ToS ban. Set UNIFIED_CLI_ENABLE_GEMINI=1 to enable it at your own risk.",
    # cross-provider conversation (conversation.py)
    "conv.sticky_switch": "cannot switch provider in a sticky conversation: locked to '{locked}'",
    "conv.sticky_switch.hint": "create the Conversation with sticky=False.",
    "conv.ctx.header": "[Prior conversation summary — from another provider]",
    "conv.ctx.user": "User",
    "conv.ctx.assistant": "Assistant",
    # conversation control
    "repl.new.done": "conversation reset.",
    "repl.resume.none": "no saved session to resume.",
    "repl.resume.done": "resumed {provider}/{model} · session {sid}… (age {age}m). "
                        "Your next message continues it.",
    "repl.turn.session_reset": "session was stale — starting a fresh one on your next message.",
    "repl.save.none": "no session to save yet (use /save after the first turn).",
    "repl.save.title": "save",
    "repl.save.body": "session_id={sid}\nresume: unified-cli chat \"...\" --resume {sid}\nor:     unified-cli chat \"...\" --continue  (last saved session)",
    "repl.history.none": "(no history yet)",
    "repl.tokens.none": "(no calls in this process)",
    # status / lang / help
    "repl.status.hint": "live status — Ctrl+C to return to the prompt",
    "repl.lang.usage": "usage: /lang <en|ko>  (current: {lang})",
    "repl.lang.changed": "language: {lang}",
    "repl.lang.unknown": "unknown language '{lang}' (supported: en, ko)",
    "repl.help.footer": "lang: {lang} · {provider}/{model} · type / for the live menu · /model with no arg opens the picker",
    "repl.help.col_cmd": "command",
    "repl.help.col_desc": "description",
    # unknown
    "repl.unknown": "unknown command: {cmd}  — /help",
    "repl.slash_error": "command error: {err}",
    # turn streaming
    "repl.turn.waiting": "waiting for response…",
    "repl.turn.tool": "using tool: {name}",
    "repl.turn.cancelled": "cancelled.",
    "repl.turn.error": "error: {err}",
    # exit
    "repl.exit.saved": "saved: resume next time with unified-cli chat \"...\" --continue",
    "repl.exit.bye": "bye.",
    # model picker (prompt_toolkit dialogs)
    "repl.picker.model_title": "select a model — {provider}",
    "repl.picker.model_text": "↑/↓ to choose, Enter to confirm (★ = default)",
    "repl.picker.provider_title": "select a provider",
    "repl.picker.provider_text": "↑/↓ to choose, Enter to confirm",
    "repl.picker.locked_suffix": " (locked)",
    "repl.picker.default_suffix": " ★",
    # slash command descriptions
    "slash.desc.help": "this list",
    "slash.desc.model": "change model (no arg → picker)",
    "slash.desc.provider": "switch provider (auto context injection)",
    "slash.desc.image": "attach an image to the next prompt (repeatable)",
    "slash.desc.images": "list attached images",
    "slash.desc.clear-images": "clear attached images",
    "slash.desc.new": "reset the conversation (drop context)",
    "slash.desc.resume": "reopen your last saved session",
    "slash.desc.save": "show current session_id + resume command",
    "slash.desc.history": "show the last N (default 10) turns",
    "slash.desc.tokens": "this process's cumulative tokens/calls",
    "slash.desc.doctor": "one-line provider health check",
    "slash.desc.status": "live status panel (Ctrl+C to exit)",
    "slash.desc.lang": "set language (en|ko)",
    "slash.desc.exit": "quit (same as Ctrl+D)",

    # ===== CLI (cli.py) =====
    # top-level / no-arg hint
    "cli.tagline": "unified CLI wrapper for Claude / Codex / Gemini",
    "cli.app.desc": "Unified wrapper around 3 AI CLIs (claude / codex / gemini). "
                    "On first run, `unified-cli setup` is recommended.",
    "cli.hint.first_time": "First time? [bold]unified-cli setup[/bold]  (interactive onboarding wizard)",
    "cli.hint.status": "Check status: [bold]unified-cli doctor[/bold] · [bold]unified-cli status[/bold]",
    "cli.hint.oneshot": "One-shot call: [bold]unified-cli chat \"hi\" -m haiku[/bold]",
    "cli.hint.continue": "Continue: [bold]unified-cli chat \"...\" --continue[/bold]  [dim](last session)[/dim]",
    "cli.hint.repl": "Chat mode: [bold]unified-cli repl[/bold]  [dim](slash commands + provider switch)[/dim]",
    "cli.hint.serve": "Dashboard + API: [bold]unified-cli serve[/bold]  [dim](localhost web UI)[/dim]",
    "cli.hint.models": "List models: [bold]unified-cli models[/bold]",
    "cli.hint.full_help": "[dim]Full help: unified-cli --help[/dim]",
    # global --lang
    "cli.help.lang": "UI language (en|ko)",
    # doctor
    "cli.help.doctor": "Check binaries · auth · model counts",
    "cli.help.doctor.json": "machine-readable JSON output (for automation scripts)",
    "cli.help.doctor.headless": "live auth preflight — run it from your service "
                                "(launchd/cron) context to catch a Keychain hang",
    "cli.doctor.title": "unified-cli doctor",
    "cli.doctor.needs_setup": "⚠ Some providers need setup. Run `unified-cli setup`.",
    "cli.doctor.headless.title": "unified-cli doctor · headless preflight",
    "cli.doctor.headless.intro": "Making a tiny real call per provider (15s timeout, "
                                 "closed stdin). Run this in the SAME context as your "
                                 "service to prove auth works there.",
    "cli.doctor.headless.ok": "auth OK in this context",
    "cli.doctor.headless.no_binary": "CLI binary not found (check PATH / $CLAUDE_CLI_PATH)",
    "cli.doctor.headless.skipped_gate": "skipped (set UNIFIED_CLI_ENABLE_GEMINI to test)",
    "cli.doctor.headless.done_ok": "✓ All providers reachable from this context.",
    "cli.doctor.headless.done_fail": "⚠ Some providers failed — see hints above.",
    # setup
    "cli.help.setup": "Interactive onboarding wizard (install + login + verify)",
    "cli.help.setup.provider": "Run only a specific provider (default: all three)",
    "cli.help.setup.skip_install": "Skip the install step",
    "cli.help.setup.skip_verify": "Skip the test call (saves tokens)",
    # status
    "cli.help.status": "Usage snapshot + (optional) live dashboard",
    "cli.help.status.watch": "Periodically refreshing dashboard via rich.live (Ctrl+C to exit)",
    "cli.help.status.watch_interval": "Refresh interval in seconds (default 5)",
    "cli.help.status.json": "JSON output (for automation scripts)",
    "cli.status.stopped": "stopped",
    # models
    "cli.help.models": "List available models",
    "cli.help.models.provider": "Provider filter (omit for all)",
    "cli.help.models.refresh": "Ignore cache and re-query the API",
    "cli.help.models.json": "JSON output",
    "cli.models.title": "Available models",
    "cli.models.col.provider": "provider",
    "cli.models.col.id": "id",
    "cli.models.col.display": "display",
    "cli.models.col.default": "default",
    "cli.models.col.source": "source",
    # providers
    "cli.help.providers": "List built-in and optional provider extensions",
    "cli.help.providers.include_ext": "Include extension entry-point metadata (does not load plugin code)",
    "cli.help.providers.json": "JSON output",
    "cli.providers.title": "Provider registry",
    "cli.providers.col.id": "provider",
    "cli.providers.col.source": "source",
    "cli.providers.col.status": "status",
    "cli.providers.col.default": "default model",
    # durable preferences
    "cli.help.config": "View or update durable CLI preferences",
    "cli.help.config.default_provider": "View or set the provider used when none is specified",
    "cli.help.config.default_provider.provider": "claude, codex, or gemini",
    "cli.help.config.default_provider.reset": "Reset to the built-in Claude default",
    "cli.config.default_provider.current": "Default provider: {provider}",
    "cli.config.default_provider.set": "Default provider set to: {provider}",
    "cli.config.default_provider.reset": "Default provider reset to: claude",
    "cli.config.default_provider.conflict": "Choose a provider or --reset, not both.",
    # chat
    "cli.help.chat": "Single prompt call (stdin input also supported)",
    "cli.help.chat.prompt": "Prompt text. Read from stdin if omitted",
    "cli.help.chat.model": "Model name or provider/model (e.g. haiku, claude/sonnet, gpt-5.4-mini)",
    "cli.help.chat.stream": "Token-by-token streaming output (spinner while waiting for first token)",
    "cli.help.chat.no_web_search": "Disable the web-search tool (ON by default)",
    "cli.help.chat.terse": "Suppress Claude's verbose answers to short questions",
    "cli.help.chat.cwd": "Working directory for the sub-CLI (affects tool use)",
    "cli.help.chat.image": "Attach a local image (repeatable). Remote URLs are not fetched.",
    "cli.help.chat.resume": "Resume a specific session_id",
    "cli.help.chat.continue": "Continue the last saved session (~/.unified-cli/state.json)",
    "cli.help.chat.new": "Ignore saved session, start a new conversation + reset state file",
    "cli.chat.route_failed": "model routing failed:",
    "cli.chat.need_prompt": "No prompt given. Usage: unified-cli chat \"your message\" "
                            "(or pipe text in: echo hi | unified-cli chat).",
    "cli.chat.no_saved_session": "⚠ No saved session — starting a new conversation.",
    "cli.chat.continue_wrong_provider": "⚠ --continue is for the previous provider ({saved}); "
                                        "-m specified {routed} — starting a new conversation.",
    "cli.chat.invalid_cwd": "Working directory does not exist or is not a directory: {cwd}",
    "cli.chat.saved_cwd_missing": "⚠ Saved working directory is unavailable ({cwd}); using the current directory.",
    "cli.chat.waiting": "waiting for response…",
    "cli.chat.using_tool": "using tool: {name}",
    # session panel
    "cli.panel.saved": "(saved)",
    "cli.panel.resume": "resume:",
    "cli.panel.or": "    or:",
    "cli.panel.title": "session",
    # repl subcommand help
    "cli.help.repl": "Interactive REPL mode (slash commands /help)",
    "cli.help.repl.provider": "Starting provider (default: saved config or claude)",
    "cli.help.repl.model": "Starting model (default: provider's default model)",
    "cli.help.repl.no_web_search": "Disable the web-search tool (ON by default)",
    "cli.help.repl.terse": "Claude terse-reply mode",
    "cli.help.repl.cwd": "Working directory for the sub-CLI",
    "cli.help.repl.continue": "Resume your last saved session on start",
    "cli.help.serve": "Launch the localhost dashboard + OpenAI-compatible API",
    "cli.help.serve.port": "Port to bind (default 8000)",
    "cli.help.serve.open": "Open the dashboard in your browser",
    "cli.serve.starting": "Serving on [cyan]{url}[/cyan]  (Ctrl+C to stop)",
    "cli.serve.missing_deps": "The server needs extra dependencies (fastapi, uvicorn).",
    "cli.serve.install_hint": "Install them with: pip install \"unified-cli[server]\"",

    # ===== onboarding (onboarding.py) =====
    "setup.banner": "unified-cli setup — onboarding 3 providers",
    "setup.gemini.gated": "gemini (agy) is disabled by default (ToS risk) — skipping it. "
                          "Set UNIFIED_CLI_ENABLE_GEMINI=1 to include it in setup.",
    "setup.rule.env": "1. Environment check",
    "setup.rule.install": "2. Install missing CLIs",
    "setup.rule.login": "3. Providers needing login (OAuth)",
    "setup.rule.verify": "4. Verify with a test call",
    "setup.rule.summary": "5. Summary",
    "setup.install.all_detected": "✓ All CLI binaries detected",
    "setup.install.no_binary_title": "\\[{name}] binary missing",
    "setup.install.no_binary_body": "The install command below will be run (Y/n). "
                                    "If declined, the command is just printed.",
    "setup.install.no_pkg_mgr": "No usable package manager (brew/npm) found.",
    "setup.install.manual": "Run one of these manually:\n{opts}",
    "setup.install.run_prompt": "\\[{name}] Run it?",
    "setup.install.skipped": "  [yellow]Skipped.[/yellow] Run manually: {cmd}",
    "setup.install.running": "  Running... (streamed output)",
    "setup.install.done": "  [green]✓ Install complete[/green]",
    "setup.install.failed": "  [red]✗ Install failed (exit {code})[/red]",
    "setup.login.needed_title": "\\[{name}] login needed",
    "setup.login.needed_body": "The command below starts OAuth login (a browser opens).\n  {cmd}\n\n",
    "setup.login.claude_note": "⚠  For Claude: after entering the TUI, type the `/login` slash command "
                               "and press Enter; when done, exit with `/exit`.",
    "setup.login.generic_note": "Setup continues automatically once done.",
    "setup.login.prompt": "\\[{name}] Log in now?",
    "setup.login.skipped": "  [yellow]Skipped.[/yellow] Run manually: {cmd}",
    "setup.login.skipped_env": "  Or set the {env} environment variable to use an API key.",
    "setup.login.spawned": "  [green]✓ Login process exited[/green]",
    "setup.login.exit_maybe_cancelled": "  [yellow]Login process exit {code} (may have been cancelled)[/yellow]",
    "setup.login.all_authed": "✓ All providers already authenticated (OAuth or API key)",
    "setup.verify.testing": "{name} test call...",
    "setup.verify.no_binary": "  [red]✗ {name}: no binary — skipping verification[/red]",
    "setup.verify.no_auth": "  [red]✗ {name}: no auth — skipping verification[/red]",
    "setup.verify.hint_label": "     hint: {hint}",
    "setup.summary.final_status": "Final status",
    "setup.summary.all_ready": "✓ All providers ready",
    "setup.summary.skip_verify_note": "[dim]Verification calls were skipped via --skip-verify.[/dim]",
    "setup.summary.next_step": "Next step: [cyan]unified-cli chat \"hi\" -m haiku[/cyan]",
    "setup.summary.some_manual": "Some providers need manual handling:",
    "setup.summary.retry": "Retry: [cyan]unified-cli setup[/cyan]",
    "setup.summary.details": "Details: [cyan]unified-cli doctor[/cyan]",

    # ===== ui (ui.py) =====
    "ui.health.ok": "OK",
    "ui.health.setup_needed": "setup needed",
    "ui.health.missing_binary": "missing binary",
    "ui.auth.none": "(none)",
    "ui.auth.keychain_blocked": "Keychain blocked",
    "ui.bin.not_found": "(not found)",
    "ui.table.status_title": "Provider status",
    "ui.table.col.provider": "Provider",
    "ui.table.col.health": "Health",
    "ui.table.col.binary": "Binary",
    "ui.table.col.auth": "Auth",
    "ui.table.col.models": "Models",
    "ui.table.col.default_model": "Default model",
    "ui.recent.title": "Recent calls (last {limit})",
    "ui.recent.col.time": "time",
    "ui.recent.col.provider": "provider",
    "ui.recent.col.model": "model",
    "ui.recent.col.in": "in",
    "ui.recent.col.out": "out",
    "ui.recent.col.latency": "latency",
    "ui.recent.col.prompt": "prompt",
    "ui.recent.col.error": "error",
    "ui.agg.title": "Usage totals (this process)",
    "ui.agg.col.provider": "provider",
    "ui.agg.col.calls": "calls",
    "ui.agg.col.errors": "errors",
    "ui.agg.col.tokens": "tokens in/out",
    "ui.agg.col.cached": "cached",
    "ui.agg.col.avg_latency": "avg latency",
    "ui.layout.title": "unified-cli status",
    "ui.layout.providers_title": "Providers",

    # ===== errors (errors.py) — messages/hints WE generate =====
    "err.cause_label": "cause",
    "err.hint.claude_login": "Re-run `claude /login` or set the ANTHROPIC_API_KEY environment variable.",
    "err.hint.codex_login": "Re-run `codex login` or set the OPENAI_API_KEY environment variable.",
    "err.hint.gemini_login": "Run `agy` to log in again via the browser (Antigravity). "
                             "The old gemini CLI is blocked for individual accounts.",
    "err.hint.antigravity_migrate": "The old gemini CLI ended individual-account support — migrated to "
                                    "Antigravity `agy`. Install/log in with `agy` first "
                                    "(https://antigravity.google).",
    "err.hint.wait_and_retry": "Try again shortly, or switch to another provider/model.",
    "err.hint.check_model_list": "Check available models with `unified-cli models`.",
    "err.hint.codex_subscription_models": "Codex models available on a ChatGPT subscription: {models}. "
                                          "To use a new model (e.g. gpt-5.5), upgrade the CLI first with "
                                          "`brew upgrade codex` or `npm i -g @openai/codex@latest`.",
    "err.hint.codex_subscription_fallback": "Check available models with `unified-cli models codex`.",
    "err.hint.network_retry": "Check your network connection. The unified wrapper already retried twice.",
    "err.hint.check_resource": "Check whether the requested resource (model/session) exists.",
    "err.hint.install_cli": "CLI binary not found. Install the provider CLI and check your PATH.",
    "err.msg.auth_expired": "{provider} authentication has expired.",
    "err.msg.rate_limit": "{provider} usage limit exceeded.",
    "err.msg.model_not_allowed": "{provider} does not allow this model.",
    "err.msg.not_found": "{provider} requested resource not found.",
    "err.msg.network": "{provider} a network error occurred.",
    "err.msg.resource_limit": "{provider} exceeded a local safety limit.",
    "err.msg.config": "{provider} configuration is invalid.",
    "err.msg.internal": "{provider} an internal error occurred.",
    "err.msg.unknown": "{provider} CLI exit code {exitcode}: unknown error",
    "err.hint.check_stderr": "Check the full stderr.",

    # ===== base.py =====
    "err.base.empty_prompt": "The prompt is empty.",
    "err.base.empty_prompt.hint": "Pass non-whitespace text. If reading from stdin, check the piped input.",
    "err.base.session_mismatch": "Requested session {requested}… was not found, so a new session {got}… was created.",
    "err.base.session_mismatch.hint": "The session may have expired or been created in a different cwd. "
                                      "Get a fresh session_id or restart the Conversation.",
    "err.base.no_binary": "{provider} CLI binary not found.",
    "err.base.timeout": "{provider} response did not arrive within {timeout}s.",
    "err.base.timeout.hint": "Possible network/CLI hang. Increase the timeout or retry. "
                             "Adjustable via BaseProvider(timeout=N).",
    "err.base.timeout_fallback": "{provider} timed out during API-key fallback.",
    "err.base.timeout_fallback.hint": "Check your network and retry.",
    "err.base.stream_timeout": "{provider} stream did not finish within {timeout}s.",
    "err.base.stream_timeout.hint": "For long replies, increase it via BaseProvider(timeout=N).",
    "err.base.no_first_output": "{provider} produced no output within {timeout}s "
                                "(the CLI appears wedged before starting).",
    "err.base.keychain_hint": "On macOS under launchd / cron / a service, `claude` can "
                              "hang forever waiting on the login Keychain (no TTY to "
                              "unlock it). Fix: run `claude setup-token` in a terminal, "
                              "then set CLAUDE_CODE_OAUTH_TOKEN in the service "
                              "environment (or export ANTHROPIC_API_KEY). See the README "
                              "section 'Running under launchd / cron / a server'.",
    "err.base.line_too_long": "{provider} emitted an oversized output line.",
    "err.base.line_too_long.hint": "A single streamed line exceeded the read buffer; "
                                   "retry with non-streaming chat if this recurs.",
    "err.base.output_limit": "{provider} output exceeded the local safety limit.",
    "err.base.output_limit.hint": "Reduce the requested output or raise the explicit "
                                  "BaseProvider output limits if this workload is trusted.",

    # ===== factory.py =====
    "err.factory.unknown_provider": "Unknown provider: {provider}",
    "err.factory.unknown_provider.hint": "provider must be one of claude / codex / gemini.",
    "err.factory.cannot_route": "Could not infer a provider for model '{model}'.",
    "err.factory.cannot_route.hint": "Use the `provider/model` form (e.g. claude/haiku) or a known prefix "
                                     "(claude-, gpt-, gemini-, haiku/sonnet/opus).",
    "err.plugin.runtime": "Provider extension '{provider}' failed.",
    "err.plugin.runtime.hint": "Check the provider extension installation and its private logs.",

    # ===== providers: login hints + install hints =====
    "err.claude.login_hint": "Re-run `claude /login`.",
    "err.claude.install_hint": "Install the Claude Desktop app or `npm i -g @anthropic-ai/claude-code`. "
                               "Or set the CLAUDE_CLI_PATH environment variable.",
    "err.claude.terse_rule": "Keep answers as concise as the question requires. "
                             "Add extra explanation only when asked.",
    "err.claude.image_url_only": "The Claude Read tool accepts local files only. Download the URL first.",
    "err.claude.empty_image": "Empty image attachment.",
    "err.claude.no_json": "No JSON in the Claude CLI response.",
    "err.claude.image_label": "Image file: {path}",
    "err.claude.image_instruction": "Read the image(s) above with the Read tool and answer the following:",
    "err.codex.login_hint": "Re-run `codex login`.",
    "err.codex.install_hint": "`brew install codex` or `npm i -g @openai/codex`.",
    "err.codex.image_url_only": "Codex `-i` accepts local files only. Download the URL first.",
    "err.codex.empty_image": "Empty image attachment.",
    "err.codex.turn_error": "The Codex turn ended with an error.",
    "err.gemini.login_hint": "Run `agy` to log in via the browser (Antigravity).",
    "err.gemini.install_hint": "Install the Antigravity CLI `agy` (https://antigravity.google). "
                               "Or set the AGY_CLI_PATH environment variable.",
    "err.gemini.gate_msg": "The agy (Antigravity) provider is disabled by default. "
                           "Automating agy risks a Google ToS violation and account suspension/ban.",
    "err.gemini.gate_hint": "To use it at your own risk, set the environment variable "
                            "{env}=1. claude / codex are unaffected.",
    "err.gemini.model_not_found": "agy model '{model}' not found.",
    "err.gemini.model_not_found.hint": "Check with `unified-cli models gemini` "
                                       "(agy silently falls back to the default for an unknown model name).",
    "err.gemini.ineligible": "This client is no longer supported — migrated to Antigravity (`agy`).",
    "err.gemini.ineligible.hint": "Make sure you logged in with `agy`. "
                                  "The old gemini CLI is blocked for individual accounts.",
    "err.gemini.empty_response": "agy returned an empty response.",
    "err.gemini.empty_response.hint": "Check the model/network status or try again.",
    "err.gemini.image_url_only": "agy `@<path>` accepts local files only. Download the URL first.",
    "err.gemini.empty_image": "Empty image attachment.",
    "err.gemini.stream_timeout": "agy stream did not finish within {timeout}s.",
    "err.gemini.stream_timeout.hint": "For a long agent task, increase the timeout.",

    # ===== server.py =====
    "server.external_bind.warning": "\n⚠️  Attempting to bind to a non-local host ({host}).\n"
                                    "    This server runs on YOUR subscription auth — if reachable\n"
                                    "    externally, other people's requests get served by your\n"
                                    "    subscription, violating each provider's ToS (account\n"
                                    "    suspension/ban risk). Use it for personal local use only.\n",
    "server.external_bind.hint": "If you really need external exposure, set {env}=1 "
                                 "(not recommended).",
    "server.external_bind.proceeding": "    {env}=1 is set — proceeding at your own risk.\n",
}

_KO = {
    "repl.banner.title": "unified-cli repl — 대화형 모드",
    "repl.banner.hint": "슬래시 명령: /help · / 입력 시 메뉴 · 종료: /exit 또는 Ctrl+D",
    "repl.banner.hint_basic": "슬래시 명령: /help · 종료: /exit 또는 Ctrl+D",
    "repl.banner.start": "시작 provider: {provider} / model: {model}",
    "repl.interrupt_hint": "/exit 로 종료하거나 Ctrl+D 를 누르세요.",
    "repl.image.usage": "/image <path>",
    "repl.image.not_found": "파일을 찾을 수 없음: {path}",
    "repl.image.attached": "이미지 첨부됨 ({n}개 대기 중). 다음 메시지에 같이 보냄.",
    "repl.images.none": "(첨부된 이미지 없음)",
    "repl.images.cleared": "{n}개 첨부 지움.",
    "repl.model.usage": "사용법: /model <name>  (인자 없이 /model 이면 선택창)",
    "repl.model.changed": "모델 변경: {model} (같은 provider 유지)",
    "repl.model.cancelled": "(모델 변경 안 함)",
    "repl.model.unknown": "알 수 없는 모델 '{model}' — 그대로 전달합니다.",
    "repl.provider.usage": "/provider <claude|codex|gemini>",
    "repl.provider.switched": "provider 전환: {old} → {new}  (다음 턴에 직전 8턴 컨텍스트 자동 주입)",
    "repl.provider.locked": "provider '{provider}' 는 잠겨 있습니다.",
    "repl.gemini.locked": "agy(Antigravity) 프로바이더는 기본 비활성화입니다 — 자동화 시 Google ToS 위반·계정 밴 위험. 본인 책임 하에 쓰려면 UNIFIED_CLI_ENABLE_GEMINI=1 을 설정하세요.",
    "conv.sticky_switch": "sticky 대화에서 provider 전환 불가: '{locked}' 로 고정됨",
    "conv.sticky_switch.hint": "sticky=False 로 Conversation 을 생성하세요.",
    "conv.ctx.header": "[이전 대화 요약 — 다른 provider 에서 진행됨]",
    "conv.ctx.user": "사용자",
    "conv.ctx.assistant": "어시스턴트",
    "repl.new.done": "대화 초기화됨.",
    "repl.resume.none": "이어쓸 저장된 세션이 없습니다.",
    "repl.resume.done": "{provider}/{model} 재개 · 세션 {sid}… ({age}분 전). "
                        "다음 메시지부터 이어집니다.",
    "repl.turn.session_reset": "세션이 만료됨 — 다음 메시지에서 새 세션으로 시작합니다.",
    "repl.save.none": "아직 저장할 세션이 없음 (첫 턴 후에 /save 쓰기).",
    "repl.save.title": "save",
    "repl.save.body": "session_id={sid}\n이어쓰기: unified-cli chat \"...\" --resume {sid}\n또는:    unified-cli chat \"...\" --continue  (마지막 저장 세션)",
    "repl.history.none": "(아직 히스토리 없음)",
    "repl.tokens.none": "(이번 프로세스에서 호출 없음)",
    "repl.status.hint": "실시간 status — Ctrl+C 로 프롬프트 복귀",
    "repl.lang.usage": "사용법: /lang <en|ko>  (현재: {lang})",
    "repl.lang.changed": "언어: {lang}",
    "repl.lang.unknown": "알 수 없는 언어 '{lang}' (지원: en, ko)",
    "repl.help.footer": "언어: {lang} · {provider}/{model} · / 입력 시 실시간 메뉴 · 인자 없는 /model 은 선택창",
    "repl.help.col_cmd": "명령",
    "repl.help.col_desc": "설명",
    "repl.unknown": "모르는 명령: {cmd}  — /help",
    "repl.slash_error": "명령 오류: {err}",
    "repl.turn.waiting": "응답 대기 중…",
    "repl.turn.tool": "도구 사용 중: {name}",
    "repl.turn.cancelled": "취소됨.",
    "repl.turn.error": "오류: {err}",
    "repl.exit.saved": "저장됨: 다음 호출에서 unified-cli chat \"...\" --continue 로 이어쓰기",
    "repl.exit.bye": "bye.",
    "repl.picker.model_title": "모델 선택 — {provider}",
    "repl.picker.model_text": "↑/↓ 선택, Enter 확정 (★ = 기본)",
    "repl.picker.provider_title": "provider 선택",
    "repl.picker.provider_text": "↑/↓ 선택, Enter 확정",
    "repl.picker.locked_suffix": " (잠김)",
    "repl.picker.default_suffix": " ★",
    "slash.desc.help": "이 목록",
    "slash.desc.model": "모델 변경 (인자 없으면 선택창)",
    "slash.desc.provider": "provider 전환 (컨텍스트 자동 주입)",
    "slash.desc.image": "다음 prompt 에 이미지 첨부 (반복 가능)",
    "slash.desc.images": "첨부된 이미지 목록",
    "slash.desc.clear-images": "첨부 이미지 지우기",
    "slash.desc.new": "대화 초기화 (컨텍스트 버리기)",
    "slash.desc.resume": "마지막 저장된 세션 다시 열기",
    "slash.desc.save": "현재 session_id + 이어쓰기 명령 표시",
    "slash.desc.history": "최근 N(기본 10)턴 표시",
    "slash.desc.tokens": "이번 프로세스 누적 토큰/호출",
    "slash.desc.doctor": "provider 헬스 한 줄 체크",
    "slash.desc.status": "실시간 status 패널 (Ctrl+C 종료)",
    "slash.desc.lang": "언어 설정 (en|ko)",
    "slash.desc.exit": "종료 (Ctrl+D 와 동일)",

    # ===== CLI (cli.py) =====
    "cli.tagline": "Claude / Codex / Gemini 통합 CLI 래퍼",
    "cli.app.desc": "3개 AI CLI (claude / codex / gemini) 통합 래퍼. "
                    "첫 실행 시 `unified-cli setup` 을 권장합니다.",
    "cli.hint.first_time": "처음이면: [bold]unified-cli setup[/bold]  (대화형 온보딩 마법사)",
    "cli.hint.status": "상태 확인: [bold]unified-cli doctor[/bold] · [bold]unified-cli status[/bold]",
    "cli.hint.oneshot": "단발 호출: [bold]unified-cli chat \"안녕\" -m haiku[/bold]",
    "cli.hint.continue": "이어쓰기: [bold]unified-cli chat \"...\" --continue[/bold]  [dim](마지막 세션)[/dim]",
    "cli.hint.repl": "대화 모드: [bold]unified-cli repl[/bold]  [dim](슬래시 명령 + provider 교체)[/dim]",
    "cli.hint.serve": "대시보드 + API: [bold]unified-cli serve[/bold]  [dim](localhost 웹 UI)[/dim]",
    "cli.hint.models": "모델 목록: [bold]unified-cli models[/bold]",
    "cli.hint.full_help": "[dim]전체 도움말: unified-cli --help[/dim]",
    "cli.help.lang": "UI 언어 (en|ko)",
    "cli.help.doctor": "바이너리 · auth · 모델 개수 점검",
    "cli.help.doctor.json": "machine-readable JSON 출력 (자동화 스크립트용)",
    "cli.help.doctor.headless": "실시간 auth preflight — 서비스(launchd/cron) 컨텍스트에서 "
                                "실행해 키체인 hang 을 잡아냅니다",
    "cli.doctor.title": "unified-cli doctor",
    "cli.doctor.needs_setup": "⚠ setup 이 필요한 provider 가 있습니다. `unified-cli setup` 을 실행하세요.",
    "cli.doctor.headless.title": "unified-cli doctor · headless preflight",
    "cli.doctor.headless.intro": "provider 마다 아주 작은 실제 호출을 합니다(15초 타임아웃, stdin 닫음). "
                                 "서비스와 동일한 컨텍스트에서 실행해 거기서 auth 가 되는지 증명하세요.",
    "cli.doctor.headless.ok": "이 컨텍스트에서 auth 정상",
    "cli.doctor.headless.no_binary": "CLI 바이너리를 찾을 수 없음 (PATH / $CLAUDE_CLI_PATH 확인)",
    "cli.doctor.headless.skipped_gate": "건너뜀 (테스트하려면 UNIFIED_CLI_ENABLE_GEMINI 설정)",
    "cli.doctor.headless.done_ok": "✓ 이 컨텍스트에서 모든 provider 도달 가능.",
    "cli.doctor.headless.done_fail": "⚠ 일부 provider 실패 — 위 힌트를 확인하세요.",
    "cli.help.setup": "대화형 온보딩 마법사 (설치 + 로그인 + 검증)",
    "cli.help.setup.provider": "특정 provider 만 진행 (기본: 세 개 모두)",
    "cli.help.setup.skip_install": "설치 단계 건너뛰기",
    "cli.help.setup.skip_verify": "테스트 호출 건너뛰기 (토큰 절약)",
    "cli.help.status": "사용량 스냅샷 + (옵션) 실시간 대시보드",
    "cli.help.status.watch": "rich.live 로 주기 갱신 대시보드 (Ctrl+C 로 종료)",
    "cli.help.status.watch_interval": "갱신 주기 초 (기본 5)",
    "cli.help.status.json": "JSON 출력 (자동화 스크립트용)",
    "cli.status.stopped": "종료",
    "cli.help.models": "사용 가능한 모델 목록",
    "cli.help.models.provider": "provider 필터 (생략 시 전부)",
    "cli.help.models.refresh": "캐시 무시하고 API 재조회",
    "cli.help.models.json": "JSON 출력",
    "cli.models.title": "사용 가능한 모델",
    "cli.models.col.provider": "provider",
    "cli.models.col.id": "id",
    "cli.models.col.display": "표시명",
    "cli.models.col.default": "기본",
    "cli.models.col.source": "출처",
    # providers
    "cli.help.providers": "내장 provider 및 선택적 확장 목록",
    "cli.help.providers.include_ext": "확장 entry point 메타데이터 포함 (plugin 코드는 로드하지 않음)",
    "cli.help.providers.json": "JSON 출력",
    "cli.providers.title": "Provider 레지스트리",
    "cli.providers.col.id": "provider",
    "cli.providers.col.source": "출처",
    "cli.providers.col.status": "상태",
    "cli.providers.col.default": "기본 모델",
    # 지속 설정
    "cli.help.config": "지속 저장되는 CLI 설정 조회 또는 변경",
    "cli.help.config.default_provider": "provider 미지정 시 사용할 provider 조회 또는 설정",
    "cli.help.config.default_provider.provider": "claude, codex 또는 gemini",
    "cli.help.config.default_provider.reset": "내장 Claude 기본값으로 초기화",
    "cli.config.default_provider.current": "기본 provider: {provider}",
    "cli.config.default_provider.set": "기본 provider 설정: {provider}",
    "cli.config.default_provider.reset": "기본 provider를 claude로 초기화했습니다",
    "cli.config.default_provider.conflict": "provider 또는 --reset 중 하나만 지정하세요.",
    "cli.help.chat": "단일 프롬프트 호출 (stdin 입력도 가능)",
    "cli.help.chat.prompt": "프롬프트 텍스트. 생략 시 stdin 에서 읽음",
    "cli.help.chat.model": "모델명 또는 provider/model (예: haiku, claude/sonnet, gpt-5.4-mini)",
    "cli.help.chat.stream": "토큰 단위 스트리밍 출력 (첫 토큰 대기 중엔 스피너 표시)",
    "cli.help.chat.no_web_search": "웹서치 도구 비활성화 (기본 ON)",
    "cli.help.chat.terse": "Claude 가 짧은 질문에 장황하게 답하는 걸 억제",
    "cli.help.chat.cwd": "하위 CLI 의 작업 디렉토리 (도구 사용 시 영향)",
    "cli.help.chat.image": "로컬 이미지 첨부 (반복 가능). 원격 URL은 자동 다운로드하지 않음",
    "cli.help.chat.resume": "특정 session_id 이어쓰기",
    "cli.help.chat.continue": "마지막 저장된 세션 이어쓰기 (~/.unified-cli/state.json)",
    "cli.help.chat.new": "저장된 세션 무시하고 새 대화 시작 + 상태파일 초기화",
    "cli.chat.route_failed": "모델 라우팅 실패:",
    "cli.chat.need_prompt": "프롬프트가 없습니다. 사용법: unified-cli chat \"메시지\" "
                            "(또는 파이프: echo 안녕 | unified-cli chat).",
    "cli.chat.no_saved_session": "⚠ 저장된 세션 없음 — 새 대화로 시작.",
    "cli.chat.continue_wrong_provider": "⚠ --continue 는 이전 provider ({saved}) 전용, "
                                        "-m 로 {routed} 지정 — 새 대화로 시작.",
    "cli.chat.invalid_cwd": "작업 디렉토리가 없거나 디렉토리가 아닙니다: {cwd}",
    "cli.chat.saved_cwd_missing": "⚠ 저장된 작업 디렉토리를 찾을 수 없습니다 ({cwd}). 현재 디렉토리를 사용합니다.",
    "cli.chat.waiting": "응답 대기 중…",
    "cli.chat.using_tool": "도구 사용 중: {name}",
    "cli.panel.saved": "(저장됨)",
    "cli.panel.resume": "이어쓰기:",
    "cli.panel.or": "     또는:",
    "cli.panel.title": "session",
    "cli.help.repl": "대화형 REPL 모드 (슬래시 명령 /help)",
    "cli.help.repl.provider": "시작 provider (기본: 저장된 설정 또는 claude)",
    "cli.help.repl.model": "시작 모델 (생략 시 provider 기본 모델)",
    "cli.help.repl.no_web_search": "웹서치 도구 비활성화 (기본 ON)",
    "cli.help.repl.terse": "Claude 짧은 응답 모드",
    "cli.help.repl.cwd": "하위 CLI 의 작업 디렉토리",
    "cli.help.repl.continue": "시작 시 마지막 저장 세션 이어쓰기",
    "cli.help.serve": "localhost 대시보드 + OpenAI 호환 API 실행",
    "cli.help.serve.port": "바인드 포트 (기본 8000)",
    "cli.help.serve.open": "브라우저에서 대시보드 열기",
    "cli.serve.starting": "[cyan]{url}[/cyan] 에서 서비스 중  (Ctrl+C 로 중지)",
    "cli.serve.missing_deps": "서버에는 추가 의존성(fastapi, uvicorn)이 필요합니다.",
    "cli.serve.install_hint": "설치: pip install \"unified-cli[server]\"",

    # ===== onboarding (onboarding.py) =====
    "setup.banner": "unified-cli setup — 3개 provider 온보딩",
    "setup.gemini.gated": "gemini (agy) 는 기본 비활성(ToS 위험) — 건너뜁니다. "
                          "setup 에 포함하려면 UNIFIED_CLI_ENABLE_GEMINI=1 을 설정하세요.",
    "setup.rule.env": "1. 환경 검사",
    "setup.rule.install": "2. 누락된 CLI 설치",
    "setup.rule.login": "3. 로그인 (OAuth) 필요한 provider",
    "setup.rule.verify": "4. 테스트 호출 검증",
    "setup.rule.summary": "5. 요약",
    "setup.install.all_detected": "✓ 모든 CLI 바이너리 감지됨",
    "setup.install.no_binary_title": "\\[{name}] 바이너리 없음",
    "setup.install.no_binary_body": "아래 설치 명령을 실행합니다 (Y/n). 거부하면 명령만 출력됩니다.",
    "setup.install.no_pkg_mgr": "사용 가능한 패키지 매니저(brew/npm) 가 없습니다.",
    "setup.install.manual": "수동으로 다음 중 하나를 실행하세요:\n{opts}",
    "setup.install.run_prompt": "\\[{name}] 실행할까요?",
    "setup.install.skipped": "  [yellow]건너뜀.[/yellow] 수동으로 실행: {cmd}",
    "setup.install.running": "  실행 중... (스트림 출력)",
    "setup.install.done": "  [green]✓ 설치 완료[/green]",
    "setup.install.failed": "  [red]✗ 설치 실패 (exit {code})[/red]",
    "setup.login.needed_title": "\\[{name}] 로그인 필요",
    "setup.login.needed_body": "아래 명령으로 OAuth 로그인을 시작합니다 (브라우저가 열립니다).\n  {cmd}\n\n",
    "setup.login.claude_note": "⚠  Claude 의 경우 TUI 진입 후 `/login` 슬래시 명령을 치고 엔터, "
                               "완료되면 `/exit` 로 나오세요.",
    "setup.login.generic_note": "완료 후 자동으로 setup 이 이어집니다.",
    "setup.login.prompt": "\\[{name}] 지금 로그인할까요?",
    "setup.login.skipped": "  [yellow]건너뜀.[/yellow] 수동 실행: {cmd}",
    "setup.login.skipped_env": "  또는 환경변수 {env} 설정으로 API key 사용 가능.",
    "setup.login.spawned": "  [green]✓ 로그인 프로세스 종료[/green]",
    "setup.login.exit_maybe_cancelled": "  [yellow]로그인 프로세스 exit {code} (취소되었을 수 있음)[/yellow]",
    "setup.login.all_authed": "✓ 모든 provider 가 이미 인증됨 (OAuth 또는 API key)",
    "setup.verify.testing": "{name} 테스트 호출...",
    "setup.verify.no_binary": "  [red]✗ {name}: 바이너리 없음 — 검증 건너뜀[/red]",
    "setup.verify.no_auth": "  [red]✗ {name}: 인증 없음 — 검증 건너뜀[/red]",
    "setup.verify.hint_label": "     힌트: {hint}",
    "setup.summary.final_status": "최종 상태",
    "setup.summary.all_ready": "✓ 모든 provider 준비 완료",
    "setup.summary.skip_verify_note": "[dim]--skip-verify 로 검증 호출은 생략됐습니다.[/dim]",
    "setup.summary.next_step": "다음 단계: [cyan]unified-cli chat \"안녕\" -m haiku[/cyan]",
    "setup.summary.some_manual": "일부 provider 는 수동 처리가 필요합니다:",
    "setup.summary.retry": "재시도: [cyan]unified-cli setup[/cyan]",
    "setup.summary.details": "상세: [cyan]unified-cli doctor[/cyan]",

    # ===== ui (ui.py) =====
    "ui.health.ok": "정상",
    "ui.health.setup_needed": "설정 필요",
    "ui.health.missing_binary": "바이너리 없음",
    "ui.auth.none": "(없음)",
    "ui.auth.keychain_blocked": "키체인 차단됨",
    "ui.bin.not_found": "(찾을 수 없음)",
    "ui.table.status_title": "Provider 상태",
    "ui.table.col.provider": "Provider",
    "ui.table.col.health": "상태",
    "ui.table.col.binary": "바이너리",
    "ui.table.col.auth": "인증",
    "ui.table.col.models": "모델",
    "ui.table.col.default_model": "기본 모델",
    "ui.recent.title": "최근 호출 (마지막 {limit})",
    "ui.recent.col.time": "시각",
    "ui.recent.col.provider": "provider",
    "ui.recent.col.model": "model",
    "ui.recent.col.in": "입력",
    "ui.recent.col.out": "출력",
    "ui.recent.col.latency": "지연",
    "ui.recent.col.prompt": "프롬프트",
    "ui.recent.col.error": "오류",
    "ui.agg.title": "사용량 합계 (이번 프로세스)",
    "ui.agg.col.provider": "provider",
    "ui.agg.col.calls": "호출",
    "ui.agg.col.errors": "오류",
    "ui.agg.col.tokens": "토큰 입력/출력",
    "ui.agg.col.cached": "캐시",
    "ui.agg.col.avg_latency": "평균 지연",
    "ui.layout.title": "unified-cli status",
    "ui.layout.providers_title": "Providers",

    # ===== errors (errors.py) =====
    "err.cause_label": "원인",
    "err.hint.claude_login": "`claude /login` 을 재실행하거나 ANTHROPIC_API_KEY 환경변수를 설정하세요.",
    "err.hint.codex_login": "`codex login` 을 재실행하거나 OPENAI_API_KEY 환경변수를 설정하세요.",
    "err.hint.gemini_login": "`agy` 를 실행해 브라우저로 다시 로그인하세요 (Antigravity). "
                             "구 gemini CLI 는 개인 계정에서 차단됨.",
    "err.hint.antigravity_migrate": "구 gemini CLI 는 개인 계정 지원 종료 — Antigravity `agy` 로 마이그레이션됨. "
                                    "`agy` 설치/로그인 후 사용하세요 (https://antigravity.google).",
    "err.hint.wait_and_retry": "잠시 후 다시 시도하거나 다른 provider/모델로 전환하세요.",
    "err.hint.check_model_list": "사용 가능한 모델은 `unified-cli models` 로 확인하세요.",
    "err.hint.codex_subscription_models": "ChatGPT 구독으로 사용 가능한 Codex 모델: {models}. "
                                          "신규 모델 (예: gpt-5.5) 을 쓰려면 `brew upgrade codex` 또는 "
                                          "`npm i -g @openai/codex@latest` 로 CLI 부터 업그레이드하세요.",
    "err.hint.codex_subscription_fallback": "사용 가능한 모델은 `unified-cli models codex` 로 확인하세요.",
    "err.hint.network_retry": "네트워크 연결을 확인하세요. 통합 래퍼는 이미 2회 재시도했습니다.",
    "err.hint.check_resource": "요청한 리소스(모델/세션)가 존재하는지 확인하세요.",
    "err.hint.install_cli": "CLI 바이너리를 찾을 수 없습니다. 해당 provider CLI를 설치하고 PATH를 확인하세요.",
    "err.msg.auth_expired": "{provider} 인증이 만료되었습니다.",
    "err.msg.rate_limit": "{provider} 사용량 한도를 초과했습니다.",
    "err.msg.model_not_allowed": "{provider} 이 모델을 허용하지 않습니다.",
    "err.msg.not_found": "{provider} 요청한 리소스를 찾을 수 없습니다.",
    "err.msg.network": "{provider} 네트워크 오류가 발생했습니다.",
    "err.msg.resource_limit": "{provider} 로컬 안전 한도를 초과했습니다.",
    "err.msg.config": "{provider} 설정이 잘못되었습니다.",
    "err.msg.internal": "{provider} 내부 오류가 발생했습니다.",
    "err.msg.unknown": "{provider} CLI 종료 코드 {exitcode}: 알 수 없는 오류",
    "err.hint.check_stderr": "stderr 전체를 확인하세요.",

    # ===== base.py =====
    "err.base.empty_prompt": "프롬프트가 비어있습니다.",
    "err.base.empty_prompt.hint": "공백이 아닌 텍스트를 전달하세요. stdin 에서 읽는 경우 파이프 입력을 확인하세요.",
    "err.base.session_mismatch": "요청한 세션 {requested}… 을 찾을 수 없어 새 세션 {got}… 이 생성되었습니다.",
    "err.base.session_mismatch.hint": "세션이 만료되었거나 다른 cwd 에서 생성됐을 수 있습니다. "
                                      "session_id 를 새로 받거나 Conversation 을 재시작하세요.",
    "err.base.no_binary": "{provider} CLI 바이너리를 찾을 수 없습니다.",
    "err.base.timeout": "{provider} 응답이 {timeout}초 안에 오지 않음.",
    "err.base.timeout.hint": "네트워크/CLI hang 가능성. timeout 을 늘리거나 다시 시도하세요. "
                             "BaseProvider(timeout=N) 으로 조정 가능.",
    "err.base.timeout_fallback": "{provider} API key fallback 중 timeout.",
    "err.base.timeout_fallback.hint": "네트워크 확인 후 재시도.",
    "err.base.stream_timeout": "{provider} 스트림이 {timeout}초 안에 끝나지 않음.",
    "err.base.stream_timeout.hint": "긴 응답이면 BaseProvider(timeout=N) 으로 늘리세요.",
    "err.base.no_first_output": "{provider}가 {timeout}초 안에 아무 출력도 내지 않음 "
                                "(시작 전에 멈춘 것으로 보입니다).",
    "err.base.keychain_hint": "macOS에서 launchd / cron / 서비스로 실행하면 `claude`가 "
                              "로그인 키체인을 여는 TTY가 없어 무한 대기할 수 있습니다. 해결: "
                              "터미널에서 `claude setup-token` 을 실행한 뒤 "
                              "CLAUDE_CODE_OAUTH_TOKEN 을 서비스 환경변수로 설정하세요 "
                              "(또는 ANTHROPIC_API_KEY export). README의 "
                              "'launchd / cron / 서버에서 실행' 섹션 참고.",
    "err.base.line_too_long": "{provider} 출력 한 줄이 너무 큽니다.",
    "err.base.line_too_long.hint": "스트리밍 한 줄이 읽기 버퍼를 초과했습니다. 반복되면 "
                                   "비스트리밍(chat)으로 재시도하세요.",
    "err.base.output_limit": "{provider} 출력이 로컬 안전 한도를 초과했습니다.",
    "err.base.output_limit.hint": "요청 출력량을 줄이거나, 신뢰하는 작업이라면 "
                                  "BaseProvider 출력 한도를 명시적으로 늘리세요.",

    # ===== factory.py =====
    "err.factory.unknown_provider": "알 수 없는 provider: {provider}",
    "err.factory.unknown_provider.hint": "provider 는 claude / codex / gemini 중 하나여야 합니다.",
    "err.factory.cannot_route": "모델 '{model}' 의 provider 를 추론할 수 없습니다.",
    "err.factory.cannot_route.hint": "`provider/model` 형식 (예: claude/haiku) 또는 알려진 접두사 "
                                     "(claude-, gpt-, gemini-, haiku/sonnet/opus) 을 사용하세요.",
    "err.plugin.runtime": "Provider 확장 '{provider}' 실행에 실패했습니다.",
    "err.plugin.runtime.hint": "Provider 확장 설치 상태와 비공개 로그를 확인하세요.",

    # ===== providers =====
    "err.claude.login_hint": "`claude /login` 을 재실행하세요.",
    "err.claude.install_hint": "Claude Desktop 앱 설치 또는 `npm i -g @anthropic-ai/claude-code`. "
                               "또는 CLAUDE_CLI_PATH 환경변수로 경로 지정.",
    "err.claude.terse_rule": "답변은 질문이 요구하는 만큼만 간결하게 하세요. 추가 설명은 요청받을 때만 덧붙이세요.",
    "err.claude.image_url_only": "Claude Read 도구는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
    "err.claude.empty_image": "비어있는 이미지 첨부.",
    "err.claude.no_json": "Claude CLI 응답에 JSON이 없습니다.",
    "err.claude.image_label": "이미지 파일: {path}",
    "err.claude.image_instruction": "위 이미지를 Read 도구로 읽고 다음 질문에 답해주세요:",
    "err.codex.login_hint": "`codex login` 을 재실행하세요.",
    "err.codex.install_hint": "`brew install codex` 또는 `npm i -g @openai/codex`.",
    "err.codex.image_url_only": "Codex `-i` 는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
    "err.codex.empty_image": "비어있는 이미지 첨부.",
    "err.codex.turn_error": "Codex 턴이 에러로 종료되었습니다.",
    "err.gemini.login_hint": "`agy` 를 실행해 브라우저로 로그인하세요 (Antigravity).",
    "err.gemini.install_hint": "Antigravity CLI `agy` 를 설치하세요 (https://antigravity.google). "
                               "또는 AGY_CLI_PATH 환경변수로 경로 지정.",
    "err.gemini.gate_msg": "agy(Antigravity) 프로바이더는 기본 비활성화되어 있습니다. "
                           "agy 자동화는 Google ToS 위반으로 계정 정지/차단(밴) 위험이 있습니다.",
    "err.gemini.gate_hint": "위험을 감수하고 직접 사용하려면 환경변수 {env}=1 을 "
                            "설정하세요. claude / codex 는 영향받지 않습니다.",
    "err.gemini.model_not_found": "agy 모델 '{model}' 을 찾을 수 없습니다.",
    "err.gemini.model_not_found.hint": "`unified-cli models gemini` 로 확인하세요 "
                                       "(agy 는 잘못된 모델명을 조용히 default 로 폴백합니다).",
    "err.gemini.ineligible": "이 클라이언트는 더 이상 지원되지 않습니다 — Antigravity(`agy`)로 마이그레이션됨.",
    "err.gemini.ineligible.hint": "`agy` 로 로그인했는지 확인하세요. 구 gemini CLI 는 개인 계정에서 차단됨.",
    "err.gemini.empty_response": "agy 가 빈 응답을 반환했습니다.",
    "err.gemini.empty_response.hint": "모델/네트워크 상태를 확인하거나 다시 시도하세요.",
    "err.gemini.image_url_only": "agy `@<path>` 는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
    "err.gemini.empty_image": "비어있는 이미지 첨부.",
    "err.gemini.stream_timeout": "agy 스트림이 {timeout}초 안에 끝나지 않음.",
    "err.gemini.stream_timeout.hint": "긴 에이전트 작업이면 timeout 을 늘리세요.",

    # ===== server.py =====
    "server.external_bind.warning": "\n⚠️  비-로컬 호스트({host})에 바인딩하려 합니다.\n"
                                    "    이 서버는 당신의 구독 인증으로 동작합니다 — 외부에서 접근하면\n"
                                    "    타인의 요청이 당신 구독으로 처리되어 각 제공자 ToS 위반(계정\n"
                                    "    정지/차단 위험)이 됩니다. 개인 로컬 용도로만 쓰세요.\n",
    "server.external_bind.hint": "정말 외부 노출이 필요하면 {env}=1 을 "
                                 "설정하세요 (권장하지 않음).",
    "server.external_bind.proceeding": "    {env}=1 설정됨 — 본인 책임 하에 진행합니다.\n",
}

MESSAGES = {"en": _EN, "ko": _KO}
