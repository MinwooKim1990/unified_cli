"""Browser chat sanitization, permission mapping, and cancellation tests."""

from __future__ import annotations

import json
import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import manage, session_manager, settings  # noqa: E402
from unified_cli.base import BaseProvider  # noqa: E402
from unified_cli.core import Attachment, Message, Response, Usage  # noqa: E402
from unified_cli.errors import UnifiedError  # noqa: E402
from unified_cli.providers.codex import CodexProvider  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(settings, "SETTINGS_DIR", state_dir)
    monkeypatch.setattr(settings, "SETTINGS_FILE", state_dir / "settings.json")
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", state_dir)
    monkeypatch.setattr(session_manager, "SESSIONS_FILE", state_dir / "sessions.json")


def _runtime(tmp_path):
    runtime = manage.ManageRuntime([str(tmp_path)])
    token = runtime.issue_bootstrap()
    _payload, cookie = runtime.bootstrap(
        supplied_token=token, supplied_csrf=None, cookie=None,
        peer_key="127.0.0.1")
    assert cookie
    owner = runtime.authenticate(cookie, rate=False)
    return runtime, owner


def _chat(runtime, owner, **overrides):
    payload = {
        "provider": "claude",
        "workspace_id": runtime.workspaces[0].id,
        "permission": "read_only",
        "prompt": "hello",
    }
    payload.update(overrides)
    return runtime.start_chat(payload, owner.key)


def _events(lines):
    return [json.loads(line) for line in lines]


def test_browser_chat_provider_workspace_permission_and_image_fail_closed(tmp_path):
    runtime, owner = _runtime(tmp_path)
    base = {
        "workspace_id": runtime.workspaces[0].id,
        "permission": "read_only", "prompt": "hello",
    }
    for provider in ("gemini", "extension", "agy"):
        with pytest.raises(manage.ManageError) as error:
            runtime.start_chat({"provider": provider, **base}, owner.key)
        assert error.value.code == "provider_forbidden"
    with pytest.raises(manage.ManageError) as error:
        runtime.start_chat({"provider": "claude", **base, "permission": "full"}, owner.key)
    assert error.value.code == "permission_forbidden"
    with pytest.raises(manage.ManageError):
        runtime.start_chat({
            "provider": "claude", **base, "workspace_id": "ws_unknown_123456789",
        }, owner.key)
    with pytest.raises(manage.ManageError):
        runtime.start_chat({
            "provider": "claude", **base,
            "images": [{"path": "/etc/passwd", "media_type": "image/png", "data": "x"}],
        }, owner.key)


def test_browser_provider_options_are_verified_read_only_mappings(tmp_path, monkeypatch):
    runtime, owner = _runtime(tmp_path)
    monkeypatch.setattr(manage, "load_settings", lambda: settings.Settings(web=False))

    claude = _chat(runtime, owner)
    conversation = runtime._conversation_for_chat(claude)
    options = conversation.provider_opts_by_provider["claude"]
    assert options["permission_mode"] == "plan"
    assert options["safe_mode"] is True
    assert set(options["tools"]) == {"Read", "Glob", "Grep"}
    assert options["cwd"] == str(tmp_path.resolve())
    runtime.finish_chat(claude.id)

    codex = _chat(runtime, owner, provider="codex")
    conversation = runtime._conversation_for_chat(codex)
    options = conversation.provider_opts_by_provider["codex"]
    assert options["sandbox"] == "read-only"
    assert options["ignore_user_config"] is True
    assert options["ignore_rules"] is True
    assert options["config_overrides"] == {"sandbox_mode": "read-only"}
    assert options["cwd"] == str(tmp_path.resolve())

    provider = CodexProvider(bin_path="codex", **options)
    fresh, _ = provider._build_args(
        "hello", session_id=None, resume_last=False,
        model=None, streaming=True,
    )
    resumed, _ = provider._build_args(
        "hello", session_id="existing-session", resume_last=False,
        model=None, streaming=True,
    )
    expected = 'sandbox_mode="read-only"'
    fresh_config = [fresh[index + 1] for index, value in enumerate(fresh) if value == "-c"]
    resumed_config = [resumed[index + 1] for index, value in enumerate(resumed) if value == "-c"]
    assert fresh[fresh.index("-s") + 1] == "read-only"
    assert expected in fresh_config
    assert expected in resumed_config
    runtime.finish_chat(codex.id)


