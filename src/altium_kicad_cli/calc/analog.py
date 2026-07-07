"""Analog building blocks: comparator hysteresis, Sallen–Key filter, ADC.

References:

* Comparator hysteresis (inverting, 3-resistor reference node): Texas
  Instruments SLVA954, *Analog Engineer's Circuit: Inverting comparator with
  hysteresis*. Node equation V+ = (VCC·G1 + VOUT·Gh)/(G1+G2+Gh) with R1 to
  VCC, R2 to GND, Rh to the output; thresholds at VOUT = VOH / VOL.
* Sallen–Key: R.P. Sallen & E.L. Key, "A Practical Method of Designing RC
  Active Filters", IRE Trans. Circuit Theory (1955); design equations per
  TI SLOA024B, *Analysis of the Sallen-Key Architecture* — equal-component
  low-pass: f_c = 1/(2πRC), Q = 1/(3−K), Butterworth needs K = 1.586.
* ADC: ideal quantization SNR = 6.02·N + 1.76 dB (W.R. Bennett 1948; Analog
  Devices MT-001); input settling to ½ LSB needs k = ln(2^(N+1)) time
  constants (ADC datasheet acquisition-time practice).
"""

from __future__ import annotations

import math

from .eseries import SERIES, snap
from .registry import CalcError, Param, Result, register

_HYST_REF = ("TI SLVA954, Analog Engineer's Circuit: Inverting comparator "
             "with hysteresis")


def _hyst_thresholds(vcc, r1, r2, rh, voh, vol):
    g1, g2, gh = 1 / r1, 1 / r2, 1 / rh
    s = g1 + g2 + gh
    vt_hi = (vcc * g1 + voh * gh) / s
    vt_lo = (vcc * g1 + vol * gh) / s
    return vt_hi, vt_lo


@register(
    "hysteresis", "Comparator hysteresis thresholds (analysis)", "ic",
    _HYST_REF,
    (Param("vcc", "V", "reference/pull-up supply"),
     Param("r1", "Ω", "node resistor to VCC"),
     Param("r2", "Ω", "node resistor to GND"),
     Param("rh", "Ω", "feedback resistor to comparator output"),
     Param("voh", "V", "output high level (0 = VCC)", default=0.0),
     Param("vol", "V", "output low level", default=0.0)),
    notes="Inverting topology: signal on IN−, this 3-resistor node on IN+.",
)
def _calc_hysteresis(vcc, r1, r2, rh, voh, vol) -> list[Result]:
    if min(r1, r2, rh) <= 0 or vcc <= 0:
        raise CalcError("vcc and all resistors must be positive")
    voh = voh or vcc
    vt_hi, vt_lo = _hyst_thresholds(vcc, r1, r2, rh, voh, vol)
    return [Result("vt_rising", vt_hi, "V", "threshold while output is high"),
            Result("vt_falling", vt_lo, "V", "threshold while output is low"),
            Result("hysteresis", vt_hi - vt_lo, "V")]


@register(
    "hysteresis-design", "Comparator hysteresis resistor pair (design)", "ic",
    _HYST_REF + "; value snapping per IEC 60063:2015",
    (Param("vcc", "V", "reference/pull-up supply"),
     Param("vt_rising", "V", "wanted upper threshold"),
     Param("vt_falling", "V", "wanted lower threshold"),
     Param("r1", "Ω", "chosen node resistor to VCC", default=100e3),
     Param("series", "", "E series", default="E96", choices=tuple(SERIES))),
    notes="Assumes rail-to-rail output (VOH = VCC, VOL = 0).",
)
def _calc_hysteresis_design(vcc, vt_rising, vt_falling, r1, series) -> list[Result]:
    if not 0 < vt_falling < vt_rising < vcc:
        raise CalcError("need 0 < vt_falling < vt_rising < vcc")
    g1 = 1 / r1
    s = g1 / (vt_falling / vcc)            # VT− = VCC·G1/S (VOL = 0)
    gh = s * (vt_rising - vt_falling) / vcc
    g2 = s - g1 - gh
    if g2 <= 0:
        raise CalcError("no solution with this r1 — thresholds too far apart; "
                        "reduce hysteresis or r1")
    rh_i, r2_i = 1 / gh, 1 / g2
    rh_std, _ = snap(rh_i, series)
    r2_std, _ = snap(r2_i, series)
    hi, lo = _hyst_thresholds(vcc, r1, r2_std, rh_std, vcc, 0.0)
    return [Result("r1", r1, "Ω", "to VCC (chosen)"),
            Result("r2_ideal", r2_i, "Ω"), Result("r2_standard", r2_std, "Ω"),
            Result("rh_ideal", rh_i, "Ω"), Result("rh_standard", rh_std, "Ω"),
            Result("vt_rising_actual", hi, "V"),
            Result("vt_falling_actual", lo, "V")]


