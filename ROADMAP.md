# Roadmap

`akcli` is an **AI-native KiCad design agent**: an LLM (or a plain CI pipeline) authors and edits
`.kicad_sch` from a versioned JSON op-list behind net-diff safety rails, verifies the result with
checks it can gate on, simulates it on KiCad's bundled ngspice, sources real parts, and imports
Altium `.SchDoc`/`.SchLib`/`.PcbDoc` into the same normalized model — all with zero dependencies
and no EDA install. Every output is typed, versioned, and machine-checkable, because the primary
user is an agent shelling out from a pipeline.

KiCad is the writable target. Altium is an **import source** (plus an optional, experimental
Windows live bridge into a running Altium instance) — not a symmetric conversion peer. That
repositioning (2026-07) reshaped this roadmap: the Altium-interop items that earlier milestones
treated as release-critical now live in a demand-driven optional track.

## Where we are (v0.7.0)

Shipped and working today (details per release in [CHANGELOG.md](CHANGELOG.md)):

- **KiCad authoring:** `plan`/`draw` from a `protocol_version 1` op-list — **18 ops + 9 macros**
  (incl. hierarchical `add_sheet`, `rename_net`, cascade delete, multi-unit placement, `mid()`
  anchors), `akcli new` blank-sheet bootstrap, deterministic UUIDv5 idempotency, atomic apply with a
  rotated backup stack (`undo --list`/`--steps`), a pure-Python connectivity gate, a before/after
  **net-membership diff** on every run, and `--apply --strict-nets` refusing named-net
  splits/merges. `relink-symbols` refreshes stale embedded libraries behind a net-equivalence gate.
- **Verification:** ERC-lite / power / BOM / nets / geometry / layout / libsync checks,
  **design-intent assertions** (`nets --intent-snapshot` → `check --intent`, per-net modes +
  wildcards), checker-agnostic `[[waiver]]` config + `--fail-on`, SARIF/JUnit output, structured
  `pos`/`anchors` on findings. Net inference is **arbitrated against `kicad-cli`'s own netlister**
  (a standing parity harness incl. rotation/mirror transforms, label scoping, buses, hierarchy).
- **Design integrity** (post-0.7.0): design **contracts** (`check --contract` — require/forbid
  pin-net & pin-pair topology, component values, NC pins, owned/expiring exceptions), schematic ↔
  PCB **equivalence** (`verify sch board.kicad_pcb` — pad-net partition, refdes, footprint), the
  **`library`** workspace (audit/repair/import-altium — fixes the footprint-nickname & 3D-path
  traps, dry-run→apply), versioned **`fab`** profiles (`fab check`/`explain` — free-via envelope,
  tenting, via-in-pad, cost thresholds, order manifest), and a **`release preflight`** gate emitting
  a traceable manifest. Guide: [docs/design-integrity.md](docs/design-integrity.md).
- **Simulation:** `akcli sim` — schematic → SPICE deck → KiCad's bundled libngspice in a
  crash-isolated, timeout-killed child; `sim.json` assertions (two-sided bounds, multi-analysis),
  `--sweep` corner matrices, `--deck-only` engine-free mode, floating-node detection with
  auto-`rshunt`, `fit-diode` datasheet fits written back to KiCad-native `Sim.*` fields.
- **Parts & manufacturing:** `jlc search`/`show`/`add` (in-process LCSC → KiCad conversion),
  `jlc bom` purchasability with qty tier pricing + JLCPCB order-CSV export + confidence-gated
  `--fix`, `jlc datasheet` PDF resolution/fetch (whole-BOM batch, `--resolve-mpn`).
- **Calculators:** `akcli calc` — 60 standards-cited engineering calculators, engineering-notation
  inputs, `--ops` bridge into op-lists.
