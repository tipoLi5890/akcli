# Task: I2C bus pull-ups

Draw the two pull-up resistors for a 3.3 V I2C bus (4.7k each):

- `R1`: pin 1 on net `3V3`, pin 2 on net `SDA`
- `R2`: pin 1 on net `3V3`, pin 2 on net `SCL`

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 1, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `Device:R`,
`Device:C`, `Device:C_Polarized`, `Device:L` (all two-pin: pin 1 / pin 2) and
power ports `GND` / `+3V3`. Coordinates are mils on a 50-mil grid, origin
top-left, +Y down; keep parts >= 400 mil apart. Use net labels on pins
(`add_net_label` with `"at": "REF.PIN"`) or `connect_and_label` for
connectivity. Scoring compares the resulting NAMED nets (exact pin
membership) against the task's ground truth — use exactly the designators,
pin assignments and net names the task specifies, and introduce no other
named nets.
