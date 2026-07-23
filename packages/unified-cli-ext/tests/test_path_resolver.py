from __future__ import annotations

import json
import os
from pathlib import Path

from unified_cli_ext.providers.path_resolver import resolve_path_installation


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def test_resolves_direct_preview_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "demo"
    _executable(binary, "#!{}\nexit 0\n".format(os.path.realpath("/bin/sh")))
    monkeypatch.setenv("PATH", str(tmp_path))

    receipt = resolve_path_installation(
        provider_id="demo", executable="demo"
    )

    assert receipt.provider_id == "demo"
    receipt.verify()


def test_resolves_npm_preview_launcher(tmp_path: Path, monkeypatch) -> None:
    prefix = tmp_path / "prefix"
    package = prefix / "lib" / "node_modules" / "@demo" / "cli"
    target = package / "dist" / "cli.js"
    target.parent.mkdir(parents=True)
    _executable(target, "#!{}\nexit 0\n".format(os.path.realpath("/bin/sh")))
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@demo/cli",
                "version": "1.2.3",
                "bin": {"demo": "dist/cli.js"},
            }
        )
    )
    bindir = prefix / "bin"
    bindir.mkdir()
    (bindir / "demo").symlink_to(target)
    monkeypatch.setenv("PATH", str(bindir))

    receipt = resolve_path_installation(
        provider_id="demo",
        executable="demo",
        package_names=("@demo/cli",),
    )

    assert receipt.distribution_name == "@demo/cli"
    assert receipt.distribution_version == "1.2.3"
    receipt.verify()


def test_resolves_npm_launcher_when_target_keeps_executable_basename(
    tmp_path: Path, monkeypatch
) -> None:
    prefix = tmp_path / "prefix"
    package = prefix / "lib" / "node_modules" / "@demo" / "cli"
    target = package / "bin" / "demo"
    target.parent.mkdir(parents=True)
    interpreter = os.path.realpath("/bin/sh")
    _executable(target, "#!{}\nexit 0\n".format(interpreter))
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@demo/cli",
                "version": "1.2.3",
                "bin": {"demo": "bin/demo"},
            }
        )
    )
    bindir = prefix / "bin"
    bindir.mkdir()
    (bindir / "demo").symlink_to(target)
    monkeypatch.setenv("PATH", str(bindir))

    receipt = resolve_path_installation(
        provider_id="demo",
        executable="demo",
        package_names=("@demo/cli",),
    )

    assert receipt.receipt_kind.value == "npm_package_launcher"
    assert receipt.argv_prefix == (interpreter, str(target))
    receipt.verify()


def test_resolves_direct_installer_symlink_when_target_keeps_basename(
    tmp_path: Path, monkeypatch
) -> None:
    install = tmp_path / "vendor" / "versions" / "1.2.3"
    target = install / "demo"
    target.parent.mkdir(parents=True)
    _executable(target, "#!{}\nexit 0\n".format(os.path.realpath("/bin/sh")))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "demo").symlink_to(target)
    monkeypatch.setenv("PATH", str(bindir))

    receipt = resolve_path_installation(
        provider_id="demo",
        executable="demo",
        package_names=("@demo/cli",),
    )

    assert receipt.receipt_kind.value == "direct_executable"
    assert receipt.provider_id == "demo"
    receipt.verify()
