# `akcli sim` — simulate a schematic and assert on the result

`akcli sim` turns a parsed schematic into a SPICE deck, runs it through
**libngspice** (the shared library that ships inside KiCad), reads back the
`.meas` measurements, and compares each one against a pass/fail bound you
declare in a `sim.json` file. The output is a measured-value table plus the same
[`Finding`](../src/akcli/report.py) records `akcli check` emits, so a
failed assertion is a normal non-zero exit you can gate CI on.

Nothing here needs KiCad's GUI or a `.cir` you hand-wrote: the deck is generated
from the same normalized net model the rest of the tool uses. If libngspice is
not installed you can still generate and inspect the deck (`--deck-only`).

```bash
# Emit the SPICE deck this schematic would simulate (no engine needed)
akcli sim board.kicad_sch --deck-only

# Run the assertions in sim.json and exit 1 if any fail
akcli sim board.kicad_sch --sim board.sim.json
```

---

## Architecture — the four stages

The command is a thin composition of four independent modules, each with a
pinned contract so they can be tested in isolation:

```
schematic (parsed, with inferred nets)
    │
    ▼
sim/deck.py     build(sch, spec) ─────────────► SPICE deck (one text string)
    │  · net  → SPICE node mapping                 · title line first
    │  · component → device (via models.resolve)   · elements, stimuli,
    │  · stimuli → V / I / B sources                 model cards, .analysis
    │  · unmodeled parts → warnings                 · .end last
    ▼
sim/engine.py   run(deck, commands) ───────────► child subprocess drives
    │  · spawns `python -m ...sim.engine`            libngspice via ctypes
    │  · feeds deck line-by-line (circbyline)        · SendChar → stdout
    │  · runs each analysis + its `meas ...`          · killed on timeout
    ▼
sim/assertions.py  run_commands(spec) ─────────► one analysis command per
    │               parse_meas_output(lines) ────►   configured analysis, each
    │               evaluate(spec, measured) ────►   followed by its meas lines
    ▼
commands/sim.py  measured-value table + findings + exit code
```

