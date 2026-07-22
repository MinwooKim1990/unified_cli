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
import errno
import json
import math
import os
import pty
import select
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
DEFAULT_BASELINE = Path(__file__).with_name("performance-baseline-v1.json")
ROOT = Path(__file__).resolve().parents[1]
CORE_VERSION = "0.5.0"
EXT_VERSION = "0.1.0"
_METRIC_NAMES = (
    "calibration_process_startup",
    "calibration_import_workload",
    "core_import",
    "core_version",
    "ext_import",
    "ext_passive_registry",
    "fake_cli_wrapper_overhead",
    "manage_bootstrap",
    "repl_first_prompt",
    "stream_relay",
)
_LEGACY_METRIC_NAMES = tuple(
    name for name in _METRIC_NAMES
    if name != "calibration_import_workload"
)
_NORMALIZED_METRICS = frozenset({
    "core_import", "core_version", "ext_passive_registry",
})
_CALIBRATION_PROFILE_MODULES = {
    "core_import": 420,
    "core_version": 500,
    "ext_passive_registry": 720,
}
_CALIBRATION_VALUE_BY_TARGET = {
    "core_import": "inner_ms",
    "core_version": "process_ms",
    "ext_passive_registry": "inner_ms",
}
_CALIBRATION_PACKAGE = "_unified_perf_import_calibration_v1"
_CALIBRATION_SENTINEL_MODULUS = 1_000_000_007
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
        "baseline_id", "baseline_source", "metrics", "schema_version",
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
    metrics = data["metrics"]
    if type(metrics) is not dict:
        raise PerformanceConfigError("performance baseline metric set is incomplete")
    metric_names = frozenset(metrics)
    legacy = metric_names == frozenset(_LEGACY_METRIC_NAMES)
    if not legacy and metric_names != frozenset(_METRIC_NAMES):
        raise PerformanceConfigError("performance baseline metric set is incomplete")

    for name in _METRIC_NAMES:
        if name not in metrics:
            continue
        metric = metrics[name]
        required = {"samples", "statistic", "threshold", "warmups"}
        allowed = set(required)
        if name in {"core_import", "core_version"}:
            allowed.add("baseline_milliseconds")
        if not legacy and name == "calibration_import_workload":
            allowed.update({"baseline_milliseconds", "profile"})
        if not legacy and name in _NORMALIZED_METRICS:
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

        if legacy:
            continue
        if name == "calibration_import_workload":
            if kind != "fixed":
                raise PerformanceConfigError(name + " threshold must be fixed")
            if metric["profile"] != "core_import":
                raise PerformanceConfigError(name + " profile is invalid")
            if float(threshold["milliseconds"]) != (
                float(metric["baseline_milliseconds"]) + 50.0
            ):
                raise PerformanceConfigError(name + " validity envelope is invalid")
        if name in _NORMALIZED_METRICS:
            normalization = metric["normalization"]
            if type(normalization) is not dict or set(normalization) != {
                "baseline_milliseconds",
                "calibration_value",
                "kind",
                "max_adjustment_milliseconds",
                "max_calibration_milliseconds",
                "profile",
            }:
                raise PerformanceConfigError(name + " normalization is invalid")
            if normalization["kind"] != "paired_import_bracket":
                raise PerformanceConfigError(name + " normalization kind is unsupported")
            if normalization["profile"] != name:
                raise PerformanceConfigError(name + " normalization profile is invalid")
            if normalization["calibration_value"] != _CALIBRATION_VALUE_BY_TARGET[name]:
                raise PerformanceConfigError(name + " calibration value is invalid")
            baseline = _exact_number(
                normalization["baseline_milliseconds"],
                name + " calibration baseline",
            )
            maximum = _exact_number(
                normalization["max_calibration_milliseconds"],
                name + " calibration maximum",
            )
            adjustment = _exact_number(
                normalization["max_adjustment_milliseconds"],
                name + " maximum adjustment",
            )
            if maximum != baseline + 50.0 or adjustment != 50.0:
                raise PerformanceConfigError(name + " calibration envelope is invalid")
    if not legacy:
        calibration = metrics["calibration_import_workload"]
        core_normalization = metrics["core_import"]["normalization"]
        if (
            calibration["baseline_milliseconds"]
            != core_normalization["baseline_milliseconds"]
            or calibration["threshold"]["milliseconds"]
            != core_normalization["max_calibration_milliseconds"]
        ):
            raise PerformanceConfigError(
                "standalone import calibration does not match Core normalization"
            )
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
    normalization_adjustments: Optional[Sequence[float]] = None,
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
    adjustment = 0.0
    normalization_details: Optional[Dict[str, Any]] = None
    normalization = metric.get("normalization")
    if normalization is not None:
        if (
            normalization_adjustments is None
            or len(normalization_adjustments) != len(samples)
            or any(
                not math.isfinite(value)
                or value < 0
                or value > normalization["max_adjustment_milliseconds"]
                for value in normalization_adjustments
            )
        ):
            raise MeasurementError("paired host normalization is invalid")
        normalized_samples = [
            max(0.0, sample - delta)
            for sample, delta in zip(samples, normalization_adjustments)
        ]
        normalized_observed = _observed(normalized_samples, metric)
        adjustment = observed - normalized_observed
        if not math.isfinite(adjustment) or adjustment < 0:
            raise MeasurementError("paired host normalization is invalid")
        normalization_details = {
            "adjustment_ms": _rounded(adjustment),
            "kind": normalization["kind"],
            "normalized_observed_ms": _rounded(normalized_observed),
            "normalized_samples_ms": [
                _rounded(value) for value in normalized_samples
            ],
            "paired_adjustments_ms": [
                _rounded(value) for value in normalization_adjustments
            ],
            "policy_threshold_ms": _rounded(policy_limit),
        }
    elif normalization_adjustments is not None:
        raise MeasurementError("unexpected host normalization adjustments")
    limit = policy_limit + adjustment
    if not math.isfinite(limit):
        raise MeasurementError("host-normalized threshold is invalid")
    result: Dict[str, Any] = {
        "median_ms": _rounded(median),
        "observed_ms": _rounded(observed),
        "p95_ms": _rounded(p95),
        "passed": observed <= limit,
        "samples_ms": [_rounded(value) for value in samples],
        "statistic": statistic,
        "threshold_ms": _rounded(limit),
    }
    result_details = dict(details or {})
    if normalization_details is not None:
        result_details["host_normalization"] = normalization_details
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
    marker = os.environ.get("UNIFIED_PERF_GUARD_MARKER")
    if marker:
        try:
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(kind + ":" + os.path.basename(str(detail)) + "\\n")
        except OSError:
            pass

