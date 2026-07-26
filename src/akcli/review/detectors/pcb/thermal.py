"""Thermal review: exposed-pad via stitching + junction-temperature estimate.

Via counting is measured geometry; the junction estimate follows
``Tj = Ta + P·θ_JA`` with θ_JA from the part's facts file (datasheet_backed)
or, failing that, a typical-package table (honestly heuristic). Without a
recorded power dissipation there is NO estimate — a temperature is never
invented.
"""

from __future__ import annotations

from ....classify import refdes_prefix as _prefix
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import geometry
from ...tables import (PACKAGE_THETA_JA, T_AMBIENT_C,
                       T_JUNCTION_MAX_DEFAULT_C, THERMAL_PAD_MIN_MM2,
                       THERMAL_VIA_MIN)

RULES = (
    Rule(
        code="REVIEW_THERMAL_VIA",
        title="Exposed pad carries too few thermal vias",
        explain=(
            f"A pad of ≥ {THERMAL_PAD_MIN_MM2:g} mm² on an active part "
            "(U/Q prefix) is treated as a thermal pad; fewer than "
            f"{THERMAL_VIA_MIN} vias on its net inside the pad boundary "
            "leaves the package's heat path mostly unbuilt — package "
            "application notes typically call for 4–9 vias under the EP. "
            "Pad-role inference is heuristic; the via count is measured."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="package thermal application guidance (vias under the "
                  "exposed pad)"),
    Rule(
        code="REVIEW_THERMAL_JUNCTION",
        title="Estimated junction temperature vs its limit",
        explain=(
            "Tj = Ta + P·θ_JA, with the ambient assumption stated. θ_JA and "
            "the dissipation come from the part's facts file "
            "(datasheet_backed); when only the dissipation is recorded, a "
            "typical-package θ_JA table fills in and the judgement stays "
            "heuristic. The limit is the facts file's t_j_max, else the "
            f"{T_JUNCTION_MAX_DEFAULT_C:g} °C industry default (assumption)."),
        default_severity="warning", confidence="datasheet_backed", version="1",
        reference="Tj = Ta + P·θ_JA (facts: theta_ja / power_dissipation / "
                  "t_j_max)"),
)


def _package_theta(footprint_name: str | None) -> float | None:
    hay = (footprint_name or "").upper()
    for token, theta in PACKAGE_THETA_JA:
        if token.upper() in hay:
            return theta
    return None


def _thermal_vias(ctx, pcb) -> list[Finding]:
    out: list[Finding] = []
    for pad in getattr(pcb, "pads", []) or []:
        ref = pad.get("component") or ""
        if _prefix(ref) not in ("U", "Q") or not pad.get("net"):
            continue
        if geometry.pad_area_mm2(pcb, pad) < THERMAL_PAD_MIN_MM2:
            continue
        vias = geometry.vias_in_pad(pcb, pad)
        if len(vias) >= THERMAL_VIA_MIN:
            continue
        label = f"{ref}.{pad.get('number')}"
        out.append(Finding(
            code="REVIEW_THERMAL_VIA", severity=Severity.WARNING,
            message=(f"thermal pad {label} "
                     f"({geometry.pad_area_mm2(pcb, pad):.1f} mm², net "
                     f"{pad.get('net')!r}) carries {len(vias)} via(s) — "
                     f"below the {THERMAL_VIA_MIN}-via floor"),
            refs=[ref], anchors=[anchor("component", ref),
                                 anchor("pin", label)],
            confidence="heuristic",
            evidence={"source": "geometry",
                      "calc": {"vias": len(vias),
                               "floor": THERMAL_VIA_MIN,
                               "pad_area_mm2": round(
                                   geometry.pad_area_mm2(pcb, pad), 2)}},
            remediation=(f"stitch ≥{THERMAL_VIA_MIN} vias through the "
                         "exposed pad to the plane it dumps heat into"),
            fix_params={"kind": "add_thermal_vias", "pad": label,
                        "have": len(vias), "want": THERMAL_VIA_MIN}))
    return out


def _junction(ctx, pcb) -> list[Finding]:
    if ctx.facts is None:
        return []
    out: list[Finding] = []
    for fp in getattr(pcb, "footprints", []) or []:
        facts = ctx.facts.lookup(fp.value)
        if facts is None:
            continue
        p_fact = facts.get("power_dissipation")
        p = p_fact.best() if p_fact is not None else None
        if p is None or p <= 0:
            continue                       # no dissipation → no estimate
        theta_fact = facts.get("theta_ja")
        theta = theta_fact.best() if theta_fact is not None else None
        confidence = "datasheet_backed"
        assumptions = [f"Ta = {T_AMBIENT_C:g} °C ambient"]
        evidence: dict = {"source": "datasheet"}
        if theta is None:
            theta = _package_theta(fp.footprint_name)
            if theta is None:
                continue
            confidence = "heuristic"
            evidence = {"source": "heuristic_rule"}
            assumptions.append(
                f"θ_JA = {theta:g} K/W from the typical-package table "
                f"({fp.footprint_name})")
        else:
            evidence["datasheet"] = theta_fact.evidence()
        limit_fact = facts.get("t_j_max")
        limit = limit_fact.best() if limit_fact is not None else None
        if limit is None:
            limit = T_JUNCTION_MAX_DEFAULT_C
            assumptions.append(
                f"Tj(max) = {limit:g} °C industry default")
        tj = T_AMBIENT_C + p * theta
        evidence["calc"] = {"formula": "Tj = Ta + P*theta_ja",
                            "inputs": {"ta_c": T_AMBIENT_C, "p_w": p,
                                       "theta_ja": theta},
                            "results": {"tj_c": round(tj, 1),
                                        "limit_c": limit}}
        evidence["assumptions"] = assumptions
        common = dict(refs=[fp.designator],
                      anchors=[anchor("component", fp.designator)],
                      confidence=confidence, evidence=evidence)
        if tj > limit:
            out.append(Finding(
                code="REVIEW_THERMAL_JUNCTION", severity=Severity.WARNING,
                message=(f"{fp.designator} ({fp.value}): estimated "
                         f"Tj ≈ {tj:.0f} °C exceeds the {limit:g} °C limit "
                         f"(P = {p:g} W, θ_JA = {theta:g} K/W)"),
                remediation=("cut the dissipation, improve the copper/via "
                             "heat path, or pick a lower-θ package"),
                **common))
        else:
            out.append(Finding(
                code="REVIEW_THERMAL_JUNCTION", severity=Severity.INFO,
                message=(f"{fp.designator} ({fp.value}): estimated "
                         f"Tj ≈ {tj:.0f} °C (limit {limit:g} °C, "
                         f"P = {p:g} W, θ_JA = {theta:g} K/W)"),
                **common))
    return out


def run(ctx) -> list[Finding]:
    pcb = ctx.pcb
    if pcb is None:
        return []
    return _thermal_vias(ctx, pcb) + _junction(ctx, pcb)


register(Detector(name="pcb.thermal", family="pcb", run=run, rules=RULES))
