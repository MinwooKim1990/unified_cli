# Releasing `unified-cli`

This is the release runbook for publishing `unified-cli` to PyPI
(GitHub: <https://github.com/MinwooKim1990/unified_cli>, default branch `main`).

Production releases have one path: push an exact `vX.Y.Z` tag for a commit
already on `main`, then let `publish.yml` publish via PyPI Trusted Publishing
(OIDC). Do not upload that production version locally with `twine`: a later
tag would correctly fail as a duplicate upload rather than silently hiding the
mistake.

---

## Version source

The release version is single-sourced in
`src/unified_cli/__init__.py`:

```python
__version__ = "X.Y.Z"
```

`pyproject.toml` reads it dynamically, so this is the **only** place to bump:

```toml
[project]
dynamic = ["version"]

[tool.setuptools.dynamic]
version = { attr = "unified_cli.__version__" }
```

> Bump `__version__` (following semver) for every release. Never hard-code a
> `version = "..."` back into `pyproject.toml` — that would reintroduce drift.

Keep a `CHANGELOG.md` entry for every release (create it on the first release
if it does not yet exist).

---

## Pre-release checklist

- [ ] Working tree is clean and `main` is up to date.
- [ ] CI is green on the commit you intend to release.
- [ ] The required GitHub release protections below are configured and active.
- [ ] Version bumped (see above) following semver.
- [ ] `CHANGELOG.md` updated with the new version and notable changes.
- [ ] `pytest` passes locally (offline/unit suite — CI cannot run live
      provider calls because `claude` / `codex` / `agy` and their auth are
      not available in CI).
- [ ] `python -m build` and `twine check dist/*` pass.
- [ ] A clean virtual environment can install the built wheel and run
      `unified-cli --version`.

---

## Optional local package preflight (no production upload)

Run this from the repo root inside your dev venv (`pip install -e
".[dev,server]"` plus `pip install build twine`) before creating the release
tag. It validates the exact package shape locally; it never uploads to the
real PyPI index.

### 1. Clean previous build artifacts

```bash
rm -rf dist/ build/ src/*.egg-info
```

### 2. Build sdist + wheel

```bash
python -m build
```

This produces `dist/unified_cli-X.Y.Z.tar.gz` and
`dist/unified_cli-X.Y.Z-py3-none-any.whl`.

### 3. Validate the metadata

```bash
twine check dist/*
```

### 4. Install the built wheel in a clean environment

```bash
python -m venv /tmp/uc-wheel-smoke
/tmp/uc-wheel-smoke/bin/python -m pip install --upgrade pip
/tmp/uc-wheel-smoke/bin/python -m pip install dist/*.whl
/tmp/uc-wheel-smoke/bin/unified-cli --version
```

If you also use TestPyPI, publish and install a distinct prerelease version
(for example `X.Y.Zrc1`) there; pin the install explicitly to that prerelease.
Do not use TestPyPI or a local `twine upload` as a second production-release
path for `X.Y.Z`.

---

## Production release (tag → Trusted Publishing)

Once Trusted Publishing is configured (one-time setup below), releasing is:

1. Bump the version + update `CHANGELOG.md`, commit, and push to `main`.
2. Wait for the `main` CI run to pass on that exact commit.
3. Create and push the matching immutable tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. The tag push runs `.github/workflows/publish.yml` exactly once:
   - the **verify-release** job fetches complete history, verifies that the
     pushed tag resolves to the checked-out event commit, and refuses it unless
     that commit is already an ancestor of `origin/main`;
   - the **test** job runs the offline suite;
   - the **build** job refuses a tag whose `vX.Y.Z` does not exactly equal the
     package version, builds the sdist + wheel, checks their metadata and
     `twine` validation, and installs the wheel in a clean virtual environment
     for an entry-point smoke test;
   - the **publish** job (with `permissions: id-token: write` and
     `environment: pypi`) downloads that verified artifact and uploads it with
     `pypa/gh-action-pypi-publish@release/v1` — authenticating purely via OIDC,
     with no saved PyPI token.

The workflow deliberately does not use `skip-existing`: a duplicate upload is
a release error to investigate, not a condition to hide.

### Required GitHub release protections

Before the first production release, configure both controls in GitHub:

- A `v*` tag ruleset must restrict creation, update, and deletion to the
  release maintainer. Do not allow a release tag to be moved after approval.
- The `pypi` Environment must allow deployments only from a custom **tag**
  policy matching `v*`; it must not accept arbitrary branches.

These controls are required, not optional: the workflow validates the tagged
commit's provenance, while the ruleset and protected environment authorize who
may initiate a production upload.

If an independent trusted reviewer or team is available, also configure it as
the `pypi` Environment's required reviewer, prevent self-review, and disable
administrator bypass. This project currently has a single release maintainer,
so that two-person approval cannot be configured honestly; the tag ruleset and
tag-only environment policy are the enforced baseline. They do not protect
against compromise of that maintainer's GitHub account.

### One-time PyPI Trusted Publishing setup

In the PyPI web UI, add a GitHub Actions trusted publisher for the project:

- **PyPI → your project → Manage → Publishing → Add a new publisher**
  (or, before the project exists, **Your account → Publishing → Add a
  pending publisher**).
- Fill in:
  - **Owner**: `MinwooKim1990`
  - **Repository name**: `unified_cli`
  - **Workflow name**: `publish.yml`
  - **Environment name**: `pypi`

These values must match `publish.yml` exactly (workflow filename and the
`environment: pypi` on the `publish` job), or PyPI will reject the OIDC token.

### Chicken-and-egg: first release vs. Trusted Publishing

Trusted Publishing can authorize an upload before the project exists on PyPI.
Add a *pending publisher* on PyPI's account-level Publishing page with the
four values above before the project exists. The first automated publish then
creates the project and promotes the pending publisher to a normal one — no
long-lived upload token or manual production upload is needed.

---

## Post-release

- [ ] Confirm the new version appears on <https://pypi.org/project/unified-cli/>.
- [ ] `pip install unified-cli` in a clean venv and smoke-test `unified-cli --version`.
- [ ] Create GitHub Release notes from the already-published, verified tag if desired.
- [ ] Open a follow-up commit to start the next dev cycle if you use a
      `.devN` / post-release versioning scheme.
