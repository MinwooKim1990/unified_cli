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


def test_versioned_baseline_is_complete_and_pins_stage0_startup_values():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)

    assert config["schema_version"] == 1
    assert config["metrics"]["core_import"]["baseline_milliseconds"] == 48.606
    assert config["metrics"]["core_version"]["baseline_milliseconds"] == 94.635
    assert config["metrics"]["calibration_process_startup"][
        "baseline_milliseconds"
    ] == 30.355
    assert config["metrics"]["ext_import"]["baseline_milliseconds"] == 52.066
    assert config["metrics"]["core_version"]["normalization"] == {
        "combine": "sum",
        "kind": "positive_baseline_delta",
        "references": ["calibration_process_startup", "core_import"],
    }
    assert config["metrics"]["ext_passive_registry"]["normalization"] == {
        "combine": "minimum",
        "kind": "positive_baseline_delta",
        "references": ["core_import", "ext_import"],
    }
    assert config["metrics"]["repl_first_prompt"]["threshold"] == {
        "kind": "fixed", "milliseconds": 300.0,
    }
    assert config["metrics"]["manage_bootstrap"]["threshold"]["milliseconds"] == 100.0
    assert config["metrics"]["stream_relay"]["threshold"]["milliseconds"] == 10.0


@pytest.mark.parametrize("payload", ({}, {"schema_version": 1}, []))
def test_malformed_or_incomplete_baseline_fails_closed(tmp_path, payload):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)


def test_missing_baseline_returns_stable_machine_error(tmp_path, capsys):
    assert check_performance.main([
        "--baseline", str(tmp_path / "missing.json"),
    ]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "error": "invalid_baseline",
        "passed": False,
        "schema_version": 1,
    }


def test_baseline_and_raw_overhead_ceilings_are_exact():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)

    assert check_performance._threshold(config["metrics"]["core_import"]) == 98.606
    assert check_performance._threshold(config["metrics"]["core_version"]) == 144.635
    assert check_performance._threshold(
        config["metrics"]["fake_cli_wrapper_overhead"], raw_median=600.0,
    ) == 60.0


def test_normalization_configuration_fails_closed(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    del config["metrics"]["ext_import"]["baseline_milliseconds"]
    baseline = tmp_path / "missing-reference-baseline.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"]["combine"] = []
    baseline = tmp_path / "malformed-combine.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"]["references"] = [
        {"not": "hashable"},
    ]
    baseline = tmp_path / "malformed-reference.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"]["references"] = [
        "missing_metric",
    ]
    baseline = tmp_path / "unknown-reference.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)

    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    config["metrics"]["core_version"]["normalization"]["references"] = [
        "ext_import",
    ]
    baseline = tmp_path / "future-reference.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(check_performance.PerformanceConfigError):
        check_performance.load_config(baseline)


def test_pre_normalization_v1_baseline_remains_compatible(tmp_path):
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    del config["metrics"]["calibration_process_startup"]["baseline_milliseconds"]
    del config["metrics"]["ext_import"]["baseline_milliseconds"]
    del config["metrics"]["core_version"]["normalization"]
    del config["metrics"]["ext_passive_registry"]["normalization"]
    baseline = tmp_path / "original-v1-shape.json"
    baseline.write_text(json.dumps(config), encoding="utf-8")

    loaded = check_performance.load_config(baseline)

    assert loaded == config
    assert check_performance._threshold(loaded["metrics"]["core_version"]) == 144.635


