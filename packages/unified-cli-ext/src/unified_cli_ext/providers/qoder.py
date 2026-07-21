"""Held metadata for a future Qoder CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Stage 6 must establish version/help identity, ACP event framing, and
# permission/config isolation from captured fixtures before this adapter can
# become executable.  The marker strings below are provisional inert metadata;
# Qoder does not officially specify their exact output text.
QODER_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="qoder",
    display_name="Qoder CLI",
    executable="qodercli",
    prompt_argv=("--acp",),
    prompt_mode=PromptMode.PROTOCOL,
    prompt_option=None,
    transport=TransportKind.ACP,
    environment_keys=frozenset(("QODER_PERSONAL_ACCESS_TOKEN",)),
    version_marker="qodercli ",
    help_chat_marker="--acp",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
