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

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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


# Retry metadata is deliberately private. ``UnifiedError.kind`` is part of the
# stable public API, while these Core-owned values let the subprocess layer
# distinguish a short-lived 429 from quota, policy, auth, and authorization
# failures that may share the same public kind.
_RETRY_AFTER_MAX_SECONDS = 5.0
_AUTH_RE = re.compile(
    r"\b401\b|authentication_error|oauth token has expired|invalid_grant"
    r"|no refresh token is set|unauthenticated|not (?:logged|signed) in",
    re.I,
)
_AUTHORIZATION_RE = re.compile(
    r"\b403\b|\bforbidden\b|authorization_error|not authorized"
    r"|permission denied(?! by policy)",
    re.I,
)
_QUOTA_RE = re.compile(
    r"insufficient[_ -]?quota|quota (?:exceeded|exhausted)|billing hard limit"
    r"|credits? (?:exhausted|depleted)|out of credits|g1 credit"
    r"|\byou(?:\s+have|['’]ve)?\s+"
    r"(?:hit|reached|exceeded|exhausted)\s+(?:your|the)\s+usage\s+limit\b"
    r"|\bhit\s+(?:(?:your|the)\s+)?(?:weekly|daily|monthly)\s+"
    r"(?:usage\s+)?limit\b"
    r"|\bno\s+(?:credits?\s+(?:remaining|remain)|remaining\s+credits?)\b"
    r"|\bquota\s+limit(?:\s+(?:has\s+been|was|is))?\s+"
    r"(?:reached|exceeded|exhausted)\b"
    r"|(?:weekly|daily|monthly)\s+(?:usage\s+)?limit"
    r"(?:\s+has\s+been)?\s+(?:reached|exceeded|exhausted)"
    r"|usage\s+limit(?:\s+has\s+been)?\s+(?:reached|exceeded|exhausted)"
    r"|(?:reached|exceeded|exhausted)\s+(?:your\s+)?"
    r"(?:weekly|daily|monthly)\s+(?:usage\s+)?limit",
    re.I,
)
# Some providers report permanent account/plan denials as HTTP 429.  The
# status and denial evidence are deliberately detected separately: JSON key
# order and pretty-printing must not matter, while assistant/output payloads
# must never supply denial evidence for a real transient 429.
_RETRY_CLASSIFIER_MAX_BYTES = 128 * 1024
_RETRY_CLASSIFIER_MAX_DEPTH = 16
_RETRY_CLASSIFIER_MAX_NODES = 256
_RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS = 8
_RETRY_CLASSIFIER_MAX_LINE = 512
_RETRY_CLASSIFIER_MAX_MESSAGE = 2048
_RETRY_CLASSIFIER_MAX_PLAIN_LINES = 8

