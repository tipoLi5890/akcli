# Task 13 — engineering change order: rename a rail, re-value two parts (edit)

`board.kicad_sch` already holds the pull-ups and decoupling of a 3.3 V I2C
bus, every connection made with a label on the pin:

| Ref | Part | Origin (mil) | Pin 1 net | Pin 2 net |
|---|---|---|---|---|
| `R1` | `Device:R` 4.7k | `[2000, 2000]` | `3V3` | `SDA` |
| `R2` | `Device:R` 4.7k | `[2600, 2000]` | `3V3` | `SCL` |
| `C1` | `Device:C` 100n | `[3200, 2000]` | `3V3` | `GND` |

No part has a footprint yet.

**The edit (ECO).**

1. The rail is renamed: `3V3` becomes `VDD_IO` — on every label that carries
   it. No pin may change nets; the net keeps its three pins under the new
   name.
2. The bus moves to 400 kHz: `R1` and `R2` become `2.2k`, get the footprint
   `Resistor_SMD:R_0402_1005Metric` and a `Tolerance` parameter of `1%`.
3. `C1` is not touched.

Teaches: `rename_net` (protocol 2 fields `old_name` / `new_name`) rewrites
every matching label in one op — the plan must report a harmless
`= RENAME`, never a split or merge; `set_component_parameters` edits `value`,
`footprint` and free-form `parameters` of a placed part without touching
connectivity.

## Also checked

- `R1`, `R2`: value `2.2k`, footprint `Resistor_SMD:R_0402_1005Metric`,
  parameter `Tolerance` = `1%`; `C1` still `100n`.
- `akcli plan` reports exactly one net change, `= RENAME 3V3 -> VDD_IO (3 pins)`,
  and no risk.

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
