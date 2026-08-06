"""Layout algebra for ``library new-symbol`` (`akcli.symbol_spec`).

The properties asserted here are the ones a hand-written symbol gets wrong:
pins off the grid, two pins on one point, and a body too small for the names it
has to contain. Everything is checked as an invariant over generated specs, not
against a golden coordinate list, so the tests still mean something after a
deliberate spacing change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.errors import AkcliError

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symlib"


def _spec(pins, **kw) -> ss.SymbolSpec:
    doc = {"protocol_version": 1, "name": kw.pop("name", "GENERIC_PART"),
           "pins": pins, **kw}
    return ss.parse_spec(doc, "<test>")


def _pin(number, side, name=None, ptype="passive", **kw) -> dict:
    return {"number": str(number), "name": name or f"P{number}",
            "type": ptype, "side": side, **kw}


def _load(name: str) -> ss.SymbolSpec:
    return ss.load(FIX / name)


# --------------------------------------------------------------------------- #
# grid + coincidence invariants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture", ["generic_opamp_8.spec.json",
                                     "generic_logic_14.spec.json"])
def test_every_pin_lands_on_the_100_mil_grid(fixture):
    lay = ss.layout(_load(fixture))
    off = [(p.spec.number, p.at) for p in lay.pins
           if p.x_mil % ss.GRID_MIL or p.y_mil % ss.GRID_MIL]
    assert off == [], f"{fixture}: off-grid pins {off}"


@pytest.mark.parametrize("n_left,n_right,n_top,n_bottom",
                         [(1, 1, 0, 0), (4, 4, 0, 0), (7, 2, 1, 1),
                          (0, 0, 5, 5), (3, 0, 8, 2), (12, 12, 4, 4)])
def test_no_two_pins_share_a_point_by_construction(n_left, n_right, n_top, n_bottom):
    """The generator's whole reason to exist: one coordinate, one pin."""
    pins, n = [], 0
    for side, count in (("left", n_left), ("right", n_right),
                        ("top", n_top), ("bottom", n_bottom)):
        for _ in range(count):
            n += 1
            pins.append(_pin(n, side))
    lay = ss.layout(_spec(pins))
    seats = [p.at for p in lay.pins]
    assert len(set(seats)) == len(seats), f"coincident pins in {seats}"


def test_pins_sit_exactly_one_pin_length_off_the_body_edge():
    lay = ss.layout(_spec([_pin(1, "left"), _pin(2, "right"),
                           _pin(3, "top"), _pin(4, "bottom")]))
    x0, y0, x1, y1 = lay.body
    at = {p.spec.number: p for p in lay.pins}
    assert at["1"].x_mil == x0 - ss.PIN_LENGTH_MIL and at["1"].orientation == 0
    assert at["2"].x_mil == x1 + ss.PIN_LENGTH_MIL and at["2"].orientation == 180
    assert at["3"].y_mil == y1 + ss.PIN_LENGTH_MIL and at["3"].orientation == 270
    assert at["4"].y_mil == y0 - ss.PIN_LENGTH_MIL and at["4"].orientation == 90


def test_top_and_bottom_columns_stay_inside_the_body_width():
    """A column poking past the body edge would land on the left/right pin column."""
    for n in range(1, 13):
        pins = [_pin(i, "top") for i in range(1, n + 1)]
        pins += [_pin(100 + i, "left") for i in range(3)]
        lay = ss.layout(_spec(pins))
        x0, x1 = lay.body[0], lay.body[2]
        cols = [p.x_mil for p in lay.pins if p.spec.side == "top"]
        assert min(cols) >= x0 + ss.BODY_MARGIN_MIL, (n, cols, lay.body)
        assert max(cols) <= x1 - ss.BODY_MARGIN_MIL, (n, cols, lay.body)


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
def test_side_order_is_group_first_appearance_then_spec_order():
    pins = [_pin(1, "left", name="B1", group="b"),
            _pin(2, "left", name="A1", group="a"),
            _pin(3, "left", name="B2", group="b"),
            _pin(4, "left", name="A2", group="a")]
    lay = ss.layout(_spec(pins))
    order = [p.spec.number for p in sorted(
        (p for p in lay.pins), key=lambda p: -p.y_mil)]
    # group "b" appears first in the document, so it takes the top rows;
    # within each group the document order is preserved.
    assert order == ["1", "3", "2", "4"]