def test_browser_images_match_ui_formats_and_total_limit(tmp_path, monkeypatch):
    runtime, owner = _runtime(tmp_path)
    base = {
        "provider": "claude", "workspace_id": runtime.workspaces[0].id,
        "permission": "read_only", "prompt": "hello",
    }
    with pytest.raises(manage.ManageError) as gif_error:
        runtime.start_chat({
            **base, "images": ["data:image/gif;base64,R0lGODlh"],
        }, owner.key)
    assert gif_error.value.code == "invalid_image"

    monkeypatch.setattr(
        manage, "_decode_data_image",
        lambda _value: Attachment(
            bytes_=b"x" * manage.MAX_IMAGE_BYTES, media_type="image/png"),
    )
    with pytest.raises(manage.ManageError) as total_error:
        runtime.start_chat({
            **base, "images": ["one", "two", "three", "four"],
        }, owner.key)
    assert total_error.value.code == "images_too_large"


def test_ndjson_sanitizes_sessions_reasoning_tools_outputs_and_errors(tmp_path, monkeypatch):
    runtime, owner = _runtime(tmp_path)
    monkeypatch.setattr(
        manage, "load_settings",
        lambda: settings.Settings(reasoning_display="compact", web=False),
    )

    class Conversation:
        def stream(self, *_args, **_kwargs):
            yield Message(
                kind="session", provider="claude", session_id="native-session-secret",
                raw={"credentials": "secret"},
            )
            yield Message(kind="text", provider="claude", text="hello\x1bworld", raw={"secret": "x"})
            yield Message(kind="reasoning", provider="claude", text="private chain", raw={})
            yield Message(
                kind="reasoning", provider="claude", text="public summary",
                raw={"public_summary": True, "private": "never"},
            )
            yield Message(
                kind="tool_use", provider="claude",
                tool={"id": "native-tool-id", "name": "Read", "input": {"path": "/secret"}},
            )
            yield Message(
                kind="tool_result", provider="claude",
                tool={"id": "native-tool-id", "output": "full secret output", "is_error": False},
            )
            yield Message(
                kind="usage", provider="claude",
                usage=Usage(input_tokens=2, output_tokens=3, cached_tokens=1),
            )
            yield Message(kind="error", provider="claude", error="credential=secret")
            yield Message(kind="done", provider="claude")

    monkeypatch.setattr(runtime, "_conversation_for_chat", lambda _chat: Conversation())
    chat = _chat(runtime, owner)
    events = _events(runtime.stream_chat(chat))
    encoded = json.dumps(events)
    assert "native-session-secret" not in encoded
    assert "native-tool-id" not in encoded
    assert "private chain" not in encoded
    assert "full secret output" not in encoded
    assert "/secret" not in encoded
    assert "credential=secret" not in encoded
    assert "public summary" in encoded
    assert [event["type"] for event in events].count("tool_started") == 1
    started = next(event for event in events if event["type"] == "tool_started")
    finished = next(event for event in events if event["type"] == "tool_finished")
    assert started["id"] == finished["id"]
    assert finished["status"] == "ok"
    assert "\x1b" not in encoded


def test_browser_session_resume_resolves_opaque_handle_only(tmp_path):
    runtime, owner = _runtime(tmp_path)
    runtime.session_manager.upsert(
        provider="claude", session_id="native-resume-id", model="haiku",
        cwd=str(tmp_path.resolve()),
    )
    handle = runtime.list_sessions()["sessions"][0]["id"]
    chat = _chat(runtime, owner, session_id=handle)
    assert chat.native_session_id == "native-resume-id"
    assert chat.browser_session_id == handle
    runtime.finish_chat(chat.id)
    with pytest.raises(manage.ManageError):
        _chat(runtime, owner, session_id="native-resume-id")


def test_cancel_chat_is_owner_scoped_and_sets_cooperative_event(tmp_path):
    runtime, owner = _runtime(tmp_path)
    chat = _chat(runtime, owner)
    with pytest.raises(manage.ManageError):
        runtime.cancel_chat(chat.id, "another-session")
    result = runtime.cancel_chat(chat.id, owner.key)
    assert result == {"id": chat.id, "cancelled": True}
    assert chat.cancel_event.is_set()
    runtime.finish_chat(chat.id)


