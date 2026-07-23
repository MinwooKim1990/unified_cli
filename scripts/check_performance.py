#!/usr/bin/env python3
"""Run deterministic, offline performance and fast-path readiness gates.

The harness itself uses only the Python standard library.  Every measured child
receives a disposable HOME/XDG/TMP/PATH, an allowlisted environment, a Python
socket guard, and no inherited credentials.  Core fast paths additionally use
an import canary so even a caught attempt to import ``unified_cli_ext`` fails the
gate.  The only executable exercised as a provider is the repository fixture.
"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import json
import math
import os
import pty
import select
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
DEFAULT_BASELINE = Path(__file__).with_name("performance-baseline-v1.json")
ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_CORE_VERSION = "0.5.3"
CANDIDATE_EXT_VERSION = "0.5.3"
REFERENCE_CORE_VERSION = "0.5.0"
REFERENCE_EXT_VERSION = "0.1.0"
_METRIC_NAMES = (
    "calibration_process_startup",
    "core_import",
    "core_version",
    "ext_import",
    "ext_passive_registry",
    "fake_cli_wrapper_overhead",
    "manage_bootstrap",
    "repl_first_prompt",
    "stream_relay",
)
_NORMALIZED_METRICS = frozenset({
    "core_import", "core_version", "ext_import", "ext_passive_registry",
    "repl_first_prompt",
})
_RATIO_NORMALIZED_METRICS = frozenset({"ext_import", "ext_passive_registry"})
REFERENCE_SHA = "be1478884735c862e894959944ba53e149ea4210"
REFERENCE_SOURCE_TREE_DIGEST = (
    "7f21edae7ab640afb342261ef4092586101edc9549661e391032ce6906fc04f4"
)
SOURCE_TREE_DIGEST_ALGORITHM = "sha256-path-content-v1"
SOURCE_TREES = (
    "src/unified_cli",
    "packages/unified-cli-ext/src/unified_cli_ext",
)
_CORE_ANCHORS = {"core_import": 48.606, "core_version": 94.635}
_EXT_IMPORT_ANCHOR = 52.066
_REGISTRY_ANCHOR = 195.661
_REPL_ANCHOR = 164.551
_CREDENTIAL_MARKERS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "REFRESH_TOKEN",
)


class PerformanceConfigError(ValueError):
    """The versioned performance baseline is absent or malformed."""


class MeasurementError(RuntimeError):
    """A benchmark could not prove its offline contract."""


@dataclass(frozen=True)
class _ManifestEntry:
    """One immutable source-tree directory or regular-file payload."""

    relative: str
    payload: Optional[bytes]


@dataclass(frozen=True)
class SourceManifest:
    """Source bytes captured once by the parent before any measured child."""

    entries: Tuple[_ManifestEntry, ...]
    digest: str


def _exact_number(value: object, label: str, *, positive: bool = True) -> float:
    if type(value) not in (int, float):
        raise PerformanceConfigError(label + " must be a number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise PerformanceConfigError(label + " is outside its allowed range")
    return number


def load_config(path: Path) -> Dict[str, Any]:
    """Load and strictly validate the complete v1 baseline (fail closed)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PerformanceConfigError("performance baseline is missing or unreadable") from exc
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceConfigError("performance baseline is not valid JSON") from exc
    if type(data) is not dict or set(data) != {
        "baseline_id", "baseline_source", "metrics", "reference", "schema_version",
    }:
        raise PerformanceConfigError("performance baseline has an invalid top-level shape")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise PerformanceConfigError("performance baseline schema version is unsupported")
    if type(data["baseline_id"]) is not str or not data["baseline_id"].strip():
        raise PerformanceConfigError("performance baseline id is invalid")
    if type(data["baseline_source"]) is not str or not data["baseline_source"].strip():
        raise PerformanceConfigError("performance baseline source is invalid")
    reference = data["reference"]
    if type(reference) is not dict or set(reference) != {
        "digest_algorithm", "sha", "source_tree_digest", "source_trees",
    }:
        raise PerformanceConfigError("performance reference is invalid")
    if reference["sha"] != REFERENCE_SHA:
        raise PerformanceConfigError("performance reference SHA is invalid")
    if reference["digest_algorithm"] != SOURCE_TREE_DIGEST_ALGORITHM:
        raise PerformanceConfigError("performance reference digest algorithm is invalid")
    if reference["source_trees"] != list(SOURCE_TREES):
        raise PerformanceConfigError("performance reference source trees are invalid")
    digest = reference["source_tree_digest"]
    if digest != REFERENCE_SOURCE_TREE_DIGEST:
        raise PerformanceConfigError("performance reference digest is invalid")
    metrics = data["metrics"]
    if type(metrics) is not dict or frozenset(metrics) != frozenset(_METRIC_NAMES):
        raise PerformanceConfigError("performance baseline metric set is incomplete")

    for name in _METRIC_NAMES:
        metric = metrics[name]
        required = {"samples", "statistic", "threshold", "warmups"}
        allowed = set(required)
        if name in {"core_import", "core_version"}:
            allowed.add("baseline_milliseconds")
        if name in _NORMALIZED_METRICS:
            allowed.add("normalization")
        if type(metric) is not dict or set(metric) != allowed:
            raise PerformanceConfigError(name + " has an invalid shape")
        if type(metric["samples"]) is not int or not 3 <= metric["samples"] <= 101:
            raise PerformanceConfigError(name + " sample count is invalid")
        if type(metric["warmups"]) is not int or not 0 <= metric["warmups"] <= 20:
            raise PerformanceConfigError(name + " warmup count is invalid")
        if (
            type(metric["statistic"]) is not str
            or metric["statistic"] not in {"median", "p95"}
        ):
            raise PerformanceConfigError(name + " statistic is invalid")
        threshold = metric["threshold"]
        if type(threshold) is not dict or type(threshold.get("kind")) is not str:
            raise PerformanceConfigError(name + " threshold is invalid")
        kind = threshold["kind"]
        if kind == "fixed":
            if set(threshold) != {"kind", "milliseconds"}:
                raise PerformanceConfigError(name + " fixed threshold is invalid")
            _exact_number(threshold["milliseconds"], name + " threshold")
        elif kind in {"baseline_regression", "raw_overhead"}:
            if set(threshold) != {
                "absolute_slack_milliseconds", "kind", "relative_slack",
            }:
                raise PerformanceConfigError(name + " regression threshold is invalid")
            _exact_number(
                threshold["absolute_slack_milliseconds"],
                name + " absolute slack",
            )
            relative = _exact_number(
                threshold["relative_slack"], name + " relative slack",
            )
            if relative > 1.0:
                raise PerformanceConfigError(name + " relative slack is invalid")
        else:
            raise PerformanceConfigError(name + " threshold kind is unsupported")
        if kind == "baseline_regression" and "baseline_milliseconds" not in metric:
            raise PerformanceConfigError(name + " baseline is missing")
        if "baseline_milliseconds" in metric:
            _exact_number(metric["baseline_milliseconds"], name + " baseline")

        if name in _NORMALIZED_METRICS:
            normalization = metric["normalization"]
            if type(normalization) is not dict or set(normalization) != {
                "anchor_milliseconds",
                "kind",
                "metric",
            }:
                raise PerformanceConfigError(name + " normalization is invalid")
            expected_kind = (
                "paired_same_metric_ratio"
                if name in _RATIO_NORMALIZED_METRICS
                else "paired_same_metric_reference"
            )
            if normalization["kind"] != expected_kind:
                raise PerformanceConfigError(name + " normalization kind is unsupported")
            if normalization["metric"] != name:
                raise PerformanceConfigError(name + " reference metric is invalid")
            anchor = _exact_number(
                normalization["anchor_milliseconds"], name + " reference anchor",
            )
            if name in _CORE_ANCHORS and anchor != _CORE_ANCHORS[name]:
                raise PerformanceConfigError(name + " reference anchor is invalid")
            if name == "ext_import" and anchor != _EXT_IMPORT_ANCHOR:
                raise PerformanceConfigError(name + " reference anchor is invalid")
            if name == "ext_passive_registry" and anchor != _REGISTRY_ANCHOR:
                raise PerformanceConfigError(name + " reference anchor is invalid")
            if name == "repl_first_prompt" and anchor != _REPL_ANCHOR:
                raise PerformanceConfigError(name + " reference anchor is invalid")
    for name, anchor in _CORE_ANCHORS.items():
        metric = metrics[name]
        if metric["baseline_milliseconds"] != anchor or metric["threshold"] != {
            "absolute_slack_milliseconds": 50.0,
            "kind": "baseline_regression",
            "relative_slack": 0.1,
        }:
            raise PerformanceConfigError(name + " policy is invalid")
    for name in ("ext_import", "ext_passive_registry"):
        if metrics[name]["threshold"] != {
            "kind": "fixed", "milliseconds": 250.0,
        }:
            raise PerformanceConfigError(name + " policy is invalid")
    if metrics["repl_first_prompt"]["threshold"] != {
        "kind": "fixed", "milliseconds": 300.0,
    }:
        raise PerformanceConfigError("repl_first_prompt policy is invalid")
    expected_shapes = {
        "core_import": (15, "median", 3),
        "core_version": (15, "median", 3),
        "ext_import": (15, "p95", 3),
        "ext_passive_registry": (61, "p95", 3),
        "repl_first_prompt": (31, "p95", 3),
    }
    for name, (samples, statistic, warmups) in expected_shapes.items():
        metric = metrics[name]
        if (
            metric["samples"] != samples
            or metric["statistic"] != statistic
            or metric["warmups"] != warmups
        ):
            raise PerformanceConfigError(name + " sampling policy is invalid")
    return data


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values or not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile input is invalid")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rounded(value: float) -> float:
    return round(float(value), 3)


def _observed(samples: Sequence[float], metric: Mapping[str, Any]) -> float:
    statistic = metric["statistic"]
    return (
        statistics.median(samples)
        if statistic == "median"
        else percentile(samples, 0.95)
    )


