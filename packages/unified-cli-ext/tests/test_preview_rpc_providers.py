from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from unified_cli.errors import UnifiedError
from unified_cli_ext.providers import droid, oh_my_pi, pi
from unified_cli_ext.providers.contract import (
    AdapterStatus,
    PromptMode,
    TransportKind,
)


@dataclass(frozen=True)
class RpcPreviewCase:
    module: object
    executable: str
    transport: TransportKind
    expected_argv: tuple[str, ...]


CASES = (
    RpcPreviewCase(
        pi,
        "pi",
        TransportKind.JSONL,
        pi.PI_RPC_FIXED_ARGV,
    ),
    RpcPreviewCase(
        oh_my_pi,
        "omp",
        TransportKind.JSONL,
        oh_my_pi.OH_MY_PI_RPC_FIXED_ARGV,
    ),
    RpcPreviewCase(
        droid,
        "droid",
        TransportKind.JSON_RPC,
        droid.DROID_RPC_FIXED_ARGV,
    ),
)


@pytest.fixture
def fixture_interpreter(tmp_path: Path) -> Path:
    target = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), target)
    target.chmod(0o700)
    return target


def _fake_cli(
    tmp_path: Path,
    case: RpcPreviewCase,
    interpreter: Path,
) -> Path:
    target = tmp_path / case.executable
    log_path = target.with_suffix(".invocation.json")
    body = """\
import json
import pathlib
import sys

PROVIDER = {provider!r}
EXPECTED_ARGV = {expected_argv!r}
LOG_PATH = pathlib.Path({log_path!r})
CWD = {cwd!r}

args = sys.argv[1:]
if args == ["--version"]:
    version = {{
        "droid": "0.178.0",
        "omp": "omp/17.0.9",
        "pi": "0.81.1",
    }}[PROVIDER]
    sys.stdout.write(version + "\\n")
    raise SystemExit(0)
if PROVIDER == "droid" and args == ["exec", "--help"]:
    sys.stdout.write(
        "Usage: droid exec [options] [prompt]\\n"
        "--input-format <format>\\n"
    )
    raise SystemExit(0)
if PROVIDER in ("pi", "omp") and args == ["--help"]:
    sys.stdout.write(
        (
            "pi - AI coding assistant with read, bash, edit, write tools\\n"
            if PROVIDER == "pi"
            else "omp v17.0.9\\n"
        )
        + ("--mode <mode>\\n" if PROVIDER == "pi" else "--mode=<value>\\n")
        + ("--no-session\\n" if PROVIDER == "pi" else "--no-tools\\n")
    )
    raise SystemExit(0)
if tuple(args) != tuple(EXPECTED_ARGV):
    sys.stderr.write("unexpected argv: " + repr(args))
    raise SystemExit(19)

LOG_PATH.write_text(json.dumps({{"argv": args}}), encoding="utf-8")

def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(20)
    return json.loads(line)

def send(value):
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\\n"
    )
    sys.stdout.flush()

if PROVIDER == "pi":
    request = receive()
    assert request == {{
        "id": "unified-cli-ext-turn",
        "type": "prompt",
        "message": request["message"],
    }}
    prompt = request["message"]
    if prompt == "broken":
        sys.stdout.write("not-json\\n")
        sys.stdout.flush()
        raise SystemExit(0)
    if prompt == "no-auth":
        send({{
            "id": "unified-cli-ext-turn",
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": {pi_no_auth_error!r},
        }})
        raise SystemExit(0)
    send({{
        "id": "unified-cli-ext-turn",
        "type": "response",
        "command": "prompt",
        "success": True,
    }})
    send({{
        "type": "message_update",
        "assistantMessageEvent": {{
            "type": "text_delta",
            "delta": "pi:",
        }},
    }})
    send({{
        "type": "message_update",
        "assistantMessageEvent": {{
            "type": "text_delta",
            "delta": prompt,
        }},
    }})
    send({{"type": "agent_settled"}})

elif PROVIDER == "omp":
    send({{"type": "ready"}})
    request = receive()
    assert request == {{
        "id": "unified-cli-ext-turn",
        "type": "prompt",
        "message": request["message"],
    }}
    prompt = request["message"]
    if prompt == "broken":
        sys.stdout.write("not-json\\n")
        sys.stdout.flush()
        raise SystemExit(0)
    send({{
        "type": "message_update",
        "assistantMessageEvent": {{
            "type": "text_delta",
            "delta": "omp:",
        }},
    }})
    send({{
        "type": "message_update",
        "assistantMessageEvent": {{
            "type": "text_delta",
            "delta": prompt,
        }},
    }})
    send({{"type": "agent_end", "willRetry": False}})
    send({{
        "id": "unified-cli-ext-turn",
        "type": "response",
        "command": "prompt",
        "success": True,
    }})

else:
    initialize = receive()
    assert initialize == {{
        "jsonrpc": "2.0",
        "id": "unified-cli-ext-init",
        "method": "droid.initialize_session",
        "params": {{
            "machineId": "unified-cli-ext",
            "cwd": CWD,
            "disableBuiltinSkills": True,
        }},
    }}
    send({{
        "jsonrpc": "2.0",
        "id": "unified-cli-ext-init",
        "result": {{"sessionId": "fake-session"}},
    }})
    request = receive()
    assert request == {{
        "jsonrpc": "2.0",
        "id": "unified-cli-ext-turn",
        "method": "droid.add_user_message",
        "params": {{"text": request["params"]["text"]}},
    }}
    prompt = request["params"]["text"]
    if prompt == "broken":
        sys.stdout.write("not-json\\n")
        sys.stdout.flush()
        raise SystemExit(0)
    send({{
        "jsonrpc": "2.0",
        "id": "permission-1",
        "method": "droid.request_permission",
        "params": {{"toolUses": [], "options": []}},
    }})
    assert receive() == {{
        "jsonrpc": "2.0",
        "id": "permission-1",
        "result": {{"selectedOption": "cancel"}},
    }}
    send({{
        "jsonrpc": "2.0",
        "id": "ask-1",
        "method": "droid.ask_user",
        "params": {{"toolCallId": "tool-1", "questions": []}},
    }})
    assert receive() == {{
        "jsonrpc": "2.0",
        "id": "ask-1",
        "result": {{"cancelled": True, "answers": []}},
    }}
    send({{
        "jsonrpc": "2.0",
        "method": "droid.session_notification",
        "params": {{
            "notification": {{
                "type": "assistant_text_delta",
                "messageId": "message-1",
                "blockIndex": 0,
                "textDelta": "droid:",
            }}
        }},
    }})
    send({{
        "jsonrpc": "2.0",
        "method": "droid.session_notification",
        "params": {{
            "notification": {{
                "type": "assistant_text_delta",
                "messageId": "message-1",
                "blockIndex": 0,
                "textDelta": prompt,
            }}
        }},
    }})
    send({{
        "jsonrpc": "2.0",
        "id": "unified-cli-ext-turn",
        "result": {{}},
    }})
    send({{
        "jsonrpc": "2.0",
        "method": "droid.session_notification",
        "params": {{
            "notification": {{
                "type": "droid_working_state_changed",
                "newState": "idle",
            }}
        }},
    }})

if sys.stdin.read() != "":
    raise SystemExit(21)
""".format(
        provider=case.executable,
        expected_argv=case.expected_argv,
        log_path=str(log_path),
        cwd=str(tmp_path),
        pi_no_auth_error=pi.PI_NO_AUTH_ERROR,
    )
    target.write_text("#!{}\n{}".format(interpreter, body), encoding="utf-8")
    target.chmod(0o700)
    return target


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.module.ADAPTER_SPEC.id)
def test_preview_rpc_provider_path_selection_reaches_exact_protocol(
    tmp_path: Path,
    fixture_interpreter: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RpcPreviewCase,
) -> None:
    binary = _fake_cli(tmp_path, case, fixture_interpreter)
    monkeypatch.setenv("PATH", str(tmp_path))
    spec = case.module.ADAPTER_SPEC
    plugin = case.module.PLUGIN
    prompt = "--literal $(touch never)\nsecond line"

    assert spec.status is AdapterStatus.PREVIEW
    assert spec.prompt.mode is PromptMode.PROTOCOL
    assert spec.transport is case.transport
    assert spec.capabilities == frozenset(("chat", "stream"))
    assert spec.server_policy.enabled is False
    assert plugin.support_status == "preview"
    assert plugin.route_prefixes == (spec.id,)
    assert plugin.server_policy.enabled is False

    provider = plugin.factory(cwd=str(tmp_path))
    response = provider.chat(prompt)
    invocation = json.loads(
        binary.with_suffix(".invocation.json").read_text(encoding="utf-8")
    )

    assert invocation["argv"] == list(case.expected_argv)
    assert response.provider == spec.id
    assert response.text == "{}:{}".format(case.executable, prompt)

    messages = list(provider.stream("stream"))
    assert [item.text for item in messages if item.kind == "text"] == [
        "{}:".format(case.executable),
        "stream",
    ]
    assert messages[-1].kind == "done"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.module.ADAPTER_SPEC.id)
