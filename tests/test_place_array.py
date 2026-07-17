"""``place_array`` macro — N identical parts in a row/column at a fixed pitch."""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import ops
from akcli.errors import AkcliError
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _doc(*ops_list, **envelope):
    return {"protocol_version": 1, "target_format": "kicad",
            "ops": list(ops_list), **envelope}


def _array(**kw_):
    base = {"op": "place_array", "lib_id": "Device:R",
            "designator_prefix": "R", "count": 4,
            "x_mil": 1000, "y_mil": 1000}
    base.update(kw_)
    return base


def test_expands_to_row_with_pitch():
    r = ops.expand_macros(_doc(_array(pitch_mil=300)))["ops"]
    assert [o["designator"] for o in r] == ["R1", "R2", "R3", "R4"]
    assert [o["x_mil"] for o in r] == [1000, 1300, 1600, 1900]
    assert all(o["y_mil"] == 1000 for o in r)
    assert not ops.validate_oplist(ops.expand_macros(_doc(_array())))


def test_directions_and_start_index():
    r = ops.expand_macros(_doc(_array(direction="down", start_index=5)))["ops"]
    assert [o["designator"] for o in r] == ["R5", "R6", "R7", "R8"]
    assert [o["y_mil"] for o in r] == [1000, 1400, 1800, 2200]
    r = ops.expand_macros(_doc(_array(direction="up", count=2)))["ops"]
    assert [o["y_mil"] for o in r] == [1000, 600]


def test_values_per_element_and_shared_value():
    r = ops.expand_macros(_doc(_array(values=["1k", "2k", "3k", "4k"])))["ops"]
    assert [o["value"] for o in r] == ["1k", "2k", "3k", "4k"]
    r = ops.expand_macros(_doc(_array(value="10k")))["ops"]
    assert all(o["value"] == "10k" for o in r)


def test_values_length_mismatch_fails():
    with pytest.raises(AkcliError) as ei:
        ops.expand_macros(_doc(_array(values=["1k", "2k"])))
    assert ei.value.code == "OP_UNSUPPORTED"
    assert "values must be 4" in ei.value.message


def test_bad_count_and_direction_fail():
    for bad in (0, -1, 201, "8", True):
        with pytest.raises(AkcliError):
            ops.expand_macros(_doc(_array(count=bad)))
    with pytest.raises(AkcliError) as ei:
        ops.expand_macros(_doc(_array(direction="diagonal")))
    assert "direction" in ei.value.message


def test_prefix_collision_caught_by_duplicate_lint():
    with pytest.raises(AkcliError) as ei:
        ops.expand_macros(_doc(_array(count=2),
                               _array(count=2, x_mil=5000, y_mil=5000)))
    assert "duplicate placement" in ei.value.message


def test_group_rides_onto_every_element():
    d = _doc(_array(group="LOADS", count=3, pitch_mil=500),
             groups={"LOADS": {"origin": [2000, 0]}})
    r = ops.resolve_groups(ops.expand_macros(d))["ops"]
    assert all(o["group"] == "LOADS" for o in r)
    assert [o["x_mil"] for o in r] == [3000, 3500, 4000]   # origin + local + k*pitch


def test_array_draws_and_persists(tmp_path):
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    d = ops.expand_macros(_doc(_array(value="4k7")))
    rs = kw.apply(d, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    refs = {c.designator for c in kreader.read_sch(str(tgt)).components}
    assert {"R1", "R2", "R3", "R4"} <= refs
