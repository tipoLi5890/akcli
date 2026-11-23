"""Board-outline assembly (rank 40): chain loose line/arc primitives into closed
loops and measure them exactly.

A KiCad board outline is whatever sits on ``Edge.Cuts``: loose ``gr_line`` /
``gr_arc`` segments that only *happen* to meet end to end, plus self-closed
shapes (``gr_rect``, ``gr_circle``, ``gr_poly``) — on the board and inside
footprints. Until this module existed the reader took the bounding box of the
straight segments and every consumer treated that as "the board": a rounded
corner cost nothing, a routed cutout was free, and a board drawn as one circle
had no outline at all.

Everything here is pure floats — no reader, no S-expressions, no units: the
caller hands over :class:`Segment` values already in its own frame (the KiCad
reader passes millimetres, +Y down) and gets back an :class:`Outline`.

**Area is exact, not tessellated.** A loop's area is the shoelace sum over its
chords plus, per arc, the circular-segment term ``r²/2 · (θ − sin θ)`` (signed
by the sweep). Tessellation is used for exactly one thing — deciding which
loop lies inside which — where a 5 µm chord error cannot change the answer.

**Nesting decides the sign.** A loop inside one other loop is a cutout and is
subtracted; a loop inside a cutout is an island and counts again (even-odd).

**An outline that does not close is reported, never guessed.** Loose segments
are joined only when two endpoints meet within :data:`CHAIN_TOLERANCE_MM`; an
endpoint left alone (or three segments at one point) makes
:attr:`Outline.closed` false and :attr:`Outline.area` ``None``. The bounding
box is still returned so a caller can fall back to it *and say so*.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .segments import UnionFind

__all__ = ["CHAIN_TOLERANCE_MM", "BBox", "Loop", "Outline", "Point", "Segment",
           "arc", "assemble", "circle", "legacy_arc", "line", "path",
           "polygon", "rectangle", "rounded_rectangle"]

Point = tuple[float, float]
BBox = tuple[Point, Point]

#: Endpoint-matching tolerance. Measured, not chosen: kicad-cli 10.0.4's own
#: outline builder (``pcb export stats`` -> ``has_outline``) joins a 0.009 mm
#: gap and refuses a 0.011 mm one.
CHAIN_TOLERANCE_MM = 0.01

# Chord error allowed when a loop is flattened for the containment test only
# (KiCad's own default arc-approximation error).
_FLATTEN_ERROR = 0.005
_MAX_ARC_STEPS = 3600
_TAU = 2.0 * math.pi
# Below this cross product three points are treated as collinear.
_COLLINEAR = 1e-12


@dataclass(frozen=True)
class Segment:
    """One open outline primitive from ``a`` to ``b``.

    ``mid`` is a point ON the arc between the two ends (KiCad's
    ``start``/``mid``/``end`` form); ``None`` means a straight line. An arc
    whose ends coincide is the full circle with diameter ``a``-``mid``.
    """

    a: Point
    b: Point
    mid: Point | None = None


@dataclass(frozen=True)
class Loop:
    """One closed loop of the outline.

    ``depth`` is how many other loops contain it: 0 is a board body, 1 a
    cutout in it, 2 an island inside that cutout, and so on.
    """

    area: float
    bbox: BBox
    depth: int
    segments: int

    @property
    def is_cutout(self) -> bool:
        return self.depth % 2 == 1


@dataclass(frozen=True)
class Outline:
    """What :func:`assemble` could establish about a set of primitives.

    ``closed`` is true only when there is at least one loop and EVERY
    primitive belongs to a closed loop; ``area`` (board bodies minus cutouts)
    is ``None`` otherwise. ``bbox`` covers every primitive, closed or not.
    ``problems`` says, in words, why the outline did not close.
    """

    closed: bool
    area: float | None
    bbox: BBox | None
    loops: tuple[Loop, ...]
    open_ends: int
    branches: int
    problems: tuple[str, ...]


# --------------------------------------------------------------------------- #
# constructors
# --------------------------------------------------------------------------- #
def line(a: Point, b: Point) -> Segment:
    return Segment(a, b)


def arc(start: Point, mid: Point, end: Point) -> Segment:
    """A three-point arc (KiCad >= 6: ``(start) (mid) (end)``)."""
    return Segment(start, end, mid)


def _rotate(p: Point, center: Point, angle: float) -> Point:
    """``p`` turned by ``angle`` (rad) about ``center`` — the plain rotation
    matrix in the caller's own frame."""
    dx, dy = p[0] - center[0], p[1] - center[1]
    c, s = math.cos(angle), math.sin(angle)
    return (center[0] + dx * c - dy * s, center[1] + dx * s + dy * c)


