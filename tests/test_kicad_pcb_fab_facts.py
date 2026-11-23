"""What ``read_pcb`` establishes for ``fab check`` — outline, via type, tenting,
drill slots — and what ``kicad_pro.read`` establishes about the project's own
design rules. Fixture-first: every board under ``tests/fixtures/kicad/fab/``
was written (or re-saved) by a real kicad-cli 10.0.4, and each expectation is
held against TWO independent references:

* the analytic value (a rounded rectangle, a disc), to a tight tolerance;
* KiCad's own measurement — the committed ``*.stats.kicad10.json`` is the
  output of ``kicad-cli pcb export stats`` on the same file. KiCad tessellates
  arcs (5 µm max error), so its areas sit a hair under the exact value and
  are compared loosely.

The legacy grammars (KiCad 5 centre/angle arcs and ``(module ...)``, KiCad 9
bare tenting tokens, the KiCad <= 8 ``viasonmask`` plot option) are
hand-authored inputs: no KiCad 5/8/9 binary was available to write them. What
proves the READING is the sibling ``*.kicad10.kicad_pcb`` — the same file as
kicad-cli 10 re-saved it — and the reader must get the same answer from both.
``tests/test_e2e_fab.py`` re-derives all of it live wherever KiCad is installed.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import pytest

from akcli.readers import kicad, kicad_pro

FAB = Path(__file__).parent / "fixtures" / "kicad" / "fab"

# The fixture coordinates are KiCad's own 6-decimal millimetres, so an exact
# formula reproduces to ~1e-5 mm²; KiCad's tessellated areas to ~1e-4 relative.
ANALYTIC = 1e-4
KICAD_REL = 3e-4

ROUNDED_RECT = 40 * 30 - (4 - math.pi) * 2 ** 2      # 40 x 30 mm, r = 2


def _read(name: str):
    return kicad.read_pcb(str(FAB / f"{name}.kicad_pcb"))


def _stats(name: str, variant: str = "stats") -> dict:
    return json.loads((FAB / f"{name}.{variant}.kicad10.json")
                      .read_text(encoding="utf-8"))


def _mm2(text: str) -> float:
    """``"1196.557 mm²"`` -> 1196.557 (KiCad prints the unit into the value)."""
    return float(text.split()[0])


def _bodies(outline: dict) -> float:
    """Board bodies only — what KiCad's statistics call ``area`` (cutouts are
    subtracted there only under ``--subtract-holes-from-board``)."""
    return sum(lp["area_mm2"] for lp in outline["loops"] if lp["depth"] == 0)


def _write(tmp_path, body: str, *, setup: str = "", name: str = "b.kicad_pcb"):
    p = tmp_path / name
    p.write_text(
        '(kicad_pcb (version 20260206) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)'
        ' (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
        f'  (setup (pad_to_mask_clearance 0) {setup})\n  (net 0 "")\n'
        + body + "\n)\n", encoding="utf-8", newline="\n")
    return kicad.read_pcb(str(p))


# --------------------------------------------------------------------------- #
# outline
# --------------------------------------------------------------------------- #
def test_arc_cornered_outline_with_cutouts():
    """gr_line + gr_arc (start/mid/end) chained into the body; a gr_circle, a
    gr_rect and a gr_poly as cutouts."""
    pcb = _read("outline_cutouts")
    outline = pcb.board["outline"]
    assert outline["closed"] is True and outline["problems"] == []
    assert [(lp["depth"], lp["cutout"]) for lp in outline["loops"]] == [
        (0, False), (1, True), (1, True), (1, True)]
    areas = [lp["area_mm2"] for lp in outline["loops"]]
    assert areas == pytest.approx(
        [ROUNDED_RECT, math.pi * 9, 16.0, 8.0], abs=ANALYTIC)
    assert outline["area_mm2"] == pytest.approx(
        ROUNDED_RECT - math.pi * 9 - 16 - 8, abs=ANALYTIC)
    assert outline["bbox"] == ((10.0, 10.0), (50.0, 40.0))
    assert pcb.board["outline_bbox"] == outline["bbox"]     # historical key

    # ...and KiCad's own numbers for the same file
    board = _stats("outline_cutouts")["board"]
    assert board["has_outline"] is True
    assert _bodies(outline) == pytest.approx(_mm2(board["area"]), rel=KICAD_REL)
    holes = _stats("outline_cutouts", "stats-subtract-holes")["board"]
    assert outline["area_mm2"] == pytest.approx(_mm2(holes["area"]),
                                                rel=KICAD_REL)
    assert (float(board["width"].split()[0]),
            float(board["height"].split()[0])) == (40.0, 30.0)


def test_a_board_that_is_one_circle():
    outline = _read("outline_circle").board["outline"]
    assert outline["closed"] is True
    assert outline["area_mm2"] == pytest.approx(math.pi * 15 ** 2, abs=ANALYTIC)
    assert outline["bbox"] == ((35.0, 35.0), (65.0, 65.0))
    board = _stats("outline_circle")["board"]
    assert outline["area_mm2"] == pytest.approx(_mm2(board["area"]),
                                                rel=KICAD_REL)


def test_rounded_rectangles_keep_their_corner_radius():
    """KiCad 10 ``(gr_rect … (radius r))`` / ``(fp_rect … (radius r))``.
    Ignoring the radius would overstate every such shape by (4 − π)·r² —
    silently. Also here: a radius kicad-cli clamped to half the short side
    (a stadium), and a rounded fp_rect in a ROTATED footprint, which
    kicad-cli re-saved as an fp_poly of four arcs and nothing else."""
    outline = _read("outline_rect_radius").board["outline"]
    assert outline["closed"] is True
    corner = 4 - math.pi                        # area one r=1 corner set removes
    body = 50 * 40 - corner * 2 ** 2
    stadium = 4 * 10 - corner * 2 ** 2
    in_footprint = 6 * 4 - corner               # twice: rotated and not
    square = 4 * 4 - corner
    areas = [lp["area_mm2"] for lp in outline["loops"]]
    assert areas == pytest.approx(
        [body, stadium, in_footprint, in_footprint, square], abs=ANALYTIC)
    assert outline["area_mm2"] == pytest.approx(
        body - stadium - 2 * in_footprint - square, abs=ANALYTIC)
    assert outline["bbox"] == ((10.0, 10.0), (60.0, 50.0))

    board = _stats("outline_rect_radius")["board"]
    assert _bodies(outline) == pytest.approx(_mm2(board["area"]), rel=KICAD_REL)
    assert _mm2(board["area"]) < 50 * 40 - 1        # KiCad rounds them too
    holes = _stats("outline_rect_radius", "stats-subtract-holes")["board"]
    assert outline["area_mm2"] == pytest.approx(_mm2(holes["area"]),
                                                rel=KICAD_REL)


def test_polygon_outline_with_an_arc_in_its_points():
    outline = _read("outline_poly_arc").board["outline"]
    assert outline["closed"] is True
    assert outline["area_mm2"] == pytest.approx(
        60 * 50 + math.pi * 5 ** 2 / 2, abs=ANALYTIC)
    assert outline["bbox"] == ((10.0, 10.0), (75.0, 60.0))   # the arc bulges
    board = _stats("outline_poly_arc")["board"]
    assert outline["area_mm2"] == pytest.approx(_mm2(board["area"]),
                                                rel=KICAD_REL)


def test_unclosed_outline_reports_no_area_but_keeps_the_bbox():
    pcb = _read("outline_open")
    outline = pcb.board["outline"]
    assert outline["closed"] is False
    assert outline["area_mm2"] is None          # never a guessed area
    assert outline["open_ends"] == 2
    assert outline["problems"] == [
        "2 segment end(s) meet nothing within 0.01 mm"]
    assert pcb.board["outline_bbox"] == ((10.0, 10.0), (50.0, 40.0))
    # KiCad says the same thing about this file
    board = _stats("outline_open")["board"]
    assert board["has_outline"] is False and board["area"] is None


def test_footprint_graphics_on_edge_cuts_are_part_of_the_outline():
    """A rotated back-side footprint supplies a notch of the body (fp_line +
    fp_arc); other footprints supply cutouts (fp_circle, fp_poly, fp_rect —
    kicad-cli re-saved the rotated fp_rect as an fp_poly). The area only
    comes out right if the footprint transform matches KiCad's."""
    outline = _read("outline_footprint").board["outline"]
    assert outline["closed"] is True
    body = 60 * 40 - 5 * 20 - math.pi * 2 ** 2 / 2     # notch + its round end
    areas = [lp["area_mm2"] for lp in outline["loops"]]
    assert areas == pytest.approx([body, math.pi * 9, 24.0, 12.0, 6.0],
                                  abs=1e-3)
    assert [lp["depth"] for lp in outline["loops"]] == [0, 1, 1, 1, 1]
    assert outline["area_mm2"] == pytest.approx(
        body - math.pi * 9 - 24 - 12 - 6, abs=1e-3)
    assert outline["bbox"] == ((0.0, 0.0), (60.0, 40.0))

    board = _stats("outline_footprint")["board"]
    assert board["has_outline"] is True
    assert _bodies(outline) == pytest.approx(_mm2(board["area"]), rel=KICAD_REL)
    holes = _stats("outline_footprint", "stats-subtract-holes")["board"]
    assert outline["area_mm2"] == pytest.approx(_mm2(holes["area"]),
                                                rel=KICAD_REL)


