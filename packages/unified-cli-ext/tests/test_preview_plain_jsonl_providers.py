from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from unified_cli_ext.providers import (
    amp,
    cline,
    codebuddy,
    copilot,
    cursor,
    gitlab_duo,
    kimi,
    mistral_vibe,
    opencode,
    qwen,
)
from unified_cli_ext.providers.contract import (
    AdapterStatus,
    PromptMode,
    TransportKind,
)


@dataclass(frozen=True)
class PreviewCase:
    module: object
    expected_argv: tuple[str, ...]
    stdin_prompt: bool = False


CASES = (
    PreviewCase(kimi, (*kimi.KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV, "-p")),
    PreviewCase(
        copilot,
        (*copilot.COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV, "-p"),
    ),
    PreviewCase(
        cursor,
        cursor.CURSOR_DOCUMENTED_PRINT_OPTIONS,
        stdin_prompt=True,
    ),
    PreviewCase(
        codebuddy,
        (*codebuddy.CODEBUDDY_HEADLESS_FIXED_ARGV, "-p"),
    ),
    PreviewCase(
        mistral_vibe,
        (*mistral_vibe.MISTRAL_VIBE_HEADLESS_FIXED_ARGV, "--prompt"),
    ),
    PreviewCase(qwen, (*qwen.QWEN_HEADLESS_FIXED_ARGV, "--prompt")),
    PreviewCase(cline, (*cline.CLINE_HEADLESS_FIXED_ARGV, "--")),
    PreviewCase(opencode, (*opencode.OPENCODE_HEADLESS_FIXED_ARGV, "--")),
    PreviewCase(amp, amp.AMP_HEADLESS_FIXED_ARGV, stdin_prompt=True),
    PreviewCase(
        gitlab_duo,
        (*gitlab_duo.GITLAB_DUO_HEADLESS_FIXED_ARGV, "--goal"),
    ),
)


def _fake_cli(
    tmp_path: Path,
    case: PreviewCase,
    interpreter: Path,
) -> Path:
    spec = case.module.ADAPTER_SPEC
    version_marker = spec.binary.version_probe.version_marker
    feature_marker = spec.binary.feature_probe.feature_markers["chat"]
    prompt_option = spec.prompt.prompt_option
    target = tmp_path / spec.binary.executable
    log_path = target.with_suffix(".invocation.json")
    body = """\
import json
import pathlib
import sys

VERSION_MARKER = {version_marker!r}
FEATURE_MARKER = {feature_marker!r}
PROMPT_OPTION = {prompt_option!r}
LOG_PATH = pathlib.Path({log_path!r})

args = sys.argv[1:]
stdin_text = sys.stdin.read()
if args == ["--version"]:
    sys.stdout.write(VERSION_MARKER + "1.2.3\\n")
    raise SystemExit(0)
if "--help" in args or args == ["help"]:
    sys.stdout.write(FEATURE_MARKER + "\\n")
    raise SystemExit(0)

if PROMPT_OPTION is not None:
    index = args.index(PROMPT_OPTION)
    prompt = args[index + 1]
elif "--" in args:
    prompt = args[args.index("--") + 1]
else:
    prompt = stdin_text
LOG_PATH.write_text(
    json.dumps({{"argv": args, "stdin": stdin_text}}),
    encoding="utf-8",
)
sys.stdout.write(prompt)
""".format(
        version_marker=version_marker,
        feature_marker=feature_marker,
        prompt_option=prompt_option,
        log_path=str(log_path),
    )
    target.write_text("#!{}\n{}".format(interpreter, body), encoding="utf-8")
    target.chmod(0o700)
    return target


@pytest.fixture
def fixture_interpreter(tmp_path: Path) -> Path:
    target = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), target)
    target.chmod(0o700)
    return target


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.module.ADAPTER_SPEC.id)
def test_preview_plain_provider_factory_reaches_process_and_preserves_prompt(
    tmp_path: Path,
    fixture_interpreter: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewCase,
) -> None:
    spec = case.module.ADAPTER_SPEC
    plugin = case.module.PLUGIN
    binary = _fake_cli(tmp_path, case, fixture_interpreter)
    monkeypatch.setenv("PATH", str(tmp_path))
    prompt = "--literal $(touch never)\nsecond line"

    assert spec.status is AdapterStatus.PREVIEW
    assert spec.transport is TransportKind.PLAIN
    assert spec.capabilities == frozenset(("chat",))
    assert spec.server_policy.enabled is False
    assert plugin.support_status == "preview"
    assert plugin.server_policy.enabled is False

    provider = plugin.factory(cwd=str(tmp_path))
    response = provider.chat(prompt)
    invocation = json.loads(
        binary.with_suffix(".invocation.json").read_text(encoding="utf-8")
    )

    expected = list(case.expected_argv)
    if not case.stdin_prompt:
        expected.append(prompt)
    assert invocation["argv"] == expected
    assert invocation["stdin"] == (prompt if case.stdin_prompt else "")
    assert response.text == prompt
    if case.stdin_prompt:
        assert spec.prompt.mode is PromptMode.STDIN
