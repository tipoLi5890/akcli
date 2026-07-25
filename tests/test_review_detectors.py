"""Signal-family review detectors (M2) — five fixture classes per family.

Detectors consume the format-agnostic model, so fixtures are hand-built
:class:`~akcli.model.Schematic` objects: positive / negative / boundary /
odd-naming / missing-info per detector, exactly the porting discipline the
integration plan mandates. (End-to-end drawn-file coverage: test_review.py.)
"""

from __future__ import annotations

from akcli.model import Component, Net, Pin, Schematic
from akcli.review import topo
from akcli.review.detectors.signal import (crystal, divider, opamp,
                                           power_protect, protection,
                                           rc_filter)
from akcli.review.facts import FactValue
from akcli.review.detectors.validation import (enable_pin, i2c_pullup,
                                               vdomain)


def _comp(ref, lib, value=None, pins=(), dnp=False):
    return Component(designator=ref, library_ref=lib, x_mil=0, y_mil=0,
                     value=value, dnp=dnp,
                     pins=[Pin(number=n, name=nm, x_mil=0, y_mil=0)
                           for n, nm in pins])


def _sch(comps, nets):
    return Schematic(
        source_path="<test>", source_format="kicad", components=comps,
        nets=[Net(name=n, members=m) for n, m in nets])


def _run(det, sch):
    return det.run(topo.build_ctx(sch))


# --------------------------------------------------------------------------- #
# value parsing (odd-naming class, shared by every detector)
# --------------------------------------------------------------------------- #
def test_parse_value_engineering_notation():
    cases = {
        "4700": 4700.0, "4.7k": 4700.0, "4k7": 4700.0, "1R0": 1.0,
        "0R": 0.0, "100n": 100e-9, "10uF": 10e-6, "2.2µF": 2.2e-6,
        "1M": 1e6, "10m": 10e-3, "22p": 22e-12,
    }
    for text, want in cases.items():
        got = topo.parse_value(text)
        assert got is not None and abs(got - want) <= 1e-12 * max(want, 1), text
    for bad in ("", None, "TBD", "DNP", "10k5k", "k7"):
        assert topo.parse_value(bad) is None, bad


# --------------------------------------------------------------------------- #
# divider
# --------------------------------------------------------------------------- #
def _divider_sch(r_top_val, r_bot_val, top="+5V", mid="2V5_REF", ic=False):
    comps = [_comp("R1", "Device:R", r_top_val),
             _comp("R2", "Device:R", r_bot_val)]
    nets = [(top, [("R1", "1")]),
            (mid, [("R1", "2"), ("R2", "1")]),
            ("GND", [("R2", "2")])]
    if ic:
        comps.append(_comp("U1", "Regulator:BUCK", "BUCK",
                           pins=(("1", "VIN"), ("2", "FB"), ("3", "GND"))))
        nets = [(top, [("R1", "1"), ("U1", "1")]),
                (mid, [("R1", "2"), ("R2", "1"), ("U1", "2")]),
                ("GND", [("R2", "2"), ("U1", "3")])]
    return _sch(comps, nets)


def test_divider_tap_mismatch_positive():
    # 5 V × 30k/(10k+30k) = 3.75 V but the tap claims 2.5 V
    fs = _run(divider, _divider_sch("10k", "30k"))
    assert [f.code for f in fs] == ["REVIEW_DIVIDER_TAP_MISMATCH"]
    f = fs[0]
    assert f.confidence == "heuristic" and f.evidence["source"] == "topology"
    assert abs(f.evidence["calc"]["results"]["v_tap"] - 3.75) < 1e-6


def test_divider_matching_tap_is_silent_negative():
    fs = _run(divider, _divider_sch("10k", "10k"))       # 2.5 V == 2V5_REF
    assert fs == []


def test_divider_boundary_within_tolerance_is_silent():
    # 5 V × 10.2k/(10k+10.2k) = 2.5247 V — inside the 5% band around 2.5 V
    fs = _run(divider, _divider_sch("10k", "10.2k"))
    assert fs == []


def test_divider_odd_value_notation_parses():
    fs = _run(divider, _divider_sch("10k", "30000"))     # bare-ohms bottom
    assert [f.code for f in fs] == ["REVIEW_DIVIDER_TAP_MISMATCH"]