def _threshold(metric: Mapping[str, Any], *, raw_median: Optional[float] = None) -> float:
    policy = metric["threshold"]
    if policy["kind"] == "fixed":
        return float(policy["milliseconds"])
    if policy["kind"] == "baseline_regression":
        reference = float(metric["baseline_milliseconds"])
    elif raw_median is not None:
        reference = raw_median
    else:
        raise MeasurementError("raw overhead threshold is missing its reference")
    return reference + max(
        float(policy["absolute_slack_milliseconds"]),
        reference * float(policy["relative_slack"]),
    ) if policy["kind"] == "baseline_regression" else max(
        float(policy["absolute_slack_milliseconds"]),
        reference * float(policy["relative_slack"]),
    )


def summarize(
    samples: Sequence[float],
    metric: Mapping[str, Any],
    *,
    raw_median: Optional[float] = None,
    details: Optional[Mapping[str, Any]] = None,
    reference_before: Optional[Sequence[float]] = None,
    reference_after: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    if len(samples) != metric["samples"] or any(
        not math.isfinite(value) or value < 0 for value in samples
    ):
        raise MeasurementError("measurement returned an invalid sample set")
    median = statistics.median(samples)
    p95 = percentile(samples, 0.95)
    statistic = metric["statistic"]
    observed = _observed(samples, metric)
    policy_limit = _threshold(metric, raw_median=raw_median)
    normalization_details: Optional[Dict[str, Any]] = None
    normalization = metric.get("normalization")
    if normalization is not None:
        if (
            reference_before is None
            or reference_after is None
            or len(reference_before) != len(samples)
            or len(reference_after) != len(samples)
            or any(
                not math.isfinite(value) or value < 0
                for value in (*reference_before, *reference_after)
            )
        ):
            raise MeasurementError("paired reference normalization is invalid")
        anchor = float(normalization["anchor_milliseconds"])
        paired_references = [
            min(before, after)
            for before, after in zip(reference_before, reference_after)
        ]
        kind = normalization["kind"]
        if kind == "paired_same_metric_reference":
            adjustments = [
                max(0.0, reference - anchor)
                for reference in paired_references
            ]
            normalized_samples = [
                max(0.0, sample - delta)
                for sample, delta in zip(samples, adjustments)
            ]
        elif kind == "paired_same_metric_ratio":
            if any(reference <= 0.0 for reference in paired_references):
                raise MeasurementError("paired ratio reference is invalid")
            normalized_samples = [
                anchor * sample / reference
                for sample, reference in zip(samples, paired_references)
            ]
            if any(
                not math.isfinite(value) or value < 0
                for value in normalized_samples
            ):
                raise MeasurementError("paired ratio normalization is invalid")
        else:
            raise MeasurementError("paired reference normalization is unsupported")
        normalized_observed = _observed(normalized_samples, metric)
        normalization_details = {
            "anchor_ms": _rounded(anchor),
            "kind": kind,
            "normalized_observed_ms": _rounded(normalized_observed),
            "normalized_samples_ms": [
                _rounded(value) for value in normalized_samples
            ],
            "policy_threshold_ms": _rounded(policy_limit),
            "reference_after_ms": [_rounded(value) for value in reference_after],
            "reference_before_ms": [_rounded(value) for value in reference_before],
        }
        if kind == "paired_same_metric_reference":
            normalization_details["paired_adjustments_ms"] = [
                _rounded(value) for value in adjustments
            ]
        else:
            normalization_details["paired_reference_ms"] = [
                _rounded(value) for value in paired_references
            ]
        comparison_observed = normalized_observed
    elif reference_before is not None or reference_after is not None:
        raise MeasurementError("unexpected reference samples")
    else:
        comparison_observed = observed
    result: Dict[str, Any] = {
        "median_ms": _rounded(median),
        "observed_ms": _rounded(observed),
        "p95_ms": _rounded(p95),
        "passed": comparison_observed <= policy_limit,
        "samples_ms": [_rounded(value) for value in samples],
        "statistic": statistic,
        "threshold_ms": _rounded(policy_limit),
    }
    result_details = dict(details or {})
    if normalization_details is not None:
        result_details["reference_normalization"] = normalization_details
    if result_details:
        result["details"] = result_details
    return result


def _is_credential_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _CREDENTIAL_MARKERS)


