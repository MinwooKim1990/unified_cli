"""Inert Held metadata for a future Cursor Agent CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


CURSOR_OFFICIAL_SOURCES = (
    "https://cursor.com/docs/cli/installation",
    "https://cursor.com/docs/cli/reference/parameters",
    "https://cursor.com/docs/cli/reference/output-format",
    "https://cursor.com/docs/cli/acp",
)
CURSOR_STAGE_6_TARGET_VERSION = "2026.07.20-8cc9c0b"
CURSOR_PRIMARY_EXECUTABLE = "agent"
CURSOR_LEGACY_EXECUTABLE = "cursor-agent"
CURSOR_LEGACY_ALIAS_SINCE = "2026-01-08"
CURSOR_STAGE_6_EVIDENCE_CAPTURED = False

CURSOR_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_PROMPT_FORM_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True
CURSOR_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True

# Current direct mode documents boolean ``-p``/``--print`` and a positional
# prompt.  The ABI supports only option-value prompts or positional prompts
# after a documented ``--`` sentinel; neither matches.  STDIN framing is also
# unproven, so no executable direct prompt candidate is encoded below.
CURSOR_PROMPT_COMMAND_IS_ABI_REPRESENTABLE = False
CURSOR_DOCUMENTED_PRINT_OPTIONS = ("--print", "--output-format", "json")
CURSOR_DOCUMENTED_AUTH_ARGV = (("login",), ("status",), ("logout",))

# PromptCommandSpec currently requires a non-empty structural placeholder even
# for a Held plugin.  ``--help`` is deliberately not the documented chat form,
# and the factory refuses before this object can be built or executed.
CURSOR_INERT_PROMPT_PLACEHOLDER = ("--help",)

ADAPTER_SPEC = held_adapter_spec(
    provider_id="cursor",
    display_name="Cursor Agent CLI",
    executable=CURSOR_PRIMARY_EXECUTABLE,
    prompt_argv=CURSOR_INERT_PROMPT_PLACEHOLDER,
    prompt_mode=PromptMode.STDIN,
    prompt_option=None,
    transport=TransportKind.JSON,
    # CURSOR_API_KEY is an env-only candidate; it is never placed on argv and
    # no ambient value is read while Held.
    environment_keys=frozenset(("CURSOR_API_KEY",)),
    version_marker="agent ",
    help_chat_marker="--print",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
