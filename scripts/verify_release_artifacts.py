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
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


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
PackageSource = Tuple[str, str]
RequiredPackageFile = Tuple[str, str]
ExpectedEntryPoints = Optional[Mapping[str, Mapping[str, str]]]


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


def _expected_optional_requirement_contracts(
    expected_dependencies: ExpectedDependencies,
) -> Tuple[str, ...]:
    if expected_dependencies is None:
        return ()
    if isinstance(expected_dependencies, str):
        values: Sequence[str] = (expected_dependencies,)
    else:
        values = tuple(expected_dependencies)
    contracts = []
    for value in values:
        _, optional, has_marker = _parsed_requirement(value, "expected optional")
        if not optional or not has_marker:
            raise ArtifactVerificationError(
                "expected optional dependency must require a selected extra"
            )
        contracts.append(re.sub(r"\s+", "", value))
    if len(contracts) != len(set(contracts)):
        raise ArtifactVerificationError(
            "expected optional dependency set is ambiguous"
        )
    return tuple(contracts)


def _validate_metadata(
    payload: bytes,
    label: str,
    *,
    expected_name: str,
    expected_version: str,
    expected_dependency: ExpectedDependencies,
    forbidden_dependencies: Sequence[str],
    expected_optional_dependency: ExpectedDependencies,
) -> Tuple[str, str]:
    metadata = _metadata(payload, label)
    identity = _identity(metadata)
    if _normalized(identity[0]) != _normalized(expected_name):
        raise ArtifactVerificationError(label + " distribution name does not match")
    if identity[1] != expected_version:
        raise ArtifactVerificationError(label + " version does not match")
    expected = _expected_requirement_contracts(expected_dependency)
    expected_names = {contract[0] for contract in expected}
    forbidden = {_normalized(value) for value in forbidden_dependencies}
    runtime: List[RequirementContract] = []
    optional_requirements: List[str] = []
    for raw_requirement in metadata.get_all("Requires-Dist", []):
        raw_text = str(raw_requirement)
        contract, optional, has_marker = _parsed_requirement(raw_text, label)
        if contract[0] in forbidden and (
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
            optional_requirements.append(re.sub(r"\s+", "", raw_text))
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
    if expected_optional_dependency is not None:
        expected_optional = _expected_optional_requirement_contracts(
            expected_optional_dependency
        )
        if (
            len(optional_requirements) != len(expected_optional)
            or set(optional_requirements) != set(expected_optional)
        ):
            raise ArtifactVerificationError(
                label + " optional dependency set does not match"
            )
    return identity


def _package_sources(
    configured: Optional[Sequence[PackageSource]],
    legacy_package_root: Optional[str],
) -> Tuple[PackageSource, ...]:
    if configured is None:
        if legacy_package_root is None:
            raise ArtifactVerificationError("at least one package source is required")
        values: Sequence[PackageSource] = (
            (legacy_package_root, "src/" + legacy_package_root),
        )
    else:
        values = tuple(configured)
        if legacy_package_root is not None:
            raise ArtifactVerificationError(
                "legacy package root and package sources cannot be combined"
            )
    normalized: List[PackageSource] = []
    seen_roots = set()
    seen_sources = set()
    for package_root, source_path in values:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package_root) is None:
            raise ArtifactVerificationError("package root is invalid")
        safe_source = _safe_member_name(source_path, "package source")
        if safe_source != source_path.rstrip("/") or safe_source.split("/")[-1] != package_root:
            raise ArtifactVerificationError("package source must end in its package root")
        if package_root in seen_roots or safe_source in seen_sources:
            raise ArtifactVerificationError("package source mapping is ambiguous")
        seen_roots.add(package_root)
        seen_sources.add(safe_source)
        normalized.append((package_root, safe_source))
    if not normalized:
        raise ArtifactVerificationError("at least one package source is required")
    return tuple(normalized)


def _required_package_files(
    configured: Sequence[RequiredPackageFile],
    package_roots: Sequence[str],
) -> Tuple[RequiredPackageFile, ...]:
    roots = set(package_roots)
    normalized: List[RequiredPackageFile] = []
    seen = set()
    for package_root, relative_path in configured:
        if package_root not in roots:
            raise ArtifactVerificationError(
                "required package file references an unknown package root"
            )
        safe_relative = _safe_member_name(relative_path, "required package file")
        key = (package_root, safe_relative)
        if safe_relative != relative_path.rstrip("/") or key in seen:
            raise ArtifactVerificationError("required package file is ambiguous")
        seen.add(key)
        normalized.append((package_root, safe_relative))
    return tuple(normalized)


