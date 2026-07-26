"""Ground plane / stackup review (EMC batches 1 & 3).

Return currents follow the path of least loop area; a multilayer board
without a ground pour, a pour covering little of the outline, or two signal
layers facing each other without a reference between them all widen return
loops — the primary radiated-emission mechanism.
"""

from __future__ import annotations

from ....classify import is_ground as _is_ground
from ....report import Finding, Severity
from ... import Detector, Rule, register
from ... import geometry

_COVERAGE_FLOOR = 0.5

RULES = (
    Rule(
        code="REVIEW_EMC_NO_GND_PLANE",
        title="Multilayer board has no ground pour",
        explain=(
            "The board declares two or more copper layers but no zone is "
            "bound to a ground net. Without a plane, every return current "
            "finds its own loop — the dominant radiated-emission source. "
            "Pour a ground plane (or waive if the ground is a deliberate "
            "grid)."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="return-current loop area vs radiated emissions"),
    Rule(
        code="REVIEW_EMC_PLANE_COVERAGE",
        title="Ground pour covers little of the board outline",
        explain=(
            "The ground zone's bounding box covers less than "
            f"{_COVERAGE_FLOOR:.0%} of the outline's bounding box. Signals "
            "leaving the pour lose their return path. Bounding boxes "
            "over-estimate real coverage, so a flagged board is worth a "
            "look; a passing one may still have voids (batch-2 void "
            "detection needs zone polygons)."),
        default_severity="note", confidence="heuristic", version="1",
        reference=None),
    Rule(
        code="REVIEW_EMC_STACKUP_ADJACENT",
        title="Two signal layers are stacked without a reference between",
        explain=(
            "Consecutive copper layers both typed `signal` in the board "
            "stackup: broadside-coupled crosstalk and undefined return "
            "paths. Layer order follows the file's declaration order "
            "(assumption stated). Re-order the stack so every signal layer "
            "faces a plane."),
        default_severity="note", confidence="heuristic", version="1",
        reference="stackup guidance: signal layers reference a plane"),
)


def run(ctx) -> list[Finding]:
    pcb = ctx.pcb
    if pcb is None:
        return []
    out: list[Finding] = []
    board = getattr(pcb, "board", {}) or {}
    copper = board.get("copper_layers") or []
    zones = getattr(pcb, "zones", []) or []
    gnd_zones = [z for z in zones if _is_ground(z.get("net"))]

    if len(copper) >= 2 and not gnd_zones:
        out.append(Finding(
            code="REVIEW_EMC_NO_GND_PLANE", severity=Severity.WARNING,
            message=(f"{len(copper)}-layer board has no ground zone — "
                     "return currents have no plane"),
            refs=[], confidence="heuristic",
            evidence={"source": "geometry",
                      "calc": {"copper_layers": len(copper),
                               "gnd_zones": 0}},
            remediation="pour a ground plane on an inner or bottom layer"))

    outline = board.get("outline_bbox")
    if gnd_zones and outline:
        s = geometry.unit_scale(pcb)
        (ox0, oy0), (ox1, oy1) = outline
        b_area = abs(ox1 - ox0) * abs(oy1 - oy0) * s * s
        best = 0.0
        for z in gnd_zones:
            if not z.get("bbox"):
                continue
            (zx0, zy0), (zx1, zy1) = z["bbox"]
            best = max(best, abs(zx1 - zx0) * abs(zy1 - zy0) * s * s)
        if b_area > 0 and best / b_area < _COVERAGE_FLOOR:
            out.append(Finding(
                code="REVIEW_EMC_PLANE_COVERAGE", severity=Severity.NOTE,
                message=(f"largest ground pour spans ≈{best / b_area:.0%} of "
                         "the outline (bbox approximation) — signals beyond "
                         "it run without a return plane"),
                refs=[], confidence="heuristic",
                evidence={"source": "geometry",
                          "calc": {"coverage": round(best / b_area, 3),
                                   "floor": _COVERAGE_FLOOR},
                          "assumptions": [
                              "coverage compared by BOUNDING BOX — real "
                              "polygons cover less, never more"]},
                remediation="extend the pour under every routed area"))

    layers = board.get("layers") or []
    cu = [entry for entry in layers
          if str(entry.get("name", "")).endswith(".Cu")]
    for a, b in zip(cu, cu[1:]):
        if a.get("type") == "signal" and b.get("type") == "signal":
            out.append(Finding(
                code="REVIEW_EMC_STACKUP_ADJACENT", severity=Severity.NOTE,
                message=(f"stackup: {a.get('name')} and {b.get('name')} are "
                         "adjacent signal layers with no reference between"),
                refs=[str(a.get("name")), str(b.get("name"))],
                anchors=[], confidence="heuristic",
                evidence={"source": "geometry",
                          "assumptions": ["layer order taken from the "
                                          "file's declaration order"]},
                remediation="re-order the stack so each signal layer faces "
                            "a plane"))
    return out


register(Detector(name="emc.planes", family="emc", run=run, rules=RULES))
