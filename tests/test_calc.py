"""Tests for the `akcli calc` engineering calculators.

Ground truth comes from three independent sources:
* **KiCad pcb_calculator outputs** (via, track width, clearance table,
  resistor-combination) — read off a live KiCad 9 session; KiCad is used as
  an independent numerical cross-check only (no code shared).
* **Published standard/datasheet values** (IEC 60063 tables, TI NE555,
  NXP UM10204, attenuator tables from Reference Data for Radio Engineers).
* **Closed-form self-checks** where the formula IS the specification.
"""

from __future__ import annotations

import math

import pytest

from altium_kicad_cli.calc import CALCS, CalcError, compute
from altium_kicad_cli.calc.eseries import SERIES, nearest, snap
from altium_kicad_cli.calc.si import fmt_eng, parse_value


def res(name, **kw):
    """compute() with kwargs, returning {result: value}."""
    doc = compute(name, {k: str(v) for k, v in kw.items()})
    return {k: v["value"] for k, v in doc["results"].items()}


# --------------------------------------------------------------------------- #
# SI parsing
# --------------------------------------------------------------------------- #
def test_parse_engineering_notation():
    assert parse_value("4700") == 4700
    assert parse_value("4.7k") == 4700
    assert parse_value("4k7") == 4700
    assert parse_value("100n") == pytest.approx(1e-7)
    assert parse_value("1e-7") == pytest.approx(1e-7)
    assert parse_value("2M2") == pytest.approx(2.2e6)
    assert parse_value("3µ3") == pytest.approx(3.3e-6)
    assert parse_value("4R7") == pytest.approx(4.7)
    assert parse_value("1m") == pytest.approx(1e-3)   # milli, not mega
    with pytest.raises(CalcError):
        parse_value("abc")


def test_fmt_eng():
    assert fmt_eng(4700, "Ω") == "4.7 kΩ"
    assert fmt_eng(1e-7, "F") == "100 nF"


# --------------------------------------------------------------------------- #
# E-series — IEC 60063:2015 tables
# --------------------------------------------------------------------------- #
def test_e24_is_the_iec_table_not_the_formula():
    # historical values 2.7/3.0/3.3 differ from 10^(i/24)
    assert 2.7 in SERIES["E24"] and 3.0 in SERIES["E24"] and 3.3 in SERIES["E24"]
    assert len(SERIES["E24"]) == 24 and len(SERIES["E12"]) == 12


def test_e48_e96_match_published_tables():
    E48 = (1.00, 1.05, 1.10, 1.15, 1.21, 1.27, 1.33, 1.40, 1.47, 1.54, 1.62,
           1.69, 1.78, 1.87, 1.96, 2.05, 2.15, 2.26, 2.37, 2.49, 2.61, 2.74,
           2.87, 3.01, 3.16, 3.32, 3.48, 3.65, 3.83, 4.02, 4.22, 4.42, 4.64,
           4.87, 5.11, 5.36, 5.62, 5.90, 6.19, 6.49, 6.81, 7.15, 7.50, 7.87,
           8.25, 8.66, 9.09, 9.53)
    assert SERIES["E48"] == E48
    assert len(SERIES["E96"]) == 96
    # spot-check E96 against the IEC/KiCad table
    for v in (1.02, 1.87, 3.57, 4.99, 6.04, 9.76):
        assert v in SERIES["E96"], v


def test_e192_iec_exception_920():
    assert 9.2 in SERIES["E192"]        # IEC 60063 lists 9.20, formula gives 9.19
    assert 9.19 not in SERIES["E192"]
    assert len(SERIES["E192"]) == 192


def test_nearest_and_snap():
    assert nearest(3140, "E24") == 3000   # |3140-3000| < |3300-3140|
    assert nearest(3140, "E96") == 3160
    v, err = snap(1050, "E24")
    assert v == 1000 and err == pytest.approx(-4.7619, abs=1e-3)


