"""Performance guard + correctness parity for the shared :class:`SegmentIndex`.

The netbuild geometric rules and the connectivity gate used to scan *every*
segment for *every* query point — O(n·m), which measured at 8.4 s for a
~5000-segment sheet (and ``plan/draw`` runs netbuild twice for its net-diff, so
big sheets paid it doubly). :class:`~akcli.netbuild.SegmentIndex`
buckets orthogonal segments so each query is O(log n + hits).

Two things are pinned here:

* **Semantics don't move.** The index must reproduce the exact-integer linear
  scans bit-for-bit, so :func:`test_index_matches_bruteforce` cross-checks
  ``segments_through`` / ``interior_hits`` against a brute-force scan over a
  mix of horizontal, vertical, diagonal and zero-length segments — including
  endpoint, interior, off-segment and collinear-but-outside probe points.
* **It stays fast.** A ~5000-segment ladder must build nets and verify in well
  under a second (generous ceilings — the point is to catch a return of the
  quadratic, not to micro-benchmark). Timings are best-of-3: the min filters
  shared-CI scheduler noise, while the quadratic's 8× overshoot still trips
  every run. No fixtures on disk; everything is generated in-memory.
"""

from __future__ import annotations

import time

from akcli import model
from akcli.netbuild import (
    SegmentIndex,
    _on_seg,
    _on_seg_interior,
    build_nets,
)
from akcli.readers.sexpr import parse
from akcli.writers import connectivity

ROOT_UUID = "8a000000-0000-4000-8000-000000000000"


# --------------------------------------------------------------------------- #
# correctness: the index reproduces the linear scans exactly
# --------------------------------------------------------------------------- #
def test_index_matches_bruteforce():
    segs = [
        ((0, 0), (100, 0)),      # horizontal
        ((50, 0), (150, 0)),     # overlapping horizontal (same y bucket)
        ((0, 0), (0, 100)),      # vertical
        ((0, 50), (0, 200)),     # overlapping vertical (same x bucket)
        ((10, 10), (110, 110)),  # diagonal
        ((20, 20), (20, 20)),    # zero-length (interior is empty)
        ((-30, 40), (70, 40)),   # horizontal spanning negative x
    ]
    idx = SegmentIndex(segs)

    # probe a grid that hits endpoints, interiors, off-points and the diagonal
    probes = set()
    for x in range(-40, 160, 5):
        for y in range(-10, 210, 5):
            probes.add((x, y))
    # exact endpoints and a couple of collinear-but-outside points
    for a, b in segs:
        probes.add(a)
        probes.add(b)
    probes.update({(200, 0), (0, 300), (60, 60), (61, 60)})

    for p in probes:
        want_through = sorted((a, b) for a, b in segs if _on_seg(p, a, b))
        got_through = sorted(idx.segments_through(p))
        assert got_through == want_through, p

        want_int = sorted((a, b) for a, b in segs if _on_seg_interior(p, a, b))
        got_int = sorted(idx.interior_hits(p))
        assert got_int == want_int, p

        assert idx.has_interior_hit(p) == bool(want_int), p
        assert idx.interior_count(p) == len(want_int), p


# --------------------------------------------------------------------------- #
# perf: a ~5000-segment ladder builds nets and verifies well under a second
# --------------------------------------------------------------------------- #
_RUNGS = 1700          # -> 3*_RUNGS = 5100 segments
_STEP = 100
_WIDTH = 400


def _fastest(fn, runs=3):
    """Best-of-N wall clock: min over ``runs`` calls filters scheduler noise
    on shared CI runners without loosening the ceiling itself."""
    best = float("inf")
    result = None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - t0)
    return result, best


def _ladder_prims() -> model.NetPrimitives:
    """Two rails (each a chain of _RUNGS segments) crossed by _RUNGS rungs."""
    wires: list[model.WireSeg] = []
    for x in (0, _WIDTH):
        for i in range(_RUNGS):
            wires.append(model.WireSeg(a=(x, i * _STEP), b=(x, (i + 1) * _STEP)))
    for i in range(_RUNGS):
        wires.append(model.WireSeg(a=(0, i * _STEP), b=(_WIDTH, i * _STEP)))
    # a handful of pins so build_nets emits real nets from the mesh
    pins = [model.PinHandle(ref=(f"R{i}", "1"), at=(0, i * _STEP)) for i in range(10)]
    return model.NetPrimitives(wires=wires, pins=pins)


def test_build_nets_ladder_is_fast():
    prims = _ladder_prims()
    assert len(prims.wires) == 3 * _RUNGS
    nets, dt = _fastest(lambda: build_nets(prims))
    # the whole mesh is one connected net; the 10 pins collapse into it
    assert len(nets) == 1
    assert dt < 1.0, f"build_nets took {dt:.3f}s for {len(prims.wires)} segments"


def _ladder_kicad_sch() -> str:
    """A ladder as .kicad_sch text: two rail polylines + _RUNGS rung wires."""
    lines = [f'(kicad_sch (uuid "{ROOT_UUID}")', "(lib_symbols)"]
    for r, x in enumerate((0, _WIDTH)):
        pts = " ".join(f"(xy {x} {i * _STEP})" for i in range(_RUNGS + 1))
        lines.append(f'(wire (pts {pts}) (uuid "rail-{r}"))')
    for i in range(_RUNGS):
        y = i * _STEP
        lines.append(
            f'(wire (pts (xy 0 {y}) (xy {_WIDTH} {y})) (uuid "rung-{i}"))'
        )
    return "\n".join(lines) + ")"


def test_verify_ladder_is_fast():
    doc = parse(_ladder_kicad_sch())
    findings, dt = _fastest(lambda: connectivity.verify(doc))
    # every rung end lands on a rail vertex; only the two free rail crowns
    # (one step above the top rung) dangle — the mesh interior is fully joined.
    dangling = [f for f in findings if f.code == connectivity.DANGLING_ENDPOINT]
    assert len(dangling) == 2, [f.refs for f in dangling]
    assert dt < 1.0, f"verify took {dt:.3f}s"
