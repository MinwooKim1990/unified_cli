"""Durable, private state for the offline extension-lab harness.

The store deliberately exposes a small compare-and-transition API.  A caller
must hold ``LabStateStore(root).locked(lab_id)`` while creating, reading, or
changing state.  Entering a pending phase commits it before control returns to
the caller, so the associated side effect can only run after the intent is
durable.  Reopening a store left in any pending phase converts it to
``RECOVERY_REQUIRED``; forward execution is never resumed after a crash.

This module only reads and writes local JSON.  It does not run commands, inspect
the host, access credentials, or contact Docker or a provider.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterator, Mapping, Optional, Sequence, Tuple, Union

from .errors import InvariantRefusalError, UnsupportedError, UsageStateError
from .model import (
    LABEL_PREFIX,
    LabResource,
    ResourceRole,
    validate_lab_id,
    validate_ownership_token,
    validate_provider_id,
)

try:  # pragma: no branch - availability is deliberately checked at runtime.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by the explicit helper test.
    _fcntl = None


STATE_SCHEMA = 3
LEGACY_FIXTURE_STATE_SCHEMA = 2
FIXTURE_EXECUTION_PROFILE = "fixture-fake-v1"
REAL_DOCKER_EXECUTION_PROFILE = "real-docker-v1"
EXECUTION_PROFILES = frozenset(
    (FIXTURE_EXECUTION_PROFILE, REAL_DOCKER_EXECUTION_PROFILE)
)
MAX_STATE_BYTES = 1024 * 1024
STATE_FILE_NAME = "state.json"
STATE_REPLACE_BACKUP_NAME = ".state.json.replace-backup"
STATE_REPLACE_INTENT_NAME = ".state.json.replace-intent"
STATE_REPLACE_BACKUP_REMOVING_NAME = ".state.json.replace-backup-removing"
STATE_REPLACE_INTENT_REMOVING_NAME = ".state.json.replace-intent-removing"
LOCK_FILE_NAME = "state.lock"
ROOT_LOCK_FILE_PREFIX = ".lab-name-lock."
_LOCK_CAPABILITY = object()

_FileIdentity = Tuple[int, int, int, int, int, int, int, int]


class StatePhase(str, Enum):
    NEW = "NEW"
    CREATE_PENDING = "CREATE_PENDING"
    CREATED = "CREATED"
    INSTALL_PENDING = "INSTALL_PENDING"
    INSTALLED = "INSTALLED"
    TEST_PENDING = "TEST_PENDING"
    TESTED = "TESTED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    EVIDENCE_CAPTURED = "EVIDENCE_CAPTURED"
    LOGOUT_PENDING = "LOGOUT_PENDING"
    LOGOUT_DONE = "LOGOUT_DONE"
    LOGOUT_FAILED = "LOGOUT_FAILED"
    DESTROY_PENDING = "DESTROY_PENDING"
    DESTROY_DONE = "DESTROY_DONE"
    DESTROY_FAILED = "DESTROY_FAILED"
    VERIFY_CLEAN_PENDING = "VERIFY_CLEAN_PENDING"
    CLEAN_VERIFIED = "CLEAN_VERIFIED"
    DIRTY = "DIRTY"
    SEAL_PENDING = "SEAL_PENDING"
    PASSED = "PASSED"
    FAILED_CLEAN = "FAILED_CLEAN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


PENDING_STEPS = MappingProxyType(
    {
        StatePhase.CREATE_PENDING: "create",
        StatePhase.INSTALL_PENDING: "install",
        StatePhase.TEST_PENDING: "test",
        StatePhase.EVIDENCE_PENDING: "evidence",
        StatePhase.LOGOUT_PENDING: "logout",
        StatePhase.DESTROY_PENDING: "destroy",
        StatePhase.VERIFY_CLEAN_PENDING: "verify_clean",
        StatePhase.SEAL_PENDING: "seal",
    }
)

STABLE_FORWARD_STEPS = MappingProxyType(
    {
        StatePhase.NEW: "create",
        StatePhase.CREATED: "install",
        StatePhase.INSTALLED: "test",
        StatePhase.TESTED: "evidence",
    }
)

TRANSITIONS = MappingProxyType(
    {
        StatePhase.NEW: frozenset((StatePhase.CREATE_PENDING,)),
        StatePhase.CREATE_PENDING: frozenset((StatePhase.CREATED,)),
        StatePhase.CREATED: frozenset((StatePhase.INSTALL_PENDING,)),
        StatePhase.INSTALL_PENDING: frozenset((StatePhase.INSTALLED,)),
        StatePhase.INSTALLED: frozenset((StatePhase.TEST_PENDING,)),
        StatePhase.TEST_PENDING: frozenset((StatePhase.TESTED,)),
        StatePhase.TESTED: frozenset((StatePhase.EVIDENCE_PENDING,)),
        StatePhase.EVIDENCE_PENDING: frozenset((StatePhase.EVIDENCE_CAPTURED,)),
        StatePhase.EVIDENCE_CAPTURED: frozenset((StatePhase.LOGOUT_PENDING,)),
        StatePhase.LOGOUT_PENDING: frozenset(
            (StatePhase.LOGOUT_DONE, StatePhase.LOGOUT_FAILED)
        ),
        StatePhase.LOGOUT_DONE: frozenset((StatePhase.DESTROY_PENDING,)),
        StatePhase.LOGOUT_FAILED: frozenset((StatePhase.DESTROY_PENDING,)),
        StatePhase.DESTROY_PENDING: frozenset(
            (StatePhase.DESTROY_DONE, StatePhase.DESTROY_FAILED)
        ),
        StatePhase.DESTROY_DONE: frozenset((StatePhase.VERIFY_CLEAN_PENDING,)),
        StatePhase.DESTROY_FAILED: frozenset((StatePhase.VERIFY_CLEAN_PENDING,)),
        StatePhase.VERIFY_CLEAN_PENDING: frozenset(
            (StatePhase.CLEAN_VERIFIED, StatePhase.DIRTY)
        ),
        StatePhase.CLEAN_VERIFIED: frozenset((StatePhase.SEAL_PENDING,)),
        # Dirty is cleanup-only but retryable: transient cleanup/inspection
        # failures must not strand resources forever.
        StatePhase.DIRTY: frozenset(
            (StatePhase.DESTROY_PENDING, StatePhase.VERIFY_CLEAN_PENDING)
        ),
        StatePhase.SEAL_PENDING: frozenset(
            (StatePhase.PASSED, StatePhase.FAILED_CLEAN)
        ),
        StatePhase.PASSED: frozenset(),
        StatePhase.FAILED_CLEAN: frozenset(),
        # Recovery is cleanup-only.  A caller may start at the earliest cleanup
        # operation it can safely perform, but can never resume create/install/
        # test/evidence work.
        StatePhase.RECOVERY_REQUIRED: frozenset(
            (
                StatePhase.LOGOUT_PENDING,
                StatePhase.DESTROY_PENDING,
                StatePhase.VERIFY_CLEAN_PENDING,
            )
        ),
    }
)

_DRAFT_FORBIDDEN_PHASES = frozenset(
    (
        StatePhase.NEW,
        StatePhase.CREATE_PENDING,
        StatePhase.CREATED,
        StatePhase.INSTALL_PENDING,
        StatePhase.INSTALLED,
        StatePhase.TEST_PENDING,
        StatePhase.TESTED,
        StatePhase.EVIDENCE_PENDING,
    )
)
_DRAFT_REQUIRED_PHASES = frozenset(
    (
        StatePhase.EVIDENCE_CAPTURED,
        StatePhase.SEAL_PENDING,
        StatePhase.PASSED,
        StatePhase.FAILED_CLEAN,
    )
)


OBSERVATION_STEPS = frozenset(
    ("create", "install", "test", "evidence", "logout", "destroy", "verify_clean", "seal")
)
OBSERVATION_OUTCOMES = frozenset(("succeeded", "failed", "skipped"))
ERROR_CODES = frozenset(
    (
        "none",
        "usage_state",
        "unsupported",
        "invariant_refusal",
        "runner_failure",
        "test_failure",
        "cleanup_incomplete",
        "timeout",
        "interrupted",
    )
)

_SEAL_INTENT_PHASES = frozenset(
    (StatePhase.SEAL_PENDING, StatePhase.PASSED, StatePhase.FAILED_CLEAN)
)

_FORBIDDEN_KEYS = frozenset(
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
        "credential",
        "credential_contents",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "source_text",
        "receipt_path",
        "uid",
        "inode",
        "url",
    )
)


def _phase(value: Union[StatePhase, str]) -> StatePhase:
    if isinstance(value, StatePhase):
        return value
    if type(value) is str:
        try:
            return StatePhase(value)
        except ValueError:
            pass
    raise UsageStateError("invalid state phase")


def _strict_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise UsageStateError("invalid {}".format(field))
    return value


def _safe_ascii(value: object, field: str, maximum: int = 128) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise UsageStateError("invalid {}".format(field))
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise UsageStateError("invalid {}".format(field))
    return value


def _check_forbidden_keys(value: object) -> None:
    """Reject persistence-shaped data that could contain host or secret data."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str:
                raise UsageStateError("JSON object keys must be strings")
            if key.lower() in _FORBIDDEN_KEYS:
                raise InvariantRefusalError("forbidden persisted field")
            _check_forbidden_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _check_forbidden_keys(nested)
    elif type(value) is float:
        raise UsageStateError("floating point values are not permitted")


@dataclass(frozen=True)
class OperationObservation:
    """The complete allowlist for a persisted operation observation."""

    step: str
    outcome: str
    exit_code: int
    latency_ns: int
    error_code: str = "none"

    def __post_init__(self) -> None:
        if self.step not in OBSERVATION_STEPS:
            raise UsageStateError("invalid operation step")
        if self.outcome not in OBSERVATION_OUTCOMES:
            raise UsageStateError("invalid operation outcome")
        if type(self.exit_code) is not int or self.exit_code < 0 or self.exit_code > 255:
            raise UsageStateError("invalid operation exit code")
        _strict_nonnegative_int(self.latency_ns, "operation latency")
        if self.error_code not in ERROR_CODES:
            raise UsageStateError("invalid operation error code")
        if self.outcome == "succeeded" and (self.exit_code != 0 or self.error_code != "none"):
            raise UsageStateError("inconsistent successful operation")
        if self.outcome == "failed" and self.error_code == "none":
            raise UsageStateError("failed operation requires an error code")
        if self.outcome == "skipped" and (self.exit_code != 0 or self.error_code != "none"):
            raise UsageStateError("inconsistent skipped operation")

    def to_dict(self) -> Dict[str, object]:
        return {
            "error_code": self.error_code,
            "exit_code": self.exit_code,
            "latency_ns": self.latency_ns,
            "outcome": self.outcome,
            "step": self.step,
        }

    @classmethod
    def from_dict(cls, value: object) -> "OperationObservation":
        data = _exact_mapping(
            value,
            frozenset(("step", "outcome", "exit_code", "latency_ns", "error_code")),
            "operation observation",
        )
        return cls(
            step=data["step"],
            outcome=data["outcome"],
            exit_code=data["exit_code"],
            latency_ns=data["latency_ns"],
            error_code=data["error_code"],
        )


