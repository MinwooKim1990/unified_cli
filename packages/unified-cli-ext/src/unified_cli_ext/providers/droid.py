"""Inert Held metadata for a future Factory Droid CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official release target: npm package ``droid`` 0.176.0.
# https://docs.factory.ai/reference/cli-reference
# https://docs.factory.ai/cli/droid-exec/overview
# https://app.factory.ai/cli
# https://registry.npmjs.org/droid/latest
# https://github.com/Factory-AI/factory

# Exact version/help output and the stream JSON-RPC envelope require isolated
# captured fixtures.  The provisional markers below are inert while Held.
DROID_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
DROID_STREAM_JSONRPC_ENVELOPE_PROTOCOL_VERSION_REQUIRES_STAGE_6_EVIDENCE = True

# Session, notification, and turn schemas must be verified before a protocol
# runner can parse or emit any JSON-RPC messages.
DROID_SESSION_NOTIFICATION_TURN_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Permission defaults must be proven deny-by-default, including all auth and
# account behavior, before any execution surface can be enabled.
DROID_PERMISSION_ASK_USER_DEFAULT_DENY_REQUIRES_STAGE_6_EVIDENCE = True
DROID_AUTH_ACCOUNT_BILLING_POLICY_REQUIRES_STAGE_6_EVIDENCE = True

# Models, images, MCP, usage accounting, and error semantics remain unverified.
DROID_MODEL_IMAGE_MCP_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True

# Resume, fork, interruption, backpressure, and process cleanup each require
# captured lifecycle evidence before this provider can leave Held status.
DROID_RESUME_FORK_INTERRUPT_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE = True
DROID_PROCESS_BACKPRESSURE_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Update behavior, config isolation, and SDK/CLI drift need separate evidence.
DROID_UPDATE_REMOVAL_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
DROID_SDK_CLI_PROTOCOL_DRIFT_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="droid",
    display_name="Factory Droid",
    executable="droid",
    prompt_argv=(
        "exec",
        "--input-format",
        "stream-jsonrpc",
        "--output-format",
        "stream-jsonrpc",
    ),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSON_RPC,
    # Static Held metadata only: no environment value is read or applied.
    environment_keys=frozenset(
        ("FACTORY_API_KEY", "FACTORY_DROID_AUTO_UPDATE_ENABLED")
    ),
    version_marker="droid ",
    help_chat_marker="exec [options] [prompt]",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
