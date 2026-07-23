"""Slash-command registry + prompt_toolkit completion for the REPL.

The pure candidate helpers (`slash_candidates`, `arg_candidates`,
`cached_or_hardcoded`) are terminal-free and unit-tested directly. The
prompt_toolkit pieces (`UnifiedCompleter`, `build_session`, `pick_model`) are
imported lazily so this module imports even if prompt_toolkit is somehow
missing — the REPL then falls back to plain `input()`.
"""

from __future__ import annotations

import unicodedata
from typing import Mapping, Optional, Sequence

from . import models
from .i18n import t
from .models import DEFAULT_MODELS
from .providers.gemini import gemini_enabled
from .registry import passive_bundled_provider_descriptors
from .repl_commands import CommandSpec, DEFAULT_REGISTRY

_PROVIDERS = ("claude", "codex", "gemini")
# This Core-owned metadata API validates the packaged entry-point names and
# targets without enumerating installed distributions or importing providers.
BUNDLED_EXTENSION_PROVIDERS = tuple(
    descriptor.id for descriptor in passive_bundled_provider_descriptors()
)


# ---------- slash command registry (single source of truth) ----------

# Historical import name retained for downstream completers/tests.
SlashCommand = CommandSpec
SLASH_COMMANDS: list[CommandSpec] = [
    CommandSpec(
        name=name,
        arg_hint=command.arg_hint if name == command.name else "",
        desc_key=command.desc_key,
        takes=command.takes,
        group=command.group,
    )
    for command in DEFAULT_REGISTRY.commands
    for name in command.all_names
]
_BY_NAME = {command.name: command for command in SLASH_COMMANDS}


def command_names() -> list[str]:
    return DEFAULT_REGISTRY.names(include_aliases=True)


# ---------- model source (never blocks the prompt) ----------

def cached_or_hardcoded(provider: str) -> list:
    """Return a model list *instantly*: the warm TTL cache if present, else the
    hardcoded fallback. Never triggers a network/subprocess call on the UI
    thread (an explicit caller may use ``warm_models_async`` to refresh it).
    """
    cached = models._cached(provider)  # type: ignore[arg-type]
    if cached is not None:
        return cached
    return models._hardcoded(provider)  # type: ignore[arg-type]


def _completion_model_snapshot(provider: str) -> tuple:
    """Passive Core snapshot for completion/pickers; never computes context."""

    return tuple(models._peek_cached_or_hardcoded(provider))  # type: ignore[arg-type]


def warm_models_async(providers=("claude", "codex")):
    """Explicitly refresh model caches in a daemon thread.

    Kept as a compatibility API, but the REPL never calls it at startup.  A
    caller must opt in explicitly because model listing may access files,
    network services, or provider subprocesses.
    """
    import threading

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
    return [
        (command.name, t(command.desc_key))
        for command in SLASH_COMMANDS
        if command.name.startswith(token)
    ]


def _provider_meta(provider: str) -> str:
    if provider == "gemini" and not gemini_enabled():
        return t("repl.picker.locked_suffix").strip()
    if provider not in _PROVIDERS:
        return t("repl.picker.ext_preview_suffix").strip()
    return ""


def arg_candidates(
    cmd: str,
    provider: str,
    token: str,
    full_argument: Optional[str] = None,
) -> list:
    """Compatibility wrapper retaining the original public call signature."""

    return _arg_candidates_from_snapshots(
        cmd, provider, token, full_argument,
        provider_snapshots=None, model_snapshots=None,
    )


