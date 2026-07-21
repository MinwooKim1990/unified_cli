import asyncio
import os
import signal
import threading
import time

import pytest

from unified_cli_ext import (
    CancellationToken,
    ConfigurationError,
    JsonRpcProcessClient as _JsonRpcProcessClient,
    JsonlProcess as _JsonlProcess,
    LimitExceeded,
    ProcessFailed,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportLimits,
    TransportTimeout,
)
from unified_cli_ext.transports.security import ExecutableIdentity


def argv(fake_cli, mode):
    return [fake_cli, mode]


def _identity_bound(constructor, argv_value, *args, **kwargs):
    if (
        "executable_identity" not in kwargs
        and not isinstance(argv_value, (str, bytes))
        and argv_value
    ):
        kwargs["executable_identity"] = ExecutableIdentity.capture(argv_value[0])
    return constructor(argv_value, *args, **kwargs)


def JsonlProcess(argv_value, *args, **kwargs):
    return _identity_bound(_JsonlProcess, argv_value, *args, **kwargs)


def JsonRpcProcessClient(argv_value, *args, **kwargs):
    return _identity_bound(_JsonRpcProcessClient, argv_value, *args, **kwargs)


def pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for_exit(*pids):
    deadline = time.monotonic() + 2
    while any(pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not pid_exists(pid) for pid in pids)


def terminate_exact_pid(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    wait_for_exit(pid)


def test_jsonl_sync_and_async_parity(fake_cli):
    sync_values = list(JsonlProcess(argv(fake_cli, "events")).iter_messages())

    async def collect():
        return [item async for item in JsonlProcess(argv(fake_cli, "events")).aiter_messages()]

    assert asyncio.run(collect()) == sync_values


def test_jsonl_bidirectional_and_argv_rejects_shell_string(fake_cli):
    with pytest.raises(Exception):
        JsonlProcess("echo unsafe")
    with JsonlProcess(argv(fake_cli, "echo")) as process:
        process.send({"safe": ["a", "b"]})
        assert process.receive() == {"safe": ["a", "b"]}


def test_environment_is_minimal_and_provider_key_must_be_explicit(fake_cli):
    process = JsonlProcess(
        argv(fake_cli, "env"),
        provider_env={"EXPLICIT_VALUE": "ok"},
        allowed_provider_env=["EXPLICIT_VALUE"],
    )
    env = list(process.iter_messages())[0]["env"]
    assert env["EXPLICIT_VALUE"] == "ok"
    assert set(env) <= {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "EXPLICIT_VALUE",
        # macOS injects this non-secret locale/terminal encoding hint into a
        # child even when it is absent from the explicit Popen environment.
        "__CF_USER_TEXT_ENCODING",
    }
    assert env["HOME"] != os.environ.get("HOME")
    with pytest.raises(Exception):
        JsonlProcess(argv(fake_cli, "events"), provider_env={"SECRET": "x"}).start()
    with pytest.raises(Exception):
        JsonlProcess(
            argv(fake_cli, "events"),
            provider_env={"HOME": "/unsafe"},
            allowed_provider_env=["HOME"],
        ).start()


def test_outbound_json_is_strict_and_bounded(fake_cli):
    with JsonlProcess(argv(fake_cli, "echo")) as process:
        with pytest.raises(ProtocolError):
            process.send({"value": float("nan")})
        with pytest.raises(ProtocolError):
            process.send({"value": "\ud800"})


def test_invalid_and_oversized_outbound_json_never_spawn(monkeypatch, fake_cli):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    popen_calls = []

    def forbidden_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("invalid outbound input reached Popen")

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", forbidden_popen)
    invalid = JsonlProcess(argv(fake_cli, "echo"))
    with pytest.raises(ProtocolError, match="bounded JSON"):
        invalid.send({"value": float("nan")})
    oversized = JsonlProcess(
        argv(fake_cli, "echo"), limits=TransportLimits(max_line_bytes=32)
    )
    with pytest.raises(LimitExceeded, match="outbound JSONL"):
        oversized.send({"payload": "x" * 100})

    assert popen_calls == []
    assert invalid.pid is None and oversized.pid is None
    assert invalid._environment.env == {}
    assert oversized._environment.env == {}


def test_cwd_with_unencodable_unicode_is_a_stable_configuration_error(fake_cli):
    with pytest.raises(ConfigurationError, match="filesystem path") as caught:
        JsonlProcess(argv(fake_cli, "events"), cwd="\ud800")
    assert caught.value.__cause__ is None


def test_malformed_and_flood_fail_closed(fake_cli):
    with pytest.raises(ProtocolError):
        list(JsonlProcess(argv(fake_cli, "malformed")).iter_messages())
    with pytest.raises(ProtocolError):
        list(JsonlProcess(argv(fake_cli, "nonfinite")).iter_messages())
    with pytest.raises(ProtocolError):
        list(JsonlProcess(argv(fake_cli, "duplicate-key")).iter_messages())
    with pytest.raises(ProtocolError, match="LF-terminated"):
        list(JsonlProcess(argv(fake_cli, "unterminated")).iter_messages())
    limits = TransportLimits(max_output_bytes=500, max_events=5)
    with pytest.raises(LimitExceeded):
        list(JsonlProcess(argv(fake_cli, "flood"), limits=limits).iter_messages())


def test_stderr_is_separate_bounded_and_secret_redacted(fake_cli):
    secret = "do-not-echo-this-secret"
    process = JsonlProcess(
        argv(fake_cli, "stderr-secret"),
        provider_env={"FAKE_TOKEN": secret},
        allowed_provider_env=["FAKE_TOKEN"],
    )
    with pytest.raises(ProcessFailed) as caught:
        list(process.iter_messages())
    assert secret not in str(caught.value)
    assert "Authorization" not in str(caught.value)
    assert secret not in process.diagnostics
    assert "[REDACTED]" in process.diagnostics


def test_clean_stdout_eof_still_drains_and_limits_stderr(fake_cli):
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    process = JsonlProcess(
        argv(fake_cli, "stderr-flood-after-stdout-eof"),
        limits=TransportLimits(max_stderr_bytes=1024),
    ).start()
    pid = process.pid

    with pytest.raises(LimitExceeded, match="stderr"):
        list(process.iter_messages())

    assert process._closed is True
    assert process._proc is None
    assert process._environment.env == {}
    assert not process._threads
    assert pid is not None
    wait_for_exit(pid)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.ident not in baseline_threads
        and thread.name.startswith("unified-cli-jsonl-")
    ]


