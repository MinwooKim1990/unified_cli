"""Unified error classification.

Every subprocess failure is funneled through `classify(provider, stderr, stdout,
exitcode)` which returns a `UnifiedError` with a stable `kind` + Korean user
message + recovery hint + short `cause` summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .core import ProviderName


ErrorKind = Literal[
    "auth_expired",
    "rate_limit",
    "model_not_allowed",
    "not_found",
    "network",
    "config",
    "internal",
]


@dataclass
class UnifiedError(Exception):
    """Structured error surfaced by the unified wrapper."""

    kind: ErrorKind
    provider: ProviderName
    message: str
    hint: str = ""
    cause: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = f"[{self.provider}:{self.kind}] {self.message}"
        if self.hint:
            base += f"\n  → {self.hint}"
        if self.cause:
            base += f"\n  (원인: {self.cause[:200]})"
        return base


# ---- hint texts (referenced by matcher tables) ----

HINTS: dict[str, str] = {
    "claude_login":
        "`claude /login` 을 재실행하거나 ANTHROPIC_API_KEY 환경변수를 설정하세요.",
    "codex_login":
        "`codex login` 을 재실행하거나 OPENAI_API_KEY 환경변수를 설정하세요.",
    "gemini_login":
        "`agy` 를 실행해 브라우저로 다시 로그인하세요 (Antigravity). "
        "구 gemini CLI 는 개인 계정에서 차단됨.",
    "antigravity_migrate":
        "구 gemini CLI 는 개인 계정 지원 종료 — Antigravity `agy` 로 마이그레이션됨. "
        "`agy` 설치/로그인 후 사용하세요 (https://antigravity.google).",
    "wait_and_retry":
        "잠시 후 다시 시도하거나 다른 provider/모델로 전환하세요.",
    "check_model_list":
        "사용 가능한 모델은 `unified-cli models` 로 확인하세요.",
    # Codex subscription-allowed models — dynamically pulled from
    # models._HARDCODED["codex"] so they stay in sync. See _build_hints below.
    "codex_subscription_models": "",  # populated at import time
    "network_retry":
        "네트워크 연결을 확인하세요. 통합 래퍼는 이미 2회 재시도했습니다.",
    "check_resource":
        "요청한 리소스(모델/세션)가 존재하는지 확인하세요.",
    "install_cli":
        "CLI 바이너리를 찾을 수 없습니다. 해당 provider CLI를 설치하고 PATH를 확인하세요.",
}


def _build_codex_hint() -> str:
    """Compose the codex subscription-models hint from the hardcoded list.

    Avoids drift between the displayed model list and the error hint text.
    Imported lazily to avoid a circular import (models.py → errors.py).
    """
    try:
        from .models import _HARDCODED
        models = " / ".join(m for m in _HARDCODED["codex"] if not m.startswith("codex-"))
        return (
            f"ChatGPT 구독으로 사용 가능한 Codex 모델: {models}. "
            "신규 모델 (예: gpt-5.5) 을 쓰려면 `brew upgrade codex` 또는 "
            "`npm i -g @openai/codex@latest` 로 CLI 부터 업그레이드하세요."
        )
    except Exception:
        return "사용 가능한 모델은 `unified-cli models codex` 로 확인하세요."


HINTS["codex_subscription_models"] = _build_codex_hint()


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
    provider: ProviderName,
    stderr: str = "",
    stdout: str = "",
    exitcode: Optional[int] = None,
) -> UnifiedError:
    """Return a UnifiedError matching the first applicable pattern.

    Search order: stderr first (more reliable), then stdout. Falls back to
    `kind="internal"` if nothing matches.
    """
    haystack = (stderr or "") + "\n" + (stdout or "")
    for pattern, kind, hint_key in MATCHERS[provider]:
        if pattern.search(haystack):
            return UnifiedError(
                kind=kind,
                provider=provider,
                message=_default_message(kind, provider),
                hint=HINTS.get(hint_key, ""),
                cause=_extract_cause(stderr, stdout),
            )
    return UnifiedError(
        kind="internal",
        provider=provider,
        message=f"{provider} CLI 종료 코드 {exitcode}: 알 수 없는 오류",
        hint="stderr 전체를 확인하세요.",
        cause=_extract_cause(stderr, stdout),
    )


def _default_message(kind: ErrorKind, provider: str) -> str:
    return {
        "auth_expired": f"{provider} 인증이 만료되었습니다.",
        "rate_limit": f"{provider} 사용량 한도를 초과했습니다.",
        "model_not_allowed": f"{provider} 이 모델을 허용하지 않습니다.",
        "not_found": f"{provider} 요청한 리소스를 찾을 수 없습니다.",
        "network": f"{provider} 네트워크 오류가 발생했습니다.",
        "config": f"{provider} 설정이 잘못되었습니다.",
        "internal": f"{provider} 내부 오류가 발생했습니다.",
    }[kind]


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
