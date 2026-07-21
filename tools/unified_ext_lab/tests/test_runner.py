"""Tests for bounded, identity-bound subprocess execution."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

from tools.unified_ext_lab import runner as runner_module
from tools.unified_ext_lab.errors import (
    InvariantRefusalError,
    RunnerFailureError,
    UsageStateError,
)
from tools.unified_ext_lab.runner import CommandResult, SubprocessRunner


class SubprocessRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = os.path.realpath(
            tempfile.mkdtemp(prefix="unified-ext-runner-test-")
        )
        self.executable = os.path.join(self.directory, "python-copy")
        # The system Python launcher on macOS uses an
        # @executable_path-relative framework and cannot be relocated.  A
        # regular executable script remains safely copy-bindable by the
        # runner while re-executing the active interpreter on every version.
        with open(self.executable, "w", encoding="utf-8") as handle:
            handle.write(
                "#!"
                + sys.executable
                + "\nimport os, sys\n"
                + "os.execv(sys.executable, (sys.executable,) + tuple(sys.argv[1:]))\n"
            )
        os.chmod(self.executable, 0o700)
        self.runners = []

    def tearDown(self) -> None:
        for runner in self.runners:
            runner.close()
        shutil.rmtree(self.directory)

    def runner(self, *, limit: int = 1024 * 1024) -> SubprocessRunner:
        runner = SubprocessRunner(self.executable, max_output_bytes=limit)
        self.runners.append(runner)
        return runner

    def run_python(
        self, runner: SubprocessRunner, program: str, *, timeout: float = 2
    ) -> CommandResult:
        return runner.run((self.executable, "-c", program), timeout=timeout)

    def capture_popen(self):
        real_popen = runner_module.subprocess.Popen
        created = []

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            created.append(process)
            return process

        return mock.patch.object(
            runner_module.subprocess, "Popen", side_effect=capture
        ), created

    def assert_process_finalized(self, process) -> None:
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_result_is_bounded_immutable_and_preserves_exact_argv(self):
        runner = self.runner(limit=32)
        result = self.run_python(
            runner,
            "import sys; print('out'); print('err', file=sys.stderr)",
        )
        self.assertEqual(result.argv[0], self.executable)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")
        with self.assertRaises(FrozenInstanceError):
            result.stdout = "changed"

    def test_private_environment_does_not_copy_ambient_credentials(self):
        old = os.environ.get("SYNTHETIC_SECRET")
        os.environ["SYNTHETIC_SECRET"] = "must-not-cross"
        try:
            runner = self.runner()
            result = self.run_python(
                runner,
                "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))",
            )
        finally:
            if old is None:
                os.environ.pop("SYNTHETIC_SECRET", None)
            else:
                os.environ["SYNTHETIC_SECRET"] = old
        environment = json.loads(result.stdout)
        self.assertNotIn("SYNTHETIC_SECRET", environment)
        expected = {"DOCKER_CONFIG", "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
        # CoreFoundation may synthesize this process-local locale key on macOS;
        # it is not copied from the runner's ambient environment.
        self.assertEqual(set(environment) - {"__CF_USER_TEXT_ENCODING"}, expected)
        for key in ("DOCKER_CONFIG", "HOME", "TMPDIR"):
            mode = stat.S_IMODE(os.stat(environment[key]).st_mode)
            self.assertEqual(mode, 0o700)
            self.assertTrue(environment[key].startswith(runner._private_root + os.sep))

    def test_timeout_nonzero_and_oversized_output_have_stable_errors(self):
        cases = (
            (
                self.runner(),
                "import time; time.sleep(2)",
                0.05,
                "runner timed out",
            ),
            (self.runner(), "raise SystemExit(9)", 2, "runner command failed"),
            (self.runner(limit=64), "print('x' * 10000)", 2, "runner output exceeded limit"),
        )
        for runner, program, timeout, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RunnerFailureError, "^" + message + "$"):
                    self.run_python(runner, program, timeout=timeout)

    def test_timeout_must_be_finite_and_bounded(self):
        for timeout in (
            float("nan"),
            float("inf"),
            float("-inf"),
            3600.0001,
            10 ** 10000,
        ):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(
                    UsageStateError, "^invalid runner timeout$"
                ):
                    self.run_python(self.runner(), "print('not run')", timeout=timeout)

    def test_cancellation_kills_the_new_process_group(self):
        runner = self.runner()
        observed = []
        events = []
        real_killpg = runner_module.os.killpg
        real_wait = runner_module.subprocess.Popen.wait
        ready = os.path.join(self.directory, "child-ready")
        orphan = os.path.join(self.directory, "orphan-marker")
        program = (
            "import pathlib,subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',"
            + repr(
                "import pathlib,time; time.sleep(0.5); "
                "pathlib.Path({!r}).write_text('orphan')".format(orphan)
            )
            + "]); "
            "pathlib.Path({!r}).write_text('ready'); time.sleep(10)".format(ready)
        )

        def target() -> None:
            try:
                self.run_python(runner, program, timeout=20)
            except Exception as error:  # captured for assertion in this thread
                observed.append(error)

        def signal_group(process_group, signal_number):
            events.append("signal")
            return real_killpg(process_group, signal_number)

        def reap(process, *args, **kwargs):
            events.append("reap")
            return real_wait(process, *args, **kwargs)

        thread = threading.Thread(target=target)
        with mock.patch.object(
            runner_module.os, "killpg", side_effect=signal_group
        ), mock.patch.object(runner_module.subprocess.Popen, "wait", new=reap):
            thread.start()
            deadline = time.monotonic() + 2
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(ready))
            runner.cancel()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], RunnerFailureError)
        self.assertEqual(str(observed[0]), "runner cancelled")
        self.assertEqual(events, ["signal", "reap"])
        time.sleep(0.6)
        self.assertFalse(os.path.exists(orphan))

    def test_parent_exit_never_leaves_background_descendants(self):
        cases = (
            ("inherited-pipes", False, 0.1),
            ("closed-pipes", True, 2.0),
        )
        for label, close_pipes, timeout in cases:
            with self.subTest(label=label):
                runner = self.runner()
                marker = os.path.join(self.directory, label + "-marker")
                child = (
                    "import pathlib,time; time.sleep(0.5); "
                    "pathlib.Path({!r}).write_text('left-running')".format(marker)
                )
                if close_pipes:
                    program = (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable,'-c',"
                        + repr(child)
                        + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                        "stderr=subprocess.DEVNULL,close_fds=True)"
                    )
                    result = self.run_python(runner, program, timeout=timeout)
                    self.assertEqual(result.returncode, 0)
                else:
                    program = (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
                    )
                    with self.assertRaisesRegex(
                        RunnerFailureError, "^runner timed out$"
                    ):
                        self.run_python(runner, program, timeout=timeout)
                time.sleep(0.6)
                self.assertFalse(os.path.exists(marker))

    def test_setup_faults_immediately_finalize_the_started_process(self):
        real_selector = runner_module.selectors.DefaultSelector

        class RegisterFailureSelector:
            def __init__(self):
                self.inner = real_selector()

            def register(self, *args, **kwargs):
                raise RuntimeError("injected selector register failure")

            def close(self):
                self.inner.close()

        cases = (
            (
                "selector-creation",
                lambda: mock.patch.object(
                    runner_module.selectors,
                    "DefaultSelector",
                    side_effect=RuntimeError("injected selector creation failure"),
                ),
                "injected selector creation failure",
            ),
            (
                "set-blocking",
                lambda: mock.patch.object(
                    runner_module.os,
                    "set_blocking",
                    side_effect=RuntimeError("injected set-blocking failure"),
                ),
                "injected set-blocking failure",
            ),
            (
                "selector-register",
                lambda: mock.patch.object(
                    runner_module.selectors,
                    "DefaultSelector",
                    side_effect=RegisterFailureSelector,
                ),
                "injected selector register failure",
            ),
        )
        for label, make_fault, message in cases:
            with self.subTest(label=label):
                runner = self.runner()
                marker = os.path.join(self.directory, label + "-leak")
                program = (
                    "import pathlib,time; time.sleep(0.3); "
                    "pathlib.Path({!r}).write_text('leaked')".format(marker)
                )
                popen_patch, created = self.capture_popen()
                with popen_patch, make_fault():
                    with self.assertRaisesRegex(RuntimeError, "^" + message + "$"):
                        self.run_python(runner, program)
                self.assertEqual(len(created), 1)
                self.assert_process_finalized(created[0])
                time.sleep(0.35)
                self.assertFalse(os.path.exists(marker))

    def test_post_popen_monotonic_interrupt_is_always_finalized(self):
        runner = self.runner()
        marker = os.path.join(self.directory, "post-popen-interrupt-leak")
        program = (
            "import pathlib,time; time.sleep(0.3); "
            "pathlib.Path({!r}).write_text('leaked')".format(marker)
        )
        popen_patch, created = self.capture_popen()
        real_monotonic = runner_module.time.monotonic

        def interrupt_after_popen():
            if created:
                raise KeyboardInterrupt("injected post-Popen interrupt")
            return real_monotonic()

        with popen_patch, mock.patch.object(
            runner_module.time,
            "monotonic",
            side_effect=interrupt_after_popen,
        ):
            with self.assertRaisesRegex(
                KeyboardInterrupt, "injected post-Popen interrupt"
            ):
                self.run_python(runner, program)
        self.assertEqual(len(created), 1)
        self.assert_process_finalized(created[0])
        time.sleep(0.35)
        self.assertFalse(os.path.exists(marker))

    def test_kqueue_registration_fault_is_owned_before_registration(self):
        cases = (("close-succeeds", False), ("close-fails", True))
        for label, close_fails in cases:
            with self.subTest(label=label):
                runner = self.runner()
                popen_patch, created = self.capture_popen()
                events = []
                real_close = runner_module._KqueueExitObserver.close

                def fail_register(observer):
                    events.append("register-failed")
                    raise RuntimeError("injected kqueue registration failure")

                def close_observer(observer):
                    events.append("observer-close")
                    real_close(observer)
                    if close_fails:
                        raise RuntimeError("injected kqueue close failure")

                with popen_patch, mock.patch.object(
                    runner_module.os, "waitid", None, create=True
                ), mock.patch.object(
                    runner_module._KqueueExitObserver,
                    "register",
                    new=fail_register,
                ), mock.patch.object(
                    runner_module._KqueueExitObserver,
                    "close",
                    new=close_observer,
                ):
                    if close_fails:
                        with self.assertRaisesRegex(
                            RunnerFailureError, "^runner cleanup uncertain$"
                        ) as raised:
                            self.run_python(runner, "import time; time.sleep(10)")
                        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
                        self.assertEqual(
                            str(raised.exception.__cause__),
                            "injected kqueue registration failure",
                        )
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "^injected kqueue registration failure$"
                        ):
                            self.run_python(runner, "import time; time.sleep(10)")
                self.assertEqual(events, ["register-failed", "observer-close"])
                self.assertEqual(len(created), 1)
                self.assert_process_finalized(created[0])

    def test_normal_kqueue_close_fault_is_stable_cleanup_uncertainty(self):
        runner = self.runner()
        popen_patch, created = self.capture_popen()
        real_close = runner_module._KqueueExitObserver.close

        def fail_after_close(observer):
            real_close(observer)
            raise RuntimeError("injected final observer close failure")

        with popen_patch, mock.patch.object(
            runner_module.os, "waitid", None, create=True
        ), mock.patch.object(
            runner_module._KqueueExitObserver,
            "close",
            new=fail_after_close,
        ):
            with self.assertRaisesRegex(
                RunnerFailureError, "^runner cleanup uncertain$"
            ) as raised:
                self.run_python(runner, "print('complete')")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(
            str(raised.exception.__cause__),
            "injected final observer close failure",
        )
        self.assertEqual(len(created), 1)
        self.assert_process_finalized(created[0])

    def test_read_baseexception_and_result_fault_cannot_leak_process_resources(self):
        class InjectedBaseException(BaseException):
            pass

        runner = self.runner()
        popen_patch, created = self.capture_popen()
        real_read = runner_module.os.read

        def fail_runner_read(file_descriptor, size):
            if created and file_descriptor in (
                created[0].stdout.fileno(),
                created[0].stderr.fileno(),
            ):
                raise InjectedBaseException("injected read abort")
            return real_read(file_descriptor, size)

        with popen_patch, mock.patch.object(
            runner_module.os,
            "read",
            side_effect=fail_runner_read,
        ):
            with self.assertRaisesRegex(InjectedBaseException, "injected read abort"):
                self.run_python(
                    runner,
                    "import sys,time; print('ready', flush=True); time.sleep(10)",
                )
        self.assertEqual(len(created), 1)
        self.assert_process_finalized(created[0])

        runner = self.runner()
        popen_patch, created = self.capture_popen()
        with popen_patch, mock.patch.object(
            runner_module,
            "CommandResult",
            side_effect=RuntimeError("injected result failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "^injected result failure$"):
                self.run_python(runner, "print('complete')")
        self.assertEqual(len(created), 1)
        self.assert_process_finalized(created[0])

    def test_killpg_failure_reaps_closes_and_reports_cleanup_uncertainty(self):
        runner = self.runner()
        popen_patch, created = self.capture_popen()
        with popen_patch, mock.patch.object(
            runner_module.os,
            "killpg",
            side_effect=PermissionError("injected killpg failure"),
        ):
            with self.assertRaisesRegex(
                RunnerFailureError, "^runner cleanup uncertain$"
            ) as raised:
                self.run_python(runner, "import time; time.sleep(10)", timeout=0.05)
        self.assertIsInstance(raised.exception.__cause__, RunnerFailureError)
        self.assertEqual(str(raised.exception.__cause__), "runner timed out")
        self.assertEqual(len(created), 1)
        self.assert_process_finalized(created[0])

    def test_missing_waitid_uses_kqueue_before_signal_and_reap(self):
        events = []
        real_killpg = runner_module.os.killpg
        real_wait = runner_module.subprocess.Popen.wait
        real_kqueue = runner_module.select.kqueue

        class TrackedKqueue:
            def __init__(self):
                self.inner = real_kqueue()
                self.closed = False
                self.observer = False

            def control(self, *args, **kwargs):
                if args and args[0] and any(
                    event.filter == runner_module.select.KQ_FILTER_PROC
                    for event in args[0]
                ):
                    self.observer = True
                    events.append("observer-register")
                return self.inner.control(*args, **kwargs)

            def close(self):
                self.closed = True
                if self.observer:
                    events.append("observer-close")
                self.inner.close()

        def signal_group(process_group, signal_number):
            events.append("signal")
            return real_killpg(process_group, signal_number)

        def reap(process, *args, **kwargs):
            events.append("reap")
            return real_wait(process, *args, **kwargs)

        runner = self.runner()
        queues = []

        def make_queue():
            queue = TrackedKqueue()
            queues.append(queue)
            return queue

        with mock.patch.object(
            runner_module.os, "waitid", None, create=True
        ), mock.patch.object(
            runner_module.select, "kqueue", side_effect=make_queue
        ), mock.patch.object(
            runner_module.os, "killpg", side_effect=signal_group
        ), mock.patch.object(runner_module.subprocess.Popen, "wait", new=reap):
            result = self.run_python(runner, "print('fallback')")
        self.assertEqual(result.stdout, "fallback\n")
        observer_queues = [queue for queue in queues if queue.observer]
        self.assertEqual(len(observer_queues), 1)
        self.assertTrue(observer_queues[0].closed)
        self.assertLess(events.index("observer-register"), events.index("signal"))
        self.assertIn("signal", events)
        self.assertIn("reap", events)
        self.assertLess(events.index("signal"), events.index("reap"))
        self.assertGreater(events.index("observer-close"), events.index("reap"))

    def test_unsupported_nonreaping_observer_refuses_before_popen(self):
        runner = self.runner()
        with mock.patch.object(
            runner_module.os, "waitid", None, create=True
        ), mock.patch.object(
            runner_module.select, "kqueue", None, create=True
        ), mock.patch.object(runner_module.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(
                RunnerFailureError,
                "^runner non-reaping observation unavailable$",
            ):
                self.run_python(runner, "print('must not start')")
        popen.assert_not_called()

    def test_group_signal_precedes_reap_for_all_runner_outcomes(self):
        descendant_marker = os.path.join(self.directory, "ordered-descendant-marker")
        descendant = (
            "import pathlib,time; time.sleep(0.5); "
            "pathlib.Path({!r}).write_text('leaked')".format(descendant_marker)
        )
        descendant_program = (
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
            + repr(descendant)
            + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True)"
        )
        cases = (
            ("success", "print('ok')", 2, None),
            ("nonzero", "raise SystemExit(7)", 2, "runner command failed"),
            ("timeout", "import time; time.sleep(10)", 0.05, "runner timed out"),
            ("descendants", descendant_program, 2, None),
        )
        for label, program, timeout, expected_error in cases:
            with self.subTest(label=label):
                events = []
                real_killpg = runner_module.os.killpg
                real_wait = runner_module.subprocess.Popen.wait

                def signal_group(process_group, signal_number):
                    events.append("signal")
                    return real_killpg(process_group, signal_number)

                def reap(process, *args, **kwargs):
                    events.append("reap")
                    return real_wait(process, *args, **kwargs)

                runner = self.runner()
                with mock.patch.object(
                    runner_module.os, "killpg", side_effect=signal_group
                ), mock.patch.object(
                    runner_module.subprocess.Popen, "wait", new=reap
                ):
                    if expected_error is None:
                        self.run_python(runner, program, timeout=timeout)
                    else:
                        with self.assertRaisesRegex(
                            RunnerFailureError, "^" + expected_error + "$"
                        ):
                            self.run_python(runner, program, timeout=timeout)
                self.assertEqual(events.count("signal"), 1)
                self.assertEqual(events.count("reap"), 1)
                self.assertLess(events.index("signal"), events.index("reap"))
        time.sleep(0.6)
        self.assertFalse(os.path.exists(descendant_marker))

    def test_cached_group_is_never_signalled_after_leader_was_reaped(self):
        process = mock.Mock(pid=43210, returncode=0)
        cleanup = runner_module._CleanupState()
        with mock.patch.object(runner_module.os, "killpg") as killpg:
            SubprocessRunner._signal_group(process, process.pid, cleanup)
        killpg.assert_not_called()
        self.assertTrue(cleanup.identity_lost)
        self.assertTrue(cleanup.cleanup_uncertain)

    def test_executable_must_be_absolute_canonical_and_keep_its_identity(self):
        link = os.path.join(self.directory, "python-link")
        os.symlink(self.executable, link)
        with self.assertRaises(InvariantRefusalError):
            SubprocessRunner(link)
        runner = self.runner()
        metadata = os.stat(self.executable)
        os.utime(self.executable, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
        with self.assertRaisesRegex(
            InvariantRefusalError, "runner executable identity changed"
        ):
            self.run_python(runner, "print('not reached')")

    def test_bound_executable_cannot_be_replaced_in_argv(self):
        runner = self.runner()
        with self.assertRaises(InvariantRefusalError):
            runner.run(("/bin/echo", "unsafe"), timeout=1)

    def test_constructor_removes_private_root_on_setup_failure_or_interrupt(self):
        real_mkdtemp = runner_module.tempfile.mkdtemp

        for failure in (OSError("injected setup failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                created = []

                def tracked_mkdtemp(*args, **kwargs):
                    path = real_mkdtemp(*args, **kwargs)
                    created.append(os.path.realpath(path))
                    return path

                with mock.patch.object(
                    runner_module.tempfile, "mkdtemp", tracked_mkdtemp
                ), mock.patch.object(
                    SubprocessRunner, "_make_private_dir", side_effect=failure
                ):
                    with self.assertRaises(type(failure)):
                        SubprocessRunner(self.executable)

                self.assertEqual(len(created), 1)
                self.assertFalse(os.path.exists(created[0]))

    def test_close_can_retry_after_private_root_removal_failure(self):
        runner = self.runner()
        private_root = runner._private_root
        real_rmtree = runner_module.shutil.rmtree
        calls = {"count": 0}

        def fail_once(path, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("injected removal failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(runner_module.shutil, "rmtree", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected removal failure"):
                runner.close()
            self.assertFalse(runner._closed)
            self.assertTrue(os.path.isdir(private_root))
            runner.close()

        self.assertTrue(runner._closed)
        self.assertFalse(os.path.exists(private_root))

    def test_close_does_not_treat_missing_child_error_as_removed_root(self):
        runner = self.runner()
        private_root = runner._private_root

        with mock.patch.object(
            runner_module.shutil,
            "rmtree",
            side_effect=FileNotFoundError("injected child lookup failure"),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "child lookup failure"):
                runner.close()

        self.assertFalse(runner._closed)
        self.assertTrue(os.path.isdir(private_root))
        runner_module.shutil.rmtree(private_root)
        runner.close()
        self.assertTrue(runner._closed)


if __name__ == "__main__":
    unittest.main()
