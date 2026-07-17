"""Relative placement — ``anchor`` (+ ``offset_mil``) on place/move_component.

The anchor ("REF.PIN" pin tip or bare "REF" component origin) resolves at
WRITER execution time against the live document, so it may reference a part
placed earlier in the same op-list. The offset is world-frame; the result
grid-snaps. Exactly one of x_mil/y_mil or anchor per op (validator-enforced).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import ops
from akcli.errors import AkcliError
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

SYMBOLS = Path(__file__).parent / "fixtures" / "kicad" / "symbols"
DEVICE = str(SYMBOLS / "Device.kicad_sym")


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


def _doc(*ops_list, **envelope):
    return {"protocol_version": 1, "target_format": "kicad",
            "ops": list(ops_list), **envelope}


def _pos(tgt: Path) -> dict:
    return {c.designator: (c.x_mil, c.y_mil)
            for c in kreader.read_sch(str(tgt)).components}


# --------------------------------------------------------------------------- #
# validator rules
# --------------------------------------------------------------------------- #
def test_exactly_one_position_form():
    both = {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
            "x_mil": 0, "y_mil": 0, "anchor": "U1.1"}
    errs = ops.validate_oplist(_doc(both))
    assert any("not both" in e.message for e in errs)

    neither = {"op": "place_component", "lib_id": "Device:R", "designator": "R1"}
    errs = ops.validate_oplist(_doc(neither))
    assert any("x_mil" in e.message for e in errs)
    assert any("anchor" in e.message for e in errs)

    dangling_offset = {"op": "move_component", "designator": "R1",
                       "x_mil": 0, "y_mil": 0, "offset_mil": [100, 0]}
    errs = ops.validate_oplist(_doc(dangling_offset))
    assert any("offset_mil requires anchor" in e.message for e in errs)

    ok = {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
          "anchor": "U1.3", "offset_mil": [200, 0]}
    assert not ops.validate_oplist(_doc(ok))
    ok_ref = {"op": "move_component", "designator": "R1", "anchor": "U1"}
    assert not ops.validate_oplist(_doc(ok_ref))


def test_anchor_shape_checked():
    bad = {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
           "anchor": "mid(A.1,B.2)"}
    errs = ops.validate_oplist(_doc(bad))
    assert any('"REF" or "REF.PIN"' in e.message for e in errs)


# --------------------------------------------------------------------------- #
# writer resolution
# --------------------------------------------------------------------------- #
def test_place_anchored_to_pin_of_earlier_op(tmp_path):
    """The anchor may be placed earlier in the SAME op-list."""
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
         "anchor": "R1.1", "offset_mil": [400, 0]},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C2",
         "anchor": "R1"},                       # bare ref = component origin
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    pos = _pos(tgt)
    # Device:R pin 1 sits 150 mil above the origin -> (1000, 850); +400 x.
    assert pos["C1"] == (1400, 850)
    assert pos["C2"] == pos["R1"] == (1000, 1000)


def test_move_component_by_anchor(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 3000, "y_mil": 3000},
        {"op": "move_component", "designator": "R2",
         "anchor": "R1", "offset_mil": [600, 0]},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    assert _pos(tgt)["R2"] == (1600, 1000)


def test_anchored_result_grid_snaps(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
         "anchor": "R1", "offset_mil": [130, 20]},   # off-grid offset
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    x, y = _pos(tgt)["C1"]
    assert x % 50 == 0 and y % 50 == 0


def test_unresolvable_anchor_fails_verify(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc({"op": "place_component", "lib_id": "Device:C", "designator": "C1",
              "anchor": "U9.4"})
    rs = kw.apply(d, str(tgt), apply=False, sources=[DEVICE])
    assert rs[0].status == "error"
    assert rs[0].error_code == "VERIFY_FAILED"


def test_reapply_is_byte_identical(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
         "anchor": "R1.1", "offset_mil": [400, 0]},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    first = tgt.read_bytes()
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    assert tgt.read_bytes() == first


# --------------------------------------------------------------------------- #
# macro + group composition
# --------------------------------------------------------------------------- #
def test_place_decoupling_anchored_to_pin(tmp_path):
    """The flagship case: a bypass cap dropped next to the pin it decouples."""
    tgt = _blank_sch(tmp_path)
    d = ops.expand_macros(_doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "U1",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "place_decoupling", "anchor": "U1.1", "offset_mil": [300, 100],
         "power_net": "VCC", "designator": "C7"},
    ))
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    assert _pos(tgt)["C7"] == (2300, 1950)      # pin1 (2000,1850) + offset


def test_macro_rejects_mixed_position_forms():
    with pytest.raises(AkcliError) as ei:
        ops.expand_macros(_doc(
            {"op": "place_pullup", "x_mil": 0, "y_mil": 0, "anchor": "U1.1",
             "net": "SDA", "rail_net": "VCC"}))
    assert "not both" in ei.value.message
    with pytest.raises(AkcliError) as ei:
        ops.expand_macros(_doc(
            {"op": "place_decoupling", "offset_mil": [0, 0], "power_net": "V"}))
    assert "offset_mil requires anchor" in ei.value.message


def test_anchored_op_survives_resolve_groups(tmp_path):
    """resolve_groups must tolerate a point-less anchored op — and still tag it."""
    d = _doc(
        {"op": "place_component", "group": "M", "lib_id": "Device:R",
         "designator": "R1", "x_mil": 100, "y_mil": 100},
        {"op": "place_component", "group": "M", "lib_id": "Device:C",
         "designator": "C1", "anchor": "R1.1", "offset_mil": [200, 0]},
        groups={"M": {"origin": [1000, 1000]}},
    )
    r = ops.resolve_groups(d)
    assert (r["ops"][0]["x_mil"], r["ops"][0]["y_mil"]) == (1100, 1100)
    anchored = r["ops"][1]
    assert "x_mil" not in anchored                  # untouched, no KeyError
    assert anchored["anchor"] == "R1.1"
    assert anchored["group"] == "M"                 # membership still persists

    tgt = _blank_sch(tmp_path)
    rs = kw.apply(r, str(tgt), apply=True, sources=[DEVICE])
    assert all(x.status == "ok" for x in rs)
    by_ref = {c.designator: c for c in kreader.read_sch(str(tgt)).components}
    assert by_ref["C1"].parameters.get("Group") == "M"
    assert _pos(tgt)["C1"] == (1300, 950)           # anchor pin + offset, not origin
