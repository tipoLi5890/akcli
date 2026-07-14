---
name: akcli-schematic-authoring
description: >-
  Create a NEW KiCad schematic from written requirements — or surgically extend an
  existing sheet — by authoring an `akcli` op-list and driving the plan -> dry-run ->
  apply -> verify loop. Use this skill whenever the task involves: designing a circuit
  from a spec or datasheet ("設計電路", "design a circuit", "create a schematic from
  scratch"); placing components, wires, net labels, power ports, or junctions in a
  .kicad_sch via op-list JSON; picking real parts with JLC/LCSC sourcing before drawing;
  or safely re-running / editing a previously drawn sheet. Triggers on keywords:
  new schematic, draw circuit, op-list, place_component, add_wire, net label,
  power port, decoupling, LDO block, connector fanout, akcli plan, akcli draw,
  JLC part selection, kicad_sch authoring.
---

# akcli-schematic-authoring — from requirements to a verified `.kicad_sch`

This skill covers *creating* circuits with the `akcli` op-list writer. For basic
read/analyze/draw mechanics, exit codes, error format, and the Altium (read-only)
path, **see the akcli-circuit-design skill** — do not re-derive them here. KiCad is the
only writable target; the writer is flat (single-sheet) v1.

**The authoring loop (never skip a stage):**
requirements → block plan → part selection → op-list → `akcli plan` (read the
**Net changes** block) → `akcli draw` (dry-run) → `akcli draw --apply`
(`--strict-nets` when editing an existing sheet) → re-read + `akcli check`
(+ `--intent` when a snapshot exists) → **verify behavior with `akcli sim`
before ordering** (see below).

## (1) Block plan before any op

Decompose the requirements into named blocks (power entry, regulation, MCU,
connectors), list every rail and named net, and assign designators up front
(U1, C1..Cn, J1...). Sketch a placement map: one block per region of the sheet,
signal flow left→right, rails at the top, GND at the bottom. Only then write ops.

## (2) Part selection — `akcli jlc` when sourcing matters

When the user cares about real orderable parts, resolve them first. `jlc` is the
only networked subcommand (exit 7 on network failure):

```bash
akcli jlc search "AMS1117-3.3" --limit 10        # find candidates (B=Basic, P=Preferred)
akcli jlc show C6186 --easyeda                    # confirm package, MPN, 3D availability
akcli jlc add C6186 --out akcli-parts/C6186       # convert symbol/footprint (in-process)
```

The produced `symbol/akcli.kicad_sym` becomes a `--symbols` source for
`plan`/`draw`; `--place --designator U1 --at 2000 1000` also emits a ready
`place.json`. Converted CAD data is third-party: always verify against the
datasheet and `akcli check` after placing. Record the LCSC C-number as an
`LCSC` parameter on the placed part so the BOM maps designator -> orderable part.

Component **values** come from `akcli calc`, not mental arithmetic: compute the
network (`vdivider-design`, `regulator-design`, `led`, `i2c-pullup`,
`crystal-caps`, ...) and place the returned `*_standard` E-series value; quote
the printed reference in the report (akcli-design-calc skill has the full catalog).

### Datasheet-driven design — `jlc datasheet` before committing values

Never design an IC's surrounding circuit from remembered specs. Pull the PDF
and read it:

```bash
akcli jlc datasheet C2984661 --fetch          # one part -> ~/.cache/akcli/datasheets/
akcli jlc datasheet board.kicad_sch --fetch   # every BOM line with an LCSC id, one run
```

- **Where the links come from:** the part's EasyEDA record (szlcsc-hosted
  PDF). A `no-link` row prints the LCSC product-page URL — fetch that page
  with a browser-grade fetcher (WebFetch) to locate the PDF, or search the
  **manufacturer's** site: original-vendor PDFs (vishay.com, ti.com, ...)
  download fine with plain `curl`, while `lcsc.com` itself bot-gates direct
  downloads. `--fetch` verifies the `%PDF` magic, so a challenge page can
  never masquerade as a datasheet on disk.
