import asyncio
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import unified_cli_ext.providers.bridge as bridge_module
import unified_cli_ext.transports.process as process_transport

from unified_cli.base import BaseProvider
from unified_cli.errors import UnifiedError
from unified_cli_ext import ConfigurationError, ProtocolError
from unified_cli_ext.providers import (
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    DynamicArgument,
    EnvironmentPolicy,
    FeatureProbeSpec,
    FixedCommandSpec,
    InstallationReceiptV1,
    JsonProbeSpec,
    OperationLimits,
    PromptCommandSpec,
    PromptMode,
    PromptSentinelPolicy,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
    adapter_plugin,
)


@pytest.fixture
def adapter_binary(tmp_path):
    source = Path(__file__).parent / "fixtures" / "providers" / "fake_adapter_cli.py"
    interpreter = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    target = tmp_path / "fake-adapter"
    source_text = source.read_text(encoding="utf-8")
    _, separator, body = source_text.partition("\n")
    assert separator
    target.write_text("#!{}\n{}".format(interpreter, body), encoding="utf-8")
    target.chmod(0o700)
    return str(target)


def _command(*argv, timeout=2.0):
    return FixedCommandSpec(
        tuple(argv),
        OperationLimits(
            timeout_seconds=timeout,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=16 * 1024,
            max_events=16,
        ),
    )


def _bridge_spec(transport=TransportKind.JSONL, **changes):
    capabilities = {ProviderCapability.CHAT.value}
    dynamic = [DynamicArgument("model", "--model", required=True)]
    if transport in (TransportKind.JSON, TransportKind.JSONL):
        capabilities.add(ProviderCapability.SESSIONS.value)
        dynamic.append(DynamicArgument("session", "--session"))
    if transport is TransportKind.JSONL:
        capabilities.update(
            (ProviderCapability.STREAM.value, ProviderCapability.TOOLS.value)
        )
    mode = {
        TransportKind.PLAIN: "bridge-plain",
        TransportKind.JSON: "bridge-json",
        TransportKind.JSONL: "bridge-jsonl",
    }.get(transport, "bridge-jsonl")
    prompt = PromptCommandSpec(
        (mode,),
        dynamic_arguments=tuple(dynamic),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
        limits=OperationLimits(
            timeout_seconds=3.0,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=16 * 1024,
            max_events=16,
        ),
    )
    spec = ProviderAdapterSpecV1(
        id="fixture-provider",
        display_name="Fixture Provider",
        status=AdapterStatus.PREVIEW,
        binary=BinarySpec(
            executable="fake-adapter",
            expected_identity="fixture-provider",
            version_probe=VersionProbeSpec(
                _command("--version-json"), minimum_version=(2, 1)
            ),
            feature_probe=FeatureProbeSpec(
                _command("--bridge-features"),
                required_features=frozenset(capabilities),
            ),
        ),
        prompt=prompt,
        transport=transport,
        environment=EnvironmentPolicy(
            allowed_keys=frozenset(("SAFE_BRIDGE",))
        ),
        doctor=DoctorProbeSpec(
            JsonProbeSpec(
                _command("--doctor-json"),
                expected={"provider": "fixture-provider"},
            )
        ),
        capabilities=frozenset(capabilities),
    )
    return replace(spec, **changes) if changes else spec


def _json_mapper(raw, state):
    state["calls"] = state.get("calls", 0) + 1
    return (
        {"type": "session", "session_id": raw["session"]},
        {"type": "text_final", "text": raw["answer"]},
        {
            "type": "usage",
            "input_tokens": raw["input_tokens"],
            "output_tokens": raw["output_tokens"],
        },
        {"type": "done", "reason": "complete"},
    )


