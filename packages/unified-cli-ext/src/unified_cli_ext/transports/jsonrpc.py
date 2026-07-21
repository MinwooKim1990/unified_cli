"""Bidirectional JSON-RPC 2.0 over the hardened JSONL subprocess transport."""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Dict, Optional, Union

from ..errors import (
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
)
from .jsonl import JsonlProcess
from .security import CancellationToken, ExecutableIdentity, TransportLimits
from ..normalization.events import freeze_json
from ..normalization.validation import validate_unicode


JsonRpcId = Union[int, str]
_HANDLER_POLL_SECONDS = 0.01


class _HandlerFailed(Exception):
    """Private marker that never carries callback diagnostics."""


def _start_handler(
    handler: Callable[[Any], Any], params: Any
) -> "queue.Queue[tuple[bool, Any]]":
    outcome: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result = handler(params)
        except BaseException:
            # Callback diagnostics may contain user data or credentials. Only
            # return a generic failure marker and suppress thread tracebacks.
            outcome.put((False, None))
        else:
            outcome.put((True, result))

    threading.Thread(
        target=invoke,
        name="unified-cli-ext-jsonrpc-handler",
        daemon=True,
    ).start()
    return outcome


def _consume_task(task: "asyncio.Future[Any]") -> None:
    try:
        task.exception()
    except BaseException:
        pass


async def _cancel_task_bounded(task: "asyncio.Future[Any]") -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.1)
    if done:
        _consume_task(task)
    else:
        task.add_done_callback(_consume_task)


def _valid_id(value: Any) -> bool:
    return (
        type(value) is int
        and abs(value) <= 2**53
    ) or (
        type(value) is str
        and bool(value)
        and _safe_id_text(value)
    )


def _safe_id_text(value: str) -> bool:
    try:
        validate_unicode(value, label="JSON-RPC id", maximum=256, empty=False)
    except ProtocolError:
        return False
    return True


def _method(value: Any) -> str:
    return validate_unicode(value, label="JSON-RPC method", maximum=256, empty=False)


def _params(value: Any) -> Any:
    if value is None:
        return None
    if type(value) not in (dict, list):
        raise ProtocolError("JSON-RPC params must be an object or array")
    try:
        return freeze_json(value, drop_reasoning=False)
    except ProtocolError:
        raise ProtocolError("JSON-RPC params must be bounded JSON") from None


