"""End-to-end lifecycle tests using only the stateful in-memory runner."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab.docker import DockerLabSpec, DockerOperation
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    RunnerFailureError,
    UsageStateError,
)
from tools.unified_ext_lab.lifecycle import FixtureLifecycle
from tools.unified_ext_lab.model import LabIdentity, ResourceRole
from tools.unified_ext_lab.state import LockedLabStateStore, StatePhase
from tools.unified_ext_lab.tests.fake_runner import FakeRunner


class _Clock:
    def __init__(self, start: int = 1_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 100
        return self.value


class FixtureLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="unified-ext-lifecycle-")
        self.root = Path(os.path.realpath(self.temporary.name))
        self.state_root = self.root / "state"
        self.output_root = self.root / "evidence"
        self.output_root.mkdir(mode=0o700)
        self.identity = LabIdentity("fixture-one", "synthetic", "a" * 32)
        self.spec = DockerLabSpec.from_locks(
            self.identity,
            docker_executable=os.path.realpath(sys.executable),
        )
        from tools.unified_ext_lab.state import LabStateStore

        self.store = LabStateStore(self.state_root)
        self.runner = FakeRunner(self.spec)
        self.clock = _Clock()
        self.lifecycle = FixtureLifecycle(
            self.store,
            self.spec,
            self.runner,
            monotonic_ns=self.clock,
            evidence_clock_ns=_Clock(2_000_000),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cleanup_and_seal(self, name: str = "result.json"):
        try:
            self.lifecycle.logout()
        except RunnerFailureError:
            pass
        destroy_state, destroyed = self.lifecycle.destroy()
        self.assertIn(
            destroy_state.phase,
            (StatePhase.DESTROY_DONE, StatePhase.DESTROY_FAILED),
        )
        clean_state, verified = self.lifecycle.verify_clean()
        self.assertEqual(clean_state.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(verified.remaining_count, 0)
        output = self.output_root / name
        sealed = self.lifecycle.seal(output)
        return sealed, output, destroyed

    def test_complete_fixture_lifecycle_passes_and_removes_every_resource(self):
        self.assertEqual(self.lifecycle.create().phase, StatePhase.CREATED)
        self.assertEqual(self.lifecycle.install().phase, StatePhase.INSTALLED)
        self.assertEqual(self.lifecycle.test().phase, StatePhase.TESTED)
        self.assertEqual(
            self.lifecycle.evidence().phase, StatePhase.EVIDENCE_CAPTURED
        )

        sealed, output, destroyed = self._cleanup_and_seal()

        self.assertEqual(sealed.phase, StatePhase.PASSED)
        self.assertEqual(destroyed.removed_count, 5)
        self.assertEqual(len(sealed.removed_roles), 5)
        self.assertFalse(self.runner.images)
        self.assertFalse(self.runner.containers)
        self.assertFalse(self.runner.volumes)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "passed")
        self.assertEqual(manifest["evidence_kind"], "harness_fixture")
        self.assertEqual(manifest["executor_kind"], "fake_docker")
        self.assertIs(manifest["promotion_eligible"], False)
        self.assertNotIn("seal", [item["step"] for item in manifest["operations"]])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)

    def test_install_failure_enters_recovery_and_seals_failed_clean(self):
        self.lifecycle.create()
        self.runner.inject_failure(DockerOperation.EXEC_GUEST)

        with self.assertRaises(RunnerFailureError):
            self.lifecycle.install()

        self.assertEqual(self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED)
        sealed, output, _destroyed = self._cleanup_and_seal("failed.json")
        self.assertEqual(sealed.phase, StatePhase.FAILED_CLEAN)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")
        self.assertTrue(
            any(item["step"] == "install" and item["outcome"] == "failed"
                for item in manifest["operations"])
        )

    def test_partial_create_failure_is_cleanup_only_and_leaves_no_residue(self):
        self.runner.inject_failure(DockerOperation.CREATE_CONTAINER)

        with self.assertRaises(RunnerFailureError):
            self.lifecycle.create()

        state = self.lifecycle.status()
        self.assertEqual(state.phase, StatePhase.RECOVERY_REQUIRED)
        with self.assertRaises(UsageStateError):
            self.lifecycle.install()
        sealed, output, destroyed = self._cleanup_and_seal("partial.json")
        self.assertEqual(sealed.phase, StatePhase.FAILED_CLEAN)
        self.assertEqual(destroyed.removed_count, 4)
        self.assertFalse(self.runner.images)
        self.assertFalse(self.runner.containers)
        self.assertFalse(self.runner.volumes)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")
        self.assertEqual(manifest["cleanup"]["created_count"], 4)
        self.assertEqual(manifest["cleanup"]["removed_count"], 4)

    def test_foreign_exact_image_reference_is_refused_before_build(self):
        image = self.spec.image
        foreign_labels = dict(image.labels)
        foreign_labels[next(iter(foreign_labels))] = "false"
        self.runner.add_residue("image", image.name, foreign_labels)

        with self.assertRaises(InvariantRefusalError):
            self.lifecycle.create()

        self.assertEqual(self.lifecycle.status().phase, StatePhase.RECOVERY_REQUIRED)
        record = self.runner.images[image.name]
        config = record["Config"]
        self.assertIsInstance(config, dict)
        self.assertEqual(config["Labels"], foreign_labels)
        self.assertFalse(
            any(command[1] == "build" for command in self.runner.commands)
        )

    def test_cleanup_discovers_resource_created_before_interrupted_state_write(self):
        self.runner.inject_failure(DockerOperation.CREATE_CONTAINER, when="after")

        with self.assertRaises(RunnerFailureError):
            self.lifecycle.create()

        self.assertEqual(len(self.lifecycle.status().created_roles), 4)
        sealed, output, destroyed = self._cleanup_and_seal("discovered.json")
        self.assertEqual(sealed.phase, StatePhase.FAILED_CLEAN)
        self.assertEqual(destroyed.removed_count, 5)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["cleanup"]["created_count"], 5)
        self.assertEqual(manifest["cleanup"]["removed_count"], 5)

    def test_inspect_mismatch_blocks_removal_and_clean_verification(self):
        self.lifecycle.create()
        self.lifecycle.install()
        self.lifecycle.test()
        self.lifecycle.evidence()
        self.lifecycle.logout()
        container = self.spec.resource(ResourceRole.CONTAINER)
        forged = dict(container.labels)
        forged[next(iter(forged))] = "false"
        self.runner.forge_labels("container", container.name, forged)

        destroyed, _summary = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_FAILED)
        verified, summary = self.lifecycle.verify_clean()
        self.assertEqual(verified.phase, StatePhase.DIRTY)
        self.assertGreater(summary.remaining_count, 0)
        self.assertIn(container.name, self.runner.containers)

    def test_shell_taint_is_durable_and_blocks_evidence(self):
        self.lifecycle.create()
        self.lifecycle.install()
        tainted = self.lifecycle.mark_shell_tainted()
        self.assertTrue(tainted.tainted)
        self.assertTrue(self.lifecycle.status().tainted)
        self.assertEqual(self.lifecycle.test().phase, StatePhase.TESTED)
        with self.assertRaisesRegex(Exception, "tainted"):
            self.lifecycle.evidence()

    def test_sealing_never_overwrites_an_existing_path(self):
        self.lifecycle.create()
        self.lifecycle.install()
        self.lifecycle.test()
        self.lifecycle.evidence()
        self.lifecycle.logout()
        self.lifecycle.destroy()
        self.lifecycle.verify_clean()
        output = self.output_root / "existing.json"
        output.write_text("existing", encoding="utf-8")
        os.chmod(output, 0o600)

        with self.assertRaises(Exception):
            self.lifecycle.seal(output)
        self.assertEqual(output.read_text(encoding="utf-8"), "existing")
        self.assertEqual(self.lifecycle.status().phase, StatePhase.SEAL_PENDING)

    def test_seal_publish_then_state_commit_crash_reconciles_idempotently(self):
        self.lifecycle.create()
        self.lifecycle.install()
        self.lifecycle.test()
        self.lifecycle.evidence()
        self.lifecycle.logout()
        self.lifecycle.destroy()
        self.lifecycle.verify_clean()
        output = self.output_root / "crash.json"
        original = LockedLabStateStore.transition
        injected = {"done": False}

        def crash_after_publish(locked, expected, new, **updates):
            if (
                expected is StatePhase.SEAL_PENDING
                and new in (StatePhase.PASSED, StatePhase.FAILED_CLEAN)
                and not injected["done"]
            ):
                injected["done"] = True
                raise RuntimeError("injected state commit crash")
            return original(locked, expected, new, **updates)

        with mock.patch.object(
            LockedLabStateStore, "transition", new=crash_after_publish
        ):
            with self.assertRaisesRegex(RuntimeError, "state commit crash"):
                self.lifecycle.seal(output)

        payload = output.read_bytes()
        pending = self.lifecycle.status()
        self.assertEqual(pending.phase, StatePhase.SEAL_PENDING)
        sealed = self.lifecycle.seal(output)
        self.assertIn(sealed.phase, (StatePhase.PASSED, StatePhase.FAILED_CLEAN))
        self.assertEqual(output.read_bytes(), payload)

    def test_destroy_retry_reconciles_absent_role_after_ledger_write_crash(self):
        self.lifecycle.create()
        self.lifecycle.install()
        self.lifecycle.test()
        self.lifecycle.evidence()
        self.lifecycle.logout()
        original = LockedLabStateStore.record_removed_role
        injected = {"done": False}

        def crash_before_ledger(locked, expected, role):
            if not injected["done"]:
                injected["done"] = True
                raise RuntimeError("injected removal ledger crash")
            return original(locked, expected, role)

        with mock.patch.object(
            LockedLabStateStore, "record_removed_role", new=crash_before_ledger
        ):
            with self.assertRaisesRegex(RuntimeError, "ledger crash"):
                self.lifecycle.destroy()

        recovered = self.lifecycle.status()
        self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(recovered.pending_step, "destroy")
        destroyed, summary = self.lifecycle.destroy()
        self.assertEqual(destroyed.phase, StatePhase.DESTROY_DONE)
        self.assertEqual(summary.removed_count, 5)
        self.assertEqual(len(destroyed.removed_roles), 5)
        clean, verified = self.lifecycle.verify_clean()
        self.assertEqual(clean.phase, StatePhase.CLEAN_VERIFIED)
        self.assertEqual(verified.remaining_count, 0)

    def test_commands_never_contain_broad_cleanup_or_host_mounts(self):
        self.lifecycle.create()
        self.lifecycle.install()
        self.lifecycle.test()
        self.lifecycle.evidence()
        self._cleanup_and_seal()

        flattened = "\n".join("\0".join(command) for command in self.runner.commands)
        self.assertNotIn("prune", flattened)
        self.assertNotIn("type=bind", flattened)
        self.assertNotIn("host.docker.internal", flattened)
        self.assertNotIn("--network\0host", flattened)


if __name__ == "__main__":
    unittest.main()
