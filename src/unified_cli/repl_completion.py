"""Slash-command registry + prompt_toolkit completion for the REPL.

The pure candidate helpers (`slash_candidates`, `arg_candidates`,
`cached_or_hardcoded`) are terminal-free and unit-tested directly. The
prompt_toolkit pieces (`UnifiedCompleter`, `build_session`, `pick_model`) are
imported lazily so this module imports even if prompt_toolkit is somehow
missing — the REPL then falls back to plain `input()`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from . import models
from .i18n import t
from .models import DEFAULT_MODELS
from .providers.gemini import gemini_enabled

_PROVIDERS = ("claude", "codex", "gemini")


# ---------- slash command registry (single source of truth) ----------

@dataclass(frozen=True)
class SlashCommand:
    name: str                 # "/model"
    arg_hint: str             # "<name>" or ""
    desc_key: str             # i18n key for the description
    takes: Optional[str] = None  # None | "model" | "provider" | "lang" | "path" | "int"
    group: str = "info"       # for grouping in /help


SLASH_COMMANDS: list[SlashCommand] = [
    SlashCommand("/help", "", "slash.desc.help", group="info"),
    SlashCommand("/model", "<name>", "slash.desc.model", takes="model", group="model"),
    SlashCommand("/provider", "<claude|codex|gemini>", "slash.desc.provider", takes="provider", group="model"),
    SlashCommand("/image", "<path>", "slash.desc.image", takes="path", group="image"),
    SlashCommand("/images", "", "slash.desc.images", group="image"),
    SlashCommand("/clear-images", "", "slash.desc.clear-images", group="image"),
    SlashCommand("/new", "", "slash.desc.new", group="session"),
    SlashCommand("/save", "", "slash.desc.save", group="session"),
    SlashCommand("/history", "[N]", "slash.desc.history", takes="int", group="session"),
    SlashCommand("/tokens", "", "slash.desc.tokens", group="info"),
    SlashCommand("/doctor", "", "slash.desc.doctor", group="info"),
    SlashCommand("/status", "", "slash.desc.status", group="info"),
    SlashCommand("/lang", "<en|ko>", "slash.desc.lang", takes="lang", group="info"),
    SlashCommand("/exit", "", "slash.desc.exit", group="session"),
]

_BY_NAME = {c.name: c for c in SLASH_COMMANDS}


def command_names() -> list[str]:
    return [c.name for c in SLASH_COMMANDS]


# ---------- model source (never blocks the prompt) ----------

def cached_or_hardcoded(provider: str) -> list:
    """Return a model list *instantly*: the warm TTL cache if present, else the
    hardcoded fallback. Never triggers a network/subprocess call on the UI
    thread (use warm_models_async to populate the cache in the background).
    """
    cached = models._cached(provider)  # type: ignore[arg-type]
    if cached is not None:
        return cached
    return models._hardcoded(provider)  # type: ignore[arg-type]


def warm_models_async(providers=("claude", "codex")) -> threading.Thread:
    """Populate the model cache in a daemon thread so the first /model is warm.

    Gemini is excluded by default — `agy models` is a subprocess that may not
    be wanted (provider is gated) and can take seconds; warm it lazily instead.
    """
    def _warm():
        for p in providers:
            try:
                models.list_models(p)  # type: ignore[arg-type]
            except Exception:
                pass

    th = threading.Thread(target=_warm, name="uc-model-warm", daemon=True)
    th.start()
    return th


# ---------- pure candidate helpers (unit-tested, no terminal) ----------

def slash_candidates(token: str) -> list:
    """[(name, description)] for slash commands whose name starts with `token`."""
    token = token.strip()
    return [(c.name, t(c.desc_key)) for c in SLASH_COMMANDS if c.name.startswith(token)]


def _provider_meta(provider: str) -> str:
    if provider == "gemini" and not gemini_enabled():
        return t("repl.picker.locked_suffix").strip()
    return ""


def arg_candidates(cmd: str, provider: str, token: str) -> list:
    """[(value, meta)] for the argument of a slash command.

    - /provider → the three providers (gemini marked locked when gated)
    - /model    → model ids for `provider` (default marked ★, gemini locked)
    - /lang     → en|ko
    """
    tok = (token or "").lower()
    if cmd == "/provider":
        return [(p, _provider_meta(p)) for p in _PROVIDERS if p.startswith(tok)]
    if cmd == "/lang":
        return [(c, "") for c in ("en", "ko") if c.startswith(tok)]
    if cmd == "/model":
        default_id = DEFAULT_MODELS.get(provider)  # type: ignore[arg-type]
        locked = provider == "gemini" and not gemini_enabled()
        out: list = []
        seen: set = set()
        for m in cached_or_hardcoded(provider):
            mid = getattr(m, "id", None)
            if not mid or mid in seen:
                continue
            seen.add(mid)
            if tok and not mid.lower().startswith(tok):
                continue
            meta = getattr(m, "display_name", "") or ""
            if getattr(m, "default", False) or mid == default_id:
                meta = (meta + " ★").strip()
            if locked:
                meta = (meta + t("repl.picker.locked_suffix")).strip()
            out.append((mid, meta))
        return out
    return []


# ---------- prompt_toolkit integration (lazy) ----------

try:  # prompt_toolkit is a core dep, but degrade gracefully if absent
    from prompt_toolkit.completion import Completer, Completion
    _HAS_PTK = True
except Exception:  # pragma: no cover
    _HAS_PTK = False
    Completer = object  # type: ignore[assignment,misc]
    Completion = None  # type: ignore[assignment]


def has_prompt_toolkit() -> bool:
    return _HAS_PTK


class UnifiedCompleter(Completer):  # type: ignore[misc]
    """Live completion: slash commands when the line starts with '/', then
    argument completion for /model · /provider · /lang. Reads the live
    `current` dict by reference so the model list tracks provider switches.
    """

    def __init__(self, current: dict):
        self.current = current

    def get_completions(self, document, complete_event):  # noqa: D401
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return
        if " " not in stripped:
            for name, meta in slash_candidates(stripped):
                yield Completion(name, start_position=-len(stripped), display_meta=meta)
            return
        cmd, _, arg = stripped.partition(" ")
        if cmd not in _BY_NAME:
            return
        last = "" if (arg == "" or arg.endswith(" ")) else arg.split()[-1]
        provider = self.current.get("provider", "claude")
        for value, meta in arg_candidates(cmd, provider, last):
            yield Completion(value, start_position=-len(last), display_meta=meta)


def build_session(history_path, current: dict):
    """Construct a prompt_toolkit PromptSession with live slash completion."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import FuzzyCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.shortcuts import CompleteStyle

    return PromptSession(
        history=FileHistory(str(history_path)),
        completer=FuzzyCompleter(UnifiedCompleter(current)),
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN,
    )


