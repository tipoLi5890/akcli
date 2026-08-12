"""Document-level op-list lints — problems no single op can see.

Shared by :mod:`akcli.ops.validate` (which reports them as ``OpError``s) and
:mod:`akcli.ops.macros` (which raises on the ones a macro expansion can
manufacture), so the two can never disagree about what "duplicate" means.
"""

from __future__ import annotations

from .base import _in, _is_point

# Annotation ops whose optional ``key`` seeds the node uuid (writer maps
# (tag, key) -> uuid, ignoring coords and op index) — validate_oplist rejects
# duplicate (tag, key) pairs so one op cannot silently replace another's node.
_ANNOTATION_KEY_TAGS: dict[str, str] = {
    "add_text": "text",
    "add_rectangle": "rectangle",
    "add_text_box": "text_box",
}


# Ops that drop a power symbol on a point/anchor. Two of them at the SAME spot
# is never intent: the bodies overlap pixel-for-pixel in eeschema (nothing to
# see, nothing to click apart) and the writer seeds both node uuids from the
# same coordinate, so which rail survives a re-apply is an implementation
# detail. Caught here, before any of that reaches the file.
_POWER_PORT_OPS: frozenset[str] = frozenset(
    {"place_power_port", "place_gnd", "place_vcc"})


def _anchor_key(at: object) -> object | None:
    """A hashable identity for an ``at`` anchor: point tuple, or the ref string."""
    if isinstance(at, str) and at:
        return at
    if _is_point(at):
        return ("pt", float(at[0]), float(at[1]))
    return None


def _is_pwr_flag_op(op: dict) -> bool:
    """``PWR_FLAG`` names no net, so it is a different species from a rail port.

    Mirrors ``writers.connectivity``'s carve-out (and netbuild's): a flag
    sharing a port's coordinate is the *prescribed* pattern — ``place_pwr_flag``
    expands to a ``place_power_port`` on ``power:PWR_FLAG``, so without this the
    stacking lint would refuse the very authoring shape the docs teach.
    """
    lib_id = str(op.get("lib_id") or "")
    return (lib_id.split(":")[-1].upper() == "PWR_FLAG"
            or str(op.get("net_name") or "").upper() == "PWR_FLAG")


def _duplicate_power_ports(ops_list: list) -> list[tuple[int, str, object, int]]:
    """``(op_index, op_name, anchor, first_index)`` per power port stacked on another.

    Keyed on ``(anchor, species)``: two rail ports on one point overlap and only
    one survives, and so do two flags — but a port and a flag together do not.
    """
    seen: dict[object, int] = {}
    dups: list[tuple[int, str, object, int]] = []
    for i, op in enumerate(ops_list):
        if not isinstance(op, dict) or not _in(op.get("op"), _POWER_PORT_OPS):
            continue
        anchor = _anchor_key(op.get("at"))
        if anchor is None:
            continue
        key = (anchor, _is_pwr_flag_op(op))
        if key in seen:
            dups.append((i, str(op.get("op")), op.get("at"), seen[key]))
        else:
            seen[key] = i
    return dups


def _duplicate_placements(ops_list: list) -> list[tuple[int, str, object, int]]:
    """``(op_index, designator, unit, first_index)`` per repeated placement.

    A ``delete_component`` releases its designator (all units), so a
    delete-then-replace sequence in one op-list is NOT a duplicate.
    """
    seen: dict[tuple, int] = {}
    dups: list[tuple[int, str, object, int]] = []
    for i, op in enumerate(ops_list):
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name == "delete_component" and isinstance(op.get("designator"), str):
            gone = op["designator"]
            seen = {k: v for k, v in seen.items() if k[0] != gone}
        elif name == "place_component":
            d, u = op.get("designator"), op.get("unit", 1)
            if isinstance(d, str) and isinstance(u, int) and not isinstance(u, bool):
                key = (d, u)
                if key in seen:
                    dups.append((i, d, u, seen[key]))
                else:
                    seen[key] = i
    return dups
