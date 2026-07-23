"""Security and behavior tests for the bounded multi-session index."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import session_manager as sm  # noqa: E402


@pytest.fixture
def manager(tmp_path):
    return sm.SessionManager(tmp_path / ".unified-cli" / "sessions.json")


def test_lifecycle_and_provider_namespaces(manager, tmp_path):
    cwd = str(tmp_path.resolve())
    one = manager.upsert(provider="claude", session_id="same-id", model="opus", cwd=cwd)
    two = manager.upsert(provider="codex", session_id="same-id", model="gpt", cwd=cwd)
    assert one.provider == "claude" and two.provider == "codex"
    assert manager.get(provider="claude", session_id="same-id").model == "opus"
    assert manager.get(provider="codex", session_id="same-id").model == "gpt"
    renamed = manager.rename(provider="claude", session_id="same-id", name="Primary")
    assert renamed.name == "Primary"
    assert manager.archive(provider="claude", session_id="same-id").archived is True
    assert [item.provider for item in manager.list()] == ["codex"]
    assert len(manager.list(include_archived=True)) == 2
    assert manager.delete(provider="claude", session_id="same-id") is True
    assert manager.delete(provider="claude", session_id="same-id") is False
    assert manager.clear(provider="codex") == 1
    assert manager.clear() == 0


def test_list_is_newest_first_deterministic_and_detached(manager, tmp_path):
    cwd = str(tmp_path.resolve())
    manager.upsert(provider="codex", session_id="b", cwd=cwd, updated_at=2)
    manager.upsert(provider="claude", session_id="a", cwd=cwd, updated_at=2)
    manager.upsert(provider="gemini", session_id="c", cwd=cwd, updated_at=1)
    records = manager.list()
    assert [(item.provider, item.session_id) for item in records] == [
        ("claude", "a"), ("codex", "b"), ("gemini", "c"),
    ]
    records[0].metadata["changed"] = True
    assert manager.get(provider="claude", session_id="a").metadata == {}


def test_fork_records_source_and_never_overwrites(manager, tmp_path):
    cwd = str(tmp_path.resolve())
    manager.upsert(
        provider="claude", session_id="source", model="opus", cwd=cwd,
        metadata={"branch": "main"},
    )
    forked = manager.record_fork(
        source_provider="claude", source_session_id="source",
        provider="codex", session_id="fork-1", model="gpt",
    )
    assert forked.forked_from == {"provider": "claude", "session_id": "source"}
    assert forked.metadata == {"branch": "main"}
    with pytest.raises(ValueError, match="already exists"):
        manager.fork(
            source_provider="claude", source_session_id="source",
            provider="codex", session_id="fork-1",
        )
    assert manager.get(provider="codex", session_id="fork-1").model == "gpt"
    with pytest.raises(KeyError):
        manager.fork(
            source_provider="claude", source_session_id="missing",
            provider="codex", session_id="new",
        )


def test_bounded_records_prune_archived_first(tmp_path):
    manager = sm.SessionManager(tmp_path / "sessions.json", max_records=2)
    cwd = str(tmp_path.resolve())
    manager.upsert(provider="claude", session_id="old", cwd=cwd, updated_at=1)
    manager.archive(provider="claude", session_id="old")
    manager.upsert(provider="claude", session_id="keep", cwd=cwd, updated_at=2)
    manager.upsert(provider="claude", session_id="new", cwd=cwd, updated_at=3)
    assert {item.session_id for item in manager.list(include_archived=True)} == {"keep", "new"}


@pytest.mark.parametrize("provider", ["../bad", "UPPER", "", "x" * 65])
def test_unsafe_provider_ids_are_rejected(manager, provider):
    with pytest.raises(ValueError):
        manager.upsert(provider=provider, session_id="safe")


@pytest.mark.parametrize("session_id", ["", "line\nbreak", "nul\x00byte", "x" * 513, "\ud800"])
def test_unsafe_session_ids_are_rejected(manager, session_id):
    with pytest.raises(ValueError):
        manager.upsert(provider="claude", session_id=session_id)


def test_session_ids_are_opaque_not_paths(manager, tmp_path):
    opaque = "../provider/session name:한글"
    manager.upsert(
        provider="claude", session_id=opaque, cwd=str(tmp_path.resolve()),
    )
    assert manager.get(provider="claude", session_id=opaque).session_id == opaque


def test_cwd_must_be_absolute_and_normalized(manager):
    with pytest.raises(ValueError):
        manager.upsert(provider="claude", session_id="one", cwd="relative/path")
    with pytest.raises(ValueError):
        manager.upsert(provider="claude", session_id="one", cwd="/safe/../escape")


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "secret"},
        {"authorizationHeader": "secret"},
        {"nested": {"access-token": "secret"}},
        {"value": object()},
        {"value": float("inf")},
    ],
)
def test_metadata_rejects_credentials_and_untrusted_json(manager, metadata):
    with pytest.raises(ValueError):
        manager.upsert(provider="claude", session_id="one", metadata=metadata)
    assert manager.list(include_archived=True) == []


def test_corrupt_oversized_and_future_files_fail_closed(tmp_path):
    path = tmp_path / "sessions.json"
    manager = sm.SessionManager(path)
    path.write_text("{bad", encoding="utf-8")
    assert manager.list() == []
    path.write_bytes(b" " * (sm._MAX_FILE_BYTES + 1))
    assert manager.list() == []
    path.write_text(json.dumps({"version": 99, "providers": {}}), encoding="utf-8")
    assert manager.list() == []
    path.write_text(json.dumps({"version": True, "providers": {}}), encoding="utf-8")
    assert manager.list() == []


def test_untrusted_namespaces_do_not_cross_or_expose_credentials(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "version": 1,
        "providers": {
            "claude": [
                {
                    "provider": "codex", "session_id": "crossed", "model": "x",
                    "cwd": "", "created_at": 1, "updated_at": 1,
                },
                {
                    "provider": "claude", "session_id": "credential", "model": "x",
                    "cwd": "", "created_at": 1, "updated_at": 1,
                    "metadata": {"api_key": "must-not-appear"},
                },
                {
                    "provider": "claude", "session_id": "valid", "model": "x",
                    "cwd": "", "created_at": 1, "updated_at": 1,
                    "metadata": {"label": "safe"}, "raw_access_token": "ignored",
                },
            ],
        },
    }), encoding="utf-8")
    records = sm.SessionManager(path).list(include_archived=True)
    assert [item.session_id for item in records] == ["valid"]
    assert "must-not-appear" not in repr(records)
    assert "raw_access_token" not in repr(records)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_directory_file_and_lock_modes_are_private(manager, tmp_path):
    manager.upsert(provider="claude", session_id="one", cwd=str(tmp_path.resolve()))
    assert stat.S_IMODE(manager.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((manager.directory / "sessions.lock").stat().st_mode) == 0o600
    os.chmod(manager.directory, 0o755)
    os.chmod(manager.path, 0o644)
    manager.list()
    assert stat.S_IMODE(manager.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600


def test_symlink_file_is_not_followed(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    directory = tmp_path / "sessions"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    path = directory / "sessions.json"
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    manager = sm.SessionManager(path)
    assert manager.list() == []
    manager.upsert(provider="claude", session_id="one", cwd=str(tmp_path.resolve()))
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not path.is_symlink()


def test_atomic_replace_failure_preserves_old_file_and_cleans_temp(manager, tmp_path, monkeypatch):
    manager.upsert(provider="claude", session_id="one", cwd=str(tmp_path.resolve()))
    original = manager.path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(sm.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        manager.upsert(provider="codex", session_id="two", cwd=str(tmp_path.resolve()))
    assert manager.path.read_bytes() == original
    assert not any(path.name.startswith(".sessions.") for path in manager.directory.iterdir())


def test_unsafe_temp_target_is_rejected_without_touching_it(manager, tmp_path, monkeypatch):
    outside = tmp_path / "outside.tmp"

    def unsafe_mkstemp(*_args, **_kwargs):
        fd = os.open(outside, os.O_RDWR | os.O_CREAT, 0o600)
        return fd, str(outside)

    monkeypatch.setattr(sm.tempfile, "mkstemp", unsafe_mkstemp)
    with pytest.raises(OSError, match="sessions directory"):
        manager.upsert(provider="claude", session_id="one", cwd=str(tmp_path.resolve()))
    assert outside.exists()
    assert outside.read_bytes() == b""


def test_concurrent_upserts_produce_valid_complete_index(manager, tmp_path):
    cwd = str(tmp_path.resolve())
    barrier = threading.Barrier(12)
    errors = []

    def add(index):
        try:
            barrier.wait()
            manager.upsert(provider="claude", session_id=f"session-{index}", cwd=cwd)
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    assert {record.session_id for record in manager.list()} == {
        f"session-{index}" for index in range(12)
    }
