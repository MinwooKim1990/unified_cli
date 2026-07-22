"""Durable orchestration for the scaffold-only extension lab.

This layer is intentionally runner-agnostic. Tests inject the stateful fake
Docker runner; a later opt-in gate may inject the identity-bound subprocess
runner. Merely importing or constructing this class executes nothing.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .docker import (
    DockerCleanupSpec,
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
    EXECUTOR_KINDS,
    EXECUTOR_KIND,
    FixturePlatform,
    REAL_EXECUTOR_KIND,
    SchemaHashes,
    build_manifest,
    canonical_evidence_bytes,
    capture_draft,
    reconcile_manifest_output,
)
from .model import ResourceRole
from .runner import CommandResult, DEFAULT_MAX_OUTPUT_BYTES, Runner
from .state import (
    FIXTURE_EXECUTION_PROFILE,
    REAL_DOCKER_EXECUTION_PROFILE,
    LabState,
    LabStateStore,
    OperationObservation,
    PlannedResource,
    SealIntent,
    StatePhase,
)


_DEFAULT_CLEANUP_ROLES = (
    ResourceRole.CONTAINER,
    ResourceRole.AUTH,
    ResourceRole.TOOL,
    ResourceRole.WORKSPACE,
    ResourceRole.IMAGE,
)

_READINESS_TIMEOUT_SECONDS = 5.0
_READINESS_POLL_SECONDS = 0.05

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
        if str(error) in ("runner timed out", "container readiness timed out"):
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
        spec: object,
        runner: Runner,
        *,
        timeout: float = 30.0,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        evidence_clock_ns: Callable[[], int] = time.time_ns,
        readiness_monotonic: Callable[[], float] = time.monotonic,
        readiness_sleep: Callable[[float], None] = time.sleep,
        execution_profile: str = FIXTURE_EXECUTION_PROFILE,
        executor_kind: str = "fake_docker",
        command_builder: Optional[object] = None,
        runtime_snapshot: Optional[object] = None,
    ) -> None:
        if type(store) is not LabStateStore:
            raise UsageStateError("invalid lifecycle state store")
        if type(spec) not in (DockerLabSpec, DockerCleanupSpec):
            raise UsageStateError("invalid lifecycle Docker spec")
        if not hasattr(runner, "run"):
            raise UsageStateError("invalid lifecycle runner")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise UsageStateError("invalid lifecycle timeout")
        if (
            not callable(monotonic_ns)
            or not callable(evidence_clock_ns)
            or not callable(readiness_monotonic)
            or not callable(readiness_sleep)
        ):
            raise UsageStateError("invalid lifecycle clock")
        if store.execution_profile != execution_profile:
            raise InvariantRefusalError("lifecycle execution profile mismatch")
        if executor_kind not in EXECUTOR_KINDS:
            raise UsageStateError("invalid lifecycle executor kind")
        expected_executor = {
            FIXTURE_EXECUTION_PROFILE: EXECUTOR_KIND,
            REAL_DOCKER_EXECUTION_PROFILE: REAL_EXECUTOR_KIND,
        }.get(execution_profile)
        if expected_executor != executor_kind:
            raise InvariantRefusalError("lifecycle executor profile mismatch")
        if (
            command_builder is None
            and execution_profile == REAL_DOCKER_EXECUTION_PROFILE
        ):
            raise InvariantRefusalError(
                "lifecycle command builder profile mismatch"
            )
        commands = (
            DockerCommandBuilder(spec) if command_builder is None else command_builder
        )
        try:
            uses_resource_ids = commands.uses_resource_ids
        except Exception as error:
            raise UsageStateError("invalid lifecycle resource-id policy") from error
        if type(uses_resource_ids) is not bool:
            raise UsageStateError("invalid lifecycle resource-id policy")
        try:
            builds_image = getattr(commands, "builds_image", True)
        except Exception as error:
            raise UsageStateError("invalid lifecycle command policy") from error
        if type(builds_image) is not bool:
            raise UsageStateError("invalid lifecycle command policy")
        expected_resource_ids = execution_profile == REAL_DOCKER_EXECUTION_PROFILE
        if uses_resource_ids is not expected_resource_ids:
            raise InvariantRefusalError("lifecycle command builder profile mismatch")
        if execution_profile == REAL_DOCKER_EXECUTION_PROFILE and (
            runtime_snapshot is None
            or not callable(getattr(runtime_snapshot, "remove", None))
            or not callable(getattr(runtime_snapshot, "present", None))
        ):
            raise InvariantRefusalError("real-Docker snapshot resource is unavailable")
        if execution_profile != REAL_DOCKER_EXECUTION_PROFILE and runtime_snapshot is not None:
            raise InvariantRefusalError("fixture lifecycle cannot own a runtime snapshot")
        self._store = store
        self._spec = spec
        self._runner = runner
        self._commands = commands
        self._builds_image = builds_image
        self._runtime_snapshot = runtime_snapshot
        required_commands = (
            "exec_guest",
            "inspect",
            "list_named",
            "list_owned",
            "remove_container",
            "remove_image",
            "stop_container",
        )
        if type(spec) is DockerLabSpec:
            required_commands += (
                "create_container",
                "inspect_base_image",
                "start_container",
            )
            if builds_image:
                required_commands += ("build_image",)
            if getattr(self._commands, "create_volume_roles", ()):
                required_commands += ("create_volume", "remove_volume")
        if uses_resource_ids:
            required_commands += ("list_identity",)
        if any(not callable(getattr(self._commands, name, None)) for name in required_commands):
            raise UsageStateError("invalid lifecycle command builder")
        self._timeout = float(timeout)
        self._monotonic_ns = monotonic_ns
        self._evidence_clock_ns = evidence_clock_ns
        self._readiness_monotonic = readiness_monotonic
        self._readiness_sleep = readiness_sleep
        self._execution_profile = execution_profile
        self._executor_kind = executor_kind

    @property
    def spec(self) -> object:
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

    def _execute(
        self, argv: Tuple[str, ...], *, timeout: Optional[float] = None
    ) -> CommandResult:
        effective_timeout = self._timeout if timeout is None else timeout
        if (
            type(effective_timeout) not in (int, float)
            or isinstance(effective_timeout, bool)
            or not math.isfinite(effective_timeout)
            or effective_timeout <= 0
        ):
            raise UsageStateError("invalid lifecycle execution timeout")
        result = self._runner.run(argv, timeout=float(effective_timeout))
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
        roles = getattr(self._commands, "planned_roles", None)
        if roles is not None and (
            type(roles) is not tuple
            or not roles
            or any(not isinstance(role, ResourceRole) for role in roles)
            or len(set(roles)) != len(roles)
        ):
            raise UsageStateError("invalid lifecycle planned-resource roles")
        return tuple(
            PlannedResource.from_value(resource)
            for resource in self._spec.resources.resources
            if roles is None or resource.role in roles
        )

    def _cleanup_roles(self) -> Tuple[ResourceRole, ...]:
        roles = getattr(self._commands, "cleanup_roles", _DEFAULT_CLEANUP_ROLES)
        if (
            type(roles) is not tuple
            or not roles
            or any(not isinstance(role, ResourceRole) for role in roles)
            or len(set(roles)) != len(roles)
        ):
            raise UsageStateError("invalid lifecycle cleanup roles")
        return roles

    def _create_volume_roles(self) -> Tuple[ResourceRole, ...]:
        roles = getattr(
            self._commands,
            "create_volume_roles",
            (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL),
        )
        if (
            type(roles) is not tuple
            or any(
                role
                not in (
                    ResourceRole.WORKSPACE,
                    ResourceRole.AUTH,
                    ResourceRole.TOOL,
                )
                for role in roles
            )
            or len(set(roles)) != len(roles)
        ):
            raise UsageStateError("invalid lifecycle volume roles")
        return roles

    def _uses_resource_ids(self) -> bool:
        value = getattr(self._commands, "uses_resource_ids", False)
        if type(value) is not bool:
            raise UsageStateError("invalid lifecycle resource-id policy")
        return value

    def _uses_resource_id(self, role: ResourceRole) -> bool:
        if not self._uses_resource_ids():
            return False
        roles = getattr(self._commands, "resource_id_roles", self._cleanup_roles())
        if (
            type(roles) is not tuple
            or any(not isinstance(item, ResourceRole) for item in roles)
            or len(set(roles)) != len(roles)
        ):
            raise UsageStateError("invalid lifecycle resource-id roles")
        return role in roles

    def _require_forward_spec(self) -> DockerLabSpec:
        if type(self._spec) is not DockerLabSpec:
            raise UsageStateError("forward lifecycle action is unavailable during cleanup")
        return self._spec

    def _require_identity(self, state: LabState) -> None:
        identity = self._spec.identity
        if (
            state.lab_id != identity.lab_id
            or state.provider_id != identity.provider_id
            or state.ownership_token != identity.ownership_token
            or state.planned_resources != self._planned_resources()
            or state.execution_profile != self._execution_profile
        ):
            raise InvariantRefusalError("lifecycle state identity mismatch")

    @staticmethod
    def _append(
        state: LabState, observation: OperationObservation
    ) -> Tuple[OperationObservation, ...]:
        return state.operations + (observation,)

    def _inspect(
        self,
        role: ResourceRole,
        *,
        cleanup: bool = False,
        expected_id: Optional[str] = None,
    ) -> Optional[str]:
        if expected_id is not None:
            if not cleanup or not self._uses_resource_id(role):
                raise UsageStateError("exact-id inspect is cleanup-only")
            result = self._execute(self._commands.inspect(role, expected_id))
            validator = getattr(
                self._commands, "validate_cleanup_identity_inspect", None
            )
            if not callable(validator):
                raise UsageStateError("exact-id cleanup validation is unavailable")
            resource_id = validator(role, result.stdout, expected_id)
            if resource_id != expected_id:
                raise InvariantRefusalError(
                    "Docker inspect returned a different immutable resource id"
                )
            return resource_id

        result = self._execute(self._commands.inspect(role))
        validator = getattr(
            self._commands,
            "validate_cleanup_inspect" if cleanup else "validate_inspect",
            None,
        )
        if cleanup and not callable(validator):
            validator = getattr(self._commands, "validate_inspect", None)
        if callable(validator):
            resource_id = validator(role, result.stdout)
            if self._uses_resource_id(role) and type(resource_id) is not str:
                raise InvariantRefusalError(
                    "inspect did not return immutable resource id"
                )
            return resource_id
        spec = self._require_forward_spec()
        validate_inspect(spec, role, result.stdout)
        return None

    def _resource_target(
        self, state: LabState, role: ResourceRole, observed_id: Optional[str] = None
    ) -> Optional[str]:
        if not self._uses_resource_id(role):
            return None
        recorded = state.resource_ids.get(role.value)
        if recorded is not None and observed_id is not None and recorded != observed_id:
            raise InvariantRefusalError("Docker resource id changed")
        target = recorded if recorded is not None else observed_id
        if type(target) is not str:
            raise InvariantRefusalError("Docker resource id is unavailable")
        return target

    def _guest_command(
        self,
        state: LabState,
        action: GuestAction,
        observed_id: Optional[str] = None,
    ) -> Tuple[str, ...]:
        if self._uses_resource_id(ResourceRole.CONTAINER):
            target = self._resource_target(
                state, ResourceRole.CONTAINER, observed_id
            )
            return self._commands.exec_guest(action, target)
        return self._commands.exec_guest(action)

    def _readiness_now(self) -> float:
        value = self._readiness_monotonic()
        if (
            type(value) not in (int, float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise UsageStateError("invalid readiness clock result")
        return float(value)

    @staticmethod
    def _workspace_is_ready(result: CommandResult) -> bool:
        import json

        try:
            value = json.loads(
                result.stdout,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=lambda pairs: FixtureLifecycle._unique_pairs(pairs),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RunnerFailureError("container readiness returned an invalid result") from error
        if type(value) is not dict or value.get("action") != "ready":
            raise RunnerFailureError("container readiness returned an invalid result")
        if set(value) != {"action", "status"} or value["status"] not in (
            "ready",
            "waiting",
        ):
            raise RunnerFailureError("container readiness returned an invalid result")
        return value["status"] == "ready"

    def _wait_for_workspace_ready(self, state: LabState) -> None:
        """Wait for PID 1's fixed workspace marker before any guest work."""

        if not self._uses_resource_id(ResourceRole.CONTAINER):
            return
        deadline = self._readiness_now() + _READINESS_TIMEOUT_SECONDS
        while True:
            remaining = deadline - self._readiness_now()
            if remaining <= 0:
                raise RunnerFailureError("container readiness timed out")
            result = self._execute(
                self._guest_command(state, GuestAction.READY),
                timeout=min(self._timeout, remaining),
            )
            if self._workspace_is_ready(result):
                return
            remaining = deadline - self._readiness_now()
            if remaining <= 0:
                raise RunnerFailureError("container readiness timed out")
            self._readiness_sleep(min(_READINESS_POLL_SECONDS, remaining))

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

    def _identity_present(self, state: LabState, role: ResourceRole) -> bool:
        if not self._uses_resource_id(role):
            return False
        resource_id = state.resource_ids.get(role.value)
        if resource_id is None:
            return False
        result = self._execute(self._commands.list_identity(role))
        lines = result.stdout.splitlines()
        observed = set()
        for line in lines:
            if role is ResourceRole.IMAGE:
                valid = (
                    len(line) == 71
                    and line.startswith("sha256:")
                    and all(
                        character in "0123456789abcdef"
                        for character in line[7:]
                    )
                )
            elif role is ResourceRole.CONTAINER:
                valid = len(line) == 64 and all(
                    character in "0123456789abcdef" for character in line
                )
            else:
                raise UsageStateError("resource role has no daemon id")
            if not valid:
                raise InvariantRefusalError("invalid Docker identity listing")
            observed.add(line)
        return resource_id in observed

    def _unresolved_create_mutation(self, state: LabState) -> bool:
        """Conservatively recognize a crashed real-Docker mutation window.

        ``CREATE_PENDING`` is durable before any daemon mutation.  A recovery
        state that still identifies ``create`` but lacks either immutable
        daemon ID cannot prove that a failed/disconnected mutation will not
        publish later.  Treating pre-mutation failures the same way is an
        intentional fail-closed false positive.
        """

        if not self._uses_resource_ids() or state.pending_step != "create":
            return False
        return any(
            role.value not in state.resource_ids
            for role in self._cleanup_roles()
            if self._uses_resource_id(role)
        )

    def _persist_mutation_hold(self, locked: object, state: LabState) -> LabState:
        if state.tainted or not self._unresolved_create_mutation(state):
            return state
        return locked.mark_tainted(state.phase)

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

    def _artifact_from_spec(self) -> ArtifactEvidence:
        spec = self._require_forward_spec()
        fixture = spec.fixture
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
    def _artifact(state: LabState) -> ArtifactEvidence:
        return ArtifactEvidence.from_value(state.artifact_evidence)

    def status(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            state = locked.load()
            self._require_identity(state)
            return state

    def bind_runtime_snapshot_intent(self) -> LabState:
        """Atomically bind validated snapshot artifact identity to NEW state."""

        spec = self._require_forward_spec()
        if (
            self._execution_profile != REAL_DOCKER_EXECUTION_PROFILE
            or self._runtime_snapshot is None
            or not self._runtime_snapshot.present()
        ):
            raise UsageStateError("runtime snapshot intent is unavailable")
        artifact = self._artifact_from_spec().to_dict()
        with self._store.locked(spec.identity.lab_id) as locked:
            state = locked.load()
            self._require_identity(state)
            return locked.bind_runtime_snapshot(StatePhase.NEW, artifact)

    def create(
        self, baseline_equalities: Optional[Mapping[str, bool]] = None
    ) -> LabState:
        spec = self._require_forward_spec()
        identity = self._spec.identity
        if self._execution_profile == REAL_DOCKER_EXECUTION_PROFILE:
            if baseline_equalities is not None:
                raise UsageStateError("real-Docker baselines are fixed")
            expected_baselines = {"runtime_snapshot_bound": True}
        else:
            expected_baselines = (
                {} if baseline_equalities is None else dict(baseline_equalities)
            )
        with self._store.locked(identity.lab_id) as locked:
            artifact = self._artifact_from_spec().to_dict()
            try:
                state = locked.load()
            except FileNotFoundError:
                if self._execution_profile == REAL_DOCKER_EXECUTION_PROFILE:
                    raise UsageStateError("real-Docker lifecycle intent is unavailable")
                state = locked.create_initial(
                    identity.provider_id,
                    identity.ownership_token,
                    self._planned_resources(),
                    baseline_equalities,
                    artifact_evidence=artifact,
                )
            else:
                if (
                    state.phase is not StatePhase.NEW
                    or dict(state.artifact_evidence) != artifact
                    or dict(state.baseline_equalities) != expected_baselines
                ):
                    raise UsageStateError("pre-created lifecycle intent is invalid")
            self._require_identity(state)
            pending = locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            started = self._now()
            unresolved_mutation = False
            try:
                for role in self._cleanup_roles():
                    self._require_absent(role)
                base = self._execute(self._commands.inspect_base_image())
                base_validator = getattr(self._commands, "validate_base_image", None)
                if callable(base_validator):
                    base_validator(base.stdout)
                else:
                    validate_base_image_inspect(spec, base.stdout)
                if self._builds_image:
                    unresolved_mutation = self._uses_resource_id(ResourceRole.IMAGE)
                    self._execute(self._commands.build_image())
                    image_id = self._inspect(ResourceRole.IMAGE)
                    if self._uses_resource_id(ResourceRole.IMAGE):
                        pending = locked.record_resource_id(
                            StatePhase.CREATE_PENDING,
                            ResourceRole.IMAGE,
                            image_id,
                        )
                    pending = locked.record_owned_role(
                        StatePhase.CREATE_PENDING, ResourceRole.IMAGE
                    )
                    unresolved_mutation = False
                for role in self._create_volume_roles():
                    self._execute(self._commands.create_volume(role))
                    self._inspect(role)
                    pending = locked.record_owned_role(
                        StatePhase.CREATE_PENDING, role
                    )
                unresolved_mutation = self._uses_resource_id(ResourceRole.CONTAINER)
                self._execute(self._commands.create_container())
                container_id = self._inspect(ResourceRole.CONTAINER)
                if self._uses_resource_id(ResourceRole.CONTAINER):
                    pending = locked.record_resource_id(
                        StatePhase.CREATE_PENDING,
                        ResourceRole.CONTAINER,
                        container_id,
                    )
                pending = locked.record_owned_role(
                    StatePhase.CREATE_PENDING, ResourceRole.CONTAINER
                )
                unresolved_mutation = False
                if self._uses_resource_id(ResourceRole.CONTAINER):
                    target = self._resource_target(
                        pending, ResourceRole.CONTAINER, container_id
                    )
                    self._execute(self._commands.start_container(target))
                else:
                    self._execute(self._commands.start_container())
                self._wait_for_workspace_ready(pending)
            except BaseException as error:
                if unresolved_mutation:
                    pending = locked.mark_tainted(StatePhase.CREATE_PENDING)
                if isinstance(error, LabError):
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
        self._require_forward_spec()
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.CREATED, StatePhase.INSTALL_PENDING
            )
            started = self._now()
            try:
                result = self._execute(self._guest_command(pending, GuestAction.INSTALL))
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
        self._require_forward_spec()
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(StatePhase.INSTALLED, StatePhase.TEST_PENDING)
            started = self._now()
            try:
                result = self._execute(self._guest_command(pending, GuestAction.TEST))
                self._require_mapping(
                    result,
                    {
                        "artifact": "synthetic-cli-fixture",
                        "marker": True,
                        "protocol": 1,
                        "status": "ok",
                        "version": self._artifact(pending).version,
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

    @staticmethod
    def _schema_hashes() -> SchemaHashes:
        return SchemaHashes(
            manifest_schema_sha256=_MANIFEST_SCHEMA_SHA256,
            observed_protocol_schema_sha256=_OBSERVED_PROTOCOL_SCHEMA_SHA256,
        )

    def evidence(self) -> LabState:
        self._require_forward_spec()
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
                    self._artifact(captured_state),
                    FixturePlatform(executor_kind=self._executor_kind),
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
        """Persist an irreversible promotion hold before interactive use."""

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
                raise UsageStateError(
                    "promotion taint is unavailable in this lifecycle phase"
                )
            return locked.mark_tainted(current.phase)

    def logout(self) -> LabState:
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            current = self._persist_mutation_hold(locked, current)
            if current.phase not in _ALLOWED_PHASES_FOR_LOGOUT:
                raise UsageStateError("logout is unavailable in this lifecycle phase")
            pending = locked.transition(current.phase, StatePhase.LOGOUT_PENDING)
            started = self._now()
            try:
                recorded_id = pending.resource_ids.get(
                    ResourceRole.CONTAINER.value
                )
                if (
                    self._uses_resource_id(ResourceRole.CONTAINER)
                    and recorded_id is not None
                ):
                    present = self._identity_present(
                        pending, ResourceRole.CONTAINER
                    )
                else:
                    present = bool(self._owned_count(ResourceRole.CONTAINER))
                if present:
                    container_id = self._inspect(
                        ResourceRole.CONTAINER,
                        cleanup=True,
                        expected_id=recorded_id,
                    )
                    self._resource_target(
                        pending, ResourceRole.CONTAINER, container_id
                    )
                    result = self._execute(
                        self._guest_command(
                            pending, GuestAction.LOGOUT, container_id
                        )
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
            current = self._persist_mutation_hold(locked, current)
            if current.phase not in _ALLOWED_PHASES_FOR_DESTROY:
                raise UsageStateError("destroy is unavailable in this lifecycle phase")
            pending = locked.transition(current.phase, StatePhase.DESTROY_PENDING)
            started = self._now()
            failed = False
            for role in self._cleanup_roles():
                recorded_id = pending.resource_ids.get(role.value)
                if self._uses_resource_id(role) and recorded_id is not None:
                    try:
                        observed_id = self._inspect(
                            role,
                            cleanup=True,
                            expected_id=recorded_id,
                        )
                        present = True
                    except InvariantRefusalError:
                        failed = True
                        continue
                    except RunnerFailureError:
                        try:
                            present = self._identity_present(pending, role)
                        except LabError:
                            failed = True
                            continue
                        if present:
                            failed = True
                            continue
                        # A process may have been killed after exact removal
                        # but before its ledger append. The global immutable-ID
                        # listing is the restart reconciliation proof.
                        if (
                            role.value in pending.created_roles
                            and role.value not in pending.removed_roles
                        ):
                            pending = locked.record_removed_role(
                                StatePhase.DESTROY_PENDING, role
                            )
                        continue
                    except LabError:
                        failed = True
                        continue
                else:
                    try:
                        present = bool(self._owned_count(role))
                    except LabError:
                        failed = True
                        continue
                if not present:
                    if (
                        self._uses_resource_id(role)
                        and recorded_id is None
                        and role.value in pending.created_roles
                    ):
                        # An ID-using run can never safely reconcile a
                        # durably-created resource without its daemon ID.
                        failed = True
                        continue
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
                if recorded_id is None:
                    try:
                        observed_id = self._inspect(role, cleanup=True)
                        if self._uses_resource_id(role):
                            pending = locked.record_resource_id(
                                StatePhase.DESTROY_PENDING,
                                role,
                                observed_id,
                            )
                    except LabError:
                        failed = True
                        continue
                pending = locked.record_owned_role(
                    StatePhase.DESTROY_PENDING, role
                )
                target = self._resource_target(pending, role, observed_id)
                role_failed = False
                if role is ResourceRole.CONTAINER:
                    try:
                        if self._uses_resource_id(role):
                            self._execute(self._commands.stop_container(target))
                        else:
                            self._execute(self._commands.stop_container())
                    except LabError:
                        role_failed = True
                    command = (
                        self._commands.remove_container(target)
                        if self._uses_resource_id(role)
                        else self._commands.remove_container()
                    )
                elif role in (
                    ResourceRole.WORKSPACE,
                    ResourceRole.AUTH,
                    ResourceRole.TOOL,
                ):
                    command = self._commands.remove_volume(role)
                elif role is ResourceRole.IMAGE:
                    command = (
                        self._commands.remove_image(target)
                        if self._uses_resource_id(role)
                        else self._commands.remove_image()
                    )
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
            if not failed and self._runtime_snapshot is not None:
                try:
                    self._runtime_snapshot.remove()
                except LabError:
                    # A failed identity proof can mean the derived snapshot was
                    # moved away from its deterministic name.  Preserve that
                    # uncertainty as an irreversible hold so a later retry
                    # cannot mistake name absence for complete cleanup.
                    pending = locked.mark_tainted(StatePhase.DESTROY_PENDING)
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
            current = self._persist_mutation_hold(locked, current)
            if current.phase not in _ALLOWED_PHASES_FOR_VERIFY:
                raise UsageStateError("clean verification is unavailable in this phase")
            pending = locked.transition(
                current.phase, StatePhase.VERIFY_CLEAN_PENDING
            )
            started = self._now()
            remaining = 0
            failed = False
            for role in self._cleanup_roles():
                try:
                    role_remaining = self._owned_count(role)
                    if (
                        not role_remaining
                        and self._uses_resource_id(role)
                        and self._identity_present(pending, role)
                    ):
                        role_remaining = 1
                    remaining += role_remaining
                except LabError:
                    failed = True
                    remaining += 1
            if self._runtime_snapshot is not None:
                try:
                    if self._runtime_snapshot.present():
                        remaining += 1
                except LabError:
                    failed = True
                    remaining += 1
            ledger_remaining = len(pending.created_roles) - len(pending.removed_roles)
            promotion_held = self._uses_resource_ids() and pending.tainted
            if failed or remaining or ledger_remaining or promotion_held:
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
            if clean_state.tainted:
                raise InvariantRefusalError(
                    "tainted state cannot seal evidence"
                )
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
                    self._artifact(clean_state),
                    FixturePlatform(executor_kind=self._executor_kind),
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