def test_kicad5_centre_angle_arcs_and_modules_read_like_their_resave():
    """``(gr_arc (start <centre>) (end <first point>) (angle <deg>))`` and
    ``(module ... (fp_arc ... (angle ...)))``: the hand-authored KiCad 5 file
    and kicad-cli 10's re-save of it must describe the same board."""
    legacy = _read("legacy_v5").board["outline"]
    resave = _read("legacy_v5.kicad10").board["outline"]
    assert legacy["closed"] is True and resave["closed"] is True
    expected = ROUNDED_RECT - 4 * 10 - math.pi * 2 ** 2 / 2 - math.pi * 9
    assert legacy["area_mm2"] == pytest.approx(expected, abs=ANALYTIC)
    assert resave["area_mm2"] == pytest.approx(legacy["area_mm2"], abs=ANALYTIC)
    assert legacy["bbox"] == resave["bbox"] == ((10.0, 10.0), (50.0, 40.0))
    assert [lp["depth"] for lp in legacy["loops"]] == [0, 1]


def test_bezier_on_edge_cuts_is_not_modelled_and_says_so(tmp_path):
    pcb = _write(tmp_path, """
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts"))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts"))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts"))
  (gr_curve (pts (xy 0 20) (xy -8 15) (xy -8 5) (xy 0 0)) (layer "Edge.Cuts"))""")
    outline = pcb.board["outline"]
    assert outline["closed"] is False and outline["area_mm2"] is None
    assert any("Bezier" in p for p in outline["problems"])
    # the control points bound the curve, so the bbox stays conservative
    assert pcb.board["outline_bbox"] == ((-8.0, 0.0), (30.0, 20.0))


