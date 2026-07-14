"""PCB physical-design calculators: track width, clearance, via, fusing, AWG.

References (per calculator, also stamped on every result):

* Track width ↔ current: IPC-2221B (2012), *Generic Standard on Printed
  Board Design*, §6.2 / Figure 6-4 curve fit
  ``I = k·ΔT^0.44·A^0.725`` (A in mil², k = 0.048 external / 0.024 internal).
  Valid to ~35 A external / 17.5 A internal, ΔT ≤ 100 °C, width ≤ 400 mil.
  (IPC-2152 (2009) supersedes the charts with measured data; the 2221 fit is
  the industry-common conservative form and what KiCad uses.)
* Electrical clearance: IPC-2221B Table 6-1 (bare/coated board, sea level /
  >3050 m columns B1–B4, A5–A7). Values > 500 V use the per-volt slopes from
  the same table.
* Via: barrel resistance R = ρL/A with A = π/4·((d+2t)² − d²); thermal
  resistance Rθ = L/(λ_Cu·A), λ_Cu = 401 W/(m·K) (CRC Handbook); ampacity via
  the IPC-2221B §6.2 fit on the barrel cross-section; parasitic inductance
  L[nH] = 5.08·h[in]·(ln(4h/d) + 1) and capacitance
  C[pF] = 1.41·εr·T·D1/(D2 − D1) per H. Johnson & M. Graham, *High-Speed
  Digital Design: A Handbook of Black Magic* (Prentice Hall, 1993), §7.
  10–90 % rise-time degradation t_r ≈ 2.2·C·(Z0/2).
* Copper resistivity ρ = 1.72×10⁻⁸ Ω·m at 20 °C (IACS annealed copper,
  ASTM B193); temperature coefficient α = 0.00393 /K.
* Fusing current: I.M. Onderdonk's equation, per E.R. Stauffacher,
  "Short-Time Current Carrying Capacity of Copper Wire", GE Review 31 (1928):
  ``I = A_cmil·√(log₁₀((Tm−Ta)/(234+Ta)+1)/(33·t))``; W.H. Preece,
  Proc. Royal Society 36 (1884): ``I = 10244·d[in]^1.5`` (copper, steady).
* Wire gauge: ASTM B258-18 — d[mm] = 0.127·92^((36−n)/39).
"""

from __future__ import annotations

import math

from .registry import CalcError, Param, Result, register

RHO_CU = 1.72e-8          # Ω·m @20 °C (IACS annealed copper, ASTM B193)
ALPHA_CU = 0.00393        # 1/K
LAMBDA_CU = 401.0         # W/(m·K) thermal conductivity (CRC Handbook)
T_MELT_CU = 1083.0        # °C
_MIL = 25.4e-6            # m
_MIL2 = _MIL * _MIL       # m²

_IPC = "IPC-2221B §6.2 Fig. 6-4 (I = k·ΔT^0.44·A^0.725; k 0.048 ext / 0.024 int)"


def _ipc_area_mil2(i: float, dtemp: float, k: float) -> float:
    return (i / (k * dtemp ** 0.44)) ** (1 / 0.725)


@register(
    "trackwidth", "PCB track width for a current (IPC-2221)", "pcb",
    _IPC + "; ρ_Cu per ASTM B193",
    (Param("i", "A", "continuous current"),
     Param("dtemp", "°C", "allowed temperature rise", default=10.0),
     Param("thickness", "m", "copper thickness (35µ = 1 oz)", default=35e-6),
     Param("length", "m", "track length for R/drop", default=0.02),
     Param("rho", "Ω·m", "conductor resistivity", default=RHO_CU)),
    notes="Fit valid to ΔT ≤ 100 °C, ≤35 A external / 17.5 A internal, "
          "width ≤ 400 mil. IPC-2152 gives measured (less conservative) data.",
)
def _calc_trackwidth(i: float, dtemp: float, thickness: float, length: float,
                      rho: float) -> list[Result]:
    if i <= 0 or dtemp <= 0 or thickness <= 0:
        raise CalcError("i, dtemp, thickness must be positive")
    h_mil = thickness / _MIL
    out: list[Result] = []
    for label, k in (("external", 0.048), ("internal", 0.024)):
        a_mil2 = _ipc_area_mil2(i, dtemp, k)
        w = a_mil2 / h_mil * _MIL          # meters
        area_m2 = a_mil2 * _MIL2
        r = rho * length / area_m2
        out += [Result(f"{label}_width", w, "m"),
                Result(f"{label}_area", area_m2, "m²"),
                Result(f"{label}_resistance", r, "Ω", f"@{length:g} m, 20 °C"),
                Result(f"{label}_vdrop", r * i, "V"),
                Result(f"{label}_ploss", r * i * i, "W")]
    return out


