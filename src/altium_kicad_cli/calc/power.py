"""Switch-mode power-stage and battery calculators.

References:

* Buck: Texas Instruments SLVA477B, *Basic Calculation of a Buck Converter's
  Power Stage* (2015) — duty D = Vout/(Vin·η), ripple
  ΔI_L = (Vin−Vout)·D/(f_s·L), C_out(min) = ΔI_L/(8·f_s·ΔV_out).
* Boost: Texas Instruments SLVA372C, *Basic Calculation of a Boost
  Converter's Power Stage* (2014) — D = 1 − (Vin·η)/Vout,
  ΔI_L = Vin·D/(f_s·L), C_out(min) = I_out·D/(f_s·ΔV_out).
* Battery life: rule-of-thumb hours = capacity·derating/I_avg; derating
  covers self-discharge/temperature/aging (advisory, not a standard).
"""

from __future__ import annotations

from .registry import CalcError, Param, Result, register


@register(
    "buck", "Buck converter power stage", "power",
    "TI SLVA477B (2015), Basic Calculation of a Buck Converter's Power Stage",
    (Param("vin", "V", "input voltage"),
     Param("vout", "V", "output voltage"),
     Param("iout", "A", "output current"),
     Param("fsw", "Hz", "switching frequency"),
     Param("ripple_pct", "%", "inductor ripple, % of Iout", default=30.0),
     Param("vripple", "V", "allowed output voltage ripple", default=0.01),
     Param("eff", "", "estimated efficiency", default=0.9)),
    notes="ΔI_L of 20–40 % of Iout is the usual design window (SLVA477B §3).",
)
def _calc_buck(vin, vout, iout, fsw, ripple_pct, vripple, eff) -> list[Result]:
    if not 0 < vout < vin:
        raise CalcError("need 0 < vout < vin")
    if iout <= 0 or fsw <= 0 or not 0 < eff <= 1:
        raise CalcError("bad iout/fsw/eff")
    duty = vout / (vin * eff)
    di = iout * ripple_pct / 100.0
    l = (vin - vout) * duty / (fsw * di)
    cout = di / (8 * fsw * vripple)
    return [Result("duty", duty, "", "D = Vout/(Vin·η)"),
            Result("inductor", l, "H"),
            Result("ripple_current", di, "A"),
            Result("i_peak", iout + di / 2, "A", "switch/inductor peak"),
            Result("cout_min", cout, "F", "ESR ignored — add margin"),
            Result("i_in_avg", vout * iout / (vin * eff), "A")]


@register(
    "boost", "Boost converter power stage", "power",
    "TI SLVA372C (2014), Basic Calculation of a Boost Converter's Power Stage",
    (Param("vin", "V", "input voltage"),
     Param("vout", "V", "output voltage"),
     Param("iout", "A", "output current"),
     Param("fsw", "Hz", "switching frequency"),
     Param("ripple_pct", "%", "inductor ripple, % of input current", default=30.0),
     Param("vripple", "V", "allowed output voltage ripple", default=0.01),
     Param("eff", "", "estimated efficiency", default=0.9)),
)
def _calc_boost(vin, vout, iout, fsw, ripple_pct, vripple, eff) -> list[Result]:
    if not 0 < vin < vout:
        raise CalcError("need 0 < vin < vout")
    if iout <= 0 or fsw <= 0 or not 0 < eff <= 1:
        raise CalcError("bad iout/fsw/eff")
    duty = 1 - (vin * eff) / vout
    i_in = vout * iout / (vin * eff)
    di = i_in * ripple_pct / 100.0
    l = vin * duty / (fsw * di)
    cout = iout * duty / (fsw * vripple)
    return [Result("duty", duty, "", "D = 1 − Vin·η/Vout"),
            Result("inductor", l, "H"),
            Result("ripple_current", di, "A"),
            Result("i_switch_peak", i_in + di / 2, "A"),
            Result("cout_min", cout, "F", "ESR ignored — add margin"),
            Result("i_in_avg", i_in, "A")]


@register(
    "battery", "Battery life estimate", "power",
    "Rule of thumb: t = capacity·derating/I_avg (advisory; derating covers "
    "self-discharge, temperature, aging — not a standard)",
    (Param("capacity", "Ah", "battery capacity"),
     Param("i_avg", "A", "average load current"),
     Param("derating", "", "usable-capacity factor", default=0.8)),
)
def _calc_battery(capacity, i_avg, derating) -> list[Result]:
    if capacity <= 0 or i_avg <= 0 or not 0 < derating <= 1:
        raise CalcError("bad capacity/i_avg/derating")
    hours = capacity * derating / i_avg
    return [Result("hours", hours, "h"),
            Result("days", hours / 24, "d")]
