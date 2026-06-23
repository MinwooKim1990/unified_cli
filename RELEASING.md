# Releasing `unified-cli`

This is the release runbook for publishing `unified-cli` to PyPI
(GitHub: <https://github.com/MinwooKim1990/unified_cli>, default branch `main`).

There are two paths:

- **Manual path** — build locally and upload with `twine` (use this for the
  very first release, or any time you want full control).
- **Automated path** — cut a GitHub Release and let `publish.yml` upload to
  PyPI via Trusted Publishing (OIDC). This is the steady-state path.

---

## Version source

The release version is single-sourced in
`src/unified_cli/__init__.py`:

```python
__version__ = "0.1.0"
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

- [ ] Working tree is clean and on `main` (or the release branch) up to date.
- [ ] CI is green on the commit you intend to release.
- [ ] Version bumped (see above) following semver.
- [ ] `CHANGELOG.md` updated with the new version and notable changes.
- [ ] `pytest` passes locally (offline/unit suite — CI cannot run live
      provider calls because `claude` / `codex` / `agy` and their auth are
      not available in CI).

---

## Manual path (local build + twine)

Run from the repo root inside your dev venv
(`pip install -e ".[dev,server]"` plus `pip install build twine`).

### 1. Bump version + changelog

Edit the version source (see [Version source](#version-source)) and add a
`CHANGELOG.md` entry. Commit:

```bash
git add -A
git commit -m "Release vX.Y.Z"
```

### 2. Clean previous build artifacts

```bash
rm -rf dist/ build/ src/*.egg-info
```

### 3. Build sdist + wheel

```bash
python -m build
```

This produces `dist/unified_cli-X.Y.Z.tar.gz` and
`dist/unified_cli-X.Y.Z-py3-none-any.whl`.

### 4. Validate the metadata

```bash
twine check dist/*
```

### 5. Upload to TestPyPI first

```bash
twine upload --repository testpypi dist/*
```

Then verify the install in a throwaway venv. TestPyPI does not mirror PyPI
dependencies, so allow the real index as an extra source:

```bash
python -m venv /tmp/uc-test && source /tmp/uc-test/bin/activate
pip install \
  -i https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  unified-cli
unified-cli --help    # smoke test the entry point
deactivate
```

### 6. Upload to real PyPI

```bash
twine upload dist/*
```

### 7. Tag and push

```bash
git tag vX.Y.Z
git push origin main --tags
```

---

## Automated path (GitHub Release → Trusted Publishing)

Once Trusted Publishing is configured (one-time setup below), releasing is:

1. Bump the version + update `CHANGELOG.md`, commit, and push to `main`.
2. On GitHub, draft a **Release** with tag `vX.Y.Z` and publish it.
3. Publishing the release fires `release: published`, which runs
   `.github/workflows/publish.yml`:
   - the **build** job builds the sdist + wheel, runs `twine check`, and
     uploads them as the `dist` artifact;
   - the **publish** job (with `permissions: id-token: write` and
     `environment: pypi`) downloads that artifact and uploads it with
     `pypa/gh-action-pypi-publish@release/v1` — authenticating purely via
     OIDC, no token.

The workflow also triggers on pushing a `v*` tag, so `git push origin vX.Y.Z`
alone will publish even without drafting a Release.

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

> Optional but recommended: protect the `pypi` GitHub Environment
> (Settings → Environments → `pypi`) with required reviewers so a human
> approves each publish run.

### Chicken-and-egg: first release vs. Trusted Publishing

Trusted Publishing can authorize an upload before the project exists on PyPI,
but you must pick one of these bootstrap routes:

- **Pending publisher (recommended).** Add a *pending publisher* on PyPI
  (account-level Publishing page) with the four values above *before* the
  project exists. The first automated publish then creates the project and
  promotes the pending publisher to a normal one — no token ever needed.
- **Token-first.** Do the very first upload manually with a PyPI API token
  via the [Manual path](#manual-path-local-build--twine) (`twine upload`).
  That creates the project; then add the trusted publisher under the project
  and use the automated path for all subsequent releases.

---

## Post-release

- [ ] Confirm the new version appears on <https://pypi.org/project/unified-cli/>.
- [ ] `pip install unified-cli` in a clean venv and smoke-test `unified-cli --help`.
- [ ] Verify the GitHub Release notes and tag are correct.
- [ ] Open a follow-up commit to start the next dev cycle if you use a
      `.devN` / post-release versioning scheme.
