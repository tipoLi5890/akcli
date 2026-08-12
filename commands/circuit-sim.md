---
description: Simulate a schematic with akcli on the bundled ngspice — inspect the SPICE deck, run assertions from a sim.json spec, sweep corners — and report pass/fail per assertion.
argument-hint: <schematic> [--sim sim.json] [--deck-only] [--sweep NAME=v1,v2] [--gnd NET]
---

Simulate the schematic with `akcli sim` and report the results.

Arguments: `$ARGUMENTS`
- First token = the schematic (`.kicad_sch` / `.SchDoc`).
- `--sim <sim.json>` is the assertion spec (stimuli, analyses, asserts). Without it, ask the
  user what behavior to verify, then author a minimal spec (see `docs/sim.md` for the schema).
- `--deck-only` emits the SPICE deck without running — works with no ngspice installed.
- Deck node names are the CANONICAL net names (eeschema driver priority: global >
  power > local); a `.meas` written against an alias fails with "no such vector".
  `--probe`/`--gnd` DO resolve aliases — check `akcli nets` first, or see the
  `akcli-net-naming` skill.
- `--sweep NAME=v1,v2,...` runs corner sweeps (component values or `temp=...`; repeatable).
- `--gnd NET` names the SPICE ground node when it is not `GND`.

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. Check the engine first: `akcli doctor` — if ngspice is missing, fall back to
   `--deck-only` (deck inspection is still useful) and tell the user what to install.
2. Preview the deck: `akcli sim <schematic> --deck-only` — confirm every component resolved
   to a SPICE device (`Sim.*` fields / `models` in sim.json) before trusting any numbers.
   Check the reported findings here, not only the deck text:
   - `SIM_PART_SKIPPED` = the part is NOT in the deck (DNP/`NP`/`OPEN` value,
     `Sim.Enable=0`, a `models` `skip`, a connector, or unmodeled). It is an **ERROR**
     (the run cannot pass) when the part has ≥2 connected nodes and an assertion
     measures one of them — model it or stop asserting on that node.
   - `SIM_VALUE_NORMALIZED` = a multi-token value was trimmed (`10k 1%` → `10k`); say
     which value was actually simulated. `SIM_VALUE_INVALID` (exit 6) = the value is
     not a value (`LED red`, `TBD`, an ohms-marked `600R` on an `L`) — fix the value,
     never work around it. IEC 60062 forms are fine (`2M2`, `1k5`, `4R7`; `1M` = 1 MΩ).
   - `SIM_UNMODELED` = a diode/transistor/IC with no model, commented out of the deck.
3. When an assertion is about an operating point, declare a **single-point `dc` sweep**
   (`"dc": "Vsupply 3.3 3.3 1"`) — an assert bound to an `op` analysis is rejected with
   `SIM_OP_ASSERT_UNSUPPORTED` (exit 2), because `.meas` needs a plot.
4. Run: `akcli sim <schematic> --sim <sim.json> --json` (exit 1 = assertion violations,
   which are findings, not tool errors). Add `--sweep` corners when the user asks about
   tolerance/temperature robustness; add `--wave out.csv` only if the user wants waveforms.

Report:
- Per-assertion pass/fail with the measured vs asserted values, and per-corner results
  for sweeps — quote numbers, don't paraphrase.
- Unresolved/skipped components, normalized values, or a `--deck-only` fallback must be
  stated explicitly — a `PASS` with a skipped part on a measured node is not a pass.
- This command is read-only. The one write path — `akcli sim fit-diode --apply <sch> --write`
  (fitting a `.model` onto the schematic) — is user-triggered only; propose it, never run it
  unprompted.
