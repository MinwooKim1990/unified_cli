"""Prompt-toolkit and non-TTY behavior checks for the REPL."""

from __future__ import annotations

import builtins
import os
import unicodedata

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.document import Document

from unified_cli import repl
from unified_cli import repl_completion as rc


def test_alt_enter_inserts_newline_while_enter_submits(tmp_path):
    current = {
        "provider": "claude", "model": "haiku", "cwd": str(tmp_path),
        "permission_mode": "provider-default", "web_search": True,
        "context_window": 8, "multiline": True,
    }
    with create_pipe_input() as pipe:
        session = rc.build_session(
            tmp_path / "history", current, input=pipe, output=DummyOutput()
        )
        pipe.send_text("first\x1b\rsecond\r")
        assert session.prompt("> ") == "first\nsecond"


def test_toolbar_contains_runtime_fields(tmp_path):
    toolbar = rc._bottom_toolbar({
        "provider": "claude", "model": "haiku", "cwd": str(tmp_path),
        "permission_mode": "default", "web_search": False,
        "context_window": 6, "last_latency_ms": 12,
    })
    assert "claude/haiku" in toolbar
    assert "perm:default" in toolbar
    assert "web:off" in toolbar and "ctx:6" in toolbar
    assert "tok:" in toolbar and "lat:12ms" in toolbar


def test_toolbar_korean_text_fits_narrow_terminal_cells(monkeypatch):
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback: os.terminal_size((21, 24))
    )
    toolbar = rc._bottom_toolbar({
        "provider": "클로드\x1b", "model": "가나다라마바사",
        "cwd": "/tmp/한글디렉터리이름", "permission_mode": "workspace_write",
        "web_search": True, "context_window": 8,
    })
    assert rc._display_width(toolbar) <= 20
    assert "\x1b" not in toolbar
    assert not any(unicodedata.category(char).startswith("C") for char in toolbar)


def test_toolbar_korean_text_fits_ten_column_terminal(monkeypatch):
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback: os.terminal_size((10, 24))
    )
    toolbar = rc._bottom_toolbar({
        "provider": "클로드\x1b", "model": "가나다라마바사",
        "cwd": "/tmp/한글디렉터리이름", "permission_mode": "workspace_write",
        "web_search": True, "context_window": 8,
    })
    assert rc._display_width(toolbar) <= 9
    assert "\x1b" not in toolbar
    assert not any(unicodedata.category(char).startswith("C") for char in toolbar)


def test_menu_tracks_unique_command_prefix_subcommands_and_values():
    completer = rc.UnifiedCompleter({
        "provider": "claude",
        "model": "haiku",
        "_completion_core_models": {},
    })
    commands = list(completer.get_completions(Document("/the"), None))
    providers = list(completer.get_completions(Document("/prov "), None))
    auth_actions = list(completer.get_completions(Document("/au st"), None))
    auth_providers = list(
        completer.get_completions(Document("/au status co"), None)
    )

    assert [item.text for item in commands] == ["/theme"]
    assert [item.text for item in providers] == [
        "claude", "codex", "gemini", *rc.BUNDLED_EXTENSION_PROVIDERS,
    ]
    assert [item.text for item in auth_actions] == ["status"]
    assert {item.text for item in auth_providers} >= {
        "codex", "copilot", "codebuddy",
    }


def test_non_tty_path_never_builds_prompt_session(monkeypatch):
    monkeypatch.setattr(repl, "_interactive", lambda: False)
    monkeypatch.setattr(repl, "has_prompt_toolkit", lambda: True)
    monkeypatch.setattr(
        repl, "build_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("TTY only")),
    )
    monkeypatch.setattr(repl, "_setup_readline", lambda: None)
    monkeypatch.setattr(repl, "_banner", lambda *args: None)
    monkeypatch.setattr(repl, "_on_exit", lambda *args: None)
    monkeypatch.setattr(builtins, "input", lambda prompt: (_ for _ in ()).throw(EOFError))
    assert repl.run_repl(provider="claude", cwd=".") == 0


def test_tty_startup_does_not_probe_models_or_extensions(monkeypatch):
    class Session:
        def prompt(self, prompt):
            raise EOFError

    history_paths = []

    def build(history_path, *args, **kwargs):
        history_paths.append(history_path)
        return Session()

    monkeypatch.setattr(repl, "_interactive", lambda: True)
    monkeypatch.setattr(repl, "has_prompt_toolkit", lambda: True)
    monkeypatch.setattr(repl, "build_session", build)
    monkeypatch.setattr(repl, "_harden_repl_history", lambda: None)
    monkeypatch.setattr(repl, "_banner", lambda *args: None)
    monkeypatch.setattr(repl, "_on_exit", lambda *args: None)
    monkeypatch.setattr(
        "unified_cli.models.list_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no model probe")),
    )
    monkeypatch.setattr(
        "unified_cli.registry.extension_provider_exists",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no discovery")),
    )
    assert repl.run_repl(provider="claude", cwd=".") == 0
    assert history_paths == [None]