def _record_mapper(raw, state):
    state["records"] = state.get("records", 0) + 1
    kind = raw["kind"]
    if kind == "session":
        return ({"type": "session", "session_id": raw["id"]},)
    if kind == "partial":
        return ({"type": "text_partial", "text": raw["value"]},)
    if kind == "delta":
        return ({"type": "text_delta", "text": raw["value"]},)
    if kind == "final":
        return ({"type": "text_final", "text": raw["value"]},)
    if kind == "tool-start":
        return (
            {
                "type": "tool_start",
                "tool_id": raw["id"],
                "name": raw["name"],
                "arguments": raw.get("arguments", {}),
            },
        )
    if kind == "tool-progress":
        return (
            {
                "type": "tool_progress",
                "tool_id": raw["id"],
                "message": "working",
                "progress": raw["value"],
            },
        )
    if kind == "tool-result":
        return (
            {
                "type": "tool_result",
                "tool_id": raw["id"],
                "result": raw["result"],
            },
        )
    if kind == "usage":
        return (
            {
                "type": "usage",
                "input_tokens": raw["input"],
                "output_tokens": raw["output"],
                "cached_input_tokens": raw["cached"],
            },
        )
    if kind == "error":
        return (
            {
                "type": "error",
                "code": raw["code"],
                "message": raw["message"],
            },
        )
    if kind == "done":
        return ({"type": "done", "reason": "complete"},)
    if kind == "mapper-failure":
        raise RuntimeError(raw["secret"])
    return ({"type": "vendor_unknown", "value": raw},)


def _provider(
    tmp_path,
    adapter_binary,
    *,
    transport=TransportKind.JSONL,
    spec=None,
    finalize=None,
    **factory_options
):
    selected = spec or _bridge_spec(transport)
    plugin = adapter_plugin(
        selected,
        default_model="fixture-model",
        map_record=_record_mapper if transport is TransportKind.JSONL else None,
        map_response=_json_mapper if transport is TransportKind.JSON else None,
        state_factory=dict,
        finalize=finalize,
    )
    return plugin.factory(
        cwd=str(tmp_path),
        bin_path=adapter_binary,
        extra_env={
            "SAFE_BRIDGE": "enabled",
            "UNDECLARED_SECRET": "must-not-be-forwarded",
        },
        **factory_options
    )


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(*pids):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(_pid_exists(pid) for pid in pids):
            return
        time.sleep(0.02)
    assert not any(_pid_exists(pid) for pid in pids)


def test_plugin_is_lazy_requires_verified_launch_and_held_stays_inert(
    tmp_path, adapter_binary
):
    calls = []
    receipt = InstallationReceiptV1.capture_direct(
        provider_id="fixture-provider",
        executable_path=adapter_binary,
        executable_basename="fake-adapter",
        distribution_name="fixture-provider",
        distribution_version="2.4.1",
        acquisition_source="offline-test",
    )
    plugin = adapter_plugin(
        _bridge_spec(),
        default_model="fixture-model",
        launch_resolver=lambda: calls.append(True) or receipt,
        map_record=_record_mapper,
    )
    assert calls == []
    provider = plugin.factory(cwd=str(tmp_path))
    assert calls == [True]
    assert isinstance(provider, BaseProvider)
    assert provider.name == "fixture-provider"

    no_resolver = adapter_plugin(
        _bridge_spec(),
        default_model="fixture-model",
        map_record=_record_mapper,
    )
    with pytest.raises(ConfigurationError, match="canonical bin_path"):
        no_resolver.factory(cwd=str(tmp_path))

    held = adapter_plugin(
        replace(_bridge_spec(), status=AdapterStatus.HELD),
        default_model="ignored",
    )
    assert held.support_status == "held"
    with pytest.raises(ConfigurationError, match="unavailable"):
        held.factory(cwd=str(tmp_path), bin_path=adapter_binary)


