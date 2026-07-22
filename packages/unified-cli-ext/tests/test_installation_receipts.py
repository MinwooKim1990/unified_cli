import ast
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from unified_cli_ext import ConfigurationError
from unified_cli_ext.providers.installation import (
    INSTALLATION_RECEIPT_ABI_V1,
    DistributionTypeV1,
    InstallationReceiptKindV1,
    InstallationReceiptV1,
    VerifiedLaunchV1,
    installation_receipt_from_record,
    installation_receipt_to_record,
)
from unified_cli_ext.transports.security import ExecutableIdentity


def _copy_executable(path, name):
    target = path / name
    shutil.copyfile(os.path.realpath(sys.executable), target)
    target.chmod(0o700)
    return target


def _capture_direct(tmp_path, **changes):
    executable = changes.pop("executable", None)
    if executable is None:
        executable = _copy_executable(tmp_path, "vendor-cli")
    arguments = {
        "provider_id": "fixture-provider",
        "executable_path": str(executable),
        "executable_basename": "vendor-cli",
        "distribution_name": "Fixture Vendor CLI",
        "distribution_version": "1.2.3",
        "acquisition_source": "manual vendor download",
        "acquisition_url": "https://vendor.invalid/download",
    }
    arguments.update(changes)
    return InstallationReceiptV1.capture_direct(**arguments)


def _npm_layout(tmp_path, *, shebang="#!/usr/bin/env node\n", link=True):
    prefix = tmp_path / "prefix"
    package = prefix / "lib" / "node_modules" / "@scope" / "fixture"
    target = package / "bin" / "fixture.js"
    launcher = prefix / "bin" / "fixture"
    package.mkdir(parents=True)
    target.parent.mkdir()
    launcher.parent.mkdir(parents=True)
    target.write_text(shebang + "console.log('fixture')\n", encoding="utf-8")
    target.chmod(0o700)
    manifest = package / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "@scope/fixture",
                "version": "2.3.4-beta.1",
                "bin": {"fixture": "bin/fixture.js"},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    if link:
        launcher.symlink_to("../lib/node_modules/@scope/fixture/bin/fixture.js")
    else:
        shutil.copyfile(target, launcher)
        launcher.chmod(0o700)
        target = launcher
    interpreter = _copy_executable(prefix, "node")
    return {
        "prefix": prefix,
        "package": package,
        "target": target,
        "manifest": manifest,
        "launcher": launcher,
        "interpreter": interpreter,
    }


def _capture_npm(layout, **changes):
    arguments = {
        "provider_id": "fixture-provider",
        "launcher_path": str(layout["launcher"]),
        "executable_basename": "fixture",
        "package_root": str(layout["package"]),
        "ownership_root": str(layout["prefix"]),
        "distribution_name": "@scope/fixture",
        "distribution_version": "2.3.4-beta.1",
        "acquisition_source": "npm registry archive",
        "acquisition_url": "https://registry.npmjs.invalid/@scope/fixture",
        "interpreter_path": str(layout["interpreter"]),
    }
    arguments.update(changes)
    return InstallationReceiptV1.capture_npm(**arguments)


def test_direct_receipt_returns_immutable_verified_launch(tmp_path):
    receipt = _capture_direct(tmp_path)

    assert receipt.abi_version == INSTALLATION_RECEIPT_ABI_V1
    assert receipt.receipt_kind is InstallationReceiptKindV1.DIRECT_EXECUTABLE
    assert receipt.distribution_type is DistributionTypeV1.VENDOR_EXECUTABLE
    result = receipt.verify()
    assert type(result) is VerifiedLaunchV1
    assert result.argv_prefix == (receipt.canonical_launch_target,)
    assert type(result.executable_identity) is ExecutableIdentity
    assert result.executable_identity.path == result.argv_prefix[0]
    with pytest.raises(FrozenInstanceError):
        result.argv_prefix = ()
    with pytest.raises(FrozenInstanceError):
        receipt.provider_id = "changed"


def test_receipt_record_codec_round_trips_and_sanitizes_persistent_metadata(
    tmp_path,
):
    receipt = _capture_direct(tmp_path)

    record = installation_receipt_to_record(receipt)
    rebuilt = installation_receipt_from_record(record)
    assert rebuilt == receipt
    assert rebuilt.verify() == receipt.verify()

    persistent = installation_receipt_to_record(receipt, persistent=True)
    assert persistent["acquisition_source"] == "configured-local-installation"
    assert persistent["acquisition_url"] is None
    persisted_receipt = installation_receipt_from_record(persistent)
    assert persisted_receipt.provider_id == receipt.provider_id
    assert persisted_receipt.verify().argv_prefix == receipt.verify().argv_prefix


