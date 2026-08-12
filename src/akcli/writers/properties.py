"""Symbol ``(property ...)`` writes + Reference/Value autoplacement.

``_set_property`` is find-or-create (new fields land hidden, the way
eeschema creates everything but Reference/Value); ``_autoplace_ref_value``
is the KiCad-autoplace-style heuristic that keeps a part's refdes and value
clear of its own body and of its neighbours' text (deterministic bump
through the per-apply anchor registry, so a replay is byte-stable).

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
"""

from __future__ import annotations

from .. import units
from ..geometry import transform as geometry
from ..readers import kicad_lib
from ..sexpr import SNode
from ..sexpr.build import (
    _at,
    _at_nm,
    _atom,
    _doc_child_indent,
    _effects,
    _list,
    _q,
)
from .context import ExecCtx, _inst_lib_id, _instance_component, _symbol_reference


def _set_property(sym: SNode, key: str, value: str) -> None:
    """Set/replace ``(property "<key>" "<value>" ...)`` on a placed symbol."""
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 2 and kids[1].value == key:
            val_atom = _atom(_q(value))
            if len(kids) >= 3:
                kids[2] = val_atom
            else:
                prop.children.insert(2, val_atom)   # type: ignore[union-attr]
                prop.ws.insert(2, " ")              # type: ignore[union-attr]
            return
    at = sym.find("at")
    pos = (_at_nm(at) if at is not None else (0, 0))
    # KiCad creates every field except Reference/Value hidden by default —
    # a NEW property node sits at the symbol anchor, so a visible custom
    # field (LCSC, MPN, ...) renders as raw text piled on the body.
    hide = key not in _VISIBLE_PROPERTIES
    prop = _list(
        _atom("property"),
        _atom(_q(key)),
        _atom(_q(value)),
        _at(pos, 0),
        _effects(hide=hide),
    )
    indent = _doc_child_indent(sym)
    k = len(sym.children or [])
    sym.children.append(prop)              # type: ignore[union-attr]
    sym.ws.insert(k, indent)               # type: ignore[union-attr]


# Properties eeschema creates hidden (raw text over the body otherwise).
_VISIBLE_PROPERTIES = frozenset({"Reference", "Value"})

# Gap between a symbol's pin bounding box and its Reference/Value text (nm).
_PROP_MARGIN_NM = units.mm_to_nm(1.27)


# Two text anchors collide when closer than roughly one label's extent.
_TEXT_CLEAR_X_NM = units.mm_to_nm(2.54)
_TEXT_CLEAR_Y_NM = units.mm_to_nm(1.27)


def _free_anchor(ctx: ExecCtx, pos: tuple[int, int],
                 step: tuple[int, int]) -> tuple[int, int]:
    """First anchor at/beyond ``pos`` (stepping by ``step``) clear of others.

    Registered VISIBLE text anchors within ~one label extent count as
    collisions; the bump direction follows the side the text was placed on, so
    the result stays deterministic and replay-stable.
    """
    anchors = ctx.text_anchors
    x, y = pos
    for _ in range(8):
        hit = any(
            abs(x - ax) < _TEXT_CLEAR_X_NM and abs(y - ay) < _TEXT_CLEAR_Y_NM
            for ax, ay in anchors
        )
        if not hit:
            break
        x += step[0]
        y += step[1]
    anchors.append((x, y))
    return (x, y)


