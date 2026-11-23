"""Component ops: place / transform / parameters / move / delete.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b); the handler
signatures became :class:`~..registry.OpCall` and nothing else changed.
"""

from __future__ import annotations

from .. import instances
from ... import units
from ...errors import AkcliError, fail
from ...geometry import transform as geometry
from ...readers import kicad_lib
from ...sexpr import SNode
from ...sexpr.build import _at, _at_nm, _atom, _list, _mm
from ..anchors import _GRID_NM, _anchored_pos_nm
from ..context import (
    _ctx_symdef,
    _inst_lib_id,
    _instance_component,
    _placed_symbols,
    _symbol_by_ref,
    _symbol_by_uuid,
    _symbol_reference,
    _symbol_unit,
    _symbols_by_ref,
)
from ..placement import _place_symbol
from ..properties import _autoplace_ref_value, _set_property
from ..registry import OpCall, _Note, op
from .delete import _delete_top_nodes, _node_uuid, _pt_on_seg_nm, _xy_points_nm


@op("place_component")
def _op_place_component(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    sources = list(src_libs)
    if op.get("symbol_source"):
        sources = [op["symbol_source"], *sources]
    rotation = int(op.get("rotation", 0))
    mirror = op.get("mirror", "none")
    unit = int(op.get("unit", 1))
    if isinstance(op.get("anchor"), str):
        pos = _anchored_pos_nm(doc, ctx, op, "place_component")
    else:
        pos = geometry.grid_snap_nm(
            (geometry.mil_to_nm(float(op["x_mil"])), geometry.mil_to_nm(float(op["y_mil"]))),
            _GRID_NM,
        )
    imap = op.get("instance_designators")
    if imap is not None:
        if not isinstance(imap, dict) or not imap:
            fail("INSTANCE_REFS_REQUIRED",
                 "instance_designators must be a non-empty object of "
                 "{\"<instance path>\": \"<REF>\"}")
        if op["designator"] not in imap.values():
            fail("INSTANCE_REFS_REQUIRED",
                 f"designator {op['designator']!r} must be one of the "
                 "instance_designators values (it seeds the symbol's uuid "
                 "and is the displayed Reference)")
    uid = _place_symbol(
        doc, ctx, sources, op["lib_id"], op["designator"],
        pos, rotation, mirror, idx, path, unit, instance_refs=imap,
    )
    sym = _symbol_by_uuid(doc, uid)
    if op.get("value") is not None and sym is not None:
        _set_property(sym, "Value", str(op["value"]))
    if op.get("footprint") is not None and sym is not None:
        _set_property(sym, "Footprint", str(op["footprint"]))
    if isinstance(op.get("group"), str) and sym is not None:
        # Functional-group membership persists in the sheet as a hidden
        # property, so `akcli groups` / `arrange --groups` can recover the
        # module map without the original op-list.
        _set_property(sym, "Group", op["group"])
    if sym is not None:
        _autoplace_ref_value(sym, _ctx_symdef(doc, ctx, op["lib_id"]), unit, pos, ctx)
    return [uid]


@op("set_component_transform")
def _op_set_component_transform(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    sym = _symbol_by_ref(doc, op["designator"])
    if sym is None:
        fail("VERIFY_FAILED", f"set_component_transform: no component {op['designator']!r}")
    at = sym.find("at")
    if "rotation" in op and at is not None and len(at.children or []) >= 4:
        at.children[3] = _atom(str(int(op["rotation"])))
    if "mirror" in op:
        mval = op["mirror"]
        existing = sym.find("mirror")
        if mval in ("x", "y"):
            node = _list(_atom("mirror"), _atom(mval))
            if existing is not None:
                sym.children[sym.children.index(existing)] = node
            else:
                # insert just after (at ...)
                ipos = sym.children.index(at) + 1 if at is not None else len(sym.children)
                sym.children.insert(ipos, node)      # type: ignore[union-attr]
                sym.ws.insert(ipos, " ")             # type: ignore[union-attr]
        elif existing is not None:                    # mirror "none" -> drop it
            j = sym.children.index(existing)
            del sym.children[j]
            del sym.ws[j]
    return []


@op("set_component_parameters")
def _op_set_component_parameters(call: OpCall) -> list[str]:
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    syms = _symbols_by_ref(doc, op["designator"])
    if not syms:
        fail("VERIFY_FAILED", f"set_component_parameters: no component {op['designator']!r}")
    imap = op.get("instance_designators")
    if imap is not None and (not isinstance(imap, dict) or not imap):
        fail("INSTANCE_REFS_REQUIRED",
             "instance_designators must be a non-empty object of "
             "{\"<instance path>\": \"<REF>\"}")
    # Every placed unit of a multi-unit part is its own symbol node, and
    # Reference / Value / Footprint / fields belong to the PART: writing only
    # the first unit left the others on the old value with nothing to flag it.
    for sym in syms:
        if imap is not None:
            instances.write_instance(doc, sym, imap, path)
        elif op.get("new_designator"):
            # A scalar rename of a multiply-instantiated symbol would smash its
            # per-path references into one duplicate refdes; write_instance
            # refuses that with INSTANCE_REFS_REQUIRED (use instance_designators).
            instances.write_instance(doc, sym, str(op["new_designator"]), path)
        if op.get("value") is not None:
            _set_property(sym, "Value", str(op["value"]))
        if op.get("footprint") is not None:
            _set_property(sym, "Footprint", str(op["footprint"]))
        for k, v in (op.get("parameters") or {}).items():
            _set_property(sym, str(k), str(v))
    return []


@op("delete_component")
def _op_delete_component(call: OpCall) -> list[str]:
    """Remove every placed instance of a designator (all units).

    Attached wires are intentionally left in place: the connectivity gate then
    reports their now-dangling endpoints, so stale wiring is cleaned up
    explicitly instead of silently. With ``"cascade": true`` the cleanup is
    done here: wires with an endpoint on any deleted pin's world coordinate,
    plus labels / no_connects anchored there, are deleted too; a junction
    anchored there is deleted only when fewer than two SURVIVING wires still
    pass through its point (a pure-X crossing of two untouched wires keeps
    its junction, else their shared net would silently split). The cascaded
    uuids are reported in the result message. Deleting an absent
    designator is a no-op (idempotent replay of a delta op-list must
    converge), reported in the result message.
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    ref = op["designator"]
    syms = _symbols_by_ref(doc, ref)
    cascade = bool(op.get("cascade", False))
    pin_pts: set[tuple[int, int]] = set()
    if cascade:
        for sym in syms:
            lib_id = _inst_lib_id(sym)
            symdef = _ctx_symdef(doc, ctx, lib_id)   # uncached lib_id fails loudly
            comp = _instance_component(sym, lib_id)
            for pin in kicad_lib.unit_pins(symdef, _symbol_unit(sym)):
                pin_pts.add(geometry.pin_world(symdef, comp, pin))
    targets = {id(s) for s in syms}
    removed = _delete_top_nodes(doc, lambda c: id(c) not in targets)
    if removed == 0:
        # replay-safe no-op; surfaced via the op result message (not an error)
        raise _Note(f"no placed instance of {ref!r} (already absent)")
    cascaded: list[str] = []
    if pin_pts:
        hit_ids: set[int] = set()
        junction_nodes: list[SNode] = []
        surviving_segs: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for c in doc.children or []:
            if not c.is_list or not (c.children or []):
                continue
            head = c.children[0].value
            if head in ("wire", "bus"):
                pts = _xy_points_nm(c)
                if any(p in pin_pts for p in pts):
                    hit_ids.add(id(c))
                    uid = _node_uuid(c)
                    if uid:
                        cascaded.append(uid)
                else:
                    surviving_segs.extend(zip(pts, pts[1:]))
            elif head == "junction":
                a = c.find("at")
                if a is not None and _at_nm(a) in pin_pts:
                    junction_nodes.append(c)
            elif head in ("label", "global_label", "hierarchical_label",
                          "no_connect"):
                a = c.find("at")
                if a is not None and _at_nm(a) in pin_pts:
                    hit_ids.add(id(c))
                    uid = _node_uuid(c)
                    if uid:
                        cascaded.append(uid)
        # A junction anchored on a deleted pin may STILL be doing real work:
        # two surviving wires crossing there (pure X) stay joined only through
        # it, and auto_junctions never re-adds pure-X joins — deleting it would
        # silently rewire nets the op never touched. Keep the junction unless
        # fewer than two surviving segments pass through its point.
        for c in junction_nodes:
            at = _at_nm(c.find("at"))
            touching = sum(1 for a, b in surviving_segs
                           if _pt_on_seg_nm(at, a, b))
            if touching < 2:
                hit_ids.add(id(c))
                uid = _node_uuid(c)
                if uid:
                    cascaded.append(uid)
        if hit_ids:
            _delete_top_nodes(doc, lambda c: id(c) not in hit_ids)
    if cascaded:
        raise _Note(f"cascade deleted {len(cascaded)} object(s): "
                    + ", ".join(cascaded))
    return []


def _instance_pin_points_nm(doc, ctx, sym) -> set[tuple[int, int]]:
    """World-coordinate (nm) pin tips of ONE placed instance, or ``set()``.

    Resolved from the document's ``(lib_symbols)`` cache. An unresolved lib_id
    yields the empty set rather than raising — a plain move must not fail just
    because a carry could not be computed; the connectivity gate then flags any
    anchor the move stranded, keeping the edit loud instead of silent.
    """
    lib_id = _inst_lib_id(sym)
    try:
        symdef = _ctx_symdef(doc, ctx, lib_id)
    except AkcliError:
        return set()
    comp = _instance_component(sym, lib_id)
    return {geometry.pin_world(symdef, comp, pin)
            for pin in kicad_lib.unit_pins(symdef, _symbol_unit(sym))}


def _translate_at(node: SNode, dx: int, dy: int) -> None:
    """Shift a node's ``(at x y [angle])`` by ``(dx, dy)`` nm, keeping the angle."""
    at = node.find("at")
    if at is None:
        return
    px, py = _at_nm(at)
    kids = node.children or []
    kids[kids.index(at)] = _at((px + dx, py + dy), _fnum_at(at, 3))


def _translate_wire_pins(wire: SNode, pins: set[tuple[int, int]],
                         dx: int, dy: int) -> bool:
    """Shift only the ``(xy)`` endpoints of ``wire`` that sit on a carried pin.

    An endpoint on a moved pin follows it (stays electrically glued); an
    endpoint on a stationary pin of another component is left in place, so the
    segment stretches to keep BOTH nets connected. Returns whether any endpoint
    moved.
    """
    pts = wire.find("pts")
    if pts is None:
        return False
    moved = False
    for xy in pts.find_all("xy"):
        kids = xy.children or []
        if len(kids) < 3:
            continue
        try:
            p = (units.mm_to_nm(float(kids[1].value)),
                 units.mm_to_nm(float(kids[2].value)))
        except (TypeError, ValueError):
            continue
        if p in pins:
            kids[1] = _mm(p[0] + dx)
            kids[2] = _mm(p[1] + dy)
            moved = True
    return moved


_CARRY_LABEL_TAGS = ("label", "global_label", "hierarchical_label")


@op("move_component")
def _op_move_component(call: OpCall) -> list[str]:
    """Move ONE placed instance (designator + optional unit) to x/y.

    Properties travel with the body (their ``(at)`` is absolute, so the same
    delta is applied). By default wires do NOT stretch — the connectivity gate
    flags any endpoint the move disconnected, keeping the edit loud instead of
    silently leaving wires behind.

    ``carry_labels`` / ``carry_wires`` promote the move to a rigid-body
    relocation: any net label (``carry_labels``) or wire endpoint
    (``carry_wires``) anchored on one of this instance's pin tips is shifted by
    the SAME delta. With the label-on-pin connectivity pattern (no cross-part
    wires) ``carry_labels`` makes the move provably net-preserving — every pin
    keeps the label that names its net. This is the atomic operation a group
    re-layout is built from (``akcli arrange --groups``).
    """
    doc, op, idx, src_libs, path, ctx = (  # noqa: F841 - uniform handler prelude
        call.doc, call.op, call.index, call.src_libs, call.inst_path, call.ctx)
    ref = op["designator"]
    unit = int(op.get("unit", 1))
    carry_labels = bool(op.get("carry_labels", False))
    carry_wires = bool(op.get("carry_wires", False))
    sym = next((s for s in _symbols_by_ref(doc, ref) if _symbol_unit(s) == unit), None)
    if sym is None:
        fail("VERIFY_FAILED", f"move_component: no placed instance of {ref!r} unit {unit}")
    if isinstance(op.get("anchor"), str):
        new = _anchored_pos_nm(doc, ctx, op, "move_component")
    else:
        new = geometry.grid_snap_nm(
            (geometry.mil_to_nm(float(op["x_mil"])), geometry.mil_to_nm(float(op["y_mil"]))),
            _GRID_NM,
        )
    at = sym.find("at")
    old = _at_nm(at) if at is not None else (0, 0)
    rot = _fnum_at(at, 3)

    # Pin tips must be sampled BEFORE the body moves — a carried anchor is one
    # sitting on a pre-move pin coordinate.
    pins = (_instance_pin_points_nm(doc, ctx, sym)
            if (carry_labels or carry_wires) else set())

    kids = sym.children or []
    kids[kids.index(at)] = _at(new, rot)
    dx, dy = new[0] - old[0], new[1] - old[1]
    for prop in sym.find_all("property"):
        pat = prop.find("at")
        if pat is None:
            continue
        px, py = _at_nm(pat)
        pkids = prop.children or []
        pkids[pkids.index(pat)] = _at((px + dx, py + dy), _fnum_at(pat, 3))

    carried_labels = carried_wires = 0
    if pins and (dx or dy):
        # Pin-tip COINCIDENCE disambiguation: when another instance's pin sits
        # on one of the mover's tips, a label there names BOTH pins — the
        # mover must leave one label behind per foreign pin (else it carries
        # the stayer's name away and the stayer's net silently splits). With
        # the label-on-pin discipline (one label per member pin) every pin
        # keeps exactly one name through any move order.
        leave_at: dict = {}
        foreign_syms = [s for s in _placed_symbols(doc)
                        if _symbol_reference(s) != ref]
        for fsym in foreign_syms:
            for tip in _instance_pin_points_nm(doc, ctx, fsym):
                if tip in pins:
                    leave_at[tip] = leave_at.get(tip, 0) + 1
        for c in doc.children or []:
            if not c.is_list or not (c.children or []):
                continue
            head = c.children[0].value
            if carry_labels and head in _CARRY_LABEL_TAGS:
                a = c.find("at")
                if a is not None and _at_nm(a) in pins:
                    pos = _at_nm(a)
                    if leave_at.get(pos, 0) > 0:
                        leave_at[pos] -= 1       # the stayer keeps this one
                        continue
                    _translate_at(c, dx, dy)
                    carried_labels += 1
            elif carry_wires and head in ("wire", "bus"):
                if _translate_wire_pins(c, pins, dx, dy):
                    carried_wires += 1
    if carry_labels or carry_wires:
        raise _Note(
            f"moved {ref!r}; carried {carried_labels} label(s), "
            f"{carried_wires} wire(s)")
    return []


def _fnum_at(at: SNode | None, idx: int) -> float:
    if at is not None and at.children and idx < len(at.children or []):
        try:
            return float(at.children[idx].value or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0