def test_divider_unparseable_value_is_insufficient_evidence():
    fs = _run(divider, _divider_sch("TBD", "10k"))
    assert [f.code for f in fs] == ["REVIEW_DIVIDER_UNVALUED"]
    assert fs[0].status == "insufficient_evidence"


def test_fb_divider_implausible_vref():
    # FB pin on the tap; 3.3 V × 100k/(10k+100k) = 3.0 V... make it worse:
    sch = _divider_sch("100k", "10k", top="+12V", mid="FB_NODE", ic=True)
    fs = _run(divider, sch)          # 12 × 10/110 = 1.09 V → plausible → INFO
    assert [f.code for f in fs] == ["REVIEW_FB_DIVIDER"]
    sch2 = _divider_sch("1k", "100k", top="+12V", mid="FB_NODE", ic=True)
    fs2 = _run(divider, sch2)        # 12 × 100/101 = 11.9 V → implausible
    assert [f.code for f in fs2] == ["REVIEW_FB_DIVIDER_VREF"]
    assert fs2[0].fix_params["kind"] == "fb_divider"


def test_fb_divider_without_rail_voltage_is_insufficient():
    sch = _divider_sch("10k", "10k", top="VBUS_RAW", mid="FB_NODE", ic=True)
    # VBUS_RAW implies no voltage → ratio-only observation
    fs = _run(divider, sch)
    assert [f.code for f in fs] == ["REVIEW_FB_DIVIDER"]
    assert fs[0].status == "insufficient_evidence"


# --------------------------------------------------------------------------- #
# rc filter
# --------------------------------------------------------------------------- #
def _rc_sch(r_val, c_val):
    return _sch(
        [_comp("R1", "Device:R", r_val), _comp("C1", "Device:C", c_val)],
        [("SIG_IN", [("R1", "1")]),
         ("SIG_F", [("R1", "2"), ("C1", "1")]),
         ("GND", [("C1", "2")])])


def test_rc_cutoff_positive():
    fs = _run(rc_filter, _rc_sch("1k", "100n"))
    assert [f.code for f in fs] == ["REVIEW_RC_CUTOFF"]
    f = fs[0]
    assert f.confidence == "deterministic"
    assert abs(f.evidence["calc"]["results"]["fc"]["value"] - 1591.5) < 1
    assert "Art of Electronics" in f.evidence["calc"]["reference"]


def test_rc_unparseable_stays_silent():
    assert _run(rc_filter, _rc_sch("1k", "DNP")) == []


# --------------------------------------------------------------------------- #
# crystal
# --------------------------------------------------------------------------- #
def _xtal_sch(c1_val=None, c2_val=None):
    comps = [_comp("Y1", "Device:Crystal", "8MHz")]
    osc1 = [("Y1", "1")]
    osc2 = [("Y1", "2")]
    nets = []
    if c1_val is not None:
        comps.append(_comp("C1", "Device:C", c1_val))
        osc1.append(("C1", "1"))
        nets.append(("GND", [("C1", "2")]))
    if c2_val is not None:
        comps.append(_comp("C2", "Device:C", c2_val))
        osc2.append(("C2", "1"))
        if nets:
            nets[0] = ("GND", nets[0][1] + [("C2", "2")])
        else:
            nets.append(("GND", [("C2", "2")]))
    return _sch(comps, [("OSC1", osc1), ("OSC2", osc2)] + nets)


def test_crystal_no_loadcaps_positive():
    fs = _run(crystal, _xtal_sch())
    assert [f.code for f in fs] == ["REVIEW_XTAL_NO_LOADCAPS"]


def test_crystal_one_sided_load():
    fs = _run(crystal, _xtal_sch(c1_val="22p"))
    assert [f.code for f in fs] == ["REVIEW_XTAL_ASYMMETRIC"]


def test_crystal_symmetric_load_reports_cl():
    fs = _run(crystal, _xtal_sch("22p", "22p"))
    assert [f.code for f in fs] == ["REVIEW_XTAL_LOAD"]
    f = fs[0]
    # CL = 11 pF series + 4 pF stray = 15 pF
    assert abs(f.evidence["calc"]["results"]["cl_pf"] - 15.0) < 0.01
    assert any("C_stray" in a for a in f.evidence["assumptions"])


