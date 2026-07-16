# Review rules — specification & references

The single source of truth for every `REVIEW_*` rule: what it checks, the
math, the confidence it honestly claims, and the engineering source its
judgement rests on. Detectors run on akcli's normalized model, which is why
every rule reviews KiCad `.kicad_sch` **and** Altium `.SchDoc` inputs alike.

Review is **advisory by default**: `review analyze` exits 0 regardless of
findings; `--fail-on SEVERITY` opts a CI job into gating. Config
`[[waiver]]` entries apply exactly as they do to `check`.

```
akcli review analyze board.kicad_sch --out review.findings.json
akcli review report review.findings.json --format markdown
akcli review explain REVIEW_XTAL_LOAD
```

## Rule table

**Signal family (M2):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_FB_DIVIDER` | info | heuristic | Vout = Vref·(1 + Rt/Rb) |
| `REVIEW_FB_DIVIDER_VREF` | warning | heuristic | typical bandgap references 0.5–1.25 V (≤2.5 V shunt) |
| `REVIEW_DIVIDER_TAP_MISMATCH` | warning | heuristic | — |
| `REVIEW_DIVIDER_UNVALUED` | note | deterministic | — (insufficient-evidence discipline) |
| `REVIEW_RC_CUTOFF` | info | deterministic | Horowitz & Hill, *AoE* (via `akcli calc rc`) |
| `REVIEW_XTAL_NO_LOADCAPS` | warning | heuristic | ST AN2867 §3 |
| `REVIEW_XTAL_ASYMMETRIC` | warning | heuristic | ST AN2867 §3 |
| `REVIEW_XTAL_LOAD` | info | heuristic | ST AN2867 §3 |
| `REVIEW_CONN_UNPROTECTED` | warning | heuristic | IEC 61000-4-2 |
| `REVIEW_OPAMP_GAIN` | info | heuristic | ideal op-amp: G = 1 + Rf/Rg; G = −Rf/Rin |
| `REVIEW_OPAMP_NO_FEEDBACK` | warning | heuristic | — |
| `REVIEW_DETECTOR_ERROR` | warning | deterministic | — (engine containment; quarantined) |

**Validation family (M3):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_I2C_NO_PULLUP` | warning | heuristic | NXP UM10204 Rev.7 §7.1 |
| `REVIEW_I2C_PULLUP_STRONG` | warning | heuristic | UM10204 §7.1: Rp(min) = (VDD−0.4 V)/3 mA |
| `REVIEW_I2C_PULLUP_WEAK` | note | heuristic | UM10204 §7.1: Rp(max) = t_r/(0.8473·C_b) |
| `REVIEW_I2C_PULLUP_MISMATCH` | note | heuristic | — |
| `REVIEW_VDOMAIN_CROSS` | warning | heuristic | receiving pin's absolute-maximum rating |
| `REVIEW_EN_FLOATING` | warning | heuristic | — (EN default states differ per part) |

**Datasheet-backed upgrades (M4):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_FB_DIVIDER_VREF_MISMATCH` | warning | datasheet_backed | facts file `vref` (sha256+page pinned) |
| `REVIEW_XTAL_LOAD_MISMATCH` | warning | datasheet_backed | facts file `load_capacitance` + ST AN2867 §3 |
| `REVIEW_VDOMAIN_CROSS` (adjudicated) | warning/info | datasheet_backed | facts file `abs_max_io` |

**PCB family (M5, needs `--pcb`):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_PCB_UNROUTED` | warning | deterministic | — (union-find copper partition) |
| `REVIEW_DECAP_DISTANCE` | warning | heuristic | supply-loop inductance vs decoupling |
| `REVIEW_THERMAL_VIA` | warning | heuristic | package EP thermal-via guidance |
| `REVIEW_THERMAL_JUNCTION` | warning/info | datasheet_backed / heuristic | Tj = Ta + P·θ_JA (facts: theta_ja/power_dissipation/t_j_max) |
| `REVIEW_TRACE_WIDTH` | info | deterministic | IPC-2221 (via `akcli calc trackwidth`) |

