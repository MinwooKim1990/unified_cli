"""Held metadata for a future Grok CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


ADAPTER_SPEC = held_adapter_spec(
    provider_id="grok",
    display_name="Grok CLI",
    executable="grok",
    prompt_argv=(
        "--no-auto-update",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "streaming-json",
    ),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.JSONL,
    version_marker="grok ",
    help_chat_marker="-p",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