def test_crystal_asymmetric_values():
    fs = _run(crystal, _xtal_sch("22p", "33p"))
    assert [f.code for f in fs] == ["REVIEW_XTAL_ASYMMETRIC"]


def test_crystal_unparseable_cap_is_silent():
    assert _run(crystal, _xtal_sch("22p", "TBD")) == []


# --------------------------------------------------------------------------- #
# protection
# --------------------------------------------------------------------------- #
def _conn_sch(with_tvs: bool):
    comps = [_comp("J1", "Connector:USB_C", "USB"),
             _comp("U1", "MCU:MCU", "MCU",
                   pins=(("1", "IO1"), ("2", "IO2"), ("3", "VDD")))]
    dp = [("J1", "2"), ("U1", "1")]
    dm = [("J1", "3"), ("U1", "2")]
    nets = [("VBUS", [("J1", "1"), ("U1", "3")]),
            ("USB_DP", dp), ("USB_DM", dm), ("GND", [("J1", "4")])]
    if with_tvs:
        comps.append(_comp("D1", "Power_Protection:USBLC6-2SC6", "USBLC6"))
        nets[1] = ("USB_DP", dp + [("D1", "1")])
        nets[2] = ("USB_DM", dm + [("D1", "2")])
    return _sch(comps, nets)


def test_connector_unprotected_positive():
    fs = _run(protection, _conn_sch(with_tvs=False))
    assert [f.code for f in fs] == ["REVIEW_CONN_UNPROTECTED"]
    f = fs[0]
    assert "J1" in f.refs and f.fix_params["kind"] == "add_esd"
    assert set(f.fix_params["nets"]) == {"USB_DP", "USB_DM"}


def test_connector_with_tvs_is_silent_negative():
    assert _run(protection, _conn_sch(with_tvs=True)) == []


def test_power_only_connector_is_not_flagged():
    sch = _sch([_comp("J2", "Connector:Barrel_Jack", "PWR")],
               [("VIN", [("J2", "1")]), ("GND", [("J2", "2")])])
    assert _run(protection, sch) == []


# --------------------------------------------------------------------------- #
# opamp (M2 closure)
# --------------------------------------------------------------------------- #
def _opamp_comp(ref="U1"):
    return _comp(ref, "Amplifier_Operational:LM358", "LM358",
                 pins=(("1", "~"), ("2", "-"), ("3", "+"),
                       ("4", "V-"), ("8", "V+")))


def _opamp_sch(*, rf=None, rg=None, rin=None, buffer=False, open_loop=False):
    """Build one op-amp stage; feedback parts are optional."""
    comps = [_opamp_comp()]
    minus = [("U1", "2")]
    plus_net = ("IN", [("U1", "3")])
    out = [("U1", "1")]
    nets = [("V+", [("U1", "8")]), ("GND_A", [("U1", "4")])]
    gnd = []
    if buffer:
        nets.append(("FB", minus + out))
        return _sch(comps, nets + [plus_net])
    if open_loop:
        nets += [("MINUS", minus), ("OUT", out), plus_net]
        return _sch(comps, nets)
    comps.append(_comp("R1", "Device:R", rf))                # feedback
    nets.append(("OUT", out + [("R1", "1")]))
    minus = minus + [("R1", "2")]
    if rg is not None:
        comps.append(_comp("R2", "Device:R", rg))            # ground leg
        minus += [("R2", "1")]
        gnd += [("R2", "2")]
    if rin is not None:
        comps.append(_comp("R3", "Device:R", rin))           # input leg
        minus += [("R3", "1")]
        nets.append(("SIG", [("R3", "2")]))
        plus_net = ("GND", [("U1", "3")])                    # + at ground
    nets.append(("MINUS", minus))
    nets.append(plus_net)
    if gnd:
        nets.append(("GND", gnd + (plus_net[1] if plus_net[0] == "GND" else [])))
    return _sch(comps, nets)


def test_opamp_non_inverting_gain():
    fs = _run(opamp, _opamp_sch(rf="10k", rg="1k"))
    assert [f.code for f in fs] == ["REVIEW_OPAMP_GAIN"]
    assert abs(fs[0].evidence["calc"]["results"]["gain"] - 11.0) < 1e-6


