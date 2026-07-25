"""Op-amp gain-topology review (M2 closure).

Recognises op-amp units by their ``+``/``-`` input pin names, classifies the
feedback topology (non-inverting / inverting / unity buffer / open loop) and
computes the DC gain from the resistor values. Identification is pin-name
based and therefore honestly ``heuristic`` — comparators share the pin shape
and legitimately run open-loop, which the finding text says. Per-part
behavioral verification (GBW, slew) arrives with the SPICE milestone.
"""

from __future__ import annotations

from ....model import PinType
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import topo

_PLUS_NAMES = frozenset({"+", "IN+", "VIN+", "INP", "IN_P"})
_MINUS_NAMES = frozenset({"-", "IN-", "VIN-", "INN", "IN_N"})
_OUT_NAMES = frozenset({"OUT", "OUTPUT", "VO", "VOUT", "~"})

RULES = (
    Rule(
        code="REVIEW_OPAMP_GAIN",
        title="Op-amp closed-loop gain computed from the feedback network",
        explain=(
            "A resistor from the output back to the inverting input closes "
            "the loop. With a ground leg R_g the stage is non-inverting, "
            "G = 1 + R_f/R_g; with the + input at a reference and an input "
            "resistor R_in it is inverting, G = −R_f/R_in; output wired "
            "straight to − is a unity buffer. The computed gain is an "
            "observation to check against the stage's intent."),
        default_severity="info", confidence="heuristic", version="1",
        reference="ideal op-amp closed-loop gain: G = 1 + Rf/Rg; G = -Rf/Rin"),
    Rule(
        code="REVIEW_OPAMP_NO_FEEDBACK",
        title="Op-amp inverting input has no feedback path",
        explain=(
            "No resistor (or direct wire) connects the output to the "
            "inverting input: the stage runs open-loop and rails at the "
            "supplies. Deliberate for a comparator — but running an actual "
            "op-amp open-loop is almost always a missing feedback part. "
            "Identification is pin-name based (+/−), which cannot tell an "
            "op-amp from a comparator; waive per part if this is a "
            "comparator."),
        default_severity="warning", confidence="heuristic", version="1",
        reference=None),
)


def _units(comp):
    """``{owner_part_id: [pins]}`` — one op-amp package is several units."""
    units: dict[int, list] = {}
    for p in comp.pins:
        units.setdefault(p.owner_part_id, []).append(p)
    return units


def _classify_pins(pins):
    """``(plus, minus, out)`` pin objects for an op-amp-shaped unit, else None."""
    plus = minus = out = None
    for p in pins:
        name = (p.name or "").strip().upper()
        if name in _PLUS_NAMES:
            plus = p
        elif name in _MINUS_NAMES:
            minus = p
    if plus is None or minus is None:
        return None
    for p in pins:
        if p is plus or p is minus:
            continue
        name = (p.name or "").strip().upper()
        if p.electrical_type == PinType.OUTPUT or name in _OUT_NAMES:
            out = p
            break
    return (plus, minus, out) if out is not None else None


def _resistors_between(ctx, net_a, net_b) -> list[str]:
    """FITTED resistors bridging the two nets — a DNP feedback resistor leaves
    the amplifier open-loop, so it must not satisfy the feedback-path search
    (topo.is_fitted)."""
    out = []
    for ref, _pin in net_a.members:
        comp = ctx.comps.get(ref)
        if comp is None or not topo.is_resistor(comp) or not topo.is_fitted(comp):
            continue
        if topo.other_net(ctx, ref, net_a) is net_b:
            out.append(ref)
    return sorted(set(out))