def legacy_arc(center: Point, start: Point, sweep_deg: float) -> list[Segment]:
    """A KiCad <= 5 arc: ``(start <centre>) (end <first point>) (angle <deg>)``.

    The sweep is applied with the plain rotation matrix in file coordinates,
    which on a +Y-down board is clockwise on screen for a positive angle —
    the reading kicad-cli 10.0.4 gives the form when it upgrades such a file
    (``tests/fixtures/kicad/fab/legacy_v5.kicad_pcb`` and its re-save). A
    sweep of a full turn or more is the whole circle.
    """
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if abs(sweep_deg) >= 360.0:
        return circle(center, radius)
    sweep = math.radians(sweep_deg)
    return [Segment(start, _rotate(start, center, sweep),
                    _rotate(start, center, sweep / 2.0))]


def circle(center: Point, radius: float) -> list[Segment]:
    """A full circle as a closed path of two half-circle arcs."""
    cx, cy = center
    east, west = (cx + radius, cy), (cx - radius, cy)
    return [Segment(east, west, (cx, cy + radius)),
            Segment(west, east, (cx, cy - radius))]


def polygon(vertices: Sequence[Point]) -> list[Segment]:
    """A closed path of straight edges through ``vertices`` (implicit close)."""
    return path([Segment(vertices[i], vertices[i + 1])
                 for i in range(len(vertices) - 1)])


def rectangle(a: Point, b: Point) -> list[Segment]:
    """The axis-aligned rectangle with opposite corners ``a`` and ``b``."""
    return polygon([a, (b[0], a[1]), b, (a[0], b[1])])


def rounded_rectangle(a: Point, b: Point, radius: float) -> list[Segment]:
    """The axis-aligned rectangle ``a``-``b`` with circular corners of ``radius``
    (KiCad 10: ``(gr_rect (start) (end) (radius r))``).

    The radius is clamped to half the shorter side — what kicad-cli 10.0.4
    does to an oversized one when it loads the file — and a radius of zero is
    the plain rectangle. Edges the corners use up entirely are left out.
    """
    (x0, x1), (y0, y1) = sorted((a[0], b[0])), sorted((a[1], b[1]))
    r = min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    if r <= 0.0:
        return rectangle((x0, y0), (x1, y1))
    k = r * (1.0 - math.sqrt(0.5))        # inset of a corner arc's midpoint
    run = [
        Segment((x0 + r, y0), (x1 - r, y0)),
        Segment((x1 - r, y0), (x1, y0 + r), (x1 - k, y0 + k)),
        Segment((x1, y0 + r), (x1, y1 - r)),
        Segment((x1, y1 - r), (x1 - r, y1), (x1 - k, y1 - k)),
        Segment((x1 - r, y1), (x0 + r, y1)),
        Segment((x0 + r, y1), (x0, y1 - r), (x0 + k, y1 - k)),
        Segment((x0, y1 - r), (x0, y0 + r)),
        Segment((x0, y0 + r), (x0 + r, y0), (x0 + k, y0 + k)),
    ]
    return [seg for seg in run if seg.mid is not None or seg.a != seg.b]


def path(segments: Sequence[Segment]) -> list[Segment]:
    """Make an ordered run of segments a closed path.

    Consecutive segments that do not share an endpoint are joined by a
    straight edge, and the last is joined back to the first — the rule a
    KiCad ``gr_poly`` follows for the points and arcs in its ``pts``.
    """
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].b != seg.a:
            out.append(Segment(out[-1].b, seg.a))
        out.append(seg)
    if out and out[-1].b != out[0].a:
        out.append(Segment(out[-1].b, out[0].a))
    return out


