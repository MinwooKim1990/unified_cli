"""Stateful in-memory Docker grammar simulator for the offline fixture CLI.

This fake recognizes only the finite argv grammar from ``docker.py``. It does
not start a subprocess, Docker daemon, network client, or provider executable.
"""

from __future__ import annotations

import copy
import json
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from tools.unified_ext_lab.docker import (
    CONTAINER_USER,
    FIXED_ENV,
    GUEST_EXECUTABLE,
    MEMORY_BYTES,
    NANO_CPUS,
    DockerCommandBuilder,
    DockerLabSpec,
    DockerOperation,
    classify_docker_argv,
)
from tools.unified_ext_lab.errors import RunnerFailureError, UsageStateError
from tools.unified_ext_lab.runner import CommandResult


def _option_values(argv: Tuple[str, ...], option: str) -> List[str]:
    values = []
    for index, value in enumerate(argv[:-1]):
        if value == option:
            values.append(argv[index + 1])
    return values


def _one_option(argv: Tuple[str, ...], option: str) -> str:
    values = _option_values(argv, option)
    if len(values) != 1:
        raise UsageStateError("fake received malformed Docker command")
    return values[0]


def _parsed_labels(argv: Tuple[str, ...]) -> Dict[str, str]:
    labels = {}
    for item in _option_values(argv, "--label"):
        key, separator, value = item.partition("=")
        if not separator or not key or key in labels:
            raise UsageStateError("fake received malformed Docker labels")
        labels[key] = value
    return labels


