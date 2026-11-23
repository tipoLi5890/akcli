# akcli — Hardened Implementation SPEC (v1.0, FROZEN-on-merge)

> Single source of truth for implementation agents. Resolves the original plan against 9 expert
> reviews. Every CRITICAL/HIGH fix is incorporated. Where reviewers disagreed, the decision and
> rationale are stated inline. Build each file from this document with **no further design decisions**.
>
> **Name cascade (LOCKED):** repo = `akcli`; PyPI dist = `akcli-kicad`; import package = `akcli`;
> CLI = `akcli`; plugin name = `akcli`; marketplace name = `akcli`.
> Python ≥ 3.11, zero **runtime** deps (stdlib only, incl. `tomllib`). Dev/test deps allowed via an extra.

---

## 0. The single most important correction (read first)

The container/record decoding is the easy part; **net inference is where correctness lives**. A naive
"just reuse an existing netlist parser" approach is wrong at the layer that matters, so this tool keeps the
low-level decoders (OLE2/CFBF container reader; Altium record framing + `|KEY=VALUE|` tokenizer) and
**rebuilds the net layer from scratch**. The net/record layer must get ALL of the following right — a
parser that keys on pin electrical type, or keeps only the first label per geometric cluster, silently
passes broken boards:

1. **Global same-name net merge.** Keeping only the *first* label per geometric cluster splits same-named
   `GND` ports and drops aliases (e.g. a `STAT`↔`LED1_GPIO_RD` short). → `netbuild.py`.
