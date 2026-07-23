"""Security boundary for the opt-in loopback browser management surface.

The ordinary OpenAI-compatible server intentionally does not depend on this
runtime.  A caller must explicitly prepare it, exchange a short-lived one-time
bootstrap secret, and then use a host-only cookie plus an in-memory CSRF token.
Provider output is reduced to a small, normalized NDJSON vocabulary before it
crosses the browser boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
from importlib import metadata as importlib_metadata
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .base import (
    _drain_binary_into,
    _popen_process_group_kwargs,
    _terminate_process_tree,
)
from .conversation import Turn, UnifiedConversation
from .core import Attachment, Message, Usage
from .errors import UnifiedError
from .models import (
    DEFAULT_MODELS,
    invalidate_model_cache as invalidate_core_model_cache,
    list_models,
)
from .plugin import ProviderServerPolicyV1
from .registry import ProviderDescriptorV1, list_providers
from .session_manager import SessionManager, SessionRecord
from .settings import load_settings, save_settings
from .usage import tracker


COOKIE_NAME = "unified_cli_manage"
BOOTSTRAP_TTL_SECONDS = 60.0
MAX_UI_BODY_BYTES = 20 * 1024 * 1024
MAX_PROMPT_CHARS = 32 * 1024
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024
MAX_STREAM_TEXT_CHARS = 4 * 1024 * 1024
MAX_STREAM_EVENTS = 20_000
MAX_ACTIVE_CHATS = 4
MAX_MANAGE_SESSIONS = 16
VERIFY_TIMEOUT_SECONDS = 5.0
MAX_VERIFY_OUTPUT_BYTES = 32 * 1024
VERIFY_VERSION_TTL_SECONDS = 300.0
VERIFY_AUTH_TTL_SECONDS = 15.0
PROVIDER_MODELS_TTL_SECONDS = 60.0
MAX_PROVIDER_CACHE_ENTRIES = 64
MAX_EXTENSION_PROVIDER_SNAPSHOTS = 32
MAX_EXTENSION_PROVIDER_CAPABILITIES = 64

_HANDLE_RE = re.compile(r"^[a-z]+_[A-Za-z0-9_-]{16,64}$", re.ASCII)
_EXT_PROVIDER_ID_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", re.ASCII
)
_EXT_CAPABILITY_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", re.ASCII
)
_CORE_PROVIDER_IDS = frozenset(("claude", "codex", "gemini"))
_RESERVED_MANAGE_PROVIDER_IDS = frozenset((*_CORE_PROVIDER_IDS, "agy"))
_EXT_SUPPORT_STATUSES = frozenset(
    ("stable", "preview", "experimental", "held")
)
_SNAPSHOT_CANCELLATION_EXCEPTIONS = (
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
)
_MANAGE_STREAM_ERROR_CODES = frozenset((
    "manage_disabled",
    "provider_forbidden",
    "provider_unsupported",
    "permission_forbidden",
))
_DATA_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]+={0,2})",
    re.ASCII,
)
_SECRET_REPLACEMENTS = (
    re.compile(r"(?i)\b(?:bearer|token|password|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"\b(?:sk|key|tok)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)


class ManageError(Exception):
    """Stable browser-facing failure with no reflected secret/provider text."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Workspace:
    id: str
    path: str
    name: str


@dataclass
class BrowserSession:
    key: str
    csrf_token: str
    created_at: float
    last_seen: float
    requests: Deque[float] = field(default_factory=deque)
    mutations: Deque[float] = field(default_factory=deque)


@dataclass
class ActiveChat:
    id: str
    owner_key: str
    provider: str
    workspace: Workspace
    model: str
    prompt: str
    images: List[Attachment]
    native_session_id: Optional[str]
    browser_session_id: Optional[str]
    cancel_event: threading.Event = field(default_factory=threading.Event)


_COPY_COMMANDS: Dict[str, Dict[str, Any]] = {
    "claude": {
        "docs_url": "https://code.claude.com/docs/en/quickstart",
        "install": "curl -fsSL https://claude.ai/install.sh | bash",
        "login": "claude auth login",
        "status": "claude auth status --text",
        "logout": "claude auth logout",
    },
    "codex": {
        "docs_url": "https://learn.chatgpt.com/docs/auth",
        "install": "npm install --global @openai/codex",
        "login": "codex login",
        "status": "codex login status",
        "logout": "codex logout",
    },
    "gemini": {
        "docs_url": "https://antigravity.google/docs/cli-getting-started",
        "install": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "login": "agy",
        "status": None,
        "logout": "Use /logout inside the agy TUI",
    },
}

# Only these literal argv templates can execute.  Install/login/logout strings
# above are data for copy-to-clipboard UI and are never passed to a shell.
_VERIFY_SPECS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "claude": (("claude", "--version"),
               ("claude", "auth", "status", "--text")),
    "codex": (("codex", "--version"),
              ("codex", "login", "status")),
    # agy has no confirmed safe noninteractive auth-status command.
    "gemini": (("agy", "--version"),),
}


@dataclass(frozen=True)
class _VerifyBinaryIdentity:
    """Cheap exact identity for an explicitly selected executable path.

    ManageRuntime deliberately does not claim package/vendor provenance.  The
    record binds cache entries to the selected invocation path, canonical
    target, link metadata, and target metadata, and those fields are re-read
    before a cached result is served.
    """

    provider: str
    executable: str
    invoked_path: str
    real_path: str
    link_stat: Tuple[int, int, int, int, int, int, int]
    target_stat: Tuple[int, int, int, int, int, int, int]

    def current(self) -> bool:
        current = _binary_identity_from_path(
            self.provider, self.executable, self.invoked_path
        )
        return current == self


class _ManageVerifyFlight:
    """One same-provider/context verification shared by explicit callers."""

    def __init__(self, context: object, *, force_refresh: bool) -> None:
        self.context = context
        self.force_refresh = force_refresh
        self.done = threading.Event()
        self.result: Optional[
            Tuple[Tuple[bool, str, str], Optional[Tuple[bool, str]]]
        ] = None
        self.error: Optional[BaseException] = None
        self.committed = False


class _ManageModelFlight:
    """One immutable model result shared within a Manage cache generation."""

    def __init__(self, key: object, *, force_refresh: bool) -> None:
        self.key = key
        self.force_refresh = force_refresh
        self.done = threading.Event()
        self.result: Optional[Tuple[Tuple[object, ...], ...]] = None
        self.error: Optional[BaseException] = None
        self.committed = False


def _stat_identity(value: os.stat_result) -> Tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        getattr(value, "st_uid", -1),
    )


def _verify_binary_identity(
    provider: str, executable: str
) -> Optional[_VerifyBinaryIdentity]:
    selected = shutil.which(executable, path=os.environ.get("PATH"))
    if selected is None:
        return None
    return _binary_identity_from_path(provider, executable, selected)


def _binary_identity_from_path(
    provider: str, executable: str, selected: str
) -> Optional[_VerifyBinaryIdentity]:
    invoked = os.path.abspath(selected)
    real_path = os.path.realpath(invoked)
    try:
        link_stat = os.lstat(invoked)
        target_stat = os.stat(real_path)
    except OSError:
        return None
    return _VerifyBinaryIdentity(
        provider=provider,
        executable=executable,
        invoked_path=invoked,
        real_path=real_path,
        link_stat=_stat_identity(link_stat),
        target_stat=_stat_identity(target_stat),
    )


def _effective_model_binary_identity(
    provider: str,
) -> Optional[_VerifyBinaryIdentity]:
    """Fingerprint only an executable the corresponding Core lister executes."""

    if provider != "gemini":
        # Claude lists through the Anthropic HTTP API; Codex reads its local
        # models_cache.json.  Neither model path executes its provider CLI.
        return None
    from .providers.gemini import gemini_enabled

    if not gemini_enabled():
        return None
    from .discovery import find_agy_bin

    selected = find_agy_bin()
    if selected is None:
        return None
    return _binary_identity_from_path(provider, "agy", selected)


