# `akcli` CLI reference

`akcli` (long alias `altium-kicad-cli`) is the command-line entry point of `altium-kicad-cli`. It reads
Altium binary `.SchDoc`/`.SchLib`/`.PcbDoc` and KiCad `.kicad_sch`/`.kicad_sym`/`.kicad_pcb`, runs
checks, diffs revisions, and draws KiCad schematics — with no Altium or KiCad install required.

> This reference is the contract for the CLI surface. It tracks the subcommands and flags defined in
> `src/altium_kicad_cli/cli.py`.

```
akcli [GLOBAL FLAGS] <subcommand> [ARGS...]
```

**Convention:** `stdout` carries data (parsed JSON/text results); `stderr` carries logs and
diagnostics. This keeps `akcli ... --json | jq` clean.

## Global flags

| Flag | Effect |
|---|---|
| `--version` | Print package version **and** `protocol_version`, then exit. |
| `-h`, `--help` | Show help for `akcli` or a subcommand, then exit. |
| `-C`, `--config PATH` | Use this `altium-kicad-cli.toml` instead of walk-up discovery from the input file's directory. |
| `-v`, `-vv` | Increase log verbosity (to stderr). `-v` info, `-vv` debug-level logs. |
| `--quiet` | Suppress non-error logs on stderr. |
| `--json` | Emit machine-readable JSON on stdout (carries `schema_version`). |
| `--no-color` | Disable ANSI color in text output. |
| `--debug` | Show full Python tracebacks instead of structured `ERROR: CODE` messages. |

## Subcommands

### `akcli read <file>`
Parse an Altium or KiCad schematic/PCB into the normalized model and print it.
- Input: `.SchDoc`, `.kicad_sch` (and PCB variants).
- `--json` prints the full `Schematic`/`Pcb` export with `schema_version`.

### `akcli net <file>`
Extract the netlist (net → pin membership) using the shared `netbuild` engine.
- Output: nets with members, aliases, and source names; `--json` validates against
  `schemas/netlist.schema.json`.

### `akcli component <file>`
List components: designator, library reference, value, footprint, pin count, and sheet provenance.

### `akcli check <file>`
Run the design checks (ERC-lite + power + BOM hygiene) and print findings.
- `-C/--config` supplies rails, MCU designator, and `[[erc_waiver]]` entries.
- **Lint-style exit:** non-zero (`1`) when findings are present.
- `--exit-zero` forces exit `0` even with findings (report mode).

### `akcli diff <file_a> <file_b>`
Diff two schematic revisions. Nets are matched by **membership** (not display name); components by
UniqueID, then `(value, footprint, pin-count)` signature, then refdes.

### `akcli pinmap <file>`
Emit the MCU pin → net table (MCU chosen by `mcu_designator` in config).
- `--expected PATH` cross-checks against an external expected pin→signal table (CSV or JSON). The
  schematic is authoritative; the expected table is advisory.

### `akcli export <file>`
Export the normalized model as JSON (the canonical `--json` shape) for downstream tooling. Honors
`--json` formatting flags; stamps `schema_version`.

### `akcli plan <oplist.json> [--target FILE]`
Validate an op-list against `protocol_version` and `schemas/ops.schema.json`, resolve it against the
target `.kicad_sch`, and print what *would* change. Never writes. `--target` overrides the op-list's
`target_file`.

### `akcli draw <oplist.json> [--target FILE] [--apply]`
Execute an op-list against a KiCad `.kicad_sch`.
- **Default is a dry run** (no file written): prints per-op results and the connectivity verification.
- `--apply` performs the write via the atomic snapshot → temp → verify-on-temp → `os.replace` pipeline,
  writing a `<target>.bak` copy alongside the file. The write is rejected if the connectivity verifier fails.
- `--target` overrides the op-list's `target_file`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success / no findings. |
| `1` | Check findings present (lint-style; suppress with `--exit-zero`). |
| `2` | Usage / argument error. |
| `3` | Parse error (corrupt OLE2 or S-expression). |
| `4` | File not found. |
| `5` | Unsupported format. |
| `6` | Op-list or verify failure. |
| `7` | Required external tool missing. |

## Structured errors

Without `--debug`, failures print a single structured line, e.g.:

```
ERROR: ALTIUM_FAT_CYCLE: FAT chain revisits sector 42 (cycle); aborting
```

Error codes (registry in `src/altium_kicad_cli/errors.py`) include `ALTIUM_BAD_MAGIC`,
`ALTIUM_FAT_CYCLE`, `ALTIUM_OOB_SECTOR`, `ALTIUM_BAD_SECTOR_SHIFT`, `ALTIUM_ALLOC_GUARD`,
`ALTIUM_MALFORMED`, `KICAD_SEXPR_DEPTH`, `KICAD_SEXPR_UNTERMINATED`, `KICAD_SEXPR_TOOBIG`,
`SYMBOL_NOT_FOUND`, `BAD_ANGLE`, `NON_ORTHOGONAL_WIRE`, `OFF_GRID`, `OVERLAP`, `VERIFY_FAILED`,
`OP_UNSUPPORTED`, `HIERARCHICAL_UNSUPPORTED`, `PROTOCOL_MISMATCH`, `PATH_OUTSIDE_ROOT`,
`KICAD_CLI_TIMEOUT`, `KICAD_CLI_MISSING`, and `BAD_CONFIG`.

## Examples

```bash
akcli read main.SchDoc --json | jq '.components | length'
akcli net  board.kicad_sch --json > netlist.json
akcli check main.SchDoc -C altium-kicad-cli.toml          # exit 1 if findings
akcli diff  v1.SchDoc v2.SchDoc
akcli pinmap main.SchDoc -C altium-kicad-cli.toml --expected pins.csv
akcli plan ops.json --target board.kicad_sch
akcli draw ops.json --target board.kicad_sch --apply
```
