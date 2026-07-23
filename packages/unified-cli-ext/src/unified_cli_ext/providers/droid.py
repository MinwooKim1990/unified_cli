"""Opt-in Preview bridge for Factory Droid's documented stream JSON-RPC mode."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Iterator, Tuple

from ..errors import ProtocolError
from ..transports import CancellationToken, JsonlProcess
from .bridge import AdapterProviderBridge
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .path_resolver import path_launch_resolver
from .pi import _PROMPT_LIMITS, _command, _protocol_plugin


DROID_OFFICIAL_PACKAGE = "droid"
DROID_DEFAULT_MODEL = "provider-default"
DROID_INITIALIZE_ID = "unified-cli-ext-init"
DROID_TURN_ID = "unified-cli-ext-turn"
DROID_RPC_FIXED_ARGV = (
    "exec",
    "--input-format",
    "stream-jsonrpc",
    "--output-format",
    "stream-jsonrpc",
    "--auto",
    "low",
)
DROID_FIXED_ENVIRONMENT = MappingProxyType(
    {"FACTORY_DROID_AUTO_UPDATE_ENABLED": "false"}
)


def _assistant_message_text(message: object) -> str:
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if type(content) is str:
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts = []
    for block in content:
        if (
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and type(block.get("text")) is str
        ):
            parts.append(block["text"])
    return "".join(parts)


def _rpc_failure(frame: Mapping, operation: str) -> None:
    error = frame.get("error")
    if not isinstance(error, Mapping):
        return
    code = error.get("code")
    suffix = " ({})".format(code) if type(code) is int else ""
    raise ProtocolError(
        "Droid {} failed with a JSON-RPC error{}".format(operation, suffix)
    )


def _server_response(frame: Mapping) -> Mapping:
    request_id = frame.get("id")
    method = frame.get("method")
    if type(request_id) not in (str, int):
        raise ProtocolError("Droid returned a malformed server request")
    if method == "droid.request_permission":
        result = {"selectedOption": "cancel"}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "droid.ask_user":
        result = {"cancelled": True, "answers": []}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32601,
            "message": "Unsupported server request",
        },
    }


class _DroidRpcBridge(AdapterProviderBridge):
    @staticmethod
    def _state() -> dict:
        return {
            "ack": False,
            "terminal": False,
            "error": False,
            "text": [],
            "fallback": "",
        }

    @staticmethod
    def _accept(turn: Any, record: Mapping) -> Tuple[Any, ...]:
        return tuple(turn.accept_record(record))

    @staticmethod
    def _notification(
        frame: Mapping,
        state: dict,
    ) -> Tuple[Mapping, ...]:
        params = frame.get("params")
        if not isinstance(params, Mapping):
            raise ProtocolError("Droid returned malformed notification parameters")
        notification = params.get("notification")
        if not isinstance(notification, Mapping):
            raise ProtocolError("Droid returned a malformed session notification")
        kind = notification.get("type")
        if type(kind) is not str:
            raise ProtocolError("Droid returned a malformed session notification")

        if kind == "assistant_text_delta":
            delta = notification.get(
                "textDelta",
                notification.get("delta", notification.get("text")),
            )
            if type(delta) is not str:
                raise ProtocolError("Droid returned a malformed assistant text delta")
            state["text"].append(delta)
            return ({"type": "text_delta", "text": delta},)

        if kind == "create_message":
            text = _assistant_message_text(notification.get("message"))
            if text:
                state["fallback"] = text
            return ()

        if kind == "droid_working_state_changed":
            new_state = notification.get(
                "newState", notification.get("state")
            )
            if new_state == "idle":
                state["terminal"] = True
            return ()

        if kind == "error":
            state["error"] = True
            state["terminal"] = True
            return (
                {
                    "type": "error",
                    "code": "provider_error",
                    "message": "Droid reported a session error.",
                    "retryable": False,
                },
            )

        return ()

    def _process(
        self,
        prompt: str,
        values: Mapping[str, str],
        token: CancellationToken,
    ) -> JsonlProcess:
        invocation = self._adapter.build_prompt(self._binary, prompt, values)
        return JsonlProcess(
            invocation.argv,
            timeout=self._limits.timeout_seconds,
            cwd=self.cwd,
            provider_env=self._provider_env,
            allowed_provider_env=tuple(
                self._adapter.spec.environment.allowed_keys
            ),
            persistent_home=self._provider_home,
            limits=self._limits.transport_limits(),
            cancellation=token,
            executable_identity=self._binary.executable_identity(),
            launch_identities=self._binary.spawn_identities(),
        )

    @staticmethod
    def _receive_initialize(process: JsonlProcess) -> None:
        while True:
            frame = process.receive()
            if frame is None:
                raise ProtocolError(
                    "Droid JSON-RPC ended before session initialization completed"
                )
            if frame.get("jsonrpc") != "2.0":
                raise ProtocolError("Droid returned a malformed JSON-RPC envelope")
            if frame.get("id") == DROID_INITIALIZE_ID:
                _rpc_failure(frame, "session initialization")
                if "result" not in frame:
                    raise ProtocolError(
                        "Droid returned a malformed initialization response"
                    )
                return
            if type(frame.get("method")) is str and "id" in frame:
                process.send(_server_response(frame))
                continue
            if type(frame.get("method")) is str and "id" not in frame:
                continue
            raise ProtocolError("Droid returned an unmatched JSON-RPC response")

    def _iter_rpc_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: Any,
        token: CancellationToken,
    ) -> Iterator[Any]:
        self._run_turn_preflight()
        process = self._process(prompt, values, token)
        state = self._state()
        stdin_closed = False
        with process:
            process.send(
                {
                    "jsonrpc": "2.0",
                    "id": DROID_INITIALIZE_ID,
                    "method": "droid.initialize_session",
                    "params": {
                        "machineId": "unified-cli-ext",
                        "cwd": self.cwd,
                        "disableBuiltinSkills": True,
                    },
                }
            )
            self._receive_initialize(process)
            process.send(
                {
                    "jsonrpc": "2.0",
                    "id": DROID_TURN_ID,
                    "method": "droid.add_user_message",
                    "params": {"text": prompt},
                }
            )

            while True:
                frame = process.receive()
                if frame is None:
                    break
                if frame.get("jsonrpc") != "2.0":
                    raise ProtocolError(
                        "Droid returned a malformed JSON-RPC envelope"
                    )
                method = frame.get("method")
                if type(method) is str and "id" in frame:
                    process.send(_server_response(frame))
                elif method == "droid.session_notification":
                    for record in self._notification(frame, state):
                        for message in self._accept(turn, record):
                            yield message
                elif frame.get("id") == DROID_TURN_ID:
                    if isinstance(frame.get("error"), Mapping):
                        state["error"] = True
                        state["terminal"] = True
                        code = frame["error"].get("code")
                        label = (
                            "droid_rpc_{}".format(code)
                            if type(code) is int
                            else "droid_rpc_error"
                        )
                        for message in self._accept(
                            turn,
                            {
                                "type": "error",
                                "code": label,
                                "message": "Droid rejected the user message.",
                                "retryable": False,
                            },
                        ):
                            yield message
                    elif "result" not in frame:
                        raise ProtocolError(
                            "Droid returned a malformed user-message response"
                        )
                    state["ack"] = True
                elif type(method) is str:
                    continue
                else:
                    raise ProtocolError(
                        "Droid returned an unmatched JSON-RPC response"
                    )

                if (
                    (state["ack"] and state["terminal"]) or state["error"]
                ) and not stdin_closed:
                    process.close_stdin()
                    stdin_closed = True

            if not (
                (state["ack"] and state["terminal"]) or state["error"]
            ):
                raise ProtocolError(
                    "Droid JSON-RPC ended before the turn completed"
                )

        text = "".join(state["text"])
        if not text:
            text = state["fallback"]
        if text:
            for message in self._accept(
                turn, {"type": "text_final", "text": text}
            ):
                yield message
        for message in self._accept(
            turn,
            {
                "type": "done",
                "reason": "error" if state["error"] else "complete",
            },
        ):
            yield message
        turn.finish()

    def _one_shot_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: Any,
        token: CancellationToken,
    ) -> Tuple[Any, ...]:
        return tuple(
            self._iter_rpc_messages(prompt, values, turn, token)
        )

    def _sync_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: Any,
        token: CancellationToken,
    ) -> Iterator[Any]:
        yield from self._iter_rpc_messages(prompt, values, turn, token)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="droid",
    display_name="Factory Droid",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="droid",
        expected_identity="droid",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="droid ",
            identity_marker="droid ",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("exec", "--help"),
            required_features=frozenset(("chat", "stream")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "chat": "Usage: droid exec",
                "stream": "--input-format",
            },
            identity_marker="Usage: droid exec",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=DROID_RPC_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.JSON_RPC,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("FACTORY_API_KEY",)),
        fixed_values=DROID_FIXED_ENVIRONMENT,
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset(
        (ProviderCapability.CHAT.value, ProviderCapability.STREAM.value)
    ),
    server_policy=AdapterServerPolicy(enabled=False),
)


PLUGIN = _protocol_plugin(
    ADAPTER_SPEC,
    default_model=DROID_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="droid",
        executable="droid",
        package_names=(DROID_OFFICIAL_PACKAGE,),
    ),
    bridge_type=_DroidRpcBridge,
)


__all__ = [
    "ADAPTER_SPEC",
    "DROID_DEFAULT_MODEL",
    "DROID_FIXED_ENVIRONMENT",
    "DROID_INITIALIZE_ID",
    "DROID_OFFICIAL_PACKAGE",
    "DROID_RPC_FIXED_ARGV",
    "DROID_TURN_ID",
    "PLUGIN",
]
