# Roadmap

`altium-kicad-cli` (`akcli`) aims to be the **install-free, agent-native bridge between Altium and
KiCad**: read anything either tool produces (binary `.SchDoc`/`.SchLib`/`.PcbDoc`, S-expression
`.kicad_sch`/`.kicad_sym`/`.kicad_pcb`) with zero dependencies, verify everything with checks an
agent can trust and a CI can gate on, draw KiCad schematics deterministically from a versioned
op-list, and drive a running Altium Designer live on Windows — while never modifying an Altium file
offline. Every output is typed, versioned, and machine-checkable, because the primary user is an AI
coding agent shelling out from a pipeline.

## Where we are (v0.1.0)

Shipped and working today:

- **Readers:** Altium `.SchDoc` (components, pins, wires, labels, power ports, footprint chain),
  text-record `.SchLib`, ASCII `.PcbDoc` sections; KiCad 7/8 `.kicad_sch`/`.kicad_sym`/`.kicad_pcb`
  via a lossless, bounded, non-recursive S-expression parser (byte-identity round-trip verified).
- **Analysis:** shared net inference (`netbuild`), `check` (ERC-lite / power / BOM), membership-based
  `diff`, `pinmap` with DTS / pinout.md expected tables, `export` (protel / kicad / csv).
- **KiCad writing:** `plan` / `draw` from a `protocol_version 1` op-list — 13 ops, atomic apply with
  backup, deterministic UUIDv5 idempotency, pure-Python connectivity gate, advisory `kicad-cli` ERC.
- **Parts:** `jlc search` / `show` / `add` (LCSC → KiCad/Altium libraries via external `nlbn`/`npnp`).
- **Agent surface:** Claude Code plugin (circuit-design skill + 4 slash commands), stable exit codes
  0–7, `stdout`-data/`stderr`-logs discipline, `schema_version`-stamped JSON.

Honest limitations:

- **Flat sheets only**, both directions: the readers return `sheets=[]` for hierarchical Altium and
  KiCad projects, and the writer rejects any non-root `instances_path` (`HIERARCHICAL_UNSUPPORTED`).
- **Binary Altium payloads are not parsed:** binary `.SchLib` symbol records are refused loudly
  (exit 5), but binary `.PcbDoc` sections (`Pads6`, `Tracks6`, `Vias6`, …) are silently *skipped*
  by `read` — the board opens with copper geometry omitted.
- **The Altium live driver is a preview:** the Python bridge is tested, but the DelphiScript half is
  an unvalidated scaffold (no CJK text, parameters/footprints not applied, no CLI entry point), and
  "ok" means *ops placed*, not *electrically verified*.
- **Op vocabulary is additive-only:** no delete, no move-to-x/y, always `(unit 1)`, no sheet ops.
- **Agents parse some JSON blind:** `check`/`diff`/`pinmap` output has no published schema; `net`/
  `component` misses exit 0 with only a stderr note; `docs/cli-reference.md` has drifted from the
  actual `plan`/`draw`/`jlc` surface.
- Not yet tagged or published to PyPI; install from source.

## Guiding principles

1. **Read-only Altium offline.** No code path ever writes an Altium file on disk. Altium writes
   happen only through the live bridge into a running Altium Designer, one undo transaction per
   op-list, with the user's project snapshotted first.
2. **Verify everything.** Dry-run by default, `--apply` is explicit and atomic, every write is
   re-read and connectivity-gated, and a "0 findings" report always carries its metadata caveats.
   New write capabilities land together with their verification step.
3. **Zero runtime dependencies.** Python ≥ 3.11 stdlib only. Network code stays isolated under
   `akcli jlc`; external binaries (`nlbn`, `npnp`, `kicad-cli`) remain optional, pinned, and advisory.
4. **Agent-first.** Stable exit codes, one-line `ERROR: CODE:` failures, `schema_version`/
   `protocol_version` contracts, published JSON Schemas, and docs that never drift from the code.

## Milestones

### Now

#### v0.2 — Agent contract hardening

Goal: every output an agent consumes is typed, discoverable, and honest — close the small sharp
edges before building on them.

