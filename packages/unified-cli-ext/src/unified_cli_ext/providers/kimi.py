"""Held metadata for a future Kimi CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


ADAPTER_SPEC = held_adapter_spec(
    provider_id="kimi",
    display_name="Kimi CLI",
    executable="kimi",
    prompt_argv=("--output-format", "stream-json"),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="-p",
    transport=TransportKind.JSONL,
    # This is the sole opt-in environment value.  Ambient credentials are not
    # read or forwarded by the held metadata or factory.
    environment_keys=frozenset(("KIMI_CODE_NO_AUTO_UPDATE",)),
    version_marker="kimi ",
    help_chat_marker="-p",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
