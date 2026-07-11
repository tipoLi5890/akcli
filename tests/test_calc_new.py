"""Tests for the sensor-session calculator batch (calc/sensor.py).

Ground truth: exact closed forms hand-verified in comments, the SLVA954
node equation cross-checked against the pre-existing `hysteresis`
calculator, and the known session case Vcc=3 V, R1=10k, R2=1.1k, Rf=1M
-> VTH ~= 0.297 V.
"""

from __future__ import annotations

import pytest

from altium_kicad_cli.calc import CALCS, CalcError, compute


def res(name, **kw):
    doc = compute(name, {k: str(v) for k, v in kw.items()})
    return {k: v["value"] for k, v in doc["results"].items()}


# --------------------------------------------------------------------------- #
# battery-life
# --------------------------------------------------------------------------- #
def test_battery_life_aa_alkaline():
    # 2500 mAh AA at 10 mA avg, default 0.7 derating: 175 h ~= 7.29 d
    r = res("battery-life", capacity=2500, i_avg=10)
    assert r["capacity_usable"] == pytest.approx(1750.0)
    assert r["hours"] == pytest.approx(175.0)
    assert r["days"] == pytest.approx(175.0 / 24)


def test_battery_life_matches_battery_calc():
    # same rule of thumb as `battery` once units and derating line up
    new = res("battery-life", capacity=2500, i_avg=10, derating=0.8)
    old = res("battery", capacity=2.5, i_avg="10m", derating=0.8)
    assert new["hours"] == pytest.approx(old["hours"], rel=1e-12)


def test_battery_life_rejects_bad_inputs():
    for kw in ({"capacity": 0, "i_avg": 10},
               {"capacity": 2500, "i_avg": 0},
               {"capacity": 2500, "i_avg": 10, "derating": 0},
               {"capacity": 2500, "i_avg": 10, "derating": 1.1}):
        with pytest.raises(CalcError):
            res("battery-life", **kw)


# --------------------------------------------------------------------------- #
# comparator-hysteresis (session case + open-drain asymmetry)
# --------------------------------------------------------------------------- #
def test_comparator_hysteresis_session_case():
    # Vcc=3, R1=10k, R2=1.1k, Rf=1M: VTH = 3*1.1k/11.1k = 0.29730 V,
    # V_rise = (3e-4+3e-6)/1.0100909e-3 = 0.29997 V, V_fall = 0.29700 V
    r = res("comparator-hysteresis", vcc=3, r1="10k", r2="1.1k", rf="1M")
    assert r["vth_nominal"] == pytest.approx(0.297297, rel=1e-4)
    assert r["v_rise"] == pytest.approx(0.299973, rel=1e-4)
    assert r["v_fall"] == pytest.approx(0.297003, rel=1e-4)
    assert r["hysteresis"] == pytest.approx(2.9700e-3, rel=1e-3)


def test_comparator_hysteresis_matches_slva954_calc():
    # push-pull case (rpu=0) must agree with the existing SLVA954 calculator
    new = res("comparator-hysteresis", vcc=3, r1="10k", r2="1.1k", rf="1M")
    old = res("hysteresis", vcc=3, r1="10k", r2="1.1k", rh="1M")
    assert new["v_rise"] == pytest.approx(old["vt_rising"], rel=1e-12)
    assert new["v_fall"] == pytest.approx(old["vt_falling"], rel=1e-12)


def test_comparator_hysteresis_open_drain_pullup():
    # Rpu=100k: rising threshold sees Rf+Rpu = 1.1M, falling still Rf = 1M
    r = res("comparator-hysteresis", vcc=3, r1="10k", r2="1.1k",
            rf="1M", rpu="100k")
    g1, g2 = 1e-4, 1 / 1.1e3
    rise = (3 * g1 + 3 / 1.1e6) / (g1 + g2 + 1 / 1.1e6)
    fall = (3 * g1) / (g1 + g2 + 1e-6)
    assert r["v_rise"] == pytest.approx(rise, rel=1e-12)
    assert r["v_fall"] == pytest.approx(fall, rel=1e-12)
    # weaker released-state feedback -> hysteresis shrinks vs push-pull
    pp = res("comparator-hysteresis", vcc=3, r1="10k", r2="1.1k", rf="1M")
    assert r["hysteresis"] < pp["hysteresis"]
    assert r["vth_nominal"] == pytest.approx(pp["vth_nominal"], rel=1e-12)


