"""Contract tests for private durable extension-lab state."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab.errors import InvariantRefusalError, UsageStateError
from tools.unified_ext_lab.model import LabIdentity
from tools.unified_ext_lab.state import (
    FIXTURE_EXECUTION_PROFILE,
    REAL_DOCKER_EXECUTION_PROFILE,
    STATE_SCHEMA,
    LabState,
    LabStateStore,
    LockedLabStateStore,
    OperationObservation,
    PENDING_STEPS,
    SealIntent,
    StatePhase,
    canonical_json_bytes,
    ensure_process_lock_supported,
    recover_interrupted_state,
    strict_json_loads,
)


TOKEN = "0123456789abcdef0123456789abcdef"
CUSTOM_ARTIFACT = {
    "package": "synthetic-cli-fixture",
    "version": "2.0.0",
    "source_kind": "local_fixture",
    "source_locator": "fixtures/synthetic-cli-fixture-2.0.0-" + "a" * 12,
    "sha256": "a" * 64,
}


def seal_intent() -> SealIntent:
    return SealIntent("d" * 64, "e" * 64, "passed")


def initial_state() -> LabState:
    identity = LabIdentity("lab", "provider", TOKEN)
    return LabState.initial(
        "lab",
        "provider",
        TOKEN,
        (identity.resource("container"), identity.resource("auth")),
        {"auth_file_equal": True},
    )


class LifecycleTests(unittest.TestCase):
    def test_default_mappings_are_immutable_and_empty_defaults_are_not_shared(self):
        first = LabState(
            STATE_SCHEMA, 0, "lab", "provider", TOKEN, StatePhase.NEW
        )
        second = LabState(
            STATE_SCHEMA, 0, "lab", "provider", TOKEN, StatePhase.NEW
        )

        self.assertEqual(dict(first.resource_ids), {})
        self.assertEqual(dict(first.baseline_equalities), {})
        self.assertIsNot(first.resource_ids, second.resource_ids)
        self.assertIsNot(first.baseline_equalities, second.baseline_equalities)
        for mapping in (
            first.resource_ids,
            first.artifact_evidence,
            first.baseline_equalities,
        ):
            with self.subTest(mapping=mapping):
                with self.assertRaises(TypeError):
                    mapping["cannot_mutate"] = "value"

    def test_explicit_stable_forward_interruption_is_narrow_and_not_automatic(self):
        expected = {
            StatePhase.NEW: "create",
            StatePhase.CREATED: "install",
            StatePhase.INSTALLED: "test",
            StatePhase.TESTED: "evidence",
        }
        state = initial_state()
        stable_states = [state]
        for pending, stable in (
            (StatePhase.CREATE_PENDING, StatePhase.CREATED),
            (StatePhase.INSTALL_PENDING, StatePhase.INSTALLED),
            (StatePhase.TEST_PENDING, StatePhase.TESTED),
        ):
            state = state.transition(pending).transition(stable)
            stable_states.append(state)
        for stable_state in stable_states:
            with self.subTest(phase=stable_state.phase.value):
                round_trip = LabState.from_dict(stable_state.to_dict())
                self.assertEqual(round_trip.phase, stable_state.phase)
                interrupted = round_trip.interrupt_stable_forward()
                self.assertEqual(interrupted.phase, StatePhase.RECOVERY_REQUIRED)
                self.assertEqual(
                    interrupted.pending_step, expected[stable_state.phase]
                )
                self.assertEqual(interrupted.operations[-1].error_code, "interrupted")
                self.assertEqual(interrupted.revision, stable_state.revision + 1)
        with self.assertRaises(UsageStateError):
            state.transition(StatePhase.EVIDENCE_PENDING).interrupt_stable_forward()

    def test_resource_ids_are_planned_typed_and_immutable(self):
        identity = LabIdentity("lab", "provider", TOKEN)
        state = LabState.initial(
            "lab",
            "provider",
            TOKEN,
            (
                identity.resource("image"),
                identity.resource("container"),
                identity.resource("auth"),
            ),
            artifact_evidence=CUSTOM_ARTIFACT,
        ).transition(StatePhase.CREATE_PENDING)
        image_id = "sha256:" + "a" * 64
        container_id = "b" * 64
        state = state.record_resource_id("image", image_id)
        state = state.record_resource_id("container", container_id)
        self.assertEqual(
            dict(state.resource_ids),
            {"image": image_id, "container": container_id},
        )
        self.assertEqual(dict(state.artifact_evidence), CUSTOM_ARTIFACT)
        self.assertIs(state.record_resource_id("image", image_id), state)
        with self.assertRaises(InvariantRefusalError):
            state.record_resource_id("image", "sha256:" + "c" * 64)
        with self.assertRaises(InvariantRefusalError):
            state.record_resource_id("container", "c" * 64)
        with self.assertRaises(UsageStateError):
            state.record_resource_id("auth", "c" * 64)
        fresh = LabState.initial(
            "lab",
            "provider",
            TOKEN,
            (identity.resource("image"), identity.resource("container")),
        ).transition(StatePhase.CREATE_PENDING)
        for role, resource_id in (
            ("container", "sha256:" + "c" * 64),
            ("image", "c" * 64),
        ):
            with self.subTest(role=role):
                with self.assertRaises(UsageStateError):
                    fresh.record_resource_id(role, resource_id)
        restored = LabState.from_dict(state.to_dict())
        self.assertEqual(dict(restored.resource_ids), dict(state.resource_ids))
        self.assertEqual(dict(restored.artifact_evidence), CUSTOM_ARTIFACT)

    def test_schema_two_migrates_only_to_fixture_profile(self):
        legacy = initial_state().to_dict()
        legacy["schema"] = 2
        legacy.pop("execution_profile")
        legacy.pop("resource_ids")
        legacy.pop("artifact_evidence")
        migrated = LabState.from_dict(legacy)
        self.assertEqual(migrated.schema, STATE_SCHEMA)
        self.assertEqual(
            migrated.execution_profile, FIXTURE_EXECUTION_PROFILE
        )
        self.assertEqual(
            LabState.from_dict(migrated.to_dict()).execution_profile,
            FIXTURE_EXECUTION_PROFILE,
        )
        claimed_real = dict(legacy)
        claimed_real["execution_profile"] = REAL_DOCKER_EXECUTION_PROFILE
        with self.assertRaises(UsageStateError):
            LabState.from_dict(claimed_real)

    def test_legacy_draft_migration_infers_the_exact_captured_artifact(self):
        captured = self._to_evidence_captured()
        for schema in (2, 3):
            with self.subTest(schema=schema):
                document = captured.to_dict()
                document.pop("resource_ids")
                document.pop("artifact_evidence")
                if schema == 2:
                    document["schema"] = 2
                    document.pop("execution_profile")
                migrated = LabState.from_dict(document)
                self.assertEqual(
                    dict(migrated.artifact_evidence),
                    dict(migrated.draft_evidence["artifact"]),
                )

    def test_every_success_transition_and_immutability(self):
        state = initial_state()
        self.assertEqual(state.phase, StatePhase.NEW)
        with self.assertRaises(FrozenInstanceError):
            state.phase = StatePhase.DIRTY

        steps = (
            StatePhase.CREATE_PENDING,
            StatePhase.CREATED,
            StatePhase.INSTALL_PENDING,
            StatePhase.INSTALLED,
            StatePhase.TEST_PENDING,
            StatePhase.TESTED,
            StatePhase.EVIDENCE_PENDING,
        )
        for phase in steps:
            state = state.transition(phase)
            if phase.value.endswith("_PENDING"):
                self.assertIsNotNone(state.pending_step)
            else:
                self.assertIsNone(state.pending_step)
        draft = {
            "artifact": dict(state.artifact_evidence),
            "captured_at_ns": 5,
            "evidence_kind": "harness_fixture",
            "executor_kind": "fake_docker",
            "manifest_schema_sha256": "b" * 64,
            "observed_protocol_schema_sha256": "c" * 64,
            "operations": [],
            "promotion_eligible": False,
        }
        state = state.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft)
        for phase in (
            StatePhase.LOGOUT_PENDING,
            StatePhase.LOGOUT_DONE,
            StatePhase.DESTROY_PENDING,
            StatePhase.DESTROY_DONE,
            StatePhase.VERIFY_CLEAN_PENDING,
            StatePhase.CLEAN_VERIFIED,
            StatePhase.SEAL_PENDING,
            StatePhase.PASSED,
        ):
            updates = {"seal_intent": seal_intent()} if phase is StatePhase.SEAL_PENDING else {}
            state = state.transition(phase, **updates)
        self.assertEqual(state.phase, StatePhase.PASSED)
        self.assertEqual(state.revision, 16)

    def test_failure_branches_and_dirty_terminal(self):
        state = self._to_evidence_captured()
        state = state.transition(StatePhase.LOGOUT_PENDING)
        state = state.transition(StatePhase.LOGOUT_FAILED)
        state = state.transition(StatePhase.DESTROY_PENDING)
        state = state.transition(StatePhase.DESTROY_FAILED)
        state = state.transition(StatePhase.VERIFY_CLEAN_PENDING)
        state = state.transition(StatePhase.DIRTY)
        with self.assertRaises(UsageStateError):
            state.transition(StatePhase.CREATE_PENDING)
        retry = state.transition(StatePhase.DESTROY_PENDING)
        retry = retry.transition(StatePhase.DESTROY_DONE)
        retry = retry.transition(StatePhase.VERIFY_CLEAN_PENDING)
        self.assertEqual(retry.phase, StatePhase.VERIFY_CLEAN_PENDING)

        direct_recheck = state.transition(StatePhase.VERIFY_CLEAN_PENDING)
        self.assertEqual(direct_recheck.pending_step, "verify_clean")

    def test_failed_clean_terminal_is_reachable_only_after_clean(self):
        state = self._to_evidence_captured()
        for phase in (
            StatePhase.LOGOUT_PENDING,
            StatePhase.LOGOUT_FAILED,
            StatePhase.DESTROY_PENDING,
            StatePhase.DESTROY_FAILED,
            StatePhase.VERIFY_CLEAN_PENDING,
            StatePhase.CLEAN_VERIFIED,
            StatePhase.SEAL_PENDING,
            StatePhase.FAILED_CLEAN,
        ):
            updates = {"seal_intent": SealIntent("d" * 64, "e" * 64, "failed_clean")} if phase is StatePhase.SEAL_PENDING else {}
            state = state.transition(phase, **updates)
        self.assertEqual(state.phase, StatePhase.FAILED_CLEAN)

    def test_taint_blocks_evidence_and_sealing(self):
        state = initial_state()
        for phase in (
            StatePhase.CREATE_PENDING,
            StatePhase.CREATED,
            StatePhase.INSTALL_PENDING,
            StatePhase.INSTALLED,
            StatePhase.TEST_PENDING,
            StatePhase.TESTED,
        ):
            state = state.transition(phase)
        state = state.transition(StatePhase.EVIDENCE_PENDING).mark_tainted()
        with self.assertRaises(InvariantRefusalError):
            state.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence={})

    def test_draft_artifact_must_equal_durable_artifact_evidence(self):
        state = initial_state()
        for phase in (
            StatePhase.CREATE_PENDING,
            StatePhase.CREATED,
            StatePhase.INSTALL_PENDING,
            StatePhase.INSTALLED,
            StatePhase.TEST_PENDING,
            StatePhase.TESTED,
            StatePhase.EVIDENCE_PENDING,
        ):
            state = state.transition(phase)
        draft = {
            "artifact": dict(CUSTOM_ARTIFACT),
            "captured_at_ns": 1,
            "evidence_kind": "harness_fixture",
            "executor_kind": "fake_docker",
            "manifest_schema_sha256": "b" * 64,
            "observed_protocol_schema_sha256": "c" * 64,
            "operations": [],
            "promotion_eligible": False,
        }
        with self.assertRaisesRegex(
            InvariantRefusalError, "durable artifact"
        ):
            state.transition(
                StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft
            )

        captured = self._to_evidence_captured().to_dict()
        captured["artifact_evidence"] = dict(CUSTOM_ARTIFACT)
        with self.assertRaisesRegex(
            InvariantRefusalError, "durable artifact"
        ):
            LabState.from_dict(captured)

    def test_mark_tainted_is_monotonic_same_phase_revision(self):
        state = initial_state()
        tainted = state.mark_tainted()
        self.assertTrue(tainted.tainted)
        self.assertEqual(tainted.phase, StatePhase.NEW)
        self.assertEqual(tainted.revision, state.revision + 1)
        self.assertIs(tainted.mark_tainted(), tainted)

    def test_owned_roles_are_monotonic_and_limited_to_create_or_cleanup(self):
        pending = initial_state().transition(StatePhase.CREATE_PENDING)
        recorded = pending.record_owned_role("container")
        self.assertEqual(recorded.created_roles, ("container",))
        self.assertEqual(recorded.revision, pending.revision + 1)
        self.assertIs(recorded.record_owned_role("container"), recorded)
        with self.assertRaises(UsageStateError):
            recorded.record_owned_role("workspace")
        created = recorded.transition(StatePhase.CREATED)
        with self.assertRaises(UsageStateError):
            created.record_owned_role("auth")

    def test_removed_roles_are_monotonic_and_require_created_cleanup_role(self):
        pending = initial_state().transition(StatePhase.CREATE_PENDING)
        pending = pending.record_owned_role("container")
        recovered = pending.fail_pending(
            OperationObservation("create", "failed", 5, 1, "runner_failure")
        )
        destroying = recovered.transition(StatePhase.DESTROY_PENDING)
        removed = destroying.record_removed_role("container")
        self.assertEqual(removed.removed_roles, ("container",))
        self.assertIs(removed.record_removed_role("container"), removed)
        with self.assertRaises(UsageStateError):
            removed.record_removed_role("auth")

    def test_pending_failure_enters_same_cleanup_only_recovery(self):
        pending = initial_state().transition(StatePhase.CREATE_PENDING)
        failure = OperationObservation(
            "create", "failed", 5, 10, "runner_failure"
        )
        recovered = pending.fail_pending(failure)
        self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(recovered.pending_step, "create")
        self.assertEqual(recovered.operations[-1], failure)
        with self.assertRaises(UsageStateError):
            recovered.transition(StatePhase.CREATED)
        cleanup = recovered.transition(StatePhase.DESTROY_PENDING)
        self.assertEqual(cleanup.pending_step, "destroy")

    def test_observations_are_strict_and_sanitized(self):
        observation = OperationObservation("test", "failed", 6, 99, "test_failure")
        self.assertEqual(observation.to_dict()["latency_ns"], 99)
        with self.assertRaises(UsageStateError):
            OperationObservation("test", "failed", 6, 1, "none")
        with self.assertRaises(UsageStateError):
            OperationObservation("shell", "succeeded", 0, 1, "none")

    def _to_evidence_captured(self) -> LabState:
        state = initial_state()
        for phase in (
            StatePhase.CREATE_PENDING,
            StatePhase.CREATED,
            StatePhase.INSTALL_PENDING,
            StatePhase.INSTALLED,
            StatePhase.TEST_PENDING,
            StatePhase.TESTED,
            StatePhase.EVIDENCE_PENDING,
        ):
            state = state.transition(phase)
        draft = {
            "artifact": dict(state.artifact_evidence),
            "captured_at_ns": 1,
            "evidence_kind": "harness_fixture",
            "executor_kind": "fake_docker",
            "manifest_schema_sha256": "b" * 64,
            "observed_protocol_schema_sha256": "c" * 64,
            "operations": [],
            "promotion_eligible": False,
        }
        return state.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_store_refuses_cross_profile_state(self):
        root = self.base / "profile-bound"
        real = LabStateStore(root, REAL_DOCKER_EXECUTION_PROFILE)
        with real.locked("lab") as locked:
            state = locked.create_initial("provider", TOKEN, ())
        self.assertEqual(state.execution_profile, REAL_DOCKER_EXECUTION_PROFILE)
        with LabStateStore(root, FIXTURE_EXECUTION_PROFILE).locked("lab") as locked:
            with self.assertRaisesRegex(
                InvariantRefusalError, "execution profile"
            ):
                locked.load()

    def test_locked_explicit_stable_interruption_and_resource_id_are_durable(self):
        root = self.base / "explicit-recovery"
        identity = LabIdentity("lab", "provider", TOKEN)
        store = LabStateStore(root)
        with store.locked("lab") as locked:
            locked.create_initial(
                "provider",
                TOKEN,
                (identity.resource("image"), identity.resource("container")),
                artifact_evidence=CUSTOM_ARTIFACT,
            )
            locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            recorded = locked.record_resource_id(
                StatePhase.CREATE_PENDING,
                "image",
                "sha256:" + "d" * 64,
            )
            self.assertEqual(recorded.resource_ids["image"], "sha256:" + "d" * 64)
            locked.transition(StatePhase.CREATE_PENDING, StatePhase.CREATED)
        with store.locked("lab") as locked:
            stable = locked.load()
            self.assertEqual(stable.phase, StatePhase.CREATED)
            recovered = locked.interrupt_stable_forward(StatePhase.CREATED)
        self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(recovered.pending_step, "install")
        with store.locked("lab") as locked:
            persisted = locked.load()
        self.assertEqual(persisted.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(persisted.resource_ids["image"], "sha256:" + "d" * 64)
        self.assertEqual(dict(persisted.artifact_evidence), CUSTOM_ARTIFACT)

    def test_lab_directory_replacement_cannot_bypass_name_lock_or_receive_writes(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "rename-protected"
        store = LabStateStore(root)
        real_flock = state_module._fcntl.flock
        with store.locked("lab") as first:
            first.create_initial("provider", TOKEN, ())
            lab = root / "lab"
            displaced = root / "displaced-lab"
            os.rename(str(lab), str(displaced))
            lab.mkdir(mode=0o700)

            with self.assertRaisesRegex(
                InvariantRefusalError, "state directory changed while locked"
            ):
                first.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            self.assertFalse((lab / "state.json").exists())
            self.assertEqual(
                strict_json_loads((displaced / "state.json").read_bytes())["phase"],
                StatePhase.NEW.value,
            )

            def nonblocking_flock(descriptor, operation):
                if operation == state_module._fcntl.LOCK_EX:
                    operation |= state_module._fcntl.LOCK_NB
                return real_flock(descriptor, operation)

            yielded = {"value": False}
            with mock.patch.object(
                state_module._fcntl, "flock", nonblocking_flock
            ):
                with self.assertRaises(BlockingIOError):
                    with store.locked("lab"):
                        yielded["value"] = True
            self.assertFalse(yielded["value"])

    def test_state_operations_are_descriptor_relative_and_reject_file_replacement(self):
        root = self.base / "descriptor-relative"
        store = LabStateStore(root)
        with store.locked("lab") as locked:
            locked.path = self.base / "unused-pathname"
            locked.create_initial("provider", TOKEN, ())
            state_path = root / "lab" / "state.json"
            original = root / "lab" / "original-state.json"
            os.rename(str(state_path), str(original))
            replacement = LabState.initial("lab", "other", "f" * 32, ())
            state_path.write_bytes(canonical_json_bytes(replacement.to_dict()))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(
                InvariantRefusalError, "state file identity changed while locked"
            ):
                locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            observed = strict_json_loads(state_path.read_bytes())
            self.assertEqual(observed["provider_id"], "other")
            self.assertEqual(observed["phase"], StatePhase.NEW.value)

    def test_initial_state_publication_rejects_swapped_temporary_inode(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "create-temp-swap"
        store = LabStateStore(root)
        real_rename_noreplace = state_module._rename_noreplace_at
        swapped = {"value": False}
        displaced = root / "lab" / "owned-temp-displaced"

        def swap_before_rename(
            source_directory, source, destination_directory, destination
        ):
            if (
                destination == state_module.STATE_FILE_NAME
                and str(source).startswith(".state.json.")
                and not swapped["value"]
            ):
                swapped["value"] = True
                source_path = root / "lab" / str(source)
                os.rename(str(source_path), str(displaced))
                source_path.write_bytes(b"same-user replacement")
                source_path.chmod(0o600)
            return real_rename_noreplace(
                source_directory,
                source,
                destination_directory,
                destination,
            )

        with store.locked("lab") as locked:
            with mock.patch.object(
                state_module,
                "_rename_noreplace_at",
                side_effect=swap_before_rename,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "state file identity changed"
                ):
                    locked.create_initial("provider", TOKEN, ())

        self.assertTrue(swapped["value"])
        generated = [
            item
            for item in (root / "lab").iterdir()
            if item.name.startswith(".state.json.") and item.name.endswith(".tmp")
        ]
        self.assertEqual(generated, [])
        self.assertTrue(displaced.exists())
        final = root / "lab" / state_module.STATE_FILE_NAME
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), b"same-user replacement")
        self.assertNotEqual(
            (final.stat().st_dev, final.stat().st_ino),
            (displaced.stat().st_dev, displaced.stat().st_ino),
        )

    def test_directory_fsync_rejects_fifo_swap_without_opening_it(self):
        from tools.unified_ext_lab import state as state_module

        directory = self.base / "fsync-directory"
        directory.mkdir(mode=0o700)
        displaced = self.base / "fsync-directory-displaced"
        real_lstat = Path.lstat
        swapped = {"value": False}

        def swap_after_lstat(path):
            observed = real_lstat(path)
            if path == directory and not swapped["value"]:
                swapped["value"] = True
                os.rename(str(directory), str(displaced))
                os.mkfifo(str(directory), 0o600)
            return observed

        with mock.patch.object(Path, "lstat", swap_after_lstat):
            with self.assertRaisesRegex(
                InvariantRefusalError, "durability check"
            ):
                state_module._fsync_directory(directory)
        self.assertTrue(swapped["value"])

    def test_state_reader_rejects_same_length_restored_mtime_mutation(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "state-ctime"
        store = LabStateStore(root)
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
        state_path = root / "lab" / state_module.STATE_FILE_NAME
        original_payload = state_path.read_bytes()
        replacement = LabState.initial("lab", "provides", TOKEN, ())
        replacement_payload = canonical_json_bytes(replacement.to_dict())
        self.assertEqual(len(replacement_payload), len(original_payload))
        before = state_path.stat()
        real_open = state_module.os.open
        mutated = {"value": False}

        def mutate_before_open(name, flags, *args, **kwargs):
            if (
                name == state_module.STATE_FILE_NAME
                and flags & os.O_RDONLY == os.O_RDONLY
                and not mutated["value"]
            ):
                mutated["value"] = True
                state_path.write_bytes(replacement_payload)
                os.utime(
                    str(state_path),
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
                self.assertEqual(state_path.stat().st_size, before.st_size)
                self.assertEqual(
                    state_path.stat().st_mtime_ns, before.st_mtime_ns
                )
                self.assertNotEqual(
                    state_path.stat().st_ctime_ns, before.st_ctime_ns
                )
            return real_open(name, flags, *args, **kwargs)

        with store.locked("lab") as locked:
            with mock.patch.object(
                state_module.os, "open", side_effect=mutate_before_open
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "identity changed"
                ):
                    locked.load()
        self.assertTrue(mutated["value"])
        state_path.write_bytes(original_payload)
        state_path.chmod(0o600)
        with store.locked("lab") as locked:
            self.assertEqual(locked.load().provider_id, "provider")

    def test_state_reader_fifo_replacement_fails_and_releases_locks(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "state-fifo"
        store = LabStateStore(root)
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
        state_path = root / "lab" / state_module.STATE_FILE_NAME
        displaced = state_path.with_name("displaced-state.json")
        real_open = state_module.os.open
        replaced = {"value": False}

        def replace_with_fifo(name, flags, *args, **kwargs):
            if name == state_module.STATE_FILE_NAME and not replaced["value"]:
                replaced["value"] = True
                os.rename(str(state_path), str(displaced))
                os.mkfifo(str(state_path), 0o600)
            return real_open(name, flags, *args, **kwargs)

        with store.locked("lab") as locked:
            with mock.patch.object(
                state_module.os, "open", side_effect=replace_with_fifo
            ):
                with self.assertRaises(InvariantRefusalError):
                    locked.load()
        self.assertTrue(replaced["value"])
        state_path.unlink()
        os.rename(str(displaced), str(state_path))
        with store.locked("lab") as locked:
            self.assertEqual(locked.load().phase, StatePhase.NEW)

    def test_lock_reader_rejects_same_inode_ctime_mutation(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "lock-ctime"
        store = LabStateStore(root)
        with store.locked("lab"):
            pass
        lock_path = root / "lab" / state_module.LOCK_FILE_NAME
        before = lock_path.stat()
        real_stat_lock = state_module._stat_private_lock_file
        mutated = {"value": False}

        def mutate_after_stat(
            directory_descriptor,
            name=state_module.LOCK_FILE_NAME,
            field="state lock",
        ):
            observed = real_stat_lock(directory_descriptor, name, field)
            if name == state_module.LOCK_FILE_NAME and not mutated["value"]:
                mutated["value"] = True
                os.chmod(str(lock_path), 0o644)
                os.chmod(str(lock_path), 0o600)
                self.assertEqual(lock_path.stat().st_ino, before.st_ino)
                self.assertNotEqual(
                    lock_path.stat().st_ctime_ns, before.st_ctime_ns
                )
            return observed

        with mock.patch.object(
            state_module,
            "_stat_private_lock_file",
            side_effect=mutate_after_stat,
        ):
            with self.assertRaisesRegex(
                InvariantRefusalError, "changed during acquisition"
            ):
                with store.locked("lab"):
                    pass
        self.assertTrue(mutated["value"])
        with store.locked("lab"):
            pass

    def test_atomic_update_never_overwrites_replacement_injected_at_rename(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "rename-cas"
        store = LabStateStore(root)
        replacement = LabState.initial("lab", "other", "e" * 32, ())
        injected = {"value": False}
        real_rename = state_module.os.rename
        real_rename_noreplace = state_module._rename_noreplace_at

        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())

            def inject_before_backup(
                source_directory,
                source,
                destination_directory,
                destination,
            ):
                if (
                    source == state_module.STATE_FILE_NAME
                    and destination == state_module.STATE_REPLACE_BACKUP_NAME
                    and not injected["value"]
                ):
                    injected["value"] = True
                    state_path = root / "lab" / "state.json"
                    real_rename(str(state_path), str(state_path.with_name("displaced.json")))
                    state_path.write_bytes(canonical_json_bytes(replacement.to_dict()))
                    state_path.chmod(0o600)
                return real_rename_noreplace(
                    source_directory,
                    source,
                    destination_directory,
                    destination,
                )

            with mock.patch.object(
                state_module,
                "_rename_noreplace_at",
                side_effect=inject_before_backup,
            ):
                with self.assertRaises(InvariantRefusalError):
                    locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)

        observed = strict_json_loads((root / "lab" / "state.json").read_bytes())
        self.assertTrue(injected["value"])
        self.assertEqual(observed["provider_id"], "other")
        self.assertEqual(observed["phase"], StatePhase.NEW.value)

    def test_transaction_intent_is_no_overwrite_and_race_detected(self):
        from tools.unified_ext_lab import state as state_module

        reserved_root = self.base / "intent-no-overwrite"
        reserved_store = LabStateStore(reserved_root)
        with reserved_store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
            intent_path = (
                reserved_root
                / "lab"
                / state_module.STATE_REPLACE_INTENT_NAME
            )
            reserved = b"reserved transaction evidence\n"
            intent_path.write_bytes(reserved)
            intent_path.chmod(0o600)
            with self.assertRaisesRegex(
                InvariantRefusalError, "requires recovery"
            ):
                locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            self.assertEqual(intent_path.read_bytes(), reserved)

        raced_root = self.base / "intent-race"
        raced_store = LabStateStore(raced_root)
        real_rename_noreplace = state_module._rename_noreplace_at
        mutated = {"value": False}
        with raced_store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
            intent_path = (
                raced_root / "lab" / state_module.STATE_REPLACE_INTENT_NAME
            )

            def mutate_intent_then_rename(
                source_directory,
                source,
                destination_directory,
                destination,
            ):
                if (
                    source == state_module.STATE_FILE_NAME
                    and destination == state_module.STATE_REPLACE_BACKUP_NAME
                    and not mutated["value"]
                ):
                    mutated["value"] = True
                    before = intent_path.stat()
                    payload = bytearray(intent_path.read_bytes())
                    marker = b'"predecessor_payload_sha256":"'
                    index = payload.index(marker) + len(marker)
                    payload[index] = ord("f") if payload[index] != ord("f") else ord("e")
                    intent_path.write_bytes(bytes(payload))
                    os.utime(
                        str(intent_path),
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    self.assertEqual(intent_path.stat().st_size, before.st_size)
                    self.assertEqual(
                        intent_path.stat().st_mtime_ns, before.st_mtime_ns
                    )
                    self.assertNotEqual(
                        intent_path.stat().st_ctime_ns, before.st_ctime_ns
                    )
                return real_rename_noreplace(
                    source_directory,
                    source,
                    destination_directory,
                    destination,
                )

            with mock.patch.object(
                state_module,
                "_rename_noreplace_at",
                side_effect=mutate_intent_then_rename,
            ):
                with self.assertRaisesRegex(
                    InvariantRefusalError, "intent changed"
                ):
                    locked.transition(
                        StatePhase.NEW, StatePhase.CREATE_PENDING
                    )
        self.assertTrue(mutated["value"])
        self.assertTrue(intent_path.exists())
        self.assertTrue(
            (
                raced_root
                / "lab"
                / state_module.STATE_REPLACE_BACKUP_NAME
            ).exists()
        )

    def test_interrupted_no_replace_transaction_restores_or_finishes(self):
        from tools.unified_ext_lab import state as state_module

        for point in (
            "after_intent",
            "after_backup",
            "after_publish",
            "during_backup_cleanup",
            "after_backup_cleanup",
            "during_intent_cleanup",
        ):
            with self.subTest(point=point):
                root = self.base / ("transaction-" + point)
                store = LabStateStore(root)
                real_rename_noreplace = state_module._rename_noreplace_at
                real_remove = state_module._remove_exact_private_file_at
                real_unlink = state_module.os.unlink
                with store.locked("lab") as locked:
                    initial = locked.create_initial("provider", TOKEN, ())

                    def crash_at_rename(
                        source_directory,
                        source,
                        destination_directory,
                        destination,
                    ):
                        moving_backup = (
                            source == state_module.STATE_FILE_NAME
                            and destination
                            == state_module.STATE_REPLACE_BACKUP_NAME
                        )
                        publishing = (
                            source.startswith(".state.json.")
                            and source.endswith(".tmp")
                            and destination == state_module.STATE_FILE_NAME
                        )
                        if point == "after_intent" and moving_backup:
                            raise RuntimeError("injected crash after intent")
                        result = real_rename_noreplace(
                            source_directory,
                            source,
                            destination_directory,
                            destination,
                        )
                        if point == "after_backup" and moving_backup:
                            raise RuntimeError("injected crash after backup")
                        if point == "after_publish" and publishing:
                            raise RuntimeError("injected crash after publish")
                        return result

                    def crash_after_remove(
                        directory_descriptor,
                        name,
                        expected_identity,
                        expected_payload,
                        field,
                    ):
                        result = real_remove(
                            directory_descriptor,
                            name,
                            expected_identity,
                            expected_payload,
                            field,
                        )
                        if (
                            point == "after_backup_cleanup"
                            and name == state_module.STATE_REPLACE_BACKUP_NAME
                        ):
                            raise RuntimeError(
                                "injected crash after backup cleanup"
                            )
                        return result

                    def crash_during_unlink(path, *args, **kwargs):
                        if (
                            point == "during_backup_cleanup"
                            and path
                            == state_module.STATE_REPLACE_BACKUP_REMOVING_NAME
                        ) or (
                            point == "during_intent_cleanup"
                            and path
                            == state_module.STATE_REPLACE_INTENT_REMOVING_NAME
                        ):
                            raise RuntimeError(
                                "injected crash during transaction cleanup"
                            )
                        return real_unlink(path, *args, **kwargs)

                    with mock.patch.object(
                        state_module,
                        "_rename_noreplace_at",
                        side_effect=crash_at_rename,
                    ), mock.patch.object(
                        state_module,
                        "_remove_exact_private_file_at",
                        side_effect=crash_after_remove,
                    ), mock.patch.object(
                        state_module.os,
                        "unlink",
                        side_effect=crash_during_unlink,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "injected crash"):
                            locked.transition(
                                StatePhase.NEW, StatePhase.CREATE_PENDING
                            )

                state_path = root / "lab" / state_module.STATE_FILE_NAME
                backup_path = (
                    root / "lab" / state_module.STATE_REPLACE_BACKUP_NAME
                )
                intent_path = (
                    root / "lab" / state_module.STATE_REPLACE_INTENT_NAME
                )
                intent_removing_path = (
                    root
                    / "lab"
                    / state_module.STATE_REPLACE_INTENT_REMOVING_NAME
                )
                backup_removing_path = (
                    root
                    / "lab"
                    / state_module.STATE_REPLACE_BACKUP_REMOVING_NAME
                )
                self.assertTrue(
                    intent_path.exists() or intent_removing_path.exists()
                )

                with store.locked("lab") as locked:
                    recovered = locked.load()

                self.assertFalse(backup_path.exists())
                self.assertFalse(intent_path.exists())
                self.assertFalse(backup_removing_path.exists())
                self.assertFalse(intent_removing_path.exists())
                self.assertTrue(state_path.exists())
                if point in ("after_intent", "after_backup"):
                    self.assertEqual(recovered.phase, StatePhase.NEW)
                    self.assertEqual(recovered.revision, initial.revision)
                else:
                    self.assertEqual(
                        recovered.phase, StatePhase.RECOVERY_REQUIRED
                    )
                    self.assertEqual(recovered.pending_step, "create")

    def test_ambiguous_interrupted_transaction_is_preserved_and_refused(self):
        from tools.unified_ext_lab import state as state_module

        root = self.base / "transaction-ambiguous"
        store = LabStateStore(root)
        real_rename_noreplace = state_module._rename_noreplace_at
        with store.locked("lab") as locked:
            initial = locked.create_initial("provider", TOKEN, ())

            def crash_after_backup(
                source_directory,
                source,
                destination_directory,
                destination,
            ):
                result = real_rename_noreplace(
                    source_directory,
                    source,
                    destination_directory,
                    destination,
                )
                if (
                    source == state_module.STATE_FILE_NAME
                    and destination == state_module.STATE_REPLACE_BACKUP_NAME
                ):
                    raise RuntimeError("injected crash after backup")
                return result

            with mock.patch.object(
                state_module,
                "_rename_noreplace_at",
                side_effect=crash_after_backup,
            ):
                with self.assertRaises(RuntimeError):
                    locked.transition(
                        StatePhase.NEW, StatePhase.CREATE_PENDING
                    )
        state_path = root / "lab" / state_module.STATE_FILE_NAME
        backup_path = root / "lab" / state_module.STATE_REPLACE_BACKUP_NAME
        intent_path = root / "lab" / state_module.STATE_REPLACE_INTENT_NAME
        # This is an individually valid same-lineage revision+1 state, but it
        # is not the exact CREATE_PENDING successor authorized by the intent.
        replacement = initial.mark_tainted()
        state_path.write_bytes(canonical_json_bytes(replacement.to_dict()))
        state_path.chmod(0o600)

        with store.locked("lab") as locked:
            with self.assertRaisesRegex(
                InvariantRefusalError, "transaction intent"
            ):
                locked.load()

        self.assertTrue(state_path.exists())
        self.assertTrue(backup_path.exists())
        self.assertTrue(intent_path.exists())
        self.assertEqual(
            strict_json_loads(state_path.read_bytes())["phase"],
            StatePhase.NEW.value,
        )
        self.assertTrue(strict_json_loads(state_path.read_bytes())["tainted"])

    def test_state_file_symlink_hardlink_mode_and_owner_are_refused(self):
        from tools.unified_ext_lab import state as state_module

        for variant in ("symlink", "hardlink", "mode", "owner"):
            with self.subTest(variant=variant):
                root = self.base / ("state-file-" + variant)
                store = LabStateStore(root)
                with store.locked("lab") as locked:
                    locked.create_initial("provider", TOKEN, ())
                state_path = root / "lab" / "state.json"
                context = contextlib.nullcontext()
                if variant == "symlink":
                    original = state_path.with_name("original.json")
                    os.rename(str(state_path), str(original))
                    state_path.symlink_to(original.name)
                elif variant == "hardlink":
                    os.link(str(state_path), str(state_path.with_name("extra.json")))
                elif variant == "mode":
                    state_path.chmod(0o644)
                else:
                    context = mock.patch.object(
                        state_module.os,
                        "geteuid",
                        return_value=os.geteuid() + 1,
                    )
                with context:
                    with self.assertRaises(InvariantRefusalError):
                        with store.locked("lab") as locked:
                            locked.load()

    def test_store_durably_rewrites_valid_schema_two_as_fixture_profile(self):
        root = self.base / "legacy-migration"
        store = LabStateStore(root)
        with store.locked("lab") as locked:
            state = locked.create_initial("provider", TOKEN, ())
        legacy = state.to_dict()
        legacy["schema"] = 2
        legacy.pop("execution_profile")
        legacy.pop("resource_ids")
        legacy.pop("artifact_evidence")
        path = root / "lab" / "state.json"
        path.write_bytes(canonical_json_bytes(legacy))
        path.chmod(0o600)

        with store.locked("lab") as locked:
            migrated = locked.load()
        persisted = strict_json_loads(path.read_bytes())
        self.assertEqual(migrated.execution_profile, FIXTURE_EXECUTION_PROFILE)
        self.assertEqual(persisted["schema"], STATE_SCHEMA)
        self.assertEqual(
            persisted["execution_profile"], FIXTURE_EXECUTION_PROFILE
        )

    def test_private_modes_atomic_transition_and_pending_restart_recovery(self):
        root = self.base / "state"
        store = LabStateStore(root)
        resource = LabIdentity("lab", "provider", TOKEN).resource("container")
        with store.locked("lab") as locked:
            state = locked.create_initial("provider", TOKEN, (resource,))
            self.assertEqual(state.phase, StatePhase.NEW)
            locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "lab").stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "lab" / "state.json").stat().st_mode & 0o777, 0o600)

        with store.locked("lab") as locked:
            recovered = locked.load()
            self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
            self.assertEqual(recovered.pending_step, "create")
            self.assertEqual(recovered.operations[-1].error_code, "interrupted")
            with self.assertRaises(UsageStateError):
                locked.transition(StatePhase.RECOVERY_REQUIRED, StatePhase.CREATED)
            cleanup = locked.transition(
                StatePhase.RECOVERY_REQUIRED, StatePhase.DESTROY_PENDING
            )
            self.assertEqual(cleanup.pending_step, "destroy")

        with store.locked("lab") as locked:
            recovered_again = locked.load()
            self.assertEqual(recovered_again.phase, StatePhase.RECOVERY_REQUIRED)
            self.assertEqual(recovered_again.pending_step, "destroy")
            self.assertEqual(
                sum(item.error_code == "interrupted" for item in recovered_again.operations),
                2,
            )

    def test_compare_and_transition_rejects_stale_phase(self):
        store = LabStateStore(self.base / "state")
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
            with self.assertRaises(UsageStateError):
                locked.transition(StatePhase.CREATED, StatePhase.INSTALL_PENDING)

    def test_locked_verification_promotion_hold_is_durable_and_irreversible(self):
        store = LabStateStore(self.base / "state")
        with store.locked("lab") as locked:
            initial = locked.create_initial("provider", TOKEN, ())
            tainted = locked.mark_tainted(StatePhase.NEW)
            self.assertEqual(tainted.revision, initial.revision + 1)
            with self.assertRaises(UsageStateError):
                locked.mark_tainted(StatePhase.CREATED)
        with store.locked("lab") as locked:
            loaded = locked.load()
            self.assertTrue(loaded.tainted)
            self.assertEqual(loaded.phase, StatePhase.NEW)

    def test_locked_owned_role_record_is_durable_during_cleanup(self):
        store = LabStateStore(self.base / "state")
        identity = LabIdentity("lab", "provider", TOKEN)
        resources = (identity.resource("container"), identity.resource("auth"))
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, resources)
            locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            first = locked.record_owned_role(StatePhase.CREATE_PENDING, "container")
            self.assertEqual(first.created_roles, ("container",))
            recovered = locked.fail_pending(
                StatePhase.CREATE_PENDING,
                OperationObservation("create", "failed", 5, 1, "runner_failure"),
            )
            pending = locked.transition(
                StatePhase.RECOVERY_REQUIRED, StatePhase.DESTROY_PENDING
            )
            discovered = locked.record_owned_role(
                StatePhase.DESTROY_PENDING, "auth"
            )
            self.assertEqual(discovered.created_roles, ("container", "auth"))
            self.assertGreater(discovered.revision, pending.revision)
        with store.locked("lab") as locked:
            self.assertEqual(locked.load().created_roles, ("container", "auth"))

    def test_locked_pending_failure_needs_no_restart_and_matches_restart_recovery(self):
        store = LabStateStore(self.base / "state")
        failure = OperationObservation(
            "create", "failed", 5, 10, "runner_failure"
        )
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
            locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING)
            recovered = locked.fail_pending(StatePhase.CREATE_PENDING, failure)
            self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
            cleanup = locked.transition(
                StatePhase.RECOVERY_REQUIRED, StatePhase.DESTROY_PENDING
            )
            self.assertEqual(cleanup.phase, StatePhase.DESTROY_PENDING)

        other = initial_state().transition(StatePhase.CREATE_PENDING)
        restart_recovery = recover_interrupted_state(other)
        self.assertEqual(restart_recovery.phase, recovered.phase)
        self.assertEqual(restart_recovery.pending_step, recovered.pending_step)

    def test_permissions_and_symlinks_are_rejected(self):
        root = self.base / "state"
        root.mkdir(mode=0o700)
        os.chmod(str(root), 0o755)
        with self.assertRaises(InvariantRefusalError):
            with LabStateStore(root).locked("lab"):
                pass
        os.chmod(str(root), 0o700)
        target = self.base / "target"
        target.mkdir(mode=0o700)
        symlink = self.base / "link"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaises(InvariantRefusalError):
            with LabStateStore(symlink).locked("lab"):
                pass

    def test_state_file_permission_and_symlink_are_rejected(self):
        store = LabStateStore(self.base / "state")
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
        path = self.base / "state" / "lab" / "state.json"
        os.chmod(str(path), 0o644)
        with store.locked("lab") as locked:
            with self.assertRaises(InvariantRefusalError):
                locked.load()

    def test_unsafe_existing_lock_permissions_are_rejected_not_repaired(self):
        store = LabStateStore(self.base / "state")
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
        lock_path = self.base / "state" / "lab" / "state.lock"
        os.chmod(str(lock_path), 0o644)
        with self.assertRaises(InvariantRefusalError):
            with store.locked("lab"):
                pass
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o644)

    def test_lock_symlink_and_hard_link_are_rejected(self):
        store = LabStateStore(self.base / "state")
        with store.locked("lab"):
            pass
        lock_path = self.base / "state" / "lab" / "state.lock"
        original = lock_path.with_name("original.lock")
        os.replace(str(lock_path), str(original))

        lock_path.symlink_to(original.name)
        with self.assertRaises(InvariantRefusalError):
            with store.locked("lab"):
                pass
        lock_path.unlink()

        os.link(str(original), str(lock_path))
        self.assertEqual(lock_path.stat().st_nlink, 2)
        with self.assertRaises(InvariantRefusalError):
            with store.locked("lab"):
                pass

    def test_post_flock_inode_mismatch_is_refused(self):
        from tools.unified_ext_lab import state as state_module

        store = LabStateStore(self.base / "state")
        with store.locked("lab"):
            pass
        lock_path = self.base / "state" / "lab" / "state.lock"
        displaced = lock_path.with_name("displaced.lock")
        real_flock = state_module._fcntl.flock
        replaced = {"value": False}

        def replace_locked_file(descriptor, operation):
            result = real_flock(descriptor, operation)
            if (
                operation == state_module._fcntl.LOCK_EX
                and stat.S_ISREG(os.fstat(descriptor).st_mode)
                and os.fstat(descriptor).st_ino == lock_path.stat().st_ino
                and not replaced["value"]
            ):
                replaced["value"] = True
                os.replace(str(lock_path), str(displaced))
                replacement = os.open(
                    str(lock_path),
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.fchmod(replacement, 0o600)
                finally:
                    os.close(replacement)
            return result

        yielded = {"value": False}
        with mock.patch.object(state_module._fcntl, "flock", replace_locked_file):
            with self.assertRaisesRegex(
                InvariantRefusalError, "state lock changed during acquisition"
            ):
                with store.locked("lab"):
                    yielded["value"] = True
        self.assertTrue(replaced["value"])
        self.assertFalse(yielded["value"])
        self.assertNotEqual(lock_path.stat().st_ino, displaced.stat().st_ino)
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_rename_and_replacement_cannot_yield_second_active_context(self):
        from tools.unified_ext_lab import state as state_module

        store = LabStateStore(self.base / "state")
        lock_path = self.base / "state" / "lab" / "state.lock"
        displaced = self.base / "state" / "lab" / "active.lock"
        real_flock = state_module._fcntl.flock

        with store.locked("lab"):
            os.replace(str(lock_path), str(displaced))
            replacement = os.open(
                str(lock_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.fchmod(replacement, 0o600)
            finally:
                os.close(replacement)

            def nonblocking_flock(descriptor, operation):
                if operation == state_module._fcntl.LOCK_EX:
                    operation |= state_module._fcntl.LOCK_NB
                return real_flock(descriptor, operation)

            second_yielded = {"value": False}
            with mock.patch.object(
                state_module._fcntl,
                "flock",
                nonblocking_flock,
            ):
                with self.assertRaises(BlockingIOError):
                    with store.locked("lab"):
                        second_yielded["value"] = True
            self.assertFalse(second_yielded["value"])

        with store.locked("lab"):
            pass

    def test_noncanonical_root_and_unsupported_lock_are_explicit(self):
        with self.assertRaises(UsageStateError):
            LabStateStore(str(self.base / "child" / ".." / "state"))
        with mock.patch("tools.unified_ext_lab.state._fcntl", None):
            from tools.unified_ext_lab.errors import UnsupportedError

            with self.assertRaises(UnsupportedError):
                ensure_process_lock_supported()
        with mock.patch(
            "tools.unified_ext_lab.state._rename_noreplace_primitive",
            return_value=None,
        ):
            from tools.unified_ext_lab.errors import UnsupportedError

            with self.assertRaises(UnsupportedError):
                ensure_process_lock_supported()

    def test_recovery_helper_is_idempotent_for_stable_state(self):
        state = initial_state()
        self.assertIs(recover_interrupted_state(state), state)
        pending = state.transition(StatePhase.CREATE_PENDING)
        recovered = recover_interrupted_state(pending)
        self.assertEqual(recovered.phase, StatePhase.RECOVERY_REQUIRED)
        self.assertEqual(recovered.operations[-1].error_code, "interrupted")
        self.assertIs(recover_interrupted_state(recovered), recovered)

    def test_every_pending_restart_records_exactly_one_interruption_except_seal(self):
        states = {}
        state = initial_state()
        state = state.transition(StatePhase.CREATE_PENDING)
        states[state.phase] = state
        state = state.transition(StatePhase.CREATED)
        state = state.transition(StatePhase.INSTALL_PENDING)
        states[state.phase] = state
        state = state.transition(StatePhase.INSTALLED)
        state = state.transition(StatePhase.TEST_PENDING)
        states[state.phase] = state
        state = state.transition(StatePhase.TESTED)
        state = state.transition(StatePhase.EVIDENCE_PENDING)
        states[state.phase] = state
        draft = {
            "artifact": dict(state.artifact_evidence),
            "captured_at_ns": 1,
            "evidence_kind": "harness_fixture",
            "executor_kind": "fake_docker",
            "manifest_schema_sha256": "b" * 64,
            "observed_protocol_schema_sha256": "c" * 64,
            "operations": [],
            "promotion_eligible": False,
        }
        state = state.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft)
        for pending, stable in (
            (StatePhase.LOGOUT_PENDING, StatePhase.LOGOUT_DONE),
            (StatePhase.DESTROY_PENDING, StatePhase.DESTROY_DONE),
            (StatePhase.VERIFY_CLEAN_PENDING, StatePhase.CLEAN_VERIFIED),
        ):
            state = state.transition(pending)
            states[pending] = state
            state = state.transition(stable)
        state = state.transition(StatePhase.SEAL_PENDING, seal_intent=seal_intent())
        states[state.phase] = state

        self.assertEqual(set(states), set(PENDING_STEPS))
        for phase, pending_state in states.items():
            with self.subTest(phase=phase.value):
                root = self.base / ("restart-" + phase.value.lower())
                store = LabStateStore(root)
                with store.locked("lab") as locked:
                    locked._create(pending_state)
                with store.locked("lab") as locked:
                    first = locked.load()
                with store.locked("lab") as locked:
                    second = locked.load()
                if phase is StatePhase.SEAL_PENDING:
                    self.assertEqual(first.phase, StatePhase.SEAL_PENDING)
                    self.assertEqual(second.operations, pending_state.operations)
                else:
                    self.assertEqual(first.phase, StatePhase.RECOVERY_REQUIRED)
                    self.assertEqual(first.pending_step, PENDING_STEPS[phase])
                    self.assertEqual(first.operations[-1].error_code, "interrupted")
                    self.assertEqual(second.operations, first.operations)

    def test_locked_store_is_invalid_after_context_exit(self):
        with self.assertRaisesRegex(UsageStateError, "active process lock"):
            LockedLabStateStore(self.base / "forged", "lab")
        store = LabStateStore(self.base / "inactive")
        with store.locked("lab") as locked:
            locked.create_initial("provider", TOKEN, ())
        for operation in (
            locked.load,
            lambda: locked.transition(StatePhase.NEW, StatePhase.CREATE_PENDING),
            lambda: locked.mark_tainted(StatePhase.NEW),
        ):
            with self.assertRaisesRegex(UsageStateError, "no longer active"):
                operation()

    def test_new_directory_fsyncs_child_then_parent_and_propagates_failure(self):
        calls = []
        descriptor_calls = []
        real_fsync = __import__("tools.unified_ext_lab.state", fromlist=["_fsync_directory"])._fsync_directory
        real_descriptor_fsync = __import__(
            "tools.unified_ext_lab.state",
            fromlist=["_fsync_directory_descriptor"],
        )._fsync_directory_descriptor

        def record(path):
            calls.append(Path(path))
            real_fsync(path)

        def record_descriptor(descriptor):
            info = os.fstat(descriptor)
            descriptor_calls.append((info.st_dev, info.st_ino))
            real_descriptor_fsync(descriptor)

        root = self.base / "ordered"
        with mock.patch(
            "tools.unified_ext_lab.state._fsync_directory", side_effect=record
        ), mock.patch(
            "tools.unified_ext_lab.state._fsync_directory_descriptor",
            side_effect=record_descriptor,
        ):
            with LabStateStore(root).locked("lab"):
                pass
        self.assertEqual(calls, [root, root.parent])
        self.assertEqual(
            descriptor_calls[:2],
            [
                ((root / "lab").stat().st_dev, (root / "lab").stat().st_ino),
                (root.stat().st_dev, root.stat().st_ino),
            ],
        )

        failed = self.base / "failed-fsync"
        count = {"value": 0}

        def fail_parent(path):
            count["value"] += 1
            if count["value"] == 2:
                raise OSError("injected parent fsync failure")
            real_fsync(path)

        with mock.patch("tools.unified_ext_lab.state._fsync_directory", side_effect=fail_parent):
            with self.assertRaisesRegex(OSError, "injected parent"):
                with LabStateStore(failed).locked("lab"):
                    pass

    def test_unreleased_schema_v1_is_strictly_rejected(self):
        data = initial_state().to_dict()
        data["schema"] = 1
        with self.assertRaisesRegex(UsageStateError, "unsupported state schema"):
            LabState.from_dict(data)


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_nonfinite_invalid_utf8_and_oversize_rejected(self):
        invalid = (
            b'{"schema":1,"schema":1}',
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b"\xff",
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(UsageStateError):
                    strict_json_loads(payload)
        with self.assertRaises(UsageStateError):
            strict_json_loads(b"{}", maximum_bytes=1)

    def test_unknown_and_sensitive_fields_and_floats_rejected(self):
        data = initial_state().to_dict()
        for key, value in (("unknown", True), ("stdout", "secret"), ("latency", 1.5)):
            candidate = dict(data)
            candidate[key] = value
            with self.subTest(key=key):
                with self.assertRaises((UsageStateError, InvariantRefusalError)):
                    LabState.from_dict(strict_json_loads(json.dumps(candidate).encode("utf-8")))

    def test_canonical_state_is_sorted_compact_and_newline_terminated(self):
        payload = canonical_json_bytes(initial_state().to_dict())
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b": ", payload)
        self.assertEqual(payload, canonical_json_bytes(strict_json_loads(payload)))


if __name__ == "__main__":
    unittest.main()
