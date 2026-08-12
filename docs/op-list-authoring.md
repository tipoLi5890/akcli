# Op-list authoring guide

An **op-list** is the JSON document `akcli plan` / `akcli draw` execute against
a KiCad schematic. This guide is the practical companion to the formal schema
(`schemas/ops.schema.json`) and the per-executor support matrix
(`schemas/ops.capabilities.json`). An agent can also fetch the coordinate-contract
limits below mechanically via `akcli capabilities --json` (`.ops.constraints`:
`rotation_enum`, `wire_orthogonal_only`, `grid_mil`, `hierarchy`) instead of
hardcoding them from this doc. Scaffolding helpers:

```bash
akcli ops list                    # the 22-op vocabulary + 10 macros + required fields + support
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

## Start from the pin→net table: `ops from-intent`

For a NEW block, the recommended starting point is not writing ops by hand but
**generating** them from a design document — the `check --intent` pin→net
table plus a `components` map (and optional `layout` hints; see
`schemas/design-intent.schema.json`):

```bash
akcli ops from-intent design.json -o ops.json --symbols MyLib.kicad_sym
akcli plan board.kicad_sch --ops ops.json --symbols MyLib.kicad_sym
```

The generator owns all geometry (uniform cells sized to body + pins +
estimated label text, 50-mil grid) and emits only the **ordinary vocabulary**
— `place_component`, label-on-pin `add_net_label`, `add_no_connect` for every
unlisted pin (`layout.no_connect_unlisted: false` opts out; the other
`layout` keys are `origin`, `columns`, `margin_mil` and per-component
`group`) — so the output is a normal op-list: edit it, re-run it through
`plan`/`draw`, `arrange --groups` it later. Round trip: the SAME design file
is the assertion baseline, so after `draw --apply`,
`akcli check board.kicad_sch --intent design.json` must report zero findings.
It refuses (exit 6, `DESIGN_UNSUPPORTED`) instead of guessing: pins addressed
by name instead of number (the message hands over the correct number),
wildcard members, a REF with no `components` entry, one pin in two nets,
coincident stacked pins on different nets, and bus-syntax net names.

## Envelope

```json
{
  "protocol_version": 2,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "groups": { "POWER": { "origin": [1000, 1000], "title": "Power supply" } },
  "ops": [ ... ]
}
```

- `protocol_version` is checked first; an executor rejects a higher major with
  `PROTOCOL_MISMATCH` rather than guessing.
- `target_file` is documentation only — `plan`/`draw` always act on the CLI's
  positional target, which is REQUIRED; the field is never read (generators
  emit it so an op-list records what it was authored against).
- `groups` (optional) declares functional modules — see
  [Modular design with functional groups](#modular-design-with-functional-groups).
- `sheets` (optional) declares child `.kicad_sch` files an op can route onto
  via a per-op `"sheet"` tag, so one op-list edits the root and its children
  in a single atomic transaction (with a `run_id` for `akcli undo --run`) —
  see [Multi-sheet op-lists](#multi-sheet-op-lists-sheets-map--per-op-sheet-020).

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

Refusals you will hit while authoring (all exit `6`, nothing written):
`OFF_GRID` (coordinate not on the 50-mil grid), `BAD_ANGLE` (rotation outside
`{0,90,180,270}`), `NON_ORTHOGONAL_WIRE` (diagonal segment — route it as an
L), `OVERLAP` (another object already at that position), `SYMBOL_NOT_FOUND`
(pass the defining library via `--symbols`), `OP_UNSUPPORTED` (unknown op or
field), `PROTOCOL_MISMATCH`, `GROUP_UNKNOWN` / `GROUP_NO_ORIGIN`,
`HIERARCHICAL_UNSUPPORTED`, `POWER_PORT_STACKED`, `SHEET_FILE_MISSING`,
`VERIFY_FAILED` (connectivity gate), `TARGET_LOCKED` (KiCad holds the file
open — `--allow-open` overrides). `akcli ops template <op>` prevents most.

## The vocabulary

Required fields per op are what the validator enforces; everything else is
optional. Run `akcli ops template <op>` for a full skeleton.

| Op | Required | Notes |
|---|---|---|
| `place_component` | `lib_id, designator` + position | position is exactly ONE of `x_mil`+`y_mil` (absolute) or `anchor` (+`offset_mil`): `"anchor": "U1.3"` is that pin's world tip, bare `"U1"` the component origin — resolved at execution time, so the anchor may be placed earlier in the SAME list; `offset_mil` is a world-frame `[dx, dy]`, the result grid-snaps. Optional `unit` places ONE unit of a multi-unit part; `value`/`footprint` set the properties; the symbol must resolve from `--symbols`/config/`symbol_source`; optional `instance_designators` supplies per-instance references on a reused sheet (see [Multi-sheet op-lists](#multi-sheet-op-lists-sheets-map--per-op-sheet-020)) |
| `set_component_transform` | `designator` | rotation/mirror of an existing instance |
| `set_component_parameters` | `designator` | `new_designator` (rename), `value`, `footprint`, free-form `parameters` map; optional `instance_designators` renames per instance on a reused sheet (same field name `place_component` uses) |
| `add_wire` | `vertices` | even, orthogonal list of `[x,y]` / `"REF.PIN"`; one segment per consecutive pair |
| `route_net` | `from, to` | deterministic orthogonal auto-route between two endpoints: coaxial -> straight; else an L whose corner provably avoids every placed pin tip (a coincident corner silently merges nets), falling back to a 3-segment z. `style: auto\|hv\|vh\|z`; optional `label` names the net ONCE at the longest segment's midpoint — the non-coaxial `connect_and_label` |
| `add_bus` | `vertices` | as `add_wire`, bus graphics |
| `add_junction` | `at` | usually unnecessary — the executor auto-inserts junctions at 3+-way meets and pin taps |
| `add_no_connect` | `pin` | `"REF.PIN"` or `[x,y]` |
| `add_net_label` | `name, at` | `at` also takes `"REF.PIN"` / `"mid(REF.PIN,REF.PIN)"`; `scope: local\|global\|hierarchical` (use `hierarchical` only in a CHILD sheet file, to pair with the parent's same-name sheet pin — see [Hierarchical sheets](#hierarchical-sheets-add_sheet)) |
| `place_power_port` | `lib_id, net_name, at` | auto-allocates the `#PWR` reference; merges by name everywhere; `at` also takes `"REF.PIN"` / `"mid(...)"`. One port per coordinate — a second op on the same point is `POWER_PORT_STACKED` (exit 6) |
| `place_gnd` / `place_vcc` | `at` | sugar over `place_power_port` |
| `add_bus_entry` | `at` | optional `size` (default 2.54 mm @ 45°); each entry end must land on a bus or a wire, else `DANGLING_BUS_ENTRY` gates the write |
| `add_text` | `text, at` | free-floating text; optional free `angle`; optional `key` = stable replacement handle |
| `add_rectangle` | `start, end` | top-level graphic border box (module frames); optional `stroke_width_mil`/`fill` (`none\|outline\|background`) and `key`. Connectivity-neutral; delete by uuid or kind (it has no `at`) |
| `add_text_box` | `text, at, size` | bordered multi-line note box (`at` = TOP-LEFT, `\n` for line breaks); optional `angle`/`key`. Connectivity-neutral; grammar fixture-verified against a real KiCad |
| `set_title_block` | at least one of `title, date, rev, company, comment1..9` | find-or-create edit of the title block (kept after `paper`, before `lib_symbols`); unchanged values replay as a no-op note |
| `rename_net` | `old_name, new_name` | rewrites matching label texts (`label`/`global_label`/`hierarchical_label`) **and** power-port net Values; optional `scope` restricts to one label kind (power Values only rewritten when unscoped); 0 matches = replay-safe note; the match count is reported. KiCad only (`altium: false`) |
| `delete_component` | `designator` | removes ALL instances; attached wires are left for the connectivity gate to flag — delete them explicitly, or set `"cascade": true` to also delete wires ending on any deleted pin's coordinate plus labels/no-connects anchored there; an anchored junction is removed only when fewer than two surviving wires still pass through it (a pure-X crossing of untouched wires keeps its junction — deleting it would silently split their net). Cascaded uuids are reported; absent target = replay-safe no-op |
| `delete_object` | `uuid` **or** `match` | remove ONE top-level object (wire/label/junction/text/...). `match: {kind, name?, at?}` addresses it without a uuid — exactly-one semantics (0 matches = replay-safe note, >1 = error listing the candidate uuids); `match.at` is exact mils, NOT grid-snapped |
| `move_component` | `designator` + position | destination is `x_mil`+`y_mil` OR `anchor` (+`offset_mil`), like `place_component`. One instance (optional `unit`); its properties travel along; wires do NOT stretch unless `carry_wires`, and labels anchored on the moved pins are left behind unless `carry_labels` (the two flags are independent) |
| `add_sheet` | `name, file, at, size` | hierarchical sheet: `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, and edge-computed sheet pins from optional `pins:[{name, type, side, offset_mil}]` (`type: input\|output\|bidirectional\|tri_state\|passive`, `side: left\|right\|top\|bottom`). `at`=TOP-LEFT corner, mils. Wires attach to a sheet pin **by coordinate** (`at`+`offset_mil` along the side, grid-snapped) — there is NO `Sheet.Pin` endpoint. KiCad only (`altium: false`); the child `.kicad_sch` must ALREADY exist (`akcli new <child>` first) or the op is refused with `SHEET_FILE_MISSING` |

### Macro ops (validated, then expanded)

Compound placements that the validator checks **first** — a macro with a
bogus field must never be expanded, or the expander would ignore the field
and manufacture a plausible-looking circuit — then expands into the core ops
above before any executor sees them; `protocol_version` is untouched. The `place_*` block macros use the collision-proof label-on-pin
pattern (labels anchored at `"REF.PIN"`, no wires emitted); netbuild merges a
same-name local label into the sheet's power/global net exactly like KiCad.
`connect_and_label` is the exception: it emits one wire plus one mid-wire
label (the facing-pin pattern below).

| Macro | Required | Expands to |
|---|---|---|
| `connect_and_label` | `from, to, net` | `add_wire [from, to]` + ONE `add_net_label` at the wire's midpoint — the canonical fix for facing-pin label collisions. `from`/`to` must BOTH be pin refs (label lands at `mid(from,to)`) or BOTH `[x,y]` points; `orientation`/`scope` optional (this macro kept the name `orientation`; the `add_net_label` it expands into takes `rotation`) |
| `place_pwr_flag` | `at` | `place_power_port power:PWR_FLAG` at `at` (`[x,y]` or `mid(REF.PIN,REF.PIN)`), default `rotation: 90`. Place it **MID-WIRE** — on-pin placement overlaps the other symbol's body (`check --layout` flags `LAYOUT_POWER_ON_PIN`). The flag never names a net |
| `terminate_unused_unit` | `designator, lib_id, unit, at, in_plus, in_minus, out` | `place_component` of the given unit + `power:<gnd>` port on `REF.<in_plus>` + `power:<vcc>` port on `REF.<in_minus>` + `add_no_connect` on `REF.<out>` (`vcc` default `VCC`, `gnd` default `GND`) — silences KiCad's `missing_input_pin` on spare op-amp/comparator units |
| `place_divider` | `x_mil, y_mil, top_net, mid_net, bottom_net` | 2 resistors (`designators`/`values`/`spacing_mil`/`lib_id` optional) + 4 pin-anchored labels; the shared `mid_net` label on both inner pins IS the connection |
| `place_decoupling` | `power_net` + position | 1 capacitor (`designator`/`value`/`gnd_net`/`lib_id` optional) + rail/ground labels on its pins. Position is `x_mil`+`y_mil` OR `anchor` (+`offset_mil`) — `"anchor": "U1.VCC"` drops the cap next to the pin it decouples |
| `place_pullup` | `net, rail_net` + position | 1 resistor (`designator`/`value`/`lib_id` optional) with rail on pin 1, signal on pin 2; same position forms as `place_decoupling` |
| `place_array` | `lib_id, designator_prefix, count, x_mil, y_mil` | N identical parts in a row/column at `pitch_mil` (default 400) stepping `direction: right\|down\|left\|up`, named `<prefix><start_index>..`; shared `value` or per-element `values` (length = count). Placement sugar — labels/wiring stay explicit |
| `place_led_indicator` | `x_mil, y_mil, net` | series R + LED to `gnd_net` (`designators`/`r_value`/`mid_net`/`spacing_mil` optional); the internal node label joins R.2 to the LED anode |
| `place_rc_filter` | `x_mil, y_mil, in_net, out_net` | series R + shunt C to `gnd_net` (`designators`/`r_value`/`c_value`/`spacing_mil` optional) |
| `place_crystal` | `x_mil, y_mil, in_net, out_net` | crystal + two load caps to `gnd_net` (`designators`/`value`/`load_c`/`spacing_mil` optional) — ST AN2867 topology |

`akcli ops list` prints them under the macro section; `akcli ops template
place_divider` emits a skeleton. `akcli calc <akcli-design-calc> --ops` emits these
macros directly (placeholder net names — edit them, then `akcli plan`), and
the validator accepts un-expanded macro documents — macro fields are validated
**before** expansion, so a typo (`valu`) or an invented field is a refusal, not
a silently-defaulted part.

**`designators` is positional, and the validator enforces it.** Each slot is a
fixed part: `place_led_indicator` is `[R, D]`, `place_rc_filter` is `[R, C]`,
`place_crystal` is `[Y|X, C, C]`, `place_divider` is `[R, R]`;
`place_decoupling`'s `designator` is the C and `place_pullup`'s is the R.
Writing them in the "reading order" you had in mind (`["D1", "R3"]`) would
otherwise name the resistor D1 and the LED R3 — electrically identical, BOM
wrong, no finding anywhere — so a refdes prefix that contradicts its slot is an
`OP_UNSUPPORTED` error naming the slot. Only `place_divider` /
`place_decoupling` / `place_pullup` accept a `lib_id`: overriding it there to
a different part class hands the convention back to you and lifts the check.
The other macros hard-code their parts and reject a `lib_id` field outright.
`terminate_unused_unit` and `place_array` choose no part, so their designators
are unconstrained.

## Modular design with functional groups

Real designs are drawn module by module (power, MCU, sensing, ...) with each
module's parts placed together. The `groups` envelope makes that the native
authoring model:

```json
{
  "protocol_version": 2,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "groups": {
    "POWER": { "origin": [1000, 1000], "title": "Power supply" },
    "MCU":   { "origin": [4000, 1000] }
  },
  "ops": [
    { "op": "place_component", "group": "POWER", "lib_id": "Regulator_Linear:AMS1117-3.3",
      "designator": "U1", "x_mil": 400, "y_mil": 300 },
    { "op": "place_decoupling", "group": "POWER", "anchor": "U1.VI",
      "offset_mil": [-300, 100], "power_net": "VIN", "designator": "C1" }
  ]
}
```

- **Group-local coordinates.** Any op may carry `"group": "<NAME>"`; its
  `[x, y]` coordinates are then relative to that group's `origin`
  (`absolute = local + origin`) — with ONE exception: `delete_object`'s
  `match.at` never translates, so a grouped delete must name world mils.
  Design each module around its own `(0, 0)`;
  **moving a whole module is a one-line origin edit** (on a NOT-yet-applied
  list — see the idempotency rules below for applied files). Pin anchors
  (`"REF.PIN"`, `"mid()"`, relative-placement `anchor`) are
  position-independent and never translate.
- **Macros inherit the tag.** A grouped macro's expansion stays group-local
  and every child op keeps the membership.
- **Membership persists in the sheet** as a hidden `Group` symbol property —
  the file itself is the module map. `akcli groups <sch>` lists every group
  with members and world bounding box; `akcli arrange <sch> --groups` (bare,
  no file) re-packs the modules from the properties alone.
- **Visual frames.** `akcli groups <sch> --frame --apply` draws one border
  rectangle + title per group (padded, grid-snapped outward). Frames carry a
  stable `key`, so after parts move a re-run **replaces** the stale border in
  place — never accumulates. `arrange --groups --frames` refreshes them right
  after packing.
- **Advisory lints.** `check --layout` warns when two groups' extents overlap
  (`LAYOUT_GROUP_OVERLAP`) and notes a frame that no longer contains its
  members (`LAYOUT_FRAME_STALE`).
- Errors: an op tagging an undeclared group fails `GROUP_UNKNOWN`; a declared
  group without an `origin` fails `GROUP_NO_ORIGIN` (both exit 6, before
  anything is written).

The full modular loop:

```
akcli new board.kicad_sch
akcli bbox Device:R --symbols mylib.kicad_sym     # reserve space per part
# author ops.json: declare groups -> place with group-local coords /
#   anchors / place_array -> route_net between modules
akcli plan board.kicad_sch --ops ops.json --render preview.svg
#   LOOK at preview.svg (grid overlay = world mils), then:
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets
akcli groups board.kicad_sch --frame --apply       # visual module borders
akcli check board.kicad_sch                        # incl. group layout lints
akcli arrange board.kicad_sch --groups --frames --apply   # re-pack later
```

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
- **Duplicate annotation keys are a lint error**: two `add_text` /
  `add_rectangle` / `add_text_box` ops of the same kind sharing a `key` seed
  ONE uuid, so the second would silently replace the first's node while both
  reported ok. The error names both op indices; the writer guards the same
  collision after macro expansion.
- **Enum slots are TypeError-safe**: a list/dict where an enum belongs
  (`op` name, `target_format`, `rotation`, mirror, `scope`, a
  sheet-pin `type`/`side`, …) yields a clean `OpError` naming the field, never
  an internal `TypeError` from an unhashable value.
- A crashing op handler surfaces as a per-op result with error code
  `INTERNAL` — never a traceback, and never a partial write.

## Execution pipeline (what happens to your ops)

The validator runs FIRST, over the ops exactly as you wrote them (so a
macro's own field typo is caught before any expander could drop it). Macros
then expand (each child inherits its macro's `group` tag), group-local
coordinates resolve to absolute, and the expanded list is validated again —
every error index mapped back to the op YOU wrote. A structurally bad group
reference therefore fails after per-op validation but still before anything
touches the target.
`plan`/`draw --render OUT.svg` additionally renders the would-be sheet from
the same temp dry-apply the net diff uses (grid overlay included), so you can
LOOK at the result before `--apply`.

1. `akcli plan <target> --ops ops.json` — validate + resolve, print what would
   change **plus the "Net changes" block** (the op-list is dry-applied to a
   temp copy and the before/after netlists diffed by pin membership). Never
   writes. `--no-net-diff` skips the diff.
2. `akcli draw <target> --ops ops.json` — **dry-run by default**: per-op
   results plus the connectivity verification plus the same net diff.
3. `... --apply` — atomic snapshot → temp → verify-on-temp → `os.replace`,
   with a rotated `<target>.bak` under `.akcli/backups/`. Any op error or
   connectivity ERROR
   (`DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `DUPLICATE_UUID`,
   `DUPLICATE_DESIGNATOR`, `UNRESOLVED_LIB_ID`, `INVALID_INSTANCES_PATH`,
   `POWER_PORT_STACKED`, `SHEET_FILE_MISSING`) refuses the write (exit 6).
   The same pass also reports `NO_CONNECT_CONFLICT` (an NC flag on a pin that
   also has a live connection) at WARNING — visible, but not on its own a
   refusal. It is the same set
   `akcli check <file> --integrity` runs read-only on any `.kicad_sch`, so an
   imported or hand-edited file can be screened before you author against it.
   Add `--strict-nets`
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

