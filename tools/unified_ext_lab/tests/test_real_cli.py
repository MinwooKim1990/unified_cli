"""Deterministic tests for the separate opt-in real-Docker command layer."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.unified_ext_lab.cli import main as fixture_main
from tools.unified_ext_lab.docker import (
    DockerCleanupSpec,
    DockerCommandBuilder,
    DockerLabSpec,
    DockerOperation,
    classify_docker_argv,
    validate_inspect,
)
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    UnsupportedError,
    UsageStateError,
)
from tools.unified_ext_lab.fake_docker import FakeRunner
from tools.unified_ext_lab.lifecycle import FixtureLifecycle
from tools.unified_ext_lab.model import LabIdentity, ResourceRole
from tools.unified_ext_lab.runner import CommandResult
from tools.unified_ext_lab.real_cli import _lifecycle, main
from tools.unified_ext_lab.state import (
    REAL_DOCKER_EXECUTION_PROFILE,
    LabStateStore,
    PlannedResource,
    StatePhase,
)


_FAKE_IMAGE_ID = "sha256:" + "a" * 64
_FAKE_CONTAINER_ID = "b" * 64


class _FakeSnapshotResource:
    """Filesystem-backed snapshot sentinel for CLI orchestration tests."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def present(self) -> bool:
        return self.path.is_dir() and not self.path.is_symlink()

    def remove(self) -> None:
        if self.present():
            self.path.rmdir()


class _IdentityFakeRunner(FakeRunner):
    """Offline simulator adapter that exposes fixed daemon IDs for real-profile tests."""

    def run(self, argv, *, timeout):
        result = super().run(argv, timeout=timeout)
        operation = classify_docker_argv(argv, self._operations)
        if operation is DockerOperation.LIST_IMAGE and result.stdout:
            return CommandResult(
                argv=result.argv,
                returncode=result.returncode,
                stdout=_FAKE_IMAGE_ID + "\n",
                stderr=result.stderr,
            )
        if operation is DockerOperation.LIST_CONTAINER and result.stdout:
            return CommandResult(
                argv=result.argv,
                returncode=result.returncode,
                stdout=_FAKE_CONTAINER_ID + "\n",
                stderr=result.stderr,
            )
        return result


class _RaiseOnceRunner(_IdentityFakeRunner):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error
        self.raised = False

    def run(self, argv, *, timeout):
        if not self.raised:
            self.raised = True
            raise self.error
        return super().run(argv, timeout=timeout)


class _LogoutFailureRunner(_IdentityFakeRunner):
    def __init__(self, *, fail_remove: bool) -> None:
        super().__init__()
        self.fail_remove = fail_remove
        self.logout_failed = False
        self.remove_failed = False

    def run(self, argv, *, timeout):
        operation = classify_docker_argv(argv, self._operations)
        if (
            operation is DockerOperation.EXEC_GUEST
            and argv[-1] == "logout"
            and not self.logout_failed
        ):
            self.logout_failed = True
            self.inject_failure(DockerOperation.EXEC_GUEST)
        elif (
            operation is DockerOperation.REMOVE_CONTAINER
            and self.fail_remove
            and not self.remove_failed
        ):
            self.remove_failed = True
            self.inject_failure(DockerOperation.REMOVE_CONTAINER)
        return super().run(argv, timeout=timeout)