def test_mapper_and_transport_capabilities_fail_closed():
    with pytest.raises(ConfigurationError, match="model id"):
        adapter_plugin(_bridge_spec(), default_model="invalid\nmodel")
    with pytest.raises(ConfigurationError, match="requires map_record"):
        adapter_plugin(_bridge_spec(), default_model="fixture-model")
    with pytest.raises(ConfigurationError, match="requires map_response"):
        adapter_plugin(
            _bridge_spec(TransportKind.JSON), default_model="fixture-model"
        )
    with pytest.raises(ConfigurationError, match="lifecycle"):
        adapter_plugin(
            _bridge_spec(TransportKind.JSON_RPC),
            default_model="fixture-model",
            map_record=_record_mapper,
        )


def test_plain_and_json_one_shot_sync_async_parity(tmp_path, adapter_binary):
    plain = _provider(
        tmp_path, adapter_binary, transport=TransportKind.PLAIN
    )
    response = plain.chat("hello")
    assert response.text == "plain:hello"
    assert response.model == "fixture-model"
    assert [item.kind for item in response.messages] == ["text", "done"]
    assert [item.text for item in plain.stream("world") if item.kind == "text"] == [
        "plain:world"
    ]

    json_provider = _provider(
        tmp_path, adapter_binary, transport=TransportKind.JSON
    )
    response = json_provider.chat("hello-json", session_id="json-session")
    assert response.text == "json:hello-json"
    assert response.session_id == "json-session"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 5

    async def exercise():
        async_response = await json_provider.achat(
            "async-json", session_id="json-session"
        )
        events = []
        async for event in plain.astream("async-plain"):
            events.append(event)
        return async_response, events

    async_response, events = asyncio.run(exercise())
    assert async_response.text == "json:async-json"
    assert [item.text for item in events if item.kind == "text"] == [
        "plain:async-plain"
    ]


def test_jsonl_normalization_deduplicates_and_correlates_turn(
    tmp_path, adapter_binary
):
    provider = _provider(tmp_path, adapter_binary)
    events = list(provider.stream("normal", session_id="requested-session"))
    assert [item.text for item in events if item.kind == "text"] == ["Hel", "lo"]
    assert [item.session_id for item in events if item.kind == "session"] == [
        "requested-session"
    ]
    assert [item.kind for item in events if item.kind.startswith("tool_")] == [
        "tool_use",
        "tool_result",
    ]
    tool_start = next(item for item in events if item.kind == "tool_use")
    assert type(tool_start.tool["input"]["query"]) is list
    tool_result = next(item for item in events if item.kind == "tool_result")
    assert type(tool_result.tool["output"]["items"]) is list
    usage = [item.usage for item in events if item.kind == "usage"][-1]
    assert usage.input_tokens == 7
    assert usage.output_tokens == 11
    assert usage.cached_tokens == 2
    assert events[-1].kind == "done"

    response = provider.chat("normal")
    assert response.text == "Hello"
    assert response.session_id == "stream-session"
    assert response.raw

    async def exercise():
        result = []
        async for event in provider.astream("normal"):
            result.append(event)
        return result

    async_events = asyncio.run(exercise())
    assert [item.text for item in async_events if item.kind == "text"] == [
        "Hel",
        "lo",
    ]
    assert async_events[-1].kind == "done"


@pytest.mark.parametrize("second_session", ("requested-session", "other-session"))
def test_turn_state_rejects_every_second_session_event(second_session):
    turn = bridge_module._TurnState(
        "fixture-provider",
        frozenset((ProviderCapability.SESSIONS.value,)),
        {},
        "requested-session",
        4,
        4096,
    )
    messages = turn.accept_record(
        {"type": "session", "session_id": "requested-session"}
    )
    assert [message.session_id for message in messages] == ["requested-session"]

    with pytest.raises(ProtocolError, match="session"):
        turn.accept_record({"type": "session", "session_id": second_session})

    exact = bridge_module._TurnState(
        "fixture-provider",
        frozenset((ProviderCapability.SESSIONS.value,)),
        {},
        "requested-session",
        4,
        4096,
    )
    exact.accept_record({"type": "session", "session_id": "requested-session"})
    exact.accept_record({"type": "done", "reason": "complete"})
    exact.finish()


