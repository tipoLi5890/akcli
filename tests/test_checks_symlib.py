"""Symbol-library lint (`akcli.checks.symlib`) — the incident detector.

The rule that matters is `SYMLIB_PIN_COINCIDENT`: an NC pin authored on top of a
GND pin makes KiCad treat them as one node in every placement of the part, and
nothing downstream reports it (ERC believes the symbol). The other two rules
are hygiene: pins with an unset electrical type (ERC pin-conflict blindness) and
pins off the 100-mil grid (a wire can never land on them).

The hard part is NOT flagging the legitimate same-coordinate cases: KiCad's
stacked-pin convention (same name + same type) and multi-unit symbols, whose
units are drawn on one body and therefore share coordinates by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.checks import symlib
from akcli.readers import kicad_lib
from akcli.report import Severity
from akcli.writers import symbol_lib

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symlib"


def _codes(findings) -> list[str]:
    return [f.code for f in findings]


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


# --------------------------------------------------------------------------- #
# SYMLIB_PIN_COINCIDENT
# --------------------------------------------------------------------------- #
def test_coincident_pins_are_an_error():
    findings = symlib.run(FIX / "coincident.kicad_sym")
    hits = _by_code(findings, symlib.SYMLIB_PIN_COINCIDENT)
    assert len(hits) == 1
    (hit,) = hits
    assert hit.severity is Severity.ERROR
    assert set(hit.refs) == {"GENERIC_AMP_8.5", "GENERIC_AMP_8.6"}
    assert "GND/power_in" in hit.message and "NC/no_connect" in hit.message
    assert {a["kind"] for a in hit.anchors} == {"pin"}


def test_stacked_convention_is_clean():
    """Same name AND same type on one point is KiCad's sanctioned stack."""
    assert _codes(symlib.run(FIX / "stacked_ok.kicad_sym")) == []


def test_multi_unit_pins_sharing_coordinates_are_not_flagged():
    """Four gates drawn on one body share every coordinate — by design."""
    findings = symlib.run(FIX / "multiunit_gates.kicad_sym")
    assert _codes(findings) == []

    # ...and the fixture really does have cross-unit coincidences to ignore.
    sym = kicad_lib.read(FIX / "multiunit_gates.kicad_sym").symbols[0]
    assert sym.part_count == 4
    seats = [(p.x_mil, p.y_mil) for p in sym.pins]
    assert len(set(seats)) < len(seats)


def test_a_real_collision_inside_one_unit_still_fires_on_a_multi_unit_part():
    """The unit filter must not become a blanket amnesty."""
    text = (FIX / "multiunit_gates.kicad_sym").read_text(encoding="utf-8")
    # move unit 1's output (pin 3) onto unit 1's input A (pin 1)
    broken = text.replace(
        '(at 10.16 0 180)\n\t\t\t\t(length 2.54)\n\t\t\t\t(name "Y"'
        '\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)'
        '\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t\t(number "3"',
        '(at -10.16 2.54 0)\n\t\t\t\t(length 2.54)\n\t\t\t\t(name "Y"'
        '\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)'
        '\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t\t(number "3"', 1)
    assert broken != text, "fixture shape drifted; the mutation no longer applies"
    hits = _by_code(symlib.run_text(broken), symlib.SYMLIB_PIN_COINCIDENT)
    assert len(hits) == 1
    assert set(hits[0].refs) == {"GENERIC_QUAD_GATE_14.1", "GENERIC_QUAD_GATE_14.3"}


def test_two_common_pins_colliding_are_reported_once_not_once_per_unit():
    """`_0_*` pins are visible on every unit; one defect, not four findings."""
    text = (FIX / "multiunit_gates.kicad_sym").read_text(encoding="utf-8")
    broken = text.replace('(at 0 -7.62 90)', '(at 0 7.62 270)', 1)   # GND onto VCC
    assert broken != text
    hits = _by_code(symlib.run_text(broken), symlib.SYMLIB_PIN_COINCIDENT)
    assert len(hits) == 1, [h.refs for h in hits]
    assert set(hits[0].refs) == {"GENERIC_QUAD_GATE_14.7", "GENERIC_QUAD_GATE_14.14"}


