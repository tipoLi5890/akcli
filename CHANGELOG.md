# Changelog

All notable changes to `altium-kicad-cli` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning policy

`altium-kicad-cli` ships **three** version numbers; this section is their contract.

- **Package version (SemVer `MAJOR.MINOR.PATCH`).** The single source of truth is
  `pyproject.toml`; `tools/sync_version.py` stamps it into `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json`, and CI fails on drift.
  SemVer rules:
  - **MAJOR** — backwards-incompatible change to the public CLI surface, the normalized data model, or
    the on-disk JSON exports.
  - **MINOR** — backwards-compatible new subcommands, flags, checks, readers, or ops.
  - **PATCH** — backwards-compatible bug fixes and internal changes.

- **`schema_version`** (stamped on every `Schematic`/`Pcb`/`Library` JSON export; currently `"1.0"`).
  Bumped independently of the package version. A **minor** schema bump only adds optional fields
  (consumers must ignore unknown keys); a **major** schema bump may remove or rename fields and
  coincides with a package MAJOR bump.

- **`protocol_version`** (integer; currently `1`) governs the op-list document and the Windows live
  bridge. It is bumped **only** on a breaking change to op shapes, the result object, or the bridge
  handshake. Executors and the bridge **reject a higher major `protocol_version`** with
  `ERROR: PROTOCOL_MISMATCH` rather than guessing. Adding a new optional op or optional op field does
  **not** bump `protocol_version`.

When in doubt, prefer additive, backwards-compatible changes and leave the version contracts untouched.

## [Unreleased]

### Added
- **20 more calculators (56 total)**, all standards-cited: differential pairs
  (IPC-2141A over Hammerstad–Jensen/Cohn single-ended), `tracktemp` (IPC-2221 solved for ΔT),
  unit conversions (dBm/W/Vrms per IEEE Std 100; mil/mm exact 25.4; oz/µm copper nominal +
  pure-Cu physics), comparator hysteresis analysis+design (TI SLVA954, round-trip-tested),
  RS-485 fail-safe bias (TIA-485-A/SLLA070D), CAN split termination (ISO 11898-2), LDO
  dissipation/dropout/thermal, MOSFET gate drive (TI SLUA618A), current-sense shunts
  (TI SBOA170), Sallen–Key equal-component design (TI SLOA024B), ADC LSB/SNR/settling
  (MT-001), TVS selection (IEC 61000-4-5 surge), fuse derating on the IEC 60127 R10 ladder,
  NTC inrush sizing (TDK/EPCOS guide), L- and PI-section matching (Pozar §5.1/Bowick),
  flyback first-order design (Erickson ch. 6). **IPC-2152 deliberately not faked** —
  chart-based licensed data with no public closed form.
- **`akcli calc` tooling:** `calc batch <file|->` (JSON job list → envelope array, exit 1
  if any job fails), `--md` (markdown result table), and `--ops FILE` — design-type
  calculators emit a schema-valid `place_component` op-list with the computed E-series
  values filled in, validated against `ops.validate_oplist` in tests.
- **`tools/calc-view/`:** local web UI (stdlib server, localhost-only) for all 56
  calculators — grouped sidebar with filter, auto-generated forms, results with units and
  the formal reference, ~45 physical-style SVG illustrations (via/trace cross-sections,
  stackups, LM317/555/I²C/RS-485/CAN/flyback networks; the resistor color-code diagram
  colors its bands from the actual result), pinned/recent calculators, and shareable URLs
  (the hash carries calculator + inputs and re-runs on load).
