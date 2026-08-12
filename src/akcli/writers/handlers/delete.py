"""``delete_object`` + the shared top-level deletion machinery.

``delete_component``'s cascade (in :mod:`.components`) reuses the node
predicates here — hence this module sits BELOW components in the import
graph rather than beside it. Relocated VERBATIM from ``writers/kicad.py``
(0.21 batch R3b).
"""

from __future__ import annotations

from ... import units
from ...errors import fail
from ...geometry import transform as geometry
from ...sexpr import SNode
from ...sexpr.build import _at_nm
from ..registry import OpCall, _Note, op


def _delete_top_nodes(doc: SNode, keep) -> int:
    """Delete top-level list nodes for which ``keep(node)`` is False; return count."""
    removed = 0
    kids = doc.children or []
    for i in range(len(kids) - 1, -1, -1):
        c = kids[i]
        if c.is_list and not keep(c):
            del doc.children[i]
            del doc.ws[i]
            removed += 1
    return removed


def _pt_on_seg_nm(p: tuple[int, int], a: tuple[int, int],
                  b: tuple[int, int]) -> bool:
    """Exact integer-nm point-on-segment (endpoints inclusive)."""
    if not (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])):
        return False
    return ((b[0] - a[0]) * (p[1] - a[1])
            == (b[1] - a[1]) * (p[0] - a[0]))


def _xy_points_nm(node: SNode) -> list[tuple[int, int]]:
    """The ``(xy ...)`` endpoints of a wire/bus ``(pts ...)`` block, in nm."""
    pts = node.find("pts")
    out: list[tuple[int, int]] = []
    for xy in (pts.find_all("xy") if pts is not None else []):
        kids = xy.children or []
        if len(kids) >= 3:
            try:
                out.append((units.mm_to_nm(float(kids[1].value)),
                            units.mm_to_nm(float(kids[2].value))))
            except (TypeError, ValueError):
                continue
    return out


def _node_uuid(node: SNode) -> str | None:
    u = node.find("uuid")
    if u is not None and len(u.children or []) >= 2:
        return u.children[1].value
    return None


@op("delete_object")
def _op_delete_object(call: OpCall) -> list[str]:
    """Remove ONE top-level object by ``uuid`` or by a ``match`` selector.

    ``match`` is ``{kind, name?, at?}``: ``kind`` is the node tag (wire, label,
    global_label, ...), ``name`` matches a label/text's content, ``at`` an
    exact ``[x, y]`` mil anchor (a wire matches when EITHER endpoint lands
    there; the point is NOT grid-snapped, so a pin's exact off-grid coordinate
    matches too). Exactly-one semantics: 0 matches is a replay-safe note,
    >1 is an error listing the candidate uuids (tighten the selector).
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    if "match" in op:
        return _delete_object_match(doc, op["match"])
    uid = op["uuid"]

    def keep(c: SNode) -> bool:
        u = c.find("uuid")
        return not (u is not None and len(u.children or []) >= 2 and u.children[1].value == uid)

    removed = _delete_top_nodes(doc, keep)
    if removed == 0:
        raise _Note(f"no object with uuid {uid!r} (already absent)")
    return []


def _delete_object_match(doc: SNode, match: dict) -> list[str]:
    kind = match.get("kind")
    want_name = match.get("name")
    want_at = None
    if match.get("at") is not None:
        at = match["at"]
        want_at = (geometry.mil_to_nm(float(at[0])), geometry.mil_to_nm(float(at[1])))
    candidates: list[SNode] = []
    for c in doc.children or []:
        if not c.is_list or not (c.children or []) or c.children[0].value != kind:
            continue
        if want_name is not None:
            kids = c.children or []
            text = kids[1].value if len(kids) >= 2 and kids[1].is_atom else None
            if text != want_name:
                continue
        if want_at is not None:
            if kind in ("wire", "bus"):
                if want_at not in _xy_points_nm(c):
                    continue
            else:
                a = c.find("at")
                if a is None or _at_nm(a) != want_at:
                    continue
        candidates.append(c)
    if not candidates:
        raise _Note(f"no {kind} matches the selector (already absent)")
    if len(candidates) > 1:
        uuids = ", ".join(_node_uuid(c) or "<no-uuid>" for c in candidates)
        fail("VERIFY_FAILED",
             f"delete_object match is ambiguous: {len(candidates)} candidates: "
             f"{uuids} (add name/at to the selector or use uuid)")
    target = id(candidates[0])
    _delete_top_nodes(doc, lambda c: id(c) != target)
    return []
