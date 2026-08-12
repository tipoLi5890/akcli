"""Design-document -> op-list generator (``akcli ops from-intent``).

Field observation from agent-operated board work: an agent reading a datasheet
reliably produces a correct pin->net table, then loses that correctness while
hand-translating the table into op-list *geometry* (label collisions, pitch
trial-and-error, coordinate-coincidence near-shorts). Label-only connectivity
is proven viable — so the pin->net table becomes the authoring interface and
this module owns the geometry.

The input DESIGN document is a superset of the ``check --intent`` file
(``schemas/design-intent.schema.json``)::

    {"protocol_version": 1,
     "nets": {"RAIL_A": ["U1.3", "C1.1"], ...},          # intent format
     "components": {"U1": {"lib_id": "...", "value": "...", ...}, ...},
     "layout": {"origin": [1000, 1000], "columns": 3, "margin_mil": 100,
                "group": "NAME", "no_connect_unlisted": true}}

Output is an ordinary op-list (op-list ``protocol_version``, which is NOT
the design-intent one) using ONLY the existing
vocabulary — ``place_component`` + label-on-pin ``add_net_label`` +
``add_no_connect`` — so it flows through the unchanged plan -> dry-apply ->
netdiff -> connectivity-verify pipeline. Components land in a uniform grid of
cells, each cell sized to the largest body + pin field + estimated label text
(the same stroke-font metrics ``check --layout`` judges the result by), ceiled
to the 50-mil writer grid.

Contract (the round-trip property): drawing the emitted op-list on a blank
sheet yields a netlist EQUAL to the design's ``nets`` table, and
``check --intent <design.json>`` over the drawn sheet reports zero findings.

Errors: shape problems raise ``BAD_CONFIG``/``PROTOCOL_MISMATCH`` (mirroring
``checks.intent.load``); semantic refusals — a pin addressed by NAME, wildcard
members, a REF missing from ``components``, one pin in two nets, coincident
stacked pins mapped to different nets, bus-vector net names — raise
``DESIGN_UNSUPPORTED`` (exit 6). An unknown ``lib_id`` raises the writer's
own ``SYMBOL_NOT_FOUND``. Deliberate MVP bounds, refused loudly rather than
guessed at: multi-unit symbols place ALL units (adjacent cells, unreferenced
units all-NC), and a multi-unit symbol with common (unit-0) pins is refused —
every placed unit would re-expose those pins and one label cannot honestly
cover N electrical points.

Determinism: identical output bytes for identical input content, regardless of
JSON dict key order — everything iterates in sorted order (components by
natural designator, nets by name, members by natural REF/pin).
"""

from __future__ import annotations

from . import contract

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import model, units
from .classify import natkey as _natkey
from .checks import intent as intent_mod
from .geometry.boxes import label_text_extent_mil
from .errors import fail
from .netbuild import expand_bus_vector
from .readers import kicad_lib
from .geometry import transform as geometry

PROTOCOL_VERSION = contract.PROTOCOL_VERSIONS["design-intent"]
# the INPUT is a design-intent doc, the OUTPUT is an op-list: two
# independently-versioned protocols that must not borrow each other's
# number (they were both 1 until op-list protocol 2).
OPLIST_PROTOCOL_VERSION = contract.PROTOCOL_VERSIONS["oplist"]
GRID_MIL = 50.0

DEFAULT_ORIGIN: tuple[float, float] = (1000.0, 1000.0)
DEFAULT_MARGIN_MIL = 100.0

# Accepted keys (the schema-agreement test asserts these match
# schemas/design-intent.schema.json in both directions).
TOP_KEYS = frozenset({"protocol_version", "mode", "nets", "components",
                      "layout"})
COMPONENT_KEYS = frozenset({"lib_id", "value", "footprint", "rotation",
                            "mirror"})
LAYOUT_KEYS = frozenset({"origin", "columns", "margin_mil", "group",
                         "no_connect_unlisted"})

_VALID_ROTATIONS = frozenset({0, 90, 180, 270})
_VALID_MIRRORS = frozenset({"none", "x", "y"})

