"""Closed, runtime-owned Agent Client Protocol stdio transport."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from types import ModuleType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from ..errors import (
    ConfigurationError,
    LimitExceeded,
    OptionalDependencyError,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
    UnsupportedPlatformError,
)
from ..normalization import DoneEvent, NormalizedEvent, SessionEvent, SessionRef, TextDeltaEvent, UsageEvent
from ..normalization.validation import utf8_size, validate_unicode
from .process import (
    _argv,
    _cleanup_spawned_process,
    _managed_environment,
    _require_nonreaping_process_observation,
)
from .security import (
    CancellationToken,
    DirectoryPin,
    ExecutableIdentity,
    IsolatedEnvironment,
    TransportLimits,
    _require_executable_identity_argv,
    strict_json_loads,
    validated_workspace,
    validate_positive_timeout,
)


_UNSET = object()
_GENERIC_PROTOCOL_ERROR = "ACP peer violated the closed text-turn protocol"
_REVERSE_REQUEST_MODELS = {
    "session/request_permission": "RequestPermissionRequest",
    "fs/write_text_file": "WriteTextFileRequest",
    "fs/read_text_file": "ReadTextFileRequest",
    "terminal/create": "CreateTerminalRequest",
    "terminal/output": "TerminalOutputRequest",
    "terminal/release": "ReleaseTerminalRequest",
    "terminal/wait_for_exit": "WaitForTerminalExitRequest",
    "terminal/kill": "KillTerminalRequest",
}
_REVERSE_NOTIFICATION_MODELS = {
    "elicitation/complete": "CompleteElicitationNotification",
}
_ELICITATION_REQUEST_MODELS = (
    "CreateFormSessionElicitationRequest",
    "CreateFormRequestElicitationRequest",
    "CreateUrlSessionElicitationRequest",
    "CreateUrlRequestElicitationRequest",
)


class _AcpReapError(TransportError):
    """The runtime could not confirm termination of its subprocess leader."""


def require_acp_sdk() -> ModuleType:
    """Load the official SDK only when ACP support is explicitly requested."""

    if sys.version_info < (3, 10) or sys.version_info >= (3, 15):
        raise OptionalDependencyError(
            "ACP support requires Python >=3.10,<3.15 and unified-cli[acp]"
        )
    try:
        return importlib.import_module("acp")
    except ModuleNotFoundError as exc:
        if exc.name == "acp":
            raise OptionalDependencyError(
                "ACP support requires the optional 'unified-cli[acp]' extra"
            ) from exc
        raise TransportError("ACP SDK import failed") from None
    except Exception:
        raise TransportError("ACP SDK import failed") from None


class AcpSdkAdapter:
    """Deprecated compatibility-only lazy SDK factory wrapper.

    New provider execution must use :class:`AcpProcessTransportV1`.  This
    wrapper remains available so existing callers are not broken, but it does
    not participate in the runtime-owned ACP execution path.
    """

    def __init__(self, factory: Callable[[ModuleType], Any]) -> None:
        if not callable(factory):
            raise TypeError("ACP adapter factory must be callable")
        self._factory = factory
        self._instance: Any = _UNSET

    @property
    def loaded(self) -> bool:
        return self._instance is not _UNSET

    def open(self) -> Any:
        if self._instance is _UNSET:
            try:
                self._instance = self._factory(require_acp_sdk())
            except OptionalDependencyError:
                raise
            except Exception:
                raise TransportError("ACP SDK factory failed") from None
        return self._instance


class _PipeWriterProtocol(asyncio.Protocol):
    """Small flow-control protocol used to expose an owned pipe as StreamWriter."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._paused = False
        self._lost: Optional[BaseException] = None
        self._waiter: Optional[asyncio.Future] = None

    def pause_writing(self) -> None:
        self._paused = True

    def resume_writing(self) -> None:
        self._paused = False
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)
        self._waiter = None

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        self._lost = exc or BrokenPipeError()
        self.resume_writing()

    async def _drain_helper(self) -> None:
        if self._lost is not None:
            raise self._lost
        if not self._paused:
            return
        if self._waiter is None:
            self._waiter = self._loop.create_future()
        await self._waiter
        if self._lost is not None:
            raise self._lost