def test_rcombo_finds_exact_1k_from_e24_pool():
    # KiCad's resistor calculator: target 1 kΩ (target itself excluded)
    # -> exact solutions exist (e.g. 1.1k ∥ 11k, or 180 + 820)
    r = res("rcombo", target="1k", series="E24")
    best = min(r["solutions"], key=lambda s: abs(s["error_pct"]))
    assert best["error_pct"] == pytest.approx(0.0, abs=1e-9)
    # the target value itself must not appear as the 1R "solution"
    one = next(s for s in r["solutions"] if s["n"] == 1)
    assert one["value"] != 1000.0


def test_rcombo_respects_excludes():
    r = res("rcombo", target="1k", series="E24", exclude1="180", exclude2="820")
    for s in r["solutions"]:
        assert "180 " not in s["expression"] + " "


# --------------------------------------------------------------------------- #
# basics
# --------------------------------------------------------------------------- #
def test_ohm_and_divider():
    r = res("ohm", v=5, r=250)
    assert r["i"] == pytest.approx(0.02) and r["p"] == pytest.approx(0.1)
    d = res("vdivider", vin=10, r_top=3000, r_bottom=2000)
    assert d["vout"] == pytest.approx(4.0)
    assert d["thevenin_r"] == pytest.approx(1200)


def test_vdivider_design_hits_ratio():
    r = res("vdivider-design", vin=5, vout=3.3, series="E96")
    assert r["vout_actual"] == pytest.approx(3.3, rel=0.01)


def test_led_resistor():
    r = res("led", vs=5, vf=2.0, i=0.010)
    assert r["r_ideal"] == pytest.approx(300)
    assert r["r_standard"] == 300  # E24 has 3.0


def test_rc_lc():
    r = res("rc", r="10k", c="100n")
    assert r["tau"] == pytest.approx(1e-3)
    assert r["fc"] == pytest.approx(159.155, rel=1e-4)
    l = res("lc", l="10u", c="100n")
    assert l["f0"] == pytest.approx(159155, rel=1e-3)
    assert l["z"] == pytest.approx(10.0)


def test_rc_charge_time():
    r = res("rc-charge", r="10k", c="100n", vs=5, vt=2.5)
    assert r["t"] == pytest.approx(1e-3 * math.log(2), rel=1e-6)


# --------------------------------------------------------------------------- #
# regulator — LM317 datasheet example (KiCad screenshot: R1=240, R2=720)
# --------------------------------------------------------------------------- #
def test_lm317_240_720_gives_5v():
    r = res("regulator", kind="adj3", r1=240, r2=720, vref=1.25, iadj="50u")
    assert r["vout_typ"] == pytest.approx(5.036, abs=1e-3)
    lo = res("regulator", kind="adj3", r1=240, r2=720,
             vref_min=1.20, vref_max=1.30)["vout_min"]
    assert lo < 5.0


def test_regulator_design_solves_r2():
    r = res("regulator-design", kind="adj3", vout=5.0, r_fixed=240,
            series="E96")
    # ideal R2 = (5.0-1.25)/(1.25/240 + 50µ) = 713.15 Ω
    assert r["r2_ideal"] == pytest.approx(713.15, abs=0.1)
    assert r["vout_actual"] == pytest.approx(5.0, rel=0.02)


def test_fb_divider_regulator():
    r = res("regulator", kind="fb", r1="10k", r2="10k", vref=0.8)
    assert r["vout_typ"] == pytest.approx(1.6)


# --------------------------------------------------------------------------- #
# IPC-2221 — KiCad pcb_calculator cross-check vectors (screenshots)
# --------------------------------------------------------------------------- #
def test_trackwidth_kicad_vector():
    # KiCad: I=1A, ΔT=10°C, 35 µm, 20 mm -> ext 0.300387 mm / int 0.781437 mm,
    # ext R = 0.0327197 Ω
    r = res("trackwidth", i=1, dtemp=10, thickness="35u", length="20m")
    assert r["external_width"] == pytest.approx(0.300387e-3, rel=2e-3)
    assert r["internal_width"] == pytest.approx(0.781437e-3, rel=2e-3)
    assert r["external_resistance"] == pytest.approx(0.0327197, rel=2e-3)
    assert r["external_vdrop"] == pytest.approx(0.0327197, rel=2e-3)