def _audit(event, args):
    if os.environ.get("UNIFIED_PERF_FORBID_SUBPROCESSES") != "1":
        return
    if event == "subprocess.Popen":
        executable = args[0] if args else "unknown"
    elif event in {"os.exec", "os.posix_spawn", "os.spawn", "os.system", "pty.spawn"}:
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


def _calibration_sentinels() -> Dict[int, int]:
    sentinels: Dict[int, int] = {}
    for index in range(max(_CALIBRATION_PROFILE_MODULES.values())):
        payload_total = sum(
            ((index + 3) * (item + 5)) % 997 for item in range(96)
        )
        sentinels[index] = (payload_total + index) % _CALIBRATION_SENTINEL_MODULUS
    return sentinels


def _calibration_profile_sentinel(profile: str) -> int:
    count = _CALIBRATION_PROFILE_MODULES[profile]
    sentinels = _calibration_sentinels()
    return sum(sentinels[index] for index in range(count)) % (
        _CALIBRATION_SENTINEL_MODULUS
    )


def _write_import_calibration(directory: Path) -> None:
    """Create a disposable pure-Python import DAG with deterministic sentinels."""
    package = directory / _CALIBRATION_PACKAGE
    package.mkdir(mode=0o700)
    package.joinpath("__init__.py").write_text(
        "WORKLOAD_VERSION = 1\n", encoding="utf-8",
    )
    sentinels = _calibration_sentinels()
    for index, sentinel in sentinels.items():
        source = f'''\
_PAYLOAD = tuple((({index} + 3) * (item + 5)) % 997 for item in range(96))

class CalibrationNode:
    __slots__ = ("value",)
    def __init__(self, value):
        self.value = value

def fold_payload():
    return sum(_PAYLOAD)

SENTINEL = (
    fold_payload() + {index}
) % {_CALIBRATION_SENTINEL_MODULUS}
assert SENTINEL == {sentinel}
'''
        package.joinpath(f"leaf_{index:03d}.py").write_text(
            source, encoding="utf-8",
        )
    for profile, count in _CALIBRATION_PROFILE_MODULES.items():
        names = [f"leaf_{index:03d}" for index in range(count)]
        imports = "\n".join(f"    {name}," for name in names)
        values = "\n".join(f"    {name}.SENTINEL," for name in names)
        expected = _calibration_profile_sentinel(profile)
        package.joinpath(f"profile_{profile}.py").write_text(f'''\
from . import (
{imports}
)

SENTINEL = sum((
{values}
)) % {_CALIBRATION_SENTINEL_MODULUS}
assert SENTINEL == {expected}
''', encoding="utf-8")