- **Readers:** KiCad 7–10 S-expression (bounded, non-recursive, hierarchical) incl. **deep
  `.kicad_pcb`** (pad-net bindings, tracks/vias/zones, board setup); Altium binary `.SchDoc`
  (multi-sheet + `.PrjPcb`), text-record `.SchLib`, `.PcbDoc` ASCII sections **plus binary copper**
  (`Tracks6`/`Vias6`/`Arcs6`/`Pads6`, cross-validated against KiCad's importer), and **`.PcbLib`
  footprint libraries** → `FootprintDef`/`FootprintPad` (also `.kicad_mod`/`.pretty`).
- **Agent surface:** Claude Code / Codex plugin (`akcli`), 9 skills, 4 slash commands, `akcli view`
  dashboard (hub + calc + live watch with SSE, ERC markers, lint overlay, BOM panel), stable exit
  codes 0–7, `schema_version`-stamped JSON.
- **Quality gates:** ~2 000 tests (parser fuzzing, round-trip netlist properties, live ngspice in
  CI, Windows/macOS/Linux × Python 3.11–3.14), ruff + mypy (parts/ + calc/), a
  **docs-conformance gate** (every documented command line and count claim is executed/asserted in
  CI), wheel-install smoke, tag-driven GitHub Releases.

Honest limitations:

- **Binary `.SchLib` symbol records are refused loudly** (exit 5); only text-record libraries read.
- **`.PcbDoc` `Fills6`/`Regions6`/`Texts6`/`Polygons6` are skipped**, not parsed.
- **The Altium live bridge is an unvalidated scaffold** (Windows + Altium 22+; no CLI entry point,
  no CJK text, parameters/footprints not applied). It is now an *optional track*, not a milestone.
- **ERC is ERC-lite:** no full N×N pin-type conflict matrix yet; power checks are net-name +
  power-port based by design.
- **`check`/`diff`/`pinmap` findings have no published JSON Schema** (exports and op-lists do);
  `net <file> NAME` / `component <file> REF` misses exit 0 with a stderr note only.
- **Not on PyPI — by decision** (2026-07): distribution is GitHub Releases; the release workflow
  already supports PyPI trusted publishing whenever that decision changes.
- **MCP server: deferred by decision** — agents drive the plain CLI today.

## Guiding principles

1. **KiCad is the writable target; Altium files are never modified.** No code path writes an
   Altium file on disk. The optional live bridge drives a *running* Altium Designer instead.
2. **Verify everything.** Dry-run by default, `--apply` is explicit and atomic, every write is
   re-read, connectivity-gated, and net-diffed; a "0 findings" report always carries its metadata
   caveats. New write capabilities land together with their verification step — and where external
   ground truth exists (`kicad-cli`, ngspice), akcli's own engines are arbitrated against it.
3. **Zero runtime dependencies.** Python ≥ 3.11 stdlib only. Network code stays isolated under
   `akcli jlc`; `kicad-cli`/libngspice remain optional, discovered, and advisory.
4. **Docs that cannot drift.** Every documented command, flag, and count is exercised against the
   real CLI in CI (the docs-conformance gate). Agent-facing contracts (`schema_version`,
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
| v0.7.0 | **Identity release:** project renamed to `akcli` (KiCad-first repositioning), `akcli doctor` + akcli-setup skill, `akcli-` prefix on all 9 skills, JLCPCB manufacturing-handoff docs, one kicad-cli discovery ladder, docs gate widened to INSTALL/ROADMAP, README restructured KiCad-first |
| (pending) | Rename to `akcli` + KiCad-first repositioning; `sim/builtin.lib` packaging fix |

## Milestones ahead

### v0.8 — Agent contract completeness

Goal: close the remaining gaps between "an agent can drive it" and "an agent can drive it blind" —
every consumable output typed, every miss machine-detectable.

- [ ] Publish `findings.schema.json`, `diff.schema.json`, `pinmap.schema.json`, and a draw-result
      schema, `schema_version`-stamped and mirrored into the package like the existing schemas (M)
- [ ] Machine-detectable misses: `found: false` + distinct exit code for `net <file> NAME` and
      `component <file> REF`; frozen `BRIDGE_BUSY`/`BRIDGE_TIMEOUT` codes (S)
- [ ] `/circuit-parts` slash command wiring `jlc search → show → add → plan → draw` into one
      documented flow (S)
- [ ] PreToolUse hook in the plugin: run `validate_oplist` before any `draw`; warn on `--apply`
      without a preceding `plan` (S)
- [ ] Honest flags: `--no-erc` to skip the advisory `kicad-cli` run; machine-readable remediation
      hints on `ERROR:` lines (S)

**Exit criterion:** an agent can validate every `--json` output against a shipped schema and branch
on exit/error codes instead of scraping stderr prose.

### v0.9 — Deeper verification

Goal: `check` grows from ERC-lite toward a tunable rule engine, and verification reaches across
artifacts.

- [ ] Full ERC pin-type conflict matrix (KiCad-style N×N, unconnected `POWER_IN`, open-collector
      mixes) behind the existing typed-pins confidence demotion (M)
- [x] Schematic-vs-PCB sync check — shipped as `akcli verify sch.kicad_sch board.kicad_pcb`:
      pad-level net partition, refdes presence, footprint assignment (M)
- [ ] Differential-pair / bus continuity checks (`_P`/`_N`, `D+`/`D-`, `D0..D7`) over the existing
      net model, configurable via `[check]` (M)
- [ ] Golden-file regression corpus: frozen `check`/`net`/`diff --json` snapshots over real boards,
      schema-validated in CI (M)
- [ ] GitHub Action: run `check` on changed `.kicad_sch`/`.SchDoc`, `diff` against the base ref,
      post SARIF annotations (M)
- [ ] Sim deepening: behavioral model library growth (op-amps, MOSFETs), `SIM_UNDRIVEN_RAIL`-class
      diagnostics expansion, waveform panel in the `view` dashboard (M)

**Exit criterion:** a schematic PR can be gated end-to-end (check + diff + intent + sim
assertions), and a false finding is tuned or waived in config rather than ignored.

### v0.10 — See the circuit

Goal: humans reviewing agent work get visuals and documents, not just JSON.

- [ ] Pure-stdlib SVG schematic rendering from the normalized model (components, pin tips, wires,
      junctions, labels) for install-free before/after review — today `view live` renders through
      the optional `kicad-cli` (L)
- [ ] `akcli doc <file> -o book.md`: pinout book composing per-IC/connector pin tables, rail
      summary, and BOM (M)

**Exit criterion:** `/circuit-draw` can show a human what it placed without any EDA install, and a
design review can start from a generated pinout book.

### v1.0 — Contracts frozen, released

Goal: the public surface is stable enough to promise.

- [ ] Contract freeze audit: `schema_version`/`protocol_version` review, deprecation policy
      documented; extend the docs-conformance gate to the frozen contracts (S)
- [ ] First PyPI release (`pip install akcli`) — **gated on reversing the standing "GitHub
      Releases only" decision**; the tag-driven workflow already supports trusted publishing (S)

**Exit criterion:** every documented command, flag, exit code, and schema is covered by a test
that fails on drift — and installation is a one-liner on the chosen channel.

### Optional track — Altium interop (demand-driven, currently frozen)

These items were milestone-critical under the old "bridge" positioning; after the KiCad-first
repositioning they proceed only if real usage pulls them:

- [ ] Binary `.SchLib` symbol decoder (pins + basic graphics) so vendor libraries read instead of
      exiting 5 (L) — prerequisite for offline `.SchLib → .kicad_sym` conversion with a fidelity gate (L)
- [ ] `.PcbDoc` remaining binary sections: fills/regions/texts/polygons (L)
- [ ] Altium `Bus`/`BusEntry` records into `netbuild` (M)
- [ ] Live bridge graduation: `draw --live` CLI wiring, DelphiScript validation on Windows +
      Altium 22+, automatic post-apply netlist re-export + diff (L)
- [ ] Real-AD-scale validation of sheet-entry positions in multi-sheet `.PrjPcb` reads (M)

### Deferred by decision

- **MCP server** (`akcli mcp`) — the plain CLI + plugin skills serve agents today; revisit on demand.
- **PyPI publishing** — see v1.0; the mechanism is built, the decision is deliberate.

## Theme tracks

| Track | v0.8 | v0.9–v0.10 | v1.0 / optional |
|---|---|---|---|
| **KiCad authoring & safety** | PreToolUse hook, honest flags | — | contract freeze |
| **Verification & checks** | findings/diff/pinmap schemas | ERC matrix, sch-vs-PCB, diff-pairs, golden corpus, GitHub Action | frozen contracts in CI |
| **Simulation** | — | behavioral models, waveform panel | — |
| **Review UX** | — | stdlib SVG render, pinout book | — |
| **Parts & manufacturing** | `/circuit-parts` command | — | — |
| **Altium import** | — | — | optional track (SchLib decoder, PcbDoc sections, live bridge) |

## Non-goals

- **Offline Altium writing.** akcli never modifies a `.SchDoc`/`.SchLib`/`.PcbDoc` on disk. Altium
  writes, if ever, go exclusively through the live bridge into a running Altium Designer.
- **Symmetric Altium↔KiCad conversion.** Altium is an import source. Library-level conversion with
  a fidelity gate stays in the optional track; "convert my whole board pixel-perfect" is out.
- **Replacing full EDA tools.** No interactive editor, no autorouter, no autolayout — akcli reads,
  checks, simulates, and makes surgical, verifiable edits; KiCad remains the design environment.
- **Pixel-perfect visual fidelity.** The SVG renderer (v0.10) targets *reviewable*,
  connectivity-true drawings, not a reproduction of either tool's canvas.
- **Becoming a dependency-heavy platform.** No third-party runtime packages, no always-on network
  features. `akcli jlc` stays the only networked surface.
