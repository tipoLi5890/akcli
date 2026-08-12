"""Wire/bus ops: add_wire, add_bus, route_net, junction, no_connect, entry.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b). ``add_wire``
and ``add_bus`` share :func:`_add_wire` (the historical ``tag=`` keyword on
one handler, which the single-argument ``OpCall`` signature no longer
carries); ``route_net`` reuses the same segment emitter, so an auto-routed
span and a hand-listed one replay to byte-identical nodes.
"""

from __future__ import annotations

from .. import identity
from ... import units
from ...errors import AkcliError, fail
from ...geometry import transform as geometry
from ...readers import kicad_lib
from ...sexpr import SNode
from ...sexpr.build import (
    _append_top_idempotent,
    _atom,
    _list,
    _mil_pair,
    _mm,
    _pts,
    _stroke,
    _uuid_node,
)
from ..anchors import _point_nm, _resolve_endpoint, _resolve_pin_world, _snap_within_nm
from ..context import (
    ExecCtx,
    _claim_uid,
    _ctx_symdef,
    _inst_lib_id,
    _instance_component,
    _placed_symbols,
    _symbol_unit,
    root_uuid,
)
from ..registry import OpCall, op
from .labels import _anchor_seed, _emit_net_label

# Default bus-entry size: 2.54 mm @ 45° (SPEC §2.2).
_BUS_ENTRY_NM = units.mm_to_nm(2.54)


# add_wire / add_bus differ only by the node tag; the shared body stays
# one function so both replay through the same seeds.
def _add_wire(doc, op, idx, src_libs, path, ctx, tag="wire") -> list[str]:
    verts = op["vertices"]
    pts = [_resolve_endpoint(doc, ctx, v) for v in verts]
    root = root_uuid(doc)
    return _emit_wire_segments(doc, root, idx, pts, tag, ctx)


@op("add_wire")
def _op_add_wire(call: OpCall) -> list[str]:
    return _add_wire(call.doc, call.op, call.index, call.src_libs,
                     call.inst_path, call.ctx, tag="wire")


@op("add_bus")
def _op_add_bus(call: OpCall) -> list[str]:
    return _add_wire(call.doc, call.op, call.index, call.src_libs,
                     call.inst_path, call.ctx, tag="bus")


def _emit_wire_segments(
    doc: SNode, root: str | None, idx: int,
    pts: list[tuple[int, int]], tag: str, ctx: ExecCtx | None = None,
) -> list[str]:
    """One ``(wire ...)``/``(bus ...)`` node per consecutive point pair.

    Shared by ``add_wire``/``add_bus`` and ``route_net``. The uuid seed is the
    segment's coordinates ALONE (no op index), so identical geometry replays
    byte-identically regardless of which op produced it *and* survives an edit
    that renumbers the op-list — inserting one op at the front used to re-seed
    every downstream wire and duplicate it on the next apply.
    """
    created: list[str] = []
    for a, b in zip(pts, pts[1:]):
        if a == b:
            continue
        seed = f"{tag}:{a[0]}:{a[1]}:{b[0]}:{b[1]}"
        uid = identity.node_uuid(root, seed)
        _claim_uid(ctx, uid, idx, f"{tag} {_mil_pair(a)}-{_mil_pair(b)}",
                   "drop the duplicate segment (one wire per span is enough)")
        node = _list(_atom(tag), _pts([a, b]), _stroke(), _uuid_node(uid))
        _append_top_idempotent(doc, node, uid)
        created.append(uid)
    return created


def _pin_tip_set(doc: SNode, ctx: ExecCtx) -> set[tuple[int, int]]:
    """Every placed pin's world tip, memoized per apply (invalidated on place).

    route_net's corner rule consults this so an auto-picked L-corner never
    lands ON a pin — a coincident corner would silently merge that pin's net
    into the route (the trap docs teach by hand; the router avoids it by
    construction).
    """
    tips = ctx.pin_tips
    if tips is None:
        tips = set()
        for sym in _placed_symbols(doc):
            lib_id = _inst_lib_id(sym)
            if not lib_id:
                continue
            try:
                symdef = _ctx_symdef(doc, ctx, lib_id)
            except AkcliError:
                continue
            unit = _symbol_unit(sym)
            comp = _instance_component(sym, lib_id)
            for pin in kicad_lib.unit_pins(symdef, unit):
                tips.add(geometry.pin_world(symdef, comp, pin))
        ctx.pin_tips = tips
    return tips


