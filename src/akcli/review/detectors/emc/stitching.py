"""Ground-via stitching review (EMC batch 1).

Ground layers act as one reference only when tied together tighter than a
fraction of the shortest wavelength on the board; sparse stitching turns the
inter-plane cavity into a patch antenna.
"""

from __future__ import annotations

from ....classify import is_ground as _is_ground
from ....report import Finding, Severity
from ... import Detector, Rule, register
from ... import geometry
from ...tables import EMC_ER_EFF, EMC_FMAX_HZ, EMC_STITCH_FRACTION

_C = 299_792_458.0e3        # mm/s


def _lambda_frac_mm() -> float:
    return _C / (EMC_FMAX_HZ * EMC_ER_EFF ** 0.5) / EMC_STITCH_FRACTION


RULES = (
    Rule(
        code="REVIEW_EMC_VIA_STITCH",
        title="Ground stitching vias are absent or too sparse",
        explain=(
            "A multilayer board's ground vias should sit closer than "
            f"λ/{EMC_STITCH_FRACTION} at the highest expected harmonic "
            f"content (assumed {EMC_FMAX_HZ / 1e9:g} GHz, ε_eff "
            f"{EMC_ER_EFF:g} → ≈{_lambda_frac_mm():.1f} mm). No ground vias "
            "at all is a warning; a sparse region (largest nearest-"
            "neighbour gap over the limit) is a note — both assumptions "
            "ride in the evidence."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="cavity-resonance suppression: stitching ≤ λ/20 at f_max"),
)


def run(ctx) -> list[Finding]:
    pcb = ctx.pcb
    if pcb is None:
        return []
    board = getattr(pcb, "board", {}) or {}
    if len(board.get("copper_layers") or []) < 2:
        return []
    s = geometry.unit_scale(pcb)
    gnd = [(v["at"][0] * s, v["at"][1] * s)
           for v in getattr(pcb, "vias", []) or []
           if _is_ground(v.get("net")) and v.get("at")]
    limit = _lambda_frac_mm()
    assumptions = [
        f"harmonic content to {EMC_FMAX_HZ / 1e9:g} GHz (assumed)",
        f"ε_eff = {EMC_ER_EFF:g} (FR4)",
        f"spacing floor λ/{EMC_STITCH_FRACTION} ≈ {limit:.1f} mm",
    ]
    if not gnd:
        return [Finding(
            code="REVIEW_EMC_VIA_STITCH", severity=Severity.WARNING,
            message=("multilayer board has no ground stitching vias — the "
                     "plane pair is an unstitched cavity"),
            refs=[], confidence="heuristic",
            evidence={"source": "geometry", "assumptions": assumptions},
            remediation="stitch the ground layers on a regular grid")]
    if len(gnd) == 1:
        return []
    worst = 0.0
    for i, (x, y) in enumerate(gnd):
        nn = min(((x - a) ** 2 + (y - b) ** 2) ** 0.5
                 for j, (a, b) in enumerate(gnd) if j != i)
        worst = max(worst, nn)
    if worst <= limit:
        return []
    return [Finding(
        code="REVIEW_EMC_VIA_STITCH", severity=Severity.NOTE,
        message=(f"sparsest ground via sits {worst:.1f} mm from its nearest "
                 f"neighbour — beyond the λ/{EMC_STITCH_FRACTION} "
                 f"≈{limit:.1f} mm stitching floor"),
        refs=[], confidence="heuristic",
        evidence={"source": "geometry",
                  "calc": {"worst_gap_mm": round(worst, 1),
                           "limit_mm": round(limit, 1),
                           "gnd_vias": len(gnd)},
                  "assumptions": assumptions},
        remediation="densify stitching where the gap is widest")]


register(Detector(name="emc.stitching", family="emc", run=run, rules=RULES))