def _environment_identity(provider: str) -> Tuple[Tuple[str, str], ...]:
    """Return a non-secret identity for provider data inputs.

    The manage verifier inherits only HOME plus locale/PATH controls; PATH is
    represented by the executable identity above.  Model discovery additionally
    consumes the provider-specific values listed here.  Secret values are
    represented only by SHA-256 digests and never retained verbatim.
    """

    common = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    names = common + {
        "claude": (
            "ANTHROPIC_API_KEY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ),
        "codex": (),
        "gemini": ("AGY_CLI_PATH", "UNIFIED_CLI_ENABLE_GEMINI"),
    }.get(provider, ())
    result = []
    for name in names:
        value = os.environ.get(name, "")
        if name == "HOME" and value:
            value = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
        result.append((name, hashlib.sha256(value.encode("utf-8")).hexdigest()))
    return tuple(result)


def _urlsafe_random() -> str:
    # 32 bytes = 256 bits before URL-safe base64 encoding.
    return secrets.token_urlsafe(32)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    output: List[str] = []
    for char in value[:maximum]:
        if char in ("\n", "\t"):
            output.append(char)
        elif unicodedata.category(char).startswith("C"):
            output.append("�")
        else:
            output.append(char)
    if len(value) > maximum:
        output.append("…")
    return "".join(output)


def _valid_snapshot_text(
    value: object, *, maximum: int, allow_empty: bool = False
) -> bool:
    """Validate one callback-free metadata string before browser exposure."""

    if type(value) is not str or len(value) > maximum:
        return False
    if (not value and not allow_empty) or value != value.strip():
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return not any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    )


def _copy_extension_provider_snapshot(
    value: object,
) -> ProviderDescriptorV1:
    """Return a conservative Core-owned copy of one Ext descriptor.

    ``ProviderDescriptorV1`` is deliberately a metadata container rather than
    a validator.  Manage mode therefore revalidates every field it retains and
    reconstructs the descriptor without provider-owned aliases.  Executable
    policy is never inferred from capabilities or from the advertised server
    policy.
    """

    if type(value) is not ProviderDescriptorV1:
        raise ValueError("extension provider snapshot is invalid")
    try:
        provider_id = value.id
        source = value.source
        lifecycle_status = value.status
        support_status = value.support_status
        default_model = value.default_model
        capabilities = value.capabilities
        route_prefixes = value.route_prefixes
        server_policy = value.server_policy
    except (AttributeError, TypeError, ValueError):
        raise ValueError("extension provider snapshot is invalid") from None

    if (
        type(provider_id) is not str
        or len(provider_id) > 64
        or _EXT_PROVIDER_ID_RE.fullmatch(provider_id) is None
        or provider_id in _RESERVED_MANAGE_PROVIDER_IDS
        or type(source) is not str
        or type(lifecycle_status) is not str
        or type(support_status) is not str
        or source != "extension"
        or lifecycle_status != "loaded"
        or support_status not in _EXT_SUPPORT_STATUSES
    ):
        raise ValueError("extension provider snapshot is invalid")

    if type(capabilities) is not frozenset or (
        len(capabilities) > MAX_EXTENSION_PROVIDER_CAPABILITIES
    ):
        raise ValueError("extension provider snapshot is invalid")
    copied_capabilities = []
    for capability in capabilities:
        if (
            type(capability) is not str
            or len(capability) > 64
            or _EXT_CAPABILITY_RE.fullmatch(capability) is None
        ):
            raise ValueError("extension provider snapshot is invalid")
        copied_capabilities.append(capability)

    if type(route_prefixes) is not tuple or len(route_prefixes) > 1:
        raise ValueError("extension provider snapshot is invalid")
    if route_prefixes and (
        type(route_prefixes[0]) is not str
        or route_prefixes[0] != provider_id
    ):
        raise ValueError("extension provider snapshot is invalid")
    if server_policy is not None and type(server_policy) is not ProviderServerPolicyV1:
        raise ValueError("extension provider snapshot is invalid")
    if server_policy is not None and (
        type(server_policy.enabled) is not bool
        or type(server_policy.requires_external_isolation) is not bool
    ):
        raise ValueError("extension provider snapshot is invalid")

    if support_status == "held":
        # Held metadata may be shown, but never as executable or configured.
        copied_default_model = None
        copied_capabilities = []
    else:
        if not _valid_snapshot_text(default_model, maximum=512):
            raise ValueError("extension provider snapshot is invalid")
        copied_default_model = default_model

    return ProviderDescriptorV1(
        id=provider_id,
        source="extension",
        status="loaded",
        support_status=support_status,
        default_model=copied_default_model,
        capabilities=frozenset(tuple(copied_capabilities)),
        route_prefixes=(provider_id,),
        # Manage's actual Ext server policy is conservative regardless of a
        # plugin's descriptive claim. No HTTP or browser permission follows.
        server_policy=ProviderServerPolicyV1(
            enabled=False,
            requires_external_isolation=True,
        ),
        error=None,
    )


def _copy_extension_provider_snapshots(value: object) -> Tuple[ProviderDescriptorV1, ...]:
    """Materialize one bounded built-in container and retain no caller aliases."""

    if value is None:
        return ()
    if type(value) is dict:
        if len(value) > MAX_EXTENSION_PROVIDER_SNAPSHOTS:
            raise ValueError("extension provider snapshots exceed the limit")
        try:
            materialized = tuple(value.items())
        except _SNAPSHOT_CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            raise ValueError("extension provider snapshots are invalid") from None
    elif type(value) in (list, tuple):
        if len(value) > MAX_EXTENSION_PROVIDER_SNAPSHOTS:
            raise ValueError("extension provider snapshots exceed the limit")
        try:
            materialized = tuple((None, item) for item in value)
        except _SNAPSHOT_CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            raise ValueError("extension provider snapshots are invalid") from None
    else:
        raise ValueError("extension provider snapshots must be a list, tuple, or dict")
    if len(materialized) > MAX_EXTENSION_PROVIDER_SNAPSHOTS:
        raise ValueError("extension provider snapshots exceed the limit")

    copied: Dict[str, ProviderDescriptorV1] = {}
    for key, descriptor in materialized:
        try:
            item = _copy_extension_provider_snapshot(descriptor)
        except _SNAPSHOT_CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            raise ValueError("extension provider snapshot is invalid") from None
        if key is not None and (type(key) is not str or key != item.id):
            raise ValueError("extension provider snapshot mapping is invalid")
        if item.id in copied:
            raise ValueError("extension provider snapshots contain a duplicate id")
        copied[item.id] = item
    return tuple(copied[provider_id] for provider_id in sorted(copied))


def _redacted_output(value: str, home: str) -> str:
    text = _safe_text(value, maximum=2_048)
    if home:
        text = text.replace(home, "~")
    for pattern in _SECRET_REPLACEMENTS:
        text = pattern.sub("[redacted]", text)
    return text


