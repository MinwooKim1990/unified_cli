"""Integration tests for the subprocess streaming core (base.py + watchdog).

These spawn a REAL child process (a tiny fake CLI run via `sys.executable -c`)
so the actual Popen / stderr-drain / watchdog / kill-on-abort / auth-fallback
paths are exercised — code that previously had zero coverage. The 0.3.0
connectivity work (output watchdog, stdin=DEVNULL, utf-8, API-key stripping)
regresses here.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import base
from unified_cli.base import BaseProvider
from unified_cli.core import Message, Response, Usage
from unified_cli.errors import UnifiedError


# A minimal fake CLI. Behaviour selected by argv[1]. Emits newline-delimited
# JSON on stdout (the shape FakeProvider._normalize understands).
_FAKE_CLI = r'''
import sys, json, time, os, subprocess
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
if mode == "ok":
    emit({"type": "session", "session_id": "sess-abc"})
    emit({"type": "text", "text": "hello"})
    emit({"type": "usage", "in": 5, "out": 3})
    emit({"type": "done"})
elif mode == "unicode":
    emit({"type": "text", "text": "안녕 \U0001f31f café"})
    emit({"type": "done"})
elif mode == "unicode_split":
    raw = (json.dumps(
        {"type": "text", "text": "안녕 \U0001f31f café"},
        ensure_ascii=False,
    ) + "\n").encode("utf-8")
    for byte in raw:
        os.write(1, bytes([byte]))
    emit({"type": "done"})
elif mode == "hang":
    time.sleep(60)
elif mode == "stderr_flood":
    sys.stderr.write("x" * 200000); sys.stderr.flush()
    emit({"type": "text", "text": "after flood"})
    emit({"type": "done"})
elif mode == "auth":
    # Succeeds ONLY if the API key reached the child env. The wrapper strips it
    # by default and re-adds it on the auth fallback, so first call fails and
    # the fallback retry succeeds.
    if os.environ.get("ANTHROPIC_API_KEY"):
        emit({"type": "text", "text": "ok-after-fallback"}); emit({"type": "done"})
    else:
        sys.stderr.write("authentication_error: OAuth token has expired\n")
        sys.stderr.flush(); sys.exit(1)
elif mode == "slow":
    emit({"type": "text", "text": "first"})
    time.sleep(60)
elif mode == "drip":
    # Healthy child: emits 4 lines 0.3s apart (each gap < idle deadline) then
    # exits. Used with a SLOW consumer to prove the watchdog measures the
    # child's cadence, not the consumer's pull rate.
    for i in range(4):
        emit({"type": "text", "text": "drip%d" % i})
        time.sleep(0.3)
    emit({"type": "done"})
elif mode == "echo_key":
    emit({"type": "text", "text": "KEY=" + os.environ.get("ANTHROPIC_API_KEY", "<none>")})
    emit({"type": "done"})
elif mode == "output_flood":
    for i in range(100):
        emit({"type": "text", "text": "x" * 2048})
elif mode == "oversized_line":
    os.write(1, b'{"type":"text","text":"' + b"x" * 4096)
elif mode == "spawn_descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
    emit({"type": "text", "text": "CHILD=" + str(child.pid)})
    time.sleep(60)
elif mode == "spawn_then_exit":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
    emit({"type": "text", "text": "CHILD=" + str(child.pid)})
    sys.exit(0)
elif mode == "detached_pipe_holder":
    # A new-session child is outside the provider leader's process group but
    # inherits both output pipes. The wrapper must finish when the leader exits
    # instead of waiting for this unrelated descriptor holder.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        start_new_session=True,
    )
    pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
    emit({"type": "text", "text": "detached-holder"})
    emit({"type": "done"})
    sys.exit(0)
elif mode == "detached_continuous_writer":
    code = r"""
import json, os, time
leader = os.getppid()
while os.getppid() == leader:
    time.sleep(0.001)
for i in range(400):
    try:
        raw = (json.dumps({"type": "text", "text": "detached%d" % i}) + "\n").encode()
        os.write(1, raw)
    except BrokenPipeError:
        break
    time.sleep(0.005)
