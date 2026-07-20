"""Factory + provider routing helpers."""

from __future__ import annotations

import re
from typing import Optional

from .base import BaseProvider
from .core import ProviderId, ProviderName
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
    provider: ProviderId,
    *,
    model: Optional[str] = None,
    **opts,
) -> BaseProvider:
    """Instantiate a provider with its default model unless overridden."""
    if provider in PROVIDERS:
        builtin = provider  # retained as a narrow key for static type checkers
        return PROVIDERS[builtin](  # type: ignore[index]
            model=model or DEFAULT_MODELS[builtin],  # type: ignore[index]
            **opts,
        )

    # Entry-point metadata is inspected only for an explicitly requested
    # unknown id.  Built-in creation above remains a zero-discovery fast path.
    from .registry import instantiate_extension_provider
    return instantiate_extension_provider(provider, model=model, **opts)


# ---- routing: model string → (provider, model) ----

_CLAUDE_PAT = re.compile(r"^(claude[-/]|haiku$|sonnet$|opus$)", re.I)
_CODEX_PAT = re.compile(r"^(gpt-|o1-|o3-|codex-)", re.I)
_GEMINI_PAT = re.compile(r"^gemini[-/]", re.I)


def _route_builtin(model_str: str) -> Optional[tuple[ProviderName, str]]:
    """Return a Core route without consulting extension metadata."""
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
    return None


def route(model_str: str) -> tuple[ProviderId, str]:
    """Map an OpenAI-style model string to (provider, model_id).

    Supports:
      - Explicit prefix: "claude/haiku", "codex/gpt-5.4-mini", "gemini/gemini-..."
      - Installed extension id: "my-provider/model" (exact id only)
      - Auto-inference: "haiku" → claude, "gpt-5.4-mini" → codex, "gemini-..." → gemini

    Extension route aliases and model-name inference are intentionally not
    supported by ABI v1, keeping discovery exact and lazy.
    """
    # Preserve Core's historical inference before consulting extension
    # metadata. In particular, slash-containing model ids such as
    # ``gpt-custom/path`` have always routed to Codex and must remain a
    # zero-discovery fast path even when plugins are disabled or broken.
    builtin = _route_builtin(model_str)
    if builtin is not None:
        return builtin

    if "/" in model_str:
        head, tail = model_str.split("/", 1)
        # Extension providers never participate in model-name inference. An
        # explicit ``id/model`` prefix performs a metadata-only exact lookup;
        # the plugin module is loaded later by create(id).
        from .registry import extension_provider_exists
        if extension_provider_exists(head):
            return head, tail

    raise _cannot_route_error(model_str)


def _cannot_route_error(model_str: str) -> UnifiedError:
    """Build the historical routing error without consulting plugins."""
    return UnifiedError(
        kind="config", provider="claude",  # unknown; annotated as config error
        message=t("err.factory.cannot_route", model=model_str),
        hint=t("err.factory.cannot_route.hint"),
    )
