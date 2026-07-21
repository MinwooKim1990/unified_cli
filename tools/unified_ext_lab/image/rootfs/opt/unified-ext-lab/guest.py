#!/usr/local/bin/python3
"""Fixed synthetic guest actions for the scaffold-only extension lab image."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys


FIXTURE = "/opt/unified-ext-lab/fixtures/fake-provider"
FIXTURE_SHA256 = "50e7754c2a4cc5fb074d640eef253f5a9b61288dcbe8074887e2cc2c728edc66"
TOOL_DIRECTORY = "/opt/unified-ext-lab/tool"
TOOL = TOOL_DIRECTORY + "/fake-provider"
AUTH_DIRECTORY = "/home/lab"
WORKSPACE = "/workspace"
WORKSPACE_MARKER = WORKSPACE + "/project.marker"
WORKSPACE_MARKER_BYTES = b"synthetic-extension-lab-v1\n"


def _writable_owned_directory(path: str) -> bool:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid() == 65532
        and info.st_gid == os.getgid() == 65532
        and bool(info.st_mode & stat.S_IWUSR)
        and os.access(path, os.W_OK | os.X_OK)
    )


def _checksum(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _install() -> int:
    if not all(
        _writable_owned_directory(path) for path in (AUTH_DIRECTORY, TOOL_DIRECTORY)
    ):
        print("synthetic volume ownership mismatch", file=sys.stderr)
        return 4
    if _checksum(FIXTURE) != FIXTURE_SHA256:
        print("synthetic fixture checksum mismatch", file=sys.stderr)
        return 4
    os.makedirs(TOOL_DIRECTORY, mode=0o700, exist_ok=True)
    temporary = TOOL + ".new"
    shutil.copyfile(FIXTURE, temporary)
    os.chmod(temporary, 0o500)
    os.replace(temporary, TOOL)
    print(json.dumps({"action": "install", "status": "ok"}, sort_keys=True))
    return 0


def _test() -> int:
    if not os.path.isfile(TOOL) or _checksum(TOOL) != FIXTURE_SHA256:
        print("synthetic fixture is not installed", file=sys.stderr)
        return 4
    environment = {
        "HOME": "/home/lab",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/home/lab/.cache",
        "XDG_CONFIG_HOME": "/home/lab/.config",
        "XDG_DATA_HOME": "/home/lab/.local/share",
    }
    completed = subprocess.run(
        [TOOL, "protocol-check"],
        cwd=WORKSPACE,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
        timeout=10,
    )
    if completed.returncode != 0:
        print("synthetic fixture test failed", file=sys.stderr)
        return 6
    sys.stdout.buffer.write(completed.stdout)
    return 0


def _logout() -> int:
    marker = os.path.join(AUTH_DIRECTORY, "synthetic-auth.json")
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass
    print(json.dumps({"action": "logout", "status": "ok"}, sort_keys=True))
    return 0


def _workspace_ready() -> bool:
    if not _writable_owned_directory(WORKSPACE):
        return False
    descriptor = None
    try:
        descriptor = os.open(
            WORKSPACE_MARKER,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        payload = os.read(descriptor, len(WORKSPACE_MARKER_BYTES) + 1)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_gid == os.getgid()
        and payload == WORKSPACE_MARKER_BYTES
    )


def _ready() -> int:
    status = "ready" if _workspace_ready() else "waiting"
    print(json.dumps({"action": "ready", "status": status}, sort_keys=True))
    return 0


def _initialize_workspace() -> bool:
    if not _writable_owned_directory(WORKSPACE):
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(WORKSPACE_MARKER, flags, 0o600)
    except FileExistsError:
        descriptor = None
        try:
            descriptor = os.open(
                WORKSPACE_MARKER,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(descriptor)
            payload = os.read(descriptor, len(WORKSPACE_MARKER_BYTES) + 1)
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_gid == os.getgid()
            and payload == WORKSPACE_MARKER_BYTES
        )
    except OSError:
        return False
    try:
        written = 0
        while written < len(WORKSPACE_MARKER_BYTES):
            count = os.write(descriptor, WORKSPACE_MARKER_BYTES[written:])
            if count < 1:
                return False
            written += count
        os.fchmod(descriptor, 0o600)
        return True
    finally:
        os.close(descriptor)


def _idle() -> int:
    # PID 1 remains inert. Docker stop supplies the only lifecycle signal.
    import signal

    if not _initialize_workspace():
        print("synthetic workspace ownership mismatch", file=sys.stderr)
        return 4

    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    while not stopped:
        signal.pause()
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("one fixed guest action is required", file=sys.stderr)
        return 2
    actions = {
        "idle": _idle,
        "ready": _ready,
        "install": _install,
        "logout": _logout,
        "test": _test,
    }
    action = actions.get(sys.argv[1])
    if action is None:
        print("unsupported guest action", file=sys.stderr)
        return 3
    return action()


if __name__ == "__main__":
    raise SystemExit(main())
