"""Opt-in Preview bridge for Pi Coding Agent's documented RPC mode."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Iterator, Tuple, Type

from unified_cli.plugin import (
    PROVIDER_CONFIGURATION_ABI_V1,
    ProviderPluginV1,
    ProviderServerPolicyV1,
)

from ..errors import ProtocolError
from ..transports import CancellationToken
from .bridge import AdapterProviderBridge, _AdapterPluginRuntime
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    OperationLimits,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .path_resolver import path_launch_resolver
from .runtime import ProtocolLaunchBoundaryV1


PI_OFFICIAL_PACKAGE = "@earendil-works/pi-coding-agent"
PI_DEFAULT_MODEL = "provider-default"
PI_PROMPT_ID = "unified-cli-ext-turn"
PI_RPC_FIXED_ARGV = (
    "--mode",
    "rpc",
    "--no-session",
    "--offline",
    "--no-tools",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--no-approve",
)
PI_FIXED_ENVIRONMENT = MappingProxyType(
    {
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }
)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 16)
_PROMPT_LIMITS = OperationLimits(
    120.0,
    16 * 1024 * 1024,
    1024 * 1024,
    50_000,
)
_DIALOG_METHODS = frozenset(("select", "confirm", "input", "editor"))


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


def _assistant_text(message: object) -> str:
    if not isinstance(message, Mapping):
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


class _ProtocolPluginRuntime(_AdapterPluginRuntime):
    """Reuse Ext inspection/receipt gates but issue a provider-specific bridge."""

    def __init__(
        self,
        spec: ProviderAdapterSpecV1,
        default_model: str,
        launch_resolver: Any,
        bridge_type: Type[AdapterProviderBridge],
    ) -> None:
        super().__init__(
            spec,
            default_model,
            launch_resolver,
            None,
            None,
            None,
            None,
            None,
        )
        self.bridge_type = bridge_type

    def factory(self, **options: Any) -> AdapterProviderBridge:
        base = super().factory(**options)
        return self.bridge_type(
            adapter=base._adapter,
            inspection=base._inspection,
            binary=base._binary,
            default_model=self.default_model,
            model=base.model,
            cwd=base.cwd,
            provider_env=base._provider_env,
            provider_home=base._provider_home,
            limits=base._limits,
            state_factory=None,
            map_record=None,
            map_response=None,
            finalize=None,
            turn_preflight=None,
        )


def _protocol_plugin(
    spec: ProviderAdapterSpecV1,
    *,
    default_model: str,
    launch_resolver: Any,
    bridge_type: Type[AdapterProviderBridge],
) -> ProviderPluginV1:
    runtime = _ProtocolPluginRuntime(
        spec,
        default_model,
        launch_resolver,
        bridge_type,
    )
    return ProviderPluginV1(
        id=spec.id,
        factory=runtime.factory,
        default_model=default_model,
        model_lister=runtime.models,
        doctor=runtime.doctor,
        capabilities=spec.capabilities,
        route_prefixes=(spec.id,),
        server_policy=ProviderServerPolicyV1(enabled=False),
        support_status="preview",
        configuration_abi_version=PROVIDER_CONFIGURATION_ABI_V1,
        launch_binder=runtime.bind,
        environment_keys=spec.environment.allowed_keys,
    )


class _PiRpcBridge(AdapterProviderBridge):
    _provider_label = "Pi"
    _prompt_id = PI_PROMPT_ID
    _requires_ready = False
    _terminal_event = "agent_settled"

    @staticmethod
    def _state() -> dict:
        return {
            "ack": False,
            "terminal": False,
            "error": False,
            "text": [],
            "fallback": "",
        }

    def _accept(self, turn: Any, record: Mapping) -> Tuple[Any, ...]:
        return tuple(turn.accept_record(record))

    def _feed(
        self,
        record: object,
        state: dict,
    ) -> Tuple[Tuple[Mapping, ...], Tuple[Mapping, ...]]:
        if not isinstance(record, Mapping) or type(record.get("type")) is not str:
            raise ProtocolError(
                "{} RPC returned a malformed frame".format(self._provider_label)
            )
        kind = record["type"]
        outbound = []
        canonical = []

        if kind == "ready":
            return (), ()
        if kind == "response":
            response_id = record.get("id")
            if response_id not in (None, self._prompt_id):
                raise ProtocolError(
                    "{} RPC returned an unmatched response".format(
                        self._provider_label
                    )
                )
            if record.get("command") not in (None, "prompt"):
                return (), ()
            success = record.get("success")
            if type(success) is not bool:
                raise ProtocolError(
                    "{} RPC returned a malformed prompt response".format(
                        self._provider_label
                    )
                )
            state["ack"] = True
            if not success:
                state["error"] = True
                state["terminal"] = True
                canonical.append(
                    {
                        "type": "error",
                        "code": "prompt_rejected",
                        "message": "The provider rejected the prompt.",
                        "retryable": False,
                    }
                )
            data = record.get("data")
            if (
                isinstance(data, Mapping)
                and data.get("agentInvoked") is False
            ):
                state["terminal"] = True
            return tuple(outbound), tuple(canonical)

        if kind == "message_update":
            event = record.get("assistantMessageEvent")
            if isinstance(event, Mapping):
                event_type = event.get("type")
                if event_type == "text_delta" and type(event.get("delta")) is str:
                    delta = event["delta"]
                    state["text"].append(delta)
                    canonical.append({"type": "text_delta", "text": delta})
                elif event_type == "text_end" and type(event.get("content")) is str:
                    state["fallback"] = event["content"]
                elif event_type == "error":
                    state["error"] = True
                    state["terminal"] = True
                    canonical.append(
                        {
                            "type": "error",
                            "code": "provider_error",
                            "message": "The provider reported an RPC error.",
                            "retryable": False,
                        }
                    )
            return (), tuple(canonical)

        if kind in ("message_end", "turn_end"):
            text = _assistant_text(record.get("message"))
            if text:
                state["fallback"] = text
            return (), ()

        if kind == "command_output":
            text = record.get("text", record.get("output"))
            if type(text) is str and text:
                state["text"].append(text)
                canonical.append({"type": "text_delta", "text": text})
            return (), tuple(canonical)

        if kind == "extension_ui_request":
            request_id = record.get("id")
            method = record.get("method")
            if type(request_id) is str and method in _DIALOG_METHODS:
                outbound.append(
                    {
                        "type": "extension_ui_response",
                        "id": request_id,
                        "cancelled": True,
                    }
                )
            return tuple(outbound), ()

        if kind == "host_tool_call":
            request_id = record.get("id")
            if type(request_id) is str:
                outbound.append(
                    {
                        "type": "host_tool_result",
                        "id": request_id,
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "Host tools are unavailable.",
                            }
                        ],
                    }
                )
            return tuple(outbound), ()

        if kind == "host_uri_request":
            request_id = record.get("id")
            if type(request_id) is str:
                outbound.append(
                    {
                        "type": "host_uri_result",
                        "id": request_id,
                        "error": "Host URI access is unavailable.",
                    }
                )
            return tuple(outbound), ()

        if kind == "prompt_result" and record.get("agentInvoked") is False:
            state["terminal"] = True
            return (), ()

        if kind == self._terminal_event:
            if record.get("willRetry", False) is not True:
                state["terminal"] = True
            return (), ()

        if kind in ("extension_error", "error"):
            state["error"] = True
            state["terminal"] = True
            canonical.append(
                {
                    "type": "error",
                    "code": "provider_error",
                    "message": "The provider reported an RPC error.",
                    "retryable": False,
                }
            )
            return (), tuple(canonical)

        return (), ()

    def _final_records(self, state: dict) -> Tuple[Mapping, ...]:
        records = []
        text = "".join(state["text"])
        if not text and state["fallback"]:
            text = state["fallback"]
        if text:
            records.append({"type": "text_final", "text": text})
        records.append(
            {
                "type": "done",
                "reason": "error" if state["error"] else "complete",
            }
        )
        return tuple(records)

    def _iter_jsonl_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: Any,
        token: CancellationToken,
    ) -> Iterator[Any]:
        boundary = self._adapter.open_transport(
            self._inspection,
            prompt,
            values,
            cwd=self.cwd,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
            cancellation=token,
            limits=self._limits,
        )
        if type(boundary) is not ProtocolLaunchBoundaryV1:
            raise ProtocolError("provider returned an incompatible RPC boundary")
        state = self._state()
        stdin_closed = False
        try:
            if self._requires_ready:
                ready = boundary.receive()
                if not isinstance(ready, Mapping) or ready.get("type") != "ready":
                    raise ProtocolError(
                        "{} RPC did not emit its ready frame".format(
                            self._provider_label
                        )
                    )
            boundary.send(
                {
                    "id": self._prompt_id,
                    "type": "prompt",
                    "message": prompt,
                }
            )
            while True:
                record = boundary.receive()
                if record is None:
                    break
                outbound, canonical = self._feed(record, state)
                for frame in outbound:
                    boundary.send(frame)
                for item in canonical:
                    for message in self._accept(turn, item):
                        yield message
                if state["ack"] and state["terminal"] and not stdin_closed:
                    boundary.close_stdin()
                    stdin_closed = True
            if not state["ack"] or not state["terminal"]:
                raise ProtocolError(
                    "{} RPC ended before the turn completed".format(
                        self._provider_label
                    )
                )
            for item in self._final_records(state):
                for message in self._accept(turn, item):
                    yield message
            turn.finish()
        finally:
            boundary.close()

    async def _aiter_jsonl_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: Any,
        token: CancellationToken,
    ) -> Any:
        boundary = self._adapter.open_transport(
            self._inspection,
            prompt,
            values,
            cwd=self.cwd,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
            cancellation=token,
            limits=self._limits,
        )
        if type(boundary) is not ProtocolLaunchBoundaryV1:
            raise ProtocolError("provider returned an incompatible RPC boundary")
        state = self._state()
        stdin_closed = False
        try:
            if self._requires_ready:
                ready = await boundary.receive_async()
                if not isinstance(ready, Mapping) or ready.get("type") != "ready":
                    raise ProtocolError(
                        "{} RPC did not emit its ready frame".format(
                            self._provider_label
                        )
                    )
            await boundary.send_async(
                {
                    "id": self._prompt_id,
                    "type": "prompt",
                    "message": prompt,
                }
            )
            while True:
                record = await boundary.receive_async()
                if record is None:
                    break
                outbound, canonical = self._feed(record, state)
                for frame in outbound:
                    await boundary.send_async(frame)
                for item in canonical:
                    for message in self._accept(turn, item):
                        yield message
                if state["ack"] and state["terminal"] and not stdin_closed:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, boundary.close_stdin)
                    stdin_closed = True
            if not state["ack"] or not state["terminal"]:
                raise ProtocolError(
                    "{} RPC ended before the turn completed".format(
                        self._provider_label
                    )
                )
            for item in self._final_records(state):
                for message in self._accept(turn, item):
                    yield message
            turn.finish()
        finally:
            await boundary.close_async()


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="pi",
    display_name="Pi Coding Agent",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="pi",
        expected_identity="pi",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="pi ",
            identity_marker="pi ",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat", "stream")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "chat": "--mode",
                "stream": "--no-session",
            },
            identity_marker="Usage: pi",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=PI_RPC_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.JSONL,
    environment=EnvironmentPolicy(fixed_values=PI_FIXED_ENVIRONMENT),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset(
        (ProviderCapability.CHAT.value, ProviderCapability.STREAM.value)
    ),
    server_policy=AdapterServerPolicy(enabled=False),
)


PLUGIN = _protocol_plugin(
    ADAPTER_SPEC,
    default_model=PI_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="pi",
        executable="pi",
        package_names=(PI_OFFICIAL_PACKAGE,),
    ),
    bridge_type=_PiRpcBridge,
)


__all__ = [
    "ADAPTER_SPEC",
    "PI_DEFAULT_MODEL",
    "PI_FIXED_ENVIRONMENT",
    "PI_OFFICIAL_PACKAGE",
    "PI_PROMPT_ID",
    "PI_RPC_FIXED_ARGV",
    "PLUGIN",
]
