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
  `review analyze` takes `--fail-on {warning,error,critical}` (advisory: exit 0
  without it). Tune the CI gate with `--fail-on`, never by hiding findings.
- **Waiver discipline — never waive without a reason.** The checker-agnostic
  `[[waiver]]` config table drops (`severity = "off"`) or demotes
  (`note`/`info`) a finding by `code`/`refs`, but a review may only rely on a
  waiver that carries an explicit `reason` string justifying it — an
  unexplained waiver is itself a Minor finding. Read the header's
  `config-waived: N (M demoted)` line every run: a waiver-cleaned pass is NOT an
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

The engine runs the detector families (signal: dividers/RC/crystal/ESD/op-amp;
validation: I²C pull-ups/voltage domains/enables; pcb: routing/decap/thermal/
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
akcli check board.SchDoc --erc --power --bom --exit-zero -C akcli.toml
akcli check board.SchDoc --exit-zero --json
akcli check board.SchDoc --contract review.contract.toml --exit-zero   # if the project has a contract file
```

`-C` supplies `[[rail]]` voltages, `mcu_designator`, and `[[erc_waiver]]` entries; omit it
and discovery walks up from the schematic's directory. Interpret the metadata header before trusting findings:

| Caveat | What it means for your review |
|---|---|
| high passive-pin ratio | When <20% of pins carry a real electrical type, driver-conflict and floating-input findings are **demoted to NOTE** and are unreliable — you must do the manual input/driver review in the heuristics section below. |
| No-ERC suppressed > 0 | Findings were hidden by No-ERC markers. List which nets carry them and confirm each suppression is justified; an unjustified one is itself a Minor finding. |
| unnamed-net count high | The power check **ignores unnamed nets entirely** — a rail on an unnamed net is unchecked. Name-check the power tree manually (Step "Power tree" below). |
| frac coords present | Altium sub-unit coordinates are in play; purely informational. |

Finding codes you will see: `ERC_FLOATING_INPUT`, `ERC_DRIVER_CONFLICT`, `ERC_DANGLING_NET`,
`ERC_NO_POWER`, `ERC_NO_GROUND`, `ERC_NET_ALIAS` (NOTE-only by design);
`POWER_NO_DECOUPLING`, `POWER_VOLTAGE_MISMATCH`, `POWER_CURRENT_BUDGET`, `POWER_NO_RAILS`;
`BOM_DUPLICATE_DESIGNATOR`, `BOM_MISSING_VALUE`, `BOM_MISSING_FOOTPRINT`, `BOM_REFDES_GAP`;
`CONTRACT_PASS` (INFO — a contract rule was checked and held), `CONTRACT_WAIVED` (NOTE —
a rule was demoted/dropped by a waiver), `CONTRACT_EXCEPTION_EXPIRED` (an approved
exception's expiry has passed — the rule is enforced again; report as a finding, not a pass).

Known blind spots — never let a clean `check` close these out:
- IC detection is designator-prefix-only (`U`/`IC`): regulators named `VR1` or modules named
  `A1` are never checked for power/ground.
- Power/ground rules are net-**name** based, not pin-type based.
- The decoupling heuristic only asks "does *some* C bridge this rail and ground" — it checks
  no cap count, value, or per-pin proximity.

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
akcli pinmap board.SchDoc --mcu U3 --exit-zero
akcli pinmap board.SchDoc --mcu U3 --expected pins.csv --exit-zero
```

`--expected` accepts a 2-column CSV (`pin,signal`, optional header) or JSON (a `{pin: signal}`
dict, or a list of `{pin, signal}` rows). To derive that table from firmware or docs, use
`akcli expected board.dts -o pins.json` (Zephyr DTS/overlay or a markdown pinout table). Result codes: `PINMAP_MISMATCH` and
`PINMAP_EXPECTED_PIN_MISSING` are WARNINGs (report as Major unless clearly benign);
`PINMAP_UNEXPECTED` is a NOTE (schematic pins the table doesn't cover — usually Info).
The schematic always wins; a mismatch is a finding to report, never a schematic edit.

### Step 5 — Revision diff (when reviewing v2 against v1)

```bash
akcli diff v1.SchDoc v2.SchDoc --exit-zero
akcli diff v1.SchDoc v2.SchDoc --exit-zero --json
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
  scan each IC's input pins for nets with no driver.
- **Connector pinout sanity** — dump each connector (`akcli component board.SchDoc J1`) and
  compare against the mating standard (USB, SWD/JTAG, Qwiic...): power/ground on the right
  positions, no shield-to-signal shorts.
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
review mode, always pass `--exit-zero` to `check`/`diff`/`pinmap` and treat findings via the
report, not the exit code. Inputs must be schematics — `.SchLib`, `.PcbDoc`, and `.kicad_pcb`
exit `5` for `check`/`diff`/`pinmap` (use `akcli read` on those instead).
