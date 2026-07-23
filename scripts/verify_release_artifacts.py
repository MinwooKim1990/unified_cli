#!/usr/bin/env python3
"""Fail closed unless one wheel and one sdist are complete release artifacts."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import re
import stat
import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union


_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 1 * 1024 * 1024
_MAX_MEMBERS = 10_000
_MAX_TOTAL_MEMBER_BYTES = 256 * 1024 * 1024
_RECORD_HASH_ALGORITHM = "sha256"
_MARKER_VARIABLES = frozenset({
    "implementation_name",
    "implementation_version",
    "os_name",
    "platform_machine",
    "platform_python_implementation",
    "platform_release",
    "platform_system",
    "platform_version",
    "python_full_version",
    "python_version",
    "sys_platform",
    "extra",
})

RequirementContract = Tuple[str, frozenset]
ExpectedDependencies = Optional[Union[str, Sequence[str]]]


class ArtifactVerificationError(ValueError):
    """A release directory or artifact does not match its declared identity."""


def _normalized(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", value):
        raise ArtifactVerificationError("expected distribution name is invalid")
    return re.sub(r"[-_.]+", "_", value)


def _version_component(value: str) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", value):
        raise ArtifactVerificationError("expected version is invalid")
    return value.replace("-", "_")


def _safe_member_name(name: str, label: str) -> str:
    directory_suffix = name.endswith("/")
    lexical = name[:-1] if directory_suffix else name
    if (
        not lexical
        or lexical.startswith("/")
        or "\\" in lexical
        or "\x00" in lexical
        or "//" in lexical
        or any(ord(character) < 32 for character in lexical)
    ):
        raise ArtifactVerificationError(label + " contains an unsafe member path")
    parts = lexical.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactVerificationError(label + " contains an unsafe member path")
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        raise ArtifactVerificationError(label + " contains an unsafe member path")
    return "/".join(parts)


def _safe_member_names(names: Iterable[str], label: str) -> Tuple[str, ...]:
    result: List[str] = []
    for index, name in enumerate(names):
        if index >= _MAX_MEMBERS:
            raise ArtifactVerificationError(label + " contains too many members")
        result.append(_safe_member_name(name, label))
    return tuple(result)


def _validate_member_hierarchy(
    names: Sequence[str], directory_flags: Sequence[bool], label: str,
) -> None:
    if len(names) != len(directory_flags):
        raise ArtifactVerificationError(label + " member type data is inconsistent")
    member_types: Dict[str, bool] = {}
    for name, is_directory in zip(names, directory_flags):
        if name in member_types:
            if member_types[name] != is_directory:
                raise ArtifactVerificationError(
                    label + " contains the same path as both a file and directory"
                )
            raise ArtifactVerificationError(label + " contains duplicate members")
        member_types[name] = is_directory
    regular_files = {
        name for name, is_directory in member_types.items() if not is_directory
    }
    for name in member_types:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor in regular_files:
                raise ArtifactVerificationError(
                    label + " contains a regular file with descendant members"
                )


def _metadata(payload: bytes, label: str) -> Message:
    if len(payload) > _MAX_METADATA_BYTES:
        raise ArtifactVerificationError(label + " metadata is too large")
    metadata = BytesParser(policy=compat32).parsebytes(payload)
    if metadata.defects:
        raise ArtifactVerificationError(label + " metadata is malformed")
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise ArtifactVerificationError(label + " metadata identity is ambiguous")
    if not names[0].strip() or not versions[0].strip():
        raise ArtifactVerificationError(label + " metadata identity is incomplete")
    return metadata


def _identity(metadata: Message) -> Tuple[str, str]:
    return metadata["Name"].strip(), metadata["Version"].strip()


def _requirement_head_contract(
    head: str, label: str, *, require_specifier: bool,
) -> RequirementContract:
    match = re.fullmatch(
        r"\s*([A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)\s*([^\s].*?)?\s*",
        head,
    )
    if match is None:
        raise ArtifactVerificationError(label + " dependency is malformed")
    name = match.group(1)
    suffix = (match.group(2) or "").strip()
    if suffix.startswith("(") and suffix.endswith(")"):
        suffix = suffix[1:-1].strip()
    if "[" in suffix or "]" in suffix or "@" in suffix or "(" in suffix or ")" in suffix:
        raise ArtifactVerificationError(label + " dependency specifier is malformed")
    if not suffix:
        if require_specifier:
            raise ArtifactVerificationError(label + " dependency specifier is malformed")
        return _normalized(name), frozenset()
    clauses = []
    for clause in suffix.split(","):
        compact = re.sub(r"\s+", "", clause)
        if not re.fullmatch(r"(?:===|==|!=|~=|<=|>=|<|>)[^,;\s]+", compact):
            raise ArtifactVerificationError(label + " dependency specifier is malformed")
        clauses.append(compact)
    if len(clauses) != len(set(clauses)):
        raise ArtifactVerificationError(label + " dependency specifier is duplicated")
    return _normalized(name), frozenset(clauses)


def _requirement_contract(value: str, label: str) -> RequirementContract:
    head, separator, marker = value.partition(";")
    if separator:
        if not marker.strip():
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        raise ArtifactVerificationError(label + " dependency must be unconditional")
    return _requirement_head_contract(head, label, require_specifier=True)


def _marker_requires_extra(marker: str, label: str) -> bool:
    """Return whether every accepted marker branch requires a selected extra."""
    source = marker.strip()
    if (
        not source
        or "#" in source
        or "\\" in source
        or any(ord(character) < 32 for character in source)
    ):
        raise ArtifactVerificationError(label + " dependency marker is malformed")
    try:
        expression = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ArtifactVerificationError(label + " dependency marker is malformed") from exc

    def inspect(node: ast.AST) -> bool:
        if isinstance(node, ast.BoolOp):
            if not node.values:
                raise ArtifactVerificationError(label + " dependency marker is malformed")
            branches = [inspect(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return any(branches)
            if isinstance(node.op, ast.Or):
                return all(branches)
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        left = node.left
        right = node.comparators[0]
        if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
            variable = left.id
            literal = right.value
            literal_node = right
        elif isinstance(right, ast.Name) and isinstance(left, ast.Constant):
            variable = right.id
            literal = left.value
            literal_node = left
        else:
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        if variable not in _MARKER_VARIABLES or not isinstance(literal, str):
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        literal_source = ast.get_source_segment(source, literal_node)
        if literal_source is None or re.fullmatch(
            r'''(?:"[^"\\\r\n]*"|'[^'\\\r\n]*')''', literal_source
        ) is None:
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        if any(ord(character) < 32 for character in literal):
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        if not isinstance(
            node.ops[0],
            (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn),
        ):
            raise ArtifactVerificationError(label + " dependency marker is malformed")
        return (
            isinstance(node.ops[0], ast.Eq)
            and variable == "extra"
            and bool(literal)
        )

    return inspect(expression.body)


def _parsed_requirement(
    value: str, label: str,
) -> Tuple[RequirementContract, bool, bool]:
    head, separator, marker = value.partition(";")
    contract = _requirement_head_contract(head, label, require_specifier=False)
    if not separator:
        return contract, False, False
    if not marker.strip():
        raise ArtifactVerificationError(label + " dependency marker is malformed")
    return contract, _marker_requires_extra(marker, label), True


def _expected_requirement_contracts(
    expected_dependency: ExpectedDependencies,
) -> Tuple[RequirementContract, ...]:
    if expected_dependency is None:
        values: Sequence[str] = ()
    elif isinstance(expected_dependency, str):
        values = (expected_dependency,)
    else:
        values = tuple(expected_dependency)
    contracts = tuple(
        _requirement_contract(value, "expected") for value in values
    )
    if len(contracts) != len(set(contracts)) or len(contracts) != len(
        {contract[0] for contract in contracts}
    ):
        raise ArtifactVerificationError("expected runtime dependency set is ambiguous")
    return contracts


def _validate_metadata(
    payload: bytes,
    label: str,
    *,
    expected_name: str,
    expected_version: str,
    expected_dependency: ExpectedDependencies,
    forbidden_dependency: str,
) -> Tuple[str, str]:
    metadata = _metadata(payload, label)
    identity = _identity(metadata)
    if _normalized(identity[0]) != _normalized(expected_name):
        raise ArtifactVerificationError(label + " distribution name does not match")
    if identity[1] != expected_version:
        raise ArtifactVerificationError(label + " version does not match")
    expected = _expected_requirement_contracts(expected_dependency)
    expected_names = {contract[0] for contract in expected}
    runtime: List[RequirementContract] = []
    for raw_requirement in metadata.get_all("Requires-Dist", []):
        contract, optional, has_marker = _parsed_requirement(
            str(raw_requirement), label
        )
        if contract[0] == forbidden_dependency and (
            contract[0] not in expected_names or optional
        ):
            raise ArtifactVerificationError(
                label + " crosses the forbidden distribution dependency boundary"
            )
        if optional:
            if contract[0] in expected_names:
                raise ArtifactVerificationError(
                    label + " expected runtime dependency cannot be optional"
                )
            continue
        if has_marker:
            raise ArtifactVerificationError(
                label + " runtime dependency must be unconditional"
            )
        runtime.append(contract)
    if len(runtime) != len(expected) or set(runtime) != set(expected):
        raise ArtifactVerificationError(
            label + " default runtime dependency set does not match"
        )
    return identity


def _bounded_zip_payloads(
    path: Path,
) -> Tuple[Tuple[str, ...], Dict[str, bytes]]:
    try:
        archive_size = path.stat().st_size
    except OSError as exc:
        raise ArtifactVerificationError("wheel archive is unreadable") from exc
    if archive_size > _MAX_ARCHIVE_BYTES:
        raise ArtifactVerificationError("wheel archive is too large")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = _safe_member_names((item.filename for item in infos), "wheel")
            _validate_member_hierarchy(
                names, tuple(item.is_dir() for item in infos), "wheel"
            )
            total = 0
            payloads: Dict[str, bytes] = {}
            for item, name in zip(infos, names):
                mode = item.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode):
                    raise ArtifactVerificationError("wheel contains a symbolic link")
                if item.flag_bits & 0x1:
                    raise ArtifactVerificationError("wheel contains an encrypted member")
                if item.is_dir():
                    raise ArtifactVerificationError("wheel contains a non-file member")
                if file_type not in (0, stat.S_IFREG):
                    raise ArtifactVerificationError("wheel contains a non-file member")
                if item.file_size > _MAX_MEMBER_BYTES:
                    raise ArtifactVerificationError("wheel contains an oversized member")
                total += item.file_size
                if total > _MAX_TOTAL_MEMBER_BYTES:
                    raise ArtifactVerificationError("wheel members are too large in aggregate")
                with archive.open(item) as handle:
                    payload = handle.read(_MAX_MEMBER_BYTES + 1)
                if len(payload) > _MAX_MEMBER_BYTES:
                    raise ArtifactVerificationError("wheel contains an oversized member")
                if len(payload) != item.file_size:
                    raise ArtifactVerificationError("wheel member size is inconsistent")
                payloads[name] = payload
    except ArtifactVerificationError:
        raise
    except (
        OSError,
        KeyError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ArtifactVerificationError("wheel could not be inspected") from exc
    return names, payloads


def _validate_record(payloads: Dict[str, bytes], record_path: str) -> None:
    try:
        record_text = payloads[record_path].decode("utf-8")
    except UnicodeError as exc:
        raise ArtifactVerificationError("wheel RECORD is not UTF-8") from exc
    rows: Dict[str, Tuple[str, str]] = {}
    try:
        for row in csv.reader(io.StringIO(record_text, newline="")):
            if len(row) != 3:
                raise ArtifactVerificationError("wheel RECORD contains a malformed row")
            name = _safe_member_name(row[0], "wheel RECORD")
            if name != row[0] or name in rows:
                raise ArtifactVerificationError("wheel RECORD contains duplicate members")
            rows[name] = (row[1], row[2])
    except csv.Error as exc:
        raise ArtifactVerificationError("wheel RECORD is malformed") from exc
    if set(rows) != set(payloads):
        raise ArtifactVerificationError("wheel RECORD does not enumerate every file exactly once")
    for name, payload in payloads.items():
        hash_field, size_field = rows[name]
        if name == record_path:
            if hash_field or size_field:
                raise ArtifactVerificationError("wheel RECORD self-entry must be unhashed")
            continue
        if not hash_field or not size_field or not size_field.isdigit():
            raise ArtifactVerificationError("wheel RECORD hash or size is missing")
        if str(int(size_field)) != size_field or int(size_field) != len(payload):
            raise ArtifactVerificationError("wheel RECORD member size does not match")
        algorithm, separator, encoded = hash_field.partition("=")
        if separator != "=" or algorithm != _RECORD_HASH_ALGORITHM or not encoded:
            raise ArtifactVerificationError("wheel RECORD must use a sha256 hash")
        digest = hashlib.new(algorithm, payload).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if encoded != expected:
            raise ArtifactVerificationError("wheel RECORD member hash does not match")


def _wheel_identity(
    path: Path,
    *,
    expected_name: str,
    expected_version: str,
    package_root: str,
    forbidden_package_root: str,
    expected_dependency: ExpectedDependencies,
) -> Tuple[str, str]:
    distribution = _distribution_component(expected_name)
    version = _version_component(expected_version)
    expected_filename = distribution + "-" + version + "-py3-none-any.whl"
    if path.name != expected_filename:
        raise ArtifactVerificationError("wheel filename does not match the release identity")
    names, payloads = _bounded_zip_payloads(path)
    expected_dist_info = distribution + "-" + version + ".dist-info"
    allowed_roots = {package_root, expected_dist_info}
    if any(name.split("/", 1)[0] not in allowed_roots for name in names):
        raise ArtifactVerificationError("wheel contains an unexpected distribution root")
    for name in names:
        parts = name.split("/")
        for index, part in enumerate(parts):
            if part.endswith(".dist-info") and not (
                index == 0 and part == expected_dist_info
            ):
                raise ArtifactVerificationError("wheel contains an unexpected dist-info root")
    if package_root + "/__init__.py" not in payloads:
        raise ArtifactVerificationError("wheel is missing its expected package root")
    if any(name.split("/", 1)[0] == forbidden_package_root for name in names):
        raise ArtifactVerificationError("wheel mixes Core and Ext package roots")
    metadata_path = expected_dist_info + "/METADATA"
    wheel_path = expected_dist_info + "/WHEEL"
    record_path = expected_dist_info + "/RECORD"
    for required in (metadata_path, wheel_path, record_path):
        if required not in payloads:
            raise ArtifactVerificationError("wheel is missing required dist-info metadata")
    if not payloads[wheel_path] or len(payloads[wheel_path]) > _MAX_METADATA_BYTES:
        raise ArtifactVerificationError("wheel WHEEL metadata is invalid")
    _validate_record(payloads, record_path)
    return _validate_metadata(
        payloads[metadata_path],
        "wheel",
        expected_name=expected_name,
        expected_version=expected_version,
        expected_dependency=expected_dependency,
        forbidden_dependency=_normalized(forbidden_package_root),
    )


def _bounded_tar_payloads(
    path: Path,
) -> Tuple[Tuple[str, ...], Dict[str, bytes], frozenset]:
    try:
        archive_size = path.stat().st_size
    except OSError as exc:
        raise ArtifactVerificationError("sdist archive is unreadable") from exc
    if archive_size > _MAX_ARCHIVE_BYTES:
        raise ArtifactVerificationError("sdist archive is too large")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            names = _safe_member_names((member.name for member in members), "sdist")
            _validate_member_hierarchy(
                names, tuple(member.isdir() for member in members), "sdist"
            )
            total = 0
            payloads: Dict[str, bytes] = {}
            directories = set()
            for member, name in zip(members, names):
                if member.isdir():
                    if member.size != 0:
                        raise ArtifactVerificationError("sdist contains an invalid directory")
                    directories.add(name)
                    continue
                if not member.isfile():
                    raise ArtifactVerificationError("sdist contains a non-file member")
                if member.size > _MAX_MEMBER_BYTES:
                    raise ArtifactVerificationError("sdist contains an oversized member")
                total += member.size
                if total > _MAX_TOTAL_MEMBER_BYTES:
                    raise ArtifactVerificationError("sdist members are too large in aggregate")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ArtifactVerificationError("sdist member is unreadable")
                payload = handle.read(_MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size:
                    raise ArtifactVerificationError("sdist member size is inconsistent")
                payloads[name] = payload
    except ArtifactVerificationError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ArtifactVerificationError("sdist could not be inspected") from exc
    return names, payloads, frozenset(directories)


def _sdist_identity(
    path: Path,
    *,
    expected_name: str,
    expected_version: str,
    package_root: str,
    forbidden_package_root: str,
    expected_dependency: ExpectedDependencies,
) -> Tuple[str, str]:
    distribution = _distribution_component(expected_name)
    version = _version_component(expected_version)
    expected_filename = distribution + "-" + version + ".tar.gz"
    expected_root = distribution + "-" + version
    if path.name != expected_filename:
        raise ArtifactVerificationError("sdist filename does not match the release identity")
    names, payloads, directories = _bounded_tar_payloads(path)
    if expected_root not in directories:
        raise ArtifactVerificationError("sdist is missing its single expected root directory")
    if any(name.split("/", 1)[0] != expected_root for name in names):
        raise ArtifactVerificationError("sdist contains an unexpected archive root")
    expected_egg_info = distribution + ".egg-info"
    source_prefix = expected_root + "/src"
    for name in names:
        if name == source_prefix:
            continue
        if name.startswith(source_prefix + "/"):
            relative = name[len(source_prefix) + 1 :]
            source_root = relative.split("/", 1)[0]
            if source_root not in {package_root, expected_egg_info}:
                raise ArtifactVerificationError("sdist contains an unexpected source root")
        if ".dist-info" in name.split("/"):
            raise ArtifactVerificationError("sdist contains a wheel dist-info root")
    expected_init = source_prefix + "/" + package_root + "/__init__.py"
    if expected_init not in payloads:
        raise ArtifactVerificationError("sdist is missing its expected package root")
    forbidden_fragment = source_prefix + "/" + forbidden_package_root + "/"
    if any(name.startswith(forbidden_fragment) for name in names):
        raise ArtifactVerificationError("sdist mixes Core and Ext package roots")
    root_metadata = expected_root + "/PKG-INFO"
    if root_metadata not in payloads:
        raise ArtifactVerificationError("sdist is missing its root PKG-INFO")
    allowed_metadata = {
        root_metadata,
        source_prefix + "/" + expected_egg_info + "/PKG-INFO",
    }
    if any(name.endswith("/PKG-INFO") and name not in allowed_metadata for name in payloads):
        raise ArtifactVerificationError("sdist contains an unexpected PKG-INFO")
    identity = _validate_metadata(
        payloads[root_metadata],
        "sdist",
        expected_name=expected_name,
        expected_version=expected_version,
        expected_dependency=expected_dependency,
        forbidden_dependency=_normalized(forbidden_package_root),
    )
    egg_metadata = source_prefix + "/" + expected_egg_info + "/PKG-INFO"
    if egg_metadata in payloads:
        if _validate_metadata(
            payloads[egg_metadata],
            "sdist egg-info",
            expected_name=expected_name,
            expected_version=expected_version,
            expected_dependency=expected_dependency,
            forbidden_dependency=_normalized(forbidden_package_root),
        ) != identity:
            raise ArtifactVerificationError("sdist metadata identities differ")
    return identity


def verify_release_directory(
    directory: Path,
    *,
    expected_name: str,
    expected_version: str,
    package_root: str,
    forbidden_package_root: str,
    expected_dependency: ExpectedDependencies = None,
) -> Tuple[Path, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactVerificationError("release artifact directory does not exist")
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ArtifactVerificationError("release artifact directory is unreadable") from exc
    for entry in entries:
        try:
            mode = entry.lstat().st_mode
        except OSError as exc:
            raise ArtifactVerificationError("release artifact entry is unreadable") from exc
        if entry.is_symlink() or not stat.S_ISREG(mode):
            raise ArtifactVerificationError(
                "release artifact directory contains a non-regular entry"
            )
    distribution = _distribution_component(expected_name)
    version = _version_component(expected_version)
    wheel_name = distribution + "-" + version + "-py3-none-any.whl"
    sdist_name = distribution + "-" + version + ".tar.gz"
    if [entry.name for entry in entries] != sorted((wheel_name, sdist_name)):
        raise ArtifactVerificationError(
            "release directory must contain exactly the expected wheel and sdist"
        )
    by_name = {entry.name: entry for entry in entries}
    wheel = by_name[wheel_name]
    sdist = by_name[sdist_name]
    identities = (
        _wheel_identity(
            wheel,
            expected_name=expected_name,
            expected_version=expected_version,
            package_root=package_root,
            forbidden_package_root=forbidden_package_root,
            expected_dependency=expected_dependency,
        ),
        _sdist_identity(
            sdist,
            expected_name=expected_name,
            expected_version=expected_version,
            package_root=package_root,
            forbidden_package_root=forbidden_package_root,
            expected_dependency=expected_dependency,
        ),
    )
    if identities[0] != identities[1]:
        raise ArtifactVerificationError("wheel and sdist metadata identities differ")
    return wheel, sdist


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--forbid-package-root", required=True)
    parser.add_argument(
        "--requires-dist",
        action="append",
        help=(
            "Require this unconditional runtime dependency with exactly these "
            "specifiers; repeat for the complete default-install dependency set"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        wheel, sdist = verify_release_directory(
            args.directory,
            expected_name=args.name,
            expected_version=args.version,
            package_root=args.package_root,
            forbidden_package_root=args.forbid_package_root,
            expected_dependency=args.requires_dist,
        )
    except (ArtifactVerificationError, OSError) as exc:
        print("release artifact verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified release artifacts: " + wheel.name + " + " + sdist.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
