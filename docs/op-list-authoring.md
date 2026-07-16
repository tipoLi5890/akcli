# Op-list authoring guide

An **op-list** is the JSON document `akcli plan` / `akcli draw` execute against
a KiCad schematic. This guide is the practical companion to the formal schema
(`schemas/ops.schema.json`) and the per-executor support matrix
(`schemas/ops.capabilities.json`). An agent can also fetch the coordinate-contract
limits below mechanically via `akcli capabilities --json` (`.ops.constraints`:
`rotation_enum`, `wire_orthogonal_only`, `grid_mil`, `hierarchy`) instead of
hardcoding them from this doc. Scaffolding helpers:

```bash
akcli ops list                    # the 18-op vocabulary + 9 macros + required fields + support
akcli ops template place_component        # fill-in JSON skeleton (all fields)
akcli ops template add_wire --required-only
akcli ops validate ops.json               # cheap structural check (envelope+ops+macros) before plan/draw; the PreToolUse hook runs this automatically before draw --apply
akcli pins Device:R --at 2000 1000        # every pin's WORLD (x,y) for that placement
akcli pins Timer:NE555P --at 2600 1500 --symbols /path/Timer.kicad_sym --json
```

`akcli pins <lib_id>` resolves a symbol from the same sources the writer uses
(`--symbols` / config `[paths]` `.kicad_sym`) and prints each pin's number, name,
electrical type, and **world coordinate** for a `--at`/`--rotation`/`--mirror`
placement — the exact points wires, labels, and power ports must land on. It
mirrors the writer's `geometry.pin_world`, so a coordinate it prints is byte-for-byte
where `draw` will place that pin. Use it instead of guessing pin offsets.

## Envelope

```json
{
  "protocol_version": 1,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "ops": [ ... ]
}
```

- `protocol_version` is checked first; an executor rejects a higher major with
  `PROTOCOL_MISMATCH` rather than guessing.
- `target_file` may be overridden by the CLI's positional target.

## Coordinate contract (memorize this)

- **mils**, origin **top-left**, **+Y down**, snapped to the **50-mil grid**.
- Rotation is the enum `{0, 90, 180, 270}`; mirror `{none, x, y}`.
- A `"REF.PIN"` endpoint (e.g. `"U1.3"`) snaps to that pin's **exact world
  coordinate** — prefer it over raw `[x, y]` whenever a wire meets a pin.
- **`"at": "mid(REF.PIN,REF.PIN)"`** — accepted by `add_net_label`,
  `place_power_port`, `place_gnd`, `place_vcc` (and the `place_pwr_flag`
  macro): the midpoint of two **exactly axis-aligned** pins (any cross-axis
  offset fails with `NON_ORTHOGONAL_WIRE` naming both world coordinates — a
  tolerance would let the anchor leave the slightly diagonal wire and attach
  to nothing), snapped to the 50-mil grid *along* the wire axis, clamped
  into the span, cross-axis kept exact. Labels anchored at `mid()`
  auto-orient along the wire. This is how you name a wire between two facing
  pins without touching either pin.
- Wire `vertices` must form orthogonal segments; dogleg through an
  intermediate `[x, y]` when two pins don't share an axis.

## The vocabulary

Required fields per op are what the validator enforces; everything else is
optional. Run `akcli ops template <op>` for a full skeleton.

