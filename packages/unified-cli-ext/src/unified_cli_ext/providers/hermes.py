"""Inert Held metadata for a future Hermes Agent ACP integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official release target: PyPI package ``hermes-agent[acp]`` 0.19.0.
# Evidence sources:
# https://pypi.org/project/hermes-agent/
# https://github.com/NousResearch/hermes-agent
# https://github.com/NousResearch/hermes-agent/blob/main/pyproject.toml
# https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md

# Hermes pins ACP 0.9.0 while Ext's optional ACP range is 0.11.x.  No protocol
# compatibility can be assumed; this remains a hard enablement blocker.
HERMES_ACP_0_9_0_VS_EXT_0_11_X_COMPATIBILITY_REQUIRES_STAGE_6_EVIDENCE = True

# Exact version/help/acp output, ACP negotiation, event framing, errors, and
# usage semantics require isolated fixtures before parsing or execution exists.
HERMES_VERSION_HELP_ACP_CHECK_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
HERMES_ACP_NEGOTIATION_EVENT_ERROR_USAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Authentication, model selection, configuration, home, and profile state must
# be isolated before a Hermes process may be started.
HERMES_AUTH_MODEL_CONFIG_HOME_PROFILE_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Permission allowlists plus tool, MCP, and plugin isolation require dedicated
# evidence; this Held module does not apply any of those controls.
HERMES_ACP_PERMISSION_ALLOWLIST_TOOL_MCP_PLUGIN_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Cancellation, stdin EOF, sessions, and worker cleanup need lifecycle fixtures
# before enablement, including proof that persistent state is not retained.
HERMES_ACP_CANCEL_STDIO_EOF_SESSION_WORKER_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Persistence and resume documentation may drift from released behavior and
# require version-pinned evidence before either behavior can be supported.
HERMES_ACP_SESSION_PERSISTENCE_RESUME_DOC_DRIFT_REQUIRES_STAGE_6_EVIDENCE = True

# Non-text and image inputs require independent ACP evidence.
HERMES_ACP_NON_TEXT_IMAGE_LIMIT_REQUIRES_STAGE_6_EVIDENCE = True

# The alternate TUI JSON-RPC and HTTP/SSE surfaces are separate integrations.
HERMES_TUI_JSONRPC_AND_HTTP_SSE_REQUIRE_SEPARATE_STAGE_6_EVIDENCE = True

# Installation, update, and post-install provenance must be captured before an
# executable integration can make acquisition or upgrade claims.
HERMES_INSTALL_CHANNEL_UPDATE_POSTINSTALL_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="hermes",
    display_name="Hermes Agent",
    executable="hermes",
    prompt_argv=("acp",),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.ACP,
    environment_keys=frozenset(),
    # Required by the generic Held metadata factory; never probed while Held.
    version_marker="hermes ",
    help_chat_marker="acp",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
