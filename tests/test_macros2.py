"""Tests for the second macro batch + writer hardening:

* ``connect_and_label`` — pin-to-pin wire + ONE mid-wire label (the fix for
  facing-pin label collisions), including the ``mid()`` anchor resolution with
  grid snap along the wire axis.
* ``place_pwr_flag`` — PWR_FLAG mid-wire (never on-pin), default rotation 90.
* ``terminate_unused_unit`` — place + terminate an unused multi-unit unit.
* ``rename_net`` — label texts + power-port Values, replay-safe on 0 matches.
* ``delete_component`` cascade + ``delete_object`` match selectors.
* per-op INTERNAL containment (a handler bug never aborts the run).

Everything runs against a tiny self-contained ``.kicad_sym`` (no KiCad
install): R, +5V/GND/PWR_FLAG power symbols and a 2-unit OPAMP.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from akcli import ops
from akcli.cli import main
from akcli.errors import EXIT, AkcliError
from akcli.readers import kicad as kreader
from akcli.readers import sexpr
from akcli.writers import kicad as kwriter

_MIN_SYM = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "R" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "OPAMP" (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "OPAMP" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "OPAMP_1_1"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "+" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin input line (at -5.08 -2.54 0) (length 2.54)
        (name "-" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "out" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27))))))
    (symbol "OPAMP_2_1"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "+" (effects (font (size 1.27 1.27)))) (number "5" (effects (font (size 1.27 1.27)))))
      (pin input line (at -5.08 -2.54 0) (length 2.54)
        (name "-" (effects (font (size 1.27 1.27)))) (number "6" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "out" (effects (font (size 1.27 1.27)))) (number "7" (effects (font (size 1.27 1.27)))))))
  (symbol "+5V" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "+5V" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "+5V_1_1"
      (pin power_in line (at 0 0 90) (length 0)
        (name "+5V" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "GND" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "GND_1_1"
      (pin power_in line (at 0 0 270) (length 0)
        (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "PWR_FLAG" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "PWR_FLAG_1_1"
      (pin power_out line (at 0 0 90) (length 0)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
)
"""


@pytest.fixture()
def sym(tmp_path: Path) -> Path:
    p = tmp_path / "min.kicad_sym"
    p.write_text(_MIN_SYM)
    return p


@pytest.fixture()
def sheet(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_sch"
    p.write_text(f'(kicad_sch (version 20231120) (generator "akcli") '
                 f'(uuid "{uuid.uuid4()}") (paper "A4"))\n')
    return p


def _oplist(op_list):
    return {"protocol_version": 1, "target_format": "kicad", "ops": op_list}


def _draw(tmp_path, sheet, sym, op_list) -> None:
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(_oplist(op_list)))
    assert main(["draw", str(sheet), "--ops", str(f),
                 "--apply", "--symbols", str(sym)]) == EXIT["OK"]


def _nets(sheet):
    return {n.name: set(n.members) for n in kreader.read_sch(str(sheet)).nets}


# --------------------------------------------------------------------------- #
# connect_and_label expansion
# --------------------------------------------------------------------------- #
def test_connect_and_label_pin_refs_defer_mid_to_writer():
    doc = ops.expand_macros(_oplist([
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
    ]))
    wire, label = doc["ops"]
    assert wire == {"op": "add_wire", "vertices": ["R1.2", "R2.1"]}
    assert label == {"op": "add_net_label", "name": "N1", "at": "mid(R1.2,R2.1)"}
    assert ops.validate_oplist(doc) == []


def test_connect_and_label_points_resolve_mid_at_expansion():
    doc = ops.expand_macros(_oplist([
        {"op": "connect_and_label", "from": [0, 0], "to": [370, 0], "net": "H"},
    ]))
    label = doc["ops"][1]
    # mid x = 185 -> snapped to 200 on the 50-mil grid; horizontal wire -> 0
    assert label["at"] == [200, 0] and label["orientation"] == 0
    doc = ops.expand_macros(_oplist([
        {"op": "connect_and_label", "from": [100, 100], "to": [100, 470],
         "net": "V", "scope": "global", "orientation": 270},
    ]))
    label = doc["ops"][1]
    assert label["at"] == [100, 300]                  # 285 -> 300 along Y
    assert label["orientation"] == 270                # explicit wins
    assert label["scope"] == "global"