| Op | Required | Notes |
|---|---|---|
| `place_component` | `lib_id, designator, x_mil, y_mil` | optional `unit` places ONE unit of a multi-unit part (each unit is its own instance sharing the designator); `value`/`footprint` set the properties; the symbol must resolve from `--symbols`/config/`symbol_source` |
| `set_component_transform` | `designator` | rotation/mirror of an existing instance |
| `set_component_parameters` | `designator` | `reference`, `value`, `footprint`, free-form `parameters` map |
| `add_wire` | `vertices` | even, orthogonal list of `[x,y]` / `"REF.PIN"`; one segment per consecutive pair |
| `add_bus` | `vertices` | as `add_wire`, bus graphics |
| `add_junction` | `at` | usually unnecessary — the executor auto-inserts junctions at 3+-way meets and pin taps |
| `add_no_connect` | `pin` | `"REF.PIN"` or `[x,y]` |
| `add_net_label` | `name, at` | `at` also takes `"REF.PIN"` / `"mid(REF.PIN,REF.PIN)"`; `scope: local\|global\|hierarchical` (writer is flat — avoid `hierarchical`) |
| `place_power_port` | `lib_id, net_name, at` | auto-allocates the `#PWR` reference; merges by name everywhere; `at` also takes `"REF.PIN"` / `"mid(...)"` |
| `place_gnd` / `place_vcc` | `at` | sugar over `place_power_port` |
| `add_bus_entry` | `at` | optional `size` (default 2.54 mm @ 45°); each entry end must land on a bus or a wire, else `DANGLING_BUS_ENTRY` gates the write |
| `add_text` | `text, at` | the only graphic op; optional free `angle` |
| `rename_net` | `from, to` | rewrites matching label texts (`label`/`global_label`/`hierarchical_label`) **and** power-port net Values; optional `scope` restricts to one label kind (power Values only rewritten when unscoped); 0 matches = replay-safe note; the match count is reported. KiCad only (`altium: false`) |
| `delete_component` | `designator` | removes ALL instances; attached wires are left for the connectivity gate to flag — delete them explicitly, or set `"cascade": true` to also delete wires ending on any deleted pin's coordinate plus labels/no-connects anchored there; an anchored junction is removed only when fewer than two surviving wires still pass through it (a pure-X crossing of untouched wires keeps its junction — deleting it would silently split their net). Cascaded uuids are reported; absent target = replay-safe no-op |
| `delete_object` | `uuid` **or** `match` | remove ONE top-level object (wire/label/junction/text/...). `match: {kind, name?, at?}` addresses it without a uuid — exactly-one semantics (0 matches = replay-safe note, >1 = error listing the candidate uuids); `match.at` is exact mils, NOT grid-snapped |
| `move_component` | `designator, x_mil, y_mil` | one instance (optional `unit`); its properties travel along; wires do NOT stretch |
| `add_sheet` | `name, file, at, size` | hierarchical sheet: `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, and edge-computed sheet pins from optional `pins:[{name, type, side, offset_mil}]` (`type: input\|output\|bidirectional\|tri_state\|passive`, `side: left\|right\|top\|bottom`). `at`=TOP-LEFT corner, mils. Wires attach to a sheet pin **by coordinate** (`at`+`offset_mil` along the side, grid-snapped) — there is NO `Sheet.Pin` endpoint. KiCad only (`altium: false`); the child `.kicad_sch` is authored separately (`akcli new`) |

### Macro ops (expanded before validation)

Compound placements that expand into the core ops above **before** the
validator, the schema, or any executor sees them — `protocol_version` is
untouched. The `place_*` block macros use the collision-proof label-on-pin
pattern (labels anchored at `"REF.PIN"`, no wires emitted); netbuild merges a
same-name local label into the sheet's power/global net exactly like KiCad.
`connect_and_label` is the exception: it emits one wire plus one mid-wire
label (the facing-pin pattern below).

| Macro | Required | Expands to |
|---|---|---|
| `connect_and_label` | `from, to, net` | `add_wire [from, to]` + ONE `add_net_label` at the wire's midpoint — the canonical fix for facing-pin label collisions. `from`/`to` must BOTH be pin refs (label lands at `mid(from,to)`) or BOTH `[x,y]` points; `orientation`/`scope` optional |
| `place_pwr_flag` | `at` | `place_power_port power:PWR_FLAG` at `at` (`[x,y]` or `mid(REF.PIN,REF.PIN)`), default `rotation: 90`. Place it **MID-WIRE** — on-pin placement overlaps the other symbol's body (`check --layout` flags `LAYOUT_POWER_ON_PIN`). The flag never names a net |
| `terminate_unused_unit` | `designator, lib_id, unit, at, in_plus, in_minus, out` | `place_component` of the given unit + `power:<gnd>` port on `REF.<in_plus>` + `power:<vcc>` port on `REF.<in_minus>` + `add_no_connect` on `REF.<out>` (`vcc` default `VCC`, `gnd` default `GND`) — silences KiCad's `missing_input_pin` on spare op-amp/comparator units |
| `place_divider` | `x_mil, y_mil, top_net, mid_net, bottom_net` | 2 resistors (`designators`/`values`/`spacing_mil`/`lib_id` optional) + 4 pin-anchored labels; the shared `mid_net` label on both inner pins IS the connection |
| `place_decoupling` | `x_mil, y_mil, power_net` | 1 capacitor (`designator`/`value`/`gnd_net`/`lib_id` optional) + rail/ground labels on its pins |
| `place_pullup` | `x_mil, y_mil, net, rail_net` | 1 resistor (`designator`/`value`/`lib_id` optional) with rail on pin 1, signal on pin 2 |
| `place_led_indicator` | `x_mil, y_mil, net` | series R + LED to `gnd_net` (`designators`/`r_value`/`mid_net`/`spacing_mil` optional); the internal node label joins R.2 to the LED anode |
| `place_rc_filter` | `x_mil, y_mil, in_net, out_net` | series R + shunt C to `gnd_net` (`designators`/`r_value`/`c_value`/`spacing_mil` optional) |
| `place_crystal` | `x_mil, y_mil, in_net, out_net` | crystal + two load caps to `gnd_net` (`designators`/`value`/`load_c`/`spacing_mil` optional) — ST AN2867 topology |

`akcli ops list` prints them under the macro section; `akcli ops template
place_divider` emits a skeleton. `akcli calc <akcli-design-calc> --ops` emits these
macros directly (placeholder net names — edit them, then `akcli plan`), and
the validator accepts un-expanded macro documents.

## Validator strictness (what `plan` rejects)

The validator is deliberately unforgiving — a typo must fail loudly, not
silently place nothing:

- **Unknown fields are errors**, with a did-you-mean suggestion
  (`add_wire: unknown field 'vertexes' (did you mean 'vertices'?)`). Keys
  starting with `_` are annotation-safe and ignored — use them for comments.
- **Per-op field types are enforced** (a string where a coordinate belongs
  names the op index and the field).
- **Duplicate placements are a lint error**: two `place_component` ops with
  the same `(designator, unit)` in one document is a real double-place
  (`delete_component` earlier in the list releases the designator).
- **Enum slots are TypeError-safe**: a list/dict where an enum belongs
  (`op` name, `target_format`, rotation/orientation, mirror, `scope`, a
  sheet-pin `type`/`side`, …) yields a clean `OpError` naming the field, never
  an internal `TypeError` from an unhashable value.
- A crashing op handler surfaces as a per-op result with error code
  `INTERNAL` — never a traceback, and never a partial write.

## Execution pipeline (what happens to your ops)

1. `akcli plan <target> --ops ops.json` — validate + resolve, print what would
   change **plus the "Net changes" block** (the op-list is dry-applied to a
   temp copy and the before/after netlists diffed by pin membership). Never
   writes. `--no-net-diff` skips the diff.
2. `akcli draw <target> --ops ops.json` — **dry-run by default**: per-op
   results plus the connectivity verification plus the same net diff.
3. `... --apply` — atomic snapshot → temp → verify-on-temp → `os.replace`,
   with a `<target>.bak` alongside. Any op error or connectivity ERROR
   (`DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `DUPLICATE_UUID`,
   `UNRESOLVED_LIB_ID`, ...) refuses the write (exit 6). Add `--strict-nets`
   to also refuse when the net diff shows a split/merge touching a **named**
   net — the machine gate for the silent-merge trap below.