- **`akcli calc` — 36 offline engineering calculators**, each stamped with its formal
  reference: E-series snap + 2–4-resistor combination search (IEC 60063:2015, tabulated
  E1–E24 + formula E48/E96/E192 with the 9.20 exception), dividers/LED/RC/LC
  (Horowitz & Hill 3rd ed.), LM317/FB regulator networks with exhaustive worst-case corner
  analysis (TI SLVS044Y), IPC-2221B §6.2 track width ↔ current and Table 6-1 clearance
  (incl. >500 V slopes), via R/thermal/ampacity/L/C/rise (Johnson & Graham 1993),
  Onderdonk/Preece fusing, ASTM B258 AWG, microstrip (Hammerstad–Jensen 1980), stripline
  (Cohn 1954 exact via AGM), coax/twin-lead (Pozar), PI/TEE/bridged-TEE attenuators,
  buck/boost stages (TI SLVA477B/372C), NE555 (SLFS022I), op-amp pairs (SLOD006B),
  I²C pull-up window (NXP UM10204 §7.1), crystal caps (ST AN2867), JESD51 thermal, battery
  life, resistor color/SMD/EIA-96 codes (IEC 60062:2016), galvanic pairs (MIL-STD-889C).
  Inputs take engineering notation (`4k7`, `100n`); `--json` returns
  `{calc, inputs, results, reference}`. Numerics cross-validated against KiCad's
  pcb_calculator outputs (independent clean reimplementation — no GPL code) and published
  handbook values in `tests/test_calc.py`. New **`design-calc` skill** teaches agents to
  compute-then-place E-series values instead of guessing.
- **Binary `.PcbDoc` copper decoded:** `Tracks6`/`Vias6`/`Arcs6`/`Pads6` are now parsed
  (new `readers/altium_pcb_bin.py` — packed little-endian records, coordinates in mils,
  native +Y-up frame) and land on the `Pcb` model as `tracks`/`vias`/`arcs`/`pads`;
  `--json` schema is now **1.1**. Layouts were cross-validated item-by-item against KiCad's
  own Altium importer (`pcbnew`) on real boards from KiCad's QA corpus: 778/778 board-level
  copper tracks, 20/20 vias, 236/236 arcs, 48/48 pads (names, sizes, drills, positions exact
  modulo ±3 nm importer rounding); a second board from a different AD version (3661 tracks /
  321 vias / 88 arcs / 468 pads) decodes with zero errors. Unknown record types inside a
  known section fail `ALTIUM_UNSUPPORTED`; truncated records fail `ALTIUM_MALFORMED` —
  nothing is silently skipped anymore. `Fills6`/`Regions6`/`Texts6`/`Polygons6` remain out
  of scope (still skipped, documented).
- **Hierarchical sheets (Altium reader):** a `.SchDoc` root recurses into sheet symbols
  (RECORD 15 + name/file 32/33), each instance in its own geometric namespace, with
  sheet-entry (RECORD 16: `Name`/`Side`/`DistanceFromTop`) ↔ child-PORT pairing per Altium's
  *Automatic* net-identifier scope — ports merge globally only in designs WITHOUT sheet symbols,
  so two children exposing the same port name stay separate; flat designs read exactly as
  before. `.PrjPcb` is accepted as input: akcli finds the top sheet (the one no sheet symbol
  references) and honors `PowerPortNamesTakePriority`. The previous RECORD-16 handling was dead
  code (it read `Text`/`Location` — real entries carry `Name`/`Side`/`DistanceFromTop`).
  Runtime-generated hierarchical fixtures; sheet-entry position scale follows the documented
  convention, real-AD validation flagged as pending.
- **Op-list authoring kit:** `docs/op-list-authoring.md` (envelope, coordinate contract, all
  16 ops with notes, pipeline, idempotency rules) plus `akcli ops list` (vocabulary + required
  fields + executor support) and `akcli ops template <op>` (fill-in JSON skeleton); the in-code
  tables are drift-guarded against `schemas/ops.schema.json` by tests.
- **Autoplace collision avoidance:** visible Reference/Value anchors register in a per-apply
  registry; a new label landing within one label extent of an existing one bumps outward
  deterministically (replays stay byte-identical). Fixes neighboring parts' texts stacking
  (the `+3V3`-on-`C2` case).
