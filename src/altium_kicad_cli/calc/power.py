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
    "ldo", "LDO dissipation / dropout / thermal check", "power",
    "P_D = (VIN−VOUT)·IOUT + VIN·IQ (LDO datasheet thermal sections, e.g. "
    "TI SLVA118 dropout appnote); Tj = Ta + P·θJA per JESD51-2A",
    (Param("vin", "V", "input voltage"),
     Param("vout", "V", "output voltage"),
     Param("iout", "A", "load current"),
     Param("iq", "A", "quiescent/ground current", default=0.0),
     Param("v_dropout", "V", "datasheet dropout at iout (0 = skip check)",
           default=0.0),
     Param("theta_ja", "°C/W", "junction-to-ambient (0 = skip Tj)", default=0.0),
     Param("ta", "°C", "ambient temperature", default=25.0),
     Param("tj_max", "°C", "junction limit", default=125.0)),
)
def _calc_ldo(vin, vout, iout, iq, v_dropout, theta_ja, ta, tj_max) -> list[Result]:
    if not 0 < vout < vin or iout <= 0:
        raise CalcError("need 0 < vout < vin and iout > 0")
    p = (vin - vout) * iout + vin * iq
    out = [Result("p_dissipated", p, "W"),
           Result("efficiency", vout * iout / (vin * (iout + iq)), "")]
    if v_dropout > 0:
        out.append(Result("dropout_ok", vin - vout >= v_dropout,
                          "", f"headroom {vin - vout:g} V vs V_DO {v_dropout:g} V"))
    if theta_ja > 0:
        tj = ta + p * theta_ja
        out += [Result("tj", tj, "°C"),
                Result("tj_ok", tj <= tj_max)]
    out.append(Result("theta_ja_max", (tj_max - ta) / p, "°C/W",
                      "package/copper must beat this"))
    return out


@register(
    "gate-drive", "MOSFET gate-drive current / resistor / power", "power",
    "L. Balogh, Fundamentals of MOSFET and IGBT Gate Driver Circuits, "
    "TI SLUA618A: I_pk ≈ Qg/t_sw; P_drive = Qg·V_drv·f_sw",
    (Param("qg", "", "total gate charge Qg at V_drv (coulomb, e.g. 20n)"),
     Param("v_drive", "V", "gate-drive voltage", default=10.0),
     Param("t_switch", "s", "wanted voltage transition time", default=50e-9),
     Param("fsw", "Hz", "switching frequency", default=100e3),
     Param("r_driver", "Ω", "driver internal resistance", default=1.0)),
)
def _calc_gate_drive(qg, v_drive, t_switch, fsw, r_driver) -> list[Result]:
    if min(qg, v_drive, t_switch, fsw) <= 0:
        raise CalcError("qg, v_drive, t_switch, fsw must be positive")
    i_pk = qg / t_switch
    r_total = v_drive / i_pk
    r_gate = r_total - r_driver
    return [Result("i_peak", i_pk, "A", "driver must source/sink this"),
            Result("r_total", r_total, "Ω", "V_drv/I_pk"),
            Result("r_gate", r_gate, "Ω",
                   "series gate resistor after driver R" if r_gate >= 0
                   else "driver alone is too slow for this t_switch"),
            Result("p_drive", qg * v_drive * fsw, "W",
                   "dissipated in driver + gate resistances"),
            Result("i_avg", qg * fsw, "A", "average gate-supply current")]


@register(
    "shunt", "Current-sense shunt sizing", "power",
    "Current-sensing practice per TI SBOA170 (shunt + amplifier); "
    "P = I²R with ≥2× power derating",
    (Param("i_max", "A", "full-scale current"),
     Param("v_sense", "V", "full-scale sense voltage", default=0.1),
     Param("adc_fs", "V", "amplifier output full-scale (0 = skip gain)",
           default=0.0)),
    notes="Use a Kelvin (4-wire) footprint; the value is usually a special "
          "shunt part, not an E-series resistor.",
)
def _calc_shunt(i_max, v_sense, adc_fs) -> list[Result]:
    if i_max <= 0 or v_sense <= 0:
        raise CalcError("i_max and v_sense must be positive")
    r = v_sense / i_max
    p = i_max * i_max * r
    out = [Result("r_shunt", r, "Ω"),
           Result("p_at_fullscale", p, "W"),
           Result("p_rating_min", 2 * p, "W", "≥2× derating"),
           Result("v_burden", v_sense, "V")]
    if adc_fs > 0:
        out.append(Result("amp_gain", adc_fs / v_sense, ""))
    return out


@register(
    "flyback", "Flyback first-order design (DCM boundary)", "power",
    "Erickson & Maksimović, Fundamentals of Power Electronics 3rd ed., "
    "ch. 6; cf. TI SLVA559: D = VOR/(VOR+VIN_min), n = VOR/(VOUT+VD), "
    "Lp ≤ (VIN_min·D)²·η/(2·POUT·fsw)",
    (Param("vin_min", "V", "minimum DC input"),
     Param("vout", "V", "output voltage"),
     Param("iout", "A", "output current"),
     Param("fsw", "Hz", "switching frequency"),
     Param("vor", "V", "reflected output voltage (design choice)"),
     Param("vd", "V", "output diode drop", default=0.5),
     Param("eff", "", "estimated efficiency", default=0.85)),
    notes="First-order DCM-boundary sizing only — leakage, snubber, and "
          "core selection still required. Switch sees VIN_max + VOR + spike.",
)
def _calc_flyback(vin_min, vout, iout, fsw, vor, vd, eff) -> list[Result]:
    if min(vin_min, vout, iout, fsw, vor) <= 0 or not 0 < eff <= 1:
        raise CalcError("bad inputs")
    duty = vor / (vor + vin_min)
    n = vor / (vout + vd)
    pout = vout * iout
    lp = (vin_min * duty) ** 2 * eff / (2 * pout * fsw)
    ipk = vin_min * duty / (lp * fsw)
    return [Result("duty_max", duty, "", "at VIN_min"),
            Result("turns_ratio", n, "", "Np : Ns"),
            Result("lp_max_dcm", lp, "H", "primary inductance, DCM boundary"),
            Result("i_pk_primary", ipk, "A"),
            Result("p_in", pout / eff, "W")]


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
