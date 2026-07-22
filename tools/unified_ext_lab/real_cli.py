"""Opt-in command line interface for the profile-locked real-Docker lab.

This module intentionally exposes a separate entry point from the offline
fixture CLI.  The grammar contains no caller-controlled Docker endpoint,
executable, platform, image, provider, URL, command, credential, timeout, or
shell input.
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

from .docker_runtime import RealDockerRuntime
from .errors import CleanupIncompleteError, LabError, UsageStateError
from .lifecycle import FixtureLifecycle
from .model import LabIdentity, ResourceRole, validate_lab_id
from .state import (
    REAL_DOCKER_EXECUTION_PROFILE,
    LabState,
    LabStateStore,
    PlannedResource,
    StatePhase,
)


_PROVIDER_ID = "synthetic"
_RUNTIME_SNAPSHOT_NAME = "runtime-snapshot"
_TERMINAL_CLEAN_PHASES = frozenset((StatePhase.PASSED, StatePhase.FAILED_CLEAN))
_STABLE_FORWARD_PHASES = frozenset(
    (StatePhase.NEW, StatePhase.CREATED, StatePhase.INSTALLED, StatePhase.TESTED)
)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Refuse invalid input without reflecting caller-controlled values."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Apply this to both the root parser and every parser created by the
        # subparser action. Runtime/network opt-ins must always be exact.
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise UsageStateError("invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="unified-ext-lab-real-docker",
        description="Run the opt-in, profile-locked real-Docker extension lab.",
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )

    prepare = commands.add_parser(
        "prepare-base", help="explicitly pull and verify the locked base image"
    )
    prepare.add_argument("--allow-network", action="store_true", required=True)
    prepare.add_argument("--json", action="store_true", help="emit canonical JSON")

    run = commands.add_parser(
        "conformance-run", help="run the real-Docker conformance lifecycle"
    )
    _add_lab_arguments(run, evidence=True)

    recover = commands.add_parser(
        "conformance-recover", help="resume cleanup or seal reconciliation only"
    )
    _add_lab_arguments(recover, evidence=True)

    status = commands.add_parser(
        "conformance-status", help="show a redacted real-Docker state summary"
    )
    _add_lab_arguments(status, evidence=False)
    return parser


def _add_lab_arguments(parser: argparse.ArgumentParser, *, evidence: bool) -> None:
    parser.add_argument("--lab-id", required=True)
    parser.add_argument("--state-root", required=True)
    if evidence:
        parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")


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


def _validate_private_directory(path: Path, field: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise UsageStateError(field + " does not exist") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise UsageStateError(field + " must be private 0700")


def _state_namespace(state_root: Path, *, create: bool) -> Path:
    """Return the fixed internal namespace, creating only its outer parent."""

    if not state_root.exists() and not state_root.is_symlink():
        if not create:
            raise UsageStateError("state root does not exist")
        try:
            state_root.mkdir(mode=0o700)
            state_root.chmod(0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise UsageStateError("state root could not be created") from error
    _validate_private_directory(state_root, "state root")
    return state_root / REAL_DOCKER_EXECUTION_PROFILE


def _state_path(state_root: Path, lab_id: str) -> Path:
    return state_root / REAL_DOCKER_EXECUTION_PROFILE / lab_id / "state.json"


def _runtime_snapshot_path(namespace: Path, lab_id: str) -> Path:
    lab = validate_lab_id(lab_id)
    path = namespace / lab / _RUNTIME_SNAPSHOT_NAME
    if (
        not namespace.is_absolute()
        or os.path.realpath(str(namespace)) != str(namespace)
        or os.path.normpath(str(path)) != str(path)
        or os.path.realpath(str(path)) != str(path)
    ):
        raise UsageStateError("runtime snapshot path is not canonical")
    return path


def _state_summary(state: LabState) -> dict:
    """Project the only state fields allowed in command output."""

    return {
        "lab_id": state.lab_id,
        "provider_id": state.provider_id,
        "phase": state.phase.value,
        "revision": state.revision,
        "tainted": state.tainted,
    }


def _emit_state(
    state: LabState, *, as_json: bool, include_result: bool = True
) -> None:
    payload = _state_summary(state)
    if include_result and state.phase is StatePhase.PASSED:
        payload["result"] = "passed"
    elif include_result and state.phase is StatePhase.FAILED_CLEAN:
        payload["result"] = "failed_clean"
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
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
    """Attempt all eligible cleanup stages and seal only verified-clean state."""

    first_error: Optional[LabError] = None
    state = lifecycle.status()
    if state.phase in _TERMINAL_CLEAN_PHASES:
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
            destroy_state, _summary = lifecycle.destroy()
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
    try:
        state = lifecycle.status()
    except BaseException:
        return None, CleanupIncompleteError.exit_code
    if uncertain_exit is None:
        return state, None
    if state.phase not in _TERMINAL_CLEAN_PHASES:
        return state, CleanupIncompleteError.exit_code
    return state, uncertain_exit


def _lifecycle(runtime: object, store: LabStateStore, identity: LabIdentity) -> FixtureLifecycle:
    spec = runtime.spec(identity)
    return FixtureLifecycle(
        store,
        spec,
        runtime.runner,
        execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
        executor_kind="real_docker",
        command_builder=runtime.commands(spec),
        runtime_snapshot=runtime.snapshot_resource,
    )


def _cleanup_lifecycle(
    runtime: object, store: LabStateStore, state: LabState
) -> FixtureLifecycle:
    identity = LabIdentity(
        state.lab_id, state.provider_id, state.ownership_token
    )
    runtime.bind_snapshot_for_cleanup(
        str(_runtime_snapshot_path(store.root, state.lab_id))
    )
    spec = runtime.cleanup_spec(identity, state.planned_resources)
    return FixtureLifecycle(
        store,
        spec,
        runtime.runner,
        execution_profile=REAL_DOCKER_EXECUTION_PROFILE,
        executor_kind="real_docker",
        command_builder=runtime.commands(spec),
        runtime_snapshot=runtime.snapshot_resource,
    )


def _new_ownership_token() -> str:
    return uuid.uuid4().hex


def _close_runtime(
    runtime: object,
    *,
    primary_error: Optional[BaseException],
    primary_exit: Optional[int],
) -> None:
    """Close once without replacing an already-established failure result."""

    try:
        runtime.close()
    except BaseException:
        if primary_error is None and primary_exit in (None, 0):
            raise


def _prepare_base(arguments: argparse.Namespace, runtime_factory: Callable[[], object]) -> int:
    runtime = runtime_factory()
    primary_error: Optional[BaseException] = None
    try:
        runtime.prepare_base(allow_network=True)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_runtime(runtime, primary_error=primary_error, primary_exit=None)
    payload = {"result": "prepared"}
    if arguments.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print("locked base prepared")
    return 0


def _conformance_run(
    arguments: argparse.Namespace,
    runtime_factory: Callable[[], object],
    token_factory: Callable[[], str],
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    evidence_output = _canonical_path(arguments.evidence_output, "evidence output")
    _private_parent(state_root, "state root")
    _private_parent(evidence_output, "evidence output")
    if evidence_output.exists() or evidence_output.is_symlink():
        raise UsageStateError("evidence output already exists")
    if _state_path(state_root, lab_id).exists():
        raise UsageStateError("lab state already exists")

    runtime = runtime_factory()
    primary_error: Optional[BaseException] = None
    primary_exit: Optional[int] = None
    try:
        # Probe Docker before persistence, but establish the exact NEW intent
        # before creating the state-derived execution snapshot. A SIGKILL can
        # therefore never leave an unowned random /tmp snapshot behind.
        runtime.preflight()
        namespace = _state_namespace(state_root, create=True)
        if _state_path(state_root, lab_id).exists():
            raise UsageStateError("lab state already exists")
        identity = LabIdentity(lab_id, _PROVIDER_ID, token_factory())
        lifecycle_store = LabStateStore(
            namespace, REAL_DOCKER_EXECUTION_PROFILE
        )
        with lifecycle_store.locked(lab_id) as locked:
            locked.create_initial(
                identity.provider_id,
                identity.ownership_token,
                (PlannedResource.from_value(identity.resource(ResourceRole.CONTAINER)),),
                {"runtime_snapshot_bound": False},
            )
        runtime.capture_snapshot(
            str(_runtime_snapshot_path(namespace, lab_id))
        )
        lifecycle = _lifecycle(
            runtime,
            lifecycle_store,
            identity,
        )
        lifecycle.bind_runtime_snapshot_intent()
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
            _stabilize_interruption(lifecycle, lifecycle_store)
        except Exception:
            uncertain_exit = 1
            _stabilize_interruption(lifecycle, lifecycle_store)

        try:
            cleanup_error = _cleanup_and_seal(lifecycle, evidence_output)
        except KeyboardInterrupt:
            uncertain_exit = 130
            _stabilize_interruption(lifecycle, lifecycle_store)
            cleanup_error = CleanupIncompleteError("fixture cleanup was interrupted")
        except Exception:
            uncertain_exit = uncertain_exit or 1
            _stabilize_interruption(lifecycle, lifecycle_store)
            cleanup_error = CleanupIncompleteError("fixture cleanup was interrupted")
        if cleanup_error is not None and (
            forward_error is None or isinstance(cleanup_error, CleanupIncompleteError)
        ):
            forward_error = cleanup_error

        state, final_exit = _final_state_after_cleanup(lifecycle, uncertain_exit)
        if state is not None:
            _emit_state(state, as_json=arguments.json)
        if final_exit is not None:
            if final_exit == CleanupIncompleteError.exit_code:
                print("error: cleanup incomplete", file=sys.stderr)
            else:
                print("error: interrupted", file=sys.stderr)
            primary_exit = final_exit
        elif forward_error is not None:
            print("error: " + _lab_error_name(forward_error), file=sys.stderr)
            primary_exit = forward_error.exit_code
        else:
            primary_exit = 0
        return primary_exit
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_runtime(
            runtime,
            primary_error=primary_error,
            primary_exit=primary_exit,
        )


def _load_existing_state(
    state_root: Path, lab_id: str, *, interrupt_stable: bool = False
) -> Tuple[LabStateStore, LabState]:
    namespace = _state_namespace(state_root, create=False)
    state_path = namespace / lab_id / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise UsageStateError("lab state does not exist")
    store = LabStateStore(namespace, REAL_DOCKER_EXECUTION_PROFILE)
    with store.locked(lab_id) as locked:
        state = locked.load()
        if interrupt_stable and state.phase in _STABLE_FORWARD_PHASES:
            state = locked.interrupt_stable_forward(state.phase)
    if state.provider_id != _PROVIDER_ID:
        raise UsageStateError("real-Docker command requires synthetic private state")
    return store, state


def _conformance_recover(
    arguments: argparse.Namespace,
    cleanup_runtime_factory: Callable[[], object],
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    evidence_output = _canonical_path(arguments.evidence_output, "evidence output")
    _private_parent(state_root, "state root")
    _private_parent(evidence_output, "evidence output")
    namespace = _state_namespace(state_root, create=False)
    state_path = namespace / lab_id / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise UsageStateError("lab state does not exist")

    runtime = cleanup_runtime_factory()
    primary_error: Optional[BaseException] = None
    primary_exit: Optional[int] = None
    try:
        # Recovery deliberately checks only daemon reachability.  It never
        # inspects or pulls the base and never resumes forward lifecycle work.
        # Probe before locked.load(), which may durably normalize an interrupted
        # pending phase; an unavailable daemon must leave persisted bytes alone.
        runtime.probe_daemon()
        store, state = _load_existing_state(
            state_root, lab_id, interrupt_stable=True
        )
        lifecycle = _cleanup_lifecycle(runtime, store, state)
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

        final_state, final_exit = _final_state_after_cleanup(lifecycle, uncertain_exit)
        if final_state is not None:
            _emit_state(final_state, as_json=arguments.json)
        if final_exit is not None:
            if final_exit == CleanupIncompleteError.exit_code:
                print("error: cleanup incomplete", file=sys.stderr)
            else:
                print("error: interrupted", file=sys.stderr)
            primary_exit = final_exit
        elif recovery_error is not None:
            print("error: " + _lab_error_name(recovery_error), file=sys.stderr)
            primary_exit = recovery_error.exit_code
        else:
            primary_exit = 0
        return primary_exit
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_runtime(
            runtime,
            primary_error=primary_error,
            primary_exit=primary_exit,
        )


def _conformance_status(arguments: argparse.Namespace) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    state_root = _canonical_path(arguments.state_root, "state root")
    _private_parent(state_root, "state root")
    _store, state = _load_existing_state(state_root, lab_id)
    _emit_state(state, as_json=arguments.json, include_result=False)
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime_factory: Callable[[], object] = RealDockerRuntime.discover,
    cleanup_runtime_factory: Callable[[], object] = RealDockerRuntime.discover_cleanup,
    token_factory: Callable[[], str] = _new_ownership_token,
) -> int:
    """Run the real-Docker CLI with fixed defaults and injectable test hooks."""

    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "prepare-base":
            return _prepare_base(arguments, runtime_factory)
        if arguments.command == "conformance-run":
            return _conformance_run(arguments, runtime_factory, token_factory)
        if arguments.command == "conformance-recover":
            return _conformance_recover(arguments, cleanup_runtime_factory)
        if arguments.command == "conformance-status":
            return _conformance_status(arguments)
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