# KiCad bus label syntax a net NAME must not use: a vector label NAME[a..b]
# (netbuild.expand_bus_vector) or the group notation {..}. eeschema reads such
# label TEXT as a bus regardless of what it is anchored on, while akcli's
# netbuild treats it as a literal name off-bus — a sheet drawn with one would
# pass every akcli gate and mean something else in eeschema. Refused.
_BUS_GROUP_CHARS = frozenset("{}")



# --------------------------------------------------------------------------- #
# design document model + loader
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ComponentSpec:
    """One declared component: symbol identity + optional instance dressing."""

    ref: str
    lib_id: str
    value: str | None = None
    footprint: str | None = None
    rotation: int = 0
    mirror: str = "none"


@dataclass
class LayoutSpec:
    """Grid-layout hints; every field has the documented default."""

    origin: tuple[float, float] = DEFAULT_ORIGIN
    columns: int | None = None          # None -> ceil(sqrt(placement count))
    margin_mil: float = DEFAULT_MARGIN_MIL
    group: str | None = None
    no_connect_unlisted: bool = True


@dataclass
class Design:
    """A validated design document (nets half validated by ``checks.intent``)."""

    nets: dict[str, intent_mod.NetSpec] = field(default_factory=dict)
    components: dict[str, ComponentSpec] = field(default_factory=dict)
    layout: LayoutSpec = field(default_factory=LayoutSpec)


def _snap50(v: float) -> float:
    """Nearest 50-mil multiple (the writer grid)."""
    return round(float(v) / GRID_MIL) * GRID_MIL


def _ceil50(v: float) -> float:
    """Smallest 50-mil multiple >= ``v``."""
    return math.ceil(float(v) / GRID_MIL) * GRID_MIL


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_point(v: object) -> bool:
    return (isinstance(v, list) and len(v) == 2 and all(_num(c) for c in v))


def _parse_components(doc: dict, where: str) -> dict[str, ComponentSpec]:
    raw = doc.get("components")
    if not isinstance(raw, dict) or not raw:
        fail("BAD_CONFIG",
             f"{where}: 'components' must be a non-empty object of "
             '{"REF": {"lib_id": "Lib:Symbol", ...}, ...}')
    out: dict[str, ComponentSpec] = {}
    for ref in sorted(map(str, raw), key=_natkey):
        meta = raw[ref]
        if not ref.strip():
            fail("BAD_CONFIG", f"{where}: component REFs must be non-empty")
        if "." in ref:
            fail("BAD_CONFIG",
                 f"{where}: component REF {ref!r} must not contain '.' "
                 "(net members split on the first dot)")
        if not isinstance(meta, dict):
            fail("BAD_CONFIG",
                 f"{where}: component '{ref}': value must be an object "
                 '{"lib_id": ..., "value"?, "footprint"?, "rotation"?, '
                 '"mirror"?}')
        extra = set(meta) - COMPONENT_KEYS
        if extra:
            fail("BAD_CONFIG",
                 f"{where}: component '{ref}': unknown key(s): "
                 f"{', '.join(sorted(map(str, extra)))} "
                 f"(expected {', '.join(sorted(COMPONENT_KEYS))})")
        lib_id = meta.get("lib_id")
        if not isinstance(lib_id, str) or not lib_id.strip():
            fail("BAD_CONFIG",
                 f"{where}: component '{ref}': 'lib_id' must be a non-empty "
                 "string (e.g. 'Device:R')")
        for key in ("value", "footprint"):
            if key in meta and not isinstance(meta[key], str):
                fail("BAD_CONFIG",
                     f"{where}: component '{ref}': {key!r} must be a string")
        rotation = meta.get("rotation", 0)
        if isinstance(rotation, bool) or rotation not in _VALID_ROTATIONS:
            fail("BAD_CONFIG",
                 f"{where}: component '{ref}': rotation must be one of "
                 "0, 90, 180, 270")
        mirror = meta.get("mirror")
        if mirror is None:
            mirror = "none"
        if mirror not in _VALID_MIRRORS:
            fail("BAD_CONFIG",
                 f"{where}: component '{ref}': mirror must be one of "
                 "none, x, y (or null)")
        out[ref] = ComponentSpec(
            ref=ref, lib_id=lib_id,
            value=meta.get("value"), footprint=meta.get("footprint"),
            rotation=int(rotation), mirror=str(mirror),
        )
    return out


