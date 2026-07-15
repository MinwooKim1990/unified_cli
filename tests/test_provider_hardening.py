"""Focused safety and compatibility tests for provider command construction."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _config_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"]


def _option_value(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def test_agy_discovery_never_selects_legacy_gemini(monkeypatch):
    from unified_cli import discovery

    monkeypatch.delenv("AGY_CLI_PATH", raising=False)
    monkeypatch.setenv("GEMINI_CLI_PATH", "/legacy/gemini")
    monkeypatch.setattr(
        discovery.os.path, "isfile", lambda path: path == "/legacy/gemini"
    )
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: True)
    monkeypatch.setattr(
        discovery.shutil,
        "which",
        lambda name: "/usr/bin/gemini" if name == "gemini" else None,
    )

    assert discovery.find_agy_bin() is None
    assert discovery.find_gemini_bin() is None


def test_agy_discovery_accepts_explicit_agy_path(monkeypatch):
    from unified_cli import discovery

    monkeypatch.setenv("AGY_CLI_PATH", "/opt/agy")
    monkeypatch.setattr(
        discovery.os.path, "isfile", lambda path: path == "/opt/agy"
    )
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: True)

    assert discovery.find_agy_bin() == "/opt/agy"
    assert discovery.find_gemini_bin() == "/opt/agy"


def test_claude_web_search_does_not_imply_permission_bypass():
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(bin_path="claude", web_search=True)
    argv, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )

    allowed = argv[argv.index("--allowedTools") + 1]
    assert "WebSearch" in allowed
    assert "WebFetch" in allowed
    assert "--permission-mode" not in argv


def test_claude_image_keeps_default_permissions_and_explicit_override(tmp_path):
    from unified_cli.providers.claude import ClaudeProvider

    image = tmp_path / "image.png"
    image.write_bytes(b"not-read-by-this-unit-test")

    safe = ClaudeProvider(bin_path="claude", web_search=False)
    safe_argv, _ = safe._build_args(
        "describe", session_id=None, resume_last=False, model=None,
        streaming=False, images=[str(image)],
    )
    safe_allowed = safe_argv[safe_argv.index("--allowedTools") + 1]
    assert "Read" in safe_allowed
    assert "--permission-mode" not in safe_argv

    explicit = ClaudeProvider(
        bin_path="claude", web_search=False, permission_mode="bypassPermissions"
    )
    explicit_argv, _ = explicit._build_args(
        "describe", session_id=None, resume_last=False, model=None,
        streaming=False, images=[str(image)],
    )
    assert explicit_argv[explicit_argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_restricted_claude_profile_has_no_text_tools_and_scopes_image_read(tmp_path):
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(
        bin_path="claude",
        web_search=False,
        safe_mode=True,
        permission_mode="dontAsk",
        tools=[],
        restrict_image_reads=True,
    )
    text_argv, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    assert "--safe-mode" in text_argv
    assert _option_value(text_argv, "--permission-mode") == "dontAsk"
    assert _option_value(text_argv, "--tools") == ""
    assert "--allowedTools" not in text_argv
    assert "WebSearch" not in text_argv and "Bash" not in text_argv

    image = tmp_path / "incoming.png"
    image.write_bytes(b"not-read-by-this-unit-test")
    image_argv, _ = provider._build_args(
        "describe", session_id=None, resume_last=False, model=None,
        streaming=False, images=[str(image)],
    )
    assert _option_value(image_argv, "--tools") == "Read"
    read_rule = _option_value(image_argv, "--allowedTools")
    assert read_rule == provider._read_rule(str(image))
    assert read_rule != "Read"
    assert "WebSearch" not in read_rule and "Bash" not in read_rule


def test_restricted_claude_profile_resolves_symlink_before_scoping(tmp_path):
    from unified_cli.providers.claude import ClaudeProvider

    target = tmp_path / "target.png"
    target.write_bytes(b"image")
    link = tmp_path / "link.png"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this test filesystem")
    assert ClaudeProvider._read_rule(str(link)) == ClaudeProvider._read_rule(str(target))


def test_restricted_claude_byte_image_uses_the_same_canonical_prompt_and_rule():
    from unified_cli.providers.claude import ClaudeProvider
    import os

    provider = ClaudeProvider(
        bin_path="claude",
        web_search=False,
        safe_mode=True,
        permission_mode="dontAsk",
        tools=[],
        restrict_image_reads=True,
    )
    scope = provider._new_temp_scope()
    try:
        argv, _ = provider._build_args_in_temp_scope(
            scope,
            "describe",
            session_id=None,
            resume_last=False,
            model=None,
            streaming=False,
            images=[b"raw-image-bytes"],
        )
        assert len(scope.files) == 1
        canonical = os.path.realpath(os.path.abspath(scope.files[0]))
        assert canonical in argv[-1]
        assert _option_value(argv, "--allowedTools") == provider._read_rule(canonical)
    finally:
        provider._cleanup_temp_files(scope)


@pytest.mark.parametrize("overrides", [
    {"safe_mode": False},
    {"permission_mode": "default"},
    {"web_search": True},
    {"allowed_tools": ["Read"]},
    {"add_dirs": ["/tmp"]},
    {"tools": ["Bash"]},
])
def test_restricted_claude_profile_rejects_capability_expansion(overrides):
    from unified_cli.providers.claude import ClaudeProvider

    options = {
        "bin_path": "claude",
        "web_search": False,
        "safe_mode": True,
        "permission_mode": "dontAsk",
        "tools": [],
        "restrict_image_reads": True,
    }
    options.update(overrides)
    with pytest.raises(ValueError):
        ClaudeProvider(**options)


def test_agy_permission_bypass_is_explicit_opt_in(monkeypatch, tmp_path):
    from unified_cli.providers.gemini import GeminiProvider

    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    options = {"bin_path": "agy", "web_search": False,
               "conversations_dir": str(tmp_path / "missing")}

    safe = GeminiProvider(**options)
    safe._validate_model = lambda model: None
    safe_argv, _ = safe._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    assert "--dangerously-skip-permissions" not in safe_argv

    unattended = GeminiProvider(skip_permissions=True, **options)
    unattended._validate_model = lambda model: None
    unattended_argv, _ = unattended._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    assert "--dangerously-skip-permissions" in unattended_argv


def test_agy_never_reinjects_inherited_api_key(monkeypatch, tmp_path):
    from unified_cli.providers.gemini import GeminiProvider

    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "inherited-key")
    provider = GeminiProvider(
        bin_path="agy", conversations_dir=str(tmp_path / "missing")
    )

    assert "GEMINI_API_KEY" not in provider._env(fallback_api_key=False)
    assert "GEMINI_API_KEY" not in provider._env(fallback_api_key=True)

    deliberate = GeminiProvider(
        bin_path="agy",
        conversations_dir=str(tmp_path / "missing"),
        extra_env={"GEMINI_API_KEY": "explicit-key"},
    )
    assert deliberate._env(fallback_api_key=True)["GEMINI_API_KEY"] == "explicit-key"


def test_codex_config_overrides_are_toml_safe_for_exec_and_resume():
    from unified_cli.providers.codex import CodexProvider

    provider = CodexProvider(
        bin_path="codex",
        config_overrides={
            "model_reasoning_effort": "high",
            "limits.max": 3,
            "limits.ratio": 1.25,
            "features.allowed": ["Read", True, 2],
        },
    )
    normal, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    resumed, _ = provider._build_args(
        "hello", session_id="thread-1", resume_last=False, model=None, streaming=False,
    )

    expected = [
        'model_reasoning_effort="high"',
        "limits.max=3",
        "limits.ratio=1.25",
        'features.allowed=["Read", true, 2]',
        "tools.web_search=true",
    ]
    assert _config_values(normal) == expected
    assert _config_values(resumed) == expected


def test_codex_config_string_is_quoted_and_invalid_values_fail_fast():
    from unified_cli.providers.codex import CodexProvider

    provider = CodexProvider(
        bin_path="codex", web_search=False,
        config_overrides={"label": 'line one\nquote " here'},
    )
    config = _config_values(provider._common_flags(None, streaming=False))[0]
    assert config.startswith('label="')
    assert "\n" not in config
    assert "\\n" in config

    for overrides in (
        {"bad key": True},
        {"nested": {"not": "a literal"}},
        {"ratio": float("nan")},
    ):
        with pytest.raises(ValueError):
            CodexProvider(bin_path="codex", web_search=False, config_overrides=overrides)


def test_codex_config_isolation_flags_apply_to_exec_and_resume():
    from unified_cli.providers.codex import CodexProvider

    provider = CodexProvider(
        bin_path="codex",
        web_search=False,
        ignore_user_config=True,
        ignore_rules=True,
    )
    normal, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=False,
    )
    resumed, _ = provider._build_args(
        "hello", session_id="thread-1", resume_last=False, model=None, streaming=False,
    )
    for argv in (normal, resumed):
        assert "--ignore-user-config" in argv
        assert "--ignore-rules" in argv


def test_server_conversation_applies_only_provider_specific_restrictions(monkeypatch):
    from unified_cli import server
    import unified_cli.conversation as conversation

    calls: dict[str, dict] = {}

    def fake_create(provider, *, model=None, **opts):
        calls[provider] = {"model": model, **opts}
        return object()

    monkeypatch.setattr(conversation, "create", fake_create)
    conv = server._new_server_conversation()
    conv._get_client("claude", None)
    conv._get_client("codex", None)
    conv._get_client("gemini", None)

    assert calls["claude"] == {
        "model": None,
        "web_search": False,
        "safe_mode": True,
        "permission_mode": "dontAsk",
        "tools": [],
        "restrict_image_reads": True,
    }
    assert calls["codex"] == {
        "model": None,
        "web_search": False,
        "ignore_user_config": True,
        "ignore_rules": True,
    }
    assert calls["gemini"] == {
        "model": None,
        "web_search": False,
        "sandbox": True,
        "skip_permissions": False,
    }


def test_claude_streaming_requests_partials_and_deduplicates_final_text():
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(bin_path="claude", web_search=False)
    argv, _ = provider._build_args(
        "hello", session_id=None, resume_last=False, model=None, streaming=True,
    )
    assert "--include-partial-messages" in argv

    events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hel"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "lo"},
        }},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Read", "input": {}, "id": "tool-1"},
        ]}},
    ]
    state = provider._new_stream_state()
    messages = [
        message
        for event in events
        for message in provider._stream_normalize(event, state)
    ]
    assert [message.text for message in messages if message.kind == "text"] == ["hel", "lo"]
    assert [message.tool["name"] for message in messages if message.kind == "tool_use"] == ["Read"]


def test_claude_streaming_dedups_reindexed_per_block_envelopes():
    # Real CLI shape (claude -p --include-partial-messages): one assistant
    # envelope per content block, each envelope's content re-indexed from 0.
    # Text streamed as block 1 (after a thinking block at 0) then arrives in
    # its own envelope at index 0 — it must still be recognized as already
    # streamed. Previously the index-only lookup missed and the complete text
    # was yielded a second time (visible duplication in consumers).
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(bin_path="claude", web_search=False)
    state = provider._new_stream_state()
    events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "plan"},
        }},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "plan"},
        ]}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "PONG"},
        }},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PONG"},
        ]}},
    ]
    messages = [
        message
        for event in events
        for message in provider._stream_normalize(event, state)
    ]
    assert [m.text for m in messages if m.kind == "text"] == ["PONG"]
    assert [m.text for m in messages if m.kind == "reasoning"] == ["plan"]


def test_claude_streaming_reconciles_reasoning_and_legacy_final_messages():
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(bin_path="claude", web_search=False)
    state = provider._new_stream_state()
    events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "hel"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "plan"},
        }},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "thinking", "thinking": "plan more"},
        ]}},
        # Older or partial-free CLI outputs can still send a final assistant
        # message without preceding deltas; it must not be dropped.
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello again"},
        ]}},
    ]
    messages = [
        message
        for event in events
        for message in provider._stream_normalize(event, state)
    ]
    assert [message.text for message in messages if message.kind == "text"] == [
        "hel", "lo", "hello again",
    ]
    assert [message.text for message in messages if message.kind == "reasoning"] == [
        "plan", " more",
    ]


def test_claude_interleaved_streams_do_not_share_partial_state():
    from unified_cli.providers.claude import ClaudeProvider

    provider = ClaudeProvider(bin_path="claude", web_search=False)
    first = provider._new_stream_state()
    second = provider._new_stream_state()
    first_delta = {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "one"},
    }}
    second_delta = {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "two"},
    }}
    first_final = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "one"},
    ]}}
    second_final = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "two"},
    ]}}

    assert [m.text for m in provider._stream_normalize(first_delta, first)] == ["one"]
    assert [m.text for m in provider._stream_normalize(second_delta, second)] == ["two"]
    assert list(provider._stream_normalize(first_final, first)) == []
    assert list(provider._stream_normalize(second_final, second)) == []