4. Re-read after applying (`akcli nets` / `akcli component`), or better:
   assert a design-intent file (`akcli check <target> --intent intent.json`)
   — an exit-0 apply proves the write landed, not that intent was met.

### Reading the "Net changes" block

```
Net changes:
  ! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.2>(2)   # a before-net's pins now live in 2+ nets
  ! MERGE MID + +3V3 -> MID                            # 2+ before-nets collapsed into one
  ~ VTH: +U1.7 (5->6 pins)                             # modified membership (added/removed pins)
  = RENAME MID -> VOUT (3 pins)                        # same pins, new display name — harmless
  + NEW BALL1_N (5)                                    # created net
  - GONE SENSE1 (3)                                    # removed net
```

`(none)` = connectivity is provably unchanged. `!` lines are the dangerous
ones: a **SPLIT** usually means you deleted a label or wire that was the only
bridge between fragments; a **MERGE** usually means a new wire/label shorted
two rails. Renames are matched by membership, so a rename can never masquerade
as remove+create. Unnamed fragments are anchored by their smallest member pin
(`<unnamed@R7.2>`). `--strict-nets` turns any `!` line touching a named net
into a refusal.

## Idempotency rules

- Created nodes get deterministic UUIDv5 keyed on the root sheet uuid plus
  `designator:op_index` (or tag:coords). **Replaying the same op-list file
  converges byte-identically after one apply.**
