"""Source contract tests for the one-distribution Core + extension release."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "verify_single_distribution.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_single_distribution_for_tests", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _provider_entry_points(pyproject: str):
    section = pyproject.split(
        '[project.entry-points."unified_cli.providers.v1"]', 1
    )[1].split("\n[", 1)[0]
    return {
        name: target
        for name, target in re.findall(
            r'^([a-z][a-z0-9-]*) = "([^"]+)"$', section, re.MULTILINE
        )
    }


def test_root_metadata_and_independent_smoke_inventory_match():
    verifier = _load_verifier()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert _provider_entry_points(pyproject) == (
        verifier.EXPECTED_PROVIDER_ENTRY_POINTS
    )
    assert len(verifier.EXPECTED_PROVIDER_ENTRY_POINTS) == 18


def test_single_distribution_verifier_tracks_ext_support_statuses():
    verifier = _load_verifier()
    assert {"qoder", "kilo", "poolside"}.issubset(
        verifier.EXPECTED_PROVIDER_ENTRY_POINTS
    )
    source = SCRIPT.read_text(encoding="utf-8")
    assert (
        'provider_id: "preview" for provider_id in EXPECTED_PROVIDER_ENTRY_POINTS'
        in source
    )
    assert "Preview provider inventory does not match" in source


def test_only_root_project_is_buildable_and_publishable():
    assert (ROOT / "pyproject.toml").is_file()
    assert not (ROOT / "packages/unified-cli-ext/pyproject.toml").exists()
    assert not (ROOT / ".github/workflows/publish-ext.yml").exists()
    assert not (ROOT / "scripts/verify_distribution_pair.py").exists()


def test_ext_source_layout_remains_pinned_for_performance_reference():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'where = ["src", "packages/unified-cli-ext/src"]' in pyproject
    assert '"unified_cli_ext.*",' in pyproject
    assert (
        ROOT
        / "packages/unified-cli-ext/src/unified_cli_ext/__init__.py"
    ).is_file()