def test_connect_and_label_rejects_mixed_and_diagonal():
    with pytest.raises(AkcliError, match="BOTH"):
        ops.expand_macros(_oplist([
            {"op": "connect_and_label", "from": "R1.2", "to": [0, 0], "net": "X"},
        ]))
    with pytest.raises(AkcliError, match="axis-aligned"):
        ops.expand_macros(_oplist([
            {"op": "connect_and_label", "from": [0, 0], "to": [400, 400], "net": "X"},
        ]))


# --------------------------------------------------------------------------- #
# place_pwr_flag / terminate_unused_unit expansion
# --------------------------------------------------------------------------- #
def test_place_pwr_flag_expansion_defaults():
    doc = ops.expand_macros(_oplist([{"op": "place_pwr_flag",
                                      "at": "mid(R1.2,R2.1)"}]))
    (op,) = doc["ops"]
    assert op == {"op": "place_power_port", "lib_id": "power:PWR_FLAG",
                  "net_name": "PWR_FLAG", "at": "mid(R1.2,R2.1)", "rotation": 90}
    with pytest.raises(AkcliError):
        ops.expand_macros(_oplist([{"op": "place_pwr_flag", "at": "nonsense"}]))


def test_terminate_unused_unit_expansion():
    doc = ops.expand_macros(_oplist([{
        "op": "terminate_unused_unit", "designator": "U1", "lib_id": "X:OPA",
        "unit": 2, "at": [2000, 2000], "in_plus": "5", "in_minus": "6",
        "out": "7", "vcc": "+5V",
    }]))
    place, gnd, vcc, nc = doc["ops"]
    assert place == {"op": "place_component", "lib_id": "X:OPA",
                     "designator": "U1", "x_mil": 2000, "y_mil": 2000, "unit": 2}
    assert gnd == {"op": "place_power_port", "lib_id": "power:GND",
                   "net_name": "GND", "at": "U1.5"}
    assert vcc == {"op": "place_power_port", "lib_id": "power:+5V",
                   "net_name": "+5V", "at": "U1.6"}
    assert nc == {"op": "add_no_connect", "pin": "U1.7"}
    assert ops.validate_oplist(doc) == []


# --------------------------------------------------------------------------- #
# writer: mid() anchors end-to-end
# --------------------------------------------------------------------------- #
def _place_two_rs(tmp_path, sheet, sym, extra_ops):
    """R1 @ (1000,1000) and R2 @ (1000,1550): facing pins R1.2 (1000,1150)
    and R2.1 (1000,1400); the mid-wire snap point is y 1275 -> 1300."""
    _draw(tmp_path, sheet, sym, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 1000, "y_mil": 1550, "value": "2k"},
        *extra_ops,
    ])


def test_connect_and_label_mid_snaps_along_axis(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
    ])
    nets = _nets(sheet)
    assert ("R1", "2") in nets["N1"] and ("R2", "1") in nets["N1"]
    doc = sexpr.parse(sheet.read_text())
    (label,) = doc.find_all("label")
    at = label.find("at").children
    # x exact (25.4 mm = 1000 mil); y snapped 1275 -> 1300 mil = 33.02 mm;
    # auto-orientation 90 = along the vertical wire
    assert [at[1].value, at[2].value, at[3].value] == ["25.4", "33.02", "90"]


