"""Enable/shutdown pin review (M3, sequencing-lite).

Scoped to what topology alone can prove: an EN/SHDN/CE pin whose net
contains nothing else is floating — the part's start-up state then hangs on
an internal pull-up/down nobody verified. Full sequencing analysis (who
enables whom, in what order) arrives with the power-tree milestone (M7).
"""

from __future__ import annotations

from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import topo

# Enable-family pin names, matched after stripping KiCad's overbar markup
# (~{EN}) and a leading active-low "n" (vendor datasheet conventions).
_EN_NAMES = frozenset({"EN", "ENABLE", "CE", "SHDN", "SD", "INH", "RUN",
                       "EN1", "EN2", "ON"})

RULES = (
    Rule(
        code="REVIEW_EN_FLOATING",
        title="Enable/shutdown pin is floating",
        explain=(
            "An EN/SHDN/CE-named pin's net has no other member: whether the "
            "part ever starts depends on an internal pull nobody verified "
            "(many regulators float OFF; some float ON — datasheets "
            "disagree). Tie it to a rail, a control line, or waive with the "
            "datasheet's default-state row as evidence."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="regulator EN default states differ per part — never float"),
)


def _en_name(raw: str | None) -> bool:
    name = (raw or "").strip().upper()
    name = name.replace("~{", "").replace("}", "").replace("~", "")
    if name.startswith("N") and name[1:] in _EN_NAMES:
        return True
    return name in _EN_NAMES


def run(ctx: topo.ReviewCtx) -> list[Finding]:
    out: list[Finding] = []
    for comp in ctx.sch.components:
        ref = comp.designator
        if topo.is_power_symbol(comp) or len(ctx.comp_nets.get(ref, [])) < 3:
            continue
        for pin in comp.pins:
            if not _en_name(pin.name):
                continue
            net = ctx.net_of.get((ref, str(pin.number)))
            if net is None:
                continue
            # Only FITTED neighbours tie the pin: a DNP pull resistor leaves
            # the enable floating on the shipped board (topo.is_fitted).
            tied = [(r, p) for r, p in net.members
                    if (r, str(p)) != (ref, str(pin.number))
                    and not topo.is_dnp(ctx.comps.get(r))]
            if tied:
                continue                       # driven / tied: fine here
            out.append(Finding(
                code="REVIEW_EN_FLOATING", severity=Severity.WARNING,
                message=(f"{ref}.{pin.number} ({pin.name}) is floating — "
                         "the part's on/off default is whatever its internal "
                         "pull does"),
                refs=[ref],
                anchors=[anchor("component", ref),
                         anchor("pin", f"{ref}.{pin.number}")],
                confidence="heuristic", evidence={"source": "topology"},
                remediation=("tie the enable to its rail or controller, or "
                             "waive citing the datasheet's default state"),
                fix_params={"kind": "tie_enable", "pin": f"{ref}.{pin.number}"}))
    return out


register(Detector(name="validation.enable_pin", family="validation", run=run,
                  rules=RULES))
