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
from unified_cli.plugin import ProviderServerPolicyV1  # noqa: E402
from unified_cli.registry import ProviderDescriptorV1  # noqa: E402


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
    extension = ProviderDescriptorV1(
        id="preview-ext",
        source="extension",
        status="loaded",
        support_status="preview",
        default_model="preview </script><img src=x onerror=alert(1)>",
        capabilities=frozenset(("chat", "models", "server")),
        route_prefixes=("preview-ext",),
        server_policy=ProviderServerPolicyV1(
            enabled=True, requires_external_isolation=False,
        ),
    )
    grok = ProviderDescriptorV1(
        id="grok",
        source="extension",
        status="discovered",
        support_status="preview",
        default_model=None,
        capabilities=frozenset(),
        route_prefixes=("grok",),
        server_policy=ProviderServerPolicyV1(
            enabled=False, requires_external_isolation=True,
        ),
    )
    manage.passive_bundled_provider_descriptors = lambda: (grok,)
    token = server.prepare_manage((str(WORKSPACE),))
    runtime = manage.get_manage_runtime()
    assert runtime is not None
    # Keep one constructor-injected-style Ext row to prove that arbitrary
    # metadata remains non-actionable beside the audited bundled Preview row.
    runtime._extension_provider_snapshots += (
        manage._copy_extension_provider_snapshot(extension),
    )
    runtime._extension_provider_ids = frozenset(
        (*runtime._extension_provider_ids, extension.id)
    )
    runtime.session_manager.upsert(
        provider="preview-ext",
        session_id="native-preview-session",
        model="preview-model",
        cwd=str(WORKSPACE),
        name="Ext metadata session",
    )
    runtime.session_manager.upsert(
        provider="orphan-ext",
        session_id="native-orphan-session",
        model="orphan-model",
        cwd=str(WORKSPACE),
        name="Uninjected Ext session",
    )
    try:
        print("READY {} {}".format(port, token), flush=True)
        uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        server.disable_manage()
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