- Because identity includes `op_index`, never reorder or insert ops mid-list
  in a file you already applied — extend with a NEW delta op-list instead.
- Deletes are replay-safe no-ops when the target is already gone.

## Connectivity is name-based — and only dangling is a hard gate

- **Power ports and net labels merge by NAME**, everywhere (power ports/global
  labels cross sheets; local labels are sheet-local). A label or power port placed
  *directly on a pin's coordinate* connects that pin with **no wire** — handy for
  dense blocks where stubs would collide.
- **`add_net_label` and `place_power_port`/`place_gnd`/`place_vcc` accept
  `"at": "REF.PIN"`** — the anchor snaps to that pin's exact world coordinate,
  making label-on-pin first-class (no `akcli pins` arithmetic needed).
- **On-pin labels auto-orient.** When a label's anchor is a pin (via `"REF.PIN"`
  or a raw coordinate that hits a pin tip) and no `orientation` is given, the
  writer rotates the text AWAY from the symbol body and emits the matching
  `(justify ...)` — KiCad ignores a bare 180° angle, so without the justify the
  text renders straight over the part it names. An explicit `orientation` always
  wins. For a stub-end label, set `orientation` to the stub's direction
  (0=right, 90=up, 180=left, 270=down).
- **Verify the graphics too:** `akcli check <file> --layout` (also in the
  default check set for `.kicad_sch`) lints for symbol bodies drawn over each
  other, label text crossing a body or another label, and texts stacked on one
  anchor. Electrically perfect ≠ readable.
- **`power:PWR_FLAG` is special**: it marks a net as driven for ERC but does **not**
  name or merge a net. Use it to satisfy KiCad's `power_pin_not_driven` without
  side effects. (Do not give a *different* power symbol the value `PWR_FLAG`.)
- **The only hard connectivity write-gates are `DANGLING_ENDPOINT` and
  `DANGLING_BUS_ENTRY`.** Non-orthogonal and overlapping/collinear wires are
  *tolerated*, so two stubs that overlap or a wire that T-junctions another net
  can silently **merge** nets — a passing `--apply` proves the write landed, not
  that the netlist is right. Three rails catch the silent cases: the **Net
  changes** block on every plan/draw (read the `!` lines), `draw --apply
  --strict-nets` (refuses named-net splits/merges), and design-intent
  assertions (`akcli nets --intent-snapshot` before the edit,
  `akcli check --intent` after).

## KiCad label scoping — the true semantics

Verified against eeschema's own netlister (kicad-cli 10), because getting this
wrong silently changes netlists:

- **On the SAME sheet, a local label merges with a same-name global label or
  power port even when they are physically disconnected.** A local `+3V3`
  label on an isolated stub IS the `+3V3` rail. eeschema exports them as ONE
  net; akcli matches. Do not use a local label as a "private" name if any
  global/power object on the sheet shares it.
- **Across sheets, local labels NEVER merge.** A child sheet's local `+3V3`
  is `/child/+3V3`, a separate net from the global `+3V3`. Only global labels,
  power ports, and hierarchical pins cross sheets.
- **Junction rule (eeschema dialect):** a wire END touching another wire's
  mid-span does NOT connect without an explicit junction node. akcli's
  KiCad reader matches this; the *writer* auto-inserts junctions at 3+-way
  meets and pin taps, so drawn output is safe — but hand-drawn touches read
  back as disconnected (`check --nets` flags `NET_PIN_MIDSPAN_TOUCH`).
  (Altium's dialect DOES connect bare T-touches; the Altium reader honors
  that.)

## Hierarchical sheets (`add_sheet`)

`add_sheet` places a child-sheet reference on the parent. The authoring flow,
parity-verified against eeschema's own netlister (kicad-cli 10):

1. **Create both files.** `akcli new root.kicad_sch` and
   `akcli new child.kicad_sch` — each gets a root uuid (the namespace for
   deterministic op uuids). `add_sheet`'s `file` must point at an EXISTING child
   (`read_sch` requires it), though the writer's own `verify()` is single-doc
   and does not recurse, so `draw --apply` succeeds before the child is fully
   authored.
2. **Place the sheet on the parent.**
   `{"op":"add_sheet", "name":"power", "file":"child.kicad_sch",
   "at":[2000,1000], "size":[1000,800],
   "pins":[{"name":"VBUS","type":"input","side":"left","offset_mil":200}]}`.
   `at` is the TOP-LEFT corner (mils, +Y down); each sheet pin's anchor is
   `at`+`offset_mil` along its side, grid-snapped to 50 mil
   (left=`(x0, y0+off)`, right=`(x0+w, y0+off)`, top=`(x0+off, y0)`,
   bottom=`(x0+off, y0+h)`). Wire to that literal coordinate — there is **no**
   `SheetName.PinName` endpoint (a REF-anchor would collide with component refs).