@contextmanager
def isolated_environment(root: Path = ROOT) -> Iterator[Tuple[Dict[str, str], Path, Path]]:
    """Yield an allowlisted environment, import marker, and fixture path."""
    with tempfile.TemporaryDirectory(prefix="unified-cli-performance-") as raw:
        base = Path(raw)
        home = base / "home"
        tmp = base / "tmp"
        binaries = base / "bin"
        guard = base / "guard"
        workspace = base / "workspace"
        calibration = base / "import-calibration"
        for directory in (home, tmp, binaries, guard, workspace, calibration):
            directory.mkdir(mode=0o700)
        marker = _write_guard(guard)
        _write_import_calibration(calibration)
        source = root / "tests" / "fixtures" / "core_provider_cli.py"
        fixture = binaries / "fixture-provider-cli"
        payload = source.read_text(encoding="utf-8")
        payload = payload.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
        fixture.write_text(payload, encoding="utf-8")
        fixture.chmod(0o700)
        python_path = os.pathsep.join((
            str(guard),
            str(root / "src"),
            str(root / "packages" / "unified-cli-ext" / "src"),
        ))
        env = {
            "COLUMNS": "100",
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "LINES": "30",
            "NO_COLOR": "1",
            "PATH": str(binaries),
            "PROMPT_TOOLKIT_NO_CPR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": python_path,
            "TERM": "xterm-256color",
            "TMPDIR": str(tmp),
            "UNIFIED_CLI_DISABLE_PLUGINS": "1",
            "UNIFIED_CLI_LANG": "en",
            "UNIFIED_PERF_GUARD_MARKER": str(marker),
            "UNIFIED_PERF_IMPORT_CALIBRATION_ROOT": str(calibration),
            "UNIFIED_PERF_REPOSITORY_ROOT": str(root.resolve()),
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
            cwd=str(ROOT if cwd is None else cwd),
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
            "startup path attempted a forbidden import or provider subprocess"
        )


def _measure_calibration(metric: Mapping[str, Any], env: Mapping[str, str]) -> List[float]:
    return _repeat_process((sys.executable, "-c", "pass"), env, metric)