def test_opamp_inverting_gain():
    fs = _run(opamp, _opamp_sch(rf="10k", rin="2k"))
    assert [f.code for f in fs] == ["REVIEW_OPAMP_GAIN"]
    assert abs(fs[0].evidence["calc"]["results"]["gain"] + 5.0) < 1e-6


def test_opamp_unity_buffer():
    fs = _run(opamp, _opamp_sch(buffer=True))
    assert [f.code for f in fs] == ["REVIEW_OPAMP_GAIN"]
    assert fs[0].evidence["calc"]["results"]["gain"] == 1.0


def test_opamp_open_loop_warns():
    fs = _run(opamp, _opamp_sch(open_loop=True))
    assert [f.code for f in fs] == ["REVIEW_OPAMP_NO_FEEDBACK"]
    assert "comparator" in fs[0].message


def test_opamp_unparseable_feedback_is_silent():
    fs = _run(opamp, _opamp_sch(rf="TBD", rg="1k"))
    assert fs == []


# --------------------------------------------------------------------------- #
# validation: i2c pull-ups (M3)
# --------------------------------------------------------------------------- #
def _i2c_sch(sda_r=None, scl_r=None, rail="+3V3"):
    comps = [_comp("U1", "MCU:MCU", "MCU",
                   pins=(("1", "SDA"), ("2", "SCL"), ("3", "VDD")))]
    sda = [("U1", "1")]
    scl = [("U1", "2")]
    nets = [(rail, [("U1", "3")])]
    for tag, val, members in (("R1", sda_r, sda), ("R2", scl_r, scl)):
        if val is not None:
            comps.append(_comp(tag, "Device:R", val))
            members.append((tag, "1"))
            nets[0] = (rail, nets[0][1] + [(tag, "2")])
    comps.append(_comp("U2", "IO:EXPANDER", "EXP",
                       pins=(("1", "SDA"), ("2", "SCL"), ("3", "VDD"))))
    sda.append(("U2", "1"))
    scl.append(("U2", "2"))
    nets[0] = (rail, nets[0][1] + [("U2", "3")])
    return _sch(comps, [("I2C1_SDA", sda), ("I2C1_SCL", scl)] + nets)


def test_i2c_missing_pullups_warn():
    fs = _run(i2c_pullup, _i2c_sch())
    assert sorted(f.code for f in fs) == ["REVIEW_I2C_NO_PULLUP"] * 2


def test_i2c_good_pullups_silent():
    assert _run(i2c_pullup, _i2c_sch("1.8k", "1.8k")) == []


def test_i2c_pullup_too_strong():
    fs = _run(i2c_pullup, _i2c_sch("470", "1.8k"))
    codes = sorted(f.code for f in fs)
    assert "REVIEW_I2C_PULLUP_STRONG" in codes          # 470 < (3.3-0.4)/3mA
    assert "REVIEW_I2C_PULLUP_MISMATCH" in codes
    strong = next(f for f in fs if f.code == "REVIEW_I2C_PULLUP_STRONG")
    assert any("C_b" in a for a in strong.evidence["assumptions"])


def test_i2c_pullup_weak_is_note_with_assumption():
    fs = _run(i2c_pullup, _i2c_sch("100k", "100k"))
    assert sorted(f.code for f in fs) == ["REVIEW_I2C_PULLUP_WEAK"] * 2
    assert all(f.severity.value == "note" for f in fs)


