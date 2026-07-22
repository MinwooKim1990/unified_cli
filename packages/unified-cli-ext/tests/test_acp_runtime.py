import asyncio
import importlib
import importlib.metadata
import os
import shutil
import sys
import types

import pytest

import unified_cli_ext
from unified_cli_ext import (
    AcpProcessTransportV1,
    LimitExceeded,
    ProtocolError,
    SessionEvent,
    TextDeltaEvent,
    TransportCancelled,
    TransportError,
    TransportLimits,
    TransportTimeout,
    UsageEvent,
)
from unified_cli_ext.transports import ExecutableIdentity


class FakeRequestError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    @classmethod
    def method_not_found(cls, method):
        return cls(-32601, "Method not found")


class Record:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ClientCapabilities(Record):
    pass


class TextContentBlock(Record):
    pass


class AgentMessageChunk(Record):
    pass


class AgentThoughtChunk(Record):
    pass


class UsageUpdate(Record):
    pass


class RequestPermissionResponse(Record):
    pass


class DeniedOutcome(Record):
    pass


class CancelElicitationResponse(Record):
    pass


class FakeConnection:
    def __init__(self, state, client):
        self.state = state
        self.client = client
        self.close_calls = 0

    async def initialize(self, **kwargs):
        self.state.initialize = kwargs
        return Record(protocol_version=self.state.negotiated_version)

    async def new_session(self, **kwargs):
        self.state.new_session = kwargs
        return Record(session_id="session-1")

    async def prompt(self, **kwargs):
        self.state.prompt = kwargs
        if self.state.prompt_started is not None:
            self.state.prompt_started.set()
        if self.state.update is not None:
            session_id, update = self.state.update
            await self.client.session_update(session_id=session_id, update=update)
        if self.state.permission:
            self.state.permission_result = await self.client.request_permission(
                session_id="session-1", tool_call=Record(), options=[]
            )
        if self.state.inbound_method is not None:
            observer = self.state.connect[3]["observers"][0]
            observer(
                Record(
                    direction=Record(value="incoming"),
                    message={"method": self.state.inbound_method},
                )
            )
        if self.state.delay is not None:
            await asyncio.sleep(self.state.delay)
        return Record(stop_reason="end_turn", usage=self.state.final_usage)

    async def close(self):
        self.close_calls += 1


class FakeSdkState:
    def __init__(self):
        self.negotiated_version = 1
        self.update = None
        self.permission = False
        self.permission_result = None
        self.final_usage = None
        self.delay = None
        self.prompt_started = None
        self.initialize = None
        self.new_session = None
        self.prompt = None
        self.connect = None
        self.connection = None
        self.inbound_method = None


def fake_sdk(state):
    schema = types.SimpleNamespace(
        AgentMessageChunk=AgentMessageChunk,
        AgentThoughtChunk=AgentThoughtChunk,
        CancelElicitationResponse=CancelElicitationResponse,
        ClientCapabilities=ClientCapabilities,
        DeniedOutcome=DeniedOutcome,
        RequestPermissionResponse=RequestPermissionResponse,
        TextContentBlock=TextContentBlock,
        UsageUpdate=UsageUpdate,
    )

    def connect_to_agent(client, input_stream, output_stream, **kwargs):
        state.connect = (client, input_stream, output_stream, kwargs)
        state.connection = FakeConnection(state, client)
        return state.connection

    return types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        RequestError=FakeRequestError,
        connect_to_agent=connect_to_agent,
        schema=schema,
    )


@pytest.fixture
def process_identity():
    executable = os.path.realpath(shutil.which("sleep") or "/usr/bin/sleep")
    return executable, ExecutableIdentity.capture(executable)


@pytest.fixture(scope="module")
def python_process_identity(tmp_path_factory):
    directory = tmp_path_factory.mktemp("acp-python")
    executable = directory / "python"
    shutil.copyfile(os.path.realpath(sys.executable), executable)
    executable.chmod(0o700)
    path = str(executable)
    return path, ExecutableIdentity.capture(path)


