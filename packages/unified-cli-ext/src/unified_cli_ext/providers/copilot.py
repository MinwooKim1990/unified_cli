"""Inert Held metadata for the official GitHub Copilot CLI.

The documented one-shot argv limits the model to read-only workspace tools, but
Copilot also consumes executable hook/plugin/MCP configuration from provider
state and repository settings.  Those inputs cannot be made immutable with
portable pathname checks around a subprocess launch, so this entry remains Held.
"""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


COPILOT_OFFICIAL_SOURCES = (
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference",
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference",
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference",
    "https://github.com/github/copilot-cli",
    "https://www.npmjs.com/package/@github/copilot",
)
COPILOT_OFFICIAL_PACKAGE = "@github/copilot"
COPILOT_DEFAULT_MODEL = "auto"
COPILOT_READ_ONLY_TOOLS = ("view", "glob", "grep")
COPILOT_STAGE_6_TARGET_VERSION = "1.0.73"
COPILOT_STAGE_6_EVIDENCE_CAPTURED = False

COPILOT_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True
COPILOT_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True
COPILOT_DEDICATED_HOME_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True


# Candidate metadata only.  ``view``, ``glob``, and ``grep`` are read-only
# Copilot tools; this is deliberately not described as a no-tools invocation.
COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV = (
    "--silent",
    "--no-ask-user",
    "--no-auto-update",
    "--no-custom-instructions",
    "--no-remote",
    "--no-remote-export",
    "--disable-builtin-mcps",
    "--available-tools",
    ",".join(COPILOT_READ_ONLY_TOOLS),
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
    # Static candidate metadata only.  No environment value is read or applied
    # while Held.
    environment_keys=frozenset(("COPILOT_HOME",)),
    version_marker="copilot ",
    help_chat_marker="-p, --prompt",
    help_argv=("help",),
)

PLUGIN = held_plugin(ADAPTER_SPEC)


__all__ = [
    "ADAPTER_SPEC",
    "COPILOT_DEFAULT_MODEL",
    "COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV",
    "COPILOT_OFFICIAL_PACKAGE",
    "COPILOT_OFFICIAL_SOURCES",
    "COPILOT_READ_ONLY_TOOLS",
    "PLUGIN",
]
