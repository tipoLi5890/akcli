"""Power-entry protection review: input fusing + reverse-polarity.

A rail that arrives from OUTSIDE the board (battery, DC jack, VBUS) is walked
as a series chain (crossing only fuses, diodes and inductors — never
resistors or capacitors, so the walk cannot wander into dividers or
decoupling) and judged for a series fuse/PTC and a reverse-polarity element.
Fuse sizing is judged through ``calc fuse-derating`` (Littelfuse Fuseology,
IEC 60127 R10 ladder) and only when the facts store records the continuous
load current — never from a guessed load.

Honesty: entry nets are recognised by NAME tokens and parts by
designator/library keywords, so an unrecognised scheme (P-FET ideal-diode
controllers, exotic naming) is a false positive to waive — every finding says
so. Series-diode ORIENTATION is not checked (the schematic does not say which
chain end is the source), and that assumption is stated on the finding.
"""

from __future__ import annotations

import re

from .... import classify
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import tables, topo

# A power-entry SEED is either the token as the whole name (``VBAT``,
# ``+VIN``, ``VBUS2``) or a token-carrying name the shared rail classifier
# recognises as power (``VBUS_5V``). A token buried in a longer signal name
# (``VBUS_SENSE``, ``VBAT_STAT``) is a derived signal, not an entry — those
# nets are still covered when the walk reaches them from the real entry.
# A rail STAGE name (``VBUS_FUSED``, ``VBAT_PROT`` — see
# :func:`akcli.classify.is_stage_name`) is not a seed either: it is the same
# rail *after* a protector, and seeding there reports the very fuse/diode
# upstream of it as missing whenever the chain walk cannot cross the part
# between them (a load switch, an ideal-diode controller).
_ENTRY_FULL_RX = re.compile(
    r"^[+\-]?" + tables.POWER_ENTRY_RX_TOKENS + r"\d*$", re.IGNORECASE)
_ENTRY_TOKEN_RX = re.compile(
    r"(^|[_./+\-])" + tables.POWER_ENTRY_RX_TOKENS + r"\d*([_./+\-]|$)",
    re.IGNORECASE)

_ORIENTATION_NOTE = (
    "series-diode orientation is not checked — the schematic does not mark "
    "which chain end is the source")

