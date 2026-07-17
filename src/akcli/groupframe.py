"""Functional-group frames — border rectangle + title per module.

The visual half of the groups feature: membership lives on placed symbols as
the hidden ``Group`` property (written by grouped ops), and this module turns
it back into geometry — per group, the world bounding box of every member
(body ∪ pin tips, the same math ``arrange``/``check --layout`` use) padded by
a margin, emitted as ``add_rectangle`` + ``add_text`` ops through the standard
draw pipeline (atomic write, verify gate, ``.bak``/undo).

Frames refresh in place: both ops carry a ``key`` (``group_frame:<name>`` /
``group_title:<name>``), so their uuids are stable across coordinate changes —
move the parts, re-run ``akcli groups --frame --apply``, and the old border is
replaced, never accumulated.
"""

from __future__ import annotations

import math
from pathlib import Path

from .readers import kicad as _krd
from .readers import sexpr

FRAME_MARGIN_MIL = 200.0
TITLE_LIFT_MIL = 100.0
_GRID_MIL = 50.0


def _frame_key(name: str) -> str:
    return f"group_frame:{name}"


def _title_key(name: str) -> str:
    return f"group_title:{name}"


def frame_uuid(root_uuid: str | None, name: str) -> str:
    """The deterministic uuid ``add_rectangle {key: group_frame:<name>}`` gets."""
    from .writers import instances
    return instances.deterministic_uuid(root_uuid, f"rectangle:key:{_frame_key(name)}", 0)


def _snap_out(v: float, *, up: bool) -> float:
    """Snap outward (away from the box) to the 50-mil grid."""
    f = math.ceil if up else math.floor
    return f(v / _GRID_MIL) * _GRID_MIL


def group_boxes(path: str | Path) -> dict[str, dict]:
    """``{group: {"members": [ref, ...], "box": [x0, y0, x1, y1]}}`` (world mils).

    Groups come from the ``Group`` symbol property; each member's box is its
    drawn body UNION pin tips (via ``arrange._collect``), so the union can
    never disagree with where wires actually land.
    """
    from . import arrange as _arr

    root = sexpr.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    syms, _anchors = _arr._collect(root)
    boxes_by_ref: dict[str, list] = {}
    for s in syms:
        b = boxes_by_ref.setdefault(s.ref, [s.box.x0, s.box.y0, s.box.x1, s.box.y1])
        # multi-unit parts: several placed instances share one ref — union them
        b[0] = min(b[0], s.box.x0)
        b[1] = min(b[1], s.box.y0)
        b[2] = max(b[2], s.box.x1)
        b[3] = max(b[3], s.box.y1)

    groups: dict[str, dict] = {}
    for sym in _krd._placed_symbols(root):
        props = _krd._props(sym)
        gname = props.get("Group")
        ref = props.get("Reference")
        if not gname or not ref or ref not in boxes_by_ref:
            continue
        b = boxes_by_ref[ref]
        entry = groups.setdefault(gname, {"members": set(), "box": list(b)})
        entry["members"].add(ref)
        eb = entry["box"]
        eb[0] = min(eb[0], b[0])
        eb[1] = min(eb[1], b[1])
        eb[2] = max(eb[2], b[2])
        eb[3] = max(eb[3], b[3])
    for entry in groups.values():
        entry["members"] = sorted(entry["members"])
    return dict(sorted(groups.items()))


def group_report(path: str | Path) -> list[dict]:
    """Per-group summary rows for ``akcli groups`` (members, box, frame state)."""
    from .writers.kicad import _root_uuid

    root = sexpr.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    root_uuid = _root_uuid(root)
    present: set[str] = set()
    for c in root.children or []:
        if not c.is_list or not (c.children or []) or c.children[0].value != "rectangle":
            continue
        un = c.find("uuid")
        if un is not None and len(un.children or []) >= 2:
            present.add(str(un.children[1].value))

    rows: list[dict] = []
    for name, info in group_boxes(path).items():
        x0, y0, x1, y1 = info["box"]
        rows.append({
            "name": name,
            "members": info["members"],
            "box_mil": [x0, y0, x1, y1],
            "width_mil": x1 - x0,
            "height_mil": y1 - y0,
            "has_frame": frame_uuid(root_uuid, name) in present,
        })
    return rows


def plan_frames(path: str | Path, *, groups_meta: dict | None = None,
                margin_mil: float = FRAME_MARGIN_MIL) -> list[dict]:
    """The ``add_rectangle``/``add_text`` ops that (re)draw every group frame.

    ``groups_meta`` may map a group name to ``{"title": ...}`` (the op-list
    envelope shape); the title defaults to the group name. Frame corners snap
    OUTWARD to the grid so the border never clips a member.
    """
    meta = groups_meta or {}
    ops: list[dict] = []
    for name, info in group_boxes(path).items():
        x0, y0, x1, y1 = info["box"]
        start = [_snap_out(x0 - margin_mil, up=False),
                 _snap_out(y0 - margin_mil, up=False)]
        end = [_snap_out(x1 + margin_mil, up=True),
               _snap_out(y1 + margin_mil, up=True)]
        gmeta = meta.get(name) if isinstance(meta.get(name), dict) else {}
        title = gmeta.get("title") or name
        ops.append({"op": "add_rectangle", "start": start, "end": end,
                    "key": _frame_key(name)})
        ops.append({"op": "add_text", "text": str(title),
                    "at": [start[0], start[1] - TITLE_LIFT_MIL],
                    "key": _title_key(name)})
    return ops
