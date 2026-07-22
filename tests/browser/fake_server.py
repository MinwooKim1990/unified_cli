"""Isolated dashboard fixture for Playwright; provider work is a hard failure."""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from importlib.metadata import EntryPoint
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_HOME = Path(tempfile.mkdtemp(prefix="unified-cli-browser-home-"))
WORKSPACE = TEST_HOME / "workspace"
WORKSPACE.mkdir()
os.environ.update({
    "HOME": str(TEST_HOME),
    "XDG_CONFIG_HOME": str(TEST_HOME / "config"),
    "XDG_CACHE_HOME": str(TEST_HOME / "cache"),
    "UNIFIED_CLI_DISABLE_PLUGINS": "1",
})
sys.path.insert(0, str(ROOT / "src"))

from unified_cli import manage, registry, server  # noqa: E402


def _unexpected_provider_probe(*_args, **_kwargs):
    raise AssertionError("the browser initial load must not probe a provider")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    import uvicorn

    # Provider descriptors are metadata-only and are permitted on load.  Any
    # subprocess/model probe or extension module import is not: it becomes a
    # loud fixture failure rather than a live credential or provider action.
    server.collect_states = lambda: []
    server.list_models = _unexpected_provider_probe
    manage.list_models = _unexpected_provider_probe
    manage.subprocess.Popen = _unexpected_provider_probe
    registry.load_provider_plugin = _unexpected_provider_probe
    EntryPoint.load = _unexpected_provider_probe

    port = _free_loopback_port()
    token = server.prepare_manage((str(WORKSPACE),))
    try:
        print("READY {} {}".format(port, token), flush=True)
        uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        server.disable_manage()
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
