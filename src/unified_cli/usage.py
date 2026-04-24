"""In-memory usage tracker — process-lifetime only.

Every completed turn passes through `UsageTracker.record(...)`. The tracker
aggregates totals per provider + per (provider, model) and keeps a bounded
ring of the most recent calls for the status UI / /dashboard.

Thread-safe via a single lock. Intentionally no persistence (DB / file) —
restart resets the counters.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

from .core import ProviderName


@dataclass
class CallRecord:
    ts: float                # unix epoch
    provider: ProviderName
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    session_id: str = ""
    conversation_id: str = ""
    prompt_preview: str = ""   # first ~60 chars, for the dashboard
    error_kind: Optional[str] = None


@dataclass
class ProviderAggregate:
    provider: ProviderName
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_latency_ms: int = 0
    last_call_ts: Optional[float] = None
    model_calls: dict[str, int] = field(default_factory=dict)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.calls if self.calls else 0.0


class UsageTracker:
    """Single global tracker instance (see `tracker` module-level singleton)."""

    def __init__(self, history_size: int = 100):
        self._history: deque[CallRecord] = deque(maxlen=history_size)
        self._agg: dict[ProviderName, ProviderAggregate] = {}
        self._lock = threading.Lock()

    def record(
        self,
        provider: ProviderName,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: int = 0,
        *,
        session_id: str = "",
        conversation_id: str = "",
        prompt_preview: str = "",
        error_kind: Optional[str] = None,
    ) -> None:
        rec = CallRecord(
            ts=time.time(),
            provider=provider, model=model,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cached_tokens=cached_tokens or 0,
            latency_ms=latency_ms or 0,
            session_id=session_id, conversation_id=conversation_id,
            prompt_preview=prompt_preview[:60],
            error_kind=error_kind,
        )
        with self._lock:
            self._history.append(rec)
            agg = self._agg.setdefault(provider, ProviderAggregate(provider=provider))
            agg.calls += 1
            if error_kind:
                agg.errors += 1
            agg.input_tokens += rec.input_tokens
            agg.output_tokens += rec.output_tokens
            agg.cached_tokens += rec.cached_tokens
            agg.total_latency_ms += rec.latency_ms
            agg.last_call_ts = rec.ts
            agg.model_calls[model] = agg.model_calls.get(model, 0) + 1

    def aggregates(self) -> list[ProviderAggregate]:
        with self._lock:
            return list(self._agg.values())

    def recent(self, limit: int = 30) -> list[CallRecord]:
        with self._lock:
            return list(self._history)[-limit:][::-1]  # newest first

    def snapshot(self) -> dict:
        """JSON-friendly dump for /v1/usage."""
        with self._lock:
            return {
                "aggregates": [
                    {**asdict(a), "avg_latency_ms": a.avg_latency_ms}
                    for a in self._agg.values()
                ],
                "recent": [asdict(r) for r in list(self._history)[::-1]],
            }

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._agg.clear()


# module-level singleton
tracker = UsageTracker()