def make_transport(tmp_path, process_identity, **kwargs):
    executable, identity = kwargs.pop("identity_pair", process_identity)
    argv = kwargs.pop("argv", [executable, "60"])
    return AcpProcessTransportV1(
        argv,
        executable_identity=identity,
        cwd=str(tmp_path),
        provider_namespace="fake",
        timeout=kwargs.pop("timeout", 2),
        **kwargs,
    )


def install_fake(monkeypatch, state):
    module = importlib.import_module("unified_cli_ext.transports.acp")
    sdk = fake_sdk(state)
    monkeypatch.setattr(module, "require_acp_sdk", lambda: sdk)
    return module, sdk


def require_official_acp_011():
    acp = pytest.importorskip("acp")
    version = importlib.metadata.version("agent-client-protocol")
    if not version.startswith("0.11."):
        pytest.skip("official ACP SDK 0.11 is not installed")
    return acp


def raw_agent_program(prompt_frames, *, batch=True):
    return """
import json
import os
import sys

PROMPT_FRAMES = {prompt_frames!r}
BATCH = {batch!r}

def write_frames(frames):
    encoded = [
        json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\\n"
        for frame in frames
    ]
    if BATCH:
        os.write(1, b"".join(encoded))
    else:
        for frame in encoded:
            os.write(1, frame)

for line in sys.stdin.buffer:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        write_frames([{{
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {{"protocolVersion": message["params"]["protocolVersion"]}},
        }}])
    elif method == "session/new":
        write_frames([{{
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {{"sessionId": "raw-session"}},
        }}])
    elif method == "session/prompt":
        frames = list(PROMPT_FRAMES)
        frames.append({{
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {{"stopReason": "end_turn"}},
        }})
        write_frames(frames)
        break
""".format(prompt_frames=prompt_frames, batch=batch)


def make_raw_transport(
    tmp_path,
    process_identity,
    python_process_identity,
    prompt_frames,
    *,
    batch=True,
    **kwargs,
):
    require_official_acp_011()
    executable = python_process_identity[0]
    return make_transport(
        tmp_path,
        process_identity,
        identity_pair=python_process_identity,
        argv=[executable, "-c", raw_agent_program(prompt_frames, batch=batch)],
        timeout=kwargs.pop("timeout", 5),
        **kwargs,
    )


def raw_session_update(update, session_id="raw-session"):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def test_public_runtime_is_lazy_and_does_not_accept_factory(tmp_path, process_identity):
    sys.modules.pop("acp", None)
    transport = make_transport(tmp_path, process_identity)
    assert "acp" not in sys.modules
    assert "AcpProcessTransportV1" in unified_cli_ext.__all__
    with pytest.raises(TypeError):
        AcpProcessTransportV1(
            [process_identity[0], "60"],
            executable_identity=process_identity[1],
            cwd=str(tmp_path),
            provider_namespace="fake",
            factory=lambda sdk: sdk,
        )
    with pytest.raises(ProtocolError, match="provider namespace"):
        AcpProcessTransportV1(
            [process_identity[0], "60"],
            executable_identity=process_identity[1],
            cwd=str(tmp_path),
            provider_namespace="Not Valid",
        )
    asyncio.run(transport.close_async())