def _write_guard(directory: Path) -> Path:
    marker = directory / "forbidden-startup-attempted"
    guard = '''\
import importlib.abc
import os
import socket
import ssl
import sys
import _thread

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

_real_socket = socket.socket
_writing_marker = False
_marker_path = os.environ.get("UNIFIED_PERF_GUARD_MARKER")
_forbid_mutations = os.environ.get("UNIFIED_PERF_FORBID_MUTATIONS") == "1"
_forbid_ctypes = os.environ.get("UNIFIED_PERF_FORBID_CTYPES") == "1"
_forbid_native_process_control = (
    os.environ.get("UNIFIED_PERF_FORBID_NATIVE_PROCESS_CONTROL") == "1"
)
_forbid_subprocesses = os.environ.get("UNIFIED_PERF_FORBID_SUBPROCESSES") == "1"
_forbid_core_imports = os.environ.get("UNIFIED_PERF_FORBID_CORE_IMPORTS") == "1"
_forbid_ext_imports = os.environ.get("UNIFIED_PERF_FORBID_EXT_IMPORTS") == "1"
_forbid_entrypoint_imports = (
    os.environ.get("UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS") == "1"
)
_process_scope = os.environ.get("UNIFIED_PERF_PROCESS_SCOPE")
_allowed_executable = os.environ.get("UNIFIED_PERF_ALLOWED_EXECUTABLE")
if _allowed_executable:
    _allowed_executable = os.path.realpath(_allowed_executable)
_writable_roots = tuple(
    os.path.realpath(item)
    for item in os.environ.get("UNIFIED_PERF_WRITABLE_ROOTS", "").split(os.pathsep)
    if item
)
_native_process_symbols = {
    "__syscall", "_Fork", "clearenv", "clone", "clone3", "dlsym",
    "execl", "execle", "execlp", "execv", "execve", "execvp", "execvpe",
    "fork", "popen", "posix_spawn", "posix_spawnp", "prctl", "putenv", "setenv",
    "setpgid", "setpgrp", "setsid", "syscall", "system", "unsetenv", "vfork",
}
_write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
if hasattr(os, "O_TMPFILE"):
    _write_flags |= os.O_TMPFILE
_open_dir_fds = {}

def _offline(*args, **kwargs):
    raise RuntimeError("network disabled by performance harness")

def _guarded_socket(family=socket.AF_INET, *args, **kwargs):
    if family not in (socket.AF_UNIX,):
        raise RuntimeError("network disabled by performance harness")
    return _real_socket(family, *args, **kwargs)

socket.socket = _guarded_socket
socket.create_connection = _offline
socket.getaddrinfo = _offline
ssl.wrap_socket = _offline

def _mark(kind, detail):
    global _writing_marker
    if _marker_path and not _writing_marker:
        _writing_marker = True
        try:
            with open(_marker_path, "a", encoding="utf-8") as handle:
                handle.write(kind + ":" + os.path.basename(str(detail)) + "\\n")
        except OSError:
            pass
        finally:
            _writing_marker = False

def _fd_path(fd):
    if type(fd) is not int or fd < 0:
        return None
    if _fcntl is not None and hasattr(_fcntl, "F_GETPATH"):
        try:
            raw = _fcntl.fcntl(fd, _fcntl.F_GETPATH, bytes(1024))
            value = os.fsdecode(raw.split(b"\\0", 1)[0])
            if value:
                return os.path.realpath(value)
        except (OSError, TypeError, ValueError):
            pass
    for root in ("/proc/self/fd", "/dev/fd"):
        try:
            value = os.readlink(os.path.join(root, str(fd)))
        except OSError:
            continue
        if os.path.isabs(value):
            return os.path.realpath(value)
    return None

def _resolve_mutation_path(path, dir_fd=None):
    if type(path) is int:
        return _fd_path(path)
    try:
        value = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        return None
    if os.path.isabs(value):
        return os.path.realpath(value)
    if dir_fd not in (None, -1):
        root = _fd_path(dir_fd)
        if root is None:
            return None
        value = os.path.join(root, value)
    return os.path.realpath(value)

def _current_open_dir_fd():
    stack = _open_dir_fds.get(_thread.get_ident())
    return stack[-1] if stack else None

def _mutation_details(event, args):
    if event == "open":
        path = args[0] if args else "unknown"
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writes = (
            isinstance(mode, str) and any(char in mode for char in "wax+")
        ) or (isinstance(flags, int) and bool(flags & _write_flags))
        if writes:
            return ((path, _current_open_dir_fd()),)
        return ()
    single_path_events = {
        "os.chflags": None, "os.chmod": 2, "os.chown": 3,
        "os.lchflags": None, "os.mkdir": 2, "os.mknod": 3,
        "os.remove": 1, "os.removexattr": None, "os.rmdir": 1,
        "os.setxattr": None, "os.truncate": None, "os.unlink": 1,
        "os.utime": 3,
    }
    if event in {"os.link", "os.rename", "os.replace"}:
        return (
            (args[0], args[2] if len(args) > 2 else None),
            (args[1], args[3] if len(args) > 3 else None),
        ) if len(args) > 1 else (("unknown", None),)
    if event == "os.symlink":
        return ((args[1], args[2] if len(args) > 2 else None),) if len(args) > 1 else (("unknown", None),)
    if event not in single_path_events:
        return ()
    dir_index = single_path_events[event]
    dir_fd = args[dir_index] if dir_index is not None and len(args) > dir_index else None
    return ((args[0], dir_fd),) if args else (("unknown", None),)

def _mutation_allowed(path, dir_fd=None):
    resolved = _resolve_mutation_path(path, dir_fd)
    if resolved is None:
        return False
    if resolved == os.path.realpath(os.devnull):
        return True
    for root in _writable_roots:
        try:
            if os.path.commonpath((root, resolved)) == root:
                return True
        except ValueError:
            continue
    return False

_real_open = os.open

class _OpenGuard:
    # A callable instance deliberately has no descriptor binding behavior.
    # Python 3.9's pathlib stores os.open on _NormalAccessor; assigning a plain
    # Python function here would bind that function and inject the accessor as
    # an extra first argument.
    def __call__(self, path, flags, mode=0o777, *, dir_fd=None):
        thread_id = _thread.get_ident()
        stack = _open_dir_fds.setdefault(thread_id, [])
        stack.append(dir_fd)
        try:
            if (
                _forbid_mutations
                and isinstance(flags, int)
                and flags & _write_flags
                and not _mutation_allowed(path, dir_fd)
            ):
                _mark("mutation", path)
                raise RuntimeError("filesystem mutation disabled by performance harness")
            return _real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            stack.pop()
            if not stack:
                _open_dir_fds.pop(thread_id, None)

_guarded_open = _OpenGuard()
os.open = _guarded_open
try:
    import posix as _posix
except ImportError:
    _posix = None
if _posix is not None:
    _posix.open = _guarded_open

def _launch_environment(event, args):
    if event == "subprocess.Popen":
        return args[3] if len(args) > 3 else None
    if event in {"os.exec", "os.posix_spawn", "os.posix_spawnp"}:
        return args[2] if len(args) > 2 else None
    if event == "os.spawn":
        return args[3] if len(args) > 3 else None
    return None

def _scope_is_preserved(event, args):
    environment = _launch_environment(event, args)
    if environment is None:
        environment = os.environ
    try:
        return (
            _process_scope is not None
            and environment.get("UNIFIED_PERF_PROCESS_SCOPE") == _process_scope
        )
    except AttributeError:
        return False

def _normalized_executable(value):
    try:
        return os.path.realpath(os.fsdecode(os.fspath(value)))
    except (TypeError, ValueError):
        return None

def _fork_exec_scope_is_preserved(environment):
    if environment is None:
        return (
            _process_scope is not None
            and os.environ.get("UNIFIED_PERF_PROCESS_SCOPE") == _process_scope
        )
    try:
        expected = os.fsencode(_process_scope) if _process_scope is not None else None
        values = []
        for item in environment:
            name, separator, value = os.fsencode(item).partition(b"=")
            if separator and name == b"UNIFIED_PERF_PROCESS_SCOPE":
                values.append(value)
        return expected is not None and values == [expected]
    except (TypeError, ValueError):
        return False

def _guard_fork_exec(args):
    executable_list = args[1] if len(args) > 1 else ()
    try:
        executables = tuple(executable_list)
    except TypeError:
        executables = ()
    executable = executables[0] if executables else "unknown"
    if not (
        _allowed_executable
        and len(executables) == 1
        and _normalized_executable(executable) == _allowed_executable
    ):
        _mark("subprocess", executable)
        raise RuntimeError("provider subprocess disabled by performance harness")
    environment = args[5] if len(args) > 5 else ()
    if not _fork_exec_scope_is_preserved(environment):
        _mark("process-scope", executable)
        raise RuntimeError("process scope removal disabled by performance harness")

try:
    import _posixsubprocess as _guarded_posixsubprocess
except ImportError:
    _guarded_posixsubprocess = None
_posixsubprocess_origin = None
if _guarded_posixsubprocess is not None:
    _posixsubprocess_spec = getattr(_guarded_posixsubprocess, "__spec__", None)
    _posixsubprocess_origin_value = getattr(
        _posixsubprocess_spec, "origin", None,
    )
    if _posixsubprocess_origin_value not in (None, "built-in", "frozen"):
        try:
            _posixsubprocess_origin = os.path.realpath(
                os.fsdecode(os.fspath(_posixsubprocess_origin_value))
            )
        except (TypeError, ValueError):
            pass
    _real_fork_exec = _guarded_posixsubprocess.fork_exec

    def _guarded_fork_exec(*args, **kwargs):
        if _forbid_subprocesses:
            _guard_fork_exec(args)
        return _real_fork_exec(*args, **kwargs)

    _guarded_posixsubprocess.fork_exec = _guarded_fork_exec
    _loaded_subprocess = sys.modules.get("subprocess")
    if getattr(_loaded_subprocess, "_fork_exec", None) is _real_fork_exec:
        _loaded_subprocess._fork_exec = _guarded_fork_exec

def _is_posixsubprocess_module(name, origin=None):
    try:
        fullname = os.fsdecode(os.fspath(name))
    except (TypeError, ValueError):
        return False
    if fullname.rpartition(".")[2] == "_posixsubprocess":
        return True
    if _posixsubprocess_origin is None or origin is None:
        return False
    try:
        return os.path.realpath(os.fsdecode(os.fspath(origin))) == (
            _posixsubprocess_origin
        )
    except (TypeError, ValueError):
        return False

def _block_process_control(*args, **kwargs):
    _mark("process-scope", "session-control")
    raise RuntimeError("process scope escape disabled by performance harness")

if _forbid_native_process_control:
    import posix as _posix
    for _module in (os, _posix):
        for _name in ("setpgid", "setpgrp", "setsid"):
            if hasattr(_module, _name):
                setattr(_module, _name, _block_process_control)

def _audit(event, args):
    if _writing_marker:
        return
    if (
        _forbid_subprocesses
        and event == "import"
        and args
        and _is_posixsubprocess_module(
            args[0], args[1] if len(args) > 1 else None,
        )
    ):
        _mark("import", args[0])
        raise RuntimeError(
            "process launch module reload disabled by performance harness"
        )
    if _forbid_mutations:
        for detail, dir_fd in _mutation_details(event, args):
            if not _mutation_allowed(detail, dir_fd):
                _mark("mutation", detail)
                raise RuntimeError("filesystem mutation disabled by performance harness")
        if event in {"os.fork", "os.forkpty"}:
            _mark("fork", event)
            raise RuntimeError("fork disabled by performance harness")
        if event in {"socket.bind", "socket.connect"}:
            address = args[1] if len(args) > 1 else "unknown"
            _mark("socket", address)
            raise RuntimeError("AF_UNIX endpoint disabled by performance harness")
    if (
        _forbid_ctypes
        and event == "import"
        and args
        and args[0] in {"ctypes", "_ctypes"}
    ):
        _mark("import", args[0])
        raise RuntimeError("native access disabled by performance harness")
    if _forbid_native_process_control:
        if event == "import" and args and args[0] in {"cffi", "_cffi_backend"}:
            _mark("native-process", args[0])
            raise RuntimeError("native process control disabled by performance harness")
        if event in {"ctypes.dlsym", "ctypes.dlsym/handle"} and args:
            symbol = str(args[-1])
            if symbol in _native_process_symbols:
                _mark("native-process", symbol)
                raise RuntimeError(
                    "native process control disabled by performance harness"
                )
    if _forbid_subprocesses:
        if event == "subprocess.Popen":
            executable = args[0] if args else "unknown"
        elif event in {
            "os.exec", "os.posix_spawn", "os.posix_spawnp", "os.spawn",
            "os.system", "pty.spawn",
        }:
            executable_index = 1 if event == "os.spawn" else 0
            executable = (
                args[executable_index]
                if len(args) > executable_index
                else "unknown"
            )
        else:
            return
        if (
            _allowed_executable
            and _normalized_executable(executable) == _allowed_executable
        ):
            if not _scope_is_preserved(event, args):
                _mark("process-scope", executable)
                raise RuntimeError(
                    "process scope removal disabled by performance harness"
                )
            return
        _mark("subprocess", executable)
        raise RuntimeError("provider subprocess disabled by performance harness")

sys.addaudithook(_audit)

class _ImportCanary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _forbid_subprocesses and _is_posixsubprocess_module(fullname):
            _mark("import", fullname)
            raise RuntimeError(
                "process launch module reload disabled by performance harness"
            )
        forbidden_core = (
            _forbid_core_imports
            and (fullname == "unified_cli" or fullname.startswith("unified_cli."))
        )
        forbidden_ext = (
            _forbid_ext_imports
            and (fullname == "unified_cli_ext" or fullname.startswith("unified_cli_ext."))
        )
        forbidden_entrypoint = (
            _forbid_entrypoint_imports
            and fullname == "performance_canary"
        )
        if forbidden_core or forbidden_ext or forbidden_entrypoint:
            _mark("import", fullname)
        return None

sys.meta_path.insert(0, _ImportCanary())
'''
    (directory / "sitecustomize.py").write_text(guard, encoding="utf-8")
    (directory / "performance_canary.py").write_text(
        "raise RuntimeError('passive registry imported its entry point')\n",
        encoding="utf-8",
    )
    dist_info = directory / "performance_canary-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: performance-canary\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[unified_cli.providers.v1]\n"
        "performance-canary = performance_canary:PLUGIN\n",
        encoding="utf-8",
    )
    return marker


_PROTECTED_PATHS_ENV = "UNIFIED_PERF_PROTECTED_PATHS"
_PROCESS_SCOPE_ENV = "UNIFIED_PERF_PROCESS_SCOPE"


def _protected_path_digest(
    path: Path, *, ignore_guard_marker: bool = False,
) -> str:
    """Hash one parent-owned sandbox path without following aliases."""
    root = path.absolute()
    digest = hashlib.sha256()

    def visit(current: Path, relative: str) -> None:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise MeasurementError("performance sandbox integrity check failed") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        stamp = metadata.st_mtime_ns
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(
                b"D\0" + relative.encode("utf-8") + b"\0"
                + str(mode).encode("ascii") + b"\0"
            )
            try:
                children = sorted(os.scandir(current), key=lambda item: item.name)
            except OSError as exc:
                raise MeasurementError(
                    "performance sandbox integrity check failed"
                ) from exc
            for child in children:
                if (
                    ignore_guard_marker
                    and relative == "."
                    and child.name == "forbidden-startup-attempted"
                ):
                    continue
                child_relative = child.name if relative == "." else (
                    relative + "/" + child.name
                )
                visit(Path(child.path), child_relative)
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise MeasurementError("performance sandbox integrity check failed")
        digest.update(
            b"F\0" + relative.encode("utf-8") + b"\0"
            + str(mode).encode("ascii") + b"\0"
            + str(stamp).encode("ascii") + b"\0"
            + str(metadata.st_size).encode("ascii") + b"\0"
        )
        try:
            with current.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise MeasurementError("performance sandbox integrity check failed") from exc

    visit(root, ".")
    return digest.hexdigest()


