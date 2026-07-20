"""Environment isolation, cancellation, and diagnostic redaction."""

from __future__ import annotations

import os
import json
import math
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

from ..errors import ConfigurationError, TransportCancelled


_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM")
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*|password\s*[:=]\s*)[^\s,;]+"
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CancellationToken:
    """Thread-safe explicit cancellation signal shared by sync/async APIs."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TransportCancelled("extension operation cancelled")


@dataclass(frozen=True)
class TransportLimits:
    max_line_bytes: int = 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    max_events: int = 50_000
    max_body_bytes: int = 16 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        for value in (
            self.max_line_bytes,
            self.max_output_bytes,
            self.max_stderr_bytes,
            self.max_events,
            self.max_body_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("transport limits must be positive integers")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be an integer between zero and ten")


def validate_positive_timeout(value: Any) -> float:
    if type(value) not in (int, float) or value <= 0:
        raise ConfigurationError("timeout must be a finite positive number")
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        raise ConfigurationError("timeout must be a finite positive number") from None
    if not math.isfinite(numeric):
        raise ConfigurationError("timeout must be a finite positive number")
    return numeric


def strict_json_loads(payload: Any) -> Any:
    """Decode RFC JSON while rejecting duplicate keys and non-finite values."""

    def object_pairs(pairs: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=object_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError("non-finite JSON number")
        ),
    )


class IsolatedEnvironment:
    """Context-managed HOME/TMPDIR with a minimal inherited environment."""

    def __init__(
        self,
        provider_env: Optional[Mapping[str, str]] = None,
        *,
        allowed_provider_keys: Iterable[str] = (),
    ) -> None:
        self._provider_env: Dict[str, str] = {}
        try:
            source = provider_env if provider_env is not None else {}
            iterator = iter(source.items())
            for index, pair in enumerate(iterator):
                if index >= 256:
                    raise ConfigurationError("provider environment exceeds 256 entries")
                try:
                    key, value = pair
                except (TypeError, ValueError):
                    raise ConfigurationError("provider environment mapping is malformed") from None
                if (
                    type(key) is not str
                    or type(value) is not str
                    or not _ENV_KEY_RE.fullmatch(key)
                    or key in _BASE_ENV_KEYS
                    or key in {"HOME", "TMPDIR"}
                ):
                    raise ConfigurationError("invalid provider environment entry")
                try:
                    encoded_value = value.encode("utf-8", "strict")
                except UnicodeError:
                    raise ConfigurationError("invalid provider environment value") from None
                if b"\x00" in encoded_value or len(encoded_value) > 64 * 1024:
                    raise ConfigurationError("invalid provider environment value")
                if key in self._provider_env:
                    raise ConfigurationError("duplicate provider environment key")
                self._provider_env[key] = value
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider environment mapping is malformed") from None
        allowed = set()
        try:
            for index, key in enumerate(allowed_provider_keys):
                if index >= 256:
                    raise ConfigurationError("provider environment allowlist exceeds 256 entries")
                if (
                    type(key) is not str
                    or not _ENV_KEY_RE.fullmatch(key)
                    or key in _BASE_ENV_KEYS
                    or key in {"HOME", "TMPDIR"}
                ):
                    raise ConfigurationError("invalid provider environment allowlist key")
                allowed.add(key)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider environment allowlist is malformed") from None
        self._allowed = frozenset(allowed)
        self._temporary: Optional[tempfile.TemporaryDirectory] = None
        self.env: Dict[str, str] = {}

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(self._provider_env.values())

    def __enter__(self) -> "IsolatedEnvironment":
        if self._temporary is not None:
            raise ConfigurationError("isolated environment is already active")
        unknown = set(self._provider_env) - self._allowed
        if unknown:
            raise ConfigurationError("provider environment key is not allowlisted")
        self._temporary = tempfile.TemporaryDirectory(prefix="unified-cli-ext-")
        root = self._temporary.name
        home = os.path.join(root, "home")
        tmp = os.path.join(root, "tmp")
        try:
            os.mkdir(home, 0o700)
            os.mkdir(tmp, 0o700)
        except OSError:
            self._temporary.cleanup()
            self._temporary = None
            raise ConfigurationError("could not create isolated environment") from None
        env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
        env.update({"HOME": home, "TMPDIR": tmp})
        env.update(self._provider_env)
        self.env = env
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self.env = {}


def redact_diagnostics(text: str, secrets: Iterable[str] = (), max_chars: int = 4096) -> str:
    """Return a bounded single diagnostic with common credentials removed."""

    if type(text) is not str:
        raise TypeError("diagnostic text must be a string")
    if type(max_chars) is not int or max_chars <= 0 or max_chars > 64 * 1024:
        raise ValueError("max_chars must be between one and 65536")
    bounded_secrets = []
    for index, item in enumerate(secrets):
        if index >= 256:
            break
        if type(item) is str and item:
            bounded_secrets.append(item[:64 * 1024])
    redacted = text[: max_chars * 4]
    for secret in sorted(bounded_secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _SECRET_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
    redacted = redacted.replace("\x00", "")
    return redacted[:max_chars]


__all__ = [
    "CancellationToken",
    "IsolatedEnvironment",
    "TransportLimits",
    "redact_diagnostics",
    "strict_json_loads",
    "validate_positive_timeout",
]