def test_graphics_on_other_layers_are_not_outline(tmp_path):
    pcb = _write(tmp_path, """
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts"))
  (gr_circle (center 100 100) (end 150 100) (layer "Dwgs.User"))
  (gr_line (start 200 200) (end 300 300) (layer "F.SilkS"))""")
    assert pcb.board["outline"]["area_mm2"] == pytest.approx(600.0)
    assert pcb.board["outline_bbox"] == ((0.0, 0.0), (30.0, 20.0))


def test_no_edge_cuts_means_no_outline_keys(tmp_path):
    pcb = _write(tmp_path, '(gr_line (start 0 0) (end 9 9) (layer "F.SilkS"))')
    assert "outline" not in pcb.board and "outline_bbox" not in pcb.board


# --------------------------------------------------------------------------- #
# via type
# --------------------------------------------------------------------------- #
def test_via_types_including_buried():
    """KiCad 10 writes ``buried`` as its own token; a ``blind`` token on a
    layer pair touching neither outer layer (all KiCad <= 9 has for a buried
    via, and what kicad-cli 10 keeps on re-save) is buried too."""
    pcb = _read("vias_tenting")
    by_pos = {v["at"][0]: v for v in pcb.vias}
    assert {x: v["type"] for x, v in by_pos.items()} == {
        5.0: "through", 8.0: "through", 11.0: "through",
        14.0: "blind",       # (via blind  ... "F.Cu" "In1.Cu")
        17.0: "buried",      # (via buried ... "In1.Cu" "In2.Cu")
        20.0: "buried",      # (via blind  ... "In1.Cu" "In2.Cu")
        23.0: "micro",
        26.0: "blind",       # (via blind  ... "In2.Cu" "B.Cu")
    }
    ours = Counter(v["type"] for v in pcb.vias)
    theirs = _stats("vias_tenting")["vias"]
    assert ours["through"] == theirs["through"] == 3
    assert ours["micro"] == theirs["micro"] == 1
    # KiCad's statistics count by TOKEN: the inner-pair `blind` via is "blind"
    # there and buried here — same vias, one reclassified.
    assert theirs == {"through": 3, "blind": 3, "buried": 1, "micro": 1}
    assert (ours["blind"], ours["buried"]) == (2, 2)


