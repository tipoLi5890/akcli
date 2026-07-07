"""Protection-component sizing: TVS, fuse derating, NTC inrush limiter.

References:

* TVS: device selection per Littelfuse *TVS Diode Selection Guide* (AN9768-
  style practice): V_RWM ≥ line max, V_C(max) < protected-pin absolute max;
  surge current from the IEC 61000-4-5 combination-wave generator
  (1.2/50 µs–8/20 µs, 2 Ω effective source, line-to-line).
* Fuse: continuous derating to ≤75 % of rating per Littelfuse *Fuseology*
  fuse-selection guide; standard current ratings follow the IEC 60127 R10
  ladder (1 / 1.25 / 1.6 / 2 / 2.5 / 3.15 / 4 / 5 / 6.3 / 8 ×10ⁿ).
* NTC inrush limiter: selection per TDK/EPCOS *NTC thermistors for inrush
  current limiting* application guide — cold resistance bounds the first
  peak (R ≥ V_pk/I_max); the part must absorb the bulk-capacitor energy
  E = ½·C·V_pk².
"""

from __future__ import annotations

import math

from .registry import CalcError, Param, Result, register

_R10 = (1.0, 1.25, 1.6, 2.0, 2.5, 3.15, 4.0, 5.0, 6.3, 8.0)


@register(
    "tvs", "TVS diode selection check", "protection",
    "Littelfuse TVS Diode Selection Guide practice; surge per IEC 61000-4-5 "
    "combination wave (8/20 µs, 2 Ω line-to-line source)",
    (Param("v_line_max", "V", "max normal operating voltage on the line"),
     Param("v_ic_absmax", "V", "protected pin absolute-maximum voltage"),
     Param("v_clamp", "V", "TVS clamping voltage at rated Ipp (datasheet)"),
     Param("v_surge", "V", "surge open-circuit voltage", default=1000.0),
     Param("z_source", "Ω", "surge source impedance", default=2.0)),
)
def _calc_tvs(v_line_max, v_ic_absmax, v_clamp, v_surge, z_source) -> list[Result]:
    if v_clamp <= v_line_max:
        raise CalcError("v_clamp must exceed v_line_max (TVS would conduct "
                        "in normal operation) — check V_RWM instead")
    ipp = (v_surge - v_clamp) / z_source
    return [Result("v_rwm_needed", v_line_max, "V",
                   "pick V_RWM at or above this"),
            Result("i_pp", max(ipp, 0.0), "A", "peak pulse current (8/20 µs)"),
            Result("p_pk", max(ipp, 0.0) * v_clamp, "W",
                   "compare with the 8/20 µs P_pk rating"),
            Result("clamp_ok", v_clamp < v_ic_absmax),
            Result("clamp_margin", v_ic_absmax - v_clamp, "V",
                   "abs-max minus clamp; negative = part damaged")]


@register(
    "fuse-derating", "Fuse rating for a continuous load", "protection",
    "Littelfuse Fuseology selection guide (≤75 % continuous); standard "
    "ratings per IEC 60127 R10 series",
    (Param("i_load", "A", "continuous load current"),
     Param("derate", "", "continuous derating factor", default=0.75),
     Param("temp_factor", "", "thermal rerating at ambient (1.0 = 23 °C)",
           default=1.0)),
    notes="Also verify I²t: fuse I²t rating must exceed the inrush surge "
          "I²t, and interrupting rating must exceed the prospective fault "
          "current — both from datasheets, not computed here.",
)
def _calc_fuse(i_load, derate, temp_factor) -> list[Result]:
    if i_load <= 0 or not 0 < derate <= 1 or not 0 < temp_factor <= 1.2:
        raise CalcError("bad i_load/derate/temp_factor")
    need = i_load / (derate * temp_factor)
    pick = None
    for dec in (0.1, 1.0, 10.0, 100.0):
        for m in _R10:
            v = m * dec
            if v >= need:
                pick = v
                break
        if pick:
            break
    return [Result("i_rating_min", need, "A"),
            Result("suggested", pick, "A",
                   "next IEC 60127 R10 rating" if pick else "out of range")]


@register(
    "inrush-ntc", "NTC inrush-limiter sizing", "protection",
    "TDK/EPCOS NTC inrush-current-limiting application guide: "
    "R_cold ≥ V_pk/I_max; energy rating ≥ ½·C·V_pk²",
    (Param("v_supply", "V", "supply voltage (RMS if AC)"),
     Param("ac", "", "supply type", default="ac", choices=("ac", "dc")),
     Param("i_inrush_max", "A", "allowed first-peak current"),
     Param("c_bulk", "F", "bulk capacitance being charged"),
     Param("i_steady", "A", "steady-state RMS current", default=0.0),
     Param("r_hot", "Ω", "NTC hot resistance (datasheet)", default=0.0)),
)
def _calc_inrush(v_supply, ac, i_inrush_max, c_bulk, i_steady, r_hot) -> list[Result]:
    if min(v_supply, i_inrush_max, c_bulk) <= 0:
        raise CalcError("v_supply, i_inrush_max, c_bulk must be positive")
    v_pk = v_supply * (math.sqrt(2) if ac == "ac" else 1.0)
    out = [Result("v_peak", v_pk, "V"),
           Result("r_cold_min", v_pk / i_inrush_max, "Ω",
                  "NTC cold resistance at or above this"),
           Result("energy", 0.5 * c_bulk * v_pk * v_pk, "",
                  "J — NTC joule rating must exceed this")]
    if i_steady > 0 and r_hot > 0:
        out.append(Result("p_steady", i_steady ** 2 * r_hot, "W",
                          "continuous dissipation at R_hot"))
    return out
