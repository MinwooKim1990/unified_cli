"""Inert Held metadata for a future OpenCode CLI integration."""

from __future__ import annotations

from .contract import PromptMode, PromptSentinelPolicy, TransportKind
from .held import held_adapter_spec, held_plugin


# The direct one-shot candidate must prove that stdin is closed at the required
# point and that the process exits cleanly.  Until then, this is only inert
# metadata; no runner applies its declared controls.
OPENCODE_ONE_SHOT_STDIN_EOF_REQUIRES_STAGE_6_EVIDENCE = True

# Exact version/help output requires isolated captured fixtures.  The markers
# below are provisional and are not probed while this adapter remains Held.
OPENCODE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

# Stage 6 must establish the JSONL event schema before a parser can rely on it.
OPENCODE_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Permission, configuration, and MCP isolation remain unverified.  The
# allowlisted controls below are inert and will not be applied until verification.
OPENCODE_PERMISSION_CONFIG_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Process/session cleanup requires isolated lifecycle evidence before enabling
# any execution path.
OPENCODE_PROCESS_SESSION_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# HTTP/SSE is a separate transport surface from this direct JSONL candidate.
OPENCODE_HTTP_SSE_SEPARATE_REQUIRES_STAGE_6_EVIDENCE = True

# ACP likewise requires independent transport and lifecycle verification.
OPENCODE_ACP_SEPARATE_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="opencode",
    display_name="OpenCode",
    executable="opencode",
    prompt_argv=("--pure", "run", "--format", "json"),
    prompt_mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
    prompt_option=None,
    sentinel_policy=PromptSentinelPolicy.REQUIRED,
    transport=TransportKind.JSONL,
    # These declared controls are static Held metadata only.  They are not read
    # or applied until Stage 6 verification makes an executable adapter safe.
    environment_keys=frozenset(
        (
            "OPENCODE_DISABLE_AUTOUPDATE",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS",
            "OPENCODE_DISABLE_LSP_DOWNLOAD",
            "OPENCODE_DISABLE_MODELS_FETCH",
            "OPENCODE_DISABLE_CLAUDE_CODE",
        )
    ),
    version_marker="opencode ",
    help_chat_marker="run [message..]",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