def test_text_turn_uses_typed_sdk_and_isolated_owned_process(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    module, sdk = install_fake(monkeypatch, state)
    original_popen = module.subprocess.Popen
    spawned = {}

    def capture_popen(*args, **kwargs):
        spawned["argv"] = tuple(args[0])
        spawned["cwd"] = os.fspath(kwargs["cwd"])
        spawned["env"] = dict(kwargs["env"])
        spawned["flags"] = {
            key: kwargs[key]
            for key in ("shell", "start_new_session", "close_fds")
        }
        spawned["process"] = original_popen(*args, **kwargs)
        return spawned["process"]

    monkeypatch.setattr(module.subprocess, "Popen", capture_popen)
    transport = make_transport(
        tmp_path,
        process_identity,
        provider_env={"ACP_TEST_TOKEN": "private-value"},
        allowed_provider_env=("ACP_TEST_TOKEN",),
    )
    events = asyncio.run(transport.text_turn("hello ACP"))

    assert isinstance(events, tuple)
    assert isinstance(events[0], SessionEvent)
    assert events[0].session.namespaced == "fake:session-1"
    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert events[-1].reason == "end_turn"
    assert state.initialize["protocol_version"] == sdk.PROTOCOL_VERSION
    capabilities = state.initialize["client_capabilities"]
    assert capabilities.fs is None
    assert capabilities.terminal is False
    assert capabilities.session is None
    assert capabilities.plan is None
    assert capabilities.auth is None
    assert capabilities.elicitation is None
    assert state.new_session == {
        "cwd": str(tmp_path),
        "additional_directories": [],
        "mcp_servers": [],
    }
    assert len(state.prompt["prompt"]) == 1
    assert state.prompt["prompt"][0].type == "text"
    assert state.prompt["prompt"][0].text == "hello ACP"
    assert state.connect[3]["use_unstable_protocol"] is False
    assert len(state.connect[3]["observers"]) == 1
    assert spawned["argv"] == (process_identity[0], "60")
    assert spawned["cwd"] == str(tmp_path)
    assert spawned["flags"] == {
        "shell": False,
        "start_new_session": True,
        "close_fds": True,
    }
    assert spawned["env"]["ACP_TEST_TOKEN"] == "private-value"
    assert "USER" not in spawned["env"]
    assert "LOGNAME" not in spawned["env"]
    assert "SHELL" not in spawned["env"]
    assert spawned["env"]["HOME"] != os.environ.get("HOME")
    assert spawned["process"].returncode is not None
    assert state.connection.close_calls == 1


def test_usage_normalization_and_closed_reverse_callbacks(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    state.final_usage = Record(
        input_tokens=7,
        output_tokens=3,
        cached_read_tokens=2,
    )
    install_fake(monkeypatch, state)
    events = asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))
    assert UsageEvent(input_tokens=7, output_tokens=3, cached_input_tokens=2) in events
    assert UsageEvent(input_tokens=17) not in events

    client = state.connect[0]

    async def exercise_closed_client():
        with pytest.raises(ProtocolError, match="closed text-turn protocol"):
            await client.session_update(
                session_id="session-1",
                update=AgentMessageChunk(
                    content=TextContentBlock(type="text", text="direct"),
                    message_id="direct-message",
                ),
            )
        permission = await client.request_permission()
        assert permission.outcome.outcome == "cancelled"
        elicitation = await client.create_elicitation()
        assert elicitation.action == "cancel"
        callbacks = (
            client.write_text_file,
            client.read_text_file,
            client.create_terminal,
            client.terminal_output,
            client.release_terminal,
            client.wait_for_terminal_exit,
            client.kill_terminal,
        )
        for callback in callbacks:
            with pytest.raises(FakeRequestError) as caught:
                await callback()
            assert caught.value.code == -32601
        with pytest.raises(FakeRequestError):
            await client.ext_method("private", {})
        with pytest.raises(FakeRequestError):
            await client.ext_notification("private", {})

    asyncio.run(exercise_closed_client())

    state = FakeSdkState()
    state.update = None
    state.permission = True
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError, match="closed text-turn protocol"):
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))
    assert state.permission_result.outcome.outcome == "cancelled"


def test_negotiation_wrong_session_and_non_text_updates_fail_closed(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    state.negotiated_version = 2
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError, match="closed text-turn protocol"):
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))

    state = FakeSdkState()
    state.update = (
        "wrong-session",
        AgentMessageChunk(content=TextContentBlock(type="text", text="x"), message_id=None),
    )
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError, match="closed text-turn protocol"):
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))

    state = FakeSdkState()
    state.update = (
        "session-1",
        AgentThoughtChunk(content=TextContentBlock(type="text", text="private")),
    )
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError, match="closed text-turn protocol") as caught:
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))
    assert "private" not in str(caught.value)

    state = FakeSdkState()
    state.update = None
    state.inbound_method = "mcp/connect"
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError, match="closed text-turn protocol"):
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))


