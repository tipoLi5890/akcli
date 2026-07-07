"""RF / high-speed calculators: wavelength, lines, attenuators.

References:

* Wavelength/propagation: c = 299 792 458 m/s exactly (SI definition,
  BIPM SI Brochure 9th ed., 2019).
* Coax / two-wire impedance: D.M. Pozar, *Microwave Engineering*, 4th ed.
  (Wiley, 2012), Table 2.1.
* Microstrip: E. Hammerstad & Ø. Jensen, "Accurate Models for Microstrip
  Computer-Aided Design", IEEE MTT-S Digest (1980) — zero-thickness closed
  forms (stated accuracy better than 0.2 % for 0.01 ≤ w/h ≤ 100).
* Stripline: S.B. Cohn, "Characteristic Impedance of the Shielded-Strip
  Transmission Line", IRE Trans. MTT-2 (1954) — exact zero-thickness solution
  via complete elliptic integrals, evaluated with the arithmetic–geometric
  mean (M. Abramowitz & I. Stegun, *Handbook of Mathematical Functions*,
  §17.6).
* Attenuators: *Reference Data for Radio Engineers*, 6th ed. (Howard W.
  Sams, 1975), ch. 10 — standard PI / TEE / bridged-TEE equations.

Transmission-line results are advisory: for production impedance control,
confirm with the fab's field solver / stackup tool.
"""

from __future__ import annotations

import math

from .eseries import SERIES, snap
from .registry import CalcError, Param, Result, register

C0 = 299_792_458.0            # m/s, exact (SI)
ETA0 = 376.730313668          # Ω, impedance of free space (CODATA 2018)

_SI = "SI Brochure 9th ed. (BIPM, 2019): c = 299 792 458 m/s exact"


@register(
    "wavelength", "Wavelength / propagation in a medium", "rf", _SI,
    (Param("f", "Hz", "frequency"),
     Param("er", "", "effective relative permittivity", default=1.0)),
)
def _calc_wavelength(f: float, er: float) -> list[Result]:
    if f <= 0 or er < 1:
        raise CalcError("need f > 0 and er ≥ 1")
    v = C0 / math.sqrt(er)
    lam = v / f
    return [Result("lambda_vacuum", C0 / f, "m"),
            Result("lambda", lam, "m", f"in εr={er:g}"),
            Result("quarter_wave", lam / 4, "m"),
            Result("velocity", v, "m/s"),
            Result("delay_per_m", 1 / v, "s/m")]


@register(
    "coax", "Coaxial line impedance", "rf",
    "Pozar, Microwave Engineering 4th ed. (2012), Table 2.1: "
    "Z0 = (η0/2π√εr)·ln(D/d)",
    (Param("d_inner", "m", "inner conductor diameter"),
     Param("d_outer", "m", "shield inner diameter"),
     Param("er", "", "dielectric relative permittivity", default=1.0)),
)
def _calc_coax(d_inner, d_outer, er) -> list[Result]:
    if not 0 < d_inner < d_outer:
        raise CalcError("need 0 < d_inner < d_outer")
    z0 = ETA0 / (2 * math.pi * math.sqrt(er)) * math.log(d_outer / d_inner)
    return [Result("z0", z0, "Ω")]


@register(
    "twinlead", "Two-wire (twisted pair) line impedance", "rf",
    "Pozar, Microwave Engineering 4th ed. (2012), Table 2.1: "
    "Z0 = (η0/π√εr)·acosh(D/d)",
    (Param("spacing", "m", "center-to-center spacing D"),
     Param("diameter", "m", "wire diameter d"),
     Param("er", "", "effective relative permittivity", default=1.0)),
)
def _calc_twinlead(spacing, diameter, er) -> list[Result]:
    if not 0 < diameter <= spacing:
        raise CalcError("need 0 < diameter ≤ spacing")
    z0 = ETA0 / (math.pi * math.sqrt(er)) * math.acosh(spacing / diameter)
    return [Result("z0", z0, "Ω")]


# --- microstrip (Hammerstad–Jensen, zero thickness) ------------------------- #
def _hj_z0_air(u: float) -> float:
    fu = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(fu / u + math.sqrt(1 + (2 / u) ** 2))


def _hj_eeff(u: float, er: float) -> float:
    a = (1 + (1 / 49) * math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432))
         + (1 / 18.7) * math.log(1 + (u / 18.1) ** 3))
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


