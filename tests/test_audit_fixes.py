"""Regression tests for the 0.2.0 adversarial-audit fixes (round 1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import io

from unified_cli import models, repl
from unified_cli.conversation import UnifiedConversation


# [1] --terse must not leak to non-claude providers (would TypeError).
def test_terse_stripped_for_non_claude():
    conv = UnifiedConversation(
        default_provider="claude",
        provider_opts={"terse": True, "bin_path": "x", "web_search": False},
    )
    c = conv._get_client("codex", None)  # must not raise (terse stripped)
    assert c.name == "codex"


def test_terse_kept_for_claude():
    conv = UnifiedConversation(
        default_provider="claude",
        provider_opts={"terse": True, "bin_path": "x", "web_search": False},
    )
    c = conv._get_client("claude", None)
    assert c.append_system_prompt  # terse injects an append-system-prompt rule


# [5] gemini model listing must not spawn `agy` when the provider is gated.
def test_gemini_listing_gated_returns_hardcoded(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    models._CACHE.clear()
    ms = models.list_models("gemini")
    assert ms and all(m.source == "hardcoded" for m in ms)


def test_gemini_listing_gated_does_not_invoke_agy_discovery(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    models._CACHE.clear()
    import unified_cli.discovery as disc
    calls = {"n": 0}

    def _spy():
        calls["n"] += 1
        return "/fake/agy"

    monkeypatch.setattr(disc, "find_agy_bin", _spy)
    models.list_models("gemini")
    assert calls["n"] == 0  # gate returns before any agy discovery/subprocess


def test_gemini_listing_unlocked_attempts_agy(monkeypatch):
    # When enabled, the lister is allowed to reach discovery (may still fall
    # back to hardcoded if agy isn't installed — we only assert it tried).
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    models._CACHE.clear()
    import unified_cli.discovery as disc
    calls = {"n": 0}
    monkeypatch.setattr(disc, "find_agy_bin", lambda: calls.__setitem__("n", 1))
    models.list_models("gemini")
    assert calls["n"] == 1


# [8] multi-word /model argument (agy display-name models) must be preserved.
def test_multiword_model_preserved():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    cur = {"provider": "claude", "model": "claude-haiku-4-5"}
    repl._handle_slash("/model Gemini 3.5 Flash (Medium)", conv, cur, {}, [], use_ptk=False)
    assert cur["model"] == "Gemini 3.5 Flash (Medium)"


def test_singleword_model_still_works():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    cur = {"provider": "claude", "model": "claude-haiku-4-5"}
    repl._handle_slash("/model claude-opus-4-7", conv, cur, {}, [], use_ptk=False)
    assert cur["model"] == "claude-opus-4-7"


# round-2: a tab (any whitespace) between /model and the name must not crash.
def test_tab_separated_model_does_not_crash():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    cur = {"provider": "claude", "model": "x"}
    repl._handle_slash("/model\topus", conv, cur, {}, [], use_ptk=False)  # no IndexError
    assert cur["model"] == "opus"


# round-2: cross-provider conversation strings are localized (English default).
def test_sticky_switch_error_localized():
    from unified_cli import i18n
    conv = UnifiedConversation(default_provider="claude", sticky=True)
    conv._locked_provider = "claude"
    i18n.set_lang("en")
    try:
        conv._resolve("codex", None)
    except Exception as e:
        assert "sticky" in e.message.lower() and "claude" in e.message
    finally:
        i18n.set_lang(None)


# round-3: the slash-error guard must not itself crash on markup-shaped error text.
def test_slash_error_print_is_markup_safe():
    from rich.console import Console
    from rich.markup import escape
    from unified_cli.i18n import t
    c = Console(file=io.StringIO())
    e = RuntimeError("subprocess failed: see [/red] [bold]x in log")
    # The exact pattern used in repl.py's guard — must not raise MarkupError.
    c.print(f"[red]{escape(t('repl.slash_error', err=e))}[/red]")


# round-3: --watch-interval with non-numeric input is rejected by argparse (exit 2),
# not an uncaught ValueError traceback.
def test_watch_interval_rejects_non_numeric():
    from unified_cli.cli import main
    with pytest.raises(SystemExit):
        main(["status", "--watch", "--watch-interval", "abc"])


# round-4: markup-shaped user/CLI text must never crash the command (Rich MarkupError).
def test_cli_chat_markup_model_does_not_crash():
    from unified_cli.cli import main
    # route() fails on this bogus model; the error print must escape it, not raise.
    assert main(["chat", "hi", "-m", "[/red]"]) == 2


def test_repl_model_markup_does_not_crash():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    cur = {"provider": "claude", "model": "x"}
    repl._handle_slash("/model [/red] [bold]x", conv, cur, {}, [], use_ptk=False)  # no MarkupError
    assert cur["model"] == "[/red] [bold]x"


# round-5: Table cells (add_row) also parse markup — /history with a model reply
# containing tags like [/INST] must render, not crash.
def test_repl_history_markup_does_not_crash():
    from unified_cli.conversation import Turn
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    conv.turns.append(Turn(provider="claude", prompt="use [bold]x",
                           text="ans: [/INST] [/red] done"))
    cur = {"provider": "claude", "model": "x"}
    repl._handle_slash("/history", conv, cur, {}, [], use_ptk=False)  # no MarkupError


def test_recent_table_markup_does_not_crash():
    import io
    from rich.console import Console
    from unified_cli import ui
    from unified_cli.usage import tracker
    tracker.record("claude", "[/red]", input_tokens=1, output_tokens=1,
                   latency_ms=5, prompt_preview="hi [/INST] [bold]x", session_id="")
    Console(file=io.StringIO()).print(ui.recent_table())  # no MarkupError


# round-6: the /model fallback list and /images listing render untrusted text too.
def test_print_model_list_markup_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        repl, "arg_candidates",
        lambda *a, **k: [("gpt[/dim][on red]X", "disp [/red]"), ("ok", "fine")],
    )
    repl._print_model_list("codex")  # must not raise MarkupError


def test_images_listing_markup_does_not_crash():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    pending = ["/tmp/[/red]evil[bold].png"]
    repl._handle_slash("/images", conv, {"provider": "claude", "model": "x"},
                       {}, pending, use_ptk=False)  # no MarkupError


# round-8: the startup banner (runs before the loop guard) must escape the model.
def test_banner_markup_model_does_not_crash():
    repl._banner({"provider": "claude", "model": "x[/]"}, False)  # no MarkupError


# round-8: setup wizard literal [provider] label must render, not be dropped as a tag.
def test_setup_provider_label_renders():
    import io
    from rich.console import Console
    from unified_cli import i18n
    cap = Console(file=io.StringIO())
    cap.print(i18n.t("setup.install.no_binary_title", name="claude"))
    assert "[claude]" in cap.file.getvalue()


# round-10: an unexpected (non-UnifiedError) turn error must degrade, not crash the REPL.
def test_turn_error_does_not_crash_repl(monkeypatch):
    import builtins
    from unified_cli import repl as R

    def boom(*a, **k):
        raise FileNotFoundError("binary vanished mid-session")

    monkeypatch.setattr(R, "_run_turn", boom)
    monkeypatch.setattr(R, "has_prompt_toolkit", lambda: False)
    monkeypatch.setattr(R, "_interactive", lambda: False)

    lines = iter(["hello"])

    def fake_input(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)
    rc = R.run_repl(provider="claude", model="claude-haiku-4-5")
    assert rc == 0  # turn error degraded, then clean EOF exit


# round-9: dashboard model-bar map must be prototype-safe (model ids are user-controlled).
def test_dashboard_model_map_is_prototype_safe():
    from unified_cli.dashboard_tpl import DASHBOARD_HTML
    assert "Object.create(null)" in DASHBOARD_HTML
    assert "var merged = {}" not in DASHBOARD_HTML


# round-7: the prompt_toolkit history file must be 0o600 (REPL prompts may hold secrets).
def test_ptk_history_is_owner_only(tmp_path, monkeypatch):
    import os
    import stat
    hist = tmp_path / ".unified-cli" / "repl_history.ptk"
    monkeypatch.setattr(repl, "_PTK_HISTORY_FILE", hist)
    repl._harden_repl_history()
    assert hist.exists()
    mode = stat.S_IMODE(os.stat(hist).st_mode)
    assert mode == 0o600, oct(mode)
    dir_mode = stat.S_IMODE(os.stat(hist.parent).st_mode)
    assert dir_mode == 0o700, oct(dir_mode)
