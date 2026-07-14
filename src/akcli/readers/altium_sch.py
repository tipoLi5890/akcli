"""``.SchDoc`` -> :class:`model.Schematic` (READ-ONLY) (SPEC §3.2).

An Altium schematic document is an OLE2/CFBF container whose ``FileHeader`` stream
holds the post-header record list (see :mod:`._cfbf` and :mod:`.altium_records`).
This module turns those records into the normalized, format-agnostic model:

* **RECORD 1** -> :class:`model.Component` placement / orientation / mirror /
  ``UniqueId``;
* **RECORD 2** -> :class:`model.Pin` with ``Electrical`` -> :class:`model.PinType`,
  ``OwnerPartId``, and the *electrical tip* = ``Location + PinLength*dir`` (the pin
  Location is the body/inner end; the wire connects at the far tip);
* **RECORD 34** -> component designator (synthesize ``$U<idx>`` when missing -- a
  component is NEVER dropped, so verify-by-re-export keeps ground truth);
* **RECORD 41** -> parameters (and ``value`` / ``Comment``);
* **RECORD 45 / 46** -> footprint (model name);
* **RECORD 27** wires, **29** junctions, **22** No-ERC, **25 / 17 / 18 / 16**
  net-labels / power-ports / ports / sheet-entries -> :class:`model.NetPrimitives`
  -> :func:`netbuild.build_nets`.

Canonical coordinate system is origin top-left, **+Y down**, unit **mils**; the
Altium schematic is +Y *up*, so every Y is **negated** on ingest (a uniform
transform that preserves net geometry exactly).
"""

from __future__ import annotations

import os

from .. import netbuild
from ..errors import fail
from ..model import (
    Component,
    NetLabel,
    NetPrimitives,
    Pin,
    PinHandle,
    Schematic,
    WireSeg,
)
from ..model import Junction as MJunction
from . import _cfbf
from .altium_records import (
    RECORD_COMPONENT,
    RECORD_DESIGNATOR,
    RECORD_IMPL_FOOTPRINT,
    RECORD_IMPL_MODEL,
    RECORD_JUNCTION,
    RECORD_NET_LABEL,
    RECORD_NO_ERC,
    RECORD_PARAMETER,
    RECORD_PIN,
    RECORD_PORT,
    RECORD_POWER_PORT,
    RECORD_SHEET_ENTRY,
    RECORD_SHEET_SYMBOL,
    RECORD_WIRE,
    coord,
    gi,
    parse_records,
    pin_electrical_type,
)

# PinConglomerate low 2 bits -> orientation unit vector (Altium Location units).
# 0 = right (+X), 1 = up (+Y), 2 = left (-X), 3 = down (-Y).
_DIRS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}

# Scope of an explicit net name by the record that carried it. RECORD 16
# (sheet entry) is NOT here: real entries carry Name/Side/DistanceFromTop, not
# Text/Location, and are handled structurally by the hierarchy walker.
_LABEL_SCOPE = {
    RECORD_NET_LABEL: "local",
    RECORD_POWER_PORT: "power",
    RECORD_PORT: "port",
}

_TRUE = {"T", "TRUE", "1"}


def _rid(rec: dict) -> int | None:
    """The integer RECORD id of a record dict, or ``None``."""
    return gi(rec, "RECORD")


def _canon(x_mil: float, y_mil: float) -> tuple[float, float]:
    """Negate Y: Altium (+Y up) -> canonical top-left (+Y down)."""
    return (x_mil, -y_mil)


def _read_fileheader(path: os.PathLike | str | bytes | bytearray) -> list[dict]:
    """Load the container and frame its ``FileHeader`` stream into records."""
    streams = _cfbf.read_streams(path)
    fh = streams.get("FileHeader")
    if fh is None:
        fail("ALTIUM_MALFORMED", "no FileHeader stream in .SchDoc container")
    return parse_records(fh, drop_header=True)