def test_nonzero_exit_still_synchronizes_stderr_overflow(fake_cli):
    process = JsonlProcess(
        argv(fake_cli, "stderr-flood-nonzero"),
        limits=TransportLimits(max_stderr_bytes=1024),
    )
    with pytest.raises(LimitExceeded, match="stderr"):
        list(process.iter_messages())
    assert process._closed is True
    assert process._proc is None
    assert process._stderr_done.is_set()


def test_unexpected_stderr_reader_escape_is_stored_before_done(
    fake_cli, monkeypatch
):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    identity = ExecutableIdentity.capture(fake_cli)
    real_popen = jsonl_module.subprocess.Popen
    real_read = jsonl_module.os.read
    captured = {}

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        return process

    def fail_stderr_read(descriptor, size):
        process = captured.get("process")
        if process is not None and process.stderr is not None:
            if descriptor == process.stderr.fileno():
                raise RuntimeError("injected stderr reader escape")
        return real_read(descriptor, size)

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(jsonl_module.os, "read", fail_stderr_read)
    process = _JsonlProcess(
        argv(fake_cli, "stderr-secret"),
        provider_env={"FAKE_TOKEN": "reader-secret"},
        allowed_provider_env=("FAKE_TOKEN",),
        executable_identity=identity,
    )
    with pytest.raises(TransportError, match="stderr"):
        list(process.iter_messages())
    assert process._stderr_done.is_set()
    assert process._closed is True
    assert process._proc is None


