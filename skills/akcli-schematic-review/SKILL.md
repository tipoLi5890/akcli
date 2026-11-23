---
name: akcli-schematic-review
description: >-
  Run a structured, severity-ranked design review (審查) of an Altium .SchDoc or KiCad
  .kicad_sch schematic using the `akcli` CLI. Use this skill whenever the task involves:
  reviewing a schematic before release, prototype, or tape-out; auditing ERC, power-rail,
  or BOM check results; spot-checking a netlist against datasheet pin functions;
  cross-checking an MCU pinmap against an expected pin table (CSV/JSON/DTS-derived);
  reviewing what changed between two schematic revisions and whether anything regressed;
  running the `akcli review` engine (signal/validation/PCB/EMC/gerber detectors) and
  interpreting its confidence-graded findings, trust summary, and EMC risk score;
  or writing a hardware review report with Blocker/Major/Minor/Info/Question findings.
  Triggers on keywords: design review, schematic review, circuit review, 審查, hardware
  review, ERC audit, power tree, decoupling, pull-up, strap pin, boot pin, floating input,
  connector pinout, netlist check, pinmap cross-check, revision diff, regression, review
  report, sign-off.
---

# akcli-schematic-review — a severity-ranked schematic review protocol on top of `akcli`

This skill is the **review protocol**: what to run, in what order, how to interpret the
caveats, which electrical checks the tool cannot do for you, and how to write the report.
For basic `akcli` mechanics (input formats, read/analyze/draw usage, config discovery,
exit codes, error format) **see the akcli-circuit-design skill** — do not re-derive them here.

A review is **strictly read-only**. Never modify the schematic under review, and never
"fix" it to match an expected table — report the divergence instead.

## Review doctrine

- **Evidence or it didn't happen.** Every finding — and every "checked, OK" — must cite the
  exact `akcli` command you ran and paste the relevant output line(s). Never assert a defect
  or a pass from memory or from reading the file listing alone.
- **"0 findings" is not "clean".** Read the metadata header `check` prints (passive-pin
  ratio, No-ERC suppressed count, unnamed-net count, frac coords) before concluding anything.
