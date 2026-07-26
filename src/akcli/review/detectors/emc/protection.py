"""TVS placement review (EMC batch 1): the clamp must sit at the connector.

An ESD strike follows the trace until it meets the clamp — every millimetre
before the TVS is unprotected inductance the transient rides over. Roles
come from designator/keyword conventions (heuristic); the distance is
measured.
"""

from __future__ import annotations

from ....classify import is_connector, is_tvs
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import geometry
from ...tables import EMC_TVS_CONN_MAX_MM

RULES = (
    Rule(
        code="REVIEW_EMC_TVS_FAR",
        title="TVS/ESD clamp sits far from the connector it protects",
        explain=(
            "The clamp's nearest connector pad is farther than "
            f"{EMC_TVS_CONN_MAX_MM:g} mm: the strike travels that much "
            "unclamped trace (plus its inductance) before the TVS acts, "
            "and the let-through spikes accordingly. Place clamps at the "
            "connector pins, before anything else on the net."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="IEC 61000-4-2 let-through vs clamp placement"),
)


def _is_tvs_fp(fp) -> bool:
    return is_tvs(fp.designator, value=fp.value, footprint=fp.footprint_name)


def _is_conn_fp(fp) -> bool:
    return is_connector(fp.designator, footprint=fp.footprint_name,
                        value=fp.value)


def run(ctx) -> list[Finding]:
    pcb = ctx.pcb
    if pcb is None:
        return []
    fps = getattr(pcb, "footprints", []) or []
    tvs_refs = {f.designator for f in fps if _is_tvs_fp(f)}
    conn_refs = {f.designator for f in fps
                 if _is_conn_fp(f) and f.designator not in tvs_refs}
    if not tvs_refs or not conn_refs:
        return []
    pads = getattr(pcb, "pads", []) or []
    conn_pads = [p for p in pads if p.get("component") in conn_refs]
    if not conn_pads:
        return []
    out: list[Finding] = []
    for ref in sorted(tvs_refs):
        tvs_pads = [p for p in pads if p.get("component") == ref]
        if not tvs_pads:
            continue
        best, best_pad = None, None
        for tp in tvs_pads:
            for cp in conn_pads:
                d = geometry.pad_distance_mm(pcb, tp, cp)
                if best is None or d < best:
                    best, best_pad = d, cp
        if best is None or best <= EMC_TVS_CONN_MAX_MM:
            continue
        target = f"{best_pad.get('component')}.{best_pad.get('number')}"
        out.append(Finding(
            code="REVIEW_EMC_TVS_FAR", severity=Severity.WARNING,
            message=(f"TVS {ref} sits {best:.1f} mm from the nearest "
                     f"connector pad ({target}) — beyond the "
                     f"{EMC_TVS_CONN_MAX_MM:g} mm clamp radius"),
            refs=[ref, best_pad.get("component") or ""],
            anchors=[anchor("component", ref), anchor("pin", target)],
            confidence="heuristic",
            evidence={"source": "geometry",
                      "calc": {"distance_mm": round(best, 1),
                               "limit_mm": EMC_TVS_CONN_MAX_MM}},
            remediation=f"move {ref} to the connector pins",
            fix_params={"kind": "move_tvs", "tvs": ref,
                        "target_pad": target}))
    return out


register(Detector(name="emc.protection", family="emc", run=run, rules=RULES))