@pytest.mark.parametrize(
    "prompt",
    ("malformed", "unknown", "mapper-failure:credential-value", "unfinished-tool"),
)
def test_malformed_mapper_and_lifecycle_errors_are_sanitized(
    tmp_path, adapter_binary, prompt
):
    provider = _provider(tmp_path, adapter_binary)
    with pytest.raises(UnifiedError, match="config"):
        provider.chat("normal", model="invalid\nmodel")
    with pytest.raises(UnifiedError) as caught:
        provider.chat(prompt)
    rendered = str(caught.value)
    assert "credential-value" not in rendered
    assert "UNDECLARED_SECRET" not in rendered
    assert caught.value.kind == "internal"


def test_error_event_cleans_up_and_never_records_success(
    tmp_path, adapter_binary, monkeypatch
):
    provider = _provider(tmp_path, adapter_binary)
    secret = "error-secret:oauth-token-value"
    usage_records = []
    monkeypatch.setattr(
        bridge_module._usage_tracker,
        "record",
        lambda *args, **kwargs: usage_records.append((args, kwargs)),
    )

    with pytest.raises(UnifiedError, match="Provider reported an error"):
        provider.chat(secret)

    events = []
    with pytest.raises(UnifiedError, match="Provider reported an error"):
        events.extend(provider.stream(secret))
    assert [item.kind for item in events[-2:]] == ["error", "done"]
    assert events[-2].error == "Provider reported an error."
    assert secret not in repr(events)

    async def exercise():
        with pytest.raises(UnifiedError, match="Provider reported an error"):
            await provider.achat(secret)
        async_events = []
        with pytest.raises(UnifiedError, match="Provider reported an error"):
            async for item in provider.astream(secret):
                async_events.append(item)
        return async_events

    async_events = asyncio.run(exercise())
    assert [item.kind for item in async_events[-2:]] == ["error", "done"]
    assert len(usage_records) == 4
    assert all(item[1].get("error_kind") == "internal" for item in usage_records)

    with pytest.raises(UnifiedError, match="invalid response"):
        provider.chat("error-after")


def test_clean_eof_requires_explicit_bounded_finalizer(tmp_path, adapter_binary):
    calls = []

    def finalize(state):
        calls.append(dict(state))
        return ({"type": "done", "reason": "clean_eof"},)

    provider = _provider(tmp_path, adapter_binary, finalize=finalize)
    assert provider.chat("clean-eof").text == "clean eof"
    assert len(calls) == 1

    async def collect_clean_eof():
        return [item async for item in provider.astream("clean-eof")]

    async_events = asyncio.run(collect_clean_eof())
    assert async_events[-1].kind == "done"
    assert len(calls) == 2

    strict = _provider(tmp_path, adapter_binary)
    with pytest.raises(UnifiedError, match="invalid response"):
        strict.chat("clean-eof")

    before = len(calls)
    with pytest.raises(UnifiedError, match="process failed"):
        provider.chat("unclean-eof")
    assert len(calls) == before

    flooding = _provider(
        tmp_path,
        adapter_binary,
        finalize=lambda _state: (
            {"type": "done", "reason": "clean_eof"} for _ in range(32)
        ),
    )
    with pytest.raises(UnifiedError, match="limit"):
        flooding.chat("clean-eof")


def test_flood_and_session_mismatch_fail_closed(tmp_path, adapter_binary):
    provider = _provider(
        tmp_path,
        adapter_binary,
        max_stream_events=8,
        max_output_bytes=4096,
    )
    with pytest.raises(UnifiedError, match="limit"):
        list(provider.stream("flood"))
    with pytest.raises(UnifiedError, match="invalid response"):
        provider.chat("session-mismatch", session_id="expected-session")
    with pytest.raises(UnifiedError, match="invalid response"):
        provider.chat("missing-session", session_id="expected-session")
    with pytest.raises(UnifiedError, match="invalid response"):
        provider.chat("text-after-final")