"""
    child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
    emit({"type": "text", "text": "leader"})
    emit({"type": "done"})
    sys.exit(0)
'''


class FakeProvider(BaseProvider):
    # Use a real provider key so classify()'s matcher table applies (the auth
    # test relies on the claude auth_expired pattern).
    name = "claude"
    default_model = "fake-model"
    api_key_env = "ANTHROPIC_API_KEY"

    def __init__(self, mode: str = "ok", **kw):
        kw.setdefault("bin_path", sys.executable)
        super().__init__(**kw)
        self._mode = mode

    @classmethod
    def _discover_bin(cls):
        return sys.executable

    @classmethod
    def _install_hint(cls):
        return ""

    def _build_args(self, prompt, *, session_id, resume_last, model,
                    streaming, images=None):
        return [sys.executable, "-c", _FAKE_CLI, self._mode], None

    def _normalize(self, obj):
        tp = obj.get("type")
        if tp == "session":
            yield Message(kind="session", provider=self.name,
                          session_id=obj.get("session_id"), raw=obj)
        elif tp == "text":
            yield Message(kind="text", provider=self.name,
                          text=obj.get("text", ""), raw=obj)
        elif tp == "usage":
            yield Message(kind="usage", provider=self.name,
                          usage=Usage(input_tokens=obj.get("in"),
                                      output_tokens=obj.get("out")), raw=obj)
        elif tp == "done":
            yield Message(kind="done", provider=self.name, raw=obj)

    def _parse_json_response(self, text, model):
        chunks = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "text":
                chunks.append(obj.get("text", ""))
        return Response(text="".join(chunks), session_id="", provider=self.name,
                        model=model, usage=Usage(), messages=[], raw=[])


class FakeOAuthOnlyProvider(FakeProvider):
    """Provider shape that exposes a key name but never permits fallback."""

    allow_api_key_fallback = False


class StatefulNormalizeProvider(FakeProvider):
    """Makes BaseProvider's per-invocation parser-state plumbing observable."""

    def _new_stream_state(self):
        return {"text_count": 0}

    def _stream_normalize(self, obj, state):
        for message in super()._stream_normalize(obj, state):
            if message.kind == "text":
                state["text_count"] += 1
                yield Message(
                    kind="text",
                    provider=self.name,
                    text=f"{state['text_count']}:{message.text}",
                    raw=message.raw,
                )
            else:
                yield message


