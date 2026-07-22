"""Deterministic side-effect-aware retry contracts (no vendor/network access)."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

import unified_cli.base as base_module
import unified_cli.providers.gemini as gemini_module
from unified_cli import ClaudeProvider, CodexProvider, GeminiProvider, UnifiedError
from unified_cli.base import _retry_evidence_proves_no_side_effects
from unified_cli.errors import (
    _RETRY_CLASSIFIER_MAX_BYTES,
    _RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS,
    _RETRY_CLASSIFIER_MAX_LINE,
    _RETRY_CLASSIFIER_MAX_MESSAGE,
    _RETRY_CLASSIFIER_MAX_PLAIN_LINES,
    _parse_retry_after,
    classify,
)


@pytest.fixture
def retry_cli(tmp_path: Path, monkeypatch):
    source = Path(__file__).with_name("fixtures") / "core_provider_cli.py"
    executable = tmp_path / "fake-retry-cli"
    executable.write_text(
        source.read_text(encoding="utf-8").replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}", 1,
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    capture = tmp_path / "calls.jsonl"

    def calls() -> list[dict]:
        if not capture.exists():
            return []
        return [json.loads(line) for line in capture.read_text().splitlines()]

    def make(provider_name: str, mode: str, *, waits=None, async_waits=None):
        env = {
            "FAKE_PROVIDER": provider_name,
            "FAKE_RESPONSE": mode,
            "FAKE_CAPTURE": str(capture),
        }
        common = {
            "bin_path": str(executable),
            "extra_env": env,
            "web_search": False,
            "_retry_random": lambda: 0.5,
            "_retry_clock": lambda: 0.0,
        }
        if waits is not None:
            common["_retry_wait"] = (
                lambda delay, event: waits.append(delay) or False
            )
        if async_waits is not None:
            async def record_async_wait(delay):
                async_waits.append(delay)
            common["_retry_async_wait"] = record_async_wait
        if provider_name == "claude":
            return ClaudeProvider(**common)
        if provider_name == "codex":
            return CodexProvider(**common)
        monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
        conversations = tmp_path / "conversations"
        env["FAKE_CONVERSATIONS_DIR"] = str(conversations)
        provider = GeminiProvider(
            conversations_dir=str(conversations), **common,
        )
        provider._validate_model = lambda model: None
        return provider

    return make, calls


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_nonstream_transient_network_retries_with_strict_attempt_cap(
    retry_cli, provider_name
):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, "transient_network", waits=waits)

    response = provider.chat("retry safely")

    assert response.text
    assert len(calls()) == 3
    assert waits == [0.5, 1.0]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("retry_after_valid", 2.0),
        ("retry_after_invalid", 0.5),
        ("retry_after_capped", 5.0),
    ],
)
def test_retry_after_is_honored_or_falls_back_and_is_capped(
    retry_cli, mode, expected
):
    make, calls = retry_cli
    waits = []
    provider = make("claude", mode, waits=waits)

    assert provider.chat("bounded rate limit").text
    assert len(calls()) == 2
    assert waits == [expected]


def test_retry_after_waits_share_a_strict_total_delay_cap(retry_cli):
    make, calls = retry_cli
    waits = []
    provider = make("claude", "retry_after_repeated", waits=waits)

    assert provider.chat("bounded total delay").text
    assert len(calls()) == 3
    assert waits == [5.0, 3.0]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("exit_401", "auth"),
        ("exit_403", "authorization"),
        ("quota", "quota"),
        ("policy", "policy"),
        ("plan_denial", "quota"),
    ],
)
@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_permanent_failures_never_replay(
    retry_cli, mode, reason, provider_name
):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, mode, waits=waits)

    with pytest.raises(UnifiedError) as caught:
        provider.chat("do not replay")

    assert getattr(caught.value, "_retry_reason") == reason
    assert len(calls()) == 1
    assert waits == []


@pytest.mark.parametrize(
    "message",
    [
        "429 weekly usage limit reached",
        "429 daily limit has been reached",
        "429 monthly usage limit exhausted",
        "429 usage limit exceeded",
        "429 reached your weekly usage limit",
        "429 You have hit your usage limit",
        "429 You've hit your usage limit",
        "429 You’ve hit the usage limit",
        "429 You hit your usage limit",
        "429 hit your weekly limit",
        "429 You've hit the monthly limit",
        "429 hit your daily usage limit",
        "429 no credits remaining",
        "429 no credit remaining",
        "429 no credits remain",
        "429 no remaining credits",
        "429 quota limit reached",
        "429 quota limit has been exceeded",
        "429 quota limit is exhausted",
    ],
)
def test_persistent_usage_limit_wording_is_quota_not_transient(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "quota"


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota",
        "Error: quota exceeded",
        "no credits remaining",
        "weekly usage limit reached",
        "You've hit the monthly limit",
        '{"error":{"message":"no credits remaining"}}',
    ],
)
def test_existing_quota_diagnostics_remain_permanent_without_numeric_status(
    message,
):
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "quota"


@pytest.mark.parametrize(
    "message",
    [
        "429 subscription limit reached",
        "429 subscription usage limit has been exceeded",
        "HTTP 429 subscription limit is exhausted",
        "Error: 429 billing limit reached",
        "status=429 billing account disabled",
        "429 billing account has been suspended",
        "429 this account is not entitled to use the model",
        "429 you are not eligible for this feature",
        "429 account entitlement exhausted",
        "429 entitlement is required for this model",
        "429 your plan does not include this model",
        "429 this plan doesn't allow agent mode",
        "429 the plan does not support this feature",
        "429 this plan does not allow agent mode",
        "429 plan does not include this model",
        "429 upgrade plan to continue",
        "429 upgrade your plan to continue",
        "429 upgrade your subscription to continue",
        "429 a subscription is required",
        "429 subscription expired",
        "429 payment required",
    ],
)
def test_permanent_429_account_denials_are_never_transient(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "quota"


@pytest.mark.parametrize(
    "message",
    [
        "429 account disabled",
        "429 account has been disabled",
        "429 account's been disabled.",
        "429 subscription has expired",
        "429 subscription’s expired.",
        "429 billing is disabled for this account",
        "429 billing's disabled for the account.",
        "status_code=429 payment required",
        "HTTP status 429: payment required",
        "HTTP status: 429 — payment required.",
        "Error HTTP status 429; payment: required.",
        "429 billing's been disabled for this account",
        "HTTP status code: 429 payment required",
        "429 payment—required",
        "429 billing is disabled for your account",
        "429 payment’s required",
        "429 payment = required",
        "429 payment’s = required",
        "429 billing's—been disabled for your account",
        "429 plan isn't supported",
    ],
)
def test_additional_permanent_account_denial_variants(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "message",
    [
        "status code 429",
        "status-code=429",
        "HTTP_STATUS=429",
        "HTTP_STATUS_CODE=429",
        "statusCode=429",
        "httpStatusCode=429",
        "HTTPStatusCode: 429",
    ],
)
def test_normalized_plain_status_spellings_remain_transient_without_denial(
    message,
):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "transient_rate_limit"
    assert getattr(error, "_retry_classification_complete") is True
    assert getattr(error, "_retry_error_shaped_429") is True


@pytest.mark.parametrize(
    "message",
    [
        "status/429 payment required",
        "status_code/429/payment/required",
        "status-code/429/payment-required",
        "HTTP/429/payment—required",
        "Error: status/429 payment required",
        "fatal/status_code/429/payment’s/required",
        "error/status-code/429/billing's/been/disabled/for/this/account",
        "Error/HTTP/429 payment required",
        "fatal/HTTP/status/429 payment required",
        "ERROR/HTTP/status-code/429/plan/isn't/supported",
    ],
)
def test_slash_normalized_status_denial_matrix(message):
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True
    assert getattr(error, "_retry_error_shaped_429") is True


def test_http_1_1_version_status_remains_permanent():
    error = classify("codex", stderr="HTTP/1.1 429 payment required")

    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "message",
    [
        "https://example.com/status/429/payment/required",
        "http://example.com/status/429/payment/required",
        "/status/429/payment/required",
        "/api/v1/status/429/payment/required",
        "docs/status/429/payment/required",
        "documentation says status/429 payment required",
        '"status/429 payment required"',
        "assistant/status/429/payment/required",
        "content/status_code/429/payment/required",
        "pkg/status-code/429/payment/required",
    ],
)
def test_slash_normalization_does_not_promote_prose_or_paths(message):
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "transient_rate_limit"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "status",
    [
        "status code 429", "status-code=429", "HTTP_STATUS=429",
        "HTTP_STATUS_CODE=429", "httpStatusCode=429",
    ],
)
@pytest.mark.parametrize(
    "denial",
    [
        "billing is disabled for your account",
        "payment’s required",
        "plan isn't supported",
    ],
)
def test_normalized_status_and_following_denial_are_combined(status, denial):
    error = classify("codex", stderr=f"{status}\n{denial}")

    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "status_key",
    [
        "statusCode", "httpStatusCode", "HTTP_STATUS", "HTTP_STATUS_CODE",
        "status-code", "http-status-code",
    ],
)
def test_normalized_json_status_keys_preserve_error_envelope(status_key):
    message = json.dumps({
        status_key: 429,
        "error": {"message": "payment—required"},
    })
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "message",
    [
        "Documentation: 429 account disabled",
        "Assistant response: HTTP status 429 payment required",
        "The docs discuss status_code=429 payment required",
        "Example prose says subscription has expired after an HTTP 429",
        "documentation says HTTP status code 429 payment required",
        "assistant: HTTP_STATUS_CODE=429 payment required",
        "content=HTTP status code: 429 payment required",
        '"HTTP status code 429 payment required"',
        "'status-code=429 payment required'",
        "(HTTP status code 429 payment required)",
        "`HTTP_STATUS_CODE=429 payment required`",
        "— HTTP status code 429 payment required",
        "pkg status-code=429 payment required",
        "pkg.httpStatusCode=429 payment required",
    ],
)
def test_additional_denial_vocabulary_in_ordinary_prose_is_not_permanent(
    message,
):
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") != "quota"


@pytest.mark.parametrize("root_key", ["type", "kind"])
@pytest.mark.parametrize(
    "event_type",
    [
        "assistant", "assistant.message", "text", "content", "output",
        "response", "result", "tool_use", "tool-result",
    ],
)
def test_json_public_root_event_type_cannot_form_error_envelope(
    root_key, event_type,
):
    message = json.dumps({
        root_key: event_type,
        "status": 429,
        "message": "payment required; account disabled",
    })

    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "transient_rate_limit"
    assert getattr(error, "_retry_classification_complete") is True


def test_json_public_root_stays_public_with_normalized_status_key():
    message = json.dumps({
        "kind": "assistant.output",
        "httpStatusCode": 429,
        "message": "payment—required; plan isn't supported",
    })
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "transient_rate_limit"
    assert getattr(error, "_retry_classification_complete") is True


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "error", "status": 429, "message": "payment required"},
        {"type": "error-response", "message": "account disabled",
         "code": 429},
        {"kind": "failed", "status_code": 429,
         "detail": "subscription has expired"},
        {"error": {"code": 429, "reason": "PAYMENT_REQUIRED"}},
        {"type": "assistant", "status": 429, "message": "public text",
         "error": {"code": 429, "message": "account disabled"}},
    ],
)
def test_json_genuine_error_types_and_nested_envelopes_remain_permanent(payload):
    error = classify("codex", stderr=json.dumps(payload))

    assert getattr(error, "_retry_reason") == "quota"
    assert getattr(error, "_retry_classification_complete") is True


def _sized_json_429(total_size: int) -> str:
    prefix = '{"status":429,"error":{"message":"server busy"},"padding":"'
    suffix = '"}'
    assert total_size >= len(prefix) + len(suffix)
    return prefix + "x" * (total_size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize(
    ("stderr_size", "complete", "reason"),
    [
        (_RETRY_CLASSIFIER_MAX_BYTES - 1, True, "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_BYTES, False, "incomplete_evidence"),
        (_RETRY_CLASSIFIER_MAX_BYTES + 1, False, "incomplete_evidence"),
    ],
)
def test_retry_classifier_byte_boundary_counts_classify_separator(
    stderr_size, complete, reason,
):
    # classify() appends one newline between stderr/stdout.  Thus stderr at
    # MAX-1 consumes exactly the complete-analysis byte budget.
    error = classify("codex", stderr=_sized_json_429(stderr_size))

    assert getattr(error, "_retry_reason") == reason
    assert getattr(error, "_retry_classification_complete") is complete
    assert getattr(error, "_retry_error_shaped_429") is True


def test_oversized_input_blocks_retry_even_when_429_is_after_bounded_prefix():
    message = "x" * _RETRY_CLASSIFIER_MAX_BYTES + "\nHTTP 429 rate limit"
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "incomplete_evidence"
    assert getattr(error, "_retry_classification_complete") is False


@pytest.mark.parametrize(
    ("document_count", "complete", "reason"),
    [
        (_RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS - 1, True,
         "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS, True,
         "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_JSON_DOCUMENTS + 1, False,
         "incomplete_evidence"),
    ],
)
def test_retry_classifier_json_document_boundary(document_count, complete, reason):
    document = json.dumps({
        "status": 429, "error": {"message": "server busy"},
    })
    error = classify("codex", stderr="\n".join([document] * document_count))

    assert getattr(error, "_retry_reason") == reason
    assert getattr(error, "_retry_classification_complete") is complete


@pytest.mark.parametrize(
    ("message_size", "complete", "reason"),
    [
        (_RETRY_CLASSIFIER_MAX_MESSAGE, True, "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_MESSAGE + 1, False, "incomplete_evidence"),
    ],
)
def test_retry_classifier_message_boundary(message_size, complete, reason):
    message = json.dumps({
        "status": 429, "error": {"message": "x" * message_size},
    })
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == reason
    assert getattr(error, "_retry_classification_complete") is complete


@pytest.mark.parametrize(
    ("line_size", "complete", "reason"),
    [
        (_RETRY_CLASSIFIER_MAX_LINE, True, "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_LINE + 1, False, "incomplete_evidence"),
    ],
)
def test_retry_classifier_plain_line_boundary(line_size, complete, reason):
    prefix = "HTTP 429 rate limit reached "
    message = prefix + "x" * (line_size - len(prefix))
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == reason
    assert getattr(error, "_retry_classification_complete") is complete
    assert getattr(error, "_retry_error_shaped_429") is True


@pytest.mark.parametrize(
    ("line_count", "complete", "reason"),
    [
        (_RETRY_CLASSIFIER_MAX_PLAIN_LINES, True, "transient_rate_limit"),
        (_RETRY_CLASSIFIER_MAX_PLAIN_LINES + 1, False, "incomplete_evidence"),
    ],
)
def test_retry_classifier_plain_line_count_boundary(line_count, complete, reason):
    lines = ["HTTP 429 rate limit reached"] + ["diagnostic"] * (line_count - 1)
    error = classify("codex", stderr="\n".join(lines))

    assert getattr(error, "_retry_reason") == reason
    assert getattr(error, "_retry_classification_complete") is complete


@pytest.mark.parametrize(
    "message",
    [
        '{"status":429,"error":',
        '{"status":429,"error":' + "[" * 2000 + "0" + "]" * 2000 + "}",
        json.dumps({"status": 429, "details": list(range(300))}),
    ],
)
def test_decode_or_structure_skip_is_explicitly_incomplete(message):
    error = classify("codex", stderr=message)

    assert getattr(error, "_retry_reason") == "incomplete_evidence"
    assert getattr(error, "_retry_classification_complete") is False
    assert getattr(error, "_retry_error_shaped_429") is True


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429 Too Many Requests\nmessage: entitlement required",
        "HTTP/1.1 429 Too Many Requests\nthis plan doesn't support agents",
        "status=429\nbody: billing limit exceeded",
        "Error: status code: 429\nreason: billing account suspended",
        "RESOURCE_EXHAUSTED(429)\ndetail: subscription expired",
    ],
)
def test_permanent_429_denial_accepts_one_tightly_bounded_body_line(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "quota"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 429, "error": {"message": "payment required"}},
        {"error": {"message": "payment required"}, "status": 429},
        {"error": {"details": [{"reason": "entitlement exhausted"}],
                   "code": 429}},
        {"error": {"reason": "SUBSCRIPTION_EXPIRED", "code": 429}},
        {"error": {"message": "subscription limit exceeded"},
         "status": {"code": 429}},
    ],
)
@pytest.mark.parametrize("pretty", [False, True])
def test_json_denial_is_order_independent_and_pretty_print_safe(payload, pretty):
    message = json.dumps(payload, indent=2 if pretty else None)

    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "quota"


@pytest.mark.parametrize("payload_key", ["assistant", "content", "output", "text"])
def test_json_public_payload_fields_cannot_supply_denial(payload_key):
    message = json.dumps({
        "status": 429,
        "error": {"message": "server is at capacity"},
        payload_key: {"message": "subscription expired; payment required"},
    })

    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "transient_rate_limit"


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429 Too Many Requests\nassistant: subscription expired",
        "HTTP 429 Too Many Requests\nserver busy\npayment required",
        "assistant:\n429 payment required",
        "Documentation example: HTTP 429 payment required",
        "An assistant discussed HTTP 429 entitlement required",
    ],
)
def test_later_or_non_diagnostic_plain_text_cannot_supply_denial(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "transient_rate_limit"


@pytest.mark.parametrize(
    "message",
    [
        "HTTP/1.1 429 Too Many Requests: rate limit reached",
        "status=429 concurrency limit reached",
        "RESOURCE_EXHAUSTED(429): request limit reached",
        "Error: HTTP 429 server-capacity limit reached",
    ],
)
def test_transient_429_controls_remain_retryable(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "transient_rate_limit"


def test_hostile_denial_evidence_is_bounded_and_cannot_authorize_replay():
    hostile_inputs = [
        '{"status":429,"error":' + "[" * 2000 + '"payment required"'
        + "]" * 2000 + "}",
        "HTTP 429 " + "x" * (129 * 1024) + " payment required",
        ("{\n" * 2000) + '"status":429,"message":"payment required"',
    ]
    started = time.monotonic()

    for message in hostile_inputs:
        error = classify("codex", stderr=message)
        assert not _retry_evidence_proves_no_side_effects("", message, error)

    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    "mode", ["entitlement_denial_multiline", "plan_denial_json"],
)
@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_structured_account_denials_execute_exactly_one_attempt(
    retry_cli, provider_name, mode
):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, mode, waits=waits)

    with pytest.raises(UnifiedError) as caught:
        provider.chat("never replay an account denial")

    assert getattr(caught.value, "_retry_reason") == "quota"
    assert len(calls()) == 1
    assert waits == []


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_incomplete_ninth_json_429_executes_exactly_one_attempt(
    retry_cli, provider_name,
):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, "incomplete_ninth_json_429", waits=waits)

    with pytest.raises(UnifiedError) as caught:
        provider.chat("never replay incompletely classified evidence")

    assert getattr(caught.value, "_retry_reason") == "incomplete_evidence"
    assert getattr(caught.value, "_retry_classification_complete") is False
    assert len(calls()) == 1
    assert waits == []


@pytest.mark.parametrize(
    "mode",
    [
        "account_disabled_denial", "payment_required_denial",
        "normalized_billing_denial", "normalized_payment_denial",
        "normalized_plan_denial", "slash_normalized_denial",
    ],
)
@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_additional_denial_variants_execute_exactly_one_attempt(
    retry_cli, provider_name, mode,
):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, mode, waits=waits)

    with pytest.raises(UnifiedError) as caught:
        provider.chat("never replay a permanent account denial")

    assert getattr(caught.value, "_retry_reason") == "quota"
    assert getattr(caught.value, "_retry_classification_complete") is True
    assert len(calls()) == 1
    assert waits == []


@pytest.mark.parametrize(
    "message",
    [
        "429 rate limit reached",
        "429 concurrency limit reached",
        "429 request limit reached",
        "429 usage limit reset is pending",
        "429 complete your daily task before retrying",
        "429 read the usage instructions before retrying",
        "429 hit your daily task limit",
        "429 no credits were charged for this request",
        "429 quota limit documentation is unavailable",
        "429 server capacity limit reached",
        "429 server is at capacity",
        "An assistant discussed 429 subscription limit reached",
        "Documentation example: 429 billing account suspended",
    ],
)
def test_non_quota_limit_wording_is_not_overclassified(message):
    error = classify("codex", stderr=message)

    assert error.kind == "rate_limit"
    assert getattr(error, "_retry_reason") == "transient_rate_limit"


@pytest.mark.parametrize(
    ("provider_name", "key_name"),
    [("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")],
)
def test_nonstream_401_never_switches_to_inherited_api_key(
    retry_cli, monkeypatch, provider_name, key_name
):
    make, calls = retry_cli
    monkeypatch.setenv(key_name, "metered-key-must-not-be-injected")
    provider = make(provider_name, "exit_401", waits=[])

    with pytest.raises(UnifiedError) as caught:
        provider.chat("do not change credentials")

    assert caught.value.kind == "auth_expired"
    assert len(calls()) == 1


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("mode", ["tool_then_failure", "tool_then_429"])
def test_nonstream_tool_evidence_blocks_transient_replay(
    retry_cli, provider_name, mode
):
    make, calls = retry_cli
    provider = make(provider_name, mode, waits=[])

    with pytest.raises(UnifiedError):
        provider.chat("tool may have changed state")

    assert len(calls()) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "status": 429, "error": {"message": "busy"}},
        {"type": "done", "status": 429},
        {"type": "session", "status": 429, "session_id": "s"},
        {"type": "usage", "status": 429, "usage": {"input": 1}},
        {"type": "reasoning", "status": 429, "reasoning": "partial"},
        {"type": "text", "status": 429, "text": "partial"},
        {"type": "tool_use", "status": 429, "tool": {"name": "write"}},
    ],
)
def test_any_raw_public_stdout_event_blocks_429_replay(event):
    error = classify("codex", stderr="429 Too Many Requests")

    assert not _retry_evidence_proves_no_side_effects(
        json.dumps(event), "429 Too Many Requests", error,
    )


def test_only_strict_pre_turn_error_envelopes_are_replay_safe():
    error = classify("codex", stderr="429 Too Many Requests")
    safe = json.dumps({
        "error": {
            "message": "temporarily busy",
            "type": "rate_limit_error",
            "code": 429,
        },
        "retry_after": 1,
        "request_id": "req-1",
    })

    assert _retry_evidence_proves_no_side_effects(
        safe, "429 Too Many Requests", error,
    )
    for partial in (
        {"assistant": {"content": "partial output"}},
        {"output": "partial assistant response"},
    ):
        partial_stderr = json.dumps(partial) + "\n429 Too Many Requests"
        assert not _retry_evidence_proves_no_side_effects(
            "", partial_stderr, error,
        )


def test_deep_untrusted_json_fails_closed_without_recursion_error():
    error = classify("codex", stderr="429 Too Many Requests")
    deep = '{"error":' + '[' * 2000 + '0' + ']' * 2000 + '}'

    assert not _retry_evidence_proves_no_side_effects(
        deep, "429 Too Many Requests", error,
    )


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize(
    "mode",
    ["raw_error_event_then_429", "stderr_partial_then_429", "deep_json_then_429"],
)
def test_adversarial_nonstream_evidence_surfaces_original_error_without_replay(
    retry_cli, provider_name, mode
):
    make, calls = retry_cli
    provider = make(provider_name, mode, waits=[])

    with pytest.raises(UnifiedError) as caught:
        provider.chat("fail closed")

    assert caught.value.kind == "rate_limit"
    assert len(calls()) == 1


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_sync_stream_retries_only_before_public_output(retry_cli, provider_name):
    make, calls = retry_cli
    waits = []
    provider = make(provider_name, "transient_network", waits=waits)

    messages = list(provider.stream("retry before output"))

    assert len(calls()) == 3
    assert waits == [0.5, 1.0]
    assert sum(message.kind == "done" for message in messages) == 1


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("mode", ["tool_then_failure", "tool_then_429"])
def test_stream_tool_event_then_failure_is_not_duplicated(
    retry_cli, provider_name, mode
):
    make, calls = retry_cli
    provider = make(provider_name, mode, waits=[])
    stream = provider.stream("tool then fail")

    first = next(stream)
    assert first.kind in {"tool_use", "text"}
    with pytest.raises(UnifiedError):
        next(stream)
    assert len(calls()) == 1


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize(
    "mode",
    ["raw_error_event_then_429", "stderr_partial_then_429", "deep_json_then_429"],
)
def test_adversarial_sync_stream_evidence_never_replays(
    retry_cli, provider_name, mode
):
    make, calls = retry_cli
    provider = make(provider_name, mode, waits=[])

    with pytest.raises(UnifiedError) as caught:
        list(provider.stream("fail closed while streaming"))

    assert caught.value.kind == "rate_limit"
    assert len(calls()) == 1


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_sync_cancel_after_reader_dequeue_prevents_public_yield_and_cleans_up(
    retry_cli, monkeypatch, provider_name
):
    make, calls = retry_cli
    provider = make(provider_name, "ok", waits=[])
    cancel = threading.Event()
    readers = []
    module = gemini_module if provider_name == "gemini" else base_module
    original_reader = module._StreamReader

    class CancelRaceReader(original_reader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            self.wait_closed_calls = 0
            readers.append(self)

        def __iter__(self):
            for line in super().__iter__():
                cancel.set()
                yield line

        def close(self):
            self.close_calls += 1
            return super().close()

        def wait_closed(self, timeout):
            self.wait_closed_calls += 1
            return super().wait_closed(timeout)

    monkeypatch.setattr(module, "_StreamReader", CancelRaceReader)

    emitted = []
    with pytest.raises(UnifiedError) as caught:
        for message in provider.stream(
            "cancel after sync dequeue", cancel_event=cancel,
        ):
            emitted.append(message)

    assert getattr(caught.value, "_cancelled", False)
    assert emitted == []
    assert len(calls()) == 1
    assert len(readers) == 1
    assert readers[0].close_calls == 1
    assert readers[0].wait_closed_calls >= 1
    assert readers[0].is_closed()
    assert readers[0]._proc.poll() is not None


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
def test_async_stream_retry_parity(retry_cli, provider_name):
    make, calls = retry_cli
    sync_waits = []
    async_waits = []
    provider = make(
        provider_name, "transient_network",
        waits=sync_waits, async_waits=async_waits,
    )

    async def consume():
        return [message async for message in provider.astream("async retry")]

    messages = asyncio.run(consume())
    assert len(calls()) == 3
    assert sum(message.kind == "done" for message in messages) == 1
    if provider_name == "gemini":
        assert sync_waits == [0.5, 1.0]
        assert async_waits == []
    else:
        assert async_waits == [0.5, 1.0]
        assert sync_waits == []


@pytest.mark.parametrize("provider_name", ["claude", "codex"])
def test_async_cancel_after_dequeue_prevents_public_yield(
    retry_cli, monkeypatch, provider_name
):
    make, _ = retry_cli
    provider = make(provider_name, "ok", waits=[])
    cancel = threading.Event()
    dequeued_kinds = []
    original_protocol = base_module._AsyncJsonlProtocol

    class CancelAfterGetQueue(asyncio.Queue):
        async def get(self):
            item = await super().get()
            dequeued_kinds.append(item[0])
            cancel.set()
            return item

    class CancelRaceProtocol(original_protocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.queue = CancelAfterGetQueue()

    monkeypatch.setattr(base_module, "_AsyncJsonlProtocol", CancelRaceProtocol)

    async def consume():
        emitted = []
        with pytest.raises(UnifiedError) as caught:
            async for message in provider.astream(
                "cancel after dequeue", cancel_event=cancel,
            ):
                emitted.append(message)
        assert getattr(caught.value, "_cancelled", False)
        assert emitted == []

    asyncio.run(consume())
    assert dequeued_kinds[0] == "line"


@pytest.mark.parametrize("provider_name", ["claude", "codex"])
def test_deep_untrusted_json_in_async_stream_never_replays(
    retry_cli, provider_name
):
    make, calls = retry_cli
    provider = make(provider_name, "deep_json_then_429", waits=[])

    async def consume():
        with pytest.raises(UnifiedError) as caught:
            async for _ in provider.astream("deep untrusted event"):
                pass
        assert caught.value.kind == "rate_limit"

    asyncio.run(consume())
    assert len(calls()) == 1


def test_sync_retry_wait_observes_cancellation(retry_cli):
    make, calls = retry_cli
    cancel = threading.Event()

    def cancel_during_wait(delay, event):
        assert delay == 0.5
        cancel.set()
        return event.wait(delay)

    provider = make("claude", "transient_network")
    provider._retry_wait = cancel_during_wait

    with pytest.raises(UnifiedError) as caught:
        provider.chat("cancel retry", cancel_event=cancel)

    assert getattr(caught.value, "_cancelled", False)
    assert len(calls()) == 1


def test_async_retry_wait_observes_cancellation_promptly(retry_cli):
    make, calls = retry_cli
    waiting = threading.Event()
    wait_calls = []
    clock_calls = []

    class ObservedEvent:
        def __init__(self):
            self._event = threading.Event()

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self._event.set()

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            waiting.set()
            return self._event.wait(timeout)

    cancel = ObservedEvent()
    provider = make("claude", "transient_network")
    provider._retry_clock = lambda: clock_calls.append(10.0) or 10.0

    async def consume():
        async def stream_to_cancel():
            with pytest.raises(UnifiedError) as caught:
                async for _ in provider.astream(
                    "cancel async", cancel_event=cancel,
                ):
                    pass
            assert getattr(caught.value, "_cancelled", False)

        task = asyncio.create_task(stream_to_cancel())
        observed = await asyncio.get_running_loop().run_in_executor(
            None, waiting.wait, 1.0,
        )
        assert observed
        cancel.set()
        await task

    asyncio.run(consume())
    assert len(calls()) == 1
    assert wait_calls == [0.5]
    assert clock_calls == [10.0]


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_cancel_at_nonstream_completion_blocks_success_and_usage_record(
    retry_cli, monkeypatch, provider_name, async_mode
):
    make, calls = retry_cli
    provider = make(provider_name, "ok", waits=[])
    cancel = threading.Event()
    records = []
    parse_response = provider._parse_json_response

    def parse_then_cancel(text, model):
        response = parse_response(text, model)
        cancel.set()
        return response

    monkeypatch.setattr(provider, "_parse_json_response", parse_then_cancel)
    monkeypatch.setattr(
        base_module._usage_tracker, "record",
        lambda *args, **kwargs: records.append(kwargs),
    )

    if async_mode:
        async def invoke():
            return await provider.achat("cancel completed call", cancel_event=cancel)

        with pytest.raises(UnifiedError) as caught:
            asyncio.run(invoke())
    else:
        with pytest.raises(UnifiedError) as caught:
            provider.chat("cancel completed call", cancel_event=cancel)

    assert getattr(caught.value, "_cancelled", False)
    assert len(calls()) == 1
    assert all("input_tokens" not in record for record in records)


@pytest.mark.parametrize("async_mode", [False, True])
def test_codex_cancel_after_usage_blocks_done(retry_cli, async_mode):
    make, _ = retry_cli
    provider = make("codex", "ok", waits=[])
    cancel = threading.Event()
    emitted = []

    if async_mode:
        async def consume():
            with pytest.raises(UnifiedError) as caught:
                async for message in provider.astream(
                    "cancel after usage", cancel_event=cancel,
                ):
                    emitted.append(message)
                    if message.kind == "usage":
                        cancel.set()
            assert getattr(caught.value, "_cancelled", False)

        asyncio.run(consume())
    else:
        with pytest.raises(UnifiedError) as caught:
            for message in provider.stream(
                "cancel after usage", cancel_event=cancel,
            ):
                emitted.append(message)
                if message.kind == "usage":
                    cancel.set()
        assert getattr(caught.value, "_cancelled", False)

    assert sum(message.kind == "usage" for message in emitted) == 1
    assert all(message.kind != "done" for message in emitted)


@pytest.mark.parametrize("async_mode", [False, True])
def test_claude_cancel_after_usage_blocks_result_tail(retry_cli, async_mode):
    make, _ = retry_cli
    provider = make("claude", "ok", waits=[])
    cancel = threading.Event()
    emitted = []

    if async_mode:
        async def consume():
            with pytest.raises(UnifiedError) as caught:
                async for message in provider.astream(
                    "cancel claude result tail", cancel_event=cancel,
                ):
                    emitted.append(message)
                    if message.kind == "usage":
                        cancel.set()
            assert getattr(caught.value, "_cancelled", False)

        asyncio.run(consume())
    else:
        with pytest.raises(UnifiedError) as caught:
            for message in provider.stream(
                "cancel claude result tail", cancel_event=cancel,
            ):
                emitted.append(message)
                if message.kind == "usage":
                    cancel.set()
        assert getattr(caught.value, "_cancelled", False)

    assert emitted[-1].kind == "usage"
    assert all(message.kind != "done" for message in emitted)


@pytest.mark.parametrize("async_mode", [False, True])
def test_gemini_cancel_after_text_blocks_session_and_done(retry_cli, async_mode):
    make, _ = retry_cli
    provider = make("gemini", "ok", waits=[])
    cancel = threading.Event()
    emitted = []

    if async_mode:
        async def consume():
            with pytest.raises(UnifiedError) as caught:
                async for message in provider.astream(
                    "cancel gemini tail", cancel_event=cancel,
                ):
                    emitted.append(message)
                    if message.kind == "text":
                        cancel.set()
            assert getattr(caught.value, "_cancelled", False)

        asyncio.run(consume())
    else:
        with pytest.raises(UnifiedError) as caught:
            for message in provider.stream(
                "cancel gemini tail", cancel_event=cancel,
            ):
                emitted.append(message)
                if message.kind == "text" and "second line" in message.text:
                    cancel.set()
        assert getattr(caught.value, "_cancelled", False)

    assert emitted
    assert all(message.kind == "text" for message in emitted)


@pytest.mark.parametrize("provider_name", ["claude", "gemini"])
def test_executor_async_bridge_cancellation_stops_sync_retry(
    retry_cli, provider_name
):
    make, calls = retry_cli
    waiting = threading.Event()
    provider = make(provider_name, "transient_network")

    def blocking_wait(delay, event):
        waiting.set()
        return event.wait(delay)

    provider._retry_wait = blocking_wait

    async def run():
        if provider_name == "claude":
            task = asyncio.create_task(provider.achat("cancel executor retry"))
        else:
            async def consume_gemini():
                return [
                    message async for message in provider.astream(
                        "cancel executor retry"
                    )
                ]
            task = asyncio.create_task(consume_gemini())
        observed = await asyncio.get_running_loop().run_in_executor(
            None, waiting.wait, 1.0,
        )
        assert observed
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.3
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(calls()) == 1


def test_error_retry_metadata_is_private_and_retry_after_parser_is_defensive():
    transient = classify(
        "codex", stderr='429 Too Many Requests\n{"retry_after":"1.25s"}',
    )
    quota = classify("codex", stderr="429 insufficient_quota")
    policy = classify("codex", stderr="429 request blocked by content policy")

    assert transient.kind == quota.kind == policy.kind == "rate_limit"
    assert getattr(transient, "_retry_reason") == "transient_rate_limit"
    assert getattr(transient, "_retry_after") == 1.25
    assert getattr(quota, "_retry_reason") == "quota"
    assert getattr(policy, "_retry_reason") == "policy"
    assert _parse_retry_after("Retry-After: nan") is None
    assert _parse_retry_after("Retry-After: -1") is None
    assert _parse_retry_after("Retry-After: 999999") == 5.0
    assert _parse_retry_after(
        "Retry-After: Thu, 01 Jan 1970 00:00:03 GMT", now=0.0,
    ) == 3.0
