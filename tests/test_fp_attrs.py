"""Board-footprint attributes — fixture-first (``tests/fixtures/kicad/fp_attrs``).

What a ``.kicad_pcb`` footprint's ``(attr ...)`` / ``(at ...)`` / ``(model
...)`` nodes mean is a KiCad grammar claim, so it is pinned against files a
real KiCad wrote: ``attrs.kicad10.kicad_pcb`` and ``legacy_v5.kicad10.kicad_pcb``
are ``kicad-cli 10.0.4 pcb upgrade`` output of the hand-authored KiCad 8 / 5
inputs beside them (see the fixtures README). Three facts the reader is built
on, each proven there:

* DNP is the ``dnp`` TOKEN inside ``(attr ...)`` in every version through
  KiCad 10 — a footprint-level ``(dnp yes)`` child does not even load;
* KiCad 10's ``(variant (name ..) (dnp yes) ..)`` block is a per-variant
  override and must NOT read as the footprint's own flag;
* legacy ``(attr virtual)`` means "excluded from BOM and position files"
  (KiCad rewrites it to exactly those two tokens) — and NOT ``board_only``.

Where a ``kicad-cli`` is installed the upgrade is re-run and must still agree
with the committed output (parsed fields, not bytes: KiCad mints fresh uuids
for the properties it adds).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from akcli import libtable, safety
from akcli.drivers import kicad_cli
from akcli.readers import kicad

FIX = Path(__file__).parent / "fixtures" / "kicad" / "fp_attrs"

needs_cli = pytest.mark.skipif(kicad_cli.find() is None,
                               reason="kicad-cli not installed")


def _by_ref(name: str) -> dict:
    return {f.designator: f for f in kicad.read_pcb(str(FIX / name)).footprints}


def _flags(fp) -> dict:
    return {"dnp": fp.dnp, "exclude_from_bom": fp.exclude_from_bom,
            "exclude_from_pos": fp.exclude_from_pos,
            "board_only": fp.board_only}


def _summary(path: Path) -> dict:
    """Everything the reader takes from a footprint node, per designator."""
    return {f.designator: (f.footprint_name, f.layer, f.at, f.rotation % 360,
                           tuple(sorted(_flags(f).items())), tuple(f.models))
            for f in kicad.read_pcb(str(path)).footprints}


_NONE = {"dnp": False, "exclude_from_bom": False, "exclude_from_pos": False,
         "board_only": False}


# --------------------------------------------------------------------------- #
# KiCad 8 input and its KiCad 10 re-save read identically
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["attrs_v8.kicad_pcb", "attrs.kicad10.kicad_pcb"])
def test_attr_tokens(name):
    fps = _by_ref(name)
    assert set(fps) == {"R1", "R2", "H1", "J1", "TP1", "R3"}
    # (attr smd) — a type token alone sets no flag
    assert _flags(fps["R1"]) == _NONE
    # (attr smd exclude_from_pos_files exclude_from_bom dnp)
    assert _flags(fps["R2"]) == {"dnp": True, "exclude_from_bom": True,
                                 "exclude_from_pos": True, "board_only": False}
    # (attr board_only exclude_from_pos_files exclude_from_bom
    #       allow_missing_courtyard allow_soldermask_bridges) — no type token,
    # and KiCad 10 wraps the list over three lines
    assert _flags(fps["H1"]) == {"dnp": False, "exclude_from_bom": True,
                                 "exclude_from_pos": True, "board_only": True}
    assert _flags(fps["J1"]) == _NONE                 # (attr through_hole)
    assert _flags(fps["TP1"]) == _NONE                # no (attr ...) node at all


@pytest.mark.parametrize("name", ["attrs_v8.kicad_pcb", "attrs.kicad10.kicad_pcb"])
def test_position_and_models(name):
    fps = _by_ref(name)
    assert fps["R1"].at == (100.0, 100.0) and fps["R1"].rotation == 90.0
    assert fps["R2"].at == (110.0, 100.0) and fps["R2"].rotation == 0.0
    assert fps["J1"].at == (130.0, 100.0) and fps["J1"].rotation == 180.0
    assert fps["J1"].layer == "B.Cu"
    assert fps["R1"].models == ["${KIPRJMOD}/3d/R_0402.step"]
    # a hidden model `(model "..." (hide yes) ...)` is still a model reference
    assert fps["J1"].models == ["${KIPRJMOD}/3d/PinHeader_1x02.step"]
    assert fps["R2"].models == [] and fps["H1"].models == []


def test_kicad10_output_reads_like_its_kicad8_input():
    assert _summary(FIX / "attrs.kicad10.kicad_pcb") == _summary(
        FIX / "attrs_v8.kicad_pcb")


def test_kicad10_fixture_is_what_kicad_wrote():
    text = (FIX / "attrs.kicad10.kicad_pcb").read_text(encoding="utf-8")
    assert "(version 20260206)" in text and '(generator_version "10.0")' in text
    # KiCad keeps DNP inside (attr ...) — no footprint-level (dnp ...) node
    assert "(attr smd exclude_from_pos_files exclude_from_bom dnp)" in text
    # the three-line wrap the reader must survive
    assert re.search(r"\(attr board_only exclude_from_pos_files "
                     r"exclude_from_bom allow_missing_courtyard\n\s+"
                     r"allow_soldermask_bridges\n\s+\)", text)


def test_variant_override_is_not_the_footprint_flag():
    """R1 carries ``(variant (name "NoFit") (dnp yes) (exclude_from_bom yes)
    (exclude_from_pos_files yes))`` — KiCad 10 wrote that block — and its own
    ``(attr smd)`` says fitted. The base flags are what the reader reports."""
    text = (FIX / "attrs.kicad10.kicad_pcb").read_text(encoding="utf-8")
    block = re.search(r'\(variant\s+\(name "NoFit"\)\s+\(dnp yes\)\s+'
                      r'\(exclude_from_bom yes\)\s+'
                      r'\(exclude_from_pos_files yes\)\s+\)', text)
    assert block, "the fixture lost its KiCad-written (variant ...) block"
    assert _flags(_by_ref("attrs.kicad10.kicad_pcb")["R1"]) == _NONE


# --------------------------------------------------------------------------- #
# KiCad 5 `(attr virtual)` -> what KiCad 10 turns it into
# --------------------------------------------------------------------------- #
def test_legacy_virtual_as_kicad10_rewrites_it():
    fps = _by_ref("legacy_v5.kicad10.kicad_pcb")
    assert set(fps) == {"FID1", "R1", "TP1"}
    # (attr virtual) -> (attr exclude_from_pos_files exclude_from_bom)
    assert _flags(fps["FID1"]) == {"dnp": False, "exclude_from_bom": True,
                                   "exclude_from_pos": True, "board_only": False}
    assert _flags(fps["R1"]) == _NONE and _flags(fps["TP1"]) == _NONE
    # KiCad normalizes the angle on save; the reader keeps it as written
    assert fps["R1"].rotation == -90.0
    assert fps["R1"].models == [
        "${KISYS3DMOD}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl"]
    text = (FIX / "legacy_v5.kicad10.kicad_pcb").read_text(encoding="utf-8")
    assert "(attr exclude_from_pos_files exclude_from_bom)" in text
    assert "virtual" not in text


def test_virtual_token_reads_as_bom_and_pos_exclusion(tmp_path):
    """The un-upgraded spelling: ``(footprint ... (attr virtual))`` reads as
    the two flags KiCad itself maps it to (see the test above)."""
    board = tmp_path / "v.kicad_pcb"
    board.write_text(
        (FIX / "attrs_v8.kicad_pcb").read_text(encoding="utf-8").replace(
            "(attr through_hole)", "(attr virtual)"),
        encoding="utf-8", newline="\n")
    j1 = {f.designator: f for f in kicad.read_pcb(str(board)).footprints}["J1"]
    assert _flags(j1) == {"dnp": False, "exclude_from_bom": True,
                          "exclude_from_pos": True, "board_only": False}


# --------------------------------------------------------------------------- #
# JSON export: the new fields are ADDITIVE
# --------------------------------------------------------------------------- #
def test_read_json_gains_the_fields_and_keeps_the_old_ones(capsys):
    from akcli.cli import main
    assert main(["read", str(FIX / "attrs.kicad10.kicad_pcb"), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    fps = {f["designator"]: f for f in doc["footprints"]}
    assert fps["R2"] == {
        # the pre-existing shape, untouched
        "designator": "R2", "footprint_name": "proj:R_0402", "layer": "F.Cu",
        "rotation": 0.0, "value": "10k",
        # additive
        "at": [110.0, 100.0], "dnp": True, "exclude_from_bom": True,
        "exclude_from_pos": True, "board_only": False, "models": [],
    }
    assert fps["H1"]["board_only"] is True
    assert fps["R1"]["models"] == ["${KIPRJMOD}/3d/R_0402.step"]


def test_a_reader_that_cannot_know_keeps_the_defaults():
    """The Altium board reader does not populate the fields: defaults, and
    ``at`` is ``None`` — never a guessed position or flag."""
    from akcli.model import Footprint
    fp = Footprint(designator="U1", footprint_name="QFN48", layer="TOP")
    assert (fp.at, fp.dnp, fp.exclude_from_bom, fp.exclude_from_pos,
            fp.board_only, fp.models) == (None, False, False, False, False, [])


# --------------------------------------------------------------------------- #
# real kicad-cli (skip-gated)
# --------------------------------------------------------------------------- #
def _exe_with_pcb_upgrade() -> str:
    exe = kicad_cli.find()
    assert exe is not None
    if (kicad_cli.version() or (0,))[0] < 10:
        pytest.skip("this kicad-cli predates `pcb upgrade` (KiCad 10)")
    return exe


def _upgrade(tmp_path: Path, source: str) -> Path:
    exe = _exe_with_pcb_upgrade()
    board = tmp_path / "board.kicad_pcb"
    shutil.copy(FIX / source, board)
    proc = safety.run_subprocess([exe, "pcb", "upgrade", str(board)], 180.0)
    assert proc.returncode == 0, proc.stderr
    return board


@needs_cli
def test_e2e_kicad_resaves_the_v8_board_the_way_the_fixture_says(tmp_path):
    fresh = _summary(_upgrade(tmp_path, "attrs_v8.kicad_pcb"))
    assert fresh == _summary(FIX / "attrs.kicad10.kicad_pcb")


@needs_cli
def test_e2e_kicad_resaves_the_v5_board_the_way_the_fixture_says(tmp_path):
    fresh = _summary(_upgrade(tmp_path, "legacy_v5.kicad_pcb"))
    assert fresh == _summary(FIX / "legacy_v5.kicad10.kicad_pcb")


@needs_cli
def test_e2e_footprint_level_dnp_node_is_not_kicad_grammar(tmp_path):
    """The claim "DNP lives only in (attr ...)" as a live probe: KiCad refuses
    a board whose footprint carries a ``(dnp yes)`` child."""
    exe = _exe_with_pcb_upgrade()
    board = tmp_path / "board.kicad_pcb"
    board.write_text(
        (FIX / "attrs_v8.kicad_pcb").read_text(encoding="utf-8").replace(
            "(attr smd exclude_from_pos_files exclude_from_bom dnp)",
            "(attr smd)\n\t\t(dnp yes)"),
        encoding="utf-8", newline="\n")
    before = board.read_bytes()
    safety.run_subprocess([exe, "pcb", "upgrade", str(board)], 180.0)
    assert board.read_bytes() == before, "KiCad accepted a (dnp yes) child"


@needs_cli
def test_e2e_boards_shipped_with_kicad_read_consistently():
    """KiCad's own template boards (written by the KiCad that shipped them):
    the reader's ``board_only`` count must equal the number of ``(attr ...)``
    nodes naming that token — no template is assumed to exist."""
    roots = [r / "template" for r in libtable.kicad_data_roots(kicad_cli.find())]
    boards = sorted(p for r in roots if r.is_dir()
                    for p in r.glob("*/*.kicad_pcb"))
    if not boards:
        pytest.skip("no KiCad template boards beside this kicad-cli")
    attr_rx = re.compile(r"\(attr\s([^()]*)\)")
    checked = 0
    for board in boards:
        text = board.read_text(encoding="utf-8", errors="replace")
        tokens = [m.group(1).split() for m in attr_rx.finditer(text)]
        fps = kicad.read_pcb(str(board)).footprints
        for flag, token in (("board_only", "board_only"),
                            ("exclude_from_bom", "exclude_from_bom"),
                            ("exclude_from_pos", "exclude_from_pos_files"),
                            ("dnp", "dnp")):
            assert (sum(1 for f in fps if getattr(f, flag))
                    == sum(1 for t in tokens if token in t)), (board.name, flag)
        checked += len(tokens)
    assert checked, "the template boards carry no (attr ...) node at all"
