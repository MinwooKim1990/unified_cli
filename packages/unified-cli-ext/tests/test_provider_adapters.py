import asyncio
import builtins
import concurrent.futures
import errno
import json
import os
import pty
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from unified_cli_ext import (
    CancellationToken,
    ConfigurationError,
    JsonRpcProcessClient,
    JsonlProcess,
    LimitExceeded,
    PermissionPolicy,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
    TransportLimits,
)
from unified_cli_ext.providers import (
    PROVIDER_ADAPTER_ABI_V1,
    AdapterServerPolicy,
    AdapterStatus,
    AdapterInspectionV1,
    AuthSpec,
    BinarySpec,
    DoctorProbeSpec,
    DynamicArgument,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    InteractiveAuthSessionV1,
    JsonProbeSpec,
    ModelProbeSpec,
    OperationLimits,
    OpenedProcessTransportV1,
    PlainTextFieldSpec,
    PlainTextProbeSpec,
    PromptCommandSpec,
    PromptMode,
    PromptSentinelPolicy,
    ProbeFormat,
    ProtocolLaunchBoundaryV1,
    ProviderAdapterRegistryV1,
    ProviderAdapterSpecV1,
    ProviderAdapterV1,
    ProviderCapability,
    TransportKind,
    TransportConfig,
    VersionProbeSpec,
    drain_pending_cleanups,
)
from unified_cli_ext.transports import run_fixed_process
from unified_cli_ext.transports.security import (
    ExecutableIdentity,
    IsolatedEnvironment,
    _OwnedTemporaryDirectory,
    redact_diagnostics,
)


@pytest.fixture
def adapter_binary(tmp_path):
    source = Path(__file__).parent / "fixtures" / "providers" / "fake_adapter_cli.py"
    interpreter = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    target = tmp_path / "fake-adapter"
    source_text = source.read_text(encoding="utf-8")
    _, separator, body = source_text.partition("\n")
    assert separator
    target.write_text(
        "#!{}\n{}".format(interpreter, body),
        encoding="utf-8",
    )
    target.chmod(0o700)
    return str(target)


def command(*argv, timeout=3.0):
    return FixedCommandSpec(
        argv,
        OperationLimits(
            timeout_seconds=timeout,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=16 * 1024,
            max_events=2,
        ),
    )


