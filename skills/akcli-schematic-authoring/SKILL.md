---
name: akcli-schematic-authoring
description: >-
  Create a NEW KiCad schematic from written requirements — or surgically extend an
  existing sheet — by authoring an `akcli` op-list and driving the plan -> dry-run ->
  apply -> verify loop. Use this skill whenever the task involves: designing a circuit
  from a spec or datasheet ("設計電路", "design a circuit", "create a schematic from
  scratch"); placing components, wires, net labels, power ports, or junctions in a
  .kicad_sch via op-list JSON; picking real parts with JLC/LCSC sourcing before drawing;
  simulating the drawn circuit with the bundled ngspice ("模擬", simulate, run a SPICE
  sim, operating point, transient, DC sweep, assert node voltages);
  building or editing a hierarchical design (parent + child sheets, add_sheet,
  multi-sheet op-list, sheet reuse); or safely re-running / editing a previously
  drawn sheet. Triggers on keywords:
  new schematic, draw circuit, op-list, place_component, add_wire, net label,
  power port, decoupling, LDO block, connector fanout, hierarchical sheet,
  multi-sheet, child sheet, akcli plan, akcli draw,
  akcli sim, ngspice, SPICE deck, sim.json, JLC part selection, kicad_sch authoring.
---

# akcli-schematic-authoring — from requirements to a verified `.kicad_sch`

This skill covers *creating* circuits with the `akcli` op-list writer. For basic
read/analyze/draw mechanics, exit codes, error format, and the Altium (read-only)
path, **see the akcli-circuit-design skill** — do not re-derive them here. KiCad is
the only writable target. An op-list edits one sheet file — or **several in one
atomic transaction**: declare the children in the envelope's `sheets` map and tag
ops with `"sheet"` (see the hierarchy bullets in §4). A child `.kicad_sch` of an
existing hierarchy is also still a valid direct target; what never works is naming
a sub-sheet *path* from the parent (`HIERARCHICAL_UNSUPPORTED` — route by `sheet`
tag instead). Copy-paste-ready starter op-lists live in `examples/circuits/`
(CI-locked, so they cannot drift from the tool).

**The authoring loop (never skip a stage):**
requirements → block plan (groups + intent) → part selection → op-list
(group-local coordinates) → `akcli ops validate` → `akcli plan --render`
(read the **Net changes** block, LOOK at the SVG) → `akcli draw` (dry-run) →
`akcli draw --apply` (`--strict-nets` when editing an existing sheet) →
re-read + `akcli check` (+ `--intent`/`--contract` when they exist) →
`akcli groups --frame` + `akcli doc` for hand-off → **verify behavior with
`akcli sim` before ordering** (see below).

## (1) Block plan before any op

Decompose the requirements into named blocks (power entry, regulation, MCU,
connectors), list every rail and named net, and assign designators up front
(U1, C1..Cn, J1...). Sketch a placement map: one block per region of the sheet,
signal flow left→right, rails at the top, GND at the bottom. Only then write ops.

Make the plan machine-usable from the start:

- **Blocks become `groups`**: give each block a name + origin and author its
  ops in group-local coordinates (see the groups envelope in §4) — the block
  plan then survives into the file as `Group` properties, and framing (`akcli
  groups --frame`) and re-packing (`arrange --groups`, §10) come for free.
- **Reserve real estate with `akcli bbox`**: `akcli bbox <lib_id> --at X Y
  [--rotation R]` prints the symbol's world bounding boxes for a candidate
  placement (sibling of `pins`) — size each group's region from real body
  extents instead of guessing, so blocks don't collide when framed.
- **Write the intent file BEFORE drawing**: turn the block plan's named nets →
  pins table into a hand-written intent JSON (§7) and use it as the acceptance
  test for the whole session, not just a post-hoc snapshot.

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

`jlc add` output defaults under config `[paths] parts_dir` (fallback
`./akcli-parts`); pass `--footprint-lib NICK` so the Footprint field's nickname
matches the project's fp-lib-table (audit later with `akcli library audit`).

Component **values** come from `akcli calc`, not mental arithmetic: compute the
network (`vdivider-design`, `regulator-design`, `led`, `i2c-pullup`,
`crystal-caps`, ...) and place the returned `*_standard` E-series value; quote
the printed reference in the report (akcli-design-calc skill has the full catalog).
Design-type calcs also emit a ready op-list draft — `akcli calc vdivider-design
vin=3.3 vout=1.8 --ops divider.json` writes a `place_divider` macro op carrying
the snapped values (other design calcs emit their own macro or plain
`place_component` ops), so the computed network enters the sheet without
transcription.

### Datasheet-driven design — `jlc datasheet` before committing values

Never design an IC's surrounding circuit from remembered specs. Pull the PDF
and read it:

```bash
akcli jlc datasheet C2984661 --fetch --out datasheets   # one part -> ./datasheets (project-local)
akcli jlc datasheet board.kicad_sch --fetch --out datasheets  # every BOM line, one run
```

- **Link resolution:** links come from the part's EasyEDA record; a `no-link`
  row prints the LCSC product-page URL — fetch that page with a browser-grade
  fetcher (WebFetch), or `curl` the **manufacturer's** own PDF (vishay.com,
  ti.com, ... work; `lcsc.com` bot-gates direct downloads). `--fetch` verifies
  the `%PDF` magic, so a challenge page never masquerades as a datasheet on disk.
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
- Numbers that will gate a design decision belong in the **project-local facts
  store** (`./datasheets`, a committed team asset — see the
  akcli-datasheet-facts skill): `review facts add` pins them to the PDF's
  sha256 + page, and `jlc datasheet --fetch --out datasheets` co-locates the
  PDFs with the store instead of the personal cache.

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

