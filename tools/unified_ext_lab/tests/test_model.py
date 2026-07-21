"""Tests for deterministic Stage 6 lab identities and ownership labels."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from tools.unified_ext_lab.errors import (
    CleanupIncompleteError,
    InvariantRefusalError,
    LabError,
    RunnerFailureError,
    TestFailureError,
    UnsupportedError,
    UsageStateError,
)
from tools.unified_ext_lab.model import (
    LABEL_PREFIX,
    RESOURCE_NAME_MAX_LENGTH,
    LabIdentity,
    LabLifecycle,
    LabResourceSet,
    ResourceRole,
    make_resource_name,
    validate_lab_id,
    validate_ownership_token,
    validate_provider_id,
)


TOKEN = "0123456789abcdef0123456789abcdef"


class ValidationTests(unittest.TestCase):
    def test_valid_construction_is_immutable_and_canonical(self):
        identity = LabIdentity("lab-7", "gitlab_duo", TOKEN, "active")
        self.assertEqual(identity.lifecycle, LabLifecycle.ACTIVE)
        self.assertEqual(identity.resource("image").role, ResourceRole.IMAGE)
        with self.assertRaises(FrozenInstanceError):
            identity.lab_id = "changed"

    def test_lab_id_rejects_unicode_controls_and_lengths(self):
        invalid = ("", "A", "lab_1", "lab\n", "lab\x00", "l\u2028ab", "a" * 33)
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(UsageStateError):
                    validate_lab_id(value)

    def test_provider_id_rejects_unicode_controls_and_lengths(self):
        invalid = ("", "A", "x-", "x__y", "x\tq", "x\u00e9", "x" * 65)
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(UsageStateError):
                    validate_provider_id(value)

    def test_ownership_token_rejects_case_unicode_controls_and_lengths(self):
        invalid = (
            "f" * 31,
            "f" * 33,
            "F" * 32,
            "g" * 32,
            "a" * 31 + "\n",
            "a" * 31 + "\u0661",
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(UsageStateError):
                    validate_ownership_token(value)

    def test_resource_name_refuses_overlength_without_truncation(self):
        with self.assertRaises(InvariantRefusalError):
            make_resource_name("a" * 32, "b" * 64, ResourceRole.WORKSPACE, TOKEN)
        name = make_resource_name("lab", "provider", ResourceRole.NETWORK, TOKEN)
        self.assertLessEqual(len(name), RESOURCE_NAME_MAX_LENGTH)
        self.assertTrue(name.endswith("89abcdef"))


class OwnershipTests(unittest.TestCase):
    def test_labels_are_exact_and_immutable(self):
        resource = LabIdentity("lab", "provider", TOKEN).resource(ResourceRole.AUTH)
        self.assertEqual(
            dict(resource.labels),
            {
                LABEL_PREFIX + "/managed": "true",
                LABEL_PREFIX + "/schema": "1",
                LABEL_PREFIX + "/lab-id": "lab",
                LABEL_PREFIX + "/provider": "provider",
                LABEL_PREFIX + "/ownership-token": TOKEN,
                LABEL_PREFIX + "/role": "auth",
            },
        )
        with self.assertRaises(TypeError):
            resource.labels[LABEL_PREFIX + "/managed"] = "false"

    def test_names_are_deterministic_and_include_the_complete_token(self):
        first = LabIdentity("lab", "provider", "0" * 24 + "12345678").resource("tool")
        second = LabIdentity("lab", "provider", "f" * 24 + "12345678").resource("tool")
        self.assertEqual(first.name, first.name)
        self.assertNotEqual(first.name, second.name)
        self.assertTrue(first.name.endswith(first.identity.ownership_token))
        self.assertTrue(second.name.endswith(second.identity.ownership_token))
        self.assertEqual(len(LabResourceSet((first, second)).resources), 2)
        with self.assertRaises(InvariantRefusalError):
            LabResourceSet((first, first))


class ErrorTests(unittest.TestCase):
    def test_exit_codes_are_stable(self):
        expected = (
            (UsageStateError, 2),
            (UnsupportedError, 3),
            (InvariantRefusalError, 4),
            (RunnerFailureError, 5),
            (TestFailureError, 6),
            (CleanupIncompleteError, 7),
        )
        for error_type, exit_code in expected:
            with self.subTest(error_type=error_type.__name__):
                self.assertTrue(issubclass(error_type, LabError))
                self.assertEqual(error_type.exit_code, exit_code)
