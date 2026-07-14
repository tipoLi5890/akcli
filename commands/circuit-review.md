---
description: Read a schematic and run akcli design checks (ERC-lite + power + BOM), then summarize findings — with an optional revision diff.
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
2. Run checks in report mode so a finding doesn't abort the flow:
   `akcli check <schematic> [-C <toml>] --exit-zero`
   (use `--json` if you want to parse/group findings precisely). `check` runs ERC-lite + power +
   power-rail + BOM by default; narrow with `--erc` / `--power` / `--bom` if asked.
3. If a prior revision was given, also run `akcli diff <old> <schematic>` and fold net/component
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