- **Reading order** (PDF readers cap ~20 pages per request — read in chunks;
  the tables live early): absolute maximum ratings → recommended operating
  conditions → electrical characteristics → typical application circuit.
  The typical-application schematic is the op-list's starting skeleton.
- **Feed the tables into `akcli calc`, then margin-check the op-list:**
  series-resistor current vs the emitter's I_F(max), dissipation at
  V_IN(max), comparator inputs vs the common-mode limit (e.g. LM339:
  V_CM ≤ V_CC − 1.5 V), logic thresholds vs the actual rail. Table values
  beat curve read-offs; quote the row you used (symbol, condition, min/typ/
  max) in the report so review can retrace it.
- Record the C-number as an `LCSC` parameter when placing — that is what
  makes the whole-BOM batch mode (and `jlc bom`) work later.

## (3) Seed the target file (new schematics only)

`plan`/`draw` require an existing target with a root `(uuid ...)`. For a brand-new
schematic, bootstrap one with `akcli new`:

```bash
akcli new board.kicad_sch                       # blank A4 sheet, root uuid + paper
akcli new board.kicad_sch --paper A3 --title "Power board"   # size + title block
akcli new board.kicad_sch --force               # overwrite an existing file
```

Keep this file for the whole session: the root uuid is the namespace for every
deterministic op UUID, so regenerating it breaks idempotent re-runs. (`--json`
returns `{created, target, paper, title, status}`.)

## (4) Op-list authoring patterns

Document shape and the op vocabulary (18 ops + 9 macros) are defined in `schemas/ops.schema.json`
(see also `docs/op-list-authoring.md` and `akcli ops list`/`ops template <op>`); per-executor support is in
`schemas/ops.capabilities.json`. Envelope: `{"protocol_version": 1,
"target_format": "kicad", "ops": [...]}`. The validator is strict: unknown
fields are errors with a did-you-mean hint (`_`-prefixed keys are safe
annotations), field types are enforced per op, and a duplicate
`(designator, unit)` placement in one document is rejected. Rules the
validator and executor enforce:

- **Coordinates**: mils, origin top-left, +Y down, 50-mil grid. Raw `[x,y]` points
  are grid-snapped; `"REF.PIN"` endpoints snap to the pin's exact world coordinate.
  Place two-pin passives on 100-mil multiples so wires between aligned pins stay
  orthogonal (use an L-shaped dogleg via an intermediate `[x,y]` otherwise).
- **Pin coordinates**: run `akcli pins <lib_id> --at X Y [--rotation R] [--symbols …]`
  to print every pin's exact WORLD `(x,y)` for a placement — target those points
  directly instead of guessing offsets. A label / power port placed *on* a pin's
  coordinate connects it with no wire (collision-proof in dense blocks).
- **`"at": "REF.PIN"` anchors**: `add_net_label` and the power-port ops accept a
  pin reference as `at` — the anchor snaps to that pin's world coordinate AND
  the label auto-orients away from the symbol body, with the `(justify ...)`
  KiCad needs (a bare 180° angle renders un-flipped, over the part). Explicit
  `orientation` wins; for stub-end labels set it to the stub direction
  (0/90/180/270 = right/up/left/down).
- **Layout lint**: after `--apply`, run `akcli check <file> --layout` — it flags
  overlapping symbol bodies, label text over a body/pin field, label-label
  overlaps, coincident text anchors, wires routed through symbol bodies, and
  power symbols anchored on another symbol's pin tip (`LAYOUT_POWER_ON_PIN` —
  see `place_pwr_flag` below). Fix the warnings; ERC cannot see them.
  `check --nets` adds attachment near-misses: a pin tip touching a wire
  mid-span with no junction is NOT connected (`NET_PIN_MIDSPAN_TOUCH`), an
  unattached label names nothing (`NET_LABEL_UNATTACHED`), and an L-wire
  corner on a pin tip is the classic accidental short
  (`NET_WIRE_CORNER_ON_PIN`).
