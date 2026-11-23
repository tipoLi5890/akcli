"""LOCKED normalized data model (SPEC §1.3).

One normalized model for both Altium and KiCad. All readers emit
``Schematic`` / ``Pcb`` / ``Library``; all checks, ops, report and the CLI are
format-agnostic. Readers additionally emit ``NetPrimitives`` (wires/junctions/
labels/power ports/pins) which the shared ``netbuild`` turns into ``Net[]`` so
the net-inference logic (incl. the STAT/LED1 same-name merge fix) is written once.

The Altium/KiCad pin-type maps live here as the single source of truth for both
the readers and ERC.
"""

from __future__ import annotations

from . import contract
import enum
import hashlib
from dataclasses import dataclass, field, fields, is_dataclass

# stamped on every Schematic/Pcb/Library export
# 1.1: Pcb gains tracks/vias/arcs/pads (optional adds)
# 1.2: Pcb gains board/zones/metadata; Library gains metadata/footprints
#      (FootprintDef/FootprintPad); all optional adds.
# 2.0: BREAKING — canonical net names now follow eeschema's own precedence, so
#      an existing design's exported `Net.name` can CHANGE (a net named by a
#      power port and a local label no longer depends on read order). Shipped
#      with op-list protocol 2 (four op fields renamed) as one migration
#      boundary; see docs/migration-0.21.md.
# 2.1: Footprint gains at/dnp/exclude_from_bom/exclude_from_pos/board_only/
#      models; FootprintDef gains graphics; vias and pads gain the fields
#      fab check reads; all optional adds.
SCHEMA_VERSION = contract.SCHEMA_VERSIONS["schematic"]   # the registry is the source

# (designator, pin_number) — a PUBLIC identity, not an implementation detail:
# it is serialized as the flat "REF.PIN" string in nets/diff JSON (the
# diff-cut-wire golden pins that encoding), consumed by findings refs, the
# netlist export and agent-eval expectations, and it shares its "." separator
# with the op-list anchor syntax. Changing its shape is a coordinated
# major-version batch — see docs/pin-identity.md for the audited migration
# boundaries (notably: node-uuid seeds are NOT part of this identity and must
# not change with it).
PinRef = tuple[str, str]


class PinType(enum.Enum):
    """Canonical, format-agnostic electrical pin type."""

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


# Mapping tables — single source of truth for readers + ERC.
ALTIUM_ELECTRICAL: dict[int, PinType] = {  # Altium Pin.Electrical int -> PinType
    0: PinType.INPUT,
    1: PinType.BIDIRECTIONAL,
    2: PinType.OUTPUT,
    3: PinType.OPEN_COLLECTOR,
    4: PinType.PASSIVE,
    5: PinType.TRI_STATE,
    6: PinType.OPEN_EMITTER,
    7: PinType.POWER_IN,
}

KICAD_PINTYPE: dict[str, PinType] = {  # KiCad pin-type token -> PinType
    "input": PinType.INPUT,
    "output": PinType.OUTPUT,
    "bidirectional": PinType.BIDIRECTIONAL,
    "tri_state": PinType.TRI_STATE,
    "passive": PinType.PASSIVE,
    "free": PinType.UNSPECIFIED,
    "unspecified": PinType.UNSPECIFIED,
    "power_in": PinType.POWER_IN,
    "power_out": PinType.POWER_OUT,
    "open_collector": PinType.OPEN_COLLECTOR,
    "open_emitter": PinType.OPEN_EMITTER,
    "no_connect": PinType.NO_CONNECT,
}


@dataclass
class Pin:
    number: str                 # pin number/designator, e.g. "2"
    name: str | None            # pin name, e.g. "P0.25"
    x_mil: float                # canonical: mils, origin top-left, +Y down (electrical tip)
    y_mil: float
    electrical_type: PinType = PinType.UNSPECIFIED
    owner_part_id: int = 1      # multi-unit part (Altium OwnerPartId)
    unique_id: str | None = None
    orientation: int = 0        # lib-frame degrees {0,90,180,270}: the pin points
                                # from its electrical tip TOWARD the symbol body


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
    # Assembly attributes (KiCad `(dnp yes)` / `(in_bom no)` / `(on_board no)`;
    # readers that cannot know keep the defaults — never guess).
    dnp: bool = False           # placed but not populated
    in_bom: bool = True         # appears on the bill of materials
    on_board: bool = True       # exported to the board (position/CPL files)


