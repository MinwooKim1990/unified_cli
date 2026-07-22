"""Static security tests for the Stage-6C Docker grammar."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from tools.unified_ext_lab.docker import (
    DockerLabSpec,
    GuestAction,
    snapshot_image_context,
)
from tools.unified_ext_lab.errors import InvariantRefusalError, UsageStateError
from tools.unified_ext_lab.model import LabIdentity
from tools.unified_ext_lab.profile import load_real_docker_profile
from tools.unified_ext_lab.provider_docker import (
    INSTALL_NETWORK,
    PROVIDER_GUEST_EXECUTABLE,
    ProviderDockerCommandBuilder,
)
from tools.unified_ext_lab.provider_profiles import get_provider_profile
from tools.unified_ext_lab.provider_runtime import ProviderDockerRuntime
from tools.unified_ext_lab.tests.provider_fake_runner import ProviderFakeRunner


class ProviderDockerGrammarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="provider-docker-")
        self.root = Path(os.path.realpath(self.temporary.name))
        snapshot_root = self.root / "snapshot"
        snapshot_root.mkdir(mode=0o700)
        snapshot = snapshot_image_context(str(snapshot_root))
        self.identity = LabIdentity("provider-one", "grok", "a" * 32)
        self.spec = DockerLabSpec.from_snapshot(
            self.identity,
            docker_executable=os.path.realpath(sys.executable),
            base_image="sha256:" + "b" * 64,
            snapshot=snapshot,
            ephemeral_storage=True,
        )
        self.profile = get_provider_profile("grok")
        self.builder = ProviderDockerCommandBuilder(
            self.spec, load_real_docker_profile(), self.profile
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_container_boundary_is_nonroot_readonly_ephemeral_and_bounded(self):
        self.builder.validate_accountless_boundary()
        argv = self.builder.create_container()
        flattened = "\0".join(argv)
        container_body = "\0".join(argv[3:])
        for token in (
            "--user\x0065532:65532",
            "--read-only",
            "--cap-drop\x00ALL",
            "--security-opt\x00no-new-privileges=true",
            "--network\x00none",
            "--pids-limit\x00128",
            "--memory\x001g",
            "--memory-swap\x001g",
            "--cpus\x001.0",
            "--ulimit\x00nofile=1024:1024",
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "/workspace:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=65532,gid=65532",
            "/home/lab:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=65532,gid=65532",
            "/opt/unified-ext-lab/tool:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=65532,gid=65532",
        ):
            self.assertIn(token, flattened)
        for forbidden in (
            "/var/run/docker.sock",
            "/Users/",
            "/home/minwoo",
            ".ssh",
            ".gitconfig",
            "--privileged",
            "--network\x00host",
        ):
            self.assertNotIn(forbidden, container_body)

    def test_guest_and_network_commands_are_exact_and_provider_scoped(self):
        container_id = "c" * 64
        install = self.builder.exec_guest(GuestAction.INSTALL, container_id)
        self.assertEqual(
            install[-4:],
            (
                PROVIDER_GUEST_EXECUTABLE,
                "install",
                "grok",
                self.profile.profile_sha256,
            ),
        )
        self.assertEqual(
            self.builder.connect_install_network(container_id)[-4:],
            ("network", "connect", INSTALL_NETWORK, container_id),
        )
        self.assertEqual(
            self.builder.disconnect_install_network(container_id)[-5:],
            ("network", "disconnect", "--force", INSTALL_NETWORK, container_id),
        )
        with self.assertRaises(UsageStateError):
            self.builder.connect_install_network("not-an-id")
        with self.assertRaises(InvariantRefusalError):
            self.builder.exec_guest(
                GuestAction.TEST, container_id, ("caller-controlled",)
            )

    def test_probe_grammar_is_only_the_immutable_profile_forms(self):
        self.assertEqual(
            self.builder.accountless_command_grammar(),
            (self.profile.version_argv, self.profile.help_argv),
        )
        cursor_identity = LabIdentity("provider-two", "cursor", "b" * 32)
        second_snapshot = self.root / "second-snapshot"
        second_snapshot.mkdir(mode=0o700)
        cursor_spec = DockerLabSpec.from_snapshot(
            cursor_identity,
            docker_executable=os.path.realpath(sys.executable),
            base_image="sha256:" + "b" * 64,
            snapshot=snapshot_image_context(str(second_snapshot)),
            ephemeral_storage=True,
        )
        cursor = get_provider_profile("cursor")
        cursor_builder = ProviderDockerCommandBuilder(
            cursor_spec, load_real_docker_profile(), cursor
        )
        self.assertEqual(
            cursor_builder.accountless_command_grammar(),
            (cursor.version_argv, cursor.help_argv, cursor.status_argv),
        )

    def test_profile_and_resource_identity_cannot_cross(self):
        with self.assertRaises(InvariantRefusalError):
            ProviderDockerCommandBuilder(
                self.spec,
                load_real_docker_profile(),
                get_provider_profile("kimi"),
            )

    def test_existing_snapshot_rebind_loads_without_recreating(self):
        parent = self.root / "persisted"
        parent.mkdir(mode=0o700)
        snapshot_root = parent / "runtime-snapshot"
        executable = os.path.realpath(sys.executable)
        first = ProviderDockerRuntime(
            executable,
            ProviderFakeRunner(self.profile),
            load_real_docker_profile(),
            self.profile,
        )
        first._local_base_id = "sha256:" + "d" * 64
        first.capture_snapshot(str(snapshot_root))

        second = ProviderDockerRuntime(
            executable,
            ProviderFakeRunner(self.profile),
            load_real_docker_profile(),
            self.profile,
        )
        second._local_base_id = "sha256:" + "d" * 64
        second.bind_existing_snapshot(str(snapshot_root))
        rebound = second.spec(self.identity)
        self.assertEqual(rebound.context, str(snapshot_root / "image-context"))
        self.assertTrue(second.snapshot_resource.present())

        guest = (
            snapshot_root
            / "image-context"
            / "rootfs"
            / "opt"
            / "unified-ext-lab"
            / "provider_guest.py"
        )
        guest.chmod(0o700)
        refused = ProviderDockerRuntime(
            executable,
            ProviderFakeRunner(self.profile),
            load_real_docker_profile(),
            self.profile,
        )
        refused._local_base_id = "sha256:" + "d" * 64
        with self.assertRaises(InvariantRefusalError):
            refused.bind_existing_snapshot(str(snapshot_root))
        self.assertIsNone(refused._snapshot)
        self.assertIsNone(refused._snapshot_resource)


if __name__ == "__main__":
    unittest.main()
