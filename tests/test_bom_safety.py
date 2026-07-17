"""BOM P0 safety gates — reverse verification, numeric confidence, no-part refs.

Acceptance tests for the three highest-risk cells in the sourcing flow, each
tied to a recorded real-world incident:

* a filled-in C-number was trusted unconditionally (wrong id = silently
  ordering the wrong part) -> ``verify_line`` / ``BOM_LCSC_MISMATCH``;
* substring value matching graded 1k high for a 5.1k line and 22µF high for
  a 2.2µF line -> normalized numeric confidence;
* test points received part suggestions (TP -> SMPS IC) -> structural refs
  never enter the suggestion flow.
"""

from __future__ import annotations

from akcli import model
from akcli.parts import bom_jlc
from akcli.parts.search import Part


def _comp(ref, value="10k", footprint="R_0402_1005Metric", params=None):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint=footprint, unique_id=ref,
        parameters=params or {})


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


def _part(lcsc="C1", mpn="X", description="", package="0402", stock=1000,
          basic=True):
    return Part(lcsc=lcsc, mpn=mpn, description=description, package=package,
                stock=stock, price=0.01, basic=basic, datasheet=None,
                category="", attributes={})


# --------------------------------------------------------------------------- #
# P0-1 reverse verification (C-number <-> Value/Footprint)
# --------------------------------------------------------------------------- #
def test_wrong_cnumber_value_is_mismatch():
    """The spec's acceptance case: 8pF cap wearing a 100nF part's C-number."""
    sch = _sch([_comp("C16", value="8pF", footprint="C_0402_1005Metric",
                      params={"LCSC": "C1525"})])
    catalog = _part("C1525", mpn="CL05B104KO5NNNC",
                    description="100nF ±10% 16V X7R 0402 MLCC",
                    package="0402")
    lines = bom_jlc.check(sch, get=lambda c: catalog, find=lambda q, **k: [])
    (line,) = lines
    assert line.verified == "mismatch"
    assert any("value mismatch" in m for m in line.mismatch)
    assert "BOM_LCSC_MISMATCH" in line.note
    assert bom_jlc.totals(lines)["mismatches"] == 1
    assert bom_jlc.totals(lines)["problems"] == 1


def test_correct_cnumber_verifies_ok_no_false_positive():
    sch = _sch([
        _comp("R21", value="5.1k", params={"LCSC": "C25905"}),
        _comp("C3", value="100nF", footprint="C_0402_1005Metric",
              params={"LCSC": "C1525"}),
        _comp("C4", value="2.2µF", footprint="C_0402_1005Metric",
              params={"LCSC": "C23630"}),
    ])
    catalog = {
        "C25905": _part("C25905", description="5.1kΩ ±1% 0402 Chip Resistor"),
        "C1525": _part("C1525", description="100nF ±10% 16V X7R 0402 MLCC"),
        # cross-unit spelling: catalog says 2200nF, schematic says 2.2µF
        "C23630": _part("C23630", description="2200nF ±10% 25V X5R 0402 MLCC"),
    }
    lines = bom_jlc.check(sch, get=lambda c: catalog[c], find=lambda q, **k: [])
    assert all(ln.verified == "ok" for ln in lines), \
        [(ln.refs, ln.mismatch) for ln in lines]
    assert bom_jlc.totals(lines)["mismatches"] == 0


def test_package_mismatch_flagged_value_agnostic():
    sch = _sch([_comp("R1", value="10k", footprint="R_0402_1005Metric",
                      params={"LCSC": "C25804"})])
    catalog = _part("C25804", description="10kΩ ±1% 0603 Chip Resistor",
                    package="0603")
    lines = bom_jlc.check(sch, get=lambda c: catalog, find=lambda q, **k: [])
    assert lines[0].verified == "mismatch"
    assert any("package mismatch" in m for m in lines[0].mismatch)


def test_nothing_comparable_is_unverified_not_pass():
    """No package on either side + unparseable value: honest 'unverified'."""
    sch = _sch([_comp("U1", value="RF-MOD-01", footprint="",
                      params={"LCSC": "C123456"})])
    catalog = _part("C123456", description="", package="")
    lines = bom_jlc.check(sch, get=lambda c: catalog, find=lambda q, **k: [])
    assert lines[0].verified == "unverified"
    assert "unverified" in lines[0].note
    assert bom_jlc.totals(lines)["mismatches"] == 0


