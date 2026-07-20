"""Focused tests for the Stage 3 REPL architecture and safety boundaries."""

from __future__ import annotations

import asyncio
import io
import os
import stat
from types import SimpleNamespace

import pytest
from rich.console import Console

from unified_cli import repl
from unified_cli import i18n
from unified_cli import conversation as conversation_module
from unified_cli.conversation import UnifiedConversation
from unified_cli.core import Message
from unified_cli.event_renderer import EventRenderer, safe_terminal_text
from unified_cli.providers.claude import ClaudeProvider
from unified_cli.providers.codex import CodexProvider
from unified_cli.repl_commands import CORE_AUTH_SPECS, DEFAULT_REGISTRY
from unified_cli.repl_state import ReplState
from unified_cli.settings import Settings


OLD_COMMANDS = {
    "/help", "/model", "/provider", "/image", "/images", "/clear-images",
    "/new", "/resume", "/save", "/history", "/tokens", "/doctor",
    "/status", "/lang", "/exit",
}


def _recording_console():
    target = io.StringIO()
    return Console(file=target, color_system=None, width=120), target


def test_registry_preserves_old_commands_and_aliases():
    names = set(DEFAULT_REGISTRY.names())
    assert OLD_COMMANDS <= names
    assert DEFAULT_REGISTRY.resolve("/tokens").name == "/usage"
    assert DEFAULT_REGISTRY.resolve("/quit").name == "/exit"
    for command in (
        "/auth", "/settings", "/reasoning", "/permissions", "/sessions",
        "/fork", "/compact", "/diff", "/theme", "/multiline",
    ):
        assert DEFAULT_REGISTRY.resolve(command) is not None


def test_registry_dispatch_retains_multiword_argument():
    seen = []
    result = DEFAULT_REGISTRY.dispatch(
        "/model Gemini 3.5 Flash (Medium)",
        lambda spec, invoked, arg: seen.append((spec.name, invoked, arg)) or False,
    )
    assert result.handled and not result.exit_requested
    assert seen == [("/model", "/model", "Gemini 3.5 Flash (Medium)")]


def test_repl_state_round_trips_legacy_dicts():
    current = {"provider": "claude", "model": "haiku"}
    options = {"cwd": "/tmp", "web_search": False}
    pending = ["image.png"]
    state = ReplState.from_legacy(current, options, pending)
    state.context_window = 12
    state.timeout = 45
    state.sync_legacy(current, options)
    assert current["context_window"] == 12
    assert options["timeout"] == 45
    assert state.pending_images is pending


def test_event_renderer_escapes_controls_and_rich_markup():
    output, target = _recording_console()
    renderer = EventRenderer(output)
    renderer.render(Message(kind="text", provider="x", text="[red]x[/red]\x1b[2J"))
    renderer.finish()
    rendered = target.getvalue()
    assert "[red]x[/red]" in rendered
    assert "\x1b" not in rendered


def test_event_renderer_correlates_tools_without_printing_payloads():
    times = iter((10.0, 10.25))
    output, target = _recording_console()
    renderer = EventRenderer(output, clock=lambda: next(times))
    renderer.render(Message(
        kind="tool_use", provider="x",
        tool={"id": "abc", "name": "Read[secret]", "input": "TOKEN"},
    ))
    renderer.render(Message(
        kind="tool_result", provider="x",
        tool={"id": "abc", "output": "PRIVATE", "is_error": False},
    ))
    rendered = target.getvalue()
    assert "tool started: Read[secret]" in rendered
    assert "tool completed: Read[secret] (0.25s)" in rendered
    assert "TOKEN" not in rendered and "PRIVATE" not in rendered


def test_reasoning_is_hidden_unless_explicitly_public_summary():
    output, target = _recording_console()
    renderer = EventRenderer(output, show_reasoning_summaries=True)
    renderer.render(Message(kind="reasoning", provider="x", text="raw chain"))
    renderer.render(Message(
        kind="reasoning", provider="x", text="safe summary",
        raw={"public_summary": True},
    ))
    rendered = target.getvalue()
    assert "raw chain" not in rendered
    assert "safe summary" in rendered


