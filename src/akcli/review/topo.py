"""Topology recognition over the normalized model — the review layer's only
new "analysis primitive" layer.

Everything here is format-agnostic: it consumes :class:`~..model.Schematic`
(KiCad or Altium alike) and NEVER parses files. Rail/ground semantics AND
part-role classification (crystal / connector / TVS / refdes prefix) are
imported from :mod:`..classify`, the leaf module the ``checks`` layer uses
too, so the review layer cannot drift from the check layer (single source of
truth).

Discipline: helpers return ``None`` rather than guess — a value that does not
parse, or a net without an implied voltage, propagates as *absence* and the
detector downgrades to ``insufficient_evidence`` instead of fabricating a
number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import classify
from ..calc.registry import CalcError
from ..calc.si import parse_value as _si_parse_value
from ..checks.power import _net_candidate_names
from ..model import Component, Net, Schematic
from . import tables

# --------------------------------------------------------------------------- #
# component classification (delegated to ..classify — one implementation)
# --------------------------------------------------------------------------- #
_is_ground = classify.is_ground
_is_power = classify.is_power

#: Leading alpha run of a refdes, upper-cased. Kept as a review-layer name for
#: the many call sites; the implementation is the shared one (which also
#: strips the ``$`` of a synthesized designator, so undesignated parts are
#: classified instead of silently skipped).
ref_prefix = classify.refdes_prefix


def _lib(comp: Component) -> str:
    return (comp.library_ref or "").lower()


def is_resistor(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in tables.RESISTOR_PREFIXES
            or _lib(comp).endswith(":r") or ":r_" in _lib(comp))


def is_capacitor(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in tables.CAPACITOR_PREFIXES
            or _lib(comp).endswith(":c") or ":c_" in _lib(comp))


def is_crystal(comp: Component) -> bool:
    """Crystal/resonator: library or value evidence first, ``Y``/``X`` prefix
    as the fallback (see :func:`akcli.classify.is_crystal`)."""
    return classify.is_crystal(comp.designator, comp.library_ref, comp.value)


def is_connector(comp: Component) -> bool:
    """Connector/header: library evidence first, prefix as the fallback — a
    part whose library or value says *crystal* is never a connector."""
    return classify.is_connector(comp.designator, comp.library_ref,
                                 comp.footprint, comp.value)


def is_tvs(comp: Component) -> bool:
    return classify.is_tvs(comp.designator, comp.library_ref, comp.value)


def is_fuse(comp: Component) -> bool:
    hay = f"{_lib(comp)} {(comp.value or '').lower()}"
    return (ref_prefix(comp.designator) in tables.FUSE_PREFIXES
            or any(k in hay for k in tables.FUSE_KEYWORDS))


def is_diode(comp: Component) -> bool:
    """Rectifier/Schottky diode — excludes TVS parts and LEDs (both often
    carry the D prefix but are shunt clamps / indicators, not a series
    reverse-polarity element)."""
    lib = _lib(comp)
    if is_tvs(comp) or "led" in lib:
        return False
    hay = f"{lib} {(comp.value or '').lower()}"
    return (ref_prefix(comp.designator) in tables.DIODE_PREFIXES
            or lib.endswith(":d") or ":d_" in lib
            or any(k in hay for k in tables.DIODE_KEYWORDS))


def is_inductor(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in {"L", "FB"}
            or _lib(comp).endswith(":l") or ":l_" in _lib(comp)
            or "inductor" in _lib(comp) or "ferrite" in _lib(comp))


def is_power_symbol(comp: Component) -> bool:
    """Power ports / PWR_FLAG pseudo-components (``#``-prefixed references)."""
    return (comp.designator or "").startswith("#")


def is_fitted(comp: Component | None) -> bool:
    """True when a component is actually populated (``dnp`` is not set).

    Every **presence-satisfier** search — "a pull-up exists", "the crystal has
    load caps", "a fuse sits in the entry path", "this connector net reaches a
    TVS" — must go through this: a Do-Not-Populate part is drawn but never
    fitted, so it satisfies nothing electrically. BOM/enumeration rules
    deliberately keep seeing DNP parts (that is what ``bom_policy`` classes are
    for) and must NOT call this.
    """
    return comp is not None and not comp.dnp


def is_dnp(comp: Component | None) -> bool:
    """True only for a component that is present AND marked Do-Not-Populate.

    The inverse of :func:`is_fitted` for *unknown* parts: an unresolved
    reference is not evidence of a DNP part, so it stays "fitted" here.
    """
    return comp is not None and comp.dnp


# --------------------------------------------------------------------------- #
# value parsing (engineering notation incl. infix style: 4k7, 1R0, 100n, 10uF)
# --------------------------------------------------------------------------- #
# Unit tail a schematic Value carries but SI parsing does not want
# ("10uF" -> "10u", "4k7Ω" -> "4k7").
_UNIT_TAIL_RX = re.compile(r"\s*(?:F|H|OHMS?|Ω)\s*$", re.IGNORECASE)
# Unambiguous upper-case multiplier letters (Altium-style "10U"/"100N"/"4P7")
# folded to their SI spelling. ``M``/``m`` is deliberately NOT folded — that
# pair is mega vs milli, the one place case carries meaning. ``Ω`` is the IEC
# 60062 decimal marker, same role as ``R``.
_LETTER_FOLD = str.maketrans({"U": "u", "N": "n", "P": "p",
                              "Ω": "R", "μ": "µ"})


def parse_value(text: str | None) -> float | None:
    """Engineering-notation component value, or ``None`` — never a guess.

    Accepts ``4700`` / ``4.7k`` / ``4k7`` / ``1R0`` / ``100n`` / ``10uF`` /
    ``2.2µF`` / ``0R`` / ``10U`` (Altium-style upper case).

    The number crunching is :func:`akcli.calc.si.parse_value` — the same
    parser the calculators use — so a value the CLI can compute with is a
    value the review detectors can see. This wrapper only does what a
    *schematic Value field* needs on top: strip the unit tail and fold the
    unambiguous upper-case multipliers, then turn a parse failure into
    ``None`` (a review detector reports *insufficient evidence*; it never
    raises).
    """
    if not text:
        return None
    s = _UNIT_TAIL_RX.sub("", str(text).strip()).strip().translate(_LETTER_FOLD)
    if not s:
        return None
    try:
        return _si_parse_value(s)
    except (CalcError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------- #
# net semantics (delegated to ..classify — single source of truth)
# --------------------------------------------------------------------------- #
def net_is_ground(net: Net) -> bool:
    return any(_is_ground(n) for n in _net_candidate_names(net))


def net_is_power(net: Net) -> bool:
    return any(_is_power(n) for n in _net_candidate_names(net))


def net_implied_voltage(net: Net) -> float | None:
    for name in _net_candidate_names(net):
        v = classify.implied_voltage(name)
        if v is not None:
            return v
    return None


def name_implied_voltage(name: str | None) -> float | None:
    return classify.implied_voltage(name)


# --------------------------------------------------------------------------- #
# review context: per-analysis indexes
# --------------------------------------------------------------------------- #
@dataclass
class ReviewCtx:
    """Immutable-per-run indexes every detector shares.

    ``facts`` is the optional datasheet facts store
    (:class:`~.facts.FactsStore`); detectors that find a fact upgrade their
    judgement to ``datasheet_backed``, and fall back to their heuristics —
    never to a guess — when it is absent.
    """

    sch: Schematic
    pcb: object | None = None
    facts: object | None = None
    gerbers: object | None = None
    comps: dict[str, Component] = field(default_factory=dict)
    net_of: dict[tuple[str, str], Net] = field(default_factory=dict)
    comp_nets: dict[str, list[Net]] = field(default_factory=dict)

    def fact_for(self, ref: str, key: str):
        """The :class:`~.facts.FactValue` for a component's fact, or ``None``."""
        if self.facts is None:
            return None
        comp = self.comps.get(ref)
        if comp is None:
            return None
        facts = self.facts.lookup_component(comp)
        return facts.get(key) if facts else None

    def pin_name(self, ref: str, pin_number: str) -> str:
        comp = self.comps.get(ref)
        if comp is None:
            return ""
        for p in comp.pins:
            if str(p.number) == str(pin_number):
                return p.name or ""
        return ""


def build_ctx(sch: Schematic, pcb: object | None = None,
              facts: object | None = None,
              gerbers: object | None = None) -> ReviewCtx:
    ctx = ReviewCtx(sch=sch, pcb=pcb, facts=facts, gerbers=gerbers)
    ctx.comps = {c.designator: c for c in sch.components}
    for net in sch.nets:
        for ref, pin in net.members:
            ctx.net_of[(ref, str(pin))] = net
            nets = ctx.comp_nets.setdefault(ref, [])
            if net not in nets:
                nets.append(net)
    return ctx


def two_terminal_nets(ctx: ReviewCtx, ref: str) -> tuple[Net, Net] | None:
    """The exactly-two distinct nets a two-terminal part bridges, else None.

    Judged by net MEMBERSHIP, not by the symbol's pin list — robust across
    formats and multi-pad passives (a part on one net twice is not a bridge).
    """
    nets = ctx.comp_nets.get(ref, [])
    if len(nets) != 2:
        return None
    return nets[0], nets[1]


def other_net(ctx: ReviewCtx, ref: str, net: Net) -> Net | None:
    tt = two_terminal_nets(ctx, ref)
    if tt is None:
        return None
    if tt[0] is net:
        return tt[1]
    if tt[1] is net:
        return tt[0]
    return None


# --------------------------------------------------------------------------- #
# divider recognition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Divider:
    """Two series resistors rail→tap→ground: the canonical divider shape.

    ``powered`` records whether the top net is power-recognised (power-named
    or voltage-implying). A feedback divider is meaningful either way (the FB
    pin marks the topology); a PLAIN divider is only reviewed when powered,
    or two series resistors between arbitrary signals would drown the report.
    """

    r_top: str
    r_bottom: str
    top: Net
    mid: Net
    bottom: Net
    powered: bool = True


def find_dividers(ctx: ReviewCtx) -> list[Divider]:
    """Every rail→R→tap→R→ground chain, deterministic order.

    ``top`` must be power-ish (power-named or with an implied voltage) and
    ``bottom`` ground; the tap is any non-ground net joining the two
    resistors. A voltage-implying tap NAME (``2V5_REF``) classifies as a
    power rail under the shared rail heuristics, so the tap filter excludes
    only ground — that naming style is exactly what the tap-mismatch rule
    exists to check. A resistor participating in several taps yields several
    dividers (each is reviewed independently).
    """
    out: list[Divider] = []
    for net in ctx.sch.nets:
        if net_is_ground(net):
            continue
        rs = sorted({ref for ref, _pin in net.members
                     if ref in ctx.comps and is_resistor(ctx.comps[ref])})
        for i, ra in enumerate(rs):
            na = other_net(ctx, ra, net)
            if na is None:
                continue
            for rb in rs[i + 1:]:
                nb = other_net(ctx, rb, net)
                if nb is None:
                    continue
                for r_top, r_bot, top, bot in ((ra, rb, na, nb),
                                               (rb, ra, nb, na)):
                    if not net_is_ground(bot) or net_is_ground(top):
                        continue
                    powered = (net_is_power(top)
                               or net_implied_voltage(top) is not None)
                    out.append(Divider(r_top=r_top, r_bottom=r_bot,
                                       top=top, mid=net, bottom=bot,
                                       powered=powered))
                    break
    return out


def fb_pin_on(ctx: ReviewCtx, net: Net) -> tuple[str, str] | None:
    """``(ref, pin)`` of an IC feedback/sense pin on this net, else None.

    An IC here is any component spanning ≥3 nets (never a passive), whose pin
    NAME matches the feedback vocabulary.
    """
    for ref, pin in net.members:
        comp = ctx.comps.get(ref)
        if comp is None or len(ctx.comp_nets.get(ref, [])) < 3:
            continue
        name = ctx.pin_name(ref, pin).upper().strip()
        if name in tables.FB_PIN_NAMES:
            return ref, str(pin)
    return None


def caps_to_ground(ctx: ReviewCtx, net: Net) -> list[str]:
    """Fitted capacitors bridging this net to ground (sorted designators).

    DNP capacitors are excluded (:func:`is_fitted`): every caller asks an
    ELECTRICAL question — does this node see a load/bypass cap (crystal load
    pair, RC pole, rail decoupling count) — and an unpopulated cap answers no.
    """
    out = []
    for ref, _pin in net.members:
        comp = ctx.comps.get(ref)
        if comp is None or not is_capacitor(comp) or not is_fitted(comp):
            continue
        o = other_net(ctx, ref, net)
        if o is not None and net_is_ground(o):
            out.append(ref)
    return sorted(set(out))
