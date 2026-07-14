---
name: akcli-design-calc
description: >-
  Offline engineering calculators for electronics design via `akcli calc` —
  E-series (IEC 60063) value snapping and resistor combinations, voltage
  dividers, LM317/FB regulator networks, IPC-2221 track width and clearance,
  via parasitics, fusing current, AWG, microstrip/stripline impedance, RF
  attenuators, buck/boost power stages, NE555, op-amp gain pairs, I2C
  pull-ups, crystal load caps, junction thermal, battery life, LDO headroom,
  comparator hysteresis, envelope detectors, resistor color/SMD/EIA-96
  codes, galvanic compatibility. Use whenever a design task needs a computed
  component value or a standards-backed physical check. Triggers on:
  resistor value, E24/E96, divider, LDO feedback, dropout, trace width,
  current capacity, clearance, creepage, via, impedance, attenuator, 555,
  pull-up, load capacitance, heatsink, hysteresis, envelope detector,
  battery runtime, color code, SMD marking.
---

# akcli-design-calc — standards-backed calculators for circuit design

Never do component-value math "from memory": run `akcli calc` and read the
answer **with its citation**. Every result prints the formal source (standard,
datasheet, or textbook) the formula comes from.

```
akcli calc list                 # all calculators, grouped
akcli calc info <name>          # parameters, defaults, the reference
akcli calc <name> k=v ... [--json]
```

Inputs accept engineering notation: `4k7`, `100n`, `35u`, `2M2`, `1e-7`.
`--json` returns `{calc, inputs, results{value,unit,note}, reference}`.

**Input-suffix rule — already-milli units take a bare number.** When a
parameter's declared unit is itself milli-denominated (`battery-life`'s
`capacity` in **mAh**, `i_avg` in **mA**), pass the plain datasheet number:
`battery-life capacity=2500 i_avg=10` means 2500 mAh @ 10 mA. A trailing
engineering `m` there is **rejected** (`ERROR: capacity is already in mAh —
write capacity=2500`) rather than silently applying a compounding 1000× milli.
The generic length unit `m` (meters) is unaffected — `width=5m` still means
5 mm via the milli prefix. (`battery-life`'s default `derating` is 0.8, aligned
with `battery`; override with `derating=`.)

## When to reach for which calculator

| Design moment | Calculator | Source of the formula |
|---|---|---|
| Any resistor/cap value you are about to place | `eseries` (snap to E24/E96) | IEC 60063:2015 |
| Value not purchasable → synthesize from 2–4 parts | `rcombo` | IEC 60063 pool |
| Divider for ADC/FB sensing | `vdivider`, `vdivider-design` | Horowitz & Hill 3rd ed. |
| LM317 / LDO / switcher feedback | `regulator`, `regulator-design` | TI SLVS044Y (LM317) |
| LED current limiting | `led` | Ohm's law + IEC 60063 |
| RC/LC filters, debounce, timing | `rc`, `rc-charge`, `lc`, `reactance` | Horowitz & Hill 3rd ed. |
| Trace must carry X amps | `trackwidth`, `trackcurrent` | IPC-2221B §6.2 |
| HV spacing between nets | `clearance` | IPC-2221B Table 6-1 |
| Via good enough for this current / this edge rate? | `via` | IPC-2221B; Johnson & Graham 1993 |
| Will this trace/wire survive a fault? | `fusing` | Onderdonk (1928), Preece (1884) |
| Wire gauge for off-board cabling | `awg` | ASTM B258-18 |
| Controlled impedance estimate | `microstrip`, `stripline`, `coax`, `twinlead` | Hammerstad–Jensen 1980; Cohn 1954; Pozar |
| Pad/attenuate an RF signal | `attenuator` | Ref. Data for Radio Engineers 6th ed. |
| Buck/boost L, ripple, C_out | `buck`, `boost` | TI SLVA477B / SLVA372C |
| 555 timer RC values | `ne555-astable`, `ne555-mono` | TI SLFS022I datasheet |
| Op-amp gain resistors | `opamp-gain` | TI SLOD006B |
| I²C bus pull-ups | `i2c-pullup` | NXP UM10204 Rev.7 §7.1 |
| MCU crystal C1/C2 | `crystal-caps` | ST AN2867 |
| Will it overheat / need a heatsink? | `thermal` | JEDEC JESD51-2A |
| Battery runtime | `battery` | rule of thumb (advisory) |
| Read/print a resistor marking | `rescolor`, `smdcode` | IEC 60062:2016; EIA-96 |
| Dissimilar-metal contact (connector plating) | `galvanic` | MIL-STD-889C |
| USB/LVDS/RS-485 differential routing | `diffpair` | IPC-2141A §5 |
| "How hot does this existing trace get?" | `tracktemp` | IPC-2221B (solved for ΔT) |
| dBm↔W↔Vrms, mil↔mm, oz↔µm | `convert-power`, `convert-length`, `convert-copper` | IEEE Std 100; NIST SP 811; IPC copper nominal |
| Comparator switching window | `hysteresis`, `hysteresis-design` | TI SLVA954 |
| RS-485 idle-bus bias / CAN termination | `rs485-bias`, `can-termination` | TIA-485-A; ISO 11898-2 |
| Will the LDO cook? | `ldo` | LDO datasheet practice + JESD51 |
| Gate resistor / driver current | `gate-drive` | TI SLUA618A (Balogh) |
| Current measurement shunt | `shunt` | TI SBOA170 |
| Anti-alias / smoothing filter | `sallen-key` | TI SLOA024B; Sallen & Key 1955 |
| ADC bits, noise floor, R-C settling | `adc` | MT-001; 6.02N+1.76 dB |
| Surge protection part | `tvs` | IEC 61000-4-5; Littelfuse guide |
| Fuse rating | `fuse-derating` | Littelfuse Fuseology; IEC 60127 R10 |
| Cap-charging inrush | `inrush-ntc` | TDK/EPCOS NTC guide |
| Antenna/PA impedance match | `lmatch`, `pimatch` | Pozar §5.1; Bowick ch. 4 |
| Isolated supply first cut | `flyback` | Erickson & Maksimović ch. 6 |
| Battery runtime from a datasheet mAh figure | `battery-life` | ANSI C18.1M + mfr. alkaline data (advisory) |
| Comparator window incl. open-drain pull-up | `comparator-hysteresis` | TI SLVA954 |
| Diode peak/envelope detector RC window | `envelope-detector` | Haykin §2.2; AoE 3rd ed. §1.6.6 |
| Enough input voltage for the LDO? | `ldo-headroom` | LDO datasheet V_DO; TI SLVA079 |

