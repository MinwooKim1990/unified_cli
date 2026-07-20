"""Unified error classification.

Every subprocess failure is funneled through `classify(provider, stderr, stdout,
exitcode)` which returns a `UnifiedError` with a stable `kind` + localized user
message + recovery hint + short `cause` summary.

The regex matcher tables that classify external CLI output (claude/codex/agy
stderr) are intentionally NOT localized — they match the wrapped CLIs' own
English/JSON output and must stay verbatim. Only the messages/hints WE present
to the user are routed through `i18n.t()`, resolved at call time so the active
language (which may be set after import) is honored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .core import ProviderId, ProviderName
from .i18n import t


ErrorKind = Literal[
    "auth_expired",
    "rate_limit",
    "model_not_allowed",
    "not_found",
    "network",
    "resource_limit",
    "config",
    "internal",
]


@dataclass
class UnifiedError(Exception):
    """Structured error surfaced by the unified wrapper."""

    kind: ErrorKind
    provider: ProviderId
    message: str
    hint: str = ""
    cause: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = f"[{self.provider}:{self.kind}] {self.message}"
        if self.hint:
            base += f"\n  → {self.hint}"
        if self.cause:
            base += f"\n  ({t('err.cause_label')}: {self.cause[:200]})"
        return base


# ---- hint texts (referenced by matcher tables) ----
#
# Each entry maps a matcher hint_key → the i18n key whose template is resolved
# at classify() time (so the active language wins, not the import-time one).

_HINT_KEYS: dict[str, str] = {
    "claude_login": "err.hint.claude_login",
    "codex_login": "err.hint.codex_login",
    "gemini_login": "err.hint.gemini_login",
    "antigravity_migrate": "err.hint.antigravity_migrate",
    "wait_and_retry": "err.hint.wait_and_retry",
    "check_model_list": "err.hint.check_model_list",
    "codex_subscription_models": "err.hint.codex_subscription_models",
    "network_retry": "err.hint.network_retry",
    "check_resource": "err.hint.check_resource",
    "install_cli": "err.hint.install_cli",
}


def _codex_subscription_models() -> str:
    """List the codex subscription-allowed models from the hardcoded table.

    Avoids drift between the displayed model list and the error hint text.
    Imported lazily to avoid a circular import (models.py → errors.py).
    """
    try:
        from .models import _HARDCODED
        return " / ".join(
            m for m in _HARDCODED["codex"] if not m.startswith("codex-")
        )
    except Exception:
        return ""


def _resolve_hint(hint_key: str) -> str:
    """Resolve a matcher hint_key to localized text at call time."""
    if not hint_key:
        return ""
    if hint_key == "codex_subscription_models":
        models = _codex_subscription_models()
        if not models:
            return t("err.hint.codex_subscription_fallback")
        return t("err.hint.codex_subscription_models", models=models)
    i18n_key = _HINT_KEYS.get(hint_key)
    return t(i18n_key) if i18n_key else ""


# Backwards-compatible read-only view (some callers/tests may inspect HINTS).
# Resolves each key for the active language on access.
class _HintsView:
    def get(self, key: str, default: str = "") -> str:
        return _resolve_hint(key) or default

    def __getitem__(self, key: str) -> str:
        return _resolve_hint(key)


HINTS = _HintsView()


# ---- matcher tables: ordered list of (regex, kind, hint_key) per provider ----

_Matcher = tuple[re.Pattern, ErrorKind, str]

MATCHERS: dict[ProviderName, list[_Matcher]] = {
    "claude": [
        (re.compile(r"OAuth token has expired|authentication_error|\"type\":\s*\"authentication_error\"", re.I),
         "auth_expired", "claude_login"),
        (re.compile(r"\b401\b", re.I), "auth_expired", "claude_login"),
        (re.compile(r"\b429\b|rate[_ -]?limit", re.I), "rate_limit", "wait_and_retry"),
        (re.compile(r"session[^\n]{0,60}(not found|does not exist|invalid|expired)"
                    r"|could not find session|unknown session"
                    r"|no conversation found with session",
                    re.I),
         "not_found", "check_resource"),
        (re.compile(
            r"model[^\n]{0,80}(not exist|not accessible|invalid|unknown|not allowed)"
            r"|is not a valid model"
            r"|requested model[^\n]{0,40}(is not|not available|not supported)"
            r"|invalid model identifier",
            re.I),
         "model_not_allowed", "check_model_list"),
        (re.compile(r"\bENOTFOUND\b|\bECONNRESET\b|getaddrinfo|network|ETIMEDOUT", re.I),
         "network", "network_retry"),
    ],
    "codex": [
        (re.compile(r'"type"\s*:\s*"authentication_error"|authentication_error', re.I),
         "auth_expired", "codex_login"),
        (re.compile(r"\b401\b", re.I), "auth_expired", "codex_login"),
        (re.compile(r"\b429\b|rate limit|Too Many Requests|RESOURCE_EXHAUSTED", re.I),
         "rate_limit", "wait_and_retry"),
        (re.compile(r"not supported when using Codex with a ChatGPT account", re.I),
         "model_not_allowed", "codex_subscription_models"),
        (re.compile(r"model.{0,40}(not found|does not exist|not available)", re.I),
         "model_not_allowed", "check_model_list"),
        (re.compile(r"\bENOTFOUND\b|\bECONNRESET\b|network|ETIMEDOUT|stream disconnected", re.I),
         "network", "network_retry"),
    ],
    # "gemini" provider now wraps the Antigravity `agy` CLI (see
    # providers/gemini.py). Matchers cover both agy errors and the legacy
    # gemini-CLI IneligibleTier message in case of a fallback binary.
    "gemini": [
        # Legacy gemini CLI individual-tier shutdown → tell user to use agy.
        (re.compile(r"IneligibleTierError|no longer supported for Gemini Code Assist", re.I),
         "auth_expired", "antigravity_migrate"),
        (re.compile(r"No refresh token is set|invalid_grant|Access blocked: Authorization"
                    r"|not (logged|signed) in|please (log|sign) in|unauthenticated", re.I),
         "auth_expired", "gemini_login"),
        (re.compile(r"\b401\b", re.I), "auth_expired", "gemini_login"),
        (re.compile(r"\b429\b|RESOURCE_EXHAUSTED|rateLimitExceeded|Quota exceeded"
                    r"|quota|G1 credit|credits exhausted|out of credits", re.I),
         "rate_limit", "wait_and_retry"),
        (re.compile(r"Requested entity was not found|\b404\b|model.{0,40}not found"
                    r"|unknown model|invalid model", re.I),
         "model_not_allowed", "check_model_list"),
        (re.compile(r"\bENOTFOUND\b|\bECONNRESET\b|network|ETIMEDOUT|timed? ?out", re.I),
         "network", "network_retry"),
    ],
}


def classify(
    provider: ProviderId,
    stderr: str = "",
    stdout: str = "",
    exitcode: Optional[int] = None,
) -> UnifiedError:
    """Return a UnifiedError matching the first applicable pattern.

    Search order: stderr first (more reliable), then stdout. Falls back to
    `kind="internal"` if nothing matches.
    """
    haystack = (stderr or "") + "\n" + (stdout or "")
    matchers = MATCHERS.get(provider)  # type: ignore[arg-type]
    if matchers is None:
        # Extension output is untrusted and has no core-owned matcher table.
        # Do not reflect its stderr/stdout through the normal ``cause`` field.
        return UnifiedError(
            kind="internal",
            provider=provider,
            message=t("err.plugin.runtime", provider=provider),
            hint=t("err.plugin.runtime.hint"),
            cause="extension provider process failed",
        )
    for pattern, kind, hint_key in matchers:
        if pattern.search(haystack):
            return UnifiedError(
                kind=kind,
                provider=provider,
                message=_default_message(kind, provider),
                hint=_resolve_hint(hint_key),
                cause=_extract_cause(stderr, stdout),
            )
    return UnifiedError(
        kind="internal",
        provider=provider,
        message=t("err.msg.unknown", provider=provider, exitcode=exitcode),
        hint=t("err.hint.check_stderr"),
        cause=_extract_cause(stderr, stdout),
    )


def _default_message(kind: ErrorKind, provider: str) -> str:
    return t(f"err.msg.{kind}", provider=provider)


def _extract_cause(stderr: str, stdout: str) -> str:
    """Pick the most informative non-empty line as a short cause summary."""
    for raw in (stderr, stdout):
        for line in (raw or "").splitlines():
            s = line.strip()
            if s and not s.startswith(("Warning:", "warning:", "{")):
                return s
    # fallback to first line of stderr
    first = (stderr or stdout or "").strip().splitlines()
    return first[0] if first else ""
