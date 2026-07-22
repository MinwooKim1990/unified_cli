"""Deterministic lifecycle and evidence tests for Stage 6C."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab.docker import DockerLabSpec
from tools.unified_ext_lab.docker_runtime import DerivedSnapshotResource
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    RunnerFailureError,
    UnsupportedError,
    UsageStateError,
)
from tools.unified_ext_lab.model import LabIdentity, ResourceRole
from tools.unified_ext_lab.provider_lifecycle import (
    ProviderLifecycle,
    profile_artifact,
)
from tools.unified_ext_lab.state import (
    PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE,
    LabStateStore,
    PlannedResource,
    StatePhase,
)
from tools.unified_ext_lab.tests.provider_fake_runner import (
    ProviderFakeCommands,
    ProviderFakeRunner,
)
from tools.unified_ext_lab.tests.provider_supply_fixture import (
    ready_synthetic_profile,
)


class _Snapshot:
    def __init__(self, path: Path) -> None:
        self.path = path

    def present(self):
        return self.path.is_dir() and not self.path.is_symlink()

    def remove(self):
        if self.present():
            self.path.rmdir()


def _ready_profile():
    return ready_synthetic_profile()


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="provider-lifecycle-")
        self.root = Path(os.path.realpath(self.temporary.name))
        self.profile = _ready_profile()
        self.identity = LabIdentity(
            "provider-one", self.profile.provider_id, "a" * 32
        )
        self.spec = DockerLabSpec.from_locks(
            self.identity, docker_executable=os.path.realpath(sys.executable)
        )
        self.store = LabStateStore(
            self.root / "state", PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE
        )
        self.runner = ProviderFakeRunner(self.profile)
        self.runner.register_spec(self.spec)
        self.commands = ProviderFakeCommands(self.spec, self.profile)
        self.snapshot_path = self.root / "runtime-snapshot"
        self.snapshot_path.mkdir(mode=0o700)
        self.snapshot = _Snapshot(self.snapshot_path)
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
                artifact_evidence=profile_artifact(self.profile).to_dict(),
            )
        self.lifecycle = ProviderLifecycle(
            self.store,
            self.spec,
            self.runner,
            self.profile,
            command_builder=self.commands,
            runtime_snapshot=self.snapshot,
        )
        self.lifecycle.bind_runtime_snapshot_intent()

    def _forward_to_logout(self):
        self.lifecycle.create()
        self.lifecycle.install(allow_network=True, allow_install=True)
        self.lifecycle.test()
        self.lifecycle.evidence()
        self.lifecycle.logout()

    def tearDown(self):
        self.temporary.cleanup()

    def test_complete_accountless_lifecycle_seals_nonpromotional_evidence(self):
        self.assertEqual(self.lifecycle.create().phase, StatePhase.CREATED)
        self.assertEqual(
            self.lifecycle.install(allow_network=True, allow_install=True).phase,
            StatePhase.INSTALLED,
        )
        self.assertFalse(self.runner.network_connected)
        self.assertEqual(self.lifecycle.test().phase, StatePhase.TESTED)
        self.assertEqual(
            self.lifecycle.evidence().phase, StatePhase.EVIDENCE_CAPTURED
        )
        commands_before_logout = len(self.runner.commands)
        self.assertEqual(self.lifecycle.logout().phase, StatePhase.LOGOUT_DONE)
        self.assertEqual(len(self.runner.commands), commands_before_logout)
        destroyed, removed = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
        self.assertEqual(removed.removed_count, 1)
        clean, verified = self.lifecycle.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(verified.remaining_count, 0)
        output_root = self.root / "evidence"
        output_root.mkdir(mode=0o700)
        output = output_root / "result.json"
        sealed = self.lifecycle.seal(output)
        self.assertEqual(sealed.phase, StatePhase.PASSED)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["evidence_kind"], "provider_accountless")
        self.assertEqual(
            manifest["executor_kind"], "provider_accountless_docker"
        )
        self.assertIs(manifest["promotion_eligible"], False)
        self.assertEqual(manifest["provider_id"], "synthetic-fixture")
        forbidden_keys = {
            "argv",
            "stdout",
            "stderr",
            "prompt",
            "response",
            "env",
            "environment",
            "account",
            "session",
            "credential",
            "path",
        }

        def keys(value):
            if isinstance(value, dict):
                result = set(value)
                for nested in value.values():
                    result.update(keys(nested))
                return result
            if isinstance(value, list):
                result = set()
                for nested in value:
                    result.update(keys(nested))
                return result
            return set()

        self.assertFalse(forbidden_keys.intersection(keys(manifest)))
        text = output.read_text(encoding="utf-8").lower()
        self.assertNotIn("/private/", text)
        self.assertNotIn("/users/", text)

    def test_provider_snapshot_identity_ledger_cannot_be_downgraded(self):
        state = self.lifecycle.status()
        with self.assertRaises(InvariantRefusalError):
            replace(
                state,
                baseline_equalities={"runtime_snapshot_bound": True},
            )

    def test_install_requires_both_exact_opt_ins_before_any_runner_command(self):
        self.lifecycle.create()
        baseline = tuple(self.runner.commands)
        for keyword in (
            {},
            {"allow_network": True},
            {"allow_install": True},
        ):
            with self.subTest(keyword=keyword):
                with self.assertRaises(UsageStateError):
                    self.lifecycle.install(**keyword)
                self.assertEqual(tuple(self.runner.commands), baseline)
                self.assertEqual(self.lifecycle.status().phase, StatePhase.CREATED)

    def test_manifestless_profile_refuses_before_network_or_installer(self):
        held = replace(self.profile, supply_manifest_source=None)
        runner = ProviderFakeRunner(held)
        runner.register_spec(self.spec)
        lifecycle = ProviderLifecycle(
            self.store,
            self.spec,
            runner,
            held,
            command_builder=ProviderFakeCommands(self.spec, held),
            runtime_snapshot=self.snapshot,
        )
        self.lifecycle.create()
        baseline = tuple(runner.commands)
        with self.assertRaises(UnsupportedError):
            lifecycle.install(allow_network=True, allow_install=True)
        self.assertEqual(tuple(runner.commands), baseline)
        self.assertFalse(runner.network_connected)

    def test_install_failure_always_disconnects_and_enters_cleanup_only(self):
        self.lifecycle.create()
        self.runner.fail_provider_action = "install"
        with self.assertRaises(RunnerFailureError):
            self.lifecycle.install(allow_network=True, allow_install=True)
        self.assertFalse(self.runner.network_connected)
        self.assertEqual(
            self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED
        )
        flattened = "\n".join("\0".join(command) for command in self.runner.commands)
        self.assertIn("provider-network-connect", flattened)
        self.assertIn("provider-network-disconnect", flattened)

    def test_uncertain_network_connect_always_attempts_exact_disconnect(self):
        self.lifecycle.create()
        self.runner.fail_network_connect_after_mutation = True
        with self.assertRaises(RunnerFailureError):
            self.lifecycle.install(allow_network=True, allow_install=True)
        self.assertFalse(self.runner.network_connected)
        self.assertEqual(
            self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED
        )
        flattened = "\n".join(
            "\0".join(command) for command in self.runner.commands
        )
        self.assertIn("provider-network-connect", flattened)
        self.assertIn("provider-network-disconnect", flattened)
        self.assertNotIn("provider-guest\0install", flattened)

    def test_successful_but_ineffective_disconnect_blocks_probe_and_recovers(self):
        self.lifecycle.create()
        self.runner.ineffective_disconnect = True
        with self.assertRaises(InvariantRefusalError):
            self.lifecycle.install(allow_network=True, allow_install=True)
        self.assertTrue(self.runner.network_connected)
        self.assertEqual(
            self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED
        )
        flattened = "\n".join(
            "\0".join(command) for command in self.runner.commands
        )
        self.assertNotIn("provider-guest\0test", flattened)

    def test_active_network_attached_before_test_is_rejected_without_probe(self):
        self.lifecycle.create()
        self.lifecycle.install(allow_network=True, allow_install=True)
        record = self.runner._provider_container()
        record["NetworkSettings"]["Networks"]["bridge"] = {
            "Gateway": "172.17.0.1",
            "IPAddress": "172.17.0.2",
            "IPPrefixLen": 16,
            "MacAddress": "02:42:ac:11:00:02",
        }
        with self.assertRaises(InvariantRefusalError):
            self.lifecycle.test()
        self.assertEqual(
            self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED
        )
        flattened = "\n".join(
            "\0".join(command) for command in self.runner.commands
        )
        self.assertNotIn("provider-guest\0test", flattened)

    def test_snapshot_moved_before_destroy_taints_without_scanning_or_deleting(self):
        self._forward_to_logout()
        resource = DerivedSnapshotResource(str(self.snapshot_path))
        resource.seal_identity()
        self.lifecycle._runtime_snapshot = resource
        moved = self.root / "moved-snapshot"
        self.snapshot_path.rename(moved)
        self.snapshot_path.mkdir(mode=0o700)
        failed, _summary = self.lifecycle.destroy()
        self.assertEqual(failed.phase, StatePhase.DESTROY_FAILED)
        self.assertTrue(failed.tainted)
        self.assertTrue(moved.is_dir())
        self.assertTrue(self.snapshot_path.is_dir())

    def test_snapshot_remove_race_taints_and_preserves_moved_identity(self):
        self._forward_to_logout()
        resource = DerivedSnapshotResource(str(self.snapshot_path))
        resource.seal_identity()
        self.lifecycle._runtime_snapshot = resource
        moved = self.root / "raced-snapshot"
        original_rmdir = os.rmdir
        raced = {"done": False}

        def replace_before_rmdir(name, *, dir_fd=None):
            if name == "runtime-snapshot" and not raced["done"]:
                raced["done"] = True
                os.rename(self.snapshot_path, moved)
                self.snapshot_path.mkdir(mode=0o700)
            return original_rmdir(name, dir_fd=dir_fd)

        with mock.patch(
            "tools.unified_ext_lab.docker_runtime.os.rmdir",
            side_effect=replace_before_rmdir,
        ):
            failed, _summary = self.lifecycle.destroy()
        self.assertEqual(failed.phase, StatePhase.DESTROY_FAILED)
        self.assertTrue(failed.tainted)
        self.assertTrue(moved.is_dir())

    def test_crash_after_exact_snapshot_remove_reconciles_once(self):
        self._forward_to_logout()
        resource = DerivedSnapshotResource(str(self.snapshot_path))
        resource.seal_identity()

        class CrashAfterRemove:
            tracks_removal_state = True

            def __init__(inner_self):
                inner_self.crashed = False

            def present(inner_self):
                return resource.present()

            def remove_for_cleanup(
                inner_self,
                *,
                reconcile_absent,
                identity_required=False,
            ):
                result = resource.remove_for_cleanup(
                    reconcile_absent=reconcile_absent,
                    identity_required=identity_required,
                )
                if not inner_self.crashed:
                    inner_self.crashed = True
                    raise KeyboardInterrupt()
                return result

            def finalize_removal(inner_self):
                return resource.finalize_removal()

            def removal_finalized(inner_self):
                return resource.removal_finalized()

        self.lifecycle._runtime_snapshot = CrashAfterRemove()
        with self.assertRaises(KeyboardInterrupt):
            self.lifecycle.destroy()
        self.assertFalse(self.snapshot_path.exists())
        recovered, _summary = self.lifecycle.destroy()
        self.assertEqual(recovered.phase, StatePhase.DESTROY_DONE)
        self.assertIs(
            recovered.baseline_equalities["runtime_snapshot_removed"], True
        )
        clean, summary = self.lifecycle.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(summary.remaining_count, 0)


if __name__ == "__main__":
    unittest.main()