# --------------------------------------------------------------------------- #
# arc geometry
# --------------------------------------------------------------------------- #
def _arc_params(seg: Segment) -> tuple[float, float, float, float] | None:
    """``(cx, cy, r, sweep)`` of an arc segment; ``None`` when it is straight.

    ``sweep`` is signed in the caller's frame (positive = the rotation that
    takes +X towards +Y) and is the angle travelled from ``a`` to ``b``
    through ``mid``. Three collinear points are a straight line.
    """
    if seg.mid is None:
        return None
    (ax, ay), (bx, by), (mx, my) = seg.a, seg.b, seg.mid
    if math.hypot(bx - ax, by - ay) <= 1e-9:
        # Coincident ends: the full circle on the diameter a-mid.
        radius = math.hypot(mx - ax, my - ay) / 2.0
        if radius <= 1e-9:
            return None
        return ((ax + mx) / 2.0, (ay + my) / 2.0, radius, _TAU)
    d = 2.0 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    scale = max(abs(ax - bx), abs(ay - by), abs(ax - mx), abs(ay - my), 1e-9)
    if abs(d) <= _COLLINEAR * scale * scale:
        return None
    a2, m2, b2 = ax * ax + ay * ay, mx * mx + my * my, bx * bx + by * by
    cx = (a2 * (my - by) + m2 * (by - ay) + b2 * (ay - my)) / d
    cy = (a2 * (bx - mx) + m2 * (ax - bx) + b2 * (mx - ax)) / d
    radius = math.hypot(ax - cx, ay - cy)
    ta = math.atan2(ay - cy, ax - cx)
    to_b = (math.atan2(by - cy, bx - cx) - ta) % _TAU
    to_m = (math.atan2(my - cy, mx - cx) - ta) % _TAU
    # ``mid`` lies on the positive way round from a to b, or on the other one.
    sweep = to_b if to_m < to_b else to_b - _TAU
    return (cx, cy, radius, sweep)


def _segment_points(seg: Segment) -> list[Point]:
    """``a`` .. ``b`` as a polyline (arcs flattened; lines are their two ends)."""
    params = _arc_params(seg)
    if params is None:
        return [seg.a, seg.b]
    cx, cy, radius, sweep = params
    err = min(_FLATTEN_ERROR, radius / 2.0)
    step = 2.0 * math.acos(1.0 - err / radius)
    n = max(2, min(_MAX_ARC_STEPS, math.ceil(abs(sweep) / step)))
    t0 = math.atan2(seg.a[1] - cy, seg.a[0] - cx)
    pts = [seg.a]
    for i in range(1, n):
        t = t0 + sweep * i / n
        pts.append((cx + radius * math.cos(t), cy + radius * math.sin(t)))
    pts.append(seg.b)
    return pts


def _segment_extent(seg: Segment) -> list[Point]:
    """Points whose bounding box is the segment's: its ends plus, for an arc,
    every axis extreme the sweep passes through."""
    pts = [seg.a, seg.b]
    params = _arc_params(seg)
    if params is None:
        if seg.mid is not None:
            pts.append(seg.mid)
        return pts
    cx, cy, radius, sweep = params
    t0 = math.atan2(seg.a[1] - cy, seg.a[0] - cx)
    for k in range(4):
        axis = k * math.pi / 2.0
        travelled = ((axis - t0) % _TAU if sweep > 0
                     else (t0 - axis) % _TAU)
        if travelled <= abs(sweep):
            pts.append((cx + radius * math.cos(axis),
                        cy + radius * math.sin(axis)))
    return pts


def _bbox(points: Iterable[Point]) -> BBox | None:
    xs: list[float] = []
    ys: list[float] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return ((min(xs), min(ys)), (max(xs), max(ys)))


def _reversed(seg: Segment) -> Segment:
    return Segment(seg.b, seg.a, seg.mid)


