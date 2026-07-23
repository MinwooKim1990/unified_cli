"""Interactive REPL — `unified-cli repl`.

Multi-turn chat + provider switching + slash commands in one process. Uses
`UnifiedConversation(sticky=False)`, so switching provider auto-injects the
last 8 turns into the new provider's prompt.

When prompt_toolkit is available and stdout is a TTY, the REPL offers a live
slash-command menu (type `/`) and model pickers; otherwise it falls back to a
plain `input()` loop with readline history. All user-facing text is localized
(English default; `--lang ko` / `/lang ko` for Korean).
"""

from __future__ import annotations

import atexit
import json
import os
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from . import settings
from .base import _popen_process_group_kwargs, _terminate_process_tree
from .conversation import UnifiedConversation
from .core import ProviderId
from .errors import UnifiedError
from .event_renderer import EventRenderer, safe_terminal_text
from .factory import PROVIDERS
from .i18n import current_lang, set_lang, t
from .models import DEFAULT_MODELS
from .providers.gemini import gemini_enabled
from .repl_commands import CORE_AUTH_SPECS, DEFAULT_REGISTRY, CommandSpec
from .repl_completion import (
    _completion_model_snapshot, arg_candidates, build_session,
    has_prompt_toolkit, pick_model, pick_provider, pick_provider_from_snapshots,
)
from .repl_state import ReplState
from .state import resolve_cwd, save_last_session
from .ui import status_layout
from .usage import tracker


console = Console()
_HISTORY_FILE = Path.home() / ".unified-cli" / "repl_history"          # readline
_PTK_HISTORY_FILE = Path.home() / ".unified-cli" / "repl_history.ptk"   # prompt_toolkit

_PERMISSION_VALUES = ("provider_default", "read_only", "workspace_write")
_EFFORT_VALUES = ("default", "low", "medium", "high", "xhigh", "max")
_STYLE_VALUES = ("default", "friendly", "pragmatic", "none")
_CORE_DIR_PROVIDERS = frozenset(("claude", "codex", "gemini"))


def _valid_explicit_provider_id(value: object) -> bool:
    from .plugin import _valid_provider_id

    return _valid_provider_id(value)


def _core_model_choices(current: dict, provider: str) -> list:
    """Return/create a passive Core model snapshot for REPL UI surfaces."""

    snapshots = current.get("_completion_core_models")
    if type(snapshots) is not dict:
        snapshots = {}
        current["_completion_core_models"] = snapshots
    choices = snapshots.get(provider)
    if not isinstance(choices, (list, tuple)):
        choices = _completion_model_snapshot(provider)
        snapshots[provider] = choices
    return list(choices)


def _replace_core_model_choices(
    current: dict, provider: str, choices: list,
) -> None:
    """Commit one explicit refresh into the dict shared by live completers."""

    snapshots = current.get("_completion_core_models")
    if type(snapshots) is not dict:
        snapshots = {}
        current["_completion_core_models"] = snapshots
    snapshots[provider] = tuple(choices)


