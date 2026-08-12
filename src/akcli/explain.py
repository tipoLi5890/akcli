"""Pin-attachment diagnosis (rank 70) — the engine behind ``akcli net --why``.

Answers the question ``component`` cannot: WHY a pin shows ``(no net)``.
Reports junction/wire-end attachment status, the nearest wire end / label /
other pin with mil distances, and the off-grid trap.  Uses the SAME quantized
helpers as :mod:`akcli.netbuild`, so the verdict can never disagree with the
net engine.

Pure engine: it builds and returns the diagnosis payload.  Resolving
``REF.PIN`` (and the miss envelope), the net JSON shape and the text/JSON
rendering all stay in the CLI layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = ["net_why", "off_grid_50"]


def off_grid_50(v: float) -> bool:
    """True when ``v`` (mil) misses the 50-mil grid by more than 0.01 mil."""
    r = v % 50.0
    return min(r, 50.0 - r) > 0.01


def net_why(path: Path, resolved: tuple[str, str], net: Any,
            net_doc: dict[str, Any] | None) -> dict[str, Any]:
    """Build the ``net --why REF.PIN`` diagnosis payload.

    ``resolved`` is the already-validated ``(ref, pin)``; ``net`` the Net the
    pin belongs to (or ``None``) and ``net_doc`` its JSON shape — the caller
    owns both, so this module never depends on the CLI's net serializer.
    """
    import math

    ref, pin = resolved
    payload: dict[str, Any] = {
        "source": path.as_posix(),
        "pin": f"{ref}.{pin}",
        "net": net_doc,
    }
    hints: list[str] = []

    if path.suffix.lower() != ".kicad_sch":
        hints.append("geometry diagnosis needs a .kicad_sch (this input only "
                     "answers net membership)")
        payload.update({"at": None, "attachment": None, "nearest": None,
                        "off_grid_50mil": None, "hints": hints})
        return payload

    from . import netbuild
    from .readers import kicad as _krd
    prims = _krd.read_primitives(path)
    ph = next((p for p in prims.pins if p.ref == resolved), None)
    if ph is None:
        hints.append("no placed pin handle found (is this unit placed on the "
                     "root sheet?)")
        payload.update({"at": None, "attachment": None, "nearest": None,
                        "off_grid_50mil": None, "hints": hints})
        return payload

    q, on_seg = netbuild.quantize, netbuild.on_segment
    qp = q(ph.at)
    segs = [(w.a, w.b) for w in prims.wires if w.sheet == ph.sheet]
    qsegs = [(q(a), q(b)) for a, b in segs]
    at_junction = any(q(j.at) == qp for j in prims.junctions
                      if j.sheet == ph.sheet)
    at_wire_end = any(qp in (qa, qb) for qa, qb in qsegs)
    midspan = (not at_wire_end and not at_junction
               and any(on_seg(qp, qa, qb) for qa, qb in qsegs))
    label_on_tip = any(
        q(lb.at) == qp for lb in prims.labels
        if lb.sheet == ph.sheet and lb.scope != "hier" and lb.text)

    def _dist(pt: tuple[float, float]) -> float:
        return math.hypot(pt[0] - ph.at[0], pt[1] - ph.at[1])

    def _nearest(
        candidates: Iterable[tuple[dict[str, Any] | None, tuple[float, float]]],
    ) -> dict[str, Any] | None:
        best: tuple[dict[str, Any] | None, tuple[float, float]] | None = None
        best_d: float | None = None
        for tag, pt in candidates:
            d = _dist(pt)
            if best_d is None or d < best_d:
                best, best_d = (tag, pt), d
        if best is None or best_d is None:
            return None
        (tag, pt) = best
        entry: dict[str, Any] = {"at": [round(pt[0], 1), round(pt[1], 1)],
                                 "dist_mil": round(best_d, 1)}
        if tag:
            entry.update(tag)
        return entry

    near_wire = _nearest(
        (None, e) for seg in segs for e in seg)
    near_label = _nearest(
        ({"text": lb.text}, lb.at) for lb in prims.labels
        if lb.sheet == ph.sheet and lb.scope != "hier" and lb.text)
    near_pin = _nearest(
        ({"pin": f"{p.ref[0]}.{p.ref[1]}"}, p.at) for p in prims.pins
        if p.sheet == ph.sheet and p.ref != resolved)

    off_grid = off_grid_50(ph.at[0]) or off_grid_50(ph.at[1])

    if midspan:
        hints.append(f"pin tip at ({round(ph.at[0])},{round(ph.at[1])}) "
                     "touches a wire MID-SPAN with no junction — not "
                     "connected; add an add_junction there or end the wire "
                     "on the pin tip")
    # a single-member net is electrically the same as "not connected" —
    # the pin only nets with itself, so the near-miss hints still apply.
    unconnected = net is None or len(net.members) <= 1
    if unconnected:
        if near_wire and 0 < near_wire["dist_mil"] <= 100:
            hints.append(f"nearest wire end is {near_wire['dist_mil']} mil "
                         f"away at ({near_wire['at'][0]},{near_wire['at'][1]})"
                         " — extend it to the pin tip")
        if near_label and 0 < near_label["dist_mil"] <= 100:
            hints.append(f"label '{near_label['text']}' floats "
                         f"{near_label['dist_mil']} mil away — anchor it "
                         "exactly on the pin tip")
    if off_grid:
        hints.append("pin tip is OFF the 50-mil grid — grid-snapped wires "
                     "cannot land on it; move the component onto the grid")

    payload.update({
        "at": [round(ph.at[0], 1), round(ph.at[1], 1)],
        "sheet": ph.sheet,
        "attachment": {"at_junction": at_junction, "at_wire_end": at_wire_end,
                       "midspan_touch": midspan, "label_on_tip": label_on_tip},
        "nearest": {"wire_end": near_wire, "label": near_label,
                    "pin": near_pin},
        "off_grid_50mil": off_grid,
        "hints": hints,
    })
    return payload
