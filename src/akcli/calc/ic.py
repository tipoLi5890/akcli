"""Common-IC design calculators: 555 timer, op-amp gain, I²C pull-ups,
crystal load capacitors, junction thermal.

References:

* NE555: Texas Instruments SLFS022I, NA555/NE555/SA555/SE555 datasheet,
  §8.4.1.2 (astable: f = 1.44/((R_A+2R_B)·C), duty = (R_A+R_B)/(R_A+2R_B)),
  §8.4.1.1 (monostable: t = 1.1·R·C).
* Op-amp: Texas Instruments SLOD006B, *Op Amps for Everyone* (2008), ch. 3
  (inverting G = −R_f/R_in; non-inverting G = 1 + R_f/R_g).
* I²C pull-up: NXP UM10204 Rev. 7 (2021), *I²C-bus specification*, §7.1:
  R_p(min) = (V_DD − V_OL(max)=0.4 V)/I_OL(3 mA);
  R_p(max) = t_r/(0.8473·C_b); t_r = 1000/300/120 ns for standard/fast/
  fast-mode-plus.
* Crystal: STMicroelectronics AN2867, *Oscillator design guide* (2021), §3.4:
  C_L = C_L1·C_L2/(C_L1+C_L2) + C_stray ⇒ C_L1 = C_L2 = 2·(C_L − C_stray).
* Thermal: JEDEC JESD51-2A / EIA/JESD51 definitions:
  T_j = T_a + P·θ_JA; required heatsink θ_SA = (T_j−T_a)/P − θ_JC − θ_CS.
"""

from __future__ import annotations

from .eseries import SERIES, series_values, snap
from .registry import CalcError, Param, Result, register


@register(
    "ne555-astable", "NE555 astable oscillator", "ic",
    "TI SLFS022I NE555 datasheet §8.4.1.2: f = 1.44/((RA+2RB)·C), "
    "duty = (RA+RB)/(RA+2RB)",
    (Param("ra", "Ω", "R_A (VCC→DIS)"),
     Param("rb", "Ω", "R_B (DIS→THR/TRIG)"),
     Param("c", "F", "timing capacitor")),
    notes="Duty (output high) is always > 50 % in this basic topology; "
          "bypass RB with a diode for < 50 %.",
)
def _calc_ne555_astable(ra: float, rb: float, c: float) -> list[Result]:
    if min(ra, rb, c) <= 0:
        raise CalcError("ra, rb, c must be positive")
    f = 1.44 / ((ra + 2 * rb) * c)
    duty = (ra + rb) / (ra + 2 * rb)
    return [Result("frequency", f, "Hz"),
            Result("duty_high", duty * 100, "%"),
            Result("t_high", 0.693 * (ra + rb) * c, "s"),
            Result("t_low", 0.693 * rb * c, "s")]


@register(
    "ne555-mono", "NE555 monostable pulse", "ic",
    "TI SLFS022I NE555 datasheet §8.4.1.1: t = 1.1·R·C",
    (Param("r", "Ω", "timing resistor"),
     Param("c", "F", "timing capacitor")),
)
def _calc_ne555_mono(r: float, c: float) -> list[Result]:
    if r <= 0 or c <= 0:
        raise CalcError("r, c must be positive")
    return [Result("pulse_width", 1.1 * r * c, "s")]


@register(
    "opamp-gain", "Op-amp gain resistor pair", "ic",
    "TI SLOD006B, Op Amps for Everyone (2008), ch. 3; value snapping per "
    "IEC 60063:2015",
    (Param("gain", "", "wanted voltage gain (magnitude)"),
     Param("topology", "", "amplifier form", default="non-inverting",
           choices=("non-inverting", "inverting")),
     Param("r_ref", "Ω", "chosen R_g (non-inv) / R_in (inv)", default=10e3),
     Param("series", "", "E series", default="E96", choices=tuple(SERIES))),
)
def _calc_opamp_gain(gain: float, topology: str, r_ref: float,
                      series: str) -> list[Result]:
    if r_ref <= 0:
        raise CalcError("r_ref must be positive")
    if topology == "non-inverting":
        if gain <= 1:
            raise CalcError("non-inverting gain must be > 1")
        ideal_rf = (gain - 1) * r_ref
    else:
        if gain <= 0:
            raise CalcError("gain must be positive (magnitude)")
        ideal_rf = gain * r_ref
    rf, _ = snap(ideal_rf, series)
    actual = 1 + rf / r_ref if topology == "non-inverting" else rf / r_ref
    return [Result("r_ref", r_ref, "Ω",
                   "R_g (GND leg)" if topology == "non-inverting" else "R_in"),
            Result("rf_ideal", ideal_rf, "Ω"),
            Result("rf_standard", rf, "Ω"),
            Result("gain_actual", actual, "",
                   f"{(actual - gain) / gain * 100:+.4f}% vs target")]