@pytest.mark.parametrize(
    "prompt", ("duplicate-session-same", "duplicate-session-conflict")
)
def test_duplicate_session_events_fail_sync_and_async_surfaces(
    tmp_path, adapter_binary, prompt
):
    provider = _provider(tmp_path, adapter_binary)

    with pytest.raises(UnifiedError, match="invalid response"):
        provider.chat(prompt, session_id="requested-session")

    sync_events = []
    with pytest.raises(UnifiedError, match="invalid response"):
        sync_events.extend(
            provider.stream(prompt, session_id="requested-session")
        )
    assert [item.session_id for item in sync_events if item.kind == "session"] == [
        "requested-session"
    ]

    async def exercise():
        with pytest.raises(UnifiedError, match="invalid response"):
            await provider.achat(prompt, session_id="requested-session")
        async_events = []
        with pytest.raises(UnifiedError, match="invalid response"):
            async for item in provider.astream(
                prompt, session_id="requested-session"
            ):
                async_events.append(item)
        return async_events

    async_events = asyncio.run(exercise())
    assert [item.session_id for item in async_events if item.kind == "session"] == [
        "requested-session"
    ]


def test_sync_cancel_and_generator_abort_clean_process_tree(tmp_path, adapter_binary):
    provider = _provider(tmp_path, adapter_binary)
    pid_file = tmp_path / "hang.pid"
    cancelled = threading.Event()
    stream = provider.stream("hang:{}".format(pid_file), cancel_event=cancelled)
    assert next(stream).text == "waiting"
    pid = int(pid_file.read_text(encoding="utf-8"))
    cancelled.set()
    with pytest.raises(UnifiedError) as caught:
        next(stream)
    assert getattr(caught.value, "_cancelled", False)
    _wait_for_exit(pid)

    descendant_file = tmp_path / "descendant.pid"
    stream = provider.stream("descendant:{}".format(descendant_file))
    assert next(stream).text == "spawned"
    pids = tuple(
        int(value)
        for value in descendant_file.read_text(encoding="utf-8").split()
    )
    stream.close()
    _wait_for_exit(*pids)


def test_async_cancel_cleans_process_tree(tmp_path, adapter_binary):
    provider = _provider(tmp_path, adapter_binary)
    pid_file = tmp_path / "async-hang.pid"

    async def exercise():
        stream = provider.astream("hang:{}".format(pid_file))
        first = await stream.__anext__()
        assert first.text == "waiting"
        pending = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()

    asyncio.run(exercise())
    _wait_for_exit(int(pid_file.read_text(encoding="utf-8")))


def test_doctor_must_be_healthy_before_factory_returns(tmp_path, adapter_binary):
    unhealthy = replace(
        _bridge_spec(),
        doctor=DoctorProbeSpec(
            JsonProbeSpec(
                _command("--doctor-false-json"),
                expected={"provider": "fixture-provider"},
            )
        ),
    )
    plugin = adapter_plugin(
        unhealthy,
        default_model="fixture-model",
        map_record=_record_mapper,
    )
    with pytest.raises(ConfigurationError, match="doctor"):
        plugin.factory(cwd=str(tmp_path), bin_path=adapter_binary)


