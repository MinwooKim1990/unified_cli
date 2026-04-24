"""Unit tests for UsageTracker (stdlib only, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.usage import UsageTracker


def test_record_updates_aggregate():
    t = UsageTracker()
    t.record("claude", "haiku", input_tokens=100, output_tokens=50, latency_ms=300)
    t.record("claude", "haiku", input_tokens=200, output_tokens=10, latency_ms=500)
    agg = {a.provider: a for a in t.aggregates()}
    assert agg["claude"].calls == 2
    assert agg["claude"].input_tokens == 300
    assert agg["claude"].output_tokens == 60
    assert agg["claude"].avg_latency_ms == 400.0
    assert agg["claude"].model_calls == {"haiku": 2}


def test_record_across_providers():
    t = UsageTracker()
    t.record("claude", "haiku", input_tokens=10, output_tokens=5)
    t.record("codex", "gpt-5.4-mini", input_tokens=20, output_tokens=8)
    t.record("gemini", "gemini-3.1-flash-lite-preview", input_tokens=30, output_tokens=15)
    aggs = {a.provider: a for a in t.aggregates()}
    assert set(aggs) == {"claude", "codex", "gemini"}
    for a in aggs.values():
        assert a.calls == 1


def test_errors_tracked():
    t = UsageTracker()
    t.record("claude", "haiku", error_kind="auth_expired")
    t.record("claude", "haiku", input_tokens=10, output_tokens=5)
    agg = {a.provider: a for a in t.aggregates()}["claude"]
    assert agg.calls == 2
    assert agg.errors == 1


def test_recent_newest_first_and_bounded():
    t = UsageTracker(history_size=3)
    for i in range(5):
        t.record("claude", f"m{i}", input_tokens=i)
    recent = t.recent()
    # only last 3 kept (m2, m3, m4), newest first
    assert [r.model for r in recent] == ["m4", "m3", "m2"]


def test_snapshot_json_serializable():
    import json
    t = UsageTracker()
    t.record("claude", "haiku", input_tokens=10, output_tokens=5, latency_ms=100)
    snap = t.snapshot()
    # must be json-dumpable (dashboard fetches as JSON)
    payload = json.dumps(snap, default=str)
    assert "claude" in payload
    assert "haiku" in payload


def test_prompt_preview_truncated():
    t = UsageTracker()
    t.record("claude", "haiku", prompt_preview="a" * 200)
    rec = t.recent(1)[0]
    assert len(rec.prompt_preview) == 60


def test_reset():
    t = UsageTracker()
    t.record("claude", "haiku", input_tokens=100)
    assert t.aggregates()
    t.reset()
    assert t.aggregates() == []
    assert t.recent() == []


if __name__ == "__main__":  # manual run
    import traceback
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                print(f"  ✗ {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
