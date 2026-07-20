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
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional

from .base import BaseProvider
from .core import Message, ProviderId, Response
from .errors import UnifiedError
from .factory import create
from .i18n import t


@dataclass
class Turn:
    provider: ProviderId
    prompt: str
    text: str
    timestamp: float = field(default_factory=time.time)


_FINAL_TEXT_ENVELOPES = frozenset((
    "assistant", "message.completed", "response.completed",
))


def _append_novel_stream_text(chunks: list[str], message: Message) -> None:
    """Accumulate text while reconciling explicit cumulative final envelopes.

    Most provider messages are deltas and are always retained.  A small set of
    raw markers identifies authoritative final text; only then do we remove an
    already-recorded prefix.  Prefix comparison walks the existing chunks so
    it does not build an extra unbounded copy of the transcript.
    """
    text = message.text
    if not isinstance(text, str) or not text:
        return
    raw = message.raw
    explicitly_final = isinstance(raw, dict) and (
        raw.get("final") is True
        or raw.get("partial") is False
        or raw.get("type") in _FINAL_TEXT_ENVELOPES
    )
    if not explicitly_final or not chunks:
        chunks.append(text)
        return

    offset = 0
    for chunk in chunks:
        end = offset + len(chunk)
        if end > len(text) or text[offset:end] != chunk:
            # This is a separate final block, not a cumulative replay.
            chunks.append(text)
            return
        offset = end
    if offset < len(text):
        chunks.append(text[offset:])


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
        default_provider: ProviderId = "claude",
        default_model: Optional[str] = None,
        sticky: bool = False,
        context_window: int = 8,
        cross_provider_context: bool = True,
        provider_opts: Optional[dict] = None,
        provider_opts_by_provider: Optional[dict[ProviderId, dict]] = None,
        max_turns: Optional[int] = None,
        max_turn_chars: Optional[int] = None,
        max_clients: Optional[int] = None,
    ):
        for name, value in (
            ("context_window", context_window),
            ("max_turns", max_turns),
            ("max_turn_chars", max_turn_chars),
            ("max_clients", max_clients),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1 when set")
        if type(cross_provider_context) is not bool:
            raise ValueError("cross_provider_context must be a boolean")
        self.default_provider = default_provider
        self.default_model = default_model
        self.sticky = sticky
        self.context_window = context_window
        self.cross_provider_context = cross_provider_context
        self.provider_opts = provider_opts or {}
        # Global provider_opts remain the backwards-compatible default. A
        # caller with genuinely provider-specific flags (notably the local
        # HTTP server's restricted execution profile) can override only the
        # matching provider without leaking unknown kwargs to the others.
        self.provider_opts_by_provider = {
            provider: dict(options)
            for provider, options in (provider_opts_by_provider or {}).items()
        }
        # Public conversations deliberately remain unbounded by default. The
        # local HTTP server opts into these limits so an arbitrary persistent
        # user id cannot retain an unlimited transcript/client cache.
        self.max_turns = max_turns
        self.max_turn_chars = max_turn_chars
        self.max_clients = max_clients

        self.sessions: dict[ProviderId, str] = {}
        self.turns: list[Turn] = []
        self._clients: "OrderedDict[tuple[ProviderId, Optional[str]], BaseProvider]" = OrderedDict()
        self._locked_provider: Optional[ProviderId] = None

    # ----- helpers -----

    def _get_client(self, provider: ProviderId, model: Optional[str]) -> BaseProvider:
        key = (provider, model)
        if key not in self._clients:
            if self.max_clients is not None and len(self._clients) >= self.max_clients:
                # The cache only preserves process/provider configuration; a
                # native session id remains in self.sessions, so recreating the
                # least-recently-used client is safe.
                self._clients.popitem(last=False)
            opts = {
                **self.provider_opts,
                **self.provider_opts_by_provider.get(provider, {}),
            }
            if provider != "claude":
                # `terse` is a Claude-only constructor option; passing it to
                # codex/gemini would raise TypeError (unexpected kwarg).
                opts.pop("terse", None)
            self._clients[key] = create(provider, model=model, **opts)
        else:
            self._clients.move_to_end(key)
        return self._clients[key]

    def _resolve(
        self,
        provider: Optional[ProviderId],
        model: Optional[str],
    ) -> tuple[ProviderId, Optional[str]]:
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

    def _context_prefix_if_switch(self, provider: ProviderId) -> str:
        """Build a short '<prior-context>' prefix when switching providers."""
        if not self.cross_provider_context or not self.turns:
            return ""
        last_provider = self.turns[-1].provider
        if last_provider == provider:
            return ""
        # Skip empty placeholder turns (e.g. the REPL resume seed) so they don't
        # inject a blank "User:/Assistant:" pair into the switched-to prompt.
        recent = [tr for tr in self.turns[-self.context_window:]
                  if tr.prompt.strip() or tr.text.strip()]
        if not recent:
            return ""
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

    def _use_native_session(self, provider: ProviderId) -> Optional[str]:
        """Return the stored session_id iff the previous turn was this provider."""
        if not self.turns or self.turns[-1].provider != provider:
            return None
        return self.sessions.get(provider)

    # ----- public API -----

    def send(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderId] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Response:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        call_kwargs = {"session_id": native_session, "images": images}
        # Third-party/test provider doubles may implement the historical
        # method shape without **kwargs. Preserve that boundary unless a
        # caller explicitly opts into cancellation.
        if cancel_event is not None:
            call_kwargs["cancel_event"] = cancel_event
        resp = client.chat(prefix + prompt if prefix else prompt, **call_kwargs)
        self._record(prov, prompt, resp.text, resp.session_id)
        return resp

    def stream(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderId] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[Message]:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        chunks: list[str] = []
        session_id: Optional[str] = None
        try:
            call_kwargs = {"session_id": native_session, "images": images}
            if cancel_event is not None:
                call_kwargs["cancel_event"] = cancel_event
            for msg in client.stream(
                prefix + prompt if prefix else prompt, **call_kwargs
            ):
                if msg.kind == "text":
                    _append_novel_stream_text(chunks, msg)
                if msg.kind == "session" and msg.session_id:
                    session_id = msg.session_id
                    # Persist immediately: a consumer that stops early (Ctrl+C,
                    # SSE disconnect) must not lose the native session id.
                    self.sessions[prov] = session_id
                yield msg
        except GeneratorExit:
            # Consumer stopped early — record the partial turn so context and
            # session resume survive, then propagate the close.
            self._record(prov, prompt, "".join(chunks), session_id)
            raise
        else:
            self._record(prov, prompt, "".join(chunks), session_id)

    async def asend(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderId] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Response:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        call_kwargs = {"session_id": native_session, "images": images}
        if cancel_event is not None:
            call_kwargs["cancel_event"] = cancel_event
        resp = await client.achat(
            prefix + prompt if prefix else prompt, **call_kwargs)
        self._record(prov, prompt, resp.text, resp.session_id)
        return resp

    async def astream(
        self,
        prompt: str,
        *,
        provider: Optional[ProviderId] = None,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> AsyncIterator[Message]:
        prov, mdl = self._resolve(provider, model)
        client = self._get_client(prov, mdl)

        prefix = self._context_prefix_if_switch(prov)
        native_session = self._use_native_session(prov)

        chunks: list[str] = []
        session_id: Optional[str] = None
        try:
            call_kwargs = {"session_id": native_session, "images": images}
            if cancel_event is not None:
                call_kwargs["cancel_event"] = cancel_event
            async for msg in client.astream(
                prefix + prompt if prefix else prompt, **call_kwargs
            ):
                if msg.kind == "text":
                    _append_novel_stream_text(chunks, msg)
                if msg.kind == "session" and msg.session_id:
                    session_id = msg.session_id
                    self.sessions[prov] = session_id  # persist eagerly (see stream())
                yield msg
        except GeneratorExit:
            self._record(prov, prompt, "".join(chunks), session_id)
            raise
        else:
            self._record(prov, prompt, "".join(chunks), session_id)

    # ----- state -----

    def _record(
        self,
        provider: ProviderId,
        prompt: str,
        text: str,
        session_id: Optional[str],
    ) -> None:
        if session_id:
            self.sessions[provider] = session_id
        if self.max_turn_chars is not None:
            prompt = self._truncate_for_storage(prompt)
            text = self._truncate_for_storage(text)
        self.turns.append(Turn(provider=provider, prompt=prompt, text=text))
        if self.max_turns is not None and len(self.turns) > self.max_turns:
            del self.turns[:-self.max_turns]

    def _truncate_for_storage(self, value: str) -> str:
        """Bound stored server history without changing the live response."""
        assert self.max_turn_chars is not None
        if len(value) <= self.max_turn_chars:
            return value
        if self.max_turn_chars == 1:
            return "…"
        return value[:self.max_turn_chars - 1] + "…"

    def history(self) -> list[Turn]:
        return list(self.turns)
