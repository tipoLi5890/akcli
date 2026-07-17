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
from pathlib import Path

from .. import model, netbuild, units
from ..errors import fail
from ..kicad_escape import unescape_string
from ..writers import geometry
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
from ..model import BusEntry as MBusEntry
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
    """Map cached ``lib_id`` -> raw ``(symbol ...)`` node (for power detection).

    Keys are **unescaped** to match the unescaped ``lib_id`` used at call sites.
    """
    out: dict[str, sexpr.SNode] = {}
    libsym = root.find("lib_symbols")
    if libsym is not None:
        for s in libsym.find_all("symbol"):
            name = unescape_string(_av(s, 1))
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

    Delegates to :func:`writers.geometry.transform_point` — the single,
    eeschema-verified transform (library +Y-up flip, then rotate-then-mirror;
    see that module's docstring and ``tests/test_kicad_parity.py``). The
    reader used to keep its own mirror-then-rotate copy, which disagreed with
    both the writer and eeschema on part of the rot × mirror matrix. Math is
    exact in integer nanometres; a non-right angle is snapped to the nearest
    90° (eeschema only stores right angles for symbol instances).
    """
    local = (units.mil_to_nm(lx_mil), -units.mil_to_nm(ly_mil))
    origin = (units.mil_to_nm(px_mil), units.mil_to_nm(py_mil))
    rot = (round(rot_deg / 90) * 90) % 360
    wx, wy = geometry.transform_point(local, rot, mirror, origin)
    return units.nm_to_mil(wx), units.nm_to_mil(wy)


def _flag(sym: sexpr.SNode, tag: str, default: bool) -> bool:
    """A KiCad yes/no assembly flag (``(dnp yes)``); absent -> ``default``.

    Older files (and akcli-authored sheets) omit these tokens entirely —
    absence means the KiCad default, never a guess.
    """
    node = sym.find(tag)
    if node is None or len(node.children or []) < 2:
        return default
    return str(node.children[1].value).strip().casefold() == "yes"


def _props(sym: sexpr.SNode) -> dict[str, str]:
    """``{property-name: value}`` for a placed ``(symbol ...)`` instance."""
    out: dict[str, str] = {}
    for p in sym.find_all("property"):
        key = _av(p, 1)
        val = _av(p, 2)
        if key is not None and val is not None:
            out[key] = val
    return out


def _reference(
    sym: sexpr.SNode, props: dict[str, str], want_path: str | None = None
) -> str | None:
    """Reference designator from the ``(instances ...)`` block, else property.

    ``want_path`` is the current sheet-instance path (``/<root>/<sheet>...``): a
    file instantiated twice carries one reference PER path, so the exact match
    must win — falling back to the first entry, then the Reference property.
    """
    inst = sym.find("instances")
    first: str | None = None
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                ref = path.find("reference")
                rv = _av(ref, 1) if ref is not None else None
                if not rv:
                    continue
                if want_path is not None and (_av(path, 1) or "") == want_path:
                    return rv
                if first is None:
                    first = rv
    return first or props.get("Reference")


# ---------------------------------------------------------------------------
# component + primitive extraction
# ---------------------------------------------------------------------------
def _placed_symbols(root: sexpr.SNode) -> list[sexpr.SNode]:
    """Top-level ``(symbol ...)`` instances (those with a ``(lib_id ...)``)."""
    return [s for s in root.find_all("symbol") if s.find("lib_id") is not None]


def _build(root: sexpr.SNode) -> tuple[list[Component], NetPrimitives]:
    """Single-file build (no sheet recursion) — see :func:`_build_hierarchy`."""
    components: list[Component] = []
    prims = NetPrimitives()
    _build_file(root, components, {}, prims, sheet="", want_path=None,
                warnings=[])
    return components, prims


def _build_file(
    root: sexpr.SNode,
    components: list[Component],
    by_designator: dict[str, tuple[Component, set[int]]],
    prims: NetPrimitives,
    sheet: str,
    want_path: str | None,
    warnings: list[str],
) -> None:
    """Emit ONE file's components/primitives into the shared accumulators.

    ``sheet`` is the geometric namespace (the sheet-instance path; ``""`` for
    the root) — two sheets never connect by coordinates, only by name/hier.

    ``by_designator`` maps a designator to ``(component, contributed_units)``:
    a further placement of the same designator + lib_id merges into that
    component only when it contributes a NEW unit (a multi-unit part). A
    placement whose unit was already contributed is a genuine duplicate
    designator; it becomes a SEPARATE component under the same designator
    (distinct ``unique_id``, so ``checks/bom.py`` BOM_DUPLICATE_DESIGNATOR
    fires) plus a reader warning — eeschema also keeps the shared reference
    on both placements' netlist nodes, so pin refs stay under the raw ref.
    """
    libsym = root.find("lib_symbols")
    library = (
        kicad_lib.library_from_lib_symbols(libsym)
        if libsym is not None
        else model.Library(source_path="<inline>", source_format="kicad", symbols=[])
    )
    raw = _raw_lib_nodes(root)

    for idx, sym in enumerate(_placed_symbols(root)):
        lib_id = unescape_string(_av(sym.find("lib_id"), 1)) or ""
        at = sym.find("at")
        px = _mm_to_mil(_fnum(at, 1))
        py = _mm_to_mil(_fnum(at, 2))
        rot = int(round(_fnum(at, 3)))
        mnode = sym.find("mirror")
        mirror = (_av(mnode, 1) if mnode is not None else None) or "none"
        unit = int(_fnum(sym.find("unit"), 1, 1.0))

        props = _props(sym)
        ref = _reference(sym, props, want_path)
        undesignated = ref is None
        if undesignated:
            ref = f"$U{idx}" if not sheet else f"$U{idx}@{sheet}"

        symdef = kicad_lib.resolve(lib_id, [library])
        # A multi-unit part is several placed instances sharing one designator
        # (unit A..E of a 74xx). Merge them into ONE component; each instance
        # contributes only ITS unit's pins (eeschema draws and connects only
        # those — treating every unit's pins as present at every instance
        # mapped all four gates onto one body and merged unrelated nets).
        pins = kicad_lib.unit_pins(symdef, unit)
        entry = None if undesignated else by_designator.get(ref)
        is_dup = False
        if (entry is not None and entry[0].library_ref == lib_id
                and unit not in entry[1]):
            comp, seen_units = entry
            seen_units.add(unit)
        else:
            is_dup = (entry is not None and entry[0].library_ref == lib_id)
            fp = unescape_string(props.get("Footprint")) or None
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
                dnp=_flag(sym, "dnp", False),
                in_bom=_flag(sym, "in_bom", True),
                on_board=_flag(sym, "on_board", True),
            )
            components.append(comp)
            if not undesignated and entry is None:
                by_designator[ref] = (comp, {unit})
            if is_dup:
                # Flag the collision on the component and warn; the shared
                # designator is kept so BOM duplicate detection (distinct
                # unique_ids under one refdes) fires and pin refs match
                # eeschema's netlist nodes.
                ndup = sum(
                    1 for c in components
                    if c.designator == ref and c.parameters.get("akcli_duplicate")
                ) + 1
                comp.parameters["akcli_duplicate"] = f"{ref}@dup{ndup}"
                warnings.append(
                    f"duplicate designator {ref!r}: unit {unit} of {lib_id!r} "
                    f"is placed more than once; the extra placement is kept as "
                    f"a distinct component ({ref}@dup{ndup}) — re-annotate"
                )

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

        # A power symbol injects a global (power-scoped) net name at its pin —
        # EXCEPT PWR_FLAG, which is a power symbol that only marks a net as
        # driven for ERC and must NOT name/merge a net (KiCad excludes it). Its
        # pin is already emitted above, so it stays electrically on whatever net
        # it is wired to; injecting a "PWR_FLAG" name here would union every rail
        # that carries a flag into one net (a false +5V↔GND short).
        if _is_power(lib_id, raw) and comp.pins:
            sym_name = lib_id.split(":")[-1]
            net_name = props.get("Value") or sym_name
            if sym_name.upper() != "PWR_FLAG" and net_name.upper() != "PWR_FLAG":
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


def _collect_wires_labels(
    root: sexpr.SNode, prims: NetPrimitives, sheet: str
) -> None:
    """Emit wire/bus/bus_entry/junction/label/``(no_connect)`` primitives."""
    for tag, dest in (("wire", prims.wires), ("bus", prims.buses)):
        for w in root.find_all(tag):
            pts = w.find("pts")
            if pts is None:
                continue
            xys = pts.find_all("xy")
            coords = [
                (_mm_to_mil(_fnum(p, 1)), _mm_to_mil(_fnum(p, 2))) for p in xys
            ]
            for a, b in zip(coords, coords[1:]):
                dest.append(WireSeg(a=a, b=b, sheet=sheet))

    # (bus_entry): end a = (at), end b = (at)+(size); a missing (size) is a
    # degenerate entry with both ends coincident (mirrors the draw gate).
    for be in root.find_all("bus_entry"):
        at = be.find("at")
        size = be.find("size")
        ax, ay = _fnum(at, 1), _fnum(at, 2)
        sx = _fnum(size, 1) if size is not None else 0.0
        sy = _fnum(size, 2) if size is not None else 0.0
        prims.bus_entries.append(
            MBusEntry(
                a=(_mm_to_mil(ax), _mm_to_mil(ay)),
                b=(_mm_to_mil(ax + sx), _mm_to_mil(ay + sy)),
                sheet=sheet,
            )
        )

    for j in root.find_all("junction"):
        at = j.find("at")
        prims.junctions.append(
            MJunction(at=(_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2))), sheet=sheet)
        )

    # local / global / hierarchical labels -> net names. A hierarchical label
    # names its net sheet-LOCALLY and additionally emits a synthetic "hier"
    # connector that pairs with the matching sheet pin on the PARENT (the
    # connector text is unique per sheet instance, so nothing else merges).
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
            pos = (_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2)))
            prims.labels.append(
                NetLabel(at=pos, text=text, scope=scope, sheet=sheet)
            )
            if tag == "hierarchical_label":
                prims.labels.append(
                    NetLabel(
                        at=pos,
                        text=_hier_key(sheet, text),
                        scope="hier",
                        sheet=sheet,
                    )
                )

    # (bus_alias ...) is intentionally NOT read into a primitive. kicad-cli
    # 10.0.4 ignores bus aliases in netlist export: a bus labeled with an alias
    # name behaves exactly like a plain, member-less bus (the netlist is
    # identical with or without the declaration), and an alias name that is
    # itself a vector expands as the vector. netbuild already reproduces this
    # for free — non-vector labels carry no members — so expanding aliases here
    # would only DIVERGE from eeschema. Verdicts locked in tests/test_bus_alias.py
    # and tests/test_kicad_parity.py section (g).

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


# Depth cap for sheet recursion (a real design is 2-4 deep; runaway = cycle).
_MAX_SHEET_DEPTH = 16


def _hier_key(sheet_instance: str, pin_name: str) -> str:
    """Synthetic connector text pairing a sheet pin with its child's label.

    Unique per (sheet instance, name): two different sheets each exposing an
    ``OUT`` pin must NOT merge — KiCad hierarchy is strictly parent<->child,
    unlike global labels. The \x02 prefix keeps it out of any real namespace.
    """
    return f"\x02hier:{sheet_instance}:{pin_name}"


def _walk_sheets(
    root: sexpr.SNode,
    file_path: str,
    root_uuid: str,
    components: list[Component],
    by_designator: dict,
    prims: NetPrimitives,
    sheet_names: list[str],
    inst_path: str,
    ancestors: tuple[str, ...],
    warnings: list[str],
) -> None:
    """Recursively read ``(sheet ...)`` children (cycle- and depth-guarded)."""
    geom_sheet = inst_path  # "" for the root file
    _build_file(root, components, by_designator, prims, geom_sheet,
                want_path=f"/{root_uuid}{inst_path}" if root_uuid else None,
                warnings=warnings)

    for sh in root.find_all("sheet"):
        suuid = _av(sh.find("uuid"), 1) or ""
        props = _props(sh)
        sname = props.get("Sheetname") or props.get("Sheet name") or suuid
        sfile = props.get("Sheetfile") or props.get("Sheet file")
        child_inst = f"{inst_path}/{suuid}"

        # Parent-side connectors: one per sheet pin, at the pin's anchor.
        for pin in sh.find_all("pin"):
            pname = _av(pin, 1)
            if not pname:
                continue
            at = pin.find("at")
            prims.labels.append(
                NetLabel(
                    at=(_mm_to_mil(_fnum(at, 1)), _mm_to_mil(_fnum(at, 2))),
                    text=_hier_key(child_inst, pname),
                    scope="hier",
                    sheet=geom_sheet,
                )
            )

        if not sfile:
            continue
        sheet_names.append(sname)
        if len(ancestors) >= _MAX_SHEET_DEPTH:
            fail("ALTIUM_MALFORMED",
                 f"sheet nesting deeper than {_MAX_SHEET_DEPTH} at {sfile!r} (cycle?)")
        child_path = (Path(os.fspath(file_path)).parent / sfile).resolve()
        if str(child_path) in ancestors:
            fail("ALTIUM_MALFORMED",
                 f"sheet recursion: {child_path} includes itself via {file_path}")
        if not child_path.exists():
            raise FileNotFoundError(
                f"{child_path} (sheet {sname!r} referenced from {file_path})")
        child_root = _parse_root(child_path, "kicad_sch")
        _walk_sheets(child_root, str(child_path), root_uuid, components,
                     by_designator, prims, sheet_names, child_inst,
                     ancestors + (str(child_path),), warnings)


def read_sch(path: os.PathLike | str) -> Schematic:
    """Read a ``.kicad_sch`` (recursing into hierarchical sheets) into a
    normalized :class:`model.Schematic`.

    Child sheets load relative to their parent file. Each sheet INSTANCE is its
    own geometric namespace: a file instantiated twice contributes its
    components once per instance, designators resolved from the matching
    ``(instances (path ...))`` entry. Connectivity crosses sheets only through
    sheet-pin<->hierarchical-label pairs, global labels, and power ports.
    """
    root = _parse_root(path, "kicad_sch")
    root_uuid = _av(root.find("uuid"), 1) or ""
    components: list[Component] = []
    prims = NetPrimitives()
    sheet_names: list[str] = []
    warnings: list[str] = []
    _walk_sheets(root, os.fspath(path), root_uuid, components, {}, prims,
                 sheet_names, inst_path="",
                 ancestors=(str(Path(os.fspath(path)).resolve()),),
                 warnings=warnings)
    # eeschema dialect: a wire end on another wire's mid-span joins only
    # through an explicit junction node (kicad-cli-verified; see netbuild).
    nets = netbuild.build_nets(prims, t_midspan_connects=False)
    return Schematic(
        source_path=str(path),
        source_format="kicad",
        components=components,
        nets=nets,
        sheets=sheet_names,
        no_erc_points=list(prims.no_erc),
        warnings=warnings,
        metadata=_metadata(components, nets),
    )


def read_primitives(path: os.PathLike | str) -> NetPrimitives:
    """Read a ``.kicad_sch`` into raw :class:`model.NetPrimitives` (pre-netbuild).

    Single-file view (no sheet recursion). A caller reproducing
    :func:`read_sch`'s nets must run ``netbuild.build_nets(prims,
    t_midspan_connects=False)`` — the eeschema dialect.
    """
    root = _parse_root(path, "kicad_sch")
    _, prims = _build(root)
    return prims


def _rot_ccw(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a footprint-frame offset by ``deg`` CCW-on-screen (KiCad, +Y down)."""
    import math
    if not deg:
        return x, y
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _node_net_name(node, net_by_idx: dict[str, str]) -> str | None:
    """Resolve a ``(net ...)`` child to a net NAME.

    Two dialects: ``(net 2 "+3V3")`` (index + name, v6-v9 boards) and
    ``(net "GND")`` (name only, KiCad 10 pads). A quoted single argument is a
    name; an unquoted one is an index into the board's net table.
    """
    net_node = node.find("net")
    if net_node is None or len(net_node.children or ()) < 2:
        return None
    if len(net_node.children) >= 3 and net_node.children[2].is_atom:
        return net_node.children[2].value or None
    arg = net_node.children[1]
    if not arg.is_atom:
        return None
    if (arg.text or "").startswith('"'):
        return arg.value or None
    return net_by_idx.get(arg.value or "")


