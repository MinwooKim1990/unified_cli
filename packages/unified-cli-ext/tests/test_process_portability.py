import concurrent.futures
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_cli_ext import (
    JsonlProcess,
    TransportError,
    UnsupportedPlatformError,
)
from unified_cli_ext.transports import run_fixed_process
from unified_cli_ext.transports.security import ExecutableIdentity


def _process_module():
    return __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )


def _force_darwin_fallback(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("Darwin libc waitid compatibility path")
    process_module = _process_module()
    monkeypatch.setattr(process_module, "_NATIVE_WAITID", None)
    monkeypatch.setattr(process_module, "_DARWIN_LIBC_WAITID", None)
    process_module._require_nonreaping_process_observation()
    return process_module


def _observe_until_exit(process_module, process):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        returncode = process_module._observe_process_returncode_nonreaping(process)
        if returncode is not None:
            return returncode
        time.sleep(0.005)
    raise AssertionError("child did not become observable")


def test_process_transport_import_keeps_ctypes_lazy():
    root = Path(__file__).resolve().parents[3]
    source_path = os.pathsep.join(
        (str(root / "src"), str(root / "packages" / "unified-cli-ext" / "src"))
    )
    script = r'''
import builtins
seen = []
original = builtins.__import__
def capture(name, *args, **kwargs):
    seen.append(name)
    return original(name, *args, **kwargs)
builtins.__import__ = capture
import unified_cli_ext.transports.process
assert "ctypes" not in seen
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = source_path
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(root),
        env=environment,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_darwin_fallback_unavailable_fails_before_spawn(tmp_path, monkeypatch):
    process_module = _process_module()
    popen_calls = []

    def unavailable():
        raise UnsupportedPlatformError("injected waitid unavailable")

    def forbidden_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("unsupported observer reached Popen")

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", None)
    monkeypatch.setattr(process_module, "_DARWIN_LIBC_WAITID", None)
    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    monkeypatch.setattr(process_module, "_load_darwin_libc_waitid", unavailable)
    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)

    with pytest.raises(UnsupportedPlatformError, match="waitid unavailable"):
        run_fixed_process(
            ("/bin/echo", "unused"),
            cwd=str(tmp_path),
            executable_identity=object(),
        )
    assert popen_calls == []


def test_malformed_darwin_fallback_result_is_reaped_safely(
    fake_cli, tmp_path, monkeypatch
):
    process_module = _process_module()
    real_popen = process_module.subprocess.Popen
    real_killpg = process_module.os.killpg
    spawned = []
    group_signals = []

    def malformed_waitid(idtype, child_id, options):
        return process_module._NonreapingWaitResult(
            child_id + 1,
            process_module.os.CLD_EXITED,
            0,
        )

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def guarded_killpg(pgid, sig):
        assert spawned and spawned[0].returncode is None
        group_signals.append(sig)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", None)
    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    monkeypatch.setattr(process_module, "_DARWIN_LIBC_WAITID", malformed_waitid)
    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(process_module.os, "killpg", guarded_killpg)

    with pytest.raises(TransportError, match="unexpected non-reaping wait result"):
        run_fixed_process(
            (fake_cli, "hang"),
            timeout=1,
            cwd=str(tmp_path),
            executable_identity=ExecutableIdentity.capture(fake_cli),
        )

    assert len(spawned) == 1
    process = spawned[0]
    assert process.returncode is not None
    assert signal.SIGTERM in group_signals
    assert signal.SIGKILL in group_signals
    assert all(
        stream is None or stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_nonreaping_observer_retries_eintr_and_maps_echild(monkeypatch):
    process_module = _process_module()
    calls = []

    def interrupted_once(idtype, child_id, options):
        calls.append(child_id)
        if len(calls) == 1:
            raise InterruptedError()
        return process_module._NonreapingWaitResult(
            child_id,
            process_module.os.CLD_EXITED,
            7,
        )

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", interrupted_once)
    process = SimpleNamespace(pid=12345, returncode=None)
    assert process_module._observe_process_returncode_nonreaping(process) == 7
    assert calls == [12345, 12345]

    def no_child(idtype, child_id, options):
        raise ChildProcessError()

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", no_child)
    with pytest.raises(TransportError, match="could not be observed without reaping"):
        process_module._observe_process_returncode_nonreaping(process)

    def observation_error(idtype, child_id, options):
        raise OSError(errno.EIO, "injected waitid failure")

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", observation_error)
    with pytest.raises(TransportError, match="could not be observed without reaping"):
        process_module._observe_process_returncode_nonreaping(process)


@pytest.mark.parametrize(
    "result_code,result_status",
    (
        (999, 0),
        (getattr(os, "CLD_EXITED", 1), -1),
        (getattr(os, "CLD_EXITED", 1), 256),
        (getattr(os, "CLD_KILLED", 2), 0),
        (getattr(os, "CLD_DUMPED", 3), signal.NSIG),
    ),
)
def test_nonreaping_observer_rejects_malformed_code_and_status_ranges(
    result_code, result_status, monkeypatch
):
    process_module = _process_module()

    def malformed(idtype, child_id, options):
        return process_module._NonreapingWaitResult(
            child_id,
            result_code,
            result_status,
        )

    monkeypatch.setattr(process_module, "_NATIVE_WAITID", malformed)
    process = SimpleNamespace(pid=12345, returncode=None)
    with pytest.raises(TransportError, match="unsupported non-reaping wait result"):
        process_module._observe_process_returncode_nonreaping(process)


def test_darwin_libc_waitid_lazy_binding_is_concurrency_safe(monkeypatch):
    process_module = _force_darwin_fallback(monkeypatch)
    monkeypatch.setattr(process_module, "_DARWIN_LIBC_WAITID", None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(
            executor.map(
                lambda unused: process_module._load_darwin_libc_waitid(),
                range(32),
            )
        )

    assert bindings
    assert all(binding is bindings[0] for binding in bindings)
    assert process_module._DARWIN_LIBC_WAITID is bindings[0]


def test_native_waitid_remains_preferred_and_nonreaping(monkeypatch):
    process_module = _process_module()
    if process_module._NATIVE_WAITID is None:
        pytest.skip("native os.waitid is unavailable")

    def forbidden_fallback():
        raise AssertionError("native waitid reached Darwin fallback")

    monkeypatch.setattr(
        process_module,
        "_load_darwin_libc_waitid",
        forbidden_fallback,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        start_new_session=True,
    )
    try:
        assert _observe_until_exit(process_module, process) == 7
        assert process.returncode is None
        assert process_module._cleanup_spawned_process(process) == 7
    finally:
        if process.returncode is None:
            process_module._cleanup_spawned_process(process)


@pytest.mark.parametrize("exit_status", (0, 7))
def test_darwin_libc_waitid_observes_exit_without_reaping(
    exit_status, monkeypatch
):
    process_module = _force_darwin_fallback(monkeypatch)
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit({})".format(exit_status)],
        start_new_session=True,
    )
    try:
        assert _observe_until_exit(process_module, process) == exit_status
        assert process.returncode is None
        assert process_module._cleanup_spawned_process(process) == exit_status
    finally:
        if process.returncode is None:
            process_module._cleanup_spawned_process(process)


@pytest.mark.parametrize("termination_signal", (signal.SIGTERM, signal.SIGKILL))
def test_darwin_libc_waitid_observes_signal_without_reaping(
    termination_signal, monkeypatch
):
    process_module = _force_darwin_fallback(monkeypatch)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        os.kill(process.pid, termination_signal)
        assert _observe_until_exit(process_module, process) == -termination_signal
        assert process.returncode is None
        assert process_module._cleanup_spawned_process(process) == -termination_signal
    finally:
        if process.returncode is None:
            process_module._cleanup_spawned_process(process)


def test_darwin_libc_waitid_runs_fixed_and_jsonl_paths(
    fake_cli, tmp_path, monkeypatch
):
    _force_darwin_fallback(monkeypatch)
    identity = ExecutableIdentity.capture(fake_cli)
    fixed = run_fixed_process(
        (fake_cli, "events"),
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    assert fixed.returncode == 0
    assert '"type":"done"' in fixed.stdout

    messages = list(
        JsonlProcess(
            (fake_cli, "events"),
            cwd=str(tmp_path),
            executable_identity=identity,
        ).iter_messages()
    )
    assert [message["type"] for message in messages] == ["text_delta", "done"]