_I2C_TR = {"standard": 1000e-9, "fast": 300e-9, "fast-plus": 120e-9}
_I2C_FMAX = {"standard": 100e3, "fast": 400e3, "fast-plus": 1e6}


@register(
    "i2c-pullup", "I²C bus pull-up resistor window", "interface",
    "NXP UM10204 Rev.7 (2021) §7.1: Rp(min) = (VDD−0.4 V)/3 mA; "
    "Rp(max) = t_r/(0.8473·C_b)",
    (Param("vdd", "V", "bus supply voltage"),
     Param("cb", "F", "total bus capacitance (spec max 400 pF)"),
     Param("mode", "", "bus speed grade", default="fast",
           choices=("standard", "fast", "fast-plus")),
     Param("series", "", "E series", default="E24", choices=tuple(SERIES))),
)
def _calc_i2c_pullup(vdd: float, cb: float, mode: str,
                      series: str) -> list[Result]:
    if vdd <= 0.4 or cb <= 0:
        raise CalcError("need vdd > 0.4 V and cb > 0")
    rmin = (vdd - 0.4) / 3e-3
    rmax = _I2C_TR[mode] / (0.8473 * cb)
    if rmax < rmin:
        raise CalcError(
            f"no valid pull-up: Rp(max)={rmax:.0f}Ω < Rp(min)={rmin:.0f}Ω — "
            "reduce bus capacitance or drop the speed grade")
    picks = [v for v in series_values(series, rmin, rmax)]
    return [Result("r_min", rmin, "Ω", "VOL sink-current limit"),
            Result("r_max", rmax, "Ω", f"rise-time @ {mode} mode"),
            Result("f_max", _I2C_FMAX[mode], "Hz"),
            Result("suggested", picks[len(picks) // 2] if picks else None, "Ω",
                   f"{series} value inside the window")]


@register(
    "crystal-caps", "Crystal load capacitors", "ic",
    "ST AN2867 (2021) §3.4: C_L1 = C_L2 = 2·(C_L − C_stray)",
    (Param("cl", "F", "crystal load capacitance (datasheet)"),
     Param("cstray", "F", "board + pin stray capacitance", default=3e-12),
     Param("series", "", "E series", default="E12", choices=tuple(SERIES))),
    notes="Cstray is typically 2–5 pF; verify oscillation margin (gm) per "
          "AN2867 §4 for low-power oscillators.",
)
def _calc_crystal_caps(cl: float, cstray: float, series: str) -> list[Result]:
    ideal = 2 * (cl - cstray)
    if ideal <= 0:
        raise CalcError("C_L must exceed C_stray")
    std, err = snap(ideal, series)
    return [Result("c1_c2_ideal", ideal, "F", "both capacitors equal"),
            Result("c1_c2_standard", std, "F", f"{series} ({err:+.2f}%)"),
            Result("cl_actual", std / 2 + cstray, "F")]


@register(
    "thermal", "Junction temperature / required heatsink", "ic",
    "JEDEC JESD51-2A definitions: Tj = Ta + P·θJA; "
    "θSA = (Tj−Ta)/P − θJC − θCS",
    (Param("p", "W", "dissipated power"),
     Param("ta", "°C", "ambient temperature", default=25.0),
     Param("theta_ja", "°C/W", "junction-to-ambient (no heatsink; 0 = skip)",
           default=0.0),
     Param("tj_max", "°C", "junction limit for heatsink sizing", default=125.0),
     Param("theta_jc", "°C/W", "junction-to-case", default=5.0),
     Param("theta_cs", "°C/W", "case-to-sink (pad/grease)", default=0.5)),
)
def _calc_thermal(p: float, ta: float, theta_ja: float, tj_max: float,
                   theta_jc: float, theta_cs: float) -> list[Result]:
    if p <= 0:
        raise CalcError("power must be positive")
    out: list[Result] = []
    if theta_ja > 0:
        out.append(Result("tj_no_heatsink", ta + p * theta_ja, "°C",
                          "compare against the device Tj(max)"))
    budget = (tj_max - ta) / p
    theta_sa = budget - theta_jc - theta_cs
    out += [Result("theta_total_budget", budget, "°C/W"),
            Result("theta_sa_required", theta_sa, "°C/W",
                   "heatsink must be at or below this" if theta_sa > 0
                   else "IMPOSSIBLE — reduce power or Ta")]
    return out