- **`check --format sarif|junit`:** SARIF 2.1.0 output for GitHub code scanning (stable
  `partialFingerprints`, schematic path as artifact URI, rule table) and JUnit XML for CI test
  reporters (WARNING+ findings as failed testcases; NOTE/INFO as passed cases with
  `system-out`; clean runs emit one passed case). Lint-style exit semantics unchanged.

### Fixed
- **`calc` output never SI-prefixes non-base units:** values already carrying a prefixed or
  compound unit (mm, °C/W, m², Ω/km) print plain — the clearance table rendered 0.2 mm as
  "200 mmm".

## [0.3.1] - 2026-07-07

### Added
- **`akcli expected` subcommand:** extract an expected pin→signal table from a Zephyr
  devicetree source/overlay (gpio phandles + Nordic `NRF_PSEL` pinctrl) or a markdown pinout
  table, as the JSON `pinmap --expected` consumes. Empty extraction exits `1` (a vacuous
  table must not read as success).
- **CLI-layer offline tests for `jlc add`** (flag validation, exit-code mapping, `--place`
  op-list emission) against the captured EasyEDA fixtures.
- **Altium fixture invariant sweep:** auto-discovering tests over every `.SchDoc` fixture —
  net members must reference real component pins, membership sorted/duplicate-free, reads
  deterministic, CSV/Protel exports agree with the inferred netlist, and the malformed corpus
  fails loudly. New fixtures are swept automatically. (The independent cross-check against
  Altium's own netlist export still requires a real AD install; KiCad's Altium importer is
  GUI-only and cannot be driven headless.)

## [0.3.0] - 2026-07-06

### Added
- **`tools/live-view/`:** a localhost dashboard that watches a `.kicad_sch` while akcli draws
  it — per-step SVG (inline, auto-cropped), KiCad ERC badges, part/net counts, notes, zoom/pan,
  follow-live; optional macOS auto-revert of an open KiCad editor. See its README.
- **New `jlcpcb-capabilities` skill:** manufacturing limits to design against, with
  **嘉立創 (jlc.com) as the primary source** — 1–64 layers, HDI blind/buried vias, 0.1 mm
  microvias, up to 6 oz copper, FPC and 經濟/標準 SMT gates — plus a difference table against
  JLCPCB-international (32 layers, no blind/buried, BGA ≥0.35 mm, ...) and the intl stencil
  specs. Sources + snapshot date stated in the skill; includes apply-while-drawing guidance
  (comfortable defaults vs 極限值, schematic-time package gating, AD/PADS export gotchas).
- **Converted libraries import into Altium Designer natively:** the footprint writer now
  emits the **KiCad 6 dialect** (`(layer)(width)` graphics, version `20211014`) — readable by
  every KiCad from 6 to 10 *and* by Altium Designer's built-in **Import Wizard » KiCad Design
  Files** (whose KiCad support is pinned to 6.0x), which converts the produced
  `.kicad_sym`/`.kicad_mod` to a native `.SchLib`/`.PcbLib`. This replaces the dead
  npnp `--to altium` path with a vendor-supported one.
- **`akcli jlc add` is back — in-process, zero-install:** LCSC → KiCad symbol/footprint/3D
  conversion now runs inside akcli via the vendored MIT conversion core of
  **JLC2KiCadLib** (TousstNicolas; license + provenance in
  `src/altium_kicad_cli/_vendor/jlc2kicadlib/` and `THIRD_PARTY_NOTICES.md`). Upstream's two
  dependencies are deliberately not vendored: `requests` is replaced by a stdlib shim and the
  GPLv3 `KicadModTree` by a clean-room `.kicad_mod` writer that emits the modern
  `(footprint ...)` dialect. `--place` emits a `place_component` op-list as before; no external
  binary and no pip dependency required.

