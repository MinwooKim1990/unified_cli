"""Stage 0 contracts for the existing Claude, Codex, and agy providers.

These tests intentionally exercise provider implementations through tiny local
executables. They pin the wrapper boundary without depending on vendor CLIs,
authentication, network access, or a user's home-directory configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from unified_cli import (
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    PROVIDERS,
    UnifiedError,
    create,
    route,
)
from unified_cli.core import Attachment


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch):
    # Run the fake executable with no access to the developer's HOME, CLI
    # configuration, credential variables, or Python user site.  Preserve only
    # PATH so ``--version``-style executable lookup semantics remain realistic;
    # the launcher itself pins this test environment's interpreter below.
    inherited_path = os.environ.get("PATH", os.defpath)
    for name in tuple(os.environ):
        monkeypatch.delenv(name, raising=False)
    fake_home = tmp_path / "home"
    fake_tmp = tmp_path / "tmp"
    for directory in (fake_home, fake_tmp):
        directory.mkdir(mode=0o700)
    safe_env = {
        "HOME": str(fake_home),
        "PATH": inherited_path,
        "TMPDIR": str(fake_tmp),
        "XDG_CACHE_HOME": str(fake_home / ".cache"),
        "XDG_CONFIG_HOME": str(fake_home / ".config"),
        "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
    }
    for name, value in safe_env.items():
        monkeypatch.setenv(name, value)

    source = Path(__file__).with_name("fixtures") / "core_provider_cli.py"
    executable = tmp_path / "fake-provider-cli"
    payload = source.read_text(encoding="utf-8")
    payload = payload.replace(
        "#!/usr/bin/env python3", f"#!{sys.executable}", 1,
    )
    executable.write_text(payload, encoding="utf-8")
    executable.chmod(0o700)
    capture = tmp_path / "capture.jsonl"

    def options(provider: str, **extra: str) -> dict[str, Any]:
        env = {
            "FAKE_PROVIDER": provider,
            "FAKE_CAPTURE": str(capture),
            **extra,
        }
        return {"bin_path": str(executable), "extra_env": env}

    def calls() -> list[dict[str, Any]]:
        if not capture.exists():
            return []
        return [json.loads(line) for line in capture.read_text().splitlines()]

    return options, calls


def _agy(fake_cli, monkeypatch, tmp_path: Path, **extra):
    options, _ = fake_cli
    conversations = tmp_path / "conversations"
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    provider = GeminiProvider(
        conversations_dir=str(conversations),
        web_search=False,
        **options(
            "gemini",
            FAKE_CONVERSATIONS_DIR=str(conversations),
            **extra,
        ),
    )
    # Model discovery is a separate contract. Avoid invoking the fake binary's
    # `models` mode while these tests characterize chat argv and parsing.
    provider._validate_model = lambda model: None
    return provider


def test_public_provider_registry_exports_and_antigravity_identity(
    fake_cli, monkeypatch, tmp_path
):
    assert PROVIDERS == {
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "gemini": GeminiProvider,
    }
    assert route("claude/haiku") == ("claude", "haiku")
    assert route("codex/gpt-5.4-mini") == ("codex", "gpt-5.4-mini")
    assert route("gemini/Gemini 3.5 Flash (Medium)") == (
        "gemini", "Gemini 3.5 Flash (Medium)",
    )

    provider = _agy(fake_cli, monkeypatch, tmp_path)
    assert provider.name == "gemini"
    assert Path(provider.bin_path).name == "fake-provider-cli"

    # `agy` is the wrapped executable name, not a fourth public provider key.
    with pytest.raises(UnifiedError) as exc:
        create("agy", bin_path="agy")  # type: ignore[arg-type]
    assert exc.value.kind == "config"


def test_fresh_nonstreaming_argv_and_stdin_contracts(
    fake_cli, monkeypatch, tmp_path
):
    options, calls = fake_cli
    prompt = "fresh contract"

    ClaudeProvider(web_search=False, **options("claude")).chat(prompt)
    assert calls()[-1] == {
        "argv": [
            "-p", "--output-format", "json", "--model",
            "claude-haiku-4-5", prompt,
        ],
        "stdin": "",
    }

    CodexProvider(web_search=False, **options("codex")).chat(prompt)
    assert calls()[-1] == {
        "argv": [
            "exec", "--json", "--skip-git-repo-check", "-m",
            "gpt-5.4-mini", "-s", "read-only", prompt,
        ],
        "stdin": "",
    }

    _agy(fake_cli, monkeypatch, tmp_path).chat(prompt)
    assert calls()[-1] == {
        "argv": [
            "--model", "gemini-3.5-flash", "--print-timeout", "300s",
            "-p", prompt,
        ],
        "stdin": "",
    }


@pytest.mark.parametrize(
    ("provider_name", "prompt", "expected_tail"),
    [
        ("claude", "--version", ["--", "--version"]),
        ("codex", "--help", ["--", "--help"]),
    ],
)
def test_dash_prefixed_prompt_reaches_fake_cli_after_option_sentinel(
    fake_cli, provider_name, prompt, expected_tail
):
    options, calls = fake_cli
    cls = ClaudeProvider if provider_name == "claude" else CodexProvider
    provider = cls(web_search=False, **options(provider_name))

    response = provider.chat(prompt)

    assert response.provider == provider_name
    assert calls()[-1]["argv"][-2:] == expected_tail
    assert calls()[-1]["stdin"] == ""


def test_claude_real_stream_boundary_normalizes_and_deduplicates(fake_cli):
    options, _ = fake_cli
    provider = ClaudeProvider(web_search=False, **options("claude"))

    messages = list(provider.stream("characterize"))

    assert [m.kind for m in messages] == [
        "session", "reasoning", "text", "reasoning", "text", "tool_use",
        "usage", "session", "done",
    ]
    assert [m.text for m in messages if m.kind == "text"] == ["hel", "lo"]
    assert [m.text for m in messages if m.kind == "reasoning"] == ["plan", " more"]
    assert [m.tool for m in messages if m.kind == "tool_use"] == [{
        "name": "Read", "input": {"path": "x"}, "id": "tool-1",
    }]
    usage = next(m.usage for m in messages if m.kind == "usage")
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (11, 5, 2)


def test_codex_real_stream_boundary_normalizes_every_core_event(fake_cli):
    options, _ = fake_cli
    provider = CodexProvider(web_search=False, **options("codex"))

    messages = list(provider.stream("characterize"))

    assert [m.kind for m in messages] == [
        "session", "reasoning", "tool_use", "tool_use", "tool_result",
        "text", "usage", "done",
    ]
    assert [m.text for m in messages if m.kind == "text"] == ["hello from codex"]
    assert [m.tool["name"] for m in messages if m.kind == "tool_use"] == [
        "web_search", "lookup",
    ]
    assert next(m.tool for m in messages if m.kind == "tool_result") == {
        "id": "call-1", "output": "ok", "is_error": False,
    }
    usage = next(m.usage for m in messages if m.kind == "usage")
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (13, 7, 3)


def test_agy_real_stream_boundary_keeps_plaintext_lines_and_recovers_session(
    fake_cli, monkeypatch, tmp_path
):
    provider = _agy(fake_cli, monkeypatch, tmp_path, FAKE_SESSION="agy-stream")

    messages = list(provider.stream("characterize"))

    assert [m.kind for m in messages] == ["text", "text", "session", "done"]
    assert [m.text for m in messages if m.kind == "text"] == [
        "hello from agy\n", "second line\n",
    ]
    assert next(m.session_id for m in messages if m.kind == "session") == "agy-stream"


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_specific_session_resume_preserves_public_session_id_and_argv(
    fake_cli, monkeypatch, tmp_path, provider_name
):
    options, calls = fake_cli
    session = f"{provider_name}-resume"
    if provider_name == "claude":
        provider = ClaudeProvider(
            web_search=False, **options("claude", FAKE_SESSION=session)
        )
    elif provider_name == "codex":
        provider = CodexProvider(
            web_search=False, **options("codex", FAKE_SESSION=session)
        )
    else:
        provider = _agy(
            fake_cli, monkeypatch, tmp_path, FAKE_SESSION=session,
        )

    response = provider.chat("follow up", session_id=session)
    argv = calls()[-1]["argv"]

    assert response.session_id == session
    if provider_name == "claude":
        assert argv[argv.index("--resume") + 1] == session
    elif provider_name == "codex":
        assert argv[0:2] == ["exec", "resume"]
        assert session in argv
    else:
        assert argv[argv.index("--conversation") + 1] == session

    continued = provider.chat("continue latest", resume_last=True)
    continue_argv = calls()[-1]["argv"]
    assert continued.session_id == session
    if provider_name == "claude":
        assert "--continue" in continue_argv
    elif provider_name == "codex":
        assert continue_argv[0:2] == ["exec", "resume"]
        assert "--last" in continue_argv
    else:
        assert "--continue" in continue_argv


def test_codex_silent_fresh_session_on_resume_is_rejected(fake_cli):
    options, _ = fake_cli
    provider = CodexProvider(
        web_search=False,
        **options("codex", FAKE_SESSION="unexpected-new-session"),
    )

    with pytest.raises(UnifiedError) as exc:
        provider.chat("follow up", session_id="requested-session")

    assert exc.value.kind == "not_found"
    assert exc.value.provider == "codex"


def test_claude_image_contract_distinguishes_direct_and_restricted_reads(
    fake_cli, tmp_path
):
    options, calls = fake_cli
    image = tmp_path / "image.png"
    image.write_bytes(b"fixture")
    provider = ClaudeProvider(web_search=False, **options("claude"))

    provider.chat("describe", images=[image])
    call = calls()[-1]
    argv = call["argv"]

    assert call["stdin"] == ""
    assert argv[-2] == "--"
    assert str(image.resolve()) in argv[-1]
    assert argv[argv.index("--allowedTools") + 1] == "Read"

    restricted = ClaudeProvider(
        web_search=False,
        safe_mode=True,
        permission_mode="dontAsk",
        tools=[],
        restrict_image_reads=True,
        **options("claude"),
    )
    restricted.chat("describe", images=[image])
    restricted_argv = calls()[-1]["argv"]
    assert restricted_argv[restricted_argv.index("--tools") + 1] == "Read"
    assert restricted_argv[restricted_argv.index("--allowedTools") + 1] == (
        restricted._read_rule(str(image.resolve()))
    )


def test_codex_image_contract_repeats_i_flags_and_moves_prompt_to_stdin(
    fake_cli, tmp_path
):
    options, calls = fake_cli
    first = tmp_path / "one.png"
    second = tmp_path / "two.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    provider = CodexProvider(web_search=False, **options("codex"))

    provider.chat("describe", images=[first, second])
    call = calls()[-1]
    argv = call["argv"]

    assert call["stdin"] == "describe"
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "-i"] == [
        str(first), str(second),
    ]
    assert "describe" not in argv


def test_agy_image_contract_uses_absolute_at_ref_without_permission_bypass(
    fake_cli, monkeypatch, tmp_path
):
    _, calls = fake_cli
    image = tmp_path / "image.png"
    image.write_bytes(b"fixture")
    provider = _agy(fake_cli, monkeypatch, tmp_path)

    provider.chat("describe", images=[Attachment(path=str(image))])
    argv = calls()[-1]["argv"]
    prompt = argv[argv.index("-p") + 1]

    assert prompt == f"@{image.resolve()} describe"
    assert "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize(
    ("provider_name", "response_mode", "expected_kind"),
    [
        ("claude", "is_error", "internal"),
        ("codex", "structured_error", "internal"),
        ("gemini", "ineligible", "auth_expired"),
    ],
)
def test_success_exit_provider_error_envelopes_are_not_reported_as_success(
    fake_cli, monkeypatch, tmp_path, provider_name, response_mode, expected_kind
):
    options, _ = fake_cli
    if provider_name == "claude":
        provider = ClaudeProvider(
            web_search=False,
            **options("claude", FAKE_RESPONSE=response_mode),
        )
    elif provider_name == "codex":
        provider = CodexProvider(
            web_search=False,
            **options("codex", FAKE_RESPONSE=response_mode),
        )
    else:
        provider = _agy(
            fake_cli, monkeypatch, tmp_path, FAKE_RESPONSE=response_mode,
        )

    with pytest.raises(UnifiedError) as exc:
        provider.chat("fail deterministically")

    assert exc.value.kind == expected_kind
    assert exc.value.provider == provider_name


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_nonzero_auth_failures_are_classified_without_api_key_fallback(
    fake_cli, monkeypatch, tmp_path, provider_name
):
    options, _ = fake_cli
    if provider_name == "claude":
        provider = ClaudeProvider(
            web_search=False,
            **options("claude", FAKE_RESPONSE="exit_auth"),
        )
    elif provider_name == "codex":
        provider = CodexProvider(
            web_search=False,
            **options("codex", FAKE_RESPONSE="exit_auth"),
        )
    else:
        provider = _agy(
            fake_cli, monkeypatch, tmp_path, FAKE_RESPONSE="exit_auth",
        )

    with pytest.raises(UnifiedError) as exc:
        provider.chat("fail with nonzero exit")

    assert exc.value.kind == "auth_expired"
    assert exc.value.provider == provider_name


def test_server_policy_rejects_explicit_agentic_provider_prefixes_before_state(
    monkeypatch,
):
    from fastapi import HTTPException
    from unified_cli import server

    monkeypatch.delenv("UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS", raising=False)
    monkeypatch.setattr(
        server,
        "_acquire_conversation",
        lambda user: pytest.fail("server state must not be acquired"),
    )

    for model in ("codex/gpt-5.4-mini", "gemini/gemini-3.5-flash"):
        request = server.ChatRequest(
            model=model,
            messages=[server.ChatMessage(role="user", content="hello")],
        )
        with pytest.raises(HTTPException) as exc:
            server.chat_completions(request)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "provider_disabled_for_server"


def test_cli_chat_keeps_stdout_clean_and_persists_v2_settings_and_v1_session_state(
    fake_cli, monkeypatch, tmp_path, capsys
):
    from unified_cli import cli, settings, state

    options, _ = fake_cli
    provider = CodexProvider(
        web_search=False,
        cwd=str(tmp_path),
        **options("codex", FAKE_SESSION="cli-session"),
    )
    state_dir = tmp_path / "state-home"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(cli, "create", lambda *args, **kwargs: provider)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: False)
    settings.set("default_provider", "codex")

    assert cli.main(["chat", "compatibility", "--cwd", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(state.STATE_FILE.read_text())
    settings_payload = json.loads(settings.SETTINGS_FILE.read_text())

    assert captured.out == "hello from codex\n"
    assert settings_payload == {
        "version": 2,
        "settings": {
            "lang": None,
            "default_provider": "codex",
            "reasoning_display": "hidden",
            "tool_display": "compact",
            "theme": "auto",
            "cross_provider_context_enabled": True,
            "context_window": 8,
            "repl_permission": "provider_default",
            "browser_permission": "read_only",
            "browser_prompt_preview": False,
            "style": None,
            "effort": None,
            "reasoning_mode": None,
            "system_prompt": None,
            "timeout": None,
            "tools": None,
            "mcp": None,
            "web": None,
            "workspace": None,
            "additional_dirs": [],
            "multiline": True,
            "provider_settings": {},
        },
    }
    assert payload["version"] == 1
    assert payload["last_session"] | {"updated_at": 0} == {
        "provider": "codex",
        "model": "gpt-5.4-mini",
        "session_id": "cli-session",
        "cwd": str(tmp_path.resolve()),
        "updated_at": 0,
    }
    assert payload["last_session"]["updated_at"] > 0


def test_cli_stream_keeps_stdout_payload_only(
    fake_cli, monkeypatch, tmp_path, capsys
):
    from unified_cli import cli, state

    options, _ = fake_cli
    provider = CodexProvider(
        web_search=False,
        cwd=str(tmp_path),
        **options("codex", FAKE_SESSION="stream-session"),
    )
    state_dir = tmp_path / "stream-state-home"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(cli, "create", lambda *args, **kwargs: provider)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: False)

    assert cli.main([
        "chat", "compatibility", "--stream", "--cwd", str(tmp_path),
    ]) == 0

    assert capsys.readouterr().out == "hello from codex\n"
