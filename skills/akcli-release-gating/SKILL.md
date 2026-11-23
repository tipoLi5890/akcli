---
name: akcli-release-gating
description: >-
  Gate a hardware release with `akcli release preflight` — every gate, a
  traceable manifest, and the governance for letting review findings block
  (calibrated allowlist policy). Use this skill whenever the task involves:
  releasing/ordering/taping out a board; running or interpreting release
  preflight; authoring a `--review-policy` allowlist; checking a fab (gerber)
  package before ordering; or deciding whether a finding may block CI.
  Triggers on: release, preflight, tape-out, order the board, 放行, 出貨,
  下單, fab package, gerber check, release manifest, review policy,
  blocking gate, allowlist.
---

# akcli-release-gating — one gate run, one traceable manifest

`release preflight` runs every applicable gate and writes a manifest binding
input hashes, tool versions, git state and each gate's findings. PASS means
every gate passed **or was skipped-with-reason** (a skipped gate is visible,
never silent — and never a blocker; only a `fail` exits non-zero), so read
the skip reasons before calling a PASS complete.

```bash
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile fab.toml --drc --gerbers fab/ --contract contract.toml --review-policy policy.toml --out release.manifest.json
```

Gates, in order: `check` (ERC/power/BOM/nets) → `intent` → `contract` →
`library-audit` → **`project`** (a `.kicad_pro` beside `--sch`; the same
`project check` findings, library auditing switched off there so
`library-audit` above isn't reported twice — no project file skips with a
reason) → **`library-baseline`** (library files vs. the committed baseline)
→ `sch-pcb` → **`pcb-library`** (board footprints vs. the project footprint
libraries) → `fab` (`--fab-profile`: a profile file, or the id of a
profile bundled with akcli — `akcli fab profiles` lists them) → **`drc`**
(`--drc`; KiCad's own DRC) → `order` → **`sim`** (`--sim` sim.json;
quantitative SPICE assertions) → **`review`** (policy-gated) → **`gerber`**
(fab package) → `git` (clean worktree; `--allow-dirty` records instead of
failing).

- **The sim gate is opt-in and strict**: give `--sim <sim.json>` and the
  assertion violations become gate findings in the manifest, alongside any
  deck-level ERROR (a part absent from a measured node, `SIM_PART_SKIPPED` —
  numbers that exclude a series element must not ship as a PASS). Deck
  WARNINGs (routine unmodeled parts off the measured path) stay out of the
  gate. A requested sim gate that cannot run (no libngspice, deck build
  failure) **FAILS, never skips** — a PASS manifest may not ship because the
  release machine lacked the engine. No `--sim` = skipped-with-reason, like
  every other gate. Escape hatches are explicit and audited: the spec's
  `acknowledged_unmodeled` array downgrades a named designator's escalation
  to WARNING, and config `[[waiver]]` entries apply to sim-gate findings the
  same way they apply to `check`.

- **The DRC gate is opt-in and strict, the same way**: `--drc` (with
  `--pcb`) runs `kicad-cli pcb drc` and turns KiCad's report into gate
  findings — `KICAD_DRC_VIOLATION` per violation, `KICAD_DRC_UNCONNECTED`
  per unrouted connection, at KiCad's own severity, the KiCad rule type
  leading each message (`clearance: …`). A requested DRC gate that cannot
  run **FAILS, never skips**: `KICAD_CLI_MISSING` (no kicad-cli — install
  KiCad or set `KICAD_CLI`), `KICAD_DRC_FAILED` (the board did not load,
  KiCad is older than 8, or the report was unreadable). `--drc` without
  `--pcb` is a usage error. What to know when you read its result:
  - **Zone fills are evaluated as saved, not refilled** (the gate's summary
    line says so every run). A board whose zones were not refilled after the
    last edit is judged with stale copper — refill and save in KiCad, commit,
    then run preflight. akcli never asks KiCad to refill or save.
  - **The project is not modified**: KiCad runs on a staged copy of the board
    with its `.kicad_pro` / `.kicad_dru` / `fp-lib-table`, so the project's
    rule severities, DRC exclusions and custom rules all apply.
  - **Triage in KiCad first, waive in akcli second.** A rule the project
    deliberately ignores belongs in Board Setup → Violation Severity (it
    travels in the `.kicad_pro`). A one-off acceptance is a config
    `[[waiver]]` with a reason: `code = "KICAD_DRC_VIOLATION"` plus `refs` =
    the KiCad rule type (`"track_dangling"`), a designator (`"U3"`), a pin
    (`"R1.1"`) or a net (`"GND"`). Never waive by code alone — that silences
    every rule KiCad has.
  - No `--drc` = skipped-with-reason; say so in the summary. The akcli `fab`
    gate is fab policy and cost, not geometric DRC — the two are complements.

