"""Macro-op validation that the CLI actually reaches.

Three regressions, all of them "akcli said OK and the circuit was wrong":

* ``plan``/``draw``/``ops validate`` expanded macros BEFORE validating, so the
  validator's macro branch was dead code on every CLI path — a misspelled or
  hallucinated macro field was silently dropped by the expander
  (:func:`akcli.ops.prepare_oplist` now validates first).
* macro designators are assigned POSITIONALLY, so ``place_led_indicator`` with
  ``["D1", "R3"]`` wrote a resistor named D1 and an LED named R3 — electrically
  identical, BOM corrupt, zero findings.
* two power ports on one anchor stack invisibly and only one survives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import ops
from akcli.errors import EXIT

from ._cli_harness import blank_sheet, json_of, run_cli


def _write(tmp_path: Path, *oplist_ops, name: str = "ops.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"protocol_version": 1, "target_format": "kicad",
                             "ops": list(oplist_ops)}),
                 encoding="utf-8", newline="\n")
    return p


def _validate(tmp_path: Path, *oplist_ops) -> dict:
    return json_of(["ops", "validate", str(_write(tmp_path, *oplist_ops)),
                    "--json"], expect=(EXIT["OK"], EXIT["OPLIST"]))


def _messages(payload: dict) -> str:
    return " | ".join(e["message"] for e in payload["errors"])


# --------------------------------------------------------------------------- #
# 1. macro fields are validated on the CLI path
# --------------------------------------------------------------------------- #
def test_macro_field_typo_is_rejected_with_did_you_mean(tmp_path):
    """``valu`` instead of ``value`` used to expand into a default 10k part."""
    payload = _validate(tmp_path, {
        "op": "place_pullup", "net": "SDA", "rail_net": "3V3",
        "x_mil": 1000, "y_mil": 1000, "designator": "R9", "valu": "4.7k"})
    assert payload["valid"] is False
    assert "unknown field 'valu'" in _messages(payload)
    assert "did you mean 'value'" in _messages(payload)


def test_macro_hallucinated_field_is_rejected(tmp_path):
    payload = _validate(tmp_path, {
        "op": "place_pullup", "net": "SDA", "rail_net": "3V3",
        "x_mil": 1000, "y_mil": 1000, "totally_bogus": 42})
    assert payload["valid"] is False
    assert "unknown field 'totally_bogus'" in _messages(payload)


def test_macro_missing_required_field_is_rejected(tmp_path):
    payload = _validate(tmp_path, {
        "op": "place_pullup", "net": "SDA", "x_mil": 1000, "y_mil": 1000})
    assert payload["valid"] is False
    assert "'rail_net'" in _messages(payload)


def test_valid_macro_still_passes(tmp_path):
    payload = _validate(tmp_path, {
        "op": "place_pullup", "net": "SDA", "rail_net": "3V3",
        "x_mil": 1000, "y_mil": 1000, "designator": "R9", "value": "4.7k"})
    assert payload["valid"] is True, payload["errors"]
    assert payload["op_count"] == 3          # the EXPANDED core ops


def test_draw_refuses_the_bad_macro_and_writes_nothing(tmp_path):
    """The refusal reaches plan/draw too, not just `ops validate`."""
    target = blank_sheet(tmp_path)
    before = target.read_bytes()
    opsfile = _write(tmp_path, {
        "op": "place_decoupling", "power_net": "3V3", "x_mil": 1000,
        "y_mil": 1000, "designator": "C9", "valu": "100n"})
    payload = json_of(["draw", str(target), "--ops", str(opsfile), "--apply",
                       "--json"], expect=EXIT["OPLIST"])
    assert payload["applied"] is False and payload["status"] == "refused"
    assert "unknown field 'valu'" in json.dumps(payload)
    assert target.read_bytes() == before


def test_bad_macro_never_expands(tmp_path):
    """A rejected macro must not reach the expander (it would invent a part)."""
    doc = {"protocol_version": 1, "target_format": "kicad", "ops": [
        {"op": "place_pullup", "net": "SDA", "rail_net": "3V3", "x_mil": 0,
         "y_mil": 0, "valu": "4.7k"}]}
    expanded, errs = ops.prepare_oplist(doc)
    assert errs and expanded["ops"][0]["op"] == "place_pullup"


# --------------------------------------------------------------------------- #
# error indices point at the AUTHOR'S op-list, not the expansion
# --------------------------------------------------------------------------- #
def test_post_expansion_error_index_maps_back_to_the_authors_op(tmp_path):
    payload = _validate(
        tmp_path,
        # 6 expanded ops — everything after this shifts if indices are raw
        {"op": "place_led_indicator", "x_mil": 3000, "y_mil": 3000,
         "net": "BLINK", "designators": ["R1", "D1"]},
        {"op": "place_gnd", "at": [1000, 1000]},
        {"op": "place_vcc", "at": [1000, 1000]},
    )
    assert payload["valid"] is False
    idx = [e["op_index"] for e in payload["errors"]]
    assert idx == [2], payload["errors"]      # the author's third op


# --------------------------------------------------------------------------- #
# 2. designator role slots (BOM corruption guard)
# --------------------------------------------------------------------------- #
_GOOD_MACROS = {
    "place_divider": {"op": "place_divider", "x_mil": 1000, "y_mil": 1000,
                      "top_net": "A", "mid_net": "B", "bottom_net": "C",
                      "designators": ["R1", "R2"]},
    "place_decoupling": {"op": "place_decoupling", "power_net": "3V3",
                         "x_mil": 1000, "y_mil": 1000, "designator": "C7"},
    "place_pullup": {"op": "place_pullup", "net": "SDA", "rail_net": "3V3",
                     "x_mil": 1000, "y_mil": 1000, "designator": "R7"},
    "place_led_indicator": {"op": "place_led_indicator", "x_mil": 1000,
                            "y_mil": 1000, "net": "BLINK",
                            "designators": ["R4", "D4"]},
    "place_rc_filter": {"op": "place_rc_filter", "x_mil": 1000, "y_mil": 1000,
                        "in_net": "IN", "out_net": "OUT",
                        "designators": ["R5", "C5"]},
    "place_crystal": {"op": "place_crystal", "x_mil": 1000, "y_mil": 1000,
                      "in_net": "XI", "out_net": "XO",
                      "designators": ["Y1", "C1", "C2"]},
}

# every slot the role table constrains, with a WRONG prefix for that slot
_BAD_SLOTS = [
    ("place_divider", ["C1", "R2"], 0, "resistor"),
    ("place_divider", ["R1", "D2"], 1, "resistor"),
    ("place_decoupling", "R7", 0, "capacitor"),
    ("place_pullup", "C7", 0, "resistor"),
    # THE bug: the intuitive [D, R] reading order for a [R, D] slot list
    ("place_led_indicator", ["D4", "R4"], 0, "resistor"),
    ("place_led_indicator", ["R4", "C4"], 1, "LED"),
    ("place_rc_filter", ["C5", "R5"], 0, "resistor"),
    ("place_rc_filter", ["R5", "R6"], 1, "capacitor"),
    ("place_crystal", ["C1", "C2", "C3"], 0, "crystal"),
    ("place_crystal", ["Y1", "R1", "C2"], 1, "capacitor"),
    ("place_crystal", ["Y1", "C1", "R2"], 2, "capacitor"),
]


@pytest.mark.parametrize("macro", sorted(_GOOD_MACROS))
def test_conventional_designators_pass(tmp_path, macro):
    payload = _validate(tmp_path, _GOOD_MACROS[macro])
    assert payload["valid"] is True, payload["errors"]


@pytest.mark.parametrize("macro,desigs,slot,role", _BAD_SLOTS)
def test_role_mismatched_designator_is_rejected(tmp_path, macro, desigs, slot, role):
    op = dict(_GOOD_MACROS[macro])
    op["designator" if isinstance(desigs, str) else "designators"] = desigs
    payload = _validate(tmp_path, op)
    assert payload["valid"] is False, op
    msg = _messages(payload)
    assert f"slot {slot} is the {role}" in msg
    assert "ops template" in msg          # points at the canonical slot order
    # the printed slot order is the template's own default designators
    field, _roles = ops._MACRO_DESIGNATOR_ROLES[macro]
    default = ops.MACRO_OPTIONAL[macro][field]
    order = ([default] if isinstance(default, str) else default)
    assert f"slot order is [{', '.join(d.rstrip('0123456789') for d in order)}]" in msg


def test_role_table_covers_every_macro_designator_slot():
    """Census: a new macro with designators must declare its slot roles."""
    for macro in ops.MACRO_OPS:
        fields = (set(ops.MACRO_REQUIRED.get(macro, []))
                  | set(ops.MACRO_OPTIONAL.get(macro, {})))
        has_desigs = bool(fields & {"designator", "designators"})
        constrained = macro in ops._MACRO_DESIGNATOR_ROLES
        if not has_desigs:
            assert not constrained, macro
            continue
        # terminate_unused_unit's part is caller-chosen: role-NEUTRAL by design
        assert constrained or macro == "terminate_unused_unit", (
            f"{macro} has designator slots but no role entry")
    # the declared slot counts match the expanders' defaults
    for macro, (field, roles) in ops._MACRO_DESIGNATOR_ROLES.items():
        default = ops.MACRO_OPTIONAL[macro][field]
        want = 1 if field == "designator" else len(default)
        assert len(roles) == want, macro
        assert all(r in ops._ROLE_PREFIXES for r in roles), macro


def test_lib_id_override_to_another_part_class_disables_the_role_check(tmp_path):
    """A caller who swaps the macro's part owns the refdes convention."""
    op = dict(_GOOD_MACROS["place_divider"])
    op["lib_id"] = "Device:Thermistor"
    op["designators"] = ["TH1", "TH2"]
    assert _validate(tmp_path, op)["valid"] is True
    # ...but a same-class override (Device:R_Small) keeps it
    op2 = dict(_GOOD_MACROS["place_divider"])
    op2["lib_id"] = "Device:R_Small"
    op2["designators"] = ["C1", "C2"]
    assert _validate(tmp_path, op2)["valid"] is False