# ---------------------------------------------------------------------------
# Component / pin extraction
# ---------------------------------------------------------------------------
def _designators(recs: list[dict]) -> dict[int, str]:
    """Map a component's record index -> its RECORD-34 designator Text."""
    out: dict[int, str] = {}
    for r in recs:
        if _rid(r) == RECORD_DESIGNATOR:
            oi = gi(r, "OwnerIndex")
            text = r.get("Text")
            if oi is not None and text:
                out[oi] = text
    return out


def _footprints(recs: list[dict]) -> dict[int, str]:
    """Map a component's record index -> its first model/footprint name.

    The model link is a chain, not a direct child of the component: the model record
    (RECORD-45/46, carrying ``ModelName``) is owned by a RECORD-44 *Implementation*,
    which in turn is owned by the RECORD-1 *Component*. So the footprint name must be
    resolved back to the **component** index by walking the ``OwnerIndex`` chain up to
    the first component — keying directly on the model's owner would land on the
    RECORD-44 and never match a component (the bug that hid every model-link footprint).
    """
    components = {i for i, r in enumerate(recs) if _rid(r) == RECORD_COMPONENT}
    n = len(recs)

    def owning_component(start: int) -> int | None:
        seen: set[int] = set()
        cur = start
        for _ in range(8):  # depth cap; real chains are model -> impl -> component
            if cur is None or cur in seen or not (0 <= cur < n):
                return None
            seen.add(cur)
            oi = gi(recs[cur], "OwnerIndex")
            if oi is None:
                return None
            if oi in components:
                return oi
            cur = oi
        return None

    out: dict[int, str] = {}
    for i, r in enumerate(recs):
        if _rid(r) in (RECORD_IMPL_MODEL, RECORD_IMPL_FOOTPRINT):
            name = r.get("ModelName") or r.get("Name")
            if not name:
                continue
            comp_idx = owning_component(i)
            if comp_idx is not None and comp_idx not in out:
                out[comp_idx] = name
    return out


def _parameters(recs: list[dict]) -> dict[int, dict[str, str]]:
    """Map a component's record index -> its RECORD-41 ``{Name: Text}`` params."""
    out: dict[int, dict[str, str]] = {}
    for r in recs:
        if _rid(r) == RECORD_PARAMETER:
            oi = gi(r, "OwnerIndex")
            name = r.get("Name")
            if oi is None or not name:
                continue
            out.setdefault(oi, {})[name] = r.get("Text", "")
    return out


def _pin_tip(d: dict) -> tuple[float, float]:
    """Canonical electrical-tip (mils) of a RECORD-2 pin = Location + PinLength*dir."""
    loc_x = coord(d, "Location.X")
    loc_y = coord(d, "Location.Y")
    length = coord(d, "PinLength") if "PinLength" in d else 0.0
    # PinLength has no _Frac twin in practice; coord() still assembles it cleanly.
    dx, dy = _DIRS[gi(d, "PinConglomerate", 0) & 3]
    return _canon(loc_x + length * dx, loc_y + length * dy)