- **The two library gates answer "is this what we released last time?"**
  `library-audit` only proves the libraries *resolve*; a footprint edited in
  place resolves perfectly.
  - **`library-baseline`** compares every project library file (`.kicad_sym`,
    `.kicad_mod`, referenced 3D models, `provenance.json`) against the
    committed, sha256-hashed record written by `akcli library audit
    --write-baseline` (default `akcli-library.baseline.json` beside the
    `.kicad_pro`; `--library-baseline FILE` or `[paths].library_baseline`
    name another). `LIB_BASELINE_DRIFT` / `LIB_BASELINE_MISSING` (error) and
    `LIB_BASELINE_UNTRACKED` (warning) all fail the gate. No baseline
    anywhere = skipped-with-reason — say so; a *named* baseline that is
    missing or unreadable is an error, never a pass. After an intended
    library change: re-run `--write-baseline`, review the diff, commit it.
    Never re-baseline just to turn the gate green — the diff IS the review.
    If every file reads as drift on another machine, the repo is converting
    line endings: mark library files `-text` in `.gitattributes`
    (docs/design-integrity.md).
  - **`pcb-library`** (with `--pcb`) compares each board footprint with its
    definition in the project `fp-lib-table`: `FPLIB_PAD_SET_DRIFT`,
    `FPLIB_PAD_DRILL_DRIFT`, `FPLIB_SOURCE_MISSING` (warnings — they fail
    the gate) and `FPLIB_MODEL_MISSING` (note). A deliberate local board edit
    is legitimate: waive it by designator with a reason. **Read the gate's
    summary line**: it states how many footprints were compared and how many
    were NOT (KiCad's own global-table libraries are never compared) —
    repeat those counts in the release summary; a pass covers only the
    compared ones. A run that could compare no footprint at all is
    skipped-with-reason, not passed.
  - The `sch-pcb` gate also fails on `SCHPCB_DNP_MISMATCH` /
    `SCHPCB_BOM_FLAG_MISMATCH` (schematic vs footprint DNP / BOM flags) —
    fix with "Update PCB from Schematic", do not waive a BOM disagreement
    casually. A board-only footprint that KiCad marks "Not in schematic"
    (mounting hole, fiducial) is a NOTE and does not fail the gate.

Two things preflight does **not** cover — say so in the release summary
instead of implying they were checked:

- **Assembly CPL is not produced.** `jlc bom --csv` covers the assembly BOM,
  but the centroid/placement (CPL) file must be exported from the PCB in
  KiCad (Fabrication Outputs → Component Placement) — akcli has no PCB stage.
- **Document integrity is part of the `check` gate.** For a `.kicad_sch` that
  gate runs ERC/power/BOM/nets plus the tree-scoped integrity checks
  (dangling endpoints, duplicate uuids/designators, unresolved `lib_id`s,
  instance paths, stacked power ports — two rails merged with nothing visible
  to see — and no-connect conflicts), so a duplicate designator or an
  unresolved symbol library fails the gate.

## Gate doctrine

- **Give every input you have.** Each omitted flag is a skipped gate; the
  report and manifest say so, and your release summary must repeat the
  skipped list — "PASS with 4 gates skipped" is the honest sentence.
- **The manifest is the artifact.** Always pass `--out`; the manifest's
  sha256-bound inputs are what makes the release auditable later. Beyond one
  hash per input file it records: every gerber/drill file's hash
  (`inputs.gerbers.file_hashes`), the fab profile as `{id, sha256, bundled}`,
  the kicad-cli version (`tool.kicad_cli`, `null` when none is installed),
  and — for a dirty worktree — `git.dirty_sha256`, a hash of the uncommitted
  diff plus the untracked paths. That last value proves two manifests came
  from the same dirty state on one machine; it is not comparable across
  machines (line-ending settings change the diff bytes), so a release of
  record still means a clean worktree.
- **A FAIL is findings, not a tool error** (exit 1 = findings, per-gate
  detail in the output). Fix or explicitly waive (with reason) — never
  re-run with fewer inputs to make it pass.

## The review gate — blocking is earned, not default

Review findings are ADVISORY everywhere else. The ONLY path by which one
blocks a release is an explicit policy file:

```toml
# policy.toml
[review]
profile = "standard"
allow = ["REVIEW_PCB_UNROUTED", "REVIEW_FB_DIVIDER_VREF_MISMATCH", "REVIEW_GERBER_STALE"]
```

Rules you enforce when authoring or reviewing a policy:

- **Only calibrated codes enter `allow`.** The promotion path is: replay a
  corpus of real boards (`python tools/corpus_replay.py CORPUS_DIR
  --write-baseline base.json`, dev checkout only) → measure the
  false-positive rate on boards known good → only then allowlist. Never
  allowlist a `heuristic`-confidence rule that has not been through this —
  a false blocker destroys trust in the whole gate.
- Start from `deterministic` rules (`REVIEW_PCB_UNROUTED`,
  `REVIEW_GERBER_*`) and `datasheet_backed` rules; they are recomputable
  facts, not judgements.
- An empty/missing `allow` list is a USAGE error by design — a policy that
  blocks on "everything" is not a policy.
- The policy file's sha256 + allow list land in the manifest
  (`inputs.review_policy`) — a release is auditable against WHICH rules were
  allowed to block it.
- Facts auto-load from `<sch dir>/datasheets/` — a well-stocked facts store
  (akcli-datasheet-facts skill) makes the gated rules `datasheet_backed`.

## The gerber gate — never order a stale package

```bash
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --gerbers fab/ --allow-dirty --out release.manifest.json
```

With `--gerbers` the package is checked for: the minimum fab set (copper ×2,
masks, outline, plated drill; missing silk is a note), copper-file count vs
the board's declared stackup, layer registration (per-layer extent alignment —
unrelated to the `akcli bbox` symbol-placement subcommand), mixed units, and
**staleness** — the outline gerber's size vs the board file's Edge.Cuts
extent. `REVIEW_GERBER_STALE` means the export predates the last board edit:
re-export, never ship. This is the "ordered the old rev" failure, caught
mechanically.

