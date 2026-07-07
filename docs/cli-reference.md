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

### `akcli read <file> [--md]`
Parse an Altium or KiCad schematic/PCB/library into the normalized model and print it.
- Input: `.SchDoc`, `.SchLib`, `.PcbDoc`, `.kicad_sch`, `.kicad_sym`, `.kicad_pcb`.
- A KiCad root sheet **recurses into its `(sheet ...)` children** (paths relative to the parent
  file, cycle- and depth-guarded); every sheet instance contributes its components under the
  designator from the matching `(instances (path ...))` entry.
- `--json` prints the full `Schematic`/`Pcb`/`Library` export with `schema_version`; `--md` prints
  a human Markdown summary.

### `akcli net <file> [NAME]`
Extract the netlist (net → pin membership) using the shared `netbuild` engine.
- With `NAME`, print just that net; a miss prints a notice to **stderr** and still exits `0`.
- Output: nets with members, aliases, and source names; `--json` validates against
  `schemas/netlist.schema.json`.

### `akcli component <file> [REF]`
Without `REF`: list components (designator, library reference, value, footprint, pin count, sheet).
With `REF`: that component's pin → net table. A missing `REF` prints a notice to **stderr** and
exits `0`.

### `akcli check <file>`
Run the design checks (ERC-lite + power + BOM hygiene) and print findings.
- `-C/--config` supplies rails, MCU designator, and `[[erc_waiver]]` entries.
- `--erc` / `--power` / `--bom` select check families (default: all).
- **Lint-style exit:** non-zero (`1`) when findings are present.
- `--exit-zero` forces exit `0` even with findings (report mode).

### `akcli diff <file_a> <file_b>`
Diff two schematic revisions. Nets are matched by **membership** (not display name); components by
UniqueID, then `(value, footprint, pin-count)` signature, then refdes.

### `akcli pinmap <file>`
Emit the MCU pin → net table (MCU chosen by `mcu_designator` in config, or `--mcu REF`).
- `--expected PATH` cross-checks against an external expected pin→signal table (CSV or JSON). The
  schematic is authoritative; the expected table is advisory.

### `akcli expected <file.dts|.overlay|.md> [-o FILE]`
Extract an **expected pin→signal table** from a Zephyr devicetree source/overlay
(`gpios = <&gpioN pin ...>` phandles and Nordic `NRF_PSEL(...)` pinctrl) or from a
markdown pinout table (`--key-header`/`--value-header` pick columns explicitly).
Emits the JSON object `pinmap --expected` consumes; `-o` writes it to a file.
Exits `1` when nothing was extracted (an empty table would make `pinmap
--expected` vacuously pass), `2` on an unsupported input type, `4` when the
file is missing. The schematic stays authoritative — this table is advisory.

### `akcli export <file> [--format protel|kicad|csv] [-o FILE]`
Export the schematic's **netlist** for other EDA tools. Default `--format protel` (an
Altium-importable `.NET`); `kicad` emits a legacy eeschema netlist; `csv` flat `net,ref,pin` rows.
Writes stdout unless `-o` is given. Deterministically sorted; unnamed nets are named by their
membership-derived `stable_id`, so re-exports diff cleanly. `--json` is **refused** (exit `2`) —
use `akcli net --json` for structured output.

### `akcli plan <target.kicad_sch> --ops FILE [--symbols PATH ...]`
Validate an op-list against `protocol_version` and `schemas/ops.schema.json`, resolve it against the
target `.kicad_sch` (symbols from repeatable `--symbols` sources and the target's inline cache), and
print what *would* change. Never writes.

### `akcli draw <target.kicad_sch> --ops FILE [--symbols PATH ...] [--apply]`
Execute an op-list against a KiCad `.kicad_sch`. The vocabulary is 16 ops (see
`schemas/ops.schema.json`), including `delete_component` / `delete_object` / `move_component` and
multi-unit placement via `place_component`'s optional `"unit"` field.
- **Default is a dry run** (no file written): prints per-op results and the connectivity
  verification. (`--dry-run` is accepted but inert — omitting `--apply` already is the dry run.)
- `--apply` performs the write via the atomic snapshot → temp → verify-on-temp → `os.replace`
  pipeline, writing a `<target>.bak` copy alongside the file. The write is rejected (exit `6`)
  if any op errors or the connectivity verifier finds an ERROR.

### `akcli jlc <search|show|add> ...`
JLCPCB/LCSC part search and library conversion — the only **networked** subcommand family. See
[docs/jlc.md](jlc.md) for the full reference.

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
akcli export main.SchDoc --format protel -o board.net
akcli plan board.kicad_sch --ops ops.json --symbols Device.kicad_sym
akcli draw board.kicad_sch --ops ops.json --symbols Device.kicad_sym --apply
```