def test_prompt_output_event_and_frame_limits(tmp_path, process_identity, monkeypatch):
    state = FakeSdkState()
    install_fake(monkeypatch, state)
    with pytest.raises(ProtocolError):
        asyncio.run(
            make_transport(
                tmp_path,
                process_identity,
                limits=TransportLimits(max_output_bytes=3),
            ).text_turn("four")
        )

    transport = make_transport(
        tmp_path,
        process_identity,
        limits=TransportLimits(max_output_bytes=3),
    )
    transport._session_id = "session-1"
    with pytest.raises(LimitExceeded, match="output text"):
        transport._accept_prevalidated_session_update(
            "session-1",
            AgentMessageChunk(
                content=TextContentBlock(type="text", text="four"),
                message_id=None,
            ),
            fake_sdk(FakeSdkState()).schema,
        )
    asyncio.run(transport.close_async())

    state = FakeSdkState()
    install_fake(monkeypatch, state)
    with pytest.raises(LimitExceeded, match="event count"):
        asyncio.run(
            make_transport(
                tmp_path,
                process_identity,
                limits=TransportLimits(max_events=1),
            ).text_turn("x")
        )


@pytest.mark.parametrize("batch", (True, False), ids=("same-write", "one-per-frame"))
def test_raw_updates_are_normalized_in_frame_order(
    tmp_path,
    process_identity,
    python_process_identity,
    batch,
):
    frames = [
        raw_session_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "first"},
                "messageId": "message-1",
            }
        ),
        raw_session_update(
            {
                "sessionUpdate": "usage_update",
                "used": 7,
                "size": 100,
            }
        ),
        raw_session_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "second"},
                "messageId": "message-2",
            }
        ),
    ]
    transport = make_raw_transport(
        tmp_path,
        process_identity,
        python_process_identity,
        frames,
        batch=batch,
    )

    events = asyncio.run(transport.text_turn("x"))

    assert events == (
        SessionEvent(events[0].session),
        TextDeltaEvent(text="first", block_id="message-1"),
        TextDeltaEvent(text="second", block_id="message-2"),
        events[-1],
    )
    assert events[0].session.namespaced == "fake:raw-session"
    assert events[-1].reason == "end_turn"


@pytest.mark.parametrize(
    "frame",
    (
        raw_session_update(
            {
                "sessionUpdate": "agent_message_chunks",
                "content": {"type": "text", "text": "ACP_RAW_FIXTURE_MARKER"},
            }
        ),
        raw_session_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "text",
                    "text": {"value": "ACP_RAW_FIXTURE_MARKER"},
                },
            }
        ),
        raw_session_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "ACP_RAW_FIXTURE_MARKER"},
            },
            session_id="wrong-session",
        ),
        raw_session_update(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "ACP_RAW_FIXTURE_MARKER"},
            }
        ),
        raw_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "ACP_RAW_FIXTURE_MARKER",
            }
        ),
        {
            "jsonrpc": "2.0",
            "id": 90,
            "method": "mcp/connect",
            "params": {"value": "ACP_RAW_FIXTURE_MARKER"},
        },
        {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "fs/read_text_file",
            "params": {
                "sessionId": "raw-session",
                "path": {"value": "ACP_RAW_FIXTURE_MARKER"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 92,
            "method": "fs/read_text_file",
            "params": {
                "sessionId": "raw-session",
                "path": "ACP_RAW_FIXTURE_MARKER",
            },
        },
    ),
    ids=(
        "bad-discriminator",
        "bad-text-object",
        "wrong-session",
        "thought",
        "tool",
        "unknown-method",
        "malformed-reverse-request",
        "closed-reverse-request",
    ),
)
def test_raw_invalid_or_closed_messages_fail_before_sensitive_sdk_logging(
    tmp_path,
    process_identity,
    python_process_identity,
    caplog,
    capsys,
    frame,
):
    transport = make_raw_transport(
        tmp_path,
        process_identity,
        python_process_identity,
        [frame],
    )

    with pytest.raises(ProtocolError, match="closed text-turn protocol") as caught:
        asyncio.run(transport.text_turn("x"))

    assert "ACP_RAW_FIXTURE_MARKER" not in str(caught.value)
    assert "ACP_RAW_FIXTURE_MARKER" not in caplog.text
    assert "ACP_RAW_FIXTURE_MARKER" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("limits", "message"),
    (
        (TransportLimits(max_output_bytes=150), "stdout"),
        (TransportLimits(max_events=4), "frame count"),
    ),
)
def test_raw_output_and_frame_limits(
    tmp_path,
    process_identity,
    python_process_identity,
    limits,
    message,
):
    frame = raw_session_update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "bounded"},
        }
    )
    transport = make_raw_transport(
        tmp_path,
        process_identity,
        python_process_identity,
        [frame],
        limits=limits,
    )

    with pytest.raises(LimitExceeded, match=message):
        asyncio.run(transport.text_turn("x"))