def _signed_area(ordered: Sequence[Segment]) -> float:
    """Signed area of a closed, head-to-tail run of segments.

    Endpoints that met only within the chaining tolerance are bridged by the
    implicit straight edge between them, so the shoelace sum is over a truly
    closed polygon. Coordinates are taken relative to the first point: board
    coordinates sit a few hundred millimetres from the origin and the sum
    would otherwise cancel large products.
    """
    ox, oy = ordered[0].a
    total = 0.0
    prev = ordered[-1].b
    for seg in ordered:
        for p, q in ((prev, seg.a), (seg.a, seg.b)):
            total += ((p[0] - ox) * (q[1] - oy) - (q[0] - ox) * (p[1] - oy)) / 2.0
        params = _arc_params(seg)
        if params is not None:
            _cx, _cy, radius, sweep = params
            total += radius * radius * (sweep - math.sin(sweep)) / 2.0
        prev = seg.b
    return total


# --------------------------------------------------------------------------- #
# chaining
# --------------------------------------------------------------------------- #
def _degenerate(seg: Segment, tol: float) -> bool:
    """A segment that fits inside the tolerance joins nothing to nothing.

    Judged on the whole extent, not the chord: a full-circle arc has
    coincident ends and is anything but degenerate.
    """
    box = _bbox(_segment_extent(seg))
    assert box is not None          # a segment always has two ends
    return max(box[1][0] - box[0][0], box[1][1] - box[0][1]) <= tol


def _cluster_endpoints(segs: Sequence[Segment], tol: float) -> list[int]:
    """Node id per endpoint (``2*i`` = ``segs[i].a``, ``2*i + 1`` = ``.b``).

    Endpoints within ``tol`` of each other share a node. A uniform grid of
    ``tol``-sized cells keeps the search local: two such points are always in
    the same or in adjacent cells.
    """
    points = [p for seg in segs for p in (seg.a, seg.b)]
    uf = UnionFind(len(points))
    cells: dict[tuple[int, int], list[int]] = {}
    for idx, (x, y) in enumerate(points):
        gx, gy = math.floor(x / tol), math.floor(y / tol)
        for nx in (gx - 1, gx, gx + 1):
            for ny in (gy - 1, gy, gy + 1):
                for other in cells.get((nx, ny), ()):
                    ox, oy = points[other]
                    if math.hypot(x - ox, y - oy) <= tol:
                        uf.union(idx, other)
        cells.setdefault((gx, gy), []).append(idx)
    return [uf.find(i) for i in range(len(points))]


def _walk(start: int, segs: Sequence[Segment], node_of: Sequence[int],
          at_node: dict[int, list[int]], used: set[int]) -> list[Segment]:
    """Follow a degree-2 cycle from segment ``start``, orienting each segment
    head to tail. Only called on components already known to be simple cycles."""
    ordered: list[Segment] = []
    seg_idx, node = start, node_of[2 * start + 1]
    ordered.append(segs[start])
    used.add(start)
    while node != node_of[2 * start]:
        seg_idx = next(i for i in at_node[node] if i not in used)
        used.add(seg_idx)
        if node_of[2 * seg_idx] == node:
            ordered.append(segs[seg_idx])
            node = node_of[2 * seg_idx + 1]
        else:
            ordered.append(_reversed(segs[seg_idx]))
            node = node_of[2 * seg_idx]
    return ordered


