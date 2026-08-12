---
name: akcli-circuit-design
description: >-
  Read, analyze, diff, and draw electronic schematics with the `akcli` CLI —
  AI-native schematic design, purpose-built for KiCad; Altium files are a
  read-only import surface. Use this
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

`akcli` is a stdlib-only Python ≥3.11 CLI purpose-built for KiCad: it reads
**KiCad** `.kicad_sch` / `.kicad_sym` / `.kicad_pcb` and **Altium binary** `.SchDoc` /
`.SchLib` / `.PcbDoc` into one normalized model, runs design checks, diffs revisions, and
writes KiCad schematics from an op-list. Altium is a read-only import on-ramp into the
KiCad flow; `akcli` is **not** an Altium-to-KiCad converter.

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
akcli capabilities --command draw # ONE command's entry (~3% of the manifest) — prefer slices
                                  # over the full ~96 KB document (--section exit_codes|ops|...)
akcli schema ops                  # any packaged JSON Schema, byte-for-byte (ops, findings,
                                  # netlist, draw-result, ...) for client-side validation
akcli read <file> --summary --json  # counts + metadata ONLY — start big boards here, never
                                    # with a full dump (context-budget escape hatch)
akcli read <file> --json          # full normalized Schematic/Pcb (carries schema_version)
akcli read <file> --md            # human Markdown summary
akcli nets <file> --match 'VDD*' --json  # filtered net listing (--limit N caps it; the JSON
                                         # envelope carries total/matched/returned/truncated)
akcli net  <file> [name] --json   # netlist: nets -> pin members, aliases, source names
                                  # (a named miss exits 8 with {"found": false})
akcli component <file> <REF>      # one component's pin -> net (e.g. U3); omit REF to list all
akcli render <file> -o out.svg    # pure-stdlib SVG — LOOK at the sheet (works on .SchDoc too)
akcli doc <file> -o book.md       # pinout book: pin->net tables + power rails + BOM (human hand-off)
akcli groups <file>               # functional groups (from hidden Group properties); --frame draws borders
akcli bbox Device:R --at 1000 800 # symbol world bounding box at a placement (spacing planning; sibling of pins)
akcli view live                   # localhost dashboard, re-renders on every apply (auto-discovers
                                  # the single .kicad_sch in CWD; bare `akcli view` serves the hub)
akcli log <dir-or-file>           # workspace write journal: what plan/draw/undo did here
```

Inputs: `.SchDoc`, `.SchLib`, `.PcbDoc`, `.PcbLib`, `.kicad_sch`, `.kicad_sym`, `.kicad_pcb`.
`stdout` is data, `stderr` is logs — so `akcli ... --json | jq` stays clean.
`akcli read --strict` turns a non-empty source that normalizes to zero objects
(`EMPTY_IMPORT`) into exit 1 instead of a quiet empty result.

### (2) Analyze — check / diff / pinmap

```bash
# Design review (ERC-lite + power rails + BOM hygiene). Lint-style: exits 1 when findings exist.
akcli check <file> -C akcli.toml          # all checks (ERC + power + BOM + nets + layout +
                                          # integrity + pairs)
akcli check <file> --erc --power --bom               # select checks
akcli check <file> --integrity                       # write-gate document checks, read-only
                                                     # (.kicad_sch: dangling endpoints, duplicate
                                                     # uuids/designators, unresolved lib_ids,
                                                     # instance paths, NC conflicts)
akcli check <file> --fail-on never                   # report mode (always exit 0)

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

KiCad is the only writable target. Build an op-list JSON (document shape and the op vocabulary — 22 ops + 10 macros incl. `delete_component`/`delete_object`/`add_sheet`/`route_net`/
`add_rectangle`/`add_text_box`/`set_title_block` and the `place_array` macro —
are defined in **`schemas/ops.schema.json`**; guide: `docs/op-list-authoring.md`, scaffolder: `akcli ops list` / `akcli ops template <op>`). One op-list can also edit
the target AND its child sheets in one atomic transaction — envelope `sheets` map +
per-op `"sheet"` tags; the net gate evaluates the whole sheet tree; `akcli undo
<target> --run <run_id>` reverts a multi-file run together (details: the
akcli-schematic-authoring skill):

```json
{
  "protocol_version": 2,
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

A `lib_id` nickname that `--symbols` and config `[paths]` do not cover is looked up
in the project and global `sym-lib-table` as a last resort, so `Device:R` resolves
against an installed KiCad — but pass `--symbols` explicitly when the result must
be reproducible on another machine.

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

`0` success / no findings · `1` check findings present (lint-style; `--fail-on never` forces 0) ·
`2` usage/arg error (incl. `BAD_CONFIG` — an explicit `-C` that is missing, a directory, or
unparseable is never silently ignored) · `3` parse error (corrupt OLE2/S-expr, or bytes that
are not UTF-8 at all: `KICAD_BAD_ENCODING`) · `4` path not usable: missing (`FILE_NOT_FOUND`)
or present but unreadable — a directory where a file was expected, no read permission, an IO
error (`FILE_UNREADABLE`) ·
`5` unsupported format · `6` op-list / verify failure (also covers `TARGET_LOCKED`, a
KiCad GUI lock on the target — every write path refuses on it: `draw`, `arrange`,
`undo`, `relink-symbols --apply`, `library repair --apply`; `--allow-open` overrides) ·
`7` required external tool missing / network unreachable ·
`8` query miss (`QUERY_MISS` — `net`/`component` asked for a name the file does not contain).

## Errors

Without `--debug`, failures print one structured line, e.g.
`ERROR: ALTIUM_FAT_CYCLE: ...`. Add `--debug` for a full traceback. Global flags on every
subcommand: `-C/--config`, `-v`/`-vv`, `-q/--quiet`, `--json`, `--no-color`, `--debug`.

## Companion slash commands

8 `/circuit-*` commands: `/circuit-review` (review engine + checks, optional diff) ·
`/circuit-pinmap` (pinmap, optional `--expected`) · `/circuit-diff` (diff two revisions) ·
`/circuit-sim` (deck inspection + SPICE assertions; read-only) · `/circuit-release`
(`release preflight` GO/NO-GO with a manifest) · `/circuit-draw` (build + validate an
op-list and draw; user-triggered only, it writes files) · `/circuit-parts` (jlc search →
show → add → plan → draw: source a real part and place it; networked + writes files,
user-triggered only) · `/circuit-datasheet` (datasheet fetch → audited facts; networked +
writes files, user-triggered only).
