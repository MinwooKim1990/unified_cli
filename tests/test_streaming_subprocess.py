"""Integration tests for the subprocess streaming core (base.py + watchdog).

These spawn a REAL child process (a tiny fake CLI run via `sys.executable -c`)
so the actual Popen / stderr-drain / watchdog / kill-on-abort / auth-fallback
paths are exercised — code that previously had zero coverage. The 0.3.0
connectivity work (output watchdog, stdin=DEVNULL, utf-8, API-key stripping)
regresses here.
"""

from __future__ import annotations

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

    def _parse_json_response(self, text, model):  # pragma: no cover - unused here
        return Response(text="", session_id="", provider=self.name,
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


# ---- happy path ----

def test_stream_ok_yields_all_events():
    fp = FakeProvider("ok")
    kinds = [m.kind for m in fp.stream("hi")]
    assert kinds == ["session", "text", "usage", "done"]


def test_stream_unicode_roundtrips():
    fp = FakeProvider("unicode")
    texts = [m.text for m in fp.stream("hi") if m.kind == "text"]
    assert texts == ["안녕 🌟 café"]


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
def test_stream_abort_kills_descendant_after_parent_exits():
    fp = FakeProvider("spawn_then_exit", timeout=30)
    gen = fp.stream("hi")
    first = next(gen)
    pid = int(first.text.split("=", 1)[1])
    # Let the direct fake CLI exit while the descendant still owns the pipe.
    time.sleep(0.1)
    gen.close()
    assert _wait_for_pid_exit(pid)


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
def test_astream_abort_kills_descendant_after_parent_exits():
    import asyncio
    fp = FakeProvider("spawn_then_exit", timeout=30)

    async def run():
        gen = fp.astream("hi")
        first = await gen.__anext__()
        await asyncio.sleep(0.1)
        await gen.aclose()
        return int(first.text.split("=", 1)[1])

    assert _wait_for_pid_exit(asyncio.run(run()))


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
def test_chat_completion_retires_descendant_after_parent_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    fp = FakeProvider(
        "spawn_then_exit",
        timeout=10,
        extra_env={"FAKE_CHILD_PID_FILE": str(pid_file)},
    )
    fp.chat("hi")
    assert pid_file.exists()
    assert _wait_for_pid_exit(int(pid_file.read_text()))


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