## Reporting a release decision

Close with a verdict backed by the manifest:

- **GO** — every gate PASS; list skipped gates and why they are acceptable
  for THIS release (e.g. no order manifest because the fab takes gerbers
  directly).
- **NO-GO** — quote the failing gate's findings verbatim (code + message);
  map each to its fix path (`review propose` for value fixes → the
  akcli-schematic-authoring skill; re-export for gerber staleness; commit
  for a dirty worktree).
- Never present "PASS with `--allow-dirty`" as a clean release — the
  manifest records the dirty worktree; your summary must too.

## Between releases: `akcli gate` — the iteration-weight sibling

Re-running full preflight after every GUI edit round is the wrong weight.
`akcli gate` bundles the four per-edit checks with none of the manifest /
input-hashing / git machinery:

```bash
akcli gate                                     # zero-argument in a configured project
akcli gate board.kicad_sch --pcb board.kicad_pcb --intent intent.json --lock bom.lock.json
```

Gates: the **full default** `check` set (wider than preflight's
erc/power/bom/nets + integrity — layout/pairs included for a
`.kicad_sch`), `intent`, `sch-pcb`, and `bom-lock` drift — each falling back
to its `[paths]` config key (`schematic`/`pcb`/`intent`/`bom_lock`), each
skipped-with-reason when absent. **Offline by default**: the bom-lock gate
answers from the HTTP cache (`--live` opts one run into a fresh fetch), and a
cache miss degrades the line to `unverified` — always counted, so a cold
cache never reads as a fully-verified PASS. Exit is worst-of across the four
(`--fail-on`/`--exit-zero` re-tune it). Run `gate` per edit round; run
`release preflight` when a release decision needs a manifest.

## When NOT to use this skill

- Running a design review (findings, not gating) → akcli-schematic-review.
- JLC manufacturability/cost questions → akcli-jlcpcb-capabilities; fab
  profile authoring lives with `akcli fab explain`.
- Fixing what a gate caught → akcli-schematic-authoring (proposal adoption).