**Deliberately missing:** IPC-2152 track current — chart-based licensed
measurement data with no public closed form; this tool refuses to fake it.
Say so when a user asks, and use the conservative IPC-2221 fit
(`trackwidth`/`tracktemp`) instead.

## Tooling

- `akcli calc batch jobs.json` — `{"jobs":[{"calc":...,"params":{...}}]}` in,
  envelope array out; exit 1 if any job failed. Use for sweeps.
- `--md` — paste-ready markdown result table for reports.
- `--ops out.json` — design-type calculators (`vdivider-design`,
  `regulator-design`, `led`, `i2c-pullup`, `crystal-caps`, `hysteresis-design`,
  `sallen-key`, `attenuator`) emit a valid `place_component` op-list with the
  computed standard values filled in: edit coordinates, `akcli plan`, then
  `draw`.
- `akcli view calc` — local web UI (localhost) with SVG illustrations,
  pinned/recent lists and shareable URLs, for humans reviewing your numbers.

## Workflow integration (this is the point)

1. **Before placing a passive** in an op-list (`akcli draw`/`plan`), compute it
   (`vdivider-design`, `led`, `regulator-design`, ...) and use the
   `*_standard` E-series value as the component's `value` field — never the
   ideal number.
2. **During schematic review**, check suspicious values: a resistor that
   `eseries` cannot match within ~1 % on E96 is likely a typo or a
   special-order part — flag it.
3. **Manufacturability**: compare `trackwidth`/`clearance` results against the
   fab's minimums (see the akcli-jlcpcb-capabilities skill) and state both numbers
   in the report.
4. **Cite the source** in any human-facing recommendation — the `reference`
   field is part of the answer, copy it.
5. **Calculator inputs come from the datasheet, not folklore.** Pull the PDF
   first (`akcli jlc datasheet <C-number> --fetch`, then read it): LED V_F
   from the electrical-characteristics table (not "2.0 V"), LDO V_DO into
   `ldo-headroom`, comparator thresholds vs the input common-mode limit,
   battery capacity at the actual load current. Prefer table values; read
   curves for trends only, and say which table row you used.
6. **Turn the computed target into a `sim.json` assertion.** A calculator gives
   you the number you *designed for*; `akcli sim` proves the *drawn* circuit
   actually hits it. After computing (say) a divider midpoint or an RC corner,
   write the same value as a bound and let libngspice check it:

   ```jsonc
   // vdivider-design said v_out ≈ 1.65 V → assert the built sheet agrees
   {"name": "mid", "meas": "MAX v(mid) from=0 to=1m", "approx": "1.65", "tol": "0.02"}
   ```

   Assertion bounds accept the same engineering notation as calc inputs
   (`1.65`, `25m`, `4.7k`), so the calculator's result drops in verbatim — no
   unit juggling. This closes the loop: **calc = intended value, sim = measured
   value, and a mismatch is a non-zero exit.** See the akcli-schematic-authoring skill
   and `docs/sim.md`.
7. **A diode with no SPICE model? Fit one from the same datasheet row.** When
   `akcli sim` reports a diode `unmodeled`, take the forward-voltage table row you
   already read (step 5) and run `akcli sim fit-diode --point 0.37@20m --n-prior 1.05`
   to get a `.model` card — then `--apply <sch> --designator D1 --write` stamps
   `Sim.Device`/`Sim.Params` onto the part so the next `sim` run resolves it. This
   is the **datasheet → model** leg that complements calc → sim; see the
   akcli-parts-sourcing skill for pulling the PDF and `docs/sim.md#akcli-sim-fit-diode--datasheet--spice-model`.

## Honesty rules

- Transmission-line numbers (`microstrip`/`stripline`) are closed-form
  estimates (zero trace thickness); for production impedance control defer to
  the fab's field solver and say so.
- `fusing`, `battery`, and `battery-life` are explicitly advisory estimates
  — never size protection or promise runtime from them without margin.
- IPC-2221 track-width is the conservative classic; IPC-2152 measured data
  allows narrower traces. If the layout is tight, mention that.
- Worst-case regulator output (`regulator` with `vref_min/max`, `tol`) is a
  full corner enumeration; quote min/max, not just typ.

Numerical behavior is cross-checked in the test suite against KiCad's
pcb_calculator (independent implementation, no code shared) and against
published datasheet/handbook values — see `tests/test_calc.py`.
