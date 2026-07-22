"""Inert Held metadata for a future xAI Grok Build integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official research inputs, frozen for the 0.1 catalog.  They are metadata,
# not captured installation or provider evidence.
GROK_OFFICIAL_SOURCES = (
    "https://github.com/xai-org/grok-build",
    "https://docs.x.ai/build/overview",
    "https://docs.x.ai/build/cli/reference",
    "https://docs.x.ai/build/cli/headless-scripting",
)
GROK_OFFICIAL_PACKAGE = "@xai-official/grok"
GROK_STAGE_6_TARGET_VERSION = "0.2.106"
GROK_REJECTED_PACKAGE_IDENTITIES = ("@vibe-kit/grok-cli",)
GROK_STAGE_6_EVIDENCE_CAPTURED = False
GROK_BINARY_IDENTITY_IS_VERIFIED = False

# The generic ``grok`` executable name is not identity evidence.  In
# particular, a host installation supplied by the rejected third-party
# package above must never satisfy this gate.
GROK_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True
GROK_XAI_BINARY_NAME_COLLISION_REQUIRES_STAGE_6_EVIDENCE = True

# Streaming JSON framing and normalization need real isolated fixtures.
GROK_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE = True

# The compound strict-sandbox/permission-filter candidate remains unable to
# prove zero writes or that no MCP server starts during initialization.
# ~/.grok and temporary paths remain writable, broad reads remain possible,
# and denying MCPTool is not an MCP-startup gate.
GROK_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Authentication, sessions, and model behavior remain docs-only research.
GROK_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE = True

# Interrupt behavior and complete child/process cleanup require lifecycle
# capture before any runner can exist.
GROK_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Auto-update suppression, package removal, quota, usage, and error behavior
# require independent evidence.
GROK_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True
GROK_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True

# ``grok agent stdio`` is a separate ACP candidate.  The 0.1 catalog neither
# implements nor enables that bridge.
GROK_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True
GROK_DOCUMENTED_ACP_ARGV = ("agent", "stdio")
GROK_DOCUMENTED_AUTH_ARGV = (
    ("login",),
    ("login", "--device-auth"),
    ("logout",),
)

# ``-p PROMPT`` is appended by PromptCommandSpec.  This tuple is a documented
# command candidate only; the Held factory never builds or launches it.
GROK_DOCUMENTED_HEADLESS_FIXED_ARGV = (
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
)

ADAPTER_SPEC = held_adapter_spec(
    provider_id="grok",
    display_name="xAI Grok Build",
    executable="grok",
    prompt_argv=GROK_DOCUMENTED_HEADLESS_FIXED_ARGV,
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.JSONL,
    # Static opt-in metadata only.  No ambient value is read while Held.
    environment_keys=frozenset(("GROK_DISABLE_AUTOUPDATER",)),
    version_marker="grok ",
    help_chat_marker="-p",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
