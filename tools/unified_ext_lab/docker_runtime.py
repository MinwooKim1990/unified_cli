"""Opt-in real-Docker runtime with a finite, identity-bound grammar."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
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
    _parse_env_mapping,
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
_GUEST_DIRECTORY = os.path.dirname(GUEST_EXECUTABLE)
_SNAPSHOT_GUEST_PARTS = ("rootfs", "opt", "unified-ext-lab")
_DERIVED_SNAPSHOT_NAME = "runtime-snapshot"
# Darwin's sys/fcntl.h defines O_SYMLINK with this stable value. Python 3.9
# does not expose the constant even though the kernel supports the flag.
_DARWIN_O_SYMLINK = 0x00200000

# Container-only conformance never loads Buildx. This compatibility constant
# remains exported for callers that audited the former build-based design.
COPIED_CLI_BUILDX_E2E_REQUIRED = False


def _directory_identity(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _require_private_directory(metadata: os.stat_result, description: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        raise InvariantRefusalError("unsafe " + description)


def _synchronize_private_directory(path: str, description: str) -> None:
    """Synchronize one private directory through a no-follow descriptor."""

    try:
        expected = os.lstat(path)
        _require_private_directory(expected, description)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except InvariantRefusalError:
        raise
    except OSError as error:
        raise InvariantRefusalError(
            description + " cannot be synchronized"
        ) from error
    try:
        if _directory_identity(os.fstat(descriptor)) != _directory_identity(expected):
            raise InvariantRefusalError(
                description + " changed during synchronization"
            )
        os.fsync(descriptor)
    except InvariantRefusalError:
        raise
    except OSError as error:
        raise InvariantRefusalError(
            description + " cannot be synchronized"
        ) from error
    finally:
        os.close(descriptor)


def _require_removed_directory_descriptor(
    descriptor: int,
    expected_path: str,
) -> None:
    """Prove that rmdir unlinked the directory pinned by ``descriptor``."""

    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise InvariantRefusalError(
            "derived snapshot changed during removal"
        ) from error
    if metadata.st_nlink == 0:
        return
    if sys.platform == "darwin":
        try:
            import fcntl

            value = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                b"\0" * 1024,
            )
            descriptor_path = os.fsdecode(value.split(b"\0", 1)[0])
        except (AttributeError, OSError, ValueError) as error:
            raise InvariantRefusalError(
                "derived snapshot removal cannot be proven"
            ) from error
        if descriptor_path == expected_path and not os.path.lexists(expected_path):
            return
    elif sys.platform.startswith("linux"):
        try:
            descriptor_path = os.readlink("/proc/self/fd/{}".format(descriptor))
        except OSError as error:
            raise InvariantRefusalError(
                "derived snapshot removal cannot be proven"
            ) from error
        if (
            descriptor_path == expected_path + " (deleted)"
            and not os.path.lexists(expected_path)
        ):
            return
    raise InvariantRefusalError("derived snapshot changed during removal")


def _open_nondirectory_at(
    parent: int,
    name: str,
    expected: os.stat_result,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if stat.S_ISLNK(expected.st_mode):
        if sys.platform == "darwin":
            flags |= getattr(os, "O_SYMLINK", _DARWIN_O_SYMLINK)
        elif sys.platform.startswith("linux") and hasattr(os, "O_PATH"):
            flags |= os.O_PATH | getattr(os, "O_NOFOLLOW", 0)
        else:
            raise InvariantRefusalError(
                "derived snapshot entry cannot be pinned"
            )
    else:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        raise InvariantRefusalError(
            "derived snapshot changed during removal"
        ) from error
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise InvariantRefusalError(
            "derived snapshot changed during removal"
        ) from error
    if _directory_identity(opened) != _directory_identity(expected):
        os.close(descriptor)
        raise InvariantRefusalError("derived snapshot changed during removal")
    return descriptor


def _require_unlinked_descriptor(
    descriptor: int,
    original_links: int,
) -> None:
    try:
        current_links = os.fstat(descriptor).st_nlink
    except OSError as error:
        raise InvariantRefusalError(
            "derived snapshot changed during removal"
        ) from error
    if original_links < 1 or current_links != original_links - 1:
        raise InvariantRefusalError("derived snapshot changed during removal")


class DerivedSnapshotResource:
    """One state-derived private snapshot with no persisted path input."""

    def __init__(self, path: str) -> None:
        if (
            type(path) is not str
            or not os.path.isabs(path)
            or os.path.normpath(path) != path
            or os.path.realpath(path) != path
            or os.path.basename(path) != _DERIVED_SNAPSHOT_NAME
        ):
            raise UsageStateError("invalid derived snapshot path")
        parent = os.path.dirname(path)
        try:
            parent_info = os.lstat(parent)
        except OSError as error:
            raise InvariantRefusalError("derived snapshot parent is unavailable") from error
        _require_private_directory(parent_info, "derived snapshot parent")
        self.path = path
        self._parent = parent
        self._name = _DERIVED_SNAPSHOT_NAME

    def create(self) -> None:
        if os.path.lexists(self.path):
            raise InvariantRefusalError("derived snapshot already exists")
        try:
            os.mkdir(self.path, 0o700)
            os.chmod(self.path, 0o700)
            metadata = os.lstat(self.path)
        except OSError as error:
            raise InvariantRefusalError("derived snapshot cannot be created") from error
        _require_private_directory(metadata, "derived snapshot")
        # First commit the new directory's mode and metadata, then its entry in
        # the durable state directory. snapshot_image_context() resynchronizes
        # this root after populating its complete tree.
        _synchronize_private_directory(self.path, "derived snapshot")
        _synchronize_private_directory(self._parent, "derived snapshot parent")

    def present(self) -> bool:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise InvariantRefusalError("derived snapshot cannot be inspected") from error
        _require_private_directory(metadata, "derived snapshot")
        return True

    @staticmethod
    def _open_directory_at(parent: int, name: str, expected: os.stat_result) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except OSError as error:
            raise InvariantRefusalError("derived snapshot changed during removal") from error
        opened = os.fstat(descriptor)
        if _directory_identity(opened) != _directory_identity(expected):
            os.close(descriptor)
            raise InvariantRefusalError("derived snapshot changed during removal")
        return descriptor

    @classmethod
    def _remove_contents(
        cls,
        descriptor: int,
        device: int,
        expected_path: str,
    ) -> None:
        try:
            names = os.listdir(descriptor)
        except OSError as error:
            raise InvariantRefusalError("derived snapshot cannot be enumerated") from error
        for name in names:
            if type(name) is not str or not name or name in (".", "..") or "/" in name:
                raise InvariantRefusalError("invalid derived snapshot entry")
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise InvariantRefusalError("derived snapshot changed during removal") from error
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                if metadata.st_dev != device:
                    raise InvariantRefusalError("derived snapshot crosses a filesystem")
                child = cls._open_directory_at(descriptor, name, metadata)
                try:
                    child_path = os.path.join(expected_path, name)
                    cls._remove_contents(child, device, child_path)
                    try:
                        current = os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        ) from error
                    if _directory_identity(current) != _directory_identity(metadata):
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        )
                    try:
                        os.rmdir(name, dir_fd=descriptor)
                    except OSError as error:
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        ) from error
                    _require_removed_directory_descriptor(child, child_path)
                finally:
                    os.close(child)
            else:
                if not (
                    stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                ):
                    raise InvariantRefusalError(
                        "unsupported derived snapshot entry"
                    )
                child = _open_nondirectory_at(descriptor, name, metadata)
                try:
                    try:
                        current = os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        ) from error
                    if _directory_identity(current) != _directory_identity(metadata):
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        )
                    try:
                        os.unlink(name, dir_fd=descriptor)
                    except OSError as error:
                        raise InvariantRefusalError(
                            "derived snapshot changed during removal"
                        ) from error
                    _require_unlinked_descriptor(child, metadata.st_nlink)
                finally:
                    os.close(child)
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise InvariantRefusalError("derived snapshot cannot be synchronized") from error

    def remove(self) -> bool:
        try:
            parent_info = os.lstat(self._parent)
        except OSError as error:
            raise InvariantRefusalError("derived snapshot parent is unavailable") from error
        _require_private_directory(parent_info, "derived snapshot parent")
        try:
            parent = os.open(
                self._parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise InvariantRefusalError(
                "derived snapshot parent changed"
            ) from error
        try:
            try:
                opened_parent = os.fstat(parent)
            except OSError as error:
                raise InvariantRefusalError(
                    "derived snapshot parent changed"
                ) from error
            if _directory_identity(opened_parent) != _directory_identity(parent_info):
                raise InvariantRefusalError("derived snapshot parent changed")
            try:
                metadata = os.stat(self._name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError as error:
                raise InvariantRefusalError("derived snapshot cannot be inspected") from error
            _require_private_directory(metadata, "derived snapshot")
            child = self._open_directory_at(parent, self._name, metadata)
            try:
                self._remove_contents(child, metadata.st_dev, self.path)
                try:
                    current = os.stat(
                        self._name,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise InvariantRefusalError(
                        "derived snapshot changed during removal"
                    ) from error
                if _directory_identity(current) != _directory_identity(metadata):
                    raise InvariantRefusalError(
                        "derived snapshot changed during removal"
                    )
                os.rmdir(self._name, dir_fd=parent)
                _require_removed_directory_descriptor(child, self.path)
                os.fsync(parent)
            except InvariantRefusalError:
                raise
            except OSError as error:
                raise InvariantRefusalError(
                    "derived snapshot cannot be removed"
                ) from error
            finally:
                os.close(child)
            return True
        finally:
            os.close(parent)


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
    config = record.get("Config")
    if type(config) is not dict:
        raise InvariantRefusalError("locked base environment drift")
    try:
        _parse_env_mapping(config.get("Env"))
    except InvariantRefusalError as error:
        raise InvariantRefusalError("locked base environment drift") from error
    return MappingProxyType(record)


class _RealDockerIdentityCommands:
    """Shared cleanup/list grammar for one deterministic resource plan."""

    uses_resource_ids = True
    resource_id_roles = (ResourceRole.CONTAINER, ResourceRole.IMAGE)
    cleanup_roles = (ResourceRole.CONTAINER,)
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
        elif role in (
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            noun = "volume"
        else:
            raise UsageStateError("real-Docker resource role is not managed")
        target = (
            self._spec.resource(role).name
            if resource_id is None
            else _resource_id(role, resource_id)
        )
        return self.prefix + (noun, "inspect", target)

    def list_owned(self, role: ResourceRole) -> Tuple[str, ...]:
        if role is ResourceRole.IMAGE:
            noun = "image"
            all_flag = ("--all",)
        elif role is ResourceRole.CONTAINER:
            noun = "container"
            all_flag = ("--all",)
        elif role in (
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            noun = "volume"
            all_flag = ()
        else:
            raise UsageStateError("real-Docker resource role is not managed")
        return self.prefix + (noun, "ls") + all_flag + ("--quiet",) + _label_args(
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
        elif role in (
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            noun = "volume"
            selector = "name=" + resource.name
        else:
            raise UsageStateError("real-Docker resource role is not managed")
        all_flag = ("--all",) if role in self.resource_id_roles else ()
        return self.prefix + (
            noun,
            "ls",
        ) + all_flag + (
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

    def remove_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        if role not in (
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            raise UsageStateError("role is not a managed volume")
        return self.prefix + ("volume", "rm", self._spec.resource(role).name)

    def _validate_identity_inspect(
        self, role: ResourceRole, payload: object
    ) -> Optional[str]:
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
        elif role in (
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            resource_id = None
            if (
                record.get("Name") != resource.name
                or record.get("Labels") != dict(resource.labels)
            ):
                raise InvariantRefusalError("managed volume identity drift")
        else:
            raise UsageStateError("real-Docker resource role is not managed")
        return resource_id

    def validate_inspect(self, role: ResourceRole, payload: object) -> Optional[str]:
        return self._validate_identity_inspect(role, payload)

    def validate_cleanup_inspect(
        self, role: ResourceRole, payload: object
    ) -> Optional[str]:
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
    """Container-only grammar bound to a local base ID and private snapshot."""

    builds_image = False
    cleanup_only = False
    planned_roles = (ResourceRole.CONTAINER,)

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
        self._expected_container_env: Optional[Tuple[str, ...]] = None
        source = os.path.join(spec.context, *_SNAPSHOT_GUEST_PARTS)
        if (
            os.path.realpath(source) != source
            or not os.path.isdir(source)
            or _GUEST_DIRECTORY != "/opt/unified-ext-lab"
        ):
            raise InvariantRefusalError("real-Docker snapshot bind is invalid")
        self._bind_mount = (source, _GUEST_DIRECTORY)

    @property
    def bind_mount(self) -> Tuple[str, str]:
        return self._bind_mount

    def inspect_base_image(self) -> Tuple[str, ...]:
        return self.prefix + ("image", "inspect", self._profile.base_reference)

    def validate_base_image(self, payload: object) -> str:
        record = validate_locked_base_inspect(
            self._profile, payload, expected_id=self._spec.base_image
        )
        base_environment = _parse_env_mapping(record["Config"]["Env"])
        fixed_environment = _parse_env_mapping(list(FIXED_ENV))
        base_environment.update(fixed_environment)
        expected = tuple(
            "{}={}".format(name, value)
            for name, value in base_environment.items()
        )
        if (
            self._expected_container_env is not None
            and _parse_env_mapping(list(self._expected_container_env))
            != _parse_env_mapping(list(expected))
        ):
            raise InvariantRefusalError("locked base environment drift")
        self._expected_container_env = expected
        return _resource_id(ResourceRole.IMAGE, record["Id"])

    def build_image(self) -> Tuple[str, ...]:
        raise UsageStateError("real-Docker conformance does not build images")

    def create_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        raise UsageStateError("real-Docker named volumes are forbidden")

    def create_container(self) -> Tuple[str, ...]:
        # The bind source is a private, hash-locked snapshot. Revalidate it at
        # the last command-construction point before exposing it to Docker.
        self._base.validate_context()
        argv = list(self._base.create_container())
        if argv[:3] != [self._spec.docker_executable, "container", "create"]:
            raise InvariantRefusalError("real-Docker container grammar drift")
        if argv[-2:] != [self._spec.image.name, "idle"]:
            raise InvariantRefusalError("real-Docker container image grammar drift")
        argv[-2] = self._spec.base_image
        try:
            insertion = argv.index("--env")
        except ValueError as error:
            raise InvariantRefusalError("real-Docker container grammar drift") from error
        source, target = self._bind_mount
        argv[insertion:insertion] = [
            "--mount",
            "type=bind,src={},dst={},readonly,bind-propagation=rprivate".format(
                source, target
            ),
        ]
        return self.prefix + (
            "container",
            "create",
            "--pull=never",
            "--platform",
            self._profile.platform,
        ) + tuple(argv[3:])

    def start_container(self, resource_id: str) -> Tuple[str, ...]:
        self._base.validate_context()
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
        # Every guest command executes code through the live read-only bind.
        # Revalidate the complete private snapshot immediately beforehand.
        self._base.validate_context()
        if action is GuestAction.INSTALL:
            self._base.exec_guest(action)
        return super().exec_guest(action, resource_id, extra)

    def remove_volume(self, role: ResourceRole) -> Tuple[str, ...]:
        raise UsageStateError("real-Docker named volumes are forbidden")

    def validate_inspect(self, role: ResourceRole, payload: object) -> str:
        if role is ResourceRole.CONTAINER:
            if self._expected_container_env is None:
                raise InvariantRefusalError(
                    "locked base environment is not bound"
                )
            validate_inspect(
                self._spec,
                role,
                payload,
                container_image=self._spec.base_image,
                bind_mount=self._bind_mount,
                container_env=self._expected_container_env,
            )
        else:
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
        self.cleanup_roles = spec.managed_roles
        self.planned_roles = (
            (
                ResourceRole.IMAGE,
                ResourceRole.CONTAINER,
                ResourceRole.WORKSPACE,
                ResourceRole.AUTH,
                ResourceRole.TOOL,
            )
            if ResourceRole.IMAGE in spec.managed_roles
            else (ResourceRole.CONTAINER,)
        )


class RealDockerRuntime:
    """Run container-only conformance from a local locked base and snapshot."""

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
        self._snapshot_resource: Optional[DerivedSnapshotResource] = None

    @classmethod
    def discover(cls) -> "RealDockerRuntime":
        profile = load_real_docker_profile()
        profile.require_routable()
        executable = discover_docker_executable()
        runner = SubprocessRunner(executable)
        try:
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
        # The lifecycle owns removal of a derived snapshot. Closing the client
        # must not make a leaked execution resource invisible to recovery.
        self._snapshot_root = None
        self._snapshot = None
        self._snapshot_resource = None
        close = getattr(self.runner, "close", None)
        if callable(close):
            close()

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

    def capture_snapshot(self, root: str) -> None:
        if self.cleanup_only or self._local_base_id is None:
            raise UsageStateError("real-Docker preflight is required")
        if self._snapshot_root is not None or self._snapshot_resource is not None:
            raise UsageStateError("real-Docker snapshot is already bound")
        resource = DerivedSnapshotResource(root)
        resource.create()
        self._snapshot_resource = resource
        self._snapshot_root = root
        self._snapshot = snapshot_image_context(root)

    def bind_snapshot_for_cleanup(self, root: str) -> None:
        if not self.cleanup_only:
            raise UsageStateError("cleanup snapshot binding requires cleanup mode")
        if self._snapshot_resource is not None:
            raise UsageStateError("cleanup snapshot is already bound")
        self._snapshot_resource = DerivedSnapshotResource(root)

    @property
    def snapshot_resource(self) -> DerivedSnapshotResource:
        if self._snapshot_resource is None:
            raise UsageStateError("real-Docker snapshot is unavailable")
        return self._snapshot_resource

    def preflight(self) -> None:
        self.probe_daemon()
        if self.cleanup_only:
            return
        self._local_base_id = self.require_local_base()

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
