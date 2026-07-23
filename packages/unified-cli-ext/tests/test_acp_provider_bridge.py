from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from unified_cli.errors import UnifiedError
from unified_cli.plugin import ProviderLaunchContextV1
from unified_cli_ext.errors import ConfigurationError
from unified_cli_ext.normalization import (
    DoneEvent,
    SessionEvent,
    SessionRef,
    TextDeltaEvent,
    UsageEvent,
)
from unified_cli_ext.providers import hermes, kilo, poolside, qoder
from unified_cli_ext.providers.acp_bridge import AcpProviderBridge
from unified_cli_ext.providers.contract import AdapterStatus


def _provider_executable(tmp_path, name: str, version: str, help_text: str):
    path = tmp_path / name
    quoted_help = " ".join("'{}'".format(line.replace("'", "'\\''")) for line in help_text.splitlines())
    quoted_version = "'{}'".format(version.replace("'", "'\\''"))
    path.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"--help"*) printf "%s\\n" ' + quoted_help + " ;;\n"
        '  *) printf "%s\\n" ' + quoted_version + " ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_experimental_metadata_and_core_server_are_closed():
    expected = {
        qoder: ("qodercli", ("--acp", "--permission-mode", "dont_ask")),
        kilo: ("kilo", ("acp", "--hostname", "127.0.0.1", "--port", "0")),
        poolside: ("pool", ("acp",)),
    }
    for module, (executable, argv) in expected.items():
        assert module.ADAPTER_SPEC.status is AdapterStatus.EXPERIMENTAL
        assert module.ADAPTER_SPEC.binary.executable == executable
        assert module.ADAPTER_SPEC.prompt.fixed_argv == argv
        assert module.PLUGIN.support_status == "experimental"
        assert module.PLUGIN.capabilities == frozenset(("chat",))
        assert module.PLUGIN.model_lister()[0].id == "default"
        assert module.PLUGIN.server_policy.enabled is False
    assert hermes.ADAPTER_SPEC.status is AdapterStatus.HELD
    assert hermes.PLUGIN.support_status == "held"
    assert hermes.PLUGIN.server_policy.enabled is False


def test_qoder_factory_runs_exact_closed_acp_argv_and_normalizes(monkeypatch, tmp_path):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    captured = {}

    class FakeAcp:
        def __init__(self, argv, **kwargs):
            captured["argv"] = tuple(argv)
            captured["kwargs"] = kwargs

        async def text_turn(self, prompt):
            captured["prompt"] = prompt
            return (
                SessionEvent(SessionRef("qoder", "session-1")),
                TextDeltaEvent("hello", "message-1"),
                UsageEvent(2, 3, 1),
                DoneEvent("end_turn"),
            )

    monkeypatch.setattr(
        "unified_cli_ext.providers.acp_bridge.AcpProcessTransportV1", FakeAcp
    )
    provider_home = tmp_path / "qoder-home"
    provider = qoder.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(provider_home),
        extra_env={
            "QODER_PERSONAL_ACCESS_TOKEN": "test-token",
        },
    )
    assert isinstance(provider, AcpProviderBridge)
    response = provider.chat("hello")
    assert response.text == "hello"
    assert response.session_id == "session-1"
    assert response.usage.input_tokens == 2
    assert captured["argv"] == (
        str(executable),
        "--acp",
        "--permission-mode",
        "dont_ask",
    )
    assert captured["prompt"] == "hello"
    assert captured["kwargs"]["provider_namespace"] == "qoder"
    assert captured["kwargs"]["persistent_home"] == str(provider_home)
    assert captured["kwargs"]["provider_env"] == {
        "QODER_PERSONAL_ACCESS_TOKEN": "test-token"
    }
    settings = provider_home / ".qoder" / "settings.json"
    assert settings.stat().st_mode & 0o077 == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "general": {
            "defaultPermissionMode": "dont_ask",
            "enableAutoUpdate": False,
        },
        "permissions": {"allow": [], "ask": [], "deny": ["*"]},
    }


