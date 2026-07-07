"""Bus-interface hardware: RS-485 fail-safe bias, CAN termination.

References:

* RS-485: TIA/EIA-485-A (receiver threshold ±200 mV); fail-safe biasing per
  TI SLLA070D, *RS-422 and RS-485 Standards Overview* — the idle bus must be
  held at V_AB ≥ 200 mV through the parallel termination.
* CAN: ISO 11898-2:2016 (high-speed CAN) — 120 Ω at each cable end; split
  termination (2 × 60 Ω + common-mode capacitor) per ISO 11898-2 §10 /
  TI SLLA270 for EMC.
"""

from __future__ import annotations

import math

from .eseries import series_values
from .registry import CalcError, Param, Result, register


@register(
    "rs485-bias", "RS-485 termination + fail-safe bias", "interface",
    "TIA/EIA-485-A (±200 mV receiver threshold); fail-safe bias per "
    "TI SLLA070D; value pick per IEC 60063 E24",
    (Param("vcc", "V", "bias supply"),
     Param("z0", "Ω", "cable impedance / termination each end", default=120.0),
     Param("n_term", "", "number of terminations on the bus", default=2.0),
     Param("v_ab_min", "V", "required idle differential", default=0.2)),
    notes="Bias network: R_up from VCC to A, R_down from B to GND, one place "
          "on the bus. Each bias resistor also counts toward the unit-load "
          "budget of the driver.",
)
def _calc_rs485(vcc, z0, n_term, v_ab_min) -> list[Result]:
    if min(vcc, z0, n_term, v_ab_min) <= 0:
        raise CalcError("all parameters must be positive")
    r_par = z0 / n_term                       # parallel terminations
    if vcc <= v_ab_min:
        raise CalcError("vcc must exceed v_ab_min")
    total_max = r_par * (vcc - v_ab_min) / v_ab_min   # R_up + R_down
    each_max = total_max / 2
    picks = series_values("E24", each_max / 3, each_max)
    pick = picks[-1] if picks else None
    v_idle = vcc * r_par / (2 * pick + r_par) if pick else None
    return [Result("r_term", z0, "Ω", "one at EACH cable end"),
            Result("r_parallel", r_par, "Ω", "all terminations in parallel"),
            Result("r_bias_each_max", each_max, "Ω", "R_up = R_down upper bound"),
            Result("suggested", pick, "Ω", "largest E24 value that still biases"),
            Result("v_ab_idle", v_idle, "V", "idle differential with suggestion")]


@register(
    "can-termination", "CAN bus split termination", "interface",
    "ISO 11898-2:2016 §10 (120 Ω each end; split 2×60 Ω + C_split for EMC); "
    "cf. TI SLLA270",
    (Param("z0", "Ω", "cable impedance", default=120.0),
     Param("c_split", "F", "split capacitor to GND", default=4.7e-9)),
)
def _calc_can(z0, c_split) -> list[Result]:
    if z0 <= 0 or c_split <= 0:
        raise CalcError("z0 and c_split must be positive")
    half = z0 / 2
    fc = 1 / (2 * math.pi * (half / 2) * c_split)
    return [Result("r_term", z0, "Ω", "standard single termination, each end"),
            Result("r_split_each", half, "Ω", "split pair (CANH–C–CANL)"),
            Result("c_split", c_split, "F", "center tap to GND"),
            Result("f_cm_corner", fc, "Hz",
                   "common-mode low-pass corner of the split network")]