def _import_calibration_once(
    env: Mapping[str, str], marker: Path, *, profile: str, value_name: str,
) -> float:
    count = _CALIBRATION_PROFILE_MODULES[profile]
    module_name = _CALIBRATION_PACKAGE + ".profile_" + profile
    expected_sentinel = _calibration_profile_sentinel(profile)
    child_env = dict(env)
    calibration_root = os.path.realpath(
        env["UNIFIED_PERF_IMPORT_CALIBRATION_ROOT"]
    )
    child_env["PYTHONPATH"] = os.pathsep.join((
        os.path.realpath(marker.parent),
        calibration_root,
    ))
    child_env["UNIFIED_PERF_CALIBRATION_MODULE"] = module_name
    child_env["UNIFIED_PERF_CALIBRATION_MODULE_COUNT"] = str(count + 2)
    child_env["UNIFIED_PERF_CALIBRATION_SENTINEL"] = str(expected_sentinel)
    child_env["UNIFIED_PERF_FORBID_CORE_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    code = r'''
import importlib
import importlib.util
import json
import os
import sys
import time

root = os.path.realpath(os.environ["UNIFIED_PERF_IMPORT_CALIBRATION_ROOT"])
repository_root = os.path.realpath(os.environ["UNIFIED_PERF_REPOSITORY_ROOT"])
module_name = os.environ["UNIFIED_PERF_CALIBRATION_MODULE"]
expected_count = int(os.environ["UNIFIED_PERF_CALIBRATION_MODULE_COUNT"])
expected_sentinel = int(os.environ["UNIFIED_PERF_CALIBRATION_SENTINEL"])

def is_within(parent, candidate):
    try:
        return os.path.commonpath((parent, candidate)) == parent
    except ValueError:
        return False

base_prefix = os.path.realpath(sys.base_prefix)
safe_paths = [root]
for entry in sys.path:
    candidate = os.path.realpath(entry or os.getcwd())
    parts = set(os.path.normpath(candidate).split(os.sep))
    if candidate == root or is_within(root, candidate):
        continue
    if not is_within(base_prefix, candidate):
        continue
    if "site-packages" in parts or "dist-packages" in parts:
        continue
    if candidate not in safe_paths:
        safe_paths.append(candidate)
sys.path[:] = safe_paths
assert not any(is_within(repository_root, path) for path in sys.path)
spec = importlib.util.find_spec(module_name)
assert spec is not None and spec.origin is not None
assert os.path.commonpath((root, os.path.realpath(spec.origin))) == root
start = time.perf_counter_ns()
module = importlib.import_module(module_name)
inner_ms = (time.perf_counter_ns() - start) / 1e6
loaded = {
    name: item for name, item in sys.modules.items()
    if name == "_unified_perf_import_calibration_v1"
    or name.startswith("_unified_perf_import_calibration_v1.")
}
assert len(loaded) == expected_count
for item in loaded.values():
    origin = getattr(item, "__file__", None)
    assert origin is not None
    assert os.path.commonpath((root, os.path.realpath(origin))) == root
assert module.SENTINEL == expected_sentinel
print(json.dumps({
    "inner_ms": inner_ms,
    "module_count": len(loaded),
    "origin": os.path.realpath(module.__file__),
    "search_paths": list(sys.path),
    "sentinel": module.SENTINEL,
}, sort_keys=True, separators=(",", ":")))
'''
    elapsed, payload = _run(
        (sys.executable, "-c", code),
        child_env,
        cwd=Path(calibration_root),
    )
    result = _json_output(payload)
    if type(result) is not dict or set(result) != {
        "inner_ms", "module_count", "origin", "search_paths", "sentinel",
    }:
        raise MeasurementError("import calibration returned malformed proof")
    root = os.path.realpath(env["UNIFIED_PERF_IMPORT_CALIBRATION_ROOT"])
    repository_root = os.path.realpath(env["UNIFIED_PERF_REPOSITORY_ROOT"])
    search_paths = result["search_paths"]
    try:
        origin_is_local = os.path.commonpath((
            root, os.path.realpath(str(result["origin"])),
        )) == root
    except (OSError, ValueError):
        origin_is_local = False
    if (
        type(result["module_count"]) is not int
        or result["module_count"] != count + 2
        or type(result["sentinel"]) is not int
        or result["sentinel"] != expected_sentinel
        or type(search_paths) is not list
        or not search_paths
        or any(type(path) is not str for path in search_paths)
        or not origin_is_local
    ):
        raise MeasurementError("import calibration proof did not match its fixture")
    resolved_search_paths = [os.path.realpath(path) for path in search_paths]
    try:
        repository_visible = any(
            os.path.commonpath((repository_root, path)) == repository_root
            for path in resolved_search_paths
        )
    except ValueError:
        repository_visible = True
    if (
        root not in resolved_search_paths
        or repository_visible
        or any(
            "site-packages" in set(os.path.normpath(path).split(os.sep))
            or "dist-packages" in set(os.path.normpath(path).split(os.sep))
            for path in resolved_search_paths
        )
    ):
        raise MeasurementError("import calibration search path is not isolated")
    inner = result["inner_ms"]
    if type(inner) not in (int, float) or not math.isfinite(inner) or inner < 0:
        raise MeasurementError("import calibration returned an invalid duration")
    _assert_guard_marker_clear(marker)
    return float(inner) if value_name == "inner_ms" else elapsed


def _measure_import_calibration(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> List[float]:
    """Measure the standalone Core-sized synthetic import readiness gate."""
    samples: List[float] = []
    for index in range(metric["warmups"] + metric["samples"]):
        value = _import_calibration_once(
            env, marker, profile=metric["profile"], value_name="inner_ms",
        )
        if index >= metric["warmups"]:
            samples.append(value)
    return samples


def _paired_adjustment(
    before: float, after: float, normalization: Mapping[str, Any],
) -> float:
    maximum = float(normalization["max_calibration_milliseconds"])
    if any(
        not math.isfinite(value) or value < 0
        for value in (before, after)
    ):
        raise MeasurementError("import calibration duration is invalid")
    outside = (before > maximum, after > maximum)
    if all(outside):
        raise MeasurementError("import calibration is outside its validity envelope")
    if any(outside):
        return 0.0
    baseline = float(normalization["baseline_milliseconds"])
    adjustment = min(max(0.0, before - baseline), max(0.0, after - baseline))
    if adjustment > float(normalization["max_adjustment_milliseconds"]):
        raise MeasurementError("import calibration adjustment exceeds its envelope")
    return adjustment


def _repeat_process_bracketed(
    argv: Sequence[str],
    env: Mapping[str, str],
    marker: Path,
    metric: Mapping[str, Any],
    *,
    inner_float: bool = False,
    validate: Optional[Any] = None,
) -> Tuple[List[float], List[float], Dict[str, Any]]:
    normalization = metric["normalization"]
    samples: List[float] = []
    adjustments: List[float] = []
    before_samples: List[float] = []
    after_samples: List[float] = []
    total = metric["warmups"] + metric["samples"]
    for index in range(total):
        before = _import_calibration_once(
            env,
            marker,
            profile=normalization["profile"],
            value_name=normalization["calibration_value"],
        )
        elapsed, payload = _run(argv, env)
        value = _float_output(payload) if inner_float else elapsed
        if validate is not None:
            validate(payload)
        after = _import_calibration_once(
            env,
            marker,
            profile=normalization["profile"],
            value_name=normalization["calibration_value"],
        )
        adjustment = _paired_adjustment(before, after, normalization)
        if index >= metric["warmups"]:
            samples.append(value)
            adjustments.append(adjustment)
            before_samples.append(before)
            after_samples.append(after)
    return samples, adjustments, {
        "import_calibration": {
            "after_ms": [_rounded(value) for value in after_samples],
            "before_ms": [_rounded(value) for value in before_samples],
            "calibration_value": normalization["calibration_value"],
            "max_calibration_ms": _rounded(
                normalization["max_calibration_milliseconds"]
            ),
            "profile": normalization["profile"],
        },
    }


def _measure_core_import(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Optional[Dict[str, Any]], Optional[List[float]],
]:
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    code = (
        "import time; start=time.perf_counter_ns(); import unified_cli; "
        "elapsed=(time.perf_counter_ns()-start)/1e6; "
        "assert unified_cli.__version__=='" + CORE_VERSION + "'; print(elapsed)"
    )
    if "normalization" in metric:
        samples, adjustments, details = _repeat_process_bracketed(
            (sys.executable, "-c", code),
            child_env,
            marker,
            metric,
            inner_float=True,
        )
    else:
        samples = _repeat_process(
            (sys.executable, "-c", code), child_env, metric, inner_float=True,
        )
        adjustments = None
        details = None
    _assert_guard_marker_clear(marker)
    return samples, None, details, adjustments


def _measure_core_version(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Optional[Dict[str, Any]], Optional[List[float]],
]:
    child_env = dict(env)
    child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"

    def validate(payload: bytes) -> None:
        if payload.decode("ascii", "strict").strip() != CORE_VERSION:
            raise MeasurementError("Core version fast path returned the wrong version")

    if "normalization" in metric:
        samples, adjustments, details = _repeat_process_bracketed(
            (sys.executable, "-m", "unified_cli.cli", "--version"),
            child_env,
            marker,
            metric,
            validate=validate,
        )
    else:
        samples = _repeat_process(
            (sys.executable, "-m", "unified_cli.cli", "--version"),
            child_env,
            metric,
            validate=validate,
        )
        adjustments = None
        details = None
    _assert_guard_marker_clear(marker)
    return samples, None, details, adjustments


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
        (sys.executable, "-c", code), child_env, metric, inner_float=True,
    )
    _assert_guard_marker_clear(marker)
    return samples


def _measure_ext_registry(
    metric: Mapping[str, Any], env: Mapping[str, str], marker: Path,
) -> Tuple[
    List[float], Optional[float], Optional[Dict[str, Any]], Optional[List[float]],
]:
    child_env = dict(env)
    child_env["UNIFIED_CLI_DISABLE_PLUGINS"] = "0"
    child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
    child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
    code = r'''
import time
start = time.perf_counter_ns()
from unified_cli_ext.providers import ProviderAdapterRegistryV1
from unified_cli.registry import list_providers
registry = ProviderAdapterRegistryV1()
assert registry.descriptors() == ()
descriptors = list_providers(include_ext=True)
assert any(item.id == "performance-canary" and item.status == "discovered" for item in descriptors)
print((time.perf_counter_ns() - start) / 1e6)
'''
    if "normalization" in metric:
        samples, adjustments, details = _repeat_process_bracketed(
            (sys.executable, "-c", code),
            child_env,
            marker,
            metric,
            inner_float=True,
        )
    else:
        samples = _repeat_process(
            (sys.executable, "-c", code), child_env, metric, inner_float=True,
        )
        adjustments = None
        details = None
    _assert_guard_marker_clear(marker)
    return samples, None, details, adjustments


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
    _, payload = _run((sys.executable, "-c", code), child_env)
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
    _, payload = _run((sys.executable, "-c", code), child_env)
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
    _, payload = _run((sys.executable, "-c", code), child_env, timeout=30.0)
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
        process = subprocess.Popen(
            [
                sys.executable, "-m", "unified_cli.cli", "repl",
                "--provider", "claude", "--no-web-search", "--cwd", str(ROOT),
            ],
            cwd=str(ROOT),
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


def run_checks(config: Mapping[str, Any], root: Path = ROOT) -> Dict[str, Any]:
    metrics = config["metrics"]
    results: Dict[str, Any] = {}
    with isolated_environment(root) as (env, marker, fixture):
        workspace = Path(env["HOME"]).parent / "workspace"
        runners = [
            ("calibration_process_startup", lambda: (
                _measure_calibration(metrics["calibration_process_startup"], env),
                None, None, None,
            )),
        ]
        if "calibration_import_workload" in metrics:
            runners.append(("calibration_import_workload", lambda: (
                _measure_import_calibration(
                    metrics["calibration_import_workload"], env, marker,
                ),
                None, None, None,
            )))
        runners.extend((
            ("core_import", lambda: _measure_core_import(
                metrics["core_import"], env, marker,
            )),
            ("core_version", lambda: _measure_core_version(
                metrics["core_version"], env, marker,
            )),
            ("ext_import", lambda: (
                _measure_ext_import(metrics["ext_import"], env, marker),
                None, None, None,
            )),
            ("ext_passive_registry", lambda: _measure_ext_registry(
                metrics["ext_passive_registry"], env, marker,
            )),
            ("fake_cli_wrapper_overhead", lambda: (
                *_measure_fake_overhead(
                    metrics["fake_cli_wrapper_overhead"], env, fixture, marker
                ),
                None,
            )),
            ("manage_bootstrap", lambda: (
                _measure_manage_bootstrap(
                    metrics["manage_bootstrap"], env, workspace, marker
                ), None, None, None,
            )),
            ("repl_first_prompt", lambda: (
                _measure_repl(metrics["repl_first_prompt"], env, marker),
                None, None, None,
            )),
            ("stream_relay", lambda: (
                _measure_stream_relay(metrics["stream_relay"], env, marker),
                None, None, None,
            )),
        ))
        for name, runner in runners:
            try:
                samples, raw_median, details, adjustments = runner()
                results[name] = summarize(
                    samples,
                    metrics[name],
                    raw_median=raw_median,
                    details=details,
                    normalization_adjustments=adjustments,
                )
            except (MeasurementError, OSError, ValueError) as exc:
                results[name] = {
                    "error": "measurement_failed",
                    "passed": False,
                }
                print("performance check " + name + " failed: " + str(exc), file=sys.stderr)
    return {
        "baseline_id": config["baseline_id"],
        "passed": all(result.get("passed") is True for result in results.values()),
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.baseline)
    except PerformanceConfigError as exc:
        payload = {
            "error": "invalid_baseline",
            "passed": False,
            "schema_version": SCHEMA_VERSION,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        print("performance baseline rejected: " + str(exc), file=sys.stderr)
        return 2
    payload = run_checks(config)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
