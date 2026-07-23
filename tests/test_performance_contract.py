"""Fail-closed contracts for the pinned same-metric performance gate."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_performance.py"
_SPEC = importlib.util.spec_from_file_location("check_performance", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_performance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check_performance
_SPEC.loader.exec_module(check_performance)


def _write_config(tmp_path: Path, config: object, name: str = "baseline.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _short(metric: dict, *, samples: int = 3) -> dict:
    result = copy.deepcopy(metric)
    result["samples"] = samples
    result["warmups"] = 0
    return result


def _set_declared_version(path: Path, version: str) -> None:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    declarations = [
        index
        for index, line in enumerate(lines)
        if line.startswith("__version__ = ")
    ]
    assert len(declarations) == 1
    lines[declarations[0]] = '__version__ = "{}"'.format(version)
    path.write_text(
        "\n".join(lines) + ("\n" if source.endswith("\n") else ""),
        encoding="utf-8",
    )


def _source_copy(
    tmp_path: Path,
    *,
    core_version: str,
    ext_version: str,
    sha: str = check_performance.REFERENCE_SHA,
    name: str,
) -> Path:
    root = tmp_path / name
    for relative in check_performance.SOURCE_TREES:
        source = ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text(sha + "\n", encoding="ascii")
    _set_declared_version(
        root / "src" / "unified_cli" / "__init__.py", core_version,
    )
    _set_declared_version(
        root
        / "packages"
        / "unified-cli-ext"
        / "src"
        / "unified_cli_ext"
        / "__init__.py",
        ext_version,
    )
    return root


def _reference_copy(
    tmp_path: Path,
    *,
    sha: str = check_performance.REFERENCE_SHA,
    name: str = "reference",
) -> Path:
    return _source_copy(
        tmp_path,
        core_version=check_performance.REFERENCE_CORE_VERSION,
        ext_version=check_performance.REFERENCE_EXT_VERSION,
        sha=sha,
        name=name,
    )


def _candidate_copy(tmp_path: Path, *, name: str = "candidate") -> Path:
    return _source_copy(
        tmp_path,
        core_version=check_performance.CANDIDATE_CORE_VERSION,
        ext_version=check_performance.CANDIDATE_EXT_VERSION,
        name=name,
    )


def _reference_digest(root: Path) -> str:
    return check_performance.source_tree_digest(
        root,
        expected_core_version=check_performance.REFERENCE_CORE_VERSION,
        expected_ext_version=check_performance.REFERENCE_EXT_VERSION,
    )


def _candidate_digest(root: Path) -> str:
    return check_performance.source_tree_digest(
        root,
        expected_core_version=check_performance.CANDIDATE_CORE_VERSION,
        expected_ext_version=check_performance.CANDIDATE_EXT_VERSION,
    )


def _reference_manifest(tmp_path: Path, *, name: str):
    reference = _reference_copy(tmp_path, name=name)
    return check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )


def _append_ext_candidate(candidate: Path, payload: str) -> None:
    target = (
        candidate
        / "packages"
        / "unified-cli-ext"
        / "src"
        / "unified_cli_ext"
        / "__init__.py"
    )
    target.write_text(
        target.read_text(encoding="utf-8") + "\n" + payload,
        encoding="utf-8",
    )


def test_baseline_pins_reference_digest_anchors_and_exact_policies():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    metrics = config["metrics"]
    assert (
        check_performance.CANDIDATE_CORE_VERSION,
        check_performance.CANDIDATE_EXT_VERSION,
    ) == ("0.5.2", "0.5.2")
    assert (
        check_performance.REFERENCE_CORE_VERSION,
        check_performance.REFERENCE_EXT_VERSION,
    ) == ("0.5.0", "0.1.0")
    assert config["schema_version"] == 1
    assert config["reference"] == {
        "digest_algorithm": "sha256-path-content-v1",
        "sha": "be1478884735c862e894959944ba53e149ea4210",
        "source_tree_digest": (
            "7f21edae7ab640afb342261ef4092586101edc9549661e391032ce6906fc04f4"
        ),
        "source_trees": [
            "src/unified_cli",
            "packages/unified-cli-ext/src/unified_cli_ext",
        ],
    }
    assert "calibration_import_workload" not in metrics
    assert metrics["core_import"]["normalization"] == {
        "anchor_milliseconds": 48.606,
        "kind": "paired_same_metric_reference",
        "metric": "core_import",
    }
    assert metrics["core_version"]["normalization"]["anchor_milliseconds"] == 94.635
    assert metrics["ext_import"]["normalization"] == {
        "anchor_milliseconds": 52.066,
        "kind": "paired_same_metric_ratio",
        "metric": "ext_import",
    }
    assert metrics["ext_passive_registry"]["normalization"] == {
        "anchor_milliseconds": 195.661,
        "kind": "paired_same_metric_ratio",
        "metric": "ext_passive_registry",
    }
    assert metrics["repl_first_prompt"]["normalization"] == {
        "anchor_milliseconds": 164.551,
        "kind": "paired_same_metric_reference",
        "metric": "repl_first_prompt",
    }
    assert metrics["ext_passive_registry"]["samples"] == 61
    assert (
        metrics["ext_import"]["samples"],
        metrics["ext_import"]["statistic"],
        metrics["ext_import"]["warmups"],
    ) == (15, "p95", 3)
    assert metrics["repl_first_prompt"]["samples"] == 31
    assert (
        metrics["repl_first_prompt"]["samples"],
        metrics["repl_first_prompt"]["statistic"],
        metrics["repl_first_prompt"]["warmups"],
    ) == (31, "p95", 3)
    assert check_performance._threshold(metrics["core_import"]) == 98.606
    assert check_performance._threshold(metrics["core_version"]) == 144.635
    assert check_performance._threshold(metrics["ext_import"]) == 250.0
    assert check_performance._threshold(metrics["ext_passive_registry"]) == 250.0
    assert check_performance._threshold(metrics["repl_first_prompt"]) == 300.0


def test_exact_repl_sampling_shape_is_fail_closed(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    for field, value in (("samples", 30), ("statistic", "median"), ("warmups", 2)):
        mutated = copy.deepcopy(config)
        mutated["metrics"]["repl_first_prompt"][field] = value
        with pytest.raises(
            check_performance.PerformanceConfigError,
            match="repl_first_prompt sampling policy",
        ):
            check_performance.load_config(
                _write_config(tmp_path, mutated, "repl-" + field + ".json")
            )

    mutated = copy.deepcopy(config)
    mutated["metrics"]["repl_first_prompt"]["normalization"][
        "anchor_milliseconds"
    ] += 0.001
    with pytest.raises(
        check_performance.PerformanceConfigError,
        match="repl_first_prompt reference anchor",
    ):
        check_performance.load_config(
            _write_config(tmp_path, mutated, "repl-anchor.json")
        )

    mutated = copy.deepcopy(config)
    mutated["metrics"]["repl_first_prompt"]["threshold"][
        "milliseconds"
    ] = 300.001
    with pytest.raises(
        check_performance.PerformanceConfigError,
        match="repl_first_prompt policy",
    ):
        check_performance.load_config(
            _write_config(tmp_path, mutated, "repl-threshold.json")
        )


def test_61_sample_p95_is_exact_sorted_index_57_and_tracks_heterogeneous_mutation():
    values = [float((index * 37) % 61) for index in range(61)]
    assert check_performance.percentile(values, 0.95) == sorted(values)[57] == 57.0
    values[values.index(57.0)] = 57.5
    assert check_performance.percentile(values, 0.95) == sorted(values)[57] == 57.5


@pytest.mark.parametrize("payload", ({}, {"schema_version": 1}, []))
def test_malformed_or_incomplete_config_fails_closed(tmp_path, payload):
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, payload))


@pytest.mark.parametrize(
    "mutation",
    (
        {"kind": "paired_import_bracket"},
        {"metric": "core_version"},
        {"factor": 1.0},
        {"ratio": 1.0},
        {"references": ["core_import"]},
        {"max_adjustment_milliseconds": 50.0},
    ),
)
def test_synthetic_partial_cross_metric_factor_and_ratio_shapes_are_rejected(
    tmp_path, mutation,
):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_import"]["normalization"].update(mutation)
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config))


@pytest.mark.parametrize(
    ("name", "kind"),
    (
        ("core_import", "paired_same_metric_ratio"),
        ("core_version", "paired_same_metric_ratio"),
        ("repl_first_prompt", "paired_same_metric_ratio"),
        ("ext_import", "paired_same_metric_reference"),
        ("ext_passive_registry", "paired_same_metric_reference"),
    ),
)
def test_normalization_kind_is_scoped_to_authorized_metrics(tmp_path, name, kind):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"][name]["normalization"]["kind"] = kind
    with pytest.raises(
        check_performance.PerformanceConfigError,
        match=name + " normalization kind",
    ):
        check_performance.load_config(
            _write_config(tmp_path, config, name + "-normalization.json")
        )


@pytest.mark.parametrize(
    ("name", "anchor"),
    (("ext_import", 52.066), ("ext_passive_registry", 195.661)),
)
def test_ratio_anchor_drift_is_rejected(tmp_path, name, anchor):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"][name]["normalization"]["anchor_milliseconds"] = anchor + 0.001
    with pytest.raises(
        check_performance.PerformanceConfigError,
        match=name + " reference anchor",
    ):
        check_performance.load_config(
            _write_config(tmp_path, config, name + "-anchor.json")
        )


def test_old_synthetic_and_partial_profiles_are_rejected(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["calibration_import_workload"] = {
        "samples": 15, "statistic": "median", "warmups": 3,
        "threshold": {"kind": "fixed", "milliseconds": 99.712},
    }
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "synthetic.json"))
    del config["metrics"]["calibration_import_workload"]
    del config["metrics"]["core_import"]["normalization"]
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "partial.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["reference"]["sha"] = check_performance.REFERENCE_SHA[:12]
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "short-sha.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["reference"]["source_tree_digest"] = "0" * 64
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "digest.json"))


def test_complete_profile_requires_reference_root():
    with pytest.raises(SystemExit):
        check_performance._parser().parse_args([])


@pytest.mark.parametrize(
    ("name", "anchor", "policy"),
    (
        ("core_import", 48.606, 98.606),
        ("core_version", 94.635, 144.635),
    ),
)
def test_core_shared_plus_100_and_exact_50_boundary(name, anchor, policy):
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"][name])
    reference = [anchor + 100.0] * 3
    shared = check_performance.summarize(
        [anchor + 100.0] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    boundary = check_performance.summarize(
        [anchor + 150.0] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    regressed = check_performance.summarize(
        [anchor + 150.001] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    assert shared["passed"] is True
    assert boundary["passed"] is True
    assert regressed["passed"] is False
    assert boundary["threshold_ms"] == policy


def test_registry_exact_250_boundary():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_passive_registry"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    reference = [anchor] * 3
    assert check_performance.summarize(
        [250.0] * 3, metric,
        reference_before=reference, reference_after=reference,
    )["passed"] is True
    assert check_performance.summarize(
        [250.001] * 3, metric,
        reference_before=reference, reference_after=reference,
    )["passed"] is False


def test_ratio_normalization_is_proportionally_invariant():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"])
    candidate = [100.0, 150.0, 200.0]
    reference = [50.0, 75.0, 100.0]
    baseline = check_performance.summarize(
        candidate, metric,
        reference_before=reference, reference_after=reference,
    )
    slowed = check_performance.summarize(
        [value * 3.0 for value in candidate], metric,
        reference_before=[value * 3.0 for value in reference],
        reference_after=[value * 3.0 for value in reference],
    )
    baseline_proof = baseline["details"]["reference_normalization"]
    slowed_proof = slowed["details"]["reference_normalization"]
    assert baseline_proof["normalized_samples_ms"] == (
        slowed_proof["normalized_samples_ms"]
    )
    assert baseline_proof["normalized_observed_ms"] == (
        slowed_proof["normalized_observed_ms"]
    )


def test_ratio_candidate_only_regression_and_one_slow_side_remain_visible():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_passive_registry"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    candidate = [250.001] * 3
    regressed = check_performance.summarize(
        candidate, metric,
        reference_before=[anchor] * 3,
        reference_after=[anchor] * 3,
    )
    one_slow_side = check_performance.summarize(
        candidate, metric,
        reference_before=[anchor] * 3,
        reference_after=[anchor * 3.0] * 3,
    )
    assert regressed["passed"] is False
    assert one_slow_side["passed"] is False
    assert one_slow_side["details"]["reference_normalization"][
        "paired_reference_ms"
    ] == [anchor] * 3


def test_ratio_unrounded_value_controls_exact_policy_boundary():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    result = check_performance.summarize(
        [250.0004] * 3, metric,
        reference_before=[anchor] * 3,
        reference_after=[anchor] * 3,
    )
    assert result["details"]["reference_normalization"][
        "normalized_observed_ms"
    ] == 250.0
    assert result["passed"] is False


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ([0.0, 1.0, 1.0], [1.0, 1.0, 1.0]),
        ([float("nan"), 1.0, 1.0], [1.0, 1.0, 1.0]),
        ([float("inf"), 1.0, 1.0], [1.0, 1.0, 1.0]),
        ([1.0, 1.0], [1.0, 1.0, 1.0]),
    ),
)
def test_ratio_invalid_reference_sets_fail_closed(before, after):
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"])
    with pytest.raises(check_performance.MeasurementError):
        check_performance.summarize(
            [1.0, 1.0, 1.0], metric,
            reference_before=before, reference_after=after,
        )


def test_ratio_missing_reference_set_fails_closed():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"])
    with pytest.raises(check_performance.MeasurementError):
        check_performance.summarize([1.0, 1.0, 1.0], metric)


def test_repl_exact_300_normalized_boundary_and_shared_host_delay():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["repl_first_prompt"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    # A power of two keeps the synthetic cancellation exact in binary floats;
    # the policy boundary itself remains 300.000 versus 300.001 milliseconds.
    shared_delay = 256.0
    reference = [anchor + shared_delay] * 3
    shared = check_performance.summarize(
        [anchor + shared_delay] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    boundary = check_performance.summarize(
        [300.0 + shared_delay] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    regressed = check_performance.summarize(
        [300.001 + shared_delay] * 3, metric,
        reference_before=reference, reference_after=reference,
    )
    one_slow_side = check_performance.summarize(
        [300.001 + shared_delay] * 3, metric,
        reference_before=reference,
        reference_after=[anchor] * 3,
    )
    assert shared["passed"] is True
    assert boundary["passed"] is True
    assert regressed["passed"] is False
    assert one_slow_side["passed"] is False
    assert shared["p95_ms"] == anchor + shared_delay
    assert shared["details"]["reference_normalization"][
        "normalized_observed_ms"
    ] == anchor


def test_repl_measurement_uses_fresh_reference_candidate_reference_order(
    monkeypatch, tmp_path,
):
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["repl_first_prompt"])
    events = []
    invocation_environments = []
    reference_number = 0

    base = tmp_path / "base"
    base_tmp = base / "tmp"
    base_tmp.mkdir(parents=True)
    base_env = {
        "HOME": str(base / "home"),
        "TMPDIR": str(base_tmp),
        "UNIFIED_PERF_EMPTY_CWD": str(base / "empty-cwd"),
        "UNIFIED_PERF_WORKSPACE": str(base / "workspace"),
        "UNIFIED_PERF_WRITABLE_ROOTS": str(base),
        "XDG_CACHE_HOME": str(base / "home" / ".cache"),
        "XDG_CONFIG_HOME": str(base / "home" / ".config"),
        "XDG_DATA_HOME": str(base / "home" / ".local" / "share"),
    }

    def fake_prompt(env, marker):
        events.append(env["kind"])
        paths = {
            name: Path(env[name])
            for name in (
                "HOME", "TMPDIR", "UNIFIED_PERF_EMPTY_CWD",
                "UNIFIED_PERF_WORKSPACE",
            )
        }
        invocation_environments.append(paths)
        assert set(env["UNIFIED_PERF_WRITABLE_ROOTS"].split(os.pathsep)) == {
            str(paths["HOME"]),
            str(paths["TMPDIR"]),
            str(paths["UNIFIED_PERF_WORKSPACE"]),
        }
        for path in paths.values():
            assert path.is_dir()
        for path in (
            paths["HOME"] / "repl_history",
            paths["TMPDIR"] / "provider.state",
            paths["UNIFIED_PERF_WORKSPACE"] / "session.state",
        ):
            assert not path.exists()
            path.write_text("poison", encoding="utf-8")
        return 200.0 if env["kind"] == "candidate" else 190.0

    def fake_fresh(manifest, env, callback, *, registry=False):
        nonlocal reference_number
        reference_number += 1
        return callback({
            **env,
            "kind": "reference-" + str(reference_number),
        })

    monkeypatch.setattr(check_performance, "_pty_prompt_once", fake_prompt)
    monkeypatch.setattr(check_performance, "_fresh_reference_once", fake_fresh)
    samples, raw_median, details, before, after = check_performance._measure_repl(
        metric,
        {**base_env, "kind": "candidate"},
        object(),
        {**base_env, "kind": "base"},
        Path("unused-marker"),
    )
    assert samples == [200.0] * 3
    assert before == after == [190.0] * 3
    assert raw_median is None
    assert details == {"reference_metric": "repl_first_prompt"}
    assert events == [
        "reference-1", "candidate", "reference-2",
        "reference-3", "candidate", "reference-4",
        "reference-5", "candidate", "reference-6",
    ]
    for name in (
        "HOME", "TMPDIR", "UNIFIED_PERF_EMPTY_CWD", "UNIFIED_PERF_WORKSPACE",
    ):
        values = [str(item[name]) for item in invocation_environments]
        assert len(values) == len(set(values)) == 9
    assert not list(base_tmp.glob("repl-invocation-*"))


def test_normalization_is_per_sample_and_one_slow_side_grants_no_credit():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    result = check_performance.summarize(
        [98.0, 140.0, 160.0], metric,
        reference_before=[anchor, anchor + 50.0, anchor + 100.0],
        reference_after=[anchor + 100.0, anchor + 50.0, anchor],
    )
    proof = result["details"]["reference_normalization"]
    assert proof["paired_adjustments_ms"] == [0.0, 50.0, 0.0]
    assert proof["normalized_samples_ms"] == [98.0, 90.0, 160.0]
    assert proof["normalized_observed_ms"] == 98.0
    assert result["passed"] is True


def test_unrounded_value_controls_policy_even_when_report_rounds_to_boundary():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    result = check_performance.summarize(
        [98.6064] * 3, metric,
        reference_before=[anchor] * 3, reference_after=[anchor] * 3,
    )
    assert result["details"]["reference_normalization"]["normalized_observed_ms"] == 98.606
    assert result["passed"] is False


def test_exact_reference_candidate_reference_order_and_failure_is_closed():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    events = []

    def reference():
        events.append("reference")
        return 10.0

    def candidate():
        events.append("candidate")
        return 11.0

    samples, before, after, _ = check_performance._repeat_same_metric_reference(
        metric, candidate, reference,
    )
    assert samples == [11.0] * 3
    assert before == after == [10.0] * 3
    assert events == ["reference", "candidate", "reference"] * 3

    called = False

    def failed_reference():
        raise check_performance.MeasurementError("reference proof failed")

    def forbidden_candidate():
        nonlocal called
        called = True
        return 1.0

    with pytest.raises(check_performance.MeasurementError, match="proof failed"):
        check_performance._repeat_same_metric_reference(
            metric, forbidden_candidate, failed_reference,
        )
    assert called is False


def test_reference_digest_sha_and_version_are_all_proven(tmp_path):
    reference = _reference_copy(tmp_path)
    expected_digest = _reference_digest(reference)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=expected_digest,
    )
    assert manifest.digest == expected_digest

    (reference / ".git" / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    with pytest.raises(check_performance.MeasurementError, match="SHA mismatch"):
        check_performance._validate_reference_manifest(
            reference,
            expected_sha=check_performance.REFERENCE_SHA,
            expected_digest=expected_digest,
        )


@pytest.mark.parametrize(
    ("package", "relative", "wrong_version"),
    (
        ("core", "src/unified_cli/__init__.py", "0.5.0"),
        (
            "ext",
            "packages/unified-cli-ext/src/unified_cli_ext/__init__.py",
            "0.1.0",
        ),
    ),
)
def test_candidate_core_and_ext_version_mismatches_fail_independently(
    tmp_path, package, relative, wrong_version,
):
    candidate = _candidate_copy(tmp_path, name="candidate-" + package)
    _set_declared_version(candidate / relative, wrong_version)
    with pytest.raises(check_performance.MeasurementError, match="version proof"):
        check_performance._read_source_manifest(
            candidate,
            expected_core_version=check_performance.CANDIDATE_CORE_VERSION,
            expected_ext_version=check_performance.CANDIDATE_EXT_VERSION,
        )


@pytest.mark.parametrize(
    ("package", "relative", "wrong_version"),
    (
        ("core", "src/unified_cli/__init__.py", "0.5.2"),
        (
            "ext",
            "packages/unified-cli-ext/src/unified_cli_ext/__init__.py",
            "0.5.2",
        ),
    ),
)
def test_reference_core_and_ext_version_mismatches_fail_independently(
    tmp_path, package, relative, wrong_version,
):
    reference = _reference_copy(tmp_path, name="reference-" + package)
    expected_digest = _reference_digest(reference)
    _set_declared_version(reference / relative, wrong_version)
    with pytest.raises(check_performance.MeasurementError, match="version proof"):
        check_performance._validate_reference_manifest(
            reference,
            expected_sha=check_performance.REFERENCE_SHA,
            expected_digest=expected_digest,
        )


@pytest.mark.parametrize(
    ("expected_core_version", "expected_ext_version"),
    (
        ("from-environment", "0.5.2"),
        ("0.5.2", "from-environment"),
        ("0.5.2", "0.1.0"),
        (["0.5.2"], "0.5.2"),
    ),
)
def test_manifest_version_expectations_reject_untrusted_or_mixed_pairs(
    tmp_path, expected_core_version, expected_ext_version,
):
    candidate = _candidate_copy(tmp_path)
    with pytest.raises(
        check_performance.MeasurementError,
        match="version expectation is invalid",
    ):
        check_performance._read_source_manifest(
            candidate,
            expected_core_version=expected_core_version,
            expected_ext_version=expected_ext_version,
        )


def test_verified_reference_bytes_survive_canonical_mutation_and_use_random_snapshots(
    tmp_path,
):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    original = next(
        entry.payload
        for entry in manifest.entries
        if entry.relative == "src/unified_cli/__init__.py"
    )
    canonical = reference / "src" / "unified_cli" / "__init__.py"
    canonical.write_bytes(b"raise RuntimeError('canonical reference was reused')\n")

    snapshots = []
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        def inspect(child):
            source_root = Path(child["UNIFIED_PERF_DESIGNATED_CORE_ROOT"]).parent
            snapshots.append(source_root)
            assert source_root.joinpath("src/unified_cli/__init__.py").read_bytes() == original
            return 1.0

        assert check_performance._fresh_reference_once(manifest, env, inspect) == 1.0
        assert not snapshots[-1].exists()
        assert check_performance._fresh_reference_once(manifest, env, inspect) == 1.0
        assert not snapshots[-1].exists()
    assert snapshots[0] != snapshots[1]


def test_candidate_never_observes_before_or_future_reference_snapshot(tmp_path):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"], samples=1)
    snapshots = []
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        def reference_once():
            def inspect(child):
                source_root = Path(child["UNIFIED_PERF_DESIGNATED_CORE_ROOT"]).parent
                snapshots.append(source_root)
                assert source_root.exists()
                return 1.0
            return check_performance._fresh_reference_once(manifest, env, inspect)

        def candidate_once():
            assert snapshots and not snapshots[-1].exists()
            assert not list(Path(env["TMPDIR"]).glob("reference-invocation-*"))
            return 1.0

        check_performance._repeat_same_metric_reference(
            metric, candidate_once, reference_once,
        )
    assert len(snapshots) == 2
    assert snapshots[0] != snapshots[1]
    assert all(not path.exists() for path in snapshots)


def test_future_candidate_source_changes_do_not_invalidate_reference_fixture(tmp_path):
    reference = _reference_copy(tmp_path, name="reference")
    candidate = _candidate_copy(tmp_path, name="candidate")
    expected_digest = _reference_digest(reference)
    candidate_init = candidate / "src" / "unified_cli" / "__init__.py"
    candidate_init.write_text(
        candidate_init.read_text(encoding="utf-8") + "\n# future candidate change\n",
        encoding="utf-8",
    )
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=expected_digest,
    )
    assert manifest.digest == expected_digest
    assert _candidate_digest(candidate) != expected_digest
    (reference / ".git" / "HEAD").write_text(
        check_performance.REFERENCE_SHA + "\n", encoding="ascii",
    )
    init = reference / "src" / "unified_cli" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(check_performance.MeasurementError, match="digest mismatch"):
        check_performance._validate_reference_manifest(
            reference,
            expected_sha=check_performance.REFERENCE_SHA,
            expected_digest=expected_digest,
        )


def test_digest_excludes_only_declared_cache_and_packaging_metadata(tmp_path):
    reference = _reference_copy(tmp_path)
    original = _reference_digest(reference)
    cache = reference / "src" / "unified_cli" / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"ambient")
    egg = reference / "src" / "unified_cli" / "ignored.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("ambient", encoding="utf-8")
    assert _reference_digest(reference) == original

    init = reference / "src" / "unified_cli" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace('"0.5.0"', '"0.5.1"', 1),
        encoding="utf-8",
    )
    with pytest.raises(check_performance.MeasurementError, match="version proof"):
        _reference_digest(reference)


def test_digest_explicitly_sorts_each_declared_tree_without_cross_tree_reordering():
    entry = check_performance._ManifestEntry
    items = (
        entry("src/unified_cli/z.py", b"z"),
        entry("packages/unified-cli-ext/src/unified_cli_ext/b.py", b"b"),
        entry("src/unified_cli", None),
        entry("packages/unified-cli-ext/src/unified_cli_ext", None),
        entry("src/unified_cli/a", None),
        entry("src/unified_cli/a/value.py", b"a"),
    )
    ordered = (
        items[2], items[4], items[5], items[0],
        items[3], items[1],
    )
    expected = hashlib.sha256()
    for item in ordered:
        path = item.relative.encode("utf-8")
        if item.payload is None:
            expected.update(b"D\0" + path + b"\0")
        else:
            expected.update(
                b"F\0" + path + b"\0" + str(len(item.payload)).encode("ascii")
                + b"\0" + item.payload
            )
    assert check_performance._manifest_digest(items) == expected.hexdigest()
    assert check_performance._manifest_digest(tuple(reversed(items))) == expected.hexdigest()


def test_source_symlink_and_root_escape_are_rejected(tmp_path):
    reference = _reference_copy(tmp_path)
    target = reference / "src" / "unified_cli" / "escape.py"
    try:
        target.symlink_to(tmp_path / "outside.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(check_performance.MeasurementError, match="symlink"):
        check_performance._safe_source_tree(reference)

    alias = tmp_path / "alias"
    alias.symlink_to(reference, target_is_directory=True)
    with pytest.raises(check_performance.MeasurementError, match="real directory"):
        check_performance._safe_source_tree(alias)


def test_manifest_rejects_symlinks_in_excluded_trees_and_special_files(tmp_path):
    reference = _reference_copy(tmp_path, name="symlink-reference")
    cache = reference / "src" / "unified_cli" / "__pycache__"
    cache.mkdir()
    try:
        (cache / "escape.pyc").symlink_to(tmp_path / "outside.pyc")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(check_performance.MeasurementError, match="symlink"):
        check_performance._read_source_manifest(
            reference,
            expected_core_version=check_performance.REFERENCE_CORE_VERSION,
            expected_ext_version=check_performance.REFERENCE_EXT_VERSION,
        )

    if not hasattr(os, "mkfifo"):
        return
    special = _reference_copy(tmp_path, name="special-reference")
    os.mkfifo(special / "src" / "unified_cli" / "special.pipe")
    with pytest.raises(check_performance.MeasurementError, match="special file"):
        check_performance._read_source_manifest(
            special,
            expected_core_version=check_performance.REFERENCE_CORE_VERSION,
            expected_ext_version=check_performance.REFERENCE_EXT_VERSION,
        )


def test_core_candidate_and_reference_origins_are_separately_proven(tmp_path):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate_env = check_performance._source_environment(env, ROOT)
        samples, _, _, before, after = check_performance._measure_core_import(
            metric, candidate_env, manifest, env, marker,
        )
    assert len(samples) == len(before) == len(after) == 3
    assert not marker.exists()


def test_core_version_uses_fresh_verified_reference_snapshots(tmp_path):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_version"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate_env = check_performance._source_environment(env, ROOT)
        samples, _, _, before, after = check_performance._measure_core_version(
            metric, candidate_env, manifest, env, marker,
        )
    assert len(samples) == len(before) == len(after) == 1
    assert not marker.exists()


def test_measured_children_embed_trusted_candidate_and_reference_versions(
    monkeypatch,
):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    calls = []
    hostile = "0.5.1'; raise RuntimeError('environment injection')"

    def fake_run(argv, env, **_kwargs):
        code = argv[-1]
        calls.append((env["kind"], code))
        if "unified_cli.cli.main" in code:
            version = (
                check_performance.CANDIDATE_CORE_VERSION
                if env["kind"] == "candidate"
                else check_performance.REFERENCE_CORE_VERSION
            )
            return 0.0, (version + "\n").encode("ascii")
        return 0.0, b"1.0\n"

    def fake_fresh(_manifest, env, callback, *, registry=False):
        assert type(registry) is bool
        return callback(dict(env, kind="reference"))

    monkeypatch.setattr(check_performance, "_run", fake_run)
    monkeypatch.setattr(
        check_performance, "_fresh_reference_once", fake_fresh,
    )
    monkeypatch.setattr(
        check_performance, "_assert_guard_marker_clear", lambda _marker: None,
    )
    candidate_env = {
        "kind": "candidate",
        "UNIFIED_PERF_EXPECTED_CORE_VERSION": hostile,
        "UNIFIED_PERF_EXPECTED_EXT_VERSION": hostile,
    }
    base_env = {
        "kind": "base",
        "UNIFIED_PERF_EXPECTED_CORE_VERSION": hostile,
        "UNIFIED_PERF_EXPECTED_EXT_VERSION": hostile,
    }
    marker = Path("unused-marker")
    runners = (
        (
            check_performance._measure_core_import,
            "core_import",
            (
                "assert unified_cli.__version__ == '0.5.2'",
            ),
            (
                "assert unified_cli.__version__ == '0.5.0'",
            ),
        ),
        (
            check_performance._measure_core_version,
            "core_version",
            (
                "output.getvalue().strip() == '0.5.2'",
            ),
            (
                "output.getvalue().strip() == '0.5.0'",
            ),
        ),
        (
            check_performance._measure_ext_import,
            "ext_import",
            (
                "assert unified_cli_ext.__version__ == '0.5.2'",
            ),
            (
                "assert unified_cli_ext.__version__ == '0.1.0'",
            ),
        ),
        (
            check_performance._measure_ext_registry,
            "ext_passive_registry",
            (
                "assert unified_cli.__version__ == '0.5.2'",
                "assert unified_cli_ext.__version__ == '0.5.2'",
            ),
            (
                "assert unified_cli.__version__ == '0.5.0'",
                "assert unified_cli_ext.__version__ == '0.1.0'",
            ),
        ),
    )
    for runner, metric_name, candidate_proofs, reference_proofs in runners:
        calls.clear()
        metric = _short(config["metrics"][metric_name], samples=1)
        runner(metric, candidate_env, object(), base_env, marker)
        assert [kind for kind, _code in calls] == [
            "reference", "candidate", "reference",
        ]
        assert all(
            proof in calls[1][1] for proof in candidate_proofs
        )
        assert all(
            proof in calls[0][1] and proof in calls[2][1]
            for proof in reference_proofs
        )
        assert all(hostile not in code for _kind, code in calls)


def test_all_python_children_use_B_and_do_not_create_bytecode(tmp_path):
    assert check_performance._python_argv("pass")[1:5] == ("-I", "-S", "-B", "-c")
    candidate = _candidate_copy(tmp_path, name="candidate")
    reference = _reference_copy(tmp_path, name="reference")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate_env = check_performance._source_environment(env, candidate)
        check_performance._run(
            check_performance._python_argv("\nimport unified_cli\n"), candidate_env,
        )
        assert not list(candidate.rglob("__pycache__"))

        def inspect(reference_env):
            source = Path(reference_env["UNIFIED_PERF_DESIGNATED_CORE_ROOT"]).parent
            check_performance._run(
                check_performance._python_argv("\nimport unified_cli\n"),
                reference_env,
            )
            assert not list(source.rglob("__pycache__"))
            return 1.0

        assert check_performance._fresh_reference_once(manifest, env, inspect) == 1.0
        assert not marker.exists()


def test_candidate_metrics_use_captured_sources_not_live_or_sibling_shadows(tmp_path):
    candidate = _candidate_copy(tmp_path, name="candidate")
    sentinel = tmp_path / "candidate-shadow-executed"
    shadow = candidate / "src" / "rich"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['SHADOW_SENTINEL']).write_text('executed')\n",
        encoding="utf-8",
    )
    manifest = check_performance._read_source_manifest(
        candidate,
        expected_core_version=check_performance.CANDIDATE_CORE_VERSION,
        expected_ext_version=check_performance.CANDIDATE_EXT_VERSION,
    )
    live_init = candidate / "src" / "unified_cli" / "__init__.py"
    live_init.write_text("raise RuntimeError('live checkout was reused')\n")

    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        environment_root = Path(env["HOME"]).parent
        candidate_env, registry_env = (
            check_performance._materialize_candidate_environments(
                manifest, env, environment_root,
            )
        )
        candidate_env["SHADOW_SENTINEL"] = str(sentinel)
        source_root = Path(candidate_env["UNIFIED_PERF_DESIGNATED_CORE_ROOT"])
        assert source_root == (
            environment_root / "candidate-source" / "src"
        ).resolve()
        assert not (source_root / "rich").exists()
        assert Path(registry_env["UNIFIED_PERF_SANITIZED_ROOT"]) == (
            environment_root / "candidate-registry-source"
        ).resolve()
        _, payload = check_performance._run(
            check_performance._python_argv(
                "\nimport unified_cli\n"
                "import unified_cli.cli\n"
                "print(unified_cli.__version__)\n"
            ),
            candidate_env,
        )
        assert (
            payload.decode("ascii").strip()
            == check_performance.CANDIDATE_CORE_VERSION
        )
        assert not sentinel.exists()
        assert not marker.exists()


def test_general_bootstrap_uses_empty_cwd_and_defeats_candidate_shadow_modules(
    tmp_path,
):
    candidate = _candidate_copy(tmp_path, name="candidate")
    sentinel = tmp_path / "shadow-executed"
    shadow = (
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['SHADOW_SENTINEL']).write_text('executed')\n"
    )
    for name in ("ssl.py", "socket.py", "sitecustomize.py"):
        (candidate / "src" / name).write_text(shadow, encoding="utf-8")

    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, candidate)
        child["SHADOW_SENTINEL"] = str(sentinel)
        _, payload = check_performance._run(
            check_performance._python_argv(r'''
import json
import os
import socket
import ssl
import sys
print(json.dumps({"cwd": os.getcwd(), "path": sys.path}, separators=(",", ":")))
'''),
            child,
        )
        proof = json.loads(payload)
        assert Path(proof["cwd"]).resolve() == Path(
            child["UNIFIED_PERF_EMPTY_CWD"]
        ).resolve()
        assert list(Path(proof["cwd"]).iterdir()) == []
        assert proof["path"][0] == child["UNIFIED_PERF_GUARD_ROOT"]
        for source in child["UNIFIED_PERF_SOURCE_PATHS"].split(os.pathsep):
            assert proof["path"].index(child["UNIFIED_PERF_GUARD_ROOT"]) < proof[
                "path"
            ].index(source)
        assert not sentinel.exists()
        assert not marker.exists()


def test_registry_uses_only_equivalent_sanitized_sources_and_fixed_inventory(tmp_path):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_passive_registry"])
    ambient = tmp_path / "ambient-site-packages" / "fake-9.9.dist-info"
    ambient.mkdir(parents=True)
    (ambient / "METADATA").write_text("Name: fake\nVersion: 9.9\n", encoding="utf-8")
    ignored = reference / "packages" / "unified-cli-ext" / "src" / "ignored.egg-info"
    ignored.mkdir()
    (ignored / "PKG-INFO").write_text("Name: ignored\n", encoding="utf-8")

    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        env["PYTHONPATH"] = str(ambient.parent)
        base = Path(env["HOME"]).parent
        candidate_source = check_performance._build_registry_sandbox(
            ROOT, base / "candidate",
        )
        assert sorted(path.name for path in candidate_source.iterdir()) == [
            "unified_cli", "unified_cli_ext",
        ]
        assert not list(candidate_source.rglob("*.egg-info"))
        sentinel = tmp_path / "registry-shadow-executed"
        shadow = (
            "from pathlib import Path\nimport os\n"
            "Path(os.environ['SHADOW_SENTINEL']).write_text('executed')\n"
        )
        for name in ("ssl.py", "socket.py", "sitecustomize.py"):
            (candidate_source / name).write_text(shadow, encoding="utf-8")
        candidate_env = check_performance._source_environment(
            env, ROOT, sanitized_root=candidate_source,
        )
        candidate_env["SHADOW_SENTINEL"] = str(sentinel)
        samples, _, _, before, after = check_performance._measure_ext_registry(
            metric, candidate_env, manifest, env, marker,
        )
    assert len(samples) == len(before) == len(after) == 3
    assert not sentinel.exists()
    assert not marker.exists()


@pytest.mark.parametrize("shape", (list, tuple))
def test_distribution_owned_entry_points_support_39_through_314_shapes(shape):
    class EntryPoint:
        group = "unified_cli.providers.v1"
        name = "fixture"
        value = "fixture:PLUGIN"

    class Metadata(dict):
        pass

    class Distribution:
        metadata = Metadata(Name="fixture-package")
        version = "1.0"
        entry_points = shape((EntryPoint(),))

    packages, entry_points = check_performance._distribution_inventory(
        [Distribution()]
    )
    assert packages == [("fixture-package", "1.0")]
    assert entry_points == [(
        "unified_cli.providers.v1", "fixture", "fixture:PLUGIN", "fixture-package",
    )]
    assert "metadata.entry_points()" not in SCRIPT.read_text(encoding="utf-8")


def test_isolated_environment_scrubs_credentials_and_blocks_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        assert not any(check_performance._is_credential_name(name) for name in env)
        child = check_performance._source_environment(env, ROOT)
        check_performance._run(check_performance._python_argv((
            "import socket\n"
            "try: socket.create_connection(('example.com', 443))\n"
            "except RuntimeError: pass\n"
            "else: raise AssertionError('network was not blocked')\n"
        )), child)
        assert not marker.exists()


def test_entrypoint_and_provider_subprocess_canaries_are_preserved():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        check_performance._run(check_performance._python_argv((
            "try:\n import performance_canary\n"
            "except RuntimeError:\n pass\n"
        )), child)
        assert marker.read_text(encoding="utf-8") == "import:performance_canary\n"

    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        check_performance._run(check_performance._python_argv((
            "import subprocess\n"
            "try: subprocess.run(['provider-must-not-run'])\n"
            "except RuntimeError: pass\n"
        )), child)
        assert marker.read_text(encoding="utf-8") == (
            "subprocess:provider-must-not-run\n"
        )


@pytest.mark.parametrize("tampering", ("remove", "replace"))
def test_allowed_provider_cannot_tamper_with_the_inherited_process_scope(
    tampering,
):
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_ALLOWED_EXECUTABLE"] = str(fixture)
        statement = (
            "environment.pop('UNIFIED_PERF_PROCESS_SCOPE')"
            if tampering == "remove"
            else "environment['UNIFIED_PERF_PROCESS_SCOPE'] = 'replacement'"
        )
        code = f'''
import os
import subprocess

environment = os.environ.copy()
{statement}
try:
    subprocess.run([{str(fixture)!r}, "-p", "scope probe"], env=environment)
except RuntimeError:
    pass
'''
        check_performance._run(check_performance._python_argv(code), child)
        assert marker.read_text(encoding="utf-8").startswith("process-scope:")


@pytest.mark.parametrize(
    ("attack", "marker_kind"),
    (("executable", "subprocess:"), ("scope", "process-scope:")),
)
def test_direct_posixsubprocess_launch_obeys_executable_and_scope_policy(
    attack, marker_kind,
):
    pytest.importorskip("_posixsubprocess")
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_ALLOWED_EXECUTABLE"] = str(fixture)
        child["FAKE_PROVIDER"] = "claude"
        code = f'''
import _posixsubprocess
import os

if {attack!r} == "executable":
    executables = (os.fsencode("/forbidden-provider"),)
    environment = None
else:
    executables = (os.fsencode({str(fixture)!r}),)
    environment = [
        os.fsencode(key) + b"=" + os.fsencode(value)
        for key, value in os.environ.items()
        if key != "UNIFIED_PERF_PROCESS_SCOPE"
    ]
try:
    _posixsubprocess.fork_exec(
        [executables[0]], executables, True, (), None, environment,
    )
except RuntimeError:
    pass
else:
    raise AssertionError("direct low-level launch was not blocked")
'''
        check_performance._run(check_performance._python_argv(code), child)
        assert marker.read_text(encoding="utf-8").startswith(marker_kind)


def test_guarded_posixsubprocess_preserves_normal_allowed_popen():
    pytest.importorskip("_posixsubprocess")
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_ALLOWED_EXECUTABLE"] = str(fixture)
        child["FAKE_PROVIDER"] = "claude"
        code = f'''
import os
import subprocess

completed = subprocess.run(
    [{str(fixture)!r}, "-p", "scope probe"],
    env=os.environ.copy(),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    preexec_fn=lambda: None,
)
assert completed.returncode == 0
'''
        check_performance._run(check_performance._python_argv(code), child)
        assert not marker.exists()


@pytest.mark.parametrize("load_path", ("reload", "alternate-loader"))
def test_posixsubprocess_module_reload_consistency(load_path):
    pytest.importorskip("_posixsubprocess")
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_ALLOWED_EXECUTABLE"] = str(fixture)
        code = f'''
import importlib
import importlib.machinery
import importlib.util
import os
import _posixsubprocess

def assert_checked(fork_exec):
    executable = os.fsencode("/forbidden-provider")
    try:
        fork_exec([executable], (executable,), True, (), None, None)
    except RuntimeError:
        return
    except TypeError as exc:
        raise AssertionError("unchecked fork_exec callable was exposed") from exc
    raise AssertionError("unchecked fork_exec callable was exposed")

if {load_path!r} == "reload":
    try:
        reloaded = importlib.reload(_posixsubprocess)
    except RuntimeError:
        pass
    else:
        assert_checked(reloaded.fork_exec)
else:
    spec = importlib.machinery.PathFinder.find_spec("_posixsubprocess")
    if spec is not None and spec.loader is not None:
        try:
            alternate = importlib.util.module_from_spec(spec)
            if alternate is not _posixsubprocess:
                spec.loader.exec_module(alternate)
        except RuntimeError:
            pass
        except (ImportError, TypeError):
            pass
        else:
            assert_checked(alternate.fork_exec)
'''
        check_performance._run(check_performance._python_argv(code), child)
        if marker.exists():
            # Python 3.9 can return the already-guarded extension singleton,
            # while newer loaders may attempt a distinct import. Both paths
            # must remain checked and leave an explicit guard marker.
            assert marker.read_text(encoding="utf-8").startswith(
                ("import:", "subprocess:")
            )


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="setsid is unavailable")
def test_guard_prevents_a_measured_child_from_leaving_its_process_scope():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        check_performance._run(check_performance._python_argv((
            "import os\n"
            "try: os.setsid()\n"
            "except RuntimeError: pass\n"
        )), child)
        assert marker.read_text(encoding="utf-8") == (
            "process-scope:session-control\n"
        )


def test_native_environment_scope_removal_is_blocked_without_banning_ctypes():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        check_performance._run(check_performance._python_argv((
            "import ctypes\n"
            "try: ctypes.CDLL(None).unsetenv\n"
            "except RuntimeError: pass\n"
        )), child)
        assert marker.read_text(encoding="utf-8") == (
            "native-process:unsetenv\n"
        )


@pytest.mark.parametrize("operation", ("write", "rename"))
def test_ext_import_cannot_replace_shared_guard_even_after_env_tampering(
    tmp_path, operation,
):
    candidate = _candidate_copy(tmp_path, name="candidate-" + operation)
    if operation == "write":
        attack = (
            "target = os.path.join(os.environ['UNIFIED_PERF_GUARD_ROOT'], "
            "'sitecustomize.py')\n"
            "try:\n"
            "    open(target, 'w').write('raise RuntimeError(\"compromised\")\\n')\n"
            "except RuntimeError:\n"
            "    pass\n"
        )
    else:
        attack = (
            "target = os.path.join(os.environ['UNIFIED_PERF_GUARD_ROOT'], "
            "'sitecustomize.py')\n"
            "try:\n"
            "    os.rename(target, target + '.disabled')\n"
            "except RuntimeError:\n"
            "    pass\n"
        )
    _append_ext_candidate(
        candidate,
        "import os\n"
        "os.environ.pop('UNIFIED_PERF_FORBID_MUTATIONS', None)\n"
        "os.environ.pop('UNIFIED_PERF_GUARD_MARKER', None)\n"
        + attack,
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"], samples=1)

    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, candidate)
        guard = Path(env["UNIFIED_PERF_GUARD_ROOT"]) / "sitecustomize.py"
        original = guard.read_bytes()
        with pytest.raises(check_performance.MeasurementError, match="forbidden action"):
            check_performance._measure_ext_import(
                metric, child,
                _reference_manifest(
                    tmp_path, name="reference-" + operation,
                ),
                env, marker,
            )
        assert marker.read_text(encoding="utf-8").startswith("mutation:")
        assert guard.read_bytes() == original
        assert not Path(str(guard) + ".disabled").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_ext_import_cannot_fork_even_after_env_tampering(tmp_path):
    candidate = _candidate_copy(tmp_path, name="candidate-fork")
    _append_ext_candidate(
        candidate,
        "import os\n"
        "os.environ.pop('UNIFIED_PERF_FORBID_MUTATIONS', None)\n"
        "try:\n"
        "    os.fork()\n"
        "except RuntimeError:\n"
        "    pass\n",
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, candidate)
        with pytest.raises(check_performance.MeasurementError, match="forbidden action"):
            check_performance._measure_ext_import(
                metric, child,
                _reference_manifest(tmp_path, name="reference-fork"),
                env, marker,
            )
        assert marker.read_text(encoding="utf-8").startswith("fork:os.fork")


def test_ext_import_cannot_leave_an_af_unix_endpoint(tmp_path):
    candidate = _candidate_copy(tmp_path, name="candidate-unix-socket")
    _append_ext_candidate(
        candidate,
        "import os\n"
        "import socket\n"
        "endpoint = os.path.join(os.environ['TMPDIR'], 'candidate.sock')\n"
        "channel = socket.socket(socket.AF_UNIX)\n"
        "try:\n"
        "    channel.bind(endpoint)\n"
        "except RuntimeError:\n"
        "    pass\n"
        "finally:\n"
        "    channel.close()\n",
    )
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, candidate)
        endpoint = Path(env["TMPDIR"]) / "candidate.sock"
        with pytest.raises(check_performance.MeasurementError, match="forbidden action"):
            check_performance._measure_ext_import(
                metric, child,
                _reference_manifest(tmp_path, name="reference-unix-socket"),
                env, marker,
            )
        assert marker.read_text(encoding="utf-8").startswith("socket:candidate.sock")
        assert not endpoint.exists()


def test_guard_integrity_preflight_blocks_the_next_reference_child(tmp_path):
    reference = _reference_copy(tmp_path, name="reference-integrity")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    sentinel = tmp_path / "compromised-guard-executed"
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        guard = Path(env["UNIFIED_PERF_GUARD_ROOT"]) / "sitecustomize.py"
        guard.write_text(
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['GUARD_SENTINEL']).write_text('executed')\n",
            encoding="utf-8",
        )

        def reference_once(child):
            probe = dict(child)
            probe["GUARD_SENTINEL"] = str(sentinel)
            check_performance._run(
                check_performance._python_argv("\nimport unified_cli\n"), probe,
            )
            return 1.0

        with pytest.raises(check_performance.MeasurementError, match="integrity"):
            check_performance._fresh_reference_once(manifest, env, reference_once)
        assert not sentinel.exists()


def test_ext_import_normal_measurement_shape_is_preserved(tmp_path):
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"], samples=2)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate = check_performance._source_environment(env, ROOT)
        samples, raw_median, details, before, after = (
            check_performance._measure_ext_import(
                metric, candidate,
                _reference_manifest(tmp_path, name="reference-ext-import"),
                env, marker,
            )
        )
        assert len(samples) == 2
        assert all(value >= 0 for value in samples)
        assert len(before) == len(after) == 2
        assert raw_median is None
        assert details == {"reference_metric": "ext_import"}
        assert not marker.exists()


def test_ext_import_uses_fresh_reference_candidate_reference_order_and_fails_closed(
    monkeypatch,
):
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"], samples=2)
    events = []
    reference_number = 0

    def fake_run(_argv, env):
        events.append(env["kind"])
        return 0.0, b"1.0"

    def fake_fresh(_manifest, env, callback, *, registry=False):
        nonlocal reference_number
        assert registry is False
        reference_number += 1
        return callback({**env, "kind": "reference-" + str(reference_number)})

    monkeypatch.setattr(check_performance, "_run", fake_run)
    monkeypatch.setattr(check_performance, "_fresh_reference_once", fake_fresh)
    monkeypatch.setattr(check_performance, "_assert_guard_marker_clear", lambda _: None)
    samples, raw_median, details, before, after = (
        check_performance._measure_ext_import(
            metric,
            {"kind": "candidate"},
            object(),
            {"kind": "base"},
            Path("unused-marker"),
        )
    )
    assert samples == before == after == [1.0, 1.0]
    assert raw_median is None
    assert details == {"reference_metric": "ext_import"}
    assert events == [
        "reference-1", "candidate", "reference-2",
        "reference-3", "candidate", "reference-4",
    ]

    events.clear()

    def failed_reference(*_args, **_kwargs):
        raise check_performance.MeasurementError("reference proof failed")

    monkeypatch.setattr(check_performance, "_fresh_reference_once", failed_reference)
    with pytest.raises(check_performance.MeasurementError, match="proof failed"):
        check_performance._measure_ext_import(
            metric,
            {"kind": "candidate"},
            object(),
            {"kind": "base"},
            Path("unused-marker"),
        )
    assert events == []


def test_candidate_can_write_only_to_disposable_state_roots_and_devnull():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        _, payload = check_performance._run(
            check_performance._python_argv(r'''
import os
from pathlib import Path

for key in ("HOME", "TMPDIR", "UNIFIED_PERF_WORKSPACE"):
    target = Path(os.environ[key]) / (key.lower() + ".state")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ok", encoding="utf-8")
directory_fd = os.open(os.environ["HOME"], os.O_RDONLY)
try:
    file_fd = os.open(
        "dir-fd.state", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
        dir_fd=directory_fd,
    )
    os.close(file_fd)
finally:
    os.close(directory_fd)
class Accessor:
    open = os.open

accessor_fd = Accessor().open(
    os.path.join(os.environ["HOME"], "accessor.state"),
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    0o600,
)
os.close(accessor_fd)
with open(os.devnull, "wb") as handle:
    handle.write(b"ok")
print("ok")
'''),
            child,
        )
        assert payload == b"ok\n"
        assert not marker.exists()


@pytest.mark.parametrize("protected", ("guard", "fixture"))
def test_candidate_cannot_temporarily_rename_protected_path_via_directory_fd(
    protected,
):
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        target = (
            Path(env["UNIFIED_PERF_GUARD_ROOT"]) / "sitecustomize.py"
            if protected == "guard"
            else fixture
        )
        original = target.read_bytes()
        child = check_performance._source_environment(env, ROOT)
        child["ATTACK_PARENT"] = str(target.parent)
        child["ATTACK_NAME"] = target.name
        check_performance._run(
            check_performance._python_argv(r'''
import os

os.chdir(os.environ["HOME"])
directory_fd = os.open(os.environ["ATTACK_PARENT"], os.O_RDONLY)
name = os.environ["ATTACK_NAME"]
held = name + ".held"
try:
    try:
        os.rename(
            name, held, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
    except RuntimeError:
        pass
    else:
        os.rename(
            held, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
finally:
    os.close(directory_fd)
'''),
            child,
        )
        assert marker.read_text(encoding="utf-8").startswith("mutation:")
        assert target.read_bytes() == original
        assert not target.with_name(target.name + ".held").exists()


@pytest.mark.parametrize("protected", ("guard", "fixture"))
def test_candidate_cannot_temporarily_write_protected_path_via_directory_fd(
    protected,
):
    with check_performance.isolated_environment(ROOT) as (env, marker, fixture):
        target = (
            Path(env["UNIFIED_PERF_GUARD_ROOT"]) / "sitecustomize.py"
            if protected == "guard"
            else fixture
        )
        original = target.read_bytes()
        child = check_performance._source_environment(env, ROOT)
        child["ATTACK_PARENT"] = str(target.parent)
        child["ATTACK_NAME"] = target.name
        child["ATTACK_ORIGINAL"] = original.hex()
        check_performance._run(
            check_performance._python_argv(r'''
import os

os.chdir(os.environ["HOME"])
directory_fd = os.open(os.environ["ATTACK_PARENT"], os.O_RDONLY)
name = os.environ["ATTACK_NAME"]
try:
    try:
        file_fd = os.open(
            name, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd,
        )
    except RuntimeError:
        pass
    else:
        try:
            os.write(file_fd, b"changed")
        finally:
            os.close(file_fd)
        original = bytes.fromhex(os.environ["ATTACK_ORIGINAL"])
        file_fd = os.open(
            name, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd,
        )
        try:
            while original:
                original = original[os.write(file_fd, original):]
        finally:
            os.close(file_fd)
finally:
    os.close(directory_fd)
'''),
            child,
        )
        assert marker.read_text(encoding="utf-8").startswith("mutation:")
        assert target.read_bytes() == original


def test_candidate_after_cannot_mutate_verified_reference_or_trigger_future_snapshot(
    tmp_path, monkeypatch,
):
    reference = _reference_copy(tmp_path, name="reference")
    candidate = _candidate_copy(tmp_path, name="candidate")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    target = reference / "src" / "unified_cli" / "__init__.py"
    original = target.read_bytes()
    candidate_init = candidate / "src" / "unified_cli" / "__init__.py"
    candidate_init.write_text(
        candidate_init.read_text(encoding="utf-8")
        + "\ntry:\n"
        + "    open(__import__('os').environ['ATTACK_TARGET'], 'ab').write(b'attack')\n"
        + "except RuntimeError:\n"
        + "    pass\n",
        encoding="utf-8",
    )
    snapshots = []
    real_materialize = check_performance._materialize_manifest

    def capture(*args, **kwargs):
        snapshots.append(args[1])
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(check_performance, "_materialize_manifest", capture)
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate_env = check_performance._source_environment(env, candidate)
        candidate_env["ATTACK_TARGET"] = str(target)
        with pytest.raises(check_performance.MeasurementError, match="forbidden action"):
            check_performance._measure_core_import(
                metric, candidate_env, manifest, env, marker,
            )
        assert marker.read_text(encoding="utf-8").startswith("mutation:__init__.py")
    assert target.read_bytes() == original
    assert len(snapshots) == 1
    assert not snapshots[0].parent.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_candidate_cannot_fork_watcher_for_future_reference_snapshot(
    tmp_path, monkeypatch,
):
    reference = _reference_copy(tmp_path, name="reference")
    candidate = _candidate_copy(tmp_path, name="candidate")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=_reference_digest(reference),
    )
    candidate_init = candidate / "src" / "unified_cli" / "__init__.py"
    candidate_init.write_text(
        candidate_init.read_text(encoding="utf-8")
        + "\ntry:\n"
        + "    __import__('os').fork()\n"
        + "except RuntimeError:\n"
        + "    pass\n",
        encoding="utf-8",
    )
    snapshots = []
    real_materialize = check_performance._materialize_manifest

    def capture(*args, **kwargs):
        snapshots.append(args[1])
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(check_performance, "_materialize_manifest", capture)
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate_env = check_performance._source_environment(env, candidate)
        with pytest.raises(check_performance.MeasurementError, match="forbidden action"):
            check_performance._measure_core_import(
                metric, candidate_env, manifest, env, marker,
            )
        assert marker.read_text(encoding="utf-8").startswith("fork:os.fork")
    assert len(snapshots) == 1
    assert not snapshots[0].parent.exists()


def test_candidate_mutation_guard_fails_closed_on_ctypes_audit_bypass():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_FORBID_CTYPES"] = "1"
        check_performance._run(
            check_performance._python_argv(
                "\ntry:\n import ctypes\nexcept RuntimeError:\n pass\n"
            ),
            child,
        )
        assert marker.read_text(encoding="utf-8").startswith("import:ctypes")


@pytest.mark.skipif(not hasattr(os, "forkpty"), reason="forkpty is unavailable")
def test_candidate_mutation_guard_blocks_forkpty():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        child["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
        check_performance._run(
            check_performance._python_argv(
                "\nimport os\ntry:\n os.forkpty()\nexcept RuntimeError:\n pass\n"
            ),
            child,
        )
        assert marker.read_text(encoding="utf-8").startswith("fork:os.forkpty")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_run_cleans_the_process_group_after_the_direct_child_exits():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        pid_file = Path(env["TMPDIR"]) / "descendant.pid"
        completed_file = Path(env["TMPDIR"]) / "descendant.completed"
        code = f'''
import os
import time
from pathlib import Path

pid_file = Path({str(pid_file)!r})
completed_file = Path({str(completed_file)!r})
child = os.fork()
if child == 0:
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    os.close(1)
    os.close(2)
    time.sleep(0.5)
    completed_file.write_text("still-running", encoding="ascii")
    os._exit(0)
while not pid_file.exists():
    time.sleep(0.005)
os._exit(0)
'''
        with pytest.raises(
            check_performance.MeasurementError, match="left a descendant process",
        ):
            check_performance._run(
                (sys.executable, "-I", "-S", "-B", "-c", code), env,
            )
        assert marker.read_text(encoding="utf-8") == (
            "descendant:process-group\n"
        )
        assert pid_file.read_text(encoding="ascii").isdigit()
        time.sleep(0.7)
        assert not completed_file.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_run_cleans_a_descendant_that_escapes_the_initial_session():
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        pid_file = Path(env["TMPDIR"]) / "escaped-descendant.pid"
        completed_file = Path(env["TMPDIR"]) / "escaped-descendant.completed"
        code = f'''
import os
import time
from pathlib import Path

pid_file = Path({str(pid_file)!r})
completed_file = Path({str(completed_file)!r})
child = os.fork()
if child == 0:
    os.setsid()
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    os.close(1)
    os.close(2)
    time.sleep(0.5)
    completed_file.write_text("still-running", encoding="ascii")
    os._exit(0)
while not pid_file.exists():
    time.sleep(0.005)
os._exit(0)
'''
        with pytest.raises(
            check_performance.MeasurementError, match="left a descendant process",
        ):
            check_performance._run(
                (sys.executable, "-I", "-S", "-B", "-c", code), env,
            )
        assert marker.read_text(encoding="utf-8") == (
            "descendant:process-scope\n"
        )
        assert pid_file.read_text(encoding="ascii").isdigit()
        time.sleep(0.7)
        assert not completed_file.exists()


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="process-scope inventory is only required on macOS and Linux",
)
def test_process_scope_inventory_capability_failure_is_fail_closed(monkeypatch):
    def unavailable(_scope):
        raise check_performance.MeasurementError(
            "isolated process scope inspection failed"
        )

    monkeypatch.setattr(check_performance, "_process_scope_pids", unavailable)
    with pytest.raises(
        check_performance.MeasurementError, match="scope inspection failed",
    ):
        with check_performance.isolated_environment(ROOT):
            pass


def test_runtime_process_scope_inspection_failure_is_not_silently_ignored(
    monkeypatch,
):
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        def unavailable(_scope):
            raise check_performance.MeasurementError(
                "isolated process scope inspection failed"
            )

        monkeypatch.setattr(check_performance, "_process_scope_pids", unavailable)
        with pytest.raises(
            check_performance.MeasurementError, match="scope inspection failed",
        ):
            check_performance._run(
                (sys.executable, "-I", "-S", "-B", "-c", "pass"), env,
            )


def test_linux_process_scope_inventory_matches_only_the_exact_inherited_id(
    tmp_path,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    expected = proc_root / "41001"
    other = proc_root / "41002"
    expected.mkdir()
    other.mkdir()
    (expected / "environ").write_bytes(
        b"A=1\0UNIFIED_PERF_PROCESS_SCOPE=expected\0"
    )
    (other / "environ").write_bytes(
        b"UNIFIED_PERF_PROCESS_SCOPE=expected-suffix\0"
    )
    assert check_performance._linux_process_scope_pids(
        "expected", proc_root,
    ) == [41001]


@pytest.mark.parametrize(
    ("platform", "expected"), (("darwin", [41]), ("linux", [42])),
)
def test_process_scope_inventory_routes_for_macos_and_linux(
    monkeypatch, platform, expected,
):
    monkeypatch.setattr(check_performance.sys, "platform", platform)
    monkeypatch.setattr(
        check_performance, "_darwin_process_scope_pids", lambda _scope: [41],
    )
    monkeypatch.setattr(
        check_performance, "_linux_process_scope_pids", lambda _scope: [42],
    )
    assert check_performance._process_scope_pids("scope") == expected


def test_run_timer_excludes_parent_integrity_recheck(monkeypatch):
    real_check = check_performance._assert_environment_integrity
    checks = 0

    def delayed_check(env):
        nonlocal checks
        checks += 1
        real_check(env)
        if checks == 2:
            time.sleep(0.05)

    monkeypatch.setattr(
        check_performance, "_assert_environment_integrity", delayed_check,
    )
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        child = check_performance._source_environment(env, ROOT)
        outer_start = time.perf_counter_ns()
        elapsed, _ = check_performance._run(
            check_performance._python_argv("\npass\n"), child,
        )
        outer_elapsed = (time.perf_counter_ns() - outer_start) / 1_000_000.0
    assert checks == 2
    assert outer_elapsed - elapsed >= 40.0


def test_run_forwards_explicit_input_bytes():
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    with check_performance.isolated_environment(ROOT) as (env, _marker, _fixture):
        _elapsed, payload = check_performance._run(
            (sys.executable, "-I", "-S", "-B", "-c", code),
            env,
            input_bytes=b"hello\n",
        )
    assert payload == b"hello\n"


def test_pty_checks_protected_candidate_before_launch():
    manifest = check_performance._read_source_manifest(
        ROOT,
        expected_core_version=check_performance.CANDIDATE_CORE_VERSION,
        expected_ext_version=check_performance.CANDIDATE_EXT_VERSION,
    )
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate, _ = check_performance._materialize_candidate_environments(
            manifest, env, Path(env["HOME"]).parent,
        )
        target = (
            Path(candidate["UNIFIED_PERF_DESIGNATED_EXT_ROOT"])
            / "unified_cli_ext"
            / "__init__.py"
        )
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# changed after capture\n",
            encoding="utf-8",
        )
        with pytest.raises(check_performance.MeasurementError, match="integrity"):
            check_performance._pty_prompt_once(candidate, marker)
        assert not marker.exists()


def test_normal_repl_and_stream_relay_measurements_are_preserved():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    stream_metric = _short(config["metrics"]["stream_relay"], samples=1)
    with check_performance.isolated_environment(ROOT) as (env, marker, _fixture):
        candidate = check_performance._source_environment(env, ROOT)
        prompt_elapsed = check_performance._pty_prompt_once(candidate, marker)
        stream_samples = check_performance._measure_stream_relay(
            stream_metric, candidate, marker,
        )
        assert prompt_elapsed >= 0
        assert len(stream_samples) == 1
        assert stream_samples[0] >= 0
        assert not marker.exists()


def test_run_checks_wires_every_candidate_metric_to_captured_sources(
    tmp_path, monkeypatch,
):
    candidate = _candidate_copy(tmp_path, name="candidate-run")
    fixture_source = ROOT / "tests" / "fixtures" / "core_provider_cli.py"
    fixture_target = candidate / "tests" / "fixtures" / fixture_source.name
    fixture_target.parent.mkdir(parents=True)
    shutil.copy2(fixture_source, fixture_target)
    reference = _reference_copy(tmp_path, name="reference-run")
    config = copy.deepcopy(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    ))
    # This test exercises captured-source wiring, not the immutable release
    # baseline.  Bind its synthetic reference copy to its own prevalidated
    # manifest so ordinary source edits cannot make the wiring test stale.
    config["reference"]["source_tree_digest"] = (
        _reference_digest(reference)
    )
    for metric in config["metrics"].values():
        metric["samples"] = 1
        metric["warmups"] = 0

    real_reader = check_performance._read_source_manifest
    changed = False

    def capture_then_change_live_source(root, **expected_versions):
        nonlocal changed
        manifest = real_reader(root, **expected_versions)
        if Path(root).resolve() == candidate.resolve() and not changed:
            changed = True
            live_init = candidate / "src" / "unified_cli" / "__init__.py"
            live_init.write_text(
                "raise RuntimeError('live source was reused')\n", encoding="utf-8",
            )
            _append_ext_candidate(
                candidate, "raise RuntimeError('live ext source was reused')\n",
            )
        return manifest

    monkeypatch.setattr(
        check_performance, "_read_source_manifest", capture_then_change_live_source,
    )
    report = check_performance.run_checks(config, reference, root=candidate)
    assert changed
    assert all("error" not in result for result in report["results"].values())


def test_json_report_shape_is_stable_and_contains_no_source_paths():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    result = check_performance.summarize(
        [anchor] * 3, metric,
        reference_before=[anchor] * 3,
        reference_after=[anchor] * 3,
    )
    payload = {
        "reference_sha": check_performance.REFERENCE_SHA,
        "results": {"core_import": result},
    }
    encoded = json.dumps(payload, sort_keys=True)
    assert check_performance.REFERENCE_SHA in encoded
    assert str(ROOT) not in encoded
    proof = result["details"]["reference_normalization"]
    assert set(proof) == {
        "anchor_ms", "kind", "normalized_observed_ms",
        "normalized_samples_ms", "paired_adjustments_ms",
        "policy_threshold_ms", "reference_after_ms", "reference_before_ms",
    }


def test_ratio_json_report_contains_raw_and_chosen_references_without_adjustments():
    metric = _short(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_import"])
    anchor = metric["normalization"]["anchor_milliseconds"]
    result = check_performance.summarize(
        [anchor, anchor * 2.0, anchor * 3.0],
        metric,
        reference_before=[anchor, anchor * 2.0, anchor * 4.0],
        reference_after=[anchor * 2.0, anchor * 3.0, anchor * 3.0],
    )
    proof = result["details"]["reference_normalization"]
    assert proof["kind"] == "paired_same_metric_ratio"
    assert proof["paired_reference_ms"] == [
        anchor, anchor * 2.0, anchor * 3.0,
    ]
    assert proof["normalized_samples_ms"] == [anchor, anchor, anchor]
    assert "paired_adjustments_ms" not in proof
    assert result["samples_ms"] == [anchor, anchor * 2.0, anchor * 3.0]
    assert set(proof) == {
        "anchor_ms", "kind", "normalized_observed_ms",
        "normalized_samples_ms", "paired_reference_ms",
        "policy_threshold_ms", "reference_after_ms", "reference_before_ms",
    }


def test_invalid_reference_failure_redacts_absolute_paths_and_raw_details(
    tmp_path, capsys,
):
    missing = tmp_path / "credential-OPENAI_API_KEY-must-not-leak"
    assert check_performance.main(["--reference-root", str(missing)]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"] == "invalid_reference"
    assert payload["passed"] is False
    assert str(tmp_path) not in captured.out + captured.err
    assert "OPENAI_API_KEY" not in captured.out + captured.err
