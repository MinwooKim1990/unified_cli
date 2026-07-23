import ast
import asyncio
import importlib
import pathlib
import subprocess
import sys
import traceback

import pytest

EXT_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(EXT_SOURCE) not in sys.path:
    sys.path.insert(0, str(EXT_SOURCE))

import unified_cli_ext
from unified_cli_ext import (
    AcpSdkAdapter,
    LimitExceeded,
    McpCallableBridge,
    OptionalDependencyError,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
)


def test_public_package_api_and_version():
    assert unified_cli_ext.__version__ == "0.5.3"
    assert "JsonlProcess" in unified_cli_ext.__all__
    assert "EventNormalizer" in unified_cli_ext.__all__
    assert "AcpSdkAdapter" in unified_cli_ext.__all__
    assert "McpCallableBridge" in unified_cli_ext.__all__


def test_import_has_no_optional_import_or_process_network_probe():
    source = pathlib.Path(unified_cli_ext.__file__).parents[1]
    script = """
import socket, subprocess, sys
sys.path.insert(0, {!r})
def blocked(*args, **kwargs):
    raise AssertionError('import performed a process/network probe')
subprocess.Popen = blocked
socket.create_connection = blocked
import unified_cli_ext
assert 'acp' not in sys.modules
assert 'mcp' not in sys.modules
print(unified_cli_ext.__version__)
""".format(str(source))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.stdout.strip() == "0.5.3"


def test_optional_dependency_absent_errors_are_clear(monkeypatch):
    acp_module = importlib.import_module("unified_cli_ext.transports.acp")
    mcp_module = importlib.import_module("unified_cli_ext.tools.mcp_bridge")
    original = importlib.import_module

    def missing(name, *args, **kwargs):
        if name in {"acp", "mcp"}:
            raise ModuleNotFoundError("missing optional SDK", name=name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(
        OptionalDependencyError, match=r"unified-cli\[acp\]"
    ):
        acp_module.require_acp_sdk()
    with pytest.raises(
        OptionalDependencyError, match=r"unified-cli\[mcp\]"
    ):
        mcp_module.require_mcp_sdk()


@pytest.mark.skipif(
    not ((3, 10) <= sys.version_info < (3, 15)),
    reason="ACP SDK import failures require a supported Python runtime",
)
def test_broken_optional_sdk_import_is_not_reported_as_uninstalled(monkeypatch):
    acp_module = importlib.import_module("unified_cli_ext.transports.acp")

    def broken(name, *args, **kwargs):
        raise ModuleNotFoundError("broken transitive dependency", name="sdk_dependency")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(TransportError, match="import failed") as caught:
        acp_module.require_acp_sdk()
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("module_name", "loader_name", "runtime_supported"),
    (
        (
            "unified_cli_ext.transports.acp",
            "require_acp_sdk",
            (3, 10) <= sys.version_info < (3, 15),
        ),
        (
            "unified_cli_ext.tools.mcp_bridge",
            "require_mcp_sdk",
            sys.version_info >= (3, 10),
        ),
    ),
)
def test_optional_sdk_runtime_import_failure_is_redacted(
    monkeypatch, module_name, loader_name, runtime_supported
):
    if not runtime_supported:
        pytest.skip("SDK import failures require a supported Python runtime")
    module = importlib.import_module(module_name)
    secret = "secret-sdk-import"

    def broken(name, *args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(TransportError, match="SDK import failed") as caught:
        getattr(module, loader_name)()
    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert secret not in str(caught.value)
    assert secret not in rendered
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("module_name", "loader_name", "runtime_supported", "requirement"),
    (
        (
            "unified_cli_ext.transports.acp",
            "require_acp_sdk",
            (3, 10) <= sys.version_info < (3, 15),
            r"Python >=3\.10,<3\.15",
        ),
        (
            "unified_cli_ext.tools.mcp_bridge",
            "require_mcp_sdk",
            sys.version_info >= (3, 10),
            r"Python >=3\.10",
        ),
    ),
)
def test_optional_sdk_unsupported_python_fails_closed_before_import(
    monkeypatch, module_name, loader_name, runtime_supported, requirement
):
    if runtime_supported:
        pytest.skip("runtime is supported by this optional SDK")
    module = importlib.import_module(module_name)
    import_attempts = []

    def unexpected_import(name, *args, **kwargs):
        import_attempts.append(name)
        raise AssertionError("unsupported runtime attempted an SDK import")

    monkeypatch.setattr(importlib, "import_module", unexpected_import)
    with pytest.raises(OptionalDependencyError, match=requirement):
        getattr(module, loader_name)()
    assert import_attempts == []


