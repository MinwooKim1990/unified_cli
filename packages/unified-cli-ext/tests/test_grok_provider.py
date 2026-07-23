from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from unified_cli import registry as core_registry
from unified_cli import settings as core_settings
from unified_cli.errors import UnifiedError
from unified_cli.extension_config import ExtensionLaunchOverridesV1
from unified_cli.factory import create as core_create
from unified_cli.plugin import ProviderCreateRequestV1, ProviderLaunchContextV1
from unified_cli.registry import (
    _reset_provider_registry_for_tests,
    clear_extension_provider_configuration,
    configure_extension_provider,
)
from unified_cli_ext import ConfigurationError, ProtocolError
from unified_cli_ext.providers import AdapterStatus, ProviderAdapterV1
from unified_cli_ext.providers.grok import (
    ADAPTER_SPEC,
    GROK_HEADLESS_FIXED_ARGV,
    GROK_OFFICIAL_INSTALLER,
    GROK_REJECTED_PACKAGE_IDENTITIES,
    PLUGIN,
)


@pytest.fixture
def grok_binary(tmp_path):
    source = Path(__file__).parent / "fixtures" / "providers" / "fake_grok_cli.py"
    interpreter = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    target = tmp_path / "grok"
    source_text = source.read_text(encoding="utf-8")
    _, separator, body = source_text.partition("\n")
    assert separator
    target.write_text("#!{}\n{}".format(interpreter, body), encoding="utf-8")
    target.chmod(0o700)
    return target


def provider(tmp_path, grok_binary, **options):
    return PLUGIN.factory(cwd=str(tmp_path), bin_path=str(grok_binary), **options)


def test_grok_preview_metadata_exact_argv_and_server_disabled(grok_binary):
    assert ADAPTER_SPEC.status is AdapterStatus.PREVIEW
    assert ADAPTER_SPEC.prompt.fixed_argv == GROK_HEADLESS_FIXED_ARGV
    assert ADAPTER_SPEC.environment.allowed_keys == frozenset(
        (
            "XAI_API_KEY",
            "GROK_MANAGED_MCPS_ENABLED",
            "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED",
        )
    )
    assert ADAPTER_SPEC.environment.required_keys == frozenset()
    assert dict(ADAPTER_SPEC.environment.fixed_values) == {
        "GROK_MANAGED_MCPS_ENABLED": "false",
        "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
    }
    assert ADAPTER_SPEC.capabilities == frozenset(("chat", "sessions", "stream"))
    assert ADAPTER_SPEC.server_policy.enabled is False
    assert PLUGIN.support_status == "preview"
    assert PLUGIN.server_policy.enabled is False
    assert GROK_OFFICIAL_INSTALLER == "https://x.ai/cli/install.sh"
    assert GROK_REJECTED_PACKAGE_IDENTITIES == ("@vibe-kit/grok-cli",)

    adapter = ProviderAdapterV1(ADAPTER_SPEC)
    binary = adapter.resolve_binary(str(grok_binary))
    prompt = "--leading; $(touch no)\nsecond line"
    built = adapter.build_prompt(
        binary, prompt, {"model": "grok-custom", "session": "session old"}
    )
    assert built.argv == (
        str(grok_binary),
        *GROK_HEADLESS_FIXED_ARGV,
        "-m",
        "grok-custom",
        "-r",
        "session old",
        "-p",
        prompt,
    )
    assert built.stdin_text is None


def test_grok_official_version_help_and_doctor_gate(tmp_path, grok_binary):
    instance = provider(tmp_path, grok_binary)
    assert instance.name == "grok"
    assert instance.model == "grok-build"

    grok_binary.with_suffix(".version").write_text(
        "grok 0.2.109 (wrong)\n", encoding="utf-8"
    )
    with pytest.raises(ProtocolError, match="below the adapter minimum"):
        provider(tmp_path, grok_binary)


def test_grok_managed_mcp_controls_are_fixed_and_not_user_overridable(
    tmp_path, grok_binary
):
    instance = provider(
        tmp_path,
        grok_binary,
        extra_env={
            "XAI_API_KEY": "fixture-key",
            "GROK_MANAGED_MCPS_ENABLED": "true",
            "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "true",
        },
    )
    assert instance.chat("managed-mcp-disabled").text == "managed-mcp-disabled"


@pytest.mark.parametrize(
    "version", ("grok 0.2.110", "grok 0.2.111 (next-patch)", "grok 0.3.0")
)
def test_grok_official_version_mode_accepts_release_and_updates(
    tmp_path, grok_binary, version
):
    grok_binary.with_suffix(".version").write_text(version + "\n", encoding="utf-8")
    assert provider(tmp_path, grok_binary).name == "grok"


