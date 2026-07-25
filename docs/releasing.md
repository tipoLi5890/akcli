# Releasing

`akcli` releases are tag-driven: pushing a `vX.Y.Z` tag runs
`.github/workflows/release.yml`, which builds the sdist + wheel, verifies the
tag matches `pyproject.toml`, extracts that version's `CHANGELOG.md` section as
the release notes, and creates a GitHub Release with the build artifacts
attached. A separate `publish-pypi` job then publishes to PyPI via
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no API
token) — it only runs if the repo has opted in (see "PyPI publishing" below);
otherwise it is skipped and the release still succeeds.

## Runbook

1. Bump `version` in `pyproject.toml` (run `python tools/sync_version.py` if
   the plugin manifests carry a version too).
2. Move `CHANGELOG.md`'s `## [Unreleased]` section to `## [X.Y.Z]` (that
   section becomes the GitHub Release body verbatim).
3. `git add pyproject.toml CHANGELOG.md ...` and commit.
4. `git push origin main` — and **wait for the CI workflow to go green on
   that exact commit before tagging**. Release commits are big squashed
   batches developed on macOS/Linux; historically every Windows-only failure
   (locale-encoding text I/O, `\n` -> `\r\n` translation, POSIX shlex
   splitting, subprocess env/exe semantics) surfaced HERE, on
   `windows-latest`, after the tag was already public (0.4.0, 0.8.0, 0.9.0,
   0.10.0). Tagging first inverts the gate — the GitHub Release ships before
   the matrix has ever run the commit.
5. `git tag vX.Y.Z && git push origin vX.Y.Z` — only once CI is green.
6. Watch the `Release` workflow run in the Actions tab; verify the GitHub
   Release was created with `dist/*.whl` and `dist/*.tar.gz` attached, and (if
   PyPI publishing is enabled) that the new version appears on PyPI.

The tag (minus the leading `v`) **must** equal the `pyproject.toml` version, or
the workflow fails fast before building anything.

## The CI-green gate (step 4, enforced)

Step 4 is no longer a human promise. The release workflow's first job,
`ci-green`, queries the GitHub API for every check run on the tagged commit
(`repos/<repo>/commits/<sha>/check-runs`) and refuses to build or publish
unless all of them concluded `success` (`skipped`/`neutral` are tolerated).
It waits up to 30 minutes for still-running checks, and treats **no check runs
at all** as a failure — that is the exact shape of "tagged a commit CI never
saw".

Escape hatch, for when a release must go out despite the gate (e.g. an
unrelated flaky infra check): run the `Release` workflow from the Actions tab
via **workflow_dispatch on the tag ref** with the `force` input set to `true`.
That logs a warning and skips the gate; everything else (tag/pyproject match,
manifest sync, build, notes extraction) still runs. Pushing a tag can never
bypass the gate — `force` only exists on manual dispatch.

## Docs conformance gate (contributor note)

`tests/test_docs_conformance.py` runs in CI and guards the docs against drift, so
keep it in mind when editing any `README*.md`, `docs/*.md`, or `skills/*/SKILL.md`:

- **Every ` ```-fenced `akcli …` line is validated** against a live
  `build_parser()` — an unknown subcommand or an unknown `--flag` fails the test.
  Write real, current invocations (`<placeholders>` and `$VARS` are substituted
  automatically). If a fenced line legitimately is **not** a runnable command (a
  usage synopsis, an entry-point string), append a ` # doc-noqa` comment to that
  line to opt it out.
- **The `N ops` / `N macros` / `N calculators` counts** (and their zh-Hans/zh-Hant
  forms) are asserted equal to the live registries. When you add an op, macro, or
  calculator, update every count in the docs in the same change or the gate fails.

Run it locally with `python -m pytest tests/test_docs_conformance.py -q`.

A sibling gate (`tests/test_schema_exports.py`) requires the packaged mirror
`src/akcli/schemas/*.json` to be byte-identical to the canonical `schemas/*.json`.
After editing a schema, run `python tools/sync_schemas.py` (or
`python tools/sync_schemas.py --check` to see the drift without writing).

## PyPI publishing

The package is published to PyPI under the distribution name `akcli-kicad`
(so `pip install akcli-kicad` is the correct invocation — the import package and the
`akcli` CLI command itself are unaffected).

PyPI publishing is opt-in and uses
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no PyPI
API token is stored in this repo. To enable it, one time:

1. On PyPI, add a trusted publisher for this project: owner `tipoLi5890`,
   repo `akcli`, workflow file `release.yml`, and environment `pypi`.
2. In GitHub repo Settings > Environments, create an environment named `pypi`
   (optionally with required reviewers for extra safety) — this is required
   because PyPI trusted publishing binds its OIDC token to that environment
   name.
3. In GitHub repo Settings > Secrets and variables > Actions > **Variables**
   (the repository-level tab, not the `pypi` environment's own variables tab),
   add a repository variable `PYPI_TRUSTED_PUBLISHING` set to `true`.
   **This must be a repository variable, not an environment variable on
   `pypi`.** The `publish-pypi` job's `if:` condition is evaluated before the
   job's environment is resolved, so an environment-scoped variable is
   invisible at that point and the job would be silently skipped even though
   everything else is configured correctly.

Until that's done, the `publish-pypi` job's `if:` condition evaluates false
and the job is skipped — the release itself never fails because PyPI isn't
configured.
