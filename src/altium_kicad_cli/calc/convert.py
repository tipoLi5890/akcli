"""Unit conversions engineers reach for constantly.

References:

* Length: 1 inch = 25.4 mm **exactly** (International Yard and Pound
  Agreement, 1959; NIST SP 811 §B.6). 1 mil = 1/1000 inch.
* Power/level: dBm is dB referenced to 1 mW (IEEE Std 100); P = 10^((dBm−30)/10) W;
  V_rms = √(P·Z0).
* Copper weight: 1 oz/ft² foil — industry nominal thickness 1.37 mil
  (34.8 µm), the value IPC-2221/IPC-4562 usage assumes; the pure-copper
  physical equivalent is 34.06 µm (28.3495 g over 929.03 cm² at
  ρ = 8.96 g/cm³). Both are reported.
"""

from __future__ import annotations

import math

from .registry import CalcError, Param, Result, register

_IN = 25.4e-3          # m, exact
_OZ_UM_NOMINAL = 34.80     # µm per oz/ft² (1.37 mil industry nominal)
_OZ_UM_PURE = 34.06        # µm per oz/ft² (pure Cu, ρ = 8.96 g/cm³)


@register(
    "convert-length", "Length: mm ↔ mil ↔ inch ↔ µm", "convert",
    "1 in = 25.4 mm exactly (International Yard & Pound Agreement 1959; "
    "NIST SP 811 §B.6)",
    (Param("value", "", "the number to convert"),
     Param("unit", "", "unit of the given value", default="mm",
           choices=("mm", "mil", "inch", "um", "m"))),
)
def _calc_convert_length(value: float, unit: str) -> list[Result]:
    meters = value * {"mm": 1e-3, "mil": _IN / 1000, "inch": _IN,
                      "um": 1e-6, "m": 1.0}[unit]
    return [Result("m", meters, "m"),
            Result("mm", meters * 1e3, ""),
            Result("um", meters * 1e6, ""),
            Result("mil", meters / (_IN / 1000), ""),
            Result("inch", meters / _IN, "")]


@register(
    "convert-power", "Level: dBm ↔ W ↔ V(rms) at Z0", "convert",
    "dBm = dB re 1 mW (IEEE Std 100); P = 10^((dBm−30)/10) W; Vrms = √(P·Z0)",
    (Param("dbm", "dBm", "level (give exactly one of dbm/w/vrms)", default=0.0),
     Param("w", "W", "power", default=0.0),
     Param("vrms", "V", "RMS voltage across Z0", default=0.0),
     Param("z0", "Ω", "reference impedance", default=50.0)),
)
def _calc_convert_power(dbm: float, w: float, vrms: float, z0: float) -> list[Result]:
    given = [n for n, v in (("w", w), ("vrms", vrms)) if v]
    if len(given) > 1:
        raise CalcError("give only one of dbm / w / vrms")
    if w:
        p = w
    elif vrms:
        p = vrms * vrms / z0
    else:
        p = 10 ** ((dbm - 30) / 10)
    return [Result("dbm", 10 * math.log10(p) + 30, "dBm"),
            Result("w", p, "W"),
            Result("vrms", math.sqrt(p * z0), "V", f"across {z0:g} Ω"),
            Result("vpeak", math.sqrt(2 * p * z0), "V", "sine peak"),
            Result("vpp", 2 * math.sqrt(2 * p * z0), "V", "sine peak-to-peak")]


@register(
    "convert-copper", "Copper weight: oz/ft² ↔ thickness", "convert",
    "Industry nominal 1 oz/ft² = 1.37 mil = 34.8 µm (IPC-2221/IPC-4562 "
    "usage); pure-Cu physical equivalent 34.06 µm (ρ = 8.96 g/cm³)",
    (Param("oz", "oz/ft²", "copper weight (0 if giving thickness)", default=0.0),
     Param("um", "µm", "thickness in µm (0 if giving oz)", default=0.0)),
)
def _calc_convert_copper(oz: float, um: float) -> list[Result]:
    if (oz > 0) == (um > 0):
        raise CalcError("give exactly one of oz / um")
    if um:
        oz = um / _OZ_UM_NOMINAL
    t_nom = oz * _OZ_UM_NOMINAL
    return [Result("oz", oz, "oz/ft²"),
            Result("thickness_nominal", t_nom * 1e-6, "m",
                   f"{t_nom:.1f} µm = {t_nom / 25.4:.2f} mil (industry nominal)"),
            Result("thickness_pure_cu", oz * _OZ_UM_PURE * 1e-6, "m",
                   "from areal density at ρ = 8.96 g/cm³")]
