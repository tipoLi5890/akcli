"""Decoupling-capacitor placement review: cap-to-pin distance on power nets.

A decoupling cap only works close to the pin it decouples — loop inductance
grows with distance. Roles come from designator prefixes (C decouples U/Q),
so the judgement is honestly ``heuristic``; the distance itself is measured
geometry carried in the evidence.
"""

from __future__ import annotations

from ....classify import (
    implied_voltage,
    is_ground as _is_ground,
    is_power as _is_power,
    refdes_prefix as _prefix,
)
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import geometry
from ...tables import DECAP_MAX_MM

_IC_PREFIXES = ("U", "Q")

RULES = (
    Rule(
        code="REVIEW_DECAP_DISTANCE",
        title="Decoupling capacitor sits far from the pin it decouples",
        explain=(
            "The capacitor's power-net pad is farther than "
            f"{DECAP_MAX_MM:g} mm from every IC pad on that net. Supply-loop "
            "inductance scales with the loop area, so a far cap stops "
            "decoupling at exactly the frequencies it was fitted for — "
            "standard high-speed layout guidance keeps the 100 nF within a "
            "few mm of its pin. Roles are inferred from designator prefixes "
            "(heuristic); the measured distance rides in the evidence."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="supply-loop inductance vs decoupling effectiveness "
                  "(high-speed layout guidance)"),
)


def _is_power_net(name: str | None) -> bool:
    return bool(name) and not _is_ground(name) and (
        _is_power(name) or implied_voltage(name) is not None)


def run(ctx) -> list[Finding]:
    pcb = ctx.pcb
    if pcb is None:
        return []
    pads = getattr(pcb, "pads", []) or []
    out: list[Finding] = []
    seen: set[str] = set()
    for cap_pad in pads:
        ref = cap_pad.get("component") or ""
        net = cap_pad.get("net")
        if _prefix(ref) != "C" or ref in seen or not _is_power_net(net):
            continue
        ic_pads = [p for p in pads
                   if p.get("net") == net
                   and _prefix(p.get("component") or "") in _IC_PREFIXES]
        if not ic_pads:
            continue
        seen.add(ref)
        nearest = min(ic_pads,
                      key=lambda p: geometry.pad_distance_mm(pcb, cap_pad, p))
        dist = geometry.pad_distance_mm(pcb, cap_pad, nearest)
        if dist <= DECAP_MAX_MM:
            continue
        target = f"{nearest.get('component')}.{nearest.get('number')}"
        out.append(Finding(
            code="REVIEW_DECAP_DISTANCE", severity=Severity.WARNING,
            message=(f"{ref} on {net!r} is {dist:.1f} mm from the nearest "
                     f"IC pad ({target}) — beyond the {DECAP_MAX_MM:g} mm "
                     "decoupling radius"),
            refs=[ref, nearest.get("component") or "", net],
            anchors=[anchor("component", ref),
                     anchor("pin", target), anchor("net", net)],
            confidence="heuristic",
            evidence={"source": "geometry",
                      "calc": {"distance_mm": round(dist, 2),
                               "limit_mm": DECAP_MAX_MM,
                               "nearest_pad": target}},
            remediation=(f"move {ref} next to {target}, or waive if this "
                         "cap is bulk (not decoupling)"),
            fix_params={"kind": "move_decap", "cap": ref,
                        "target_pad": target,
                        "distance_mm": round(dist, 2)}))
    return out


register(Detector(name="pcb.decap", family="pcb", run=run, rules=RULES))
