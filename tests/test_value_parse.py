"""parts.value_parse — normalized R/C/L value parsing (BOM P0-2).

The regression suite doubles as the incident record: every historical
wrong-part suggestion (5.1k -> 1k, 2.2µF -> 22µF, TP -> SMPS IC) traces to
substring value matching, which these semantics replace.
"""

from __future__ import annotations

import pytest

from akcli.parts import value_parse as vp


# --------------------------------------------------------------------------- #
# parse: spellings of one value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,hint,value,klass", [
    ("5.1k", "R", 5100.0, "R"),
    ("5k1", "", 5100.0, "R"),          # infix multiplier
    ("5.1kΩ", "", 5100.0, "R"),
    ("5.1 kohm", "", 5100.0, "R"),
    ("5100", "R", 5100.0, "R"),        # bare number needs the R hint
    ("4R7", "", 4.7, "R"),             # IEC ohms decimal mark
    ("0R", "", 0.0, "R"),
    ("1M", "R", 1e6, "R"),
    ("2.2u", "C", 2.2e-6, "C"),
    ("2u2", "C", 2.2e-6, "C"),
    ("2.2µF", "", 2.2e-6, "C"),
    ("2200nF", "", 2.2e-6, "C"),
    ("0.1uF", "", 1e-7, "C"),
    ("100n", "C", 1e-7, "C"),
    ("8p", "C", 8e-12, "C"),
    ("8pF C0G", "", 8e-12, "C"),
    ("10uH", "", 1e-5, "L"),
    ("4n7H", "", 4.7e-9, "L"),
])
def test_parse_spellings(text, hint, value, klass):
    got = vp.parse(text, hint=hint)
    assert got is not None, text
    assert got.value == pytest.approx(value, rel=1e-6)
    assert got.unit_class == klass


def test_cross_unit_spellings_compare_equal():
    a = vp.parse("2.2µF")
    b = vp.parse("2200nF")
    assert a and b and vp.same_value(a.value, b.value)


@pytest.mark.parametrize("text,hint", [
    ("", ""),
    (None, "C"),
    ("DNP", "R"),
    ("NE555P", "U"),
    ("2200", "C"),        # bare number on a cap is ambiguous -> refuse
    ("2200", ""),
])
def test_unparseable_returns_none(text, hint):
    assert vp.parse(text, hint=hint) is None


def test_qualifiers_extracted():
    got = vp.parse("8pF C0G 1% 50V", hint="C")
    assert got is not None
    assert got.dielectric == "C0G"
    assert got.tolerance == "1%"
    assert got.voltage == "50V"
    # NP0 canonicalizes to C0G (same dielectric)
    got = vp.parse("8p NP0", hint="C")
    assert got is not None and got.dielectric == "C0G"


# --------------------------------------------------------------------------- #
# extract_values: catalog descriptions
# --------------------------------------------------------------------------- #
def test_extract_from_catalog_description():
    desc = "22uF ±20% 25V X5R 0805 Multilayer Ceramic Capacitors MLCC"
    vals = vp.extract_values(desc, unit_class="C")
    assert any(vp.same_value(v.value, 22e-6) for v in vals)
    # the "25" of 25V and the "0805" package NEVER become candidate values
    assert not any(vp.same_value(v.value, 25.0) for v in vals)
    assert not any(vp.same_value(v.value, 805.0) for v in vals)
    assert vals[0].dielectric == "X5R"


def test_extract_requires_explicit_unit_in_free_text():
    assert vp.extract_values("RC0402FR-071KL 1kΩ resistor", unit_class="R")
    assert not vp.extract_values("plain 1000 text", unit_class="R")


# --------------------------------------------------------------------------- #
# the historical incidents, as physics
# --------------------------------------------------------------------------- #
def test_incident_5k1_never_matches_1k():
    line = vp.parse("5.1k", hint="R")
    cand = vp.extract_values("1kΩ ±1% 0402 Chip Resistor", unit_class="R")
    assert line and cand
    assert not any(vp.same_value(line.value, c.value) for c in cand)


def test_incident_2u2_never_matches_22u():
    line = vp.parse("2.2u", hint="C")
    cand = vp.extract_values("22uF ±20% 25V X5R 0805 MLCC", unit_class="C")
    assert line and cand
    assert not any(vp.same_value(line.value, c.value) for c in cand)


def test_refdes_unit_class():
    assert vp.unit_class_of_ref("R21") == "R"
    assert vp.unit_class_of_ref("C16") == "C"
    assert vp.unit_class_of_ref("L2") == "L"
    assert vp.unit_class_of_ref("FB1") == "L"
    assert vp.unit_class_of_ref("TP4") == ""
    assert vp.unit_class_of_ref("U1") == ""
    assert vp.unit_class_of_ref(None) == ""
