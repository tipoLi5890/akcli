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
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile fab.toml --gerbers fab/ --contract contract.toml --review-policy policy.toml --out release.manifest.json
```

Gates, in order: `check` (ERC/power/BOM/nets) → `intent` → `contract` →
`library-audit` → **`project`** (a `.kicad_pro` beside `--sch`; the same
`project check` findings, library auditing switched off there so
`library-audit` above isn't reported twice — no project file skips with a
reason) → `sch-pcb` → `fab` → `order` → **`sim`** (`--sim` sim.json;
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

Two things preflight does **not** cover — say so in the release summary
instead of implying they were checked:

- **Assembly CPL is not produced.** `jlc bom --csv` covers the assembly BOM,
  but the centroid/placement (CPL) file must be exported from the PCB in
  KiCad (Fabrication Outputs → Component Placement) — akcli has no PCB stage.
- **Document integrity is not in the `check` gate.** That gate runs
  ERC/power/BOM/nets only; run `akcli check <sch> --integrity --fail-on never`
  alongside preflight (dangling endpoints, duplicate uuids/designators,
  unresolved `lib_id`s, instance paths, stacked power ports — two rails merged
  with nothing visible to see — a `(sheet …)` whose child file is missing, and
  no-connect conflicts) and report its
  result with the manifest — a duplicate designator or an unresolved symbol
  library is a ship-stopper the gate list will not raise.

## Gate doctrine

- **Give every input you have.** Each omitted flag is a skipped gate; the
  report and manifest say so, and your release summary must repeat the
  skipped list — "PASS with 4 gates skipped" is the honest sentence.
- **The manifest is the artifact.** Always pass `--out`; the manifest's
  sha256-bound inputs are what makes the release auditable later.
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

Gates: the **full default** `check` set (wider than preflight's fixed
erc/power/bom/nets four-tuple — layout/integrity/pairs included for a
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
