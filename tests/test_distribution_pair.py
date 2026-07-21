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
    dist_info_root: str | None = None,
    package_member: str | None = None,
) -> Path:
    dist_root = distribution.replace("-", "_")
    metadata_root = dist_info_root or f"{dist_root}-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {item}" for item in requirements)
    metadata.append("")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(package_member or f"{package_root}/__init__.py", "")
        archive.writestr(
            f"{metadata_root}/METADATA",
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


@pytest.mark.parametrize(
    "forbidden_member",
    (
        "tools/unified_ext_lab/state.py",
        "scripts/unified-ext-lab",
    ),
)
def test_synthetic_core_wheel_rejects_source_only_lab_paths(
    tmp_path, forbidden_member
):
    core = _wheel(
        tmp_path / "unified_cli-0.5.0-py3-none-any.whl",
        distribution="unified-cli",
        version="0.5.0",
        package_root="unified_cli",
        extra_members=(forbidden_member,),
    )
    ext = _wheel(
        tmp_path / "unified_cli_ext-0.1.0-py3-none-any.whl",
        distribution="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        requirements=("unified-cli>=0.5,<0.6",),
    )

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="outside unified_cli",
    ):
        verify_distribution_pair.verify_pair(core, ext)


def test_core_rejects_any_unexpected_top_level_path(tmp_path):
    core, ext = _valid_pair(tmp_path)
    with zipfile.ZipFile(core, "a") as archive:
        archive.writestr("unexpected_payload/module.py", "")

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="outside unified_cli",
    ):
        verify_distribution_pair.verify_pair(core, ext)


@pytest.mark.parametrize(
    ("target", "package_root"),
    (("core", "unified_cli"), ("ext", "unified_cli_ext")),
)
def test_package_root_must_be_a_real_package_directory(
    tmp_path, target, package_root
):
    core, ext = _valid_pair(tmp_path)
    if target == "core":
        core = _wheel(
            tmp_path / "single-file-core.whl",
            distribution="unified-cli",
            version="0.5.0",
            package_root=package_root,
            package_member=package_root,
        )
    else:
        ext = _wheel(
            tmp_path / "single-file-ext.whl",
            distribution="unified-cli-ext",
            version="0.1.0",
            package_root=package_root,
            requirements=("unified-cli>=0.5,<0.6",),
            package_member=package_root,
        )

    with pytest.raises(
        verify_distribution_pair.VerificationError,
        match="does not contain",
    ):
        verify_distribution_pair.verify_pair(core, ext)


@pytest.mark.parametrize(
    ("target", "mismatched_root", "message"),
    (
        ("core", "some_other_name-9.9.dist-info", "Core wheel"),
        ("ext", "unified_cli_ext-9.9.dist-info", "Ext wheel"),
    ),
)
def test_metadata_directory_must_match_name_and_version(
    tmp_path, target, mismatched_root, message
):
    core, ext = _valid_pair(tmp_path)
    if target == "core":
        core = _wheel(
            tmp_path / "mismatched-core.whl",
            distribution="unified-cli",
            version="0.5.0",
            package_root="unified_cli",
            dist_info_root=mismatched_root,
        )
    else:
        ext = _wheel(
            tmp_path / "mismatched-ext.whl",
            distribution="unified-cli-ext",
            version="0.1.0",
            package_root="unified_cli_ext",
            requirements=("unified-cli>=0.5,<0.6",),
            dist_info_root=mismatched_root,
        )

    with pytest.raises(verify_distribution_pair.VerificationError, match=message):
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


def test_core_sdist_excludes_unified_ext_lab():
    manifest = (Path(__file__).parents[1] / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tools" in manifest
    assert "exclude scripts/unified-ext-lab" in manifest