2. **Junctions (RECORD 29, 71) and T-junctions** (a wire endpoint on another wire's mid-span) — not just
   pins/labels but wire vertices and junction dots. → `netbuild.py`.
3. **No-ERC markers (RECORD 22).** Otherwise ERC re-flags every designer-blessed point.
4. **Pin electrical type** (Altium `Electrical` 0–7). ERC is dead without it.
5. **Component placement / orientation / mirror / part-id** (RECORD 1 `Location.X/Y`, `Orientation`,
   pin `OwnerPartId`) — needed for `place_component` and verify-by-re-export.
6. **`%UTF8%` twin-field decoding & `_Frac` coordinates.** Blanket-`latin1` mangles CJK/Ω/µ; integer-only
   coords drop the `_Frac` sub-unit → off-grid misses.
7. **Multi-storage OLE containers.** Keying streams by **bare name** collapses `.SchLib` (many `Data`) and
   `.PcbDoc` (many `Header`/`Data`) to one survivor; needs a path-qualified directory-tree walk.
8. **Safety.** `chain()`/`read_mini()` have no cycle detection or bounds (infinite-loop / OOM on hostile
   input); header fields (`ssz`, `ndifat`, `mini_cutoff`) are unvalidated allocation bombs.

**Therefore the parser is referred to throughout as a *starting point with a known net-naming defect*,
never "validated."** Milestone 2 ships a regression fixture reproducing the STAT/LED1 alias and asserts the
fix before any check is built on top.

---

## 1. Final architecture overview + LOCKED normalized data model

### 1.1 Layered architecture

```
                         ┌────────────────────────── CLI / Plugin surface ──────────────────────────┐
 bin/akcli (wrapper) ─▶  cli.py ─▶ subcommands: read net component check diff pinmap expected ops calc plan draw export jlc
                         skills/ + commands/ (Claude Code) call `akcli …`
                         └──────────────────────────────────────────────────────────────────────────┘
                                                     │
        ┌──────────────── format-agnostic core (the "analysis brain") ────────────────┐
        │  model.py (Schematic/Component/Pin/Net/Pcb/Library + PinType enum)           │
        │  netbuild.py  ◀─ NetPrimitives ─┐    checks/{erc,power,bom,diff,pinmap}.py    │
        │  opspec.py + ops/ (vocab + validator)  report.py   config.py   units.py      │
        │  errors.py (ERROR codes + exit codes)  safety.py (limits, safe_path, atomic)  │
        └──────────────────────────────────────────────────────────────────────────────┘
            ▲ primitives                         ▲ primitives                    │ op-list
   ┌────────┴─────────┐               ┌──────────┴──────────┐         ┌──────────┴──────────────┐
   │ Altium readers   │               │ KiCad readers       │         │ Executors               │
   │ _cfbf, records,  │               │ sexpr, kicad,       │         │ writers/kicad (xplat)   │
   │ sch, schlib, pcb │               │ kicad_lib           │         │ (the ONLY write path)   │
   │ (READ-ONLY)      │               │ (read + symbol defs)│         │ drivers/kicad_cli (verify)│
   └──────────────────┘               └─────────────────────┘         └─────────────────────────┘
```

**Principles (LOCKED):**
- **One normalized model.** All readers emit `Schematic`/`Pcb`/`Library`; all checks, ops, report, CLI are
  format-agnostic.
- **Readers emit `NetPrimitives` (wires, junctions, labels, power ports, pins).** A single shared
  `netbuild.py` turns primitives → `Net[]`, so Altium and KiCad share the exact same net-inference logic
  (the STAT fix is written once).
- **HW/FW raise only raw data; all derived logic is in this tool / SW.** (project rule.)
- **Altium is import/analysis-only, never written.** KiCad gets a full cross-platform writer; there
  is NO Altium write path (the Windows live-driver scaffold was deleted in 0.17).
- **Canonical coordinate system:** origin **top-left, +Y down**, unit **mils**, default **50-mil grid**
  (KiCad convention). Every reader normalizes *into* this system on ingest (the Altium reader negates Y).
- **All internal KiCad-writer geometry is integer nanometres** (`1 mil = 25_400 nm`, `1 mm = 1_000_000 nm`);
  convert to string only at serialize time.
- **Every emitted JSON object carries a version stamp** — `schema_version` for command payloads,
  or a family field where the document IS the format (`protocol_version` for op-lists,
  `proposals_version`, `facts_version`); `jlc search --json` is a bare array.

### 1.2 Coordinate-unit contract (LOCKED — resolves reviewer disagreement)

Reviewers split on the Altium SCH unit (one said "~10 mil/unit empirically", another "0.1 mil"). **Decision:
1 Altium schematic integer Location unit = 10 mil = 1/100 inch**, with a companion `*_Frac` field in
1/100000 of that unit. Evidence: a standard 200-mil pin has `PinLength=20` (200/20 = 10), and the sheet
extent ~1150 units ≈ 11.5 in. The "0.1 mil" figure is the *PCB* internal unit, not schematic. This constant
lives in `units.py` as `ALTIUM_SCH_MIL_PER_UNIT = 10.0` and **MUST be unit-tested** against a known pin pitch
before any coordinate is trusted. Conversion: `mil = (intval + frac/100000.0) * 10.0`.

`units.py` is the single source of truth for ALL conversions; verify-by-re-export uses **tolerance compare**
(not exact equality) to absorb mil↔mm float drift.

### 1.3 LOCKED normalized data model (`model.py`)

All dataclasses; `from __future__ import annotations`. `PinRef = tuple[str, str]` = `(designator, pin_number)`.

```python
SCHEMA_VERSION = "2.0"          # 1.1 Pcb tracks/vias; 1.2 pad-level nets; 1.3 finding evidence envelope; 2.0 eeschema-aligned canonical net names (see docs/migration-0.21.md)
                                # 1.0 -> 1.1: Pcb gained tracks/vias/arcs/pads (binary .PcbDoc copper)

class PinType(enum.Enum):       # canonical, format-agnostic
    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    TRI_STATE = "tri_state"
    PASSIVE = "passive"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    OPEN_COLLECTOR = "open_collector"
    OPEN_EMITTER = "open_emitter"
    NO_CONNECT = "no_connect"
    UNSPECIFIED = "unspecified"

# Mapping tables live in model.py as the single source of truth for both readers + ERC.
ALTIUM_ELECTRICAL = {           # Altium Pin.Electrical int -> PinType
    0: PinType.INPUT, 1: PinType.BIDIRECTIONAL, 2: PinType.OUTPUT,
    3: PinType.OPEN_COLLECTOR, 4: PinType.PASSIVE, 5: PinType.TRI_STATE,
    6: PinType.OPEN_EMITTER, 7: PinType.POWER_IN,
}
KICAD_PINTYPE = {               # KiCad pin-type token -> PinType
    "input": PinType.INPUT, "output": PinType.OUTPUT, "bidirectional": PinType.BIDIRECTIONAL,
    "tri_state": PinType.TRI_STATE, "passive": PinType.PASSIVE, "free": PinType.UNSPECIFIED,
    "unspecified": PinType.UNSPECIFIED, "power_in": PinType.POWER_IN, "power_out": PinType.POWER_OUT,
    "open_collector": PinType.OPEN_COLLECTOR, "open_emitter": PinType.OPEN_EMITTER,
    "no_connect": PinType.NO_CONNECT,
}

@dataclass
class Pin:
    number: str                 # pin number/designator, e.g. "2"
    name: str | None            # pin name, e.g. "P0.25"
    x_mil: float                # canonical: mils, origin top-left, +Y down (electrical endpoint/tip)
    y_mil: float
    electrical_type: PinType = PinType.UNSPECIFIED
    orientation: int = 0        # lib-frame degrees {0,90,180,270}: which way the pin points
    owner_part_id: int = 1      # multi-unit part (Altium OwnerPartId)
    unique_id: str | None = None

@dataclass
class Component:
    designator: str             # may be synthesized "$U<idx>" if missing (never dropped)
    library_ref: str | None     # symbol name / KiCad lib_id "Device:R"
    x_mil: float
    y_mil: float
    rotation: int = 0           # {0,90,180,270}
    mirror: str = "none"        # {none,x,y}
    value: str | None = None
    footprint: str | None = None
    dnp: bool = False           # KiCad `(dnp yes)` — placed but not populated
    in_bom: bool = True         # KiCad `(in_bom no)` inverts
    on_board: bool = True       # KiCad `(on_board no)` inverts
    unique_id: str | None = None
    part_count: int = 1
    sheet: str = ""             # source sheet path (provenance)
    parameters: dict[str, str] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    undesignated: bool = False  # True when designator was synthesized

@dataclass
class Net:
    name: str                   # canonical display name
    members: list[PinRef]       # SORTED stable (designator, pin_number) keys
    aliases: list[str] = field(default_factory=list)        # other explicit names on same net
    source_names: list[str] = field(default_factory=list)   # labels/ports that contributed
    is_named: bool = True
    confidence: float = 1.0     # 0..1; lowered on ambiguous merges
    merge_reasons: list[str] = field(default_factory=list)  # explainability per merge
    sheet_path: str = ""        # KiCad sheet-path PREFIX for the exported net name
                                # ("/", "/Amp/"); "" for global-scoped names (global
                                # label / power port) and unnamed nets. `name` stays bare.
    @property
    def stable_id(self) -> str: # hash of sorted membership — NEVER coordinate-derived
        return "net_" + hashlib.sha1("|".join(f"{d}.{p}" for d, p in self.members).encode()).hexdigest()[:12]

@dataclass
class Schematic:
    source_path: str
    source_format: str          # "altium" | "kicad"
    components: list[Component]
    nets: list[Net]
    sheets: list[str] = field(default_factory=list)
    no_erc_points: list[tuple[float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)   # passive_pin_ratio, frac_present, unnamed_net_count, ...
    title_block: dict[str, str] = field(default_factory=dict)  # Title/Revision/Date/... document parameters
    schema_version: str = SCHEMA_VERSION

# --- PCB is a SEPARATE sub-model (footprints/pads/nets, NOT symbol pins) ---
@dataclass
class Footprint:
    designator: str
    footprint_name: str | None
    layer: str | None
    rotation: float = 0.0
    value: str | None = None
    # additive (KiCad board reader; other readers keep the defaults — never a guess)
    at: tuple[float, float] | None = None      # origin, reader's board frame (KiCad: mm, +Y down)
    dnp: bool = False                          # (attr ... dnp)
    exclude_from_bom: bool = False             # exclude_from_bom / legacy `virtual`
    exclude_from_pos: bool = False             # exclude_from_pos_files / legacy `virtual`
    board_only: bool = False                   # KiCad "not in schematic"
    models: list[str] = field(default_factory=list)   # 3D model paths as written

@dataclass
class Pcb:
    source_path: str
    source_format: str
    nets: list[str]                          # net names only (v1 scope)
    footprints: list[Footprint]
    classes: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    tracks: list[dict] = field(default_factory=list)   # binary Tracks6 (mils, +Y up)
    vias: list[dict] = field(default_factory=list)     # binary Vias6
    arcs: list[dict] = field(default_factory=list)     # binary Arcs6
    pads: list[dict] = field(default_factory=list)     # binary Pads6
    schema_version: str = SCHEMA_VERSION

# --- Library model (symbol defs WITH pin electrical types) ---
@dataclass
class SymbolDef:
    name: str
    lib_id: str | None
    pins: list[Pin]
    part_count: int = 1
    extends: str | None = None
    body_sexpr: object | None = None         # KiCad: raw symbol node for writer lib_cache

@dataclass
class Library:
    source_path: str
    source_format: str
    symbols: list[SymbolDef]
    schema_version: str = SCHEMA_VERSION
```

**`NetPrimitives`** (the reader→netbuild interface, also in `model.py`):

```python
@dataclass
class WireSeg:    a: tuple[float, float]; b: tuple[float, float]; sheet: str = ""
@dataclass
class Junction:   at: tuple[float, float]; sheet: str = ""
@dataclass
class NetLabel:   at: tuple[float, float]; text: str; scope: str = "local"; sheet: str = ""  # local|global|power|port|sheet_entry
@dataclass
class PinHandle:  ref: PinRef; at: tuple[float, float]; sheet: str = ""
@dataclass
class NetPrimitives:
    wires: list[WireSeg]; junctions: list[Junction]
    labels: list[NetLabel]; pins: list[PinHandle]
    no_erc: list[tuple[float, float]]
    power_priority: bool = False             # PrjPcb PowerPortNamesTakePriority
    emit_single_pin_nets: bool = True        # PrjPcb NetlistSinglePinNets
```

---

## 2. LOCKED op-list vocabulary + JSON-schema sketch

### 2.1 Coordinate / unit contract for ops (LOCKED)
- Origin **top-left, +Y down**, units **mils**, default **50-mil grid** (pins/wire endpoints snap to grid).
- Rotation is an **enum `{0,90,180,270}`** (never free degrees — both tools quantize). `add_text` may use any angle.
- Mirror is an **enum `{none,x,y}`**. Transform order: **rotate, then mirror**. Rotation
  direction (established against kicad-cli over the full rot×mirror matrix, v0.4): a file
  angle of `+90` rotates **counter-clockwise on screen**, i.e. `(x,y) → (y,−x)` in the
  +Y-down frame.
- Wire geometry is a **structured array of `[x,y]` vertices** (even length, orthogonal segments). CSV-in-JSON
  is banned.
- Wire/port endpoints MAY be a **pin reference string `"REF.PIN"`** (e.g. `"U3.7"`); the executor snaps it to
  the pin's computed world coordinate. Raw `[x,y]` snaps to grid.
- Label/power-port anchors MAY additionally be **`"mid(REF.PIN,REF.PIN)"`** (v0.4): the midpoint
  of two exactly axis-aligned pins, snapped to grid along the wire axis (misaligned pins fail
  with `NON_ORTHOGONAL_WIRE`).

### 2.2 Op vocabulary (13 LOCKED ops + 3 additive v0.2 ops + 1 additive v0.4 op + 1 additive hierarchical op + 4 additive v0.10 ops)

The **Altium** column is informational only: it records which ops an Altium writer would have to
implement. Altium is import-only — there is no Altium write executor and none is planned
(`plan`/`draw` always build `target_format: "kicad"`).

| op | purpose | KiCad writer | Altium (informational) |
|---|---|---|---|
| `place_component` | place a symbol instance (optional `instance_designators`: per-instance references when the target sheet is instantiated more than once) | ✅ | ✅ |
| `set_component_transform` | rotate/mirror an existing component | ✅ | ✅ |
| `set_component_parameters` | set ref/value/footprint/params (`new_designator` renames; optional `instance_designators`: per-instance rename on a multiply-instantiated sheet) | ✅ | ✅ |
| `add_wire` | draw orthogonal wire (may emit many segments) | ✅ | ✅ |
| `add_junction` | explicit junction dot | ✅ | ✅ |
| `add_no_connect` | NC flag on a pin | ✅ | ✅ |
| `add_net_label` | net label (`scope: local\|global\|hierarchical`) | ✅ | ✅ |
| `place_power_port` | power symbol (sugar: `place_gnd`, `place_vcc`); ONE port per coordinate — a second port op on the same point is `POWER_PORT_STACKED` | ✅ | ✅ |
| `add_bus` | bus polyline | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_bus_entry` | bus entry (fixed 2.54 mm @ 45°) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_text` | free text/note | ✅ | ✅ |
| `delete_component` | remove ALL placed instances of a designator (wires left for the gate to flag; absent target = replay-safe no-op; optional `cascade: true` also removes attached wires/labels/no-connects plus junctions no longer needed by surviving wires) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `delete_object` | remove ONE top-level object by uuid, or via `match: {kind, name?, at?}` selector (exactly-one semantics) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `move_component` | move one instance (designator + optional `unit`); its properties travel along, wires are NOT stretched | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `rename_net` (v0.4) | rewrite matching label texts + power-port net Values (`scope` restricts kind; 0 matches = replay-safe note) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_sheet` | hierarchical child-sheet reference: `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, edge-computed sheet pins (`pins:[{name, type, side, offset_mil}]`); `at`=top-left mils; wires attach to a sheet pin BY COORDINATE (no `Sheet.Pin` endpoint); cross-sheet net = parent sheet-pin ↔ child same-name hierarchical label; the child `.kicad_sch` must ALREADY exist (`akcli new` it first) or the op is refused `SHEET_FILE_MISSING` — declare that child in the envelope's `sheets` map (§2.3) and the same op-list can fill it in the same transaction, OR set optional `create: true` (bool, default `false`) to have this op write a blank child stub in the same transaction instead (an existing child is never touched; the child's root uuid is content-seeded from the parent's root uuid + `Sheetfile`, so plan/apply/re-create agree — `identity.seed_sheet_file`). Additive, `protocol_version` unchanged | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `route_net` (v0.10) | deterministic orthogonal auto-route between two endpoints: coaxial → straight; else L (`style: auto\|hv\|vh\|z`) whose corner avoids every placed pin tip (a coincident corner would silently merge nets), z fallback; optional `label` names the net once at the longest segment's midpoint | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_rectangle` (v0.10) | top-level graphic border box (`start`/`end`, optional `stroke_width_mil`/`fill`/`key`); connectivity-neutral; the group-frame primitive | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_text_box` (v0.10) | bordered multi-line note box (`text`/`at`=top-left/`size`, optional `angle`/`key`); grammar fixture-verified against a real kicad-cli | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `set_title_block` (v0.10) | find-or-create edit of `(title_block)` fields (`title/date/rev/company/comment1..9`, ≥1 required); node kept after `paper`, before `lib_symbols`; unchanged values = replay-safe note | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |

**Relative placement (v0.10, additive).** `place_component`/`move_component` (and the
single-part macros `place_decoupling`/`place_pullup`) accept `anchor` (+`offset_mil`) as the
position instead of `x_mil`/`y_mil` (exactly one form per op): `"REF.PIN"` = that pin's world
tip, bare `"REF"` = the component origin, resolved at execution time against the live document
(the anchor may be placed earlier in the same list); `offset_mil` is world-frame, the result
grid-snaps.

**Stable annotation keys (v0.10).** `add_rectangle`/`add_text`/`add_text_box` accept `key`: the
uuid derives from the key alone (not coordinates, not op index), so re-emitting the same key
replaces the node in place — group frames (`group_frame:<name>`/`group_title:<name>`) refresh
instead of accumulating.

`place_component` additionally takes an optional `"unit": N` (multi-unit parts: each unit is
its own placed instance sharing the designator; `"REF.PIN"` resolves against the instance whose
unit owns the pin, and a pin on an unplaced unit fails loudly).

**Per-instance references (0.20, additive).** A sheet file instantiated more than once is
annotated per instance in KiCad, so one scalar designator would write the SAME refdes onto
every instance. `place_component` and
`set_component_parameters` both accept `instance_designators` — both
`{"<instance path>": "<REF>"}` with pairwise-distinct values; `designator` stays the scalar
uuid seed, so per-instance references never re-identify the node. A scalar reference on such
a symbol is refused with `INSTANCE_REFS_REQUIRED`, whose message lists the instance paths the
map has to cover. Power ports need no map — the writer allocates one `#PWR0n` per instance.

A per-executor **capability matrix** ships as `schemas/ops.capabilities.json`; an executor returns
`ERROR: OP_UNSUPPORTED` for any op it cannot map. `place_gnd`/`place_vcc` are documented sugar over
`place_power_port` with a preset `lib_id`.

**Bus netlist semantics (stage 2, kicad-cli-arbitrated).** The `netbuild` engine models buses as a
real layer, verified against `kicad-cli` netlist exports: a `(bus_entry)` conducts between its two
ends; an entry end attaches to a *wire* only at a wire endpoint or junction (a bare mid-span touch
floats, like a pin) but to a *bus* anywhere along a segment; the bus member a rip joins is selected
by the **wire-side label** (an unlabeled rip stays unconnected, a plain label placed on the bus
selects nothing); `NAME[a..b]` vector expansion is inclusive at both ends in either order; bus
clusters merge by name under label-scope rules (local = sheet-scoped, global merges member nets
across sheets, sheet-pin↔hierarchical-label ports stitch parent/child). `model.NetPrimitives`
gained `buses` and `bus_entries` (both default-empty, so the Altium reader is untouched).

### 2.3 op-list document shape

```json
{
  "protocol_version": 2,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "run_id": "uuid-or-stable-key",
  "sheets": { "amp": { "file": "amp.kicad_sch" } },
  "groups": { "POWER": { "origin": [1000, 1000], "title": "Power supply" } },
  "ops": [ { "op": "place_component", "sheet": "amp", "group": "POWER", "...": "..." } ]
}
```

**Multi-sheet routing (0.20, additive).** The optional `sheets` map declares the child files
this op-list may edit: `{NAME: {file: "<child>.kicad_sch"}}`, each resolved relative to the
draw target's directory and required to exist already — UNLESS an `add_sheet {"create": true}`
in the same op-list creates it (the transaction writes the blank child before any op runs).
Any op may carry `"sheet": "<NAME>"`
and is then applied to that child instead of the target (absent = the target itself; an
undeclared tag is `SHEET_UNKNOWN` with a did-you-mean; a tagged macro propagates the tag onto
every op it expands into). Both fields were purely additive — they did not move
`protocol_version` (which reached `2` with the 1.0 field renames, see
[docs/migration-0.21.md](migration-0.21.md)).
A routed run is ONE transaction (`writers/txn.py`, §3.5): every dirty file is staged and
connectivity-verified before the first replace, and a partial commit rolls back. `run_id`
names that transaction — it is recorded in the journal, echoed in the `draw --json` payload
alongside per-file `files[]`, and is the key `akcli undo --run` restores; it is generated when
the envelope omits it.

**Functional groups (v0.10, additive).** The optional `groups` map declares modules:
`{NAME: {origin: [x_mil, y_mil], title?, frame?}}`. Any op may carry `"group": "<NAME>"`; its
`[x, y]` coordinates are then GROUP-LOCAL and resolve to absolute (`local + origin`) in a pure
pre-pass after macro expansion and before validation (pin anchors never translate; macros
propagate the tag onto expanded children). Membership persists on placed symbols as a hidden
`Group` property — recoverable by `akcli groups`, `arrange --groups` (bare) and the layout
lints. Errors: `GROUP_UNKNOWN` (op tags an undeclared group), `GROUP_NO_ORIGIN` (declared group
without an origin), both exit 6.

### 2.4 Per-op result object (LOCKED)

```json
{ "op_index": 0, "op": "place_component", "status": "ok|error",
  "created_uuids": ["..."], "error_code": null, "message": "" }
```

### 2.5 `schemas/ops.schema.json` sketch (authoritative; hand-rolled validator, NOT jsonschema at runtime)

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://akcli/schemas/ops.schema.json",   // OWN namespace, no altium-mcp bytes
  "title": "AkcliOpList",
  "type": "object",
  "required": ["protocol_version", "target_format", "ops"],
  "properties": {
    "protocol_version": { "const": 2 },
    "target_format": { "enum": ["kicad", "altium"] },
    "target_file": { "type": "string" },
    "run_id": { "type": "string" },                  // transaction id: journalled, named by `undo --run`
    "sheets": {                                      // 0.20 multi-sheet routing map
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "properties": { "file": { "type": "string" } },
        "required": ["file"], "additionalProperties": false }
    },
    "groups": { "type": "object" },                  // v0.10 functional groups (§2.3)
    "ops": { "type": "array", "items": { "$ref": "#/$defs/op" } }
  },
  "$defs": {
    "point": { "type": "array", "items": { "type": "number" }, "minItems": 2, "maxItems": 2 },
    "endpoint": { "oneOf": [ { "$ref": "#/$defs/point" }, { "type": "string", "pattern": "^[^.]+\\.[^.]+$" } ] },
    "rotation": { "enum": [0, 90, 180, 270] },
    "mirror": { "enum": ["none", "x", "y"] },
    "op": {
      "type": "object",
      "required": ["op"],
      // Every branch declares `additionalProperties: false` and additionally accepts the two
      // universal tags: "group": "<NAME>" (§2.3 functional groups) and
      // "sheet": "<NAME>" (§2.3 multi-sheet routing).
      "oneOf": [
        { "properties": { "op": { "const": "place_component" },
            "lib_id": { "type": "string" }, "designator": { "type": "string" },
            "x_mil": { "type": "number" }, "y_mil": { "type": "number" },
            "rotation": { "$ref": "#/$defs/rotation" }, "mirror": { "$ref": "#/$defs/mirror" },
            "value": { "type": "string" }, "footprint": { "type": "string" },
            "symbol_source": { "type": "string" },
            "instance_designators": { "type": "object" } },
          "required": ["op", "lib_id", "designator", "x_mil", "y_mil"] },
        { "properties": { "op": { "const": "set_component_transform" },
            "designator": { "type": "string" },
            "rotation": { "$ref": "#/$defs/rotation" }, "mirror": { "$ref": "#/$defs/mirror" } },
          "required": ["op", "designator"] },
        { "properties": { "op": { "const": "set_component_parameters" },
            "designator": { "type": "string" }, "new_designator": { "type": "string" },
            "value": { "type": "string" }, "footprint": { "type": "string" },
            "parameters": { "type": "object" },
            "instance_designators": { "type": "object" } },
          "required": ["op", "designator"] },
        { "properties": { "op": { "const": "add_wire" },
            "vertices": { "type": "array", "items": { "$ref": "#/$defs/endpoint" }, "minItems": 2 } },
          "required": ["op", "vertices"] },
        { "properties": { "op": { "const": "add_junction" }, "at": { "$ref": "#/$defs/point" } },
          "required": ["op", "at"] },
        { "properties": { "op": { "const": "add_no_connect" }, "pin": { "type": "string" } },
          "required": ["op", "pin"] },
        { "properties": { "op": { "const": "add_net_label" }, "name": { "type": "string" },
            "at": { "$ref": "#/$defs/point" }, "scope": { "enum": ["local", "global", "hierarchical"] },
            "rotation": { "$ref": "#/$defs/rotation" } },
          "required": ["op", "name", "at"] },
        { "properties": { "op": { "const": "place_power_port" }, "lib_id": { "type": "string" },
            "net_name": { "type": "string" }, "at": { "$ref": "#/$defs/point" },
            "rotation": { "$ref": "#/$defs/rotation" } },
          "required": ["op", "lib_id", "net_name", "at"] },
        { "properties": { "op": { "const": "add_bus" },
            "vertices": { "type": "array", "items": { "$ref": "#/$defs/point" }, "minItems": 2 } },
          "required": ["op", "vertices"] },
        { "properties": { "op": { "const": "add_bus_entry" }, "at": { "$ref": "#/$defs/point" },
            "size": { "$ref": "#/$defs/point" } }, "required": ["op", "at"] },
        { "properties": { "op": { "const": "add_text" }, "text": { "type": "string" },
            "at": { "$ref": "#/$defs/point" }, "angle": { "type": "number" } },
          "required": ["op", "text", "at"] }
      ]
    }
  }
}
```

---

## 3. COMPLETE FILE MANIFEST

> Path is relative to repo root `../akcli/`. "Tested by" names the test file (every module
> has a dedicated test; fixtures under `tests/fixtures/`). All readers/writers map exceptions to
> `errors.py` codes — **a raw traceback never reaches the agent** (unless `--debug`).

### 3.1 Foundation (`src/akcli/` core — authored & FROZEN first)

| File | Purpose | Public API (signatures) | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `model.py` | LOCKED dataclasses + `PinType` enum + Altium/KiCad type maps + `NetPrimitives` | (all of §1.3) `to_json(obj)->dict`, `Schematic.export()->dict` | stdlib `dataclasses,enum,hashlib` | `Net.stable_id` = sha1 of sorted membership; `to_json` stamps `schema_version` | `test_model.py` |
| `opspec.py` | THE op vocabulary as data: one `OpSpec` per core/sugar/macro op (fields, kinds, required-ness, template placeholders, constraints, designator roles, capability row) + the `ENUMS` value vocabularies | `CORE`, `MACROS`, `ALL`, `UNIVERSAL_FIELDS`, `ROLE_PREFIXES`, `ENUMS`; `core_names()/sugar_names()/macro_names()/op_names()/by_name()`; `class OpSpec/Field/Constraint` | stdlib `dataclasses,typing` only (imports NOTHING from akcli) | The single source the JSON schemas (`tools/schemas.py build`), the `akcli.ops` tables and the capability matrix are all views of; `mypy --strict` clean | `test_ops_kit.py` |
| `ops/__init__.py` | package shim re-exporting the flat `akcli.ops.*` surface | `PROTOCOL_VERSION`, `OP_NAMES`, `MACRO_OPS`, `validate_oplist`, `prepare_oplist`, `PreparedOpList`, `load_oplist`, `op_template`, `expand_macros`, `resolve_groups`, `load_schema`, `load_capabilities`, `parse_mid_anchor`, `Op`, `OpError`, `GRID_MIL` | the sibling modules below | Strict DAG inside the package: `base`/`vocab` import no sibling; `validate` sits on top | `test_ops.py` |
| `ops/base.py` | op-list primitives | `PROTOCOL_VERSION:int`; `GRID_MIL`; `class OpError`, `class Op` typed accessors; `parse_mid_anchor`; `_is_point/_is_endpoint/_is_anchor/_is_anchor_ref/_is_pin_ref`; `_in` | stdlib only | Value SHAPE only — never which ops exist | `test_ops.py` |
| `ops/vocab.py` | every op/field/enum table, DERIVED from `opspec` at import | `OP_NAMES`, `MACRO_OPS`, `_OP_REQUIRED`, `_OP_FIELDS`, `_OP_OPTIONAL`, `MACRO_REQUIRED`, `MACRO_OPTIONAL`, `_MACRO_DESIGNATOR_ROLES`, `_ROLE_PREFIXES`, `_VALID_*`, `_MATCH_KINDS`, `_SHEET_PIN_*` | `opspec` | No hand-written vocabulary left: adding an op touches `opspec.py` and a handler/expander only | `test_ops_kit.py` |
| `ops/lints.py` | document-level lints shared by validator + expander | `_duplicate_placements`, `_duplicate_power_ports`, `_ANNOTATION_KEY_TAGS`, `_POWER_PORT_OPS` | `ops/base` | One definition of "duplicate" for both callers | `test_ops.py` |
| `ops/template.py` | `akcli ops template` skeletons | `op_template(name,*,include_optional=True)->dict` | `opspec`, `ops/vocab` | Placeholders are `Field.template` — declared per field per op | `test_ops_kit.py` |
| `ops/macros.py` | the 10 macro expanders + expansion pass | `expand_macros(doc,*,origins=None)->dict`; `_MACRO_EXPANDERS` | `errors`, `ops/base`, `ops/lints`, `ops/vocab` | Label-on-pin connectivity; macros never reach the schema or the executors | `test_macros2.py`, `test_macro_validation.py` |
| `ops/groups.py` | group-local → absolute coordinate resolution | `resolve_groups(doc)->dict`; `is_resolved(doc)->bool`; `_GROUP_XY_OPS/_GROUP_AT_OPS/_GROUP_VERTEX_OPS` (derived from `Field.coord`) | `opspec`, `errors`, `ops/base` | Pure vector add; the resolved document's TYPE (`_ResolvedDoc`, a `dict` subclass) makes a second call a no-op — never a marker KEY, which would leak into echoed payloads | `test_groups.py` |
| `ops/validate.py` | the zero-dependency structural validator | `validate_oplist(doc:dict)->list[OpError]`; `prepare_oplist(doc)->PreparedOpList` (frozen `doc`/`errors`/`groups_resolved`); `load_oplist(path)->dict` | `classify`, `errors`, `ops/base|vocab|lints|macros|groups` | Mirrors `ops.schema.json` (`jsonschema` only in dev) because both are views of `opspec`; rejects free angles, odd wire arrays, unknown ops with `ERROR` codes; `_RENAMED_IN_V2` turns a protocol-1 field spelling into a named rename instead of a did-you-mean guess | `test_ops.py`, `test_fuzz_oplist.py` |
| `ops/schema_io.py` | reads the published JSON schemas | `load_schema()->dict`; `load_capabilities()->dict` | stdlib `importlib.resources` | Packaged mirror first, repo-root `schemas/` fallback | `test_schema_exports.py` |
| `errors.py` | ERROR-code registry + exit-code table + `AkcliError`; top-level `as_error()` wrapper | `class AkcliError(Exception)`; `EXIT:dict[str,int]`; `ERROR_CODES:frozenset`; `def fail(code:str,msg:str)->NoReturn`; `def to_exit(exc)->int` | stdlib only | Codes: `ALTIUM_BAD_MAGIC, ALTIUM_FAT_CYCLE, ALTIUM_OOB_SECTOR, ALTIUM_BAD_SECTOR_SHIFT, ALTIUM_ALLOC_GUARD, ALTIUM_MALFORMED, ALTIUM_UNSUPPORTED, KICAD_SEXPR_DEPTH, KICAD_SEXPR_UNTERMINATED, KICAD_SEXPR_TOOBIG, KICAD_SEXPR_BAD_NUMBER, KICAD_BAD_ENCODING, INTERNAL, SYMBOL_NOT_FOUND, BAD_ANGLE, NON_ORTHOGONAL_WIRE, OFF_GRID, OVERLAP, VERIFY_FAILED, OP_UNSUPPORTED, HIERARCHICAL_UNSUPPORTED, DESIGN_UNSUPPORTED, PROTOCOL_MISMATCH, TARGET_LOCKED, PATH_OUTSIDE_ROOT, FILE_UNREADABLE, GROUP_UNKNOWN, GROUP_NO_ORIGIN, POWER_PORT_STACKED, SHEET_FILE_MISSING, PROJECT_ROOT_AMBIGUOUS, PROJECT_ROOT_MISMATCH, PROJECT_EXISTS, PROJECT_FILE_INVALID, PROJECT_FILE_MISSING, SHEET_UNKNOWN, INSTANCE_REFS_REQUIRED, TXN_ROLLBACK_FAILED, KICAD_CLI_TIMEOUT, KICAD_CLI_MISSING, NGSPICE_MISSING, NGSPICE_FAILED, NETWORK, SIM_NO_GROUND, SIM_NODE_COLLISION, SIM_NODE_COUNT_MISMATCH, SIM_VALUE_INVALID, SIM_OP_ASSERT_UNSUPPORTED, BINFETCH_DOWNLOAD, BINFETCH_CHECKSUM, BAD_CONFIG, BRIDGE_BUSY, BRIDGE_TIMEOUT` (the `BRIDGE_*` pair is reserved/unraised). `TARGET_LOCKED` (→ exit 6) is raised when a `~<name>.lck` GUI lock is present and `--allow-open` was not passed — by the KiCad writer, and by the `relink-symbols`/`library repair` apply paths (`safety.refuse_if_gui_locked`). Exit table: `docs/cli-reference.md` §"Exit codes" | `test_errors.py` |
| `safety.py` | hard limits + safe IO helpers used everywhere | `MAX_FILE_BYTES, MAX_SECTORS, MAX_RECORDS, MAX_DIR_ENTRIES, MAX_DECODED_BYTES, MAX_SEXPR_DEPTH, MAX_ATOM_BYTES, MAX_NODES`; `safe_path(base,cand)->Path`; `run_subprocess(argv,timeout,maxout)->CompletedProcess`; `atomic_write_with_backup(path,data,backup_dir)->None`; `refuse_if_gui_locked(paths,*,allow_open,what)->None` | stdlib `os,subprocess,resource,signal,pathlib,shutil` | `read_text_guarded`: the ONE UTF-8 text reader for KiCad documents — strict decode (`KICAD_BAD_ENCODING`), OSError family → `FILE_UNREADABLE`, size cap → `KICAD_SEXPR_TOOBIG`, so a corrupt file gets ONE answer across `read`/`arrange`/`groups`/`check`; `safe_path`: realpath both, reject escapes/symlinks, never expand env from untrusted files; `refuse_if_gui_locked`: the ONE `~<name>.lck` guard shared by `draw`/`arrange`/`undo`/`relink-symbols`/`library repair` apply paths → `TARGET_LOCKED`; `run_subprocess`: `shell=False`, abs exe, `--` before paths, timeout, output cap; `atomic_write`: snapshot→temp-in-same-dir→fsync→`os.replace` | `test_safety.py`, `test_fuzz_safety.py` |
| `units.py` | all coordinate conversions + grid + tolerance | `ALTIUM_SCH_MIL_PER_UNIT=10.0`; `MIL_PER_MM=1/0.0254`; `NM_PER_MIL=25400`; `NM_PER_MM=1_000_000`; `altium_to_mil(i,frac)->float`; `mil_to_nm(m)->int`; `nm_to_mm_str(nm)->str`; `snap_mil(m,grid=50)->float`; `approx_eq(a,b,tol_nm)->bool` | stdlib | `nm_to_mm_str` strips trailing zeros/dot (KiCad float style); integer-nm math only | `test_units.py` |
| `classify.py` | shared classification vocabulary: refdes prefixes, rail names, part roles | `refdes_prefix(ref)->str`; `norm/implied_voltage/rail_matches`; `is_power/is_ground/power_root/is_stage_name(name)`; `is_tvs/is_crystal/is_connector(designator,lib_id,…)`; `POWER_NAMES, GROUND_NAMES, SIGNAL_SUFFIXES, STAGE_SUFFIXES, TVS_KEYWORDS, CONNECTOR_PREFIXES, CRYSTAL_PREFIXES, PASSIVE_TIE_PREFIXES` | stdlib only (LEAF — so `checks` and `review` can both depend on it with no cycle) | ONE implementation of the questions `checks/erc.py`, `checks/power.py`, `review/topo.py`, the review detectors and `sim/models.py` used to answer with six diverged copies: prefix strips a synthesized `$U3`; rail names match derived forms (`VBUS_FUSED`) but not signals about a rail (`VBUS_DET`); part roles take library/value evidence over the designator prefix (so `X1` is a crystal, not a connector) | `test_classify.py` |
| `project.py` | sheet-tree topology: root discovery + tree staging for the hierarchy-aware net gate (0.20) | `resolve_root(target, explicit=None)->Path`; `tree_files(root)->list[Path]`; `stage_tree(files, base)->dict[Path,Path]` | `errors`, `readers.kicad` (`sheet_refs`), `readers.kicad_pro` | Reverse index (who's `Sheetfile` points at me) over the project directory — nearest `.kicad_pro` ancestor, bounded climb, shallow ancestor globs; a file nobody references is its own root (flat short-circuit); several claiming roots try the `.kicad_pro` tie-break in strongest-evidence order — a single project file's `sheets[0][0]` uuid matched against each candidate root's own `(uuid ...)` first (identity), the project's stem against the candidates' filenames second (convention, KiCad's own naming rule but satisfiable by accident on a renamed tree) — and only refuse `PROJECT_ROOT_AMBIGUOUS` once both fail; a malformed/unreadable `.kicad_pro` degrades silently (`readers.kicad_pro` never raises) straight to the stem rule; `--root` wins but is forward-validated (`PROJECT_ROOT_MISMATCH`). Staging preserves the relative layout under a common-ancestor anchor so `Sheetfile` paths resolve inside the shadow tree | `test_project.py` |
| `readers/kicad_pro.py` | `.kicad_pro` reader — degrade-only, never raises | `class KicadPro(path,ok,error,meta_filename,version,sheets:list[tuple[uuid,name]],text_variables,net_classes,pinned_symbol_libs,pinned_footprint_libs,design_rules:dict[str,float])` (`design_rules` = the numeric entries of `board.design_settings.rules`, mm, sorted; `{}` when the project states none); `read(path)->KicadPro`; `root_sheet_uuid(pro)->str\|None`; `find(project_dir_or_file)->Path\|None` | `safety` (size cap only) | Every failure mode (missing, oversized, undecodable, invalid JSON, not an object) yields `ok=False`+`error`, never an exception — `resolve_root`'s tie-break and `project check`'s `PROJECT_FILE_MALFORMED` finding both depend on that; unknown top-level keys are ignored rather than rejected (KiCad accumulates per-version sections this module neither reads nor writes); `find` matches `libtable.discover`'s directory-vs-file rule | `test_kicad_pro.py` |
| `scaffold.py` | project scaffolding engine: which KiCad files (`.kicad_pro`/root `.kicad_sch`/child sheets/`.kicad_pcb`/lib tables/`.gitignore`) a directory is missing and how to land the whole set atomically — the engine behind `akcli project new`/`project init` | `plan(directory,*,mode,name=None,paper="A4",title=None,force=False,no_pcb=False,no_lib_tables=False,no_gitignore=False,sheets=(),root=None,example=None)->Plan` (pure); `apply(p,*,backup_depth=...)->None`; `class Plan/PlannedFile`; `record(p,status,*,dry_run=False)->None`; `sheet_names(raw)->list[str]` | `journal`, `project`, `safety`, `errors`, `examples`, `writers.atomic`, `writers.blank` | `plan` reads the directory (and, for `init`, the existing root schematic's uuid) and decides each file's fate (`created`/`kept`/`overwritten`/`conflict`) without touching disk — `--dry-run` is that same call, stopped before `apply`; `apply` preflights the WHOLE set (any `conflict` → `PROJECT_EXISTS` before `mkdir`), stages every write through `writers.atomic.prepare`, then promotes in commit order (`pro`→`sch`→`sheet`→`pcb`→`sym-lib-table`→`fp-lib-table`→`gitignore`), and rolls a partial commit back (unlink new `created` files, restore `overwritten` ones from the backup `commit` just took) on any failure; an existing `.gitignore` (and, under `init`, an existing `.kicad_pro`) is never overwritten, not even with `--force` | `test_scaffold.py` |
| `commands/project.py` | `akcli project new`/`init`/`info`/`check` CLI surface: two write verbs over `scaffold.py`, two read verbs (`check_findings`) over `readers.kicad_pro` | `register(sub,common)`; `check_findings(pro_path,*,with_lib_audit=True)->list[Finding]` (shared with the `release preflight` `project` gate) | `scaffold`, `writers.blank` (paper validation), `readers.kicad_pro`, `readers.kicad`, `libtable`, `errors` | Two argument sets over one write engine: `new` refuses an existing project (`PROJECT_EXISTS`, exit 2) unless `--force` (`--example NAME` additionally drops the packaged `akcli.examples` op-list as `DIR/ops.json`); `init` is purely additive and idempotent (no `--force`), reporting `status: unchanged` on a second run; `--dry-run` always exits 0; text output is one `created`/`kept`/`OVERWROTE`/`CONFLICT` line per file plus a `status:` summary, `--json` is `_stamp`ed and carries the same `Plan.to_json()` shape both verbs share. `info` is read-only (root-sheet binding, sheet tree, boards, lib tables, net classes, text variables, pinned libs; degrades to `ok:false` on a malformed `.kicad_pro` rather than raising); `check` runs `check_findings` through the shared `--fail-on`/`--exit-zero` lint exit policy — `PROJECT_FILE_MALFORMED`(WARNING), `PROJECT_META_FILENAME_STALE`(NOTE), `PROJECT_ROOT_SHEET_MISSING`/`PROJECT_ROOT_SHEET_MISMATCH`(ERROR), reused `SHEET_FILE_MISSING`(ERROR), `PROJECT_PCB_MISSING`(NOTE), `libtable.audit`'s own findings, `PROJECT_STATE_UNIGNORED`(NOTE) — calibrated so a freshly scaffolded project reports NOTE-at-most and exits 0 | `test_project_cmd.py` |
| `examples/__init__.py` | packaged mirror of `examples/circuits/*/ops.json` (`akcli.examples`), the shipping copy `project new --example` reads from an installed wheel | `names()->list[str]`; `ops_path(name)->Path\|None`; `ops_text(name)->str` | none (stdlib `pathlib` only) | Roster comes from what is actually packaged (`iterdir` over the directory), never a hand-kept constant, so a wheel built without the package data offers nothing rather than a name it cannot deliver; `tools/examples_mirror.py build` populates this directory from the canonical `examples/circuits/` corpus, `check` is the CI drift gate (also asserted by `tests/test_examples.py`) — the same mirror+check shape `tools/schemas.py` uses for `akcli.schemas` | `test_examples.py` |
| `config.py` | discover + parse + validate `akcli.toml` | `find_config(start:Path)->Path\|None`; `load_config(path)->Config`; `class Config` (`mcu_designator, rails:list, paths:dict, erc_waivers:list`) | `tomllib`, `errors`, `safety` | walk-up discovery from cwd; `--config` override; paths resolve relative to toml dir; reject unknown keys → `BAD_CONFIG`; schema in §3.11 | `test_config.py` |
| `report.py` | render findings + metadata caveats; text + `--json` | `render(findings,fmt,meta)->str`; `class Finding(code,severity,message,refs)`; `Severity` enum | `model`, `errors` | Always prints metadata header: passive-pin ratio, No-ERC suppressed count, unnamed-net count, frac-coord presence — so a vacuous pass is never read as clean | `test_report.py` |
| `__main__.py` | `python -m` entry | `from .cli import main; raise SystemExit(main())` | `cli` | thin shim only | covered by `test_cli.py` |
| `cli.py` | argparse dispatch, exit codes, global flags | `def main(argv=None)->int`; one subcommand family per `commands/*.py` module (live list: `akcli capabilities --section commands`) | everything | global `--version` (pkg + protocol), `-C/--config`, `-v/-vv/--quiet`, `--json`, `--no-color`, `--debug`; `draw` defaults `--dry-run`, needs `--apply` to write; stdout = data, stderr = logs | `test_cli.py` |

### 3.2 Altium readers (`src/akcli/readers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `_cfbf.py` | **hardened** OLE2/CFBF reader; path-qualified directory tree walk | `read_streams(path_or_bytes)->dict[str,bytes]`; `read_streams_qualified(...)->dict[str,bytes]` (e.g. `"Components6/Data"`) | `struct`, `safety`, `errors` | **Hardened** OLE2/CFBF parsing **with**: `len>=512` guard; assert `ssz_shift∈{9,12}`, `msz_shift==6`, `mini_cutoff==4096`; cycle-detected `chain/read_mini` (seen-set + `MAX_SECTORS` cap) → `ALTIUM_FAT_CYCLE`; range-check every `off(s)` → `ALTIUM_OOB_SECTOR`; **walk red-black dir tree (Child/Left/Right) for path-qualified, non-colliding names** (fixes 41× `Data`/51× `Header`); guard missing root-storage; DIFAT-spillover (>109) walked under declared-count + cycle + sector-cap guards | `test_cfbf.py`, `test_fuzz_safety.py` |
| `altium_records.py` | record framing + field tokenizer + `%UTF8%` + `_Frac` + electrical map + RECORD-ID constants | `parse_records(buf,drop_header:bool)->list[dict]`; `fields(r)->dict`; `gi(d,k,default)`; `coord(d,key)->float` (assembles int+frac→mil); `RECORDS:dict` | `units`, `model`, `safety`, `errors` | Port of lines 109–130 **with**: `%UTF8%`-prefixed keys decoded UTF-8 (re-encode latin1→bytes→utf-8); `_Frac` companion assembled via `units.altium_to_mil`; `Electrical`→`PinType`; **header detection** (drop leading record only if it's the schematic HEADER, not unconditional `[1:]`) to fix SchLib/PcbDoc OwnerIndex base; record/byte caps → `MAX_RECORDS`. RECORD IDs: 1=Component,2=Pin,6=Polyline,15=SheetSymbol,16=SheetEntry,17=PowerPort,18=Port,22=NoERC,25=NetLabel,27=Wire,29=Junction,34=Designator,41=Parameter,44/45/46/47/48=Implementation | `test_altium_records.py` |
| `altium_sch.py` | `.SchDoc` → `Schematic` (READ-ONLY) | `read(path)->Schematic`; `read_primitives(path)->NetPrimitives` | `_cfbf`, `altium_records`, `model`, `netbuild`, `units` | Extract: RECORD 1 placement/orientation/mirror/UniqueID; RECORD 2 pins with `Electrical`, `OwnerPartId`, tip = `Location + PinLength*dir` (negate Y into canonical); RECORD 34 designators (synthesize `$U<idx>` when missing — never drop); RECORD 41 params (value/comment, where `Comment="=<Param>"` is an indirection to that component's own parameter; an ownerless 41 is a document parameter → `Schematic.title_block`); RECORD 45/46 footprint (`ModelType` PCBLIB/absent only, `IsCurrent=T` preferred — a SIM/SI/3D model name must never land in `footprint`); RECORD 27 wires, 29 junctions, 22 No-ERC, 25/17 labels+power ports → emit `NetPrimitives` → `netbuild.build_nets()` | `test_altium_sch.py` |
| `altium_schlib.py` | `.SchLib` → `Library` | `read(path)->Library` | `_cfbf`, `altium_records`, `model` | Use `read_streams_qualified` — each symbol in its own storage `Data` stream; per-stream OwnerIndex base (no blind `[1:]`); refuse binary records (4th length byte ≠ 0) loudly | `test_altium_schlib.py` |
| `altium_pcb.py` | `.PcbDoc` → `Pcb` (ASCII sections + binary copper) | `read(path)->Pcb` | `_cfbf`, `altium_records`, `altium_pcb_bin`, `model` | ASCII `|KEY=VAL|` sections `Nets6`/`Components6`/`Classes6`/`Rules6` plus binary `Tracks6`/`Vias6`/`Arcs6`/`Pads6` via `altium_pcb_bin`. **Guard:** still refuse to ASCII-parse a Header-declared binary section; `Fills6/Regions6/Texts6/Polygons6` remain deferred | `test_altium_pcb.py` |
| `altium_pcb_bin.py` | binary `.PcbDoc` section decoders | `parse_tracks/parse_vias/parse_arcs/parse_pads(buf,nets)->list[dict]`; `UNIT_MIL` | `errors` | Framing `u8 type + u32 LE len + payload`; coords 1/10000 mil → emit mils, native +Y-up frame; nets by index into `Nets6` order (0xFFFF→None). Track: layer u8@0, net u16@3, comp u16@7, x1/y1/x2/y2 s32@13.., w s32@29. Via: x/y@13/17, dia@21, hole@25, layers u8@29/30. Arc: center@13/17, r@21, angles f64@25/33 (deg), w@41. Pad: block-structured (name pascal block + 3 reserved + geometry ≥61B: size@21/25, hole@45, shape u8@49, rot f64@52). Unknown type→`ALTIUM_UNSUPPORTED`, truncation→`ALTIUM_MALFORMED`. Layouts cross-validated item-by-item vs KiCad's pcbnew importer on real boards (778/778 tracks, 20/20 vias, 236/236 arcs, 48/48 pads) | `test_altium_pcb_bin.py` |

### 3.3 Net inference (`src/akcli/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `netbuild.py` | format-agnostic net inference (the STAT fix) | `build_nets(prims:NetPrimitives)->list[Net]` | `model`, `units` | **Pipeline (LOCKED):** (1) exact-integer geometric union-find on wire segments (coords in fine-int from `_Frac`); (2) union each **junction(29)** point onto every segment it lies on; (3) **T-junction**: union every wire vertex lying on another wire's mid-span; (4) union pins/labels lying on a segment (`on_seg`, exact integer cross-product) — **pins connect at segment endpoints or junction-marked points only** (a bare mid-span touch does not connect: eeschema's rule, and Altium's editor inserts a junction record for every pin tap); labels connect anywhere along the wire; (5) **GLOBAL same-name merge:** group all label/power-port names per component, `name→set(roots)`, union every pair sharing any name — *this stitches the two STAT clusters and aliases STAT≡LED1_GPIO_RD*; (6) naming priority: KiCad sources follow eeschema's driver ladder (GLOBAL > POWER_PIN > LOCAL_LABEL > HIER_LABEL > SHEET_PIN > auto, alphabetical tie-break); Altium sources keep power-port vs net-label ordered by `power_priority`; keep all names as `aliases`, emit confidence<1 + a NOTE on multi-name nets; (7) **stable synthetic ids from sorted membership**, never `N$x_y`; (8) record `merge_reasons` for explainability; (9) multi-sheet: union by Port/SheetEntry name (net labels are sheet-local, power ports global) | `test_netbuild.py` (+ STAT/alias, junction, T-junction, NoERC, two-same-name-GND fixtures) |

### 3.4 KiCad readers (`src/akcli/readers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `sexpr/parser.py` | iterative, depth/size-capped S-expr tokenizer+parser (shared by readers + writer) | `parse(text:str)->SNode`; `dumps(node:SNode)->str`; `class SNode` (preserves untouched atom text + child order) | `safety`, `errors` | **Explicit-stack** (no recursion; `setrecursionlimit` banned); enforce `MAX_SEXPR_DEPTH/MAX_ATOM_BYTES/MAX_NODES`; bounded quote/escape scan; unterminated/over-deep → `KICAD_SEXPR_*`. `SNode` keeps original token text so untouched nodes reserialize byte-identical | `test_sexpr.py`, `test_fuzz_safety.py` |
| `kicad.py` | `.kicad_sch`/`.kicad_pcb` → `Schematic`/`Pcb` | `read_sch(path)->Schematic`; `read_pcb(path)->Pcb`; `read_primitives(path)->NetPrimitives` | `sexpr`, `kicad_lib`, `model`, `netbuild`, `units` | Resolve **pin electrical types from `lib_symbols` at read time** (KiCad pins on instances carry no type); parse `(wire)`,`(junction)`,`(no_connect)`,`(label/global_label/hierarchical_label)`,`(symbol …)` with `(instances)` refdes; **recurse into `(sheet …)` children** (per-instance namespaces; designators from the matching instances path; sheet-pin↔hier-label pairs via synthetic never-naming "hier" connectors; cycle/depth-guarded); coords mm→mil (no Y flip; KiCad already +Y down); → `NetPrimitives` → `netbuild` | `test_kicad_reader.py` |
| `altium_prj.py` | `.PrjPcb` project → top-sheet `Schematic` | `read(path)->Schematic`; `read_project(path)->dict` | `altium_sch`, `errors` | INI-ish parse (configparser, strict=False); document list (backslash paths resolved), `PowerPortNamesTakePriority` → `NetPrimitives.power_priority`; top sheet = the schematic no sheet symbol references | `test_altium_hierarchy.py` |
| `kicad_lib.py` | `.kicad_sym` + inline `lib_symbols` → `Library` | `read(path)->Library`; `resolve(lib_id, sources)->SymbolDef`; `pin_offsets(sym)->list` | `sexpr`, `model` | Resolve `(extends ...)` (load base); keep `body_sexpr` for writer's lib_cache; pin offsets in symbol-local coords for world-coord computation | `test_kicad_lib.py` |

### 3.5 KiCad writer (`src/akcli/writers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `geometry/transform.py` | coord/transform core (the module that makes wires hit pins; package also holds `boxes.py` box primitives, `segments.py` integer segment/union-find and `outline.py` — board-outline assembly: `assemble(loose,shapes)->Outline(closed,area,bbox,loops,…)` chains line/arc `Segment`s into loops within `CHAIN_TOLERANCE_MM` and measures them exactly, cutouts subtracted; pure floats, used by `readers/kicad.py` for `board["outline"]`) | `pin_world(sym,inst,pin)->tuple[int,int]` (nm); `transform_point(pt,rot,mirror,origin)->...`; `grid_snap_nm(pt,grid)->...`; `mil_to_nm/nm_to_mm_str` re-exports | `units`, `model` | Integer-nm; rotation 0/90/180/270 + mirror (x/y) matrices, **rotate-then-mirror**, Y-down; transform child `(property)` positions/angles too | `test_geometry.py` |
| `sexpr/writer.py` | atom-construction helpers (quoting + nm float rendering) | `quote(value:str)->str` (alias of `kicad_escape.quote_atom`); `atom_for_token/atom_for_value/atom_for_nm(...)->SNode` | `sexpr`, `units`, `kicad_escape` | The ONE serializer is `sexpr.dumps` (parser module); this module only builds atoms. String quoting/escaping delegates to `kicad_escape.quote_atom` (single implementation, every historical alias pinned `is`-identical by `test_sexpr_quoting.py`). **Gate:** byte-identical no-op round-trip via `dumps` | `test_roundtrip_byte_identity.py` |
| `txn.py` | multi-file op-list transaction (0.20): envelope `sheets` map + per-op `sheet` tags route ops across the target's child files | `apply_project(oplist,target,apply,*,sources,file_map,verify_out,allow_open,run_id)->(results,report)`; `resolve_sheet_files(oplist,target)->(dict[str,Path], dict[Path,PendingChild])`; `sheet_routed(oplist)->bool`; `class PendingChild(path,parent,file_name)` | `atomic`, `blank`, `connectivity`, `identity`, `instances`, `kicad` (`run_ops`/`envelope_gate`), `journal`, `safety` | Per-file doc/ctx (each file seeds uuids from its OWN root uuid), per-op routing through the shared `run_ops` loop, every dirty file verified then STAGED (`atomic.prepare`) before the first `os.replace`; pre-write bytes + manifest under `.akcli/backups/runs/<run_id>/` (journal-independent recovery unit); partial commit auto-rolls-back (`VERIFY_FAILED`), rollback failure is loud (`TXN_ROLLBACK_FAILED` + `undo --run`); `file_map` = shadow-tree dry-apply (no locks/backups/journal). **`add_sheet {"create": true}` (0.22)** makes `sheet_routed` true even for a single-file op-list and materialises the blank child HERE, parents first, BEFORE any op runs — on disk under `--apply`, into the staged copy under `file_map`, in memory only for a plan (whose `SHEET_FILE_MISSING` is then suppressed); the per-file report gains `created`, and a rollback restores CONTENT and never unlinks a created child | `test_txn.py`, `test_add_sheet_create.py` |
| `atomic.py` | atomic-write primitives shared by the executor and the project transaction | `prepare(p,text,snap_sha,verify)->PreparedWrite`; `commit(pw,backup_dir,backup_depth)`; `abort(pw)`; `atomic_write(...)`; `backup_name(name,level)`; `rotate_backups(bd,name,depth)`; `check_snapshot(p,snap_sha)` | `errors` (stdlib otherwise) | The historical single-shot atomic write decomposed into prepare/commit/abort so a multi-file caller can stage EVERY temp (written+fsync'd+verified) before the first `os.replace` — two-phase-commit shape; binary write (`os.fdopen(fd,"wb")`), snapshot-guarded optimistic lock, rotated `.bak` stack with crash-safe pending→rotate→promote | `test_ops.py` / `test_op_idempotency_census.py` (via `apply`) |
| `blank.py` | blank-document bootstrap: every byte of a fresh KiCad project's non-schematic files, plus the blank `.kicad_sch` `akcli new` also uses | `blank_sch(paper,title,*,uuid=None)->str`; `blank_pro(project_filename,root_sch_uuid)->str`; `blank_pcb(paper="A4")->str`; `blank_lib_table(kind)->str`; `kicad_gitignore()->str`; `KICAD_PAPERS` | `sexpr.build` (quoting only) | Every template transcribed verbatim from a real KiCad 10 install (fixture-first: `tests/fixtures/kicad/blank_project/`); `.kicad_pro` renders `json.dumps(...,sort_keys=True)` to match KiCad's own byte layout; the board is a **container, not a layout** (zero footprints/tracks/outline) written in the KiCad 10 (`20241229`) board-file format — KiCad 8 cannot open it, the schematic stays KiCad 8 format and opens fine | `test_scaffold.py` |
| `lib_cache.py` | `lib_symbols` cache resolution & copy | `ensure_cached(doc,lib_id,sources)->None`; `SYMBOL_NOT_FOUND` on miss | `kicad_lib`, `sexpr`, `errors`, `config` | Requalify parent → `Nick:Name`, keep child unit names unqualified (`Name_0_1`); **flatten** an `(extends)`-derived symbol into one standalone entry (base inlined under the derived name, units renamed, derived properties overlaid, `extends` dropped — KiCad's own save behavior; its loader will not resolve a bare `(extends "Base")` against a qualified cached base); **dedup by lib_id**; copy full pin electrical types (ERC needs them); symbol source = config `.kicad_sym` paths and/or template `.kicad_sch` | `test_lib_cache.py` (incl. `C_Polarized` extends fixture) |
| `instances.py` | refdes/instances/sheet-path + `#PWR` allocation | `write_instance(doc,sym,ref:str\|Mapping[str,str],path)->None`; `symbol_references(sym)->dict[str,str]`; `primary_reference(sym)->str\|None`; `alloc_pwr_ref(doc)->str`; `alloc_pwr_refs(doc,count)->list[str]`; `instances_path(doc,sheet)->str`; `sibling_paths(doc,exclude)->list[str]` | `sexpr`, `errors` | Write BOTH `(property "Reference")` AND `(instances (project … (path … (reference …)(unit …))+))`, in sync; derive project name + root-sheet uuid; `instances_path` writes onto the target file's OWN root sheet only — an op naming a sub-sheet path → `HIERARCHICAL_UNSUPPORTED` (a child `.kicad_sch` is instead authored as its own draw target; the connectivity gate accepts its parent-hierarchy path). In a CHILD file the flat path is not the truth: when the symbols already there carry multi-segment hierarchy paths, a newly placed one JOINS that exact set (one `(path …)` entry per instantiation) — the single-segment form made kicad-cli report annotation errors. **Per-path references (0.20):** `ref` may be a `{instance path: reference}` mapping (values pairwise-distinct, insertion order preserved, the first pair shown in the `Reference` property); a scalar `ref` on a symbol sitting on several instance paths is refused `INSTANCE_REFS_REQUIRED` listing those paths, and `symbol_references`/`primary_reference` read the per-path set back so a lookup by refdes matches ANY instance's reference; `#PWR0xx` unique alloc (`alloc_pwr_refs` hands out one per instance); **deterministic UUIDv5** = `uuid5(sheet_uuid, designator+":0")` for idempotency (op-index-free: inserting/reordering ops must not change untouched parts' identity) | `test_instances.py` |
| `connectivity.py` | pure-Python ERC-lite — **primary** post-write gate | `verify(doc,*,path=None)->list[Finding]`; `auto_junctions(doc)->None` | `model`, `geometry`, `errors` | Exact-coincidence of every new wire endpoint vs pin/label/junction/port/another wire ENDPOINT — a mid-span T conducts only with a `(junction)`, netbuild's rule (`t_midspan_connects=False`), pinned by `test_engine_parity.py`; auto-insert `(junction)` at 3+ way meets; honor `(no_connect)`; `DUPLICATE_UUID` + `DUPLICATE_DESIGNATOR` (same `(reference, unit)` counted per PATH ENTRY — two symbol nodes, or one symbol carrying the same ref on two instance paths of a reused sheet; the re-apply/uuid-seed-mismatch symptom; `?` placeholders of an unannotated sheet are NOT duplicates) + `UNRESOLVED_LIB_ID` + `INVALID_INSTANCES_PATH` (accepts `/<own-root>` **or** a multi-segment `/<top-root>/<sheet>…` parent-hierarchy path, so a child sheet file is writable) + `POWER_PORT_STACKED` (two net-naming power symbols on one coordinate; `PWR_FLAG` exempt, as in netbuild) + `SHEET_FILE_MISSING` (needs `path`) + `NO_CONNECT_CONFLICT`. Also exposed read-only as `check --integrity` over any `.kicad_sch`. **Runs with no KiCad installed** | `test_writer_connectivity.py` |
| `kicad.py` (facade over `executor/registry/context/anchors/properties/placement/handlers/*`) | op-list executor → surgical `.kicad_sch` edits | `apply(oplist:dict,path:str,apply:bool)->list[OpResult]`; `envelope_gate(oplist)->None`; `run_ops(oplist,src_libs,resolve)->tuple[list[OpResult],bool]` | all writers (incl. `atomic`) + `ops`,`safety`,`errors` | Per op: snap pin-ref endpoints to `pin_world`; emit per-pin `(pin "N" (uuid …))`. The document-level gates (higher major `protocol_version` → `PROTOCOL_MISMATCH`, structural validation, non-`kicad` `target_format`) live in `envelope_gate` and the per-op dispatch loop (with per-op containment, SPEC §2.4) in `run_ops` — **both shared verbatim with the multi-file transaction** (`txn.py`), which passes a sheet-routed `resolve(op)` where the single-file executor passes a constant one. The write itself is `atomic.py`'s primitives, not a private copy: snapshot/hash optimistic lock → temp+fsync → re-parse + connectivity verify on TEMP → `os.replace` only on pass, with the rotated `.bak` stack; `--dry-run` emits ops+verify, no write | `test_kicad_writer.py` |

### 3.6 Checks (`src/akcli/checks/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `erc.py` | electrical rule checks | `run(sch:Schematic,cfg:Config)->list[Finding]` | `model`, `report`, `config` | **Net-name-based power/ground detection** (not electrical-type — real boards are 292/298 Passive): nets touching a power port or matching rail-name set = power/ground; "IC has power+ground" tests net identity. Driver-conflict/floating gated behind **type-confidence** (fraction non-Passive) and downgraded when degenerate. Honor No-ERC suppression set (geo-match within grid tol) + config `erc_waivers`; the inverse contradiction — a No-ERC-marked pin that IS a member of a ≥2-pin net — is flagged as `NC_PIN_CONNECTED` (WARNING, waiver token `nc_pin_connected`). Net-alias conflicts → NOTE (e.g. `STAT==LED1_GPIO_RD`) | `test_erc.py` |
| `power.py` | rail enumeration + decoupling heuristic | `run(sch,cfg)->list[Finding]` | `model`, `config` | Enumerate rails from power ports + config `rails`; list consumers; decoupling-cap heuristic per IC power net; optional current budget if BOM annotated; rail voltage sanity vs config | `test_power.py` |
| `bom.py` | BOM hygiene | `run(sch)->list[Finding]` | `model` | Refdes parsed as `(alpha-prefix, opt-int-suffix)`; gap-detection **only within a numeric-suffixed prefix**; skip compound refs (`J_USB_C`,`X3`); dedup duplicate-designator by UniqueID/part; missing value/footprint | `test_bom.py` |
| `diff.py` | net-level v1↔v2 diff | `run(a:Schematic,b:Schematic)->DiffReport` | `model` | **Match nets by membership** (Jaccard bipartite), NOT display name; components by **UniqueID** (Altium↔Altium) then `(value,footprint,pin-count)` signature, then refdes; report name-vs-membership changes separately; document low-confidence for cross-revision | `test_diff.py` |
| `pinmap.py` | MCU pin→net + optional cross-check (GENERIC) | `run(sch,cfg,expected:dict\|None)->list[Finding]` | `model`, `config` | Generic only: emit MCU `pin→net`; cross-check against an **external expected pin→signal table** (CSV/JSON passed in). **No DTS/pinout parsing here** — that lives in `adapters/` (keeps engine reusable). Pin-name `Pn.mm` parser; schematic authoritative, expected-table advisory | `test_pinmap.py` |
| `intent.py` | design-intent net-membership assertions | `snapshot(sch)->dict`; `run(sch,spec)->list[Finding]` | `model`, `report` | Match declared `REF.PIN` members to actual nets by **pin membership** (not name); `exact`/`subset` modes + `fnmatch` refs; `INTENT_*` findings | `test_checks_intent.py` |
| `contract.py` | design contracts (topology/semantic policy ERC can't express) | `load(path)->dict`; `run(sch,doc)->list[Finding]` | `model`, `report`, `errors`, `calc.si` (lazy) | TOML `[[contract]]`: `require`/`forbid` pin-net, `require_same_net`/`forbid_same_net` pin pairs, `component`+`value` (an exact string, or a range table `{nominal,tolerance}` / `{min,max}` — values and bounds parse through `calc.si`, trailing BOM qualifiers allowed, an unparseable Value is `CONTRACT_FAIL`, never a pass), `nc`; `evidence` entries are strings and/or `{source,page,url}` tables; pin spec resolves number then name; `waived` exceptions w/ owner/expiry; PASS/FAIL/WAIVED all explicit as codes (`CONTRACT_PASS` info / `CONTRACT_FAIL` at the rule's severity / `CONTRACT_WAIVED` note), expired → `CONTRACT_EXCEPTION_EXPIRED` (warning) | `test_contract.py` |
| `integrity.py` | tree-scoped document integrity — what `check --integrity` runs | `run_tree(root,*,label_root=False)->list[Finding]` | `sexpr`, `readers.kicad`, `writers.connectivity`, `report` | Walks the sheet tree DOWNWARD from the given file (no root discovery); each file is verified once with the write gate's single-document checks (a child's messages are prefixed with its path); `DUPLICATE_DESIGNATOR` is also counted across the tree per live sheet instance by the reference eeschema resolves there (`readers.kicad.sheet_instance_refs`); `?` placeholders and `#` virtual references are not counted across sheets; a missing child is the parent's `SHEET_FILE_MISSING` and the walk continues | `test_check_integrity.py` |
| `schpcb.py` | schematic ↔ PCB equivalence | `run(sch:Schematic,pcb:Pcb)->list[Finding]` | `model`, `report` | Refdes presence + footprint/value assignment + pad-level **net partition** (names untrusted); errors `SCHPCB_MISSING_ON_PCB`/`_FOOTPRINT_MISMATCH`/`_PAD_MISSING`/`_NET_SPLIT`/`_NET_MERGE`, warnings `SCHPCB_EXTRA_ON_PCB` (NOTE when the footprint carries KiCad's `board_only` attribute — mounting holes, fiducials)/`_VALUE_MISMATCH`/`_UNNETTED_PAD`/`_NO_PAD_DATA`, plus `SCHPCB_DNP_MISMATCH`/`SCHPCB_BOM_FLAG_MISMATCH` (schematic `dnp`/`in_bom` vs footprint `dnp`/`exclude_from_bom`; KiCad schematic + KiCad board only — other readers cannot know the flags); `#`-prefixed pseudo-components excluded | `test_schpcb.py`, `test_fplib.py` |
| `fplib.py` | board footprints ↔ project footprint libraries (`verify --library`, preflight `pcb-library`) | `run(pcb:Pcb,ws:Workspace)->tuple[list[Finding],dict]`; `coverage_text(coverage)->str`; `NOT_COMPARED_REASONS` | `model`, `report`, `libtable` | Each board footprint vs. the definition its library id names in the PROJECT `fp-lib-table` (`libtable.load_libraries`): `FPLIB_PAD_SET_DRIFT` (pad-number multiset), `FPLIB_PAD_DRILL_DRIFT` (per-number type+drill), `FPLIB_SOURCE_MISSING` (nickname registered, footprint absent) — warnings, a local board edit being legitimate — and `FPLIB_MODEL_MISSING` (note; a `${KICAD<N>_3DMODEL_DIR}` stock reference counts as present). All `refs=[designator]`. Returns a coverage record `{footprints, compared, not_compared, reasons}`: footprints from the global table, an unregistered nickname, no library id, an unavailable or non-`.pretty` library or an ambiguous designator are COUNTED, never implied compared | `test_fplib.py` |

### 3.6b Design integrity (`src/akcli/` — library workspace, fab policy, footprint libs)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `libtable.py` | project library workspace: `sym-lib-table`/`fp-lib-table` audit + repair plans | `discover(project)->Workspace`; `load_libraries(ws,kinds?)->list[LoadedLibrary]`; `audit(ws,sch?)->list[Finding]`; `audit_with_origins(ws,sch?)->list[tuple[Finding,Path\|None]]`; `nearest_project_table(start,kind)->tuple[dir,table]`; `nicknames_for_dir(table,dir)->list[str]`; `plan_rename(ws,old,new)`; `plan_model_paths(ws,mode)` | `model`, `readers.footprint_lib`, `readers.sexpr`, `report` | Cross-check schematics ↔ tables ↔ library contents ↔ 3D; `FOOTPRINT_LIB_UNREGISTERED`/`FOOTPRINT_MISSING`/`LIB_URI_UNRESOLVED`/`MODEL_MISSING`/`FOOTPRINT_LEGACY_FORMAT`; findings carry waiver-scopable `refs` (designator, or table nickname `nick`/`nick:footprint`); `audit_with_origins` pairs each finding with the file it is about; `nicknames_for_dir` is an EXACT directory match (a `.pretty` is flat); repairs = lossless S-expr rewrites (dry-run→apply). `load_libraries` is the ONE walk over the project table entries (uri resolution → `.kicad_sym` symbol names / per-`.kicad_mod` `LoadedFootprint` with resolved `ModelRef`s) that `audit`, `library_baseline` and `checks/fplib` share; it reports nothing itself. A variable-anchored uri/model path (`${KIPRJMOD}/…`) is complete once expanded — only a BARE relative path is joined to the project/library directory, so a relative project directory resolves correctly; `is_kicad_builtin_ref` flags `${KICAD<N>_*_DIR}` references (KiCad's own stock files) | `test_library_workspace.py`, `test_library_baseline.py` |
| `library_baseline.py` | library baseline — the committed, hashed record of a project's libraries | `build(ws,sch?)->dict`; `dumps(doc)->str`; `write(path,doc)->bool`; `load(path)->dict`; `compare(baseline,current)->list[Finding]`; `summary(doc)->dict`; `DEFAULT_FILENAME` | `contract`, `libtable`, `report`, `errors`, `writers.atomic` | One record per PROJECT table entry: nickname, uri as written, project-relative posix path (`null` outside the project), status `ok`/`missing`/`unresolved`/`builtin`, and SHA-256 over raw bytes per file (`.kicad_sym`; every `.kicad_mod`; `provenance.json`; each referenced 3D model — status `hashed`/`missing`/`unresolved`/`builtin`, never dropped). Machine-independent by construction: sorted keys/lists, `\n`, no timestamp/tool version/absolute path; a `${KICAD<N>_*_DIR}` reference is recorded by reference only, and libraries the schematics reach outside the project tables by nickname only (`global_libraries`). `compare` → `LIB_BASELINE_DRIFT`/`LIB_BASELINE_MISSING` (error), `LIB_BASELINE_UNTRACKED` (warning), `refs` = nickname + `nickname:file`; `load` refuses anything unparseable or of another major version (`BAD_CONFIG`). `write` is atomic (`writers.atomic`), binary, no backup, and a no-op on identical bytes | `test_library_baseline.py` |
| `fab.py` | versioned fab profile + order-manifest checks | `load_profile(path)`; `resolve_profile(arg)->(profile,ProfileOrigin)`; `bundled_profiles()`; `retrieved_at(profile)`; `check(pcb,profile,order=None,*,today=None,origin=None,rules=None)->list[Finding]`; `evaluate(...)->(findings,facts)` (same check + the JSON-ready facts: `outline`, `via_types`, `tenting`, `project_rules`); `find_project(board)->Path\|None`; `read_project_rules(pro)->ProjectRules(path,rules,error)`; `load_order`/`check_order`; `explain(code,profile?)` | `model`, `report`, `errors`, `fab_profiles`, `readers.kicad_pro` | TOML profile (mandatory `[source]` urls); via geometry/type (blind, buried, micro)/via-in-pad (w/ `thermal_via` exceptions)/slots/stackup/cost thresholds (`[cost.warn_if]` WARNING, `[cost.error_if]` the same code at ERROR; optional `[cost.error_if]`/`[slot]`/`[rules]` tables are key- and type-validated on load); severity policy error/warning/note; order manifest = declared intent, never guessed. Area/drill density use the reader's true `Edge.Cuts` area, falling back to the bounding box — and saying so — when the outline does not close. The tenting cap binds vias the board file marks tented, or all of them when `order` declares `via_covering="tented"`; otherwise ONE `FAB_TENTING_UNKNOWN` NOTE. `rules` (the project's DRC minimums) below the profile's minimums → one `FAB_RULES_LOOSER_THAN_PROFILE` WARNING. `resolve_profile`: an existing file wins, else a bundled profile by `id`/file stem, else `BAD_CONFIG` listing the bundled ids; `ProfileOrigin(bundled,path,name,sha256)` hashes the exact bytes parsed. A BUNDLED profile >12 months past `retrieved_at` (or undated) adds one `FAB_PROFILE_STALE` NOTE; a profile file never does | `test_fab.py`, `test_fab_profiles.py` |
| `fab_profiles/__init__.py` | packaged mirror of the fab profiles under `examples/fab/` (`akcli.fab_profiles`), the shipping copy `--profile <id>` reads from an installed wheel | `names()->list[str]`; `read_bytes(name)->bytes` | none (stdlib `importlib.resources` only) | Data only — parsing/validation/id lookup live in `fab.py`. Roster comes from what is actually packaged, so a wheel built without the package data offers nothing; names are matched against the roster, never path-joined. `tools/examples_mirror.py build` populates it byte-for-byte, `check` is the CI drift gate | `test_fab_profiles.py` |
| `readers/footprint_lib.py` | `.PcbLib`/`.kicad_mod`/`.pretty` → `Library` w/ `FootprintDef`/`FootprintPad` | `read_pcblib(path,*,layer_map?)`; `read_pcblib_report(path,*,layer_map?)->(Library, per-footprint counts)`; `read_kicad_mod(path)`; `read_pretty(path)`; `DEFAULT_LAYER_MAP`; `KICAD_FOOTPRINT_LAYERS`; `canonical_altium_layer(name)` | `model`, `readers._cfbf`, `readers.sexpr`, `readers.altium_pcb_bin` | PcbLib pad framing shares `altium_pcb_bin` offsets, track/arc payloads its `decode_track`/`decode_arc`; +Y-up→+Y-down flip; tracks/arcs → `FootprintDef.graphics` (line/arc/circle) on the layer KiCad's Altium importer uses, `MechanicalN` → `User.N` + `MECHANICAL_LAYER` warning; text/fills/regions/3D/vias/keepouts and layers with no KiCad target → `UNSUPPORTED_PRIMITIVE` warnings; cross-validated vs KiCad's Altium importer | `test_footprint_lib.py`, `test_pcblib_graphics.py` |
| `writers/footprint_mod.py` | `FootprintDef` → `.kicad_mod` text (PcbLib import target) | `to_kicad_mod(fp,*,courtyard_mm?,warnings?)->str` | `model` | Pads verbatim (never recomputed); `graphics` → `fp_line`/`fp_arc`/`fp_circle` in source order; optional declared pad-bbox courtyard; deterministic output | `test_library_workspace.py`, `test_pcblib_graphics.py` |
| `commands/library.py` | `akcli library audit`/`repair`/`import-altium` | `register(sub,common)` | `libtable`, `library_baseline`, `readers.footprint_lib`, `writers.footprint_mod`, `safety` | `import-altium` writes `.kicad_mod` + `provenance.json` (source sha256, converter version, declared transforms); `audit` applies config `[[waiver]]` like `check`; `audit --write-baseline [FILE]` writes the library baseline (FILE → `[paths].library_baseline` → `akcli-library.baseline.json` beside the `.kicad_pro`; refuses to overwrite a file that is not a baseline) and `audit --baseline [FILE]` adds `library_baseline.compare`'s findings to the audit's; repair is atomic w/ `.bak` + re-audit (exit 1 when an ERROR remains in a rewritten file; `--json` `post_audit`) | `test_library_workspace.py` |
| `commands/fab.py` | `akcli fab check`/`explain`/`profiles` | `register(sub,common)` | `fab`, `readers.kicad` | deep-reads `.kicad_pcb`, runs profile + optional order manifest (`--order`, read first: its `via_covering` feeds the tenting rule) + the project's design rules (`.kicad_pro` discovered beside the board, `--project FILE` overrides; absent → skipped and stated in `--json`); `--json` adds `via_types`/`outline`/`tenting`/`project`; lint-style exit. `--profile FILE\|ID` goes through `fab.resolve_profile`; `fab profiles [--json]` lists the bundled profiles (`_stamp`ed: id, vendor, file, sha256, source urls, `retrieved_at`) | `test_fab.py`, `test_fab_profiles.py` |
| `commands/release.py` | `akcli release preflight` — run all gates, emit manifest | `register(sub,common)` | `checks.*`, `libtable`, `library_baseline`, `fab`, `readers.kicad`, `drivers.kicad_cli`, `sexpr`, `safety` | Gates check/intent/contract/library/library-baseline/sch-pcb/pcb-library/fab/drc/order/git; absent input → `skipped` w/ reason; `library-baseline` compares against `--library-baseline` → `[paths].library_baseline` → the default-named file beside the schematic (a NAMED baseline that is missing/unparseable is an error, not a skip; `inputs.library_baseline` = path+sha256); `pcb-library` is `checks/fplib` on `--pcb` as its own gate (`summary`/`coverage` state compared vs NOT-compared footprints; skipped when no footprint could be compared); manifest binds input sha256 (+ per-gerber `file_hashes`, fab profile `{id,sha256,bundled}`) + `tool.kicad_cli` + git rev (+ `dirty_sha256` when dirty: `git diff HEAD --binary` bytes, then NUL+path per untracked path — git output captured as bytes) + findings; dirty worktree fails unless `--allow-dirty`. `--drc` (opt-in, needs `--pcb`): `kicad_cli.drc` report → `KICAD_DRC_VIOLATION`/`KICAD_DRC_UNCONNECTED` at KiCad's severity, `refs` = rule type + designators/pins/nets resolved by item uuid against the board (`_board_items`; descriptions are localized, never parsed), `[[waiver]]`s applied; requested-but-unrunnable FAILS (`KICAD_CLI_MISSING`/`KICAD_DRC_FAILED`/`KICAD_CLI_TIMEOUT`); summary states zone fills are evaluated as saved | `test_release_preflight.py` |
| `commands/gate.py` | `akcli gate` — iteration-weight 4-check bundle for the GUI edit loop | `register(sub,common)`; `_default_check_names` shared with `commands/checks.py` | `checks.*`, `parts.bom_jlc`, `readers.kicad` | 4 gates: `check` (the FULL default `check` set, not preflight's narrower erc/power/bom/nets four-tuple), `intent` (`[paths].intent`, same key `check --intent` uses), `sch-pcb` (`[paths].pcb`), `bom-lock` (lock-drift via `bom_jlc.diff_lock`, the same call `jlc bom --against-lock` makes — DEFAULT OFFLINE/cache-only, `--live` opts into the network; a cache-degraded "unverified" line is surfaced honestly, never folded into a silent pass). `PATH` falls back to `[paths].schematic`. No release manifest/input-hashing/git-state — that stays `release preflight`'s job | `test_gate.py` |

### 3.7 Drivers (`src/akcli/drivers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `kicad_cli.py` | optional secondary verify wrapper | `available()->bool`; `version()->tuple\|None`; `version_text()->str\|None`; `erc(path)->dict\|None`; `drc(path)->dict\|None`; `netlist(path)->dict\|None` | `safety`, `errors` | `shutil.which`-gated; `sch erc --format json` (≥8), fall back `export netlist` (7); **never** pass `--exit-code-violations` (erc exits 0 even with violations); nonzero/crash = our write bug; absence non-fatal (connectivity.py is primary). `drc`: `pcb drc --format json --severity-error --severity-warning` (≥8) on a STAGED COPY (`_stage_board`: board + `.kicad_pro` + `.kicad_dru` + `fp-lib-table` with `${KIPRJMOD}` rewritten to the project dir) because kicad-cli 10 writes `<board>.kicad_prl` beside its input; never `--save-board`/`--refill-zones`; an unproducible report is `report: None` + `error` (never raised) | `test_kicad_cli.py` (skipif no `kicad-cli`) |
| `jlc2kicad.py` | LCSC→KiCad library conversion (in-process) | `convert(lcsc,out_dir,*,with_3d,lib_name,footprint_lib,footprint_nickname,model_path,force)->ConvertResult` | `_vendor.jlc2kicadlib` | Orchestrates the vendored converter: resolve CAD uuids via EasyEDA `products/<C>/svgs`, then vendored `create_footprint`/`create_symbol`; `footprint_lib` = output dir **and** the fp-lib-table nickname stamped into the symbol's Footprint field (`footprint_nickname` overrides the nickname only — `jlc add` passes the one the project's fp-lib-table registers for that dir); `model_path` ∈ `relative`/`absolute`/`${VAR}` for the 3D reference; error mapping `NETWORK`/`CONVERT_PART_NOT_FOUND`/`CONVERT_FAILED`/`CONVERT_NO_ARTIFACTS`; networked (the only one besides `jlc search/show`) | `test_jlc2kicad.py` (offline fixtures, injected transport) |


### 3.8 Vendored third-party source (`src/akcli/_vendor/`)

| Module | Purpose | Key surface | Notes | Tests |
|---|---|---|---|---|
| `jlc2kicadlib/` (upstream files) | JLC2KiCadLib conversion core (MIT, © TousstNicolas, commit `48d36032`) | `footprint.create_footprint`, `symbol.create_symbol`, `helper` | Vendored verbatim except import-level patches (all listed in its `PROVENANCE.md`); upstream `LICENSE` preserved in-tree; upstream CLI entry NOT vendored | `test_jlc2kicad.py` |
| `jlc2kicadlib/_http.py` (ours) | stdlib drop-in for the `requests` slice the vendored code uses | `get(url,headers)->Response`, `codes.ok`, injectable `opener` | urllib-based, size-capped, never raises on HTTP errors (returns status) | idem |
| `jlc2kicadlib/_kmt.py` (ours) | **clean-room** replacement for GPLv3 `KicadModTree` | `Footprint/Pad/Line/Arc/Circle/Polygon/Rect*/Text/Model/Translation/Vector2D`, `KicadFileHandler.writeFile` | Implemented from the public KiCad footprint file format (KicadModTree source neither copied nor consulted); emits the **KiCad-6 dialect** (`(layer)(width)` tails, version `20211014`) so output is readable by KiCad 6–10 AND Altium Designer's Import Wizard; `Model.at` legacy-inches → `(offset (xyz mm))` | idem |

### 3.8b Engineering calculators (`src/akcli/calc/`)

| File | Purpose | Public API | Reference basis | Tested by |
|---|---|---|---|---|
| `registry.py` | self-describing calc registry; every entry REQUIRES a formal citation | `Param`, `Result`, `Calc`, `CALCS`, `register()`, `compute(name,raw)->dict` (envelope carries `reference`) | — | `test_calc.py` |
| `si.py` | engineering-notation parse/format (`4k7`, `100n`, `2M2`) | `parse_value`, `fmt_eng` | BIPM SI Brochure 9th ed.; IEC 60062 R-marking | idem |
| `eseries.py` | E1–E24 tabulated + E48/E96/E192 formula series; snap; 2–4R combination search (single⊕pair, pair⊕pair meet-in-middle) | `SERIES`, `nearest`, `snap`, calcs `eseries`/`rcombo` | IEC 60063:2015 (incl. E192 9.20 exception) | idem |
| `electrical.py` | ohm/vdivider(+design)/led/rc/rc-charge/lc/reactance | registry calcs | Horowitz & Hill 3rd ed. §1.2/§1.4/§1.7 | idem |
| `regulator.py` | adj3 (LM317) + FB divider; exhaustive worst-case corners; pair design | `regulator`, `regulator-design` | TI SLVS044Y §8.2 | idem |
| `pcb.py` | IPC-2221 trackwidth/trackcurrent, Table 6-1 clearance (>500 V slopes), via R/Rθ/ampacity/L/C/rise, Onderdonk/Preece fusing, ASTM B258 AWG | registry calcs | IPC-2221B; Johnson & Graham 1993 §7; Stauffacher 1928; Preece 1884; ASTM B258-18 | idem |
| `rf.py` | wavelength, coax, twin-lead, microstrip (Hammerstad–Jensen), stripline (Cohn exact, K(k) via AGM), PI/TEE/bridged-TEE attenuators | registry calcs | H&J 1980; Cohn 1954; A&S §17.6; Pozar 4th ed.; Ref. Data for Radio Engineers 6th ed. | idem |
| `power.py` | buck/boost power stage, LDO dissipation, gate drive, shunt, flyback, battery life | registry calcs | TI SLVA477B/372C; SLUA618A; SBOA170; Erickson ch.6 | idem |
| `ic.py` | ne555-astable/mono, opamp-gain, i2c-pullup, crystal-caps, thermal | registry calcs | TI SLFS022I, SLOD006B; NXP UM10204 §7.1; ST AN2867; JESD51-2A | idem |
| `codes.py` | resistor color bands, SMD/EIA-96 decode, galvanic pairs | registry calcs | IEC 60062:2016; EIA-96; MIL-STD-889C | idem |
| `convert.py` | dBm/W/Vrms, mil/mm/inch/µm, oz↔µm copper | registry calcs | IEEE Std 100 (dBm); NIST SP 811 (25.4 exact); IPC copper nominal 34.8 µm + pure-Cu 34.06 µm | `test_calc_extended.py` |
| `analog.py` | comparator hysteresis (analysis+design, 3-R node), Sallen–Key equal-component LP, ADC LSB/SNR/settling | registry calcs | TI SLVA954; Sallen & Key 1955 / TI SLOA024B; MT-001, k=ln(2^(N+1)) settling | idem |
| `interface.py` | RS-485 fail-safe bias, CAN split termination (+ i2c-pullup regrouped here) | registry calcs | TIA/EIA-485-A + TI SLLA070D; ISO 11898-2:2016 §10 | idem |
| `protection.py` | TVS check, fuse derating (R10 ladder), NTC inrush | registry calcs | IEC 61000-4-5 + Littelfuse guide; IEC 60127; TDK/EPCOS NTC guide | idem |
| `opsmap.py` | calc→op-list bridge (`--ops`): design calcs → macro ops (`place_divider`/`place_led_indicator`/`place_pullup`/`place_crystal`) or `place_component` ops with E-series values | `MAPPABLE`, `to_oplist(name, envelope)->dict` | protocol-1 op-list; schema-validated in tests | idem |

Verification: `tests/test_calc.py` pins outputs to **KiCad pcb_calculator readings** (via vector
0.000575362 Ω / 83.29 °C/W / 2.9993 A / 0.5995 pF / 32.97 ps / 1.207 nH / 3.793 Ω; track 0.300387 /
0.781437 mm; clearance rows; 1 kΩ E24 exact combo) plus published table values (3 dB PI 292.4/17.61 Ω,
NE555 480 Hz, UM10204 966.7/3540 Ω, AN2867 19 pF). KiCad is a numerical cross-check only — GPL code
neither copied nor consulted.

### 3.9 Pinout / device-tree adapters (`src/akcli/adapters/` — optional, in-repo, imports only public model)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `dts.py` | Zephyr DTS/overlay + pinctrl parser → expected pin→signal table | `parse_dts(path)->dict`; `to_expected_table(dts)->dict` | stdlib | Extract `&gpio0 25`, `nordic,nrf-psel`/`NRF_PSEL` node→GPIO; output the external table `pinmap.run` consumes. Generic-Zephyr, not board-hardcoded | `test_dts.py` |
| `pinout_md.py` | parse human `pinout.md` table (columns `網路名`/`韌體節點`) → expected table | `parse_pinout_md(path)->dict` | stdlib | Markdown table by header; advisory source (low-severity on mismatch; pinout.md is explicitly untrusted) | `test_pinout_md.py` |

### 3.10 Plugin / packaging / tooling (repo root)

| File | Purpose | Key contents | Tested by |
|---|---|---|---|
| `.claude-plugin/plugin.json` | plugin manifest (name `akcli`) | §5.1 — no `version` during dev; no `skills`/`commands` arrays (default scan) | CI `claude plugin validate` |
| `.claude-plugin/marketplace.json` | self-marketplace (`source:"./"`) | §5.2 — required `owner.name` | CI `claude plugin validate . --strict` |
| `bin/akcli` | self-locating zero-dep PATH wrapper | §5.3 — mode 100755 | `test -x bin/akcli` in CI; `akcli --help` smoke |
| `bin/akcli` | long-alias bare command | relative symlink → `akcli` | CI smoke |
| `hooks/hooks.json` | SessionStart Python-version warning | §5.4 — stderr only, exit 0 | `jq . hooks/hooks.json` in CI |
| `pyproject.toml` | PyPI dist + console_scripts | §7 — setuptools backend, `packages.find where=["src"]`, EDA classifier | `python -m build && twine check dist/*` |
| `tools/sync_version.py` | stamp plugin.json/marketplace.json from pyproject version | `main()`; CI fails on drift | `test_sync_version.py` |
| `tools/schemas.py` | the ONE schema entry point: `build` regenerates `schemas/ops.schema.json` + `ops.capabilities.json` from `akcli.opspec` AND refreshes the packaged mirror `src/akcli/schemas/` (pruning orphans); `check` reports both drifts without writing | `build_schema()`, `build_capabilities()`, `render()`, `generate(check=)`, `sync(check=)`, `main(argv)`; generated output is byte-exact, and the two schema directories must hold the same set of names | `test_ops_kit.py`, `test_schema_exports.py` |
| `tools/gen_ops_schema.py` | thin compatibility entry point onto the GENERATE half of `tools/schemas.py` | `main(argv)` | — |
| `tools/sync_schemas.py` | thin compatibility entry point onto the MIRROR half of `tools/schemas.py` | `main(argv)` | — |
| `tools/examples_mirror.py` | the same mirror+check shape as `tools/schemas.py`, over the example corpora instead of the op schemas: `build` copies every `examples/circuits/<name>/ops.json` into the packaged `src/akcli/examples/<name>/ops.json` AND every fab profile under `examples/fab/*.toml` (recognised by content: `id` + `[source]`; the order manifest is not one) into `src/akcli/fab_profiles/` (pruning orphans whose canonical source was renamed/deleted); `check` is the CI drift gate over both (also asserted by `tests/test_examples.py` / `tests/test_fab_profiles.py`) | `source_names()`, `mirror_names()`, `profile_source_names()`, `profile_mirror_names()`, `sync(check=)`, `main(argv)`; bytes are compared/copied verbatim, never reformatted | `test_examples.py`, `test_fab_profiles.py` |
| `.gitattributes` | binary/text fixture rules | §6 — `tests/fixtures/** binary`, golden/`*.kicad_sch text eol=lf` | implicit (Windows CI) |
| `.github/workflows/ci.yml` | CI matrix + validators | §6.4 | self |
| `examples/akcli.toml.example` | tested reference config incl. `[[erc_waiver]]` | §3.11 | `test_config.py` |
| `LICENSE` | MIT (repo) | — | — |
| `THIRD_PARTY_NOTICES.md` | MIT attribution chain for altium-mcp **patterns** | credits flaco-source (2026) + coffeenmusic/Siddharth Ahuja (2025) | — |
| `SECURITY.md` | untrusted-input threat model + enforced limits | — | — |
| `CHANGELOG.md` | SemVer + protocol_version policy | — | — |
| `README.md`, `INSTALL.md` | docs + install UX | — | link-check (optional) |
| `docs/cli-reference.md` (config + CLI), `docs/op-list-authoring.md`, `schemas/ops.capabilities.json` (op-capability matrix) | contracts | — | — |

### 3.11 Config schema (`akcli.toml`)

```toml
[project]
mcu_designator = "U3"
grid = "50mil"          # schematic pin grid: bare number = mils, or "Nmil"/"Nmm"
backup_depth = 3        # rotated .akcli/backups/ depth, 1..99

[[rail]]
name = "RAIL_A"
voltage = 3.3
tolerance_pct = 5

[paths]
schematic = "hardware/main.SchDoc"   # resolved relative to THIS file's dir
dts       = "firmware/board.dts"     # reserved — not read today; pass via `pinmap --expected`
pinout_md = "docs/pinout.md"         # reserved — not read today; pass via `pinmap --expected`
intent    = "intent.json"            # default for `check --intent` / `gate` (flag overrides)
pcb       = "hardware/main.kicad_pcb" # default sch-pcb-gate input for `gate` (flag overrides)
bom_lock  = "bom.lock.json"          # default bom-lock-gate input for `gate` (flag overrides)
library_baseline = "akcli-library.baseline.json"  # default FILE for `library audit --write-baseline`/`--baseline` and preflight's library-baseline gate
parts_dir = "hardware/libs"          # default out-dir for `jlc add` / `library import-altium` / `library new-symbol`

[[erc_waiver]]                       # legacy ERC-only waiver (back-compat alias)
net    = "LED1_GPIO_RD"
rule   = "driver_conflict"
reason = "LED1 shares MCP73831 open-drain STAT (P0.25) by design; FW reads it as input"

[[waiver]]                           # checker-agnostic waiver (report.apply_waivers)
code     = "PAIR_INCOMPLETE"         # finding code or fnmatch glob
refs     = ["USB_D_P"]               # optional ref filter (string or list)
severity = "off"                     # note | info | off
reason   = "single-ended debug tap by design"

[check]
pairs = true                         # diff-pair/bus continuity in the default set
pair_suffixes = [["_P", "_N"]]       # replaces the suffix table
bus_min_family = 4                   # BUS_GAP threshold
group_clearance = 1000               # mils; LAYOUT_GROUP_CLEARANCE floor (0 = off)

[bom]
coverage_floor = 0.9                 # 0..1; jlc bom coverage gate
coverage_min_parts = 5
min_stock = 100
extended_fee = 3.0
[bom.classes]
no_part = ["TP", "FID", "MH"]        # structural refdes prefixes, never sourced

[arrange]                            # group-layout policy (mils); flags override
group_margin = 200                   # clearance between bundles inside a group
group_gap = 1000                     # channel between group blocks, both axes
row_width = 4000                     # wrap a group's internal shelf
page_width = 20000                   # 2D side-by-side packing wrap (omit = single column)
```

`config.load_config` rejects unknown keys → `BAD_CONFIG`; discovery walks up from cwd; `-C/--config` overrides.
Exception: `[paths]` accepts **arbitrary** keys (values must be strings, resolved against the
config dir) — any entry whose value ends in `.kicad_sym` is additionally offered to the writer
as a symbol source, so a project can register its libraries under any names it likes.
Every top-level config table is asserted present in this block AND in `examples/akcli.toml.example`
by the config-surface gate in `tests/test_docs_conformance.py`, so a new table cannot ship undocumented.

### 3.12 Schemas (`schemas/`)

Mirrored byte-identically into `src/akcli/schemas/` (wheel-packaged); the mirror and this
table are gated — every `schemas/*.json` file must appear here (`tests/test_docs_conformance.py`)
and in the packaged mirror (`tests/test_schema_exports.py`).

| File | Purpose |
|---|---|
| `ops.schema.json` | §2.5 — own `$id` namespace, own ERROR enum, `protocol_version` const; every op branch declares `additionalProperties: false` (plus the universal `group` tag), so schema validation catches field typos too |
| `ops.capabilities.json` | per-op executor support matrix (KiCad writer + the informational `altium` column); document-level `altium_write_wired: false` / `altium_write_note` (published as `ops.altium_write_wired` by `akcli capabilities --json`) |
| `schematic.schema.json` | `Schematic` export shape + `schema_version` |
| `netlist.schema.json` | net membership + Altium net-naming rules (same-name merge, priority, single-pin gating) |
| `diff.schema.json` | `akcli diff --json` findings envelope |
| `pinmap.schema.json` | `akcli pinmap --json` findings envelope (pin→net rows + expected-table mismatches) |
| `draw-result.schema.json` | `plan`/`draw --json` per-op results + connectivity + net diff |
| `findings.schema.json` | `review analyze --json` confidence-graded findings + evidence envelope |
| `datasheet-facts.schema.json` | facts store: PDF sha256+page-pinned component facts |
| `proposals.schema.json` | `review propose --json` fix drafts (op-list/contract/sim) |
| `sim.schema.json` | `akcli sim` assertion document + result envelope |
| `doc.schema.json` | `akcli doc --json` pinout book (pin tables + rails + BOM) |
| `design-intent.schema.json` | `akcli ops from-intent` design document: the `check --intent` nets table + `components` + `layout` hints (a SUPERSET of the intent-assertion format — one file drives generation and stays the assertion baseline) |
| `library-baseline.schema.json` | the library baseline `library audit --write-baseline` writes and `--baseline` / `release preflight` compare against: per project lib-table entry the nickname, uri, project-relative path, status and SHA-256 per file + 3D model; `additionalProperties: false`, own `schema_version` (`1.0`) |
| `symbol-pins.schema.json` | `akcli library new-symbol` INPUT pin table — coordinates are computed, never authored; `side` and `type` are required per pin, and `additionalProperties: false` throughout means a field typo is refused instead of silently defaulted |

---

## 4. PARALLEL OWNERSHIP GROUPS (zero file overlap)

**FOUNDATION (Group F) — authored FIRST and FROZEN before any other group starts.** No other group may edit
these; they are the contract everyone codes against:
`model.py, opspec.py, ops/, errors.py, safety.py, units.py, config.py, report.py, __main__.py`,
`schemas/{ops,ops.capabilities,schematic,netlist}.schema.json`,
`.claude-plugin/{plugin,marketplace}.json, bin/akcli, bin/akcli, hooks/hooks.json, pyproject.toml,
.gitattributes`, `tests/fixtures/_gen/{altium_fixture.py,ole_writer.py,cfbf_builder.py}`,
`tests/fixtures/MANIFEST.sha256`.

> Foundation includes the **fixture generators** because multiple downstream groups need synthetic fixtures;
> generating them centrally prevents drift and gives every group a stable input.

After F is frozen, these groups proceed **in parallel** (each owns disjoint files + writes its own tests):

| Group | Files (owned exclusively) | Depends on | May start after |
|---|---|---|---|
| **A — Altium readers** | `readers/_cfbf.py, readers/altium_records.py, readers/altium_sch.py, readers/altium_schlib.py, readers/altium_pcb.py` + their tests | F | F frozen |
| **N — Net inference** | `netbuild.py` + `test_netbuild.py` | F | F frozen |
| **K — KiCad readers** | `readers/sexpr.py, readers/kicad.py, readers/kicad_lib.py` + tests | F, N (uses `build_nets`) | F frozen |
| **W — KiCad writer** | `writers/{geometry,sexpr_writer,lib_cache,instances,connectivity,kicad}.py` + tests | F, K (uses `sexpr`,`kicad_lib`) | **K's `sexpr.py`+`kicad_lib.py` exist** |
| **C — Checks** | `checks/{erc,power,bom,diff,pinmap}.py` + tests | F, model populated by A/N/K | A+N (and K for KiCad inputs) ready |
| **D — Drivers** | `drivers/kicad_cli.py, drivers/jlc2kicad.py` + tests | F (ops, errors) | F frozen |
| **ADP — Adapter** | `adapters/{dts,pinout_md}.py` + tests | F | F frozen |
| **Docs** | `README.md, INSTALL.md, SECURITY.md, THIRD_PARTY_NOTICES.md, CHANGELOG.md, LICENSE, docs/*, tools/sync_version.py, .github/workflows/ci.yml, examples/*` | F (exact names) | F frozen |
| **X — CLI glue** | `cli.py` (+ `test_cli.py`) | A, N, K, W, C, D | those groups expose stable APIs |
| **S — Skill/commands** | `skills/akcli-circuit-design/SKILL.md, commands/circuit-{review,pinmap,draw,diff}.md` | X (CLI flags pinned in `docs/cli-reference.md`) | X ready |

**Dependency order (DAG):** `F → {A, N, D, ADP, Docs}`; `F → K`; `K → W`; `{A,N,K} → C`; `{A,N,K,W,C,D} → X`; `X → S`.

`sexpr.py` is owned by **K** (not F) but is the only K file W needs — W starts as soon as `sexpr.py` +
`kicad_lib.py` land, even if `kicad.py` is still in progress. No two groups ever touch the same file.

---

## 5. Exact plugin/wrapper/hook contents

### 5.1 `.claude-plugin/plugin.json`
```json
{
  "name": "akcli",
  "displayName": "akcli — AI-native KiCad design agent (+ Altium import)",
  "description": "Verification layer for AI-authored KiCad schematics (zero-dependency pure-stdlib Python CLI): every .kicad_sch edit goes through a JSON op-list that is dry-run by default, net-diffed, connectivity-gated and reversible; run ERC/design/intent/BOM checks, simulate on KiCad's bundled ngspice, source real parts and fetch datasheets, and import Altium .SchDoc/.SchLib/.PcbDoc read-only into the KiCad flow.",
  "keywords": ["altium","kicad","schdoc","kicad-sch","eda","schematic","pcb","netlist","erc","claude-code","ai-agents"],
  "repository": "https://github.com/tipoLi5890/akcli",
  "homepage": "https://github.com/tipoLi5890/akcli",
  "license": "MIT"
}
```
> No `version` during active development (commit-SHA versioning). No `skills`/`commands` keys → default
> `skills/` + `commands/` scans run. The plugin root MUST equal the repo root (`source:"./"`) because `src/`,
> `bin/`, `skills/`, `commands/`, `hooks/` are all copied into the cache together.

### 5.2 `.claude-plugin/marketplace.json`
```json
{
  "name": "akcli",
  "owner": { "name": "Li, ching yu" },
  "plugins": [
    {
      "name": "akcli",
      "source": "./",
      "description": "Verification layer for AI-authored KiCad schematics (zero-dependency pure-stdlib Python CLI): every .kicad_sch edit goes through a JSON op-list that is dry-run by default, net-diffed, connectivity-gated and reversible; run ERC/design/intent/BOM checks, simulate on KiCad's bundled ngspice, source real parts and fetch datasheets, and import Altium .SchDoc/.SchLib/.PcbDoc read-only into the KiCad flow.",
      "keywords": ["altium","kicad","schdoc","kicad-sch","eda","schematic","pcb","netlist","erc","claude-code","ai-agents"]
    }
  ]
}
```
> Required `owner.name`. **No partial `skills`/`commands` arrays** (a typo would silently drop the default
> scan). The `@`-token users type after install is the marketplace **name** (`akcli`), not the repo.

### 5.3 `bin/akcli` (mode 100755)
```bash
#!/usr/bin/env bash
set -euo pipefail
# Self-locating: $CLAUDE_PLUGIN_ROOT is NOT guaranteed for bin/ executables, so derive from $0.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# Discover a Python >=3.11 (macOS default python3 is often 3.9; we need stdlib tomllib).
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "akcli requires Python >=3.11 (none found). Install python@3.11+ or: pipx install akcli-kicad" >&2; exit 1; }
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m akcli "$@"
```
`bin/akcli` = relative symlink → `akcli` (preserved in plugin cache).

### 5.4 `hooks/hooks.json`
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 -c \"import sys; sys.stderr.write('warn: akcli needs Python>=3.11; akcli auto-selects a newer interpreter if present\\n') if sys.version_info < (3,11) else None\"; exit 0"
          }
        ]
      }
    ]
  }
}
```
> SessionStart **cannot block** and its **stdout is injected into model context** — so we emit to stderr only
> and `exit 0`. The wrapper's own interpreter check is the real guard; this hook is a courtesy warning kept fast.

---

## 6. Testing & fixtures plan (synthetic only)

### 6.1 Fixturing the binary Altium parser (the hard part)
- **Generators (committed, Foundation, pure stdlib):**
  - `tests/fixtures/_gen/altium_fixture.py` — high-level records → raw FileHeader buffer; **auto-prepends
    HEADER, auto-computes OwnerIndex** (kills the off-by-one landmine). Emits both a binary blob and a
    reviewable `.records.txt` sibling.
  - `tests/fixtures/_gen/ole_writer.py` / `cfbf_builder.py` — pack `{stream:bytes}` into a valid OLE2/CFBF,
    with a flag to force **miniFAT (<4096 B)** vs **multi-sector FAT-chain (>4096 B, ≥9 sectors)** layout, so
    both `read_mini` and `read_chain` get CI coverage (the real board uses the FAT-chain path; the env-gated
    real test never runs in CI).
- **Two synthetic `.SchDoc` containers by design:** `ole_minifat.SchDoc` and `ole_fatchain.SchDoc`; a test
  asserts identical parsed records from both.
- **Net-inference regression fixtures (hand-authored expected netlists — NEVER snapshotted from `build()`):**
  - `shared_name_label` — two same-`Text` labels on disjoint clusters (STAT/LED1 class) → assert ONE net
    `{U2.1,U3.2,R7.1,R12.1}`, zero single-pin nets there.
  - `junction_cross` — `+` crossing with a RECORD-29 dot → merge; without dot → no merge.
  - `t_junction` — wire endpoint on another wire's mid-span → merge.
  - `no_erc` — RECORD-22 marker on a deliberately open pin → suppressed.
  - `two_gnd_ports` — two same-name `GND` power ports on separate clusters → collapse to one net.
- **Malformed corpus** `tests/fixtures/malformed/`: FAT cycle, OOB sector, bogus sector_shift, huge `ndifat`,
  `mini_cutoff` bomb, truncated header, zero-length stream, missing root; deeply-nested S-expr, 10 MB atom,
  unterminated quote, symlinked lib path. `test_fuzz_safety.py` asserts each raises a **structured** error
  within a time/memory budget (stdlib `signal.alarm` + `resource.setrlimit`).
- **Edge micro-fixtures:** truncated final record; label `Text` containing `|`/`=`; CJK + Ω/µ `%UTF8%` field
  (assert clean BOM round-trip); stream sized exactly 4095 and 4096 (cutoff boundary).
- **Schema validation, as it actually exists today:** dev-extra (`pytest.importorskip("jsonschema")`) tests
  validate rendered payloads against their published schema — `test_output_schemas.py` (`doc`/`diff`/
  `pinmap`/`draw-result`), `test_cli_json_errors.py` (`draw-result`), `test_schema_exports.py`
  (`schematic.schema.json` against every `read --json` fixture, `ops.schema.json` against every op/macro
  template, plus a packaged-mirror byte-identity check over **every** `schemas/*.json` file),
  `test_findings_schema.py` / `test_review_facts.py` (`findings`/`datasheet-facts`), `test_review_propose.py`
  (`proposals`), `test_sim_assertions.py` (`sim`), `test_intentgen.py` (`design-intent`, plus a
  both-directions key/enum census against the loader), `test_symbol_pins_schema.py` (`symbol-pins`,
  plus schema↔loader reject-parity). `netlist.schema.json` is the one schema with **no**
  payload-validation test — a contract artifact (documents net-membership/naming-rules shape) that no command's
  JSON output is checked against; only its packaged-mirror byte-identity is gated. There is no env-gated
  real-Altium-file integration test in this repo — `test_integration_real.py` / `AKCLI_ALTIUM_SAMPLE` /
  `AKCLI_ALTIUM_EXPECT` never existed (checked against the full git history), despite the `.gitignore` entry
  for `AKCLI_ALTIUM_EXPECT*.json` left over from planning it.

### 6.2 KiCad fixtures
- Vendored minimal symbol source `tests/fixtures/kicad/symbols/` — `Device` R/C/L + `power` GND/+3V3, incl.
  one `(extends)`-derived symbol (`C_Polarized`) — because dev/CI machine has **no KiCad libs**.
- Version-matrix fixtures: at least one **KiCad 7** and one **KiCad 8** `.kicad_sch`.
- **Gates (block all writer work until green):**
  - `test_roundtrip_byte_identity.py` — read real `.kicad_sch` → write unchanged → byte-identical.
  - `test_writer_connectivity.py` — place R/C + power port, wire pin-to-pin → `connectivity.verify` reports
    zero dangling + auto-junctions inserted; optional `kicad-cli` cross-check when available.

### 6.3 Golden/diff hygiene
- Canonicalize unnamed nets by **smallest member token** before any golden compare; order-invariance test
  (shuffled record order → identical canonical netlist).
- `.gitattributes` marks fixtures binary, golden/`.kicad_sch` text `eol=lf`; `MANIFEST.sha256` is written by
  the fixture generator but — as of this writing — verified by no test or CI step (the planned
  Windows-checkout-corruption gate was never wired).

### 6.4 CI matrix
- **Lint/unit/fixture jobs:** OS = {ubuntu, macos, windows} × Python = {3.11, 3.12, 3.13, 3.14}. Runtime is
  zero-dep; tests use `pip install -e .[dev]` (`pytest`; `jsonschema` dev-only for schema tests).
- **KiCad-cli job:** single dedicated entry **ubuntu + pinned KiCad 10.x** (heavy install; pin one major).
  Everywhere else `shutil.which`-skipif. A "no kicad-cli" job proves graceful degradation.
- **Manifest/packaging jobs:** `jq . hooks/hooks.json`; `claude plugin validate . --strict` (marketplace) +
  a separate plugin-component validation/load smoke (copy tree minus marketplace.json, validate); `test -x
  bin/akcli`; `akcli --help` from a clean checkout; `python -m build && twine check dist/*`;
  `tools/sync_version.py --check`.

---

## 7. Packaging metadata (`pyproject.toml`)
```toml
[build-system]
# PEP 639 bare license string (`license = "MIT"`) requires setuptools >= 77.
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
# PyPI dist name: the distribution ships as `akcli-kicad` while the import
# package and CLI stay `akcli`.
name = "akcli-kicad"
version = "0.23.0"
description = "Verification layer for AI-authored KiCad schematics: draw and edit .kicad_sch from JSON op-lists that are dry-run by default, net-diffed and connectivity-gated, run ERC/design/intent checks, simulate on bundled ngspice, source parts, and import Altium .SchDoc/.PcbDoc read-only into the KiCad flow."
requires-python = ">=3.11"
readme = "README.md"
license = "MIT"
classifiers = [
  "Development Status :: 3 - Alpha",
  "Environment :: Console",
  "Intended Audience :: Developers",
  "Intended Audience :: Manufacturing",
  "Intended Audience :: Science/Research",
  "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: 3.14",
  "Operating System :: OS Independent",
]

[project.urls]
Homepage = "https://github.com/tipoLi5890/akcli"
Repository = "https://github.com/tipoLi5890/akcli.git"
Changelog = "https://github.com/tipoLi5890/akcli/blob/main/CHANGELOG.md"

[project.scripts]
akcli = "akcli.cli:main"   # doc-noqa (pyproject entry point, not a CLI command)

[project.optional-dependencies]
dev = ["pytest>=8", "jsonschema>=4", "build", "twine", "ruff>=0.15", "mypy>=1.10"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"akcli.webui" = ["*.html"]
"akcli.schemas" = ["*.json"]
"akcli.examples" = ["*/ops.json"]
"akcli.fab_profiles" = ["*.toml"]
"akcli.sim" = ["*.lib"]
"akcli._vendor.jlc2kicadlib" = ["LICENSE", "PROVENANCE.md"]
```
> The EDA classifier string is exact-trove (validated by `twine check` in CI).

---

## 8. Risk register + platform validity

**Exit-code table (`errors.EXIT`):** 0 success/no findings · 1 check findings present · 2 usage/arg error ·
3 parse error (corrupt OLE2/sexpr) · 4 file not found or otherwise unusable path (directory-as-file, unreadable bytes) · 5 unsupported format · 6 op-list/verify failure ·
7 external tool missing · 8 query miss (entity absent from a file that parsed). `check` is lint-style
(non-zero on findings) with `--fail-on never` for report mode (`--exit-zero` is a deprecated alias).

| # | Risk | Mitigation | macOS-validatable? |
|---|---|---|---|
| 1 | Net-inference defect (STAT/LED1 not merged) ships as "expected" | Rebuild net layer in `netbuild.py`; hand-authored golden; STAT regression fixture gates checks | ✅ fully (synthetic) |
| 2 | Multi-storage OLE collapse (.SchLib/.PcbDoc) | `_cfbf.read_streams_qualified` walks dir tree; per-storage tests | ✅ |
| 3 | Pin types absent/Passive → vacuous ERC pass | net-name-based power/ground + type-confidence gating + report caveat | ✅ |
| 4 | Unbounded FAT/miniFAT loops, header allocation bombs | `safety` caps + cycle detection + header validation; `test_fuzz_safety` | ✅ |
| 5 | S-expr recursion → uncatchable SIGSEGV | iterative parser, depth/atom/node caps, ban `setrecursionlimit` | ✅ |
| 6 | KiCad wires don't connect (geometry/units) | integer-nm math, pin-world snapping, `connectivity.verify` primary gate, byte-identity gate | ✅ (no KiCad needed for primary gate) |
| 7 | Non-deterministic UUIDs → non-idempotent writes; an edited op-list re-identifying untouched nodes → the design duplicated downstream of the edit | UUIDv5 from sheet-uuid + **content** for EVERY created node — designator (parts), endpoints (wires), name+written anchor (labels), file+name (sheets), coordinates (graphics) — with **no op index anywhere**; the same content-seeding extends to FILES: `add_sheet {"create": true}` mints the created child's own ROOT uuid from the PARENT's root uuid + the `Sheetfile` string (`identity.seed_sheet_file`), so plan and apply agree on the child's identity before either writes anything, and a deleted-then-recreated child re-identifies byte-for-byte; `#PWR` refs recovered by position (+species, so a `PWR_FLAG` may share a port's point); two ops seeding one uuid are refused naming both indices; in a multi-sheet transaction every routed file seeds from its OWN root uuid, so the namespaces stay per-file and each file replays byte-identically; byte-identity re-apply gate, and `DUPLICATE_DESIGNATOR`/`POWER_PORT_STACKED` as ERRORs in the write gate / `check --integrity` | ✅ |
| 8 | `(instances)` path / `#PWR` wrong → empty netlist / R? | write both legacy + instances path; ops target one file's own root sheet (sub-sheet path → `HIERARCHICAL_UNSUPPORTED`), while the gate accepts a child file's parent-hierarchy path so child sheets are writable — a part placed INTO a child copies its siblings' full sheet-path set (both entries of a twice-instantiated sheet), since the child cannot derive its parents' uuids; on such a sheet the reference is written PER path (`instance_designators`) and a scalar designator is refused `INSTANCE_REFS_REQUIRED` rather than stamping one refdes onto every instance, with `DUPLICATE_DESIGNATOR` counting per path entry; `INVALID_INSTANCES_PATH` in the write gate and `check --integrity` | ✅ |
| 9 | `bin/akcli` `$CLAUDE_PLUGIN_ROOT` empty / python 3.9 | self-locating wrapper + interpreter discovery | ✅ |
| 10 | DIFF noise (coordinate-named nets) | match by membership/UniqueID, never display name | ✅ |
| 11 | Licensing: proprietary header + false "clean-room" | relicense parser to MIT (strip `LicenseRef-Proprietary`); own schema namespace; `THIRD_PARTY_NOTICES` crediting altium-mcp chain | ✅ |
| 12 | Path traversal via lib_id / config | `safety.safe_path` allowlist; atomic writes stay inside the project root | ✅ |
| 13 | Destructive write corrupts user schematic | snapshot+temp+fsync+verify-temp+`os.replace`, mtime lock, `--apply` required | ✅ |
| 14 | kicad-cli erc exit-code misread | never pass `--exit-code-violations`; parse JSON; advisory only | ⚠️ needs kicad-cli (CI ubuntu only) |
| 15 | PCB binary sections (Pads/Tracks) | `altium_pcb_bin` decodes Tracks6/Vias6/Arcs6/Pads6; cross-validated vs KiCad's pcbnew importer on real boards; fills/regions/texts/polygons still guarded/deferred | ✅ (read + verify) |
| 16 | **Altium authoritative netlist / ERC / write-back** | out of scope by the 0.14 KiCad-first decision: Altium is IMPORT-ONLY. The Windows live-driver scaffold (`altium_live/`) was DELETED in 0.17 — net equivalence is proved by `akcli verify` against the imported model instead | **❌ not supported (by design)** |
| 18 | Multi-sheet/hierarchical merge validated on synthetic trees only (no real-world board fixture) | `tests/fixtures/kicad/hier/` is a hand-authored 3-level tree whose child sheet is instantiated twice; its cross-sheet net NAMING is arbitrated against a committed real `kicad-cli` export (`netlist_hier_root.kicad10.net`, re-derived live on the KiCad CI job). Around it: `test_project.py` (root discovery / tree staging), `test_txn.py` (multi-file transaction, rollback, `undo --run`), `test_kicad_hierarchy.py` (writing into a twice-instantiated child, per-instance references), agent-eval task 09 (the `sheets` envelope end-to-end). A real-world hierarchical board is still not in the corpus, so keep the caveat | ✅ (synthetic tree; live kicad-cli re-derive on CI ubuntu) |
| 19 | Version drift across 3 manifests | pyproject is SoT; `tools/sync_version.py` + CI check | ✅ |
| 20 | Repo public over-promises | Roadmap section; public claims match shipped features | ✅ |

