"""Functional groups — envelope ``groups`` + a per-op ``group`` tag.

  "groups": {"POWER": {"origin": [1000, 1000], "title": "Power supply"}}
  {"op": "place_component", "group": "POWER", "x_mil": 400, "y_mil": 300}

A grouped op's coordinates are GROUP-LOCAL; :func:`resolve_groups` translates
them by the group's origin (absolute = local + origin). Moving a whole module
is a one-line origin edit. Resolution runs AFTER
:func:`akcli.ops.macros.expand_macros` (which propagates ``group`` onto every
expanded child op, so macro-internal offsets stay group-local) and BEFORE
:func:`akcli.ops.validate.validate_oplist`. Only ``[x, y]`` points translate —
``"REF.PIN"`` / ``"mid()"`` anchors are position-independent. The ``group``
tag stays on each op so the writer can persist membership as a hidden
``Group`` symbol property.
"""

from __future__ import annotations

from .. import opspec
from ..errors import AkcliError
from .base import _is_point

_GROUP_META_FIELDS = ("origin", "title", "frame")


class _ResolvedDoc(dict):
    """An op-list document whose group-local coordinates are already absolute.

    The "already resolved" bit used to be a ``_groups_resolved`` KEY inside the
    document — which meant an internal pipeline marker leaked into every
    consumer that echoed the prepared op-list (journal payloads, ``--json``
    draw results). Carrying it as the document's TYPE keeps the second-call
    guard (``group`` tags survive resolution, so a re-resolve would
    double-translate) with nothing added to the JSON: a dict subclass
    serializes exactly like the dict it copies.
    """

    __slots__ = ()

# Which coordinate slots are group-local is DERIVED FROM opspec — the ONE
# vocabulary source (``Field.coord``), so a new op's coordinates translate the
# moment they are declared instead of when someone remembers this file.
#
# Ops whose x_mil/y_mil pair is group-local (x/y may be absent on an
# anchor-relative placement — translate only what is present).
_GROUP_XY_OPS: frozenset[str] = frozenset(
    spec.name for spec in opspec.CORE
    if any(f.coord == "x" for f in spec.fields))
# Ops whose "at" translates when it is a point (add_no_connect's "pin" and
# add_rectangle's start/end are handled by name below — a single slot each).
_GROUP_AT_OPS: frozenset[str] = frozenset(
    spec.name for spec in opspec.CORE
    if any(f.name == "at" and f.coord == "point" for f in spec.fields))
_GROUP_VERTEX_OPS: frozenset[str] = frozenset(
    spec.name for spec in opspec.CORE
    if any(f.coord == "vertices" for f in spec.fields))


def _shift_point(v, ox, oy) -> list:
    return [v[0] + ox, v[1] + oy]


def _translate_op_coords(op: dict, ox, oy) -> dict:
    """A copy of ``op`` with its group-local ``[x, y]`` coordinates translated."""
    name = op.get("op")
    out = dict(op)
    if name in _GROUP_XY_OPS:
        for key in ("x_mil", "y_mil"):
            v = out.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[key] = v + (ox if key == "x_mil" else oy)
    elif name in _GROUP_AT_OPS:
        if _is_point(out.get("at")):
            out["at"] = _shift_point(out["at"], ox, oy)
    elif name == "add_no_connect":
        if _is_point(out.get("pin")):
            out["pin"] = _shift_point(out["pin"], ox, oy)
    elif name == "add_rectangle":
        for key in ("start", "end"):
            if _is_point(out.get(key)):
                out[key] = _shift_point(out[key], ox, oy)
    elif name == "route_net":
        for key in ("from", "to"):
            if _is_point(out.get(key)):
                out[key] = _shift_point(out[key], ox, oy)
    elif name in _GROUP_VERTEX_OPS and isinstance(out.get("vertices"), list):
        out["vertices"] = [
            _shift_point(v, ox, oy) if _is_point(v) else v
            for v in out["vertices"]
        ]
    return out


def _group_origin(groups: object, name: str, idx: int) -> tuple:
    if not isinstance(groups, dict) or name not in groups:
        raise AkcliError(
            "GROUP_UNKNOWN",
            f"[{idx}] op tags group {name!r} but the envelope 'groups' map has "
            f"no such entry")
    meta = groups[name]
    origin = meta.get("origin") if isinstance(meta, dict) else None
    if not _is_point(origin):
        raise AkcliError(
            "GROUP_NO_ORIGIN",
            f"group {name!r} needs an 'origin': [x_mil, y_mil]")
    return origin[0], origin[1]


def is_resolved(doc: object) -> bool:
    """True when ``doc`` is a document :func:`resolve_groups` translated."""
    return isinstance(doc, _ResolvedDoc)


def resolve_groups(doc: dict) -> dict:
    """Translate every grouped op's coordinates by its group origin.

    Returns a new document (the input is never mutated); a document with no
    grouped ops passes through untouched. Resolution is a pure, deterministic
    vector add, so op uuids stay stable for identical input. The returned
    document is a :class:`_ResolvedDoc`, which makes a second call a no-op —
    group tags remain on the ops (the writer persists them), so without that
    marker a re-resolve would double-translate. The marker is the TYPE, never
    a document key: nothing internal leaks into the JSON a caller echoes.

    Raises :class:`AkcliError` (``GROUP_UNKNOWN`` / ``GROUP_NO_ORIGIN``,
    exit 6) on a bad group reference, mirroring ``expand_macros``' contract.
    """
    if not isinstance(doc, dict) or isinstance(doc, _ResolvedDoc):
        return doc
    ops = doc.get("ops")
    if not isinstance(ops, list) or not any(
            isinstance(o, dict) and isinstance(o.get("group"), str)
            for o in ops):
        return doc
    groups = doc.get("groups")
    out: list = []
    for idx, op in enumerate(ops):
        if isinstance(op, dict) and isinstance(op.get("group"), str):
            ox, oy = _group_origin(groups, op["group"], idx)
            out.append(_translate_op_coords(op, ox, oy))
        else:
            out.append(op)
    new = _ResolvedDoc(doc)
    new["ops"] = out
    return new
