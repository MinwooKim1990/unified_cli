"""Held metadata for a future Cline CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# The candidate direct form is ``cline --json -- <prompt>``, but the held helper
# cannot declare its required sentinel policy and the released process currently
# waits for stdin EOF.  A Cline-specific runner must build that sentinel argv,
# close stdin, and prove a clean exit before this adapter can be enabled.
CLINE_ONE_SHOT_LIFECYCLE_REQUIRES_STAGE_6_EVIDENCE = True

# Released JSON events do not yet match the documented schema closely enough to
# establish a stable parser contract.  Captured fixtures are required first.
CLINE_OUTPUT_SCHEMA_REQUIRES_STAGE_6_EVIDENCE = True

# Stage 6 must prove isolated settings, tools, and MCP behavior.  In particular,
# no ambient credential is allowlisted by this held record.
CLINE_CONFIG_ISOLATION_REQUIRES_STAGE_6_EVIDENCE = True

# ACP is a separate integration surface and is not claimed by this direct JSONL
# candidate.  It requires its own captured transport and lifecycle evidence.
CLINE_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="cline",
    display_name="Cline CLI",
    executable="cline",
    prompt_argv=("--json",),
    # This protocol placeholder is inert.  It prevents the generic runner from
    # claiming a positional one-shot lifecycle that the helper cannot express.
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.JSONL,
    # This control is safe to opt in; CLINE_API_KEY and other ambient
    # credentials remain excluded.
    environment_keys=frozenset(("CLINE_NO_AUTO_UPDATE",)),
    # Exact identity output still requires an isolated fixture.  These markers
    # are provisional inert metadata and are never probed while held.
    version_marker="cline ",
    help_chat_marker="--json",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