**EMC family (M6, needs `--pcb`, `deep` profile):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_EMC_NO_GND_PLANE` | warning | heuristic | return-loop area vs emissions |
| `REVIEW_EMC_PLANE_COVERAGE` | note | heuristic | — (bbox approximation, stated) |
| `REVIEW_EMC_STACKUP_ADJACENT` | note | heuristic | signal layers reference a plane |
| `REVIEW_EMC_VIA_STITCH` | warning/note | heuristic | λ/20 stitching @ assumed 1 GHz |
| `REVIEW_EMC_EDGE_TRACK` | note | heuristic | plane-edge fringing |
| `REVIEW_EMC_CLOCK_EDGE` | warning | heuristic | clock harmonics vs emission limits |
| `REVIEW_EMC_DIFFPAIR_SKEW` | warning | heuristic | ~25 ps intra-pair budget @6.6 ps/mm |
| `REVIEW_EMC_TVS_FAR` | warning | heuristic | IEC 61000-4-2 let-through vs placement |

When the EMC family runs, the report metadata gains an **advisory `emc`
block**: `risk_score` (severity-weighted, capped 100), `probe_points`
(refs of warning+ findings — the near-field starting list) and the standing
note that this is *pre-compliance risk analysis, not a compliance verdict*.
A quiet board scores 0 with the block present — "reviewed and quiet" is
distinguishable from "never reviewed".

**Domain family (M8, `deep` profile — first family: USB-C):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_USB_CC_MISSING` | warning | heuristic | USB Type-C §4.5.1.2: Rd = 5.1 kΩ |
| `REVIEW_USB_CC_VALUE` | warning | heuristic | USB Type-C §4.5.1.2: Rd = 5.1 kΩ ±10 % |

A CC net reaching any IC is assumed handled by a USB-C controller
(integrated Rd) and skipped. RF / Ethernet / HDMI / memory / BMS / motor
families remain demand-ordered backlog.

**Gerber family (M9, needs `--gerbers DIR`):**

| code | severity | confidence | reference |
|---|---|---|---|
| `REVIEW_GERBER_INCOMPLETE` | warning/note | deterministic | Gerber X2 file functions: minimum fab set |
| `REVIEW_GERBER_LAYER_MISMATCH` | warning | deterministic | — (board stackup vs copper files) |
| `REVIEW_GERBER_ALIGNMENT` | warning | deterministic | — (bbox registration, stated) |
| `REVIEW_GERBER_STALE` | warning | deterministic | — (outline vs .kicad_pcb Edge.Cuts) |
| `REVIEW_GERBER_UNITS_MIXED` | note | deterministic | — |

**Facts-store audit codes (`review facts verify`):** `FACTS_SCHEMA_INVALID`
(error), `FACTS_PDF_MISSING` (warning), `FACTS_STALE` (error — the PDF's
sha256 no longer matches), `FACTS_QUOTE_MISMATCH` (warning),
`FACTS_QUOTE_UNVERIFIED` (note — pdftotext absent), `FACTS_EMPTY` (note).

**BOM (M3, lives in `check --bom`):** `BOM_MPN_COVERAGE` (warning,
deterministic) — MPN/distributor-field coverage below 50 % on a ≥10-part
sheet: the BOM cannot be ordered as-is.

`akcli review explain <CODE>` prints the full specification of any registered detector rule (the `REVIEW_DETECTOR_ERROR` engine-containment code is internal — no `explain` page).

## Spec pages

### signal.divider

**Topology.** Rail→R_top→tap→R_bottom→GND chains, found by net membership
(two-terminal parts judged by spanning exactly two nets — format-agnostic).
A tap whose NAME implies a voltage (`2V5_REF`) classifies as a power rail
under the shared rail heuristics, so the tap filter excludes only ground.
A chain whose tap carries an IC feedback/sense pin (`FB`/`ADJ`/`SENSE`/…)
is a *feedback* divider and is reviewed even when the top rail's voltage is
unknown; a *plain* divider is reviewed only when the top net is
power-recognised (otherwise two series resistors between arbitrary signals
would drown the report).

**Math.** `V_tap = V_rail·R_b/(R_t+R_b)`; feedback form
`Vref = Vout·R_b/(R_t+R_b)` with the plausible-reference band 0.2–3.0 V.
Divider-tap mismatch tolerance: 5 %.

