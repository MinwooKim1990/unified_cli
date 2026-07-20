#!/usr/bin/env python3
"""Offline fake provider; never imports or executes a real provider binary."""

import json
import os
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
elif mode == "env":
    emit({"env": dict(os.environ)})
elif mode == "hang":
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
