# Task 17 — battery-monitor front end from block macros

Draw the analog front end that lets a 3.3 V MCU measure a battery, as four
standard blocks on one sheet:

- **Divider** — `R1` (100k, top) and `R2` (33k, bottom): `R1.1` = `VBAT`,
  `R1.2` + `R2.1` = `VDIV`, `R2.2` = `GND`.
- **Anti-alias RC** — series `R3` (1k) then shunt `C1` (100n): `R3.1` =
  `VDIV`, `R3.2` + `C1.1` = `ADC_IN`, `C1.2` = `GND`.
- **Decoupling** — `C2` (100n): `C2.1` = `3V3`, `C2.2` = `GND`.
- **Pull-up** — `R4` (10k) on the gauge's open-drain alert: `R4.1` = `3V3`,
  `R4.2` = `ALERT_N`.

Suggested block origins (mil): divider `[2000, 2000]`, RC `[3200, 2000]`,
decoupling `[4400, 2000]`, pull-up `[5200, 2000]`.

Each block is one macro op — `place_divider`, `place_rc_filter`,
`place_decoupling`, `place_pullup` (`akcli ops template <macro>` prints the
fields). They place the parts AND the pin labels: a divider's `designators`
are `[top, bottom]` with `values` in the same order; an RC filter's are
`[R, C]`; a pull-up puts the rail on pin 1 and the signal on pin 2. The
stacked two-part macros take `spacing_mil` — 800 to 900 keeps the facing pin
labels from overlapping. Plain `place_component` + `add_net_label` ops score
the same; the macros are simply the shortest correct answer.

## Also checked

- Values: `R1` 100k, `R2` 33k, `R3` 1k, `C1` 100n, `C2` 100n, `R4` 10k.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `Device:R`, `Device:C` (two-pin: pin 1 / pin 2).
Coordinates are mils on a 50-mil grid, origin top-left, +Y down; keep parts
>= 400 mil apart. Scoring compares the resulting NAMED nets (exact pin
membership; auto-numbered power-symbol pins such as `#PWR01.1` are ignored)
against the task's ground truth, and every item under "Also checked" is one
more scored check — use exactly the designators, pin assignments and net
names the task specifies, and introduce no other named nets.
