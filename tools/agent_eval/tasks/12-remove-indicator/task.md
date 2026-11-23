# Task 12 — remove an indicator and leave nothing behind (edit)

`board.kicad_sch` already holds a 3.3 V rail with a decoupling cap, an enable
pull-up and a power-on indicator LED:

| Ref | Part | Origin (mil) | Rotation | Pin tips (mil) |
|---|---|---|---|---|
| `C1` | `Device:C` 100n | `[2000, 2000]` | 0 | 1 = `[2000, 1850]`, 2 = `[2000, 2150]` |
| `R2` | `Device:R` 10k | `[2600, 2000]` | 0 | 1 = `[2600, 1850]`, 2 = `[2600, 2150]` |
| `R1` | `Device:R` 330 | `[3400, 2000]` | 0 | 1 = `[3400, 1850]`, 2 = `[3400, 2150]` |
| `D1` | `Device:LED` GREEN | `[3400, 2600]` | 90 | 2 (anode) = `[3400, 2450]`, 1 (cathode) = `[3400, 2750]` |

As drawn:

- labels `3V3` on `C1.1`, `R2.1` and `R1.1`; label `EN` on `R2.2`;
- labels `GND` on `C1.2` and on `D1.1`;
- a wire `R1.2` -> `D1.2` carrying the label `LED_A` MID-WIRE, at
  `[3400, 2300]` (not on a pin).

Nets: `3V3` = `C1.1` + `R2.1` + `R1.1`; `GND` = `C1.2` + `D1.1`; `LED_A` =
`R1.2` + `D1.2`; `EN` = `R2.2`.

**The edit.** The indicator is cost-reduced out. Remove `R1` and `D1`
COMPLETELY: both parts, the wire between them, every label that belonged to
them. `C1` and `R2` keep their nets, and the sheet must be left clean — no
dangling wire end and no label that names nothing.

Teaches: `delete_component` alone leaves attached wires for the connectivity
gate to refuse; `"cascade": true` also deletes the wires ENDING on the deleted
pins and the labels anchored ON those pins — but a mid-wire label is neither,
so it needs its own `delete_object` (`match` by `kind` + `name`).

## Also checked

- `R1` and `D1` are gone; `C1` (100n) and `R2` (10k) are untouched.
- `akcli check --integrity --nets` reports no `DANGLING_ENDPOINT` and no
  `NET_LABEL_UNATTACHED`.

## Contract (edit task)

The workspace already holds `board.kicad_sch` — the schematic described above.
A copy sits next to this file at `setup/board.kicad_sch`, with the op-list
that produced it in `setup/setup_ops.json`. Author a single akcli op-list JSON
document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`
holding ONLY the edit — a delta list; do not re-place what is already there.
It is validated with `akcli ops validate` and applied to the existing sheet
with `akcli draw --apply --strict-nets`.
Coordinates are mils on a 50-mil grid, origin top-left, +Y down. Scoring
compares the resulting NAMED nets (exact pin membership) against the ground
truth, and every item under "Also checked" is one more scored check — use
exactly the designators, pin assignments and net names the task specifies,
and introduce no other named nets.
