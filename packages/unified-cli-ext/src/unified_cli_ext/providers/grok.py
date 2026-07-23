"""Read-only Preview adapter for the official xAI Grok Build CLI."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import sys
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from ..errors import ConfigurationError, ProtocolError
from ..transports.security import private_persistent_home
from .bridge import adapter_plugin
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    DynamicArgument,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    OperationLimits,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .installation import InstallationReceiptV1
from .path_resolver import resolve_path_installation


GROK_OFFICIAL_SOURCES = (
    "https://github.com/xai-org/grok-build",
    "https://docs.x.ai/build/overview",
    "https://docs.x.ai/build/cli/reference",
    "https://docs.x.ai/build/cli/headless-scripting",
    "https://docs.x.ai/build/enterprise",
)
GROK_OFFICIAL_PACKAGE = "@xai-official/grok"
GROK_OFFICIAL_INSTALLER = "https://x.ai/cli/install.sh"
GROK_STAGE_6_TARGET_VERSION = "0.2.111"
GROK_DEFAULT_MODEL = "grok-4.5"
GROK_REJECTED_PACKAGE_IDENTITIES = ("@vibe-kit/grok-cli",)
GROK_REAL_AUTHENTICATED_SMOKE_CAPTURED = True
GROK_REAL_SMOKE_VERSION = "0.2.111"
GROK_REAL_SMOKE_PLATFORM = "macos-aarch64"
GROK_REAL_SMOKE_DATE = "2026-07-23"
GROK_NATIVE_SHA256 = "e1fafdfffe14f339460befaf194360e8f90bfd02efe8a4f24cfa1c7aea657ffe"
GROK_NATIVE_SNAPSHOT = "native-0.2.111-darwin-arm64-e1fafdfffe14"
GROK_SAFE_CONFIG = """[cli]
auto_update = false

[session]
load_envrc = false

[features]
web_fetch = false
write_file = false
tool_search = false
lsp_tools = false

[tools]
respect_gitignore = true

[subagents]
enabled = false

[memory]
enabled = false

[sandbox]
profile = "strict"
auto_allow_bash = false

