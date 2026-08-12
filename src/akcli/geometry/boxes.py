"""Bounding-box primitives (rank 40) — formerly private to ``checks/layout``.

The overlap lint, ``arrange`` and ``groupframe`` all reason about the same
world-frame boxes; keeping the primitives inside the checks layer forced
``arrange`` (rank 70) to import ``checks.layout`` (rank 72) — the layering
inversion this module deletes.  Everything here is pure floats + the
rank-40 transform; no reader/writer/check imports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .transform import point_world_mil, world_box_from_extent

# Text metrics (mil). KiCad default field/label text is 1.27 mm = 50 mil tall;
# the stroke font advances roughly 0.9 * height per character. A global label
# adds its bubble outline + input/output arrow (~1.6 character widths).
TEXT_H = 50.0
CHAR_ADV = 0.9 * TEXT_H
PAD_GLOBAL = 1.6 * CHAR_ADV
PAD_LOCAL = 0.4 * CHAR_ADV
HALF_H_GLOBAL = 0.75 * TEXT_H
HALF_H_LOCAL = 0.6 * TEXT_H

# Two boxes must interpenetrate by more than this to count (touching is fine).
TOL = 1.0


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    name: str                  # designator or label text
    at: tuple[float, float]    # anchor (mil), for the finding message


def overlaps(a: Box, b: Box) -> bool:
    return (
        a.x0 < b.x1 - TOL and b.x0 < a.x1 - TOL
        and a.y0 < b.y1 - TOL and b.y0 < a.y1 - TOL
    )


def seg_in_box(
    a: tuple[float, float], b: tuple[float, float], box: Box,
    shrink: float = TOL,
) -> float:
    """Length (mil) of segment ``a->b`` strictly INSIDE ``box`` shrunk by
    ``shrink`` (Liang–Barsky clip); 0 for a miss or a boundary graze."""
    x0, y0 = box.x0 + shrink, box.y0 + shrink
    x1, y1 = box.x1 - shrink, box.y1 - shrink
    if x0 >= x1 or y0 >= y1:
        return 0.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return 0.0  # parallel and outside this edge
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    if t0 >= t1:
        return 0.0
    return math.hypot(dx, dy) * (t1 - t0)


def on_seg_f(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float],
    tol: float = 0.5,
) -> bool:
    """Float point-on-segment (inclusive of endpoints) within ``tol`` mil."""
    if not (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol):
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1]) <= tol
    return abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / length <= tol


def world_box(
    ext: tuple[float, float, float, float],
    px: float, py: float, rot: int, mirror: str,
    name: str,
) -> Box:
    """Instance-transform a lib-frame extent to a world (mil, +Y down) box."""
    (x0, y0, x1, y1) = ext
    pts = [
        point_world_mil(x, y, px, py, rot, mirror)
        for (x, y) in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    ]
    wx = [p[0] for p in pts]
    wy = [p[1] for p in pts]
    return Box(min(wx), min(wy), max(wx), max(wy), name, (px, py))


def placement_boxes_nm(
    ext_mil: tuple[float, float, float, float] | None,
    pin_pts: Sequence[tuple[int, int]],
    rotation: int, mirror: str, origin_nm: tuple[int, int],
) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int]]:
    """``(body_box, full_box)`` in world nm for one placed symbol unit.

    ``ext_mil`` is the drawn-body extent (``kicad_lib.body_extent_mil``) or
    ``None`` for a pin-only symbol; ``pin_pts`` are the unit's already
    world-transformed pin tips (``transform.pin_world``).  ``full_box`` is the
    body UNION the pin tips, degenerating to the placement origin when the
    symbol has neither — the spacing-planning box behind ``akcli bbox``.
    """
    body_nm = (None if ext_mil is None
               else world_box_from_extent(ext_mil, rotation, mirror, origin_nm))
    boxes: list[tuple[int, int, int, int]] = (
        ([body_nm] if body_nm is not None else [])
        + [(x, y, x, y) for (x, y) in pin_pts])
    if not boxes:                       # neither body nor pins: origin point
        boxes = [(origin_nm[0], origin_nm[1], origin_nm[0], origin_nm[1])]
    full_nm = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    return body_nm, full_nm


def label_text_extent_mil(text: str, *, is_global: bool = False) -> tuple[float, float]:
    """Estimated ``(length, half_height)`` of a label's rendered text, in mils.

    The stroke-font approximation the layout lint boxes labels with — the ONE
    label-metric authority, so the op-list generator (:mod:`akcli.intentgen`)
    sizes its cells with exactly the metrics the lint later judges the drawn
    sheet by.
    """
    length = len(text) * CHAR_ADV + (PAD_GLOBAL if is_global else PAD_LOCAL)
    half_h = HALF_H_GLOBAL if is_global else HALF_H_LOCAL
    return length, half_h


def label_box(text: str, x: float, y: float, angle: int, is_global: bool) -> Box:
    """Estimated world box of a label's text (+bubble for global labels)."""
    length, half_h = label_text_extent_mil(text, is_global=is_global)
    a = int(angle) % 360
    if a == 0:      # text extends +X
        return Box(x, y - half_h, x + length, y + half_h, text, (x, y))
    if a == 180:    # -X
        return Box(x - length, y - half_h, x, y + half_h, text, (x, y))
    if a == 90:     # up-screen (-Y)
        return Box(x - half_h, y - length, x + half_h, y, text, (x, y))
    return Box(x - half_h, y, x + half_h, y + length, text, (x, y))