def _parse_layout(doc: dict, where: str) -> LayoutSpec:
    raw = doc.get("layout")
    if raw is None:
        return LayoutSpec()
    if not isinstance(raw, dict):
        fail("BAD_CONFIG", f"{where}: 'layout' must be an object")
    extra = set(raw) - LAYOUT_KEYS
    if extra:
        fail("BAD_CONFIG",
             f"{where}: layout: unknown key(s): "
             f"{', '.join(sorted(map(str, extra)))} "
             f"(expected {', '.join(sorted(LAYOUT_KEYS))})")
    out = LayoutSpec()
    if "origin" in raw:
        if not _is_point(raw["origin"]):
            fail("BAD_CONFIG",
                 f"{where}: layout.origin must be [x_mil, y_mil]")
        out.origin = (_snap50(raw["origin"][0]), _snap50(raw["origin"][1]))
    if "columns" in raw:
        cols = raw["columns"]
        if isinstance(cols, bool) or not isinstance(cols, int) or cols < 1:
            fail("BAD_CONFIG",
                 f"{where}: layout.columns must be an integer >= 1")
        out.columns = cols
    if "margin_mil" in raw:
        if not _num(raw["margin_mil"]) or raw["margin_mil"] < 0:
            fail("BAD_CONFIG",
                 f"{where}: layout.margin_mil must be a number >= 0")
        out.margin_mil = float(raw["margin_mil"])
    if "group" in raw:
        if not isinstance(raw["group"], str) or not raw["group"].strip():
            fail("BAD_CONFIG",
                 f"{where}: layout.group must be a non-empty string")
        out.group = raw["group"]
    if "no_connect_unlisted" in raw:
        if not isinstance(raw["no_connect_unlisted"], bool):
            fail("BAD_CONFIG",
                 f"{where}: layout.no_connect_unlisted must be true or false")
        out.no_connect_unlisted = raw["no_connect_unlisted"]
    return out