def _private_history_path(path: Path) -> Optional[Path]:
    """Create/verify an owner-only regular history file, or fail closed.

    Both readline and prompt_toolkit reopen history by pathname, so an unsafe
    target must never be handed to either library.  The containing directory is
    made private first; O_NOFOLLOW and inode comparison provide the strongest
    available protection on each supported platform.
    """
    parent = path.parent
    fd: Optional[int] = None
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            return None
        if hasattr(os, "geteuid") and parent_info.st_uid != os.geteuid():
            return None
        os.chmod(str(parent), 0o700, follow_symlinks=False)
        parent_after = parent.lstat()
        if (
            not stat.S_ISDIR(parent_after.st_mode)
            or not os.path.samestat(parent_info, parent_after)
            or stat.S_IMODE(parent_after.st_mode) != 0o700
        ):
            return None

        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and (
            stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
        ):
            return None

        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(str(path), flags, 0o600)
        fd_info = os.fstat(fd)
        path_info = path.lstat()
        parent_final = parent.lstat()
        if (
            not stat.S_ISREG(fd_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or not os.path.samestat(fd_info, path_info)
            or (before is not None and not os.path.samestat(before, fd_info))
            or not os.path.samestat(parent_after, parent_final)
        ):
            return None
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            return None
        return path
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _setup_readline() -> None:
    """Arrow-key history + line editing for the input() fallback path."""
    try:
        import readline
    except ImportError:
        return
    history_path = _private_history_path(_HISTORY_FILE)
    try:
        if history_path is not None:
            readline.read_history_file(str(history_path))
        readline.set_history_length(500)
        if history_path is not None:
            atexit.register(lambda: _save_history_silent(readline, history_path))
    except Exception:
        pass


def _save_history_silent(readline_mod, history_path: Optional[Path] = None) -> None:
    path = _private_history_path(history_path or _HISTORY_FILE)
    if path is None:
        return
    try:
        readline_mod.write_history_file(str(path))
        # Re-verify after readline reopened the pathname.  A changed/unsafe
        # target is never chmodded or reused on a later save.
        _private_history_path(path)
    except Exception:
        pass


def _interactive() -> bool:
    """True when we have a real terminal for prompt_toolkit / rich.Live."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _harden_repl_history() -> Optional[Path]:
    """Keep the prompt_toolkit history file owner-only (0o600), matching the
    readline history / state.json / settings.json. REPL prompts can contain
    secrets and must not be world-readable on a shared host.  ``None`` selects
    prompt_toolkit's in-memory history rather than exposing an unsafe path."""
    return _private_history_path(_PTK_HISTORY_FILE)


def _unlink_if_same_regular_file(path: Path, expected: os.stat_result) -> None:
    """Remove only the regular file represented by ``expected``."""
    try:
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and os.path.samestat(current, expected):
            path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _write_private_json_new(path: Path, payload: object) -> None:
    """Write JSON to a brand-new 0600 regular file without following links."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    created_info: Optional[os.stat_result] = None
    try:
        # O_EXCL makes target creation the no-overwrite atomic operation; on
        # POSIX it also refuses a final-component symlink even without
        # O_NOFOLLOW.  The latter remains useful defense in depth elsewhere.
        fd = os.open(str(path), flags, 0o600)
        created_info = os.fstat(fd)
        path_info = path.lstat()
        if (
            not stat.S_ISREG(created_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or not os.path.samestat(created_info, path_info)
        ):
            raise OSError("export target is not a regular file")
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise OSError("export target mode is not private")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created_info is not None:
            _unlink_if_same_regular_file(path, created_info)
        raise


# ---------- REPL entry ----------

def run_repl(
    *,
    provider: ProviderId = "claude",
    model: Optional[str] = None,
    web_search: bool = True,
    terse: bool = False,
    cwd: Optional[str] = None,
    continue_session: bool = False,
) -> int:
    if not _valid_explicit_provider_id(provider):
        _safe_print(t("cli.provider.invalid"), style="red")
        return 2
    extension_descriptor = None
    if provider not in PROVIDERS:
        try:
            # Resolve the exact explicit extension before any Core-only
            # DEFAULT_MODELS indexing.  This invokes metadata loading only.
            from .registry import snapshot_provider_descriptor

            extension_descriptor = snapshot_provider_descriptor(provider)
        except UnifiedError as exc:
            _print_unified_error(exc)
            return 2
        if model is not None:
            from .plugin import _valid_model_id as _valid_extension_model_id

            if not _valid_extension_model_id(model):
                _safe_print(t("cli.model.invalid_extension"), style="red")
                return 2
        if terse:
            _safe_print(
                t("repl.extension.terse_unsupported", provider=provider),
                style="red",
            )
            return 2

    saved_preferences = settings.load_settings()
    # If the user asked to start on the gated provider, fall back gracefully.
    if provider == "gemini" and not gemini_enabled():
        _safe_print(t("repl.gemini.locked"), style="yellow")
        provider = "claude"
        model = None

    explicit_cwd: Optional[str] = None
    if cwd is not None:
        explicit_cwd = resolve_cwd(cwd)
        if explicit_cwd is None:
            _safe_print(t("cli.chat.invalid_cwd", cwd=cwd), style="red")
            return 2
    # Always retain the effective directory so state.json can restore the exact
    # tool workspace on --continue or /resume. This is equivalent to inherited
    # subprocess cwd when the caller did not provide --cwd.
    saved_cwd = None
    if cwd is None and saved_preferences.workspace:
        saved_cwd = resolve_cwd(saved_preferences.workspace)
    effective_cwd = explicit_cwd or saved_cwd or resolve_cwd(os.getcwd()) or os.getcwd()
    effective_web = web_search
    # An explicit --no-web-search remains authoritative.  The saved preference
    # only replaces the CLI's ordinary True default.
    if web_search and saved_preferences.web is not None:
        effective_web = saved_preferences.web
    provider_opts: dict = {"cwd": effective_cwd}
    if provider in PROVIDERS:
        # Keep Core's established default.  Ext never receives the Core-only
        # web_search constructor option; explicit requests fail closed before
        # a turn instead.
        provider_opts["web_search"] = effective_web
    if saved_preferences.timeout is not None:
        provider_opts["timeout"] = saved_preferences.timeout
    if terse:
        provider_opts["terse"] = True

    conv = UnifiedConversation(
        default_provider=provider,
        default_model=model,
        sticky=False,
        provider_opts=provider_opts,
    )

    if model is not None:
        initial_model = model
    elif extension_descriptor is not None:
        initial_model = extension_descriptor.default_model or "default"
    else:
        initial_model = DEFAULT_MODELS[provider]  # type: ignore[index]
    current = {"provider": provider, "model": initial_model}
    pending_images: list[str] = []

    repl_state = ReplState.from_legacy(
        current, provider_opts, pending_images, context_window=conv.context_window
    )
    # Ext omits the Core-only constructor keyword, but the local state still
    # records the explicit CLI choice so toolbar display and later Core
    # switches retain --no-web-search accurately.
    repl_state.web_search = effective_web
    if extension_descriptor is not None:
        repl_state.remember_extension_descriptor(extension_descriptor)
    repl_state.sync_legacy(current, provider_opts)

    if continue_session:
        _apply_resume(
            conv, current, provider_opts, preserve_cwd=explicit_cwd is not None,
            repl_state=repl_state,
        )
        repl_state.provider = str(current["provider"])
        repl_state.model = str(current["model"])
        repl_state.cwd = str(provider_opts.get("cwd") or repl_state.cwd)
    # False can only arrive through the explicit --no-web-search CLI flag;
    # the ordinary True default remains provider-managed unless saved.web is set.
    repl_state.web_explicit = not web_search
    _apply_saved_preferences(repl_state, conv, saved_preferences)
    repl_state.sync_legacy(current, provider_opts)

    # Input driver: prompt_toolkit (live menu) when interactive, else readline.
    use_ptk = has_prompt_toolkit() and _interactive()
    session = None
    if use_ptk:
        session = build_session(_harden_repl_history(), current)
    else:
        _setup_readline()

    _banner(current, use_ptk)

    while True:
        try:
            prompt_str = _prompt(current)
            line = session.prompt(prompt_str) if session else input(prompt_str)
        except EOFError:
            console.print()
            _on_exit(conv, current, provider_opts)
            return 0
        except KeyboardInterrupt:
            console.print()
            _safe_print(t("repl.interrupt_hint"), style="dim")
            continue

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            # Defense-in-depth: a bug in any slash handler must not tear down
            # the whole REPL (losing in-memory session state).
            try:
                stop = _handle_slash(line, conv, current, provider_opts,
                                     pending_images, use_ptk,
                                     preserve_cwd=explicit_cwd is not None,
                                     repl_state=repl_state)
            except KeyboardInterrupt:
                console.print()
                continue
            except Exception:  # noqa: BLE001 - degrade, don't crash
                # Do not reflect raw exception text: provider/extension errors
                # can contain stderr, tokens, markup, or terminal controls.
                _safe_print(t("repl.slash_error.safe"), style="red")
                continue
            if stop:
                _on_exit(conv, current, provider_opts)
                return 0
            continue

        imgs = pending_images[:] if pending_images else None
        pending_images.clear()
        # Defense-in-depth (same as the slash path): an unexpected turn error
        # (e.g. the CLI binary removed mid-session, or invalid UTF-8 from the
        # child) must not tear down the REPL and lose in-memory history.
        try:
            _run_turn(conv, current, line, images=imgs, repl_state=repl_state)
        except Exception:  # noqa: BLE001 - degrade, don't crash
            _safe_print(t("repl.turn.unexpected"), style="red")


# ---------- slash commands ----------

def _apply_resume(
    conv: UnifiedConversation,
    current: dict,
    provider_opts: Optional[dict] = None,
    *,
    preserve_cwd: bool = False,
    repl_state: Optional[ReplState] = None,
) -> bool:
    """Load the last saved session and seed the conversation so the NEXT turn
    resumes it natively. Returns True on success.

    A placeholder Turn is appended so UnifiedConversation._use_native_session
    (which requires the previous turn to be the same provider) engages on the
    first real turn. The gemini gate is honored.
    """
    from .conversation import Turn
    from .state import load_last_session

    saved = load_last_session()
    if saved is None:
        _safe_print(t("repl.resume.none"), style="yellow")
        return False
    if saved.provider == "gemini" and not gemini_enabled():
        _safe_print(t("repl.gemini.locked"), style="yellow")
        return False
    if saved.provider not in PROVIDERS:
        descriptor = None
        if repl_state is not None:
            descriptor = repl_state.loaded_extension_descriptors.get(saved.provider)
        if descriptor is None:
            try:
                from .registry import snapshot_provider_descriptor

                descriptor = snapshot_provider_descriptor(saved.provider)
            except UnifiedError as exc:
                _print_unified_error(exc)
                return False
        if "sessions" not in descriptor.capabilities:
            _safe_print(
                t(
                    "repl.extension.capability_required",
                    provider=saved.provider,
                    capability="sessions",
                ),
                style="red",
            )
            return False
        from .plugin import _valid_model_id as _valid_extension_model_id

        if not _valid_extension_model_id(saved.model):
            _safe_print(t("cli.model.invalid_extension"), style="red")
            return False
        if repl_state is not None:
            repl_state.remember_extension_descriptor(descriptor)

    current["provider"] = saved.provider
    current["model"] = saved.model
    conv.sessions[saved.provider] = saved.session_id
    conv.turns.append(Turn(provider=saved.provider, prompt="", text=""))
    if provider_opts is not None and not preserve_cwd and saved.cwd:
        restored_cwd = resolve_cwd(saved.cwd)
        if restored_cwd is not None:
            provider_opts["cwd"] = restored_cwd
            # `/resume` can happen after an earlier provider call. Recreate
            # cached clients so the resumed session cannot retain that old
            # call's workspace.
            conv._clients.clear()
        else:
            _safe_print(t("cli.chat.saved_cwd_missing", cwd=saved.cwd), style="yellow")
    age_min = int(saved.age_seconds // 60)
    _safe_print(
        t(
            "repl.resume.done", provider=saved.provider,
            model=_short(saved.model), sid=saved.session_id[:12], age=age_min,
        ),
        style="green",
    )
    return True


def _switch_provider(
    current: dict, new_provider: str, repl_state: Optional[ReplState] = None
) -> bool:
    """Switch an explicitly named provider, preserving the Gemini gate.

    Built-ins stay on a zero-discovery path.  Only an explicit unknown id can
    trigger extension metadata lookup and loading.
    """
    if not _valid_explicit_provider_id(new_provider):
        _safe_print(t("cli.provider.invalid"), style="red")
        return False
    default_model: Optional[str] = None
    if new_provider in PROVIDERS:
        default_model = DEFAULT_MODELS[new_provider]  # type: ignore[index]
    else:
        try:
            descriptor = (
                repl_state.loaded_extension_descriptors.get(new_provider)
                if repl_state is not None else None
            )
            if descriptor is None:
                from .registry import snapshot_provider_descriptor

                descriptor = snapshot_provider_descriptor(new_provider)
            default_model = descriptor.default_model
            if repl_state is not None:
                repl_state.remember_extension_descriptor(descriptor)
        except UnifiedError as exc:
            _print_unified_error(exc)
            return False
        except Exception:  # noqa: BLE001 - extension boundary
            _safe_print(t("repl.provider.extension_failed", provider=new_provider), style="red")
            return False
    if new_provider == "gemini" and not gemini_enabled():
        _safe_print(t("repl.gemini.locked"), style="yellow")
        return False
    old = current["provider"]
    if new_provider == old:
        return False
    current["provider"] = new_provider
    current["model"] = default_model or "default"
    if repl_state is not None:
        repl_state.provider = new_provider
        repl_state.model = current["model"]
    _safe_print(t("repl.provider.switched", old=old, new=new_provider), style="dim")
    return True


def _handle_slash(
    line: str,
    conv: UnifiedConversation,
    current: dict,
    provider_opts: dict,
    pending_images: list[str],
    use_ptk: bool,
    *,
    preserve_cwd: bool = False,
    repl_state: Optional[ReplState] = None,
) -> bool:
    """Dispatch one slash command. Return True if the REPL should exit.

    This compatibility signature is intentionally retained.  New callers can
    pass a durable ``ReplState``; old callers get a state reconstructed from
    the dictionaries and synced back before return.
    """
    state = repl_state or ReplState.from_legacy(
        current, provider_opts, pending_images, context_window=conv.context_window
    )
    context = _SlashContext(
        conv=conv,
        current=current,
        provider_opts=provider_opts,
        state=state,
        use_ptk=use_ptk,
        preserve_cwd=preserve_cwd,
    )
    result = DEFAULT_REGISTRY.dispatch(line, context.execute)
    state.sync_legacy(current, provider_opts)
    if not result.handled:
        command = line.strip().split(None, 1)[0]
        _safe_print(t("repl.unknown.detail", cmd=command), style="red")
        return False
    return result.exit_requested


class _SlashContext:
    def __init__(
        self,
        *,
        conv: UnifiedConversation,
        current: dict,
        provider_opts: dict,
        state: ReplState,
        use_ptk: bool,
        preserve_cwd: bool,
    ):
        self.conv = conv
        self.current = current
        self.provider_opts = provider_opts
        self.state = state
        self.use_ptk = use_ptk
        self.preserve_cwd = preserve_cwd

    def execute(self, spec: CommandSpec, invoked: str, argument: str) -> bool:
        del invoked
        cmd = spec.name
        if cmd == "/exit":
            return True
        if cmd == "/help":
            _print_help(self.current)
        elif cmd == "/provider":
            self._provider(argument)
        elif cmd == "/model":
            self._model(argument)
        elif cmd == "/auth":
            self._auth(argument)
        elif cmd == "/doctor":
            self._doctor()
        elif cmd == "/status":
            _print_repl_status_snapshot(self.state, self.conv)
        elif cmd == "/settings":
            self._settings()
        elif cmd == "/style":
            self._style(argument)
        elif cmd == "/effort":
            self._effort(argument)
        elif cmd == "/reasoning":
            self._toggle("reasoning", argument)
        elif cmd == "/context":
            self._context(argument)
        elif cmd == "/system":
            self._system(argument)
        elif cmd == "/timeout":
            self._timeout(argument)
        elif cmd == "/permissions":
            self._permissions(argument)
        elif cmd == "/tools":
            _unavailable("/tools", "repl.unavail.tools.reason", "repl.unavail.tools.alt")
        elif cmd == "/mcp":
            _unavailable("/mcp", "repl.unavail.mcp.reason", "repl.unavail.mcp.alt")
        elif cmd == "/web":
            self._web(argument)
        elif cmd == "/cwd":
            self._cwd(argument)
        elif cmd == "/add-dir":
            self._add_dir(argument)
        elif cmd == "/sessions":
            self._sessions()
        elif cmd == "/rename":
            self._rename(argument)
        elif cmd == "/fork":
            _unavailable("/fork", "repl.unavail.fork.reason", "repl.unavail.fork.alt")
        elif cmd == "/compact":
            _unavailable("/compact", "repl.unavail.compact.reason", "repl.unavail.compact.alt")
        elif cmd == "/export":
            self._export(argument)
        elif cmd == "/copy":
            _unavailable("/copy", "repl.unavail.copy.reason", "repl.unavail.copy.alt")
        elif cmd in ("/clear", "/new"):
            _reset_conversation(self.conv)
            _safe_print(t("repl.new.done"), style="dim")
        elif cmd == "/resume":
            _apply_resume(
                self.conv, self.current, self.provider_opts,
                preserve_cwd=self.preserve_cwd,
                repl_state=self.state,
            )
            self.state.provider = self.current["provider"]
            self.state.model = self.current["model"]
            self.state.cwd = str(self.provider_opts.get("cwd") or self.state.cwd)
        elif cmd == "/save":
            self._save()
        elif cmd == "/history":
            self._history(argument)
        elif cmd == "/image":
            self._image(argument)
        elif cmd == "/images":
            self._images()
        elif cmd == "/clear-images":
            count = len(self.state.pending_images)
            self.state.pending_images.clear()
            _safe_print(t("repl.images.cleared", n=count), style="dim")
        elif cmd in ("/review", "/init"):
            safe_prompt = t(
                "repl.unavail.review.prompt" if cmd == "/review"
                else "repl.unavail.init.prompt"
            )
            _unavailable(
                cmd,
                "repl.unavail.quick.reason", "repl.unavail.quick.alt", prompt=safe_prompt,
            )
        elif cmd == "/diff":
            self._diff()
        elif cmd == "/theme":
            self._theme(argument)
        elif cmd == "/multiline":
            self._toggle("multiline", argument)
        elif cmd == "/usage":
            _print_usage()
        elif cmd == "/lang":
            self._lang(argument)
        return False

    def _provider(self, argument: str) -> None:
        if argument:
            # Provider ids never contain spaces.  Extra tokens are rejected so
            # an extension is loaded only for the exact id the user typed.
            pieces = argument.split()
            if len(pieces) != 1:
                _safe_print(t("repl.provider.usage"), style="red")
                return
            _switch_provider(self.current, pieces[0], self.state)
            return
        if self.use_ptk:
            loaded = list(self.state.loaded_extension_descriptors)
            chosen = (
                pick_provider_from_snapshots(loaded) if loaded
                else pick_provider()
            )
            if chosen:
                _switch_provider(self.current, chosen, self.state)
        else:
            _safe_print(t("repl.provider.usage"), style="dim")

    def _model(self, argument: str) -> None:
        provider = self.state.provider
        if argument == "--refresh":
            try:
                if provider in PROVIDERS:
                    from .models import list_models

                    choices = list_models(provider, force_refresh=True)
                    _replace_core_model_choices(
                        self.current, provider, choices,
                    )
                else:
                    from .registry import list_extension_models

                    # Exactly one explicit lister call.  Do not discard the
                    # last-good snapshot unless this call fully succeeds.
                    choices = list_extension_models(provider)
            except UnifiedError as exc:
                _print_unified_error(exc)
                return
            except Exception:  # noqa: BLE001 - provider/list boundary
                _safe_print(t("repl.model.refresh_failed"), style="red")
                return
            if provider not in PROVIDERS:
                self.state.replace_extension_models(provider, choices)
                # An empty successful refresh retains the descriptor default,
                # so display the committed snapshot rather than the raw empty
                # callback result.
                choices = list(self.state.extension_models(provider))
            _safe_print(t("repl.model.refreshed", count=len(choices)), style="dim")
            _print_model_list(provider, choices=choices)
            return
        if argument:
            valid_model = _valid_model_id(argument)
            if provider not in PROVIDERS:
                from .plugin import _valid_model_id as _valid_extension_model_id

                valid_model = _valid_extension_model_id(argument)
            if not valid_model:
                _safe_print(t("repl.value.invalid", name="model"), style="red")
                return
            self.current["model"] = argument
            self.state.model = argument
            if provider in PROVIDERS:
                known = {
                    model.id
                    for model in _core_model_choices(self.current, provider)
                }
            else:
                known = {
                    model.id for model in self.state.extension_models(provider)
                }
            if known and argument not in known:
                _safe_print(t("repl.model.unknown", model=argument), style="yellow")
            _safe_print(t("repl.model.changed", model=argument), style="dim")
            return
        if provider not in PROVIDERS:
            choices = list(self.state.extension_models(provider))
            if not choices:
                _safe_print(t("repl.model.extension_hint"), style="dim")
                return
            if self.use_ptk:
                chosen = pick_model(provider, choices=choices)
                if chosen:
                    self.current["model"] = chosen
                    self.state.model = chosen
                    _safe_print(t("repl.model.changed", model=chosen), style="dim")
                else:
                    _safe_print(t("repl.model.cancelled"), style="dim")
            else:
                _print_model_list(provider, choices=choices)
            return
        if self.use_ptk:
            core_choices = _core_model_choices(self.current, provider)
            chosen = pick_model(provider, choices=core_choices)
            if chosen:
                self.current["model"] = chosen
                self.state.model = chosen
                _safe_print(t("repl.model.changed", model=chosen), style="dim")
            else:
                _safe_print(t("repl.model.cancelled"), style="dim")
        else:
            _print_model_list(
                provider,
                choices=_core_model_choices(self.current, provider),
            )

    def _auth(self, argument: str) -> None:
        parts = argument.split()
        if len(parts) != 2:
            _safe_print(t("repl.auth.usage"), style="red")
            return
        provider, action = parts
        if provider in {"status", "login", "logout"}:
            action, provider = provider, action
        if provider == "gemini":
            _unavailable(
                "/auth gemini",
                "repl.auth.gemini.reason", "repl.auth.gemini.alt",
            )
            return
        if provider not in PROVIDERS:
            descriptor = self.state.loaded_extension_descriptors.get(provider)
            if descriptor is None or "auth" not in descriptor.capabilities:
                _unavailable(
                    "/auth " + action + " " + provider,
                    "repl.auth.extension.unsupported.reason",
                    "repl.auth.extension.unsupported.alt",
                )
            else:
                _unavailable(
                    "/auth " + action + " " + provider,
                    "repl.auth.extension.config_v1.reason",
                    "repl.auth.extension.config_v1.alt",
                )
            return
        if action in {"status", "login", "logout"} and provider not in CORE_AUTH_SPECS:
            _unavailable(
                "/auth " + action + " " + provider,
                "repl.auth.untrusted.reason", "repl.auth.untrusted.alt",
            )
            return
        if action not in CORE_AUTH_SPECS.get(provider, {}):
            _safe_print(t("repl.auth.usage"), style="red")
            return
        _run_core_auth(provider, action)

    def _doctor(self) -> None:
        provider = self.state.provider
        if provider not in PROVIDERS:
            try:
                from .registry import doctor_provider

                # The extension return value is intentionally ignored.  Only
                # this explicit command invokes the doctor callback, and Core
                # renders a fixed generic outcome.
                doctor_provider(provider)
            except UnifiedError as exc:
                _print_unified_error(exc)
            except Exception:  # noqa: BLE001 - extension diagnostic boundary
                _safe_print(
                    t("repl.doctor.extension_failed", provider=provider),
                    style="red",
                )
            else:
                _safe_print(
                    t("repl.doctor.extension_ok", provider=provider),
                    style="green",
                )
            return

        # Preserve the historical Core doctor table, but only on a Core
        # selection. Ext diagnostics must not probe unrelated Core providers.
        from .ui import _HEALTH_STYLE, collect_states
        for state in collect_states():
            icon, color, label_key = _HEALTH_STYLE.get(state.health, ("•", "dim", None))
            label = t(label_key) if label_key else state.health
            console.print(Text("  " + icon + " " + state.name + ": " + label, style=color))

    def _settings(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="cyan")
        table.add_column()
        for key, value in self.state.summary().items():
            table.add_row(
                Text(t("repl.settings.label." + key)),
                Text(safe_terminal_text(_localized_setting_value(key, value, self.state))),
            )
        console.print(table)

    def _style(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.status.value", name="style", value=self.state.style), style="dim")
            return
        value = argument.lower()
        if value not in _STYLE_VALUES:
            _safe_print(t("repl.value.invalid", name="style"), style="red")
            return
        if value == "default":
            self.state.style = value
            _persist_setting("style", None)
            self._refresh_capability_options()
            _safe_print(t("repl.status.value", name="style", value="default"), style="dim")
            return
        if self.state.provider != "codex":
            _unavailable(
                "/style", "repl.unavail.style.reason", "repl.unavail.style.alt",
            )
            return
        self.state.style = value
        _persist_setting("style", value)
        self._refresh_capability_options()
        _safe_print(t("repl.status.value", name="style", value=value), style="dim")

    def _effort(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.status.value", name="effort", value=self.state.effort), style="dim")
            return
        value = argument.lower()
        if value not in _EFFORT_VALUES:
            _safe_print(t("repl.value.invalid", name="effort"), style="red")
            return
        if value == "default":
            self.state.effort = value
            _persist_setting("effort", None)
            self._refresh_capability_options()
            _safe_print(t("repl.status.value", name="effort", value="default"), style="dim")
            return
        if self.state.provider not in {"claude", "codex"}:
            _unavailable(
                "/effort", "repl.unavail.effort.reason", "repl.unavail.effort.alt",
            )
            return
        self.state.effort = value
        _persist_setting("effort", value)
        self._refresh_capability_options()
        _safe_print(t("repl.status.value", name="effort", value=value), style="dim")

    def _system(self, argument: str) -> None:
        if not argument:
            _print_system_status(self.state.system_prompt)
            return
        if argument.lower() in {"default", "clear"}:
            self.state.system_prompt = None
            _persist_setting("system_prompt", None)
            self._refresh_capability_options()
            _print_system_status(None)
            return
        if self.state.provider not in {"claude", "codex"}:
            _unavailable(
                "/system", "repl.unavail.system.reason", "repl.unavail.system.alt",
            )
            return
        value: Optional[str] = argument
        if not _valid_system_prompt(value):
            _safe_print(t("repl.value.invalid", name="system"), style="red")
            return
        self.state.system_prompt = value
        _persist_setting("system_prompt", value)
        self._refresh_capability_options()
        _print_system_status(value)

    def _refresh_capability_options(self) -> None:
        _apply_provider_capabilities(self.state, self.conv)
        _clear_cached_clients(self.conv)

    def _toggle(self, name: str, argument: str) -> None:
        attr = {
            "reasoning": "reasoning_summaries",
            "multiline": "multiline",
        }[name]
        if not argument:
            value = bool(getattr(self.state, attr))
            _safe_print(t("repl.status.value", name=name, value="on" if value else "off"), style="dim")
            return
        normalized = argument.lower()
        valid = {"on", "off"}
        if name == "reasoning":
            valid |= {"hidden", "summary"}
        if normalized not in valid:
            _safe_print(t("repl.toggle.usage", name=name), style="red")
            return
        value = normalized in {"on", "summary"}
        setattr(self.state, attr, value)
        if name == "reasoning":
            _persist_setting("reasoning_display", "compact" if value else "hidden")
        elif name == "multiline":
            _persist_setting("multiline", value)
        display = ("summary" if value else "hidden") if name == "reasoning" else normalized
        _safe_print(t("repl.status.value", name=name, value=display), style="dim")

    def _web(self, argument: str) -> None:
        if not argument:
            value = (
                ("on" if self.state.web_search else "off")
                if self.state.web_explicit else "default"
            )
            _safe_print(t("repl.status.value", name="web", value=value), style="dim")
            return
        value = argument.lower()
        if value == "default":
            self.state.web_search = True
            self.state.web_explicit = False
            self.provider_opts["web_search"] = True
            _persist_setting("web", None)
            _clear_cached_clients(self.conv)
            _safe_print(t("repl.status.value", name="web", value="default"), style="dim")
            return
        if value not in {"on", "off"}:
            _safe_print(t("repl.toggle.usage", name="web"), style="red")
            return
        if self.state.provider not in {"claude", "codex"}:
            _unavailable(
                "/web", "repl.unavail.web.reason", "repl.unavail.web.alt",
            )
            return
        enabled = value == "on"
        self.state.web_search = enabled
        self.state.web_explicit = True
        self.provider_opts["web_search"] = enabled
        _persist_setting("web", enabled)
        _clear_cached_clients(self.conv)
        _safe_print(t("repl.status.value", name="web", value=value), style="dim")

    def _context(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.context.status", value=self.state.context_window), style="dim")
            return
        try:
            value = int(argument)
        except ValueError:
            value = 0
        if value < 1 or value > 100:
            _safe_print(t("repl.context.usage"), style="red")
            return
        self.state.context_window = value
        self.conv.context_window = value
        _persist_setting("context_window", value)
        _safe_print(t("repl.context.changed", value=value), style="dim")

    def _timeout(self, argument: str) -> None:
        if not argument:
            value = "default" if self.state.timeout is None else str(self.state.timeout)
            _safe_print(t("repl.status.value", name="timeout", value=value), style="dim")
            return
        if argument.lower() == "default":
            self.state.timeout = None
            self.provider_opts.pop("timeout", None)
        else:
            try:
                value = float(argument)
            except ValueError:
                value = 0
            if value < 1 or value > 3600:
                _safe_print(t("repl.timeout.usage"), style="red")
                return
            self.state.timeout = value
            self.provider_opts["timeout"] = value
        _persist_setting("timeout", self.state.timeout)
        self.conv._clients.clear()  # type: ignore[attr-defined]
        _safe_print(t("repl.timeout.changed"), style="dim")

    def _permissions(self, argument: str) -> None:
        if not argument:
            _safe_print(
                t("repl.permissions.current", value=self.state.permission_mode,
                  choices=", ".join(_PERMISSION_VALUES)),
                style="dim",
            )
            return
        value = argument.lower()
        if value not in _PERMISSION_VALUES:
            _safe_print(t("repl.value.invalid", name="permissions"), style="red")
            return
        broadening = (
            value in {"provider_default", "workspace_write"}
            and self.state.permission_mode != value
        )
        if broadening:
            if not _interactive():
                _unavailable(
                    "/permissions", "repl.unavail.permissions.confirm.reason",
                    "repl.unavail.permissions.confirm.alt",
                )
                return
            if not _confirm_action(
                t("repl.permissions.confirm", old=self.state.permission_mode, new=value)
            ):
                _safe_print(t("repl.permissions.unchanged"), style="dim")
                return
        self.state.permission_mode = value
        _persist_setting("repl_permission", value)
        self._refresh_capability_options()
        _safe_print(t("repl.status.value", name="permissions", value=value), style="dim")

    def _cwd(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.status.value", name="cwd", value=self.state.cwd), style="dim")
            return
        resolved = resolve_cwd(argument)
        if resolved is None:
            _safe_print(t("cli.chat.invalid_cwd", cwd=argument), style="red")
            return
        self.state.cwd = resolved
        self.provider_opts["cwd"] = resolved
        self.conv._clients.clear()  # type: ignore[attr-defined]
        _persist_setting("workspace", resolved)
        _safe_print(t("repl.cwd.changed", cwd=resolved), style="dim")

    def _add_dir(self, argument: str) -> None:
        if not argument:
            if not self.state.added_dirs:
                _safe_print(t("repl.add_dir.none"), style="dim")
                return
            _safe_print(
                t("repl.add_dir.list", count=len(self.state.added_dirs)),
                style="dim",
            )
            for index, path in enumerate(self.state.added_dirs[:32], 1):
                _safe_print(t("repl.add_dir.item", index=index, path=path), style="dim")
            return
        if argument.lower() == "clear":
            self.state.added_dirs.clear()
            _persist_setting("additional_dirs", [])
            self._refresh_capability_options()
            _safe_print(t("repl.add_dir.cleared"), style="dim")
            return
        if self.state.provider not in _CORE_DIR_PROVIDERS:
            _unavailable(
                "/add-dir", "repl.unavail.add_dir.reason", "repl.unavail.add_dir.alt",
            )
            return
        resolved = _canonical_directory(argument)
        if resolved is None:
            _safe_print(t("cli.chat.invalid_cwd", cwd=argument), style="red")
            return
        if resolved in self.state.added_dirs:
            _safe_print(t("repl.add_dir.exists"), style="dim")
            return
        if len(self.state.added_dirs) >= 32:
            _safe_print(t("repl.add_dir.limit"), style="red")
            return
        self.state.added_dirs.append(resolved)
        _persist_setting("additional_dirs", list(self.state.added_dirs))
        self._refresh_capability_options()
        _safe_print(t("repl.add_dir.added"), style="dim")

    def _sessions(self) -> None:
        manager = _load_session_manager()
        if manager is None:
            _unavailable("/sessions", "repl.unavail.sessions.reason", "repl.unavail.sessions.alt")
            return
        try:
            records = manager.list(include_archived=False)
        except Exception:  # noqa: BLE001 - optional persistence boundary
            _safe_print(t("repl.sessions.failed"), style="red")
            return
        if not records:
            _safe_print(t("repl.sessions.none"), style="dim")
            return
        table = Table(header_style="bold cyan")
        for key in ("provider", "name", "model", "session"):
            table.add_column(t("repl.sessions.col_" + key))
        for record in records[:100]:
            table.add_row(
                Text(safe_terminal_text(getattr(record, "provider", ""), max_chars=32)),
                Text(safe_terminal_text(getattr(record, "name", "") or "—", max_chars=48)),
                Text(safe_terminal_text(getattr(record, "model", "") or "—", max_chars=48)),
                Text(safe_terminal_text(getattr(record, "session_id", "")[:12]) + "…"),
            )
        console.print(table)

    def _rename(self, argument: str) -> None:
        if not argument or len(argument) > 100 or any(ch in argument for ch in "\r\n"):
            _safe_print(t("repl.rename.usage"), style="red")
            return
        sid = self.conv.sessions.get(self.state.provider)
        if not sid:
            _safe_print(t("repl.save.none"), style="yellow")
            return
        manager = _load_session_manager()
        if manager is None:
            _unavailable("/rename", "repl.unavail.sessions.reason", "repl.unavail.rename.alt")
            return
        try:
            if manager.get(provider=self.state.provider, session_id=sid) is None:
                manager.upsert(
                    provider=self.state.provider,
                    session_id=sid,
                    model=self.state.model,
                    cwd=self.state.cwd or None,
                )
            manager.rename(
                provider=self.state.provider, session_id=sid, name=argument
            )
        except Exception:  # noqa: BLE001 - optional persistence boundary
            _safe_print(t("repl.rename.failed"), style="red")
            return
        _safe_print(t("repl.rename.done", name=argument), style="dim")

    def _export(self, argument: str) -> None:
        base = Path(self.state.cwd or os.getcwd()).expanduser()
        target = Path(argument).expanduser() if argument else base / (
            "unified-cli-export-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
        )
        if not target.is_absolute():
            target = base / target
        payload = {
            "version": 1,
            "provider": self.state.provider,
            "model": self.state.model,
            "turns": [
                {
                    "provider": turn.provider,
                    "prompt": turn.prompt,
                    "reply": turn.text,
                    "timestamp": turn.timestamp,
                }
                for turn in self.conv.turns
            ],
        }
        try:
            _write_private_json_new(target, payload)
        except FileExistsError:
            _safe_print(t("repl.export.exists", path=str(target)), style="red")
            return
        except (OSError, TypeError, ValueError, UnicodeError):
            _safe_print(t("repl.export.failed"), style="red")
            return
        _safe_print(t("repl.export.done", path=str(target)), style="dim")

    def _diff(self) -> None:
        output, truncated, ok = _safe_git_diff(self.state.cwd or os.getcwd())
        if not ok:
            _unavailable("/diff", "repl.unavail.diff.reason", "repl.unavail.diff.alt")
            return
        if not output:
            _safe_print(t("repl.diff.none"), style="dim")
            return
        console.print(Text(safe_terminal_text(output, max_chars=65_536)))
        if truncated:
            _safe_print(t("repl.diff.truncated"), style="yellow")

    def _theme(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.status.value", name="theme", value=self.state.theme), style="dim")
            return
        value = argument.lower()
        if value not in {"auto", "dark", "light"}:
            _safe_print(t("repl.theme.usage"), style="red")
            return
        self.state.theme = value
        _persist_setting("theme", value)
        _safe_print(t("repl.theme.changed", value=value), style="dim")

    def _lang(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.lang.usage", lang=current_lang()), style="dim")
            return
        code = argument.lower()
        try:
            set_lang(code)
        except ValueError:
            _safe_print(t("repl.lang.unknown", lang=code), style="red")
            return
        try:
            settings.set("lang", code)
        except Exception:
            pass
        _safe_print(t("repl.lang.changed", lang=code), style="dim")
        _banner(self.current, self.use_ptk)

    def _save(self) -> None:
        sid = self.conv.sessions.get(self.state.provider)
        if not sid:
            _safe_print(t("repl.save.none"), style="yellow")
            return
        console.print(Panel(
            Text(t("repl.save.body", sid=safe_terminal_text(sid))),
            title=t("repl.save.title"), border_style="cyan",
        ))

    def _history(self, argument: str) -> None:
        limit = 10
        if argument:
            try:
                limit = max(1, min(100, int(argument)))
            except ValueError:
                _safe_print(t("repl.history.usage"), style="red")
                return
        turns = self.conv.turns[-limit:]
        if not turns:
            _safe_print(t("repl.history.none"), style="dim")
            return
        table = Table(show_lines=False, header_style="bold magenta")
        table.add_column("#", justify="right", style="dim")
        table.add_column(t("repl.history.col_provider"))
        table.add_column(t("repl.history.col_prompt"), overflow="ellipsis", max_width=30)
        table.add_column(t("repl.history.col_reply"), overflow="ellipsis", max_width=40)
        start = len(self.conv.turns) - len(turns) + 1
        for index, turn in enumerate(turns, start=start):
            table.add_row(
                str(index), Text(safe_terminal_text(turn.provider, max_chars=32)),
                Text(safe_terminal_text(turn.prompt)), Text(safe_terminal_text(turn.text)),
            )
        console.print(table)

    def _image(self, argument: str) -> None:
        if not argument:
            _safe_print(t("repl.image.usage"), style="red")
            return
        try:
            path = Path(argument).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            _safe_print(t("repl.image.not_found", path=argument), style="red")
            return
        if not path.is_file():
            _safe_print(t("repl.image.not_found", path=argument), style="red")
            return
        self.state.pending_images.append(str(path))
        _safe_print(t("repl.image.attached", n=len(self.state.pending_images)), style="dim")

    def _images(self) -> None:
        if not self.state.pending_images:
            _safe_print(t("repl.images.none"), style="dim")
            return
        for index, path in enumerate(self.state.pending_images, 1):
            _safe_print(t("repl.image.item", index=index, path=path))


def _safe_print(value: object, *, style: str = "") -> None:
    console.print(Text(safe_terminal_text(value), style=style))


def _localized_setting_value(key: str, value: object, state: ReplState) -> object:
    """Translate presentation values without coupling ``ReplState`` to i18n."""
    if key == "system":
        return t("repl.settings.value.default") if state.system_prompt is None else t(
            "repl.system.configured", chars=len(state.system_prompt)
        )
    if key == "reasoning":
        return t("repl.settings.value.public_summaries" if state.reasoning_summaries
                 else "repl.settings.value.hidden")
    if key in {"web", "timeout", "multiline"} and value in {"default", "on", "off"}:
        return t("repl.settings.value." + str(value))
    return value


def _apply_saved_preferences(
    state: ReplState, conv: UnifiedConversation, saved: settings.Settings
) -> None:
    """Apply local v2 preferences without probing or loading providers."""
    state.permission_mode = saved.repl_permission
    state.context_window = saved.context_window
    conv.context_window = saved.context_window
    conv.cross_provider_context = saved.cross_provider_context_enabled
    cli_web_explicit = state.web_explicit
    provider_opts = getattr(conv, "provider_opts", None)
    if saved.web is not None and not cli_web_explicit:
        state.web_search = saved.web
        if type(provider_opts) is dict and state.provider in PROVIDERS:
            provider_opts["web_search"] = saved.web
    state.web_explicit = cli_web_explicit or saved.web is not None
    if type(provider_opts) is dict:
        if state.provider in PROVIDERS:
            provider_opts.setdefault("web_search", state.web_search)
        else:
            # Never leak a Core-only tool setting into Ext construction.
            provider_opts.pop("web_search", None)
    state.style = saved.style or "default"
    state.effort = saved.effort or "default"
    state.system_prompt = saved.system_prompt
    # Even a persisted "full" display can only enable explicitly public
    # summaries. EventRenderer never renders ordinary reasoning events.
    state.reasoning_summaries = saved.reasoning_display != "hidden"
    state.theme = saved.theme
    state.added_dirs = list(saved.additional_dirs)
    state.multiline = saved.multiline
    _apply_provider_capabilities(state, conv)


def _apply_provider_capabilities(state: ReplState, conv: UnifiedConversation) -> None:
    """Translate preferences into isolated constructor options for each Core provider.

    This function only mutates option dictionaries.  It performs no provider
    discovery or construction, and always removes stale keys owned by these
    mappings before applying the current values.
    """
    mappings = getattr(conv, "provider_opts_by_provider", None)
    if type(mappings) is not dict:
        mappings = {}
        conv.provider_opts_by_provider = mappings

    claude = dict(mappings.get("claude", {}))
    for key in ("permission_mode", "effort", "system_prompt", "add_dirs"):
        claude.pop(key, None)
    if state.permission_mode == "read_only":
        claude["permission_mode"] = "plan"
    if state.effort in _EFFORT_VALUES[1:]:
        claude["effort"] = state.effort
    if state.system_prompt is not None:
        claude["system_prompt"] = state.system_prompt
    if state.added_dirs:
        claude["add_dirs"] = list(state.added_dirs)
    _replace_provider_options(mappings, "claude", claude)

    codex = dict(mappings.get("codex", {}))
    codex.pop("sandbox", None)
    codex.pop("add_dirs", None)
    raw_config = codex.get("config_overrides")
    config = dict(raw_config) if type(raw_config) is dict else {}
    for key in (
        "model_reasoning_effort", "personality", "developer_instructions",
        "sandbox_mode", "sandbox_workspace_write.writable_roots",
    ):
        config.pop(key, None)
    if state.permission_mode == "read_only":
        codex["sandbox"] = "read-only"
        config["sandbox_mode"] = "read-only"
    elif state.permission_mode == "workspace_write":
        codex["sandbox"] = "workspace-write"
        config["sandbox_mode"] = "workspace-write"
    if state.effort in _EFFORT_VALUES[1:]:
        config["model_reasoning_effort"] = state.effort
    if state.style in _STYLE_VALUES[1:]:
        config["personality"] = state.style
    if state.system_prompt is not None:
        config["developer_instructions"] = state.system_prompt
    if state.added_dirs:
        codex["add_dirs"] = list(state.added_dirs)
        config["sandbox_workspace_write.writable_roots"] = list(state.added_dirs)
    if config:
        codex["config_overrides"] = config
    else:
        codex.pop("config_overrides", None)
    _replace_provider_options(mappings, "codex", codex)

    gemini = dict(mappings.get("gemini", {}))
    gemini.pop("add_dirs", None)
    if state.added_dirs:
        gemini["add_dirs"] = list(state.added_dirs)
    _replace_provider_options(mappings, "gemini", gemini)


def _replace_provider_options(mappings: dict, provider: str, options: dict) -> None:
    if options:
        mappings[provider] = options
    else:
        mappings.pop(provider, None)


def _clear_cached_clients(conv: UnifiedConversation) -> None:
    clients = getattr(conv, "_clients", None)
    clear = getattr(clients, "clear", None)
    if callable(clear):
        clear()


def _valid_model_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 512:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    )


def _valid_system_prompt(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 32_768:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(
        (unicodedata.category(char).startswith("C") and char not in "\t\r\n")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    )


def _print_system_status(value: Optional[str]) -> None:
    if value is None:
        _safe_print(t("repl.system.default"), style="dim")
    else:
        _safe_print(t("repl.system.configured", chars=len(value)), style="dim")


def _canonical_directory(value: object) -> Optional[str]:
    if type(value) is not str or not value or len(value) > 4096:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(path) if path.is_dir() else None


def _turn_capabilities_supported(
    state: ReplState,
    provider: str,
    *,
    images: bool = False,
    resume: bool = False,
) -> bool:
    """Fail closed before provider construction when a request cannot be mapped."""
    if state.permission_mode not in _PERMISSION_VALUES:
        _unavailable(
            "/permissions", "repl.unavail.permission.invalid.reason",
            "repl.unavail.permission.invalid.alt",
        )
        return False
    if state.permission_mode == "read_only" and provider not in {"claude", "codex"}:
        _unavailable(
            "/permissions read_only",
            "repl.unavail.permission.read_only.reason",
            "repl.unavail.permission.read_only.alt",
        )
        return False
    if state.permission_mode == "workspace_write" and provider != "codex":
        _unavailable(
            "/permissions workspace_write",
            "repl.unavail.permission.workspace_write.reason",
            "repl.unavail.permission.workspace_write.alt",
        )
        return False

    if state.web_explicit and provider not in {"claude", "codex"}:
        _unavailable(
            "/web", "repl.unavail.web.reason", "repl.unavail.web.alt",
        )
        return False

    if state.effort not in _EFFORT_VALUES:
        _unavailable(
            "/effort", "repl.unavail.effort.invalid.reason", "repl.unavail.effort.invalid.alt",
        )
        return False
    if state.effort != "default" and provider not in {"claude", "codex"}:
        _unavailable(
            "/effort " + state.effort,
            "repl.unavail.effort.reason", "repl.unavail.effort.alt",
        )
        return False

    if state.style not in _STYLE_VALUES:
        _unavailable(
            "/style", "repl.unavail.style.invalid.reason", "repl.unavail.style.invalid.alt",
        )
        return False
    if state.style != "default" and provider != "codex":
        _unavailable(
            "/style " + state.style,
            "repl.unavail.style.reason", "repl.unavail.style.alt",
        )
        return False

    if state.system_prompt is not None:
        if not _valid_system_prompt(state.system_prompt):
            _unavailable(
                "/system", "repl.unavail.system.invalid.reason", "repl.unavail.system.invalid.alt",
            )
            return False
        if provider not in {"claude", "codex"}:
            _unavailable(
                "/system", "repl.unavail.system.reason", "repl.unavail.system.alt",
            )
            return False

    for path in state.added_dirs:
        if _canonical_directory(path) != path:
            _unavailable(
                "/add-dir", "repl.unavail.add_dir.invalid.reason", "repl.unavail.add_dir.invalid.alt",
            )
            return False
    if state.added_dirs and provider not in _CORE_DIR_PROVIDERS:
        _unavailable(
            "/add-dir", "repl.unavail.add_dir.reason", "repl.unavail.add_dir.alt",
        )
        return False
    if provider not in PROVIDERS and (images or resume):
        descriptor = state.loaded_extension_descriptors.get(provider)
        required = [
            capability
            for capability, needed in (("sessions", resume), ("images", images))
            if needed
        ]
        missing = next(
            (
                capability for capability in required
                if descriptor is None or capability not in descriptor.capabilities
            ),
            None,
        )
        if missing is not None:
            _safe_print(
                t(
                    "repl.extension.capability_required",
                    provider=provider,
                    capability=missing,
                ),
                style="red",
            )
            return False
    return True


def _persist_setting(key: str, value: object) -> bool:
    try:
        settings.set(key, value)
        return True
    except Exception:  # noqa: BLE001 - local persistence must not tear down REPL
        _safe_print(t("repl.settings.persist_failed"), style="yellow")
        return False


def _print_unified_error(error: UnifiedError) -> None:
    # ``cause`` can contain provider stderr.  REPL output uses only Core-owned
    # classified text and its recovery hint.
    _safe_print(t("repl.error", provider=error.provider, kind=error.kind, message=error.message), style="red")
    if error.hint:
        _safe_print(t("repl.error.hint", hint=error.hint), style="dim")


def _unavailable(command: str, reason_key: str, alternative_key: str, **kw: str) -> None:
    _safe_print(
        t("repl.unavailable", command=command, reason=t(reason_key),
          alternative=t(alternative_key, **kw)),
        style="yellow",
    )


def _reset_conversation(conv: UnifiedConversation) -> None:
    conv.turns.clear()
    conv.sessions.clear()
    conv._clients.clear()  # type: ignore[attr-defined]
    conv._locked_provider = None  # type: ignore[attr-defined]


def _print_usage() -> None:
    aggregates = tracker.aggregates()
    if not aggregates:
        _safe_print(t("repl.tokens.none"), style="dim")
        return
    table = Table(header_style="bold green")
    table.add_column(t("repl.usage.col_provider"), style="bold")
    table.add_column(t("repl.usage.col_calls"), justify="right")
    table.add_column(t("repl.usage.col_tokens"), justify="right")
    table.add_column(t("repl.usage.col_latency"), justify="right")
    for aggregate in aggregates:
        table.add_row(
            Text(safe_terminal_text(aggregate.provider, max_chars=32)),
            str(aggregate.calls),
            str(aggregate.input_tokens) + "/" + str(aggregate.output_tokens),
            t("repl.usage.latency", value=aggregate.avg_latency_ms),
        )
    console.print(table)


def _load_session_manager():
    """Lazy optional boundary for the separately shipped session index."""
    try:
        from .session_manager import SessionManager
        return SessionManager()
    except (ImportError, OSError, ValueError):
        return None


def _record_indexed_session(conv: UnifiedConversation, state: ReplState) -> None:
    sid = conv.sessions.get(state.provider)
    if not sid:
        return
    manager = _load_session_manager()
    if manager is None:
        return
    try:
        manager.upsert(
            provider=state.provider,
            session_id=sid,
            model=state.model,
            cwd=state.cwd or None,
        )
    except Exception:
        # The legacy state.json session remains authoritative and functional.
        pass


def _minimal_subprocess_env() -> dict[str, str]:
    """Allow only non-secret process/runtime variables for explicit helpers."""
    allowed = (
        "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "COLORTERM", "NO_COLOR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _confirm_action(prompt: str) -> bool:
    try:
        response = input(prompt + " [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().lower() in {"y", "yes"}


def _run_core_auth(provider: str, action: str) -> None:
    argv = CORE_AUTH_SPECS[provider][action]
    if action in {"login", "logout"}:
        if not _interactive():
            _unavailable(
                "/auth " + provider + " " + action,
                "repl.auth.confirm.required.reason",
                "repl.auth.confirm.required.alt", argv=" ".join(argv),
            )
            return
        if not _confirm_action(
            t("repl.auth.confirm", argv=" ".join(argv))
        ):
            _safe_print(t("repl.auth.cancelled"), style="dim")
            return
    env = _minimal_subprocess_env()
    try:
        if action == "status":
            returncode, output, truncated, timed_out = _run_fixed_process(
                argv, cwd=None, env=env, timeout=30, capture=True,
                merge_stderr=True,
            )
            if timed_out:
                _safe_print(t("repl.auth.timeout"), style="red")
                return
            if returncode == 0 and output.strip():
                console.print(Text(safe_terminal_text(output, max_chars=65_536)))
            if returncode == 0 and truncated:
                _safe_print(t("repl.output.truncated"), style="yellow")
        else:
            returncode, _output, _truncated, timed_out = _run_fixed_process(
                argv, cwd=None, env=env, timeout=300, capture=False,
            )
            if timed_out:
                _safe_print(t("repl.auth.timeout"), style="red")
                return
    except FileNotFoundError:
        _safe_print(t("repl.auth.binary_missing", provider=provider), style="red")
        return
    except OSError:
        _safe_print(t("repl.auth.failed"), style="red")
        return
    if returncode == 0:
        _safe_print(t("repl.auth.done", action=action, provider=provider), style="dim")
    else:
        code = returncode if returncode is not None else -1
        _safe_print(t("repl.auth.exit", code=code), style="red")


def _run_fixed_process(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    env: dict[str, str],
    timeout: float,
    capture: bool,
    merge_stderr: bool = False,
    limit: int = 65_536,
) -> tuple[Optional[int], str, bool, bool]:
    """Run fixed argv in a dedicated process group and always retire its tree."""
    import threading

    chunks: list[bytes] = []
    state = {"stored": 0, "truncated": False}
    stream_kwargs = {}
    if capture:
        stream_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        }
    process = subprocess.Popen(
        list(argv),
        shell=False,
        cwd=cwd,
        env=env,
        **stream_kwargs,
        **_popen_process_group_kwargs(),
    )

    reader = None
    if capture:
        def drain() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(16_384)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                remaining = limit - state["stored"]
                if remaining > 0:
                    kept = chunk[:remaining]
                    chunks.append(kept)
                    state["stored"] += len(kept)
                if len(chunk) > max(0, remaining):
                    state["truncated"] = True

        reader = threading.Thread(
            target=drain, name="uc-repl-bounded-output", daemon=True
        )
        reader.start()

    timed_out = False
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        # This also handles KeyboardInterrupt and unexpected wait/read errors.
        # The group is wrapper-owned, so descendants must not survive even if
        # the direct leader already exited.
        _terminate_process_tree(process, force_group=True)
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if reader is not None:
            reader.join(timeout=2)
    return (
        getattr(process, "returncode", None),
        b"".join(chunks).decode("utf-8", "replace"),
        bool(state["truncated"]),
        timed_out,
    )


def _bounded_process_output(
    argv: Sequence[str], *, cwd: str, env: dict[str, str], limit: int = 65_536
) -> tuple[str, bool, bool]:
    """Run fixed argv with bounded memory/output and a hard timeout."""
    try:
        returncode, output, truncated, timed_out = _run_fixed_process(
            argv, cwd=cwd, env=env, timeout=10, capture=True, limit=limit,
        )
    except OSError:
        return "", False, False
    if timed_out:
        return "", truncated, False
    return output, truncated, returncode == 0


def _safe_git_diff(cwd: str) -> tuple[str, bool, bool]:
    """Return fixed-argv diff plus untracked names; never run diff drivers."""
    env = _minimal_subprocess_env()
    env.update({
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    })
    git = (
        "git", "--no-pager",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=" + os.devnull,
    )
    diff, diff_truncated, diff_ok = _bounded_process_output(
        git + (
            "diff", "--no-ext-diff", "--no-textconv", "--unified=3", "--", ".",
        ),
        cwd=cwd,
        env=env,
    )
    if not diff_ok:
        return "", diff_truncated, False
    staged, staged_truncated, staged_ok = _bounded_process_output(
        git + (
            "diff", "--cached", "--no-ext-diff", "--no-textconv",
            "--unified=3", "--", ".",
        ),
        cwd=cwd,
        env=env,
    )
    if not staged_ok:
        return "", diff_truncated or staged_truncated, False
    if staged.strip():
        diff += ("\n" if diff else "") + "Staged changes:\n" + staged
    untracked, names_truncated, names_ok = _bounded_process_output(
        git + ("ls-files", "--others", "--exclude-standard", "--"),
        cwd=cwd,
        env=env,
        limit=16_384,
    )
    if not names_ok:
        return "", diff_truncated or staged_truncated or names_truncated, False
    if untracked.strip():
        diff += ("\n" if diff else "") + "Untracked files:\n" + untracked
    combined_truncated = diff_truncated or staged_truncated or names_truncated
    if len(diff) > 65_536:
        diff = diff[:65_536]
        combined_truncated = True
    return diff, combined_truncated, True


def _print_model_list(provider: str, *, choices: Optional[list] = None) -> None:
    """Fallback model display when prompt_toolkit isn't available."""
    if choices is None:
        cands = arg_candidates("/model", provider, "")
    else:
        cands = [
            (str(getattr(model, "id", "")), str(getattr(model, "display_name", "") or ""))
            for model in choices
            if getattr(model, "id", None)
        ]
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="cyan")
    tbl.add_column(style="dim")
    for mid, meta in cands:
        tbl.add_row(Text(safe_terminal_text(mid)), Text(safe_terminal_text(meta)))
    console.print(tbl)
    _safe_print(t("repl.model.usage"), style="dim")