def test_receipt_record_codec_rejects_unknown_missing_and_changed_evidence(tmp_path):
    receipt = _capture_direct(tmp_path)
    record = installation_receipt_to_record(receipt)

    with_unknown = dict(record)
    with_unknown["unexpected"] = True
    with pytest.raises(ConfigurationError):
        installation_receipt_from_record(with_unknown)

    missing = dict(record)
    del missing["target_identity"]
    with pytest.raises(ConfigurationError):
        installation_receipt_from_record(missing)

    boolean_schema = dict(record)
    boolean_schema["schema"] = True
    with pytest.raises(ConfigurationError, match="schema"):
        installation_receipt_from_record(boolean_schema)

    changed = json.loads(json.dumps(record))
    changed["target_identity"]["sha256"] = "0" * 64
    with pytest.raises(ConfigurationError):
        installation_receipt_from_record(changed)


def test_explicit_direct_capture_is_complete_verified_receipt(tmp_path):
    executable = _copy_executable(tmp_path, "vendor-cli")
    receipt = InstallationReceiptV1.capture_explicit_direct(
        provider_id="fixture-provider",
        executable_path=str(executable),
        executable_basename="vendor-cli",
    )

    assert receipt.acquisition_url is None
    assert receipt.acquisition_source == "explicit-local-path"
    assert receipt.distribution_version == "0.0.0+explicit"
    assert receipt.verify().argv_prefix == (str(executable),)


def test_direct_rejects_symlink_and_detects_file_or_permission_change(tmp_path):
    executable = _copy_executable(tmp_path, "vendor-cli")
    symlink = tmp_path / "link-cli"
    symlink.symlink_to(executable.name)
    with pytest.raises(ConfigurationError):
        _capture_direct(
            tmp_path,
            executable=symlink,
            executable_basename="link-cli",
        )

    receipt = _capture_direct(tmp_path, executable=executable)
    executable.write_bytes(executable.read_bytes() + b"changed")
    with pytest.raises(ConfigurationError, match="changed"):
        receipt.verify()

    executable = _copy_executable(tmp_path, "second-cli")
    receipt = _capture_direct(
        tmp_path,
        executable=executable,
        executable_basename="second-cli",
    )
    executable.chmod(0o722)
    with pytest.raises(ConfigurationError, match="changed|permissions"):
        receipt.verify()


def test_direct_script_binds_absolute_shebang_interpreter(tmp_path):
    interpreter = _copy_executable(tmp_path, "vendor-python")
    executable = tmp_path / "vendor-cli"
    executable.write_text(
        "#!{}\nprint('fixture')\n".format(interpreter),
        encoding="utf-8",
    )
    executable.chmod(0o700)

    receipt = _capture_direct(tmp_path, executable=executable)
    assert receipt.interpreter_identity is not None
    assert receipt.interpreter_identity.path == str(interpreter)
    assert receipt.executable_identity.path == str(executable)
    assert (
        receipt.verify().executable_identity.interpreter
        == receipt.interpreter_identity
    )


