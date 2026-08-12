"""Repair drafts that make a group re-pack net-safe (``arrange --propose-labels``).

``plan_groups`` moves every bundle rigidly but INDEPENDENTLY, so connectivity
that lives in GEOMETRY between two bundles can split when they separate — the
net-preservation gate then refuses the re-pack. This module turns that refusal
into a mechanical fix: an op-list draft that makes the same connectivity
NAME-backed (a label on every grouped pin of a spanning net) and removes the
copper that would otherwise be stretched across the page.

Nothing here touches the target file: the draft is a plain op-list document
the caller writes out and the author applies through the ordinary
``plan``/``draw`` pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..classify import natkey
from ..contract import PROTOCOL_VERSIONS
from ..geometry.segments import UnionFind, on_segment, point_key
from ..sexpr.query import child_mil

__all__ = ["LabelProposal", "propose_group_labels", "redundant_wire_deletes"]


@dataclass(frozen=True)
class LabelProposal:
    """A ``--propose-labels`` draft plus the counts the CLI reports."""

    doc: dict[str, Any]                 # op-list document (never written here)
    label_ops: int                      # add_net_label ops
    fix_ops: int                        # wire/junction/satellite fix ops
    spanning: int                       # named nets touching >= 2 bundles
    unnamed: tuple[str, ...]            # multi-bundle nets a human must name


def propose_group_labels(target: Path,
                         groups: dict[str, list[str]]) -> LabelProposal:
    """Build a label-on-pin op-list draft that makes a re-pack net-safe.

    ``arrange --groups`` moves every bundle (component + its power
    satellites) rigidly but INDEPENDENTLY — so any net that relies on
    geometry BETWEEN two bundles (a connecting wire, pin-tip coincidence)
    can split when they move, same group or not. The fix is the label-on-pin
    discipline: every NAMED net touching >= 2 grouped components gets an
    ``add_net_label`` on each grouped member pin, making connectivity
    name-backed and move-invariant (the exact property the net-preservation
    gate demands). The draft NEVER touches the file — apply it with
    ``plan``/``draw`` like any op-list, then re-run ``arrange --groups``.
    Unnamed multi-bundle nets are reported (they need a human-chosen name).
    """
    from ..readers import kicad as kreader
    sch = kreader.read_sch(str(target))
    ref_group: dict[str, str] = {}
    for gname, refs in groups.items():
        for ref in refs:
            if not ref.startswith("#"):
                ref_group[ref] = gname
    label_ops: list[dict[str, Any]] = []
    unnamed: list[str] = []
    spanning = 0
    for net in sorted(sch.nets, key=lambda n: n.name or ""):
        grouped = [(r, p) for r, p in net.members if r in ref_group]
        if len({r for r, _p in grouped}) < 2:   # single-bundle nets are safe
            continue
        if not net.is_named or not net.name:
            ref, pin = grouped[0]
            unnamed.append(net.name or f"<unnamed@{ref}.{pin}>")
            continue
        spanning += 1
        for r, pin in sorted(grouped, key=lambda m: (natkey(m[0]), natkey(m[1]))):
            label_ops.append({"op": "add_net_label", "name": net.name,
                              "at": f"{r}.{pin}"})
    labeled_nets = {op["name"] for op in label_ops}
    delete_ops = redundant_wire_deletes(target, sch, ref_group, labeled_nets)
    doc = {"protocol_version": PROTOCOL_VERSIONS["oplist"],
           "target_format": "kicad",
           "target_file": target.name, "ops": label_ops + delete_ops}
    return LabelProposal(doc=doc, label_ops=len(label_ops),
                         fix_ops=len(delete_ops), spanning=spanning,
                         unnamed=tuple(unnamed))


def redundant_wire_deletes(target: Path, sch: Any, ref_group: dict[str, str],
                           labeled: set[str]) -> list[dict[str, Any]]:
    """``delete_object`` ops for wire clusters the new labels make redundant.

    A stretched wire is the remaining hazard after labeling: rigid bundle
    moves drag cross-bundle wires into diagonals that can sweep across
    unrelated pins and MERGE nets. A wire cluster (segments transitively
    connected by shared endpoints, tee-touches and junctions) is deleted only
    when it is provably redundant: every component pin it touches sits on a
    net that just received pin labels — so removing the copper cannot lose
    connectivity, it can only remove the failure mode. Junctions inside a
    deleted cluster go with it.
    """
    from .. import sexpr as _sexpr
    from ..safety import read_text_guarded as _read_text_guarded

    # pin position -> (ref, pin_number); (ref, pin) -> net name
    pos_pin: dict[tuple[int, int], tuple[str, str]] = {}
    for c in sch.components:
        for pin in c.pins:
            pos_pin[point_key(pin.x_mil, pin.y_mil)] = (c.designator, pin.number)
    pin_net: dict[tuple[str, str], str | None] = {}
    for net in sch.nets:
        for r, p in net.members:
            pin_net[(r, p)] = net.name if net.is_named else None

    root = _sexpr.parse(_read_text_guarded(target))
    segs: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for wire in root.find_all("wire"):
        pts = wire.find("pts")
        un = wire.find("uuid")
        if pts is None or un is None or len(un.children or []) < 2:
            continue
        xy = pts.find_all("xy")
        if len(xy) < 2:
            continue
        p0 = (child_mil(xy[0], 1), child_mil(xy[0], 2))
        p1 = (child_mil(xy[-1], 1), child_mil(xy[-1], 2))
        segs.append((str((un.children or [])[1].value), p0, p1))

    # union-find over segments: shared endpoints AND endpoint-on-segment tees
    uf = UnionFind(len(segs))

    by_point: dict[tuple[int, int], list[int]] = {}
    for i, (_u, p0, p1) in enumerate(segs):
        for pt in (p0, p1):
            by_point.setdefault(point_key(*pt), []).append(i)
    for indices in by_point.values():
        for i in indices[1:]:
            uf.union(indices[0], i)
    for i, (_u, p0, p1) in enumerate(segs):
        for j, (_u2, q0, q1) in enumerate(segs):
            if i != j and (on_segment(q0, p0, p1) or on_segment(q1, p0, p1)):
                uf.union(i, j)

    clusters = uf.clusters()

    junctions: list[tuple[str, tuple[int, int]]] = []   # (uuid, keyed position)
    for jn in root.find_all("junction"):
        at, un = jn.find("at"), jn.find("uuid")
        if at is None or un is None or len(un.children or []) < 2:
            continue
        junctions.append((str((un.children or [])[1].value),
                          point_key(child_mil(at, 1), child_mil(at, 2))))

    deletes: list[dict[str, Any]] = []
    deleted_idx: set[int] = set()
    for members in clusters.values():
        touched = []
        pts_seen: set[tuple[int, int]] = set()
        for i in members:
            _u, p0, p1 = segs[i]
            pts_seen.add(point_key(*p0))
            pts_seen.add(point_key(*p1))
        for pk in pts_seen:
            if pk in pos_pin:
                touched.append(pos_pin[pk])
        real = [(r, p) for r, p in touched if not r.startswith("#")]
        if len({r for r, _p in real}) < 2:
            continue                             # intra-bundle copper stays
        if any(pin_net.get(t) not in labeled for t in real):
            continue                             # some pin is NOT name-backed
        for i in sorted(members, key=lambda n: segs[n][0]):
            deletes.append({"op": "delete_object", "uuid": segs[i][0]})
            deleted_idx.add(i)
        for juuid, jpos in junctions:
            if jpos in pts_seen:
                deletes.append({"op": "delete_object", "uuid": juuid})

    # `#` satellites (PWR_FLAG, mid-wire power ports) connect by TOUCH. One
    # that neither sits on a real pin nor TERMINATES a surviving wire (it
    # rides mid-segment, or its copper is being deleted) will strand when
    # bundles move — relocate it onto the first real pin of its net, where
    # pin-tip coincidence makes it ride that host's bundle through any
    # re-pack. Endpoint-terminating ports stay: moving them would leave the
    # wire dangling.
    sat_moves: list[dict[str, Any]] = []
    real_tips = {point_key(q.x_mil, q.y_mil)
                 for c in sch.components if not c.designator.startswith("#")
                 for q in c.pins}
    real_pin_pos = {(c.designator, q.number): (q.x_mil, q.y_mil)
                    for c in sch.components
                    if not c.designator.startswith("#") for q in c.pins}
    surviving_ends = {point_key(*pt)
                      for i, (_u, p0, p1) in enumerate(segs)
                      if i not in deleted_idx for pt in (p0, p1)}
    for net in sch.nets:
        real = sorted((m for m in net.members if m in real_pin_pos),
                      key=lambda m: (m[0], m[1]))
        if not real:
            continue
        for r, p in net.members:
            if not r.startswith("#"):
                continue
            comp = next(c for c in sch.components if c.designator == r)
            pin = next((q for q in comp.pins if q.number == p), None)
            if pin is None:
                continue
            tip = point_key(pin.x_mil, pin.y_mil)
            if tip in real_tips or tip in surviving_ends:
                continue          # pin-coincident, or it anchors a live wire
            hx, hy = real_pin_pos[real[0]]
            sat_moves.append({
                "op": "move_component", "designator": r,
                "x_mil": comp.x_mil + (hx - pin.x_mil),
                "y_mil": comp.y_mil + (hy - pin.y_mil)})
    return sat_moves + deletes
