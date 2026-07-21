"""Offline contracts for the hardened opt-in real-Docker profile."""

from __future__ import annotations

import json
import os
import shutil
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


def base_payload(profile: RealDockerProfile, image_id: str = BASE_ID) -> str:
    return json.dumps(
        [
            {
                "Architecture": profile.architecture,
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
                "Env": list(FIXED_ENV),
                "ExposedPorts": {},
                "Image": self.builder.spec.image.name,
                "Labels": dict(resource.labels),
                "User": CONTAINER_USER,
                "Volumes": {},
                "WorkingDir": "/workspace",
            },
            "HostConfig": {
                "Binds": [],
                "CapAdd": [],
                "CapDrop": ["ALL"],
                "DeviceRequests": [],
                "Devices": [],
                "Init": True,
                "Memory": MEMORY_BYTES,
                "MemorySwap": MEMORY_BYTES,
                "NanoCpus": NANO_CPUS,
                "NetworkMode": "none",
                "PidsLimit": 128,
                "PortBindings": {},
                "Privileged": False,
                "PublishAllPorts": False,
                "ReadonlyRootfs": True,
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": dict(TMPFS_OPTIONS),
                "Ulimits": [{"Hard": 1024, "Name": "nofile", "Soft": 1024}],
                "VolumesFrom": [],
            },
            "Id": container_id,
            "Mounts": [],
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
        if hasattr(self.builder, "build_image") and argv == self.builder.build_image():
            self.images[IMAGE_ID] = self._image_record(IMAGE_ID)
            self.image_name_id = IMAGE_ID
            return self._result(argv, IMAGE_ID + "\n")
        if hasattr(self.builder, "create_container") and argv == self.builder.create_container():
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
            hasattr(self.builder, "start_container")
            and argv == self.builder.start_container(CONTAINER_ID)
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
                if argv == self.builder.exec_guest(action, container_id):
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

    def test_preflight_binds_local_base_and_private_snapshot(self):
        with self.assertRaises(UsageStateError):
            self.runtime.spec(self.identity)
        self.runtime.preflight()
        self.assertEqual(
            self.runner.commands,
            [
                self.runtime.version_argv(),
                self.runtime.buildx_version_argv(),
                self.runtime.inspect_base_argv(),
            ],
        )
        spec = self.runtime.spec(self.identity)
        builder = self.runtime.commands(spec)
        self.assertEqual(spec.base_image, BASE_ID)
        self.assertTrue(spec.context_is_snapshot)
        self.assertTrue(spec.ephemeral_storage)
        self.assertNotEqual(spec.context, IMAGE_DIRECTORY)
        build = builder.build_image()
        self.assertIn("BASE_IMAGE=" + BASE_ID, build)
        self.assertNotIn(IMAGE_DIRECTORY, build)
        self.assertEqual(
            builder.inspect_base_image(),
            self.runtime.prefix + ("image", "inspect", BASE_ID),
        )
        self.assertNotIn("--platform", builder.inspect_base_image())
        create = builder.create_container()
        self.assertNotIn("--mount", create)
        self.assertNotIn("volume", create)
        tmpfs_values = [
            create[index + 1]
            for index, value in enumerate(create[:-1])
            if value == "--tmpfs"
        ]
        self.assertEqual({item.split(":", 1)[0] for item in tmpfs_values}, set(TMPFS_OPTIONS))
        snapshot_root = self.runtime._snapshot_root
        self.runtime.close()
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

    def test_missing_or_inexact_buildx_fails_before_base_and_snapshot(self):
        self.runner.responses[self.runtime.buildx_version_argv()] = RunnerFailureError("missing")
        with self.assertRaises(UnsupportedError):
            self.runtime.preflight()
        self.assertEqual(
            self.runner.commands,
            [self.runtime.version_argv(), self.runtime.buildx_version_argv()],
        )
        self.assertIsNone(self.runtime._snapshot_root)

    def test_discover_binds_buildx_but_cleanup_discovery_does_not(self):
        runner = RecordingRunner()
        with mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_docker_executable",
            return_value=DOCKER,
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.discover_buildx_executable",
            return_value="/fixed/docker-buildx",
        ), mock.patch(
            "tools.unified_ext_lab.docker_runtime.SubprocessRunner", return_value=runner
        ):
            runtime = RealDockerRuntime.discover()
        self.assertEqual(runner.bound_buildx, ["/fixed/docker-buildx"])
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
        self.assertTrue(COPIED_CLI_BUILDX_E2E_REQUIRED)


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
        self.spec = self.runtime.spec(self.identity)
        self.builder = self.runtime.commands(self.spec)
        self.runner = RealModelRunner(self.builder, self.profile)
        self.store = LabStateStore(
            state_parent / REAL_DOCKER_EXECUTION_PROFILE,
            REAL_DOCKER_EXECUTION_PROFILE,
        )
        self.lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            self.runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
        )

    def _forward_to_evidence(self):
        created = self.lifecycle.create()
        self.assertEqual(
            dict(created.resource_ids),
            {"container": CONTAINER_ID, "image": IMAGE_ID},
        )
        self.lifecycle.install()
        self.lifecycle.test()
        return self.lifecycle.evidence()

    def test_real_command_builder_is_accepted_at_lifecycle_construction(self):
        self.assertIsInstance(self.lifecycle, FixtureLifecycle)
        self.assertIs(self.lifecycle.spec, self.spec)
        self.assertIs(self.builder.uses_resource_ids, True)

    def _cleanup_lifecycle(self):
        state = self.lifecycle.status()
        runtime = RealDockerRuntime(
            DOCKER,
            self.runner,
            None,
            timeout=1,
            cleanup_only=True,
        )
        spec = runtime.cleanup_spec(self.identity, state.planned_resources)
        builder = runtime.commands(spec)
        self.assertIsInstance(builder, RealDockerCleanupCommandBuilder)
        self.runner.builder = builder
        lifecycle = FixtureLifecycle(
            self.store,
            spec,
            self.runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=builder,
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
        )
        return lifecycle, sleeps

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
        self.assertIn(IMAGE_ID, flattened)

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
                if argv == inner_self.builder.exec_guest(GuestAction.READY, CONTAINER_ID):
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

    def test_local_base_removed_after_preflight_fails_before_build(self):
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
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        self.assertEqual(runner.commands[-1], self.builder.inspect_base_image())
        self.assertNotIn(self.builder.build_image(), runner.commands)
        self.assertFalse(any("pull" in argv for argv in runner.commands))
        self.assertFalse(any(self.profile.base_reference in argv for argv in runner.commands))

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

    def test_image_replacement_between_inspect_and_delete_is_not_deleted(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        self.runner.race_role = ResourceRole.IMAGE
        self.lifecycle.destroy()
        self.assertNotIn(IMAGE_ID, self.runner.images)
        self.assertIn(REPLACEMENT_IMAGE_ID, self.runner.images)
        dirty, _summary = self.lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

    def test_same_process_cleanup_does_not_depend_on_forward_platform_metadata(self):
        self._forward_to_evidence()
        self.lifecycle.logout()
        self.runner.images[IMAGE_ID]["Architecture"] = "unavailable-after-run"
        destroyed, _summary = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
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
        class PublishThenFailBuild(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.builder.build_image():
                    result = super(PublishThenFailBuild, inner_self).run(
                        argv, timeout=timeout
                    )
                    raise RunnerFailureError("ambiguous build failure")
                return super(PublishThenFailBuild, inner_self).run(
                    argv, timeout=timeout
                )

        runner = PublishThenFailBuild(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        self.assertTrue(lifecycle.status().tainted)
        runner.images[IMAGE_ID]["Config"]["Labels"] = {"changed": "true"}

        destroyed, _summary = lifecycle.destroy()

        self.assertEqual(destroyed.phase, StatePhase.DESTROY_FAILED)
        self.assertIn(IMAGE_ID, runner.images)
        self.assertNotIn(self.builder.remove_image(IMAGE_ID), runner.commands)
        dirty, _verification = lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)

    def test_late_build_publish_is_cleanup_retryable_but_never_promotable(self):
        class FailBeforePublish(RealModelRunner):
            failed = False

            def run(inner_self, argv, *, timeout):
                if argv == inner_self.builder.build_image() and not inner_self.failed:
                    inner_self.failed = True
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("ambiguous build failure")
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
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        self.assertTrue(lifecycle.status().tainted)
        lifecycle.destroy()
        dirty, summary = lifecycle.verify_clean()
        self.assertEqual(dirty.phase, StatePhase.DIRTY)
        self.assertEqual(summary.remaining_count, 0)
        with self.assertRaisesRegex(InvariantRefusalError, "tainted"):
            lifecycle.seal(self.output_parent / "late-build.json")

        runner.images[IMAGE_ID] = runner._image_record(IMAGE_ID)
        runner.image_name_id = IMAGE_ID
        removed, _summary = lifecycle.destroy()
        self.assertEqual(removed.phase, StatePhase.DESTROY_DONE)
        self.assertNotIn(IMAGE_ID, runner.images)
        still_dirty, _summary = lifecycle.verify_clean()
        self.assertEqual(still_dirty.phase, StatePhase.DIRTY)

    def test_late_container_publish_is_cleanup_retryable_but_never_promotable(self):
        class FailBeforePublish(RealModelRunner):
            failed = False

            def run(inner_self, argv, *, timeout):
                if (
                    argv == inner_self.builder.create_container()
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
        )
        with self.assertRaises(RunnerFailureError):
            lifecycle.create()
        state = lifecycle.status()
        self.assertTrue(state.tainted)
        self.assertEqual(state.resource_ids.get("image"), IMAGE_ID)
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
            if role is ResourceRole.IMAGE and not interrupted["done"]:
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
        class FailBuild(RealModelRunner):
            def run(inner_self, argv, *, timeout):
                if argv == inner_self.builder.build_image():
                    inner_self.commands.append(argv)
                    raise RunnerFailureError("ambiguous build failure")
                return super(FailBuild, inner_self).run(argv, timeout=timeout)

        runner = FailBuild(self.builder, self.profile)
        lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            runner,
            execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
            executor_kind="real_docker",
            command_builder=self.builder,
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


if __name__ == "__main__":
    unittest.main()
