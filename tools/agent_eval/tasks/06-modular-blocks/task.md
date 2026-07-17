# Task: modular blocks (groups + relative placement + route)

Draw TWO functional modules on one sheet using the groups envelope, then
connect them:

- Group `DIVIDER` (origin `[1000, 1000]`): `R1` (5.1k) at group-local
  `[0, 0]`, `R2` (10k) at group-local `[0, 600]`. Nets: `R1.1` = `VIN`,
  `R1.2` + `R2.1` = `VMID`, `R2.2` = `GND`.
- Group `FILTER` (origin `[3500, 1000]`): `C1` (100n) placed RELATIVE —
  `anchor` on `R2.1` with `offset_mil` `[2500, 0]`. Net `C1.2` = `GND`.
- Route: connect `C1.1` to net `VMID` (`add_net_label` on the pin, or
  `route_net` from `R2.1` to `C1.1` with `label` `VMID`).

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
