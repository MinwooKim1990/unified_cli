"""Read-only Preview adapter for the official xAI Grok Build CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from ..errors import ConfigurationError, ProtocolError
from .bridge import adapter_plugin
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    DynamicArgument,
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


GROK_OFFICIAL_SOURCES = (
    "https://github.com/xai-org/grok-build",
    "https://docs.x.ai/build/overview",
    "https://docs.x.ai/build/cli/reference",
    "https://docs.x.ai/build/cli/headless-scripting",
    "https://docs.x.ai/build/enterprise",
)
GROK_OFFICIAL_PACKAGE = "@xai-official/grok"
GROK_OFFICIAL_INSTALLER = "https://x.ai/cli/install.sh"
GROK_STAGE_6_TARGET_VERSION = "0.2.110"
GROK_REJECTED_PACKAGE_IDENTITIES = ("@vibe-kit/grok-cli",)
GROK_REAL_AUTHENTICATED_SMOKE_CAPTURED = False

_BLOCKED_WORKSPACE_ENTRIES = (
    (".grok",),
    (".envrc",),
    (".mcp.json",),
    (".cursor", "mcp.json"),
    (".cursor", "hooks.json"),
    (".claude",),
)
_BLOCKED_HOME_ENTRIES = (
    (".bashrc",),
    (".bash_profile",),
    (".bash_login",),
    (".profile",),
    (".bash_logout",),
    (".grok", "config.toml"),
    (".grok", "managed_config.toml"),
    (".grok", "requirements.toml"),
    (".grok", "plugins"),
    (".grok", "hooks"),
    (".grok", "hooks-paths"),
    (".claude.json",),
    (".claude",),
    (".cursor", "mcp.json"),
    (".cursor", "hooks.json"),
)
_BLOCKED_SYSTEM_ENTRIES = (
    "/etc/grok/managed_config.toml",
    "/etc/grok/requirements.toml",
)

_PROBE_LIMITS = OperationLimits(
    timeout_seconds=10.0,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=16 * 1024,
    max_events=8,
)
_PROMPT_LIMITS = OperationLimits(
    timeout_seconds=120.0,
    max_stdout_bytes=16 * 1024 * 1024,
    max_stderr_bytes=1024 * 1024,
    max_events=50_000,
)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(tuple(argv), limits=_PROBE_LIMITS)


GROK_HEADLESS_FIXED_ARGV = (
    "--no-auto-update",
    "--sandbox",
    "strict",
    "--permission-mode",
    "dontAsk",
    "--tools",
    "read_file,grep,list_dir",
    "--allow",
    "Read",
    "--allow",
    "Grep",
    "--deny",
    "Bash",
    "--deny",
    "Edit",
    "--deny",
    "MCPTool",
    "--deny",
    "WebFetch",
    "--deny",
    "WebSearch",
    "--no-plan",
    "--no-subagents",
    "--no-memory",
    "--disable-web-search",
    "--output-format",
    "streaming-json",
)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="grok",
    display_name="xAI Grok Build",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="grok",
        expected_identity="grok",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0, 2, 110),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="grok ",
            identity_marker="grok ",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat", "sessions", "stream")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "chat": "-p, --single",
                "sessions": "-r, --resume",
                "stream": "--output-format",
            },
            identity_marker="Usage: grok",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=GROK_HEADLESS_FIXED_ARGV,
        dynamic_arguments=(
            DynamicArgument("model", "-m", required=True),
            DynamicArgument("session", "-r"),
        ),
        mode=PromptMode.OPTION_VALUE,
        prompt_option="-p",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.JSONL,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("XAI_API_KEY",)),
        fixed_values={
            "GROK_MANAGED_MCPS_ENABLED": "false",
            "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
        },
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("inspect", "--json"))),
    capabilities=frozenset(
        (
            ProviderCapability.CHAT.value,
            ProviderCapability.STREAM.value,
            ProviderCapability.SESSIONS.value,
        )
    ),
    server_policy=AdapterServerPolicy(enabled=False),
)


def _state() -> dict:
    return {"ended": False, "stop_reason": None}


def _nonempty_string(record: Mapping, name: str) -> str:
    value = record.get(name)
    if type(value) is not str or not value:
        raise ProtocolError("Grok returned a malformed end record")
    return value


def _usage_counter(usage: Mapping, name: str) -> int:
    value = usage.get(name)
    if type(value) is not int or value < 0 or value > 10**15:
        raise ProtocolError("Grok returned malformed usage counters")
    return value


def _entry_exists(root: str, parts: tuple[str, ...]) -> bool:
    return os.path.lexists(os.path.join(root, *parts))


def _validate_provider_configuration(provider_home: object) -> None:
    if provider_home is not None:
        if type(provider_home) is not str or not os.path.isabs(provider_home):
            raise ConfigurationError("Grok Preview provider home is invalid")
        home = os.path.realpath(provider_home)
        if any(_entry_exists(home, parts) for parts in _BLOCKED_HOME_ENTRIES):
            raise ConfigurationError(
                "Grok Preview refuses provider-home tool, plugin, or hook configuration"
            )
    if any(os.path.lexists(path) for path in _BLOCKED_SYSTEM_ENTRIES):
        raise ConfigurationError(
            "Grok Preview refuses system-managed runtime configuration"
        )


def _validate_runtime_boundary(cwd: object, provider_home: object) -> None:
    _validate_provider_configuration(provider_home)
    if type(cwd) is not str or not os.path.isabs(cwd):
        raise ConfigurationError("Grok Preview requires an explicit absolute cwd")
    root = os.path.realpath(cwd)
    if not os.path.isdir(root):
        raise ConfigurationError("Grok Preview cwd is unavailable")

    current = root
    git_root = None
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            git_root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    current = root
    while True:
        if any(_entry_exists(current, parts) for parts in _BLOCKED_WORKSPACE_ENTRIES):
            raise ConfigurationError(
                "Grok Preview refuses project tool, plugin, or hook configuration"
            )
        if git_root is None or current == git_root:
            break
        current = os.path.dirname(current)


def _map_record(record: Mapping, state: dict):
    if not isinstance(record, Mapping) or type(state) is not dict:
        raise ProtocolError("Grok returned a malformed stream record")
    if state.get("ended") is not False:
        raise ProtocolError("Grok returned a record after end")
    kind = record.get("type")
    if kind in ("text", "thought"):
        if set(record) != {"type", "data"} or type(record.get("data")) is not str:
            raise ProtocolError("Grok returned a malformed stream record")
        if kind == "thought":
            return ()
        return ({"type": "text_delta", "text": record["data"]},)
    if kind == "end":
        required = {"type", "stopReason", "sessionId", "requestId"}
        if not required <= set(record):
            raise ProtocolError("Grok returned a malformed end record")
        stop_reason = _nonempty_string(record, "stopReason")
        session_id = _nonempty_string(record, "sessionId")
        _nonempty_string(record, "requestId")
        usage_is_incomplete = record.get("usage_is_incomplete", False)
        if type(usage_is_incomplete) is not bool or usage_is_incomplete:
            raise ProtocolError("Grok returned incomplete usage counters")
        events = [{"type": "session", "session_id": session_id}]
        usage = record.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ProtocolError("Grok returned malformed usage counters")
            events.append(
                {
                    "type": "usage",
                    "input_tokens": _usage_counter(usage, "input_tokens"),
                    "cached_input_tokens": _usage_counter(
                        usage, "cache_read_input_tokens"
                    ),
                    "output_tokens": _usage_counter(usage, "output_tokens"),
                }
            )
        state["ended"] = True
        state["stop_reason"] = stop_reason
        return tuple(events)
    raise ProtocolError("Grok returned an unknown stream record")


def _finalize(state: dict):
    if type(state) is not dict or state.get("ended") is not True:
        raise ProtocolError("Grok stream ended without an end record")
    reason = state.get("stop_reason")
    if type(reason) is not str or not reason:
        raise ProtocolError("Grok stream end state is malformed")
    return ({"type": "done", "reason": reason},)


_BASE_PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model="grok-build",
    state_factory=_state,
    map_record=_map_record,
    finalize=_finalize,
    turn_preflight=_validate_runtime_boundary,
)


def _checked_factory(*args, **kwargs):
    if args:
        raise ConfigurationError(
            "provider factory received unsupported positional options"
        )
    _validate_runtime_boundary(kwargs.get("cwd"), kwargs.get("provider_home"))
    return _BASE_PLUGIN.factory(**kwargs)


def _checked_binder(context):
    bound = _BASE_PLUGIN.launch_binder(context)
    _validate_provider_configuration(bound.provider_home)

    def create_checked(request):
        _validate_runtime_boundary(request.workspace, bound.provider_home)
        return bound.factory(request)

    def doctor_checked():
        _validate_provider_configuration(bound.provider_home)
        return bound.doctor()

    return replace(bound, factory=create_checked, doctor=doctor_checked)


def _checked_doctor():
    _validate_provider_configuration(None)
    return _BASE_PLUGIN.doctor()


PLUGIN = replace(
    _BASE_PLUGIN,
    factory=_checked_factory,
    doctor=_checked_doctor,
    launch_binder=_checked_binder,
)


__all__ = [
    "ADAPTER_SPEC",
    "GROK_HEADLESS_FIXED_ARGV",
    "GROK_OFFICIAL_INSTALLER",
    "GROK_OFFICIAL_PACKAGE",
    "GROK_OFFICIAL_SOURCES",
    "GROK_REAL_AUTHENTICATED_SMOKE_CAPTURED",
    "GROK_REJECTED_PACKAGE_IDENTITIES",
    "GROK_STAGE_6_TARGET_VERSION",
    "PLUGIN",
]
