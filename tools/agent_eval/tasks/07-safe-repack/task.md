# Task: safe re-pack (label-on-pin across blocks)

Draw TWO functional modules that will be MECHANICALLY RE-PACKED afterwards
(`akcli arrange --groups` relocates each block rigidly). Cross-block
connectivity must therefore be **label-on-pin only** — a wire or pin-tip
touch between blocks makes the re-pack unsafe and the harness FAILS the task
when the net-preservation gate refuses it.

- Group `SENSE` (origin `[1000, 1000]`): `R1` (10k) at group-local `[0, 0]`,
  `R2` (10k) at group-local `[0, 600]`. Nets: `R1.1` = `V3V3`,
  `R1.2` + `R2.1` = `NTC_OUT`, `R2.2` = `GND`.
- Group `FILTER` (origin `[4000, 1000]`): `C1` (100n) at group-local
  `[0, 0]`. Nets: `C1.1` = `NTC_OUT`, `C1.2` = `GND`.

After your op-list applies, the harness runs
`akcli arrange --groups --apply` (group_gap 1000, page_width 12000) and then
compares the named nets — both the gate and the ground truth must pass.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
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
