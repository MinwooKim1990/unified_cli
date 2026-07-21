"""Contract tests for canonical offline fixture evidence."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab.errors import InvariantRefusalError, UsageStateError
from tools.unified_ext_lab.evidence import (
    ArtifactEvidence,
    CleanupEvidence,
    FixturePlatform,
    SchemaHashes,
    artifact_from_receipt_fields,
    build_manifest,
    canonical_evidence_bytes,
    capture_draft,
    seal_manifest,
    reconcile_manifest_output,
    strict_evidence_loads,
    validate_manifest,
    validate_source_locator,
)
from tools.unified_ext_lab.model import LabIdentity
from tools.unified_ext_lab.state import (
    LabState,
    OperationObservation,
    SealIntent,
    StatePhase,
)


TOKEN = "0123456789abcdef0123456789abcdef"
ARTIFACT = {
    "package": "demo-pkg",
    "version": "1.2.3",
    "source_kind": "wheel",
    "source_locator": "fixtures/demo-pkg-1.2.3.whl",
    "sha256": "a" * 64,
}
PLATFORM = {
    "evidence_kind": "harness_fixture",
    "executor_kind": "fake_docker",
    "promotion_eligible": False,
}
HASHES = {
    "manifest_schema_sha256": "b" * 64,
    "observed_protocol_schema_sha256": "c" * 64,
}
CLEANUP = {
    "created_count": 2,
    "removed_count": 2,
    "remaining_count": 0,
    "logout_succeeded": True,
    "destroy_succeeded": True,
    "verified_clean": True,
}


def evidence_pending(*, tainted: bool = False, failed: bool = False) -> LabState:
    operation = (
        OperationObservation("test", "failed", 6, 25, "test_failure")
        if failed
        else OperationObservation("test", "succeeded", 0, 25, "none")
    )
    identity = LabIdentity("lab", "provider", TOKEN)
    state = LabState.initial(
        "lab",
        "provider",
        TOKEN,
        (identity.resource("container"), identity.resource("auth")),
    )
    state = state.transition(StatePhase.CREATE_PENDING)
    state = state.record_owned_role("container")
    state = state.record_owned_role("auth")
    for phase in (
        StatePhase.CREATED,
        StatePhase.INSTALL_PENDING,
        StatePhase.INSTALLED,
        StatePhase.TEST_PENDING,
        StatePhase.TESTED,
    ):
        state = state.transition(phase)
    state = state.transition(StatePhase.EVIDENCE_PENDING, operations=(operation,))
    return state.mark_tainted() if tainted else state


def seal_pending(*, failed: bool = False) -> LabState:
    state = evidence_pending(failed=failed)
    draft = capture_draft(state, ARTIFACT, PLATFORM, HASHES, clock_ns=lambda: 123456789)
    state = state.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft)
    state = state.transition(StatePhase.LOGOUT_PENDING)
    state = state.transition(
        StatePhase.LOGOUT_DONE,
        operations=state.operations
        + (OperationObservation("logout", "succeeded", 0, 3, "none"),),
    )
    state = state.transition(StatePhase.DESTROY_PENDING)
    state = state.record_removed_role("container")
    state = state.record_removed_role("auth")
    state = state.transition(
        StatePhase.DESTROY_DONE,
        operations=state.operations
        + (OperationObservation("destroy", "succeeded", 0, 4, "none"),),
    )
    state = state.transition(StatePhase.VERIFY_CLEAN_PENDING)
    state = state.transition(
        StatePhase.CLEAN_VERIFIED,
        operations=state.operations
        + (OperationObservation("verify_clean", "succeeded", 0, 5, "none"),),
    )
    state = state.transition(
        StatePhase.SEAL_PENDING,
        seal_intent=SealIntent("d" * 64, "e" * 64, "passed"),
    )
    return state


class EvidenceValidationTests(unittest.TestCase):
    def test_fake_clock_produces_golden_canonical_bytes(self):
        manifest = build_manifest(seal_pending(), CLEANUP)
        payload = canonical_evidence_bytes(manifest)
        expected = (
            b'{"artifact":{"package":"demo-pkg","sha256":"' + b"a" * 64
            + b'","source_kind":"wheel","source_locator":"fixtures/demo-pkg-1.2.3.whl","version":"1.2.3"},'
            b'"captured_at_ns":123456789,"cleanup":{"created_count":2,"destroy_succeeded":true,'
            b'"logout_succeeded":true,"remaining_count":0,"removed_count":2,"verified_clean":true},'
            b'"evidence_kind":"harness_fixture","executor_kind":"fake_docker","lab_id":"lab",'
            b'"manifest_schema_sha256":"' + b"b" * 64
            + b'","observed_protocol_schema_sha256":"' + b"c" * 64
            + b'","operations":[{"error_code":"none","exit_code":0,"latency_ns":25,'
            b'"outcome":"succeeded","step":"test"},{"error_code":"none","exit_code":0,'
            b'"latency_ns":3,"outcome":"succeeded","step":"logout"},{"error_code":"none",'
            b'"exit_code":0,"latency_ns":4,"outcome":"succeeded","step":"destroy"},'
            b'{"error_code":"none","exit_code":0,"latency_ns":5,"outcome":"succeeded",'
            b'"step":"verify_clean"}],"promotion_eligible":false,'
            b'"provider_id":"provider","result":"passed","schema":1}\n'
        )
        self.assertEqual(payload, expected)
        self.assertEqual(payload, canonical_evidence_bytes(json.loads(payload)))

    def test_failed_operation_yields_only_fully_clean_failure(self):
        manifest = build_manifest(seal_pending(failed=True), CLEANUP)
        self.assertEqual(manifest["result"], "failed_clean")
        dirty = dict(CLEANUP)
        dirty["removed_count"] = 1
        dirty["remaining_count"] = 1
        dirty["verified_clean"] = False
        with self.assertRaises(InvariantRefusalError):
            build_manifest(seal_pending(failed=True), dirty)

    def test_cleanup_failure_with_zero_resources_is_failed_clean_not_passed(self):
        clean_after_failure = dict(CLEANUP)
        clean_after_failure["logout_succeeded"] = False
        manifest = build_manifest(seal_pending(), clean_after_failure)
        self.assertEqual(manifest["result"], "failed_clean")

    def test_draft_operations_must_be_exact_prefix_of_final_operations(self):
        state = seal_pending()
        tampered = state.operations[1:]
        object.__setattr__(state, "operations", tampered)
        with self.assertRaises(InvariantRefusalError):
            build_manifest(state, CLEANUP)

    def test_early_failure_captures_only_after_cleanup_and_seals_failed_clean(self):
        state = LabState.initial("lab", "provider", TOKEN, ())
        state = state.transition(StatePhase.CREATE_PENDING)
        state = state.fail_pending(
            OperationObservation("create", "failed", 5, 2, "runner_failure")
        )
        state = state.transition(StatePhase.LOGOUT_PENDING)
        state = state.transition(
            StatePhase.LOGOUT_DONE,
            operations=state.operations
            + (OperationObservation("logout", "skipped", 0, 1, "none"),),
        )
        state = state.transition(StatePhase.DESTROY_PENDING)
        state = state.transition(
            StatePhase.DESTROY_DONE,
            operations=state.operations
            + (OperationObservation("destroy", "succeeded", 0, 3, "none"),),
        )
        state = state.transition(StatePhase.VERIFY_CLEAN_PENDING)
        state = state.transition(
            StatePhase.CLEAN_VERIFIED,
            operations=state.operations
            + (OperationObservation("verify_clean", "succeeded", 0, 4, "none"),),
        )
        draft = capture_draft(
            state, ARTIFACT, PLATFORM, HASHES, clock_ns=lambda: 9
        )
        state = state.transition(
            StatePhase.SEAL_PENDING,
            draft_evidence=draft,
            seal_intent=SealIntent("d" * 64, "e" * 64, "failed_clean"),
        )
        cleanup = dict(CLEANUP, created_count=0, removed_count=0)
        self.assertEqual(build_manifest(state, cleanup)["result"], "failed_clean")

    def test_fixture_can_never_be_promotional(self):
        bad = dict(PLATFORM)
        bad["promotion_eligible"] = True
        with self.assertRaises(InvariantRefusalError):
            capture_draft(evidence_pending(), ARTIFACT, bad, HASHES, clock_ns=lambda: 1)
        manifest = build_manifest(seal_pending(), CLEANUP)
        manifest["promotion_eligible"] = True
        with self.assertRaises(InvariantRefusalError):
            validate_manifest(manifest)

    def test_shell_taint_blocks_capture(self):
        with self.assertRaises(InvariantRefusalError):
            capture_draft(
                evidence_pending(tainted=True), ARTIFACT, PLATFORM, HASHES, clock_ns=lambda: 1
            )

    def test_manifest_sealing_order_requires_seal_pending(self):
        pending = evidence_pending()
        draft = capture_draft(pending, ARTIFACT, PLATFORM, HASHES, clock_ns=lambda: 1)
        captured = pending.transition(StatePhase.EVIDENCE_CAPTURED, draft_evidence=draft)
        with self.assertRaises(UsageStateError):
            build_manifest(captured, CLEANUP)

    def test_unknown_pii_secret_receipt_and_numeric_float_fields_rejected(self):
        manifest = build_manifest(seal_pending(), CLEANUP)
        candidates = (
            ("stdout", "token=secret"),
            ("receipt_path", "/private/tmp/receipt"),
            ("uid", 501),
            ("latency", 1.25),
            ("url", "https://user:pass@example.test/a"),
        )
        for key, value in candidates:
            candidate = dict(manifest)
            candidate[key] = value
            with self.subTest(key=key):
                with self.assertRaises((UsageStateError, InvariantRefusalError)):
                    canonical_evidence_bytes(candidate)

    def test_strict_evidence_parser_rejects_duplicate_and_nonfinite_json(self):
        for payload in (
            b'{"schema":1,"schema":1}',
            b'{"schema":NaN}',
            b'{"schema":Infinity}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(UsageStateError):
                    strict_evidence_loads(payload)

    def test_schema_hashes_are_separate_required_fields(self):
        bad = dict(HASHES)
        del bad["observed_protocol_schema_sha256"]
        with self.assertRaises(UsageStateError):
            capture_draft(evidence_pending(), ARTIFACT, PLATFORM, bad, clock_ns=lambda: 1)

    def test_manifest_identity_uses_shared_lab_and_provider_validators(self):
        manifest = build_manifest(seal_pending(), CLEANUP)
        for field, value in (("lab_id", "INVALID"), ("provider_id", "bad/provider")):
            candidate = dict(manifest)
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(UsageStateError):
                    validate_manifest(candidate)


class LocatorAndReceiptTests(unittest.TestCase):
    def test_locator_accepts_relative_pinned_and_rejects_unsafe_forms(self):
        self.assertEqual(
            validate_source_locator("fixtures/demo-pkg-1.2.3.whl"),
            "fixtures/demo-pkg-1.2.3.whl",
        )
        invalid = (
            "/tmp/a-1.whl",
            "../a-1.whl",
            "x/../a-1.whl",
            "x/./a-1.whl",
            "x//a-1.whl",
            "C:/a-1.whl",
            "https://example.test/a-1.whl",
            "user@example.test/a-1.whl",
            "a-1.whl?download=1",
            "a-1.whl#fragment",
            "a%2f..%2fa-1.whl",
            "latest/a.whl",
            "releases/a-latest.whl",
            "a\n-1.whl",
        )
        for locator in invalid:
            with self.subTest(locator=repr(locator)):
                with self.assertRaises((UsageStateError, InvariantRefusalError)):
                    validate_source_locator(locator)

    def test_artifact_requires_locator_pinned_by_version_or_digest(self):
        bad = dict(ARTIFACT)
        bad["source_locator"] = "fixtures/demo-pkg.whl"
        with self.assertRaises(InvariantRefusalError):
            ArtifactEvidence.from_value(bad)

    def test_receipt_projection_accepts_only_explicit_safe_fields(self):
        artifact = artifact_from_receipt_fields(**ARTIFACT)
        self.assertEqual(artifact.package, "demo-pkg")
        with self.assertRaises(TypeError):
            artifact_from_receipt_fields(**dict(ARTIFACT, receipt_path="/tmp/receipt"))


class SealTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "evidence"
        self.root.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def test_seal_is_no_overwrite_atomic_and_private(self):
        output = self.root / "manifest.json"
        payload = seal_manifest(seal_pending(), CLEANUP, output)
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(InvariantRefusalError):
            seal_manifest(seal_pending(), CLEANUP, output)

    def test_reconcile_accepts_only_exact_private_canonical_existing_bytes(self):
        output = self.root / "manifest.json"
        payload = seal_manifest(seal_pending(), CLEANUP, output)
        reconcile_manifest_output(output, payload)
        output.write_bytes(payload + b" ")
        os.chmod(output, 0o600)
        with self.assertRaisesRegex(InvariantRefusalError, "does not match"):
            reconcile_manifest_output(output, payload)

    def test_hardlink_temp_unlink_is_followed_by_parent_fsync(self):
        output = self.root / "manifest.json"
        events = []
        real_unlink = Path.unlink
        real_fsync = os.fsync

        def tracked_unlink(path, *args, **kwargs):
            if path.name.endswith(".tmp"):
                events.append("unlink")
            return real_unlink(path, *args, **kwargs)

        def tracked_fsync(descriptor):
            events.append("fsync-after-unlink" if "unlink" in events else "fsync")
            return real_fsync(descriptor)

        with mock.patch.object(Path, "unlink", tracked_unlink), mock.patch(
            "tools.unified_ext_lab.evidence.os.fsync", side_effect=tracked_fsync
        ):
            seal_manifest(seal_pending(), CLEANUP, output)
        self.assertIn("fsync-after-unlink", events)

    def test_reconcile_recovers_exact_generated_temp_link_after_publish_crash(self):
        output = self.root / "manifest.json"
        payload = canonical_evidence_bytes(build_manifest(seal_pending(), CLEANUP))
        temporary_name = ".manifest.json.0123456789abcdef.tmp"
        real_unlink = Path.unlink
        injected = {"done": False}

        def crash_before_temp_unlink(path, *args, **kwargs):
            if path.name == temporary_name and not injected["done"]:
                injected["done"] = True
                raise RuntimeError("injected crash after publication")
            return real_unlink(path, *args, **kwargs)

        with mock.patch(
            "tools.unified_ext_lab.evidence.secrets.token_hex",
            return_value="0123456789abcdef",
        ), mock.patch.object(Path, "unlink", crash_before_temp_unlink):
            with self.assertRaisesRegex(RuntimeError, "after publication"):
                seal_manifest(seal_pending(), CLEANUP, output)

        temporary = self.root / temporary_name
        self.assertTrue(output.exists())
        self.assertTrue(temporary.exists())
        self.assertEqual(output.stat().st_nlink, 2)
        self.assertEqual(
            (output.stat().st_dev, output.stat().st_ino),
            (temporary.stat().st_dev, temporary.stat().st_ino),
        )

        events = []
        real_fsync = os.fsync

        def tracked_recovery_unlink(path, *args, **kwargs):
            if path.name == temporary_name:
                events.append("unlink")
            return real_unlink(path, *args, **kwargs)

        def tracked_recovery_fsync(descriptor):
            events.append("fsync-after-unlink" if "unlink" in events else "fsync")
            return real_fsync(descriptor)

        with mock.patch.object(
            Path, "unlink", tracked_recovery_unlink
        ), mock.patch(
            "tools.unified_ext_lab.evidence.os.fsync",
            side_effect=tracked_recovery_fsync,
        ):
            reconcile_manifest_output(output, payload)

        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(output.stat().st_nlink, 1)
        self.assertFalse(temporary.exists())
        self.assertIn("fsync-after-unlink", events)

    def test_reconcile_rejects_unexplained_or_inexact_hardlink(self):
        for link_name in (
            "unexplained-link",
            ".manifest.json.not-a-generated-token.tmp",
        ):
            with self.subTest(link_name=link_name):
                output = self.root / "manifest.json"
                payload = seal_manifest(seal_pending(), CLEANUP, output)
                hardlink = self.root / link_name
                os.link(str(output), str(hardlink))

                with self.assertRaisesRegex(
                    InvariantRefusalError, "unexplained evidence output hardlink"
                ):
                    reconcile_manifest_output(output, payload)

                self.assertTrue(output.exists())
                self.assertTrue(hardlink.exists())
                self.assertEqual(output.stat().st_nlink, 2)
                hardlink.unlink()
                output.unlink()

    def test_reconcile_does_not_remove_exactly_named_unrelated_temp(self):
        output = self.root / "manifest.json"
        payload = seal_manifest(seal_pending(), CLEANUP, output)
        generated_link = self.root / ".manifest.json.0123456789abcdef.tmp"
        unrelated = self.root / ".manifest.json.fedcba9876543210.tmp"
        os.link(str(output), str(generated_link))
        unrelated.write_bytes(payload)
        os.chmod(unrelated, 0o600)

        with self.assertRaisesRegex(
            InvariantRefusalError, "unexplained generated evidence temporary"
        ):
            reconcile_manifest_output(output, payload)

        self.assertTrue(generated_link.exists())
        self.assertTrue(unrelated.exists())
        self.assertNotEqual(
            (output.stat().st_dev, output.stat().st_ino),
            (unrelated.stat().st_dev, unrelated.stat().st_ino),
        )

    def test_output_parent_permissions_and_existing_symlink_rejected(self):
        os.chmod(str(self.root), 0o755)
        with self.assertRaises(InvariantRefusalError):
            seal_manifest(seal_pending(), CLEANUP, self.root / "manifest.json")
        os.chmod(str(self.root), 0o700)
        target = self.root / "target"
        target.write_bytes(b"untouched")
        output = self.root / "manifest.json"
        output.symlink_to(target)
        with self.assertRaises(InvariantRefusalError):
            seal_manifest(seal_pending(), CLEANUP, output)
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_noncanonical_output_path_is_rejected(self):
        output = Path(str(self.root) + "/child/../manifest.json")
        with self.assertRaises(UsageStateError):
            seal_manifest(seal_pending(), CLEANUP, output)


if __name__ == "__main__":
    unittest.main()
