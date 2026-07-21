"""Contract tests for private durable extension-lab state."""

from __future__ import annotations

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
            "artifact": {
                "package": "pkg",
                "version": "1.0.0",
                "source_kind": "wheel",
                "source_locator": "fixtures/pkg-1.0.0.whl",
                "sha256": "a" * 64,
            },
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
            "artifact": {
                "package": "pkg",
                "version": "1",
                "source_kind": "fixture",
                "source_locator": "pkg-1.tgz",
                "sha256": "a" * 64,
            },
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

    def test_locked_mark_tainted_is_durable_before_shell(self):
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

            def nonblocking_directory_flock(descriptor, operation):
                if (
                    operation == state_module._fcntl.LOCK_EX
                    and stat.S_ISDIR(os.fstat(descriptor).st_mode)
                ):
                    operation |= state_module._fcntl.LOCK_NB
                return real_flock(descriptor, operation)

            second_yielded = {"value": False}
            with mock.patch.object(
                state_module._fcntl,
                "flock",
                nonblocking_directory_flock,
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
            "artifact": {
                "package": "pkg",
                "version": "1",
                "source_kind": "fixture",
                "source_locator": "pkg-1.tgz",
                "sha256": "a" * 64,
            },
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
                    locked._write(pending_state)
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
        real_fsync = __import__("tools.unified_ext_lab.state", fromlist=["_fsync_directory"])._fsync_directory

        def record(path):
            calls.append(Path(path))
            real_fsync(path)

        root = self.base / "ordered"
        with mock.patch("tools.unified_ext_lab.state._fsync_directory", side_effect=record):
            with LabStateStore(root).locked("lab"):
                pass
        self.assertEqual(calls[:4], [root, root.parent, root / "lab", root])

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