def _capture_popen(monkeypatch):
    processes = []
    popen = base.subprocess.Popen

    def capture_process(*args, **kwargs):
        proc = popen(*args, **kwargs)
        proc._test_pipe_fds = tuple(
            pipe.fileno() for pipe in (proc.stdout, proc.stderr)
            if pipe is not None
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr(base.subprocess, "Popen", capture_process)
    return processes


def _assert_popen_pipes_closed(proc):
    assert all(
        pipe is None or pipe.closed
        for pipe in (proc.stdin, proc.stdout, proc.stderr)
    )


def _capture_async_transports(monkeypatch):
    transports = []
    connection_made = base._AsyncJsonlProtocol.connection_made

    def capture(protocol, transport):
        transports.append(transport)
        connection_made(protocol, transport)

    monkeypatch.setattr(base._AsyncJsonlProtocol, "connection_made", capture)
    return transports


def _cleanup_detached_holder(pid_file):
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    assert _wait_for_pid_exit(pid)


# ---- happy path ----

def test_stream_ok_yields_all_events(monkeypatch):
    processes = _capture_popen(monkeypatch)
    fp = FakeProvider("ok")
    kinds = [m.kind for m in fp.stream("hi")]
    assert kinds == ["session", "text", "usage", "done"]
    assert len(processes) == 1
    _assert_popen_pipes_closed(processes[0])


def test_stream_unicode_roundtrips():
    fp = FakeProvider("unicode")
    texts = [m.text for m in fp.stream("hi") if m.kind == "text"]
    assert texts == ["안녕 🌟 café"]


@pytest.mark.parametrize("async_mode", [False, True])
def test_stream_unicode_roundtrips_when_utf8_is_byte_split(async_mode):
    fp = FakeProvider("unicode_split")
    if async_mode:
        import asyncio

        async def run():
            return [message.text async for message in fp.astream("hi")
                    if message.kind == "text"]

        texts = asyncio.run(run())
    else:
        texts = [message.text for message in fp.stream("hi")
                 if message.kind == "text"]
    assert texts == ["안녕 🌟 café"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptors")
def test_high_fd_chat_and_stream_retain_output(monkeypatch):
    import resource

    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit <= 1150:
        pytest.skip("RLIMIT_NOFILE is too low for a safe high-fd regression")
    fillers = []
    try:
        highest = -1
        while highest < 1100:
            highest = os.open(os.devnull, os.O_RDONLY)
            fillers.append(highest)
        processes = _capture_popen(monkeypatch)
        fp = FakeProvider("ok")
        assert fp.chat("hi").text == "hello"
        texts = [message.text for message in fp.stream("hi")
                 if message.kind == "text"]
        assert texts == ["hello"]
        assert len(processes) == 2
        assert all(min(proc._test_pipe_fds) >= 1100 for proc in processes)
        for proc in processes:
            _assert_popen_pipes_closed(proc)
    finally:
        for fd in fillers:
            os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX selectors")
@pytest.mark.parametrize("api", ["chat", "stream"])
def test_sync_reader_selector_failure_fails_closed(monkeypatch, api):
    class BrokenSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, *_args):
            raise OSError("selector registration failed")

    monkeypatch.setattr(base.selectors, "DefaultSelector", BrokenSelector)
    fp = FakeProvider("ok", timeout=1)
    with pytest.raises(UnifiedError) as error:
        if api == "chat":
            fp.chat("hi")
        else:
            list(fp.stream("hi"))
    assert error.value.kind == "internal"
    assert "reader failed" in error.value.message


def test_sync_interleaved_streams_receive_distinct_parser_state():
    fp = StatefulNormalizeProvider("ok")
    first = fp.stream("first")
    second = fp.stream("second")
    try:
        first_text = next(message.text for message in first if message.kind == "text")
        second_text = next(message.text for message in second if message.kind == "text")
    finally:
        first.close()
        second.close()
    assert first_text == "1:hello"
    assert second_text == "1:hello"


def test_stream_stderr_flood_no_deadlock():
    # 200 KB of stderr (> the ~64 KB pipe buffer) would wedge an undrained
    # child. The concurrent drain must let stdout finish. Bound the wall clock.
    fp = FakeProvider("stderr_flood", timeout=15)
    t0 = time.time()
    texts = [m.text for m in fp.stream("hi") if m.kind == "text"]
    assert texts == ["after flood"]
    assert time.time() - t0 < 10


# ---- watchdog: hang before first output ----

def test_stream_hang_before_output_fails_fast():
    fp = FakeProvider("hang", timeout=5, first_output_timeout=1)
    t0 = time.time()
    with pytest.raises(UnifiedError) as ei:
        list(fp.stream("hi"))
    elapsed = time.time() - t0
    assert ei.value.kind == "network"
    # Killed by the first-output watchdog (~1-2s), NOT after the 60s child sleep
    # or even the 5s stream timeout.
    assert elapsed < 4, elapsed


# ---- kill on abort ----

def test_stream_abort_kills_child_immediately():
    fp = FakeProvider("slow", timeout=30)
    gen = fp.stream("hi")
    first = next(gen)
    assert first.kind == "text" and first.text == "first"
    t0 = time.time()
    gen.close()  # deterministic GeneratorExit → finally → proc.kill()
    # Must not wait out the child's 60s sleep.
    assert time.time() - t0 < 5


# ---- backpressure: a slow CONSUMER must not kill a healthy child ----
# (regression for the watchdog measuring consumer pull-rate instead of the
# child's own output cadence — an SSE client applying backpressure would
# otherwise SIGKILL a live child mid-response.)

def test_slow_consumer_does_not_kill_healthy_child_sync():
    # idle deadline 1s; consumer sleeps 2s between pulls (> deadline). The child
    # drips a line every 0.3s (each gap < deadline), so it stays healthy.
    fp = FakeProvider("drip", timeout=1)
    texts = []
    for m in fp.stream("hi"):
        if m.kind == "text":
            texts.append(m.text)
            time.sleep(2)  # slow consumer — MUST NOT trigger the watchdog
    assert texts == ["drip0", "drip1", "drip2", "drip3"]


def test_slow_consumer_does_not_kill_healthy_child_async():
    import asyncio
    fp = FakeProvider("drip", timeout=1)

    async def run():
        out = []
        async for m in fp.astream("hi"):
            if m.kind == "text":
                out.append(m.text)
                await asyncio.sleep(2)  # slow async consumer (backpressure)
        return out

    assert asyncio.run(run()) == ["drip0", "drip1", "drip2", "drip3"]


# ---- _env: subscription-by-default + auth fallback ----

def test_env_strips_api_key_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    fp = FakeProvider("ok")
    assert "ANTHROPIC_API_KEY" not in fp._env(fallback_api_key=False)
    assert fp._env(fallback_api_key=True).get("ANTHROPIC_API_KEY") == "sk-should-not-leak"


def test_env_extra_env_wins_over_pop(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    fp = FakeProvider("ok", extra_env={"ANTHROPIC_API_KEY": "sk-deliberate"})
    assert fp._env(fallback_api_key=False)["ANTHROPIC_API_KEY"] == "sk-deliberate"


def test_default_stream_does_not_leak_inherited_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    fp = FakeProvider("echo_key")
    texts = [m.text for m in fp.stream("hi") if m.kind == "text"]
    assert texts == ["KEY=<none>"]  # key stripped from the child env


def test_stream_auth_fallback_retries_with_key(monkeypatch):
    # First invocation: key stripped → child emits auth error. Fallback retry:
    # key re-added → child succeeds. Exercises both the _env fix and the retry.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fp = FakeProvider("auth")
    texts = [m.text for m in fp.stream("hi") if m.kind == "text"]
    assert texts == ["ok-after-fallback"]


def test_stream_auth_no_key_no_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fp = FakeProvider("auth")
    with pytest.raises(UnifiedError) as ei:
        list(fp.stream("hi"))
    assert ei.value.kind == "auth_expired"


def test_oauth_only_provider_never_retries_with_inherited_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fp = FakeOAuthOnlyProvider("auth")
    assert "ANTHROPIC_API_KEY" not in fp._env(fallback_api_key=True)
    with pytest.raises(UnifiedError) as ei:
        list(fp.stream("hi"))
    assert ei.value.kind == "auth_expired"


# ---- async streaming (astream) ----

def test_astream_ok():
    import asyncio
    fp = FakeProvider("ok")

    async def run():
        return [m.kind async for m in fp.astream("hi")]

    assert asyncio.run(run()) == ["session", "text", "usage", "done"]


def test_concurrent_astreams_receive_distinct_parser_state():
    import asyncio
    fp = StatefulNormalizeProvider("ok")

    async def consume():
        return [message.text async for message in fp.astream("hi")
                if message.kind == "text"]

    async def run():
        return await asyncio.gather(consume(), consume())

    assert asyncio.run(run()) == [
        ["1:hello"], ["1:hello"],
    ]


def test_astream_hang_before_output_fails_fast():
    import asyncio
    fp = FakeProvider("hang", timeout=5, first_output_timeout=1)

    async def run():
        return [m async for m in fp.astream("hi")]

    t0 = time.time()
    with pytest.raises(UnifiedError) as ei:
        asyncio.run(run())
    assert ei.value.kind == "network"
    assert time.time() - t0 < 4


def test_async_protocol_keeps_delayed_final_line_within_exit_grace():
    import asyncio

    class FakeTransport:
        def __init__(self):
            self.closing = False

        def get_returncode(self):
            return 0

        def get_pid(self):
            return None

        def is_closing(self):
            return self.closing

        def close(self):
            self.closing = True

    async def run():
        loop = asyncio.get_running_loop()
        protocol = base._AsyncJsonlProtocol(
            loop,
            max_output_bytes=4096,
            max_stderr_bytes=4096,
            max_buffer_bytes=4096,
            max_events=10,
            max_line_bytes=4096,
        )
        transport = FakeTransport()
        protocol.connection_made(transport)
        protocol.process_exited()
        await asyncio.sleep(base._PIPE_EXIT_GRACE / 2)
        raw = (json.dumps(
            {"type": "text", "text": "늦은 🌟"}, ensure_ascii=False
        ) + "\n").encode("utf-8")
        split = raw.index("🌟".encode("utf-8")) + 2
        protocol.pipe_data_received(1, raw[:split])
        protocol.pipe_data_received(1, raw[split:])
        await asyncio.sleep(base._PIPE_EXIT_GRACE)
        queued = []
        while not protocol.queue.empty():
            queued.append(protocol.queue.get_nowait())
        return transport, queued

    transport, queued = asyncio.run(run())
    lines = [payload.decode("utf-8") for kind, payload in queued
             if kind == "line"]
    assert lines == [json.dumps(
        {"type": "text", "text": "늦은 🌟"}, ensure_ascii=False
    ) + "\n"]
    assert queued[-1][0] == "eof"
    assert transport.is_closing()


@pytest.mark.parametrize(
    "expected_reason,overrides,raw",
    [
        ("line", {"max_line_bytes": 4}, b"xxxx"),
        ("stdout", {"max_output_bytes": 2}, b"{}\n"),
        ("event_count", {"max_events": 0}, b"{}\n"),
        ("stream_buffer", {"max_buffer_bytes": 2}, b"{}\n"),
    ],
)
def test_stream_reader_post_exit_limit_does_not_create_or_replace_reason(
    expected_reason, overrides, raw
):
    def make_reader(terminated):
        limits = {
            "max_buffer_bytes": 1024,
            "max_output_bytes": 1024,
            "max_events": 10,
            "max_line_bytes": 1024,
        }
        limits.update(overrides)
        return base._StreamReader(
            object(),
            first_output=1,
            idle=1,
            terminate=lambda: terminated.append(True),
            **limits,
        )

    terminated = []
    reader = make_reader(terminated)
    assert reader._queue_line(raw, after_exit=True) is False
    assert reader.overflow_reason == ""
    assert terminated == []

    reader.overflow_reason = "preexisting"
    assert reader._queue_line(raw, after_exit=True) is False
    assert reader.overflow_reason == "preexisting"
    assert terminated == []

    pre_exit_terminated = []
    pre_exit_reader = make_reader(pre_exit_terminated)
    assert pre_exit_reader._queue_line(raw, after_exit=False) is False
    assert pre_exit_reader.overflow_reason == expected_reason
    assert pre_exit_terminated == [True]


# ---- bounded output / exact temp-file ownership ----

def test_stream_output_limit_terminates_child_promptly():
    fp = FakeProvider(
        "output_flood",
        timeout=10,
        max_output_bytes=1024,
        max_stream_buffer_bytes=1024,
    )
    t0 = time.time()
    with pytest.raises(UnifiedError) as ei:
        list(fp.stream("hi"))
    assert ei.value.kind == "resource_limit"
    assert time.time() - t0 < 5


@pytest.mark.parametrize("async_mode", [False, True])
def test_stream_oversized_line_is_resource_limit(async_mode):
    fp = FakeProvider(
        "oversized_line",
        timeout=2,
        max_stream_line_bytes=128,
        max_stream_buffer_bytes=1024,
    )

    with pytest.raises(UnifiedError) as error:
        if async_mode:
            import asyncio

            async def run():
                return [message async for message in fp.astream("hi")]

            asyncio.run(run())
        else:
            list(fp.stream("hi"))
    assert error.value.kind == "resource_limit"
    assert "line" in (error.value.cause or "")


def test_chat_output_limit_is_bounded():
    fp = FakeProvider("output_flood", timeout=10, max_output_bytes=1024)
    with pytest.raises(UnifiedError) as ei:
        fp.chat("hi")
    assert ei.value.kind == "resource_limit"


class _ScopedTempProvider(FakeProvider):
    def __init__(self):
        super().__init__("drip", timeout=5)
        self.materialized: list[str] = []

    def _build_args(self, prompt, *, session_id, resume_last, model,
                    streaming, images=None):
        fd, path = tempfile.mkstemp(prefix="unified-cli-scope-", suffix=".img")
        os.write(fd, b"image")
        os.close(fd)
        self._register_temp_file(path)
        self.materialized.append(path)
        return [sys.executable, "-c", _FAKE_CLI, self._mode], None


class _FailingTempProvider(_ScopedTempProvider):
    def _build_args(self, *args, **kwargs):
        super()._build_args(*args, **kwargs)
        raise RuntimeError("build failed after materialization")


def test_overlapping_astream_temp_scopes_do_not_cross_cleanup():
    import asyncio
    fp = _ScopedTempProvider()

    async def run():
        first = fp.astream("first", images=[b"one"])
        second = fp.astream("second", images=[b"two"])
        await first.__anext__()
        await second.__anext__()
        first_path, second_path = fp.materialized
        assert os.path.exists(first_path)
        assert os.path.exists(second_path)

        async for _ in first:
            pass
        after_first = (os.path.exists(first_path), os.path.exists(second_path))
        await second.aclose()
        after_second = os.path.exists(second_path)
        return after_first, after_second

    after_first, after_second = asyncio.run(run())
    assert after_first == (False, True)
    assert after_second is False


def test_astream_build_error_cleans_its_temp_scope():
    import asyncio
    fp = _FailingTempProvider()

    async def run():
        gen = fp.astream("broken", images=[b"image"])
        with pytest.raises(RuntimeError, match="build failed"):
            await gen.__anext__()

    asyncio.run(run())
    assert len(fp.materialized) == 1
    assert not os.path.exists(fp.materialized[0])


# ---- POSIX process-group cancellation ----

@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipe polling")
def test_chat_completion_ignores_detached_pipe_holder(monkeypatch, tmp_path):
    processes = _capture_popen(monkeypatch)
    pid_file = tmp_path / "holder.pid"
    fp = FakeProvider(
        "detached_pipe_holder", timeout=0.2,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    try:
        started = time.monotonic()
        assert fp.chat("hi").text == "detached-holder"
        elapsed = time.monotonic() - started
        assert elapsed < 0.8, elapsed
        assert len(processes) == 1
        _assert_popen_pipes_closed(processes[0])
    finally:
        _cleanup_detached_holder(pid_file)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipe polling")
def test_stream_completion_ignores_detached_pipe_holder(monkeypatch, tmp_path):
    processes = _capture_popen(monkeypatch)
    pid_file = tmp_path / "holder.pid"
    fp = FakeProvider(
        "detached_pipe_holder", timeout=0.2,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    try:
        started = time.monotonic()
        texts = [message.text for message in fp.stream("hi")
                 if message.kind == "text"]
        elapsed = time.monotonic() - started
        assert texts == ["detached-holder"]
        assert elapsed < 0.8, elapsed
        assert len(processes) == 1
        _assert_popen_pipes_closed(processes[0])
    finally:
        _cleanup_detached_holder(pid_file)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipe polling")
def test_astream_completion_ignores_detached_pipe_holder(monkeypatch, tmp_path):
    import asyncio
    pid_file = tmp_path / "holder.pid"
    fp = FakeProvider(
        "detached_pipe_holder", timeout=0.2,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    transports = _capture_async_transports(monkeypatch)

    async def run():
        texts = [message.text async for message in fp.astream("hi")
                 if message.kind == "text"]
        await asyncio.sleep(0)
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks()
                   if task is not current and not task.done()]
        return texts, pending

    try:
        started = time.monotonic()
        texts, pending = asyncio.run(run())
        elapsed = time.monotonic() - started
        assert texts == ["detached-holder"]
        assert pending == []
        assert elapsed < 0.8, elapsed
        assert len(transports) == 1
        assert transports[0].is_closing()
    finally:
        _cleanup_detached_holder(pid_file)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process exit")
@pytest.mark.parametrize("api", ["chat", "stream", "astream"])
def test_continuous_detached_writer_has_absolute_cutoff_without_limit(
    tmp_path, api
):
    pid_file = tmp_path / f"{api}-writer.pid"
    fp = FakeProvider(
        "detached_continuous_writer",
        timeout=0.3,
        max_output_bytes=256,
        max_stream_events=3,
        max_stream_buffer_bytes=1024,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    try:
        started = time.monotonic()
        if api == "chat":
            texts = [fp.chat("hi").text]
        elif api == "stream":
            texts = [message.text for message in fp.stream("hi")
                     if message.kind == "text"]
        else:
            import asyncio

            async def run():
                return [message.text async for message in fp.astream("hi")
                        if message.kind == "text"]

            texts = asyncio.run(run())
        elapsed = time.monotonic() - started
        assert texts and texts[0].startswith("leader")
        assert len(texts) < 20
        assert elapsed < 0.8, elapsed
    finally:
        _cleanup_detached_holder(pid_file)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process exit")
def test_gemini_stream_closes_pipes_with_detached_holder(
    monkeypatch, tmp_path
):
    from unified_cli.providers.gemini import GeminiProvider

    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    processes = _capture_popen(monkeypatch)
    pid_file = tmp_path / "gemini-holder.pid"
    provider = GeminiProvider(
        bin_path=sys.executable,
        timeout=0.2,
        conversations_dir=str(tmp_path / "conversations"),
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    script = r'''
import os, subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(5)"],
    start_new_session=True,
)
with open(os.environ["FAKE_CHILD_PID_FILE"], "w") as f:
    f.write(str(child.pid))
sys.stdout.write("gemini leader\n")
sys.stdout.flush()
'''
    try:
        started = time.monotonic()
        texts = [message.text for message in provider._stream_run(
            [sys.executable, "-c", script]
        ) if message.kind == "text"]
        elapsed = time.monotonic() - started
        assert texts == ["gemini leader\n"]
        assert elapsed < 0.8, elapsed
        assert len(processes) == 1
        _assert_popen_pipes_closed(processes[0])
    finally:
        _cleanup_detached_holder(pid_file)


def _wait_for_pid_exit(pid: int, timeout: float = 4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_stream_abort_kills_descendant_process():
    fp = FakeProvider("spawn_descendant", timeout=30)
    gen = fp.stream("hi")
    first = next(gen)
    pid = int(first.text.split("=", 1)[1])
    gen.close()
    assert _wait_for_pid_exit(pid)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_stream_abort_kills_descendant_after_parent_exits(monkeypatch):
    processes = _capture_popen(monkeypatch)
    fp = FakeProvider("spawn_then_exit", timeout=30)
    gen = fp.stream("hi")
    first = next(gen)
    pid = int(first.text.split("=", 1)[1])
    # Let the direct fake CLI exit while the descendant still owns the pipe.
    time.sleep(0.1)
    gen.close()
    assert _wait_for_pid_exit(pid)
    assert len(processes) == 1
    _assert_popen_pipes_closed(processes[0])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_astream_abort_kills_descendant_process():
    import asyncio
    fp = FakeProvider("spawn_descendant", timeout=30)

    async def run():
        gen = fp.astream("hi")
        first = await gen.__anext__()
        await gen.aclose()
        return int(first.text.split("=", 1)[1])

    assert _wait_for_pid_exit(asyncio.run(run()))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_astream_abort_kills_descendant_after_parent_exits(monkeypatch):
    import asyncio
    fp = FakeProvider("spawn_then_exit", timeout=30)
    transports = _capture_async_transports(monkeypatch)

    async def run():
        gen = fp.astream("hi")
        first = await gen.__anext__()
        await asyncio.sleep(0.1)
        await gen.aclose()
        return int(first.text.split("=", 1)[1])

    assert _wait_for_pid_exit(asyncio.run(run()))
    assert len(transports) == 1
    assert transports[0].is_closing()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_chat_timeout_kills_descendant_process(tmp_path):
    pid_file = tmp_path / "child.pid"
    fp = FakeProvider(
        "spawn_descendant",
        timeout=1,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    with pytest.raises(UnifiedError) as ei:
        fp.chat("hi")
    assert ei.value.kind == "network"
    assert pid_file.exists()
    assert _wait_for_pid_exit(int(pid_file.read_text()))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_chat_completion_retires_descendant_after_parent_exits(
    tmp_path, monkeypatch
):
    processes = _capture_popen(monkeypatch)
    pid_file = tmp_path / "child.pid"
    fp = FakeProvider(
        "spawn_then_exit",
        timeout=10,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    fp.chat("hi")
    assert pid_file.exists()
    assert _wait_for_pid_exit(int(pid_file.read_text()))
    assert len(processes) == 1
    _assert_popen_pipes_closed(processes[0])


# ---- hang-error diagnosis helper ----

def test_hang_error_is_network_and_names_provider():
    fp = FakeProvider("ok")
    err = fp._hang_error(before_output=True)
    assert err.kind == "network"
    assert "claude" in err.message


def test_keychain_block_not_suspected_off_darwin(monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "linux")
    fp = FakeProvider("ok")
    assert fp._keychain_block_suspected() is False
