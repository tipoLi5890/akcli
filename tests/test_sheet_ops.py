"""``add_sheet`` — hierarchical sheet authoring (op-list -> ``(sheet ...)`` node).

Contract (SPEC §2.2): ``add_sheet`` emits a KiCad ``(sheet ...)`` node with
Sheetname/Sheetfile properties, stroke/fill defaults, a deterministic uuid,
sheet pins at computed edge coordinates, and the ``(instances)`` page block the
reader/eeschema resolve designator paths against. The referenced child file is
NOT created by the op. Wires attach to a sheet pin by its ``at`` + ``offset_mil``
coordinate — a label anchor, so the endpoint does not dangle. Cross-sheet
membership parity with eeschema lives in ``tests/test_kicad_parity.py``.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

from akcli import ops
from akcli.readers import kicad as kreader
from akcli.readers import sexpr
from akcli.report import Severity
from akcli.writers import kicad as kw

_LIB = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "RR" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "RR_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))))
)
"""


def _oplist(*op):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(op)}


def _seed(d: Path, name: str = "root.kicad_sch") -> Path:
    p = d / name
    p.write_text(
        f'(kicad_sch (version 20231120) (generator "akcli") '
        f'(uuid "{_uuid.uuid4()}") (paper "A4"))\n'
    )
    return p


def _apply(tgt: Path, lib: Path, *op, apply: bool = True):
    verify_out: list = []
    res = kw.apply(_oplist(*op), str(tgt), apply=apply,
                   sources=[str(lib)], verify_out=verify_out)
    return res, verify_out


def _sheet(doc: sexpr.SNode) -> sexpr.SNode:
    return doc.find("sheet")


def _props(node: sexpr.SNode) -> dict[str, str]:
    out = {}
    for p in node.find_all("property"):
        out[p.children[1].value] = p.children[2].value
    return out


def _at_mm(node: sexpr.SNode) -> tuple[float, float]:
    at = node.find("at")
    return (float(at.children[1].value), float(at.children[2].value))