- **Live dashboard**: `akcli view live <file>` serves a localhost timeline that
  re-renders the sheet (SVG + ERC badges + part/net counts) on every apply.
- **Wire vs label vs power port**: wire (`add_wire`, `"REF.PIN"` endpoints) for
  short in-block connections; `add_net_label` with `"scope": "local"` on a short
  wire stub for readable in-block nets; `"scope": "global"` for nets crossing
  blocks (writer is flat — avoid `hierarchical`, sheets are unsupported);
  `place_power_port` / `place_gnd` / `place_vcc` for rails (power ports merge by
  name everywhere and auto-allocate `#PWR` refs). Connectivity is name-based, so
  overlapping/collinear stubs or a wire T-junctioning another net silently MERGE
  nets — the hard write-gates are `DANGLING_ENDPOINT`/`DANGLING_BUS_ENTRY` only,
  so read the **Net changes** block on every plan/draw and verify membership
  with `akcli nets` after applying; never trust a clean apply alone. Label
  scoping truth (matches eeschema): a local label DOES merge with a same-name
  global label or power port on the SAME sheet even when physically
  disconnected — never use a rail name as a "private" local label.
- **Facing pins — `connect_and_label`**: two pins facing each other on one
  axis (555.OUT→R.1, R.2→C.1 chains) must NOT each get a label-on-pin —
  auto-orient extends both texts toward each other and `check --layout` flags
  the overlap. `{"op": "connect_and_label", "from": "U1.3", "to": "R2.1",
  "net": "PWM"}` emits the coaxial pin-to-pin wire plus ONE label at
  `mid(from,to)`, auto-oriented along the wire. (The `mid(REF.PIN,REF.PIN)`
  anchor also works directly in `add_net_label`/power-port ops: exactly
  axis-aligned pins only, snapped along the wire axis.)
- **`power:PWR_FLAG` — use the `place_pwr_flag` macro, mid-wire**: the flag
  silences KiCad's `power_pin_not_driven`; it marks the net driven but never
  names or merges a net, so it is safe on every rail. Do NOT anchor it on a
  pin ("#PWR01.1") — two bodies stack on one point and `check --layout` flags
  `LAYOUT_POWER_ON_PIN`/`LAYOUT_SYMBOL_OVERLAP`. `{"op": "place_pwr_flag",
  "at": [2000, 1750]}` (or `"at": "mid(REF.PIN,REF.PIN)"`) places it on the
  wire, rotated 90 so the body extends into empty space.
- **Spare multi-unit units — `terminate_unused_unit`**: unused op-amp/
  comparator units must be placed and terminated or KiCad ERC warns
  `missing_input_pin` (and `akcli check --erc` flags `ERC_UNPLACED_UNIT`).
  One op places the unit, ties +in to GND and −in to a rail, and no-connects
  the output: `{"op": "terminate_unused_unit", "designator": "U1",
  "lib_id": "Amplifier_Operational:LM358", "unit": 2, "at": [4000, 3000],
  "in_plus": "5", "in_minus": "6", "out": "7", "vcc": "+3V3"}`.
- **Junctions**: where 3+ wire ends meet, `auto_junctions` inserts `(junction)`
  nodes automatically before verify — do not hand-place `add_junction` unless the
  dry-run connectivity report shows a genuine miss. Pure X crossings are never
  auto-junctioned (by design): dogleg one wire instead.
- **Annotation**: `add_text` (`text`, `at`, optional free `angle`) for block titles
  and design notes; it is the only graphic op.
- **Multi-unit parts**: `place_component` takes an optional `"unit": N` — each
  unit is its own instance sharing the designator (`U1` gate A = unit 1, gate B
  = unit 2 ...). `"REF.PIN"` resolves against the instance whose unit owns the
  pin; wiring a pin on an unplaced unit fails loudly.