def _chain(loose: Sequence[Segment], tol: float,
           ) -> tuple[list[list[Segment]], int, int]:
    """Loose segments -> ``(closed runs, open ends, branch points)``.

    A component of the endpoint graph is a loop exactly when every node in it
    joins two segment ends. Components that fail are left out of the result
    and show up only in the two counts.
    """
    segs = [s for s in loose if not _degenerate(s, tol)]
    if not segs:
        return [], 0, 0
    node_of = _cluster_endpoints(segs, tol)
    ends_at: dict[int, int] = {}
    at_node: dict[int, list[int]] = {}
    for end, node in enumerate(node_of):
        ends_at[node] = ends_at.get(node, 0) + 1
        members = at_node.setdefault(node, [])
        if end // 2 not in members:
            members.append(end // 2)
    open_ends = sum(1 for n in ends_at.values() if n == 1)
    branches = sum(1 for n in ends_at.values() if n > 2)

    # Connected components over segments (two segments sharing a node).
    comp = UnionFind(len(segs))
    for members in at_node.values():
        for other in members[1:]:
            comp.union(members[0], other)
    runs: list[list[Segment]] = []
    used: set[int] = set()
    for members in comp.clusters().values():
        if any(ends_at[node_of[2 * i + e]] != 2 for i in members for e in (0, 1)):
            continue
        runs.append(_walk(members[0], segs, node_of, at_node, used))
    return runs, open_ends, branches


# --------------------------------------------------------------------------- #
# nesting
# --------------------------------------------------------------------------- #
def _inside(point: Point, poly: Sequence[Point]) -> bool:
    """Even-odd ray cast: is ``point`` inside the closed polygon ``poly``?"""
    x, y = point
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def _box_within(inner: BBox, outer: BBox, tol: float) -> bool:
    return (inner[0][0] >= outer[0][0] - tol and inner[0][1] >= outer[0][1] - tol
            and inner[1][0] <= outer[1][0] + tol
            and inner[1][1] <= outer[1][1] + tol)


def assemble(loose: Iterable[Segment],
             shapes: Iterable[Sequence[Segment]] = (),
             *,
             extra_points: Iterable[Point] = (),
             problems: Iterable[str] = (),
             tol: float = CHAIN_TOLERANCE_MM) -> Outline:
    """Chain ``loose`` segments, add the already-closed ``shapes``, measure.

    ``shapes`` are closed paths in order (see :func:`path`, :func:`circle`,
    :func:`rectangle`): they are never chained to anything else, exactly as
    KiCad treats a rectangle or polygon on ``Edge.Cuts``. ``extra_points``
    widen the bounding box only, and ``problems`` are caller-side reasons the
    outline cannot be trusted (a primitive this module has no model for) —
    any of them makes the result not closed.
    """
    loose_segs = list(loose)
    shape_runs = [list(s) for s in shapes if s]
    runs, open_ends, branches = _chain(loose_segs, tol)
    runs.extend(shape_runs)

    reasons = list(problems)
    if open_ends:
        reasons.append(f"{open_ends} segment end(s) meet nothing within "
                       f"{tol:g} mm")
    if branches:
        reasons.append(f"{branches} point(s) where more than two segments meet")

    extent = [p for seg in loose_segs for p in _segment_extent(seg)]
    extent.extend(p for run in shape_runs for seg in run
                  for p in _segment_extent(seg))
    extent.extend(extra_points)
    bbox = _bbox(extent)

    measured: list[tuple[float, BBox, list[Point], int]] = []
    for run in runs:
        box = _bbox(p for seg in run for p in _segment_extent(seg))
        area = abs(_signed_area(run))
        if box is None or area <= 0.0:
            reasons.append("a closed shape encloses no area")
            continue
        flat: list[Point] = []
        for seg in run:
            flat.extend(_segment_points(seg)[:-1])
        measured.append((area, box, flat, len(run)))

    loops: list[Loop] = []
    for i, (area, box, flat, count) in enumerate(measured):
        depth = sum(
            1 for j, (_a, other_box, other_flat, _n) in enumerate(measured)
            if j != i and _box_within(box, other_box, tol)
            and _inside(flat[0], other_flat))
        loops.append(Loop(area=area, bbox=box, depth=depth, segments=count))
    loops.sort(key=lambda lp: (lp.depth, -lp.area, lp.bbox))

    closed = bool(loops) and not reasons
    net = (sum(-lp.area if lp.is_cutout else lp.area for lp in loops)
           if closed else None)
    return Outline(closed=closed, area=net, bbox=bbox, loops=tuple(loops),
                   open_ends=open_ends, branches=branches,
                   problems=tuple(reasons))
