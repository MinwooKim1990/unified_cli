"""Exact command and policy tests for the offline Docker scaffold."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from tools.unified_ext_lab import docker as docker_module
from tools.unified_ext_lab.docker import (
    CONTAINER_AUTH,
    CONTAINER_TOOL,
    CONTAINER_USER,
    CONTAINER_WORKSPACE,
    FIXED_ENV,
    GUEST_EXECUTABLE,
    DockerCommandBuilder,
    DockerLabSpec,
    DockerOperation,
    GuestAction,
    classify_docker_argv,
    validate_base_image_inspect,
    validate_inspect,
)
from tools.unified_ext_lab.errors import InvariantRefusalError, RunnerFailureError
from tools.unified_ext_lab.model import LABEL_PREFIX, LabIdentity, ResourceRole
from tools.unified_ext_lab.tests.fake_runner import FakeRunner


TOKEN_A = "0123456789abcdef0123456789abcdef"
TOKEN_B = "abcdef0123456789abcdef0123456789"
DOCKER = "/usr/bin/docker"


def make_spec(lab_id: str = "lab-a", token: str = TOKEN_A) -> DockerLabSpec:
    return DockerLabSpec.from_locks(
        LabIdentity(lab_id, "synthetic", token), docker_executable=DOCKER
    )


def execute_create(fake: FakeRunner, builder: DockerCommandBuilder) -> None:
    base = fake.run(builder.inspect_base_image(), timeout=1)
    validate_base_image_inspect(builder.spec, base.stdout)
    fake.run(builder.build_image(), timeout=1)
    for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL):
        fake.run(builder.create_volume(role), timeout=1)
    fake.run(builder.create_container(), timeout=1)
    fake.run(builder.start_container(), timeout=1)


class CommandSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = make_spec()
        self.builder = DockerCommandBuilder(self.spec)

    def test_build_argv_is_hermetic_pinned_and_uses_only_image_context(self):
        image = self.spec.image
        expected = (
            DOCKER,
            "build",
            "--pull=false",
            "--network",
            "none",
            "--no-cache",
            "--file",
            os.path.join(self.spec.context, "Dockerfile"),
            "--build-arg",
            "BASE_IMAGE=" + self.spec.base_image,
            "--tag",
            image.name,
        )
        for key, value in image.labels.items():
            expected += ("--label", key + "=" + value)
        expected += (self.spec.context,)
        self.assertEqual(self.builder.build_image(), expected)
        self.assertIn("@sha256:", self.spec.base_image)
        self.assertEqual(self.builder.build_image()[-1], self.spec.context)

    def test_volume_and_container_argv_snapshots(self):
        workspace = self.spec.resource(ResourceRole.WORKSPACE)
        expected_volume = (DOCKER, "volume", "create")
        for key, value in workspace.labels.items():
            expected_volume += ("--label", key + "=" + value)
        expected_volume += (workspace.name,)
        self.assertEqual(self.builder.create_volume(ResourceRole.WORKSPACE), expected_volume)

        container = self.spec.resource(ResourceRole.CONTAINER)
        expected_container = [DOCKER, "container", "create", "--name", container.name]
        for key, value in container.labels.items():
            expected_container.extend(("--label", key + "=" + value))
        expected_container.extend(
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
        for role, target in (
            (ResourceRole.WORKSPACE, CONTAINER_WORKSPACE),
            (ResourceRole.AUTH, CONTAINER_AUTH),
            (ResourceRole.TOOL, CONTAINER_TOOL),
        ):
            mount = "type=volume,src={},dst={}".format(
                self.spec.resource(role).name, target
            )
            expected_container.extend(
                (
                    "--mount",
                    mount,
                )
            )
        for value in FIXED_ENV:
            expected_container.extend(("--env", value))
        expected_container.extend(
            (
                "--workdir",
                CONTAINER_WORKSPACE,
                "--entrypoint",
                GUEST_EXECUTABLE,
                self.spec.image.name,
                "idle",
            )
        )
        self.assertEqual(self.builder.create_container(), tuple(expected_container))

    def test_lifecycle_commands_are_exact_names_without_wildcards_or_prune(self):
        container = self.spec.resource(ResourceRole.CONTAINER)
        self.assertEqual(
            self.builder.start_container(),
            (DOCKER, "container", "start", container.name),
        )
        self.assertEqual(
            self.builder.stop_container(),
            (DOCKER, "container", "stop", "--time", "10", container.name),
        )
        self.assertEqual(
            self.builder.remove_container(),
            (DOCKER, "container", "rm", "--force", container.name),
        )
        self.assertEqual(
            self.builder.remove_image(), (DOCKER, "image", "rm", self.spec.image.name)
        )
        for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL):
            resource = self.spec.resource(role)
            self.assertEqual(
                self.builder.remove_volume(role),
                (DOCKER, "volume", "rm", resource.name),
            )
        flattened = "\n".join(" ".join(command) for command in self.all_commands())
        self.assertNotIn("prune", flattened)
        self.assertNotIn("*", flattened)

    def test_guest_action_is_finite_and_no_extra_command_is_accepted(self):
        command = self.builder.exec_guest(GuestAction.TEST)
        expected = [DOCKER, "container", "exec", "--user", CONTAINER_USER]
        for value in FIXED_ENV:
            expected.extend(("--env", value))
        expected.extend(
            (
                "--workdir",
                CONTAINER_WORKSPACE,
                self.spec.resource(ResourceRole.CONTAINER).name,
                GUEST_EXECUTABLE,
                "test",
            )
        )
        self.assertEqual(command, tuple(expected))
        with self.assertRaises(InvariantRefusalError):
            self.builder.exec_guest(GuestAction.TEST, ("arbitrary",))
        with self.assertRaises(Exception):
            self.builder.exec_guest("shell")

    def test_verify_clean_uses_every_exact_ownership_label_and_only_lists(self):
        commands = self.builder.verify_clean()
        roles = (
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
            ResourceRole.IMAGE,
        )
        self.assertEqual(len(commands), len(roles) * 2)
        for index, role in enumerate(roles):
            owned, named = commands[index * 2 : index * 2 + 2]
            self.assertIn("ls", owned)
            self.assertIn("ls", named)
            self.assertNotIn("rm", owned)
            self.assertNotIn("rm", named)
            resource = self.spec.resource(role)
            filters = [owned[position + 1] for position, token in enumerate(owned) if token == "--filter"]
            self.assertEqual(
                filters,
                ["label=" + key + "=" + value for key, value in resource.labels.items()],
            )
            named_filters = [
                named[position + 1]
                for position, token in enumerate(named)
                if token == "--filter"
            ]
            prefix = "reference=" if role is ResourceRole.IMAGE else "name="
            suffix = ":latest" if role is ResourceRole.IMAGE else ""
            self.assertEqual(named_filters, [prefix + resource.name + suffix])

    def test_inspect_and_list_snapshots_use_one_exact_resource(self):
        auth = self.spec.resource(ResourceRole.AUTH)
        self.assertEqual(
            self.builder.inspect(ResourceRole.AUTH),
            (DOCKER, "volume", "inspect", auth.name),
        )
        expected_list = (DOCKER, "volume", "ls", "--quiet")
        for key, value in auth.labels.items():
            expected_list += ("--filter", "label=" + key + "=" + value)
        self.assertEqual(self.builder.list_owned(ResourceRole.AUTH), expected_list)

    def test_forbidden_token_scan_covers_all_emitted_commands(self):
        lowered = "\n".join(" ".join(command).lower() for command in self.all_commands())
        forbidden = (
            "--privileged",
            "host.docker.internal",
            "network=host",
            "/var/run/docker.sock",
            "ssh_auth_sock",
            "keychain",
            ".gitconfig",
            "--publish",
            "-p=",
            "compose",
            "http://",
            "https://",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, lowered)
        self.assertNotIn("type=bind", lowered)

    def all_commands(self):
        commands = [self.builder.build_image(), self.builder.create_container()]
        for role in (ResourceRole.WORKSPACE, ResourceRole.AUTH, ResourceRole.TOOL):
            commands.extend(
                (
                    self.builder.create_volume(role),
                    self.builder.inspect(role),
                    self.builder.remove_volume(role),
                    self.builder.list_owned(role),
                )
            )
        commands.extend(
            (
                self.builder.start_container(),
                self.builder.inspect(ResourceRole.CONTAINER),
                self.builder.inspect(ResourceRole.IMAGE),
                self.builder.exec_guest(GuestAction.TEST),
                self.builder.stop_container(),
                self.builder.remove_container(),
                self.builder.remove_image(),
                self.builder.list_owned(ResourceRole.CONTAINER),
                self.builder.list_owned(ResourceRole.IMAGE),
            )
        )
        return commands


class FakeRunnerAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = make_spec()
        self.builder = DockerCommandBuilder(self.spec)
        self.fake = FakeRunner(self.spec)

    def test_full_fake_lifecycle_and_strict_inspect_policy(self):
        execute_create(self.fake, self.builder)
        for role in (
            ResourceRole.IMAGE,
            ResourceRole.CONTAINER,
            ResourceRole.WORKSPACE,
            ResourceRole.AUTH,
            ResourceRole.TOOL,
        ):
            payload = self.fake.run(self.builder.inspect(role), timeout=1).stdout
            validate_inspect(self.spec, role, payload)
        for action in GuestAction:
            self.fake.run(self.builder.exec_guest(action), timeout=1)
        self.assertEqual(
            [item[1] for item in self.fake.guest_actions],
            ["ready", "install", "test", "logout"],
        )

    def test_inspect_rejects_forged_labels_and_security_drift(self):
        execute_create(self.fake, self.builder)
        container = self.spec.resource(ResourceRole.CONTAINER)
        forged = dict(container.labels)
        forged[LABEL_PREFIX + "/ownership-token"] = TOKEN_B
        self.fake.forge_labels("container", container.name, forged)
        payload = self.fake.run(
            self.builder.inspect(ResourceRole.CONTAINER), timeout=1
        ).stdout
        with self.assertRaisesRegex(InvariantRefusalError, "inspect policy drift"):
            validate_inspect(self.spec, ResourceRole.CONTAINER, payload)

        self.fake.forge_labels("container", container.name, container.labels)
        record = self.fake.containers[container.name]
        host = record["HostConfig"]
        self.assertIsInstance(host, dict)
        host["NetworkMode"] = "host"
        payload = self.fake.run(
            self.builder.inspect(ResourceRole.CONTAINER), timeout=1
        ).stdout
        with self.assertRaisesRegex(InvariantRefusalError, "inspect policy drift"):
            validate_inspect(self.spec, ResourceRole.CONTAINER, payload)

    def test_two_labs_cannot_delete_or_list_each_others_resources(self):
        # Reuse the old short suffix; complete token names remain disjoint.
        other_spec = make_spec("lab-b", "f" * 24 + TOKEN_A[-8:])
        other_builder = DockerCommandBuilder(other_spec)
        self.fake.register_spec(other_spec)
        execute_create(self.fake, self.builder)
        execute_create(self.fake, other_builder)

        self.fake.run(self.builder.remove_container(), timeout=1)
        self.fake.run(self.builder.remove_volume(ResourceRole.AUTH), timeout=1)
        self.fake.run(self.builder.remove_image(), timeout=1)
        self.assertIn(
            other_spec.resource(ResourceRole.CONTAINER).name, self.fake.containers
        )
        self.assertIn(other_spec.resource(ResourceRole.AUTH).name, self.fake.volumes)
        self.assertIn(other_spec.image.name, self.fake.images)
        listed = self.fake.run(
            other_builder.list_owned(ResourceRole.CONTAINER), timeout=1
        ).stdout.splitlines()
        self.assertEqual(listed, [other_spec.resource(ResourceRole.CONTAINER).name])

    def test_fake_models_real_image_tag_replacement(self):
        foreign = {LABEL_PREFIX + "/managed": "false"}
        self.fake.add_residue("image", self.spec.image.name, foreign)
        self.fake.run(self.builder.build_image(), timeout=1)
        payload = self.fake.run(self.builder.inspect(ResourceRole.IMAGE), timeout=1).stdout
        validate_inspect(self.spec, ResourceRole.IMAGE, payload)

    def test_failure_injection_before_and_after_exposes_expected_residue(self):
        self.fake.inject_failure(DockerOperation.CREATE_VOLUME, when="before")
        workspace = self.spec.resource(ResourceRole.WORKSPACE)
        with self.assertRaises(RunnerFailureError):
            self.fake.run(self.builder.create_volume(ResourceRole.WORKSPACE), timeout=1)
        self.assertNotIn(workspace.name, self.fake.volumes)

        self.fake.inject_failure(DockerOperation.CREATE_VOLUME, when="after")
        with self.assertRaises(RunnerFailureError):
            self.fake.run(self.builder.create_volume(ResourceRole.WORKSPACE), timeout=1)
        self.assertIn(workspace.name, self.fake.volumes)

    def test_manual_residue_is_visible_only_through_all_exact_filters(self):
        workspace = self.spec.resource(ResourceRole.WORKSPACE)
        self.fake.add_residue("volume", workspace.name, workspace.labels)
        listed = self.fake.run(
            self.builder.list_owned(ResourceRole.WORKSPACE), timeout=1
        ).stdout
        self.assertEqual(listed, workspace.name + "\n")
        forged = dict(workspace.labels)
        forged[LABEL_PREFIX + "/schema"] = "9"
        self.fake.forge_labels("volume", workspace.name, forged)
        listed = self.fake.run(
            self.builder.list_owned(ResourceRole.WORKSPACE), timeout=1
        ).stdout
        self.assertEqual(listed, "")

    def test_operation_classifier_rejects_unknown_commands(self):
        allowed = dict(self.builder.command_operations())
        self.assertEqual(
            classify_docker_argv(self.builder.remove_image(), allowed),
            DockerOperation.REMOVE_IMAGE,
        )
        with self.assertRaises(Exception):
            classify_docker_argv((DOCKER, "system", "prune"), allowed)
        with self.assertRaises(Exception):
            classify_docker_argv(self.builder.build_image() + ("--privileged",), allowed)


class FixtureIntegrityTests(unittest.TestCase):
    def test_owned_reader_is_nonblocking_cloexec_and_detects_path_replacement(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="owned-reader-"))
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, "locked.json")
        replacement = os.path.join(root, "replacement.json")
        pathlib.Path(path).write_bytes(b"locked")
        pathlib.Path(replacement).write_bytes(b"locked")
        os.chmod(path, 0o600)
        os.chmod(replacement, 0o600)
        real_open = os.open
        observed_flags = []

        def capture_open(name, flags, *args, **kwargs):
            observed_flags.append(flags)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(docker_module.os, "open", new=capture_open):
            self.assertEqual(
                docker_module._owned_regular_bytes(path, "test lock", 64),
                b"locked",
            )
        self.assertTrue(observed_flags[-1] & os.O_NONBLOCK)
        self.assertTrue(observed_flags[-1] & os.O_CLOEXEC)

        real_read = os.read
        swapped = {"done": False}

        def swap_after_read(descriptor, count):
            payload = real_read(descriptor, count)
            if payload and not swapped["done"]:
                swapped["done"] = True
                os.replace(replacement, path)
            return payload

        with mock.patch.object(docker_module.os, "read", new=swap_after_read):
            with self.assertRaisesRegex(InvariantRefusalError, "invalid test lock"):
                docker_module._owned_regular_bytes(path, "test lock", 64)

    def test_fixture_lock_matches_artifact_and_is_explicitly_scaffold_only(self):
        spec = make_spec()
        with open(spec.fixture.artifact_path, "rb") as handle:
            checksum = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(checksum, spec.fixture.sha256)
        self.assertTrue(spec.fixture.scaffold_only)
        self.assertEqual(spec.fixture.version, "1.0.0")

    def test_official_python_rootfs_uses_absolute_python_and_locked_checksum(self):
        spec = make_spec()
        dockerfile = pathlib.Path(spec.context, "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("FROM ${BASE_IMAGE} AS runtime", dockerfile)
        self.assertIn("FROM scratch", dockerfile)
        self.assertIn("COPY --from=runtime / /", dockerfile)
        guest = pathlib.Path(
            spec.context,
            "rootfs",
            "opt",
            "unified-ext-lab",
            "guest.py",
        ).read_text(encoding="utf-8")
        fixture = pathlib.Path(spec.fixture.artifact_path).read_text(
            encoding="utf-8"
        )
        self.assertTrue(guest.startswith("#!/usr/local/bin/python3\n"))
        self.assertTrue(fixture.startswith("#!/usr/local/bin/python3\n"))
        self.assertIn(
            'FIXTURE_SHA256 = "{}"'.format(spec.fixture.sha256), guest
        )

    def test_writable_volume_seed_directories_are_locked_into_the_image(self):
        spec = make_spec()
        for relative in (
            os.path.join("rootfs", "home", "lab", ".volume-owner"),
            os.path.join(
                "rootfs", "opt", "unified-ext-lab", "tool", ".volume-owner"
            ),
        ):
            path = os.path.join(spec.context, relative)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(pathlib.Path(path).read_text(encoding="utf-8"), "uid=65532\ngid=65532\n")
        dockerfile = pathlib.Path(spec.context, "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("COPY --chown=65532:65532 rootfs/ /", dockerfile)
        self.assertNotIn("volume-nocopy", "\0".join(DockerCommandBuilder(spec).create_container()))

    def test_context_directory_and_lock_file_drift_are_refused(self):
        source = make_spec()
        root = os.path.realpath(tempfile.mkdtemp(prefix="unified-ext-context-drift-"))
        self.addCleanup(shutil.rmtree, root)
        directory = os.path.join(root, "image")
        shutil.copytree(source.context, directory)
        writable = os.path.join(directory, "rootfs", "workspace")
        os.chmod(writable, 0o775)
        with self.assertRaisesRegex(InvariantRefusalError, "directory drift"):
            DockerLabSpec.from_locks(
                source.identity, docker_executable=DOCKER, context=directory
            )

        lock_copy = os.path.join(root, "base-lock.json")
        shutil.copyfile(
            os.path.join(os.path.dirname(source.context), "locks", "base-images.v1.json"),
            lock_copy,
        )
        os.chmod(lock_copy, 0o666)
        with self.assertRaisesRegex(InvariantRefusalError, "invalid base image lock"):
            DockerLabSpec.from_locks(
                source.identity,
                docker_executable=DOCKER,
                base_lock_path=lock_copy,
            )

    def test_checksum_mismatch_is_refused_before_install_reaches_executor(self):
        source = make_spec()
        root = os.path.realpath(tempfile.mkdtemp(prefix="unified-ext-context-"))
        self.addCleanup(shutil.rmtree, root)
        directory = os.path.join(root, "image")
        shutil.copytree(source.context, directory)
        artifact = os.path.join(
            directory,
            "rootfs",
            "opt",
            "unified-ext-lab",
            "fixtures",
            "fake-provider",
        )
        with open(artifact, "ab") as handle:
            handle.write(b"tampered")
        with self.assertRaisesRegex(InvariantRefusalError, "image context file drift"):
            DockerLabSpec.from_locks(
                source.identity,
                docker_executable=DOCKER,
                context=directory,
            )

    def test_owned_sources_contain_no_real_provider_or_service_identifier(self):
        directory = os.path.dirname(os.path.dirname(__file__))
        paths = (
            os.path.join(directory, "docker.py"),
            os.path.join(directory, "runner.py"),
            os.path.join(directory, "image", "rootfs", "opt", "unified-ext-lab", "guest.py"),
            os.path.join(
                directory,
                "image",
                "rootfs",
                "opt",
                "unified-ext-lab",
                "fixtures",
                "fake-provider",
            ),
        )
        forbidden = ("api_key", "access_token", "client_secret", "oauth", "bearer ")
        content = "\n".join(
            pathlib.Path(path).read_text(encoding="utf-8").lower() for path in paths
        )
        for value in forbidden:
            self.assertNotIn(value, content)


if __name__ == "__main__":
    unittest.main()
