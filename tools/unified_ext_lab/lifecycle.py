"""Durable orchestration for the scaffold-only extension lab.

This layer is intentionally runner-agnostic. Tests inject the stateful fake
Docker runner; a later opt-in gate may inject the identity-bound subprocess
runner. Merely importing or constructing this class executes nothing.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .docker import (
    GuestAction,
    DockerCommandBuilder,
    DockerLabSpec,
    validate_base_image_inspect,
    validate_inspect,
)
from .errors import (
    CleanupIncompleteError,
    InvariantRefusalError,
    LabError,
    RunnerFailureError,
    TestFailureError,
    UnsupportedError,
    UsageStateError,
)
from .evidence import (
    ArtifactEvidence,
    CleanupEvidence,
    FixturePlatform,
    SchemaHashes,
    build_manifest,
    canonical_evidence_bytes,
    capture_draft,
    reconcile_manifest_output,
)
from .model import ResourceRole
from .runner import CommandResult, DEFAULT_MAX_OUTPUT_BYTES, Runner
from .state import (
    LabState,
    LabStateStore,
    OperationObservation,
    PlannedResource,
    SealIntent,
    StatePhase,
)


_CLEANUP_ROLES = (
    ResourceRole.CONTAINER,
    ResourceRole.AUTH,
    ResourceRole.TOOL,
    ResourceRole.WORKSPACE,
    ResourceRole.IMAGE,
)

# These are hashes of schemas, not command output, prompts, or responses.
_MANIFEST_SCHEMA_DESCRIPTOR = (
    b"unified-ext-lab/evidence/v1:artifact,captured_at_ns,cleanup,"
    b"evidence_kind,executor_kind,lab_id,manifest_schema_sha256,operations,"
    b"observed_protocol_schema_sha256,promotion_eligible,provider_id,result,schema"
)
_OBSERVED_PROTOCOL_SCHEMA_DESCRIPTOR = (
    b"synthetic-cli-fixture/v1:artifact:string,marker:boolean,protocol:integer,"
    b"status:string,version:string"
)
_MANIFEST_SCHEMA_SHA256 = hashlib.sha256(_MANIFEST_SCHEMA_DESCRIPTOR).hexdigest()
_OBSERVED_PROTOCOL_SCHEMA_SHA256 = hashlib.sha256(
    _OBSERVED_PROTOCOL_SCHEMA_DESCRIPTOR
).hexdigest()

_ALLOWED_PHASES_FOR_LOGOUT = frozenset(
    (StatePhase.EVIDENCE_CAPTURED, StatePhase.RECOVERY_REQUIRED)
)
_ALLOWED_PHASES_FOR_DESTROY = frozenset(
    (
        StatePhase.LOGOUT_DONE,
        StatePhase.LOGOUT_FAILED,
        StatePhase.RECOVERY_REQUIRED,
        StatePhase.DIRTY,
    )
)
_ALLOWED_PHASES_FOR_VERIFY = frozenset(
    (
        StatePhase.DESTROY_DONE,
        StatePhase.DESTROY_FAILED,
        StatePhase.RECOVERY_REQUIRED,
        StatePhase.DIRTY,
    )
)


@dataclass(frozen=True)
class CleanupSummary:
    """Non-sensitive cleanup counts returned to a command layer."""

    removed_count: int
    remaining_count: Optional[int]


def _error_code(error: LabError) -> str:
    if isinstance(error, CleanupIncompleteError):
        return "cleanup_incomplete"
    if isinstance(error, TestFailureError):
        return "test_failure"
    if isinstance(error, RunnerFailureError):
        if str(error) == "runner timed out":
            return "timeout"
        return "runner_failure"
    if isinstance(error, UnsupportedError):
        return "unsupported"
    if isinstance(error, UsageStateError):
        return "usage_state"
    if isinstance(error, InvariantRefusalError):
        return "invariant_refusal"
    return "invariant_refusal"


class FixtureLifecycle:
    """Coordinate one exact fixture lab through durable lifecycle states."""

    def __init__(
        self,
        store: LabStateStore,
        spec: DockerLabSpec,
        runner: Runner,
        *,
        timeout: float = 30.0,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        evidence_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if type(store) is not LabStateStore:
            raise UsageStateError("invalid lifecycle state store")
        if type(spec) is not DockerLabSpec:
            raise UsageStateError("invalid lifecycle Docker spec")
        if not hasattr(runner, "run"):
            raise UsageStateError("invalid lifecycle runner")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise UsageStateError("invalid lifecycle timeout")
        if not callable(monotonic_ns) or not callable(evidence_clock_ns):
            raise UsageStateError("invalid lifecycle clock")
        self._store = store
        self._spec = spec
        self._runner = runner
        self._commands = DockerCommandBuilder(spec)
        self._timeout = float(timeout)
        self._monotonic_ns = monotonic_ns
        self._evidence_clock_ns = evidence_clock_ns

    @property
    def spec(self) -> DockerLabSpec:
        return self._spec

    def _now(self) -> int:
        value = self._monotonic_ns()
        if type(value) is not int or value < 0:
            raise UsageStateError("invalid monotonic clock result")
        return value

    def _observation(
        self,
        step: str,
        started_ns: int,
        *,
        outcome: str = "succeeded",
        error: Optional[LabError] = None,
    ) -> OperationObservation:
        ended_ns = self._now()
        latency = max(0, ended_ns - started_ns)
        if error is None:
            return OperationObservation(
                step=step,
                outcome=outcome,
                exit_code=0,
                latency_ns=latency,
                error_code="none",
            )
        return OperationObservation(
            step=step,
            outcome="failed",
            exit_code=error.exit_code,
            latency_ns=latency,
            error_code=_error_code(error),
        )

    def _execute(self, argv: Tuple[str, ...]) -> CommandResult:
        result = self._runner.run(argv, timeout=self._timeout)
        if type(result) is not CommandResult:
            raise RunnerFailureError("runner returned an invalid result")
        if result.argv != argv or result.returncode != 0:
            raise RunnerFailureError("runner returned an invalid result")
        encoded_size = len(result.stdout.encode("utf-8")) + len(
            result.stderr.encode("utf-8")
        )
        if encoded_size > DEFAULT_MAX_OUTPUT_BYTES * 2:
            raise RunnerFailureError("runner output exceeded lifecycle limit")
        return result

    def _planned_resources(self) -> Tuple[PlannedResource, ...]:
        return tuple(
            PlannedResource.from_value(resource)
            for resource in self._spec.resources.resources
        )

    def _require_identity(self, state: LabState) -> None:
        identity = self._spec.identity
        if (
            state.lab_id != identity.lab_id
            or state.provider_id != identity.provider_id
            or state.ownership_token != identity.ownership_token
            or state.planned_resources != self._planned_resources()
        ):
            raise InvariantRefusalError("lifecycle state identity mismatch")

    @staticmethod
    def _append(
        state: LabState, observation: OperationObservation
    ) -> Tuple[OperationObservation, ...]:
        return state.operations + (observation,)

    def _inspect(self, role: ResourceRole) -> None:
        result = self._execute(self._commands.inspect(role))
        validate_inspect(self._spec, role, result.stdout)

    def _listing_count(self, argv: Tuple[str, ...]) -> int:
        result = self._execute(argv)
        if not result.stdout:
            return 0
        lines = result.stdout.splitlines()
        if not lines or any(not line or len(line) > 256 for line in lines):
            raise InvariantRefusalError("invalid owned-resource listing")
        if len(lines) > 1:
            raise InvariantRefusalError("multiple resources share an exact ownership set")
        return 1

    def _owned_count(self, role: ResourceRole) -> int:
        owned = self._listing_count(self._commands.list_owned(role))
        named = self._listing_count(self._commands.list_named(role))
        if owned != named:
            raise InvariantRefusalError("resource name and ownership labels disagree")
        return owned

    def _require_absent(self, role: ResourceRole) -> None:
        if self._owned_count(role) != 0:
            raise InvariantRefusalError(
                "planned resource already exists before creation"
            )

    @staticmethod
    def _require_mapping(
        result: CommandResult,
        expected: Mapping[str, object],
        *,
        test_result: bool = False,
    ) -> None:
        import json

        try:
            value = json.loads(
                result.stdout,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=lambda pairs: FixtureLifecycle._unique_pairs(pairs),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            failure = TestFailureError if test_result else RunnerFailureError
            raise failure("synthetic fixture returned an invalid result") from error
        if type(value) is not dict or value != dict(expected):
            failure = TestFailureError if test_result else RunnerFailureError
            raise failure("synthetic fixture returned an unexpected result")

    @staticmethod
    def _unique_pairs(pairs: Sequence[Tuple[str, object]]) -> Mapping[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def status(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            state = locked.load()
            self._require_identity(state)
            return state

    def create(
        self, baseline_equalities: Optional[Mapping[str, bool]] = None
    ) -> LabState:
        identity = self._spec.identity
        with self._store.locked(identity.lab_id) as locked:
            state = locked.create_initial(
                identity.provider_id,
                identity.ownership_token,
                self._planned_resources(),
                baseline_equalities,
            )
            self._require_identity(state)
            pending = locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            started = self._now()
            try:
                base = self._execute(self._commands.inspect_base_image())
                validate_base_image_inspect(self._spec, base.stdout)
                for role in _CLEANUP_ROLES:
                    self._require_absent(role)
                self._execute(self._commands.build_image())
                self._inspect(ResourceRole.IMAGE)
                pending = locked.record_owned_role(
                    StatePhase.CREATE_PENDING, ResourceRole.IMAGE
                )
                for role in (
                    ResourceRole.WORKSPACE,
                    ResourceRole.AUTH,
                    ResourceRole.TOOL,
                ):
                    self._execute(self._commands.create_volume(role))
                    self._inspect(role)
                    pending = locked.record_owned_role(
                        StatePhase.CREATE_PENDING, role
                    )
                self._execute(self._commands.create_container())
                self._inspect(ResourceRole.CONTAINER)
                pending = locked.record_owned_role(
                    StatePhase.CREATE_PENDING, ResourceRole.CONTAINER
                )
                self._execute(self._commands.start_container())
            except LabError as error:
                locked.fail_pending(
                    StatePhase.CREATE_PENDING,
                    self._observation("create", started, error=error),
                )
                raise
            observation = self._observation("create", started)
            return locked.transition(
                StatePhase.CREATE_PENDING,
                StatePhase.CREATED,
                operations=self._append(pending, observation),
            )

    def install(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.CREATED, StatePhase.INSTALL_PENDING
            )
            started = self._now()
            try:
                result = self._execute(
                    self._commands.exec_guest(GuestAction.INSTALL)
                )
                self._require_mapping(
                    result, {"action": "install", "status": "ok"}
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.INSTALL_PENDING,
                    self._observation("install", started, error=error),
                )
                raise
            observation = self._observation("install", started)
            return locked.transition(
                StatePhase.INSTALL_PENDING,
                StatePhase.INSTALLED,
                generation=pending.generation + 1,
                operations=self._append(pending, observation),
            )

    def test(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(StatePhase.INSTALLED, StatePhase.TEST_PENDING)
            started = self._now()
            try:
                result = self._execute(self._commands.exec_guest(GuestAction.TEST))
                self._require_mapping(
                    result,
                    {
                        "artifact": "synthetic-cli-fixture",
                        "marker": True,
                        "protocol": 1,
                        "status": "ok",
                        "version": self._spec.fixture.version,
                    },
                    test_result=True,
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.TEST_PENDING,
                    self._observation("test", started, error=error),
                )
                raise
            observation = self._observation("test", started)
            return locked.transition(
                StatePhase.TEST_PENDING,
                StatePhase.TESTED,
                operations=self._append(pending, observation),
            )

    def _artifact(self) -> ArtifactEvidence:
        fixture = self._spec.fixture
        return ArtifactEvidence(
            package="synthetic-cli-fixture",
            version=fixture.version,
            source_kind="local_fixture",
            source_locator="fixtures/synthetic-cli-fixture-{}-{}".format(
                fixture.version, fixture.sha256[:12]
            ),
            sha256=fixture.sha256,
        )

    @staticmethod
    def _schema_hashes() -> SchemaHashes:
        return SchemaHashes(
            manifest_schema_sha256=_MANIFEST_SCHEMA_SHA256,
            observed_protocol_schema_sha256=_OBSERVED_PROTOCOL_SCHEMA_SHA256,
        )

    def evidence(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.TESTED, StatePhase.EVIDENCE_PENDING
            )
            started = self._now()
            try:
                observation = self._observation("evidence", started)
                operations = self._append(pending, observation)
                captured_state = replace(pending, operations=operations)
                draft = capture_draft(
                    captured_state,
                    self._artifact(),
                    FixturePlatform(),
                    self._schema_hashes(),
                    clock_ns=self._evidence_clock_ns,
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.EVIDENCE_PENDING,
                    self._observation("evidence", started, error=error),
                )
                raise
            return locked.transition(
                StatePhase.EVIDENCE_PENDING,
                StatePhase.EVIDENCE_CAPTURED,
                operations=operations,
                draft_evidence=draft,
            )

    def mark_shell_tainted(self) -> LabState:
        """Persist irreversible taint; this scaffold never starts a shell."""

        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            if current.phase in (
                StatePhase.NEW,
                StatePhase.CREATE_PENDING,
                StatePhase.INSTALL_PENDING,
                StatePhase.TEST_PENDING,
                StatePhase.EVIDENCE_PENDING,
                StatePhase.LOGOUT_PENDING,
                StatePhase.DESTROY_PENDING,
                StatePhase.VERIFY_CLEAN_PENDING,
                StatePhase.SEAL_PENDING,
                StatePhase.PASSED,
                StatePhase.FAILED_CLEAN,
            ):
                raise UsageStateError("shell taint is unavailable in this lifecycle phase")
            return locked.mark_tainted(current.phase)

    def logout(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            if current.phase not in _ALLOWED_PHASES_FOR_LOGOUT:
                raise UsageStateError("logout is unavailable in this lifecycle phase")
            pending = locked.transition(current.phase, StatePhase.LOGOUT_PENDING)
            started = self._now()
            try:
                present = self._owned_count(ResourceRole.CONTAINER)
                if present:
                    self._inspect(ResourceRole.CONTAINER)
                    result = self._execute(
                        self._commands.exec_guest(GuestAction.LOGOUT)
                    )
                    self._require_mapping(
                        result, {"action": "logout", "status": "ok"}
                    )
                    observation = self._observation("logout", started)
                else:
                    observation = self._observation(
                        "logout", started, outcome="skipped"
                    )
            except LabError as error:
                observation = self._observation("logout", started, error=error)
                locked.transition(
                    StatePhase.LOGOUT_PENDING,
                    StatePhase.LOGOUT_FAILED,
                    operations=self._append(pending, observation),
                )
                raise
            return locked.transition(
                StatePhase.LOGOUT_PENDING,
                StatePhase.LOGOUT_DONE,
                auth_generation=pending.auth_generation + 1,
                operations=self._append(pending, observation),
            )

    def destroy(self) -> Tuple[LabState, CleanupSummary]:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            if current.phase not in _ALLOWED_PHASES_FOR_DESTROY:
                raise UsageStateError("destroy is unavailable in this lifecycle phase")
            pending = locked.transition(current.phase, StatePhase.DESTROY_PENDING)
            started = self._now()
            failed = False
            for role in _CLEANUP_ROLES:
                try:
                    present = self._owned_count(role)
                except LabError:
                    failed = True
                    continue
                if not present:
                    # A process may have been killed after exact removal but
                    # before its ledger append. Absence reconciles only a role
                    # that was already durably known to have existed.
                    if (
                        role.value in pending.created_roles
                        and role.value not in pending.removed_roles
                    ):
                        pending = locked.record_removed_role(
                            StatePhase.DESTROY_PENDING, role
                        )
                    continue
                try:
                    self._inspect(role)
                except LabError:
                    failed = True
                    continue
                pending = locked.record_owned_role(
                    StatePhase.DESTROY_PENDING, role
                )
                role_failed = False
                if role is ResourceRole.CONTAINER:
                    try:
                        self._execute(self._commands.stop_container())
                    except LabError:
                        role_failed = True
                    command = self._commands.remove_container()
                elif role in (
                    ResourceRole.WORKSPACE,
                    ResourceRole.AUTH,
                    ResourceRole.TOOL,
                ):
                    command = self._commands.remove_volume(role)
                elif role is ResourceRole.IMAGE:
                    command = self._commands.remove_image()
                else:  # pragma: no cover - the cleanup role tuple is closed.
                    raise UsageStateError("resource role cannot be removed")
                try:
                    self._execute(command)
                except LabError:
                    role_failed = True
                else:
                    # Record each success before considering the next role.
                    # A retry can safely reconcile an absent created role.
                    pending = locked.record_removed_role(
                        StatePhase.DESTROY_PENDING, role
                    )
                if role_failed:
                    failed = True
            if failed:
                error = RunnerFailureError("one or more exact removals failed")
                observation = self._observation("destroy", started, error=error)
                state = locked.transition(
                    StatePhase.DESTROY_PENDING,
                    StatePhase.DESTROY_FAILED,
                    operations=self._append(pending, observation),
                )
            else:
                observation = self._observation("destroy", started)
                state = locked.transition(
                    StatePhase.DESTROY_PENDING,
                    StatePhase.DESTROY_DONE,
                    operations=self._append(pending, observation),
                )
            return state, CleanupSummary(
                removed_count=len(state.removed_roles), remaining_count=None
            )

    def verify_clean(self) -> Tuple[LabState, CleanupSummary]:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            if current.phase not in _ALLOWED_PHASES_FOR_VERIFY:
                raise UsageStateError("clean verification is unavailable in this phase")
            pending = locked.transition(
                current.phase, StatePhase.VERIFY_CLEAN_PENDING
            )
            started = self._now()
            remaining = 0
            failed = False
            for role in _CLEANUP_ROLES:
                try:
                    remaining += self._owned_count(role)
                except LabError:
                    failed = True
                    remaining += 1
            ledger_remaining = len(pending.created_roles) - len(pending.removed_roles)
            if failed or remaining or ledger_remaining:
                error = CleanupIncompleteError("owned resources remain")
                observation = self._observation(
                    "verify_clean", started, error=error
                )
                state = locked.transition(
                    StatePhase.VERIFY_CLEAN_PENDING,
                    StatePhase.DIRTY,
                    operations=self._append(pending, observation),
                )
            else:
                observation = self._observation("verify_clean", started)
                state = locked.transition(
                    StatePhase.VERIFY_CLEAN_PENDING,
                    StatePhase.CLEAN_VERIFIED,
                    operations=self._append(pending, observation),
                )
            return state, CleanupSummary(
                removed_count=len(state.removed_roles),
                remaining_count=max(remaining, ledger_remaining),
            )

    @staticmethod
    def _latest_succeeded(state: LabState, step: str) -> bool:
        for observation in reversed(state.operations):
            if observation.step == step:
                return observation.outcome == "succeeded"
        return False

    @staticmethod
    def _output_identity(output_path: Path) -> str:
        value = str(output_path)
        if (
            not output_path.is_absolute()
            or os.path.normpath(value) != value
            or os.path.realpath(value) != value
        ):
            raise UsageStateError("evidence output path must be absolute and canonical")
        return hashlib.sha256(os.fsencode(value)).hexdigest()

    def _cleanup_evidence(self, state: LabState) -> CleanupEvidence:
        created_count = len(state.created_roles)
        removed_count = len(state.removed_roles)
        remaining_count = created_count - removed_count
        if remaining_count:
            raise InvariantRefusalError(
                "durable cleanup ledger is not fully reconciled"
            )
        return CleanupEvidence(
            created_count=created_count,
            removed_count=removed_count,
            remaining_count=remaining_count,
            logout_succeeded=self._latest_succeeded(state, "logout"),
            destroy_succeeded=self._latest_succeeded(state, "destroy"),
            verified_clean=True,
        )

    def _seal_payload(self, state: LabState) -> Tuple[bytes, str]:
        manifest = build_manifest(state, self._cleanup_evidence(state))
        result = manifest["result"]
        if result not in ("passed", "failed_clean"):
            raise InvariantRefusalError("invalid sealed evidence result")
        return canonical_evidence_bytes(manifest), result

    def _reconcile_seal_locked(
        self, locked: object, pending: LabState, output_path: Path
    ) -> LabState:
        if pending.phase is not StatePhase.SEAL_PENDING or pending.seal_intent is None:
            raise UsageStateError("seal reconciliation requires SEAL_PENDING")
        intent = pending.seal_intent
        if self._output_identity(output_path) != intent.output_identity:
            raise InvariantRefusalError("evidence output does not match seal intent")
        payload, result = self._seal_payload(pending)
        if (
            hashlib.sha256(payload).hexdigest() != intent.payload_sha256
            or result != intent.result
        ):
            raise InvariantRefusalError("reconstructed evidence does not match seal intent")
        started = self._now()
        reconcile_manifest_output(output_path, payload)
        observation = self._observation("seal", started)
        target = (
            StatePhase.PASSED if result == "passed" else StatePhase.FAILED_CLEAN
        )
        return locked.transition(
            StatePhase.SEAL_PENDING,
            target,
            operations=self._append(pending, observation),
        )

    def seal(self, output_path: Path) -> LabState:
        if not isinstance(output_path, Path):
            raise UsageStateError("evidence output path must be absolute")
        output_identity = self._output_identity(output_path)
        with self._store.locked(self._spec.identity.lab_id) as locked:
            clean_state = locked.load()
            self._require_identity(clean_state)
            if clean_state.phase is StatePhase.SEAL_PENDING:
                return self._reconcile_seal_locked(
                    locked, clean_state, output_path
                )
            if clean_state.phase is not StatePhase.CLEAN_VERIFIED:
                raise UsageStateError("evidence sealing requires clean verification")

            transition_updates = {}
            if clean_state.draft_evidence is None:
                draft = capture_draft(
                    clean_state,
                    self._artifact(),
                    FixturePlatform(),
                    self._schema_hashes(),
                    clock_ns=self._evidence_clock_ns,
                )
                transition_updates["draft_evidence"] = draft
            # First build the deterministic bytes from an immutable preview.
            # The placeholder digest is not persisted and is excluded from
            # evidence, avoiding a self-referential payload hash.
            preview = clean_state.transition(
                StatePhase.SEAL_PENDING,
                seal_intent=SealIntent(output_identity, "0" * 64, "passed"),
                **transition_updates,
            )
            payload, result = self._seal_payload(preview)
            intent = SealIntent(
                output_identity=output_identity,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                result=result,
            )
            pending = locked.transition(
                StatePhase.CLEAN_VERIFIED,
                StatePhase.SEAL_PENDING,
                seal_intent=intent,
                **transition_updates,
            )
            return self._reconcile_seal_locked(locked, pending, output_path)


__all__ = ["CleanupSummary", "FixtureLifecycle"]
