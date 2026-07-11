"""Battery-powered sensor front-end checks: runtime, comparator window,
envelope detector, LDO headroom.

Distilled from a real low-power sensor design review; each is a quick
go/no-go with the numbers a datasheet actually gives you (mAh, V_DO, ...).

References:

* Battery life: ANSI C18.1M (portable primary cells) rates capacity at low
  drain; manufacturer alkaline datasheets (Energizer/Duracell) show the
  usable fraction dropping with load and temperature — the default 0.7
  derating is that manufacturer-typical figure (advisory, not a standard).
* Comparator thresholds: Texas Instruments SLVA954, *Analog Engineer's
  Circuit: Inverting comparator with hysteresis* — node equation
  V+ = (VCC·G1 + VOUT·Gf)/(G1+G2+Gf). An open-drain output with pull-up
  Rpu feeds the node through Rf+Rpu while released and through Rf to GND
  while low, so the two thresholds use different feedback conductances.
* Envelope detector: design rule 1/f_carrier ≪ RC ≪ 1/f_signal with ripple
  ≈ V_pk/(f_carrier·RC) — S. Haykin, *Communication Systems* 4th ed. §2.2
  (diagonal clipping); Horowitz & Hill 3rd ed. §1.6.6 (peak detector).
* LDO headroom: regulation requires VIN ≥ VOUT + V_DO(IOUT) per datasheet
  recommended operating conditions — TI SLVA079, *Understanding the Terms
  and Definitions of LDO Voltage Regulators*; P = (VIN−VOUT)·IOUT.
"""

from __future__ import annotations

from .registry import CalcError, Param, Result, register


@register(
    "battery-life", "Battery runtime from datasheet mAh", "power",
    "ANSI C18.1M rated capacity (low drain) × manufacturer-typical usable "
    "fraction under load (Energizer/Duracell alkaline datasheets) — advisory",
    (Param("capacity", "mAh", "datasheet capacity"),
     Param("i_avg", "mA", "average load current"),
     Param("derating", "", "usable-capacity factor", default=0.7)),
    notes="Same estimate as `battery` but in the mAh/mA a datasheet quotes; "
          "duty-cycle the load into i_avg first. Never promise runtime "
          "without margin — capacity falls further with cold and pulse loads.",
)
def _calc_battery_life(capacity, i_avg, derating) -> list[Result]:
    if capacity <= 0 or i_avg <= 0 or not 0 < derating <= 1:
        raise CalcError("need capacity > 0, i_avg > 0, 0 < derating <= 1")
    hours = capacity * derating / i_avg
    return [Result("capacity_usable", capacity * derating, "mAh",
                   f"{derating:g} × rated"),
            Result("hours", hours, "h"),
            Result("days", hours / 24, "d")]


@register(
    "comparator-hysteresis",
    "Comparator thresholds: divider + feedback, open-drain aware", "ic",
    "TI SLVA954, Analog Engineer's Circuit: Inverting comparator with "
    "hysteresis — V+ = (VCC·G1 + VOUT·Gf)/(G1+G2+Gf); open-drain pull-up "
    "adds Rpu in series with Rf only while the output is released",
    (Param("vcc", "V", "supply / divider source"),
     Param("r1", "Ω", "divider resistor to VCC"),
     Param("r2", "Ω", "divider resistor to GND"),
     Param("rf", "Ω", "feedback resistor to comparator output"),
     Param("rpu", "Ω", "open-drain pull-up to VCC (0 = push-pull output)",
           default=0.0)),
    notes="Inverting topology: signal on IN−, this node on IN+. With Rpu the "
          "rising threshold sees Rf+Rpu (output released) but the falling "
          "threshold still sees Rf to GND — hysteresis is asymmetric.",
)
def _calc_comparator_hysteresis(vcc, r1, r2, rf, rpu) -> list[Result]:
    if vcc <= 0 or min(r1, r2, rf) <= 0 or rpu < 0:
        raise CalcError("need vcc, r1, r2, rf > 0 and rpu >= 0")
    g1, g2 = 1 / r1, 1 / r2
    gf_lo = 1 / rf                 # output low: pin at ~0 V, Rf to GND
    gf_hi = 1 / (rf + rpu)         # output released: VCC via Rpu then Rf
    v_rise = (vcc * g1 + vcc * gf_hi) / (g1 + g2 + gf_hi)
    v_fall = (vcc * g1) / (g1 + g2 + gf_lo)
    return [Result("vth_nominal", vcc * r2 / (r1 + r2), "V",
                   "divider alone, no feedback"),
            Result("v_rise", v_rise, "V", "trip point for rising input"),
            Result("v_fall", v_fall, "V", "trip point for falling input"),
            Result("hysteresis", v_rise - v_fall, "V")]