def test_acp_adapter_is_lazy_official_sdk_wrapper(monkeypatch):
    module = importlib.import_module("unified_cli_ext.transports.acp")
    marker = object()
    monkeypatch.setattr(module, "require_acp_sdk", lambda: marker)
    adapter = AcpSdkAdapter(lambda sdk: {"sdk": sdk})
    assert not adapter.loaded
    assert adapter.open() == {"sdk": marker}
    assert adapter.loaded
    empty = AcpSdkAdapter(lambda sdk: None)
    assert empty.open() is None
    assert empty.loaded
    broken = AcpSdkAdapter(lambda sdk: (_ for _ in ()).throw(RuntimeError("private")))
    with pytest.raises(TransportError, match="factory failed") as caught:
        broken.open()
    assert caught.value.__cause__ is None


class FakeResult:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode):
        assert mode == "json"
        return {"value": self.value}


class FakeSession:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResult(arguments)


def test_mcp_bridge_allowlist_bounds_timeout_cancel_and_sync_async(monkeypatch):
    module = importlib.import_module("unified_cli_ext.tools.mcp_bridge")
    monkeypatch.setattr(module, "require_mcp_sdk", lambda: object())
    session = FakeSession()
    bridge = McpCallableBridge(session, ["safe"], max_input_bytes=50, max_output_bytes=100)
    assert bridge.call("safe", {"x": 1}) == {"value": {"x": 1}}
    with pytest.raises(ProtocolError, match="allowlisted"):
        bridge.call("unsafe")
    with pytest.raises(LimitExceeded):
        bridge.call("safe", {"x": "z" * 100})
    with pytest.raises(LimitExceeded):
        McpCallableBridge(session, ("tool{}".format(i) for i in range(1025)))
    with pytest.raises(ProtocolError, match="allowlist"):
        McpCallableBridge(session, "safe")

    slow = McpCallableBridge(FakeSession(delay=1), ["safe"], timeout=0.05)
    with pytest.raises(TransportTimeout):
        asyncio.run(slow.call_async("safe"))

    token = unified_cli_ext.CancellationToken()
    cancelled = McpCallableBridge(FakeSession(delay=1), ["safe"], cancellation=token)

    async def cancel_call():
        task = asyncio.create_task(cancelled.call_async("safe"))
        await asyncio.sleep(0.02)
        token.cancel()
        return await task

    with pytest.raises(TransportCancelled):
        asyncio.run(cancel_call())


def test_all_source_files_parse_with_python_39_grammar():
    root = pathlib.Path(unified_cli_ext.__file__).parent
    files = sorted(root.rglob("*.py"))
    assert files
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


def test_root_pyproject_owns_both_namespaces_and_ext_provider_entry_points():
    repository_root = pathlib.Path(__file__).resolve().parents[3]
    pyproject_path = repository_root / "pyproject.toml"
    text = pyproject_path.read_text(encoding="utf-8")

    assert 'name = "unified-cli"' in text
    assert 'where = ["src", "packages/unified-cli-ext/src"]' in text
    assert '"unified_cli",' in text
    assert '"unified_cli_ext",' in text
    assert '"agent-client-protocol>=0.11,<0.12; python_version >= \'3.10\'' in text
    assert '"mcp>=1.27,<2; python_version >= \'3.10\'"' in text
    assert '[project.optional-dependencies]' in text
    assert 'acp = [' in text
    assert 'mcp = [' in text

    entry_point_section = text.split(
        '[project.entry-points."unified_cli.providers.v1"]', 1
    )[1].split('[tool.setuptools.dynamic]', 1)[0]
    entry_points = [
        line for line in entry_point_section.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(entry_points) == 18
    assert all(' = "unified_cli_ext.providers.' in line for line in entry_points)

    project_dependencies = text.split("dependencies = [", 1)[1].split("]\n", 1)[0]
    assert "unified-cli-ext" not in project_dependencies
    assert '"unified-cli"' not in project_dependencies
    assert not (repository_root / "packages" / "unified-cli-ext" / "pyproject.toml").exists()
    assert (pathlib.Path(unified_cli_ext.__file__).parent / "py.typed").exists()
