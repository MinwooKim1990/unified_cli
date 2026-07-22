"""Command grammar tests for the source-only Stage-6C launcher."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab import provider_cli
from tools.unified_ext_lab.docker import DockerCleanupSpec, DockerLabSpec
from tools.unified_ext_lab.errors import UsageStateError
from tools.unified_ext_lab.provider_lifecycle import profile_artifact
from tools.unified_ext_lab.provider_profiles import get_provider_profile
from tools.unified_ext_lab.tests.provider_fake_runner import (
    ProviderFakeCommands,
    ProviderFakeRunner,
)
from tools.unified_ext_lab.tests.test_provider_lifecycle import _ready_profile


class _Snapshot:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def present(self):
        return self.path.is_dir() and not self.path.is_symlink()

    def remove(self):
        if self.present():
            self.path.rmdir()


class _FakeRuntime:
    def __init__(self, runner, profile=None, *, cleanup=False) -> None:
        self.runner = runner
        self.profile = profile
        self.cleanup = cleanup
        self._snapshot = None

    def preflight(self):
        return None

    def probe_daemon(self):
        return None

    def capture_snapshot(self, path):
        Path(path).mkdir(mode=0o700)
        self._snapshot = _Snapshot(path)

    def bind_existing_snapshot(self, path):
        self._snapshot = _Snapshot(path)
        if not self._snapshot.present():
            raise UsageStateError("fake snapshot is unavailable")

    def bind_snapshot_for_cleanup(self, path):
        self._snapshot = _Snapshot(path)

    @property
    def snapshot_resource(self):
        if self._snapshot is None:
            raise UsageStateError("fake snapshot is unavailable")
        return self._snapshot

    def spec(self, identity):
        spec = DockerLabSpec.from_locks(
            identity, docker_executable="/offline/fake-docker"
        )
        self.runner.register_spec(spec)
        return spec

    def cleanup_spec(self, identity, planned_resources):
        return DockerCleanupSpec.from_persisted(
            identity,
            docker_executable="/offline/fake-docker",
            planned_resources=planned_resources,
        )

    def commands(self, spec):
        if type(spec) is DockerCleanupSpec:
            forward = DockerLabSpec.from_locks(
                spec.identity, docker_executable="/offline/fake-docker"
            )
            self.runner.register_spec(forward)
            return ProviderFakeCommands(forward, self.profile or self.runner.profile)
        return ProviderFakeCommands(spec, self.profile or self.runner.profile)

    def close(self):
        return None


class ProviderCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="provider-cli-")
        self.root = Path(os.path.realpath(self.temporary.name))
        self.state_parent = self.root / "private"
        self.state_parent.mkdir(mode=0o700)
        self.state_root = self.state_parent / "state"
        self.evidence = self.state_parent / "evidence.json"
        self.token = "a" * 32

    def tearDown(self):
        self.temporary.cleanup()

    def _invoke(self, argv, runtime_factory, cleanup_runtime_factory):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = provider_cli.main(
                argv,
                runtime_factory=runtime_factory,
                cleanup_runtime_factory=cleanup_runtime_factory,
                token_factory=lambda: self.token,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def _common(self, command, provider="grok"):
        return [
            command,
            "--provider",
            provider,
            "--lab-id",
            "provider-one",
            "--state-root",
            str(self.state_root),
            "--json",
        ]

    def test_parser_has_no_url_argv_executable_shell_or_cwd_surface(self):
        called = []

        def runtime(_profile):
            called.append(True)
            raise AssertionError("runtime must not be constructed")

        for forbidden in ("--url", "--argv", "--executable", "--shell", "--cwd"):
            with self.subTest(forbidden=forbidden):
                code, stdout, stderr = self._invoke(
                    self._common("create") + [forbidden, "value"],
                    runtime,
                    lambda: None,
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("usage:", stderr)
        self.assertEqual(called, [])

    def test_full_lifecycle_with_ready_test_profile_is_accountless_and_exact(self):
        profile = _ready_profile()
        runner = ProviderFakeRunner(profile)

        def forward(_profile):
            return _FakeRuntime(runner, profile)

        def cleanup():
            return _FakeRuntime(runner, profile, cleanup=True)

        original = get_provider_profile
        with mock.patch.object(
            provider_cli,
            "PROVIDER_IDS",
            tuple(sorted(provider_cli.PROVIDER_IDS + (profile.provider_id,))),
        ), mock.patch.object(
            provider_cli,
            "get_provider_profile",
            side_effect=lambda value: (
                profile if value == profile.provider_id else original(value)
            ),
        ):
            commands = (
                self._common("create", profile.provider_id),
                self._common("install", profile.provider_id)
                + ["--allow-network", "--allow-install"],
                self._common("test", profile.provider_id),
                self._common("evidence", profile.provider_id),
                self._common("logout", profile.provider_id),
                self._common("destroy", profile.provider_id),
                self._common("verify-clean", profile.provider_id)
                + ["--evidence-output", str(self.evidence)],
            )
            for argv in commands:
                code, stdout, stderr = self._invoke(argv, forward, cleanup)
                self.assertEqual((code, stderr), (0, ""), msg=argv[0])
                payload = json.loads(stdout)
                self.assertIs(payload["accountless_only"], True)
                self.assertIs(payload["promotion_eligible"], False)

        manifest = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(manifest["evidence_kind"], "provider_accountless")
        self.assertIs(manifest["promotion_eligible"], False)
        self.assertFalse(runner.network_connected)
        flattened = "\n".join("\0".join(command) for command in runner.commands)
        self.assertNotIn("shell", flattened)
        self.assertNotIn("curl", flattened)
        self.assertNotIn("/var/run/docker.sock", flattened)

    def test_current_profile_install_is_held_before_network_and_cleans_exactly(self):
        profile = get_provider_profile("grok")
        runner = ProviderFakeRunner(profile)
        forward_calls = []

        def forward(_profile):
            forward_calls.append(_profile.provider_id)
            return _FakeRuntime(runner, profile)

        def cleanup():
            return _FakeRuntime(runner, profile, cleanup=True)

        code, _stdout, _stderr = self._invoke(
            self._common("create"), forward, cleanup
        )
        self.assertEqual(code, 0)
        forward_calls.clear()
        unrelated_name = "unrelated-container"
        runner.add_residue(
            "container", unrelated_name, {"unrelated": "true"}
        )
        baseline = tuple(runner.commands)
        code, _stdout, _stderr = self._invoke(
            self._common("install")
            + ["--allow-network", "--allow-install"],
            forward,
            cleanup,
        )
        self.assertEqual(code, 3)
        self.assertEqual(forward_calls, [])
        self.assertEqual(tuple(runner.commands), baseline)
        self.assertFalse(runner.network_connected)

        for argv in (
            self._common("logout"),
            self._common("destroy"),
            self._common("verify-clean")
            + ["--evidence-output", str(self.evidence)],
        ):
            code, _stdout, stderr = self._invoke(argv, forward, cleanup)
            self.assertEqual((code, stderr), (0, ""))
        self.assertIn(unrelated_name, runner.containers)
        manifest = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")
        self.assertIs(manifest["promotion_eligible"], False)

    def test_profile_update_cannot_strand_cleanup_or_rewrite_artifact(self):
        original = get_provider_profile("grok")
        updated = replace(original, version="0.2.107-candidate")
        runner = ProviderFakeRunner(original)

        def forward(profile):
            return _FakeRuntime(runner, profile)

        def cleanup():
            return _FakeRuntime(runner, updated, cleanup=True)

        code, _stdout, stderr = self._invoke(
            self._common("create"), forward, cleanup
        )
        self.assertEqual((code, stderr), (0, ""))
        with mock.patch.object(
            provider_cli,
            "get_provider_profile",
            side_effect=AssertionError(
                "cleanup must not consult the current provider profile"
            ),
        ):
            for argv in (
                self._common("logout"),
                self._common("destroy"),
                self._common("verify-clean")
                + ["--evidence-output", str(self.evidence)],
            ):
                code, _stdout, stderr = self._invoke(
                    argv, forward, cleanup
                )
                self.assertEqual((code, stderr), (0, ""), msg=argv[0])
        manifest = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["artifact"], profile_artifact(original).to_dict()
        )
        self.assertNotEqual(
            manifest["artifact"], profile_artifact(updated).to_dict()
        )


if __name__ == "__main__":
    unittest.main()