class FakeRunner:
    """A deterministic Docker state machine with explicit failure injection."""

    def __init__(self, spec: Optional[DockerLabSpec] = None) -> None:
        if spec is not None and type(spec) is not DockerLabSpec:
            raise UsageStateError("fake runner requires one exact Docker spec")
        self._operations: Dict[Tuple[str, ...], DockerOperation] = {}
        if spec is not None:
            self.register_spec(spec)
        self.commands: List[Tuple[str, ...]] = []
        self.images: Dict[str, Dict[str, object]] = {}
        self.containers: Dict[str, Dict[str, object]] = {}
        self.volumes: Dict[str, Dict[str, object]] = {}
        self.running = set()
        self.guest_actions: List[Tuple[str, str]] = []
        self._fail_before: Dict[DockerOperation, int] = {}
        self._fail_after: Dict[DockerOperation, int] = {}

    def register_spec(self, spec: DockerLabSpec) -> None:
        """Add another immutable grammar for cross-lab ownership tests."""

        if type(spec) is not DockerLabSpec:
            raise UsageStateError("fake runner requires one exact Docker spec")
        additions = dict(DockerCommandBuilder(spec).command_operations())
        for command, operation in additions.items():
            existing = self._operations.get(command)
            if existing is not None and existing is not operation:
                raise UsageStateError("fake command grammar conflicts")
        self._operations.update(additions)

    def inject_failure(
        self, operation: DockerOperation, *, when: str = "before", count: int = 1
    ) -> None:
        if not isinstance(operation, DockerOperation):
            raise UsageStateError("invalid fake failure operation")
        if when not in ("before", "after") or type(count) is not int or count < 1:
            raise UsageStateError("invalid fake failure injection")
        target = self._fail_before if when == "before" else self._fail_after
        target[operation] = target.get(operation, 0) + count

    @staticmethod
    def _take_failure(table: Dict[DockerOperation, int], operation: DockerOperation) -> bool:
        remaining = table.get(operation, 0)
        if remaining < 1:
            return False
        if remaining == 1:
            del table[operation]
        else:
            table[operation] = remaining - 1
        return True

    def forge_labels(
        self, kind: str, name: str, labels: Mapping[str, str]
    ) -> None:
        record = self._records(kind).get(name)
        if record is None:
            raise UsageStateError("fake resource does not exist")
        copied = dict(labels)
        if kind == "image":
            config = record["Config"]
            assert isinstance(config, dict)
            config["Labels"] = copied
        else:
            record["Labels"] = copied
            if kind == "container":
                config = record["Config"]
                assert isinstance(config, dict)
                config["Labels"] = copied

    def add_residue(
        self,
        kind: str,
        name: str,
        labels: Mapping[str, str],
        record: Optional[Dict[str, object]] = None,
    ) -> None:
        target = self._records(kind)
        if name in target:
            raise UsageStateError("fake resource already exists")
        if record is None:
            if kind == "volume":
                record = {
                    "Name": name,
                    "Labels": dict(labels),
                    "Driver": "local",
                    "Options": {},
                    "Scope": "local",
                }
            elif kind == "image":
                record = {
                    "RepoTags": [name + ":latest"],
                    "Config": {"Labels": dict(labels)},
                }
            else:
                record = {"Name": "/" + name, "Config": {"Labels": dict(labels)}}
        target[name] = copy.deepcopy(record)

    def _records(self, kind: str) -> Dict[str, Dict[str, object]]:
        if kind == "image":
            return self.images
        if kind == "container":
            return self.containers
        if kind == "volume":
            return self.volumes
        raise UsageStateError("invalid fake resource kind")

    def run(self, argv: Tuple[str, ...], *, timeout: float) -> CommandResult:
        if type(argv) is not tuple or not argv or type(timeout) not in (int, float) or timeout <= 0:
            raise UsageStateError("invalid fake runner call")
        operation = classify_docker_argv(argv, self._operations)
        self.commands.append(argv)
        if self._take_failure(self._fail_before, operation):
            raise RunnerFailureError("injected fake failure before operation")
        stdout = self._apply(operation, argv)
        if self._take_failure(self._fail_after, operation):
            raise RunnerFailureError("injected fake failure after operation")
        return CommandResult(argv=argv, returncode=0, stdout=stdout, stderr="")

    def _apply(self, operation: DockerOperation, argv: Tuple[str, ...]) -> str:
        if operation is DockerOperation.INSPECT_BASE_IMAGE:
            return json.dumps(
                [{"RepoDigests": [argv[-1]]}],
                sort_keys=True,
                separators=(",", ":"),
            )
        if operation is DockerOperation.BUILD_IMAGE:
            return self._build_image(argv)
        if operation is DockerOperation.CREATE_VOLUME:
            return self._create_volume(argv)
        if operation is DockerOperation.CREATE_CONTAINER:
            return self._create_container(argv)
        if operation is DockerOperation.START_CONTAINER:
            name = argv[-1]
            self._require(self.containers, name)
            self.running.add(name)
            return name + "\n"
        if operation in (
            DockerOperation.INSPECT_IMAGE,
            DockerOperation.INSPECT_CONTAINER,
            DockerOperation.INSPECT_VOLUME,
        ):
            return self._inspect(operation, argv[-1])
        if operation is DockerOperation.EXEC_GUEST:
            return self._exec_guest(argv)
        if operation is DockerOperation.STOP_CONTAINER:
            name = argv[-1]
            self._require(self.containers, name)
            self.running.discard(name)
            return name + "\n"
        if operation is DockerOperation.REMOVE_CONTAINER:
            name = argv[-1]
            self._require(self.containers, name)
            del self.containers[name]
            self.running.discard(name)
            return name + "\n"
        if operation is DockerOperation.REMOVE_VOLUME:
            name = argv[-1]
            self._require(self.volumes, name)
            del self.volumes[name]
            return name + "\n"
        if operation is DockerOperation.REMOVE_IMAGE:
            name = argv[-1]
            self._require(self.images, name)
            del self.images[name]
            return name + "\n"
        if operation in (
            DockerOperation.LIST_IMAGE,
            DockerOperation.LIST_CONTAINER,
            DockerOperation.LIST_VOLUME,
        ):
            return self._list(operation, argv)
        raise UsageStateError("fake operation is not implemented")

    @staticmethod
    def _require(records: Mapping[str, object], name: str) -> None:
        if name not in records:
            raise RunnerFailureError("fake resource is absent")

    def _build_image(self, argv: Tuple[str, ...]) -> str:
        name = _one_option(argv, "--tag")
        # Docker tagging replaces an existing matching reference. The
        # lifecycle must therefore prove exact-name absence before build.
        self.images[name] = {
            "RepoTags": [name + ":latest"],
            "Config": {
                "Labels": _parsed_labels(argv),
                "User": CONTAINER_USER,
                "Entrypoint": [GUEST_EXECUTABLE],
                "Cmd": ["idle"],
                "Env": [],
                "ExposedPorts": {},
                "Volumes": {},
            },
        }
        return "synthetic-image-id\n"

    def _create_volume(self, argv: Tuple[str, ...]) -> str:
        name = argv[-1]
        if name in self.volumes:
            raise RunnerFailureError("fake volume already exists")
        self.volumes[name] = {
            "Name": name,
            "Labels": _parsed_labels(argv),
            "Driver": "local",
            "Options": {},
            "Scope": "local",
        }
        return name + "\n"

    def _create_container(self, argv: Tuple[str, ...]) -> str:
        name = _one_option(argv, "--name")
        if name in self.containers:
            raise RunnerFailureError("fake container already exists")
        mounts = []
        for item in _option_values(argv, "--mount"):
            fields = dict(part.split("=", 1) for part in item.split(","))
            mounts.append(
                {
                    "Type": fields["type"],
                    "Name": fields["src"],
                    "Destination": fields["dst"],
                    "RW": True,
                }
            )
        self.containers[name] = {
            "Name": "/" + name,
            "Labels": _parsed_labels(argv),
            "Config": {
                "Labels": _parsed_labels(argv),
                "User": _one_option(argv, "--user"),
                "Image": argv[-2],
                "Env": _option_values(argv, "--env"),
                "WorkingDir": _one_option(argv, "--workdir"),
                "Entrypoint": [_one_option(argv, "--entrypoint")],
                "Cmd": [argv[-1]],
                "ExposedPorts": {},
                "Volumes": {},
            },
            "HostConfig": {
                "ReadonlyRootfs": "--read-only" in argv,
                "CapDrop": _option_values(argv, "--cap-drop"),
                "SecurityOpt": ["no-new-privileges:true"],
                "NetworkMode": _one_option(argv, "--network"),
                "Init": "--init" in argv,
                "PidsLimit": int(_one_option(argv, "--pids-limit")),
                "Memory": MEMORY_BYTES,
                "MemorySwap": MEMORY_BYTES,
                "NanoCpus": NANO_CPUS,
                "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
                "Privileged": False,
                "CapAdd": [],
                "Binds": [],
                "VolumesFrom": [],
                "Devices": [],
                "DeviceRequests": [],
                "PublishAllPorts": False,
                "PortBindings": {},
                "Tmpfs": {
                    "/tmp": "rw,nosuid,nodev,noexec,size=67108864,mode=1777"
                },
            },
            "Mounts": mounts,
            "NetworkSettings": {
                "Ports": {},
                "Networks": {
                    "none": {
                        "Aliases": None,
                        "DNSNames": None,
                        "DriverOpts": None,
                        "Gateway": "",
                        "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0,
                        "IPAddress": "",
                        "IPAMConfig": None,
                        "IPPrefixLen": 0,
                        "IPv6Gateway": "",
                        "Links": None,
                        "MacAddress": "",
                    }
                },
            },
        }
        return "synthetic-container-id\n"

    def _inspect(self, operation: DockerOperation, name: str) -> str:
        if operation is DockerOperation.INSPECT_IMAGE:
            records = self.images
        elif operation is DockerOperation.INSPECT_CONTAINER:
            records = self.containers
        else:
            records = self.volumes
        self._require(records, name)
        return json.dumps([records[name]], sort_keys=True, separators=(",", ":"))

    def _exec_guest(self, argv: Tuple[str, ...]) -> str:
        if argv[-2] != GUEST_EXECUTABLE or argv[-1] not in ("ready", "install", "test", "logout"):
            raise UsageStateError("fake received arbitrary guest command")
        candidates = [item for item in argv if item in self.containers]
        if len(candidates) != 1:
            raise UsageStateError("fake received malformed guest command")
        name = candidates[0]
        if name not in self.running:
            raise RunnerFailureError("fake container is not running")
        self.guest_actions.append((name, argv[-1]))
        if argv[-1] == "ready":
            return json.dumps(
                {"action": "ready", "status": "ready"},
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        if argv[-1] == "test":
            return json.dumps(
                {
                    "artifact": "synthetic-cli-fixture",
                    "marker": True,
                    "protocol": 1,
                    "status": "ok",
                    "version": "1.0.0",
                },
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        return json.dumps(
            {"action": argv[-1], "status": "ok"},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"

    def _list(self, operation: DockerOperation, argv: Tuple[str, ...]) -> str:
        if operation is DockerOperation.LIST_IMAGE:
            records = self.images
            kind = "image"
        elif operation is DockerOperation.LIST_CONTAINER:
            records = self.containers
            kind = "container"
        else:
            records = self.volumes
            kind = "volume"
        filters = {}
        name_filter = None
        reference_filter = None
        for item in _option_values(argv, "--filter"):
            if item.startswith("label="):
                key, separator, value = item[len("label=") :].partition("=")
                if not separator:
                    raise UsageStateError("fake received malformed list filter")
                filters[key] = value
            elif item.startswith("name=") and kind in ("container", "volume"):
                name_filter = item[len("name=") :]
            elif item.startswith("reference=") and kind == "image":
                reference_filter = item[len("reference=") :]
            else:
                raise UsageStateError("fake received unsafe list filter")
        names = []
        for name, record in records.items():
            if kind == "image":
                config = record["Config"]
                assert isinstance(config, dict)
                labels = config["Labels"]
            elif kind == "container":
                config = record["Config"]
                assert isinstance(config, dict)
                labels = config["Labels"]
            else:
                labels = record["Labels"]
            assert isinstance(labels, dict)
            if name_filter is not None and name != name_filter:
                continue
            if reference_filter is not None:
                tags = record.get("RepoTags")
                if not isinstance(tags, list) or reference_filter not in tags:
                    continue
            if all(labels.get(key) == value for key, value in filters.items()):
                names.append(name)
        return "".join(name + "\n" for name in sorted(names))


__all__ = ["FakeRunner"]