- **The schematic is authoritative; expected tables and datasheet notes are advisory.**
- **Confidence is part of the finding.** `review` findings carry an explicit
  confidence: `deterministic` (recomputable geometry/math — trust it),
  `datasheet_backed` (carries the PDF sha256+page — trust it, cite the page),
  `heuristic` (name/prefix inference — YOU adjudicate before reporting above
  Minor), `llm_reviewed` (an accepted observation — never more than that).
  A `status: insufficient_evidence` finding is an open TODO (usually "add a
  facts file" — see akcli-datasheet-facts), never a pass.
- Run `check`/`diff`/`pinmap` in report mode so exit `1` means "findings exist",
  not "tool failed": every findings-emitting command (`check`/`diff`/`pinmap`/
  `library audit`/`fab check`) takes the same
  `--fail-on {info,note,warning,error,never}` (`--exit-zero` is the deprecated
  alias for `--fail-on never`); the boolean proof `verify` takes `--exit-zero`.
  `review analyze` takes the same ladder plus `critical`
  (`--fail-on {info,note,warning,error,critical,never}`) but with the opposite
  default: advisory (exit 0) unless `--fail-on` is given. Tune the CI gate with
  `--fail-on`, never by hiding findings.
- **Waiver discipline — never waive without a reason.** The checker-agnostic
  `[[waiver]]` config table drops (`severity = "off"`) or demotes
  (`note`/`info`) a finding by `code`/`refs`, but a review may only rely on a
  waiver that carries an explicit `reason` string justifying it — an
  unexplained waiver is itself a Minor finding. Read the header's
  `config-waived: N (M demoted)` line every run (`check`, `library audit` and
  `verify <sch> <pcb>` all print it): a waiver-cleaned pass is NOT an
  intrinsically clean pass, and each waived code must be re-justified against the
  current schematic (waivers are `[[waiver]]`, distinct from the ERC-only
  `[[erc_waiver]]`).

## Review pipeline

### Step 1 — Read and summarize

```bash
akcli read board.SchDoc --md
akcli read board.SchDoc --json | jq '{components: (.components|length), nets: (.nets|length)}'
```

Open the report with a two-paragraph summary: what the board is, the main IC(s), the power
entry and rails, the connectors, anything undesignated (`$U<n>` components) or unusual.
Identify the 3–5 highest-risk parts (MCU, regulators, USB/level shifters, connectors) — they
get the deep treatment in Steps 3–4.

### Step 2 — Run the review engine first (breadth), then the structural checks

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb --gerbers fab/ --out review.findings.json
akcli review analyze board.SchDoc --profile standard --out review.findings.json
akcli review report review.findings.json --format markdown
```

The engine runs the detector families (signal: dividers/RC/crystal/ESD/op-amp/
power-entry protection; validation: I²C pull-ups/voltage domains/enables; pcb: routing/decap/thermal/
trace ampacity; emc: 8 pre-compliance rules; gerber: fab-package checks;
domain: USB-C CC) — `--pcb`/`--gerbers` are optional and their families are
listed in `detectors_skipped` when absent, so the metadata always says what
was and wasn't reviewed. Interpretation rules:

- Read `trust_summary` first: it counts findings per confidence tier. A
  report that is all-heuristic needs YOUR adjudication before any of it
  lands above Minor.
- `akcli review explain REVIEW_XTAL_LOAD` prints any rule's spec, formula and
  literature reference — cite it in the report instead of re-deriving.
- The `emc` metadata block (deep profile + `--pcb`) is a severity-weighted
  advisory `risk_score` with `probe_points`; quote its standing note — it is
  pre-compliance risk analysis, never a compliance verdict. A quiet board
  scores 0 *with the block present*: "reviewed and quiet" ≠ "never reviewed".
- Findings with `fix_params` feed `akcli review propose` — mention available
  auto-fixes in the report but NEVER apply them during a review (read-only).
- Quantitative findings (RC corners, divider ratios) can be **simulation-
  verified** with `akcli review testbench <sch>`: akcli cuts out the
  subcircuit, synthesizes the bench, and ngspice delivers the verdict —
  cite "simulated: PASS at 15.88 kHz" instead of restating the calc. It is
  read-only (the bench runs on an extracted copy) and safe during a review;
  no engine → `--deck-only` still documents the bench.
- **Power-entry protection** (`signal.power_protect`): nets named as board
  inputs (`VBAT`/`VIN`/`VBUS`/`DCIN`… as the whole name, or rail-classified)
  are chain-walked (crossing only fuses/diodes/inductors) and judged for a
  series fuse and reverse-polarity protection. Codes: `REVIEW_FUSE_MISSING`,
  `REVIEW_REVPOL_UNPROTECTED`, `REVIEW_REVPOL_SHUNT_NO_FUSE` (a shunt crowbar
  without its series fuse); `REVIEW_FUSE_UNDERSIZED` fires only with a
  facts-store `i_load` (datasheet_backed — sizing goes through
  `calc fuse-derating`), and an unparseable rating reports
  `REVIEW_FUSE_UNRATED` as `insufficient_evidence` — an open TODO, never a pass.
  A deliberately unprotected branch is a waiver-with-reason, not a deletion.
- Heuristic findings you adjudicate as false positives are waived in config
  with a `reason`, not deleted from the report.
- Upgrade path: heuristic divider/crystal/domain findings that say "verify
  against the datasheet" become `datasheet_backed` once a facts file exists —
  extract with the akcli-datasheet-facts skill and re-run; that is stronger
  review evidence than hand-checking.

For LLM-side depth the detectors cannot reach (design intent, cross-domain
semantics), generate candidates and gate them through `akcli review validate`
— see the akcli-deep-review skill; quarantined candidates are NOT findings.

### Step 2b — Structural checks, then interpret the caveats

```bash
akcli check board.SchDoc --erc --power --bom --fail-on never -C akcli.toml
akcli check board.SchDoc --fail-on never --json
akcli check board.SchDoc --contract review.contract.toml --fail-on never   # if the project has a contract file
akcli check board.kicad_sch --integrity --fail-on never   # document integrity, read-only (.kicad_sch)
```

Default families for a bare `check`: ERC-lite + power + BOM + net hygiene +
layout + **integrity** (+ pair/bus continuity unless `[check].pairs = false`).
`--integrity` is the write gate's own document checks run read-only over any
`.kicad_sch`: dangling endpoints, duplicate uuids **and duplicate designators**,
unresolved `lib_id`s, instance paths, stacked power ports, a `(sheet …)` whose
child file is missing, no-connect conflicts. Findings here are
file-structural, not electrical — a `DUPLICATE_UUID` or `UNRESOLVED_LIB_ID` on a
file that was never written by akcli is a real defect (hand edit, bad merge, a
library that moved) and belongs in the report at Major or above.

`-C` supplies `[[rail]]` voltages, `mcu_designator`, and `[[erc_waiver]]` entries; omit it
and discovery walks up from the schematic's directory. Interpret the metadata header before trusting findings:

| Caveat | What it means for your review |
|---|---|
| high passive-pin ratio | When <20% of pins carry a real electrical type, type-based findings (driver conflict, floating input, undriven power pin) are **demoted to NOTE** and are unreliable — you must do the manual input/driver review in the heuristics section below. The demotion is **per net, not per board**: a net whose *every* member pin carries a real type keeps full severity even on a low-confidence board (the message says which case applied), so a WARNING here is trustworthy despite the header ratio. |
| No-ERC suppressed > 0 | Findings were hidden by No-ERC markers. List which nets carry them and confirm each suppression is justified; an unjustified one is itself a Minor finding. |
| unnamed-net count high | The power check **ignores unnamed nets entirely** — a rail on an unnamed net is unchecked. Name-check the power tree manually (Step "Power tree" below). |
| a net's name looks "wrong" | Canonical names follow eeschema's driver priority (global > power > local > hierarchical); the label you expected is usually in `aliases`, not lost — see the `akcli-net-naming` skill before flagging it. |
| frac coords present | Altium sub-unit coordinates are in play; purely informational. |

Finding codes you will see: `ERC_FLOATING_INPUT`, `ERC_DRIVER_CONFLICT`, `ERC_DANGLING_NET`,
`ERC_NO_POWER`, `ERC_NO_GROUND`, `ERC_NET_ALIAS` (NOTE-only by design),
`NC_PIN_CONNECTED` (WARNING — a no-connect marker on a pin that is nevertheless a member of
a ≥2-pin net; the marker and the wire/label contradict each other, the classic stale marker
left from an earlier revision; waiver token `nc_pin_connected`);
`POWER_NO_DECOUPLING`, `POWER_VOLTAGE_MISMATCH`, `POWER_CURRENT_BUDGET`, `POWER_NO_RAILS`,
`POWER_RAIL_NO_SOURCE` (WARNING — a recognized rail with ≥2 consumers and nothing that
could feed it: no `power_out`/`output` pin, no `PWR_FLAG`, no connector/battery/test-point
member, no entry-style rail name, no series-fed upstream source, no `[[rail]]` declaration.
It fires only on a rail nobody wired, so treat a hit as Major until you find the source);
`BOM_DUPLICATE_DESIGNATOR`, `BOM_MISSING_VALUE`, `BOM_MISSING_FOOTPRINT`, `BOM_REFDES_GAP`;
integrity: `DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `DUPLICATE_UUID`,
`DUPLICATE_DESIGNATOR`, `UNRESOLVED_LIB_ID`, `INVALID_INSTANCES_PATH`,
`POWER_PORT_STACKED` (two net-naming power symbols on one coordinate — the
rails merge with nothing visible to see), `SHEET_FILE_MISSING`,
`NO_CONNECT_CONFLICT` (WARNING — the wire-based subset of `NC_PIN_CONNECTED`, caught
pre-netbuild; unlike its ERROR siblings above it never blocks a write);
`CONTRACT_PASS` (INFO — a contract rule was checked and held), `CONTRACT_WAIVED` (NOTE —
a rule was demoted/dropped by a waiver), `CONTRACT_EXCEPTION_EXPIRED` (an approved
exception's expiry has passed — the rule is enforced again; report as a finding, not a pass).

Known blind spots — never let a clean `check` close these out:
- IC detection is designator-prefix-only (`U`/`IC`): regulators named `VR1` or modules named
  `A1` are never checked for power/ground.
- Power/ground rules are net-**name** based, not pin-type based.
- The decoupling heuristic only asks "does *some* C bridge this rail and ground" — it checks
  no cap count, value, or per-pin proximity.

**DNP parts satisfy nothing** (`check` and `review` alike): a `(dnp yes)` bypass
cap does not decouple, a DNP pull-up does not pull up, a DNP fuse/diode is not in
the series path, a DNP load cap does not load the crystal, a DNP resistor does not
tie an enable pin. The presence-satisfier searches gate on fitted parts, so a rule
still fires with the part drawn on the sheet — do NOT dismiss such a finding as a
false positive because "the cap is right there". BOM/enumeration rules deliberately
still see DNP parts. When reviewing, list the DNP population and check that nothing
electrically load-bearing sits in it.

### Step 3 — Netlist spot-checks against the datasheet

For each high-risk part from Step 1, pull the datasheet pin table and compare pin-by-pin:

```bash
akcli component board.SchDoc U3
akcli net board.SchDoc VBUS --json
```

Confirm: every supply pin on the right rail, every ground pin on a ground net, EN/RESET pins
driven or pulled as the datasheet requires, feedback/sense pins wired to the right divider.
Caution: `net <file> NAME` and `component <file> REF` exit `8` (`QUERY_MISS`) when nothing
matches — check the exit code or stderr for `no net named ...` / `no component ...`, do not
assume exit 0 means a hit.

### Step 4 — Pinmap cross-check against the expected table

```bash
akcli pinmap board.SchDoc --mcu U3 --fail-on never
akcli pinmap board.SchDoc --mcu U3 --expected pins.csv --fail-on never
```

`--expected` accepts a 2-column CSV (`pin,signal`, optional header) or JSON (a `{pin: signal}`
dict, or a list of `{pin, signal}` rows). To derive that table from firmware or docs, use
`akcli expected board.dts -o pins.json` (Zephyr DTS/overlay or a markdown pinout table). Result codes: `PINMAP_MISMATCH` and
`PINMAP_EXPECTED_PIN_MISSING` are WARNINGs (report as Major unless clearly benign);
`PINMAP_UNEXPECTED` is a NOTE (schematic pins the table doesn't cover — usually Info).
The schematic always wins; a mismatch is a finding to report, never a schematic edit.

### Step 5 — Revision diff (when reviewing v2 against v1)

```bash
akcli diff v1.SchDoc v2.SchDoc --fail-on never
akcli diff v1.SchDoc v2.SchDoc --fail-on never --json
akcli review diff v1.findings.json v2.findings.json
```

`review diff` aligns the two runs' findings by their wording-immune
fingerprints: report the `added` list (regressions), celebrate `resolved`,
and call out `severity_changed` (a heuristic that became `datasheet_backed`
is evidence hardening, not churn).

Nets are matched by pin **membership**, not display name, so a pure rename shows as a NOTE
and a membership change as a WARNING with `+`/`-` pin lists. Answer three questions in the
report: (a) is every *intended* change present, (b) did anything **regress** — nets that lost
pins, removed decoupling caps, a connector or MCU pin that silently moved nets — and (c) are
there unexplained changes nobody asked for? If the JSON marks `low_confidence` (cross-revision
files without shared UniqueIDs, or weak Jaccard matches), say so explicitly and downgrade
diff-derived conclusions to Question where the match is doubtful.

### Step 6 — Schematic ↔ PCB equivalence (when a board file exists)

```bash
akcli verify board.SchDoc board.kicad_pcb --strict --exit-zero
```

This compares the schematic against the layout on three axes: refdes presence (every
designator on both sides), footprint/value assignment (`--strict` also fails on value/
footprint mismatches), and the pad-level net **partition** — net *names* are untrusted,
so nets are compared by which pads actually group together. Findings:
`SCHPCB_NET_SPLIT` (a schematic net's pads land in two or more PCB nets),
`SCHPCB_NET_MERGE` (two schematic nets share one PCB net), `SCHPCB_PAD_MISSING`
(a schematic pin has no corresponding PCB pad), `SCHPCB_FOOTPRINT_MISMATCH`. Power
symbols (`#PWR`) and No-ERC/PWR_FLAG markers (`#FLG`) are excluded from the comparison —
do not report those refdes as missing on the PCB side, that is expected.

For a KiCad schematic + KiCad board it also compares assembly flags:
`SCHPCB_DNP_MISMATCH` / `SCHPCB_BOM_FLAG_MISMATCH` (the schematic's DNP / in-BOM flag
disagrees with the footprint's `dnp` / `exclude_from_bom` attribute — a BOM-vs-placement
disagreement in the making; report it). A footprint on the board with no symbol is
`SCHPCB_EXTRA_ON_PCB`: a WARNING, unless KiCad marks it *Not in schematic* (`board_only` —
mounting holes, fiducials), which is expected and arrives as a NOTE — do not escalate it.

Add `--library` to also compare each board footprint with its definition in the
**project** `fp-lib-table` (`FPLIB_PAD_SET_DRIFT`, `FPLIB_PAD_DRILL_DRIFT`,
`FPLIB_SOURCE_MISSING` — warnings; `FPLIB_MODEL_MISSING` — note):

```bash
akcli verify board.kicad_sch board.kicad_pcb --library --exit-zero
```

A pad drift may be a deliberate local edit — ask, do not assume a defect. **Always quote the
`library:` coverage line**: footprints from KiCad's own (global-table) libraries are NOT
compared, so "no drift" is a statement about the compared footprints only.

## Electrical heuristics the tool does not run — apply them yourself

Work through this list for every review; gather evidence with `akcli component` /
`akcli net` and cite it. For value sanity, use `akcli calc` (akcli-design-calc skill):
a resistor `eseries` cannot match within ~1 % on E96 is a likely typo; recompute
suspicious dividers/pull-ups/load caps and cite the printed reference.

- **Decoupling per power pin** — the tool checks one cap per rail per IC at most. For each
  IC, list its supply pins (`akcli component board.SchDoc U3`) and confirm the rail net's
  membership (`akcli net board.SchDoc 3V3`) includes enough `C` parts for the pin count.
- **Config / strap / boot pins** — BOOT, MODE, ADDR, CFG pins must have a defined level
  (pull-up/down `R`, or driven). A floating strap pin is at least Major.
- **Unconnected inputs** — on passive-heavy boards `ERC_FLOATING_INPUT` is demoted; manually
  scan each IC's input pins for nets with no driver. The rule also treats a **passive tie to
  a rail** (pull-up/pull-down, series feed) and a **connector** on the net as drivers, so it
  says nothing about *whether the level is the right one* — check strap/boot pin polarity by
  hand.
- **Connector pinout sanity** — dump each connector (`akcli component board.SchDoc J1`) and
  compare against the mating standard (USB, SWD/JTAG, Qwiic...): power/ground on the right
  positions, no shield-to-signal shorts. For a board-wide sweep,
  `akcli doc board.SchDoc -o book.md` renders every IC/connector's pin→net table (plus the
  rail summary and BOM) in one deterministic Markdown document — the natural artifact to
  attach to the review.
- **Power-tree consistency** — start from `akcli review tree board.kicad_sch` (rails →
  regulating IC found via its feedback divider → consumers → decoupling count) and verify
  every rail has exactly one source, every consumer sits on the intended rail, and rail
  names imply voltages consistent with `[[rail]]` config. Paste the tree into the report's
  power section.
- **Interface cross-check** — I2C: SDA/SCL each one net end-to-end with pull-ups to the right
  rail. UART: A's TX lands on B's RX (`akcli net board.SchDoc UART_TX` then `component` both
  ends) — a TX-to-TX net is a Blocker. SPI: MOSI/MISO orientation per datasheet naming, one
  CS per slave.
- **Current-return paths** — every connector and off-board interface must carry ground:
  confirm the GND net membership includes those connector pins.

## The review report

Rank every finding on this severity ladder, most severe first:

| Severity | Meaning | Examples |
|---|---|---|
| **Blocker** | Board will not function or may be damaged | TX-to-TX UART, reversed supply, `ERC_DRIVER_CONFLICT` on push-pull outputs, connector with no ground return |
| **Major** | Likely malfunction or unreliability | floating strap pin, missing I2C pull-ups, `POWER_VOLTAGE_MISMATCH`, supply pin with no decoupling |
| **Minor** | Hygiene / robustness | `BOM_MISSING_FOOTPRINT`, refdes gaps, a dangling test net |
| **Info** | Observations and tool caveats | metadata header notes, `ERC_NET_ALIAS`, `low_confidence` diff |
| **Question** | Cannot be resolved from schematic + datasheet | unclear intent, missing datasheet, ambiguous expected table |

Rules for the report body:
- Each finding: `[Severity] <one-line defect> — evidence:` followed by the exact command and
  the pasted `akcli` output line(s) that prove it. **Never assert without a command result**;
  anything you could not verify with tool output plus a datasheet is filed as Question.
- Include a "Caveats" section quoting the `check` metadata header verbatim, plus the
  `low_confidence` state of any diff.
- Close with a verdict: approve / approve-with-Minors / request-changes, justified only by
  the Blocker/Major list.

## Exit codes and flags

See the akcli-circuit-design skill for the full exit-code table and global flags. In this skill's
review mode, always pass `--fail-on never` (`--exit-zero` is its deprecated alias) to
`check`/`diff`/`pinmap` and treat findings via the
report, not the exit code. Inputs must be schematics — `.SchLib`, `.PcbDoc`, and `.kicad_pcb`
exit `5` for `check`/`diff`/`pinmap` (use `akcli read` on those instead).