**Summary of platform split:**
- **Fully buildable + validated on macOS/Linux/CI:** all Altium **read/analyze**, net inference, all checks,
  report, op-list + validator, KiCad **read + write/draw** (primary connectivity gate is pure Python), CLI,
  plugin, docs, safety/fuzz.
- **Cross-platform but needs `kicad-cli` (KiCad 10+; CI ubuntu + KiCad 10):** the
  *optional secondary* ERC verify, and the `.kicad_pro`/`.kicad_pcb` ground-truth e2e.
- **Not supported anywhere (by design):** writing Altium files or driving a running Altium — Altium is
  import-only; every write path targets `.kicad_sch` (`akcli project new`/`project init`'s scaffold
  engine additionally emits `.kicad_pro`/`.kicad_pcb`/the two lib tables as empty containers. It
  never parses-and-rewrites the CONTENT of any of the four: `init` keeps an existing one untouched,
  and `new --force` replaces it wholesale with a fresh blank — a board scaffold is an empty
  container, not a layout, and akcli authors no PCB content).

---

## 9. Build order

**Frozen-first set (author + freeze before anything else):** `model.py, opspec.py, ops/, errors.py, safety.py,
units.py, config.py, schemas/*.json, pyproject.toml, .claude-plugin/plugin.json,
.claude-plugin/marketplace.json, bin/akcli, hooks/hooks.json` + the fixture generators. Everything else codes
against these signatures and never edits them. After the foundation, the ownership groups in §4 proceed in
the dependency order of that section's DAG.