# --------------------------------------------------------------------------- #
# validation: voltage domains (M3)
# --------------------------------------------------------------------------- #
def _vdomain_sch(rail_a="+5V", rail_b="+3V3", bridge=False):
    comps = [
        _comp("U1", "MCU:BIG", "TX",
              pins=(("1", "TX"), ("2", "VDD"), ("3", "GND"))),
        _comp("U2", "MCU:SMALL", "RX",
              pins=(("1", "RX"), ("2", "VDD"), ("3", "GND"))),
    ]
    sig = [("U1", "1"), ("U2", "1")]
    nets = [("UART_TX", sig),
            (rail_a, [("U1", "2")]),
            (rail_b, [("U2", "2")]),
            ("GND", [("U1", "3"), ("U2", "3")])]
    if bridge:
        comps.append(_comp("U3", "Logic:SHIFTER", "TXB0101",
                           pins=(("1", "A"), ("2", "VCCA"), ("3", "VCCB"),
                                 ("4", "GND"))))
        nets[0] = ("UART_TX", sig + [("U3", "1")])
        nets[1] = (rail_a, nets[1][1] + [("U3", "2")])
        nets[2] = (rail_b, nets[2][1] + [("U3", "3")])
        nets[3] = ("GND", nets[3][1] + [("U3", "4")])
    return _sch(comps, nets)


def test_vdomain_cross_warns():
    fs = _run(vdomain, _vdomain_sch())
    assert [f.code for f in fs] == ["REVIEW_VDOMAIN_CROSS"]
    assert "U1" in fs[0].refs and "U2" in fs[0].refs


def test_vdomain_same_rail_silent():
    assert _run(vdomain, _vdomain_sch(rail_a="+3V3", rail_b="+3V3")) == []


def test_vdomain_level_shifter_bridges():
    """A part powered from BOTH rails makes the net one domain."""
    assert _run(vdomain, _vdomain_sch(bridge=True)) == []


# --------------------------------------------------------------------------- #
# validation: enable pins (M3)
# --------------------------------------------------------------------------- #
def _en_sch(tied: bool):
    comps = [_comp("U1", "Regulator:BUCK", "BUCK",
                   pins=(("1", "VIN"), ("2", "EN"), ("3", "GND"),
                         ("4", "VOUT")))]
    en = [("U1", "2")]
    nets = [("+5V", [("U1", "1")]), ("GND", [("U1", "3")]),
            ("+3V3", [("U1", "4")])]
    if tied:
        nets[0] = ("+5V", nets[0][1] + en)
        return _sch(comps, nets)
    return _sch(comps, nets + [("EN_NET", en)])


def test_enable_floating_warns():
    fs = _run(enable_pin, _en_sch(tied=False))
    assert [f.code for f in fs] == ["REVIEW_EN_FLOATING"]
    assert fs[0].fix_params["pin"] == "U1.2"


def test_enable_tied_is_silent():
    assert _run(enable_pin, _en_sch(tied=True)) == []


# --------------------------------------------------------------------------- #
# format-agnostic contract: identical findings for kicad vs altium models
# --------------------------------------------------------------------------- #
def test_detectors_are_source_format_agnostic():
    from akcli.review import engine
    kic = _divider_sch("10k", "30k")
    alt = _divider_sch("10k", "30k")
    alt.source_format = "altium"
    fk, mk = engine.analyze(kic, profile="standard")
    fa, ma = engine.analyze(alt, profile="standard")
    assert [(f.code, f.fingerprint) for f in fk] == \
           [(f.code, f.fingerprint) for f in fa]
    assert (mk["source_format"], ma["source_format"]) == ("kicad", "altium")


# --------------------------------------------------------------------------- #
# power-entry protection (fuse + reverse polarity)
# --------------------------------------------------------------------------- #
def _entry_sch(*, fuse=True, series_diode=True, shunt_diode=False,
               fuse_value="1A", entry="VBAT"):
    """J1 battery entry -> [F1] -> [D1 series] -> load; optional crowbar."""
    comps = [_comp("J1", "Conn:1x02", "BATT", pins=(("1", "P1"), ("2", "P2"))),
             _comp("U1", "MCU:M0", "MCU",
                   pins=(("1", "VDD"), ("2", "GND"), ("3", "IO")))]
    nets = {entry: [("J1", "1")], "GND": [("J1", "2"), ("U1", "2")],
            "IO_NET": [("U1", "3")]}
    up = entry
    if fuse:
        comps.append(_comp("F1", "Device:Fuse", fuse_value))
        nets[up].append(("F1", "1"))
        up = entry + "_F"                # token-matches too: chain dedupe
        nets[up] = [("F1", "2")]
    if series_diode:
        comps.append(_comp("D1", "Device:D", "SS34"))
        nets[up].append(("D1", "2"))
        up = "VSYS"
        nets[up] = [("D1", "1")]
    nets[up].append(("U1", "1"))
    if shunt_diode:
        clamp = entry + "_F" if fuse else entry
        comps.append(_comp("D9", "Device:D", "SS34"))
        nets[clamp].append(("D9", "1"))
        nets["GND"].append(("D9", "2"))
    return _sch(comps, list(nets.items()))


