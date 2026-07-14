"""Elementary circuit calculators: Ohm's law, dividers, LED resistor, RC/RL/LC.

References:
* Ohm's law / dividers / time constants: any circuit-theory text; we cite
  P. Horowitz & W. Hill, *The Art of Electronics*, 3rd ed. (Cambridge, 2015),
  §1.2 (Ohm's law, dividers), §1.4 (RC time constants, filters).
* E-series snapping: IEC 60063:2015.
"""

from __future__ import annotations

import math

from .eseries import SERIES, series_values, snap
from .registry import CalcError, Param, Result, register
from .si import fmt_eng

_HH = ("P. Horowitz & W. Hill, The Art of Electronics, 3rd ed. (2015)")


@register(
    "ohm", "Ohm's law / power (give any two of v, i, r)", "basics",
    f"{_HH}, §1.2.2 (V = I·R, P = V·I)",
    (Param("v", "V", "voltage", default=0.0),
     Param("i", "A", "current", default=0.0),
     Param("r", "Ω", "resistance", default=0.0)),
)
def _calc_ohm(v: float, i: float, r: float) -> list[Result]:
    given = [n for n, x in (("v", v), ("i", i), ("r", r)) if x]
    if len(given) != 2:
        raise CalcError("give exactly two of v, i, r (non-zero)")
    if not v:
        v = i * r
    elif not i:
        i = v / r
    else:
        r = v / i
    return [Result("v", v, "V"), Result("i", i, "A"), Result("r", r, "Ω"),
            Result("p", v * i, "W")]


@register(
    "vdivider", "Resistive voltage divider (analysis)", "basics",
    f"{_HH}, §1.2.3 (Vout = Vin·R2/(R1+R2))",
    (Param("vin", "V", "input voltage"),
     Param("r_top", "Ω", "upper resistor (Vin→Vout)"),
     Param("r_bottom", "Ω", "lower resistor (Vout→GND)")),
)
def _calc_vdivider(vin: float, r_top: float, r_bottom: float) -> list[Result]:
    if r_top <= 0 or r_bottom <= 0:
        raise CalcError("resistors must be positive")
    vout = vin * r_bottom / (r_top + r_bottom)
    i = vin / (r_top + r_bottom)
    return [Result("vout", vout, "V"),
            Result("current", i, "A"),
            Result("p_top", i * i * r_top, "W"),
            Result("p_bottom", i * i * r_bottom, "W"),
            Result("thevenin_r", r_top * r_bottom / (r_top + r_bottom), "Ω",
                   "source impedance seen at Vout")]


@register(
    "vdivider-design", "Voltage divider design (E-series pair search)", "basics",
    f"{_HH}, §1.2.3; value snapping per IEC 60063:2015",
    (Param("vin", "V", "input voltage"),
     Param("vout", "V", "wanted output voltage"),
     Param("series", "", "E series", default="E96", choices=tuple(SERIES)),
     Param("r_total", "Ω", "target R1+R2 magnitude", default=100e3)),
)
def _calc_vdivider_design(vin: float, vout: float, series: str,
                          r_total: float) -> list[Result]:
    if not 0 < vout < vin:
        raise CalcError("need 0 < vout < vin")
    ratio = vout / vin
    cands = []
    for r_bot in series_values(series, r_total * ratio / 10, r_total * ratio * 10):
        ideal_top = r_bot * (vin - vout) / vout
        r_top, _ = snap(ideal_top, series)
        got = vin * r_bot / (r_top + r_bot)
        cands.append({
            "r_top": r_top, "r_bottom": r_bot,
            "vout": round(got, 9),
            "error_pct": round((got - vout) / vout * 100, 6),
            "current": vin / (r_top + r_bot),
        })
    cands.sort(key=lambda c: (abs(c["error_pct"]),
                              abs(c["r_top"] + c["r_bottom"] - r_total)))
    best = cands[0]
    return [Result("r_top", best["r_top"], "Ω"),
            Result("r_bottom", best["r_bottom"], "Ω"),
            Result("vout_actual", best["vout"], "V",
                   f"{best['error_pct']:+.4f}% vs target"),
            Result("divider_current", best["current"], "A"),
            Result("alternatives", cands[1:4])]


