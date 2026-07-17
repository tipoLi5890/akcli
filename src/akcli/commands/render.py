"""`akcli render` — install-free SVG rendering of a schematic.

The visual feedback channel for agents and reviewers: after a `draw --apply`,
render the sheet and *look* at it (a multimodal agent reads the image
directly) — no KiCad install, works on Altium `.SchDoc` too, deterministic
output. Connectivity-true, not pixel-faithful: see :mod:`akcli.render_svg`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..errors import EXIT
from ._shared import _dumps, _emit, _load_schematic, _require_path


def _cmd_render(args: argparse.Namespace) -> int:
    from .. import render_svg
    from ..readers import kicad as kreader

    path = _require_path(args.path)
    sch = _load_schematic(path)
    if sch.source_format == "kicad":
        prims = kreader.read_primitives(str(path))
    else:
        from ..readers import altium_sch
        prims = altium_sch.read_primitives(str(path))

    svg = render_svg.render(sch, prims, grid=bool(getattr(args, "grid", False)))

    out_arg = getattr(args, "output", None)
    if out_arg == "-":
        _emit(svg)
        return EXIT["OK"]
    out = Path(out_arg) if out_arg else path.with_suffix(path.suffix + ".svg")
    out.write_text(svg, encoding="utf-8")

    summary = {
        "schema_version": "1",
        "render_version": render_svg.RENDER_VERSION,
        "source": str(path),
        "output": str(out),
        "components": len(sch.components),
        "wires": len(prims.wires),
        "labels": len(prims.labels),
        "junctions": len(prims.junctions),
        "bytes": len(svg.encode("utf-8")),
    }
    if args.json:
        _emit(_dumps(summary))
    else:
        sys.stderr.write(f"wrote {out} ({summary['components']} component(s), "
                         f"{summary['wires']} wire(s))\n")
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "render", parents=[common],
        help="render a schematic to SVG (no KiCad install; Altium too)")
    p.add_argument("path", nargs="?", help="input .kicad_sch or .SchDoc")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="output SVG path ('-' = stdout; default: "
                        "<input>.svg next to the input)")
    p.add_argument("--grid", action="store_true",
                   help="overlay world-mil gridlines + coordinate captions + "
                        "origin cross (read placement coordinates off the "
                        "image; plan/draw --render previews include it)")
    p.set_defaults(handler=_cmd_render)