def test_swapped_led_designators_used_to_reach_the_file(tmp_path):
    """Proof the guard is load-bearing: the expansion really does swap parts."""
    expanded = ops.expand_macros({"ops": [
        {"op": "place_led_indicator", "x_mil": 0, "y_mil": 0, "net": "BLINK",
         "designators": ["D1", "R3"]}]})
    placed = {o["designator"]: o["lib_id"] for o in expanded["ops"]
              if o["op"] == "place_component"}
    assert placed == {"D1": "Device:R", "R3": "Device:LED"}


# --------------------------------------------------------------------------- #
# 3. stacked power ports
# --------------------------------------------------------------------------- #
def test_two_power_ports_on_one_point_are_rejected(tmp_path):
    payload = _validate(
        tmp_path,
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": [2000, 2000]},
        {"op": "place_power_port", "lib_id": "power:+5V", "net_name": "+5V",
         "at": [2000, 2000]},
    )
    assert payload["valid"] is False
    assert "duplicate power port" in _messages(payload)
    assert payload["errors"][0]["op_index"] == 1
    assert "op [0]" in _messages(payload)


def test_power_ports_on_distinct_points_pass(tmp_path):
    payload = _validate(
        tmp_path,
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": [2000, 2000]},
        {"op": "place_power_port", "lib_id": "power:+5V", "net_name": "+5V",
         "at": [2000, 2100]},
    )
    assert payload["valid"] is True, payload["errors"]