def test_qoder_bridge_rejects_resume_images_models_and_workspace_config(
    monkeypatch, tmp_path
):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    provider = qoder.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(tmp_path / "home"),
        extra_env={},
    )
    for kwargs in (
        {"session_id": "old"},
        {"resume_last": True},
        {"images": ["image.png"]},
        {"model": "other"},
    ):
        with pytest.raises(UnifiedError):
            provider.chat("hello", **kwargs)
    (tmp_path / ".qoder").mkdir()
    with pytest.raises(UnifiedError):
        provider.chat("hello")


def test_qoder_rejects_project_mcp_config_before_probe_or_acp_spawn(
    monkeypatch, tmp_path
):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    calls = {"popen": 0, "acp": 0}

    def forbidden_popen(*args, **kwargs):
        del args, kwargs
        calls["popen"] += 1
        raise AssertionError("unsafe workspace config reached a provider probe")

    class ForbiddenAcp:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            calls["acp"] += 1
            raise AssertionError("unsafe workspace config reached ACP spawn")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(
        "unified_cli_ext.providers.acp_bridge.AcpProcessTransportV1",
        ForbiddenAcp,
    )
    with pytest.raises(ConfigurationError):
        qoder.PLUGIN.factory(
            cwd=str(tmp_path),
            bin_path=str(executable),
            provider_home=str(tmp_path / "home"),
        )
    assert calls == {"popen": 0, "acp": 0}


def test_qoder_turn_guard_precedes_cold_inspection(monkeypatch, tmp_path):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    provider = qoder.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(tmp_path / "home"),
    )
    provider._adapter.invalidate_cache("inspect")
    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("unsafe workspace config reached a cold probe")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(UnifiedError):
        provider.chat("hello")
    assert calls == 0


def test_factory_rejects_unsupported_model_before_probe(monkeypatch, tmp_path):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("unsupported model reached a provider probe")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ConfigurationError):
        qoder.PLUGIN.factory(
            model="other",
            cwd=str(tmp_path),
            bin_path=str(executable),
            provider_home=str(tmp_path / "home"),
        )
    assert calls == 0


def test_achat_rejects_unknown_options_before_turn(monkeypatch, tmp_path):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    provider = qoder.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(tmp_path / "home"),
    )
    calls = 0

    async def forbidden(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("unsupported async option reached the ACP turn")

    monkeypatch.setattr(provider, "_turn_events", forbidden)
    with pytest.raises(UnifiedError):
        asyncio.run(provider.achat("hello", web_search=True))
    assert calls == 0


@pytest.mark.parametrize(
    ("module", "name", "version", "help_text", "expected_argv"),
    (
        (
            kilo,
            "kilo",
            "kilo 7.4.11",
            "kilo acp\nstart ACP\n--hostname\n--port",
            ("acp", "--hostname", "127.0.0.1", "--port", "0"),
        ),
        (
            poolside,
            "pool",
            "pool 1.0.13",
            "pool acp\nAgent Client Protocol\nstandard input\n--version",
            ("acp",),
        ),
    ),
)
def test_kilo_and_poolside_execute_shared_acp_runtime(
    monkeypatch,
    tmp_path,
    module,
    name,
    version,
    help_text,
    expected_argv,
):
    executable = _provider_executable(tmp_path, name, version, help_text)
    captured = {}

    class FakeAcp:
        def __init__(self, argv, **kwargs):
            captured["argv"] = tuple(argv)
            captured["kwargs"] = kwargs

        async def text_turn(self, prompt):
            captured["prompt"] = prompt
            return (
                SessionEvent(SessionRef(module.ADAPTER_SPEC.id, "session-1")),
                TextDeltaEvent("ok", "message-1"),
                DoneEvent("end_turn"),
            )

    monkeypatch.setattr(
        "unified_cli_ext.providers.acp_bridge.AcpProcessTransportV1", FakeAcp
    )
    provider = module.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(tmp_path / (name + "-home")),
    )
    response = provider.chat("hello")
    assert response.text == "ok"
    assert captured["argv"] == (str(executable),) + expected_argv
    assert captured["kwargs"]["provider_namespace"] == module.ADAPTER_SPEC.id
    assert captured["kwargs"]["persistent_home"].endswith(name + "-home")
    if module is kilo:
        assert captured["kwargs"]["provider_env"]["KILO_PURE"] == "1"
        assert captured["kwargs"]["provider_env"]["KILO_NO_DAEMON"] == "1"
    else:
        assert captured["kwargs"]["provider_env"] == {}


