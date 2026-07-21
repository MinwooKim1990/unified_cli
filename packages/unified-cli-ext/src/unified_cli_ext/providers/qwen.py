"""Held metadata for a future Qwen Code integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Stage 6 must establish version/help identity, the stream-json event schema,
# and permission/config isolation from captured fixtures.  In particular, no
# backend credential is allowed through until routing selects one Qwen backend.
QWEN_REQUIRES_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="qwen",
    display_name="Qwen Code",
    executable="qwen",
    prompt_argv=("--output-format", "stream-json"),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="--prompt",
    transport=TransportKind.JSONL,
    # No ambient backend credential may cross this boundary until routing has
    # selected one of Qwen Code's supported backends.
    environment_keys=frozenset(),
    # These provisional markers are inert while the integration is held and
    # must be replaced or verified by Stage 6 fixtures.
    version_marker="qwen ",
    help_chat_marker="--prompt",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