Document shape and the op vocabulary (22 ops + 10 macros) are defined in `schemas/ops.schema.json`
(see also `docs/op-list-authoring.md` and `akcli ops list`/`ops template <op>`); per-executor support is in
`schemas/ops.capabilities.json`. Envelope: `{"protocol_version": 1,
"target_format": "kicad", "ops": [...]}`. The validator is strict: unknown
fields are errors with a did-you-mean hint (`_`-prefixed keys are safe
annotations), field types are enforced per op, and a duplicate
`(designator, unit)` placement in one document is rejected. **Macros are
validated the same way, BEFORE expansion** — a typo (`valu`) is a refusal, not
a silently-defaulted part — and a macro's `designators` list is **positional**,
each slot carrying a fixed part role (`place_led_indicator` is `[R, D]`,
`place_rc_filter` `[R, C]`, `place_crystal` `[Y|X, C, C]`, `place_divider`
`[R, R]`): writing them in the reading order you had in mind names the resistor
`D1` and is an error naming the slot. Emit the skeleton with
`akcli ops template <macro>` rather than guessing.

**Generate before you hand-write.** For a fresh block, `akcli ops from-intent
design.json -o ops.json` turns a design document — the `check --intent`
pin→net table plus a `components` map and optional `layout` hints
(`schemas/design-intent.schema.json`) — into a complete op-list of the
ordinary vocabulary (`place_component` + label-on-pin `add_net_label` +
`add_no_connect` on every unlisted pin), with the tool owning all geometry.
The output goes through the normal plan → dry-run → apply loop below, and the
SAME design file is the post-apply assertion:
`akcli check <sch> --intent design.json` must come back clean. Hand-authoring
remains the tool for surgical edits and anything the generator refuses
(`DESIGN_UNSUPPORTED` names the reason).

### Functional groups — the `groups` envelope (author blocks, not loose parts)

Declare each block once in the envelope; tag its ops with `"group"`. Tagged
coordinates are **group-local** (absolute = local + origin) — a whole module
moves by editing one origin, and macros propagate the tag onto their children:

```json
{ "protocol_version": 1, "target_format": "kicad",
  "groups": { "POWER": { "origin": [1000, 1000], "title": "Power supply" } },
  "ops": [
    { "op": "place_component", "group": "POWER", "lib_id": "Device:C",
      "designator": "C1", "x_mil": 500, "y_mil": 150, "value": "10u" } ] }
```

