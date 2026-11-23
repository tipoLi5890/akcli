# Roadmap

`akcli` is an **AI-native KiCad design agent**: an LLM (or a plain CI pipeline) authors and edits
`.kicad_sch` from a versioned JSON op-list behind net-diff safety rails, verifies the result with
checks it can gate on, **runs an advisory engineering design review**, simulates it on KiCad's
bundled ngspice, sources real parts, and imports Altium `.SchDoc`/`.SchLib`/`.PcbDoc` into the same
normalized model — a zero-dependency (pure-stdlib Python) toolchain, purpose-built for KiCad.
Every output is typed, versioned,
and machine-checkable, because the primary user is an agent shelling out from a pipeline.

KiCad is the writable target and the sole development line. Altium is an **import source** — a
read-only on-ramp into the KiCad flow, not a symmetric conversion peer. That repositioning
(2026-07) reshaped this roadmap: the Altium-interop items that earlier milestones treated as
release-critical now live in a demand-driven optional track, and the experimental Windows live
bridge was **deleted outright** (0.17).

## Where we are (v0.23.0)

Shipped and working today (details per release in [CHANGELOG.md](CHANGELOG.md)):

- **KiCad authoring:** `plan`/`draw` from a `protocol_version 2` op-list — **22 ops + 10 macros**
  (incl. hierarchical `add_sheet`, optionally creating its child sheet in the same transaction with
  `"create": true`, `rename_net`, cascade delete, multi-unit placement, `mid()`
  anchors), `akcli new` blank-sheet bootstrap, `akcli project new`/`project init` KiCad project
  scaffolding (`.kicad_pro`/a blank `.kicad_pcb` board container/lib tables/`.gitignore`, atomic and
  idempotent, `--example NAME` for a packaged worked op-list — akcli writes no PCB content, the
  board is a scaffold, not a layout), `akcli project info`/`project check` reading
  and gating that project's integrity (root-sheet binding, sheet tree, board, lib tables,
  gitignore coverage — reused by the `release preflight` `project` gate), deterministic UUIDv5 idempotency, atomic apply with a
  rotated backup stack (`undo --list`/`--steps`), a pure-Python connectivity gate, a before/after
  **net-membership diff** on every run (a minority pin group moving between two existing nets
  prints one `MOVE` line, not a SPLIT plus a MERGE), and `--apply --strict-nets` refusing
  named-net splits/merges/moves. **Net-preserving re-layout** (0.8.0): `move_component` carries a part's net labels
  and wire endpoints (`carry_labels`/`carry_wires`), and `arrange --groups` relocates whole
  functional blocks as rigid bundles; `library check-lock` refuses a write under an open KiCad GUI.
  `relink-symbols` refreshes stale embedded libraries behind a net-equivalence gate (and, like
  `library repair`, refuses to write under an open KiCad GUI). Ops apply to a **child sheet of an
  existing hierarchy**, not just to a root file, and symbol resolution falls back to the project and
  per-user global `sym-lib-table` when neither `--symbols` nor the config provides a nickname (0.17);
  the `${KICAD*_SYMBOL_DIR}` variables those bundled tables are written against are compiled into
  KiCad rather than exported, so akcli derives them from the located `kicad-cli` — standard
  nicknames resolve **on a stock KiCad install**, an environment value still wins, and `doctor`'s
  `symbols` probe reports whether the resolution works on this machine (0.18).
  **Hierarchy-first (0.20)**: `plan`/`draw` on a child sheet now evaluate the net gate over the
  whole sheet tree, not the child alone — the root is auto-discovered from the nearest
  `.kicad_pro` (or named via `--root`/`--flat`). A `sheets` envelope routes one op-list across
  several files inside a single atomic transaction, recoverable with `akcli undo <target> --run
  <run_id>`, and a sheet instantiated more than once now takes per-instance references
  (`instance_designators` on both ops), refusing a duplicate refdes with the new
  `INSTANCE_REFS_REQUIRED`. `add_sheet` now takes an optional `create: true` to write the child
  `.kicad_sch` as a blank stub in the same transaction, instead of a separate `akcli new` step.
  **Modular authoring (v0.10)**: functional-group envelope with group-local coordinates and a
  persisted `Group` property, `akcli groups`/`--frame` self-refreshing module borders,
  bare `arrange --groups --frames`, relative placement (`anchor`+`offset_mil`), `place_array`,
  pin-safe `route_net` L/Z auto-routing, `add_rectangle`/`add_text_box` annotation graphics with
  stable `key`s, `set_title_block`, `akcli bbox` spacing planning, `plan/draw --render` look-
  before-apply previews and `render --grid` coordinate overlays, plus group layout lints.
- **Verification:** ERC-lite / power / BOM / nets / geometry / layout / libsync / **integrity**
  checks (0.17: `check --integrity` runs the write gate's document checks — dangling endpoints,
  duplicate uuids/designators, unresolved `lib_id`s, instance paths, no-connect conflicts — read-only
  over any `.kicad_sch`, and joins the default family; `POWER_RAIL_NO_SOURCE` flags a rail nothing
  feeds; the type-confidence demotion is now per **net**, so a fully-typed net keeps full ERC
  severity on an otherwise low-confidence board; 0.23: tree-scoped — the given sheet and every
  sheet below it, with `DUPLICATE_DESIGNATOR` judged across sheets per live instance),
  **design-intent assertions** (`nets --intent-snapshot` → `check --intent`, per-net modes +
  wildcards), checker-agnostic `[[waiver]]` config + `--fail-on`, SARIF/JUnit output, structured
  `pos`/`anchors` on findings. Net inference is **arbitrated against `kicad-cli`'s own netlister**
  (a standing parity harness incl. rotation/mirror transforms, label scoping, buses, hierarchy) —
  as is `export --format kicad`, which carries the `design`/`libparts` sections and the
  `sheetpath`/`tstamps` UUID identity a KiCad re-import keys footprints by (0.17).
