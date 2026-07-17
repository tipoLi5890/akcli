"""Pure-stdlib SVG rendering of the normalized schematic model (ROADMAP v0.10).

Draws a **reviewable, connectivity-true** picture — component bodies with
refdes/value, pin tips, wires, buses, junctions, labels, power ports and
No-ERC marks — from the same normalized model every check runs on, so an
Altium ``.SchDoc`` renders as readily as a ``.kicad_sch``, with **no KiCad
install**. Deliberately NOT a reproduction of either tool's canvas: symbol
bodies are synthesized from pin geometry (the model carries pin tips, not
symbol artwork). Hierarchical designs render one titled block per sheet.

Coordinates are canonical mils (origin top-left, +Y down) which is exactly
SVG's frame, so geometry maps 1:1. Output is deterministic: same input bytes,
same SVG bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .model import Component, NetPrimitives, Schematic

RENDER_VERSION = "1.0"

_MARGIN_MIL = 200.0
_SHEET_GAP_MIL = 600.0
_PIN_DOT_R = 8.0
_JUNCTION_R = 15.0
_BODY_MIN_MIL = 60.0
_BODY_INSET_MIL = 75.0

# One place for every color/width so the drawing reads as one system.
_STYLE = (
    "text{font-family:monospace}"
    ".wire{stroke:#0a7d2c;stroke-width:6;fill:none;stroke-linecap:round}"
    ".bus{stroke:#00489e;stroke-width:16;fill:none;stroke-linecap:round}"
    ".busentry{stroke:#00489e;stroke-width:6;fill:none}"
    ".body{fill:#fff3d6;stroke:#8b1a1a;stroke-width:5}"
    ".pin{fill:#8b1a1a}"
    ".junction{fill:#0a7d2c}"
    ".label{fill:#003b8e;font-size:50px}"
    ".label-global{fill:#7a0d7a;font-size:50px}"
    ".label-power{fill:#8b1a1a;font-size:45px}"
    ".refdes{fill:#333;font-size:55px;font-weight:bold}"
    ".value{fill:#555;font-size:45px}"
    ".sheet-title{fill:#000;font-size:70px;font-weight:bold}"
    ".noerc{stroke:#c00;stroke-width:6}"
    ".frame{fill:none;stroke:#bbb;stroke-width:3;stroke-dasharray:20,20}"
    ".grid{stroke:#d5d5e0;stroke-width:2;stroke-dasharray:8,16}"
    ".gridlabel{fill:#9a9ab0;font-size:38px}"
    ".origin{stroke:#c08;stroke-width:5}"
)

_GRID_STEP_MIL = 500.0     # major gridline pitch (10x the 50-mil pin grid)
_GRID_LABEL_EVERY = 1000.0  # coordinate captions on every 1000-mil line


@dataclass
class _Bounds:
    min_x: float = float("inf")
    min_y: float = float("inf")
    max_x: float = float("-inf")
    max_y: float = float("-inf")

    def add(self, x: float, y: float) -> None:
        self.min_x = min(self.min_x, x)
        self.min_y = min(self.min_y, y)
        self.max_x = max(self.max_x, x)
        self.max_y = max(self.max_y, y)

    @property
    def empty(self) -> bool:
        return self.min_x > self.max_x

    @property
    def width(self) -> float:
        return 0.0 if self.empty else self.max_x - self.min_x

    @property
    def height(self) -> float:
        return 0.0 if self.empty else self.max_y - self.min_y


def _fmt(v: float) -> str:
    """Trim trailing zeros so output stays byte-stable and compact."""
    return f"{v:.1f}".rstrip("0").rstrip(".")


@dataclass
class _SheetScene:
    """Everything drawn for one sheet, in canonical coordinates."""

    name: str
    components: list[Component] = field(default_factory=list)
    prims: NetPrimitives = field(default_factory=NetPrimitives)
    bounds: _Bounds = field(default_factory=_Bounds)

    def measure(self) -> None:
        for c in self.components:
            self.bounds.add(c.x_mil, c.y_mil)
            for p in c.pins:
                self.bounds.add(p.x_mil, p.y_mil)
        for w in list(self.prims.wires) + list(self.prims.buses):
            self.bounds.add(*w.a)
            self.bounds.add(*w.b)
        for e in self.prims.bus_entries:
            self.bounds.add(*e.a)
            self.bounds.add(*e.b)
        for j in self.prims.junctions:
            self.bounds.add(*j.at)
        for lab in self.prims.labels:
            self.bounds.add(*lab.at)
        for pt in self.prims.no_erc:
            self.bounds.add(*pt)


def _split_sheets(sch: Schematic, prims: NetPrimitives) -> list[_SheetScene]:
    names: list[str] = []
    seen: set[str] = set()
    for obj_sheet in ([c.sheet for c in sch.components]
                      + [w.sheet for w in prims.wires]
                      + [lab.sheet for lab in prims.labels]):
        if obj_sheet not in seen:
            seen.add(obj_sheet)
            names.append(obj_sheet)
    if not names:
        names = [""]
    scenes = {name: _SheetScene(name=name) for name in names}

    for c in sch.components:
        scenes.get(c.sheet, scenes[names[0]]).components.append(c)

    def _route(items: list, into: str) -> None:
        for item in items:
            scene = scenes.get(item.sheet, scenes[names[0]])
            getattr(scene.prims, into).append(item)

    _route(prims.wires, "wires")
    _route(prims.buses, "buses")
    _route(prims.bus_entries, "bus_entries")
    _route(prims.junctions, "junctions")
    _route(prims.labels, "labels")
    # no_erc points carry no sheet field; attach them to the root scene
    scenes[names[0]].prims.no_erc.extend(prims.no_erc)

    ordered = [scenes[n] for n in sorted(names)]
    for s in ordered:
        s.measure()
    return [s for s in ordered if not s.bounds.empty] or ordered[:1]


def _body_rect(c: Component) -> tuple[float, float, float, float]:
    """Synthesized body: pin-tip bbox inset by the nominal pin length."""
    if not c.pins:
        half = _BODY_MIN_MIL / 2
        return c.x_mil - half, c.y_mil - half, _BODY_MIN_MIL, _BODY_MIN_MIL
    b = _Bounds()
    for p in c.pins:
        b.add(p.x_mil, p.y_mil)
    x0, y0 = b.min_x, b.min_y
    w, h = b.width, b.height
    # inset each axis that has pin spread (tips must sit OUTSIDE the body);
    # inflate a spread-less axis so a 2-pin part still gets a visible box
    if w > 2 * _BODY_INSET_MIL:
        x0 += _BODY_INSET_MIL
        w -= 2 * _BODY_INSET_MIL
    else:
        x0 -= (_BODY_MIN_MIL - w) / 2
        w = _BODY_MIN_MIL
    if h > 2 * _BODY_INSET_MIL:
        y0 += _BODY_INSET_MIL
        h -= 2 * _BODY_INSET_MIL
    else:
        y0 -= (_BODY_MIN_MIL - h) / 2
        h = _BODY_MIN_MIL
    return x0, y0, w, h


def _render_grid(scene: _SheetScene, x, y, out: list[str]) -> None:
    """World-coordinate gridlines + captions so an agent can READ positions.

    The whole authoring model is coordinate-driven (mils, 50-mil grid), but a
    plain render gives a multimodal agent no way to tell where (1000, 1600)
    is. Major lines every 500 mil, coordinate captions every 1000, and an
    origin cross when (0, 0) is in view — all in world mils, exactly the
    numbers op-lists use.
    """
    import math
    b = scene.bounds
    if b.empty:
        return
    pad = 100.0
    x0, x1 = b.min_x - pad, b.max_x + pad
    y0, y1 = b.min_y - pad, b.max_y + pad
    gx = math.floor(x0 / _GRID_STEP_MIL) * _GRID_STEP_MIL
    while gx <= x1:
        out.append(f'<line class="grid" x1="{x(gx)}" y1="{y(y0)}" '
                   f'x2="{x(gx)}" y2="{y(y1)}"/>')
        if gx % _GRID_LABEL_EVERY == 0:
            out.append(f'<text class="gridlabel" x="{x(gx + 10)}" '
                       f'y="{y(y0 - 15)}">{_fmt(gx)}</text>')
        gx += _GRID_STEP_MIL
    gy = math.floor(y0 / _GRID_STEP_MIL) * _GRID_STEP_MIL
    while gy <= y1:
        out.append(f'<line class="grid" x1="{x(x0)}" y1="{y(gy)}" '
                   f'x2="{x(x1)}" y2="{y(gy)}"/>')
        if gy % _GRID_LABEL_EVERY == 0:
            out.append(f'<text class="gridlabel" x="{x(x0 - 90)}" '
                       f'y="{y(gy - 10)}">{_fmt(gy)}</text>')
        gy += _GRID_STEP_MIL
    if x0 <= 0 <= x1 and y0 <= 0 <= y1:
        out.append(f'<path class="origin" d="M {x(-60)} {y(0)} L {x(60)} {y(0)} '
                   f'M {x(0)} {y(-60)} L {x(0)} {y(60)}"/>')


def _render_sheet(scene: _SheetScene, dx: float, dy: float,
                  out: list[str], *, grid: bool = False) -> None:
    def x(v: float) -> str:
        return _fmt(v + dx)

    def y(v: float) -> str:
        return _fmt(v + dy)

    b = scene.bounds
    out.append(f'<g data-sheet="{escape(scene.name or "/", {chr(34): "&quot;"})}">')
    if grid:
        _render_grid(scene, x, y, out)
    if scene.name:
        out.append(f'<text class="sheet-title" x="{x(b.min_x)}" '
                   f'y="{y(b.min_y - 80)}">{escape(scene.name)}</text>')
        out.append(f'<rect class="frame" x="{x(b.min_x - 60)}" y="{y(b.min_y - 60)}" '
                   f'width="{_fmt(b.width + 120)}" height="{_fmt(b.height + 120)}"/>')

    for w in scene.prims.buses:
        out.append(f'<line class="bus" x1="{x(w.a[0])}" y1="{y(w.a[1])}" '
                   f'x2="{x(w.b[0])}" y2="{y(w.b[1])}"/>')
    for e in scene.prims.bus_entries:
        out.append(f'<line class="busentry" x1="{x(e.a[0])}" y1="{y(e.a[1])}" '
                   f'x2="{x(e.b[0])}" y2="{y(e.b[1])}"/>')
    for w in scene.prims.wires:
        out.append(f'<line class="wire" x1="{x(w.a[0])}" y1="{y(w.a[1])}" '
                   f'x2="{x(w.b[0])}" y2="{y(w.b[1])}"/>')

    for c in sorted(scene.components, key=lambda c: c.designator):
        bx, by, bw, bh = _body_rect(c)
        out.append(f'<g data-ref="{escape(c.designator, {chr(34): "&quot;"})}">')
        out.append(f'<rect class="body" x="{x(bx)}" y="{y(by)}" '
                   f'width="{_fmt(bw)}" height="{_fmt(bh)}" rx="10"/>')
        for p in c.pins:
            out.append(f'<circle class="pin" cx="{x(p.x_mil)}" '
                       f'cy="{y(p.y_mil)}" r="{_fmt(_PIN_DOT_R)}"/>')
        out.append(f'<text class="refdes" x="{x(bx)}" y="{y(by - 20)}">'
                   f'{escape(c.designator)}</text>')
        if c.value:
            out.append(f'<text class="value" x="{x(bx)}" y="{y(by + bh + 55)}">'
                       f'{escape(c.value)}</text>')
        out.append("</g>")

    for j in scene.prims.junctions:
        out.append(f'<circle class="junction" cx="{x(j.at[0])}" '
                   f'cy="{y(j.at[1])}" r="{_fmt(_JUNCTION_R)}"/>')
    for lab in sorted(scene.prims.labels, key=lambda item: (item.at, item.text)):
        cls = {"global": "label-global", "power": "label-power"}.get(
            lab.scope, "label")
        out.append(f'<text class="{cls}" x="{x(lab.at[0] + 15)}" '
                   f'y="{y(lab.at[1] - 15)}">{escape(lab.text)}</text>')
    for pt in scene.prims.no_erc:
        out.append(f'<path class="noerc" d="M {x(pt[0] - 20)} {y(pt[1] - 20)} '
                   f'L {x(pt[0] + 20)} {y(pt[1] + 20)} '
                   f'M {x(pt[0] - 20)} {y(pt[1] + 20)} '
                   f'L {x(pt[0] + 20)} {y(pt[1] - 20)}"/>')
    out.append("</g>")


def render(sch: Schematic, prims: NetPrimitives, *, grid: bool = False) -> str:
    """Render the schematic + its primitives to an SVG document string.

    ``grid=True`` overlays world-mil gridlines, coordinate captions and an
    origin cross (see :func:`_render_grid`) — for coordinate-driven review.
    """
    scenes = _split_sheets(sch, prims)

    total_w = max((s.bounds.width for s in scenes), default=0.0)
    offsets: list[tuple[float, float]] = []
    cursor_y = _MARGIN_MIL
    for s in scenes:
        title_pad = 150.0 if s.name else 0.0
        offsets.append((_MARGIN_MIL - s.bounds.min_x,
                        cursor_y + title_pad - s.bounds.min_y))
        cursor_y += s.bounds.height + title_pad + _SHEET_GAP_MIL
    height = cursor_y - _SHEET_GAP_MIL + _MARGIN_MIL
    width = total_w + 2 * _MARGIN_MIL

    body: list[str] = []
    for scene, (dx, dy) in zip(scenes, offsets):
        _render_sheet(scene, dx, dy, body, grid=grid)

    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'data-akcli-render="{RENDER_VERSION}">',
        f"<style>{_STYLE}</style>",
        '<rect width="100%" height="100%" fill="#fdfdf8"/>',
        *body,
        "</svg>",
    ]) + "\n"
