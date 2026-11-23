# GitHub Actions: gate schematic pull requests

A workflow for a KiCad repository. On every pull request it:

- uploads `akcli check` findings as SARIF, shown as code-scanning alerts on the pull request;
- fails when `akcli check` reports a finding at or above the `--fail-on` threshold (default
  `warning`);
- fails when the pull request adds design-review findings the base revision does not have
  (`akcli review diff --fail-on-new`).

Copy it to `.github/workflows/schematic-gate.yml`, set `SCH` to the root schematic
(repository-relative) and `AKCLI_VERSION` to an `akcli-kicad` release.

## Workflow

```yaml
name: schematic-gate

on:
  pull_request:

permissions:
  contents: read
  security-events: write   # upload-sarif
  actions: read            # upload-sarif, private repositories only

env:
  SCH: hardware/board.kicad_sch
  AKCLI_VERSION: "X.Y.Z"

jobs:
  akcli:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          path: base
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install "akcli-kicad==$AKCLI_VERSION"
      - run: akcli --version

      - name: Check report (SARIF)
        run: akcli check "$SCH" --format sarif --fail-on never > akcli.sarif
      - uses: github/codeql-action/upload-sarif@v4
        if: github.event.pull_request.head.repo.full_name == github.repository
        with:
          sarif_file: akcli.sarif
          category: akcli-check

      - name: Check gate
        run: akcli check "$SCH"

      - name: Review drift against the base revision
        if: ${{ !cancelled() }}
        run: |
          akcli review analyze "$SCH" --out head.findings.json
          if [ -f "base/$SCH" ]; then
            akcli review analyze "base/$SCH" --out base.findings.json
            akcli review diff base.findings.json head.findings.json --fail-on-new
          fi
```

## Commands

The `akcli` lines of the workflow and of the optional steps below. They run locally from the
repository root with `SCH` set; the base revision is a second checkout under `base/`, and
`RUNNER_TEMP` is any directory outside the worktree.

```bash
akcli --version
akcli check "$SCH" --format sarif --fail-on never > akcli.sarif
akcli check "$SCH"
akcli review analyze "$SCH" --out head.findings.json
akcli review analyze "base/$SCH" --out base.findings.json
akcli review diff base.findings.json head.findings.json --fail-on-new
akcli nets "$SCH" --intent-snapshot hardware/intent.json
akcli check "$SCH" --intent hardware/intent.json
akcli gate "$SCH" --pcb hardware/board.kicad_pcb --intent hardware/intent.json
akcli release preflight --sch "$SCH" --pcb hardware/board.kicad_pcb --out "$RUNNER_TEMP/release.manifest.json"
```

## Steps

**Check report.** `check` selects its output with `--format`; `sarif` writes SARIF 2.1.0 to
stdout, so the step redirects it to a file. `--fail-on never` keeps the step at exit `0`, so the
upload runs whether or not there are findings; the gate is the next step. The SARIF artifact URI
is the schematic path as given on the command line: run from the repository root with a
repository-relative path. Alert fingerprints exclude positions, so moving a part does not open a
new alert. The upload is skipped for pull requests from forks, where the workflow token is
read-only. Without code scanning, drop the upload step; `--format junit` writes a JUnit XML
report instead.

**Check gate.** The default check families at the default threshold: exit `1` when any finding is
`warning` or higher. `--fail-on error` raises the threshold. A finding accepted by design belongs
in `akcli.toml` as a `[[waiver]]` with a `reason`; waivers apply to the report and to the gate
alike. See [`akcli check`](../cli-reference.md#akcli-check-file).

**Review drift.** `review diff` compares two findings files, not two schematics. Each side is
written by `review analyze --out`. The second checkout step places the whole base revision under
`base/`, so its child sheets, `akcli.toml` and `datasheets/` facts store are read as they were.
Findings are aligned by fingerprint, independent of the checkout path. `review analyze` itself is
advisory (exit `0`); only `review diff --fail-on-new` gates. The step runs even when the check
gate failed (`if: ${{ !cancelled() }}`), so one run reports both verdicts. When the schematic does
not exist on the base revision, the comparison is skipped. Pass the same `--profile` to both
sides. See the `akcli review` section of [cli-reference.md](../cli-reference.md).

## Optional steps

### Intent snapshot

Snapshot the netlist once and commit the file. Re-snapshot in a pull request that changes
connectivity on purpose, so the change to `intent.json` is part of the review.

```bash
akcli nets "$SCH" --intent-snapshot hardware/intent.json
```

```yaml
      - name: Intent assertion
        run: akcli check "$SCH" --intent hardware/intent.json
```

`--intent` selects the intent family alone: keep the check gate step. Nets are matched by pin
membership, so a rename does not hide a change.

### `akcli gate`

One command for the `check`, `intent`, `sch-pcb` and `bom-lock` gates with a single exit code. It
replaces the check gate and intent steps. A gate without its input is skipped with a reason. The
`bom-lock` gate reads the HTTP cache only unless `--live` is passed.

```yaml
      - name: Gate
        run: akcli gate "$SCH" --pcb hardware/board.kicad_pcb --intent hardware/intent.json
```

### `release preflight`

Runs every applicable release gate and writes a traceable manifest. Run it as a job of its own:
the `git` gate fails on a dirty worktree, and the `base/` checkout and report files of the job
above are untracked files. The manifest is written outside the worktree for the same reason.
`--drc` and `--sim` need KiCad on the runner; a requested gate that cannot run fails.

```yaml
  preflight:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install "akcli-kicad==$AKCLI_VERSION"
      - run: akcli release preflight --sch "$SCH" --pcb hardware/board.kicad_pcb --out "$RUNNER_TEMP/release.manifest.json"
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: release-manifest
          path: ${{ runner.temp }}/release.manifest.json
```

## Exit codes in CI

| Exit | Meaning in this workflow |
|---|---|
| `0` | The step passed. |
| `1` | A gate fired: a `check` or `gate` finding at or above `--fail-on`, new review findings (`review diff --fail-on-new`), or a failed `release preflight` gate. |
| `2` | Usage or config error: an unknown flag, a malformed `akcli.toml` or intent file. |
| `3` | The schematic does not parse. |
| `4` | A path does not exist or cannot be read. |
| `6` | An intent or contract file declares a `protocol_version` the installed akcli does not accept (`PROTOCOL_MISMATCH`). |

Exit `1` is a design verdict. Any other non-zero code means the workflow, its inputs or the runner
need fixing. Full table: [Exit codes](../cli-reference.md#exit-codes).

## Pinning the akcli version

- Install an exact release: `pip install "akcli-kicad==X.Y.Z"`. A minor release can add checks
  and review rules, which changes the verdict on an unchanged schematic. Raise the pin in a pull
  request of its own.
- `akcli --version` prints the package version and the op-list `protocol_version`; the workflow
  logs it.
- Both revisions are analyzed by the same install in one job, so a rule added by an upgrade fires
  on both sides and is not counted as new.
- akcli needs Python 3.11 or newer and has no runtime dependencies.
- What a release may change is defined in [stability.md](../stability.md).
- Action versions are pinned by major tag. Pin by commit SHA where your policy requires it.
