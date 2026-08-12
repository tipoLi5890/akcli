"""Integer segment geometry: keyed points, collinearity, and segment clusters.

The primitives ``arrange.propose`` needs to reason about wire copper without
re-deriving them: a coordinate KEY that makes "same point" an exact integer
comparison (1/10 mil, KiCad's practical resolution), an endpoint-on-segment
test with the tolerance that key implies, and the union-find that turns
segments touching each other into clusters.

Kept in the spatial layer (rank 40) rather than inside the engine that first
needed it: "are these two wires the same piece of copper" is geometry, and the
next consumer (a checks-side cluster report) must not grow a second answer.
"""

from __future__ import annotations

__all__ = ["UnionFind", "Point", "PointKey", "on_segment", "point_key"]

Point = tuple[float, float]
PointKey = tuple[int, int]


def point_key(x: float, y: float) -> PointKey:
    """Snap a mil coordinate pair to a hashable 1/10-mil integer key."""
    return (round(x * 10), round(y * 10))


def on_segment(pt: Point, a: Point, b: Point) -> bool:
    """Is ``pt`` collinear with ``a``-``b`` AND between them (a tee-touch)?

    The collinearity tolerance is 0.5 mil·mil of cross product — the same
    "practically the same point" scale as :func:`point_key`.
    """
    (px, py), (ax, ay), (bx, by) = pt, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 0.5:                     # not collinear (0.05 mil·mil)
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    return 0 <= dot <= (bx - ax) ** 2 + (by - ay) ** 2


class UnionFind:
    """Disjoint-set over ``range(n)`` with path halving (iterative, no recursion)."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        parent = self.parent
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)

    def clusters(self) -> dict[int, list[int]]:
        """Members per root, in ascending member order (deterministic output)."""
        out: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            out.setdefault(self.find(i), []).append(i)
        return out