@pytest.mark.parametrize(
    ("program", "limits", "error_type"),
    (
        (
            "import sys; sys.stdout.write('{bad}\\n'); sys.stdout.flush()",
            TransportLimits(),
            ProtocolError,
        ),
        (
            "import sys; sys.stdout.write('x' * 100); sys.stdout.flush()",
            TransportLimits(max_line_bytes=16, max_output_bytes=1024),
            LimitExceeded,
        ),
        (
            "import sys; sys.stderr.write('x' * 100); sys.stderr.flush()",
            TransportLimits(max_stderr_bytes=16),
            LimitExceeded,
        ),
        (
            "import sys; sys.stdout.write('{\"id\":1}\\n' * 3); sys.stdout.flush()",
            TransportLimits(max_events=2),
            LimitExceeded,
        ),
    ),
)
def test_malformed_and_bounded_raw_streams(
    tmp_path,
    process_identity,
    python_process_identity,
    monkeypatch,
    program,
    limits,
    error_type,
):
    state = FakeSdkState()
    state.update = None
    state.delay = 60
    install_fake(monkeypatch, state)
    executable = python_process_identity[0]
    transport = make_transport(
        tmp_path,
        process_identity,
        identity_pair=python_process_identity,
        argv=[executable, "-c", program],
        limits=limits,
    )
    with pytest.raises(error_type):
        asyncio.run(transport.text_turn("x"))
    assert state.connection.close_calls == 1