def test_direct_rejects_unsafe_parent_and_malformed_explicit_metadata(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    executable = _copy_executable(unsafe, "vendor-cli")
    unsafe.chmod(0o777)
    try:
        with pytest.raises(ConfigurationError, match="permissions"):
            _capture_direct(tmp_path, executable=executable)
    finally:
        unsafe.chmod(0o700)

    for changes in (
        {"provider_id": True},
        {"distribution_version": "01.2.3"},
        {"distribution_version": "1.2"},
        {"acquisition_source": "x" * 1025},
        {"executable_path": "vendor-cli"},
    ):
        with pytest.raises(ConfigurationError):
            _capture_direct(tmp_path, **changes)

    receipt = _capture_direct(tmp_path)
    with pytest.raises(ConfigurationError, match="ABI"):
        replace(receipt, abi_version=True)
    with pytest.raises(ConfigurationError, match="kind"):
        replace(receipt, receipt_kind="direct_executable")


def test_npm_scoped_package_relative_symlink_and_env_interpreter(tmp_path):
    layout = _npm_layout(tmp_path)
    receipt = _capture_npm(layout)

    assert receipt.receipt_kind is InstallationReceiptKindV1.NPM_PACKAGE_LAUNCHER
    assert receipt.distribution_type is DistributionTypeV1.NPM_PACKAGE
    assert receipt.distribution_name == "@scope/fixture"
    assert receipt.package_manifest_identity is not None
    assert len(receipt.package_manifest_identity.sha256) == 64
    assert receipt.symlink_chain[0].target_text.startswith("../")
    assert receipt.canonical_launch_target == str(layout["target"])
    assert receipt.argv_prefix == (
        str(layout["interpreter"]),
        str(layout["target"]),
    )
    result = receipt.verify()
    assert result.argv_prefix == receipt.argv_prefix
    assert result.executable_identity == receipt.interpreter_identity
    assert result.executable_identity.path == str(layout["interpreter"])


@pytest.mark.parametrize(
    "change",
    [
        {"distribution_name": "@scope/other"},
        {"distribution_version": "2.3.5"},
        {"executable_basename": "other"},
    ],
)
def test_npm_rejects_expected_manifest_identity_or_bin_mismatch(tmp_path, change):
    layout = _npm_layout(tmp_path)
    with pytest.raises(ConfigurationError):
        _capture_npm(layout, **change)


def test_npm_rejects_manifest_bin_target_mismatch(tmp_path):
    layout = _npm_layout(tmp_path)
    other = layout["target"].with_name("other.js")
    other.write_text("#!/usr/bin/env node\nconsole.log('other')\n", encoding="utf-8")
    other.chmod(0o700)
    layout["manifest"].write_text(
        json.dumps(
            {
                "name": "@scope/fixture",
                "version": "2.3.4-beta.1",
                "bin": {"fixture": "bin/other.js"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="resolve"):
        _capture_npm(layout)


def test_npm_rejects_duplicate_nonfinite_deep_and_oversized_manifest(tmp_path):
    layout = _npm_layout(tmp_path)
    invalid_values = [
        (
            b'{"name":"@scope/fixture","name":"@scope/fixture",'
            b'"version":"2.3.4-beta.1",'
            b'"bin":{"fixture":"bin/fixture.js"}}'
        ),
        (
            b'{"name":"@scope/fixture","version":"2.3.4-beta.1",'
            b'"bin":{"fixture":"bin/fixture.js"},"bad":NaN}'
        ),
        (
            '{"name":"@scope/fixture","version":"2.3.4-beta.1",'
            '"bin":{"fixture":"bin/fixture.js"},"deep":'
            + "[" * 34
            + "0"
            + "]" * 34
            + "}"
        ).encode("utf-8"),
        b" " * (256 * 1024 + 1),
    ]
    for content in invalid_values:
        layout["manifest"].write_bytes(content)
        with pytest.raises(ConfigurationError):
            _capture_npm(layout)


def test_npm_detects_manifest_replacement_and_content_change(tmp_path):
    layout = _npm_layout(tmp_path)
    receipt = _capture_npm(layout)
    replacement = layout["manifest"].with_suffix(".replacement")
    replacement.write_bytes(layout["manifest"].read_bytes())
    replacement.chmod(0o600)
    os.replace(str(replacement), str(layout["manifest"]))
    with pytest.raises(ConfigurationError, match="manifest"):
        receipt.verify()

    layout = _npm_layout(tmp_path / "content")
    receipt = _capture_npm(layout)
    data = json.loads(layout["manifest"].read_text(encoding="utf-8"))
    data["description"] = "changed after capture"
    layout["manifest"].write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="manifest"):
        receipt.verify()


def test_npm_detects_target_interpreter_symlink_and_parent_changes(tmp_path):
    layout = _npm_layout(tmp_path / "target")
    receipt = _capture_npm(layout)
    layout["target"].write_text("#!/usr/bin/env node\nchanged\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="artifact|changed"):
        receipt.verify()

    layout = _npm_layout(tmp_path / "interpreter")
    receipt = _capture_npm(layout)
    layout["interpreter"].chmod(0o500)
    with pytest.raises(ConfigurationError, match="identity|changed"):
        receipt.verify()

    layout = _npm_layout(tmp_path / "symlink")
    receipt = _capture_npm(layout)
    layout["launcher"].unlink()
    layout["launcher"].symlink_to("../lib/node_modules/@scope/fixture/package.json")
    with pytest.raises(ConfigurationError, match="symlink|target"):
        receipt.verify()

    layout = _npm_layout(tmp_path / "parent")
    receipt = _capture_npm(layout)
    layout["package"].chmod(0o777)
    try:
        with pytest.raises(ConfigurationError, match="permissions|binding"):
            receipt.verify()
    finally:
        layout["package"].chmod(0o755)


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/usr/bin/env node --trace-warnings\n",
        "#!/usr/bin/env -S node\n",
        "#!/usr/bin/node --trace-warnings\n",
    ],
)
def test_npm_rejects_extra_shebang_dispatch_or_options(tmp_path, shebang):
    layout = _npm_layout(tmp_path, shebang=shebang)
    with pytest.raises(ConfigurationError, match="shebang"):
        _capture_npm(layout)


def test_npm_native_target_is_bound_directly(tmp_path):
    layout = _npm_layout(tmp_path, shebang="")
    shutil.copyfile(os.path.realpath(sys.executable), layout["target"])
    layout["target"].chmod(0o700)
    receipt = _capture_npm(layout, interpreter_path=None)
    assert receipt.argv_prefix == (str(layout["target"]),)
    assert receipt.interpreter_identity is None
    assert receipt.executable_identity.path == str(layout["target"])
    receipt.verify()


def test_npm_rejects_symlink_loop_escape_and_depth(tmp_path):
    loop = _npm_layout(tmp_path / "loop")
    loop["launcher"].unlink()
    loop["launcher"].symlink_to("fixture")
    with pytest.raises(ConfigurationError, match="recursive"):
        _capture_npm(loop)

    escape = _npm_layout(tmp_path / "escape")
    escape["launcher"].unlink()
    escape["launcher"].symlink_to("../../../outside")
    with pytest.raises(ConfigurationError, match="escapes"):
        _capture_npm(escape)

    deep = _npm_layout(tmp_path / "deep")
    deep["launcher"].unlink()
    chain_dir = deep["prefix"] / "chain"
    chain_dir.mkdir()
    paths = [chain_dir / "link-{}".format(index) for index in range(17)]
    deep["launcher"].symlink_to(os.path.relpath(str(paths[0]), str(deep["launcher"].parent)))
    for current, following in zip(paths, paths[1:]):
        current.symlink_to(following.name)
    paths[-1].symlink_to(
        os.path.relpath(str(deep["target"]), str(paths[-1].parent))
    )
    with pytest.raises(ConfigurationError, match="depth"):
        _capture_npm(deep)


def test_npm_requires_exact_types_and_bounded_text(tmp_path):
    layout = _npm_layout(tmp_path)
    for changes in (
        {"provider_id": True},
        {"launcher_path": Path(layout["launcher"])},
        {"interpreter_path": True},
        {"distribution_name": "@SCOPE/fixture"},
        {"distribution_version": "2.3"},
        {"acquisition_url": "x" * 4097},
    ):
        with pytest.raises(ConfigurationError):
            _capture_npm(layout, **changes)

    receipt = _capture_npm(layout)
    with pytest.raises(ConfigurationError, match="mode"):
        replace(receipt.target_identity, mode=True)
    with pytest.raises(ConfigurationError, match="size"):
        replace(receipt.target_identity, size=512 * 1024 * 1024 + 1)
    with pytest.raises(ConfigurationError, match="binding"):
        replace(receipt, symlink_chain=list(receipt.symlink_chain))


def test_capture_uses_no_subprocess_network_or_path_lookup(tmp_path, monkeypatch):
    layout = _npm_layout(tmp_path)

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("ambient or external operation was attempted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(os, "get_exec_path", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    monkeypatch.setenv("PATH", "/definitely/not/used")
    receipt = _capture_npm(layout)
    assert receipt.verify().argv_prefix[0] == str(layout["interpreter"])

    source = (
        Path(__file__).parents[1]
        / "src"
        / "unified_cli_ext"
        / "providers"
        / "installation.py"
    )
    imports = {
        alias.name
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "subprocess" not in imports
    assert "socket" not in imports


def test_capture_detects_injected_target_race_and_closes_faulted_fd(tmp_path, monkeypatch):
    import unified_cli_ext.providers.installation as installation

    layout = _npm_layout(tmp_path / "race")
    original_capture = installation._capture_artifact

    def racing_capture(path, **kwargs):
        result = original_capture(path, **kwargs)
        if path == str(layout["target"]):
            layout["target"].write_text("#!/usr/bin/env node\nraced\n", encoding="utf-8")
        return result

    monkeypatch.setattr(installation, "_capture_artifact", racing_capture)
    with pytest.raises(ConfigurationError, match="changed"):
        _capture_npm(layout)

    monkeypatch.undo()
    layout = _npm_layout(tmp_path / "fault")
    closed = []
    original_close = installation.os.close

    def faulted_read(descriptor, amount):
        del descriptor, amount
        raise OSError("injected read fault with private detail")

    def tracked_close(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(installation.os, "read", faulted_read)
    monkeypatch.setattr(installation.os, "close", tracked_close)
    with pytest.raises(ConfigurationError, match="could not be captured") as caught:
        _capture_npm(layout)
    assert "private detail" not in str(caught.value)
    assert closed