def _arg_candidates_from_snapshots(
    cmd: str,
    provider: str,
    token: str,
    full_argument: Optional[str],
    *,
    provider_snapshots: Optional[Mapping[str, object]],
    model_snapshots: Optional[Mapping[str, Sequence[object]]],
) -> list:
    """[(value, meta)] for the argument of a slash command.

    - /provider → the three providers (gemini marked locked when gated)
    - /model    → model ids for `provider` (default marked ★, gemini locked)
    - /lang     → en|ko
    """
    tok = (token or "").lower()
    providers = list(_PROVIDERS) + list(BUNDLED_EXTENSION_PROVIDERS)
    if provider_snapshots is not None:
        for candidate in provider_snapshots:
            if (
                candidate not in providers
                and _safe_provider_id(candidate)
            ):
                providers.append(candidate)
    if cmd == "/provider":
        return [(p, _provider_meta(p)) for p in providers if p.startswith(tok)]
    if cmd == "/lang":
        return [(c, "") for c in ("en", "ko") if c.startswith(tok)]
    if cmd == "/auth":
        words = (full_argument or token or "").split()
        trailing_space = bool(full_argument and full_argument.endswith(" "))
        if len(words) <= 1 and not trailing_space:
            return [
                (value, "") for value in ("status", "login", "logout")
                if value.startswith(tok)
            ]
        return [(value, "") for value in providers if value.startswith(tok)]
    if cmd == "/web":
        return [(c, "") for c in ("default", "on", "off") if c.startswith(tok)]
    if cmd == "/multiline":
        return [(c, "") for c in ("on", "off") if c.startswith(tok)]
    if cmd == "/reasoning":
        return [(c, "") for c in ("hidden", "summary") if c.startswith(tok)]
    if cmd == "/theme":
        return [(c, "") for c in ("auto", "dark", "light") if c.startswith(tok)]
    if cmd == "/timeout":
        return [("default", "")] if "default".startswith(tok) else []
    if cmd == "/permissions":
        return [
            (value, "")
            for value in ("provider_default", "read_only", "workspace_write")
            if value.startswith(tok)
        ]
    if cmd == "/effort":
        return [
            (value, "")
            for value in ("default", "low", "medium", "high", "xhigh", "max")
            if value.startswith(tok)
        ]
    if cmd == "/style":
        return [
            (value, "")
            for value in ("default", "friendly", "pragmatic", "none")
            if value.startswith(tok)
        ]
    if cmd == "/system":
        return [(value, "") for value in ("default", "clear") if value.startswith(tok)]
    if cmd == "/add-dir":
        return [("clear", "")] if "clear".startswith(tok) else []
    if cmd == "/model":
        refresh = [("--refresh", t("repl.model.refresh_meta"))]
        if tok.startswith("-"):
            return refresh if "--refresh".startswith(tok) else []
        # Completion must remain terminal-local and instantaneous.  Extension
        # model listing is only performed by an explicit `/model --refresh`.
        if model_snapshots is None:
            if provider not in _PROVIDERS:
                return []
            model_choices = cached_or_hardcoded(provider)
        else:
            model_choices = model_snapshots.get(provider, ())
        default_id = DEFAULT_MODELS.get(provider)  # type: ignore[arg-type]
        locked = provider == "gemini" and not gemini_enabled()
        out: list = []
        seen: set = set()
        for m in model_choices:
            mid = getattr(m, "id", None)
            if not _safe_model_id(mid) or mid in seen:
                continue
            seen.add(mid)
            if tok and not mid.lower().startswith(tok):
                continue
            meta = _safe_display_meta(getattr(m, "display_name", "") or "")
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
        supplied = current.get("_completion_core_models")
        if type(supplied) is dict:
            self._core_model_snapshots = supplied
        else:
            # Snapshot Core metadata at construction, never from a completion
            # callback.  This is cache/hardcoded-only and performs no probe.
            self._core_model_snapshots = {
                provider: _completion_model_snapshot(provider)
                for provider in _PROVIDERS
            }

    def get_completions(self, document, complete_event):  # noqa: D401
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return
        if not any(char.isspace() for char in stripped):
            for name, meta in slash_candidates(stripped):
                yield Completion(name, start_position=-len(stripped), display_meta=meta)
            return
        boundary = next(
            (index for index, char in enumerate(stripped) if char.isspace()),
            None,
        )
        if boundary is None:
            return
        invoked = stripped[:boundary]
        spec, ambiguous = DEFAULT_REGISTRY.resolve_prefix(invoked)
        if spec is None or ambiguous:
            return
        cmd = spec.name
        arg = stripped[boundary + 1:]
        last = "" if (arg == "" or arg[-1:].isspace()) else arg.split()[-1]
        provider = self.current.get("provider", "claude")
        descriptors = self.current.get("loaded_extension_descriptors")
        if type(descriptors) is not dict:
            descriptors = {}
        ext_models = self.current.get("loaded_extension_models")
        if type(ext_models) is not dict:
            ext_models = {}
        model_snapshots = dict(self._core_model_snapshots)
        model_snapshots.update(ext_models)
        for value, meta in _arg_candidates_from_snapshots(
            cmd,
            provider,
            last,
            arg,
            provider_snapshots=descriptors,
            model_snapshots=model_snapshots,
        ):
            yield Completion(value, start_position=-len(last), display_meta=meta)