def test_ungrouped_pins_keep_spec_order():
    pins = [_pin(n, "right") for n in (5, 1, 9, 3)]
    lay = ss.layout(_spec(pins))
    order = [p.spec.number for p in sorted(lay.pins, key=lambda p: -p.y_mil)]
    assert order == ["5", "1", "9", "3"]


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_layout_is_deterministic_for_identical_input():
    doc = json.loads((FIX / "generic_logic_14.spec.json").read_text(encoding="utf-8"))

    def geometry():
        lay = ss.layout(ss.parse_spec(json.loads(json.dumps(doc)), "<test>"))
        return (lay.body, [(p.spec.number, p.at, p.orientation) for p in lay.pins])

    assert geometry() == geometry()


# --------------------------------------------------------------------------- #
# body sizing
# --------------------------------------------------------------------------- #
def test_body_height_is_monotonic_in_pin_count():
    heights = []
    for n in range(1, 15):
        lay = ss.layout(_spec([_pin(i, "left") for i in range(1, n + 1)]))
        heights.append(lay.body_height_mil)
    assert heights == sorted(heights)
    assert heights[-1] > heights[0]


def test_body_width_is_monotonic_in_pin_name_length():
    widths = []
    for length in range(1, 20):
        lay = ss.layout(_spec([_pin(1, "left", name="N" * length),
                               _pin(2, "right", name="M")]))
        widths.append(lay.body_width_mil)
    assert widths == sorted(widths)
    assert widths[-1] > widths[0]


def test_body_width_accounts_for_both_name_columns():
    narrow = ss.layout(_spec([_pin(1, "left", name="A"),
                              _pin(2, "right", name="B")]))
    wide = ss.layout(_spec([_pin(1, "left", name="A" * 12),
                            _pin(2, "right", name="B" * 12)]))
    assert wide.body_width_mil > narrow.body_width_mil


def test_body_width_override_may_widen_but_never_shrink():
    base = ss.layout(_spec([_pin(1, "left", name="LONG_PIN_NAME_HERE"),
                            _pin(2, "right", name="ALSO_QUITE_LONG")]))
    wider = ss.layout(_spec([_pin(1, "left", name="LONG_PIN_NAME_HERE"),
                             _pin(2, "right", name="ALSO_QUITE_LONG")],
                            body={"width_mil": base.body_width_mil + 400}))
    assert wider.body_width_mil == base.body_width_mil + 400
    assert wider.body_width_overridden is True

    squeezed = ss.layout(_spec([_pin(1, "left", name="LONG_PIN_NAME_HERE"),
                                _pin(2, "right", name="ALSO_QUITE_LONG")],
                               body={"width_mil": 200}))
    assert squeezed.body_width_mil == base.body_width_mil
    assert squeezed.body_width_overridden is False
    assert any("may only widen" in w for w in squeezed.warnings)


def test_body_width_stays_a_multiple_of_two_grid_steps():
    """Half-width must be a whole grid step, or the pins fall off the grid."""
    for length in range(1, 25):
        lay = ss.layout(_spec([_pin(1, "left", name="X" * length)]))
        assert lay.body_width_mil % (2 * ss.GRID_MIL) == 0


# --------------------------------------------------------------------------- #
# stacked pins — the ONE sanctioned same-coordinate case
# --------------------------------------------------------------------------- #
def test_stacked_pins_share_their_base_coordinate():
    lay = ss.layout(_spec([
        _pin(1, "left", name="IN", ptype="input"),
        _pin(2, "top", name="VCC", ptype="power_in"),
        _pin(3, "top", name="VCC", ptype="power_in", stack_with="2"),
    ]))
    at = {p.spec.number: p.at for p in lay.pins}
    assert at["2"] == at["3"] != at["1"]


def test_stacked_pins_do_not_consume_a_row():
    plain = ss.layout(_spec([_pin(1, "left", name="A", ptype="input"),
                             _pin(2, "left", name="B", ptype="input")]))
    stacked = ss.layout(_spec([_pin(1, "left", name="A", ptype="input"),
                               _pin(2, "left", name="B", ptype="input"),
                               _pin(3, "left", name="B", ptype="input",
                                    stack_with="2")]))
    assert stacked.body == plain.body


