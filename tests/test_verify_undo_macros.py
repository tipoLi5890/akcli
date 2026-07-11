"""`akcli verify` / `akcli undo` / macro ops — the full round-trip story.

One flow exercises all three: a macro op draws a decoupling network onto a
copy of the fixture (label-on-pin connectivity, merged into the power rails
by netbuild), `verify` proves the copy is no longer net-equivalent to the
original, `undo` restores it, and `verify` passes again.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from altium_kicad_cli import cli
from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.ops import expand_macros, op_template
from altium_kicad_cli.readers import kicad as kreader

FIXTURE = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"


def _oplist(ops):
    return {"protocol_version": 1, "target_format": "kicad",
            "target_file": "x", "ops": ops}


# ------------------------------------------------------------- macros ----

def test_expand_divider_uses_label_on_pin():
    doc = expand_macros(_oplist([{
        "op": "place_divider", "x_mil": 3000, "y_mil": 1000,
        "top_net": "VIN", "mid_net": "FB", "bottom_net": "GND",
        "designators": ["R7", "R8"], "values": ["10k", "4k7"],
    }]))
    ops = doc["ops"]
    assert [o["op"] for o in ops] == ["place_component"] * 2 + ["add_net_label"] * 4
    assert ops[1]["y_mil"] == 1400                     # default 400-mil spacing
    anchors = {(o["name"], o["at"]) for o in ops[2:]}
    assert anchors == {("VIN", "R7.1"), ("FB", "R7.2"),
                       ("FB", "R8.1"), ("GND", "R8.2")}


def test_expand_macros_untouched_without_macros():
    doc = _oplist([{"op": "delete_component", "designator": "R1"}])
    assert expand_macros(doc) is doc                   # no copy, no changes


def test_expand_macros_rejects_bad_args():
    with pytest.raises(AkcliError):
        expand_macros(_oplist([{"op": "place_divider", "x_mil": 1, "y_mil": 2,
                                "top_net": "A", "mid_net": "B"}]))   # missing bottom_net
    with pytest.raises(AkcliError):
        expand_macros(_oplist([{"op": "place_decoupling", "x_mil": "NaN",
                                "y_mil": 2, "power_net": "V"}]))     # non-numeric


def test_macro_templates_and_listing(capsys):
    t = op_template("place_decoupling")
    assert t["op"] == "place_decoupling" and t["gnd_net"] == "GND"
    assert cli.main(["ops", "template", "place_divider"]) == 0
    assert "top_net" in capsys.readouterr().out
    assert cli.main(["ops", "list"]) == 0
    out = capsys.readouterr().out
    assert "place_divider" in out and "place_decoupling" in out


# ---------------------------------------------- verify + undo round-trip ----

def test_verify_undo_roundtrip(tmp_path, capsys):
    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)

    # identical copies are equivalent
    assert cli.main(["verify", str(FIXTURE), str(work)]) == 0
    assert "PASS" in capsys.readouterr().out

    # draw a decoupling cap through the macro (writes + leaves a .bak)
    opsfile = tmp_path / "ops.json"
    opsfile.write_text(json.dumps(_oplist([{
        "op": "place_decoupling", "x_mil": 4000, "y_mil": 2000,
        "power_net": "+3V3", "designator": "C9",
    }])))
    assert cli.main(["draw", str(work), "--ops", str(opsfile), "--apply"]) == 0
    capsys.readouterr()

    # netbuild merges the label-on-pin cap into the existing rails
    sch = kreader.read_sch(str(work))
    nets = {n.name: set(n.members) for n in sch.nets}
    assert ("C9", "1") in nets["+3V3"] and ("R1", "1") in nets["+3V3"]
    assert ("C9", "2") in nets["GND"]

    # no longer equivalent to the original
    assert cli.main(["verify", str(FIXTURE), str(work)]) == 1
    assert "FAIL" in capsys.readouterr().out

    # undo: dry-run first, then swap; equivalence restored
    assert cli.main(["undo", str(work)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert cli.main(["undo", str(work), "--apply"]) == 0
    capsys.readouterr()
    assert cli.main(["verify", str(FIXTURE), str(work)]) == 0

    # undo again = redo (C9 returns)
    assert cli.main(["undo", str(work), "--apply"]) == 0
    capsys.readouterr()
    assert any(c.designator == "C9"
               for c in kreader.read_sch(str(work)).components)


def test_undo_without_backup_exits_4(tmp_path, capsys):
    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)
    assert cli.main(["undo", str(work)]) == 4
    assert "no backup" in capsys.readouterr().err


def test_verify_strict_flags_value_changes(tmp_path, capsys):
    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_oplist([{
        "op": "set_component_parameters", "designator": "R1", "value": "22k",
    }])))
    assert cli.main(["draw", str(work), "--ops", str(ops), "--apply"]) == 0
    capsys.readouterr()
    assert cli.main(["verify", str(FIXTURE), str(work)]) == 0     # nets equal
    assert "value/footprint" in capsys.readouterr().out
    assert cli.main(["verify", str(FIXTURE), str(work), "--strict"]) == 1


# ------------------------------------------------------------ nets check ----

def test_check_nets_single_pin(tmp_path, capsys):
    from altium_kicad_cli.checks import nets as netcheck

    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)
    clean = netcheck.run(kreader.read_sch(str(work)))
    assert clean == []                                  # fixture is healthy

    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_oplist([
        # a cap on a brand-new net nobody else uses -> single-pin net
        {"op": "place_decoupling", "x_mil": 4000, "y_mil": 2000,
         "power_net": "ORPHAN", "gnd_net": "GND", "designator": "C9"},
    ])))
    assert cli.main(["draw", str(work), "--ops", str(ops), "--apply"]) == 0
    capsys.readouterr()
    findings = netcheck.run(kreader.read_sch(str(work)))
    assert {f.code for f in findings} == {"NET_SINGLE_PIN"}
    assert any("ORPHAN" in f.message for f in findings)


def test_check_nets_off_grid_direct_model():
    # the draw pipeline snaps to grid, so off-grid inputs come from outside —
    # exercise the check on the model directly
    from altium_kicad_cli import model
    from altium_kicad_cli.checks import nets as netcheck

    comp = model.Component(
        designator="U1", library_ref="X:Y", x_mil=1003.0, y_mil=1000.0,
        pins=[model.Pin(number="1", name=None, x_mil=1003.0, y_mil=850.0),
              model.Pin(number="2", name=None, x_mil=1003.0, y_mil=1150.0),
              model.Pin(number="3", name=None, x_mil=1050.0, y_mil=1000.0)])
    sch = model.Schematic(source_path="mem", source_format="kicad",
                          components=[comp], nets=[])
    findings = netcheck.run(sch)
    assert len(findings) == 1 and findings[0].code == "NET_OFF_GRID"
    assert "2 pin(s)" in findings[0].message and "U1" in findings[0].message


# ---------------------------------------------------------- macro library ----

def test_expand_pullup_led_rc_crystal():
    doc = expand_macros(_oplist([
        {"op": "place_pullup", "x_mil": 100, "y_mil": 100,
         "net": "SDA", "rail_net": "VDD", "designator": "R3", "value": "2k2"},
        {"op": "place_led_indicator", "x_mil": 500, "y_mil": 100,
         "net": "STATUS", "designators": ["R4", "D2"]},
        {"op": "place_rc_filter", "x_mil": 900, "y_mil": 100,
         "in_net": "RAW", "out_net": "FILT"},
        # explicit designators: the RC filter above already owns the default C1
        # (two macros left on defaults now FAIL the duplicate-placement lint)
        {"op": "place_crystal", "x_mil": 1500, "y_mil": 100,
         "in_net": "XI", "out_net": "XO", "load_c": "18p",
         "designators": ["Y1", "C3", "C4"]},
    ]))
    ops = doc["ops"]
    labels = {(o["name"], o["at"]) for o in ops if o["op"] == "add_net_label"}
    # pullup: rail on pin 1, signal on pin 2
    assert {("VDD", "R3.1"), ("SDA", "R3.2")} <= labels
    # LED chain: shared internal node on R.2 and the LED ANODE (pin 2)
    assert {("STATUS", "R4.1"), ("N_R4_D2", "R4.2"),
            ("N_R4_D2", "D2.2"), ("GND", "D2.1")} <= labels
    # RC filter: out_net bridges R.2 and C.1
    assert {("RAW", "R1.1"), ("FILT", "R1.2"),
            ("FILT", "C1.1"), ("GND", "C1.2")} <= labels
    # crystal: XI joins Y1.1 + C3.1 (crystal uses its own designators)
    assert {("XI", "Y1.1"), ("XO", "Y1.2"),
            ("XI", "C3.1"), ("GND", "C4.2")} <= labels
    parts = [o for o in ops if o["op"] == "place_component"]
    assert sum(o["lib_id"] == "Device:Crystal" for o in parts) == 1
    assert sum(o["lib_id"] == "Device:LED" for o in parts) == 1


def test_pullup_macro_end_to_end_netlist(tmp_path, capsys):
    """A drawn pullup lands CONNECTED: its rail label joins the power net."""
    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_oplist([{
        "op": "place_pullup", "x_mil": 4400, "y_mil": 1200,
        "net": "SDA", "rail_net": "+3V3", "designator": "R5", "value": "2k2",
    }])))
    assert cli.main(["draw", str(work), "--ops", str(ops), "--apply"]) == 0
    capsys.readouterr()
    nets = {n.name: set(n.members) for n in kreader.read_sch(str(work)).nets}
    assert ("R5", "1") in nets["+3V3"]          # merged into the power rail
    assert ("R5", "2") in nets["SDA"]


def test_validator_accepts_unexpanded_macros():
    from altium_kicad_cli.ops import validate_oplist
    doc = _oplist([{"op": "place_pullup", "x_mil": 1, "y_mil": 2,
                    "net": "A", "rail_net": "B"}])
    assert validate_oplist(doc) == []
    bad = _oplist([{"op": "place_pullup", "x_mil": 1, "y_mil": 2}])
    errs = validate_oplist(bad)
    assert len(errs) == 2 and all(e.code == "OP_UNSUPPORTED" for e in errs)