def test_poolside_rejects_persistent_policy_before_probe_and_each_turn(
    monkeypatch, tmp_path
):
    executable = _provider_executable(
        tmp_path,
        "pool",
        "pool 1.0.13",
        "pool acp\nAgent Client Protocol\nstandard input\n--version",
    )
    blocked_home = tmp_path / "blocked-pool-home"
    blocked_policy = blocked_home / ".config" / "poolside"
    blocked_policy.mkdir(parents=True)
    (blocked_policy / "settings.yaml").write_text(
        "mcp_servers: {}", encoding="utf-8"
    )
    popen_calls = 0
    real_popen = subprocess.Popen

    def forbidden_popen(*args, **kwargs):
        nonlocal popen_calls
        del args, kwargs
        popen_calls += 1
        raise AssertionError("unsafe Poolside policy reached a provider probe")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    with pytest.raises(ConfigurationError):
        poolside.PLUGIN.factory(
            cwd=str(tmp_path),
            bin_path=str(executable),
            provider_home=str(blocked_home),
        )
    assert popen_calls == 0
    monkeypatch.setattr(subprocess, "Popen", real_popen)

    home = tmp_path / "pool-home"
    provider = poolside.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(home),
    )
    policy = home / ".config" / "poolside"
    policy.mkdir(parents=True)
    (policy / "settings.yaml").write_text("mcp_servers: {}", encoding="utf-8")
    provider._adapter.invalidate_cache("inspect")
    calls = 0

    class ForbiddenAcp:
        def __init__(self, *args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            raise AssertionError("unsafe Poolside policy reached ACP spawn")

    monkeypatch.setattr(
        "unified_cli_ext.providers.acp_bridge.AcpProcessTransportV1",
        ForbiddenAcp,
    )
    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    popen_calls = 0
    with pytest.raises(UnifiedError):
        provider.chat("hello")
    assert calls == 0
    assert popen_calls == 0


def test_poolside_bound_doctor_and_models_guard_before_cold_probe(
    monkeypatch, tmp_path
):
    executable = _provider_executable(
        tmp_path,
        "pool",
        "pool 1.0.13",
        "pool acp\nAgent Client Protocol\nstandard input\n--version",
    )
    home = tmp_path / "pool-bound-home"
    binder = poolside.PLUGIN.launch_binder
    assert binder is not None
    bound = binder(
        ProviderLaunchContextV1(
            provider_id="poolside",
            bin_path=str(executable),
            provider_home=str(home),
        )
    )
    policy = home / ".config" / "poolside"
    policy.mkdir(parents=True)
    (policy / "settings.yaml").write_text("mcp_servers: {}", encoding="utf-8")
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("unsafe bound policy reached a cold probe")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert bound.doctor()["available"] is False
    with pytest.raises(ConfigurationError):
        bound.model_lister()
    assert calls == 0


def test_qoder_environment_allowlist_drops_unrelated_values(tmp_path):
    executable = _provider_executable(
        tmp_path,
        "qodercli",
        "qodercli 1.1.1",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
    )
    provider = qoder.PLUGIN.factory(
        cwd=str(tmp_path),
        bin_path=str(executable),
        provider_home=str(tmp_path / "home"),
        extra_env={"AWS_SECRET_ACCESS_KEY": "no"},
    )
    assert provider._env() == {}
