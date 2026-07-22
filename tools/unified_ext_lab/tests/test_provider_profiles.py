"""Exact identity and acquisition-hold tests for Stage 6C profiles."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import runpy
from pathlib import Path
from dataclasses import replace
from unittest import mock

from tools.unified_ext_lab import provider_profiles
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    UnsupportedError,
    UsageStateError,
)
from tools.unified_ext_lab.evidence import (
    ACCOUNTLESS_EXECUTOR_KIND,
    AccountlessProviderPlatform,
    FixturePlatform,
)
from tools.unified_ext_lab.provider_profiles import (
    AcquisitionKind,
    PROVIDER_IDS,
    PROVIDER_PROFILES,
    SupplyManifestSource,
    ValidatedSupplyManifest,
    get_provider_profile,
)
from tools.unified_ext_lab.tests.provider_supply_fixture import (
    SYNTHETIC_SUPPLY_SOURCE,
    ready_synthetic_profile,
)


class ProviderProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture_profile = ready_synthetic_profile()
        self.fixture_path = (
            Path(provider_profiles.__file__).resolve().parent
            / "locks"
            / "provider-supply"
            / SYNTHETIC_SUPPLY_SOURCE.filename
        )
        self.fixture_payload = self.fixture_path.read_bytes()
        self.fixture_manifest = json.loads(
            self.fixture_payload.decode("ascii")
        )

    @staticmethod
    def _canonical(manifest):
        return (
            json.dumps(
                manifest,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )

    def _refuse_payload(self, payload, *, entry_count=None, mode=0o644):
        count = (
            json.loads(payload.decode("ascii"))["entry_count"]
            if entry_count is None
            else entry_count
        )
        source = replace(
            SYNTHETIC_SUPPLY_SOURCE,
            sha256=hashlib.sha256(payload).hexdigest(),
            entry_count=count,
        )
        with tempfile.TemporaryDirectory(prefix="provider-supply-lock-") as value:
            root = Path(os.path.realpath(value))
            root.chmod(0o700)
            target = root / source.filename
            target.write_bytes(payload)
            target.chmod(mode)
            with mock.patch.object(
                provider_profiles, "_SUPPLY_LOCK_ROOT", str(root)
            ):
                with self.assertRaises(InvariantRefusalError):
                    replace(
                        self.fixture_profile,
                        supply_manifest_source=source,
                    )

    def test_exact_timestamped_candidate_identity_allowlist(self):
        self.assertEqual(PROVIDER_IDS, ("copilot", "cursor", "grok", "kimi"))
        expected = {
            "grok": (
                "@xai-official/grok",
                "0.2.106",
                "grok",
                ("grok", "--version"),
                ("grok", "--help"),
                None,
            ),
            "kimi": (
                "@moonshot-ai/kimi-code",
                "0.29.0",
                "kimi",
                ("kimi", "--version"),
                ("kimi", "--help"),
                None,
            ),
            "copilot": (
                "@github/copilot",
                "1.0.73",
                "copilot",
                ("copilot", "--binary-version"),
                ("copilot", "help"),
                None,
            ),
            "cursor": (
                None,
                "2026.07.20-8cc9c0b",
                "agent",
                ("agent", "--version"),
                ("agent", "--help"),
                ("agent", "status"),
            ),
        }
        for provider_id, values in expected.items():
            profile = get_provider_profile(provider_id)
            self.assertEqual(
                (
                    profile.package_name,
                    profile.version,
                    profile.binary,
                    profile.version_argv,
                    profile.help_argv,
                    profile.status_argv,
                ),
                values,
            )
            self.assertTrue(profile.accountless_only)
            self.assertFalse(profile.promotion_eligible)
            self.assertFalse(profile.install_ready)
            self.assertRegex(profile.profile_sha256, r"^[0-9a-f]{64}$")

    def test_allowlist_and_nested_identity_are_immutable(self):
        with self.assertRaises(TypeError):
            PROVIDER_PROFILES["grok"] = PROVIDER_PROFILES["kimi"]
        with self.assertRaises(TypeError):
            get_provider_profile("grok").canonical_identity()["binary"] = "other"
        with self.assertRaises(Exception):
            get_provider_profile("grok").binary = "other"

    def test_name_collision_and_cursor_checksum_holds_are_explicit(self):
        grok = get_provider_profile("grok")
        cursor = get_provider_profile("cursor")
        self.assertEqual(grok.binary, "grok")
        self.assertNotEqual(grok.binary, cursor.binary)
        self.assertNotIn("vibe-kit", grok.package_name)
        self.assertIs(cursor.acquisition_lock, None)
        self.assertEqual(
            cursor.acquisition_kind, AcquisitionKind.PINNED_ARTIFACT
        )
        with self.assertRaises(UnsupportedError):
            cursor.require_install_ready()

    def test_locked_guest_profile_hashes_match_host_allowlist(self):
        guest_path = (
            Path(__file__).resolve().parents[1]
            / "image"
            / "rootfs"
            / "opt"
            / "unified-ext-lab"
            / "provider_guest.py"
        )
        guest = runpy.run_path(str(guest_path))["PROFILES"]
        self.assertEqual(set(guest), set(PROVIDER_PROFILES))
        for provider_id, profile in PROVIDER_PROFILES.items():
            self.assertEqual(
                guest[provider_id]["profile_sha256"], profile.profile_sha256
            )
            self.assertEqual(guest[provider_id]["version"], profile.version_argv)
            self.assertEqual(guest[provider_id]["help"], profile.help_argv)
            self.assertEqual(guest[provider_id]["status"], profile.status_argv)

    def test_guest_requires_an_exact_version_token(self):
        guest_path = (
            Path(__file__).resolve().parents[1]
            / "image"
            / "rootfs"
            / "opt"
            / "unified-ext-lab"
            / "provider_guest.py"
        )
        matches = runpy.run_path(str(guest_path))["_contains_exact_version"]
        self.assertTrue(matches(b"grok version 0.2.106\n", "0.2.106"))
        self.assertFalse(matches(b"grok version 0.2.1061\n", "0.2.106"))
        self.assertFalse(matches(b"grok version 0.2.106-beta\n", "0.2.106"))
        self.assertFalse(matches(b"grok version v0.2.106\n", "0.2.106"))

    def test_readiness_is_derived_from_real_canonical_manifest_entries(self):
        profile = self.fixture_profile
        proof = profile.require_install_ready()
        self.assertIsInstance(proof, ValidatedSupplyManifest)
        self.assertTrue(profile.install_ready)
        self.assertEqual(
            hashlib.sha256(self.fixture_payload).hexdigest(),
            proof.source.sha256,
        )
        closure = proof.acquisition_lock
        self.assertEqual(closure.locked_entry_count, 3)
        self.assertEqual(closure.locked_entry_count, 1 + len(closure.dependencies))
        self.assertEqual(
            closure.root.package_name,
            "@example/unified-ext-provider-fixture",
        )
        self.assertEqual(
            closure.root.locator,
            "npm/example/unified-ext-provider-fixture/0.0.1",
        )
        self.assertEqual(closure.root.version, "0.0.1")
        self.assertTrue(closure.root.integrity.startswith("sha512-"))
        self.assertRegex(closure.root.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(closure.root.size_bytes, 4096)
        self.assertEqual(
            tuple(entry.package_name for entry in closure.dependencies),
            (
                "@example/synthetic-dependency-a",
                "@example/synthetic-dependency-b",
            ),
        )
        self.assertEqual(proof.runtime_lock.architecture, "amd64")
        self.assertEqual(proof.runtime_lock.operating_system, "linux")
        self.assertRegex(
            proof.runtime_lock.base_reference,
            r"@sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(
            proof.runtime_lock.base_image_id,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(proof.runtime_lock.node_version, "22.17.0")
        self.assertRegex(
            proof.runtime_lock.node_executable_sha256,
            r"^[0-9a-f]{64}$",
        )
        self.assertTrue(profile.fixture_only)
        self.assertFalse(profile.promotion_eligible)
        self.assertNotIn(profile.provider_id, PROVIDER_IDS)

    def test_placeholder_objects_or_digest_count_alone_cannot_enable_install(self):
        grok = get_provider_profile("grok")
        for field_name in (
            "runtime_lock",
            "acquisition_lock",
            "runtime_lock_ready",
            "acquisition_lock_ready",
            "npm_integrity",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(TypeError):
                    replace(grok, **{field_name: object()})
        wrong_digest = replace(
            SYNTHETIC_SUPPLY_SOURCE,
            sha256="d" * 64,
        )
        with self.assertRaises(InvariantRefusalError):
            replace(
                self.fixture_profile,
                supply_manifest_source=wrong_digest,
            )
        wrong_count = replace(SYNTHETIC_SUPPLY_SOURCE, entry_count=23)
        with self.assertRaises(InvariantRefusalError):
            replace(
                self.fixture_profile,
                supply_manifest_source=wrong_count,
            )
        self.assertFalse(grok.install_ready)
        with self.assertRaises(UnsupportedError):
            grok.require_install_ready()

    def test_manifest_subject_root_platform_and_count_mutations_refuse(self):
        mutations = []

        subject = json.loads(json.dumps(self.fixture_manifest))
        subject["provider"]["version"] = "9.9.9"
        mutations.append(("subject", subject, None))

        for field_name, value in (
            ("locator", "npm/example/other-root/0.0.1"),
            ("integrity", "sha512-not-base64"),
            ("sha256", "d" * 63),
            ("size_bytes", 0),
        ):
            root = json.loads(json.dumps(self.fixture_manifest))
            root["root"][field_name] = value
            mutations.append(("root-" + field_name, root, None))

        for field_name, value in (
            ("operating_system", "windows"),
            ("architecture", "ppc64"),
            ("base_reference", "example.invalid/unpinned"),
            ("base_image_id", "sha256:short"),
            ("node_version", " bad"),
            ("node_executable_sha256", "0" * 63),
        ):
            platform = json.loads(json.dumps(self.fixture_manifest))
            platform["platform"][field_name] = value
            mutations.append(("platform-" + field_name, platform, None))

        top_count = json.loads(json.dumps(self.fixture_manifest))
        top_count["entry_count"] = 2
        mutations.append(("entry-count", top_count, 2))

        for name, manifest, source_count in mutations:
            with self.subTest(name=name):
                self._refuse_payload(
                    self._canonical(manifest), entry_count=source_count
                )

    def test_dependency_duplicate_missing_and_incomplete_closures_refuse(self):
        bad_integrity = json.loads(json.dumps(self.fixture_manifest))
        bad_integrity["dependencies"][0]["integrity"] = "sha512-not-base64"

        bad_hash = json.loads(json.dumps(self.fixture_manifest))
        bad_hash["dependencies"][0]["sha256"] = "e" * 63

        bad_size = json.loads(json.dumps(self.fixture_manifest))
        bad_size["dependencies"][0]["size_bytes"] = 0

        bad_locator = json.loads(json.dumps(self.fixture_manifest))
        bad_locator["dependencies"][0]["locator"] = "https://example.invalid/x"

        bad_version = json.loads(json.dumps(self.fixture_manifest))
        bad_version["dependencies"][0]["version"] = " bad"

        missing_field = json.loads(json.dumps(self.fixture_manifest))
        del missing_field["dependencies"][0]["locator"]

        duplicate = json.loads(json.dumps(self.fixture_manifest))
        duplicate["dependencies"].append(
            dict(duplicate["dependencies"][0])
        )
        duplicate["entry_count"] = 4

        empty = json.loads(json.dumps(self.fixture_manifest))
        empty["dependencies"] = []
        empty["entry_count"] = 1

        cases = (
            ("integrity", bad_integrity, 3),
            ("hash", bad_hash, 3),
            ("size", bad_size, 3),
            ("locator", bad_locator, 3),
            ("version", bad_version, 3),
            ("missing", missing_field, 3),
            ("duplicate", duplicate, 4),
            ("empty", empty, 1),
        )
        for name, manifest, count in cases:
            with self.subTest(name=name):
                self._refuse_payload(
                    self._canonical(manifest), entry_count=count
                )

    def test_manifest_duplicate_json_key_noncanonical_mode_path_and_symlink_refuse(self):
        duplicate_key = self.fixture_payload.replace(
            b'"entry_count":3',
            b'"entry_count":3,"entry_count":3',
            1,
        )
        self._refuse_payload(duplicate_key, entry_count=3)
        self._refuse_payload(self.fixture_payload, mode=0o600)

        with self.assertRaises(UsageStateError):
            SupplyManifestSource(
                filename="../synthetic-readiness.supply.v1.json",
                sha256=SYNTHETIC_SUPPLY_SOURCE.sha256,
                entry_count=3,
                fixture_only=True,
            )

        with tempfile.TemporaryDirectory(prefix="provider-supply-link-") as value:
            root = Path(os.path.realpath(value))
            root.chmod(0o700)
            target = root / SYNTHETIC_SUPPLY_SOURCE.filename
            target.symlink_to(self.fixture_path)
            with mock.patch.object(
                provider_profiles, "_SUPPLY_LOCK_ROOT", str(root)
            ):
                with self.assertRaises(InvariantRefusalError):
                    replace(
                        self.fixture_profile,
                        supply_manifest_source=SYNTHETIC_SUPPLY_SOURCE,
                    )

        with tempfile.TemporaryDirectory(prefix="provider-supply-mode-") as value:
            root = Path(os.path.realpath(value))
            target = root / SYNTHETIC_SUPPLY_SOURCE.filename
            target.write_bytes(self.fixture_payload)
            target.chmod(0o644)
            root.chmod(0o770)
            with mock.patch.object(
                provider_profiles, "_SUPPLY_LOCK_ROOT", str(root)
            ):
                with self.assertRaises(InvariantRefusalError):
                    replace(
                        self.fixture_profile,
                        supply_manifest_source=SYNTHETIC_SUPPLY_SOURCE,
                    )

        with tempfile.TemporaryDirectory(prefix="provider-supply-root-link-") as value:
            parent = Path(os.path.realpath(value))
            real_root = parent / "real-lock-root"
            real_root.mkdir(mode=0o700)
            target = real_root / SYNTHETIC_SUPPLY_SOURCE.filename
            target.write_bytes(self.fixture_payload)
            target.chmod(0o644)
            linked_root = parent / "linked-lock-root"
            linked_root.symlink_to(real_root, target_is_directory=True)
            with mock.patch.object(
                provider_profiles, "_SUPPLY_LOCK_ROOT", str(linked_root)
            ):
                with self.assertRaises(InvariantRefusalError):
                    replace(
                        self.fixture_profile,
                        supply_manifest_source=SYNTHETIC_SUPPLY_SOURCE,
                    )

    def test_profile_constructor_rejects_caller_shaped_commands(self):
        base = get_provider_profile("grok")
        for command in (
            ("sh", "-c", "echo unsafe"),
            ("grok", "--help\nunsafe"),
            ["grok", "--help"],
        ):
            with self.subTest(command=command):
                with self.assertRaises(UsageStateError):
                    replace(base, help_argv=command)
        with self.assertRaises(UnsupportedError):
            get_provider_profile("unknown")

    def test_accountless_and_synthetic_evidence_kinds_cannot_be_crossed(self):
        with self.assertRaises(UsageStateError):
            FixturePlatform(executor_kind=ACCOUNTLESS_EXECUTOR_KIND)
        with self.assertRaises(UsageStateError):
            AccountlessProviderPlatform(executor_kind="real_docker")
        self.assertIs(AccountlessProviderPlatform().promotion_eligible, False)


if __name__ == "__main__":
    unittest.main()
