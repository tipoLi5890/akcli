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

import enum
import hashlib
from dataclasses import dataclass, field, fields, is_dataclass

SCHEMA_VERSION = "1.1"  # 1.1: Pcb gains tracks/vias/arcs/pads (optional adds)  # stamped on every Schematic/Pcb/Library export

# (designator, pin_number)
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


@dataclass
class Net:
    name: str                   # canonical display name
    members: list[PinRef]       # SORTED stable (designator, pin_number) keys
    aliases: list[str] = field(default_factory=list)        # other explicit names
    source_names: list[str] = field(default_factory=list)   # labels/ports that contributed
    is_named: bool = True
    confidence: float = 1.0     # 0..1; lowered on ambiguous merges
    merge_reasons: list[str] = field(default_factory=list)  # explainability per merge

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
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        """Return the JSON-serializable export dict (stamps ``schema_version``)."""
        d = to_json(self)
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
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        d = to_json(self)
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


@dataclass
class Library:
    source_path: str
    source_format: str
    symbols: list[SymbolDef]
    schema_version: str = SCHEMA_VERSION

    def export(self) -> dict:
        d = to_json(self)
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
