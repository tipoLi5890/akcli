# Task 11 — relocate a filter without changing a net (edit)

`board.kicad_sch` already holds an NTC sense divider feeding an RC anti-alias
filter:

| Ref | Part | Origin (mil) | Rotation | Pin tips (mil) |
|---|---|---|---|---|
| `R1` | `Device:R` 10k | `[2000, 2000]` | 0 | 1 = `[2000, 1850]`, 2 = `[2000, 2150]` |
| `R2` | `Device:R` 10k | `[2000, 2600]` | 0 | 1 = `[2000, 2450]`, 2 = `[2000, 2750]` |
| `R3` | `Device:R` 1k | `[3000, 2450]` | 90 | 1 = `[2850, 2450]`, 2 = `[3150, 2450]` |
| `C1` | `Device:C` 100n | `[3600, 2750]` | 0 | 1 = `[3600, 2600]`, 2 = `[3600, 2900]` |

As drawn:

- label `V3V3` on `R1.1`; labels `GND` on `R2.2` and on `C1.2`;
- a wire `R1.2` -> `R2.1` carrying the label `NTC_OUT` mid-wire, at
  `[2000, 2300]`;
- a straight wire `R2.1` -> `R3.1` (so `R3.1` is on `NTC_OUT`);
- labels `ADC_IN` on `R3.2` and on `C1.1` (label-on-pin; no wire between them).

Nets: `V3V3` = `R1.1`; `NTC_OUT` = `R1.2` + `R2.1` + `R3.1`; `ADC_IN` =
`R3.2` + `C1.1`; `GND` = `R2.2` + `C1.2`.

**The edit.** The ADC moved to the right-hand side of the sheet. Move the
filter 1500 mil to the right — `R3` to `[4500, 2450]` and `C1` to
`[5100, 2750]` — as a NET-PRESERVING relocation:

- the wire from the divider must stay attached to `R3.1` (it stretches);
- every label sitting on a moved pin must travel with that pin;
- `R1` / `R2` do not move, and nothing may be left behind (no stray label, no
  dangling wire end).

Teaches: `move_component` moves the body only — `carry_wires` drags the wire
ends that sit on the moved pins, `carry_labels` drags the labels anchored
there. The plan's "Net changes" block must read `(none)`.

## Also checked

- `R3` at `[4500, 2450]` (rotation still 90), `C1` at `[5100, 2750]`; `R1`,
  `R2` unmoved.
- `akcli plan` reports an EQUIVALENT net diff (no change of any kind).
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
