---
description: Emit the MCU pin->net map for a schematic with akcli, optionally cross-checked against an expected pinout/DTS table.
argument-hint: <schematic> [--mcu U3] [--expected pins.csv|.json|pinout.md|board.dts] [-C akcli.toml]
---

Produce the MCU pin → net table for the schematic with `akcli pinmap` and report it.

Arguments: `$ARGUMENTS`
- First token = the schematic (`.SchDoc` / `.kicad_sch`, etc.).
- `--mcu <REF>` overrides the MCU designator (otherwise from config `mcu_designator`).
- `--expected <file>` is an expected pin→signal table to cross-check against. The schematic is
  authoritative; the expected table is advisory.
- `-C/--config <toml>` supplies `mcu_designator` and paths.

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. If the user supplied an **expected** source that is NOT already a `.csv`/`.json` pin→signal
   table — e.g. a Zephyr **`.dts`/`.dtsi`/`.overlay`** or a human **`pinout.md`** — first convert
   it with the built-in extractor:
   `akcli expected <board.dts|pinout.md> -o pins.json`
   (markdown column names can be forced with `--key-header`/`--value-header`), then pass
   `pins.json` as `--expected`. If it is already a `.csv`/`.json` table, pass it through directly.
2. Run the pin map:
   `akcli pinmap <schematic> [--mcu <REF>] [-C <toml>] [--expected <table.csv|.json>] --fail-on never`
   (add `--json` if you want to parse it).

Report:
- The full `pin → net` table for the MCU.
- If `--expected` was used, list mismatches/missing/extra explicitly, and state that the
  schematic is the source of truth (DTS/pinout.md are advisory — flag, don't "fix").
- This command is read-only; do not modify files.
