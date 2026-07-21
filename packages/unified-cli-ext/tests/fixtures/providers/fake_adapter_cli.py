# Test setup replaces this line with a pinned absolute interpreter shebang.
"""Offline Stage 5 fixture; it never imports or invokes a real provider."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


mode = sys.argv[1]
identity = "wrong-provider" if "--wrong-identity" in sys.argv else "fixture-provider"

if mode == "--version-json":
    if "--prerelease" in sys.argv:
        version = "2.1.0-rc1"
    elif "--leading-zero" in sys.argv:
        version = "02.4.1"
    elif "--empty-prerelease" in sys.argv:
        version = "2.4.1-rc..1"
    elif "--numeric-prerelease-zero" in sys.argv:
        version = "2.4.1-01"
    elif "--oversized-component" in sys.argv:
        version = "1000001.0"
    else:
        version = "2.4.1"
    emit({"provider": identity, "version": version})
elif mode == "--version-single-json":
    sys.stdout.write(json.dumps({"provider": identity, "version": "2.4.1"}))
elif mode == "--version":
    if "--prose-marker" in sys.argv:
        sys.stdout.write("unsupported prose: {} 2.4.1\n".format(identity))
    elif "--duplicate-field" in sys.argv:
        sys.stdout.write("{0} 2.4.1\n{0} 9.9.9\n".format(identity))
    else:
        sys.stdout.write("{} 2.4.1\n".format(identity))
elif mode == "--help":
    if "--prose-markers" in sys.argv:
        sys.stdout.write(
            "{}\nunsupported --auth --chat --models --sessions prose\n".format(
                identity
            )
        )
    elif "--wrong-prose-identity" in sys.argv:
        sys.stdout.write(
            "not-{}\n  --auth\n  --chat\n  --models\n  --sessions\n".format(
                identity
            )
        )
    else:
        sys.stdout.write(
            "{}\n  --auth\n  --chat\n  --models\n  --sessions\n".format(identity)
        )
elif mode == "--features-json":
    emit(
        {
            "provider": identity,
            "features": ["auth", "chat", "models", "sessions"],
        }
    )
elif mode == "--doctor-json":
    emit({"provider": identity, "ok": True})
elif mode == "--models-json":
    emit({"provider": identity, "models": ["fixture-small", "fixture-large"]})
elif mode == "--auth-json":
    state = Path(os.environ["HOME"]) / "fixture-authenticated"
    emit(
        {
            "provider": identity,
            "authenticated": os.environ.get("FIXTURE_AUTH") == "ready" or state.exists(),
            "visible_env": sorted(os.environ),
        }
    )
elif mode == "--single-json":
    sys.stdout.write(json.dumps({"provider": identity, "ok": True}))
elif mode == "--exit-ok":
    raise SystemExit(0)
elif mode == "--jsonl-hang":
    emit({"provider": identity, "stream": "ready"})
    time.sleep(30)
elif mode == "--malformed-json":
    sys.stdout.write("{not-json}\n")
    sys.stdout.flush()
elif mode == "--metadata-json":
    emit({"provider": identity, "unexpected": True})
elif mode == "auth":
    state = Path(os.environ["HOME"]) / "fixture-authenticated"
    if sys.argv[2] == "login":
        state.write_text("ready", encoding="utf-8")
    elif sys.argv[2] == "logout":
        if state.exists():
            state.unlink()
    else:
        raise SystemExit(2)
    emit({"home": os.environ["HOME"], "tmpdir": os.environ["TMPDIR"]})
elif mode == "chat":
    stdin_text = sys.stdin.read()
    emit(
        {
            "provider": identity,
            "argv": sys.argv[2:],
            "stdin": stdin_text,
            "home": os.environ["HOME"],
            "tmpdir": os.environ["TMPDIR"],
            "cwd": os.getcwd(),
            "project_marker": (
                Path("project.marker").read_text(encoding="utf-8")
                if Path("project.marker").is_file()
                else None
            ),
        }
    )
elif mode == "--process-hang":
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
elif mode == "--process-term-ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
elif mode == "--process-flood":
    chunk = b"x" * 65536
    for _ in range(64):
        os.write(sys.stdout.fileno(), chunk)
elif mode == "--process-descendant":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        shell=False,
    )
    Path(sys.argv[2]).write_text(
        "{} {}".format(os.getpid(), child.pid), encoding="utf-8"
    )
    time.sleep(30)
elif mode == "--process-detached-pipe":
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        time.sleep(0.5)
        os._exit(0)
    Path(sys.argv[2]).write_text(str(child_pid), encoding="utf-8")
elif mode == "--process-executed":
    Path(sys.argv[2]).write_text(os.getcwd(), encoding="utf-8")
else:
    raise SystemExit(2)