def _valid_image_signature(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def _decode_data_image(value: object) -> Attachment:
    if type(value) is not str:
        raise ManageError(400, "invalid_image", "Images must be canonical data URIs.")
    match = _DATA_IMAGE_RE.fullmatch(value)
    if match is None:
        raise ManageError(
            400, "invalid_image",
            "Only canonical base64 PNG, JPEG, or WebP data images are accepted.",
        )
    media_type, encoded = match.groups()
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ManageError(413, "image_too_large", "An image exceeds the size limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ManageError(400, "invalid_image", "An image is not valid base64 data.") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ManageError(413, "image_too_large", "An image exceeds the size limit.")
    if not _valid_image_signature(media_type, data):
        raise ManageError(400, "invalid_image", "An image does not match its MIME type.")
    return Attachment(bytes_=data, media_type=media_type)


def _minimal_verify_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    return env


def _run_verify_argv(argv: Tuple[str, ...], cwd: str) -> Dict[str, Any]:
    """Run one fixed verifier without shell, inherited secrets, or unbounded IO."""
    executable = shutil.which(argv[0], path=os.environ.get("PATH"))
    if executable is None:
        return {"ok": False, "code": "missing_binary", "output": ""}
    fixed_argv = (executable,) + argv[1:]
    try:
        proc = subprocess.Popen(
            fixed_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_minimal_verify_env(),
            **_popen_process_group_kwargs(),
        )
    except (OSError, ValueError):
        return {"ok": False, "code": "spawn_failed", "output": ""}

    overflow = threading.Event()
    sources: List[str] = []
    stdout: List[bytes] = []
    stderr: List[bytes] = []
    terminate = lambda: _terminate_process_tree(proc, force_group=True)
    threads = [
        threading.Thread(
            target=_drain_binary_into,
            kwargs={
                "pipe": proc.stdout, "sink": stdout,
                "max_bytes": MAX_VERIFY_OUTPUT_BYTES,
                "overflow": overflow, "source": sources,
                "source_name": "stdout", "terminate": terminate,
            }, daemon=True,
        ),
        threading.Thread(
            target=_drain_binary_into,
            kwargs={
                "pipe": proc.stderr, "sink": stderr,
                "max_bytes": MAX_VERIFY_OUTPUT_BYTES,
                "overflow": overflow, "source": sources,
                "source_name": "stderr", "terminate": terminate,
            }, daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=VERIFY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    finally:
        terminate()
        for thread in threads:
            thread.join(timeout=1)

    if timed_out:
        return {"ok": False, "code": "timeout", "output": ""}
    if overflow.is_set():
        return {"ok": False, "code": "output_limit", "output": ""}
    combined = b"\n".join((b"".join(stdout), b"".join(stderr))).decode(
        "utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "code": "ok" if proc.returncode == 0 else "not_ready",
        "output": _redacted_output(combined.strip(), os.environ.get("HOME", "")),
    }


class ManageRuntime:
    """Process-scoped state for one explicitly prepared manage server."""

    def __init__(
        self,
        workspaces: Sequence[str],
        *,
        provider_snapshots: object = None,
    ):
        self._lock = threading.RLock()
        self._handle_key = secrets.token_bytes(32)
        self._bootstrap_digest = _digest(_urlsafe_random())  # replaced immediately
        self._bootstrap_expires = 0.0
        self._sessions: "OrderedDict[str, BrowserSession]" = OrderedDict()
        self._active_chats: Dict[str, ActiveChat] = {}
        self._provider_active: Dict[str, str] = {}
        self._disabled = False
        self._verify_flights: Dict[Tuple[str, int], _ManageVerifyFlight] = {}
        self._verify_generations: Dict[str, int] = {}
        self._model_flights: Dict[
            Tuple[str, int, object], _ManageModelFlight
        ] = {}
        self._model_generations: Dict[str, int] = {}
        self._provider_identities: Dict[
            Tuple[str, str], Optional[_VerifyBinaryIdentity]
        ] = {}
        self._version_cache: "OrderedDict[object, Tuple[float, Tuple[bool, str, str]]]" = OrderedDict()
        self._auth_cache: "OrderedDict[object, Tuple[float, Tuple[bool, str]]]" = OrderedDict()
        self._models_cache: "OrderedDict[object, Tuple[float, Tuple[Tuple[object, ...], ...]]]" = OrderedDict()
        self._failed_bootstraps: Dict[str, Deque[float]] = {}
        self.session_manager = SessionManager()
        self.workspaces = self._prepare_workspaces(workspaces)
        self._workspace_by_id = {workspace.id: workspace for workspace in self.workspaces}
        self._extension_provider_snapshots = _copy_extension_provider_snapshots(
            provider_snapshots
        )
        self._extension_provider_ids = frozenset(
            descriptor.id for descriptor in self._extension_provider_snapshots
        )

    def _prepare_workspaces(self, values: Sequence[str]) -> Tuple[Workspace, ...]:
        if isinstance(values, (str, bytes)) or len(values) > 32:
            raise ValueError("workspaces must be a sequence of at most 32 paths")
        result: List[Workspace] = []
        seen: set[str] = set()
        for value in values:
            if type(value) is not str or not value or "\x00" in value:
                raise ValueError("workspace paths must be non-empty strings")
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("each workspace must be an existing directory")
            canonical = str(path)
            if canonical in seen:
                continue
            seen.add(canonical)
            result.append(Workspace(
                id=self._opaque_handle("workspace", canonical),
                path=canonical,
                name=_safe_text(path.name or canonical, maximum=200),
            ))
        return tuple(result)

    def issue_bootstrap(self) -> str:
        token = _urlsafe_random()
        with self._lock:
            self._require_enabled_locked()
            self._bootstrap_digest = _digest(token)
            self._bootstrap_expires = time.monotonic() + BOOTSTRAP_TTL_SECONDS
        return token

    def disable(self) -> None:
        with self._lock:
            self._disabled = True
            chats = list(self._active_chats.values())
            self._active_chats.clear()
            self._provider_active.clear()
            self._sessions.clear()
            self._provider_identities.clear()
            for name in _VERIFY_SPECS:
                self._verify_generations[name] = (
                    self._verify_generations.get(name, 0) + 1
                )
                self._model_generations[name] = (
                    self._model_generations.get(name, 0) + 1
                )
            self._version_cache.clear()
            self._auth_cache.clear()
            self._models_cache.clear()
            # Lock order is always ManageRuntime -> Core model cache.  Model
            # owners never retain the Core lock while reacquiring this lock.
            invalidate_core_model_cache()
            for flight in tuple(self._verify_flights.values()) + tuple(
                self._model_flights.values()
            ):
                if not flight.done.is_set():
                    if not flight.committed and flight.error is None:
                        flight.error = ManageError(
                            503,
                            "manage_disabled",
                            "The management runtime is disabled.",
                        )
                    flight.done.set()
            # Owners and waiters retain their direct flight references while
            # they unwind.  The disabled runtime itself must retain no active
            # work registry; owner cleanup uses identity checks and remains
            # safe when the entry has already been retired here.
            self._verify_flights.clear()
            self._model_flights.clear()
            self._bootstrap_digest = ""
            self._bootstrap_expires = 0.0
        for chat in chats:
            chat.cancel_event.set()

    def _require_enabled_locked(self) -> None:
        if self._disabled:
            raise ManageError(
                503,
                "manage_disabled",
                "The management runtime is disabled.",
            )

    @staticmethod
    def _cache_get(cache: OrderedDict, key: object) -> Optional[object]:
        now = time.monotonic()
        entry = cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if now >= expires_at:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return value

    @staticmethod
    def _cache_put(
        cache: OrderedDict, key: object, ttl: float, value: object
    ) -> None:
        cache[key] = (time.monotonic() + ttl, value)
        cache.move_to_end(key)
        while len(cache) > MAX_PROVIDER_CACHE_ENTRIES:
            cache.popitem(last=False)

    def _owner_fence_error_locked(self, flight: object) -> Optional[BaseException]:
        error = flight.error  # type: ignore[attr-defined]
        if error is None and self._disabled:
            error = ManageError(
                503,
                "manage_disabled",
                "The management runtime is disabled.",
            )
            flight.error = error  # type: ignore[attr-defined]
        return error

    def _clear_provider_entries_locked(
        self, provider_id: str, caches: Sequence[OrderedDict]
    ) -> None:
        for cache in caches:
            for key in tuple(cache):
                if isinstance(key, tuple) and key and key[0] == provider_id:
                    cache.pop(key, None)

    def _invalidate_verify_cache_locked(self, provider_id: str) -> None:
        self._clear_provider_entries_locked(
            provider_id, (self._version_cache, self._auth_cache)
        )
        self._verify_generations[provider_id] = (
            self._verify_generations.get(provider_id, 0) + 1
        )

    def _invalidate_model_cache_locked(self, provider_id: str) -> None:
        self._clear_provider_entries_locked(provider_id, (self._models_cache,))
        self._model_generations[provider_id] = (
            self._model_generations.get(provider_id, 0) + 1
        )
        # Keep this call under the Manage lock so invalidation has one visible
        # linearization point.  Core never calls back into Manage while holding
        # its cache lock, so the lock order cannot form a cycle.
        invalidate_core_model_cache(provider_id)  # type: ignore[arg-type]

    def _invalidate_provider_cache_locked(self, provider_id: str) -> None:
        self._clear_provider_entries_locked(
            provider_id,
            (self._version_cache, self._auth_cache),
        )
        self._verify_generations[provider_id] = (
            self._verify_generations.get(provider_id, 0) + 1
        )
        self._invalidate_model_cache_locked(provider_id)

    def invalidate_provider_cache(self, provider_id: Optional[str] = None) -> None:
        """Drop explicit verify/model probe results for one or all providers."""

        if provider_id is not None and provider_id not in _VERIFY_SPECS:
            raise ManageError(
                403, "provider_unsupported", "Provider cache invalidation is unsupported."
            )
        with self._lock:
            if provider_id is None:
                self._version_cache.clear()
                self._auth_cache.clear()
                self._models_cache.clear()
                self._provider_identities.clear()
                invalidate_core_model_cache()
                for name in _VERIFY_SPECS:
                    self._verify_generations[name] = (
                        self._verify_generations.get(name, 0) + 1
                    )
                    self._model_generations[name] = (
                        self._model_generations.get(name, 0) + 1
                    )
            else:
                self._invalidate_provider_cache_locked(provider_id)
                for key in tuple(self._provider_identities):
                    if key[1] == provider_id:
                        self._provider_identities.pop(key, None)

    def _observe_binary(
        self,
        scope: str,
        provider_id: str,
        identity: Optional[_VerifyBinaryIdentity],
    ) -> None:
        key = (scope, provider_id)
        with self._lock:
            if self._disabled:
                return
            if (
                key in self._provider_identities
                and self._provider_identities[key] != identity
            ):
                self._invalidate_provider_cache_locked(provider_id)
            self._provider_identities[key] = identity

    def _opaque_handle(self, namespace: str, value: str) -> str:
        digest = hmac.new(
            self._handle_key,
            (namespace + "\0" + value).encode("utf-8"),
            hashlib.sha256,
        ).digest()[:24]
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        prefix = {"workspace": "ws", "session": "sess"}.get(namespace, namespace)
        return prefix + "_" + encoded

    def bootstrap(
        self,
        *,
        supplied_token: Optional[str],
        supplied_csrf: Optional[str],
        cookie: Optional[str],
        peer_key: str,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        existing = self.authenticate(cookie, rate=False, required=False)
        if existing is not None:
            # Cookies are scoped to a host, not a TCP port.  Requiring the
            # origin-scoped browser proof as well makes a cookie observed by a
            # different localhost service insufficient to use this runtime.
            self.check_csrf(existing, supplied_csrf)
            return self._bootstrap_payload(existing), None

        now = time.monotonic()
        raw_cookie = _urlsafe_random()
        key = _digest(raw_cookie)
        session = BrowserSession(
            key=key, csrf_token=_urlsafe_random(),
            created_at=time.time(), last_seen=now,
        )
        with self._lock:
            self._require_enabled_locked()
            attempts = self._failed_bootstraps.setdefault(_digest(peer_key), deque())
            while attempts and now - attempts[0] > 60.0:
                attempts.popleft()
            if len(attempts) >= 10:
                raise ManageError(429, "rate_limited", "Too many bootstrap attempts.")
            if (
                type(supplied_token) is not str
                or not self._bootstrap_digest
                or now > self._bootstrap_expires
                or not hmac.compare_digest(
                    _digest(supplied_token), self._bootstrap_digest)
            ):
                attempts.append(now)
                raise ManageError(401, "bootstrap_required", "A valid bootstrap token is required.")
            # Validation and consumption share one lock: at most one racing
            # request can turn the one-time secret into a browser session.
            self._bootstrap_digest = ""
            self._bootstrap_expires = 0.0
            self._sessions[key] = session
            while len(self._sessions) > MAX_MANAGE_SESSIONS:
                self._sessions.popitem(last=False)
        return self._bootstrap_payload(session), raw_cookie

    def _bootstrap_payload(self, session: BrowserSession) -> Dict[str, Any]:
        with self._lock:
            self._require_enabled_locked()
            return self._bootstrap_payload_locked(session)

    def _bootstrap_payload_locked(self, session: BrowserSession) -> Dict[str, Any]:
        settings = self.safe_settings()
        default_workspace = self._workspace_by_id.get(settings["workspace_id"])
        release_version = self._core_version()
        versions = {
            "unified_cli": release_version,
            # The extension namespace ships in the same distribution.  Do not
            # import it or perform entry-point discovery during bootstrap.
            "unified_cli_ext": release_version,
        }
        return {
            "version": 1,
            "versions": versions,
            "mode": "manage",
            "manage": True,
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "lang": settings["lang"],
            "providers": self.provider_metadata()["providers"],
            "workspaces": [
                {"id": workspace.id, "name": workspace.name}
                for workspace in self.workspaces
            ],
            "settings": settings,
            "defaults": {
                "provider": settings["default_provider"],
                "model": DEFAULT_MODELS[settings["default_provider"]],
                "workspace": settings["workspace_id"],
                "workspace_name": (
                    default_workspace.name if default_workspace is not None else None
                ),
            },
            "security": {
                "web": settings["web"],
                "mcp": False,
                "workspaces": len(self.workspaces),
                "server_allowlist": ["claude", "codex"],
            },
            "limits": {
                "prompt_chars": MAX_PROMPT_CHARS,
                "images": MAX_IMAGES,
                "image_bytes": MAX_IMAGE_BYTES,
                "image_total_bytes": MAX_IMAGE_TOTAL_BYTES,
                "active_chats": MAX_ACTIVE_CHATS,
            },
        }

    @staticmethod
    def _core_version() -> str:
        # Importing the package version does not enumerate or import plugins.
        from . import __version__
        return __version__

    def authenticate(
        self,
        cookie: Optional[str],
        *,
        rate: bool = True,
        mutation: bool = False,
        required: bool = True,
    ) -> Optional[BrowserSession]:
        if type(cookie) is not str or not cookie or len(cookie) > 128:
            if required:
                raise ManageError(401, "session_required", "A manage session is required.")
            return None
        key = _digest(cookie)
        with self._lock:
            self._require_enabled_locked()
            session = self._sessions.get(key)
            if session is None:
                if required:
                    raise ManageError(401, "session_required", "A manage session is required.")
                return None
            self._sessions.move_to_end(key)
            session.last_seen = time.monotonic()
            if rate:
                self._check_rate(session, mutation=mutation)
            return session

    @staticmethod
    def _check_rate(session: BrowserSession, *, mutation: bool) -> None:
        now = time.monotonic()
        queue = session.mutations if mutation else session.requests
        limit = 30 if mutation else 120
        while queue and now - queue[0] > 60.0:
            queue.popleft()
        if len(queue) >= limit:
            raise ManageError(429, "rate_limited", "Too many manage requests.")
        queue.append(now)

    @staticmethod
    def check_csrf(session: BrowserSession, supplied: Optional[str]) -> None:
        if (
            type(supplied) is not str
            or len(supplied) > 128
            or not hmac.compare_digest(supplied, session.csrf_token)
        ):
            raise ManageError(403, "csrf_required", "A valid CSRF token is required.")

    def safe_settings(self) -> Dict[str, Any]:
        settings = load_settings()
        workspace_id = None
        if settings.workspace:
            canonical = os.path.realpath(settings.workspace)
            workspace_id = next(
                (item.id for item in self.workspaces if item.path == canonical), None)
        return {
            "lang": settings.lang if settings.lang in {"en", "ko"} else "en",
            "theme": settings.theme if settings.theme in {"auto", "light", "dark"} else "auto",
            "reasoning_display": (
                settings.reasoning_display
                if settings.reasoning_display in {"hidden", "compact"} else "hidden"
            ),
            "tool_display": (
                settings.tool_display
                if settings.tool_display in {"hidden", "compact"} else "compact"
            ),
            "browser_permission": "read_only",
            "browser_prompt_preview": bool(settings.browser_prompt_preview),
            "default_provider": (
                settings.default_provider
                if settings.default_provider in {"claude", "codex", "gemini"} else "claude"
            ),
            "workspace_id": workspace_id,
            "web": settings.web is True,
        }

    def patch_settings(self, payload: object) -> Dict[str, Any]:
        if type(payload) is not dict:
            raise ManageError(400, "invalid_settings", "Settings must be an object.")
        allowed = {
            "lang", "theme", "reasoning_display", "tool_display", "browser_permission",
            "browser_prompt_preview", "default_provider", "workspace_id", "web",
        }
        if not payload or not set(payload).issubset(allowed):
            raise ManageError(400, "invalid_settings", "Settings contain unsupported keys.")
        current = load_settings()
        candidate = replace(current)
        for key, value in payload.items():
            if key == "lang" and type(value) is str and value in {"en", "ko"}:
                candidate.lang = value
            elif key == "theme" and type(value) is str and value in {"auto", "light", "dark"}:
                candidate.theme = value
            elif key == "reasoning_display" and type(value) is str and value in {"hidden", "compact"}:
                candidate.reasoning_display = value
            elif key == "tool_display" and type(value) is str and value in {"hidden", "compact"}:
                candidate.tool_display = value
            elif key == "browser_permission" and value == "read_only":
                candidate.browser_permission = "read_only"
            elif key == "browser_prompt_preview" and type(value) is bool:
                candidate.browser_prompt_preview = value
            elif key == "default_provider" and type(value) is str and value in {"claude", "codex", "gemini"}:
                candidate.default_provider = value
            elif key == "workspace_id" and (value is None or type(value) is str):
                if value is None:
                    candidate.workspace = None
                else:
                    workspace = self._workspace_by_id.get(value)
                    if workspace is None:
                        raise ManageError(400, "invalid_workspace", "Workspace is not registered.")
                    candidate.workspace = workspace.path
            elif key == "web" and type(value) is bool:
                candidate.web = value
            else:
                raise ManageError(400, "invalid_settings", "A setting value is not allowed.")
        with self._lock:
            self._require_enabled_locked()
            try:
                save_settings(candidate)
            except (OSError, TypeError, ValueError):
                raise ManageError(500, "settings_failed", "Settings could not be saved.") from None
        return self.safe_settings()

    def provider_metadata(self) -> Dict[str, Any]:
        with self._lock:
            self._require_enabled_locked()
            return self._provider_metadata_locked()

    def _provider_metadata_locked(self) -> Dict[str, Any]:
        # Core listing is a pure in-process snapshot. Ext entry points are
        # never enumerated here; only constructor-injected Core-owned copies
        # are eligible for display.
        descriptors = list_providers(include_ext=False)
        rows = []
        for descriptor in descriptors:
            policy = descriptor.server_policy
            row: Dict[str, Any] = {
                "id": descriptor.id,
                "source": descriptor.source,
                "status": descriptor.status,
                "support_status": descriptor.support_status,
                "default_model": descriptor.default_model,
                "capabilities": sorted(descriptor.capabilities),
                "server_policy": {
                    "enabled": bool(policy is not None and policy.enabled is True),
                    "requires_external_isolation": bool(
                        policy is None
                        or policy.requires_external_isolation is True
                    ),
                },
                "chat_supported": descriptor.id in {"claude", "codex"},
                "verify_supported": descriptor.id in _VERIFY_SPECS,
                "models_supported": descriptor.id in _CORE_PROVIDER_IDS,
                "default_supported": descriptor.id in _CORE_PROVIDER_IDS,
                "metadata_only": False,
            }
            if descriptor.id in _COPY_COMMANDS:
                commands = dict(_COPY_COMMANDS[descriptor.id])
                row["commands"] = commands
                row["install_command"] = commands["install"]
                row["login_command"] = commands["login"]
            if descriptor.error:
                row["error"] = descriptor.error
            rows.append(row)
        for descriptor in self._extension_provider_snapshots:
            policy = descriptor.server_policy
            rows.append({
                "id": descriptor.id,
                "source": "extension",
                "status": "loaded",
                "support_status": descriptor.support_status,
                "default_model": descriptor.default_model,
                "capabilities": sorted(descriptor.capabilities),
                "server_policy": {
                    "enabled": False,
                    "requires_external_isolation": bool(
                        policy is None
                        or policy.requires_external_isolation is True
                    ),
                },
                "chat_supported": False,
                "verify_supported": False,
                "models_supported": False,
                "default_supported": False,
                "metadata_only": True,
            })
        return {"providers": rows}

    def _is_extension_provider_id(self, value: object) -> bool:
        return (
            type(value) is str
            and len(value) <= 64
            and value in self._extension_provider_ids
        )

    @staticmethod
    def _provider_unsupported() -> ManageError:
        return ManageError(
            403,
            "provider_unsupported",
            "This provider operation is unavailable in browser management.",
        )

    def provider_models(
        self, provider_id: str, *, force_refresh: bool = False
    ) -> Dict[str, Any]:
        if type(provider_id) is not str or provider_id not in _CORE_PROVIDER_IDS:
            raise ManageError(403, "provider_unsupported", "Provider models are unavailable.")

        with self._lock:
            if self._disabled:
                raise ManageError(
                    503, "manage_disabled", "The management runtime is disabled."
                )

        identity = _effective_model_binary_identity(provider_id)
        self._observe_binary("models", provider_id, identity)
        context = _environment_identity(provider_id)
        key = (provider_id, identity, context)
        identity_is_current = identity is None or identity.current()
        with self._lock:
            if self._disabled:
                raise ManageError(
                    503, "manage_disabled", "The management runtime is disabled."
                )
            if not force_refresh and identity_is_current:
                record = self._cache_get(self._models_cache, key)
                if record is not None:
                    return self._models_response(provider_id, record)

            generation = self._model_generations.get(provider_id, 0)
            flight_key = (provider_id, generation, key)
            flight = self._model_flights.get(flight_key)
            if force_refresh and not (
                flight is not None and flight.force_refresh
            ):
                # A Manage force refresh fences both its own previous flight
                # and Core's corresponding provider flight.  Otherwise Core
                # would legitimately coalesce with the older force probe.
                self._invalidate_model_cache_locked(provider_id)
                generation = self._model_generations[provider_id]
                flight_key = (provider_id, generation, key)
                flight = self._model_flights.get(flight_key)
            if flight is None:
                if len(self._model_flights) >= MAX_PROVIDER_CACHE_ENTRIES:
                    raise ManageError(
                        429,
                        "models_busy",
                        "Provider model discovery is at capacity.",
                    )
                flight = _ManageModelFlight(
                    key, force_refresh=force_refresh
                )
                self._model_flights[flight_key] = flight
                owner = True
            else:
                owner = False

        if not owner:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return self._models_response(provider_id, flight.result)

        try:
            with self._lock:
                fence_error = self._owner_fence_error_locked(flight)
            if fence_error is not None:
                raise fence_error
            fallback_fence_error = None
            try:
                # ManageRuntime owns the shorter UI TTL, so a miss must not be
                # silently extended by Core's longer process cache.
                try:
                    models = list_models(provider_id, force_refresh=True)
                except TypeError as error:
                    # Preserve compatibility with downstream/test listers that
                    # implement the historical one-argument callable shape.
                    if "force_refresh" not in str(error):
                        raise
                    with self._lock:
                        fallback_fence_error = (
                            self._owner_fence_error_locked(flight)
                        )
                    if fallback_fence_error is None:
                        models = list_models(provider_id)
            except UnifiedError as error:
                if error.kind == "resource_limit":
                    raise ManageError(
                        429,
                        "models_busy",
                        "Provider model discovery is busy. Retry shortly.",
                    ) from None
                raise ManageError(
                    502,
                    "models_unavailable",
                    "Provider models are unavailable.",
                ) from None
            except Exception:
                raise ManageError(
                    502,
                    "models_unavailable",
                    "Provider models are unavailable.",
                ) from None
            if fallback_fence_error is not None:
                raise fallback_fence_error

            rows = tuple(
                (
                    item.id,
                    item.display_name,
                    bool(item.default),
                    bool(item.deprecated),
                    item.source,
                )
                for item in models[:1_000]
                if item.provider == provider_id
                and type(item.id) is str
                and len(item.id) <= 512
            )
            current_identity = _effective_model_binary_identity(provider_id)
            commit_error = None
            with self._lock:
                if flight.error is not None:
                    commit_error = flight.error
                elif self._disabled:
                    commit_error = ManageError(
                        503,
                        "manage_disabled",
                        "The management runtime is disabled.",
                    )
                    flight.error = commit_error
                else:
                    if (
                        rows
                        and self._model_generations.get(provider_id, 0) == generation
                        and current_identity == identity
                        and (identity is None or identity.current())
                    ):
                        self._cache_put(
                            self._models_cache,
                            key,
                            PROVIDER_MODELS_TTL_SECONDS,
                            rows,
                        )
                    flight.result = rows
                    flight.committed = True
            if commit_error is not None:
                raise commit_error
            if current_identity != identity:
                self._observe_binary("models", provider_id, current_identity)
            return self._models_response(provider_id, rows)
        except BaseException as error:
            with self._lock:
                if flight.error is None:
                    flight.error = error
                public_error = flight.error
            if public_error is error:
                raise
            raise public_error
        finally:
            with self._lock:
                if self._model_flights.get(flight_key) is flight:
                    self._model_flights.pop(flight_key, None)
                flight.done.set()

    @staticmethod
    def _models_response(
        provider_id: str, rows: Tuple[Tuple[object, ...], ...]
    ) -> Dict[str, Any]:
        return {
            "provider": provider_id,
            "models": [
                {
                    "id": row[0],
                    "display_name": row[1],
                    "default": row[2],
                    "deprecated": row[3],
                    "source": row[4],
                }
                for row in rows
            ],
        }

    def verify_provider(
        self, provider_id: str, *, force_refresh: bool = False
    ) -> Dict[str, Any]:
        if self._is_extension_provider_id(provider_id):
            raise self._provider_unsupported()
        specs = (
            _VERIFY_SPECS.get(provider_id)
            if type(provider_id) is str and len(provider_id) <= 64
            else None
        )
        if specs is None:
            raise ManageError(403, "verify_unsupported", "Provider verification is unsupported.")

        while True:
            with self._lock:
                if self._disabled:
                    raise ManageError(
                        503,
                        "manage_disabled",
                        "The management runtime is disabled.",
                    )

            identity = _verify_binary_identity(provider_id, specs[0][0])
            self._observe_binary("verify", provider_id, identity)
            context = _environment_identity(provider_id)
            version_key = (provider_id, identity)
            auth_key = (provider_id, identity, context)
            version_record = None
            auth_record = None
            if not force_refresh and identity is not None and identity.current():
                with self._lock:
                    if self._disabled:
                        raise ManageError(
                            503,
                            "manage_disabled",
                            "The management runtime is disabled.",
                        )
                    version_record = self._cache_get(
                        self._version_cache, version_key
                    )
                    if len(specs) > 1:
                        auth_record = self._cache_get(
                            self._auth_cache, auth_key
                        )

            need_version = version_record is None
            need_auth = len(specs) > 1 and auth_record is None
            if not need_version and not need_auth:
                return self._verify_response(
                    provider_id, version_record, auth_record
                )

            flight_context = (identity, context)
            wait_for = None
            with self._lock:
                if self._disabled:
                    raise ManageError(
                        503,
                        "manage_disabled",
                        "The management runtime is disabled.",
                    )
                generation = self._verify_generations.get(provider_id, 0)
                flight_key = (provider_id, generation)
                active = next(
                    (
                        (key, value)
                        for key, value in self._verify_flights.items()
                        if key[0] == provider_id
                    ),
                    None,
                )
                if active is not None:
                    active_key, flight = active
                    if (
                        active_key == flight_key
                        and flight.context == flight_context
                    ):
                        if force_refresh and not flight.force_refresh:
                            # A force caller must not consume the ordinary
                            # result, but verification remains strictly serial.
                            wait_for = flight
                        else:
                            owner = False
                            flight_key = active_key
                    elif not force_refresh and active_key == flight_key:
                        raise ManageError(
                            429,
                            "verify_busy",
                            "This provider already has a verification running.",
                        )
                    else:
                        # The active flight belongs to a fenced generation (or
                        # a force caller's previous context). Recompute state
                        # only after its provider slot has been released.
                        wait_for = flight
                else:
                    if force_refresh:
                        self._invalidate_verify_cache_locked(provider_id)
                        generation = self._verify_generations[provider_id]
                        flight_key = (provider_id, generation)
                        version_record = None
                        auth_record = None
                        need_version = True
                        need_auth = len(specs) > 1
                    flight = _ManageVerifyFlight(
                        flight_context, force_refresh=force_refresh
                    )
                    self._verify_flights[flight_key] = flight
                    owner = True

            if wait_for is not None:
                wait_for.done.wait()
                with self._lock:
                    shutdown_error = (
                        self._owner_fence_error_locked(wait_for)
                        if self._disabled
                        else None
                    )
                if shutdown_error is not None:
                    raise shutdown_error
                continue
            break

        if not owner:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return self._verify_response(
                provider_id, flight.result[0], flight.result[1]
            )

        try:
            with tempfile.TemporaryDirectory(prefix="unified-cli-verify-") as cwd:
                if need_version:
                    with self._lock:
                        fence_error = self._owner_fence_error_locked(flight)
                    if fence_error is not None:
                        raise fence_error
                    result = _run_verify_argv(specs[0], cwd)
                    version_record = (
                        bool(result["ok"]),
                        str(result["code"]),
                        str(result["output"]),
                    )
                if (
                    need_auth
                    and version_record is not None
                    and version_record[1] != "missing_binary"
                ):
                    with self._lock:
                        fence_error = self._owner_fence_error_locked(flight)
                    if fence_error is not None:
                        raise fence_error
                    result = _run_verify_argv(specs[1], cwd)
                    # Authentication output may contain an account identifier;
                    # only the bounded boolean classification is retained.
                    auth_record = (bool(result["ok"]), str(result["code"]))
            current_identity = _verify_binary_identity(provider_id, specs[0][0])
            if current_identity != identity or (
                identity is not None and not identity.current()
            ):
                self._observe_binary("verify", provider_id, current_identity)
                version_record = (False, "binary_changed", "")
                auth_record = None
            assert version_record is not None
            commit_error = None
            with self._lock:
                commit_error = self._owner_fence_error_locked(flight)
                if commit_error is None:
                    if self._verify_generations.get(provider_id, 0) == generation:
                        if identity is not None and version_record[0]:
                            self._cache_put(
                                self._version_cache,
                                version_key,
                                VERIFY_VERSION_TTL_SECONDS,
                                version_record,
                            )
                        if (
                            identity is not None
                            and auth_record is not None
                            and auth_record[1] in {"ok", "not_ready"}
                        ):
                            self._cache_put(
                                self._auth_cache,
                                auth_key,
                                VERIFY_AUTH_TTL_SECONDS,
                                auth_record,
                            )
                    flight.result = (version_record, auth_record)
                    flight.committed = True
            if commit_error is not None:
                raise commit_error
            return self._verify_response(
                provider_id, version_record, auth_record
            )
        except BaseException as error:
            with self._lock:
                if flight.error is None:
                    flight.error = error
                public_error = flight.error
            if public_error is error:
                raise
            raise public_error
        finally:
            with self._lock:
                if self._verify_flights.get(flight_key) is flight:
                    self._verify_flights.pop(flight_key, None)
                flight.done.set()

    @staticmethod
    def _verify_response(
        provider_id: str,
        version_record: Tuple[bool, str, str],
        auth_record: Optional[Tuple[bool, str]],
    ) -> Dict[str, Any]:
        results = [{
            "check": "version",
            "ok": version_record[0],
            "code": version_record[1],
            "output": version_record[2],
        }]
        if auth_record is not None:
            results.append({
                "check": "auth_status",
                "ok": auth_record[0],
                "code": auth_record[1],
                "output": "",
            })
        installed = bool(version_record[0])
        auth = "unknown"
        if auth_record is not None:
            auth = "authenticated" if auth_record[0] else "not_authenticated"
        ready = installed and (auth == "authenticated" or provider_id == "gemini")
        return {
            "provider": provider_id,
            "installed": installed,
            "ready": ready,
            "status": "ready" if ready else "unavailable",
            "auth": auth,
            "checks": results,
            "commands": dict(_COPY_COMMANDS[provider_id]),
        }

    def _record_to_safe(self, record: SessionRecord) -> Dict[str, Any]:
        workspace_id = next(
            (item.id for item in self.workspaces
             if item.path == os.path.realpath(record.cwd)),
            None,
        ) if record.cwd else None
        return {
            "id": self._opaque_handle(
                "session", record.provider + "\0" + record.session_id),
            "provider": record.provider,
            "model": _safe_text(record.model, maximum=512),
            "name": _safe_text(record.name, maximum=200),
            "workspace_id": workspace_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "archived": bool(record.archived),
        }

    def list_sessions(self) -> Dict[str, Any]:
        with self._lock:
            self._require_enabled_locked()
            try:
                records = self.session_manager.list(include_archived=True)
            except (OSError, ValueError):
                raise ManageError(500, "sessions_failed", "Sessions could not be read.") from None
            return {"sessions": [self._record_to_safe(record) for record in records]}

    def _resolve_session(self, handle: object) -> SessionRecord:
        if type(handle) is not str or _HANDLE_RE.fullmatch(handle) is None:
            raise ManageError(404, "session_not_found", "Session was not found.")
        try:
            records = self.session_manager.list(include_archived=True)
        except (OSError, ValueError):
            raise ManageError(500, "sessions_failed", "Sessions could not be read.") from None
        found = None
        for record in records:
            candidate = self._opaque_handle(
                "session", record.provider + "\0" + record.session_id)
            if hmac.compare_digest(handle, candidate):
                found = record
        if found is None:
            raise ManageError(404, "session_not_found", "Session was not found.")
        return found

    def patch_session(self, handle: str, payload: object) -> Dict[str, Any]:
        if type(payload) is not dict or not payload or not set(payload).issubset({"name", "archived"}):
            raise ManageError(400, "invalid_session", "Session changes are invalid.")
        name = payload.get("name")
        if "name" in payload and (
            type(name) is not str or not name.strip() or len(name) > 200
        ):
            raise ManageError(400, "invalid_session", "Session name is invalid.")
        archived = payload.get("archived")
        if "archived" in payload and type(archived) is not bool:
            raise ManageError(400, "invalid_session", "Archived must be a boolean.")
        record = self._resolve_session(handle)
        with self._lock:
            self._require_enabled_locked()
            try:
                if "name" in payload:
                    record = self.session_manager.rename(
                        provider=record.provider, session_id=record.session_id, name=name)
                if "archived" in payload:
                    record = self.session_manager.archive(
                        provider=record.provider, session_id=record.session_id,
                        archived=archived)
            except ManageError:
                raise
            except (KeyError, OSError, ValueError):
                raise ManageError(404, "session_not_found", "Session was not found.") from None
        return {"session": self._record_to_safe(record)}

    def delete_session(self, handle: str) -> Dict[str, Any]:
        record = self._resolve_session(handle)
        with self._lock:
            self._require_enabled_locked()
            try:
                removed = self.session_manager.delete(
                    provider=record.provider, session_id=record.session_id)
            except (OSError, ValueError):
                raise ManageError(500, "sessions_failed", "Session could not be deleted.") from None
        if not removed:
            raise ManageError(404, "session_not_found", "Session was not found.")
        return {"deleted": True, "id": handle}

    def usage_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._require_enabled_locked()
            snapshot = tracker.snapshot()
            settings = load_settings()
            recent = []
            for record in snapshot.get("recent", [])[:100]:
                error_kind = record.get("error_kind")
                safe = {
                    "timestamp": record.get("ts"),
                    "provider": record.get("provider"),
                    "model": record.get("model"),
                    "input_tokens": record.get("input_tokens"),
                    "output_tokens": record.get("output_tokens"),
                    "cached_tokens": record.get("cached_tokens"),
                    "latency_ms": record.get("latency_ms"),
                    "error_code": error_kind,
                    "status": "error" if error_kind else "success",
                }
                if settings.browser_prompt_preview:
                    safe["prompt_preview"] = _safe_text(
                        record.get("prompt_preview", ""), maximum=60)
                recent.append(safe)
            aggregates = snapshot.get("aggregates", [])
            return {
                "aggregates": aggregates,
                "totals": {
                    "input_tokens": sum(int(row.get("input_tokens") or 0) for row in aggregates),
                    "output_tokens": sum(int(row.get("output_tokens") or 0) for row in aggregates),
                    "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in aggregates),
                },
                "recent": recent,
            }

    def _workspace(self, value: object) -> Workspace:
        if type(value) is not str or len(value) > 80:
            raise ManageError(400, "invalid_workspace", "Workspace is not registered.")
        workspace = self._workspace_by_id.get(value)
        if workspace is None:
            raise ManageError(403, "workspace_forbidden", "Workspace is not registered.")
        return workspace

    @staticmethod
    def _model(value: object, provider: str) -> str:
        if value is None:
            return DEFAULT_MODELS[provider]  # type: ignore[index]
        if (
            type(value) is not str or not value.strip() or len(value) > 256
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ManageError(400, "invalid_model", "Model is invalid.")
        return value

    def _resume_session(
        self, handle: object, provider: str, workspace: Workspace,
    ) -> Tuple[Optional[str], Optional[str]]:
        if handle is None:
            return None, None
        record = self._resolve_session(handle)
        if record.provider != provider:
            raise ManageError(403, "session_forbidden", "Session does not match the provider.")
        if not record.cwd or os.path.realpath(record.cwd) != workspace.path:
            raise ManageError(403, "session_forbidden", "Session does not match the workspace.")
        return record.session_id, handle  # native id remains inside the runtime

    def start_chat(self, payload: object, owner_key: str) -> ActiveChat:
        if type(payload) is not dict:
            raise ManageError(400, "invalid_chat", "Chat request must be an object.")
        allowed = {
            "provider", "model", "prompt", "workspace", "workspace_id", "permission",
            "session_id", "images",
        }
        if not set(payload).issubset(allowed):
            raise ManageError(400, "invalid_chat", "Chat request contains unsupported fields.")
        provider = payload.get("provider")
        if self._is_extension_provider_id(provider):
            raise self._provider_unsupported()
        if type(provider) is not str or provider not in {"claude", "codex"}:
            raise ManageError(403, "provider_forbidden", "Provider is unavailable for browser chat.")
        if payload.get("permission", "read_only") != "read_only":
            raise ManageError(403, "permission_forbidden", "Browser chat is read-only.")
        with self._lock:
            self._require_enabled_locked()
        prompt = payload.get("prompt")
        if type(prompt) is not str or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ManageError(400, "invalid_prompt", "Prompt is empty or exceeds the limit.")
        if "workspace" in payload and "workspace_id" in payload:
            raise ManageError(400, "invalid_workspace", "Specify one workspace identifier.")
        workspace = self._workspace(payload.get("workspace_id", payload.get("workspace")))
        model = self._model(payload.get("model"), provider)
        native_session, browser_session = self._resume_session(
            payload.get("session_id"), provider, workspace)
        raw_images = payload.get("images", [])
        if type(raw_images) is not list or len(raw_images) > MAX_IMAGES:
            raise ManageError(413, "too_many_images", "Too many images were supplied.")
        images = []
        total_image_bytes = 0
        for value in raw_images:
            if type(value) is dict:
                if set(value) != {"name", "media_type", "data"}:
                    raise ManageError(400, "invalid_image", "Image fields are invalid.")
                name = value.get("name")
                media_type = value.get("media_type")
                data = value.get("data")
                if type(name) is not str or len(name) > 180 or "\x00" in name:
                    raise ManageError(400, "invalid_image", "Image name is invalid.")
                if type(media_type) is not str or type(data) is not str:
                    raise ManageError(400, "invalid_image", "Image fields are invalid.")
                value = "data:" + media_type + ";base64," + data
            image = _decode_data_image(value)
            total_image_bytes += len(image.bytes_ or b"")
            if total_image_bytes > MAX_IMAGE_TOTAL_BYTES:
                raise ManageError(
                    413, "images_too_large",
                    "The combined images exceed the size limit.",
                )
            images.append(image)

        with self._lock:
            self._require_enabled_locked()
            if len(self._active_chats) >= MAX_ACTIVE_CHATS:
                raise ManageError(429, "chat_capacity", "Too many chats are active.")
            if provider in self._provider_active:
                raise ManageError(429, "provider_busy", "This provider already has an active chat.")
            chat_id = "chat_" + _urlsafe_random()
            chat = ActiveChat(
                id=chat_id, owner_key=owner_key, provider=provider,
                workspace=workspace, model=model, prompt=prompt, images=images,
                native_session_id=native_session,
                browser_session_id=browser_session,
            )
            self._active_chats[chat_id] = chat
            self._provider_active[provider] = chat_id
            return chat

    def finish_chat(self, chat_id: str) -> None:
        with self._lock:
            chat = self._active_chats.pop(chat_id, None)
            if chat is not None and self._provider_active.get(chat.provider) == chat_id:
                del self._provider_active[chat.provider]

    def cancel_chat(self, chat_id: str, owner_key: str) -> Dict[str, Any]:
        if type(chat_id) is not str or _HANDLE_RE.fullmatch(chat_id) is None:
            raise ManageError(404, "chat_not_found", "Chat was not found.")
        with self._lock:
            self._require_enabled_locked()
            chat = self._active_chats.get(chat_id)
            if chat is None or not hmac.compare_digest(chat.owner_key, owner_key):
                raise ManageError(404, "chat_not_found", "Chat was not found.")
            chat.cancel_event.set()
        return {"id": chat_id, "cancelled": True}

    def _conversation_for_chat(self, chat: ActiveChat) -> UnifiedConversation:
        web = load_settings().web is True
        if chat.provider == "claude":
            tools = ["Read", "Glob", "Grep"]
            if web:
                tools += ["WebSearch", "WebFetch"]
            provider_options = {
                "cwd": chat.workspace.path,
                "safe_mode": True,
                "permission_mode": "plan",
                "tools": tools,
                "allowed_tools": tools,
                "web_search": web,
            }
        elif chat.provider == "codex":
            provider_options = {
                "cwd": chat.workspace.path,
                "sandbox": "read-only",
                "ignore_user_config": True,
                "ignore_rules": True,
                "web_search": web,
                # Codex's `exec resume` does not accept `-s`; the validated
                # TOML override is shared by fresh and resumed argv and keeps
                # an older workspace-write session read-only in the browser.
                "config_overrides": {"sandbox_mode": "read-only"},
            }
        else:  # defensive: start_chat has already fail-closed
            raise ManageError(403, "provider_forbidden", "Provider is unavailable for browser chat.")
        conversation = UnifiedConversation(
            default_provider=chat.provider,
            default_model=chat.model,
            sticky=True,
            cross_provider_context=False,
            provider_opts_by_provider={chat.provider: provider_options},
            max_turns=8,
            max_turn_chars=4_096,
            max_clients=1,
        )
        if chat.native_session_id:
            conversation.sessions[chat.provider] = chat.native_session_id
            conversation.turns.append(Turn(provider=chat.provider, prompt="", text=""))
        return conversation

    @staticmethod
    def _line(event: Mapping[str, Any]) -> bytes:
        return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _public_reasoning(message: Message) -> bool:
        raw = message.raw
        if not isinstance(raw, dict):
            return False
        visibility = raw.get("visibility")
        event_type = raw.get("type")
        return (
            raw.get("public_summary") is True
            or visibility == "public" or visibility == "summary"
            or event_type == "reasoning_summary" or event_type == "summary"
        )

    def stream_chat(self, chat: ActiveChat) -> Iterator[bytes]:
        with self._lock:
            self._require_enabled_locked()
        conversation = self._conversation_for_chat(chat)
        initial: Dict[str, Any] = {"type": "session", "chat_id": chat.id}
        if chat.browser_session_id:
            initial["session_id"] = chat.browser_session_id
        yield self._line(initial)
        tool_handles: Dict[str, Tuple[str, str]] = {}
        tool_counter = 0
        event_count = 1
        text_chars = 0
        reasoning_enabled = load_settings().reasoning_display == "compact"
        upstream = None
        try:
            with self._lock:
                self._require_enabled_locked()
            upstream = conversation.stream(
                chat.prompt,
                provider=chat.provider,
                model=chat.model,
                images=chat.images or None,
                cancel_event=chat.cancel_event,
            )
            for message in upstream:
                event_count += 1
                if event_count > MAX_STREAM_EVENTS:
                    chat.cancel_event.set()
                    yield self._line({
                        "type": "error", "code": "event_limit",
                        "message": "Provider output exceeded the event limit.",
                    })
                    break
                if message.kind == "session" and message.session_id:
                    native = message.session_id
                    try:
                        record = self.session_manager.upsert(
                            provider=chat.provider,
                            session_id=native,
                            model=chat.model,
                            cwd=chat.workspace.path,
                        )
                    except (OSError, ValueError):
                        continue
                    handle = self._record_to_safe(record)["id"]
                    chat.native_session_id = native
                    chat.browser_session_id = handle
                    yield self._line({
                        "type": "session", "chat_id": chat.id,
                        "session_id": handle,
                    })
                elif message.kind == "text" and isinstance(message.text, str) and message.text:
                    remaining = MAX_STREAM_TEXT_CHARS - text_chars
                    if remaining <= 0:
                        chat.cancel_event.set()
                        yield self._line({
                            "type": "error", "code": "text_limit",
                            "message": "Provider text exceeded the response limit.",
                        })
                        break
                    text = _safe_text(message.text, maximum=remaining)
                    if text:
                        text_chars += min(len(message.text), remaining)
                        yield self._line({"type": "text_delta", "text": text})
                elif (
                    message.kind == "reasoning" and message.text
                    and reasoning_enabled and self._public_reasoning(message)
                ):
                    yield self._line({
                        "type": "reasoning_summary",
                        "text": _safe_text(message.text, maximum=4_096),
                    })
                elif message.kind == "tool_use":
                    tool = message.tool if isinstance(message.tool, dict) else {}
                    raw_id = tool.get("id")
                    if type(raw_id) is not str or not raw_id or len(raw_id) > 256:
                        continue
                    tool_counter += 1
                    public_id = "tool_" + str(tool_counter)
                    name = _safe_text(tool.get("name") or "tool", maximum=80)
                    tool_handles[raw_id] = (public_id, name)
                    yield self._line({
                        "type": "tool_started", "id": public_id, "name": name,
                    })
                elif message.kind == "tool_result":
                    tool = message.tool if isinstance(message.tool, dict) else {}
                    raw_id = tool.get("id")
                    matched = tool_handles.pop(raw_id, None) if isinstance(raw_id, str) else None
                    if matched is None:
                        continue
                    public_id, name = matched
                    yield self._line({
                        "type": "tool_finished", "id": public_id, "name": name,
                        "status": "error" if bool(tool.get("is_error")) else "ok",
                    })
                elif message.kind == "usage" and message.usage:
                    yield self._line({
                        "type": "usage", **self._safe_usage(message.usage),
                    })
                elif message.kind == "error":
                    yield self._line({
                        "type": "error", "code": "provider_error",
                        "message": "The provider reported an error.",
                    })
                elif message.kind == "done":
                    pass
            status = "cancelled" if chat.cancel_event.is_set() else "completed"
            yield self._line({"type": "done", "status": status})
        except ManageError as error:
            safe_code = (
                error.code
                if type(error.code) is str
                and error.code in _MANAGE_STREAM_ERROR_CODES
                else "manage_error"
            )
            safe_message = (
                _safe_text(error.message, maximum=300)
                if safe_code != "manage_error" and type(error.message) is str
                else "The management request failed."
            )
            yield self._line({
                "type": "error",
                "code": safe_code,
                "message": safe_message,
            })
            yield self._line({"type": "done", "status": "error"})
        except UnifiedError as error:
            if chat.cancel_event.is_set() or getattr(error, "_cancelled", False):
                yield self._line({"type": "done", "status": "cancelled"})
            else:
                yield self._line({
                    "type": "error", "code": error.kind,
                    "message": "The provider request failed.",
                })
                yield self._line({"type": "done", "status": "error"})
        except Exception:
            yield self._line({
                "type": "error", "code": "internal",
                "message": "The provider request failed.",
            })
            yield self._line({"type": "done", "status": "error"})
        finally:
            if upstream is not None:
                close = getattr(upstream, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            self.finish_chat(chat.id)

    @staticmethod
    def _safe_usage(usage: Usage) -> Dict[str, int]:
        def number(value: Optional[int]) -> int:
            return value if type(value) is int and 0 <= value <= 10**12 else 0
        input_tokens = number(usage.input_tokens)
        output_tokens = number(usage.output_tokens)
        cached_tokens = number(usage.cached_tokens)
        total_tokens = number(usage.total_tokens) or input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
        }

    def state_event(self) -> Dict[str, Any]:
        with self._lock:
            active = [
                {"id": chat.id, "provider": chat.provider}
                for chat in self._active_chats.values()
            ]
        return {"type": "state", "active_chats": active, "ts": int(time.time())}


_RUNTIME_LOCK = threading.RLock()
_RUNTIME: Optional[ManageRuntime] = None


def prepare_manage(
    workspaces: Sequence[str], *, provider_snapshots: object = None
) -> str:
    """Enable/replace manage mode and return a one-time 256-bit bootstrap token."""
    global _RUNTIME
    try:
        runtime = ManageRuntime(
            workspaces, provider_snapshots=provider_snapshots
        )
    except (OSError, TypeError, ValueError):
        raise UnifiedError(
            kind="config", provider="claude",
            message="Browser management workspaces are invalid or unavailable.",
            hint="Pass only existing directories that this process may read.",
        ) from None
    token = runtime.issue_bootstrap()
    with _RUNTIME_LOCK:
        previous = _RUNTIME
        _RUNTIME = runtime
        if previous is not None:
            previous.disable()
    return token


def ensure_manage(
    workspaces: Sequence[str], *, provider_snapshots: object = None
) -> Optional[str]:
    """Prepare once for ``run(manage=True)``; never rotate an existing token."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return None
        try:
            runtime = ManageRuntime(
                workspaces, provider_snapshots=provider_snapshots
            )
        except (OSError, TypeError, ValueError):
            raise UnifiedError(
                kind="config", provider="claude",
                message="Browser management workspaces are invalid or unavailable.",
                hint="Pass only existing directories that this process may read.",
            ) from None
        token = runtime.issue_bootstrap()
        _RUNTIME = runtime
        return token


def disable_manage() -> None:
    """Disable routes and cancel active manage subprocesses (primarily tests)."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
        if runtime is not None:
            runtime.disable()


def get_manage_runtime() -> Optional[ManageRuntime]:
    with _RUNTIME_LOCK:
        return _RUNTIME


__all__ = [
    "COOKIE_NAME", "MAX_UI_BODY_BYTES", "ManageError", "ManageRuntime",
    "prepare_manage", "ensure_manage", "disable_manage", "get_manage_runtime",
]
