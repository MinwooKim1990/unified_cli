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
CORE_VERSION = "0.5.0"
EXT_VERSION = "0.1.0"
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
    "core_import", "core_version", "ext_passive_registry",
})
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
_REGISTRY_ANCHOR = 195.661
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
            if normalization["kind"] != "paired_same_metric_reference":
                raise PerformanceConfigError(name + " normalization kind is unsupported")
            if normalization["metric"] != name:
                raise PerformanceConfigError(name + " reference metric is invalid")
            anchor = _exact_number(
                normalization["anchor_milliseconds"], name + " reference anchor",
            )
            if name in _CORE_ANCHORS and anchor != _CORE_ANCHORS[name]:
                raise PerformanceConfigError(name + " reference anchor is invalid")
            if name == "ext_passive_registry" and anchor != _REGISTRY_ANCHOR:
                raise PerformanceConfigError(name + " reference anchor is invalid")
    for name, anchor in _CORE_ANCHORS.items():
        metric = metrics[name]
        if metric["baseline_milliseconds"] != anchor or metric["threshold"] != {
            "absolute_slack_milliseconds": 50.0,
            "kind": "baseline_regression",
            "relative_slack": 0.1,
        }:
            raise PerformanceConfigError(name + " policy is invalid")
    if metrics["ext_passive_registry"]["threshold"] != {
        "kind": "fixed", "milliseconds": 250.0,
    }:
        raise PerformanceConfigError("ext_passive_registry policy is invalid")
    expected_shapes = {
        "core_import": (15, "median", 3),
        "core_version": (15, "median", 3),
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
        adjustments = [
            max(0.0, min(before, after) - anchor)
            for before, after in zip(reference_before, reference_after)
        ]
        normalized_samples = [
            max(0.0, sample - delta)
            for sample, delta in zip(samples, adjustments)
        ]
        normalized_observed = _observed(normalized_samples, metric)
        normalization_details = {
            "anchor_ms": _rounded(anchor),
            "kind": normalization["kind"],
            "normalized_observed_ms": _rounded(normalized_observed),
            "normalized_samples_ms": [
                _rounded(value) for value in normalized_samples
            ],
            "paired_adjustments_ms": [
                _rounded(value) for value in adjustments
            ],
            "policy_threshold_ms": _rounded(policy_limit),
            "reference_after_ms": [_rounded(value) for value in reference_after],
            "reference_before_ms": [_rounded(value) for value in reference_before],
        }
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

_real_socket = socket.socket
_writing_marker = False

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
    marker = os.environ.get("UNIFIED_PERF_GUARD_MARKER")
    if marker and not _writing_marker:
        _writing_marker = True
        try:
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(kind + ":" + os.path.basename(str(detail)) + "\\n")
        except OSError:
            pass
        finally:
            _writing_marker = False

def _mutation_detail(event, args):
    if event == "open":
        path = args[0] if args else "unknown"
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write_flags = (
            os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        )
        if hasattr(os, "O_TMPFILE"):
            write_flags |= os.O_TMPFILE
        writes = (
            isinstance(mode, str) and any(char in mode for char in "wax+")
        ) or (isinstance(flags, int) and bool(flags & write_flags))
        if writes:
            return path
        return None
    mutation_events = {
        "os.chdir", "os.chflags", "os.chmod", "os.chown", "os.fchdir",
        "os.lchflags", "os.link", "os.mkdir", "os.mknod", "os.remove",
        "os.removexattr", "os.rename", "os.replace", "os.rmdir",
        "os.setxattr", "os.symlink", "os.truncate", "os.unlink", "os.utime",
    }
    if event in mutation_events:
        return args[0] if args else "unknown"
    return None

def _audit(event, args):
    if _writing_marker:
        return
    if os.environ.get("UNIFIED_PERF_FORBID_MUTATIONS") == "1":
        detail = _mutation_detail(event, args)
        if detail is not None:
            _mark("mutation", detail)
            raise RuntimeError("filesystem mutation disabled by performance harness")
        if event in {"os.fork", "os.forkpty"}:
            _mark("fork", event)
            raise RuntimeError("fork disabled by performance harness")
        if event == "import" and args and args[0] in {"ctypes", "_ctypes"}:
            _mark("import", args[0])
            raise RuntimeError("native audit bypass disabled by performance harness")
    if os.environ.get("UNIFIED_PERF_FORBID_SUBPROCESSES") == "1":
        if event == "subprocess.Popen":
            executable = args[0] if args else "unknown"
        elif event in {
            "os.exec", "os.posix_spawn", "os.spawn", "os.system", "pty.spawn",
        }:
            executable = args[0] if args else "unknown"
        else:
            return
        allowed = os.environ.get("UNIFIED_PERF_ALLOWED_EXECUTABLE")
        if allowed and os.path.realpath(str(executable)) == os.path.realpath(allowed):
            return
        _mark("subprocess", executable)
        raise RuntimeError("provider subprocess disabled by performance harness")

sys.addaudithook(_audit)

class _ImportCanary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden_core = (
            os.environ.get("UNIFIED_PERF_FORBID_CORE_IMPORTS") == "1"
            and (fullname == "unified_cli" or fullname.startswith("unified_cli."))
        )
        forbidden_ext = (
            os.environ.get("UNIFIED_PERF_FORBID_EXT_IMPORTS") == "1"
            and (fullname == "unified_cli_ext" or fullname.startswith("unified_cli_ext."))
        )
        forbidden_entrypoint = (
            os.environ.get("UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS") == "1"
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


def _read_source_manifest(root: Path) -> SourceManifest:
    """Capture verified bytes using no-follow descriptor-relative traversal."""
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
        manifest, "src/unified_cli/__init__.py", CORE_VERSION,
    )
    _require_manifest_version(
        manifest,
        "packages/unified-cli-ext/src/unified_cli_ext/__init__.py",
        EXT_VERSION,
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


def source_tree_digest(root: Path) -> str:
    """Hash sorted POSIX paths and file bytes from the two versioned trees.

    ``sha256-path-content-v1`` writes ``D\\0<path>\\0`` for directories and
    ``F\\0<path>\\0<size>\\0<bytes>`` for regular files. Cache/bytecode and
    packaging metadata directories are excluded from both hashing and copying.
    """
    return _read_source_manifest(root).digest


def _safe_source_tree(root: Path) -> Path:
    """Compatibility validator backed by the descriptor-safe manifest reader."""
    _read_source_manifest(root)
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
    manifest = _read_source_manifest(root)
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
        _read_source_manifest(root), destination, registry=True,
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
            "UNIFIED_PERF_WORKSPACE": str(workspace),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
        if any(_is_credential_name(name) for name in env):
            raise MeasurementError("isolated environment contains a credential-like name")
        yield env, marker, fixture


def _run(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    timeout: float = 10.0,
    input_bytes: Optional[bytes] = None,
    cwd: Optional[Path] = None,
) -> Tuple[float, bytes]:
    start = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(Path(env["UNIFIED_PERF_EMPTY_CWD"]) if cwd is None else cwd),
            env=dict(env),
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError("isolated subprocess did not complete") from exc
    elapsed = (time.perf_counter_ns() - start) / 1_000_000.0
    if completed.returncode != 0:
        raise MeasurementError("isolated subprocess returned a failure status")
    return elapsed, completed.stdout


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
    child.pop("PYTHONPATH", None)
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
    def once(env: Mapping[str, str]) -> float:
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        code = r'''
import os
import sys
import time
start = time.perf_counter_ns()
import unified_cli
elapsed = (time.perf_counter_ns() - start) / 1e6
assert unified_cli.__version__ == "0.5.0"
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
        lambda: once(candidate_env),
        lambda: _fresh_reference_once(
            reference_manifest, base_env, once,
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
    def once(env: Mapping[str, str]) -> float:
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
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
assert status == 0 and output.getvalue().strip() == "0.5.0"
print(output.getvalue().strip())
'''
        elapsed, payload = _run(_python_argv(code), child_env)
        if payload.decode("ascii", "strict").strip() != CORE_VERSION:
            raise MeasurementError("Core version fast path returned the wrong version")
        _assert_guard_marker_clear(marker)
        return elapsed

    samples, before, after, details = _repeat_same_metric_reference(
        metric,
        lambda: once(candidate_env),
        lambda: _fresh_reference_once(
            reference_manifest, base_env, once,
        ),
    )
    _assert_guard_marker_clear(marker)
    return samples, None, details, before, after


def _measure_ext_import(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> List[float]:
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    code = (
        "import time; start=time.perf_counter_ns(); import unified_cli_ext; "
        "elapsed=(time.perf_counter_ns()-start)/1e6; "
        "assert unified_cli_ext.__version__=='" + EXT_VERSION + "'; print(elapsed)"
    )
    samples = _repeat_process(
        _python_argv(code), child_env, metric, inner_float=True,
    )
    _assert_guard_marker_clear(marker)
    return samples


def _measure_ext_registry(
    metric: Mapping[str, Any], candidate_env: Mapping[str, str],
    reference_manifest: SourceManifest, base_env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Dict[str, Any], List[float], List[float],
]:
    def once(env: Mapping[str, str]) -> float:
        child_env = dict(env)
        child_env["UNIFIED_CLI_DISABLE_PLUGINS"] = "0"
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
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
from unified_cli.registry import list_providers
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
        lambda: once(candidate_env),
        lambda: _fresh_reference_once(
            reference_manifest, base_env, once, registry=True,
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
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    master_fd, slave_fd = pty.openpty()
    process: Optional[subprocess.Popen] = None
    start = time.perf_counter_ns()
    output = bytearray()
    found_at: Optional[float] = None
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
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError("real PTY measurement failed") from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)
    _assert_guard_marker_clear(marker)
    return found_at


def _measure_repl(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> List[float]:
    samples: List[float] = []
    for index in range(metric["warmups"] + metric["samples"]):
        value = _pty_prompt_once(env, marker)
        if index >= metric["warmups"]:
            samples.append(value)
    return samples


def run_checks(
    config: Mapping[str, Any], reference_root: Path, root: Path = ROOT,
) -> Dict[str, Any]:
    root = root.absolute()
    candidate_manifest = _read_source_manifest(root)
    reference_manifest = load_reference_manifest(reference_root, config)
    metrics = config["metrics"]
    results: Dict[str, Any] = {}
    with isolated_environment(root) as (env, marker, fixture):
        environment_root = Path(env["HOME"]).parent
        workspace = environment_root / "workspace"
        candidate_registry_root = _materialize_manifest(
            candidate_manifest,
            environment_root / "candidate-registry-source",
            registry=True,
        )
        candidate_env = _source_environment(env, root)
        candidate_registry_env = _source_environment(
            env, root, sanitized_root=candidate_registry_root,
        )
        candidate_registry_env["UNIFIED_PERF_REGISTRY_BOOTSTRAP"] = "1"
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
            ("ext_import", lambda: (
                _measure_ext_import(metrics["ext_import"], candidate_env, marker),
                None, None, None, None,
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
            ("repl_first_prompt", lambda: (
                _measure_repl(metrics["repl_first_prompt"], candidate_env, marker),
                None, None, None, None,
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