- **Design review (0.8.0):** `akcli review analyze|report|explain|facts|propose|testbench|diff|tree|validate`
  — an advisory engineering-review engine on the normalized model (so it reviews Altium `.SchDoc`
  as readily as `.kicad_sch`). Six detector families (signal / validation / pcb / emc / domain /
  gerber; **44 rules**) emit **confidence-graded** findings (deterministic / heuristic /
  datasheet_backed / llm_reviewed) with an evidence envelope published as `findings.schema.json`.
  A **datasheet facts store** (`review facts`, PDF sha256+page pinned) upgrades findings to
  datasheet_backed; `review propose` recomputes + E-series-snaps fixes into op-list/contract/sim
  drafts (never touching a file — they go back through `plan → draw`); `review validate` gates LLM
  candidates (four checks, quarantine); `review diff`/`tree` add fingerprint-aligned drift and a
  power tree. A **DNP part satisfies nothing** — a Do-Not-Populate pull-up/load-cap/bypass-cap/
  TVS/EN-tie never silences a presence rule (in `review` or `check`), while BOM enumeration still
  sees it (0.17). Advisory by default — the only blocking path is `release preflight --review-policy`
  (a calibrated allowlist). Guide: [docs/review-rules.md](docs/review-rules.md).
- **Design integrity:** design **contracts** (`check --contract` — require/forbid
  pin-net & pin-pair topology, component values, NC pins, owned/expiring exceptions), schematic ↔
  PCB **equivalence** (`verify sch board.kicad_pcb` — pad-net partition, refdes, footprint), the
  **`library`** workspace (audit/repair/import-altium/check-lock — fixes the footprint-nickname &
  3D-path traps, dry-run→apply), versioned **`fab`** profiles (`fab check`/`explain` — free-via
  envelope, tenting, via-in-pad, cost thresholds, order manifest), and a **`release preflight`**
  gate emitting a traceable manifest (with `--review-policy` and `--gerbers` gates in 0.8.0, and
  the opt-in `--sim` assertion gate — a requested sim that cannot run fails rather than skips).
  0.23 hardened the suite: `release preflight` gained `--drc` (KiCad's own DRC on a staged copy),
  `library-baseline` and `pcb-library` gates, and integrity checks inside `check`, and its manifest
  records evidence (`kicad-cli` version, per-file gerber hashes, bundled-profile flag, dirty-tree
  hash); `fab profiles` lists the bundled profiles, selectable by id wherever a profile file is
  accepted; `fab check` measures the true `Edge.Cuts` outline area, recognises buried vias and
  real tenting, compares the project's own design rules with the profile, flags narrow slots, and
  supports per-profile cost severity; `library audit --write-baseline`/`--baseline` commits and
  checks hashes of the project libraries; `verify --library` compares board footprints with their
  library source (pad set, drill, DNP/BOM flags); `[[waiver]]` entries apply to `library audit`
  and `verify`; `check --contract` accepts numeric value ranges and `{source, page, url}`
  evidence. Guide: [docs/design-integrity.md](docs/design-integrity.md).
- **Simulation:** `akcli sim` — schematic → SPICE deck → KiCad's bundled libngspice in a
  crash-isolated, timeout-killed child; `sim.json` assertions (two-sided bounds, multi-analysis),
  `--sweep` corner matrices, `--deck-only` engine-free mode, floating-node detection with
  auto-`rshunt`, `fit-diode` datasheet fits written back to KiCad-native `Sim.*` fields. The deck
  build refuses to guess: a component value is normalized (`SIM_VALUE_NORMALIZED`) or rejected
  (`SIM_VALUE_INVALID`), two nets that would collapse onto one ngspice node raise
  `SIM_NODE_COLLISION`, and every part left out of the deck is reported (`SIM_PART_SKIPPED`) (0.17).
- **Parts & manufacturing:** `jlc search`/`show`/`add` (in-process LCSC → KiCad conversion, with
  EasyEDA pin types mapped onto KiCad electrical types and the part's real `Value`),
  `jlc bom` purchasability with qty tier pricing + JLCPCB order-CSV export + confidence-gated
  `--fix`, `jlc datasheet` PDF resolution/fetch (whole-BOM batch, `--resolve-mpn`) — which now
  feeds the review facts store.
- **Calculators:** `akcli calc` — 60 standards-cited engineering calculators, engineering-notation
  inputs, `--ops` bridge into op-lists (also the engine the review layer recomputes fixes through).
- **Readers:** KiCad 7–10 S-expression (bounded, non-recursive, hierarchical) incl. **deep
  `.kicad_pcb`** (pad-net bindings, tracks/vias/zones, board setup); Altium binary `.SchDoc`
  (multi-sheet + `.PrjPcb`), text-record `.SchLib`, `.PcbDoc` ASCII sections **plus binary copper**
  (`Tracks6`/`Vias6`/`Arcs6`/`Pads6`, cross-validated against KiCad's importer), **`.PcbLib`**
  footprint libraries → `FootprintDef`/`FootprintPad` plus track/arc graphics (also
  `.kicad_mod`/`.pretty`; `library import-altium` writes the graphics, `--layer-map` redirects a
  mechanical layer, 0.23), and a
  **RS-274X/Excellon gerber** directory reader (0.8.0) feeding the fab-package checks.
- **Agent surface:** Claude Code / Codex plugin (`akcli`), 13 skills, 8 slash commands, `akcli view`
  dashboard (hub + calc + live watch with SSE, ERC markers, lint overlay, BOM panel), stable exit
  codes 0–8, `schema_version`-stamped JSON, and a `--json` error envelope (`{"error": {code,
  message, exit, remediation}}`) on every failing exit path so stdout always parses as JSON even
  on failure. `akcli --version` reports the code actually running (a
  source checkout's `pyproject.toml` wins over stale installed metadata). 0.9.0 additions:
  `akcli capabilities` (the self-describing surface manifest), `akcli render` (pure-stdlib SVG),
  the workspace write journal + `akcli log`, `akcli ops validate` + a PreToolUse draw guard,
  `read --summary`/`nets --match`/`--limit` output throttling, and structured per-op
  `remediation` hints. 0.12.0 additions: `akcli doc` (the pinout book), the `.akcli/` workspace
  state root (journal + rotated undo backups + `--note` design-intent records,
  [docs/agent-state.md](docs/agent-state.md)), 2D side-by-side group packing
  (`arrange --groups --page-width`, `[arrange]` policy, `[check] group_clearance`), and the
  enforced net-preservation gate on `arrange --groups --apply`. 0.13.0 additions:
  `arrange --groups --propose-labels` (the refusal -> label-on-pin repair-draft loop, proven on a
  real 88-part board now in the corpus), collision-free two-phase group moves, bare
  `check --intent` via `[paths] intent`, the `doctor` workspace probe, config-surface/schema-table
  conformance gates, and agent-eval task 07 (safe re-pack post-check). 0.14.0 additions: an
  output-placement policy (SPEC §10) classifying every written file state/cache/deliverable,
  self-ignoring `.akcli/` state roots (a dropped `*` `.gitignore`, legacy roots self-heal),
  `[paths].parts_dir` for `jlc add`/`library import-altium` output, and bare `akcli view`
  auto-discovering the single `.kicad_sch` in the current directory. 0.17 additions: argparse
  failures (wrong flag, wrong choice, unknown command) emit the standard `USAGE` error envelope
  under `--json`, an `INTERNAL` last-resort handler keeps the envelope contract even for an
  unexpected exception, a corrupt numeric field parses into `KICAD_SEXPR_BAD_NUMBER` instead of a
  raw traceback, and pin-level `net --at`/`--same`/`--why` answer connectivity questions directly.
  0.20 additions: `akcli capabilities --section NAME`/`--command VERB` slice the ~106 KB manifest
  into a stamped subset, and `akcli schema NAME` prints any packaged JSON Schema byte-for-byte.
  0.23 additions: a GitHub Actions workflow recipe that gates schematic pull requests
  ([docs/recipes/](docs/recipes/README.md)), the public-contract inventory
  ([docs/stability.md](docs/stability.md)), and an agent-eval corpus of 20 tasks covering the whole
  op vocabulary behind a coverage gate.
- **Quality gates:** ~4 500 tests (parser fuzzing, round-trip netlist properties, live ngspice in
  CI, Windows/macOS/Linux × Python 3.11–3.14), ruff over `src`/`tests`/`tools`/`hooks` + a mypy
  beachhead scoped by `[tool.mypy].files` in `pyproject.toml` (`parts/`, `calc/`, the typed core,
  the net/parse spine, the renderer, the `pipeline/` engines, `opspec`, the project scaffolder and
  the `.kicad_pro` reader), a **docs-conformance gate** (every fenced `akcli …` line in the docs is
  validated against the live argument parser, and the op, macro, calculator, skill, command,
  schema, rule and eval-task counts are asserted against the repository, with `docs/` scanned
  recursively), an AST text-I/O portability gate over `src/akcli`, `tools/` and `hooks/`, wheel-install
  smoke, and tag-driven GitHub Releases that refuse to publish unless CI is green on the tagged
  commit — 0.18 made "CI never ran this commit" a distinct failure from "CI ran and failed" and
  pinned the `windows-latest` matrix jobs by name, so a shrinking matrix fails the gate.

Honest limitations:

- **Binary `.SchLib` symbol records are refused loudly** (exit 5); only text-record libraries read.
- **`.PcbLib` import covers pads and track/arc/circle graphics.** Text, fills, regions, 3D bodies,
  vias and keepouts are skipped with a counted `UNSUPPORTED_PRIMITIVE` warning.
- **`.PcbDoc` `Fills6`/`Regions6`/`Texts6`/`Polygons6` are skipped**, not parsed.
- **Records the reader does not model are counted, not silently dropped** — an Altium read warns
  `altium: skipped N unsupported record(s): 26 (Bus) ×3, ...` (stderr + `read --json` `warnings`).
  Buses (`Bus`/`BusEntry`) still do not reach `netbuild`, so a bus-wired sheet imports with missing
  connectivity — visible, but missing.
- **A `.PrjPcb` with several top-level sheets reads only one** (the others are named in a warning).
- **ERC power checks are net-name + power-port based by design.** The pin-type conflict
  matrix shipped in 0.9.0 (`ERC_PIN_CONFLICT` — the high-signal cells of KiCad's default
  matrix — plus `ERC_POWER_IN_UNDRIVEN`), type-confidence-gated like every type-based rule —
  per net, so only a net that still has untyped pins is demoted;
  the remaining KiCad matrix cells (unspecified-vs-anything etc.) are deliberately not
  flagged (documented in `checks/erc.py`).
- **PDN impedance / anti-resonance is not built** — the EMC review is a pre-compliance risk
  analyzer (never a compliance verdict). Stdlib SVG rendering shipped in 0.9.0 as
  `akcli render` and draws faithful symbol artwork from the embedded `lib_symbols` on KiCad
  sources (0.14.0; 0.18 made every optional node in a symbol body None-safe, so the standard
  KiCad symbol library renders instead of crashing); Altium sources and multi-unit parts fall
  back to synthesized bodies, and `view live`'s canvas still renders through the optional
  `kicad-cli`.
- **MCP server: deferred by decision** — agents drive the plain CLI today.

## Guiding principles

1. **KiCad is the writable target; Altium files are never modified.** No code path writes an
   Altium file on disk, and none drives a running Altium either (the live-bridge scaffold was
   deleted in 0.17).
2. **Verify everything.** Dry-run by default, `--apply` is explicit and atomic, every write is
   re-read, connectivity-gated, and net-diffed; a "0 findings" report always carries its metadata
   caveats. New write capabilities land together with their verification step — and where external
   ground truth exists (`kicad-cli`, ngspice), akcli's own engines are arbitrated against it.
3. **Advisory review, earned gating.** Review findings carry explicit confidence and never block a
   release except through a calibrated, explicitly-approved policy allowlist; a finding that leans
   on a datasheet number cites its PDF sha256+page, and one that lacks evidence says so
   (`insufficient_evidence`) rather than guessing.
4. **Zero runtime dependencies.** Python ≥ 3.11 stdlib only. Network code stays isolated under
   `akcli jlc`; `kicad-cli`/libngspice/`pdftotext` remain optional, discovered, and advisory.
5. **Docs that cannot drift.** Every fenced `akcli …` line in the docs is validated against the
   live argument parser, and the op, macro, calculator, skill, command, schema, rule and eval-task
   counts are asserted against the repository (the docs-conformance gate). Agent-facing contracts (`schema_version`,
   `protocol_version`, exit codes) change only with a changelog entry.

## Shipped milestones (what actually happened)

The original v0.2–v0.6 plan and reality diverged: the planned "v0.5 Altium alive / v0.6 ecosystem"
arc was displaced by KiCad-native depth that proved more valuable in real design sessions. The
honest history:

| Version | Theme that actually shipped |
|---|---|
| v0.2.0 | Editing ops (delete/move/multi-unit), hierarchical KiCad read, agent-contract fixes |
| v0.3.x | Altium multi-sheet + `.PrjPcb`, `expected` adapters, SARIF/JUnit, binary `.PcbDoc` copper |
| v0.4.0 | the calculator pack (60 today) + akcli-design-calc skill, unified `view` dashboard, verify/undo, macro ops, nets check, `jlc bom` |
| v0.5.0 | **Safety-rail release:** net-diff + `--strict-nets`, intent assertions, `mid()` anchors + new macros, `relink-symbols`, `jlc datasheet`, waivers + `--fail-on`, structured finding positions, cli decomposition, transform/netbuild parity fixes, `new` + multi-level undo, bus netlist semantics, ~55× netbuild speedup |
| v0.6.0 | **Simulation release:** `akcli sim` (deck/engine/models/assertions/sweeps/fit-diode), docs-conformance gate, bus aliases, `--resolve-mpn`, mypy calc/, live ngspice in CI |
| v0.7.0 | **Identity release:** project renamed to `akcli` (KiCad-first repositioning), `akcli doctor` + akcli-setup skill, `akcli-` prefix on all skills, JLCPCB manufacturing-handoff docs, one kicad-cli discovery ladder, docs gate widened to INSTALL/ROADMAP, README restructured KiCad-first |
| v0.8.0 | **Design review release:** the `akcli review` engine (M1–M9 below — signal/validation/PCB/EMC/domain/gerber detectors, datasheet-facts store, propose/diff/tree, deep-review gate, gerber package checks), design-integrity suite (contracts, sch↔PCB `verify`, library workspace, fab profiles, `release preflight`), deep `.kicad_pcb` + Altium `.PcbLib` reading, net-preserving re-layout (`arrange --groups`, `move_component` carry), `library check-lock`, `findings.schema.json`, and the working-tree-authoritative `--version` fix |
| v0.9.0 | **Agent-contract release:** capabilities manifest (+op constraints, altium_live_wired honesty), QUERY_MISS exit 8, output throttling (`read`/`nets`/`component` `--match`/`--limit`, `read --summary`), write journal + `akcli log`, `ops validate` + PreToolUse draw guard, published diff/pinmap/draw-result schemas, `--fail-on` everywhere, remediation on ALL error codes, `--json` error envelope on every exit path, universal `schema_version` stamps (AST-enforced), `akcli render` SVG, ERC pin-conflict matrix, `check --pairs`, `draw --no-erc`, `/circuit-parts`, golden corpus + review-calibration baseline in CI, sim behavioral models + solver-trap diagnostics, `review testbench`, and the agent-loop eval harness (`tools/agent_eval/`) |
| v0.10.0 | **Modular authoring release:** functional-group envelope (group-local coordinates, persisted `Group` property), `akcli groups --frame`, bare `arrange --groups --frames`, relative placement (`anchor` + `offset_mil`), `place_array`, `route_net`, `add_rectangle`/`add_text_box`, `set_title_block`, `akcli bbox`, `plan`/`draw --render` previews, `render --grid`, group layout lints |
| v0.11.0 | **BOM completeness release:** normalized R/C/L value parsing with reverse-verified LCSC numbers, fitted/DNP/external/no-part BOM classes, BOM lockfiles, multi-board carts, assembly economics, JLC-EDA order CSV, transparent waivers, the AST text-I/O portability gate |
| v0.12.0 | **Group layout and pinout book release:** `arrange --groups --page-width` 2D packing, `[arrange]` policy, `[check] group_clearance`, enforced net-preservation on `arrange --groups --apply`, `akcli doc` pinout book, `.akcli/` workspace state root |
| v0.13.0 | **Repair loop and real-board corpus release:** `arrange --groups --propose-labels`, collision-free two-phase group moves, bare `check --intent` via `[paths] intent`, `doctor` workspace probe, the 88-part production board in the corpus, agent-eval task 07 |
| v0.14.0 | **Power-entry and artwork release:** `signal.power_protect` review rules (44 total), faithful symbol artwork in `akcli render`, output-placement policy, self-ignoring `.akcli/` state roots, `[paths].parts_dir`, bare `akcli view` auto-discovery |
| v0.15.0 | **Distribution release:** first PyPI release as `akcli-kicad` via OIDC trusted publishing, documentation refresh for the new dist name |
| v0.16.0 | **Skills catch-up release:** groups-envelope authoring taught in the plugin skills, slash commands and skills aligned to the 0.13–0.15 surface, Japanese README |
| v0.17.0 | **Write-identity and hardening release:** content-seeded node identity (op-list edits no longer duplicate untouched parts), writable hierarchical child sheets, `check --integrity`, pin-level `net --at`/`--same`/`--why`, machine-readable argparse failures, DNP-aware review, simulation refusal codes, Altium live bridge deleted |
| v0.18.0 | **Post-audit release:** fixes for 0.17 write-correctness regressions (power-port identity, child-sheet instance paths, connectivity-engine parity), standard symbol nicknames resolving on a stock KiCad install, `render` on the full standard library, macro field validation, drift gates |
| v0.19.0 | **Field-hardening and schematic-design release:** `akcli ops from-intent`, `library new-symbol`/`lint-symbols`, `akcli gate`, `NC_PIN_CONNECTED`, sim node-count and suspicious-zero guards, `release preflight --sim` fixes, `-o` alias on `--out` flags, regression locks from a multi-day agent-operated board campaign |
| v0.20.0 | **Hierarchy-first release:** net gate evaluated over the whole sheet tree, multi-sheet op-lists in one atomic transaction (`undo --run`), per-instance references for reused sheets, `capabilities --section`/`--command`, `akcli schema`, `examples/circuits/` |
| v0.21.0 | **Architecture release:** layered packages with a tested layering invariant, `opspec` as the single op vocabulary, eeschema-aligned canonical net names, `protocol_version` 2 and `schema_version` 2.0 ([migration guide](docs/migration-0.21.md)) |
| v0.22.0 | **Project scaffolding release:** `akcli project new`/`init`/`info`/`check`, `.kicad_pro` reader, `release preflight` `project` gate, `add_sheet` `create: true` |
| v0.23.0 | **Design-integrity hardening release:** `release preflight --drc` and library gates, manifest evidence, bundled fab profiles by id, `fab check` fidelity, library baseline, `verify --library`, tree-scoped `check --integrity`, net-diff `MOVE`, `.PcbLib` track/arc import, PR-gate workflow recipe, `docs/stability.md` |

The first PyPI release (`pip install akcli-kicad`) shipped in 0.15.0; the import package and CLI
stay `akcli`.

## v0.8.0 — Design review release (shipped)

A native `akcli review` capability finding the engineering risks structural checks cannot express —
built on the normalized model (so every rule reviews Altium `.SchDoc` too), advisory by default,
findings carrying explicit confidence + evidence with literature citations, and the fix path staying
behind the existing `propose → plan → draw` safety rails. The whole track (M1–M9) shipped together
in 0.8.0. Per-rule specification and provenance: [docs/review-rules.md](docs/review-rules.md).

- [x] **M1 — foundation:** `Finding` evidence envelope (confidence/evidence/fingerprint/
      status), `findings.schema.json` + wheel mirror, SARIF v2 wording-immune fingerprints,
      markdown renderer, `review analyze|report|explain` CLI skeleton
- [x] **M2 — signal detectors:** divider (feedback Vref plausibility, tap-name
      mismatch), RC cutoff (via `calc rc`), crystal load caps, connector ESD/TVS coverage,
      op-amp gain topology (non-inverting/inverting/buffer/open-loop) — five fixture classes each,
      KiCad+Altium format-agnostic contract (the fuse-sizing / reverse-polarity backlog
      closed in 0.14.0 as `signal.power_protect`)
- [x] **M3 — validation detectors + BOM:** I²C pull-up window (missing/strong/weak/
      mismatch via `calc i2c-pullup`), cross-voltage-domain signals (level-shifter aware),
      floating enable pins; MPN-coverage sourcing audit into `check --bom` (backlog: SPI/UART
      pull-up rules, full sequencing → M7 power tree)
- [x] **M4 — datasheet facts store:** versioned facts schema (`datasheet-facts` 1.0,
      wheel-mirrored) + `review facts add|verify|lookup`, PDF sha256+page binding
      (`jlc datasheet` supplies the PDFs), optional `pdftotext` driver for quote verification;
      divider Vref / crystal CL / vdomain abs-max upgraded to `datasheet_backed`
- [x] **M5 — PCB detectors + thermal:** copper-island union-find (layer-aware,
      zone-bbox merge), unrouted-net detection, decap distance, exposed-pad thermal vias,
      IPC-2221 trace ampacity, junction-temperature estimation (facts-backed θ_JA with
      typical-package fallback) — DFM scoring deliberately left to `fab check` (single
      policy source)
- [x] **M6 — EMC rules:** eight rules across the geometric / analytical /
      stackup batches (plane presence+coverage, via stitching, edge + clock-edge routing,
      diff-pair skew, TVS placement, adjacent signal layers), every threshold a stated
      assumption; advisory `emc` metadata block (risk score + probe points + the standing
      not-a-compliance-verdict note). PDN impedance/anti-resonance stays on the backlog
      (needs zone polygons / SPICE)
- [x] **M7 — closed loop:** `review propose` (values recomputed + E-series-snapped
      via `calc eseries`; op-list/contract/sim drafts; schema-enforced "unconfirmed ⇒ no
      op-list"; layout fixes stay manual — akcli writes schematics only), `review diff`
      (fingerprint-aligned drift, `--fail-on-new`), `review tree` (rails → regulator via FB
      divider → consumers). The auto-generated subcircuit SPICE testbenches from this
      backlog **shipped in 0.9.0** as `review testbench` (RC corner + divider-ratio
      generators, cone extraction, recomputed bounds, ngspice verdicts). Backlog: full
      what-if parameter sweeps
- [x] **M8 — deep-review gate + blocking policy:** `review validate` (four
      deterministic gates, failures quarantined with reasons, accepted = `llm_reviewed`
      observations), `release preflight --review-policy` (explicit allowlist is the only
      blocking path; policy hash in the manifest), first domain family (USB-C CC
      termination), `tools/corpus_replay.py` calibration harness. Backlog (demand-ordered):
      RF/Ethernet/HDMI/memory/BMS/motor families; lifecycle audit via optional distributor
      drivers (`jlc bom` already covers LCSC purchasability)
- [x] **M9 — Gerber:** RS-274X/Excellon directory reader (X2 role detection,
      never-guess coordinate handling) + package checks (completeness, stackup count,
      registration, mixed units, outline staleness vs the board file) wired into
      `review analyze --gerbers` and a `release preflight --gerbers` gate

Three plugin skills teach the agent half of the loop: `akcli-datasheet-facts` (PDF → audited facts),
`akcli-deep-review` (candidate generation gated by `review validate`), and `akcli-release-gating`
(preflight + calibrated blocking policy).

## v0.9.0 — Agent contract completeness & deeper verification (shipped)

Goal: close the remaining "an agent can drive it blind" gaps, and grow `check` from ERC-lite toward
a tunable rule engine reaching across artifacts.

- [x] Publish `diff.schema.json`, `pinmap.schema.json`, and a draw-result schema,
      `schema_version`-stamped and mirrored into the package like the existing schemas —
      **shipped in 0.9.0**, plus the previously-missing `netlist`/`schematic` wheel mirrors
      and one `$id` host across every schema (M)
- [x] Machine-detectable misses: `found: false` + exit `8` (`QUERY_MISS`) for `net <file> NAME`
      and `component <file> REF`; reserved (unraised) `BRIDGE_BUSY`/`BRIDGE_TIMEOUT` codes — **shipped
      post-0.8.0** (S)
- [x] `/circuit-parts` slash command wiring `jlc search → show → add → plan → draw` into one
      documented, stage-gated flow — **shipped in 0.9.0** (S)
- [x] PreToolUse hook in the plugin: `akcli ops validate` blocks a structurally invalid
      `draw --apply`; a missing prior `plan` (checked against the workspace journal) warns —
      **shipped in 0.9.0** (S)
- [x] Honest flags: `draw --no-erc` skips the advisory `kicad-cli` run (logged, never
      silent) and every failed op carries a machine-readable `remediation` field —
      **shipped in 0.9.0** (S)
- [x] ERC pin-type conflict matrix (`ERC_PIN_CONFLICT` — the high-signal KiCad cells — and
      `ERC_POWER_IN_UNDRIVEN`) behind the typed-pins confidence demotion — **shipped
      post-0.8.0**; the remaining matrix cells are documented non-goals in `checks/erc.py` (M)
- [x] Differential-pair / bus continuity checks (`check --pairs`, default-on): `PAIR_INCOMPLETE`
      (asymmetric — a lone `_N`/`_L` active-low name never fires), `PAIR_PIN_MISMATCH`, `BUS_GAP`;
      configurable via `[check]` `pairs`/`pair_suffixes`/`bus_min_family` — **shipped
      post-0.8.0**; the review EMC layer keeps the geometric *skew* detector (M)
- [x] Golden-file regression corpus: frozen `nets`/`check`/`diff`/`review`/`render` snapshots
      over the committed fixture boards, byte-compared in CI
      (`tests/golden/` + `tools/golden_regen.py`) — **shipped in 0.9.0**; growing it with
      real-world boards stays open (M)
- [x] Sim deepening — `AKCLI_OPAMP` + `AKCLI_NMOS_SW`/`AKCLI_PMOS_SW` (engine-validated in
      CI), the `SIM_ZERO_PASSIVE`/`SIM_STIMULUS_SHORTED` solver-trap warnings, and
      **`review testbench`** — **shipped in 0.9.0**; the waveform panel in the `view`
      dashboard is deferred by decision (2026-07) (M)

- [x] `--json` machine-readable failure envelope on every exit path (code recovery from
      wrapped errors, remediation for every code incl. EXIT pseudo-codes) — **shipped in 0.9.0** (M)
- [x] Universal `schema_version` stamps on every JSON object payload, enforced behaviorally AND
      mechanically (AST scan) in CI — **shipped in 0.9.0** (S)
- [x] Review-rule calibration baseline replayed in CI (`tools/corpus_replay.py` +
      `tests/golden/corpus_replay_baseline.json`) — **shipped in 0.9.0** (S)
- [x] Agent-loop eval harness (`tools/agent_eval/` — 20 ground-truthed design tasks (0.20 added hierarchy + bus coverage), scored
      through the real safety rails; references CI-pinned at 100%) — **shipped in 0.9.0** (M)

**Exit criterion:** a schematic PR can be gated end-to-end (check + review + diff + intent + sim
assertions), and a false finding is tuned or waived in config rather than ignored.

## Visual review (shipped in 0.9.0 / 0.12.0)

Goal: humans reviewing agent work get visuals and documents, not just JSON.

- [x] Pure-stdlib SVG schematic rendering from the normalized model (components, pin tips, wires,
      junctions, labels) for stdlib-only before/after review — **shipped in 0.9.0 as
      `akcli render`** (connectivity-true, per-sheet blocks, deterministic); **faithful symbol
      artwork shipped in 0.14.0** (`render_art` walks the embedded `lib_symbols` graphics through
      the net engine's own transform chain; synthesized-body fallback for Altium/multi-unit); a
      `view live` integration stays open (L)
- [x] `akcli doc <file> -o book.md`: pinout book composing per-IC/connector pin tables, rail
      summary (from `review tree`), and BOM — **shipped in 0.12.0** (deterministic Markdown +
      `--json`; `--refs` widens the pin-table set) (M)
- GitHub Action and the `view` waveform panel — a workflow recipe ships in
  [docs/recipes/](docs/recipes/README.md); a packaged Marketplace Action and the waveform panel are
  **deferred by decision (2026-07)**, see below.

**Exit criterion (met in 0.12.0):** `/circuit-draw` can show a human what it placed (`render` +
`doc`), and a design review can start from a generated pinout book.

## Milestones ahead

### v1.0 — Contracts frozen, released

Goal: the public surface is stable enough to promise.

- [ ] Contract freeze audit (S): the audit document is [docs/stability.md](docs/stability.md) —
      contract surfaces with their gates, what is not contract, the versioning rules, the
      deprecation policy (adopted 2026-10-12, in force from 1.0) and the pre-freeze decisions.
      Remaining: the contract-freeze batch — the code changes section 5 decides (exit codes of
      `project info`/`check` on a non-project path, `log --limit` below 1, removal of the
      unraised `BRIDGE_*` codes and the producer-less `netlist` family, the `--exit-zero`
      notice, `--out` on every subcommand, `testbench_version` in the registry, tree-aware
      `#PWR` allocation, the environment-variable and flag gates, the uuid seed pin).
      Started in 0.17:
      every op schema branch now rejects unknown keys (`additionalProperties: false`) and
      `tools/schemas.py check` gates the generated schemas against `opspec` and the packaged mirrors against `schemas/`. 0.18 added
      field-kind and enum-table assertions against `ops.schema.json` (only field *names* were
      compared), two-way mirror pruning so an orphan in `src/akcli/schemas/` cannot hide, and a
      gate asserting the shipped `pyproject` version has a non-empty CHANGELOG section. 0.20 added
      the `sheets` envelope as a purely additive change (`protocol_version` stayed 1 through
      0.20) and the manifest's `constraints.hierarchy` now reports
      `hierarchical_write_v2`/`sheet_reuse: true`. 0.21 spent both numbers deliberately:
      `protocol_version` 2 (four op-field renames) and `SCHEMA_VERSION` 2.0
      (eeschema-aligned canonical net names) — see `docs/migration-0.21.md`

**Exit criterion:** every documented command, flag, exit code, and schema is covered by a test
that fails on drift.

### Review backlog (demand-ordered)

Extensions to the shipped review engine, pulled in when real boards need them:

- [ ] More domain families: RF, Ethernet, HDMI, memory, BMS, motor (each its own detector module).
- [ ] PDN impedance / anti-resonance and plane-void EMC rules (need zone polygons / SPICE).
- [ ] Lifecycle/obsolescence audit via optional DigiKey/Mouser drivers (`jlc bom` covers LCSC today).
- [ ] Additional signal/validation rules: SPI CS counts, UART voltage-domain pairing, full
      power-sequencing analysis. (Fuse sizing + reverse-polarity shipped in 0.14.0 as
      `signal.power_protect` — `calc fuse-derating`-backed sizing, entry-chain walk,
      calibrated on the `power_entry` corpus board.)

### Design-integrity backlog

Extensions to the shipped contract / `verify` / `library` / `fab` / `release preflight` suite
([docs/design-integrity.md](docs/design-integrity.md)).

Later, demand-driven:

- [ ] Declarative placement-distance rules.
- [ ] Remaining `.PcbLib` primitives: text, fills, regions, bodies, 3D references.
- [ ] ENIG exposed-copper and V-cut cost estimation (these stay order-manifest review items).
- [ ] Bezier `gr_curve` outlines, and slots routed on `Edge.Cuts`, in `fab check`.
- [ ] Rail current budgets.
- [ ] Per-symbol baseline hashing.
- [ ] A `.kicad_dru` generator.
- [ ] `project set --text-var` and `project netclass`.
- [ ] Tree-aware power-symbol (`#PWR`) reference allocation across sheets.
- [ ] A `unit` selector for `set_component_transform` on multi-unit parts.
- [ ] KiCad title block in `read --json`.
- [ ] `#FLG` references for `place_pwr_flag`.

### Optional track — Altium interop (demand-driven, currently frozen)

These items were milestone-critical under the old "bridge" positioning; after the KiCad-first
repositioning they proceed only if real usage pulls them:

- [ ] Binary `.SchLib` symbol decoder (pins + basic graphics) so vendor libraries read instead of
      exiting 5 (L) — prerequisite for offline `.SchLib → .kicad_sym` conversion with a fidelity gate (L)
- [ ] `.PcbDoc` remaining binary sections: fills/regions/texts/polygons (L)
- [ ] Altium `Bus`/`BusEntry` records into `netbuild` (M)
- [ ] Real-AD-scale validation of sheet-entry positions in multi-sheet `.PrjPcb` reads (M)

### Deferred by decision

- **Altium live bridge** (2026-07) — **shelved indefinitely, then deleted (0.17)**. The Windows
  scaffold (`drivers/altium_live/`) and its tests are gone; Altium is import-only and KiCad is the
  sole development line. Reviving it would start from scratch, by decision.
- **MCP server** (`akcli mcp`) — the plain CLI + plugin skills serve agents today; revisit on demand.
- **GitHub Action** (2026-07) — a copyable workflow recipe ships in
  [docs/recipes/](docs/recipes/README.md); a packaged Marketplace Action stays deferred.
- **`view` waveform panel** (2026-07) — sim runs and assertions work headless; the dashboard
  visualization is deferred until interactive waveform inspection is actually needed.

## Theme tracks

| Track | ≤ 0.9 | 0.10–0.14 | 0.15–0.23 | v1.0 / optional |
|---|---|---|---|---|
| **KiCad authoring & safety** | `arrange --groups`, `move_component` carry, `check-lock`, PreToolUse hook, honest flags | functional groups, relative placement, `route_net`, 2D group packing, `--propose-labels` repair loop | content-seeded identity, writable child sheets, tree-scoped net gate, multi-sheet transactions, per-instance refs, `ops from-intent`, `project new`/`init`, `add_sheet` `create`, net-diff `MOVE` | contract freeze |
| **Verification & checks** | `findings.schema.json`, sch↔PCB `verify`, diff/pinmap schemas, ERC matrix, diff-pairs, golden corpus | BOM semantics and lockfiles, `check --intent` via `[paths]`, `group_clearance` | `check --integrity`, pin-level `net --at`/`--why`, `NC_PIN_CONNECTED`, `akcli gate`, `project check`, eeschema net naming, tree-scoped `check --integrity`, `release preflight --drc`, library baseline, `verify --library`, `fab check` fidelity, PR-gate workflow recipe | frozen contracts in CI; packaged Marketplace Action (deferred) |
| **Design review** | full engine M1–M9 (signal/validation/pcb/emc/domain/gerber, facts, propose/diff/tree, validate, `--review-policy`) | `signal.power_protect` | DNP-aware rules, `REVIEW_FACTS_UNMATCHED`, `review drift` | more domain families, PDN/EMC depth, lifecycle drivers |
| **Simulation** | behavioral models, solver-trap diagnostics, review testbenches | — | refusal codes for guessed values, node-count and suspicious-zero guards, `release preflight --sim` | waveform panel (deferred) |
| **Review UX** | `review tree`, markdown/SARIF reports, stdlib SVG render, `akcli doc` pinout book (0.12) | faithful symbol artwork, `view` auto-discovery | `render` on the full standard library | — |
| **Parts & manufacturing** | facts store from `jlc datasheet`, `/circuit-parts` | JLC-EDA order CSV, BOM lockfiles, multi-board carts, `parts_dir` | PyPI distribution, `library new-symbol`/`lint-symbols`, `jlc search --category`, bundled fab profiles by id | design-integrity backlog |
| **Altium import** | `.PcbLib` reading, skipped-record histogram | — | live bridge deleted (0.17), import-only, `.PcbLib` track/arc import with `--layer-map` (0.23) | frozen optional track (SchLib decoder, PcbDoc sections, Bus/BusEntry) |

## Non-goals

- **Altium writing, offline or live.** akcli never modifies a `.SchDoc`/`.SchLib`/`.PcbDoc`, and no
   longer ships any path into a running Altium Designer (scaffold deleted in 0.17).
- **Symmetric Altium↔KiCad conversion.** Altium is an import source. Library-level conversion with
  a fidelity gate stays in the optional track; "convert my whole board pixel-perfect" is out.
- **Replacing full EDA tools.** No interactive editor, no autorouter, no autolayout — akcli reads,
  checks, reviews, simulates, and makes surgical, verifiable edits; KiCad remains the design environment.
- **A compliance predictor.** The EMC review is a pre-compliance *risk* analyzer that states its
  assumptions; only a calibrated measurement in an accredited lab establishes regulatory compliance.
- **Pixel-perfect visual fidelity.** The SVG renderer targets *reviewable*, connectivity-true
  drawings: it draws the real symbol artwork where the source carries it (KiCad `lib_symbols`),
  but does not reproduce either tool's canvas (fonts, exact text metrics, sheet decorations).
- **Becoming a dependency-heavy platform.** No third-party runtime packages, no always-on network
  features. `akcli jlc` stays the only networked surface.