_PERMANENT_DENIAL_RE = re.compile(
    _QUOTA_RE.pattern
    + r"|entitlement(?:['’]s(?:\s+been)?|\s+(?:has\s+been|was|is))?\s+"
      r"(?:exhausted|required)"
    + r"|not\s+(?:entitled|eligible)\b"
    + r"|billing\s+limit(?:\s+(?:has\s+been|was|is))?\s+"
      r"(?:reached|exceeded|exhausted)"
    + r"|(?:billing\s+)?account"
      r"(?:['’]s(?:\s+been)?|\s+(?:has\s+been|was|is))?\s+"
      r"(?:disabled|suspended)"
    + r"|billing(?:['’]s|\s+(?:has\s+been|was|is))?\s+disabled\s+"
      r"for\s+(?:this|your|the)\s+account"
    + r"|subscription\s+(?:usage\s+)?limit"
      r"(?:\s+(?:has\s+been|was|is))?\s+"
      r"(?:reached|exceeded|exhausted)"
    + r"|subscription"
      r"(?:['’]s(?:\s+been)?|\s+(?:has(?:\s+been)?|was|is))?\s+"
      r"(?:expired|required)"
    + r"|(?:a\s+)?subscription\s+is\s+required"
    + r"|(?:(?:your|the|this|current)\s+)?plan\s+"
      r"(?:(?:does|is)\s+not\s+"
      r"(?:include|support|allow|included|supported|allowed))\b"
    + r"|upgrade(?:\s+your)?\s+(?:plan|subscription)\b"
    + r"|payment(?:\s+is)?\s+required\b",
    re.I,
)
_QUOTA_DIAGNOSTIC_START_RE = re.compile(
    r"^(?:insufficient[_ -]?quota|quota\b|billing hard limit|credits?\b"
    r"|out of credits|g1 credit|you\b|you['’]ve\b|hit\b|no\b"
    r"|weekly\b|daily\b|monthly\b|usage\b|reached\b|exceeded\b"
    r"|exhausted\b)",
    re.I,
)
_JSON_STATUS_KEYS = {
    "code", "http_status", "http_status_code", "httpstatus",
    "httpstatuscode", "status", "status_code", "statuscode",
}
_JSON_MESSAGE_KEYS = {"detail", "message", "reason"}
_JSON_SAFE_CONTAINER_KEYS = {
    "detail", "details", "error", "errors", "status",
}
_JSON_PUBLIC_PAYLOAD_KEYS = {
    "assistant", "command_execution", "content", "delta", "function_call",
    "item", "output", "reasoning", "response", "result", "role", "text",
    "tool_call", "tool_calls", "tool_result", "tool_use", "usage",
}
_JSON_ERROR_TYPES = {
    "error", "error_response", "failed", "failure", "request_error",
}
_JSON_PUBLIC_EVENT_TYPE_PREFIXES = (
    "assistant", "content", "output", "response", "result", "text", "tool",
)
_JSON_429_RE = re.compile(
    r"(?:429|http(?:/\d(?:\.\d)?)?\s+429|resource_exhausted\s*"
    r"(?:\(\s*429\s*\)|[:=]\s*429))\Z",
    re.I,
)
_RAW_JSON_429_RE = re.compile(
    r'(?i)"(?:code|http_?status(?:_?code)?|status(?:_?code)?)"\s*:\s*'
    r'(?:429\b|"429")'
)
_HTTP_VERSION_RE = re.compile(
    r"\bhttp/(?:1(?:\.0|\.1)?|2|3)\b", re.I,
)
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_SEPARATOR_RE = re.compile(
    r"[/_:=;,\-\u2010-\u2015\u2212\ufe58\ufe63\uff0d]+"
)
_TOKEN_WHITESPACE_RE = re.compile(r"\s+")
_HAS_BEEN_CONTRACTION_RE = re.compile(
    r"\b(billing|account|subscription|entitlement)'s\s+been\b", re.I,
)
_IS_CONTRACTION_RE = re.compile(
    r"\b(payment|billing)'s(?=\s+(?:required|disabled)\b)", re.I,
)
_IS_NOT_CONTRACTION_RE = re.compile(r"\bisn't\b", re.I)
_DOES_NOT_CONTRACTION_RE = re.compile(r"\bdoesn't\b", re.I)
_RESOURCE_429_PARENS_RE = re.compile(
    r"\bresource\s+exhausted\s*\(\s*429\s*\)", re.I,
)
_PLAIN_PUBLIC_PREFIXES = {
    "assistant", "content", "documentation", "docs", "doc", "example",
    "instruction", "instructions", "output", "response", "text", "usage",
}
_POLICY_RE = re.compile(
    r"policy (?:denied|denial|violation|blocked)|denied by policy"
    r"|blocked by (?:a )?(?:content|safety) policy|content_policy_violation",
    re.I,
)
_TRANSIENT_429_RE = re.compile(
    r"\b429\b|too many requests|rate[_ -]?limit|resource_exhausted",
    re.I,
)
_TRANSIENT_NETWORK_RE = re.compile(
    r"\bENOTFOUND\b|\bECONNRESET\b|\bECONNREFUSED\b|\bEAI_AGAIN\b"
    r"|\bETIMEDOUT\b|getaddrinfo|dns (?:lookup|resolution)"
    r"|network (?:error|unavailable|unreachable)|stream disconnected",
    re.I,
)
_PRE_REQUEST_NETWORK_RE = re.compile(
    r"\bENOTFOUND\b|\bECONNREFUSED\b|\bEAI_AGAIN\b|getaddrinfo"
    r"|dns (?:lookup|resolution)|network unreachable",
    re.I,
)
_RETRY_AFTER_HEADER_RE = re.compile(
    r"(?im)^\s*retry-after\s*[:=]\s*([^\r\n]{1,64})\s*$"
)
_RETRY_AFTER_JSON_RE = re.compile(
    r'(?i)"(?:retry[_-]?after|retry[_-]?delay(?:[_-]?seconds)?)"\s*:\s*'
    r'("[^"\\]{1,64}"|-?\d+(?:\.\d+)?)'
)
_JSON_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class _RetryDenialAnalysis:
    permanent: bool
    complete: bool
    error_429: bool


