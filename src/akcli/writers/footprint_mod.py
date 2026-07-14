"""``FootprintDef`` -> ``.kicad_mod`` text (the PcbLib import target).

Pads are carried over verbatim — positions, sizes, drills, shapes and rotation
are NEVER recomputed. What the reader did not decode (silkscreen graphics,
text, 3D bodies) is not invented here either; the import command surfaces those
as warnings and records them in the provenance file. A courtyard is only added
when the caller asks for one (declared transformation, not a silent default).
"""

from __future__ import annotations

from ..model import FootprintDef, FootprintPad

__all__ = ["to_kicad_mod"]

_FORMAT_VERSION = "20240108"

_SHAPES = {"circle": "circle", "rect": "rect", "roundrect": "roundrect",
           "oval": "oval", "octagon": "rect"}  # KiCad has no octagon pad shape


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _pad_sexpr(p: FootprintPad, warnings: list[str]) -> str:
    shape = _SHAPES.get(p.shape)
    if shape is None:
        warnings.append(f"pad {p.number}: shape {p.shape!r} approximated as rect")
        shape = "rect"
    elif p.shape == "octagon":
        warnings.append(f"pad {p.number}: octagon approximated as rect")
    at = f"(at {_fmt(p.x_mm)} {_fmt(p.y_mm)}"
    if p.rotation:
        at += f" {_fmt(p.rotation)}"
    at += ")"
    parts = [f'(pad "{p.number}" {p.pad_type} {shape}',
             f"\t\t{at}",
             f"\t\t(size {_fmt(p.size_x_mm)} {_fmt(p.size_y_mm)})"]
    if p.drill_mm:
        parts.append(f"\t\t(drill {_fmt(p.drill_mm)})")
    layers = " ".join(f'"{layer}"' for layer in p.layers) or '"F.Cu" "F.Paste" "F.Mask"'
    parts.append(f"\t\t(layers {layers})")
    return "\n".join(parts) + "\n\t)"


def _courtyard(pads: list[FootprintPad], clearance_mm: float) -> list[str]:
    xs = [p.x_mm - p.size_x_mm / 2 for p in pads] + [p.x_mm + p.size_x_mm / 2 for p in pads]
    ys = [p.y_mm - p.size_y_mm / 2 for p in pads] + [p.y_mm + p.size_y_mm / 2 for p in pads]
    x0, x1 = min(xs) - clearance_mm, max(xs) + clearance_mm
    y0, y1 = min(ys) - clearance_mm, max(ys) + clearance_mm
    lines = []
    for (ax, ay), (bx, by) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                               ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        lines.append(
            f"\t(fp_line\n\t\t(start {_fmt(ax)} {_fmt(ay)})\n"
            f"\t\t(end {_fmt(bx)} {_fmt(by)})\n"
            f"\t\t(stroke\n\t\t\t(width 0.05)\n\t\t\t(type solid)\n\t\t)\n"
            f'\t\t(layer "F.CrtYd")\n\t)')
    return lines


def to_kicad_mod(fp: FootprintDef, *, courtyard_mm: float | None = None,
                 warnings: list[str] | None = None) -> str:
    """Render ``fp`` as modern ``.kicad_mod`` text.

    ``courtyard_mm``: when given (and the source has no courtyard), draw a
    pad-bbox rectangle on ``F.CrtYd`` with that clearance — a DECLARED
    transformation the caller reports, never a silent one.
    """
    w = warnings if warnings is not None else []
    attr = "through_hole" if "through_hole" in fp.attributes else "smd"
    body = [f'(footprint "{fp.name}"',
            f"\t(version {_FORMAT_VERSION})",
            '\t(generator "akcli")',
            '\t(layer "F.Cu")',
            f"\t(attr {attr})"]
    if courtyard_mm is not None and not fp.courtyard and fp.pads:
        body.extend(_courtyard(fp.pads, courtyard_mm))
        w.append(f"{fp.name}: courtyard synthesized from pad bbox "
                 f"(+{_fmt(courtyard_mm)}mm) — declared transformation")
    for p in fp.pads:
        body.append("\t" + _pad_sexpr(p, w))
    body.append(")")
    return "\n".join(body) + "\n"
