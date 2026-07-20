"""Lazy, allowlisted, bounded bridge to an official MCP ClientSession."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any, Awaitable, Dict, Iterable, Optional

from ..errors import (
    LimitExceeded,
    OptionalDependencyError,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
)
from ..normalization.events import FrozenJsonMap, freeze_json
from ..normalization.validation import validate_unicode
from ..transports.security import CancellationToken, validate_positive_timeout


def require_mcp_sdk() -> ModuleType:
    if sys.version_info < (3, 10):
        raise OptionalDependencyError(
            "MCP support requires Python >=3.10 and unified-cli-ext[mcp]"
        )
    try:
        return importlib.import_module("mcp")
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            raise OptionalDependencyError(
                "MCP support requires the optional 'unified-cli-ext[mcp]' extra"
            ) from exc
        raise TransportError("MCP SDK import failed") from None
    except Exception:
        raise TransportError("MCP SDK import failed") from None


def _tool_name(value: str) -> str:
    return validate_unicode(value, label="MCP tool name", maximum=256, empty=False)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError("MCP value is not bounded JSON") from None


def _result_value(value: Any) -> Any:
    try:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump(mode="json")
        return _plain(freeze_json(value))
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("MCP result does not expose a bounded JSON model") from None


def _consume_task(task: "asyncio.Future[Any]") -> None:
    try:
        task.exception()
    except BaseException:
        pass


async def _cancel_bounded(task: "asyncio.Future[Any]") -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.1)
    if done:
        _consume_task(task)
    else:
        # An SDK awaitable may violate cancellation. Never block the caller;
        # consume its eventual exception if the event loop remains alive.
        task.add_done_callback(_consume_task)


class McpCallableBridge:
    """Call only explicitly allowlisted tools on an official MCP session."""

    def __init__(
        self,
        session: Any,
        allowed_tools: Iterable[str],
        *,
        timeout: float = 30.0,
        max_input_bytes: int = 1024 * 1024,
        max_output_bytes: int = 16 * 1024 * 1024,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        if isinstance(allowed_tools, (str, bytes)):
            raise ProtocolError("MCP tool allowlist must be a string collection")
        tools = set()
        try:
            iterator = iter(allowed_tools)
            for index, name in enumerate(iterator):
                if index >= 1024:
                    raise LimitExceeded("MCP tool allowlist exceeds 1024 entries")
                tools.add(_tool_name(name))
        except (ProtocolError, LimitExceeded):
            raise
        except Exception:
            raise ProtocolError("MCP tool allowlist is malformed") from None
        if not tools:
            raise ProtocolError("MCP bridge requires a nonempty tool allowlist")
        clean_timeout = validate_positive_timeout(timeout)
        if (
            type(max_input_bytes) is not int
            or type(max_output_bytes) is not int
            or max_input_bytes <= 0
            or max_output_bytes <= 0
        ):
            raise ValueError("MCP bridge byte limits must be positive integers")
        try:
            call_tool = getattr(session, "call_tool")
        except Exception:
            raise TypeError("MCP session must expose call_tool") from None
        if not callable(call_tool):
            raise TypeError("MCP session must expose call_tool")
        self._call_tool = call_tool
        self._allowed_tools = frozenset(tools)
        self.timeout = clean_timeout
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        if cancellation is not None and type(cancellation) is not CancellationToken:
            raise TypeError("cancellation must be CancellationToken")
        self.cancellation = cancellation if cancellation is not None else CancellationToken()

    async def call_async(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        require_mcp_sdk()
        name = _tool_name(name)
        if name not in self._allowed_tools:
            raise ProtocolError("MCP tool is not allowlisted")
        if arguments is None:
            request: Dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            frozen = freeze_json(arguments)
            if not isinstance(frozen, FrozenJsonMap):
                raise ProtocolError("MCP tool arguments must be an object")
            request = _plain(frozen)
        else:
            raise ProtocolError("MCP tool arguments must be an object")
        if len(_json_bytes(request)) > self.max_input_bytes:
            raise LimitExceeded("MCP tool input exceeds configured limit")
        self.cancellation.raise_if_cancelled()
        try:
            awaitable: Awaitable[Any] = self._call_tool(name, arguments=request)
            task = asyncio.ensure_future(awaitable)
        except Exception:
            raise TransportError("MCP session rejected the bounded call") from None
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            while True:
                if self.cancellation.cancelled:
                    await _cancel_bounded(task)
                    raise TransportCancelled("MCP tool call cancelled")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await _cancel_bounded(task)
                    raise TransportTimeout("MCP tool call timed out")
                done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
                if done:
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        raise TransportCancelled("MCP SDK cancelled the tool call") from None
                    except Exception:
                        raise TransportError("MCP SDK tool call failed") from None
                    break
        except asyncio.CancelledError:
            await asyncio.shield(_cancel_bounded(task))
            raise
        value = _result_value(result)
        if len(_json_bytes(value)) > self.max_output_bytes:
            raise LimitExceeded("MCP tool result exceeds configured limit")
        return value

    def call(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.call_async(name, arguments))
        raise RuntimeError("use call_async() from a running event loop")


__all__ = ["McpCallableBridge", "require_mcp_sdk"]