**Honesty.** Rail voltages come from NET NAMES → `heuristic`. Unparseable
resistor values → `REVIEW_DIVIDER_UNVALUED` with
`status: insufficient_evidence`; the ratio is never guessed. With a facts
file recording the regulator's `vref`, the comparison is `datasheet_backed`
(`REVIEW_FB_DIVIDER_VREF_MISMATCH` on >5 % disagreement).

### signal.rc_filter

Series R into a node with a shunt C to ground. `fc` comes from
`akcli calc rc` — the calc envelope (inputs, results, literature reference)
rides in `evidence.calc` verbatim, so every number is re-computable and
cited. Known confusable: a pull-up + decoupling pair matches the same shape;
the rule text says so and stays `info`.

### signal.crystal

Two-signal-pin crystals (oscillator modules skip). Checks: no load caps at
all; one-sided load; >5 % asymmetric pair; else reports
`CL = C1·C2/(C1+C2) + C_stray` with `C_stray = 4 pF` **stated as an
assumption** (cf. ST AN2867 §3). With a facts file recording the crystal's
`load_capacitance`, the comparison is `datasheet_backed`
(`REVIEW_XTAL_LOAD_MISMATCH` on >10 % disagreement, remediation suggesting
C = 2·(CL − C_stray)).

### signal.protection

Connectors (designator prefix `J/P/CN/X/USB` or library keyword) whose
signal nets (non-power, non-ground, ≥2 members) reach no recognised TVS/ESD
part (library/value keyword table in `review/tables.py`). Keyword-based role
detection is honestly `heuristic` — an exotic part naming scheme evades it;
waive per net or rename the part. Power-only connectors are out of scope
for this rule.

### signal.opamp

Op-amp units recognised by `+`/`-` input pin names (per unit of a multi-unit
package); the output pin by electrical type or name. Topology decides the
finding: output wired to `-` → unity buffer; feedback R plus a ground leg →
non-inverting `G = 1 + Rf/Rg`; feedback R plus an input R with `+` at a
reference → inverting `G = −Rf/Rin`; no feedback path at all →
`REVIEW_OPAMP_NO_FEEDBACK` (legitimate for comparators, which share the pin
shape — waive per part). Per-part behavioral limits (GBW, slew, output
swing) arrive with the SPICE milestone.

### validation.i2c_pullup

Nets named `SDA*`/`SCL*` (token match, prefix-tolerant: `I2C1_SDA`). Missing
pull-up → warning (internal MCU pull-ups exist but rarely meet spec rise
times — confirm deliberately). With a pull-up and a rail voltage, the window
comes from `akcli calc i2c-pullup` (NXP UM10204 §7.1): `R < R_min` needs no
bus-capacitance guess and warns; `R > R_max` depends on the assumed
`C_b = 100 pF` and therefore stays a NOTE with the assumption stated.
SDA/SCL value mismatch is a NOTE.

### validation.vdomain

Per-IC domain = the implied voltages of the power rails its pins touch
(ICs = parts spanning ≥3 nets; connectors and TVS parts excluded). A signal
net joining ICs whose rails differ by more than 0.6 V is flagged — unless
some part on the net touches BOTH extremes (a level shifter), which makes
the net one domain. With facts files recording the receiving pins'
`abs_max_io`, the judgement is `datasheet_backed`: every pin rated ≥ the
driving rail downgrades the finding to a verified-tolerant INFO; a rated
violation keeps the warning with the exact page as evidence.

### pcb.routing / geometry engine

Per-net **union-find over copper elements** (pads, track segments, vias) in
mm: two elements join when they share a copper layer (through pads/vias span
all) AND geometrically touch (point-to-segment distance against the summed
touch radii, so T-junctions count). Zones merge everything inside their
bounding box on their layers — deliberately conservative: a zone can only
OVER-merge, so `REVIEW_PCB_UNROUTED` (more than one pad-bearing island) is
never a poured-plane false positive. Boards declare their units
(KiCad mm / Altium mil); geometry normalises.

### pcb.decap

Capacitors (C prefix) whose pad sits on a power-recognised net shared with
an IC (U/Q prefix): min pad-to-pad distance to the nearest IC pad, warning
past 4 mm. Role inference is heuristic; the measured distance rides in
evidence. Bulk caps legitimately sit far — waive per ref.

