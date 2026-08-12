"""Placed ``(symbol ...)`` construction — cache, seed, append, instance.

``_place_symbol`` is the one path every placement takes (``place_component``
and ``place_power_port`` alike): flatten the library symbol into
``(lib_symbols)``, seed the instance + per-pin uuids from the DESIGNATOR
(never the op index, so an op-list edit keeps every untouched part's
identity), append idempotently, and write the ``(instances)`` reference.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
"""

from __future__ import annotations

from ..errors import fail
from ..kicad_escape import escape_lib_id
from ..readers import kicad_lib
from ..sexpr import SNode
from ..sexpr.build import (
    _append_top_idempotent,
    _at,
    _atom,
    _list,
    _q,
    _uuid_node,
)
from . import identity, instances, lib_cache
from .context import ExecCtx, _symdef_from_body, root_uuid


def _make_symbol(
    lib_id: str, pos_nm: tuple[int, int], rotation: int, mirror: str,
    uuid_str: str, pin_numbers: list[str], pin_uuids: list[str], unit: int = 1,
) -> SNode:
    """Build a placed ``(symbol ...)`` node with per-pin ``(pin "N" (uuid))``."""
    children = [
        _list(_atom("lib_id"), _atom(_q(escape_lib_id(lib_id)))),
        _at(pos_nm, rotation),
        _list(_atom("unit"), _atom(str(int(unit)))),
    ]
    if mirror in ("x", "y"):
        children.insert(2, _list(_atom("mirror"), _atom(mirror)))
    children.append(_uuid_node(uuid_str))
    for num, puid in zip(pin_numbers, pin_uuids):
        children.append(_list(_atom("pin"), _atom(_q(num)), _uuid_node(puid)))
    return _list(_atom("symbol"), *children)


def _place_symbol(
    doc: SNode, ctx: ExecCtx, src_libs: list, lib_id: str, designator: str,
    pos_nm: tuple[int, int], rotation: int, mirror: str, op_index: int, path: str,
    unit: int = 1, instance_refs: dict | None = None,
) -> str:
    """Cache + place ONE UNIT of a symbol; return its (deterministic) uuid."""
    body = lib_cache.ensure_cached(doc, lib_id, src_libs)
    symdef = ctx.symdefs.get(lib_id)
    if symdef is None:
        # Resolve from just this one cached body — never the whole cache (perf).
        symdef = _symdef_from_body(body, lib_id)
        ctx.symdefs[lib_id] = symdef
    if unit < 1 or unit > max(1, symdef.part_count):
        fail("VERIFY_FAILED",
             f"{lib_id!r} has {symdef.part_count} unit(s); cannot place unit {unit}")
    # The instance carries only ITS unit's pins (eeschema draws and connects
    # only those); emitting every unit's pins mapped all gates onto one body
    # and let phantom pin points mask real dangles in the verifier.
    pin_numbers = [p.number for p in kicad_lib.unit_pins(symdef, unit)]

    root = root_uuid(doc)
    # Symbol/pin uuids are seeded from the DESIGNATOR alone (op_index pinned
    # to 0): editing an op-list — inserting or reordering ops — must not
    # change the identity of untouched parts, or a re-apply duplicates the
    # whole design downstream of the edit. (Unit >1 carries a #u suffix.)
    sym_uuid = identity.node_uuid(root, identity.seed_symbol(designator, unit))
    # A pin NUMBER may legitimately repeat within one symbol (multi-unit parts
    # with shared pads, e.g. dual DirectFETs: unit A pins 1,2,3 / unit B pins
    # 1,4,5). Seed later occurrences with a #k suffix so their uuids stay
    # unique — otherwise the two "(pin "1" ...)" nodes collide and the
    # connectivity gate refuses the write (DUPLICATE_UUID).
    pin_uuids = []
    _seen: dict[str, int] = {}
    for n in pin_numbers:
        k = _seen.get(n, 0)
        _seen[n] = k + 1
        pin_uuids.append(identity.node_uuid(
            root, identity.seed_symbol_pin(designator, unit, n, k)))

    # Idempotent replay: a same-uuid instance already present is replaced wholesale.
    sym = _make_symbol(lib_id, pos_nm, rotation, mirror, sym_uuid, pin_numbers, pin_uuids, unit)
    _append_top_idempotent(doc, sym, sym_uuid)
    # instance_refs (a {path: ref} map) takes over on a multiply-instantiated
    # sheet — the uuid seed above stays the SCALAR designator either way, so
    # per-instance references never re-identify the node.
    instances.write_instance(doc, sym, instance_refs or designator, path)
    # New pins invalidate the route_net corner-avoidance memo (ops run in
    # order; a later route must see THIS part's pins).
    ctx.pin_tips = None
    return sym_uuid
