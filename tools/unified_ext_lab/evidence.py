"""Canonical, non-promotional evidence for the offline fixture harness.

Only the synthetic harness fixture can be represented here. The executor is
exactly either the in-memory fake or the separately gated real-Docker profile.
Both remain non-promotional and cannot be mistaken for provider evidence.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from .errors import InvariantRefusalError, UsageStateError
from .model import validate_lab_id, validate_provider_id
from .state import LabState, OperationObservation, StatePhase, strict_json_loads


EVIDENCE_SCHEMA = 1
MAX_EVIDENCE_BYTES = 1024 * 1024
EVIDENCE_KIND = "harness_fixture"
EXECUTOR_KIND = "fake_docker"
REAL_EXECUTOR_KIND = "real_docker"
EXECUTOR_KINDS = frozenset((EXECUTOR_KIND, REAL_EXECUTOR_KIND))
PROMOTION_ELIGIBLE = False

SOURCE_KINDS = frozenset(
    (
        "archive",
        "fixture",
        "git_commit",
        "local_fixture",
        "package_index",
        "repository",
        "sdist",
        "wheel",
    )
)
MUTABLE_ALIASES = frozenset(
    ("latest", "current", "stable", "head", "main", "master", "nightly", "snapshot", "dev")
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
_TEMPORARY_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")

_ARTIFACT_KEYS = frozenset(("package", "version", "source_kind", "source_locator", "sha256"))
_PLATFORM_KEYS = frozenset(("evidence_kind", "executor_kind", "promotion_eligible"))
_SCHEMA_HASH_KEYS = frozenset(
    ("manifest_schema_sha256", "observed_protocol_schema_sha256")
)
_CLEANUP_KEYS = frozenset(
    (
        "created_count",
        "removed_count",
        "remaining_count",
        "logout_succeeded",
        "destroy_succeeded",
        "verified_clean",
    )
)
_DRAFT_KEYS = frozenset(
    (
        "evidence_kind",
        "executor_kind",
        "promotion_eligible",
        "artifact",
        "manifest_schema_sha256",
        "observed_protocol_schema_sha256",
        "operations",
        "captured_at_ns",
    )
)
_MANIFEST_KEYS = frozenset(
    (
        "schema",
        "evidence_kind",
        "executor_kind",
        "promotion_eligible",
        "lab_id",
        "provider_id",
        "artifact",
        "manifest_schema_sha256",
        "observed_protocol_schema_sha256",
        "operations",
        "cleanup",
        "captured_at_ns",
        "result",
    )
)
_FORBIDDEN_FIELDS = frozenset(
    (
        "argv",
        "stdout",
        "stderr",
        "env",
        "environment",
        "prompt",
        "response",
        "account",
        "session",
        "hostname",
        "username",
        "pid",
        "container_id",
        "container_ids",
        "absolute_path",
        "path",
        "credential",
        "credential_contents",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "source_text",
        "receipt",
        "receipt_path",
        "uid",
        "inode",
        "url",
    )
)


class _EvidenceParentChangedError(InvariantRefusalError):
    """Internal marker: never reconcile through a changed parent namespace."""


class _EvidenceFinalNameRaceError(InvariantRefusalError):
    """Internal marker: the final name appeared during atomic publication."""


def _exact_mapping(value: object, keys: frozenset, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value.keys()) != keys:
        raise UsageStateError("invalid {} fields".format(field))
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise UsageStateError("invalid {}".format(field))
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise UsageStateError("invalid {}".format(field))
    return value


def _reject_unsafe_tree(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str:
                raise UsageStateError("evidence keys must be strings")
            if key.lower() in _FORBIDDEN_FIELDS:
                raise InvariantRefusalError("forbidden evidence field")
            _reject_unsafe_tree(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_unsafe_tree(nested)
    elif type(value) is float:
        raise UsageStateError("floating point evidence is not permitted")
    elif value is not None and type(value) not in (str, int, bool):
        raise UsageStateError("evidence contains a non-JSON value")


def validate_source_locator(value: object) -> str:
    """Validate a relative, immutable, URL-free acquisition locator."""

    if type(value) is not str or not value or len(value) > 512:
        raise UsageStateError("invalid artifact source locator")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise UsageStateError("invalid artifact source locator")
    if (
        value.startswith(("/", "\\", "~"))
        or "\\" in value
        or "://" in value
        or "@" in value
        or "?" in value
        or "#" in value
        or "%" in value
    ):
        raise InvariantRefusalError("artifact source locator is not a safe relative locator")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not parts
        or ":" in value
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise InvariantRefusalError("artifact source locator is not a safe relative locator")
    for part in parts:
        lowered = part.lower()
        stem_tokens = frozenset(token for token in re.split(r"[._+-]", lowered) if token)
        if lowered in MUTABLE_ALIASES or stem_tokens.intersection(MUTABLE_ALIASES):
            raise InvariantRefusalError("artifact source locator uses a mutable alias")
    return value


@dataclass(frozen=True)
class ArtifactEvidence:
    package: str
    version: str
    source_kind: str
    source_locator: str
    sha256: str

    def __post_init__(self) -> None:
        if type(self.package) is not str or NAME_RE.fullmatch(self.package) is None:
            raise UsageStateError("invalid artifact package")
        if (
            type(self.version) is not str
            or VERSION_RE.fullmatch(self.version) is None
            or self.version.lower() in MUTABLE_ALIASES
        ):
            raise UsageStateError("invalid immutable artifact version")
        if self.source_kind not in SOURCE_KINDS:
            raise UsageStateError("invalid artifact source kind")
        locator = validate_source_locator(self.source_locator)
        digest = _sha256(self.sha256, "artifact sha256")
        # A relative locator is considered immutable only when it carries the
        # pinned version or a digest prefix, not merely a mutable package name.
        if self.version not in locator and digest[:12] not in locator:
            raise InvariantRefusalError("artifact source locator is not version or digest pinned")

    @classmethod
    def from_value(cls, value: Union["ArtifactEvidence", Mapping[str, object]]) -> "ArtifactEvidence":
        if type(value) is cls:
            return value
        data = _exact_mapping(value, _ARTIFACT_KEYS, "artifact")
        return cls(
            package=data["package"],
            version=data["version"],
            source_kind=data["source_kind"],
            source_locator=data["source_locator"],
            sha256=data["sha256"],
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "package": self.package,
            "sha256": self.sha256,
            "source_kind": self.source_kind,
            "source_locator": self.source_locator,
            "version": self.version,
        }


@dataclass(frozen=True)
class FixturePlatform:
    evidence_kind: str = EVIDENCE_KIND
    executor_kind: str = EXECUTOR_KIND
    promotion_eligible: bool = PROMOTION_ELIGIBLE

    def __post_init__(self) -> None:
        if (
            self.evidence_kind != EVIDENCE_KIND
            or self.executor_kind not in EXECUTOR_KINDS
        ):
            raise UsageStateError("invalid harness fixture execution kind")
        if self.promotion_eligible is not False:
            raise InvariantRefusalError("fixture evidence cannot be promotional")

    @classmethod
    def from_value(cls, value: Union["FixturePlatform", Mapping[str, object]]) -> "FixturePlatform":
        if type(value) is cls:
            return value
        data = _exact_mapping(value, _PLATFORM_KEYS, "platform")
        return cls(
            evidence_kind=data["evidence_kind"],
            executor_kind=data["executor_kind"],
            promotion_eligible=data["promotion_eligible"],
        )


@dataclass(frozen=True)
class SchemaHashes:
    manifest_schema_sha256: str
    observed_protocol_schema_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.manifest_schema_sha256, "manifest schema sha256")
        _sha256(self.observed_protocol_schema_sha256, "observed protocol schema sha256")

    @classmethod
    def from_value(cls, value: Union["SchemaHashes", Mapping[str, object]]) -> "SchemaHashes":
        if type(value) is cls:
            return value
        data = _exact_mapping(value, _SCHEMA_HASH_KEYS, "schema hashes")
        return cls(
            manifest_schema_sha256=data["manifest_schema_sha256"],
            observed_protocol_schema_sha256=data["observed_protocol_schema_sha256"],
        )


@dataclass(frozen=True)
class CleanupEvidence:
    created_count: int
    removed_count: int
    remaining_count: int
    logout_succeeded: bool
    destroy_succeeded: bool
    verified_clean: bool

    def __post_init__(self) -> None:
        _nonnegative_int(self.created_count, "cleanup created count")
        _nonnegative_int(self.removed_count, "cleanup removed count")
        _nonnegative_int(self.remaining_count, "cleanup remaining count")
        if any(
            type(value) is not bool
            for value in (self.logout_succeeded, self.destroy_succeeded, self.verified_clean)
        ):
            raise UsageStateError("cleanup flags must be booleans")
        if self.removed_count > self.created_count:
            raise UsageStateError("cleanup removed count exceeds created count")
        if self.created_count != self.removed_count + self.remaining_count:
            raise UsageStateError("cleanup counts do not reconcile")
        if self.verified_clean and self.remaining_count != 0:
            raise UsageStateError("verified-clean cleanup cannot have remaining resources")

    @property
    def fully_clean(self) -> bool:
        return self.verified_clean and self.remaining_count == 0

    @classmethod
    def from_value(cls, value: Union["CleanupEvidence", Mapping[str, object]]) -> "CleanupEvidence":
        if type(value) is cls:
            return value
        data = _exact_mapping(value, _CLEANUP_KEYS, "cleanup")
        return cls(
            created_count=data["created_count"],
            removed_count=data["removed_count"],
            remaining_count=data["remaining_count"],
            logout_succeeded=data["logout_succeeded"],
            destroy_succeeded=data["destroy_succeeded"],
            verified_clean=data["verified_clean"],
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "created_count": self.created_count,
            "destroy_succeeded": self.destroy_succeeded,
            "logout_succeeded": self.logout_succeeded,
            "remaining_count": self.remaining_count,
            "removed_count": self.removed_count,
            "verified_clean": self.verified_clean,
        }


def artifact_from_receipt_fields(
    *, package: object, version: object, source_kind: object, source_locator: object, sha256: object
) -> ArtifactEvidence:
    """Project explicit safe acquisition fields; never accept/serialize a receipt."""

    return ArtifactEvidence(
        package=package,
        version=version,
        source_kind=source_kind,
        source_locator=source_locator,
        sha256=sha256,
    )


def _clock_value(clock_ns: Callable[[], int]) -> int:
    try:
        value = clock_ns()
    except Exception as error:
        raise UsageStateError("evidence clock failed") from error
    return _nonnegative_int(value, "evidence capture time")


def capture_draft(
    state: LabState,
    artifact: Union[ArtifactEvidence, Mapping[str, object]],
    platform: Union[FixturePlatform, Mapping[str, object]],
    schema_hashes: Union[SchemaHashes, Mapping[str, object]],
    *,
    clock_ns: Callable[[], int] = time.time_ns,
) -> Mapping[str, object]:
    """Capture the strict draft to attach during EVIDENCE_PENDING.

    The caller persists the returned mapping by transitioning to
    ``EVIDENCE_CAPTURED`` with ``draft_evidence=draft``.
    """

    if type(state) is not LabState or state.phase not in (
        StatePhase.EVIDENCE_PENDING,
        StatePhase.CLEAN_VERIFIED,
    ):
        raise UsageStateError("evidence draft requires EVIDENCE_PENDING or CLEAN_VERIFIED")
    if state.phase is StatePhase.CLEAN_VERIFIED and not any(
        operation.outcome == "failed" for operation in state.operations
    ):
        raise UsageStateError("post-cleanup draft requires a recorded failure")
    if state.tainted:
        raise InvariantRefusalError(
            "verification/promotion-held state cannot produce evidence"
        )
    artifact_value = ArtifactEvidence.from_value(artifact)
    if artifact_value.to_dict() != dict(state.artifact_evidence):
        raise InvariantRefusalError(
            "captured artifact does not match durable artifact evidence"
        )
    platform_value = FixturePlatform.from_value(platform)
    hashes = SchemaHashes.from_value(schema_hashes)
    draft = {
        "artifact": artifact_value.to_dict(),
        "captured_at_ns": _clock_value(clock_ns),
        "evidence_kind": platform_value.evidence_kind,
        "executor_kind": platform_value.executor_kind,
        "manifest_schema_sha256": hashes.manifest_schema_sha256,
        "observed_protocol_schema_sha256": hashes.observed_protocol_schema_sha256,
        "operations": [operation.to_dict() for operation in state.operations],
        "promotion_eligible": False,
    }
    validate_draft(draft)
    return MappingProxyType(
        {
            "artifact": MappingProxyType(dict(draft["artifact"])),
            "captured_at_ns": draft["captured_at_ns"],
            "evidence_kind": draft["evidence_kind"],
            "executor_kind": draft["executor_kind"],
            "manifest_schema_sha256": draft["manifest_schema_sha256"],
            "observed_protocol_schema_sha256": draft[
                "observed_protocol_schema_sha256"
            ],
            "operations": tuple(
                MappingProxyType(dict(operation)) for operation in draft["operations"]
            ),
            "promotion_eligible": False,
        }
    )


def validate_draft(value: object) -> Mapping[str, object]:
    data = _exact_mapping(value, _DRAFT_KEYS, "evidence draft")
    ArtifactEvidence.from_value(data["artifact"])
    FixturePlatform(
        evidence_kind=data["evidence_kind"],
        executor_kind=data["executor_kind"],
        promotion_eligible=data["promotion_eligible"],
    )
    SchemaHashes(
        manifest_schema_sha256=data["manifest_schema_sha256"],
        observed_protocol_schema_sha256=data["observed_protocol_schema_sha256"],
    )
    if type(data["operations"]) not in (list, tuple):
        raise UsageStateError("invalid evidence operations")
    for operation in data["operations"]:
        OperationObservation.from_dict(operation)
    _nonnegative_int(data["captured_at_ns"], "evidence capture time")
    _reject_unsafe_tree(data)
    return data


def build_manifest(
    state: LabState,
    cleanup: Union[CleanupEvidence, Mapping[str, object]],
) -> Dict[str, object]:
    """Build, but do not write, the final manifest after verified cleanup."""

    if type(state) is not LabState or state.phase is not StatePhase.SEAL_PENDING:
        raise UsageStateError("manifest sealing requires SEAL_PENDING")
    if state.tainted:
        raise InvariantRefusalError(
            "verification/promotion-held state cannot seal evidence"
        )
    if state.draft_evidence is None:
        raise UsageStateError("manifest sealing requires captured evidence")
    clean = CleanupEvidence.from_value(cleanup)
    if not clean.fully_clean:
        raise InvariantRefusalError("manifest sealing requires verified clean resources")
    ledger_created = len(state.created_roles)
    ledger_removed = len(state.removed_roles)
    ledger_remaining = ledger_created - ledger_removed
    if (
        clean.created_count != ledger_created
        or clean.removed_count != ledger_removed
        or clean.remaining_count != ledger_remaining
    ):
        raise InvariantRefusalError("cleanup evidence does not match the durable role ledger")
    draft = validate_draft(dict(state.draft_evidence))
    if dict(draft["artifact"]) != dict(state.artifact_evidence):
        raise InvariantRefusalError(
            "draft artifact does not match durable artifact evidence"
        )
    draft_operations = tuple(
        OperationObservation.from_dict(item) for item in draft["operations"]
    )
    if state.operations[: len(draft_operations)] != draft_operations:
        raise InvariantRefusalError("captured operations are not a prefix of final operations")
    operations = state.operations
    cleanup_steps = frozenset(operation.step for operation in operations)
    if not frozenset(("logout", "destroy", "verify_clean")).issubset(cleanup_steps):
        raise InvariantRefusalError("final evidence is missing cleanup observations")
    failed = (
        any(operation.outcome != "succeeded" for operation in operations)
        or not clean.logout_succeeded
        or not clean.destroy_succeeded
    )
    manifest = {
        "artifact": dict(draft["artifact"]),
        "captured_at_ns": draft["captured_at_ns"],
        "cleanup": clean.to_dict(),
        "evidence_kind": EVIDENCE_KIND,
        "executor_kind": draft["executor_kind"],
        "lab_id": state.lab_id,
        "manifest_schema_sha256": draft["manifest_schema_sha256"],
        "observed_protocol_schema_sha256": draft["observed_protocol_schema_sha256"],
        "operations": [operation.to_dict() for operation in operations],
        "promotion_eligible": False,
        "provider_id": state.provider_id,
        "result": "failed_clean" if failed else "passed",
        "schema": EVIDENCE_SCHEMA,
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: object) -> Mapping[str, object]:
    data = _exact_mapping(value, _MANIFEST_KEYS, "evidence manifest")
    if data["schema"] != EVIDENCE_SCHEMA:
        raise UsageStateError("unsupported evidence schema")
    if (
        data["evidence_kind"] != EVIDENCE_KIND
        or data["executor_kind"] not in EXECUTOR_KINDS
    ):
        raise UsageStateError("invalid evidence execution kind")
    if data["promotion_eligible"] is not False:
        raise InvariantRefusalError("fixture evidence cannot be promotional")
    ArtifactEvidence.from_value(data["artifact"])
    SchemaHashes(
        manifest_schema_sha256=data["manifest_schema_sha256"],
        observed_protocol_schema_sha256=data["observed_protocol_schema_sha256"],
    )
    if type(data["operations"]) is not list:
        raise UsageStateError("invalid evidence operations")
    operations = tuple(OperationObservation.from_dict(item) for item in data["operations"])
    cleanup = CleanupEvidence.from_value(data["cleanup"])
    if not cleanup.fully_clean:
        raise InvariantRefusalError("final evidence is not fully clean")
    if data["result"] not in ("passed", "failed_clean"):
        raise UsageStateError("invalid evidence result")
    cleanup_steps = frozenset(operation.step for operation in operations)
    if not frozenset(("logout", "destroy", "verify_clean")).issubset(cleanup_steps):
        raise InvariantRefusalError("final evidence is missing cleanup observations")
    has_failure = (
        any(item.outcome != "succeeded" for item in operations)
        or not cleanup.logout_succeeded
        or not cleanup.destroy_succeeded
    )
    if data["result"] == "passed" and has_failure:
        raise UsageStateError("successful result contains a failed operation")
    if data["result"] == "failed_clean" and not has_failure:
        raise UsageStateError("failed-clean result contains no failure")
    _nonnegative_int(data["captured_at_ns"], "evidence capture time")
    validate_lab_id(data["lab_id"])
    validate_provider_id(data["provider_id"])
    _reject_unsafe_tree(data)
    return data


def canonical_evidence_bytes(value: object) -> bytes:
    """Return canonical sorted compact UTF-8 JSON with exactly one newline."""

    validate_manifest(value)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise UsageStateError("manifest is not strict JSON") from error
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise UsageStateError("evidence manifest is too large")
    return payload


def strict_evidence_loads(payload: bytes) -> Mapping[str, object]:
    """Parse and validate bounded evidence without losing duplicate keys."""

    value = strict_json_loads(payload, maximum_bytes=MAX_EVIDENCE_BYTES)
    return validate_manifest(value)


def _check_private_parent(path: Path) -> None:
    if (
        not path.is_absolute()
        or os.path.normpath(str(path)) != str(path)
        or not path.name
        or any(ord(character) < 0x20 for character in path.name)
        or not path.parent.exists()
    ):
        raise UsageStateError("evidence output path must be absolute with an existing parent")
    current = Path(path.parent.anchor)
    for component in path.parent.parts[1:]:
        current = current / component
        if current.exists() and current.is_symlink() and str(current) not in ("/var", "/tmp"):
            raise InvariantRefusalError("unsafe evidence output directory component")
    st = path.parent.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InvariantRefusalError("unsafe evidence output directory")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise InvariantRefusalError("evidence output directory is not owned")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise InvariantRefusalError("evidence output directory permissions must be 0700")


def _is_private_owned_regular(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode)
        and stat.S_IMODE(st.st_mode) == 0o600
        and (not hasattr(os, "geteuid") or st.st_uid == os.geteuid())
    )


def _file_identity(st: os.stat_result) -> Tuple[int, int, int, int, int, int, int, int]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_nlink,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _read_bounded_descriptor(descriptor: int) -> bytes:
    chunks = []
    remaining = MAX_EVIDENCE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _fsync_directory(path: Path) -> None:
    try:
        before = path.lstat()
    except OSError as error:
        raise InvariantRefusalError(
            "evidence directory changed during durability check"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise InvariantRefusalError("unsafe evidence durability directory")
    identity = _directory_identity(before)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise InvariantRefusalError(
            "evidence directory changed during durability check"
        ) from error
    try:
        opened = os.fstat(descriptor)
        post_open = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(post_open.st_mode)
            or not stat.S_ISDIR(post_open.st_mode)
            or _directory_identity(opened) != identity
            or _directory_identity(post_open) != identity
        ):
            raise InvariantRefusalError(
                "evidence directory changed during durability check"
            )
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            _directory_identity(after) != identity
            or stat.S_ISLNK(final.st_mode)
            or not stat.S_ISDIR(final.st_mode)
            or _directory_identity(final) != identity
        ):
            raise InvariantRefusalError(
                "evidence directory changed during durability check"
            )
    finally:
        os.close(descriptor)


def _directory_identity(st: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid)


def _open_pinned_private_parent(path: Path) -> Tuple[int, Tuple[int, int, int, int, int]]:
    """Open and bind the exact private parent named by a canonical output path."""

    _check_private_parent(path)
    try:
        before = path.parent.lstat()
    except OSError as error:
        raise _EvidenceParentChangedError(
            "evidence output directory changed during acquisition"
        ) from error
    identity = _directory_identity(before)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path.parent), flags)
    except OSError as error:
        raise _EvidenceParentChangedError(
            "evidence output directory changed during acquisition"
        ) from error
    try:
        opened = os.fstat(descriptor)
        post_open = path.parent.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(post_open.st_mode)
            or not stat.S_ISDIR(post_open.st_mode)
            or _directory_identity(opened) != identity
            or _directory_identity(post_open) != identity
        ):
            raise _EvidenceParentChangedError(
                "evidence output directory changed during acquisition"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _validate_pinned_private_parent(
    path: Path,
    descriptor: int,
    identity: Tuple[int, int, int, int, int],
) -> None:
    opened = os.fstat(descriptor)
    try:
        named = path.parent.lstat()
    except OSError as error:
        raise _EvidenceParentChangedError(
            "evidence output directory changed during publication"
        ) from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _directory_identity(opened) != identity
        or _directory_identity(named) != identity
    ):
        raise _EvidenceParentChangedError(
            "evidence output directory changed during publication"
        )


def _is_generated_temporary_name(output: Path, candidate: Path) -> bool:
    prefix = ".{}.".format(output.name)
    suffix = ".tmp"
    name = candidate.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix):-len(suffix)]
    return _TEMPORARY_TOKEN_RE.fullmatch(token) is not None


def _recover_generated_temporary_links(
    path: Path,
    expected: bytes,
    final_descriptor: int,
    final_st: os.stat_result,
    parent_descriptor: int,
    parent_identity: Tuple[int, int, int, int, int],
) -> None:
    """Remove only fully explained crash-time links to a published manifest."""

    identity = _file_identity(final_st)
    candidates = []
    _validate_pinned_private_parent(path, parent_descriptor, parent_identity)
    for candidate_name in os.listdir(parent_descriptor):
        candidate = path.parent / candidate_name
        if not _is_generated_temporary_name(path, candidate):
            continue
        try:
            candidate_st = os.stat(
                candidate_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if _file_identity(candidate_st) != identity:
            raise InvariantRefusalError("unexplained generated evidence temporary")
        if not _is_private_owned_regular(candidate_st):
            raise InvariantRefusalError("unsafe generated evidence temporary link")

        descriptor = os.open(
            candidate_name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            observed = _read_bounded_descriptor(descriptor)
            after = os.fstat(descriptor)
            if (
                _file_identity(opened) != identity
                or not _is_private_owned_regular(opened)
                or _file_identity(after) != _file_identity(opened)
                or observed != expected
            ):
                raise InvariantRefusalError(
                    "unsafe generated evidence temporary link"
                )
        finally:
            os.close(descriptor)
        current = os.stat(
            candidate_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(current) != identity
            or not _is_private_owned_regular(current)
        ):
            raise InvariantRefusalError("generated evidence temporary link changed")
        candidates.append(candidate_name)

    current_final = os.fstat(final_descriptor)
    if (
        _file_identity(current_final) != identity
        or not _is_private_owned_regular(current_final)
        or current_final.st_nlink != len(candidates) + 1
    ):
        raise InvariantRefusalError("unexplained evidence output hardlink")
    if len(candidates) > 1:
        # One publication attempt creates exactly one generated temp name.
        # Refuse ambiguous extras before mutating any directory entry.
        raise InvariantRefusalError("unexplained evidence output hardlink")

    removed_any = False
    try:
        for candidate_name in candidates:
            _validate_pinned_private_parent(
                path, parent_descriptor, parent_identity
            )
            current = os.stat(
                candidate_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _file_identity(current) != identity
                or not _is_private_owned_regular(current)
            ):
                raise InvariantRefusalError(
                    "generated evidence temporary link changed"
                )
            os.unlink(candidate_name, dir_fd=parent_descriptor)
            removed_any = True
    finally:
        if removed_any:
            os.fsync(parent_descriptor)
            _validate_pinned_private_parent(
                path, parent_descriptor, parent_identity
            )

    current_final = os.fstat(final_descriptor)
    current_named = os.stat(
        path.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        _file_identity(current_named) != _file_identity(current_final)
        or not _is_private_owned_regular(current_final)
        or current_final.st_nlink != 1
    ):
        raise InvariantRefusalError("evidence output hardlink recovery failed")


def _same_bound_private_file(
    before: Tuple[int, int, int, int, int, int, int, int],
    after: os.stat_result,
) -> bool:
    """Match the created inode while allowing hard-link ctime/nlink changes."""

    observed = _file_identity(after)
    return (
        observed[:4] == before[:4]
        and observed[5:7] == before[5:7]
        and observed[-1] >= before[-1]
    )


def _read_private_named_at(
    parent_descriptor: int, name: str
) -> Tuple[bytes, os.stat_result]:
    try:
        named = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(named.st_mode) or not _is_private_owned_regular(named):
        raise InvariantRefusalError("unsafe evidence output file")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            _file_identity(opened) != _file_identity(named)
            or not _is_private_owned_regular(opened)
        ):
            raise InvariantRefusalError("evidence output identity changed")
        payload = _read_bounded_descriptor(descriptor)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(opened):
            raise InvariantRefusalError("evidence output identity changed")
    finally:
        os.close(descriptor)
    final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        _file_identity(final) != _file_identity(opened)
        or not _is_private_owned_regular(final)
    ):
        raise InvariantRefusalError("evidence output identity changed")
    return payload, final


def _unlink_bound_temporary_at(
    parent_descriptor: int,
    name: str,
    expected_identity: Tuple[int, int, int, int, int, int, int, int],
) -> bool:
    """Remove a generated temp only while it names the exact created inode."""

    try:
        current = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(current.st_mode)
        or not _is_private_owned_regular(current)
        or not _same_bound_private_file(expected_identity, current)
    ):
        raise InvariantRefusalError("generated evidence temporary changed")
    os.unlink(name, dir_fd=parent_descriptor)
    return True


def _atomic_create_no_overwrite(path: Path, payload: bytes) -> None:
    parent_descriptor, parent_identity = _open_pinned_private_parent(path)
    temporary_name = ".{}.{}.tmp".format(path.name, secrets.token_hex(8))
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    temporary_identity = None
    published = False
    temporary_removed = False
    try:
        try:
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise _EvidenceFinalNameRaceError(
                "evidence output already exists"
            )
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        if not _is_private_owned_regular(opened) or opened.st_nlink != 1:
            raise InvariantRefusalError("unsafe generated evidence temporary")
        temporary_identity = _file_identity(opened)
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = _read_bounded_descriptor(descriptor)
        after_read = os.fstat(descriptor)
        named = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            observed != payload
            or _file_identity(after_read) != temporary_identity
            or _file_identity(named) != temporary_identity
        ):
            raise InvariantRefusalError(
                "generated evidence temporary changed during creation"
            )
        _validate_pinned_private_parent(
            path, parent_descriptor, parent_identity
        )
        # Hard-link publication is atomic and fails if the final name already
        # exists. Both names are resolved beneath the pinned private parent.
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise _EvidenceFinalNameRaceError(
                "evidence output already exists"
            ) from error
        published = True
        linked = os.fstat(descriptor)
        named_temporary = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_bound_private_file(temporary_identity, linked)
            or linked.st_nlink != 2
            or _file_identity(named_temporary) != _file_identity(linked)
        ):
            raise InvariantRefusalError(
                "generated evidence temporary changed during publication"
            )
        temporary_removed = _unlink_bound_temporary_at(
            parent_descriptor, temporary_name, temporary_identity
        )
        after_unlink = os.fstat(descriptor)
        if (
            not temporary_removed
            or not _same_bound_private_file(temporary_identity, after_unlink)
            or after_unlink.st_nlink != 1
        ):
            raise InvariantRefusalError(
                "evidence output identity changed during publication"
            )
        os.fsync(parent_descriptor)
        _validate_pinned_private_parent(
            path, parent_descriptor, parent_identity
        )
        final_payload, final = _read_private_named_at(
            parent_descriptor, path.name
        )
        if (
            final_payload != payload
            or final.st_nlink != 1
            or not _same_bound_private_file(temporary_identity, final)
        ):
            raise InvariantRefusalError(
                "evidence output identity changed during publication"
            )
    finally:
        try:
            if (
                descriptor is not None
                and not temporary_removed
                and not published
            ):
                current_identity = _file_identity(os.fstat(descriptor))
                if _unlink_bound_temporary_at(
                    parent_descriptor,
                    temporary_name,
                    current_identity,
                ):
                    os.fsync(parent_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)


def _read_existing_canonical(path: Path, expected: bytes) -> None:
    """Accept only the exact already-published private canonical manifest."""

    parent_descriptor, parent_identity = _open_pinned_private_parent(path)
    try:
        try:
            st = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise UsageStateError("evidence output is absent") from error
        if (
            stat.S_ISLNK(st.st_mode)
            or not _is_private_owned_regular(st)
        ):
            raise InvariantRefusalError("unsafe evidence output file")
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                _file_identity(opened) != _file_identity(st)
                or not _is_private_owned_regular(opened)
            ):
                raise InvariantRefusalError("evidence output identity changed")
            observed = _read_bounded_descriptor(descriptor)
            after_read = os.fstat(descriptor)
            if _file_identity(after_read) != _file_identity(opened):
                raise InvariantRefusalError("evidence output identity changed")
            if observed != expected:
                raise InvariantRefusalError(
                    "existing evidence output does not match seal intent"
                )
            if opened.st_nlink != 1:
                _recover_generated_temporary_links(
                    path,
                    expected,
                    descriptor,
                    opened,
                    parent_descriptor,
                    parent_identity,
                )
            verified = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(final) != _file_identity(verified)
            or final.st_nlink != 1
            or not _is_private_owned_regular(final)
        ):
            raise InvariantRefusalError("evidence output identity changed")
        _validate_pinned_private_parent(
            path, parent_descriptor, parent_identity
        )
        manifest = strict_evidence_loads(observed)
        if canonical_evidence_bytes(manifest) != observed:
            raise InvariantRefusalError(
                "existing evidence output is not canonical"
            )
    finally:
        os.close(parent_descriptor)


def reconcile_manifest_output(
    output_path: Union[str, os.PathLike], payload: bytes
) -> None:
    """Publish absent bytes or verify an exact prior crash-time publication."""

    if type(payload) is not bytes or len(payload) > MAX_EVIDENCE_BYTES:
        raise UsageStateError("invalid evidence payload")
    manifest = strict_evidence_loads(payload)
    if canonical_evidence_bytes(manifest) != payload:
        raise InvariantRefusalError("seal payload is not canonical")
    path = Path(output_path)
    if path.exists() or path.is_symlink():
        _read_existing_canonical(path, payload)
        return
    try:
        _atomic_create_no_overwrite(path, payload)
    except _EvidenceFinalNameRaceError:
        # A racing publisher is acceptable only when it created the exact
        # intended private canonical file.
        _read_existing_canonical(path, payload)
    else:
        _read_existing_canonical(path, payload)


def seal_manifest(
    state: LabState,
    cleanup: Union[CleanupEvidence, Mapping[str, object]],
    output_path: Union[str, os.PathLike],
) -> bytes:
    """Atomically create the final canonical manifest without overwriting."""

    manifest = build_manifest(state, cleanup)
    payload = canonical_evidence_bytes(manifest)
    _atomic_create_no_overwrite(Path(output_path), payload)
    return payload


# Compact compatibility names for command-layer callers.
EvidenceArtifact = ArtifactEvidence
EvidenceCleanup = CleanupEvidence
canonical_bytes = canonical_evidence_bytes