def _build_components(
    recs: list[dict],
) -> tuple[list[Component], dict[int, Component]]:
    """Build the component list (with pins), keyed by record index."""
    desig = _designators(recs)
    fps = _footprints(recs)
    params = _parameters(recs)

    by_index: dict[int, Component] = {}
    components: list[Component] = []
    for idx, r in enumerate(recs):
        if _rid(r) != RECORD_COMPONENT:
            continue
        name = desig.get(idx)
        undesignated = name is None
        if undesignated:
            name = f"$U{idx}"  # synthesize -- a component is NEVER dropped
        rot = (gi(r, "Orientation", 0) & 3) * 90
        mirror = "x" if (r.get("IsMirrored", "") or "").upper() in _TRUE else "none"
        comp = Component(
            designator=name,
            library_ref=r.get("LibReference") or r.get("DesignItemId"),
            x_mil=_canon(coord(r, "Location.X"), coord(r, "Location.Y"))[0],
            y_mil=_canon(coord(r, "Location.X"), coord(r, "Location.Y"))[1],
            rotation=rot,
            mirror=mirror,
            value=r.get("Comment") or (params.get(idx, {}).get("Value")),
            # RECORD 45/46 model-link first; fall back to the RECORD-41 `Footprint` /
            # `Supplier Footprint` parameter (converter-generated parts leave the model-link
            # empty but write the footprint as a parameter).
            footprint=(
                fps.get(idx)
                or params.get(idx, {}).get("Footprint")
                or params.get(idx, {}).get("Supplier Footprint")
            ),
            unique_id=r.get("UniqueId"),
            part_count=gi(r, "PartCount", 1) or 1,
            parameters=params.get(idx, {}),
            undesignated=undesignated,
        )
        by_index[idx] = comp
        components.append(comp)

    # attach pins (RECORD 2) to their owner component
    for r in recs:
        if _rid(r) != RECORD_PIN:
            continue
        oi = gi(r, "OwnerIndex")
        owner = by_index.get(oi) if oi is not None else None
        if owner is None:
            continue
        tip_x, tip_y = _pin_tip(r)
        owner.pins.append(
            Pin(
                number=r.get("Designator", ""),
                name=r.get("Name"),
                x_mil=tip_x,
                y_mil=tip_y,
                electrical_type=pin_electrical_type(r),
                owner_part_id=gi(r, "OwnerPartId", 1) or 1,
                unique_id=r.get("UniqueId"),
            )
        )
    return components, by_index


# ---------------------------------------------------------------------------
# Net primitives
# ---------------------------------------------------------------------------
def _hier_key(sheet_ns: str, name: str) -> str:
    """Synthetic sheet-entry<->child-port connector text (never names a net)."""
    return f"\x02hier:{sheet_ns}:{name}"


def _build_primitives(
    recs: list[dict], by_index: dict[int, Component],
    prims: NetPrimitives | None = None, sheet: str = "",
    hier_mode: bool = False,
) -> NetPrimitives:
    """Emit wires/junctions/labels/power-ports/pins/No-ERC for net inference.

    ``sheet`` is the geometric namespace (the sheet-instance path; ``""`` root).
    ``hier_mode`` implements Altium's *Automatic* net-identifier scope: when the
    design contains sheet symbols, PORTs stop merging globally by name and
    instead pair with their own parent's matching sheet entry (the walker adds
    the entry-side connectors); flat designs keep the historical global-port
    behavior, so existing reads are unchanged.
    """
    prims = prims if prims is not None else NetPrimitives()

    for idx, r in enumerate(recs):
        rid = _rid(r)
        if rid == RECORD_WIRE:
            n = gi(r, "LocationCount", 0) or 0
            pts = [_canon(coord(r, f"X{k}"), coord(r, f"Y{k}")) for k in range(1, n + 1)]
            for a, b in zip(pts, pts[1:]):
                prims.wires.append(WireSeg(a=a, b=b, sheet=sheet))
        elif rid == RECORD_JUNCTION:
            prims.junctions.append(
                MJunction(at=_canon(coord(r, "Location.X"), coord(r, "Location.Y")), sheet=sheet)
            )
        elif rid in _LABEL_SCOPE:
            text = r.get("Text")
            if text:
                scope = _LABEL_SCOPE[rid]
                at = _canon(coord(r, "Location.X"), coord(r, "Location.Y"))
                if rid == RECORD_PORT and hier_mode:
                    # hierarchical scope: the port names its net locally and
                    # pairs ONLY with this sheet's own entry on the parent.
                    prims.labels.append(NetLabel(at=at, text=text,
                                                 scope="local", sheet=sheet))
                    prims.labels.append(NetLabel(at=at, text=_hier_key(sheet, text),
                                                 scope="hier", sheet=sheet))
                else:
                    prims.labels.append(NetLabel(at=at, text=text,
                                                 scope=scope, sheet=sheet))
        elif rid == RECORD_NO_ERC:
            prims.no_erc.append(_canon(coord(r, "Location.X"), coord(r, "Location.Y")))

    # pins: PinHandle keyed by (owner designator, pin number) at the electrical tip
    for r in recs:
        if _rid(r) != RECORD_PIN:
            continue
        oi = gi(r, "OwnerIndex")
        owner = by_index.get(oi) if oi is not None else None
        if owner is None:
            continue
        prims.pins.append(
            PinHandle(
                ref=(owner.designator, r.get("Designator", "")),
                at=_pin_tip(r),
                sheet=sheet,
            )
        )
    return prims


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _metadata(components: list[Component], nets, frac_present: bool) -> dict:
    pins = [p for c in components for p in c.pins]
    n_pins = len(pins)
    from ..model import PinType

    passive = sum(1 for p in pins if p.electrical_type is PinType.PASSIVE)
    return {
        "component_count": len(components),
        "pin_count": n_pins,
        "passive_pin_ratio": (passive / n_pins) if n_pins else 0.0,
        "unnamed_net_count": sum(1 for n in nets if not n.is_named),
        "undesignated_count": sum(1 for c in components if c.undesignated),
        "frac_present": frac_present,
    }


