import dataclasses
import math
from collections.abc import Mapping

import pytest

from unified_cli_ext import (
    EventNormalizer,
    FinalTextEvent,
    PermissionDecision,
    PermissionPolicy,
    ProtocolError,
    SessionRef,
    TextDeduplicator,
    ToolProgressEvent,
    ToolStartEvent,
    freeze_json,
    validate_correlation_id,
    map_permission_decision,
)


def test_partial_final_dedup_and_block_reindex_restart():
    dedup = TextDeduplicator()
    assert dedup.partial("hel", "0") == "hel"
    assert dedup.partial("hello", "0") == "lo"
    assert dedup.partial("hello", "1") == ""
    assert dedup.partial("hello world", "1") == " world"
    assert dedup.final("hello world") == ("", "hello world")


def test_normalizer_emits_immutable_events_without_raw_reasoning():
    normalizer = EventNormalizer("fake")
    assert normalizer.feed({"type": "thinking", "text": "private"}) == []
    session = normalizer.feed({"type": "session", "session_id": "abc"})[0]
    assert session.session.namespaced == "fake:abc"
    start = normalizer.feed(
        {
            "type": "tool_start",
            "tool_id": "tool-1",
            "name": "read",
            "arguments": {"path": "/tmp/x", "chain_of_thought": "private"},
        }
    )[0]
    assert isinstance(start, ToolStartEvent)
    assert "chain_of_thought" not in start.arguments
    with pytest.raises(TypeError):
        start.arguments["path"] = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        start.name = "write"


@pytest.mark.parametrize(
    "key", ["rawReasoning", "raw-reasoning", "reasoningContent", "thinking_content"]
)
def test_reasoning_key_variants_are_removed_from_normalized_payloads(key):
    frozen = freeze_json({key: "private", "public_summary": "safe"})
    assert key not in frozen
    assert frozen["public_summary"] == "safe"


def test_final_event_separates_unseen_suffix_from_authoritative_text():
    normalizer = EventNormalizer("fake")
    normalizer.feed({"type": "text_partial", "block_id": "0", "text": "abc"})
    event = normalizer.feed({"type": "text_final", "text": "abcdef"})[0]
    assert event == FinalTextEvent(text="def", complete_text="abcdef")


def test_restart_final_reconstruction_is_internally_consistent():
    dedup = TextDeduplicator()
    dedup.partial("hello world")
    unseen, complete = dedup.final("world!")
    assert unseen == "!"
    assert complete == "hello world!"
    assert complete == dedup.complete_text


def test_short_unrelated_partial_after_block_reindex_is_not_lost():
    dedup = TextDeduplicator()
    assert dedup.partial("first answer", "0") == "first answer"
    # Reused provider block identifiers are not evidence that a shorter,
    # unrelated cumulative value is stale.
    assert dedup.partial("new", "0") == "new"
    assert dedup.complete_text == "first answernew"


def test_text_state_has_bounded_blocks_and_accepts_many_linear_partials():
    dedup = TextDeduplicator(max_text_bytes=2 * 1024 * 1024)
    text = ""
    for _ in range(2000):
        text += "x"
        dedup.partial(text)
    assert dedup.complete_text == text
    bounded = TextDeduplicator()
    for index in range(256):
        bounded.partial("x", str(index))
    with pytest.raises(Exception):
        bounded.partial("x", "overflow")


def test_tool_ids_fail_closed_for_malformed_duplicate_and_unmatched():
    normalizer = EventNormalizer("fake")
    with pytest.raises(ProtocolError):
        normalizer.feed({"type": "tool_result", "tool_id": "missing", "result": None})
    normalizer.feed({"type": "tool_start", "tool_id": "x", "name": "read"})
    with pytest.raises(ProtocolError):
        normalizer.feed({"type": "tool_start", "tool_id": "x", "name": "read"})
    with pytest.raises(ProtocolError):
        EventNormalizer("fake").feed({"type": "tool_start", "tool_id": "", "name": "read"})


def test_rejected_tool_events_do_not_mutate_correlation_state():
    normalizer = EventNormalizer("fake")
    with pytest.raises(ProtocolError):
        normalizer.feed(
            {
                "type": "tool_start",
                "tool_id": "retry-start",
                "name": "read",
                "arguments": [],
            }
        )
    normalizer.feed(
        {
            "type": "tool_start",
            "tool_id": "retry-start",
            "name": "read",
            "arguments": {},
        }
    )
    with pytest.raises(ProtocolError):
        normalizer.feed(
            {
                "type": "tool_result",
                "tool_id": "retry-start",
                "result": {"ok": True},
                "is_error": "no",
            }
        )
    event = normalizer.feed(
        {
            "type": "tool_result",
            "tool_id": "retry-start",
            "result": {"ok": True},
            "is_error": False,
        }
    )[0]
    assert event.tool_id == "retry-start"


