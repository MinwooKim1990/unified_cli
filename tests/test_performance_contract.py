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