### Removed
- **`akcli jlc add` (external library conversion):** the upstream `nlbn`/`npnp` converter
  repositories are no longer available, so the delegation, the pinned auto-downloader, and the
  `--place` op-list emission were removed. `jlc search`/`jlc show` (and `--easyeda` metadata)
  are unchanged. Symbols/footprints now come from the official KiCad libraries or project
  `.kicad_sym` files.

## [0.2.0] - 2026-07-06

Not yet published to PyPI; install from source (see `INSTALL.md`).

### Added
- **Readers:** Altium `.SchDoc` / `.SchLib` and ASCII `.PcbDoc`; KiCad `.kicad_sch` (v7/v8) via a
  bounded, non-recursive S-expression parser with pin-type resolution from `lib_symbols`.
- **Net inference** (`netbuild`) shared across both formats: same-name merge, junctions/T-junctions,
  No-ERC handling.
- **CLI:** `read`, `net`, `component`, `check` (ERC/power/BOM), `diff` (net-membership), `pinmap`,
  `export`.
- **KiCad write/draw:** `plan` / `draw` from a versioned JSON op-list — atomic, idempotent (UUIDv5),
  connectivity-verified.
- **JLCPCB/LCSC parts:** `jlc search` / `show` / `add` (conversion via external `nlbn` / `npnp`).
- **Claude Code plugin:** circuit-design skill + `circuit-review` / `circuit-pinmap` / `circuit-draw` /
  `circuit-diff` commands; DTS / pinout adapters.
- **Altium live driver (preview):** Python file-based JSON bridge; the Windows DelphiScript half is a
  scaffold pending validation.