- **Delete/move/rename**: `delete_component` (all instances of a designator;
  attached wires are left for the connectivity gate to flag — delete them via
  `delete_object`, or set `"cascade": true` to also remove wires ending on the
  deleted pins plus labels/no-connects/junctions anchored there),
  `delete_object` (by `uuid`, or by `match: {kind, name?, at?}` with
  exactly-one semantics), `move_component` (one instance, properties travel
  with the body; wires do NOT stretch, so re-wire after a move), `rename_net`
  (rewrites label texts + power-port Values; zero matches is a replay-safe
  no-op). Deleting an absent target is a replay-safe no-op. CAREFUL: deleting
  a label can silently SPLIT a net whose fragments it held together — watch
  the `! SPLIT` lines in the Net changes block.
- **Hierarchical sheets — `add_sheet`**: place a child-sheet reference on the
  parent with `{"op":"add_sheet","name":"power","file":"child.kicad_sch",
  "at":[2000,1000],"size":[1000,800],"pins":[{"name":"VBUS","type":"input",
  "side":"left","offset_mil":200}]}` (`at` = TOP-LEFT corner, mils; `type` ∈
  input|output|bidirectional|tri_state|passive, `side` ∈ left|right|top|bottom).
  Full flow: `akcli new` both root and child files → `add_sheet` on the root →
  drop a same-name **hierarchical label** in the child (`add_net_label …
  "scope":"hierarchical"`) → `akcli nets root.kicad_sch` to confirm the parent
  sheet-pin and the child label merged into ONE cross-sheet net (parity-verified
  against eeschema). Wires attach to a sheet pin **by coordinate** (`at` +
  `offset_mil` along the side, grid-snapped) — there is NO `Sheet.Pin` endpoint.
  The child `.kicad_sch` must already exist; `add_sheet` never creates it.
  KiCad only (`altium: false`).
- **Bus rips — label the WIRE, not the bus**: draw the bus (`add_bus`), name it
  with a vector label (`add_net_label "K[3..0]"` on the bus — inclusive both
  ends, either order), add the diagonal `add_bus_entry`, then label the
  individual ripped **wire** (`K2`) — that wire-side label selects the bus
  member. An unlabeled rip FLOATS (stays unconnected); a plain label placed on
  the bus itself selects nothing. A `(bus_entry)` conducts end-to-end, and each
  end must land on a bus or a wire or `DANGLING_BUS_ENTRY` gates the write.

### Example A — LDO regulator block (new sheet)

```json
{ "protocol_version": 1, "target_format": "kicad", "target_file": "board.kicad_sch",
  "ops": [
    { "op": "place_component", "lib_id": "Regulator_Linear:AMS1117-3.3",
      "designator": "U1", "x_mil": 2000, "y_mil": 1000 },
    { "op": "place_component", "lib_id": "Device:C", "designator": "C1",
      "x_mil": 1500, "y_mil": 1150, "value": "10u" },
    { "op": "place_component", "lib_id": "Device:C", "designator": "C2",
      "x_mil": 2500, "y_mil": 1150, "value": "10u" },
    { "op": "add_wire", "vertices": ["C1.1", [1500, 1000], "U1.3"] },
    { "op": "add_wire", "vertices": ["U1.2", [2500, 1000], "C2.1"] },
    { "op": "add_net_label", "name": "VIN_5V", "at": [1500, 1000], "scope": "global" },
    { "op": "place_power_port", "lib_id": "power:+3V3", "net_name": "+3V3", "at": [2500, 1000] },
    { "op": "add_wire", "vertices": ["C1.2", [1500, 1400], [2500, 1400], "C2.2"] },
    { "op": "add_wire", "vertices": ["U1.1", [2000, 1400]] },
    { "op": "place_gnd", "at": [2000, 1400] },
    { "op": "add_text", "text": "3V3 LDO regulator", "at": [1500, 800] }
  ] }
```

### Example B — decoupling cap on an existing MCU (surgical edit)

```json
{ "protocol_version": 1, "target_format": "kicad", "ops": [
    { "op": "place_component", "lib_id": "Device:C", "designator": "C10",
      "x_mil": 3200, "y_mil": 900, "value": "100n",
      "footprint": "Capacitor_SMD:C_0402_1005Metric" },
    { "op": "add_wire", "vertices": ["C10.1", "U3.11"] },
    { "op": "add_wire", "vertices": ["C10.2", "U3.12"] }
  ] }
```

