# Test setup replaces this line with a pinned absolute interpreter shebang.
"""Offline fake provider; never imports or executes a real provider binary."""

import json
import os
import signal
import subprocess
import sys
import time


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


mode = sys.argv[1]

if mode == "events":
    emit({"type": "text_delta", "text": "hello"})
    emit({"type": "done", "reason": "complete"})
elif mode == "echo":
    for line in sys.stdin:
        emit(json.loads(line))
elif mode == "malformed":
    sys.stdout.write("{not json}\n")
    sys.stdout.flush()
elif mode == "nonfinite":
    sys.stdout.write('{"value":NaN}\n')
    sys.stdout.flush()
elif mode == "duplicate-key":
    sys.stdout.write('{"value":1,"value":2}\n')
    sys.stdout.flush()
elif mode == "unterminated":
    sys.stdout.write('{"value":1}')
    sys.stdout.flush()
elif mode == "flood":
    for index in range(10000):
        emit({"index": index, "payload": "x" * 80})
elif mode == "stderr-secret":
    sys.stderr.write("Authorization: Bearer " + os.environ.get("FAKE_TOKEN", "missing") + "\n")
    sys.stderr.flush()
    raise SystemExit(7)
elif mode == "stderr-flood-after-stdout-eof":
    # Make stdout EOF observable before a clean leader exit while stderr still
    # has enough buffered data to exceed a deliberately small transport cap.
    sys.stdout.close()
    os.write(sys.stderr.fileno(), b"stderr-flood\n" * 8192)
elif mode == "stderr-flood-nonzero":
    sys.stdout.close()
    os.write(sys.stderr.fileno(), b"stderr-flood-nonzero\n" * 8192)
    raise SystemExit(9)
elif mode == "env":
    emit({"env": dict(os.environ)})
elif mode == "hang":
    time.sleep(60)
elif mode == "term-ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
elif mode == "escaped-inherited-pipes":
    ready_read, ready_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_read)
        os.setsid()
        os.write(ready_write, b"1")
        os.close(ready_write)
        time.sleep(60)
        os._exit(0)
    os.close(ready_write)
    os.read(ready_read, 1)
    os.close(ready_read)
    emit({"child_pid": child_pid})
    time.sleep(60)
elif mode == "descendant":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    emit({"child_pid": child.pid})
    time.sleep(60)
elif mode == "no-read-descendant":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    emit({"child_pid": child.pid})
    # Deliberately leave stdin unread so the parent transport must enforce its
    # own deadline/cancellation while the OS pipe is full.
    time.sleep(60)
elif mode == "rpc":
    request = json.loads(sys.stdin.readline())
    emit({"jsonrpc": "2.0", "id": "server-1", "method": "permission", "params": {"risk": "low"}})
    reverse = json.loads(sys.stdin.readline())
    emit({"jsonrpc": "2.0", "id": request["id"], "result": {"reverse": reverse.get("result")}})
elif mode == "rpc-unmatched":
    request = json.loads(sys.stdin.readline())
    emit({"jsonrpc": "2.0", "id": request["id"] + 1, "result": None})
elif mode == "rpc-bool-id":
    request = json.loads(sys.stdin.readline())
    emit({"jsonrpc": "2.0", "id": True, "result": None})
elif mode == "rpc-twice":
    for _ in range(2):
        request = json.loads(sys.stdin.readline())
        emit({"jsonrpc": "2.0", "id": request["id"], "result": request.get("params")})
elif mode == "rpc-shape":
    request = json.loads(sys.stdin.readline())
    emit(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "has_params": "params" in request,
                "params": request.get("params"),
            },
        }
    )
elif mode == "rpc-error-missing-message":
    request = json.loads(sys.stdin.readline())
    emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": 7}})
elif mode == "rpc-error":
    request = json.loads(sys.stdin.readline())
    emit(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": 7, "message": "secret peer details"},
        }
    )
else:
    raise SystemExit(2)
