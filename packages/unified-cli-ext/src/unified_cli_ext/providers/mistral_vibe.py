"""Held metadata for a future Mistral Vibe CLI integration."""

from __future__ import annotations

from .contract import PromptMode, TransportKind
from .held import held_adapter_spec, held_plugin


# Version 2.21.0 has been checked, but no isolated fixture establishes a safe
# minimum or exact version/help output.  Keep the held helper's inert (0,)
# minimum and these deliberately conservative marker strings until Stage 6.
MISTRAL_VIBE_VERSION_HELP_OUTPUT_REQUIRES_STAGE_6_EVIDENCE = True

# ``vibe-acp`` is a separate ACP entry point.  This direct provider record is
# intentionally bound to ``vibe``; ACP transport requires its own evidence.
MISTRAL_VIBE_ACP_REQUIRES_SEPARATE_STAGE_6_EVIDENCE = True

ADAPTER_SPEC = held_adapter_spec(
    provider_id="mistral-vibe",
    display_name="Mistral Vibe",
    executable="vibe",
    prompt_argv=(
        "--output",
        "streaming",
        "--agent",
        "plan",
        "--disabled-tools",
        "*",
    ),
    prompt_mode=PromptMode.OPTION_VALUE,
    prompt_option="--prompt",
    transport=TransportKind.JSONL,
    version_marker="vibe",
    help_chat_marker="--prompt",
)

PLUGIN = held_plugin(ADAPTER_SPEC)
