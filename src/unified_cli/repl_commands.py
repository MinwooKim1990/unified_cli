"""Declarative slash-command registry used by the REPL and completer."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class CommandSpec:
    name: str
    arg_hint: str
    desc_key: str
    takes: Optional[str] = None
    group: str = "info"
    aliases: Tuple[str, ...] = ()

    @property
    def all_names(self) -> Tuple[str, ...]:
        return (self.name,) + self.aliases


@dataclass(frozen=True)
class DispatchResult:
    handled: bool
    exit_requested: bool = False


CommandHandler = Callable[[CommandSpec, str, str], bool]


class CommandRegistry:
    """Ordered command metadata plus a small, terminal-free dispatcher.

    The handler receives ``(spec, invoked_name, argument_text)`` and returns
    whether the REPL should exit.  Parsing never invokes a shell and retains
    the argument text verbatim (apart from surrounding whitespace), which is
    important for paths and multi-word model names.
    """

    def __init__(self, commands: Iterable[CommandSpec] = ()):
        self._commands: list[CommandSpec] = []
        self._by_name: dict[str, CommandSpec] = {}
        for command in commands:
            self.register(command)

    def register(self, command: CommandSpec) -> None:
        for name in command.all_names:
            if not name.startswith("/") or any(ch.isspace() for ch in name):
                raise ValueError("slash command names must start with '/' and contain no spaces")
            if name in self._by_name:
                raise ValueError("duplicate slash command: " + name)
        self._commands.append(command)
        for name in command.all_names:
            self._by_name[name] = command

    @property
    def commands(self) -> Tuple[CommandSpec, ...]:
        return tuple(self._commands)

    def resolve(self, name: str) -> Optional[CommandSpec]:
        return self._by_name.get(name)

    def names(self, *, include_aliases: bool = True) -> list[str]:
        if include_aliases:
            return [name for command in self._commands for name in command.all_names]
        return [command.name for command in self._commands]

    def dispatch(self, line: str, handler: CommandHandler) -> DispatchResult:
        stripped = line.strip()
        if not stripped.startswith("/"):
            return DispatchResult(False)
        invoked, separator, argument = stripped.partition(" ")
        # Also accept tabs between command and argument without making quoting
        # or shell-like expansion part of the command language.
        if not separator:
            pieces = stripped.split(None, 1)
            invoked = pieces[0]
            argument = pieces[1] if len(pieces) == 2 else ""
        spec = self.resolve(invoked)
        if spec is None:
            return DispatchResult(False)
        return DispatchResult(True, bool(handler(spec, invoked, argument.strip())))


# Fixed argv documented by the provider vendors.  These tuples are internal
# constants: REPL input can select a key, but can never add argv, cwd, env, or
# shell syntax.  Antigravity intentionally has no executable auth spec because
# its documented login/logout flows live inside the full-screen ``agy`` TUI.
CORE_AUTH_SPECS: Mapping[str, Mapping[str, Tuple[str, ...]]] = MappingProxyType({
    "claude": MappingProxyType({
        "login": ("claude", "auth", "login"),
        "logout": ("claude", "auth", "logout"),
        "status": ("claude", "auth", "status", "--text"),
    }),
    "codex": MappingProxyType({
        "login": ("codex", "login"),
        "logout": ("codex", "logout"),
        "status": ("codex", "login", "status"),
    }),
})


DEFAULT_COMMANDS: Tuple[CommandSpec, ...] = (
    CommandSpec("/help", "", "slash.desc.help", group="info"),
    CommandSpec("/provider", "<id>", "slash.desc.provider", takes="provider", group="model"),
    CommandSpec("/model", "[name|--refresh]", "slash.desc.model", takes="model", group="model"),
    CommandSpec("/auth", "<status|login|logout> <provider>", "slash.desc.auth", group="model"),
    CommandSpec("/doctor", "", "slash.desc.doctor", group="model"),
    CommandSpec("/status", "", "slash.desc.status", group="model"),
    CommandSpec("/settings", "", "slash.desc.settings", group="settings"),
    CommandSpec("/style", "[name]", "slash.desc.style", group="settings"),
    CommandSpec("/effort", "[level]", "slash.desc.effort", group="settings"),
    CommandSpec("/reasoning", "[hidden|summary]", "slash.desc.reasoning", group="settings"),
    CommandSpec("/context", "[turns]", "slash.desc.context", takes="int", group="settings"),
    CommandSpec("/system", "[text]", "slash.desc.system", group="settings"),
    CommandSpec("/timeout", "[seconds|default]", "slash.desc.timeout", group="settings"),
    CommandSpec("/permissions", "[mode]", "slash.desc.permissions", group="tools"),
    CommandSpec("/tools", "", "slash.desc.tools", group="tools"),
    CommandSpec("/mcp", "", "slash.desc.mcp", group="tools"),
    CommandSpec("/web", "[on|off]", "slash.desc.web", group="tools"),
    CommandSpec("/cwd", "[path]", "slash.desc.cwd", takes="path", group="tools"),
    CommandSpec("/add-dir", "<path>", "slash.desc.add-dir", takes="path", group="tools"),
    CommandSpec("/sessions", "", "slash.desc.sessions", group="session"),
    CommandSpec("/rename", "<name>", "slash.desc.rename", group="session"),
    CommandSpec("/fork", "[name]", "slash.desc.fork", group="session"),
    CommandSpec("/compact", "", "slash.desc.compact", group="session"),
    CommandSpec("/export", "[path]", "slash.desc.export", takes="path", group="session"),
    CommandSpec("/copy", "", "slash.desc.copy", group="session"),
    CommandSpec("/clear", "", "slash.desc.clear", group="session"),
    CommandSpec("/new", "", "slash.desc.new", group="session"),
    CommandSpec("/resume", "", "slash.desc.resume", group="session"),
    CommandSpec("/save", "", "slash.desc.save", group="session"),
    CommandSpec("/history", "[N]", "slash.desc.history", takes="int", group="session"),
    CommandSpec("/image", "<path>", "slash.desc.image", takes="path", group="image"),
    CommandSpec("/images", "", "slash.desc.images", group="image"),
    CommandSpec("/clear-images", "", "slash.desc.clear-images", group="image"),
    CommandSpec("/review", "", "slash.desc.review", group="workflow"),
    CommandSpec("/diff", "", "slash.desc.diff", group="workflow"),
    CommandSpec("/init", "", "slash.desc.init", group="workflow"),
    CommandSpec("/theme", "[auto|dark|light]", "slash.desc.theme", group="info"),
    CommandSpec("/multiline", "[on|off]", "slash.desc.multiline", group="info"),
    CommandSpec("/usage", "", "slash.desc.usage", group="info", aliases=("/tokens",)),
    CommandSpec("/lang", "<en|ko>", "slash.desc.lang", takes="lang", group="info"),
    CommandSpec("/exit", "", "slash.desc.exit", group="session", aliases=("/quit",)),
)


DEFAULT_REGISTRY = CommandRegistry(DEFAULT_COMMANDS)
