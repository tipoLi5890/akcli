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
3. T-junction: union every wire vertex lying on another wire's mid-span.
4. Union pins/labels lying on a segment (exact-integer cross-product ``on_seg``).
   Pins connect at segment ENDPOINTS or at a junction-marked point — a bare
   mid-span touch does not connect (eeschema's rule; Altium's editor inserts a
   junction record for every pin tap). Labels connect anywhere along the wire.
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

from collections import defaultdict

from . import model

# Quantization factor: floats (mils) -> exact integers. 1e4 preserves the
# Altium ``_Frac`` sub-unit resolution (10 mil / 100000) and absorbs the tiny
# float error from mm↔mil conversions without merging genuinely distinct points.
_QUANT = 10_000

# Scope buckets. Local net labels are sheet-LOCAL (only merge within one sheet);
# everything else is global and merges across sheets by name.
_POWER_SCOPES = frozenset({"power"})
_GLOBAL_SCOPES = frozenset({"global", "power", "port", "sheet_entry"})
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


def build_nets(prims: model.NetPrimitives) -> list[model.Net]:
    """Infer nets from raw primitives. See module docstring for the pipeline."""
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

    # (2) junctions(29) — union a junction onto every same-sheet segment it lies on.
    junctions_by_sheet: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for j in prims.junctions:
        qj = _q(j.at)
        nj = _node(j.sheet, qj)
        dsu.add(nj)
        junctions_by_sheet[j.sheet].add(qj)
        for qa, qb in segs_by_sheet.get(j.sheet, ()):  # noqa: B007
            if _on_seg(qj, qa, qb):
                dsu.union(nj, _node(j.sheet, qa))

    # (3) T-junctions — a wire vertex on another same-sheet wire's mid-span.
    for sheet, segs in segs_by_sheet.items():
        for i, (qa, qb) in enumerate(segs):
            for v in (qa, qb):
                for k, (a2, b2) in enumerate(segs):
                    if k == i or v == a2 or v == b2:
                        continue
                    if _on_seg(v, a2, b2):
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
        for qa, qb in segs_by_sheet.get(ph.sheet, ()):
            if not _on_seg(qp, qa, qb):
                continue
            if has_junction or qp == qa or qp == qb:
                dsu.union(np_, _node(ph.sheet, qa))
        pin_nodes.append((ph.ref, np_))

    # (4b) labels / power ports on segments (same sheet).
    label_nodes: list[tuple[str, str, str, tuple]] = []
    for lb in prims.labels:
        ql = _q(lb.at)
        nl = _node(lb.sheet, ql)
        dsu.add(nl)
        for qa, qb in segs_by_sheet.get(lb.sheet, ()):
            if _on_seg(ql, qa, qb):
                dsu.union(nl, _node(lb.sheet, qa))
        label_nodes.append((lb.text, lb.scope, lb.sheet, nl))

    # (5)/(8) GLOBAL same-name merge — stitches disjoint clusters sharing a name
    # (the STAT fix; same-name GND collapse; cross-sheet Port/global join).
    name_groups: dict = defaultdict(list)
    for text, scope, sheet, ql in label_nodes:
        name_groups[_name_key(scope, text, sheet)].append(ql)
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
