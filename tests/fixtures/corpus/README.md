# Real-board corpus

Boards here are **authored by akcli itself** — each `<name>.kicad_sch` was drawn by
applying the committed `<name>.ops.json` to a blank `akcli new` sheet with the
fixture symbol libraries (`tests/fixtures/kicad/symbols/`), so the corpus is fully
reproducible:

```bash  # doc-noqa
akcli new <name>.kicad_sch
akcli draw <name>.kicad_sch --ops <name>.ops.json \
  --symbols tests/fixtures/kicad/symbols/Device.kicad_sym \
  --symbols tests/fixtures/kicad/symbols/power.kicad_sym --apply --strict-nets
```

The boards deliberately keep their *honest* findings (missing footprints,
off-board nets) — the golden corpus (`tests/golden/`) freezes those outputs, so
a check/review/netlist behavior drift on realistic multi-block circuitry fails CI.

- `analog_frontend` — power entry π-filter (L1/C1/C2), 2× decoupling, I²C
  pull-up pair, reference divider, sensor divider + RC anti-alias filter;
  8 named nets, 13 components, exercises 5 macro ops.
