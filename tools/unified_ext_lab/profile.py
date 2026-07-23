"""Finite, source-controlled execution profile for the real-Docker slice."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import InvariantRefusalError, UnsupportedError, UsageStateError
from .state import REAL_DOCKER_EXECUTION_PROFILE, strict_json_loads


MODULE_DIRECTORY = Path(__file__).resolve().parent
REAL_DOCKER_PROFILE_LOCK = MODULE_DIRECTORY / "locks" / "real-docker-profile.v1.json"
FIXED_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
FIXED_PLATFORM = "linux/amd64"
_MAX_PROFILE_BYTES = 64 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]{0,127}$")
_TAG_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")


def _exact_stat_identity(metadata: os.stat_result) -> tuple:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _exact(value: object, keys: frozenset, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value.keys()) != keys:
        raise InvariantRefusalError("invalid {}".format(field))
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise InvariantRefusalError("invalid {}".format(field))
    return value


@dataclass(frozen=True)
class RealDockerProfile:
    execution_profile: str
    docker_endpoint: str
    platform: str
    base_image: str
    base_digest: str
    source_tag: str
    source_index_digest: str
    routable: bool

    def __post_init__(self) -> None:
        if self.execution_profile != REAL_DOCKER_EXECUTION_PROFILE:
            raise InvariantRefusalError("invalid real-Docker execution profile")
        if self.docker_endpoint != FIXED_DOCKER_ENDPOINT:
            raise InvariantRefusalError("invalid real-Docker endpoint")
        if self.platform != FIXED_PLATFORM:
            raise InvariantRefusalError("invalid real-Docker platform")
        if type(self.base_image) is not str or _IMAGE_RE.fullmatch(self.base_image) is None:
            raise InvariantRefusalError("invalid real-Docker base image")
        _digest(self.base_digest, "real-Docker base digest")
        if type(self.source_tag) is not str or _TAG_RE.fullmatch(self.source_tag) is None:
            raise InvariantRefusalError("invalid real-Docker source tag")
        _digest(self.source_index_digest, "real-Docker source index digest")
        if type(self.routable) is not bool:
            raise InvariantRefusalError("invalid real-Docker routing hold")

    @property
    def base_reference(self) -> str:
        return "{}@{}".format(self.base_image, self.base_digest)

    @property
    def operating_system(self) -> str:
        return self.platform.split("/", 1)[0]

    @property
    def architecture(self) -> str:
        return self.platform.split("/", 1)[1]

    def require_routable(self) -> None:
        if not self.routable:
            raise UnsupportedError("real-Docker profile is held")

    @classmethod
    def from_mapping(cls, value: object) -> "RealDockerProfile":
        data = _exact(
            value,
            frozenset(
                (
                    "schema",
                    "execution_profile",
                    "docker_endpoint",
                    "platform",
                    "base",
                    "routable",
                )
            ),
            "real-Docker profile lock",
        )
        if data["schema"] != 1:
            raise InvariantRefusalError("invalid real-Docker profile schema")
        base = _exact(
            data["base"],
            frozenset(("image", "digest", "source_tag", "source_index_digest")),
            "real-Docker base lock",
        )
        return cls(
            execution_profile=data["execution_profile"],
            docker_endpoint=data["docker_endpoint"],
            platform=data["platform"],
            base_image=base["image"],
            base_digest=base["digest"],
            source_tag=base["source_tag"],
            source_index_digest=base["source_index_digest"],
            routable=data["routable"],
        )


def _read_profile_lock() -> bytes:
    path = REAL_DOCKER_PROFILE_LOCK
    try:
        before = path.lstat()
    except OSError as error:
        raise UnsupportedError("real-Docker profile lock is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > _MAX_PROFILE_BYTES
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (
            hasattr(os, "geteuid")
            and before.st_uid not in (0, os.geteuid())
        )
    ):
        raise InvariantRefusalError("unsafe real-Docker profile lock")
    try:
        descriptor = os.open(
            str(path),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise InvariantRefusalError(
            "real-Docker profile lock changed"
        ) from error
    try:
        opened = os.fstat(descriptor)
        before_identity = _exact_stat_identity(before)
        opened_identity = _exact_stat_identity(opened)
        if opened_identity != before_identity:
            raise InvariantRefusalError("real-Docker profile lock changed")
        chunks = []
        remaining = _MAX_PROFILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            named_after = path.lstat()
        except OSError as error:
            raise InvariantRefusalError(
                "real-Docker profile lock changed"
            ) from error
        if (
            len(payload) > _MAX_PROFILE_BYTES
            or _exact_stat_identity(after) != opened_identity
            or _exact_stat_identity(named_after) != opened_identity
        ):
            raise InvariantRefusalError("real-Docker profile lock changed")
        return payload
    finally:
        os.close(descriptor)


def load_real_docker_profile() -> RealDockerProfile:
    try:
        value = strict_json_loads(_read_profile_lock(), _MAX_PROFILE_BYTES)
    except UsageStateError as error:
        raise InvariantRefusalError("invalid real-Docker profile lock") from error
    return RealDockerProfile.from_mapping(value)


__all__ = [
    "FIXED_DOCKER_ENDPOINT",
    "FIXED_PLATFORM",
    "REAL_DOCKER_PROFILE_LOCK",
    "RealDockerProfile",
    "load_real_docker_profile",
]
