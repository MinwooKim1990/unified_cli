"""BaseProvider ABC with shared subprocess execution, retry, and fallback."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, Iterator, Optional

from .core import Message, ModelInfo, ProviderName, Response, Usage
from .errors import UnifiedError, classify


# Max 2 retries (0.5s, 1.5s) for network errors; 1 retry for auth fallback.
_NETWORK_BACKOFF = (0.5, 1.5)


class BaseProvider(ABC):
    """Base class for a single-provider CLI wrapper.

    Subclasses must implement:
      - `_build_args(prompt, session_id, resume_last, model, streaming)` → argv list
      - `_normalize(obj)` → iterator of Message (from raw JSON object)
      - `_parse_response(raw_text)` → Response (for non-streaming `--output-format json`)
      - `_default_env()` → dict of env vars to set (subclass-specific)
    """

    name: ClassVar[ProviderName]
    default_model: ClassVar[str]
    api_key_env: ClassVar[str]       # e.g., "ANTHROPIC_API_KEY"
    login_hint: ClassVar[str]        # e.g., "`claude /login` 재실행"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        bin_path: Optional[str] = None,
        extra_env: Optional[dict] = None,
        timeout: Optional[float] = None,
        web_search: bool = True,
    ):
        self.model = model or self.default_model
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.timeout = timeout
        self.web_search = web_search

        resolved = bin_path or self._discover_bin()
        if not resolved:
            raise UnifiedError(
                kind="config", provider=self.name,
                message=f"{self.name} CLI 바이너리를 찾을 수 없습니다.",
                hint=self._install_hint(),
            )
        self.bin_path = resolved

    # ----- abstract -----

    @classmethod
    @abstractmethod
    def _discover_bin(cls) -> Optional[str]: ...

    @classmethod
    @abstractmethod
    def _install_hint(cls) -> str: ...

    @abstractmethod
    def _build_args(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        streaming: bool,
    ) -> list[str]: ...

    @abstractmethod
    def _normalize(self, obj: dict) -> Iterator[Message]: ...

    @abstractmethod
    def _parse_json_response(self, text: str, model: str) -> Response: ...

    # ----- env + subprocess -----

    def _env(self, fallback_api_key: bool = False) -> dict:
        env = os.environ.copy()
        env.update(self.extra_env)
        if fallback_api_key and self.api_key_env in os.environ:
            env[self.api_key_env] = os.environ[self.api_key_env]
        return env

    def _run(self, args: list[str]) -> str:
        """Run subprocess with non-streaming output. Returns stdout on success.

        Handles auth-expired fallback (retry once with API key env) and network
        retries (up to 2 with exponential backoff).
        """
        tried_api_fallback = False
        last_err: Optional[UnifiedError] = None

        for attempt in range(len(_NETWORK_BACKOFF) + 1):
            result = subprocess.run(
                args, capture_output=True, text=True,
                cwd=self.cwd, env=self._env(), timeout=self.timeout,
            )
            if result.returncode == 0:
                return result.stdout

            err = classify(self.name, result.stderr, result.stdout, result.returncode)
            last_err = err

            if err.kind == "auth_expired" and not tried_api_fallback:
                if self.api_key_env in os.environ:
                    tried_api_fallback = True
                    args_retry = args
                    result = subprocess.run(
                        args_retry, capture_output=True, text=True,
                        cwd=self.cwd, env=self._env(fallback_api_key=True),
                        timeout=self.timeout,
                    )
                    if result.returncode == 0:
                        return result.stdout
                    err = classify(self.name, result.stderr, result.stdout, result.returncode)
                    last_err = err
                raise err  # no key available or fallback also failed

            if err.kind == "network" and attempt < len(_NETWORK_BACKOFF):
                time.sleep(_NETWORK_BACKOFF[attempt])
                continue

            raise err

        assert last_err is not None
        raise last_err

    # ----- public API -----

    def chat(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
    ) -> Response:
        args = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=False,
        )
        stdout = self._run(args)
        return self._parse_json_response(stdout, model or self.model)

    def stream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
    ) -> Iterator[Message]:
        args = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=True,
        )
        yield from self._stream_run(args)

    async def achat(self, prompt: str, **kw) -> Response:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.chat(prompt, **kw))

    async def astream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
    ) -> AsyncIterator[Message]:
        args = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=True,
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd, env=self._env(),
        )
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                line = raw.decode().strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._normalize(obj):
                    yield msg
        finally:
            await proc.wait()
            if proc.returncode != 0:
                err_bytes = await proc.stderr.read() if proc.stderr else b""
                raise classify(self.name, err_bytes.decode(), "", proc.returncode)

    def _stream_once(
        self,
        args: list[str],
        *,
        fallback: bool,
    ) -> Iterator[Message]:
        """Run subprocess once, yield normalized messages, raise on failure."""
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.cwd,
            env=self._env(fallback_api_key=fallback), bufsize=1,
        )
        assert proc.stdout is not None
        produced_any = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._normalize(obj):
                    produced_any = True
                    yield msg
        finally:
            proc.wait(timeout=self.timeout)
            stderr_text = proc.stderr.read() if proc.stderr else ""

        if proc.returncode not in (0, None):
            err = classify(self.name, stderr_text, "", proc.returncode)
            # attach a marker so the outer retry loop can decide
            err._produced_any = produced_any  # type: ignore[attr-defined]
            raise err

    def _stream_run(self, args: list[str]) -> Iterator[Message]:
        """Sync streaming with one auth-fallback retry on pre-stream failure."""
        try:
            yield from self._stream_once(args, fallback=False)
            return
        except UnifiedError as err:
            produced = getattr(err, "_produced_any", False)
            if (err.kind == "auth_expired"
                    and not produced
                    and self.api_key_env in os.environ):
                yield from self._stream_once(args, fallback=True)
                return
            raise