def load_design(path: str | Path) -> Design:
    """Load + validate a design document (see the module docstring for shape).

    The ``nets`` half is validated by ``checks.intent.load_doc`` — the SAME
    loader ``check --intent`` runs, so a design file that generates is
    guaranteed loadable as the assertion baseline. Shape errors raise
    ``BAD_CONFIG``/``PROTOCOL_MISMATCH``; ``FileNotFoundError`` propagates
    (the CLI maps it to exit 4).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        fail("BAD_CONFIG", f"invalid design JSON in {p}: {exc}")
    where = str(p)
    if not isinstance(doc, dict):
        fail("BAD_CONFIG", f"{where}: design root must be a JSON object")
    # top-level key tolerance + protocol/mode/nets shape: the intent loader
    # owns it (its _TOP_KEYS is the design superset)
    spec = intent_mod.load_doc(doc, where)
    components = _parse_components(doc, where)
    layout = _parse_layout(doc, where)
    return Design(nets=dict(spec.nets), components=components, layout=layout)


# --------------------------------------------------------------------------- #
# semantic validation (DESIGN_UNSUPPORTED refusals)
# --------------------------------------------------------------------------- #
def _check_net_names_and_members(design: Design) -> dict[tuple[str, str], str]:
    """Refuse wildcards / bus names / undeclared REFs / double-netted pins.

    Returns the ``(ref, pin) -> net name`` map generation places labels from.
    """
    pin_net: dict[tuple[str, str], str] = {}
    for name in sorted(design.nets):
        if expand_bus_vector(name) or any(c in name for c in _BUS_GROUP_CHARS):
            fail("DESIGN_UNSUPPORTED",
                 f"net name {name!r} uses KiCad bus syntax (NAME[a..b] vector "
                 "or {..} group notation) — eeschema would read a label with "
                 "this text as a bus, not a net; name each member net "
                 "individually")
        for m in design.nets[name].members:
            if m.is_wildcard:
                fail("DESIGN_UNSUPPORTED",
                     f"net '{name}': member {m.token!r} is a wildcard pattern "
                     "— wildcards are legal for `check --intent` assertions "
                     "but generation needs literal 'REF.PIN' members")
            if m.ref not in design.components:
                fail("DESIGN_UNSUPPORTED",
                     f"net '{name}': member {m.token!r} references REF "
                     f"{m.ref!r} which has no \"components\" entry — declare "
                     "it with a lib_id")
            key = (m.ref, m.pin)
            if key in pin_net and pin_net[key] != name:
                fail("DESIGN_UNSUPPORTED",
                     f"pin {m.token!r} is listed in two nets "
                     f"({pin_net[key]!r} and {name!r}) — one pin belongs to "
                     "exactly one net")
            pin_net[key] = name
    return pin_net


def _check_pins_exist(
    ref: str, comp: ComponentSpec, symdef: model.SymbolDef,
    members: list[tuple[str, str]],
) -> None:
    """Refuse a member pin the symbol does not have (with a name->number hint)."""
    numbers = {p.number for p in symdef.pins}
    for net_name, pin in members:
        if pin in numbers:
            continue
        by_name = sorted(
            (p.number for p in symdef.pins if (p.name or "") == pin),
            key=_natkey)
        if by_name:
            fail("DESIGN_UNSUPPORTED",
                 f"net '{net_name}': member '{ref}.{pin}' addresses a pin by "
                 f"NAME — use the pin NUMBER: '{ref}.{by_name[0]}' "
                 f"(pin {by_name[0]} is named {pin!r})")
        fail("DESIGN_UNSUPPORTED",
             f"net '{net_name}': component '{ref}' ({comp.lib_id}) has no "
             f"pin {pin!r} (valid pins: "
             f"{', '.join(sorted(numbers, key=_natkey))})")


def _coord_key(p: model.Pin) -> tuple[int, int]:
    """Symbol-local pin coordinate key robust to mm->mil float noise."""
    return (round(p.x_mil * 10), round(p.y_mil * 10))


def _check_coincident_pins(
    ref: str, symdef: model.SymbolDef, unit: int,
    pin_net: dict[tuple[str, str], str],
) -> None:
    """Refuse two physically-coincident pins mapped to DIFFERENT nets.

    Stacked pins (same symbol-local coordinate) share one electrical point on
    the canvas; two different labels there would short the two nets while the
    design table claims they are separate.
    """
    by_coord: dict[tuple[int, int], list[model.Pin]] = {}
    for p in kicad_lib.unit_pins(symdef, unit):
        by_coord.setdefault(_coord_key(p), []).append(p)
    for stacked in by_coord.values():
        if len(stacked) < 2:
            continue
        netted = [(p.number, pin_net[(ref, p.number)])
                  for p in stacked if (ref, p.number) in pin_net]
        nets = sorted({n for _num_, n in netted})
        if len(nets) > 1:
            pins = ", ".join(f"{ref}.{num} -> {net!r}"
                             for num, net in sorted(netted,
                                                    key=lambda t: _natkey(t[0])))
            fail("DESIGN_UNSUPPORTED",
                 f"component '{ref}': physically-coincident (stacked) pins "
                 f"are mapped to different nets ({pins}) — they share one "
                 "point on the canvas and would short; put them on one net "
                 "or use a symbol without stacked pins")


# --------------------------------------------------------------------------- #
# geometry: per-placeable cells on a uniform grid
# --------------------------------------------------------------------------- #
@dataclass
class _Placeable:
    """One placed symbol instance (a unit of a component) and its cell needs."""

    ref: str
    comp: ComponentSpec
    symdef: model.SymbolDef
    unit: int
    multi_unit: bool
    # extent of body + pin tips around the symbol origin, world-oriented
    # (rotation/mirror applied), in mils
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    label_len: float = 0.0              # longest estimated label text, mils
    cell_w: float = 0.0
    cell_h: float = 0.0
    off_x: float = 0.0                  # symbol origin offset inside the cell
    off_y: float = 0.0


def _instance_box_mil(
    symdef: model.SymbolDef, unit: int, rotation: int, mirror: str,
) -> tuple[float, float, float, float]:
    """World-oriented extent (mils) of body + pin tips about the origin.

    Uses the writer's canonical transform chain (``geometry``) — never a
    reimplementation — with the instance at (0, 0).
    """
    xs: list[float] = []
    ys: list[float] = []
    ext = kicad_lib.body_extent_mil(symdef, unit)
    if ext is not None:
        bx0, by0, bx1, by1 = geometry.world_box_from_extent(
            ext, rotation, mirror, (0, 0))
        xs += [units.nm_to_mil(bx0), units.nm_to_mil(bx1)]
        ys += [units.nm_to_mil(by0), units.nm_to_mil(by1)]
    for p in kicad_lib.unit_pins(symdef, unit):
        wx, wy = geometry.transform_point(
            (geometry.mil_to_nm(p.x_mil), -geometry.mil_to_nm(p.y_mil)),
            rotation, mirror)
        xs.append(units.nm_to_mil(wx))
        ys.append(units.nm_to_mil(wy))
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _size_cells(placeables: list[_Placeable], margin: float,
                pin_net: dict[tuple[str, str], str]) -> None:
    for pl in placeables:
        unit_pin_nums = {p.number for p in kicad_lib.unit_pins(pl.symdef,
                                                               pl.unit)}
        label_names = sorted({
            net for (ref, pin), net in pin_net.items()
            if ref == pl.ref and pin in unit_pin_nums})
        pl.label_len = max(
            (label_text_extent_mil(n)[0] for n in label_names), default=0.0)
        pl.box = _instance_box_mil(pl.symdef, pl.unit,
                                   pl.comp.rotation, pl.comp.mirror)
        x0, y0, x1, y1 = pl.box
        pad = pl.label_len + margin
        # +GRID_MIL slack absorbs the ceil of the in-cell origin offset, so
        # content provably stays inside the cell (see off_x/off_y below)
        pl.cell_w = _ceil50((x1 - x0) + 2 * pad + GRID_MIL)
        pl.cell_h = _ceil50((y1 - y0) + 2 * pad + GRID_MIL)
        pl.off_x = _ceil50(pad - x0)
        pl.off_y = _ceil50(pad - y0)


def _as_json_num(v: float) -> Any:
    """Ints where the value is integral, so the emitted JSON stays clean."""
    return int(v) if float(v).is_integer() else float(v)


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
def generate(design: Design, libs: list[model.Library]) -> dict:
    """Emit a complete, valid op-list document for ``design``.

    ``libs`` are resolved symbol libraries (the caller coerces ``--symbols`` /
    config paths through the same chain ``plan``/``draw`` use). Raises
    ``DESIGN_UNSUPPORTED`` for the documented refusals and propagates
    ``SYMBOL_NOT_FOUND`` for an unknown ``lib_id``. Pure and deterministic:
    no I/O, identical output for identical design content.
    """
    pin_net = _check_net_names_and_members(design)

    # net members grouped per REF, for pin-existence checks (sorted for
    # deterministic first-refusal order)
    members_by_ref: dict[str, list[tuple[str, str]]] = {}
    for name in sorted(design.nets):
        for m in sorted(design.nets[name].members,
                        key=lambda m: (_natkey(m.ref), _natkey(m.pin))):
            members_by_ref.setdefault(m.ref, []).append((name, m.pin))

    placeables: list[_Placeable] = []
    for ref in sorted(design.components, key=_natkey):
        comp = design.components[ref]
        symdef = kicad_lib.resolve(comp.lib_id, libs)   # SYMBOL_NOT_FOUND
        _check_pins_exist(ref, comp, symdef, members_by_ref.get(ref, []))
        part_count = max(1, symdef.part_count or 1)
        if part_count > 1 and any(p.owner_part_id == 0 for p in symdef.pins):
            fail("DESIGN_UNSUPPORTED",
                 f"component '{ref}' ({comp.lib_id}): a multi-unit symbol "
                 "with common (unit-0) pins is not supported — every placed "
                 "unit re-exposes those pins as separate electrical points; "
                 "author this part with a hand-written op-list")
        for unit in range(1, part_count + 1):
            _check_coincident_pins(ref, symdef, unit, pin_net)
            placeables.append(_Placeable(ref=ref, comp=comp, symdef=symdef,
                                         unit=unit,
                                         multi_unit=part_count > 1))

    layout = design.layout
    _size_cells(placeables, layout.margin_mil, pin_net)
    cell_w = max((pl.cell_w for pl in placeables), default=GRID_MIL)
    cell_h = max((pl.cell_h for pl in placeables), default=GRID_MIL)
    columns = layout.columns or max(1, math.ceil(math.sqrt(len(placeables))
                                                 or 1))
    grouped = layout.group is not None
    base_x, base_y = (0.0, 0.0) if grouped else layout.origin

    ops: list[dict] = []
    for i, pl in enumerate(placeables):
        col, row = i % columns, i // columns
        op: dict = {
            "op": "place_component",
            "lib_id": pl.comp.lib_id,
            "designator": pl.ref,
            "x_mil": _as_json_num(base_x + col * cell_w + pl.off_x),
            "y_mil": _as_json_num(base_y + row * cell_h + pl.off_y),
        }
        if pl.comp.rotation:
            op["rotation"] = pl.comp.rotation
        if pl.comp.mirror != "none":
            op["mirror"] = pl.comp.mirror
        if pl.multi_unit:
            op["unit"] = pl.unit
        # value/footprint go on EVERY unit instance (each placed symbol
        # carries its own properties; eeschema keeps them in sync)
        if pl.comp.value is not None:
            op["value"] = pl.comp.value
        if pl.comp.footprint is not None:
            op["footprint"] = pl.comp.footprint
        if grouped:
            op["group"] = layout.group
        ops.append(op)

    for name in sorted(design.nets):
        for m in sorted(design.nets[name].members,
                        key=lambda m: (_natkey(m.ref), _natkey(m.pin))):
            op = {"op": "add_net_label", "name": name, "at": m.token}
            if grouped:
                op["group"] = layout.group
            ops.append(op)

    if layout.no_connect_unlisted:
        ops.extend(_no_connect_ops(placeables, pin_net, layout.group))

    doc: dict = {
        "protocol_version": OPLIST_PROTOCOL_VERSION,
        "target_format": "kicad",
        # provenance annotation: `_`-prefixed keys are validator-ignored
        "_generator": "akcli ops from-intent",
    }
    if grouped:
        ox, oy = layout.origin
        doc["groups"] = {layout.group: {
            "origin": [_as_json_num(ox), _as_json_num(oy)]}}
    doc["ops"] = ops
    return doc


def _no_connect_ops(
    placeables: list[_Placeable],
    pin_net: dict[tuple[str, str], str],
    group: str | None,
) -> list[dict]:
    """``add_no_connect`` for every unlisted pin of every placed instance.

    Coincident-pin care: a pin stacked on a NETTED pin of the same instance is
    electrically that net already — an NC marker there would contradict the
    live connection, so it is skipped; a stack of only-unlisted pins gets ONE
    marker (lowest pin number), not N stacked markers.
    """
    ops: list[dict] = []
    for pl in placeables:
        netted_coords = set()
        pins = sorted(kicad_lib.unit_pins(pl.symdef, pl.unit),
                      key=lambda p: _natkey(p.number))
        for p in pins:
            if (pl.ref, p.number) in pin_net:
                netted_coords.add(_coord_key(p))
        seen_nc_coords: set[tuple[int, int]] = set()
        for p in pins:
            if (pl.ref, p.number) in pin_net:
                continue
            coord = _coord_key(p)
            if coord in netted_coords or coord in seen_nc_coords:
                continue
            seen_nc_coords.add(coord)
            op = {"op": "add_no_connect", "pin": f"{pl.ref}.{p.number}"}
            if group is not None:
                op["group"] = group
            ops.append(op)
    return ops