def _parse_entry_points(payload: bytes, label: str) -> Dict[str, Dict[str, str]]:
    if not payload or len(payload) > _MAX_METADATA_BYTES:
        raise ArtifactVerificationError(label + " entry-point metadata is invalid")
    try:
        lines = payload.decode("utf-8", "strict").splitlines()
    except UnicodeError as exc:
        raise ArtifactVerificationError(
            label + " entry-point metadata is not UTF-8"
        ) from exc
    result: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        section_match = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if section_match is not None:
            current = section_match.group(1)
            if current in result:
                raise ArtifactVerificationError(
                    label + " entry-point section is duplicated"
                )
            result[current] = {}
            continue
        if current is None or line.startswith(("#", ";")):
            raise ArtifactVerificationError(
                label + " entry-point metadata is malformed"
            )
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)\s*=\s*"
            r"([A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*)",
            line,
        )
        if match is None:
            raise ArtifactVerificationError(
                label + " entry-point metadata is malformed"
            )
        name, target = match.groups()
        if name in result[current]:
            raise ArtifactVerificationError(
                label + " entry-point name is duplicated"
            )
        result[current][name] = target
    if not result:
        raise ArtifactVerificationError(label + " entry-point metadata is empty")
    return result


def _validate_entry_points(
    payload: bytes,
    label: str,
    expected_entry_points: ExpectedEntryPoints,
) -> None:
    if expected_entry_points is None:
        return
    expected = {
        str(group): {str(name): str(target) for name, target in entries.items()}
        for group, entries in expected_entry_points.items()
    }
    if _parse_entry_points(payload, label) != expected:
        raise ArtifactVerificationError(label + " entry-point set does not match")


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
    package_sources: Sequence[PackageSource],
    required_package_files: Sequence[RequiredPackageFile],
    expected_dependency: ExpectedDependencies,
    expected_optional_dependency: ExpectedDependencies,
    forbidden_dependencies: Sequence[str],
    expected_entry_points: ExpectedEntryPoints,
) -> Tuple[str, str]:
    distribution = _distribution_component(expected_name)
    version = _version_component(expected_version)
    expected_filename = distribution + "-" + version + "-py3-none-any.whl"
    if path.name != expected_filename:
        raise ArtifactVerificationError("wheel filename does not match the release identity")
    names, payloads = _bounded_zip_payloads(path)
    expected_dist_info = distribution + "-" + version + ".dist-info"
    package_roots = tuple(package_root for package_root, _ in package_sources)
    allowed_roots = set(package_roots) | {expected_dist_info}
    if any(name.split("/", 1)[0] not in allowed_roots for name in names):
        raise ArtifactVerificationError("wheel contains an unexpected distribution root")
    for name in names:
        parts = name.split("/")
        for index, part in enumerate(parts):
            if part.endswith(".dist-info") and not (
                index == 0 and part == expected_dist_info
            ):
                raise ArtifactVerificationError("wheel contains an unexpected dist-info root")
    for package_root in package_roots:
        if package_root + "/__init__.py" not in payloads:
            raise ArtifactVerificationError("wheel is missing an expected package root")
    for package_root, relative_path in required_package_files:
        if package_root + "/" + relative_path not in payloads:
            raise ArtifactVerificationError("wheel is missing a required package file")
    metadata_path = expected_dist_info + "/METADATA"
    wheel_path = expected_dist_info + "/WHEEL"
    entry_points_path = expected_dist_info + "/entry_points.txt"
    record_path = expected_dist_info + "/RECORD"
    for required in (metadata_path, wheel_path, record_path):
        if required not in payloads:
            raise ArtifactVerificationError("wheel is missing required dist-info metadata")
    if not payloads[wheel_path] or len(payloads[wheel_path]) > _MAX_METADATA_BYTES:
        raise ArtifactVerificationError("wheel WHEEL metadata is invalid")
    if expected_entry_points is not None:
        if entry_points_path not in payloads:
            raise ArtifactVerificationError("wheel is missing entry-point metadata")
        _validate_entry_points(
            payloads[entry_points_path], "wheel", expected_entry_points
        )
    _validate_record(payloads, record_path)
    return _validate_metadata(
        payloads[metadata_path],
        "wheel",
        expected_name=expected_name,
        expected_version=expected_version,
        expected_dependency=expected_dependency,
        forbidden_dependencies=forbidden_dependencies,
        expected_optional_dependency=expected_optional_dependency,
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
    package_sources: Sequence[PackageSource],
    required_package_files: Sequence[RequiredPackageFile],
    expected_dependency: ExpectedDependencies,
    expected_optional_dependency: ExpectedDependencies,
    forbidden_dependencies: Sequence[str],
    expected_entry_points: ExpectedEntryPoints,
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
    egg_metadata_candidates = (
        expected_root + "/" + expected_egg_info + "/PKG-INFO",
        expected_root + "/src/" + expected_egg_info + "/PKG-INFO",
    )
    present_egg_metadata = tuple(
        candidate for candidate in egg_metadata_candidates if candidate in payloads
    )
    if len(present_egg_metadata) != 1:
        raise ArtifactVerificationError(
            "sdist must contain exactly one expected egg-info identity"
        )
    egg_metadata = present_egg_metadata[0]
    allowed_egg_prefix = egg_metadata.rsplit("/", 1)[0]
    source_children: Dict[str, set] = {}
    for package_root, source_path in package_sources:
        parent, _, child = source_path.rpartition("/")
        if child != package_root:
            raise ArtifactVerificationError("package source mapping is inconsistent")
        source_children.setdefault(parent, set()).add(package_root)
        expected_init = expected_root + "/" + source_path + "/__init__.py"
        if expected_init not in payloads:
            raise ArtifactVerificationError("sdist is missing an expected package root")
    for package_root, relative_path in required_package_files:
        source_path = dict(package_sources)[package_root]
        if expected_root + "/" + source_path + "/" + relative_path not in payloads:
            raise ArtifactVerificationError("sdist is missing a required package file")
    for name in names:
        if ".dist-info" in name.split("/"):
            raise ArtifactVerificationError("sdist contains a wheel dist-info root")
        parts = name.split("/")
        for index, part in enumerate(parts):
            if part.endswith(".egg-info"):
                prefix = "/".join(parts[: index + 1])
                if prefix != allowed_egg_prefix:
                    raise ArtifactVerificationError(
                        "sdist contains an unexpected source root (egg-info)"
                    )
        for parent, allowed_children in source_children.items():
            container = expected_root + "/" + parent
            if name == container:
                continue
            if name.startswith(container + "/"):
                relative = name[len(container) + 1 :]
                source_root = relative.split("/", 1)[0]
                allowed = set(allowed_children)
                if allowed_egg_prefix == container + "/" + expected_egg_info:
                    allowed.add(expected_egg_info)
                if source_root not in allowed:
                    raise ArtifactVerificationError(
                        "sdist contains an unexpected source root"
                    )
    root_metadata = expected_root + "/PKG-INFO"
    if root_metadata not in payloads:
        raise ArtifactVerificationError("sdist is missing its root PKG-INFO")
    allowed_metadata = {
        root_metadata,
        egg_metadata,
    }
    if any(name.endswith("/PKG-INFO") and name not in allowed_metadata for name in payloads):
        raise ArtifactVerificationError("sdist contains an unexpected PKG-INFO")
    identity = _validate_metadata(
        payloads[root_metadata],
        "sdist",
        expected_name=expected_name,
        expected_version=expected_version,
        expected_dependency=expected_dependency,
        forbidden_dependencies=forbidden_dependencies,
        expected_optional_dependency=expected_optional_dependency,
    )
    if _validate_metadata(
        payloads[egg_metadata],
        "sdist egg-info",
        expected_name=expected_name,
        expected_version=expected_version,
        expected_dependency=expected_dependency,
        forbidden_dependencies=forbidden_dependencies,
        expected_optional_dependency=expected_optional_dependency,
    ) != identity:
        raise ArtifactVerificationError("sdist metadata identities differ")
    if expected_entry_points is not None:
        entry_points_path = allowed_egg_prefix + "/entry_points.txt"
        if entry_points_path not in payloads:
            raise ArtifactVerificationError("sdist is missing entry-point metadata")
        _validate_entry_points(
            payloads[entry_points_path], "sdist", expected_entry_points
        )
    return identity


def verify_release_directory(
    directory: Path,
    *,
    expected_name: str,
    expected_version: str,
    package_root: Optional[str] = None,
    forbidden_package_root: Optional[str] = None,
    expected_dependency: ExpectedDependencies = None,
    package_sources: Optional[Sequence[PackageSource]] = None,
    required_package_files: Sequence[RequiredPackageFile] = (),
    expected_optional_dependency: ExpectedDependencies = None,
    forbidden_dependencies: Sequence[str] = (),
    expected_entry_points: ExpectedEntryPoints = None,
) -> Tuple[Path, Path]:
    resolved_sources = _package_sources(package_sources, package_root)
    resolved_required_files = _required_package_files(
        required_package_files,
        tuple(package for package, _ in resolved_sources),
    )
    resolved_forbidden_dependencies = tuple(forbidden_dependencies)
    if forbidden_package_root is not None:
        resolved_forbidden_dependencies += (_normalized(forbidden_package_root),)
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
            package_sources=resolved_sources,
            required_package_files=resolved_required_files,
            expected_dependency=expected_dependency,
            expected_optional_dependency=expected_optional_dependency,
            forbidden_dependencies=resolved_forbidden_dependencies,
            expected_entry_points=expected_entry_points,
        ),
        _sdist_identity(
            sdist,
            expected_name=expected_name,
            expected_version=expected_version,
            package_sources=resolved_sources,
            required_package_files=resolved_required_files,
            expected_dependency=expected_dependency,
            expected_optional_dependency=expected_optional_dependency,
            forbidden_dependencies=resolved_forbidden_dependencies,
            expected_entry_points=expected_entry_points,
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
    package = parser.add_mutually_exclusive_group(required=True)
    package.add_argument("--package-root")
    package.add_argument(
        "--package-source",
        action="append",
        help="Require a wheel root and sdist source mapping as ROOT=PATH; repeat",
    )
    parser.add_argument("--forbid-package-root")
    parser.add_argument(
        "--require-package-file",
        action="append",
        help="Require a file inside one package root as ROOT=RELATIVE_PATH; repeat",
    )
    parser.add_argument(
        "--forbid-requires-dist",
        action="append",
        help="Reject this distribution name from every Requires-Dist field; repeat",
    )
    parser.add_argument(
        "--requires-dist",
        action="append",
        help=(
            "Require this unconditional runtime dependency with exactly these "
            "specifiers; repeat for the complete default-install dependency set"
        ),
    )
    parser.add_argument(
        "--optional-requires-dist",
        action="append",
        help=(
            "Require this exact optional dependency including an extra marker; "
            "repeat for the complete optional dependency set"
        ),
    )
    parser.add_argument(
        "--console-script",
        action="append",
        help="Require an exact console script as NAME=MODULE:ATTRIBUTE; repeat",
    )
    parser.add_argument(
        "--provider-entry-point",
        action="append",
        help="Require an exact provider entry point as NAME=MODULE:ATTRIBUTE; repeat",
    )
    return parser


def _assignment(value: str, label: str) -> Tuple[str, str]:
    key, separator, target = value.partition("=")
    if separator != "=" or not key or not target:
        raise ArtifactVerificationError(label + " must use NAME=VALUE")
    if key != key.strip() or target != target.strip():
        raise ArtifactVerificationError(label + " contains surrounding whitespace")
    return key, target


def _assignments(values: Optional[Sequence[str]], label: str) -> Tuple[Tuple[str, str], ...]:
    if values is None:
        return ()
    result = tuple(_assignment(value, label) for value in values)
    if len(result) != len({key for key, _ in result}):
        raise ArtifactVerificationError(label + " names are duplicated")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        package_sources = (
            _assignments(args.package_source, "package source")
            if args.package_source is not None
            else None
        )
        required_package_files = _assignments(
            args.require_package_file, "required package file"
        )
        console_scripts = dict(_assignments(args.console_script, "console script"))
        provider_entry_points = dict(
            _assignments(args.provider_entry_point, "provider entry point")
        )
        expected_entry_points: ExpectedEntryPoints = None
        if console_scripts or provider_entry_points:
            expected_entry_points = {
                "console_scripts": console_scripts,
                "unified_cli.providers.v1": provider_entry_points,
            }
        wheel, sdist = verify_release_directory(
            args.directory,
            expected_name=args.name,
            expected_version=args.version,
            package_root=args.package_root,
            forbidden_package_root=args.forbid_package_root,
            expected_dependency=args.requires_dist,
            package_sources=package_sources,
            required_package_files=required_package_files,
            expected_optional_dependency=args.optional_requires_dist,
            forbidden_dependencies=args.forbid_requires_dist or (),
            expected_entry_points=expected_entry_points,
        )
    except (ArtifactVerificationError, OSError) as exc:
        print("release artifact verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified release artifacts: " + wheel.name + " + " + sdist.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