def test_comparator_hysteresis_rejects_bad_inputs():
    with pytest.raises(CalcError):
        res("comparator-hysteresis", vcc=0, r1="10k", r2="1k", rf="1M")
    with pytest.raises(CalcError):
        res("comparator-hysteresis", vcc=3, r1="10k", r2="1k", rf=0)


# --------------------------------------------------------------------------- #
# envelope-detector (session case + both invalid sides)
# --------------------------------------------------------------------------- #
def test_envelope_detector_session_case_valid():
    # 10n * 1M = 10 ms; 0.5 ms carrier << 10 ms << 100 ms signal -> VALID
    r = res("envelope-detector", c_hold="10n", r_bleed="1M",
            f_carrier="2k", f_signal=10)
    assert r["tau"] == pytest.approx(10e-3, rel=1e-9)
    assert r["ripple_pct"] == pytest.approx(5.0, rel=1e-9)   # 1/(2k*10ms)
    assert r["ratio_carrier"] == pytest.approx(20.0, rel=1e-9)
    assert r["ratio_signal"] == pytest.approx(10.0, rel=1e-9)
    assert r["verdict"] == "VALID"


def test_envelope_detector_tau_too_small():
    # carrier 200 Hz: tau*f_c = 2 < 5 -> ripple side fails
    r = res("envelope-detector", c_hold="10n", r_bleed="1M",
            f_carrier=200, f_signal=10)
    assert r["verdict"].startswith("INVALID")
    assert "ripple" in r["verdict"]


def test_envelope_detector_tau_too_large():
    # signal 50 Hz: 1/(tau*f_m) = 2 < 5 -> envelope-tracking side fails
    r = res("envelope-detector", c_hold="10n", r_bleed="1M",
            f_carrier="2k", f_signal=50)
    assert r["verdict"].startswith("INVALID")
    assert "smeared" in r["verdict"]


def test_envelope_detector_no_window():
    # f_c/f_m = 4 < margin^2 = 25: both sides cannot pass any tau
    r = res("envelope-detector", c_hold="1u", r_bleed="10k",
            f_carrier=200, f_signal=50)
    assert "no workable" in r["verdict"]


def test_envelope_detector_rejects_carrier_below_signal():
    with pytest.raises(CalcError):
        res("envelope-detector", c_hold="10n", r_bleed="1M",
            f_carrier=10, f_signal="2k")
    with pytest.raises(CalcError):
        res("envelope-detector", c_hold=0, r_bleed="1M",
            f_carrier="2k", f_signal=10)


# --------------------------------------------------------------------------- #
# ldo-headroom
# --------------------------------------------------------------------------- #
def test_ldo_headroom_pass():
    r = res("ldo-headroom", vin_min=3.3, vout=3.0, v_dropout=0.2,
            i_load="50m")
    assert r["headroom"] == pytest.approx(0.3)
    assert r["margin"] == pytest.approx(0.1)
    assert r["p_dissipated"] == pytest.approx(0.015)
    assert r["headroom_ok"] is True


def test_ldo_headroom_fail_verdict():
    # 3.1 V in, 3.0 V out, 200 mV dropout: 100 mV short of regulation
    r = res("ldo-headroom", vin_min=3.1, vout=3.0, v_dropout=0.2,
            i_load="50m")
    assert r["margin"] == pytest.approx(-0.1)
    assert r["headroom_ok"] is False


def test_ldo_headroom_rejects_bad_inputs():
    with pytest.raises(CalcError):
        res("ldo-headroom", vin_min=3.0, vout=3.3, v_dropout=0.2, i_load=0.05)
    with pytest.raises(CalcError):
        res("ldo-headroom", vin_min=3.3, vout=3.0, v_dropout=0.2, i_load=0)


# --------------------------------------------------------------------------- #
# registry hygiene
# --------------------------------------------------------------------------- #
def test_sensor_calcs_registered_with_references():
    for name in ("battery-life", "comparator-hysteresis",
                 "envelope-detector", "ldo-headroom"):
        assert name in CALCS, name
        assert len(CALCS[name].reference) > 15, name


def test_sensor_calc_units_are_not_si_prefixable():
    # the CLI SI-prefixes only bare base units; mAh/h/d/% must stay plain
    # (regression guard for the '200 mmm' class of bug)
    prefixable = ("Ω", "V", "A", "W", "F", "H", "Hz", "s", "m")
    doc = compute("battery-life", {"capacity": "2500", "i_avg": "10"})
    for cell in doc["results"].values():
        assert cell["unit"] in ("mAh", "h", "d")
        assert cell["unit"] not in prefixable