def test_a_common_pin_landing_on_each_units_own_pin_reports_every_unit():
    """Four units, four genuinely different colliding PAIRS — all of them real."""
    text = (FIX / "multiunit_gates.kicad_sym").read_text(encoding="utf-8")
    broken = text.replace('(at 0 -7.62 90)', '(at -10.16 2.54 0)', 1)  # GND onto "A"
    assert broken != text
    hits = _by_code(symlib.run_text(broken), symlib.SYMLIB_PIN_COINCIDENT)
    assert {tuple(sorted(h.refs)) for h in hits} == {
        ("GENERIC_QUAD_GATE_14.1", "GENERIC_QUAD_GATE_14.7"),
        ("GENERIC_QUAD_GATE_14.12", "GENERIC_QUAD_GATE_14.7"),
        ("GENERIC_QUAD_GATE_14.4", "GENERIC_QUAD_GATE_14.7"),
        ("GENERIC_QUAD_GATE_14.7", "GENERIC_QUAD_GATE_14.9"),
    }
    assert all("on unit" in h.message for h in hits)


def test_three_pins_on_one_point_report_all_of_them():
    text = (FIX / "coincident.kicad_sym").read_text(encoding="utf-8")
    broken = text.replace('(at -10.16 -2.54 0)', '(at 0 -7.62 90)', 1)
    (hit,) = _by_code(symlib.run_text(broken), symlib.SYMLIB_PIN_COINCIDENT)
    assert set(hit.refs) == {"GENERIC_AMP_8.5", "GENERIC_AMP_8.6", "GENERIC_AMP_8.7"}


# --------------------------------------------------------------------------- #
# SYMLIB_PIN_TYPE_UNSPECIFIED / SYMLIB_PIN_OFF_GRID
# --------------------------------------------------------------------------- #
def test_unspecified_types_are_flagged_including_the_free_token():
    findings = symlib.run(FIX / "unspecified_type.kicad_sym")
    (hit,) = _by_code(findings, symlib.SYMLIB_PIN_TYPE_UNSPECIFIED)
    assert hit.severity is Severity.NOTE
    # pins 1/2 are `unspecified`, pin 3 is `free` — KiCad's other unset token
    assert set(hit.refs) == {"GENERIC_LOGIC_14_UNTYPED.1",
                             "GENERIC_LOGIC_14_UNTYPED.2",
                             "GENERIC_LOGIC_14_UNTYPED.3"}
    assert "pin conflicts" in hit.message


def test_off_grid_pin_is_a_warning():
    findings = symlib.run(FIX / "unspecified_type.kicad_sym")
    (hit,) = _by_code(findings, symlib.SYMLIB_PIN_OFF_GRID)
    assert hit.severity is Severity.WARNING
    assert hit.refs == ["GENERIC_LOGIC_14_UNTYPED.4"]
    assert "(-400,50)" in hit.message


def test_a_fully_typed_on_grid_symbol_is_clean():
    assert _codes(symlib.run(FIX / "stacked_ok.kicad_sym")) == []


# --------------------------------------------------------------------------- #
# the generator's own output is clean by construction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_file", ["generic_opamp_8.spec.json",
                                       "generic_logic_14.spec.json"])
def test_generated_symbols_pass_their_own_lint(spec_file):
    text = symbol_lib.to_kicad_sym(ss.layout(ss.load(FIX / spec_file)))
    assert symlib.run_text(text, spec_file) == []


def test_generated_symbol_with_stacked_pins_passes_the_lint():
    spec = ss.parse_spec({"protocol_version": 1, "name": "GENERIC_STACK", "pins": [
        {"number": "1", "name": "IN", "type": "input", "side": "left"},
        {"number": "2", "name": "VCC", "type": "power_in", "side": "top"},
        {"number": "3", "name": "VCC", "type": "power_in", "side": "top",
         "stack_with": "2", "hidden": True}]})
    text = symbol_lib.to_kicad_sym(ss.layout(spec))
    assert symlib.run_text(text) == []


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def test_non_kicad_sym_input_is_skipped_not_crashed(tmp_path):
    other = tmp_path / "board.kicad_sch"
    other.write_text("(kicad_sch)", encoding="utf-8")
    (only,) = symlib.run(other)
    assert only.severity is Severity.INFO
    assert "supports .kicad_sym only" in only.message


def test_extends_stubs_are_not_double_reported():
    """A derived symbol inherits its base's pins; linting both doubles findings."""
    device = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symbols" \
        / "Device.kicad_sym"
    lib = kicad_lib.read(device)
    assert any(s.extends and not s.pins for s in lib.symbols), \
        "Device.kicad_sym lost its (extends ...) symbol"
    findings = symlib.run(device)
    assert all("C_Polarized" not in ref for f in findings for ref in f.refs)


def test_findings_are_stable_across_runs():
    first = symlib.run(FIX / "unspecified_type.kicad_sym")
    second = symlib.run(FIX / "unspecified_type.kicad_sym")
    assert [(f.code, tuple(f.refs), f.message) for f in first] == \
           [(f.code, tuple(f.refs), f.message) for f in second]
