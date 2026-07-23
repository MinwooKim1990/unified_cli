"""Inert Held metadata for a future Mistral Vibe CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


MISTRAL_VIBE_OFFICIAL_SOURCES = (
    "https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli",
    "https://github.com/mistralai/mistral-vibe",
)
MISTRAL_VIBE_DEFAULT_MODEL = "default"
MISTRAL_VIBE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True
MISTRAL_VIBE_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True
MISTRAL_VIBE_HEADLESS_FIXED_ARGV = (
    "--output",
    "streaming",
    "--agent",
    "plan",
    "--disabled-tools",
    "*",
)


ADAPTER_SPEC = held_adapter_spec(
    provider_id="mistral-vibe",
    display_name="Mistral Vibe",
    executable="vibe",
    prompt_argv=MISTRAL_VIBE_HEADLESS_FIXED_ARGV,
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="--prompt",
    transport=TransportKind.JSONL,
    # Static candidate metadata only.  MISTRAL_API_KEY is not read or applied
    # while workspace hooks, MCP, prompts, and update behavior remain unisolated.
    environment_keys=frozenset(),
    version_marker="vibe ",
    help_chat_marker="--prompt",
)

PLUGIN = held_plugin(ADAPTER_SPEC)


__all__ = [
    "ADAPTER_SPEC",
    "MISTRAL_VIBE_DEFAULT_MODEL",
    "MISTRAL_VIBE_HEADLESS_FIXED_ARGV",
    "MISTRAL_VIBE_OFFICIAL_SOURCES",
    "PLUGIN",
]
