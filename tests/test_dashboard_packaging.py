"""Dashboard package-resource loading and distribution metadata checks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_dashboard_html_is_loaded_from_package_resource():
    from unified_cli.dashboard_tpl import DASHBOARD_HTML, load_dashboard_asset

    html, mime = load_dashboard_asset("dashboard.html")
    assert html == DASHBOARD_HTML
    assert mime == "text/html; charset=utf-8"
    assert '/dashboard/assets/app.css' in html
    assert '/dashboard/assets/app.js' in html


@pytest.mark.parametrize("name, mime", [
    ("app.css", "text/css; charset=utf-8"),
    ("app.js", "text/javascript; charset=utf-8"),
])
def test_dashboard_asset_loader_allowlist(name, mime):
    from unified_cli.dashboard_tpl import load_dashboard_asset

    content, returned_mime = load_dashboard_asset(name)
    assert content
    assert returned_mime == mime


@pytest.mark.parametrize("name", ["../dashboard.html", "web/app.js", "missing.js", ""])
def test_dashboard_asset_loader_rejects_unknown_or_traversal(name):
    from unified_cli.dashboard_tpl import load_dashboard_asset

    with pytest.raises(ValueError, match="unknown dashboard asset"):
        load_dashboard_asset(name)


def test_package_metadata_declares_all_dashboard_assets():
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    for asset in ("web/dashboard.html", "web/app.css", "web/app.js"):
        assert asset in metadata


def test_browser_harness_is_pinned_and_not_a_core_runtime_dependency():
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    harness = (root / "tests" / "browser" / "package.json").read_text(encoding="utf-8")

    assert "playwright" not in metadata.lower()
    assert '"@playwright/test": "1.61.1"' in harness
    assert '"axe-core": "4.12.1"' in harness


def test_browser_harness_uses_isolated_fixtures_and_coverage_projects():
    root = Path(__file__).resolve().parents[1]
    fixture = (root / "tests" / "browser" / "fake_server.py").read_text(encoding="utf-8")
    spec = (root / "tests" / "browser" / "dashboard.spec.mjs").read_text(encoding="utf-8")
    config = (root / "tests" / "browser" / "playwright.config.mjs").read_text(encoding="utf-8")

    assert "tempfile.mkdtemp" in fixture
    assert "shutil.rmtree" in fixture
    assert "manage.list_models = _unexpected_provider_probe" in fixture
    assert "manage.subprocess.Popen = _unexpected_provider_probe" in fixture
    assert "EntryPoint.load = _unexpected_provider_probe" in fixture
    assert '"UNIFIED_CLI_DISABLE_PLUGINS": "1"' in fixture
    assert "readReadyLine" in spec and "waitForExit" in spec
    assert "#bootstrap=${encodeURIComponent(bootstrapToken)}" in spec
    assert "axe.run(document)" in spec
    for viewport in ("width: 360", "width: 768", "width: 1440"):
        assert viewport in config
