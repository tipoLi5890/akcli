# Changelog

All notable changes to `altium-kicad-cli` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning policy

`altium-kicad-cli` ships **three** version numbers; this section is their contract.

- **Package version (SemVer `MAJOR.MINOR.PATCH`).** The single source of truth is
  `pyproject.toml`; `tools/sync_version.py` stamps it into `.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json`, and CI fails on drift. During active pre-1.0 development the
  plugin manifests intentionally carry **no `version`** (commit-SHA versioning); a `version` is added
  at the first tagged release. SemVer rules:
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

Not yet tagged or published to PyPI; install from source (see `INSTALL.md`).

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

### Fixed
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
