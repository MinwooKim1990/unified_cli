"""Adversarial tests for release artifact identity and archive integrity."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_release_artifacts.py"
_SPEC = importlib.util.spec_from_file_location("verify_release_artifacts", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verify_release_artifacts = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = verify_release_artifacts
_SPEC.loader.exec_module(verify_release_artifacts)


def _metadata(name, version, dependency=None):
    lines = [
        "Metadata-Version: 2.4",
        "Name: " + name,
        "Version: " + version,
    ]
    if dependency is not None:
        dependencies = (dependency,) if isinstance(dependency, str) else dependency
        for item in dependencies:
            lines.append("Requires-Dist: " + item)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _record_hash(payload, algorithm="sha256"):
    digest = hashlib.new(algorithm, payload).digest()
    return algorithm + "=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _write_wheel(
    path,
    *,
    name,
    version,
    package_root,
    dependency=None,
    extra=(),
    prepend=(),
    omit=(),
    record_algorithm="sha256",
    record_mutator=None,
    metadata_dependencies=None,
    metadata_name=None,
    metadata_version=None,
):
    normalized = name.replace("-", "_")
    dist_info = normalized + "-" + version + ".dist-info"
    entries = list(prepend) + [
        (package_root + "/__init__.py", b""),
        (
            dist_info + "/METADATA",
            _metadata(
                metadata_name or name,
                metadata_version or version,
                dependency if metadata_dependencies is None else metadata_dependencies,
            ),
        ),
        (
            dist_info + "/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        ),
    ]
    entries.extend(extra)
    entries = [(member, payload) for member, payload in entries if member not in omit]
    record_path = dist_info + "/RECORD"
    rows = [
        [member, _record_hash(payload, record_algorithm), str(len(payload))]
        for member, payload in entries
    ]
    rows.append([record_path, "", ""])
    if record_mutator is not None:
        rows = record_mutator(rows)
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    if record_path not in omit:
        entries.append((record_path, stream.getvalue().encode("utf-8")))
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in entries:
            archive.writestr(member, payload)


def _add_tar_directory(archive, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    archive.addfile(info)


def _add_tar_bytes(archive, name, payload):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _write_sdist(
    path,
    *,
    name,
    version,
    package_root,
    dependency=None,
    root=None,
    extra_sources=(),
    extra_members=(),
    egg_dependencies=None,
    metadata_name=None,
    metadata_version=None,
    root_dependencies=None,
):
    normalized = name.replace("-", "_")
    root = root or normalized + "-" + version
    egg_info = normalized + ".egg-info"
    root_metadata = _metadata(
        metadata_name or name,
        metadata_version or version,
        dependency if root_dependencies is None else root_dependencies,
    )
    egg_metadata = _metadata(
        metadata_name or name,
        metadata_version or version,
        dependency if egg_dependencies is None else egg_dependencies,
    )
    with tarfile.open(path, "w:gz") as archive:
        for directory in (
            root,
            root + "/src",
            root + "/src/" + package_root,
            root + "/src/" + egg_info,
        ):
            _add_tar_directory(archive, directory)
        _add_tar_bytes(archive, root + "/PKG-INFO", root_metadata)
        _add_tar_bytes(
            archive, root + "/src/" + package_root + "/__init__.py", b""
        )
        _add_tar_bytes(
            archive, root + "/src/" + egg_info + "/PKG-INFO", egg_metadata
        )
        for source_root in extra_sources:
            _add_tar_directory(archive, root + "/src/" + source_root)
            _add_tar_bytes(
                archive, root + "/src/" + source_root + "/__init__.py", b""
            )
        for member, payload in extra_members:
            _add_tar_bytes(archive, member, payload)


def _write_colliding_sdist(path, *, ancestor_first):
    root = "unified_cli-0.5.0"
    package = root + "/src/unified_cli"
    child = package + "/__init__.py"
    egg_info = root + "/src/unified_cli.egg-info"
    metadata = _metadata("unified-cli", "0.5.0")
    with tarfile.open(path, "w:gz") as archive:
        for directory in (root, root + "/src", egg_info):
            _add_tar_directory(archive, directory)
        _add_tar_bytes(archive, root + "/PKG-INFO", metadata)
        _add_tar_bytes(archive, egg_info + "/PKG-INFO", metadata)
        collision = ((package, b"file"), (child, b"child"))
        if not ancestor_first:
            collision = tuple(reversed(collision))
        for member, payload in collision:
            _add_tar_bytes(archive, member, payload)


def _artifacts(
    tmp_path,
    *,
    name="unified-cli",
    version="0.5.0",
    package_root="unified_cli",
    dependency=None,
    wheel_options=None,
    sdist_options=None,
):
    normalized = name.replace("-", "_")
    wheel = tmp_path / (normalized + "-" + version + "-py3-none-any.whl")
    sdist = tmp_path / (normalized + "-" + version + ".tar.gz")
    _write_wheel(
        wheel,
        name=name,
        version=version,
        package_root=package_root,
        dependency=dependency,
        **(wheel_options or {}),
    )
    _write_sdist(
        sdist,
        name=name,
        version=version,
        package_root=package_root,
        dependency=dependency,
        **(sdist_options or {}),
    )
    return wheel, sdist


def _verify_core(tmp_path):
    return verify_release_artifacts.verify_release_directory(
        tmp_path,
        expected_name="unified-cli",
        expected_version="0.5.0",
        package_root="unified_cli",
        forbidden_package_root="unified_cli_ext",
    )


def _verify_ext(tmp_path):
    return verify_release_artifacts.verify_release_directory(
        tmp_path,
        expected_name="unified-cli-ext",
        expected_version="0.1.0",
        package_root="unified_cli_ext",
        forbidden_package_root="unified_cli",
        expected_dependency="unified-cli>=0.5,<0.6",
    )


_UNIFIED_ENTRY_POINTS = {
    "console_scripts": {
        "unified-cli": "unified_cli.cli:main",
    },
    "unified_cli.providers.v1": {
        "grok": "unified_cli_ext.providers.grok:PLUGIN",
        "kimi": "unified_cli_ext.providers.kimi:PLUGIN",
    },
}


def _entry_points_payload(entries=_UNIFIED_ENTRY_POINTS):
    lines = []
    for group, values in entries.items():
        lines.append("[" + group + "]")
        lines.extend(
            name + " = " + target for name, target in sorted(values.items())
        )
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _unified_artifacts(tmp_path, *, dependencies=None, entry_points=None):
    version = "0.5.3"
    root = "unified_cli-" + version
    wheel_entry_points = "unified_cli-0.5.3.dist-info/entry_points.txt"
    sdist_entry_points = root + "/src/unified_cli.egg-info/entry_points.txt"
    payload = _entry_points_payload(
        _UNIFIED_ENTRY_POINTS if entry_points is None else entry_points
    )
    return _artifacts(
        tmp_path,
        version=version,
        dependency=dependencies,
        wheel_options={
            "extra": (
                ("unified_cli/py.typed", b""),
                ("unified_cli_ext/__init__.py", b""),
                ("unified_cli_ext/py.typed", b""),
                (wheel_entry_points, payload),
            ),
        },
        sdist_options={
            "extra_members": (
                (root + "/src/unified_cli/py.typed", b""),
                (
                    root
                    + "/packages/unified-cli-ext/src/unified_cli_ext/__init__.py",
                    b"",
                ),
                (
                    root
                    + "/packages/unified-cli-ext/src/unified_cli_ext/py.typed",
                    b"",
                ),
                (sdist_entry_points, payload),
            ),
        },
    )


def _verify_unified(tmp_path, *, expected_optional_dependency=None):
    return verify_release_artifacts.verify_release_directory(
        tmp_path,
        expected_name="unified-cli",
        expected_version="0.5.3",
        package_sources=(
            ("unified_cli", "src/unified_cli"),
            (
                "unified_cli_ext",
                "packages/unified-cli-ext/src/unified_cli_ext",
            ),
        ),
        required_package_files=(
            ("unified_cli", "py.typed"),
            ("unified_cli_ext", "py.typed"),
        ),
        expected_dependency=("rich>=13", "prompt-toolkit>=3.0.43"),
        expected_optional_dependency=expected_optional_dependency,
        forbidden_dependencies=("unified-cli", "unified-cli-ext"),
        expected_entry_points=_UNIFIED_ENTRY_POINTS,
    )


def _metadata_target_options(target, dependencies):
    if target == "wheel":
        return {"wheel_options": {"metadata_dependencies": dependencies}}
    if target == "sdist-root":
        return {"sdist_options": {"root_dependencies": dependencies}}
    if target == "sdist-egg":
        return {"sdist_options": {"egg_dependencies": dependencies}}
    raise AssertionError("unknown metadata target")


def test_exact_wheel_sdist_record_and_identity_pass(tmp_path):
    wheel, sdist = _artifacts(tmp_path)

    assert _verify_core(tmp_path) == (wheel, sdist)


def test_unified_distribution_requires_both_namespaces_and_exact_entry_points(
    tmp_path,
):
    wheel, sdist = _unified_artifacts(
        tmp_path,
        dependencies=("prompt-toolkit>=3.0.43", "rich>=13"),
    )

    assert _verify_unified(tmp_path) == (wheel, sdist)


def test_unified_distribution_rejects_legacy_ext_dependency(tmp_path):
    _unified_artifacts(
        tmp_path,
        dependencies=(
            "rich>=13",
            "prompt-toolkit>=3.0.43",
            'unified-cli-ext>=0.1; extra == "extensions"',
        ),
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="forbidden distribution dependency boundary",
    ):
        _verify_unified(tmp_path)


def test_unified_distribution_rejects_entry_point_drift(tmp_path):
    broken = {
        group: dict(entries) for group, entries in _UNIFIED_ENTRY_POINTS.items()
    }
    broken["unified_cli.providers.v1"]["grok"] = (
        "unified_cli_ext.providers.kimi:PLUGIN"
    )
    _unified_artifacts(
        tmp_path,
        dependencies=("rich>=13", "prompt-toolkit>=3.0.43"),
        entry_points=broken,
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="entry-point set does not match",
    ):
        _verify_unified(tmp_path)


def test_unified_distribution_optional_dependency_set_is_exact(tmp_path):
    expected_optional = (
        'mcp>=1.27,<2; python_version >= "3.10" and extra == "mcp"',
    )
    wheel, sdist = _unified_artifacts(
        tmp_path,
        dependencies=(
            "rich>=13",
            "prompt-toolkit>=3.0.43",
            expected_optional[0],
        ),
    )
    assert _verify_unified(
        tmp_path, expected_optional_dependency=expected_optional
    ) == (wheel, sdist)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="optional dependency set does not match",
    ):
        _verify_unified(
            tmp_path,
            expected_optional_dependency=(
                'mcp>=1.28,<2; python_version >= "3.10" and extra == "mcp"',
            ),
        )


def test_ext_dependency_contract_is_order_insensitive_but_exact(tmp_path):
    wheel, sdist = _artifacts(
        tmp_path,
        name="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        dependency="unified-cli<0.6,>=0.5",
    )

    assert _verify_ext(tmp_path) == (wheel, sdist)


def test_complete_runtime_dependency_set_is_order_insensitive_and_allows_extras(
    tmp_path,
):
    wheel, sdist = _artifacts(
        tmp_path,
        dependency=(
            'uvicorn>=0.23; python_version >= "3.9" and extra == "server"',
            "prompt_toolkit>=3.0.43",
            "rich>=13",
        ),
    )

    assert verify_release_artifacts.verify_release_directory(
        tmp_path,
        expected_name="unified-cli",
        expected_version="0.5.0",
        package_root="unified_cli",
        forbidden_package_root="unified_cli_ext",
        expected_dependency=("rich>=13", "prompt-toolkit>=3.0.43"),
    ) == (wheel, sdist)


@pytest.mark.parametrize("target", ("wheel", "sdist-root", "sdist-egg"))
def test_core_rejects_injected_runtime_dependency_in_every_metadata(tmp_path, target):
    _artifacts(
        tmp_path,
        **_metadata_target_options(target, ("unified-cli-ext>=0.1",)),
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="forbidden distribution dependency boundary",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize("target", ("wheel", "sdist-root", "sdist-egg"))
def test_ext_rejects_additional_runtime_dependency_in_every_metadata(tmp_path, target):
    required = "unified-cli>=0.5,<0.6"
    _artifacts(
        tmp_path,
        name="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        dependency=required,
        **_metadata_target_options(target, (required, "injected-runtime>=1")),
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="default runtime dependency set does not match",
    ):
        _verify_ext(tmp_path)


def test_ext_allows_dependencies_restricted_to_selected_extras(tmp_path):
    wheel, sdist = _artifacts(
        tmp_path,
        name="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        dependency=(
            "unified-cli>=0.5,<0.6",
            'mcp>=1.27,<2; python_version >= "3.10" and extra == "mcp"',
            'agent-client-protocol>=0.11,<0.12; (extra == "acp" and python_version < "3.15")',
        ),
    )

    assert _verify_ext(tmp_path) == (wheel, sdist)


@pytest.mark.parametrize(
    "dependency",
    (
        'injected-runtime>=1; python_version >= "3.10"',
        'injected-runtime>=1; extra != "server"',
        'injected-runtime>=1; extra == "server" or os_name == "posix"',
        'injected-runtime>=1; unknown_marker == "value" and extra == "server"',
        'injected-runtime>=1; extra ==',
        'injected-runtime>=1; extra == "server" # ignored text',
        'injected-runtime>=1; extra == "serv" "er"',
        "injected-runtime>=1; extra == '''server'''",
    ),
)
def test_core_rejects_runtime_or_malformed_optional_markers(tmp_path, dependency):
    _artifacts(tmp_path, dependency=dependency)

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)


@pytest.mark.parametrize("target", ("wheel", "sdist-root", "sdist-egg"))
def test_core_rejects_optional_dependency_on_ext_in_every_metadata(tmp_path, target):
    _artifacts(
        tmp_path,
        **_metadata_target_options(
            target,
            ('unified-cli-ext>=0.1; extra == "extensions"',),
        ),
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="forbidden distribution dependency boundary",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize(
    "dependency",
    (
        "unified-cli>=0.4,<0.6",
        "unified-cli>=0.5,<0.7",
        'unified-cli>=0.5,<0.6; python_version >= "3.10"',
    ),
)
def test_ext_dependency_drift_fails_closed(tmp_path, dependency):
    _artifacts(
        tmp_path,
        name="unified-cli-ext",
        version="0.1.0",
        package_root="unified_cli_ext",
        dependency=dependency,
    )

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        verify_release_artifacts.verify_release_directory(
            tmp_path,
            expected_name="unified-cli-ext",
            expected_version="0.1.0",
            package_root="unified_cli_ext",
            forbidden_package_root="unified_cli",
            expected_dependency="unified-cli>=0.5,<0.6",
        )


@pytest.mark.parametrize("entry_kind", ("file", "directory", "symlink"))
def test_extra_or_non_regular_directory_entry_fails_closed(tmp_path, entry_kind):
    wheel, _sdist = _artifacts(tmp_path)
    extra = tmp_path / "unexpected"
    if entry_kind == "file":
        extra.write_text("not releasable", encoding="utf-8")
    elif entry_kind == "directory":
        extra.mkdir()
    else:
        os.symlink(wheel, extra)

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)


@pytest.mark.parametrize("artifact", ("wheel", "sdist"))
def test_wrong_artifact_filename_fails_before_metadata(tmp_path, artifact):
    wheel, sdist = _artifacts(tmp_path)
    selected = wheel if artifact == "wheel" else sdist
    selected.rename(tmp_path / selected.name.replace("0.5.0", "0.5.1"))

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="exactly the expected wheel and sdist",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize("required", ("METADATA", "WHEEL", "RECORD"))
def test_wheel_requires_complete_expected_dist_info(tmp_path, required):
    dist_info = "unified_cli-0.5.0.dist-info/" + required
    _artifacts(tmp_path, wheel_options={"omit": (dist_info,)})

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)


def _record_mutator(kind):
    def mutate(rows):
        rows = [list(row) for row in rows]
        if kind == "missing":
            rows.pop(0)
        elif kind == "duplicate":
            rows.append(list(rows[0]))
        elif kind == "hash":
            rows[0][1] = "sha256=wrong"
        elif kind == "size":
            rows[0][2] = "1"
        elif kind == "self":
            rows[-1][1] = "sha256=wrong"
            rows[-1][2] = "1"
        return rows

    return mutate


@pytest.mark.parametrize("corruption", ("missing", "duplicate", "hash", "size", "self"))
def test_wheel_record_corruption_is_rejected(tmp_path, corruption):
    _artifacts(
        tmp_path,
        wheel_options={"record_mutator": _record_mutator(corruption)},
    )

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)


def test_wheel_record_rejects_valid_sha512_rows(tmp_path):
    _artifacts(tmp_path, wheel_options={"record_algorithm": "sha512"})

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="must use a sha256 hash",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize("ancestor_first", (True, False))
def test_wheel_rejects_file_descendant_collision_in_either_order(
    tmp_path, ancestor_first,
):
    collision = (("unified_cli", b"regular-file"),)
    options = {"prepend" if ancestor_first else "extra": collision}
    _artifacts(tmp_path, wheel_options=options)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="regular file with descendant members",
    ):
        _verify_core(tmp_path)


def test_wheel_rejects_deep_file_descendant_collision(tmp_path):
    _artifacts(
        tmp_path,
        wheel_options={
            "extra": (
                ("unified_cli/providers", b"regular-file"),
                ("unified_cli/providers/nested/plugin.py", b"child"),
            )
        },
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="regular file with descendant members",
    ):
        _verify_core(tmp_path)


def test_wheel_rejects_same_lexical_path_as_file_and_directory(tmp_path):
    _artifacts(
        tmp_path,
        wheel_options={
            "prepend": (("unified_cli", b"file"), ("unified_cli/", b""))
        },
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="both a file and directory",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize(
    "member",
    ("../escape", "foreign_package/__init__.py", "other-0.dist-info/METADATA"),
)
def test_wheel_unsafe_or_foreign_roots_are_rejected(tmp_path, member):
    _artifacts(tmp_path, wheel_options={"extra": ((member, b"foreign"),)})

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)


def test_duplicate_wheel_member_is_rejected(tmp_path):
    wheel, _sdist = _artifacts(tmp_path)
    with pytest.warns(UserWarning), zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("unified_cli/__init__.py", b"duplicate")

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="duplicate members",
    ):
        _verify_core(tmp_path)


def test_wheel_symlink_member_is_rejected(tmp_path):
    wheel, _sdist = _artifacts(tmp_path)
    link = zipfile.ZipInfo("unified_cli/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(link, "target")

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="symbolic link",
    ):
        _verify_core(tmp_path)


def test_wheel_member_size_limit_is_enforced(tmp_path, monkeypatch):
    _artifacts(
        tmp_path,
        wheel_options={"extra": (("unified_cli/data.bin", b"1234"),)},
    )
    monkeypatch.setattr(verify_release_artifacts, "_MAX_MEMBER_BYTES", 3)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="oversized member",
    ):
        _verify_core(tmp_path)


def test_wheel_member_count_limit_is_enforced(tmp_path, monkeypatch):
    _artifacts(tmp_path)
    monkeypatch.setattr(verify_release_artifacts, "_MAX_MEMBERS", 3)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="too many members",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize(
    "source_root",
    ("unified_cli_ext", "foreign_package", "other.egg-info"),
)
def test_sdist_rejects_every_unexpected_source_root(tmp_path, source_root):
    _artifacts(tmp_path, sdist_options={"extra_sources": (source_root,)})

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="unexpected source root",
    ):
        _verify_core(tmp_path)


def test_sdist_rejects_second_archive_root(tmp_path):
    _artifacts(
        tmp_path,
        sdist_options={"extra_members": (("other-root/file.py", b"bad"),)},
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="unexpected archive root",
    ):
        _verify_core(tmp_path)


@pytest.mark.parametrize("ancestor_first", (True, False))
def test_sdist_rejects_file_descendant_collision_in_either_order(
    tmp_path, ancestor_first,
):
    _wheel, sdist = _artifacts(tmp_path)
    _write_colliding_sdist(sdist, ancestor_first=ancestor_first)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="regular file with descendant members",
    ):
        _verify_core(tmp_path)


def test_sdist_rejects_deep_file_descendant_collision(tmp_path):
    root = "unified_cli-0.5.0"
    _artifacts(
        tmp_path,
        sdist_options={
            "extra_members": (
                (root + "/src/unified_cli/providers", b"regular-file"),
                (root + "/src/unified_cli/providers/nested/plugin.py", b"child"),
            )
        },
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="regular file with descendant members",
    ):
        _verify_core(tmp_path)


def test_sdist_rejects_same_lexical_path_as_file_and_directory(tmp_path):
    root = "unified_cli-0.5.0"
    _artifacts(
        tmp_path,
        sdist_options={
            "extra_members": ((root + "/src/unified_cli", b"regular-file"),)
        },
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="both a file and directory",
    ):
        _verify_core(tmp_path)


def test_sdist_rejects_unsafe_member_path(tmp_path):
    _artifacts(
        tmp_path,
        sdist_options={"extra_members": (("../escape", b"bad"),)},
    )

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="unsafe member path",
    ):
        _verify_core(tmp_path)


def test_sdist_symlink_member_is_rejected(tmp_path):
    _wheel, sdist = _artifacts(tmp_path)
    # Rebuild a valid archive and add a symlink in one write pass because gzip
    # tar archives cannot be appended safely.
    sdist.unlink()
    normalized = "unified_cli"
    root = normalized + "-0.5.0"
    with tarfile.open(sdist, "w:gz") as archive:
        for directory in (root, root + "/src", root + "/src/unified_cli"):
            _add_tar_directory(archive, directory)
        metadata = _metadata("unified-cli", "0.5.0")
        _add_tar_bytes(archive, root + "/PKG-INFO", metadata)
        _add_tar_bytes(archive, root + "/src/unified_cli/__init__.py", b"")
        link = tarfile.TarInfo(root + "/src/unified_cli/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="non-file member",
    ):
        _verify_core(tmp_path)


def test_duplicate_sdist_member_is_rejected(tmp_path):
    _wheel, sdist = _artifacts(tmp_path)
    root = "unified_cli-0.5.0"
    with tarfile.open(sdist, "w:gz") as archive:
        _add_tar_directory(archive, root)
        metadata = _metadata("unified-cli", "0.5.0")
        _add_tar_bytes(archive, root + "/PKG-INFO", metadata)
        _add_tar_bytes(archive, root + "/PKG-INFO", metadata)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="duplicate members",
    ):
        _verify_core(tmp_path)


def test_sdist_member_size_limit_is_enforced(tmp_path, monkeypatch):
    _artifacts(
        tmp_path,
        sdist_options={
            "extra_members": (("unified_cli-0.5.0/README.md", b"1234"),)
        },
    )
    monkeypatch.setattr(verify_release_artifacts, "_MAX_MEMBER_BYTES", 3)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="oversized member",
    ):
        _verify_core(tmp_path)


def test_sdist_member_count_limit_is_enforced(tmp_path, monkeypatch):
    _wheel, sdist = _artifacts(tmp_path)
    monkeypatch.setattr(verify_release_artifacts, "_MAX_MEMBERS", 3)

    with pytest.raises(
        verify_release_artifacts.ArtifactVerificationError,
        match="too many members",
    ):
        verify_release_artifacts._bounded_tar_payloads(sdist)


def test_hierarchy_validation_uses_path_components_not_similar_prefixes():
    verify_release_artifacts._validate_member_hierarchy(
        ("pkg", "pkg2/module.py"),
        (False, False),
        "archive",
    )


@pytest.mark.parametrize("metadata_field", ("name", "version"))
def test_metadata_identity_cannot_disagree_with_exact_filenames(tmp_path, metadata_field):
    options = {
        "metadata_name": "wrong-name" if metadata_field == "name" else None,
        "metadata_version": "9.9.9" if metadata_field == "version" else None,
    }
    _artifacts(tmp_path, wheel_options=options, sdist_options=options)

    with pytest.raises(verify_release_artifacts.ArtifactVerificationError):
        _verify_core(tmp_path)
