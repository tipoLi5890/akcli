"""Anchor / endpoint resolution: op coordinates -> integer-nm world points.

Every op that names a place — a wire endpoint, a label's ``at``, a relative
``anchor`` + ``offset_mil``, a ``mid(A.p,B.p)`` midpoint — funnels through
here. ``"REF.PIN"`` resolves to the pin's EXACT world tip (never
grid-snapped, so the wire lands on the electrical point); a raw ``[x, y]``
mil pair is converted to nm and snapped to the 50-mil grid.

Resolution always runs against the LIVE document at execution time (the
anchor may have been placed by an earlier op in the same op-list); a second
geometry engine in ``ops.py`` would inevitably diverge.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
"""

from __future__ import annotations

import difflib

from .. import model, units
from ..errors import AkcliError, fail
from ..geometry import transform as geometry
from ..ops import parse_mid_anchor
from ..readers import kicad_lib
from ..sexpr import SNode
from ..sexpr.build import _at_nm  # noqa: F401 - re-exported for the facade
from .context import (
    ExecCtx,
    _ctx_symdef,
    _inst_lib_id,
    _instance_component,
    _placed_symbols,
    _symbol_by_ref,
    _symbol_unit,
    _symbols_by_ref,
)

# Default schematic grid (50 mil) in integer nm — raw [x,y] endpoints snap here.
_GRID_NM = geometry.DEFAULT_GRID_NM


def _resolve_pin_inst(
    doc: SNode, ctx: ExecCtx, ref: str, pin_number: str,
) -> tuple[model.Component, model.Pin, tuple[int, int]]:
    """``(instance, pin, world_nm)`` of ``ref.pin_number`` in the current document.

    A multi-unit part is several placed instances sharing ``ref``; the pin is
    looked up on the instance whose UNIT owns it (eeschema exposes only that
    unit's pins there). A pin living on an unplaced unit fails loudly instead
    of silently snapping to another unit's body — that divergence merged all
    four 74xx gates onto one instance.
    """
    syms = _symbols_by_ref(doc, ref)
    if not syms:
        fail("VERIFY_FAILED", f"pin reference {ref!r} matches no placed component")
    symdef = None
    available: list[str] = []
    for sym in syms:
        lib_id = _inst_lib_id(sym)
        try:
            symdef = _ctx_symdef(doc, ctx, lib_id)
        except AkcliError:
            fail("SYMBOL_NOT_FOUND", f"pin reference {ref!r}: lib_id {lib_id!r} not in cache")
        unit = _symbol_unit(sym)
        comp = _instance_component(sym, lib_id)
        for pin in kicad_lib.unit_pins(symdef, unit):
            if pin.number == pin_number:
                return comp, pin, geometry.pin_world(symdef, comp, pin)
            available.append(pin.number)
    if symdef is not None:
        owners = sorted({p.owner_part_id for p in symdef.pins if p.number == pin_number})
        if owners:
            fail(
                "VERIFY_FAILED",
                f"pin {ref}.{pin_number} is on unit {owners[0]} which is not placed; "
                f'place it first (place_component with "unit": {owners[0]})',
            )
    hint = ""
    if available:
        close = difflib.get_close_matches(pin_number, available, n=1)
        did_you_mean = f" — did you mean {close[0]!r}?" if close else ""
        hint = f"{did_you_mean} (valid pins: {', '.join(sorted(set(available)))})"
    fail("VERIFY_FAILED", f"component {ref!r} has no pin {pin_number!r}{hint}")


def _resolve_pin_world(doc: SNode, ctx: ExecCtx, ref: str, pin_number: str) -> tuple[int, int]:
    """World coordinate (nm) of ``ref.pin_number`` (see :func:`_resolve_pin_inst`)."""
    return _resolve_pin_inst(doc, ctx, ref, pin_number)[2]


def _pin_at_point(
    doc: SNode, ctx: ExecCtx, p: tuple[int, int],
) -> tuple[model.Component, model.Pin] | None:
    """The placed ``(instance, pin)`` whose electrical tip sits exactly at ``p``.

    Reverse lookup for label auto-orientation: a label anchored on a raw
    coordinate that happens to be a pin tip should still orient away from that
    pin's body. Returns the first match (coincident pins of two symbols share
    the point anyway; either orientation choice is between the same bodies).
    """
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
            if geometry.pin_world(symdef, comp, pin) == p:
                return comp, pin
    return None


def _resolve_anchor_origin(
    doc: SNode, ctx: ExecCtx, anchor: str, opname: str,
) -> tuple[int, int]:
    """World coordinate (nm) of a relative-placement anchor.

    ``"REF.PIN"`` resolves to that pin's electrical tip (the same machinery
    wires use); a bare ``"REF"`` resolves to the component's ``(at)`` origin.
    The anchor may have been placed earlier in the SAME op-list — resolution
    happens at execution time against the live document, never as an ops.py
    pre-pass (a second geometry engine would inevitably diverge).
    """
    if "." in anchor:
        ref, pin_number = anchor.rsplit(".", 1)
        return _resolve_pin_world(doc, ctx, ref, pin_number)
    sym = _symbol_by_ref(doc, anchor)
    if sym is None:
        fail("VERIFY_FAILED",
             f"{opname}: anchor {anchor!r}: no placed component {anchor!r}")
    at = sym.find("at")
    return _at_nm(at) if at is not None else (0, 0)


