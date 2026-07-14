# akcli — Hardened Implementation SPEC (v1.0, FROZEN-on-merge)

> Single source of truth for implementation agents. Resolves the original plan against 9 expert
> reviews. Every CRITICAL/HIGH fix is incorporated. Where reviewers disagreed, the decision and
> rationale are stated inline. Build each file from this document with **no further design decisions**.
>
> **Name cascade (LOCKED):** repo + PyPI dist = `akcli`; import package = `akcli`;
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
        │  ops.py (op-list vocab + validator)   report.py   config.py   units.py        │
        │  errors.py (ERROR codes + exit codes)  safety.py (limits, safe_path, atomic)  │
        └──────────────────────────────────────────────────────────────────────────────┘
            ▲ primitives                         ▲ primitives                    │ op-list
   ┌────────┴─────────┐               ┌──────────┴──────────┐         ┌──────────┴──────────────┐
   │ Altium readers   │               │ KiCad readers       │         │ Executors               │
   │ _cfbf, records,  │               │ sexpr, kicad,       │         │ writers/kicad (xplat)   │
   │ sch, schlib, pcb │               │ kicad_lib           │         │ drivers/altium_live (win)│
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
- **Altium is analysis-only, never written offline.** KiCad gets a full cross-platform writer. Altium
  write/draw only via the optional Windows live driver.
- **Canonical coordinate system:** origin **top-left, +Y down**, unit **mils**, default **50-mil grid**
  (KiCad convention). Every reader normalizes *into* this system on ingest (the Altium reader negates Y).
- **All internal KiCad-writer geometry is integer nanometres** (`1 mil = 25_400 nm`, `1 mm = 1_000_000 nm`);
  convert to string only at serialize time.
- **Every emitted JSON carries `schema_version`; the op-list and bridge carry `protocol_version`.**

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
SCHEMA_VERSION = "1.2"          # stamped on every Schematic/Pcb/Library export
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
    schema_version: str = SCHEMA_VERSION

# --- PCB is a SEPARATE sub-model (footprints/pads/nets, NOT symbol pins) ---
@dataclass
class Footprint:
    designator: str
    footprint_name: str | None
    layer: str | None
    rotation: float = 0.0
    value: str | None = None

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

### 2.2 Op vocabulary (13 LOCKED ops + 3 additive v0.2 ops + 1 additive v0.4 op + 1 additive hierarchical op)