def test_trackcurrent_inverts_trackwidth():
    w = res("trackwidth", i=2.5, dtemp=20)["external_width"]
    back = res("trackcurrent", width=w, dtemp=20, layer="external")["i_max"]
    assert back == pytest.approx(2.5, rel=1e-6)


def test_clearance_table_kicad_rows():
    # IPC-2221B Table 6-1 rows as displayed by KiCad (mm)
    r = res("clearance", voltage=100)
    assert (r["b1"], r["b2"], r["b3"]) == (0.1, 0.6, 1.5)
    assert (r["b4"], r["a5"], r["a6"], r["a7"]) == (0.13, 0.13, 0.5, 0.13)
    r = res("clearance", voltage=250)
    assert r["b3"] == 6.4 and r["b2"] == 1.25
    r = res("clearance", voltage=12)
    assert r["b1"] == 0.05 and r["a6"] == 0.13


def test_clearance_above_500v_uses_slopes():
    r = res("clearance", voltage=1000)
    assert r["b1"] == pytest.approx(2.5)      # 1000 V × 0.0025 mm/V
    assert r["b3"] == pytest.approx(25.0)


def test_via_kicad_vector():
    # KiCad: drill 0.4, plating 0.035, length 1.6, pad 0.6, antipad 1.0,
    # Z0 50, 1 A, εr 4.5, ΔT 10, t_r 1 ns
    r = res("via", drill="0.4m", plating="35u", length="1.6m",
            pad="0.6m", clearance_hole="1m")
    assert r["resistance"] == pytest.approx(0.000575362, rel=1e-3)
    assert r["vdrop"] == pytest.approx(0.000575362, rel=1e-3)
    assert r["thermal_resistance"] == pytest.approx(83.2937, rel=5e-3)
    assert r["i_max"] == pytest.approx(2.9993, rel=1e-3)
    assert r["capacitance"] == pytest.approx(0.599508e-12, rel=1e-3)
    assert r["rise_degradation"] == pytest.approx(32.9729e-12, rel=1e-3)
    assert r["inductance"] == pytest.approx(1.20723e-9, rel=1e-3)
    assert r["reactance"] == pytest.approx(3.79262, rel=1e-3)


def test_fusing_onderdonk_magnitude():
    # 1000 circular mils ≈ AWG 20; Onderdonk 1 s fusing ≈ 146 A
    d_cmil_1000 = math.sqrt(1000) * 25.4e-6  # d such that d² [mil²] = 1000
    r = res("fusing", diameter=d_cmil_1000, time=1)
    assert r["i_fusing_onderdonk"] == pytest.approx(146.3, rel=0.01)
    assert "i_fusing_preece" in r


def test_awg_astm_b258():
    r = res("awg", gauge=24)
    assert r["diameter"] == pytest.approx(0.511e-3, rel=1e-3)   # ASTM B258
    r0 = res("awg", gauge=0)
    assert r0["diameter"] == pytest.approx(8.251e-3, rel=1e-3)


# --------------------------------------------------------------------------- #
# RF
# --------------------------------------------------------------------------- #
def test_wavelength():
    r = res("wavelength", f="100M")
    assert r["lambda_vacuum"] == pytest.approx(2.99792458, rel=1e-9)
    r = res("wavelength", f="100M", er=4.0)
    assert r["lambda"] == pytest.approx(1.49896229, rel=1e-9)


def test_coax_rg58_like():
    # air-line sanity: Z0 = 59.952·ln(D/d); D/d = e -> 59.952 Ω
    r = res("coax", d_inner=1e-3, d_outer=math.e * 1e-3)
    assert r["z0"] == pytest.approx(59.952, rel=1e-3)


