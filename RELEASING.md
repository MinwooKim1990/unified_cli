# Releasing Core and Ext

This runbook is the only production release path for:

- Core: `unified-cli` 0.5.0, immutable tag `v0.5.0`, workflow `publish.yml`;
- Ext: `unified-cli-ext` 0.1.0, immutable tag `ext-v0.1.0`, workflow
  `publish-ext.yml`.

Both tags must identify the same exact clean commit at the tip of `main`. Core
is published and smoke-tested first. Ext is tagged only after the Core workflow
has installed Core 0.5.0 from public PyPI and created the mandatory Core GitHub
Release. The Ext workflow then tests against that released Core, publishes Ext,
smoke-tests both public packages, and creates the mandatory Ext GitHub Release.
Each GitHub Release carries the exact wheel and sdist already verified by its
build job; a release record without those two matching assets is incomplete.

Never move, delete, or reuse a release tag. Never upload the same version twice,
use `skip-existing`, or use a local production `twine upload`. A duplicate upload
is a release failure, not a condition to hide.

## Sources of version truth

Core is single-sourced in `src/unified_cli/__init__.py`:

```python
__version__ = "0.5.0"
```

`pyproject.toml` reads that attribute dynamically. Ext is single-sourced in
`packages/unified-cli-ext/pyproject.toml`:

```toml
version = "0.1.0"
dependencies = ["unified-cli>=0.5,<0.6"]
```

`packages/unified-cli-ext/src/unified_cli_ext/__init__.py` must expose the same
Ext version. Both changelogs must contain the versions being released.

## One-time trusted-publisher and GitHub setup

Create separate PyPI projects and GitHub Actions trusted publishers. The values
are exact and current:

| Package | Owner | Repository | Workflow | GitHub environment |
| --- | --- | --- | --- | --- |
| `unified-cli` | `MinwooKim1990` | `unified_cli` | `publish.yml` | `pypi` |
| `unified-cli-ext` | `MinwooKim1990` | `unified_cli` | `publish-ext.yml` | `pypi-ext` |

In each PyPI project, use **Manage → Publishing → Add a new publisher**. Before
a project exists, create a pending publisher from the account Publishing page;
the first trusted publish creates the project without a long-lived token.

Configure these GitHub controls before creating either tag:

- a tag ruleset restricts creation, update, and deletion of `v*` and `ext-v*`
  tags to the release maintainer; updates and force movement remain forbidden;
- environment `pypi` accepts only `v*` tags;
- environment `pypi-ext` accepts only `ext-v*` tags;
- required reviewers, prevention of self-review, and disabled administrator
  bypass are enabled whenever an independent trusted reviewer is available.

The workflows grant `id-token: write` only to their PyPI publish jobs and
`contents: write` only to their final GitHub Release jobs. No PyPI token is
stored in GitHub.

## Prepare one exact release commit

1. Update the Core and Ext version sources and both changelogs.
2. From a clean `main`, run the complete offline Core and Ext suites, the
   performance gate, distribution builds, metadata checks, and clean installs.
3. Push the release commit to `main` and wait for every required CI check on
   that exact commit to pass.
4. Record the immutable candidate:

   ```bash
   git switch main
   git pull --ff-only origin main
   test -z "$(git status --porcelain=v1 --untracked-files=all)"
   MAIN_SHA="$(git rev-parse HEAD)"
   test "$MAIN_SHA" = "$(git rev-parse origin/main)"
   ```

Do not merge or push another `main` commit between the two tag pushes. Both
release workflows reject an older ancestor of `main`; equality with the current
`origin/main` commit is required.

## Release 1 of 2: Core 0.5.0

Create Core's tag at the recorded commit and push only that tag:

```bash
git tag v0.5.0 "$MAIN_SHA"
git push origin refs/tags/v0.5.0
```

`publish.yml` must complete in this order:

1. prove `v0.5.0`, the event SHA, the checkout, and current `origin/main` are
   the same commit; prove the checkout is clean and the source version is 0.5.0;
2. run the complete offline Core suite and performance/readiness gate;
3. build exactly one Core wheel and one Core sdist, verify both metadata
   identities, filenames, archive roots, wheel RECORD SHA-256 hashes/sizes,
   member file/directory hierarchy, the exact `rich>=13` and
   `prompt-toolkit>=3.0.43` default-runtime dependency set, optional-extra
   markers, and package boundaries, reject every Core dependency on Ext, and
   clean-install the wheel;