def _protect_environment_path(
    env: Mapping[str, str], label: str, path: Path, *, guard: bool = False,
) -> Dict[str, str]:
    child = dict(env)
    try:
        protections = json.loads(child.get(_PROTECTED_PATHS_ENV, "[]"))
    except json.JSONDecodeError as exc:
        raise MeasurementError("performance sandbox integrity metadata is invalid") from exc
    if type(protections) is not list:
        raise MeasurementError("performance sandbox integrity metadata is invalid")
    resolved = str(path.absolute())
    for item in protections:
        if type(item) is not list or len(item) != 4:
            raise MeasurementError("performance sandbox integrity metadata is invalid")
        if item[1] == resolved:
            return child
    protections.append([
        label,
        resolved,
        _protected_path_digest(path, ignore_guard_marker=guard),
        guard,
    ])
    child[_PROTECTED_PATHS_ENV] = json.dumps(protections, separators=(",", ":"))
    return child


def _assert_environment_integrity(env: Mapping[str, str]) -> None:
    try:
        protections = json.loads(env.get(_PROTECTED_PATHS_ENV, ""))
    except json.JSONDecodeError as exc:
        raise MeasurementError("performance sandbox integrity metadata is invalid") from exc
    if type(protections) is not list or not protections:
        raise MeasurementError("performance sandbox integrity metadata is invalid")
    for item in protections:
        if (
            type(item) is not list
            or len(item) != 4
            or type(item[0]) is not str
            or type(item[1]) is not str
            or type(item[2]) is not str
            or type(item[3]) is not bool
        ):
            raise MeasurementError("performance sandbox integrity metadata is invalid")
        actual = _protected_path_digest(
            Path(item[1]), ignore_guard_marker=item[3],
        )
        if actual != item[2]:
            raise MeasurementError("performance sandbox integrity check failed")


def _excluded_source_path(path: Path) -> bool:
    return (
        "__pycache__" in path.parts
        or path.suffix in {".pyc", ".pyo"}
        or any(part.endswith((".egg-info", ".dist-info")) for part in path.parts)
    )


def _manifest_digest(entries: Sequence[_ManifestEntry]) -> str:
    digest = hashlib.sha256()
    consumed = set()
    for source_tree in SOURCE_TREES:
        prefix = source_tree + "/"
        bucket = [
            entry for entry in entries
            if entry.relative == source_tree or entry.relative.startswith(prefix)
        ]
        for entry in sorted(bucket, key=lambda item: item.relative):
            consumed.add(entry.relative)
            encoded = entry.relative.encode("utf-8")
            if entry.payload is None:
                digest.update(b"D\0" + encoded + b"\0")
            else:
                digest.update(
                    b"F\0" + encoded + b"\0"
                    + str(len(entry.payload)).encode("ascii") + b"\0"
                    + entry.payload
                )
    if len(consumed) != len(entries):
        raise MeasurementError("source manifest contains an undeclared path")
    return digest.hexdigest()


