"""``plan/draw --render OUT.svg`` — the look-before-you-apply channel."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

__all__ = ["Warn", "render_preview"]

# One stderr line WITHOUT its trailing newline. The CLI owns stderr; engines
# only describe what they would say (see pipeline/__init__).
Warn = Callable[[str], None]


def render_preview(tmp_path: Path, out_path: str, warn: Warn) -> dict[str, Any] | None:
    """Render the dry-applied temp copy to ``out_path`` (SVG).

    The agent's look-before-apply channel: ``plan/draw --render OUT.svg``
    shows the WOULD-BE sheet without touching the target. Non-fatal by
    policy — a broken preview must never block a valid draw (same contract
    as "net diff unavailable"), so any failure is a stderr warning + ``None``.
    """
    try:
        from .. import render_svg
        from ..readers import kicad as kreader
        sch = kreader.read_sch(str(tmp_path))
        prims = kreader.read_primitives(str(tmp_path))
        # previews are agent-facing: always include the coordinate grid so
        # the next op-list's numbers can be read straight off the image
        svg = render_svg.render(sch, prims, grid=True)
        out = Path(out_path)
        out.write_text(svg, encoding="utf-8", newline="\n")
        return {"path": str(out), "bytes": len(svg.encode("utf-8"))}
    except Exception as e:  # noqa: BLE001 — advisory surface, never fatal
        warn(f"WARNING: preview render unavailable: {type(e).__name__}: {e}")
        return None
