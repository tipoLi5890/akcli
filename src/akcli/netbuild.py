"""Format-agnostic net inference (SPEC §3.3) — the STAT fix.

A single shared net-builder consumed by both the Altium and KiCad readers, so
the same-name merge / junction / T-junction logic is written exactly once. The
A naive builder that keeps only the *first* label per geometric cluster
(``root_name.setdefault``) splits same-named ``GND`` ports and drops aliases
like ``STAT``↔``LED1_GPIO_RD`` entirely. This module rebuilds the layer from
scratch.

LOCKED pipeline (``build_nets``):

1. Exact-integer geometric union-find on wire segments.
2. Union each junction(29) point onto every segment it lies on.
3. T-junction: union every wire vertex lying on another wire's mid-span —
   **only when** ``t_midspan_connects`` (the Altium rule). eeschema does NOT
   join a wire end to another wire's interior without an explicit junction
   node (verified against ``kicad-cli sch export netlist``, KiCad 10.0.4:
   the arm pin stayed ``unconnected-*``), so the KiCad reader passes
   ``False``; eeschema-authored files always carry the junction anyway.
4. Union pins/labels lying on a segment (exact-integer cross-product ``on_seg``).
   Pins connect at segment ENDPOINTS or at a junction-marked point — a bare
   mid-span touch does not connect (eeschema's rule; Altium's editor inserts a
   junction record for every pin tap). Labels connect anywhere along the wire
   (kicad-cli-verified: a mid-span local label joins the net).
4c/4d. BUS layer (KiCad; kicad-cli-arbitrated, see the inline block comment):
   bus segments cluster like wires but never join wires directly; labels on a
   bus name it (vector labels expand to members); a (bus_entry) conducts
   between its two ends and joins its wire-side cluster to the bus member
   selected by that cluster's own label — an unlabeled rip stays unconnected.
5. GLOBAL same-name merge: group label/power-port names, union every pair of
   clusters sharing any name. This stitches the two STAT clusters AND collapses
   same-name ``GND`` ports. Net labels are sheet-local; power ports / ports /
   sheet-entries / global labels merge across sheets.
6. Naming priority power-port > net-label > auto (honoring ``power_priority``);
   keep all names as aliases, lower confidence + record merge_reasons on
   multi-name nets.
7. Stable membership-hash ids (``model.Net.stable_id``) — NEVER coordinate-derived.
8. Multi-sheet union by Port/SheetEntry/global name.

Coordinates arrive as floats (mils from Altium ``_Frac`` assembly, or mm→mil
from KiCad). They are quantized to integers (``_QUANT``) so every geometric test
is exact integer arithmetic — no floating-point coincidence fuzz.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict

from . import model

# Quantization factor: floats (mils) -> exact integers. 1e4 preserves the
# Altium ``_Frac`` sub-unit resolution (10 mil / 100000) and absorbs the tiny
# float error from mm↔mil conversions without merging genuinely distinct points.
_QUANT = 10_000

# Scope buckets. Local net labels are sheet-LOCAL (only merge within one sheet);
# everything else is global and merges across sheets by name.
_POWER_SCOPES = frozenset({"power"})
# "hier" carries synthetic parent-sheet-pin <-> child-hierarchical-label
# connectors: unique per (sheet instance, pin name), so the global merge joins
# exactly that pair — and NOTHING else. They never name a net (see below).
_GLOBAL_SCOPES = frozenset({"global", "power", "port", "sheet_entry", "hier"})
_LABEL_SCOPES = frozenset({"local", "global", "port", "sheet_entry"})

# Confidence assigned to a net that carries more than one distinct explicit name.
_MULTI_NAME_CONFIDENCE = 0.8


def _q(pt: tuple[float, float]) -> tuple[int, int]:
    """Quantize a float point to an exact integer key."""
    return (round(pt[0] * _QUANT), round(pt[1] * _QUANT))


class _DSU:
    """Disjoint-set union keyed by hashable nodes (quantized point tuples)."""

    def __init__(self) -> None:
        self._parent: dict = {}

    def add(self, x) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x):
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra
        return ra


def _on_seg(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Exact integer test: does point ``p`` lie on segment ``a``→``b`` (inclusive)."""
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if cross != 0:
        return False
    return (
        min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def _on_seg_interior(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Exact integer test: does ``p`` lie STRICTLY between ``a`` and ``b``?

    Endpoints and a zero-length segment (no interior) return ``False``. This is
    the diagonal fallback for :class:`SegmentIndex`; orthogonal segments never
    reach it.
    """
    if p == a or p == b:
        return False
    ax, ay = a
    bx, by = b
    px, py = p
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < 0:
        return False
    sqlen = (bx - ax) ** 2 + (by - ay) ** 2
    return 0 < dot < sqlen


class SegmentIndex:
    """O(log n + k) spatial index over exact-integer orthogonal segments.

    Manhattan schematics are almost entirely axis-aligned, so the naive
    "scan every segment for every query" is O(n·m). This buckets horizontal
    segments by their constant ``y`` and vertical segments by their constant
    ``x``; within a bucket the intervals are sorted on the varying axis so a
    :mod:`bisect` narrows the candidates to those spanning the query. Diagonal
    and zero-length segments (rare) fall back to a short linear list scanned
    with the exact integer on-segment test.

    Every coordinate is an integer, so the index reproduces the linear
    ``_on_seg`` / ``_on_seg_interior`` scans it replaces bit-for-bit — a hit is
    an exact geometric coincidence, never a tolerance match.

    ``segments_through`` / ``interior_hits`` yield the stored ``(a, b)`` pairs
    (original endpoint order preserved, so callers can union onto endpoint
    ``a``); ``has_interior_hit`` / ``interior_count`` are cheap bool/count
    wrappers.
    """

    __slots__ = ("_h", "_hx", "_v", "_vy", "_diag")

    def __init__(self, segments) -> None:
        h: dict = defaultdict(list)  # y -> [(xmin, xmax, a, b), ...]
        v: dict = defaultdict(list)  # x -> [(ymin, ymax, a, b), ...]
        diag: list = []
        for a, b in segments:
            ax, ay = a
            bx, by = b
            if ay == by and ax != bx:
                lo, hi = (ax, bx) if ax <= bx else (bx, ax)
                h[ay].append((lo, hi, a, b))
            elif ax == bx and ay != by:
                lo, hi = (ay, by) if ay <= by else (by, ay)
                v[ax].append((lo, hi, a, b))
            else:  # diagonal or zero-length
                diag.append((a, b))
        self._h: dict = {}
        self._hx: dict = {}
        for y, items in h.items():
            items.sort(key=lambda it: it[0])
            self._h[y] = items
            self._hx[y] = [it[0] for it in items]
        self._v: dict = {}
        self._vy: dict = {}
        for x, items in v.items():
            items.sort(key=lambda it: it[0])
            self._v[x] = items
            self._vy[x] = [it[0] for it in items]
        self._diag = diag

    def segments_through(self, p: tuple[int, int]):
        """Yield ``(a, b)`` for every stored segment ``p`` lies on (inclusive)."""
        px, py = p
        items = self._h.get(py)
        if items is not None:
            xs = self._hx[py]
            for i in range(bisect.bisect_right(xs, px)):
                _lo, hi, a, b = items[i]
                if hi >= px:
                    yield a, b
        items = self._v.get(px)
        if items is not None:
            ys = self._vy[px]
            for i in range(bisect.bisect_right(ys, py)):
                _lo, hi, a, b = items[i]
                if hi >= py:
                    yield a, b
        for a, b in self._diag:
            if _on_seg(p, a, b):
                yield a, b

    def interior_hits(self, p: tuple[int, int]):
        """Yield ``(a, b)`` for every segment ``p`` lies STRICTLY inside."""
        px, py = p
        items = self._h.get(py)
        if items is not None:
            xs = self._hx[py]
            for i in range(bisect.bisect_left(xs, px)):
                _lo, hi, a, b = items[i]
                if hi > px:
                    yield a, b
        items = self._v.get(px)
        if items is not None:
            ys = self._vy[px]
            for i in range(bisect.bisect_left(ys, py)):
                _lo, hi, a, b = items[i]
                if hi > py:
                    yield a, b
        for a, b in self._diag:
            if _on_seg_interior(p, a, b):
                yield a, b

    def has_interior_hit(self, p: tuple[int, int]) -> bool:
        for _ in self.interior_hits(p):
            return True
        return False

    def interior_count(self, p: tuple[int, int]) -> int:
        return sum(1 for _ in self.interior_hits(p))


# Vector bus label: NAME[a..b] -> NAMEa .. NAMEb (inclusive, either order).
_VECTOR_RE = re.compile(r"^(.*?)\[(\d+)\.\.(\d+)\]$")


def expand_bus_vector(text: str) -> list[str]:
    """Member net names of a vector bus label; ``[]`` when ``text`` is not one.

    ``D[0..7]`` -> ``D0..D7``; ``K[3..0]`` -> ``K3..K0`` — inclusive at both
    endpoints in either order (kicad-cli-verified: rips ``K3`` and ``K0`` off a
    ``K[3..0]`` bus both resolve). A plain (non-vector) label on a bus
    contributes NO members — eeschema treats it as a member-less bus, so an
    unlabeled rip next to it stays unconnected. Group notation ``{...}`` and
    bus aliases are out of scope: kicad-cli 10.0.4 ignores ``(bus_alias ...)``
    in netlist export (an alias-labeled bus is member-less, so this returns
    ``[]`` for the alias name — matching eeschema; see tests/test_bus_alias.py).
    """
    m = _VECTOR_RE.match(text)
    if m is None:
        return []
    prefix, a, b = m.group(1), int(m.group(2)), int(m.group(3))
    step = 1 if b >= a else -1
    return [f"{prefix}{i}" for i in range(a, b + step, step)]


def _name_priority(scope: str, power_priority: bool) -> int:
    """Rank an explicit name's scope. Higher wins as the canonical net name.

    Power ports outrank net labels when ``power_priority`` (PrjPcb
    ``PowerPortNamesTakePriority``) is set; otherwise net labels outrank power
    ports (the realistic Altium default). Both always outrank auto-names.
    """
    if scope in _POWER_SCOPES:
        return 3 if power_priority else 1
    if scope in _LABEL_SCOPES:
        return 2
    return 0


def _name_key(scope: str, text: str, sheet: str):
    """Merge key for the global same-name pass.

    Local labels are sheet-scoped ``(sheet, text)``; global-class names merge
    across sheets on ``text`` alone.
    """
    if scope in _GLOBAL_SCOPES:
        return ("\x00global", text)
    return (sheet, text)


def build_nets(
    prims: model.NetPrimitives, *, t_midspan_connects: bool = True
) -> list[model.Net]:
    """Infer nets from raw primitives. See module docstring for the pipeline.

    ``t_midspan_connects`` selects step 3's dialect: ``True`` (Altium — a wire
    vertex on another wire's mid-span connects with no explicit dot), ``False``
    (eeschema — only a junction node joins a mid-span touch).
    """
    dsu = _DSU()

    # Geometry is per-sheet: every node key is (sheet, qpoint), so two sheets
    # with identical coordinates are NOT geometrically connected. Cross-sheet
    # connectivity comes ONLY from the global same-name merge (Port/SheetEntry/
    # global label / power port). Segments are bucketed by sheet for on_seg.
    segs_by_sheet: dict[str, list[tuple[tuple[int, int], tuple[int, int]]]] = (
        defaultdict(list)
    )

    def _node(sheet: str, pt) -> tuple:
        return (sheet, pt)

    # (1) wire segments — union each segment's two endpoints (same sheet).
    for w in prims.wires:
        qa, qb = _q(w.a), _q(w.b)
        na, nb = _node(w.sheet, qa), _node(w.sheet, qb)
        dsu.add(na)
        dsu.add(nb)
        dsu.union(na, nb)
        segs_by_sheet[w.sheet].append((qa, qb))

    # One orthogonal-segment index per sheet — every geometric rule below is a
    # point-vs-all-segments query, quadratic under a linear scan; the index
    # makes each query O(log n + hits). Semantics are unchanged: integer coords
    # make each hit an exact coincidence (see SegmentIndex).
    index_by_sheet: dict[str, SegmentIndex] = {
        sheet: SegmentIndex(segs) for sheet, segs in segs_by_sheet.items()
    }

    # (2) junctions(29) — union a junction onto every same-sheet segment it lies on.
    junctions_by_sheet: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for j in prims.junctions:
        qj = _q(j.at)
        nj = _node(j.sheet, qj)
        dsu.add(nj)
        junctions_by_sheet[j.sheet].add(qj)
        idx = index_by_sheet.get(j.sheet)
        if idx is not None:
            for qa, _qb in idx.segments_through(qj):
                dsu.union(nj, _node(j.sheet, qa))

    # (3) T-junctions — a wire vertex on another same-sheet wire's mid-span.
    # Altium dialect only; eeschema requires a junction node (see docstring).
    # ``interior_hits(v)`` excludes every segment on which v is an endpoint
    # (strict interior), which subsumes the old ``k == i`` / ``v == a2/b2``
    # guards, so the same pairs merge.
    if t_midspan_connects:
        for sheet, segs in segs_by_sheet.items():
            idx = index_by_sheet[sheet]
            for qa, qb in segs:
                for v in (qa, qb):
                    for a2, _b2 in idx.interior_hits(v):
                        dsu.union(_node(sheet, v), _node(sheet, a2))

    # (4a) pins on segments (same sheet). A pin connects at a segment ENDPOINT,
    # or anywhere on the segment when a junction marks that exact point — but a
    # bare mid-span touch does NOT connect. This is eeschema's rule (and
    # Altium's: the editor inserts a junction record for every pin tap), and
    # diverging from it made `akcli net` claim connectivity KiCad rejects.
    pin_nodes: list[tuple[model.PinRef, tuple]] = []
    for ph in prims.pins:
        qp = _q(ph.at)
        np_ = _node(ph.sheet, qp)
        dsu.add(np_)
        has_junction = qp in junctions_by_sheet.get(ph.sheet, ())
        idx = index_by_sheet.get(ph.sheet)
        if idx is not None:
            for qa, qb in idx.segments_through(qp):
                if has_junction or qp == qa or qp == qb:
                    dsu.union(np_, _node(ph.sheet, qa))
        pin_nodes.append((ph.ref, np_))

    # (4b) labels / power ports on segments (same sheet).
    label_nodes: list[tuple[str, str, str, tuple]] = []
    for lb in prims.labels:
        ql = _q(lb.at)
        nl = _node(lb.sheet, ql)
        dsu.add(nl)
        idx = index_by_sheet.get(lb.sheet)
        if idx is not None:
            for qa, _qb in idx.segments_through(ql):
                dsu.union(nl, _node(lb.sheet, qa))
        label_nodes.append((lb.text, lb.scope, lb.sheet, nl))

    # (4c) BUS LAYER — kicad-cli-arbitrated (tests/test_kicad_parity.py):
    #   * bus segments form their own per-sheet clusters (shared endpoints +
    #     junctions, same wire rules); wires touching a bus do NOT join it.
    #   * a label anchored ON a bus attaches to that bus cluster (anywhere
    #     along it); bus clusters merge by name under the SAME scope rules as
    #     nets — a local ``D[0..7]`` is sheet-scoped, a global one crosses
    #     sheets, and the synthetic hier connectors stitch a parent sheet-pin
    #     bus to the child's hierarchical bus label.
    #   * vector labels expand to member net names (``expand_bus_vector``).
    #   * a (bus_entry) conducts between its two ends. Each end attaches to a
    #     WIRE at a wire endpoint or junction-marked point (mid-span bare
    #     touch does not attach — verified: the ripped wire floats), and to a
    #     BUS anywhere along a segment.
    #   * a rip joins the bus member selected by the WIRE-side cluster's own
    #     label; an unlabeled rip stays unconnected to members.
    bus_dsu = _DSU()
    bus_segs_by_sheet: dict[str, list[tuple[tuple[int, int], tuple[int, int]]]] = (
        defaultdict(list)
    )
    for bs in prims.buses:
        qa, qb = _q(bs.a), _q(bs.b)
        bus_dsu.union(_node(bs.sheet, qa), _node(bs.sheet, qb))
        bus_segs_by_sheet[bs.sheet].append((qa, qb))
    bus_index_by_sheet: dict[str, SegmentIndex] = {
        sheet: SegmentIndex(segs) for sheet, segs in bus_segs_by_sheet.items()
    }
    bus_label_atts: list[tuple[str, str, str, tuple]] = []
    if bus_segs_by_sheet:
        for j in prims.junctions:
            bidx = bus_index_by_sheet.get(j.sheet)
            if bidx is not None:
                qj = _q(j.at)
                for qa, _qb in bidx.segments_through(qj):
                    bus_dsu.union(_node(j.sheet, qj), _node(j.sheet, qa))
        if t_midspan_connects:
            for sheet, segs in bus_segs_by_sheet.items():
                bidx = bus_index_by_sheet[sheet]
                for qa, qb in segs:
                    for v in (qa, qb):
                        for a2, _b2 in bidx.interior_hits(v):
                            bus_dsu.union(_node(sheet, v), _node(sheet, a2))
        for lb in prims.labels:
            bidx = bus_index_by_sheet.get(lb.sheet)
            if bidx is None:
                continue
            ql = _q(lb.at)
            for qa, _qb in bidx.segments_through(ql):
                bus_dsu.union(_node(lb.sheet, ql), _node(lb.sheet, qa))
                bus_label_atts.append(
                    (lb.text, lb.scope, lb.sheet, _node(lb.sheet, ql))
                )
                break
        bus_name_groups: dict = defaultdict(list)
        for text, scope, sheet, bn in bus_label_atts:
            bus_name_groups[_name_key(scope, text, sheet)].append(bn)
        for nodes in bus_name_groups.values():
            for n in nodes[1:]:
                bus_dsu.union(nodes[0], n)
    members_by_busroot: dict = defaultdict(set)
    for text, _scope, _sheet, bn in bus_label_atts:
        members_by_busroot[bus_dsu.find(bn)].update(expand_bus_vector(text))

    # (4d) bus entries: conduct end<->end on the wire layer; record which bus
    # cluster(s) each entry taps for the member merge below.
    entry_taps: list[tuple[tuple, set]] = []
    for be in prims.bus_entries:
        q1, q2 = _q(be.a), _q(be.b)
        n1, n2 = _node(be.sheet, q1), _node(be.sheet, q2)
        dsu.add(n1)
        dsu.add(n2)
        idx = index_by_sheet.get(be.sheet)
        if idx is not None:
            juncs = junctions_by_sheet.get(be.sheet, ())
            for qe, ne in ((q1, n1), (q2, n2)):
                has_junction = qe in juncs
                for qa, qb in idx.segments_through(qe):
                    if has_junction or qe == qa or qe == qb:
                        dsu.union(ne, _node(be.sheet, qa))
        dsu.union(n1, n2)
        bidx = bus_index_by_sheet.get(be.sheet)
        if bidx is not None:
            roots = set()
            for qe in (q1, q2):
                for qa, _qb in bidx.segments_through(qe):
                    roots.add(bus_dsu.find(_node(be.sheet, qa)))
            if roots:
                entry_taps.append((n1, roots))

    # Member selection uses the GEOMETRIC wire cluster's own labels (eeschema
    # resolves drivers per connection subgraph, before any same-name merging).
    bus_merged_names: set[str] = set()
    if entry_taps:
        texts_by_root: dict = defaultdict(set)
        for text, _scope, _sheet, nl in label_nodes:
            texts_by_root[dsu.find(nl)].add(text)
        member_groups: dict = defaultdict(list)
        for n1, roots in entry_taps:
            r = dsu.find(n1)
            for br in roots:
                mem = members_by_busroot.get(br)
                if not mem:
                    continue
                for text in texts_by_root.get(r, ()):
                    if text in mem:
                        member_groups[(br, text)].append(n1)
        for (_br, text), nodes in member_groups.items():
            if len({dsu.find(n) for n in nodes}) > 1:
                bus_merged_names.add(text)
            for n in nodes[1:]:
                dsu.union(nodes[0], n)

    # (5)/(8) GLOBAL same-name merge — stitches disjoint clusters sharing a name
    # (the STAT fix; same-name GND collapse; cross-sheet Port/global join).
    name_groups: dict = defaultdict(list)
    global_on_sheet: dict = defaultdict(set)  # text -> sheets with a global anchor
    for text, scope, sheet, ql in label_nodes:
        name_groups[_name_key(scope, text, sheet)].append(ql)
        if scope in _GLOBAL_SCOPES and scope != "hier":
            global_on_sheet[text].add(sheet)
    # (5b) a LOCAL label also joins a same-name global-class name (power port /
    # global label) anchored on the SAME sheet — KiCad semantics, verified
    # against kicad-cli 10.0.4 netlists (tests/test_kicad_parity.py): eeschema
    # merges a local "X" with a same-sheet global "X" / power port EVEN WHEN
    # the two are physically disconnected, but NEVER across sheets (a child
    # sheet's local "+3V3" netlists as "/child/+3V3", separate from "+3V3").
    # This is what makes the label-on-pin pattern connect to rails.
    for key, nodes in list(name_groups.items()):
        first, text = key
        if first != "\x00global" and first in global_on_sheet.get(text, ()):
            name_groups[("\x00global", text)].extend(nodes)
    cross_cluster_names: set[str] = set()
    for (_, text), nodes in name_groups.items():
        roots = {dsu.find(n) for n in nodes}
        if len(roots) > 1:
            cross_cluster_names.add(text)
        first = nodes[0]
        for n in nodes[1:]:
            dsu.union(first, n)

    # (6)/(7) collect final clusters by root and assemble Net objects.
    members_by_root: dict = defaultdict(list)
    for ref, qp in pin_nodes:
        members_by_root[dsu.find(qp)].append(ref)
    names_by_root: dict = defaultdict(list)
    for text, scope, sheet, ql in label_nodes:
        if scope == "hier":
            continue  # synthetic connector: merges clusters, never names them
        names_by_root[dsu.find(ql)].append((text, scope))

    nets: list[model.Net] = []
    for root, members in members_by_root.items():
        uniq_members = sorted(set(members))
        if len(uniq_members) == 1 and not prims.emit_single_pin_nets:
            continue

        # distinct names on this net, preserving (name -> first-seen scope).
        scope_of: dict[str, str] = {}
        order: list[str] = []
        for text, scope in names_by_root.get(root, []):
            if text not in scope_of:
                scope_of[text] = scope
                order.append(text)

        merge_reasons: list[str] = []
        if order:
            ranked = sorted(
                order,
                key=lambda t: (-_name_priority(scope_of[t], prims.power_priority), t),
            )
            canonical = ranked[0]
            all_names = sorted(order)
            aliases = [n for n in all_names if n != canonical]
            is_named = True
            confidence = 1.0
            if len(all_names) > 1:
                confidence = _MULTI_NAME_CONFIDENCE
                merge_reasons.append("multiple names on one net: " + ", ".join(all_names))
            for n in all_names:
                if n in cross_cluster_names:
                    merge_reasons.append(f"global same-name merge: {n}")
                if n in bus_merged_names:
                    merge_reasons.append(f"bus member merge: {n}")
        else:
            canonical = None
            aliases = []
            all_names = []
            is_named = False
            confidence = 1.0

        nets.append(
            model.Net(
                name=canonical,
                members=uniq_members,
                aliases=aliases,
                source_names=all_names,
                is_named=is_named,
                confidence=confidence,
                merge_reasons=merge_reasons,
            )
        )

    # Deterministic, coordinate-independent ordering (membership-keyed).
    nets.sort(key=lambda n: n.members)
    return nets