def test_normalizer_canonicalizes_hostile_mapping_and_huge_numbers():
    class HostileGetMapping(Mapping):
        def __getitem__(self, key):
            return {"type": "done", "reason": "safe"}[key]

        def __iter__(self):
            return iter(("type", "reason"))

        def __len__(self):
            return 2

        def get(self, key, default=None):
            raise RuntimeError("hostile-get")

        def items(self):
            return iter((("type", "done"), ("reason", "safe")))

    assert EventNormalizer("fake").feed(HostileGetMapping())[0].reason == "safe"
    with pytest.raises(ProtocolError) as caught:
        EventNormalizer("fake").feed(
            {"type": "tool_progress", "tool_id": "missing", "progress": 10**10000}
        )
    assert caught.value.__cause__ is None


def test_sessions_are_provider_namespaced_and_cannot_change():
    assert SessionRef.parse("fake:opaque:part") == SessionRef("fake", "opaque:part")
    with pytest.raises(ProtocolError):
        SessionRef.parse("unscoped")
    normalizer = EventNormalizer("fake")
    normalizer.feed({"type": "session", "session_id": "one"})
    with pytest.raises(ProtocolError):
        normalizer.feed({"type": "session", "session_id": "two"})


@pytest.mark.parametrize("value", [None, True, "yes", "allow", "allow_always", {}, 1])
def test_unknown_or_unsafe_permission_values_deny(value):
    assert map_permission_decision(value) is PermissionDecision.DENY


def test_permission_policy_never_approves_without_explicit_allow_once():
    request = EventNormalizer("fake").feed(
        {"type": "permission", "request_id": "p1", "operation": "write"}
    )[0]
    assert PermissionPolicy().decide(request) is PermissionDecision.DENY
    assert PermissionPolicy(lambda _: "allow_once").decide(request) is PermissionDecision.ALLOW_ONCE
    assert PermissionPolicy(lambda _: (_ for _ in ()).throw(RuntimeError())).decide(request) is PermissionDecision.DENY


def test_unknown_event_and_usage_shape_fail_closed():
    normalizer = EventNormalizer("fake")
    with pytest.raises(ProtocolError):
        normalizer.feed({"type": "future-event", "private": "data"})
    with pytest.raises(ProtocolError):
        normalizer.feed({"type": "usage", "input_tokens": -1})


@pytest.mark.parametrize("bad", ["x\x00y", "x\x85y", "x\u2028y", "\ud800"])
def test_identifiers_and_strings_convert_unsafe_unicode_to_protocol_error(bad):
    with pytest.raises(ProtocolError):
        SessionRef("fake", bad)
    with pytest.raises(ProtocolError):
        TextDeduplicator().partial("safe", bad)
    with pytest.raises(ProtocolError):
        validate_correlation_id(bad)
    with pytest.raises(ProtocolError):
        EventNormalizer("fake").feed({"type": "text_delta", "text": bad})


@pytest.mark.parametrize("bad", ["safe\u202eevil", "safe\u2066evil", "safe\u200b"])
def test_format_controls_are_rejected_from_identifiers(bad):
    with pytest.raises(ProtocolError):
        SessionRef("fake", bad)
    with pytest.raises(ProtocolError):
        validate_correlation_id(bad)


def test_ordinary_unicode_and_emoji_sequences_remain_valid_text():
    assert SessionRef("fake", "세션😀").session_id == "세션😀"
    text = "가족 👨‍👩‍👧‍👦"
    event = EventNormalizer("fake").feed({"type": "text_delta", "text": text})[0]
    assert event.text == text


def test_direct_tool_progress_rejects_huge_integer_cause_free():
    with pytest.raises(ProtocolError, match="between zero and one") as caught:
        ToolProgressEvent("tool-1", "working", 10**10000)
    assert caught.value.__cause__ is None


class DuplicateMapping(Mapping):
    def __getitem__(self, key):
        return 1

    def __iter__(self):
        return iter(["x"])

    def __len__(self):
        return 1

    def items(self):
        return iter([("x", 1), ("x", 2)])


class EndlessMapping(DuplicateMapping):
    def items(self):
        while True:
            yield ("x{}".format(id(object())), 1)


@pytest.mark.parametrize("value", [math.nan, math.inf, 2**80, [0] * 10_001])
def test_freeze_json_rejects_nonfinite_or_oversized_values(value):
    with pytest.raises(ProtocolError):
        freeze_json(value)


def test_freeze_json_rejects_duplicate_and_unbounded_mapping_iteration():
    with pytest.raises(ProtocolError):
        freeze_json(DuplicateMapping())
    with pytest.raises(ProtocolError):
        freeze_json(EndlessMapping())


def test_hostile_permission_equality_cannot_grant():
    class Hostile:
        def __eq__(self, other):
            return True

    assert map_permission_decision(Hostile()) is PermissionDecision.DENY