def _body_box_world(
    symdef, unit: int, comp, origin: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """World (nm, +Y down) box of the symbol's drawn body, or ``None``."""
    ext = kicad_lib.body_extent_mil(symdef, unit)
    if ext is None:
        return None
    return geometry.world_box_from_extent(ext, comp.rotation, comp.mirror, origin)


def _autoplace_ref_value(sym: SNode, symdef, unit: int, ctxpos: tuple[int, int],
                         ctx: ExecCtx | None = None) -> None:
    """Position Reference/Value clear of the body (KiCad-autoplace style).

    The writer used to leave every property at the component origin (and the
    synthesized Reference at absolute 0,0), stacking raw text over the symbol
    body. Heuristic: from the placed unit's pin bounding box UNION its drawn
    body extent, put the text to the RIGHT of a tall part, or ABOVE/BELOW a
    wide one; power symbols hide the reference (any ``(power)`` symbol, not
    just ``#``-refs — a PWR_FLAG placed as ``FLG1`` must not print its ref)
    and show the value past the side the body extends to (+5V arrow: above;
    GND: below), exactly like eeschema. Property text angle compensates the
    instance rotation (KiCad renders properties at symbol+property angle), so
    a rotated resistor's "R3"/"470" still read horizontally. Neighboring
    parts' labels are avoided via the per-apply anchor registry
    (deterministic bump, replay-stable).
    """
    ctx = ctx if ctx is not None else {}
    lib_id = _inst_lib_id(sym)
    comp = _instance_component(sym, lib_id)
    pts = [geometry.pin_world(symdef, comp, p) for p in kicad_lib.unit_pins(symdef, unit)]
    ref = _symbol_reference(sym) or ""
    is_power = ref.startswith("#") or kicad_lib.is_power_symbol(symdef)
    # Counter-rotate so the text renders horizontal on a rotated instance
    # (mod 180: KiCad never draws text upside-down, and an angle of 180
    # WOULD render inverted rather than normalize).
    text_angle = (360 - comp.rotation) % 180

    if pts:
        min_x = min(p[0] for p in pts); max_x = max(p[0] for p in pts)
        min_y = min(p[1] for p in pts); max_y = max(p[1] for p in pts)
    else:
        min_x = max_x = ctxpos[0]
        min_y = max_y = ctxpos[1]
    body = _body_box_world(symdef, unit, comp, ctxpos)
    if body is not None:
        min_x = min(min_x, body[0]); min_y = min(min_y, body[1])
        max_x = max(max_x, body[2]); max_y = max(max_y, body[3])

    if is_power:
        # eeschema: hidden reference; value past the body's far side (a +5V
        # arrow extends up -> value above it; GND extends down -> below).
        _place_prop(sym, "Reference", (ctxpos[0], max_y + _PROP_MARGIN_NM),
                    hide=True, angle=text_angle)
        if (min_y + max_y) // 2 < ctxpos[1]:
            vpos = _free_anchor(ctx, (ctxpos[0], min_y - _PROP_MARGIN_NM),
                                (0, -_PROP_MARGIN_NM))
        else:
            vpos = _free_anchor(ctx, (ctxpos[0], max_y + _PROP_MARGIN_NM),
                                (0, _PROP_MARGIN_NM))
        _place_prop(sym, "Value", vpos, angle=text_angle)
        return
    tall = (max_y - min_y) >= (max_x - min_x)
    if tall:   # vertical body (R/C/L...): text to the right, left-justified
        x = max_x + _PROP_MARGIN_NM
        rpos = _free_anchor(ctx, (x, ctxpos[1] - _PROP_MARGIN_NM),
                            (_TEXT_CLEAR_X_NM, 0))
        vpos = _free_anchor(ctx, (rpos[0], ctxpos[1] + _PROP_MARGIN_NM),
                            (_TEXT_CLEAR_X_NM, 0))
        _place_prop(sym, "Reference", rpos, justify="left", angle=text_angle)
        _place_prop(sym, "Value", vpos, justify="left", angle=text_angle)
    else:      # wide body (ICs, connectors): text above/below the body
        rpos = _free_anchor(ctx, (ctxpos[0], min_y - _PROP_MARGIN_NM),
                            (0, -_PROP_MARGIN_NM))
        vpos = _free_anchor(ctx, (ctxpos[0], max_y + _PROP_MARGIN_NM),
                            (0, _PROP_MARGIN_NM))
        _place_prop(sym, "Reference", rpos, angle=text_angle)
        _place_prop(sym, "Value", vpos, angle=text_angle)


def _place_prop(
    sym: SNode, key: str, pos_nm: tuple[int, int],
    *, justify: str | None = None, hide: bool = False, angle: float = 0.0,
) -> None:
    """Set an existing property's ``(at ...)`` / ``(effects ...)`` in place."""
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 2 and kids[1].value == key:
            at = prop.find("at")
            new_at = _at(pos_nm, angle)
            if at is not None:
                kids[kids.index(at)] = new_at
            else:
                prop.children.append(new_at)          # type: ignore[union-attr]
                prop.ws.insert(len(prop.ws) - 1, " ")  # type: ignore[union-attr]
            eff = prop.find("effects")
            new_eff = _effects(hide=hide, justify=justify)
            if eff is not None:
                kids[kids.index(eff)] = new_eff
            else:
                prop.children.append(new_eff)          # type: ignore[union-attr]
                prop.ws.insert(len(prop.ws) - 1, " ")  # type: ignore[union-attr]
            return
