---
name: schematic-authoring
description: >-
  Create a NEW KiCad schematic from written requirements — or surgically extend an
  existing sheet — by authoring an `akcli` op-list and driving the plan -> dry-run ->
  apply -> verify loop. Use this skill whenever the task involves: designing a circuit
  from a spec or datasheet ("設計電路", "design a circuit", "create a schematic from
  scratch"); placing components, wires, net labels, power ports, or junctions in a
  .kicad_sch via op-list JSON; picking real parts with JLC/LCSC sourcing before drawing;
  or safely re-running / editing a previously drawn sheet. Triggers on keywords:
  new schematic, draw circuit, op-list, place_component, add_wire, net label,
  power port, decoupling, LDO block, connector fanout, akcli plan, akcli draw,
  JLC part selection, kicad_sch authoring.
---

# schematic-authoring — from requirements to a verified `.kicad_sch`

This skill covers *creating* circuits with the `akcli` op-list writer. For basic
read/analyze/draw mechanics, exit codes, error format, and the Altium (read-only)
path, **see the circuit-design skill** — do not re-derive them here. KiCad is the
only writable target; the writer is flat (single-sheet) v1.

**The authoring loop (never skip a stage):**
requirements → block plan → part selection → op-list → `akcli plan` →
`akcli draw` (dry-run) → `akcli draw --apply` → re-read + `akcli check`.

> CLI drift warning: `docs/cli-reference.md` shows an older `plan <oplist> --target`
> signature. The real surface is `akcli plan <target.kicad_sch> --ops FILE` — trust
> `akcli plan --help` and this skill, never invent flags.

## (1) Block plan before any op

Decompose the requirements into named blocks (power entry, regulation, MCU,
connectors), list every rail and named net, and assign designators up front
(U1, C1..Cn, J1...). Sketch a placement map: one block per region of the sheet,
signal flow left→right, rails at the top, GND at the bottom. Only then write ops.

## (2) Part selection — `akcli jlc` when sourcing matters

When the user cares about real orderable parts (or you need a symbol that no local
library has), resolve parts first. `jlc` is the only networked subcommand (exit 7
on network failure):

```bash
akcli jlc search "AMS1117-3.3" --limit 10        # find candidates (B=Basic, P=Preferred)
akcli jlc show C6186 --easyeda                    # confirm package, MPN, 3D availability
akcli jlc add C6186 --to kicad --out akcli-parts/C6186   # fetch + convert symbol/footprint
```

The produced `.kicad_sym` becomes a `--symbols` source for `plan`/`draw`. To get a
ready-made placement op, add `--place --designator U1 --at 2000 1000` (KiCad-only;
requires both flags) — it writes `akcli-parts/C6186/place.json` which you apply with
`akcli draw`. Converter output is third-party: always `akcli check` after placing.

## (3) Seed the target file (new schematics only)

`plan`/`draw` require an existing target with a root `(uuid ...)`. For a brand-new
schematic, seed a minimal skeleton once:

```bash
python3 -c 'import uuid,pathlib; pathlib.Path("board.kicad_sch").write_text(
    f"(kicad_sch (version 20231120) (generator \"akcli\") (uuid \"{uuid.uuid4()}\") (paper \"A4\"))\n")'
```

Keep this file for the whole session: the root uuid is the namespace for every
deterministic op UUID, so regenerating it breaks idempotent re-runs.

## (4) Op-list authoring patterns

Document shape and the 13-op vocabulary are defined in `schemas/ops.schema.json`
(11 core ops + `place_gnd`/`place_vcc` sugar); per-executor support is in
`schemas/ops.capabilities.json`. Envelope: `{"protocol_version": 1,
"target_format": "kicad", "ops": [...]}`. Rules the validator and executor enforce:

- **Coordinates**: mils, origin top-left, +Y down, 50-mil grid. Raw `[x,y]` points
  are grid-snapped; `"REF.PIN"` endpoints snap to the pin's exact world coordinate.
  Place two-pin passives on 100-mil multiples so wires between aligned pins stay
  orthogonal (use an L-shaped dogleg via an intermediate `[x,y]` otherwise).
- **Wire vs label vs power port**: wire (`add_wire`, `"REF.PIN"` endpoints) for
  short in-block connections; `add_net_label` with `"scope": "local"` on a short
  wire stub for readable in-block nets; `"scope": "global"` for nets crossing
  blocks (writer is flat — avoid `hierarchical`, sheets are unsupported);
  `place_power_port` / `place_gnd` / `place_vcc` for rails (power ports merge by
  name everywhere and auto-allocate `#PWR` refs).
- **Junctions**: where 3+ wire ends meet, `auto_junctions` inserts `(junction)`
  nodes automatically before verify — do not hand-place `add_junction` unless the
  dry-run connectivity report shows a genuine miss. Pure X crossings are never
  auto-junctioned (by design): dogleg one wire instead.
- **Annotation**: `add_text` (`text`, `at`, optional free `angle`) for block titles
  and design notes; it is the only graphic op.
- **No delete/move ops exist.** `set_component_transform` changes rotation/mirror
  only; to reposition, restore the `.bak` and redraw with corrected coordinates.

### Example A — LDO regulator block (new sheet)

```json
{ "protocol_version": 1, "target_format": "kicad", "target_file": "board.kicad_sch",
  "ops": [
    { "op": "place_component", "lib_id": "Regulator_Linear:AMS1117-3.3",
      "designator": "U1", "x_mil": 2000, "y_mil": 1000 },
    { "op": "place_component", "lib_id": "Device:C", "designator": "C1",
      "x_mil": 1500, "y_mil": 1150, "value": "10u" },
    { "op": "place_component", "lib_id": "Device:C", "designator": "C2",
      "x_mil": 2500, "y_mil": 1150, "value": "10u" },
    { "op": "add_wire", "vertices": ["C1.1", [1500, 1000], "U1.3"] },
    { "op": "add_wire", "vertices": ["U1.2", [2500, 1000], "C2.1"] },
    { "op": "add_net_label", "name": "VIN_5V", "at": [1500, 1000], "scope": "global" },
    { "op": "place_power_port", "lib_id": "power:+3V3", "net_name": "+3V3", "at": [2500, 1000] },
    { "op": "add_wire", "vertices": ["C1.2", [1500, 1400], [2500, 1400], "C2.2"] },
    { "op": "add_wire", "vertices": ["U1.1", [2000, 1400]] },
    { "op": "place_gnd", "at": [2000, 1400] },
    { "op": "add_text", "text": "3V3 LDO regulator", "at": [1500, 800] }
  ] }
```

### Example B — decoupling cap on an existing MCU (surgical edit)

```json
{ "protocol_version": 1, "target_format": "kicad", "ops": [
    { "op": "place_component", "lib_id": "Device:C", "designator": "C10",
      "x_mil": 3200, "y_mil": 900, "value": "100n",
      "footprint": "Capacitor_SMD:C_0402_1005Metric" },
    { "op": "add_wire", "vertices": ["C10.1", "U3.11"] },
    { "op": "add_wire", "vertices": ["C10.2", "U3.12"] }
  ] }
```

### Example C — connector fanout with global labels

```json
{ "protocol_version": 1, "target_format": "kicad", "ops": [
    { "op": "place_component", "lib_id": "Connector_Generic:Conn_01x04",
      "designator": "J1", "x_mil": 1000, "y_mil": 2000 },
    { "op": "add_wire", "vertices": ["J1.1", [1300, 2000]] },
    { "op": "add_net_label", "name": "UART_TX", "at": [1300, 2000], "scope": "global" },
    { "op": "add_wire", "vertices": ["J1.2", [1300, 2100]] },
    { "op": "add_net_label", "name": "UART_RX", "at": [1300, 2100], "scope": "global" },
    { "op": "add_wire", "vertices": ["J1.3", [1300, 2200]] },
    { "op": "place_gnd", "at": [1300, 2200] },
    { "op": "add_no_connect", "pin": "J1.4" }
  ] }
```

## (5) Validate → dry-run → apply

Every `place_component` / power-port symbol must resolve from a source: per-op
`symbol_source`, repeatable `--symbols` (a `.kicad_sym`, or a `.kicad_sch` whose
inline `lib_symbols` is harvested), or config `[paths]` entries ending in
`.kicad_sym`. A miss is `SYMBOL_NOT_FOUND`.

```bash
SYMS=/usr/share/kicad/symbols    # macOS: /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols
akcli plan board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym"      # never writes
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym"      # dry-run (the default)
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym" --apply
```

Exit 6 means an op errored or connectivity found an ERROR/CRITICAL
(`DANGLING_ENDPOINT`, `UNRESOLVED_LIB_ID`, ...): on `--apply` nothing was written.
Use `--json` for machine output: `{applied, ops[], connectivity[]}`. On
`OP_UNSUPPORTED`, `PROTOCOL_MISMATCH`, or geometry errors: stop and report, do not
retry blindly. Do not pass `--dry-run` — it is accepted but inert; omitting
`--apply` already is the dry run.

## (6) Idempotency and safe re-runs

- Created nodes get deterministic UUIDv5 keyed on the root sheet uuid plus
  `designator:op_index` (or tag:coords). **Re-running the same op-list file
  converges** — prior same-uuid nodes are replaced, `#PWR` refs are recovered,
  re-applies stay byte-identical.
- Because identity includes `op_index`, never reorder or insert ops mid-list in a
  file you already applied. To extend an applied sheet, author a NEW small op-list
  containing only the delta ops (Example B).
- `set_component_transform` / `set_component_parameters` address parts by
  designator; an unknown designator fails that op with `VERIFY_FAILED` and blocks
  the whole write.
- Apply is atomic with an optimistic lock: if the target changed on disk between
  read and write, apply aborts (`VERIFY_FAILED`) and the file is untouched. A
  `<name>.kicad_sch.bak` copy always lands next to the target on apply.

## (7) Verify everything after `--apply` — never trust a silent success

`applied: true` proves a write happened, not that the circuit is right. Always:

```bash
akcli read board.kicad_sch --md                        # parts present, values correct?
akcli net board.kicad_sch --json | jq '.nets[] | {name, members}'   # every intended net, exact membership
akcli component board.kicad_sch U1                     # pin->net map of key parts
akcli check board.kicad_sch --exit-zero                # ERC-lite + power + BOM, report mode
akcli diff board.kicad_sch.bak board.kicad_sch --exit-zero   # the delta is exactly what you drew
```

Compare each net's membership against your block plan pin by pin. Read `check`'s
metadata caveats (passive-pin ratio, unnamed-net count) before declaring the sheet
clean — a findings-free run on a mostly-passive sheet proves little. If anything
diverges, fix the op-list and re-run the loop from `akcli plan`.