def _pcb_pad(pad, ref: str, fx: float, fy: float, frot: float,
             fp_layer: str | None, net_by_idx: dict[str, str]) -> dict:
    """One footprint pad -> a board-frame pad dict (mm, +Y down)."""
    at = pad.find("at")
    lx, ly = _fnum(at, 1), _fnum(at, 2)
    ax, ay = _rot_ccw(lx, ly, frot)
    size = pad.find("size")
    layers_node = pad.find("layers")
    layers = [c.value for c in (layers_node.children or ())[1:]
              if c.is_atom and c.value] if layers_node is not None else []
    drill = pad.find("drill")
    drill_mm = None
    if drill is not None:
        v = _av(drill, 1)
        if v == "oval":
            v = _av(drill, 2)
        try:
            drill_mm = float(v) if v is not None else None
        except (TypeError, ValueError):
            drill_mm = None
    return {
        "component": ref,
        "number": _av(pad, 1) or "",
        "pad_type": _av(pad, 2) or "smd",
        "shape": _av(pad, 3) or "rect",
        "at": (round(fx + ax, 6), round(fy + ay, 6)),
        "size": (_fnum(size, 1), _fnum(size, 2)),
        "rotation": _fnum(at, 3),   # absolute (KiCad stores fp rot folded in)
        "layers": layers,
        "footprint_layer": fp_layer,
        "drill": drill_mm,
        "net": _node_net_name(pad, net_by_idx),
    }


