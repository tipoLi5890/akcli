"""Label / power-port / rename ops — everything that NAMES a net.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
``place_gnd`` / ``place_vcc`` are documented sugar over
``place_power_port`` and share its handler (the preset table is
:data:`_SUGAR_POWER`). :func:`_emit_net_label` is shared with
``route_net``'s inline ``label``.
"""

from __future__ import annotations

from .. import identity, instances
from ... import units
from ...errors import AkcliError
from ...geometry import transform as geometry
from ...sexpr import SNode
from ...sexpr.build import (
    _append_top_idempotent,
    _at,
    _atom,
    _effects,
    _label_justify,
    _list,
    _mil_pair,
    _q,
    _unescape_atom,
    _uuid_node,
)
from ..anchors import (
    _point_nm,
    _resolve_mid_anchor,
    _resolve_pin_inst,
    _resolve_pin_world,
    _pin_at_point,
)
from ..context import (
    ExecCtx,
    _claim_uid,
    _ctx_symdef,
    _inst_lib_id,
    _placed_symbols,
    _symbol_by_uuid,
    _symbol_reference,
    root_uuid,
)
from ..placement import _place_symbol
from ..properties import _autoplace_ref_value, _set_property
from ..registry import OpCall, _Note, op


# Sugar power-port presets (SPEC §2.2): documented sugar over place_power_port.
_SUGAR_POWER = {
    "place_gnd": ("power:GND", "GND"),
    "place_vcc": ("power:VCC", "VCC"),
}