@register(
    "sallen-key", "Sallen–Key low-pass (equal component) design", "ic",
    "Sallen & Key, IRE Trans. Circuit Theory (1955); TI SLOA024B: "
    "fc = 1/(2πRC), Q = 1/(3−K); Butterworth K = 1.586",
    (Param("fc", "Hz", "wanted −3 dB corner"),
     Param("c", "F", "chosen capacitor (C1 = C2)"),
     Param("q", "", "filter Q (0.7071 = Butterworth)", default=0.7071),
     Param("r_gain", "Ω", "gain-network Rg (op-amp − to GND)", default=10e3),
     Param("series", "", "E series for resistors", default="E96",
           choices=tuple(SERIES))),
    notes="Equal-component variant: R1 = R2, C1 = C2; gain K = 3 − 1/Q sets "
          "Q, so the amp runs at K ≈ 1.59 for Butterworth.",
)
def _calc_sallen_key(fc, c, q, r_gain, series) -> list[Result]:
    if fc <= 0 or c <= 0 or q <= 0:
        raise CalcError("fc, c, q must be positive")
    k = 3 - 1 / q
    if k < 1:
        raise CalcError("q too low for the equal-component topology (K < 1)")
    r_i = 1 / (2 * math.pi * fc * c)
    r_std, _ = snap(r_i, series)
    rf_i = (k - 1) * r_gain
    rf_std, _ = snap(rf_i, series) if rf_i > 0 else (0.0, 0.0)
    fc_act = 1 / (2 * math.pi * r_std * c)
    return [Result("r_ideal", r_i, "Ω", "R1 = R2"),
            Result("r_standard", r_std, "Ω"),
            Result("fc_actual", fc_act, "Hz"),
            Result("gain_k", k, "", f"Q = {q:g}"),
            Result("rf_standard", rf_std, "Ω", f"with Rg = {r_gain:g} Ω"),
            Result("q_actual", 1 / (3 - (1 + (rf_std / r_gain if r_gain else 0))), "")]


@register(
    "adc", "ADC resolution / SNR / input settling", "ic",
    "Ideal SNR = 6.02·N + 1.76 dB (Bennett 1948; Analog Devices MT-001); "
    "settling to ½ LSB in k = ln(2^(N+1)) time constants",
    (Param("bits", "", "resolution N"),
     Param("vref", "V", "full-scale reference", default=3.3),
     Param("r_source", "Ω", "source impedance (0 = skip settling)", default=0.0),
     Param("c_sample", "F", "sample/input capacitance", default=0.0)),
)
def _calc_adc(bits, vref, r_source, c_sample) -> list[Result]:
    n = int(bits)
    if not 1 <= n <= 32:
        raise CalcError("bits must be 1..32")
    lsb = vref / (2 ** n)
    out = [Result("lsb", lsb, "V"),
           Result("counts", float(2 ** n), ""),
           Result("snr_ideal", 6.02 * n + 1.76, "dB", "quantization limit"),
           Result("dynamic_range", 20 * math.log10(2 ** n), "dB")]
    if r_source > 0 and c_sample > 0:
        k = math.log(2 ** (n + 1))
        out.append(Result("t_settle", k * r_source * c_sample, "s",
                          f"{k:.2f}·τ to reach ½ LSB"))
    return out