# Depth cap for sheet-symbol recursion (a real design is 2-4 deep).
_MAX_SHEET_DEPTH = 16

# RECORD-16 Side -> which sheet-symbol edge carries the entry.
_SIDE_LEFT, _SIDE_RIGHT, _SIDE_TOP, _SIDE_BOTTOM = 0, 1, 2, 3


def _sheet_children(recs: list[dict]) -> list[dict]:
    """Sheet symbols (RECORD 15) with their name/file (32/33) and entries (16).

    Entry positions follow the Altium convention: the symbol's Location is its
    TOP-LEFT corner in the +Y-up record frame (the body extends right and
    down), and ``DistanceFromTop`` counts from the side's origin in 1/10
    Location units. Scale validated against generated fixtures; flagged for
    confirmation against a real AD hierarchical design.
    """
    from ..units import altium_to_mil

    out = []
    for idx, r in enumerate(recs):
        if _rid(r) != RECORD_SHEET_SYMBOL:
            continue
        x0 = coord(r, "Location.X")
        y0 = coord(r, "Location.Y")
        w = coord(r, "XSize")
        h = coord(r, "YSize")
        child = {"index": idx, "name": None, "file": None, "entries": []}
        for r2 in recs:
            if gi(r2, "OwnerIndex") != idx:
                continue
            rid2 = _rid(r2)
            if rid2 == 32:
                child["name"] = r2.get("Text")
            elif rid2 == 33:
                child["file"] = r2.get("Text")
            elif rid2 == RECORD_SHEET_ENTRY:
                name = r2.get("Name") or r2.get("Text")
                if not name:
                    continue
                d = altium_to_mil(gi(r2, "DistanceFromTop", 0) * 10, 0)
                side = gi(r2, "Side", _SIDE_LEFT)
                if side == _SIDE_RIGHT:
                    pt = (x0 + w, y0 - d)
                elif side == _SIDE_TOP:
                    pt = (x0 + d, y0)
                elif side == _SIDE_BOTTOM:
                    pt = (x0 + d, y0 - h)
                else:
                    pt = (x0, y0 - d)
                child["entries"].append((name, _canon(*pt)))
        out.append(child)
    return out