def test_process_transports_require_canonical_identity_before_spawn(
    fake_cli, monkeypatch
):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    calls = []

    def forbidden_popen(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("unbound executable reached Popen")

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", forbidden_popen)
    with pytest.raises(TypeError, match="executable_identity"):
        _JsonlProcess(argv(fake_cli, "events"))
    with pytest.raises(TypeError, match="executable_identity"):
        _JsonRpcProcessClient(argv(fake_cli, "rpc"))
    identity = ExecutableIdentity.capture(fake_cli)
    with pytest.raises(ConfigurationError, match="canonical and absolute"):
        _JsonlProcess(
            (os.path.basename(fake_cli), "events"),
            executable_identity=identity,
        )
    assert calls == []


def test_jsonl_start_close_and_start_start_are_serialized(fake_cli, monkeypatch):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    real_popen = jsonl_module.subprocess.Popen
    entered = threading.Event()
    release = threading.Event()
    spawned = []
    popen_calls = []

    def blocked_popen(*args, **kwargs):
        popen_calls.append(args[0])
        entered.set()
        assert release.wait(timeout=2)
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", blocked_popen)
    process = JsonlProcess(argv(fake_cli, "hang"), timeout=3)
    start_failures = []
    close_failures = []

    def start_process():
        try:
            process.start()
        except BaseException as caught:
            start_failures.append(caught)

    starter = threading.Thread(target=start_process)
    starter.start()
    assert entered.wait(timeout=2)

    def close_process():
        try:
            process.close()
        except BaseException as caught:
            close_failures.append(caught)

    closer = threading.Thread(target=close_process)
    closer.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with process._lifecycle_lock:
            if process._close_requested:
                break
        time.sleep(0.005)
    assert process._close_requested is True
    release.set()
    starter.join(timeout=3)
    closer.join(timeout=3)
    assert not starter.is_alive() and not closer.is_alive()
    assert len(popen_calls) == 1
    assert len(start_failures) == 1
    assert isinstance(start_failures[0], TransportError)
    assert close_failures == []
    assert process._closed is True
    assert process._resources_complete()
    assert spawned
    wait_for_exit(spawned[0].pid)

    entered.clear()
    release.clear()
    spawned.clear()
    popen_calls.clear()
    second = JsonlProcess(argv(fake_cli, "hang"), timeout=3)
    results = []
    failures = []
    rendezvous = threading.Barrier(3)

    def concurrent_start():
        rendezvous.wait()
        try:
            results.append(second.start())
        except BaseException as caught:
            failures.append(caught)

    first_thread = threading.Thread(target=concurrent_start)
    second_thread = threading.Thread(target=concurrent_start)
    first_thread.start()
    second_thread.start()
    rendezvous.wait()
    assert entered.wait(timeout=2)
    release.set()
    first_thread.join(timeout=3)
    second_thread.join(timeout=3)
    assert failures == []
    assert results == [second, second]
    assert len(popen_calls) == 1
    pid = second.pid
    second.close()
    assert pid is not None
    wait_for_exit(pid)


@pytest.mark.parametrize("operation", ("send", "receive"))
def test_jsonl_sync_operation_racing_close_after_start_is_stable(
    fake_cli, operation
):
    process = JsonlProcess(argv(fake_cli, "hang"), timeout=3).start()
    original_start = process.start
    start_returned = threading.Event()
    release_operation = threading.Event()
    failures = []

    def paused_start():
        result = original_start()
        start_returned.set()
        assert release_operation.wait(timeout=2)
        return result

    process.start = paused_start

    def run_operation():
        try:
            if operation == "send":
                process.send({"value": 1})
            else:
                process.receive()
        except BaseException as caught:
            failures.append(caught)

    worker = threading.Thread(target=run_operation)
    worker.start()
    try:
        assert start_returned.wait(timeout=2)
        process.close()
    finally:
        release_operation.set()
        worker.join(timeout=2)
        if not process._closed:
            process.close()

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], TransportError)
    assert str(failures[0]) == "transport is closed"
    assert process._resources_complete()


@pytest.mark.parametrize("operation", ("send_async", "receive_async"))
def test_jsonl_async_operation_racing_close_after_start_is_stable(
    fake_cli, operation
):
    async def run():
        process = JsonlProcess(argv(fake_cli, "hang"), timeout=3).start()
        original_start = process.start
        start_returned = threading.Event()
        release_operation = threading.Event()

        def paused_start():
            result = original_start()
            start_returned.set()
            assert release_operation.wait(timeout=2)
            return result

        process.start = paused_start
        if operation == "send_async":
            task = asyncio.create_task(process.send_async({"value": 1}))
        else:
            task = asyncio.create_task(process.receive_async())
        loop = asyncio.get_running_loop()
        try:
            assert await loop.run_in_executor(None, start_returned.wait, 2)
            await process.close_async()
        finally:
            release_operation.set()

        with pytest.raises(TransportError, match="^transport is closed$"):
            await asyncio.wait_for(task, timeout=2)
        assert process._resources_complete()

    asyncio.run(run())


