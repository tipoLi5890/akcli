# Task 15 — repair a mislabeled connector pin (edit)

`board.kicad_sch` already holds an I2C breakout: two pull-ups, two series
resistors and a 4-pin header. Every connection is a label on a pin.

| Ref | Part | Origin (mil) | Rotation | Pin tips (mil) |
|---|---|---|---|---|
| `R1` | `Device:R` 4.7k | `[1400, 1400]` | 0 | 1 = `[1400, 1250]`, 2 = `[1400, 1550]` |
| `R2` | `Device:R` 4.7k | `[2000, 1400]` | 0 | 1 = `[2000, 1250]`, 2 = `[2000, 1550]` |
| `R3` | `Device:R` 33 | `[1400, 2600]` | 90 | 1 = `[1250, 2600]`, 2 = `[1550, 2600]` |
| `R4` | `Device:R` 33 | `[1400, 3100]` | 90 | 1 = `[1250, 3100]`, 2 = `[1550, 3100]` |
| `J1` | `Generic:CONN_1x4` | `[3000, 2000]` | 0 | 1 = `[2700, 1850]`, 2 = `[2700, 1950]`, 3 = `[2700, 2050]`, 4 = `[2700, 2150]` |

Labels as drawn: `3V3` on `R1.1`, `R2.1`, `J1.1`; `SDA` on `R1.2`, `R3.2`,
`J1.2` **and `J1.3`**; `SCL` on `R2.2`, `R4.2`; `SDA_MCU` on `R3.1`;
`SCL_MCU` on `R4.1`; `GND` on `J1.4`.

**The bug.** Header pin 3 is the clock, but its label says `SDA` — on this
sheet `J1.3` is shorted to the data line and the header has no clock at all.

**The edit.** Repair only that pin: remove the wrong `SDA` label sitting on
`J1.3` (it is the label at exactly `[2700, 2050]`) and label `J1.3` `SCL`
instead. Do not use `rename_net` — it would rename the WHOLE `SDA` net.

Target nets: `3V3` = `R1.1` + `R2.1` + `J1.1`; `SDA` = `R1.2` + `R3.2` +
`J1.2`; `SCL` = `R2.2` + `R4.2` + `J1.3`; `SDA_MCU` = `R3.1`; `SCL_MCU` =
`R4.1`; `GND` = `J1.4`.

Teaches: `delete_object` with a `match` selector (`kind` + `name` + exact
`at`, since three other labels are also called `SDA`), followed by a
re-label; and reading the plan's "Net changes" block — one pin leaving a net
for another existing net is reported as `! MOVE J1.3: SDA -> SCL`. That line
is the intended repair; any other `!` line would be collateral damage.

## Also checked

- `akcli plan` reports exactly one net change, `! MOVE J1.3: SDA -> SCL`
  (risky, not equivalent) — nothing else may change.
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
with `akcli draw --apply` — WITHOUT `--strict-nets`, because this repair
intentionally changes named nets (the exact resulting netlist is still
enforced).
Coordinates are mils on a 50-mil grid, origin top-left, +Y down. Scoring
compares the resulting NAMED nets (exact pin membership) against the ground
truth, and every item under "Also checked" is one more scored check — use
exactly the designators, pin assignments and net names the task specifies,
and introduce no other named nets.
