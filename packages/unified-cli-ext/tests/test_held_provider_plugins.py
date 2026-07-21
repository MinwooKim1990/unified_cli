"""Stage 5B contract checks for inert external provider entry points."""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace

import pytest

from unified_cli import (
    PROVIDERS,
    ProviderPluginV1,
    UnifiedError,
    create,
    doctor_provider,
    list_models,
    list_providers,
)
from unified_cli import registry as core_registry
from unified_cli_ext.providers import AdapterStatus, ProviderAdapterSpecV1, ProviderCapability
from unified_cli_ext.providers.held import (
    HELD_UNAVAILABLE_MESSAGE,
    HeldProviderUnavailableError,
)


ENTRY_POINTS = {
    "grok": "unified_cli_ext.providers.grok:PLUGIN",
    "kimi": "unified_cli_ext.providers.kimi:PLUGIN",
    "copilot": "unified_cli_ext.providers.copilot:PLUGIN",
    "cursor": "unified_cli_ext.providers.cursor:PLUGIN",
}

EXPECTED_COMMANDS = {
    "grok": {
        "executable": "grok",
        "prompt": (
            "--no-auto-update",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "streaming-json",
        ),
        "transport": "jsonl",
        "environment": frozenset(),
    },
    "kimi": {
        "executable": "kimi",
        "prompt": ("--output-format", "stream-json"),
        "transport": "jsonl",
        "environment": frozenset(("KIMI_CODE_NO_AUTO_UPDATE",)),
    },
    "copilot": {
        "executable": "copilot",
        "prompt": (
            "--silent",
            "--no-ask-user",
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--available-tools",
            "view,glob,grep",
        ),
        "transport": "plain",
        "environment": frozenset(),
    },
    "cursor": {
        "executable": "cursor-agent",
        "prompt": ("--print", "--output-format", "json"),
        "transport": "json",
        "environment": frozenset(),
    },
}


def _module(provider_id):
    return importlib.import_module("unified_cli_ext.providers." + provider_id)


