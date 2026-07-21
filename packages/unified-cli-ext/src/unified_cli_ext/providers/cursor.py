"""Held metadata for a future Cursor CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Stage 6 must capture isolated cursor-agent output before this provisional
# JSON-print form can become an execution contract.  In particular, do not add
# a positional sentinel or claim a formally established prompt framing here.
CURSOR_PROMPT_FORM_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="cursor",
    display_name="Cursor CLI",
    executable="cursor-agent",
    prompt_argv=("--print", "--output-format", "json"),
    prompt_mode=PromptMode.STDIN,
    prompt_option=None,
    transport=TransportKind.JSON,
    version_marker="cursor-agent ",
    help_chat_marker="--print",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