### Example C — connector fanout with global labels

```json
{ "protocol_version": 1, "target_format": "kicad", "ops": [
    { "op": "place_component", "lib_id": "Connector_Generic:Conn_01x04",
      "designator": "J1", "x_mil": 1000, "y_mil": 2000 },
    { "op": "add_wire", "vertices": ["J1.1", [1300, 2000]] },
    { "op": "add_net_label", "name": "UART_TX", "at": [1300, 2000], "scope": "global" },
    { "op": "add_wire", "vertices": ["J1.2", [1300, 2100]] },
    { "op": "add_net_label", "name": "UART_RX", "at": [1300, 2100], "scope": "global" },
    { "op": "add_wire", "vertices": ["J1.3", [1300, 2200]] },
    { "op": "place_gnd", "at": [1300, 2200] },
    { "op": "add_no_connect", "pin": "J1.4" }
  ] }
```

## (5) Validate → dry-run → apply

Every `place_component` / power-port symbol must resolve from a source: per-op
`symbol_source`, repeatable `--symbols` (a `.kicad_sym`, or a `.kicad_sch` whose
inline `lib_symbols` is harvested), or config `[paths]` entries ending in
`.kicad_sym`. A miss is `SYMBOL_NOT_FOUND`.

```bash
SYMS=/usr/share/kicad/symbols    # macOS: /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols
akcli plan board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym"      # never writes
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym"      # dry-run (the default)
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym" --apply
```

