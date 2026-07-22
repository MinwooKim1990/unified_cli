"""Inert Held metadata for a future Kimi Code CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


KIMI_OFFICIAL_SOURCES = (
    "https://moonshotai.github.io/kimi-code/en/guides/getting-started.html",
    "https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html",
    "https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html",
    "https://www.npmjs.com/package/@moonshot-ai/kimi-code",
)
KIMI_OFFICIAL_PACKAGE = "@moonshot-ai/kimi-code"
KIMI_STAGE_6_TARGET_VERSION = "0.28.1"
KIMI_NPM_MINIMUM_NODE_VERSION = "22.19"
KIMI_STAGE_6_EVIDENCE_CAPTURED = False

KIMI_VERSION_HELP_IDENTITY_PROVENANCE_REQUIRES_STAGE_6_EVIDENCE = True
KIMI_PROMPT_OUTPUT_FRAMING_REQUIRES_STAGE_6_EVIDENCE = True

# Official documentation says noninteractive ``-p`` always applies the auto
# permission policy.  It cannot be combined with yolo/auto/plan and exposes no
# per-run no-tools, read-only, web-off, or MCP-off contract.
KIMI_PERMISSION_TOOL_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True
KIMI_NONINTERACTIVE_AUTO_APPROVAL_REQUIRES_STAGE_6_EVIDENCE = True

KIMI_AUTH_SESSION_MODEL_REQUIRES_STAGE_6_EVIDENCE = True
KIMI_CANCELLATION_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True
KIMI_UPDATE_REMOVAL_REQUIRES_STAGE_6_EVIDENCE = True
KIMI_QUOTA_USAGE_ERROR_REQUIRES_STAGE_6_EVIDENCE = True

# ``kimi acp`` documents auth/session/prompt/cancel plus client-routed file
# operations and local shell/permission requests.  It is a separate candidate,
# not an enabled ACP bridge.
KIMI_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True
KIMI_DOCUMENTED_ACP_ARGV = ("acp",)
KIMI_DOCUMENTED_AUTH_AND_DIAGNOSTIC_ARGV = (
    ("login",),
    ("doctor",),
    ("provider", "list", "--json"),
)
KIMI_TUI_LOGOUT_COMMAND = "/logout"
KIMI_PROMPT_USES_OS_WORKING_DIRECTORY = True

KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV = ("--output-format", "stream-json")

ADAPTER_SPEC = held_adapter_spec(
    provider_id="kimi",
    display_name="Kimi Code CLI",
    executable="kimi",
    prompt_argv=KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV,
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.JSONL,
    # Static opt-in controls only.  Ambient credentials are neither claimed
    # nor read/forwarded by the Held metadata or factory.
    environment_keys=frozenset(
        ("KIMI_CODE_NO_AUTO_UPDATE", "KIMI_DISABLE_TELEMETRY")
    ),
    version_marker="kimi ",
    help_chat_marker="-p",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