def test_power_entry_unprotected_positive():
    fs = _run(power_protect,
              _entry_sch(fuse=False, series_diode=False))
    assert {f.code for f in fs} == {"REVIEW_FUSE_MISSING",
                                    "REVIEW_REVPOL_UNPROTECTED"}
    assert all(f.confidence == "heuristic" for f in fs)


def test_power_entry_fully_protected_negative():
    # VBAT -> F1 -> VBAT_F (also token-matches: chain dedupe) -> D1 -> VSYS
    assert _run(power_protect, _entry_sch()) == []


def test_power_entry_crowbar_needs_fuse():
    fs = _run(power_protect,
              _entry_sch(fuse=False, series_diode=False, shunt_diode=True))
    assert {f.code for f in fs} == {"REVIEW_FUSE_MISSING",
                                    "REVIEW_REVPOL_SHUNT_NO_FUSE"}
    # with the fuse the crowbar scheme is accepted
    ok = _run(power_protect,
              _entry_sch(fuse=True, series_diode=False, shunt_diode=True))
    assert ok == []


def test_power_entry_odd_naming_ptc_and_plus_vin():
    sch = _sch(
        [_comp("RT1", "Device:Polyfuse", "PTC 750mA"),
         _comp("D1", "Device:D_Schottky", "B340A"),
         _comp("U1", "MCU:M0", "MCU",
               pins=(("1", "VDD"), ("2", "GND"), ("3", "IO")))],
        [("+VIN", [("RT1", "1")]),
         ("VIN_F", [("RT1", "2"), ("D1", "2")]),
         ("VSYS", [("D1", "1"), ("U1", "1")]),
         ("GND", [("U1", "2")]), ("IO_NET", [("U1", "3")])])
    assert _run(power_protect, sch) == []


def test_power_entry_single_member_net_is_skipped():
    sch = _sch([_comp("J1", "Conn:1x02", "BATT",
                      pins=(("1", "P1"), ("2", "P2")))],
               [("VBAT", [("J1", "1")])])
    assert _run(power_protect, sch) == []


def test_fuse_unrated_is_insufficient_evidence():
    fs = _run(power_protect, _entry_sch(fuse_value="Fuse"))
    assert [f.code for f in fs] == ["REVIEW_FUSE_UNRATED"]
    assert fs[0].status == "insufficient_evidence"
    assert fs[0].confidence == "deterministic"


class _Facts:
    def __init__(self, mapping):
        self._m = mapping

    def lookup_component(self, comp):
        return self._m.get(comp.designator)


def _run_with_facts(sch, mapping):
    return power_protect.run(topo.build_ctx(sch, facts=_Facts(mapping)))


def test_fuse_undersized_datasheet_backed():
    fact = FactValue(key="i_load", unit="A", page=7, value=2.0,
                     sha256="00" * 32, pdf="board.pdf")
    fs = _run_with_facts(_entry_sch(fuse_value="1A"),
                         {"F1": {"i_load": fact}})
    assert [f.code for f in fs] == ["REVIEW_FUSE_UNDERSIZED"]
    f = fs[0]
    assert f.confidence == "datasheet_backed"
    assert f.evidence["datasheet"]["page"] == 7
    assert f.fix_params["suggested"] == 3.15   # 2/0.75 = 2.67 -> R10 3.15


def test_fuse_sizing_boundary_exact_floor_is_silent():
    fact = FactValue(key="i_load", unit="A", page=7, value=0.75,
                     sha256="00" * 32, pdf="board.pdf")
    # 0.75/0.75 = 1.0 A floor; a 1A fuse sits exactly on it: not undersized
    assert _run_with_facts(_entry_sch(fuse_value="1A"),
                           {"F1": {"i_load": fact}}) == []


def test_fuse_no_load_fact_stays_silent():
    assert _run(power_protect, _entry_sch(fuse_value="1A")) == []


