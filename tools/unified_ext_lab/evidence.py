"""Canonical, non-promotional evidence for the offline fixture harness.

Only the fake-Docker harness fixture can be represented here.  The schema has
no extension fields and hard-codes ``promotion_eligible`` to false, preventing
fixture output from being mistaken for provider or production evidence.
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
        if self.evidence_kind != EVIDENCE_KIND or self.executor_kind != EXECUTOR_KIND:
            raise UsageStateError("only fake-Docker fixture evidence is supported")
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
        raise InvariantRefusalError("tainted shell state cannot produce evidence")
    artifact_value = ArtifactEvidence.from_value(artifact)
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
        raise InvariantRefusalError("tainted shell state cannot seal evidence")
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
        "executor_kind": EXECUTOR_KIND,
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
    if data["evidence_kind"] != EVIDENCE_KIND or data["executor_kind"] != EXECUTOR_KIND:
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
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
) -> None:
    """Remove only fully explained crash-time links to a published manifest."""

    identity = (final_st.st_dev, final_st.st_ino)
    candidates = []
    for candidate in path.parent.iterdir():
        if not _is_generated_temporary_name(path, candidate):
            continue
        try:
            candidate_st = candidate.lstat()
        except FileNotFoundError:
            continue
        if (candidate_st.st_dev, candidate_st.st_ino) != identity:
            raise InvariantRefusalError("unexplained generated evidence temporary")
        if not _is_private_owned_regular(candidate_st):
            raise InvariantRefusalError("unsafe generated evidence temporary link")

        descriptor = os.open(
            str(candidate), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != identity
                or not _is_private_owned_regular(opened)
                or _read_bounded_descriptor(descriptor) != expected
            ):
                raise InvariantRefusalError(
                    "unsafe generated evidence temporary link"
                )
        finally:
            os.close(descriptor)
        current = candidate.lstat()
        if (
            (current.st_dev, current.st_ino) != identity
            or not _is_private_owned_regular(current)
        ):
            raise InvariantRefusalError("generated evidence temporary link changed")
        candidates.append(candidate)

    current_final = os.fstat(final_descriptor)
    if (
        (current_final.st_dev, current_final.st_ino) != identity
        or not _is_private_owned_regular(current_final)
        or current_final.st_nlink != len(candidates) + 1
    ):
        raise InvariantRefusalError("unexplained evidence output hardlink")

    removed_any = False
    try:
        for candidate in candidates:
            current = candidate.lstat()
            if (
                (current.st_dev, current.st_ino) != identity
                or not _is_private_owned_regular(current)
            ):
                raise InvariantRefusalError(
                    "generated evidence temporary link changed"
                )
            candidate.unlink()
            removed_any = True
    finally:
        if removed_any:
            _fsync_directory(path.parent)

    current_final = os.fstat(final_descriptor)
    if (
        (current_final.st_dev, current_final.st_ino) != identity
        or not _is_private_owned_regular(current_final)
        or current_final.st_nlink != 1
    ):
        raise InvariantRefusalError("evidence output hardlink recovery failed")


def _atomic_create_no_overwrite(path: Path, payload: bytes) -> None:
    _check_private_parent(path)
    if path.exists() or path.is_symlink():
        raise InvariantRefusalError("evidence output already exists")
    temporary = path.with_name(".{}.{}.tmp".format(path.name, secrets.token_hex(8)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        # Hard-link publication is atomic and fails if the final name already
        # exists.  The temporary and final names are in the same private dir.
        try:
            os.link(str(temporary), str(path), follow_symlinks=False)
        except FileExistsError as error:
            raise InvariantRefusalError("evidence output already exists") from error
    finally:
        temporary.unlink(missing_ok=True)
    # Persist both the final hard-link and the removal of the temporary name.
    # Syncing before unlink would leave the directory-entry cleanup vulnerable
    # to a crash even though the manifest itself had been published.
    _fsync_directory(path.parent)
    st = path.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_nlink != 1
        or stat.S_IMODE(st.st_mode) != 0o600
        or (hasattr(os, "geteuid") and st.st_uid != os.geteuid())
    ):
        raise InvariantRefusalError("unsafe evidence output file")


def _read_existing_canonical(path: Path, expected: bytes) -> None:
    """Accept only the exact already-published private canonical manifest."""

    _check_private_parent(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise UsageStateError("evidence output is absent")
    if (
        stat.S_ISLNK(st.st_mode)
        or not _is_private_owned_regular(st)
    ):
        raise InvariantRefusalError("unsafe evidence output file")
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)
            or not _is_private_owned_regular(opened)
        ):
            raise InvariantRefusalError("evidence output identity changed")
        observed = _read_bounded_descriptor(descriptor)
        if observed != expected:
            raise InvariantRefusalError(
                "existing evidence output does not match seal intent"
            )
        if opened.st_nlink != 1:
            _recover_generated_temporary_links(
                path, expected, descriptor, opened
            )
    finally:
        os.close(descriptor)
    final = path.lstat()
    if (
        (final.st_dev, final.st_ino) != (st.st_dev, st.st_ino)
        or final.st_nlink != 1
        or not _is_private_owned_regular(final)
    ):
        raise InvariantRefusalError("evidence output identity changed")
    manifest = strict_evidence_loads(observed)
    if canonical_evidence_bytes(manifest) != observed:
        raise InvariantRefusalError("existing evidence output is not canonical")


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
    except InvariantRefusalError:
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
