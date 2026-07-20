"""Offline tests for the Core/Ext wheel-boundary verifier."""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_distribution_pair.py"
_SPEC = importlib.util.spec_from_file_location("verify_distribution_pair", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verify_distribution_pair = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = verify_distribution_pair
_SPEC.loader.exec_module(verify_distribution_pair)


def _wheel(
    path: Path,
    *,
    distribution: str,
    version: str,
    package_root: str,
    requirements: tuple[str, ...] = (),
    extra_members: tuple[str, ...] = (),
) -> Path:
    dist_root = distribution.replace("-", "_")
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {item}" for item in requirements)
    metadata.append("")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package_root}/__init__.py", "")
        archive.writestr(
            f"{dist_root}-{version}.dist-info/METADATA",
            "\n".join(metadata),
        )
        for member in extra_members:
            archive.writestr(member, "")
    return path


def _valid_pair(tmp_path: Path) -> tuple[Path, Path]:
    core = _wheel(
        tmp_path / "unified_cli-0.5.0-py3-none-any.whl",
        distribution="unified-cli",
        version="0.5.0",
        package_root="unified_cli",
    )
    ext = _wheel(
        tmp_path / "unified_cli_ext-0.1.0-py3-none-any.whl",
        distribution="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        requirements=("unified-cli>=0.5,<0.6",),
    )
    return core, ext


def test_valid_core_ext_pair_passes(tmp_path):
    core, ext = _valid_pair(tmp_path)

    inspected_core, inspected_ext = verify_distribution_pair.verify_pair(
        core,
        ext,
        core_version="0.5.0",
        ext_version="0.1.0",
    )

    assert inspected_core.distribution == "unified-cli"
    assert inspected_ext.distribution == "unified-cli-ext"


def test_ext_cannot_ship_core_paths(tmp_path):
    core, ext = _valid_pair(tmp_path)
    with zipfile.ZipFile(ext, "a") as archive:
        archive.writestr("unified_cli/override.py", "")

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="overlap|outside unified_cli_ext|contains unified_cli",
    ):
        verify_distribution_pair.verify_pair(core, ext)


def test_ext_requires_exactly_the_core_compatibility_line(tmp_path):
    core, _ = _valid_pair(tmp_path)
    ext = _wheel(
        tmp_path / "bad_ext-0.1.0-py3-none-any.whl",
        distribution="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        requirements=("unified-cli>=0.4",),
    )

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="0.5.x line",
    ):
        verify_distribution_pair.verify_pair(core, ext)


@pytest.mark.parametrize(
    "deceptive",
    (
        "unified-cli>=0.50,<0.60",
        "unified-cli>=0.5,<0.6,!=0.5.*",
        "unified-cli>=0.5,<0.6,>=0.5",
        "unified-cli>=0.5,<0.6; python_version >= '3.9'",
    ),
)
def test_ext_rejects_deceptive_core_requirement_bounds(tmp_path, deceptive):
    core, _ = _valid_pair(tmp_path)
    ext = _wheel(
        tmp_path / "deceptive_ext-0.1.0-py3-none-any.whl",
        distribution="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        requirements=(deceptive,),
    )

    with pytest.raises(verify_distribution_pair.VerificationError):
        verify_distribution_pair.verify_pair(core, ext)


def test_unsafe_wheel_member_is_rejected(tmp_path):
    wheel = _wheel(
        tmp_path / "unsafe-0.1.0-py3-none-any.whl",
        distribution="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        requirements=("unified-cli>=0.5,<0.6",),
        extra_members=("../escape.py",),
    )

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="unsafe wheel member",
    ):
        verify_distribution_pair.inspect_wheel(wheel)


def test_distribution_and_version_metadata_are_checked(tmp_path):
    core, ext = _valid_pair(tmp_path)

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="version",
    ):
        verify_distribution_pair.verify_pair(
            core, ext, core_version="0.5.1", ext_version="0.1.0"
        )


def test_cli_returns_nonzero_for_invalid_pair(tmp_path, capsys):
    core, ext = _valid_pair(tmp_path)

    result = verify_distribution_pair.main([
        str(core), str(ext), "--ext-version", "9.9.9",
    ])

    assert result == 1
    assert "distribution verification failed" in capsys.readouterr().err


def test_ext_publish_workflow_invokes_the_shared_pair_verifier():
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish-ext.yml"
    ).read_text(encoding="utf-8")
    assert "pip download --no-deps --only-binary=:all:" in workflow
    assert "scripts/verify_distribution_pair.py" in workflow
    assert '--core-version "${CORE_VERSION}"' in workflow


def test_core_sdist_excludes_repo_only_distribution_test():
    manifest = (Path(__file__).parents[1] / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests *.py" in manifest
    assert "exclude tests/test_distribution_pair.py" in manifest
