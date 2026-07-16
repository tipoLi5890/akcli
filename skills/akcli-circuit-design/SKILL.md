---
name: akcli-circuit-design
description: >-
  Read, analyze, diff, and draw electronic schematics with the zero-dependency
  `akcli` CLI (akcli) — no Altium or KiCad install required. Use this
  skill whenever the task involves: reading or parsing an Altium .SchDoc/.SchLib/.PcbDoc
  or a KiCad .kicad_sch/.kicad_sym/.kicad_pcb; extracting a netlist; running ERC,
  power-rail, BOM, or a design review on a schematic; producing a pin map / pinout /
  MCU pin->net table; diffing two schematic revisions (v1 vs v2); drawing or editing a
  KiCad schematic from an op-list; or working with JLC/LCSC/EDA parts, footprints, and
  connectivity. Triggers on keywords: SchDoc, kicad_sch, schematic, netlist, ERC,
  electrical rule check, design review, power rails, pinmap, pinout, schematic diff,
  draw KiCad, op-list.
---

# akcli-circuit-design — driving `akcli` for schematic read / analyze / diff / draw

`akcli` is a zero-dependency Python ≥3.11 CLI that reads
**Altium binary** `.SchDoc` / `.SchLib` / `.PcbDoc` and **KiCad** `.kicad_sch` / `.kicad_sym` /
`.kicad_pcb` into one normalized model, runs design checks, diffs revisions, and writes KiCad
schematics from an op-list — with **no Altium or KiCad installed**. It is **not** an
Altium-to-KiCad converter.

When the plugin is installed, `akcli` is on `PATH`. Run it with the `Bash` tool. From a raw
checkout instead use `PYTHONPATH=src python3 -m akcli ...` or `bin/akcli ...`.

## Core design principles (follow these — they are why this tool exists)

- **Raw data only at the source; derive everything yourself, downstream.** Readers emit a
  normalized model (components, pins, nets, primitives); checks/diff/pinmap compute on top of it.
- **Never trust a single tool blindly.** The original Altium net merge had a real bug (split
  same-name `GND`, dropped a `STAT`↔`LED1_GPIO_RD` alias). Always **verify converted/derived
  results**: re-read after writing, cross-check a netlist against an expected table, and treat
  any vacuous "0 findings" with suspicion (read the metadata caveats `check` prints — passive-pin
  ratio, No-ERC suppressed count, unnamed-net count).
- **Altium is read/analyze-only.** `akcli` never writes Altium files offline. KiCad gets a full
  cross-platform writer (`plan`/`draw`). For Altium edits, deliver human draw instructions plus an
  Altium-importable Protel netlist (`akcli export --format protel`).
- **The schematic is authoritative; external pinout/DTS tables are advisory.**

## Workflow

### (1) Read — normalize the schematic first

```bash
akcli capabilities --json         # COLD-START: the full CLI surface manifest (commands, flags,
                                  # exit/error tables, op vocabulary, schemas) in one document
akcli read <file> --summary --json  # counts + metadata ONLY — start big boards here, never
                                    # with a full dump (context-budget escape hatch)
akcli read <file> --json          # full normalized Schematic/Pcb (carries schema_version)
akcli read <file> --md            # human Markdown summary
akcli nets <file> --match 'VDD*' --json  # filtered net listing (--limit N caps it; the JSON
                                         # envelope carries total/matched/returned/truncated)
akcli net  <file> [name] --json   # netlist: nets -> pin members, aliases, source names
                                  # (a named miss exits 8 with {"found": false})
akcli component <file> <REF>      # one component's pin -> net (e.g. U3); omit REF to list all
akcli render <file> -o out.svg    # install-free SVG — LOOK at the sheet (works on .SchDoc too)
akcli log <dir-or-file>           # workspace write journal: what plan/draw/undo did here
```

Inputs: `.SchDoc`, `.SchLib`, `.PcbDoc`, `.PcbLib`, `.kicad_sch`, `.kicad_sym`, `.kicad_pcb`.
`stdout` is data, `stderr` is logs — so `akcli ... --json | jq` stays clean.
`akcli read --strict` turns a non-empty source that normalizes to zero objects
(`EMPTY_IMPORT`) into exit 1 instead of a quiet empty result.

### (2) Analyze — check / diff / pinmap

