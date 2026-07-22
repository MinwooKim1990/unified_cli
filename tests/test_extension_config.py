"""Typed extension launch context, persistence, and routing tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from unified_cli import (
    BoundProviderOperationsV1,
    ExtensionLaunchOverridesV1,
    ModelInfo,
    PROVIDER_CONFIGURATION_ABI_V1,
    ProviderCreateRequestV1,
    ProviderLaunchContextV1,
    ProviderPluginV1,
    ProviderReceiptEnvelopeV1,
    UnifiedConversation,
    UnifiedError,
    bind_extension_provider,
    clear_extension_provider_configuration,
    configure_extension_provider,
    create,
    doctor_provider,
    list_models,
)
from unified_cli import extension_config, registry, settings
from unified_cli.base import BaseProvider
from unified_cli.registry import _reset_provider_registry_for_tests


class _DummyProvider(BaseProvider):
    name = "acme"
    default_model = "acme-default"
    api_key_env = ""

    def __init__(self, model, request):
        self.model = model
        self.request = request

    @classmethod
    def _discover_bin(cls):
        return None

    @classmethod
    def _install_hint(cls):
        return ""

    def _build_args(self, *args, **kwargs):
        return []

    def _normalize(self, obj):
        return iter(())

    def _parse_json_response(self, raw, model):
        raise NotImplementedError


class _FakeEntryPoint:
    group = registry.ENTRY_POINT_GROUP

    def __init__(self, provider_id, plugin):
        self.name = provider_id
        self.plugin = plugin
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self.plugin


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    root = tmp_path / "settings"
    monkeypatch.setattr(settings, "SETTINGS_DIR", root)
    monkeypatch.setattr(settings, "SETTINGS_FILE", root / "settings.json")
    monkeypatch.delenv(registry.DISABLE_PLUGINS_ENV, raising=False)
    _reset_provider_registry_for_tests()
    yield
    _reset_provider_registry_for_tests()


def _receipt(provider_id="acme", marker="receipt-marker"):
    return ProviderReceiptEnvelopeV1(
        provider_id=provider_id,
        media_type="application/vnd.acme.receipt+json",
        payload={"schema": 1, "marker": marker, "items": [1, 2]},
    )


def _set_plugin(monkeypatch, plugin):
    entry_point = _FakeEntryPoint(plugin.id, plugin)
    monkeypatch.setattr(
        registry.importlib_metadata,
        "entry_points",
        lambda: [entry_point],
    )
    return entry_point


def _configured_plugin(calls, *, available=True, binder_error=None):
    fallback = _receipt(marker="resolver-receipt")

    def legacy_factory(*, model=None, **opts):
        raise AssertionError("configured plugin used its legacy factory")

    def binder(context):
        if binder_error is not None:
            raise RuntimeError(binder_error)
        calls.append(("bind", context))
        normalized = context.receipt or fallback

        def factory(request):
            calls.append(("factory", context, request))
            return _DummyProvider(request.model, request)

        def models():
            calls.append(("models", context))
            return (
                ModelInfo(
                    id="acme-default",
                    provider="acme",
                    default=True,
                    source="plugin",
                ),
            )

        def doctor():
            calls.append(("doctor", context))
            return {"available": available}

        return BoundProviderOperationsV1(
            provider_id="acme",
            factory=factory,
            model_lister=models,
            doctor=doctor,
            normalized_receipt=normalized,
            provider_home=context.provider_home,
        )

    return ProviderPluginV1(
        id="acme",
        factory=legacy_factory,
        default_model="acme-default",
        model_lister=lambda: (),
        doctor=lambda: {"available": False},
        route_prefixes=("acme",),
        support_status="preview",
        configuration_abi_version=PROVIDER_CONFIGURATION_ABI_V1,
        launch_binder=binder,
        environment_keys=frozenset(("SAFE_ENV",)),
    )


def _receipt_files(provider_id="acme"):
    directory = settings.SETTINGS_DIR / "providers" / provider_id
    return list(directory.glob("receipt-v1-*.json")) if directory.exists() else []


def test_configuration_abi_is_additive_and_core_types_copy_inputs(tmp_path):
    legacy = ProviderPluginV1(
        "legacy",
        lambda **opts: _DummyProvider("legacy", None),
        "legacy-model",
        lambda: (),
        lambda: {},
    )
    assert legacy.configuration_abi_version is None
    assert legacy.launch_binder is None
    assert legacy.environment_keys == frozenset()

    values = {"SAFE_ENV": "one"}
    context = ProviderLaunchContextV1(
        provider_id="acme",
        receipt=_receipt(),
        provider_home=str(tmp_path),
        provider_env=values,
    )
    values["SAFE_ENV"] = "changed"
    assert dict(context.provider_env) == {"SAFE_ENV": "one"}
    with pytest.raises(TypeError):
        context.provider_env["SAFE_ENV"] = "two"

    with pytest.raises(ValueError):
        ProviderPluginV1(
            "broken",
            lambda **opts: _DummyProvider("broken", None),
            "broken-model",
            lambda: (),
            lambda: {},
            launch_binder=lambda context: None,
        )
    with pytest.raises(TypeError):
        ProviderReceiptEnvelopeV1(
            provider_id="acme",
            media_type="application/vnd.acme.receipt+json",
            payload=[],
        )
    with pytest.raises(ValueError):
        ProviderPluginV1(
            "broken",
            lambda **opts: _DummyProvider("broken", None),
            "broken-model",
            lambda: (),
            lambda: {},
            configuration_abi_version=True,
            launch_binder=lambda context: None,
        )
    with pytest.raises(TypeError):
        ProviderPluginV1(
            "broken",
            lambda **opts: _DummyProvider("broken", None),
            "broken-model",
            lambda: (),
            lambda: {},
            configuration_abi_version=PROVIDER_CONFIGURATION_ABI_V1,
            launch_binder=lambda context: None,
            environment_keys="PATH",
        )
    with pytest.raises(ValueError):
        ProviderLaunchContextV1(
            provider_id="acme",
            receipt=_receipt(),
            bin_path=str(tmp_path / "binary"),
        )


def test_overrides_are_immutable_bounded_and_never_accept_relative_paths(tmp_path):
    supplied = {"SAFE_ENV": "value"}
    launch = ExtensionLaunchOverridesV1(
        receipt=_receipt(),
        provider_home=str(tmp_path),
        extra_env=supplied,
    )
    supplied["SAFE_ENV"] = "changed"
    assert dict(launch.extra_env) == {"SAFE_ENV": "value"}
    with pytest.raises(ValueError):
        ExtensionLaunchOverridesV1(bin_path="relative/provider")
    with pytest.raises(ValueError):
        ExtensionLaunchOverridesV1(extra_env={"SAFE_ENV": "line\nbreak"})
    with pytest.raises(ValueError):
        ExtensionLaunchOverridesV1(extra_env={"SAFE_ENV": "\ud800"})
    with pytest.raises(ValueError):
        ExtensionLaunchOverridesV1(
            receipt=_receipt(), bin_path=str(tmp_path / "provider"),
        )


def test_store_is_content_addressed_private_and_clear_preserves_other_settings(tmp_path):
    settings.set_provider_setting("acme", "format", "wide")
    home = tmp_path / "provider-home"
    receipt = _receipt(marker="not-in-settings")

    saved = extension_config.save_extension_launch(
        "acme", receipt, provider_home=str(home),
    )
    again = extension_config.save_extension_launch(
        "acme", receipt, provider_home=str(home),
    )
    loaded = extension_config.load_extension_launch("acme")

    assert loaded is not None
    assert loaded.receipt == receipt
    assert loaded.receipt_sha256 == saved.receipt_sha256 == again.receipt_sha256
    assert loaded.provider_home == str(home)
    assert len(_receipt_files()) == 1
    receipt_file = _receipt_files()[0]
    assert stat.S_IMODE(receipt_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_file.parent.stat().st_mode) == 0o700
    settings_text = settings.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "not-in-settings" not in settings_text
    assert "unified_cli_launch" in settings_text

    home.mkdir(mode=0o700)
    assert clear_extension_provider_configuration("acme") is True
    assert settings.get_provider_settings("acme") == {"format": "wide"}
    assert home.is_dir()
    # Clear is pointer-only: portable POSIX cannot identity-bind a pathname
    # unlink against a same-user replacement race.
    assert receipt_file.exists()
    assert clear_extension_provider_configuration("acme") is False


def test_receipt_publish_never_overwrites_a_late_injected_target(
    monkeypatch, tmp_path
):
    original_link = extension_config.os.link
    injected = []

    def inject_before_link(source, target):
        target_path = Path(target)
        target_path.write_bytes(b"do-not-overwrite")
        target_path.chmod(0o600)
        injected.append(target_path)
        return original_link(source, target)

    monkeypatch.setattr(extension_config.os, "link", inject_before_link)
    with pytest.raises(OSError, match="does not match"):
        extension_config.save_extension_launch(
            "acme", _receipt(), provider_home=str(tmp_path / "home"),
        )

    assert len(injected) == 1
    assert injected[0].read_bytes() == b"do-not-overwrite"
    assert settings.get_extension_launch_settings("acme") is None


def test_clear_unpublishes_pointer_without_pathname_unlink(monkeypatch, tmp_path):
    extension_config.save_extension_launch(
        "acme", _receipt(), provider_home=str(tmp_path / "home"),
    )
    blob = _receipt_files()[0]
    original_unlink = extension_config.os.unlink

    def reject_blob_unlink(path):
        if str(path) == str(blob):
            raise AssertionError("clear attempted pathname-based blob deletion")
        return original_unlink(path)

    monkeypatch.setattr(extension_config.os, "unlink", reject_blob_unlink)
    assert extension_config.clear_extension_launch("acme") is True
    assert blob.read_bytes()
    assert settings.get_extension_launch_settings("acme") is None


def test_store_rejects_tamper_symlink_hardlink_and_duplicate_json(tmp_path):
    stored = extension_config.save_extension_launch(
        "acme", _receipt(), provider_home=str(tmp_path / "home"),
    )
    receipt_file = _receipt_files()[0]
    receipt_file.write_bytes(receipt_file.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest"):
        extension_config.load_extension_launch("acme")

    settings.clear_extension_launch_settings("acme")
    receipt_file.unlink()
    victim = tmp_path / "victim"
    victim.write_text("victim", encoding="utf-8")
    receipt_file.symlink_to(victim)
    settings.set_extension_launch_settings(
        settings.ExtensionLaunchSettingsV1(
            provider_id="acme",
            receipt_sha256=stored.receipt_sha256,
            provider_home=None,
        )
    )
    with pytest.raises(OSError):
        extension_config.load_extension_launch("acme")

    settings.clear_extension_launch_settings("acme")
    receipt_file.unlink()
    linked = tmp_path / "linked"
    linked.write_text("not-a-receipt", encoding="utf-8")
    os.link(linked, receipt_file)
    settings.set_extension_launch_settings(
        settings.ExtensionLaunchSettingsV1(
            provider_id="acme",
            receipt_sha256=stored.receipt_sha256,
            provider_home=None,
        )
    )
    with pytest.raises(OSError):
        extension_config.load_extension_launch("acme")

    settings.clear_extension_launch_settings("acme")
    receipt_file.unlink()
    duplicate = (
        b'{"schema":1,"provider_id":"acme",'
        b'"media_type":"application/vnd.acme.receipt+json",'
        b'"payload":{"schema":1,"schema":1}}'
    )
    digest = hashlib.sha256(duplicate).hexdigest()
    duplicate_file = receipt_file.parent / "receipt-v1-{}.json".format(digest)
    duplicate_file.write_bytes(duplicate)
    duplicate_file.chmod(0o600)
    settings.set_extension_launch_settings(
        settings.ExtensionLaunchSettingsV1(
            provider_id="acme", receipt_sha256=digest, provider_home=None,
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        extension_config.load_extension_launch("acme")


def test_store_rejects_symlinked_provider_directory(tmp_path):
    extension_config.save_extension_launch(
        "acme", _receipt(), provider_home=str(tmp_path / "home"),
    )
    provider_directory = settings.SETTINGS_DIR / "providers" / "acme"
    moved = provider_directory.with_name("acme-real")
    provider_directory.rename(moved)
    provider_directory.symlink_to(moved, target_is_directory=True)

    with pytest.raises(OSError, match="real directory"):
        extension_config.load_extension_launch("acme")


def test_store_rejects_digest_matching_but_noncanonical_json(tmp_path):
    extension_config.default_provider_home("acme")
    value = {
        "payload": {"schema": 1, "marker": "noncanonical"},
        "media_type": "application/vnd.acme.receipt+json",
        "provider_id": "acme",
        "schema": 1,
    }
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    blob = (
        settings.SETTINGS_DIR
        / "providers"
        / "acme"
        / "receipt-v1-{}.json".format(digest)
    )
    blob.write_bytes(encoded)
    blob.chmod(0o600)
    settings.set_extension_launch_settings(
        settings.ExtensionLaunchSettingsV1(
            provider_id="acme",
            receipt_sha256=digest,
            provider_home=str(tmp_path / "home"),
        )
    )

    with pytest.raises(ValueError, match="canonical"):
        extension_config.load_extension_launch("acme")


def test_receipt_first_crash_order_never_publishes_missing_data(monkeypatch, tmp_path):
    def fail(_value):
        raise RuntimeError("interrupted settings write")

    monkeypatch.setattr(settings, "set_extension_launch_settings", fail)
    with pytest.raises(RuntimeError):
        extension_config.save_extension_launch(
            "acme", _receipt(), provider_home=str(tmp_path / "home"),
        )

    assert settings.get_extension_launch_settings("acme") is None
    assert len(_receipt_files()) == 1


def test_store_serializes_publish_and_clear_for_the_same_receipt(monkeypatch, tmp_path):
    receipt = _receipt()
    extension_config.save_extension_launch(
        "acme", receipt, provider_home=str(tmp_path / "home"),
    )
    original_write = extension_config._write_receipt
    entered = threading.Event()
    release = threading.Event()
    clear_done = threading.Event()
    failures = []
    clear_result = []

    def blocking_write(path, encoded):
        entered.set()
        if not release.wait(2):
            raise AssertionError("test did not release receipt publication")
        return original_write(path, encoded)

    def publish():
        try:
            extension_config.save_extension_launch(
                "acme", receipt, provider_home=str(tmp_path / "home"),
            )
        except BaseException as exc:
            failures.append(exc)

    def clear():
        try:
            clear_result.append(extension_config.clear_extension_launch("acme"))
        except BaseException as exc:
            failures.append(exc)
        finally:
            clear_done.set()

    monkeypatch.setattr(extension_config, "_write_receipt", blocking_write)
    publisher = threading.Thread(target=publish)
    clearer = threading.Thread(target=clear)
    publisher.start()
    assert entered.wait(2)
    clearer.start()
    assert not clear_done.wait(0.1)
    release.set()
    publisher.join(2)
    clearer.join(2)

    assert not failures
    assert clear_result == [True]
    assert extension_config.load_extension_launch("acme") is None
    assert stat.S_IMODE(
        (settings.SETTINGS_DIR / "providers.lock").stat().st_mode
    ) == 0o600


def test_reserved_settings_pointer_is_typed_and_malformed_value_isolated(tmp_path):
    settings.set_provider_setting("acme", "format", "wide")
    with pytest.raises(ValueError):
        settings.set_provider_setting("acme", "unified_cli_launch", {})
    with pytest.raises(ValueError, match="typed settings API"):
        settings.set(
            "provider_settings",
            {
                "acme": {
                    "unified_cli_launch": {
                        "schema": 1,
                        "receipt_sha256": "0" * 64,
                        "provider_home": None,
                    }
                }
            },
        )
    forged = settings.Settings(
        provider_settings={
            "acme": {
                "unified_cli_launch": {
                    "schema": 1,
                    "receipt_sha256": "0" * 64,
                    "provider_home": None,
                }
            }
        }
    )
    with pytest.raises(ValueError, match="typed API"):
        settings.save_settings(forged)

    pointer = settings.ExtensionLaunchSettingsV1(
        provider_id="acme", receipt_sha256="1" * 64, provider_home=None,
    )
    settings.set_extension_launch_settings(pointer)
    round_trip = settings.load_settings()
    round_trip.theme = "dark"
    settings.save_settings(round_trip)
    assert settings.get_extension_launch_settings("acme") == pointer

    settings.SETTINGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "version": 2,
                "settings": {
                    "provider_settings": {
                        "acme": {
                            "format": "wide",
                            "unified_cli_launch": {
                                "schema": 99,
                                "receipt_sha256": "0" * 64,
                                "provider_home": None,
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings.SETTINGS_FILE.chmod(0o600)
    assert settings.load_settings().provider_settings == {
        "acme": {"format": "wide"}
    }
    with pytest.raises(ValueError, match="unsupported"):
        settings.get_extension_launch_settings("acme")
    with pytest.raises(ValueError, match="unsupported"):
        extension_config.load_extension_launch("acme")
    malformed = settings.SETTINGS_FILE.read_bytes()
    with pytest.raises(ValueError, match="reserved extension launch"):
        settings.set("theme", "dark")
    assert settings.SETTINGS_FILE.read_bytes() == malformed
    assert extension_config.clear_extension_launch("acme") is True
    assert settings.get_provider_settings("acme") == {"format": "wide"}


def test_ordinary_writes_preserve_only_the_current_typed_launch_pointer():
    stale_without_pointer = settings.load_settings()
    pointer = settings.ExtensionLaunchSettingsV1(
        provider_id="acme",
        receipt_sha256="2" * 64,
        provider_home=None,
    )
    settings.set_extension_launch_settings(pointer)

    stale_without_pointer.theme = "dark"
    settings.save_settings(stale_without_pointer)
    assert settings.get_extension_launch_settings("acme") == pointer

    settings.set("theme", "light")
    assert settings.get_extension_launch_settings("acme") == pointer
    settings.set_provider_setting("acme", "density", "compact")
    assert settings.get_extension_launch_settings("acme") == pointer

    settings.set(
        "provider_settings",
        {"acme": {"format": "wide"}, "other": {"format": "compact"}},
    )
    assert settings.get_extension_launch_settings("acme") == pointer
    assert settings.get_provider_settings("acme") == {"format": "wide"}
    assert settings.clear_provider_settings("acme") is True
    assert settings.get_provider_settings("acme") == {}
    assert settings.get_extension_launch_settings("acme") == pointer
    assert settings.clear_provider_settings("acme") is False

    stale_with_pointer = settings.load_settings()
    assert settings.clear_extension_launch_settings("acme") is True
    stale_with_pointer.theme = "light"
    with pytest.raises(ValueError, match="typed API"):
        settings.save_settings(stale_with_pointer)
    assert settings.get_extension_launch_settings("acme") is None


def test_schema_booleans_are_not_accepted_as_version_one():
    record = {
        "schema": True,
        "receipt_sha256": "0" * 64,
        "provider_home": None,
    }
    with pytest.raises(ValueError, match="unsupported"):
        settings._extension_launch_record(record, provider_id="acme")

    encoded = (
        b'{"schema":true,"provider_id":"acme",'
        b'"media_type":"application/vnd.acme.receipt+json",'
        b'"payload":{"schema":1}}'
    )
    with pytest.raises(ValueError, match="schema"):
        extension_config._decoded_envelope(encoded)


def test_configure_create_models_and_doctor_share_typed_receipt_home_and_env(
    monkeypatch, tmp_path
):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls))
    home = str(tmp_path / "provider-home")
    receipt = _receipt()
    launch = ExtensionLaunchOverridesV1(
        receipt=receipt,
        provider_home=home,
        extra_env={"SAFE_ENV": "ephemeral"},
    )

    stored = configure_extension_provider("acme", launch)
    assert stored.receipt == receipt
    configure_context = next(call[1] for call in calls if call[0] == "bind")
    doctor_context = next(call[1] for call in calls if call[0] == "doctor")
    assert doctor_context is configure_context
    assert dict(configure_context.provider_env) == {"SAFE_ENV": "ephemeral"}

    calls.clear()
    monkeypatch.setenv("SAFE_ENV", "ambient-must-not-flow")
    provider = create("acme", cwd=str(tmp_path))
    models = list_models("acme")
    state = doctor_provider("acme")
    assert provider.name == "acme"
    assert provider.request.workspace == str(tmp_path)
    assert [item.id for item in models] == ["acme-default"]
    assert state == {"available": True}
    contexts = [call[1] for call in calls if call[0] == "bind"]
    assert len(contexts) == 3
    assert all(context.receipt == receipt for context in contexts)
    assert all(context.provider_home == home for context in contexts)
    assert all(dict(context.provider_env) == {} for context in contexts)
    assert "ephemeral" not in settings.SETTINGS_FILE.read_text(encoding="utf-8")

    explicit_env = ExtensionLaunchOverridesV1(extra_env={"SAFE_ENV": "new"})
    bound = bind_extension_provider("acme", extension_launch=explicit_env)
    bound.model_lister()
    bound.doctor()
    bound.factory(
        ProviderCreateRequestV1(
            provider_id="acme",
            model="acme-default",
            workspace=str(tmp_path),
        )
    )
    recent = calls[-4:]
    assert [item[0] for item in recent] == ["bind", "models", "doctor", "factory"]
    assert all(item[1] is recent[0][1] for item in recent[1:])
    assert dict(recent[0][1].provider_env) == {"SAFE_ENV": "new"}


def test_typed_launch_rejects_undeclared_env_before_state_access(
    monkeypatch, tmp_path
):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls))
    state_access = []

    def forbidden_state_access(*args, **kwargs):
        state_access.append((args, kwargs))
        raise AssertionError("typed environment validation happened too late")

    monkeypatch.setattr(
        settings, "get_extension_launch_settings", forbidden_state_access,
    )
    monkeypatch.setattr(
        extension_config, "load_extension_launch", forbidden_state_access,
    )
    monkeypatch.setattr(
        extension_config, "default_provider_home", forbidden_state_access,
    )
    launch = ExtensionLaunchOverridesV1(
        receipt=_receipt(),
        extra_env={"SAFE_ENV": "ok", "IGNORED_ENV": "typo"},
    )

    with pytest.raises(UnifiedError, match="configuration is invalid"):
        configure_extension_provider("acme", launch)

    assert calls == []
    assert state_access == []
    assert not settings.SETTINGS_DIR.exists()


def test_explicit_receipt_bypasses_corrupt_stored_blob_without_leaking_details(
    monkeypatch, tmp_path
):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls))
    receipt = _receipt()
    home = str(tmp_path / "provider-home")
    configure_extension_provider(
        "acme",
        ExtensionLaunchOverridesV1(receipt=receipt, provider_home=home),
    )
    blob = _receipt_files()[0]
    blob.write_bytes(blob.read_bytes() + b"tampered")

    with pytest.raises(UnifiedError) as failed:
        create("acme", cwd=str(tmp_path))
    rendered = str(failed.value)
    assert "configuration is invalid" in rendered
    assert str(blob) not in rendered
    assert "digest" not in rendered

    provider = create(
        "acme",
        cwd=str(tmp_path),
        extension_launch=ExtensionLaunchOverridesV1(receipt=receipt),
    )
    assert provider.name == "acme"
    context = [call[1] for call in calls if call[0] == "bind"][-1]
    assert context.receipt == receipt
    assert context.provider_home == home


def test_unhealthy_or_failing_binder_is_sanitized_and_never_persisted(
    monkeypatch, tmp_path
):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls, available=False))
    with pytest.raises(UnifiedError) as unhealthy:
        configure_extension_provider(
            "acme",
            ExtensionLaunchOverridesV1(
                receipt=_receipt(), provider_home=str(tmp_path / "home"),
            ),
        )
    assert "configuration is invalid" in str(unhealthy.value)
    assert settings.get_extension_launch_settings("acme") is None

    _reset_provider_registry_for_tests()
    _set_plugin(
        monkeypatch,
        _configured_plugin(calls, binder_error="secret=binder-leak"),
    )
    with pytest.raises(UnifiedError) as failed:
        create(
            "acme",
            cwd=str(tmp_path),
            extension_launch=ExtensionLaunchOverridesV1(receipt=_receipt()),
        )
    rendered = str(failed.value)
    assert "could not bind" in rendered
    assert "binder-leak" not in rendered


def test_invalid_create_options_are_rejected_before_binder_runs(monkeypatch, tmp_path):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls))

    with pytest.raises(UnifiedError, match="could not be created"):
        create(
            "acme",
            cwd=str(tmp_path),
            unsupported_option="must-not-reach-binder",
            extension_launch=ExtensionLaunchOverridesV1(receipt=_receipt()),
        )
    assert calls == []

    with pytest.raises(UnifiedError, match="could not be created"):
        create(
            "acme",
            extension_launch=ExtensionLaunchOverridesV1(receipt=_receipt()),
        )
    assert calls == []


def test_malformed_stored_pointer_fails_before_fallback_binder(monkeypatch, tmp_path):
    calls = []
    _set_plugin(monkeypatch, _configured_plugin(calls))
    settings.SETTINGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "version": 2,
                "settings": {
                    "provider_settings": {
                        "acme": {
                            "unified_cli_launch": {
                                "schema": True,
                                "receipt_sha256": "0" * 64,
                                "provider_home": None,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings.SETTINGS_FILE.chmod(0o600)

    with pytest.raises(UnifiedError, match="configuration is invalid"):
        create("acme", cwd=str(tmp_path))
    assert calls == []


def test_extension_doctor_result_is_bounded_core_owned_data(monkeypatch):
    supplied = {
        "available": True,
        "details": ["ready", {"version": "1.2.3"}],
    }

    def binder(context):
        return BoundProviderOperationsV1(
            provider_id="acme",
            factory=lambda request: _DummyProvider(request.model, request),
            model_lister=lambda: (),
            doctor=lambda: supplied,
            normalized_receipt=context.receipt or _receipt(),
            provider_home=context.provider_home,
        )

    plugin = ProviderPluginV1(
        id="acme",
        factory=lambda **opts: _DummyProvider("acme", None),
        default_model="acme-default",
        model_lister=lambda: (),
        doctor=lambda: (_ for _ in ()).throw(
            AssertionError("configured doctor used legacy callback")
        ),
        route_prefixes=("acme",),
        configuration_abi_version=PROVIDER_CONFIGURATION_ABI_V1,
        launch_binder=binder,
    )
    _set_plugin(monkeypatch, plugin)

    result = doctor_provider("acme")
    supplied["available"] = False
    supplied["details"][1]["version"] = "mutated"
    assert result == {
        "available": True,
        "details": ["ready", {"version": "1.2.3"}],
    }
    assert result is not supplied

    supplied["details"] = [object()]
    with pytest.raises(UnifiedError, match="doctor failed"):
        doctor_provider("acme")

    _reset_provider_registry_for_tests()
    legacy_result = ("legacy-any", object())
    legacy = ProviderPluginV1(
        id="acme",
        factory=lambda **opts: _DummyProvider("acme", None),
        default_model="acme-default",
        model_lister=lambda: (),
        doctor=lambda: legacy_result,
        route_prefixes=("acme",),
    )
    _set_plugin(monkeypatch, legacy)
    assert doctor_provider("acme") is legacy_result


def test_held_provider_rejects_before_configuration_store_read(monkeypatch):
    held = ProviderPluginV1(
        id="acme",
        factory=lambda **opts: _DummyProvider("acme", None),
        default_model="acme-default",
        model_lister=lambda: (),
        doctor=lambda: {},
        route_prefixes=("acme",),
        support_status="held",
    )
    _set_plugin(monkeypatch, held)
    monkeypatch.setattr(
        extension_config,
        "load_extension_launch",
        lambda provider_id: (_ for _ in ()).throw(
            AssertionError("configuration store read before Held gate")
        ),
    )
    with pytest.raises(UnifiedError, match="held"):
        configure_extension_provider("acme", ExtensionLaunchOverridesV1())


def test_builtin_fast_paths_reject_extension_context_without_discovery(
    monkeypatch, tmp_path
):
    def forbidden():
        raise AssertionError("entry points touched on built-in configuration error")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden)
    launch = ExtensionLaunchOverridesV1(receipt=_receipt())
    with pytest.raises(UnifiedError):
        create("claude", extension_launch=launch, bin_path="/bin/echo")
    with pytest.raises(UnifiedError):
        list_models("codex", extension_launch=launch)
    with pytest.raises(UnifiedError):
        doctor_provider("gemini", extension_launch=launch)

    conversation = UnifiedConversation(
        default_provider="acme",
        provider_opts={"extra_env": {"SAFE_ENV": "raw"}},
    )
    with pytest.raises(UnifiedError, match="typed"):
        conversation._get_client("acme", None)
