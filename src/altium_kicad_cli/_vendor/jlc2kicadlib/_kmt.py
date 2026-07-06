"""Clean-room mini footprint writer replacing the GPLv3 ``KicadModTree``.

Original akcli code (project MIT license) — NOT an upstream JLC2KiCadLib file,
and NOT derived from KicadModTree's source: it implements, from the public
KiCad footprint *file format*, exactly the API surface the vendored
JLC2KiCadLib modules use (class names/kwargs are the interface those modules
call; the serialization below is the modern ``(footprint ...)`` dialect KiCad
7-10 read natively, not KicadModTree 1.x's legacy ``(module ...)`` output).

Coordinate/unit contract: geometry values arrive in **mm** (the vendored
handlers convert EasyEDA mils to mm). ``Model.at`` arrives in the legacy
KicadModTree convention of **inches** and is serialized as a modern
``(offset (xyz ...))`` in mm (x25.4).
"""

from __future__ import annotations

import math

__all__ = [
    "Arc", "Circle", "Footprint", "KicadFileHandler", "Line", "Model", "Pad",
    "Polygon", "RectFill", "RectLine", "Text", "Translation", "Vector2D",
]


# --------------------------------------------------------------------------- #
# small geometry helper (used by the vendored SVG-arc math)
# --------------------------------------------------------------------------- #
class Vector2D:
    def __init__(self, x, y=None):
        if y is None:  # Vector2D((x, y)) / Vector2D([x, y]) / Vector2D(other)
            x, y = x[0], x[1]
        self.x = float(x)
        self.y = float(y)

    def __getitem__(self, i):
        return (self.x, self.y)[i]

    def __add__(self, other):
        return Vector2D(self.x + other[0], self.y + other[1])

    def __mul__(self, k):
        return Vector2D(self.x * k, self.y * k)

    __rmul__ = __mul__

    def rotate(self, degrees):
        r = math.radians(degrees)
        c, s = math.cos(r), math.sin(r)
        return Vector2D(self.x * c - self.y * s, self.x * s + self.y * c)

    def distance_to(self, other):
        return math.hypot(self.x - other[0], self.y - other[1])


def _xy(pt) -> tuple[float, float]:
    return (float(pt[0]), float(pt[1]))


