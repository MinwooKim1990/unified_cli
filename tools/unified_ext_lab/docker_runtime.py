"""Opt-in real-Docker runtime with a finite, identity-bound grammar."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple, Union

from .docker import (
    CONTAINER_USER,
    FIXED_ENV,
    GUEST_EXECUTABLE,
    DockerCleanupSpec,
    DockerCommandBuilder,
    DockerLabSpec,
    DockerOperation,
    GuestAction,
    LockedContextSnapshot,
    snapshot_image_context,
    validate_inspect,
)
from .errors import (
    InvariantRefusalError,
    RunnerFailureError,
    UnsupportedError,
    UsageStateError,
)
from .model import LabIdentity, LabResource, ResourceRole
from .profile import (
    FIXED_DOCKER_ENDPOINT,
    RealDockerProfile,
    load_real_docker_profile,
)
from .runner import CommandResult, Runner, SubprocessRunner


# Discovery never consults PATH. A symlink at one reviewed location is resolved
# to a canonical identity before the runner makes its private executable copy.
DOCKER_CANDIDATES = (
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/usr/bin/docker",
)
BUILDX_CANDIDATES = (
    "/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx",
    "/usr/local/lib/docker/cli-plugins/docker-buildx",
    "/usr/local/libexec/docker/cli-plugins/docker-buildx",
    "/opt/homebrew/lib/docker/cli-plugins/docker-buildx",
    "/usr/lib/docker/cli-plugins/docker-buildx",
    "/usr/libexec/docker/cli-plugins/docker-buildx",
)
_VERSION_FORMAT = "{{json .}}"
_DOCKER_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_BUILDX_VERSION_RE = re.compile(
    r"^github\.com/docker/buildx "
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)? "
    r"[0-9a-f]{7,64}\n?$"
)
_LOCAL_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 3600.0

# Identity binding and exact argv are unit-tested. Promotion still requires an
# actual-Docker E2E gate for the copied Docker CLI and copied Buildx companion.
COPIED_CLI_BUILDX_E2E_REQUIRED = True


def _safe_candidate(path: str, description: str) -> Optional[str]:
    if not os.path.lexists(path):
        return None
    canonical = os.path.realpath(path)
    if not os.path.isabs(canonical) or os.path.normpath(canonical) != canonical:
        raise InvariantRefusalError("unsafe " + description + " candidate")
    try:
        info = os.lstat(canonical)
    except OSError as error:
        raise InvariantRefusalError("unsafe " + description + " candidate") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(canonical, os.X_OK)
        or (hasattr(os, "geteuid") and info.st_uid not in (0, os.geteuid()))
    ):
        raise InvariantRefusalError("unsafe " + description + " candidate")
    return canonical


def _discover_single(candidates: Tuple[str, ...], description: str) -> str:
    discovered = []
    for candidate in candidates:
        canonical = _safe_candidate(candidate, description)
        if canonical is not None and canonical not in discovered:
            discovered.append(canonical)
    if not discovered:
        raise UnsupportedError(description + " is unavailable")
    if len(discovered) != 1:
        raise InvariantRefusalError("multiple " + description + " identities are present")
    return discovered[0]


def discover_docker_executable() -> str:
    """Return the sole canonical Docker executable in reviewed locations."""

    return _discover_single(DOCKER_CANDIDATES, "Docker CLI")


def discover_buildx_executable() -> str:
    """Return the sole canonical Buildx companion in reviewed locations."""

    return _discover_single(BUILDX_CANDIDATES, "Docker Buildx companion")


def _resource_id(role: ResourceRole, value: object) -> str:
    if role is ResourceRole.IMAGE:
        valid = type(value) is str and _LOCAL_IMAGE_ID_RE.fullmatch(value) is not None
    elif role is ResourceRole.CONTAINER:
        valid = type(value) is str and _CONTAINER_ID_RE.fullmatch(value) is not None
    else:
        valid = False
    if not valid:
        raise InvariantRefusalError("invalid immutable Docker resource id")
    return value


def _label_args(resource: LabResource) -> Tuple[str, ...]:
    arguments = []
    for key, value in resource.labels.items():
        arguments.extend(("--filter", "label=" + key + "=" + value))
    return tuple(arguments)


def _unique_pairs(pairs: object) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_utf8_text(value: object, maximum_bytes: int) -> bool:
    if type(value) is not str:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum_bytes
    except UnicodeEncodeError:
        return False


def _inspect_record(payload: object, description: str) -> Dict[str, object]:
    if not _bounded_utf8_text(payload, 1024 * 1024):
        raise InvariantRefusalError("invalid " + description)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvariantRefusalError("invalid " + description) from error
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise InvariantRefusalError("invalid " + description)
    return value[0]


def validate_version_probe(payload: object) -> Mapping[str, object]:
    if not _bounded_utf8_text(payload, 256 * 1024):
        raise InvariantRefusalError("invalid Docker version response")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvariantRefusalError("invalid Docker version response") from error
    if type(value) is not dict:
        raise InvariantRefusalError("invalid Docker version response")
    client = value.get("Client")
    server = value.get("Server")
    if (
        type(client) is not dict
        or type(server) is not dict
        or type(client.get("Version")) is not str
        or _DOCKER_VERSION_RE.fullmatch(client["Version"]) is None
        or type(server.get("Version")) is not str
        or _DOCKER_VERSION_RE.fullmatch(server["Version"]) is None
    ):
        raise InvariantRefusalError("invalid Docker version response")
    return MappingProxyType(value)


def validate_buildx_version(payload: object) -> str:
    if (
        not _bounded_utf8_text(payload, 512)
        or _BUILDX_VERSION_RE.fullmatch(payload) is None
    ):
        raise InvariantRefusalError("invalid Docker Buildx version response")
    return payload.rstrip("\n")


def validate_locked_base_inspect(
    profile: RealDockerProfile,
    payload: object,
    *,
    expected_id: Optional[str] = None,
) -> Mapping[str, object]:
    if type(profile) is not RealDockerProfile:
        raise InvariantRefusalError("invalid locked base inspect response")
    record = _inspect_record(payload, "locked base inspect response")
    image_id = record.get("Id")
    digests = record.get("RepoDigests")
    if (
        type(image_id) is not str
        or _LOCAL_IMAGE_ID_RE.fullmatch(image_id) is None
        or (expected_id is not None and image_id != expected_id)
        or type(digests) is not list
        or any(type(item) is not str for item in digests)
        or profile.base_reference not in digests
        or record.get("Os") != profile.operating_system
        or record.get("Architecture") != profile.architecture
    ):
        raise InvariantRefusalError("locked base image or platform drift")
    return MappingProxyType(record)


class _RealDockerIdentityCommands:
    """Shared cleanup/list grammar for one deterministic resource plan."""

    uses_resource_ids = True
    cleanup_roles = (ResourceRole.CONTAINER, ResourceRole.IMAGE)
    create_volume_roles: Tuple[ResourceRole, ...] = ()

    def __init__(self, spec: Union[DockerLabSpec, DockerCleanupSpec]) -> None:
        self._spec = spec

    @property
    def spec(self) -> Union[DockerLabSpec, DockerCleanupSpec]:
        return self._spec

    @property
    def prefix(self) -> Tuple[str, ...]:
        return (
            self._spec.docker_executable,
            "--host",
            FIXED_DOCKER_ENDPOINT,
        )

    def inspect(
        self, role: ResourceRole, resource_id: Optional[str] = None
    ) -> Tuple[str, ...]:
        if role is ResourceRole.IMAGE:
            noun = "image"
        elif role is ResourceRole.CONTAINER:
            noun = "container"
        else:
            raise UsageStateError("real-Docker volumes are not managed")
        target = (
            self._spec.resource(role).name
            if resource_id is None
            else _resource_id(role, resource_id)
        )
        return self.prefix + (noun, "inspect", target)

    def list_owned(self, role: ResourceRole) -> Tuple[str, ...]:
        if role is ResourceRole.IMAGE:
            noun = "image"
        elif role is ResourceRole.CONTAINER:
            noun = "container"
        else:
            raise UsageStateError("real-Docker volumes are not managed")
        return self.prefix + (noun, "ls", "--all", "--quiet") + _label_args(
            self._spec.resource(role)
        )

    def list_named(self, role: ResourceRole) -> Tuple[str, ...]:
        resource = self._spec.resource(role)
        if role is ResourceRole.IMAGE:
            noun = "image"
            selector = "reference=" + resource.name + ":latest"
        elif role is ResourceRole.CONTAINER:
            noun = "container"
            selector = "name=" + resource.name
        else:
            raise UsageStateError("real-Docker volumes are not managed")
        return self.prefix + (
            noun,
            "ls",
            "--all",
            "--quiet",
            "--filter",
            selector,
        )

    def list_identity(self, role: ResourceRole) -> Tuple[str, ...]:
        """List daemon IDs globally so stripped labels/names cannot look absent."""

        if role is ResourceRole.IMAGE:
            noun = "image"
        elif role is ResourceRole.CONTAINER:
            noun = "container"
        else:
            raise UsageStateError("real-Docker volumes are not managed")
        return self.prefix + (
            noun,
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
        )

    def exec_guest(
        self,
        action: GuestAction,
        resource_id: str,
        extra: Tuple[str, ...] = (),
    ) -> Tuple[str, ...]:
        if not isinstance(action, GuestAction) or type(extra) is not tuple or extra:
            raise InvariantRefusalError("guest command arguments are fixed")
        target = _resource_id(ResourceRole.CONTAINER, resource_id)
        command = list(self.prefix + ("container", "exec", "--user", CONTAINER_USER))
        for value in FIXED_ENV:
            command.extend(("--env", value))
        command.extend(
            (
                "--workdir",
                "/workspace",
                target,
                GUEST_EXECUTABLE,
                action.value,
            )
        )
        return tuple(command)

    def stop_container(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "container",
            "stop",
            "--time",
            "10",
            _resource_id(ResourceRole.CONTAINER, resource_id),
        )

    def remove_container(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "container",
            "rm",
            "--force",
            _resource_id(ResourceRole.CONTAINER, resource_id),
        )

    def remove_image(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "image",
            "rm",
            _resource_id(ResourceRole.IMAGE, resource_id),
        )

    def _validate_identity_inspect(
        self, role: ResourceRole, payload: object
    ) -> str:
        record = _inspect_record(payload, "managed resource inspect response")
        resource = self._spec.resource(role)
        if role is ResourceRole.IMAGE:
            resource_id = _resource_id(role, record.get("Id"))
            config = record.get("Config")
            if (
                type(config) is not dict
                or config.get("Labels") != dict(resource.labels)
                or record.get("RepoTags") != [resource.name + ":latest"]
            ):
                raise InvariantRefusalError("managed image identity drift")
        elif role is ResourceRole.CONTAINER:
            resource_id = _resource_id(role, record.get("Id"))
            config = record.get("Config")
            if (
                type(config) is not dict
                or config.get("Labels") != dict(resource.labels)
                or record.get("Name") != "/" + resource.name
            ):
                raise InvariantRefusalError("managed container identity drift")
        else:
            raise UsageStateError("real-Docker volumes are not managed")
        return resource_id

    def validate_inspect(self, role: ResourceRole, payload: object) -> str:
        return self._validate_identity_inspect(role, payload)

    def validate_cleanup_inspect(
        self, role: ResourceRole, payload: object
    ) -> str:
        """Validate conservative name-and-label discovery without durable ID."""

        return self._validate_identity_inspect(role, payload)

    def validate_cleanup_identity_inspect(
        self, role: ResourceRole, payload: object, expected_id: str
    ) -> str:
        """Validate only immutable identity after cleanup has a durable ID.

        Docker names, tags, and labels are mutable.  Once state contains the
        daemon ID, cleanup targets that exact object and deliberately does not
        make mutable metadata an authorization requirement.
        """

        record = _inspect_record(payload, "managed resource inspect response")
        observed = _resource_id(role, record.get("Id"))
        expected = _resource_id(role, expected_id)
        if observed != expected:
            raise InvariantRefusalError("managed resource immutable identity drift")
        return observed

    def verify_clean(self) -> Tuple[Tuple[str, ...], ...]:
        commands = []
        for role in self.cleanup_roles:
            commands.extend((self.list_owned(role), self.list_named(role)))
        return tuple(commands)


class RealDockerCommandBuilder(_RealDockerIdentityCommands):
    """Forward-run grammar bound to a local base ID and private snapshot."""

    cleanup_only = False

    def __init__(self, spec: DockerLabSpec, profile: RealDockerProfile) -> None:
        if type(spec) is not DockerLabSpec or type(profile) is not RealDockerProfile:
            raise UsageStateError("invalid real-Docker command profile")
        if (
            _LOCAL_IMAGE_ID_RE.fullmatch(spec.base_image) is None
            or not spec.context_is_snapshot
            or not spec.ephemeral_storage
            or profile.docker_endpoint != FIXED_DOCKER_ENDPOINT
        ):
            raise InvariantRefusalError("real-Docker forward spec is not hardened")
        super().__init__(spec)
        self._profile = profile
        self._base = DockerCommandBuilder(spec)

    def inspect_base_image(self) -> Tuple[str, ...]:
        return self.prefix + ("image", "inspect", self._spec.base_image)

    def validate_base_image(self, payload: object) -> str:
        record = validate_locked_base_inspect(
            self._profile, payload, expected_id=self._spec.base_image
        )
        return _resource_id(ResourceRole.IMAGE, record["Id"])

    def build_image(self) -> Tuple[str, ...]:
        argv = self._base.build_image()
        return self.prefix + (
            "build",
            "--platform",
            self._profile.platform,
        ) + argv[2:]

    def create_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        raise UsageStateError("real-Docker named volumes are forbidden")

    def create_container(self) -> Tuple[str, ...]:
        argv = self._base.create_container()
        return self.prefix + (
            "container",
            "create",
            "--platform",
            self._profile.platform,
        ) + argv[3:]

    def start_container(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "container",
            "start",
            _resource_id(ResourceRole.CONTAINER, resource_id),
        )

    def exec_guest(
        self,
        action: GuestAction,
        resource_id: str,
        extra: Tuple[str, ...] = (),
    ) -> Tuple[str, ...]:
        if action is GuestAction.INSTALL:
            # Revalidate the private snapshot fixture immediately before the
            # guest is allowed to copy its image-baked counterpart.
            self._base.exec_guest(action)
        return super().exec_guest(action, resource_id, extra)

    def remove_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        raise UsageStateError("real-Docker named volumes are forbidden")

    def validate_inspect(self, role: ResourceRole, payload: object) -> str:
        validate_inspect(self._spec, role, payload)
        resource_id = super().validate_inspect(role, payload)
        if role is ResourceRole.IMAGE:
            record = _inspect_record(payload, "managed image inspect response")
            if (
                record.get("Os") != self._profile.operating_system
                or record.get("Architecture") != self._profile.architecture
            ):
                raise InvariantRefusalError("managed image platform drift")
        return resource_id

    def command_operations(
        self,
    ) -> Tuple[Tuple[Tuple[str, ...], DockerOperation], ...]:
        """Return the static pre-ID grammar; ID mutations are state-derived."""

        commands = [
            (self.inspect_base_image(), DockerOperation.INSPECT_BASE_IMAGE),
            (self.build_image(), DockerOperation.BUILD_IMAGE),
            (self.create_container(), DockerOperation.CREATE_CONTAINER),
        ]
        for role in self.cleanup_roles:
            operation = (
                DockerOperation.INSPECT_CONTAINER
                if role is ResourceRole.CONTAINER
                else DockerOperation.INSPECT_IMAGE
            )
            list_operation = (
                DockerOperation.LIST_CONTAINER
                if role is ResourceRole.CONTAINER
                else DockerOperation.LIST_IMAGE
            )
            commands.extend(
                (
                    (self.inspect(role), operation),
                    (self.list_owned(role), list_operation),
                    (self.list_named(role), list_operation),
                    (self.list_identity(role), list_operation),
                )
            )
        result = tuple(commands)
        if len({argv for argv, _operation in result}) != len(result):
            raise InvariantRefusalError("real-Docker command grammar contains duplicates")
        return result


class RealDockerCleanupCommandBuilder(_RealDockerIdentityCommands):
    """Cleanup grammar reconstructed only from durable resource identity."""

    cleanup_only = True

    def __init__(self, spec: DockerCleanupSpec) -> None:
        if type(spec) is not DockerCleanupSpec:
            raise UsageStateError("invalid real-Docker cleanup spec")
        super().__init__(spec)


class RealDockerRuntime:
    """Own one copied Docker CLI and, for forward runs, one copied Buildx."""

    def __init__(
        self,
        executable: str,
        runner: Runner,
        profile: Optional[RealDockerProfile],
        *,
        timeout: float = 30.0,
        cleanup_only: bool = False,
    ) -> None:
        if (
            type(executable) is not str
            or not os.path.isabs(executable)
            or os.path.realpath(executable) != executable
            or not callable(getattr(runner, "run", None))
            or type(timeout) not in (int, float)
            or type(cleanup_only) is not bool
            or (cleanup_only and profile is not None)
            or (not cleanup_only and type(profile) is not RealDockerProfile)
        ):
            raise UsageStateError("invalid real-Docker runtime")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as error:
            raise UsageStateError("invalid real-Docker runtime") from error
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_TIMEOUT_SECONDS
        ):
            raise UsageStateError("invalid real-Docker runtime")
        self.executable = executable
        self.runner = runner
        self.profile = profile
        self.timeout = timeout_seconds
        self.cleanup_only = cleanup_only
        self._local_base_id: Optional[str] = None
        self._snapshot_root: Optional[str] = None
        self._snapshot: Optional[LockedContextSnapshot] = None

    @classmethod
    def discover(cls) -> "RealDockerRuntime":
        profile = load_real_docker_profile()
        profile.require_routable()
        executable = discover_docker_executable()
        buildx = discover_buildx_executable()
        runner = SubprocessRunner(executable)
        try:
            bind = getattr(runner, "bind_docker_buildx", None)
            if not callable(bind):
                raise InvariantRefusalError("Docker runner cannot bind Buildx")
            bind(buildx)
            return cls(executable, runner, profile)
        except BaseException:
            runner.close()
            raise

    @classmethod
    def discover_cleanup(cls) -> "RealDockerRuntime":
        """Discover only Docker; no profile, base, context, or Buildx input."""

        executable = discover_docker_executable()
        return cls(
            executable,
            SubprocessRunner(executable),
            None,
            cleanup_only=True,
        )

    def __enter__(self) -> "RealDockerRuntime":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        snapshot_error = None
        if self._snapshot_root is not None:
            try:
                shutil.rmtree(self._snapshot_root)
            except FileNotFoundError:
                pass
            except BaseException as error:  # pragma: no cover - OS fault path.
                snapshot_error = error
            self._snapshot_root = None
            self._snapshot = None
        close = getattr(self.runner, "close", None)
        if callable(close):
            close()
        if snapshot_error is not None:
            raise InvariantRefusalError("private context cleanup failed") from snapshot_error

    @property
    def prefix(self) -> Tuple[str, ...]:
        return (self.executable, "--host", FIXED_DOCKER_ENDPOINT)

    def version_argv(self) -> Tuple[str, ...]:
        return self.prefix + ("version", "--format", _VERSION_FORMAT)

    def buildx_version_argv(self) -> Tuple[str, ...]:
        return self.prefix + ("buildx", "version")

    def inspect_base_argv(self) -> Tuple[str, ...]:
        if self.cleanup_only or type(self.profile) is not RealDockerProfile:
            raise UsageStateError("base inspection is unavailable in cleanup-only mode")
        return self.prefix + ("image", "inspect", self.profile.base_reference)

    def pull_base_argv(self) -> Tuple[str, ...]:
        if self.cleanup_only or type(self.profile) is not RealDockerProfile:
            raise UsageStateError("base preparation is unavailable in cleanup-only mode")
        return self.prefix + (
            "image",
            "pull",
            "--platform",
            self.profile.platform,
            self.profile.base_reference,
        )

    def _execute(self, argv: Tuple[str, ...]) -> CommandResult:
        result = self.runner.run(argv, timeout=self.timeout)
        if (
            type(result) is not CommandResult
            or result.argv != argv
            or result.returncode != 0
        ):
            raise RunnerFailureError("Docker runner returned an invalid result")
        return result

    def probe_daemon(self) -> None:
        if not self.cleanup_only:
            assert isinstance(self.profile, RealDockerProfile)
            self.profile.require_routable()
        try:
            result = self._execute(self.version_argv())
        except RunnerFailureError as error:
            raise UnsupportedError("Docker daemon is unavailable") from error
        validate_version_probe(result.stdout)

    def probe_buildx(self) -> None:
        if self.cleanup_only:
            raise UsageStateError("Buildx is unavailable in cleanup-only mode")
        try:
            result = self._execute(self.buildx_version_argv())
        except RunnerFailureError as error:
            raise UnsupportedError("Docker Buildx is unavailable") from error
        validate_buildx_version(result.stdout)

    def require_local_base(self) -> str:
        if self.cleanup_only or type(self.profile) is not RealDockerProfile:
            raise UsageStateError("base inspection is unavailable in cleanup-only mode")
        try:
            result = self._execute(self.inspect_base_argv())
        except RunnerFailureError as error:
            raise UnsupportedError("locked base image is unavailable") from error
        record = validate_locked_base_inspect(self.profile, result.stdout)
        return _resource_id(ResourceRole.IMAGE, record["Id"])

    def _capture_snapshot(self) -> None:
        if self._snapshot_root is not None:
            shutil.rmtree(self._snapshot_root)
            self._snapshot_root = None
            self._snapshot = None
        created = tempfile.mkdtemp(prefix="unified-ext-lab-context-")
        root = os.path.realpath(created)
        try:
            os.chmod(root, 0o700)
            snapshot = snapshot_image_context(root)
        except BaseException as error:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass
            except BaseException as cleanup_error:
                if hasattr(error, "add_note"):
                    error.add_note(
                        "private snapshot root cleanup failed: "
                        + type(cleanup_error).__name__
                    )
            raise
        self._snapshot_root = root
        self._snapshot = snapshot

    def preflight(self) -> None:
        self.probe_daemon()
        if self.cleanup_only:
            return
        self.probe_buildx()
        self._local_base_id = self.require_local_base()
        self._capture_snapshot()

    def prepare_base(self, *, allow_network: bool) -> None:
        if self.cleanup_only:
            raise UsageStateError("prepare-base is unavailable in cleanup-only mode")
        if allow_network is not True:
            raise UsageStateError("prepare-base requires --allow-network")
        assert isinstance(self.profile, RealDockerProfile)
        self.profile.require_routable()
        self.probe_daemon()
        self._execute(self.pull_base_argv())
        self._local_base_id = self.require_local_base()

    def spec(self, identity: LabIdentity) -> DockerLabSpec:
        if self.cleanup_only:
            raise UsageStateError("forward spec is unavailable in cleanup-only mode")
        if type(identity) is not LabIdentity:
            raise UsageStateError("invalid real-Docker lab identity")
        if self._local_base_id is None or self._snapshot is None:
            raise UsageStateError("real-Docker preflight is required")
        return DockerLabSpec.from_snapshot(
            identity,
            docker_executable=self.executable,
            base_image=self._local_base_id,
            snapshot=self._snapshot,
            ephemeral_storage=True,
        )

    def cleanup_spec(
        self, identity: LabIdentity, planned_resources: object
    ) -> DockerCleanupSpec:
        if not self.cleanup_only:
            raise UsageStateError("cleanup spec requires cleanup-only runtime")
        return DockerCleanupSpec.from_persisted(
            identity,
            docker_executable=self.executable,
            planned_resources=planned_resources,
        )

    def commands(
        self, spec: Union[DockerLabSpec, DockerCleanupSpec]
    ) -> Union[RealDockerCommandBuilder, RealDockerCleanupCommandBuilder]:
        if spec.docker_executable != self.executable:
            raise InvariantRefusalError("Docker spec executable drift")
        if self.cleanup_only:
            if type(spec) is not DockerCleanupSpec:
                raise UsageStateError("cleanup-only runtime requires cleanup spec")
            return RealDockerCleanupCommandBuilder(spec)
        if type(spec) is not DockerLabSpec or type(self.profile) is not RealDockerProfile:
            raise UsageStateError("forward runtime requires Docker lab spec")
        return RealDockerCommandBuilder(spec, self.profile)


__all__ = [
    "BUILDX_CANDIDATES",
    "COPIED_CLI_BUILDX_E2E_REQUIRED",
    "DOCKER_CANDIDATES",
    "RealDockerCleanupCommandBuilder",
    "RealDockerCommandBuilder",
    "RealDockerRuntime",
    "discover_buildx_executable",
    "discover_docker_executable",
    "validate_buildx_version",
    "validate_locked_base_inspect",
    "validate_version_probe",
]
