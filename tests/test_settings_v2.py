"""Settings v2 migration, validation and filesystem-security tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import settings as st  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    directory = tmp_path / ".unified-cli"
    monkeypatch.setattr(st, "SETTINGS_DIR", directory)
    monkeypatch.setattr(st, "SETTINGS_FILE", directory / "settings.json")


def _write(value) -> None:
    st.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    st.SETTINGS_FILE.write_text(json.dumps(value), encoding="utf-8")


def test_v2_defaults_are_typed_and_safe():
    value = st.Settings()
    assert value.reasoning_display == "hidden"
    assert value.tool_display == "compact"
    assert value.theme == "auto"
    assert value.cross_provider_context_enabled is True
    assert value.context_window == 8
    assert value.repl_permission == "provider_default"
    assert value.browser_permission == "read_only"
    assert value.browser_prompt_preview is False
    assert value.additional_dirs == []
    assert value.multiline is True
    assert value.provider_settings == {}


def test_v1_migrates_in_memory_then_writes_v2_on_explicit_save():
    _write({"version": 1, "settings": {"lang": "ko", "default_provider": "codex"}})
    original = st.SETTINGS_FILE.read_bytes()
    value = st.load_settings()
    assert (value.lang, value.default_provider) == ("ko", "codex")
    assert st.SETTINGS_FILE.read_bytes() == original
    st.save_settings(value)
    migrated = json.loads(st.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert migrated["version"] == 2
    assert migrated["settings"]["theme"] == "auto"
    assert migrated["settings"]["context_window"] == 8


def test_v1_read_does_not_attempt_a_migration_write(monkeypatch):
    _write({"version": 1, "settings": {"lang": "en", "default_provider": "gemini"}})

    def forbidden_write(_settings):
        raise AssertionError("load attempted to write")

    monkeypatch.setattr(st, "_save_unlocked", forbidden_write)
    loaded = st.load_settings()
    assert (loaded.lang, loaded.default_provider) == ("en", "gemini")


@pytest.mark.skipif(os.name != "posix", reason="POSIX read-only mode contract")
def test_private_read_only_v1_remains_readable_when_mode_repair_fails(monkeypatch):
    _write({"version": 1, "settings": {"lang": "ko", "default_provider": "claude"}})
    real_chmod = os.chmod
    real_chmod(st.SETTINGS_FILE, 0o400)
    real_chmod(st.SETTINGS_DIR, 0o500)

    def denied(*_args, **_kwargs):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(st.os, "chmod", denied)
    monkeypatch.setattr(st.os, "fchmod", denied)
    try:
        loaded = st.load_settings()
        assert (loaded.lang, loaded.default_provider) == ("ko", "claude")
    finally:
        real_chmod(st.SETTINGS_DIR, 0o700)
        real_chmod(st.SETTINGS_FILE, 0o600)


def test_future_malformed_and_oversized_files_fail_closed():
    _write({"version": 999, "settings": {"theme": "dark"}})
    assert st.load_settings() == st.Settings()
    st.SETTINGS_FILE.write_text("{bad", encoding="utf-8")
    assert st.load_settings() == st.Settings()
    st.SETTINGS_FILE.write_bytes(b" " * (st._MAX_FILE_BYTES + 1))
    assert st.load_settings() == st.Settings()
    _write({"version": True, "settings": {"theme": "dark"}})
    assert st.load_settings() == st.Settings()


def test_untrusted_fields_are_normalized_independently():
    _write({
        "version": 2,
        "settings": {
            "lang": ["ko"],
            "theme": "neon",
            "context_window": -1,
            "browser_permission": "full",
            "repl_permission": "full",
            "cross_provider_context_enabled": "yes",
            "provider_settings": {
                "valid-ext": {"format": "wide"},
                "../../bad": {"format": "stolen"},
                "other-ext": {"api_key": "do-not-load"},
            },
        },
    })
    value = st.load_settings()
    assert value.lang is None
    assert value.theme == "auto"
    assert value.context_window == 8
    assert value.browser_permission == "read_only"
    assert value.repl_permission == "provider_default"
    assert value.cross_provider_context_enabled is True
    assert value.provider_settings == {"valid-ext": {"format": "wide"}}


@pytest.mark.parametrize("key", ["repl_permission", "browser_permission"])
def test_full_permission_is_rejected_and_never_persisted(key):
    st.save_settings(st.Settings(theme="dark"))
    with pytest.raises(ValueError):
        st.set(key, "full")
    body = st.SETTINGS_FILE.read_text(encoding="utf-8")
    assert '"repl_permission": "full"' not in body
    assert '"browser_permission": "full"' not in body
    assert st.load_settings().theme == "dark"


def test_durable_fields_roundtrip(tmp_path):
    workspace = str(tmp_path.resolve())
    value = st.Settings(
        style="terse", effort="high", reasoning_mode="summary",
        system_prompt="Be precise.", timeout=12.5, tools=True, mcp=False,
        web=True, workspace=workspace, additional_dirs=[workspace], multiline=True,
    )
    st.save_settings(value)
    loaded = st.load_settings()
    assert loaded.style == "terse"
    assert loaded.effort == "high"
    assert loaded.reasoning_mode == "summary"
    assert loaded.system_prompt == "Be precise."
    assert loaded.timeout == 12.5
    assert (loaded.tools, loaded.mcp, loaded.web) == (True, False, True)
    assert loaded.workspace == workspace
    assert loaded.additional_dirs == [workspace]
    assert loaded.multiline is True


def test_text_preferences_reject_terminal_controls_but_system_allows_newlines():
    for key in ("style", "effort", "reasoning_mode"):
        with pytest.raises(ValueError):
            st.set(key, "high\nspoofed")
        with pytest.raises(ValueError):
            st.set(key, "safe\N{RIGHT-TO-LEFT OVERRIDE}spoofed")

    st.set("system_prompt", "First line\nSecond line\tindented")
    assert st.load_settings().system_prompt == "First line\nSecond line\tindented"
    with pytest.raises(ValueError):
        st.set("system_prompt", "unsafe\N{RIGHT-TO-LEFT OVERRIDE}text")


def test_provider_namespaces_are_isolated_and_return_copies():
    st.set_provider_setting("acme-one", "endpoint", {"region": "east"})
    st.set_provider_setting("acme-two", "endpoint", {"region": "west"})
    one = st.get_provider_settings("acme-one")
    one["endpoint"]["region"] = "mutated"
    assert st.get_provider_settings("acme-one") == {"endpoint": {"region": "east"}}
    assert st.get_provider_settings("acme-two") == {"endpoint": {"region": "west"}}
    assert st.clear_provider_settings("acme-one") is True
    assert st.get_provider_settings("acme-one") == {}
    assert st.get_provider_settings("acme-two")["endpoint"]["region"] == "west"


@pytest.mark.parametrize("provider", ["../bad", "UPPER", "", "a" * 65])
def test_provider_namespace_rejects_unsafe_ids(provider):
    with pytest.raises(ValueError):
        st.set_provider_setting(provider, "format", True)


@pytest.mark.parametrize(
    "key,value",
    [
        ("api_key", "secret"),
        ("authorizationHeader", "secret"),
        ("format", object()),
        ("format", float("nan")),
        ("format", {"nested": {"password": "secret"}}),
    ],
)
def test_provider_namespace_rejects_credentials_and_non_json(key, value):
    with pytest.raises(ValueError):
        st.set_provider_setting("safe-ext", key, value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_existing_directory_and_file_modes_are_repaired():
    st.SETTINGS_DIR.mkdir(mode=0o777)
    st.SETTINGS_FILE.write_text(
        json.dumps({"version": 2, "settings": {}}), encoding="utf-8",
    )
    os.chmod(st.SETTINGS_DIR, 0o755)
    os.chmod(st.SETTINGS_FILE, 0o644)
    st.load_settings()
    assert stat.S_IMODE(st.SETTINGS_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(st.SETTINGS_FILE.stat().st_mode) == 0o600
    st.save_settings(st.Settings())
    assert stat.S_IMODE(st.SETTINGS_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(st.SETTINGS_FILE.stat().st_mode) == 0o600
    assert stat.S_IMODE((st.SETTINGS_DIR / "settings.lock").stat().st_mode) == 0o600


def test_symlink_file_is_not_followed(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel": true}', encoding="utf-8")
    st.SETTINGS_DIR.mkdir()
    try:
        st.SETTINGS_FILE.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert st.load_settings() == st.Settings()
    st.save_settings(st.Settings(theme="dark"))
    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert not st.SETTINGS_FILE.is_symlink()


def test_symlink_directory_is_rejected(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(st, "SETTINGS_DIR", linked)
    monkeypatch.setattr(st, "SETTINGS_FILE", linked / "settings.json")
    assert st.load_settings() == st.Settings()
    with pytest.raises(OSError):
        st.save_settings(st.Settings())
    assert list(outside.iterdir()) == []


def test_atomic_replace_failure_preserves_old_file_and_cleans_temp(monkeypatch):
    st.save_settings(st.Settings(theme="light"))
    original = st.SETTINGS_FILE.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(st.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        st.save_settings(st.Settings(theme="dark"))
    assert st.SETTINGS_FILE.read_bytes() == original
    assert not any(path.name.startswith(".settings.") for path in st.SETTINGS_DIR.iterdir())


def test_unsafe_temp_target_is_rejected_without_touching_it(tmp_path, monkeypatch):
    outside = tmp_path / "outside.tmp"
    captured = []

    def unsafe_mkstemp(*_args, **_kwargs):
        fd = os.open(outside, os.O_RDWR | os.O_CREAT, 0o600)
        captured.append(fd)
        return fd, str(outside)

    monkeypatch.setattr(st.tempfile, "mkstemp", unsafe_mkstemp)
    with pytest.raises(OSError, match="settings directory"):
        st.save_settings(st.Settings())
    assert outside.exists()
    assert outside.read_bytes() == b""
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_v1_unknown_fields_are_not_promoted_by_mutations():
    st.SETTINGS_DIR.mkdir()
    st.SETTINGS_FILE.write_text(json.dumps({
        "version": 1,
        "settings": {
            "lang": "ko",
            "default_provider": "codex",
            "theme": "dark",
            "browser_prompt_preview": True,
            "provider_settings": {"evil": {"endpoint": "unexpected"}},
        },
    }), encoding="utf-8")

    st.set("context_window", 16)

    loaded = st.load_settings()
    assert loaded.lang == "ko"
    assert loaded.default_provider == "codex"
    assert loaded.context_window == 16
    assert loaded.theme == "auto"
    assert loaded.browser_prompt_preview is False
    assert loaded.provider_settings == {}


def test_concurrent_set_operations_do_not_lose_distinct_fields():
    barrier = threading.Barrier(4)
    errors = []

    def update(key, value):
        try:
            barrier.wait()
            st.set(key, value)
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [
        threading.Thread(target=update, args=("theme", "dark")),
        threading.Thread(target=update, args=("context_window", 16)),
        threading.Thread(target=update, args=("multiline", True)),
        threading.Thread(target=update, args=("browser_prompt_preview", True)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    value = st.load_settings()
    assert (value.theme, value.context_window, value.multiline) == ("dark", 16, True)
    assert value.browser_prompt_preview is True
