---
description: Read a schematic, run the akcli review engine (confidence-graded detectors) plus design checks (ERC-lite + power + BOM + net/layout/pair-continuity), then summarize findings — with an optional revision diff.
argument-hint: <schematic> [against <old-schematic>] [-C akcli.toml]
---

Run a design review of the schematic with `akcli` and report the findings.

Arguments: `$ARGUMENTS`
- First token = the schematic to review (`.SchDoc`, `.SchLib`, `.PcbDoc`, or `.kicad_sch`).
- If the user gave a second schematic (e.g. `against v1.SchDoc`, or a clear "compare to" / "vs"
  intent), treat it as a prior revision to diff against.
- An `-C/--config <toml>` token, if present, supplies rails / MCU designator / `[[erc_waiver]]`.

Steps (use the Bash tool; `akcli` is on PATH when the plugin is installed, otherwise use
`PYTHONPATH=src python3 -m akcli` or `bin/akcli`):

1. Read context: `akcli read <schematic> --md` (and `akcli net <schematic> --json` if you need the
   netlist to explain a finding).
2. Run the review engine first (advisory; exit 0 by design):
   `akcli review analyze <schematic> --json`
   (add `--pcb <board.kicad_pcb>` / `--gerbers <dir>` / `--profile deep` when those inputs
   exist). Read `metadata.trust_summary`: deterministic/datasheet_backed findings are
   trustworthy as printed; heuristic ones need your adjudication before reporting above
   Minor; `detectors_skipped` says what was NOT reviewed — repeat it in the summary.
   `akcli review explain <CODE>` prints any rule's formula + reference for the report.
   Pass `--fail-on <severity>` if the caller wants `review analyze` to exit non-zero at a
   chosen finding severity instead of the default always-exit-0 advisory behavior.
3. Run the structural checks in report mode so a finding doesn't abort the flow:
   `akcli check <schematic> [-C <toml>] --exit-zero`
   (use `--json` if you want to parse/group findings precisely). `check` runs ERC-lite + power +
   BOM + net-hygiene (+ layout-overlap for `.kicad_sch`, + differential-pair/bus continuity
   unless `[check].pairs = false`) by default; narrow with `--erc` / `--power` / `--bom` /
   `--nets` / `--layout` / `--pairs` if asked.
4. If a prior revision was given, also run `akcli diff <old> <schematic>` and fold net/component
   changes into the summary.

Summarize for the user:
- **Always surface the metadata caveats** `check` prints (passive-pin ratio, No-ERC suppressed
  count, unnamed-net count, frac-coord presence) — a "0 findings" pass on a board that is mostly
  passive pins is NOT a clean bill of health; say so.
- Group findings by severity, give each a one-line cause + suggested fix, and cite the offending
  `REF.PIN` / net names.
- Note that the schematic is authoritative and these checks are advisory ERC-lite (the secondary
  `kicad-cli` ERC is optional and may be absent).
- Do NOT modify any files; this command is read-only.