def _read_hier(
    path: os.PathLike | str,
    *,
    power_priority: bool = False,
) -> Schematic:
    """Read a ``.SchDoc``, recursing into sheet symbols' child documents."""
    from pathlib import Path as _P

    root_path = _P(os.fspath(path))
    loaded: list[tuple[str, _P, list[dict], list[dict]]] = []  # ns, path, recs, children

    def _walk(p: _P, ns: str, ancestors: tuple[str, ...]) -> None:
        recs = _read_fileheader(p)
        children = _sheet_children(recs)
        loaded.append((ns, p, recs, children))
        for child in children:
            fname = child["file"]
            if not fname:
                continue
            child_path = (p.parent / fname.replace("\\", "/")).resolve()
            child_ns = f"{ns}/s{child['index']}"
            child["ns"] = child_ns
            if len(ancestors) >= _MAX_SHEET_DEPTH:
                fail("ALTIUM_MALFORMED",
                     f"sheet nesting deeper than {_MAX_SHEET_DEPTH} at {fname!r}")
            if str(child_path) in ancestors:
                fail("ALTIUM_MALFORMED",
                     f"sheet recursion: {child_path} includes itself via {p}")
            if not child_path.exists():
                raise FileNotFoundError(
                    f"{child_path} (sheet {child['name'] or fname!r} referenced from {p})")
            _walk(child_path, child_ns, ancestors + (str(child_path),))

    _walk(root_path, "", (str(root_path.resolve()),))

    hier_mode = any(c["entries"] or c["file"] for _, _, _, cs in loaded for c in cs)

    components: list[Component] = []
    prims = NetPrimitives()
    prims.power_priority = power_priority
    sheet_names: list[str] = []
    frac_present = False
    for ns, _p, recs, children in loaded:
        comps, by_index = _build_components(recs)
        for c in comps:
            c.sheet = ns
        components.extend(comps)
        _build_primitives(recs, by_index, prims, sheet=ns, hier_mode=hier_mode)
        frac_present = frac_present or any(k.endswith("_Frac") for r in recs for k in r)
        for child in children:
            if child.get("name"):
                sheet_names.append(child["name"])
            child_ns = child.get("ns")
            for name, pt in child["entries"]:
                # An entry is a CONNECTOR, not a net label: emit only the
                # never-naming hier pair to the child's same-named PORT. (A
                # raw-name local label here would wrongly merge same-named
                # entries of two different children on the parent sheet.)
                if child_ns:
                    prims.labels.append(NetLabel(at=pt,
                                                 text=_hier_key(child_ns, name),
                                                 scope="hier", sheet=ns))

    nets = netbuild.build_nets(prims)
    return Schematic(
        source_path=str(path),
        source_format="altium",
        components=components,
        nets=nets,
        sheets=sheet_names,
        no_erc_points=list(prims.no_erc),
        warnings=[],
        metadata=_metadata(components, nets, frac_present),
    )


def read(
    path: os.PathLike | str | bytes | bytearray,
    *,
    power_priority: bool = False,
) -> Schematic:
    """Read a ``.SchDoc`` into a normalized :class:`model.Schematic`.

    A path input recurses into sheet symbols (RECORD 15/16/32/33): child
    documents load relative to the parent file, each sheet instance is its own
    geometric namespace, and connectivity crosses sheets through sheet-entry↔
    child-port pairs (Altium's *Automatic* scope: PORTs merge globally only in
    designs WITHOUT sheet symbols), global labels excluded, power ports always
    global. Bytes input stays single-sheet (no filesystem to recurse into).
    """
    if isinstance(path, (str, os.PathLike)):
        return _read_hier(path, power_priority=power_priority)
    recs = _read_fileheader(path)
    components, by_index = _build_components(recs)
    prims = _build_primitives(recs, by_index)
    prims.power_priority = power_priority
    nets = netbuild.build_nets(prims)
    frac_present = any(k.endswith("_Frac") for r in recs for k in r)
    return Schematic(
        source_path="<bytes>",
        source_format="altium",
        components=components,
        nets=nets,
        sheets=[],
        no_erc_points=list(prims.no_erc),
        warnings=[],
        metadata=_metadata(components, nets, frac_present),
    )


def read_primitives(path: os.PathLike | str | bytes | bytearray) -> NetPrimitives:
    """Read a ``.SchDoc`` into raw :class:`model.NetPrimitives` (pre-netbuild)."""
    recs = _read_fileheader(path)
    _, by_index = _build_components(recs)
    return _build_primitives(recs, by_index)


__all__ = ["read", "read_primitives"]
