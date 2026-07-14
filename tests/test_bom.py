"""Tests for the BOM hygiene check (SPEC §3.6)."""

from __future__ import annotations

import os

import pytest

from akcli.checks import bom
from akcli.model import Component, Schematic
from akcli.readers import altium_sch
from akcli.report import Finding, Severity

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _comp(
    designator: str,
    *,
    value: str | None = "10k",
    footprint: str | None = "0402",
    unique_id: str | None = None,
    undesignated: bool = False,
) -> Component:
    return Component(
        designator=designator,
        library_ref="Device:R",
        x_mil=0.0,
        y_mil=0.0,
        value=value,
        footprint=footprint,
        unique_id=unique_id,
        undesignated=undesignated,
    )


def _sch(components: list[Component]) -> Schematic:
    return Schematic(
        source_path="<test>",
        source_format="altium",
        components=components,
        nets=[],
    )


def _codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]


def _by_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


# ---------------------------------------------------------------------------
# return contract
# ---------------------------------------------------------------------------
def test_returns_list_of_findings():
    out = bom.run(_sch([_comp("R1")]))
    assert isinstance(out, list)
    assert all(isinstance(f, Finding) for f in out)


def test_clean_bom_has_no_findings():
    sch = _sch([_comp("R1"), _comp("R2"), _comp("C1")])
    assert bom.run(sch) == []


# ---------------------------------------------------------------------------
# refdes gap detection
# ---------------------------------------------------------------------------
def test_gap_within_prefix_reported():
    sch = _sch([_comp("R7"), _comp("R12")])
    gaps = _by_code(bom.run(sch), "BOM_REFDES_GAP")
    assert len(gaps) == 1
    assert gaps[0].refs == ["R8", "R9", "R10", "R11"]
    assert gaps[0].severity is Severity.NOTE


def test_no_gap_when_contiguous():
    sch = _sch([_comp("U1"), _comp("U2"), _comp("U3")])
    assert _by_code(bom.run(sch), "BOM_REFDES_GAP") == []


def test_lone_member_is_not_a_gap():
    # X3 alone (no X1/X2): min==max==3 -> empty range -> no gap (the SPEC's X3 case).
    sch = _sch([_comp("X3")])
    assert _by_code(bom.run(sch), "BOM_REFDES_GAP") == []


def test_gap_does_not_extend_below_min():
    # R3, R5 present -> only R4 missing; R1/R2 are NOT reported.
    sch = _sch([_comp("R3"), _comp("R5")])
    gaps = _by_code(bom.run(sch), "BOM_REFDES_GAP")
    assert len(gaps) == 1
    assert gaps[0].refs == ["R4"]


def test_gaps_are_per_prefix():
    sch = _sch([_comp("R1"), _comp("R3"), _comp("C1"), _comp("C4")])
    gaps = _by_code(bom.run(sch), "BOM_REFDES_GAP")
    refs = {tuple(g.refs) for g in gaps}
    assert refs == {("R2",), ("C2", "C3")}


def test_compound_refs_skipped_from_gaps():
    # J_USB_C does not parse to (prefix,int) and must never trigger a gap.
    sch = _sch([_comp("J_USB_C"), _comp("J1"), _comp("J3")])
    gaps = _by_code(bom.run(sch), "BOM_REFDES_GAP")
    assert len(gaps) == 1
    assert gaps[0].refs == ["J2"]  # J_USB_C ignored, J1..J3 -> J2 missing


# ---------------------------------------------------------------------------
# duplicate designators / dedup by UniqueId (multi-unit parts)
# ---------------------------------------------------------------------------
def test_duplicate_designator_distinct_components_flagged():
    sch = _sch([_comp("R1", unique_id=None), _comp("R1", unique_id=None)])
    dups = _by_code(bom.run(sch), "BOM_DUPLICATE_DESIGNATOR")
    assert len(dups) == 1
    assert dups[0].refs == ["R1"]
    assert dups[0].severity is Severity.ERROR


def test_multiunit_shared_unique_id_is_deduped_not_flagged():
    # Two placements (units) of ONE physical part sharing a UniqueId -> no error.
    sch = _sch(
        [
            _comp("U1", unique_id="ABC-123"),
            _comp("U1", unique_id="ABC-123"),
        ]
    )
    assert _by_code(bom.run(sch), "BOM_DUPLICATE_DESIGNATOR") == []


def test_same_designator_different_unique_ids_flagged():
    sch = _sch(
        [
            _comp("U1", unique_id="ABC-123"),
            _comp("U1", unique_id="XYZ-999"),
        ]
    )
    dups = _by_code(bom.run(sch), "BOM_DUPLICATE_DESIGNATOR")
    assert len(dups) == 1


def test_multiunit_dedup_value_footprint_counted_once():
    # Value/footprint live on only one unit; the part must not be flagged missing.
    sch = _sch(
        [
            _comp("U1", unique_id="ABC", value="MCU", footprint="QFN48"),
            _comp("U1", unique_id="ABC", value=None, footprint=None),
        ]
    )
    out = bom.run(sch)
    assert _by_code(out, "BOM_MISSING_VALUE") == []
    assert _by_code(out, "BOM_MISSING_FOOTPRINT") == []
    assert _by_code(out, "BOM_DUPLICATE_DESIGNATOR") == []


