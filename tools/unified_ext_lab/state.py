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
import json
import os
import secrets
import stat
from dataclasses import dataclass, replace
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


STATE_SCHEMA = 2
MAX_STATE_BYTES = 1024 * 1024
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "state.lock"
_LOCK_CAPABILITY = object()


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


def _validate_draft(value: object) -> Optional[Mapping[str, object]]:
    if value is None:
        return None
    data = _exact_mapping(value, _DRAFT_KEYS, "draft evidence")
    _exact_mapping(data["artifact"], _ARTIFACT_KEYS, "draft artifact")
    if data["evidence_kind"] != "harness_fixture":
        raise UsageStateError("invalid draft evidence kind")
    if data["executor_kind"] != "fake_docker":
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
    """Immutable schema-v2 state; use :meth:`transition` to advance it."""

    schema: int
    revision: int
    lab_id: str
    provider_id: str
    ownership_token: str
    phase: StatePhase
    planned_resources: Tuple[PlannedResource, ...] = ()
    generation: int = 0
    auth_generation: int = 0
    pending_step: Optional[str] = None
    created_roles: Tuple[str, ...] = ()
    removed_roles: Tuple[str, ...] = ()
    tainted: bool = False
    operations: Tuple[OperationObservation, ...] = ()
    draft_evidence: Optional[Mapping[str, object]] = None
    seal_intent: Optional[SealIntent] = None
    baseline_equalities: Mapping[str, bool] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.schema != STATE_SCHEMA:
            raise UsageStateError("unsupported state schema")
        _strict_nonnegative_int(self.revision, "state revision")
        object.__setattr__(self, "lab_id", validate_lab_id(self.lab_id))
        object.__setattr__(self, "provider_id", validate_provider_id(self.provider_id))
        object.__setattr__(self, "ownership_token", validate_ownership_token(self.ownership_token))
        phase = _phase(self.phase)
        object.__setattr__(self, "phase", phase)
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
            planned_resources=resources,
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
            "auth_generation": self.auth_generation,
            "baseline_equalities": dict(self.baseline_equalities),
            "created_roles": list(self.created_roles),
            "draft_evidence": draft,
            "generation": self.generation,
            "lab_id": self.lab_id,
            "operations": [operation.to_dict() for operation in self.operations],
            "ownership_token": self.ownership_token,
            "pending_step": self.pending_step,
            "phase": self.phase.value,
            "planned_resources": [resource.to_dict() for resource in self.planned_resources],
            "provider_id": self.provider_id,
            "removed_roles": list(self.removed_roles),
            "revision": self.revision,
            "schema": self.schema,
            "seal_intent": None if self.seal_intent is None else self.seal_intent.to_dict(),
            "tainted": self.tainted,
        }

    @classmethod
    def from_dict(cls, value: object) -> "LabState":
        keys = frozenset(
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
        data = _exact_mapping(value, keys, "state")
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
            planned_resources=tuple(PlannedResource.from_dict(item) for item in data["planned_resources"]),
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
            raise InvariantRefusalError("tainted state cannot produce evidence")
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
        """Return a same-phase revision that permanently records shell use.

        A command layer must persist this state before launching a shell.  The
        flag is monotonic and there is intentionally no API that clears it.
        """

        if self.phase in (
            StatePhase.SEAL_PENDING,
            StatePhase.PASSED,
            StatePhase.FAILED_CLEAN,
        ):
            raise UsageStateError("sealed state cannot start a shell")
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


def ensure_process_lock_supported() -> None:
    if os.name != "posix" or _fcntl is None:
        raise UnsupportedError("process locking is unsupported on this platform")
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
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
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


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


def _stat_private_lock_file(directory_descriptor: int) -> os.stat_result:
    try:
        named = os.stat(
            LOCK_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise InvariantRefusalError("state lock changed during acquisition") from error
    if stat.S_ISLNK(named.st_mode):
        raise InvariantRefusalError("unsafe state lock")
    _check_private_file_stat(named, "state lock")
    return named


def _check_lock_descriptor_path(
    descriptor: int,
    directory_descriptor: int,
) -> None:
    opened = os.fstat(descriptor)
    _check_private_file_stat(opened, "state lock")
    named = _stat_private_lock_file(directory_descriptor)
    if not _same_inode(opened, named):
        raise InvariantRefusalError("state lock changed during acquisition")


def _open_private_lock_file(directory_descriptor: int) -> int:
    flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        descriptor = os.open(
            LOCK_FILE_NAME,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        # Validate the name before opening as well as the resulting descriptor.
        # O_NONBLOCK prevents a raced-in special file such as a FIFO from
        # stalling acquisition before fstat can reject it.
        _stat_private_lock_file(directory_descriptor)
        try:
            descriptor = os.open(
                LOCK_FILE_NAME,
                flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError as error:
            raise InvariantRefusalError("state lock changed during acquisition") from error
    else:
        created = True
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _check_lock_descriptor_path(descriptor, directory_descriptor)
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
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        _check_private_file(path)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, secrets.token_hex(8)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
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
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class LabStateStore:
    """Root-scoped state store.  All I/O requires :meth:`locked`."""

    def __init__(self, root: Union[str, os.PathLike]) -> None:
        self.root = Path(root)
        if (
            not self.root.is_absolute()
            or os.path.normpath(str(self.root)) != str(self.root)
        ):
            raise UsageStateError("state root must be absolute")

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
        return cls(root)

    @contextlib.contextmanager
    def locked(self, lab_id: object) -> Iterator["LockedLabStateStore"]:
        ensure_process_lock_supported()
        lab = validate_lab_id(lab_id)
        _ensure_private_directory(self.root, create=True)
        lab_root = self.root / lab
        _ensure_private_directory(lab_root, create=True)
        directory_descriptor = _open_private_directory(lab_root)
        directory_locked = False
        descriptor = None
        descriptor_locked = False
        try:
            # The directory lock remains stable if state.lock is renamed.  It
            # serializes new implementations while the file lock preserves
            # compatibility with already-running older implementations.
            _fcntl.flock(directory_descriptor, _fcntl.LOCK_EX)
            directory_locked = True
            _check_directory_descriptor_path(directory_descriptor, lab_root)
            descriptor = _open_private_lock_file(directory_descriptor)
            _fcntl.flock(descriptor, _fcntl.LOCK_EX)
            descriptor_locked = True
            # flock may have blocked while another process renamed state.lock.
            # Never expose a store unless the locked descriptor is still the
            # exact regular, private file currently named by the directory.
            _check_lock_descriptor_path(descriptor, directory_descriptor)
            locked = LockedLabStateStore(lab_root, lab, _LOCK_CAPABILITY)
            try:
                yield locked
            finally:
                locked.invalidate()
        finally:
            try:
                if descriptor is not None:
                    _unlock_and_close(descriptor, descriptor_locked)
            finally:
                _unlock_and_close(directory_descriptor, directory_locked)


class LockedLabStateStore:
    """Operations available while the per-lab process lock is held."""

    def __init__(self, root: Path, lab_id: str, capability: object = None) -> None:
        if capability is not _LOCK_CAPABILITY:
            raise UsageStateError("locked state store requires an active process lock")
        self.root = root
        self.lab_id = lab_id
        self.path = root / STATE_FILE_NAME
        self._cached: Optional[LabState] = None
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise UsageStateError("state lock is no longer active")

    def invalidate(self) -> None:
        self._active = False
        self._cached = None

    def create_initial(
        self,
        provider_id: object,
        ownership_token: object,
        planned_resources: Sequence[Union[PlannedResource, Mapping[str, object]]],
        baseline_equalities: Optional[Mapping[str, bool]] = None,
    ) -> LabState:
        self._require_active()
        if self.path.exists() or self.path.is_symlink():
            raise UsageStateError("lab state already exists")
        state = LabState.initial(
            self.lab_id,
            provider_id,
            ownership_token,
            planned_resources,
            baseline_equalities,
        )
        self._write(state)
        return state

    def load(self) -> LabState:
        self._require_active()
        if self._cached is not None:
            return self._cached
        _check_private_file(self.path)
        descriptor = os.open(str(self.path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks = []
            remaining = MAX_STATE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        state = LabState.from_dict(strict_json_loads(payload))
        if state.lab_id != self.lab_id:
            raise InvariantRefusalError("state lab does not match directory")
        recovered = recover_interrupted_state(state)
        if recovered is not state:
            self._write(recovered)
            return recovered
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
        """Durably taint the current phase before an interactive shell starts."""

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

    def _write(self, state: LabState) -> None:
        self._require_active()
        _atomic_replace(self.path, canonical_json_bytes(state.to_dict()))
        self._cached = state


# Conservative compatibility names for callers that prefer lifecycle nouns.
LabPhase = StatePhase
StateStore = LabStateStore
