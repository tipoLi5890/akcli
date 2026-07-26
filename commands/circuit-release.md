---
description: Run the akcli release gate — every applicable preflight gate (check/intent/contract/library/sch-pcb/fab/order/review/gerber/git, optional sim) with a traceable manifest — and report GO / NO-GO with skipped gates named.
argument-hint: --sch <board.kicad_sch> [--pcb board.kicad_pcb] [--gerbers DIR] [--sim sim.json] [--out manifest.json]
---

Gate a hardware release with `akcli release preflight` and report GO / NO-GO.

Arguments: `$ARGUMENTS`
- The schematic goes in as `--sch <board.kicad_sch>` — `release preflight` takes no
  positional argument (a bare path exits `2`). Every additional input enables its gate: `--pcb`
  (sch-pcb + fab), `--gerbers` (gerber package), `--intent`/`--contract` files,
  `--sim <sim.json>` (quantitative simulation assertions), `--out` (the manifest).
- **Each omitted input is a skipped gate, recorded WITH its reason — never silent.**

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. Gather every input the project has before running — a preflight with four skipped
   gates is honest but weak; ask the user for missing inputs rather than assuming
   they don't exist.
2. Run: `akcli release preflight --sch <sch> [inputs...] --out manifest.json --json`
   (always pass `--out`: the sha256-bound manifest is what makes the release auditable).
3. Exit 1 means **findings, not a tool error** — quote the failing gate's findings
   verbatim. Never re-run with fewer inputs to make it pass.

Report:
- **GO** — every gate PASS; list the skipped gates and why each skip is acceptable.
- **NO-GO** — the failing gate's findings (code + message), the fix path
  (fix / waive-with-reason), and what to re-run.
- State what preflight does NOT cover: CPL/centroid export stays in KiCad, simulation
  runs only if `--sim` was given, and the `check` gate is ERC/power/BOM/nets only — run
  `akcli check <sch> --integrity --fail-on never` separately and report it too.
- This command is read-only except the manifest you explicitly asked for with `--out`.
