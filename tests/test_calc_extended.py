"""Tests for the second calculator batch (A/B items) + calc tooling (C items).

Ground truth: exact closed forms hand-verified in comments, published
standard values (25.4 mm/in, 1 mW = 0 dBm, IEC 60127 R10 fuse ladder),
and self-consistency (design→analyze round trips, L-match textbook example).
"""

from __future__ import annotations

import json
import math

import pytest

from altium_kicad_cli import cli, ops
from altium_kicad_cli.calc import CALCS, CalcError, compute
from altium_kicad_cli.calc import opsmap


def res(name, **kw):
    doc = compute(name, {k: str(v) for k, v in kw.items()})
    return {k: v["value"] for k, v in doc["results"].items()}


# --------------------------------------------------------------------------- #
# A — conversions (exact standards)
# --------------------------------------------------------------------------- #
def test_convert_length_exact():
    r = res("convert-length", value=1000, unit="mil")
    assert r["mm"] == pytest.approx(25.4, rel=1e-12)      # 1 in = 25.4 mm exact
    assert r["inch"] == pytest.approx(1.0, rel=1e-12)
    r = res("convert-length", value=1, unit="mm")
    assert r["mil"] == pytest.approx(39.3700787, rel=1e-6)


def test_convert_power_dbm():
    r = res("convert-power", dbm=0)
    assert r["w"] == pytest.approx(1e-3, rel=1e-12)        # 0 dBm = 1 mW
    assert r["vrms"] == pytest.approx(0.223607, rel=1e-5)  # @50 Ω
    r = res("convert-power", w=1)
    assert r["dbm"] == pytest.approx(30.0, abs=1e-9)
    with pytest.raises(CalcError):
        res("convert-power", w=1, vrms=1)


def test_convert_copper():
    r = res("convert-copper", oz=1)
    assert r["thickness_nominal"] == pytest.approx(34.8e-6, rel=1e-3)
    assert r["thickness_pure_cu"] == pytest.approx(34.06e-6, rel=1e-3)
    r2 = res("convert-copper", um=35)
    assert r2["oz"] == pytest.approx(1.0057, rel=1e-3)


# --------------------------------------------------------------------------- #
# A — diffpair, tracktemp
# --------------------------------------------------------------------------- #
def test_diffpair_microstrip_limits():
    # very wide spacing -> uncoupled -> Zdiff ≈ 2·Z0
    far = res("diffpair", width="1.8m", spacing="50m", height="1m", er=4.5)
    assert far["z_diff"] == pytest.approx(2 * far["z0_single"], rel=1e-3)
    near = res("diffpair", width="1.8m", spacing="0.2m", height="1m", er=4.5)
    assert near["z_diff"] < far["z_diff"]                  # coupling lowers Zdiff
    # IPC-2141A formula spot value: s = h -> 2·Z0·(1−0.48·e^−0.96)
    sh = res("diffpair", width="1.8m", spacing="1m", height="1m", er=4.5)
    expect = 2 * sh["z0_single"] * (1 - 0.48 * math.exp(-0.96))
    assert sh["z_diff"] == pytest.approx(expect, rel=1e-9)


def test_diffpair_stripline():
    r = res("diffpair", width="0.5m", spacing="0.5m", height="1m",
            er=4.5, topology="stripline")
    expect = 2 * r["z0_single"] * (1 - 0.347 * math.exp(-2.9 * 0.5))
    assert r["z_diff"] == pytest.approx(expect, rel=1e-9)


def test_tracktemp_inverts_trackwidth():
    w = res("trackwidth", i=2, dtemp=25)["external_width"]
    back = res("tracktemp", width=w, i=2)
    assert back["dtemp"] == pytest.approx(25.0, rel=1e-6)
    assert back["fit_ok"] is True


# --------------------------------------------------------------------------- #
# A — hysteresis (TI SLVA954 topology), round trip
# --------------------------------------------------------------------------- #
def test_hysteresis_design_round_trip():
    d = res("hysteresis-design", vcc=5, vt_rising=2.0, vt_falling=1.0, r1="100k")
    a = res("hysteresis", vcc=5, r1="100k",
            r2=d["r2_standard"], rh=d["rh_standard"])
    assert a["vt_rising"] == pytest.approx(d["vt_rising_actual"], rel=1e-9)
    assert a["vt_rising"] == pytest.approx(2.0, rel=0.03)   # E96 snap error
    assert a["vt_falling"] == pytest.approx(1.0, rel=0.03)


def test_hysteresis_design_rejects_bad_thresholds():
    # (with rail-to-rail output any 0 < VT− < VT+ < VCC is solvable,
    #  so only the ordering constraint can reject)
    with pytest.raises(CalcError):
        res("hysteresis-design", vcc=5, vt_rising=5.0, vt_falling=1.0)
    with pytest.raises(CalcError):
        res("hysteresis-design", vcc=5, vt_rising=1.0, vt_falling=2.0)