Created nodes get deterministic UUIDv5 keyed on the root sheet uuid plus a
per-node seed. **Replaying the same op-list file converges byte-identically
after one apply.** No seed contains the op's INDEX, so inserting, reordering or
deleting ops never re-identifies an untouched node — a re-apply after an
op-list edit rewrites nodes in place instead of duplicating everything
downstream of the edit. In a multi-sheet transaction each routed file keeps
its OWN uuid namespace (seeded from that file's own root uuid, not the
target's), so the same op-list replays byte-identically per file:

- **Parts are seeded from the designator alone** — `place_component`, and the
  power symbols behind `place_power_port`/`place_gnd`/`place_vcc`. An
  auto-allocated `#PWR0<n>` reference (the designator is not carried in the op)
  is recovered by the port's **position** — and its species, so a `PWR_FLAG`
  may share a real port's coordinate without either claiming the other's
  reference. Because the net name is *not* part of that identity, re-applying a
  list that renames a port's rail (`GND` → `+3V3` at the same point) rewrites
  the existing symbol; it does not stack a second port on top of the first.
- **Wires, junctions, no-connects, labels, bus entries, sheets and keyless
  graphics are seeded from a kind tag + their own content**: the segment's two
  endpoints, the annotation's coordinates, the label's net name + the anchor as
  WRITTEN (`"R1.2"` stays `"R1.2"`, a raw point stays its coordinates), the
  sheet's child file + name. Two coincident pins can therefore each carry their
  own label (`{"at": "R1.2"}` and `{"at": "R2.1"}` are different nodes even
  though the point is the same) — which is what keeps `move_component
  --carry_labels` honest.
- **Two ops that would seed ONE uuid are refused**, naming both op indices
  (exit 6): two identical wires, two texts on one point, two power ports on one
  coordinate (`POWER_PORT_STACKED`). The later op would otherwise replace the
  earlier one's node while both reported `ok`.
- Graphics/notes with a `key` (`add_rectangle`, `add_text`, `add_text_box`)
  are addressed by that key instead of their coordinates: the same key always
  replaces the same node, wherever it moves to. Two ops of the same kind
  sharing one `key` are rejected by the validator (they would seed one uuid, so
  the second would silently replace the first's node while both reported ok).
- **A power port that MOVES leaves its old node behind.** Several `GND` ports
  on one sheet is legal topology, so nothing can tell "the port moved" from "a
  second port was added": change the `at` of an already-applied
  `place_power_port` and you get two. Delete the old one explicitly
  (`delete_component` with the port's auto-allocated reference — e.g.
  `{"designator": "#PWR03"}`, found via `akcli component <sheet>`) or `akcli
  undo` and re-apply the edited list from the reverted file.
- **Group origins.** Editing a group's `origin` and re-running an
  ALREADY-applied list moves the member parts in place (designator seeds), but
  every label/wire/junction in the group is re-derived at the new coordinates
  and appended — the originals stay behind at the old coordinates as strays.
  Move an applied module with `move_component` (+`carry_labels`/`carry_wires`)
  in a delta list, or `akcli arrange --groups`, then refresh frames.
- Deletes are replay-safe no-ops when the target is already gone.

**Migration caveat (files written before the op-index-free seeds).** Older akcli
builds seeded nodes from `<content>:op_index` — parts up to 0.16, and wires /
junctions / labels / no-connects / bus entries / sheets / keyless graphics up to
0.17. Re-applying a list against a file written by one of those can append a
second node for anything whose original op index was ≠ 0, because the old node's
uuid no longer matches. Parts surface as `DUPLICATE_DESIGNATOR` (the write gate
refuses); the other nodes are net-neutral duplicates you can see with
`akcli check --layout` (stacked text/labels) or by diffing. Re-apply the
unedited list once, or roll back with `akcli undo`, before editing the list.

## Connectivity is name-based — and only dangling is a hard gate

- **Power ports and net labels merge by NAME**, everywhere (power ports/global
  labels cross sheets; local labels are sheet-local). A label or power port placed
  *directly on a pin's coordinate* connects that pin with **no wire** — handy for
  dense blocks where stubs would collide.
- **`add_net_label` and `place_power_port`/`place_gnd`/`place_vcc` accept
  `"at": "REF.PIN"`** — the anchor snaps to that pin's exact world coordinate,
  making label-on-pin first-class (no `akcli pins` arithmetic needed).
- **On-pin labels auto-orient.** When a label's anchor is a pin (via `"REF.PIN"`
  or a raw coordinate that hits a pin tip) and no `rotation` is given, the
  writer rotates the text AWAY from the symbol body and emits the matching
  `(justify ...)` — KiCad ignores a bare 180° angle, so without the justify the
  text renders straight over the part it names. An explicit `rotation` always
  wins. For a stub-end label, set `rotation` to the stub's direction
  (0=right, 90=up, 180=left, 270=down).
- **Verify the graphics too:** `akcli check <file> --layout` (also in the
  default check set for `.kicad_sch`) lints for symbol bodies drawn over each
  other, label text crossing a body or another label, and texts stacked on one
  anchor. Electrically perfect ≠ readable.
- **`power:PWR_FLAG` is special**: it marks a net as driven for ERC but does **not**
  name or merge a net. Use it to satisfy KiCad's `power_pin_not_driven` without
  side effects. (Do not give a *different* power symbol the value `PWR_FLAG`.)
- **A T needs a junction.** A wire end that stops on another wire's mid-span
  conducts in eeschema only through an explicit `(junction)` — `akcli nets` and
  the write gate both apply that rule, and the writer inserts the junction for
  you (`auto_junctions`) on every wire IT draws. In an imported or hand-drawn
  file a junction-less T therefore reads as `DANGLING_ENDPOINT`, which is the
  honest answer: KiCad calls those two nets too.
  The finding's message is enriched when deleting the wire would be the wrong
  fix: if the same wire carries a net label further along its span, or if its
  other end is genuinely connected, the message appends a
  `; note: …deleting the whole wire would…; trim the wire to its last
  connected point instead` suffix. Read that suffix before authoring a
  `delete_object` — the honest repair is usually a shorter `add_wire`, not a
  deletion.
- **Of the wire-GEOMETRY rules, the only hard write-gates are
  `DANGLING_ENDPOINT` and `DANGLING_BUS_ENTRY`** (the other ERROR-severity
  document findings listed under `--apply` above gate too, but they are not
  about wire shape). Non-orthogonal and overlapping/collinear wires are
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

1. **Create both files, child FIRST.** `akcli new child.kicad_sch` and
   `akcli new root.kicad_sch` — each gets a root uuid (the namespace for
   deterministic op uuids). `add_sheet`'s `file` must point at an EXISTING
   child: the op is refused with `SHEET_FILE_MISSING` otherwise, and the same
   code fires on any later write to a root whose child has gone missing. (It
   used to succeed, and then every `nets`/`check`/`render` on the design failed
   with exit 4 because the child could not be read.) The child needs to exist,
   not to be finished — a bare `akcli new` stub is enough to start wiring the
   parent.
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

Sheet uuids are deterministic (`sheet:<file>:<name>` /
`sheet:<file>:<name>.pin.<pinname>` keyed on the root, no op index), so replay
is byte-identical even after an op-list edit; the instances page
number is `op_index + 2` (root is page 1) and may be non-contiguous when
non-sheet ops interleave (KiCad tolerates it).

**A child sheet is a normal draw target** (and since 0.20 one op-list may also
edit several files at once — see [Multi-sheet op-lists](#multi-sheet-op-lists-sheets-map--per-op-sheet-020)
below). Point
`target_file` at the child and its ops apply there, even though its existing
symbols carry a `/<top-root>/<sheet>` parent-hierarchy instance path (the
connectivity gate accepts those). A part placed into a child JOINS its
siblings' sheet path(s) — including both entries when the child is
instantiated twice — because a child file cannot derive its parents' uuids on
its own. What is still unsupported is reaching into a
sub-sheet *from the parent's* op-list by naming its instance PATH — that fails
`HIERARCHICAL_UNSUPPORTED`. Route by the `sheets` envelope instead (below).

## Multi-sheet op-lists (`sheets` map + per-op `sheet`, 0.20)

One op-list can edit the root AND its children in **one atomic transaction**:

```json
{
  "protocol_version": 2,
  "target_format": "kicad",
  "sheets": { "amp": { "file": "amp.kicad_sch" } },
  "ops": [
    { "op": "add_sheet", "name": "Amp", "file": "amp.kicad_sch",
      "at": [5000, 2000], "size": [1000, 800],
      "pins": [{ "name": "OUT", "type": "output", "side": "left", "offset_mil": 200 }] },
    { "op": "place_component", "sheet": "amp", "lib_id": "Device:R",
      "designator": "R101", "x_mil": 2000, "y_mil": 2000, "value": "10k" },
    { "op": "add_net_label", "sheet": "amp", "at": "R101.1", "name": "OUT",
      "scope": "hierarchical" }
  ]
}
```

- `sheets` declares the child files (relative to the draw target's directory;
  each must already exist — `akcli new` it first, or `SHEET_UNKNOWN`). A
  per-op `"sheet": "<key>"` routes that op onto the declared file; no tag =
  the target itself. An undeclared tag is `SHEET_UNKNOWN` at validate time
  (with did-you-mean). A `sheet`-tagged macro expands with the tag riding
  onto every child op.
- Each routed file keeps its OWN uuid namespace (seeded from that file's root
  uuid), so a multi-sheet op-list replays byte-identically per file.
- **The write is a transaction**: every dirty file is staged (temp + fsync +
  verify) before the first `os.replace`; the pre-write bytes of every file
  land in `.akcli/backups/runs/<run_id>/` with a manifest; a partial commit
  rolls back automatically, and `akcli undo <target> --run <run_id> --apply`
  restores (or redoes) ALL files of the transaction together.
- The net diff evaluates the whole WOULD-BE tree, so the `add_sheet` +
  populate-the-child pattern above reports the real cross-sheet nets before
  anything is written.

**Sheet reuse (a child instantiated more than once)**: each instance is its
own annotation scope, so a scalar `designator` would write the SAME refdes
onto every instance — refused with `INSTANCE_REFS_REQUIRED` (the message
lists the instance paths). Pass per-instance references instead:

```json
{ "op": "place_component", "sheet": "stage", "lib_id": "Device:R",
  "designator": "R101",
  "instance_designators": { "/<root-uuid>/<sheet1-uuid>": "R101",
                             "/<root-uuid>/<sheet2-uuid>": "R201" } }
```

(`designator` must be one of the map's values — it seeds the symbol's uuid
and is the displayed Reference. `set_component_parameters` renames per
instance with the same field `instance_designators`; power ports need no
map — the writer auto-allocates one `#PWR0n` per instance.)

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

## The blessed connection ladder (pick the FIRST rung that fits)

The vocabulary offers several ways to express one connection; they are NOT
equally safe. Work down this ladder and stop at the first rung that fits —
every rung below it trades safety for control you usually don't need:

1. **Label-on-pin** (`add_net_label` anchored at `"REF.PIN"`) — the DEFAULT.
   Names the net at the pin tip itself: zero geometric-merge risk, survives
   `arrange` re-packs, and is exactly what every `place_*` macro does
   internally. Rails use its power-port form: `place_gnd` / `place_vcc` /
   `place_power_port` (one entry point per rail kind — these are the same
   pattern, not alternatives to weigh).
2. **`connect_and_label`** — ONLY for two pins facing each other on one axis
   (label-on-pin would collide the two texts): one coaxial wire + ONE label
   at `mid(from,to)`.
3. **`route_net`** — when a visible wire between non-coaxial pins is wanted:
   deterministic orthogonal route whose corner provably avoids pin tips;
   add `label` to name it. Between groups still prefer rung 1 — labels
   survive re-layout, long wires don't.
4. **Raw `add_wire`** — last resort, exact-geometry cases only. Highest risk
   rung: overlapping stubs, T-junction touches and corners on pin tips all
   silently merge nets (see the patterns below). If you reached this rung,
   say why in the op's `--note`.

`place_pwr_flag` is not a connection — it only marks a net driven for ERC,
mid-wire, never on a pin.

**Advanced layer — reach for these only when the task explicitly demands
them:** bus ops (`add_bus`/`add_bus_entry`, labeled-rip semantics),
hierarchical `add_sheet`, and the graphics ops
(`add_text`/`add_text_box`/`add_rectangle`, netlist-neutral by contract).
None of them participate in ordinary flat-sheet connectivity, and each has
its own section below.

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
  names or merges a net. A **rail port** is the opposite case: `place_gnd` /
  `place_vcc` / `place_power_port at "REF.PIN"` belongs ON the pin (rung 1
  above) and the lint stays quiet — it only fires there if the port's *body*
  actually lands on the part (a port rotated back over the symbol).
- **Vertical `place_array` + label-on-pin = label-over-neighbor collisions.**
  A `direction: down` array stacks parts at `pitch_mil` while a label on a
  side-facing pin auto-orients horizontally — on a tight pitch the text runs
  across the next part's body and `check --layout` fills with
  `LAYOUT_LABEL_OVER_SYMBOL`. Prefer `direction: right` (labels extend up/
  down into free space) for arrays whose every element gets a label-on-pin,
  or widen `pitch_mil` until the layout lint is quiet — the lint is the
  arbiter, not the eye.
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
{ "protocol_version": 2, "target_format": "kicad", "target_file": "board.kicad_sch",
  "ops": [
    { "op": "place_component", "lib_id": "Device:R", "designator": "R1",
      "x_mil": 2000, "y_mil": 1000, "value": "10k" },
    { "op": "place_gnd", "at": [2000, 1200] },
    { "op": "add_wire", "vertices": ["R1.2", [2000, 1200]] },
    { "op": "add_net_label", "name": "SENSE", "at": [2000, 700], "scope": "global" },
    { "op": "add_wire", "vertices": ["R1.1", [2000, 700]] }
  ] }
```

More patterns (LDO block, decoupling, connector fanout, multi-unit gates) live
in the `akcli-schematic-authoring` skill.
