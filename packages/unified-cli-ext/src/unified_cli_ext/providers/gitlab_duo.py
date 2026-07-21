"""Inert Held metadata for a future GitLab Duo CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Official GA target: GitLab Duo CLI 9.6.0 (researched 2026-07-21).
# https://docs.gitlab.com/user/gitlab_duo_cli/
# https://gitlab.com/gitlab-org/editor-extensions/gitlab-lsp/-/tree/main/packages/cli
# https://www.npmjs.com/package/@gitlab/duo-cli
# ``duo --version`` is documented/provisionally expected to print bare semver;
# the marker remains inert until Stage 6 captures isolated command fixtures.

# Exact version/help output must be captured before provisional probes can run.
GITLAB_DUO_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

# Package, binary identity, provenance hash, and signature need separate proof.
GITLAB_DUO_BINARY_GENERIC_PACKAGE_NPM_PROVENANCE_HASH_SIGNATURE_REQUIRES_STAGE_6_EVIDENCE = True

# Bare semver needs an independent identity probe before binary selection.
GITLAB_DUO_BARE_SEMVER_SEPARATE_IDENTITY_PROBE_REQUIRES_STAGE_6_EVIDENCE = True

# Headless run must prove one goal option, JSON stdout, stderr, and exit code.
GITLAB_DUO_RUN_GOAL_OPTION_SINGLE_JSON_STDOUT_STDERR_EXIT_CODE_REQUIRES_STAGE_6_EVIDENCE = True

# The documented JSON schema 1.0 needs captured normalization fixtures.
GITLAB_DUO_RUN_JSON_SCHEMA_1_0_NORMALIZATION_REQUIRES_STAGE_6_EVIDENCE = True

# Empty JSON output serialization failure semantics remain unverified.
GITLAB_DUO_JSON_EMPTY_OUTPUT_SERIALIZATION_FAILURE_REQUIRES_STAGE_6_EVIDENCE = True

# Headless run auto-approves tools; sandbox boundaries require evidence and stay Held.
GITLAB_DUO_HEADLESS_AUTO_APPROVAL_SANDBOX_BOUNDARY_REQUIRES_STAGE_6_EVIDENCE = True

# Tool, MCP, hook, skill, and project-config isolation requires verification.
GITLAB_DUO_TOOL_MCP_HOOK_SKILL_PROJECT_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Auth, glab helper, config-home, environment, and secret isolation need proof.
GITLAB_DUO_AUTH_GLAB_HELPER_CONFIG_HOME_ENV_SECRET_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# New/resume session, plans, approvals, and context lifecycle remain unverified.
GITLAB_DUO_SESSION_NEW_RESUME_PLAN_APPROVAL_CONTEXT_REQUIRES_STAGE_6_EVIDENCE = True

# Model, context, and instruction persistence needs separate lifecycle evidence.
GITLAB_DUO_MODEL_CONTEXT_INSTRUCTION_PERSISTENCE_REQUIRES_STAGE_6_EVIDENCE = True

# Cancellation, signals, WebSocket retries, and child/MCP cleanup need proof.
GITLAB_DUO_CANCEL_SIGNAL_WEBSOCKET_RETRY_PROCESS_CHILD_MCP_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Usage, errors, reasoning, images, and schema behavior remain unverified.
GITLAB_DUO_USAGE_ERROR_REASONING_IMAGE_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Telemetry, logs, updates, and configuration containment need verification.
GITLAB_DUO_TELEMETRY_LOG_UPDATE_CONFIG_CONTAINMENT_REQUIRES_STAGE_6_EVIDENCE = True

# CI credits, subscriptions, namespaces, and quotas need isolated evidence.
GITLAB_DUO_CI_CREDITS_SUBSCRIPTION_NAMESPACE_QUOTA_REQUIRES_STAGE_6_EVIDENCE = True

# Compiled binary and npm channels, update, and removal behavior need proof.
GITLAB_DUO_COMPILED_BINARY_NPM_CHANNEL_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True

# Windows behavior requires transport fixtures separate from Unix evidence.
GITLAB_DUO_WINDOWS_REQUIRES_SEPARATE_TRANSPORT_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="gitlab-duo",
    display_name="GitLab Duo CLI",
    executable="duo",
    prompt_argv=("run", "--output-format", "json"),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="--goal",
    transport=TransportKind.JSON,
    # Static Held metadata only: no environment value is read or applied.
    environment_keys=frozenset(
        (
            "GITLAB_TOKEN",
            "GITLAB_OAUTH_TOKEN",
            "GITLAB_BASE_URL",
            "GITLAB_URL",
            "GITLAB_DUO_MODEL",
        )
    ),
    version_marker="duo ",
    help_chat_marker="Run a workflow in non-interactive / headless mode.",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
