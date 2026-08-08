# RC low-pass with a real ground port

An RC filter (fc ≈ 1 kHz) plus a `power:GND` port — the smallest example
showing the two ways a net gets its name: labels (`IN`/`OUT`) and a power
symbol (`GND`, global by nature; `place_gnd` auto-allocates the `#PWR0n`
reference).

```bash
akcli new board.kicad_sch
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets
akcli nets board.kicad_sch --json
akcli sim board.kicad_sch --deck-only
```

The resulting nets are pinned in `expected_nets.json` and CI re-applies this
op-list and asserts them.

Points to copy:

- `place_gnd` is sugar for `place_power_port` with `power:GND` — the port's
  pin coordinate is its `at`, and its net name (`GND`) merges with every
  same-named label globally. C1's label-on-pin `GND` is the connection; no
  wire touches the port.
- Check the corner with `akcli calc rc r=1.6k c=100n` before changing values.
