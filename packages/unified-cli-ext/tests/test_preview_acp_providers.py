from __future__ import annotations

import os

import pytest

from unified_cli_ext.normalization import (
    DoneEvent,
    SessionEvent,
    SessionRef,
    TextDeltaEvent,
)
from unified_cli_ext.providers import hermes, kilo, poolside, qoder
from unified_cli_ext.providers.contract import AdapterStatus


PROVIDERS = (
    (
        qoder,
        "qodercli",
        "1.1.4",
        "Usage: qodercli\n--acp\nAgent Client Protocol\n--permission-mode",
        ("--acp", "--permission-mode", "dont_ask"),
        {},
    ),
    (
        kilo,
        "kilo",
        "7.4.15",
        "kilo acp\nstart ACP\n--hostname\n--port",
        ("acp", "--hostname", "127.0.0.1", "--port", "0"),
        {
            "KILO_PURE": "1",
            "KILO_NO_DAEMON": "1",
            "KILO_CONFIG_CONTENT": (
                '{"mcp":{},"plugin":[],"snapshot":false,'
                '"permission":{"*":"deny"}}'
            ),
        },
    ),
    (
        poolside,
        "pool",
        "pool 1.0.13",
        "pool acp\nAgent Client Protocol\nstandard input\n--version",
        ("acp",),
        {},
    ),
    (
        hermes,
        "hermes",
        "Hermes Agent v0.19.0 (2026-07-01)",
        (
            "usage: hermes acp\n"
            "Start Hermes Agent in ACP mode\n"
            "--check\n"
            "--version"
        ),
        ("acp",),
        {
            "HERMES_IGNORE_RULES": "1",
            "HERMES_IGNORE_USER_CONFIG": "1",
        },
    ),
)


def _official_cli_fixture(
    tmp_path,
    name: str,
    version: str,
    help_text: str,
    *,
    help_to_stderr: bool = False,
):
    executable = tmp_path / name
    quoted_help = " ".join(
        "'{}'".format(line.replace("'", "'\\''"))
        for line in help_text.splitlines()
    )
    quoted_version = "'{}'".format(version.replace("'", "'\\''"))
    executable.write_text(
        "#!{}\n".format(os.path.realpath("/bin/sh"))
        + 'case "$*" in\n'
        + '  *"--help"*) printf "%s\\n" '
        + quoted_help
        + (" >&2" if help_to_stderr else "")
        + " ;;\n"
        + '  *) printf "%s\\n" '
        + quoted_version
        + " ;;\n"
        + "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(
    ("module", "name", "version", "help_text", "argv", "provider_env"),
    PROVIDERS,
)
def test_preview_metadata_is_opt_in_and_server_closed(
    module,
    name,
    version,
    help_text,
    argv,
    provider_env,
):
    del version, help_text, provider_env
    assert module.ADAPTER_SPEC.status is AdapterStatus.PREVIEW
    assert module.ADAPTER_SPEC.binary.executable == name
    assert module.ADAPTER_SPEC.prompt.fixed_argv == argv
    assert module.PLUGIN.support_status == "preview"
    assert module.PLUGIN.route_prefixes == (module.ADAPTER_SPEC.id,)
    assert module.PLUGIN.capabilities == frozenset(("chat",))
    assert module.PLUGIN.server_policy.enabled is False


@pytest.mark.parametrize(
    ("module", "name", "version", "help_text", "argv", "provider_env"),
    PROVIDERS,
)
def test_path_selection_reaches_the_official_sdk_process_boundary(
    monkeypatch,
    tmp_path,
    module,
    name,
    version,
    help_text,
    argv,
    provider_env,
):
    executable = _official_cli_fixture(
        tmp_path,
        name,
        version,
        help_text,
        help_to_stderr=module.ADAPTER_SPEC.binary.feature_probe.use_stderr,
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    captured = {}

    class FakeOfficialSdkProcess:
        def __init__(self, process_argv, **kwargs):
            captured["argv"] = tuple(process_argv)
            captured["kwargs"] = kwargs

        async def text_turn(self, prompt):
            captured["prompt"] = prompt
            return (
                SessionEvent(
                    SessionRef(module.ADAPTER_SPEC.id, "official-session")
                ),
                TextDeltaEvent("official reply", "message-1"),
                DoneEvent("end_turn"),
            )

    monkeypatch.setattr(
        "unified_cli_ext.providers.acp_bridge.AcpProcessTransportV1",
        FakeOfficialSdkProcess,
    )
    home = tmp_path / "{}-home".format(name)
    provider = module.PLUGIN.factory(
        cwd=str(tmp_path),
        provider_home=str(home),
    )
    response = provider.chat("hello")

    assert response.text == "official reply"
    assert response.session_id == "official-session"
    assert captured["prompt"] == "hello"
    assert captured["argv"] == (str(executable),) + argv
    assert captured["kwargs"]["provider_namespace"] == module.ADAPTER_SPEC.id
    assert captured["kwargs"]["persistent_home"] == str(home)
    assert captured["kwargs"]["provider_env"] == provider_env
