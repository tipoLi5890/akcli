# Task 18 — MCU clock, status LED and a button pull-up bank

Draw three support blocks for a microcontroller on one sheet:

- **Crystal** — `Y1` (`Device:Crystal`, 8MHz) with two 18p load capacitors
  `C1`, `C2`: `Y1.1` + `C1.1` = `OSC_IN`, `Y1.2` + `C2.1` = `OSC_OUT`,
  `C1.2` + `C2.2` = `GND`.
- **Status LED** — series `R1` (330) into `D1` (`Device:LED`) to ground:
  `R1.1` = `STATUS`, `R1.2` + `D1.2` (anode) = `STATUS_A`, `D1.1` (cathode) =
  `GND`.
- **Button pull-ups** — three identical 10k resistors `R11`, `R12`, `R13` in
  a row: every pin 1 on `3V3`; `R11.2` = `BTN0`, `R12.2` = `BTN1`, `R13.2` =
  `BTN2`.

Suggested origins (mil): crystal `[2500, 2000]`, LED chain `[4200, 2000]`,
first pull-up `[5400, 2000]` with a 500 mil pitch to the right.

The matching macros are `place_crystal` (`designators` = `[Y, C, C]`,
`value` for the crystal, `load_c` for both capacitors), `place_led_indicator`
(`designators` = `[R, D]`; name the internal node with `mid_net`) and
`place_array` (`designator_prefix` + `start_index` + `count`). The first two
label their own pins; `place_array` is placement ONLY — its six pin labels
are yours to add. `spacing_mil` 600 on the crystal and LED macros keeps their
labels clear of each other.

## Also checked

- `Y1` is a `Device:Crystal` valued `8MHz`; `C1`, `C2` are `18p`; `R1` is
  `330`; `D1` is a `Device:LED`; `R11`, `R12`, `R13` are `10k`.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `Device:R`, `Device:C`, `Device:Crystal` (all two-pin: pin 1 / pin 2) and
`Device:LED` (pin 1 = K cathode, pin 2 = A anode).
Coordinates are mils on a 50-mil grid, origin top-left, +Y down; keep parts
>= 400 mil apart. Scoring compares the resulting NAMED nets (exact pin
membership; auto-numbered power-symbol pins such as `#PWR01.1` are ignored)
against the task's ground truth, and every item under "Also checked" is one
more scored check — use exactly the designators, pin assignments and net
names the task specifies, and introduce no other named nets.