class _ClosedAcpClient:
    """ACP client implementation exposing no filesystem, terminal, or tool API."""

    def __init__(self, owner: "AcpProcessTransportV1", sdk: ModuleType, schema: ModuleType) -> None:
        self._owner = owner
        self._sdk = sdk
        self._schema = schema

    def _method_not_found(self, method: str) -> BaseException:
        self._owner._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
        return self._sdk.RequestError.method_not_found(method)

    async def request_permission(self, **kwargs: Any) -> Any:
        self._owner._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
        return self._schema.RequestPermissionResponse(
            outcome=self._schema.DeniedOutcome(outcome="cancelled")
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._owner._ack_prevalidated_session_update(session_id, update, self._schema)

    async def write_text_file(self, **kwargs: Any) -> Any:
        raise self._method_not_found("fs/write_text_file")

    async def read_text_file(self, **kwargs: Any) -> Any:
        raise self._method_not_found("fs/read_text_file")

    async def create_terminal(self, **kwargs: Any) -> Any:
        raise self._method_not_found("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> Any:
        raise self._method_not_found("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> Any:
        raise self._method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        raise self._method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> Any:
        raise self._method_not_found("terminal/kill")

    async def create_elicitation(self, **kwargs: Any) -> Any:
        self._owner._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
        return self._schema.CancelElicitationResponse(action="cancel")

    async def complete_elicitation(self, **kwargs: Any) -> None:
        raise self._method_not_found("elicitation/complete")

    async def ext_method(self, method: str, params: Mapping[str, Any]) -> Any:
        raise self._method_not_found("_{}".format(method))

    async def ext_notification(self, method: str, params: Mapping[str, Any]) -> None:
        raise self._method_not_found("_{}".format(method))

    def on_connect(self, conn: Any) -> None:
        return None


class AcpProcessTransportV1:
    """Single-use, default-closed ACP 0.11 text-turn subprocess boundary."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        executable_identity: ExecutableIdentity,
        cwd: str,
        provider_namespace: str,
        provider_env: Optional[Mapping[str, str]] = None,
        allowed_provider_env: Sequence[str] = (),
        persistent_home: Optional[str] = None,
        limits: TransportLimits = TransportLimits(),
        timeout: float = 30.0,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        if os.name != "posix":
            raise UnsupportedPlatformError(
                "ACP transport requires POSIX process-group cleanup"
            )
        _require_nonreaping_process_observation()
        self._argv = _argv(argv)
        if type(executable_identity) is not ExecutableIdentity:
            raise ConfigurationError("executable_identity must be ExecutableIdentity")
        _require_executable_identity_argv(self._argv[0], executable_identity)
        if type(limits) is not TransportLimits:
            raise ConfigurationError("limits must be TransportLimits")
        token = cancellation if cancellation is not None else CancellationToken()
        if type(token) is not CancellationToken:
            raise ConfigurationError("cancellation must be CancellationToken")
        self._identity = executable_identity
        self._cwd = validated_workspace(cwd)
        self._provider_namespace = SessionRef(
            provider=provider_namespace,
            session_id="pending",
        ).provider
        self._limits = limits
        self._timeout = validate_positive_timeout(timeout)
        self._token = token
        self._environment = IsolatedEnvironment(
            provider_env,
            allowed_provider_keys=allowed_provider_env,
            persistent_home=persistent_home,
        )

        self._used = False
        self._close_requested = False
        self._closed = False
        self._completed = False
        self._session_id: Optional[str] = None
        self._events = []  # type: list[NormalizedEvent]
        self._event_count = 0
        self._text_bytes = 0
        self._inbound_frames = 0
        self._outbound_frames = 0
        self._prevalidated_incoming_frames = 0
        self._pending_session_updates = []  # type: list[Tuple[Any, ...]]
        self._outgoing_requests = {}  # type: dict[Any, str]
        self._prompt_response_seen = False
        self._protocol_failure: Optional[BaseException] = None
        self._failure_event: Optional[asyncio.Event] = None
        self._close_event: Optional[asyncio.Event] = None
        self._resource_lock: Optional[asyncio.Lock] = None
        self._close_task: Optional[asyncio.Task] = None

        self._stack: Optional[ExitStack] = None
        self._process: Optional[subprocess.Popen] = None
        self._connection: Any = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pipe_transports = []  # type: list[asyncio.BaseTransport]
        self._relay_tasks = []  # type: list[asyncio.Task]
        self._stderr = bytearray()

    async def text_turn(self, prompt: str) -> Tuple[NormalizedEvent, ...]:
        """Run exactly one bounded text-only ACP session and return immutable events."""

        if self._close_requested or self._closed:
            raise TransportError("ACP transport is closed")
        if self._used:
            raise TransportError("ACP transport is single-use")
        self._used = True
        self._token.raise_if_cancelled()
        validate_unicode(
            prompt,
            label="ACP prompt",
            maximum=self._limits.max_output_bytes,
            empty=True,
            allow_text_newlines=True,
        )
        self._failure_event = asyncio.Event()
        self._close_event = asyncio.Event()
        self._resource_lock = asyncio.Lock()

        operation = asyncio.create_task(self._run_text_turn(prompt))
        cancel_watch = asyncio.create_task(self._wait_for_cancellation())
        failure_watch = asyncio.create_task(self._failure_event.wait())
        close_watch = asyncio.create_task(self._close_event.wait())
        watches = (cancel_watch, failure_watch, close_watch)
        failure = None  # type: Optional[BaseException]
        result = None  # type: Optional[Tuple[NormalizedEvent, ...]]
        try:
            done, _ = await asyncio.wait(
                (operation,) + watches,
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done and self._protocol_failure is not None:
                failure = self._protocol_failure
            elif operation in done:
                result = await operation
            elif not done:
                failure = TransportTimeout("ACP text turn timed out")
            elif cancel_watch in done and self._token.cancelled:
                failure = TransportCancelled("extension operation cancelled")
            elif failure_watch in done and self._protocol_failure is not None:
                failure = self._protocol_failure
            else:
                failure = TransportCancelled("ACP transport was closed")
            if failure is not None:
                operation.cancel()
                with contextlib.suppress(BaseException):
                    await operation
                raise failure
            assert result is not None
            return result
        except (OptionalDependencyError, ConfigurationError, ProtocolError, TransportError):
            raise
        except asyncio.CancelledError:
            raise
        except BaseException:
            raise TransportError("ACP transport failed") from None
        finally:
            primary_failure = sys.exc_info()[1]
            for task in watches:
                task.cancel()
            await asyncio.gather(*watches, return_exceptions=True)
            cleanup_failure = None
            try:
                await asyncio.shield(self.close_async())
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(self.close_async())
                except BaseException as exc:
                    cleanup_failure = exc
            except BaseException as exc:
                cleanup_failure = exc
            if cleanup_failure is not None and (
                primary_failure is None
                or isinstance(cleanup_failure, _AcpReapError)
            ):
                raise cleanup_failure

    async def _run_text_turn(self, prompt: str) -> Tuple[NormalizedEvent, ...]:
        sdk, schema = self._load_sdk()
        await self._start_async(sdk, schema)
        if self._close_requested:
            raise TransportCancelled("ACP transport was closed")
        connection = self._connection
        initialize = await connection.initialize(
            protocol_version=sdk.PROTOCOL_VERSION,
            client_capabilities=schema.ClientCapabilities(
                fs=None,
                terminal=False,
                session=None,
                plan=None,
                auth=None,
                elicitation=None,
                nes=None,
                position_encodings=None,
            ),
        )
        if type(initialize.protocol_version) is not int or initialize.protocol_version != sdk.PROTOCOL_VERSION:
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
        session = await connection.new_session(
            cwd=self._cwd,
            additional_directories=[],
            mcp_servers=[],
        )
        session_id = session.session_id
        validate_unicode(session_id, label="ACP session id", maximum=1024, empty=False)
        self._session_id = session_id
        self._append_event(
            SessionEvent(
                SessionRef(provider=self._provider_namespace, session_id=session_id)
            )
        )
        response = await connection.prompt(
            session_id=session_id,
            prompt=[schema.TextContentBlock(type="text", text=prompt)],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            cached_input_tokens = getattr(usage, "cached_read_tokens", None)
            if cached_input_tokens is None:
                cached_input_tokens = 0
            values = (input_tokens, output_tokens, cached_input_tokens)
            if any(
                type(value) is not int or value < 0 or value > 10**15
                for value in values
            ):
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._append_event(
                UsageEvent(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_input_tokens=cached_input_tokens,
                )
            )
        self._completed = True
        if self._protocol_failure is not None:
            raise self._protocol_failure
        reason = response.stop_reason
        if type(reason) is not str:
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
        self._append_event(DoneEvent(reason=reason))
        return tuple(self._events)

    def _load_sdk(self) -> Tuple[ModuleType, ModuleType]:
        sdk = require_acp_sdk()
        required = ("PROTOCOL_VERSION", "RequestError", "connect_to_agent")
        if any(not hasattr(sdk, name) for name in required):
            raise TransportError("ACP SDK is incompatible")
        if type(sdk.PROTOCOL_VERSION) is not int:
            raise TransportError("ACP SDK is incompatible")
        schema = getattr(sdk, "schema", None)
        if schema is None:
            try:
                schema = importlib.import_module("acp.schema")
            except Exception:
                raise TransportError("ACP SDK is incompatible") from None
        return sdk, schema

    async def _start_async(self, sdk: ModuleType, schema: ModuleType) -> None:
        assert self._resource_lock is not None
        async with self._resource_lock:
            if self._close_requested:
                raise TransportCancelled("ACP transport was closed")
            stack = ExitStack()
            self._stack = stack
            cwd_pin = stack.enter_context(DirectoryPin(self._cwd))
            stack.enter_context(_managed_environment(self._environment))
            self._token.raise_if_cancelled()
            self._identity.verify()
            cwd_pin.verify()
            self._environment.verify_for_spawn()
            try:
                process = subprocess.Popen(
                    list(self._argv),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd_pin,
                    env=self._environment.env,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                    bufsize=0,
                )
            except (OSError, UnicodeError):
                raise TransportError("failed to start ACP subprocess") from None
            self._process = process
            cwd_pin.verify()
            self._environment.verify_after_spawn()
            self._identity.verify_metadata()
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            loop = asyncio.get_running_loop()
            stdout_reader = asyncio.StreamReader(limit=self._limits.max_line_bytes + 1)
            stdout_protocol = asyncio.StreamReaderProtocol(stdout_reader)
            stdout_transport, _ = await loop.connect_read_pipe(lambda: stdout_protocol, process.stdout)
            self._pipe_transports.append(stdout_transport)
            stderr_reader = asyncio.StreamReader(limit=64 * 1024)
            stderr_protocol = asyncio.StreamReaderProtocol(stderr_reader)
            stderr_transport, _ = await loop.connect_read_pipe(lambda: stderr_protocol, process.stderr)
            self._pipe_transports.append(stderr_transport)
            write_protocol = _PipeWriterProtocol()
            stdin_transport, _ = await loop.connect_write_pipe(lambda: write_protocol, process.stdin)
            self._pipe_transports.append(stdin_transport)
            writer = asyncio.StreamWriter(stdin_transport, write_protocol, None, loop)
            self._writer = writer
            sdk_reader = asyncio.StreamReader(limit=self._limits.max_line_bytes + 1)
            self._relay_tasks = [
                asyncio.create_task(self._relay_stdout(stdout_reader, sdk_reader, schema)),
                asyncio.create_task(self._drain_stderr(stderr_reader)),
            ]
            client = _ClosedAcpClient(self, sdk, schema)
            self._connection = sdk.connect_to_agent(
                client,
                writer,
                sdk_reader,
                use_unstable_protocol=False,
                observers=[self._observe_frame],
            )

    async def _relay_stdout(
        self,
        source: asyncio.StreamReader,
        destination: asyncio.StreamReader,
        schema: ModuleType,
    ) -> None:
        pending = bytearray()
        total = 0
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._limits.max_output_bytes:
                    raise LimitExceeded("ACP stdout exceeds configured limit")
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    frame = bytes(pending[:newline])
                    del pending[: newline + 1]
                    self._validate_inbound_frame(frame, schema)
                    destination.feed_data(frame + b"\n")
                if len(pending) > self._limits.max_line_bytes:
                    raise LimitExceeded("ACP frame exceeds configured line limit")
            if pending:
                frame = bytes(pending)
                self._validate_inbound_frame(frame, schema)
                destination.feed_data(frame)
        except asyncio.CancelledError:
            raise
        except (LimitExceeded, ProtocolError) as exc:
            self._fail_protocol(exc)
        except BaseException:
            self._fail_protocol(TransportError("ACP stdout relay failed"))
        finally:
            destination.feed_eof()

    def _validate_inbound_frame(self, frame: bytes, schema: ModuleType) -> None:
        if len(frame) > self._limits.max_line_bytes:
            raise LimitExceeded("ACP frame exceeds configured line limit")
        self._inbound_frames += 1
        self._check_frame_count()
        try:
            value = strict_json_loads(frame.decode("utf-8", "strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR) from None
        if type(value) is not dict:
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
        self._prevalidate_inbound_message(value, schema)
        self._prevalidated_incoming_frames += 1

    def _prevalidate_inbound_message(self, message: Mapping[str, Any], schema: ModuleType) -> None:
        method = message.get("method")
        if method is None:
            if "id" not in message:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            request_method = self._outgoing_requests.get(message.get("id"))
            if request_method == "session/prompt":
                self._prompt_response_seen = True
            return
        if type(method) is not str:
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR)

        has_id = "id" in message
        params = message.get("params")
        if method == "session/update":
            if has_id:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            notification = self._validate_schema_model(
                schema,
                "SessionNotification",
                params,
            )
            self._accept_prevalidated_session_update(
                notification.session_id,
                notification.update,
                schema,
            )
            return

        model_name = _REVERSE_REQUEST_MODELS.get(method)
        if model_name is not None:
            if not has_id:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._validate_schema_model(schema, model_name, params)
            self._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
            return

        if method == "elicitation/create":
            if not has_id:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._validate_elicitation_request(schema, params)
            self._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
            return

        model_name = _REVERSE_NOTIFICATION_MODELS.get(method)
        if model_name is not None:
            if has_id:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._validate_schema_model(schema, model_name, params)
            self._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
            return

        raise ProtocolError(_GENERIC_PROTOCOL_ERROR)

    def _validate_schema_model(
        self,
        schema: ModuleType,
        model_name: str,
        params: Any,
    ) -> Any:
        model = getattr(schema, model_name, None)
        validator = getattr(model, "model_validate", None)
        if not callable(validator):
            raise TransportError("ACP SDK is incompatible")
        try:
            return validator(params)
        except Exception:
            raise ProtocolError(_GENERIC_PROTOCOL_ERROR) from None

    def _validate_elicitation_request(self, schema: ModuleType, params: Any) -> Any:
        available = False
        for model_name in _ELICITATION_REQUEST_MODELS:
            model = getattr(schema, model_name, None)
            validator = getattr(model, "model_validate", None)
            if not callable(validator):
                continue
            available = True
            try:
                return validator(params)
            except Exception:
                continue
        if not available:
            raise TransportError("ACP SDK is incompatible")
        raise ProtocolError(_GENERIC_PROTOCOL_ERROR) from None

    async def _drain_stderr(self, source: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    return
                if len(self._stderr) + len(chunk) > self._limits.max_stderr_bytes:
                    raise LimitExceeded("ACP stderr exceeds configured limit")
                self._stderr.extend(chunk)
        except asyncio.CancelledError:
            raise
        except LimitExceeded as exc:
            self._fail_protocol(exc)
        except BaseException:
            self._fail_protocol(TransportError("ACP stderr relay failed"))

    def _observe_frame(self, event: Any) -> None:
        direction = getattr(getattr(event, "direction", None), "value", getattr(event, "direction", None))
        if direction == "outgoing":
            self._outbound_frames += 1
            try:
                self._check_frame_count()
            except BaseException as exc:
                self._fail_protocol(exc)
            message = getattr(event, "message", None)
            if type(message) is dict:
                method = message.get("method")
                if type(method) is str and "id" in message:
                    self._outgoing_requests[message.get("id")] = method
        elif direction == "incoming":
            if self._prevalidated_incoming_frames < 1:
                self._fail_protocol(ProtocolError(_GENERIC_PROTOCOL_ERROR))
                return
            self._prevalidated_incoming_frames -= 1

    def _check_frame_count(self) -> None:
        if self._inbound_frames + self._outbound_frames > self._limits.max_events:
            raise LimitExceeded("ACP frame count exceeds configured limit")

    def _session_update_signature(
        self,
        session_id: str,
        update: Any,
        schema: ModuleType,
    ) -> Tuple[Any, ...]:
        if isinstance(update, schema.AgentMessageChunk):
            content = update.content
            if not isinstance(content, schema.TextContentBlock):
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            return (
                "agent_message_chunk",
                session_id,
                content.text,
                update.message_id,
            )
        if isinstance(update, schema.UsageUpdate):
            return ("usage_update", session_id, update.used, update.size)
        raise ProtocolError(_GENERIC_PROTOCOL_ERROR)

    def _accept_prevalidated_session_update(
        self,
        session_id: str,
        update: Any,
        schema: ModuleType,
    ) -> None:
        try:
            if (
                self._completed
                or self._prompt_response_seen
                or self._session_id is None
                or session_id != self._session_id
            ):
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            signature = self._session_update_signature(session_id, update, schema)
            if signature[0] == "agent_message_chunk":
                text = signature[2]
                size = utf8_size(text, label="ACP output text")
                if self._text_bytes + size > self._limits.max_output_bytes:
                    raise LimitExceeded("ACP output text exceeds configured limit")
                self._text_bytes += size
                block_id = signature[3] if signature[3] is not None else "default"
                self._append_event(TextDeltaEvent(text=text, block_id=block_id))
            else:
                used = signature[2]
                size = signature[3]
                if (
                    type(used) is not int
                    or type(size) is not int
                    or used < 0
                    or size < 0
                    or used > 10**15
                    or size > 10**15
                ):
                    raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._pending_session_updates.append(signature)
        except (LimitExceeded, ProtocolError) as exc:
            self._fail_protocol(exc)
            raise
        except BaseException:
            failure = ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._fail_protocol(failure)
            raise failure from None

    def _ack_prevalidated_session_update(
        self,
        session_id: str,
        update: Any,
        schema: ModuleType,
    ) -> None:
        try:
            if not self._pending_session_updates:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            signature = self._session_update_signature(session_id, update, schema)
            if signature != self._pending_session_updates[0]:
                raise ProtocolError(_GENERIC_PROTOCOL_ERROR)
            del self._pending_session_updates[0]
        except (LimitExceeded, ProtocolError) as exc:
            self._fail_protocol(exc)
            raise
        except BaseException:
            failure = ProtocolError(_GENERIC_PROTOCOL_ERROR)
            self._fail_protocol(failure)
            raise failure from None

    def _append_event(self, event: NormalizedEvent) -> None:
        self._event_count += 1
        if self._event_count > self._limits.max_events:
            raise LimitExceeded("ACP event count exceeds configured limit")
        self._events.append(event)

    def _fail_protocol(self, failure: BaseException) -> None:
        if self._protocol_failure is None:
            if isinstance(failure, (LimitExceeded, ProtocolError, TransportError)):
                self._protocol_failure = failure
            else:
                self._protocol_failure = ProtocolError(_GENERIC_PROTOCOL_ERROR)
            if self._failure_event is not None:
                self._failure_event.set()

    async def _wait_for_cancellation(self) -> None:
        while not self._token.cancelled:
            await asyncio.sleep(0.02)

    async def close_async(self) -> None:
        """Idempotently close SDK tasks, streams, and the owned process group."""

        self._close_requested = True
        if self._close_event is not None:
            self._close_event.set()
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        lock = self._resource_lock
        if lock is None:
            self._closed = True
            return
        cleanup_failed = False
        reap_failed = False
        async with lock:
            try:
                connection = self._connection
                self._connection = None
                if connection is not None:
                    try:
                        await asyncio.wait_for(connection.close(), timeout=0.5)
                    except BaseException:
                        cleanup_failed = True
                for task in self._relay_tasks:
                    task.cancel()
                if self._relay_tasks:
                    await asyncio.gather(*self._relay_tasks, return_exceptions=True)
                self._relay_tasks = []
                writer = self._writer
                self._writer = None
                if writer is not None:
                    with contextlib.suppress(BaseException):
                        writer.close()
                        await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
                for transport in self._pipe_transports:
                    with contextlib.suppress(BaseException):
                        transport.close()
                self._pipe_transports = []
                process = self._process
                self._process = None
                if process is not None:
                    try:
                        await asyncio.to_thread(
                            _cleanup_spawned_process,
                            process,
                            executable_identity=self._identity,
                        )
                    except TransportError:
                        reap_failed = True
                    except BaseException:
                        cleanup_failed = True
                stack = self._stack
                self._stack = None
                if stack is not None:
                    try:
                        stack.close()
                    except BaseException:
                        cleanup_failed = True
            finally:
                self._stderr.clear()
                self._closed = True
        if reap_failed:
            raise _AcpReapError(
                "ACP subprocess termination could not be confirmed"
            ) from None
        if cleanup_failed:
            raise TransportError("ACP transport cleanup failed") from None


__all__ = ["AcpProcessTransportV1", "AcpSdkAdapter", "require_acp_sdk"]
