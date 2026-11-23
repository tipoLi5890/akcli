"""``akcli.geometry.outline`` — chaining loose segments into loops and measuring
them exactly.

Pure geometry: every expectation here is an analytic value (a rounded
rectangle is ``w·h − (4 − π)·r²``, a disc is ``π·r²``), so these tests need
no KiCad. The reader-level tests (``test_kicad_pcb_fab_facts.py``) are where
the same numbers are held against what real KiCad wrote and measured.
"""

from __future__ import annotations

import math

import pytest

from akcli.geometry import outline as o

# Analytic areas must come back to within floating-point noise.
TIGHT = 1e-9


def _flat(bbox) -> tuple[float, float, float, float]:
    (x0, y0), (x1, y1) = bbox
    return (x0, y0, x1, y1)


def _rounded_rect(x0: float, y0: float, w: float, h: float, r: float
                  ) -> list[o.Segment]:
    """Four edges + four exact quarter-circle corners, drawn out of order
    and with mixed directions — chaining must not depend on either."""
    x1, y1 = x0 + w, y0 + h
    k = r * (1 - math.sqrt(0.5))                  # corner arc's midpoint inset
    return [
        o.line((x0 + r, y0), (x1 - r, y0)),
        o.arc((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1)),          # SE
        o.line((x1 - r, y1), (x0 + r, y1)),
        o.arc((x0 + r, y0), (x0 + k, y0 + k), (x0, y0 + r)),          # NW, reversed
        o.line((x1, y0 + r), (x1, y1 - r)),
        o.arc((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r)),          # NE
        o.line((x0, y0 + r), (x0, y1 - r)),                           # reversed
        o.arc((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r)),          # SW
    ]


def test_rounded_rectangle_area_is_analytic():
    w, h, r = 40.0, 30.0, 2.0
    out = o.assemble(_rounded_rect(110.0, 85.0, w, h, r))
    assert out.closed and out.problems == ()
    assert out.area == pytest.approx(w * h - (4 - math.pi) * r * r, abs=TIGHT)
    assert len(out.loops) == 1 and out.loops[0].segments == 8
    assert _flat(out.bbox) == pytest.approx((110.0, 85.0, 150.0, 115.0))


def test_circle_area_and_bbox_are_analytic():
    out = o.assemble([], [o.circle((50.0, 50.0), 15.0)])
    assert out.closed
    assert out.area == pytest.approx(math.pi * 225.0, abs=TIGHT)
    # the bounding box comes from the arc's extremes, not from its endpoints
    assert out.bbox == ((35.0, 35.0), (65.0, 65.0))


def test_cutouts_are_subtracted_and_islands_added_back():
    board = _rounded_rect(0.0, 0.0, 40.0, 30.0, 2.0)
    out = o.assemble(board, [
        o.circle((20.0, 15.0), 5.0),                    # cutout
        o.rectangle((30.0, 5.0), (34.0, 9.0)),          # cutout
        o.polygon([(4.0, 4.0), (8.0, 4.0), (6.0, 8.0)]),  # cutout (triangle)
        o.circle((20.0, 15.0), 1.0),                    # island inside a cutout
    ])
    assert out.closed
    body = 40 * 30 - (4 - math.pi) * 4
    assert out.area == pytest.approx(
        body - math.pi * 25 - 16 - 8 + math.pi * 1, abs=TIGHT)
    assert [lp.depth for lp in out.loops] == [0, 1, 1, 1, 2]
    assert [lp.is_cutout for lp in out.loops] == [False, True, True, True, False]
    assert out.loops[0].area == pytest.approx(body, abs=TIGHT)


