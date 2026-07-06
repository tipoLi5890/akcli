"""``.kicad_sch`` / ``.kicad_pcb`` -> :class:`model.Schematic` / :class:`model.Pcb`
(SPEC §3.4).

A KiCad schematic is an S-expression document (parsed by :mod:`.sexpr`). Two
facts drive this reader:

* **Pin electrical types live in the library, not the instance.** A placed
  ``(symbol ... (pin "1" (uuid ...)))`` carries only a pin *number*; the type
  (passive / power_in / ...) is resolved from the document's inline
  ``(lib_symbols ...)`` cache via :mod:`.kicad_lib` at read time.
* **KiCad is already +Y-down**, so — unlike the Altium reader — coordinates are
  converted mm->mil with **no Y flip**. The only flip is the per-symbol library
  convention (+Y up) applied when computing a pin's world position from its
  symbol-local offset.

Net inference is **shared with Altium**: this reader emits
:class:`model.NetPrimitives` (wires / junctions / labels / power-port pseudo-
labels / pins / no-connects) and hands them to :func:`netbuild.build_nets`, so
the same-name merge / junction / T-junction logic is written exactly once.
"""

from __future__ import annotations

import os

from .. import model, netbuild, units
from ..errors import fail
from ..model import (
    Component,
    NetLabel,
    NetPrimitives,
    Pin,
    PinHandle,
    Pcb,
    Schematic,
    WireSeg,
)
from ..model import Footprint as MFootprint
from ..model import Junction as MJunction
from . import kicad_lib, sexpr
from .kicad_lib import _read_text

__all__ = ["read_sch", "read_pcb", "read_primitives"]


def _mm_to_mil(mm: float) -> float:
    """mm -> mil via integer nanometres (no Y flip; KiCad is already +Y down)."""
    return units.nm_to_mil(units.mm_to_nm(mm))


def _av(node: sexpr.SNode | None, idx: int) -> str | None:
    """Atom value of child ``idx`` (or ``None``)."""
    if node is not None and node.children and 0 <= idx < len(node.children):
        c = node.children[idx]
        if c.is_atom:
            return c.value
    return None


def _fnum(node: sexpr.SNode | None, idx: int, default: float = 0.0) -> float:
    v = _av(node, idx)
    return float(v) if v is not None else default


# ---------------------------------------------------------------------------
# lib_symbols cache helpers
# ---------------------------------------------------------------------------
def _raw_lib_nodes(root: sexpr.SNode) -> dict[str, sexpr.SNode]:
    """Map cached ``lib_id`` -> raw ``(symbol ...)`` node (for power detection)."""
    out: dict[str, sexpr.SNode] = {}
    libsym = root.find("lib_symbols")
    if libsym is not None:
        for s in libsym.find_all("symbol"):
            name = _av(s, 1)
            if name:
                out[name] = s
    return out


def _is_power(lib_id: str, raw: dict[str, sexpr.SNode]) -> bool:
    """True when ``lib_id`` (or its extends base) is a KiCad ``(power)`` symbol."""
    seen: set[str] = set()
    cur: str | None = lib_id
    while cur and cur not in seen:
        seen.add(cur)
        node = raw.get(cur)
        if node is None:
            # tolerate a qualified/unqualified mismatch on the extends base name
            tail = cur.split(":")[-1]
            node = next((n for k, n in raw.items() if k.split(":")[-1] == tail), None)
        if node is None:
            return False
        if node.find("power") is not None:
            return True
        ext = node.find("extends")
        cur = _av(ext, 1) if ext is not None else None
    return False


# ---------------------------------------------------------------------------
# instance transform
# ---------------------------------------------------------------------------
def _pin_world(
    lx_mil: float,
    ly_mil: float,
    px_mil: float,
    py_mil: float,
    rot_deg: int,
    mirror: str,
) -> tuple[float, float]:
    """World coords (mil, +Y down) of a symbol-local pin offset.

    Library pins are +Y up, so the base case (rotation 0, no mirror) is the
    KiCad library flip ``(px + lx, py - ly)`` — this is the exact path the
    fixtures (rotation 0 only) exercise. Rotation 0/90/180/270 and mirror x/y
    are applied in the schematic (+Y-down) frame for completeness.
    """
    x, y = lx_mil, -ly_mil  # library (+Y up) -> schematic (+Y down)
    if mirror == "x":       # mirror across the X axis -> flip Y
        y = -y
    elif mirror == "y":     # mirror across the Y axis -> flip X
        x = -x
    r = rot_deg % 360
    if r == 90:
        x, y = -y, x
    elif r == 180:
        x, y = -x, -y
    elif r == 270:
        x, y = y, -x
    return px_mil + x, py_mil + y


