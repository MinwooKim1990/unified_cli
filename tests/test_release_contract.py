"""Source-level release workflow and runbook drift checks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CORE_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
EXT_WORKFLOW = ROOT / ".github" / "workflows" / "publish-ext.yml"
RELEASE_ASSET_SCRIPT = ROOT / "scripts" / "verify_github_release_assets.py"
_RELEASE_ASSET_SPEC = importlib.util.spec_from_file_location(
    "verify_github_release_assets", RELEASE_ASSET_SCRIPT
)
assert _RELEASE_ASSET_SPEC is not None and _RELEASE_ASSET_SPEC.loader is not None
verify_github_release_assets = importlib.util.module_from_spec(_RELEASE_ASSET_SPEC)
sys.modules[_RELEASE_ASSET_SPEC.name] = verify_github_release_assets
_RELEASE_ASSET_SPEC.loader.exec_module(verify_github_release_assets)

CORE_WHEEL = "unified_cli-0.5.0-py3-none-any.whl"
CORE_SDIST = "unified_cli-0.5.0.tar.gz"


def _text(path):
    return path.read_text(encoding="utf-8")


def _release_asset_fixture(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    payloads = {CORE_WHEEL: b"wheel", CORE_SDIST: b"sdist"}
    records = []
    for name, payload in payloads.items():
        (assets / name).write_bytes(payload)
        records.append({
            "name": name,
            "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        })
    release = {
        "assets": records,
        "isDraft": False,
        "isPrerelease": False,
        "tagName": "v0.5.0",
    }
    release_json = tmp_path / "release.json"
    release_json.write_text(json.dumps(release), encoding="utf-8")
    return release_json, assets, release


def test_release_versions_and_changelogs_stay_aligned():
    core_init = _text(ROOT / "src" / "unified_cli" / "__init__.py")
    ext_init = _text(
        ROOT / "packages" / "unified-cli-ext" / "src" / "unified_cli_ext" / "__init__.py"
    )
    ext_project = _text(ROOT / "packages" / "unified-cli-ext" / "pyproject.toml")

    assert '__version__ = "0.5.0"' in core_init
    assert '__version__ = "0.1.0"' in ext_init
    assert 'version = "0.1.0"' in ext_project
    assert '"unified-cli>=0.5,<0.6"' in ext_project
    assert "## [0.5.0]" in _text(ROOT / "CHANGELOG.md")
    assert "## [0.1.0]" in _text(
        ROOT / "packages" / "unified-cli-ext" / "CHANGELOG.md"
    )


def test_source_only_release_tests_are_not_shipped_in_the_core_sdist():
    manifest = _text(ROOT / "MANIFEST.in")
    for name in (
        "test_distribution_pair.py",
        "test_performance_contract.py",
        "test_release_artifacts.py",
        "test_release_contract.py",
    ):
        assert "exclude tests/" + name in manifest


def test_publish_workflows_require_exact_clean_main_not_ancestry_only():
    core = _text(CORE_WORKFLOW)
    ext = _text(EXT_WORKFLOW)

    for workflow in (core, ext):
        assert 'main_commit="$(git rev-parse \'refs/remotes/origin/main^{commit}\')"' in workflow
        assert 'test "$tag_commit" = "$main_commit"' in workflow
        assert "git status --porcelain=v1 --untracked-files=all" in workflow
        assert "merge-base --is-ancestor" not in workflow
        assert "skip-existing" not in workflow
        assert "workflow_dispatch" not in workflow
    assert 'core_commit="$(git rev-parse "refs/tags/v${CORE_VERSION}^{commit}")"' in ext
    assert 'test "$tag_commit" = "$core_commit"' in ext


def test_release_artifacts_and_public_smokes_cannot_mix_core_and_ext():
    core = _text(CORE_WORKFLOW)
    ext = _text(EXT_WORKFLOW)

    assert "python -m build --outdir dist/core ." in core
    assert "name: core-dist-${{ github.sha }}" in core
    assert "packages-dir: dist/core/" in core
    assert "--package-root unified_cli --forbid-package-root unified_cli_ext" in core
    assert '--requires-dist "rich>=13"' in core
    assert '--requires-dist "prompt-toolkit>=3.0.43"' in core
    assert "unified_cli_ext.__version__" not in core

    assert 'python -m build --outdir dist/ext "${EXT_PACKAGE_DIR}"' in ext
    assert "name: ext-dist-${{ github.sha }}" in ext
    assert "packages-dir: dist/ext/" in ext
    assert "--package-root unified_cli_ext --forbid-package-root unified_cli" in ext
    assert '--requires-dist "unified-cli>=0.5,<0.6"' in ext
    assert 'assert unified_cli.__version__ == os.environ["CORE_VERSION"]' in ext
    assert 'assert unified_cli_ext.__version__ == os.environ["EXT_VERSION"]' in ext


def test_github_releases_are_mandatory_and_only_follow_public_pypi_smoke():
    core = _text(CORE_WORKFLOW)
    ext = _text(EXT_WORKFLOW)

    for workflow in (core, ext):
        assert workflow.index("  publish:") < workflow.index("  pypi-smoke:")
        assert workflow.index("  pypi-smoke:") < workflow.index("  github-release:")
        release_job = workflow[workflow.index("  github-release:"):]
        assert "needs: pypi-smoke" in release_job
        assert "contents: write" in release_job
        assert 'gh release create "$RELEASE_TAG"' in release_job
        assert "--verify-tag" in release_job
        assert '"$EXPECTED_WHEEL" "$EXPECTED_SDIST"' in release_job
        assert 'gh release view "$RELEASE_TAG"' in release_job
        assert 'gh release download "$RELEASE_TAG"' in release_job
        assert 'payload.get("isDraft") is not False' in release_job
        assert 'payload.get("isPrerelease") is not False' in release_job
        assert 'cmp "$EXPECTED_WHEEL"' in release_job
        assert 'cmp "$EXPECTED_SDIST"' in release_job
    assert '"unified-cli==${CORE_VERSION}"' in core
    assert '"unified-cli-ext==${EXT_VERSION}"' in ext


def test_github_release_jobs_redownload_only_the_verified_build_artifacts():
    core = _text(CORE_WORKFLOW)
    ext = _text(EXT_WORKFLOW)
    core_release = core[core.index("  github-release:"):]
    ext_release = ext[ext.index("  github-release:"):]

    download_pin = (
        "actions/download-artifact@"
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
    )
    assert download_pin in core_release
    assert "name: core-dist-${{ github.sha }}" in core_release
    assert "path: dist/core/" in core_release
    assert "unified_cli-${{ env.CORE_VERSION }}-py3-none-any.whl" in core_release
    assert "unified_cli-${{ env.CORE_VERSION }}.tar.gz" in core_release

    assert download_pin in ext_release
    assert "name: ext-dist-${{ github.sha }}" in ext_release
    assert "path: dist/ext/" in ext_release
    assert "unified_cli_ext-${{ env.EXT_VERSION }}-py3-none-any.whl" in ext_release
    assert "unified_cli_ext-${{ env.EXT_VERSION }}.tar.gz" in ext_release


def test_ext_requires_the_final_core_github_release_before_testing_or_building():
    ext = _text(EXT_WORKFLOW)
    verify = ext[ext.index("  verify-release:"):ext.index("  test-and-build:")]

    assert 'CORE_RELEASE_TAG: v${{ env.CORE_VERSION }}' in verify
    assert "EXPECTED_CORE_WHEEL:" in verify and "EXPECTED_CORE_SDIST:" in verify
    assert 'gh release view "$CORE_RELEASE_TAG"' in verify
    assert "--json tagName,isDraft,isPrerelease,assets" in verify
    assert 'test ! -e "$core_assets_dir"' in verify
    assert "python scripts/verify_github_release_assets.py" in verify
    assert "--manifest-only" in verify
    assert 'gh release download "$CORE_RELEASE_TAG"' in verify
    assert '--pattern "$EXPECTED_CORE_WHEEL"' in verify
    assert '--pattern "$EXPECTED_CORE_SDIST"' in verify
    assert 'python scripts/verify_release_artifacts.py "$core_assets_dir"' in verify
    assert '--requires-dist "rich>=13"' in verify
    assert '--requires-dist "prompt-toolkit>=3.0.43"' in verify
    assert verify.index("--manifest-only") < verify.index("gh release download")
    assert verify.index("gh release download") < verify.rindex(
        "python scripts/verify_github_release_assets.py"
    )
    assert verify.rindex("python scripts/verify_github_release_assets.py") < verify.index(
        'python scripts/verify_release_artifacts.py "$core_assets_dir"'
    )


def test_final_core_release_asset_manifest_and_downloaded_bytes_pass(tmp_path):
    release_json, assets, _release = _release_asset_fixture(tmp_path)

    wheel, sdist = verify_github_release_assets.verify_release_assets(
        release_json,
        assets,
        expected_tag="v0.5.0",
        wheel_name=CORE_WHEEL,
        sdist_name=CORE_SDIST,
    )

    assert wheel == assets / CORE_WHEEL
    assert sdist == assets / CORE_SDIST


@pytest.mark.parametrize(
    "corruption",
    (
        "draft",
        "extra-asset",
        "extra-downloaded-file",
        "missing-digest",
        "prerelease",
        "wrong-bytes",
        "wrong-digest",
        "wrong-size",
        "wrong-tag",
        "zero-byte",
    ),
)
def test_final_core_release_asset_corruption_fails_closed(tmp_path, corruption):
    release_json, assets, release = _release_asset_fixture(tmp_path)
    wheel_record = next(item for item in release["assets"] if item["name"] == CORE_WHEEL)
    if corruption == "draft":
        release["isDraft"] = True
    elif corruption == "extra-asset":
        payload = b"extra"
        (assets / "unexpected.whl").write_bytes(payload)
        release["assets"].append({
            "name": "unexpected.whl",
            "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        })
    elif corruption == "extra-downloaded-file":
        (assets / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif corruption == "missing-digest":
        wheel_record["digest"] = None
    elif corruption == "prerelease":
        release["isPrerelease"] = True
    elif corruption == "wrong-bytes":
        (assets / CORE_WHEEL).write_bytes(b"WHEEL")
    elif corruption == "wrong-digest":
        wheel_record["digest"] = "sha256:" + ("0" * 64)
    elif corruption == "wrong-size":
        wheel_record["size"] += 1
    elif corruption == "wrong-tag":
        release["tagName"] = "v0.5.1"
    elif corruption == "zero-byte":
        (assets / CORE_WHEEL).write_bytes(b"")
        wheel_record["size"] = 0
        wheel_record["digest"] = "sha256:" + hashlib.sha256(b"").hexdigest()
    else:
        raise AssertionError("unknown corruption")
    release_json.write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(
        verify_github_release_assets.ReleaseAssetVerificationError
    ):
        verify_github_release_assets.verify_release_assets(
            release_json,
            assets,
            expected_tag="v0.5.0",
            wheel_name=CORE_WHEEL,
            sdist_name=CORE_SDIST,
        )


def test_public_pypi_smokes_ignore_private_indexes_links_and_no_index_config():
    for path in (CORE_WORKFLOW, EXT_WORKFLOW):
        workflow = _text(path)
        smoke = workflow[workflow.index("  pypi-smoke:"):workflow.index("  github-release:")]
        assert "PIP_CONFIG_FILE: /dev/null" in smoke
        assert 'PIP_EXTRA_INDEX_URL: ""' in smoke
        assert 'PIP_FIND_LINKS: ""' in smoke
        assert "PIP_INDEX_URL: https://pypi.org/simple" in smoke
        assert 'PIP_NO_INDEX: "false"' in smoke
        assert '--index-url "${PIP_INDEX_URL}"' in smoke
        assert "--no-cache-dir" in smoke


def test_actions_are_commit_pinned_and_context_values_enter_shell_via_env():
    expected_actions = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247",
    }
    for path in (CORE_WORKFLOW, EXT_WORKFLOW):
        workflow = _text(path)
        uses = re.findall(r"^\s*uses:\s*([^\s]+)", workflow, flags=re.MULTILINE)
        assert uses
        assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
        assert set(uses).issubset(expected_actions)
        assert "RELEASE_TAG: ${{ github.ref_name }}" in workflow
        assert 'refs/tags/${{ github.ref_name }}' not in workflow


def test_oidc_permission_is_confined_to_the_artifact_only_publish_job():
    for path in (CORE_WORKFLOW, EXT_WORKFLOW):
        workflow = _text(path)
        publish = workflow[workflow.index("  publish:"):workflow.index("  pypi-smoke:")]
        smoke_and_release = workflow[workflow.index("  pypi-smoke:"):]
        assert publish.count("id-token: write") == 1
        assert publish.count("uses:") == 2
        assert "pytest" not in publish and "pip install" not in publish
        assert "id-token: write" not in smoke_and_release


def test_ci_uses_current_immutable_checkout_and_python_action_pins():
    ci = _text(ROOT / ".github" / "workflows" / "ci.yml")
    uses = set(re.findall(r"^\s*uses:\s*([^\s]+)", ci, flags=re.MULTILINE))
    assert uses == {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    }


def test_ci_and_runbook_preserve_the_ordered_offline_readiness_contract():
    ci = _text(ROOT / ".github" / "workflows" / "ci.yml")
    runbook = _text(ROOT / "RELEASING.md")

    assert "python scripts/check_performance.py" in ci
    assert "python scripts/verify_release_artifacts.py dist/core" in ci
    assert "python scripts/verify_release_artifacts.py dist/ext" in ci
    assert '--requires-dist "rich>=13"' in ci
    assert '--requires-dist "prompt-toolkit>=3.0.43"' in ci
    assert '--requires-dist "unified-cli>=0.5,<0.6"' in ci
    core_release = runbook.index("## Release 1 of 2: Core 0.5.0")
    ext_release = runbook.index("## Release 2 of 2: Ext 0.1.0")
    rollback = runbook.index("## Failure and rollback rules")
    assert core_release < ext_release < rollback
    assert "same exact clean commit at the tip of `main`" in runbook
    assert "Never move, delete, or reuse a release tag" in runbook
    assert "Never upload the same version twice" in runbook
    assert "two GitHub Releases are mandatory" in runbook
    assert "`rich>=13`" in runbook and "`prompt-toolkit>=3.0.43`" in runbook
    assert "recorded sizes and SHA-256 digests" in runbook
    assert "exactly one default-runtime dependency" in runbook
    assert "yank only `unified-cli-ext` 0.1.0" in runbook
    assert "Leave Core 0.5.0" in runbook
    assert "publish.yml" in runbook and "environment `pypi`" in runbook
    assert "publish-ext.yml" in runbook and "environment `pypi-ext`" in runbook
