"""Connector ESD/TVS coverage review.

Every board-edge connector signal should see a TVS/ESD clamp somewhere on
its net (IEC 61000-4-2 events enter at the connector). Component roles come
from designator-prefix + library/value keywords (honestly ``heuristic`` — an
exotic part naming scheme evades it, which the confidence field says out loud).
"""

from __future__ import annotations

from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import topo

_MAX_NETS_LISTED = 8

RULES = (
    Rule(
        code="REVIEW_CONN_UNPROTECTED",
        title="Connector signal nets carry no ESD/TVS protection",
        explain=(
            "None of this connector's signal nets (non-power, non-ground, "
            "≥2 members) reaches a recognised TVS/ESD part. Board-edge "
            "interfaces are the primary ESD entry path; IEC 61000-4-2 events "
            "on unprotected pins reach the IC directly. Detection is by "
            "designator/library/value keywords, so an unrecognised protection "
            "part is a false positive — waive it or rename the part."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="IEC 61000-4-2 (contact/air ESD enters at board-edge connectors)"),
)


def run(ctx: topo.ReviewCtx) -> list[Finding]:
    out: list[Finding] = []
    tvs_nets: set[str] = set()
    for comp in ctx.sch.components:
        # Only FITTED clamps protect anything: a DNP TVS leaves the net
        # exposed on the shipped board (topo.is_fitted).
        if topo.is_tvs(comp) and topo.is_fitted(comp):
            for net in ctx.comp_nets.get(comp.designator, []):
                tvs_nets.add(net.stable_id)

    for comp in ctx.sch.components:
        if not topo.is_connector(comp) or topo.is_power_symbol(comp):
            continue
        if topo.is_tvs(comp):
            continue
        ref = comp.designator
        signal_nets = [
            n for n in ctx.comp_nets.get(ref, [])
            if not topo.net_is_ground(n) and not topo.net_is_power(n)
            and len(n.members) >= 2
        ]
        if not signal_nets:
            continue                    # power-only connector: not this rule
        unprotected = [n for n in signal_nets if n.stable_id not in tvs_nets]
        if not unprotected:
            continue
        names = sorted(n.name for n in unprotected)
        shown = ", ".join(names[:_MAX_NETS_LISTED]) + (
            f", … (+{len(names) - _MAX_NETS_LISTED})"
            if len(names) > _MAX_NETS_LISTED else "")
        out.append(Finding(
            code="REVIEW_CONN_UNPROTECTED", severity=Severity.WARNING,
            message=(f"connector {ref}: {len(unprotected)} of "
                     f"{len(signal_nets)} signal net(s) have no ESD/TVS "
                     f"part: {shown}"),
            refs=[ref] + names[:_MAX_NETS_LISTED],
            anchors=[anchor("component", ref)]
                    + [anchor("net", n) for n in names[:_MAX_NETS_LISTED]],
            confidence="heuristic", evidence={"source": "heuristic_rule"},
            remediation=("clamp exposed signals with a TVS/ESD array near "
                         "the connector, or waive if the interface is "
                         "internal-only"),
            fix_params={"kind": "add_esd", "connector": ref,
                        "nets": names[:_MAX_NETS_LISTED]}))
    return out


register(Detector(name="signal.protection", family="signal", run=run,
                  rules=RULES))