# --------------------------------------------------------------------------- #
# node structure
# --------------------------------------------------------------------------- #
def test_add_sheet_emits_sheet_node(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    res, verify = _apply(
        tgt, lib,
        {"op": "add_sheet", "name": "power", "file": "power.kicad_sch",
         "at": [2000, 1000], "size": [1000, 800]},
    )
    assert [r.status for r in res] == ["ok"]
    assert [f for f in verify if f.severity in (Severity.ERROR, Severity.CRITICAL)] == []
    doc = sexpr.parse(tgt.read_text())
    sh = _sheet(doc)
    assert sh is not None
    props = _props(sh)
    assert props["Sheetname"] == "power"
    assert props["Sheetfile"] == "power.kicad_sch"
    # stroke/fill/uuid/instances all present
    assert sh.find("stroke") is not None and sh.find("fill") is not None
    assert sh.find("uuid") is not None
    assert sh.find("instances") is not None
    # box geometry: origin at (2000,1000) mil = (50.8, 25.4) mm
    assert _at_mm(sh) == (50.8, 25.4)


def test_sheet_instances_path_references_root_uuid(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    _apply(tgt, lib,
           {"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
            "at": [0, 0], "size": [1000, 800]})
    doc = sexpr.parse(tgt.read_text())
    root_uuid = doc.find("uuid").children[1].value
    inst = _sheet(doc).find("instances")
    path = inst.find("project").find("path")
    assert path.children[1].value == "/" + root_uuid
    assert path.find("page") is not None


# --------------------------------------------------------------------------- #
# sheet-pin edge coordinates
# --------------------------------------------------------------------------- #
def _pin_coords(tmp_path, side: str, offset_mil: float) -> tuple[float, float]:
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    _apply(tgt, lib,
           {"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
            "at": [2000, 1000], "size": [1000, 800],
            "pins": [{"name": "P", "type": "input", "side": side,
                      "offset_mil": offset_mil}]})
    pin = _sheet(sexpr.parse(tgt.read_text())).find("pin")
    return _at_mm(pin)


def test_sheet_pin_left_edge(tmp_path):
    # left edge: x = x0 (50.8 mm), y = y0 + offset (1000+200 mil = 30.48 mm)
    assert _pin_coords(tmp_path, "left", 200) == (50.8, 30.48)


def test_sheet_pin_right_edge(tmp_path):
    # right edge: x = x0 + w (2000+1000 mil = 76.2 mm)
    assert _pin_coords(tmp_path, "right", 200) == (76.2, 30.48)


def test_sheet_pin_top_edge(tmp_path):
    # top edge: y = y0 (25.4 mm), x = x0 + offset (2000+400 mil = 60.96 mm)
    assert _pin_coords(tmp_path, "top", 400) == (60.96, 25.4)


def test_sheet_pin_bottom_edge(tmp_path):
    # bottom edge: y = y0 + h (1000+800 mil = 45.72 mm)
    assert _pin_coords(tmp_path, "bottom", 400) == (60.96, 45.72)


def test_sheet_pin_carries_type_and_uuid(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    _apply(tgt, lib,
           {"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
            "at": [0, 0], "size": [1000, 800],
            "pins": [{"name": "CLK", "type": "output", "side": "right",
                      "offset_mil": 100}]})
    pin = _sheet(sexpr.parse(tgt.read_text())).find("pin")
    assert pin.children[1].value == "CLK"
    assert pin.children[2].value == "output"   # KiCad electrical-type token
    assert pin.find("uuid") is not None


# --------------------------------------------------------------------------- #
# wires attach to a sheet pin without dangling
# --------------------------------------------------------------------------- #
def test_wire_to_sheet_pin_does_not_dangle(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    res, verify = _apply(
        tgt, lib,
        {"op": "place_component", "lib_id": "RR", "designator": "R1",
         "x_mil": 1000, "y_mil": 1200},
        {"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
         "at": [2000, 1000], "size": [1000, 800],
         "pins": [{"name": "NET1", "type": "bidirectional", "side": "left",
                   "offset_mil": 200}]},
        # wire R1.1 -> the left sheet-pin coordinate (2000, 1200) mil
        {"op": "add_wire", "vertices": ["R1.1", [2000, 1200]]},
    )
    assert [r.status for r in res] == ["ok", "ok", "ok"]
    errs = [f for f in verify if f.severity in (Severity.ERROR, Severity.CRITICAL)]
    assert errs == [], [f.message for f in errs]


# --------------------------------------------------------------------------- #
# reader round-trip (Sheetname + cross-sheet membership via a child fixture)
# --------------------------------------------------------------------------- #
def _child_file(tmp_path: Path, root_uuid: str, sheet_uuid: str) -> None:
    """A hand-written child: R2 wired to a hierarchical_label 'NET1'.

    The child symbol's ``(instances)`` path is ``/<root>/<sheet>`` so the reader
    resolves R2 under the sheet instance (matching eeschema).
    """
    child_path = f"/{root_uuid}/{sheet_uuid}"
    lib_inline = """\
 (lib_symbols
  (symbol "RR" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "RR_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))))"""
    (tmp_path / "s.kicad_sch").write_text(
        '(kicad_sch (version 20231120) (generator "akcli")\n'
        f' (uuid "{_uuid.uuid4()}") (paper "A4")\n'
        + lib_inline + "\n"
        ' (symbol (lib_id "RR") (at 50.8 50.8 0) (unit 1)\n'
        f'   (uuid "{_uuid.uuid4()}")\n'
        '   (property "Reference" "R2" (at 53 49 0) (effects (font (size 1.27 1.27))))\n'
        '   (property "Value" "RR" (at 53 51 0) (effects (font (size 1.27 1.27))))\n'
        f'   (pin "1" (uuid "{_uuid.uuid4()}"))\n'
        f'   (pin "2" (uuid "{_uuid.uuid4()}"))\n'
        f'   (instances (project "noname" (path "{child_path}" (reference "R2") (unit 1)))))\n'
        ' (wire (pts (xy 50.8 46.99) (xy 50.8 40.64)) (stroke (width 0) (type default))\n'
        f'   (uuid "{_uuid.uuid4()}"))\n'
        ' (hierarchical_label "NET1" (shape bidirectional) (at 50.8 40.64 90)\n'
        '   (effects (font (size 1.27 1.27)))\n'
        f'   (uuid "{_uuid.uuid4()}"))\n'
        ')\n'
    )


def test_reader_reads_sheetname_and_crosses_sheet(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    _apply(
        tgt, lib,
        {"op": "place_component", "lib_id": "RR", "designator": "R1",
         "x_mil": 1000, "y_mil": 1200},
        {"op": "add_sheet", "name": "child", "file": "s.kicad_sch",
         "at": [2000, 1000], "size": [1000, 800],
         "pins": [{"name": "NET1", "type": "bidirectional", "side": "left",
                   "offset_mil": 200}]},
        {"op": "add_wire", "vertices": ["R1.1", [2000, 1200]]},
    )
    doc = sexpr.parse(tgt.read_text())
    root_uuid = doc.find("uuid").children[1].value
    sheet_uuid = _sheet(doc).find("uuid").children[1].value
    _child_file(tmp_path, root_uuid, sheet_uuid)

    sch = kreader.read_sch(tgt)
    assert sch.sheets == ["child"]
    assert {c.designator for c in sch.components} == {"R1", "R2"}
    net = next(n for n in sch.nets if ("R1", "1") in n.members)
    assert ("R2", "1") in net.members    # crosses the sheet boundary


# --------------------------------------------------------------------------- #
# idempotent replay
# --------------------------------------------------------------------------- #
def test_add_sheet_replay_is_byte_identical(tmp_path):
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(_LIB)
    tgt = _seed(tmp_path)
    op = {"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
          "at": [2000, 1000], "size": [1000, 800],
          "pins": [{"name": "NET1", "type": "input", "side": "left",
                    "offset_mil": 200}]}
    _apply(tgt, lib, op)
    first = tgt.read_text()
    _apply(tgt, lib, op)
    assert tgt.read_text() == first


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _codes(doc) -> list[str]:
    return [e.code for e in ops.validate_oplist(doc)]


def test_valid_add_sheet_passes_validator():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": [1000, 800],
                   "pins": [{"name": "P", "type": "input", "side": "left",
                             "offset_mil": 0}]})
    assert ops.validate_oplist(doc) == []


def test_add_sheet_missing_required_fields():
    doc = _oplist({"op": "add_sheet", "name": "s"})
    codes = _codes(doc)
    assert codes and all(c == "OP_UNSUPPORTED" for c in codes)


def test_add_sheet_bad_size_type():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": "big"})
    assert "OP_UNSUPPORTED" in _codes(doc)


def test_add_sheet_bad_pin_type_and_side():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": [100, 100],
                   "pins": [{"name": "P", "type": "analog", "side": "up",
                             "offset_mil": 0}]})
    errs = ops.validate_oplist(doc)
    msgs = " ".join(e.message for e in errs)
    assert "type" in msgs and "side" in msgs
    assert all(e.code == "OP_UNSUPPORTED" for e in errs)


def test_add_sheet_pin_missing_field():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": [100, 100],
                   "pins": [{"name": "P", "type": "input", "side": "left"}]})
    assert any("offset_mil" in e.message for e in ops.validate_oplist(doc))


def test_add_sheet_unknown_pin_field():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": [100, 100],
                   "pins": [{"name": "P", "type": "input", "side": "left",
                             "offset_mil": 0, "bogus": 1}]})
    assert any("bogus" in e.message for e in ops.validate_oplist(doc))


def test_add_sheet_unhashable_pin_type_does_not_crash():
    doc = _oplist({"op": "add_sheet", "name": "s", "file": "s.kicad_sch",
                   "at": [0, 0], "size": [100, 100],
                   "pins": [{"name": "P", "type": ["input"], "side": "left",
                             "offset_mil": 0}]})
    errs = ops.validate_oplist(doc)   # must not raise TypeError
    assert any("type" in e.message for e in errs)


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #
def test_add_sheet_template_is_valid():
    tmpl = ops.op_template("add_sheet")
    assert tmpl["op"] == "add_sheet"
    for field in ("name", "file", "at", "size"):
        assert field in tmpl
    doc = _oplist(tmpl)
    assert ops.validate_oplist(doc) == []
