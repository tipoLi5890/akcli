"""Topology recognition over the normalized model — the review layer's only
new "analysis primitive" layer.

Everything here is format-agnostic: it consumes :class:`~..model.Schematic`
(KiCad or Altium alike) and NEVER parses files. Rail/ground semantics are
imported from :mod:`..checks.power` / :mod:`..checks._rails` so the review
layer cannot drift from the check layer (single source of truth).

Discipline: helpers return ``None`` rather than guess — a value that does not
parse, or a net without an implied voltage, propagates as *absence* and the
detector downgrades to ``insufficient_evidence`` instead of fabricating a
number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..checks import _rails
from ..checks.power import _is_ground, _is_power, _net_candidate_names
from ..model import Component, Net, Schematic
from . import tables

# --------------------------------------------------------------------------- #
# component classification
# --------------------------------------------------------------------------- #
_PREFIX_RX = re.compile(r"^([A-Za-z]+)")


def ref_prefix(designator: str) -> str:
    m = _PREFIX_RX.match(designator or "")
    return (m.group(1) if m else "").upper()


def _lib(comp: Component) -> str:
    return (comp.library_ref or "").lower()


def is_resistor(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in tables.RESISTOR_PREFIXES
            or _lib(comp).endswith(":r") or ":r_" in _lib(comp))


def is_capacitor(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in tables.CAPACITOR_PREFIXES
            or _lib(comp).endswith(":c") or ":c_" in _lib(comp))


def is_crystal(comp: Component) -> bool:
    return ("crystal" in _lib(comp) or "xtal" in _lib(comp)
            or ref_prefix(comp.designator) in tables.CRYSTAL_PREFIXES)


def is_connector(comp: Component) -> bool:
    return (ref_prefix(comp.designator) in tables.CONNECTOR_PREFIXES
            or "conn" in _lib(comp) or "usb" in _lib(comp))


def is_tvs(comp: Component) -> bool:
    hay = f"{_lib(comp)} {(comp.value or '').lower()}"
    return any(k in hay for k in tables.TVS_KEYWORDS)


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
_MULT: dict[str, float] = {
    "G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "R": 1.0, "r": 1.0, "Ω": 1.0,
    "": 1.0, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12,
    "f": 1e-15,
}
_VAL_RX = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([GMkKmuµnpfRrΩ]?)\s*([0-9]*)\s*"
    r"(?:F|H|ohms?|Ohms?|OHMS?)?\s*$"
)


def parse_value(text: str | None) -> float | None:
    """Engineering-notation component value, or ``None`` — never a guess.

    Accepts ``4700`` / ``4.7k`` / ``4k7`` / ``1R0`` / ``100n`` / ``10uF`` /
    ``2.2µF`` / ``0R``. Case is significant for m (milli) vs M (mega).
    """
    if not text:
        return None
    m = _VAL_RX.match(str(text))
    if not m:
        return None
    head, letter, tail = m.groups()
    try:
        base = float(f"{head}.{tail}") if tail else float(head)
    except ValueError:
        return None
    return base * _MULT.get(letter, 1.0)


# --------------------------------------------------------------------------- #
# net semantics (delegated to the check layer — single source of truth)
# --------------------------------------------------------------------------- #
def net_is_ground(net: Net) -> bool:
    return any(_is_ground(n) for n in _net_candidate_names(net))


def net_is_power(net: Net) -> bool:
    return any(_is_power(n) for n in _net_candidate_names(net))


def net_implied_voltage(net: Net) -> float | None:
    for name in _net_candidate_names(net):
        v = _rails.implied_voltage(name)
        if v is not None:
            return v
    return None


def name_implied_voltage(name: str | None) -> float | None:
    return _rails.implied_voltage(name)


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