def _route_points(
    a: tuple[int, int], b: tuple[int, int], style: str,
    tips: set[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Deterministic orthogonal route from ``a`` to ``b``.

    Coaxial endpoints -> one straight segment. Otherwise an L with corner
    ``hv`` = (b.x, a.y) (run horizontal first) or ``vh`` = (a.x, b.y);
    ``auto`` prefers ``hv``, swaps to ``vh`` when that corner sits on a pin
    tip, and falls back to a 3-segment ``z`` (longer axis split at its
    grid-snapped midpoint) when both corners are pins. Collision-aware
    routing is explicitly out of scope — geometry stays advisory; the
    connectivity gate and the NET_WIRE_CORNER_ON_PIN lint are the safety net.
    """
    if a[0] == b[0] or a[1] == b[1]:
        return [a, b]
    c_hv = (b[0], a[1])
    c_vh = (a[0], b[1])
    if style == "hv":
        return [a, c_hv, b]
    if style == "vh":
        return [a, c_vh, b]
    if style == "auto":
        if c_hv not in tips:
            return [a, c_hv, b]
        if c_vh not in tips:
            return [a, c_vh, b]
    # style "z", or auto with both L-corners on pin tips
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    if dx >= dy:
        mx = _snap_within_nm((a[0] + b[0]) // 2, a[0], b[0])
        return [a, (mx, a[1]), (mx, b[1]), b]
    my = _snap_within_nm((a[1] + b[1]) // 2, a[1], b[1])
    return [a, (a[0], my), (b[0], my), b]


@op("route_net")
def _op_route_net(call: OpCall) -> list[str]:
    """Orthogonal auto-route between two endpoints (+ optional mid-wire label).

    The non-coaxial companion of the ``wire_net``/``connect_and_label``
    pattern: resolves both endpoints (pin tips exact, points grid-snapped),
    synthesizes the L/Z vertices via :func:`_route_points`, and emits the
    segments through the shared wire path — same seeds, byte-identical
    re-apply. ``label`` names the net once at the longest segment's midpoint,
    snapped along the wire axis and clamped into the span.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    a = _resolve_endpoint(doc, ctx, op["from"])
    b = _resolve_endpoint(doc, ctx, op["to"])
    if a == b:
        fail("VERIFY_FAILED", "route_net: from and to resolve to the same point")
    pts = _route_points(a, b, op.get("style", "auto"), _pin_tip_set(doc, ctx))
    root = root_uuid(doc)
    created = _emit_wire_segments(doc, root, idx, pts, "wire", ctx)
    if op.get("label"):
        (ax, ay), (bx, by) = max(
            zip(pts, pts[1:]),
            key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
        if ay == by:   # horizontal segment: snap along X, read along the wire
            p = (_snap_within_nm((ax + bx) // 2, ax, bx), ay)
            orientation = 0
        else:
            p = (ax, _snap_within_nm((ay + by) // 2, ay, by))
            orientation = 90
        created.append(_emit_net_label(
            doc, root, idx, str(op["label"]), p, orientation,
            op.get("scope", "local"), ctx))
    return created


@op("add_junction")
def _op_add_junction(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    p = _point_nm(op["at"])
    root = root_uuid(doc)
    uid = identity.node_uuid(root, identity.seed_junction(p))
    _claim_uid(ctx, uid, idx, f"junction at {_mil_pair(p)}",
               "one junction per point; drop the duplicate op")
    node = _list(
        _atom("junction"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _list(_atom("diameter"), _atom("0")),
        _list(_atom("color"), _atom("0"), _atom("0"), _atom("0"), _atom("0")),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


@op("add_no_connect")
def _op_add_no_connect(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    ep = op["pin"]
    if isinstance(ep, str) and "." in ep:
        ref, pin_number = ep.rsplit(".", 1)
        p = _resolve_pin_world(doc, ctx, ref, pin_number)
    else:
        p = _point_nm(ep)
    root = root_uuid(doc)
    seed = _anchor_seed(ep, p)
    uid = identity.node_uuid(root, identity.seed_no_connect(seed))
    _claim_uid(ctx, uid, idx, f"no_connect at {seed}",
               "one no_connect per pin; drop the duplicate op")
    node = _list(
        _atom("no_connect"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


@op("add_bus_entry")
def _op_add_bus_entry(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    p = _point_nm(op["at"])
    if isinstance(op.get("size"), (list, tuple)) and len(op["size"]) == 2:
        size = (geometry.mil_to_nm(float(op["size"][0])), geometry.mil_to_nm(float(op["size"][1])))
    else:
        size = (_BUS_ENTRY_NM, _BUS_ENTRY_NM)
    root = root_uuid(doc)
    uid = identity.node_uuid(root, identity.seed_bus_entry(p))
    _claim_uid(ctx, uid, idx, f"bus_entry at {_mil_pair(p)}",
               "one bus entry per rip point; drop the duplicate op")
    node = _list(
        _atom("bus_entry"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _list(_atom("size"), _mm(size[0]), _mm(size[1])),
        _stroke(),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]
