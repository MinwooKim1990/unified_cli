"""Inert Held metadata for a future Pi Coding Agent CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official release target: npm package ``@earendil-works/pi-coding-agent``
# 0.80.10.  Evidence sources:
# https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json
# https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md
# https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md

# Exact version/help output and custom NDJSON RPC framing, events, errors, and
# usage semantics must be captured in isolated fixtures before parsing exists.
PI_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
PI_RPC_FRAMING_EVENT_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Authentication, model selection, and configuration isolation are unverified.
PI_AUTH_MODEL_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# The fixed no-tools form is inert metadata; tool, resource, and permission
# isolation require dedicated evidence before this provider can run.
PI_TOOL_RESOURCE_PERMISSION_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# The candidate ``--offline`` control must be verified against startup update,
# package, and telemetry activity before it can become an executable policy.
PI_OFFLINE_UPDATE_PACKAGE_TELEMETRY_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE = True

# Cancellation, stdin EOF, process cleanup, session/resume, and image behavior
# each require lifecycle and protocol fixtures before enablement.
PI_RPC_CANCEL_STDIN_EOF_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
PI_SESSION_RESUME_IMAGE_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="pi",
    display_name="Pi Coding Agent",
    executable="pi",
    prompt_argv=(
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
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSONL,
    environment_keys=frozenset(),
    # Required by the generic Held metadata factory; never probed while Held.
    version_marker="pi ",
    help_chat_marker="--mode",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
