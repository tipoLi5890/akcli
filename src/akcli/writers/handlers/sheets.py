"""Hierarchical ``add_sheet``: the child-sheet node + its pins + paging.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
"""

from __future__ import annotations

from pathlib import Path

from .. import identity, instances
from ...errors import fail
from ...geometry import transform as geometry
from ...sexpr.build import (
    _append_top_idempotent,
    _at,
    _atom,
    _effects,
    _list,
    _mm,
    _q,
    _stroke,
    _uuid_node,
)
from ..anchors import _GRID_NM
from ..context import _claim_uid
from ..registry import OpCall, op


# KiCad sheet-pin edge angle: right side = 0 (verified against real files);
# left/top/bottom follow. Only the (x, y) anchor matters for connectivity — the
# angle is cosmetic (which way the pin name text renders).
_SHEET_PIN_ANGLE = {"right": 0, "left": 180, "top": 90, "bottom": 270}


def _sheet_pin_pos(
    origin: tuple[int, int], size: tuple[int, int], side: str, off_nm: int,
) -> tuple[int, int]:
    """Edge-anchor (nm) of a sheet pin on ``side`` at ``off_nm`` from the corner."""
    x0, y0 = origin
    w, h = size
    if side == "left":
        return (x0, y0 + off_nm)
    if side == "right":
        return (x0 + w, y0 + off_nm)
    if side == "top":
        return (x0 + off_nm, y0)
    return (x0 + off_nm, y0 + h)             # bottom


@op("add_sheet")
def _op_add_sheet(call: OpCall) -> list[str]:
    """Emit a hierarchical ``(sheet ...)`` node (SPEC §2.2, add_sheet).

    Writes Sheetname/Sheetfile properties, stroke/fill defaults, a deterministic
    uuid, one sheet pin per ``pins`` entry at its computed edge coordinate, and
    the ``(instances (project ... (path "/<root>" (page N))))`` block KiCad needs
    to page the sub-sheet. The referenced child ``.kicad_sch`` is NOT created
    here — author it FIRST (``akcli new <file>``): a sheet pointing at a file
    that does not exist is refused with ``SHEET_FILE_MISSING``, because it used
    to "succeed" and then brick every later ``nets``/``check``/``render`` on the
    design (none of them can read the missing child).
    Wires attach to a sheet pin
    by its coordinate: ``at`` + ``offset_mil`` along the pin's side, grid-snapped
    (the connectivity gate already treats sheet pins as label anchors, so a wire
    ending there does not dangle).
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    file_name = str(op["file"])
    doc_path = ctx.doc_path if ctx is not None else None
    if doc_path is not None and not (Path(doc_path).parent / file_name).exists():
        fail("SHEET_FILE_MISSING",
             f"add_sheet {str(op['name'])!r}: child file {file_name!r} does not "
             f"exist next to {Path(doc_path).name} — create it first "
             f"(`akcli new {file_name}`), then re-run this op-list")
    origin = geometry.grid_snap_nm(
        (geometry.mil_to_nm(float(op["at"][0])), geometry.mil_to_nm(float(op["at"][1]))),
        _GRID_NM,
    )
    size = (geometry.mil_to_nm(float(op["size"][0])),
            geometry.mil_to_nm(float(op["size"][1])))
    root = instances.root_uuid(doc)
    name = str(op["name"])
    # Seeded by child FILE + sheet name, never the op index: two sheets naming
    # the same child stay one node, two same-named sheets on different files
    # stay distinct, and an op-list edit replays onto the SAME sub-sheet node.
    sheet_uuid = identity.node_uuid(root, identity.seed_sheet(file_name, name))
    _claim_uid(ctx, sheet_uuid, idx, f"sheet {name!r} -> {file_name!r}",
               "two sheets cannot share both a name and a child file; rename one")

    x0, y0 = origin
    w, h = size
    children = [
        _list(_atom("at"), _mm(x0), _mm(y0)),
        _list(_atom("size"), _mm(w), _mm(h)),
        _stroke(),
        _list(_atom("fill"), _list(_atom("color"), _atom("0"), _atom("0"),
                                   _atom("0"), _atom("0.0000"))),
        _uuid_node(sheet_uuid),
        _list(
            _atom("property"), _atom(_q("Sheetname")), _atom(_q(name)),
            _at((x0, y0), 0), _effects(justify="left bottom"),
        ),
        _list(
            _atom("property"), _atom(_q("Sheetfile")), _atom(_q(str(op["file"]))),
            _at((x0, y0 + h), 0), _effects(justify="left top"),
        ),
    ]
    for pin in op.get("pins") or []:
        side = pin["side"]
        off_nm = geometry.mil_to_nm(float(pin["offset_mil"]))
        pos = geometry.grid_snap_nm(
            _sheet_pin_pos(origin, size, side, off_nm), _GRID_NM)
        pname = str(pin["name"])
        puid = identity.node_uuid(
            root, identity.seed_sheet_pin(file_name, name, pname))
        children.append(_list(
            _atom("pin"), _atom(_q(pname)), _atom(str(pin["type"])),
            _at(pos, _SHEET_PIN_ANGLE.get(side, 0)),
            _effects(),
            _uuid_node(puid),
        ))
    # (instances): a sub-sheet is paged under the root path; page number is
    # per-op-index so replaying the same op-list stays byte-identical.
    proj = instances.project_name(doc)
    page = _list(_atom("page"), _atom(_q(str(idx + 2))))
    inst = _list(
        _atom("instances"),
        _list(_atom("project"), _atom(_q(proj)),
              _list(_atom("path"), _atom(_q("/" + root)), page)),
    )
    children.append(inst)
    _append_top_idempotent(doc, _list(_atom("sheet"), *children), sheet_uuid)
    return [sheet_uuid]