# --------------------------------------------------------------------------- #
# A — RS-485 / CAN
# --------------------------------------------------------------------------- #
def test_rs485_bias_5v_two_terminations():
    # 60 Ω parallel, 200 mV: Rup+Rdown ≤ 60·(5−0.2)/0.2 = 1440 -> each ≤ 720
    r = res("rs485-bias", vcc=5)
    assert r["r_parallel"] == pytest.approx(60)
    assert r["r_bias_each_max"] == pytest.approx(720)
    assert r["suggested"] == 680                            # largest E24 ≤ 720
    assert r["v_ab_idle"] >= 0.2


def test_can_split_corner():
    r = res("can-termination", c_split="4.7n")
    assert r["r_split_each"] == 60
    # fc = 1/(2π·30·4.7n) = 1.129 MHz
    assert r["f_cm_corner"] == pytest.approx(1.129e6, rel=1e-3)


# --------------------------------------------------------------------------- #
# A — LDO, gate drive
# --------------------------------------------------------------------------- #
def test_ldo_dissipation():
    r = res("ldo", vin=5, vout=3.3, iout=0.5, iq="5m", theta_ja=62,
            v_dropout=0.3)
    assert r["p_dissipated"] == pytest.approx(0.875)        # 1.7·0.5 + 5·5m
    assert r["dropout_ok"] is True
    assert r["tj"] == pytest.approx(25 + 0.875 * 62)


def test_gate_drive_slua618():
    # Qg 20 nC, 10 V, 50 ns, 100 kHz, Rdrv 1 Ω
    r = res("gate-drive", qg="20n", v_drive=10, t_switch="50n",
            fsw="100k", r_driver=1)
    assert r["i_peak"] == pytest.approx(0.4)
    assert r["r_gate"] == pytest.approx(25 - 1)
    assert r["p_drive"] == pytest.approx(0.02)              # Qg·V·f
    assert r["i_avg"] == pytest.approx(2e-3)


# --------------------------------------------------------------------------- #
# B — Sallen-Key, ADC
# --------------------------------------------------------------------------- #
def test_sallen_key_butterworth():
    r = res("sallen-key", fc="1k", c="10n")
    assert r["r_ideal"] == pytest.approx(15915.5, rel=1e-4)
    assert r["r_standard"] == 15800                          # E96
    assert r["gain_k"] == pytest.approx(1.5858, rel=1e-3)    # 3 − 1/0.7071
    assert r["fc_actual"] == pytest.approx(1007.3, rel=1e-3)


def test_adc_12bit():
    r = res("adc", bits=12, vref=3.3, r_source="1k", c_sample="10p")
    assert r["lsb"] == pytest.approx(3.3 / 4096)
    assert r["snr_ideal"] == pytest.approx(74.0, abs=0.01)   # 6.02·12+1.76
    # 13·ln2·τ, τ = 10 ns
    assert r["t_settle"] == pytest.approx(13 * math.log(2) * 1e-8, rel=1e-9)


# --------------------------------------------------------------------------- #
# B — shunt, NTC, fuse, TVS
# --------------------------------------------------------------------------- #
def test_shunt_10a_100mv():
    r = res("shunt", i_max=10, v_sense=0.1, adc_fs=3.3)
    assert r["r_shunt"] == pytest.approx(0.01)
    assert r["p_at_fullscale"] == pytest.approx(1.0)
    assert r["p_rating_min"] == pytest.approx(2.0)
    assert r["amp_gain"] == pytest.approx(33.0)


def test_inrush_ntc_230vac():
    r = res("inrush-ntc", v_supply=230, i_inrush_max=20, c_bulk="100u")
    assert r["v_peak"] == pytest.approx(325.27, rel=1e-4)
    assert r["r_cold_min"] == pytest.approx(16.26, rel=1e-3)
    assert r["energy"] == pytest.approx(0.5 * 100e-6 * 325.27 ** 2, rel=1e-4)


def test_fuse_r10_ladder():
    # 2 A / (0.75·0.9) = 2.96 A -> next IEC 60127 rating is 3.15 A
    r = res("fuse-derating", i_load=2, temp_factor=0.9)
    assert r["i_rating_min"] == pytest.approx(2.963, rel=1e-3)
    assert r["suggested"] == 3.15


def test_tvs_iec61000():
    r = res("tvs", v_line_max=24, v_ic_absmax=40, v_clamp=30, v_surge=1000)
    assert r["i_pp"] == pytest.approx(485.0)                 # (1000−30)/2
    assert r["p_pk"] == pytest.approx(485 * 30)
    assert r["clamp_ok"] is True and r["clamp_margin"] == pytest.approx(10)
    with pytest.raises(CalcError):
        res("tvs", v_line_max=24, v_ic_absmax=40, v_clamp=20)