def test_undeclared_model_and_unsupported_core_options_fail_closed(
    tmp_path, adapter_binary
):
    spec = _bridge_spec()
    fixed_model_spec = replace(
        spec,
        prompt=replace(spec.prompt, dynamic_arguments=()),
    )
    plugin = adapter_plugin(
        fixed_model_spec,
        default_model="fixture-model",
        map_record=_record_mapper,
    )
    with pytest.raises(ConfigurationError, match="model argument"):
        plugin.factory(
            cwd=str(tmp_path),
            bin_path=adapter_binary,
            model="invented-model",
        )

    provider = plugin.factory(
        cwd=str(tmp_path),
        bin_path=adapter_binary,
        model="fixture-model",
    )
    assert provider.chat("normal").model == "fixture-model"
    with pytest.raises(UnifiedError, match="config"):
        provider.chat("normal", model="invented-model")

    dynamic = adapter_plugin(
        spec,
        default_model="fixture-model",
        map_record=_record_mapper,
    )
    with pytest.raises(ConfigurationError, match="first_output_timeout"):
        dynamic.factory(
            cwd=str(tmp_path),
            bin_path=adapter_binary,
            first_output_timeout=0.5,
        )
    with pytest.raises(ConfigurationError, match="web search"):
        dynamic.factory(
            cwd=str(tmp_path),
            bin_path=adapter_binary,
            web_search=True,
        )


