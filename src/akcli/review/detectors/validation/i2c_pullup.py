"""I²C pull-up review (M3).

Judged through akcli's own ``calc i2c-pullup`` (NXP UM10204-cited). The
sink-current floor R_min = (VDD−0.4 V)/3 mA needs no bus-capacitance guess;
the rise-time ceiling does, so a weak pull-up is only ever a NOTE with the
C_b assumption stated — never a confident claim.

Bus recognition is by net-name TOKEN, not by a name suffix: a net is a bus
line when one of its ``_``/``.``/``-``-separated tokens is ``SDA``/``SCL``
(optionally numbered — ``I2C1_SDA``, ``SDA0_3V3``). Two further tokens,
``SDATA`` and ``SCLK``, name the same lines on plenty of boards but are also
what I²S and SPI call their data/clock — so they only count when BOTH lines
of one bus (same remaining base name) are present: ``SENSOR_SDATA`` +
``SENSOR_SCLK`` is a bus, a lone ``SPI_SCLK`` next to ``SPI_MOSI`` is not.
Generic ``DAT``/``CLK`` are deliberately NOT recognized at all — on a real
board they name everything from an SD card to a shift register, and a
missing-pull-up warning on those is pure noise.
"""

from __future__ import annotations

import re

from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import topo

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z]+")
# token (digits stripped) -> bus line. "Strong" tokens are I²C-exclusive;
# "weak" ones are shared with I²S/SPI and need their partner line (see above).
_STRONG_TOKENS: dict[str, str] = {"SDA": "SDA", "SCL": "SCL"}
_WEAK_TOKENS: dict[str, str] = {"SDATA": "SDA", "SCLK": "SCL"}

# Rise-time ceiling needs the bus capacitance nobody drew: assume a typical
# short-bus figure and SAY SO. Standard-mode numbers keep the ceiling
# conservative.
_CB_ASSUMED_F = 100e-12
_I2C_MODE = "standard"