```bash
# Design review (ERC-lite + power rails + BOM hygiene). Lint-style: exits 1 when findings exist.
akcli check <file> -C akcli.toml          # all checks
akcli check <file> --erc --power --bom               # select checks
akcli check <file> --exit-zero                       # report mode (always exit 0)

# Net-level revision diff. Nets matched by MEMBERSHIP (not display name); components by
# UniqueID, then (value, footprint, pin-count) signature, then refdes.
akcli diff <file_a> <file_b>

# MCU pin -> net table (MCU from config mcu_designator or --mcu). Optional cross-check.
akcli pinmap <file> -C akcli.toml
akcli pinmap <file> --mcu U3 --expected pins.csv     # expected = .csv or .json

akcli verify sch.kicad_sch board.kicad_pcb --strict   # schematic <-> PCB net/refdes/footprint equivalence
akcli check board.kicad_sch --contract contract.toml   # topology require/forbid rules ERC can't express
```

`-C/--config akcli.toml` supplies `mcu_designator`, `[[rail]]` voltages, and
`[[erc_waiver]]` entries; without `-C`, discovery walks up from the schematic's directory. Always read the metadata
header `check` prints before declaring a board clean.

Component values you place should come from **`akcli calc`** (60 standards-cited
engineering calculators — E-series snap, dividers, IPC-2221 track width, I²C pull-ups,
555, buck/boost, ...), not mental arithmetic — see the `akcli-design-calc` skill.

### (3) Draw / edit a **KiCad** schematic — op-list, then plan, then draw

KiCad is the only writable target. Build an op-list JSON (document shape and the op vocabulary — 18 ops + 9 macros incl. `delete_component`/`delete_object`/`add_sheet`
are defined in **`schemas/ops.schema.json`**; guide: `docs/op-list-authoring.md`, scaffolder: `akcli ops list` / `akcli ops template <op>`):

```json
{
  "protocol_version": 1,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "ops": [
    { "op": "place_component", "lib_id": "Device:R", "designator": "R10",
      "x_mil": 1000, "y_mil": 800, "value": "10k" },
    { "op": "add_wire", "vertices": ["R10.2", [1100, 800]] },
    { "op": "place_power_port", "lib_id": "power:GND", "net_name": "GND", "at": [1100, 900] }
  ]
}
```

Op-list rules (the validator enforces them): coordinates are **mils, origin top-left, +Y down,
50-mil grid**; rotation is the enum `{0,90,180,270}`; mirror `{none,x,y}`; wire `vertices` is an
even, orthogonal array of `[x,y]` points or `"REF.PIN"` endpoint strings (the executor snaps a
pin ref to the pin's real world coordinate). Then:

```bash
# Validate + resolve against the target; print what WOULD change. Never writes.
akcli plan board.kicad_sch --ops ops.json

# Apply. DEFAULT IS DRY-RUN (verify only). Add --apply to write.
akcli draw board.kicad_sch --ops ops.json              # dry-run: per-op results + connectivity verify
akcli draw board.kicad_sch --ops ops.json --apply      # atomic snapshot->temp->verify-on-temp->os.replace + backup
akcli draw board.kicad_sch --ops ops.json --symbols extra.kicad_sym   # extra symbol source (repeatable)
```

`--apply` writes only if the pure-Python connectivity verifier passes (zero dangling endpoints);
otherwise the write is rejected and the original file is untouched. **After applying, re-read
(`akcli read` / `akcli net`) to confirm the change** — never assume the write was correct.

### (4) Draw / edit an **Altium** schematic — analyze-only, deliver instructions + Protel netlist

`akcli` cannot write Altium offline. To request Altium edits:
1. Author the same op-list as in step (3) and translate it into clear, human draw instructions
   (place which symbol where, which pins to wire, which power ports/labels to add).
2. Produce an Altium-importable netlist for verification/import:
   ```bash
   akcli export <file> --format protel -o board.net     # also: --format kicad|csv
   ```
3. Hand both to the user; after they edit in Altium, `akcli read`/`akcli diff` the result to
   verify it matches intent.

## Exit codes

`0` success / no findings · `1` check findings present (lint-style; `--exit-zero` forces 0) ·
`2` usage/arg error · `3` parse error (corrupt OLE2/S-expr) · `4` file not found ·
`5` unsupported format · `6` op-list / verify failure (also covers `TARGET_LOCKED`, a
KiCad GUI lock on the target — see `--allow-open` on `draw`/`arrange`/`undo`) ·
`7` required external tool missing.

## Errors

Without `--debug`, failures print one structured line, e.g.
`ERROR: ALTIUM_FAT_CYCLE: ...`. Add `--debug` for a full traceback. Global flags on every
subcommand: `-C/--config`, `-v`/`-vv`, `-q/--quiet`, `--json`, `--no-color`, `--debug`.

## Companion slash commands

`/circuit-review` (check + optional diff) · `/circuit-pinmap` (pinmap, optional `--expected`) ·
`/circuit-diff` (diff two revisions) · `/circuit-draw` (build + validate an op-list and draw;
user-triggered only, it writes files) · `/circuit-parts` (jlc search → show → add → plan →
draw: source a real part and place it; networked + writes files, user-triggered only).