def test_partial_then_final_text_is_not_duplicated():
    output, target = _recording_console()
    renderer = EventRenderer(output)
    renderer.render(Message(
        kind="text", provider="x", text="Hel",
        raw={"type": "stream_event", "partial": True},
    ))
    renderer.render(Message(
        kind="text", provider="x", text="Hello",
        raw={"type": "assistant", "final": True},
    ))
    renderer.finish()
    assert target.getvalue().strip() == "Hello"


def test_event_flood_is_bounded():
    output, target = _recording_console()
    renderer = EventRenderer(output, max_events=1)
    renderer.render(Message(kind="text", provider="x", text="one"))
    renderer.render(Message(kind="text", provider="x", text="two"))
    renderer.render(Message(kind="text", provider="x", text="three"))
    assert target.getvalue().count("Further provider events") == 1


def test_korean_renderer_and_permission_block_are_localized(monkeypatch):
    i18n.set_lang("ko")
    try:
        output, target = _recording_console()
        renderer = EventRenderer(output, show_reasoning_summaries=True)
        renderer.render(Message(
            kind="reasoning", provider="x", text="safe summary",
            raw={"public_summary": True},
        ))
        assert "추론 요약" in target.getvalue()

        output, target = _recording_console()
        monkeypatch.setattr(repl, "console", output)
        state = ReplState(provider="gemini", model="x", permission_mode="read_only")
        assert repl._turn_capabilities_supported(state, "gemini") is False
        rendered = target.getvalue()
        assert "읽기 전용" in rendered
        assert "This provider has no enforceable" not in rendered
        assert repl._localized_setting_value("reasoning", "public summaries", state) == "숨김"
        assert repl._localized_setting_value("web", "default", state) == "기본값"
    finally:
        i18n.set_lang(None)


def test_auth_runner_uses_fixed_argv_shell_false_and_minimal_env(monkeypatch):
    calls = []
    terminated = []

    class Process:
        def __init__(self):
            self.stdout = io.BytesIO(b"Logged in\n")
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(repl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        repl, "_popen_process_group_kwargs", lambda: {"start_new_session": True}
    )
    monkeypatch.setattr(
        repl, "_terminate_process_tree",
        lambda process, **kwargs: terminated.append((process, kwargs)),
    )
    repl._run_core_auth("codex", "status")
    argv, kwargs = calls[0]
    assert tuple(argv) == CORE_AUTH_SPECS["codex"]["status"]
    assert kwargs["shell"] is False and kwargs["cwd"] is None
    assert kwargs["start_new_session"] is True
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert terminated and terminated[-1][1] == {"force_group": True}