def test_pyproject_registers_all_held_provider_entry_points_exactly():
    package_root = pathlib.Path(__file__).resolve().parents[1]
    text = (package_root / "pyproject.toml").read_text(encoding="utf-8")
    group = '[project.entry-points."unified_cli.providers.v1"]'
    assert group in text
    section = text.split(group, 1)[1].split("\n[", 1)[0]
    for provider_id, target in ENTRY_POINTS.items():
        assert '{} = "{}"'.format(provider_id, target) in section


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_held_specs_and_plugins_are_immutable_and_minimal(provider_id):
    module = _module(provider_id)
    spec = module.ADAPTER_SPEC
    plugin = module.PLUGIN
    expected = EXPECTED_COMMANDS[provider_id]

    assert type(spec) is ProviderAdapterSpecV1
    assert spec.id == provider_id
    assert spec.status is AdapterStatus.HELD
    assert spec.binary.executable == expected["executable"]
    assert spec.binary.version_probe.command.argv == ("--version",)
    assert spec.binary.feature_probe.command.argv == ("--help",)
    assert spec.prompt.fixed_argv == expected["prompt"]
    assert spec.transport.value == expected["transport"]
    assert spec.environment.allowed_keys == expected["environment"]
    assert spec.environment.required_keys == frozenset()
    assert spec.capabilities == frozenset((ProviderCapability.CHAT.value,))
    assert spec.auth is None
    assert spec.models is None
    assert spec.doctor is None
    assert spec.server_policy.enabled is False
    assert spec.server_policy.requires_external_isolation is True
    with pytest.raises(FrozenInstanceError):
        spec.id = "changed"  # type: ignore[misc]

    assert type(plugin) is ProviderPluginV1
    assert plugin.id == provider_id
    assert plugin.support_status == "held"
    assert plugin.capabilities == frozenset()
    assert plugin.route_prefixes == (provider_id,)
    assert plugin.server_policy.enabled is False
    assert plugin.model_lister() == ()
    doctor = plugin.doctor()
    assert dict(doctor) == {
        "id": provider_id,
        "status": "Held",
        "available": False,
        "message": HELD_UNAVAILABLE_MESSAGE,
    }
    with pytest.raises(TypeError):
        doctor["available"] = True


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_held_factories_fail_before_provider_creation_or_execution(provider_id, monkeypatch):
    module = _module(provider_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("held factory attempted external execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    with pytest.raises(HeldProviderUnavailableError) as caught:
        module.PLUGIN.factory(model="ignored")
    assert str(caught.value) == HELD_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize("provider_id", tuple(ENTRY_POINTS))
def test_core_held_gate_never_calls_plugin_callbacks(provider_id, monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}

    def forbidden(name):
        def callback(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError("Core called a Held plugin callback")

        return callback

    plugin = replace(
        _module(provider_id).PLUGIN,
        factory=forbidden("factory"),
        model_lister=forbidden("models"),
        doctor=forbidden("doctor"),
    )

    class FakeEntryPoint:
        group = core_registry.ENTRY_POINT_GROUP
        name = provider_id
        load_calls = 0

        def load(self):
            self.load_calls += 1
            return plugin

    entry_point = FakeEntryPoint()
    core_registry._reset_provider_registry_for_tests()
    monkeypatch.setattr(
        core_registry.importlib_metadata,
        "entry_points",
        lambda: [entry_point],
    )
    try:
        discovered = list_providers(include_ext=True)[-1]
        assert discovered.lifecycle_status == "discovered"
        assert discovered.support_status == "unknown"
        assert entry_point.load_calls == 0

        for call in (
            lambda: create(provider_id),
            lambda: list_models(provider_id),
            lambda: doctor_provider(provider_id),
        ):
            with pytest.raises(
                UnifiedError, match="is held",
            ):
                call()

        assert calls == {"factory": 0, "models": 0, "doctor": 0}
        assert entry_point.load_calls == 1
        loaded = list_providers(include_ext=True)[-1]
        assert loaded.lifecycle_status == "loaded"
        assert loaded.support_status == "held"
        assert loaded.default_model is None
        assert loaded.capabilities == frozenset()
    finally:
        core_registry._reset_provider_registry_for_tests()


def test_base_import_and_entry_point_metadata_enumeration_do_not_import_plugins():
    root = pathlib.Path(__file__).resolve().parents[3]
    ext_source = root / "packages" / "unified-cli-ext" / "src"
    script = r'''
import importlib.metadata
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, {root!r})
sys.path.insert(0, {ext_source!r})
def forbidden(*args, **kwargs):
    raise AssertionError("passive discovery attempted an external operation")
subprocess.Popen = forbidden
shutil.which = forbidden
socket.create_connection = forbidden
import unified_cli_ext
assert not any(name.startswith("unified_cli_ext.providers.") and name.rsplit(".", 1)[-1] in {providers!r} for name in sys.modules)
importlib.metadata.entry_points()
assert not any(name.startswith("unified_cli_ext.providers.") and name.rsplit(".", 1)[-1] in {providers!r} for name in sys.modules)
'''.format(
        root=str(root),
        ext_source=str(ext_source),
        providers=tuple(ENTRY_POINTS),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_core_builtins_are_unchanged_by_held_extension_metadata():
    assert tuple(PROVIDERS) == ("claude", "codex", "gemini")
    assert [item.id for item in list_providers()] == ["claude", "codex", "gemini"]
    assert create("claude", bin_path="/bin/echo").name == "claude"


def test_cursor_records_that_stage_6_must_establish_prompt_framing():
    module = _module("cursor")
    assert module.CURSOR_PROMPT_FORM_REQUIRES_STAGE_6_EVIDENCE is True
    assert "--" not in module.ADAPTER_SPEC.prompt.fixed_argv