### pcb.thermal

Exposed pads (≥4 mm² on U/Q parts, net-bound) must carry ≥4 vias inside the
pad boundary (+0.3 mm margin; 90°-family rotations handled). Junction
estimate: `Tj = Ta + P·θ_JA` with `theta_ja`/`power_dissipation`/`t_j_max`
from the part's facts file (datasheet_backed) or θ_JA from a
typical-package table (heuristic, stated); **no recorded dissipation → no
estimate** — a temperature is never invented. Ambient 25 °C stated as an
assumption.

### pcb.trace_width

Every power-named net with copper: thinnest segment + its IPC-2221
continuous ampacity (1 oz, ΔT 10 °C external — both stated). The
`calc trackwidth` envelope rides in evidence as the round-trip oracle. INFO
only: the rail's real current is not on the board; the automated comparison
arrives with the power tree (M7).

### emc.* (batches)

**Batch 1 — geometric:** ground-pour presence (multilayer without a GND
zone) + bbox coverage note; ground-via stitching (no vias = warning; largest
nearest-neighbour gap over λ/20 at the assumed 1 GHz/ε_eff 4.3 ≈ 7.2 mm =
note); board-edge tracks (0.5 mm margin against the Edge.Cuts bbox —
rectangular approximation stated; **no outline → silent, not a pass**);
TVS-to-connector distance (10 mm clamp radius).

**Batch 2 — analytical:** differential-pair intra-pair skew — pairs found
by name convention (`_P/_N`, `+/-`, `_DP/_DM`), per-net summed track length,
>25 ps at 6.6 ps/mm warns with the short side named in `fix_params`; an
unrouted side belongs to `REVIEW_PCB_UNROUTED`, not this rule. Clock-named
nets (CLK/SCK/MCLK/XTAL/OSC… tokens) at the board edge warn.

**Batch 3 — stackup:** consecutive copper layers both typed `signal` in the
declared stack order. Deeper PDN work (anti-resonance, impedance) needs
zone polygons / SPICE and stays on the backlog — the honest boundary is
stated rather than approximated.

## Closed loop (M7): propose / testbench / diff / tree

```
akcli review analyze board.kicad_sch --out review.findings.json
akcli review propose review.findings.json --out proposals.json
akcli review testbench board.kicad_sch --findings review.findings.json
akcli review diff old.findings.json new.findings.json --fail-on-new
akcli review tree board.kicad_sch
```

**`review propose`** turns findings into declarative candidate changes —
never touching a design file. Value fixes are **recomputed** (never copied)
and E-series-snapped via `akcli calc eseries`; the op-list draft is a
protocol-1 document to run through `akcli plan` / `draw --apply`, inheriting
every safety rail. Contract drafts carry the fact's `sha256+page` into the
contract `evidence` line — the sedimentation chain closes. The structural
guarantee (in code AND in `schemas/proposals.schema.json`): **a proposal
with open `requires_confirmation` items carries no op-list draft**; PCB
fixes are `layout` proposals because akcli writes schematics only.

**`review testbench`** closes the review → sim loop: every finding with a
simulable claim becomes a **runnable subcircuit SPICE testbench** — the net
cone around the finding's components is cut out of the schematic, stimuli
and pass/fail bounds are synthesized from the finding's own calc evidence,
and ngspice delivers the verdict ("fc = 15.9 kHz" upgrades from *computed*
to *simulated*). Generators today: `REVIEW_RC_CUTOFF` (AC sweep, assert the
−3 dB crossing on 1/(2πRC), ±8 %) and the divider family
(`REVIEW_FB_DIVIDER`/`_VREF_MISMATCH`/`REVIEW_DIVIDER_TAP_MISMATCH` — drive
the top rail, assert the tap at Vtop·Rb/(Rt+Rb), ±2 %). The same honesty
rules as everywhere: predictions are **recomputed from the live schematic**
(a stale findings file cannot vacuously pass), a topology that no longer
re-derives is **skipped with a reason**, and the cone note states that the
verdict covers the local network, not the loaded in-circuit response.
`--findings` reuses a saved envelope (default: in-process `analyze`);
`--deck-only --out DIR` emits the decks with no engine; without an engine
the run mode exits 7 (`NGSPICE_MISSING`) like `akcli sim`. Lint-style exit:
1 when any bench fails.