- Documentation (`README.md`, `INSTALL.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, `docs/SPEC.md`,
  `docs/cli-reference.md`), reference config, and CI matrix.

- **`BOM_CORRUPT_TEXT` check (NOTE):** components whose value/parameters contain the U+FFFD
  replacement character are surfaced with an aggregated finding instead of silently printing `�`.
  Root-cause analysis on real-world files showed the corruption is baked into the `.SchDoc` at
  export time (a legacy-code-page value pushed through a lossy UTF-8 decode by the authoring tool --
  both the ANSI field and its `%UTF8%` twin carry the damage), so no decoder can recover it; the
  finding says so and points at re-export.

### Added
- **Hierarchical sheets (KiCad reader):** `read`/`net`/`check`/`diff`/`pinmap` on a root
  `.kicad_sch` now recurse into `(sheet ...)` children (paths relative to the parent file,
  cycle- and depth-guarded). Every sheet INSTANCE is its own geometric namespace — a file
  instantiated twice contributes its components once per instance with designators resolved
  from the matching `(instances (path ...))` entry — and connectivity crosses sheets only
  through sheet-pin↔hierarchical-label pairs (strictly parent↔child, never global), global
  labels, and power ports. The writer stays flat-only v1.
- **`delete_component` / `delete_object` / `move_component` ops:** delete removes all placed
  instances of a designator (or one object by uuid) — attached wires are left for the
  connectivity gate to flag, so stale wiring is cleaned up explicitly, and deleting an
  already-absent target is a replay-safe no-op; move repositions one instance (designator +
  optional unit) with its properties travelling along, wires intentionally not stretched.
- **Property autoplace:** placed symbols now get eeschema-style field layout — Reference/Value
  beside a tall (vertical-pin) body or above/below a wide one, `Footprint`/`Datasheet`/
  `Description` created hidden, power symbols with hidden `#PWR` references and the value
  below the anchor. Previously every field rendered at the component origin (the synthesized
  Reference even at absolute 0,0), stacking raw text over the body.
- **Multi-unit placement:** `place_component` takes an optional `"unit": N` — each unit is
  its own placed instance sharing the designator (74xx gate A/B/...). `"REF.PIN"` endpoints
  resolve against the instance whose unit owns the pin; wiring a pin on an **unplaced** unit
  fails loudly with the unit to place, instead of silently snapping to another unit's body.

### Fixed
- **Placed instances expose only their own unit's pins** (reader, writer, verifier): every
  unit of a multi-unit symbol shares local pin geometry, so treating all units' pins as
  present at one instance mapped all four 74xx gates onto one body — `akcli net` merged
  unrelated gate pins into one net while eeschema saw two, and phantom pin points masked
  real dangling wires in the connectivity gate. Instances of one designator now merge into
  a single component on read (no false `BOM_DUPLICATE_DESIGNATOR`).
- **Multi-line/control text is escaped KiCad-style:** `_q` escaped only `\` and `"`, so an
  `add_text` with a newline wrote a file KiCad refused to parse while every akcli gate
  passed (akcli's lexer tolerates a raw newline in a quoted atom; eeschema does not).
  `\n`/`\r`/`\t` are now escaped in all writer quoting helpers.
- **Pin taps now follow eeschema connectivity:** a pin tip touching a wire's **mid-span**
  connects only when a junction marks that point (or at a segment endpoint) — both in net
  inference (`netbuild`) and, constructively, in the writer: `auto_junctions` now also
  considers pins lying on a segment interior, so a placed part tapping a rail gets its
  junction automatically (previously the mid-span-pin rule never fired because candidates
  were wire endpoints only, and `akcli net` claimed connectivity KiCad rejected).
- **Replaying an op-list is byte-identical after ONE apply:** idempotent replay now replaces
  same-uuid nodes **in place** instead of remove-then-append, which migrated every op node to
  the end of the file while auto-junctions stayed put — the first re-apply reordered the
  document and byte-idempotency only converged on the second apply.
- **Large op-lists are no longer quadratic:** each placement re-parsed the whole (growing)
  inline `lib_symbols` cache to resolve its symbol; symbols now resolve once, from just their
  own cached body, memoized per apply run. A 478-placement sheet went from >120 s (timeout)
  to 1.7 s.
- **Duplicate pin numbers across units no longer collide:** multi-unit parts with shared pads
  (e.g. dual DirectFETs — unit A pins 1,2,3 / unit B pins 1,4,5) legitimately repeat a pin
  number, but the writer seeded every per-pin UUID with just `designator.pin<N>`, so the two
  `(pin "1" ...)` nodes got the same UUID and the connectivity gate refused the write
  (`DUPLICATE_UUID`). Later occurrences now carry a `#k` suffix in the seed; first occurrences
  keep the historical seed, so existing schematics replay byte-identically. Found by the
  library-wide sweep (`Transistor_FET:IRL6297SD` was the one failure in 478).
- **Alternate (DeMorgan) body styles no longer duplicate every pin:** the KiCad library
  reader collected pins from every `Name_<unit>_<style>` sub-symbol, including the `_<unit>_2`
  DeMorgan re-drawing of the same physical unit — so a 74xx-style symbol resolved with each
  gate pin twice, the writer emitted colliding per-pin UUIDs, and the connectivity gate refused
  the placement (`DUPLICATE_UUID`, exit 6). Only body style 1 is collected now, and each pin
  records its owning unit in `owner_part_id` (`_0_*` common sub-symbols map to unit 1). Found by
  a library-wide sweep placing every derived symbol in KiCad's official 74xx library.
- **`(extends)`-derived symbols are now FLATTENED into the written `lib_symbols` cache**
  (KiCad-save style): the base's units/pins/graphics are inlined under the derived name (unit
  sub-symbols renamed `Base_u_s` → `Derived_u_s`), the derived symbol's own properties/settings
  overlaid, and the `extends` clause dropped — no base is cached separately. Previously the cache
  kept a bare `(extends "Base")` next to a library-qualified `Nick:Base` entry, which KiCad's
  loader does **not** resolve: eeschema reported `lib_symbol_mismatch`, the derived part lost all
  its pins, every wire to it dangled (`unconnected_wire_endpoint`), and KiCad's netlist omitted
  the part entirely — while akcli's own verifier and netlist looked clean. Found by running a
  drawn AMS1117-3.3 LDO block through KiCad 10's own ERC; regression-tested in
  `test_e2e_draw.py` (cache shape everywhere; pins-on-net via real `kicad-cli` in the KiCad CI job).
- **`kicad-cli` advisory runs work on KiCad 10 again:** KiCad 10's argument parser rejects the
  `--` end-of-options separator (`Unknown argument: --`), so every advisory ERC/netlist run was
  silently degrading to `report: null` (exit 1). Paths are now passed absolute instead of behind
  `--`, keeping the option-injection guard (an absolute path cannot start with `-`).
- **CFBF DIFAT spillover (> 109 FAT sectors) is now walked**, not refused: the spillover chain is
  read under the header-declared count, a cycle set, and the global sector cap (hostile input still
  fails with `ALTIUM_FAT_CYCLE` / `ALTIUM_ALLOC_GUARD` / `ALTIUM_MALFORMED`). Large real-world
  `.PcbDoc` containers now open.
- **`BOM_MISSING_VALUE` no longer fires on vendor-library parts** whose value lives in the part
  identity: a part-number parameter (`Manufacturer Part`, `LCSC Part Name`, ...) or a digit-bearing
  `library_ref` (e.g. `AO2301`) now substitutes for a blank `Comment`/`Value`. Generic symbols with
  no identity still report.
- **`ERC_NO_POWER` / `ERC_NO_GROUND` skip `U`-prefixed parts with fewer than 3 pins** (2-pin
  headers/jumper stubs designated `U*` are not ICs).
- **`pinmap` without a configured MCU** now says how to fix it (`--mcu <REF>` or
  `[project].mcu_designator`) instead of a bare warning.
- **Footprints** now resolve via the model-link chain (RECORD-45 model → RECORD-44 implementation →
  RECORD-1 component): the owner keying was wrong, so the model-link footprint was never found; the
  RECORD-41 `Footprint` / `Supplier Footprint` parameter is the fallback. Removes false
  `BOM_MISSING_FOOTPRINT` (80/80 components resolved on the reference board).
- **Rail voltage inference** no longer mis-fires on underscore-suffixed rails (`V3V3_BNO`, `V3V3_FSR`):
  the trailing word-boundary that `_` defeated is replaced; logic is now shared in `checks/_rails.py`,
  and configured `[[rail]]` names match `<rail>_suffix` too. Fixes false `ERC_NO_POWER`.
- **`export --json`** now errors (exit 2) with guidance instead of emitting non-JSON at exit 0.
- **`.SchLib` / `.PcbDoc`** binary records now surface as `ALTIUM_UNSUPPORTED` (exit 5, *unsupported*)
  instead of `ALTIUM_MALFORMED` (exit 3, *parse error*).
- **`pinmap --expected`** unmatched pins are now `WARNING` (non-zero exit) instead of a silent NOTE.
- **`-C/--config`** (and other global flags) are accepted before *or* after the subcommand.
- `schema_version` now stamped on every machine-readable command: `check` / `diff` / `pinmap` reports,
  plus `read` and `component --json` (`net` stays a bare array, as documented).
- **`draw --apply`** writes a `<target>.bak` next to the file (the doc'd backup was never wired up).
- `tarfile` extraction uses `filter="data"` (Python 3.14-ready, hardened).

### Known limitations
- A value whose Ω/µ/± was already written as the U+FFFD replacement bytes (`EF BF BD`) by an upstream
  tool on a non-UTF-8 locale is corrupted **at export** and cannot be recovered on read by any codec.
- `draw` snaps off-grid / non-orthogonal geometry rather than rejecting it.
- The Windows Altium *live driver* (DelphiScript half) needs a Windows + Altium 22+ box to validate.

### Notes
- Baselines at the first tagged release: package `0.1.0`, `schema_version = "1.0"`,
  `protocol_version = 1`.

[Unreleased]: https://github.com/tipoLi5890/altium-kicad-cli/commits/main