RULES = (
    Rule(
        code="REVIEW_FUSE_MISSING",
        title="Power-entry rail has no series fuse/PTC",
        explain=(
            "A net named like a board input (VBAT/VIN/VBUS/DCIN…) reaches "
            "the board with no fuse or resettable PTC in its series path. A "
            "hard fault downstream then dumps the full source energy into "
            "the board. Detection is by net-name token + designator/library "
            "keywords — an unrecognised protector or an output-only rail is "
            "a false positive to waive."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="IEC 60127 / Littelfuse Fuseology: fault protection at the "
                  "power entry"),
    Rule(
        code="REVIEW_FUSE_UNRATED",
        title="Fuse value does not parse as a current rating",
        explain=(
            "A fuse's Value field carries no parseable current (e.g. bare "
            "'Fuse'), so its sizing cannot be audited. Record the rating "
            "(e.g. '500mA') in the Value field."),
        default_severity="note", confidence="deterministic", version="1",
        reference=None),
    Rule(
        code="REVIEW_FUSE_UNDERSIZED",
        title="Fuse rating below the derated continuous-load floor",
        explain=(
            "Rating < I_load/(derate·temp_factor) per `akcli calc "
            "fuse-derating` (Littelfuse Fuseology ≤75 % continuous; IEC "
            "60127 R10 ladder). The continuous load current comes from the "
            "datasheet facts store — this rule stays silent when no i_load "
            "fact is recorded rather than guessing the load."),
        default_severity="warning", confidence="datasheet_backed", version="1",
        reference="Littelfuse Fuseology selection guide (≤75 % continuous); "
                  "IEC 60127 R10 ratings"),
    Rule(
        code="REVIEW_REVPOL_UNPROTECTED",
        title="Power-entry rail has no reverse-polarity protection",
        explain=(
            "No series diode (and no fuse-plus-shunt-diode crowbar) is found "
            "in the entry rail's path. A reversed source then drives every "
            "downstream part backwards. P-FET/ideal-diode schemes are NOT "
            "recognised by this topology pattern — waive the finding when "
            "one is fitted, or when the connector is polarised by "
            "mechanical design."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="TI SLVA139 (reverse-battery protection topologies)"),
    Rule(
        code="REVIEW_REVPOL_SHUNT_NO_FUSE",
        title="Shunt reverse diode without an upstream fuse",
        explain=(
            "A reverse shunt (crowbar) diode clamps the rail, but the chain "
            "carries no series fuse — on a reversed source the diode "
            "conducts the full short-circuit current with nothing to open "
            "the path, and fails (often open) before protecting anything. "
            "The crowbar scheme only works as fuse + diode together."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="TI SLVA139 (shunt-diode scheme requires a series fuse)"),
)


_CURRENT_RX = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([GMkKmuµ]?)A(?![0-9A-Za-z])")


def _parse_current(text: str | None) -> float | None:
    """The amps rating inside a fuse Value (``1A`` / ``500mA`` /
    ``PTC 750mA``), or ``None`` — a value with no ampere token is not a
    rating."""
    if not text:
        return None
    m = _CURRENT_RX.search(str(text))
    if not m:
        return None
    return topo.parse_value(m.group(1) + m.group(2))


def _entry_names(net) -> bool:
    from ....checks.power import net_candidate_names
    names = net_candidate_names(net)
    if any(_ENTRY_FULL_RX.match(n or "") for n in names):
        return True
    if any(classify.is_stage_name(n) for n in names):
        return False                      # downstream stage, not the entry
    return (topo.net_is_power(net)
            and any(_ENTRY_TOKEN_RX.search(n or "") for n in names))


def _chain_from(ctx: topo.ReviewCtx, seed) -> tuple[list, list[str]]:
    """(nets, crossed part refs) of the series chain containing ``seed``.

    Crosses only FITTED fuse/diode/inductor two-terminal parts, never into
    ground: a DNP protector protects nothing, so it must not silence
    ``REVIEW_FUSE_MISSING``/``REVIEW_REVPOL_UNPROTECTED`` (topo.is_fitted).
    """
    nets = [seed]
    seen = {seed.stable_id}
    crossed: list[str] = []
    frontier = [seed]
    while frontier and len(nets) < tables.POWER_CHAIN_MAX_NETS:
        net = frontier.pop(0)
        for ref, _pin in sorted(net.members):
            comp = ctx.comps.get(ref)
            if comp is None or topo.is_power_symbol(comp):
                continue
            if not topo.is_fitted(comp):
                continue                  # DNP: no series path, no protection
            if not (topo.is_fuse(comp) or topo.is_diode(comp)
                    or topo.is_inductor(comp)):
                continue
            on = topo.other_net(ctx, ref, net)
            if on is None or topo.net_is_ground(on):
                continue
            if ref not in crossed:
                crossed.append(ref)
            if on.stable_id not in seen:
                seen.add(on.stable_id)
                nets.append(on)
                frontier.append(on)
    return nets, crossed


def _shunt_reverse_diodes(ctx: topo.ReviewCtx, chain_nets) -> list[str]:
    """Fitted diodes bridging a chain net to ground (the crowbar element)."""
    out = []
    for net in chain_nets:
        for ref, _pin in net.members:
            comp = ctx.comps.get(ref)
            if comp is None or not topo.is_diode(comp) or not topo.is_fitted(comp):
                continue
            on = topo.other_net(ctx, ref, net)
            if on is not None and topo.net_is_ground(on):
                out.append(ref)
    return sorted(set(out))


def run(ctx: topo.ReviewCtx) -> list[Finding]:
    from ....calc import compute

    out: list[Finding] = []

    # ---- power-entry chains: fuse presence + reverse-polarity ---------- #
    walked: set[str] = set()
    for net in sorted((n for n in ctx.sch.nets if _entry_names(n)),
                      key=lambda n: n.name or ""):
        if net.stable_id in walked or topo.net_is_ground(net):
            continue
        chain, crossed = _chain_from(ctx, net)
        walked.update(n.stable_id for n in chain)
        if sum(len(n.members) for n in chain) < 2:
            continue                     # nothing actually connected
        series_fuses = [r for r in crossed if topo.is_fuse(ctx.comps[r])]
        series_diodes = [r for r in crossed
                         if topo.is_diode(ctx.comps[r])
                         and not topo.is_fuse(ctx.comps[r])]
        shunts = _shunt_reverse_diodes(ctx, chain)
        anchors = [anchor("net", net.name)]
        if not series_fuses:
            out.append(Finding(
                code="REVIEW_FUSE_MISSING", severity=Severity.WARNING,
                message=(f"power-entry net {net.name!r}: no series fuse/PTC "
                         "in its path"),
                refs=[net.name], anchors=anchors, confidence="heuristic",
                evidence={"source": "topology"},
                remediation=("fuse the entry rail (fuse or resettable PTC), "
                             "or waive if this rail is a board OUTPUT or is "
                             "protected upstream"),
                fix_params={"kind": "add_fuse", "net": net.name}))
        if series_diodes:
            pass                          # series element present: protected
        elif shunts and series_fuses:
            pass                          # crowbar scheme: fuse + shunt diode
        elif shunts:
            out.append(Finding(
                code="REVIEW_REVPOL_SHUNT_NO_FUSE", severity=Severity.WARNING,
                message=(f"power-entry net {net.name!r}: shunt reverse "
                         f"diode {', '.join(shunts)} has no upstream series "
                         "fuse — the crowbar scheme cannot open the path"),
                refs=[net.name] + shunts,
                anchors=anchors + [anchor("component", r) for r in shunts],
                confidence="heuristic", evidence={"source": "topology"},
                remediation="add a series fuse ahead of the shunt diode",
                fix_params={"kind": "add_fuse", "net": net.name}))
        else:
            out.append(Finding(
                code="REVIEW_REVPOL_UNPROTECTED", severity=Severity.WARNING,
                message=(f"power-entry net {net.name!r}: no reverse-polarity "
                         "protection found (no series diode, no fuse+shunt "
                         "crowbar)"),
                refs=[net.name], anchors=anchors, confidence="heuristic",
                evidence={"source": "topology",
                          "assumptions": [_ORIENTATION_NOTE,
                                          "P-FET/ideal-diode schemes are not "
                                          "recognised — waive if fitted"]},
                remediation=("add a series (Schottky) diode or a P-FET "
                             "reverse-protection stage, or waive for a "
                             "polarised connector"),
                fix_params={"kind": "add_reverse_protection",
                            "net": net.name}))

    # ---- fuse sizing (every fuse, chain membership not required) ------- #
    for comp in ctx.sch.components:
        if topo.is_power_symbol(comp) or not topo.is_fuse(comp):
            continue
        ref = comp.designator
        rating = _parse_current(comp.value)
        if rating is None:
            out.append(Finding(
                code="REVIEW_FUSE_UNRATED", severity=Severity.NOTE,
                message=(f"fuse {ref}: value {comp.value!r} carries no "
                         "parseable current rating — sizing unauditable"),
                refs=[ref], anchors=[anchor("component", ref)],
                confidence="deterministic", status="insufficient_evidence",
                evidence={"source": "topology"},
                remediation="record the rating in the Value field, e.g. "
                            "'500mA'"))
            continue
        fact = ctx.fact_for(ref, "i_load")
        i_load = fact.best() if fact is not None else None
        if i_load is None or i_load <= 0:
            continue                     # no recorded load: no guess
        env = compute("fuse-derating", {"i_load": i_load})
        need = env["results"]["i_rating_min"]["value"]
        suggested = env["results"]["suggested"]["value"]
        if rating < need:
            out.append(Finding(
                code="REVIEW_FUSE_UNDERSIZED", severity=Severity.WARNING,
                message=(f"fuse {ref}={comp.value} < derated floor "
                         f"{need:.3g} A for the recorded continuous load "
                         f"{i_load:.3g} A — it will fatigue or nuisance-open"),
                refs=[ref], anchors=[anchor("component", ref)],
                confidence="datasheet_backed",
                evidence={"source": "datasheet",
                          "datasheet": fact.evidence(), "calc": env},
                remediation=(f"use the next IEC 60127 R10 rating ≥ "
                             f"{need:.3g} A"
                             + (f" (suggested {suggested:g} A)"
                                if suggested else "")),
                fix_params={"kind": "resize_fuse", "ref": ref,
                            "i_rating_min": round(need, 4),
                            "suggested": suggested}))
    return out


register(Detector(name="signal.power_protect", family="signal", run=run,
                  rules=RULES))
