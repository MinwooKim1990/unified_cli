"""Offline contracts for the hardened opt-in real-Docker profile."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab import profile as profile_module
from tools.unified_ext_lab.docker import (
    CONTAINER_USER,
    CONTEXT_LOCK,
    FIXED_ENV,
    FIXTURE_LOCK,
    GUEST_EXECUTABLE,
    IMAGE_DIRECTORY,
    MEMORY_BYTES,
    NANO_CPUS,
    TMPFS_OPTIONS,
    DockerCommandBuilder,
    DockerLabSpec,
    GuestAction,
    snapshot_image_context,
)
from tools.unified_ext_lab.docker_runtime import (
    COPIED_CLI_BUILDX_E2E_REQUIRED,
    DerivedSnapshotResource,
    RealDockerCleanupCommandBuilder,
    RealDockerRuntime,
    discover_buildx_executable,
    discover_docker_executable,
    validate_buildx_version,
    validate_locked_base_inspect,
    validate_version_probe,
)
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    RunnerFailureError,
    UnsupportedError,
    UsageStateError,
)
from tools.unified_ext_lab.evidence import strict_evidence_loads
from tools.unified_ext_lab.lifecycle import FixtureLifecycle
from tools.unified_ext_lab.model import LabIdentity, ResourceRole
from tools.unified_ext_lab.profile import (
    FIXED_DOCKER_ENDPOINT,
    FIXED_PLATFORM,
    RealDockerProfile,
    load_real_docker_profile,
)
from tools.unified_ext_lab.runner import CommandResult, SubprocessRunner
from tools.unified_ext_lab.state import (
    REAL_DOCKER_EXECUTION_PROFILE,
    LabStateStore,
    LockedLabStateStore,
    PlannedResource,
    StatePhase,
)


DOCKER = os.path.realpath(sys.executable)
TOKEN = "0123456789abcdef0123456789abcdef"
BASE_ID = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
REPLACEMENT_IMAGE_ID = "sha256:" + "c" * 64
CONTAINER_ID = "d" * 64
REPLACEMENT_CONTAINER_ID = "e" * 64
VERSION_PAYLOAD = json.dumps(
    {"Client": {"Version": "99.0.0"}, "Server": {"Version": "99.0.0"}}
)
BUILDX_PAYLOAD = "github.com/docker/buildx v0.99.1-deadbeef 0123456789abcdef\n"
BASE_ENV = (
    "LANG=C.UTF-8",
    "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305",
    "PYTHON_VERSION=3.12.13",
    "PYTHON_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684",
)
ACTUAL_CONTAINER_ENV = (
    "HOME=/home/lab",
    "PATH=/usr/bin:/bin",
    "TMPDIR=/tmp",
    "XDG_CACHE_HOME=/home/lab/.cache",
    "XDG_CONFIG_HOME=/home/lab/.config",
    "XDG_DATA_HOME=/home/lab/.local/share",
) + BASE_ENV


def base_payload(profile: RealDockerProfile, image_id: str = BASE_ID) -> str:
    return json.dumps(
        [
            {
                "Architecture": profile.architecture,
                "Config": {"Env": list(BASE_ENV)},
                "Id": image_id,
                "Os": profile.operating_system,
                "RepoDigests": [profile.base_reference],
            }
        ]
    )


class RecordingRunner:
    def __init__(self, responses=None) -> None:
        self.responses = {} if responses is None else dict(responses)
        self.commands = []
        self.closed = False
        self.bound_buildx = []

    def bind_docker_buildx(self, executable):
        self.bound_buildx.append(executable)

    def run(self, argv, *, timeout):
        self.commands.append(argv)
        response = self.responses.get(argv, "")
        if isinstance(response, BaseException):
            raise response
        if type(response) is tuple:
            returncode, stdout, stderr = response
        else:
            returncode, stdout, stderr = 0, response, ""
        return CommandResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def close(self) -> None:
        self.closed = True


class RealModelRunner:
    """Small exact-command Docker model with immutable daemon IDs."""

    def __init__(self, builder, profile) -> None:
        self.builder = builder
        self.profile = profile
        self.commands = []
        self.image_name_id = None
        self.container_name_id = None
        self.images = {}
        self.containers = {}
        self.running = set()
        self.ready_after = 0
        self.readiness_attempts = 0
        self.race_role = None
        self.hidden_roles = set()
        self.inspect_id_overrides = {}
        # Snapshot validation is part of forward command construction.  Cache the
        # exact create command while the durable snapshot is present so the
        # cleanup model never reconstructs it after lifecycle-owned removal.
        self.create_argv = (
            self.builder.create_container()
            if hasattr(self.builder, "create_container")
            else None
        )
        self.start_argv = (
            self.builder.start_container(CONTAINER_ID)
            if hasattr(self.builder, "start_container")
            else None
        )
        self.guest_argv = (
            {
                action: self.builder.exec_guest(action, CONTAINER_ID)
                for action in GuestAction
            }
            if hasattr(self.builder, "exec_guest")
            else {}
        )

    def _image_record(self, image_id, *, tagged=True):
        resource = self.builder.spec.resource(ResourceRole.IMAGE)
        return {
            "Architecture": self.profile.architecture,
            "Config": {
                "Cmd": ["idle"],
                "Entrypoint": [GUEST_EXECUTABLE],
                "Env": [],
                "ExposedPorts": {},
                "Labels": dict(resource.labels),
                "User": CONTAINER_USER,
                "Volumes": {},
            },
            "Id": image_id,
            "Os": self.profile.operating_system,
            "RepoTags": [resource.name + ":latest"] if tagged else [],
        }

    def _container_record(self, container_id):
        resource = self.builder.spec.resource(ResourceRole.CONTAINER)
        return {
            "Config": {
                "Cmd": ["idle"],
                "Entrypoint": [GUEST_EXECUTABLE],
                "Env": list(ACTUAL_CONTAINER_ENV),
                "Image": self.builder.spec.base_image,
                "Labels": dict(resource.labels),
                "User": CONTAINER_USER,
                "Volumes": None,
                "WorkingDir": "/workspace",
            },
            "HostConfig": {
                "Binds": None,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "DeviceRequests": None,
                "Devices": [],
                "Init": True,
                "Memory": MEMORY_BYTES,
                "MemorySwap": MEMORY_BYTES,
                "Mounts": [
                    {
                        "BindOptions": {"Propagation": "rprivate"},
                        "ReadOnly": True,
                        "Source": self.builder.bind_mount[0],
                        "Target": self.builder.bind_mount[1],
                        "Type": "bind",
                    }
                ],
                "NanoCpus": NANO_CPUS,
                "NetworkMode": "none",
                "PidsLimit": 128,
                "PortBindings": {},
                "Privileged": False,
                "PublishAllPorts": False,
                "ReadonlyRootfs": True,
                "SecurityOpt": ["no-new-privileges=true"],
                "Tmpfs": dict(TMPFS_OPTIONS),
                "Ulimits": [{"Hard": 1024, "Name": "nofile", "Soft": 1024}],
                "VolumesFrom": None,
            },
            "Id": container_id,
            "Image": self.builder.spec.base_image,
            "Mounts": [
                {
                    "Destination": self.builder.bind_mount[1],
                    "Mode": "",
                    "Propagation": "rprivate",
                    "RW": False,
                    "Source": self.builder.bind_mount[0],
                    "Type": "bind",
                }
            ],
            "Name": "/" + resource.name,
            "NetworkSettings": {"Ports": {}},
        }

    @staticmethod
    def _result(argv, stdout=""):
        return CommandResult(argv=argv, returncode=0, stdout=stdout, stderr="")

    def _list(self, argv):
        for role, current_id, records in (
            (ResourceRole.CONTAINER, self.container_name_id, self.containers),
            (ResourceRole.IMAGE, self.image_name_id, self.images),
        ):
            if argv == self.builder.list_identity(role):
                return self._result(
                    argv,
                    "".join(resource_id + "\n" for resource_id in sorted(records)),
                )
            if argv == self.builder.list_owned(role):
                if current_id is None or role in self.hidden_roles:
                    return self._result(argv)
                resource = self.builder.spec.resource(role)
                labels = records[current_id].get("Config", {}).get("Labels")
                return self._result(
                    argv,
                    current_id + "\n" if labels == dict(resource.labels) else "",
                )
            if argv == self.builder.list_named(role):
                if current_id is None or role in self.hidden_roles:
                    return self._result(argv)
                resource = self.builder.spec.resource(role)
                if role is ResourceRole.IMAGE:
                    named = resource.name + ":latest" in records[current_id].get(
                        "RepoTags", []
                    )
                else:
                    named = records[current_id].get("Name") == "/" + resource.name
                return self._result(
                    argv,
                    current_id + "\n" if named else "",
                )
        return None

    def _replace_after_inspect(self, role):
        if self.race_role is not role:
            return
        self.race_role = None
        if role is ResourceRole.CONTAINER:
            replacement = REPLACEMENT_CONTAINER_ID
            self.containers[replacement] = self._container_record(replacement)
            self.container_name_id = replacement
        else:
            old_id = self.image_name_id
            if old_id in self.images:
                self.images[old_id]["RepoTags"] = []
            replacement = REPLACEMENT_IMAGE_ID
            self.images[replacement] = self._image_record(replacement)
            self.image_name_id = replacement

    def run(self, argv, *, timeout):
        self.commands.append(argv)
        listed = self._list(argv)
        if listed is not None:
            return listed
        if (
            hasattr(self.builder, "inspect_base_image")
            and argv == self.builder.inspect_base_image()
        ):
            return self._result(argv, base_payload(self.profile))
        if self.create_argv is not None and argv == self.create_argv:
            self.containers[CONTAINER_ID] = self._container_record(CONTAINER_ID)
            self.container_name_id = CONTAINER_ID
            return self._result(argv, CONTAINER_ID + "\n")
        for role, current_id, records in (
            (ResourceRole.IMAGE, self.image_name_id, self.images),
            (ResourceRole.CONTAINER, self.container_name_id, self.containers),
        ):
            inspected_id = None
            if argv == self.builder.inspect(role):
                inspected_id = current_id
            else:
                for resource_id in records:
                    if argv == self.builder.inspect(role, resource_id):
                        inspected_id = resource_id
                        break
            if inspected_id is not None:
                record = self.inspect_id_overrides.get(
                    (role, inspected_id), records[inspected_id]
                )
                payload = json.dumps([record], sort_keys=True)
                self._replace_after_inspect(role)
                return self._result(argv, payload)
            if argv == self.builder.inspect(role):
                raise RunnerFailureError("resource is absent")
        if len(argv) >= 2 and argv[-2] == "inspect":
            raise RunnerFailureError("resource is absent")
        if (
            self.start_argv is not None
            and argv == self.start_argv
        ):
            self.running.add(CONTAINER_ID)
            return self._result(argv, CONTAINER_ID + "\n")
        for container_id in tuple(self.containers):
            for action in (
                GuestAction.READY,
                GuestAction.INSTALL,
                GuestAction.TEST,
                GuestAction.LOGOUT,
            ):
                expected_guest = (
                    self.guest_argv[action]
                    if container_id == CONTAINER_ID
                    else self.builder.exec_guest(action, container_id)
                )
                if argv == expected_guest:
                    if container_id not in self.running:
                        raise RunnerFailureError("container is not running")
                    if action is GuestAction.READY:
                        self.readiness_attempts += 1
                        payload = {
                            "action": "ready",
                            "status": (
                                "waiting"
                                if self.readiness_attempts <= self.ready_after
                                else "ready"
                            ),
                        }
                    elif action is GuestAction.TEST:
                        payload = {
                            "artifact": "synthetic-cli-fixture",
                            "marker": True,
                            "protocol": 1,
                            "status": "ok",
                            "version": "1.0.0",
                        }
                    else:
                        payload = {"action": action.value, "status": "ok"}
                    return self._result(argv, json.dumps(payload, sort_keys=True) + "\n")
            if argv == self.builder.stop_container(container_id):
                self.running.discard(container_id)
                return self._result(argv, container_id + "\n")
            if argv == self.builder.remove_container(container_id):
                del self.containers[container_id]
                self.running.discard(container_id)
                if self.container_name_id == container_id:
                    self.container_name_id = None
                return self._result(argv, container_id + "\n")
        for image_id in tuple(self.images):
            if argv == self.builder.remove_image(image_id):
                del self.images[image_id]
                if self.image_name_id == image_id:
                    self.image_name_id = None
                return self._result(argv, image_id + "\n")
        raise AssertionError("unexpected real-Docker command: {!r}".format(argv))


class ProfileAndParserTests(unittest.TestCase):
    def test_profile_reader_is_nonblocking_cloexec_and_detects_path_replacement(self):
        with tempfile.TemporaryDirectory(prefix="profile-reader-") as temporary:
            root = Path(os.path.realpath(temporary))
            path = root / "profile.json"
            replacement = root / "replacement.json"
            payload = profile_module.REAL_DOCKER_PROFILE_LOCK.read_bytes()
            path.write_bytes(payload)
            replacement.write_bytes(payload)
            path.chmod(0o600)
            replacement.chmod(0o600)
            real_open = os.open
            observed_flags = []

            def capture_open(name, flags, *args, **kwargs):
                observed_flags.append(flags)
                return real_open(name, flags, *args, **kwargs)

            with mock.patch.object(
                profile_module, "REAL_DOCKER_PROFILE_LOCK", path
            ), mock.patch.object(profile_module.os, "open", new=capture_open):
                self.assertEqual(profile_module._read_profile_lock(), payload)
            self.assertTrue(observed_flags[-1] & os.O_NONBLOCK)
            self.assertTrue(observed_flags[-1] & os.O_CLOEXEC)

            real_read = os.read
            swapped = {"done": False}

            def swap_after_read(descriptor, count):
                data = real_read(descriptor, count)
                if data and not swapped["done"]:
                    swapped["done"] = True
                    os.replace(replacement, path)
                return data

            with mock.patch.object(
                profile_module, "REAL_DOCKER_PROFILE_LOCK", path
            ), mock.patch.object(profile_module.os, "read", new=swap_after_read):
                with self.assertRaisesRegex(InvariantRefusalError, "lock changed"):
                    profile_module._read_profile_lock()

    def test_profile_and_strict_probe_parsers(self):
        profile = load_real_docker_profile()
        self.assertEqual(profile.execution_profile, REAL_DOCKER_EXECUTION_PROFILE)
        self.assertEqual(profile.docker_endpoint, FIXED_DOCKER_ENDPOINT)
        self.assertEqual(profile.platform, FIXED_PLATFORM)
        self.assertTrue(profile.routable)
        self.assertEqual(validate_version_probe(VERSION_PAYLOAD)["Server"]["Version"], "99.0.0")
        self.assertEqual(validate_buildx_version(BUILDX_PAYLOAD).split()[1], "v0.99.1-deadbeef")
        self.assertEqual(
            validate_locked_base_inspect(profile, base_payload(profile))["Id"],
            BASE_ID,
        )
        for payload in (
            "",
            "buildx v1.2.3 deadbeef\n",
            "github.com/docker/buildx latest deadbeef\n",
        ):
            with self.assertRaises(InvariantRefusalError):
                validate_buildx_version(payload)
        wrong = json.loads(base_payload(profile))
        del wrong[0]["Id"]
        with self.assertRaises(InvariantRefusalError):
            validate_locked_base_inspect(profile, json.dumps(wrong))

    def test_locked_base_environment_is_a_strict_unique_mapping(self):
        profile = load_real_docker_profile()
        invalid_environments = (
            None,
            "LANG=C.UTF-8",
            [None],
            ["MALFORMED"],
            ["9INVALID=value"],
            ["BAD-NAME=value"],
            ["BAD\nNAME=value"],
            ["BAD\x00NAME=value"],
            ["LANG=C.UTF-8", "LANG=wrong"],
        )
        for environment in invalid_environments:
            with self.subTest(environment=environment):
                payload = json.loads(base_payload(profile))
                payload[0]["Config"]["Env"] = environment
                with self.assertRaisesRegex(
                    InvariantRefusalError, "locked base environment drift"
                ):
                    validate_locked_base_inspect(profile, json.dumps(payload))

    def test_non_routable_profile_fails_closed(self):
        profile = load_real_docker_profile()
        held = RealDockerProfile(
            execution_profile=profile.execution_profile,
            docker_endpoint=profile.docker_endpoint,
            platform=profile.platform,
            base_image=profile.base_image,
            base_digest=profile.base_digest,
            source_tag=profile.source_tag,
            source_index_digest=profile.source_index_digest,
            routable=False,
        )
        with self.assertRaises(UnsupportedError):
            held.require_routable()

    def test_discovery_is_fixed_and_ambiguous_identity_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="real-discovery-") as temporary:
            first = Path(temporary) / "one"
            second = Path(temporary) / "two"
            for path in (first, second):
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o700)
            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.DOCKER_CANDIDATES", (str(first),)
            ), mock.patch(
                "tools.unified_ext_lab.docker_runtime.BUILDX_CANDIDATES", (str(second),)
            ), mock.patch.dict(os.environ, {"PATH": "/untrusted"}):
                self.assertEqual(discover_docker_executable(), os.path.realpath(first))
                self.assertEqual(discover_buildx_executable(), os.path.realpath(second))
            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.DOCKER_CANDIDATES",
                (str(first), str(second)),
            ):
                with self.assertRaises(InvariantRefusalError):
                    discover_docker_executable()


class SnapshotAndRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_real_docker_profile()
        self.runner = RecordingRunner()
        self.runtime = RealDockerRuntime(DOCKER, self.runner, self.profile, timeout=1)
        self.runner.responses[self.runtime.version_argv()] = VERSION_PAYLOAD
        self.runner.responses[self.runtime.buildx_version_argv()] = BUILDX_PAYLOAD
        self.runner.responses[self.runtime.inspect_base_argv()] = base_payload(self.profile)
        self.identity = LabIdentity("real-lab", "synthetic", TOKEN)
        self.addCleanup(self.runtime.close)

    def _capture_derived_snapshot(self):
        temporary = tempfile.TemporaryDirectory(prefix="derived-snapshot-")
        self.addCleanup(temporary.cleanup)
        parent = Path(os.path.realpath(temporary.name)) / self.identity.lab_id
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        root = parent / "runtime-snapshot"
        self.runtime.capture_snapshot(str(root))
        return root

    def test_preflight_binds_local_base_and_private_snapshot(self):
        with self.assertRaises(UsageStateError):
            self.runtime.spec(self.identity)
        self.runtime.preflight()
        self.assertEqual(
            self.runner.commands,
            [
                self.runtime.version_argv(),
                self.runtime.inspect_base_argv(),
            ],
        )
        with self.assertRaises(UsageStateError):
            self.runtime.spec(self.identity)
        self._capture_derived_snapshot()
        spec = self.runtime.spec(self.identity)
        builder = self.runtime.commands(spec)
        self.assertEqual(spec.base_image, BASE_ID)
        self.assertTrue(spec.context_is_snapshot)
        self.assertTrue(spec.ephemeral_storage)
        self.assertNotEqual(spec.context, IMAGE_DIRECTORY)
        with self.assertRaisesRegex(UsageStateError, "does not build"):
            builder.build_image()
        self.assertEqual(
            builder.inspect_base_image(),
            self.runtime.prefix + ("image", "inspect", self.profile.base_reference),
        )
        self.assertNotIn("--platform", builder.inspect_base_image())
        create = builder.create_container()
        self.assertIn("--pull=never", create)
        self.assertIn(BASE_ID, create)
        self.assertNotIn(self.profile.base_reference, create)
        self.assertNotIn("BASE_IMAGE=" + BASE_ID, create)
        self.assertIn("--mount", create)
        mount = create[create.index("--mount") + 1]
        self.assertEqual(
            mount,
            "type=bind,src={},dst={},readonly,bind-propagation=rprivate".format(
                *builder.bind_mount
            ),
        )
        self.assertTrue(builder.bind_mount[0].startswith(spec.context + os.sep))
        self.assertEqual(builder.bind_mount[1], "/opt/unified-ext-lab")
        self.assertNotIn("volume", create)
        tmpfs_values = [
            create[index + 1]
            for index, value in enumerate(create[:-1])
            if value == "--tmpfs"
        ]
        self.assertEqual({item.split(":", 1)[0] for item in tmpfs_values}, set(TMPFS_OPTIONS))
        self.assertIn("noexec", TMPFS_OPTIONS["/opt/unified-ext-lab/tool"].split(","))
        self.assertNotIn("exec", TMPFS_OPTIONS["/opt/unified-ext-lab/tool"].split(","))
        snapshot_root = self.runtime._snapshot_root
        snapshot_resource = self.runtime.snapshot_resource
        self.runtime.close()
        self.assertTrue(os.path.exists(snapshot_root))
        self.assertTrue(snapshot_resource.remove())
        self.assertFalse(os.path.exists(snapshot_root))

    def test_snapshot_survives_source_swap_and_rejects_snapshot_mutation(self):
        with tempfile.TemporaryDirectory(prefix="context-source-") as temporary:
            root = Path(os.path.realpath(temporary))
            source = root / "source"
            shutil.copytree(IMAGE_DIRECTORY, source)
            source.chmod(0o755)
            context_lock = root / "context.lock.json"
            fixture_lock = root / "fixture.lock.json"
            shutil.copyfile(CONTEXT_LOCK, context_lock)
            shutil.copyfile(FIXTURE_LOCK, fixture_lock)
            context_lock.chmod(0o600)
            fixture_lock.chmod(0o600)
            private = root / "private"
            private.mkdir(mode=0o700)
            snapshot = snapshot_image_context(
                str(private),
                context=str(source),
                context_lock=str(context_lock),
                fixture_lock=str(fixture_lock),
            )
            locked_dockerfile = Path(snapshot.context) / "Dockerfile"
            expected = locked_dockerfile.read_bytes()
            moved = root / "source-old"
            source.rename(moved)
            source.mkdir(mode=0o755)
            (source / "Dockerfile").write_text("FROM untrusted\n", encoding="utf-8")
            spec = DockerLabSpec.from_snapshot(
                self.identity,
                docker_executable=DOCKER,
                base_image=BASE_ID,
                snapshot=snapshot,
                ephemeral_storage=True,
            )
            argv = DockerCommandBuilder(spec).build_image()
            self.assertEqual(locked_dockerfile.read_bytes(), expected)
            self.assertNotIn(str(source), argv)
            locked_dockerfile.write_text("FROM changed\n", encoding="utf-8")
            with self.assertRaises(InvariantRefusalError):
                DockerCommandBuilder(spec).build_image()

    def test_snapshot_fsyncs_every_entry_bottom_up(self):
        real_fsync = os.fsync
        synchronized = []

        def record_fsync(descriptor):
            metadata = os.fstat(descriptor)
            synchronized.append((metadata.st_dev, metadata.st_ino))
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory(prefix="snapshot-fsync-") as temporary:
            private = Path(os.path.realpath(temporary)) / "private"
            private.mkdir(mode=0o700)
            with mock.patch(
                "tools.unified_ext_lab.docker.os.fsync",
                side_effect=record_fsync,
            ):
                snapshot = snapshot_image_context(str(private))

            entries = [Path(snapshot.context), Path(snapshot.context_lock), private]
            entries.extend(Path(snapshot.context).rglob("*"))
            identities = {
                path: (os.lstat(str(path)).st_dev, os.lstat(str(path)).st_ino)
                for path in entries
            }
            positions = {
                identity: index for index, identity in enumerate(synchronized)
            }
            for path, identity in identities.items():
                self.assertIn(identity, positions, str(path))
                if path.is_dir():
                    for child in path.iterdir():
                        self.assertLess(
                            positions[identities[child]],
                            positions[identity],
                            "{} was not synchronized before {}".format(child, path),
                        )

    def test_derived_snapshot_create_fsyncs_child_then_parent(self):
        real_fsync = os.fsync
        synchronized = []

        def record_fsync(descriptor):
            metadata = os.fstat(descriptor)
            synchronized.append((metadata.st_dev, metadata.st_ino))
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory(prefix="snapshot-create-fsync-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            resource = DerivedSnapshotResource(str(root))
            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.fsync",
                side_effect=record_fsync,
            ):
                resource.create()

            root_info = root.stat()
            parent_info = parent.stat()
            self.assertEqual(
                synchronized,
                [
                    (root_info.st_dev, root_info.st_ino),
                    (parent_info.st_dev, parent_info.st_ino),
                ],
            )

    def test_snapshot_directory_fsync_failure_prevents_binding_and_is_recoverable(self):
        self.runtime.preflight()
        real_fsync = os.fsync
        directory_syncs = {"count": 0}

        def fail_first_snapshot_directory(descriptor):
            metadata = os.fstat(descriptor)
            if stat.S_ISDIR(metadata.st_mode):
                directory_syncs["count"] += 1
                if directory_syncs["count"] == 3:
                    raise OSError("injected directory fsync failure")
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory(prefix="snapshot-fsync-fault-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            with mock.patch(
                "tools.unified_ext_lab.docker.os.fsync",
                side_effect=fail_first_snapshot_directory,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "cannot be synchronized"
                ):
                    self.runtime.capture_snapshot(str(root))

            with self.assertRaisesRegex(UsageStateError, "preflight is required"):
                self.runtime.spec(self.identity)
            self.assertTrue(self.runtime.snapshot_resource.remove())
            self.assertFalse(root.exists())

    def test_derived_snapshot_removal_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-unlink-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            outside = Path(os.path.realpath(temporary)) / "outside"
            outside.mkdir(mode=0o700)
            retained = outside / "retained"
            retained.write_text("keep", encoding="utf-8")
            resource = DerivedSnapshotResource(str(root))
            resource.create()
            nested = root / "nested"
            nested.mkdir(mode=0o700)
            (nested / "payload").write_text("remove", encoding="utf-8")
            (root / "outside-link").symlink_to(outside, target_is_directory=True)

            self.assertTrue(resource.remove())
            self.assertFalse(root.exists())
            self.assertEqual(retained.read_text(encoding="utf-8"), "keep")

    def test_derived_snapshot_removal_refuses_directory_entry_replacement(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-race-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            moved = parent / "moved-snapshot"
            resource = DerivedSnapshotResource(str(root))
            resource.create()
            original_rmdir = os.rmdir
            replaced = {"done": False}

            def replace_before_rmdir(name, *, dir_fd=None):
                if (
                    name == "runtime-snapshot"
                    and dir_fd is not None
                    and not replaced["done"]
                ):
                    replaced["done"] = True
                    os.rename(
                        name,
                        "moved-snapshot",
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    os.mkdir(name, 0o700, dir_fd=dir_fd)
                return original_rmdir(name, dir_fd=dir_fd)

            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.rmdir",
                side_effect=replace_before_rmdir,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError,
                    "changed during removal",
                ):
                    resource.remove()

            self.assertTrue(replaced["done"])
            self.assertFalse(root.exists())
            self.assertTrue(moved.exists())

    def test_derived_snapshot_removal_refuses_nested_directory_replacement(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-nested-race-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            moved = parent / "moved-nested"
            resource = DerivedSnapshotResource(str(root))
            resource.create()
            (root / "nested").mkdir(mode=0o700)
            original_rmdir = os.rmdir
            replaced = {"done": False}

            def replace_before_rmdir(name, *, dir_fd=None):
                if name == "nested" and not replaced["done"]:
                    replaced["done"] = True
                    os.rename(root / name, moved)
                    (root / name).mkdir(mode=0o700)
                return original_rmdir(name, dir_fd=dir_fd)

            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.rmdir",
                side_effect=replace_before_rmdir,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError,
                    "changed during removal",
                ):
                    resource.remove()

            self.assertTrue(replaced["done"])
            self.assertTrue(root.exists())
            self.assertTrue(moved.exists())

    def test_derived_snapshot_removal_refuses_regular_file_replacement(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-file-race-") as temporary:
            parent = Path(os.path.realpath(temporary)) / self.identity.lab_id
            parent.mkdir(mode=0o700)
            root = parent / "runtime-snapshot"
            moved = parent / "moved-payload"
            resource = DerivedSnapshotResource(str(root))
            resource.create()
            payload = root / "payload"
            payload.write_bytes(b"original")
            original_unlink = os.unlink
            replaced = {"done": False}

            def replace_before_unlink(name, *, dir_fd=None):
                if name == "payload" and not replaced["done"]:
                    replaced["done"] = True
                    os.rename(payload, moved)
                    payload.write_bytes(b"replacement")
                return original_unlink(name, dir_fd=dir_fd)

            with mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.unlink",
                side_effect=replace_before_unlink,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError,
                    "changed during removal",
                ):
                    resource.remove()

            self.assertTrue(replaced["done"])
            self.assertTrue(root.exists())
            self.assertEqual(moved.read_bytes(), b"original")

    def test_prepare_base_is_the_only_pull_path(self):
        with self.assertRaises(UsageStateError):
            self.runtime.prepare_base(allow_network=False)
        self.assertEqual(self.runner.commands, [])
        self.runtime.prepare_base(allow_network=True)
        self.assertEqual(
            self.runner.commands,
            [
                self.runtime.version_argv(),
                self.runtime.pull_base_argv(),
                self.runtime.inspect_base_argv(),
            ],
        )

    def test_container_only_preflight_does_not_probe_buildx(self):
        self.runner.responses[self.runtime.buildx_version_argv()] = RunnerFailureError("missing")
        self.runtime.preflight()
        self.assertEqual(
            self.runner.commands,
            [self.runtime.version_argv(), self.runtime.inspect_base_argv()],
        )
        self.assertIsNone(self.runtime._snapshot_root)

    def test_discovery_never_consults_or_binds_buildx(self):
        runner = RecordingRunner()
        with mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_docker_executable",
            return_value=DOCKER,
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_buildx_executable",
            side_effect=AssertionError("forward discovery consulted Buildx"),
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.SubprocessRunner", return_value=runner
        ):
            runtime = RealDockerRuntime.discover()
        self.assertEqual(runner.bound_buildx, [])
        runtime.close()

        cleanup_runner = RecordingRunner()
        with mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_docker_executable",
            return_value=DOCKER,
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_buildx_executable",
            side_effect=AssertionError("cleanup consulted Buildx"),
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.load_real_docker_profile",
            side_effect=AssertionError("cleanup consulted profile"),
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.SubprocessRunner",
            return_value=cleanup_runner,
        ):
            cleanup = RealDockerRuntime.discover_cleanup()
        self.assertEqual(cleanup_runner.bound_buildx, [])
        cleanup.close()

    def test_buildx_companion_is_copied_into_private_docker_config(self):
        with tempfile.TemporaryDirectory(prefix="buildx-identity-") as temporary:
            plugin = Path(temporary) / "docker-buildx"
            plugin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            plugin.chmod(0o700)
            runner = SubprocessRunner(DOCKER)
            self.addCleanup(runner.close)
            identity = runner.bind_docker_buildx(os.path.realpath(plugin))
            environment = dict(runner.private_environment)
            copied = Path(environment["DOCKER_CONFIG"]) / "cli-plugins" / "docker-buildx"
            self.assertEqual(copied.read_bytes(), plugin.read_bytes())
            self.assertEqual(identity.canonical_path, os.path.realpath(plugin))
            plugin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            runner._check_identity(require_buildx=False)
            with self.assertRaises(InvariantRefusalError):
                runner._check_identity()
        self.assertFalse(COPIED_CLI_BUILDX_E2E_REQUIRED)


class RealLifecycleAndCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="real-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(os.path.realpath(self.temporary.name))
        state_parent = self.root / "state"
        state_parent.mkdir(mode=0o700)
        self.output_parent = self.root / "evidence"
        self.output_parent.mkdir(mode=0o700)
        self.profile = load_real_docker_profile()
        probe = RecordingRunner()
        self.runtime = RealDockerRuntime(DOCKER, probe, self.profile, timeout=1)
        self.addCleanup(self.runtime.close)
        probe.responses[self.runtime.version_argv()] = VERSION_PAYLOAD
        probe.responses[self.runtime.buildx_version_argv()] = BUILDX_PAYLOAD
        probe.responses[self.runtime.inspect_base_argv()] = base_payload(self.profile)
        self.runtime.preflight()
        self.identity = LabIdentity("real-lab", "synthetic", TOKEN)
        self.store = LabStateStore(
            state_parent / REAL_DOCKER_EXECUTION_PROFILE,
            REAL_DOCKER_EXECUTION_PROFILE,
        )
        with self.store.locked(self.identity.lab_id) as locked:
            locked.create_initial(
                self.identity.provider_id,
                self.identity.ownership_token,
                (
                    PlannedResource.from_value(
                        self.identity.resource(ResourceRole.CONTAINER)
                    ),
                ),
                {"runtime_snapshot_bound": False},
            )
        self.snapshot_root = (
            self.store.root / self.identity.lab_id / "runtime-snapshot"
        )
        self.runtime.capture_snapshot(str(self.snapshot_root))
        self.spec = self.runtime.spec(self.identity)
        self.builder = self.runtime.commands(self.spec)
        self.runner = RealModelRunner(self.builder, self.profile)
        self.lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            self.runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        self.lifecycle.bind_runtime_snapshot_intent()

    def _forward_to_evidence(self):
        created = self.lifecycle.create()
        self.assertEqual(
            dict(created.resource_ids),
            {"container": CONTAINER_ID},
        )
        self.lifecycle.install()
        self.lifecycle.test()
        return self.lifecycle.evidence()

    def test_real_command_builder_is_accepted_at_lifecycle_construction(self):
        self.assertIsInstance(self.lifecycle, FixtureLifecycle)
        self.assertIs(self.lifecycle.spec, self.spec)
        self.assertIs(self.builder.uses_resource_ids, True)
        self.assertIs(self.builder.builds_image, False)
        self.assertEqual(self.builder.cleanup_roles, (ResourceRole.CONTAINER,))

    def _cleanup_lifecycle(self):
        state = self.lifecycle.status()
        runtime = RealDockerRuntime(
            DOCKER,
            self.runner,
            None,
            timeout=1,
            cleanup_only=True,
        )
        runtime.bind_snapshot_for_cleanup(str(self.snapshot_root))
        spec = runtime.cleanup_spec(self.identity, state.planned_resources)
        builder = runtime.commands(spec)
        self.assertIsInstance(builder, RealDockerCleanupCommandBuilder)
        self.assertEqual(builder.cleanup_roles, (ResourceRole.CONTAINER,))
        self.assertEqual(builder.planned_roles, (ResourceRole.CONTAINER,))
        self.runner.builder = builder
        lifecycle = FixtureLifecycle(
            self.store,
            spec,
            self.runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=builder,
            runtime_snapshot=runtime.snapshot_resource,
        )
        return runtime, lifecycle

    def _make_readiness_lifecycle(self, *, tick: float = 0.5):
        clock = {"value": 0.0}
        sleeps = []

        def monotonic():
            value = clock["value"]
            clock["value"] += tick
            return value

        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            self.runner,
            readiness_monotonic=monotonic,
            readiness_sleep=sleeps.append,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        return lifecycle, sleeps

    def _assert_snapshot_cleanup_hold(self, patcher, residue):
        self._forward_to_evidence()
        self.lifecycle.logout()

        class PatchFirstRemoval:
            def __init__(inner_self, resource):
                inner_self.resource = resource
                inner_self.used = False

            def remove(inner_self):
                if not inner_self.used:
                    inner_self.used = True
                    with patcher:
                        return inner_self.resource.remove()
                return inner_self.resource.remove()

            def present(inner_self):
                return inner_self.resource.present()

        self.lifecycle._runtime_snapshot = PatchFirstRemoval(
            self.runtime.snapshot_resource
        )
        failed, _summary = self.lifecycle.destroy()
        self.assertEqual(failed.phase, StatePhase.DESTROY_FAILED)
        self.assertTrue(failed.tainted)
        self.assertTrue(residue.exists())

        first_verification, _summary = self.lifecycle.verify_clean()
        self.assertEqual(first_verification.phase, StatePhase.DIRTY)
        self.assertTrue(first_verification.tainted)
        retried, _summary = self.lifecycle.destroy()
        self.assertEqual(retried.phase, StatePhase.DESTROY_DONE)
        self.assertTrue(retried.tainted)
        dirty, _summary = self.lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)
        self.assertTrue(dirty.tainted)

    def test_real_lifecycle_uses_ids_tmpfs_and_persisted_artifact(self):
        captured = self._forward_to_evidence()
        self.assertEqual(captured.artifact_evidence["version"], "1.0.0")
        self.lifecycle.logout()
        runtime, cleanup = self._cleanup_lifecycle()
        self.addCleanup(runtime.close)
        cleanup.destroy()
        clean, summary = cleanup.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(summary.remaining_count, 0)
        output = self.output_parent / "passed.json"
        final = cleanup.seal(output)
        manifest = strict_evidence_loads(output.read_bytes())
        self.assertEqual(final.phase, StatePhase.PASSED)
        self.assertEqual(manifest["executor_kind"], "real_docker")
        self.assertFalse(manifest["promotion_eligible"])
        flattened = tuple(argument for argv in self.runner.commands for argument in argv)
        self.assertNotIn("volume", flattened)
        self.assertIn(CONTAINER_ID, flattened)
        self.assertNotIn(IMAGE_ID, flattened)
        self.assertFalse(self.snapshot_root.exists())

    def test_top_snapshot_replacement_persists_cleanup_hold_across_retry(self):
        moved = self.root / "moved-snapshot"
        original_rmdir = os.rmdir
        replaced = {"done": False}

        def replace_before_rmdir(name, *, dir_fd=None):
            if name == "runtime-snapshot" and not replaced["done"]:
                replaced["done"] = True
                os.rename(self.snapshot_root, moved)
                self.snapshot_root.mkdir(mode=0o700)
            return original_rmdir(name, dir_fd=dir_fd)

        self._assert_snapshot_cleanup_hold(
            mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.rmdir",
                side_effect=replace_before_rmdir,
            ),
            moved,
        )
        self.assertTrue(replaced["done"])

    def test_nested_snapshot_replacement_persists_cleanup_hold_across_retry(self):
        nested = (
            self.snapshot_root
            / "image-context"
            / "rootfs"
            / "opt"
            / "unified-ext-lab"
            / "fixtures"
        )
        moved = self.root / "moved-fixtures"
        original_rmdir = os.rmdir
        replaced = {"done": False}

        def replace_before_rmdir(name, *, dir_fd=None):
            if name == "fixtures" and not replaced["done"]:
                replaced["done"] = True
                os.rename(nested, moved)
                nested.mkdir(mode=0o700)
            return original_rmdir(name, dir_fd=dir_fd)

        self._assert_snapshot_cleanup_hold(
            mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.rmdir",
                side_effect=replace_before_rmdir,
            ),
            moved,
        )
        self.assertTrue(replaced["done"])

    def test_file_snapshot_replacement_persists_cleanup_hold_across_retry(self):
        payload = (
            self.snapshot_root
            / "image-context"
            / "rootfs"
            / "opt"
            / "unified-ext-lab"
            / "guest.py"
        )
        moved = self.root / "moved-guest.py"
        original_unlink = os.unlink
        replaced = {"done": False}

        def replace_before_unlink(name, *, dir_fd=None):
            if name == "guest.py" and not replaced["done"]:
                replaced["done"] = True
                os.rename(payload, moved)
                payload.write_bytes(b"replacement")
            return original_unlink(name, dir_fd=dir_fd)

        self._assert_snapshot_cleanup_hold(
            mock.patch(
                "tools.unified_ext_lab.docker_runtime.os.unlink",
                side_effect=replace_before_unlink,
            ),
            moved,
        )
        self.assertTrue(replaced["done"])

    def test_create_waits_for_delayed_workspace_readiness_before_guest_work(self):
        self.runner.ready_after = 2
        lifecycle, sleeps = self._make_readiness_lifecycle()

        self.assertEqual(lifecycle.create().phase, StatePhase.CREATED)

        ready = self.builder.exec_guest(GuestAction.READY, CONTAINER_ID)
        install = self.builder.exec_guest(GuestAction.INSTALL, CONTAINER_ID)
        tested = self.builder.exec_guest(GuestAction.TEST, CONTAINER_ID)
        self.assertEqual(self.runner.readiness_attempts, 3)
        self.assertEqual(self.runner.commands.count(ready), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertLess(
            self.runner.commands.index(self.builder.start_container(CONTAINER_ID)),
            self.runner.commands.index(ready),
        )
        self.assertNotIn(install, self.runner.commands)
        self.assertNotIn(tested, self.runner.commands)

    def test_readiness_timeout_enters_cleanup_only_recovery_without_guest_work(self):
        self.runner.ready_after = 999
        lifecycle, sleeps = self._make_readiness_lifecycle()

        with self.assertRaisesRegex(RunnerFailureError, "readiness timed out"):
            lifecycle.create()

        failed = lifecycle.status()
        self.assertEqual(failed.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(failed.pending_step, "create")
        self.assertEqual(failed.operations[-1].step, "create")
        self.assertEqual(failed.operations[-1].error_code, "timeout")
        self.assertLessEqual(len(sleeps), 4)
        self.assertNotIn(
            self.builder.exec_guest(GuestAction.INSTALL, CONTAINER_ID),
            self.runner.commands,
        )
        self.assertNotIn(
            self.builder.exec_guest(GuestAction.TEST, CONTAINER_ID),
            self.runner.commands,
        )
        runtime, cleanup = self._cleanup_lifecycle()
        self.addCleanup(runtime.close)
        cleanup.destroy()
        clean, summary = cleanup.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(summary.remaining_count, 0)

    def test_readiness_cancellation_enters_cleanup_only_recovery(self):
        class CancelDuringReady(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.guest_argv[GuestAction.READY]:
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("runner cancelled")
                return super(CancelDuringReady, inner_self).run(argv, timeout=timeout)

        runner = CancelDuringReady(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )

        with self.assertRaisesRegex(RunnerFailureError, "runner cancelled"):
            lifecycle.create()

        failed = lifecycle.status()
        self.assertEqual(failed.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(failed.operations[-1].error_code, "runner_failure")
        self.assertNotIn(
            self.builder.exec_guest(GuestAction.INSTALL, CONTAINER_ID), runner.commands
        )
        self.assertNotIn(
            self.builder.exec_guest(GuestAction.TEST, CONTAINER_ID), runner.commands
        )

    def test_local_base_removed_after_preflight_fails_before_container_create(self):
        class MissingBaseRunner(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.builder.inspect_base_image():
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("local base vanished")
                return super(MissingBaseRunner, inner_self).run(argv, timeout=timeout)

        runner = MissingBaseRunner(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        self.assertEqual(runner.commands[-1], self.builder.inspect_base_image())
        self.assertNotIn(self.builder.create_container(), runner.commands)
        self.assertFalse(any("pull" in argv for argv in runner.commands))
        self.assertEqual(
            runner.commands.count(self.builder.inspect_base_image()),
            1,
        )

    def test_container_replacement_between_inspect_and_delete_is_not_deleted(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        self.runner.race_role = ResourceRole.CONTAINER
        _state, summary = self.lifecycle.destroy()
        self.assertGreaterEqual(summary.removed_count, 1)
        self.assertNotIn(CONTAINER_ID, self.runner.containers)
        self.assertIn(REPLACEMENT_CONTAINER_ID, self.runner.containers)
        dirty, verification = self.lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)
        self.assertGreater(verification.remaining_count, 0)
        retried, _summary = self.lifecycle.destroy()
        self.assertEqual(retried.phase, StatePhase.DESTROY_DONE)
        self.assertIn(REPLACEMENT_CONTAINER_ID, self.runner.containers)
        self.assertNotIn(
            self.builder.remove_container(REPLACEMENT_CONTAINER_ID),
            self.runner.commands,
        )

    def test_locked_base_image_is_never_managed_or_removed(self):
        captured = self._forward_to_evidence()
        self.assertNotIn("image", captured.resource_ids)
        self.assertNotIn("image", captured.created_roles)
        self.assertEqual(self.runner.images, {})
        self.assertFalse(any("build" in argv for argv in self.runner.commands))
        self.assertFalse(any("image" in argv and "rm" in argv for argv in self.runner.commands))

    def test_container_runtime_image_id_drift_is_refused(self):
        self.builder.validate_base_image(base_payload(self.profile))
        record = self.runner._container_record(CONTAINER_ID)
        record["Image"] = REPLACEMENT_IMAGE_ID

        with self.assertRaisesRegex(InvariantRefusalError, "inspect policy drift"):
            self.builder.validate_inspect(
                ResourceRole.CONTAINER,
                json.dumps([record]),
            )

    def test_container_inspect_requires_bound_base_environment(self):
        record = self.runner._container_record(CONTAINER_ID)
        with self.assertRaisesRegex(
            InvariantRefusalError, "base environment is not bound"
        ):
            self.builder.validate_inspect(
                ResourceRole.CONTAINER,
                json.dumps([record]),
            )

    def test_actual_engine_environment_shape_and_reordering_pass(self):
        self.builder.validate_base_image(base_payload(self.profile))
        record = self.runner._container_record(CONTAINER_ID)
        self.assertEqual(tuple(record["Config"]["Env"]), ACTUAL_CONTAINER_ENV)
        self.builder.validate_inspect(
            ResourceRole.CONTAINER,
            json.dumps([record]),
        )
        record["Config"]["Env"].reverse()
        self.builder.validate_inspect(
            ResourceRole.CONTAINER,
            json.dumps([record]),
        )

    def test_actual_engine_container_inspect_shape_is_modeled(self):
        record = self.runner._container_record(CONTAINER_ID)

        self.assertNotIn("ExposedPorts", record["Config"])
        self.assertIsNone(record["Config"]["Volumes"])
        for key in ("Binds", "CapAdd", "DeviceRequests", "VolumesFrom"):
            with self.subTest(key=key):
                self.assertIsNone(record["HostConfig"][key])
        self.assertEqual(
            record["HostConfig"]["SecurityOpt"],
            ["no-new-privileges=true"],
        )
        self.assertEqual(
            record["Mounts"],
            [
                {
                    "Destination": self.builder.bind_mount[1],
                    "Mode": "",
                    "Propagation": "rprivate",
                    "RW": False,
                    "Source": self.builder.bind_mount[0],
                    "Type": "bind",
                }
            ],
        )

    def test_container_exposed_ports_accepts_only_empty_engine_shapes(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)

        self.builder.validate_inspect(
            ResourceRole.CONTAINER,
            json.dumps([baseline]),
        )
        for exposed_ports in (None, {}):
            with self.subTest(accepted=exposed_ports):
                record = json.loads(json.dumps(baseline))
                record["Config"]["ExposedPorts"] = exposed_ports
                self.builder.validate_inspect(
                    ResourceRole.CONTAINER,
                    json.dumps([record]),
                )
        for exposed_ports in (
            {"8080/tcp": {}},
            [],
            "",
            0,
            False,
        ):
            with self.subTest(rejected=exposed_ports):
                record = json.loads(json.dumps(baseline))
                record["Config"]["ExposedPorts"] = exposed_ports
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_container_config_still_requires_every_other_policy_field(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)

        for key in (
            "Cmd",
            "Entrypoint",
            "Env",
            "Image",
            "Labels",
            "User",
            "Volumes",
            "WorkingDir",
        ):
            with self.subTest(key=key):
                record = json.loads(json.dumps(baseline))
                del record["Config"][key]
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_container_top_level_bind_is_required_exactly_once(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)
        variants = {}

        missing = json.loads(json.dumps(baseline))
        missing["Mounts"] = []
        variants["missing"] = missing
        omitted = json.loads(json.dumps(baseline))
        del omitted["Mounts"]
        variants["omitted"] = omitted
        duplicate = json.loads(json.dumps(baseline))
        duplicate["Mounts"].append(dict(duplicate["Mounts"][0]))
        variants["duplicate"] = duplicate
        wrong_source = json.loads(json.dumps(baseline))
        wrong_source["Mounts"][0]["Source"] = "/private/wrong-source"
        variants["wrong_source"] = wrong_source
        wrong_destination = json.loads(json.dumps(baseline))
        wrong_destination["Mounts"][0]["Destination"] = "/wrong-destination"
        variants["wrong_destination"] = wrong_destination
        writable = json.loads(json.dumps(baseline))
        writable["Mounts"][0]["RW"] = True
        variants["writable"] = writable
        shared = json.loads(json.dumps(baseline))
        shared["Mounts"][0]["Propagation"] = "rshared"
        variants["shared"] = shared
        missing_mode = json.loads(json.dumps(baseline))
        del missing_mode["Mounts"][0]["Mode"]
        variants["missing_mode"] = missing_mode
        nonempty_mode = json.loads(json.dumps(baseline))
        nonempty_mode["Mounts"][0]["Mode"] = "ro"
        variants["nonempty_mode"] = nonempty_mode
        unexpected_key = json.loads(json.dumps(baseline))
        unexpected_key["Mounts"][0]["Name"] = "unexpected"
        variants["unexpected_key"] = unexpected_key

        for name, record in variants.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_container_host_bind_drift_and_duplication_are_refused(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)
        variants = {}

        missing = json.loads(json.dumps(baseline))
        missing["HostConfig"]["Mounts"] = []
        variants["missing"] = missing
        omitted = json.loads(json.dumps(baseline))
        del omitted["HostConfig"]["Mounts"]
        variants["omitted"] = omitted
        duplicate = json.loads(json.dumps(baseline))
        duplicate["HostConfig"]["Mounts"].append(
            json.loads(json.dumps(duplicate["HostConfig"]["Mounts"][0]))
        )
        variants["duplicate"] = duplicate
        wrong_source = json.loads(json.dumps(baseline))
        wrong_source["HostConfig"]["Mounts"][0]["Source"] = (
            "/private/wrong-source"
        )
        variants["wrong_source"] = wrong_source
        writable = json.loads(json.dumps(baseline))
        writable["HostConfig"]["Mounts"][0]["ReadOnly"] = False
        variants["writable"] = writable
        shared = json.loads(json.dumps(baseline))
        shared["HostConfig"]["Mounts"][0]["BindOptions"]["Propagation"] = (
            "rshared"
        )
        variants["shared"] = shared
        missing_target = json.loads(json.dumps(baseline))
        del missing_target["HostConfig"]["Mounts"][0]["Target"]
        variants["missing_target"] = missing_target
        unexpected_key = json.loads(json.dumps(baseline))
        unexpected_key["HostConfig"]["Mounts"][0]["Consistency"] = "default"
        variants["unexpected_key"] = unexpected_key
        missing_bind_option = json.loads(json.dumps(baseline))
        missing_bind_option["HostConfig"]["Mounts"][0]["BindOptions"] = {}
        variants["missing_bind_option"] = missing_bind_option
        extra_bind_option = json.loads(json.dumps(baseline))
        extra_bind_option["HostConfig"]["Mounts"][0]["BindOptions"][
            "NonRecursive"
        ] = False
        variants["extra_bind_option"] = extra_bind_option

        for name, record in variants.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_fixed_environment_overrides_same_named_base_values(self):
        payload = json.loads(base_payload(self.profile))
        payload[0]["Config"]["Env"].append("PATH=/untrusted/base/path")
        self.builder.validate_base_image(json.dumps(payload))
        record = self.runner._container_record(CONTAINER_ID)
        self.builder.validate_inspect(
            ResourceRole.CONTAINER,
            json.dumps([record]),
        )
        record["Config"]["Env"] = [
            "PATH=/untrusted/base/path" if value == "PATH=/usr/bin:/bin" else value
            for value in record["Config"]["Env"]
        ]
        with self.assertRaisesRegex(InvariantRefusalError, "inspect policy drift"):
            self.builder.validate_inspect(
                ResourceRole.CONTAINER,
                json.dumps([record]),
            )

    def test_container_environment_drift_is_refused_exactly(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)
        environments = {
            "duplicate": list(ACTUAL_CONTAINER_ENV) + ["HOME=/other"],
            "unknown": list(ACTUAL_CONTAINER_ENV) + ["UNKNOWN=value"],
            "missing": list(ACTUAL_CONTAINER_ENV[:-1]),
            "wrong": [
                "LANG=wrong" if value == "LANG=C.UTF-8" else value
                for value in ACTUAL_CONTAINER_ENV
            ],
            "missing_equals": list(ACTUAL_CONTAINER_ENV) + ["MALFORMED"],
            "invalid_name": list(ACTUAL_CONTAINER_ENV) + ["9INVALID=value"],
            "newline": list(ACTUAL_CONTAINER_ENV) + ["BAD=value\nnext"],
            "nul": list(ACTUAL_CONTAINER_ENV) + ["BAD=value\x00next"],
            "non_string": list(ACTUAL_CONTAINER_ENV) + [None],
        }
        for name, environment in environments.items():
            with self.subTest(name=name):
                record = json.loads(json.dumps(baseline))
                record["Config"]["Env"] = environment
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )
        for malformed in (None, "HOME=/home/lab", {}):
            with self.subTest(container=type(malformed).__name__):
                record = json.loads(json.dumps(baseline))
                record["Config"]["Env"] = malformed
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_security_option_equivalent_spellings_and_drift(self):
        self.builder.validate_base_image(base_payload(self.profile))
        baseline = self.runner._container_record(CONTAINER_ID)
        for option in ("no-new-privileges=true", "no-new-privileges:true"):
            with self.subTest(option=option):
                record = json.loads(json.dumps(baseline))
                record["HostConfig"]["SecurityOpt"] = [option]
                self.builder.validate_inspect(
                    ResourceRole.CONTAINER,
                    json.dumps([record]),
                )
        for options in (
            ["no-new-privileges=false"],
            ["no-new-privileges:false"],
            ["no-new-privileges"],
            ["no-new-privileges=true", "seccomp=unconfined"],
            [],
            None,
        ):
            with self.subTest(options=options):
                record = json.loads(json.dumps(baseline))
                record["HostConfig"]["SecurityOpt"] = options
                with self.assertRaisesRegex(
                    InvariantRefusalError, "inspect policy drift"
                ):
                    self.builder.validate_inspect(
                        ResourceRole.CONTAINER,
                        json.dumps([record]),
                    )

    def test_create_records_identity_starts_and_reaches_ready_untainted(self):
        created = self.lifecycle.create()
        self.assertEqual(created.phase, StatePhase.CREATED)
        self.assertEqual(created.created_roles, ("container",))
        self.assertEqual(dict(created.resource_ids), {"container": CONTAINER_ID})
        self.assertFalse(created.tainted)
        create = self.builder.create_container()
        inspect_container = self.builder.inspect(ResourceRole.CONTAINER)
        start = self.builder.start_container(CONTAINER_ID)
        ready = self.builder.exec_guest(GuestAction.READY, CONTAINER_ID)
        self.assertLess(
            self.runner.commands.index(create),
            self.runner.commands.index(inspect_container),
        )
        self.assertLess(
            self.runner.commands.index(inspect_container),
            self.runner.commands.index(start),
        )
        self.assertLess(
            self.runner.commands.index(start), self.runner.commands.index(ready)
        )
        self.assertEqual(self.runner.readiness_attempts, 1)

    def test_same_process_cleanup_does_not_reinspect_the_locked_base(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        base_inspects = self.runner.commands.count(self.builder.inspect_base_image())
        destroyed, _summary = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
        self.assertEqual(
            self.runner.commands.count(self.builder.inspect_base_image()),
            base_inspects,
        )
        clean, verification = self.lifecycle.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(verification.remaining_count, 0)

    def test_exact_id_mismatch_never_removes_the_persisted_or_other_id(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        replacement = self.runner._container_record(REPLACEMENT_CONTAINER_ID)
        self.runner.containers[REPLACEMENT_CONTAINER_ID] = replacement
        self.runner.inspect_id_overrides[
            (ResourceRole.CONTAINER, CONTAINER_ID)
        ] = replacement

        destroyed, _summary = self.lifecycle.destroy()

        self.assertEqual(destroyed.phase, StatePhase.DESTROY_FAILED)
        self.assertIn(CONTAINER_ID, self.runner.containers)
        self.assertIn(REPLACEMENT_CONTAINER_ID, self.runner.containers)
        self.assertNotIn(
            self.builder.remove_container(REPLACEMENT_CONTAINER_ID),
            self.runner.commands,
        )

    def test_no_durable_id_retains_name_and_label_validation(self):
        class PublishThenFailCreate(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.create_argv:
                    result = super(PublishThenFailCreate, inner_self).run(
                        argv, timeout=timeout
                    )
                    raise RunnerFailureError("ambiguous container-create failure")
                return super(PublishThenFailCreate, inner_self).run(
                    argv, timeout=timeout
                )

        runner = PublishThenFailCreate(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        self.assertTrue(lifecycle.status().tainted)
        runner.containers[CONTAINER_ID]["Config"]["Labels"] = {"changed": "true"}

        destroyed, _summary = lifecycle.destroy()

        self.assertEqual(destroyed.phase, StatePhase.DESTROY_FAILED)
        self.assertIn(CONTAINER_ID, runner.containers)
        self.assertNotIn(self.builder.remove_container(CONTAINER_ID), runner.commands)
        dirty, _verification = lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

    def test_late_container_publish_is_cleanup_retryable_but_never_promotable(self):
        class FailBeforePublish(RealModelRunner):
            failed = False

            def run(inner_self, argv, *, timeout):
                if (
                    argv == inner_self.create_argv
                    and not inner_self.failed
                ):
                    inner_self.failed = True
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("ambiguous container-create failure")
                return super(FailBeforePublish, inner_self).run(
                    argv, timeout=timeout
                )

        runner = FailBeforePublish(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        state = lifecycle.status()
        self.assertTrue(state.tainted)
        self.assertEqual(dict(state.resource_ids), {})
        lifecycle.destroy()
        dirty, _summary = lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

        runner.containers[CONTAINER_ID] = runner._container_record(CONTAINER_ID)
        runner.container_name_id = CONTAINER_ID
        removed, _summary = lifecycle.destroy()
        self.assertEqual(removed.phase, StatePhase.DESTROY_DONE)
        self.assertNotIn(CONTAINER_ID, runner.containers)
        still_dirty, _summary = lifecycle.verify_clean()
        self.assertEqual(still_dirty.phase, StatePhase.DIRTY)

    def test_state_write_interruption_inside_mutation_window_taints(self):
        original = LockedLabStateStore.record_resource_id
        interrupted = {"done": False}

        def interrupt_record(locked, expected, role, resource_id):
            if role is ResourceRole.CONTAINER and not interrupted["done"]:
                interrupted["done"] = True
                raise KeyboardInterrupt("injected state-write interruption")
            return original(locked, expected, role, resource_id)

        with mock.patch.object(
            LockedLabStateStore, "record_resource_id", new=interrupt_record
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "state-write"):
                self.lifecycle.create()

        state = self.lifecycle.status()
        self.assertEqual(state.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertTrue(state.tainted)
        self.lifecycle.destroy()
        dirty, _summary = self.lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

    def test_recovery_infers_unresolved_create_window_before_cleanup(self):
        class FailCreate(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.create_argv:
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("ambiguous container-create failure")
                return super(FailCreate, inner_self).run(argv, timeout=timeout)

        runner = FailCreate(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
            runtime_snapshot=self.runtime.snapshot_resource,
        )
        original = LockedLabStateStore.mark_tainted
        interrupted = {"done": False}

        def interrupt_first_taint(locked, expected):
            if not interrupted["done"]:
                interrupted["done"] = True
                raise KeyboardInterrupt("injected taint-write interruption")
            return original(locked, expected)

        with mock.patch.object(
            LockedLabStateStore, "mark_tainted", new=interrupt_first_taint
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "taint-write"):
                lifecycle.create()

        recovered = lifecycle.status()
        self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(recovered.pending_step, "create")
        self.assertFalse(recovered.tainted)
        destroyed, _summary = lifecycle.destroy()
        self.assertTrue(destroyed.tainted)
        dirty, _summary = lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

    def test_persisted_id_cleanup_ignores_label_and_name_drift(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        self.runner.hidden_roles.add(ResourceRole.CONTAINER)
        container = self.runner.containers[CONTAINER_ID]
        container["Name"] = "/renamed-container"
        container["Config"]["Labels"] = {"changed": "true"}
        destroyed, _summary = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
        self.assertNotIn(CONTAINER_ID, self.runner.containers)
        clean, verification = self.lifecycle.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(verification.remaining_count, 0)

    def test_cleanup_spec_rejects_persisted_plan_drift(self):
        state = self._forward_to_evidence()
        cleanup = RealDockerRuntime(
            DOCKER, RecordingRunner(), None, cleanup_only=True
        )
        self.addCleanup(cleanup.close)
        planned = list(state.planned_resources)
        first = planned[0]
        planned[0] = PlannedResource(
            role=first.role,
            name=first.name + "-drift",
            labels=first.labels,
        )
        with self.assertRaises(InvariantRefusalError):
            cleanup.cleanup_spec(self.identity, tuple(planned))

    def test_legacy_five_resource_cleanup_recovers_without_snapshot(self):
        legacy_roles = (
            ResourceRole.IMAGE,
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        )
        cleanup_order = (
            ResourceRole.CONTAINER,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
            ResourceRole.WORKSPACE,
            ResourceRole.IMAGE,
        )
        planned = tuple(
            PlannedResource.from_value(self.identity.resource(role))
            for role in legacy_roles
        )

        with tempfile.TemporaryDirectory(prefix="legacy-real-cleanup-") as temporary:
            store = LabStateStore(
                Path(os.path.realpath(temporary)) / REAL_DOCKER_EXECUTION_PROFILE,
                REAL_DOCKER_EXECUTION_PROFILE,
            )
            with store.locked(self.identity.lab_id) as locked:
                locked.create_initial(
                    self.identity.provider_id,
                    self.identity.ownership_token,
                    planned,
                    {"runtime_snapshot_bound": False},
                )
                locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
                locked.record_resource_id(
                    StatePhase.CREATE_PENDING, ResourceRole.IMAGE, IMAGE_ID
                )
                locked.record_resource_id(
                    StatePhase.CREATE_PENDING, ResourceRole.CONTAINER, CONTAINER_ID
                )
                for role in legacy_roles:
                    locked.record_owned_role(StatePhase.CREATE_PENDING, role)
                locked.transition(StatePhase.CREATE_PENDING, StatePhase.CREATED)
                locked.interrupt_stable_forward(StatePhase.CREATED)

            runtime = RealDockerRuntime(
                DOCKER, RecordingRunner(), None, timeout=1, cleanup_only=True
            )
            self.addCleanup(runtime.close)
            snapshot_path = store.root / self.identity.lab_id / "runtime-snapshot"
            runtime.bind_snapshot_for_cleanup(str(snapshot_path))
            self.assertFalse(runtime.snapshot_resource.present())
            spec = runtime.cleanup_spec(self.identity, planned)
            builder = runtime.commands(spec)
            self.assertEqual(builder.cleanup_roles, cleanup_order)
            self.assertEqual(builder.planned_roles, legacy_roles)

            class LegacyCleanupRunner:
                def __init__(inner_self):
                    inner_self.commands = []
                    inner_self.present = set(cleanup_order)

                def _record(inner_self, role):
                    resource = spec.resource(role)
                    if role is ResourceRole.IMAGE:
                        return {
                            "Config": {"Labels": dict(resource.labels)},
                            "Id": IMAGE_ID,
                            "RepoTags": [resource.name + ":latest"],
                        }
                    if role is ResourceRole.CONTAINER:
                        return {
                            "Config": {"Labels": dict(resource.labels)},
                            "Id": CONTAINER_ID,
                            "Name": "/" + resource.name,
                        }
                    return {
                        "Labels": dict(resource.labels),
                        "Name": resource.name,
                    }

                def run(inner_self, argv, *, timeout):
                    inner_self.commands.append(argv)
                    stdout = ""
                    for role in cleanup_order:
                        resource_id = {
                            ResourceRole.IMAGE: IMAGE_ID,
                            ResourceRole.CONTAINER: CONTAINER_ID,
                        }.get(role, spec.resource(role).name)
                        if argv in (
                            builder.list_owned(role),
                            builder.list_named(role),
                        ):
                            stdout = (
                                resource_id + "\n"
                                if role in inner_self.present
                                else ""
                            )
                            break
                        if (
                            role in builder.resource_id_roles
                            and argv == builder.list_identity(role)
                        ):
                            if role is ResourceRole.IMAGE:
                                values = [BASE_ID]
                                if role in inner_self.present:
                                    values.append(IMAGE_ID)
                                stdout = "\n".join(values) + "\n"
                            elif role in inner_self.present:
                                stdout = CONTAINER_ID + "\n"
                            break
                        inspect_argvs = [builder.inspect(role)]
                        if role in builder.resource_id_roles:
                            inspect_argvs.append(builder.inspect(role, resource_id))
                        if argv in inspect_argvs:
                            stdout = json.dumps([inner_self._record(role)])
                            break
                        if (
                            role is ResourceRole.CONTAINER
                            and argv == builder.stop_container(CONTAINER_ID)
                        ):
                            break
                        if (
                            role is ResourceRole.CONTAINER
                            and argv == builder.remove_container(CONTAINER_ID)
                        ):
                            inner_self.present.remove(role)
                            break
                        if (
                            role is ResourceRole.IMAGE
                            and argv == builder.remove_image(IMAGE_ID)
                        ):
                            inner_self.present.remove(role)
                            break
                        if (
                            role not in builder.resource_id_roles
                            and argv == builder.remove_volume(role)
                        ):
                            inner_self.present.remove(role)
                            break
                    else:
                        raise AssertionError(
                            "unexpected legacy cleanup argv: {!r}".format(argv)
                        )
                    return CommandResult(
                        argv=argv, returncode=0, stdout=stdout, stderr=""
                    )

            runner = LegacyCleanupRunner()
            lifecycle = FixtureLifecycle(
                store,
                spec,
                runner,
                execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
                executor_kind="real_docker",
                command_builder=builder,
                runtime_snapshot=runtime.snapshot_resource,
            )
            destroyed, summary = lifecycle.destroy()
            self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
            self.assertEqual(summary.removed_count, 5)
            clean, verification = lifecycle.verify_clean()
            self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
            self.assertEqual(verification.remaining_count, 0)
            self.assertFalse(runner.present)

            removals = [
                builder.remove_container(CONTAINER_ID),
                builder.remove_volume(ResourceRole.AUTH),
                builder.remove_volume(ResourceRole.TOOL),
                builder.remove_volume(ResourceRole.WORKSPACE),
                builder.remove_image(IMAGE_ID),
            ]
            self.assertTrue(all(argv in runner.commands for argv in removals))
            self.assertTrue(
                all(
                    BASE_ID not in argv
                    and self.profile.base_reference not in argv
                    for argv in removals
                )
            )


if __name__ == "__main__":
    unittest.main()