@register(
    "led", "LED series resistor", "basics",
    f"{_HH}, §1.2.2 (R = (Vs − n·Vf)/If); value snapping per IEC 60063:2015",
    (Param("vs", "V", "supply voltage"),
     Param("vf", "V", "LED forward voltage (datasheet)"),
     Param("i", "A", "wanted LED current"),
     Param("n", "", "LEDs in series", default=1.0),
     Param("series", "", "E series", default="E24", choices=tuple(SERIES))),
)
def _calc_led(vs: float, vf: float, i: float, n: float, series: str) -> list[Result]:
    drop = vs - n * vf
    if drop <= 0:
        raise CalcError(f"supply too low: {n:g} × Vf = {n * vf:g} V ≥ Vs")
    if i <= 0:
        raise CalcError("current must be positive")
    r_ideal = drop / i
    r_std, _ = snap(r_ideal, series)
    i_actual = drop / r_std
    return [Result("r_ideal", r_ideal, "Ω"),
            Result("r_standard", r_std, "Ω", fmt_eng(r_std, "Ω")),
            Result("i_actual", i_actual, "A"),
            Result("p_resistor", i_actual * i_actual * r_std, "W",
                   "pick ≥2× rated package")]


@register(
    "rc", "RC time constant / cutoff frequency", "basics",
    f"{_HH}, §1.4.1 (τ = RC), §1.7.1 (f_c = 1/(2πRC), −3 dB)",
    (Param("r", "Ω", "resistance"),
     Param("c", "F", "capacitance")),
)
def _calc_rc(r: float, c: float) -> list[Result]:
    tau = r * c
    return [Result("tau", tau, "s"),
            Result("fc", 1 / (2 * math.pi * tau), "Hz", "−3 dB corner"),
            Result("t_63pct", tau, "s"),
            Result("t_99pct", 4.6 * tau, "s", "≈ ln(100)·τ")]


@register(
    "rc-charge", "RC charge time to a threshold", "basics",
    f"{_HH}, §1.4.1 (V(t) = Vs·(1−e^(−t/RC)))",
    (Param("r", "Ω", "resistance"),
     Param("c", "F", "capacitance"),
     Param("vs", "V", "step/supply voltage"),
     Param("vt", "V", "threshold voltage to reach")),
)
def _calc_rc_charge(r: float, c: float, vs: float, vt: float) -> list[Result]:
    if not 0 < vt < vs:
        raise CalcError("need 0 < vt < vs")
    t = -r * c * math.log(1 - vt / vs)
    return [Result("t", t, "s", f"time to reach {vt:g} V")]


@register(
    "lc", "LC resonance / characteristic impedance", "basics",
    f"{_HH}, §1.7.14 (f₀ = 1/(2π√LC)); Z = √(L/C)",
    (Param("l", "H", "inductance"),
     Param("c", "F", "capacitance")),
)
def _calc_lc(l: float, c: float) -> list[Result]:
    return [Result("f0", 1 / (2 * math.pi * math.sqrt(l * c)), "Hz"),
            Result("z", math.sqrt(l / c), "Ω", "surge impedance √(L/C)")]


@register(
    "reactance", "Capacitive / inductive reactance at f", "basics",
    f"{_HH}, §1.7.1 (X_C = 1/(2πfC)), §1.7.13 (X_L = 2πfL)",
    (Param("f", "Hz", "frequency"),
     Param("c", "F", "capacitance", default=0.0),
     Param("l", "H", "inductance", default=0.0)),
)
def _calc_reactance(f: float, c: float, l: float) -> list[Result]:
    if not c and not l:
        raise CalcError("give c and/or l")
    out = []
    if c:
        out.append(Result("xc", 1 / (2 * math.pi * f * c), "Ω"))
    if l:
        out.append(Result("xl", 2 * math.pi * f * l, "Ω"))
    return out