class JsonRpcProcessClient:
    """Single-flight JSON-RPC client with allowlisted reverse requests.

    Callback execution is deadline- and cancellation-bounded from the
    transport's perspective. Python cannot forcibly stop arbitrary in-process
    synchronous code, so a non-cooperative callback is abandoned only in a
    daemon worker after the provider process group has been cleaned up.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        executable_identity: ExecutableIdentity,
        request_handlers: Optional[Mapping[str, Callable[[Any], Any]]] = None,
        timeout: float = 30.0,
        cwd: Optional[str] = None,
        provider_env: Optional[Mapping[str, str]] = None,
        allowed_provider_env: Sequence[str] = (),
        limits: TransportLimits = TransportLimits(),
        cancellation: Optional[CancellationToken] = None,
        persistent_home: Optional[str] = None,
    ) -> None:
        handlers: Dict[str, Callable[[Any], Any]] = {}
        try:
            source = request_handlers if request_handlers is not None else {}
            iterator = iter(source.items())
            for index, pair in enumerate(iterator):
                if index >= 256:
                    raise ProtocolError("JSON-RPC handlers exceed 256 entries")
                try:
                    name, handler = pair
                except (TypeError, ValueError):
                    raise ProtocolError("JSON-RPC handlers are malformed") from None
                name = _method(name)
                if name in handlers:
                    raise ProtocolError("duplicate JSON-RPC request handler")
                if not callable(handler):
                    raise ProtocolError("JSON-RPC request handler must be callable")
                handlers[name] = handler
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("JSON-RPC handlers are malformed") from None
        self._handlers = handlers
        self._transport = JsonlProcess(
            argv,
            timeout=timeout,
            cwd=cwd,
            provider_env=provider_env,
            allowed_provider_env=allowed_provider_env,
            limits=limits,
            cancellation=cancellation,
            persistent_home=persistent_home,
            executable_identity=executable_identity,
        )
        self._next_id = 1
        self._pending = set()
        self._state_lock = threading.Lock()
        self._in_request = False

    @property
    def pid(self) -> Optional[int]:
        return self._transport.pid

    def __enter__(self) -> "JsonRpcProcessClient":
        self._transport.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException as failure:
            if exc_type is None or (
                isinstance(failure, TransportError) and "reaped" in str(failure)
            ):
                raise

    def close(self) -> None:
        self._transport.close()

    def _begin(self) -> int:
        with self._state_lock:
            if self._in_request:
                raise TransportError("JSON-RPC client supports one in-flight request")
            if self._next_id > 2**53:
                raise ProtocolError("JSON-RPC request id space exhausted")
            self._in_request = True
            request_id = self._next_id
            self._next_id += 1
            self._pending.add(request_id)
            return request_id

    def _finish(self, request_id: int) -> None:
        with self._state_lock:
            self._pending.discard(request_id)
            self._in_request = False

    def _handler_result_sync(
        self, handler: Callable[[Any], Any], params: Any
    ) -> Any:
        outcome = _start_handler(handler, params)
        while True:
            self._transport.cancellation.raise_if_cancelled()
            remaining = self._transport.remaining_timeout()
            try:
                succeeded, result = outcome.get(
                    timeout=min(_HANDLER_POLL_SECONDS, remaining)
                )
            except queue.Empty:
                continue
            if not succeeded:
                raise _HandlerFailed()
            return result

    async def _handler_result_async(
        self, handler: Callable[[Any], Any], params: Any
    ) -> Any:
        outcome = _start_handler(handler, params)
        while True:
            self._transport.cancellation.raise_if_cancelled()
            remaining = self._transport.remaining_timeout()
            try:
                succeeded, result = outcome.get_nowait()
            except queue.Empty:
                await asyncio.sleep(min(_HANDLER_POLL_SECONDS, remaining))
                continue
            if not succeeded:
                raise _HandlerFailed()
            return result

    async def _await_handler_result(self, awaitable: Any) -> Any:
        try:
            task = asyncio.ensure_future(awaitable)
        except Exception:
            raise _HandlerFailed() from None
        try:
            while True:
                self._transport.cancellation.raise_if_cancelled()
                remaining = self._transport.remaining_timeout()
                done, _ = await asyncio.wait(
                    {task}, timeout=min(_HANDLER_POLL_SECONDS, remaining)
                )
                if done:
                    try:
                        return task.result()
                    except BaseException:
                        raise _HandlerFailed() from None
        except BaseException:
            await asyncio.shield(_cancel_task_bounded(task))
            raise

    @staticmethod
    def _validate_message(message: Any) -> Dict[str, Any]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ProtocolError("invalid JSON-RPC 2.0 message")
        return message

    def _handle_reverse_sync(self, message: Dict[str, Any]) -> None:
        if "result" in message or "error" in message:
            raise ProtocolError("JSON-RPC request contains response fields")
        method = _method(message.get("method"))
        params = message.get("params")
        if "params" in message and not isinstance(params, (dict, list)):
            raise ProtocolError("JSON-RPC params must be an object or array")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not is_notification and not _valid_id(request_id):
            raise ProtocolError("invalid server-to-client JSON-RPC request id")
        handler = self._handlers.get(method)
        if handler is None:
            if not is_notification:
                self._transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "method not allowlisted"},
                    }
                )
            return
        try:
            result = self._handler_result_sync(handler, params)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise _HandlerFailed()
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (TransportTimeout, TransportCancelled):
            raise
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "client handler failed"},
            }
        if not is_notification:
            self._transport.send(response)

    async def _handle_reverse_async(self, message: Dict[str, Any]) -> None:
        if "result" in message or "error" in message:
            raise ProtocolError("JSON-RPC request contains response fields")
        method = _method(message.get("method"))
        params = message.get("params")
        if "params" in message and not isinstance(params, (dict, list)):
            raise ProtocolError("JSON-RPC params must be an object or array")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not is_notification and not _valid_id(request_id):
            raise ProtocolError("invalid server-to-client JSON-RPC request id")
        handler = self._handlers.get(method)
        if handler is None:
            if not is_notification:
                await self._transport.send_async(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "method not allowlisted"},
                    }
                )
            return
        try:
            result = await self._handler_result_async(handler, params)
            if inspect.isawaitable(result):
                result = await self._await_handler_result(result)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (TransportTimeout, TransportCancelled):
            raise
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "client handler failed"},
            }
        if not is_notification:
            await self._transport.send_async(response)

    @staticmethod
    def _response_value(message: Dict[str, Any], request_id: int) -> Any:
        response_id = message.get("id")
        if not _valid_id(response_id) or type(response_id) is not type(request_id) or response_id != request_id:
            raise ProtocolError("unmatched JSON-RPC response id")
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            raise ProtocolError("JSON-RPC response must contain exactly one result or error")
        if has_error:
            error = message["error"]
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or type(error.get("message")) is not str
            ):
                raise ProtocolError("malformed JSON-RPC error response")
            validate_unicode(
                error["message"],
                label="JSON-RPC error message",
                maximum=4096,
                empty=True,
                allow_text_newlines=True,
            )
            # Do not reflect arbitrary peer data in the public exception.
            raise TransportError("JSON-RPC peer returned error {}".format(error["code"]))
        return message["result"]

    def request(self, method: str, params: Any = None) -> Any:
        method = _method(method)
        params = _params(params)
        request_id = self._begin()
        try:
            self._transport.reset_timeout()
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._transport.send(message)
            while True:
                message = self._transport.receive()
                if message is None:
                    raise TransportError("JSON-RPC peer closed before responding")
                message = self._validate_message(message)
                if "method" in message:
                    self._handle_reverse_sync(message)
                    continue
                return self._response_value(message, request_id)
        except BaseException:
            self.close()
            raise
        finally:
            self._finish(request_id)

    async def request_async(self, method: str, params: Any = None) -> Any:
        method = _method(method)
        params = _params(params)
        request_id = self._begin()
        try:
            self._transport.reset_timeout()
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            await self._transport.send_async(message)
            while True:
                message = await self._transport.receive_async()
                if message is None:
                    raise TransportError("JSON-RPC peer closed before responding")
                message = self._validate_message(message)
                if "method" in message:
                    await self._handle_reverse_async(message)
                    continue
                return self._response_value(message, request_id)
        except BaseException:
            self.close()
            raise
        finally:
            self._finish(request_id)


__all__ = ["JsonRpcId", "JsonRpcProcessClient"]