def test_place_pwr_flag_mid_wire(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
        {"op": "place_pwr_flag", "at": "mid(R1.2,R2.1)"},
    ])
    nets = _nets(sheet)
    assert "PWR_FLAG" not in nets                      # flag never names a net
    assert ("R1", "2") in nets["N1"] and ("R2", "1") in nets["N1"]
    doc = sexpr.parse(sheet.read_text())
    flag = next(s for s in doc.find_all("symbol")
                if s.find("lib_id") is not None
                and s.find("lib_id").children[1].value == "power:PWR_FLAG")
    at = flag.find("at").children
    assert [at[1].value, at[2].value, at[3].value] == ["25.4", "33.02", "90"]


def test_mid_anchor_rejects_non_axis_aligned(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R3",
         "x_mil": 2000, "y_mil": 1550},
    ])
    res = kwriter.apply(_oplist([
        {"op": "add_net_label", "name": "X", "at": "mid(R1.1,R3.2)"},
    ]), str(sheet), apply=False, sources=[str(sym)])
    (r,) = res
    assert r.status == "error" and r.error_code == "NON_ORTHOGONAL_WIRE"
    # the error names BOTH pins' world coordinates
    assert "R1.1 at (1000,850) mil" in r.message
    assert "R3.2 at (2000,1700) mil" in r.message


def test_terminate_unused_unit_end_to_end(tmp_path, sheet, sym):
    _draw(tmp_path, sheet, sym, [{
        "op": "terminate_unused_unit", "designator": "U1",
        "lib_id": "Device:OPAMP", "unit": 2, "at": [2000, 2000],
        "in_plus": "5", "in_minus": "6", "out": "7", "vcc": "+5V",
    }])
    nets = _nets(sheet)
    assert ("U1", "5") in nets["GND"]
    assert ("U1", "6") in nets["+5V"]
    doc = sexpr.parse(sheet.read_text())
    (nc,) = doc.find_all("no_connect")
    at = nc.find("at").children
    # U1.7 world = (2000+200, 2000) mil = (55.88, 50.8) mm
    assert [at[1].value, at[2].value] == ["55.88", "50.8"]


# --------------------------------------------------------------------------- #
# rename_net
# --------------------------------------------------------------------------- #
def test_rename_net_labels_and_power_values(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
        {"op": "place_power_port", "lib_id": "power:+5V", "net_name": "+5V",
         "at": "R1.1"},
    ])
    res = kwriter.apply(_oplist([
        {"op": "rename_net", "from": "N1", "to": "NET_X"},
        {"op": "rename_net", "from": "+5V", "to": "+3V3"},
        {"op": "rename_net", "from": "GHOST", "to": "ANY"},
    ]), str(sheet), apply=True, sources=[str(sym)])
    assert [r.status for r in res] == ["ok", "ok", "ok"]
    assert res[0].message == "renamed 1 object(s) from 'N1' to 'NET_X'"
    assert res[1].message == "renamed 1 object(s) from '+5V' to '+3V3'"
    assert "nothing renamed" in res[2].message         # replay-safe note
    nets = _nets(sheet)
    assert ("R1", "2") in nets["NET_X"] and "N1" not in nets
    assert ("R1", "1") in nets["+3V3"] and "+5V" not in nets


def test_rename_net_scope_restricts_label_kind(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
    ])
    res = kwriter.apply(_oplist([
        {"op": "rename_net", "from": "N1", "to": "NEVER", "scope": "global"},
    ]), str(sheet), apply=True, sources=[str(sym)])
    assert res[0].status == "ok" and "nothing renamed" in res[0].message
    assert "N1" in _nets(sheet)                        # local label untouched