# --------------------------------------------------------------------------- #
# refusals (MVP scope + spec hygiene)
# --------------------------------------------------------------------------- #
def _refusal(doc) -> AkcliError:
    with pytest.raises(AkcliError) as ei:
        ss.parse_spec(doc, "<test>")
    return ei.value


def test_refuses_multi_unit_request_by_name():
    err = _refusal({"protocol_version": 1, "name": "X", "units": 4,
                    "pins": [_pin(1, "left")]})
    assert err.code == "BAD_CONFIG"
    assert "multi-unit symbols are not supported yet" in err.message
    per_pin = _refusal({"protocol_version": 1, "name": "X",
                        "pins": [dict(_pin(1, "left"), unit=2)]})
    assert "multi-unit symbols are not supported yet" in per_pin.message


def test_refuses_duplicate_pin_numbers():
    err = _refusal({"protocol_version": 1, "name": "X",
                    "pins": [_pin(1, "left"), _pin(1, "right")]})
    assert err.code == "BAD_CONFIG"
    assert "duplicate pin number '1'" in err.message


def test_refuses_unknown_type_and_lists_the_vocabulary():
    err = _refusal({"protocol_version": 1, "name": "X",
                    "pins": [dict(_pin(1, "left"), type="analog")]})
    assert err.code == "BAD_CONFIG"
    for token in ss.PIN_TYPES:
        assert token in err.message


def test_refuses_stack_with_name_or_type_mismatch():
    mismatch_name = _refusal({"protocol_version": 1, "name": "X", "pins": [
        _pin(1, "top", name="VCC", ptype="power_in"),
        _pin(2, "top", name="VDD", ptype="power_in", stack_with="1")]})
    assert mismatch_name.code == "BAD_CONFIG"
    assert "SAME name and the SAME electrical type" in mismatch_name.message

    mismatch_type = _refusal({"protocol_version": 1, "name": "X", "pins": [
        _pin(1, "top", name="VCC", ptype="power_in"),
        _pin(2, "top", name="VCC", ptype="passive", stack_with="1")]})
    assert "SAME name and the SAME electrical type" in mismatch_type.message


def test_refuses_stack_with_unknown_target_self_or_chain():
    assert "names no pin" in _refusal(
        {"protocol_version": 1, "name": "X",
         "pins": [dict(_pin(1, "left"), stack_with="99")]}).message
    assert "points at itself" in _refusal(
        {"protocol_version": 1, "name": "X",
         "pins": [dict(_pin(1, "left"), stack_with="1")]}).message
    chain = _refusal({"protocol_version": 1, "name": "X", "pins": [
        _pin(1, "top", name="V", ptype="power_in"),
        _pin(2, "top", name="V", ptype="power_in", stack_with="1"),
        _pin(3, "top", name="V", ptype="power_in", stack_with="2")]})
    assert "chains are refused" in chain.message


def test_refuses_empty_pins():
    err = _refusal({"protocol_version": 1, "name": "X", "pins": []})
    assert err.code == "BAD_CONFIG"
    assert "non-empty array" in err.message


def test_refuses_missing_or_unknown_side():
    assert "is never guessed" in _refusal(
        {"protocol_version": 1, "name": "X",
         "pins": [{"number": "1", "name": "A", "type": "input"}]}).message
    assert "is never guessed" in _refusal(
        {"protocol_version": 1, "name": "X",
         "pins": [dict(_pin(1, "left"), side="north")]}).message


def test_refuses_wrong_protocol_version():
    err = _refusal({"protocol_version": 2, "name": "X",
                    "pins": [_pin(1, "left")]})
    assert err.code == "PROTOCOL_MISMATCH"


def test_refuses_unknown_keys():
    assert "unknown key(s) colour" in _refusal(
        {"protocol_version": 1, "name": "X", "colour": "red",
         "pins": [_pin(1, "left")]}).message
    assert "unknown key(s) rotation" in _refusal(
        {"protocol_version": 1, "name": "X",
         "pins": [dict(_pin(1, "left"), rotation=90)]}).message


def test_load_reports_invalid_json_as_bad_config(tmp_path):
    bad = tmp_path / "spec.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        ss.load(bad)
    assert ei.value.code == "BAD_CONFIG"


def test_fixture_specs_load_and_lay_out():
    for name in ("generic_opamp_8.spec.json", "generic_logic_14.spec.json"):
        spec = _load(name)
        assert spec.protocol_version == ss.PROTOCOL_VERSION
        assert ss.layout(spec).pins
