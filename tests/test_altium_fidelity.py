"""Altium reader FIDELITY: the silent-wrong-data class, not the can't-read class.

Every case here used to import *plausible but wrong* data — worse than a
refusal, because downstream checks then pass on the corruption:

* a component's SPICE / 3D / signal-integrity model name landing in
  ``Component.footprint`` (which then satisfies ``BOM_MISSING_FOOTPRINT`` and
  ships a nonexistent footprint into the BOM/PCB), and the non-current
  footprint winning over the ``IsCurrent=T`` one Altium actually pushes;
* Altium's indirect ``Comment="=Value"`` special string imported as the literal
  four characters, overwriting the real value;
* RECORD 47 (implementation pin association) counted as *unsupported*, diluting
  the skipped-record warning on every file that has a footprint at all;
* the document parameters (Title / Revision / DocumentNumber / Date) vanishing
  with no output field and no histogram entry;
* a RECORD-41 parameter owned by an unsupported record vanishing behind 41's
  "handled" status, so the histogram under-reported the real loss.

Fixtures are generated at runtime with the in-repo toolkit
(``tests/fixtures/_gen``) — no new binaries are committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "_gen"))
from altium_fixture import SchDocBuilder, write_schdoc  # noqa: E402

from akcli.checks import bom  # noqa: E402
from akcli.readers import altium_records, altium_sch  # noqa: E402

from tests._cli_harness import json_of, run_cli  # noqa: E402


def _write(tmp_path: Path, name: str, builder: SchDocBuilder) -> Path:
    p = tmp_path / name
    write_schdoc(str(p), builder, emit_records_txt=False)
    return p


def _one_part(**component_extra) -> tuple[SchDocBuilder, int]:
    """A one-component sheet (R1, one pin); returns the builder + component index."""
    b = SchDocBuilder()
    r1 = b.component("R1", 900, 1000, lib_ref="RES", **component_extra)
    b.pin(r1, "1", "A", 1000, 1000)
    return b, r1


def _read(tmp_path: Path, name: str, builder: SchDocBuilder):
    return altium_sch.read(_write(tmp_path, name, builder))


def _r1(sch):
    return next(c for c in sch.components if c.designator == "R1")


# --------------------------------------------------------------------------- #
# 1. ModelType / IsCurrent: only a PCBLIB model may become the footprint
# --------------------------------------------------------------------------- #
def test_sim_model_never_becomes_the_footprint(tmp_path):
    """A SIM model written FIRST must not win: footprint is the PCBLIB one."""
    b, r1 = _one_part()
    b.model(r1, "RESISTOR_SPICE", model_type="SIM")
    b.model(r1, "R_0603", model_type="PCBLIB")
    assert _r1(_read(tmp_path, "sim_first.SchDoc", b)).footprint == "R_0603"


def test_is_current_model_wins_over_record_order(tmp_path):
    """Two PCBLIB models: ``IsCurrent=T`` is the one Altium pushes to the PCB."""
    b, r1 = _one_part()
    b.model(r1, "R_0402", model_type="PCBLIB", is_current=False)
    b.model(r1, "R_0603", model_type="PCBLIB", is_current=True)
    assert _r1(_read(tmp_path, "is_current.SchDoc", b)).footprint == "R_0603"


def test_is_current_wins_across_model_types(tmp_path):
    """The SIM model may even carry IsCurrent=T — it is still not a footprint."""
    b, r1 = _one_part()
    b.model(r1, "RESISTOR_SPICE", model_type="SIM", is_current=True)
    b.model(r1, "R_0805", model_type="PCBLIB", is_current=False)
    assert _r1(_read(tmp_path, "sim_current.SchDoc", b)).footprint == "R_0805"


def test_first_pcblib_wins_when_none_is_current(tmp_path):
    b, r1 = _one_part()
    b.model(r1, "R_0402", model_type="PCBLIB")
    b.model(r1, "R_0603", model_type="PCBLIB")
    assert _r1(_read(tmp_path, "no_current.SchDoc", b)).footprint == "R_0402"


def test_absent_model_type_is_still_a_footprint(tmp_path):
    """Legacy/converter records omit ModelType — that link is the footprint."""
    b, r1 = _one_part()
    b.model(r1, "SOT-23", model_type=None)
    assert _r1(_read(tmp_path, "no_type.SchDoc", b)).footprint == "SOT-23"


def test_non_footprint_model_types_leave_the_footprint_empty(tmp_path):
    """SIM / SI / PCB3DLib only -> empty footprint, so the BOM check can fire."""
    b, r1 = _one_part()
    b.model(r1, "RESISTOR_SPICE", model_type="SIM")
    b.model(r1, "R0603_SI", model_type="SI")
    b.model(r1, "R0603.STEP", model_type="PCB3DLib")
    sch = _read(tmp_path, "no_footprint.SchDoc", b)
    assert _r1(sch).footprint is None
    codes = [f.code for f in bom.run(sch)]
    assert "BOM_MISSING_FOOTPRINT" in codes, (
        "a non-footprint model name masked the missing-footprint finding")


def test_unknown_model_type_fails_closed(tmp_path):
    """An unrecognized ModelType is excluded (allow-list), not guessed in."""
    b, r1 = _one_part()
    b.model(r1, "MYSTERY", model_type="SomeFutureModelKind")
    assert _r1(_read(tmp_path, "unknown_type.SchDoc", b)).footprint is None


def test_footprint_parameter_fallback_survives_a_sim_only_model(tmp_path):
    """With no PCBLIB link the RECORD-41 `Footprint` parameter still fills in."""
    b, r1 = _one_part()
    b.model(r1, "RESISTOR_SPICE", model_type="SIM")
    b.parameter(r1, "Footprint", "R_0603_1608Metric")
    assert _r1(_read(tmp_path, "param_fp.SchDoc", b)).footprint == "R_0603_1608Metric"


# --------------------------------------------------------------------------- #
# 2. Comment "=<ParamName>" is an indirection, not a literal
# --------------------------------------------------------------------------- #
def test_comment_parameter_reference_resolves(tmp_path):
    b, r1 = _one_part(Comment="=Value")
    b.parameter(r1, "Value", "10k")
    assert _r1(_read(tmp_path, "ref.SchDoc", b)).value == "10k"


def test_comment_reference_is_case_insensitive(tmp_path):
    b, r1 = _one_part(Comment="=VALUE")
    b.parameter(r1, "Value", "4.7uF")
    assert _r1(_read(tmp_path, "ref_case.SchDoc", b)).value == "4.7uF"


def test_comment_reference_resolves_a_non_value_parameter(tmp_path):
    b, r1 = _one_part(Comment="=Comment2")
    b.parameter(r1, "Comment2", "MMBT3904")
    b.parameter(r1, "Value", "not this one")
    assert _r1(_read(tmp_path, "ref_other.SchDoc", b)).value == "MMBT3904"


def test_plain_comment_is_untouched(tmp_path):
    b, _r = _one_part(Comment="10k 1%")
    assert _r1(_read(tmp_path, "plain.SchDoc", b)).value == "10k 1%"


def test_bare_equals_is_not_a_reference(tmp_path):
    b, _r = _one_part(Comment="=")
    sch = _read(tmp_path, "bare_eq.SchDoc", b)
    assert _r1(sch).value == "="
    assert sch.warnings == []


def test_unresolved_reference_keeps_the_literal_and_warns(tmp_path):
    b, r1 = _one_part(Comment="=Value")
    b.parameter(r1, "Tolerance", "1%")     # no `Value` parameter exists
    sch = _read(tmp_path, "unresolved.SchDoc", b)
    assert _r1(sch).value == "=Value", "the literal must be preserved, not blanked"
    assert sch.warnings == [
        "altium: 1 unresolved Comment parameter reference(s) "
        "(imported literally): R1 '=Value'"
    ]


def test_unresolved_reference_warning_collapses_and_reaches_the_cli(tmp_path):
    b = SchDocBuilder()
    for i in range(14):
        c = b.component(f"R{i}", 900 + i * 100, 1000, Comment="=Value")
        b.pin(c, "1", "A", 1000 + i * 100, 1000)
    p = _write(tmp_path, "many_refs.SchDoc", b)
    sch = altium_sch.read(p)
    (warn,) = sch.warnings
    assert warn.startswith("altium: 14 unresolved Comment parameter reference(s)")
    assert warn.endswith("+2 more")
    _rc, _out, err = run_cli(["read", str(p)])
    assert "warning: altium: 14 unresolved Comment parameter reference(s)" in err
    doc = json_of(["read", str(p), "--json"])
    assert any("unresolved Comment parameter" in w for w in doc["warnings"])


# --------------------------------------------------------------------------- #
# 3. RECORD 47 is part of the model link, not an unsupported record
# --------------------------------------------------------------------------- #
def test_record_47_has_a_name():
    assert altium_records.record_name(47) == "47 (ImplementationPinAssociation)"


def test_record_47_is_not_reported_as_unsupported(tmp_path):
    """Every real file with a footprint carries these; they must not warn."""
    b, r1 = _one_part()
    impl = b.add(44, owner=r1)
    b.add(45, owner=impl, ModelName="R_0603", ModelType="PCBLIB", IsCurrent="T")
    for n in ("1", "2"):
        b.add(47, owner=impl, **{"Designator": n, "DesignatorPart": n})
    sch = _read(tmp_path, "pin_assoc.SchDoc", b)
    assert sch.warnings == []
    assert _r1(sch).footprint == "R_0603"


# --------------------------------------------------------------------------- #
# 4. Document parameters -> Schematic.title_block
# --------------------------------------------------------------------------- #
_DOC_PARAMS = {
    "Title": "Power Entry",
    "Revision": "B2",
    "DocumentNumber": "SCH-0042",
    "Date": "2026-07-25",
}


def _titled(tmp_path: Path) -> Path:
    b, _r = _one_part()
    for k, v in _DOC_PARAMS.items():
        b.doc_parameter(k, v)
    return _write(tmp_path, "titled.SchDoc", b)


def test_document_parameters_reach_the_title_block(tmp_path):
    sch = altium_sch.read(_titled(tmp_path))
    assert sch.title_block == _DOC_PARAMS
    assert sch.warnings == [], "document parameters are consumed, not skipped"


def test_title_block_is_in_read_json(tmp_path):
    doc = json_of(["read", str(_titled(tmp_path)), "--json"], expect=0)
    assert doc["title_block"] == _DOC_PARAMS


def test_title_block_is_in_read_summary(tmp_path):
    """--summary is the context-budget view; the title block is cheap and identifying."""
    doc = json_of(["read", str(_titled(tmp_path)), "--json", "--summary"], expect=0)
    assert doc["title_block"] == _DOC_PARAMS


def test_read_summary_omits_an_empty_title_block(tmp_path):
    b, _r = _one_part()
    p = _write(tmp_path, "untitled_summary.SchDoc", b)
    assert "title_block" not in json_of(["read", str(p), "--json", "--summary"], expect=0)


def test_title_block_survives_a_bytes_read(tmp_path):
    data = _titled(tmp_path).read_bytes()
    assert altium_sch.read(data).title_block == _DOC_PARAMS


def test_document_parameters_are_not_component_parameters(tmp_path):
    sch = altium_sch.read(_titled(tmp_path))
    assert _r1(sch).parameters == {}


def test_title_block_defaults_empty(tmp_path):
    b, _r = _one_part()
    sch = _read(tmp_path, "untitled.SchDoc", b)
    assert sch.title_block == {}


def test_hierarchical_read_takes_the_root_title_block(tmp_path):
    child = SchDocBuilder()
    cr = child.component("R9", 900, 1000)
    child.pin(cr, "1", "A", 1000, 1000)
    child.doc_parameter("Title", "Child Sheet")
    _write(tmp_path, "child.SchDoc", child)

    parent, _r = _one_part()
    ss = parent.sheet_symbol(2000, 2000, 400, 300)
    parent.sheet_name(ss, "Child")
    parent.sheet_file(ss, "child.SchDoc")
    parent.doc_parameter("Title", "Root Sheet")
    sch = altium_sch.read(_write(tmp_path, "root.SchDoc", parent))
    assert sch.title_block == {"Title": "Root Sheet"}


# --------------------------------------------------------------------------- #
# 5. A parameter that lands nowhere is counted, not swallowed
# --------------------------------------------------------------------------- #
def test_parameter_under_an_unsupported_owner_is_counted(tmp_path):
    """RECORD 41 under a RECORD 43 parameter set: BOTH records are lost."""
    b, _r = _one_part()
    pset = b.add(43, **{"Location.X": 3000, "Location.Y": 1000})
    b.parameter(pset, "Rule", "NetClass=Power")
    b.parameter(pset, "Comment", "blanket directive")
    sch = _read(tmp_path, "param_set.SchDoc", b)
    assert sch.warnings == [
        "altium: skipped 3 unsupported record(s): 41 (Parameter) ×2, 43 (WarningSign) ×1"
    ]


def test_parameter_with_a_dangling_owner_is_counted(tmp_path):
    b, _r = _one_part()
    b.add(41, owner=9999, Name="Ghost", Text="x")
    sch = _read(tmp_path, "dangling_param.SchDoc", b)
    assert sch.warnings == [
        "altium: skipped 1 unsupported record(s): 41 (Parameter) ×1"
    ]


def test_component_and_document_parameters_never_count_as_skipped(tmp_path):
    b, r1 = _one_part()
    b.parameter(r1, "Value", "10k")
    b.doc_parameter("Title", "T")
    sch = _read(tmp_path, "kept_params.SchDoc", b)
    assert sch.warnings == []
    assert _r1(sch).parameters == {"Value": "10k"}
    assert sch.title_block == {"Title": "T"}
