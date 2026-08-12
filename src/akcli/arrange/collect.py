"""Symbol collection shared by the arrange planners and group frames.

``collect`` walks a parsed sheet once and returns every placed symbol's
world box (body ∪ pin tips — the same math ``check --layout`` judges by)
plus every wire/label anchor point.  Split out of the old ``arrange.py`` so
``frames.py`` imports it module-level instead of lazily reaching back into
``arrange`` (one leg of the historical import cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..geometry.boxes import Box, world_box
from ..readers import kicad as _krd
from ..readers import kicad_lib
from ..sexpr import SNode
from ..sexpr.query import child_mil


@dataclass
class SymInfo:
    """One placed symbol: its padded full box and anchoring state."""

    ref: str
    at: tuple[float, float]           # placement anchor (mil)
    box: Box                          # body + pin field, world frame
    pin_tips: set[tuple[float, float]]
    anchored: bool = False


def collect(root: SNode) -> tuple[list[SymInfo], set[tuple[float, float]]]:
    """All placed symbols with world boxes + every wire/label anchor point."""
    libsym = root.find("lib_symbols")
    library = (kicad_lib.library_from_lib_symbols(libsym)
               if libsym is not None else None)
    syms: list[SymInfo] = []
    for sym in _krd._placed_symbols(root):
        lib_id = _krd._av(sym.find("lib_id"), 1) or ""
        at = sym.find("at")
        px, py = child_mil(at, 1), child_mil(at, 2)
        rot = int(round(_krd._fnum(at, 3))) % 360
        mnode = sym.find("mirror")
        mirror = (_krd._av(mnode, 1) if mnode is not None else None) or "none"
        unit = int(_krd._fnum(sym.find("unit"), 1, 1.0))
        ref = _krd._props(sym).get("Reference") or lib_id
        try:
            symdef = kicad_lib.resolve(lib_id, [library] if library else [])
        except Exception:
            continue
        pins = kicad_lib.unit_pins(symdef, unit)
        pin_world = [_krd._pin_world(lp.x_mil, lp.y_mil, px, py, rot, mirror)
                     for lp in pins]
        ext = kicad_lib.body_extent_mil(symdef, unit)
        if ext is not None:
            body = world_box(ext, px, py, rot, mirror, ref)
            xs = [body.x0, body.x1] + [q[0] for q in pin_world]
            ys = [body.y0, body.y1] + [q[1] for q in pin_world]
        elif pin_world:
            xs = [q[0] for q in pin_world]
            ys = [q[1] for q in pin_world]
        else:
            continue
        syms.append(SymInfo(
            ref=ref, at=(px, py),
            box=Box(min(xs), min(ys), max(xs), max(ys), ref, (px, py)),
            pin_tips={(q[0], q[1]) for q in pin_world}))

    anchors: set[tuple[float, float]] = set()
    for wire in root.find_all("wire"):
        pts = wire.find("pts")
        if pts is None:
            continue
        for xy in pts.find_all("xy"):
            anchors.add((child_mil(xy, 1), child_mil(xy, 2)))
    for tag in ("label", "global_label", "hierarchical_label"):
        for lb in root.find_all(tag):
            at = lb.find("at")
            anchors.add((child_mil(at, 1), child_mil(at, 2)))
    return syms, anchors
