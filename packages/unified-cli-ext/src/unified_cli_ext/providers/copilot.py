"""Inert Held metadata for a future GitHub Copilot CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


COPILOT_OFFICIAL_SOURCES = (
    "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli",
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference",
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server",
    "https://www.npmjs.com/package/@github/copilot",
)
COPILOT_OFFICIAL_PACKAGE = "@github/copilot"
COPILOT_STAGE_6_TARGET_VERSION = "1.0.73"
COPILOT_STAGE_6_EVIDENCE_CAPTURED = False

COPILOT_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True

# Official references do not establish a JSONL schema or complete user- and
# workspace-MCP isolation.  ACP remains an independently held surface.
COPILOT_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True
COPILOT_DEDICATED_HOME_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# ``-p PROMPT`` is appended by PromptCommandSpec.  Token precedence and auth
# belong in documentation/evidence; token values are never represented here.
COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV = (
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
)

ADAPTER_SPEC = held_adapter_spec(
    provider_id="copilot",
    display_name="GitHub Copilot CLI",
    executable="copilot",
    prompt_argv=COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV,
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.PLAIN,
    # A future lab must provide a dedicated value; Held import never reads it.
    environment_keys=frozenset(("COPILOT_HOME",)),
    version_marker="copilot ",
    help_chat_marker="-p",
    help_argv=("help",),
)

PLUGIN = held_plugin(ADAPTER_SPEC)