def build_session(history_path, current: dict, *, input=None, output=None):
    """Construct a PromptSession with safe multiline keys and a live toolbar.

    Enter always submits.  Alt/Option+Enter and Ctrl+J insert a newline.  The
    latter is registered as an escape-prefixed key where terminal encodings
    permit it; unsupported terminals simply retain their normal behavior.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import FuzzyCompleter
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.shortcuts import CompleteStyle

    if type(current.get("_completion_core_models")) is not dict:
        current["_completion_core_models"] = {
            provider: _completion_model_snapshot(provider)
            for provider in _PROVIDERS
        }

    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _alt_enter(event):
        if current.get("multiline", True):
            event.current_buffer.insert_text("\n")

    # Ctrl+J is LF, distinct from Enter/CR on terminals that preserve it.
    @bindings.add("c-j")
    def _ctrl_j(event):
        if current.get("multiline", True):
            event.current_buffer.insert_text("\n")

    history = FileHistory(str(history_path)) if history_path is not None else InMemoryHistory()
    return PromptSession(
        history=history,
        completer=FuzzyCompleter(UnifiedCompleter(current)),
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN,
        key_bindings=bindings,
        multiline=False,
        bottom_toolbar=lambda: _bottom_toolbar(current),
        input=input,
        output=output,
    )


def _bottom_toolbar(current: dict) -> str:
    """Narrow-terminal-friendly, markup-free status line."""
    from pathlib import Path
    import shutil
    from .usage import tracker

    provider = _toolbar_piece(current.get("provider", "?"), 14)
    model = _toolbar_piece(current.get("model", "?"), 20)
    cwd_value = str(current.get("cwd") or "")
    cwd = _toolbar_piece(Path(cwd_value).name or cwd_value or ".", 18)
    permission = _toolbar_piece(current.get("permission_mode", "provider_default"), 16)
    if current.get("web_explicit", True):
        web = "web:on" if current.get("web_search", True) else "web:off"
    else:
        web = "web:default"
    context = "ctx:" + str(current.get("context_window", 8))
    totals = sum(a.input_tokens + a.output_tokens for a in tracker.aggregates())
    latency = current.get("last_latency_ms", 0)
    line = (
        provider + "/" + model + "  " + cwd + "  perm:" + permission + "  "
        + web + "  " + context + "  tok:" + str(totals) + "  "
        + ("lat:" + str(latency) + "ms" if latency else "lat:—")
    )
    # Leave one cell for prompt_toolkit's terminal bookkeeping, but never use
    # a wider floor than the terminal actually reports.
    columns = max(1, shutil.get_terminal_size(fallback=(120, 24)).columns - 1)
    return _truncate_display_width(line, columns)


def _toolbar_piece(value: object, limit: int) -> str:
    # Prompt-toolkit receives plain text, but terminal control characters still
    # need removal because model/cwd values can be user-controlled.
    from .event_renderer import safe_terminal_text
    text = safe_terminal_text(value, max_chars=max(256, limit * 4))
    return _truncate_display_width(text, limit)


def _display_width(value: str) -> int:
    """Return terminal-cell width for sanitized toolbar text.

    East Asian wide/fullwidth characters take two cells; combining marks take
    none. Ambiguous-width characters deliberately retain the portable width of
    one cell, matching prompt_toolkit's conservative terminal behavior.
    """
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate_display_width(value: str, limit: int) -> str:
    """Clip sanitized text to ``limit`` terminal cells with an ellipsis."""
    if limit <= 0:
        return ""
    if _display_width(value) <= limit:
        return value
    ellipsis = "…"
    available = max(0, limit - _display_width(ellipsis))
    out: list[str] = []
    width = 0
    for char in value:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if char_width == 0:
            # Do not leave a leading combining mark to attach to the ellipsis.
            if out:
                out.append(char)
            continue
        if width + char_width > available:
            break
        out.append(char)
        width += char_width
    return "".join(out) + ellipsis


def _safe_model_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 512:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        return False
    return True


def _safe_provider_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 64:
        return False
    if not value[0].islower() or not value[0].isascii():
        return False
    return all(
        (char.isascii() and (char.islower() or char.isdigit()))
        or char in "-_"
        for char in value
    )


def _safe_display_meta(value: object) -> str:
    from .event_renderer import safe_terminal_text
    return safe_terminal_text(value, max_chars=512)


def pick_model(provider: str, choices: Optional[list] = None) -> Optional[str]:
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
    model_choices = choices
    if model_choices is None:
        model_choices = cached_or_hardcoded(provider) if provider in _PROVIDERS else []
    for m in model_choices:
        mid = getattr(m, "id", None)
        if not _safe_model_id(mid) or mid in seen:
            continue
        seen.add(mid)
        label = mid
        disp = _safe_display_meta(getattr(m, "display_name", "") or "")
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
    """Interactive Core provider selector; original signature retained."""

    return _pick_provider_from_snapshots(())


def pick_provider_from_snapshots(choices: Sequence[str]) -> Optional[str]:
    """Interactive selector for Core plus already-loaded Ext snapshots."""

    return _pick_provider_from_snapshots(choices)


def _pick_provider_from_snapshots(choices: Sequence[str]) -> Optional[str]:
    from prompt_toolkit.shortcuts import radiolist_dialog
    from .i18n import t

    values = []
    providers = list(_PROVIDERS) + list(BUNDLED_EXTENSION_PROVIDERS)
    for candidate in choices:
        if candidate not in providers and _safe_provider_id(candidate):
            providers.append(candidate)
    for p in providers:
        label = p + (t("repl.picker.locked_suffix") if _provider_meta(p) else "")
        values.append((p, label))
    return radiolist_dialog(
        title=t("repl.picker.provider_title"),
        text=t("repl.picker.provider_text"),
        values=values,
        default="claude",
    ).run()


def pick_value(
    name: str,
    choices: Sequence[object],
    *,
    default: Optional[str] = None,
) -> Optional[str]:
    """Open a small safe value picker used by no-argument slash commands."""
    from prompt_toolkit.shortcuts import radiolist_dialog

    values = []
    seen: set[str] = set()
    for choice in choices:
        if isinstance(choice, tuple) and len(choice) == 2:
            value, label = choice
        else:
            value = label = choice
        if (
            type(value) is not str
            or not value
            or value in seen
            or type(label) is not str
        ):
            continue
        seen.add(value)
        values.append((value, _safe_display_meta(label)))
    if not values:
        return None
    selected = default if default in seen else values[0][0]
    return radiolist_dialog(
        title=t("repl.picker.setting_title", name=name),
        text=t("repl.picker.setting_text"),
        values=values,
        default=selected,
    ).run()


def prompt_value(
    name: str,
    *,
    default: str = "",
) -> Optional[str]:
    """Open a bounded prompt-toolkit input dialog for a setting value."""
    from prompt_toolkit.shortcuts import input_dialog

    safe_default = default if type(default) is str and len(default) <= 16_384 else ""
    return input_dialog(
        title=t("repl.picker.input_title", name=name),
        text=t("repl.picker.input_text"),
        default=safe_default,
    ).run()