class BlockingProvider(BaseProvider):
    name = "blocking"
    default_model = "test"
    api_key_env = "TEST_API_KEY"

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return None

    @classmethod
    def _install_hint(cls) -> str:
        return ""

    def _build_args(self, prompt, **_kwargs):
        return [self.bin_path, self.mode, self.pidfile], None

    def _normalize(self, obj) -> Iterator[Message]:
        yield Message(kind="text", provider=self.name, text=obj.get("text", ""), raw=obj)

    def _parse_json_response(self, text, model):
        return Response(text=text, session_id="", provider=self.name,
                        model=model, usage=Usage(), messages=[], raw=[])


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cancellation")
@pytest.mark.parametrize("mode", ["blocked", "continuous"])
def test_cancel_event_promptly_kills_process_tree_before_or_during_output(tmp_path, mode):
    script = tmp_path / "provider.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys, time\n"
        "mode, pidfile = sys.argv[1:3]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(os.getpid()) + ' ' + str(child.pid)); f.flush(); os.fsync(f.fileno())\n"
        "if mode == 'continuous':\n"
        "    while True:\n"
        "        print(json.dumps({'text':'x'}), flush=True); time.sleep(0.005)\n"
        "else:\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    pidfile = tmp_path / "pids"
    provider = BlockingProvider(bin_path=str(script), timeout=30, first_output_timeout=30)
    provider.mode = mode
    provider.pidfile = str(pidfile)
    cancel = threading.Event()
    outcome = []

    def consume():
        try:
            list(provider.stream("prompt", cancel_event=cancel))
        except UnifiedError as error:
            outcome.append(error)

    thread = threading.Thread(target=consume)
    started = time.monotonic()
    thread.start()
    deadline = time.monotonic() + 3
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists()
    parent_pid, child_pid = map(int, pidfile.read_text().split())
    cancel.set()
    thread.join(3)
    assert not thread.is_alive()
    assert time.monotonic() - started < 4
    assert outcome and getattr(outcome[0], "_cancelled", False)
    deadline = time.monotonic() + 2
    while (_pid_exists(parent_pid) or _pid_exists(child_pid)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(child_pid)


def test_conversation_does_not_forward_none_cancel_kwarg_to_legacy_client(monkeypatch):
    from unified_cli.conversation import UnifiedConversation

    class LegacyClient:
        def chat(self, prompt, *, session_id=None, images=None):
            return Response(
                text="ok", session_id="", provider="claude", model="m",
                usage=Usage(), messages=[], raw=[],
            )

        def stream(self, prompt, *, session_id=None, images=None):
            yield Message(kind="text", provider="claude", text="ok")

    conversation = UnifiedConversation()
    monkeypatch.setattr(conversation, "_get_client", lambda *_a: LegacyClient())
    assert conversation.send("hi").text == "ok"
    assert [message.text for message in conversation.stream("hi")] == ["ok"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cancellation")
def test_async_manage_relay_disconnect_cancels_blocked_provider_tree(tmp_path, monkeypatch):
    from unified_cli import server

    script = tmp_path / "provider.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "pidfile = sys.argv[2]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(os.getpid()) + ' ' + str(child.pid)); f.flush(); os.fsync(f.fileno())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    pidfile = tmp_path / "relay-pids"
    provider = BlockingProvider(bin_path=str(script), timeout=30, first_output_timeout=30)
    provider.mode = "blocked"
    provider.pidfile = str(pidfile)
    runtime, owner = _runtime(tmp_path)
    chat = _chat(runtime, owner)

    class Conversation:
        def stream(self, prompt, **kwargs):
            return provider.stream(prompt, cancel_event=kwargs["cancel_event"])

    monkeypatch.setattr(runtime, "_conversation_for_chat", lambda _chat: Conversation())

    async def scenario():
        relay = server._async_manage_chat_stream(runtime, chat)
        first = await relay.__anext__()
        assert json.loads(first)["type"] == "session"
        blocked = asyncio.create_task(relay.__anext__())
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        await relay.aclose()

    asyncio.run(scenario())
    assert chat.cancel_event.is_set()
    parent_pid, child_pid = map(int, pidfile.read_text().split())
    deadline = time.monotonic() + 2
    while (_pid_exists(parent_pid) or _pid_exists(child_pid)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(child_pid)
    assert chat.id not in runtime._active_chats


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cancellation")
def test_gemini_async_cancel_event_is_forwarded_without_typeerror(tmp_path, monkeypatch):
    from unified_cli.providers.gemini import GeminiProvider

    script = tmp_path / "agy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "pidfile = os.environ['PIDFILE']\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(os.getpid()) + ' ' + str(child.pid)); f.flush(); os.fsync(f.fileno())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    pidfile = tmp_path / "gemini-pids"
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    provider = GeminiProvider(
        bin_path=str(script), timeout=30, conversations_dir=str(conversations),
        extra_env={"PIDFILE": str(pidfile)},
    )
    cancel = threading.Event()

    async def consume():
        async for _message in provider.astream("prompt", cancel_event=cancel):
            pass

    async def scenario():
        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        cancel.set()
        with pytest.raises(UnifiedError) as error:
            await asyncio.wait_for(task, timeout=3)
        assert getattr(error.value, "_cancelled", False)

    asyncio.run(scenario())
    parent_pid, child_pid = map(int, pidfile.read_text().split())
    deadline = time.monotonic() + 2
    while (_pid_exists(parent_pid) or _pid_exists(child_pid)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(child_pid)
