"""Stage 5B-5F contract checks for inert external provider entry points."""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace

import pytest

from unified_cli import (
    PROVIDERS,
    ProviderPluginV1,
    UnifiedError,
    create,
    doctor_provider,
    list_models,
    list_providers,
)
from unified_cli import registry as core_registry
from unified_cli_ext.providers import (
    AdapterStatus,
    PromptMode,
    PromptSentinelPolicy,
    ProviderAdapterSpecV1,
    ProviderCapability,
)
from unified_cli_ext.providers.held import (
    HELD_UNAVAILABLE_MESSAGE,
    HeldProviderUnavailableError,
)


ENTRY_POINTS = {
    "grok": "unified_cli_ext.providers.grok:PLUGIN",
    "kimi": "unified_cli_ext.providers.kimi:PLUGIN",
    "copilot": "unified_cli_ext.providers.copilot:PLUGIN",
    "cursor": "unified_cli_ext.providers.cursor:PLUGIN",
    "codebuddy": "unified_cli_ext.providers.codebuddy:PLUGIN",
    "qoder": "unified_cli_ext.providers.qoder:PLUGIN",
    "mistral-vibe": "unified_cli_ext.providers.mistral_vibe:PLUGIN",
    "qwen": "unified_cli_ext.providers.qwen:PLUGIN",
    "cline": "unified_cli_ext.providers.cline:PLUGIN",
    "opencode": "unified_cli_ext.providers.opencode:PLUGIN",
    "kilo": "unified_cli_ext.providers.kilo:PLUGIN",
    "droid": "unified_cli_ext.providers.droid:PLUGIN",
    "pi": "unified_cli_ext.providers.pi:PLUGIN",
    "oh-my-pi": "unified_cli_ext.providers.oh_my_pi:PLUGIN",
    "hermes": "unified_cli_ext.providers.hermes:PLUGIN",
    "poolside": "unified_cli_ext.providers.poolside:PLUGIN",
    "amp": "unified_cli_ext.providers.amp:PLUGIN",
    "gitlab-duo": "unified_cli_ext.providers.gitlab_duo:PLUGIN",
}

STAGE_6_RESEARCH_PROVIDER_IDS = ("grok", "kimi", "copilot", "cursor")