- [ ] Fix `docs/cli-reference.md` drift (`plan`/`draw` take the target positionally with `--ops`;
      document the whole `jlc` family, `read --md`, `--symbols`) and reference
      `schemas/ops.capabilities.json` from `SKILL.md` (S)
- [ ] Machine-detectable misses: `found: false` / distinct exit for `net <file> NAME` and
      `component <file> REF` lookups; frozen `BRIDGE_BUSY` / `BRIDGE_TIMEOUT` error codes (S)
- [ ] Publish `findings.schema.json`, `diff.schema.json`, `pinmap.schema.json`, and a draw-result
      schema, `schema_version`-stamped like the existing exports (M)
- [x] CFBF DIFAT spillover support (> 109 FAT sectors) so large `.PcbDoc`/`.SchDoc` containers open (S)
- [ ] Multi-unit symbol placement: `unit` field on `place_component` (drop the hard-coded
      `(unit 1)`) (S)
- [ ] `akcli expected <file.dts|pinout.md> [-o expected.json]` subcommand wrapping the DTS /
      pinout.md adapters for the `pinmap --expected` pipeline (S)
- [ ] `/circuit-parts` slash command wiring `jlc search → show → add --to kicad --place → plan →
      draw` into one documented flow (S)
- [ ] Honest flags and hints: make `draw --dry-run` explicit, add `--no-erc` to skip the advisory
      `kicad-cli` run, append machine-readable remediation hints to `ERROR:` lines (S)
- [ ] KiCad 9 read/write fixtures with byte-identity round-trip; adapt `kicad-cli` version gates (M)

**Exit criterion:** an agent can validate every `--json` output against a shipped schema, branch on
exit/error codes instead of scraping stderr prose, and follow `docs/cli-reference.md` verbatim
without hitting a nonexistent flag.

#### v0.3 — Whole projects, deeper checks

Goal: real multi-sheet designs produce correct netlists on both sides, and `check` grows from
ERC-lite into a tunable, CI-consumable rule engine.

- [ ] Hierarchical KiCad **read**: recurse `(sheet)` nodes into a full multi-sheet netlist with
      correct hierarchical-label scoping (M)
- [ ] Altium multi-sheet: RECORD 15 `SheetSymbol` handler plus a `.PrjPcb` project reader (sheet
      list, `PowerPortNamesTakePriority` and friends) (L)
- [ ] Binary `.SchLib` symbol decoder (framed records with non-zero flag byte: pins + basic
      graphics) so real vendor libraries read instead of exiting 5 (L)
- [ ] Full ERC pin-type conflict matrix (KiCad-style N×N, unconnected `POWER_IN`, open-collector
      mixes) behind the existing 20 %-typed-pins confidence demotion (M)
- [ ] `[check]` section in `altium-kicad-cli.toml`: per-rule enable/severity, rail current capacity,
      decoupling requirements, component+pin-level waivers (M)
- [ ] `check --format sarif|junit` output for GitHub code scanning and CI test reporters, built on
      the v0.2 findings schema (M)
- [ ] Golden-file regression corpus: frozen `check`/`net`/`diff --json` snapshots over real Altium +
      KiCad boards, schema-validated in CI (M)

**Exit criterion:** a hierarchical KiCad or Altium project yields the same net membership akcli
would compute if the design were flattened by hand, and a false ERC finding can be tuned or waived
in config rather than ignored.

### Next

#### v0.4 — Editing power and op-list authoring

Goal: agents can *fix* a schematic, not just add to it — and authoring an op-list stops being
hand-computed mil arithmetic.

- [ ] Editing ops: `delete_component` / `remove_wire` / `remove_label` and a real move (x/y) op,
      reusing the deterministic-UUID replacement machinery (M)
- [ ] Op-list authoring kit: the missing `docs/op-list-authoring.md`, `akcli ops template <op>`
      scaffolder driven by `ops.schema.json` + `ops.capabilities.json`, next-free-grid-slot
      placement helper (M)
- [ ] PreToolUse hook in the plugin: run `validate_oplist` before any `draw` invocation and warn on
      `--apply` without a preceding `plan` (S)
- [ ] Post-apply connectivity **delta** verification: compute the expected net-membership delta from
      the op-list, diff before/after reads, exit 6 on unexpected deltas (M)