# --------------------------------------------------------------------------- #
# B — matching, flyback
# --------------------------------------------------------------------------- #
def test_lmatch_textbook_50_to_200():
    # Pozar §5.1: Q = √3, Xs = 86.6 Ω, Xp = 115.5 Ω @100 MHz
    r = res("lmatch", f="100M", r_source=50, r_load=200)
    assert r["q"] == pytest.approx(math.sqrt(3), rel=1e-9)
    assert r["x_series"] == pytest.approx(86.60, rel=1e-3)
    assert r["x_shunt"] == pytest.approx(115.47, rel=1e-3)
    assert r["lowpass_l"] == pytest.approx(137.8e-9, rel=1e-3)
    assert r["lowpass_c"] == pytest.approx(13.78e-12, rel=1e-3)


def test_pimatch_symmetric_q5():
    # Rs = Rl = 50, Q = 5: Rv = 50/26, both halves identical
    r = res("pimatch", f="10M", r_source=50, r_load=50, q=5)
    assert r["r_virtual"] == pytest.approx(50 / 26, rel=1e-9)
    assert r["c_source"] == pytest.approx(r["c_load"], rel=1e-9)
    with pytest.raises(CalcError):
        res("pimatch", f="10M", r_source=50, r_load=200, q=0.5)  # < Qmin


def test_flyback_first_order():
    # VIN 100, VOR 100 -> D = 0.5; VOUT 12, VD 0.5 -> n = 8
    r = res("flyback", vin_min=100, vout=12, iout=1, fsw="100k", vor=100)
    assert r["duty_max"] == pytest.approx(0.5)
    assert r["turns_ratio"] == pytest.approx(8.0)
    # Lp = (100·0.5)²·0.85/(2·12·1e5)
    assert r["lp_max_dcm"] == pytest.approx(2500 * 0.85 / 2.4e6, rel=1e-9)


# --------------------------------------------------------------------------- #
# C — batch / --md / --ops
# --------------------------------------------------------------------------- #
def test_calc_batch(tmp_path, capsys):
    jobs = {"jobs": [
        {"calc": "rc", "params": {"r": "10k", "c": "100n"}},
        {"calc": "nope"},
    ]}
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps(jobs))
    assert cli.main(["calc", "batch", str(f)]) == 1          # one job failed
    out = json.loads(capsys.readouterr().out)
    assert "results" in out[0] and "error" in out[1]

    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"jobs": [{"calc": "ohm",
                                        "params": {"v": 5, "r": 100}}]}))
    assert cli.main(["calc", "batch", str(ok)]) == 0


def test_calc_batch_bad_input(tmp_path, capsys):
    f = tmp_path / "bad.json"
    f.write_text("not json")
    assert cli.main(["calc", "batch", str(f)]) == 2
    assert cli.main(["calc", "batch", str(tmp_path / "missing.json")]) == 4
    assert cli.main(["calc", "batch"]) == 2


def test_calc_md_output(capsys):
    assert cli.main(["calc", "rc", "r=10k", "c=100n", "--md"]) == 0
    out = capsys.readouterr().out
    assert "| result | value |" in out and "*Reference:" in out


def test_calc_ops_emits_valid_oplist(tmp_path, capsys):
    dest = tmp_path / "divider.json"
    assert cli.main(["calc", "vdivider-design", "vin=5", "vout=3.3",
                     "--ops", str(dest)]) == 0
    doc = json.loads(dest.read_text())
    assert ops.validate_oplist(doc) == []                    # schema-valid
    assert [o["designator"] for o in doc["ops"]] == ["R1", "R2"]
    assert all(o["op"] == "place_component" for o in doc["ops"])


def test_calc_ops_stdout_and_unsupported(capsys):
    assert cli.main(["calc", "crystal-caps", "cl=12.5p", "--ops", "-"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [o["value"] for o in doc["ops"]] == ["18p", "18p"]
    assert cli.main(["calc", "rc", "r=1k", "c=1n", "--ops", "-"]) == 2


def test_opsmap_covers_only_registered_calcs():
    for name in opsmap.MAPPABLE:
        assert name in CALCS, f"opsmap maps unknown calc {name!r}"


def test_new_calcs_all_have_references():
    for name in ("diffpair", "convert-length", "hysteresis", "rs485-bias",
                 "can-termination", "ldo", "gate-drive", "sallen-key", "adc",
                 "shunt", "inrush-ntc", "fuse-derating", "tvs", "lmatch",
                 "pimatch", "flyback", "tracktemp"):
        assert len(CALCS[name].reference) > 15, name