**`review diff`** aligns two findings files by the wording-immune
fingerprint: added / resolved / severity-or-confidence changed / persisting.
`--fail-on-new` opts a CI job into failing on new findings.

**`review tree`** prints the power structure per rail: implied voltage, the
regulating IC (found via its feedback divider), consumers, decoupling count.

### gerber.package (M9)

`readers/gerber.py` reads a fab-output directory: file roles from **X2
`TF.FileFunction` attributes first**, filename conventions (KiCad tokens +
Protel extensions) second; RS-274X units/format/extents and Excellon
tools/holes/extents. Honesty rule: an Excellon file with bare-integer
(implied-decimal) coordinates gets a warning and **no bbox — never a
guessed one**. Checks: minimum fab set (copper×2, masks, outline, plated
drill; silk is a note), copper-file count vs the board's declared stackup,
bbox registration across copper/outline (+drill hits inside the outline),
**staleness** — the outline gerber's size vs the `.kicad_pcb` Edge.Cuts
extent (>1 mm = the export predates the last edit, the classic
"ordered the old rev" failure), and mixed units. Also a `release preflight
--gerbers DIR` gate.

```
akcli review analyze board.kicad_sch --pcb board.kicad_pcb --gerbers fab/
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --gerbers fab/
```

## Deep-review gate & blocking policy (M8)

**`review validate candidates.json board.kicad_sch`** — the deterministic
gate for LLM deep-review output. Four gates; a failure lands in
`metadata.quarantined` with its reasons (nothing silently dropped or
accepted): **G1 schema** (fields legal; a candidate may not claim any
confidence but `llm_reviewed`, may not pre-set status, must be anchored),
**G2 anchors** (every component/net/pin anchor resolves against the model),
**G3 datasheet evidence** (a cited sha256+page must match the facts store;
quotes checked via `pdftotext` when available), **G4 masquerade** (the code
may not collide with a registered deterministic rule). Accepted candidates
are `llm_reviewed` observations — they never block, never override a
deterministic finding, never auto-create a contract.

**`release preflight --review-policy policy.toml`** — the only path by which
review findings block a release:

```toml
[review]
profile = "standard"
allow = ["REVIEW_PCB_UNROUTED", "REVIEW_FB_DIVIDER_VREF_MISMATCH"]
```

Only explicitly allowlisted codes gate; everything else stays advisory. The
intended promotion path: replay a corpus (`tools/corpus_replay.py`,
dev-only) → measure the false-positive rate → then allowlist. The policy
file's sha256 and allow list are recorded in the release manifest.

## Datasheet facts store (M4)

```
datasheets/
  C123456_TPS61023.pdf          # fetched by `akcli jlc datasheet`
  extracted/<MPN>.json          # ONE audited facts file per MPN
```

Every fact is pinned to its source PDF by **sha256 + page** (optionally a
verbatim quote): `datasheet_backed` findings always trace to the exact
document. The store is audit-first — `manual` entry is a first-class
extraction method; the discipline lives in `review facts verify`
(schema, PDF presence, sha256 staleness, page bounds, quote presence via
the optional `pdftotext` driver — absent tool → NOTE, never a silent skip).
Schema: `schemas/datasheet-facts.schema.json` (wheel-mirrored).

```
akcli review facts add TPS61023 --pdf datasheets/C123_TPS61023.pdf --set vref=0.6V@5
akcli review facts verify
akcli review facts lookup TPS61023 vref
akcli review analyze board.kicad_sch --facts datasheets
```

`review analyze` auto-discovers `<sch dir>/datasheets` when it holds an
`extracted/` store. Standard fact keys consumed today: `vref` (regulator
feedback reference), `load_capacitance` (crystal CL), `abs_max_io` (pin
absolute maximum). Detectors with a fact upgrade to `datasheet_backed`;
without one they fall back to their heuristics — never to a guess.

### validation.enable_pin

EN/SHDN/CE-named pins (overbar markup and a leading active-low `n`
stripped) on ICs, whose net contains nothing else: floating enable →
warning. Whether the part starts then depends on an internal pull that
differs per part family — tie it, or waive citing the datasheet's
default-state row.
