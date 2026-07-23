"""Safe, bounded rendering for normalized provider stream events."""

from __future__ import annotations

import time
import unicodedata
from typing import Any, Callable, Optional

from rich.console import Console
from rich.text import Text

from .i18n import t


def safe_terminal_text(value: object, *, max_chars: int = 16_384) -> str:
    """Return terminal-safe text with markup and control effects disabled.

    Rich markup is avoided by rendering :class:`~rich.text.Text`; this helper
    additionally replaces terminal/BiDi control characters.  Newlines and
    tabs are retained because assistant prose relies on them.
    """
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    out: list[str] = []
    for char in value[:max_chars]:
        if char in ("\n", "\t"):
            out.append(char)
            continue
        if unicodedata.category(char).startswith("C"):
            out.append("�")
        else:
            out.append(char)
    if len(value) > max_chars:
        out.append("…")
    return "".join(out)


class EventRenderer:
    """Render ``Message`` objects without reflecting raw provider payloads.

    Tool inputs/results and reasoning are intentionally not printed.  Tool
    lifecycle lines use only a sanitized name, success state, and elapsed time.
    Reasoning can be enabled only for events explicitly marked as a public
    summary by the provider; ordinary ``reasoning`` events always stay hidden.
    """

    def __init__(
        self,
        console: Optional[Console] = None,
        *,
        show_reasoning_summaries: bool = False,
        max_events: int = 10_000,
        max_text_chars: int = 1_000_000,
        max_tools: int = 1_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.console = console or Console()
        self.show_reasoning_summaries = show_reasoning_summaries
        self.max_events = max(1, int(max_events))
        self.max_text_chars = max(1, int(max_text_chars))
        self.max_tools = max(1, int(max_tools))
        self._clock = clock
        self._event_count = 0
        self._text_chars = 0
        self._emitted_text = ""
        self._tools: dict[str, tuple[str, float]] = {}
        self._anonymous_tool = 0
        self._flood_noted = False
        self._text_limit_noted = False
        self.text_started = False
        self._line_open = False

    def render(self, message: Any) -> bool:
        """Render one event and return whether assistant text is now visible."""
        self._event_count += 1
        if self._event_count > self.max_events:
            self._note_flood()
            return self.text_started
        try:
            kind = getattr(message, "kind", "")
            if kind == "text":
                self._render_text(getattr(message, "text", None), getattr(message, "raw", None))
            elif kind == "reasoning":
                self._render_reasoning(message)
            elif kind == "tool_use":
                self._render_tool_use(getattr(message, "tool", None))
            elif kind == "tool_result":
                self._render_tool_result(getattr(message, "tool", None))
            elif kind == "error":
                self._line(t("repl.renderer.provider_error"), style="red")
        except Exception:  # noqa: BLE001 - untrusted extension event boundary
            # Malformed extension events are untrusted.  Do not reflect the raw
            # object or exception; one stable line is enough for recovery.
            self._line(t("repl.renderer.malformed_event"), style="yellow")
        return self.text_started

    def finish(self) -> None:
        if self._line_open:
            self.console.print()
            self._line_open = False

    def _render_text(self, value: object, raw: object) -> None:
        if not isinstance(value, str) or not value:
            return
        text = self._novel_text(value, raw)
        if not text:
            return
        remaining = self.max_text_chars - self._text_chars
        if remaining <= 0:
            self._note_text_limit()
            return
        visible = safe_terminal_text(text[:remaining], max_chars=remaining)
        if not visible:
            return
        self.console.print(Text(visible), end="", soft_wrap=True)
        self._line_open = not visible.endswith("\n")
        self.text_started = True
        self._text_chars += len(text[:remaining])
        self._emitted_text = (self._emitted_text + text[:remaining])[-65_536:]
        if len(text) > remaining:
            self._note_text_limit()

    def _novel_text(self, text: str, raw: object) -> str:
        if not isinstance(raw, dict):
            return text
        event_type = raw.get("type")
        explicitly_final = (
            raw.get("final") is True
            or raw.get("partial") is False
            or event_type in {"assistant", "message.completed", "response.completed"}
        )
        if not explicitly_final or not self._emitted_text:
            return text
        if text == self._emitted_text:
            return ""
        if text.startswith(self._emitted_text):
            return text[len(self._emitted_text):]
        return text

    def _render_reasoning(self, message: Any) -> None:
        if not self.show_reasoning_summaries:
            return
        raw = getattr(message, "raw", None)
        if not isinstance(raw, dict):
            return
        is_public = (
            raw.get("public_summary") is True
            or raw.get("visibility") in {"public", "summary"}
            or raw.get("type") in {"reasoning_summary", "summary"}
        )
        if not is_public:
            return
        text = getattr(message, "text", None)
        if isinstance(text, str) and text:
            self._line(t("repl.renderer.reasoning_summary", text=safe_terminal_text(text)), style="dim")

    def _render_tool_use(self, tool: object) -> None:
        data = tool if isinstance(tool, dict) else {}
        name = safe_terminal_text(data.get("name") or "tool", max_chars=80)
        identifier = data.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            self._anonymous_tool += 1
            identifier = "anonymous-" + str(self._anonymous_tool)
        if len(self._tools) < self.max_tools:
            self._tools[identifier] = (name, self._clock())
        self._line(t("repl.renderer.tool_started", name=name), style="dim")

    def _render_tool_result(self, tool: object) -> None:
        data = tool if isinstance(tool, dict) else {}
        identifier = data.get("id")
        record = self._tools.pop(identifier, None) if isinstance(identifier, str) else None
        failed = bool(data.get("is_error"))
        state = t("repl.renderer.tool_state.failed" if failed else "repl.renderer.tool_state.completed")
        if record is None:
            self._line(t("repl.renderer.tool_unmatched", state=state), style="red" if failed else "dim")
            return
        name, started = record
        elapsed = max(0.0, self._clock() - started)
        self._line(
            t("repl.renderer.tool_result", state=state, name=name, elapsed=elapsed),
            style="red" if failed else "dim",
        )

    def _line(self, value: str, *, style: str = "") -> None:
        if self._line_open:
            self.console.print()
            self._line_open = False
        self.console.print(Text(safe_terminal_text(value), style=style))

    def _note_flood(self) -> None:
        if not self._flood_noted:
            self._flood_noted = True
            self._line(t("repl.renderer.event_limit"), style="yellow")

    def _note_text_limit(self) -> None:
        if not self._text_limit_noted:
            self._text_limit_noted = True
            self._line(t("repl.renderer.text_limit"), style="yellow")