def test_rounded_rectangle_shape_matches_the_chained_one():
    """KiCad 10's ``(gr_rect … (radius r))`` as one closed shape."""
    w, h, r = 40.0, 30.0, 2.0
    shape = o.rounded_rectangle((150.0, 85.0), (110.0, 115.0), r)   # any corners
    out = o.assemble([], [shape])
    assert out.closed and len(shape) == 8
    assert out.area == pytest.approx(w * h - (4 - math.pi) * r * r, abs=TIGHT)
    assert _flat(out.bbox) == pytest.approx((110.0, 85.0, 150.0, 115.0))
    # no radius: the plain rectangle
    assert o.assemble([], [o.rounded_rectangle((0, 0), (4, 3), 0.0)]).area == \
        pytest.approx(12.0)


def test_rounded_rectangle_radius_is_clamped_like_kicad():
    # kicad-cli re-saves (radius 5) on a 4 x 10 rectangle as (radius 2): the
    # corners eat the short edges and what is left is a stadium
    stadium = o.rounded_rectangle((15.0, 15.0), (19.0, 25.0), 5.0)
    assert len(stadium) == 6                       # 4 arcs + the 2 long edges
    out = o.assemble([], [stadium])
    assert out.area == pytest.approx(4 * 10 - (4 - math.pi) * 4, abs=TIGHT)
    # a square with r = half its side is a circle
    disc = o.assemble([], [o.rounded_rectangle((0, 0), (6, 6), 99.0)])
    assert disc.area == pytest.approx(math.pi * 9, abs=TIGHT)


def test_two_separate_boards_add_up():
    out = o.assemble([], [o.rectangle((0, 0), (10, 10)),
                          o.rectangle((20, 0), (25, 10))])
    assert out.closed and out.area == pytest.approx(150.0)
    assert [lp.depth for lp in out.loops] == [0, 0]


def test_a_major_arc_keeps_its_sweep():
    # three quarters of the unit disc: a 270° arc closed by two radii
    out = o.assemble([o.arc((1.0, 0.0), (-1.0, 0.0), (0.0, -1.0)),
                      o.line((0.0, -1.0), (0.0, 0.0)),
                      o.line((0.0, 0.0), (1.0, 0.0))])
    assert out.closed
    assert out.area == pytest.approx(0.75 * math.pi, abs=TIGHT)
    assert _flat(out.bbox) == pytest.approx((-1.0, -1.0, 1.0, 1.0))


def test_legacy_centre_angle_arcs_match_the_three_point_form():
    """KiCad <= 5: (start = centre) (end = first point) (angle = sweep, deg).

    A positive angle is the plain rotation matrix in file coordinates —
    (48,10) about (48,12) by +90° lands on (50,12), as kicad-cli 10 re-saves
    it."""
    (seg,) = o.legacy_arc((48.0, 12.0), (48.0, 10.0), 90.0)
    assert seg.a == (48.0, 10.0)
    assert seg.b == pytest.approx((50.0, 12.0))
    assert seg.mid == pytest.approx((48 + math.sqrt(2), 12 - math.sqrt(2)))

    legacy = [
        o.line((12, 10), (48, 10)), *o.legacy_arc((48, 12), (48, 10), 90),
        o.line((50, 12), (50, 38)), *o.legacy_arc((48, 38), (50, 38), 90),
        o.line((48, 40), (12, 40)), *o.legacy_arc((12, 38), (10, 38), -90),
        o.line((10, 38), (10, 12)), *o.legacy_arc((12, 12), (12, 10), -90),
    ]
    out = o.assemble(legacy)
    assert out.closed
    assert out.area == pytest.approx(1200 - (4 - math.pi) * 4, abs=TIGHT)
    # a full turn is the whole circle
    assert o.assemble([], [o.legacy_arc((0, 0), (3, 0), 360.0)]).area == \
        pytest.approx(math.pi * 9, abs=TIGHT)


def test_polygon_path_with_an_arc_closes_implicitly():
    # KiCad gr_poly: (pts (xy) (xy) (arc ...) (xy) (xy)) — no closing point
    run = o.path([o.line((10, 10), (70, 10)),
                  o.arc((70, 10), (75, 15), (70, 20)),
                  o.line((70, 20), (70, 60)), o.line((70, 60), (10, 60))])
    out = o.assemble([], [run])
    assert out.closed
    assert out.area == pytest.approx(60 * 50 + math.pi * 25 / 2, abs=TIGHT)
    assert _flat(out.bbox) == pytest.approx((10.0, 10.0, 75.0, 60.0))