EXPECTED_COMMANDS = {
    "grok": {
        "executable": "grok",
        "prompt": (
            "--no-auto-update",
            "--sandbox",
            "strict",
            "--permission-mode",
            "dontAsk",
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
            "--disable-web-search",
            "--no-subagents",
            "--no-memory",
            "--verbatim",
            "--output-format",
            "streaming-json",
        ),
        "transport": "jsonl",
        "environment": frozenset(("GROK_DISABLE_AUTOUPDATER",)),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "-p",
    },
    "kimi": {
        "executable": "kimi",
        "prompt": ("--output-format", "stream-json"),
        "transport": "jsonl",
        "environment": frozenset(
            ("KIMI_CODE_NO_AUTO_UPDATE", "KIMI_DISABLE_TELEMETRY")
        ),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "-p",
    },
    "copilot": {
        "executable": "copilot",
        "feature_probe": ("help",),
        "prompt": (
            "--silent",
            "--no-ask-user",
            "--no-auto-update",
            "--no-custom-instructions",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--available-tools",
            "view,glob,grep",
            "--deny-tool=write",
            "--deny-tool=shell",
            "--deny-tool=url",
            "--deny-tool=memory",
            "--output-format=text",
        ),
        "transport": "plain",
        "environment": frozenset(("COPILOT_HOME",)),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "-p",
    },
    "cursor": {
        "executable": "agent",
        "prompt": ("--help",),
        "transport": "json",
        "environment": frozenset(("CURSOR_API_KEY",)),
        "mode": PromptMode.STDIN,
        "prompt_option": None,
    },
    "codebuddy": {
        "executable": "codebuddy",
        "prompt": (
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--include-partial-messages",
            "--strict-mcp-config",
        ),
        "transport": "jsonl",
        "environment": frozenset(("DISABLE_AUTOUPDATER",)),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "qoder": {
        "executable": "qodercli",
        "prompt": ("--acp",),
        "transport": "acp",
        "environment": frozenset(("QODER_PERSONAL_ACCESS_TOKEN",)),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "mistral-vibe": {
        "executable": "vibe",
        "prompt": (
            "--output",
            "streaming",
            "--agent",
            "plan",
            "--disabled-tools",
            "*",
        ),
        "transport": "jsonl",
        "environment": frozenset(),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "--prompt",
    },
    "qwen": {
        "executable": "qwen",
        "prompt": ("--output-format", "stream-json"),
        "transport": "jsonl",
        "environment": frozenset(),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "--prompt",
    },
    "cline": {
        "executable": "cline",
        "prompt": ("--json",),
        "transport": "jsonl",
        "environment": frozenset(("CLINE_NO_AUTO_UPDATE",)),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "opencode": {
        "executable": "opencode",
        "prompt": ("--pure", "run", "--format", "json"),
        "transport": "jsonl",
        "environment": frozenset(
            (
                "OPENCODE_DISABLE_AUTOUPDATE",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                "OPENCODE_DISABLE_LSP_DOWNLOAD",
                "OPENCODE_DISABLE_MODELS_FETCH",
                "OPENCODE_DISABLE_CLAUDE_CODE",
            )
        ),
        "mode": PromptMode.POSITIONAL_AFTER_SENTINEL,
        "prompt_option": None,
    },
    "kilo": {
        "executable": "kilo",
        "prompt": (
            "--pure",
            "acp",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--no-mdns",
        ),
        "transport": "acp",
        "environment": frozenset(("KILO_DISABLE_AUTOUPDATE",)),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "droid": {
        "executable": "droid",
        "prompt": (
            "exec",
            "--input-format",
            "stream-jsonrpc",
            "--output-format",
            "stream-jsonrpc",
        ),
        "transport": "jsonrpc",
        "environment": frozenset(
            ("FACTORY_API_KEY", "FACTORY_DROID_AUTO_UPDATE_ENABLED")
        ),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "pi": {
        "executable": "pi",
        "prompt": (
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
        ),
        "transport": "jsonl",
        "environment": frozenset(),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "oh-my-pi": {
        "executable": "omp",
        "prompt": (
            "--mode",
            "rpc",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-lsp",
            "--no-pty",
            "--no-prewalk",
            "--no-title",
            "--approval-mode",
            "always-ask",
        ),
        "transport": "jsonl",
        "environment": frozenset(),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "hermes": {
        "executable": "hermes",
        "prompt": ("acp",),
        "transport": "acp",
        "environment": frozenset(),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "poolside": {
        "executable": "pool",
        "prompt": ("acp",),
        "transport": "acp",
        "environment": frozenset(
            (
                "POOLSIDE_API_KEY",
                "POOLSIDE_TOKEN",
                "POOLSIDE_API_URL",
                "POOLSIDE_STANDALONE_BASE_URL",
                "POOLSIDE_STANDALONE_MODEL",
            )
        ),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "amp": {
        "executable": "amp",
        "prompt": ("--execute", "--stream-json", "--stream-json-input"),
        "transport": "jsonl",
        "environment": frozenset(("AMP_API_KEY", "AMP_SKIP_UPDATE_CHECK")),
        "mode": PromptMode.PROTOCOL,
        "prompt_option": None,
    },
    "gitlab-duo": {
        "executable": "duo",
        "prompt": ("run", "--output-format", "json"),
        "transport": "json",
        "environment": frozenset(
            (
                "GITLAB_TOKEN",
                "GITLAB_OAUTH_TOKEN",
                "GITLAB_BASE_URL",
                "GITLAB_URL",
                "GITLAB_DUO_MODEL",
            )
        ),
        "mode": PromptMode.OPTION_VALUE,
        "prompt_option": "--goal",
    },
}

EVIDENCE_FLAGS = {
    "grok": (
        "GROK_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_XAI_BINARY_NAME_COLLISION_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE",
        "GROK_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
    ),
    "kimi": (
        "KIMI_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_NONINTERACTIVE_AUTO_APPROVAL_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE",
        "KIMI_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
    ),
    "copilot": (
        "COPILOT_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE",
        "COPILOT_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
        "COPILOT_DEDICATED_HOME_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "cursor": (
        "CURSOR_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_PROMPT_FORM_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE",
        "CURSOR_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
    ),
    "codebuddy": (
        "CODEBUDDY_PROTOCOL_FRAME_REQUIRES_STAGE_6_EVIDENCE",
        "CODEBUDDY_NO_TOOLS_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "CODEBUDDY_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "qoder": ("QODER_REQUIRES_STAGE_6_EVIDENCE",),
    "mistral-vibe": (
        "MISTRAL_VIBE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "MISTRAL_VIBE_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
    ),
    "qwen": ("QWEN_REQUIRES_STAGE_6_EVIDENCE",),
    "cline": (
        "CLINE_ONE_SHOT_LIFECYCLE_REQUIRES_STAGE_6_EVIDENCE",
        "CLINE_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "CLINE_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "CLINE_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
    ),
    "opencode": (
        "OPENCODE_ONE_SHOT_STDIN_EOF_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_PERMISSION_CONFIG_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_PROCESS_SESSION_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_HTTP_SSE_SEPARATE_REQUIRES_STAGE_6_EVIDENCE",
        "OPENCODE_ACP_SEPARATE_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "kilo": (
        "KILO_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "KILO_ACP_LIFECYCLE_REQUIRES_STAGE_6_EVIDENCE",
        "KILO_LOOPBACK_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "KILO_PERMISSION_CONFIG_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "KILO_AUTH_SESSION_MODEL_EVENT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "droid": (
        "DROID_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_STREAM_JSONRPC_ENVELOPE_PROTOCOL_VERSION_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_SESSION_NOTIFICATION_TURN_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_PERMISSION_ASK_USER_DEFAULT_DENY_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_AUTH_ACCOUNT_BILLING_POLICY_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_MODEL_IMAGE_MCP_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_RESUME_FORK_INTERRUPT_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_PROCESS_BACKPRESSURE_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_UPDATE_REMOVAL_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "DROID_SDK_CLI_PROTOCOL_DRIFT_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "pi": (
        "PI_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "PI_RPC_FRAMING_EVENT_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "PI_AUTH_MODEL_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "PI_TOOL_RESOURCE_PERMISSION_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "PI_OFFLINE_UPDATE_PACKAGE_TELEMETRY_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE",
        "PI_RPC_CANCEL_STDIN_EOF_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "PI_SESSION_RESUME_IMAGE_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "oh-my-pi": (
        "OH_MY_PI_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_RPC_READY_FRAMING_COMPLETION_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_CONFIG_ENV_AUTH_MODEL_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_TOOL_EXTENSION_RULE_MCP_SUBAGENT_PERMISSION_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_RPC_CANCEL_STDIN_EOF_WORKER_MCP_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_SESSION_RESUME_IMAGE_REQUIRES_STAGE_6_EVIDENCE",
        "OH_MY_PI_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE",
        "OH_MY_PI_UPDATE_CHECK_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "hermes": (
        "HERMES_ACP_0_9_0_VS_EXT_0_11_X_COMPATIBILITY_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_VERSION_HELP_ACP_CHECK_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_ACP_NEGOTIATION_EVENT_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_AUTH_MODEL_CONFIG_HOME_PROFILE_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_ACP_PERMISSION_ALLOWLIST_TOOL_MCP_PLUGIN_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_ACP_CANCEL_STDIO_EOF_SESSION_WORKER_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_ACP_SESSION_PERSISTENCE_RESUME_DOC_DRIFT_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_ACP_NON_TEXT_IMAGE_LIMIT_REQUIRES_STAGE_6_EVIDENCE",
        "HERMES_TUI_JSONRPC_AND_HTTP_SSE_REQUIRE_SEPARATE_STAGE_6_EVIDENCE",
        "HERMES_INSTALL_CHANNEL_UPDATE_POSTINSTALL_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "poolside": (
        "POOLSIDE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_INSTALL_CHANNEL_BINARY_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_ACP_HANDSHAKE_EVENT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_AUTH_MODEL_SESSION_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_PERMISSION_TOOL_MCP_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_IMAGE_USAGE_ERROR_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_PROCESS_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_EXEC_JSONL_SEPARATE_REQUIRES_STAGE_6_EVIDENCE",
        "POOLSIDE_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "amp": (
        "AMP_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_INSTALL_CHANNEL_BINARY_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_STREAM_JSON_INPUT_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_TEXT_TOOL_USAGE_ERROR_IMAGE_NORMALIZATION_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_AUTH_LOGIN_LOGOUT_STATUS_BILLING_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_SESSION_CONTINUE_RESUME_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_PERMISSION_TOOL_PLUGIN_MCP_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_CANCEL_STEER_STDIN_EOF_PROCESS_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_UPDATE_REMOVAL_SETTINGS_ENV_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "AMP_SDK_CLI_SCHEMA_DRIFT_REQUIRES_STAGE_6_EVIDENCE",
    ),
    "gitlab-duo": (
        "GITLAB_DUO_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_BINARY_GENERIC_PACKAGE_NPM_PROVENANCE_HASH_SIGNATURE_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_BARE_SEMVER_SEPARATE_IDENTITY_PROBE_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_RUN_GOAL_OPTION_SINGLE_JSON_STDOUT_STDERR_EXIT_CODE_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_RUN_JSON_SCHEMA_1_0_NORMALIZATION_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_JSON_EMPTY_OUTPUT_SERIALIZATION_FAILURE_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_HEADLESS_AUTO_APPROVAL_SANDBOX_BOUNDARY_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_TOOL_MCP_HOOK_SKILL_PROJECT_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_AUTH_GLAB_HELPER_CONFIG_HOME_ENV_SECRET_ISOLATION_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_SESSION_NEW_RESUME_PLAN_APPROVAL_CONTEXT_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_MODEL_CONTEXT_INSTRUCTION_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_CANCEL_SIGNAL_WEBSOCKET_RETRY_PROCESS_CHILD_MCP_CLEANUP_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_USAGE_ERROR_REASONING_IMAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_TELEMETRY_LOG_UPDATE_CONFIG_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_CI_CREDITS_SUBSCRIPTION_NAMESPACE_QUOTA_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_COMPILED_BINARY_NPM_CHANNEL_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE",
        "GITLAB_DUO_WINDOWS_REQUIRES_SEPARATE_TRANSPORT_EVIDENCE",
    ),
}


def _module(provider_id):
    module_name = ENTRY_POINTS[provider_id].partition(":")[0]
    return importlib.import_module(module_name)


def test_pyproject_registers_all_held_provider_entry_points_exactly():
    package_root = pathlib.Path(__file__).resolve().parents[1]
    text = (package_root / "pyproject.toml").read_text(encoding="utf-8")
    group = '[project.entry-points."unified_cli.providers.v1"]'
    assert group in text
    section = text.split(group, 1)[1].split("\n[", 1)[0]
    declared = {}
    for line in section.strip().splitlines():
        provider_id, _, target = line.partition(" = ")
        declared[provider_id] = target.strip().strip('"')
    assert len(declared) == 18
    assert declared == ENTRY_POINTS


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_held_specs_and_plugins_are_immutable_and_minimal(provider_id):
    module = _module(provider_id)
    spec = module.ADAPTER_SPEC
    plugin = module.PLUGIN
    expected = EXPECTED_COMMANDS[provider_id]

    assert type(spec) is ProviderAdapterSpecV1
    assert spec.id == provider_id
    assert spec.status is AdapterStatus.HELD
    assert spec.binary.executable == expected["executable"]
    assert spec.binary.version_probe.command.argv == expected.get(
        "version_probe", ("--version",)
    )
    assert spec.binary.feature_probe.command.argv == expected.get(
        "feature_probe", ("--help",)
    )
    assert spec.prompt.fixed_argv == expected["prompt"]
    assert spec.prompt.mode is expected["mode"]
    assert spec.prompt.prompt_option == expected["prompt_option"]
    if expected["mode"] is PromptMode.POSITIONAL_AFTER_SENTINEL:
        assert spec.prompt.sentinel_policy is PromptSentinelPolicy.REQUIRED
    else:
        assert spec.prompt.sentinel_policy is PromptSentinelPolicy.FORBIDDEN
    assert spec.transport.value == expected["transport"]
    assert spec.environment.allowed_keys == expected["environment"]
    assert spec.environment.required_keys == frozenset()
    assert spec.capabilities == frozenset((ProviderCapability.CHAT.value,))
    assert spec.auth is None
    assert spec.models is None
    assert spec.doctor is None
    assert spec.server_policy.enabled is False
    assert spec.server_policy.requires_external_isolation is True
    with pytest.raises(FrozenInstanceError):
        spec.id = "changed"  # type: ignore[misc]

    assert type(plugin) is ProviderPluginV1
    assert plugin.id == provider_id
    assert plugin.support_status == "held"
    assert plugin.capabilities == frozenset()
    assert plugin.route_prefixes == (provider_id,)
    assert plugin.server_policy.enabled is False
    assert plugin.model_lister() == ()
    doctor = plugin.doctor()
    assert dict(doctor) == {
        "id": provider_id,
        "status": "Held",
        "available": False,
        "message": HELD_UNAVAILABLE_MESSAGE,
    }
    with pytest.raises(TypeError):
        doctor["available"] = True


@pytest.mark.parametrize("provider_id", tuple(EVIDENCE_FLAGS))
def test_held_entries_record_every_remaining_evidence_gate(provider_id):
    module = _module(provider_id)
    expected = set(EVIDENCE_FLAGS[provider_id])
    evidence_prefix = provider_id.upper().replace("-", "_") + "_"
    recorded = {
        name
        for name, value in vars(module).items()
        if value is True
        and (
            name.endswith("_STAGE_6_EVIDENCE")
            or (name.startswith(evidence_prefix) and name.endswith("_EVIDENCE"))
        )
    }
    assert recorded == expected
    for flag in expected:
        assert getattr(module, flag) is True


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_held_factories_fail_before_provider_creation_or_execution(provider_id, monkeypatch):
    module = _module(provider_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("held factory attempted external execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    with pytest.raises(HeldProviderUnavailableError) as caught:
        module.PLUGIN.factory(model="ignored")
    assert str(caught.value) == HELD_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_core_held_gate_never_calls_plugin_callbacks(provider_id, monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}

    def forbidden(name):
        def callback(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError("Core called a Held plugin callback")

        return callback

    plugin = replace(
        _module(provider_id).PLUGIN,
        factory=forbidden("factory"),
        model_lister=forbidden("models"),
        doctor=forbidden("doctor"),
    )

    class FakeEntryPoint:
        group = core_registry.ENTRY_POINT_GROUP
        name = provider_id
        load_calls = 0

        def load(self):
            self.load_calls += 1
            return plugin

    entry_point = FakeEntryPoint()
    core_registry._reset_provider_registry_for_tests()
    monkeypatch.setattr(
        core_registry.importlib_metadata,
        "entry_points",
        lambda: [entry_point],
    )
    try:
        discovered = list_providers(include_ext=True)[-1]
        assert discovered.lifecycle_status == "discovered"
        assert discovered.support_status == "unknown"
        assert entry_point.load_calls == 0

        for call in (
            lambda: create(provider_id),
            lambda: list_models(provider_id),
            lambda: doctor_provider(provider_id),
        ):
            with pytest.raises(
                UnifiedError, match="is held",
            ):
                call()

        assert calls == {"factory": 0, "models": 0, "doctor": 0}
        assert entry_point.load_calls == 1
        loaded = list_providers(include_ext=True)[-1]
        assert loaded.lifecycle_status == "loaded"
        assert loaded.support_status == "held"
        assert loaded.default_model is None
        assert loaded.capabilities == frozenset()
    finally:
        core_registry._reset_provider_registry_for_tests()


def test_base_import_and_entry_point_metadata_enumeration_do_not_import_plugins():
    root = pathlib.Path(__file__).resolve().parents[3]
    ext_source = root / "packages" / "unified-cli-ext" / "src"
    script = r'''
import importlib.metadata
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, {root!r})
sys.path.insert(0, {ext_source!r})
def forbidden(*args, **kwargs):
    raise AssertionError("passive discovery attempted an external operation")
subprocess.Popen = forbidden
shutil.which = forbidden
socket.create_connection = forbidden
import unified_cli_ext
assert not any(name.startswith("unified_cli_ext.providers.") and name.rsplit(".", 1)[-1] in {providers!r} for name in sys.modules)
importlib.metadata.entry_points()
assert not any(name.startswith("unified_cli_ext.providers.") and name.rsplit(".", 1)[-1] in {providers!r} for name in sys.modules)
'''.format(
        root=str(root),
        ext_source=str(ext_source),
        providers=tuple(
            target.partition(":")[0].rsplit(".", 1)[-1]
            for target in ENTRY_POINTS.values()
        ),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_core_builtins_are_unchanged_by_held_extension_metadata():
    assert tuple(PROVIDERS) == ("claude", "codex", "gemini")
    assert [item.id for item in list_providers()] == ["claude", "codex", "gemini"]
    assert create("claude", bin_path="/bin/echo").name == "claude"


@pytest.mark.parametrize("provider_id", STAGE_6_RESEARCH_PROVIDER_IDS)
def test_stage_6_research_metadata_is_static_and_not_capture_evidence(provider_id):
    module = _module(provider_id)
    prefix = provider_id.upper()
    expected_versions = {
        "grok": "0.2.106",
        "kimi": "0.28.1",
        "copilot": "1.0.73",
        "cursor": "2026.07.20-8cc9c0b",
    }
    sources = getattr(module, prefix + "_OFFICIAL_SOURCES")
    target = getattr(module, prefix + "_STAGE_6_TARGET_VERSION")

    assert type(sources) is tuple
    assert sources
    assert all(type(url) is str and url.startswith("https://") for url in sources)
    assert type(target) is str
    assert target == expected_versions[provider_id]
    assert getattr(module, prefix + "_STAGE_6_EVIDENCE_CAPTURED") is False
    assert module.ADAPTER_SPEC.status is AdapterStatus.HELD
    assert module.PLUGIN.support_status == "held"
    assert module.PLUGIN.capabilities == frozenset()
    assert module.PLUGIN.server_policy.enabled is False


@pytest.mark.parametrize("provider_id", STAGE_6_RESEARCH_PROVIDER_IDS)
def test_researched_held_import_factory_doctor_and_models_are_ambient_free(
    provider_id,
):
    root = pathlib.Path(__file__).resolve().parents[3]
    ext_source = root / "packages" / "unified-cli-ext" / "src"
    module_name = ENTRY_POINTS[provider_id].partition(":")[0]
    script = r'''
import importlib
import os
import pathlib
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, {root!r})
sys.path.insert(0, {ext_source!r})
from unified_cli_ext.providers.held import HeldProviderUnavailableError

def forbidden(*args, **kwargs):
    raise AssertionError("Held metadata attempted ambient access or execution")

class ForbiddenEnvironment:
    def __contains__(self, key):
        return forbidden(key)
    def __getitem__(self, key):
        return forbidden(key)
    def __iter__(self):
        return forbidden()
    def __len__(self):
        return forbidden()
    def copy(self):
        return forbidden()
    def get(self, key, default=None):
        return forbidden(key, default)
    def items(self):
        return forbidden()
    def keys(self):
        return forbidden()

os.environ = ForbiddenEnvironment()
os.getenv = forbidden
os.system = forbidden
pathlib.Path.home = forbidden
shutil.which = forbidden
socket.create_connection = forbidden
subprocess.Popen = forbidden
subprocess.call = forbidden
subprocess.check_call = forbidden
subprocess.check_output = forbidden
subprocess.run = forbidden

module = importlib.import_module({module_name!r})
assert module.PLUGIN.model_lister() == ()
doctor = module.PLUGIN.doctor()
assert doctor["available"] is False
try:
    module.PLUGIN.factory(model="ignored")
except HeldProviderUnavailableError:
    pass
else:
    raise AssertionError("Held factory did not refuse")
'''.format(
        root=str(root),
        ext_source=str(ext_source),
        module_name=module_name,
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_grok_requires_xai_identity_and_rejects_third_party_name_collision():
    module = _module("grok")
    assert module.GROK_OFFICIAL_PACKAGE == "@xai-official/grok"
    assert module.GROK_REJECTED_PACKAGE_IDENTITIES == ("@vibe-kit/grok-cli",)
    assert module.GROK_BINARY_IDENTITY_IS_VERIFIED is False
    assert module.ADAPTER_SPEC.binary.executable == "grok"
    assert module.GROK_XAI_BINARY_NAME_COLLISION_REQUIRES_STAGE_6_EVIDENCE is True
    assert "GROK_DISABLE_AUTOUPDATER" in module.ADAPTER_SPEC.environment.allowed_keys


def test_kimi_documents_auto_approval_without_claiming_safe_execution():
    module = _module("kimi")
    assert module.KIMI_OFFICIAL_PACKAGE == "@moonshot-ai/kimi-code"
    assert module.KIMI_NPM_MINIMUM_NODE_VERSION == "22.19"
    assert module.KIMI_PROMPT_USES_OS_WORKING_DIRECTORY is True
    assert module.KIMI_TUI_LOGOUT_COMMAND == "/logout"
    assert module.KIMI_NONINTERACTIVE_AUTO_APPROVAL_REQUIRES_STAGE_6_EVIDENCE
    assert module.PLUGIN.capabilities == frozenset()


def test_copilot_candidate_keeps_every_documented_containment_control():
    module = _module("copilot")
    fixed = module.ADAPTER_SPEC.prompt.fixed_argv
    assert fixed == module.COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV
    for control in (
        "--no-custom-instructions",
        "--no-remote",
        "--no-remote-export",
        "--disable-builtin-mcps",
        "--deny-tool=write",
        "--deny-tool=shell",
        "--deny-tool=url",
        "--deny-tool=memory",
        "--output-format=text",
    ):
        assert control in fixed
    assert module.ADAPTER_SPEC.environment.allowed_keys == frozenset(
        ("COPILOT_HOME",)
    )


def test_cursor_records_that_stage_6_must_establish_prompt_framing():
    module = _module("cursor")
    assert module.CURSOR_PROMPT_FORM_REQUIRES_STAGE_6_EVIDENCE is True
    assert module.CURSOR_PROMPT_COMMAND_IS_ABI_REPRESENTABLE is False
    assert module.CURSOR_PRIMARY_EXECUTABLE == "agent"
    assert module.CURSOR_LEGACY_EXECUTABLE == "cursor-agent"
    assert module.CURSOR_LEGACY_ALIAS_SINCE == "2026-01-08"
    assert module.ADAPTER_SPEC.binary.executable == "agent"
    assert module.ADAPTER_SPEC.prompt.fixed_argv == module.CURSOR_INERT_PROMPT_PLACEHOLDER
    assert module.CURSOR_DOCUMENTED_PRINT_OPTIONS != module.ADAPTER_SPEC.prompt.fixed_argv
    assert "CURSOR_API_KEY" not in module.ADAPTER_SPEC.prompt.fixed_argv
    assert "--" not in module.ADAPTER_SPEC.prompt.fixed_argv
