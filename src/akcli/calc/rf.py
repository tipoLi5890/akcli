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
def _calc_coax(d_inner: float, d_outer: float, er: float) -> list[Result]:
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
def _calc_twinlead(spacing: float, diameter: float, er: float) -> list[Result]:
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
def _calc_microstrip(width: float, height: float, er: float) -> list[Result]:
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
def _calc_stripline(width: float, spacing: float, er: float) -> list[Result]:
    if width <= 0 or spacing <= 0 or er < 1:
        raise CalcError("need width, spacing > 0 and er ≥ 1")
    k = math.tanh(math.pi * width / (2 * spacing))
    kp = math.sqrt(1 - k * k)
    z0 = 30 * math.pi / math.sqrt(er) * _ellipk(kp) / _ellipk(k)
    return [Result("z0", z0, "Ω"),
            Result("delay_per_m", math.sqrt(er) / C0, "s/m")]


# --- differential pairs ------------------------------------------------------ #
@register(
    "diffpair", "Differential pair impedance (edge-coupled)", "rf",
    "IPC-2141A (2004) §5 coupled-line approximations (cf. National AN-905): "
    "microstrip Zdiff = 2·Z0·(1−0.48·e^(−0.96·s/h)); "
    "stripline Zdiff = 2·Z0·(1−0.347·e^(−2.9·s/b)); single-ended Z0 from "
    "Hammerstad–Jensen / Cohn",
    (Param("width", "m", "trace width"),
     Param("spacing", "m", "edge-to-edge gap s"),
     Param("height", "m", "microstrip: dielectric h; stripline: plane gap b"),
     Param("er", "", "substrate relative permittivity", default=4.5),
     Param("topology", "", "line type", default="microstrip",
           choices=("microstrip", "stripline"))),
    notes="Closed-form estimate (loose coupling, zero thickness); for "
          "production impedance control use the fab's field solver.",
)
def _calc_diffpair(width: float, spacing: float, height: float, er: float,
                    topology: str) -> list[Result]:
    if min(width, spacing, height) <= 0 or er < 1:
        raise CalcError("width, spacing, height must be positive; er ≥ 1")
    if topology == "microstrip":
        u = width / height
        if not 0.01 <= u <= 100:
            raise CalcError("model valid for 0.01 ≤ w/h ≤ 100")
        z0 = _hj_z0_air(u) / math.sqrt(_hj_eeff(u, er))
        zdiff = 2 * z0 * (1 - 0.48 * math.exp(-0.96 * spacing / height))
    else:
        k = math.tanh(math.pi * width / (2 * height))
        kp = math.sqrt(1 - k * k)
        z0 = 30 * math.pi / math.sqrt(er) * _ellipk(kp) / _ellipk(k)
        zdiff = 2 * z0 * (1 - 0.347 * math.exp(-2.9 * spacing / height))
    return [Result("z0_single", z0, "Ω", "uncoupled single-ended"),
            Result("z_diff", zdiff, "Ω"),
            Result("z_odd", zdiff / 2, "Ω")]


# --- matching networks -------------------------------------------------------- #
_MATCH_REF = ("D.M. Pozar, Microwave Engineering 4th ed. (2012) §5.1 "
              "(L-section matching); PI form per C. Bowick, RF Circuit "
              "Design 2nd ed. (2008) ch. 4")


@register(
    "lmatch", "L-section impedance match (real→real)", "rf", _MATCH_REF,
    (Param("f", "Hz", "design frequency"),
     Param("r_source", "Ω", "source resistance"),
     Param("r_load", "Ω", "load resistance")),
    notes="Series element on the low-R side, shunt on the high-R side. Both "
          "low-pass (L series + C shunt) and high-pass duals are given.",
)
def _calc_lmatch(f: float, r_source: float, r_load: float) -> list[Result]:
    if min(f, r_source, r_load) <= 0:
        raise CalcError("f, r_source, r_load must be positive")
    if abs(r_source - r_load) < 1e-12:
        raise CalcError("already matched (r_source = r_load)")
    r_hi, r_lo = max(r_source, r_load), min(r_source, r_load)
    q = math.sqrt(r_hi / r_lo - 1)
    x_series = q * r_lo
    x_shunt = r_hi / q
    w = 2 * math.pi * f
    return [Result("q", q, ""),
            Result("x_series", x_series, "Ω", "on the low-R side"),
            Result("x_shunt", x_shunt, "Ω", "on the high-R side"),
            Result("lowpass_l", x_series / w, "H", "series L"),
            Result("lowpass_c", 1 / (w * x_shunt), "F", "shunt C"),
            Result("highpass_c", 1 / (w * x_series), "F", "series C"),
            Result("highpass_l", x_shunt / w, "H", "shunt L"),
            Result("bandwidth_est", f / q if q else None, "Hz", "≈ f/Q")]


@register(
    "pimatch", "PI-section impedance match (real→real)", "rf", _MATCH_REF,
    (Param("f", "Hz", "design frequency"),
     Param("r_source", "Ω", "source resistance"),
     Param("r_load", "Ω", "load resistance"),
     Param("q", "", "loaded Q (0 = minimum possible)", default=0.0)),
    notes="Two back-to-back L-sections through a virtual resistance; Q "
          "applies to the higher-R side and must exceed √(R_hi/R_lo − 1). "
          "Low-pass form: shunt C — series L — shunt C.",
)
def _calc_pimatch(f: float, r_source: float, r_load: float,
                   q: float) -> list[Result]:
    if min(f, r_source, r_load) <= 0:
        raise CalcError("f, r_source, r_load must be positive")
    r_hi, r_lo = max(r_source, r_load), min(r_source, r_load)
    q_min = math.sqrt(r_hi / r_lo - 1) if r_hi > r_lo else 0.0
    if q <= 0:
        q = max(q_min, 1.0)
    if q < q_min:
        raise CalcError(f"q must be ≥ {q_min:.3f} for this ratio")
    r_v = r_hi / (q * q + 1)
    q_hi = q
    q_lo = math.sqrt(r_lo / r_v - 1) if r_lo > r_v else 0.0
    x_c_hi = r_hi / q_hi
    x_c_lo = r_lo / q_lo if q_lo else float("inf")
    x_l = r_v * (q_hi + q_lo)
    w = 2 * math.pi * f
    c_hi = 1 / (w * x_c_hi)
    c_lo = 1 / (w * x_c_lo) if math.isfinite(x_c_lo) else 0.0
    src_is_lo = r_source <= r_load
    return [Result("q_min", q_min, ""), Result("q_used", q, ""),
            Result("r_virtual", r_v, "Ω"),
            Result("c_source", c_lo if src_is_lo else c_hi, "F", "shunt at source"),
            Result("l_series", x_l / w, "H"),
            Result("c_load", c_hi if src_is_lo else c_lo, "F", "shunt at load")]


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
def _calc_attenuator(db: float, z0: float, topology: str,
                      series: str) -> list[Result]:
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