def test_chaining_tolerance_matches_kicad():
    """kicad-cli 10.0.4 joins a 0.009 mm gap and refuses a 0.011 mm one."""
    def square(gap: float) -> list[o.Segment]:
        return [o.line((0, 0), (30, 0)), o.line((30, gap), (30, 20)),
                o.line((30, 20), (0, 20)), o.line((0, 20), (0, 0))]

    joined = o.assemble(square(0.009))
    assert joined.closed and joined.area == pytest.approx(600.0, abs=0.2)
    refused = o.assemble(square(0.011))
    assert not refused.closed and refused.area is None
    assert refused.open_ends == 2


def test_an_open_outline_reports_why_and_still_has_a_bbox():
    segs = _rounded_rect(0.0, 0.0, 40.0, 30.0, 2.0)[:-1]      # one corner gone
    out = o.assemble(segs)
    assert not out.closed
    assert out.area is None                    # never a guessed area
    assert out.open_ends == 2 and out.loops == ()
    assert "2 segment end(s) meet nothing" in out.problems[0]
    assert _flat(out.bbox) == pytest.approx((0.0, 0.0, 40.0, 30.0))


def test_a_stray_segment_spoils_an_otherwise_closed_outline():
    # KiCad agrees: a dangling Edge.Cuts line makes its has_outline false.
    out = o.assemble([o.line((50, 50), (55, 50))],
                     [o.rectangle((0, 0), (10, 10))])
    assert not out.closed and out.area is None
    assert len(out.loops) == 1                 # the rectangle did close
    assert out.bbox == ((0, 0), (55, 50))


def test_three_segments_at_one_point_is_a_branch_not_a_loop():
    square = o.rectangle((0, 0), (10, 10))
    out = o.assemble([*square, o.line((0, 0), (-5, -5))])
    assert not out.closed
    assert out.branches == 1 and out.open_ends == 1
    assert any("more than two segments" in p for p in out.problems)


def test_caller_side_problems_keep_the_outline_from_closing():
    out = o.assemble([], [o.rectangle((0, 0), (10, 10))],
                     extra_points=[(20.0, 5.0)],
                     problems=["1 Bezier curve(s) on Edge.Cuts are not modelled"])
    assert not out.closed and out.area is None
    assert out.problems == ("1 Bezier curve(s) on Edge.Cuts are not modelled",)
    assert out.bbox == ((0, 0), (20.0, 10))    # extra points widen the box


def test_degenerate_input():
    empty = o.assemble([])
    assert not empty.closed and empty.bbox is None and empty.loops == ()
    # a zero-length segment joins nothing; it is not an open end either
    dot = o.assemble([o.line((1, 1), (1, 1))])
    assert not dot.closed and dot.open_ends == 0 and dot.bbox == ((1, 1), (1, 1))
    # three collinear "arc" points are a straight line
    flat = o.assemble([o.arc((0, 0), (5, 0), (10, 0)), o.line((10, 0), (10, 5)),
                       o.line((10, 5), (0, 0))])
    assert flat.closed and flat.area == pytest.approx(25.0)
    # a shape with no area is said so, not counted as a board
    line_shape = o.assemble([], [o.polygon([(0, 0), (10, 0)])])
    assert not line_shape.closed
    assert "encloses no area" in line_shape.problems[0]


def test_loop_order_is_deterministic():
    shapes = [o.rectangle((0, 0), (10, 10)), o.rectangle((20, 0), (40, 10)),
              o.circle((30, 5), 2)]
    a = o.assemble([], shapes)
    b = o.assemble([], list(reversed(shapes)))
    assert a.loops == b.loops
    assert [round(lp.area, 3) for lp in a.loops] == [200.0, 100.0, 12.566]
