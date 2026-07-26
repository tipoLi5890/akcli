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
   Quantitative findings (RC corners, divider ratios) can be simulation-verified with
   `akcli review testbench <schematic>` (read-only; no ngspice → `--deck-only` still documents
   the bench). For a shareable artifact, `akcli review report <findings.json> --format markdown`.
   A `./datasheets` facts store next to the schematic is auto-discovered and upgrades
   heuristic findings to datasheet_backed.
3. Run the structural checks in report mode so a finding doesn't abort the flow:
   `akcli check <schematic> [-C <toml>] --fail-on never`
   (`--exit-zero` is the deprecated alias.)
   (use `--json` if you want to parse/group findings precisely). `check` runs ERC-lite + power +
   BOM + net-hygiene (+ layout-overlap and document integrity for `.kicad_sch`,
   + differential-pair/bus continuity unless `[check].pairs = false`) by default; narrow with
   `--erc` / `--power` / `--bom` / `--nets` / `--layout` / `--integrity` / `--pairs` if asked.
   `--integrity` is the write gate's document checks read-only (dangling endpoints, duplicate
   uuids/designators, unresolved `lib_id`s, instance paths, stacked power ports, a `(sheet …)`
   whose child file is missing, no-connect conflicts) — on a file
   akcli did not write, a hit there is a real defect (hand edit, bad merge, moved library).
4. If a prior revision was given, also run `akcli diff <old> <schematic>` and fold net/component
   changes into the summary.

Summarize for the user:
- **Always surface the metadata caveats** `check` prints (passive-pin ratio, No-ERC suppressed
  count, unnamed-net count, frac-coord presence) — a "0 findings" pass on a board that is mostly
  passive pins is NOT a clean bill of health; say so. The type-confidence demotion is per NET:
  a finding on a fully-typed net keeps full severity even when the board-wide ratio is low.
- **DNP parts satisfy nothing** in `check`/`review`: a `(dnp yes)` bypass cap, pull-up, load
  cap, fuse, diode, TVS, or enable-tie resistor does not silence the matching rule. Do not
  dismiss such a finding as a false positive because the part is drawn on the sheet.
- Group findings by severity, give each a one-line cause + suggested fix, and cite the offending
  `REF.PIN` / net names.
- Note that the schematic is authoritative and these checks are advisory ERC-lite (the secondary
  `kicad-cli` ERC is optional and may be absent).
- Do NOT modify any files; this command is read-only.