def test_core_version_normalization_cancels_host_penalties_not_regressions():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    metric = config["metrics"]["core_version"]
    references = {
        "calibration_process_startup": (130.355, 30.355),
        "core_import": (48.606, 48.606),
    }

    host_only = check_performance.summarize(
        [194.635] * metric["samples"],
        metric,
        normalization_references=references,
    )
    regressed = check_performance.summarize(
        [245.635] * metric["samples"],
        metric,
        normalization_references=references,
    )

    assert host_only["passed"] is True
    assert host_only["threshold_ms"] == 244.635
    assert host_only["details"]["host_normalization"] == {
        "adjustment_ms": 100.0,
        "kind": "positive_baseline_delta",
        "normalized_observed_ms": 94.635,
        "policy_threshold_ms": 144.635,
        "reference_deltas_ms": {
            "calibration_process_startup": 100.0,
            "core_import": 0.0,
        },
    }
    assert regressed["passed"] is False

    with pytest.raises(
        check_performance.MeasurementError,
        match="host normalization adjustment is invalid",
    ):
        check_performance.summarize(
            [194.635] * metric["samples"],
            metric,
            normalization_references={
                "calibration_process_startup": (1e308, 30.355),
                "core_import": (1e308, 48.606),
            },
        )


def test_ci_core_version_values_pass_but_reference_regression_still_fails_gate():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    version_metric = config["metrics"]["core_version"]
    ci_references = {
        "calibration_process_startup": (30.359, 30.355),
        "core_import": (97.357, 48.606),
    }

    ci_result = check_performance.summarize(
        [178.029] * version_metric["samples"],
        version_metric,
        normalization_references=ci_references,
    )
    assert ci_result["passed"] is True
    normalization = ci_result["details"]["host_normalization"]
    assert normalization["adjustment_ms"] == 48.755
    assert normalization["normalized_observed_ms"] == 129.274

    core_metric = config["metrics"]["core_import"]
    slow_core = check_performance.summarize(
        [108.606] * core_metric["samples"], core_metric,
    )
    version_using_slow_core = check_performance.summarize(
        [154.635] * version_metric["samples"],
        version_metric,
        normalization_references={
            "calibration_process_startup": (30.355, 30.355),
            "core_import": (108.606, 48.606),
        },
    )
    assert version_using_slow_core["passed"] is True
    assert slow_core["passed"] is False
    assert all(
        result["passed"] for result in (slow_core, version_using_slow_core)
    ) is False


def test_ext_normalization_requires_shared_host_slowdown_and_keeps_hard_gate():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    metric = config["metrics"]["ext_passive_registry"]
    ci_references = {
        "core_import": (97.357, 48.606),
        "ext_import": (141.474, 52.066),
    }

    ci_result = check_performance.summarize(
        [258.523] * metric["samples"],
        metric,
        normalization_references=ci_references,
    )
    regressed = check_performance.summarize(
        [299.523] * metric["samples"],
        metric,
        normalization_references=ci_references,
    )
    one_slow_reference = check_performance.summarize(
        [250.001] * metric["samples"],
        metric,
        normalization_references={
            "core_import": (48.606, 48.606),
            "ext_import": (152.066, 52.066),
        },
    )

    assert ci_result["passed"] is True
    normalization = ci_result["details"]["host_normalization"]
    assert normalization["adjustment_ms"] == 48.751
    assert normalization["normalized_observed_ms"] == 209.772
    assert regressed["passed"] is False
    assert one_slow_reference["threshold_ms"] == 250.0
    assert one_slow_reference["passed"] is False


def test_isolated_environment_scrubs_credentials_and_real_provider_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-survive")

    with check_performance.isolated_environment() as (env, marker, fixture):
        assert not any(check_performance._is_credential_name(name) for name in env)
        assert env["HOME"] != os.environ.get("HOME")
        assert env["PATH"] == str(fixture.parent)
        assert fixture.name == "fixture-provider-cli"
        assert not marker.exists()


def test_core_fast_path_and_passive_canary_run_offline_without_plugin_import():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    core_metric = copy.deepcopy(config["metrics"]["core_import"])
    registry_metric = copy.deepcopy(config["metrics"]["ext_passive_registry"])
    for metric in (core_metric, registry_metric):
        metric["samples"] = 3
        metric["warmups"] = 0

    with check_performance.isolated_environment() as (env, marker, _fixture):
        core = check_performance._measure_core_import(core_metric, env, marker)
        registry = check_performance._measure_ext_registry(registry_metric, env, marker)

    assert len(core) == len(registry) == 3
    assert not marker.exists()


