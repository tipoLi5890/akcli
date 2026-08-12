"""Annotation ops: add_text, add_rectangle, add_text_box, set_title_block.

Pure annotation — netbuild never reads graphics or the title block, so every
op here is connectivity-neutral BY CONSTRUCTION (asserted by the netdiff
neutrality tests). Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch
R3b).
"""

from __future__ import annotations

from .. import identity
from ...errors import AkcliError
from ...geometry import transform as geometry
from ...sexpr import SNode
from ...sexpr.build import (
    _append_top_idempotent,
    _at,
    _atom,
    _doc_child_indent,
    _effects,
    _fill,
    _list,
    _mm,
    _q,
    _stroke,
    _unescape_atom,
    _uuid_node,
)
from ..anchors import _point_nm
from ..context import ExecCtx, _claim_uid, root_uuid
from ..registry import OpCall, _Note, op


@op("add_text")
def _op_add_text(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    p = _point_nm(op["at"])
    angle = float(op.get("angle", 0))
    root = root_uuid(doc)
    uid = _annotation_uuid(root, "text", op, f"{p[0]}:{p[1]}", idx, ctx)
    node = _list(
        _atom("text"),
        _atom(_q(str(op["text"]))),
        _at(p, angle),
        _effects(),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _annotation_uuid(root: str | None, tag: str, op: dict,
                     coord_seed: str, idx: int, ctx: ExecCtx | None = None) -> str:
    """Deterministic uuid for a graphic/annotation node.

    Default seed follows the wire convention: the node's COORDINATES, never the
    op index (an op-list edit that renumbers ops must not re-identify — and so
    duplicate — every annotation downstream of it). Editing the text of an
    annotation therefore rewrites the node that is already there; two keyless
    annotations of one kind on one point collide and are refused
    (:func:`_claim_uid`). An optional ``key`` gives the node a STABLE handle
    independent of its coordinates too: re-emitting the same key replaces the
    node in place (``_append_top_idempotent``), which is what lets a group frame
    refresh itself after parts move instead of piling up.
    """
    key = op.get("key")
    if isinstance(key, str) and key:
        uid = identity.node_uuid(root, identity.seed_annotation_key(tag, key))
        claimed = ctx.annotation_uids if ctx is not None else {}
        prev = claimed.setdefault(uid, (idx, tag, key))
        if prev != (idx, tag, key):
            raise AkcliError(
                "OP_UNSUPPORTED",
                f"op [{idx}] {tag} key {key!r} collides with op [{prev[0]}] "
                f"(same key seeds the same uuid; the later op would replace "
                "the earlier node) — use distinct keys")
        return uid
    uid = identity.node_uuid(root, identity.seed_annotation_coords(tag, coord_seed))
    return _claim_uid(ctx, uid, idx, f"{tag} at {coord_seed}",
                      'give one of them a distinct "at", or a "key" to '
                      "address them separately")


@op("add_rectangle")
def _op_add_rectangle(call: OpCall) -> list[str]:
    """Emit a top-level graphic ``(rectangle ...)`` (SPEC §2.2, add_rectangle).

    Pure annotation — netbuild never reads graphics, so a rectangle is
    connectivity-neutral by construction. The primary customer is the
    functional-group frame (``akcli groups --frame``), which draws a border
    box around a module's bounding box; a ``key`` keeps the frame's uuid
    stable so a refresh replaces it in place.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    a = _point_nm(op["start"])
    b = _point_nm(op["end"])
    width_nm = geometry.mil_to_nm(float(op.get("stroke_width_mil", 0) or 0))
    root = root_uuid(doc)
    uid = _annotation_uuid(root, "rectangle", op,
                           f"{a[0]}:{a[1]}:{b[0]}:{b[1]}", idx, ctx)
    node = _list(
        _atom("rectangle"),
        _list(_atom("start"), _mm(a[0]), _mm(a[1])),
        _list(_atom("end"), _mm(b[0]), _mm(b[1])),
        _stroke(width_nm),
        _fill(str(op.get("fill", "none"))),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


@op("add_text_box")
def _op_add_text_box(call: OpCall) -> list[str]:
    """Emit a bordered note box ``(text_box ...)`` (SPEC §2.2, add_text_box).

    Multi-line annotation with a border — module notes, design rationale,
    bring-up instructions. Grammar is fixture-verified against a real KiCad
    (tests/fixtures/kicad/text_box.kicad_sch, accepted by kicad-cli export +
    ERC): content, exclude_from_sim, at, size, stroke, fill, effects
    (left/top justified), uuid. ``at`` is the TOP-LEFT corner; connectivity-
    neutral like every graphic; an optional ``key`` gives a stable handle.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    p = _point_nm(op["at"])
    size = op["size"]
    size_nm = (geometry.mil_to_nm(float(size[0])),
               geometry.mil_to_nm(float(size[1])))
    angle = float(op.get("angle", 0))
    root = root_uuid(doc)
    uid = _annotation_uuid(root, "text_box", op, f"{p[0]}:{p[1]}", idx, ctx)
    node = _list(
        _atom("text_box"),
        _atom(_q(str(op["text"]))),
        _list(_atom("exclude_from_sim"), _atom("no")),
        _at(p, angle),
        _list(_atom("size"), _mm(size_nm[0]), _mm(size_nm[1])),
        _stroke(),
        _fill(),
        _effects(justify="left top"),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


_TITLE_BLOCK_SIMPLE = ("title", "date", "rev", "company")


def _set_tb_field(tb: SNode, tag: str, comment_n: int | None, value: str) -> int:
    """Set/replace one title_block child; 1 when the stored value changed."""
    val_idx = 1 if comment_n is None else 2
    for c in tb.children or []:
        if not c.is_list or not (c.children or []) or c.children[0].value != tag:
            continue
        kids = c.children
        if comment_n is not None and not (
                len(kids) >= 2 and str(kids[1].value) == str(comment_n)):
            continue
        if len(kids) > val_idx and _unescape_atom(str(kids[val_idx].value)) == value:
            return 0
        atom = _atom(_q(value))
        if len(kids) > val_idx:
            kids[val_idx] = atom
        else:
            c.children.append(atom)                # type: ignore[union-attr]
            c.ws.insert(len(c.ws) - 1, " ")        # type: ignore[union-attr]
        return 1
    kids = [_atom(tag)]
    if comment_n is not None:
        kids.append(_atom(str(comment_n)))
    kids.append(_atom(_q(value)))
    node = _list(*kids)
    k = len(tb.children or [])
    tb.children.append(node)                       # type: ignore[union-attr]
    tb.ws.insert(k, _doc_child_indent(tb))         # type: ignore[union-attr]
    return 1


@op("set_title_block")
def _op_set_title_block(call: OpCall) -> list[str]:
    """Set/replace title-block fields (SPEC §2.2, set_title_block).

    Find-or-create of the single ``(title_block ...)`` node — inserted after
    ``(paper ...)`` and before ``(lib_symbols ...)``, the position eeschema
    expects (elsewhere triggers a repair prompt). The node has no uuid, so
    idempotency is find-or-replace-by-tag; an op that changes nothing raises
    a replay-safe note. Pure metadata: netbuild never reads the title block,
    so the op is connectivity-neutral by construction.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    tb = doc.find("title_block")
    if tb is None:
        tb = _list(_atom("title_block"))
        pos = None
        for i, c in enumerate(doc.children or []):
            if c.is_list and (c.children or []) and c.children[0].value == "paper":
                pos = i + 1
                break
        if pos is None:
            for i, c in enumerate(doc.children or []):
                if (c.is_list and (c.children or [])
                        and c.children[0].value == "lib_symbols"):
                    pos = i
                    break
        if pos is None:
            pos = len(doc.children or [])
        doc.children.insert(pos, tb)               # type: ignore[union-attr]
        doc.ws.insert(pos, _doc_child_indent(doc))  # type: ignore[union-attr]
    changed = 0
    for fname in _TITLE_BLOCK_SIMPLE:
        if op.get(fname) is not None:
            changed += _set_tb_field(tb, fname, None, str(op[fname]))
    for n in range(1, 10):
        v = op.get(f"comment{n}")
        if v is not None:
            changed += _set_tb_field(tb, "comment", n, str(v))
    if not changed:
        raise _Note("title_block already matches (nothing to change)")
    return []