Membership persists as a hidden `Group` property on each symbol, so the sheet
remembers its blocks across sessions: `akcli groups board.kicad_sch` lists
them; `akcli groups board.kicad_sch --frame --apply` draws/refreshes a border
rectangle + title per group (keyed uuids replace stale frames in place;
`--margin` pads, default 200). `check --layout` lints the result
(`LAYOUT_GROUP_OVERLAP`, `LAYOUT_FRAME_STALE`), and `arrange --groups` re-packs
the blocks later (§10). Prefer this over hand-maintained `add_rectangle`
graphics for anything that is a functional block.

Rules the validator and executor enforce:

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
  a `PWR_FLAG` (or a power symbol whose body lands on the part) anchored on
  another symbol's pin tip (`LAYOUT_POWER_ON_PIN` — see `place_pwr_flag`
  below; a rail port at a pin tip is the blessed pattern and stays quiet).
  Fix the warnings; ERC cannot see them.
  `check --nets` adds attachment near-misses: a pin tip touching a wire
  mid-span with no junction is NOT connected (`NET_PIN_MIDSPAN_TOUCH`), an
  unattached label names nothing (`NET_LABEL_UNATTACHED`), and an L-wire
  corner on a pin tip is the classic accidental short
  (`NET_WIRE_CORNER_ON_PIN`).
- **Live dashboard**: `akcli view live <file>` serves a localhost timeline that
  re-renders the sheet (SVG + ERC badges + part/net counts) on every apply —
  keep it open for the whole loop. With no path it auto-discovers the single
  `.kicad_sch` in the CWD; bare `akcli view` serves the hub (calculators +
  idle /live).
- **The blessed connection ladder — pick the FIRST rung that fits** (full
  rationale in docs/op-list-authoring.md): (1) **label-on-pin** is the
  default for every connection (`add_net_label` at `"REF.PIN"`; rails via
  `place_gnd`/`place_vcc`/`place_power_port`) — zero geometric-merge risk,
  survives `arrange`; (2) `connect_and_label` only for facing coaxial pins;
  (3) `route_net` when a visible wire is wanted; (4) raw `add_wire` last,
  exact-geometry cases only. Bus/sheet/graphics ops are the advanced layer —
  only when the task explicitly demands them.
- **Wire vs label vs power port**: wire (`add_wire`, `"REF.PIN"` endpoints) for
  short in-block connections; `add_net_label` with `"scope": "local"` on a short
  wire stub for readable in-block nets; `"scope": "global"` for nets crossing
  blocks on the same sheet (`hierarchical` only when the net leaves the sheet
  through a `(sheet)` pin — see `add_sheet` below);
  `place_power_port` / `place_gnd` / `place_vcc` for rails (power ports merge by
  name everywhere and auto-allocate `#PWR` refs; ONE port per coordinate — a
  port's identity IS its position, so re-running a list that renames its rail
  rewrites that port, while a second port on the same point is refused with
  `POWER_PORT_STACKED`, and a port you MOVE leaves the old node behind for you
  to `delete_object`). Connectivity is name-based, so
  overlapping/collinear stubs or a wire T-junctioning another net silently MERGE
  nets — the hard write-gates are `DANGLING_ENDPOINT`/`DANGLING_BUS_ENTRY` only,
  so read the **Net changes** block on every plan/draw and verify membership
  with `akcli nets` after applying; never trust a clean apply alone. Label
  scoping truth (matches eeschema): a local label DOES merge with a same-name
  global label or power port on the SAME sheet even when physically
  disconnected — never use a rail name as a "private" local label.
