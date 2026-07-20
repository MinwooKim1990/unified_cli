"""Security boundary for the opt-in loopback browser management surface.

The ordinary OpenAI-compatible server intentionally does not depend on this
runtime.  A caller must explicitly prepare it, exchange a short-lived one-time
bootstrap secret, and then use a host-only cookie plus an in-memory CSRF token.
Provider output is reduced to a small, normalized NDJSON vocabulary before it
crosses the browser boundary.
"""

from __future__ import annotations

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
from .models import DEFAULT_MODELS, list_models
from .registry import list_providers
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

_HANDLE_RE = re.compile(r"^[a-z]+_[A-Za-z0-9_-]{16,64}$", re.ASCII)
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

    def __init__(self, workspaces: Sequence[str]):
        self._lock = threading.RLock()
        self._handle_key = secrets.token_bytes(32)
        self._bootstrap_digest = _digest(_urlsafe_random())  # replaced immediately
        self._bootstrap_expires = 0.0
        self._sessions: "OrderedDict[str, BrowserSession]" = OrderedDict()
        self._active_chats: Dict[str, ActiveChat] = {}
        self._provider_active: Dict[str, str] = {}
        self._verify_active = False
        self._failed_bootstraps: Dict[str, Deque[float]] = {}
        self.session_manager = SessionManager()
        self.workspaces = self._prepare_workspaces(workspaces)
        self._workspace_by_id = {workspace.id: workspace for workspace in self.workspaces}

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
            self._bootstrap_digest = _digest(token)
            self._bootstrap_expires = time.monotonic() + BOOTSTRAP_TTL_SECONDS
        return token

    def disable(self) -> None:
        with self._lock:
            chats = list(self._active_chats.values())
            self._active_chats.clear()
            self._provider_active.clear()
            self._sessions.clear()
            self._bootstrap_digest = ""
            self._bootstrap_expires = 0.0
        for chat in chats:
            chat.cancel_event.set()

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
        settings = self.safe_settings()
        default_workspace = self._workspace_by_id.get(settings["workspace_id"])
        versions = {"unified_cli": self._core_version()}
        try:
            versions["unified_cli_ext"] = importlib_metadata.version(
                "unified-cli-ext")
        except importlib_metadata.PackageNotFoundError:
            pass
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
        try:
            save_settings(candidate)
        except (OSError, TypeError, ValueError):
            raise ManageError(500, "settings_failed", "Settings could not be saved.") from None
        return self.safe_settings()

    @staticmethod
    def provider_metadata() -> Dict[str, Any]:
        try:
            descriptors = list_providers(include_ext=True)
        except UnifiedError:
            descriptors = list_providers(include_ext=False)
        rows = []
        for descriptor in descriptors:
            row: Dict[str, Any] = {
                "id": descriptor.id,
                "source": descriptor.source,
                "status": descriptor.status,
                "default_model": descriptor.default_model,
                "chat_supported": descriptor.id in {"claude", "codex"},
                "verify_supported": descriptor.id in _VERIFY_SPECS,
            }
            if descriptor.id in _COPY_COMMANDS:
                commands = dict(_COPY_COMMANDS[descriptor.id])
                row["commands"] = commands
                row["install_command"] = commands["install"]
                row["login_command"] = commands["login"]
            if descriptor.error:
                row["error"] = descriptor.error
            rows.append(row)
        return {"providers": rows}

    @staticmethod
    def provider_models(provider_id: str) -> Dict[str, Any]:
        if provider_id not in {"claude", "codex", "gemini"}:
            raise ManageError(403, "provider_unsupported", "Provider models are unavailable.")
        try:
            models = list_models(provider_id)
        except Exception:
            raise ManageError(502, "models_unavailable", "Provider models are unavailable.") from None
        return {
            "provider": provider_id,
            "models": [
                {
                    "id": item.id,
                    "display_name": item.display_name,
                    "default": bool(item.default),
                    "deprecated": bool(item.deprecated),
                    "source": item.source,
                }
                for item in models[:1_000]
                if item.provider == provider_id and type(item.id) is str and len(item.id) <= 512
            ],
        }

    def verify_provider(self, provider_id: str) -> Dict[str, Any]:
        specs = _VERIFY_SPECS.get(provider_id)
        if specs is None:
            raise ManageError(403, "verify_unsupported", "Provider verification is unsupported.")
        with self._lock:
            if self._verify_active:
                raise ManageError(429, "verify_busy", "A provider verification is already running.")
            self._verify_active = True
        results = []
        try:
            with tempfile.TemporaryDirectory(prefix="unified-cli-verify-") as cwd:
                for index, argv in enumerate(specs):
                    result = _run_verify_argv(argv, cwd)
                    if index > 0:
                        # Auth commands are reduced to a boolean classification;
                        # account identifiers and provider diagnostics do not
                        # need to cross the browser boundary.
                        result["output"] = ""
                    results.append({
                        "check": "version" if index == 0 else "auth_status",
                        **result,
                    })
                    if result["code"] == "missing_binary":
                        break
        finally:
            with self._lock:
                self._verify_active = False
        installed = bool(results and results[0]["ok"])
        auth = "unknown"
        if len(results) > 1:
            auth = "authenticated" if results[1]["ok"] else "not_authenticated"
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
        record = self._resolve_session(handle)
        try:
            if "name" in payload:
                name = payload["name"]
                if type(name) is not str or not name.strip() or len(name) > 200:
                    raise ManageError(400, "invalid_session", "Session name is invalid.")
                record = self.session_manager.rename(
                    provider=record.provider, session_id=record.session_id, name=name)
            if "archived" in payload:
                archived = payload["archived"]
                if type(archived) is not bool:
                    raise ManageError(400, "invalid_session", "Archived must be a boolean.")
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
        try:
            removed = self.session_manager.delete(
                provider=record.provider, session_id=record.session_id)
        except (OSError, ValueError):
            raise ManageError(500, "sessions_failed", "Session could not be deleted.") from None
        if not removed:
            raise ManageError(404, "session_not_found", "Session was not found.")
        return {"deleted": True, "id": handle}

    @staticmethod
    def usage_snapshot() -> Dict[str, Any]:
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
        if provider not in {"claude", "codex"}:
            raise ManageError(403, "provider_forbidden", "Provider is unavailable for browser chat.")
        if payload.get("permission", "read_only") != "read_only":
            raise ManageError(403, "permission_forbidden", "Browser chat is read-only.")
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


def prepare_manage(workspaces: Sequence[str]) -> str:
    """Enable/replace manage mode and return a one-time 256-bit bootstrap token."""
    global _RUNTIME
    try:
        runtime = ManageRuntime(workspaces)
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


def ensure_manage(workspaces: Sequence[str]) -> Optional[str]:
    """Prepare once for ``run(manage=True)``; never rotate an existing token."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return None
        try:
            runtime = ManageRuntime(workspaces)
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
