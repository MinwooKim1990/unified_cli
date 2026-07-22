"""Fail-closed contracts for the pinned same-metric performance gate."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
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


def _reference_copy(
    tmp_path: Path,
    *,
    sha: str = check_performance.REFERENCE_SHA,
    name: str = "reference",
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
    return root


def test_baseline_pins_reference_digest_anchors_and_exact_policies():
    config = check_performance.load_config(check_performance.DEFAULT_BASELINE)
    metrics = config["metrics"]
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
    assert metrics["ext_passive_registry"]["normalization"] == {
        "anchor_milliseconds": 195.661,
        "kind": "paired_same_metric_reference",
        "metric": "ext_passive_registry",
    }
    assert metrics["ext_passive_registry"]["samples"] == 61
    assert metrics["repl_first_prompt"]["samples"] == 31
    assert (
        metrics["repl_first_prompt"]["samples"],
        metrics["repl_first_prompt"]["statistic"],
        metrics["repl_first_prompt"]["warmups"],
    ) == (31, "p95", 3)
    assert check_performance._threshold(metrics["core_import"]) == 98.606
    assert check_performance._threshold(metrics["core_version"]) == 144.635
    assert check_performance._threshold(metrics["ext_passive_registry"]) == 250.0


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
    expected_digest = check_performance.source_tree_digest(reference)
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


def test_verified_reference_bytes_survive_canonical_mutation_and_use_random_snapshots(
    tmp_path,
):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=check_performance.source_tree_digest(reference),
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
        expected_digest=check_performance.source_tree_digest(reference),
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
    candidate = _reference_copy(tmp_path, name="candidate")
    expected_digest = check_performance.source_tree_digest(reference)
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
    assert check_performance.source_tree_digest(candidate) != expected_digest
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
    original = check_performance.source_tree_digest(reference)
    cache = reference / "src" / "unified_cli" / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"ambient")
    egg = reference / "src" / "unified_cli" / "ignored.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("ambient", encoding="utf-8")
    assert check_performance.source_tree_digest(reference) == original

    init = reference / "src" / "unified_cli" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace('"0.5.0"', '"0.5.1"', 1),
        encoding="utf-8",
    )
    with pytest.raises(check_performance.MeasurementError, match="version proof"):
        check_performance.source_tree_digest(reference)


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
        check_performance._read_source_manifest(reference)

    if not hasattr(os, "mkfifo"):
        return
    special = _reference_copy(tmp_path, name="special-reference")
    os.mkfifo(special / "src" / "unified_cli" / "special.pipe")
    with pytest.raises(check_performance.MeasurementError, match="special file"):
        check_performance._read_source_manifest(special)


def test_core_candidate_and_reference_origins_are_separately_proven(tmp_path):
    reference = _reference_copy(tmp_path)
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=check_performance.source_tree_digest(reference),
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
        expected_digest=check_performance.source_tree_digest(reference),
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


def test_all_python_children_use_B_and_do_not_create_bytecode(tmp_path):
    assert check_performance._python_argv("pass")[1:5] == ("-I", "-S", "-B", "-c")
    candidate = _reference_copy(tmp_path, name="candidate")
    reference = _reference_copy(tmp_path, name="reference")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=check_performance.source_tree_digest(reference),
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


def test_general_bootstrap_uses_empty_cwd_and_defeats_candidate_shadow_modules(
    tmp_path,
):
    candidate = _reference_copy(tmp_path, name="candidate")
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
        expected_digest=check_performance.source_tree_digest(reference),
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


def test_candidate_after_cannot_mutate_verified_reference_or_trigger_future_snapshot(
    tmp_path, monkeypatch,
):
    reference = _reference_copy(tmp_path, name="reference")
    candidate = _reference_copy(tmp_path, name="candidate")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=check_performance.source_tree_digest(reference),
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
    candidate = _reference_copy(tmp_path, name="candidate")
    manifest = check_performance._validate_reference_manifest(
        reference,
        expected_sha=check_performance.REFERENCE_SHA,
        expected_digest=check_performance.source_tree_digest(reference),
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
        child["UNIFIED_PERF_FORBID_MUTATIONS"] = "1"
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
