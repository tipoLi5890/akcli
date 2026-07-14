# Contributing to akcli

Thanks for your interest in improving `akcli`. This guide covers the dev
setup, the project's hard invariants, and how to get a change merged.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Project invariants (please don't break these)

- **Zero runtime dependencies.** The package is **standard-library only** (including
  `tomllib`). Test/build tooling lives in the optional `dev` extra; never add a runtime
  dependency. If you think you need one, open an issue first.
- **Python ≥ 3.11.**
- **Altium is read-only offline.** The tool never writes Altium files without the optional
  Windows live driver. KiCad writes go through the atomic snapshot → temp → verify → replace
  pipeline with the pure-Python connectivity gate.
- **Untrusted input is bounded.** Parsers must stay within the caps in `safety.py` (depth,
  allocation, sector/atom/node limits) and fail with a structured `errors.py` code — never
  hang, crash the interpreter, or read outside the workspace. See [SECURITY.md](SECURITY.md).
- **Version contracts.** Package version (SemVer, source of truth = `pyproject.toml`),
  `schema_version`, and `protocol_version` each have rules — see the *Versioning policy* in
  [CHANGELOG.md](CHANGELOG.md). Don't bump them casually.

## Dev setup

```bash
git clone https://github.com/tipoLi5890/akcli
cd akcli
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

More options (run-from-clone, agents) are in [INSTALL.md](INSTALL.md).

## Before opening a PR

Run the same checks CI runs:

```bash
pytest                                   # full suite (stdlib + dev extra)
python -m build && twine check dist/*    # packaging
claude plugin validate . --strict        # Claude Code plugin / marketplace
python tools/sync_version.py --check      # version-sync drift (manifests vs pyproject)
```

- Add or update tests for any behavior change — fixtures are **synthetic** (no proprietary
  or third-party board data; generators live under `tests/fixtures/_gen/`).
- Keep changes focused; match the surrounding code style.
- New CLI subcommands/flags must be reflected in `docs/cli-reference.md`; new ops in the
  `schemas/` files.

## Pull request process

1. Fork and branch off `main`.
2. Make the change with tests; ensure all the checks above pass locally.
3. Open the PR with a clear description of *what* and *why*. CI (the full OS × Python matrix,
   packaging, and plugin validation) must be green.
4. Be responsive to review. Maintainers may request changes to uphold the invariants above.

## Reporting bugs / requesting features

Use the [issue templates](.github/ISSUE_TEMPLATE). For **security** issues, follow
[SECURITY.md](SECURITY.md) (private reporting) — do not file a public issue.