@pytest.mark.parametrize(
    "version",
    ("0.2.110", "grok 0.2.bad", "grok-cli 1.1.7"),
)
def test_grok_official_version_mode_rejects_wrong_or_malformed_identity(
    tmp_path, grok_binary, version
):
    grok_binary.with_suffix(".version").write_text(version + "\n", encoding="utf-8")
    with pytest.raises(ProtocolError):
        provider(tmp_path, grok_binary)


def test_grok_probe_rejects_third_party_collision(tmp_path, grok_binary):
    grok_binary.with_suffix(".identity").write_text("third-party\n", encoding="utf-8")
    with pytest.raises(ProtocolError, match="required marker"):
        provider(tmp_path, grok_binary)


def test_grok_chat_stream_session_thought_drop_and_finalizer(tmp_path, grok_binary):
    instance = provider(tmp_path, grok_binary, model="grok-dynamic")
    prompt = "--not-an-option; $(echo data)\nnext"
    response = instance.chat(prompt)
    assert response.text == prompt
    assert response.session_id == "session-new"
    assert response.model == "grok-dynamic"
    assert [message.kind for message in response.messages] == [
        "text",
        "session",
        "usage",
        "done",
    ]
    assert response.usage.input_tokens == 3
    assert response.usage.cached_tokens == 1
    assert response.usage.output_tokens == 2
    assert "never expose" not in repr(response)

    messages = list(instance.stream("continued", session_id="grok:session-old"))
    assert [message.kind for message in messages] == [
        "text",
        "session",
        "usage",
        "done",
    ]
    assert messages[1].session_id == "session-old"


def test_grok_maps_official_end_usage_fields(tmp_path, grok_binary):
    response = provider(tmp_path, grok_binary).chat("usage")
    assert response.text == "usage"
    assert response.session_id == "session-new"
    assert response.usage.input_tokens == 3
    assert response.usage.cached_tokens == 1
    assert response.usage.output_tokens == 2


@pytest.mark.parametrize(
    "entry",
    (
        (".grok",),
        (".envrc",),
        (".mcp.json",),
        (".cursor", "mcp.json"),
        (".cursor", "hooks.json"),
        (".claude",),
    ),
)
def test_grok_refuses_project_runtime_configuration(
    tmp_path, grok_binary, entry
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    target = workspace.joinpath(*entry)
    if len(entry) == 1 and "." not in entry[0][1:]:
        target.mkdir()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="project tool, plugin, or hook"):
        provider(workspace, grok_binary)


def test_grok_refuses_parent_project_configuration(tmp_path, grok_binary):
    workspace = tmp_path / "repo"
    nested = workspace / "src" / "package"
    nested.mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / ".mcp.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="project tool, plugin, or hook"):
        provider(nested, grok_binary)


def test_grok_non_git_workspace_does_not_inherit_host_home_config(
    tmp_path, grok_binary
):
    host_home = tmp_path / "host-home"
    workspace = host_home / "plain-workspace"
    workspace.mkdir(parents=True)
    (host_home / ".grok").mkdir()
    provider_home = tmp_path / "isolated-provider-home"

    response = provider(
        workspace,
        grok_binary,
        provider_home=str(provider_home),
    ).chat("plain-workspace")
    assert response.text == "plain-workspace"


def _private_provider_home(path):
    path.mkdir(mode=0o700)
    grok_home = path / ".grok"
    grok_home.mkdir(mode=0o700)
    return grok_home


def test_grok_allows_isolated_auth_file_only(tmp_path, grok_binary):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    provider_home = tmp_path / "provider-home"
    grok_home = _private_provider_home(provider_home)
    auth = grok_home / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)

    response = provider(
        workspace,
        grok_binary,
        provider_home=str(provider_home),
    ).chat("auth-only")
    assert response.text == "auth-only"


def test_grok_refuses_provider_home_runtime_configuration(tmp_path, grok_binary):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    provider_home = tmp_path / "provider-home"
    grok_home = _private_provider_home(provider_home)
    config = grok_home / "config.toml"
    config.write_text("[mcp]\n", encoding="utf-8")
    config.chmod(0o600)

    with pytest.raises(ConfigurationError, match="provider-home tool, plugin, or hook"):
        provider(
            workspace,
            grok_binary,
            provider_home=str(provider_home),
        )


def _assert_all_turn_shapes_refuse(instance):
    with pytest.raises(UnifiedError, match="configuration is unavailable"):
        instance.chat("blocked")
    with pytest.raises(UnifiedError, match="configuration is unavailable"):
        list(instance.stream("blocked"))
    with pytest.raises(UnifiedError, match="configuration is unavailable"):
        asyncio.run(instance.achat("blocked"))

    async def collect():
        return [item async for item in instance.astream("blocked")]

    with pytest.raises(UnifiedError, match="configuration is unavailable"):
        asyncio.run(collect())