- **Cross-block runs — `route_net`**: for a pin-to-pin connection that is NOT
  coaxial, `{"op": "route_net", "from": "U1.7", "to": "J2.3", "style": "auto",
  "label": "SCL", "scope": "global"}` emits a deterministic orthogonal route —
  straight when coaxial, else an L whose corner provably avoids every placed
  pin tip (a coincident corner silently merges nets), falling back to a
  3-segment z; optional `label` names the net ONCE at the longest segment's
  midpoint (`style: hv|vh|z` forces a shape). Between groups, though, prefer a
  label on each pin over a long routed wire — labels survive `arrange` re-packs.
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
- **Annotation & sheet furniture**: `add_text` (`text`, `at`, optional free
  `angle`) for short notes; `add_text_box` (`text`, `at`, `size`) for design
  rationale paragraphs; `add_rectangle` (`start`, `end`, optional `key` for
  idempotent replace) for ad-hoc frames — but functional-block borders should
  come from `groups --frame`, not hand-drawn rectangles. A `key` (accepted by
  `add_text`/`add_text_box`/`add_rectangle`) must be **unique per op-list**:
  the node uuid is derived from `(kind, key)` alone, so the validator refuses a
  duplicate rather than letting the second op replace the first's node while
  both report ok. `set_title_block`
  (`title`/`date`/`rev`/`company`/`comment1..`) stamps the sheet metadata that
  `akcli new --title` didn't set.
- **Repetitive placements — `place_array`**: N same-symbol parts in one op
  (`lib_id`, `designator_prefix`, `count`, `x_mil`/`y_mil`, `pitch_mil`,
  `direction`, per-part `values`) — pull-up banks, LED bars, connector
  terminations; cheaper and less error-prone than N `place_component` ops.
  When every element gets a label-on-pin, prefer `direction: right` (or a
  wider `pitch_mil`): a `down` stack auto-orients the labels horizontally
  across the neighbor below and `check --layout` fills with
  `LAYOUT_LABEL_OVER_SYMBOL`.
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
- **Hierarchical sheets — `add_sheet`**: a parent `(sheet)` pin and a
  same-name **hierarchical label** in the child merge into ONE cross-sheet net
  (parity-verified against eeschema; confirm with `akcli nets` on the root).
  Wires attach to a sheet pin **by coordinate** — there is NO `Sheet.Pin`
  endpoint — and the child `.kicad_sch` must already exist (`akcli new` it
  first): pointing at a missing file is refused with `SHEET_FILE_MISSING`,
  because a dangling sheet makes every later `nets`/`check`/`render` on the
  design fail. A part drawn INTO a child file joins its siblings' sheet paths
  automatically. KiCad only. Full flow + pin fields: the "Hierarchical sheets"
  section of `docs/op-list-authoring.md`.
- **Multi-sheet op-lists — the `sheets` envelope (0.20)**: declare children
  once (`"sheets": {"amp": {"file": "amp.kicad_sch"}}`) and tag ops with
  `"sheet": "amp"` to route them onto that file — so `add_sheet` + populate
  the child is ONE op-list, ONE net-diff over the whole would-be tree, ONE
  atomic write (all files staged + verified before the first replace;
  pre-write bytes in `.akcli/backups/runs/<run_id>/`; `akcli undo <target>
  --run <run_id> --apply` restores every file together, and again = redo).
  An undeclared tag is `SHEET_UNKNOWN` with a did-you-mean; a tagged macro
  expands with the tag on every child op. Each routed file keeps its own
  uuid namespace, so replay stays byte-identical per file.
- **Sheet reuse — per-instance references**: a child instantiated N times is
  N annotation scopes, so a scalar `designator` there is refused with
  `INSTANCE_REFS_REQUIRED` (the message lists the instance paths to use —
  copy them into `"instance_designators": {"<path>": "R101", "<path2>":
  "R201"}`; `designator` must be one of the values). Renames use
  `set_component_parameters.instance_references`; power ports need no map
  (one `#PWR0n` is auto-allocated per instance). Addressing a part by ANY
  instance's refdes works (`R201.1` finds the same symbol `R101.1` does).
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

(Connector fanout follows the same shapes: a short wire stub + global label
per signal pin, `place_gnd` on the ground pin, `add_no_connect` on unused pins
— `{"op": "add_no_connect", "pin": "J1.4"}`.)

## (5) Validate → dry-run → apply

