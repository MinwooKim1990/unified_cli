from __future__ import annotations

from unified_cli_ext.providers import AdapterStatus
from unified_cli_ext.providers.copilot import (
    ADAPTER_SPEC,
    COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV,
    COPILOT_READ_ONLY_TOOLS,
    PLUGIN,
)


def test_copilot_is_runnable_preview_with_read_only_candidate_metadata():
    assert ADAPTER_SPEC.status is AdapterStatus.PREVIEW
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

    assert PLUGIN.support_status == "preview"
    assert PLUGIN.default_model == "auto"
    assert PLUGIN.capabilities == frozenset(("chat",))
    assert PLUGIN.server_policy.enabled is False
    assert PLUGIN.model_lister()[0].provider == "copilot"


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