def _retry_classifier_text_is_bounded(text: str) -> bool:
    """Bound adversarial provider output before parsing or line matching."""
    if len(text) > _RETRY_CLASSIFIER_MAX_BYTES:
        return False
    return len(text.encode("utf-8")) <= _RETRY_CLASSIFIER_MAX_BYTES


def _normalize_bounded_tokens(text: str) -> str:
    """Canonicalize bounded diagnostic tokens without broad prose parsing."""
    if len(text) > _RETRY_CLASSIFIER_MAX_BYTES:
        return ""
    normalized = _HTTP_VERSION_RE.sub("http", text)
    normalized = _CAMEL_ACRONYM_BOUNDARY_RE.sub(" ", normalized)
    normalized = _CAMEL_WORD_BOUNDARY_RE.sub(" ", normalized)
    normalized = normalized.replace("’", "'")
    normalized = _TOKEN_SEPARATOR_RE.sub(" ", normalized)
    normalized = _HAS_BEEN_CONTRACTION_RE.sub(r"\1 has been", normalized)
    normalized = _IS_CONTRACTION_RE.sub(r"\1 is", normalized)
    normalized = _IS_NOT_CONTRACTION_RE.sub("is not", normalized)
    normalized = _DOES_NOT_CONTRACTION_RE.sub("does not", normalized)
    return _TOKEN_WHITESPACE_RE.sub(" ", normalized).strip().lower()


def _canonical_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _normalize_bounded_tokens(value).replace(" ", "_")


def _plain_429_denial_text(line: str) -> Optional[str]:
    """Return text after one strictly line-anchored normalized 429 status."""
    if not line or len(line) > _RETRY_CLASSIFIER_MAX_LINE:
        return None
    stripped_line = line.lstrip()
    if not stripped_line or not stripped_line[0].isalnum():
        return None
    normalized = _normalize_bounded_tokens(line)
    # Parentheses are normalized only for the established
    # RESOURCE_EXHAUSTED(429) status spelling; a parenthesized HTTP example
    # must not become a line-anchored diagnostic.
    normalized = _RESOURCE_429_PARENS_RE.sub(
        "resource exhausted 429", normalized,
    )
    status_tokens = normalized.split()
    if not status_tokens:
        return None
    index = 0
    if status_tokens[index] in {"error", "fatal"}:
        index += 1
        if index >= len(status_tokens):
            return None
    if (len(status_tokens) >= index + 3
            and status_tokens[index:index + 3]
            == ["resource", "exhausted", "429"]):
        return " ".join(status_tokens[index + 3:])
    if status_tokens[index] == "http":
        index += 1
        if index >= len(status_tokens):
            return None
    if status_tokens[index] == "status":
        index += 1
        if index < len(status_tokens) and status_tokens[index] == "code":
            index += 1
    elif status_tokens[index] == "code":
        index += 1
    if index >= len(status_tokens) or status_tokens[index] != "429":
        return None
    return " ".join(status_tokens[index + 1:])


def _plain_line_has_public_prefix(line: str) -> bool:
    normalized = _normalize_bounded_tokens(line)
    first = normalized.split(" ", 1)[0] if normalized else ""
    return first in _PLAIN_PUBLIC_PREFIXES


def _normalized_plain_diagnostic_body(line: str) -> str:
    normalized = _normalize_bounded_tokens(line)
    tokens = normalized.split()
    if len(tokens) >= 2 and tokens[:2] == ["api", "error"]:
        tokens = tokens[2:]
    elif tokens and tokens[0] in {"body", "detail", "error", "message", "reason"}:
        tokens = tokens[1:]
    return " ".join(tokens)


def _json_value_is_bounded(value: object) -> bool:
    """Validate decoded JSON with an iterative depth/node walk."""
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if (visited > _RETRY_CLASSIFIER_MAX_NODES
                or depth > _RETRY_CLASSIFIER_MAX_DEPTH):
            return False
        if isinstance(item, dict):
            if visited + len(stack) + len(item) > _RETRY_CLASSIFIER_MAX_NODES:
                return False
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if visited + len(stack) + len(item) > _RETRY_CLASSIFIER_MAX_NODES:
                return False
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (bool, int, float, str)):
            return False
    return True


