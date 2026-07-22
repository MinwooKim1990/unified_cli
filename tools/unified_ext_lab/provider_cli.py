"""Source-only Stage-6C accountless-provider lab command layer."""

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
from .model import LabIdentity, ResourceRole, validate_lab_id
from .provider_lifecycle import ProviderLifecycle, profile_artifact
from .provider_profiles import PROVIDER_IDS, get_provider_profile
from .provider_runtime import ProviderDockerRuntime
from .state import (
    PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE,
    LabState,
    LabStateStore,
    PlannedResource,
    StatePhase,
)


_SNAPSHOT_NAME = "runtime-snapshot"
_STABLE_FORWARD_PHASES = frozenset(
    (StatePhase.NEW, StatePhase.CREATED, StatePhase.INSTALLED, StatePhase.TESTED)
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        raise UsageStateError("invalid provider lab arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="unified-ext-provider-lab",
        description="held accountless-provider validation foundation",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "create one provider-scoped isolated container"),
        ("test", "run fixed accountless identity probes"),
        ("evidence", "capture a non-promotional accountless evidence draft"),
        ("logout", "record accountless local logout without vendor auth mutation"),
        ("destroy", "remove only the exact provider-scoped container"),
    ):
        command = commands.add_parser(name, help=help_text, allow_abbrev=False)
        _add_common(command)
    install = commands.add_parser(
        "install", help="perform a fully locked provider acquisition", allow_abbrev=False
    )
    _add_common(install)
    install.add_argument("--allow-network", action="store_true")
    install.add_argument("--allow-install", action="store_true")
    verify = commands.add_parser(
        "verify-clean",
        help="verify exact cleanup and seal accountless evidence",
        allow_abbrev=False,
    )
    _add_common(verify)
    verify.add_argument("--evidence-output", required=True)
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", required=True, choices=PROVIDER_IDS)
    parser.add_argument("--lab-id", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--json", action="store_true")


def _canonical_path(value: object, field: str) -> Path:
    if type(value) is not str or not os.path.isabs(value):
        raise UsageStateError(field + " must be an absolute canonical path")
    if value != os.path.normpath(value) or value != os.path.realpath(value):
        raise UsageStateError(field + " must be an absolute canonical path")
    return Path(value)


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


def _private_parent(path: Path, field: str) -> None:
    _validate_private_directory(path.parent, field + " parent")


def _namespace(state_root: Path, *, create: bool) -> Path:
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
    return state_root / PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE


def _snapshot_path(namespace: Path, lab_id: str) -> Path:
    lab = validate_lab_id(lab_id)
    path = namespace / lab / _SNAPSHOT_NAME
    if (
        not namespace.is_absolute()
        or os.path.realpath(str(namespace)) != str(namespace)
        or os.path.normpath(str(path)) != str(path)
        or os.path.realpath(str(path)) != str(path)
    ):
        raise UsageStateError("provider runtime snapshot path is not canonical")
    return path


def _state_path(state_root: Path, lab_id: str) -> Path:
    return (
        state_root
        / PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE
        / validate_lab_id(lab_id)
        / "state.json"
    )


def _summary(state: LabState) -> dict:
    return {
        "accountless_only": True,
        "lab_id": state.lab_id,
        "phase": state.phase.value,
        "promotion_eligible": False,
        "provider_id": state.provider_id,
        "revision": state.revision,
        "tainted": state.tainted,
    }


def _emit(state: LabState, *, as_json: bool) -> None:
    payload = _summary(state)
    if state.phase is StatePhase.PASSED:
        payload["result"] = "passed"
    elif state.phase is StatePhase.FAILED_CLEAN:
        payload["result"] = "failed_clean"
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(
        "lab={lab_id} provider={provider_id} phase={phase} revision={revision} "
        "accountless_only=true promotion_eligible=false tainted={tainted}".format(
            **payload
        )
    )


def _close_runtime(runtime: object, primary: Optional[BaseException]) -> None:
    try:
        runtime.close()
    except BaseException:
        if primary is None:
            raise


def _load(
    state_root: Path,
    lab_id: str,
    provider_id: str,
    *,
    require_current_profile: bool = True,
) -> Tuple[LabStateStore, LabState]:
    if type(require_current_profile) is not bool:
        raise UsageStateError("invalid provider profile binding policy")
    namespace = _namespace(state_root, create=False)
    path = namespace / lab_id / "state.json"
    if not path.is_file() or path.is_symlink():
        raise UsageStateError("provider lab state does not exist")
    store = LabStateStore(namespace, PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE)
    with store.locked(lab_id) as locked:
        state = locked.load()
    if state.provider_id != provider_id:
        raise UsageStateError("provider lab state identity mismatch")
    if require_current_profile:
        profile = get_provider_profile(provider_id)
        if dict(state.artifact_evidence) != profile_artifact(profile).to_dict():
            raise UsageStateError("provider profile identity changed")
    return store, state


def _make_forward_lifecycle(
    runtime: object,
    store: LabStateStore,
    state: LabState,
    profile: object,
) -> ProviderLifecycle:
    identity = LabIdentity(state.lab_id, state.provider_id, state.ownership_token)
    spec = runtime.spec(identity)
    return ProviderLifecycle(
        store,
        spec,
        runtime.runner,
        profile,
        command_builder=runtime.commands(spec),
        runtime_snapshot=runtime.snapshot_resource,
    )


def _make_cleanup_lifecycle(
    runtime: object,
    store: LabStateStore,
    state: LabState,
) -> ProviderLifecycle:
    identity = LabIdentity(state.lab_id, state.provider_id, state.ownership_token)
    spec = runtime.cleanup_spec(identity, state.planned_resources)
    return ProviderLifecycle(
        store,
        spec,
        runtime.runner,
        None,
        command_builder=runtime.commands(spec),
        runtime_snapshot=runtime.snapshot_resource,
    )


def _create(
    arguments: argparse.Namespace,
    runtime_factory: Callable[[object], object],
    token_factory: Callable[[], str],
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    profile = get_provider_profile(arguments.provider)
    state_root = _canonical_path(arguments.state_root, "state root")
    _private_parent(state_root, "state root")
    if _state_path(state_root, lab_id).exists():
        raise UsageStateError("provider lab state already exists")
    runtime = runtime_factory(profile)
    primary = None
    try:
        runtime.preflight()
        namespace = _namespace(state_root, create=True)
        if _state_path(state_root, lab_id).exists():
            raise UsageStateError("provider lab state already exists")
        identity = LabIdentity(lab_id, profile.provider_id, token_factory())
        store = LabStateStore(namespace, PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE)
        with store.locked(lab_id) as locked:
            locked.create_initial(
                identity.provider_id,
                identity.ownership_token,
                (
                    PlannedResource.from_value(
                        identity.resource(ResourceRole.CONTAINER)
                    ),
                ),
                {"runtime_snapshot_bound": False},
                artifact_evidence=profile_artifact(profile).to_dict(),
            )
        runtime.capture_snapshot(str(_snapshot_path(namespace, lab_id)))
        store, state = _load(state_root, lab_id, profile.provider_id)
        lifecycle = _make_forward_lifecycle(runtime, store, state, profile)
        lifecycle.bind_runtime_snapshot_intent()
        state = lifecycle.create()
        _emit(state, as_json=arguments.json)
        return 0
    except BaseException as error:
        primary = error
        raise
    finally:
        _close_runtime(runtime, primary)


def _forward_action(
    arguments: argparse.Namespace,
    runtime_factory: Callable[[object], object],
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    profile = get_provider_profile(arguments.provider)
    state_root = _canonical_path(arguments.state_root, "state root")
    _private_parent(state_root, "state root")
    store, state = _load(state_root, lab_id, profile.provider_id)
    if arguments.command == "install":
        if (
            arguments.allow_network is not True
            or arguments.allow_install is not True
        ):
            raise UsageStateError(
                "provider install requires --allow-network and --allow-install"
            )
        # Acquisition policy is independent of Docker health.  Reject a held
        # built-in profile before discovering the client, probing the daemon,
        # or constructing any network-capable runtime.  The lifecycle repeats
        # both checks as a defense-in-depth boundary for non-CLI callers.
        profile.require_install_ready()
    runtime = runtime_factory(profile)
    primary = None
    try:
        runtime.preflight()
        runtime.bind_existing_snapshot(
            str(_snapshot_path(store.root, lab_id))
        )
        lifecycle = _make_forward_lifecycle(runtime, store, state, profile)
        if arguments.command == "install":
            state = lifecycle.install(
                allow_network=arguments.allow_network,
                allow_install=arguments.allow_install,
            )
        elif arguments.command == "test":
            state = lifecycle.test()
        elif arguments.command == "evidence":
            state = lifecycle.evidence()
        else:
            raise UsageStateError("unsupported provider forward action")
        _emit(state, as_json=arguments.json)
        return 0
    except BaseException as error:
        primary = error
        raise
    finally:
        _close_runtime(runtime, primary)


def _cleanup_action(
    arguments: argparse.Namespace,
    cleanup_runtime_factory: Callable[[], object],
) -> int:
    lab_id = validate_lab_id(arguments.lab_id)
    provider_id = arguments.provider
    state_root = _canonical_path(arguments.state_root, "state root")
    _private_parent(state_root, "state root")
    store, state = _load(
        state_root,
        lab_id,
        provider_id,
        require_current_profile=False,
    )
    runtime = cleanup_runtime_factory()
    primary = None
    try:
        runtime.probe_daemon()
        if arguments.command == "logout" and state.phase in _STABLE_FORWARD_PHASES:
            with store.locked(lab_id) as locked:
                current = locked.load()
                if current.phase is not state.phase:
                    raise UsageStateError("provider lab state changed")
                state = locked.interrupt_stable_forward(current.phase)
        runtime.bind_snapshot_for_cleanup(
            str(_snapshot_path(store.root, lab_id))
        )
        lifecycle = _make_cleanup_lifecycle(runtime, store, state)
        if arguments.command == "logout":
            state = lifecycle.logout()
        elif arguments.command == "destroy":
            state, _summary_value = lifecycle.destroy()
        elif arguments.command == "verify-clean":
            evidence_output = _canonical_path(
                arguments.evidence_output, "evidence output"
            )
            _private_parent(evidence_output, "evidence output")
            if (
                state.phase is not StatePhase.SEAL_PENDING
                and (evidence_output.exists() or evidence_output.is_symlink())
            ):
                raise UsageStateError("evidence output already exists")
            if state.phase not in (
                StatePhase.CLEAN_VERIFIED,
                StatePhase.SEAL_PENDING,
            ):
                state, clean = lifecycle.verify_clean()
                if (
                    state.phase is not StatePhase.CLEAN_VERIFIED
                    or clean.remaining_count != 0
                ):
                    raise CleanupIncompleteError(
                        "provider cleanup could not be verified"
                    )
            state = lifecycle.seal(evidence_output)
        else:
            raise UsageStateError("unsupported provider cleanup action")
        _emit(state, as_json=arguments.json)
        return 0
    except BaseException as error:
        primary = error
        raise
    finally:
        _close_runtime(runtime, primary)


def _error_name(error: LabError) -> str:
    return {
        2: "usage/state error",
        3: "unsupported operation",
        4: "safety invariant refused",
        5: "isolated runner failure",
        6: "accountless probe failure",
        7: "cleanup incomplete",
    }.get(error.exit_code, "provider lab failure")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime_factory: Callable[[object], object] = ProviderDockerRuntime.discover,
    cleanup_runtime_factory: Callable[[], object] = RealDockerRuntime.discover_cleanup,
    token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "create":
            return _create(arguments, runtime_factory, token_factory)
        if arguments.command in ("install", "test", "evidence"):
            return _forward_action(arguments, runtime_factory)
        if arguments.command in ("logout", "destroy", "verify-clean"):
            return _cleanup_action(arguments, cleanup_runtime_factory)
        raise UsageStateError("unsupported provider lab command")
    except LabError as error:
        print("error: " + _error_name(error), file=sys.stderr)
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
