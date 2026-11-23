"""PCB <-> library leg of ``verify`` + the assembly-flag / board-only rules.

The board and the footprint library under test are REAL KiCad output
(``tests/fixtures/kicad/fp_attrs``: ``kicad-cli 10.0.4 pcb upgrade`` /
``fp upgrade``), so the negative case — "an unedited board matches its
library" — is proven on what KiCad actually writes. Each finding is then
provoked by one targeted edit of a copy.

Board (6 footprints): R1/R2 ``proj:R_0402``, H1 ``proj:MountingHole_3.2mm``
(board_only), J1 ``proj:PinHeader_1x02``, TP1 ``proj:TestPoint`` — all from the
project ``fp-lib-table`` — and R3 ``Resistor_SMD:R_0402_1005Metric``, which
only the (conftest-controlled) GLOBAL table registers.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from akcli import libtable
from akcli.checks import fplib, schpcb
from akcli.cli import main
from akcli.readers import kicad
from tests._fp_attrs_project import BOARD, make_project
from tests._fp_attrs_project import edit as _edit
from tests._fp_attrs_project import waive as _waive
from tests._fp_attrs_project import write as _write


@pytest.fixture()
def proj(tmp_path) -> Path:
    return make_project(tmp_path)


def _board(proj: Path) -> Path:
    return proj / BOARD


def _lib_run(proj: Path):
    return fplib.run(kicad.read_pcb(str(_board(proj))), libtable.discover(proj))


def _schpcb(proj: Path):
    return schpcb.run(kicad.read_sch(str(proj / "board.kicad_sch")),
                      kicad.read_pcb(str(_board(proj))))


def _by_code(findings) -> dict:
    out: dict = {}
    for f in findings:
        out.setdefault(f.code, []).append(f)
    return out


# --------------------------------------------------------------------------- #
# schpcb: board-only demotion + assembly flags (no --library needed)
# --------------------------------------------------------------------------- #
def test_real_kicad_pair_is_clean_except_the_board_only_note(proj):
    findings = _schpcb(proj)
    assert [(f.code, f.severity.value, f.refs) for f in findings] == [
        ("SCHPCB_EXTRA_ON_PCB", "note", ["H1"])]
    assert "board-only" in findings[0].message          # ... and says why


def test_extra_footprint_without_the_marker_stays_a_warning(proj):
    # same footprint, attribute removed -> an unexplained extra part again
    _edit(_board(proj), "(attr board_only exclude_from_pos_files "
                        "exclude_from_bom allow_missing_courtyard",
          "(attr exclude_from_pos_files exclude_from_bom "
          "allow_missing_courtyard")
    (extra,) = _by_code(_schpcb(proj))["SCHPCB_EXTRA_ON_PCB"]
    assert extra.severity.value == "warning" and extra.refs == ["H1"]
    assert "Not in schematic" in extra.message


def test_dnp_mismatch_both_directions(proj):
    # schematic DNP, board fitted
    _edit(_board(proj), "(attr smd exclude_from_pos_files exclude_from_bom dnp)",
          "(attr smd exclude_from_pos_files exclude_from_bom)")
    (hit,) = _by_code(_schpcb(proj))["SCHPCB_DNP_MISMATCH"]
    assert hit.severity.value == "warning" and hit.refs == ["R2"]
    assert "DNP on the schematic, fitted on the board" in hit.message
    # schematic fitted, board DNP
    _edit(_board(proj), "(attr through_hole)", "(attr through_hole dnp)")
    hits = _by_code(_schpcb(proj))["SCHPCB_DNP_MISMATCH"]
    assert sorted(h.refs[0] for h in hits) == ["J1", "R2"]
    j1 = next(h for h in hits if h.refs == ["J1"])
    assert "fitted on the schematic, DNP on the board" in j1.message


def test_bom_flag_mismatch_both_directions(proj):
    # schematic in_bom no, board no longer excludes it
    _edit(_board(proj), "(attr smd exclude_from_pos_files exclude_from_bom dnp)",
          "(attr smd exclude_from_pos_files dnp)")
    (hit,) = _by_code(_schpcb(proj))["SCHPCB_BOM_FLAG_MISMATCH"]
    assert hit.severity.value == "warning" and hit.refs == ["R2"]
    # schematic in_bom yes, board excludes it
    _edit(_board(proj), "(attr through_hole)",
          "(attr through_hole exclude_from_bom)")
    hits = _by_code(_schpcb(proj))["SCHPCB_BOM_FLAG_MISMATCH"]
    assert sorted(h.refs[0] for h in hits) == ["J1", "R2"]


def test_matching_flags_are_silent(proj):
    codes = {f.code for f in _schpcb(proj)}
    assert "SCHPCB_DNP_MISMATCH" not in codes        # R2: DNP on both sides
    assert "SCHPCB_BOM_FLAG_MISMATCH" not in codes   # R2: out of BOM on both


def test_flags_are_not_compared_when_a_reader_cannot_know_them(proj):
    """An Altium schematic / board carries the model DEFAULTS for these
    flags, not facts: comparing them would invent mismatches."""
    sch = kicad.read_sch(str(proj / "board.kicad_sch"))
    pcb = kicad.read_pcb(str(_board(proj)))
    for c in sch.components:
        c.dnp, c.in_bom = False, True                # what an Altium read holds
    assert "SCHPCB_DNP_MISMATCH" in {f.code for f in schpcb.run(sch, pcb)}
    sch.source_format = "altium"
    codes = {f.code for f in schpcb.run(sch, pcb)}
    assert not codes & {"SCHPCB_DNP_MISMATCH", "SCHPCB_BOM_FLAG_MISMATCH"}
    sch.source_format, pcb.source_format = "kicad", "altium"
    codes = {f.code for f in schpcb.run(sch, pcb)}
    assert not codes & {"SCHPCB_DNP_MISMATCH", "SCHPCB_BOM_FLAG_MISMATCH"}


# --------------------------------------------------------------------------- #
# fplib: each finding code, positive + negative
# --------------------------------------------------------------------------- #
def test_unedited_board_matches_its_library(proj):
    findings, coverage = _lib_run(proj)
    # the only findings are the model notes of the three model-less footprints
    assert sorted((f.code, f.severity.value, f.refs[0]) for f in findings) == [
        ("FPLIB_MODEL_MISSING", "note", "H1"),
        ("FPLIB_MODEL_MISSING", "note", "R2"),
        ("FPLIB_MODEL_MISSING", "note", "TP1"),
    ]
    assert coverage == {
        "footprints": 6, "compared": 5, "not_compared": 1,
        "reasons": {"global_library": 1, "unregistered_library": 0,
                    "no_library_id": 0, "library_unavailable": 0,
                    "unsupported_library": 0, "not_in_library": 0,
                    "ambiguous_designator": 0},
    }


def test_pad_set_drift_pad_removed_on_the_board(proj):
    # delete J1's pad 2 from the board copy
    text = _board(proj).read_text(encoding="utf-8")
    start = text.index('(pad "2" thru_hole oval')
    end = text.index("(embedded_fonts no)", start)
    _write(_board(proj), text[:start] + text[end:])
    (hit,) = _by_code(_lib_run(proj)[0])["FPLIB_PAD_SET_DRIFT"]
    assert hit.severity.value == "warning" and hit.refs == ["J1"]
    assert "board has 1 pad(s), library has 2" in hit.message
    assert "only in the library: 2" in hit.message
    assert "local board edit is legitimate" in hit.message


def test_pad_set_drift_pad_renumbered_in_the_library(proj):
    _edit(proj / "proj.pretty" / "R_0402.kicad_mod", '(pad "2" smd', '(pad "3" smd')
    hits = _by_code(_lib_run(proj)[0])["FPLIB_PAD_SET_DRIFT"]
    assert sorted(h.refs[0] for h in hits) == ["R1", "R2"]   # both placements
    assert all("only on the board: 2" in h.message
               and "only in the library: 3" in h.message for h in hits)


def test_pad_drill_drift_on_drill_size(proj):
    _edit(_board(proj), "(drill 1)", "(drill 0.8)")
    by = _by_code(_lib_run(proj)[0])
    assert "FPLIB_PAD_SET_DRIFT" not in by              # same pad numbers
    (hit,) = by["FPLIB_PAD_DRILL_DRIFT"]
    assert hit.severity.value == "warning" and hit.refs == ["J1"]
    assert "pad 1: board thru_hole drill 0.8, library thru_hole drill 1" \
        in hit.message
    assert "local board edit is legitimate" in hit.message


def test_pad_drill_drift_on_pad_type(proj):
    # the library's mounting hole becomes a plated hole; the board keeps NPTH
    _edit(proj / "proj.pretty" / "MountingHole_3.2mm.kicad_mod",
          '(pad "" np_thru_hole circle', '(pad "" thru_hole circle')
    (hit,) = _by_code(_lib_run(proj)[0])["FPLIB_PAD_DRILL_DRIFT"]
    assert hit.refs == ["H1"]
    assert "pad (unnumbered): board np_thru_hole drill 3.2, library " \
           "thru_hole drill 3.2" in hit.message


def test_source_missing_when_the_library_lost_the_footprint(proj):
    (proj / "proj.pretty" / "TestPoint.kicad_mod").unlink()
    findings, coverage = _lib_run(proj)
    (hit,) = _by_code(findings)["FPLIB_SOURCE_MISSING"]
    assert hit.severity.value == "warning" and hit.refs == ["TP1"]
    assert "proj:TestPoint" in hit.message
    assert coverage["compared"] == 4
    assert coverage["reasons"]["not_in_library"] == 1
    # a footprint that IS in the library never reports it
    assert all(f.refs != ["R1"] for f in _by_code(findings)["FPLIB_SOURCE_MISSING"])


def test_model_missing_when_the_file_is_gone_or_the_variable_unknown(proj):
    (proj / "3d" / "R_0402.step").unlink()
    _edit(_board(proj), "${KIPRJMOD}/3d/PinHeader_1x02.step",
          "${AKCLI_NO_SUCH_VAR}/PinHeader_1x02.step")
    notes = {f.refs[0]: f for f in _by_code(_lib_run(proj)[0])["FPLIB_MODEL_MISSING"]}
    assert "file not found" in notes["R1"].message
    assert "unresolvable path variable" in notes["J1"].message
    assert all(f.severity.value == "note" for f in notes.values())


def test_kicad_builtin_model_reference_is_not_reported_missing(proj):
    """``${KICAD<N>_3DMODEL_DIR}`` names KiCad's own stock model: whether it
    is installed is a property of the machine, so the result must not depend
    on it."""
    _edit(_board(proj), "${KIPRJMOD}/3d/R_0402.step",
          "${KICAD99_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402.step")
    assert "R1" not in {f.refs[0] for f in _lib_run(proj)[0]
                        if f.code == "FPLIB_MODEL_MISSING"}


def test_relative_model_path_resolves_against_project_then_library(proj):
    _edit(_board(proj), "${KIPRJMOD}/3d/R_0402.step", "3d/R_0402.step")
    refs = {f.refs[0] for f in _lib_run(proj)[0] if f.code == "FPLIB_MODEL_MISSING"}
    assert "R1" not in refs
    shutil.move(str(proj / "3d" / "R_0402.step"),
                str(proj / "proj.pretty" / "R_0402.step"))
    _edit(_board(proj), "3d/R_0402.step", "R_0402.step")
    refs = {f.refs[0] for f in _lib_run(proj)[0] if f.code == "FPLIB_MODEL_MISSING"}
    assert "R1" not in refs                          # found beside the .kicad_mod


# --------------------------------------------------------------------------- #
# coverage: what was NOT compared is counted, by reason
# --------------------------------------------------------------------------- #
def test_coverage_counts_every_not_compared_reason(proj):
    board = _board(proj)
    # R3 -> a nickname no table registers
    _edit(board, '"Resistor_SMD:R_0402_1005Metric"', '"nowhere:R_0402"')
    # TP1 -> no library id at all
    _edit(board, '(footprint "proj:TestPoint"', '(footprint "TestPoint"')
    # R2 -> shares R1's designator: its pads cannot be attributed
    _edit(board, '(property "Reference" "R2"', '(property "Reference" "R1"')
    findings, coverage = _lib_run(proj)
    assert coverage["footprints"] == 6
    assert coverage["compared"] == 2                     # H1, J1
    assert coverage["not_compared"] == 4
    assert coverage["reasons"] == {
        "global_library": 0, "unregistered_library": 1, "no_library_id": 1,
        "library_unavailable": 0, "unsupported_library": 0,
        "not_in_library": 0, "ambiguous_designator": 2}
    assert sum(coverage["reasons"].values()) == coverage["not_compared"]
    text = fplib.coverage_text(coverage)
    assert text.startswith("2 of 6 board footprint(s) compared")
    assert "4 NOT compared" in text and "shared by several" in text


def test_coverage_when_the_project_library_does_not_resolve(proj):
    shutil.rmtree(proj / "proj.pretty")
    findings, coverage = _lib_run(proj)
    assert coverage["compared"] == 0
    assert coverage["reasons"]["library_unavailable"] == 5
    # `library audit` reports the broken table entry; this leg invents nothing
    assert {f.code for f in findings} <= {"FPLIB_MODEL_MISSING"}


def test_single_file_library_is_not_compared_and_not_called_missing(proj):
    """An Altium ``.PcbLib`` registered in the fp-lib-table is a library KiCad
    reads and this check does not: counted as not compared, never reported
    as a footprint that went missing."""
    shutil.rmtree(proj / "proj.pretty")
    (proj / "proj.PcbLib").write_bytes(b"not a real PcbLib")
    _edit(proj / "fp-lib-table", '(type "KiCad") (uri "${KIPRJMOD}/proj.pretty")',
          '(type "Altium") (uri "${KIPRJMOD}/proj.PcbLib")')
    findings, coverage = _lib_run(proj)
    assert "FPLIB_SOURCE_MISSING" not in {f.code for f in findings}
    assert coverage["compared"] == 0
    assert coverage["reasons"]["unsupported_library"] == 5
    assert "not a .pretty directory" in fplib.coverage_text(coverage)


def test_no_project_table_means_nothing_is_compared(proj):
    (proj / "fp-lib-table").unlink()
    findings, coverage = _lib_run(proj)
    assert findings == [] and coverage["compared"] == 0
    assert coverage["not_compared"] == 6
    assert coverage["reasons"]["unregistered_library"] == 5   # proj:* now unknown
    assert coverage["reasons"]["global_library"] == 1         # R3


# --------------------------------------------------------------------------- #
# CLI: verify --library
# --------------------------------------------------------------------------- #
def _verify(proj: Path, *extra: str) -> list[str]:
    return ["verify", str(proj / "board.kicad_sch"), str(_board(proj)), *extra]


def test_cli_without_the_flag_is_unchanged(proj, capsys):
    assert main(_verify(proj, "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "library" not in doc
    assert {f["code"] for f in doc["findings"]} == {"SCHPCB_EXTRA_ON_PCB"}
    assert main(_verify(proj)) == 0
    assert "library:" not in capsys.readouterr().out


def test_cli_library_json_is_stamped_and_states_coverage(proj, capsys):
    assert main(_verify(proj, "--library", "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1"
    assert doc["equivalent"] is True and doc["mode"] == "sch-pcb"
    assert doc["library"] == {
        "footprints": 6, "compared": 5, "not_compared": 1,
        "reasons": {"global_library": 1, "unregistered_library": 0,
                    "no_library_id": 0, "library_unavailable": 0,
                    "unsupported_library": 0, "not_in_library": 0,
                    "ambiguous_designator": 0},
    }
    assert {f["code"] for f in doc["findings"]} == {
        "SCHPCB_EXTRA_ON_PCB", "FPLIB_MODEL_MISSING"}


def test_cli_library_text_states_what_was_not_compared(proj, capsys):
    assert main(_verify(proj, "--library")) == 0
    out = capsys.readouterr().out
    assert "SCH-PCB EQUIVALENCE: PASS" in out
    assert ("library: 5 of 6 board footprint(s) compared against the project "
            "fp-lib-table; 1 NOT compared (1 library only in the global "
            "fp-lib-table)") in out


def test_cli_library_drift_is_a_warning_not_a_verdict(proj, capsys):
    _edit(_board(proj), "(drill 1)", "(drill 0.8)")
    assert main(_verify(proj, "--library", "--json")) == 0     # PASS: no ERROR
    doc = json.loads(capsys.readouterr().out)
    assert doc["equivalent"] is True
    (hit,) = [f for f in doc["findings"] if f["code"] == "FPLIB_PAD_DRILL_DRIFT"]
    assert hit["severity"] == "warning" and hit["refs"] == ["J1"]


def test_cli_library_waiver_by_designator(proj, capsys):
    _edit(_board(proj), "(drill 1)", "(drill 0.8)")
    _waive(proj, 'code = "FPLIB_PAD_DRILL_DRIFT"\nrefs = ["J1"]\n'
                 'reason = "0.8 mm pins on this build"')
    assert main(_verify(proj, "--library", "--json")) == 0
    io = capsys.readouterr()
    doc = json.loads(io.out)
    assert "FPLIB_PAD_DRILL_DRIFT" not in {f["code"] for f in doc["findings"]}
    assert doc["waived"] == [{"code": "FPLIB_PAD_DRILL_DRIFT", "refs": ["J1"],
                              "action": "dropped",
                              "reason": "0.8 mm pins on this build"}]
    assert "waived: FPLIB_PAD_DRILL_DRIFT [J1] dropped" in io.err
    assert doc["library"]["compared"] == 5       # a waiver never hides coverage

    # another designator's waiver does not touch it
    _waive(proj, 'code = "FPLIB_PAD_DRILL_DRIFT"\nrefs = ["R1"]')
    assert main(_verify(proj, "--library", "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "FPLIB_PAD_DRILL_DRIFT" in {f["code"] for f in doc["findings"]}


def test_cli_dnp_mismatch_waiver_by_designator(proj, capsys):
    _edit(_board(proj), "(attr through_hole)", "(attr through_hole dnp)")
    assert main(_verify(proj, "--json")) == 0                   # a WARNING
    doc = json.loads(capsys.readouterr().out)
    (hit,) = [f for f in doc["findings"] if f["code"] == "SCHPCB_DNP_MISMATCH"]
    assert hit["refs"] == ["J1"] and hit["severity"] == "warning"
    _waive(proj, 'code = "SCHPCB_DNP_MISMATCH"\nrefs = ["J1"]')
    assert main(_verify(proj, "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "SCHPCB_DNP_MISMATCH" not in {f["code"] for f in doc["findings"]}


def test_cli_library_needs_a_board(proj, capsys):
    sch = str(proj / "board.kicad_sch")
    assert main(["verify", sch, sch, "--library"]) == 2
    assert "--library" in capsys.readouterr().err