# --------------------------------------------------------------------------- #
# DNP parts satisfy nothing (presence-satisfier discipline, topo.is_fitted)
#
# A Do-Not-Populate part is drawn but never fitted: the shipped board behaves
# as if it were absent, so it must NOT silence a "this part is missing"
# finding. Every fixture below is the detector's own negative case with the
# satisfying part flipped to dnp=True — the finding must come back.
# --------------------------------------------------------------------------- #
def _dnp(sch, *refs):
    """Mark components DNP in place; returns the schematic for chaining."""
    for comp in sch.components:
        if comp.designator in refs:
            comp.dnp = True
    return sch


def test_topo_fitted_helpers():
    fitted = _comp("R1", "Device:R", "10k")
    unfitted = _comp("R2", "Device:R", "10k", dnp=True)
    assert topo.is_fitted(fitted) and not topo.is_dnp(fitted)
    assert topo.is_dnp(unfitted) and not topo.is_fitted(unfitted)
    # an UNRESOLVED reference is not evidence of a DNP part
    assert not topo.is_fitted(None) and not topo.is_dnp(None)


def test_dnp_pullup_does_not_satisfy_i2c():
    sch = _dnp(_i2c_sch("1.8k", "1.8k"), "R1")
    fs = _run(i2c_pullup, sch)
    assert [f.code for f in fs] == ["REVIEW_I2C_NO_PULLUP"]
    assert "I2C1_SDA" in fs[0].refs


def test_dnp_divider_pullup_leaves_the_bus_unpulled_on_both_lines():
    sch = _dnp(_i2c_sch("1.8k", "1.8k"), "R1", "R2")
    assert sorted(f.code for f in _run(i2c_pullup, sch)) == \
        ["REVIEW_I2C_NO_PULLUP"] * 2


def test_dnp_load_caps_do_not_satisfy_the_crystal():
    sch = _dnp(_xtal_sch("22p", "22p"), "C1", "C2")
    assert [f.code for f in _run(crystal, sch)] == ["REVIEW_XTAL_NO_LOADCAPS"]
    # one side DNP is a one-sided load, not a symmetric pair
    one = _dnp(_xtal_sch("22p", "22p"), "C2")
    assert [f.code for f in _run(crystal, one)] == ["REVIEW_XTAL_ASYMMETRIC"]


def test_dnp_cap_forms_no_rc_pole():
    assert _run(rc_filter, _dnp(_rc_sch("1k", "100n"), "C1")) == []


def test_dnp_pull_leaves_the_enable_floating():
    # EN tied to +5V through a DNP resistor: the shipped board floats it.
    comps = [_comp("U1", "Regulator:BUCK", "BUCK",
                   pins=(("1", "VIN"), ("2", "EN"), ("3", "GND"),
                         ("4", "VOUT"))),
             _comp("R1", "Device:R", "100k", dnp=True)]
    sch = _sch(comps, [("+5V", [("U1", "1"), ("R1", "1")]),
                       ("GND", [("U1", "3")]),
                       ("+3V3", [("U1", "4")]),
                       ("EN_NET", [("U1", "2"), ("R1", "2")])])
    assert [f.code for f in _run(enable_pin, sch)] == ["REVIEW_EN_FLOATING"]
    # fitted, the same resistor ties it
    comps[1].dnp = False
    assert _run(enable_pin, sch) == []


def test_dnp_tvs_does_not_protect_a_connector():
    sch = _dnp(_conn_sch(with_tvs=True), "D1")
    assert [f.code for f in _run(protection, sch)] == ["REVIEW_CONN_UNPROTECTED"]


def test_dnp_fuse_and_diode_do_not_protect_the_entry():
    sch = _dnp(_entry_sch(), "F1", "D1")
    assert {f.code for f in _run(power_protect, sch)} == {
        "REVIEW_FUSE_MISSING", "REVIEW_REVPOL_UNPROTECTED"}


def test_dnp_feedback_resistor_leaves_the_opamp_open_loop():
    sch = _dnp(_opamp_sch(rf="100k", rg="10k"), "R1")   # R1 = the feedback R
    assert [f.code for f in _run(opamp, sch)] == ["REVIEW_OPAMP_NO_FEEDBACK"]
