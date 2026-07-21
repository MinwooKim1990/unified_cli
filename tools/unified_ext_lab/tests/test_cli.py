"""Offline integration tests for the source-only fixture command line."""

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

from tools.unified_ext_lab.cli import main
from tools.unified_ext_lab.docker import DockerOperation, classify_docker_argv
from tools.unified_ext_lab.errors import UsageStateError
from tools.unified_ext_lab.fake_docker import FakeRunner
from tools.unified_ext_lab.lifecycle import FixtureLifecycle


class _RaiseOnceRunner(FakeRunner):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.raised = False

    def run(self, argv, *, timeout):
        if not self.raised:
            self.raised = True
            raise self.error
        return super().run(argv, timeout=timeout)


class _RaiseOnceOnOperationRunner(FakeRunner):
    def __init__(self, error, operation: DockerOperation):
        super().__init__()
        self.error = error
        self.operation = operation
        self.raised = False

    def run(self, argv, *, timeout):
        if (
            not self.raised
            and classify_docker_argv(argv, self._operations) is self.operation
        ):
            self.raised = True
            raise self.error
        return super().run(argv, timeout=timeout)


def _bound_factory(runner: FakeRunner):
    def factory(spec):
        runner.register_spec(spec)
        return runner

    return factory


class FixtureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.state_root = self.base / "state"
        self.output = self.base / "manifest.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_main(self, *arguments: str, runner_factory=FakeRunner):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments, runner_factory=runner_factory)
        return code, stdout.getvalue(), stderr.getvalue()

    def fixture_arguments(self):
        return (
            "fixture-run",
            "--lab-id",
            "fixture-lab",
            "--state-root",
            str(self.state_root),
            "--evidence-output",
            str(self.output),
        )

    def isolated_fixture(self, label: str):
        test_root = self.base / label
        test_root.mkdir(mode=0o700)
        return test_root / "state", test_root / "manifest.json"

    @staticmethod
    def fixture_arguments_for(state_root: Path, output: Path):
        return (
            "fixture-run",
            "--lab-id",
            "fixture-lab",
            "--state-root",
            str(state_root),
            "--evidence-output",
            str(output),
        )

    @staticmethod
    def recovery_arguments_for(state_root: Path, output: Path):
        return (
            "fixture-recover",
            "--lab-id",
            "fixture-lab",
            "--state-root",
            str(state_root),
            "--evidence-output",
            str(output),
        )

    def prepare_recovery(self, state_root: Path, output: Path) -> None:
        abrupt = _RaiseOnceRunner(SystemExit(99))
        code, _stdout, _stderr = self.run_main(
            *self.fixture_arguments_for(state_root, output),
            runner_factory=_bound_factory(abrupt),
        )
        self.assertEqual(code, 99)
        self.assertFalse(output.exists())

    def test_happy_run_writes_passed_manifest_and_removes_all_resources(self) -> None:
        runner = FakeRunner()
        code, _stdout, stderr = self.run_main(
            *self.fixture_arguments(),
            "--json",
            runner_factory=_bound_factory(runner),
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertFalse(runner.images)
        self.assertFalse(runner.containers)
        self.assertFalse(runner.volumes)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "passed")
        self.assertFalse(manifest["promotion_eligible"])

    def test_forward_failure_is_cleaned_and_sealed_as_failed_clean(self) -> None:
        runner = FakeRunner()
        runner.inject_failure(DockerOperation.EXEC_GUEST)
        code, _stdout, _stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(runner)
        )
        self.assertEqual(code, 5)
        self.assertFalse(runner.images)
        self.assertFalse(runner.containers)
        self.assertFalse(runner.volumes)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")

    def test_refuses_existing_state_or_evidence(self) -> None:
        code, _stdout, _stderr = self.run_main(*self.fixture_arguments())
        self.assertEqual(code, 0)
        code, _stdout, _stderr = self.run_main(*self.fixture_arguments())
        self.assertEqual(code, 2)

    def test_status_is_redacted(self) -> None:
        code, _stdout, _stderr = self.run_main(*self.fixture_arguments())
        self.assertEqual(code, 0)
        code, stdout, _stderr = self.run_main(
            "status",
            "--lab-id",
            "fixture-lab",
            "--state-root",
            str(self.state_root),
            "--json",
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(set(payload), {"lab_id", "provider_id", "phase", "revision", "tainted"})
        self.assertEqual(payload["lab_id"], "fixture-lab")
        self.assertEqual(payload["provider_id"], "synthetic")
        self.assertEqual(payload["phase"], "PASSED")
        self.assertIsInstance(payload["revision"], int)
        self.assertFalse(payload["tainted"])
        self.assertNotIn("ownership", stdout)
        self.assertNotIn(str(self.base), stdout)

    def test_help_and_describe_do_not_construct_or_call_runner(self) -> None:
        calls = []

        def factory(spec):
            calls.append("constructed")
            return FakeRunner(spec)

        self.assertEqual(self.run_main("--help", runner_factory=factory)[0], 0)
        self.assertEqual(self.run_main("describe", runner_factory=factory)[0], 0)
        self.assertEqual(calls, [])

    def test_stable_usage_and_runner_exit_codes(self) -> None:
        self.assertEqual(
            self.run_main(
                "fixture-run",
                "--lab-id",
                "INVALID",
                "--state-root",
                str(self.state_root),
                "--evidence-output",
                str(self.output),
            )[0],
            2,
        )
        runner = FakeRunner()
        runner.inject_failure(DockerOperation.BUILD_IMAGE)
        self.assertEqual(
            self.run_main(
                *self.fixture_arguments(), runner_factory=_bound_factory(runner)
            )[0],
            5,
        )

    def test_cleanup_failure_cannot_return_success_after_resources_are_gone(self) -> None:
        runner = FakeRunner()
        runner.inject_failure(DockerOperation.STOP_CONTAINER)
        code, _stdout, _stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(runner)
        )
        self.assertEqual(code, 7)
        self.assertFalse(runner.images)
        self.assertFalse(runner.containers)
        self.assertFalse(runner.volumes)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")

    def test_cleanup_residue_exit_takes_priority_over_forward_failure(self) -> None:
        runner = FakeRunner()
        runner.inject_failure(DockerOperation.EXEC_GUEST)
        runner.inject_failure(DockerOperation.REMOVE_CONTAINER)
        code, _stdout, _stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(runner)
        )
        self.assertEqual(code, 7)
        self.assertTrue(runner.containers)
        self.assertFalse(self.output.exists())

    def test_keyboard_interrupt_is_durable_cleaned_and_returns_130(self) -> None:
        runner = _RaiseOnceRunner(KeyboardInterrupt())
        code, _stdout, stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(runner)
        )
        self.assertEqual(code, 130)
        self.assertIn("interrupted", stderr)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], "failed_clean")
        interrupted = [
            item for item in manifest["operations"]
            if item["error_code"] == "interrupted"
        ]
        self.assertEqual(len(interrupted), 1)

    def test_unexpected_runner_exception_is_cleaned_and_returns_stable_one(self) -> None:
        runner = _RaiseOnceRunner(RuntimeError("unexpected"))
        code, _stdout, stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(runner)
        )
        self.assertEqual(code, 1)
        self.assertIn("interrupted", stderr)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["result"],
            "failed_clean",
        )

    def test_nested_stabilization_interrupt_does_not_skip_run_cleanup(self) -> None:
        original_status = FixtureLifecycle.status
        for forward_error, expected in (
            (KeyboardInterrupt(), 130),
            (RuntimeError("unexpected"), 1),
        ):
            with self.subTest(error=type(forward_error).__name__):
                state_root, output = self.isolated_fixture(
                    "nested-run-" + type(forward_error).__name__
                )
                runner = _RaiseOnceOnOperationRunner(
                    forward_error, DockerOperation.EXEC_GUEST
                )
                status_calls = [0]

                def interrupt_first_status(lifecycle):
                    status_calls[0] += 1
                    if status_calls[0] == 1:
                        raise KeyboardInterrupt()
                    return original_status(lifecycle)

                with mock.patch.object(
                    FixtureLifecycle, "status", new=interrupt_first_status
                ):
                    code, _stdout, stderr = self.run_main(
                        *self.fixture_arguments_for(state_root, output),
                        runner_factory=_bound_factory(runner),
                    )
                self.assertEqual(code, expected)
                self.assertIn("interrupted", stderr)
                self.assertFalse(runner.images)
                self.assertFalse(runner.containers)
                self.assertFalse(runner.volumes)
                self.assertEqual(
                    json.loads(output.read_text(encoding="utf-8"))["result"],
                    "failed_clean",
                )

    def test_run_final_status_uncertainty_always_returns_cleanup_incomplete(self) -> None:
        original_status = FixtureLifecycle.status
        triggers = (None, KeyboardInterrupt(), RuntimeError("unexpected"))
        final_errors = (
            UsageStateError("status unavailable"),
            RuntimeError("status unavailable"),
            KeyboardInterrupt(),
        )
        for trigger in triggers:
            for final_error in final_errors:
                trigger_name = "none" if trigger is None else type(trigger).__name__
                final_name = type(final_error).__name__
                with self.subTest(trigger=trigger_name, final_error=final_name):
                    state_root, output = self.isolated_fixture(
                        "run-final-" + trigger_name + "-" + final_name
                    )
                    if trigger is None:
                        runner = FakeRunner()
                    else:
                        runner = _RaiseOnceOnOperationRunner(
                            trigger, DockerOperation.EXEC_GUEST
                        )

                    def fail_terminal_status(lifecycle):
                        state = original_status(lifecycle)
                        if state.phase.value in ("PASSED", "FAILED_CLEAN"):
                            raise final_error
                        return state

                    with mock.patch.object(
                        FixtureLifecycle, "status", new=fail_terminal_status
                    ):
                        code, _stdout, stderr = self.run_main(
                            *self.fixture_arguments_for(state_root, output),
                            runner_factory=_bound_factory(runner),
                        )
                    self.assertEqual(code, 7)
                    self.assertIn("cleanup incomplete", stderr)
                    self.assertTrue(output.exists())

    def test_recovery_terminal_uncertainty_preserves_exception_exit(self) -> None:
        original_seal = FixtureLifecycle.seal
        for recovery_error, expected in (
            (KeyboardInterrupt(), 130),
            (RuntimeError("unexpected"), 1),
        ):
            with self.subTest(error=type(recovery_error).__name__):
                state_root, output = self.isolated_fixture(
                    "recover-terminal-" + type(recovery_error).__name__
                )
                self.prepare_recovery(state_root, output)

                def seal_then_raise(lifecycle, output_path):
                    original_seal(lifecycle, output_path)
                    raise recovery_error

                with mock.patch.object(
                    FixtureLifecycle, "seal", new=seal_then_raise
                ):
                    code, _stdout, stderr = self.run_main(
                        *self.recovery_arguments_for(state_root, output),
                        runner_factory=FakeRunner,
                    )
                self.assertEqual(code, expected)
                self.assertIn("interrupted", stderr)
                self.assertEqual(
                    json.loads(output.read_text(encoding="utf-8"))["result"],
                    "failed_clean",
                )

    def test_nested_stabilization_interrupt_does_not_escape_recovery(self) -> None:
        original_seal = FixtureLifecycle.seal
        original_status = FixtureLifecycle.status
        for recovery_error, expected in (
            (KeyboardInterrupt(), 130),
            (RuntimeError("unexpected"), 1),
        ):
            with self.subTest(error=type(recovery_error).__name__):
                state_root, output = self.isolated_fixture(
                    "nested-recover-" + type(recovery_error).__name__
                )
                self.prepare_recovery(state_root, output)
                terminal_status_interrupted = [False]

                def seal_then_raise(lifecycle, output_path):
                    original_seal(lifecycle, output_path)
                    raise recovery_error

                def interrupt_first_status(lifecycle):
                    state = original_status(lifecycle)
                    if (
                        state.phase.value in ("PASSED", "FAILED_CLEAN")
                        and not terminal_status_interrupted[0]
                    ):
                        terminal_status_interrupted[0] = True
                        raise KeyboardInterrupt()
                    return state

                with mock.patch.object(
                    FixtureLifecycle, "seal", new=seal_then_raise
                ), mock.patch.object(
                    FixtureLifecycle, "status", new=interrupt_first_status
                ):
                    code, _stdout, stderr = self.run_main(
                        *self.recovery_arguments_for(state_root, output),
                        runner_factory=FakeRunner,
                    )
                self.assertEqual(code, expected)
                self.assertIn("interrupted", stderr)
                self.assertTrue(output.exists())

    def test_recovery_final_status_uncertainty_always_returns_cleanup_incomplete(self) -> None:
        original_seal = FixtureLifecycle.seal
        original_status = FixtureLifecycle.status
        triggers = (None, KeyboardInterrupt(), RuntimeError("unexpected"))
        final_errors = (
            UsageStateError("status unavailable"),
            RuntimeError("status unavailable"),
            KeyboardInterrupt(),
        )
        for trigger in triggers:
            for final_error in final_errors:
                trigger_name = "none" if trigger is None else type(trigger).__name__
                final_name = type(final_error).__name__
                with self.subTest(trigger=trigger_name, final_error=final_name):
                    state_root, output = self.isolated_fixture(
                        "recover-final-" + trigger_name + "-" + final_name
                    )
                    self.prepare_recovery(state_root, output)

                    def fail_terminal_status(lifecycle):
                        state = original_status(lifecycle)
                        if state.phase.value in ("PASSED", "FAILED_CLEAN"):
                            raise final_error
                        return state

                    if trigger is None:
                        seal_patch = mock.patch.object(
                            FixtureLifecycle, "seal", new=original_seal
                        )
                    else:
                        def seal_then_raise(lifecycle, output_path):
                            original_seal(lifecycle, output_path)
                            raise trigger

                        seal_patch = mock.patch.object(
                            FixtureLifecycle, "seal", new=seal_then_raise
                        )
                    with seal_patch, mock.patch.object(
                        FixtureLifecycle, "status", new=fail_terminal_status
                    ):
                        code, _stdout, stderr = self.run_main(
                            *self.recovery_arguments_for(state_root, output),
                            runner_factory=FakeRunner,
                        )
                    self.assertEqual(code, 7)
                    self.assertIn("cleanup incomplete", stderr)
                    self.assertTrue(output.exists())

    def test_interruption_with_cleanup_residue_returns_cleanup_incomplete(self) -> None:
        for error in (KeyboardInterrupt(), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                test_root = self.base / type(error).__name__
                test_root.mkdir(mode=0o700)
                state_root = test_root / "state"
                output = test_root / "manifest.json"
                runner = _RaiseOnceOnOperationRunner(error, DockerOperation.EXEC_GUEST)
                runner.inject_failure(DockerOperation.REMOVE_CONTAINER)
                code, _stdout, stderr = self.run_main(
                    "fixture-run",
                    "--lab-id",
                    "fixture-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    runner_factory=_bound_factory(runner),
                )
                self.assertEqual(code, 7)
                self.assertIn("cleanup incomplete", stderr)
                self.assertTrue(runner.containers)
                self.assertFalse(output.exists())

    def test_recovery_interruption_with_cleanup_residue_returns_cleanup_incomplete(self) -> None:
        for error in (KeyboardInterrupt(), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                test_root = self.base / type(error).__name__
                test_root.mkdir(mode=0o700)
                state_root = test_root / "state"
                output = test_root / "manifest.json"
                runner = _RaiseOnceOnOperationRunner(SystemExit(99), DockerOperation.EXEC_GUEST)
                code, _stdout, _stderr = self.run_main(
                    "fixture-run",
                    "--lab-id",
                    "fixture-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    runner_factory=_bound_factory(runner),
                )
                self.assertEqual(code, 99)

                runner.error = error
                runner.operation = DockerOperation.REMOVE_VOLUME
                runner.raised = False
                runner.inject_failure(DockerOperation.REMOVE_CONTAINER)
                code, _stdout, stderr = self.run_main(
                    "fixture-recover",
                    "--lab-id",
                    "fixture-lab",
                    "--state-root",
                    str(state_root),
                    "--evidence-output",
                    str(output),
                    runner_factory=_bound_factory(runner),
                )
                self.assertEqual(code, 7)
                self.assertIn("cleanup incomplete", stderr)
                self.assertTrue(runner.containers)
                self.assertFalse(output.exists())

    def test_launcher_ignores_hostile_python_import_environment(self) -> None:
        launcher = Path(__file__).resolve().parents[3] / "scripts" / "unified-ext-lab"
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
                [str(launcher), "describe", "--json"],
                cwd=str(launcher.parent.parent),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["provider_id"], "synthetic")
        self.assertFalse(marker.exists())

    def test_launcher_rejects_a_symlink_entrypoint(self) -> None:
        launcher = Path(__file__).resolve().parents[3] / "scripts" / "unified-ext-lab"
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "unified-ext-lab"
            link.symlink_to(launcher)
            result = subprocess.run(
                [str(link), "describe", "--json"],
                cwd=temporary,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("symlink launcher is not supported", result.stderr)

    def test_fixture_recover_after_abrupt_exit_runs_cleanup_only(self) -> None:
        abrupt = _RaiseOnceRunner(SystemExit(99))
        code, _stdout, _stderr = self.run_main(
            *self.fixture_arguments(), runner_factory=_bound_factory(abrupt)
        )
        self.assertEqual(code, 99)
        self.assertFalse(self.output.exists())

        recovery_runner = FakeRunner()
        code, stdout, stderr = self.run_main(
            "fixture-recover",
            "--lab-id",
            "fixture-lab",
            "--state-root",
            str(self.state_root),
            "--evidence-output",
            str(self.output),
            "--json",
            runner_factory=_bound_factory(recovery_runner),
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["phase"], "FAILED_CLEAN")
        flattened = "\n".join("\0".join(command) for command in recovery_runner.commands)
        self.assertNotIn("\0build\0", flattened)
        self.assertNotIn("\0create\0", flattened)
        self.assertNotIn("\0start\0", flattened)
        self.assertNotIn(str(self.base), stdout)

    def test_fixture_recover_exposes_no_provider_executable_url_account_or_shell(self) -> None:
        for forbidden in (
            "--provider",
            "--executable",
            "--url",
            "--account",
            "--shell",
        ):
            with self.subTest(forbidden=forbidden):
                code, _stdout, _stderr = self.run_main(
                    "fixture-recover",
                    "--lab-id",
                    "fixture-lab",
                    "--state-root",
                    str(self.state_root),
                    "--evidence-output",
                    str(self.output),
                    forbidden,
                    "value",
                )
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
