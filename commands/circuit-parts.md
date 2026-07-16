---
description: Source a real part (JLCPCB/LCSC), convert it to a KiCad library, and place it on a schematic — jlc search → show → add → plan → draw in one flow. Networked + writes files — user-triggered only.
argument-hint: <board.kicad_sch> "<part request>" [--qty N] [--place REF X Y] [--apply]
disable-model-invocation: true
---

Source a **real, purchasable part** and get it onto a KiCad schematic with `akcli` — the
`jlc search → show → add → plan → draw` chain, each stage verified before the next. This
command **uses the network** (the `jlc` family is akcli's only networked surface) and
**writes library files** (plus the schematic on `--apply`), so it is user-triggered only.

Arguments: `$ARGUMENTS`
- The target must be a KiCad `.kicad_sch`. The part request is natural language
  ("3.3V 500mA LDO, SOT-23-5, basic part") or a concrete LCSC id (`C123456`) / MPN.
- `--qty N` evaluates pricing at build quantity. `--place REF X Y` also places the part
  (designator + mils position). `--apply` writes the schematic (default: dry-run).

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. **Search** — `akcli jlc search "<query>" --json`. Prefer Basic parts (no setup fee),
   in-stock, and the package the user asked for. Show the top candidates with stock/price
   and pick one **with the user** unless the request was already a concrete `C` number.
2. **Inspect** — `akcli jlc show <C-number> --json`. Confirm the package, datasheet link,
   and stock/tier pricing at `--qty`. If a value/rating matters (voltage, tolerance,
   dropout), verify it here — or fetch the PDF via
   `akcli jlc datasheet <C-number> --fetch` and cite page numbers; never guess from the
   part title (see the akcli-parts-sourcing and akcli-datasheet-facts skills).
3. **Convert** — `akcli jlc add <C-number> --footprint-lib <NICKNAME>` where `NICKNAME`
   is a footprint-library nickname the project's `fp-lib-table` actually registers
   (`akcli library audit` verifies; a wrong nickname is KiCad's "footprint not found"
   trap). This writes the KiCad symbol + footprint (+ STEP with `--3d`). Treat the
   conversion as a **claim, not a fact**: read back the symbol with
   `akcli read <out>.kicad_sym` and sanity-check pin count/names against the datasheet.
4. **Author the placement** — with `--place REF X Y` you may let
   `akcli jlc add ... --place --designator REF --at X Y` emit the `place_component`
   op-list directly; otherwise author one (see `/circuit-draw` and the
   akcli-schematic-authoring skill) wiring the new part into the circuit
   (`connect_and_label`, `place_decoupling`, ...). Validate first:
   `akcli ops validate ops.json`.
5. **Plan → draw** — `akcli plan <board> --ops ops.json --symbols <new>.kicad_sym`
   (read the **Net changes** block), then `akcli draw ... ` (dry-run), then only on the
   user's explicit `--apply`: `akcli draw ... --apply --strict-nets`. Re-read and
   `akcli check <board>` after writing; `akcli render <board>` to show what was placed.
6. **Close the loop** — `akcli jlc bom <board> --qty N` to confirm the finished BOM is
   purchasable at quantity, and `akcli library audit` to prove the new library is
   correctly registered.

Never skip a stage: a part that cannot be bought, a symbol that does not match its
datasheet, or an op-list that fails `plan` must stop the flow and be reported.
