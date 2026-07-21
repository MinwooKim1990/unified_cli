"""Inert Held metadata for a future Poolside Agent CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official proprietary native installer/release, current release v1.0.13.
# https://docs.poolside.ai/cli/install
# https://docs.poolside.ai/cli/cli-reference
# https://github.com/poolsideai/pool/releases/tag/v1.0.13
# https://github.com/poolsideai/pool
# https://github.com/agentclientprotocol/registry/blob/main/poolside/agent.json
# The documented Unix installation location is ``~/.local/bin/pool``; Held
# metadata never resolves, installs, or executes that binary.

# Exact version and help output must be captured in isolated fixtures before
# these provisional markers can be trusted.
POOLSIDE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

# The native release channel, resolved executable identity, and available
# provenance evidence must be captured before the binary can be selected.
POOLSIDE_INSTALL_CHANNEL_BINARY_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True

# ACP handshake and event framing require separate protocol fixtures.
POOLSIDE_ACP_HANDSHAKE_EVENT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Authentication, model selection, and session lifecycle remain unverified.
POOLSIDE_AUTH_MODEL_SESSION_REQUIRES_STAGE_6_EVIDENCE = True

# Permission behavior, tool and MCP controls, and configuration isolation must
# be proven safe before any execution path is enabled.
POOLSIDE_PERMISSION_TOOL_MCP_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Image handling, usage accounting, and error schemas are not supported claims
# until captured evidence establishes their behavior.
POOLSIDE_IMAGE_USAGE_ERROR_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Process lifecycle and child cleanup require isolated lifecycle evidence.
POOLSIDE_PROCESS_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# A direct ``pool`` execution JSONL surface needs evidence separate from ACP.
POOLSIDE_EXEC_JSONL_SEPARATE_REQUIRES_STAGE_6_EVIDENCE = True

# Update behavior and removal/cleanup procedures require separate evidence.
POOLSIDE_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="poolside",
    display_name="Poolside Agent CLI",
    executable="pool",
    prompt_argv=("acp",),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.ACP,
    # Static Held metadata only: no environment value is read or applied.
    environment_keys=frozenset(
        (
            "POOLSIDE_API_KEY",
            "POOLSIDE_TOKEN",
            "POOLSIDE_API_URL",
            "POOLSIDE_STANDALONE_BASE_URL",
            "POOLSIDE_STANDALONE_MODEL",
        )
    ),
    version_marker="pool ",
    help_chat_marker="pool acp",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