def _anchored_pos_nm(doc: SNode, ctx: ExecCtx, op: dict, opname: str) -> tuple[int, int]:
    """Grid-snapped world position for an anchor (+world-frame offset_mil) op."""
    base = _resolve_anchor_origin(doc, ctx, str(op["anchor"]), opname)
    off = op.get("offset_mil") or [0, 0]
    return geometry.grid_snap_nm(
        (base[0] + geometry.mil_to_nm(float(off[0])),
         base[1] + geometry.mil_to_nm(float(off[1]))),
        _GRID_NM,
    )


def _resolve_endpoint(doc: SNode, ctx: ExecCtx, ep: object) -> tuple[int, int]:
    """Resolve a wire/port endpoint to integer-nm coordinates.

    ``"REF.PIN"`` snaps to the pin's world coordinate (exact, never grid-snapped);
    a raw ``[x, y]`` mil point is converted to nm and snapped to the 50-mil grid.
    """
    if isinstance(ep, str):
        ref, pin_number = ep.rsplit(".", 1)
        return _resolve_pin_world(doc, ctx, ref, pin_number)
    if isinstance(ep, (list, tuple)) and len(ep) == 2:
        nm = (geometry.mil_to_nm(float(ep[0])), geometry.mil_to_nm(float(ep[1])))
        return geometry.grid_snap_nm(nm, _GRID_NM)
    fail("NON_ORTHOGONAL_WIRE", f"malformed endpoint {ep!r}")


def _point_nm(at: object) -> tuple[int, int]:
    """Convert a raw ``[x, y]`` mil point to grid-snapped nm."""
    if isinstance(at, (list, tuple)) and len(at) == 2:
        nm = (geometry.mil_to_nm(float(at[0])), geometry.mil_to_nm(float(at[1])))
        return geometry.grid_snap_nm(nm, _GRID_NM)
    fail("OFF_GRID", f"malformed point {at!r}")


# mid() anchors: the two pins must be axis-aligned within half a grid step.
def _snap_within_nm(v: int, a: int, b: int) -> int:
    """Snap ``v`` to the 50-mil grid, clamped into ``[min(a,b), max(a,b)]``.

    Clamping keeps the anchor ON the wire between off-grid pins — a snapped
    midpoint outside the span would leave the label/flag floating.
    """
    s = int(round(v / _GRID_NM)) * _GRID_NM
    lo, hi = min(a, b), max(a, b)
    return min(max(s, lo), hi)


def _resolve_mid_anchor(
    doc: SNode, ctx: ExecCtx, at: str, opname: str,
) -> tuple[tuple[int, int], str]:
    """Resolve ``"mid(A.p,B.p)"`` to ``(point_nm, wire_axis)``.

    Both pins must be EXACTLY axis-aligned (equal integer-nm cross-axis
    coordinate); the midpoint is snapped to the 50-mil grid ALONG the wire
    axis (``"x"`` horizontal / ``"y"`` vertical) while the cross-axis
    coordinate keeps the pins' shared value — so the anchor always lands on
    the straight wire drawn between the two pins. A tolerance here would be
    a lie: a snapped anchor between misaligned pins leaves the (slightly
    diagonal) wire, and netbuild's exact-integer on-segment test would let
    the label/flag silently attach to nothing.
    """
    parsed = parse_mid_anchor(at)
    if parsed is None:
        fail("OP_UNSUPPORTED",
             f'{opname}: malformed mid() anchor {at!r}; '
             f'expected "mid(REF.PIN,REF.PIN)"')
    a, b = parsed
    pa = _resolve_pin_world(doc, ctx, *a.rsplit(".", 1))
    pb = _resolve_pin_world(doc, ctx, *b.rsplit(".", 1))
    dx, dy = abs(pa[0] - pb[0]), abs(pa[1] - pb[1])
    if min(dx, dy) != 0:
        fail("NON_ORTHOGONAL_WIRE",
             f"{opname}: mid() pins are not axis-aligned: "
             f"{a} at ({units.nm_to_mil(pa[0]):g},{units.nm_to_mil(pa[1]):g}) mil, "
             f"{b} at ({units.nm_to_mil(pb[0]):g},{units.nm_to_mil(pb[1]):g}) mil")
    if dx >= dy:   # wire runs along X
        return (_snap_within_nm((pa[0] + pb[0]) // 2, pa[0], pb[0]),
                (pa[1] + pb[1]) // 2), "x"
    return ((pa[0] + pb[0]) // 2,
            _snap_within_nm((pa[1] + pb[1]) // 2, pa[1], pb[1])), "y"
