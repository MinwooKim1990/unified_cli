"""Tests for the slash/model completion helpers (terminal-free)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import models
from unified_cli import repl_completion as rc


@pytest.fixture(autouse=True)
def _gemini_locked(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    yield


def test_slash_candidates_prefix():
    names = [n for n, _ in rc.slash_candidates("/mo")]
    assert names == ["/model"]


def test_slash_candidates_all():
    names = [n for n, _ in rc.slash_candidates("/")]
    assert "/help" in names and "/status" in names and "/lang" in names
    assert len(names) == len(rc.SLASH_COMMANDS)


def test_slash_candidates_have_descriptions():
    cands = rc.slash_candidates("/")
    assert all(desc for _, desc in cands)  # every command has a non-empty meta


def test_arg_candidates_provider():
    vals = [v for v, _ in rc.arg_candidates("/provider", "claude", "")]
    assert vals == ["claude", "codex", "gemini"]


def test_arg_candidates_provider_gemini_locked():
    metas = dict(rc.arg_candidates("/provider", "claude", ""))
    assert "locked" in metas["gemini"].lower()
    assert metas["claude"] == ""


def test_arg_candidates_model_marks_default():
    cands = rc.arg_candidates("/model", "codex", "")
    ids = [c for c, _ in cands]
    assert models.DEFAULT_MODELS["codex"] in ids
    default_meta = dict(cands)[models.DEFAULT_MODELS["codex"]]
    assert "★" in default_meta


def test_arg_candidates_model_prefix_filter():
    cands = rc.arg_candidates("/model", "claude", "claude-")
    assert cands  # at least one
    assert all(cid.startswith("claude-") for cid, _ in cands)


def test_arg_candidates_model_cache_cold_is_instant(monkeypatch):
    # Simulate a cold TTL cache: candidates must still come back (from hardcoded),
    # never hitting the network/subprocess.
    monkeypatch.setattr(models, "_CACHE", {})
    cands = rc.arg_candidates("/model", "claude", "")
    assert cands


def test_arg_candidates_gemini_locked_meta():
    cands = rc.arg_candidates("/model", "gemini", "")
    assert cands
    assert all("(locked)" in meta for _, meta in cands)


def test_arg_candidates_lang():
    vals = [v for v, _ in rc.arg_candidates("/lang", "claude", "")]
    assert vals == ["en", "ko"]


def test_arg_candidates_auth_is_explicit_provider_second():
    actions = [v for v, _ in rc.arg_candidates("/auth", "claude", "", "")]
    assert actions == ["status", "login", "logout"]
    providers = [
        v for v, _ in rc.arg_candidates("/auth", "claude", "co", "status co")
    ]
    assert providers == ["codex"]


def test_model_refresh_completion_is_explicit_only():
    assert rc.arg_candidates("/model", "claude", "--") == [
        ("--refresh", rc.t("repl.model.refresh_meta"))
    ]


def test_capability_completions_are_canonical_and_complete():
    permissions = [
        value for value, _ in rc.arg_candidates("/permissions", "claude", "")
    ]
    assert permissions == ["provider_default", "read_only", "workspace_write"]
    assert "full" not in permissions
    assert [value for value, _ in rc.arg_candidates("/effort", "claude", "")] == [
        "default", "low", "medium", "high", "xhigh", "max",
    ]
    assert [value for value, _ in rc.arg_candidates("/web", "claude", "")] == [
        "default", "on", "off",
    ]


def test_toolbar_distinguishes_provider_managed_web():
    toolbar = rc._bottom_toolbar({
        "provider": "claude", "model": "haiku", "cwd": "/tmp",
        "permission_mode": "provider_default", "web_search": True,
        "web_explicit": False,
    })
    assert "web:default" in toolbar


def test_has_prompt_toolkit_returns_bool():
    assert isinstance(rc.has_prompt_toolkit(), bool)


def test_cached_or_hardcoded_never_empty():
    monkeypatch_cache = {}
    models._CACHE.clear()
    for p in ("claude", "codex", "gemini"):
        assert rc.cached_or_hardcoded(p)