def _print_help(current: dict) -> None:
    tbl = Table(show_header=True, box=None, padding=(0, 2),
                header_style="bold cyan")
    tbl.add_column(t("repl.help.col_cmd"), style="cyan bold")
    tbl.add_column(t("repl.help.col_desc"))
    for c in DEFAULT_REGISTRY.commands:
        name = f"{c.name} {c.arg_hint}".strip()
        if c.aliases:
            name += "  (" + ", ".join(c.aliases) + ")"
        tbl.add_row(Text(name), Text(t(c.desc_key)))
    console.print(tbl)
    _safe_print(
        t(
            "repl.help.footer",
            lang=current_lang(),
            provider=safe_terminal_text(current["provider"], max_chars=32),
            model=safe_terminal_text(_short(current["model"]), max_chars=64),
        ),
        style="dim",
    )


def _live_status() -> None:
    """Auto-refreshing status panel; Ctrl+C returns to the prompt."""
    if not _interactive():
        console.print(status_layout())
        return
    _safe_print(t("repl.status.hint"), style="dim")
    try:
        with Live(status_layout(), console=console, refresh_per_second=2,
                  screen=False) as live:
            while True:
                time.sleep(1.5)
                live.update(status_layout())
    except KeyboardInterrupt:
        pass


def _print_repl_status_snapshot(state: ReplState, conv: UnifiedConversation) -> None:
    """Render process-local REPL state without probing any provider.

    The global CLI status command keeps its established discovery behavior.
    REPL `/status` is deliberately a point-in-time view of already-held state,
    session memory, usage counters, and loaded immutable Ext metadata only.
    """

    rows = [
        ("provider", state.provider),
        ("model", state.model),
        ("cwd", state.cwd),
        ("permission", state.permission_mode),
        (
            "web",
            ("on" if state.web_search else "off")
            if state.web_explicit else "default",
        ),
        ("context", state.context_window),
    ]
    sessions = getattr(conv, "sessions", {})
    session_id = sessions.get(state.provider) if type(sessions) is dict else None
    rows.append(("session", "active" if session_id else "none"))
    totals = sum(
        aggregate.input_tokens + aggregate.output_tokens
        for aggregate in tracker.aggregates()
    )
    rows.append(("tokens", totals))
    descriptor = state.loaded_extension_descriptors.get(state.provider)
    if descriptor is not None:
        rows.extend((
            ("support", descriptor.support_status),
            (
                "capabilities",
                ", ".join(sorted(descriptor.capabilities)) or "none",
            ),
        ))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column()
    for key, value in rows:
        display = _localized_status_value(key, value)
        table.add_row(
            Text(t("repl.status.label." + key)),
            Text(safe_terminal_text(display)),
        )
    console.print(table)


