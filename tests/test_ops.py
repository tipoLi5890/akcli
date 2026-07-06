"""Tests for the op-list vocabulary and validator (ops.py)."""

from __future__ import annotations

import json

import pytest

from altium_kicad_cli import ops
from altium_kicad_cli.errors import AkcliError


def _doc(op_list):
    return {"protocol_version": 1, "target_format": "kicad", "ops": op_list}


def test_protocol_version_and_op_names():
    assert ops.PROTOCOL_VERSION == 1
    assert "place_component" in ops.OP_NAMES
    assert "place_gnd" in ops.OP_NAMES and "place_vcc" in ops.OP_NAMES
    # 13 original + delete_component / delete_object / move_component
    assert len(ops.OP_NAMES) == 16
    assert {"delete_component", "delete_object", "move_component"} <= ops.OP_NAMES


def test_valid_oplist_has_no_errors():
    doc = _doc([
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 100, "y_mil": 200, "rotation": 90},
        {"op": "add_wire", "vertices": [[0, 0], [0, 100], "U1.1"]},
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND", "at": [0, 0]},
    ])
    assert ops.validate_oplist(doc) == []


def test_protocol_mismatch():
    doc = _doc([])
    doc["protocol_version"] = 2
    errs = ops.validate_oplist(doc)
    assert any(e.code == "PROTOCOL_MISMATCH" for e in errs)


def test_unknown_op_is_unsupported():
    errs = ops.validate_oplist(_doc([{"op": "frobnicate"}]))
    assert any(e.code == "OP_UNSUPPORTED" for e in errs)


def test_free_angle_rotation_rejected():
    errs = ops.validate_oplist(_doc([
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 0, "y_mil": 0, "rotation": 45},
    ]))
    assert any(e.code == "BAD_ANGLE" for e in errs)


def test_add_text_free_angle_allowed():
    errs = ops.validate_oplist(_doc([
        {"op": "add_text", "text": "note", "at": [0, 0], "angle": 33.5},
    ]))
    assert errs == []


def test_malformed_wire_vertices_rejected():
    errs = ops.validate_oplist(_doc([{"op": "add_wire", "vertices": [[0, 0]]}]))
    assert any(e.code == "NON_ORTHOGONAL_WIRE" for e in errs)
    errs2 = ops.validate_oplist(_doc([{"op": "add_wire", "vertices": [[0, 0], [1, 2, 3]]}]))
    assert any(e.code == "NON_ORTHOGONAL_WIRE" for e in errs2)


def test_missing_required_field():
    errs = ops.validate_oplist(_doc([{"op": "place_component", "lib_id": "Device:R"}]))
    assert any(e.code == "OP_UNSUPPORTED" and "designator" in e.message for e in errs)


def test_bad_target_format():
    doc = _doc([])
    doc["target_format"] = "eagle"
    errs = ops.validate_oplist(doc)
    assert any(e.code == "OP_UNSUPPORTED" for e in errs)


def test_endpoint_pin_ref_accepted_but_only_for_wire():
    # add_bus only accepts plain points, not pin refs
    errs = ops.validate_oplist(_doc([{"op": "add_bus", "vertices": ["U1.1", [0, 0]]}]))
    assert any(e.code == "NON_ORTHOGONAL_WIRE" for e in errs)


def test_load_oplist_roundtrip(tmp_path):
    p = tmp_path / "ops.json"
    doc = _doc([{"op": "add_junction", "at": [0, 0]}])
    p.write_text(json.dumps(doc))
    loaded = ops.load_oplist(p)
    assert loaded == doc


def test_load_oplist_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(AkcliError) as ei:
        ops.load_oplist(p)
    assert ei.value.code == "OP_UNSUPPORTED"


def test_op_accessor():
    op = ops.Op({"op": "add_junction", "at": [1, 2]})
    assert op.name == "add_junction"
    assert op["at"] == [1, 2]
    assert op.get("missing", 7) == 7


def test_capabilities_loadable():
    cap = ops.load_capabilities()
    assert cap["ops"]["add_bus"]["altium"] is False
    assert cap["ops"]["place_component"]["kicad"] is True
