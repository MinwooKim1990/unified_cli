"""Checks for the generated Ext provider support documentation table."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_ext_provider_support.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "check_ext_provider_support_for_tests", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_docs(tmp_path: Path) -> Path:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    for filename in ("extensions.md", "extensions.ko.md"):
        shutil.copyfile(ROOT / "docs" / filename, docs_dir / filename)
    return docs_dir


def _run_check(docs_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--check",
            "--docs-dir",
            str(docs_dir),
        ],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def test_generated_ext_provider_support_check_is_clean(tmp_path):
    result = _run_check(_copy_docs(tmp_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("filename", ("extensions.md", "extensions.ko.md"))
def test_grok_preview_docs_include_the_runnable_canonical_setup(filename):
    content = (ROOT / "docs" / filename).read_text(encoding="utf-8")
    assert '"bin" / "grok"' in content
    assert "configure_extension_provider" in content
    assert 'provider_home=str(root / "home")' in content
    assert "GROK_MANAGED_MCPS_ENABLED=false" in content
    assert "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED=false" in content
    assert "`.envrc`" in content
    assert "usage_is_incomplete" not in content


def test_generated_ext_provider_support_check_detects_modified_block(tmp_path):
    docs_dir = _copy_docs(tmp_path)
    english_doc = docs_dir / "extensions.md"
    english_doc.write_text(
        english_doc.read_text(encoding="utf-8").replace(
            "| `grok` | `preview` | `chat, sessions, stream` | `disabled` |",
            "| `grok` | `stable` | `none` | `disabled` |",
        ),
        encoding="utf-8",
    )

    result = _run_check(docs_dir)
    assert result.returncode == 1
    assert "generated Ext provider support table is out of date: extensions.md" in result.stderr


def test_entry_point_parser_rejects_malformed_declaration(tmp_path):
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        '[project.entry-points."unified_cli.providers.v1"]\n'
        'grok = "unified_cli_ext.providers.grok:NOT_PLUGIN"\n',
        encoding="utf-8",
    )

    module = _load_script_module()
    with pytest.raises(module.SupportTableError, match="invalid Ext provider entry-point target"):
        module.read_entry_points(pyproject_path)


def test_provider_rows_reject_entry_point_plugin_id_mismatch():
    module = _load_script_module()
    with pytest.raises(module.SupportTableError, match="does not match plugin id"):
        module.provider_rows(
            [("different", "unified_cli_ext.providers.grok:PLUGIN")]
        )
