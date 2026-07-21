"""Inert Held metadata for a future Kilo Code CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Exact version/help output needs isolated fixtures before the provisional
# markers below can be trusted.  Held metadata never probes them.
KILO_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

# ACP startup, negotiation, and shutdown need captured lifecycle evidence.
KILO_ACP_LIFECYCLE_REQUIRES_STAGE_6_EVIDENCE = True

# The loopback listener and its child process require cleanup verification.
KILO_LOOPBACK_PROCESS_CLEANUP_REQUIRES_STAGE_6_EVIDENCE = True

# Permission, configuration, and MCP isolation must be proven before enabling
# this provider; the declared control remains inert while Held.
KILO_PERMISSION_CONFIG_MCP_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# Authentication, session/model behavior, and ACP event schemas are unverified.
KILO_AUTH_SESSION_MODEL_EVENT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="kilo",
    display_name="Kilo Code",
    executable="kilo",
    prompt_argv=("--pure", "acp", "--hostname", "127.0.0.1", "--port", "0", "--no-mdns"),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.ACP,
    # This is inert static metadata and is not read or applied until Stage 6
    # confirms an isolated, safe execution contract.
    environment_keys=frozenset(("KILO_DISABLE_AUTOUPDATE",)),
    version_marker="kilo ",
    help_chat_marker="kilo acp",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
