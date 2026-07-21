"""Inert Held metadata for a future Oh My Pi CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official release target: npm package ``@oh-my-pi/pi-coding-agent`` 17.0.6.
# Evidence sources:
# https://github.com/can1357/oh-my-pi
# https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/package.json
# https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md
# https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md

# Exact version/help output plus custom NDJSON readiness, framing, completion,
# error, and usage semantics require isolated fixtures before parsing exists.
OH_MY_PI_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
OH_MY_PI_RPC_READY_FRAMING_COMPLETION_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Configuration, environment, authentication, and model selection must be
# isolated before a process is ever allowed to inherit provider state.
OH_MY_PI_CONFIG_ENV_AUTH_MODEL_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Tool, extension, rule, MCP, subagent, and permission isolation require
# dedicated evidence; the fixed arguments below remain inert Held metadata.
OH_MY_PI_TOOL_EXTENSION_RULE_MCP_SUBAGENT_PERMISSION_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Cancellation, stdin EOF, worker cleanup, session/resume, and image handling
# need lifecycle and protocol fixtures before this provider can be enabled.
OH_MY_PI_RPC_CANCEL_STDIN_EOF_WORKER_MCP_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
OH_MY_PI_SESSION_RESUME_IMAGE_REQUIRES_STAGE_6_EVIDENCE = True

# ACP is a separate transport surface from the custom NDJSON RPC candidate.
OH_MY_PI_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True

# Package acquisition, post-install behavior, and updates must remain contained
# and evidenced independently of the held runtime integration.
OH_MY_PI_UPDATE_CHECK_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="oh-my-pi",
    display_name="Oh My Pi",
    executable="omp",
    prompt_argv=(
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
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSONL,
    environment_keys=frozenset(),
    # Required by the generic Held metadata factory; never probed while Held.
    version_marker="omp ",
    help_chat_marker="--mode",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