# --------------------------------------------------------------------------- #
# P0-2 numeric confidence (the incident regressions)
# --------------------------------------------------------------------------- #
def _suggest_one(comp, candidates):
    sch = _sch([comp])
    lines = bom_jlc.check(sch, get=lambda c: None,
                          find=lambda q, **k: candidates)
    bom_jlc.suggest_parts(lines, find=lambda q, **k: candidates)
    return lines[0]


def test_incident_1k_candidate_never_high_for_5k1_line():
    line = _suggest_one(
        _comp("R21", value="5.1k"),
        [_part("C11702", mpn="RC0402FR-071KL",
               description="1kΩ ±1% 1/16W 0402 Chip Resistor")])
    assert line.suggestion is not None
    assert line.suggestion_confidence == "low"


def test_incident_22u_candidate_never_high_for_2u2_line():
    line = _suggest_one(
        _comp("C9", value="2.2µF", footprint="C_0805_2012Metric"),
        [_part("C45783", mpn="GRM21BR61E226ME44L", package="0805",
               description="22uF ±20% 25V X5R 0805 MLCC")])
    assert line.suggestion is not None
    assert line.suggestion_confidence == "low"


def test_exact_value_and_package_is_high():
    line = _suggest_one(
        _comp("C9", value="2.2µF", footprint="C_0805_2012Metric"),
        [_part("C49217", mpn="CL21B225KAFNNNE", package="0805",
               description="2.2uF ±10% 25V X7R 0805 MLCC")])
    assert line.suggestion_confidence == "high"


def test_dielectric_conflict_downgrades():
    line = _suggest_one(
        _comp("C2", value="8pF C0G", footprint="C_0402_1005Metric"),
        [_part("C99999", package="0402",
               description="8pF ±5% 50V X7R 0402 MLCC")])
    assert line.suggestion_confidence == "low"


def test_unparseable_value_never_high():
    line = _suggest_one(
        _comp("U7", value="whatever-special", footprint="R_0402_1005Metric"),
        [_part("C1", package="0402", description="whatever-special thing")])
    assert line.suggestion is None or line.suggestion_confidence == "low"


# --------------------------------------------------------------------------- #
# P0-3 structural refs never get suggestions
# --------------------------------------------------------------------------- #
def test_testpoints_never_receive_suggestions():
    """The TP -> SMPS-IC incident, closed: structural refs skip suggest."""
    smps = _part("C666", mpn="MP2359", package="",
                 description="SMPS Step-Down DC-DC 1.2MHz SOT-23-6")
    sch = _sch([
        _comp("TP1", value="TestPoint", footprint="TestPoint_Pad_D1.0mm"),
        _comp("TP2", value="TestPoint", footprint="TestPoint_Pad_D1.0mm"),
        _comp("FID1", value="Fiducial", footprint="Fiducial_0.5mm"),
        _comp("MH1", value="MountingHole", footprint="MountingHole_M2"),
        _comp("H3", value="MountingHole", footprint="MountingHole_M2"),
    ])
    lines = bom_jlc.check(sch, get=lambda c: None, find=lambda q, **k: [smps])
    n = bom_jlc.suggest_parts(lines, find=lambda q, **k: [smps])
    assert n == 0
    assert all(ln.suggestion is None for ln in lines)
    assert all(ln.note == "structural, no part expected" for ln in lines)
    # and therefore --fix has nothing to write for them
    assert bom_jlc.fix_ops(lines, min_confidence="low") == []


def test_mixed_line_with_real_ref_still_suggests():
    """A line whose refs are NOT all structural stays in the suggest flow."""
    cand = _part("C25905", package="0402",
                 description="5.1kΩ ±1% 0402 Chip Resistor")
    sch = _sch([_comp("R21", value="5.1k")])
    lines = bom_jlc.check(sch, get=lambda c: None, find=lambda q, **k: [cand])
    assert bom_jlc.suggest_parts(lines, find=lambda q, **k: [cand]) == 1
    assert lines[0].suggestion_confidence == "high"
