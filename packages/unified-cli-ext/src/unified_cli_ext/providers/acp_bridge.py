"""Fail-closed Core bridge for verified ACP 0.11 stdio providers."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Callable, Iterator, Optional, Tuple

from unified_cli.base import BaseProvider
from unified_cli.core import Message, ModelInfo, Response
from unified_cli.errors import UnifiedError
from unified_cli.plugin import (
    PROVIDER_CONFIGURATION_ABI_V1,
    BoundProviderOperationsV1,
    ProviderCreateRequestV1,
    ProviderLaunchContextV1,
    ProviderPluginV1,
    ProviderServerPolicyV1,
)

from ..errors import ConfigurationError, ExtensionError, ProtocolError
from ..transports.acp import AcpProcessTransportV1
from ..transports.security import (
    CancellationToken,
    TransportLimits,
    private_persistent_home,
    validated_workspace,
)
from .bridge import (
    _CancellationRelay,
    _TurnState,
    _core_error,
    _effective_limits,
    _validated_model_id,
    installation_receipt_envelope,
    installation_receipt_from_envelope,
)
from .contract import (
    AdapterStatus,
    OperationLimits,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
)
from .held import held_plugin
from .installation import InstallationReceiptV1
from .runtime import AdapterInspectionV1, BinaryProvenance, ProviderAdapterV1


AcpLaunchResolverV1 = Callable[[], InstallationReceiptV1]
AcpHomePreparerV1 = Callable[[str], None]
AcpWorkspaceGuardV1 = Callable[[str], None]

_INTERNAL_CAPABILITIES = frozenset(
    (ProviderCapability.CHAT.value, ProviderCapability.SESSIONS.value)
)


def _ensure_private_directory(path: str) -> None:
    """Create one owner-only directory without following a final symlink."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except OSError:
            raise ConfigurationError("provider config directory is unavailable") from None
        metadata = os.lstat(path)
    except OSError:
        raise ConfigurationError("provider config directory is unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        raise ConfigurationError("provider config directory is not private")


def write_private_json(path: str, value: Mapping[str, object]) -> None:
    """Atomically replace a bounded provider-owned JSON configuration file."""

    if type(path) is not str or not os.path.isabs(path):
        raise ConfigurationError("provider config path is invalid")
    parent = os.path.dirname(path)
    _ensure_private_directory(parent)
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ConfigurationError("provider config exceeds its limit")
    temporary = os.path.join(parent, ".unified-cli.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ConfigurationError("provider config file is not private")
    except ConfigurationError:
        raise
    except OSError:
        raise ConfigurationError("provider config file is unavailable") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def reject_workspace_config(workspace: str, names: Tuple[str, ...]) -> None:
    """Reject provider config roots that could widen the closed ACP policy."""

    root = validated_workspace(workspace)
    for name in names:
        if (
            type(name) is not str
            or not name
            or os.path.basename(name) != name
            or name in (".", "..")
        ):
            raise ConfigurationError("provider workspace guard is invalid")
        candidate = os.path.join(root, name)
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise ConfigurationError("provider workspace config could not be inspected") from None
        raise ConfigurationError("provider workspace contains an unsafe provider config")


def reject_provider_home_config(home: str, paths: Tuple[Tuple[str, ...], ...]) -> None:
    """Reject documented provider policy roots inside a private provider HOME."""

    root = private_persistent_home(home)
    for parts in paths:
        if (
            not parts
            or any(
                type(part) is not str
                or not part
                or os.path.basename(part) != part
                or part in (".", "..")
                for part in parts
            )
        ):
            raise ConfigurationError("provider home guard is invalid")
        current = root
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError:
                raise ConfigurationError(
                    "provider home config could not be inspected"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ConfigurationError("provider home config must not use symlinks")
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ConfigurationError("provider home config path is invalid")
            if index == len(parts) - 1:
                raise ConfigurationError(
                    "provider home contains an unsafe provider config"
                )


def _run_provider_guards(
    *,
    workspace: Optional[str],
    provider_home: str,
    workspace_guard: Optional[AcpWorkspaceGuardV1],
    home_preparer: Optional[AcpHomePreparerV1],
) -> None:
    """Run every provider policy guard before any probe or prompt launch."""

    if workspace is not None and workspace_guard is not None:
        workspace_guard(workspace)
    if home_preparer is not None:
        home_preparer(provider_home)


class AcpProviderBridge(BaseProvider):
    """A single-turn Core provider over one verified ACP 0.11 subprocess."""

    name = "extension-acp"
    default_model = "default"
    api_key_env = ""
    allow_api_key_fallback = False

    def __init__(
        self,
        *,
        spec: ProviderAdapterSpecV1,
        adapter: ProviderAdapterV1,
        inspection: AdapterInspectionV1,
        binary: BinaryProvenance,
        default_model: str,
        model: Optional[str],
        cwd: str,
        provider_env: Mapping[str, str],
        provider_home: str,
        limits: OperationLimits,
        home_preparer: Optional[AcpHomePreparerV1],
        workspace_guard: Optional[AcpWorkspaceGuardV1],
    ) -> None:
        if spec.transport is not TransportKind.ACP or spec.prompt.mode is not PromptMode.PROTOCOL:
            raise ConfigurationError("ACP bridge requires one protocol-mode ACP adapter")
        issued_model = _validated_model_id(default_model)
        selected_model = _validated_model_id(issued_model if model is None else model)
        if selected_model != issued_model:
            raise ConfigurationError("ACP bridge does not expose model selection")
        self.name = spec.id
        self.default_model = issued_model
        super().__init__(
            model=selected_model,
            cwd=validated_workspace(cwd),
            bin_path=binary.real_path,
            extra_env={},
            timeout=limits.timeout_seconds,
            first_output_timeout=limits.timeout_seconds,
            web_search=False,
            max_output_bytes=limits.max_stdout_bytes,
            max_stderr_bytes=limits.max_stderr_bytes,
            max_stream_buffer_bytes=limits.max_stdout_bytes,
            max_stream_events=limits.max_events,
            max_stream_line_bytes=min(limits.max_stdout_bytes, 1024 * 1024),
        )
        self._spec = spec
        self._adapter = adapter
        self._inspection = inspection
        self._binary = binary
        self._provider_env = MappingProxyType(dict(provider_env))
        self._provider_home = private_persistent_home(provider_home)
        self._limits = limits
        self._home_preparer = home_preparer
        self._workspace_guard = workspace_guard

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return None

    @classmethod
    def _install_hint(cls) -> str:
        return "Supply an explicit verified provider installation."

    def _build_args(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise ConfigurationError("ACP bridge argv are runtime-owned")

    def _normalize(self, obj: dict) -> Iterator[Message]:
        del obj
        raise ConfigurationError("ACP normalization is transport-owned")

    def _parse_json_response(self, text: str, model: str) -> Response:
        del text, model
        raise ConfigurationError("ACP response parsing is transport-owned")

    def _env(self, fallback_api_key: bool = False) -> dict:
        del fallback_api_key
        return dict(self._provider_env)

    def _validate_call(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        images: Optional[list],
    ) -> str:
        if type(prompt) is not str or not prompt.strip():
            raise ConfigurationError("provider prompt must not be empty")
        if session_id is not None or resume_last:
            raise ConfigurationError("ACP bridge does not expose session resume")
        if images:
            raise ConfigurationError("ACP bridge does not expose image input")
        selected = _validated_model_id(self.model if model is None else model)
        if selected != self.default_model or _validated_model_id(self.model) != self.default_model:
            raise ConfigurationError("ACP bridge does not expose model selection")
        return selected

    def _prepare(self) -> None:
        _run_provider_guards(
            workspace=self.cwd,
            provider_home=self._provider_home,
            workspace_guard=self._workspace_guard,
            home_preparer=self._home_preparer,
        )
        self._binary.verify(self._spec.binary.executable)
        current = self._adapter.inspect(
            self._binary,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
        )
        if current != self._inspection:
            raise ConfigurationError("ACP provider inspection changed after issuance")

    async def _turn_events(
        self, prompt: str, token: CancellationToken
    ) -> Tuple[object, ...]:
        self._prepare()
        identities = self._binary.spawn_identities()
        if not identities:
            raise ConfigurationError("ACP launch identity is unavailable")
        transport = AcpProcessTransportV1(
            self._binary.argv_prefix + self._spec.prompt.fixed_argv,
            executable_identity=identities[0],
            launch_identities=identities,
            cwd=self.cwd,
            provider_namespace=self._spec.id,
            provider_env=self._provider_env,
            allowed_provider_env=self._spec.environment.allowed_keys,
            persistent_home=self._provider_home,
            limits=TransportLimits(
                max_line_bytes=min(self._limits.max_stdout_bytes, 1024 * 1024),
                max_output_bytes=self._limits.max_stdout_bytes,
                max_stderr_bytes=self._limits.max_stderr_bytes,
                max_events=self._limits.max_events,
            ),
            timeout=self._limits.timeout_seconds,
            cancellation=token,
        )
        return await transport.text_turn(prompt)

    def _turn_state(self, events: Tuple[object, ...]) -> _TurnState:
        turn = _TurnState(
            self._spec.id,
            _INTERNAL_CAPABILITIES,
            {},
            None,
            self._limits.max_events,
            self._limits.max_stdout_bytes,
        )
        for event in events:
            message = turn._accept_event(event)
            if message is not None:
                turn.messages.append(message)
        turn.finish()
        return turn

    async def _achat_impl(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        images: Optional[list],
        cancel_event: Optional[threading.Event],
    ) -> Response:
        selected = self._validate_call(
            prompt,
            session_id=session_id,
            resume_last=resume_last,
            model=model,
            images=images,
        )
        token = CancellationToken()
        relay = _CancellationRelay(cancel_event, token)
        try:
            events = await self._turn_events(prompt, token)
            token.raise_if_cancelled()
            return self._turn_state(events).response(selected)
        finally:
            relay.close()

    def chat(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Response:
        try:
            return asyncio.run(
                self._achat_impl(
                    prompt,
                    session_id=session_id,
                    resume_last=resume_last,
                    model=model,
                    images=images,
                    cancel_event=cancel_event,
                )
            )
        except ExtensionError as error:
            raise _core_error(self._spec.id, error) from None

    def stream(self, prompt: str, **kwargs: Any) -> Iterator[Message]:
        response = self.chat(prompt, **kwargs)
        yield from response.messages

    async def achat(self, prompt: str, **kwargs: Any) -> Response:
        try:
            unsupported = set(kwargs).difference(
                {
                    "session_id",
                    "resume_last",
                    "model",
                    "images",
                    "cancel_event",
                }
            )
            if unsupported:
                raise ConfigurationError(
                    "ACP provider received unsupported arguments"
                )
            session_id = kwargs.pop("session_id", None)
            resume_last = kwargs.pop("resume_last", False)
            model = kwargs.pop("model", None)
            images = kwargs.pop("images", None)
            cancel_event = kwargs.pop("cancel_event", None)
            return await self._achat_impl(
                prompt,
                session_id=session_id,
                resume_last=resume_last,
                model=model,
                images=images,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            raise
        except ExtensionError as error:
            raise _core_error(self._spec.id, error) from None

    async def astream(self, prompt: str, **kwargs: Any) -> Any:
        response = await self.achat(prompt, **kwargs)
        for message in response.messages:
            yield message


class _AcpPluginRuntime:
    def __init__(
        self,
        spec: ProviderAdapterSpecV1,
        default_model: str,
        launch_resolver: Optional[AcpLaunchResolverV1],
        home_preparer: Optional[AcpHomePreparerV1],
        workspace_guard: Optional[AcpWorkspaceGuardV1],
    ) -> None:
        self.spec = spec
        self.default_model = default_model
        self.launch_resolver = launch_resolver
        self.home_preparer = home_preparer
        self.workspace_guard = workspace_guard

    def _guard_boundaries(
        self, workspace: Optional[str], provider_home: str
    ) -> None:
        _run_provider_guards(
            workspace=workspace,
            provider_home=provider_home,
            workspace_guard=self.workspace_guard,
            home_preparer=self.home_preparer,
        )

    def _candidate(
        self,
        bin_path: Optional[str],
        receipt: Optional[InstallationReceiptV1],
    ) -> InstallationReceiptV1:
        if bin_path is not None and receipt is not None:
            raise ConfigurationError("bin_path and installation receipt are mutually exclusive")
        if bin_path is not None:
            return InstallationReceiptV1.capture_explicit_direct(
                provider_id=self.spec.id,
                executable_path=bin_path,
                executable_basename=self.spec.binary.executable,
            )
        if receipt is not None:
            if type(receipt) is not InstallationReceiptV1:
                raise ConfigurationError("provider installation receipt is invalid")
            return receipt
        if self.launch_resolver is None:
            raise ConfigurationError(
                "provider requires an explicit canonical bin_path or installation receipt"
            )
        candidate = self.launch_resolver()
        if type(candidate) is not InstallationReceiptV1:
            raise ConfigurationError("provider resolver returned an invalid receipt")
        return candidate

    def _gate(
        self,
        receipt: InstallationReceiptV1,
        provider_env: Mapping[str, str],
        provider_home: str,
    ) -> Tuple[ProviderAdapterV1, BinaryProvenance, AdapterInspectionV1]:
        self._guard_boundaries(None, provider_home)
        adapter = ProviderAdapterV1(self.spec)
        binary = adapter.resolve_installation(receipt)
        inspection = adapter.inspect(
            binary, provider_env=provider_env, provider_home=provider_home
        )
        if not adapter.doctor_provider(
            inspection, provider_env=provider_env, provider_home=provider_home
        ):
            raise ConfigurationError("provider doctor reported unavailable")
        return adapter, binary, inspection

    def factory(
        self,
        *,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        bin_path: Optional[str] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        first_output_timeout: Optional[float] = None,
        web_search: bool = False,
        max_output_bytes: Optional[int] = None,
        max_stderr_bytes: Optional[int] = None,
        max_stream_buffer_bytes: Optional[int] = None,
        max_stream_events: Optional[int] = None,
        max_stream_line_bytes: Optional[int] = None,
        receipt: Optional[InstallationReceiptV1] = None,
        provider_home: Optional[str] = None,
        **unknown: Any,
    ) -> AcpProviderBridge:
        if unknown or first_output_timeout is not None or web_search:
            raise ConfigurationError("ACP provider factory received unsupported options")
        if type(cwd) is not str:
            raise ConfigurationError("ACP provider factory requires an explicit absolute cwd")
        if type(provider_home) is not str:
            raise ConfigurationError("ACP provider requires an explicit private provider_home")
        selected_model = _validated_model_id(
            self.default_model if model is None else model
        )
        if selected_model != self.default_model:
            raise ConfigurationError("ACP bridge does not expose model selection")
        workspace = validated_workspace(cwd)
        home = private_persistent_home(provider_home)
        self._guard_boundaries(workspace, home)
        selected_env = self.spec.environment.select(extra_env)
        limits = _effective_limits(
            self.spec,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            max_stderr_bytes=max_stderr_bytes,
            max_stream_buffer_bytes=max_stream_buffer_bytes,
            max_stream_events=max_stream_events,
            max_stream_line_bytes=max_stream_line_bytes,
        )
        candidate = self._candidate(bin_path, receipt)
        adapter, binary, inspection = self._gate(candidate, selected_env, home)
        return AcpProviderBridge(
            spec=self.spec,
            adapter=adapter,
            inspection=inspection,
            binary=binary,
            default_model=self.default_model,
            model=model,
            cwd=workspace,
            provider_env=selected_env,
            provider_home=home,
            limits=limits,
            home_preparer=self.home_preparer,
            workspace_guard=self.workspace_guard,
        )

    def models(self) -> Tuple[ModelInfo, ...]:
        return (
            ModelInfo(
                id=self.default_model,
                provider=self.spec.id,
                default=True,
                source="plugin",
            ),
        )

    def _doctor(
        self,
        receipt: InstallationReceiptV1,
        provider_env: Mapping[str, str],
        provider_home: str,
    ) -> Mapping[str, object]:
        try:
            _adapter, _binary, inspection = self._gate(
                receipt, provider_env, provider_home
            )
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except ExtensionError:
            return MappingProxyType(
                {
                    "id": self.spec.id,
                    "available": False,
                    "status": self.spec.status.value,
                }
            )
        return MappingProxyType(
            {
                "id": self.spec.id,
                "available": True,
                "status": self.spec.status.value,
                "version": inspection.version,
            }
        )

    def doctor(self) -> Mapping[str, object]:
        try:
            receipt = self._candidate(None, None)
            home = private_persistent_home(
                os.path.join(os.path.expanduser("~"), ".unified-cli", self.spec.id)
            )
            return self._doctor(receipt, self.spec.environment.select({}), home)
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except ExtensionError:
            return MappingProxyType(
                {
                    "id": self.spec.id,
                    "available": False,
                    "status": self.spec.status.value,
                }
            )

    def bind(self, context: ProviderLaunchContextV1) -> BoundProviderOperationsV1:
        if context.provider_id != self.spec.id or context.provider_home is None:
            raise ConfigurationError("ACP launch context is incomplete")
        receipt = self._candidate(
            context.bin_path,
            None
            if context.receipt is None
            else installation_receipt_from_envelope(context.receipt),
        )
        receipt.verify()
        selected_env = self.spec.environment.select(context.provider_env)
        home = private_persistent_home(context.provider_home)
        self._guard_boundaries(None, home)

        def create_bound(request: ProviderCreateRequestV1) -> AcpProviderBridge:
            if request.provider_id != self.spec.id:
                raise ConfigurationError("ACP create request does not match adapter")
            return self.factory(
                model=request.model,
                cwd=request.workspace,
                receipt=receipt,
                extra_env=selected_env,
                provider_home=home,
                timeout=request.timeout,
                max_output_bytes=request.max_output_bytes,
                max_stderr_bytes=request.max_stderr_bytes,
                max_stream_buffer_bytes=request.max_stream_buffer_bytes,
                max_stream_events=request.max_stream_events,
                max_stream_line_bytes=request.max_stream_line_bytes,
            )

        def list_bound_models() -> Tuple[ModelInfo, ...]:
            self._guard_boundaries(None, home)
            return self.models()

        return BoundProviderOperationsV1(
            provider_id=self.spec.id,
            factory=create_bound,
            model_lister=list_bound_models,
            doctor=lambda: self._doctor(receipt, selected_env, home),
            normalized_receipt=installation_receipt_envelope(receipt, persistent=True),
            provider_home=home,
        )


def acp_plugin(
    spec: ProviderAdapterSpecV1,
    *,
    default_model: str = "default",
    launch_resolver: Optional[AcpLaunchResolverV1] = None,
    home_preparer: Optional[AcpHomePreparerV1] = None,
    workspace_guard: Optional[AcpWorkspaceGuardV1] = None,
) -> ProviderPluginV1:
    """Build one lazy, server-disabled ACP 0.11 provider plugin."""

    if spec.status is AdapterStatus.HELD:
        return held_plugin(spec)
    if (
        spec.status not in (AdapterStatus.PREVIEW, AdapterStatus.EXPERIMENTAL)
        or spec.transport is not TransportKind.ACP
        or spec.prompt.mode is not PromptMode.PROTOCOL
        or spec.server_policy.enabled
        or spec.capabilities != frozenset((ProviderCapability.CHAT.value,))
    ):
        raise ConfigurationError("ACP provider contract is not closed")
    runtime = _AcpPluginRuntime(
        spec,
        _validated_model_id(default_model),
        launch_resolver,
        home_preparer,
        workspace_guard,
    )
    return ProviderPluginV1(
        id=spec.id,
        factory=runtime.factory,
        default_model=runtime.default_model,
        model_lister=runtime.models,
        doctor=runtime.doctor,
        capabilities=spec.capabilities,
        route_prefixes=(spec.id,),
        server_policy=ProviderServerPolicyV1(enabled=False),
        support_status=spec.status.value.lower(),
        configuration_abi_version=PROVIDER_CONFIGURATION_ABI_V1,
        launch_binder=runtime.bind,
        environment_keys=spec.environment.allowed_keys,
    )


__all__ = [
    "AcpProviderBridge",
    "acp_plugin",
    "reject_provider_home_config",
    "reject_workspace_config",
    "write_private_json",
]