def _read_regular_file(directory_fd: int, name: str, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MeasurementError("source tree changed while it was inspected") from exc
    try:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise MeasurementError("source tree contains a special file")
        chunks: List[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        stable_fields = (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise MeasurementError("source tree changed while it was inspected")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise MeasurementError("source tree changed while it was inspected")
        return payload
    finally:
        os.close(file_fd)


def _open_directory(directory_fd: int, name: str, expected: os.stat_result) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        child_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MeasurementError("source tree changed while it was inspected") from exc
    observed = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        os.close(child_fd)
        raise MeasurementError("source tree contains a special file")
    return child_fd


def _scan_manifest_directory(
    directory_fd: int,
    relative: str,
    entries: List[_ManifestEntry],
    *,
    include: bool,
) -> None:
    if include:
        entries.append(_ManifestEntry(relative, None))
    try:
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise MeasurementError("source tree is unreadable") from exc
    for child in children:
        child_relative = relative + "/" + child.name
        try:
            observed = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise MeasurementError("source tree changed while it was inspected") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise MeasurementError("source tree contains a symlink")
        excluded = _excluded_source_path(Path(child_relative))
        if stat.S_ISDIR(observed.st_mode):
            child_fd = _open_directory(directory_fd, child.name, observed)
            try:
                _scan_manifest_directory(
                    child_fd,
                    child_relative,
                    entries,
                    include=include and not excluded,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(observed.st_mode):
            payload = _read_regular_file(directory_fd, child.name, observed)
            if include and not excluded:
                entries.append(_ManifestEntry(child_relative, payload))
        else:
            raise MeasurementError("source tree contains a special file")


def _require_expected_version_pair(
    expected_core_version: object, expected_ext_version: object,
) -> Tuple[str, str]:
    pair = (expected_core_version, expected_ext_version)
    if (
        type(expected_core_version) is not str
        or type(expected_ext_version) is not str
        or (
            pair != (CANDIDATE_CORE_VERSION, CANDIDATE_EXT_VERSION)
            and pair != (REFERENCE_CORE_VERSION, REFERENCE_EXT_VERSION)
        )
    ):
        raise MeasurementError("source version expectation is invalid")
    return expected_core_version, expected_ext_version


def _read_source_manifest(
    root: Path, *,
    expected_core_version: str,
    expected_ext_version: str,
) -> SourceManifest:
    """Capture verified bytes using no-follow descriptor-relative traversal."""
    expected_core_version, expected_ext_version = _require_expected_version_pair(
        expected_core_version, expected_ext_version,
    )
    root = root.absolute()
    try:
        root_lstat = os.lstat(root)
    except OSError as exc:
        raise MeasurementError("source root is missing or unreadable") from exc
    if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(root_lstat.st_mode):
        raise MeasurementError("source root is not a real directory")
    if root.resolve() != root:
        raise MeasurementError("source root is not a real directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(str(root), flags)
    except OSError as exc:
        raise MeasurementError("source root is missing or unreadable") from exc
    entries: List[_ManifestEntry] = []
    try:
        opened_root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino)
            != (root_lstat.st_dev, root_lstat.st_ino)
        ):
            raise MeasurementError("source root changed while it was inspected")
        for relative in SOURCE_TREES:
            current_fd = os.dup(root_fd)
            try:
                for component in relative.split("/"):
                    try:
                        observed = os.stat(
                            component, dir_fd=current_fd, follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise MeasurementError("source tree is missing or unreadable") from exc
                    if stat.S_ISLNK(observed.st_mode):
                        raise MeasurementError("source tree is missing or symlinked")
                    next_fd = _open_directory(current_fd, component, observed)
                    os.close(current_fd)
                    current_fd = next_fd
                _scan_manifest_directory(
                    current_fd, relative, entries, include=True,
                )
            finally:
                os.close(current_fd)
    finally:
        os.close(root_fd)
    manifest = SourceManifest(tuple(entries), _manifest_digest(entries))
    _require_manifest_version(
        manifest, "src/unified_cli/__init__.py", expected_core_version,
    )
    _require_manifest_version(
        manifest,
        "packages/unified-cli-ext/src/unified_cli_ext/__init__.py",
        expected_ext_version,
    )
    return manifest


def _require_manifest_version(
    manifest: SourceManifest, relative: str, expected: str,
) -> None:
    payload = next(
        (entry.payload for entry in manifest.entries if entry.relative == relative),
        None,
    )
    if payload is None:
        raise MeasurementError("source version proof failed")
    try:
        module = ast.parse(payload.decode("utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise MeasurementError("source version proof failed") from exc
    versions = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if versions != [expected]:
        raise MeasurementError("source version proof failed")


def source_tree_digest(
    root: Path, *,
    expected_core_version: str,
    expected_ext_version: str,
) -> str:
    """Hash sorted POSIX paths and file bytes from the two versioned trees.

    ``sha256-path-content-v1`` writes ``D\\0<path>\\0`` for directories and
    ``F\\0<path>\\0<size>\\0<bytes>`` for regular files. Cache/bytecode and
    packaging metadata directories are excluded from both hashing and copying.
    """
    return _read_source_manifest(
        root,
        expected_core_version=expected_core_version,
        expected_ext_version=expected_ext_version,
    ).digest


def _safe_source_tree(root: Path) -> Path:
    """Compatibility validator backed by the descriptor-safe manifest reader."""
    _read_source_manifest(
        root,
        expected_core_version=CANDIDATE_CORE_VERSION,
        expected_ext_version=CANDIDATE_EXT_VERSION,
    )
    return root.absolute()


def _reference_head_sha(root: Path) -> str:
    marker = root / ".git"
    if marker.is_symlink():
        raise MeasurementError("reference Git metadata is symlinked")
    if marker.is_dir():
        git_dir = marker.resolve()
    elif marker.is_file():
        try:
            declaration = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise MeasurementError("reference Git metadata is unreadable") from exc
        if not declaration.startswith("gitdir: "):
            raise MeasurementError("reference Git metadata is malformed")
        git_dir = Path(declaration[8:])
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise MeasurementError("reference Git directory is invalid")
        git_dir = git_dir.resolve()
    else:
        raise MeasurementError("reference Git metadata is missing")
    head = git_dir / "HEAD"
    if head.is_symlink() or not head.is_file():
        raise MeasurementError("reference HEAD proof is missing")
    try:
        value = head.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise MeasurementError("reference HEAD proof is unreadable") from exc
    if (
        len(value) != 40
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise MeasurementError("reference checkout is not at a detached full SHA")
    return value


def _validate_reference_manifest(
    root: Path, *, expected_sha: str, expected_digest: str,
) -> SourceManifest:
    """Validate explicit expectations and retain the exact verified bytes."""
    root = root.absolute()
    if _reference_head_sha(root) != expected_sha:
        raise MeasurementError("reference checkout SHA mismatch")
    manifest = _read_source_manifest(
        root,
        expected_core_version=REFERENCE_CORE_VERSION,
        expected_ext_version=REFERENCE_EXT_VERSION,
    )
    if manifest.digest != expected_digest:
        raise MeasurementError("reference source-tree digest mismatch")
    return manifest


def load_reference_manifest(
    root: Path, config: Mapping[str, Any],
) -> SourceManifest:
    reference = config["reference"]
    return _validate_reference_manifest(
        root,
        expected_sha=reference["sha"],
        expected_digest=reference["source_tree_digest"],
    )


def validate_reference_root(root: Path, config: Mapping[str, Any]) -> Path:
    load_reference_manifest(root, config)
    return root.absolute()


def _materialize_manifest(
    manifest: SourceManifest, destination: Path, *, registry: bool = False,
) -> Path:
    if destination.exists():
        raise MeasurementError("sanitized source destination already exists")
    try:
        destination.mkdir(mode=0o700)
        for entry in manifest.entries:
            relative = entry.relative
            if registry:
                mapped: Optional[str] = None
                for source_tree in SOURCE_TREES:
                    if relative == source_tree:
                        mapped = Path(source_tree).name
                        break
                    prefix = source_tree + "/"
                    if relative.startswith(prefix):
                        mapped = Path(source_tree).name + "/" + relative[len(prefix):]
                        break
                if mapped is None:
                    continue
                relative = mapped
            target = destination / relative
            if entry.payload is None:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                file_fd = os.open(str(target), flags, 0o600)
                try:
                    view = memoryview(entry.payload)
                    while view:
                        written = os.write(file_fd, view)
                        if written <= 0:
                            raise OSError("short write")
                        view = view[written:]
                finally:
                    os.close(file_fd)
    except OSError as exc:
        raise MeasurementError("could not create sanitized source tree") from exc
    return destination.resolve()


def _build_registry_sandbox(root: Path, destination: Path) -> Path:
    return _materialize_manifest(
        _read_source_manifest(
            root,
            expected_core_version=CANDIDATE_CORE_VERSION,
            expected_ext_version=CANDIDATE_EXT_VERSION,
        ),
        destination,
        registry=True,
    )


@contextmanager
def isolated_environment(root: Path = ROOT) -> Iterator[Tuple[Dict[str, str], Path, Path]]:
    """Yield an allowlisted environment, import marker, and fixture path."""
    with tempfile.TemporaryDirectory(prefix="unified-cli-performance-") as raw:
        base = Path(raw)
        home = base / "home"
        tmp = base / "tmp"
        binaries = base / "bin"
        child_cwd = base / "empty-cwd"
        guard = base / "guard"
        workspace = base / "workspace"
        for directory in (home, tmp, binaries, child_cwd, guard, workspace):
            directory.mkdir(mode=0o700)
        marker = _write_guard(guard)
        source = root / "tests" / "fixtures" / "core_provider_cli.py"
        fixture = binaries / "fixture-provider-cli"
        payload = source.read_text(encoding="utf-8")
        payload = payload.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
        fixture.write_text(payload, encoding="utf-8")
        fixture.chmod(0o700)
        runtime_paths = []
        for item in sys.path:
            if not item:
                continue
            candidate = Path(item).resolve()
            parts = set(candidate.parts)
            if (
                candidate.is_dir()
                and ("site-packages" in parts or "dist-packages" in parts)
                and str(candidate) not in runtime_paths
            ):
                runtime_paths.append(str(candidate))
        env = {
            "COLUMNS": "100",
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "LINES": "30",
            "NO_COLOR": "1",
            "PATH": str(binaries),
            "PROMPT_TOOLKIT_NO_CPR": "1",
            "TERM": "xterm-256color",
            "TMPDIR": str(tmp),
            "UNIFIED_CLI_DISABLE_PLUGINS": "1",
            "UNIFIED_CLI_LANG": "en",
            "UNIFIED_PERF_EMPTY_CWD": str(child_cwd),
            "UNIFIED_PERF_GUARD_MARKER": str(marker),
            "UNIFIED_PERF_GUARD_ROOT": str(guard),
            "UNIFIED_PERF_RUNTIME_PATHS": os.pathsep.join(runtime_paths),
            "UNIFIED_PERF_WRITABLE_ROOTS": os.pathsep.join(
                str(path) for path in (home, tmp, workspace)
            ),
            "UNIFIED_PERF_WORKSPACE": str(workspace),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
        env = _protect_environment_path(env, "guard", guard, guard=True)
        env = _protect_environment_path(env, "fixture", fixture)
        if any(_is_credential_name(name) for name in env):
            raise MeasurementError("isolated environment contains a credential-like name")
        _verify_process_scope_inspection(env)
        yield env, marker, fixture


def _record_parent_guard_violation(
    env: Mapping[str, str], kind: str, detail: str,
) -> None:
    marker = env.get("UNIFIED_PERF_GUARD_MARKER")
    if not marker:
        return
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(marker, flags, 0o600)
        try:
            os.write(fd, (kind + ":" + detail + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _linux_process_scope_pids(
    scope: str, proc_root: Path = Path("/proc"),
) -> List[int]:
    marker = (_PROCESS_SCOPE_ENV + "=" + scope).encode("ascii")
    matches: List[int] = []
    try:
        entries = os.scandir(proc_root)
    except OSError as exc:
        raise MeasurementError("isolated process scope inspection failed") from exc
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in {0, 1, os.getpid()}:
                continue
            try:
                payload = (Path(entry.path) / "environ").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            except OSError as exc:
                raise MeasurementError(
                    "isolated process scope inspection failed"
                ) from exc
            if marker in payload.split(b"\0"):
                matches.append(pid)
    return matches


def _darwin_process_scope_pids(scope: str) -> List[int]:
    # ``KERN_PROCARGS2`` exposes the environment installed at exec, so an
    # ordinary ``unsetenv`` cannot make an already-created process disappear
    # from this parent-owned scope inventory.
    import ctypes
    import ctypes.util

    try:
        libproc = ctypes.CDLL(
            ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
            use_errno=True,
        )
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise MeasurementError("isolated process scope inspection failed") from exc
    libproc.proc_listallpids.argtypes = (ctypes.c_void_p, ctypes.c_int)
    libproc.proc_listallpids.restype = ctypes.c_int
    libc.sysctl.argtypes = (
        ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p, ctypes.c_size_t,
    )
    libc.sysctl.restype = ctypes.c_int

    capacity = max(64, libproc.proc_listallpids(None, 0) + 64)
    pids = (ctypes.c_int * capacity)()
    count = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    if count < 0:
        raise MeasurementError("isolated process scope inspection failed")
    marker = (_PROCESS_SCOPE_ENV + "=" + scope).encode("ascii")
    matches: List[int] = []
    for pid in pids[:min(count, capacity)]:
        if pid in {0, 1, os.getpid()}:
            continue
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            continue
        if size.value <= 0:
            continue
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(
            mib, 3, buffer, ctypes.byref(size), None, 0,
        ) != 0:
            continue
        payload = buffer.raw[:size.value]
        if marker in payload.split(b"\0"):
            matches.append(pid)
    return matches


def _process_scope_pids(scope: str) -> List[int]:
    if sys.platform.startswith("linux"):
        return _linux_process_scope_pids(scope)
    if sys.platform == "darwin":
        return _darwin_process_scope_pids(scope)
    return []


def _verify_process_scope_inspection(env: Mapping[str, str]) -> None:
    """Fail before measurement if the host cannot inventory a scoped child."""
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return
    scope = os.urandom(16).hex()
    child_env = dict(env)
    child_env[_PROCESS_SCOPE_ENV] = scope
    process: Optional[subprocess.Popen] = None
    verified = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(5)"],
            cwd=env["UNIFIED_PERF_EMPTY_CWD"],
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if process.pid in _process_scope_pids(scope):
                verified = True
                break
            if process.poll() is not None:
                break
            time.sleep(0.01)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError("isolated process scope inspection failed") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    if not verified:
        raise MeasurementError("isolated process scope inspection failed")


def _kill_process_scope(
    scope: str, process: Optional[subprocess.Popen] = None,
) -> bool:
    """Stop every live process carrying this invocation's inherited scope."""
    found = False
    deadline = time.monotonic() + 2.0
    while True:
        pids = _process_scope_pids(scope)
        if process is not None and process.poll() is not None:
            pids = [pid for pid in pids if pid != process.pid]
        if not pids:
            return found
        found = True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise MeasurementError(
                    "isolated process scope cleanup failed"
                ) from exc
        if process is not None and process.pid in pids:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if time.monotonic() >= deadline:
            raise MeasurementError("isolated process scope cleanup failed")
        time.sleep(0.01)


def _kill_process_group(process: subprocess.Popen) -> bool:
    """Kill the isolated process group, including descendants after leader exit."""
    if os.name != "posix":
        if process.poll() is None:
            process.kill()
            return True
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise MeasurementError("isolated process group cleanup failed") from exc
    return True


def _run(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    timeout: float = 10.0,
    input_bytes: Optional[bytes] = None,
    cwd: Optional[Path] = None,
) -> Tuple[float, bytes]:
    _assert_environment_integrity(env)
    child_env = dict(env)
    process_scope = os.urandom(16).hex()
    child_env[_PROCESS_SCOPE_ENV] = process_scope
    child_env["UNIFIED_PERF_FORBID_NATIVE_PROCESS_CONTROL"] = "1"
    child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    start = time.perf_counter_ns()
    process: Optional[subprocess.Popen] = None
    payload = b""
    failure: Optional[BaseException] = None
    group_was_present = False
    scope_was_present = False
    measured_end: Optional[int] = None
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(Path(env["UNIFIED_PERF_EMPTY_CWD"]) if cwd is None else cwd),
            env=child_env,
            stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        payload, _ = process.communicate(input=input_bytes, timeout=timeout)
        measured_end = time.perf_counter_ns()
    except (OSError, subprocess.SubprocessError) as exc:
        failure = exc
    finally:
        if process is not None:
            try:
                group_was_present = _kill_process_group(process)
                scope_was_present = _kill_process_scope(process_scope, process)
                if process.poll() is None:
                    process.communicate(timeout=2.0)
            except (OSError, subprocess.SubprocessError, MeasurementError) as exc:
                if failure is None:
                    failure = exc
        integrity_failure: Optional[MeasurementError] = None
        try:
            _assert_environment_integrity(env)
        except MeasurementError as exc:
            integrity_failure = exc
        if group_was_present or scope_was_present:
            detail = "process-scope" if scope_was_present else "process-group"
            _record_parent_guard_violation(env, "descendant", detail)
        if integrity_failure is not None:
            raise integrity_failure
    if failure is not None:
        if isinstance(failure, MeasurementError):
            raise failure
        raise MeasurementError("isolated subprocess did not complete") from failure
    if group_was_present or scope_was_present:
        raise MeasurementError("isolated subprocess left a descendant process")
    if process is None or process.returncode != 0:
        raise MeasurementError("isolated subprocess returned a failure status")
    if measured_end is None:
        raise MeasurementError("isolated subprocess did not record its duration")
    elapsed = (measured_end - start) / 1_000_000.0
    return elapsed, payload


def _float_output(payload: bytes) -> float:
    try:
        return float(payload.decode("ascii").strip())
    except (UnicodeError, ValueError) as exc:
        raise MeasurementError("measurement subprocess returned malformed output") from exc


def _json_output(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MeasurementError("measurement subprocess returned malformed JSON") from exc


def _distribution_inventory(
    distributions: Sequence[Any],
) -> Tuple[List[Tuple[Any, Any]], List[Tuple[Any, Any, Any, Any]]]:
    """Return an exact inventory without the version-sensitive global EP API."""
    packages = sorted(
        (distribution.metadata.get("Name"), distribution.version)
        for distribution in distributions
    )
    entry_points = sorted(
        (
            entry_point.group,
            entry_point.name,
            entry_point.value,
            distribution.metadata.get("Name"),
        )
        for distribution in distributions
        for entry_point in distribution.entry_points
    )
    return packages, entry_points


_ISOLATED_BOOTSTRAP = r'''
import importlib.util as _perf_importlib_util
import os as _perf_os
import sys as _perf_sys

def _perf_bootstrap_within(parent, candidate):
    try:
        return _perf_os.path.commonpath((parent, candidate)) == parent
    except ValueError:
        return False

_perf_base = _perf_os.path.realpath(_perf_sys.base_prefix)
_perf_stdlib = []
for _perf_entry in _perf_sys.path:
    if not _perf_entry:
        continue
    _perf_candidate = _perf_os.path.realpath(_perf_entry)
    _perf_parts = set(_perf_os.path.normpath(_perf_candidate).split(_perf_os.sep))
    if not _perf_bootstrap_within(_perf_base, _perf_candidate):
        continue
    if "site-packages" in _perf_parts or "dist-packages" in _perf_parts:
        continue
    if _perf_candidate not in _perf_stdlib:
        _perf_stdlib.append(_perf_candidate)
assert _perf_stdlib
_perf_sys.path[:] = _perf_stdlib

_perf_guard_root = _perf_os.path.realpath(
    _perf_os.environ["UNIFIED_PERF_GUARD_ROOT"]
)
_perf_guard_path = _perf_os.path.join(_perf_guard_root, "sitecustomize.py")
_perf_guard_spec = _perf_importlib_util.spec_from_file_location(
    "_unified_performance_guard", _perf_guard_path
)
assert _perf_guard_spec is not None and _perf_guard_spec.loader is not None
_perf_guard_module = _perf_importlib_util.module_from_spec(_perf_guard_spec)
_perf_sys.modules[_perf_guard_spec.name] = _perf_guard_module
_perf_guard_spec.loader.exec_module(_perf_guard_module)
del _perf_sys.modules[_perf_guard_spec.name]
del _perf_guard_module
del _perf_guard_spec

_perf_sources = [
    _perf_os.path.realpath(item)
    for item in _perf_os.environ["UNIFIED_PERF_SOURCE_PATHS"].split(_perf_os.pathsep)
    if item
]
_perf_runtime = []
if _perf_os.environ.get("UNIFIED_PERF_REGISTRY_BOOTSTRAP") != "1":
    _perf_runtime = [
        _perf_os.path.realpath(item)
        for item in _perf_os.environ.get("UNIFIED_PERF_RUNTIME_PATHS", "").split(
            _perf_os.pathsep
        )
        if item
    ]
_perf_sys.path[:] = [_perf_guard_root] + _perf_stdlib + _perf_sources + _perf_runtime
assert _perf_sys.path[0] == _perf_guard_root
assert all(_perf_sys.path.index(_perf_guard_root) < _perf_sys.path.index(item)
           for item in _perf_sources)
assert _perf_os.path.realpath(_perf_os.getcwd()) == _perf_os.path.realpath(
    _perf_os.environ["UNIFIED_PERF_EMPTY_CWD"]
)
assert not _perf_os.listdir(_perf_os.getcwd())
'''


def _python_argv(code: str) -> Tuple[str, ...]:
    """Run all Python children through one isolated, absolute guard bootstrap."""
    return (sys.executable, "-I", "-S", "-B", "-c", _ISOLATED_BOOTSTRAP + code)


def _repeat_process(
    argv: Sequence[str],
    env: Mapping[str, str],
    metric: Mapping[str, Any],
    *,
    inner_float: bool = False,
    validate: Optional[Any] = None,
) -> List[float]:
    samples: List[float] = []
    total = metric["warmups"] + metric["samples"]
    for index in range(total):
        elapsed, payload = _run(argv, env)
        value = _float_output(payload) if inner_float else elapsed
        if validate is not None:
            validate(payload)
        if index >= metric["warmups"]:
            samples.append(value)
    return samples


def _assert_guard_marker_clear(marker: Path) -> None:
    if marker.exists():
        raise MeasurementError(
            "startup path attempted a forbidden action"
        )


def _measure_calibration(metric: Mapping[str, Any], env: Mapping[str, str]) -> List[float]:
    return _repeat_process(_python_argv("\npass\n"), env, metric)


def _environment_for_source_paths(
    env: Mapping[str, str], core_root: Path, ext_root: Path,
) -> Dict[str, str]:
    child = dict(env)
    guard = Path(env["UNIFIED_PERF_GUARD_ROOT"]).resolve()
    core_root = core_root.resolve()
    ext_root = ext_root.resolve()
    source_paths = []
    for path in (core_root, ext_root):
        if str(path) not in source_paths:
            source_paths.append(str(path))
    child["UNIFIED_PERF_SOURCE_PATHS"] = os.pathsep.join(source_paths)
    child["UNIFIED_PERF_DESIGNATED_CORE_ROOT"] = str(core_root)
    child["UNIFIED_PERF_DESIGNATED_EXT_ROOT"] = str(ext_root)
    child["UNIFIED_PERF_GUARD_ROOT"] = str(guard)
    child["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
    child["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    child.pop("PYTHONPATH", None)
    for index, path in enumerate((core_root, ext_root)):
        child = _protect_environment_path(child, "source-" + str(index), path)
    return child


def _source_environment(
    env: Mapping[str, str], root: Path, *, sanitized_root: Optional[Path] = None,
) -> Dict[str, str]:
    root = _safe_source_tree(root)
    if sanitized_root is None:
        core_root = (root / "src").resolve()
        ext_root = (root / "packages" / "unified-cli-ext" / "src").resolve()
    else:
        sanitized_root = sanitized_root.resolve()
        core_root = ext_root = sanitized_root
    child = _environment_for_source_paths(env, core_root, ext_root)
    if sanitized_root is not None:
        child["UNIFIED_PERF_SANITIZED_ROOT"] = str(sanitized_root)
    return child


def _materialize_candidate_environments(
    manifest: SourceManifest, env: Mapping[str, str], destination: Path,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Use the parent-captured bytes for every candidate measurement."""
    source = _materialize_manifest(manifest, destination / "candidate-source")
    registry_source = _materialize_manifest(
        manifest, destination / "candidate-registry-source", registry=True,
    )
    candidate = _source_environment(env, source)
    registry = _source_environment(
        env, source, sanitized_root=registry_source,
    )
    registry["UNIFIED_PERF_REGISTRY_BOOTSTRAP"] = "1"
    return candidate, registry


_ORIGIN_PROOF = r'''
def _perf_within(parent, candidate):
    try:
        return os.path.commonpath((parent, candidate)) == parent
    except ValueError:
        return False

def _perf_prove_origins(prefix, root):
    loaded = {
        name: module for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    assert loaded
    for module in loaded.values():
        origin = getattr(module, "__file__", None)
        assert origin is not None
        assert _perf_within(root, os.path.realpath(origin))
'''


def _repeat_same_metric_reference(
    metric: Mapping[str, Any],
    candidate_once: Any,
    reference_once: Any,
) -> Tuple[List[float], List[float], List[float], Dict[str, Any]]:
    samples: List[float] = []
    before_samples: List[float] = []
    after_samples: List[float] = []
    total = metric["warmups"] + metric["samples"]
    for index in range(total):
        before = float(reference_once())
        candidate = float(candidate_once())
        after = float(reference_once())
        if any(
            not math.isfinite(value) or value < 0
            for value in (before, candidate, after)
        ):
            raise MeasurementError("paired reference measurement is invalid")
        if index >= metric["warmups"]:
            before_samples.append(before)
            samples.append(candidate)
            after_samples.append(after)
    return samples, before_samples, after_samples, {
        "reference_metric": metric["normalization"]["metric"],
    }


@contextmanager
def _fresh_reference_environment(
    manifest: SourceManifest,
    env: Mapping[str, str],
    *,
    registry: bool = False,
) -> Iterator[Dict[str, str]]:
    """Materialize one random reference snapshot for exactly one invocation."""
    with tempfile.TemporaryDirectory(
        prefix="reference-invocation-", dir=env["TMPDIR"],
    ) as raw:
        destination = Path(raw) / "source"
        source = _materialize_manifest(manifest, destination, registry=registry)
        if registry:
            child = _environment_for_source_paths(env, source, source)
            child["UNIFIED_PERF_SANITIZED_ROOT"] = str(source)
            child["UNIFIED_PERF_REGISTRY_BOOTSTRAP"] = "1"
        else:
            child = _environment_for_source_paths(
                env,
                source / "src",
                source / "packages" / "unified-cli-ext" / "src",
            )
        yield child


def _fresh_reference_once(
    manifest: SourceManifest,
    env: Mapping[str, str],
    callback: Callable[[Mapping[str, str]], float],
    *,
    registry: bool = False,
) -> float:
    with _fresh_reference_environment(manifest, env, registry=registry) as child:
        return float(callback(child))


def _measure_core_import(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(
        env: Mapping[str, str],
        expected_core_version: str,
        expected_ext_version: str,
    ) -> float:
        expected_core_version, _ = _require_expected_version_pair(
            expected_core_version, expected_ext_version,
        )
        version_literal = repr(expected_core_version)
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_CTYPES"] = "1"
        child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        code = r'''
import os
import sys
import time
start = time.perf_counter_ns()
import unified_cli
elapsed = (time.perf_counter_ns() - start) / 1e6
assert unified_cli.__version__ == ''' + version_literal + r'''
''' + _ORIGIN_PROOF + r'''
_perf_prove_origins(
    "unified_cli", os.path.realpath(os.environ["UNIFIED_PERF_DESIGNATED_CORE_ROOT"])
)
print(elapsed)
'''
        _, payload = _run(_python_argv(code), child_env)
        value = _float_output(payload)
        _assert_guard_marker_clear(marker)
        return value

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(
            candidate_env, CANDIDATE_CORE_VERSION, CANDIDATE_EXT_VERSION,
        ),
        lambda: _fresh_reference_once(
            reference_manifest,
            base_env,
            lambda env: once(
                env, REFERENCE_CORE_VERSION, REFERENCE_EXT_VERSION,
            ),
        ),
    )
    _assert_guard_marker_clear(marker)
    return samples, None, details, before, after


def _measure_core_version(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(
        env: Mapping[str, str],
        expected_core_version: str,
        expected_ext_version: str,
    ) -> float:
        expected_core_version, _ = _require_expected_version_pair(
            expected_core_version, expected_ext_version,
        )
        version_literal = repr(expected_core_version)
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_CTYPES"] = "1"
        child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        code = r'''
import contextlib
import io
import os
import sys
import unified_cli
import unified_cli.cli
''' + _ORIGIN_PROOF + r'''
_perf_prove_origins(
    "unified_cli", os.path.realpath(os.environ["UNIFIED_PERF_DESIGNATED_CORE_ROOT"])
)
output = io.StringIO()
with contextlib.redirect_stdout(output):
    status = unified_cli.cli.main(["--version"])
assert status == 0 and output.getvalue().strip() == ''' + version_literal + r'''
print(output.getvalue().strip())
'''
        elapsed, payload = _run(_python_argv(code), child_env)
        if payload.decode("ascii", "strict").strip() != expected_core_version:
            raise MeasurementError("Core version fast path returned the wrong version")
        _assert_guard_marker_clear(marker)
        return elapsed

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(
            candidate_env, CANDIDATE_CORE_VERSION, CANDIDATE_EXT_VERSION,
        ),
        lambda: _fresh_reference_once(
            reference_manifest,
            base_env,
            lambda env: once(
                env, REFERENCE_CORE_VERSION, REFERENCE_EXT_VERSION,
            ),
        ),
    )
    _assert_guard_marker_clear(marker)
    return samples, None, details, before, after


def _measure_ext_import(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(
        env: Mapping[str, str],
        expected_core_version: str,
        expected_ext_version: str,
    ) -> float:
        _, expected_ext_version = _require_expected_version_pair(
            expected_core_version, expected_ext_version,
        )
        version_literal = repr(expected_ext_version)
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_CTYPES"] = "1"
        child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        code = r'''
import os
import sys
import time
start = time.perf_counter_ns()
import unified_cli_ext
elapsed = (time.perf_counter_ns() - start) / 1e6
assert unified_cli_ext.__version__ == ''' + version_literal + r'''
''' + _ORIGIN_PROOF + r'''
_perf_prove_origins(
    "unified_cli_ext",
    os.path.realpath(os.environ["UNIFIED_PERF_DESIGNATED_EXT_ROOT"]),
)
print(elapsed)
'''
        _, payload = _run(_python_argv(code), child_env)
        value = _float_output(payload)
        _assert_guard_marker_clear(marker)
        return value

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(
            candidate_env, CANDIDATE_CORE_VERSION, CANDIDATE_EXT_VERSION,
        ),
        lambda: _fresh_reference_once(
            reference_manifest,
            base_env,
            lambda env: once(
                env, REFERENCE_CORE_VERSION, REFERENCE_EXT_VERSION,
            ),
        ),
    )
    _assert_guard_marker_clear(marker)
    return samples, None, details, before, after


def _measure_ext_registry(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(
        env: Mapping[str, str],
        expected_core_version: str,
        expected_ext_version: str,
    ) -> float:
        expected_core_version, expected_ext_version = (
            _require_expected_version_pair(
                expected_core_version, expected_ext_version,
            )
        )
        core_version_literal = repr(expected_core_version)
        ext_version_literal = repr(expected_ext_version)
        child_env = dict(env)
        child_env["UNIFIED_CLI_DISABLE_PLUGINS"] = "0"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_CTYPES"] = "1"
        child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        child_env["UNIFIED_PERF_REGISTRY_BOOTSTRAP"] = "1"
        code = r'''
from importlib import metadata
import os
import sys
import time
guard_root = os.path.realpath(os.environ["UNIFIED_PERF_GUARD_ROOT"])
start = time.perf_counter_ns()
from unified_cli_ext.providers import ProviderAdapterRegistryV1
import unified_cli
import unified_cli_ext
from unified_cli.registry import list_providers
assert unified_cli.__version__ == ''' + core_version_literal + r'''
assert unified_cli_ext.__version__ == ''' + ext_version_literal + r'''
registry = ProviderAdapterRegistryV1()
assert registry.descriptors() == ()
descriptors = list_providers(include_ext=True)
elapsed = (time.perf_counter_ns() - start) / 1e6
assert sorted((item.id, item.source, item.status) for item in descriptors) == [
    ("claude", "builtin", "builtin"),
    ("codex", "builtin", "builtin"),
    ("gemini", "builtin", "builtin"),
    ("performance-canary", "extension", "discovered"),
]
distributions = list(metadata.distributions())
inventory = sorted(
    (distribution.metadata.get("Name"), distribution.version)
    for distribution in distributions
)
assert inventory == [("performance-canary", "1.0")]
for distribution in distributions:
    distribution_root = os.path.realpath(distribution.locate_file(""))
    assert _perf_bootstrap_within(guard_root, distribution_root)
entry_points = sorted(
    (item.group, item.name, item.value, distribution.metadata.get("Name"))
    for distribution in distributions
    for item in distribution.entry_points
)
assert entry_points == [(
    "unified_cli.providers.v1", "performance-canary",
    "performance_canary:PLUGIN", "performance-canary",
)]
''' + _ORIGIN_PROOF + r'''
_perf_prove_origins(
    "unified_cli", os.path.realpath(os.environ["UNIFIED_PERF_DESIGNATED_CORE_ROOT"])
)
_perf_prove_origins(
    "unified_cli_ext", os.path.realpath(os.environ["UNIFIED_PERF_DESIGNATED_EXT_ROOT"])
)
assert "performance_canary" not in sys.modules
print(elapsed)
'''
        _, payload = _run(_python_argv(code), child_env)
        value = _float_output(payload)
        _assert_guard_marker_clear(marker)
        return value

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(
            candidate_env, CANDIDATE_CORE_VERSION, CANDIDATE_EXT_VERSION,
        ),
        lambda: _fresh_reference_once(
            reference_manifest,
            base_env,
            lambda env: once(
                env, REFERENCE_CORE_VERSION, REFERENCE_EXT_VERSION,
            ),
            registry=True,
        ),
    )
    _assert_guard_marker_clear(marker)
    return samples, None, details, before, after


def _measure_manage_bootstrap(
    metric: Mapping[str, Any], env: Mapping[str, str], workspace: Path, marker: Path,
) -> List[float]:
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    child_env["UNIFIED_PERF_SAMPLES"] = str(metric["samples"])
    child_env["UNIFIED_PERF_WARMUPS"] = str(metric["warmups"])
    child_env["UNIFIED_PERF_WORKSPACE"] = str(workspace)
    code = r'''
import json
import os
import time

from unified_cli.manage import ManageRuntime

runtime = ManageRuntime((os.environ["UNIFIED_PERF_WORKSPACE"],))
samples = []
total = int(os.environ["UNIFIED_PERF_SAMPLES"]) + int(os.environ["UNIFIED_PERF_WARMUPS"])
for index in range(total):
    start = time.perf_counter_ns()
    token = runtime.issue_bootstrap()
    payload, cookie = runtime.bootstrap(
        supplied_token=token,
        supplied_csrf=None,
        cookie=None,
        peer_key="performance-loopback",
    )
    elapsed = (time.perf_counter_ns() - start) / 1e6
    assert payload["manage"] is True and cookie
    if index >= int(os.environ["UNIFIED_PERF_WARMUPS"]):
        samples.append(elapsed)
print(json.dumps(samples, separators=(",", ":")))
'''
    _, payload = _run(_python_argv(code), child_env)
    values = _json_output(payload)
    if type(values) is not list:
        raise MeasurementError("manage bootstrap returned malformed samples")
    samples = [float(value) for value in values]
    _assert_guard_marker_clear(marker)
    return samples


def _measure_stream_relay(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> List[float]:
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    child_env["UNIFIED_PERF_SAMPLES"] = str(metric["samples"])
    child_env["UNIFIED_PERF_WARMUPS"] = str(metric["warmups"])
    code = r'''
import asyncio
import json
import os
import threading
import time

from unified_cli.server import _async_manage_chat_stream

class Runtime:
    def __init__(self):
        self.finished = 0
    def stream_chat(self, chat):
        yield b"fixture-event"
    def finish_chat(self, chat_id):
        self.finished += 1

class Chat:
    id = "performance-chat"
    def __init__(self):
        self.cancel_event = threading.Event()

async def main():
    runtime = Runtime()
    samples = []
    count = int(os.environ["UNIFIED_PERF_SAMPLES"])
    warmups = int(os.environ["UNIFIED_PERF_WARMUPS"])
    for index in range(count + warmups):
        chat = Chat()
        relay = _async_manage_chat_stream(runtime, chat)
        start = time.perf_counter_ns()
        item = await relay.__anext__()
        elapsed = (time.perf_counter_ns() - start) / 1e6
        assert item == b"fixture-event"
        await relay.aclose()
        assert chat.cancel_event.is_set()
        if index >= warmups:
            samples.append(elapsed)
    assert runtime.finished == count + warmups
    print(json.dumps(samples, separators=(",", ":")))

asyncio.run(main())
'''
    _, payload = _run(_python_argv(code), child_env)
    values = _json_output(payload)
    if type(values) is not list:
        raise MeasurementError("stream relay returned malformed samples")
    samples = [float(value) for value in values]
    _assert_guard_marker_clear(marker)
    return samples


def _measure_fake_overhead(
    metric: Mapping[str, Any], env: Mapping[str, str], fixture: Path, marker: Path,
) -> Tuple[List[float], float, Dict[str, Any]]:
    child_env = dict(env)
    child_env.update({
        "FAKE_PROVIDER": "claude",
        "UNIFIED_PERF_FIXTURE": str(fixture),
        "UNIFIED_PERF_ALLOWED_EXECUTABLE": str(fixture),
        "UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS": "1",
        "UNIFIED_PERF_FORBID_SUBPROCESSES": "1",
        "UNIFIED_PERF_SAMPLES": str(metric["samples"]),
        "UNIFIED_PERF_WARMUPS": str(metric["warmups"]),
    })
    code = r'''
import json
import os
import subprocess
import time
from unified_cli import ClaudeProvider

fixture = os.environ["UNIFIED_PERF_FIXTURE"]
raw_argv = [
    fixture, "-p", "--output-format", "json", "--model",
    "claude-haiku-4-5", "performance fixture",
]
provider = ClaudeProvider(
    bin_path=fixture,
    web_search=False,
    extra_env={"FAKE_PROVIDER": "claude"},
)

def raw_call():
    completed = subprocess.run(
        raw_argv, env=os.environ.copy(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    assert b"hello from claude" in completed.stdout

def wrapper_call():
    response = provider.chat("performance fixture")
    assert response.text == "hello from claude"

raw_samples = []
wrapper_samples = []
count = int(os.environ["UNIFIED_PERF_SAMPLES"])
warmups = int(os.environ["UNIFIED_PERF_WARMUPS"])
for index in range(count + warmups):
    operations = (raw_call, wrapper_call) if index % 2 == 0 else (wrapper_call, raw_call)
    timings = {}
    for operation in operations:
        start = time.perf_counter_ns()
        operation()
        timings[operation.__name__] = (time.perf_counter_ns() - start) / 1e6
    if index >= warmups:
        raw_samples.append(timings["raw_call"])
        wrapper_samples.append(timings["wrapper_call"])
print(json.dumps({"raw": raw_samples, "wrapper": wrapper_samples}, separators=(",", ":")))
'''
    _, payload = _run(_python_argv(code), child_env, timeout=30.0)
    values = _json_output(payload)
    if type(values) is not dict or set(values) != {"raw", "wrapper"}:
        raise MeasurementError("fake CLI comparison returned malformed samples")
    raw = [float(value) for value in values["raw"]]
    wrapper = [float(value) for value in values["wrapper"]]
    if len(raw) != metric["samples"] or len(wrapper) != metric["samples"]:
        raise MeasurementError("fake CLI comparison returned the wrong sample count")
    overhead = [max(0.0, wrapped - direct) for direct, wrapped in zip(raw, wrapper)]
    raw_median = statistics.median(raw)
    details = {
        "raw_median_ms": _rounded(raw_median),
        "wrapper_median_ms": _rounded(statistics.median(wrapper)),
    }
    _assert_guard_marker_clear(marker)
    return overhead, raw_median, details


def _pty_prompt_once(env: Mapping[str, str], marker: Path) -> float:
    _assert_environment_integrity(env)
    child_env = dict(env)
    process_scope = os.urandom(16).hex()
    child_env[_PROCESS_SCOPE_ENV] = process_scope
    child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_NATIVE_PROCESS_CONTROL"] = "1"
    child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    master_fd, slave_fd = pty.openpty()
    process: Optional[subprocess.Popen] = None
    start = time.perf_counter_ns()
    output = bytearray()
    found_at: Optional[float] = None
    completed_cleanly = False
    descendant_found = False
    scope_process_found = False
    try:
        repl_code = r'''
import runpy
runpy.run_module("unified_cli.cli", run_name="__main__")
'''
        process = subprocess.Popen(
            [
                *_python_argv(repl_code), "repl", "--provider", "claude",
                "--no-web-search", "--cwd", child_env["UNIFIED_PERF_WORKSPACE"],
            ],
            cwd=child_env["UNIFIED_PERF_EMPTY_CWD"],
            env=child_env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select((master_fd,), (), (), 0.05)
            if not readable:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master_fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            # Prompt-toolkit may insert terminal control sequences around the
            # prompt, but its final visible suffix remains this ASCII marker.
            if b"] >" in output:
                found_at = (time.perf_counter_ns() - start) / 1_000_000.0
                break
        if found_at is None:
            raise MeasurementError("real PTY did not render the first REPL prompt")
        os.write(master_fd, b"/exit\r")
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2.0)
        if process.returncode != 0:
            raise MeasurementError("REPL did not exit cleanly after the first prompt")
        completed_cleanly = True
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError("real PTY measurement failed") from exc
    finally:
        if process is not None:
            leader_running = process.poll() is None
            descendant_found = _kill_process_group(process)
            scope_process_found = _kill_process_scope(process_scope, process)
            if leader_running and process.poll() is None:
                process.wait()
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)
        _assert_environment_integrity(env)
    if completed_cleanly and (descendant_found or scope_process_found):
        detail = "process-scope" if scope_process_found else "process-group"
        _record_parent_guard_violation(child_env, "descendant", detail)
        raise MeasurementError("REPL left a descendant process")
    _assert_guard_marker_clear(marker)
    return found_at


@contextmanager
def _fresh_repl_invocation_environment(
    env: Mapping[str, str],
) -> Iterator[Dict[str, str]]:
    """Give one REPL timing invocation private, disposable state roots."""
    parent = Path(env["TMPDIR"])
    with tempfile.TemporaryDirectory(
        prefix="repl-invocation-", dir=parent,
    ) as raw:
        base = Path(raw)
        home = base / "home"
        tmp = base / "tmp"
        child_cwd = base / "empty-cwd"
        workspace = base / "workspace"
        for directory in (home, tmp, child_cwd, workspace):
            directory.mkdir(mode=0o700)
        child = dict(env)
        child.update({
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "UNIFIED_PERF_EMPTY_CWD": str(child_cwd),
            "UNIFIED_PERF_WORKSPACE": str(workspace),
            "UNIFIED_PERF_WRITABLE_ROOTS": os.pathsep.join(
                str(path) for path in (home, tmp, workspace)
            ),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        })
        yield child


def _measure_repl(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(env: Mapping[str, str]) -> float:
        with _fresh_repl_invocation_environment(env) as child:
            return _pty_prompt_once(child, marker)

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(candidate_env),
        lambda: _fresh_reference_once(
            reference_manifest,
            base_env,
            once,
        ),
    )
    return samples, None, details, before, after


def run_checks(
    config: Mapping[str, Any], reference_root: Path, root: Path = ROOT,
) -> Dict[str, Any]:
    root = root.absolute()
    candidate_manifest = _read_source_manifest(
        root,
        expected_core_version=CANDIDATE_CORE_VERSION,
        expected_ext_version=CANDIDATE_EXT_VERSION,
    )
    reference_manifest = load_reference_manifest(reference_root, config)
    metrics = config["metrics"]
    results: Dict[str, Any] = {}
    with isolated_environment(root) as (env, marker, fixture):
        environment_root = Path(env["HOME"]).parent
        workspace = environment_root / "workspace"
        candidate_env, candidate_registry_env = (
            _materialize_candidate_environments(
                candidate_manifest, env, environment_root,
            )
        )
        runners = [
            ("calibration_process_startup", lambda: (
                _measure_calibration(
                    metrics["calibration_process_startup"], candidate_env,
                ),
                None, None, None, None,
            )),
        ]
        runners.extend((
            ("core_import", lambda: _measure_core_import(
                metrics["core_import"], candidate_env, reference_manifest, env, marker,
            )),
            ("core_version", lambda: _measure_core_version(
                metrics["core_version"], candidate_env, reference_manifest, env, marker,
            )),
            ("ext_import", lambda: _measure_ext_import(
                metrics["ext_import"], candidate_env, reference_manifest, env, marker,
            )),
            ("ext_passive_registry", lambda: _measure_ext_registry(
                metrics["ext_passive_registry"], candidate_registry_env,
                reference_manifest, env, marker,
            )),
            ("fake_cli_wrapper_overhead", lambda: (
                *_measure_fake_overhead(
                    metrics["fake_cli_wrapper_overhead"], candidate_env,
                    fixture, marker,
                ),
                None, None,
            )),
            ("manage_bootstrap", lambda: (
                _measure_manage_bootstrap(
                    metrics["manage_bootstrap"], candidate_env, workspace, marker
                ), None, None, None, None,
            )),
            ("repl_first_prompt", lambda: _measure_repl(
                metrics["repl_first_prompt"], candidate_env,
                reference_manifest, env, marker,
            )),
            ("stream_relay", lambda: (
                _measure_stream_relay(
                    metrics["stream_relay"], candidate_env, marker,
                ),
                None, None, None, None,
            )),
        ))
        for name, runner in runners:
            try:
                samples, raw_median, details, before, after = runner()
                results[name] = summarize(
                    samples,
                    metrics[name],
                    raw_median=raw_median,
                    details=details,
                    reference_before=before,
                    reference_after=after,
                )
            except (MeasurementError, OSError, ValueError):
                results[name] = {
                    "error": "measurement_failed",
                    "passed": False,
                }
                print("performance check " + name + " failed", file=sys.stderr)
    return {
        "baseline_id": config["baseline_id"],
        "passed": all(result.get("passed") is True for result in results.values()),
        "reference_sha": config["reference"]["sha"],
        "results": results,
        "schema_version": SCHEMA_VERSION,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="versioned JSON baseline/config (default: %(default)s)",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        required=True,
        help="checkout of the immutable reference SHA",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.baseline)
    except PerformanceConfigError:
        payload = {
            "error": "invalid_baseline",
            "passed": False,
            "schema_version": SCHEMA_VERSION,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        print("performance baseline rejected", file=sys.stderr)
        return 2
    try:
        payload = run_checks(config, args.reference_root)
    except (MeasurementError, OSError, ValueError):
        payload = {
            "error": "invalid_reference",
            "passed": False,
            "reference_sha": config["reference"]["sha"],
            "schema_version": SCHEMA_VERSION,
        }
        print("performance reference rejected", file=sys.stderr)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