@pytest.mark.parametrize(
    "entry", ((".envrc",), (".mcp.json",), (".cursor", "hooks.json"))
)
def test_grok_rechecks_workspace_boundary_immediately_before_every_turn(
    tmp_path, grok_binary, entry
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    instance = provider(workspace, grok_binary)
    target = workspace.joinpath(*entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    _assert_all_turn_shapes_refuse(instance)
    assert not grok_binary.with_suffix(".prompt").exists()


@pytest.mark.parametrize(
    "entry",
    (
        (".bashrc",),
        (".bash_profile",),
        (".bash_login",),
        (".profile",),
        (".bash_logout",),
        (".grok", "config.toml"),
        (".grok", "hooks-paths"),
        (".cursor", "hooks.json"),
    ),
)
def test_grok_rechecks_provider_home_hook_sources_before_turn(
    tmp_path, grok_binary, entry
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    provider_home = tmp_path / "provider-home"
    _private_provider_home(provider_home)
    instance = provider(
        workspace,
        grok_binary,
        provider_home=str(provider_home),
    )
    target = provider_home.joinpath(*entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("exit 99\n", encoding="utf-8")

    _assert_all_turn_shapes_refuse(instance)
    assert not grok_binary.with_suffix(".prompt").exists()


def test_grok_bound_factory_and_doctor_recheck_configuration(
    tmp_path, grok_binary
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    provider_home = tmp_path / "provider-home"
    grok_home = _private_provider_home(provider_home)
    bound = PLUGIN.launch_binder(
        ProviderLaunchContextV1(
            provider_id="grok",
            bin_path=str(grok_binary),
            provider_home=str(provider_home),
        )
    )
    request = ProviderCreateRequestV1(
        provider_id="grok",
        model="grok-build",
        workspace=str(workspace),
    )
    assert bound.factory(request).chat("bound").text == "bound"
    assert bound.doctor()["available"] is True

    config = grok_home / "config.toml"
    config.write_text("[mcp]\n", encoding="utf-8")
    config.chmod(0o600)
    with pytest.raises(ConfigurationError, match="provider-home tool, plugin, or hook"):
        bound.factory(request)
    with pytest.raises(ConfigurationError, match="provider-home tool, plugin, or hook"):
        bound.doctor()


def test_grok_core_registry_uses_explicit_isolated_launch_configuration(
    monkeypatch, tmp_path, grok_binary
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    provider_home = tmp_path / "provider-home"

    class EntryPoint:
        name = "grok"
        group = core_registry.ENTRY_POINT_GROUP

        @staticmethod
        def load():
            return PLUGIN

    state_root = tmp_path / "core-settings"
    monkeypatch.setattr(core_settings, "SETTINGS_DIR", state_root)
    monkeypatch.setattr(
        core_settings,
        "SETTINGS_FILE",
        state_root / "settings.json",
    )
    monkeypatch.setattr(
        core_registry.importlib_metadata,
        "entry_points",
        lambda: [EntryPoint()],
    )
    _reset_provider_registry_for_tests()
    try:
        configure_extension_provider(
            "grok",
            ExtensionLaunchOverridesV1(
                bin_path=str(grok_binary),
                provider_home=str(provider_home),
            ),
        )
        instance = core_create("grok", cwd=str(workspace))
        assert instance.chat("through-core").text == "through-core"
        assert provider_home.is_dir()
        assert clear_extension_provider_configuration("grok") is True
    finally:
        _reset_provider_registry_for_tests()


@pytest.mark.parametrize(
    "prompt",
    (
        "malformed",
        "unknown",
        "missing-end",
        "duplicate-end",
        "after-end",
        "malformed-usage",
        "incomplete-usage",
    ),
)
def test_grok_stream_rejects_malformed_terminal_contract(
    tmp_path, grok_binary, prompt
):
    instance = provider(tmp_path, grok_binary)
    with pytest.raises(UnifiedError, match="invalid response"):
        instance.chat(prompt)


def test_grok_generic_nonzero_cancel_and_flood_guards(tmp_path, grok_binary):
    instance = provider(tmp_path, grok_binary, max_stream_events=4)
    with pytest.raises(UnifiedError, match="process failed"):
        instance.chat("nonzero")
    with pytest.raises(UnifiedError, match="configured limit"):
        instance.chat("flood")

    cancelled = threading.Event()
    caught = []

    def run():
        try:
            instance.chat("cancel", cancel_event=cancelled)
        except BaseException as error:
            caught.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.1)
    cancelled.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], UnifiedError)
    assert getattr(caught[0], "_cancelled", False) is True


def test_grok_mapper_functions_are_protocol_strict():
    from unified_cli_ext.providers.grok import _finalize, _map_record, _state

    state = _state()
    assert tuple(_map_record({"type": "thought", "data": "hidden"}, state)) == ()
    with pytest.raises(ProtocolError):
        _finalize(state)