@dataclass
class Net:
    name: str                   # canonical display name
    members: list[PinRef]       # SORTED stable (designator, pin_number) keys
    aliases: list[str] = field(default_factory=list)        # other explicit names
    source_names: list[str] = field(default_factory=list)   # labels/ports that contributed
    is_named: bool = True
    confidence: float = 1.0     # 0..1; lowered on ambiguous merges
    merge_reasons: list[str] = field(default_factory=list)  # explainability per merge
    # KiCad sheet-path PREFIX for the exported net name ("/" on the root sheet,
    # "/Amp/" one level down). Empty when nothing should be prefixed: an unnamed
    # net, or a name whose scope is global (global label / power port) — KiCad
    # prefixes neither (kicad-cli 10.0.4-verified, see exporters._kicad_net_name).
    # `name` itself always stays the BARE name; this is emission metadata only.
    sheet_path: str = ""

    @property
    def stable_id(self) -> str:
        """Hash of sorted membership — NEVER coordinate-derived."""
        joined = "|".join(f"{d}.{p}" for d, p in self.members)
        return "net_" + hashlib.sha1(joined.encode()).hexdigest()[:12]


@dataclass
class Schematic:
    source_path: str
    source_format: str          # "altium" | "kicad"
    components: list[Component]
    nets: list[Net]
    sheets: list[str] = field(default_factory=list)
    no_erc_points: list[tuple[float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)  # passive_pin_ratio, frac_present, ...
    # Document-level parameters that populate the sheet's title block
    # (Title / Revision / DocumentNumber / Date / Organization ...), verbatim
    # from the source document. Empty when the file carries none. Additive in
    # schema 1.3 — consumers that ignore unknown keys are unaffected.
    title_block: dict[str, str] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        """Return the JSON-serializable export dict (stamps ``schema_version``)."""
        d = to_json(self)
        assert isinstance(d, dict)  # to_json of a dataclass is always a dict
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d


# --- PCB is a SEPARATE sub-model (footprints/pads/nets, NOT symbol pins) ---
@dataclass
class Footprint:
    designator: str
    footprint_name: str | None
    layer: str | None
    rotation: float = 0.0
    value: str | None = None
    # Placement + assembly attributes (additive; consumers ignore unknown
    # keys). Populated by the KiCad board reader from ``(at ...)`` /
    # ``(attr ...)`` / ``(model ...)``; a reader that cannot know keeps the
    # defaults — never a guess (same rule as ``Component.dnp``). ``at`` is the
    # footprint origin in the reader's own board frame (KiCad: mm, +Y down —
    # the frame ``Pcb.pads[*]["at"]`` uses); the angle stays in ``rotation``.
    at: tuple[float, float] | None = None
    dnp: bool = False               # KiCad `(attr ... dnp)`: placed, not populated
    exclude_from_bom: bool = False  # `exclude_from_bom` (legacy `virtual`)
    exclude_from_pos: bool = False  # `exclude_from_pos_files` (legacy `virtual`)
    board_only: bool = False        # `board_only`: KiCad's "not in schematic"
    models: list[str] = field(default_factory=list)   # 3D model paths as written


@dataclass
class Pcb:
    source_path: str
    source_format: str
    nets: list[str]                          # net names only (v1 scope)
    footprints: list[Footprint]
    classes: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    # Copper geometry decoded from the binary sections (Altium frame, mils,
    # +Y up; empty when a section is absent). Added in schema 1.1.
    tracks: list[dict] = field(default_factory=list)
    vias: list[dict] = field(default_factory=list)
    arcs: list[dict] = field(default_factory=list)
    pads: list[dict] = field(default_factory=list)
    # Schema 1.2 additive fields: board setup (layers/thickness/design rules,
    # KiCad reader), copper zones, and reader-side metadata/warnings.
    # The KiCad reader's dict keys grew additively in schema 2.1 (nothing
    # was renamed or removed): a via carries `type`
    # (through|blind|buried|micro) and `tenting` {front, back}; a pad carries
    # `drill_shape` + `drill_size` (w, h) beside `drill`; `board` carries
    # `tenting` {front, back, source} and `outline` {closed, area_mm2, bbox,
    # loops, open_ends, problems} beside the historical `outline_bbox`.
    zones: list[dict] = field(default_factory=list)
    board: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        d = to_json(self)
        assert isinstance(d, dict)  # to_json of a dataclass is always a dict
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d


# --- Library model (symbol defs WITH pin electrical types) ---
@dataclass
class SymbolDef:
    name: str
    lib_id: str | None
    pins: list[Pin]
    part_count: int = 1
    extends: str | None = None
    body_sexpr: object | None = None         # KiCad: raw symbol node for writer lib_cache


# --- Footprint library model (schema 1.2) ---
@dataclass
class FootprintPad:
    """One pad of a footprint DEFINITION (library frame: mm, +Y down)."""

    number: str
    x_mm: float
    y_mm: float
    size_x_mm: float
    size_y_mm: float
    shape: str = "rect"          # rect|roundrect|circle|oval|custom
    pad_type: str = "smd"        # smd|thru_hole|np_thru_hole|connect
    layers: list[str] = field(default_factory=list)
    drill_mm: float | None = None
    rotation: float = 0.0


@dataclass
class FootprintDef:
    """A footprint library item (KiCad ``.kicad_mod`` / Altium ``.PcbLib`` entry)."""

    name: str
    pads: list[FootprintPad] = field(default_factory=list)
    layer: str = "F.Cu"
    attributes: list[str] = field(default_factory=list)   # smd|through_hole|...
    models: list[str] = field(default_factory=list)       # 3D model paths as written
    courtyard: bool = False       # has any *.Courtyard graphics
    format_version: str | None = None   # KiCad (version ...) token, None = legacy/unknown
    warnings: list[str] = field(default_factory=list)
    # Outline/silkscreen graphics in source order (additive; library frame: mm,
    # +Y down). Lines, arcs and circles only — every item carries ``kind``,
    # ``layer`` (KiCad name), ``source_layer`` (Altium name, None for a KiCad
    # source) and ``width``, plus:
    #   line   -> ``start``, ``end``
    #   arc    -> ``start``, ``mid``, ``end`` (the KiCad ``fp_arc`` triple)
    #   circle -> ``center``, ``radius``
    graphics: list[dict] = field(default_factory=list)


@dataclass
class Library:
    source_path: str
    source_format: str
    symbols: list[SymbolDef]
    footprints: list[FootprintDef] = field(default_factory=list)   # schema 1.2
    warnings: list[str] = field(default_factory=list)              # schema 1.2
    metadata: dict = field(default_factory=dict)                   # schema 1.2
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        d = to_json(self)
        assert isinstance(d, dict)  # to_json of a dataclass is always a dict
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d


# --- NetPrimitives: the reader -> netbuild interface ---
@dataclass
class WireSeg:
    a: tuple[float, float]
    b: tuple[float, float]
    sheet: str = ""


@dataclass
class Junction:
    at: tuple[float, float]
    sheet: str = ""


@dataclass
class NetLabel:
    at: tuple[float, float]
    text: str
    scope: str = "local"  # local|global|power|port|sheet_entry
    sheet: str = ""


@dataclass
class PinHandle:
    ref: PinRef
    at: tuple[float, float]
    sheet: str = ""


@dataclass
class BusEntry:
    """A ``(bus_entry)``: two ends, ``a`` = ``(at)``, ``b`` = ``(at)+(size)``.

    Which end lands on the bus is a geometric question netbuild answers; the
    entry itself conducts between its two ends (kicad-cli-verified)."""

    a: tuple[float, float]
    b: tuple[float, float]
    sheet: str = ""


@dataclass
class NetPrimitives:
    wires: list[WireSeg] = field(default_factory=list)
    junctions: list[Junction] = field(default_factory=list)
    labels: list[NetLabel] = field(default_factory=list)
    pins: list[PinHandle] = field(default_factory=list)
    # Bus layer (KiCad reader; empty for Altium). Bus SEGMENTS reuse WireSeg;
    # bus labels are ordinary NetLabels whose anchor netbuild finds on a bus.
    buses: list[WireSeg] = field(default_factory=list)
    bus_entries: list[BusEntry] = field(default_factory=list)
    no_erc: list[tuple[float, float]] = field(default_factory=list)
    power_priority: bool = False        # PrjPcb PowerPortNamesTakePriority
    emit_single_pin_nets: bool = True   # PrjPcb NetlistSinglePinNets
    # Canonical-name dialect: True (KiCad reader) ranks name drivers by
    # eeschema's CONNECTION_SUBGRAPH priority; False (Altium readers) keeps
    # the legacy two-mode rule driven by power_priority.
    eeschema_naming: bool = False


def to_json(obj: object) -> object:
    """Recursively convert dataclasses/enums/containers into JSON-native types.

    Enums serialize to ``.value``; tuples to lists; ``Net`` additionally carries
    its computed ``stable_id``. ``schema_version`` (a normal field) is preserved.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        result: dict = {}
        for f in fields(obj):
            val = getattr(obj, f.name)
            # ``body_sexpr`` is a writer-only raw S-expression (SNode) handle kept
            # for the lib_cache; it is not part of the JSON contract and is not
            # JSON-serializable, so it always exports as null.
            if f.name == "body_sexpr":
                val = None
            result[f.name] = to_json(val)
        if isinstance(obj, Net):
            result["stable_id"] = obj.stable_id
        return result
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    return obj