@dataclass(frozen=True)
class SealIntent:
    """Non-sensitive identity and exact bytes expected for seal recovery."""

    output_identity: str
    payload_sha256: str
    result: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.output_identity, "seal output identity"),
            (self.payload_sha256, "seal payload sha256"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise UsageStateError("invalid {}".format(field))
        if self.result not in ("passed", "failed_clean"):
            raise UsageStateError("invalid seal result")

    def to_dict(self) -> Dict[str, object]:
        return {
            "output_identity": self.output_identity,
            "payload_sha256": self.payload_sha256,
            "result": self.result,
        }

    @classmethod
    def from_value(cls, value: object) -> "SealIntent":
        if type(value) is cls:
            return value
        data = _exact_mapping(
            value,
            frozenset(("output_identity", "payload_sha256", "result")),
            "seal intent",
        )
        return cls(
            output_identity=data["output_identity"],
            payload_sha256=data["payload_sha256"],
            result=data["result"],
        )


@dataclass(frozen=True)
class PlannedResource:
    """A resource plan containing only its deterministic name and labels."""

    role: str
    name: str
    labels: Mapping[str, str]

    def __post_init__(self) -> None:
        try:
            role = ResourceRole(self.role).value
        except (ValueError, TypeError):
            raise UsageStateError("invalid planned resource role")
        name = _safe_ascii(self.name, "planned resource name", 128)
        if "/" in name or "\\" in name or name in (".", ".."):
            raise UsageStateError("invalid planned resource name")
        if not isinstance(self.labels, Mapping):
            raise UsageStateError("invalid planned resource labels")
        expected_keys = frozenset(
            (
                LABEL_PREFIX + "/managed",
                LABEL_PREFIX + "/schema",
                LABEL_PREFIX + "/lab-id",
                LABEL_PREFIX + "/provider",
                LABEL_PREFIX + "/ownership-token",
                LABEL_PREFIX + "/role",
            )
        )
        if frozenset(self.labels.keys()) != expected_keys:
            raise UsageStateError("invalid planned resource labels")
        labels = {}
        for key, value in self.labels.items():
            labels[key] = _safe_ascii(value, "planned resource label", 128)
        if labels[LABEL_PREFIX + "/role"] != role:
            raise UsageStateError("planned resource role mismatch")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "labels", MappingProxyType(labels))

    def to_dict(self) -> Dict[str, object]:
        return {"labels": dict(self.labels), "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, value: object) -> "PlannedResource":
        data = _exact_mapping(value, frozenset(("role", "name", "labels")), "planned resource")
        return cls(role=data["role"], name=data["name"], labels=data["labels"])

    @classmethod
    def from_value(cls, value: object) -> "PlannedResource":
        if type(value) is cls:
            return value
        if type(value) is LabResource:
            return cls(role=value.role.value, name=value.name, labels=value.labels)
        return cls.from_dict(value)


def _exact_mapping(value: object, keys: frozenset, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value.keys()) != keys:
        raise UsageStateError("invalid {} fields".format(field))
    return value


def _validate_baselines(value: object) -> Mapping[str, bool]:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise UsageStateError("invalid baseline equality fields")
    result = {}
    for key, equal in value.items():
        safe_key = _safe_ascii(key, "baseline equality key", 32)
        if not all(character.islower() or character.isdigit() or character == "_" for character in safe_key):
            raise UsageStateError("invalid baseline equality key")
        if safe_key in _FORBIDDEN_KEYS:
            raise InvariantRefusalError("forbidden persisted field")
        if type(equal) is not bool:
            raise UsageStateError("baseline values must be booleans")
        result[safe_key] = equal
    return MappingProxyType(result)


_ARTIFACT_KEYS = frozenset(("package", "version", "source_kind", "source_locator", "sha256"))
_LEGACY_SYNTHETIC_ARTIFACT = MappingProxyType(
    {
        "package": "synthetic-cli-fixture",
        "version": "1.0.0",
        "source_kind": "local_fixture",
        "source_locator": "fixtures/synthetic-cli-fixture-1.0.0-50e7754c2a4c",
        "sha256": "50e7754c2a4cc5fb074d640eef253f5a9b61288dcbe8074887e2cc2c728edc66",
    }
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


def _validate_execution_profile(value: object) -> str:
    if type(value) is not str or value not in EXECUTION_PROFILES:
        raise UsageStateError("invalid execution profile")
    return value


def _executor_for_profile(execution_profile: str) -> str:
    return {
        FIXTURE_EXECUTION_PROFILE: "fake_docker",
        REAL_DOCKER_EXECUTION_PROFILE: "real_docker",
    }[execution_profile]


def _validate_artifact_evidence(value: object) -> Mapping[str, object]:
    data = _exact_mapping(value, _ARTIFACT_KEYS, "artifact evidence")
    from .evidence import ArtifactEvidence

    artifact = ArtifactEvidence.from_value(data)
    copied = artifact.to_dict()
    _check_forbidden_keys(copied)
    return MappingProxyType(copied)


def _validate_resource_ids(
    value: object, planned_resources: Tuple[PlannedResource, ...]
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise UsageStateError("invalid resource ids")
    planned_roles = frozenset(resource.role for resource in planned_resources)
    result = {}
    for key, resource_id in value.items():
        try:
            role = ResourceRole(key)
        except (TypeError, ValueError):
            raise UsageStateError("invalid resource id role")
        if role.value not in planned_roles:
            raise UsageStateError("resource id role was not planned")
        if type(resource_id) is not str:
            raise UsageStateError("invalid resource id")
        if role is ResourceRole.IMAGE:
            valid = (
                len(resource_id) == 71
                and resource_id.startswith("sha256:")
                and all(character in "0123456789abcdef" for character in resource_id[7:])
            )
        elif role is ResourceRole.CONTAINER:
            valid = len(resource_id) == 64 and all(
                character in "0123456789abcdef" for character in resource_id
            )
        else:
            valid = False
        if not valid:
            raise UsageStateError("invalid resource id")
        result[role.value] = resource_id
    return MappingProxyType(result)


def _validate_draft(value: object) -> Optional[Mapping[str, object]]:
    if value is None:
        return None
    data = _exact_mapping(value, _DRAFT_KEYS, "draft evidence")
    _exact_mapping(data["artifact"], _ARTIFACT_KEYS, "draft artifact")
    if data["evidence_kind"] != "harness_fixture":
        raise UsageStateError("invalid draft evidence kind")
    if data["executor_kind"] not in ("fake_docker", "real_docker"):
        raise UsageStateError("invalid draft executor kind")
    if data["promotion_eligible"] is not False:
        raise InvariantRefusalError("fixture evidence cannot be promotional")
    if type(data["operations"]) not in (list, tuple):
        raise UsageStateError("invalid draft operations")
    for operation in data["operations"]:
        OperationObservation.from_dict(operation)
    _strict_nonnegative_int(data["captured_at_ns"], "draft capture time")
    _check_forbidden_keys(data)
    # Import lazily to avoid coupling state importability to the evidence
    # module while still applying the complete artifact/hash/locator schema.
    from .evidence import validate_draft

    validate_draft(data)
    # Round-trip through JSON-compatible structures and then freeze the top
    # level.  Nested values are reconstructed on each ``to_dict`` call.
    copied = {
        "artifact": MappingProxyType(dict(data["artifact"])),
        "captured_at_ns": data["captured_at_ns"],
        "evidence_kind": data["evidence_kind"],
        "executor_kind": data["executor_kind"],
        "manifest_schema_sha256": data["manifest_schema_sha256"],
        "observed_protocol_schema_sha256": data["observed_protocol_schema_sha256"],
        "operations": tuple(
            MappingProxyType(dict(operation)) for operation in data["operations"]
        ),
        "promotion_eligible": data["promotion_eligible"],
    }
    return MappingProxyType(copied)


@dataclass(frozen=True)
class LabState:
    """Immutable schema-v3 state; use :meth:`transition` to advance it."""

    schema: int
    revision: int
    lab_id: str
    provider_id: str
    ownership_token: str
    phase: StatePhase
    execution_profile: str = FIXTURE_EXECUTION_PROFILE
    planned_resources: Tuple[PlannedResource, ...] = ()
    resource_ids: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    artifact_evidence: Mapping[str, object] = field(
        default_factory=lambda: _LEGACY_SYNTHETIC_ARTIFACT
    )
    generation: int = 0
    auth_generation: int = 0
    pending_step: Optional[str] = None
    created_roles: Tuple[str, ...] = ()
    removed_roles: Tuple[str, ...] = ()
    tainted: bool = False
    operations: Tuple[OperationObservation, ...] = ()
    draft_evidence: Optional[Mapping[str, object]] = None
    seal_intent: Optional[SealIntent] = None
    baseline_equalities: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.schema != STATE_SCHEMA:
            raise UsageStateError("unsupported state schema")
        _strict_nonnegative_int(self.revision, "state revision")
        object.__setattr__(self, "lab_id", validate_lab_id(self.lab_id))
        object.__setattr__(self, "provider_id", validate_provider_id(self.provider_id))
        object.__setattr__(self, "ownership_token", validate_ownership_token(self.ownership_token))
        phase = _phase(self.phase)
        object.__setattr__(self, "phase", phase)
        execution_profile = _validate_execution_profile(self.execution_profile)
        object.__setattr__(self, "execution_profile", execution_profile)
        if type(self.planned_resources) is not tuple:
            raise UsageStateError("planned resources must be an immutable tuple")
        if len(set(resource.role for resource in self.planned_resources)) != len(self.planned_resources):
            raise UsageStateError("duplicate planned resource role")
        for resource in self.planned_resources:
            if type(resource) is not PlannedResource:
                raise UsageStateError("invalid planned resource")
            labels = resource.labels
            if labels[LABEL_PREFIX + "/lab-id"] != self.lab_id:
                raise UsageStateError("planned resource lab mismatch")
            if labels[LABEL_PREFIX + "/provider"] != self.provider_id:
                raise UsageStateError("planned resource provider mismatch")
            if labels[LABEL_PREFIX + "/ownership-token"] != self.ownership_token:
                raise UsageStateError("planned resource token mismatch")
        object.__setattr__(
            self,
            "resource_ids",
            _validate_resource_ids(self.resource_ids, self.planned_resources),
        )
        object.__setattr__(
            self,
            "artifact_evidence",
            _validate_artifact_evidence(self.artifact_evidence),
        )
        _strict_nonnegative_int(self.generation, "generation")
        _strict_nonnegative_int(self.auth_generation, "auth generation")
        required_pending = PENDING_STEPS.get(phase)
        if required_pending is not None and self.pending_step != required_pending:
            raise UsageStateError("pending step does not match phase")
        if required_pending is None and phase is not StatePhase.RECOVERY_REQUIRED and self.pending_step is not None:
            raise UsageStateError("stable state cannot have a pending step")
        if phase is StatePhase.RECOVERY_REQUIRED and self.pending_step not in set(PENDING_STEPS.values()):
            raise UsageStateError("recovery state requires interrupted step")
        if type(self.created_roles) is not tuple or len(set(self.created_roles)) != len(self.created_roles):
            raise UsageStateError("invalid created roles")
        planned_roles = set(resource.role for resource in self.planned_resources)
        for role in self.created_roles:
            try:
                ResourceRole(role)
            except (ValueError, TypeError):
                raise UsageStateError("invalid created role")
            if role not in planned_roles:
                raise UsageStateError("created role was not planned")
        if type(self.removed_roles) is not tuple or len(set(self.removed_roles)) != len(self.removed_roles):
            raise UsageStateError("invalid removed roles")
        for role in self.removed_roles:
            try:
                ResourceRole(role)
            except (ValueError, TypeError):
                raise UsageStateError("invalid removed role")
            if role not in self.created_roles:
                raise UsageStateError("removed role was not created")
        if type(self.tainted) is not bool:
            raise UsageStateError("invalid taint flag")
        if type(self.operations) is not tuple or any(
            type(operation) is not OperationObservation for operation in self.operations
        ):
            raise UsageStateError("invalid operation observations")
        validated_draft = _validate_draft(self.draft_evidence)
        if (
            validated_draft is not None
            and validated_draft["executor_kind"]
            != _executor_for_profile(execution_profile)
        ):
            raise InvariantRefusalError(
                "draft executor does not match execution profile"
            )
        if (
            validated_draft is not None
            and dict(validated_draft["artifact"])
            != dict(self.artifact_evidence)
        ):
            raise InvariantRefusalError(
                "draft artifact does not match durable artifact evidence"
            )
        if phase in _DRAFT_FORBIDDEN_PHASES and validated_draft is not None:
            raise UsageStateError("draft evidence is premature for state phase")
        if phase in _DRAFT_REQUIRED_PHASES and validated_draft is None:
            raise UsageStateError("state phase requires draft evidence")
        if validated_draft is not None:
            draft_operations = tuple(
                OperationObservation.from_dict(item)
                for item in validated_draft["operations"]
            )
            if self.operations[: len(draft_operations)] != draft_operations:
                raise InvariantRefusalError(
                    "draft operations are not a prefix of state operations"
                )
        object.__setattr__(self, "draft_evidence", validated_draft)
        if self.seal_intent is not None:
            object.__setattr__(self, "seal_intent", SealIntent.from_value(self.seal_intent))
        if phase in _SEAL_INTENT_PHASES and self.seal_intent is None:
            raise UsageStateError("state phase requires seal intent")
        if phase not in _SEAL_INTENT_PHASES and self.seal_intent is not None:
            raise UsageStateError("seal intent is premature for state phase")
        object.__setattr__(self, "baseline_equalities", _validate_baselines(self.baseline_equalities))

    @classmethod
    def initial(
        cls,
        lab_id: object,
        provider_id: object,
        ownership_token: object,
        planned_resources: Sequence[Union[PlannedResource, Mapping[str, object]]],
        baseline_equalities: Optional[Mapping[str, bool]] = None,
        execution_profile: object = FIXTURE_EXECUTION_PROFILE,
        artifact_evidence: Optional[Mapping[str, object]] = None,
    ) -> "LabState":
        resources = tuple(
            PlannedResource.from_value(item)
            for item in planned_resources
        )
        return cls(
            schema=STATE_SCHEMA,
            revision=0,
            lab_id=lab_id,
            provider_id=provider_id,
            ownership_token=ownership_token,
            phase=StatePhase.NEW,
            execution_profile=execution_profile,
            planned_resources=resources,
            artifact_evidence=(
                _LEGACY_SYNTHETIC_ARTIFACT
                if artifact_evidence is None
                else artifact_evidence
            ),
            baseline_equalities={} if baseline_equalities is None else baseline_equalities,
        )

    def to_dict(self) -> Dict[str, object]:
        draft = None
        if self.draft_evidence is not None:
            draft = {
                "artifact": dict(self.draft_evidence["artifact"]),
                "captured_at_ns": self.draft_evidence["captured_at_ns"],
                "evidence_kind": self.draft_evidence["evidence_kind"],
                "executor_kind": self.draft_evidence["executor_kind"],
                "manifest_schema_sha256": self.draft_evidence["manifest_schema_sha256"],
                "observed_protocol_schema_sha256": self.draft_evidence[
                    "observed_protocol_schema_sha256"
                ],
                "operations": [
                    dict(operation) for operation in self.draft_evidence["operations"]
                ],
                "promotion_eligible": self.draft_evidence["promotion_eligible"],
            }
        return {
            "artifact_evidence": dict(self.artifact_evidence),
            "auth_generation": self.auth_generation,
            "baseline_equalities": dict(self.baseline_equalities),
            "created_roles": list(self.created_roles),
            "draft_evidence": draft,
            "execution_profile": self.execution_profile,
            "generation": self.generation,
            "lab_id": self.lab_id,
            "operations": [operation.to_dict() for operation in self.operations],
            "ownership_token": self.ownership_token,
            "pending_step": self.pending_step,
            "phase": self.phase.value,
            "planned_resources": [resource.to_dict() for resource in self.planned_resources],
            "provider_id": self.provider_id,
            "removed_roles": list(self.removed_roles),
            "resource_ids": dict(self.resource_ids),
            "revision": self.revision,
            "schema": self.schema,
            "seal_intent": None if self.seal_intent is None else self.seal_intent.to_dict(),
            "tainted": self.tainted,
        }

    @classmethod
    def from_dict(cls, value: object) -> "LabState":
        schema_v2_keys = frozenset(
            (
                "schema",
                "revision",
                "lab_id",
                "provider_id",
                "ownership_token",
                "phase",
                "planned_resources",
                "generation",
                "auth_generation",
                "pending_step",
                "created_roles",
                "removed_roles",
                "tainted",
                "operations",
                "draft_evidence",
                "seal_intent",
                "baseline_equalities",
            )
        )
        early_schema_v3_keys = schema_v2_keys | frozenset(("execution_profile",))
        schema_v3_keys = early_schema_v3_keys | frozenset(
            ("artifact_evidence", "resource_ids")
        )
        if not isinstance(value, Mapping):
            raise UsageStateError("invalid state fields")
        if value.get("schema") == LEGACY_FIXTURE_STATE_SCHEMA:
            legacy = _exact_mapping(value, schema_v2_keys, "state")
            data = dict(legacy)
            data["schema"] = STATE_SCHEMA
            # Schema 2 was emitted only by the fixture-only launcher. Never
            # infer a real executor from legacy content or its filesystem.
            data["execution_profile"] = FIXTURE_EXECUTION_PROFILE
            legacy_draft = data.get("draft_evidence")
            data["artifact_evidence"] = (
                dict(legacy_draft["artifact"])
                if isinstance(legacy_draft, Mapping)
                and isinstance(legacy_draft.get("artifact"), Mapping)
                else dict(_LEGACY_SYNTHETIC_ARTIFACT)
            )
            data["resource_ids"] = {}
        else:
            keys = frozenset(value.keys())
            if keys == early_schema_v3_keys:
                data = dict(value)
                early_draft = data.get("draft_evidence")
                data["artifact_evidence"] = (
                    dict(early_draft["artifact"])
                    if isinstance(early_draft, Mapping)
                    and isinstance(early_draft.get("artifact"), Mapping)
                    else dict(_LEGACY_SYNTHETIC_ARTIFACT)
                )
                data["resource_ids"] = {}
            else:
                data = _exact_mapping(value, schema_v3_keys, "state")
        _check_forbidden_keys(data)
        if (
            type(data["planned_resources"]) is not list
            or type(data["created_roles"]) is not list
            or type(data["removed_roles"]) is not list
        ):
            raise UsageStateError("invalid state collections")
        if type(data["operations"]) is not list:
            raise UsageStateError("invalid state operations")
        return cls(
            schema=data["schema"],
            revision=data["revision"],
            lab_id=data["lab_id"],
            provider_id=data["provider_id"],
            ownership_token=data["ownership_token"],
            phase=data["phase"],
            execution_profile=data["execution_profile"],
            planned_resources=tuple(PlannedResource.from_dict(item) for item in data["planned_resources"]),
            resource_ids=data["resource_ids"],
            artifact_evidence=data["artifact_evidence"],
            generation=data["generation"],
            auth_generation=data["auth_generation"],
            pending_step=data["pending_step"],
            created_roles=tuple(data["created_roles"]),
            removed_roles=tuple(data["removed_roles"]),
            tainted=data["tainted"],
            operations=tuple(OperationObservation.from_dict(item) for item in data["operations"]),
            draft_evidence=data["draft_evidence"],
            seal_intent=data["seal_intent"],
            baseline_equalities=data["baseline_equalities"],
        )

    def transition(self, new_phase: Union[StatePhase, str], **safe_updates: object) -> "LabState":
        """Return the next immutable state after a legal lifecycle edge.

        Allowed updates are intentionally finite: ``created_roles``, ``removed_roles``,
        ``generation``, ``auth_generation``, ``operations``,
        ``draft_evidence``, ``seal_intent``, and ``baseline_equalities``.
        """

        target = _phase(new_phase)
        if target not in TRANSITIONS[self.phase]:
            raise UsageStateError("invalid state transition")
        allowed = frozenset(
            (
                "created_roles",
                "removed_roles",
                "generation",
                "auth_generation",
                "operations",
                "draft_evidence",
                "seal_intent",
                "baseline_equalities",
            )
        )
        if not set(safe_updates).issubset(allowed):
            raise UsageStateError("unsafe state update")
        if self.tainted and target in (
            StatePhase.EVIDENCE_PENDING,
            StatePhase.EVIDENCE_CAPTURED,
            StatePhase.SEAL_PENDING,
            StatePhase.PASSED,
            StatePhase.FAILED_CLEAN,
        ):
            raise InvariantRefusalError(
                "tainted verification/promotion-held state cannot produce evidence"
            )
        updates = dict(safe_updates)
        if "created_roles" in updates:
            updates["created_roles"] = tuple(updates["created_roles"])
            if self.created_roles != updates["created_roles"][: len(self.created_roles)]:
                raise UsageStateError("created roles are monotonic")
        if "removed_roles" in updates:
            updates["removed_roles"] = tuple(updates["removed_roles"])
            if self.removed_roles != updates["removed_roles"][: len(self.removed_roles)]:
                raise UsageStateError("removed roles are monotonic")
        if "operations" in updates:
            updates["operations"] = tuple(updates["operations"])
            if self.operations != updates["operations"][: len(self.operations)]:
                raise UsageStateError("operation observations are append-only")
        if "generation" in updates:
            _strict_nonnegative_int(updates["generation"], "generation")
            if updates["generation"] < self.generation:
                raise UsageStateError("generation is monotonic")
        if "auth_generation" in updates:
            _strict_nonnegative_int(updates["auth_generation"], "auth generation")
            if updates["auth_generation"] < self.auth_generation:
                raise UsageStateError("auth generation is monotonic")
        if "draft_evidence" in updates:
            normal_capture = target is StatePhase.EVIDENCE_CAPTURED
            recovered_failure_capture = (
                target is StatePhase.SEAL_PENDING
                and self.phase is StatePhase.CLEAN_VERIFIED
                and any(operation.outcome == "failed" for operation in self.operations)
            )
            if (
                (not normal_capture and not recovered_failure_capture)
                or self.draft_evidence is not None
            ):
                raise UsageStateError("draft evidence can only be captured once")
        if "seal_intent" in updates:
            if target is not StatePhase.SEAL_PENDING or self.seal_intent is not None:
                raise UsageStateError("seal intent can only be recorded once")
            updates["seal_intent"] = SealIntent.from_value(updates["seal_intent"])
        effective_draft = updates.get("draft_evidence", self.draft_evidence)
        if target in (
            StatePhase.EVIDENCE_CAPTURED,
            StatePhase.SEAL_PENDING,
            StatePhase.PASSED,
            StatePhase.FAILED_CLEAN,
        ) and effective_draft is None:
            raise UsageStateError("evidence phase requires a captured draft")
        effective_seal_intent = updates.get("seal_intent", self.seal_intent)
        if target in _SEAL_INTENT_PHASES and effective_seal_intent is None:
            raise UsageStateError("seal phase requires a seal intent")
        if target is StatePhase.EVIDENCE_CAPTURED or "draft_evidence" in updates:
            validated_draft = _validate_draft(effective_draft)
            if dict(validated_draft["artifact"]) != dict(self.artifact_evidence):
                raise InvariantRefusalError(
                    "captured artifact does not match durable artifact evidence"
                )
            effective_operations = updates.get("operations", self.operations)
            draft_operations = tuple(
                OperationObservation.from_dict(item)
                for item in validated_draft["operations"]
            )
            if draft_operations != tuple(effective_operations):
                raise InvariantRefusalError(
                    "captured evidence operations do not match state operations"
                )
        updates.update(
            {
                "phase": target,
                "revision": self.revision + 1,
                "pending_step": PENDING_STEPS.get(target),
            }
        )
        return replace(self, **updates)

    def mark_tainted(self) -> "LabState":
        """Enter the irreversible verification and promotion hold.

        A command layer must persist this state before any action that makes
        automated verification or promotion ineligible. The flag is monotonic
        and there is intentionally no API that clears it.
        """

        if self.phase in (
            StatePhase.SEAL_PENDING,
            StatePhase.PASSED,
            StatePhase.FAILED_CLEAN,
        ):
            raise UsageStateError(
                "sealed state cannot enter the verification/promotion hold"
            )
        if self.tainted:
            return self
        return replace(self, revision=self.revision + 1, tainted=True)

    def record_owned_role(self, role: Union[ResourceRole, str]) -> "LabState":
        """Durably record one validated owned resource during create/cleanup.

        The cleanup phase may discover a resource created immediately before
        an interrupted write. Recording it before removal keeps final fixture
        cleanup counts conservative and restart-stable.
        """

        if self.phase not in (StatePhase.CREATE_PENDING, StatePhase.DESTROY_PENDING):
            raise UsageStateError("owned resources can only be recorded during create or cleanup")
        try:
            normalized = role if isinstance(role, ResourceRole) else ResourceRole(role)
        except (TypeError, ValueError):
            raise UsageStateError("invalid owned resource role")
        value = normalized.value
        planned = frozenset(resource.role for resource in self.planned_resources)
        if value not in planned:
            raise UsageStateError("owned resource was not planned")
        if value in self.created_roles:
            return self
        return replace(
            self,
            revision=self.revision + 1,
            created_roles=self.created_roles + (value,),
        )

    def record_resource_id(
        self,
        role: Union[ResourceRole, str],
        resource_id: object,
    ) -> "LabState":
        """Record one immutable daemon identity for exact cleanup."""

        if self.phase not in (
            StatePhase.CREATE_PENDING,
            StatePhase.DESTROY_PENDING,
        ):
            raise UsageStateError(
                "resource ids can only be recorded during create or cleanup"
            )
        try:
            normalized = role if isinstance(role, ResourceRole) else ResourceRole(role)
        except (TypeError, ValueError):
            raise UsageStateError("invalid resource id role")
        candidate = dict(self.resource_ids)
        existing = candidate.get(normalized.value)
        if existing is not None:
            if existing != resource_id:
                raise InvariantRefusalError("recorded resource id is immutable")
            return self
        candidate[normalized.value] = resource_id
        validated = _validate_resource_ids(candidate, self.planned_resources)
        return replace(
            self,
            revision=self.revision + 1,
            resource_ids=validated,
        )

    def record_removed_role(self, role: Union[ResourceRole, str]) -> "LabState":
        """Durably append one exact successfully removed resource role."""

        if self.phase is not StatePhase.DESTROY_PENDING:
            raise UsageStateError("removed resources can only be recorded during cleanup")
        try:
            normalized = role if isinstance(role, ResourceRole) else ResourceRole(role)
        except (TypeError, ValueError):
            raise UsageStateError("invalid removed resource role")
        value = normalized.value
        if value not in self.created_roles:
            raise UsageStateError("removed resource was not durably recorded as created")
        if value in self.removed_roles:
            return self
        return replace(
            self,
            revision=self.revision + 1,
            removed_roles=self.removed_roles + (value,),
        )

    def fail_pending(self, observation: OperationObservation) -> "LabState":
        """Record a failed pending side effect and enter cleanup-only recovery."""

        interrupted_step = PENDING_STEPS.get(self.phase)
        if interrupted_step is None:
            raise UsageStateError("only a pending state can fail into recovery")
        if self.phase is StatePhase.SEAL_PENDING:
            raise UsageStateError("seal pending must be reconciled")
        if type(observation) is not OperationObservation:
            raise UsageStateError("invalid pending failure observation")
        if observation.step != interrupted_step or observation.outcome != "failed":
            raise UsageStateError("pending failure observation does not match step")
        return replace(
            self,
            phase=StatePhase.RECOVERY_REQUIRED,
            revision=self.revision + 1,
            pending_step=interrupted_step,
            operations=self.operations + (observation,),
        )

    def interrupt_stable_forward(self) -> "LabState":
        """Explicitly abandon one stable forward phase into cleanup recovery.

        Ordinary loading deliberately does not call this method. Command layers
        use it only after an interruption occurred before the next pending
        intent could be committed.
        """

        interrupted_step = STABLE_FORWARD_STEPS.get(self.phase)
        if interrupted_step is None:
            raise UsageStateError(
                "only a stable forward state can be interrupted into recovery"
            )
        observation = OperationObservation(
            step=interrupted_step,
            outcome="failed",
            exit_code=1,
            latency_ns=0,
            error_code="interrupted",
        )
        return replace(
            self,
            phase=StatePhase.RECOVERY_REQUIRED,
            revision=self.revision + 1,
            pending_step=interrupted_step,
            operations=self.operations + (observation,),
        )


def recover_interrupted_state(state: LabState) -> LabState:
    """Convert a loaded pending state into durable cleanup-only recovery."""

    if state.phase not in PENDING_STEPS or state.phase is StatePhase.SEAL_PENDING:
        return state
    interrupted_step = PENDING_STEPS[state.phase]
    observation = OperationObservation(
        step=interrupted_step,
        outcome="failed",
        exit_code=1,
        latency_ns=0,
        error_code="interrupted",
    )
    return replace(
        state,
        phase=StatePhase.RECOVERY_REQUIRED,
        revision=state.revision + 1,
        pending_step=interrupted_step,
        operations=state.operations + (observation,),
    )


def _reject_constant(value: str) -> object:
    raise UsageStateError("non-finite JSON number is not permitted")


def _pairs_no_duplicates(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageStateError("duplicate JSON object key")
        result[key] = value
    return result


def strict_json_loads(payload: bytes, maximum_bytes: int = MAX_STATE_BYTES) -> object:
    """Decode bounded UTF-8 JSON, rejecting duplicates and non-finite values."""

    if type(payload) is not bytes or len(payload) > maximum_bytes:
        raise UsageStateError("invalid or oversized JSON document")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise UsageStateError("invalid JSON document") from error
    _check_forbidden_keys(value)
    return value


def canonical_json_bytes(value: object) -> bytes:
    _check_forbidden_keys(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise UsageStateError("state is not strict JSON") from error
    if len(encoded) > MAX_STATE_BYTES:
        raise UsageStateError("state document is too large")
    return encoded


def _rename_noreplace_primitive() -> Optional[object]:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is not None:
            function.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            function.restype = ctypes.c_int
            return (function, 0x00000004)  # RENAME_EXCL
    if sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is not None:
            function.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            function.restype = ctypes.c_int
            return (function, 0x00000001)  # RENAME_NOREPLACE
    return None


def _rename_noreplace_at(
    source_directory: int,
    source: str,
    destination_directory: int,
    destination: str,
) -> None:
    primitive = _rename_noreplace_primitive()
    if primitive is None:
        raise UnsupportedError("atomic no-replace rename is unsupported")
    function, flag = primitive
    ctypes.set_errno(0)
    result = function(
        source_directory,
        os.fsencode(source),
        destination_directory,
        os.fsencode(destination),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number, os.strerror(error_number), destination
        )
    if error_number in (
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    ):
        raise UnsupportedError("atomic no-replace rename is unsupported")
    raise OSError(error_number, os.strerror(error_number), destination)


def ensure_process_lock_supported() -> None:
    if os.name != "posix" or _fcntl is None:
        raise UnsupportedError("process locking is unsupported on this platform")
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
        or os.link not in os.supports_dir_fd
        or os.rename not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or _rename_noreplace_primitive() is None
    ):
        raise UnsupportedError("safe process locking is unsupported on this platform")


def _check_owned(st: os.stat_result, field: str) -> None:
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise InvariantRefusalError("{} is not owned by the current user".format(field))


def _check_private_directory_stat(st: os.stat_result, field: str) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise InvariantRefusalError("unsafe {}".format(field))
    _check_owned(st, field)
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise InvariantRefusalError("{} permissions must be 0700".format(field))


def _check_private_file_stat(st: os.stat_result, field: str) -> None:
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise InvariantRefusalError("unsafe {}".format(field))
    _check_owned(st, field)
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise InvariantRefusalError("{} permissions must be 0600".format(field))


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_ctime_ns) == (
        right.st_dev,
        right.st_ino,
        right.st_ctime_ns,
    )


def _ensure_private_directory(path: Path, create: bool) -> None:
    if not path.is_absolute():
        raise UsageStateError("state root must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        # macOS exposes /var (and commonly /tmp) as a root-owned compatibility
        # symlink.  It is outside the caller-controlled state tree; all other
        # symlink components are refused.
        if current.exists() and current.is_symlink() and str(current) not in ("/var", "/tmp"):
            raise InvariantRefusalError("unsafe state directory component")
    try:
        before = path.lstat()
    except FileNotFoundError:
        if not create:
            raise UsageStateError("state directory does not exist")
        if not path.parent.exists():
            raise UsageStateError("state directory parent does not exist")
        if path.parent.is_symlink():
            raise InvariantRefusalError("unsafe state directory component")
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        before = path.lstat()
        # Make the new directory contents durable before its parent entry.
        # This ordering is repeated independently for the state root and the
        # per-lab directory.
        _fsync_directory(path)
        _fsync_directory(path.parent)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise InvariantRefusalError("unsafe state directory")
    _check_private_directory_stat(before, "state directory")


def _check_private_file(path: Path) -> None:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise InvariantRefusalError("unsafe state file")
    _check_private_file_stat(st, "state file")


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        _check_private_directory_stat(opened, "state directory")
        named = path.lstat()
        if stat.S_ISLNK(named.st_mode):
            raise InvariantRefusalError("unsafe state directory")
        _check_private_directory_stat(named, "state directory")
        if not _same_inode(opened, named):
            raise InvariantRefusalError("state directory changed during acquisition")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_directory_descriptor_path(descriptor: int, path: Path) -> None:
    opened = os.fstat(descriptor)
    _check_private_directory_stat(opened, "state directory")
    try:
        named = path.lstat()
    except FileNotFoundError as error:
        raise InvariantRefusalError("state directory changed during acquisition") from error
    if stat.S_ISLNK(named.st_mode):
        raise InvariantRefusalError("unsafe state directory")
    _check_private_directory_stat(named, "state directory")
    if not _same_inode(opened, named):
        raise InvariantRefusalError("state directory changed during acquisition")


def _stat_identity(st: os.stat_result) -> _FileIdentity:
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


def _same_file_after_rename(
    before: _FileIdentity, after: os.stat_result
) -> bool:
    """Match a renamed inode while accounting for the rename ctime update."""

    observed = _stat_identity(after)
    return observed[:-1] == before[:-1] and observed[-1] >= before[-1]


def _stat_private_lock_file(
    directory_descriptor: int,
    name: str = LOCK_FILE_NAME,
    field: str = "state lock",
) -> os.stat_result:
    try:
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise InvariantRefusalError("{} changed during acquisition".format(field)) from error
    if stat.S_ISLNK(named.st_mode):
        raise InvariantRefusalError("unsafe {}".format(field))
    _check_private_file_stat(named, field)
    return named


def _check_lock_descriptor_path(
    descriptor: int,
    directory_descriptor: int,
    name: str = LOCK_FILE_NAME,
    field: str = "state lock",
) -> None:
    opened = os.fstat(descriptor)
    _check_private_file_stat(opened, field)
    named = _stat_private_lock_file(directory_descriptor, name, field)
    if not _same_inode(opened, named):
        raise InvariantRefusalError("{} changed during acquisition".format(field))


def _open_private_lock_file(
    directory_descriptor: int,
    name: str = LOCK_FILE_NAME,
    field: str = "state lock",
) -> int:
    flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    created = False
    named = None
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        # Validate the name before opening as well as the resulting descriptor.
        # O_NONBLOCK prevents a raced-in special file such as a FIFO from
        # stalling acquisition before fstat can reject it.
        named = _stat_private_lock_file(directory_descriptor, name, field)
        try:
            descriptor = os.open(
                name,
                flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError as error:
            raise InvariantRefusalError(
                "{} changed during acquisition".format(field)
            ) from error
    else:
        created = True
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        elif _stat_identity(os.fstat(descriptor)) != _stat_identity(named):
            raise InvariantRefusalError(
                "{} changed during acquisition".format(field)
            )
        _check_lock_descriptor_path(
            descriptor, directory_descriptor, name, field
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _unlock_and_close(descriptor: int, locked: bool) -> None:
    try:
        if locked:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        before = path.lstat()
    except OSError as error:
        raise InvariantRefusalError(
            "directory changed during durability check"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise InvariantRefusalError("unsafe durability directory")
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise InvariantRefusalError(
            "directory changed during durability check"
        ) from error
    try:
        opened = os.fstat(descriptor)
        post_open = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(post_open.st_mode)
            or not stat.S_ISDIR(post_open.st_mode)
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
                opened.st_gid,
            )
            != identity
            or (
                post_open.st_dev,
                post_open.st_ino,
                post_open.st_mode,
                post_open.st_uid,
                post_open.st_gid,
            )
            != identity
        ):
            raise InvariantRefusalError(
                "directory changed during durability check"
            )
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_gid,
            )
            != identity
            or stat.S_ISLNK(final.st_mode)
            or not stat.S_ISDIR(final.st_mode)
            or (
                final.st_dev,
                final.st_ino,
                final.st_mode,
                final.st_uid,
                final.st_gid,
            )
            != identity
        ):
            raise InvariantRefusalError(
                "directory changed during durability check"
            )
    finally:
        os.close(descriptor)


def _fsync_directory_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _ensure_private_directory_at(
    parent_descriptor: int, name: str, *, create: bool
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise UsageStateError("state directory does not exist")
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        _check_private_directory_stat(current, "state directory")
        child = _open_private_directory_at(parent_descriptor, name)
        try:
            _fsync_directory_descriptor(child)
        finally:
            os.close(child)
        _fsync_directory_descriptor(parent_descriptor)
        return
    if stat.S_ISLNK(current.st_mode):
        raise InvariantRefusalError("unsafe state directory")
    _check_private_directory_stat(current, "state directory")


def _open_private_directory_at(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        _check_private_directory_stat(opened, "state directory")
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(named.st_mode):
            raise InvariantRefusalError("unsafe state directory")
        _check_private_directory_stat(named, "state directory")
        if not _same_inode(opened, named):
            raise InvariantRefusalError("state directory changed during acquisition")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_directory_descriptor_entry(
    descriptor: int,
    parent_descriptor: int,
    name: str,
) -> None:
    opened = os.fstat(descriptor)
    _check_private_directory_stat(opened, "state directory")
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as error:
        raise InvariantRefusalError("state directory changed while locked") from error
    if stat.S_ISLNK(named.st_mode):
        raise InvariantRefusalError("unsafe state directory")
    _check_private_directory_stat(named, "state directory")
    if not _same_inode(opened, named):
        raise InvariantRefusalError("state directory changed while locked")


def _stat_private_named_state_file(
    directory_descriptor: int,
    name: str,
    field: str = "state file",
) -> os.stat_result:
    try:
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(named.st_mode):
        raise InvariantRefusalError("unsafe {}".format(field))
    _check_private_file_stat(named, field)
    return named


def _stat_private_state_file(directory_descriptor: int) -> os.stat_result:
    return _stat_private_named_state_file(
        directory_descriptor, STATE_FILE_NAME
    )


def _read_private_named_state_file_at(
    directory_descriptor: int,
    name: str,
    field: str = "state file",
) -> Tuple[bytes, os.stat_result]:
    named = _stat_private_named_state_file(
        directory_descriptor, name, field
    )
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        _check_private_file_stat(opened, field)
        if _stat_identity(opened) != _stat_identity(named):
            raise InvariantRefusalError(
                "{} identity changed while locked".format(field)
            )
        chunks = []
        remaining = MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if _stat_identity(after) != _stat_identity(opened):
            raise InvariantRefusalError(
                "{} identity changed while locked".format(field)
            )
    finally:
        os.close(descriptor)
    final_named = _stat_private_named_state_file(
        directory_descriptor, name, field
    )
    if _stat_identity(final_named) != _stat_identity(opened):
        raise InvariantRefusalError(
            "{} identity changed while locked".format(field)
        )
    return payload, final_named


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


@dataclass(frozen=True)
class _StateReplaceIntent:
    """Exact, private authorization for one no-replace state transaction."""

    lab_id: str
    execution_profile: str
    predecessor_payload_sha256: str
    successor_payload_sha256: str
    predecessor_revision: int
    successor_revision: int
    predecessor_file_identity: _FileIdentity
    format: int = 1

    def __post_init__(self) -> None:
        if self.format != 1:
            raise UsageStateError("unsupported state replacement intent")
        validate_lab_id(self.lab_id)
        _validate_execution_profile(self.execution_profile)
        for value, field in (
            (self.predecessor_payload_sha256, "predecessor payload digest"),
            (self.successor_payload_sha256, "successor payload digest"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise UsageStateError("invalid {}".format(field))
        _strict_nonnegative_int(
            self.predecessor_revision, "predecessor state revision"
        )
        _strict_nonnegative_int(
            self.successor_revision, "successor state revision"
        )
        if (
            type(self.predecessor_file_identity) is not tuple
            or len(self.predecessor_file_identity) != 8
            or any(
                type(item) is not int or item < 0
                for item in self.predecessor_file_identity
            )
        ):
            raise UsageStateError("invalid predecessor file identity")

    def to_dict(self) -> Dict[str, object]:
        return {
            "execution_profile": self.execution_profile,
            "format": self.format,
            "lab_id": self.lab_id,
            "predecessor_file_identity": list(
                self.predecessor_file_identity
            ),
            "predecessor_payload_sha256": self.predecessor_payload_sha256,
            "predecessor_revision": self.predecessor_revision,
            "successor_payload_sha256": self.successor_payload_sha256,
            "successor_revision": self.successor_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> "_StateReplaceIntent":
        keys = frozenset(
            (
                "execution_profile",
                "format",
                "lab_id",
                "predecessor_file_identity",
                "predecessor_payload_sha256",
                "predecessor_revision",
                "successor_payload_sha256",
                "successor_revision",
            )
        )
        data = _exact_mapping(value, keys, "state replacement intent")
        identity = data["predecessor_file_identity"]
        if type(identity) is not list:
            raise UsageStateError("invalid predecessor file identity")
        return cls(
            execution_profile=data["execution_profile"],
            format=data["format"],
            lab_id=data["lab_id"],
            predecessor_file_identity=tuple(identity),
            predecessor_payload_sha256=data["predecessor_payload_sha256"],
            predecessor_revision=data["predecessor_revision"],
            successor_payload_sha256=data["successor_payload_sha256"],
            successor_revision=data["successor_revision"],
        )


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _state_from_transaction_payload(payload: bytes) -> LabState:
    return LabState.from_dict(strict_json_loads(payload))


def _new_state_replace_intent_at(
    directory_descriptor: int, intent: _StateReplaceIntent
) -> Tuple[bytes, _FileIdentity]:
    payload = canonical_json_bytes(intent.to_dict())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            STATE_REPLACE_INTENT_NAME,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError as error:
        raise InvariantRefusalError(
            "unfinished state replacement requires recovery"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        _check_private_file_stat(opened, "state replacement intent")
    finally:
        os.close(descriptor)
    named = _stat_private_named_state_file(
        directory_descriptor,
        STATE_REPLACE_INTENT_NAME,
        "state replacement intent",
    )
    if _stat_identity(named) != _stat_identity(opened):
        raise InvariantRefusalError(
            "state replacement intent changed during creation"
        )
    _fsync_directory_descriptor(directory_descriptor)
    return payload, _stat_identity(named)


def _read_state_replace_intent_at(
    directory_descriptor: int,
) -> Tuple[_StateReplaceIntent, bytes, os.stat_result]:
    payload, named = _read_private_named_state_file_at(
        directory_descriptor,
        STATE_REPLACE_INTENT_NAME,
        "state replacement intent",
    )
    try:
        intent = _StateReplaceIntent.from_dict(strict_json_loads(payload))
        if canonical_json_bytes(intent.to_dict()) != payload:
            raise UsageStateError("noncanonical state replacement intent")
    except UsageStateError as error:
        raise InvariantRefusalError("invalid state replacement intent") from error
    return intent, payload, named


def _remove_exact_private_file_at(
    directory_descriptor: int,
    name: str,
    expected_identity: _FileIdentity,
    expected_payload: bytes,
    field: str,
) -> None:
    current = _stat_private_named_state_file(
        directory_descriptor, name, field
    )
    if _stat_identity(current) != expected_identity:
        raise InvariantRefusalError("{} changed before removal".format(field))
    quarantine_names = {
        STATE_REPLACE_BACKUP_NAME: STATE_REPLACE_BACKUP_REMOVING_NAME,
        STATE_REPLACE_INTENT_NAME: STATE_REPLACE_INTENT_REMOVING_NAME,
    }
    try:
        quarantine = quarantine_names[name]
    except KeyError as error:
        raise UsageStateError("unsupported private transaction removal") from error
    _rename_noreplace_at(
        directory_descriptor,
        name,
        directory_descriptor,
        quarantine,
    )
    moved_payload, moved = _read_private_named_state_file_at(
        directory_descriptor, quarantine, field
    )
    if (
        not _same_file_after_rename(expected_identity, moved)
        or moved_payload != expected_payload
    ):
        raise InvariantRefusalError("{} changed during removal".format(field))
    os.unlink(quarantine, dir_fd=directory_descriptor)


def _restore_interrupted_private_removal_at(
    directory_descriptor: int,
    name: str,
    quarantine: str,
    field: str,
) -> None:
    """Put a crash-interrupted exact-file removal back for normal recovery."""

    try:
        payload, moved = _read_private_named_state_file_at(
            directory_descriptor, quarantine, field
        )
    except FileNotFoundError:
        return
    try:
        _stat_private_named_state_file(directory_descriptor, name, field)
    except FileNotFoundError:
        pass
    else:
        raise InvariantRefusalError(
            "{} and its removal record both exist".format(field)
        )
    moved_identity = _stat_identity(moved)
    try:
        _rename_noreplace_at(
            directory_descriptor,
            quarantine,
            directory_descriptor,
            name,
        )
    except FileExistsError as error:
        raise InvariantRefusalError(
            "{} changed during removal recovery".format(field)
        ) from error
    restored_payload, restored = _read_private_named_state_file_at(
        directory_descriptor, name, field
    )
    if (
        restored_payload != payload
        or not _same_file_after_rename(moved_identity, restored)
    ):
        raise InvariantRefusalError(
            "{} changed during removal recovery".format(field)
        )
    _fsync_directory_descriptor(directory_descriptor)


def _same_bound_private_file(
    before: _FileIdentity, after: os.stat_result
) -> bool:
    """Match the exact created inode while allowing link/rename ctime changes."""

    observed = _stat_identity(after)
    return (
        observed[:4] == before[:4]
        and observed[5:7] == before[5:7]
        and observed[-1] >= before[-1]
    )


def _unlink_bound_temporary_at(
    directory_descriptor: int,
    name: str,
    expected_identity: _FileIdentity,
) -> bool:
    """Remove the generated name only while it still names our exact inode."""

    try:
        current = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(current.st_mode)
        or not _same_bound_private_file(expected_identity, current)
    ):
        raise InvariantRefusalError("temporary state file changed")
    os.unlink(name, dir_fd=directory_descriptor)
    return True


def _new_private_temporary(
    directory_descriptor: int, payload: bytes
) -> Tuple[str, int, _FileIdentity]:
    temporary = ".{}.{}.tmp".format(STATE_FILE_NAME, secrets.token_hex(8))
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        _check_private_file_stat(opened, "temporary state file")
        identity = _stat_identity(opened)
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = b""
        while len(observed) <= MAX_STATE_BYTES:
            chunk = os.read(descriptor, MAX_STATE_BYTES + 1 - len(observed))
            if not chunk:
                break
            observed += chunk
        after = os.fstat(descriptor)
        named = _stat_private_named_state_file(
            directory_descriptor, temporary, "temporary state file"
        )
        if (
            observed != payload
            or _stat_identity(after) != identity
            or _stat_identity(named) != identity
        ):
            raise InvariantRefusalError(
                "temporary state file changed during creation"
            )
    except BaseException:
        try:
            owned_identity = _stat_identity(os.fstat(descriptor))
            _unlink_bound_temporary_at(
                directory_descriptor, temporary, owned_identity
            )
        except FileNotFoundError:
            pass
        finally:
            os.close(descriptor)
        raise
    return temporary, descriptor, identity


def _atomic_create_at(directory_descriptor: int, payload: bytes) -> os.stat_result:
    try:
        _stat_private_state_file(directory_descriptor)
    except FileNotFoundError:
        pass
    else:
        raise UsageStateError("lab state already exists")
    temporary, temporary_descriptor, temporary_identity = (
        _new_private_temporary(directory_descriptor, payload)
    )
    try:
        try:
            _rename_noreplace_at(
                directory_descriptor,
                temporary,
                directory_descriptor,
                STATE_FILE_NAME,
            )
        except FileExistsError as error:
            raise UsageStateError("lab state already exists") from error
        published_payload, published = _read_private_named_state_file_at(
            directory_descriptor, STATE_FILE_NAME
        )
        if (
            published_payload != payload
            or not _same_bound_private_file(
                temporary_identity, published
            )
            or published.st_nlink != 1
        ):
            raise InvariantRefusalError(
                "state file identity changed during publication"
            )
    finally:
        try:
            _unlink_bound_temporary_at(
                directory_descriptor, temporary, temporary_identity
            )
        finally:
            os.close(temporary_descriptor)
    _fsync_directory_descriptor(directory_descriptor)
    created_payload, created = _read_private_named_state_file_at(
        directory_descriptor, STATE_FILE_NAME
    )
    if (
        created_payload != payload
        or not _same_bound_private_file(temporary_identity, created)
        or created.st_nlink != 1
    ):
        raise InvariantRefusalError(
            "state file identity changed during publication"
        )
    return created


def _atomic_replace_at(
    directory_descriptor: int,
    payload: bytes,
    expected_identity: _FileIdentity,
) -> os.stat_result:
    predecessor_payload, current = _read_private_named_state_file_at(
        directory_descriptor, STATE_FILE_NAME
    )
    if _stat_identity(current) != expected_identity:
        raise InvariantRefusalError("state file identity changed while locked")
    predecessor_state = _state_from_transaction_payload(predecessor_payload)
    successor_state = _state_from_transaction_payload(payload)
    if canonical_json_bytes(strict_json_loads(payload)) != payload:
        raise InvariantRefusalError("successor state payload is not canonical")
    same_lineage = (
        predecessor_state.lab_id == successor_state.lab_id
        and predecessor_state.provider_id == successor_state.provider_id
        and predecessor_state.ownership_token == successor_state.ownership_token
        and predecessor_state.execution_profile
        == successor_state.execution_profile
        and predecessor_state.planned_resources
        == successor_state.planned_resources
        and predecessor_state.artifact_evidence
        == successor_state.artifact_evidence
    )
    normal_successor = (
        successor_state.revision == predecessor_state.revision + 1
    )
    exact_migration = (
        successor_state.revision == predecessor_state.revision
        and successor_state == predecessor_state
        and payload != predecessor_payload
    )
    if not same_lineage or (not normal_successor and not exact_migration):
        raise InvariantRefusalError("invalid state replacement successor")

    temporary, temporary_descriptor, temporary_identity = (
        _new_private_temporary(directory_descriptor, payload)
    )
    try:
        intent = _StateReplaceIntent(
            lab_id=predecessor_state.lab_id,
            execution_profile=predecessor_state.execution_profile,
            predecessor_payload_sha256=_payload_sha256(predecessor_payload),
            successor_payload_sha256=_payload_sha256(payload),
            predecessor_revision=predecessor_state.revision,
            successor_revision=successor_state.revision,
            predecessor_file_identity=expected_identity,
        )
        intent_payload, intent_identity = _new_state_replace_intent_at(
            directory_descriptor, intent
        )
        current_payload, current = _read_private_named_state_file_at(
            directory_descriptor, STATE_FILE_NAME
        )
        if (
            _stat_identity(current) != expected_identity
            or current_payload != predecessor_payload
        ):
            raise InvariantRefusalError("state file identity changed while locked")
        current_intent = _stat_private_named_state_file(
            directory_descriptor,
            STATE_REPLACE_INTENT_NAME,
            "state replacement intent",
        )
        if _stat_identity(current_intent) != intent_identity:
            raise InvariantRefusalError(
                "state replacement intent changed while locked"
            )

        # A portable rename() is not compare-and-swap: a non-cooperating
        # same-user writer can replace state.json after the identity check and
        # be silently overwritten.  Move the current name aside and publish
        # the new name with kernel-enforced no-replace operations instead.
        try:
            _rename_noreplace_at(
                directory_descriptor,
                STATE_FILE_NAME,
                directory_descriptor,
                STATE_REPLACE_BACKUP_NAME,
            )
        except FileExistsError as error:
            raise InvariantRefusalError(
                "unfinished state replacement requires recovery"
            ) from error

        backup = _stat_private_named_state_file(
            directory_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            "state replacement backup",
        )
        if not _same_file_after_rename(expected_identity, backup):
            # The checked target was replaced immediately before the first
            # rename. Restore exactly what was moved, but never overwrite a
            # new state.json that may have appeared in the meantime.
            try:
                _rename_noreplace_at(
                    directory_descriptor,
                    STATE_REPLACE_BACKUP_NAME,
                    directory_descriptor,
                    STATE_FILE_NAME,
                )
            except FileExistsError:
                pass
            raise InvariantRefusalError(
                "state file identity changed while locked"
            )
        backup_payload, backup = _read_private_named_state_file_at(
            directory_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            "state replacement backup",
        )
        if (
            not _same_file_after_rename(expected_identity, backup)
            or backup_payload != predecessor_payload
        ):
            raise InvariantRefusalError(
                "state replacement backup does not match transaction intent"
            )
        current_intent = _stat_private_named_state_file(
            directory_descriptor,
            STATE_REPLACE_INTENT_NAME,
            "state replacement intent",
        )
        if _stat_identity(current_intent) != intent_identity:
            raise InvariantRefusalError(
                "state replacement intent changed while locked"
            )

        try:
            _rename_noreplace_at(
                directory_descriptor,
                temporary,
                directory_descriptor,
                STATE_FILE_NAME,
            )
        except FileExistsError as error:
            # Preserve any raced-in target. Restoring the old state is only
            # attempted if the target name is still absent.
            try:
                _rename_noreplace_at(
                    directory_descriptor,
                    STATE_REPLACE_BACKUP_NAME,
                    directory_descriptor,
                    STATE_FILE_NAME,
                )
            except FileExistsError:
                pass
            raise InvariantRefusalError(
                "state file changed during replacement"
            ) from error

        published_payload, published = _read_private_named_state_file_at(
            directory_descriptor, STATE_FILE_NAME
        )
        if (
            not _same_file_after_rename(temporary_identity, published)
            or published_payload != payload
        ):
            raise InvariantRefusalError(
                "state file identity changed during replacement"
            )
        _fsync_directory_descriptor(directory_descriptor)

        backup_payload, backup = _read_private_named_state_file_at(
            directory_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            "state replacement backup",
        )
        if (
            backup_payload != predecessor_payload
            or _payload_sha256(backup_payload)
            != intent.predecessor_payload_sha256
        ):
            raise InvariantRefusalError(
                "state replacement backup identity changed"
            )
        _remove_exact_private_file_at(
            directory_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            _stat_identity(backup),
            predecessor_payload,
            "state replacement backup",
        )
        _fsync_directory_descriptor(directory_descriptor)

        observed_intent, observed_intent_payload, observed_intent_named = (
            _read_state_replace_intent_at(directory_descriptor)
        )
        if (
            observed_intent != intent
            or observed_intent_payload != intent_payload
            or _stat_identity(observed_intent_named) != intent_identity
        ):
            raise InvariantRefusalError(
                "state replacement intent changed while locked"
            )
        _remove_exact_private_file_at(
            directory_descriptor,
            STATE_REPLACE_INTENT_NAME,
            intent_identity,
            intent_payload,
            "state replacement intent",
        )
        _fsync_directory_descriptor(directory_descriptor)
    finally:
        try:
            _unlink_bound_temporary_at(
                directory_descriptor, temporary, temporary_identity
            )
        finally:
            os.close(temporary_descriptor)
    replaced_payload, replaced = _read_private_named_state_file_at(
        directory_descriptor, STATE_FILE_NAME
    )
    if (
        replaced_payload != payload
        or not _same_file_after_rename(temporary_identity, replaced)
    ):
        raise InvariantRefusalError(
            "state file identity changed during replacement"
        )
    if replaced.st_nlink != 1:
        raise InvariantRefusalError("unsafe state file")
    return replaced


class LabStateStore:
    """Root-scoped state store.  All I/O requires :meth:`locked`."""

    def __init__(
        self,
        root: Union[str, os.PathLike],
        execution_profile: object = FIXTURE_EXECUTION_PROFILE,
    ) -> None:
        self.root = Path(root)
        if (
            not self.root.is_absolute()
            or os.path.normpath(str(self.root)) != str(self.root)
        ):
            raise UsageStateError("state root must be absolute")
        self.execution_profile = _validate_execution_profile(execution_profile)

    @classmethod
    def for_repository(
        cls,
        repository_root: Union[str, os.PathLike],
        state_root: Optional[Union[str, os.PathLike]] = None,
    ) -> "LabStateStore":
        repository = Path(repository_root)
        if not repository.is_absolute() or not repository.is_dir() or repository.is_symlink():
            raise UsageStateError("invalid repository root")
        root = repository / ".unified-ext-lab-state" if state_root is None else Path(state_root)
        return cls(root, FIXTURE_EXECUTION_PROFILE)

    @contextlib.contextmanager
    def locked(self, lab_id: object) -> Iterator["LockedLabStateStore"]:
        ensure_process_lock_supported()
        lab = validate_lab_id(lab_id)
        _ensure_private_directory(self.root, create=True)
        lab_root = self.root / lab
        root_descriptor = _open_private_directory(self.root)
        name_lock_name = ROOT_LOCK_FILE_PREFIX + lab
        name_lock_descriptor = None
        name_lock_locked = False
        lab_descriptor = None
        lab_descriptor_locked = False
        legacy_descriptor = None
        legacy_descriptor_locked = False
        try:
            _check_directory_descriptor_path(root_descriptor, self.root)
            # The primary lock name lives in the pinned root, so replacing the
            # per-lab directory cannot create a second cooperating context.
            name_lock_descriptor = _open_private_lock_file(
                root_descriptor,
                name_lock_name,
                "lab name lock",
            )
            _fcntl.flock(name_lock_descriptor, _fcntl.LOCK_EX)
            name_lock_locked = True
            _check_directory_descriptor_path(root_descriptor, self.root)
            _check_lock_descriptor_path(
                name_lock_descriptor,
                root_descriptor,
                name_lock_name,
                "lab name lock",
            )

            _ensure_private_directory_at(root_descriptor, lab, create=True)
            lab_descriptor = _open_private_directory_at(root_descriptor, lab)
            _fcntl.flock(lab_descriptor, _fcntl.LOCK_EX)
            lab_descriptor_locked = True
            _check_directory_descriptor_entry(
                lab_descriptor, root_descriptor, lab
            )

            # Preserve compatibility with already-running schema-2 stores that
            # know only the directory and in-directory state.lock locks.
            legacy_descriptor = _open_private_lock_file(lab_descriptor)
            _fcntl.flock(legacy_descriptor, _fcntl.LOCK_EX)
            legacy_descriptor_locked = True
            _check_lock_descriptor_path(legacy_descriptor, lab_descriptor)
            locked = LockedLabStateStore(
                lab_root,
                lab,
                self.execution_profile,
                root_path=self.root,
                root_descriptor=root_descriptor,
                lab_descriptor=lab_descriptor,
                name_lock_descriptor=name_lock_descriptor,
                name_lock_name=name_lock_name,
                legacy_lock_descriptor=legacy_descriptor,
                capability=_LOCK_CAPABILITY,
            )
            try:
                yield locked
            finally:
                locked.invalidate()
        finally:
            try:
                if legacy_descriptor is not None:
                    _unlock_and_close(
                        legacy_descriptor, legacy_descriptor_locked
                    )
            finally:
                try:
                    if lab_descriptor is not None:
                        _unlock_and_close(
                            lab_descriptor, lab_descriptor_locked
                        )
                finally:
                    try:
                        if name_lock_descriptor is not None:
                            _unlock_and_close(
                                name_lock_descriptor, name_lock_locked
                            )
                    finally:
                        os.close(root_descriptor)


class LockedLabStateStore:
    """Operations available while the per-lab process lock is held."""

    def __init__(
        self,
        root: Path,
        lab_id: str,
        execution_profile: object = FIXTURE_EXECUTION_PROFILE,
        *,
        root_path: Optional[Path] = None,
        root_descriptor: Optional[int] = None,
        lab_descriptor: Optional[int] = None,
        name_lock_descriptor: Optional[int] = None,
        name_lock_name: Optional[str] = None,
        legacy_lock_descriptor: Optional[int] = None,
        capability: object = None,
    ) -> None:
        if (
            capability is not _LOCK_CAPABILITY
            or type(root_descriptor) is not int
            or type(lab_descriptor) is not int
            or type(name_lock_descriptor) is not int
            or type(legacy_lock_descriptor) is not int
            or type(name_lock_name) is not str
            or not isinstance(root_path, Path)
        ):
            raise UsageStateError("locked state store requires an active process lock")
        self.root = root
        self.root_path = root_path
        self.lab_id = lab_id
        self.execution_profile = _validate_execution_profile(execution_profile)
        self.path = root / STATE_FILE_NAME
        self._root_descriptor = root_descriptor
        self._lab_descriptor = lab_descriptor
        self._name_lock_descriptor = name_lock_descriptor
        self._name_lock_name = name_lock_name
        self._legacy_lock_descriptor = legacy_lock_descriptor
        self._state_identity: Optional[_FileIdentity] = None
        self._cached: Optional[LabState] = None
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise UsageStateError("state lock is no longer active")
        _check_directory_descriptor_path(
            self._root_descriptor, self.root_path
        )
        _check_lock_descriptor_path(
            self._name_lock_descriptor,
            self._root_descriptor,
            self._name_lock_name,
            "lab name lock",
        )
        _check_directory_descriptor_entry(
            self._lab_descriptor,
            self._root_descriptor,
            self.lab_id,
        )
        _check_lock_descriptor_path(
            self._legacy_lock_descriptor, self._lab_descriptor
        )

    def invalidate(self) -> None:
        self._active = False
        self._cached = None
        self._state_identity = None

    def _recover_atomic_replacement(self) -> None:
        """Finish or roll back an interrupted no-replace state transaction."""

        _restore_interrupted_private_removal_at(
            self._lab_descriptor,
            STATE_REPLACE_INTENT_NAME,
            STATE_REPLACE_INTENT_REMOVING_NAME,
            "state replacement intent",
        )
        _restore_interrupted_private_removal_at(
            self._lab_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            STATE_REPLACE_BACKUP_REMOVING_NAME,
            "state replacement backup",
        )
        try:
            intent, intent_payload, intent_named = _read_state_replace_intent_at(
                self._lab_descriptor
            )
        except FileNotFoundError:
            try:
                _stat_private_named_state_file(
                    self._lab_descriptor,
                    STATE_REPLACE_BACKUP_NAME,
                    "state replacement backup",
                )
            except FileNotFoundError:
                return
            raise InvariantRefusalError(
                "unfinished state replacement is missing transaction intent"
            )

        if (
            intent.lab_id != self.lab_id
            or intent.execution_profile != self.execution_profile
        ):
            raise InvariantRefusalError(
                "state replacement intent does not match locked state"
            )
        intent_identity = _stat_identity(intent_named)

        def require_unchanged_intent() -> None:
            observed, observed_payload, observed_named = (
                _read_state_replace_intent_at(self._lab_descriptor)
            )
            if (
                observed != intent
                or observed_payload != intent_payload
                or _stat_identity(observed_named) != intent_identity
            ):
                raise InvariantRefusalError(
                    "state replacement intent changed during recovery"
                )

        def validate_bound_state(
            payload: bytes,
            digest: str,
            revision: int,
            field: str,
        ) -> LabState:
            if _payload_sha256(payload) != digest:
                raise InvariantRefusalError(
                    "{} does not match transaction intent".format(field)
                )
            try:
                state = _state_from_transaction_payload(payload)
            except UsageStateError as error:
                raise InvariantRefusalError(
                    "{} is invalid".format(field)
                ) from error
            if (
                state.lab_id != self.lab_id
                or state.execution_profile != self.execution_profile
                or state.revision != revision
            ):
                raise InvariantRefusalError(
                    "{} does not match transaction intent".format(field)
                )
            return state

        try:
            backup_payload, backup_named = _read_private_named_state_file_at(
                self._lab_descriptor,
                STATE_REPLACE_BACKUP_NAME,
                "state replacement backup",
            )
        except FileNotFoundError:
            backup_payload = None
            backup_named = None

        try:
            current_payload, current_named = _read_private_named_state_file_at(
                self._lab_descriptor, STATE_FILE_NAME
            )
        except FileNotFoundError:
            current_payload = None
            current_named = None

        require_unchanged_intent()

        if backup_payload is None:
            if current_payload is None:
                raise InvariantRefusalError(
                    "state replacement intent has no bound state files"
                )
            current_digest = _payload_sha256(current_payload)
            if current_digest == intent.predecessor_payload_sha256:
                validate_bound_state(
                    current_payload,
                    intent.predecessor_payload_sha256,
                    intent.predecessor_revision,
                    "state replacement predecessor",
                )
                if _stat_identity(current_named) != tuple(
                    intent.predecessor_file_identity
                ):
                    raise InvariantRefusalError(
                        "state replacement predecessor identity changed"
                    )
            elif current_digest == intent.successor_payload_sha256:
                validate_bound_state(
                    current_payload,
                    intent.successor_payload_sha256,
                    intent.successor_revision,
                    "state replacement successor",
                )
            else:
                raise InvariantRefusalError(
                    "state file does not match transaction intent"
                )
            require_unchanged_intent()
            _remove_exact_private_file_at(
                self._lab_descriptor,
                STATE_REPLACE_INTENT_NAME,
                intent_identity,
                intent_payload,
                "state replacement intent",
            )
            _fsync_directory_descriptor(self._lab_descriptor)
            return

        validate_bound_state(
            backup_payload,
            intent.predecessor_payload_sha256,
            intent.predecessor_revision,
            "state replacement backup",
        )
        if not _same_file_after_rename(
            tuple(intent.predecessor_file_identity), backup_named
        ):
            raise InvariantRefusalError(
                "state replacement backup identity changed"
            )

        if current_payload is None:
            # The process stopped after moving the exact predecessor aside but
            # before publishing the successor. Restore only that bound inode.
            backup_identity = _stat_identity(backup_named)
            try:
                _rename_noreplace_at(
                    self._lab_descriptor,
                    STATE_REPLACE_BACKUP_NAME,
                    self._lab_descriptor,
                    STATE_FILE_NAME,
                )
            except FileExistsError as error:
                raise InvariantRefusalError(
                    "state file changed during replacement recovery"
                ) from error
            restored_payload, restored = _read_private_named_state_file_at(
                self._lab_descriptor, STATE_FILE_NAME
            )
            if (
                restored_payload != backup_payload
                or not _same_file_after_rename(backup_identity, restored)
            ):
                raise InvariantRefusalError(
                    "state file identity changed during replacement recovery"
                )
            _fsync_directory_descriptor(self._lab_descriptor)
            require_unchanged_intent()
            _remove_exact_private_file_at(
                self._lab_descriptor,
                STATE_REPLACE_INTENT_NAME,
                intent_identity,
                intent_payload,
                "state replacement intent",
            )
            _fsync_directory_descriptor(self._lab_descriptor)
            return

        validate_bound_state(
            current_payload,
            intent.successor_payload_sha256,
            intent.successor_revision,
            "state replacement successor",
        )
        require_unchanged_intent()
        _remove_exact_private_file_at(
            self._lab_descriptor,
            STATE_REPLACE_BACKUP_NAME,
            _stat_identity(backup_named),
            backup_payload,
            "state replacement backup",
        )
        _fsync_directory_descriptor(self._lab_descriptor)
        require_unchanged_intent()
        _remove_exact_private_file_at(
            self._lab_descriptor,
            STATE_REPLACE_INTENT_NAME,
            intent_identity,
            intent_payload,
            "state replacement intent",
        )
        _fsync_directory_descriptor(self._lab_descriptor)

    def create_initial(
        self,
        provider_id: object,
        ownership_token: object,
        planned_resources: Sequence[Union[PlannedResource, Mapping[str, object]]],
        baseline_equalities: Optional[Mapping[str, bool]] = None,
        artifact_evidence: Optional[Mapping[str, object]] = None,
    ) -> LabState:
        self._require_active()
        state = LabState.initial(
            self.lab_id,
            provider_id,
            ownership_token,
            planned_resources,
            baseline_equalities,
            self.execution_profile,
            artifact_evidence,
        )
        self._create(state)
        return state

    def load(self) -> LabState:
        self._require_active()
        if self._cached is not None:
            return self._cached
        self._recover_atomic_replacement()
        payload, final_named = _read_private_named_state_file_at(
            self._lab_descriptor, STATE_FILE_NAME
        )
        self._require_active()
        self._state_identity = _stat_identity(final_named)
        document = strict_json_loads(payload)
        legacy_fixture = (
            isinstance(document, Mapping)
            and document.get("schema") == LEGACY_FIXTURE_STATE_SCHEMA
        )
        early_schema_v3 = (
            isinstance(document, Mapping)
            and document.get("schema") == STATE_SCHEMA
            and "resource_ids" not in document
            and "artifact_evidence" not in document
        )
        state = LabState.from_dict(document)
        if state.lab_id != self.lab_id:
            raise InvariantRefusalError("state lab does not match directory")
        if state.execution_profile != self.execution_profile:
            raise InvariantRefusalError("state execution profile mismatch")
        recovered = recover_interrupted_state(state)
        if recovered is not state:
            self._write(recovered)
            return recovered
        if legacy_fixture or early_schema_v3:
            # Commit the explicit fixture profile migration while the same
            # private per-lab lock that authorized the read is still held.
            self._write(state)
            return state
        self._cached = state
        return state

    def transition(
        self,
        expected: Union[StatePhase, str],
        new: Union[StatePhase, str],
        **safe_updates: object
    ) -> LabState:
        self._require_active()
        current = self.load()
        if current.phase is not _phase(expected):
            raise UsageStateError("state phase changed")
        updated = current.transition(new, **safe_updates)
        self._write(updated)
        return updated

    def mark_tainted(self, expected: Union[StatePhase, str]) -> LabState:
        """Durably enter the irreversible verification/promotion hold."""

        self._require_active()
        current = self.load()
        if current.phase is not _phase(expected):
            raise UsageStateError("state phase changed")
        updated = current.mark_tainted()
        if updated is not current:
            self._write(updated)
        return updated

    def record_owned_role(
        self,
        expected: Union[StatePhase, str],
        role: Union[ResourceRole, str],
    ) -> LabState:
        """Persist one exact resource identity after validation, before reuse/removal."""

        self._require_active()
        current = self.load()
        if current.phase is not _phase(expected):
            raise UsageStateError("state phase changed")
        updated = current.record_owned_role(role)
        if updated is not current:
            self._write(updated)
        return updated

    def record_resource_id(
        self,
        expected: Union[StatePhase, str],
        role: Union[ResourceRole, str],
        resource_id: object,
    ) -> LabState:
        """Persist one immutable image or container daemon identity."""

        self._require_active()
        current = self.load()
        if current.phase is not _phase(expected):
            raise UsageStateError("state phase changed")
        updated = current.record_resource_id(role, resource_id)
        if updated is not current:
            self._write(updated)
        return updated

    def record_removed_role(
        self,
        expected: Union[StatePhase, str],
        role: Union[ResourceRole, str],
    ) -> LabState:
        """Persist one exact successful or restart-confirmed removal."""

        self._require_active()
        current = self.load()
        if current.phase is not _phase(expected):
            raise UsageStateError("state phase changed")
        updated = current.record_removed_role(role)
        if updated is not current:
            self._write(updated)
        return updated

    def fail_pending(
        self,
        expected: Union[StatePhase, str],
        observation: OperationObservation,
    ) -> LabState:
        """Durably record an in-process side-effect failure as recovery."""

        self._require_active()
        current = self.load()
        expected_phase = _phase(expected)
        if current.phase is not expected_phase or expected_phase not in PENDING_STEPS:
            raise UsageStateError("state phase changed or is not pending")
        updated = current.fail_pending(observation)
        self._write(updated)
        return updated

    def interrupt_stable_forward(
        self, expected: Union[StatePhase, str]
    ) -> LabState:
        """Explicitly persist stable-forward interruption as cleanup recovery."""

        self._require_active()
        current = self.load()
        expected_phase = _phase(expected)
        if (
            current.phase is not expected_phase
            or expected_phase not in STABLE_FORWARD_STEPS
        ):
            raise UsageStateError(
                "state phase changed or is not stable forward"
            )
        updated = current.interrupt_stable_forward()
        self._write(updated)
        return updated

    def _create(self, state: LabState) -> None:
        self._require_active()
        self._recover_atomic_replacement()
        created = _atomic_create_at(
            self._lab_descriptor, canonical_json_bytes(state.to_dict())
        )
        self._require_active()
        self._state_identity = _stat_identity(created)
        self._cached = state

    def _write(self, state: LabState) -> None:
        self._require_active()
        if self._state_identity is None:
            raise UsageStateError("state must be loaded before replacement")
        replaced = _atomic_replace_at(
            self._lab_descriptor,
            canonical_json_bytes(state.to_dict()),
            self._state_identity,
        )
        self._require_active()
        self._state_identity = _stat_identity(replaced)
        self._cached = state


# Conservative compatibility names for callers that prefer lifecycle nouns.
LabPhase = StatePhase
StateStore = LabStateStore