def test_preview_rpc_provider_malformed_protocol_is_diagnostic(
    tmp_path: Path,
    fixture_interpreter: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RpcPreviewCase,
) -> None:
    _fake_cli(tmp_path, case, fixture_interpreter)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    provider = case.module.PLUGIN.factory(cwd=str(tmp_path))

    with pytest.raises(UnifiedError) as captured:
        provider.chat("broken")

    assert "invalid response" in captured.value.message
    reports = list(
        (tmp_path / ".unified-cli" / "preview-diagnostics").iterdir()
    )
    assert len(reports) == 1
    assert "provider={}".format(case.module.ADAPTER_SPEC.id) in reports[0].read_text(
        encoding="utf-8"
    )


def test_pi_fixed_no_auth_response_maps_to_safe_core_error(
    tmp_path: Path,
    fixture_interpreter: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CASES[0]
    _fake_cli(tmp_path, case, fixture_interpreter)
    monkeypatch.setenv("PATH", str(tmp_path))
    provider = pi.PLUGIN.factory(cwd=str(tmp_path))

    with pytest.raises(UnifiedError) as captured:
        provider.chat("no-auth")

    assert captured.value.kind == "auth_expired"
    assert captured.value.provider == "pi"
    assert pi.PI_NO_AUTH_ERROR not in str(captured.value)