Exit 6 means an op errored or connectivity found an ERROR/CRITICAL
(`DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `UNRESOLVED_LIB_ID`, ...): on
`--apply` nothing was written. Another exit-6 cause is `TARGET_LOCKED`:
`draw`/`arrange`/`undo --apply` refuse to write while the KiCad GUI holds the
file open (a `~<name>.lck` lock file). After confirming it is safe, use
`--apply --allow-open`, then File > Revert in KiCad to pick up the change. Use
`--json` for machine output:
`{applied, status, ops[], connectivity[], net_diff}`. On `OP_UNSUPPORTED`,
`PROTOCOL_MISMATCH`, or geometry errors: stop and report, do not retry
blindly. Do not pass `--dry-run` — it is accepted but inert; omitting
`--apply` already is the dry run.

**Read the "Net changes" block on every plan/draw** — the before/after
netlists are diffed by pin membership:

```
Net changes:
  ! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.2>(2)   # DANGER: a fragment lost its name anchor
  ! MERGE MID + +3V3 -> MID                            # DANGER: a wire/label shorted two nets
  ~ VTH: +U1.7 (5->6 pins)                             # membership grew — is that the intent?
  = RENAME MID -> VOUT (3 pins)                        # harmless (same pins)
```

`(none)` proves the edit was connectivity-neutral. On surgical edits to an
existing sheet, apply with `--strict-nets`: any `!` line touching a named net
refuses the write (exit 6). An intended merge/rename? Re-run without
`--strict-nets` after confirming the lines are exactly the intended ones.

## (6) Idempotency and safe re-runs

- Created nodes get deterministic UUIDv5 keyed on the root sheet uuid plus
  `designator:op_index` (or tag:coords). **Re-running the same op-list file
  converges** — prior same-uuid nodes are replaced, `#PWR` refs are recovered,
  re-applies stay byte-identical.
- Because identity includes `op_index`, never reorder or insert ops mid-list in a
  file you already applied. To extend an applied sheet, author a NEW small op-list
  containing only the delta ops (Example B).
- `set_component_transform` / `set_component_parameters` address parts by
  designator; an unknown designator fails that op with `VERIFY_FAILED` and blocks
  the whole write.
- Apply is atomic with an optimistic lock: if the target changed on disk between
  read and write, apply aborts (`VERIFY_FAILED`) and the file is untouched. A
  `<name>.kicad_sch.bak` copy always lands next to the target on apply.

## (7) Verify everything after `--apply` — never trust a silent success

`applied: true` proves a write happened, not that the circuit is right. Always:

```bash
akcli read board.kicad_sch --md                        # parts present, values correct?
akcli nets board.kicad_sch                             # every net -> sorted members, one line each
akcli component board.kicad_sch U1                     # pin->net map of key parts
akcli check board.kicad_sch --exit-zero                # ERC-lite + power + BOM + nets + layout
akcli diff board.kicad_sch.bak board.kicad_sch --exit-zero   # the delta is exactly what you drew
```

Compare each net's membership against your block plan pin by pin. Read `check`'s
metadata caveats (passive-pin ratio, unnamed-net count) before declaring the sheet
clean — a findings-free run on a mostly-passive sheet proves little. If anything
diverges, fix the op-list and re-run the loop from `akcli plan`.

**Intent snapshot → assert (make the block plan machine-checkable).** Once the
sheet matches the plan, snapshot the netlist you MEAN; after every later edit,
assert it instead of eyeballing:

```bash
akcli nets board.kicad_sch --intent-snapshot intent.json   # capture named nets -> pins
# ... later surgical edits (draw --apply --strict-nets) ...
akcli check board.kicad_sch --intent intent.json           # exit 1 on ANY intent violation
```

The intent file is plain JSON (`{"protocol_version": 1, "mode": "exact",
"nets": {"SWCLK": ["U1.4", "J2.2"], ...}}`) — hand-edit it when the plan
changes, or write it from the block plan BEFORE drawing and use it as the
acceptance test. Matching is by pin membership, so renames don't false-fail;
`INTENT_NETS_SHORTED` catches two planned nets landing on one actual net,
`INTENT_EXTRA_MEMBER` catches accidental joins (`"mode": "subset"` skips it
when other tools add pins). For key nets only, prefer a small hand-written
intent file over a full snapshot — it asserts design intent, not incidental
wiring.

**Contracts → pin-level topology policy.** `akcli nets --intent-snapshot` asserts net
*membership* — which pins share a net. `akcli check <file> --contract contract.toml`
asserts a different, stricter layer: pin-level topology *policy* — require or forbid a
specific pin on a specific net, require or forbid two pins ever sharing a net, expected
component values, NC pins, and approved exceptions carrying an owner and expiry. For a
hard invariant like "FB2 must never touch V3V3", a contract is more reliable than manual
review (it is machine-checked on every run) and it composes with an intent snapshot —
use intent for "this is what I wired" and a contract for "this must always/never be true".

## (8) Verify behavior with `akcli sim` before ordering

`check` and `intent` prove the sheet is *connected as planned*; they say nothing
about whether the circuit *works*. Before you order boards, turn the block plan's
numeric intent (a divider's midpoint, an RC corner, a detector threshold, a
regulator's output) into a machine-checkable simulation:

```bash
akcli sim board.kicad_sch --deck-only                  # inspect the SPICE deck it would run
akcli sim board.kicad_sch --sim board.sim.json         # run + assert, exit 1 on any violation
```

`sim.json` declares the stimuli (a supply, a stimulus current, a signal source),
the analyses, and one bound per behavior you care about — e.g.
`{"name": "mid", "meas": "MAX v(mid) from=0 to=1m", "approx": "1.65", "tol": "0.02"}`.
Bounds accept the same engineering notation as `akcli calc`, so a value you
computed there drops straight into an assertion (see the akcli-design-calc skill).
Components resolve to SPICE devices via the `Sim.*`-fields → `sim.json` `models`
→ heuristic ladder; anything the tool cannot honestly model is reported
`SIM_UNMODELED` (a loud warning, never a silent guess) — give those parts a
`Sim.*` field, a `models` entry, or a `fit_diode` model card. `--deck-only`
works with no ngspice installed, so it doubles as a review/CI plan step. Full
reference: `docs/sim.md`.
