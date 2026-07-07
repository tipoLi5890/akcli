# calc-view — local web UI for `akcli calc`

A local dashboard for the 56 standards-cited calculators:
grouped sidebar, auto-generated forms (engineering notation accepted), results
with units and the **formal reference**, plus a physical-style SVG illustration
per calculator (via cross-section, trace cross-section, stackups, LM317
network, 555, I²C bus, RS-485/CAN, flyback, ...). The resistor color-code
diagram colors its bands from the actual result; the SMD-marking chip shows
your code. Pin calculators (★), a recent list, and **shareable URLs** — the
hash carries the calculator and its inputs, and re-runs on load.

## Run

```bash
python3 tools/calc-view/server.py            # opens http://127.0.0.1:8766
python3 tools/calc-view/server.py --port 9000 --no-browser
```

Zero dependencies (stdlib `http.server`), binds to **localhost only**. The
backend imports `altium_kicad_cli.calc` directly from `src/`, so results are
byte-identical to `akcli calc <name> ... --json`.

- `GET /api/list` — the calculator registry (groups, params, defaults, references)
- `GET /api/run?name=<calc>&k=v...` — one computation; errors return `{"error": ...}`
