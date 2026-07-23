"""Fail-closed Core bridge for verified provider-adapter one-shot turns.

The bridge is intentionally lazy.  Constructing plugin metadata never resolves
an executable, reads ambient credentials, or starts a process.  A factory call
must supply either an explicit canonical ``bin_path``, a complete local
installation receipt, or the provider's trusted receipt resolver. Inspection
and doctor probes precede provider construction; every prompt launch then
revalidates the issued inspection and
binary/receipt binding in :mod:`unified_cli_ext.providers.runtime`.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterator, Optional, Tuple

from unified_cli.base import BaseProvider
from unified_cli.core import Message, ModelInfo, Response, Usage
from unified_cli.errors import UnifiedError
from unified_cli.plugin import (
    PROVIDER_CONFIGURATION_ABI_V1,
    BoundProviderOperationsV1,
    ProviderCreateRequestV1,
    ProviderLaunchContextV1,
    ProviderPluginV1,
    ProviderReceiptEnvelopeV1,
    ProviderServerPolicyV1,
)
from unified_cli.usage import tracker as _usage_tracker

from ..errors import (
    ConfigurationError,
    ExtensionError,
    LimitExceeded,
    ProcessFailed,
    ProviderReportedError,
    ProtocolError,
    TransportCancelled,
    TransportTimeout,
)
from ..normalization import (
    DoneEvent,
    ErrorEvent,
    EventNormalizer,
    FinalTextEvent,
    PermissionRequestEvent,
    ReasoningSummaryEvent,
    SessionEvent,
    TextDeltaEvent,
    ToolProgressEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
    freeze_json,
)
from ..transports import CancellationToken
from ..transports.security import (
    private_persistent_home,
    strict_json_loads,
    validated_workspace,
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
from .installation import (
    InstallationReceiptV1,
    installation_receipt_from_record,
    installation_receipt_to_record,
)
from .runtime import (
    AdapterInspectionV1,
    BinaryProvenance,
    OpenedProcessTransportV1,
    ProtocolLaunchBoundaryV1,
    ProviderAdapterV1,
)


AdapterLaunchResolverV1 = Callable[[], InstallationReceiptV1]
AdapterStateFactoryV1 = Callable[[], Any]
AdapterRecordMapperV1 = Callable[[Mapping, Any], Iterable[Mapping]]
AdapterResponseMapperV1 = Callable[[Any, Any], Iterable[Mapping]]
AdapterFinalizerV1 = Callable[[Any], Iterable[Mapping]]
AdapterTurnPreflightV1 = Callable[[str, Optional[str]], None]

_SUPPORTED_PROCESS_TRANSPORTS = frozenset(
    (TransportKind.PLAIN, TransportKind.JSON, TransportKind.JSONL)
)
_UNSUPPORTED_CAPABILITIES = frozenset(
    (
        ProviderCapability.PERMISSIONS.value,
        ProviderCapability.IMAGES.value,
        ProviderCapability.MCP.value,
    )
)
_MAX_MODEL_ID_CHARS = 512
INSTALLATION_RECEIPT_MEDIA_TYPE_V1 = (
    "application/vnd.unified-cli-ext.installation-receipt.v1+json"
)


@dataclass(frozen=True)
class _ProviderIssuance:
    """Route-critical values fixed when the verified provider is issued."""

    provider_id: str
    default_model: str
    configured_model: str
    dynamic_arguments: frozenset
    capabilities: frozenset


def _configuration_error(provider: str, message: str) -> UnifiedError:
    return UnifiedError(kind="config", provider=provider, message=message)


def _cancelled_error(provider: str) -> UnifiedError:
    error = UnifiedError(
        kind="internal",
        provider=provider,
        message="Provider request was cancelled.",
    )
    error._cancelled = True  # type: ignore[attr-defined]
    return error


def _core_error(provider: str, error: ExtensionError) -> UnifiedError:
    """Translate Ext failures without copying provider diagnostics."""

    if isinstance(error, TransportCancelled):
        return _cancelled_error(provider)
    if isinstance(error, ConfigurationError):
        return _configuration_error(
            provider, "Provider adapter configuration is unavailable."
        )
    if isinstance(error, ProviderReportedError):
        message = "Provider reported an error."
    elif isinstance(error, TransportTimeout):
        message = "Provider request timed out."
    elif isinstance(error, LimitExceeded):
        message = "Provider output exceeded its configured limit."
    elif isinstance(error, ProtocolError):
        message = "Provider returned an invalid response."
    elif isinstance(error, ProcessFailed):
        message = "Provider process failed."
    else:
        message = "Provider runtime failed."
    return UnifiedError(kind="internal", provider=provider, message=message)


def installation_receipt_envelope(
    receipt: InstallationReceiptV1, *, persistent: bool = True
) -> ProviderReceiptEnvelopeV1:
    """Return a Core-owned serialized envelope for one verified receipt."""

    if type(receipt) is not InstallationReceiptV1:
        raise ConfigurationError("provider installation receipt is invalid")
    return ProviderReceiptEnvelopeV1(
        provider_id=receipt.provider_id,
        media_type=INSTALLATION_RECEIPT_MEDIA_TYPE_V1,
        payload=installation_receipt_to_record(receipt, persistent=persistent),
    )


def installation_receipt_from_envelope(
    envelope: ProviderReceiptEnvelopeV1,
) -> InstallationReceiptV1:
    """Decode and reverify exactly the bridge-owned receipt media type."""

    if (
        type(envelope) is not ProviderReceiptEnvelopeV1
        or envelope.media_type != INSTALLATION_RECEIPT_MEDIA_TYPE_V1
    ):
        raise ConfigurationError("provider installation receipt envelope is invalid")
    receipt = installation_receipt_from_record(envelope.payload)
    if receipt.provider_id != envelope.provider_id:
        raise ConfigurationError("provider installation receipt id changed")
    return receipt


def _validated_model_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_MODEL_ID_CHARS:
        raise ConfigurationError("provider model id is invalid")
    if value != value.strip():
        raise ConfigurationError("provider model id is invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ConfigurationError("provider model id is invalid") from None
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        raise ConfigurationError("provider model id is invalid")
    return value


def _plain_json(value: Any) -> Any:
    """Thaw normalized JSON into Core-owned mutable containers."""

    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ProtocolError("provider tool payload is invalid")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    raise ProtocolError("provider tool payload is invalid")


def _safe_mapper_output(
    mapper: Callable[..., Iterable[Mapping]],
    argument: Any,
    state: Any,
    *,
    remaining: int,
) -> Tuple[Mapping, ...]:
    """Materialize bounded canonical records from one pure mapper call."""

    try:
        supplied = mapper(argument, state)
        if isinstance(supplied, (str, bytes, Mapping)):
            raise TypeError
        iterator = iter(supplied)
        records = []
        for index, record in enumerate(iterator):
            if index >= remaining:
                raise LimitExceeded("mapper emitted too many canonical events")
            if not isinstance(record, Mapping):
                raise TypeError
            records.append(record)
    except LimitExceeded:
        raise
    except (KeyboardInterrupt, GeneratorExit):
        raise
    except BaseException:
        raise ProtocolError("provider response mapper failed") from None
    return tuple(records)


def _safe_finalizer_output(
    finalizer: AdapterFinalizerV1,
    state: Any,
    *,
    remaining: int,
) -> Tuple[Mapping, ...]:
    """Materialize one bounded clean-EOF finalizer result."""

    try:
        supplied = finalizer(state)
        if isinstance(supplied, (str, bytes, Mapping)):
            raise TypeError
        iterator = iter(supplied)
        records = []
        for index, record in enumerate(iterator):
            if index >= remaining:
                raise LimitExceeded("finalizer emitted too many canonical events")
            if not isinstance(record, Mapping):
                raise TypeError
            records.append(record)
    except LimitExceeded:
        raise
    except (KeyboardInterrupt, GeneratorExit):
        raise
    except BaseException:
        raise ProtocolError("provider response finalizer failed") from None
    return tuple(records)


class _CancellationRelay:
    """Relay an Event-like Core cancellation signal into an Ext token."""

    def __init__(self, source: Optional[object], token: CancellationToken) -> None:
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        if source is None:
            return
        check = getattr(source, "is_set", None)
        if not callable(check):
            raise ConfigurationError("cancel_event must expose is_set()")
        try:
            already_cancelled = bool(check())
        except BaseException:
            raise ConfigurationError("cancel_event could not be inspected") from None
        if already_cancelled:
            token.cancel()
            return

        def relay() -> None:
            while not self._stop.wait(0.02):
                try:
                    if check():
                        token.cancel()
                        return
                except BaseException:
                    token.cancel()
                    return

        thread = threading.Thread(
            target=relay,
            name="unified-cli-ext-cancel-relay",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)


class _TurnState:
    """Invocation-local mapper, normalization, and correlation state."""

    def __init__(
        self,
        provider: str,
        capabilities: frozenset,
        mapper_state: Any,
        expected_session: Optional[str],
        max_events: int,
        max_text_bytes: int,
    ) -> None:
        self.provider = provider
        self.capabilities = capabilities
        self.mapper_state = mapper_state
        self.normalizer = EventNormalizer(
            provider, max_text_bytes=max_text_bytes
        )
        self.expected_session = expected_session
        self.max_events = max_events
        self.canonical_count = 0
        self.messages = []  # type: list[Message]
        self.text_chunks = []  # type: list[str]
        self.final_text = None  # type: Optional[str]
        self.session_id = ""
        self.usage = Usage()
        self.active_tools = set()  # type: set[str]
        self.done = False
        self.saw_error = False
        self.error_retryable = False

    def remaining_events(self) -> int:
        return self.max_events - self.canonical_count

    def accept_record(self, record: Mapping) -> Tuple[Message, ...]:
        if self.done:
            raise ProtocolError("provider emitted an event after done")
        if self.canonical_count >= self.max_events:
            raise LimitExceeded("canonical event limit exceeded")
        self.canonical_count += 1
        try:
            normalized = self.normalizer.feed(record)
        except ExtensionError:
            raise
        except BaseException:
            raise ProtocolError("provider canonical event is invalid") from None
        emitted = []
        for event in normalized:
            message = self._accept_event(event)
            if message is not None:
                self.messages.append(message)
                emitted.append(message)
        return tuple(emitted)

    def _accept_event(self, event: Any) -> Optional[Message]:
        if self.saw_error and not isinstance(event, DoneEvent):
            raise ProtocolError("provider emitted an event after error")
        if isinstance(event, SessionEvent):
            if ProviderCapability.SESSIONS.value not in self.capabilities:
                raise ProtocolError("provider emitted an undeclared session event")
            session_id = event.session.session_id
            if self.session_id:
                raise ProtocolError("provider emitted multiple session events")
            if self.expected_session is not None and session_id != self.expected_session:
                raise ProtocolError("provider session did not match the request")
            self.session_id = session_id
            return Message(
                kind="session",
                provider=self.provider,
                session_id=session_id,
                raw={"type": "session"},
            )
        if isinstance(event, TextDeltaEvent):
            if self.final_text is not None:
                raise ProtocolError("provider emitted text after final text")
            if not event.text:
                return None
            self.text_chunks.append(event.text)
            return Message(
                kind="text",
                provider=self.provider,
                text=event.text,
                raw={"type": "text_delta"},
            )
        if isinstance(event, FinalTextEvent):
            if self.final_text is not None:
                raise ProtocolError("provider emitted multiple final text events")
            self.final_text = event.complete_text
            if not event.text:
                return None
            self.text_chunks.append(event.text)
            return Message(
                kind="text",
                provider=self.provider,
                text=event.text,
                raw={"type": "text_final"},
            )
        if isinstance(event, ReasoningSummaryEvent):
            if ProviderCapability.REASONING_SUMMARIES.value not in self.capabilities:
                raise ProtocolError("provider emitted undeclared reasoning metadata")
            return Message(
                kind="reasoning",
                provider=self.provider,
                text=event.summary,
                raw={"type": "reasoning_summary"},
            )
        if isinstance(event, ToolStartEvent):
            if ProviderCapability.TOOLS.value not in self.capabilities:
                raise ProtocolError("provider emitted an undeclared tool event")
            self.active_tools.add(event.tool_id)
            return Message(
                kind="tool_use",
                provider=self.provider,
                tool={
                    "id": event.tool_id,
                    "name": event.name,
                    "input": _plain_json(event.arguments),
                },
                raw={"type": "tool_start"},
            )
        if isinstance(event, ToolProgressEvent):
            if ProviderCapability.TOOLS.value not in self.capabilities:
                raise ProtocolError("provider emitted an undeclared tool event")
            if event.tool_id not in self.active_tools:
                raise ProtocolError("provider emitted progress for an inactive tool")
            # Core has no distinct progress kind.  Re-emitting ``tool_use``
            # would make manage create a phantom second tool invocation.
            return None
        if isinstance(event, ToolResultEvent):
            if ProviderCapability.TOOLS.value not in self.capabilities:
                raise ProtocolError("provider emitted an undeclared tool event")
            if event.tool_id not in self.active_tools:
                raise ProtocolError("provider emitted a result for an inactive tool")
            self.active_tools.discard(event.tool_id)
            return Message(
                kind="tool_result",
                provider=self.provider,
                tool={
                    "id": event.tool_id,
                    "output": _plain_json(event.result),
                    "is_error": event.is_error,
                },
                raw={"type": "tool_result"},
            )
        if isinstance(event, PermissionRequestEvent):
            raise ProtocolError(
                "one-shot adapters do not implement a permission lifecycle"
            )
        if isinstance(event, UsageEvent):
            usage = Usage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cached_tokens=event.cached_input_tokens,
                total_tokens=event.input_tokens + event.output_tokens,
            )
            self.usage = usage
            return Message(
                kind="usage",
                provider=self.provider,
                usage=usage,
                raw={"type": "usage"},
            )
        if isinstance(event, ErrorEvent):
            if self.saw_error:
                raise ProtocolError("provider emitted multiple error events")
            self.saw_error = True
            self.error_retryable = event.retryable
            return Message(
                kind="error",
                provider=self.provider,
                error="Provider reported an error.",
                raw={"type": "error", "retryable": event.retryable},
            )
        if isinstance(event, DoneEvent):
            if self.active_tools:
                raise ProtocolError("provider finished with active tool calls")
            self.done = True
            return Message(
                kind="done",
                provider=self.provider,
                raw={"type": "done"},
            )
        raise ProtocolError("provider emitted an unsupported normalized event")

    def finish(self) -> None:
        if not self.done:
            raise ProtocolError("provider response did not include done")
        if self.active_tools:
            raise ProtocolError("provider response left active tool calls")
        if self.saw_error:
            raise ProviderReportedError(retryable=self.error_retryable)
        if self.expected_session is not None and not self.session_id:
            raise ProtocolError("provider response omitted the requested session")

    def response(self, model: str) -> Response:
        self.finish()
        text = self.final_text
        if text is None:
            text = "".join(self.text_chunks)
        return Response(
            text=text,
            session_id=self.session_id,
            provider=self.provider,
            model=model,
            usage=self.usage,
            messages=list(self.messages),
            raw=[dict(message.raw) for message in self.messages],
        )


class AdapterProviderBridge(BaseProvider):
    """A real Core ``BaseProvider`` backed by one verified Ext adapter."""

    name = "extension"
    default_model = "default"
    api_key_env = ""
    allow_api_key_fallback = False

    def __init__(
        self,
        *,
        adapter: ProviderAdapterV1,
        inspection: AdapterInspectionV1,
        binary: BinaryProvenance,
        default_model: str,
        model: Optional[str],
        cwd: str,
        provider_env: Mapping[str, str],
        provider_home: Optional[str],
        limits: OperationLimits,
        state_factory: Optional[AdapterStateFactoryV1],
        map_record: Optional[AdapterRecordMapperV1],
        map_response: Optional[AdapterResponseMapperV1],
        finalize: Optional[AdapterFinalizerV1],
        turn_preflight: Optional[AdapterTurnPreflightV1],
        first_output_timeout: Optional[float] = None,
        web_search: bool = False,
        max_stream_buffer_bytes: Optional[int] = None,
        max_stream_line_bytes: Optional[int] = None,
    ) -> None:
        spec = adapter.spec
        if first_output_timeout is not None:
            raise ConfigurationError(
                "adapter bridge does not support first_output_timeout"
            )
        if web_search:
            raise ConfigurationError("adapter does not declare web search support")
        issued_default_model = _validated_model_id(default_model)
        selected_model = _validated_model_id(
            issued_default_model if model is None else model
        )
        dynamic_arguments = frozenset(
            item.name for item in spec.prompt.dynamic_arguments
        )
        if selected_model != issued_default_model and "model" not in dynamic_arguments:
            raise ConfigurationError("adapter does not declare a model argument")
        self._issuance = _ProviderIssuance(
            provider_id=spec.id,
            default_model=issued_default_model,
            configured_model=selected_model,
            dynamic_arguments=dynamic_arguments,
            capabilities=spec.capabilities,
        )
        self.name = spec.id
        self.default_model = issued_default_model
        super().__init__(
            model=selected_model,
            cwd=cwd,
            bin_path=binary.real_path,
            extra_env={},
            timeout=limits.timeout_seconds,
            first_output_timeout=limits.timeout_seconds,
            web_search=web_search,
            max_output_bytes=limits.max_stdout_bytes,
            max_stderr_bytes=limits.max_stderr_bytes,
            max_stream_buffer_bytes=(
                limits.max_stdout_bytes
                if max_stream_buffer_bytes is None
                else max_stream_buffer_bytes
            ),
            max_stream_events=limits.max_events,
            max_stream_line_bytes=(
                min(limits.max_stdout_bytes, 1024 * 1024)
                if max_stream_line_bytes is None
                else max_stream_line_bytes
            ),
        )
        self._adapter = adapter
        self._inspection = inspection
        self._binary = binary
        self._provider_env = MappingProxyType(dict(provider_env))
        self._provider_home = provider_home
        self._limits = limits
        self._state_factory = state_factory
        self._map_record = map_record
        self._map_response = map_response
        self._finalize = finalize
        self._turn_preflight = turn_preflight

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return None

    @classmethod
    def _install_hint(cls) -> str:
        return "Supply an explicit verified provider installation."

    def _build_args(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise ConfigurationError("bridge argv are runtime-owned")

    def _normalize(self, obj: dict) -> Iterator[Message]:
        del obj
        raise ConfigurationError("bridge normalization is invocation-local")

    def _parse_json_response(self, text: str, model: str) -> Response:
        del text, model
        raise ConfigurationError("bridge response parsing is invocation-local")

    def _env(self, fallback_api_key: bool = False) -> dict:
        del fallback_api_key
        return dict(self._provider_env)

    def _mapper_state(self) -> Any:
        if self._state_factory is None:
            return {}
        try:
            return self._state_factory()
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException:
            raise ProtocolError("provider mapper state initialization failed") from None

    def _run_turn_preflight(self) -> None:
        callback = self._turn_preflight
        if callback is None:
            return
        try:
            callback(self.cwd, self._provider_home)
        except ExtensionError:
            raise
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException:
            raise ConfigurationError("provider turn preflight failed") from None

    def _session_value(self, session_id: Optional[str]) -> Optional[str]:
        if session_id is None:
            return None
        if type(session_id) is not str or not session_id:
            raise ConfigurationError("provider session id is invalid")
        if ":" in session_id:
            provider, separator, value = session_id.partition(":")
            if (
                not separator
                or provider != self._issuance.provider_id
                or not value
            ):
                raise ConfigurationError("provider session namespace is invalid")
            return value
        return session_id

    def _prompt_values(
        self,
        *,
        model: str,
        session_id: Optional[str],
        resume_last: bool,
    ) -> Mapping[str, str]:
        declared = self._issuance.dynamic_arguments
        values = {}
        if "model" in declared:
            values["model"] = model
        if session_id is not None:
            if "session" in declared:
                values["session"] = session_id
            elif "session_id" in declared:
                values["session_id"] = session_id
            else:
                raise ConfigurationError(
                    "adapter does not declare a session argument"
                )
        if resume_last:
            raise ConfigurationError(
                "adapter ABI v1 does not declare a resume-last argument"
            )
        return MappingProxyType(values)

    def _selected_model(self, model: Optional[str]) -> str:
        issuance = self._issuance
        if (
            self.name != issuance.provider_id
            or self.default_model != issuance.default_model
        ):
            raise ConfigurationError("provider issuance state changed")
        configured_model = _validated_model_id(self.model)
        requested_model = (
            configured_model if model is None else _validated_model_id(model)
        )
        if "model" not in issuance.dynamic_arguments:
            if configured_model != issuance.configured_model:
                raise ConfigurationError("provider configured model changed")
            if requested_model != issuance.configured_model:
                raise ConfigurationError("adapter does not declare a model argument")
            return issuance.configured_model
        return requested_model

    def _provider_id(self) -> str:
        """Return the immutable provider identity used for errors and usage."""

        return self._issuance.provider_id

    def _initial_model(self) -> str:
        """Return the issued model before validating public mutable state."""

        return self._issuance.configured_model

    def _turn(
        self, expected_session: Optional[str]
    ) -> _TurnState:
        return _TurnState(
            self._issuance.provider_id,
            self._issuance.capabilities,
            self._mapper_state(),
            expected_session,
            self._limits.max_events,
            self._limits.max_stdout_bytes,
        )

    @staticmethod
    def _validate_call(prompt: str, images: Optional[list]) -> None:
        if type(prompt) is not str or not prompt.strip():
            raise ConfigurationError("provider prompt must not be empty")
        if images:
            raise ConfigurationError("one-shot adapter bridge does not support images")

    def _mapped_messages(
        self,
        mapper: Callable[..., Iterable[Mapping]],
        argument: Any,
        turn: _TurnState,
    ) -> Tuple[Message, ...]:
        records = _safe_mapper_output(
            mapper,
            argument,
            turn.mapper_state,
            remaining=turn.remaining_events(),
        )
        messages = []
        for record in records:
            messages.extend(turn.accept_record(record))
        return tuple(messages)

    def _finalized_messages(self, turn: _TurnState) -> Tuple[Message, ...]:
        finalizer = self._finalize
        if finalizer is None:
            return ()
        records = _safe_finalizer_output(
            finalizer,
            turn.mapper_state,
            remaining=turn.remaining_events(),
        )
        messages = []
        for record in records:
            messages.extend(turn.accept_record(record))
        return tuple(messages)

    def _one_shot_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: _TurnState,
        token: CancellationToken,
    ) -> Tuple[Message, ...]:
        self._run_turn_preflight()
        transport = self._adapter.open_transport(
            self._inspection,
            prompt,
            values,
            cwd=self.cwd,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
            cancellation=token,
            limits=self._limits,
        )
        if type(transport) is not OpenedProcessTransportV1:
            raise ProtocolError("adapter returned an incompatible one-shot transport")
        result = transport.run()
        if result.returncode != 0:
            raise ProcessFailed(result.returncode, result.stderr)
        if self._adapter.spec.transport is TransportKind.PLAIN:
            records = (
                {"type": "text_final", "text": result.stdout},
                {"type": "done", "reason": "complete"},
            )
            messages = []
            for record in records:
                messages.extend(turn.accept_record(record))
            return tuple(messages)
        if self._map_response is None:
            raise ProtocolError("JSON adapter has no response mapper")
        try:
            parsed = strict_json_loads(result.stdout)
            bounded = freeze_json(parsed, drop_reasoning=False)
        except (TypeError, ValueError, UnicodeError, RecursionError, ProtocolError):
            raise ProtocolError("provider returned malformed JSON") from None
        return self._mapped_messages(self._map_response, bounded, turn)

    def _iter_jsonl_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: _TurnState,
        token: CancellationToken,
    ) -> Iterator[Message]:
        if self._map_record is None:
            raise ProtocolError("JSONL adapter has no record mapper")
        self._run_turn_preflight()
        boundary = self._adapter.open_transport(
            self._inspection,
            prompt,
            values,
            cwd=self.cwd,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
            cancellation=token,
            limits=self._limits,
        )
        if type(boundary) is not ProtocolLaunchBoundaryV1:
            raise ProtocolError("adapter returned an incompatible JSONL transport")
        iterator = None
        try:
            boundary.close_stdin()
            iterator = boundary.iter_messages()
            for record in iterator:
                for message in self._mapped_messages(
                    self._map_record, record, turn
                ):
                    yield message
            for message in self._finalized_messages(turn):
                yield message
            turn.finish()
        finally:
            if iterator is not None:
                iterator.close()
            boundary.close()

    async def _aiter_jsonl_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: _TurnState,
        token: CancellationToken,
    ) -> Any:
        if self._map_record is None:
            raise ProtocolError("JSONL adapter has no record mapper")
        self._run_turn_preflight()
        boundary = self._adapter.open_transport(
            self._inspection,
            prompt,
            values,
            cwd=self.cwd,
            provider_env=self._provider_env,
            provider_home=self._provider_home,
            cancellation=token,
            limits=self._limits,
        )
        if type(boundary) is not ProtocolLaunchBoundaryV1:
            raise ProtocolError("adapter returned an incompatible JSONL transport")
        iterator = None
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, boundary.close_stdin)
            iterator = boundary.aiter_messages()
            async for record in iterator:
                for message in self._mapped_messages(
                    self._map_record, record, turn
                ):
                    yield message
            for message in self._finalized_messages(turn):
                yield message
            turn.finish()
        finally:
            try:
                if iterator is not None:
                    await iterator.aclose()
            finally:
                await boundary.close_async()

    def _sync_messages(
        self,
        prompt: str,
        values: Mapping[str, str],
        turn: _TurnState,
        token: CancellationToken,
    ) -> Iterator[Message]:
        if self._adapter.spec.transport is TransportKind.JSONL:
            yield from self._iter_jsonl_messages(prompt, values, turn, token)
            return
        for message in self._one_shot_messages(prompt, values, turn, token):
            yield message
        turn.finish()

    @staticmethod
    def _record_success(
        provider: str, model: str, turn: _TurnState, started: float
    ) -> None:
        _usage_tracker.record(
            provider,
            model,
            input_tokens=turn.usage.input_tokens or 0,
            output_tokens=turn.usage.output_tokens or 0,
            cached_tokens=turn.usage.cached_tokens or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
            session_id=turn.session_id,
        )

    @staticmethod
    def _record_error(provider: str, model: str, started: float) -> None:
        _usage_tracker.record(
            provider,
            model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_kind="internal",
        )

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
        provider_id = self._provider_id()
        selected_model = self._initial_model()
        started = time.monotonic()
        token = CancellationToken()
        relay = None
        try:
            selected_model = self._selected_model(model)
            self._validate_call(prompt, images)
            clean_session = self._session_value(session_id)
            values = self._prompt_values(
                model=selected_model,
                session_id=clean_session,
                resume_last=resume_last,
            )
            turn = self._turn(clean_session)
            relay = _CancellationRelay(cancel_event, token)
            tuple(self._sync_messages(prompt, values, turn, token))
            token.raise_if_cancelled()
            response = turn.response(selected_model)
            self._record_success(provider_id, selected_model, turn, started)
            return response
        except ExtensionError as error:
            self._record_error(provider_id, selected_model, started)
            raise _core_error(provider_id, error) from None
        finally:
            if relay is not None:
                relay.close()

    def stream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[Message]:
        provider_id = self._provider_id()
        selected_model = self._initial_model()
        started = time.monotonic()
        token = CancellationToken()
        relay = None
        succeeded = False
        try:
            selected_model = self._selected_model(model)
            self._validate_call(prompt, images)
            clean_session = self._session_value(session_id)
            values = self._prompt_values(
                model=selected_model,
                session_id=clean_session,
                resume_last=resume_last,
            )
            turn = self._turn(clean_session)
            relay = _CancellationRelay(cancel_event, token)
            for message in self._sync_messages(prompt, values, turn, token):
                token.raise_if_cancelled()
                yield message
            token.raise_if_cancelled()
            succeeded = True
            self._record_success(provider_id, selected_model, turn, started)
        except ExtensionError as error:
            self._record_error(provider_id, selected_model, started)
            raise _core_error(provider_id, error) from None
        finally:
            if not succeeded:
                token.cancel()
            if relay is not None:
                relay.close()

    async def achat(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Response:
        provider_id = self._provider_id()
        selected_model = self._initial_model()
        started = time.monotonic()
        token = CancellationToken()
        relay = None
        worker = None
        try:
            selected_model = self._selected_model(model)
            self._validate_call(prompt, images)
            clean_session = self._session_value(session_id)
            values = self._prompt_values(
                model=selected_model,
                session_id=clean_session,
                resume_last=resume_last,
            )
            turn = self._turn(clean_session)
            relay = _CancellationRelay(cancel_event, token)

            def run() -> Response:
                tuple(self._sync_messages(prompt, values, turn, token))
                token.raise_if_cancelled()
                return turn.response(selected_model)

            loop = asyncio.get_running_loop()
            worker = loop.run_in_executor(None, run)
            try:
                response = await asyncio.shield(worker)
            except asyncio.CancelledError:
                token.cancel()
                try:
                    await asyncio.shield(worker)
                except (ExtensionError, UnifiedError):
                    pass
                raise
            self._record_success(provider_id, selected_model, turn, started)
            return response
        except asyncio.CancelledError:
            raise
        except ExtensionError as error:
            self._record_error(provider_id, selected_model, started)
            raise _core_error(provider_id, error) from None
        finally:
            if relay is not None:
                relay.close()

    async def astream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Any:
        provider_id = self._provider_id()
        selected_model = self._initial_model()
        started = time.monotonic()
        token = CancellationToken()
        relay = None
        succeeded = False
        try:
            selected_model = self._selected_model(model)
            self._validate_call(prompt, images)
            clean_session = self._session_value(session_id)
            values = self._prompt_values(
                model=selected_model,
                session_id=clean_session,
                resume_last=resume_last,
            )
            turn = self._turn(clean_session)
            relay = _CancellationRelay(cancel_event, token)
            if self._adapter.spec.transport is TransportKind.JSONL:
                async for message in self._aiter_jsonl_messages(
                    prompt, values, turn, token
                ):
                    token.raise_if_cancelled()
                    yield message
            else:
                loop = asyncio.get_running_loop()
                worker = loop.run_in_executor(
                    None,
                    lambda: self._one_shot_messages(
                        prompt, values, turn, token
                    ),
                )
                try:
                    messages = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    token.cancel()
                    try:
                        await asyncio.shield(worker)
                    except ExtensionError:
                        pass
                    raise
                for message in messages:
                    token.raise_if_cancelled()
                    yield message
                turn.finish()
            token.raise_if_cancelled()
            succeeded = True
            self._record_success(provider_id, selected_model, turn, started)
        except asyncio.CancelledError:
            token.cancel()
            raise
        except ExtensionError as error:
            self._record_error(provider_id, selected_model, started)
            raise _core_error(provider_id, error) from None
        finally:
            if not succeeded:
                token.cancel()
            if relay is not None:
                relay.close()


def _effective_limits(
    spec: ProviderAdapterSpecV1,
    *,
    timeout: Optional[float],
    max_output_bytes: Optional[int],
    max_stderr_bytes: Optional[int],
    max_stream_buffer_bytes: Optional[int],
    max_stream_events: Optional[int],
    max_stream_line_bytes: Optional[int],
) -> OperationLimits:
    declared = spec.prompt.limits
    timeout_values = [declared.timeout_seconds]
    if timeout is not None:
        try:
            numeric_timeout = float(timeout)
        except (OverflowError, TypeError, ValueError):
            raise ConfigurationError("provider timeout is invalid") from None
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(numeric_timeout)
            or numeric_timeout <= 0
        ):
            raise ConfigurationError("provider timeout is invalid")
        timeout_values.append(numeric_timeout)
    stdout_values = [declared.max_stdout_bytes]
    for value in (max_output_bytes, max_stream_buffer_bytes, max_stream_line_bytes):
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ConfigurationError("provider output limit is invalid")
            stdout_values.append(value)
    stderr = declared.max_stderr_bytes
    if max_stderr_bytes is not None:
        if type(max_stderr_bytes) is not int or max_stderr_bytes <= 0:
            raise ConfigurationError("provider stderr limit is invalid")
        stderr = min(stderr, max_stderr_bytes)
    events = declared.max_events
    if max_stream_events is not None:
        if type(max_stream_events) is not int or max_stream_events <= 0:
            raise ConfigurationError("provider event limit is invalid")
        events = min(events, max_stream_events)
    return OperationLimits(
        timeout_seconds=min(timeout_values),
        max_stdout_bytes=min(stdout_values),
        max_stderr_bytes=stderr,
        max_events=events,
    )


class _AdapterPluginRuntime:
    def __init__(
        self,
        spec: ProviderAdapterSpecV1,
        default_model: str,
        launch_resolver: Optional[AdapterLaunchResolverV1],
        state_factory: Optional[AdapterStateFactoryV1],
        map_record: Optional[AdapterRecordMapperV1],
        map_response: Optional[AdapterResponseMapperV1],
        finalize: Optional[AdapterFinalizerV1],
        turn_preflight: Optional[AdapterTurnPreflightV1],
    ) -> None:
        self.spec = spec
        self.default_model = default_model
        self.launch_resolver = launch_resolver
        self.state_factory = state_factory
        self.map_record = map_record
        self.map_response = map_response
        self.finalize = finalize
        self.turn_preflight = turn_preflight

    def _candidate(
        self,
        bin_path: Optional[str],
        receipt: Optional[InstallationReceiptV1],
    ) -> InstallationReceiptV1:
        if bin_path is not None and receipt is not None:
            raise ConfigurationError(
                "bin_path and installation receipt are mutually exclusive"
            )
        if bin_path is not None:
            if type(bin_path) is not str:
                raise ConfigurationError("provider binary path is invalid")
            return InstallationReceiptV1.capture_explicit_direct(
                provider_id=self.spec.id,
                executable_path=bin_path,
                executable_basename=self.spec.binary.executable,
            )
        if receipt is not None:
            if type(receipt) is not InstallationReceiptV1:
                raise ConfigurationError("provider installation receipt is invalid")
            return receipt
        resolver = self.launch_resolver
        if resolver is None:
            raise ConfigurationError(
                "provider requires an explicit canonical bin_path or installation receipt"
            )
        try:
            candidate = resolver()
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException:
            raise ConfigurationError("provider launch resolution failed") from None
        if type(candidate) is not InstallationReceiptV1:
            raise ConfigurationError(
                "provider resolver must return an InstallationReceiptV1"
            )
        return candidate

    def _gate(
        self,
        *,
        bin_path: Optional[str],
        receipt: Optional[InstallationReceiptV1],
        provider_env: Mapping[str, str],
        provider_home: Optional[str],
    ) -> Tuple[ProviderAdapterV1, BinaryProvenance, AdapterInspectionV1]:
        adapter = ProviderAdapterV1(self.spec)
        candidate = self._candidate(bin_path, receipt)
        binary = adapter.resolve_installation(candidate)
        inspection = adapter.inspect(
            binary,
            provider_env=provider_env,
            provider_home=provider_home,
        )
        if self.spec.doctor is None:
            raise ConfigurationError(
                "enabled provider adapters require a doctor probe"
            )
        if not adapter.doctor_provider(
            inspection,
            provider_env=provider_env,
            provider_home=provider_home,
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
    ) -> AdapterProviderBridge:
        if unknown:
            raise ConfigurationError("provider factory received unsupported options")
        if type(cwd) is not str:
            raise ConfigurationError(
                "provider factory requires an explicit absolute cwd"
            )
        workspace = validated_workspace(cwd)
        if type(web_search) is not bool:
            raise ConfigurationError("web_search must be bool")
        if web_search:
            raise ConfigurationError("adapter does not declare web search support")
        if first_output_timeout is not None:
            raise ConfigurationError(
                "adapter bridge does not support first_output_timeout"
            )
        selected_model = _validated_model_id(
            self.default_model if model is None else model
        )
        declared = frozenset(
            item.name for item in self.spec.prompt.dynamic_arguments
        )
        if selected_model != self.default_model and "model" not in declared:
            raise ConfigurationError("adapter does not declare a model argument")
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
        adapter, binary, inspection = self._gate(
            bin_path=bin_path,
            receipt=receipt,
            provider_env=selected_env,
            provider_home=provider_home,
        )
        return AdapterProviderBridge(
            adapter=adapter,
            inspection=inspection,
            binary=binary,
            default_model=self.default_model,
            model=model,
            cwd=workspace,
            provider_env=selected_env,
            provider_home=provider_home,
            limits=limits,
            state_factory=self.state_factory,
            map_record=self.map_record,
            map_response=self.map_response,
            finalize=self.finalize,
            turn_preflight=self.turn_preflight,
            first_output_timeout=first_output_timeout,
            web_search=web_search,
            max_stream_buffer_bytes=(
                limits.max_stdout_bytes
                if max_stream_buffer_bytes is None
                else min(max_stream_buffer_bytes, limits.max_stdout_bytes)
            ),
            max_stream_line_bytes=(
                min(limits.max_stdout_bytes, 1024 * 1024)
                if max_stream_line_bytes is None
                else min(max_stream_line_bytes, limits.max_stdout_bytes)
            ),
        )

    def _models_with_context(
        self,
        receipt: InstallationReceiptV1,
        provider_env: Mapping[str, str],
        provider_home: Optional[str],
    ) -> Tuple[ModelInfo, ...]:
        receipt.verify()
        if self.spec.models is None:
            return (
                ModelInfo(
                    id=self.default_model,
                    provider=self.spec.id,
                    default=True,
                    source="plugin",
                ),
            )
        adapter, _binary, inspection = self._gate(
            bin_path=None,
            receipt=receipt,
            provider_env=provider_env,
            provider_home=provider_home,
        )
        model_ids = adapter.list_models(
            inspection,
            provider_env=provider_env,
            provider_home=provider_home,
        )
        return tuple(
            ModelInfo(
                id=value,
                provider=self.spec.id,
                default=value == self.default_model,
                source="plugin",
            )
            for value in model_ids
        )

    def _doctor_with_context(
        self,
        receipt: InstallationReceiptV1,
        provider_env: Mapping[str, str],
        provider_home: Optional[str],
    ) -> Mapping[str, object]:
        try:
            _adapter, _binary, inspection = self._gate(
                bin_path=None,
                receipt=receipt,
                provider_env=provider_env,
                provider_home=provider_home,
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

    def bind(self, context: ProviderLaunchContextV1) -> BoundProviderOperationsV1:
        """Close every Core operation over one verified launch snapshot."""

        if (
            type(context) is not ProviderLaunchContextV1
            or context.provider_id != self.spec.id
        ):
            raise ConfigurationError("provider launch context does not match adapter")
        decoded = (
            None
            if context.receipt is None
            else installation_receipt_from_envelope(context.receipt)
        )
        receipt = self._candidate(context.bin_path, decoded)
        if receipt.provider_id != self.spec.id:
            raise ConfigurationError("provider installation receipt id changed")
        receipt.verify()
        selected_env = self.spec.environment.select(context.provider_env)
        provider_home = context.provider_home
        if provider_home is not None:
            provider_home = private_persistent_home(provider_home)

        def create_bound(request: ProviderCreateRequestV1) -> AdapterProviderBridge:
            if (
                type(request) is not ProviderCreateRequestV1
                or request.provider_id != self.spec.id
            ):
                raise ConfigurationError("provider create request does not match adapter")
            return self.factory(
                model=request.model,
                cwd=request.workspace,
                receipt=receipt,
                extra_env=selected_env,
                provider_home=provider_home,
                timeout=request.timeout,
                max_output_bytes=request.max_output_bytes,
                max_stderr_bytes=request.max_stderr_bytes,
                max_stream_buffer_bytes=request.max_stream_buffer_bytes,
                max_stream_events=request.max_stream_events,
                max_stream_line_bytes=request.max_stream_line_bytes,
                web_search=False,
                first_output_timeout=None,
            )

        def list_bound_models() -> Tuple[ModelInfo, ...]:
            return self._models_with_context(receipt, selected_env, provider_home)

        def doctor_bound() -> Mapping[str, object]:
            return self._doctor_with_context(receipt, selected_env, provider_home)

        return BoundProviderOperationsV1(
            provider_id=self.spec.id,
            factory=create_bound,
            model_lister=list_bound_models,
            doctor=doctor_bound,
            normalized_receipt=installation_receipt_envelope(
                receipt, persistent=True
            ),
            provider_home=provider_home,
        )

    def models(self) -> Tuple[ModelInfo, ...]:
        if self.spec.models is None:
            return (
                ModelInfo(
                    id=self.default_model,
                    provider=self.spec.id,
                    default=True,
                    source="plugin",
                ),
            )
        adapter, _binary, inspection = self._gate(
            bin_path=None,
            receipt=None,
            provider_env=MappingProxyType({}),
            provider_home=None,
        )
        model_ids = adapter.list_models(inspection)
        return tuple(
            ModelInfo(
                id=value,
                provider=self.spec.id,
                default=value == self.default_model,
                source="plugin",
            )
            for value in model_ids
        )

    def doctor(self) -> Mapping[str, object]:
        try:
            _adapter, _binary, inspection = self._gate(
                bin_path=None,
                receipt=None,
                provider_env=MappingProxyType({}),
                provider_home=None,
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


def adapter_plugin(
    spec: ProviderAdapterSpecV1,
    *,
    default_model: str,
    launch_resolver: Optional[AdapterLaunchResolverV1] = None,
    state_factory: Optional[AdapterStateFactoryV1] = None,
    map_record: Optional[AdapterRecordMapperV1] = None,
    map_response: Optional[AdapterResponseMapperV1] = None,
    finalize: Optional[AdapterFinalizerV1] = None,
    turn_preflight: Optional[AdapterTurnPreflightV1] = None,
) -> ProviderPluginV1:
    """Build lazy Core plugin metadata for one declarative adapter spec."""

    if type(spec) is not ProviderAdapterSpecV1:
        raise ConfigurationError("adapter plugin requires ProviderAdapterSpecV1")
    default_model = _validated_model_id(default_model)
    if spec.status is AdapterStatus.HELD:
        return held_plugin(spec)
    if spec.transport not in _SUPPORTED_PROCESS_TRANSPORTS:
        raise ConfigurationError(
            "adapter transport lacks a complete one-shot lifecycle"
        )
    if spec.prompt.mode is PromptMode.PROTOCOL:
        raise ConfigurationError(
            "adapter protocol prompt lacks a complete one-shot lifecycle"
        )
    unsupported = spec.capabilities.intersection(_UNSUPPORTED_CAPABILITIES)
    if unsupported:
        raise ConfigurationError(
            "adapter advertises a capability unavailable to the Core bridge"
        )
    if launch_resolver is not None and not callable(launch_resolver):
        raise ConfigurationError("adapter launch_resolver must be callable")
    if state_factory is not None and not callable(state_factory):
        raise ConfigurationError("adapter state_factory must be callable")
    if map_record is not None and not callable(map_record):
        raise ConfigurationError("adapter map_record must be callable")
    if map_response is not None and not callable(map_response):
        raise ConfigurationError("adapter map_response must be callable")
    if finalize is not None and not callable(finalize):
        raise ConfigurationError("adapter finalize must be callable")
    if turn_preflight is not None and not callable(turn_preflight):
        raise ConfigurationError("adapter turn_preflight must be callable")
    if spec.transport is TransportKind.JSONL and map_record is None:
        raise ConfigurationError("JSONL adapter requires map_record")
    if spec.transport is TransportKind.JSON and map_response is None:
        raise ConfigurationError("JSON adapter requires map_response")
    if spec.transport is TransportKind.PLAIN and (
        map_record is not None or map_response is not None or finalize is not None
    ):
        raise ConfigurationError("plain adapter does not accept response mappers")
    if spec.transport is not TransportKind.JSONL and finalize is not None:
        raise ConfigurationError("clean-EOF finalizer requires JSONL transport")

    runtime = _AdapterPluginRuntime(
        spec,
        default_model,
        launch_resolver,
        state_factory,
        map_record,
        map_response,
        finalize,
        turn_preflight,
    )
    return ProviderPluginV1(
        id=spec.id,
        factory=runtime.factory,
        default_model=default_model,
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


provider_plugin = adapter_plugin


__all__ = [
    "AdapterLaunchResolverV1",
    "AdapterFinalizerV1",
    "AdapterProviderBridge",
    "AdapterRecordMapperV1",
    "AdapterResponseMapperV1",
    "AdapterStateFactoryV1",
    "AdapterTurnPreflightV1",
    "INSTALLATION_RECEIPT_MEDIA_TYPE_V1",
    "adapter_plugin",
    "installation_receipt_envelope",
    "installation_receipt_from_envelope",
    "provider_plugin",
]