def _props(sym: sexpr.SNode) -> dict[str, str]:
    """``{property-name: value}`` for a placed ``(symbol ...)`` instance."""
    out: dict[str, str] = {}
    for p in sym.find_all("property"):
        key = _av(p, 1)
        val = _av(p, 2)
        if key is not None and val is not None:
            out[key] = val
    return out


def _reference(sym: sexpr.SNode, props: dict[str, str]) -> str | None:
    """Reference designator from the ``(instances ...)`` block, else property."""
    inst = sym.find("instances")
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                ref = path.find("reference")
                rv = _av(ref, 1) if ref is not None else None
                if rv:
                    return rv
    return props.get("Reference")


# ---------------------------------------------------------------------------
# component + primitive extraction
# ---------------------------------------------------------------------------
def _placed_symbols(root: sexpr.SNode) -> list[sexpr.SNode]:
    """Top-level ``(symbol ...)`` instances (those with a ``(lib_id ...)``)."""
    return [s for s in root.find_all("symbol") if s.find("lib_id") is not None]


def _build(root: sexpr.SNode) -> tuple[list[Component], NetPrimitives]:
    """Resolve instances + pin types and emit components and net primitives."""
    libsym = root.find("lib_symbols")
    library = (
        kicad_lib.library_from_lib_symbols(libsym)
        if libsym is not None
        else model.Library(source_path="<inline>", source_format="kicad", symbols=[])
    )
    raw = _raw_lib_nodes(root)

    components: list[Component] = []
    by_designator: dict[str, Component] = {}
    prims = NetPrimitives()
    sheet = ""

    for idx, sym in enumerate(_placed_symbols(root)):
        lib_id = _av(sym.find("lib_id"), 1) or ""
        at = sym.find("at")
        px = _mm_to_mil(_fnum(at, 1))
        py = _mm_to_mil(_fnum(at, 2))
        rot = int(round(_fnum(at, 3)))
        mnode = sym.find("mirror")
        mirror = (_av(mnode, 1) if mnode is not None else None) or "none"
        unit = int(_fnum(sym.find("unit"), 1, 1.0))

        props = _props(sym)
        ref = _reference(sym, props)
        undesignated = ref is None
        if undesignated:
            ref = f"$U{idx}"

        symdef = kicad_lib.resolve(lib_id, [library])
        # A multi-unit part is several placed instances sharing one designator
        # (unit A..E of a 74xx). Merge them into ONE component; each instance
        # contributes only ITS unit's pins (eeschema draws and connects only
        # those — treating every unit's pins as present at every instance
        # mapped all four gates onto one body and merged unrelated nets).
        pins = kicad_lib.unit_pins(symdef, unit)
        existing = None if undesignated else by_designator.get(ref)
        if existing is not None and existing.library_ref == lib_id:
            comp = existing
        else:
            fp = props.get("Footprint") or None
            comp = Component(
                designator=ref,
                library_ref=lib_id,
                x_mil=px,
                y_mil=py,
                rotation=rot % 360,
                mirror=mirror,
                value=props.get("Value") or None,
                footprint=fp,
                unique_id=_av(sym.find("uuid"), 1),
                part_count=symdef.part_count,
                sheet=sheet,
                parameters=dict(props),
                undesignated=undesignated,
            )
            components.append(comp)
            if not undesignated:
                by_designator[ref] = comp

        for lp in pins:
            wx, wy = _pin_world(lp.x_mil, lp.y_mil, px, py, rot, mirror)
            comp.pins.append(
                Pin(
                    number=lp.number,
                    name=lp.name,
                    x_mil=wx,
                    y_mil=wy,
                    electrical_type=lp.electrical_type,
                    owner_part_id=lp.owner_part_id,
                )
            )
            prims.pins.append(
                PinHandle(ref=(ref, lp.number), at=(wx, wy), sheet=sheet)
            )

        # A power symbol injects a global (power-scoped) net name at its pin.
        if _is_power(lib_id, raw) and comp.pins:
            net_name = props.get("Value") or lib_id.split(":")[-1]
            ppin = comp.pins[0]
            prims.labels.append(
                NetLabel(
                    at=(ppin.x_mil, ppin.y_mil),
                    text=net_name,
                    scope="power",
                    sheet=sheet,
                )
            )

    _collect_wires_labels(root, prims, sheet)
    return components, prims