def test_caught_entry_point_import_still_sets_the_guard_marker():
    code = (
        "try:\n"
        " import performance_canary\n"
        "except RuntimeError:\n"
        " pass\n"
        "else:\n"
        " raise AssertionError('canary unexpectedly loaded')\n"
        "print('caught')\n"
    )
    with check_performance.isolated_environment() as (env, marker, _fixture):
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] = "1"
        _elapsed, payload = check_performance._run(
            (sys.executable, "-c", code), child_env,
        )

        assert payload == b"caught\n"
        assert marker.read_text(encoding="utf-8") == "import:performance_canary\n"
        with pytest.raises(check_performance.MeasurementError):
            check_performance._assert_guard_marker_clear(marker)


def test_passive_registry_metric_fails_a_caught_canary_import(monkeypatch):
    metric = {
        "samples": 1,
        "statistic": "p95",
        "threshold": {"kind": "fixed", "milliseconds": 250.0},
        "warmups": 0,
    }
    original_run = check_performance._run

    def caught_canary(_argv, child_env, _metric, **_kwargs):
        assert child_env["UNIFIED_PERF_FORBID_ENTRYPOINT_IMPORTS"] == "1"
        assert child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] == "1"
        code = (
            "try:\n"
            " import performance_canary\n"
            "except RuntimeError:\n"
            " pass\n"
        )
        original_run((sys.executable, "-c", code), child_env)
        return [1.0]

    monkeypatch.setattr(check_performance, "_repeat_process", caught_canary)
    with check_performance.isolated_environment() as (env, marker, _fixture):
        with pytest.raises(
            check_performance.MeasurementError,
            match="forbidden import or provider subprocess",
        ):
            check_performance._measure_ext_registry(metric, env, marker)


def test_caught_provider_subprocess_attempt_still_sets_the_guard_marker():
    code = (
        "import subprocess\n"
        "try:\n"
        " subprocess.run(['provider-must-not-run'], check=True)\n"
        "except RuntimeError:\n"
        " pass\n"
        "else:\n"
        " raise AssertionError('provider executable unexpectedly ran')\n"
        "print('caught')\n"
    )
    with check_performance.isolated_environment() as (env, marker, _fixture):
        child_env = dict(env)
        child_env["UNIFIED_PERF_FORBID_SUBPROCESSES"] = "1"
        _elapsed, payload = check_performance._run(
            (sys.executable, "-c", code), child_env,
        )

        assert payload == b"caught\n"
        assert marker.read_text(encoding="utf-8") == (
            "subprocess:provider-must-not-run\n"
        )
        with pytest.raises(check_performance.MeasurementError):
            check_performance._assert_guard_marker_clear(marker)


def test_subprocess_guard_allows_only_the_named_fixture_executable():
    code = (
        "import os, subprocess\n"
        "result=subprocess.run([os.environ['UNIFIED_PERF_ALLOWED_EXECUTABLE']], "
        "env=os.environ.copy(), stdout=subprocess.PIPE, check=True)\n"
        "assert b'hello from claude' in result.stdout\n"
        "print('fixture-only')\n"
    )
    with check_performance.isolated_environment() as (env, marker, fixture):
        child_env = dict(env)
        child_env.update({
            "FAKE_PROVIDER": "claude",
            "UNIFIED_PERF_ALLOWED_EXECUTABLE": str(fixture),
            "UNIFIED_PERF_FORBID_SUBPROCESSES": "1",
        })
        _elapsed, payload = check_performance._run(
            (sys.executable, "-c", code), child_env,
        )

        assert payload == b"fixture-only\n"
        assert not marker.exists()
