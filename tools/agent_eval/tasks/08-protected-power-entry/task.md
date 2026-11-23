# Task: protected power entry (fuse + reverse-polarity diode)

Draw a battery power entry that survives a design review: the rail must pass
through a series fuse and then a series diode (reverse-polarity protection)
before reaching its load — the discipline `akcli review analyze` checks with
`REVIEW_FUSE_MISSING` / `REVIEW_REVPOL_UNPROTECTED`.

- `F1` = `Device:Fuse`, value `500mA`, at `[1000, 1000]`.
- `D1` = `Device:D`, value `SS34`, at `[1600, 1000]` — NOTE the pin map:
  pin `1` is the cathode (K), pin `2` is the anode (A). Current must flow
  fuse → anode → cathode → load.
- `C1` = `Device:C`, value `100n`, at `[2200, 1000]` (rail decoupling).
- `R1` = `Device:R`, value `10k`, at `[2800, 1000]` (the load).

Nets (exact): `VBAT` = `F1.1` alone (the raw battery side); `VBAT_F` =
`F1.2` + `D1.2`; `VSYS` = `D1.1` + `C1.1` + `R1.1`; `GND` = `C1.2` + `R1.2`.

## Contract (same for every task)

Author a single akcli op-list JSON document:
`{"protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch", "ops": [...]}`.
It will be validated with `akcli ops validate` and applied to a fresh blank
sheet with `akcli draw --apply --strict-nets`. Available symbols: `Device:R`,
`Device:C`, `Device:C_Polarized`, `Device:L` (all two-pin: pin 1 / pin 2),
`Device:D` (pin 1 = K cathode, pin 2 = A anode), `Device:Fuse` (pin 1 /
pin 2) and power ports `GND` / `+3V3`. Coordinates are mils on a 50-mil
grid, origin top-left, +Y down; keep parts >= 400 mil apart. Use net labels
on pins (`add_net_label` with `"at": "REF.PIN"`) or `connect_and_label` for
connectivity. Scoring compares the resulting NAMED nets (exact pin
membership) against the task's ground truth — use exactly the designators,
pin assignments and net names the task specifies, and introduce no other
named nets.
