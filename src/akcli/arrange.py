"""``akcli arrange`` — resolve symbol overlaps by nudging FREE components.

Closes the layout loop: ``draw`` places, ``check --layout`` finds overlaps,
``arrange`` fixes the ones that are safe to fix. A component is only moved
when it is **free** — none of its pin tips carries a wire endpoint or a label
anchor. Moving an anchored component would silently strand its labels/wires
(labels do not travel with ``move_component``), which is exactly the class of
bug ``check --nets`` exists to catch; those are reported as skipped instead.

The planner is a greedy first-fit: components keep their positions in
reading order (top-left first); each overlapping free component slides right
in grid steps (then down a row) until its padded bounding box fits. Moves are
emitted as ``move_component`` ops and applied through the standard draw
pipeline — atomic write, ``.bak``, connectivity re-verify — so ``arrange
--apply`` inherits every safety rail and ``akcli undo`` reverts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .checks.layout import _Box, _overlaps, _mil, _world_box
from .readers import kicad as _krd
from .readers import kicad_lib, sexpr

GRID_MIL = 100.0          # nudge step (2x the 50-mil pin grid)
MARGIN_MIL = 50.0         # required clearance between full boxes
_MAX_TRIES = 400          # bounded search per component


@dataclass
class SymInfo:
    """One placed symbol: its padded full box and anchoring state."""

    ref: str
    at: tuple[float, float]           # placement anchor (mil)
    box: _Box                         # body + pin field, world frame
    pin_tips: set[tuple[float, float]]
    anchored: bool = False


@dataclass
class Move:
    ref: str
    frm: tuple[float, float]
    to: tuple[float, float]

    def to_op(self) -> dict:
        return {"op": "move_component", "designator": self.ref,
                "x_mil": self.to[0], "y_mil": self.to[1]}


def _collect(root: sexpr.SNode) -> tuple[list[SymInfo], set[tuple[float, float]]]:
    """All placed symbols with world boxes + every wire/label anchor point."""
    libsym = root.find("lib_symbols")
    library = (kicad_lib.library_from_lib_symbols(libsym)
               if libsym is not None else None)
    syms: list[SymInfo] = []
    for sym in _krd._placed_symbols(root):
        lib_id = _krd._av(sym.find("lib_id"), 1) or ""
        at = sym.find("at")
        px, py = _mil(at, 1), _mil(at, 2)
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
            body = _world_box(ext, px, py, rot, mirror, ref)
            xs = [body.x0, body.x1] + [q[0] for q in pin_world]
            ys = [body.y0, body.y1] + [q[1] for q in pin_world]
        elif pin_world:
            xs = [q[0] for q in pin_world]
            ys = [q[1] for q in pin_world]
        else:
            continue
        syms.append(SymInfo(
            ref=ref, at=(px, py),
            box=_Box(min(xs), min(ys), max(xs), max(ys), ref, (px, py)),
            pin_tips={(q[0], q[1]) for q in pin_world}))

    anchors: set[tuple[float, float]] = set()
    for wire in root.find_all("wire"):
        pts = wire.find("pts")
        if pts is None:
            continue
        for xy in pts.find_all("xy"):
            anchors.add((_mil(xy, 1), _mil(xy, 2)))
    for tag in ("label", "global_label", "hierarchical_label"):
        for lb in root.find_all(tag):
            at = lb.find("at")
            anchors.add((_mil(at, 1), _mil(at, 2)))
    return syms, anchors


def _pad(b: _Box, m: float) -> _Box:
    return _Box(b.x0 - m, b.y0 - m, b.x1 + m, b.y1 + m, b.name, b.at)


def _shift(b: _Box, dx: float, dy: float) -> _Box:
    return _Box(b.x0 + dx, b.y0 + dy, b.x1 + dx, b.y1 + dy, b.name, b.at)


def plan(path: str | Path, *, grid: float = GRID_MIL,
         margin: float = MARGIN_MIL) -> dict:
    """Compute the nudges that would make the sheet overlap-free.

    Returns ``{"moves": [Move], "anchored_overlaps": [ref], "clean": bool,
    "symbols": N}``. Never touches the file.
    """
    root = sexpr.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    syms, anchors = _collect(root)
    for s in syms:
        s.anchored = bool(s.pin_tips & anchors)

    # reading order: anchored symbols are immovable obstacles, free symbols
    # keep their place when possible and slide when they collide
    ordered = sorted(syms, key=lambda s: (s.box.y0, s.box.x0))
    placed: list[_Box] = [s.box for s in ordered if s.anchored]
    moves: list[Move] = []
    anchored_overlaps: list[str] = []

    fixed = [s for s in ordered if s.anchored]
    for a in fixed:
        for b in fixed:
            if a.ref < b.ref and _overlaps(_pad(a.box, margin / 2),
                                           _pad(b.box, margin / 2)):
                anchored_overlaps.extend([a.ref, b.ref])

    for s in ordered:
        if s.anchored:
            continue
        box = _pad(s.box, margin / 2)
        dx = dy = 0.0
        tries = 0
        while any(_overlaps(_shift(box, dx, dy), _pad(p, margin / 2))
                  for p in placed) and tries < _MAX_TRIES:
            dx += grid
            tries += 1
            if tries % 40 == 0:          # give up on the row, start the next
                dx = 0.0
                dy += grid * 4
        if tries >= _MAX_TRIES:
            anchored_overlaps.append(s.ref)
            placed.append(s.box)
            continue
        placed.append(_shift(s.box, dx, dy))
        if dx or dy:
            moves.append(Move(ref=s.ref, frm=s.at,
                              to=(s.at[0] + dx, s.at[1] + dy)))

    return {
        "moves": moves,
        "anchored_overlaps": sorted(set(anchored_overlaps)),
        "clean": not moves and not anchored_overlaps,
        "symbols": len(syms),
    }
