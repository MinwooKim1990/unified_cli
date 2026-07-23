"""Fail-closed source contracts for the unified 0.5.3 release path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
EXT_WORKFLOW = ROOT / ".github" / "workflows" / "publish-ext.yml"
PERFORMANCE_REQUIREMENTS = ROOT / "scripts" / "performance-requirements.txt"
RELEASE_ASSET_SCRIPT = ROOT / "scripts" / "verify_github_release_assets.py"

_RELEASE_ASSET_SPEC = importlib.util.spec_from_file_location(
    "verify_github_release_assets", RELEASE_ASSET_SCRIPT
)
assert _RELEASE_ASSET_SPEC is not None
assert _RELEASE_ASSET_SPEC.loader is not None
verify_github_release_assets = importlib.util.module_from_spec(
    _RELEASE_ASSET_SPEC
)
sys.modules[_RELEASE_ASSET_SPEC.name] = verify_github_release_assets
_RELEASE_ASSET_SPEC.loader.exec_module(verify_github_release_assets)

WHEEL = "unified_cli-0.5.3-py3-none-any.whl"
SDIST = "unified_cli-0.5.3.tar.gz"
PERFORMANCE_REFERENCE_SHA = "be1478884735c862e894959944ba53e149ea4210"
LEGACY_SPLIT_SHA = "7abb7ebc36a4668b3cc9634fd65af5c75b30c758"
PERFORMANCE_INSTALL_COMMAND = (
    "PIP_CONFIG_FILE=/dev/null python -m pip install --isolated --no-cache-dir "
    "--require-hashes --only-binary=:all: --index-url https://pypi.org/simple "
    "-r scripts/performance-requirements.txt"
)
PERFORMANCE_DEPENDENCIES = {
    "annotated-doc": "0.0.4",
    "annotated-types": "0.7.0",
    "anyio": "4.14.2",
    "click": "8.4.2",
    "fastapi": "0.139.2",
    "h11": "0.16.0",
    "idna": "3.18",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "prompt-toolkit": "3.0.52",
    "pydantic": "2.13.4",
    "pydantic-core": "2.46.4",
    "pygments": "2.20.0",
    "rich": "15.0.0",
    "starlette": "1.3.1",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.2",
    "uvicorn": "0.51.0",
    "wcwidth": "0.8.2",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _job_section(workflow: str, job_name: str) -> str:
    start = workflow.index("  " + job_name + ":\n")
    following = re.search(
        r"^  [a-z][a-z0-9-]*:\n", workflow[start + 1 :], re.M
    )
    if following is None:
        return workflow[start:]
    return workflow[start : start + 1 + following.start()]


def _checkout_blocks(workflow: str):
    for match in re.finditer(
        r"^        uses: actions/checkout@[^\n]+$", workflow, re.M
    ):
        next_step = workflow.find("\n      - name:", match.end())
        yield (
            workflow[match.start() :]
            if next_step == -1
            else workflow[match.start() : next_step]
        )


def _assert_hash_locked_performance_dependencies(lock: str) -> None:
    for name, version in PERFORMANCE_DEPENDENCIES.items():
        pattern = (
            r"^"
            + re.escape(name + "==" + version)
            + r" \\\n    --hash=sha256:[0-9a-f]{64}(?: \\)?$"
        )
        assert re.search(pattern, lock, re.M), (
            name + " must be exact and hash pinned"
        )
    assert lock.count("--hash=sha256:") == len(PERFORMANCE_DEPENDENCIES) + 1
    assert ">=" not in lock and "~=" not in lock


def _release_asset_fixture(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    payloads = {WHEEL: b"wheel", SDIST: b"sdist"}
    records = []
    for name, payload in payloads.items():
        (assets / name).write_bytes(payload)
        records.append(
            {
                "name": name,
                "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
    release = {
        "assets": records,
        "isDraft": False,
        "isPrerelease": False,
        "tagName": "v0.5.3",
    }
    release_json = tmp_path / "release.json"
    release_json.write_text(json.dumps(release), encoding="utf-8")
    return release_json, assets, release


def test_one_project_owns_both_namespace_versions_and_release_record():
    core_init = _text(ROOT / "src/unified_cli/__init__.py")
    ext_init = _text(
        ROOT
        / "packages/unified-cli-ext/src/unified_cli_ext/__init__.py"
    )
    pyproject = _text(ROOT / "pyproject.toml")

    assert '__version__ = "0.5.3"' in core_init
    assert '__version__ = "0.5.3"' in ext_init
    assert 'name = "unified-cli"' in pyproject
    assert 'where = ["src", "packages/unified-cli-ext/src"]' in pyproject
    assert pyproject.count(
        '[project.entry-points."unified_cli.providers.v1"]'
    ) == 1
    assert len(
        re.findall(
            r'^[a-z][a-z0-9-]* = "unified_cli_ext\.providers\.[^"]+:PLUGIN"$',
            pyproject,
            re.M,
        )
    ) == 18
    assert '"unified-cli-ext' not in pyproject
    assert "## [0.5.3] - 2026-07-23" in _text(ROOT / "CHANGELOG.md")
    assert "## [0.5.1] - 2026-07-23" in _text(ROOT / "CHANGELOG.md")
    assert "## [0.5.0]" in _text(ROOT / "CHANGELOG.md")


def test_no_second_build_or_publish_surface_exists():
    assert not (ROOT / "packages/unified-cli-ext/pyproject.toml").exists()
    assert not (ROOT / "packages/unified-cli-ext/MANIFEST.in").exists()
    assert not EXT_WORKFLOW.exists()
    assert not (ROOT / "scripts/verify_distribution_pair.py").exists()
    assert not (ROOT / "tests/test_distribution_pair.py").exists()


def test_source_only_release_tests_are_not_shipped_in_sdist():
    manifest = _text(ROOT / "MANIFEST.in")
    for name in (
        "test_single_distribution.py",
        "test_performance_contract.py",
        "test_release_artifacts.py",
        "test_release_contract.py",
    ):
        assert "exclude tests/" + name in manifest
    assert "prune tools" in manifest
    assert "exclude scripts/unified-ext-lab*" in manifest


def test_publish_requires_exact_clean_main_and_exact_version_tag():
    workflow = _text(PUBLISH_WORKFLOW)
    assert 'main_commit="$(git rev-parse \'refs/remotes/origin/main^{commit}\')"' in workflow
    assert 'test "$tag_commit" = "$main_commit"' in workflow
    assert "git status --porcelain=v1 --untracked-files=all" in workflow
    assert "merge-base --is-ancestor" not in workflow
    assert "skip-existing" not in workflow
    assert "workflow_dispatch" not in workflow
    assert 'RELEASE_VERSION: "0.5.3"' in workflow
    assert 'os.environ["RELEASE_TAG"] != "v" + expected' in workflow
    assert workflow.count("namespace version source is ambiguous or wrong") == 1
    assert "legacy Ext project still exists" in workflow
    assert "legacy Ext publisher still exists" in workflow


def test_performance_jobs_share_the_immutable_hash_locked_reference():
    lock = _text(PERFORMANCE_REQUIREMENTS)
    _assert_hash_locked_performance_dependencies(lock)
    install_commands = []
    for workflow_path in (CI_WORKFLOW, PUBLISH_WORKFLOW):
        workflow = _text(workflow_path)
        performance = _job_section(workflow, "performance")
        assert "runs-on: ubuntu-24.04" in performance
        assert 'python-version: "3.14.6"' in performance
        assert "path: performance-reference" in performance
        assert "ref: " + PERFORMANCE_REFERENCE_SHA in performance
        assert performance.count("persist-credentials: false") == 2
        assert "python scripts/check_performance.py" in performance
        assert "--reference-root performance-reference" in performance
        install = re.search(
            r"Install hash-locked performance dependencies\n"
            r"        run: >-\n"
            r"          (?P<command>[^\n]+)",
            performance,
        )
        assert install is not None
        install_commands.append(install.group("command"))
    assert install_commands == [PERFORMANCE_INSTALL_COMMAND] * 2


def test_every_checkout_is_credential_free_and_actions_are_commit_pinned():
    expected_actions = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247",
    }
    for workflow_path in (CI_WORKFLOW, PUBLISH_WORKFLOW):
        workflow = _text(workflow_path)
        blocks = tuple(_checkout_blocks(workflow))
        assert blocks
        assert all("persist-credentials: false" in block for block in blocks)
        uses = re.findall(
            r"^\s*uses:\s*([^\s]+)", workflow, flags=re.MULTILINE
        )
        assert uses
        assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
        assert set(uses).issubset(expected_actions)
    assert "RELEASE_TAG: ${{ github.ref_name }}" in _text(PUBLISH_WORKFLOW)
    assert "refs/tags/${{ github.ref_name }}" not in _text(PUBLISH_WORKFLOW)


def test_ci_required_gate_covers_every_required_job():
    required = _job_section(_text(CI_WORKFLOW), "required-ci")
    jobs = (
        "test",
        "performance",
        "ext-test",
        "ext-optional-extras",
        "ext-lab-fixture",
        "unified-distribution",
    )
    assert "name: Required CI gate" in required
    assert "if: always()" in required
    assert (
        "needs: [test, performance, ext-test, ext-optional-extras, "
        "ext-lab-fixture, unified-distribution]"
    ) in required
    for job in jobs:
        assert 'test "${{ needs.' + job + '.result }}" = "success"' in required


def test_ci_builds_one_artifact_and_tests_legacy_split_cleanup():
    ci = _text(CI_WORKFLOW)
    unified = _job_section(ci, "unified-distribution")
    assert "python -m build --outdir dist/unified ." in unified
    assert "python -m build --outdir dist/ext" not in unified
    assert "dist/unified_cli_ext" not in unified
    assert "verify_unified_release_artifacts.py" in unified
    assert "verify_single_distribution.py" in unified
    assert "Rebuild the wheel from the verified sdist" in unified
    assert "pip uninstall -y \\\n            unified-cli-ext" in unified
    assert "--force-reinstall" in unified
    assert '"unified-cli==0.5.0"' in unified
    assert "ref: " + LEGACY_SPLIT_SHA in unified
    assert "unified_cli_ext-0.1.0-py3-none-any.whl" in unified
    assert "find_spec('unified_cli_ext') is None" in unified


def test_release_build_publish_smoke_and_release_order_is_fail_closed():
    workflow = _text(PUBLISH_WORKFLOW)
    assert workflow.index("  build:") < workflow.index("  publish:")
    assert workflow.index("  publish:") < workflow.index("  pypi-smoke:")
    assert workflow.index("  pypi-smoke:") < workflow.index(
        "  github-release:"
    )
    build = _job_section(workflow, "build")
    assert "needs: [verify-release, test, performance]" in build
    assert "python -m build --outdir dist/unified ." in build
    assert "verify_unified_release_artifacts.py" in build
    assert "verify_single_distribution.py" in build
    assert "python -m twine check dist/unified/*" in build
    publish = _job_section(workflow, "publish")
    assert "environment: pypi" in publish
    assert "packages-dir: dist/unified/" in publish
    smoke = _job_section(workflow, "pypi-smoke")
    assert '"unified-cli==${RELEASE_VERSION}"' in smoke
    assert "verify_single_distribution.py" in smoke
    release = _job_section(workflow, "github-release")
    assert "needs: pypi-smoke" in release
    assert 'gh release create "$RELEASE_TAG"' in release
    assert "--verify-tag" in release
    assert "verify_github_release_assets.py" in release
    assert "verify_unified_release_artifacts.py" in release
    assert 'cmp "$EXPECTED_WHEEL"' in release
    assert 'cmp "$EXPECTED_SDIST"' in release


def test_public_pypi_smoke_ignores_private_indexes_and_local_links():
    smoke = _job_section(_text(PUBLISH_WORKFLOW), "pypi-smoke")
    assert "PIP_CONFIG_FILE: /dev/null" in smoke
    assert 'PIP_EXTRA_INDEX_URL: ""' in smoke
    assert 'PIP_FIND_LINKS: ""' in smoke
    assert "PIP_INDEX_URL: https://pypi.org/simple" in smoke
    assert 'PIP_NO_INDEX: "false"' in smoke
    assert '--index-url "${PIP_INDEX_URL}"' in smoke
    assert "--no-cache-dir" in smoke


def test_oidc_permission_is_confined_to_artifact_only_publish_job():
    workflow = _text(PUBLISH_WORKFLOW)
    publish = _job_section(workflow, "publish")
    after_publish = workflow[workflow.index("  pypi-smoke:") :]
    assert workflow.count("id-token: write") == 1
    assert publish.count("id-token: write") == 1
    assert publish.count("uses:") == 2
    assert "pytest" not in publish
    assert "pip install" not in publish
    assert "id-token: write" not in after_publish


def test_final_release_asset_manifest_and_downloaded_bytes_pass(tmp_path):
    release_json, assets, _ = _release_asset_fixture(tmp_path)
    wheel, sdist = verify_github_release_assets.verify_release_assets(
        release_json,
        assets,
        expected_tag="v0.5.3",
        wheel_name=WHEEL,
        sdist_name=SDIST,
    )
    assert wheel == assets / WHEEL
    assert sdist == assets / SDIST


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
def test_final_release_asset_corruption_fails_closed(tmp_path, corruption):
    release_json, assets, release = _release_asset_fixture(tmp_path)
    wheel_record = next(
        item for item in release["assets"] if item["name"] == WHEEL
    )
    if corruption == "draft":
        release["isDraft"] = True
    elif corruption == "extra-asset":
        payload = b"extra"
        (assets / "unexpected.whl").write_bytes(payload)
        release["assets"].append(
            {
                "name": "unexpected.whl",
                "size": len(payload),
                "digest": "sha256:"
                + hashlib.sha256(payload).hexdigest(),
            }
        )
    elif corruption == "extra-downloaded-file":
        (assets / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif corruption == "missing-digest":
        wheel_record["digest"] = None
    elif corruption == "prerelease":
        release["isPrerelease"] = True
    elif corruption == "wrong-bytes":
        (assets / WHEEL).write_bytes(b"WHEEL")
    elif corruption == "wrong-digest":
        wheel_record["digest"] = "sha256:" + ("0" * 64)
    elif corruption == "wrong-size":
        wheel_record["size"] += 1
    elif corruption == "wrong-tag":
        release["tagName"] = "v0.5.0"
    elif corruption == "zero-byte":
        (assets / WHEEL).write_bytes(b"")
        wheel_record["size"] = 0
        wheel_record["digest"] = (
            "sha256:" + hashlib.sha256(b"").hexdigest()
        )
    release_json.write_text(json.dumps(release), encoding="utf-8")
    with pytest.raises(
        verify_github_release_assets.ReleaseAssetVerificationError
    ):
        verify_github_release_assets.verify_release_assets(
            release_json,
            assets,
            expected_tag="v0.5.3",
            wheel_name=WHEEL,
            sdist_name=SDIST,
        )


def test_runbook_describes_one_immutable_release_and_aborted_ext_marker():
    runbook = _text(ROOT / "RELEASING.md")
    assert "one distribution, one PyPI project, one immutable" in runbook
    assert "tag: `v0.5.3`" in runbook
    assert "environment, workflow, or GitHub Release for extensions" in runbook
    assert "`ext-v0.1.0` tag was an aborted publishing attempt" in runbook
    assert "Never rerun its historical" in runbook
    assert "Never move, delete, or reuse a release tag" in runbook
    assert "Never upload the same version" in runbook
    assert "exactly one `unified-cli` wheel and one sdist" in runbook
    assert "public-PyPI smoke passes" in runbook
    assert "new `unified-cli` version" in runbook
    assert "`pypi` environment" in runbook
    assert "pypi-ext" not in runbook
    assert "publish-ext.yml" not in runbook