@register(
    "trackcurrent", "Max current of a PCB track (IPC-2221)", "pcb", _IPC,
    (Param("width", "m", "track width"),
     Param("dtemp", "°C", "allowed temperature rise", default=10.0),
     Param("thickness", "m", "copper thickness", default=35e-6),
     Param("layer", "", "layer position", default="external",
           choices=("external", "internal"))),
)
def _calc_trackcurrent(width: float, dtemp: float, thickness: float,
                        layer: str) -> list[Result]:
    a_mil2 = (width / _MIL) * (thickness / _MIL)
    k = 0.048 if layer == "external" else 0.024
    return [Result("i_max", k * dtemp ** 0.44 * a_mil2 ** 0.725, "A")]


@register(
    "tracktemp", "Temperature rise of an existing track (IPC-2221)", "pcb",
    _IPC + " (solved for ΔT)",
    (Param("width", "m", "track width"),
     Param("i", "A", "continuous current"),
     Param("thickness", "m", "copper thickness", default=35e-6),
     Param("layer", "", "layer position", default="external",
           choices=("external", "internal"))),
    notes="IPC-2152 (measured charts) supersedes this fit but is chart-based "
          "licensed data with no public closed form — this tool refuses to "
          "fake it. The 2221 fit is the conservative classic.",
)
def _calc_tracktemp(width: float, i: float, thickness: float,
                     layer: str) -> list[Result]:
    if min(width, i, thickness) <= 0:
        raise CalcError("width, i, thickness must be positive")
    a_mil2 = (width / _MIL) * (thickness / _MIL)
    k = 0.048 if layer == "external" else 0.024
    dt = (i / (k * a_mil2 ** 0.725)) ** (1 / 0.44)
    return [Result("dtemp", dt, "°C", "estimated rise above ambient"),
            Result("fit_ok", dt <= 100.0, "",
                   "IPC-2221 fit is only characterized to ΔT ≤ 100 °C")]


# --- IPC-2221B Table 6-1 ---------------------------------------------------- #
#   B1 internal · B2 external uncoated ≤3050 m · B3 external uncoated >3050 m
#   B4 external polymer-coated · A5 external conformal-coated ·
#   A6 component lead uncoated · A7 component lead conformal-coated   [mm]
_CLEARANCE_COLS = ("b1", "b2", "b3", "b4", "a5", "a6", "a7")
_CLEARANCE_TABLE = (
    (15,   (0.05, 0.1,  0.1,  0.05, 0.13, 0.13, 0.13)),
    (30,   (0.05, 0.1,  0.1,  0.05, 0.13, 0.25, 0.13)),
    (50,   (0.1,  0.6,  0.6,  0.13, 0.13, 0.4,  0.13)),
    (100,  (0.1,  0.6,  1.5,  0.13, 0.13, 0.5,  0.13)),
    (150,  (0.2,  0.6,  3.2,  0.4,  0.4,  0.8,  0.4)),
    (170,  (0.2,  1.25, 3.2,  0.4,  0.4,  0.8,  0.4)),
    (250,  (0.2,  1.25, 6.4,  0.4,  0.4,  0.8,  0.4)),
    (300,  (0.2,  1.25, 12.5, 0.4,  0.4,  0.8,  0.8)),
    (500,  (0.25, 2.5,  12.5, 0.8,  0.8,  1.5,  0.8)),
)
# >500 V per-volt slopes [mm/V], same table's last row
_CLEARANCE_SLOPE = (0.0025, 0.005, 0.025, 0.00305, 0.00305, 0.00305, 0.00305)


@register(
    "clearance", "Minimum electrical clearance (IPC-2221 Table 6-1)", "pcb",
    "IPC-2221B (2012) Table 6-1, Electrical Conductor Spacing",
    (Param("voltage", "V", "peak voltage between conductors"),),
    notes="Columns: B1 internal; B2 external uncoated ≤3050 m; B3 external "
          "uncoated >3050 m; B4 external polymer-coated; A5 external "
          "conformal-coated; A6/A7 component leads uncoated/coated. "
          ">500 V uses the table's mm-per-volt slopes.",
)
def _calc_clearance(voltage: float) -> list[Result]:
    if voltage <= 0:
        raise CalcError("voltage must be positive")
    if voltage > 500:
        vals = tuple(round(voltage * s, 4) for s in _CLEARANCE_SLOPE)
    else:
        vals = next(row for vmax, row in _CLEARANCE_TABLE if voltage <= vmax)
    return [Result(col, v, "mm") for col, v in zip(_CLEARANCE_COLS, vals)]


