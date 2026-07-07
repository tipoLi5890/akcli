"""Component marking codes and galvanic compatibility.

References:

* Resistor color bands and R/K/M decimal marking: IEC 60062:2016, *Marking
  codes for resistors and capacitors* (digit colors, multiplier, tolerance
  letters).
* SMD 3-/4-digit codes: IEC 60062:2016 numeric marking; EIA-96 1%-resistor
  code (2 digits = E96 index, letter = multiplier) per EIA standard practice.
* Galvanic corrosion: MIL-STD-889C (2016), *Dissimilar Metals* — anodic
  index; compatibility thresholds 0.15 V (harsh/marine), 0.25 V (normal
  industrial), 0.50 V (controlled environments) per common application of
  Table I.
"""

from __future__ import annotations

import math

from .eseries import SERIES
from .registry import CalcError, Param, Result, register
from .si import fmt_eng, parse_value

_COLORS = ("black", "brown", "red", "orange", "yellow",
           "green", "blue", "violet", "grey", "white")
_TOL_COLOR = {1: "brown", 2: "red", 0.5: "green", 0.25: "blue",
              0.1: "violet", 5: "gold", 10: "silver"}


@register(
    "rescolor", "Resistor color bands for a value", "codes",
    "IEC 60062:2016, Marking codes for resistors and capacitors",
    (Param("value", "Ω", "resistance"),
     Param("tolerance", "%", "tolerance band", default=1.0),
     Param("bands", "", "band count", default="5", choices=("4", "5"))),
)
def _calc_rescolor(value, tolerance, bands) -> list[Result]:
    if value <= 0:
        raise CalcError("value must be positive")
    ndig = 2 if bands == "4" else 3
    exp = math.floor(math.log10(value)) - (ndig - 1)
    digits = round(value / 10 ** exp)
    if digits >= 10 ** ndig:      # e.g. 999.6 rounds up a decade
        digits //= 10
        exp += 1
    if not -2 <= exp <= 9:
        raise CalcError("value out of color-code range")
    digit_names = [_COLORS[int(ch)] for ch in str(digits).zfill(ndig)]
    mult = {-2: "silver", -1: "gold"}.get(exp, _COLORS[exp] if exp >= 0 else "?")
    tol = _TOL_COLOR.get(tolerance)
    if tol is None:
        raise CalcError(f"no color for {tolerance:g}% "
                        f"(known: {sorted(_TOL_COLOR)})")
    coded = digits * 10 ** exp
    return [Result("bands_list", digit_names + [mult, tol]),
            Result("encoded_value", coded, "Ω", fmt_eng(coded, "Ω"))]


# EIA-96: 2-digit code 01..96 -> E96 value; letter -> multiplier
_EIA96_MULT = {"Z": 0.001, "Y": 0.01, "R": 0.01, "X": 0.1, "S": 0.1,
               "A": 1.0, "B": 10.0, "H": 10.0, "C": 100.0, "D": 1e3,
               "E": 1e4, "F": 1e5}


@register(
    "smdcode", "Decode an SMD resistor marking", "codes",
    "IEC 60062:2016 numeric marking (3/4-digit, R decimal); EIA-96 1% code "
    "(2-digit E96 index + multiplier letter)",
    (Param("code", "", "printed marking, e.g. 472, 4R7, 1002, 01C",
           text=True),),
)
def _calc_smdcode(code) -> list[Result]:
    s = str(code).strip().upper()
    if not s:
        raise CalcError("give code=<marking>")
    if s.startswith("R"):
        s = "0" + s
    # EIA-96: exactly 2 digits + 1 multiplier letter
    if len(s) == 3 and s[:2].isdigit() and s[2] in _EIA96_MULT:
        idx = int(s[:2])
        if 1 <= idx <= 96:
            base = SERIES["E96"][idx - 1] * 100     # 100..976
            v = base * _EIA96_MULT[s[2]]
            return [Result("value", v, "Ω", fmt_eng(v, "Ω")),
                    Result("system", "EIA-96", "", "1 % (E96) marking")]
    if "R" in s:                                    # 4R7 / R47 / 47R
        v = parse_value(s if s[-1] != "R" else s + "0", "code")
        return [Result("value", v, "Ω", fmt_eng(v, "Ω")),
                Result("system", "IEC 60062 R-decimal")]
    if s.isdigit() and len(s) in (3, 4):
        v = int(s[:-1]) * 10 ** int(s[-1])
        sys_name = f"{len(s)}-digit"
        return [Result("value", float(v), "Ω", fmt_eng(v, "Ω")),
                Result("system", sys_name, "",
                       "significant digits × 10^last")]
    raise CalcError(f"unrecognized marking {code!r}")


# MIL-STD-889C Table I anodic index (V, versus gold)
_ANODIC = {
    "gold": 0.00, "silver": 0.15, "nickel": 0.30, "copper": 0.35,
    "brass": 0.40, "bronze": 0.40, "tin": 0.65, "solder-snpb": 0.65,
    "lead": 0.70, "steel": 0.85, "aluminum": 0.90, "aluminum-alloy": 0.95,
    "cadmium": 0.95, "galvanized-steel": 1.20, "zinc": 1.25,
    "magnesium": 1.75,
}
_ENV_LIMIT = {"harsh": 0.15, "normal": 0.25, "controlled": 0.50}


@register(
    "galvanic", "Galvanic (dissimilar-metal) compatibility", "codes",
    "MIL-STD-889C (2016) Table I anodic index; thresholds 0.15/0.25/0.50 V "
    "for harsh/normal/controlled environments",
    (Param("metal1", "", "first metal", default="",
           choices=tuple(sorted(_ANODIC))),
     Param("metal2", "", "second metal", default="",
           choices=tuple(sorted(_ANODIC))),
     Param("environment", "", "service environment", default="normal",
           choices=tuple(_ENV_LIMIT))),
)
def _calc_galvanic(metal1, metal2, environment) -> list[Result]:
    if not metal1 or not metal2:
        raise CalcError(f"give metal1= and metal2= from {sorted(_ANODIC)}")
    v1, v2 = _ANODIC[metal1], _ANODIC[metal2]
    dv = abs(v1 - v2)
    limit = _ENV_LIMIT[environment]
    anode = metal1 if v1 > v2 else metal2
    return [Result("delta_v", round(dv, 3), "V", "anodic-index difference"),
            Result("limit", limit, "V", f"{environment} environment"),
            Result("compatible", dv <= limit),
            Result("corroding_metal", anode, "",
                   "the more anodic metal corrodes first")]