---

## 10. Output placement policy (state / cache / deliverable)

Every file akcli writes belongs to exactly ONE class, and the class decides where its
**default** location may be. Adding a write surface without classifying it fails the
census gate in `tests/test_output_policy.py`.

| Class | What | Default location | Rules |
|---|---|---|---|
| **State** | tool-internal, re-derivable, supports undo: `journal.jsonl`, rotated backups | `.akcli/` next to the edited file (`journal.workspace_dir`) | Created only via `journal.ensure_workspace_dir`/`ensure_backups_dir` (or the `safety` backup hook), which drop a self-ignoring `.akcli/.gitignore` (`*`) so the user's `.gitignore` is never touched. Never committed, never required to rebuild anything. |
| **Cache** | re-derivable, personal, shareable across projects: jlc search cache, fetched datasheet PDFs | XDG cache (`~/.cache/akcli/...`); env overrides `AKCLI_JLC_CACHE` / `AKCLI_DATASHEET_DIR` | Must never default into the CWD. Never load-bearing: review results must not silently depend on one person's cache. |
| **Deliverable** | what the user asked for / the team signs off on: the `.kicad_sch` itself, `sym-lib-table`/`fp-lib-table`, converted libraries (`jlc add`, `library import-altium`), the **datasheet facts store** (`./datasheets` — committed, code-reviewed numbers that survive handoff and feed CI), the **library baseline** (`akcli-library.baseline.json` — the committed hash record the release gate compares against), rendered SVG | in-repo; libraries under `[paths].parts_dir` (default `./akcli-parts`), facts under `./datasheets`, the library baseline beside the `.kicad_pro` (`[paths].library_baseline` / `--write-baseline FILE` override), SVG next to the schematic | Every deliverable command MUST accept `--out`/`--dir`. In-repo defaults must be a single predictable directory (no scattering) and be documented in `docs/cli-reference.md`. |