def _localized_status_value(key: str, value: object) -> object:
    if key == "web" and value in {"default", "on", "off"}:
        return t("repl.settings.value." + str(value))
    if key == "session" and value in {"active", "none"}:
        return t("repl.status.value." + str(value))
    if key == "capabilities" and value == "none":
        return t("repl.status.value.none")
    return value


# ---------- turn execution ----------

def _run_turn(
    conv: UnifiedConversation,
    current: dict,
    prompt: str,
    *,
    images: Optional[list] = None,
    repl_state: Optional[ReplState] = None,
) -> None:
    state = repl_state or ReplState.from_legacy(
        current,
        getattr(conv, "provider_opts", None),
        context_window=getattr(conv, "context_window", 8),
    )
    provider = str(current.get("provider") or state.provider)
    state.provider = provider
    state.model = str(current.get("model") or state.model)
    turns = getattr(conv, "turns", ())
    sessions = getattr(conv, "sessions", {})
    resuming = bool(
        turns
        and getattr(turns[-1], "provider", None) == provider
        and type(sessions) is dict
        and sessions.get(provider)
    )
    if not _turn_capabilities_supported(
        state, provider, images=bool(images), resume=resuming
    ):
        return
    _apply_provider_capabilities(state, conv)

    status = Status(f"[cyan]{t('repl.turn.waiting')}[/cyan]", console=console, spinner="dots")
    status.start()
    renderer = EventRenderer(
        console,
        show_reasoning_summaries=bool(
            state.reasoning_summaries
        ),
    )
    stream = None
    try:
        stream = conv.stream(
            prompt,
            provider=current["provider"],
            model=current["model"],
            images=images,
        )
        for msg in stream:
            was_started = renderer.text_started
            if msg.kind in {"text", "tool_use", "tool_result", "error"}:
                if not was_started:
                    status.stop()
            renderer.render(msg)
    except KeyboardInterrupt:
        status.stop()
        # Closing the conversation generator propagates GeneratorExit through
        # BaseProvider.stream(), whose finally block terminates the child
        # process tree and removes temporary attachments.
        if stream is not None:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        _safe_print(t("repl.turn.cancelled"), style="yellow")
        return
    except UnifiedError as e:
        status.stop()
        _print_unified_error(e)
        if e.kind == "not_found":
            # Stale/expired native session (e.g. a resumed session the CLI has
            # since dropped) — clear it so the next turn starts fresh instead of
            # failing the same way again.
            conv.sessions.pop(current["provider"], None)
            _safe_print(t("repl.turn.session_reset"), style="dim")
        return
    finally:
        status.stop()
        if stream is not None:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        # Always close an open assistant line, including when an unexpected
        # iterator/extension exception propagates to the outer REPL guard.
        renderer.finish()
        if repl_state is not None:
            _record_indexed_session(conv, repl_state)
    if repl_state is not None:
        recent = tracker.recent(1)
        if recent:
            repl_state.last_latency_ms = recent[0].latency_ms
            current["last_latency_ms"] = repl_state.last_latency_ms


