"""Held metadata for a future CodeBuddy Code CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# The prompt belongs in a provider-specific JSONL input frame.  The generic
# runtime cannot construct that exact frame, so Stage 6 must capture and verify
# it before this metadata can become executable.
CODEBUDDY_PROTOCOL_FRAME_REQUIRES_STAGE_6_EVIDENCE = True

# The documented no-tools form ends in an empty argument, which the declarative
# contract deliberately rejects.  Stage 6 must establish a valid no-tools form
# and verify MCP/config isolation before adding any equivalent fixed arguments.
CODEBUDDY_NO_TOOLS_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Exact version/help output has not been captured in an isolated fixture.  The
# marker strings below are intentionally conservative and inert while held.
CODEBUDDY_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="codebuddy",
    display_name="CodeBuddy Code",
    executable="codebuddy",
    prompt_argv=(
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--include-partial-messages",
        "--strict-mcp-config",
    ),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSONL,
    # This control is safe to opt in; ambient credentials remain excluded.
    environment_keys=frozenset(("DISABLE_AUTOUPDATER",)),
    version_marker="codebuddy",
    help_chat_marker="--output-format",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