Rationale: deliverables belong in the repo (they are committed, referenced by
lib-tables, and — for facts — are the auditable basis of review/release gates, so
they must travel with the design when someone else takes over); everything else
defaulting into the user's repo is workspace pollution, and everything
project-decisive defaulting OUTSIDE the repo is a reproducibility bug. The
enforcement lives in `tests/test_output_policy.py`: state-root self-ignore,
cache-defaults-outside-CWD, facts-store resolution, and a source census that
whitelists the only permitted CWD-relative default-path literals.

---

### Appendix A — Altium RECORD-ID quick reference (net-bearing marked ★)
`1` Component · `2` Pin★ · `6` Polyline · `15` SheetSymbol★ · `16` SheetEntry★ · `17` PowerPort★ ·
`18` Port★ · `22` No-ERC · `25` NetLabel★ · `27` Wire★ · `29` Junction★ · `34` Designator ·
`41` Parameter (ownerless = **document** parameter → title block) ·
`44/45/46/48` Implementation/Model/Footprint · `47` ImplementationPinAssociation.
A model link (`45/46`) is a **footprint** only when `ModelType` is `PCBLIB` or absent —
`SIM`/`SI`/`PCB3DLib` are other model kinds — and `IsCurrent=T` wins over record order.

### Appendix B — Licensing posture (LOCKED)
- Repo LICENSE = **MIT**. The ported parser logic is **relicensed** (same author) — strip the
  `SPDX-License-Identifier: LicenseRef-Proprietary` header on port; record provenance in the commit/NOTICE.
- altium-mcp is used as an **independent-design reference for high-level patterns only** (
  a protocol-version field, structured `ERROR: CODE` strings). **No schema bytes copied** — our
  `$id`, titles, ERROR enum, and `protocol_version` are original. `THIRD_PARTY_NOTICES.md` credits both
  flaco-source/altium-mcp (2026) and coffeenmusic/Siddharth Ahuja (2025) with full MIT text. The word
  "clean-room" is replaced everywhere by *"independent reimplementation; no source copied; attribution
  retained where structures are referenced."*