def adapter_spec(executable="fake-adapter", **changes):
    prompt = PromptCommandSpec(
        ("chat", "--jsonl"),
        dynamic_arguments=(
            DynamicArgument("model", "--model", required=True),
            DynamicArgument("session", "--session"),
        ),
        mode=PromptMode.ARGV,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    version_argv = changes.pop("version_argv", ("--version-json",))
    feature_argv = changes.pop("feature_argv", ("--features-json",))
    spec = ProviderAdapterSpecV1(
        id="fixture-provider",
        display_name="Fixture Provider",
        status=AdapterStatus.PREVIEW,
        binary=BinarySpec(
            executable=executable,
            expected_identity="fixture-provider",
            version_probe=VersionProbeSpec(command(*version_argv), minimum_version=(2, 1)),
            feature_probe=FeatureProbeSpec(
                command(*feature_argv),
                required_features=frozenset(("auth", "chat", "models", "sessions")),
            ),
        ),
        prompt=prompt,
        transport=TransportKind.JSONL,
        environment=EnvironmentPolicy(
            allowed_keys=frozenset(("FIXTURE_AUTH",)),
        ),
        auth=AuthSpec(
            status_probe=JsonProbeSpec(
                command("--auth-json"),
                expected={"provider": "fixture-provider"},
            ),
            login_command=command("auth", "login"),
            logout_command=command("auth", "logout"),
        ),
        doctor=DoctorProbeSpec(
            JsonProbeSpec(
                command("--doctor-json"),
                expected={"provider": "fixture-provider"},
            )
        ),
        models=ModelProbeSpec(
            JsonProbeSpec(
                command("--models-json"),
                expected={"provider": "fixture-provider"},
            )
        ),
        capabilities=frozenset(
            (
                ProviderCapability.AUTH.value,
                ProviderCapability.CHAT.value,
                ProviderCapability.MODELS.value,
                ProviderCapability.SESSIONS.value,
            )
        ),
    )
    return replace(spec, **changes) if changes else spec


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_exit(*pids):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(pid_exists(pid) for pid in pids):
            return
        time.sleep(0.02)
    assert not any(pid_exists(pid) for pid in pids)


class InjectedBaseException(BaseException):
    pass


def owned_temporary(prefix="unified-cli-ext-test-"):
    owner = _OwnedTemporaryDirectory(prefix=prefix)
    owner.create()
    return owner


def run_tty_call(callback):
    master_fd, slave_fd = pty.openpty()
    try:
        with os.fdopen(slave_fd, "r+b", buffering=0) as terminal:
            returncode = callback(terminal)
        output = bytearray()
        while True:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
        return returncode, bytes(output).decode("utf-8").replace("\r\n", "\n")
    finally:
        os.close(master_fd)


def run_auth_tty(session):
    return run_tty_call(
        lambda terminal: session.run(
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
        )
    )


def test_metadata_is_versioned_immutable_and_server_disabled():
    spec = adapter_spec()
    adapter = ProviderAdapterV1(spec)

    assert spec.abi_version == PROVIDER_ADAPTER_ABI_V1
    assert adapter.descriptor.status is AdapterStatus.PREVIEW
    assert adapter.descriptor.route_prefix == "fixture-provider"
    assert adapter.descriptor.session_namespace == "fixture-provider"
    assert adapter.descriptor.server_enabled is False
    with pytest.raises(FrozenInstanceError):
        spec.id = "changed"
    with pytest.raises(ConfigurationError, match="disabled in server mode"):
        adapter.require_server_access()


@pytest.mark.parametrize("abi_version", (True, 1.0))
def test_adapter_abi_version_requires_an_exact_integer(abi_version):
    with pytest.raises(ConfigurationError, match="unsupported provider adapter ABI"):
        replace(adapter_spec(), abi_version=abi_version)


def test_model_probe_rejects_plain_text_without_a_bounded_list_grammar():
    plain_probe = PlainTextProbeSpec(command("--models-plain"))

    with pytest.raises(ConfigurationError, match="model probe must return JSON output"):
        ModelProbeSpec(plain_probe)


def test_entire_line_version_extraction_is_unique_bounded_and_exact():
    probe = PlainTextProbeSpec(
        command("--version"),
        fields={
            "version": PlainTextFieldSpec(
                "",
                max_chars=128,
                first_token=True,
                entire_line=True,
            )
        },
        expected={"version": None},
    )

    assert dict(
        ProviderAdapterV1._plain_record(
            "0.0.1784809706-g96cc8a (released at a bounded timestamp)\n",
            probe,
        )
    ) == {"version": "0.0.1784809706-g96cc8a"}
    with pytest.raises(ProtocolError, match="ambiguous"):
        ProviderAdapterV1._plain_record("1.2.3\nunexpected\n", probe)

    suffixed = PlainTextProbeSpec(
        command("--version"),
        fields={
            "version": PlainTextFieldSpec(
                "GitHub Copilot CLI ",
                max_chars=128,
                required_suffix=".",
            )
        },
        expected={"version": None},
    )
    assert dict(
        ProviderAdapterV1._plain_record(
            "GitHub Copilot CLI 1.0.73.\n",
            suffixed,
        )
    ) == {"version": "1.0.73"}
    with pytest.raises(ProtocolError, match="required suffix"):
        ProviderAdapterV1._plain_record(
            "GitHub Copilot CLI 1.0.73\n",
            suffixed,
        )


def test_adapter_metadata_has_aggregate_utf8_bound_and_stable_snapshot():
    oversized_prompt = PromptCommandSpec(
        tuple("x" * (16 * 1024) for _ in range(20)),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    with pytest.raises(ConfigurationError, match="262144 UTF-8 bytes"):
        replace(adapter_spec(), prompt=oversized_prompt)

    original = adapter_spec()
    adapter = ProviderAdapterV1(original)
    assert adapter.spec is adapter.spec
    assert adapter.spec is not original
    for _ in range(1000):
        assert adapter.spec.transport is TransportKind.JSONL
    with pytest.raises(ConfigurationError, match="disabled in server mode"):
        AdapterServerPolicy(enabled=True)


@pytest.mark.parametrize(
    "provider_id",
    (
        "",
        "Fixture",
        "bad--id",
        "bad/id",
        "claude",
        "agy",
        "gpt-fourth-party",
        "x" * 65,
    ),
)
def test_unsafe_and_reserved_provider_ids_are_rejected(provider_id):
    with pytest.raises(ConfigurationError, match="invalid provider adapter id"):
        replace(adapter_spec(), id=provider_id)


def test_registry_rejects_duplicates_without_resolving_or_probing(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("lazy registry attempted provider I/O")

    monkeypatch.setattr(os, "open", forbidden)
    first = adapter_spec()
    registry = ProviderAdapterRegistryV1((first,))
    assert registry.descriptors()[0].id == "fixture-provider"
    with pytest.raises(ConfigurationError, match="duplicate"):
        registry.register(first)
    assert calls == []


def test_import_is_lazy_and_does_not_import_core_plugin_or_probe(monkeypatch):
    original_import = builtins.__import__
    seen = []

    def capture(name, *args, **kwargs):
        seen.append(name)
        if name == "unified_cli.plugin":
            raise AssertionError("provider import reached Core plugin ABI")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", capture)
    sys.modules.pop("unified_cli_ext.providers.registry", None)
    __import__("unified_cli_ext.providers.registry")
    assert "unified_cli.plugin" not in seen


def test_fixed_argv_prompt_builder_never_creates_a_shell_string(adapter_binary):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    prompt = "--danger; $(touch should-not-run)\nsecond line"
    built = adapter.build_prompt(
        binary,
        prompt,
        {"model": "--also-a-value", "session": "session with spaces"},
    )

    assert built.argv[0] == os.path.realpath(adapter_binary)
    assert built.argv[-2:] == ("--", prompt)
    assert built.argv[built.argv.index("--model") + 1] == "--also-a-value"
    assert built.argv[built.argv.index("--session") + 1] == "session with spaces"
    assert built.stdin_text is None
    assert all(type(value) is str for value in built.argv)
    with pytest.raises(ConfigurationError, match="not declared"):
        adapter.build_prompt(binary, "hello", {"model": "safe", "shell": "sh -c"})


def test_prompt_sentinel_policy_fails_closed():
    with pytest.raises(ConfigurationError, match="require"):
        PromptCommandSpec(
            ("chat",),
            mode=PromptMode.ARGV,
            sentinel_policy=PromptSentinelPolicy.FORBIDDEN,
        )
    with pytest.raises(ConfigurationError, match="must not"):
        PromptCommandSpec(
            ("chat",),
            mode=PromptMode.STDIN,
            sentinel_policy=PromptSentinelPolicy.REQUIRED,
        )
    with pytest.raises(ConfigurationError, match="string collection"):
        FixedCommandSpec("--version")


def test_exact_binary_provenance_and_fixture_driven_probes(adapter_binary):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    inspection = adapter.inspect(binary)

    assert inspection.id == "fixture-provider"
    assert inspection.version == "2.4.1"
    assert inspection.features == frozenset(("auth", "chat", "models", "sessions"))
    assert len(inspection.binary.sha256) == 64
    assert inspection.binary.real_path == os.path.realpath(adapter_binary)
    assert inspection.binary.owner in (0, os.geteuid())
    assert inspection.binary.parent_chain
    assert inspection.binary.executable_identity().parent_chain == (
        inspection.binary.parent_chain
    )
    assert adapter.doctor_provider(inspection) is True
    assert adapter.list_models(inspection) == ("fixture-small", "fixture-large")


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable authority contract")
def test_executable_identity_rejects_unsafe_parent_and_write_modes(tmp_path):
    unsafe_parent = tmp_path / "unsafe-bin"
    unsafe_parent.mkdir(mode=0o700)
    executable = unsafe_parent / "fixture-bin"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    unsafe_parent.chmod(0o777)
    try:
        with pytest.raises(ConfigurationError, match="parent.*permissions"):
            ExecutableIdentity.capture(str(executable))
    finally:
        unsafe_parent.chmod(0o700)

    executable.chmod(0o720)
    with pytest.raises(ConfigurationError, match="group/world writable"):
        ExecutableIdentity.capture(str(executable))


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="changing executable ownership requires root",
)
def test_executable_identity_rejects_unsafe_owner(tmp_path):
    executable = tmp_path / "foreign-bin"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    os.chown(str(executable), 1, -1)
    with pytest.raises(ConfigurationError, match="unsafe owner"):
        ExecutableIdentity.capture(str(executable))


def test_executable_operations_require_exact_issued_inspection(adapter_binary, tmp_path):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    provider_home = str(tmp_path / "provider-home")
    operations = (
        lambda value: adapter.doctor_provider(value),
        lambda value: adapter.list_models(value),
        lambda value: adapter.authenticated(value),
        lambda value: adapter.prepare_auth_login(value, provider_home=provider_home),
        lambda value: adapter.prepare_auth_logout(value, provider_home=provider_home),
        lambda value: adapter.execute_auth_login(value, provider_home=provider_home),
        lambda value: adapter.execute_auth_logout(value, provider_home=provider_home),
    )
    for operation in operations:
        with pytest.raises(ConfigurationError, match="inspection"):
            operation(binary)

    inspection = adapter.inspect(binary)
    forged = AdapterInspectionV1(
        id=inspection.id,
        version=inspection.version,
        features=inspection.features,
        binary=inspection.binary,
        abi_version=inspection.abi_version,
    )
    twin = ProviderAdapterV1(adapter_spec())
    with pytest.raises(ConfigurationError, match="inspection"):
        adapter.doctor_provider(forged)
    with pytest.raises(ConfigurationError, match="inspection"):
        twin.doctor_provider(inspection)

    second_inspection = adapter.inspect(binary)
    alternate_path = tmp_path / "alternate" / "fake-adapter"
    alternate_path.parent.mkdir()
    alternate_path.write_bytes(Path(adapter_binary).read_bytes())
    alternate_path.chmod(0o700)
    object.__setattr__(
        second_inspection,
        "binary",
        adapter.resolve_binary(str(alternate_path)),
    )
    with pytest.raises(ConfigurationError, match="inspection"):
        adapter.doctor_provider(second_inspection)

    third_inspection = adapter.inspect(binary)
    object.__setattr__(third_inspection.binary, "sha256", "0" * 64)
    with pytest.raises(ConfigurationError, match="inspection"):
        adapter.doctor_provider(third_inspection)


def test_executable_operation_reverifies_inspected_binary(adapter_binary):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    inspection = adapter.inspect(binary)
    with open(adapter_binary, "ab") as stream:
        stream.write(b"\n# replaced after inspection\n")
    with pytest.raises(ConfigurationError, match="provenance changed"):
        adapter.doctor_provider(inspection)


def test_executable_digest_is_not_recomputed_in_inspection_or_run(
    adapter_binary, tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_sha256 = security_module.hashlib.sha256
    captures = []

    def counted_sha256(*args, **kwargs):
        captures.append(True)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(security_module.hashlib, "sha256", counted_sha256)
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    # The script and its directly bound interpreter are each captured once.
    assert len(captures) == 2

    def forbidden_rehash(*args, **kwargs):
        raise AssertionError("run path attempted a full executable rehash")

    monkeypatch.setattr(security_module.hashlib, "sha256", forbidden_rehash)
    inspection = adapter.inspect(binary)
    assert adapter.doctor_provider(inspection) is True
    prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    plain = ProviderAdapterV1(
        replace(
            adapter_spec(),
            prompt=prompt,
            transport=TransportKind.JSON,
            capabilities=frozenset(("auth", "chat", "models")),
        )
    )
    plain_binary = binary
    plain_inspection = plain.inspect(plain_binary)
    assert plain.open_transport(
        plain_inspection, "hello", cwd=str(tmp_path)
    ).run().returncode == 0


def test_wrong_binary_identity_is_rejected(adapter_binary):
    adapter = ProviderAdapterV1(
        adapter_spec(version_argv=("--version-json", "--wrong-identity"))
    )
    binary = adapter.resolve_binary(adapter_binary)
    with pytest.raises(ProtocolError, match="identity"):
        adapter.inspect(binary)


def test_binary_provenance_detects_replacement(adapter_binary):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    with open(adapter_binary, "ab") as stream:
        stream.write(b"\n# changed\n")
    with pytest.raises(ConfigurationError, match="provenance changed"):
        adapter.build_prompt(binary, "hello", {"model": "fixture-small"})


def test_environment_policy_strips_ambient_and_unlisted_secrets(adapter_binary, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-token")
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    inspection = adapter.inspect(binary)

    selected = adapter.spec.environment.select(
        {
            "FIXTURE_AUTH": "ready",
            "AWS_SECRET_ACCESS_KEY": "explicit-but-unlisted",
            "GITHUB_TOKEN": "explicit-token",
        }
    )
    assert dict(selected) == {"FIXTURE_AUTH": "ready"}
    assert adapter.authenticated(
        inspection,
        provider_env={
            "FIXTURE_AUTH": "ready",
            "AWS_SECRET_ACCESS_KEY": "must-be-stripped",
        },
    ) is True


def test_environment_policy_fixed_values_cannot_be_overridden():
    policy = EnvironmentPolicy(
        allowed_keys=frozenset(("FIXTURE_AUTH",)),
        fixed_values={"FIXTURE_SAFE_MODE": "true"},
    )

    assert policy.allowed_keys == frozenset(
        ("FIXTURE_AUTH", "FIXTURE_SAFE_MODE")
    )
    assert dict(
        policy.select(
            {"FIXTURE_AUTH": "ready", "FIXTURE_SAFE_MODE": "false"}
        )
    ) == {"FIXTURE_AUTH": "ready", "FIXTURE_SAFE_MODE": "true"}


@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_isolated_environment_enter_cleans_every_baseexception(
    tmp_path, monkeypatch, failure_type
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_create = security_module._OwnedTemporaryDirectory.create
    real_mkdir = security_module.os.mkdir
    roots = []

    def captured_create(owner):
        roots.append(owner.name)
        return real_create(owner)

    def fail_after_mkdir(path, mode=0o777, *args, **kwargs):
        real_mkdir(path, mode, *args, **kwargs)
        if os.path.basename(path) == "tmp":
            raise failure_type("injected environment enter failure")

    monkeypatch.setattr(
        security_module._OwnedTemporaryDirectory, "create", captured_create
    )
    monkeypatch.setattr(security_module.os, "mkdir", fail_after_mkdir)
    environment = IsolatedEnvironment()
    with pytest.raises(failure_type, match="injected environment enter failure"):
        environment.__enter__()

    assert environment.env == {}
    assert environment._temporary is None
    assert environment._home_pin is None
    assert environment._tmp_pin is None
    assert roots and not os.path.exists(roots[0])


def test_runtime_temporary_owner_survives_root_mkdir_create_then_raise(monkeypatch):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_mkdir = security_module.os.mkdir
    created = []

    def create_then_raise(path, mode=0o777, *args, **kwargs):
        real_mkdir(path, mode, *args, **kwargs)
        if os.path.basename(path).startswith("unified-cli-ext-") and not created:
            created.append(path)
            raise InjectedBaseException("injected root mkdir create-then-raise")

    monkeypatch.setattr(security_module.os, "mkdir", create_then_raise)
    environment = IsolatedEnvironment()
    with pytest.raises(InjectedBaseException, match="create-then-raise"):
        environment.__enter__()
    assert created
    assert not os.path.lexists(created[0])
    assert environment._temporary is None


@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_persistent_home_creation_baseexception_removes_partial_directory(
    tmp_path, monkeypatch, failure_type
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    home = str(tmp_path / "provider-home")
    real_mkdir = security_module.os.mkdir

    def create_then_fail(path, mode=0o777, *args, **kwargs):
        real_mkdir(path, mode, *args, **kwargs)
        if path == home:
            raise failure_type("injected persistent HOME creation failure")

    monkeypatch.setattr(security_module.os, "mkdir", create_then_fail)
    with pytest.raises(failure_type, match="persistent HOME creation failure"):
        security_module.private_persistent_home(home)
    assert not os.path.exists(home)


@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_isolated_environment_exit_preserves_body_and_falls_back_on_cleanup_failure(
    monkeypatch, failure_type
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_create = security_module._OwnedTemporaryDirectory.create
    real_rmtree = security_module.shutil.rmtree
    roots = []

    def captured_create(owner):
        roots.append(owner.name)
        return real_create(owner)

    def remove_then_fail(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        raise failure_type("injected cleanup failure")

    monkeypatch.setattr(
        security_module._OwnedTemporaryDirectory, "create", captured_create
    )
    monkeypatch.setattr(security_module.shutil, "rmtree", remove_then_fail)
    environment = IsolatedEnvironment()
    with pytest.raises(failure_type, match="original body failure"):
        with environment:
            raise failure_type("original body failure")

    assert environment.env == {}
    assert environment._temporary is None
    assert roots and not os.path.exists(roots[0])


def test_directory_pin_close_failure_terminalizes_without_retry(
    tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    pin = security_module.DirectoryPin(str(tmp_path))
    descriptor = pin._descriptor
    real_close = security_module.os.close
    attempts = []

    def fail_once(candidate):
        if candidate == descriptor and not attempts:
            attempts.append(candidate)
            raise OSError("injected descriptor close failure")
        return real_close(candidate)

    monkeypatch.setattr(security_module.os, "close", fail_once)
    with pytest.raises(OSError, match="descriptor close failure"):
        pin.close()
    assert pin._closed is True
    assert pin._descriptor == -1
    os.fstat(descriptor)

    # The ambiguous descriptor is deliberately not retried by the owner.
    pin.close()
    assert attempts == [descriptor]
    os.fstat(descriptor)
    real_close(descriptor)


def test_directory_pin_close_reconciles_closed_and_reused_descriptors(
    tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_close = security_module.os.close

    closed_pin = security_module.DirectoryPin(str(tmp_path))
    closed_descriptor = closed_pin._descriptor

    def close_then_raise(candidate):
        assert candidate == closed_descriptor
        real_close(candidate)
        raise OSError("injected close-after-success failure")

    monkeypatch.setattr(security_module.os, "close", close_then_raise)
    with pytest.raises(OSError, match="close-after-success"):
        closed_pin.close()
    assert closed_pin.closed is True
    assert closed_pin._descriptor == -1
    with pytest.raises(OSError):
        os.fstat(closed_descriptor)
    closed_pin.close()

    monkeypatch.setattr(security_module.os, "close", real_close)
    other = tmp_path / "unrelated"
    other.mkdir()
    reused_pin = security_module.DirectoryPin(str(tmp_path))
    reused_descriptor = reused_pin._descriptor
    replacement_descriptors = []

    def close_reuse_then_raise(candidate):
        assert candidate == reused_descriptor
        real_close(candidate)
        replacement = os.open(
            str(other),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        if replacement != candidate:
            os.dup2(replacement, candidate)
            real_close(replacement)
            replacement = candidate
        replacement_descriptors.append(replacement)
        raise OSError("injected fd-reuse close failure")

    monkeypatch.setattr(security_module.os, "close", close_reuse_then_raise)
    with pytest.raises(OSError, match="fd-reuse"):
        reused_pin.close()
    assert replacement_descriptors == [reused_descriptor]
    assert reused_pin.closed is True
    assert reused_pin._descriptor == -1
    unrelated_stat = os.fstat(reused_descriptor)
    assert unrelated_stat.st_ino == os.stat(other).st_ino
    reused_pin.close()
    os.fstat(reused_descriptor)
    real_close(reused_descriptor)


def test_directory_pin_close_serializes_and_never_closes_same_inode_reuse(
    tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_close = security_module.os.close

    pin = security_module.DirectoryPin(str(tmp_path))
    descriptor = pin._descriptor
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_close(candidate):
        calls.append(candidate)
        entered.set()
        assert release.wait(timeout=2)
        real_close(candidate)

    monkeypatch.setattr(security_module.os, "close", blocked_close)
    failures = []

    def close_pin():
        try:
            pin.close()
        except BaseException as caught:
            failures.append(caught)

    first = threading.Thread(target=close_pin)
    second = threading.Thread(target=close_pin)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert calls == [descriptor]

    monkeypatch.setattr(security_module.os, "close", real_close)
    reused = security_module.DirectoryPin(str(tmp_path))
    reused_descriptor = reused._descriptor

    def close_reopen_same_inode_then_raise(candidate):
        real_close(candidate)
        replacement = os.open(
            str(tmp_path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        if replacement != candidate:
            os.dup2(replacement, candidate)
            real_close(replacement)
        raise OSError("injected same-inode fd reuse")

    monkeypatch.setattr(
        security_module.os, "close", close_reopen_same_inode_then_raise
    )
    with pytest.raises(OSError, match="same-inode"):
        reused.close()
    assert reused.closed is True
    reused.close()
    assert os.fstat(reused_descriptor).st_ino == os.stat(tmp_path).st_ino
    real_close(reused_descriptor)


def test_redaction_hides_long_secrets_across_output_boundary():
    secret = "s" * (64 * 1024)
    at_start = redact_diagnostics(secret + " visible", (secret,), max_chars=128)
    assert "s" * 8 not in at_start
    assert "[REDACTED]" in at_start

    crossing = "x" * 124 + secret + " tail"
    bounded = redact_diagnostics(crossing, (secret,), max_chars=128)
    assert len(bounded) <= 128
    assert "s" not in bounded
    assert bounded.startswith("x" * 124)

    huge_input = "safe-prefix " + ("z" * (2 * 1024 * 1024))
    assert len(redact_diagnostics(huge_input, (), max_chars=64)) == 64

    overlapping = redact_diagnostics("xxabcabcxx", ("abc", "bc"), max_chars=64)
    assert "abc" not in overlapping and "bc" not in overlapping


@pytest.mark.parametrize("max_chars", (4096, 65536))
def test_redaction_excessive_overlapping_secret_set_is_fast_and_fully_redacted(
    max_chars,
):
    secrets = tuple(
        ("shared-secret-prefix-" * 3120)[: 64 * 1024 - 16]
        + "{:016x}".format(index)
        for index in range(256)
    )
    text = secrets[-1] + " visible diagnostic"
    started = time.perf_counter()
    result = redact_diagnostics(text, secrets, max_chars=max_chars)
    elapsed = time.perf_counter() - started
    assert result == "[REDACTED]"
    assert "shared-secret" not in result
    assert elapsed < 5.0


def test_executable_identity_rejects_env_shebang_and_pins_direct_interpreter(
    tmp_path
):
    env_script = tmp_path / "env-script"
    env_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    env_script.chmod(0o700)
    with pytest.raises(ConfigurationError, match="without env"):
        ExecutableIdentity.capture(str(env_script))

    interpreter = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    direct_script = tmp_path / "direct-script"
    direct_script.write_text(
        "#!{}\nraise SystemExit(0)\n".format(interpreter),
        encoding="utf-8",
    )
    direct_script.chmod(0o700)
    identity = ExecutableIdentity.capture(str(direct_script))
    assert identity.interpreter is not None
    assert identity.interpreter.path == str(interpreter)
    with open(interpreter, "ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ConfigurationError, match="identity changed"):
        identity.verify()


def test_interpreter_chain_depth_is_bounded_in_capture_and_runtime_copy(tmp_path):
    binary = tmp_path / "chain-binary"
    shutil.copyfile(os.path.realpath(sys.executable), binary)
    binary.chmod(0o700)
    current = binary
    for index in range(4):
        script = tmp_path / "chain-script-{}".format(index)
        script.write_text("#!{}\n".format(current), encoding="utf-8")
        script.chmod(0o700)
        current = script
    bounded = ExecutableIdentity.capture(str(current))
    assert bounded.interpreter is not None

    excess = tmp_path / "chain-excess"
    excess.write_text("#!{}\n".format(current), encoding="utf-8")
    excess.chmod(0o700)
    with pytest.raises(ConfigurationError, match="maximum depth"):
        ExecutableIdentity.capture(str(excess))

    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    forged = ExecutableIdentity.capture(str(binary))
    for _ in range(5):
        forged = replace(forged, interpreter=forged)
    with pytest.raises(ConfigurationError, match="maximum depth"):
        runtime_module._identity_record(forged)


def test_isolated_environment_cleanup_retries_pins_and_temporary_tree(
    tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    environment = IsolatedEnvironment().__enter__()
    temporary = environment._temporary
    assert temporary is not None
    temporary_root = os.path.realpath(temporary.name)
    real_cleanup = security_module._OwnedTemporaryDirectory.cleanup
    primary_attempts = []

    def fail_primary_once(owner):
        if owner is temporary and not primary_attempts:
            primary_attempts.append(True)
            raise OSError("injected primary cleanup failure")
        return real_cleanup(owner)

    monkeypatch.setattr(
        security_module._OwnedTemporaryDirectory, "cleanup", fail_primary_once
    )
    with pytest.raises(OSError, match="primary cleanup failure"):
        environment._cleanup()

    assert environment._temporary is temporary
    assert os.path.exists(temporary_root)
    assert environment.env == {}
    assert environment._home_pin is None
    assert environment._tmp_pin is None

    environment._cleanup()
    assert environment._temporary is None
    assert not os.path.exists(temporary_root)


def test_malformed_metadata_and_probe_results_fail_closed(adapter_binary):
    with pytest.raises(ConfigurationError):
        replace(adapter_spec(), status="Preview")
    with pytest.raises(ConfigurationError):
        replace(adapter_spec(), route_prefix="other-provider")
    with pytest.raises(ConfigurationError):
        replace(adapter_spec(), session_namespace="other-provider")
    with pytest.raises(ConfigurationError):
        replace(adapter_spec(), capabilities=frozenset(("unknown-capability",)))

    malformed = ProviderAdapterV1(
        adapter_spec(version_argv=("--malformed-json",))
    )
    with pytest.raises(ProtocolError):
        malformed.inspect(malformed.resolve_binary(adapter_binary))


def test_held_adapter_and_pre_cancelled_probe_do_not_execute(adapter_binary):
    held = ProviderAdapterV1(replace(adapter_spec(), status=AdapterStatus.HELD))
    binary = held.resolve_binary(adapter_binary)
    with pytest.raises(ConfigurationError, match="held"):
        held.inspect(binary)

    cancellation = CancellationToken()
    cancellation.cancel()
    adapter = ProviderAdapterV1(adapter_spec())
    with pytest.raises(TransportCancelled, match="cancelled"):
        adapter.inspect(adapter.resolve_binary(adapter_binary), cancellation=cancellation)


def test_foundation_exposes_no_core_execution_bridge():
    adapter = ProviderAdapterV1(adapter_spec())
    assert not hasattr(adapter, "build_core_plugin")


def test_probe_formats_plain_single_json_jsonl_and_exit_status(adapter_binary):
    plain_spec = replace(
        adapter_spec(),
        binary=BinarySpec(
            executable="fake-adapter",
            expected_identity="fixture-provider",
            version_probe=VersionProbeSpec(
                command("--version"),
                minimum_version=(2, 1),
                format=ProbeFormat.PLAIN_TEXT,
                version_marker="fixture-provider ",
            ),
            feature_probe=FeatureProbeSpec(
                command("--help"),
                required_features=frozenset(("auth", "chat", "models", "sessions")),
                format=ProbeFormat.PLAIN_TEXT,
                feature_markers={
                    "auth": "  --auth",
                    "chat": "  --chat",
                    "models": "  --models",
                    "sessions": "  --sessions",
                },
            ),
        ),
    )
    plain = ProviderAdapterV1(plain_spec)
    inspected = plain.inspect(plain.resolve_binary(adapter_binary))
    assert inspected.version == "2.4.1"
    assert inspected.features == frozenset(("auth", "chat", "models", "sessions"))

    single_spec = replace(
        adapter_spec(),
        binary=replace(
            adapter_spec().binary,
            version_probe=VersionProbeSpec(
                command("--version-single-json"),
                minimum_version=(2, 1),
                format=ProbeFormat.JSON,
            ),
        ),
        doctor=DoctorProbeSpec(
            JsonProbeSpec(command("--single-json"), format=ProbeFormat.JSON)
        ),
    )
    single = ProviderAdapterV1(single_spec)
    single_binary = single.resolve_binary(adapter_binary)
    single_inspection = single.inspect(single_binary)
    assert single_inspection.version == "2.4.1"
    assert single.doctor_provider(single_inspection) is True

    exit_adapter = ProviderAdapterV1(
        replace(
            adapter_spec(),
            doctor=DoctorProbeSpec(ExitStatusProbeSpec(command("--exit-ok"))),
        )
    )
    exit_inspection = exit_adapter.inspect(exit_adapter.resolve_binary(adapter_binary))
    assert exit_adapter.doctor_provider(exit_inspection) is True

    with pytest.raises(ConfigurationError, match="feature evidence"):
        FeatureProbeSpec(command("--exit-ok"), format=ProbeFormat.EXIT_STATUS)


@pytest.mark.parametrize(
    ("version_argv", "feature_argv"),
    (
        (("--version", "--prose-marker"), ("--help",)),
        (("--version", "--duplicate-field"), ("--help",)),
        (("--version",), ("--help", "--prose-markers")),
        (("--version",), ("--help", "--wrong-prose-identity")),
    ),
)
def test_plain_probe_evidence_is_line_anchored_and_unambiguous(
    adapter_binary, version_argv, feature_argv
):
    spec = replace(
        adapter_spec(),
        binary=BinarySpec(
            executable="fake-adapter",
            expected_identity="fixture-provider",
            version_probe=VersionProbeSpec(
                command(*version_argv),
                minimum_version=(2, 1),
                format=ProbeFormat.PLAIN_TEXT,
                version_marker="fixture-provider ",
            ),
            feature_probe=FeatureProbeSpec(
                command(*feature_argv),
                required_features=frozenset(("auth", "chat", "models", "sessions")),
                format=ProbeFormat.PLAIN_TEXT,
                feature_markers={
                    "auth": "  --auth",
                    "chat": "  --chat",
                    "models": "  --models",
                    "sessions": "  --sessions",
                },
            ),
        ),
    )
    adapter = ProviderAdapterV1(spec)
    with pytest.raises(ProtocolError):
        adapter.inspect(adapter.resolve_binary(adapter_binary))


def test_presence_only_expected_field_and_final_minimum_reject_prerelease(adapter_binary):
    missing = ProviderAdapterV1(
        replace(
            adapter_spec(),
            doctor=DoctorProbeSpec(
                JsonProbeSpec(
                    command("--metadata-json"),
                    expected={"missing": None},
                )
            ),
        )
    )
    missing_inspection = missing.inspect(missing.resolve_binary(adapter_binary))
    with pytest.raises(ProtocolError, match="specification"):
        missing.doctor_provider(missing_inspection)

    prerelease = ProviderAdapterV1(
        adapter_spec(version_argv=("--version-json", "--prerelease"))
    )
    with pytest.raises(ProtocolError, match="prerelease"):
        prerelease.inspect(prerelease.resolve_binary(adapter_binary))


@pytest.mark.parametrize(
    "flag",
    (
        "--leading-zero",
        "--empty-prerelease",
        "--numeric-prerelease-zero",
        "--oversized-component",
    ),
)
def test_version_parser_rejects_adversarial_versions(adapter_binary, flag):
    adapter = ProviderAdapterV1(
        adapter_spec(version_argv=("--version-json", flag))
    )
    with pytest.raises(ProtocolError, match="invalid version"):
        adapter.inspect(adapter.resolve_binary(adapter_binary))


def test_all_prompt_modes_preserve_untrusted_text_and_reject_nul(adapter_binary):
    binary = ProviderAdapterV1(adapter_spec()).resolve_binary(adapter_binary)
    prompt = "--leading\n유니코드"
    cases = (
        (
            PromptCommandSpec(("chat",), mode=PromptMode.STDIN),
            (("chat",), prompt, None),
        ),
        (
            PromptCommandSpec(
                ("chat",),
                dynamic_arguments=(DynamicArgument("model", "-m"),),
                mode=PromptMode.OPTION_VALUE,
                prompt_option="-p",
            ),
            (("chat", "-m", "--model", "-p", prompt), None, None),
        ),
        (
            PromptCommandSpec(
                ("chat",),
                mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
                sentinel_policy=PromptSentinelPolicy.REQUIRED,
            ),
            (("chat", "--", prompt), None, None),
        ),
        (
            PromptCommandSpec(("chat",), mode=PromptMode.PROTOCOL),
            (("chat",), None, prompt),
        ),
    )
    for spec, expected in cases:
        built = spec.build(binary.real_path, prompt, {"model": "--model"} if spec.dynamic_arguments else None)
        assert built.argv[1:] == expected[0]
        assert built.stdin_text == expected[1]
        assert built.protocol_text == expected[2]
        with pytest.raises(ConfigurationError, match="prompt"):
            spec.build(binary.real_path, "bad\x00prompt")
    with pytest.raises(ConfigurationError, match="short or long"):
        DynamicArgument("model", "model")
    with pytest.raises(ConfigurationError, match="short or long"):
        PromptCommandSpec(
            ("chat",), mode=PromptMode.OPTION_VALUE, prompt_option="-bad"
        )


def test_persistent_provider_home_is_explicit_private_and_shared(adapter_binary, tmp_path, monkeypatch):
    ambient = tmp_path / "ambient-home"
    ambient.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(ambient))
    provider_home = tmp_path / "provider-home"
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    inspection = adapter.inspect(binary)

    session = adapter.prepare_auth_login(inspection, provider_home=str(provider_home))
    assert isinstance(session, InteractiveAuthSessionV1)
    assert not hasattr(session, "argv")
    assert not hasattr(session, "cwd")
    assert "login" not in repr(session).lower()
    assert str(provider_home) not in repr(session)
    login_returncode, _ = run_auth_tty(session)
    assert login_returncode == 0
    assert str(provider_home) != str(ambient)
    assert stat.S_IMODE(provider_home.stat().st_mode) == 0o700
    assert adapter.authenticated(inspection, provider_home=str(provider_home)) is True
    logout_returncode, _ = run_auth_tty(
        adapter.prepare_auth_logout(inspection, provider_home=str(provider_home))
    )
    assert logout_returncode == 0
    assert adapter.authenticated(inspection, provider_home=str(provider_home)) is False

    with pytest.raises(ConfigurationError, match="deprecated"):
        adapter.build_auth_login(binary)

    no_tty = adapter.prepare_auth_login(inspection, provider_home=str(provider_home))
    with pytest.raises(ConfigurationError, match="interactive auth"):
        no_tty.run()

    direct_login, _ = run_tty_call(
        lambda terminal: adapter.execute_auth_login(
            inspection,
            provider_home=str(provider_home),
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
        )
    )
    assert direct_login == 0
    assert adapter.authenticated(inspection, provider_home=str(provider_home)) is True
    direct_logout, _ = run_tty_call(
        lambda terminal: adapter.execute_auth_logout(
            inspection,
            provider_home=str(provider_home),
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
        )
    )
    assert direct_logout == 0
    assert adapter.authenticated(inspection, provider_home=str(provider_home)) is False

    loose = tmp_path / "loose-home"
    loose.mkdir(mode=0o755)
    with pytest.raises(ConfigurationError, match="owner-only"):
        adapter.authenticated(inspection, provider_home=str(loose))

    target = tmp_path / "target-home"
    target.mkdir(mode=0o700)
    link = tmp_path / "linked-home"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlink"):
        adapter.authenticated(inspection, provider_home=str(link))


@pytest.mark.skipif(os.name != "posix", reason="POSIX TTY identity contract")
def test_interactive_auth_rejects_mixed_tty_devices_before_spawn(
    adapter_binary, tmp_path, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    session = adapter.prepare_auth_login(
        inspection, provider_home=str(tmp_path / "provider-home")
    )
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mixed TTY validation spawned a subprocess")
        ),
    )
    first_master, first_slave = pty.openpty()
    second_master, second_slave = pty.openpty()
    try:
        with os.fdopen(first_slave, "r+b", buffering=0) as first_terminal:
            with os.fdopen(second_slave, "r+b", buffering=0) as second_terminal:
                with pytest.raises(ConfigurationError, match="same TTY"):
                    session.run(
                        stdin=first_terminal,
                        stdout=second_terminal,
                        stderr=first_terminal,
                    )
    finally:
        os.close(first_master)
        os.close(second_master)


def test_pre_cancelled_jsonl_never_creates_environment_or_calls_popen(monkeypatch):
    import importlib

    jsonl_module = importlib.import_module("unified_cli_ext.transports.jsonl")
    security_module = importlib.import_module("unified_cli_ext.transports.security")
    counts = {"temporary": 0, "popen": 0}

    def forbidden_temporary(*args, **kwargs):
        counts["temporary"] += 1
        raise AssertionError("temporary environment was created")

    def forbidden_popen(*args, **kwargs):
        counts["popen"] += 1
        raise AssertionError("Popen was called")

    monkeypatch.setattr(security_module.tempfile, "TemporaryDirectory", forbidden_temporary)
    monkeypatch.setattr(jsonl_module.subprocess, "Popen", forbidden_popen)
    token = CancellationToken()
    token.cancel()
    echo = os.path.realpath("/bin/echo")
    process = JsonlProcess(
        (echo, "{}"),
        cancellation=token,
        executable_identity=ExecutableIdentity.capture(echo),
    )
    with pytest.raises(TransportCancelled):
        process.start()
    assert counts == {"temporary": 0, "popen": 0}


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_fixed_process_timeout_cancel_flood_and_term_ignore_cleanup(
    adapter_binary, tmp_path
):
    identity = ExecutableIdentity.capture(adapter_binary)
    workspace = str(tmp_path)

    timeout_marker = tmp_path / "timeout.pid"
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(timeout_marker)),
            timeout=1.0,
            cwd=workspace,
            executable_identity=identity,
        )
    assert time.monotonic() - started < 3
    timeout_pid = int(timeout_marker.read_text(encoding="utf-8"))
    wait_for_exit(timeout_pid)

    cancel_marker = tmp_path / "cancel.pid"
    token = CancellationToken()

    def cancel_after_start():
        deadline = time.monotonic() + 2
        while not cancel_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        token.cancel()

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()
    with pytest.raises(TransportCancelled):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(cancel_marker)),
            timeout=3,
            cwd=workspace,
            cancellation=token,
            executable_identity=identity,
        )
    canceller.join(timeout=1)
    cancel_pid = int(cancel_marker.read_text(encoding="utf-8"))
    wait_for_exit(cancel_pid)

    started = time.monotonic()
    with pytest.raises(LimitExceeded):
        run_fixed_process(
            (adapter_binary, "--process-flood"),
            timeout=3,
            cwd=workspace,
            limits=TransportLimits(max_output_bytes=1024),
            executable_identity=identity,
        )
    assert time.monotonic() - started < 3

    ignore_marker = tmp_path / "ignore.pid"
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        run_fixed_process(
            (adapter_binary, "--process-term-ignore", str(ignore_marker)),
            timeout=1.0,
            cwd=workspace,
            executable_identity=identity,
        )
    assert time.monotonic() - started < 3
    wait_for_exit(int(ignore_marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_fixed_process_kills_descendants_and_falls_back_when_killpg_fails(
    adapter_binary, tmp_path, monkeypatch
):
    identity = ExecutableIdentity.capture(adapter_binary)
    marker = tmp_path / "descendant.pids"
    with pytest.raises(TransportTimeout):
        run_fixed_process(
            (adapter_binary, "--process-descendant", str(marker)),
            timeout=1.0,
            cwd=str(tmp_path),
            executable_identity=identity,
        )
    parent_pid, child_pid = (
        int(value) for value in marker.read_text(encoding="utf-8").split()
    )
    wait_for_exit(parent_pid, child_pid)

    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    fallback_marker = tmp_path / "fallback.pid"
    monkeypatch.setattr(
        process_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(TransportTimeout):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(fallback_marker)),
            timeout=1.0,
            cwd=str(tmp_path),
            executable_identity=identity,
        )
    wait_for_exit(int(fallback_marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
def test_fixed_and_interactive_group_signals_never_follow_leader_reap(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    real_popen = process_module.subprocess.Popen
    real_killpg = process_module.os.killpg
    spawned = {}
    observations = []

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned[process.pid] = process
        return process

    def guarded_killpg(pgid, sig):
        process = spawned[pgid]
        observations.append((pgid, sig, process.returncode))
        assert process.returncode is None
        return real_killpg(pgid, sig)

    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(process_module.os, "killpg", guarded_killpg)
    identity = ExecutableIdentity.capture(adapter_binary)

    normal = run_fixed_process(
        (adapter_binary, "--exit-ok"),
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    assert normal.returncode == 0

    detached_marker = tmp_path / "guarded-detached.pid"
    detached = run_fixed_process(
        (adapter_binary, "--process-detached-pipe", str(detached_marker)),
        timeout=2,
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    assert detached.returncode == 0
    wait_for_exit(int(detached_marker.read_text(encoding="utf-8")))

    with pytest.raises(TransportTimeout):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(tmp_path / "guarded-hang.pid")),
            timeout=0.05,
            cwd=str(tmp_path),
            executable_identity=identity,
        )

    provider_home = str(tmp_path / "guarded-provider-home")
    returncode, _ = run_tty_call(
        lambda terminal: process_module._run_interactive_process(
            (adapter_binary, "auth", "login"),
            timeout=2,
            cwd=str(tmp_path),
            provider_env=None,
            allowed_provider_env=(),
            persistent_home=provider_home,
            cancellation=None,
            executable_identity=identity,
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
        )
    )
    assert returncode == 0
    assert observations
    assert all(process.returncode is not None for process in spawned.values())


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
def test_fixed_process_detached_inherited_pipe_cannot_deadlock_close(
    adapter_binary, tmp_path
):
    marker = tmp_path / "detached.pid"
    started = time.monotonic()
    result = run_fixed_process(
        (adapter_binary, "--process-detached-pipe", str(marker)),
        timeout=2,
        cwd=str(tmp_path),
        executable_identity=ExecutableIdentity.capture(adapter_binary),
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 1
    wait_for_exit(int(marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
def test_fixed_process_post_popen_setup_failure_reaps_and_cleans_environment(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    real_popen = process_module.subprocess.Popen
    captured = {}

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        captured["environment_root"] = os.path.dirname(kwargs["env"]["HOME"])
        return process

    def fail_set_blocking(*args, **kwargs):
        raise OSError("injected descriptor setup failure")

    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(process_module.os, "set_blocking", fail_set_blocking)
    with pytest.raises(TransportError, match="initialize extension subprocess pipes"):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(tmp_path / "setup.pid")),
            timeout=2,
            cwd=str(tmp_path),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )

    process = captured["process"]
    assert process.poll() is not None
    wait_for_exit(process.pid)
    assert not os.path.exists(captured["environment_root"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
def test_fixed_process_post_popen_runtime_error_reaps_and_cleans_environment(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    real_popen = process_module.subprocess.Popen
    real_observe = process_module._observe_process_returncode_nonreaping
    captured = {}
    observations = []

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        captured["environment_root"] = os.path.dirname(kwargs["env"]["TMPDIR"])
        return process

    def fail_once(process):
        observations.append(process.pid)
        if len(observations) == 1:
            raise RuntimeError("injected post-Popen failure")
        return real_observe(process)

    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(
        process_module, "_observe_process_returncode_nonreaping", fail_once
    )
    with pytest.raises(RuntimeError, match="injected post-Popen failure"):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(tmp_path / "runtime.pid")),
            timeout=2,
            cwd=str(tmp_path),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )

    process = captured["process"]
    assert process.poll() is not None
    wait_for_exit(process.pid)
    assert not os.path.exists(captured["environment_root"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_fixed_process_post_popen_baseexception_reaps_and_cleans_everything(
    adapter_binary, tmp_path, monkeypatch, failure_type
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    real_popen = process_module.subprocess.Popen
    captured = {}

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        captured["environment_root"] = os.path.dirname(kwargs["env"]["TMPDIR"])
        return process

    def fail_pipe_setup(*args, **kwargs):
        raise failure_type("injected post-Popen BaseException")

    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(process_module.os, "set_blocking", fail_pipe_setup)
    with pytest.raises(failure_type, match="injected post-Popen BaseException"):
        run_fixed_process(
            (adapter_binary, "--process-hang", str(tmp_path / "base.pid")),
            timeout=2,
            cwd=str(tmp_path),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )

    process = captured["process"]
    wait_for_exit(process.pid)
    assert process.poll() is not None
    assert all(
        stream is None or stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )
    assert not os.path.exists(captured["environment_root"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_jsonl_start_baseexception_before_popen_cleans_environment(
    adapter_binary, tmp_path, monkeypatch, failure_type
):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    captured = {}

    def fail_popen(*args, **kwargs):
        captured["environment_root"] = os.path.dirname(kwargs["env"]["HOME"])
        raise failure_type("injected Popen BaseException")

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", fail_popen)
    process = JsonlProcess(
        (adapter_binary, "--process-hang", str(tmp_path / "never.pid")),
        cwd=str(tmp_path),
        executable_identity=ExecutableIdentity.capture(adapter_binary),
    )
    with pytest.raises(failure_type, match="injected Popen BaseException"):
        process.start()

    assert process._proc is None
    assert process._threads == []
    assert process._cwd_pin is None
    assert process._environment.env == {}
    assert not os.path.exists(captured["environment_root"])


def test_jsonl_partial_environment_entry_retains_owner_for_later_close(
    adapter_binary, tmp_path, monkeypatch
):
    process = JsonlProcess(
        (adapter_binary, "--exit-ok"),
        cwd=str(tmp_path),
        executable_identity=ExecutableIdentity.capture(adapter_binary),
    )
    environment = process._environment
    real_enter = environment.__enter__
    real_cleanup = environment._cleanup
    roots = []
    cleanup_attempts = []

    def enter_then_fail():
        entered = real_enter()
        roots.append(os.path.dirname(entered.env["HOME"]))
        raise RuntimeError("injected partial environment entry failure")

    def fail_cleanup_twice():
        cleanup_attempts.append(True)
        if len(cleanup_attempts) <= 2:
            raise OSError("injected persistent environment cleanup failure")
        return real_cleanup()

    monkeypatch.setattr(environment, "__enter__", enter_then_fail)
    monkeypatch.setattr(environment, "_cleanup", fail_cleanup_twice)
    with pytest.raises(RuntimeError, match="partial environment entry"):
        process.start()
    assert process._closed is False
    assert process._lifecycle_state == "cleanup_failed"
    assert process._environment_active is True
    assert environment.has_resources is True
    assert roots and os.path.exists(roots[0])

    process.close()
    assert len(cleanup_attempts) == 3
    assert process._closed is True
    assert process._lifecycle_state == "closed"
    assert environment.has_resources is False
    assert not os.path.exists(roots[0])


def test_fixed_partial_environment_entry_retries_before_losing_owner(
    adapter_binary, tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_enter = security_module.IsolatedEnvironment.__enter__
    real_cleanup = security_module.IsolatedEnvironment._cleanup
    tracked = []
    attempts = {}

    def enter_then_fail(environment):
        entered = real_enter(environment)
        tracked.append((environment, os.path.dirname(entered.env["HOME"])))
        raise RuntimeError("injected fixed environment entry failure")

    def fail_cleanup_three_times(environment):
        count = attempts.get(id(environment), 0) + 1
        attempts[id(environment)] = count
        if count <= 3:
            raise OSError("injected fixed cleanup failure")
        return real_cleanup(environment)

    monkeypatch.setattr(
        security_module.IsolatedEnvironment, "__enter__", enter_then_fail
    )
    monkeypatch.setattr(
        security_module.IsolatedEnvironment, "_cleanup", fail_cleanup_three_times
    )
    with pytest.raises(RuntimeError, match="fixed environment entry"):
        run_fixed_process(
            (adapter_binary, "--exit-ok"),
            cwd=str(tmp_path),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )
    assert tracked
    environment, root = tracked[0]
    assert attempts[id(environment)] == 4
    assert environment.has_resources is False
    assert not os.path.exists(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_jsonl_thread_start_baseexception_reaps_and_cleans_everything(
    adapter_binary, tmp_path, monkeypatch, failure_type
):
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    real_popen = jsonl_module.subprocess.Popen
    captured = {}

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        captured["environment_root"] = os.path.dirname(kwargs["env"]["HOME"])
        return process

    def fail_thread_start(self):
        raise failure_type("injected thread start BaseException")

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(jsonl_module.threading.Thread, "start", fail_thread_start)
    process = JsonlProcess(
        (adapter_binary, "--process-hang", str(tmp_path / "thread.pid")),
        cwd=str(tmp_path),
        executable_identity=ExecutableIdentity.capture(adapter_binary),
    )
    with pytest.raises(failure_type, match="injected thread start BaseException"):
        process.start()

    spawned = captured["process"]
    wait_for_exit(spawned.pid)
    assert spawned.poll() is not None
    assert process._proc is None
    assert process._threads == []
    assert process._environment.env == {}
    assert all(
        stream is None or stream.closed
        for stream in (spawned.stdin, spawned.stdout, spawned.stderr)
    )
    assert not os.path.exists(captured["environment_root"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX retryable cleanup contract")
def test_jsonl_close_retries_pipe_pin_and_environment_owners(
    adapter_binary, tmp_path, monkeypatch
):
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    process = JsonlProcess(
        (adapter_binary, "--process-hang", str(tmp_path / "owners.pid")),
        cwd=str(tmp_path),
        executable_identity=ExecutableIdentity.capture(adapter_binary),
    ).start()
    assert process._proc is not None and process._proc.stdin is not None
    spawned = process._proc
    owned_pin = process._cwd_pin
    environment_root = os.path.dirname(process._environment.env["HOME"])
    real_pin_close = security_module.DirectoryPin.close
    real_environment_cleanup = process._environment._cleanup
    pin_attempts = []
    environment_attempts = []

    class FailOncePipe:
        def __init__(self, stream):
            self.stream = stream
            self.attempts = 0

        @property
        def closed(self):
            return self.stream.closed

        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("injected pipe close failure")
            return self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

    owned_pipe = FailOncePipe(spawned.stdin)
    spawned.stdin = owned_pipe

    def fail_pin_once(pin):
        if pin is owned_pin and not pin_attempts:
            pin_attempts.append(True)
            raise OSError("injected pin close failure")
        return real_pin_close(pin)

    def fail_environment_once():
        if len(environment_attempts) < 2:
            environment_attempts.append(True)
            raise OSError("injected environment cleanup failure")
        return real_environment_cleanup()

    monkeypatch.setattr(security_module.DirectoryPin, "close", fail_pin_once)
    monkeypatch.setattr(process._environment, "_cleanup", fail_environment_once)

    with pytest.raises(OSError, match="pipe close failure"):
        process.close()

    assert process._closed is False
    assert process._proc is spawned
    assert process._cwd_pin is owned_pin
    assert process._environment_active is True
    assert os.path.exists(environment_root)

    process.close()
    assert process._closed is True
    assert process._proc is None
    assert process._cwd_pin is None
    assert process._environment_active is False
    assert not process._threads
    assert not os.path.exists(environment_root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup contract")
def test_interactive_post_popen_runtime_error_reaps_and_cleans_environment(
    adapter_binary, tmp_path, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    session = adapter.prepare_auth_login(
        inspection, provider_home=str(tmp_path / "provider-home")
    )
    auth_temporary = session._temporary.name
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    real_popen = process_module.subprocess.Popen
    real_observe = process_module._observe_process_returncode_nonreaping
    captured = {}
    observations = []

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        captured["environment_root"] = os.path.dirname(kwargs["env"]["TMPDIR"])
        return process

    def fail_once(process):
        observations.append(process.pid)
        if len(observations) == 1:
            raise RuntimeError("injected post-Popen failure")
        return real_observe(process)

    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(
        process_module, "_observe_process_returncode_nonreaping", fail_once
    )
    with pytest.raises(RuntimeError, match="injected post-Popen failure"):
        run_auth_tty(session)

    process = captured["process"]
    assert process.poll() is not None
    wait_for_exit(process.pid)
    assert not os.path.exists(captured["environment_root"])
    assert not os.path.exists(auth_temporary)


@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
def test_prepare_auth_command_baseexception_removes_owned_temporary(
    adapter_binary, tmp_path, monkeypatch, failure_type
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    real_create = runtime_module._OwnedTemporaryDirectory.create
    roots = []

    def captured_create(owner):
        roots.append(owner.name)
        return real_create(owner)

    def fail_validation(path):
        raise failure_type("injected auth preparation BaseException")

    monkeypatch.setattr(
        runtime_module._OwnedTemporaryDirectory, "create", captured_create
    )
    monkeypatch.setattr(runtime_module, "validated_workspace", fail_validation)
    with pytest.raises(failure_type, match="injected auth preparation BaseException"):
        adapter.prepare_auth_login(
            inspection,
            provider_home=str(tmp_path / "provider-home"),
        )

    assert roots and not os.path.exists(roots[0])


@pytest.mark.skipif(os.name != "posix", reason="POSIX probe process cleanup contract")
@pytest.mark.parametrize(
    "failure_type", (KeyboardInterrupt, SystemExit, InjectedBaseException)
)
@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_probe_baseexception_preserves_body_and_removes_all_owned_state(
    adapter_binary, tmp_path, monkeypatch, failure_type, cleanup_fails
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_create = runtime_module._OwnedTemporaryDirectory.create
    real_rmtree = security_module.shutil.rmtree
    real_popen = process_module.subprocess.Popen
    roots = []
    environments = []
    processes = []
    baseline_threads = {thread.ident for thread in threading.enumerate()}

    def captured_create(owner):
        roots.append(owner.name)
        return real_create(owner)

    def remove_then_fail(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        if cleanup_fails:
            raise RuntimeError("injected probe cleanup failure")

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        environments.append(os.path.dirname(kwargs["env"]["TMPDIR"]))
        return process

    def fail_parse(payload):
        raise failure_type("injected probe body BaseException")

    monkeypatch.setattr(
        runtime_module._OwnedTemporaryDirectory, "create", captured_create
    )
    monkeypatch.setattr(security_module.shutil, "rmtree", remove_then_fail)
    monkeypatch.setattr(runtime_module, "strict_json_loads", fail_parse)
    monkeypatch.setattr(process_module.subprocess, "Popen", capture_popen)
    probe = JsonProbeSpec(
        command("--version-json"),
        format=ProbeFormat.JSON,
    )
    with pytest.raises(failure_type, match="injected probe body BaseException"):
        adapter._run_probe(binary, probe)

    assert processes
    for process in processes:
        wait_for_exit(process.pid)
        assert process.poll() is not None
    assert roots and all(not os.path.exists(root) for root in roots)
    assert environments and all(
        not os.path.exists(root) for root in environments
    )
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.ident not in baseline_threads
        and thread.name.startswith("unified-cli")
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX probe process cleanup contract")
def test_probe_persistent_cleanup_is_retained_then_explicitly_drained(
    adapter_binary, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    owner_type = runtime_module._OwnedTemporaryDirectory
    real_create = owner_type.create
    real_cleanup = owner_type.cleanup
    roots = []

    def captured_create(owner):
        result = real_create(owner)
        if os.path.basename(owner.name).startswith("unified-cli-ext-probe-"):
            roots.append(owner.name)
        return result

    def persistent_failure(owner):
        if owner.name in roots:
            raise RuntimeError("injected persistent probe cleanup failure")
        return real_cleanup(owner)

    monkeypatch.setattr(owner_type, "create", captured_create)
    monkeypatch.setattr(owner_type, "cleanup", persistent_failure)
    probe = JsonProbeSpec(
        command("--version-json"),
        format=ProbeFormat.JSON,
    )
    with pytest.raises(TransportError, match="retained"):
        adapter._run_probe(binary, probe)
    assert roots and all(os.path.exists(root) for root in roots)
    drain_results = []
    drain_failures = []
    rendezvous = threading.Barrier(3)

    def concurrent_drain():
        rendezvous.wait()
        try:
            drain_results.append(drain_pending_cleanups(max_passes=2))
        except BaseException as caught:
            drain_failures.append(caught)

    drainers = [threading.Thread(target=concurrent_drain) for _ in range(2)]
    for drainer in drainers:
        drainer.start()
    rendezvous.wait()
    for drainer in drainers:
        drainer.join(timeout=2)
    assert all(not drainer.is_alive() for drainer in drainers)
    assert drain_failures == []
    assert drain_results == [1, 1]

    monkeypatch.setattr(owner_type, "cleanup", real_cleanup)
    assert drain_pending_cleanups(max_passes=4) == 0
    assert all(not os.path.exists(root) for root in roots)


@pytest.mark.skipif(os.name != "posix", reason="POSIX probe process cleanup contract")
def test_probe_failed_start_retains_process_pins_and_temp_until_drain(
    adapter_binary, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_init = runtime_module.JsonlProcess.__init__
    real_exit = security_module.IsolatedEnvironment.__exit__
    real_popen = jsonl_module.subprocess.Popen
    captured = []

    def capture_init(owner, *args, **kwargs):
        real_init(owner, *args, **kwargs)
        captured.append(owner)

    def fail_popen(*args, **kwargs):
        raise RuntimeError("injected probe start failure")

    def persistent_environment_cleanup(environment, *args):
        if captured and environment is captured[-1]._environment:
            raise OSError("injected persistent environment cleanup")
        return real_exit(environment, *args)

    monkeypatch.setattr(runtime_module.JsonlProcess, "__init__", capture_init)
    monkeypatch.setattr(jsonl_module.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(
        security_module.IsolatedEnvironment,
        "__exit__",
        persistent_environment_cleanup,
    )
    probe = JsonProbeSpec(command("--version-json"), format=ProbeFormat.JSONL)
    with pytest.raises(RuntimeError, match="probe start failure"):
        adapter._run_probe(binary, probe)

    process = captured[-1]
    environment_root = process._environment._temporary.name
    assert process._lifecycle_state == "cleanup_failed"
    assert process._environment.has_resources
    assert os.path.isdir(environment_root)
    assert drain_pending_cleanups(max_passes=1) == 1

    monkeypatch.setattr(security_module.IsolatedEnvironment, "__exit__", real_exit)
    monkeypatch.setattr(jsonl_module.subprocess, "Popen", real_popen)
    assert drain_pending_cleanups(max_passes=4) == 0
    assert process._resources_complete()
    assert process._lifecycle_state == "closed"
    assert not os.path.lexists(environment_root)


def test_probe_retries_repeated_primary_and_fallback_cleanup_failures(
    adapter_binary, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    owner_type = runtime_module._OwnedTemporaryDirectory
    real_create = owner_type.create
    real_cleanup = owner_type.cleanup
    roots = []
    primary_attempts = []

    def captured_create(owner):
        result = real_create(owner)
        if os.path.basename(owner.name).startswith("unified-cli-ext-probe-"):
            roots.append(owner.name)
        return result

    def repeated_cleanup(owner):
        if owner.name in roots:
            primary_attempts.append(True)
            if len(primary_attempts) <= 2:
                raise OSError("injected repeated probe primary failure")
        return real_cleanup(owner)

    monkeypatch.setattr(owner_type, "create", captured_create)
    monkeypatch.setattr(owner_type, "cleanup", repeated_cleanup)
    probe = JsonProbeSpec(command("--version-json"), format=ProbeFormat.JSON)
    with pytest.raises(OSError, match="repeated probe primary"):
        adapter._run_probe(binary, probe)
    assert len(primary_attempts) == 3
    assert roots and all(not os.path.exists(root) for root in roots)


@pytest.mark.skipif(os.name != "posix", reason="POSIX pinned-directory contract")
def test_cwd_swap_at_popen_conversion_fails_before_provider_execution(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    marker = tmp_path / "executed.marker"
    real_popen = process_module.subprocess.Popen

    def swap_then_popen(*args, **kwargs):
        workspace.rename(tmp_path / "workspace-original")
        replacement.rename(workspace)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process_module.subprocess, "Popen", swap_then_popen)
    with pytest.raises(ConfigurationError, match="directory changed"):
        run_fixed_process(
            (adapter_binary, "--process-executed", str(marker)),
            cwd=str(workspace),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX pinned-directory contract")
def test_persistent_home_swap_during_popen_fails_before_provider_execution(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    home = tmp_path / "provider-home"
    replacement = tmp_path / "replacement-home"
    home.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    marker = tmp_path / "executed.marker"
    real_popen = process_module.subprocess.Popen
    captured = {}

    def swap_then_popen(*args, **kwargs):
        captured["environment_root"] = os.path.dirname(kwargs["env"]["TMPDIR"])
        home.rename(tmp_path / "provider-home-original")
        replacement.rename(home)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process_module.subprocess, "Popen", swap_then_popen)
    with pytest.raises(ConfigurationError, match="directory changed"):
        run_fixed_process(
            (adapter_binary, "--process-executed", str(marker)),
            cwd=str(tmp_path),
            persistent_home=str(home),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )

    assert not marker.exists()
    assert not os.path.exists(captured["environment_root"])


def test_transport_dispatch_uses_process_kinds_and_fails_closed_elsewhere(
    adapter_binary, tmp_path, monkeypatch
):
    structured_caps = frozenset(("auth", "chat", "models"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "project.marker").write_text("visible-project", encoding="utf-8")

    def opened(kind, prompt_spec, config=TransportConfig()):
        adapter = ProviderAdapterV1(
            replace(
                adapter_spec(),
                prompt=prompt_spec,
                transport=kind,
                transport_config=config,
                capabilities=structured_caps,
            )
        )
        binary = adapter.resolve_binary(adapter_binary)
        inspection = adapter.inspect(binary)
        return adapter, inspection

    argv_prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    for kind in (TransportKind.PLAIN, TransportKind.JSON):
        adapter, inspection = opened(kind, argv_prompt)
        boundary = adapter.open_transport(
            inspection, "--safe\n문자", cwd=str(workspace)
        )
        assert isinstance(boundary, OpenedProcessTransportV1)
        result = boundary.run()
        assert result.returncode == 0
        record = json.loads(result.stdout)
        assert record["argv"][-2:] == ["--", "--safe\n문자"]
        assert record["cwd"] == str(workspace)
        assert record["project_marker"] == "visible-project"

    for kind in (TransportKind.JSONL, TransportKind.JSON_RPC):
        adapter, inspection = opened(kind, argv_prompt)
        boundary = adapter.open_transport(inspection, "safe", cwd=str(workspace))
        assert isinstance(boundary, ProtocolLaunchBoundaryV1)
        assert not hasattr(boundary, "transport")
        owned_jsonl = (
            boundary._owned_transport
            if kind is TransportKind.JSONL
            else boundary._owned_transport._transport
        )
        assert owned_jsonl._lifecycle_state == "new"
        assert owned_jsonl.pid is None
        if kind is TransportKind.JSONL:
            with pytest.raises(ConfigurationError, match="not valid"):
                boundary.request("fixture")
            boundary.close_stdin()
            record = boundary.receive()
            assert record["provider"] == "fixture-provider"
            assert boundary.receive() is None
        else:
            with pytest.raises(ConfigurationError, match="not valid"):
                boundary.send({"fixture": True})
        boundary.close()

    protocol_prompt = PromptCommandSpec(("chat",), mode=PromptMode.PROTOCOL)
    popen_calls = []
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        popen_calls.append(args[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    for kind, error in (
        (TransportKind.ACP, "ACP provider execution is not implemented"),
        (TransportKind.HTTP_JSON, "HTTP provider execution is not implemented"),
        (TransportKind.HTTP_SSE, "HTTP provider execution is not implemented"),
    ):
        adapter, inspection = opened(kind, protocol_prompt)
        before = len(popen_calls)
        with pytest.raises(ConfigurationError, match=error):
            adapter.open_transport(inspection, "protocol text", cwd=str(workspace))
        assert len(popen_calls) == before

    with pytest.raises(ConfigurationError, match="prestarted HTTP"):
        replace(
            adapter_spec(),
            prompt=protocol_prompt,
            transport=TransportKind.HTTP_JSON,
            capabilities=structured_caps,
            transport_config=TransportConfig(base_url="http://127.0.0.1:9/fixture"),
        )


def test_protocol_boundary_async_generator_aclose_is_synchronous(
    adapter_binary, tmp_path
):
    identity = ExecutableIdentity.capture(adapter_binary)
    owned = JsonlProcess(
        (adapter_binary, "--jsonl-hang"),
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    boundary = ProtocolLaunchBoundaryV1(
        TransportKind.JSONL, owned, "", None, identity
    )

    async def consume_one_then_close():
        iterator = boundary.aiter_messages()
        record = await iterator.__anext__()
        assert record["provider"] == "fixture-provider"
        await iterator.aclose()
        assert boundary._closed is True
        assert owned._closed is True
        assert owned._proc is None

    asyncio.run(consume_one_then_close())


def test_open_transport_returns_failed_start_owner_for_cleanup_recovery(
    adapter_binary, tmp_path, monkeypatch
):
    prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    adapter = ProviderAdapterV1(
        replace(adapter_spec(), prompt=prompt, transport=TransportKind.JSONL)
    )
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    boundary = adapter.open_transport(inspection, "hello", cwd=str(tmp_path))
    process = boundary._owned_transport
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    security_module = __import__(
        "unified_cli_ext.transports.security", fromlist=["unused"]
    )
    real_exit = security_module.IsolatedEnvironment.__exit__

    def fail_popen(*args, **kwargs):
        raise RuntimeError("injected boundary start failure")

    def persistent_cleanup(environment, *args):
        if environment is process._environment:
            raise OSError("injected boundary cleanup failure")
        return real_exit(environment, *args)

    monkeypatch.setattr(jsonl_module.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(
        security_module.IsolatedEnvironment, "__exit__", persistent_cleanup
    )
    with pytest.raises(RuntimeError, match="boundary start failure"):
        boundary.receive()
    root = process._environment._temporary.name
    assert process._lifecycle_state == "cleanup_failed"
    assert os.path.isdir(root)

    monkeypatch.setattr(security_module.IsolatedEnvironment, "__exit__", real_exit)
    boundary.close()
    assert boundary._closed is True
    assert process._resources_complete()
    assert not os.path.lexists(root)


def test_runtime_transport_handles_are_slot_backed_read_only_and_secret_safe(
    adapter_binary, tmp_path
):
    prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    workspace = str(tmp_path)
    secret = "fixture-super-secret"

    plain_adapter = ProviderAdapterV1(
        replace(
            adapter_spec(),
            prompt=prompt,
            transport=TransportKind.JSON,
            capabilities=frozenset(("auth", "chat", "models")),
        )
    )
    plain_inspection = plain_adapter.inspect(
        plain_adapter.resolve_binary(adapter_binary)
    )
    plain = plain_adapter.open_transport(
        plain_inspection,
        "hello",
        cwd=workspace,
        provider_env={"FIXTURE_AUTH": secret},
    )
    assert not hasattr(plain, "__dict__")
    assert secret not in repr(plain)
    assert workspace not in repr(plain)
    with pytest.raises(AttributeError, match="read-only"):
        plain.invocation = None
    with pytest.raises(AttributeError, match="read-only"):
        plain._provider_env = {}
    first_invocation = plain.invocation
    second_invocation = plain.invocation
    assert first_invocation is not second_invocation
    object.__setattr__(first_invocation, "argv", ("/bin/false",))
    assert plain.invocation.argv[0] == adapter_binary
    assert plain.run().returncode == 0

    protocol_adapter = ProviderAdapterV1(
        replace(adapter_spec(), prompt=prompt, transport=TransportKind.JSONL)
    )
    protocol_inspection = protocol_adapter.inspect(
        protocol_adapter.resolve_binary(adapter_binary)
    )
    boundary = protocol_adapter.open_transport(
        protocol_inspection,
        "hello",
        cwd=workspace,
        provider_env={"FIXTURE_AUTH": secret},
    )
    owned = boundary._owned_transport
    assert not hasattr(boundary, "__dict__")
    assert not hasattr(boundary, "transport")
    assert secret not in repr(boundary)
    assert workspace not in repr(boundary)
    with pytest.raises(AttributeError, match="read-only"):
        boundary.transport = object()
    with pytest.raises(AttributeError, match="read-only"):
        boundary._transport = object()
    boundary.close()
    assert owned._closed is True


def test_one_shot_runtime_handles_are_atomic_across_two_threads(
    adapter_binary, tmp_path, monkeypatch
):
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    identity = ExecutableIdentity.capture(adapter_binary)
    invocation = runtime_module.BuiltPromptInvocation(
        (adapter_binary, "--exit-ok"), None, None
    )
    opened = OpenedProcessTransportV1(
        TransportKind.JSON,
        invocation,
        identity,
        {},
        (),
        None,
        OperationLimits(timeout_seconds=2),
        None,
        str(tmp_path),
    )
    entered = threading.Event()
    release = threading.Event()
    fixed_calls = []

    def fake_fixed(*args, **kwargs):
        fixed_calls.append(args[0])
        entered.set()
        assert release.wait(timeout=2)
        return object()

    monkeypatch.setattr(runtime_module, "run_fixed_process", fake_fixed)
    opened_results = []
    opened_failures = []
    rendezvous = threading.Barrier(3)

    def run_opened():
        rendezvous.wait()
        try:
            opened_results.append(opened.run())
        except BaseException as caught:
            opened_failures.append(caught)

    threads = [threading.Thread(target=run_opened) for _ in range(2)]
    for thread in threads:
        thread.start()
    rendezvous.wait()
    assert entered.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=3)
    assert len(fixed_calls) == 1
    assert len(opened_results) == 1
    assert len(opened_failures) == 1
    assert isinstance(opened_failures[0], ConfigurationError)

    temporary = owned_temporary("unified-cli-ext-auth-race-")
    workspace = os.path.realpath(temporary.name)
    session = InteractiveAuthSessionV1(
        (adapter_binary, "auth", "login"),
        identity,
        {},
        (),
        str(tmp_path / "provider-home"),
        OperationLimits(timeout_seconds=2),
        None,
        workspace,
        temporary,
    )
    entered.clear()
    release.clear()
    interactive_calls = []

    def fake_interactive(*args, **kwargs):
        interactive_calls.append(args[0])
        entered.set()
        assert release.wait(timeout=2)
        return 0

    monkeypatch.setattr(runtime_module, "_run_interactive_process", fake_interactive)
    session_results = []
    session_failures = []
    rendezvous = threading.Barrier(3)

    def run_session():
        rendezvous.wait()
        try:
            session_results.append(
                session.run(stdin=object(), stdout=object(), stderr=object())
            )
        except BaseException as caught:
            session_failures.append(caught)

    threads = [threading.Thread(target=run_session) for _ in range(2)]
    for thread in threads:
        thread.start()
    rendezvous.wait()
    assert entered.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=3)
    assert len(interactive_calls) == 1
    assert session_results == [0]
    assert len(session_failures) == 1
    assert isinstance(session_failures[0], ConfigurationError)
    assert not os.path.exists(workspace)
    assert session._temporary is None
    assert session._owned_temporary is None
    assert session._cwd is None
    os.mkdir(workspace, 0o700)
    session.close()
    assert os.path.isdir(workspace)
    os.rmdir(workspace)


def test_adapter_facade_and_registry_cannot_replace_issued_policy(adapter_binary):
    original = replace(adapter_spec(), status=AdapterStatus.HELD)
    adapter = ProviderAdapterV1(original)
    registry = ProviderAdapterRegistryV1((original,))

    assert not hasattr(adapter, "__dict__")
    with pytest.raises(AttributeError, match="read-only"):
        adapter.spec = adapter_spec()
    with pytest.raises(AttributeError, match="read-only"):
        adapter.transport = TransportKind.JSON
    with pytest.raises(FrozenInstanceError):
        adapter.spec.environment.allowed_keys = frozenset()

    exposed = adapter.spec
    assert exposed is adapter.spec
    assert exposed is not original
    assert adapter.spec.status is AdapterStatus.HELD
    assert adapter.spec.transport is TransportKind.JSONL
    with pytest.raises(ConfigurationError, match="held"):
        adapter.build_prompt(object(), "hello", {"model": "fixture-small"})

    mapped = registry.adapters
    with pytest.raises(TypeError):
        mapped["fixture-provider"] = ProviderAdapterV1(adapter_spec())
    mapped_copy = mapped["fixture-provider"].spec
    assert mapped_copy is mapped["fixture-provider"].spec
    assert registry.get("fixture-provider").spec.status is AdapterStatus.HELD

    object.__setattr__(original, "status", AdapterStatus.PREVIEW)
    assert adapter.spec.status is AdapterStatus.HELD


def test_opened_process_issued_state_tamper_fails_before_spawn(
    adapter_binary, tmp_path, monkeypatch
):
    prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    adapter = ProviderAdapterV1(
        replace(
            adapter_spec(),
            prompt=prompt,
            transport=TransportKind.JSON,
            capabilities=frozenset(("auth", "chat", "models")),
        )
    )
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    opened = adapter.open_transport(inspection, "hello", cwd=str(tmp_path))
    object.__setattr__(opened._invocation, "argv", ("/bin/false",))
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    calls = []

    def forbidden_popen(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("tampered handle spawned a subprocess")

    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)
    with pytest.raises(ConfigurationError, match="changed after issuance"):
        opened.run()
    assert calls == []


def test_interactive_auth_handle_is_read_only_and_tamper_cleans_owned_workspace(
    adapter_binary, tmp_path, monkeypatch
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("tampered auth handle spawned a subprocess")

    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)

    execution = adapter.prepare_auth_login(
        inspection, provider_home=str(tmp_path / "provider-home")
    )
    execution_root = execution._temporary.name
    assert not hasattr(execution, "__dict__")
    assert str(tmp_path) not in repr(execution)
    with pytest.raises(AttributeError, match="read-only"):
        execution._argv = ("/bin/false",)
    object.__setattr__(execution, "_argv", ("/bin/false",))
    with pytest.raises(ConfigurationError, match="changed after issuance"):
        execution.run()
    assert not os.path.exists(execution_root)

    temporary = adapter.prepare_auth_logout(
        inspection, provider_home=str(tmp_path / "provider-home")
    )
    temporary_root = temporary._temporary.name
    object.__setattr__(temporary, "_temporary", None)
    with pytest.raises(ConfigurationError, match="changed after issuance"):
        temporary.close()
    assert not os.path.exists(temporary_root)


def test_identity_bound_process_entrypoints_reject_mismatch_before_spawn(
    adapter_binary, tmp_path, monkeypatch
):
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    jsonl_module = __import__(
        "unified_cli_ext.transports.jsonl", fromlist=["unused"]
    )
    identity = ExecutableIdentity.capture(adapter_binary)
    mismatch = str(tmp_path / "different-executable")
    calls = []

    def forbidden_popen(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("mismatched executable spawned a subprocess")

    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(jsonl_module.subprocess, "Popen", forbidden_popen)

    with pytest.raises(TypeError, match="executable_identity"):
        run_fixed_process((adapter_binary, "--exit-ok"), cwd=str(tmp_path))
    with pytest.raises(ConfigurationError, match="canonical and absolute"):
        run_fixed_process(
            (os.path.basename(adapter_binary), "--exit-ok"),
            cwd=str(tmp_path),
            executable_identity=identity,
        )
    with pytest.raises(ConfigurationError, match="does not match"):
        run_fixed_process(
            (mismatch,), cwd=str(tmp_path), executable_identity=identity
        )
    with pytest.raises(ConfigurationError, match="does not match"):
        process_module._run_interactive_process(
            (mismatch,),
            timeout=1,
            cwd=str(tmp_path),
            provider_env=None,
            allowed_provider_env=(),
            persistent_home=str(tmp_path / "provider-home"),
            cancellation=None,
            executable_identity=identity,
            stdin=object(),
            stdout=object(),
            stderr=object(),
        )
    with pytest.raises(ConfigurationError, match="does not match"):
        JsonlProcess(
            (mismatch,), cwd=str(tmp_path), executable_identity=identity
        )
    with pytest.raises(ConfigurationError, match="does not match"):
        JsonRpcProcessClient(
            (mismatch,), cwd=str(tmp_path), executable_identity=identity
        )
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX retryable close contract")
def test_protocol_boundary_close_retries_reaping_and_finishes_owned_cleanup(
    adapter_binary, tmp_path, monkeypatch
):
    identity = ExecutableIdentity.capture(adapter_binary)
    process = JsonlProcess(
        (adapter_binary, "--process-hang", str(tmp_path / "retry.pid")),
        cwd=str(tmp_path),
        executable_identity=identity,
    ).start()
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    temporary = owned_temporary("unified-cli-ext-boundary-test-")
    temporary_root = temporary.name
    boundary = ProtocolLaunchBoundaryV1(
        TransportKind.JSONL,
        process,
        "",
        temporary,
        identity,
    )
    pid = process.pid
    real_reap = process._terminate_and_reap
    real_killpg = os.killpg
    attempts = []
    signal_returncodes = []

    def fail_once(proc):
        attempts.append(proc.pid)
        if len(attempts) == 1:
            raise TransportError(
                "extension subprocess could not be reaped after termination"
            )
        return real_reap(proc)

    def guarded_killpg(pgid, sig):
        assert process._proc is not None
        signal_returncodes.append(process._proc.returncode)
        assert process._proc.returncode is None
        return real_killpg(pgid, sig)

    monkeypatch.setattr(process, "_terminate_and_reap", fail_once)
    monkeypatch.setattr(os, "killpg", guarded_killpg)
    with pytest.raises(TransportError, match="reaped"):
        boundary.close()
    assert boundary._closed is False
    assert process._closed is False
    assert process._proc is not None
    assert not os.path.exists(temporary_root)

    boundary.close()
    assert len(attempts) == 2
    assert boundary._closed is True
    assert process._closed is True
    assert process._proc is None
    assert all(not thread.is_alive() for thread in process._threads)
    assert signal_returncodes
    assert pid is not None
    wait_for_exit(pid)


def test_protocol_boundary_retries_primary_and_fallback_temporary_cleanup(
    adapter_binary, tmp_path, monkeypatch
):
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    identity = ExecutableIdentity.capture(adapter_binary)
    transport = JsonlProcess(
        (adapter_binary, "--exit-ok"),
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    temporary = owned_temporary("unified-cli-ext-boundary-retry-")
    temporary_root = os.path.realpath(temporary.name)
    real_cleanup = runtime_module._OwnedTemporaryDirectory.cleanup
    primary_attempts = []

    def fail_primary_once(owner):
        if owner is temporary and not primary_attempts:
            primary_attempts.append(True)
            raise OSError("injected boundary primary cleanup failure")
        return real_cleanup(owner)

    monkeypatch.setattr(
        runtime_module._OwnedTemporaryDirectory, "cleanup", fail_primary_once
    )
    boundary = ProtocolLaunchBoundaryV1(
        TransportKind.JSONL, transport, "", temporary, identity
    )

    with pytest.raises(OSError, match="boundary primary cleanup failure"):
        boundary.close()
    assert boundary._closed is False
    assert boundary._owned_temporary is temporary
    assert boundary._temporary_cleaned is False
    assert os.path.exists(temporary_root)

    boundary.close()
    assert boundary._closed is True
    assert boundary._temporary_cleaned is True
    assert boundary._temporary is None
    assert boundary._owned_temporary is None
    assert not os.path.exists(temporary_root)
    os.mkdir(temporary_root, 0o700)
    boundary.close()
    assert os.path.isdir(temporary_root)
    os.rmdir(temporary_root)


def test_pending_temporary_owner_never_removes_replacement_path(
    adapter_binary, tmp_path, monkeypatch
):
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    identity = ExecutableIdentity.capture(adapter_binary)
    transport = JsonlProcess(
        (adapter_binary, "--exit-ok"),
        cwd=str(tmp_path),
        executable_identity=identity,
    )
    temporary = owned_temporary("unified-cli-ext-pending-owner-")
    original_path = temporary.name
    moved_path = original_path + "-moved"
    owner_type = runtime_module._OwnedTemporaryDirectory
    real_cleanup = owner_type.cleanup
    attempts = []

    def fail_once(owner):
        if owner is temporary and not attempts:
            attempts.append(True)
            raise OSError("injected pending temporary cleanup")
        return real_cleanup(owner)

    monkeypatch.setattr(owner_type, "cleanup", fail_once)
    boundary = ProtocolLaunchBoundaryV1(
        TransportKind.JSONL, transport, "", temporary, identity
    )
    with pytest.raises(OSError, match="pending temporary"):
        boundary.close()

    os.rename(original_path, moved_path)
    os.mkdir(original_path, 0o700)
    boundary.close()
    assert boundary._closed is True
    assert os.path.isdir(original_path)
    shutil.rmtree(original_path)
    shutil.rmtree(moved_path)


def test_actual_chat_requires_canonical_existing_workspace(adapter_binary, tmp_path):
    prompt = PromptCommandSpec(
        ("chat",),
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
    )
    adapter = ProviderAdapterV1(
        replace(
            adapter_spec(),
            prompt=prompt,
            transport=TransportKind.JSON,
            capabilities=frozenset(("auth", "chat", "models")),
        )
    )
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    with pytest.raises(ConfigurationError, match="absolute"):
        adapter.open_transport(inspection, "hello", cwd="relative/project")
    with pytest.raises(ConfigurationError, match="absolute"):
        adapter.open_transport(inspection, "hello")
    missing = tmp_path / "missing"
    with pytest.raises(ConfigurationError, match="existing directory"):
        adapter.open_transport(inspection, "hello", cwd=str(missing))
    file_path = tmp_path / "file"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="directory"):
        adapter.open_transport(inspection, "hello", cwd=str(file_path))
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlink"):
        adapter.open_transport(inspection, "hello", cwd=str(linked))
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "project").mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlink"):
        adapter.open_transport(
            inspection,
            "hello",
            cwd=str(linked_parent / "project"),
        )
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_workspace = unsafe_parent / "project"
    unsafe_workspace.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    try:
        with pytest.raises(ConfigurationError, match="secure parent chain"):
            adapter.open_transport(
                inspection,
                "hello",
                cwd=str(unsafe_workspace),
            )
    finally:
        unsafe_parent.chmod(0o700)
    with pytest.raises(ConfigurationError, match="explicit provider workspace"):
        run_fixed_process(
            (adapter_binary, "--exit-ok"),
            executable_identity=ExecutableIdentity.capture(adapter_binary),
        )


def test_capability_invariants_are_bidirectional_and_transport_aware():
    spec = adapter_spec()
    with pytest.raises(ConfigurationError, match="chat"):
        replace(spec, capabilities=frozenset(("auth", "models")))
    with pytest.raises(ConfigurationError, match="auth specification"):
        replace(spec, auth=None)
    with pytest.raises(ConfigurationError, match="auth specification"):
        replace(spec, capabilities=frozenset(("chat", "models", "sessions")))
    with pytest.raises(ConfigurationError, match="model probe"):
        replace(spec, models=None)
    with pytest.raises(ConfigurationError, match="model probe"):
        replace(spec, capabilities=frozenset(("auth", "chat", "sessions")))
    with pytest.raises(ConfigurationError, match="streaming"):
        replace(
            spec,
            transport=TransportKind.JSON,
            capabilities=spec.capabilities | frozenset(("stream",)),
        )
    with pytest.raises(ConfigurationError, match="tools"):
        replace(
            spec,
            capabilities=spec.capabilities | frozenset(("permissions",)),
            permission_policy=PermissionPolicy(),
        )
    with pytest.raises(ConfigurationError, match="default-deny"):
        replace(
            spec,
            capabilities=spec.capabilities | frozenset(("permissions", "tools")),
        )
    with pytest.raises(ConfigurationError, match="runtime binds"):
        replace(
            spec,
            capabilities=spec.capabilities | frozenset(("permissions", "tools")),
            permission_policy=PermissionPolicy(),
        )
    with pytest.raises(ConfigurationError, match="structured"):
        replace(spec, transport=TransportKind.PLAIN)
    with pytest.raises(ConfigurationError, match="structured"):
        replace(
            spec,
            transport=TransportKind.PLAIN,
            capabilities=spec.capabilities | frozenset(("images",)),
        )
    for capability in ("tools", "reasoning_summaries"):
        with pytest.raises(ConfigurationError, match="structured"):
            replace(
                spec,
                transport=TransportKind.PLAIN,
                capabilities=frozenset(("auth", "chat", "models", capability)),
            )
    with pytest.raises(ConfigurationError, match="JSON-RPC, ACP"):
        replace(spec, capabilities=spec.capabilities | frozenset(("mcp",)))
    with pytest.raises(ConfigurationError, match="feature probe evidence"):
        replace(
            spec,
            binary=replace(
                spec.binary,
                feature_probe=replace(
                    spec.binary.feature_probe,
                    required_features=frozenset(("auth", "chat", "models")),
                ),
            ),
        )

    protocol = PromptCommandSpec(("chat",), mode=PromptMode.PROTOCOL)
    http_mcp = replace(
        spec,
        binary=replace(
            spec.binary,
            feature_probe=replace(
                spec.binary.feature_probe,
                required_features=spec.binary.feature_probe.required_features
                | frozenset(("mcp",)),
            ),
        ),
        prompt=protocol,
        transport=TransportKind.HTTP_JSON,
        capabilities=spec.capabilities | frozenset(("mcp",)),
    )
    assert "mcp" in http_mcp.capabilities


def test_prompt_modes_are_compatible_with_transport_role():
    spec = adapter_spec()
    protocol = PromptCommandSpec(("chat",), mode=PromptMode.PROTOCOL)
    stdin_prompt = PromptCommandSpec(("chat",), mode=PromptMode.STDIN)
    with pytest.raises(ConfigurationError, match="protocol transport"):
        replace(
            spec,
            prompt=protocol,
            transport=TransportKind.PLAIN,
            capabilities=frozenset(("auth", "chat", "models")),
        )
    with pytest.raises(ConfigurationError, match="process transport"):
        replace(spec, prompt=stdin_prompt, transport=TransportKind.ACP)
    with pytest.raises(ConfigurationError, match="collide"):
        replace(spec, prompt=stdin_prompt, transport=TransportKind.JSON_RPC)


def test_held_adapter_cannot_build_or_open(adapter_binary):
    normal = ProviderAdapterV1(adapter_spec())
    binary = normal.resolve_binary(adapter_binary)
    inspection = normal.inspect(binary)
    held = ProviderAdapterV1(replace(adapter_spec(), status=AdapterStatus.HELD))

    with pytest.raises(ConfigurationError, match="held"):
        held.build_prompt(binary, "hello", {"model": "fixture-small"})
    with pytest.raises(ConfigurationError, match="held"):
        held.open_transport(inspection, "hello", {"model": "fixture-small"})
def test_cached_inspection_is_shared_and_force_refresh_retires_it(adapter_binary):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    first = adapter.inspect(binary)
    second = adapter.inspect(binary)

    assert first is second
    assert adapter.doctor_provider(first) is True
    refreshed = adapter.inspect(binary, force_refresh=True)
    with pytest.raises(ConfigurationError, match="inspection"):
        adapter.doctor_provider(first)
    assert adapter.doctor_provider(refreshed) is True
    assert not hasattr(adapter, "_inspections")


def test_provider_import_is_true_cold_and_side_effect_free():
    root = Path(__file__).resolve().parents[3]
    source_path = os.pathsep.join(
        (str(root / "src"), str(root / "packages" / "unified-cli-ext" / "src"))
    )
    script = r'''
import builtins
import subprocess
import sys
import typing
seen = []
original = builtins.__import__
def capture(name, *args, **kwargs):
    seen.append(name)
    if name == "unified_cli.plugin":
        raise AssertionError("cold import reached Core plugin ABI")
    return original(name, *args, **kwargs)
builtins.__import__ = capture
subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe spawned"))
import unified_cli_ext.providers
assert "unified_cli.plugin" not in seen
assert "unified_cli.plugin" not in sys.modules
assert unified_cli_ext.providers.ProviderAdapterRegistryV1().descriptors() == ()
assert "unified_cli_ext.providers.runtime" not in sys.modules
assert "unified_cli_ext.providers.installation" not in sys.modules
assert "unified_cli_ext.providers.bridge" not in sys.modules
register_hints = typing.get_type_hints(
    unified_cli_ext.providers.ProviderAdapterRegistryV1.register
)
get_hints = typing.get_type_hints(
    unified_cli_ext.providers.ProviderAdapterRegistryV1.get
)
adapters_hints = typing.get_type_hints(
    unified_cli_ext.providers.ProviderAdapterRegistryV1.adapters.fget
)
runtime_type = sys.modules[
    "unified_cli_ext.providers.runtime"
].ProviderAdapterV1
assert register_hints["return"] is runtime_type
assert runtime_type in typing.get_args(get_hints["return"])
assert typing.get_args(adapters_hints["return"])[1] is runtime_type
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = source_path
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(root),
        env=environment,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_adapter_probe_cache_hit_force_expiry_invalidation_and_immutable(
    adapter_binary, monkeypatch,
):
    runtime_module = __import__(
        "unified_cli_ext.providers.runtime", fromlist=["unused"]
    )
    real_run = ProviderAdapterV1._run_probe
    probes = []

    def counted(self, binary, probe, **kwargs):
        probes.append(probe)
        return real_run(self, binary, probe, **kwargs)

    monkeypatch.setattr(ProviderAdapterV1, "_run_probe", counted)
    adapter = ProviderAdapterV1(adapter_spec())
    assert not adapter._cache
    binary = adapter.resolve_binary(adapter_binary)

    first = adapter.inspect(binary)
    assert len(probes) == 2
    second = adapter.inspect(binary)
    assert len(probes) == 2
    assert second is first
    inspection = adapter.inspect(binary, force_refresh=True)
    assert len(probes) == 4

    models = adapter.list_models(inspection)
    assert models == ("fixture-small", "fixture-large")
    assert adapter.list_models(inspection) == models
    assert len(probes) == 5
    with pytest.raises(TypeError):
        models[0] = "mutated"

    adapter.list_models(inspection, force_refresh=True)
    assert len(probes) == 6
    adapter.invalidate_cache("models")
    adapter.list_models(inspection)
    assert len(probes) == 7

    with adapter._cache_lock:
        for key, (_expires_at, value) in tuple(adapter._cache.items()):
            if key[0] == "models":
                adapter._cache[key] = (runtime_module.time.monotonic() - 1, value)
    adapter.list_models(inspection)
    assert len(probes) == 8

    wall = [10_000.0]
    monkeypatch.setattr(runtime_module.time, "time", lambda: wall[0])
    adapter.list_models(inspection)
    wall[0] = -10_000.0
    adapter.list_models(inspection)
    assert len(probes) == 8


def test_adapter_model_cache_single_flight_owner_failure_wakes_waiters(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    real_run = ProviderAdapterV1._run_probe
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(8)
    calls = []

    def failed(self, binary, probe, **kwargs):
        if probe is self.spec.models.probe:
            calls.append(True)
            entered.set()
            assert release.wait(2)
            raise RuntimeError("injected model probe failure")
        return real_run(self, binary, probe, **kwargs)

    monkeypatch.setattr(ProviderAdapterV1, "_run_probe", failed)

    def listing():
        start.wait(timeout=2)
        return adapter.list_models(inspection, force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(listing) for _ in range(8)]
        assert entered.wait(1)
        assert not release.wait(0.05)
        release.set()
        for future in futures:
            with pytest.raises(RuntimeError, match="injected model probe failure"):
                future.result(timeout=2)
    assert len(calls) == 1

    monkeypatch.setattr(ProviderAdapterV1, "_run_probe", real_run)
    assert adapter.list_models(inspection) == ("fixture-small", "fixture-large")


def test_adapter_inspect_force_refresh_is_eight_way_usable_single_flight(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    real_run = ProviderAdapterV1._run_probe
    start = threading.Barrier(8)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []

    def counted(self, binary_value, probe, **kwargs):
        with lock:
            calls.append(probe)
            first = len(calls) == 1
        if first:
            entered.set()
            assert release.wait(2)
        return real_run(self, binary_value, probe, **kwargs)

    monkeypatch.setattr(ProviderAdapterV1, "_run_probe", counted)

    def inspect():
        start.wait(timeout=2)
        return adapter.inspect(binary, force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(inspect) for _ in range(8)]
        assert entered.wait(1)
        assert not release.wait(0.05)
        release.set()
        inspections = [future.result(timeout=3) for future in futures]
    assert len(calls) == 2
    assert all(value is inspections[0] for value in inspections)
    assert adapter.doctor_provider(inspections[0]) is True


def test_stale_inspect_completion_cannot_retire_force_refreshed_inspection(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    original = adapter.inspect(binary)
    real_activate = ProviderAdapterV1._activate_inspection
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    delay_next = [True]

    def delayed(self, record, *, context):
        with lock:
            should_delay = delay_next[0]
            delay_next[0] = False
        if should_delay:
            entered.set()
            assert release.wait(2)
        return real_activate(self, record, context=context)

    monkeypatch.setattr(ProviderAdapterV1, "_activate_inspection", delayed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(adapter.inspect, binary)
        assert entered.wait(1)
        refreshed = adapter.inspect(binary, force_refresh=True)
        assert refreshed is not original
        release.set()
        assert stale.result(timeout=2) is refreshed
    assert adapter.doctor_provider(refreshed) is True


def test_cancellation_aware_inspect_does_not_reuse_existing_cached_record(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    cached = adapter.inspect(binary)
    cancellation = CancellationToken()

    def inspected(self, binary_value, **kwargs):
        assert self is adapter
        assert binary_value is binary
        assert kwargs["cancellation"] is cancellation
        return AdapterInspectionV1(
            id=cached.id,
            version="2.4.2",
            features=cached.features,
            binary=binary,
            abi_version=cached.abi_version,
        )

    monkeypatch.setattr(ProviderAdapterV1, "_inspect_uncached", inspected)
    refreshed = adapter.inspect(binary, cancellation=cancellation)
    assert refreshed is not cached
    assert refreshed.version == "2.4.2"
    assert adapter.doctor_provider(refreshed) is True


def test_adapter_invalidation_fences_old_model_flight_and_cleanup(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    lock = threading.Lock()
    calls = []

    def listing(self, inspection_value, **_kwargs):
        assert inspection_value is inspection
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return ("generation-{}".format(index),)

    monkeypatch.setattr(ProviderAdapterV1, "_list_models_uncached", listing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(adapter.list_models, inspection)
        assert entered[0].wait(1)
        adapter.invalidate_cache("models")
        replacement = pool.submit(adapter.list_models, inspection)
        assert entered[1].wait(1)
        joined = pool.submit(adapter.list_models, inspection)
        release[0].set()
        assert old.result(timeout=1) == ("generation-0",)
        assert not replacement.done()
        release[1].set()
        assert replacement.result(timeout=1) == ("generation-1",)
        assert joined.result(timeout=1) == ("generation-1",)
    assert len(calls) == 2
    assert adapter.list_models(inspection) == ("generation-1",)


def test_adapter_force_refresh_fences_ordinary_flight_and_force_callers_share(
    adapter_binary, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    start = threading.Barrier(8)
    lock = threading.Lock()
    calls = []

    def listing(self, inspection_value, **_kwargs):
        assert inspection_value is inspection
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return ("refresh-{}".format(index),)

    monkeypatch.setattr(ProviderAdapterV1, "_list_models_uncached", listing)

    def forced():
        start.wait(timeout=2)
        return adapter.list_models(inspection, force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        old = pool.submit(adapter.list_models, inspection)
        assert entered[0].wait(1)
        forced_calls = [pool.submit(forced) for _ in range(8)]
        assert entered[1].wait(1)
        assert not release[1].wait(0.05)
        assert len(calls) == 2
        release[0].set()
        release[1].set()
        assert old.result(timeout=1) == ("refresh-0",)
        assert {
            future.result(timeout=1) for future in forced_calls
        } == {("refresh-1",)}
    assert adapter.list_models(inspection) == ("refresh-1",)


def test_adapter_empty_model_results_are_not_cached(adapter_binary, monkeypatch):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    calls = []

    def listing(self, inspection_value, **_kwargs):
        assert inspection_value is inspection
        calls.append(True)
        return () if len(calls) == 1 else ("available",)

    monkeypatch.setattr(ProviderAdapterV1, "_list_models_uncached", listing)
    assert adapter.list_models(inspection) == ()
    assert adapter.list_models(inspection) == ("available",)
    assert len(calls) == 2


def test_adapter_auth_cache_isolated_by_home_and_selected_environment(
    adapter_binary, tmp_path, monkeypatch,
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    real_run = ProviderAdapterV1._run_probe
    calls = []

    def counted(self, binary, probe, **kwargs):
        if probe is self.spec.auth.status_probe:
            calls.append(True)
        return real_run(self, binary, probe, **kwargs)

    monkeypatch.setattr(ProviderAdapterV1, "_run_probe", counted)
    first_home = str(tmp_path / "first-home")
    second_home = str(tmp_path / "second-home")
    assert adapter.authenticated(
        inspection,
        provider_home=first_home,
        provider_env={"FIXTURE_AUTH": "ready", "IGNORED_SECRET": "one"},
    ) is True
    assert adapter.authenticated(
        inspection,
        provider_home=first_home,
        provider_env={"FIXTURE_AUTH": "ready", "IGNORED_SECRET": "two"},
    ) is True
    assert len(calls) == 1
    assert adapter.authenticated(
        inspection,
        provider_home=second_home,
        provider_env={"FIXTURE_AUTH": "ready"},
    ) is True
    assert adapter.authenticated(
        inspection,
        provider_home=first_home,
        provider_env={},
    ) is False
    assert len(calls) == 3


def test_adapter_binary_replacement_rejects_old_and_drops_probe_cache(
    adapter_binary,
):
    adapter = ProviderAdapterV1(adapter_spec())
    binary = adapter.resolve_binary(adapter_binary)
    inspection = adapter.inspect(binary)
    adapter.list_models(inspection)
    assert any(key[0] == "models" for key in adapter._cache)

    with open(adapter_binary, "ab") as stream:
        stream.write(b"\n# stage7 replacement\n")
    with pytest.raises(ConfigurationError, match="provenance changed"):
        adapter.list_models(inspection)

    replacement = adapter.resolve_binary(adapter_binary)
    assert not any(key[0] == "models" for key in adapter._cache)
    replacement_inspection = adapter.inspect(replacement)
    assert adapter.list_models(replacement_inspection) == (
        "fixture-small", "fixture-large"
    )


def test_adapter_auth_paths_invalidate_cached_account_state(
    adapter_binary, tmp_path,
):
    adapter = ProviderAdapterV1(adapter_spec())
    inspection = adapter.inspect(adapter.resolve_binary(adapter_binary))
    provider_home = str(tmp_path / "provider-home")
    assert adapter.authenticated(inspection, provider_home=provider_home) is False
    assert adapter.authenticated(inspection, provider_home=provider_home) is False

    login, _ = run_auth_tty(
        adapter.prepare_auth_login(inspection, provider_home=provider_home)
    )
    assert login == 0
    assert adapter.authenticated(inspection, provider_home=provider_home) is True

    logout, _ = run_auth_tty(
        adapter.prepare_auth_logout(inspection, provider_home=provider_home)
    )
    assert logout == 0
    assert adapter.authenticated(inspection, provider_home=provider_home) is False