3. **Pair it with a hierarchical label in the child.** A parent sheet pin and a
   child `hierarchical_label` of the **same name** are one cross-sheet net
   (reader's `_hier_key` pairing). In the child, drop the matching hierarchical
   label (`add_net_label … "scope":"hierarchical"`) on the corresponding pin.
4. **Verify the parity.** `akcli nets root.kicad_sch` recurses into the child and
   reports the merged cross-sheet net (root `VBUS` pin + child `VBUS` pin as ONE
   net) — confirm it matches your block plan, exactly as `kicad-cli` exports it.

Sheet uuids are deterministic (`sheet:<name>` / `sheet:<name>.pin.<pinname>`
keyed on the root + op index), so replay is byte-identical; the instances page
number is `op_index + 2` (root is page 1) and may be non-contiguous when
non-sheet ops interleave (KiCad tolerates it).

## Bus authoring (stage 2 semantics — labeled rips)

`add_bus` draws the bus polyline; `add_bus_entry` the diagonal rip. The netlist
semantics were arbitrated against real `kicad-cli` netlist exports, so author to
them exactly:

- **A rip's member is chosen by the WIRE-side label.** The bus carries a vector
  (`add_net_label` `NAME[a..b]` on the bus, e.g. `K[3..0]`); the individual wire
  ripped off the bus must carry its own plain label (`K2`) — THAT label selects
  which bus member the rip joins. **An unlabeled rip floats** (stays
  unconnected); a plain label placed directly on the bus selects nothing.
- **Vector expansion `NAME[a..b]` is inclusive at both ends, either order** —
  `K[3..0]` resolves K3, K2, K1, K0. A non-vector label on a bus contributes no
  members.
- **A `(bus_entry)` conducts between its two ends** — two wires ending on its two
  ends are one net even with no bus present. An entry end attaches to a **wire**
  only at a wire endpoint or a junction (a bare mid-span touch floats, same as a
  pin), but to a **bus** anywhere along a segment. Each end must land on a bus or
  a wire or the `DANGLING_BUS_ENTRY` gate refuses the write.
- **Scope:** local bus labels are sheet-scoped; a **global** bus label merges
  member nets across sheets, and a sheet-pin↔hierarchical-label bus port stitches
  parent/child — same rules as plain labels.

## Robust connectivity patterns (distilled from real sessions)

- **Facing pins: one coaxial wire + ONE mid-wire label.** Two pins facing
  each other on the same axis (555.OUT→R.1, R.2→C.1 chains) must NOT each get
  a label-on-pin: auto-orient extends both texts toward each other and
  `check --layout` flags the overlap. Use the `connect_and_label` macro —
  a straight pin-to-pin `add_wire` (zero geometric-merge risk) plus one label
  at `mid(from,to)`, auto-oriented along the wire.
- **`PWR_FLAG` goes MID-WIRE, never on a pin.** Anchoring the flag at a power
  symbol's pin ("#PWR01.1") stacks two bodies on one point —
  `LAYOUT_POWER_ON_PIN` / `LAYOUT_SYMBOL_OVERLAP`, and no rotation fixes it.
  The `place_pwr_flag` macro places `power:PWR_FLAG` at a grid point on the
  wire (`[x,y]` or `mid(REF.PIN,REF.PIN)`, default rotation 90 so the body
  extends into empty space). The flag marks the net driven for ERC and never
  names or merges a net.
- **Deleting labels can split nets — the Net-changes block catches it.** When
  a net's fragments are held together only by same-name labels, deleting
  "redundant" labels silently splits it (deleting both THR labels split THR
  into `THR(2) + <unnamed@R7.2>(2)` in a real session). Every fragment must
  keep a name anchor. Watch for `! SPLIT` lines; gate with `--strict-nets`.
- **Terminate spare multi-unit units.** Unused op-amp/comparator units must be
  PLACED and terminated (+in to GND, −in to a rail, output no-connect) or
  KiCad ERC warns `missing_input_pin` — `akcli check --erc` flags them as
  `ERC_UNPLACED_UNIT`. One `terminate_unused_unit` op does the whole ritual.
- **L-wire corners on pin tips short adjacent nets.** Two stubs on the same
  axis that overlap, or an L-bend landing exactly on another part's pin tip,
  merge nets with no visual tell — `check --nets` flags the corner-on-pin
  case as `NET_WIRE_CORNER_ON_PIN` (and a label that lost its wire as
  `NET_LABEL_UNATTACHED`); prefer label-on-pin or `connect_and_label` over
  hand-routed stub mazes in dense areas.
- **Snapshot intent before, assert after.** `akcli nets <sch> --intent-snapshot
  intent.json` captures the netlist you mean; after any surgery,
  `akcli check <sch> --intent intent.json` proves every listed pin is still on
  its net and nothing shorted (`INTENT_NETS_SHORTED`). Intent matches by
  membership, so renames don't false-fail.

## Worked example

```json
{ "protocol_version": 1, "target_format": "kicad", "target_file": "board.kicad_sch",
  "ops": [
    { "op": "place_component", "lib_id": "Device:R", "designator": "R1",
      "x_mil": 2000, "y_mil": 1000, "value": "10k" },
    { "op": "place_gnd", "at": [2000, 1200] },
    { "op": "add_wire", "vertices": ["R1.2", [2000, 1200]] },
    { "op": "add_net_label", "name": "SENSE", "at": [2000, 850], "scope": "global" },
    { "op": "add_wire", "vertices": ["R1.1", [2000, 850]] }
  ] }
```

More patterns (LDO block, decoupling, connector fanout, multi-unit gates) live
in the `akcli-schematic-authoring` skill.