def test_jsonl_pre_cancelled_start_terminalizes_before_bounded_close(fake_cli):
    cancellation = CancellationToken()
    cancellation.cancel()
    process = JsonlProcess(
        argv(fake_cli, "hang"), timeout=2, cancellation=cancellation
    )
    with pytest.raises(TransportCancelled):
        process.start()
    assert process._lifecycle_state == "closed"
    assert process._resources_complete()

    failures = []
    def close_process():
        try:
            process.close()
        except BaseException as caught:
            failures.append(caught)

    closer = threading.Thread(target=close_process)
    closer.start()
    closer.join(timeout=1)
    assert not closer.is_alive()
    assert failures == []


def test_async_cancellation_during_start_keeps_cleanup_owner(fake_cli, monkeypatch):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    real_popen = jsonl_module.subprocess.Popen
    entered = threading.Event()
    release = threading.Event()
    spawned = []

    def blocked_popen(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", blocked_popen)

    async def run():
        process = JsonlProcess(argv(fake_cli, "hang"), timeout=5)
        task = asyncio.create_task(process.receive_async())
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        return process

    process = asyncio.run(run())
    assert process._closed is True
    assert process._lifecycle_state == "closed"
    assert process._resources_complete()
    assert spawned
    wait_for_exit(spawned[0].pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_jsonl_group_signals_never_follow_leader_reap(fake_cli, monkeypatch):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    real_popen = jsonl_module.subprocess.Popen
    real_killpg = jsonl_module.os.killpg
    spawned = {}
    observations = []

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned[process.pid] = process
        return process

    def guarded_killpg(pgid, sig):
        process = spawned[pgid]
        observations.append((pgid, sig, process.returncode))
        assert process.returncode is None
        return real_killpg(pgid, sig)

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(jsonl_module.os, "killpg", guarded_killpg)

    assert list(JsonlProcess(argv(fake_cli, "events")).iter_messages())
    failing = JsonlProcess(argv(fake_cli, "hang"), timeout=0.05)
    with pytest.raises(TransportTimeout):
        failing.receive()

    assert observations
    assert all(process.returncode is not None for process in spawned.values())


def test_timeout_and_explicit_cancellation_cleanup(fake_cli):
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        JsonlProcess(argv(fake_cli, "hang"), timeout=0.15).receive()
    assert time.monotonic() - started < 2

    token = CancellationToken()
    process = JsonlProcess(argv(fake_cli, "hang"), timeout=5, cancellation=token).start()
    token.cancel()
    with pytest.raises(TransportCancelled):
        process.receive()
    process.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
@pytest.mark.parametrize("via_timeout", (False, True))
def test_escaped_descendant_inherited_pipes_cannot_block_cleanup(
    fake_cli, via_timeout
):
    process = JsonlProcess(
        argv(fake_cli, "escaped-inherited-pipes"), timeout=2
    ).start()
    leader_pid = process.pid
    child_pid = None
    environment_root = os.path.dirname(process._environment.env["HOME"])
    try:
        child_pid = process.receive()["child_pid"]
        started = time.monotonic()
        if via_timeout:
            process.timeout = 0.08
            process.reset_timeout()
            with pytest.raises(TransportTimeout):
                process.receive()
        else:
            process.close()
        assert time.monotonic() - started < 1.5
        assert all(not thread.is_alive() for thread in process._threads)
        assert not any(
            thread.name.startswith("unified-cli-jsonl-{}-".format(leader_pid))
            for thread in threading.enumerate()
        )
        assert process._environment.env == {}
        assert not os.path.exists(environment_root)
        assert leader_pid is not None
        wait_for_exit(leader_pid)
        # A deliberately setsid-detached child is outside the provider group.
        # The wrapper is bounded, while an OS containment layer must own the
        # escaped process itself.
        assert pid_exists(child_pid)
    finally:
        if not process._closed:
            process.close()
        if child_pid is not None:
            terminate_exact_pid(child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_jsonl_term_ignore_and_killpg_failure_have_bounded_fallback(
    fake_cli, monkeypatch
):
    ignoring = JsonlProcess(argv(fake_cli, "term-ignore"), timeout=2).start()
    ignoring_pid = ignoring.pid
    started = time.monotonic()
    ignoring.close()
    assert time.monotonic() - started < 1.5
    assert ignoring_pid is not None
    wait_for_exit(ignoring_pid)

    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    fallback = JsonlProcess(argv(fake_cli, "hang"), timeout=2).start()
    fallback_pid = fallback.pid
    monkeypatch.setattr(
        jsonl_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(PermissionError()),
    )
    started = time.monotonic()
    fallback.close()
    assert time.monotonic() - started < 1.5
    assert fallback_pid is not None
    wait_for_exit(fallback_pid)


def test_async_receive_cancellation_closes_process(fake_cli):
    async def run():
        process = JsonlProcess(argv(fake_cli, "hang"), timeout=5).start()
        pid = process.pid
        task = asyncio.create_task(process.receive_async())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(run())
    assert pid is not None
    deadline = time.monotonic() + 2
    while pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pid_exists(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
def test_blocked_sync_send_honors_deadline_and_cleans_process_group(fake_cli):
    process = JsonlProcess(argv(fake_cli, "no-read-descendant"), timeout=1).start()
    leader_pid = process.pid
    child_pid = process.receive()["child_pid"]
    process.timeout = 0.1
    process.reset_timeout()
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        process.send({"payload": "x" * 900_000})
    assert time.monotonic() - started < 1
    assert leader_pid is not None
    wait_for_exit(leader_pid, child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
def test_blocked_sync_send_honors_explicit_cancellation(fake_cli):
    token = CancellationToken()
    process = JsonlProcess(
        argv(fake_cli, "no-read-descendant"), timeout=5, cancellation=token
    ).start()
    leader_pid = process.pid
    child_pid = process.receive()["child_pid"]
    process.reset_timeout()

    async def cancel_soon():
        await asyncio.sleep(0.05)
        token.cancel()

    async def run():
        task = asyncio.create_task(process.send_async({"payload": "x" * 900_000}))
        canceller = asyncio.create_task(cancel_soon())
        with pytest.raises(TransportCancelled):
            await task
        await canceller

    asyncio.run(run())
    assert leader_pid is not None
    wait_for_exit(leader_pid, child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
def test_blocked_async_send_task_cancel_cleans_process_group(fake_cli):
    async def run():
        process = JsonlProcess(argv(fake_cli, "no-read-descendant"), timeout=5).start()
        leader_pid = process.pid
        child_pid = await process.receive_async()
        process.reset_timeout()
        task = asyncio.create_task(process.send_async({"payload": "x" * 900_000}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return leader_pid, child_pid["child_pid"]

    leader_pid, child_pid = asyncio.run(run())
    assert leader_pid is not None
    wait_for_exit(leader_pid, child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_early_close_kills_descendant_process_group(fake_cli):
    process = JsonlProcess(argv(fake_cli, "descendant"), timeout=5)
    stream = process.iter_messages()
    child_pid = next(stream)["child_pid"]
    assert pid_exists(child_pid)
    stream.close()
    deadline = time.monotonic() + 2
    while pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pid_exists(child_pid)


def test_jsonrpc_correlates_ids_and_handles_server_requests(fake_cli):
    client = JsonRpcProcessClient(
        argv(fake_cli, "rpc"),
        request_handlers={"permission": lambda params: {"decision": "deny", "risk": params["risk"]}},
    )
    assert client.request("run", {"value": 1}) == {
        "reverse": {"decision": "deny", "risk": "low"}
    }
    client.close()


def test_jsonrpc_async_parity_and_unmatched_response_fails(fake_cli):
    async def run():
        client = JsonRpcProcessClient(
            argv(fake_cli, "rpc"),
            request_handlers={"permission": lambda params: "deny"},
        )
        try:
            return await client.request_async("run")
        finally:
            client.close()

    assert asyncio.run(run()) == {"reverse": "deny"}
    with pytest.raises(ProtocolError):
        JsonRpcProcessClient(argv(fake_cli, "rpc-unmatched")).request("run")
    with pytest.raises(ProtocolError):
        JsonRpcProcessClient(argv(fake_cli, "rpc-bool-id")).request("run")


def test_jsonrpc_timeout_is_fresh_for_each_persistent_request(fake_cli):
    client = JsonRpcProcessClient(argv(fake_cli, "rpc-twice"), timeout=1.0)
    try:
        assert client.request("first", {"n": 1}) == {"n": 1}
        time.sleep(1.05)
        assert client.request("second", {"n": 2}) == {"n": 2}
    finally:
        client.close()


def test_jsonrpc_async_reverse_handler_respects_operation_timeout(fake_cli):
    async def slow_handler(params):
        await asyncio.sleep(5)

    async def run():
        client = JsonRpcProcessClient(
            argv(fake_cli, "rpc"),
            request_handlers={"permission": slow_handler},
            timeout=0.08,
        )
        try:
            await client.request_async("run")
        finally:
            client.close()

    with pytest.raises(TransportTimeout):
        asyncio.run(run())


def test_jsonrpc_blocking_sync_reverse_handler_respects_deadline(fake_cli):
    release = threading.Event()
    client = JsonRpcProcessClient(
        argv(fake_cli, "rpc"),
        request_handlers={"permission": lambda params: release.wait(5)},
        timeout=0.1,
    )
    with client:
        pid = client.pid
        started = time.monotonic()
        with pytest.raises(TransportTimeout):
            client.request("run")
        assert time.monotonic() - started < 1
    release.set()
    assert pid is not None
    wait_for_exit(pid)


def test_jsonrpc_blocking_sync_callback_does_not_block_async_loop(fake_cli):
    release = threading.Event()

    async def run():
        client = JsonRpcProcessClient(
            argv(fake_cli, "rpc"),
            request_handlers={"permission": lambda params: release.wait(5)},
            timeout=0.1,
        )
        client.__enter__()
        pid = client.pid
        started = time.monotonic()
        try:
            with pytest.raises(TransportTimeout):
                await client.request_async("run")
            assert time.monotonic() - started < 1
        finally:
            release.set()
            client.close()
        return pid

    pid = asyncio.run(run())
    assert pid is not None
    wait_for_exit(pid)


def test_jsonrpc_async_reverse_handler_observes_explicit_token(fake_cli):
    async def run():
        token = CancellationToken()
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def handler(params):
            entered.set()
            try:
                await asyncio.sleep(5)
            finally:
                cleaned.set()

        client = JsonRpcProcessClient(
            argv(fake_cli, "rpc"),
            request_handlers={"permission": handler},
            cancellation=token,
            timeout=5,
        )
        client.__enter__()
        pid = client.pid
        task = asyncio.create_task(client.request_async("run"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        token.cancel()
        with pytest.raises(TransportCancelled):
            await asyncio.wait_for(task, timeout=1)
        assert cleaned.is_set()
        client.close()
        return pid

    pid = asyncio.run(run())
    assert pid is not None
    wait_for_exit(pid)


def test_jsonrpc_params_wire_shape_and_scalar_rejection_sync_async(fake_cli):
    with JsonRpcProcessClient(argv(fake_cli, "rpc-shape")) as client:
        assert client.request("run") == {"has_params": False, "params": None}

    rejected = JsonRpcProcessClient(argv(fake_cli, "rpc-shape"))
    with pytest.raises(ProtocolError, match="params"):
        rejected.request("run", 1)
    assert rejected.pid is None

    async def run():
        with JsonRpcProcessClient(argv(fake_cli, "rpc-shape")) as client:
            result = await client.request_async("run")
        rejected_async = JsonRpcProcessClient(argv(fake_cli, "rpc-shape"))
        with pytest.raises(ProtocolError, match="params"):
            await rejected_async.request_async("run", "scalar")
        assert rejected_async.pid is None
        return result

    assert asyncio.run(run()) == {"has_params": False, "params": None}


def test_jsonrpc_error_requires_message_and_never_reflects_it(fake_cli):
    with pytest.raises(ProtocolError, match="malformed"):
        JsonRpcProcessClient(argv(fake_cli, "rpc-error-missing-message")).request("run")

    async def malformed_async():
        await JsonRpcProcessClient(
            argv(fake_cli, "rpc-error-missing-message")
        ).request_async("run")

    with pytest.raises(ProtocolError, match="malformed"):
        asyncio.run(malformed_async())

    with pytest.raises(TransportError, match="error 7") as caught:
        JsonRpcProcessClient(argv(fake_cli, "rpc-error")).request("run")
    assert "secret peer details" not in str(caught.value)


def test_huge_integer_timeout_is_a_stable_configuration_error(fake_cli):
    with pytest.raises(ConfigurationError, match="finite positive") as caught:
        JsonlProcess(argv(fake_cli, "events"), timeout=10**10000)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 0, -1])
def test_transport_timeout_must_be_finite_positive(fake_cli, bad):
    with pytest.raises(Exception):
        JsonlProcess(argv(fake_cli, "events"), timeout=bad)
