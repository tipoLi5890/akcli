# Task 19 — dual op-amp: one buffer, one properly terminated spare

`U1` is a dual op-amp, `EvalParts:OPAMP_DUAL`, drawn as three
units that share the designator:

| Unit | Pins |
|---|---|
| 1 (amplifier A) | 3 = `+` input, 2 = `-` input, 1 = output |
| 2 (amplifier B) | 5 = `+` input, 6 = `-` input, 7 = output |
| 3 (supply) | 8 = `V+`, 4 = `V-` |

Draw all three:

- **Unit 1 — unity-gain buffer.** `U1.3` = `VIN`; the output is fed straight
  back to the inverting input: `U1.1` + `U1.2` = `VOUT`. Route the feedback
  AROUND the symbol body, not through it.
- **Unit 2 — unused, so terminate it.** A floating op-amp input oscillates
  and fails ERC: tie the `+` input `U1.5` to `GND`, the `-` input `U1.6` to
  `VCC`, and flag the output `U1.7` no-connect.
- **Unit 3 — supply.** `U1.8` = `VCC`, `U1.4` = `GND`, each through a REAL
  power symbol (`power:VCC` / `power:GND`), plus a 100n decoupling capacitor
  `C1` next to the supply pins: `C1.1` = `VCC`, `C1.2` = `GND`.

Suggested origins (mil): unit 1 `[3000, 2000]`, unit 2 `[4800, 2000]`, unit 3
`[3000, 3600]`. At `[3000, 2000]` unit 1's tips are `U1.3` = `[2700, 1900]`,
`U1.2` = `[2700, 2100]`, `U1.1` = `[3300, 2000]`.

Useful vocabulary: `place_component` with `"unit": N` places one unit;
`terminate_unused_unit` places a spare unit AND terminates it in one op
(`in_plus` / `in_minus` / `out` are that unit's pin numbers); `place_vcc` /
`place_gnd` drop a power symbol on `"at": "U1.8"`; `place_decoupling` takes
an `anchor` pin plus `offset_mil` instead of absolute coordinates.

## Also checked

- `U1.7` carries a no-connect flag.
- `VCC` and `GND` each carry at least one real power symbol (labels alone do
  not count).
- `C1` is `100n`.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `EvalParts:OPAMP_DUAL` (pins as tabled above), `Device:C`
(pin 1 / pin 2) and the power symbols `power:VCC` / `power:GND`.
Coordinates are mils on a 50-mil grid, origin top-left, +Y down; keep parts
>= 400 mil apart. Scoring compares the resulting NAMED nets (exact pin
membership; auto-numbered power-symbol pins such as `#PWR01.1` are ignored)
against the task's ground truth, and every item under "Also checked" is one
more scored check — use exactly the designators, pin assignments and net
names the task specifies, and introduce no other named nets.