def _collect_wires_labels(
    root: sexpr.SNode, prims: NetPrimitives, sheet: str
) -> None:
    """Emit ``(wire)`` / ``(junction)`` / labels / ``(no_connect)`` primitives."""
    for w in root.find_all("wire"):
        pts = w.find("pts")
        if pts is None:
            continue
        xys = pts.find_all("xy")
        coords = [(_mm_to_mil(_fnum(p, 1)), _mm_to_mil(_fnum(p, 2))) for p in xys]
        for a, b in zip(coords, coords[1:]):
            prims.wires.append(WireSeg(a=a, b=b, sheet=sheet))

    for j in root.find_all("junction"):
        at = j.find("at")
        prims.junctions.append(
            MJunction(at=(_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2))), sheet=sheet)
        )

    # local / global / hierarchical labels -> net names (hierarchical is sheet-local).
    for tag, scope in (
        ("label", "local"),
        ("global_label", "global"),
        ("hierarchical_label", "local"),
    ):
        for lb in root.find_all(tag):
            text = _av(lb, 1)
            if not text:
                continue
            at = lb.find("at")
            prims.labels.append(
                NetLabel(
                    at=(_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2))),
                    text=text,
                    scope=scope,
                    sheet=sheet,
                )
            )

    for nc in root.find_all("no_connect"):
        at = nc.find("at")
        prims.no_erc.append(
            (_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2)))
        )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def _metadata(components: list[Component], nets: list[model.Net]) -> dict:
    pins = [p for c in components for p in c.pins]
    n = len(pins)
    passive = sum(1 for p in pins if p.electrical_type is model.PinType.PASSIVE)
    return {
        "component_count": len(components),
        "pin_count": n,
        "passive_pin_ratio": (passive / n) if n else 0.0,
        "unnamed_net_count": sum(1 for net in nets if not net.is_named),
        "undesignated_count": sum(1 for c in components if c.undesignated),
        "frac_present": False,
    }


def _parse_root(path: os.PathLike | str, expect: str) -> sexpr.SNode:
    root = sexpr.parse(_read_text(path))
    if root.tag != expect:
        fail("ALTIUM_MALFORMED", f"not a {expect}: root tag {root.tag!r}")
    return root


def read_sch(path: os.PathLike | str) -> Schematic:
    """Read a ``.kicad_sch`` into a normalized :class:`model.Schematic`."""
    root = _parse_root(path, "kicad_sch")
    components, prims = _build(root)
    nets = netbuild.build_nets(prims)
    return Schematic(
        source_path=str(path),
        source_format="kicad",
        components=components,
        nets=nets,
        sheets=[],
        no_erc_points=list(prims.no_erc),
        warnings=[],
        metadata=_metadata(components, nets),
    )


def read_primitives(path: os.PathLike | str) -> NetPrimitives:
    """Read a ``.kicad_sch`` into raw :class:`model.NetPrimitives` (pre-netbuild)."""
    root = _parse_root(path, "kicad_sch")
    _, prims = _build(root)
    return prims


def read_pcb(path: os.PathLike | str) -> Pcb:
    """Read a ``.kicad_pcb`` into a :class:`model.Pcb` (footprints + net names)."""
    root = _parse_root(path, "kicad_pcb")

    nets: list[str] = []
    for net in root.find_all("net"):
        name = _av(net, 2)
        if name:  # net 0 is the unconnected pseudo-net ("")
            nets.append(name)

    footprints: list[MFootprint] = []
    for fp in root.find_all("footprint"):
        fp_name = _av(fp, 1)
        props = _props(fp)
        ref = props.get("Reference")
        value = props.get("Value")
        if ref is None or value is None:
            # fall back to legacy (fp_text reference/value "...") nodes
            for ft in fp.find_all("fp_text"):
                kind = _av(ft, 1)
                txt = _av(ft, 2)
                if kind == "reference" and ref is None:
                    ref = txt
                elif kind == "value" and value is None:
                    value = txt
        layer = _av(fp.find("layer"), 1)
        at = fp.find("at")
        rot = _fnum(at, 3) if at is not None else 0.0
        footprints.append(
            MFootprint(
                designator=ref or "",
                footprint_name=fp_name,
                layer=layer,
                rotation=rot,
                value=value,
            )
        )

    return Pcb(
        source_path=str(path),
        source_format="kicad",
        nets=nets,
        footprints=footprints,
    )
