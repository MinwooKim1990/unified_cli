"""Factory + provider routing helpers."""

from __future__ import annotations

import re
from typing import Optional

from .base import BaseProvider
from .core import ProviderName
from .errors import UnifiedError
from .i18n import t
from .models import DEFAULT_MODELS
from .providers import ClaudeProvider, CodexProvider, GeminiProvider


PROVIDERS: dict[ProviderName, type[BaseProvider]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
}


def create(
    provider: ProviderName,
    *,
    model: Optional[str] = None,
    **opts,
) -> BaseProvider:
    """Instantiate a provider with its default model unless overridden."""
    if provider not in PROVIDERS:
        raise UnifiedError(
            kind="config", provider=provider,  # type: ignore[arg-type]
            message=t("err.factory.unknown_provider", provider=provider),
            hint=t("err.factory.unknown_provider.hint"),
        )
    return PROVIDERS[provider](model=model or DEFAULT_MODELS[provider], **opts)


# ---- routing: model string → (provider, model) ----

_CLAUDE_PAT = re.compile(r"^(claude[-/]|haiku$|sonnet$|opus$)", re.I)
_CODEX_PAT = re.compile(r"^(gpt-|o1-|o3-|codex-)", re.I)
_GEMINI_PAT = re.compile(r"^gemini[-/]", re.I)


def route(model_str: str) -> tuple[ProviderName, str]:
    """Map an OpenAI-style model string to (provider, model_id).

    Supports:
      - Explicit prefix: "claude/haiku", "codex/gpt-5.4-mini", "gemini/gemini-..."
      - Auto-inference: "haiku" → claude, "gpt-5.4-mini" → codex, "gemini-..." → gemini
    """
    if "/" in model_str:
        head, tail = model_str.split("/", 1)
        if head in PROVIDERS:
            return head, tail  # type: ignore[return-value]

    if _CLAUDE_PAT.match(model_str):
        return "claude", model_str
    if _CODEX_PAT.match(model_str):
        return "codex", model_str
    if _GEMINI_PAT.match(model_str):
        return "gemini", model_str

    raise UnifiedError(
        kind="config", provider="claude",  # unknown; annotated as config error
        message=t("err.factory.cannot_route", model=model_str),
        hint=t("err.factory.cannot_route.hint"),
    )
