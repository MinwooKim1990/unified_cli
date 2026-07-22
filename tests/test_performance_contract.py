"""Contract tests for the offline, versioned performance gate."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_performance.py"
_SPEC = importlib.util.spec_from_file_location("check_performance", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_performance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check_performance
_SPEC.loader.exec_module(check_performance)


def _write_config(tmp_path, config, name="baseline.json"):
    path = tmp_path / name
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _short_metric(metric, *, samples=3):
    result = copy.deepcopy(metric)
    result["samples"] = samples
    result["warmups"] = 0
    return result


def test_versioned_baseline_pins_code_policy_and_calibration_profiles():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    metrics = config["metrics"]

    assert config["schema_version"] == 1
    assert metrics["core_import"]["baseline_milliseconds"] == 48.606
    assert metrics["core_version"]["baseline_milliseconds"] == 94.635
    assert metrics["calibration_import_workload"] == {
        "baseline_milliseconds": 49.712,
        "profile": "core_import",
        "samples": 15,
        "statistic": "median",
        "threshold": {"kind": "fixed", "milliseconds": 99.712},
        "warmups": 3,
    }
    assert metrics["core_import"]["normalization"] == {
        "baseline_milliseconds": 49.712,
        "calibration_value": "inner_ms",
        "kind": "paired_import_bracket",
        "max_adjustment_milliseconds": 50.0,
        "max_calibration_milliseconds": 99.712,
        "profile": "core_import",
    }
    assert metrics["core_version"]["normalization"]["profile"] == "core_version"
    assert metrics["core_version"]["normalization"]["calibration_value"] == (
        "process_ms"
    )
    assert metrics["ext_passive_registry"]["normalization"]["profile"] == (
        "ext_passive_registry"
    )
    assert metrics["repl_first_prompt"]["threshold"]["milliseconds"] == 300.0
    assert metrics["manage_bootstrap"]["threshold"]["milliseconds"] == 100.0
    assert metrics["stream_relay"]["threshold"]["milliseconds"] == 10.0


@pytest.mark.parametrize("payload", ({}, {"schema_version": 1}, []))
def test_malformed_or_incomplete_baseline_fails_closed(tmp_path, payload):
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, payload))


def test_missing_baseline_returns_stable_machine_error(tmp_path, capsys):
    assert check_performance.main([
        "--baseline", str(tmp_path / "missing.json"),
    ]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "invalid_baseline",
        "passed": False,
        "schema_version": 1,
    }


def test_policy_ceilings_remain_exact():
    metrics = check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]
    assert check_performance._threshold(metrics["core_import"]) == 98.606
    assert check_performance._threshold(metrics["core_version"]) == 144.635
    assert check_performance._threshold(
        metrics["fake_cli_wrapper_overhead"], raw_median=600.0,
    ) == 60.0


def test_half_migrated_or_unbounded_config_fails_closed(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    del config["metrics"]["calibration_import_workload"]
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "half.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    del config["metrics"]["core_import"]["normalization"]
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "no-pair.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"][
        "max_adjustment_milliseconds"
    ] = 50.001
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "unbounded.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"]["profile"] = (
        "core_import"
    )
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "profile.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_import"]["normalization"].update({
        "combine": "maximum",
        "references": ["core_import"],
    })
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "project-ref.json"))

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["calibration_import_workload"]["threshold"] = {
        "absolute_slack_milliseconds": 50.0,
        "kind": "baseline_regression",
        "relative_slack": 0.1,
    }
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, config, "cal-policy.json"))


def test_only_explicit_pre_normalization_v1_shape_is_legacy_compatible(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    del config["metrics"]["calibration_import_workload"]
    for name in ("core_import", "core_version", "ext_passive_registry"):
        del config["metrics"][name]["normalization"]
    legacy = _write_config(tmp_path, config, "legacy.json")
    assert check_performance.load_config(legacy) == config

    unsafe = copy.deepcopy(config)
    unsafe["metrics"]["core_version"]["normalization"] = {
        "combine": "sum",
        "kind": "positive_baseline_delta",
        "references": ["core_import"],
    }
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(_write_config(tmp_path, unsafe, "unsafe.json"))


@pytest.mark.parametrize(
    ("name", "base_observed"),
    (("core_import", 48.606), ("core_version", 94.635)),
)
def test_shared_slowdown_passes_but_target_only_50_001_fails(
    name, base_observed,
):
    metric = _short_metric(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"][name])
    shared = 20.0

    host_only = check_performance.summarize(
        [base_observed + shared] * metric["samples"],
        metric,
        normalization_adjustments=[shared] * metric["samples"],
    )
    exact_boundary = check_performance.summarize(
        [base_observed + shared + 50.0] * metric["samples"],
        metric,
        normalization_adjustments=[shared] * metric["samples"],
    )
    regressed = check_performance.summarize(
        [base_observed + shared + 50.001] * metric["samples"],
        metric,
        normalization_adjustments=[shared] * metric["samples"],
    )

    assert host_only["passed"] is True
    assert exact_boundary["passed"] is True
    assert regressed["passed"] is False
    assert regressed["details"]["host_normalization"][
        "normalized_observed_ms"
    ] == pytest.approx(base_observed + 50.001, abs=0.001)


def test_registry_uses_only_its_paired_neutral_calibration():
    metric = _short_metric(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_passive_registry"])
    host_only = check_performance.summarize(
        [270.0] * metric["samples"],
        metric,
        normalization_adjustments=[20.0] * metric["samples"],
    )
    regressed = check_performance.summarize(
        [320.001] * metric["samples"],
        metric,
        normalization_adjustments=[20.0] * metric["samples"],
    )
    assert host_only["passed"] is True
    assert regressed["passed"] is False


def test_normalization_is_applied_per_pair_before_the_target_statistic():
    metric = _short_metric(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    result = check_performance.summarize(
        [98.0, 140.0, 160.0],
        metric,
        normalization_adjustments=[0.0, 50.0, 0.0],
    )

    normalization = result["details"]["host_normalization"]
    assert normalization["normalized_samples_ms"] == [98.0, 90.0, 160.0]
    assert normalization["normalized_observed_ms"] == 98.0
    assert normalization["adjustment_ms"] == 42.0
    assert result["passed"] is True


def test_p95_normalization_is_computed_from_paired_samples():
    metric = _short_metric(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["ext_passive_registry"])
    result = check_performance.summarize(
        [240.0, 280.0, 300.0],
        metric,
        normalization_adjustments=[0.0, 50.0, 0.0],
    )

    normalization = result["details"]["host_normalization"]
    assert normalization["normalized_samples_ms"] == [240.0, 230.0, 300.0]
    assert normalization["normalized_observed_ms"] == 294.0
    assert normalization["adjustment_ms"] == 4.0
    assert result["passed"] is False


def test_one_slow_bracket_grants_zero_and_envelope_is_exact():
    normalization = check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"]["normalization"]
    baseline = normalization["baseline_milliseconds"]

    assert check_performance._paired_adjustment(
        baseline + 20.0, baseline, normalization,
    ) == 0.0
    assert check_performance._paired_adjustment(
        baseline, baseline + 20.0, normalization,
    ) == 0.0
    assert check_performance._paired_adjustment(
        baseline + 20.0, baseline + 20.0, normalization,
    ) == 20.0
    assert check_performance._paired_adjustment(
        baseline + 50.0, baseline + 50.0, normalization,
    ) == 50.0
    assert check_performance._paired_adjustment(
        baseline + 50.001, baseline, normalization,
    ) == 0.0
    assert check_performance._paired_adjustment(
        baseline, baseline + 50.001, normalization,
    ) == 0.0
    with pytest.raises(
        check_performance.MeasurementError,
        match="outside its validity envelope",
    ):
        check_performance._paired_adjustment(
            baseline + 50.001, baseline + 50.001, normalization,
        )


def test_pairing_is_before_target_after_and_failed_calibration_gives_no_credit(
    monkeypatch,
):
    metric = _short_metric(check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]["core_import"])
    baseline = metric["normalization"]["baseline_milliseconds"]
    calibration_values = iter((
        baseline + 10, baseline + 10,
        baseline + 20, baseline,
        baseline, baseline + 20,
    ))
    events = []

    def calibration(*_args, **_kwargs):
        events.append("calibration")
        return next(calibration_values)

    def target(*_args, **_kwargs):
        events.append("target")
        return 1.0, b"100.0\n"

    monkeypatch.setattr(check_performance, "_import_calibration_once", calibration)
    monkeypatch.setattr(check_performance, "_run", target)
    samples, adjustments, _details = check_performance._repeat_process_bracketed(
        (sys.executable, "-c", "pass"),
        {},
        Path("unused-marker"),
        metric,
        inner_float=True,
    )
    assert samples == [100.0, 100.0, 100.0]
    assert adjustments == [10.0, 0.0, 0.0]
    assert events == ["calibration", "target", "calibration"] * 3

    target_called = False

    def failed_calibration(*_args, **_kwargs):
        raise check_performance.MeasurementError("bad calibration")

    def forbidden_target(*_args, **_kwargs):
        nonlocal target_called
        target_called = True
        return 1.0, b"100.0\n"

    monkeypatch.setattr(
        check_performance, "_import_calibration_once", failed_calibration,
    )
    monkeypatch.setattr(check_performance, "_run", forbidden_target)
    with pytest.raises(check_performance.MeasurementError, match="bad calibration"):
        check_performance._repeat_process_bracketed(
            (sys.executable, "-c", "pass"),
            {},
            Path("unused-marker"),
            metric,
            inner_float=True,
        )
    assert target_called is False


def test_project_metric_regression_cannot_inflate_downstream_allowance():
    metrics = check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]
    core = _short_metric(metrics["core_import"])
    version = _short_metric(metrics["core_version"])
    registry = _short_metric(metrics["ext_passive_registry"])

    within_limit_core = check_performance.summarize(
        [97.606] * core["samples"],
        core,
        normalization_adjustments=[0.0] * core["samples"],
    )
    slow_version = check_performance.summarize(
        [193.635] * version["samples"],
        version,
        normalization_adjustments=[0.0] * version["samples"],
    )
    slow_registry = check_performance.summarize(
        [300.0] * registry["samples"],
        registry,
        normalization_adjustments=[0.0] * registry["samples"],
    )
    assert within_limit_core["passed"] is True
    assert slow_version["passed"] is False
    assert slow_registry["passed"] is False


def test_invalid_adjustments_and_abnormal_standalone_calibration_fail():
    metrics = check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]
    core = _short_metric(metrics["core_import"])
    with pytest.raises(
        check_performance.MeasurementError,
        match="paired host normalization is invalid",
    ):
        check_performance.summarize(
            [48.606] * core["samples"], core,
            normalization_adjustments=[0.0, 0.0],
        )
    calibration = _short_metric(metrics["calibration_import_workload"])
    result = check_performance.summarize(
        [99.713] * calibration["samples"], calibration,
    )
    assert result["passed"] is False


def test_isolated_environment_scrubs_credentials_and_builds_no_bytecode(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-survive")
    with check_performance.isolated_environment() as (env, marker, fixture):
        assert not any(check_performance._is_credential_name(name) for name in env)
        assert env["HOME"] != os.environ.get("HOME")
        assert env["PATH"] == str(fixture.parent)
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        value = check_performance._import_calibration_once(
            env, marker, profile="core_import", value_name="inner_ms",
        )
        assert value > 0
        calibration_root = Path(env["UNIFIED_PERF_IMPORT_CALIBRATION_ROOT"])
        assert not list(calibration_root.rglob("__pycache__"))
        assert not marker.exists()


def test_import_calibration_hides_repository_from_process_search_paths(monkeypatch):
    observed = {}
    real_run = check_performance._run

    def capture_run(argv, child_env, **kwargs):
        observed["cwd"] = kwargs.get("cwd")
        observed["pythonpath"] = child_env["PYTHONPATH"].split(os.pathsep)
        return real_run(argv, child_env, **kwargs)

    monkeypatch.setattr(check_performance, "_run", capture_run)
    with check_performance.isolated_environment() as (env, marker, _fixture):
        calibration_root = Path(
            env["UNIFIED_PERF_IMPORT_CALIBRATION_ROOT"]
        ).resolve()
        value = check_performance._import_calibration_once(
            env, marker, profile="core_import", value_name="inner_ms",
        )

        assert value > 0
        assert Path(observed["cwd"]).resolve() == calibration_root
        assert [Path(item).resolve() for item in observed["pythonpath"]] == [
            marker.parent.resolve(),
            calibration_root,
        ]
        assert check_performance.ROOT.resolve() not in [
            Path(item).resolve() for item in observed["pythonpath"]
        ]


@pytest.mark.parametrize("module", ("unified_cli", "unified_cli_ext"))
def test_import_calibration_canary_rejects_project_modules(module):
    with check_performance.isolated_environment() as (env, marker, _fixture):
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_CORE_IMPORTS"] = "1"
        child_env["UNIFIED_PERF_FORBID_EXT_IMPORTS"] = "1"
        check_performance._run(
            (sys.executable, "-c", "import " + module), child_env,
        )
        assert ("import:" + module) in marker.read_text(encoding="utf-8")


def test_core_fast_path_and_registry_canaries_remain_offline():
    metrics = check_performance.load_config(
        check_performance.DEFAULT_BASELINE
    )["metrics"]
    core = _short_metric(metrics["core_import"])
    registry = _short_metric(metrics["ext_passive_registry"])
    del core["normalization"]
    del registry["normalization"]
    with check_performance.isolated_environment() as (env, marker, _fixture):
        core_samples, _, _, _ = check_performance._measure_core_import(
            core, env, marker,
        )
        registry_samples, _, _, _ = check_performance._measure_ext_registry(
            registry, env, marker,
        )
    assert len(core_samples) == len(registry_samples) == 3
    assert not marker.exists()


def test_caught_entry_point_import_still_sets_the_guard_marker():
    code = (
        "try:\n import performance_canary\n"
        "except RuntimeError:\n pass\n"
        "else:\n raise AssertionError('canary unexpectedly loaded')\n"
    )
    with check_performance.isolated_environment() as (env, marker, _fixture):
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        check_performance._run((sys.executable, "-c", code), child_env)
        assert marker.read_text(encoding="utf-8") == "import:performance_canary\n"


def test_passive_registry_fails_a_caught_canary_import(monkeypatch):
    metric = {
        "samples": 1,
        "statistic": "p95",
        "threshold": {"kind": "fixed", "milliseconds": 250.0},
        "warmups": 0,
    }
    original_run = check_performance._run

    def caught_canary(_argv, child_env, _metric, **_kwargs):
        original_run((
            sys.executable,
            "-c",
            "try:\n import performance_canary\nexcept RuntimeError:\n pass\n",
        ), child_env)
        return [1.0]

    monkeypatch.setattr(check_performance, "_repeat_process", caught_canary)
    with check_performance.isolated_environment() as (env, marker, _fixture):
        with pytest.raises(
            check_performance.MeasurementError,
            match="forbidden import or provider subprocess",
        ):
            check_performance._measure_ext_registry(metric, env, marker)


def test_subprocess_guard_blocks_unknown_and_allows_named_fixture():
    blocked = (
        "import subprocess\n"
        "try:\n subprocess.run(['provider-must-not-run'], check=True)\n"
        "except RuntimeError:\n pass\n"
    )
    with check_performance.isolated_environment() as (env, marker, _fixture):
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        check_performance._run((sys.executable, "-c", blocked), child_env)
        assert marker.read_text(encoding="utf-8") == (
            "subprocess:provider-must-not-run\n"
        )

    allowed = (
        "import os, subprocess\n"
        "result=subprocess.run([os.environ['UNIFIED_PERF_ALLOWED_EXECUTABLE']], "
        "env=os.environ.copy(), stdout=subprocess.PIPE, check=True)\n"
        "assert b'hello from claude' in result.stdout\n"
    )
    with check_performance.isolated_environment() as (env, marker, fixture):
        child_env = dict(env)
        child_env.update({
            "FAKE_PROVIDER": "claude",
            "UNIFIED_PERF_ALLOWED_EXECUTABLE": str(fixture),
            "UNIFIED_PERF_FORBID_SUBPROCESSES": "1",
        })
        check_performance._run((sys.executable, "-c", allowed), child_env)
        assert not marker.exists()