Every `place_component` / power-port symbol must resolve from a source: per-op
`symbol_source`, repeatable `--symbols` (a `.kicad_sym`, or a `.kicad_sch` whose
inline `lib_symbols` is harvested), or config `[paths]` entries ending in
`.kicad_sym`. As a LAST resort a `lib_id` nickname the explicit sources do not
cover is looked up in the project and global `sym-lib-table`, so `Device:R`
resolves against an installed KiCad — an explicit `--symbols` always wins, and
passing it stays the deterministic recommendation (the fallback depends on the
machine's KiCad install). A miss is still `SYMBOL_NOT_FOUND`.

```bash
SYMS=/usr/share/kicad/symbols    # macOS: /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols
akcli ops validate ldo.json      # structural check first — no target needed, exit 6 lists
                                 # EVERY problem at once (the PreToolUse hook runs this too)
akcli plan board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym" \
  --render preview.svg    # never writes; LOOK at the dry-applied SVG before drawing
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym"      # dry-run (the default)
akcli draw board.kicad_sch --ops ldo.json --symbols "$SYMS/Regulator_Linear.kicad_sym" \
  --symbols "$SYMS/Device.kicad_sym" --symbols "$SYMS/power.kicad_sym" --apply
```

Exit 6 means an op errored or connectivity found an ERROR/CRITICAL
(`DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `UNRESOLVED_LIB_ID`, ...), or a
hierarchy gate refused (`SHEET_UNKNOWN`, `INSTANCE_REFS_REQUIRED`,
`PROJECT_ROOT_AMBIGUOUS` — each remediation names the exact fix; see the
akcli-circuit-debug skill): on
`--apply` nothing was written. Another exit-6 cause is `TARGET_LOCKED`:
`draw`/`arrange`/`undo --apply` refuse to write while the KiCad GUI holds the
file open (a `~<name>.lck` lock file). After confirming it is safe, use
`--apply --allow-open`, then File > Revert in KiCad to pick up the change. Use
`--json` for machine output:
`{applied, status, ops[], connectivity[], net_diff}` — a failed op carries a
`remediation` field saying what to do next. On `OP_UNSUPPORTED`,
`PROTOCOL_MISMATCH`, or geometry errors: stop and report, do not retry
blindly. After a successful `--apply`, `akcli render board.kicad_sch` gives
you an SVG to *look at* what you placed (it writes `board.kicad_sch.svg` next
to the input — pass `-o -` when you only want the SVG on stdout and no file
left behind), `akcli doc board.kicad_sch` builds
the Markdown pinout book (pin→net tables + rails + BOM) for human hand-off,
and `akcli log .` shows the write journal (every plan/draw/undo with its
op-list hash and net-diff verdict). Pass `--note "why"` on writes so the
journal carries the design intent, not just the mechanics — resuming a
session starts from `akcli log .` + `akcli read` + `akcli check --intent` +
`akcli undo --list`, never from memory (see docs/agent-state.md). The
`.akcli/` state root (journal + rotated backups) is self-ignoring — it drops
its own `.gitignore`; what belongs in git is `akcli.toml`, the intent JSON,
the contract TOML, and `datasheets/`. Do not pass `--dry-run` — it is
accepted but inert; omitting `--apply` already is the dry run.

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

**The diff's scope is the sheet TREE, not the file (0.20).** Editing a child
of a hierarchy evaluates before/after nets from the resolved ROOT (the
`--json` payload then carries `project_root` + `evaluation_scope: "tree"`),
so a global label's member count includes what the sibling sheets contribute
— the truncated single-file view silently under-reported exactly the
splits/merges `--strict-nets` exists to catch. The root is auto-discovered;
several candidate roots refuse with `PROJECT_ROOT_AMBIGUOUS` (pass `--root
FILE`; `--flat` is the explicit opt-out back to the single-file view — under
`--strict-nets` an unresolvable root fails CLOSED). Multi-sheet
(`sheets`-routed) runs report per-file results (`run_id` + `files[]`) and
their status line names the `undo --run` command that reverts the whole
transaction.

## (6) Idempotency and safe re-runs

- Created nodes get deterministic UUIDv5 keyed on the root sheet uuid plus the
  node's own CONTENT — **never the op's index**: symbols/pins key on the
  designator, wires on their endpoints, labels on net name + the anchor as
  written (`"R1.2"`), sheets on child file + name, graphics on `tag:coords`
  (annotation `key`s ignore coords entirely). **Re-running the same op-list file
  converges** — prior same-uuid nodes are replaced, `#PWR` refs are recovered by
  position, re-applies stay byte-identical.
- **Editing an applied op-list is safe.** Inserting, reordering, or deleting ops
  does not change any untouched node's uuid, so a re-apply updates in place
  instead of stacking a second copy of everything after the edit. What still
  keys on position is *geometry*: a wire, port, graphic or annotation whose
  coordinates change becomes a new node and the old one is left behind — remove
  it with `delete_object` (or use an annotation `key`, which survives moves).
  Two ops that would seed ONE uuid (two identical wires, two texts on a point,
  two power ports on a coordinate) are refused, naming both op indices.
- **One-time migration:** a file written by a *pre-0.18* akcli carries the old
  `<content>:op_index` uuids for wires/labels/junctions/no-connects/bus
  entries/sheets/graphics (and pre-0.17 for parts). Re-apply the UNEDITED
  op-list once (or start from `akcli undo`) before editing it; editing first can
  duplicate nodes whose original op index was ≠ 0.
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
akcli net board.kicad_sch --same U1.7 C3.1             # assert THE connection you just drew
akcli net board.kicad_sch --why U1.7                   # a pin shows (no net)? attachment
                                                       # diagnosis + nearest wire/label + hints
akcli check board.kicad_sch --fail-on never            # ERC-lite + power + BOM + nets + layout
                                                       # + integrity (+ --pairs for diff-pair/bus)
akcli check board.kicad_sch --integrity --fail-on never  # the write gate's document checks alone,
                                                       # read-only: dangling endpoints, duplicate
                                                       # uuids/designators, unresolved lib_ids,
                                                       # instance paths, no-connect conflicts
akcli diff board.kicad_sch.bak board.kicad_sch --fail-on never  # the delta is exactly what you drew
                                                                # (--bom adds the BOM-line delta)
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
hard invariant like "the regulator feedback node must never touch RAIL_A", a contract is more reliable than manual
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
works with no ngspice installed, so it doubles as a review/CI plan step.

Three gates that decide whether a `PASS` means anything (full rules:
`docs/sim.md`):

- **Values are gated, not guessed.** A passive's value must parse: IEC 60062
  forms work (`2M2`, `1k5`, `4R7`; `1M` = 1 MΩ → SPICE `1MEG`), prefixes are
  case-insensitive so Altium-style upper-case values pass (`10U`, `100N`,
  `4U7`, `1T`, and Greek `μ`/micro `µ`) — the ONE exception is `m` (milli) vs
  `M` (mega), where case is the whole meaning — a split unit is
  re-joined (`4.7 k`, `100 nF`), and trailing rating/tolerance tokens are
  dropped with a `SIM_VALUE_NORMALIZED` warning (`10k 1%` → `10k`). Anything
  unparseable (`LED red`, `TBD`, or an ohms-marked value on an `L`) is
  `SIM_VALUE_INVALID`, exit 6 — the tool refuses to simulate rather than let
  ngspice read `"10k 1%"` as `10`. Numbers Python accepts but ngspice reads
  differently (or not at all) are refused for the same reason: `1_000`
  (ngspice reads 1), `inf`, `nan`, `1e400`.
- **Unpopulated parts fail the run they matter to.** A `DNP`/`NP`/`OPEN` value,
  `Sim.Enable=0`, or a `models` `skip` leaves the part out of the deck and
  reports `SIM_PART_SKIPPED`; that finding is an **ERROR** (exit 1) when the
  part has ≥2 connected nodes and an assertion measures one of them — a skipped
  series element makes the asserted number meaningless. Model it or stop
  asserting on that node.
- **`op` analyses cannot carry assertions.** ngspice's `.meas` reads a plot and
  an operating point produces none, so an assert bound to `op` is refused up
  front with `SIM_OP_ASSERT_UNSUPPORTED` (exit 2). Assert an operating point
  with a **single-point `dc` sweep** instead (`"dc": "Vsupply 3.3 3.3 1"`).

Go beyond the single nominal run when the design has margins to defend:
`--sweep R21=2.2k,3.3k --sweep temp=0,25,60` re-runs every assertion across
the corner grid (≤64 corners — component overrides and temperature compose),
and `--wave out.csv` dumps the waveform vectors for plotting. When a review
finding is quantitative, `akcli review testbench` auto-generates and runs a
focused subcircuit deck from the finding itself — no hand-written `sim.json`
needed for that check. Full reference: `docs/sim.md`.

## (9) Adopt review proposals — the finding→fix loop

When a review run produced findings with `fix_params`, do not hand-derive the
fix — let `propose` recompute it:

```bash
akcli review analyze board.kicad_sch --out review.findings.json
akcli review propose review.findings.json --out proposals.json
```

Each proposal carries the source finding's fingerprint, a recomputed +
E-series-snapped value, and up to three drafts. Adoption rules (details:
akcli-schematic-review skill): an **`oplist_draft`** is a complete protocol-1
document — save it and run it through the NORMAL `plan` → `draw --apply`
pipeline, never bypassing plan; a non-empty **`requires_confirmation`** means
the draft is null BY DESIGN — ask the user (often the answer is a facts file,
akcli-datasheet-facts, which makes the next `propose` run auto-applicable);
**`kind: layout`** is a manual PCB action (akcli writes schematics only); a
**`contract_draft`** gets pasted into the project's contract TOML as a
standing gate. Close the loop: re-run analyze and `akcli review diff
review.findings.json new.findings.json` — the finding must land under
`resolved`, with nothing new unexplained.

## (10) Group re-layout — rigid, net-preserving moves

To tidy a grown sheet into functional blocks, never hand-move parts (labels
strand). If the sheet was authored with the groups envelope (§4), bare
`--groups` derives the block map from the persisted `Group` properties;
otherwise supply a TOML/JSON map of block names → designator lists:

```bash
akcli library check-lock .                    # refuse to write under an open KiCad GUI
akcli arrange board.kicad_sch --groups                        # bare: map from Group properties
akcli arrange board.kicad_sch --groups groups.toml            # explicit map — both dry-run
akcli arrange board.kicad_sch --groups --page-width 8000 --frames --apply
```

Each part moves as a rigid bundle with the power symbols riding on its pins,
via `move_component` ops carrying `carry_labels`/`carry_wires` — with
label-on-pin connectivity the re-layout is net-preserving by construction, and
the pipeline REFUSES to write on any net change. Layout knobs: `--group-gap`
(channel between blocks, default 1200), `--row-width` (shelf wrap inside a
block), `--page-width` (2D side-by-side packing of blocks, wrapping past the
width — default is a single column), `--frames` (redraw each group's border +
title after packing). `akcli undo` reverts (`--list` shows the backup stack).

**When the net-preservation refusal fires** (named nets still ride on
cross-group wires), don't hand-fix and don't reach for `--allow-net-changes`:

```bash
akcli arrange board.kicad_sch --groups --propose-labels fix.json   # repair draft
akcli plan board.kicad_sch --ops fix.json                          # normal pipeline
akcli draw board.kicad_sch --ops fix.json --apply --strict-nets
akcli arrange board.kicad_sch --groups --frames --apply            # now re-pack
```

`--propose-labels` writes a label-on-pin op-list covering every named net that
spans groups, so the re-pack stops depending on cross-group wires — the
propose → apply → re-arrange loop is the supported repair path;
`--allow-net-changes` is a last resort taken only after reading the exact
`!` lines it would permit. For a single surgical move, `move_component` with
`"carry_labels": true, "carry_wires": true` is the same rigid semantics as
one op.