@op("add_net_label")
def _op_add_net_label(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    at = op["at"]
    comp = pin = axis = None
    if isinstance(at, str) and at.startswith("mid("):
        # "mid(A.p,B.p)" anchor: grid-snapped midpoint of two axis-aligned pins.
        p, axis = _resolve_mid_anchor(doc, ctx, at, "add_net_label")
    elif isinstance(at, str) and "." in at:
        # "REF.PIN" anchor: exact pin world coordinate, never grid-snapped.
        ref, pin_number = at.rsplit(".", 1)
        comp, pin, p = _resolve_pin_inst(doc, ctx, ref, pin_number)
    else:
        p = _point_nm(at)
        if "rotation" not in op:
            hit = _pin_at_point(doc, ctx, p)
            if hit is not None:
                comp, pin = hit
    scope = op.get("scope", "local")
    if "rotation" in op:
        orientation = int(op["rotation"])
    elif pin is not None:
        # Label lands on a pin tip: orient the text away from the symbol body
        # so it never runs over the part it names.
        orientation = geometry.label_angle_away(pin.orientation, comp.rotation, comp.mirror)
    elif axis is not None:
        # Label sits mid-wire: read along the wire axis (never across it).
        orientation = 0 if axis == "x" else 90
    else:
        orientation = 0
    root = root_uuid(doc)
    return [_emit_net_label(doc, root, idx, str(op["name"]), p, orientation,
                            scope, ctx, _anchor_seed(at, p))]


def _anchor_seed(at: object, p: tuple[int, int]) -> str:
    """Seed fragment identifying WHERE an op anchored a node.

    A written anchor (``"R1.2"``, ``"mid(A.1,B.1)"``) seeds as itself, a raw
    point as its coordinates. Anchors, not resolved coordinates, because two
    pins can share one tip: the label-on-pin discipline puts one label on EACH
    of a coincident pair (that is how ``move_component --carry_labels`` knows
    which name travels with which part), and coordinate seeding collapsed those
    two nodes into one — the mover then carried the only label away and the
    staying pin silently lost its net. It also makes the node follow its part:
    re-applying after a move rewrites the SAME label at the pin's new place.
    """
    return str(at) if isinstance(at, str) else f"{p[0]}:{p[1]}"


def _emit_net_label(
    doc: SNode, root: str | None, idx: int, name: str,
    p: tuple[int, int], orientation: int, scope: str, ctx: ExecCtx | None = None,
    anchor_seed: str | None = None,
) -> str:
    """Emit one net-label node (shared by ``add_net_label`` and ``route_net``).

    Seeded by kind + net name + anchor, never the op index (see
    :func:`_claim_uid`): the same label re-emitted from an edited op-list must
    land on the SAME node instead of stacking a second copy on the first.
    """
    tag = {"local": "label", "global": "global_label",
           "hierarchical": "hierarchical_label"}[scope]
    seed = anchor_seed if anchor_seed is not None else f"{p[0]}:{p[1]}"
    uid = identity.node_uuid(root, identity.seed_label(tag, name, seed))
    _claim_uid(ctx, uid, idx, f"{tag} {name!r} at {seed}",
               "one label of a given name per anchor; drop the duplicate op")
    children = [_atom(tag), _atom(_q(name))]
    if tag != "label":
        children.append(_list(_atom("shape"), _atom("input")))
    children += [
        _at(p, orientation),
        _effects(justify=_label_justify(tag, orientation)),
        _uuid_node(uid),
    ]
    _append_top_idempotent(doc, _list(*children), uid)
    return uid


def _is_pwr_flag(lib_id: str, net_name: str) -> bool:
    """``power:PWR_FLAG`` (any nick), the power symbol that names NO net.

    netbuild applies exactly this carve-out (``readers/kicad.py``): a flag only
    marks a rail as driven for ERC, so — unlike a real power port — it may sit
    on the same coordinate as one without shorting anything. Identity and the
    stacking gates below are therefore per SPECIES.
    """
    return (lib_id.split(":")[-1].upper() == "PWR_FLAG"
            or net_name.upper() == "PWR_FLAG")


def _existing_pwr_ref_for_op(doc: SNode, pos_nm: tuple[int, int],
                             want_flag: bool) -> str | None:
    """Reference of a power symbol a prior replay placed HERE, else ``None``.

    A power port's ``#PWR0<n>`` reference is *auto-allocated* (it isn't carried
    in the op), so on replay :func:`instances.alloc_pwr_ref` would hand out the
    *next* free number — a different designator and therefore a different
    deterministic uuid, breaking idempotency.  Recover the original by the op's
    own identity: its **position** plus its species (port vs ``PWR_FLAG``, which
    legitimately coincides with a port), and nothing else (uuids are
    designator-seeded, so they cannot disambiguate power ports).

    The net name is deliberately NOT part of that identity. Including it (0.17)
    meant re-applying an op-list that renamed a port's net — ``GND`` -> ``+3V3``
    at the same coordinate — matched nothing, allocated a fresh ``#PWR0<n>``,
    and appended a SECOND symbol exactly on top of the first: the stale ``GND``
    stayed, invisible under the new port, shorting the two nets through their
    coincident pins. Position-only identity makes that op a replace-in-place.
    A power port MOVED to a new coordinate still leaves its old node behind
    (several ``GND`` ports on one sheet is legal, so nothing can tell a move
    from an addition) — delete the old one explicitly; see
    ``docs/op-list-authoring.md``.
    """
    sym = _existing_pwr_sym_for_op(doc, pos_nm, want_flag)
    return _symbol_reference(sym) if sym is not None else None


def _existing_pwr_sym_for_op(doc: SNode, pos_nm: tuple[int, int],
                             want_flag: bool) -> SNode | None:
    """The power symbol a prior replay placed at ``pos_nm``, else ``None``."""
    for sym in _placed_symbols(doc):
        ref = _symbol_reference(sym)
        if not ref or not ref.startswith("#"):
            continue
        at = sym.find("at")
        if at is None or len(at.children or []) < 3:
            continue
        try:
            sx = units.mm_to_nm(float(at.children[1].value))
            sy = units.mm_to_nm(float(at.children[2].value))
        except ValueError:
            continue
        if (sx, sy) != pos_nm:
            continue
        value = ""
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3 and _unescape_atom(kids[1].value) == "Value":
                value = _unescape_atom(kids[2].value)
        if _is_pwr_flag(_inst_lib_id(sym), value) is want_flag:
            return sym
    return None


@op("place_power_port", "place_gnd", "place_vcc")
def _op_place_power_port(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    name = op.get("op")
    if name in _SUGAR_POWER:
        default_lib, default_net = _SUGAR_POWER[name]
        lib_id = op.get("lib_id", default_lib)
        net_name = op.get("net_name", default_net)
    else:
        lib_id = op["lib_id"]
        net_name = op["net_name"]
    at = op["at"]
    if isinstance(at, str) and at.startswith("mid("):
        # "mid(A.p,B.p)" anchor: the port lands mid-wire (on-seg connects).
        pos, _axis = _resolve_mid_anchor(doc, ctx, at, name or "place_power_port")
    elif isinstance(at, str) and "." in at:
        # "REF.PIN" anchor: the port lands on the pin tip (connects with no wire).
        ref_ep, pin_number = at.rsplit(".", 1)
        pos = _resolve_pin_world(doc, ctx, ref_ep, pin_number)
    else:
        pos = _point_nm(at)
    rotation = int(op.get("rotation", 0))
    is_flag = _is_pwr_flag(str(lib_id), str(net_name))
    # One port (and one flag) per coordinate, per apply: a second op on the same
    # point would otherwise recover the first op's #PWR ref below and silently
    # REPLACE it — both ops reporting ok, one net vanishing. Identity is the
    # position, so the collision is position-based too: the net names are
    # irrelevant, two ports on one point short their nets whatever they name.
    claimed = ctx.power_ports
    prev = claimed.setdefault((pos, is_flag), (idx, str(net_name)))
    if prev != (idx, str(net_name)):
        raise AkcliError(
            "POWER_PORT_STACKED",
            f"op [{idx}] power port {net_name!r} at {_mil_pair(pos)} collides "
            f"with op [{prev[0]}] ({prev[1]!r} on the same coordinate; the "
            f"later op would replace the earlier port) — move one of them or "
            f"drop it")
    # Reuse the ref(s) from a prior replay of this op so the auto-allocated
    # #PWR designators (and thus the deterministic uuid) stay stable ->
    # idempotent. On a sheet instantiated N times the port needs N distinct
    # references (one per instance path) — allocated as a consecutive batch,
    # zero change to the op's interface.
    prior = _existing_pwr_sym_for_op(doc, pos, is_flag)
    inst_refs: dict | None = None
    if prior is not None:
        prior_refs = instances.symbol_references(prior)
        ref = _symbol_reference(prior) or instances.alloc_pwr_ref(doc)
        if len(prior_refs) > 1:
            inst_refs = dict(prior_refs)
    else:
        sheet_paths = instances.sibling_paths(doc)
        multi = (len(sheet_paths) > 1 and all(
            len([s for s in p.strip("/").split("/") if s]) >= 2
            for p in sheet_paths))
        if multi:
            batch = instances.alloc_pwr_refs(doc, len(sheet_paths))
            inst_refs = dict(zip(sheet_paths, batch))
            ref = batch[0]
        else:
            ref = instances.alloc_pwr_ref(doc)
    uid = _place_symbol(
        doc, ctx, list(src_libs), lib_id, ref,
        pos, rotation, "none", idx, path, instance_refs=inst_refs,
    )
    sym = _symbol_by_uuid(doc, uid)
    if sym is not None:
        _set_property(sym, "Value", str(net_name))
        if isinstance(op.get("group"), str):
            _set_property(sym, "Group", op["group"])
        _autoplace_ref_value(sym, _ctx_symdef(doc, ctx, lib_id), 1, pos, ctx)
    return [uid]


@op("rename_net")
def _op_rename_net(call: OpCall) -> list[str]:
    """Rename a net everywhere it is NAMED: label texts + power-port Values.

    ``scope`` restricts the rewrite to one label kind (local / global /
    hierarchical); without it every label kind AND power-port net Values
    (symbols with an auto ``#``-prefixed reference) are rewritten. Renaming a
    net nobody names is a replay-safe note, not an error; the match count is
    reported in the result message.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    frm, to = str(op["old_name"]), str(op["new_name"])
    scope = op.get("scope")
    tags = {"local": ("label",), "global": ("global_label",),
            "hierarchical": ("hierarchical_label",)}.get(
                scope, ("label", "global_label", "hierarchical_label"))
    count = 0
    for tag in tags:
        for node in doc.find_all(tag):
            kids = node.children or []
            if len(kids) >= 2 and kids[1].is_atom and kids[1].value == frm:
                kids[1] = _atom(_q(to))
                count += 1
    if scope is None:
        for sym in _placed_symbols(doc):
            ref = _symbol_reference(sym) or ""
            if not ref.startswith("#"):
                continue
            for prop in sym.find_all("property"):
                kids = prop.children or []
                if (len(kids) >= 3 and kids[1].value == "Value"
                        and kids[2].value == frm):
                    kids[2] = _atom(_q(to))
                    count += 1
    if count == 0:
        raise _Note(f"no label or power port named {frm!r} (nothing renamed)")
    raise _Note(f"renamed {count} object(s) from {frm!r} to {to!r}")