4. publish only the verified Core artifact through environment `pypi`;
5. install `unified-cli==0.5.0` from the explicit public
   `https://pypi.org/simple` index with cache, extra indexes, local links, and
   `no-index` configuration disabled, then verify its import, entry point,
   version, and dependency health;
6. only after that PyPI smoke passes, download the verified build artifact and
   create the mandatory GitHub Release for `v0.5.0` with that exact wheel and
   sdist attached. A safe rerun verifies an existing final release, asset names,
   sizes, digests when available, and downloaded bytes instead of uploading a
   replacement.

Confirm both outcomes before proceeding:

- <https://pypi.org/project/unified-cli/0.5.0/>
- <https://github.com/MinwooKim1990/unified_cli/releases/tag/v0.5.0>

If Core did not complete all six steps, do not create the Ext tag.

## Release 2 of 2: Ext 0.1.0

First prove that `main` has not moved and both tags will share the Core release
commit:

```bash
git fetch --no-tags origin main
test "$MAIN_SHA" = "$(git rev-parse origin/main)"
test "$MAIN_SHA" = "$(git rev-parse 'v0.5.0^{commit}')"
git tag ext-v0.1.0 "$MAIN_SHA"
git push origin refs/tags/ext-v0.1.0
```

`publish-ext.yml` must complete in this order:

1. prove `ext-v0.1.0`, `v0.5.0`, the event SHA, the checkout, and current
   `origin/main` are the same clean commit; prove the Ext name, version, and
   exact Core dependency line; require the `v0.5.0` GitHub Release to exist as
   a final, non-draft, non-prerelease release with exactly both non-empty Core
   artifacts, require their recorded sizes and SHA-256 digests, download them
   into a new empty directory, compare the downloaded bytes to that metadata,
   and rerun the complete Core wheel/sdist verifier;
2. install Core 0.5.0 from public PyPI and run the complete offline Ext suite;
3. build exactly one Ext wheel and one Ext sdist, verify both metadata
   identities, filenames, archive roots, wheel RECORD SHA-256 hashes/sizes,
   member file/directory hierarchy, exactly one default-runtime dependency
   (`unified-cli>=0.5,<0.6`), optional-extra markers, reject Core package paths,
   and check the Ext wheel against the released Core wheel;
4. clean-install the built Ext wheel alongside released Core and assert both
   versions;
5. publish only the verified Ext artifact through environment `pypi-ext`;
6. install `unified-cli==0.5.0` and `unified-cli-ext==0.1.0` together from the
   explicit public PyPI index with cache, extra indexes, local links, and
   `no-index` configuration disabled, then verify both versions and dependency
   health;
7. only after that PyPI smoke passes, download the verified Ext build artifact
   and create the mandatory GitHub Release for `ext-v0.1.0` with the exact Ext
   wheel and sdist attached. Reruns verify an existing final release and both
   downloaded asset bytes rather than replacing them.

Confirm both outcomes:

- <https://pypi.org/project/unified-cli-ext/0.1.0/>
- <https://github.com/MinwooKim1990/unified_cli/releases/tag/ext-v0.1.0>

## Failure and rollback rules

PyPI releases are immutable. A repair always uses a new version and new tag;
never move the failed tag or attempt another upload of the same version.

- If Core is defective or its PyPI smoke fails before Ext is tagged, stop. Yank
  only `unified-cli` 0.5.0, annotate its GitHub Release if one exists, fix on
  `main`, and release a new Core version. Do not publish Ext against the yanked
  Core.
- If Ext is defective after Core is healthy, yank only `unified-cli-ext` 0.1.0
  and annotate only the Ext GitHub Release. Leave Core 0.5.0 and its GitHub
  Release intact; repair Ext under a new Ext version/tag.
- If the PyPI upload and public-PyPI smoke passed but the final GitHub Release
  API call failed, do not rerun or bypass the upload. Verify the public package
  and immutable tag again, then create the missing GitHub Release manually for
  that existing tag with the exact verified wheel and sdist. If a release
  already exists, its final/draft state and attached assets must match; do not
  overwrite or silently add to a mismatched release. Record the failed workflow
  and recovery in the release notes.
- A yank limits new resolution but is not deletion. Publish a corrected version
  promptly and explain the affected package/version without changing the other
  package's history.

The two GitHub Releases are mandatory release records, not optional notes.