def test_stacked_sugar_and_pin_anchored_power_ports_are_rejected(tmp_path):
    """The sugar ops and ``"REF.PIN"`` anchors share the lint."""
    assert _validate(
        tmp_path,
        {"op": "place_gnd", "at": "U1.7"},
        {"op": "place_vcc", "at": "U1.7"},
    )["valid"] is False


def test_pwr_flag_may_share_a_rail_port_point(tmp_path):
    """The prescribed pattern: a flag marks the rail port's node as driven.

    ``place_pwr_flag`` expands to a ``place_power_port`` on ``power:PWR_FLAG``,
    so a lint keyed on the coordinate alone would refuse the exact shape the
    docs teach. ``writers.connectivity`` and netbuild both exempt the flag
    (it names no net) and SPEC risk #7 says a flag may share a port's point —
    the op-list lint has to agree, or the two layers disagree about a legal
    design.
    """
    payload = _validate(
        tmp_path,
        {"op": "place_gnd", "at": [1500, 1500]},
        {"op": "place_pwr_flag", "at": [1500, 1500]},
    )
    assert payload["valid"] is True, payload["errors"]


def test_two_pwr_flags_on_one_point_are_rejected(tmp_path):
    """Species-aware, not flag-blind: two flags still overlap into one node."""
    payload = _validate(
        tmp_path,
        {"op": "place_pwr_flag", "at": [1500, 1500]},
        {"op": "place_pwr_flag", "at": [1500, 1500]},
    )
    assert payload["valid"] is False
    assert "duplicate power port" in _messages(payload)


def test_draw_refuses_stacked_power_ports(tmp_path):
    target = blank_sheet(tmp_path)
    before = target.read_bytes()
    opsfile = _write(
        tmp_path,
        {"op": "place_gnd", "at": [2000, 2000]},
        {"op": "place_gnd", "at": [2000, 2000]})
    rc, _, err = run_cli(["draw", str(target), "--ops", str(opsfile), "--apply"])
    assert rc == EXIT["OPLIST"]
    assert "duplicate power port" in err
    assert target.read_bytes() == before
