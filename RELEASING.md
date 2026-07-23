# Releasing unified-cli

The planned 0.5.3 release has one distribution, one PyPI project, one immutable
tag, and one GitHub Release:

- distribution and PyPI project: `unified-cli`;
- tag: `v0.5.3`;
- GitHub Release: `v0.5.3`, with the verified wheel and sdist attached.

The single wheel contains both public Python namespaces, `unified_cli` and
`unified_cli_ext`. Core and extensions are a feature boundary, not separate
release units. Do not create a second package, tag, trusted publisher,
environment, workflow, or GitHub Release for extensions.

The historical `ext-v0.1.0` tag was an aborted publishing attempt. No extension
PyPI project or GitHub Release was published. Never rerun its historical
workflow.

Never move, delete, or reuse a release tag. Never upload the same version
twice, use `skip-existing`, or use a local production `twine upload`. A
duplicate upload is a release failure, not a condition to hide.

## Version and release notes

Set the one project version to `0.5.3` in the authoritative Core version source
used by `pyproject.toml`. Update the root changelog with the 0.5.3 release note
before tagging. The extension-source changelog may describe the bundled feature
changes, but it is not an independently released version.

The release notes must state that the wheel provides both namespaces; Core
defaults remain Claude, Codex, and Gemini only; and 18 executable Preview
adapters are explicit and lazy. The 2026-07-23 accountless lab reached
`create()` for 13 current official installations; Cursor, Hermes, Mistral Vibe,
and Qoder had bounded compatibility blockers, and Poolside was not installed
because EULA acceptance was outside the test authorization. Grok has
representative live-test evidence, while common transports are fixture-tested
without a vendor or account compatibility guarantee; all Ext server policies
remain disabled.
Do not claim authentication or login E2E coverage beyond recorded evidence.

## One-time trusted-publisher and GitHub setup

Configure exactly one PyPI trusted publisher and one GitHub environment for the
repository's release workflow:

| Package | Owner | Repository | Tag | GitHub environment |
| --- | --- | --- | --- | --- |
| `unified-cli` | `MinwooKim1990` | `unified_cli` | `v0.5.3` | `pypi` |

In PyPI, use **Manage → Publishing → Add a new publisher** for `unified-cli`.
Before the project exists, create a pending publisher from the account
Publishing page; the first trusted publish creates the project without a
long-lived token.

Protect `v*` tags so only the release maintainer can create them and tag updates
or deletion remain forbidden. Limit the `pypi` environment to `v*` tags. Grant
`id-token: write` only to the PyPI publish job and `contents: write` only to the
final GitHub Release job. Do not store a PyPI token in GitHub.

## Prepare one exact release commit

1. Update the authoritative version to `0.5.3` and update the root changelog.
2. From a clean `main`, run the complete required offline test suite,
   distribution build, metadata checks, clean-install checks, and all required
   readiness gates for the unified wheel.
3. Verify that the built wheel includes both `unified_cli` and
   `unified_cli_ext`, declares the `acp` and `mcp` extras on `unified-cli`, and
   has no separate-distribution dependency or artifact.
4. Push the candidate to `main` and wait for all required CI checks on that
   exact commit to pass.
5. Record the immutable candidate:

   ```bash
   git switch main
   git pull --ff-only origin main
   test -z "$(git status --porcelain=v1 --untracked-files=all)"
   MAIN_SHA="$(git rev-parse HEAD)"
   test "$MAIN_SHA" = "$(git rev-parse origin/main)"
   ```

Do not merge or push another `main` commit between recording `MAIN_SHA` and
tagging it.

## Publish 0.5.3

Create and push the one release tag:

```bash
git tag v0.5.3 "$MAIN_SHA"
git push origin refs/tags/v0.5.3
```

The release workflow must:

1. prove that `v0.5.3`, the event SHA, checkout, and current `origin/main` are
   the same clean commit, and that the source version is `0.5.3`;
2. run the required offline tests and readiness gates before building;
3. build exactly one `unified-cli` wheel and one sdist, verify their metadata,
   archive roots, RECORD integrity, hashes, package hierarchy, default runtime
   dependencies, and optional-extra markers, then clean-install the wheel;
4. publish only those verified artifacts through the `pypi` environment;
5. install `unified-cli==0.5.3` from the explicit public
   `https://pypi.org/simple` index with cache, extra indexes, local links, and
   `no-index` configuration disabled, then verify both public namespaces, the
   entry point, version, and dependency health; and
6. only after that public-PyPI smoke passes, create the final GitHub Release for
   `v0.5.3` with the exact verified wheel and sdist attached. A safe rerun
   verifies an existing final release and downloaded asset bytes rather than
   replacing them.

Confirm the two outcomes:

- <https://pypi.org/project/unified-cli/0.5.3/>
- <https://github.com/MinwooKim1990/unified_cli/releases/tag/v0.5.3>

## Failure and rollback rules

PyPI releases are immutable. A repair always uses a new `unified-cli` version
and a new `v*` tag; never move the failed tag or attempt another upload of the
same version. If public-PyPI smoke fails, stop, fix on `main`, and release a new
version. A yank limits new resolution but is not deletion.

If publishing succeeded but GitHub Release creation failed, do not rerun or
bypass the upload. Re-verify the public package and immutable tag, then create
the missing GitHub Release manually with the exact verified wheel and sdist. If
a release already exists, its state and attached assets must match; do not
overwrite or silently add mismatched files.