def read_pcb(path: os.PathLike | str) -> Pcb:
    """Read a ``.kicad_pcb`` into a :class:`model.Pcb`.

    Beyond footprints + net names (schema 1.0), this decodes — additively —
    pad-level net bindings, tracks, vias, zones and the board setup
    (schema 1.2). PCB-side geometry stays in **KiCad's native frame: mm,
    +Y down** (``board["units"] == "mm"``), unlike the Altium PCB reader
    which keeps mils; consumers must check the source_format.
    """
    root = _parse_root(path, "kicad_pcb")

    nets: list[str] = []
    net_by_idx: dict[str, str] = {}
    for net in root.find_all("net"):
        idx, name = _av(net, 1), _av(net, 2)
        if name:  # net 0 is the unconnected pseudo-net ("")
            nets.append(name)
            if idx is not None:
                net_by_idx[idx] = name

    def _net_ref(node) -> str | None:
        return _node_net_name(node, net_by_idx)

    footprints: list[MFootprint] = []
    pads: list[dict] = []
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
        fx, fy = (_fnum(at, 1), _fnum(at, 2)) if at is not None else (0.0, 0.0)
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
        for pad in fp.find_all("pad"):
            pads.append(_pcb_pad(pad, ref or "", fx, fy, rot, layer, net_by_idx))

    tracks: list[dict] = []
    for seg in root.find_all("segment"):
        start, end = seg.find("start"), seg.find("end")
        tracks.append({
            "start": (_fnum(start, 1), _fnum(start, 2)),
            "end": (_fnum(end, 1), _fnum(end, 2)),
            "width": _fnum(seg.find("width"), 1),
            "layer": _av(seg.find("layer"), 1),
            "net": _net_ref(seg),
        })

    vias: list[dict] = []
    for via in root.find_all("via"):
        kind = "through"
        for c in (via.children or ())[1:]:
            if c.is_atom and c.value in ("blind", "micro"):
                kind = c.value
                break
        at = via.find("at")
        layers_node = via.find("layers")
        vlayers = [c.value for c in (layers_node.children or ())[1:]
                   if c.is_atom and c.value] if layers_node is not None else []
        vias.append({
            "at": (_fnum(at, 1), _fnum(at, 2)),
            "size": _fnum(via.find("size"), 1),
            "drill": _fnum(via.find("drill"), 1),
            "layers": vlayers,
            "type": kind,
            "net": _net_ref(via),
        })

    zones: list[dict] = []
    for zone in root.find_all("zone"):
        zlayers = []
        lnode = zone.find("layer") or zone.find("layers")
        if lnode is not None:
            zlayers = [c.value for c in (lnode.children or ())[1:]
                       if c.is_atom and c.value]
        xs: list[float] = []
        ys: list[float] = []
        poly = zone.find("polygon")
        if poly is not None:
            pts = poly.find("pts")
            for xy in (pts.find_all("xy") if pts is not None else []):
                xs.append(_fnum(xy, 1))
                ys.append(_fnum(xy, 2))
        zones.append({
            "net": _net_ref(zone),
            "layers": zlayers,
            "name": _av(zone.find("name"), 1),
            "bbox": ((min(xs), min(ys)), (max(xs), max(ys))) if xs else None,
        })

    board: dict = {"units": "mm"}
    general = root.find("general")
    if general is not None:
        board["thickness"] = _fnum(general.find("thickness"), 1)
    layers_node = root.find("layers")
    if layers_node is not None:
        copper = []
        all_layers = []
        for entry in layers_node.children or ():
            if not entry.is_list or len(entry.children) < 3:
                continue
            lname = entry.children[1].value
            ltype = entry.children[2].value
            all_layers.append({"name": lname, "type": ltype})
            if ltype in ("signal", "power", "mixed") and lname and lname.endswith(".Cu"):
                copper.append(lname)
        board["layers"] = all_layers
        board["copper_layers"] = copper
    setup = root.find("setup")
    if setup is not None:
        setup_vals: dict = {}
        for child in setup.children or ():
            if not child.is_list or not child.tag:
                continue
            vals = [c.value for c in (child.children or ())[1:] if c.is_atom]
            if len(vals) == 1:
                setup_vals[child.tag] = vals[0]
            elif vals:
                setup_vals[child.tag] = vals
        board["setup"] = setup_vals

    # Edge.Cuts extent (board outline bbox) from gr_lines/gr_rects/gr_arcs.
    exs: list[float] = []
    eys: list[float] = []
    for gr in list(root.find_all("gr_line")) + list(root.find_all("gr_rect")):
        if _av(gr.find("layer"), 1) != "Edge.Cuts":
            continue
        for tag in ("start", "end"):
            node = gr.find(tag)
            if node is not None:
                exs.append(_fnum(node, 1))
                eys.append(_fnum(node, 2))
    if exs:
        board["outline_bbox"] = ((min(exs), min(eys)), (max(exs), max(eys)))

    return Pcb(
        source_path=str(path),
        source_format="kicad",
        nets=nets,
        footprints=footprints,
        pads=pads,
        tracks=tracks,
        vias=vias,
        zones=zones,
        board=board,
    )