@register(
    "via", "Via electrical / thermal / parasitic properties", "pcb",
    "R, Rθ: ρL/A on barrel annulus (ASTM B193, CRC Handbook λ_Cu=401); "
    "ampacity: IPC-2221B §6.2 fit; L, C, rise-time: Johnson & Graham, "
    "High-Speed Digital Design (1993) §7",
    (Param("drill", "m", "finished hole diameter"),
     Param("plating", "m", "plating thickness", default=35e-6),
     Param("length", "m", "via length (board thickness)", default=1.6e-3),
     Param("pad", "m", "via pad diameter", default=0.6e-3),
     Param("clearance_hole", "m", "plane antipad diameter", default=1.0e-3),
     Param("z0", "Ω", "line characteristic impedance", default=50.0),
     Param("i", "A", "applied current", default=1.0),
     Param("er", "", "board relative permittivity", default=4.5),
     Param("dtemp", "°C", "temp rise for ampacity", default=10.0),
     Param("trise", "s", "signal rise time (for reactance)", default=1e-9),
     Param("rho", "Ω·m", "plating resistivity", default=RHO_CU)),
)
def _calc_via(drill: float, plating: float, length: float, pad: float,
              clearance_hole: float, z0: float, i: float, er: float,
              dtemp: float, trise: float, rho: float) -> list[Result]:
    if drill <= 0 or plating <= 0 or length <= 0:
        raise CalcError("drill, plating, length must be positive")
    d_outer = drill + 2 * plating
    area = math.pi / 4 * (d_outer ** 2 - drill ** 2)       # barrel annulus, m²
    r = rho * length / area
    rth = length / (LAMBDA_CU * area)
    a_mil2 = area / _MIL2
    i_max = 0.048 * dtemp ** 0.44 * a_mil2 ** 0.725
    # Johnson & Graham: inches for L and C
    h_in, d_in = length / 0.0254, drill / 0.0254
    ind = 5.08 * h_in * (math.log(4 * h_in / d_in) + 1) * 1e-9     # H
    t_in = length / 0.0254
    cap = 1.41 * er * t_in * (pad / 0.0254) / ((clearance_hole - pad) / 0.0254) * 1e-12
    tr_deg = 2.2 * cap * (z0 / 2)
    react = math.pi * ind / trise
    return [Result("resistance", r, "Ω", "barrel, 20 °C"),
            Result("vdrop", r * i, "V"),
            Result("ploss", r * i * i, "W"),
            Result("thermal_resistance", rth, "°C/W"),
            Result("i_max", i_max, "A", f"IPC-2221 fit @ΔT={dtemp:g} °C"),
            Result("capacitance", cap, "F"),
            Result("rise_degradation", tr_deg, "s", "10–90 %, ≈2.2·C·Z0/2"),
            Result("inductance", ind, "H"),
            Result("reactance", react, "Ω", "X = π·L/t_rise")]


@register(
    "fusing", "Trace/wire fusing current (Onderdonk, Preece)", "pcb",
    "Onderdonk per Stauffacher, GE Review 31 (1928); "
    "Preece, Proc. Royal Society 36 (1884), I = 10244·d[in]^1.5 (Cu)",
    (Param("width", "m", "trace width (0 if using diameter)", default=0.0),
     Param("thickness", "m", "trace copper thickness", default=35e-6),
     Param("diameter", "m", "round-wire diameter (0 if trace)", default=0.0),
     Param("time", "s", "fault duration for Onderdonk", default=1.0),
     Param("ambient", "°C", "ambient temperature", default=25.0),
     Param("tmelt", "°C", "conductor melting point", default=T_MELT_CU)),
    notes="Onderdonk is adiabatic (short events, ≤ ~10 s); Preece is the "
          "steady-state fusing estimate for round wire. Estimates only — "
          "never use as a protection design without margin.",
)
def _calc_fusing(width: float, thickness: float, diameter: float, time: float,
                  ambient: float, tmelt: float) -> list[Result]:
    if diameter > 0:
        area = math.pi / 4 * diameter ** 2
    elif width > 0:
        area = width * thickness
    else:
        raise CalcError("give width (trace) or diameter (wire)")
    if time <= 0:
        raise CalcError("time must be positive")
    a_cmil = area / _MIL2 / (math.pi / 4)          # circular mils
    i_onder = a_cmil * math.sqrt(
        math.log10((tmelt - ambient) / (234 + ambient) + 1) / (33 * time))
    out = [Result("i_fusing_onderdonk", i_onder, "A",
                  f"adiabatic, t = {time:g} s")]
    if diameter > 0:
        out.append(Result("i_fusing_preece", 10244 * (diameter / 0.0254) ** 1.5,
                          "A", "steady-state, round Cu wire"))
    return out


@register(
    "awg", "AWG wire gauge properties", "pcb",
    "ASTM B258-18 (d = 0.127·92^((36−n)/39) mm); ρ_Cu per ASTM B193",
    (Param("gauge", "", "AWG number (use -1/-2/-3 for 2/0, 3/0, 4/0)"),
     Param("length", "m", "wire length for resistance", default=1.0)),
)
def _calc_awg(gauge: float, length: float) -> list[Result]:
    n = float(gauge)
    d_mm = 0.127 * 92 ** ((36 - n) / 39)
    d = d_mm * 1e-3
    area = math.pi / 4 * d * d
    r = RHO_CU * length / area
    return [Result("diameter", d, "m"),
            Result("area", area, "m²"),
            Result("resistance", r, "Ω", f"@{length:g} m, 20 °C"),
            Result("resistance_per_km", RHO_CU * 1000 / area, "Ω/km")]