# --------------------------------------------------------------------------- #
# delete_component cascade / delete_object match
# --------------------------------------------------------------------------- #
def test_delete_component_cascade(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
        {"op": "add_net_label", "name": "VIN", "at": "R1.1"},
    ])
    res = kwriter.apply(_oplist([
        {"op": "delete_component", "designator": "R1", "cascade": True},
    ]), str(sheet), apply=True, sources=[str(sym)])
    (r,) = res
    assert r.status == "ok"
    # cascade removed the R1.2->R2.1 wire AND the VIN label on R1.1
    assert r.message.startswith("cascade deleted 2 object(s): ")
    uuids = r.message.split(": ", 1)[1].split(", ")
    assert len(uuids) == 2 and all(len(u) == 36 for u in uuids)
    doc = sexpr.parse(sheet.read_text())
    assert doc.find_all("wire") == []
    assert {n.children[1].value for n in doc.find_all("label")} == {"N1"}
    sch = kreader.read_sch(str(sheet))
    assert [c.designator for c in sch.components if not
            c.designator.startswith("#")] == ["R2"]


def test_delete_component_without_cascade_keeps_wires(tmp_path, sheet, sym):
    _place_two_rs(tmp_path, sheet, sym, [
        {"op": "connect_and_label", "from": "R1.2", "to": "R2.1", "net": "N1"},
    ])
    res = kwriter.apply(_oplist([
        {"op": "delete_component", "designator": "R1"},
    ]), str(sheet), apply=False, sources=[str(sym)])
    assert res[0].status == "ok" and res[0].message == ""
    # dry-run: the wire survives in the edited doc (dangling gate reports it)


def test_delete_object_match_selectors(tmp_path, sheet, sym):
    # ambiguity: two labels named A -> error listing both candidate uuids
    res = kwriter.apply(_oplist([
        {"op": "add_net_label", "name": "A", "at": [2000, 2000]},
        {"op": "add_net_label", "name": "A", "at": [2500, 2000]},
        {"op": "delete_object", "match": {"kind": "label", "name": "A"}},
    ]), str(sheet), apply=False, sources=[str(sym)])
    assert res[2].status == "error" and res[2].error_code == "VERIFY_FAILED"
    assert "2 candidates" in res[2].message and res[2].message.count("-") >= 8

    # exactly one (name + at) -> deleted; wire matched by an endpoint
    res = kwriter.apply(_oplist([
        {"op": "add_net_label", "name": "A", "at": [2000, 2000]},
        {"op": "add_net_label", "name": "A", "at": [2500, 2000]},
        {"op": "add_wire", "vertices": [[2000, 2000], [2500, 2000]]},
        {"op": "delete_object",
         "match": {"kind": "label", "name": "A", "at": [2000, 2000]}},
        {"op": "delete_object", "match": {"kind": "wire", "at": [2500, 2000]}},
        {"op": "delete_object",
         "match": {"kind": "label", "name": "A", "at": [2000, 2000]}},
    ]), str(sheet), apply=False, sources=[str(sym)])
    assert [r.status for r in res] == ["ok"] * 6
    assert res[3].message == ""                        # deleted, silently
    assert res[4].message == ""
    assert "already absent" in res[5].message          # replay-safe note