RULES = (
    Rule(
        code="REVIEW_I2C_NO_PULLUP",
        title="I²C bus line has no pull-up resistor",
        explain=(
            "A net whose name carries an SDA/SCL token (≥2 members) reaches "
            "no resistor tied "
            "to a power rail. Open-drain I²C needs a pull-up per line; some "
            "MCUs provide weak internal pull-ups, but those are usually too "
            "weak for spec rise times — confirm deliberately, or fit the "
            "resistor."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="NXP UM10204 Rev.7 §7.1 (open-drain bus needs pull-ups)"),
    Rule(
        code="REVIEW_I2C_PULLUP_STRONG",
        title="I²C pull-up below the sink-current floor",
        explain=(
            "R < R_min = (VDD−0.4 V)/3 mA (NXP UM10204 §7.1, via `akcli calc "
            "i2c-pullup`): the bus device cannot sink enough current to pull "
            "the line to a valid LOW. This floor needs no bus-capacitance "
            "assumption — only the rail voltage, which comes from the rail "
            "NAME (hence heuristic)."),
        default_severity="warning", confidence="heuristic", version="1",
        reference="NXP UM10204 Rev.7 §7.1: Rp(min) = (VDD−0.4 V)/3 mA"),
    Rule(
        code="REVIEW_I2C_PULLUP_WEAK",
        title="I²C pull-up above the rise-time ceiling (assumed C_b)",
        explain=(
            f"R > R_max for the assumed bus capacitance ({_CB_ASSUMED_F * 1e12:.0f} pF, "
            f"{_I2C_MODE} mode): the rise time likely misses spec. The real "
            "C_b depends on trace length and device count nobody drew — this "
            "stays a NOTE with the assumption stated."),
        default_severity="note", confidence="heuristic", version="1",
        reference="NXP UM10204 Rev.7 §7.1: Rp(max) = t_r/(0.8473·C_b)"),
    Rule(
        code="REVIEW_I2C_PULLUP_MISMATCH",
        title="SDA and SCL pull-ups differ",
        explain=(
            "The two lines of one bus carry different pull-up values. Legal, "
            "but almost always an oversight — the lines see the same load "
            "and should rise together."),
        default_severity="note", confidence="heuristic", version="1",
        reference="NXP UM10204 Rev.7 §7.1 (open-drain bus needs pull-ups)"),
)


def _pullups(ctx: topo.ReviewCtx, net) -> list[tuple[str, object]]:
    """``(resistor_ref, rail_net)`` FITTED pull-ups from this net to a rail.

    A DNP pull-up satisfies nothing — the shipped board's bus floats — so it
    does not silence ``REVIEW_I2C_NO_PULLUP`` (:func:`topo.is_fitted`).
    """
    out = []
    for ref, _pin in net.members:
        comp = ctx.comps.get(ref)
        if comp is None or not topo.is_resistor(comp) or not topo.is_fitted(comp):
            continue
        on = topo.other_net(ctx, ref, net)
        if on is None or topo.net_is_ground(on):
            continue
        if topo.net_is_power(on) or topo.net_implied_voltage(on) is not None:
            out.append((ref, on))
    return sorted(out, key=lambda t: t[0])


def _line_of_name(name: str | None) -> tuple[str, str, bool] | None:
    """``(kind, base, strong)`` for a bus-line NAME, else ``None``.

    ``kind`` is ``SDA``/``SCL``; ``base`` is the rest of the name (the bus
    instance — ``SENSOR`` in ``SENSOR_SDA``), so two lines are paired only
    when they belong to the same bus; ``strong`` marks an I²C-exclusive
    token.
    """
    tokens = [t.upper() for t in _TOKEN_SPLIT.split(name or "") if t]
    for i, tok in enumerate(tokens):
        core = tok.rstrip("0123456789")
        kind = _STRONG_TOKENS.get(core)
        if kind is not None:
            return kind, "_".join(tokens[:i] + tokens[i + 1:]), True
        kind = _WEAK_TOKENS.get(core)
        if kind is not None:
            return kind, "_".join(tokens[:i] + tokens[i + 1:]), False
    return None


def _line_of_net(net) -> tuple[str, str, bool] | None:
    """First bus-line classification among every name the net answers to."""
    from ....checks.power import _net_candidate_names
    for name in _net_candidate_names(net):
        hit = _line_of_name(name)
        if hit is not None:
            return hit
    return None


def _bus_lines(ctx: topo.ReviewCtx) -> list[tuple[str, str, object]]:
    """``(kind, base, net)`` for every recognized bus line, in net order.

    A line found only through a WEAK token is kept only when its partner
    line (same base, other kind) is on the board too — that is what keeps
    ``SPI_SCLK`` off this report while ``SENSOR_SCLK`` + ``SENSOR_SDATA``
    stays on it.
    """
    found: list[tuple[str, str, object, bool]] = []
    for net in ctx.sch.nets:
        if len(net.members) < 2:
            continue
        hit = _line_of_net(net)
        if hit is None:
            continue
        kind, base, strong = hit
        found.append((kind, base, net, strong))
    kinds_by_base: dict[str, set[str]] = {}
    for kind, base, _net, _strong in found:
        kinds_by_base.setdefault(base, set()).add(kind)
    return [(kind, base, net) for kind, base, net, strong in found
            if strong or kinds_by_base.get(base) == {"SDA", "SCL"}]


def run(ctx: topo.ReviewCtx) -> list[Finding]:
    from ....calc import compute

    out: list[Finding] = []
    buses: dict[tuple[str, str], dict] = {}   # (base, kind) -> pull-up record
    for kind, base, net in _bus_lines(ctx):
        pulls = _pullups(ctx, net)
        anchors = [anchor("net", net.name)]
        if not pulls:
            out.append(Finding(
                code="REVIEW_I2C_NO_PULLUP", severity=Severity.WARNING,
                message=f"I2C {kind} net {net.name!r} has no pull-up resistor",
                refs=[net.name], anchors=anchors, confidence="heuristic",
                evidence={"source": "topology"},
                remediation=("fit a pull-up to the bus rail, or confirm the "
                             "host's internal pull-ups meet the rise time"),
                fix_params={"kind": "add_pullup", "net": net.name}))
            continue
        r_ref, rail = pulls[0]
        r = topo.parse_value(ctx.comps[r_ref].value)
        vdd = topo.net_implied_voltage(rail)
        buses.setdefault((base, kind), {"net": net.name, "ref": r_ref, "r": r})
        if r is None or vdd is None:
            continue                          # no value / no rail voltage: no guess
        env = compute("i2c-pullup",
                      {"vdd": vdd, "cb": _CB_ASSUMED_F, "mode": _I2C_MODE})
        r_min = env["results"]["r_min"]["value"]
        r_max = env["results"]["r_max"]["value"]
        evidence = {
            "source": "calc", "calc": env,
            "assumptions": [f"C_b = {_CB_ASSUMED_F * 1e12:.0f} pF assumed "
                            f"({_I2C_MODE} mode); rail {rail.name!r} implies "
                            f"VDD = {vdd} V"],
        }
        common = dict(refs=[r_ref, net.name],
                      anchors=anchors + [anchor("component", r_ref)],
                      confidence="heuristic", evidence=evidence)
        if r < r_min:
            out.append(Finding(
                code="REVIEW_I2C_PULLUP_STRONG", severity=Severity.WARNING,
                message=(f"I2C {kind} pull-up {r_ref}="
                         f"{ctx.comps[r_ref].value} < R_min "
                         f"{r_min:,.0f} Ω @ VDD {vdd} V — bus cannot reach "
                         "a valid LOW"),
                remediation=f"raise the pull-up above {r_min:,.0f} Ω",
                fix_params={"kind": "retune_pullup", "ref": r_ref,
                            "r_min": round(r_min, 1)},
                **common))
        elif r > r_max:
            out.append(Finding(
                code="REVIEW_I2C_PULLUP_WEAK", severity=Severity.NOTE,
                message=(f"I2C {kind} pull-up {r_ref}="
                         f"{ctx.comps[r_ref].value} > R_max "
                         f"{r_max:,.0f} Ω under the assumed "
                         f"{_CB_ASSUMED_F * 1e12:.0f} pF bus — rise time "
                         "likely out of spec"),
                remediation="verify against the real bus capacitance",
                **common))
    # Mismatch is judged PER BUS (same base name): a board with two I2C buses
    # must not have SENSOR_SDA's pull-up compared against DISPLAY_SCL's.
    for base in sorted({b for b, _k in buses}):
        sda, scl = buses.get((base, "SDA")), buses.get((base, "SCL"))
        if not (sda and scl) or sda["r"] is None or scl["r"] is None:
            continue
        if abs(sda["r"] - scl["r"]) <= 1e-9:
            continue
        out.append(Finding(
            code="REVIEW_I2C_PULLUP_MISMATCH", severity=Severity.NOTE,
            message=(f"SDA pull-up {sda['ref']} and SCL pull-up {scl['ref']} "
                     "differ — one bus should rise symmetrically"),
            refs=[sda["ref"], scl["ref"]],
            anchors=[anchor("component", sda["ref"]),
                     anchor("component", scl["ref"])],
            confidence="heuristic", evidence={"source": "topology"},
            remediation="use the same value on both lines"))
    return out


register(Detector(name="validation.i2c_pullup", family="validation", run=run,
                  rules=RULES))
