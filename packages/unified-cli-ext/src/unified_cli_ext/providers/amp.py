"""Inert Held metadata for a future Amp CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official reference material (including the manual appendix):
# https://ampcode.com/manual
# https://ampcode.com/manual/appendix
# Package-transition reference: https://www.npmjs.com/package/@ampcode/cli
# These sources document the move to ``@ampcode/cli``; no unstable exact
# current package or CLI version is asserted here.

# Exact ``--version``/``--help`` output and binary provenance require captured
# Stage 6 fixtures before the provisional markers below may be evaluated.
AMP_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
AMP_INSTALL_CHANNEL_BINARY_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True

# The stream-json input/output schema, including text, tool-usage, errors, and
# image normalization, must be captured before a protocol runner can exist.
AMP_STREAM_JSON_INPUT_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True
AMP_TEXT_TOOL_USAGE_ERROR_IMAGE_NORMALIZATION_REQUIRES_STAGE_6_EVIDENCE = True

# Login, logout, status, and billing behavior must be isolated and verified.
AMP_AUTH_LOGIN_LOGOUT_STATUS_BILLING_REQUIRES_STAGE_6_EVIDENCE = True

# Session continuation/resume semantics and persistence remain unverified.
AMP_SESSION_CONTINUE_RESUME_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE = True

# Held rationale: approvals are off by default, but workspace settings may
# override the settings file; project/system plugins and MCP may load, and no
# permission-request channel is available to the host.
AMP_PERMISSION_TOOL_PLUGIN_MCP_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Cancellation, steering, stdin EOF, and process/child cleanup need lifecycle
# fixtures before this integration can execute.
AMP_CANCEL_STEER_STDIN_EOF_PROCESS_CHILD_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Update/removal behavior, settings/environment isolation, and SDK/CLI schema
# drift require independent evidence before enablement.
AMP_UPDATE_REMOVAL_SETTINGS_ENV_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
AMP_SDK_CLI_SCHEMA_DRIFT_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="amp",
    display_name="Amp CLI",
    executable="amp",
    prompt_argv=("--execute", "--stream-json", "--stream-json-input"),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSONL,
    # Static Held metadata only: no environment value is read or applied.
    environment_keys=frozenset(("AMP_API_KEY", "AMP_SKIP_UPDATE_CHECK")),
    # Provisional until Stage 6 captures isolated command output fixtures.
    version_marker="amp ",
    help_chat_marker="--stream-json-input",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