@register(
    "envelope-detector", "Diode envelope detector RC validity", "ic",
    "1/f_carrier ≪ RC ≪ 1/f_signal, ripple ≈ V_pk/(f_carrier·RC) — Haykin, "
    "Communication Systems 4th ed. §2.2; Horowitz & Hill 3rd ed. §1.6.6",
    (Param("c_hold", "F", "hold capacitor"),
     Param("r_bleed", "Ω", "bleed resistor across the capacitor"),
     Param("f_carrier", "Hz", "carrier / excitation frequency"),
     Param("f_signal", "Hz", "highest envelope (modulation) frequency"),
     Param("margin", "", "minimum ≪ ratio per side", default=5.0)),
    notes="τ too small = excess ripple; τ too large = envelope smeared "
          "(diagonal clipping). Diode drop and source impedance ignored.",
)
def _calc_envelope_detector(c_hold, r_bleed, f_carrier, f_signal,
                            margin) -> list[Result]:
    if min(c_hold, r_bleed, f_carrier, f_signal) <= 0 or margin < 1:
        raise CalcError("need positive c_hold/r_bleed/frequencies, margin >= 1")
    if f_carrier <= f_signal:
        raise CalcError("need f_carrier > f_signal — no envelope to detect")
    tau = r_bleed * c_hold
    ratio_carrier = tau * f_carrier          # τ / T_carrier, want ≥ margin
    ratio_signal = 1 / (tau * f_signal)      # T_signal / τ, want ≥ margin
    ok_c, ok_s = ratio_carrier >= margin, ratio_signal >= margin
    if ok_c and ok_s:
        verdict = "VALID"
    elif not ok_c and not ok_s:
        verdict = "INVALID: f_carrier too close to f_signal — no workable τ"
    elif not ok_c:
        verdict = "INVALID: τ too small vs carrier — excess ripple"
    else:
        verdict = "INVALID: τ too large vs signal — envelope smeared"
    return [Result("tau", tau, "s", "R·C"),
            Result("ripple_pct", 100 / ratio_carrier, "%",
                   "peak-to-peak, fraction of V_pk"),
            Result("ratio_carrier", ratio_carrier, "",
                   f"τ·f_carrier, want ≥ {margin:g}"),
            Result("ratio_signal", ratio_signal, "",
                   f"1/(τ·f_signal), want ≥ {margin:g}"),
            Result("verdict", verdict)]


@register(
    "ldo-headroom", "LDO input-headroom go/no-go", "power",
    "VIN(min) ≥ VOUT + V_DO(IOUT) per LDO datasheet recommended operating "
    "conditions (TI SLVA079, Understanding the Terms and Definitions of LDO "
    "Voltage Regulators); P = (VIN−VOUT)·IOUT",
    (Param("vin_min", "V", "minimum input voltage (end-of-life battery!)"),
     Param("vout", "V", "output voltage"),
     Param("v_dropout", "V", "datasheet dropout at i_load"),
     Param("i_load", "A", "load current")),
    notes="Quick go/no-go at VIN_min; use `ldo` for IQ, efficiency and "
          "thermal (θJA/Tj). Dissipation is worst at VIN_max, not VIN_min.",
)
def _calc_ldo_headroom(vin_min, vout, v_dropout, i_load) -> list[Result]:
    if not 0 < vout < vin_min or v_dropout < 0 or i_load <= 0:
        raise CalcError("need 0 < vout < vin_min, v_dropout >= 0, i_load > 0")
    headroom = vin_min - vout
    margin = headroom - v_dropout
    return [Result("headroom", headroom, "V", "VIN_min − VOUT"),
            Result("margin", margin, "V", "headroom − V_DO"),
            Result("p_dissipated", headroom * i_load, "W",
                   "at VIN_min — recompute at VIN_max for worst case"),
            Result("headroom_ok", margin >= 0, "",
                   f"headroom {headroom:g} V vs V_DO {v_dropout:g} V")]