def _bounded_json_documents(
    text: str,
) -> tuple[list[object], list[tuple[int, int]], bool]:
    """Decode line-start JSON and report every bounded-analysis skip."""
    documents: list[object] = []
    spans: list[tuple[int, int]] = []
    offset = 0
    candidates = 0
    complete = True
    text_length = len(text)
    while offset < text_length:
        line_end = text.find("\n", offset)
        if line_end < 0:
            line_end = text_length
        start = offset
        while start < line_end and text[start] in " \t\r":
            start += 1
        if start < line_end and text[start] in "{[":
            candidates += 1
            if candidates > _RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS:
                complete = False
                offset = line_end + 1
                continue
            try:
                value, end = _JSON_DECODER.raw_decode(text, start)
            except (json.JSONDecodeError, ValueError, RecursionError):
                complete = False
                offset = line_end + 1
                continue
            decoded_line_end = text.find("\n", end)
            if decoded_line_end < 0:
                decoded_line_end = text_length
            span_end = min(decoded_line_end + 1, text_length)
            spans.append((offset, span_end))
            if text[end:decoded_line_end].strip():
                complete = False
            elif _json_value_is_bounded(value):
                documents.append(value)
            else:
                complete = False
            offset = max(end, span_end)
            continue
        offset = line_end + 1
    return documents, spans, complete


def _text_has_error_shaped_429(text: str) -> bool:
    """Find a raw status marker even when structured analysis is incomplete."""
    if _RAW_JSON_429_RE.search(text):
        return True
    return any(
        _plain_429_denial_text(line[:_RETRY_CLASSIFIER_MAX_LINE]) is not None
        for line in text.splitlines()
    )


