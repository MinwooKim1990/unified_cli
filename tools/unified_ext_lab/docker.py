"""Pure Docker argv construction and strict inspect-policy validation.

No function in this module starts Docker.  ``DockerCommandBuilder`` emits
direct, deterministic argv tuples for an injected runner; it has no generic
command escape hatch, Compose support, prune operation, wildcard deletion, or
provider-wide deletion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Dict, Mapping, Sequence, Tuple

from .errors import InvariantRefusalError, UsageStateError
from .model import LabIdentity, LabResource, LabResourceSet, ResourceRole


MODULE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIRECTORY = os.path.join(MODULE_DIRECTORY, "image")
BASE_IMAGE_LOCK = os.path.join(MODULE_DIRECTORY, "locks", "base-images.v1.json")
FIXTURE_LOCK = os.path.join(
    MODULE_DIRECTORY, "locks", "synthetic-fixtures.v1.json"
)
CONTEXT_LOCK = os.path.join(MODULE_DIRECTORY, "locks", "image-context.v1.json")

CONTAINER_USER = "65532:65532"
CONTAINER_HOME = "/home/lab"
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_AUTH = "/home/lab"
CONTAINER_TOOL = "/opt/unified-ext-lab/tool"
GUEST_EXECUTABLE = "/opt/unified-ext-lab/guest.py"
MEMORY_BYTES = 1024 * 1024 * 1024
NANO_CPUS = 1_000_000_000

FIXED_ENV = (
    "HOME=" + CONTAINER_HOME,
    "PATH=/usr/bin:/bin",
    "TMPDIR=/tmp",
    "XDG_CACHE_HOME=" + CONTAINER_HOME + "/.cache",
    "XDG_CONFIG_HOME=" + CONTAINER_HOME + "/.config",
    "XDG_DATA_HOME=" + CONTAINER_HOME + "/.local/share",
)

VOLUME_TARGETS = MappingProxyType(
    {
        ResourceRole.WORKSPACE: CONTAINER_WORKSPACE,
        ResourceRole.AUTH: CONTAINER_AUTH,
        ResourceRole.TOOL: CONTAINER_TOOL,
    }
)

_BASE_REFERENCE_RE = re.compile(
    r"^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$"
)
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_FIXTURE_RELATIVE_PATH = os.path.join(
    "rootfs", "opt", "unified-ext-lab", "fixtures", "fake-provider"
)
_EXPECTED_CONTEXT_FILES = {
    "Dockerfile",
    os.path.join("rootfs", "home", "lab", ".volume-owner"),
    os.path.join("rootfs", "opt", "unified-ext-lab", "guest.py"),
    _FIXTURE_RELATIVE_PATH,
    os.path.join(
        "rootfs", "opt", "unified-ext-lab", "tool", ".volume-owner"
    ),
    os.path.join("rootfs", "workspace", "README.md"),
    os.path.join("rootfs", "workspace", "project.marker"),
}
_EXPECTED_CONTEXT_DIRECTORIES = {
    "rootfs",
    os.path.join("rootfs", "home"),
    os.path.join("rootfs", "home", "lab"),
    os.path.join("rootfs", "opt"),
    os.path.join("rootfs", "opt", "unified-ext-lab"),
    os.path.join("rootfs", "opt", "unified-ext-lab", "fixtures"),
    os.path.join("rootfs", "opt", "unified-ext-lab", "tool"),
    os.path.join("rootfs", "workspace"),
}
_MAX_LOCK_BYTES = 64 * 1024
_MAX_CONTEXT_FILE_BYTES = 16 * 1024 * 1024


class GuestAction(str, Enum):
    INSTALL = "install"
    TEST = "test"
    LOGOUT = "logout"


class DockerOperation(str, Enum):
    INSPECT_BASE_IMAGE = "inspect_base_image"
    BUILD_IMAGE = "build_image"
    CREATE_VOLUME = "create_volume"
    CREATE_CONTAINER = "create_container"
    START_CONTAINER = "start_container"
    INSPECT_IMAGE = "inspect_image"
    INSPECT_CONTAINER = "inspect_container"
    INSPECT_VOLUME = "inspect_volume"
    EXEC_GUEST = "exec_guest"
    STOP_CONTAINER = "stop_container"
    REMOVE_CONTAINER = "remove_container"
    REMOVE_VOLUME = "remove_volume"
    REMOVE_IMAGE = "remove_image"
    LIST_IMAGE = "list_image"
    LIST_CONTAINER = "list_container"
    LIST_VOLUME = "list_volume"


@dataclass(frozen=True)
class SyntheticFixture:
    artifact_path: str
    version: str
    sha256: str
    scaffold_only: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.artifact_path) is not str
            or not os.path.isabs(self.artifact_path)
            or os.path.realpath(self.artifact_path) != self.artifact_path
        ):
            raise UsageStateError("fixture artifact path must be absolute and canonical")
        if type(self.version) is not str or not self.version:
            raise UsageStateError("invalid fixture version")
        if type(self.sha256) is not str or _CHECKSUM_RE.fullmatch(self.sha256) is None:
            raise UsageStateError("invalid fixture checksum")
        if self.scaffold_only is not True:
            raise InvariantRefusalError("fixture must be scaffold-only")


@dataclass(frozen=True)
class DockerLabSpec:
    """All exact identities and locks needed to build Docker commands."""

    identity: LabIdentity
    image: LabResource
    base_image: str
    context: str
    context_lock: str
    context_lock_sha256: str
    resources: LabResourceSet
    docker_executable: str
    fixture: SyntheticFixture

    def __post_init__(self) -> None:
        if type(self.identity) is not LabIdentity:
            raise UsageStateError("invalid Docker lab identity")
        if type(self.image) is not LabResource or self.image.role is not ResourceRole.IMAGE:
            raise UsageStateError("invalid Docker image resource")
        if self.image.identity != self.identity:
            raise InvariantRefusalError("Docker image identity mismatch")
        if type(self.resources) is not LabResourceSet:
            raise UsageStateError("invalid Docker resource set")
        expected_roles = {
            ResourceRole.IMAGE,
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        }
        roles = {resource.role for resource in self.resources.resources}
        if roles != expected_roles or len(self.resources.resources) != len(expected_roles):
            raise InvariantRefusalError("Docker resource inventory is incomplete")
        for resource in self.resources.resources:
            if resource.identity != self.identity:
                raise InvariantRefusalError("Docker resource identity mismatch")
        if self.image not in self.resources.resources:
            raise InvariantRefusalError("Docker image is absent from resource inventory")
        if type(self.base_image) is not str or _BASE_REFERENCE_RE.fullmatch(
            self.base_image
        ) is None:
            raise UsageStateError("base image must be pinned by sha256")
        if (
            type(self.context) is not str
            or not os.path.isabs(self.context)
            or os.path.realpath(self.context) != self.context
            or not os.path.isdir(self.context)
        ):
            raise UsageStateError("image context must be an absolute canonical directory")
        if (
            type(self.context_lock) is not str
            or not os.path.isabs(self.context_lock)
            or os.path.realpath(self.context_lock) != self.context_lock
            or os.path.normpath(self.context_lock) != self.context_lock
            or type(self.context_lock_sha256) is not str
            or _CHECKSUM_RE.fullmatch(self.context_lock_sha256) is None
        ):
            raise UsageStateError("invalid image context lock identity")
        _validate_image_context(
            self.context, self.context_lock, self.context_lock_sha256
        )
        if (
            type(self.docker_executable) is not str
            or not os.path.isabs(self.docker_executable)
            or os.path.normpath(self.docker_executable) != self.docker_executable
            or os.path.realpath(self.docker_executable) != self.docker_executable
        ):
            raise UsageStateError("Docker executable must be absolute and canonical")
        if type(self.fixture) is not SyntheticFixture:
            raise UsageStateError("invalid synthetic fixture")
        expected_fixture_path = os.path.join(self.context, _FIXTURE_RELATIVE_PATH)
        if self.fixture.artifact_path != expected_fixture_path:
            raise InvariantRefusalError("fixture does not match the image context")

    @classmethod
    def from_locks(
        cls,
        identity: LabIdentity,
        *,
        docker_executable: str,
        context: str = IMAGE_DIRECTORY,
        base_lock_path: str = BASE_IMAGE_LOCK,
        fixture_lock_path: str = FIXTURE_LOCK,
        context_lock_path: str = CONTEXT_LOCK,
    ) -> "DockerLabSpec":
        if type(identity) is not LabIdentity:
            raise UsageStateError("invalid Docker lab identity")
        if (
            type(context) is not str
            or not os.path.isabs(context)
            or os.path.normpath(context) != context
            or os.path.realpath(context) != context
        ):
            raise UsageStateError(
                "image context path must be absolute and canonical"
            )
        context_path = context
        base_data = _load_json_object(base_lock_path, "base image lock")
        if (
            set(base_data) != {"schema", "base_image"}
            or type(base_data["schema"]) is not int
            or base_data["schema"] != 1
        ):
            raise InvariantRefusalError("invalid base image lock")
        base = base_data["base_image"]
        if type(base) is not dict or set(base) != {"name", "digest"}:
            raise InvariantRefusalError("invalid base image lock")
        if type(base["name"]) is not str or type(base["digest"]) is not str:
            raise InvariantRefusalError("invalid base image lock")
        base_reference = "{}@{}".format(base["name"], base["digest"])

        fixture_data = _load_json_object(fixture_lock_path, "fixture lock")
        if (
            set(fixture_data) != {"schema", "fixture"}
            or type(fixture_data["schema"]) is not int
            or fixture_data["schema"] != 1
        ):
            raise InvariantRefusalError("invalid fixture lock")
        fixture_record = fixture_data["fixture"]
        required_fixture_keys = {
            "artifact",
            "version",
            "sha256",
            "scaffold_only",
        }
        if type(fixture_record) is not dict or set(fixture_record) != required_fixture_keys:
            raise InvariantRefusalError("invalid fixture lock")
        if (
            type(fixture_record["artifact"]) is not str
            or type(fixture_record["version"]) is not str
            or type(fixture_record["sha256"]) is not str
            or type(fixture_record["scaffold_only"]) is not bool
        ):
            raise InvariantRefusalError("invalid fixture lock")
        artifact = os.path.realpath(os.path.join(MODULE_DIRECTORY, fixture_record["artifact"]))
        fixture = SyntheticFixture(
            artifact_path=artifact,
            version=fixture_record["version"],
            sha256=fixture_record["sha256"],
            scaffold_only=fixture_record["scaffold_only"],
        )
        resource_tuple = tuple(
            identity.resource(role)
            for role in (
                ResourceRole.IMAGE,
                ResourceRole.CONTAINER,
                ResourceRole.WORKSPACE,
                ResourceRole.AUTH,
                ResourceRole.TOOL,
            )
        )
        resources = LabResourceSet(resource_tuple)
        if type(context_lock_path) is not str:
            raise UsageStateError("image context lock path must be canonical")
        canonical_context_lock = os.path.realpath(context_lock_path)
        if (
            canonical_context_lock != context_lock_path
            or os.path.normpath(context_lock_path) != context_lock_path
        ):
            raise InvariantRefusalError("image context lock must be canonical")
        context_lock_sha256 = _file_sha256(
            canonical_context_lock, "image context lock"
        )
        return cls(
            identity=identity,
            image=resource_tuple[0],
            base_image=base_reference,
            context=context_path,
            context_lock=canonical_context_lock,
            context_lock_sha256=context_lock_sha256,
            resources=resources,
            docker_executable=docker_executable,
            fixture=fixture,
        )

    def resource(self, role: ResourceRole) -> LabResource:
        normalized = _role(role)
        for resource in self.resources.resources:
            if resource.role is normalized:
                return resource
        raise UsageStateError("resource role is not present")


def _owned_regular_bytes(
    path: object, description: str, maximum_bytes: int
) -> bytes:
    """Read one canonical, owner-controlled, single-link regular file."""

    if (
        type(path) is not str
        or not os.path.isabs(path)
        or os.path.normpath(path) != path
        or os.path.realpath(path) != path
    ):
        raise UsageStateError(description + " path must be absolute and canonical")
    try:
        before = os.lstat(path)
    except OSError as error:
        raise InvariantRefusalError("invalid " + description) from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum_bytes
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise InvariantRefusalError("invalid " + description)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InvariantRefusalError("invalid " + description) from error
    try:
        opened = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != opened_identity:
            raise InvariantRefusalError("invalid " + description)
        chunks = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(payload) > maximum_bytes or after_identity != opened_identity:
            raise InvariantRefusalError("invalid " + description)
        return payload
    finally:
        os.close(descriptor)


def _unique_json_pairs(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _load_json_object(path: object, description: str) -> Dict[str, object]:
    payload = _owned_regular_bytes(path, description, _MAX_LOCK_BYTES)
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise InvariantRefusalError("invalid " + description) from error
    if type(value) is not dict:
        raise InvariantRefusalError("invalid " + description)
    return value


def _locked_mode(value: object, description: str) -> int:
    if type(value) is not str or re.fullmatch(r"0[0-7]{3}", value) is None:
        raise InvariantRefusalError("invalid " + description)
    mode = int(value, 8)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InvariantRefusalError("invalid " + description)
    return mode


def _relative_context_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise InvariantRefusalError("invalid image context lock")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise InvariantRefusalError("invalid image context lock")
    return os.path.join(*path.parts)


def _validate_image_context(
    context: str, context_lock: str, expected_lock_sha256: str
) -> None:
    if _file_sha256(context_lock, "image context lock") != expected_lock_sha256:
        raise InvariantRefusalError("image context lock identity changed")
    lock = _load_json_object(context_lock, "image context lock")
    if set(lock) != {"schema", "root_mode", "directories", "files"}:
        raise InvariantRefusalError("invalid image context lock")
    if type(lock["schema"]) is not int or lock["schema"] != 1:
        raise InvariantRefusalError("invalid image context lock")
    if type(lock["directories"]) is not dict or type(lock["files"]) is not dict:
        raise InvariantRefusalError("invalid image context lock")
    root_mode = _locked_mode(lock["root_mode"], "image context root mode")
    directory_modes = {
        _relative_context_path(relative): _locked_mode(mode, "image context directory mode")
        for relative, mode in lock["directories"].items()
    }
    file_records = {}
    for relative, record in lock["files"].items():
        normalized = _relative_context_path(relative)
        if type(record) is not dict or set(record) != {"mode", "sha256"}:
            raise InvariantRefusalError("invalid image context file lock")
        if type(record["sha256"]) is not str or _CHECKSUM_RE.fullmatch(
            record["sha256"]
        ) is None:
            raise InvariantRefusalError("invalid image context file lock")
        file_records[normalized] = (
            _locked_mode(record["mode"], "image context file mode"),
            record["sha256"],
        )
    if (
        set(directory_modes) != _EXPECTED_CONTEXT_DIRECTORIES
        or set(file_records) != _EXPECTED_CONTEXT_FILES
    ):
        raise InvariantRefusalError("image context inventory drift")

    try:
        root = os.lstat(context)
    except OSError as error:
        raise InvariantRefusalError("image context cannot be inspected") from error
    if (
        stat.S_ISLNK(root.st_mode)
        or not stat.S_ISDIR(root.st_mode)
        or stat.S_IMODE(root.st_mode) != root_mode
        or root.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (hasattr(os, "geteuid") and root.st_uid != os.geteuid())
    ):
        raise InvariantRefusalError("image context root drift")

    observed_files = set()
    observed_directories = set()

    def refuse_walk_error(error: OSError) -> None:
        raise InvariantRefusalError("image context cannot be inspected") from error

    try:
        for directory, directories, files in os.walk(
            context, onerror=refuse_walk_error, followlinks=False
        ):
            for name in directories:
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, context)
                info = os.lstat(path)
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISDIR(info.st_mode)
                    or relative not in directory_modes
                    or stat.S_IMODE(info.st_mode) != directory_modes[relative]
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                ):
                    raise InvariantRefusalError("image context directory drift")
                observed_directories.add(relative)
            for name in files:
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, context)
                if relative not in file_records:
                    raise InvariantRefusalError("image context inventory drift")
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise InvariantRefusalError("image context file drift")
                expected_mode, expected_sha256 = file_records[relative]
                payload = _owned_regular_bytes(
                    path,
                    "image context file",
                    _MAX_CONTEXT_FILE_BYTES,
                )
                if (
                    stat.S_IMODE(info.st_mode) != expected_mode
                    or hashlib.sha256(payload).hexdigest() != expected_sha256
                ):
                    raise InvariantRefusalError("image context file drift")
                observed_files.add(relative)
    except OSError as error:
        raise InvariantRefusalError("image context cannot be inspected") from error
    if (
        observed_files != _EXPECTED_CONTEXT_FILES
        or observed_directories != _EXPECTED_CONTEXT_DIRECTORIES
    ):
        raise InvariantRefusalError("image context inventory drift")


def _role(value: object) -> ResourceRole:
    if isinstance(value, ResourceRole):
        return value
    if type(value) is str:
        try:
            return ResourceRole(value)
        except ValueError:
            pass
    raise UsageStateError("invalid Docker resource role")


def _action(value: object) -> GuestAction:
    if isinstance(value, GuestAction):
        return value
    if type(value) is str:
        try:
            return GuestAction(value)
        except ValueError:
            pass
    raise UsageStateError("invalid guest action")


def _label_args(resource: LabResource) -> Tuple[str, ...]:
    arguments = []
    for key, value in resource.labels.items():
        arguments.extend(("--label", key + "=" + value))
    return tuple(arguments)


def _filter_args(resource: LabResource) -> Tuple[str, ...]:
    arguments = []
    for key, value in resource.labels.items():
        arguments.extend(("--filter", "label=" + key + "=" + value))
    return tuple(arguments)


def _file_sha256(path: str, description: str = "fixture artifact") -> str:
    payload = _owned_regular_bytes(path, description, _MAX_CONTEXT_FILE_BYTES)
    return hashlib.sha256(payload).hexdigest()


class DockerCommandBuilder:
    """Build only the finite Docker command set used by one exact lab."""

    def __init__(self, spec: DockerLabSpec) -> None:
        if type(spec) is not DockerLabSpec:
            raise UsageStateError("invalid Docker lab spec")
        self._spec = spec

    @property
    def spec(self) -> DockerLabSpec:
        return self._spec

    def build_image(self) -> Tuple[str, ...]:
        # Revalidate immediately before the runner receives the original
        # context path. The lock digest itself was captured in DockerLabSpec.
        _validate_image_context(
            self._spec.context,
            self._spec.context_lock,
            self._spec.context_lock_sha256,
        )
        image = self._spec.image
        dockerfile = os.path.join(self._spec.context, "Dockerfile")
        return (
            self._spec.docker_executable,
            "build",
            "--pull=false",
            "--network",
            "none",
            "--no-cache",
            "--file",
            dockerfile,
            "--build-arg",
            "BASE_IMAGE=" + self._spec.base_image,
            "--tag",
            image.name,
        ) + _label_args(image) + (self._spec.context,)

    def inspect_base_image(self) -> Tuple[str, ...]:
        """Require the digest-pinned base to exist locally before build.

        Docker may otherwise contact a registry to resolve a missing ``FROM``
        image even when the build network is disabled.
        """

        return (
            self._spec.docker_executable,
            "image",
            "inspect",
            self._spec.base_image,
        )

    def create_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        normalized = _role(role)
        if normalized not in VOLUME_TARGETS:
            raise UsageStateError("role is not a managed volume")
        resource = self._spec.resource(normalized)
        return (
            self._spec.docker_executable,
            "volume",
            "create",
        ) + _label_args(resource) + (resource.name,)

    def create_container(self) -> Tuple[str, ...]:
        container = self._spec.resource(ResourceRole.CONTAINER)
        command = [
            self._spec.docker_executable,
            "container",
            "create",
            "--name",
            container.name,
        ]
        command.extend(_label_args(container))
        command.extend(
            (
                "--user",
                CONTAINER_USER,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--network",
                "none",
                "--init",
                "--pids-limit",
                "128",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--cpus",
                "1.0",
                "--ulimit",
                "nofile=1024:1024",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            )
        )
        for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL):
            volume = self._spec.resource(role)
            mount = "type=volume,src={},dst={}".format(
                volume.name, VOLUME_TARGETS[role]
            )
            command.extend(
                (
                    "--mount",
                    mount,
                )
            )
        for value in FIXED_ENV:
            command.extend(("--env", value))
        command.extend(
            (
                "--workdir",
                CONTAINER_WORKSPACE,
                "--entrypoint",
                GUEST_EXECUTABLE,
                self._spec.image.name,
                "idle",
            )
        )
        return tuple(command)

    def start_container(self) -> Tuple[str, ...]:
        return (
            self._spec.docker_executable,
            "container",
            "start",
            self._spec.resource(ResourceRole.CONTAINER).name,
        )

    def inspect(self, role: ResourceRole) -> Tuple[str, ...]:
        normalized = _role(role)
        resource = self._spec.resource(normalized)
        if normalized is ResourceRole.IMAGE:
            noun = "image"
        elif normalized is ResourceRole.CONTAINER:
            noun = "container"
        elif normalized in VOLUME_TARGETS:
            noun = "volume"
        else:
            raise UsageStateError("role cannot be inspected")
        return (self._spec.docker_executable, noun, "inspect", resource.name)

    def exec_guest(
        self, action: GuestAction, extra: Tuple[str, ...] = ()
    ) -> Tuple[str, ...]:
        normalized = _action(action)
        if type(extra) is not tuple or extra:
            raise InvariantRefusalError("guest command arguments are fixed")
        if normalized is GuestAction.INSTALL:
            if _file_sha256(self._spec.fixture.artifact_path) != self._spec.fixture.sha256:
                raise InvariantRefusalError("fixture checksum mismatch")
        command = [
            self._spec.docker_executable,
            "container",
            "exec",
            "--user",
            CONTAINER_USER,
        ]
        for value in FIXED_ENV:
            command.extend(("--env", value))
        command.extend(
            (
                "--workdir",
                CONTAINER_WORKSPACE,
                self._spec.resource(ResourceRole.CONTAINER).name,
                GUEST_EXECUTABLE,
                normalized.value,
            )
        )
        return tuple(command)

    def stop_container(self) -> Tuple[str, ...]:
        return (
            self._spec.docker_executable,
            "container",
            "stop",
            "--time",
            "10",
            self._spec.resource(ResourceRole.CONTAINER).name,
        )

    def remove_container(self) -> Tuple[str, ...]:
        return (
            self._spec.docker_executable,
            "container",
            "rm",
            "--force",
            self._spec.resource(ResourceRole.CONTAINER).name,
        )

    def remove_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        normalized = _role(role)
        if normalized not in VOLUME_TARGETS:
            raise UsageStateError("role is not a managed volume")
        return (
            self._spec.docker_executable,
            "volume",
            "rm",
            self._spec.resource(normalized).name,
        )

    def remove_image(self) -> Tuple[str, ...]:
        return (
            self._spec.docker_executable,
            "image",
            "rm",
            self._spec.image.name,
        )

    def list_owned(self, role: ResourceRole) -> Tuple[str, ...]:
        normalized = _role(role)
        resource = self._spec.resource(normalized)
        if normalized is ResourceRole.IMAGE:
            noun = "image"
            all_flag = ("--all",)
        elif normalized is ResourceRole.CONTAINER:
            noun = "container"
            all_flag = ("--all",)
        elif normalized in VOLUME_TARGETS:
            noun = "volume"
            all_flag = ()
        else:
            raise UsageStateError("role cannot be listed")
        return (
            self._spec.docker_executable,
            noun,
            "ls",
        ) + all_flag + ("--quiet",) + _filter_args(resource)

    def list_named(self, role: ResourceRole) -> Tuple[str, ...]:
        """List only the recorded name/reference, independent of its labels.

        Cleanup requires both this query and :meth:`list_owned`. A resource
        whose labels drift must remain visible through its exact recorded name
        and can therefore never be mistaken for successful cleanup.
        """

        normalized = _role(role)
        resource = self._spec.resource(normalized)
        if normalized is ResourceRole.IMAGE:
            noun = "image"
            all_flag = ("--all",)
            selector = ("--filter", "reference=" + resource.name + ":latest")
        elif normalized is ResourceRole.CONTAINER:
            noun = "container"
            all_flag = ("--all",)
            selector = ("--filter", "name=" + resource.name)
        elif normalized in VOLUME_TARGETS:
            noun = "volume"
            all_flag = ()
            selector = ("--filter", "name=" + resource.name)
        else:
            raise UsageStateError("role cannot be listed")
        return (
            self._spec.docker_executable,
            noun,
            "ls",
        ) + all_flag + ("--quiet",) + selector

    def verify_clean(self) -> Tuple[Tuple[str, ...], ...]:
        commands = []
        for role in (
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
            ResourceRole.IMAGE,
        ):
            commands.extend((self.list_owned(role), self.list_named(role)))
        return tuple(commands)

    def command_operations(
        self,
    ) -> Tuple[Tuple[Tuple[str, ...], DockerOperation], ...]:
        """Return the complete exact command grammar for this immutable spec."""

        commands = [
            (self.inspect_base_image(), DockerOperation.INSPECT_BASE_IMAGE),
            (self.build_image(), DockerOperation.BUILD_IMAGE),
        ]
        for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL):
            commands.append((self.create_volume(role), DockerOperation.CREATE_VOLUME))
        commands.extend(
            (
                (self.create_container(), DockerOperation.CREATE_CONTAINER),
                (self.start_container(), DockerOperation.START_CONTAINER),
            )
        )
        for role in (
            ResourceRole.IMAGE,
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            if role is ResourceRole.IMAGE:
                operation = DockerOperation.INSPECT_IMAGE
            elif role is ResourceRole.CONTAINER:
                operation = DockerOperation.INSPECT_CONTAINER
            else:
                operation = DockerOperation.INSPECT_VOLUME
            commands.append((self.inspect(role), operation))
            commands.extend(
                (
                    (
                        self.list_owned(role),
                        {
                            ResourceRole.IMAGE: DockerOperation.LIST_IMAGE,
                            ResourceRole.CONTAINER: DockerOperation.LIST_CONTAINER,
                        }.get(role, DockerOperation.LIST_VOLUME),
                    ),
                    (
                        self.list_named(role),
                        {
                            ResourceRole.IMAGE: DockerOperation.LIST_IMAGE,
                            ResourceRole.CONTAINER: DockerOperation.LIST_CONTAINER,
                        }.get(role, DockerOperation.LIST_VOLUME),
                    ),
                )
            )
        for action in (GuestAction.INSTALL, GuestAction.TEST, GuestAction.LOGOUT):
            commands.append((self.exec_guest(action), DockerOperation.EXEC_GUEST))
        commands.extend(
            (
                (self.stop_container(), DockerOperation.STOP_CONTAINER),
                (self.remove_container(), DockerOperation.REMOVE_CONTAINER),
                (self.remove_volume(ResourceRole.WORKSPACE), DockerOperation.REMOVE_VOLUME),
                (self.remove_volume(ResourceRole.AUTH), DockerOperation.REMOVE_VOLUME),
                (self.remove_volume(ResourceRole.TOOL), DockerOperation.REMOVE_VOLUME),
                (self.remove_image(), DockerOperation.REMOVE_IMAGE),
            )
        )
        if len({command for command, _operation in commands}) != len(commands):
            raise InvariantRefusalError("Docker command grammar contains duplicates")
        return tuple(commands)


def classify_docker_argv(
    argv: object,
    allowed: Mapping[Tuple[str, ...], DockerOperation],
) -> DockerOperation:
    """Classify only an exact tuple from one immutable spec grammar."""

    if type(argv) is not tuple or not isinstance(allowed, Mapping):
        raise UsageStateError("unrecognized Docker command")
    try:
        operation = allowed[argv]
    except (KeyError, TypeError) as error:
        raise UsageStateError("unrecognized Docker command") from error
    if not isinstance(operation, DockerOperation):
        raise UsageStateError("unrecognized Docker command")
    return operation


def _labels(value: object) -> Dict[str, str]:
    if type(value) is not dict:
        raise InvariantRefusalError("inspect policy drift")
    if any(type(key) is not str or type(item) is not str for key, item in value.items()):
        raise InvariantRefusalError("inspect policy drift")
    return value


def _dict(value: object) -> Dict[str, object]:
    if type(value) is not dict:
        raise InvariantRefusalError("inspect policy drift")
    return value


def _list(value: object) -> Sequence[object]:
    if type(value) is not list:
        raise InvariantRefusalError("inspect policy drift")
    return value


def _empty_dict(value: object) -> Dict[str, object]:
    if value is None:
        return {}
    return _dict(value)


def _empty_list(value: object) -> Sequence[object]:
    if value is None:
        return []
    return _list(value)


def _expected_mounts(spec: DockerLabSpec) -> Tuple[Tuple[object, ...], ...]:
    return tuple(
        (
            "volume",
            spec.resource(role).name,
            VOLUME_TARGETS[role],
            True,
        )
        for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL)
    )


def validate_inspect(spec: DockerLabSpec, role: ResourceRole, payload: object) -> None:
    """Reject malformed JSON or any drift from the managed security policy."""

    if type(spec) is not DockerLabSpec:
        raise UsageStateError("invalid Docker lab spec")
    normalized = _role(role)
    if type(payload) is bytes:
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvariantRefusalError("invalid inspect payload") from error
    if type(payload) is not str:
        raise InvariantRefusalError("invalid inspect payload")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError) as error:
        raise InvariantRefusalError("invalid inspect payload") from error
    if type(decoded) is not list or len(decoded) != 1 or type(decoded[0]) is not dict:
        raise InvariantRefusalError("invalid inspect payload")
    record = decoded[0]
    resource = spec.resource(normalized)
    try:
        if normalized is ResourceRole.CONTAINER:
            _validate_container_record(spec, resource, record)
        elif normalized is ResourceRole.IMAGE:
            _validate_image_record(resource, record)
        elif normalized in VOLUME_TARGETS:
            _validate_volume_record(resource, record)
        else:
            raise UsageStateError("role cannot be inspected")
    except (KeyError, TypeError, ValueError, InvariantRefusalError) as error:
        if isinstance(error, UsageStateError):
            raise
        raise InvariantRefusalError("inspect policy drift") from error


def validate_base_image_inspect(spec: DockerLabSpec, payload: object) -> None:
    """Confirm the digest-pinned base is already present without a pull."""

    if type(spec) is not DockerLabSpec or type(payload) is not str:
        raise InvariantRefusalError("invalid base image inspect payload")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvariantRefusalError("invalid base image inspect payload") from error
    if (
        type(decoded) is not list
        or len(decoded) != 1
        or type(decoded[0]) is not dict
    ):
        raise InvariantRefusalError("base image digest is not locally verified")
    digests = decoded[0].get("RepoDigests")
    if (
        type(digests) is not list
        or any(type(item) is not str for item in digests)
        or spec.base_image not in digests
    ):
        raise InvariantRefusalError("base image digest is not locally verified")


def _validate_image_record(resource: LabResource, record: Dict[str, object]) -> None:
    config = _dict(record["Config"])
    observed = (
        tuple(_list(record["RepoTags"])),
        _labels(config["Labels"]),
        config["User"],
        tuple(_list(config["Entrypoint"])),
        tuple(_list(config["Cmd"])),
        tuple(_empty_list(config["Env"])),
        _empty_dict(config["ExposedPorts"]),
        _empty_dict(config["Volumes"]),
    )
    expected = (
        (resource.name + ":latest",),
        dict(resource.labels),
        CONTAINER_USER,
        (GUEST_EXECUTABLE,),
        ("idle",),
        (),
        {},
        {},
    )
    if observed != expected:
        raise InvariantRefusalError("inspect policy drift")


def _validate_volume_record(resource: LabResource, record: Dict[str, object]) -> None:
    options = record["Options"]
    if options is None:
        options = {}
    observed = (
        record["Name"],
        _labels(record["Labels"]),
        record["Driver"],
        _dict(options),
        record["Scope"],
    )
    expected = (resource.name, dict(resource.labels), "local", {}, "local")
    if observed != expected:
        raise InvariantRefusalError("inspect policy drift")


def _validate_container_record(
    spec: DockerLabSpec, resource: LabResource, record: Dict[str, object]
) -> None:
    config = _dict(record["Config"])
    host = _dict(record["HostConfig"])
    network = _dict(record["NetworkSettings"])
    mounts = []
    for mount_value in _list(record["Mounts"]):
        mount = _dict(mount_value)
        mounts.append(
            (mount["Type"], mount["Name"], mount["Destination"], mount["RW"])
        )
    ulimits = []
    for limit_value in _list(host["Ulimits"]):
        limit = _dict(limit_value)
        ulimits.append((limit["Name"], limit["Soft"], limit["Hard"]))
    binds = host["Binds"]
    if binds is None:
        binds = []
    observed = (
        record["Name"],
        _labels(config["Labels"]),
        config["User"],
        config["Image"],
        tuple(_list(config["Env"])),
        config["WorkingDir"],
        tuple(_list(config["Entrypoint"])),
        tuple(_list(config["Cmd"])),
        _empty_dict(config["ExposedPorts"]),
        _empty_dict(config["Volumes"]),
        host["ReadonlyRootfs"],
        tuple(_list(host["CapDrop"])),
        tuple(_list(host["SecurityOpt"])),
        host["NetworkMode"],
        host["Init"],
        host["PidsLimit"],
        host["Memory"],
        host["MemorySwap"],
        host["NanoCpus"],
        tuple(ulimits),
        host["Privileged"],
        tuple(_empty_list(host["CapAdd"])),
        tuple(_empty_list(binds)),
        tuple(_empty_list(host["VolumesFrom"])),
        tuple(_empty_list(host["Devices"])),
        tuple(_empty_list(host["DeviceRequests"])),
        host["PublishAllPorts"],
        _empty_dict(host["PortBindings"]),
        tuple(mounts),
        _empty_dict(network["Ports"]),
    )
    expected = (
        "/" + resource.name,
        dict(resource.labels),
        CONTAINER_USER,
        spec.image.name,
        FIXED_ENV,
        CONTAINER_WORKSPACE,
        (GUEST_EXECUTABLE,),
        ("idle",),
        {},
        {},
        True,
        ("ALL",),
        ("no-new-privileges:true",),
        "none",
        True,
        128,
        MEMORY_BYTES,
        MEMORY_BYTES,
        NANO_CPUS,
        (("nofile", 1024, 1024),),
        False,
        (),
        (),
        (),
        (),
        (),
        False,
        {},
        _expected_mounts(spec),
        {},
    )
    if observed != expected:
        raise InvariantRefusalError("inspect policy drift")
    tmpfs = _dict(host["Tmpfs"])
    if set(tmpfs) != {"/tmp"}:
        raise InvariantRefusalError("inspect policy drift")
    normalized_tmpfs = set(str(tmpfs["/tmp"]).split(","))
    common_tmpfs = {"rw", "nosuid", "nodev", "noexec", "mode=1777"}
    if normalized_tmpfs not in (
        common_tmpfs | {"size=64m"},
        common_tmpfs | {"size=67108864"},
    ):
        raise InvariantRefusalError("inspect policy drift")


__all__ = [
    "BASE_IMAGE_LOCK",
    "CONTAINER_AUTH",
    "CONTAINER_HOME",
    "CONTAINER_TOOL",
    "CONTAINER_USER",
    "CONTAINER_WORKSPACE",
    "DockerCommandBuilder",
    "DockerLabSpec",
    "DockerOperation",
    "FIXED_ENV",
    "FIXTURE_LOCK",
    "GUEST_EXECUTABLE",
    "GuestAction",
    "IMAGE_DIRECTORY",
    "MEMORY_BYTES",
    "NANO_CPUS",
    "SyntheticFixture",
    "VOLUME_TARGETS",
    "classify_docker_argv",
    "validate_inspect",
]