class _FakeIdentityCommands:
    """ID-bound command facade over the offline fixture command grammar."""

    uses_resource_ids = True
    builds_image = False
    planned_roles = (ResourceRole.CONTAINER,)
    cleanup_roles = (ResourceRole.CONTAINER,)
    create_volume_roles = ()

    def __init__(self, spec: DockerLabSpec) -> None:
        self._spec = spec
        self._base = DockerCommandBuilder(spec)

    def __getattr__(self, name):
        return getattr(self._base, name)

    @staticmethod
    def _resource_id(role: ResourceRole) -> str:
        if role is ResourceRole.IMAGE:
            return _FAKE_IMAGE_ID
        if role is ResourceRole.CONTAINER:
            return _FAKE_CONTAINER_ID
        raise UsageStateError("real-Docker volumes are not managed")

    def inspect(self, role: ResourceRole, resource_id=None):
        if resource_id is not None and resource_id != self._resource_id(role):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.inspect(role)

    def list_identity(self, role: ResourceRole):
        self._resource_id(role)
        return self._base.list_owned(role)

    def exec_guest(self, action, resource_id, extra=()):
        if resource_id != self._resource_id(ResourceRole.CONTAINER):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.exec_guest(action, extra)

    def start_container(self, resource_id):
        if resource_id != self._resource_id(ResourceRole.CONTAINER):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.start_container()

    def stop_container(self, resource_id):
        if resource_id != self._resource_id(ResourceRole.CONTAINER):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.stop_container()

    def remove_container(self, resource_id):
        if resource_id != self._resource_id(ResourceRole.CONTAINER):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.remove_container()

    def remove_image(self, resource_id):
        if resource_id != self._resource_id(ResourceRole.IMAGE):
            raise InvariantRefusalError("managed resource immutable identity drift")
        return self._base.remove_image()

    def validate_inspect(self, role, payload):
        validate_inspect(self._spec, role, payload)
        return self._resource_id(role)

    def validate_cleanup_inspect(self, role, payload):
        return self.validate_inspect(role, payload)

    def validate_cleanup_identity_inspect(self, role, payload, expected_id):
        observed_id = self.validate_inspect(role, payload)
        if observed_id != expected_id:
            raise InvariantRefusalError("managed resource immutable identity drift")
        return observed_id


