"""Faithful symbol artwork for `akcli render` (KiCad sources).

Walks each placed component's library symbol graphics — the raw
``(symbol ...)`` S-expression preserved on ``SymbolDef.body_sexpr`` —
and emits SVG fragments in world coordinates: rectangles, circles, arcs,
polylines, beziers, graphic text, pin stubs and (when the symbol does not
hide them) pin numbers/names. Every point goes through
:func:`akcli.writers.geometry.transform_point` — the same Y-flip +
rotate-then-mirror chain the net engine uses for pins — so artwork can never
disagree with connectivity.

Honest limits (fallback = the synthesized pin-box body):

* multi-unit parts (``part_count > 1``): the normalized model keeps ONE
  component per designator and only the first instance's placement, so the
  extra units' bodies cannot be positioned — synthesized body instead;
* symbols missing from the sheet's embedded ``lib_symbols`` (or Altium
  sources, which carry no KiCad artwork);
* pin name/number text is drawn horizontally at an approximate anchor
  (reviewable, not a font-metric reproduction).

Deterministic: same input bytes → same fragments (stable walk order, fixed
float formatting).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from xml.sax.saxutils import escape

from . import units
from .errors import AkcliError
from .kicad_escape import unescape_string
from .model import Component, Library, SymbolDef
from .readers import kicad_lib
from .readers.kicad_lib import _atom_value, _fnum_atom, _mm_to_mil, _sub_unit_style
from .sexpr import SNode
from .geometry import transform as geometry

_PIN_NUM_NUDGE_MIL = 25.0    # pin number: offset from the stub midpoint
_PIN_NAME_GAP_MIL = 30.0     # pin name: past the stub's inner (body) end
_PIN_NAME_OFFSET_MM = 0.508  # KiCad default when (pin_names) carries no (offset)

_Fmt = Callable[[float], str]


def _atom(node: SNode | None, idx: int) -> str | None:
    """None-safe :func:`_atom_value` — ``SNode.find`` returns ``None`` freely.

    Optional sub-nodes are the norm, not the exception, in real symbol
    libraries: ``(pin_names hide)`` carries no ``(offset)``, a pin may have no
    ``(name)``/``(number)``/``(length)``, ``(fill)`` no ``(type)``. Handing a
    raw ``find`` result to ``_atom_value`` (which dereferences
    ``node.children``) is therefore an ``AttributeError`` waiting to happen —
    it crashed ``akcli render`` on 59% of the standard KiCad libraries. Numbers
    go through :func:`_fnum_atom`, which is None-safe the same way.
    """
    return _atom_value(node, idx) if node is not None else None


def _fill_class(g: SNode) -> str:
    fill = g.find("fill")
    kind = _atom(fill.find("type"), 1) if fill is not None else None
    return "sym sym-fill" if kind == "background" else "sym"


def _hide_flag(node: SNode | None) -> bool:
    """True for ``(x ... hide)`` / ``(x ... (hide yes))`` (KiCad 6–9 forms)."""
    if node is None:
        return False
    for c in node.children or []:
        if c.is_list and c.tag == "hide":
            return (_atom(c, 1) or "yes") == "yes"
        if c.is_atom and c.text == "hide":
            return True
    return False


class SymbolArt:
    """Per-render artwork provider over a sheet's embedded symbol library."""

    def __init__(self, library: Library):
        self._lib = library
        self._cache: dict[str, SymbolDef | None] = {}

    # ------------------------------------------------------------------ #
    # symbol lookup
    # ------------------------------------------------------------------ #
    def _symdef(self, comp: Component) -> SymbolDef | None:
        lib_id = comp.library_ref or ""
        if lib_id not in self._cache:
            try:
                self._cache[lib_id] = kicad_lib.resolve(lib_id, [self._lib])
            except AkcliError:
                self._cache[lib_id] = None
        return self._cache[lib_id]

    def _bodies(self, symdef: SymbolDef) -> list[SNode]:
        """Body roots to draw: the base symbol first for an (extends) part."""
        # `SymbolDef.body_sexpr` is typed `object | None` (model.py cannot
        # import the reader) — narrow it here instead of trusting it.
        out: list[SNode] = []
        if symdef.extends:
            try:
                base = kicad_lib.resolve(symdef.extends, [self._lib])
                if isinstance(base.body_sexpr, SNode):
                    out.append(base.body_sexpr)
            except AkcliError:
                pass
        if isinstance(symdef.body_sexpr, SNode):
            out.append(symdef.body_sexpr)
        return out

    def _drawable(self, comp: Component) -> SymbolDef | None:
        symdef = self._symdef(comp)
        if symdef is None or symdef.part_count > 1:
            return None                  # multi-unit: placement not modeled
        return symdef if self._bodies(symdef) else None

    # ------------------------------------------------------------------ #
    # geometry
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pt(comp: Component, lx_mil: float, ly_mil: float) -> tuple[float, float]:
        """Lib-frame (mil, +Y up) point → world mils (+Y down)."""
        local = (units.mil_to_nm(lx_mil), -units.mil_to_nm(ly_mil))
        origin = (units.mil_to_nm(comp.x_mil), units.mil_to_nm(comp.y_mil))
        wx, wy = geometry.transform_point(local, comp.rotation, comp.mirror,
                                          origin)
        return units.nm_to_mil(wx), units.nm_to_mil(wy)

    def world_bbox(
        self, comp: Component,
    ) -> tuple[float, float, float, float] | None:
        """World-mil AABB of the drawn body (no pins), or ``None``."""
        symdef = self._drawable(comp)
        if symdef is None:
            return None
        exts = [e for e in (kicad_lib.body_extent_mil(b_def, unit=1)
                            for b_def in self._extent_defs(symdef))
                if e is not None]
        if not exts:
            return None
        ext = (min(e[0] for e in exts), min(e[1] for e in exts),
               max(e[2] for e in exts), max(e[3] for e in exts))
        origin = (units.mil_to_nm(comp.x_mil), units.mil_to_nm(comp.y_mil))
        box = geometry.world_box_from_extent(ext, comp.rotation, comp.mirror,
                                             origin)
        return (units.nm_to_mil(box[0]), units.nm_to_mil(box[1]),
                units.nm_to_mil(box[2]), units.nm_to_mil(box[3]))

    def _extent_defs(self, symdef: SymbolDef) -> list[SymbolDef]:
        """The symdefs whose bodies contribute drawn extent (base + derived)."""
        out = [symdef]
        if symdef.extends:
            try:
                out.insert(0, kicad_lib.resolve(symdef.extends, [self._lib]))
            except AkcliError:
                pass
        return out

    # ------------------------------------------------------------------ #
    # SVG emission
    # ------------------------------------------------------------------ #
    def emit(self, comp: Component, x: _Fmt, y: _Fmt,
             fmt: _Fmt) -> list[str] | None:
        """SVG fragments for the component's faithful body, or ``None``.

        ``x``/``y`` map world-mil coordinates to offset page strings (the
        renderer's per-sheet formatters); ``fmt`` formats scalar lengths.
        """
        symdef = self._drawable(comp)
        if symdef is None:
            return None
        power = kicad_lib.is_power_symbol(symdef)
        out: list[str] = []
        for body in self._bodies(symdef):
            hide_numbers = _hide_flag(body.find("pin_numbers"))
            names_node = body.find("pin_names")
            hide_names = _hide_flag(names_node)
            # `(pin_names hide)` — the majority form in the standard libraries
            # — has no `(offset)` child at all: `find` returns None.
            offset_node = (names_node.find("offset")
                           if names_node is not None else None)
            name_offset_mil = _mm_to_mil(
                _fnum_atom(offset_node, 1, _PIN_NAME_OFFSET_MM))
            for holder in self._holders(body):
                for g in holder.children or []:
                    if not g.is_list:
                        continue
                    self._emit_graphic(comp, g, x, y, fmt, out,
                                       hide_numbers=hide_numbers or power,
                                       hide_names=hide_names or power,
                                       name_offset_mil=name_offset_mil)
        return out or None

    @staticmethod
    def _holders(body: SNode) -> list[SNode]:
        """Body + the unit-1/common sub-symbols, style ≤ 1 (no DeMorgan)."""
        holders = [body]
        for sub in body.find_all("symbol"):
            u, style = _sub_unit_style(_atom(sub, 1))
            if style is not None and style >= 2:
                continue
            if u is None or u in (0, 1):
                holders.append(sub)
        return holders

    def _emit_graphic(self, comp: Component, g: SNode, x: _Fmt, y: _Fmt,
                      fmt: _Fmt, out: list[str], *, hide_numbers: bool,
                      hide_names: bool, name_offset_mil: float) -> None:
        pt = self._pt

        def take(node: SNode | None) -> tuple[float, float] | None:
            if node is None:
                return None
            return pt(comp, _mm_to_mil(_fnum_atom(node, 1)),
                      _mm_to_mil(_fnum_atom(node, 2)))

        if g.tag == "rectangle":
            a, b = take(g.find("start")), take(g.find("end"))
            if a and b:
                x0, x1 = min(a[0], b[0]), max(a[0], b[0])
                y0, y1 = min(a[1], b[1]), max(a[1], b[1])
                out.append(f'<rect class="{_fill_class(g)}" x="{x(x0)}" '
                           f'y="{y(y0)}" width="{fmt(x1 - x0)}" '
                           f'height="{fmt(y1 - y0)}"/>')
        elif g.tag == "circle":
            c = take(g.find("center"))
            if c is not None:
                r = _mm_to_mil(_fnum_atom(g.find("radius"), 1))
                out.append(f'<circle class="{_fill_class(g)}" cx="{x(c[0])}" '
                           f'cy="{y(c[1])}" r="{fmt(r)}"/>')
        elif g.tag == "arc":
            s, m, e = (take(g.find("start")), take(g.find("mid")),
                       take(g.find("end")))
            if s and m and e:
                out.append(self._arc_path(g, s, m, e, x, y, fmt))
        elif g.tag in ("polyline", "bezier"):
            pts_node = g.find("pts")
            pts = [p for p in (take(xy) for xy in
                               (pts_node.find_all("xy")
                                if pts_node is not None else []))
                   if p is not None]
            if len(pts) < 2:
                return
            if g.tag == "bezier" and len(pts) == 4:
                d = (f"M {x(pts[0][0])} {y(pts[0][1])} "
                     f"C {x(pts[1][0])} {y(pts[1][1])} "
                     f"{x(pts[2][0])} {y(pts[2][1])} "
                     f"{x(pts[3][0])} {y(pts[3][1])}")
                out.append(f'<path class="{_fill_class(g)}" d="{d}"/>')
            else:
                coords = " ".join(f"{x(px)},{y(py)}" for px, py in pts)
                out.append(f'<polyline class="{_fill_class(g)}" '
                           f'points="{coords}"/>')
        elif g.tag == "text":
            at = take(g.find("at"))
            value = unescape_string(_atom(g, 1)) or ""
            if at is not None and value:
                out.append(f'<text class="symtext" x="{x(at[0])}" '
                           f'y="{y(at[1])}">{escape(value)}</text>')
        elif g.tag == "pin":
            self._emit_pin(comp, g, x, y, out, hide_numbers=hide_numbers,
                           hide_names=hide_names,
                           name_offset_mil=name_offset_mil)

    def _emit_pin(self, comp: Component, g: SNode, x: _Fmt, y: _Fmt,
                  out: list[str], *, hide_numbers: bool, hide_names: bool,
                  name_offset_mil: float) -> None:
        at = g.find("at")
        if at is None or _hide_flag(g):
            return
        tipx = _mm_to_mil(_fnum_atom(at, 1))
        tipy = _mm_to_mil(_fnum_atom(at, 2))
        orient = _fnum_atom(at, 3)
        length = _mm_to_mil(_fnum_atom(g.find("length"), 1))
        dx = {0.0: 1.0, 180.0: -1.0}.get(orient % 360, 0.0)
        dy = {90.0: 1.0, 270.0: -1.0}.get(orient % 360, 0.0)
        innx, inny = tipx + length * dx, tipy + length * dy
        if length:
            a = self._pt(comp, tipx, tipy)
            b = self._pt(comp, innx, inny)
            out.append(f'<line class="pinstub" x1="{x(a[0])}" y1="{y(a[1])}" '
                       f'x2="{x(b[0])}" y2="{y(b[1])}"/>')
        if not hide_numbers:
            number = _atom(g.find("number"), 1) or ""
            if number:
                mid = self._pt(comp, (tipx + innx) / 2,
                               (tipy + inny) / 2 + _PIN_NUM_NUDGE_MIL)
                out.append(f'<text class="pinnum" text-anchor="middle" '
                           f'x="{x(mid[0])}" y="{y(mid[1])}">'
                           f'{escape(number)}</text>')
        if not hide_names:
            name = unescape_string(_atom(g.find("name"), 1)) or ""
            if name and name != "~":
                gap = length + name_offset_mil + _PIN_NAME_GAP_MIL
                anchor = self._pt(comp, tipx + gap * dx, tipy + gap * dy)
                out.append(f'<text class="pinname" text-anchor="middle" '
                           f'x="{x(anchor[0])}" y="{y(anchor[1])}">'
                           f'{escape(name)}</text>')

    @staticmethod
    def _arc_path(g: SNode, s: tuple[float, float], m: tuple[float, float],
                  e: tuple[float, float], x: _Fmt, y: _Fmt, fmt: _Fmt) -> str:
        """Three-point arc → SVG A-path (falls back to a polyline chord)."""
        ax, ay = s
        bx, by = m
        cx_, cy_ = e
        d = 2 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        if abs(d) < 1e-9:                # collinear: degenerate arc
            return (f'<polyline class="{_fill_class(g)}" '
                    f'points="{x(ax)},{y(ay)} {x(cx_)},{y(cy_)}"/>')
        ux = ((ax * ax + ay * ay) * (by - cy_) + (bx * bx + by * by)
              * (cy_ - ay) + (cx_ * cx_ + cy_ * cy_) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx_ - bx) + (bx * bx + by * by)
              * (ax - cx_) + (cx_ * cx_ + cy_ * cy_) * (bx - ax)) / d
        r = math.hypot(ax - ux, ay - uy)

        def ang(px: float, py: float) -> float:
            return math.atan2(py - uy, px - ux)

        tau = 2 * math.pi
        d_m = (ang(bx, by) - ang(ax, ay)) % tau
        d_e = (ang(cx_, cy_) - ang(ax, ay)) % tau
        sweep = 1 if d_m <= d_e else 0   # mid on the increasing-angle path?
        span = d_e if sweep else tau - d_e
        large = 1 if span > math.pi else 0
        return (f'<path class="{_fill_class(g)}" d="M {x(ax)} {y(ay)} '
                f'A {fmt(r)} {fmt(r)} 0 {large} {sweep} '
                f'{x(cx_)} {y(cy_)}"/>')