def test_auth_mutation_requires_confirmation(monkeypatch):
    monkeypatch.setattr(repl, "_interactive", lambda: True)
    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: False)
    monkeypatch.setattr(
        repl.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    repl._run_core_auth("claude", "logout")


def test_rename_uses_lazy_session_manager_keyword_api(monkeypatch):
    calls = []

    class Manager:
        def get(self, **kwargs):
            calls.append(("get", kwargs))
            return object()

        def rename(self, **kwargs):
            calls.append(("rename", kwargs))

    conv = SimpleNamespace(
        sessions={"claude": "session-1"}, turns=[], _clients={}, _locked_provider=None,
        context_window=8,
    )
    current = {"provider": "claude", "model": "haiku"}
    monkeypatch.setattr(repl, "_load_session_manager", lambda: Manager())
    repl._handle_slash("/rename useful name", conv, current, {}, [], False)
    assert calls == [
        ("get", {"provider": "claude", "session_id": "session-1"}),
        ("rename", {
            "provider": "claude", "session_id": "session-1", "name": "useful name",
        }),
    ]


def test_turn_cancellation_explicitly_closes_iterator(monkeypatch):
    class CancellingIterator:
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    iterator = CancellingIterator()
    conv = SimpleNamespace(stream=lambda *args, **kwargs: iterator, sessions={})
    monkeypatch.setattr(repl, "console", Console(file=io.StringIO(), color_system=None))
    repl._run_turn(conv, {"provider": "claude", "model": "haiku"}, "hi")
    assert iterator.closed is True


def test_history_refuses_symlink_file_and_final_directory(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    history_dir = tmp_path / "history-dir"
    history_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("sentinel", encoding="utf-8")
    linked_file = history_dir / "history"
    try:
        linked_file.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert repl._private_history_path(linked_file) is None
    assert outside.read_text(encoding="utf-8") == "sentinel"

    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    assert repl._private_history_path(linked_dir / "history") is None
    assert not (real_dir / "history").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_private_history_and_export_are_owner_only_from_creation(tmp_path, monkeypatch):
    history = tmp_path / "private" / "history"
    assert repl._private_history_path(history) == history
    assert stat.S_IMODE(history.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(history.stat().st_mode) == 0o600

    target = tmp_path / "transcript.json"
    observed_modes = []
    real_dump = repl.json.dump

    def checking_dump(payload, handle, **kwargs):
        observed_modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return real_dump(payload, handle, **kwargs)

    monkeypatch.setattr(repl.json, "dump", checking_dump)
    repl._write_private_json_new(target, {"secret": "value"})
    assert observed_modes == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_export_refuses_symlink_and_cleans_partial_failure(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    target = tmp_path / "export.json"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileExistsError):
        repl._write_private_json_new(target, {"secret": "value"})
    assert outside.read_text(encoding="utf-8") == "sentinel"

    partial = tmp_path / "partial.json"
    monkeypatch.setattr(
        repl.json, "dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected")),
    )
    with pytest.raises(ValueError, match="injected"):
        repl._write_private_json_new(partial, {"secret": "value"})
    assert not partial.exists()


def test_safe_git_diff_uses_fixed_hardened_argv_every_time(tmp_path, monkeypatch):
    calls = []

    def fake_bounded(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return "", False, True

    monkeypatch.setattr(repl, "_bounded_process_output", fake_bounded)
    assert repl._safe_git_diff(str(tmp_path)) == ("", False, True)
    assert len(calls) == 3
    prefix = (
        "git", "--no-pager", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=" + os.devnull,
    )
    assert all(argv[:len(prefix)] == prefix for argv, _kwargs in calls)
    assert "--no-ext-diff" in calls[0][0] and "--no-textconv" in calls[0][0]
    assert "--no-ext-diff" in calls[1][0] and "--no-textconv" in calls[1][0]
    assert "ls-files" in calls[2][0]


def test_unexpected_stream_error_finishes_renderer(monkeypatch):
    events = []

    class Renderer:
        text_started = False

        def __init__(self, *args, **kwargs):
            pass

        def render(self, message):
            events.append(("render", message.text))
            self.text_started = True

        def finish(self):
            events.append(("finish", None))

    def broken_stream(*args, **kwargs):
        yield Message(kind="text", provider="x", text="partial")
        raise RuntimeError("provider secret")

    conv = SimpleNamespace(stream=broken_stream, sessions={})
    monkeypatch.setattr(repl, "EventRenderer", Renderer)
    monkeypatch.setattr(repl, "console", Console(file=io.StringIO(), color_system=None))
    with pytest.raises(RuntimeError, match="provider secret"):
        repl._run_turn(conv, {"provider": "claude", "model": "haiku"}, "hi")
    assert events == [("render", "partial"), ("finish", None)]


def test_on_exit_ignores_invalid_opaque_session_data(monkeypatch):
    monkeypatch.setattr(
        repl, "save_last_session",
        lambda **kwargs: (_ for _ in ()).throw(TypeError("opaque session")),
    )
    monkeypatch.setattr(repl, "console", Console(file=io.StringIO(), color_system=None))
    conv = SimpleNamespace(sessions={"claude": object()})
    repl._on_exit(conv, {"provider": "claude", "model": "haiku"})


def test_safe_terminal_text_handles_korean_and_controls():
    assert safe_terminal_text("안녕하세요\x00") == "안녕하세요�"


def test_provider_capabilities_create_exact_scoped_kwargs_and_merge_codex_config(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_create(provider, **kwargs):
        calls.append((provider, kwargs))
        return object()

    monkeypatch.setattr(conversation_module, "create", fake_create)
    conv = UnifiedConversation(
        provider_opts={"web_search": False, "cwd": str(tmp_path)},
        provider_opts_by_provider={
            "codex": {"config_overrides": {"tools.web_search": False}},
        },
    )
    state = ReplState(
        provider="claude", model="haiku", permission_mode="read_only",
        effort="max", system_prompt="Be precise.", added_dirs=[str(tmp_path)],
    )
    repl._apply_provider_capabilities(state, conv)

    conv._get_client("claude", "haiku")
    conv._get_client("codex", "gpt")
    assert calls == [
        ("claude", {
            "model": "haiku", "web_search": False, "cwd": str(tmp_path),
            "permission_mode": "plan", "effort": "max",
            "system_prompt": "Be precise.", "add_dirs": [str(tmp_path)],
        }),
        ("codex", {
            "model": "gpt", "web_search": False, "cwd": str(tmp_path),
            "sandbox": "read-only", "add_dirs": [str(tmp_path)],
            "config_overrides": {
                "tools.web_search": False,
                "sandbox_mode": "read-only",
                "model_reasoning_effort": "max",
                "developer_instructions": "Be precise.",
                "sandbox_workspace_write.writable_roots": [str(tmp_path)],
            },
        }),
    ]


def test_capability_reapply_removes_stale_mapped_options_without_clobbering_web():
    conv = UnifiedConversation(provider_opts_by_provider={
        "claude": {
            "permission_mode": "plan", "effort": "high",
            "system_prompt": "old", "add_dirs": ["/old"], "safe_mode": True,
        },
        "codex": {
            "sandbox": "workspace-write", "add_dirs": ["/old"],
            "config_overrides": {
                "tools.web_search": True, "model_reasoning_effort": "high",
                "personality": "friendly", "developer_instructions": "old",
                "sandbox_mode": "workspace-write",
                "sandbox_workspace_write.writable_roots": ["/old"],
            },
        },
        "gemini": {"add_dirs": ["/old"], "sandbox": True},
    })
    state = ReplState(provider="codex", model="gpt")
    repl._apply_provider_capabilities(state, conv)
    assert conv.provider_opts_by_provider == {
        "claude": {"safe_mode": True},
        "codex": {"config_overrides": {"tools.web_search": True}},
        "gemini": {"sandbox": True},
    }


@pytest.mark.parametrize("permission,sandbox", [
    ("read_only", "read-only"),
    ("workspace_write", "workspace-write"),
])
def test_codex_permission_and_add_dirs_apply_to_fresh_and_resume_argv(
    permission, sandbox, tmp_path,
):
    existing = tmp_path / 'extra "root"'
    existing.mkdir()
    conv = UnifiedConversation(provider_opts_by_provider={
        "codex": {"config_overrides": {"tools.web_search": False}},
    })
    state = ReplState(
        provider="codex", model="gpt", permission_mode=permission,
        added_dirs=[str(existing)],
    )
    repl._apply_provider_capabilities(state, conv)
    provider = CodexProvider(
        bin_path="codex", web_search=False,
        **conv.provider_opts_by_provider["codex"],
    )
    fresh, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    resumed, _ = provider._build_args(
        "hello", session_id="session-1", resume_last=False,
        model=None, streaming=False,
    )
    fresh_config = [fresh[index + 1] for index, value in enumerate(fresh) if value == "-c"]
    resume_config = [
        resumed[index + 1] for index, value in enumerate(resumed) if value == "-c"
    ]
    sandbox_config = 'sandbox_mode="' + sandbox + '"'
    roots_config = (
        "sandbox_workspace_write.writable_roots="
        + provider._toml_literal([str(existing)])
    )
    assert fresh[fresh.index("-s") + 1] == sandbox
    assert sandbox_config in fresh_config and sandbox_config in resume_config
    assert roots_config in fresh_config and roots_config in resume_config
    assert '\\"root\\"' in roots_config
    assert fresh[fresh.index("--add-dir") + 1] == str(existing)
    assert "--add-dir" not in resumed
    assert 'tools.web_search=false' in fresh_config
    assert 'tools.web_search=false' in resume_config


@pytest.mark.parametrize("provider,state_kwargs", [
    ("gemini", {"permission_mode": "read_only"}),
    ("claude", {"permission_mode": "workspace_write"}),
    ("extension", {"effort": "high"}),
    ("claude", {"style": "friendly"}),
    ("gemini", {"system_prompt": "instruction"}),
])
def test_unsupported_capability_stops_before_stream(
    provider, state_kwargs, monkeypatch,
):
    called = []
    conv = SimpleNamespace(
        stream=lambda *args, **kwargs: called.append((args, kwargs)), sessions={},
        provider_opts={}, provider_opts_by_provider={}, context_window=8,
    )
    state = ReplState(provider=provider, model="model", **state_kwargs)
    output, target = _recording_console()
    monkeypatch.setattr(repl, "console", output)
    repl._run_turn(
        conv, {"provider": provider, "model": "model"}, "hello", repl_state=state,
    )
    assert called == []
    assert "unavailable" in target.getvalue().lower()


def test_permissions_confirmation_and_full_rejection(monkeypatch):
    saved = []
    conv = SimpleNamespace(
        context_window=8, provider_opts_by_provider={}, _clients={"stale": object()},
    )
    current = {"provider": "codex", "model": "gpt", "permission_mode": "read_only"}
    state = ReplState(provider="codex", model="gpt", permission_mode="read_only")
    monkeypatch.setattr(repl.settings, "set", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(repl, "_interactive", lambda: False)
    repl._handle_slash(
        "/permissions workspace_write", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "read_only" and saved == []

    monkeypatch.setattr(repl, "_interactive", lambda: True)
    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: False)
    repl._handle_slash(
        "/permissions provider_default", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "read_only" and saved == []

    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: True)
    repl._handle_slash(
        "/permissions workspace_write", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "workspace_write"
    assert saved == [("repl_permission", "workspace_write")]
    assert conv._clients == {}

    repl._handle_slash(
        "/permissions full", conv, current, {}, [], False, repl_state=state,
    )
    assert state.permission_mode == "workspace_write"
    assert all(value != "full" for _key, value in saved)

    # provider_default also needs confirmation when leaving workspace_write.
    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: False)
    repl._handle_slash(
        "/permissions provider_default", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "workspace_write"


def test_provider_default_to_workspace_write_requires_approval(monkeypatch):
    saved = []
    stale_client = object()
    conv = SimpleNamespace(
        context_window=8, provider_opts_by_provider={},
        _clients={"stale": stale_client},
    )
    current = {
        "provider": "codex", "model": "gpt",
        "permission_mode": "provider_default",
    }
    state = ReplState(
        provider="codex", model="gpt", permission_mode="provider_default",
    )
    monkeypatch.setattr(repl.settings, "set", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(repl, "_interactive", lambda: True)
    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: False)
    repl._handle_slash(
        "/permissions workspace_write", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "provider_default"
    assert saved == []
    assert conv._clients == {"stale": stale_client}

    monkeypatch.setattr(repl, "_confirm_action", lambda prompt: True)
    repl._handle_slash(
        "/permissions workspace_write", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.permission_mode == "workspace_write"
    assert saved == [("repl_permission", "workspace_write")]
    assert conv._clients == {}


def test_default_and_clear_recover_on_unsupported_provider(monkeypatch, tmp_path):
    saved = []
    conv = SimpleNamespace(
        context_window=8, provider_opts_by_provider={
            "claude": {"effort": "high", "system_prompt": "old", "add_dirs": [str(tmp_path)]},
            "codex": {"config_overrides": {
                "model_reasoning_effort": "high", "personality": "friendly",
                "developer_instructions": "old",
            }, "add_dirs": [str(tmp_path)]},
            "gemini": {"add_dirs": [str(tmp_path)]},
        },
        _clients={"stale": object()},
    )
    current = {"provider": "extension", "model": "model"}
    state = ReplState(
        provider="extension", model="model", style="friendly", effort="high",
        system_prompt="old", added_dirs=[str(tmp_path)], web_search=False,
        web_explicit=True,
    )
    monkeypatch.setattr(repl.settings, "set", lambda key, value: saved.append((key, value)))
    for command in (
        "/style default", "/effort default", "/system clear",
        "/add-dir clear", "/web default",
    ):
        repl._handle_slash(
            command, conv, current, {}, [], False, repl_state=state,
        )
    assert (state.style, state.effort, state.system_prompt, state.added_dirs) == (
        "default", "default", None, [],
    )
    assert state.web_explicit is False
    assert saved == [
        ("style", None), ("effort", None), ("system_prompt", None),
        ("additional_dirs", []), ("web", None),
    ]
    assert conv.provider_opts_by_provider == {}


def test_system_validation_status_does_not_echo_content(monkeypatch):
    output, target = _recording_console()
    monkeypatch.setattr(repl, "console", output)
    monkeypatch.setattr(repl.settings, "set", lambda *args: None)
    conv = SimpleNamespace(context_window=8, provider_opts_by_provider={}, _clients={})
    current = {"provider": "claude", "model": "haiku"}
    state = ReplState(provider="claude", model="haiku")
    secret = "first\tline\r\nsecond"
    repl._handle_slash(
        "/system " + secret, conv, current, {}, [], False, repl_state=state,
    )
    assert state.system_prompt == secret
    assert secret not in target.getvalue()
    assert str(len(secret)) in target.getvalue()
    repl._handle_slash(
        "/system bad\x00value", conv, current, {}, [], False, repl_state=state,
    )
    assert state.system_prompt == secret


def test_add_dir_canonical_dedupes_and_image_requires_regular_file(
    monkeypatch, tmp_path,
):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        link = real
    conv = SimpleNamespace(context_window=8, provider_opts_by_provider={}, _clients={})
    current = {"provider": "claude", "model": "haiku"}
    state = ReplState(provider="claude", model="haiku")
    monkeypatch.setattr(repl.settings, "set", lambda *args: None)
    repl._handle_slash(
        "/add-dir " + str(link), conv, current, {}, [], False, repl_state=state,
    )
    repl._handle_slash(
        "/add-dir " + str(real), conv, current, {}, [], False, repl_state=state,
    )
    assert state.added_dirs == [str(real.resolve())]

    repl._handle_slash(
        "/image " + str(real), conv, current, {}, [], False, repl_state=state,
    )
    assert state.pending_images == []
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    repl._handle_slash(
        "/image " + str(image), conv, current, {}, [], False, repl_state=state,
    )
    assert state.pending_images == [str(image.resolve())]


def test_saved_preferences_apply_cross_context_and_capabilities(tmp_path):
    conv = UnifiedConversation(provider_opts={"web_search": True})
    state = ReplState(provider="codex", model="gpt")
    saved = Settings(
        cross_provider_context_enabled=False, repl_permission="workspace_write",
        effort="xhigh", style="pragmatic", system_prompt="instruction",
        additional_dirs=[str(tmp_path)], web=False,
    )
    repl._apply_saved_preferences(state, conv, saved)
    assert conv.cross_provider_context is False
    assert state.web_search is False and state.web_explicit is True
    assert conv.provider_opts_by_provider["codex"] == {
        "sandbox": "workspace-write", "add_dirs": [str(tmp_path)],
        "config_overrides": {
            "sandbox_mode": "workspace-write",
            "model_reasoning_effort": "xhigh", "personality": "pragmatic",
            "developer_instructions": "instruction",
            "sandbox_workspace_write.writable_roots": [str(tmp_path)],
        },
    }


def test_model_validation_preserves_safe_multiword_and_rejects_controls(monkeypatch):
    conv = SimpleNamespace(context_window=8)
    current = {"provider": "gemini", "model": "old"}
    state = ReplState(provider="gemini", model="old")
    repl._handle_slash(
        "/model Gemini 3.5 Flash (Medium)", conv, current, {}, [], False,
        repl_state=state,
    )
    assert state.model == "Gemini 3.5 Flash (Medium)"
    for unsafe in ("x\nnew", "x\u2028new", "x\ud800"):
        repl._handle_slash(
            "/model " + unsafe, conv, current, {}, [], False, repl_state=state,
        )
    assert state.model == "Gemini 3.5 Flash (Medium)"


def test_explicit_web_fails_closed_but_default_does_not(monkeypatch):
    calls = []
    conv = SimpleNamespace(
        stream=lambda *args, **kwargs: calls.append(1) or iter(()), sessions={},
        provider_opts={"web_search": False}, provider_opts_by_provider={},
        context_window=8,
    )
    output, _target = _recording_console()
    monkeypatch.setattr(repl, "console", output)
    state = ReplState(
        provider="gemini", model="model", web_search=False, web_explicit=True,
    )
    repl._run_turn(
        conv, {"provider": "gemini", "model": "model"}, "hi", repl_state=state,
    )
    assert calls == []
    state.web_explicit = False
    repl._run_turn(
        conv, {"provider": "gemini", "model": "model"}, "hi", repl_state=state,
    )
    assert calls == [1]


def test_claude_effort_is_strict_and_emitted():
    provider = ClaudeProvider(bin_path="claude", web_search=False, effort="xhigh")
    argv, _stdin = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    assert argv[argv.index("--effort") + 1] == "xhigh"
    with pytest.raises(ValueError, match="Claude effort"):
        ClaudeProvider(bin_path="claude", effort="high\n--dangerous")


@pytest.mark.parametrize("events", [
    [
        Message(
            kind="text", provider="claude", text="Hel",
            raw={"type": "stream_event", "partial": True},
        ),
        Message(
            kind="text", provider="claude", text="Hello",
            raw={"type": "assistant", "final": True},
        ),
    ],
    [
        Message(kind="text", provider="claude", text="Hel"),
        Message(kind="text", provider="claude", text="lo"),
    ],
])
def test_sync_stream_records_only_novel_assistant_text(events):
    class Client:
        def stream(self, *args, **kwargs):
            yield from events

    conv = UnifiedConversation(default_provider="claude")
    conv._get_client = lambda provider, model: Client()  # type: ignore[method-assign]
    yielded = list(conv.stream("hi", provider="claude"))
    assert yielded == events
    assert conv.turns[0].text == "Hello"
    assert "HelHello" not in conv._context_prefix_if_switch("codex")


def test_sync_stream_keeps_unrelated_explicit_final_block():
    events = [
        Message(kind="text", provider="claude", text="first "),
        Message(
            kind="text", provider="claude", text="second",
            raw={"type": "assistant", "final": True},
        ),
    ]

    class Client:
        def stream(self, *args, **kwargs):
            yield from events

    conv = UnifiedConversation(default_provider="claude")
    conv._get_client = lambda provider, model: Client()  # type: ignore[method-assign]
    list(conv.stream("hi", provider="claude"))
    assert conv.turns[0].text == "first second"


def test_async_stream_deduplicates_final_and_records_partial_on_early_close():
    class Client:
        async def astream(self, *args, **kwargs):
            yield Message(
                kind="text", provider="claude", text="Hel",
                raw={"type": "stream_event", "partial": True},
            )
            yield Message(
                kind="text", provider="claude", text="Hello",
                raw={"type": "response.completed"},
            )

    async def consume_all() -> None:
        conv = UnifiedConversation(default_provider="claude")
        conv._get_client = lambda provider, model: Client()  # type: ignore[method-assign]
        yielded = [message async for message in conv.astream("hi", provider="claude")]
        assert [message.text for message in yielded] == ["Hel", "Hello"]
        assert conv.turns[0].text == "Hello"

    async def close_early() -> None:
        conv = UnifiedConversation(default_provider="claude")
        conv._get_client = lambda provider, model: Client()  # type: ignore[method-assign]
        stream = conv.astream("hi", provider="claude")
        first = await stream.__anext__()
        assert first.text == "Hel"
        await stream.aclose()
        assert conv.turns[0].text == "Hel"

    asyncio.run(consume_all())
    asyncio.run(close_early())