class _FakeRuntime:
    """Runtime-shaped fake whose lifecycle commands remain fully in memory."""

    def __init__(
        self,
        runner=None,
        *,
        preflight_error=None,
        probe_error=None,
        close_error=None,
    ) -> None:
        self.runner = _IdentityFakeRunner() if runner is None else runner
        self.preflight_error = preflight_error
        self.probe_error = probe_error
        self.close_error = close_error
        self.calls = []
        self.closed = False
        self._snapshot_resource = None

    def preflight(self) -> None:
        self.calls.append("preflight")
        if self.preflight_error is not None:
            raise self.preflight_error

    def prepare_base(self, *, allow_network: bool) -> None:
        self.calls.append(("prepare_base", allow_network))

    def probe_daemon(self) -> None:
        self.calls.append("probe_daemon")
        if self.probe_error is not None:
            raise self.probe_error

    def capture_snapshot(self, path: str) -> None:
        self.calls.append("capture_snapshot")
        resource = _FakeSnapshotResource(path)
        resource.path.mkdir(mode=0o700)
        self._snapshot_resource = resource

    def bind_snapshot_for_cleanup(self, path: str) -> None:
        self.calls.append("bind_snapshot_for_cleanup")
        self._snapshot_resource = _FakeSnapshotResource(path)

    @property
    def snapshot_resource(self):
        if self._snapshot_resource is None:
            raise UsageStateError("runtime snapshot is unavailable")
        return self._snapshot_resource

    def spec(self, identity) -> DockerLabSpec:
        self.calls.append("spec")
        spec = DockerLabSpec.from_locks(
            identity, docker_executable="/offline/fake-docker"
        )
        self.runner.register_spec(spec)
        return spec

    def cleanup_spec(self, identity, planned_resources) -> DockerCleanupSpec:
        self.calls.append("cleanup_spec")
        return DockerCleanupSpec.from_persisted(
            identity,
            docker_executable="/offline/fake-docker",
            planned_resources=planned_resources,
        )

    def commands(self, spec):
        self.calls.append("commands")
        if type(spec) is DockerCleanupSpec:
            forward_spec = DockerLabSpec.from_locks(
                spec.identity, docker_executable="/offline/fake-docker"
            )
            self.runner.register_spec(forward_spec)
            return _FakeIdentityCommands(forward_spec)
        return _FakeIdentityCommands(spec)

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class RealDockerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.state_root = self.base / "state"
        self.output = self.base / "manifest.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_main(
        self,
        *arguments: str,
        runtime_factory=None,
        cleanup_runtime_factory=None,
        token_factory=None,
    ):
        stdout = io.StringIO()
        stderr = io.StringIO()
        keywords = {}
        if runtime_factory is not None:
            keywords["runtime_factory"] = runtime_factory
        if cleanup_runtime_factory is not None:
            keywords["cleanup_runtime_factory"] = cleanup_runtime_factory
        if token_factory is not None:
            keywords["token_factory"] = token_factory
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments, **keywords)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_arguments(self):
        return (
            "conformance-run",
            "--lab-id",
            "real-lab",
            "--state-root",
            str(self.state_root),
            "--evidence-output",
            str(self.output),
        )

    def recover_arguments(self):
        return (
            "conformance-recover",
            "--lab-id",
            "real-lab",
            "--state-root",
            str(self.state_root),
            "--evidence-output",
            str(self.output),
        )

    def prepare_stable_phase(self, state_root: Path, phase: StatePhase):
        state_root.mkdir(mode=0o700)
        namespace = state_root / REAL_DOCKER_EXECUTION_PROFILE
        store = LabStateStore(namespace, REAL_DOCKER_EXECUTION_PROFILE)
        runtime = _FakeRuntime()
        identity = LabIdentity("real-lab", "synthetic", "2" * 32)
        with store.locked(identity.lab_id) as locked:
            locked.create_initial(
                identity.provider_id,
                identity.ownership_token,
                (
                    PlannedResource.from_value(
                        identity.resource(ResourceRole.CONTAINER)
                    ),
                ),
                {"runtime_snapshot_bound": False},
            )
        runtime.capture_snapshot(
            str(namespace / identity.lab_id / "runtime-snapshot")
        )
        lifecycle = _lifecycle(runtime, store, identity)
        lifecycle.bind_runtime_snapshot_intent()
        if phase is StatePhase.NEW:
            return runtime
        lifecycle.create()
        if phase is StatePhase.CREATED:
            return runtime
        lifecycle.install()
        if phase is StatePhase.INSTALLED:
            return runtime
        lifecycle.test()
        if phase is StatePhase.TESTED:
            return runtime
        self.fail("unsupported stable test phase")

    def test_parser_rejects_all_caller_controlled_runtime_inputs(self) -> None:
        forbidden = (
            "--docker",
            "--host",
            "--context",
            "--platform",
            "--provider",
            "--url",
            "--cwd",
            "--argv",
            "--credential",
            "--shell",
            "--timeout",
        )
        for option in forbidden:
            with self.subTest(option=option):
                calls = []
                code, stdout, stderr = self.run_main(
                    *self.run_arguments(),
                    option,
                    "secret-value",
                    runtime_factory=lambda: calls.append("runtime"),
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(calls, [])
                self.assertNotIn("secret-value", stderr)

    def test_parser_rejects_abbreviated_flags_without_constructing_runtime(self) -> None:
        cases = (
            ("prepare-base", "--a"),
            ("prepare-base", "--allow-net"),
            ("conformance-run", "--lab"),
            ("conformance-run", "--state"),
            ("conformance-run", "--evidence"),
            ("conformance-run", "--j"),
            ("conformance-recover", "--lab"),
            ("conformance-recover", "--state"),
            ("conformance-recover", "--evidence"),
            ("conformance-recover", "--j"),
        )
        exact = {
            "--lab": ("--state-root", str(self.state_root), "--evidence-output", str(self.output)),
            "--state": ("--lab-id", "real-lab", "--evidence-output", str(self.output)),
            "--evidence": ("--lab-id", "real-lab", "--state-root", str(self.state_root)),
            "--j": (
                "--lab-id",
                "real-lab",
                "--state-root",
                str(self.state_root),
                "--evidence-output",
                str(self.output),
            ),
        }
        values = {
            "--lab": "real-lab",
            "--state": str(self.state_root),
            "--evidence": str(self.output),
        }
        for command, abbreviated in cases:
            with self.subTest(command=command, abbreviated=abbreviated):
                calls = []
                arguments = [command, abbreviated]
                if abbreviated in values:
                    arguments.append(values[abbreviated])
                arguments.extend(exact.get(abbreviated, ()))
                code, _stdout, _stderr = self.run_main(
                    *arguments,
                    runtime_factory=lambda: calls.append("runtime"),
                    cleanup_runtime_factory=lambda: calls.append("cleanup"),
                )
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])

    def test_prepare_base_requires_exact_opt_in_without_constructing_runtime(self) -> None:
        calls = []
        code, _stdout, _stderr = self.run_main(
            "prepare-base", runtime_factory=lambda: calls.append("runtime")
        )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

        code, _stdout, _stderr = self.run_main(
            "prepare-base",
            "--allow-network=false",
            runtime_factory=lambda: calls.append("runtime"),
        )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_prepare_base_calls_only_fixed_runtime_helper_and_closes(self) -> None:
        runtime = _FakeRuntime()
        code, stdout, stderr = self.run_main(
            "prepare-base",
            "--allow-network",
            "--json",
            runtime_factory=lambda: runtime,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"result": "prepared"})
        self.assertEqual(stderr, "")
        self.assertEqual(runtime.calls, [("prepare_base", True), "close"])

    def test_preflight_failure_creates_no_identity_state_or_evidence(self) -> None:
        runtime = _FakeRuntime(preflight_error=UnsupportedError("daemon absent"))
        token_calls = []
        code, stdout, stderr = self.run_main(
            *self.run_arguments(),
            runtime_factory=lambda: runtime,
            token_factory=lambda: token_calls.append("token"),
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("unsupported operation", stderr)
        self.assertEqual(token_calls, [])
        self.assertFalse(self.state_root.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(runtime.calls, ["preflight", "close"])

    def test_happy_run_uses_internal_namespace_and_redacted_output(self) -> None:
        runtime = _FakeRuntime()
        code, stdout, stderr = self.run_main(
            *self.run_arguments(),
            "--json",
            runtime_factory=lambda: runtime,
            token_factory=lambda: "1" * 32,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(
            set(payload),
            {"lab_id", "provider_id", "phase", "revision", "tainted", "result"},
        )
        self.assertEqual(payload["phase"], "PASSED")
        self.assertEqual(payload["result"], "passed")
        self.assertTrue(
            (
                self.state_root
                / REAL_DOCKER_EXECUTION_PROFILE
                / "real-lab"
                / "state.json"
            ).is_file()
        )
        self.assertFalse((self.state_root / "real-lab" / "state.json").exists())
        self.assertNotIn(str(self.base), stdout)
        self.assertNotIn("offline/fake-docker", stdout)
        self.assertNotIn("ownership", stdout)
        self.assertNotIn("version", stdout)
        self.assertTrue(runtime.closed)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["executor_kind"], "real_docker")
        self.assertEqual(manifest["result"], "passed")

    def test_forward_failure_is_cleaned_and_sealed_failed_clean(self) -> None:
        runtime = _FakeRuntime()
        runtime.runner.inject_failure(DockerOperation.EXEC_GUEST)
        code, _stdout, stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: runtime
        )
        self.assertEqual(code, 5)
        self.assertIn("fixture runner failure", stderr)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["result"],
            "failed_clean",
        )
        self.assertFalse(runtime.runner.images)
        self.assertFalse(runtime.runner.containers)
        self.assertFalse(runtime.runner.volumes)
        self.assertTrue(runtime.closed)

    def test_logout_error_cannot_mask_removal_residue(self) -> None:
        runner = _LogoutFailureRunner(fail_remove=True)
        runtime = _FakeRuntime(runner)
        code, _stdout, stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: runtime
        )

        self.assertEqual(code, 7)
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertTrue(runner.logout_failed)
        self.assertTrue(runner.remove_failed)
        self.assertTrue(runner.containers)
        self.assertFalse(self.output.exists())
        namespace = self.state_root / REAL_DOCKER_EXECUTION_PROFILE
        with LabStateStore(
            namespace, REAL_DOCKER_EXECUTION_PROFILE
        ).locked("real-lab") as locked:
            state = locked.load()
        self.assertEqual(state.phase, StatePhase.DIRTY)
        self.assertEqual(
            [(item.step, item.outcome) for item in state.operations[:4]],
            [
                ("create", "succeeded"),
                ("install", "succeeded"),
                ("test", "succeeded"),
                ("evidence", "succeeded"),
            ],
        )
        self.assertTrue(runtime.closed)

    def test_verified_clean_seal_preserves_logout_error(self) -> None:
        runner = _LogoutFailureRunner(fail_remove=False)
        runtime = _FakeRuntime(runner)
        code, _stdout, stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: runtime
        )

        self.assertEqual(code, 5)
        self.assertEqual(stderr, "error: fixture runner failure\n")
        self.assertTrue(runner.logout_failed)
        self.assertFalse(runner.images)
        self.assertFalse(runner.containers)
        self.assertFalse(runner.volumes)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["result"],
            "failed_clean",
        )
        namespace = self.state_root / REAL_DOCKER_EXECUTION_PROFILE
        with LabStateStore(
            namespace, REAL_DOCKER_EXECUTION_PROFILE
        ).locked("real-lab") as locked:
            self.assertEqual(locked.load().phase, StatePhase.FAILED_CLEAN)
        self.assertTrue(runtime.closed)

    def test_logout_error_cannot_mask_permanent_taint_hold(self) -> None:
        runner = _LogoutFailureRunner(fail_remove=False)
        runtime = _FakeRuntime(runner)
        original_logout = FixtureLifecycle.logout

        def taint_then_logout(lifecycle):
            lifecycle.mark_shell_tainted()
            return original_logout(lifecycle)

        with mock.patch.object(
            FixtureLifecycle, "logout", taint_then_logout
        ):
            code, _stdout, stderr = self.run_main(
                *self.run_arguments(), runtime_factory=lambda: runtime
            )

        self.assertEqual(code, 7)
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertTrue(runner.logout_failed)
        self.assertFalse(runner.images)
        self.assertFalse(runner.containers)
        self.assertFalse(runner.volumes)
        self.assertFalse(self.output.exists())
        namespace = self.state_root / REAL_DOCKER_EXECUTION_PROFILE
        with LabStateStore(
            namespace, REAL_DOCKER_EXECUTION_PROFILE
        ).locked("real-lab") as locked:
            state = locked.load()
        self.assertEqual(state.phase, StatePhase.DIRTY)
        self.assertTrue(state.tainted)
        self.assertTrue(runtime.closed)

    def test_recovery_probes_then_runs_cleanup_only(self) -> None:
        abrupt = _FakeRuntime(_RaiseOnceRunner(SystemExit(99)))
        code, _stdout, _stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: abrupt
        )
        self.assertEqual(code, 99)
        self.assertFalse(self.output.exists())

        recovery = _FakeRuntime()
        code, stdout, stderr = self.run_main(
            *self.recover_arguments(),
            "--json",
            runtime_factory=lambda: self.fail("forward runtime was discovered"),
            cleanup_runtime_factory=lambda: recovery,
        )
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(stdout)["phase"], "DIRTY")
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertFalse(self.output.exists())
        self.assertEqual(
            recovery.calls,
            [
                "probe_daemon",
                "bind_snapshot_for_cleanup",
                "cleanup_spec",
                "commands",
                "close",
            ],
        )
        flattened = "\n".join("\0".join(command) for command in recovery.runner.commands)
        self.assertNotIn("\0build\0", flattened)
        self.assertNotIn("\0create\0", flattened)
        self.assertNotIn("\0start\0", flattened)
        self.assertTrue(recovery.closed)

    def test_recovery_probe_failure_leaves_state_bytes_unchanged(self) -> None:
        abrupt = _FakeRuntime(_RaiseOnceRunner(SystemExit(99)))
        code, _stdout, _stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: abrupt
        )
        self.assertEqual(code, 99)
        state_path = (
            self.state_root
            / REAL_DOCKER_EXECUTION_PROFILE
            / "real-lab"
            / "state.json"
        )
        before = state_path.read_bytes()

        unavailable = _FakeRuntime(
            probe_error=UnsupportedError("daemon unavailable")
        )
        code, stdout, stderr = self.run_main(
            *self.recover_arguments(),
            runtime_factory=lambda: self.fail("forward runtime was discovered"),
            cleanup_runtime_factory=lambda: unavailable,
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("unsupported operation", stderr)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(unavailable.calls, ["probe_daemon", "close"])
        self.assertFalse(self.output.exists())

    def test_sigkill_after_snapshot_before_artifact_bind_is_recoverable(self) -> None:
        class ExitAfterSnapshot(_FakeRuntime):
            def capture_snapshot(inner_self, path: str) -> None:
                super(ExitAfterSnapshot, inner_self).capture_snapshot(path)
                raise SystemExit(137)

        abrupt = ExitAfterSnapshot()
        code, stdout, stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: abrupt
        )
        self.assertEqual((code, stdout, stderr), (137, "", ""))
        snapshot = (
            self.state_root
            / REAL_DOCKER_EXECUTION_PROFILE
            / "real-lab"
            / "runtime-snapshot"
        )
        self.assertTrue(snapshot.is_dir())
        with LabStateStore(
            self.state_root / REAL_DOCKER_EXECUTION_PROFILE,
            REAL_DOCKER_EXECUTION_PROFILE,
        ).locked("real-lab") as locked:
            state = locked.load()
        self.assertEqual(state.phase, StatePhase.NEW)
        self.assertEqual(
            dict(state.baseline_equalities), {"runtime_snapshot_bound": False}
        )

        recovery = _FakeRuntime(abrupt.runner)
        code, stdout, stderr = self.run_main(
            *self.recover_arguments(),
            "--json",
            cleanup_runtime_factory=lambda: recovery,
        )
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(stdout)["phase"], "DIRTY")
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertFalse(snapshot.exists())
        flattened = "\n".join(
            "\0".join(command) for command in recovery.runner.commands
        )
        self.assertNotIn("\0build\0", flattened)
        self.assertNotIn("\0create\0", flattened)
        self.assertNotIn("\0start\0", flattened)

    def test_sigkill_after_artifact_bind_before_create_is_recoverable(self) -> None:
        abrupt = _FakeRuntime()
        with mock.patch.object(
            FixtureLifecycle, "create", side_effect=SystemExit(137)
        ):
            code, stdout, stderr = self.run_main(
                *self.run_arguments(), runtime_factory=lambda: abrupt
            )
        self.assertEqual((code, stdout, stderr), (137, "", ""))
        snapshot = (
            self.state_root
            / REAL_DOCKER_EXECUTION_PROFILE
            / "real-lab"
            / "runtime-snapshot"
        )
        self.assertTrue(snapshot.is_dir())
        with LabStateStore(
            self.state_root / REAL_DOCKER_EXECUTION_PROFILE,
            REAL_DOCKER_EXECUTION_PROFILE,
        ).locked("real-lab") as locked:
            state = locked.load()
        self.assertEqual(state.phase, StatePhase.NEW)
        self.assertEqual(
            dict(state.baseline_equalities), {"runtime_snapshot_bound": True}
        )
        self.assertEqual(state.artifact_evidence["version"], "1.0.0")

        recovery = _FakeRuntime(abrupt.runner)
        code, stdout, stderr = self.run_main(
            *self.recover_arguments(),
            "--json",
            cleanup_runtime_factory=lambda: recovery,
        )
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(stdout)["phase"], "DIRTY")
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertFalse(snapshot.exists())
        flattened = "\n".join(
            "\0".join(command) for command in recovery.runner.commands
        )
        self.assertNotIn("\0build\0", flattened)
        self.assertNotIn("\0create\0", flattened)
        self.assertNotIn("\0start\0", flattened)

    def test_explicit_recovery_converts_every_stable_forward_phase_without_reexecution(self) -> None:
        for phase in (
            StatePhase.NEW,
            StatePhase.CREATED,
            StatePhase.INSTALLED,
            StatePhase.TESTED,
        ):
            with self.subTest(phase=phase.value):
                state_root = self.base / ("stable-" + phase.value.lower())
                output = self.base / ("stable-" + phase.value.lower() + ".json")
                setup = self.prepare_stable_phase(state_root, phase)
                command_offset = len(setup.runner.commands)
                recovery = _FakeRuntime(setup.runner)
                code, stdout, stderr = self.run_main(
                    "conformance-recover",
                    "--lab-id",
                    "real-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    "--json",
                    runtime_factory=lambda: self.fail(
                        "forward runtime was discovered"
                    ),
                    cleanup_runtime_factory=lambda: recovery,
                )
                if phase is StatePhase.NEW:
                    self.assertEqual(code, 7)
                    self.assertEqual(json.loads(stdout)["phase"], "DIRTY")
                    self.assertEqual(stderr, "error: cleanup incomplete\n")
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")
                    self.assertEqual(json.loads(stdout)["phase"], "FAILED_CLEAN")
                    self.assertEqual(
                        json.loads(output.read_text(encoding="utf-8"))["result"],
                        "failed_clean",
                    )
                recovery_commands = setup.runner.commands[command_offset:]
                flattened = "\n".join(
                    "\0".join(command) for command in recovery_commands
                )
                self.assertNotIn("\0build\0", flattened)
                self.assertNotIn("\0create\0", flattened)
                self.assertNotIn("\0start\0", flattened)
                self.assertNotIn("\0install", flattened)
                self.assertNotIn("\0test", flattened)

    def test_run_interruption_in_stable_created_phase_enters_cleanup_only(self) -> None:
        runtime = _FakeRuntime()

        def interrupt_before_install(_lifecycle):
            raise KeyboardInterrupt()

        with mock.patch.object(
            FixtureLifecycle, "install", new=interrupt_before_install
        ):
            code, _stdout, stderr = self.run_main(
                *self.run_arguments(), runtime_factory=lambda: runtime
            )
        self.assertEqual(code, 130)
        self.assertEqual(stderr, "error: interrupted\n")
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["result"],
            "failed_clean",
        )
        flattened = "\n".join(
            "\0".join(command) for command in runtime.runner.commands
        )
        self.assertEqual(flattened.count("\0build\0"), 0)
        self.assertNotIn("\0install", flattened)

    def test_status_does_not_discover_or_probe_runtime(self) -> None:
        runtime = _FakeRuntime()
        self.assertEqual(
            self.run_main(*self.run_arguments(), runtime_factory=lambda: runtime)[0],
            0,
        )
        calls = []
        code, stdout, stderr = self.run_main(
            "conformance-status",
            "--lab-id",
            "real-lab",
            "--state-root",
            str(self.state_root),
            "--json",
            runtime_factory=lambda: calls.append("runtime"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(
            set(payload),
            {"lab_id", "provider_id", "phase", "revision", "tainted"},
        )
        self.assertEqual(payload["phase"], "PASSED")
        self.assertEqual(calls, [])

    def test_status_refuses_cross_profile_state_without_runtime(self) -> None:
        self.state_root.mkdir(mode=0o700)
        fixture_namespace = self.state_root / REAL_DOCKER_EXECUTION_PROFILE
        fixture_output = self.base / "fixture.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            fixture_code = fixture_main(
                (
                    "fixture-run",
                    "--lab-id",
                    "real-lab",
                    "--state-root",
                    str(fixture_namespace),
                    "--evidence-output",
                    str(fixture_output),
                )
            )
        self.assertEqual(fixture_code, 0)
        calls = []
        code, _stdout, stderr = self.run_main(
            "conformance-status",
            "--lab-id",
            "real-lab",
            "--state-root",
            str(self.state_root),
            runtime_factory=lambda: calls.append("runtime"),
        )
        self.assertEqual(code, 4)
        self.assertIn("safety invariant refused", stderr)
        self.assertEqual(calls, [])

    def test_keyboard_interrupt_cleans_closes_and_returns_130(self) -> None:
        runtime = _FakeRuntime(_RaiseOnceRunner(KeyboardInterrupt()))
        code, _stdout, stderr = self.run_main(
            *self.run_arguments(), runtime_factory=lambda: runtime
        )
        self.assertEqual(code, 7)
        self.assertEqual(stderr, "error: cleanup incomplete\n")
        self.assertTrue(runtime.closed)
        self.assertFalse(self.output.exists())

    def test_close_fault_after_clean_result_has_one_stable_diagnostic(self) -> None:
        for close_error, expected_code, diagnostic in (
            (RuntimeError("close failed"), 1, "error: internal error\n"),
            (KeyboardInterrupt(), 130, "error: interrupted\n"),
        ):
            with self.subTest(error=type(close_error).__name__):
                state_root = self.base / ("clean-" + type(close_error).__name__)
                output = self.base / ("clean-" + type(close_error).__name__ + ".json")
                runtime = _FakeRuntime(close_error=close_error)
                code, stdout, stderr = self.run_main(
                    "conformance-run",
                    "--lab-id",
                    "real-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    "--json",
                    runtime_factory=lambda: runtime,
                )
                self.assertEqual(code, expected_code)
                self.assertEqual(stderr, diagnostic)
                self.assertEqual(json.loads(stdout)["phase"], "PASSED")
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["result"], "passed")

    def test_close_fault_cannot_replace_cleanup_incomplete_result(self) -> None:
        for close_error in (RuntimeError("close failed"), KeyboardInterrupt()):
            with self.subTest(error=type(close_error).__name__):
                state_root = self.base / ("dirty-" + type(close_error).__name__)
                output = self.base / ("dirty-" + type(close_error).__name__ + ".json")
                runtime = _FakeRuntime(close_error=close_error)
                runtime.runner.inject_failure(DockerOperation.REMOVE_CONTAINER)
                code, _stdout, stderr = self.run_main(
                    "conformance-run",
                    "--lab-id",
                    "real-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    runtime_factory=lambda: runtime,
                )
                self.assertEqual(code, 7)
                self.assertEqual(stderr, "error: cleanup incomplete\n")
                self.assertFalse(output.exists())

    def test_help_does_not_construct_runtime(self) -> None:
        calls = []
        code, _stdout, _stderr = self.run_main(
            "--help", runtime_factory=lambda: calls.append("runtime")
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_launcher_ignores_hostile_import_environment(self) -> None:
        launcher = Path(__file__).resolve().parents[3] / "scripts" / "unified-ext-lab-real-docker"
        with tempfile.TemporaryDirectory() as temporary:
            hostile = Path(temporary)
            marker = hostile / "executed"
            payload = (
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ['HOSTILE_MARKER']).write_text('executed', encoding='utf-8')\n"
            )
            (hostile / "sitecustomize.py").write_text(payload, encoding="utf-8")
            (hostile / "uuid.py").write_text(payload, encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONPATH": str(hostile),
                    "PYTHONHOME": str(hostile),
                    "HOSTILE_MARKER": str(marker),
                }
            )
            result = subprocess.run(
                ["/bin/sh", str(launcher), "--help"],
                cwd=str(launcher.parent.parent),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_launcher_rejects_symlink_entrypoint(self) -> None:
        launcher = Path(__file__).resolve().parents[3] / "scripts" / "unified-ext-lab-real-docker"
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "unified-ext-lab-real-docker"
            link.symlink_to(launcher)
            result = subprocess.run(
                ["/bin/sh", str(link), "--help"],
                cwd=temporary,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("symlink launcher is not supported", result.stderr)


if __name__ == "__main__":
    unittest.main()