def test_kicad5_board_vias_classify_the_same_as_the_resave():
    legacy = Counter(v["type"] for v in _read("legacy_v5").vias)
    resave = Counter(v["type"] for v in _read("legacy_v5.kicad10").vias)
    assert legacy == resave == {"through": 1, "blind": 1, "buried": 1,
                                "micro": 1}


# --------------------------------------------------------------------------- #
# tenting
# --------------------------------------------------------------------------- #
def test_kicad10_board_and_per_via_tenting():
    pcb = _read("vias_tenting")
    assert pcb.board["tenting"] == {"front": True, "back": False,
                                    "source": "setup.tenting"}
    by_pos = {v["at"][0]: v["tenting"] for v in pcb.vias}
    assert by_pos[5.0] == {"front": True, "back": False}    # follows the board
    assert by_pos[8.0] == {"front": False, "back": False}   # (front no) (back no)
    assert by_pos[11.0] == {"front": True, "back": True}    # (front yes) (back yes)


def test_kicad10_per_side_none_follows_the_board(tmp_path):
    pcb = _write(tmp_path, """
  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
    (tenting (front none) (back yes)))""",
                 setup="(tenting (front no) (back no))")
    assert pcb.vias[0]["tenting"] == {"front": False, "back": True}


def test_kicad9_bare_token_tenting():
    """``(tenting front)`` on the board: the unnamed side is NOT tented —
    kicad-cli 10 re-saves it as (front yes) (back no)."""
    legacy, resave = _read("tenting_v9"), _read("tenting_v9.kicad10")
    assert legacy.board["tenting"] == resave.board["tenting"] == {
        "front": True, "back": False, "source": "setup.tenting"}

    old = [v["tenting"] for v in legacy.vias]
    new = [v["tenting"] for v in resave.vias]
    assert old[0] == new[0] == {"front": True, "back": False}   # no override
    assert old[1] == new[1] == {"front": True, "back": True}    # front back
    # A side a KiCad 9 via does NOT name: kicad-cli 10 reads it as "follow the
    # board"; the form itself reads as "not tented". Unverified against KiCad
    # 9, so it is known only where both readings agree (board side open).
    assert old[2] == {"front": None, "back": False}             # (tenting none)
    assert new[2] == {"front": True, "back": False}
    assert old[3] == {"front": None, "back": True}              # (tenting back)
    assert new[3] == {"front": True, "back": True}


@pytest.mark.parametrize("setup, expected", [
    ("(tenting front back)", (True, True)),
    ("(tenting none)", (False, False)),
    ("(tenting back)", (False, True)),
])
def test_kicad9_board_tenting_forms(tmp_path, setup, expected):
    board = _write(tmp_path, "", setup=setup).board["tenting"]
    assert (board["front"], board["back"]) == expected
    assert board["source"] == "setup.tenting"


def test_kicad8_and_older_carry_tenting_as_a_plot_option(tmp_path):
    """No board tenting before KiCad 9 — only ``(pcbplotparams (viasonmask
    ..))``, which kicad-cli 10 migrates into the board setting."""
    legacy, resave = _read("legacy_v5"), _read("legacy_v5.kicad10")
    assert legacy.board["tenting"] == {
        "front": False, "back": False, "source": "pcbplotparams.viasonmask"}
    assert resave.board["tenting"] == {
        "front": False, "back": False, "source": "setup.tenting"}
    assert all(v["tenting"] == {"front": False, "back": False}
               for v in legacy.vias)

    tented = _write(tmp_path, "", setup="(pcbplotparams (viasonmask false))")
    assert tented.board["tenting"] == {
        "front": True, "back": True, "source": "pcbplotparams.viasonmask"}