# ---------- helpers ----------

def _prompt(current: dict) -> str:
    provider = safe_terminal_text(current.get("provider", "?"), max_chars=32)
    model = safe_terminal_text(_short(str(current.get("model", "?"))), max_chars=64)
    return "[" + provider + "/" + model + "] > "


def _short(model: str) -> str:
    if model.startswith("claude-"):
        return model.split("-")[1] if "-" in model[7:] else model
    return model


def _banner(current: dict, use_ptk: bool) -> None:
    # The live "/" menu only exists on the prompt_toolkit path.
    hint = t("repl.banner.hint") if use_ptk else t("repl.banner.hint_basic")
    body = Text()
    body.append(t("repl.banner.title"), style="bold")
    body.append("\n" + hint, style="dim")
    body.append(
        "\n" + t(
            "repl.banner.start",
            provider=safe_terminal_text(current["provider"], max_chars=32),
            model=safe_terminal_text(current["model"], max_chars=128),
        ),
        style="dim",
    )
    console.print(Panel.fit(body, border_style="cyan"))


def _on_exit(
    conv: UnifiedConversation, current: dict, provider_opts: Optional[dict] = None
) -> None:
    try:
        provider = current.get("provider")
        sid = conv.sessions.get(provider)
        if sid:
            save_last_session(
                provider=provider,
                model=current.get("model"),
                session_id=sid,
                cwd=(provider_opts or {}).get("cwd"),
            )
            _safe_print(t("repl.exit.saved"), style="dim")
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        # Provider sessions are opaque external data.  A malformed value must
        # not turn an otherwise clean /exit or EOF into a traceback.
        pass
    _safe_print(t("repl.exit.bye"), style="dim")
