# Task 16 — document a sheet without touching a net (edit)

`board.kicad_sch` already holds a finished, undocumented sensor divider:

| Ref | Part | Origin (mil) | Pin tips (mil) |
|---|---|---|---|
| `R1` | `Device:R` 5.1k | `[2000, 2000]` | 1 = `[2000, 1850]`, 2 = `[2000, 2150]` |
| `R2` | `Device:R` 10k | `[2000, 2600]` | 1 = `[2000, 2450]`, 2 = `[2000, 2750]` |
| `C1` | `Device:C` 100n | `[3800, 2600]` | 1 = `[3800, 2450]`, 2 = `[3800, 2750]` |

Nets: `VIN` = `R1.1`; `VMID` = `R1.2` + `R2.1` + `C1.1`; `GND` = `R2.2` +
`C1.2`. The sheet has no title block and no annotation.

**The edit.** Add the documentation — and ONLY documentation: the netlist
must come through unchanged (the plan's "Net changes" block must read
`(none)`).

1. Title block: title `Sensor divider`, revision `B`, company
   `Example Labs`, comment 1 `ECO-17: documentation only`.
2. A border rectangle framing the divider: it must contain `R1` and `R2`
   (bodies and pin tips) and must NOT contain `C1`. `[1500, 1600]` to
   `[2700, 3000]` works.
3. A free text reading exactly `DIVIDER`, placed just above the frame (e.g.
   at `[1550, 1550]`).
4. A text box holding exactly these two lines (one `\n` between them):

   ```text
   VMID = VIN x 10k / (5.1k + 10k)
   Keep C1 next to the ADC pin
   ```

   e.g. top-left corner `[3000, 1600]`, size `[1600, 400]`.

Teaches: the annotation ops — `set_title_block`, `add_rectangle`,
`add_text`, `add_text_box` — are connectivity-neutral; give the graphics a
stable `key` so a later re-apply replaces them in place.

## Also checked

- The title block holds `title`, `rev`, `company` and `comment1` exactly as
  above.
- A rectangle enclosing `R1` + `R2` (not `C1`) exists; a text `DIVIDER`
  exists; a text box with exactly the two-line note exists.
- `akcli plan` reports an EQUIVALENT net diff (no change of any kind).

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