# --------------------------------------------------------------------------- #
# INTERNAL containment
# --------------------------------------------------------------------------- #
def test_handler_crash_is_contained_as_internal(tmp_path, sheet, sym, monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setitem(kwriter._HANDLERS, "add_junction", _boom)
    res = kwriter.apply(_oplist([
        {"op": "add_junction", "at": [1000, 1000]},
        {"op": "add_text", "text": "still runs", "at": [1000, 1200]},
    ]), str(sheet), apply=False, sources=[str(sym)])
    assert res[0].status == "error"
    assert res[0].error_code == "INTERNAL"
    assert res[0].message == "ValueError: kaboom"
    assert res[1].status == "ok"                       # the run went on


# --------------------------------------------------------------------------- #
# review-round regressions: mid() exact alignment, cascade junction safety
# --------------------------------------------------------------------------- #
_OFFGRID_SYM = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "Roff" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "X" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Roff" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "Roff_1_1"
      (pin passive line (at 0.508 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0.508 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
)
"""


def test_mid_anchor_rejects_small_misalignment(tmp_path, sheet, sym):
    # An off-grid metric-library pin (20-mil cross-axis offset INSIDE the
    # symbol — placement coordinates themselves always snap to grid) used to
    # fall inside a 25-mil tolerance and produced a grid-snapped anchor OFF
    # the (slightly diagonal) wire: the label silently attached to nothing.
    # Exact alignment is now required.
    off = tmp_path / "off.kicad_sym"
    off.write_text(_OFFGRID_SYM)
    f = tmp_path / "setup.json"
    f.write_text(json.dumps(_oplist([
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:Roff", "designator": "X1",
         "x_mil": 1000, "y_mil": 1550},
    ])))
    assert main(["draw", str(sheet), "--ops", str(f), "--apply",
                 "--symbols", str(sym), "--symbols", str(off)]) == EXIT["OK"]
    res = kwriter.apply(_oplist([
        {"op": "add_net_label", "name": "X", "at": "mid(R1.2,X1.1)"},
    ]), str(sheet), apply=False, sources=[str(sym), str(off)])
    (r,) = res
    assert r.status == "error" and r.error_code == "NON_ORTHOGONAL_WIRE"


def _x_crossing(tmp_path, sheet, sym, with_vertical=True):
    """R3.1 pin tip on (2000,2000); a horizontal wire through that point,
    optionally a vertical one crossing there too (pure X)."""
    vertical = ([
        {"op": "place_component", "lib_id": "Device:R", "designator": "R5",
         "x_mil": 2000, "y_mil": 1650},                    # R5.2 -> (2000,1800)
        {"op": "add_wire", "vertices": [[2000, 1800], [2000, 2200]]},
        {"op": "add_net_label", "name": "B", "at": [2000, 2200],
         "scope": "global"},
    ] if with_vertical else [])
    _draw(tmp_path, sheet, sym, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R4",
         "x_mil": 1800, "y_mil": 1850},                    # R4.2 -> (1800,2000)
        {"op": "place_component", "lib_id": "Device:R", "designator": "R3",
         "x_mil": 2000, "y_mil": 2150},                    # R3.1 -> (2000,2000)
        {"op": "add_wire", "vertices": [[1800, 2000], [2200, 2000]]},
        {"op": "add_net_label", "name": "A", "at": [2200, 2000],
         "scope": "global"},
        *vertical,
    ])
    doc = sexpr.parse(sheet.read_text())
    assert doc.find_all("junction"), "precondition: auto-junction expected"


def test_cascade_keeps_junction_needed_by_surviving_wires(tmp_path, sheet, sym):
    # Pure-X crossing: cascade-deleting R3 must NOT delete the junction —
    # the two untouched wires stay joined only through it, and
    # auto_junctions never re-adds pure-X joins.
    _x_crossing(tmp_path, sheet, sym, with_vertical=True)
    res = kwriter.apply(_oplist([
        {"op": "delete_component", "designator": "R3", "cascade": True},
    ]), str(sheet), apply=True, sources=[str(sym)])
    assert res[0].status == "ok"
    doc = sexpr.parse(sheet.read_text())
    assert len(doc.find_all("junction")) == 1          # junction survived
    nets = _nets(sheet)
    holder = next(name for name, m in nets.items() if ("R4", "2") in m)
    assert ("R5", "2") in nets[holder]                 # X-net still one piece


def test_cascade_deletes_junction_only_the_component_needed(tmp_path, sheet, sym):
    # Same shape without the vertical wire: the junction only joined R3's
    # pin to the horizontal wire, so it goes with the component.
    _x_crossing(tmp_path, sheet, sym, with_vertical=False)
    res = kwriter.apply(_oplist([
        {"op": "delete_component", "designator": "R3", "cascade": True},
    ]), str(sheet), apply=True, sources=[str(sym)])
    assert res[0].status == "ok" and "cascade deleted" in res[0].message
    doc = sexpr.parse(sheet.read_text())
    assert doc.find_all("junction") == []
    assert ("R4", "2") in _nets(sheet)["A"]            # wire + net intact