def _fmt(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# nodes (constructor kwargs == the surface the vendored handlers call)
# --------------------------------------------------------------------------- #
class Line:
    def __init__(self, start, end, width, layer):
        self.start, self.end = _xy(start), _xy(end)
        self.width, self.layer = float(width), layer


class Arc:
    def __init__(self, start, end, center, width, layer):
        self.start, self.end, self.center = _xy(start), _xy(end), _xy(center)
        self.width, self.layer = float(width), layer


class Circle:
    def __init__(self, center, radius, width, layer):
        self.center, self.radius = _xy(center), float(radius)
        self.width, self.layer = float(width), layer


class Polygon:
    def __init__(self, nodes, layer=None, width=0.0):
        self.nodes = [_xy(p) for p in nodes]  # materialize (callers pass zip())
        self.layer, self.width = layer, float(width)


class RectLine:
    def __init__(self, start, end, width, layer):
        self.start, self.end = _xy(start), _xy(end)
        self.width, self.layer = float(width), layer


class RectFill:
    def __init__(self, start, end, layer):
        self.start, self.end, self.layer = _xy(start), _xy(end), layer


class Text:
    def __init__(self, type, text, at, layer):  # noqa: A002 - upstream kwarg name
        self.type, self.text = type, str(text)
        self.at, self.layer = _xy(at), layer


class Pad:
    TYPE_THT = "thru_hole"
    TYPE_SMT = "smd"
    TYPE_NPTH = "np_thru_hole"
    SHAPE_CIRCLE = "circle"
    SHAPE_OVAL = "oval"
    SHAPE_RECT = "rect"
    SHAPE_CUSTOM = "custom"
    LAYERS_THT = ["*.Cu", "*.Mask"]
    LAYERS_SMT = ["F.Cu", "F.Paste", "F.Mask"]
    LAYERS_NPTH = ["*.Cu", "*.Mask"]

    def __init__(self, number, type, shape, at, size, layers,  # noqa: A002
                 rotation=0.0, drill=None, primitives=""):
        self.number = str(number)
        self.type = type
        self.shape = shape
        self.at = _xy(at)
        self.size = _xy(size) if isinstance(size, (list, tuple)) else (float(size), float(size))
        self.rotation = float(rotation or 0.0)
        self.drill = drill
        self.layers = list(layers)
        self.primitives = list(primitives) if primitives else []


class Model:
    def __init__(self, filename, at, rotate, scale=(1.0, 1.0, 1.0)):
        # upstream sometimes pre-quotes path-variable filenames — strip that.
        self.filename = str(filename).strip('"')
        self.at = tuple(float(v) for v in at)          # legacy inches
        self.rotate = tuple(float(v) for v in rotate)
        self.scale = tuple(float(v) for v in scale)


class Translation:
    """Container that offsets everything inside it (the only transform used)."""

    def __init__(self, x, y):
        self.offset = (float(x), float(y))
        self.children: list = []

    def append(self, node) -> None:
        self.children.append(node)


class Footprint:
    def __init__(self, name):
        self.name = str(name).strip('"')  # upstream passes a pre-quoted name
        self.description = ""
        self.tags = ""
        self.attribute = "smd"
        self.children: list = []

    def setDescription(self, s):  # noqa: N802 - upstream API name
        self.description = str(s)

    def setTags(self, s):  # noqa: N802
        self.tags = str(s)

    def setAttribute(self, s):  # noqa: N802
        self.attribute = str(s)

    def append(self, node) -> None:
        self.children.append(node)

    def insert(self, node) -> None:
        """Re-parent all current children under ``node`` (Translation wrap)."""
        if isinstance(node, Translation):
            node.children = self.children
            self.children = [node]
        else:
            self.children.append(node)

    def getAllChilds(self):  # noqa: N802
        out = []
        stack = list(self.children)
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(getattr(n, "children", []) or [])
        return out


# --------------------------------------------------------------------------- #
# serialization — modern (footprint ...) s-expression
# --------------------------------------------------------------------------- #
def _shift(pt, off):
    return (pt[0] + off[0], pt[1] + off[1])


def _arc_mid(start, end, center):
    """Point on the arc halfway from start to end (clockwise, +Y-down plane).

    The vendored handler normalizes SVG sweep so the arc always runs clockwise
    (screen sense, +Y down) from ``start`` to ``end`` around ``center``.
    """
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    r = math.hypot(start[0] - center[0], start[1] - center[1])
    sweep = (a1 - a0) % (2 * math.pi)  # CW in y-down == increasing atan2 angle
    am = a0 + sweep / 2.0
    return (center[0] + r * math.cos(am), center[1] + r * math.sin(am))


def _stroke(width):
    return f"(stroke (width {_fmt(width)}) (type solid))"


def _serialize_node(node, off, out: list[str]) -> None:
    ind = "\t"
    if isinstance(node, Translation):
        noff = (off[0] + node.offset[0], off[1] + node.offset[1])
        for child in node.children:
            _serialize_node(child, noff, out)
        return
    if isinstance(node, Line):
        s, e = _shift(node.start, off), _shift(node.end, off)
        out.append(f"{ind}(fp_line (start {_fmt(s[0])} {_fmt(s[1])}) "
                   f"(end {_fmt(e[0])} {_fmt(e[1])}) {_stroke(node.width)} "
                   f"(layer {_q(node.layer)}))")
    elif isinstance(node, RectLine) or isinstance(node, RectFill):
        s, e = _shift(node.start, off), _shift(node.end, off)
        width = getattr(node, "width", 0.0)
        fill = "none" if isinstance(node, RectLine) else "solid"
        out.append(f"{ind}(fp_rect (start {_fmt(s[0])} {_fmt(s[1])}) "
                   f"(end {_fmt(e[0])} {_fmt(e[1])}) {_stroke(width)} "
                   f"(fill {fill}) (layer {_q(node.layer)}))")
    elif isinstance(node, Arc):
        s, e = _shift(node.start, off), _shift(node.end, off)
        c = _shift(node.center, off)
        m = _arc_mid(s, e, c)
        out.append(f"{ind}(fp_arc (start {_fmt(s[0])} {_fmt(s[1])}) "
                   f"(mid {_fmt(m[0])} {_fmt(m[1])}) (end {_fmt(e[0])} {_fmt(e[1])}) "
                   f"{_stroke(node.width)} (layer {_q(node.layer)}))")
    elif isinstance(node, Circle):
        c = _shift(node.center, off)
        out.append(f"{ind}(fp_circle (center {_fmt(c[0])} {_fmt(c[1])}) "
                   f"(end {_fmt(c[0] + node.radius)} {_fmt(c[1])}) "
                   f"{_stroke(node.width)} (fill none) (layer {_q(node.layer)}))")
    elif isinstance(node, Polygon):
        pts = " ".join(f"(xy {_fmt(p[0] + off[0])} {_fmt(p[1] + off[1])})"
                       for p in node.nodes)
        layer = node.layer or "F.Fab"
        out.append(f"{ind}(fp_poly (pts {pts}) {_stroke(node.width)} "
                   f"(fill solid) (layer {_q(layer)}))")
    elif isinstance(node, Text):
        a = _shift(node.at, off)
        out.append(f"{ind}(fp_text {node.type} {_q(node.text)} "
                   f"(at {_fmt(a[0])} {_fmt(a[1])}) (layer {_q(node.layer)}) "
                   f"(effects (font (size 1 1) (thickness 0.15))))")
    elif isinstance(node, Pad):
        a = _shift(node.at, off)
        at = (f"(at {_fmt(a[0])} {_fmt(a[1])}"
              + (f" {_fmt(node.rotation)}" if node.rotation else "") + ")")
        parts = [f"(pad {_q(node.number)} {node.type} {node.shape} {at} "
                 f"(size {_fmt(node.size[0])} {_fmt(node.size[1])})"]
        drill = node.drill
        if drill and node.type in (Pad.TYPE_THT, Pad.TYPE_NPTH):
            if isinstance(drill, (list, tuple)):
                parts.append(f"(drill oval {_fmt(drill[0])} {_fmt(drill[1])})")
            elif float(drill) > 0:
                parts.append(f"(drill {_fmt(drill)})")
        parts.append("(layers " + " ".join(_q(layer) for layer in node.layers) + ")")
        if node.shape == Pad.SHAPE_CUSTOM and node.primitives:
            prims = []
            for prim in node.primitives:
                if isinstance(prim, Polygon):
                    pts = " ".join(f"(xy {_fmt(p[0])} {_fmt(p[1])})" for p in prim.nodes)
                    prims.append(f"(gr_poly (pts {pts}) (width 0) (fill yes))")
            parts.append("(options (clearance outline) (anchor circle)) "
                         "(primitives " + " ".join(prims) + ")")
        out.append(ind + " ".join(parts) + ")")
    elif isinstance(node, Model):
        o = tuple(v * 25.4 for v in node.at)  # legacy inches -> mm offset
        out.append(
            f"{ind}(model {_q(node.filename)}\n"
            f"{ind}\t(offset (xyz {_fmt(o[0])} {_fmt(o[1])} {_fmt(o[2])}))\n"
            f"{ind}\t(scale (xyz {_fmt(node.scale[0])} {_fmt(node.scale[1])} "
            f"{_fmt(node.scale[2])}))\n"
            f"{ind}\t(rotate (xyz {_fmt(node.rotate[0])} {_fmt(node.rotate[1])} "
            f"{_fmt(node.rotate[2])}))\n"
            f"{ind})"
        )
    # unknown nodes are ignored (none exist in the vendored call surface)


class KicadFileHandler:
    def __init__(self, footprint: Footprint):
        self.footprint = footprint

    def serialize(self) -> str:
        fp = self.footprint
        lines = [
            f"(footprint {_q(fp.name)}",
            '\t(version 20240108)',
            '\t(generator "akcli")',
            '\t(layer "F.Cu")',
            f"\t(descr {_q(fp.description)})",
            f"\t(tags {_q(fp.tags)})",
            f"\t(attr {fp.attribute})",
        ]
        body: list[str] = []
        for child in fp.children:
            _serialize_node(child, (0.0, 0.0), body)
        # KiCad expects graphics/text before pads before models; keep a stable
        # grouped order for diffability.
        def _rank(line: str) -> int:
            if line.lstrip("\t").startswith("(pad"):
                return 1
            if line.lstrip("\t").startswith("(model"):
                return 2
            return 0
        body.sort(key=_rank)
        lines.extend(body)
        lines.append(")")
        return "\n".join(lines) + "\n"

    def writeFile(self, path) -> None:  # noqa: N802 - upstream API name
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.serialize())
