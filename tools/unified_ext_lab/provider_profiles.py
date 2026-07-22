"""Immutable Stage-6C candidate identities for accountless validation.

The command layer accepts only a provider id.  Package coordinates, binaries,
probe forms, acquisition locations, and all safety holds live in this module;
none can be supplied by a caller or loaded from mutable configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

from .errors import InvariantRefusalError, UnsupportedError, UsageStateError
from .model import validate_provider_id


_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
_BINARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,127}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NPM_INTEGRITY_RE = re.compile(r"^sha512-[A-Za-z0-9+/]+={0,2}$")
_RUNTIME_REFERENCE_RE = re.compile(
    r"^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$"
)
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUPPLY_FILENAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,95}\.supply\.v1\.json$"
)
_MAX_ARGUMENT_LENGTH = 256
_MAX_SUPPLY_MANIFEST_BYTES = 64 * 1024
_MAX_SUPPLY_ENTRIES = 256
_SUPPLY_SCHEMA = "unified-ext-provider-supply-lock/v1"
_SYNTHETIC_PROVIDER_ID = "synthetic-fixture"
_SUPPLY_LOCK_ROOT = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "locks", "provider-supply"
)
_VALIDATED_SUPPLY_TOKEN = object()


class AcquisitionKind(str, Enum):
    """The only acquisition mechanisms Stage 6C can ever route."""

    NPM_REGISTRY_INTEGRITY = "npm_registry_integrity"
    PINNED_ARTIFACT = "pinned_artifact"


@dataclass(frozen=True)
class SupplyManifestSource:
    """Fixed source metadata for one checked-in canonical supply manifest."""

    filename: str
    sha256: str
    entry_count: int
    fixture_only: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.filename) is not str
            or _SUPPLY_FILENAME_RE.fullmatch(self.filename) is None
            or os.path.basename(self.filename) != self.filename
            or type(self.sha256) is not str
            or _SHA256_RE.fullmatch(self.sha256) is None
            or type(self.entry_count) is not int
            or not 1 <= self.entry_count <= _MAX_SUPPLY_ENTRIES
            or type(self.fixture_only) is not bool
        ):
            raise UsageStateError("invalid provider supply-manifest source")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "entry_count": self.entry_count,
                "filename": self.filename,
                "fixture_only": self.fixture_only,
                "sha256": self.sha256,
            }
        )


def _valid_integrity(value: object) -> bool:
    if type(value) is not str or _NPM_INTEGRITY_RE.fullmatch(value) is None:
        return False
    encoded = value[len("sha512-") :]
    try:
        decoded = b64decode(encoded.encode("ascii"), validate=True)
    except (Base64Error, ValueError):
        return False
    return len(decoded) == 64 and b64encode(decoded).decode("ascii") == encoded


def _valid_locator(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > 512
        or value.startswith("/")
        or "\\" in value
        or "://" in value
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        return False
    return all(component not in ("", ".", "..") for component in value.split("/"))


@dataclass(frozen=True)
class SupplyManifestEntry:
    """One exact root or dependency artifact parsed from canonical bytes."""

    package_name: Optional[str]
    version: str
    locator: str
    integrity: Optional[str]
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            self.package_name is not None
            and (
                type(self.package_name) is not str
                or _PACKAGE_RE.fullmatch(self.package_name) is None
            )
        ):
            raise UsageStateError("invalid supply-manifest package")
        if (
            type(self.version) is not str
            or _VERSION_RE.fullmatch(self.version) is None
            or not _valid_locator(self.locator)
            or (self.integrity is not None and not _valid_integrity(self.integrity))
            or type(self.sha256) is not str
            or _SHA256_RE.fullmatch(self.sha256) is None
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= (2**63 - 1)
        ):
            raise UsageStateError("invalid supply-manifest artifact entry")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "integrity": self.integrity,
                "locator": self.locator,
                "package_name": self.package_name,
                "sha256": self.sha256,
                "size_bytes": self.size_bytes,
                "version": self.version,
            }
        )


@dataclass(frozen=True)
class RuntimePlatformLock:
    """Runtime/platform identity derived only from a validated manifest."""

    base_reference: str
    base_image_id: str
    operating_system: str
    architecture: str
    node_version: str
    node_executable_sha256: str
    supply_manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.base_reference) is not str
            or _RUNTIME_REFERENCE_RE.fullmatch(self.base_reference) is None
            or type(self.base_image_id) is not str
            or _IMAGE_ID_RE.fullmatch(self.base_image_id) is None
            or self.operating_system != "linux"
            or self.architecture not in ("amd64", "arm64")
            or type(self.node_version) is not str
            or _VERSION_RE.fullmatch(self.node_version) is None
            or type(self.node_executable_sha256) is not str
            or _SHA256_RE.fullmatch(self.node_executable_sha256) is None
            or type(self.supply_manifest_sha256) is not str
            or _SHA256_RE.fullmatch(self.supply_manifest_sha256) is None
        ):
            raise UsageStateError("invalid provider runtime/platform lock")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "architecture": self.architecture,
                "base_image_id": self.base_image_id,
                "base_reference": self.base_reference,
                "node_executable_sha256": self.node_executable_sha256,
                "node_version": self.node_version,
                "operating_system": self.operating_system,
                "supply_manifest_sha256": self.supply_manifest_sha256,
            }
        )


@dataclass(frozen=True)
class AcquisitionClosureLock:
    """Exact immutable closure derived from every validated manifest entry."""

    acquisition_kind: AcquisitionKind
    operating_system: str
    architecture: str
    supply_manifest_sha256: str
    locked_entry_count: int
    root: SupplyManifestEntry
    dependencies: Tuple[SupplyManifestEntry, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.acquisition_kind, AcquisitionKind)
            or self.operating_system != "linux"
            or self.architecture not in ("amd64", "arm64")
            or type(self.supply_manifest_sha256) is not str
            or _SHA256_RE.fullmatch(self.supply_manifest_sha256) is None
            or type(self.locked_entry_count) is not int
            or not 1 <= self.locked_entry_count <= _MAX_SUPPLY_ENTRIES
            or type(self.root) is not SupplyManifestEntry
            or type(self.dependencies) is not tuple
            or any(type(entry) is not SupplyManifestEntry for entry in self.dependencies)
            or self.locked_entry_count != 1 + len(self.dependencies)
        ):
            raise UsageStateError("invalid provider acquisition closure lock")
        entries = (self.root,) + self.dependencies
        if self.acquisition_kind is AcquisitionKind.NPM_REGISTRY_INTEGRITY:
            if (
                not self.dependencies
                or any(
                    entry.package_name is None or not _valid_integrity(entry.integrity)
                    for entry in entries
                )
            ):
                raise UsageStateError("invalid npm acquisition closure lock")
        elif (
            self.root.package_name is not None
            or self.root.integrity is not None
            or self.dependencies
            or self.locked_entry_count != 1
        ):
            raise UsageStateError("invalid artifact acquisition closure lock")
        identities = set()
        locators = set()
        for entry in entries:
            identity = (entry.package_name, entry.version)
            if identity in identities or entry.locator in locators:
                raise UsageStateError("duplicate provider acquisition entry")
            identities.add(identity)
            locators.add(entry.locator)

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "acquisition_kind": self.acquisition_kind.value,
                "architecture": self.architecture,
                "dependencies": tuple(
                    dict(entry.to_dict()) for entry in self.dependencies
                ),
                "locked_entry_count": self.locked_entry_count,
                "operating_system": self.operating_system,
                "root": dict(self.root.to_dict()),
                "supply_manifest_sha256": self.supply_manifest_sha256,
            }
        )


@dataclass(frozen=True)
class ValidatedSupplyManifest:
    """Parser-issued immutable proof that canonical manifest bytes were checked."""

    source: SupplyManifestSource
    runtime_lock: RuntimePlatformLock
    acquisition_lock: AcquisitionClosureLock
    fixture_only: bool
    promotion_eligible: bool
    _validation_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._validation_token is not _VALIDATED_SUPPLY_TOKEN
            or type(self.source) is not SupplyManifestSource
            or type(self.runtime_lock) is not RuntimePlatformLock
            or type(self.acquisition_lock) is not AcquisitionClosureLock
            or type(self.fixture_only) is not bool
            or self.promotion_eligible is not False
            or self.runtime_lock.supply_manifest_sha256 != self.source.sha256
            or self.acquisition_lock.supply_manifest_sha256 != self.source.sha256
            or self.acquisition_lock.locked_entry_count != self.source.entry_count
            or self.runtime_lock.operating_system
            != self.acquisition_lock.operating_system
            or self.runtime_lock.architecture != self.acquisition_lock.architecture
        ):
            raise UsageStateError("invalid validated provider supply manifest")


def _safe_command(value: object, binary: str, field: str) -> Tuple[str, ...]:
    if (
        type(value) is not tuple
        or not value
        or value[0] != binary
        or any(
            type(item) is not str
            or not item
            or len(item) > _MAX_ARGUMENT_LENGTH
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in item)
            for item in value
        )
    ):
        raise UsageStateError("invalid provider {} command".format(field))
    return value


@dataclass(frozen=True)
class AccountlessProviderProfile:
    """One exact, source-controlled candidate accountless identity."""

    provider_id: str
    vendor: str
    package_name: Optional[str]
    version: str
    binary: str
    version_argv: Tuple[str, ...]
    help_argv: Tuple[str, ...]
    status_argv: Optional[Tuple[str, ...]]
    acquisition_kind: AcquisitionKind
    acquisition_locator: str
    supply_manifest_source: Optional[SupplyManifestSource] = None
    hold_reason: str = "acquisition lock unavailable"
    accountless_only: bool = True
    promotion_eligible: bool = False
    fixture_only: bool = False
    _validated_supply: Optional[ValidatedSupplyManifest] = field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        provider = validate_provider_id(self.provider_id)
        if provider not in ("grok", "kimi", "copilot", "cursor") and not (
            provider == _SYNTHETIC_PROVIDER_ID and self.fixture_only is True
        ):
            raise UsageStateError("unsupported accountless provider profile")
        if (
            type(self.vendor) is not str
            or not self.vendor
            or len(self.vendor) > 64
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in self.vendor)
        ):
            raise UsageStateError("invalid provider vendor")
        if type(self.version) is not str or _VERSION_RE.fullmatch(self.version) is None:
            raise UsageStateError("invalid provider version")
        if type(self.binary) is not str or _BINARY_RE.fullmatch(self.binary) is None:
            raise UsageStateError("invalid provider binary")
        _safe_command(self.version_argv, self.binary, "version")
        _safe_command(self.help_argv, self.binary, "help")
        if self.status_argv is not None:
            _safe_command(self.status_argv, self.binary, "status")
        if not isinstance(self.acquisition_kind, AcquisitionKind):
            raise UsageStateError("invalid provider acquisition kind")
        if (
            type(self.acquisition_locator) is not str
            or not self.acquisition_locator
            or len(self.acquisition_locator) > 512
            or any(
                ord(character) < 0x20 or ord(character) > 0x7E
                for character in self.acquisition_locator
            )
        ):
            raise UsageStateError("invalid provider acquisition locator")
        if self.supply_manifest_source is not None and (
            type(self.supply_manifest_source) is not SupplyManifestSource
        ):
            raise UsageStateError("invalid provider supply-manifest source")
        if (
            type(self.hold_reason) is not str
            or not self.hold_reason
            or len(self.hold_reason) > 256
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in self.hold_reason)
        ):
            raise UsageStateError("invalid provider hold reason")
        if (
            self.accountless_only is not True
            or self.promotion_eligible is not False
            or type(self.fixture_only) is not bool
            or (
                self.supply_manifest_source is not None
                and self.supply_manifest_source.fixture_only != self.fixture_only
            )
            or (self.fixture_only and provider != _SYNTHETIC_PROVIDER_ID)
            or (
                self.fixture_only
                and self.vendor != "Synthetic local fixture"
            )
        ):
            raise InvariantRefusalError("provider profile must remain accountless and non-promotional")

        if self.acquisition_kind is AcquisitionKind.NPM_REGISTRY_INTEGRITY:
            if (
                type(self.package_name) is not str
                or _PACKAGE_RE.fullmatch(self.package_name) is None
                or not self.acquisition_locator.startswith("npm/")
            ):
                raise UsageStateError("invalid npm provider acquisition")
        else:
            if (
                self.package_name is not None
                or not self.acquisition_locator.startswith("cursor-build/")
            ):
                raise UsageStateError("invalid artifact provider acquisition")

        if self.supply_manifest_source is not None:
            object.__setattr__(
                self,
                "_validated_supply",
                _load_supply_manifest(self, self.supply_manifest_source),
            )

    @property
    def install_ready(self) -> bool:
        return self._validated_supply is not None

    @property
    def runtime_lock(self) -> Optional[RuntimePlatformLock]:
        if self._validated_supply is None:
            return None
        return self._validated_supply.runtime_lock

    @property
    def acquisition_lock(self) -> Optional[AcquisitionClosureLock]:
        if self._validated_supply is None:
            return None
        return self._validated_supply.acquisition_lock

    @property
    def package_spec(self) -> str:
        if self.acquisition_kind is not AcquisitionKind.NPM_REGISTRY_INTEGRITY:
            raise UnsupportedError("provider does not use npm acquisition")
        assert isinstance(self.package_name, str)
        return "{}@{}".format(self.package_name, self.version)

    def require_install_ready(self) -> ValidatedSupplyManifest:
        if self._validated_supply is None:
            raise UnsupportedError("provider acquisition is held")
        return self._validated_supply

    def canonical_identity(self) -> Mapping[str, object]:
        """Return the non-secret profile identity used only for hashing."""

        return MappingProxyType(
            {
                "accountless_only": True,
                "acquisition_kind": self.acquisition_kind.value,
                "acquisition_locator": self.acquisition_locator,
                "binary": self.binary,
                "fixture_only": self.fixture_only,
                "help_argv": self.help_argv,
                "package_name": self.package_name,
                "promotion_eligible": False,
                "provider_id": self.provider_id,
                "status_argv": self.status_argv,
                "supply_manifest_source": (
                    None
                    if self.supply_manifest_source is None
                    else dict(self.supply_manifest_source.to_dict())
                ),
                "vendor": self.vendor,
                "version": self.version,
                "version_argv": self.version_argv,
            }
        )

    @property
    def profile_sha256(self) -> str:
        payload = json.dumps(
            dict(self.canonical_identity()),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


def _stat_identity(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_nlink,
        getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)),
    )


def _read_supply_manifest(source: SupplyManifestSource) -> bytes:
    root = _SUPPLY_LOCK_ROOT
    if (
        type(root) is not str
        or not os.path.isabs(root)
        or os.path.normpath(root) != root
        or os.path.realpath(root) != root
    ):
        raise InvariantRefusalError("provider supply lock root is unsafe")
    try:
        root_info = os.lstat(root)
    except OSError as error:
        raise InvariantRefusalError("provider supply lock root is unavailable") from error
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (hasattr(os, "geteuid") and root_info.st_uid != os.geteuid())
    ):
        raise InvariantRefusalError("provider supply lock root is unsafe")
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise InvariantRefusalError("provider supply lock root is unavailable") from error
    try:
        opened_root = os.fstat(root_descriptor)
        if _stat_identity(opened_root) != _stat_identity(root_info):
            raise InvariantRefusalError("provider supply lock root changed")
        try:
            before = os.stat(
                source.filename,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise InvariantRefusalError(
                "provider supply manifest is unavailable"
            ) from error
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o644
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_SUPPLY_MANIFEST_BYTES
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
        ):
            raise InvariantRefusalError("provider supply manifest is unsafe")
        try:
            descriptor = os.open(
                source.filename,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_descriptor,
            )
        except OSError as error:
            raise InvariantRefusalError(
                "provider supply manifest is unavailable"
            ) from error
        try:
            opened = os.fstat(descriptor)
            if _stat_identity(opened) != _stat_identity(before):
                raise InvariantRefusalError("provider supply manifest changed")
            chunks = []
            remaining = _MAX_SUPPLY_MANIFEST_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 8192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            named = os.stat(
                source.filename,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                len(payload) > _MAX_SUPPLY_MANIFEST_BYTES
                or len(payload) != opened.st_size
                or _stat_identity(after) != _stat_identity(opened)
                or _stat_identity(named) != _stat_identity(opened)
                or _stat_identity(os.fstat(root_descriptor))
                != _stat_identity(opened_root)
            ):
                raise InvariantRefusalError("provider supply manifest changed")
        except InvariantRefusalError:
            raise
        except OSError as error:
            raise InvariantRefusalError("provider supply manifest changed") from error
        finally:
            os.close(descriptor)
    finally:
        os.close(root_descriptor)
    if hashlib.sha256(payload).hexdigest() != source.sha256:
        raise InvariantRefusalError("provider supply manifest checksum drift")
    return payload


def _reject_duplicate_keys(pairs):
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate supply-manifest key")
        result[key] = value
    return result


def _require_keys(value: object, expected: Tuple[str, ...], field_name: str) -> dict:
    if type(value) is not dict or set(value) != set(expected):
        raise InvariantRefusalError("invalid provider supply-manifest " + field_name)
    return value


def _manifest_entry(value: object, field_name: str) -> SupplyManifestEntry:
    record = _require_keys(
        value,
        ("integrity", "locator", "package_name", "sha256", "size_bytes", "version"),
        field_name,
    )
    try:
        return SupplyManifestEntry(
            package_name=record["package_name"],
            version=record["version"],
            locator=record["locator"],
            integrity=record["integrity"],
            sha256=record["sha256"],
            size_bytes=record["size_bytes"],
        )
    except UsageStateError as error:
        raise InvariantRefusalError(
            "invalid provider supply-manifest " + field_name
        ) from error


def _load_supply_manifest(
    profile: AccountlessProviderProfile,
    source: SupplyManifestSource,
) -> ValidatedSupplyManifest:
    """Load one fixed checked-in manifest and derive readiness from its entries."""

    payload = _read_supply_manifest(source)
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise InvariantRefusalError("provider supply manifest is not canonical")
    try:
        text = payload[:-1].decode("ascii")
        manifest = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("invalid JSON constant")
            ),
        )
        canonical = (
            json.dumps(
                manifest,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise InvariantRefusalError("provider supply manifest is invalid") from error
    if canonical != payload:
        raise InvariantRefusalError("provider supply manifest is not canonical")

    top = _require_keys(
        manifest,
        (
            "dependencies",
            "entry_count",
            "fixture_only",
            "platform",
            "promotion_eligible",
            "provider",
            "root",
            "schema",
        ),
        "root",
    )
    if (
        top["schema"] != _SUPPLY_SCHEMA
        or type(top["fixture_only"]) is not bool
        or top["fixture_only"] != source.fixture_only
        or top["fixture_only"] != profile.fixture_only
        or top["promotion_eligible"] is not False
        or type(top["entry_count"]) is not int
        or not 1 <= top["entry_count"] <= _MAX_SUPPLY_ENTRIES
        or top["entry_count"] != source.entry_count
        or type(top["dependencies"]) is not list
    ):
        raise InvariantRefusalError("provider supply-manifest metadata mismatch")

    provider = _require_keys(
        top["provider"],
        (
            "acquisition_kind",
            "acquisition_locator",
            "package_name",
            "provider_id",
            "vendor",
            "version",
        ),
        "provider",
    )
    if provider != {
        "acquisition_kind": profile.acquisition_kind.value,
        "acquisition_locator": profile.acquisition_locator,
        "package_name": profile.package_name,
        "provider_id": profile.provider_id,
        "vendor": profile.vendor,
        "version": profile.version,
    }:
        raise InvariantRefusalError("provider supply-manifest subject mismatch")

    platform = _require_keys(
        top["platform"],
        (
            "architecture",
            "base_image_id",
            "base_reference",
            "node_executable_sha256",
            "node_version",
            "operating_system",
        ),
        "platform",
    )
    try:
        runtime_lock = RuntimePlatformLock(
            base_reference=platform["base_reference"],
            base_image_id=platform["base_image_id"],
            operating_system=platform["operating_system"],
            architecture=platform["architecture"],
            node_version=platform["node_version"],
            node_executable_sha256=platform["node_executable_sha256"],
            supply_manifest_sha256=source.sha256,
        )
    except UsageStateError as error:
        raise InvariantRefusalError(
            "invalid provider supply-manifest platform"
        ) from error

    root = _manifest_entry(top["root"], "root artifact")
    dependencies = tuple(
        _manifest_entry(value, "dependency artifact")
        for value in top["dependencies"]
    )
    if (
        top["entry_count"] != 1 + len(dependencies)
        or root.package_name != profile.package_name
        or root.version != profile.version
        or root.locator != profile.acquisition_locator
        or tuple(
            (entry.package_name or "", entry.version, entry.locator)
            for entry in dependencies
        )
        != tuple(
            sorted(
                (entry.package_name or "", entry.version, entry.locator)
                for entry in dependencies
            )
        )
    ):
        raise InvariantRefusalError("provider supply-manifest closure mismatch")
    try:
        acquisition_lock = AcquisitionClosureLock(
            acquisition_kind=profile.acquisition_kind,
            operating_system=runtime_lock.operating_system,
            architecture=runtime_lock.architecture,
            supply_manifest_sha256=source.sha256,
            locked_entry_count=top["entry_count"],
            root=root,
            dependencies=dependencies,
        )
    except UsageStateError as error:
        raise InvariantRefusalError(
            "invalid provider supply-manifest closure"
        ) from error
    return ValidatedSupplyManifest(
        source=source,
        runtime_lock=runtime_lock,
        acquisition_lock=acquisition_lock,
        fixture_only=top["fixture_only"],
        promotion_eligible=False,
        _validation_token=_VALIDATED_SUPPLY_TOKEN,
    )


# These are timestamped, unverified candidate identities from the Stage-6C
# research cutoff (2026-07-22). Acquisition remains held until complete
# canonical source-controlled supply manifests are added and validated.
_PROFILES = {
    "grok": AccountlessProviderProfile(
        provider_id="grok",
        vendor="xAI",
        package_name="@xai-official/grok",
        version="0.2.106",
        binary="grok",
        version_argv=("grok", "--version"),
        help_argv=("grok", "--help"),
        status_argv=None,
        acquisition_kind=AcquisitionKind.NPM_REGISTRY_INTEGRITY,
        acquisition_locator="npm/xai-official/grok/0.2.106",
        hold_reason="pinned npm SRI closure and Node runtime digest are unavailable",
    ),
    "kimi": AccountlessProviderProfile(
        provider_id="kimi",
        vendor="Moonshot AI",
        package_name="@moonshot-ai/kimi-code",
        version="0.28.1",
        binary="kimi",
        version_argv=("kimi", "--version"),
        help_argv=("kimi", "--help"),
        status_argv=None,
        acquisition_kind=AcquisitionKind.NPM_REGISTRY_INTEGRITY,
        acquisition_locator="npm/moonshot-ai/kimi-code/0.28.1",
        hold_reason="pinned npm SRI closure and Node runtime digest are unavailable",
    ),
    "copilot": AccountlessProviderProfile(
        provider_id="copilot",
        vendor="GitHub",
        package_name="@github/copilot",
        version="1.0.73",
        binary="copilot",
        version_argv=("copilot", "--version"),
        help_argv=("copilot", "help"),
        status_argv=None,
        acquisition_kind=AcquisitionKind.NPM_REGISTRY_INTEGRITY,
        acquisition_locator="npm/github/copilot/1.0.73",
        hold_reason="pinned npm SRI closure and Node runtime digest are unavailable",
    ),
    "cursor": AccountlessProviderProfile(
        provider_id="cursor",
        vendor="Cursor",
        package_name=None,
        version="2026.07.20-8cc9c0b",
        binary="agent",
        version_argv=("agent", "--version"),
        help_argv=("agent", "--help"),
        status_argv=("agent", "status"),
        acquisition_kind=AcquisitionKind.PINNED_ARTIFACT,
        acquisition_locator="cursor-build/2026.07.20-8cc9c0b/linux/x64/agent-cli-package.tar.gz",
        hold_reason="the candidate Cursor artifact has no evidenced checksum",
    ),
}

PROVIDER_PROFILES: Mapping[str, AccountlessProviderProfile] = MappingProxyType(
    dict(_PROFILES)
)
PROVIDER_IDS = tuple(sorted(PROVIDER_PROFILES))


def get_provider_profile(provider_id: object) -> AccountlessProviderProfile:
    provider = validate_provider_id(provider_id)
    try:
        return PROVIDER_PROFILES[provider]
    except KeyError as error:
        raise UnsupportedError("provider has no accountless validation profile") from error


__all__ = [
    "AcquisitionClosureLock",
    "AcquisitionKind",
    "AccountlessProviderProfile",
    "PROVIDER_IDS",
    "PROVIDER_PROFILES",
    "RuntimePlatformLock",
    "SupplyManifestEntry",
    "SupplyManifestSource",
    "ValidatedSupplyManifest",
    "get_provider_profile",
]
