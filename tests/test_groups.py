"""Functional groups — envelope ``groups`` + per-op ``group`` tag (SPEC §2.1).

Covers the ops.py resolution pass (group-local -> absolute coordinates), the
macro interaction (children inherit the tag and stay group-local through
expansion), the two error codes, and the writer's hidden ``Group`` property
(membership persists in the sheet and round-trips through the reader).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from akcli import netdiff, ops
from akcli.errors import AkcliError
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"
POWER = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "power.kicad_sym"


def _doc(*ops_list, groups=None):
    d = {"protocol_version": 1, "target_format": "kicad", "ops": list(ops_list)}
    if groups is not None:
        d["groups"] = groups
    return d


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


# --------------------------------------------------------------------------- #
# resolve_groups — coordinate translation
# --------------------------------------------------------------------------- #
def test_place_component_translates_by_origin():
    d = _doc({"op": "place_component", "group": "P", "lib_id": "Device:R",
              "designator": "R1", "x_mil": 400, "y_mil": 300},
             groups={"P": {"origin": [1000, 2000]}})
    r = ops.resolve_groups(d)
    assert r["ops"][0]["x_mil"] == 1400
    assert r["ops"][0]["y_mil"] == 2300
    # tag stays on the op (the writer persists membership from it)
    assert r["ops"][0]["group"] == "P"


def test_ungrouped_ops_and_docs_pass_through():
    op = {"op": "place_component", "lib_id": "Device:R",
          "designator": "R1", "x_mil": 400, "y_mil": 300}
    d = _doc(op, groups={"P": {"origin": [1000, 2000]}})
    assert ops.resolve_groups(d) is d          # no grouped op: same object back
    assert op["x_mil"] == 400


def test_point_fields_translate_per_op_kind():
    groups = {"G": {"origin": [100, 200]}}
    d = _doc(
        {"op": "add_junction", "group": "G", "at": [50, 50]},
        {"op": "add_text", "group": "G", "text": "hi", "at": [0, 0]},
        {"op": "add_no_connect", "group": "G", "pin": [10, 20]},
        {"op": "move_component", "group": "G", "designator": "R1",
         "x_mil": 0, "y_mil": 0},
        {"op": "add_wire", "group": "G", "vertices": [[0, 0], [100, 0]]},
        groups=groups,
    )
    r = ops.resolve_groups(d)["ops"]
    assert r[0]["at"] == [150, 250]
    assert r[1]["at"] == [100, 200]
    assert r[2]["pin"] == [110, 220]
    assert (r[3]["x_mil"], r[3]["y_mil"]) == (100, 200)
    assert r[4]["vertices"] == [[100, 200], [200, 200]]


def test_pin_anchors_never_translate():
    groups = {"G": {"origin": [100, 200]}}
    d = _doc(
        {"op": "add_net_label", "group": "G", "name": "N", "at": "R1.1"},
        {"op": "place_power_port", "group": "G", "lib_id": "power:GND",
         "net_name": "GND", "at": "mid(R1.1,R2.2)"},
        {"op": "add_wire", "group": "G", "vertices": ["R1.1", [50, 0]]},
        groups=groups,
    )
    r = ops.resolve_groups(d)["ops"]
    assert r[0]["at"] == "R1.1"
    assert r[1]["at"] == "mid(R1.1,R2.2)"
    assert r[2]["vertices"] == ["R1.1", [150, 200]]


def test_resolve_is_pure_and_marker_prevents_double_translate():
    d = _doc({"op": "add_junction", "group": "G", "at": [0, 0]},
             groups={"G": {"origin": [100, 100]}})
    before = copy.deepcopy(d)
    r = ops.resolve_groups(d)
    assert d == before                          # input never mutated
    assert r["_groups_resolved"] is True
    r2 = ops.resolve_groups(r)                  # second call is a no-op
    assert r2["ops"][0]["at"] == [100, 100]


def test_macro_children_inherit_group_and_stay_local():
    d = _doc({"op": "place_decoupling", "group": "PWR", "x_mil": 300,
              "y_mil": 0, "power_net": "VCC", "designator": "C9"},
             groups={"PWR": {"origin": [1000, 500]}})
    r = ops.resolve_groups(ops.expand_macros(d))
    place = [o for o in r["ops"] if o["op"] == "place_component"][0]
    assert (place["x_mil"], place["y_mil"]) == (1300.0, 500.0)
    assert place["group"] == "PWR"
    labels = [o for o in r["ops"] if o["op"] == "add_net_label"]
    assert all(lab["group"] == "PWR" for lab in labels)
    assert all(lab["at"].startswith("C9.") for lab in labels)
    assert not ops.validate_oplist(r)


# --------------------------------------------------------------------------- #
# errors + validation
# --------------------------------------------------------------------------- #
def test_unknown_group_raises():
    d = _doc({"op": "add_junction", "group": "NOPE", "at": [0, 0]},
             groups={"P": {"origin": [0, 0]}})
    with pytest.raises(AkcliError) as ei:
        ops.resolve_groups(d)
    assert ei.value.code == "GROUP_UNKNOWN"


def test_group_without_origin_raises():
    d = _doc({"op": "add_junction", "group": "B", "at": [0, 0]},
             groups={"B": {"title": "no origin"}})
    with pytest.raises(AkcliError) as ei:
        ops.resolve_groups(d)
    assert ei.value.code == "GROUP_NO_ORIGIN"


def test_validate_lints_groups_envelope_shape():
    bad = _doc({"op": "add_junction", "at": [0, 0]},
               groups={"A": {"origin": [0, 0], "titel": "typo"},
                       "B": {"title": "missing origin"},
                       "C": {"origin": [0, 0], "frame": "yes"}})
    codes = {(e.code, e.message) for e in ops.validate_oplist(bad)}
    msgs = "\n".join(m for _, m in codes)
    assert any(c == "GROUP_NO_ORIGIN" for c, _ in codes)
    assert "did you mean 'title'" in msgs
    assert "frame must be true or false" in msgs


def test_validate_rejects_non_string_group_tag():
    d = _doc({"op": "add_junction", "group": 7, "at": [0, 0]})
    errs = ops.validate_oplist(d)
    assert any("group must be a string" in e.message for e in errs)


def test_group_tag_is_not_an_unknown_field():
    d = _doc({"op": "add_junction", "group": "G", "at": [0, 0]},
             {"op": "place_decoupling", "group": "G", "x_mil": 0, "y_mil": 0,
              "power_net": "VCC"})
    assert not ops.validate_oplist(d)


# --------------------------------------------------------------------------- #
# writer — hidden Group property persists membership
# --------------------------------------------------------------------------- #
def test_writer_persists_group_property(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "group": "PWR", "lib_id": "Device:R",
         "designator": "R1", "x_mil": 1000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R",
         "designator": "R2", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
        {"op": "place_power_port", "group": "PWR", "lib_id": "power:GND",
         "net_name": "GND", "at": [1000, 1600]},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[str(DEVICE), str(POWER)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]

    sch = kreader.read_sch(str(tgt))
    by_ref = {c.designator: c for c in sch.components}
    assert by_ref["R1"].parameters.get("Group") == "PWR"
    assert "Group" not in by_ref["R2"].parameters
    pwr = [c for c in sch.components if c.designator.startswith("#PWR")]
    assert pwr and pwr[0].parameters.get("Group") == "PWR"

    # hidden: the Group property node must carry (hide yes)
    text = tgt.read_text(encoding="utf-8")
    import re
    m = re.search(r'\(property "Group"[^)]*\)[^)]*\)[^)]*\)', text)
    assert '"Group" "PWR"' in text
    prop_idx = text.index('"Group" "PWR"')
    assert "hide yes" in text[prop_idx:prop_idx + 300]


def test_group_property_is_netdiff_neutral_and_idempotent(tmp_path):
    tgt = _blank_sch(tmp_path)
    base = [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "add_net_label", "name": "A", "at": "R1.1"},
        {"op": "add_net_label", "name": "B", "at": "R1.2"},
    ]
    grouped = _doc(*[dict(o, group="G1") for o in base],
                   groups={"G1": {"origin": [0, 0]}})
    grouped = ops.resolve_groups(grouped)
    rs = kw.apply(grouped, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    first = tgt.read_bytes()
    nets_before = kreader.read_sch(str(tgt)).nets

    # re-apply is byte-identical (deterministic uuids + property replace)
    rs = kw.apply(grouped, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    assert tgt.read_bytes() == first

    # the Group property influences no net (netbuild ignores properties)
    plain = _doc(*base)
    (tmp_path / "plain").mkdir(exist_ok=True)
    tgt2 = _blank_sch(tmp_path / "plain")
    rs = kw.apply(plain, str(tgt2), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    d = netdiff.diff(kreader.read_sch(str(tgt2)).nets, nets_before)
    assert d.equivalent, netdiff.format_summary(d)