**Exit criterion:** an agent can misplace a component, then correct it with move/delete ops through
`plan → draw --apply`, and the apply fails loudly if the write changed any net it should not have.

#### v0.5 — Altium alive

Goal: the Windows live driver graduates from scaffold to supported write path, with the same
verify-everything posture the KiCad writer already has.

- [ ] Wire the bridge into the CLI: `akcli draw <file>.SchDoc --live` / `plan --live`, using the
      frozen bridge error codes from v0.2 (M)
- [ ] Validate and deepen the DelphiScript half on Windows + Altium 22+: CJK/`\uXXXX` text, real
      footprint/custom parameter setting, Port promotion for net-label scopes, wire-vertex fix (L)
- [ ] Automatic post-apply live verification: re-export the Altium netlist (`export --format
      protel` semantics) and stable_id-diff it against the intended ops (M)
- [ ] Altium bus support: read `Bus`/`BusEntry` records into `netbuild`; implement `add_bus` /
      `add_bus_entry` in the live driver so `ops.capabilities.json` has no false rows (M)
- [ ] Binary `.PcbDoc` geometry decoders: `Pads6`/`Vias6`/`Tracks6`/`Arcs6` first, then
      fills/regions/polygons, behind the existing safety caps (L)

**Exit criterion:** an op-list placed live into Altium Designer 22+ from `akcli draw --live` is
automatically re-exported and net-diffed, and a bus-heavy `.SchDoc` reads with correct connectivity.

#### v0.6 — Cross-validation and ecosystem

Goal: akcli's own analysis is adversarially tested and cross-checked against ground truth, and the
tool plugs natively into CI, MCP, and costing workflows.

- [ ] `check --xnet`: export `kicad-cli`'s netlist, canonicalize to (designator, pin) membership,
      diff against `netbuild`'s stable_id sets, report divergences as findings (M)
- [ ] Parser fuzzing harness (CFBF, `altium_records`, sexpr) seeded from `tests/fixtures`, oracle
      "AkcliError or clean parse, bounded time/memory", scheduled CI job (M)
- [ ] Schematic-vs-PCB sync check: `akcli diff sch.kicad_sch board.kicad_pcb` comparing net
      membership, refdes presence, footprint assignment (M)
- [ ] Differential-pair and bus continuity checks (`_P`/`_N`, `D+`/`D-`, `D0..D7`) over the existing
      net model, configurable via `[check]` (M)