def pick_model(provider: str) -> Optional[str]:
    """Interactive model selector (used by `/model` with no arg). Returns the
    chosen id, or None if cancelled. Default is preselected.
    """
    from prompt_toolkit.shortcuts import radiolist_dialog
    from .i18n import t

    default_id = DEFAULT_MODELS.get(provider)  # type: ignore[arg-type]
    locked = provider == "gemini" and not gemini_enabled()
    values = []
    seen: set = set()
    preselect = None
    for m in cached_or_hardcoded(provider):
        mid = getattr(m, "id", None)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        label = mid
        disp = getattr(m, "display_name", "") or ""
        if disp and disp != mid:
            label = f"{mid}  —  {disp}"
        if getattr(m, "default", False) or mid == default_id:
            label += t("repl.picker.default_suffix")
            preselect = mid
        if locked:
            label += t("repl.picker.locked_suffix")
        values.append((mid, label))

    if not values:
        return None
    return radiolist_dialog(
        title=t("repl.picker.model_title", provider=provider),
        text=t("repl.picker.model_text"),
        values=values,
        default=preselect,
    ).run()


def pick_provider() -> Optional[str]:
    """Interactive provider selector. Returns chosen provider or None."""
    from prompt_toolkit.shortcuts import radiolist_dialog
    from .i18n import t

    values = []
    for p in _PROVIDERS:
        label = p + (t("repl.picker.locked_suffix") if _provider_meta(p) else "")
        values.append((p, label))
    return radiolist_dialog(
        title=t("repl.picker.provider_title"),
        text=t("repl.picker.provider_text"),
        values=values,
        default="claude",
    ).run()