# ---------------------------------------------------------------------------
# missing value / footprint
# ---------------------------------------------------------------------------
def test_missing_value_reported():
    out = bom.run(_sch([_comp("R1", value=None)]))
    mv = _by_code(out, "BOM_MISSING_VALUE")
    assert len(mv) == 1
    assert mv[0].refs == ["R1"]
    assert mv[0].severity is Severity.WARNING


def test_missing_footprint_reported():
    out = bom.run(_sch([_comp("R1", footprint=None)]))
    mf = _by_code(out, "BOM_MISSING_FOOTPRINT")
    assert len(mf) == 1
    assert mf[0].refs == ["R1"]


def test_blank_value_treated_as_missing():
    out = bom.run(_sch([_comp("R1", value="   ")]))
    assert len(_by_code(out, "BOM_MISSING_VALUE")) == 1


# ---------------------------------------------------------------------------
# undesignated / synthesized components are excluded entirely
# ---------------------------------------------------------------------------
def test_undesignated_components_excluded():
    sch = _sch(
        [
            _comp("$U7", value=None, footprint=None, undesignated=True),
            _comp("$U8", value=None, footprint=None, undesignated=True),
        ]
    )
    assert bom.run(sch) == []


# ---------------------------------------------------------------------------
# real-fixture smoke test (parses an actual .SchDoc via the frozen reader)
# ---------------------------------------------------------------------------
def test_real_fixture_smoke():
    sch = altium_sch.read(os.path.join(FIXTURES, "shared_name_label.SchDoc"))
    out = bom.run(sch)
    assert all(isinstance(f, Finding) for f in out)
    codes = _codes(out)
    # fixture components: U2, R7, U3, R12 -- all lack value+footprint, R8..R11 gap.
    gaps = _by_code(out, "BOM_REFDES_GAP")
    assert any(g.refs == ["R8", "R9", "R10", "R11"] for g in gaps)
    assert "BOM_MISSING_VALUE" in codes
    assert "BOM_MISSING_FOOTPRINT" in codes
    # U2/U3 are contiguous -> no U-prefix gap.
    assert not any(g.refs and g.refs[0].startswith("U") for g in gaps)


@pytest.mark.parametrize(
    "fixture",
    [
        "junction_cross.SchDoc",
        "no_erc.SchDoc",
        "t_junction.SchDoc",
        "two_gnd_ports.SchDoc",
    ],
)
def test_all_fixtures_run_without_error(fixture):
    sch = altium_sch.read(os.path.join(FIXTURES, fixture))
    out = bom.run(sch)
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# part identity substitutes for a blank value (vendor-library Altium exports)
# ---------------------------------------------------------------------------
def test_blank_value_with_part_number_libref_not_flagged():
    c = _comp("Q1", value=None)
    c.library_ref = "AO2301"                      # digit-bearing part number
    out = bom.run(_sch([c]))
    assert "BOM_MISSING_VALUE" not in _codes(out)


def test_blank_value_with_manufacturer_part_param_not_flagged():
    c = _comp("U2", value=None)
    c.library_ref = None
    c.parameters = {"Manufacturer Part": "MCP73831T-2ACI/OT"}
    out = bom.run(_sch([c]))
    assert "BOM_MISSING_VALUE" not in _codes(out)


def test_blank_value_with_generic_libref_still_flagged():
    c = _comp("R1", value=None)
    c.library_ref = "Device:R"                    # generic symbol: no digits
    c.parameters = {}
    out = bom.run(_sch([c]))
    assert "BOM_MISSING_VALUE" in _codes(out)


# ---------------------------------------------------------------------------
# BOM_CORRUPT_TEXT: U+FFFD baked into the file is surfaced, not silent
# ---------------------------------------------------------------------------
def test_fffd_in_value_reports_corrupt_text():
    c = _comp("R5", value="10k��")
    out = bom.run(_sch([c]))
    corrupt = _by_code(out, "BOM_CORRUPT_TEXT")
    assert len(corrupt) == 1
    assert corrupt[0].severity is Severity.NOTE
    assert "R5" in corrupt[0].refs


def test_fffd_in_parameter_reports_corrupt_text():
    c = _comp("Q1", value="AO2301")
    c.parameters = {"Manufacturer": "TECH PUBLIC(��)"}
    out = bom.run(_sch([c]))
    assert any(f.code == "BOM_CORRUPT_TEXT" and "Q1" in f.refs for f in out)


def test_clean_text_reports_no_corrupt_text():
    out = bom.run(_sch([_comp("R1", value="10kΩ")]))
    assert "BOM_CORRUPT_TEXT" not in _codes(out)


# ---------------------------------------------------------------------------
# virtual (#-prefixed) parts never reach the BOM


def test_power_symbols_are_not_bom_items():
    # power ports / PWR_FLAG have no value/footprint by design and their
    # numbering is not a refdes sequence — none of that is a finding
    sch = _sch([
        _comp("#PWR01", value=None, footprint=None),
        _comp("#PWR07", value=None, footprint=None),
        _comp("#FLG01", value=None, footprint=None),
        _comp("R1"),
    ])
    assert bom.run(sch) == []
