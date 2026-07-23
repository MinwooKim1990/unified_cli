"""Stable errors for the isolated extension lab.

These errors deliberately carry only a process exit code and a caller supplied
message.  They never contain command output, credentials, or provider details.
"""

from __future__ import annotations


class LabError(Exception):
    """Base class for stable, actionable lab failures."""

    exit_code = 4


class UsageStateError(LabError):
    """The requested input or lifecycle state is invalid."""

    exit_code = 2


class UnsupportedError(LabError):
    """The requested operation is deliberately not supported."""

    exit_code = 3


class InvariantRefusalError(LabError):
    """A safety invariant failed, so the lab must refuse the operation."""

    exit_code = 4


class RunnerFailureError(LabError):
    """An isolated runner failed after it was allowed to start."""

    exit_code = 5


class TestFailureError(LabError):
    """An isolated provider test completed unsuccessfully."""

    __test__ = False
    exit_code = 6


class CleanupIncompleteError(LabError):
    """Lab cleanup did not establish that all owned resources are gone."""

    exit_code = 7


# Short aliases retain the stable semantics when callers use the noun-first
# spelling in a command layer.
LabUsageError = UsageStateError
LabUnsupportedError = UnsupportedError
LabInvariantError = InvariantRefusalError
LabRunnerError = RunnerFailureError
LabTestFailureError = TestFailureError
LabCleanupIncompleteError = CleanupIncompleteError