def test_microstrip_hammerstad_jensen():
    # air, w/h = 1 -> the H-J closed form gives ~126.4 Ω
    r = res("microstrip", width=1e-3, height=1e-3, er=1)
    assert r["z0"] == pytest.approx(126.4, rel=5e-3)
    assert r["eeff"] == pytest.approx(1.0, abs=1e-9)
    # FR4-ish 50 Ω design point: εr 4.5, w/h = 2 -> ~48 Ω
    r = res("microstrip", width=2e-3, height=1e-3, er=4.5)
    assert 46 < r["z0"] < 50
    assert 3.2 < r["eeff"] < 3.6


def test_stripline_cohn():
    # w/b = 0.5 in air ≈ 100 Ω (Cohn exact); scales as 1/√εr
    r = res("stripline", width=0.5e-3, spacing=1e-3, er=1)
    assert r["z0"] == pytest.approx(100.5, rel=0.01)
    r45 = res("stripline", width=0.5e-3, spacing=1e-3, er=4.5)
    assert r45["z0"] == pytest.approx(100.5 / math.sqrt(4.5), rel=0.01)


def test_attenuator_published_tables():
    # Reference Data for Radio Engineers: 3 dB PI @50 Ω = 292.4/17.61,
    # 3 dB TEE = 8.55/141.9, 6 dB bridged-TEE = 49.76/50.24
    r = res("attenuator", db=3, topology="pi")
    assert r["r_shunt"] == pytest.approx(292.4, rel=1e-3)
    assert r["r_series"] == pytest.approx(17.61, rel=1e-3)
    r = res("attenuator", db=3, topology="tee")
    assert r["r_series"] == pytest.approx(8.550, rel=1e-3)
    assert r["r_shunt"] == pytest.approx(141.9, rel=1e-3)
    r = res("attenuator", db=6, topology="bridged-tee")
    assert r["r_bridge"] == pytest.approx(49.76, rel=1e-3)
    assert r["r_shunt"] == pytest.approx(50.24, rel=1e-3)


# --------------------------------------------------------------------------- #
# power / IC
# --------------------------------------------------------------------------- #
def test_buck_slva477_shape():
    r = res("buck", vin=12, vout=5, iout=2, fsw="500k", eff=0.9)
    assert 0.4 < r["duty"] < 0.5            # 5/(12·0.9) = 0.463
    assert r["ripple_current"] == pytest.approx(0.6)
    # L = (Vin-Vout)·D/(f·ΔI) = 7·0.463/(5e5·0.6)
    assert r["inductor"] == pytest.approx(7 * (5 / (12 * 0.9)) / (5e5 * 0.6),
                                          rel=1e-9)
    assert r["i_peak"] == pytest.approx(2.3)


def test_boost_slva372_shape():
    r = res("boost", vin=3.3, vout=12, iout=0.5, fsw="1M", eff=0.9)
    assert r["duty"] == pytest.approx(1 - 3.3 * 0.9 / 12, rel=1e-9)
    assert r["i_in_avg"] == pytest.approx(12 * 0.5 / (3.3 * 0.9), rel=1e-9)


def test_ne555_datasheet_formulas():
    r = res("ne555-astable", ra="10k", rb="10k", c="100n")
    assert r["frequency"] == pytest.approx(480.0)   # 1.44/(30k·100n)
    assert r["duty_high"] == pytest.approx(66.667, rel=1e-4)
    m = res("ne555-mono", r="100k", c="10u")
    assert m["pulse_width"] == pytest.approx(1.1)


def test_opamp_gain_pair():
    r = res("opamp-gain", gain=11, topology="non-inverting", r_ref="1k",
            series="E96")
    assert r["rf_ideal"] == pytest.approx(10e3)
    assert r["rf_standard"] == 10e3
    assert r["gain_actual"] == pytest.approx(11.0)