- **`sim/deck.py`** is the pure-text *value* layer. It never touches ngspice; it
  walks the schematic's nets, resolves each component to a `DeviceCard`, and
  renders one deck string. See [Model resolution](#model-resolution) below.
- **`sim/engine.py`** is the only stage that loads the shared library, and it
  does so **in a child subprocess** for crash isolation (a malformed deck can
  make ngspice call `abort()` on the whole process, and a pathological transient
  can spin forever). The parent kills the child on timeout and scrapes results
  from its stdout — it never raises for engine trouble.
- **`sim/assertions.py`** loads and validates `sim.json`, builds the engine
  command list (`run_commands`), parses ngspice's `SendChar` output back into
  numbers, and turns the comparison into `Finding`s.
- **`commands/sim.py`** wires the three together and renders the result.

> **Multi-analysis runs are dispatched per analysis.** A single `run` executes
> only the *first* dot-analysis in the deck, so a spec with more than one entry
> in `analyses` (e.g. `tran` **and** `ac`) would leave the second analysis's
> `.meas` with no plot to read — ngspice then prints `Error: meas ac …` and the
> whole run used to be misdiagnosed as an engine failure. `run_commands` instead
> issues one **interactive** analysis command per configured analysis
> (`tran …` / `ac …` / `op`), each immediately followed by the `meas` lines
> whose analysis resolves to it, so every measurement reads the plot its own
> analysis just produced. The deck still carries the `.tran`/`.ac` dot-cards for
> `--deck-only` portability; the engine just drives the analyses explicitly
> instead of relying on `run`.

---

## Engine discovery — where libngspice comes from

`sim/engine.available()` returns the first shared library it can actually
`dlopen`, searched in this order:

1. **`AKCLI_NGSPICE`** environment override. Set it to an absolute path to a
   libngspice shared object to force that one. Set it to `0`, `off`, `none`,
   `false`, or empty to **disable simulation entirely** (`available()` returns
   `None`, so `akcli sim --sim ...` exits `7`). An override path that will not
   load is a dead end — the search does **not** fall through to another library.
2. **macOS KiCad bundle** — `…/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib`
   and the `Frameworks/` variant.
3. **`ctypes.util.find_library("ngspice")`** — the platform loader's own search.
4. **Linux sonames** — `libngspice.so.0`, then `libngspice.so`.
5. **Windows KiCad** — newest `C:/Program Files/KiCad/*/bin/libngspice*.dll`.

Every candidate is verified by actually loading it, so a stale path never wins.
When nothing loads, `akcli sim` (run mode) prints
`ERROR: NGSPICE_MISSING: …` and exits `7`.

```bash
# Force a specific library
AKCLI_NGSPICE=/opt/ngspice/lib/libngspice.so.0 akcli sim board.kicad_sch --sim s.json

# Prove the engine-free plan path even on a machine that has KiCad
AKCLI_NGSPICE=off akcli sim board.kicad_sch --deck-only    # still works (no engine)
```

---

## `--deck-only` — the engine-free plan mode

`--deck-only` runs stages 1–2's *build* step and stops: it renders the deck and
exits `0` without ever touching libngspice. Use it to review exactly what would
be simulated, to hand a deck to another SPICE tool, or on a CI runner that has
no ngspice. Unmodeled/dangling warnings print to stderr but **never** fail
deck-only.

```bash
$ akcli sim board_v8.kicad_sch --deck-only
* akcli sim: board_v8.kicad_sch
R1 _3V3 MID 10k
R2 MID 0 10k
C1 MID 0 100n
.end
```

Write it to a file with `--out deck.cir`, or get a structured envelope with
`--json`:

```bash
$ akcli sim board_v8.kicad_sch --deck-only --json
{
  "schema_version": "1",
  "deck_sha": "d841800352a7",
  "deck": "* akcli sim: board_v8.kicad_sch\nR1 _3V3 MID 10k\n…\n.end\n",
  "warnings": [],
  "unmodeled": [],
  "parts_skipped": []
}
```

`parts_skipped` lists **every** part the deck left out — skipped *and* unmodeled
— as `{designator, status, reason, nodes, measured}`, where `measured` is `true`
when that part sits in series on a node an assertion measures (`SIM_PART_SKIPPED`
in the findings table below). The same field appears in the full-run `--json`
envelope and in each `--sweep` corner.

`deck_sha` is a short SHA-1 of the deck text — a stable fingerprint you can use
to tell whether a schematic edit actually changed what gets simulated.

---

## `sim.json` — the assertion spec

The full format is validated by
[`sim/assertions.load()`](../src/akcli/sim/assertions.py), with
[`schemas/sim.schema.json`](../schemas/sim.schema.json) as the published
shape contract (draft 2020-12, byte-identical mirror packaged under
`src/akcli/schemas/`). The loader is the authority: some cross-field rules —
such as the exactly-one-of `meas`/`when` requirement per assert — are
enforced only there, so a document can pass the schema and still be rejected
by `load()`. Unknown
keys are rejected at the top level and inside each `assert` entry; `stimuli`
entries and `options` are more permissive — only `stimuli[].name` and
`options.rshunt` are actually validated, so other keys there are accepted
without error.

```json
{
  "protocol_version": 1,
  "stimuli": [
    {"kind": "vsource", "name": "supply", "node": "+3V3", "node2": "0", "value": "dc 3.3"}
  ],
  "analyses": {"tran": "5u 100m", "ac": "dec 40 10 100k"},
  "models": {
    "D1": {"model_card": ".model DBAT D(IS=2.4e-8 N=1.05 RS=0.84 CJO=50p)", "model_name": "DBAT"}
  },
  "assert": [
    {"name": "vpeak", "meas": "MAX v(peak) from=20m to=60m", "gt": "0.35"},
    {"name": "t_detect", "when": "v(peak)=0.297 RISE=1", "lt": "25m"},
    {"name": "idle", "meas": "MAX v(peak) from=0 to=19m", "lt": "0.05"}
  ],
  "options": {}
}
```

| Key | Type | Meaning |
|---|---|---|
| `protocol_version` | integer, **required**, `1` | Spec version. A value greater than this build understands raises `PROTOCOL_MISMATCH`. |
| `stimuli` | array of objects | Independent sources injected into the deck (see below). |
| `analyses` | object `str → str` | ngspice analysis name → its argument text, e.g. `{"tran": "5u 100m"}`. Rendered into the deck as `.tran 5u 100m`. |
| `models` | object | Per-device model overrides keyed by designator or `lib_id` (see [Model resolution](#model-resolution)). |
| `assert` | array of assert objects | The pass/fail checks (see below). |
| `options` | object | `extra_cards` (extra `.model`/`.subckt` text), `inline_analyses` (default `true`), `wave_vectors` (default `all`; an explicit list names the `--wave` CSV columns), `rshunt` (floating-node fix — see [Troubleshooting](#troubleshooting--floating-nodes-and-the-auto-rshunt-fix)). |
| `acknowledged_unmodeled` | array of strings | Designators explicitly accepted as absent from the deck (typically ICs with no vendor SPICE model). Downgrades that part's in-series `SIM_PART_SKIPPED` escalation from ERROR to WARNING — visible but non-blocking; unlisted parts keep the full ERROR. A stale entry (the part is modeled, or gone) is reported as `SIM_UNMODELED_ACK_UNUSED` (NOTE finding, non-blocking) so blanket lists cannot rot silently. |

### Stimuli

Each stimulus becomes one source line in the deck. Every stimulus **must** carry
a `name` that is a bare identifier matching `^[A-Za-z][A-Za-z0-9_]*$` and unique
across all stimuli — it becomes the SPICE element designator, so a missing name,
a leading digit (`"3V3"`), or an embedded space would corrupt the element line;
`load` rejects those with `BAD_CONFIG`. `node`/`node2` are matched against net
names (case-insensitively, via the net's names/aliases); `node2` defaults to
ground (`0`). A `node` that matches no net still emits (handy in `--deck-only`
mode) but is reported as a `SIM_UNKNOWN_STIMULUS_NODE` warning (non-blocking)
with close-match suggestions so a typo is obvious.

| `kind` | Emits | Fields |
|---|---|---|
| `vsource` | `V<name> n1 n2 <value>` — the `V` is prefixed only when `name` does not already start with it (`"supply"` → `Vsupply`, `"Vsup"` → `Vsup`); same rule for `I`/`B` below | `value` e.g. `"dc 3.3"`, `"SIN(0 1 1k)"` |
| `isource` | `I<name> n1 n2 <value>` | `value` |
| `bsource` | `B<name> n1 n2 I=<expr>` | `quantity` (`I`/`V`, default `I`), `expr` |

> **Gotcha the deck builder absorbs for you:** a B-source expression has **all
> whitespace stripped** before it is emitted — libngspice's parser treats
> `I=expr` as a single token and mis-parses embedded spaces. Write
> `"0.2m + 0.1m*sin(2*pi*100*time)"` in the JSON and it lands as
> `I=0.2m+0.1m*sin(2*pi*100*time)`.

### Asserts

Each assert names exactly **one** measurement source and its bound(s).

- **Source** — either `meas` (verbatim text after `meas <analysis> <name>`, e.g.
  `"MAX v(peak) from=20m to=60m"`) or `when` (shorthand for a `WHEN`
  measurement, e.g. `"v(peak)=0.297 RISE=1"` → `meas <analysis> <name> WHEN
  v(peak)=0.297 RISE=1`). Supplying both, or neither, is a `BAD_CONFIG` error.
- **Bound** — a single one of `gt` / `lt` / `ge` / `le` / `approx`, **or** a
  two-sided range: one lower bound (`gt` *or* `ge`) together with one upper bound
  (`lt` *or* `le`) in the same entry, e.g. `{"ge": "3.0", "le": "3.6"}` for a
  3.3 V rail window. You may not pair two lowers (`gt`+`ge`) or two uppers
  (`lt`+`le`), and `approx` stays exclusive — it cannot be combined with any of
  the four inequalities. `approx` additionally accepts `tol` (relative, default
  `0.05` = 5%). When a two-sided assert fails, `evaluate()` names the side that
  was violated. Bound values accept engineering notation (`"25m"`, `"4.7k"`,
  `"1e-7"`, `"100N"`) parsed with the same rules as `akcli calc` — prefixes are
  case-insensitive except `m` (milli) vs `M` (mega), and `inf`/`nan`/`1_000`
  are rejected rather than silently accepted by `float()`.

  > **Best practice: never leave a critical assert upper-bound-only.** An
  > upper bound alone (`{"lt": "50m"}`) is satisfied *vacuously* by a dead
  > branch — delete the load component and the measurement reads exactly 0,
  > which "passes". Pair it with a lower bound (`{"gt": "1m", "lt": "50m"}`)
  > or use `approx`+`tol` so a broken circuit fails instead of green-lighting.
  > `evaluate()` backstops the vacuous case with a `SIM_SUSPICIOUS_ZERO`
  > warning when an upper-bound-only assert measures exactly 0.
- **Analysis** — inferred automatically, or pinned with an explicit `analysis`
  key (which must name a configured analysis). Inference order:
  1. explicit `analysis` key;
  2. a `when` source, or a `from=`/`to=` window → `tran`;
  3. a measurement whose text *starts with* `FIND` → `ac`, but **only if** the
     spec configures an `ac` analysis (the test is the leading `FIND` token
     alone, so a transient `FIND … WHEN` lands on `ac` too — pin it with an
     explicit `analysis`);
  4. otherwise → `tran`.

  If the inferred analysis was never configured in `analyses`, the run fails
  with `BAD_CONFIG` (exit 2) when the engine command list is built — *not*
  inside `load`, so `--deck-only` still succeeds on such a spec, and on a host
  without libngspice the engine check fires first (`NGSPICE_MISSING`, exit 7).
  Add the analysis, or pin the assert to one you declared.

> **`op` analyses cannot carry assertions.** ngspice's `meas` reads a *plot*, and
> an operating-point run produces none, so `meas op …` always errors — the whole
> run then surfaced as a generic engine failure (exit 7). An assert bound to `op`
> (explicitly, or because `op` is the only analysis declared) is therefore
> rejected up front with `SIM_OP_ASSERT_UNSUPPORTED` (exit 2). To assert on an
> operating point, use a **single-point `dc` sweep**, which does support `.meas`:
>
> ```json
> {"analyses": {"dc": "Vsupply 3.3 3.3 1"},
>  "assert": [{"name": "vmid", "meas": "FIND v(mid) AT=3.3",
>              "analysis": "dc", "approx": "1.65", "tol": "0.02"}]}
> ```
>
> An `op` analysis with **no** asserts is still fine (it runs, and its node
> voltages show up in a `--wave` capture).

---

## Model resolution

Before the deck builder can emit an element line for a component, it asks
[`sim/models.resolve(comp, spec)`](../src/akcli/sim/models.py) what
SPICE primitive (if any) that component should become. The answer follows a
strict **first-hit-wins ladder**:

1. **`Sim.*` symbol fields** — the KiCad-native convention, the user's explicit
   intent. Recognized fields: `Sim.Device` (`R`/`C`/`L`/`D`/`Q`/`SUBCKT`/…),
   `Sim.Name` (model/subckt name), `Sim.Params` (value/parameters),
   `Sim.Pins` (SPICE-terminal → symbol-pin remap, e.g. `"1=2 2=1"`), and
   `Sim.Enable=0` to skip the part. A `Sim.Device=SUBCKT` / `Sim.Name` that names
   a builtin subcircuit pulls in its `.subckt` card automatically.
2. **`spec.models` override** — a `sim.json` entry keyed by designator (`"D1"`)
   or `lib_id`, carrying `device`, `params`/`value`, `model_card`, `model_name`,
   `pin_order`, or `skip`.
3. **Prefix + value heuristic** — `R`/`C`/`L` with a parseable value become
   passives; connectors / test-points / mechanical (`J`, `TP`, `MP`, `#…`, …)
   are skipped; **a diode, transistor, or IC with no model is returned
   `unmodeled` — never guessed at.** An unmodeled part is commented out of the
   deck and reported as a `SIM_UNMODELED` warning, so a missing model is loud,
   not a silent lie.

> **Nothing leaves the deck silently.** Every part the deck does not emit —
> skipped (`Sim.Enable=0`, a `spec.models` `skip`, a connector/test-point, a
> `DNP`/`NP`/`OPEN` value) *or* unmodeled — is listed in the `parts_skipped`
> field of the `--json` output. A **skipped** part always also raises a
> `SIM_PART_SKIPPED` finding; an **unmodeled** part raises `SIM_UNMODELED`
> instead, and adds `SIM_PART_SKIPPED` only when it is in series on a measured
> node. When such a part has
> two or more connected nodes and one of them is **measured** by an assertion, the
> finding is an **ERROR** and the run cannot pass: a skipped `R2` in a measured
> divider changes the very value being asserted, so a `PASS` there would be
> meaningless. Model it (`Sim.Device`/`Sim.Params` or a `spec.models` entry),
> stop asserting on that node, or — when the part genuinely has no obtainable
> model (a PMIC, a radio module) and the assertion is still worth running —
> list it in the spec's top-level `acknowledged_unmodeled` array to downgrade
> exactly that designator's escalation to WARNING. The acknowledgment is
> per-designator, version-controlled with the spec, and audited: an entry that
> stops matching an absent part is reported as `SIM_UNMODELED_ACK_UNUSED`
> (NOTE, non-blocking).

> **Diode/BJT terminal order comes from pin NAMES, not pin numbers.** SPICE node
> order is positional and semantic (`D <anode> <cathode>`, `Q <collector> <base>
> <emitter>`), but KiCad's stock symbols number diode pins `K`=1 / `A`=2 — so
> trusting pin *numbers* silently reverses polarity (an envelope/peak detector
> charges the wrong way). When a diode or transistor is emitted without an
> explicit `Sim.Pins`/`pin_order`, `resolve` reorders its terminals by pin
> **name** (`A`/`K`, `C`/`B`/`E`, case-insensitive). If the names cannot identify
> the terminals, it keeps schematic pin-number order and the deck emits a
> `SIM_PIN_ORDER_ASSUMED` warning — set `Sim.Pins` or a `spec.models` `pin_order`
> to fix the polarity.

> **`pin_order` also selects WHICH pads reach SPICE, not just their order.** A
> lumped equivalent on a multi-pad part — say a 3-pad connector modeled as a
> 33 nF capacitor via `{"device": "C", "value": "33n"}` — must name exactly the
> two pads that are the capacitor's terminals: `{"device": "C", "value": "33n",
> "pin_order": ["1", "3"]}`. Without it the deck would emit every schematic pin
> (`CJ1 N1 N2 N3 33n` — a 3-node capacitor), which ngspice rejects with a bare
> `circuit not parsed`. The deck builder refuses that shape up front:
> `SIM_NODE_COUNT_MISMATCH` (exit 6) fires whenever the node count disagrees
> with the device letter's fixed terminal count (`R`/`C`/`L`/`D`/`V`/`I`/`B`
> take 2, `Q` takes 3; `X` subcircuits are exempt — their port count comes from
> the model).

### The builtin behavioral library

`sim/builtin.lib` ships six clean-room behavioral subcircuits, loaded
automatically when `Sim.Name`/`model_name` references one — enough for
sanity-level transient/AC runs, never a substitute for a vendor model:

| Subcircuit | Ports | What it models |
|---|---|---|
| `AKCLI_COMP555` | `GND TRIG OUT RESET CTRL THRES DISCH VCC` | NE555 astable/monostable core (⅓/⅔ VCC comparators, SR latch, discharge sink) |
| `AKCLI_COMPARATOR` | `inp inn out vcc` | LM393-style open-collector comparator |
| `AKCLI_OPAMP` | `inp inn out vcc vee` | single-pole op-amp: A0 = 100 dB, GBW ≈ 1 MHz, soft rail clamp, 75 Ω output |
| `AKCLI_NMOS_SW` | `d g s` (`vth=2.0 ron=0.1`) | enhancement NMOS as a smooth Ron/Roff switch |
| `AKCLI_PMOS_SW` | `d g s` (`vth=2.0 ron=0.1`) | the PMOS complement |
| `AKCLI_PHOTOTRANS` | `c e` (`gain=1`) | phototransistor collector-emitter path |

The op-amp and FET switches are engine-validated in CI (a unity-gain buffer
must track its input; the switch must actually switch) — the models are tested
against ngspice, not just inspected as text.

### The `M`/`MEG` value fix (and other deck gotchas)

`spice_value()` (inside `models.py`) is where engineering notation becomes a
SPICE-safe value, and it resolves the notorious **`M` collision**: in the KiCad
values this tool reads, `M` means **mega** (a "1M" resistor is 1 MΩ), but in
SPICE `M` means **milli** and mega must be written `MEG`. The tool parses with
the mega meaning and renders SPICE `MEG`, so a `1M` resistor lands as `1MEG` and
a `25m` value stays `25m`. Every other prefix is read case-insensitively (`10U`,
`100N`, `1T`) and re-rendered in canonical SPICE spelling. It also strips
trailing unit words (`4.7kohm`, `100nF`, `10uH`, `10UF`).

#### The value gate — what an `R`/`C`/`L` value may say

`spice_value()` is deliberately lenient (text it cannot parse passes through), so
every value that is actually **emitted** as a passive goes through
`models.passive_value()` first. Understood forms:

| Written | Emitted | Note |
|---|---|---|
| `4700`, `4.7k`, `100n`, `1e-7` | unchanged | plain engineering notation |
| `1M` | `1MEG` | KiCad mega → SPICE mega |
| `2M2`, `1k5` | `2.2MEG`, `1.5k` | IEC 60062 embedded decimal |
| `600R`, `4R7` | `600`, `4.7` | IEC 60062 `R` = decimal **and** ohms marker |
| `10U`, `100N`, `4U7`, `1T`, `10K` | `10u`, `100n`, `4.7u`, `1T`, `10k` | prefixes are **case-insensitive** — except `m`/`M` (see below) |
| `1μF`, `1µF` | `1u` | both micro signs: U+03BC greek mu and U+00B5 micro sign |
| `4.7 k`, `220 ohm`, `100 nF` | `4.7k`, `220`, `100n` | a unit/prefix split off by a space is re-joined, never dropped |
| `10k 1%`, `100n 50V X7R` | `10k`, `100n` | trailing rating/tolerance tokens dropped → `SIM_VALUE_NORMALIZED` warning |
| `DNP`, `NP`, `OPEN`, `NO POP` | *(nothing)* | deliberately unpopulated → the part is skipped |
| `LED red`, `TBD`, `1%` | — | **`SIM_VALUE_INVALID`** (exit 6): not a value |
| `1_000`, `inf`, `nan`, `1e400` | — | **`SIM_VALUE_INVALID`**: Python's `float()` accepts all four; a circuit can mean none of them (see below) |
| `600R` on an **`L`** | — | **`SIM_VALUE_INVALID`**: an ohms-marked value is an impedance rating, not an inductance — a ferrite bead's `600R` is 600 Ω at its test frequency, never 600 H (the same rule rejects a farad-marked value on an `R`, etc.) |

**Case.** SPICE is case-insensitive and Altium libraries routinely carry
upper-cased values, so `10U` = `10u`, `100N` = `100n`, `1T` = `1t`, `10G` =
`10g`. The single exception is the pair that *only* case distinguishes:
lowercase `m` is **milli**, uppercase `M` is **mega** (KiCad convention) — `2M2`
is 2.2 MΩ, `2m2` is 2.2 mΩ. Both are re-rendered into unambiguous SPICE
(`2.2MEG` / `2.2m`), so ngspice never has to guess either.

> ### ⚠️ Why this is a hard gate
> `"10k 1%"` used to reach ngspice verbatim, where it parses as **10** — a
> 10k/10k divider off 3.3 V then measured **3.30 V** instead of 1.65 V, and the
> run exited `0` with `PASS`. `1_000` was the same trap wearing a number's
> clothes: Python's `float()` reads it as 1000, ngspice reads it as **1**, and a
> 10k/`1_000` divider measured **3.3e-4 V** with no warning at all. `inf`/`nan`
> and the silently-overflowing `1e400` went straight into the deck too. A
> confident wrong number is worse than an error, so a value the tool cannot read
> *the same way ngspice will* now refuses to simulate — and the refusal lists the
> formats it does accept.

Two more hard-won gotchas the deck builder handles so you never see them:

- **Line 1 is a TITLE line.** ngspice always treats the first deck line as a
  comment/title, so the builder emits `* akcli sim: <source>` first and your
  real elements start on line 2.
- **To ngspice itself, `1M` means milli and mega must be written `1MEG`** — the
  opposite of the KiCad value convention, which is why every emitted value goes
  through `spice_value()` (see above) and the deck builder never re-implements it.

### Fitting a diode from a datasheet — `fit_diode`

When a diode has no ready SPICE model, `models.fit_diode()` turns datasheet
forward-voltage points into a `.model` line you can paste into `spec.models`
(or `options.extra_cards`). It takes `(V_F, I_F)` points, an ideality prior
`n_prior` (default `1.05`, Schottky), an optional high-current `rs_point` for
series resistance, and an optional `cjo` junction capacitance. Every forward
point (and `rs_point`) must be positive and finite on both axes — a sign typo or
a swapped `V@I` (`1m@0.3`) is rejected with `BAD_CONFIG` rather than silently
producing a physically impossible model (negative or absurd `IS`). It is exposed on
the command line as **[`akcli sim fit-diode`](#akcli-sim-fit-diode--datasheet--spice-model)**
(below), which can also write the fitted model straight back onto a schematic.

**The table-beats-curve rule:** with a *single* datasheet point plus the
ideality prior, IS is solved directly. This is the deliberate, honest default —
because a two-point fit off eyeballed *curve* coordinates routinely lands IS
wildly wrong.

> ### ⚠️ The live IS-1000× story
> While modelling a BAT54H Schottky for the spiro rev C demod channel, a
> two-point fit taken from the datasheet's *I–V curve* (Fig. 2) produced
> `IS ≈ 1e-5` — about **1000× too large**. Its exaggerated reverse leakage
> drained the peak-detector hold cap every carrier period, so the simulated
> detector never latched. Re-fitting from the datasheet *table* row
> (`V_F ≤ 0.37 V @ I_F = 20 mA`) with the Schottky prior `N = 1.05` gave
> `IS ≈ 2.4e-8` and a working channel. **When the datasheet gives you a table
> row, use it; treat curve coordinates as eyeball-grade.** `fit_diode` attaches
> a `note` warning whenever a multi-point fit's `N` is clamped or disagrees with
> the prior by more than 30%.

---

## Output and exit codes

In **run mode** the text output is a metadata header (engine path + short deck
SHA-1), an always-printed measured-value table, then the findings:

```
# akcli sim
  engine: /Applications/KiCad/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib
  deck sha1: 2da713640306

measured values:
  mid                              1.65   ~1.65 (tol 2%)         PASS

# metadata
  ...                                       (standard report metadata block)
# findings (0)
  (none)
```

`--json` returns `{schema_version, deck_sha, engine, measured, findings, parts_skipped, ok}`.

### `--wave OUT.csv` — the clean waveform CSV

`--wave OUT.csv` writes the simulated vectors to a **tidy CSV**: a single scale
column followed by one verbatim column per vector, in the order requested. ngspice's
raw `wrdata` format repeats the scale in front of *every* vector (`t v1 t v2 …`);
`sim/wave.rewrite_wrdata()` collapses those redundant scale columns into one. The
scale column is labelled `time` for a transient/op run and `frequency` for an `ac`
analysis (with multiple analyses, `--wave` captures the last one, and the label
tracks it). Column names come from an explicit `options.wave_vectors` list — that
list *is* the CSV header — so for named columns you must set it:

```bash
akcli sim board.kicad_sch --sim board.sim.json --wave wave.csv
```

```
time,v(MID)
0.00000000e+00,1.65000000e+00
1.00000000e-07,1.65000000e+00
2.00000000e-07,1.65000000e+00
…
```

With no explicit `wave_vectors` (the `all` default, where ngspice chooses the
column set and their names are not known ahead of time) the raw `wrdata` file is
moved through verbatim instead of being re-shaped. A `--wave` path containing
spaces is handled (ngspice `wrdata`s to a fixed name, then the file is moved).

| Exit | When |
|---|---|
| `0` | All assertions passed (or `--exit-zero`, or `--deck-only`). |
| `1` | At least one assertion failed, a measurement never produced a result, or the deck raised an ERROR-severity finding (`SIM_PART_SKIPPED` — a part missing from a measured node). |
| `2` | Usage/config error — neither `--sim` nor `--deck-only` given, a `BAD_CONFIG` in `sim.json`, or `SIM_OP_ASSERT_UNSUPPORTED` (an assertion bound to an `op` analysis). |
| `4` | `sim.json` file not found. |
| `6` | Deck build failure — `SIM_NO_GROUND` (no ground net; pass `--gnd <net>`), `SIM_NODE_COLLISION` (two nets map to one node — including one name reused in two sheet scopes), `SIM_NODE_COUNT_MISMATCH` (an element would be emitted with the wrong number of nodes for its device letter — add a `pin_order` naming the SPICE terminals), `SIM_VALUE_INVALID` (a component value SPICE cannot take), or a `PROTOCOL_MISMATCH`. Also returned by a failed `fit-diode --apply --write`. |
| `7` | libngspice missing (`NGSPICE_MISSING`) or an engine failure/timeout (`NGSPICE_FAILED`). A deck that ngspice cannot parse (e.g. `Error: circuit not parsed.`) is an engine failure: the run exits `7` with the parse error on stderr — it is never mistaken for a clean pass, even for a zero-assertion spec. |

`--exit-zero` forces `0` even when assertions fail (useful for reporting-only
runs). Deck `WARNING`-severity findings (unmodeled parts, dangling pins) render
but never change the exit code; deck `ERROR`-severity findings **do** — a part
dropped from a measured node means the numbers above it are not the circuit you
asked about. Config `[[waiver]]` entries (discovered from `akcli.toml` or an
explicit `-C`) apply to single-run sim findings exactly as they do to `check`
findings (`--sweep` corners are evaluated **without** waivers) —
a reviewed, reasoned acceptance can demote or drop a finding by `code`/`refs`;
for the specific "this IC has no obtainable model" case prefer the spec's
`acknowledged_unmodeled` list, which travels with the sim spec itself.

Under `--json`, a failing exit still prints machine-readable JSON on stdout:
`{schema_version, error: {code, message, exit, remediation}}` (e.g.
`NGSPICE_MISSING`/`BAD_CONFIG`/`SIM_NO_GROUND`), instead of leaving stdout
empty.

Findings raised by the sim pipeline:

| Code | Severity | Meaning |
|---|---|---|
| `SIM_ASSERT_FAIL` | ERROR | Measurement ran but violated its bound. |
| `SIM_MEAS_FAILED` | ERROR | The measurement itself failed (e.g. a `WHEN` edge that never crossed) or produced no result. |
| `SIM_SUSPICIOUS_ZERO` | WARNING | An upper-bound-only assert (`lt`/`le` with no `gt`/`ge`) measured **exactly** 0 — consistent with an open circuit (the measured branch lost its load component), which satisfies any upper bound vacuously. The PASS stands and the exit code is unchanged, but treat it as unverified: check the branch is populated, or make the assert falsifiable by pairing the upper bound with a `gt`/`ge` lower bound or using `approx`. (In `release preflight --sim`, assertion-layer findings like this one fail the gate even at WARNING — deck-build warnings such as `SIM_UNMODELED` do not; waivable via `[[waiver]]`.) |
| `SIM_UNMODELED` | WARNING | A component had no SPICE model and was omitted from the deck. |
| `SIM_PART_SKIPPED` | WARNING / **ERROR** | A part is absent from the deck. Every *skipped* part (`Sim.Enable=0`, a spec `skip`, connector/test-point, `DNP`) raises this at WARNING; an *unmodeled* part raises `SIM_UNMODELED` instead and gets this code only via escalation. Escalation to ERROR (both statuses): the absent part has ≥2 connected nodes and one of them is measured by an assertion — the measurement excluded a series element, so the run cannot pass. Designators listed in the spec's `acknowledged_unmodeled` keep the escalated finding at WARNING (visible, non-blocking). Every absent part is also listed in the `parts_skipped` JSON field. |
| `SIM_UNMODELED_ACK_UNUSED` | NOTE | An `acknowledged_unmodeled` entry matched no part absent from the deck — the part is modeled now, or no longer exists. Remove the stale entry so a future regression on that designator is not pre-forgiven. |
| `SIM_VALUE_NORMALIZED` | WARNING | A component value carried trailing tokens beyond the value itself (`"10k 1%"`, `"100n 50V X7R"`); only the leading value reached ngspice. Move ratings/tolerance into their own field if that is not what you meant. |
| `SIM_DANGLING_PIN` | WARNING | A pin sat on no net; tied to a unique `NC_<ref>_<pin>` node. |
| `SIM_BAD_STIMULUS` | WARNING | A stimulus had an unknown `kind` and was skipped. |
| `SIM_PIN_ORDER_ASSUMED` | WARNING | A diode/transistor's pin names could not identify the SPICE terminal order; it was emitted in schematic pin-number order — verify polarity. |
| `SIM_UNKNOWN_STIMULUS_NODE` | WARNING | A stimulus `node` matched no net; a new dangling node was created (suggestions offered). |
| `SIM_FLOATING_NODE` | WARNING | A net has no DC path to ground (only a cap/nothing left after skips) — ngspice would return a singular matrix. Auto-`rshunt` (below) makes it solvable. |
| `SIM_UNDRIVEN_RAIL` | WARNING | A power-named net (`+*`/`VCC*`/`VDD*`/`VBAT*`/`VSUP*`) has neither a vsource stimulus nor any emitted element with a driving device letter (`V`/`I`/`B`/`X` — a rail fed by an `X` subcircuit regulator is exempt) while the spec has stimuli — a silent read-≈0 trap (forgot the rail source). |
| `SIM_ZERO_PASSIVE` | WARNING | An `R`/`C`/`L` value parsed to exactly 0 — a solver trap (R=0 is a hard short / singular matrix; C=0/L=0 silently deletes the part). Almost always a placeholder value; set the real one or `Sim.Enable=0`. |
| `SIM_STIMULUS_SHORTED` | WARNING | Both terminals of a stimulus resolved to the same node (usually both to ground) — the source drives nothing. Check `node`/`node2` and the `--gnd` mapping. |
| `SIM_RSHUNT_ADDED` | NOTE | Auto-`rshunt` appended `.option rshunt=1e12` because a floating node was found. Below WARNING, so it never changes the exit code. |
| `SIM_SWEEP_IGNORED` | WARNING | A `--sweep` component-value override has no effect because that part's SPICE card resolves from `Sim.Params` / `spec.models` / a device model, not its component value — every corner would be identical. |

---

## `akcli sim fit-diode` — datasheet → SPICE model

The library `fit_diode()` (above) is exposed as a subcommand: run
`akcli sim fit-diode` with one or more datasheet forward-voltage `--point V@I`
values and it prints the fitted `.model` card plus the matching `Sim.Params`
string. Every value takes engineering notation, and the `V@I` point syntax is a
forward voltage, `@`, a forward current (`0.37@20m` = 0.37 V at 20 mA).

```bash
# One table row + the Schottky prior → IS solved directly (the honest default)
akcli sim fit-diode --point 0.37@20m --n-prior 1.05 --name DBAT
```

```
# akcli sim fit-diode

.model DBAT D(IS=2.4034e-08 N=1.0500)

Sim.Params: IS=2.4034e-08 N=1.0500
```

Add `--rs-point V@I` (a high-current point) to solve series resistance `RS`, and
`--cjo F` (e.g. `50p`) for junction capacitance. `--json` returns
`{schema_version, name, model_card, sim_params, params, note}` — `note` carries the fit warning
when a multi-point `N` is clamped or disagrees with the prior.

### Writing the fit back onto a schematic — the datasheet → model loop

`--apply SCH --designator REF` closes the loop from a sourced part to a
ready-to-simulate schematic: it plans a native `set_component_parameters` write
that stamps `Sim.Device` + `Sim.Params` (and `Sim.Name` when a model name is
carried) onto that component, so the very next `akcli sim` run resolves it via
the [`Sim.*` first-hit-wins ladder](#model-resolution). It is a **dry run by
default** — it prints the exact op-list it *would* apply and changes nothing:

```bash
akcli sim fit-diode --point 0.37@20m --name DBAT \
  --apply board.kicad_sch --designator D4          # dry-run: prints the op-list
```

```
# akcli sim fit-diode (dry-run)

.model DBAT D(IS=2.4034e-08 N=1.0500)

would set on D4 in board.kicad_sch:
{
  "protocol_version": 1,
  "target_format": "kicad",
  "ops": [
    {
      "op": "set_component_parameters",
      "designator": "D4",
      "parameters": {
        "Sim.Device": "D",
        "Sim.Params": "IS=2.4034e-08 N=1.0500"
      }
    }
  ]
}

re-run with --write to apply (writes .akcli/backups/board.kicad_sch.bak; `akcli undo` reverts)
```

Add `--write` to commit through the KiCad writer (rotated `.bak`, connectivity
re-verify; a failed apply exits `6`).

> **The applied fit round-trips into a working deck.** The op-list above writes
> only `Sim.Device=D` + `Sim.Params` (no `Sim.Name`). When the next `akcli sim`
> resolves that component, `models.resolve` sees a native device carrying inline
> parameters but no model to point at, so it **synthesizes** a deterministic
> model name (`AKCLI_<designator>`) and a matching `.model` card from the
> `Sim.Params` (`D → .model AKCLI_D4 D(IS=… N=…)`; `NPN`/`PNP` likewise). The
> element line and its model card are emitted together, so a `fit-diode
> --apply --write` result simulates as-is — the diode conducts — instead of
> emitting a modelless element that ngspice rejects with `circuit not parsed`.

This is the last leg of the full
datasheet-to-model workflow: **source the part → grab its datasheet PDF → read
off the forward-voltage table row → `fit-diode --apply --write`**. The first two
legs are the parts commands — see [`docs/jlc.md` §`jlc datasheet`](jlc.md) and
the **akcli-parts-sourcing** skill for pulling the datasheet, and the **akcli-design-calc**
skill for the surrounding component-value math.

## `--sweep` — corner matrices

`--sweep NAME=v1,v2,…` re-runs the whole simulate-and-assert pass across a
**Cartesian matrix** of corners and prints a per-corner verdict table. Each
`--sweep` is one axis; the flag is repeatable and the product is capped at **64**
corners. Two axis kinds are recognized:

- **Component-value override** — `R21=2.2k,3.3k` swaps that designator's value on
  a deep copy of the schematic before the deck is rebuilt (nothing on disk
  changes).
- **Temperature** — `temp=0,25,60` injects a `.option temp=<v>` line into the
  deck for each value.

```bash
akcli sim divider.kicad_sch --sim divider.sim.json --sweep R1=10k,20k
```

```
# akcli sim (sweep: 2 corner(s))
  engine: /Applications/KiCad/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib

corner  R1   vmid  verdict
------  ---  ----  -------
1       10k  1.65  PASS
2       20k  1.1   FAIL

corners: 1/2 passed
```

The run exits `1` if **any** corner fails an assertion (or `0` with
`--exit-zero`). `--json` returns `{schema_version, engine, corners: [{params, measured, ok,
parts_skipped, findings}], warnings, ok}`. `--sweep` is engine-only: it is a usage error
together with `--deck-only`, without `--sim`, or with `--wave`.

**Deck-build diagnostics are surfaced in sweep mode too.** Single-run mode prints
the deck-build warnings (`SIM_FLOATING_NODE`, `SIM_RSHUNT_ADDED`,
`SIM_UNMODELED`, `SIM_UNDRIVEN_RAIL`, …); sweep mode used to drop them, so an
auto-inserted `rshunt` could mask a real wiring error in exactly the sign-off
mode. Each corner's warnings are now gathered, deduped, and rendered under the
corner table (and in the `warnings` array of `--json`). A **component** sweep
whose value the deck will silently ignore — because the part resolves through
`Sim.Params`, a `spec.models` entry, or a D/Q/X device model rather than its
component value — raises a `SIM_SWEEP_IGNORED` warning instead of producing a
matrix of byte-identical corners with no notice.

## Worked example — a resistor divider (self-contained)

The `board_v8` fixture is a 10 kΩ / 10 kΩ divider between `+3V3` and ground,
with a 100 nF cap on the midpoint. Drive `+3V3` with 3.3 V and assert the
midpoint sits at half-rail:

```json
{
  "protocol_version": 1,
  "stimuli": [
    {"kind": "vsource", "name": "supply", "node": "+3V3", "node2": "0", "value": "dc 3.3"}
  ],
  "analyses": {"tran": "10u 1m"},
  "assert": [
    {"name": "mid", "meas": "MAX v(mid) from=0 to=1m", "approx": "1.65", "tol": "0.02"}
  ]
}
```

```
$ akcli sim board_v8.kicad_sch --sim divider.sim.json
# akcli sim
  engine: /Applications/KiCad/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib
  deck sha1: 2da713640306

measured values:
  mid                              1.65   ~1.65 (tol 2%)         PASS

# metadata
  ...                                       (standard report metadata block)
# findings (0)
  (none)
$ echo $?
0
```

> **Node names must match after sanitizing.** The net is named `+3V3`; the deck
> sanitizes it to node `_3V3`. The stimulus `node` field is matched against the
> *net* name (`+3V3`), so use `"+3V3"` there — using `"3V3"` would create a new,
> floating node and the divider would read 0 V.

> **A SPICE deck is flat — sheet-local names must be unique.** Net labels are
> sheet-scoped in KiCad, so two instances of one child sheet each own a net named
> `MID`; ngspice has no hierarchy, so emitting both would silently wire them
> together and every measurement on that node would be meaningless. The deck
> builder refuses instead: `SIM_NODE_COLLISION` (exit 6) naming the two sheet
> scopes. Give them distinct names (`MID_A`/`MID_B`) or promote them to one global
> label if they really are one net. The same error catches two *different* names
> that sanitize to the same token (`VIN+` and `VIN-` both → `VIN_`), since ngspice
> node names are case- and punctuation-insensitive after sanitizing.

## Worked example — the spiro rev C demod channel

The reference the sim stack was built against is an IR photodiode demodulation
channel: a `TCRT5000`-driven photocurrent (ambient DC + 100 Hz flicker + a
gated ~2 kHz carrier while a ball passes), a high-pass front end, and a
`BAT54H` peak detector with a hold cap and bleed resistor. Simulated over a
100 ms transient with the datasheet-fitted `BAT54H` model (`IS ≈ 2.4e-8`,
`N = 1.05`, `RS ≈ 0.84 Ω`, `CJO = 50 pF` — see the [IS-1000× box](#fitting-a-diode-from-a-datasheet--fit_diode)),
the channel's assertions come out as:

| Measurement | Meas / when | Bound | Value |
|---|---|---|---|
| `t_detect` | `WHEN v(peak)=0.297 RISE=1` | `< 25m` | **2.3 ms** — detector latches well within budget |
| `vpeak` | `MAX v(peak) from=20m to=60m` | `> 0.35` | **0.49 V** — carrier envelope during the pass |
| `idle` | `MAX v(peak) from=0 to=19m` | `< 0.05` | **39 mV** — quiescent baseline before the ball |

That is the whole point of `akcli sim`: the design intent ("the detector must
latch inside 25 ms and idle below 50 mV") is written down as machine-checkable
bounds, and a schematic edit that breaks them turns into a non-zero exit instead
of a surprise on the bench.

---

## Worked example — a closed-loop comparator channel

A more involved reference: a **single-channel non-inverting Schmitt peak-detector
comparator**, built entirely from the pieces above (passives, a datasheet-fitted
Schottky, and the packaged `AKCLI_COMPARATOR` builtin subcircuit). It exercises
the `spec.models` `pin_order` path, positive-feedback hysteresis, and the
`.options method=gear` integration escape hatch.

**Topology.** A `bsource` drives node `HP` with an amplitude-triangle 50 kHz
carrier; a BAT54-style Schottky `D1` (anode `HP`, cathode `PEAK`) rectifies into a
peak-hold `R3` (100k) ‖ `C1` (10n) [RC = 1 ms] on `PEAK`; `PEAK` reaches the
comparator summing node `SUMM` through `R4` (100k); `R5` (1 MEG) feeds `OUT` back to
`SUMM` (positive-feedback hysteresis); `R1` (40k) / `R2` (10k) divider sets
`VREF ≈ 1.0 V`; `R6` (10k) is the open-collector pull-up `VCC → OUT`.

> **The comparator-sense gotcha.** `AKCLI_COMPARATOR` (ports `inp inn out vcc`)
> **sinks `OUT` low when `inp > inn`** — the *opposite* sense to a real LM339
> (which sinks when IN− > IN+). So the physical non-inverting (+) input maps to the
> model's `inn`, and the reference to `inp`. The `pin_order` encodes exactly this.
> The resistor designators must be **numeric-suffixed** (`R1`, not `Rt`) or the
> passive prefix heuristic rejects them, and the loop needs
> `.options method=gear` — trapezoidal integration hits "Timestep too small at
> vsup#branch" at the release edge (~7.6 ms) and aborts; gear converges over the
> full 10 ms.

The `sim.json` (`protocol_version 1`):

```json
{
  "protocol_version": 1,
  "stimuli": [
    {"kind": "vsource", "name": "Vsup", "node": "VCC", "node2": "0", "value": "5"},
    {"kind": "bsource", "name": "Bdrv", "node": "HP", "node2": "0", "quantity": "V",
     "expr": "2*(1-abs(time-5m)/5m)*sin(3.14159e5*time)"}
  ],
  "analyses": {"tran": "1u 10m"},
  "models": {
    "U1": {"device": "X", "model_name": "AKCLI_COMPARATOR", "pin_order": ["4", "5", "2", "3"]},
    "D1": {"device": "D", "model_name": "DBAT", "model_card": ".model DBAT D(IS=2.4e-8 N=1.05 RS=0.1)"}
  },
  "options": {"extra_cards": [".options method=gear"]}
}
```

`U1`'s symbol pins are `5`=IN+, `4`=IN−, `2`=OUT, `3`=V+; `pin_order`
`["4","5","2","3"]` is the model's `[inp, inn, out, vcc]` terminal order, i.e.
`VREF(inp) SUMM(inn) OUT VCC`. The diode order `HP → PEAK` is name-derived (pins
`A`/`K`), not from `pin_order`. The resulting deck contains **exactly one**
X-element:

```
D1 HP PEAK DBAT
R1 VCC VREF 40k
R2 VREF 0 10k
R3 PEAK 0 100k
R4 PEAK SUMM 100k
R5 OUT SUMM 1MEG
R6 VCC OUT 10k
C1 PEAK 0 10n
XU1 VREF SUMM OUT VCC AKCLI_COMPARATOR
Vsup VCC 0 5
Bdrv HP 0 V=2*(1-abs(time-5m)/5m)*sin(3.14159e5*time)
```

Live over `tran 1u 10m` (libngspice 45.2 in the KiCad bundle, reproducible): the
channel idles `OUT` LOW (`vout_lo = 0.005 V`, `peak_idle = 0.022 V < VREF`), snaps
`OUT` HIGH on detect (`vout_hi = 4.969 V`) with a 10–90% rise of **0.81 µs**
(≈1200× faster than the 1 ms RC — regenerative), and shows a hysteresis band of
**0.431 V** (attack 1.199 V − release 0.768 V). That band tracks the non-inverting
Schmitt prediction `ΔV_in = ΔV_out·(R4/R5) = 4.964·0.1 = 0.496 V` to within 13%.

---

## Troubleshooting — floating nodes and the auto-`rshunt` fix

Skipping ICs (`spec.models: {"U6": {"skip": true}}`) can leave nets whose only
remaining connection is a capacitor — a 555's CV pin with just its 10 n cap, a
regulator input holding only its bulk cap. At DC such a node is **floating**, the
operating point becomes a *singular matrix*, every gmin/source-stepping rescue
fails, and ngspice starts the transient from an unconverged state: the first
~100 µs show a phantom start-up ramp that can charge peak detectors and flip
time-based assertions (a `WHEN ... RISE=1` firing at microseconds is the tell-tale).

**The deck builder now detects and fixes this automatically.** Any net with no DC
path to ground is reported `SIM_FLOATING_NODE` (WARNING, naming the components that
stranded it), and — under the default `rshunt` policy — the builder appends
`.option rshunt=1e12` to make the operating point solvable, noting it with a
`SIM_RSHUNT_ADDED` NOTE (below WARNING, so it never changes the exit code).
`rshunt` hangs a 1 TΩ resistor from every node to ground: invisible to the
electrical results, but the matrix is no longer singular. So in most cases you no
longer need to do anything — the phantom ramp simply goes away.

`options.rshunt` controls the policy:

| `rshunt` value | Behavior |
|---|---|
| absent / `"auto"` (default) | Append `.option rshunt=1e12` **only when** ≥1 floating node is found (emits `SIM_RSHUNT_ADDED`). |
| `true` | Same as `"auto"` — the boolean is a policy switch, not a value. |
| `false` | **Never** emit it — you keep the raw singular matrix (and the `SIM_FLOATING_NODE` warning). |
| a number or string (`1e12`, `"1G"`) | **Always** emit exactly that value, floating node or not. |

The manual override still works if you want to pin a specific value or add other
cards — `options.extra_cards` is a raw passthrough:

```json
"options": {"extra_cards": [".option rshunt=1e12"]}
```

(Live validation: the spiro rev C channel reproduced its reference simulation to
six significant figures once the two cap-only nets left behind by skipping the 555
and the regulator were shunted — which auto-`rshunt` now does on its own.)

---

## See also

- [`docs/cli-reference.md`](cli-reference.md) — the `akcli sim` flag table.
- [`schemas/sim.schema.json`](../schemas/sim.schema.json) — the `sim.json` schema.
- [`docs/jlc.md`](jlc.md) — `jlc datasheet` for pulling the PDF whose forward-voltage
  table row feeds `sim fit-diode`.
- [`skills/akcli-circuit-debug/SKILL.md`](../skills/akcli-circuit-debug/SKILL.md) — using
  `sim` as a hypothesis tester when debugging.
