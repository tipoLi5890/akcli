# Task 20 — fused 3.3 V supply entry with real power symbols

Draw the supply entry of a board fed with regulated 3.3 V through a 4-pin
header: `J1` -> fuse -> the `+3V3` rail with its bulk and bypass capacitors.

Parts and suggested placement (mil):

| Ref | Part | Origin | Orientation | Pin tips |
|---|---|---|---|---|
| `J1` | `Generic:CONN_1x4`, value `PWR_IN` | `[1500, 2000]` | mirror `y` (pins face right) | 1 = `[1800, 1850]`, 2 = `[1800, 1950]`, 3 = `[1800, 2050]`, 4 = `[1800, 2150]` |
| `F1` | `Device:Fuse` 1A | `[2700, 1850]` | rotation 90 | 1 = `[2550, 1850]`, 2 = `[2850, 1850]` |
| `C1` | `Device:C_Polarized` 47u | `[3300, 2150]` | — | 1 (+) = `[3300, 2000]`, 2 = `[3300, 2300]` |
| `C2` | `Device:C` 100n | `[3800, 2150]` | — | 1 = `[3800, 2000]`, 2 = `[3800, 2300]` |

Connectivity:

- **`VIN`** = `J1.1` + `F1.1` — one straight wire, named `VIN`. A header is
  a passive source, so mark the net as driven with a `PWR_FLAG` placed
  MID-WIRE (never on a pin).
- **`+3V3`** = `F1.2` + `C1.1` + `C2.1`, carried by a REAL `power:+3V3`
  symbol: a trunk wire runs from `F1.2` along `y = 1850` and turns down into
  `C2.1`; the power symbol sits above the trunk at `[3300, 1600]` and ONE
  vertical wire drops from it straight through the trunk to `C1.1`. A wire
  that merely CROSSES another does not connect — that crossing at
  `[3300, 1850]` needs an explicit junction.
- **`GND`** = `J1.4` + `C1.2` + `C2.2`, each through a REAL `power:GND`
  symbol (on `C1.2`, on `C2.2`, and at the end of a short stub from `J1.4`).
- `J1.2` and `J1.3` are unused: flag both no-connect.

Useful vocabulary: `place_power_port` (`lib_id` `power:+3V3`, `net_name`
`+3V3`), `place_gnd`, `place_pwr_flag` (`"at": "mid(J1.1,F1.1)"`),
`add_no_connect`, `add_wire`, and `add_junction` — the writer auto-inserts a
junction where a wire END lands on another wire, but never where two wires
only cross.

## Also checked

- `J1.2` and `J1.3` carry no-connect flags.
- `+3V3` and `GND` each carry at least one real power symbol; `VIN` carries a
  `PWR_FLAG`.
- Values: `F1` 1A, `C1` 47u, `C2` 100n.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `Generic:CONN_1x4` (pins 1-4), `Device:Fuse`, `Device:C`,
`Device:C_Polarized` (two-pin: pin 1 / pin 2) and the power symbols
`power:+3V3`, `power:GND`, `power:PWR_FLAG`.
Coordinates are mils on a 50-mil grid, origin top-left, +Y down; keep parts
>= 400 mil apart. Scoring compares the resulting NAMED nets (exact pin
membership; auto-numbered power-symbol pins such as `#PWR01.1` are ignored)
against the task's ground truth, and every item under "Also checked" is one
more scored check — use exactly the designators, pin assignments and net
names the task specifies, and introduce no other named nets.