def _json_status_is_429(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 429
    if isinstance(value, str) and len(value) <= 64:
        return bool(_JSON_429_RE.fullmatch(value.strip()))
    return False


def _normalized_json_event_type(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        return ""
    normalized = _canonical_identifier(value)
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _json_event_type_is_public(value: str) -> bool:
    return any(
        value == prefix or value.startswith(prefix + "_")
        for prefix in _JSON_PUBLIC_EVENT_TYPE_PREFIXES
    )


def _json_error_evidence(value: object) -> tuple[bool, list[str], bool]:
    """Return 429 state and named messages from one safe error envelope."""
    if not isinstance(value, dict) or not value:
        return False, [], True
    root_keys = {_canonical_identifier(key) for key in value}
    complete = True
    event_types_list: list[str] = []
    for key in ("type", "kind"):
        event_value = value.get(key)
        if isinstance(event_value, str) and len(event_value) > 64:
            complete = False
        event_types_list.append(_normalized_json_event_type(event_value))
    event_types = tuple(event_types_list)
    public_root = any(
        _json_event_type_is_public(item) for item in event_types if item
    )
    if public_root:
        traversal_roots = [
            child for key, child in value.items()
            if _canonical_identifier(key) in {"error", "errors"}
        ]
        is_envelope = bool(traversal_roots)
    else:
        traversal_roots = [value]
        is_envelope = bool(
            root_keys.intersection({"error", "errors"} | _JSON_STATUS_KEYS)
            or any(item in _JSON_ERROR_TYPES for item in event_types)
        )
    if not is_envelope:
        return False, [], complete

    has_429 = False
    messages: list[str] = []
    # The bool says scalar strings in this node came from a specifically named
    # message/detail/reason field.  Other strings under arrays are ignored.
    stack: list[tuple[object, bool]] = [
        (item, False) for item in traversal_roots
    ]
    while stack:
        item, named_message = stack.pop()
        if isinstance(item, str):
            if named_message:
                if len(item) <= _RETRY_CLASSIFIER_MAX_MESSAGE:
                    messages.append(item)
                else:
                    complete = False
            continue
        if isinstance(item, list):
            stack.extend((child, named_message) for child in item)
            continue
        if not isinstance(item, dict):
            continue
        for key, child in item.items():
            key_text = _canonical_identifier(key)
            if key_text in _JSON_PUBLIC_PAYLOAD_KEYS:
                continue
            if key_text in _JSON_STATUS_KEYS:
                if isinstance(child, str) and len(child) > 64:
                    complete = False
                elif _json_status_is_429(child):
                    has_429 = True
            if key_text in _JSON_MESSAGE_KEYS:
                stack.append((child, True))
            elif key_text in _JSON_SAFE_CONTAINER_KEYS:
                stack.append((child, False))
    return has_429, messages, complete


def _plain_line_is_error_429(line: str) -> bool:
    return _plain_429_denial_text(line) is not None


def _evidence_matches(pattern: re.Pattern, text: str) -> bool:
    """Match bounded prose or normalized diagnostic token spellings."""
    if pattern.search(text):
        return True
    normalized = _normalize_bounded_tokens(text)
    return bool(pattern.search(normalized))


def _plain_following_line_has_denial(line: str) -> bool:
    if not line or len(line) > _RETRY_CLASSIFIER_MAX_LINE:
        return False
    if _plain_line_has_public_prefix(line):
        return False
    if line.lstrip().startswith(("\"", "'", "`", ">")):
        return False
    candidate = _normalized_plain_diagnostic_body(line)
    return _evidence_matches(_PERMANENT_DENIAL_RE, candidate)


def _plain_lines_outside_json(
    text: str, json_spans: list[tuple[int, int]],
) -> list[str]:
    """Return physical non-JSON lines without copying decoded documents."""
    lines: list[str] = []
    offset = 0
    span_index = 0
    for raw_line in text.splitlines(keepends=True):
        line_end = offset + len(raw_line)
        while (span_index < len(json_spans)
               and json_spans[span_index][1] <= offset):
            span_index += 1
        overlaps_json = bool(
            span_index < len(json_spans)
            and json_spans[span_index][0] < line_end
            and json_spans[span_index][1] > offset
        )
        if not overlaps_json:
            lines.append(raw_line.rstrip("\r\n"))
        offset = line_end
    return lines


def _plain_denial_analysis(
    text: str, json_spans: list[tuple[int, int]],
) -> _RetryDenialAnalysis:
    lines = _plain_lines_outside_json(text, json_spans)
    complete = len(lines) <= _RETRY_CLASSIFIER_MAX_PLAIN_LINES
    if any(len(line) > _RETRY_CLASSIFIER_MAX_LINE for line in lines):
        complete = False
    permanent = False
    for index, line in enumerate(lines[:_RETRY_CLASSIFIER_MAX_PLAIN_LINES]):
        if _plain_line_has_public_prefix(line):
            break
        denial_text = _plain_429_denial_text(line)
        if denial_text is not None:
            if _evidence_matches(_PERMANENT_DENIAL_RE, denial_text):
                permanent = True
                break
            # At most the immediately following physical line may be treated
            # as a diagnostic body; later output may already be assistant text.
            if (index + 1 < len(lines)
                    and _plain_following_line_has_denial(lines[index + 1])):
                permanent = True
                break
        # Preserve strong quota diagnostics that providers emit without a
        # numeric status, but keep them anchored rather than scanning prose.
        stripped = _normalized_plain_diagnostic_body(line)
        if (len(line) <= _RETRY_CLASSIFIER_MAX_LINE
                and not _plain_line_has_public_prefix(line)
                and _QUOTA_DIAGNOSTIC_START_RE.match(stripped)
                and _evidence_matches(_QUOTA_RE, stripped)):
            permanent = True
            break
    return _RetryDenialAnalysis(
        permanent=permanent,
        complete=complete,
        error_429=_text_has_error_shaped_429(text),
    )


def _analyze_permanent_rate_limit_denial(text: str) -> _RetryDenialAnalysis:
    """Classify bounded JSON envelopes and anchored plain diagnostics."""
    if not text:
        return _RetryDenialAnalysis(False, True, False)
    if not _retry_classifier_text_is_bounded(text):
        # Classification is already fail-closed.  Inspect only a bounded
        # prefix for diagnostic metadata rather than scanning arbitrary-sized
        # hostile text after the byte ceiling was crossed.
        error_429 = _text_has_error_shaped_429(
            text[:_RETRY_CLASSIFIER_MAX_BYTES]
        )
        return _RetryDenialAnalysis(False, False, error_429)

    error_429 = _text_has_error_shaped_429(text)
    documents, json_spans, complete = _bounded_json_documents(text)
    permanent = False
    structured_error_429 = False
    for value in documents:
        has_429, messages, envelope_complete = _json_error_evidence(value)
        structured_error_429 = structured_error_429 or has_429
        complete = complete and envelope_complete
        if any(_evidence_matches(_QUOTA_RE, message) for message in messages):
            permanent = True
        if has_429 and any(
            _evidence_matches(_PERMANENT_DENIAL_RE, message)
            for message in messages
        ):
            permanent = True
    plain = _plain_denial_analysis(text, json_spans)
    return _RetryDenialAnalysis(
        permanent=permanent or plain.permanent,
        complete=complete and plain.complete,
        error_429=error_429 or structured_error_429 or plain.error_429,
    )


def _has_permanent_rate_limit_denial(text: str) -> bool:
    """Backward-compatible bool view of the structured denial analysis."""
    return _analyze_permanent_rate_limit_denial(text).permanent


def _bounded_retry_after(value: object, *, now: Optional[float] = None) -> Optional[float]:
    """Parse one Retry-After value without preserving or reflecting raw text."""
    seconds: Optional[float] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate or len(candidate) > 64:
            return None
        numeric = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)?", candidate, re.I)
        if numeric:
            seconds = float(numeric.group(1))
        else:
            try:
                parsed = parsedate_to_datetime(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                wall_now = datetime.fromtimestamp(
                    now if now is not None else datetime.now(timezone.utc).timestamp(),
                    tz=timezone.utc,
                )
                seconds = (parsed - wall_now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
    if seconds is None or seconds <= 0 or seconds != seconds:
        return None
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _parse_retry_after(text: str, *, now: Optional[float] = None) -> Optional[float]:
    """Extract a bounded Retry-After from provider status/error metadata."""
    for match in _RETRY_AFTER_HEADER_RE.finditer(text or ""):
        parsed = _bounded_retry_after(match.group(1), now=now)
        if parsed is not None:
            return parsed
    for match in _RETRY_AFTER_JSON_RE.finditer(text or ""):
        raw = match.group(1)
        if raw.startswith('"'):
            try:
                value: object = json.loads(raw)
            except (TypeError, ValueError):
                continue
        else:
            try:
                value = float(raw)
            except ValueError:
                continue
        parsed = _bounded_retry_after(value, now=now)
        if parsed is not None:
            return parsed
    return None


def _retry_reason(
    text: str,
    analysis: Optional[_RetryDenialAnalysis] = None,
) -> str:
    """Return a private, Core-owned retry disposition for provider output."""
    denial = analysis or _analyze_permanent_rate_limit_denial(text)
    if not denial.complete:
        # A parser/size boundary means some denial evidence was not inspected.
        # Never turn that uncertainty into an automatic replay.
        return "incomplete_evidence"
    if _POLICY_RE.search(text):
        return "policy"
    if denial.permanent:
        return "quota"
    if _AUTHORIZATION_RE.search(text):
        return "authorization"
    if _AUTH_RE.search(text):
        return "auth"
    if _TRANSIENT_429_RE.search(text):
        return "transient_rate_limit"
    if _TRANSIENT_NETWORK_RE.search(text):
        return "transient_network"
    return "permanent"


def _attach_retry_metadata(error: UnifiedError, text: str) -> UnifiedError:
    analysis = _analyze_permanent_rate_limit_denial(text)
    reason = _retry_reason(text, analysis)
    error._retry_reason = reason  # type: ignore[attr-defined]
    error._retry_classification_complete = analysis.complete  # type: ignore[attr-defined]
    error._retry_error_shaped_429 = analysis.error_429  # type: ignore[attr-defined]
    error._retry_after = (  # type: ignore[attr-defined]
        _parse_retry_after(text) if reason == "transient_rate_limit" else None
    )
    error._retry_pre_request = bool(  # type: ignore[attr-defined]
        reason == "transient_network" and _PRE_REQUEST_NETWORK_RE.search(text)
    )
    return error


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
        return _attach_retry_metadata(UnifiedError(
            kind="internal",
            provider=provider,
            message=t("err.plugin.runtime", provider=provider),
            hint=t("err.plugin.runtime.hint"),
            cause="extension provider process failed",
        ), "")
    for pattern, kind, hint_key in matchers:
        if pattern.search(haystack):
            return _attach_retry_metadata(UnifiedError(
                kind=kind,
                provider=provider,
                message=_default_message(kind, provider),
                hint=_resolve_hint(hint_key),
                cause=_extract_cause(stderr, stdout),
            ), haystack)
    return _attach_retry_metadata(UnifiedError(
        kind="internal",
        provider=provider,
        message=t("err.msg.unknown", provider=provider, exitcode=exitcode),
        hint=t("err.hint.check_stderr"),
        cause=_extract_cause(stderr, stdout),
    ), haystack)


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
