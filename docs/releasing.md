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

1. Bump `version` in `pyproject.toml`, then run `python tools/sync_version.py`
   to stamp it into all three manifests (`.claude-plugin/plugin.json`,
   `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`).
   `--check` is a CI gate, and a missing manifest is an error, not a skip.
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
`ci-green`, runs [`.github/scripts/ci_green_gate.sh`](../.github/scripts/ci_green_gate.sh),
which asks the **workflow-runs** API directly —
`repos/<repo>/actions/workflows/ci.yml/runs?head_sha=<sha>` — instead of
aggregating check runs on the commit. That distinction matters: the gate is
itself a check run on the tagged SHA, so "the commit has no check runs" can
never be true and a check-run count could not detect the failure mode this
gate exists for. Querying the CI workflow by `head_sha` separates the two
states cleanly:

| State on the tagged SHA | Gate behaviour |
| --- | --- |
| No CI run exists | grace-poll, then **fail**: "CI never ran this commit" |
| CI run still `queued`/`in_progress` | grace-poll, then **fail** on timeout |
| CI run concluded != `success` | **fail**, listing the non-green jobs |
| CI run concluded `success` | check the required-job whitelist |

The grace poll is every 30 s for up to 30 minutes, so a tag pushed *together
with* its commit is waited out rather than insta-failed (or waved through).

Run-level `success` is still not enough: the gate then asserts, by exact job
name, that **all four `windows-latest` matrix legs** (`test (windows-latest /
py3.11 … py3.14)`) are present and successful. Every historically red release
(0.4.0, 0.8.0, 0.9.0, 0.10.0) failed on `windows-latest` and nowhere else, so a
silently shrunk or renamed matrix must fail the gate rather than quietly
reduce it. The whitelist lives in `REQUIRED_JOBS_DEFAULT` in the gate script
and is kept in lock-step with `ci.yml`'s matrix by
`tests/test_release_gate.py`, which also replays the gate's decision table
above against a mocked API — **change the matrix and the gate in the same
commit**.

Escape hatch, for when a release must go out despite the gate (e.g. an
unrelated flaky infra check): run the `Release` workflow from the Actions tab
via **workflow_dispatch on the tag ref** with the `force` input set to `true`.
That logs a warning and skips the gate; everything else (tag/pyproject match,
manifest sync, build, notes extraction) still runs. Pushing a tag can never
bypass the gate — `force` only exists on manual dispatch.

## Release notes come from CHANGELOG.md (strict)

The workflow renders the release body with
`python tools/changelog_extract.py --strict <version>`. `--strict` means there
is **no fallback**: if `CHANGELOG.md` has no non-empty `## [X.Y.Z]` section for
the version being released, the job fails instead of quietly publishing the
`[Unreleased]` section (i.e. the *next* version's notes) as this release's
body. `tests/test_docs_conformance.py` fails on `main` for the same condition,
so the mistake surfaces at step 3, not after the Release is public.

## Docs conformance gate (contributor note)

`tests/test_docs_conformance.py` runs in CI and guards the docs against drift, so
keep it in mind when editing any `README*.md`, `INSTALL.md`, `ROADMAP.md`,
`docs/*.md`, `skills/*/SKILL.md`, or `commands/*.md`:

- **Every ` ```-fenced `akcli …` line is validated** against a live
  `build_parser()` — an unknown subcommand or an unknown `--flag` fails the test.
  Write real, current invocations (`<placeholders>` and `$VARS` are substituted
  automatically). If a fenced line legitimately is **not** a runnable command (a
  usage synopsis, an entry-point string), append a ` # doc-noqa` comment to that
  line to opt it out.
- **The `N ops` / `N macros` / `N calculators` / `N skills` / `N commands`
  counts** (and their zh-Hant/zh-Hans/ja forms) are asserted equal to the live
  registries — the op/macro/calc tables, and the `skills/*/SKILL.md` and
  `commands/*.md` file counts. When you add an op, macro, calculator, skill or
  plugin command, update every count in the docs in the same change or the
  gate fails.

Run it locally with `python -m pytest tests/test_docs_conformance.py -q`.

A sibling gate (`tests/test_schema_exports.py`) requires the packaged mirror
`src/akcli/schemas/*.json` to be byte-identical to the canonical `schemas/*.json`
— and to contain nothing else: `ops._schema_text()` reads the mirror first, so a
mirror file whose canonical original was deleted or renamed would keep being
served from the wheel. After editing, adding **or removing** a schema, run
`python tools/sync_schemas.py` (or `python tools/sync_schemas.py --check` to see
the drift, including orphaned mirrors, without writing).

## PyPI publishing

The package is published to PyPI under the distribution name `akcli-kicad`
(so `pip install akcli-kicad` is the correct invocation — the import package and the
`akcli` CLI command itself are unaffected).

PyPI publishing is opt-in and uses
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no PyPI
API token is stored in this repo. To enable it, one time:

1. On PyPI, add a trusted publisher for this project: owner `tipoLi5890`,
   repo `akcli`, workflow file `release.yml`, and environment `pypi`.
2. In GitHub repo Settings > Environments, create an environment named `pypi` —
   this is required because PyPI trusted publishing binds its OIDC token to
   that environment name. **Recommended:** on that environment, add yourself
   under **Required reviewers**. The `publish-pypi` job holds
   `id-token: write`; a required reviewer makes every PyPI upload pause for a
   human click, which is the only stage of the pipeline that is irreversible
   (a PyPI version number can never be reused).
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
configured. A skipped job is indistinguishable from a job that was never meant
to run, so the `release` job emits a `::warning::` on every run where
`PYPI_TRUSTED_PUBLISHING` is not `true`: if you expected a PyPI upload and the
run summary carries that warning, the repository variable is the thing to
check.

`pypa/gh-action-pypi-publish` is pinned to a **commit SHA**, not the mutable
`release/v1` branch ref — that job holds `id-token: write`, so a retargeted
branch would hand out the trusted-publishing identity itself. The pin is
updated by hand: rewrite the SHA and its trailing version comment together.
