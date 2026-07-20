"""Fail-closed permission contracts."""

from __future__ import annotations

from typing import Any, Callable

from ..normalization.events import PermissionDecision, PermissionRequestEvent


def map_permission_decision(value: Any) -> PermissionDecision:
    """Map an explicit UI decision; persistent/unknown grants become deny."""

    if value is PermissionDecision.ALLOW_ONCE or (
        type(value) is str and value == "allow_once"
    ):
        return PermissionDecision.ALLOW_ONCE
    return PermissionDecision.DENY


class PermissionPolicy:
    """Default-deny policy with an optional explicit decision callback."""

    def __init__(
        self,
        decide: Callable[[PermissionRequestEvent], Any] = None,
    ) -> None:
        self._decide = decide

    def decide(self, request: PermissionRequestEvent) -> PermissionDecision:
        if self._decide is None:
            return PermissionDecision.DENY
        try:
            return map_permission_decision(self._decide(request))
        except Exception:
            return PermissionDecision.DENY
