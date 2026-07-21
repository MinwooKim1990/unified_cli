"""Offline-only command line interface for the synthetic extension-lab fixture.

The command layer deliberately constructs :class:`FakeRunner` directly.  It
never instantiates the subprocess Docker runner and accepts no command, URL,
provider, or credential input.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from .docker import DockerLabSpec
from .errors import CleanupIncompleteError, LabError, UsageStateError
from .fake_docker import FakeRunner
from .lifecycle import FixtureLifecycle
from .model import LabIdentity, validate_lab_id
from .state import LabState, LabStateStore, StatePhase


_PROVIDER_ID = "synthetic"
# This value is passed only to the pure command builder. FakeRunner consumes the
# resulting tuple in memory, so this is never executed or resolved as a binary.
_FAKE_DOCKER_EXECUTABLE = "/offline/fake-docker"
_TERMINAL_CLEAN_PHASES = frozenset((StatePhase.PASSED, StatePhase.FAILED_CLEAN))
_STABLE_FORWARD_PHASES = frozenset(
    (StatePhase.NEW, StatePhase.CREATED, StatePhase.INSTALLED, StatePhase.TESTED)
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unified-ext-lab",
        description="Run the source-only synthetic extension-lab fixture.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fixture = commands.add_parser("fixture-run", help="run the offline fixture lifecycle")
    fixture.add_argument("--lab-id", required=True)
    fixture.add_argument("--state-root", required=True)
    fixture.add_argument("--evidence-output", required=True)
    fixture.add_argument("--json", action="store_true", help="emit canonical JSON")

    recover = commands.add_parser(
        "fixture-recover", help="resume cleanup or seal reconciliation only"
    )
    recover.add_argument("--lab-id", required=True)
    recover.add_argument("--state-root", required=True)
    recover.add_argument("--evidence-output", required=True)
    recover.add_argument("--json", action="store_true", help="emit canonical JSON")

    status = commands.add_parser("status", help="show a redacted state summary")
    status.add_argument("--lab-id", required=True)
    status.add_argument("--state-root", required=True)
    status.add_argument("--json", action="store_true", help="emit canonical JSON")

    describe = commands.add_parser("describe", help="describe this offline fixture")
    describe.add_argument("--json", action="store_true", help="emit canonical JSON")
    return parser


def _canonical_path(value: object, field: str) -> Path:
    if type(value) is not str or not os.path.isabs(value):
        raise UsageStateError(field + " must be an absolute canonical path")
    normalized = os.path.normpath(value)
    canonical = os.path.realpath(value)
    if value != normalized or value != canonical:
        raise UsageStateError(field + " must be an absolute canonical path")
    return Path(value)


def _private_parent(path: Path, field: str) -> None:
    parent = path.parent
    try:
        info = parent.lstat()
    except OSError as error:
        raise UsageStateError(field + " parent must already be private 0700") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise UsageStateError(field + " parent must already be private 0700")


def _state_summary(state: LabState) -> dict:
    """Return the only state fields safe for command output."""

    return {
        "lab_id": state.lab_id,
        "provider_id": state.provider_id,
        "phase": state.phase.value,
        "revision": state.revision,
        "tainted": state.tainted,
    }


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if "description" in payload:
        print(payload["description"])
        return
    print(
        "lab={lab_id} provider={provider_id} phase={phase} revision={revision} "
        "tainted={tainted}".format(**payload)
    )


def _lab_error_name(error: LabError) -> str:
    return {
        2: "usage/state error",
        3: "unsupported operation",
        4: "safety invariant refused",
        5: "fixture runner failure",
        6: "fixture test failure",
        7: "cleanup incomplete",
    }.get(error.exit_code, "lab failure")


def _cleanup_and_seal(
    lifecycle: FixtureLifecycle, evidence_output: Path
) -> Optional[LabError]:
    """Attempt every cleanup stage, then seal whenever clean verification succeeds."""

    first_error: Optional[LabError] = None
    state = lifecycle.status()
    if state.phase in (StatePhase.PASSED, StatePhase.FAILED_CLEAN):
        return None
    if state.phase is StatePhase.SEAL_PENDING:
        if state.tainted:
            return CleanupIncompleteError(
                "fixture cleanup is held by permanent taint"
            )
        try:
            lifecycle.seal(evidence_output)
        except LabError as error:
            return error
        return None

    if state.phase is StatePhase.EVIDENCE_CAPTURED or (
        state.phase is StatePhase.RECOVERY_REQUIRED
        and state.pending_step in ("create", "install", "test", "evidence", "logout")
    ):
        try:
            lifecycle.logout()
        except LabError as error:
            # Logout is best-effort: destroy and verification remain mandatory.
            first_error = error
        state = lifecycle.status()

    if state.phase in (
        StatePhase.LOGOUT_DONE,
        StatePhase.LOGOUT_FAILED,
        StatePhase.DIRTY,
    ) or (
        state.phase is StatePhase.RECOVERY_REQUIRED
        and state.pending_step == "destroy"
    ):
        try:
            destroy_state, _destroy_summary = lifecycle.destroy()
            if destroy_state.phase is StatePhase.DESTROY_FAILED and first_error is None:
                first_error = CleanupIncompleteError(
                    "one or more fixture cleanup actions failed"
                )
        except LabError as error:
            if first_error is None:
                first_error = error
        state = lifecycle.status()

    if state.phase in (StatePhase.DESTROY_DONE, StatePhase.DESTROY_FAILED) or (
        state.phase is StatePhase.RECOVERY_REQUIRED
        and state.pending_step == "verify_clean"
    ):
        try:
            state, summary = lifecycle.verify_clean()
        except LabError:
            return CleanupIncompleteError(
                "fixture cleanup could not be verified"
            )
        if (
            state.phase is not StatePhase.CLEAN_VERIFIED
            or summary.remaining_count != 0
        ):
            return CleanupIncompleteError(
                "fixture cleanup could not be verified"
            )

    if state.phase is not StatePhase.CLEAN_VERIFIED:
        if (
            state.phase is StatePhase.DIRTY
            or state.tainted
            or first_error is not None
        ):
            return CleanupIncompleteError(
                "fixture cleanup could not be verified"
            )
        return UsageStateError("state is not eligible for cleanup-only recovery")
    if state.tainted:
        return CleanupIncompleteError(
            "fixture cleanup is held by permanent taint"
        )
    try:
        lifecycle.seal(evidence_output)
    except LabError as error:
        return first_error or error
    return first_error


def _stabilize_interruption(
    lifecycle: FixtureLifecycle, store: LabStateStore
) -> None:
    """Best-effort conversion of pending or stable forward state to cleanup-only."""

    # Preserve the fixture CLI's historical best-effort status probe. Besides
    # normalizing a pending intent, this consumes a nested interruption without
    # allowing it to suppress the subsequent cleanup attempt.
    try:
        lifecycle.status()
    except BaseException:
        pass
    try:
        with store.locked(lifecycle.spec.identity.lab_id) as locked:
            state = locked.load()
            if state.phase in _STABLE_FORWARD_PHASES:
                locked.interrupt_stable_forward(state.phase)
    except BaseException:
        pass


def _final_state_after_cleanup(
    lifecycle: FixtureLifecycle, uncertain_exit: Optional[int]
) -> Tuple[Optional[LabState], Optional[int]]:
    """Read final state, converting any cleanup-status uncertainty to exit 7."""

    try:
        state = lifecycle.status()
    except KeyboardInterrupt:
        return None, CleanupIncompleteError.exit_code
    except Exception:
        return None, CleanupIncompleteError.exit_code
    if uncertain_exit is None:
        return state, None
    if state.phase not in _TERMINAL_CLEAN_PHASES:
        return state, CleanupIncompleteError.exit_code
    return state, uncertain_exit


def _fixture_run(
    arguments: argparse.Namespace, runner_factory: Callable[[DockerLabSpec], object]
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    evidence_output = _canonical_path(arguments.evidence_output, "evidence output")
    _private_parent(state_root, "state root")
    _private_parent(evidence_output, "evidence output")
    if evidence_output.exists() or evidence_output.is_symlink():
        raise UsageStateError("evidence output already exists")
    # Refuse an existing run before constructing any lifecycle object.
    if (state_root / lab_id / "state.json").exists():
        raise UsageStateError("lab state already exists")

    identity = LabIdentity(lab_id, _PROVIDER_ID, uuid.uuid4().hex)
    spec = DockerLabSpec.from_locks(
        identity, docker_executable=_FAKE_DOCKER_EXECUTABLE
    )
    store = LabStateStore(state_root)
    lifecycle = FixtureLifecycle(store, spec, runner_factory(spec))
    forward_error: Optional[LabError] = None
    uncertain_exit: Optional[int] = None
    try:
        lifecycle.create()
        lifecycle.install()
        lifecycle.test()
        lifecycle.evidence()
    except LabError as error:
        forward_error = error
    except KeyboardInterrupt:
        uncertain_exit = 130
        _stabilize_interruption(lifecycle, store)
    except Exception:
        uncertain_exit = 1
        _stabilize_interruption(lifecycle, store)

    try:
        cleanup_error = _cleanup_and_seal(lifecycle, evidence_output)
    except KeyboardInterrupt:
        uncertain_exit = 130
        _stabilize_interruption(lifecycle, store)
        cleanup_error = CleanupIncompleteError("fixture cleanup was interrupted")
    except Exception:
        uncertain_exit = uncertain_exit or 1
        _stabilize_interruption(lifecycle, store)
        cleanup_error = CleanupIncompleteError("fixture cleanup was interrupted")
    if cleanup_error is not None and (
        forward_error is None or isinstance(cleanup_error, CleanupIncompleteError)
    ):
        # Residual resources are the final actionable outcome even when the
        # forward fixture operation also failed.
        forward_error = cleanup_error

    state, final_exit = _final_state_after_cleanup(lifecycle, uncertain_exit)
    if state is not None:
        payload = _state_summary(state)
        if state.phase is StatePhase.PASSED:
            payload["result"] = "passed"
        elif state.phase is StatePhase.FAILED_CLEAN:
            payload["result"] = "failed_clean"
        _emit(payload, as_json=arguments.json)
    if final_exit is not None:
        if final_exit == CleanupIncompleteError.exit_code:
            print("error: cleanup incomplete", file=sys.stderr)
        else:
            print("error: interrupted", file=sys.stderr)
        return final_exit
    if forward_error is not None:
        print("error: " + _lab_error_name(forward_error), file=sys.stderr)
        return forward_error.exit_code
    return 0


def _fixture_recover(
    arguments: argparse.Namespace, runner_factory: Callable[[DockerLabSpec], object]
) -> int:
    """Perform offline cleanup/seal work using only private persisted identity."""

    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    evidence_output = _canonical_path(arguments.evidence_output, "evidence output")
    _private_parent(state_root, "state root")
    _private_parent(evidence_output, "evidence output")
    state_path = state_root / lab_id / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise UsageStateError("lab state does not exist")
    store = LabStateStore(state_root)
    with store.locked(lab_id) as locked:
        state = locked.load()
        if state.phase in _STABLE_FORWARD_PHASES:
            state = locked.interrupt_stable_forward(state.phase)
    if state.provider_id != _PROVIDER_ID:
        raise UsageStateError("fixture recovery requires synthetic private state")
    identity = LabIdentity(state.lab_id, state.provider_id, state.ownership_token)
    spec = DockerLabSpec.from_locks(
        identity, docker_executable=_FAKE_DOCKER_EXECUTABLE
    )
    lifecycle = FixtureLifecycle(store, spec, runner_factory(spec))
    try:
        recovery_error = _cleanup_and_seal(lifecycle, evidence_output)
    except KeyboardInterrupt:
        _stabilize_interruption(lifecycle, store)
        recovery_error = None
        uncertain_exit = 130
    except Exception:
        _stabilize_interruption(lifecycle, store)
        recovery_error = None
        uncertain_exit = 1
    else:
        uncertain_exit = None

    final_state, final_exit = _final_state_after_cleanup(
        lifecycle, uncertain_exit
    )
    if final_state is not None:
        payload = _state_summary(final_state)
        if final_state.phase is StatePhase.PASSED:
            payload["result"] = "passed"
        elif final_state.phase is StatePhase.FAILED_CLEAN:
            payload["result"] = "failed_clean"
        _emit(payload, as_json=arguments.json)
    if final_exit is not None:
        if final_exit == CleanupIncompleteError.exit_code:
            print("error: cleanup incomplete", file=sys.stderr)
        else:
            print("error: interrupted", file=sys.stderr)
        return final_exit
    if recovery_error is not None:
        print("error: " + _lab_error_name(recovery_error), file=sys.stderr)
        return recovery_error.exit_code
    return 0


def _status(arguments: argparse.Namespace) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    _private_parent(state_root, "state root")
    if not (state_root / lab_id / "state.json").is_file():
        raise UsageStateError("lab state does not exist")
    store = LabStateStore(state_root)
    with store.locked(lab_id) as locked:
        state = locked.load()
    _emit(_state_summary(state), as_json=arguments.json)
    return 0


def _describe(arguments: argparse.Namespace) -> int:
    _emit(
        {
            "description": "offline synthetic fixture only; no Docker, network, provider, auth, browser, or shell",
            "provider_id": _PROVIDER_ID,
        },
        as_json=arguments.json,
    )
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runner_factory: Callable[[DockerLabSpec], object] = FakeRunner,
) -> int:
    """Run the fixture-only CLI and return a stable process exit code.

    ``runner_factory`` is dependency injection for offline tests; the public
    command-line entry point always uses the in-memory :class:`FakeRunner`.
    """

    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "fixture-run":
            return _fixture_run(arguments, runner_factory)
        if arguments.command == "fixture-recover":
            return _fixture_recover(arguments, runner_factory)
        if arguments.command == "status":
            return _status(arguments)
        if arguments.command == "describe":
            return _describe(arguments)
        raise UsageStateError("unsupported command")
    except LabError as error:
        print("error: " + _lab_error_name(error), file=sys.stderr)
        return error.exit_code
    except SystemExit as error:
        return int(error.code)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("error: internal error", file=sys.stderr)
        return 1


__all__ = ["main"]