def test_a_board_that_states_nothing_is_not_assumed_tented(tmp_path):
    pcb = _write(tmp_path, '(via (at 1 1) (size 0.6) (drill 0.3) '
                           '(layers "F.Cu" "B.Cu"))')
    assert pcb.board["tenting"] == {"front": None, "back": None, "source": None}
    assert pcb.vias[0]["tenting"] == {"front": None, "back": None}


# --------------------------------------------------------------------------- #
# drills / slots
# --------------------------------------------------------------------------- #
def test_oval_drills_keep_both_dimensions():
    pcb = _read("slots")
    drilled = {(p["number"], p["at"][0]): p for p in pcb.pads if p["drill"]}
    got = {key: (p["drill_shape"], p["drill_size"], p["drill"])
           for key, p in drilled.items()}
    assert got == {
        ("1", 5.0): ("oval", (0.8, 2.2), 0.8),
        ("2", 10.0): ("oval", (0.3, 1.0), 0.3),
        ("3", 15.0): ("oval", (0.9, 0.9), 0.9),     # written (drill oval 0.9)
        ("4", 20.0): ("oval", (2.2, 0.8), 2.2),     # ...(offset 0.2 0) ignored
        ("5", 25.0): ("round", (0.8, 0.8), 0.8),
        ("", 30.0): ("oval", (0.6, 2.5), 0.6),
        ("", 35.0): ("oval", (1.0, 2.5), 1.0),
    }
    # KiCad's own drill table for the same file, as a multiset
    ours = Counter(
        ("Slot" if p["drill_shape"] == "oval" else "Round",
         p["drill_size"], p["pad_type"] != "np_thru_hole")
        for p in pcb.pads if p["drill"])
    theirs = Counter(
        (h["shape"], (float(h["x_size"].split()[0]),
                      float(h["y_size"].split()[0])), h["plated"])
        for h in _stats("slots")["drill_holes"] for _ in range(h["count"]))
    assert ours == theirs


def test_pads_without_a_hole_have_no_drill_fields(tmp_path):
    pcb = _write(tmp_path, """
  (footprint "X" (layer "F.Cu") (at 0 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "1k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))""")
    pad = pcb.pads[0]
    assert (pad["drill"], pad["drill_shape"], pad["drill_size"]) == (
        None, None, None)


# --------------------------------------------------------------------------- #
# .kicad_pro design rules
# --------------------------------------------------------------------------- #
RULES_PRO = FAB / "rules" / "rules_demo.kicad_pro"


def test_design_rules_are_read_from_the_project_file():
    """The ``rules`` object is KiCad's own (transcribed from the
    Arduino_Mega template KiCad ships): millimetre minimums are kept, the
    booleans living beside them are not measurements."""
    pro = kicad_pro.read(RULES_PRO)
    assert pro.ok
    rules = pro.design_rules
    assert rules["min_clearance"] == 0.0
    assert rules["min_track_width"] == 0.2
    assert rules["min_via_diameter"] == 0.4
    assert rules["min_through_hole_diameter"] == 0.3
    assert rules["min_via_annular_width"] == 0.05
    assert rules["min_hole_to_hole"] == 0.25
    assert rules["min_copper_edge_clearance"] == 0.075
    assert "allow_microvias" not in rules              # a bool, not a number
    assert "use_height_for_length_calcs" not in rules
    assert list(rules) == sorted(rules)                # deterministic order
    assert all(isinstance(v, float) for v in rules.values())


@pytest.mark.parametrize("doc", [
    {},                                                 # no "board" at all
    {"board": []},
    {"board": {"design_settings": None}},
    {"board": {"design_settings": {"rules": []}}},
    {"board": {"design_settings": {"rules": {}}}},      # a fresh project
    {"board": {"design_settings": {"rules": {
        "min_clearance": "0.2", "min_track_width": True, "x": None}}}},
])
def test_design_rules_degrade_to_empty(tmp_path, doc):
    p = tmp_path / "p.kicad_pro"
    p.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    pro = kicad_pro.read(p)
    assert pro.ok and pro.design_rules == {}


def test_design_rules_of_an_unreadable_project_are_empty(tmp_path):
    p = tmp_path / "p.kicad_pro"
    p.write_text("{ not json", encoding="utf-8", newline="\n")
    pro = kicad_pro.read(p)
    assert not pro.ok and pro.design_rules == {}
    assert kicad_pro.read(tmp_path / "missing.kicad_pro").design_rules == {}