[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false

[compat.cursor]
skills = false
rules = false
agents = false
mcps = false
hooks = false

[compat.codex]
skills = false
rules = false
agents = false
mcps = false
hooks = false

[marketplace]
default_skills_installs_purged = true
official_marketplace_auto_installed = false
"""

GROK_FIXED_ENVIRONMENT = MappingProxyType({
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_WRITE_FILE": "0",
    "GROK_TOOL_SEARCH": "0",
    "GROK_LSP_TOOLS": "0",
    "GROK_MEMORY": "0",
    "GROK_SUBAGENTS": "0",
    "GROK_WEB_FETCH": "0",
    "GROK_RESPECT_GITIGNORE": "1",
    "GROK_CURSOR_SKILLS_ENABLED": "false",
    "GROK_CURSOR_RULES_ENABLED": "false",
    "GROK_CURSOR_AGENTS_ENABLED": "false",
    "GROK_CURSOR_MCPS_ENABLED": "false",
    "GROK_CURSOR_HOOKS_ENABLED": "false",
    "GROK_CURSOR_SESSIONS_ENABLED": "false",
    "GROK_CLAUDE_SKILLS_ENABLED": "false",
    "GROK_CLAUDE_RULES_ENABLED": "false",
    "GROK_CLAUDE_AGENTS_ENABLED": "false",
    "GROK_CLAUDE_MCPS_ENABLED": "false",
    "GROK_CLAUDE_HOOKS_ENABLED": "false",
    "GROK_CLAUDE_SESSIONS_ENABLED": "false",
    "GROK_CODEX_SKILLS_ENABLED": "false",
    "GROK_CODEX_RULES_ENABLED": "false",
    "GROK_CODEX_AGENTS_ENABLED": "false",
    "GROK_CODEX_MCPS_ENABLED": "false",
    "GROK_CODEX_HOOKS_ENABLED": "false",
    "GROK_CODEX_SESSIONS_ENABLED": "false",
    "GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER": "0",
    "GROK_MARKETPLACE_REQUIRE_SHA": "1",
    "GROK_MANAGED_MCPS_ENABLED": "false",
    "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
})

_BLOCKED_WORKSPACE_ENTRIES = (
    (".grok",),
    (".envrc",),
    (".mcp.json",),
    (".cursor", "mcp.json"),
    (".cursor", "hooks.json"),
    (".claude",),
)
_BLOCKED_HOME_ENTRIES = (
    (".bashrc",),
    (".bash_profile",),
    (".bash_login",),
    (".profile",),
    (".bash_logout",),
    (".grok", "managed_config.toml"),
    (".grok", "requirements.toml"),
    (".grok", "plugins"),
    (".grok", "hooks"),
    (".grok", "hooks-paths"),
    (".claude.json",),
    (".claude",),
    (".cursor", "mcp.json"),
    (".cursor", "hooks.json"),
)
_BLOCKED_SYSTEM_ENTRIES = (
    "/etc/grok/managed_config.toml",
    "/etc/grok/requirements.toml",
)

_PROBE_LIMITS = OperationLimits(
    timeout_seconds=10.0,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=16 * 1024,
    max_events=8,
)
_PROMPT_LIMITS = OperationLimits(
    timeout_seconds=120.0,
    max_stdout_bytes=16 * 1024 * 1024,
    max_stderr_bytes=1024 * 1024,
    max_events=50_000,
)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(tuple(argv), limits=_PROBE_LIMITS)


GROK_HEADLESS_FIXED_ARGV = (
    "--no-auto-update",
    "--sandbox",
    "strict",
    "--permission-mode",
    "dontAsk",
    "--tools",
    "read_file,grep,list_dir",
    "--allow",
    "Read",
    "--allow",
    "Grep",
    "--deny",
    "Bash",
    "--deny",
    "Edit",
    "--deny",
    "MCPTool",
    "--deny",
    "WebFetch",
    "--deny",
    "WebSearch",
    "--no-plan",
    "--no-subagents",
    "--no-memory",
    "--disable-web-search",
    "--output-format",
    "streaming-json",
)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="grok",
    display_name="xAI Grok Build",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="grok",
        expected_identity="grok",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0, 2, 111),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="grok ",
            identity_marker="grok 0.2.111 ",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat", "sessions", "stream")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "chat": "-p, --single",
                "sessions": "-r, --resume",
                "stream": "--output-format",
            },
            identity_marker="Usage: grok",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=GROK_HEADLESS_FIXED_ARGV,
        dynamic_arguments=(
            DynamicArgument("model", "-m", required=True),
            DynamicArgument("session", "-r"),
        ),
        mode=PromptMode.OPTION_VALUE,
        prompt_option="-p",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.JSONL,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("XAI_API_KEY",)),
        fixed_values=GROK_FIXED_ENVIRONMENT,
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("inspect", "--json"))),
    capabilities=frozenset(
        (
            ProviderCapability.CHAT.value,
            ProviderCapability.STREAM.value,
            ProviderCapability.SESSIONS.value,
        )
    ),
    server_policy=AdapterServerPolicy(enabled=False),
)


def _state() -> dict:
    return {"ended": False, "stop_reason": None}


def _nonempty_string(record: Mapping, name: str) -> str:
    value = record.get(name)
    if type(value) is not str or not value:
        raise ProtocolError("Grok returned a malformed end record")
    return value


def _usage_counter(usage: Mapping, name: str) -> int:
    value = usage.get(name)
    if type(value) is not int or value < 0 or value > 10**15:
        raise ProtocolError("Grok returned malformed usage counters")
    return value


def _entry_exists(root: str, parts: tuple[str, ...]) -> bool:
    return os.path.lexists(os.path.join(root, *parts))


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_uid", "st_mode", "st_nlink")
    return all(getattr(before, name) == getattr(after, name) for name in fields)


def _managed_provider_home(provider_home: str) -> bool:
    """Return whether Core owns the requested Grok home subtree."""

    root = os.path.realpath(
        os.path.join(
            os.path.expanduser("~"),
            ".unified-cli",
            "providers",
            ADAPTER_SPEC.id,
        )
    )
    try:
        return os.path.commonpath((root, provider_home)) == root
    except (TypeError, ValueError):
        return False


def _prepare_managed_provider_configuration(provider_home: object) -> None:
    """Provision only the deterministic safe config in Core's private home.

    Authentication is deliberately left to the vendor's official login
    command. Existing paths are never replaced or truncated; the normal
    validation path remains authoritative after this helper returns.
    """

    if type(provider_home) is not str:
        return
    try:
        home = private_persistent_home(provider_home)
    except ConfigurationError:
        raise ConfigurationError(
            "Grok Preview requires an explicit private provider home"
        ) from None
    if not _managed_provider_home(home):
        return

    state_dir = os.path.join(home, ".grok")
    try:
        os.mkdir(state_dir, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ConfigurationError(
            "Grok Preview provider config could not be prepared"
        ) from None
    _validate_state_directory(
        state_dir,
        label=".grok state directory",
        forbid_shared_write=True,
    )

    config_path = os.path.join(state_dir, "config.toml")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(config_path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError:
        raise ConfigurationError(
            "Grok Preview provider config could not be prepared"
        ) from None
    if descriptor is not None:
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != getattr(os, "geteuid", lambda: metadata.st_uid)()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ConfigurationError(
                    "Grok Preview provider config could not be prepared"
                )
            remaining = memoryview(GROK_SAFE_CONFIG.encode("utf-8"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except ConfigurationError:
            raise
        except OSError:
            raise ConfigurationError(
                "Grok Preview provider config could not be prepared"
            ) from None
        finally:
            os.close(descriptor)
    _validate_safe_config(home)


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _private_snapshot_directory(parent: str, name: str) -> str:
    path = os.path.join(parent, name)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ConfigurationError(
            "Grok native runtime snapshot could not be prepared"
        ) from None
    try:
        metadata = os.lstat(path)
    except OSError:
        raise ConfigurationError(
            "Grok native runtime snapshot could not be inspected"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != getattr(os, "geteuid", lambda: metadata.st_uid)()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ConfigurationError("Grok native runtime snapshot is unsafe")
    return path


def _verified_native_source() -> int:
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        raise ConfigurationError(
            "Grok native Preview is verified only on macOS arm64"
        )
    home = os.path.realpath(os.path.expanduser("~"))
    source = os.path.join(home, ".grok", "downloads", "grok-macos-aarch64")
    launcher = shutil.which(ADAPTER_SPEC.binary.executable)
    if launcher is None or os.path.realpath(launcher) != source:
        raise ConfigurationError("official Grok native installation was not found")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(source, flags)
        metadata = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ConfigurationError(
            "official Grok native installation could not be inspected"
        ) from None
    try:
        verified = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid
            == getattr(os, "geteuid", lambda: metadata.st_uid)()
            and metadata.st_nlink == 1
            and not stat.S_IMODE(metadata.st_mode) & 0o022
            and _hash_descriptor(descriptor) == GROK_NATIVE_SHA256
        )
    except OSError:
        os.close(descriptor)
        raise ConfigurationError(
            "official Grok native installation could not be inspected"
        ) from None
    if not verified:
        os.close(descriptor)
        raise ConfigurationError("official Grok native installation is unverified")
    return descriptor


def _verified_snapshot_receipt() -> InstallationReceiptV1:
    """Copy the reviewed native binary into a stable Core-owned launch path."""

    source = _verified_native_source()
    try:
        from unified_cli.extension_config import default_provider_home

        provider_home = default_provider_home(ADAPTER_SPEC.id)
        provider_root = os.path.dirname(provider_home)
        snapshot = _private_snapshot_directory(provider_root, GROK_NATIVE_SNAPSHOT)
        bin_dir = _private_snapshot_directory(snapshot, "bin")
        target = os.path.join(bin_dir, ADAPTER_SPEC.binary.executable)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        target_descriptor = None
        try:
            target_descriptor = os.open(target, flags, 0o500)
        except FileExistsError:
            pass
        except OSError:
            raise ConfigurationError(
                "Grok native runtime snapshot could not be created"
            ) from None
        if target_descriptor is not None:
            try:
                os.fchmod(target_descriptor, 0o500)
                os.lseek(source, 0, os.SEEK_SET)
                while True:
                    block = os.read(source, 1024 * 1024)
                    if not block:
                        break
                    pending = memoryview(block)
                    while pending:
                        written = os.write(target_descriptor, pending)
                        if written <= 0:
                            raise OSError("short write")
                        pending = pending[written:]
                os.fsync(target_descriptor)
            except OSError:
                raise ConfigurationError(
                    "Grok native runtime snapshot could not be created"
                ) from None
            finally:
                os.close(target_descriptor)

        verify_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        verify_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            verified = os.open(target, verify_flags)
            metadata = os.fstat(verified)
        except OSError:
            raise ConfigurationError(
                "Grok native runtime snapshot could not be verified"
            ) from None
        try:
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid
                != getattr(os, "geteuid", lambda: metadata.st_uid)()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o500
                or _hash_descriptor(verified) != GROK_NATIVE_SHA256
            ):
                raise ConfigurationError("Grok native runtime snapshot is invalid")
        finally:
            os.close(verified)
        return InstallationReceiptV1.capture_explicit_direct(
            provider_id=ADAPTER_SPEC.id,
            executable_path=target,
            executable_basename=ADAPTER_SPEC.binary.executable,
        )
    finally:
        os.close(source)


def _resolve_grok_installation() -> InstallationReceiptV1:
    try:
        return resolve_path_installation(
            provider_id=ADAPTER_SPEC.id,
            executable=ADAPTER_SPEC.binary.executable,
            package_names=(GROK_OFFICIAL_PACKAGE,),
        )
    except ConfigurationError:
        return _verified_snapshot_receipt()


def _validate_state_directory(
    path: str,
    *,
    label: str,
    exact_mode: int | None = None,
    forbid_shared_write: bool = False,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        before_path = os.lstat(path)
        descriptor = os.open(path, flags)
    except OSError:
        raise ConfigurationError(
            "Grok Preview {} must be a private real directory".format(label)
        ) from None
    try:
        opened = os.fstat(descriptor)
        after_path = os.lstat(path)
        effective_uid = getattr(os, "geteuid", lambda: opened.st_uid)()
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISDIR(before_path.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(after_path.st_mode)
            or opened.st_uid != effective_uid
            or not os.path.samestat(before_path, opened)
            or not os.path.samestat(after_path, opened)
            or not _same_identity(before_path, opened)
            or not _same_identity(opened, after_path)
            or (exact_mode is not None and mode != exact_mode)
            or (forbid_shared_write and mode & 0o022)
        ):
            raise ConfigurationError(
                "Grok Preview {} must be a private real directory".format(label)
            )
    except OSError:
        raise ConfigurationError(
            "Grok Preview {} must be a private real directory".format(label)
        ) from None
    finally:
        os.close(descriptor)


def _open_private_state_file(path: str, *, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ConfigurationError(
            "Grok Preview {} must be a private regular file".format(label)
        ) from None
    try:
        opened = os.fstat(descriptor)
        path_info = os.lstat(path)
        effective_uid = getattr(os, "geteuid", lambda: opened.st_uid)()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or opened.st_uid != effective_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or not os.path.samestat(path_info, opened)
            or not _same_identity(path_info, opened)
        ):
            raise ConfigurationError(
                "Grok Preview {} must be a private regular file".format(label)
            )
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _validate_safe_config(home: str) -> None:
    path = os.path.join(home, ".grok", "config.toml")
    if not os.path.lexists(path):
        raise ConfigurationError(
            "Grok Preview provider config must match the safe template"
        )
    expected = GROK_SAFE_CONFIG.encode("utf-8")
    try:
        descriptor, before = _open_private_state_file(
            path, label="provider config"
        )
    except ConfigurationError:
        raise ConfigurationError(
            "Grok Preview provider config must match the safe template"
        ) from None
    try:
        if before.st_size != len(expected):
            raise ConfigurationError(
                "Grok Preview provider config must match the safe template"
            )
        payload = bytearray()
        while len(payload) <= len(expected):
            chunk = os.read(descriptor, len(expected) + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        path_info = os.lstat(path)
        if (
            bytes(payload) != expected
            or not stat.S_ISREG(path_info.st_mode)
            or not os.path.samestat(path_info, after)
            or not _same_identity(before, after)
            or not _same_identity(after, path_info)
            or before.st_size != after.st_size
            or after.st_size != path_info.st_size
            or getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))
            != getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9))
            or getattr(before, "st_ctime_ns", int(before.st_ctime * 1e9))
            != getattr(after, "st_ctime_ns", int(after.st_ctime * 1e9))
        ):
            raise ConfigurationError(
                "Grok Preview provider config must match the safe template"
            )
    except OSError:
        raise ConfigurationError(
            "Grok Preview provider config must match the safe template"
        ) from None
    finally:
        os.close(descriptor)


def _validate_auth_state(home: str) -> None:
    path = os.path.join(home, ".grok", "auth.json")
    if not os.path.lexists(path):
        return
    descriptor, before = _open_private_state_file(path, label="auth state")
    try:
        after = os.fstat(descriptor)
        path_info = os.lstat(path)
        if (
            not os.path.samestat(path_info, after)
            or not _same_identity(before, after)
            or not _same_identity(after, path_info)
            or before.st_size != after.st_size
            or after.st_size != path_info.st_size
            or getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))
            != getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9))
            or getattr(before, "st_ctime_ns", int(before.st_ctime * 1e9))
            != getattr(after, "st_ctime_ns", int(after.st_ctime * 1e9))
        ):
            raise ConfigurationError(
                "Grok Preview auth state must be a private regular file"
            )
    except OSError:
        raise ConfigurationError(
            "Grok Preview auth state must be a private regular file"
        ) from None
    finally:
        os.close(descriptor)


def _validate_provider_configuration(provider_home: object) -> None:
    if (
        type(provider_home) is not str
        or not os.path.isabs(provider_home)
        or os.path.normpath(provider_home) != provider_home
        or os.path.realpath(provider_home) != provider_home
    ):
        raise ConfigurationError(
            "Grok Preview requires an explicit private provider home"
        )
    home = provider_home
    _validate_state_directory(home, label="provider home", exact_mode=0o700)
    _validate_state_directory(
        os.path.join(home, ".grok"),
        label=".grok state directory",
        forbid_shared_write=True,
    )
    _validate_safe_config(home)
    _validate_auth_state(home)
    if any(_entry_exists(home, parts) for parts in _BLOCKED_HOME_ENTRIES):
        raise ConfigurationError(
            "Grok Preview refuses provider-home tool, plugin, or hook configuration"
        )
    if any(os.path.lexists(path) for path in _BLOCKED_SYSTEM_ENTRIES):
        raise ConfigurationError(
            "Grok Preview refuses system-managed runtime configuration"
        )


def _validate_runtime_boundary(cwd: object, provider_home: object) -> None:
    _validate_provider_configuration(provider_home)
    if type(cwd) is not str or not os.path.isabs(cwd):
        raise ConfigurationError("Grok Preview requires an explicit absolute cwd")
    root = os.path.realpath(cwd)
    if not os.path.isdir(root):
        raise ConfigurationError("Grok Preview cwd is unavailable")

    current = root
    git_root = None
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            git_root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    current = root
    while True:
        if any(_entry_exists(current, parts) for parts in _BLOCKED_WORKSPACE_ENTRIES):
            raise ConfigurationError(
                "Grok Preview refuses project tool, plugin, or hook configuration"
            )
        if git_root is None or current == git_root:
            break
        current = os.path.dirname(current)


def _map_record(record: Mapping, state: dict):
    if not isinstance(record, Mapping) or type(state) is not dict:
        raise ProtocolError("Grok returned a malformed stream record")
    if state.get("ended") is not False:
        raise ProtocolError("Grok returned a record after end")
    kind = record.get("type")
    if kind in ("text", "thought"):
        if set(record) != {"type", "data"} or type(record.get("data")) is not str:
            raise ProtocolError("Grok returned a malformed stream record")
        if kind == "thought":
            return ()
        return ({"type": "text_delta", "text": record["data"]},)
    if kind == "error":
        message = record.get("message")
        if type(message) is not str or not message:
            raise ProtocolError("Grok returned a malformed error record")
        try:
            message_size = len(message.encode("utf-8", "strict"))
        except UnicodeError:
            raise ProtocolError("Grok returned a malformed error record") from None
        if message_size > 64 * 1024:
            raise ProtocolError("Grok returned a malformed error record")
        # Keep the vendor message inside the transport boundary. The process
        # exits non-zero and the bridge classifies only controlled markers
        # from its already bounded, redacted diagnostics.
        return ()
    if kind == "end":
        required = {"type", "stopReason", "sessionId", "requestId"}
        if not required <= set(record):
            raise ProtocolError("Grok returned a malformed end record")
        stop_reason = _nonempty_string(record, "stopReason")
        session_id = _nonempty_string(record, "sessionId")
        _nonempty_string(record, "requestId")
        usage_is_incomplete = record.get("usage_is_incomplete", False)
        if type(usage_is_incomplete) is not bool or usage_is_incomplete:
            raise ProtocolError("Grok returned incomplete usage counters")
        events = [{"type": "session", "session_id": session_id}]
        usage = record.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ProtocolError("Grok returned malformed usage counters")
            events.append(
                {
                    "type": "usage",
                    "input_tokens": _usage_counter(usage, "input_tokens"),
                    "cached_input_tokens": _usage_counter(
                        usage, "cache_read_input_tokens"
                    ),
                    "output_tokens": _usage_counter(usage, "output_tokens"),
                }
            )
        state["ended"] = True
        state["stop_reason"] = stop_reason
        return tuple(events)
    raise ProtocolError("Grok returned an unknown stream record")


def _finalize(state: dict):
    if type(state) is not dict or state.get("ended") is not True:
        raise ProtocolError("Grok stream ended without an end record")
    reason = state.get("stop_reason")
    if type(reason) is not str or not reason:
        raise ProtocolError("Grok stream end state is malformed")
    return ({"type": "done", "reason": reason},)


_BASE_PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=GROK_DEFAULT_MODEL,
    launch_resolver=_resolve_grok_installation,
    state_factory=_state,
    map_record=_map_record,
    finalize=_finalize,
    turn_preflight=_validate_runtime_boundary,
)


def _checked_factory(*args, **kwargs):
    if args:
        raise ConfigurationError(
            "provider factory received unsupported positional options"
        )
    _prepare_managed_provider_configuration(kwargs.get("provider_home"))
    _validate_runtime_boundary(kwargs.get("cwd"), kwargs.get("provider_home"))
    return _require_exact_version(_BASE_PLUGIN.factory(**kwargs))


def _require_exact_version(instance):
    inspection = getattr(instance, "_inspection", None)
    version = getattr(inspection, "version", None)
    if type(version) is not str or version != GROK_STAGE_6_TARGET_VERSION:
        raise ProtocolError(
            "Grok Preview requires exact version {}".format(
                GROK_STAGE_6_TARGET_VERSION
            )
        )
    return instance


def _require_exact_doctor_version(result):
    if (
        not isinstance(result, Mapping)
        or result.get("available") is not True
        or result.get("version") != GROK_STAGE_6_TARGET_VERSION
    ):
        raise ProtocolError(
            "Grok Preview requires exact version {}".format(
                GROK_STAGE_6_TARGET_VERSION
            )
        )
    return result


def _checked_binder(context):
    bound = _BASE_PLUGIN.launch_binder(context)
    _prepare_managed_provider_configuration(bound.provider_home)
    _validate_provider_configuration(bound.provider_home)

    def create_checked(request):
        _validate_runtime_boundary(request.workspace, bound.provider_home)
        return _require_exact_version(bound.factory(request))

    def doctor_checked():
        _validate_provider_configuration(bound.provider_home)
        return _require_exact_doctor_version(bound.doctor())

    return replace(bound, factory=create_checked, doctor=doctor_checked)


def _checked_doctor():
    return _require_exact_doctor_version(_BASE_PLUGIN.doctor())


PLUGIN = replace(
    _BASE_PLUGIN,
    factory=_checked_factory,
    doctor=_checked_doctor,
    launch_binder=_checked_binder,
)


__all__ = [
    "ADAPTER_SPEC",
    "GROK_HEADLESS_FIXED_ARGV",
    "GROK_DEFAULT_MODEL",
    "GROK_FIXED_ENVIRONMENT",
    "GROK_OFFICIAL_INSTALLER",
    "GROK_OFFICIAL_PACKAGE",
    "GROK_OFFICIAL_SOURCES",
    "GROK_REAL_AUTHENTICATED_SMOKE_CAPTURED",
    "GROK_REAL_SMOKE_DATE",
    "GROK_REAL_SMOKE_PLATFORM",
    "GROK_REAL_SMOKE_VERSION",
    "GROK_REJECTED_PACKAGE_IDENTITIES",
    "GROK_SAFE_CONFIG",
    "GROK_STAGE_6_TARGET_VERSION",
    "PLUGIN",
]
