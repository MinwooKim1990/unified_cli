import asyncio
import os
import signal
import sys
import threading
import time

import pytest

from unified_cli_ext import (
    CancellationToken,
    ConfigurationError,
    JsonRpcProcessClient,
    JsonlProcess,
    LimitExceeded,
    ProcessFailed,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportLimits,
    TransportTimeout,
)


def argv(fake_cli, mode):
    return [sys.executable, fake_cli, mode]


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
    client = JsonRpcProcessClient(argv(fake_cli, "rpc-twice"), timeout=0.2)
    try:
        assert client.request("first", {"n": 1}) == {"n": 1}
        time.sleep(0.25)
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
