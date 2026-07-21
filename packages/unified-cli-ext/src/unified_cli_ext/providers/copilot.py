"""Held metadata for a future Copilot CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


ADAPTER_SPEC = held_adapter_spec(
    provider_id="copilot",
    display_name="Copilot CLI",
    executable="copilot",
    prompt_argv=(
        "--silent",
        "--no-ask-user",
        "--no-auto-update",
        "--disable-builtin-mcps",
        "--available-tools",
        "view,glob,grep",
    ),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.PLAIN,
    version_marker="copilot ",
    help_chat_marker="-p",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
