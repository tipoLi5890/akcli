# Task 14 — fix two parts drawn the wrong way round (edit)

`board.kicad_sch` already holds a fused, reverse-polarity-protected adjustable
regulator. Power flows left to right along `y = 1000`:

| Ref | Part | Origin (mil) | As drawn | Pin tips as drawn (mil) |
|---|---|---|---|---|
| `F1` | `Device:Fuse` 500mA | `[1000, 1000]` | rotation 90 | 1 = `[850, 1000]`, 2 = `[1150, 1000]` |
| `D1` | `Device:D` SS34 | `[2100, 1000]` | rotation 0 | 1 (K, cathode) = `[1950, 1000]`, 2 (A, anode) = `[2250, 1000]` |
| `U1` | `Generic:REG3` | `[3500, 1100]` | rotation 0, mirror `y` | 2 (OUT) = `[3200, 1000]`, 3 (IN) = `[3800, 1000]`, 1 (ADJ) = `[3500, 1400]` |
| `C1` | `Device:C` 100n | `[4400, 1200]` | rotation 0 | 1 = `[4400, 1050]`, 2 = `[4400, 1350]` |
| `R1` | `Device:R` 240 | `[5000, 1200]` | rotation 0 | 1 = `[5000, 1050]`, 2 = `[5000, 1350]` |
| `R2` | `Device:R` 390 | `[5000, 2000]` | rotation 0 | 1 = `[5000, 1850]`, 2 = `[5000, 2150]` |

Wires and labels (they are anchored to COORDINATES, not to pins):

- label `VBAT` at `[850, 1000]` (on `F1.1`);
- a wire `[1150, 1000]` -> `[1950, 1000]` labelled `VBAT_F` (fuse output to
  the diode's LEFT tip);
- a wire `[2250, 1000]` -> `[3200, 1000]` labelled `VIN_PROT` (the diode's
  RIGHT tip to the regulator's LEFT tip);
- label `VOUT` at `[3800, 1000]` (the regulator's RIGHT tip), and on `C1.1`
  and `R1.1`;
- label `VADJ` at `[3500, 1400]` (on `U1.1`) and on the wire `R1.2` -> `R2.1`;
- labels `GND` on `C1.2` and `R2.2`.

**Two parts are the wrong way round:**

1. `D1` sits at rotation 0, so the fuse feeds its CATHODE. Current must flow
   fuse -> anode -> cathode -> regulator.
2. `U1` was dropped in mirrored left-to-right (`mirror: y`), so its OUT pin
   faces the input and its IN pin faces the load.

**The edit.** Turn both parts round IN PLACE — `D1` to rotation `180`, `U1`
back to its library orientation (mirror `none`, rotation 0). Do not move,
delete or re-draw anything: the wires and labels stay where they are and the
pins swap underneath them. `U1`'s ADJ pin lies on the mirror axis, so `VADJ`
(and `VBAT`, `GND`) must come through unchanged.

Target nets: `VBAT` = `F1.1`; `VBAT_F` = `F1.2` + `D1.2`; `VIN_PROT` =
`D1.1` + `U1.3`; `VOUT` = `U1.2` + `C1.1` + `R1.1`; `VADJ` = `U1.1` + `R1.2`
+ `R2.1`; `GND` = `C1.2` + `R2.2`.

Teaches: `set_component_transform` (`rotation` / `mirror`) turns a placed
part about its origin and nothing else follows it — which is exactly the
repair here. The plan shows `! SPLIT` / `! MERGE` lines for the pins that
swap; read them, confirm they are the intended swaps, then apply.

## Also checked

- `D1` at `[2100, 1000]`, rotation 180, mirror `none`; `U1` at
  `[3500, 1100]`, rotation 0, mirror `none`.
- `akcli plan` reports a non-equivalent, RISKY net diff (the intended swaps).
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