@register(
    "microstrip", "Microstrip impedance (Hammerstad–Jensen)", "rf",
    "Hammerstad & Jensen, IEEE MTT-S Digest (1980); zero-thickness model, "
    "accuracy <0.2 % for 0.01 ≤ w/h ≤ 100",
    (Param("width", "m", "trace width"),
     Param("height", "m", "dielectric height (trace to plane)"),
     Param("er", "", "substrate relative permittivity", default=4.5)),
    notes="Zero trace thickness assumed; solder mask ignored. Confirm "
          "production stackups with the fab's field solver.",
)
def _calc_microstrip(width, height, er) -> list[Result]:
    if width <= 0 or height <= 0 or er < 1:
        raise CalcError("need width, height > 0 and er ≥ 1")
    u = width / height
    if not 0.01 <= u <= 100:
        raise CalcError("model valid for 0.01 ≤ w/h ≤ 100")
    eeff = _hj_eeff(u, er)
    z0 = _hj_z0_air(u) / math.sqrt(eeff)
    return [Result("z0", z0, "Ω"),
            Result("eeff", eeff, "", "effective permittivity"),
            Result("delay_per_m", math.sqrt(eeff) / C0, "s/m")]


# --- stripline (Cohn exact, zero thickness) --------------------------------- #
def _ellipk(k: float) -> float:
    """Complete elliptic integral K(k) by AGM (Abramowitz & Stegun §17.6)."""
    a, b = 1.0, math.sqrt(1 - k * k)
    for _ in range(64):
        if abs(a - b) < 1e-15:
            break
        a, b = (a + b) / 2, math.sqrt(a * b)
    return math.pi / (2 * a)


@register(
    "stripline", "Symmetric stripline impedance (Cohn)", "rf",
    "S.B. Cohn, IRE Trans. MTT-2 (1954): Z0 = (30π/√εr)·K(k')/K(k), "
    "k = tanh(πw/2b); K via AGM (Abramowitz & Stegun §17.6)",
    (Param("width", "m", "trace width"),
     Param("spacing", "m", "plane-to-plane spacing b"),
     Param("er", "", "dielectric relative permittivity", default=4.5)),
    notes="Exact for zero trace thickness, centered strip.",
)
def _calc_stripline(width, spacing, er) -> list[Result]:
    if width <= 0 or spacing <= 0 or er < 1:
        raise CalcError("need width, spacing > 0 and er ≥ 1")
    k = math.tanh(math.pi * width / (2 * spacing))
    kp = math.sqrt(1 - k * k)
    z0 = 30 * math.pi / math.sqrt(er) * _ellipk(kp) / _ellipk(k)
    return [Result("z0", z0, "Ω"),
            Result("delay_per_m", math.sqrt(er) / C0, "s/m")]


# --- attenuators ------------------------------------------------------------ #
_ATT_REF = ("Reference Data for Radio Engineers, 6th ed. (Howard W. Sams, "
            "1975), ch. 10; value snapping per IEC 60063:2015")


def _att_snap(name: str, exact: float, series: str) -> list[Result]:
    std, err = snap(exact, series)
    return [Result(name, exact, "Ω"),
            Result(f"{name}_std", std, "Ω", f"{series} ({err:+.2f}%)")]


@register(
    "attenuator", "Resistive attenuator (PI / TEE / bridged-TEE)", "rf",
    _ATT_REF,
    (Param("db", "dB", "attenuation"),
     Param("z0", "Ω", "system impedance", default=50.0),
     Param("topology", "", "network form", default="pi",
           choices=("pi", "tee", "bridged-tee")),
     Param("series", "", "E series for snapping", default="E96",
           choices=tuple(SERIES))),
)
def _calc_attenuator(db, z0, topology, series) -> list[Result]:
    if db <= 0 or z0 <= 0:
        raise CalcError("need db > 0 and z0 > 0")
    a = 10 ** (db / 20)
    if topology == "pi":
        shunt = z0 * (a + 1) / (a - 1)
        srs = z0 * (a * a - 1) / (2 * a)
        return (_att_snap("r_shunt", shunt, series)
                + _att_snap("r_series", srs, series))
    if topology == "tee":
        srs = z0 * (a - 1) / (a + 1)
        shunt = 2 * z0 * a / (a * a - 1)
        return (_att_snap("r_series", srs, series)
                + _att_snap("r_shunt", shunt, series))
    # bridged-tee: two Z0 arms + bridge/shunt
    return (_att_snap("r_bridge", z0 * (a - 1), series)
            + _att_snap("r_shunt", z0 / (a - 1), series)
            + [Result("r_arms", z0, "Ω", "both fixed arms = Z0")])
