"""Voltage-regulator feedback network calculators.

Two topologies:

* ``adj3`` — 3-terminal adjustable regulator (LM317 family):
  ``Vout = Vref·(1 + R2/R1) + Iadj·R2`` with R1 from OUT to ADJ and R2 from
  ADJ to GND. Reference: TI LM317 datasheet, SLVS044Y (2020), §8.2
  ("Typical Application", output-voltage equation); Vref 1.20–1.30 V
  (1.25 typ), Iadj 50 µA typ / 100 µA max per §6.5.
* ``fb`` — fixed-reference FB-pin divider (most LDOs / switchers):
  ``Vout = Vref·(1 + R_top/R_bottom)``, R_top from VOUT to FB, R_bottom FB to
  GND. Reference: e.g. TI SLVA966 (feedback-divider design) — the equation is
  the plain divider inverse.

Worst-case analysis enumerates every corner of Vref, Iadj and the resistor
tolerance band (exhaustive corner evaluation — no monotonicity assumptions).
"""

from __future__ import annotations

import itertools

from .eseries import SERIES, series_values, snap
from .registry import CalcError, Param, Result, register

_REF = ("TI LM317 datasheet SLVS044Y §8.2 (adj3); FB-divider per plain "
        "divider inverse, cf. TI SLVA966 (fb)")


def _vout(kind: str, r1: float, r2: float, vref: float, iadj: float) -> float:
    if kind == "adj3":
        return vref * (1 + r2 / r1) + iadj * r2
    return vref * (1 + r1 / r2)   # fb: r1 = top, r2 = bottom


def _corners(kind, r1, r2, tol_pct, vref_min, vref_max, iadj_min, iadj_max):
    t = tol_pct / 100.0
    vals = []
    for f1, f2, vr, ia in itertools.product(
            (1 - t, 1 + t), (1 - t, 1 + t),
            (vref_min, vref_max), (iadj_min, iadj_max)):
        vals.append(_vout(kind, r1 * f1, r2 * f2, vr, ia))
    return min(vals), max(vals)


@register(
    "regulator", "Adjustable-regulator output voltage (worst case)", "power",
    _REF,
    (Param("kind", "", "topology", default="adj3", choices=("adj3", "fb")),
     Param("r1", "Ω", "adj3: OUT→ADJ resistor; fb: VOUT→FB (top)"),
     Param("r2", "Ω", "adj3: ADJ→GND resistor; fb: FB→GND (bottom)"),
     Param("vref", "V", "reference voltage (typ)", default=1.25),
     Param("vref_min", "V", "reference min (0 = use typ)", default=0.0),
     Param("vref_max", "V", "reference max (0 = use typ)", default=0.0),
     Param("iadj", "A", "ADJ pin current typ (adj3 only)", default=50e-6),
     Param("iadj_max", "A", "ADJ pin current max", default=100e-6),
     Param("tol", "%", "resistor tolerance", default=1.0)),
)
def _calc_regulator(kind, r1, r2, vref, vref_min, vref_max, iadj, iadj_max,
                    tol) -> list[Result]:
    if r1 <= 0 or r2 <= 0:
        raise CalcError("resistors must be positive")
    if kind == "fb":
        iadj = iadj_max = 0.0
    vmin_ref = vref_min or vref
    vmax_ref = vref_max or vref
    typ = _vout(kind, r1, r2, vref, iadj)
    lo, hi = _corners(kind, r1, r2, tol, vmin_ref, vmax_ref,
                      0.0 if kind == "adj3" else 0.0, iadj_max)
    return [Result("vout_typ", typ, "V"),
            Result("vout_min", lo, "V", "worst-case corner"),
            Result("vout_max", hi, "V", "worst-case corner"),
            Result("tolerance_pct", (hi - lo) / (2 * typ) * 100, "%",
                   "± about typ")]


@register(
    "regulator-design", "Feedback resistor pair for a target Vout", "power",
    _REF + "; value snapping per IEC 60063:2015",
    (Param("kind", "", "topology", default="adj3", choices=("adj3", "fb")),
     Param("vout", "V", "wanted output voltage"),
     Param("vref", "V", "reference voltage (typ)", default=1.25),
     Param("iadj", "A", "ADJ pin current typ (adj3 only)", default=50e-6),
     Param("r_fixed", "Ω", "chosen fixed resistor (adj3: R1; fb: R_bottom)",
           default=240.0),
     Param("series", "", "E series", default="E96", choices=tuple(SERIES))),
)
def _calc_regulator_design(kind, vout, vref, iadj, r_fixed, series) -> list[Result]:
    if vout <= vref:
        raise CalcError(f"vout must exceed vref ({vref:g} V)")
    if kind == "adj3":
        # solve R2 from Vout = Vref(1 + R2/R1) + Iadj·R2
        ideal = (vout - vref) / (vref / r_fixed + iadj)
    else:
        ideal = r_fixed * (vout - vref) / vref     # top resistor
        iadj = 0.0
    r_std, _ = snap(ideal, series)
    actual = _vout(kind, r_fixed if kind == "adj3" else r_std,
                   r_std if kind == "adj3" else r_fixed, vref, iadj)
    other = "r2" if kind == "adj3" else "r_top"
    return [Result("r_fixed", r_fixed, "Ω",
                   "R1 (OUT→ADJ)" if kind == "adj3" else "R_bottom (FB→GND)"),
            Result(f"{other}_ideal", ideal, "Ω"),
            Result(f"{other}_standard", r_std, "Ω"),
            Result("vout_actual", actual, "V",
                   f"{(actual - vout) / vout * 100:+.4f}% vs target")]