def test_fixed_model_issuance_rejects_public_mutation_before_spawn(
    tmp_path, adapter_binary, monkeypatch
):
    spec = _bridge_spec()
    fixed_model_spec = replace(
        spec,
        prompt=replace(spec.prompt, dynamic_arguments=()),
    )
    provider = _provider(
        tmp_path,
        adapter_binary,
        spec=fixed_model_spec,
    )
    starts = []
    usage_records = []

    def forbidden_popen(*args, **kwargs):
        starts.append((args, kwargs))
        raise AssertionError("model validation must precede process launch")

    monkeypatch.setattr(process_transport.subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(
        bridge_module._usage_tracker,
        "record",
        lambda *args, **kwargs: usage_records.append((args, kwargs)),
    )

    provider.model = "invented-model"
    with pytest.raises(UnifiedError, match="config"):
        provider.chat("normal")
    with pytest.raises(UnifiedError, match="config"):
        list(provider.stream("normal"))

    async def exercise_model_mutation():
        with pytest.raises(UnifiedError, match="config"):
            await provider.achat("normal")
        with pytest.raises(UnifiedError, match="config"):
            async for _item in provider.astream("normal"):
                pass

    asyncio.run(exercise_model_mutation())

    provider.model = "fixture-model"
    provider.default_model = "changed-default"
    with pytest.raises(UnifiedError, match="config"):
        provider.chat("normal")
    provider.default_model = "fixture-model"
    provider.name = "spoofed-provider"
    with pytest.raises(UnifiedError, match="config") as caught:
        provider.chat("normal")

    assert caught.value.provider == "fixture-provider"
    assert starts == []
    assert len(usage_records) == 6
    assert all(
        record[0][:2] == ("fixture-provider", "fixture-model")
        for record in usage_records
    )
    assert all(record[1].get("error_kind") == "internal" for record in usage_records)


def test_declared_dynamic_model_mutation_is_routed_and_reported_truthfully(
    tmp_path, adapter_binary, monkeypatch
):
    provider = _provider(tmp_path, adapter_binary)
    usage_records = []
    monkeypatch.setattr(
        bridge_module._usage_tracker,
        "record",
        lambda *args, **kwargs: usage_records.append((args, kwargs)),
    )
    provider.model = "dynamic-model"

    response = provider.chat("model-echo")
    assert (response.text, response.model) == ("dynamic-model", "dynamic-model")
    assert [item.text for item in provider.stream("model-echo") if item.kind == "text"] == [
        "dynamic-model"
    ]

    async def exercise():
        async_response = await provider.achat("model-echo")
        async_events = [item async for item in provider.astream("model-echo")]
        return async_response, async_events

    async_response, async_events = asyncio.run(exercise())
    assert (async_response.text, async_response.model) == (
        "dynamic-model",
        "dynamic-model",
    )
    assert [item.text for item in async_events if item.kind == "text"] == [
        "dynamic-model"
    ]

    overridden = provider.chat("model-echo", model="per-call-model")
    assert (overridden.text, overridden.model) == (
        "per-call-model",
        "per-call-model",
    )
    assert [record[0][1] for record in usage_records] == [
        "dynamic-model",
        "dynamic-model",
        "dynamic-model",
        "dynamic-model",
        "per-call-model",
    ]


def _npm_receipt(tmp_path):
    source = Path(__file__).parent / "fixtures" / "providers" / "fake_adapter_cli.py"
    ownership = tmp_path / "installation"
    package = ownership / "package"
    package.mkdir(parents=True, mode=0o700)
    interpreter = ownership / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    target = package / "cli.py"
    _, separator, body = source.read_text(encoding="utf-8").partition("\n")
    assert separator
    target.write_text("#!{}\n{}".format(interpreter, body), encoding="utf-8")
    target.chmod(0o700)
    (package / "package.json").write_text(
        '{"name":"@fixture/provider","version":"2.4.1",'
        '"bin":{"fake-adapter":"cli.py"}}',
        encoding="utf-8",
    )
    launcher = ownership / "fake-adapter"
    launcher.symlink_to(target)
    receipt = InstallationReceiptV1.capture_npm(
        provider_id="fixture-provider",
        launcher_path=str(launcher),
        executable_basename="fake-adapter",
        package_root=str(package),
        ownership_root=str(ownership),
        distribution_name="@fixture/provider",
        distribution_version="2.4.1",
        acquisition_source="offline-test",
        interpreter_path=str(interpreter),
    )
    return receipt, package / "package.json"


@pytest.mark.parametrize("transport", (TransportKind.JSON, TransportKind.JSONL))
def test_npm_script_swap_at_popen_is_blocked_before_execution(
    tmp_path, monkeypatch, transport
):
    receipt, _manifest = _npm_receipt(tmp_path)
    plugin = adapter_plugin(
        _bridge_spec(transport),
        default_model="fixture-model",
        map_record=_record_mapper if transport is TransportKind.JSONL else None,
        map_response=_json_mapper if transport is TransportKind.JSON else None,
    )
    provider = plugin.factory(cwd=str(tmp_path), receipt=receipt)
    marker = tmp_path / "replacement-executed"
    target = Path(receipt.canonical_launch_target)
    interpreter = receipt.argv_prefix[0]
    original_popen = process_transport.subprocess.Popen
    raced = []

    def swap_then_spawn(argv, *args, **kwargs):
        if not raced:
            raced.append(True)
            target.write_text(
                "#!{}\nfrom pathlib import Path\n"
                "Path({!r}).write_text('executed', encoding='utf-8')\n".format(
                    interpreter, str(marker)
                ),
                encoding="utf-8",
            )
            target.chmod(0o700)
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(process_transport.subprocess, "Popen", swap_then_spawn)
    with pytest.raises(UnifiedError) as caught:
        provider.chat("race", session_id="json-session")
    assert caught.value.kind == "config"
    assert raced == [True]
    assert not marker.exists()


def test_npm_receipt_prefix_runs_and_is_reverified_before_prompt(tmp_path):
    receipt, manifest = _npm_receipt(tmp_path)
    verified_launch = receipt.verify()
    plugin = adapter_plugin(
        _bridge_spec(TransportKind.JSON),
        default_model="fixture-model",
        map_response=_json_mapper,
    )
    provider = plugin.factory(cwd=str(tmp_path), receipt=receipt)
    assert provider.chat("npm", session_id="json-session").text == "json:npm"
    with pytest.raises(ConfigurationError, match="receipt"):
        plugin.factory(cwd=str(tmp_path), receipt=verified_launch)

    manifest.write_text(
        '{"name":"@fixture/provider","version":"9.9.9",'
        '"bin":{"fake-adapter":"cli.py"}}',
        encoding="utf-8",
    )
    with pytest.raises(UnifiedError) as caught:
        provider.chat("changed", session_id="json-session")
    assert caught.value.kind == "config"
