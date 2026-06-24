"""UnifiedConversation — multi-provider history management.

Two modes:
  sticky=True : first `send()` locks the provider; switching raises UnifiedError.
  sticky=False (default): free provider switching. When switching, the last
    `context_window` turns (user+assistant) are summarized and injected as a
    prompt prefix so the new provider has context.

Inside a single provider, resumption uses that provider's native session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional

from .base import BaseProvider
from .core import Message, ProviderName, Response
from .errors import UnifiedError
from .factory import create
from .i18n import t


@dataclass
class Turn:
    provider: ProviderName
    prompt: str
    text: str
    timestamp: float = field(default_factory=time.time)


class UnifiedConversation:
    """Multi-turn conversation across one or more providers.

    Usage:
        conv = UnifiedConversation()
        conv.send("hi")                              # uses default provider
        conv.send("more", provider="codex")          # switch with context
    """

    def __init__(
        self,
        *,
        default_provider: ProviderName = "claude",
        default_model: Optional[str] = None,
        sticky: bool = False,
        context_window: int = 8,
        provider_opts: Optional[dict] = None,
    ):
        self.default_provider = default_provider
        self.default_model = default_model
        self.sticky = sticky
        self.context_window = context_window
        self.provider_opts = provider_opts or {}

        self.sessions: dict[ProviderName, str] = {}
        self.turns: list[Turn] = []
        self._clients: dict[tuple[ProviderName, Optional[str]], BaseProvider] = {}
        self._locked_provider: Optional[ProviderName] = None

    # ----- helpers -----

    def _get_client(self, provider: ProviderName, model: Optional[str]) -> BaseProvider:
        key = (provider, model)
        if key not in self._clients:
            opts = {**self.provider_opts}
            if provider != "claude":
                # `terse` is a Claude-only constructor option; passing it to
                # codex/gemini would raise TypeError (unexpected kwarg).
                opts.pop("terse", None)
            self._clients[key] = create(provider, model=model, **opts)
        return self._clients[key]

    def _resolve(
        self,
        provider: Optional[ProviderName],
        model: Optional[str],
    ) -> tuple[ProviderName, Optional[str]]:
        prov = provider or self._locked_provider or self.default_provider
        if self.sticky:
            if self._locked_provider is None:
                self._locked_provider = prov
            elif self._locked_provider != prov:
                raise UnifiedError(
                    kind="config", provider=prov,
                    message=t("conv.sticky_switch", locked=self._locked_provider),
                    hint=t("conv.sticky_switch.hint"),
                )
        mdl = model or self.default_model
        return prov, mdl

    def _context_prefix_if_switch(self, provider: ProviderName) -> str:
        """Build a short '<prior-context>' prefix when switching providers."""
        if not self.turns:
            return ""
        last_provider = self.turns[-1].provider
        if last_provider == provider:
            return ""
        recent = self.turns[-self.context_window:]
        lines = [t("conv.ctx.header")]
        user_label = t("conv.ctx.user")
        asst_label = t("conv.ctx.assistant")
        for turn in recent:
            lines.append(f"{user_label}: {turn.prompt}")
            reply = (turn.text or "").strip().replace("\n", " ")
            if len(reply) > 400:
                reply = reply[:400] + "…"
            lines.append(f"{asst_label}: {reply}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _use_native_session(self, provider: ProviderName) -> Optional[str]:
        """Return the stored session_id iff the previous turn was this provider."""
        if not self.turns or self.turns[-1].provider != provider:
            return None
        return self.sessions.get(provider)

    # ----- public API -----

    def send(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderName] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> Response:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        resp = client.chat(
            prefix + prompt if prefix else prompt,
            session_id=native_session,
            images=images,
        )
        self._record(prov, prompt, resp.text, resp.session_id)
        return resp

    def stream(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderName] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> Iterator[Message]:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        chunks: list[str] = []
        session_id: Optional[str] = None
        for msg in client.stream(
            prefix + prompt if prefix else prompt,
            session_id=native_session,
            images=images,
        ):
            if msg.kind == "text" and msg.text:
                chunks.append(msg.text)
            if msg.kind == "session" and msg.session_id:
                session_id = msg.session_id
            yield msg

        self._record(prov, prompt, "".join(chunks), session_id)

    async def asend(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderName] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> Response:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        resp = await client.achat(
            prefix + prompt if prefix else prompt,
            session_id=native_session,
            images=images,
        )
        self._record(prov, prompt, resp.text, resp.session_id)
        return resp

    async def astream(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderName] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> AsyncIterator[Message]:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        chunks: list[str] = []
        session_id: Optional[str] = None
        async for msg in client.astream(
            prefix + prompt if prefix else prompt,
            session_id=native_session,
            images=images,
        ):
            if msg.kind == "text" and msg.text:
                chunks.append(msg.text)
            if msg.kind == "session" and msg.session_id:
                session_id = msg.session_id
            yield msg

        self._record(prov, prompt, "".join(chunks), session_id)

    # ----- state -----

    def _record(
        self,
        provider: ProviderName,
        prompt: str,
        text: str,
        session_id: Optional[str],
    ) -> None:
        if session_id:
            self.sessions[provider] = session_id
        self.turns.append(Turn(provider=provider, prompt=prompt, text=text))

    def history(self) -> list[Turn]:
        return list(self.turns)
