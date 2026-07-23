from __future__ import annotations

import subprocess

import pytest

from unified_cli_ext.providers import AdapterStatus
from unified_cli_ext.providers.copilot import (
    ADAPTER_SPEC,
    COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV,
    COPILOT_READ_ONLY_TOOLS,
    PLUGIN,
)
from unified_cli_ext.providers.held import (
    HELD_UNAVAILABLE_MESSAGE,
    HeldProviderUnavailableError,
)


def test_copilot_is_inert_held_read_only_candidate_metadata():
    assert ADAPTER_SPEC.status is AdapterStatus.HELD
    assert ADAPTER_SPEC.prompt.fixed_argv == COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV
    assert ADAPTER_SPEC.transport.value == "plain"
    assert ADAPTER_SPEC.environment.allowed_keys == frozenset(("COPILOT_HOME",))
    assert ADAPTER_SPEC.server_policy.enabled is False

    assert COPILOT_READ_ONLY_TOOLS == ("view", "glob", "grep")
    tools_index = COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV.index("--available-tools")
    assert COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV[tools_index + 1] == (
        "view,glob,grep"
    )
    for denied in ("write", "shell", "url", "memory"):
        assert "--deny-tool={}".format(denied) in COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV

    assert PLUGIN.support_status == "held"
    assert PLUGIN.default_model == "unavailable"
    assert PLUGIN.capabilities == frozenset()
    assert PLUGIN.server_policy.enabled is False
    assert PLUGIN.model_lister() == ()
    assert dict(PLUGIN.doctor()) == {
        "id": "copilot",
        "status": "Held",
        "available": False,
        "message": HELD_UNAVAILABLE_MESSAGE,
    }


def test_copilot_candidate_prompt_placement_is_static_metadata_only():
    prompt = "--not-an-option; $(touch nope)\nsecond line"
    built = ADAPTER_SPEC.prompt.build("/private/tmp/copilot", prompt)
    assert built.argv == (
        "/private/tmp/copilot",
        *COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV,
        "-p",
        prompt,
    )
    assert built.stdin_text is None


def test_copilot_held_factory_fails_before_external_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Held Copilot attempted external execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(HeldProviderUnavailableError) as caught:
        PLUGIN.factory(
            cwd="/private/tmp",
            bin_path="/private/tmp/copilot",
            provider_home="/private/tmp/copilot-home",
        )
    assert str(caught.value) == HELD_UNAVAILABLE_MESSAGE