| op | purpose | KiCad writer | Altium live |
|---|---|---|---|
| `place_component` | place a symbol instance | ✅ | ✅ |
| `set_component_transform` | rotate/mirror an existing component | ✅ | ✅ |
| `set_component_parameters` | set ref/value/footprint/params | ✅ | ✅ |
| `add_wire` | draw orthogonal wire (may emit many segments) | ✅ | ✅ |
| `add_junction` | explicit junction dot | ✅ | ✅ |
| `add_no_connect` | NC flag on a pin | ✅ | ✅ |
| `add_net_label` | net label (`scope: local\|global\|hierarchical`) | ✅ | ✅ |
| `place_power_port` | power symbol (sugar: `place_gnd`, `place_vcc`) | ✅ | ✅ |
| `add_bus` | bus polyline | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_bus_entry` | bus entry (fixed 2.54 mm @ 45°) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_text` | free text/note | ✅ | ✅ |
| `delete_component` | remove ALL placed instances of a designator (wires left for the gate to flag; absent target = replay-safe no-op; optional `cascade: true` also removes attached wires/labels/no-connects plus junctions no longer needed by surviving wires) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `delete_object` | remove ONE top-level object by uuid, or via `match: {kind, name?, at?}` selector (exactly-one semantics) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `move_component` | move one instance (designator + optional `unit`); its properties travel along, wires are NOT stretched | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `rename_net` (v0.4) | rewrite matching label texts + power-port net Values (`scope` restricts kind; 0 matches = replay-safe note) | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |
| `add_sheet` | hierarchical child-sheet reference: `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, edge-computed sheet pins (`pins:[{name, type, side, offset_mil}]`); `at`=top-left mils; wires attach to a sheet pin BY COORDINATE (no `Sheet.Pin` endpoint); cross-sheet net = parent sheet-pin ↔ child same-name hierarchical label. Additive, `protocol_version` unchanged | ✅ | ⚠️ `OP_UNSUPPORTED` v1 |

`place_component` additionally takes an optional `"unit": N` (multi-unit parts: each unit is
its own placed instance sharing the designator; `"REF.PIN"` resolves against the instance whose
unit owns the pin, and a pin on an unplaced unit fails loudly).

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
  "protocol_version": 1,
  "target_format": "kicad",
  "target_file": "board.kicad_sch",
  "run_id": "uuid-or-stable-key",
  "ops": [ { "op": "place_component", "...": "..." } ]
}
```

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
    "protocol_version": { "const": 1 },
    "target_format": { "enum": ["kicad", "altium"] },
    "target_file": { "type": "string" },
    "run_id": { "type": "string" },
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
      "oneOf": [
        { "properties": { "op": { "const": "place_component" },
            "lib_id": { "type": "string" }, "designator": { "type": "string" },
            "x_mil": { "type": "number" }, "y_mil": { "type": "number" },
            "rotation": { "$ref": "#/$defs/rotation" }, "mirror": { "$ref": "#/$defs/mirror" },
            "value": { "type": "string" }, "footprint": { "type": "string" },
            "symbol_source": { "type": "string" } },
          "required": ["op", "lib_id", "designator", "x_mil", "y_mil"] },
        { "properties": { "op": { "const": "set_component_transform" },
            "designator": { "type": "string" },
            "rotation": { "$ref": "#/$defs/rotation" }, "mirror": { "$ref": "#/$defs/mirror" } },
          "required": ["op", "designator"] },
        { "properties": { "op": { "const": "set_component_parameters" },
            "designator": { "type": "string" }, "reference": { "type": "string" },
            "value": { "type": "string" }, "footprint": { "type": "string" },
            "parameters": { "type": "object" } },
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
            "orientation": { "$ref": "#/$defs/rotation" } },
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
| `ops.py` | op-list vocab constants, `PROTOCOL_VERSION=1`, hand-rolled validator | `PROTOCOL_VERSION:int`; `OP_NAMES:frozenset`; `validate_oplist(doc:dict)->list[OpError]`; `load_oplist(path)->dict`; `Op` typed accessors | `errors`, `schemas/*` (read at import) | Zero-dep structural validator mirroring `ops.schema.json` (`jsonschema` only in dev); rejects free angles, odd wire arrays, unknown ops with `ERROR` codes | `test_ops.py` |
| `errors.py` | ERROR-code registry + exit-code table + `AkcliError`; top-level `as_error()` wrapper | `class AkcliError(Exception)`; `EXIT:dict[str,int]`; `ERROR_CODES:frozenset`; `def fail(code:str,msg:str)->NoReturn`; `def to_exit(exc)->int` | stdlib only | Codes: `ALTIUM_BAD_MAGIC, ALTIUM_FAT_CYCLE, ALTIUM_OOB_SECTOR, ALTIUM_BAD_SECTOR_SHIFT, ALTIUM_ALLOC_GUARD, ALTIUM_MALFORMED, ALTIUM_UNSUPPORTED, KICAD_SEXPR_DEPTH, KICAD_SEXPR_UNTERMINATED, KICAD_SEXPR_TOOBIG, SYMBOL_NOT_FOUND, BAD_ANGLE, NON_ORTHOGONAL_WIRE, OFF_GRID, OVERLAP, VERIFY_FAILED, OP_UNSUPPORTED, HIERARCHICAL_UNSUPPORTED, PROTOCOL_MISMATCH, TARGET_LOCKED, PATH_OUTSIDE_ROOT, KICAD_CLI_TIMEOUT, KICAD_CLI_MISSING, BINFETCH_DOWNLOAD, BINFETCH_CHECKSUM, BAD_CONFIG`. `TARGET_LOCKED` (→ exit 6) is raised by the KiCad writer when a `~<name>.lck` GUI lock is present and `--allow-open` was not passed. Exit table: §8 | `test_errors.py` |
| `safety.py` | hard limits + safe IO helpers used everywhere | `MAX_FILE_BYTES, MAX_SECTORS, MAX_RECORDS, MAX_DIR_ENTRIES, MAX_DECODED_BYTES, MAX_SEXPR_DEPTH, MAX_ATOM_BYTES, MAX_NODES`; `safe_path(base,cand)->Path`; `run_subprocess(argv,timeout,maxout)->CompletedProcess`; `atomic_write_with_backup(path,data,backup_dir)->None` | stdlib `os,subprocess,resource,signal,pathlib,shutil` | `safe_path`: realpath both, reject escapes/symlinks, never expand env from untrusted files; `run_subprocess`: `shell=False`, abs exe, `--` before paths, timeout, output cap; `atomic_write`: snapshot→temp-in-same-dir→fsync→`os.replace` | `test_safety.py`, `test_fuzz_safety.py` |
| `units.py` | all coordinate conversions + grid + tolerance | `ALTIUM_SCH_MIL_PER_UNIT=10.0`; `MIL_PER_MM=1/0.0254`; `NM_PER_MIL=25400`; `NM_PER_MM=1_000_000`; `altium_to_mil(i,frac)->float`; `mil_to_nm(m)->int`; `nm_to_mm_str(nm)->str`; `snap_mil(m,grid=50)->float`; `approx_eq(a,b,tol_nm)->bool` | stdlib | `nm_to_mm_str` strips trailing zeros/dot (KiCad float style); integer-nm math only | `test_units.py` |
| `config.py` | discover + parse + validate `akcli.toml` | `find_config(start:Path)->Path\|None`; `load_config(path)->Config`; `class Config` (`mcu_designator, rails:list, paths:dict, erc_waivers:list`) | `tomllib`, `errors`, `safety` | walk-up discovery from cwd; `--config` override; paths resolve relative to toml dir; reject unknown keys → `BAD_CONFIG`; schema in §3.11 | `test_config.py` |
| `report.py` | render findings + metadata caveats; text + `--json` | `render(findings,fmt,meta)->str`; `class Finding(code,severity,message,refs)`; `Severity` enum | `model`, `errors` | Always prints metadata header: passive-pin ratio, No-ERC suppressed count, unnamed-net count, frac-coord presence — so a vacuous pass is never read as clean | `test_report.py` |
| `__main__.py` | `python -m` entry | `from .cli import main; raise SystemExit(main())` | `cli` | thin shim only | covered by `test_cli.py` |
| `cli.py` | argparse dispatch, exit codes, global flags | `def main(argv=None)->int`; subcommands `read net component check diff pinmap expected ops calc plan draw export jlc` | everything | global `--version` (pkg + protocol), `-C/--config`, `-v/-vv/--quiet`, `--json`, `--no-color`, `--debug`; `draw` defaults `--dry-run`, needs `--apply` to write; stdout = data, stderr = logs | `test_cli.py` |

### 3.2 Altium readers (`src/akcli/readers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `_cfbf.py` | **hardened** OLE2/CFBF reader; path-qualified directory tree walk | `read_streams(path_or_bytes)->dict[str,bytes]`; `read_streams_qualified(...)->dict[str,bytes]` (e.g. `"Components6/Data"`) | `struct`, `safety`, `errors` | **Hardened** OLE2/CFBF parsing **with**: `len>=512` guard; assert `ssz_shift∈{9,12}`, `msz_shift==6`, `mini_cutoff==4096`; cycle-detected `chain/read_mini` (seen-set + `MAX_SECTORS` cap) → `ALTIUM_FAT_CYCLE`; range-check every `off(s)` → `ALTIUM_OOB_SECTOR`; **walk red-black dir tree (Child/Left/Right) for path-qualified, non-colliding names** (fixes 41× `Data`/51× `Header`); guard missing root-storage; DIFAT-spillover (>109) walked under declared-count + cycle + sector-cap guards | `test_cfbf.py`, `test_fuzz_safety.py` |
| `altium_records.py` | record framing + field tokenizer + `%UTF8%` + `_Frac` + electrical map + RECORD-ID constants | `parse_records(buf,drop_header:bool)->list[dict]`; `fields(r)->dict`; `gi(d,k,default)`; `coord(d,key)->float` (assembles int+frac→mil); `RECORDS:dict` | `units`, `model`, `safety`, `errors` | Port of lines 109–130 **with**: `%UTF8%`-prefixed keys decoded UTF-8 (re-encode latin1→bytes→utf-8); `_Frac` companion assembled via `units.altium_to_mil`; `Electrical`→`PinType`; **header detection** (drop leading record only if it's the schematic HEADER, not unconditional `[1:]`) to fix SchLib/PcbDoc OwnerIndex base; record/byte caps → `MAX_RECORDS`. RECORD IDs: 1=Component,2=Pin,6=Polyline,15=SheetSymbol,16=SheetEntry,17=PowerPort,18=Port,22=NoERC,25=NetLabel,27=Wire,29=Junction,34=Designator,41=Parameter,44/45/46/48=Implementation | `test_altium_records.py` |
| `altium_sch.py` | `.SchDoc` → `Schematic` (READ-ONLY) | `read(path)->Schematic`; `read_primitives(path)->NetPrimitives` | `_cfbf`, `altium_records`, `model`, `netbuild`, `units` | Extract: RECORD 1 placement/orientation/mirror/UniqueID; RECORD 2 pins with `Electrical`, `OwnerPartId`, tip = `Location + PinLength*dir` (negate Y into canonical); RECORD 34 designators (synthesize `$U<idx>` when missing — never drop); RECORD 41 params (value/comment); RECORD 45/46 footprint; RECORD 27 wires, 29 junctions, 22 No-ERC, 25/17 labels+power ports → emit `NetPrimitives` → `netbuild.build_nets()` | `test_altium_sch.py` |
| `altium_schlib.py` | `.SchLib` → `Library` | `read(path)->Library` | `_cfbf`, `altium_records`, `model` | Use `read_streams_qualified` — each symbol in its own storage `Data` stream; per-stream OwnerIndex base (no blind `[1:]`); refuse binary records (4th length byte ≠ 0) loudly | `test_altium_schlib.py` |
| `altium_pcb.py` | `.PcbDoc` → `Pcb` (ASCII sections + binary copper) | `read(path)->Pcb` | `_cfbf`, `altium_records`, `altium_pcb_bin`, `model` | ASCII `|KEY=VAL|` sections `Nets6`/`Components6`/`Classes6`/`Rules6` plus binary `Tracks6`/`Vias6`/`Arcs6`/`Pads6` via `altium_pcb_bin`. **Guard:** still refuse to ASCII-parse a Header-declared binary section; `Fills6/Regions6/Texts6/Polygons6` remain deferred | `test_altium_pcb.py` |
| `altium_pcb_bin.py` | binary `.PcbDoc` section decoders | `parse_tracks/parse_vias/parse_arcs/parse_pads(buf,nets)->list[dict]`; `UNIT_MIL` | `errors` | Framing `u8 type + u32 LE len + payload`; coords 1/10000 mil → emit mils, native +Y-up frame; nets by index into `Nets6` order (0xFFFF→None). Track: layer u8@0, net u16@3, comp u16@7, x1/y1/x2/y2 s32@13.., w s32@29. Via: x/y@13/17, dia@21, hole@25, layers u8@29/30. Arc: center@13/17, r@21, angles f64@25/33 (deg), w@41. Pad: block-structured (name pascal block + 3 reserved + geometry ≥61B: size@21/25, hole@45, shape u8@49, rot f64@52). Unknown type→`ALTIUM_UNSUPPORTED`, truncation→`ALTIUM_MALFORMED`. Layouts cross-validated item-by-item vs KiCad's pcbnew importer on real boards (778/778 tracks, 20/20 vias, 236/236 arcs, 48/48 pads) | `test_altium_pcb_bin.py` |

### 3.3 Net inference (`src/akcli/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `netbuild.py` | format-agnostic net inference (the STAT fix) | `build_nets(prims:NetPrimitives)->list[Net]` | `model`, `units` | **Pipeline (LOCKED):** (1) exact-integer geometric union-find on wire segments (coords in fine-int from `_Frac`); (2) union each **junction(29)** point onto every segment it lies on; (3) **T-junction**: union every wire vertex lying on another wire's mid-span; (4) union pins/labels lying on a segment (`on_seg`, exact integer cross-product) — **pins connect at segment endpoints or junction-marked points only** (a bare mid-span touch does not connect: eeschema's rule, and Altium's editor inserts a junction record for every pin tap); labels connect anywhere along the wire; (5) **GLOBAL same-name merge:** group all label/power-port names per component, `name→set(roots)`, union every pair sharing any name — *this stitches the two STAT clusters and aliases STAT≡LED1_GPIO_RD*; (6) naming priority: power-port > net-label > auto, honor `power_priority`; keep all names as `aliases`, emit confidence<1 + a NOTE on multi-name nets; (7) **stable synthetic ids from sorted membership**, never `N$x_y`; (8) record `merge_reasons` for explainability; (9) multi-sheet: union by Port/SheetEntry name (net labels are sheet-local, power ports global) | `test_netbuild.py` (+ STAT/alias, junction, T-junction, NoERC, two-same-name-GND fixtures) |

### 3.4 KiCad readers (`src/akcli/readers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `sexpr.py` | iterative, depth/size-capped S-expr tokenizer+parser (shared by readers + writer) | `parse(text:str)->SNode`; `dumps(node:SNode)->str`; `class SNode` (preserves untouched atom text + child order) | `safety`, `errors` | **Explicit-stack** (no recursion; `setrecursionlimit` banned); enforce `MAX_SEXPR_DEPTH/MAX_ATOM_BYTES/MAX_NODES`; bounded quote/escape scan; unterminated/over-deep → `KICAD_SEXPR_*`. `SNode` keeps original token text so untouched nodes reserialize byte-identical | `test_sexpr.py`, `test_fuzz_safety.py` |
| `kicad.py` | `.kicad_sch`/`.kicad_pcb` → `Schematic`/`Pcb` | `read_sch(path)->Schematic`; `read_pcb(path)->Pcb`; `read_primitives(path)->NetPrimitives` | `sexpr`, `kicad_lib`, `model`, `netbuild`, `units` | Resolve **pin electrical types from `lib_symbols` at read time** (KiCad pins on instances carry no type); parse `(wire)`,`(junction)`,`(no_connect)`,`(label/global_label/hierarchical_label)`,`(symbol …)` with `(instances)` refdes; **recurse into `(sheet …)` children** (per-instance namespaces; designators from the matching instances path; sheet-pin↔hier-label pairs via synthetic never-naming "hier" connectors; cycle/depth-guarded); coords mm→mil (no Y flip; KiCad already +Y down); → `NetPrimitives` → `netbuild` | `test_kicad_reader.py` |
| `altium_prj.py` | `.PrjPcb` project → top-sheet `Schematic` | `read(path)->Schematic`; `read_project(path)->dict` | `altium_sch`, `errors` | INI-ish parse (configparser, strict=False); document list (backslash paths resolved), `PowerPortNamesTakePriority` → `NetPrimitives.power_priority`; top sheet = the schematic no sheet symbol references | `test_altium_hierarchy.py` |
| `kicad_lib.py` | `.kicad_sym` + inline `lib_symbols` → `Library` | `read(path)->Library`; `resolve(lib_id, sources)->SymbolDef`; `pin_offsets(sym)->list` | `sexpr`, `model` | Resolve `(extends ...)` (load base); keep `body_sexpr` for writer's lib_cache; pin offsets in symbol-local coords for world-coord computation | `test_kicad_lib.py` |

### 3.5 KiCad writer (`src/akcli/writers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `geometry.py` | coord/transform core (the module that makes wires hit pins) | `pin_world(sym,inst,pin)->tuple[int,int]` (nm); `transform_point(pt,rot,mirror,origin)->...`; `grid_snap_nm(pt,grid)->...`; `mil_to_nm/nm_to_mm_str` re-exports | `units`, `model` | Integer-nm; rotation 0/90/180/270 + mirror (x/y) matrices, **rotate-then-mirror**, Y-down; transform child `(property)` positions/angles too | `test_geometry.py` |
| `sexpr_writer.py` | KiCad-faithful serializer | `serialize(node:SNode)->str` | `sexpr`, `units` | 2-space indent, KiCad string quoting/escaping, float via `nm_to_mm_str`, preserve untouched-node text + child order. **Gate:** byte-identical no-op round-trip | `test_roundtrip_byte_identity.py` |
| `lib_cache.py` | `lib_symbols` cache resolution & copy | `ensure_cached(doc,lib_id,sources)->None`; `SYMBOL_NOT_FOUND` on miss | `kicad_lib`, `sexpr`, `errors`, `config` | Requalify parent → `Nick:Name`, keep child unit names unqualified (`Name_0_1`); **flatten** an `(extends)`-derived symbol into one standalone entry (base inlined under the derived name, units renamed, derived properties overlaid, `extends` dropped — KiCad's own save behavior; its loader will not resolve a bare `(extends "Base")` against a qualified cached base); **dedup by lib_id**; copy full pin electrical types (ERC needs them); symbol source = config `.kicad_sym` paths and/or template `.kicad_sch` | `test_lib_cache.py` (incl. `C_Polarized` extends fixture) |
| `instances.py` | refdes/instances/sheet-path + `#PWR` allocation | `write_instance(doc,sym,ref,path)->None`; `alloc_pwr_ref(doc)->str`; `instances_path(doc,sheet)->str` | `sexpr`, `errors` | Write BOTH `(property "Reference")` AND `(instances (project … (path … (reference …)(unit …))))`, in sync; derive project name + root-sheet uuid; flat-only v1 (sub-sheet → `HIERARCHICAL_UNSUPPORTED`); `#PWR0xx` unique alloc; **deterministic UUIDv5** = `uuid5(sheet_uuid, designator+":"+op_index)` for idempotency | `test_instances.py` |
| `connectivity.py` | pure-Python ERC-lite — **primary** post-write gate | `verify(doc)->list[Finding]`; `auto_junctions(doc)->None` | `model`, `geometry`, `errors` | Exact-coincidence of every new wire endpoint vs pin/label/junction/port; auto-insert `(junction)` at 3+ way meets; honor `(no_connect)`; duplicate-UUID + unresolved-lib_id + invalid-instances-path checks. **Runs with no KiCad installed** | `test_writer_connectivity.py` |
| `kicad.py` | op-list executor → surgical `.kicad_sch` edits | `apply(oplist:dict,path:str,apply:bool)->list[OpResult]` | all writers + `ops`,`safety`,`errors` | Per op: snap pin-ref endpoints to `pin_world`; emit per-pin `(pin "N" (uuid …))`; **atomic write w/ backup** (snapshot→temp→fsync→re-parse+connectivity verify on TEMP→`os.replace` only on pass); mtime/hash optimistic lock; reject op-list with higher major `protocol_version` → `PROTOCOL_MISMATCH`; `--dry-run` emits ops+verify, no write | `test_kicad_writer.py` |

### 3.6 Checks (`src/akcli/checks/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `erc.py` | electrical rule checks | `run(sch:Schematic,cfg:Config)->list[Finding]` | `model`, `report`, `config` | **Net-name-based power/ground detection** (not electrical-type — real boards are 292/298 Passive): nets touching a power port or matching rail-name set = power/ground; "IC has power+ground" tests net identity. Driver-conflict/floating gated behind **type-confidence** (fraction non-Passive) and downgraded when degenerate. Honor No-ERC suppression set (geo-match within grid tol) + config `erc_waivers`. Net-alias conflicts → NOTE (e.g. `STAT==LED1_GPIO_RD`) | `test_erc.py` |
| `power.py` | rail enumeration + decoupling heuristic | `run(sch,cfg)->list[Finding]` | `model`, `config` | Enumerate rails from power ports + config `rails`; list consumers; decoupling-cap heuristic per IC power net; optional current budget if BOM annotated; rail voltage sanity vs config | `test_power.py` |
| `bom.py` | BOM hygiene | `run(sch)->list[Finding]` | `model` | Refdes parsed as `(alpha-prefix, opt-int-suffix)`; gap-detection **only within a numeric-suffixed prefix**; skip compound refs (`J_USB_C`,`X3`); dedup duplicate-designator by UniqueID/part; missing value/footprint | `test_bom.py` |
| `diff.py` | net-level v1↔v2 diff | `run(a:Schematic,b:Schematic)->DiffReport` | `model` | **Match nets by membership** (Jaccard bipartite), NOT display name; components by **UniqueID** (Altium↔Altium) then `(value,footprint,pin-count)` signature, then refdes; report name-vs-membership changes separately; document low-confidence for cross-revision | `test_diff.py` |
| `pinmap.py` | MCU pin→net + optional cross-check (GENERIC) | `run(sch,cfg,expected:dict\|None)->list[Finding]` | `model`, `config` | Generic only: emit MCU `pin→net`; cross-check against an **external expected pin→signal table** (CSV/JSON passed in). **No DTS/pinout parsing here** — that lives in `adapters/` (keeps engine reusable). Pin-name `Pn.mm` parser; schematic authoritative, expected-table advisory | `test_pinmap.py` |
| `intent.py` | design-intent net-membership assertions | `snapshot(sch)->dict`; `run(sch,spec)->list[Finding]` | `model`, `report` | Match declared `REF.PIN` members to actual nets by **pin membership** (not name); `exact`/`subset` modes + `fnmatch` refs; `INTENT_*` findings | `test_checks_intent.py` |
| `contract.py` | design contracts (topology/semantic policy ERC can't express) | `load(path)->dict`; `run(sch,doc)->list[Finding]` | `model`, `report`, `errors` | TOML `[[contract]]`: `require`/`forbid` pin-net, `require_same_net`/`forbid_same_net` pin pairs, `component`+`value`, `nc`; pin spec resolves number then name; `waived` exceptions w/ owner/expiry; PASS/FAIL/WAIVED all explicit, expired → `CONTRACT_EXCEPTION_EXPIRED` | `test_contract.py` |
| `schpcb.py` | schematic ↔ PCB equivalence | `run(sch:Schematic,pcb:Pcb)->list[Finding]` | `model`, `report` | Refdes presence + footprint/value assignment + pad-level **net partition** (names untrusted); `SCHPCB_NET_SPLIT`/`_MERGE`/`_PAD_MISSING`/`_FOOTPRINT_MISMATCH`; `#`-prefixed pseudo-components excluded | `test_schpcb.py` |

### 3.6b Design integrity (`src/akcli/` — library workspace, fab policy, footprint libs)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `libtable.py` | project library workspace: `sym-lib-table`/`fp-lib-table` audit + repair plans | `discover(project)->Workspace`; `audit(ws,sch?)->list[Finding]`; `plan_rename(ws,old,new)`; `plan_model_paths(ws,mode)` | `model`, `readers.footprint_lib`, `readers.sexpr`, `report` | Cross-check schematics ↔ tables ↔ library contents ↔ 3D; `FOOTPRINT_LIB_UNREGISTERED`/`FOOTPRINT_MISSING`/`LIB_URI_UNRESOLVED`/`MODEL_MISSING`/`FOOTPRINT_LEGACY_FORMAT`; repairs = lossless S-expr rewrites (dry-run→apply) | `test_library_workspace.py` |
| `fab.py` | versioned fab profile + order-manifest checks | `load_profile(path)`; `check(pcb,profile)->list[Finding]`; `load_order`/`check_order`; `explain(code,profile?)` | `model`, `report`, `errors` | TOML profile (mandatory `[source]` urls); via geometry/tenting/via-in-pad (w/ `thermal_via` exceptions)/stackup/cost thresholds; severity policy error/warning/note; order manifest = declared intent, never guessed | `test_fab.py` |
| `readers/footprint_lib.py` | `.PcbLib`/`.kicad_mod`/`.pretty` → `Library` w/ `FootprintDef`/`FootprintPad` | `read_pcblib(path)`; `read_kicad_mod(path)`; `read_pretty(path)` | `model`, `readers._cfbf`, `readers.sexpr`, `readers.altium_pcb_bin` | PcbLib pad framing shares `altium_pcb_bin` offsets; +Y-up→+Y-down flip; undecoded primitives → `UNSUPPORTED_PRIMITIVE` warnings; cross-validated vs KiCad's Altium importer | `test_footprint_lib.py` |
| `writers/footprint_mod.py` | `FootprintDef` → `.kicad_mod` text (PcbLib import target) | `to_kicad_mod(fp,*,courtyard_mm?,warnings?)->str` | `model` | Pads verbatim (never recomputed); optional declared pad-bbox courtyard; deterministic output | `test_library_workspace.py` |
| `commands/library.py` | `akcli library audit`/`repair`/`import-altium` | `register(sub,common)` | `libtable`, `readers.footprint_lib`, `writers.footprint_mod`, `safety` | `import-altium` writes `.kicad_mod` + `provenance.json` (source sha256, converter version, declared transforms); repair is atomic w/ `.bak` + re-audit | `test_library_workspace.py` |
| `commands/fab.py` | `akcli fab check`/`explain` | `register(sub,common)` | `fab`, `readers.kicad` | deep-reads `.kicad_pcb`, runs profile + optional order manifest; lint-style exit | `test_fab.py` |
| `commands/release.py` | `akcli release preflight` — run all gates, emit manifest | `register(sub,common)` | `checks.*`, `libtable`, `fab`, `readers.kicad`, `safety` | Gates check/intent/contract/library/sch-pcb/fab/order/git; absent input → `skipped` w/ reason; manifest binds input sha256 + git rev + findings; dirty worktree fails unless `--allow-dirty` | `test_release_preflight.py` |

### 3.7 Drivers (`src/akcli/drivers/`)

| File | Purpose | Public API | Imports | Algorithm notes | Tested by |
|---|---|---|---|---|---|
| `kicad_cli.py` | optional secondary verify wrapper | `available()->bool`; `version()->tuple\|None`; `erc(path)->dict\|None`; `netlist(path)->dict\|None` | `safety`, `errors` | `shutil.which`-gated; `sch erc --format json` (≥8), fall back `export netlist` (7); **never** pass `--exit-code-violations` (erc exits 0 even with violations); nonzero/crash = our write bug; absence non-fatal (connectivity.py is primary) | `test_kicad_cli.py` (skipif no `kicad-cli`) |
| `jlc2kicad.py` | LCSC→KiCad library conversion (in-process) | `convert(lcsc,out_dir,*,with_3d,lib_name,footprint_lib,model_path,force)->ConvertResult` | `_vendor.jlc2kicadlib` | Orchestrates the vendored converter: resolve CAD uuids via EasyEDA `products/<C>/svgs`, then vendored `create_footprint`/`create_symbol`; `footprint_lib` = output dir **and** the fp-lib-table nickname stamped into the symbol's Footprint field; `model_path` ∈ `relative`/`absolute`/`${VAR}` for the 3D reference; error mapping `NETWORK`/`CONVERT_PART_NOT_FOUND`/`CONVERT_FAILED`/`CONVERT_NO_ARTIFACTS`; networked (the only one besides `jlc search/show`) | `test_jlc2kicad.py` (offline fixtures, injected transport) |
| `altium_live/bridge.py` | optional Windows file-based JSON bridge (offline-unit-testable) | `send(op:dict,reqdir:Path,timeout)->dict`; `ping()->dict` | `safety`, `ops`, `errors` | atomic `request.json.tmp`→rename; poll `response.json` every 200 ms; `.lock` single-flight; `altium_ping` handshake returns `{protocol_version,altium_version}`; reject `protocol_version` mismatch → `PROTOCOL_MISMATCH`; per-run unique 0700 dir, `O_NOFOLLOW`. **Offline test mocks response.json** | `test_bridge.py` |
| `altium_live/scripts/altium_api.pas` | DelphiScript half (Windows + Altium 22+) | (Altium scripting entry) | — | **Clean-room** from Altium's public scripting API + documented method; reads request.json, drives running Altium, writes response.json. **Validated only on user's Windows box** | manual (Windows) |
| `altium_live/scripts/altium_api.PrjScr` | Altium script project wrapper | — | — | pairs with `.pas` | manual (Windows) |


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
| `opsmap.py` | calc→op-list bridge (`--ops`): design calcs → `place_component` ops with E-series values | `MAPPABLE`, `to_oplist(name, envelope)->dict` | protocol-1 op-list; schema-validated in tests | idem |

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
| `.gitattributes` | binary/text fixture rules | §6 — `tests/fixtures/** binary`, golden/`*.kicad_sch text eol=lf` | implicit (Windows CI) |
| `.github/workflows/ci.yml` | CI matrix + validators | §6.4 | self |
| `examples/akcli.toml.example` | tested reference config incl. `[[erc_waiver]]` | §3.11 | `test_config.py` |
| `LICENSE` | MIT (repo) | — | — |
| `THIRD_PARTY_NOTICES.md` | MIT attribution chain for altium-mcp **patterns** | credits flaco-source (2026) + coffeenmusic/Siddharth Ahuja (2025) | — |
| `SECURITY.md` | untrusted-input threat model + enforced limits | — | — |
| `CHANGELOG.md` | SemVer + protocol_version policy | — | — |
| `README.md`, `INSTALL.md` | docs + install UX | — | link-check (optional) |
| `docs/config-schema.md`, `docs/cli-reference.md`, `docs/op-list-authoring.md`, `docs/op-capability-matrix.md` | contracts | — | — |

### 3.11 Config schema (`akcli.toml`)

```toml
[project]
mcu_designator = "U3"

[[rail]]
name = "V3V3"
voltage = 3.3
tolerance_pct = 5

[paths]
schematic = "hardware/main.SchDoc"   # resolved relative to THIS file's dir
dts       = "firmware/board.dts"
pinout_md = "docs/pinout.md"

[[erc_waiver]]
net    = "LED1_GPIO_RD"
rule   = "driver_conflict"
reason = "LED1 shares MCP73831 open-drain STAT (P0.25) by design; FW reads it as input"
```

`config.load_config` rejects unknown keys → `BAD_CONFIG`; discovery walks up from cwd; `-C/--config` overrides.

### 3.12 Schemas (`schemas/`)

| File | Purpose |
|---|---|
| `ops.schema.json` | §2.5 — own `$id` namespace, own ERROR enum, `protocol_version` const |
| `ops.capabilities.json` | per-op executor support matrix (KiCad writer vs Altium live) |
| `schematic.schema.json` | `Schematic` export shape + `schema_version` |
| `netlist.schema.json` | net membership + Altium net-naming rules (same-name merge, priority, single-pin gating) |

---

## 4. PARALLEL OWNERSHIP GROUPS (zero file overlap)

**FOUNDATION (Group F) — authored FIRST and FROZEN before any other group starts.** No other group may edit
these; they are the contract everyone codes against:
`model.py, ops.py, errors.py, safety.py, units.py, config.py, report.py, __main__.py`,
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
| **D — Drivers** | `drivers/kicad_cli.py, drivers/altium_live/*` + tests | F (ops, errors) | F frozen |
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
  "description": "AI-native KiCad design agent (zero-dependency Python CLI + Claude Code plugin): author and edit .kicad_sch from JSON op-lists, run ERC/design/intent/BOM checks, simulate on KiCad's bundled ngspice, source parts, and import Altium .SchDoc/.SchLib/.PcbDoc — no Altium or KiCad install required.",
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
      "description": "AI-native KiCad design agent (zero-dependency Python CLI + Claude Code plugin): author and edit .kicad_sch from JSON op-lists, run ERC/design/intent/BOM checks, simulate on KiCad's bundled ngspice, source parts, and import Altium .SchDoc/.SchLib/.PcbDoc — no Altium or KiCad install required.",
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
[ -n "$PY" ] || { echo "akcli requires Python >=3.11 (none found). Install python@3.11+ or: pipx install akcli" >&2; exit 1; }
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
- **Env-gated real-file test** `test_integration_real.py` (`AKCLI_ALTIUM_SAMPLE` path; **skips** when unset):
  asserts only **generic invariants** (parses; components>0; every named net ≥1 member; deterministic across
  two runs; JSON round-trips + validates against `netlist.schema.json`). **No project-specific net/pin
  names** in the public repo — those expectations live in a project-specific adapter behind `AKCLI_ALTIUM_EXPECT`
  (project-specific, non-committed JSON). **Never snapshots.**

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
- `.gitattributes` marks fixtures binary, golden/`.kicad_sch` text `eol=lf`; `MANIFEST.sha256` verified in CI
  to catch Windows-checkout corruption.

### 6.4 CI matrix
- **Lint/unit/fixture jobs:** OS = {ubuntu, macos, windows} × Python = {3.11, 3.12, 3.13}. Runtime is
  zero-dep; tests use `pip install -e .[dev]` (`pytest`; `jsonschema` dev-only for schema tests).
- **KiCad-cli job:** single dedicated entry **ubuntu + pinned KiCad 8.x** (heavy install; pin one major).
  Everywhere else `shutil.which`-skipif. A "no kicad-cli" job proves graceful degradation.
- **Manifest/packaging jobs:** `jq . hooks/hooks.json`; `claude plugin validate . --strict` (marketplace) +
  a separate plugin-component validation/load smoke (copy tree minus marketplace.json, validate); `test -x
  bin/akcli`; `akcli --help` from a clean checkout; `python -m build && twine check dist/*`;
  `tools/sync_version.py --check`.

---

## 7. Packaging metadata (`pyproject.toml`)
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "akcli"
description = "AI-native KiCad design agent: draw and edit .kicad_sch from JSON op-lists with net-diff safety rails, run ERC/design/intent checks, simulate on bundled ngspice, source parts, and import Altium .SchDoc/.PcbDoc — no EDA install required."
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
  "Operating System :: OS Independent",
]

[project.scripts]
akcli = "akcli.cli:main"   # doc-noqa (pyproject entry point, not a CLI command)

[project.optional-dependencies]
dev = ["pytest>=8", "jsonschema>=4", "build", "twine"]

[tool.setuptools.packages.find]
where = ["src"]
```
> The EDA classifier string is exact-trove (validated by `twine check` in CI).

---

## 8. Risk register + platform validity

**Exit-code table (`errors.EXIT`):** 0 success/no findings · 1 check findings present · 2 usage/arg error ·
3 parse error (corrupt OLE2/sexpr) · 4 file not found · 5 unsupported format · 6 op-list/verify failure ·
7 external tool missing. `check` is lint-style (non-zero on findings) with `--exit-zero` for report mode.

| # | Risk | Mitigation | macOS-validatable? |
|---|---|---|---|
| 1 | Net-inference defect (STAT/LED1 not merged) ships as "expected" | Rebuild net layer in `netbuild.py`; hand-authored golden; STAT regression fixture gates checks | ✅ fully (synthetic) |
| 2 | Multi-storage OLE collapse (.SchLib/.PcbDoc) | `_cfbf.read_streams_qualified` walks dir tree; per-storage tests | ✅ |
| 3 | Pin types absent/Passive → vacuous ERC pass | net-name-based power/ground + type-confidence gating + report caveat | ✅ |
| 4 | Unbounded FAT/miniFAT loops, header allocation bombs | `safety` caps + cycle detection + header validation; `test_fuzz_safety` | ✅ |
| 5 | S-expr recursion → uncatchable SIGSEGV | iterative parser, depth/atom/node caps, ban `setrecursionlimit` | ✅ |
| 6 | KiCad wires don't connect (geometry/units) | integer-nm math, pin-world snapping, `connectivity.verify` primary gate, byte-identity gate | ✅ (no KiCad needed for primary gate) |
| 7 | Non-deterministic UUIDs → non-idempotent writes | UUIDv5 from sheet-uuid+designator+op-index | ✅ |
| 8 | `(instances)` path / `#PWR` wrong → empty netlist / R? | write both legacy + instances path; flat-only v1 | ✅ |
| 9 | `bin/akcli` `$CLAUDE_PLUGIN_ROOT` empty / python 3.9 | self-locating wrapper + interpreter discovery | ✅ |
| 10 | DIFF noise (coordinate-named nets) | match by membership/UniqueID, never display name | ✅ |
| 11 | Licensing: proprietary header + false "clean-room" | relicense parser to MIT (strip `LicenseRef-Proprietary`); own schema namespace; `THIRD_PARTY_NOTICES` crediting altium-mcp chain | ✅ |
| 12 | Path traversal via lib_id / config / bridge | `safety.safe_path` allowlist; per-run 0700 bridge dir, `O_NOFOLLOW` | ✅ |
| 13 | Destructive write corrupts user schematic | snapshot+temp+fsync+verify-temp+`os.replace`, mtime lock, `--apply` required | ✅ |
| 14 | kicad-cli erc exit-code misread | never pass `--exit-code-violations`; parse JSON; advisory only | ⚠️ needs kicad-cli (CI ubuntu only) |
| 15 | PCB binary sections (Pads/Tracks) | `altium_pcb_bin` decodes Tracks6/Vias6/Arcs6/Pads6; cross-validated vs KiCad's pcbnew importer on real boards; fills/regions/texts/polygons still guarded/deferred | ✅ (read + verify) |
| 16 | **Altium authoritative netlist / ERC / live write** | optional Windows live driver; python `bridge.py` offline-unit-testable with mocked response | **❌ Windows + Altium 22+ only** |
| 17 | DelphiScript half (`altium_api.pas/.PrjScr`) | scaffolded + iterated on user's Windows box | **❌ Windows-only/unvalidatable here** |
| 18 | Multi-sheet/hierarchical merge unvalidated (no real fixture) | synthetic multi-sheet fixtures + loud caveat | ✅ synthetic only |
| 19 | Version drift across 3 manifests | pyproject is SoT; `tools/sync_version.py` + CI check | ✅ |
| 20 | Repo public over-promises | Roadmap section; public claims match shipped features | ✅ |

**Summary of platform split:**
- **Fully buildable + validated on macOS/Linux/CI:** all Altium **read/analyze**, net inference, all checks,
  report, op-list + validator, KiCad **read + write/draw** (primary connectivity gate is pure Python), CLI,
  plugin, docs, safety/fuzz, `bridge.py` offline unit tests.
- **Cross-platform but needs `kicad-cli` (CI ubuntu + KiCad 8):** the *optional secondary* ERC verify.
- **Windows + Altium 22+ only (cannot be validated from this macOS session):** Altium authoritative
  netlist/ERC and the live write/draw driver (`altium_api.pas`/`.PrjScr`).

---

## 9. Build order

**Frozen-first set (author + freeze before anything else):** `model.py, ops.py, errors.py, safety.py,
units.py, config.py, schemas/*.json, pyproject.toml, .claude-plugin/plugin.json,
.claude-plugin/marketplace.json, bin/akcli, hooks/hooks.json` + the fixture generators. Everything else codes
against these signatures and never edits them. After the foundation, the ownership groups in §4 proceed in
the dependency order of that section's DAG.

---

### Appendix A — Altium RECORD-ID quick reference (net-bearing marked ★)
`1` Component · `2` Pin★ · `6` Polyline · `15` SheetSymbol★ · `16` SheetEntry★ · `17` PowerPort★ ·
`18` Port★ · `22` No-ERC · `25` NetLabel★ · `27` Wire★ · `29` Junction★ · `34` Designator ·
`41` Parameter · `44/45/46/48` Implementation/Model/Footprint.

### Appendix B — Licensing posture (LOCKED)
- Repo LICENSE = **MIT**. The ported parser logic is **relicensed** (same author) — strip the
  `SPDX-License-Identifier: LicenseRef-Proprietary` header on port; record provenance in the commit/NOTICE.
- altium-mcp is used as an **independent-design reference for high-level patterns only** (file-based JSON
  bridge, a protocol-version field, structured `ERROR: CODE` strings). **No schema bytes copied** — our
  `$id`, titles, ERROR enum, and `protocol_version` are original. `THIRD_PARTY_NOTICES.md` credits both
  flaco-source/altium-mcp (2026) and coffeenmusic/Siddharth Ahuja (2025) with full MIT text. The word
  "clean-room" is replaced everywhere by *"independent reimplementation; no source copied; attribution
  retained where structures are referenced."*