def test_partial_pipe_setup_tracks_resources_and_reaps_process(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    module, _ = install_fake(monkeypatch, state)
    original_popen = module.subprocess.Popen
    spawned = []

    def capture_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", capture_popen)
    connected = []

    async def scenario():
        loop = asyncio.get_running_loop()
        loop_type = type(loop)
        original_connect = loop_type.connect_read_pipe
        calls = 0

        async def fail_second(loop_self, protocol_factory, pipe):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected pipe setup failure")
            result = await original_connect(loop_self, protocol_factory, pipe)
            connected.append(result[0])
            return result

        monkeypatch.setattr(loop_type, "connect_read_pipe", fail_second)
        with pytest.raises(TransportError, match="ACP transport failed"):
            await make_transport(tmp_path, process_identity).text_turn("x")

    asyncio.run(scenario())
    assert len(connected) == 1
    assert connected[0].is_closing()
    assert len(spawned) == 1
    assert spawned[0].returncode is not None


def test_official_sdk_011_minimal_stdio_round_trip(tmp_path):
    acp = pytest.importorskip("acp")
    version = importlib.metadata.version("agent-client-protocol")
    if not version.startswith("0.11."):
        pytest.skip("official ACP SDK 0.11 is not installed")

    executable_path = tmp_path / "official-python"
    shutil.copyfile(os.path.realpath(sys.executable), executable_path)
    executable_path.chmod(0o700)
    executable = str(executable_path)
    identity = ExecutableIdentity.capture(executable)
    package_root = os.path.dirname(os.path.dirname(acp.__file__))
    import_paths = [package_root]
    for item in sys.path:
        if (
            type(item) is str
            and os.path.isabs(item)
            and "site-packages" in item
            and item not in import_paths
        ):
            import_paths.append(item)
    program = """
import asyncio
import acp
from acp import schema

class Agent:
    def on_connect(self, connection):
        self.connection = connection

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        assert client_capabilities.fs.read_text_file is False
        assert client_capabilities.fs.write_text_file is False
        assert client_capabilities.terminal is False
        return schema.InitializeResponse(protocol_version=protocol_version)

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        assert additional_directories == []
        assert mcp_servers == []
        return schema.NewSessionResponse(session_id="official-session")

    async def prompt(self, session_id, prompt, **kwargs):
        assert len(prompt) == 1
        assert isinstance(prompt[0], schema.TextContentBlock)
        await self.connection.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=schema.TextContentBlock(type="text", text="official"),
                message_id="official-message",
            ),
        )
        await self.connection.session_update(
            session_id=session_id,
            update=schema.UsageUpdate(
                session_update="usage_update",
                used=9,
                size=100,
            ),
        )
        return schema.PromptResponse(
            stop_reason="end_turn",
            usage=schema.Usage(
                total_tokens=5,
                input_tokens=3,
                output_tokens=2,
                cached_read_tokens=1,
            ),
        )

asyncio.run(acp.run_agent(Agent(), use_unstable_protocol=False))
"""
    transport = AcpProcessTransportV1(
        [executable, "-c", program],
        executable_identity=identity,
        cwd=str(tmp_path),
        provider_namespace="official",
        provider_env={"PYTHONPATH": os.pathsep.join(import_paths)},
        allowed_provider_env=("PYTHONPATH",),
        timeout=5,
    )
    events = asyncio.run(transport.text_turn("hello"))
    assert events[0].session.namespaced == "official:official-session"
    assert TextDeltaEvent(text="official", block_id="official-message") in events
    assert UsageEvent(
        input_tokens=3,
        output_tokens=2,
        cached_input_tokens=1,
    ) in events
    assert UsageEvent(input_tokens=9) not in events
    assert events[-1].reason == "end_turn"


def test_cleanup_error_precedence_preserves_operation_except_reap_uncertainty(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    state.update = (
        "wrong-session",
        AgentMessageChunk(content=TextContentBlock(type="text", text="x")),
    )
    module, _ = install_fake(monkeypatch, state)
    original_cleanup = module._cleanup_spawned_process

    def reaped_then_close_failure(*args, **kwargs):
        original_cleanup(*args, **kwargs)
        raise RuntimeError("private cleanup detail")

    monkeypatch.setattr(module, "_cleanup_spawned_process", reaped_then_close_failure)
    with pytest.raises(ProtocolError, match="closed text-turn protocol") as caught:
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))
    assert "private" not in str(caught.value)

    state = FakeSdkState()
    state.update = (
        "wrong-session",
        AgentMessageChunk(content=TextContentBlock(type="text", text="x")),
    )
    install_fake(monkeypatch, state)

    def reaped_then_uncertain(*args, **kwargs):
        original_cleanup(*args, **kwargs)
        raise TransportError("private reap detail")

    monkeypatch.setattr(module, "_cleanup_spawned_process", reaped_then_uncertain)
    with pytest.raises(
        TransportError,
        match="subprocess termination could not be confirmed",
    ) as caught:
        asyncio.run(make_transport(tmp_path, process_identity).text_turn("x"))
    assert "private" not in str(caught.value)


def test_timeout_precancel_midcancel_and_double_close(
    tmp_path, process_identity, monkeypatch
):
    state = FakeSdkState()
    state.delay = 60
    install_fake(monkeypatch, state)
    with pytest.raises(TransportTimeout):
        asyncio.run(
            make_transport(tmp_path, process_identity, timeout=0.05).text_turn("x")
        )
    assert state.connection.close_calls == 1

    token = unified_cli_ext.CancellationToken()
    token.cancel()
    transport = make_transport(tmp_path, process_identity, cancellation=token)
    with pytest.raises(TransportCancelled):
        asyncio.run(transport.text_turn("x"))

    state = FakeSdkState()
    state.delay = 60
    install_fake(monkeypatch, state)
    token = unified_cli_ext.CancellationToken()
    transport = make_transport(tmp_path, process_identity, cancellation=token)

    async def cancel_mid_turn():
        state.prompt_started = asyncio.Event()
        turn = asyncio.create_task(transport.text_turn("x"))
        await state.prompt_started.wait()
        token.cancel()
        await turn

    with pytest.raises(TransportCancelled):
        asyncio.run(cancel_mid_turn())
    assert state.connection.close_calls == 1

    transport = make_transport(tmp_path, process_identity)

    async def close_twice():
        await asyncio.gather(transport.close_async(), transport.close_async())
        await transport.close_async()

    asyncio.run(close_twice())
    with pytest.raises(TransportError, match="closed"):
        asyncio.run(transport.text_turn("x"))