def run(ctx: topo.ReviewCtx) -> list[Finding]:
    out: list[Finding] = []
    for comp in ctx.sch.components:
        if topo.is_power_symbol(comp) or len(ctx.comp_nets.get(
                comp.designator, [])) < 3:
            continue
        ref = comp.designator
        for unit_id, pins in sorted(_units(comp).items()):
            trio = _classify_pins(pins)
            if trio is None:
                continue
            plus, minus, o = trio
            n_plus = ctx.net_of.get((ref, str(plus.number)))
            n_minus = ctx.net_of.get((ref, str(minus.number)))
            n_out = ctx.net_of.get((ref, str(o.number)))
            if n_minus is None or n_out is None:
                continue
            anchors = [anchor("component", ref),
                       anchor("pin", f"{ref}.{minus.number}"),
                       anchor("pin", f"{ref}.{o.number}")]
            unit_tag = f"{ref}" + (f" unit {unit_id}" if unit_id != 1 else "")
            if n_out is n_minus:
                out.append(Finding(
                    code="REVIEW_OPAMP_GAIN", severity=Severity.INFO,
                    message=f"{unit_tag}: unity-gain buffer (output wired to −)",
                    refs=[ref], anchors=anchors, confidence="heuristic",
                    evidence={"source": "topology",
                              "calc": {"formula": "G = 1 (voltage follower)",
                                       "results": {"gain": 1.0}}}))
                continue
            rf_refs = _resistors_between(ctx, n_minus, n_out)
            if not rf_refs:
                out.append(Finding(
                    code="REVIEW_OPAMP_NO_FEEDBACK", severity=Severity.WARNING,
                    message=(f"{unit_tag}: no feedback path from output "
                             f"({n_out.name!r}) to − ({n_minus.name!r}) — "
                             "open loop (fine for a comparator, a bug for "
                             "an amplifier)"),
                    refs=[ref], anchors=anchors, confidence="heuristic",
                    evidence={"source": "topology"},
                    remediation=("add the feedback network, or waive if this "
                                 "part is a comparator")))
                continue
            rf_ref = rf_refs[0]
            rf = topo.parse_value(ctx.comps[rf_ref].value)
            if rf is None or rf <= 0:
                continue                 # unverifiable gain: silence, no guess
            # ground leg on the − node → non-inverting
            rg_refs = []
            for r, _p in n_minus.members:
                cmp2 = ctx.comps.get(r)
                if cmp2 is None or not topo.is_resistor(cmp2) or r == rf_ref:
                    continue
                on = topo.other_net(ctx, r, n_minus)
                if on is not None and topo.net_is_ground(on):
                    rg_refs.append(r)
            if rg_refs:
                rg_ref = sorted(set(rg_refs))[0]
                rg = topo.parse_value(ctx.comps[rg_ref].value)
                if rg is None or rg <= 0:
                    continue
                gain = 1.0 + rf / rg
                out.append(Finding(
                    code="REVIEW_OPAMP_GAIN", severity=Severity.INFO,
                    message=(f"{unit_tag}: non-inverting, G = 1 + "
                             f"{rf_ref}/{rg_ref} = {gain:.4g}"),
                    refs=[ref, rf_ref, rg_ref], anchors=anchors,
                    confidence="heuristic",
                    evidence={"source": "topology",
                              "calc": {"formula": "G = 1 + Rf/Rg",
                                       "inputs": {"rf": rf, "rg": rg},
                                       "results": {"gain": round(gain, 4)}}},
                    remediation="confirm the gain suits the stage's intent"))
                continue
            # input resistor into the − node → inverting (virtual ground on +)
            rin_refs = []
            for r, _p in n_minus.members:
                cmp2 = ctx.comps.get(r)
                if cmp2 is None or not topo.is_resistor(cmp2) or r == rf_ref:
                    continue
                on = topo.other_net(ctx, r, n_minus)
                if on is not None and on is not n_out \
                        and not topo.net_is_ground(on):
                    rin_refs.append(r)
            plus_ref_ok = (n_plus is not None and (
                topo.net_is_ground(n_plus)
                or topo.net_implied_voltage(n_plus) is not None))
            if rin_refs and plus_ref_ok:
                rin_ref = sorted(set(rin_refs))[0]
                rin = topo.parse_value(ctx.comps[rin_ref].value)
                if rin is None or rin <= 0:
                    continue
                gain = -rf / rin
                out.append(Finding(
                    code="REVIEW_OPAMP_GAIN", severity=Severity.INFO,
                    message=(f"{unit_tag}: inverting, G = −{rf_ref}/{rin_ref}"
                             f" = {gain:.4g}"),
                    refs=[ref, rf_ref, rin_ref], anchors=anchors,
                    confidence="heuristic",
                    evidence={"source": "topology",
                              "calc": {"formula": "G = -Rf/Rin",
                                       "inputs": {"rf": rf, "rin": rin},
                                       "results": {"gain": round(gain, 4)}}},
                    remediation="confirm the gain suits the stage's intent"))
    return out


register(Detector(name="signal.opamp", family="signal", run=run, rules=RULES))
