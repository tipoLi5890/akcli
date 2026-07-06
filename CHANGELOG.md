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