def test_i2c_pullup_um10204():
    # NXP UM10204: VDD 3.3 V -> Rp(min) = 2.9/3m = 966.7 Ω;
    # fast mode, Cb 100 pF -> Rp(max) = 300n/(0.8473·100p) ≈ 3540 Ω
    r = res("i2c-pullup", vdd=3.3, cb="100p", mode="fast")
    assert r["r_min"] == pytest.approx(966.67, rel=1e-4)
    assert r["r_max"] == pytest.approx(3540.9, rel=1e-3)
    assert r["r_min"] < r["suggested"] < r["r_max"]


def test_i2c_pullup_impossible_window():
    # 5 V, 400 pF at fast-mode-plus: Rp(min)=1533 Ω > Rp(max)=354 Ω
    with pytest.raises(CalcError):
        res("i2c-pullup", vdd=5, cb="400p", mode="fast-plus")


def test_crystal_caps_an2867():
    # CL 12.5 pF, Cstray 3 pF -> C1 = C2 = 19 pF (classic result)
    r = res("crystal-caps", cl="12.5p", cstray="3p", series="E24")
    assert r["c1_c2_ideal"] == pytest.approx(19e-12)
    assert r["cl_actual"] == pytest.approx(12.5e-12, rel=0.05)


def test_thermal_junction_and_heatsink():
    r = res("thermal", p=2, ta=25, theta_ja=62)
    assert r["tj_no_heatsink"] == pytest.approx(149.0)
    r = res("thermal", p=10, ta=40, tj_max=125, theta_jc=1.5, theta_cs=0.5)
    assert r["theta_sa_required"] == pytest.approx(6.5)


def test_battery_life():
    r = res("battery", capacity=2.0, i_avg="10m", derating=0.8)
    assert r["hours"] == pytest.approx(160.0)


# --------------------------------------------------------------------------- #
# codes
# --------------------------------------------------------------------------- #
def test_rescolor_4k7():
    r = res("rescolor", value="4.7k", tolerance=1, bands="5")
    assert r["bands_list"] == ["yellow", "violet", "black", "brown", "brown"]
    assert r["encoded_value"] == 4700
    r = res("rescolor", value=470, tolerance=5, bands="4")
    assert r["bands_list"] == ["yellow", "violet", "brown", "gold"]


def test_smdcode_decodes():
    assert res("smdcode", code="472")["value"] == 4700
    assert res("smdcode", code="1002")["value"] == 10000
    assert res("smdcode", code="4R7")["value"] == pytest.approx(4.7)
    assert res("smdcode", code="R47")["value"] == pytest.approx(0.47)
    eia = res("smdcode", code="01C")
    assert eia["value"] == pytest.approx(10000)   # E96 #01 (100) × 100
    assert eia["system"] == "EIA-96"


def test_galvanic_mil_std_889():
    r = res("galvanic", metal1="copper", metal2="tin", environment="normal")
    assert r["delta_v"] == pytest.approx(0.30)
    assert r["compatible"] is False               # 0.30 > 0.25
    assert r["corroding_metal"] == "tin"
    ok = res("galvanic", metal1="copper", metal2="nickel", environment="normal")
    assert ok["compatible"] is True


# --------------------------------------------------------------------------- #
# registry hygiene
# --------------------------------------------------------------------------- #
def test_every_calculator_has_a_reference():
    for c in CALCS.values():
        assert c.reference and len(c.reference) > 10, c.name
        assert c.title and c.group


def test_unknown_calc_and_bad_params():
    with pytest.raises(KeyError):
        compute("nope", {})
    with pytest.raises(CalcError):
        compute("rc", {"r": "10k"})               # missing c
    with pytest.raises(CalcError):
        compute("rc", {"r": "10k", "c": "1n", "zz": "1"})
    with pytest.raises(CalcError):
        compute("ohm", {"v": "5"})                # needs exactly two