- [ ] `akcli mcp`: stdio MCP server exposing read/net/check/diff/pinmap/plan as typed tools (draw
      excluded or gated, mirroring `/circuit-draw`'s posture) (L)
- [ ] GitHub Action: run `check` on changed `.kicad_sch`/`.SchDoc`, `diff` against the base ref,
      post SARIF annotations surfacing the four metadata caveats (M)
- [ ] `akcli bom --cost`: join the BOM against JLC/LCSC pricing (qty-aware tiers, Basic/Preferred
      flags, unmatched-part findings) (M)
- [ ] `export --format spice`: R/C/L cards from BOM values plus `.SUBCKT` stubs with explicit
      unmapped-part caveats (M)

**Exit criterion:** a schematic PR can be gated end-to-end by the Action (check + diff + pinmap),
and akcli's net inference is continuously proven against `kicad-cli` and fuzzed inputs.

### Later

#### v0.7 — Hierarchy everywhere, library bridge

Goal: writing is no longer flat-only, and users' existing Altium libraries convert offline.

- [ ] Hierarchical KiCad **write**: `add_sheet` / `add_sheet_pin` ops and non-root `instances_path`,
      verified by the (v0.3) hierarchical reader (L)
- [ ] Offline `.SchLib → .kicad_sym` conversion with a round-trip fidelity gate (re-read model
      equivalence: pins, types, geometry within tolerance) — built on the v0.3 binary SchLib
      decoder, no network, no external converters (L)

**Exit criterion:** an agent can create a sub-sheet, place into it, and convert a vendor `.SchLib`
to KiCad — all offline, all re-read-verified.

#### v0.8 — See the circuit

Goal: humans reviewing agent work get visuals and documents, not just JSON.

- [ ] Pure-stdlib SVG/PNG schematic rendering from the normalized model (components, pin tips,
      wires, junctions, labels) for before/after review of `draw` (L)
- [ ] `akcli doc <file> -o book.md`: pinout book composing per-IC/connector pin tables, rail
      summary, and BOM (embedding symbols once SVG lands) (M)
- [ ] `akcli watch`: re-run `check` and diff against the previous state on file change, one JSON
      event per change, for live agent/human co-editing (M)

**Exit criterion:** `/circuit-draw` can show a human what it placed, and a design review can start
from a generated pinout book instead of raw reads.

#### v0.9 — Layout-aware verification

Goal: verification extends from the schematic to copper.

- [ ] Geometry DRC for `.kicad_pcb` (net-aware clearance, track width, annular ring) driven by
      `[check]`, KiCad-only until the Altium binary decoders mature (L)

**Exit criterion:** akcli flags a clearance violation in a `.kicad_pcb` that KiCad's own DRC also
flags, with zero false positives on the golden corpus boards.

#### v1.0 — Contracts frozen, released

Goal: the public surface is stable enough to promise.

- [ ] First tagged release published to PyPI (`pip install altium-kicad-cli`), plugin manifests
      versioned per the CHANGELOG policy (S)
- [ ] Contract freeze audit: `schema_version` / `protocol_version` review, doc-vs-code parity check
      in CI, deprecation policy documented (S)

**Exit criterion:** `pip install altium-kicad-cli && akcli --version` works, and every documented
command, flag, exit code, and schema is covered by a test that fails on drift.

## Theme tracks

| Track | v0.2–v0.3 (now) | v0.4–v0.6 (next) | v0.7–v1.0 (later) |
|---|---|---|---|
| **Altium depth** | DIFAT spillover; binary `.SchLib` decoder; RECORD 15 + `.PrjPcb` multi-sheet | `draw --live` CLI; Windows DelphiScript validation; live post-apply verify; bus read/write; binary `.PcbDoc` geometry | `.SchLib → .kicad_sym` offline conversion |
| **KiCad depth** | Multi-unit `unit` field; KiCad 9 fixtures; hierarchical read | delete/move ops; op-list authoring kit | hierarchical write (`add_sheet`); SVG rendering |
| **Checks & verification** | ERC pin-type matrix; `[check]` config; golden-file corpus | post-apply delta verify; `--xnet` cross-validation; fuzzing; sch-vs-PCB sync; diff-pair/bus checks | `.kicad_pcb` geometry DRC |
| **AI-agent experience** | not-found + bridge error codes; findings/diff/pinmap schemas; doc-drift fix; `akcli expected`; honest `--dry-run`/`--no-erc`/hints | PreToolUse op-list hook; `akcli mcp` server | `akcli watch`; contract freeze audit |
| **Parts & manufacturing** | `/circuit-parts` command | `bom --cost` JLC pricing; SPICE export | pinout book (`akcli doc`) |
| **Ecosystem & CI** | SARIF/JUnit output | GitHub Action for schematic PRs | PyPI release + tagged v1.0 |

## Non-goals

- **Offline Altium writing.** akcli will never modify a `.SchDoc`/`.SchLib`/`.PcbDoc` on disk.
  Altium writes go exclusively through the live bridge into a running Altium Designer, where the
  user's own tool owns the file format and the undo stack.
- **Replacing full EDA tools.** No interactive editor, no autorouter, no full autolayout — the
  suggest-position helper stops at "next free grid slot". akcli reads, checks, and makes surgical,
  verifiable edits; Altium and KiCad remain the design environments.
- **Whole-design Altium-to-KiCad conversion.** Library-level conversion (v0.7) with a fidelity gate
  is in scope; "convert my board" pixel-perfect translation of complete projects is not, and both
  `plugin.json` and `SKILL.md` will keep saying so.
- **Pixel-perfect visual fidelity.** The SVG renderer (v0.8) targets *reviewable* connectivity-true
  drawings, not a reproduction of either tool's canvas.
- **Becoming a dependency-heavy platform.** No third-party runtime packages, no vendored converters,
  no always-on network features. `akcli jlc` stays the only networked surface.
