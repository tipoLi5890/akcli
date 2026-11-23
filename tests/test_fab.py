"""Fab profile checks (`akcli fab check`) — plan acceptance criteria:

* 0.20/0.40 mm via  -> cost ERROR (paid small-via process)
* 0.30/0.40 mm via  -> passes, minimum-margin NOTE + below-preferred-annular NOTE
* 0.30/0.45 mm via  -> passes clean (the recommended geometry)
* a TENTED drill > 0.40 mm -> ERROR — tented per the board file, or per the
  order manifest's ``via_covering``; unknown tenting and no order -> one NOTE
* via inside an SMD pad -> ERROR; a registered thermal_via exception -> NOTE
* blind / buried / micro via under ``forbid_blind_buried`` -> ERROR
* profile stackup vs board setup drift -> ERROR
* cost thresholds: ``[cost.warn_if]`` -> WARNING, ``[cost.error_if]`` -> the
  same code at ERROR; area is the true Edge.Cuts outline when it closes, the
  bounding box (and the message says so) when it does not
* a slot narrower than ``[slot]`` allows -> ERROR
* project DRC rules looser than the profile's minimums -> one WARNING
* order manifest: missing fields ERROR; ENIG/panel raise review findings

Boards under ``tests/fixtures/kicad/fab/`` are real kicad-cli 10 output (see
that directory's README section); ``tests/test_kicad_pcb_fab_facts.py`` pins
what the reader makes of them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from akcli import fab
from akcli.cli import main
from akcli.errors import AkcliError
from akcli.readers import kicad
from tests._cli_harness import json_of, run_cli

ROOT = Path(__file__).resolve().parents[1]
FAB_FIX = Path(__file__).parent / "fixtures" / "kicad" / "fab"

_PROFILE = """
id = "jlc-4l-1oz-free-vias-2026-07"
vendor = "JLCPCB"

[source]
urls = ["https://jlcpcb.com/hk/help/article/pcb-via-covering"]
retrieved_at = "2026-07-14"

[stackup]
layers = 4
thickness_mm = 1.0

[via]
covering = "tented"
min_drill_mm = 0.30
min_pad_mm = 0.40
preferred_annular_mm = 0.075
max_tented_drill_mm = 0.40
forbid_via_in_pad = true
forbid_blind_buried = true

[cost.warn_if]
board_length_mm_gte = 600
board_area_cm2_gt = 650
drill_density_per_m2_gt = 150000
trace_width_mm_lte = 0.0889

[[exception]]
type = "thermal_via"
component = "U9"
owner = "hw-lead"
reason = "QFN exposed-pad thermal vias"
expires = "2099-01-01"
"""

_BOARD_HEAD = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.0))
  (layers
    (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
"""


def _board(tmp_path, body: str):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_BOARD_HEAD + body + "\n)")
    return kicad.read_pcb(str(p))


@pytest.fixture()
def profile(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text(_PROFILE)
    return fab.load_profile(p)


def _codes(findings):
    return {f.code for f in findings}


def test_paid_via_geometry_is_an_error(tmp_path, profile):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.4) (drill 0.2) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    findings = fab.check(pcb, profile)
    assert "FAB_VIA_PAID_PROCESS" in _codes(findings)
    assert any(f.severity.value == "error" for f in findings
               if f.code == "FAB_VIA_PAID_PROCESS")


def test_free_minimum_via_passes_with_margin_notes(tmp_path, profile):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.4) (drill 0.3) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    findings = fab.check(pcb, profile)
    codes = _codes(findings)
    assert "FAB_VIA_PAID_PROCESS" not in codes
    assert "FAB_VIA_MIN_MARGIN" in codes                  # drill on the boundary
    assert "FAB_VIA_ANNULAR_BELOW_PREFERRED" in codes     # 0.05 < 0.075
    assert all(f.severity.value == "note" for f in findings
               if f.code.startswith("FAB_VIA_"))


def test_recommended_via_passes_clean(tmp_path, profile):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.45) (drill 0.3) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    findings = fab.check(pcb, profile)
    assert not [f for f in findings if f.code.startswith("FAB_VIA_")
                and f.code != "FAB_VIA_MIN_MARGIN"]


def test_oversize_tented_via_fails(tmp_path, profile):
    # tented per the board file itself (KiCad 10 per-via form)
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.6) (drill 0.45) '
                           '(layers "F.Cu" "B.Cu") (net 1) '
                           '(tenting (front yes) (back yes)))')
    hits = [f for f in fab.check(pcb, profile)
            if f.code == "FAB_VIA_TENTED_TOO_BIG"]
    assert len(hits) == 1 and hits[0].severity.value == "error"
    assert "the board file tents this via" in hits[0].message


def test_oversize_via_of_unknown_tenting_is_one_note_not_an_error(tmp_path,
                                                                  profile):
    """The board says nothing about tenting (a KiCad 8 file) and no order was
    given: the cap is not applied, and that is SAID — once."""
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.6) (drill 0.45) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    findings = fab.check(pcb, profile)
    assert "FAB_VIA_TENTED_TOO_BIG" not in _codes(findings)
    notes = [f for f in findings if f.code == "FAB_TENTING_UNKNOWN"]
    assert len(notes) == 1 and notes[0].severity.value == "note"
    assert "NOT applied" in notes[0].message and "--order" in notes[0].message


def test_blind_via_forbidden(tmp_path, profile):
    pcb = _board(tmp_path, '(via blind (at 10 10) (size 0.45) (drill 0.3) '
                           '(layers "F.Cu" "In1.Cu") (net 1))')
    assert "FAB_VIA_TYPE_FORBIDDEN" in _codes(fab.check(pcb, profile))


_QFN = """(footprint "QFN" (layer "F.Cu") (at 10 10 0)
    (property "Reference" "%s" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "PMIC" (at 0 0 0) (layer "F.Fab"))
    (pad "EP" smd rect (at 0 0) (size 3 3) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "GND")))
  (via (at 10.5 10.5) (size 0.45) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))"""


def test_via_in_pad_fails_and_names_the_pad(tmp_path, profile):
    pcb = _board(tmp_path, _QFN % "U1")
    findings = fab.check(pcb, profile)
    hits = [f for f in findings if f.code == "FAB_VIA_IN_PAD"]
    assert len(hits) == 1 and "U1.EP" in hits[0].message


def test_registered_thermal_exception_passes_as_note(tmp_path, profile):
    pcb = _board(tmp_path, _QFN % "U9")          # U9 has the exception
    findings = fab.check(pcb, profile)
    assert "FAB_VIA_IN_PAD" not in _codes(findings)
    hits = [f for f in findings if f.code == "FAB_VIA_IN_PAD_EXCEPTION"]
    assert len(hits) == 1 and "hw-lead" in hits[0].message


def test_expired_exception_fails(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text(_PROFILE.replace('expires = "2099-01-01"',
                                  'expires = "2020-01-01"'))
    profile = fab.load_profile(p)
    pcb = _board(tmp_path, _QFN % "U9")
    assert "FAB_EXCEPTION_EXPIRED" in _codes(fab.check(pcb, profile))


def test_stackup_drift_fails(tmp_path, profile):
    src = (_BOARD_HEAD + ")").replace('(thickness 1.0)', '(thickness 1.6)')
    p = tmp_path / "b.kicad_pcb"
    p.write_text(src)
    findings = fab.check(kicad.read_pcb(str(p)), profile)
    assert "FAB_STACKUP_MISMATCH" in _codes(findings)


def test_fine_trace_cost_warning(tmp_path, profile):
    pcb = _board(tmp_path, '(segment (start 1 1) (end 2 1) (width 0.08) '
                           '(layer "In1.Cu") (net 1))')
    assert "FAB_COST_TRACE_WIDTH" in _codes(fab.check(pcb, profile))


def test_profile_requires_sources(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('id = "x"\n')
    with pytest.raises(AkcliError) as ei:
        fab.load_profile(p)
    assert ei.value.code == "BAD_CONFIG"


# --------------------------------------------------------------------------- #
# order manifest
# --------------------------------------------------------------------------- #
_ORDER = """
delivery_format = "single"
design_count = 1
rush = false
surface_finish = "HASL_LF"
via_covering = "tented"
board_material = "FR4"
copper_weight_oz = 1
thickness_mm = 1.0
"""


def test_order_complete_and_consistent(tmp_path, profile):
    p = tmp_path / "order.toml"
    p.write_text(_ORDER)
    assert fab.check_order(fab.load_order(p), profile) == []


def test_order_missing_fields_is_an_error(tmp_path, profile):
    p = tmp_path / "order.toml"
    p.write_text('delivery_format = "single"\n')
    findings = fab.check_order(fab.load_order(p), profile)
    assert any(f.code == "ORDER_INCOMPLETE" and f.severity.value == "error"
               for f in findings)


def test_order_enig_and_panel_require_review(tmp_path, profile):
    p = tmp_path / "order.toml"
    p.write_text(_ORDER.replace('"HASL_LF"', '"ENIG"')
                 .replace('"single"', '"panel"'))
    findings = fab.check_order(fab.load_order(p), profile)
    reviews = [f for f in findings if f.code == "ORDER_REVIEW_REQUIRED"]
    assert len(reviews) == 2


def test_order_profile_conflict(tmp_path, profile):
    p = tmp_path / "order.toml"
    p.write_text(_ORDER.replace('"tented"', '"plugged"'))
    findings = fab.check_order(fab.load_order(p), profile)
    assert any(f.code == "ORDER_PROFILE_CONFLICT" for f in findings)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_fab_check_json(tmp_path, capsys):
    (tmp_path / "profile.toml").write_text(_PROFILE)
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_BOARD_HEAD + '(via (at 10 10) (size 0.4) (drill 0.2) '
                                   '(layers "F.Cu" "B.Cu") (net 1))\n)')
    code = main(["fab", "check", str(board), "--profile",
                 str(tmp_path / "profile.toml"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    assert doc["profile"]["id"] == "jlc-4l-1oz-free-vias-2026-07"
    assert any(f["code"] == "FAB_VIA_PAID_PROCESS" for f in doc["findings"])


def test_cli_fab_explain_cites_sources(tmp_path, capsys):
    (tmp_path / "profile.toml").write_text(_PROFILE)
    code = main(["fab", "explain", "FAB_VIA_IN_PAD",
                 "--profile", str(tmp_path / "profile.toml")])
    out = capsys.readouterr().out
    assert code == 0
    assert "thermal_via" in out
    assert "jlcpcb.com" in out


# --------------------------------------------------------------------------- #
# `fab explain` completeness gate
# --------------------------------------------------------------------------- #
def _emitted_fab_codes() -> set[str]:
    """Every FAB_*/ORDER_* code ``fab.py`` constructs a Finding with.

    An AST scan, not a run: a code behind a branch no fixture happens to take
    is still a code a user can be handed, and must be explainable.
    """
    import ast
    import re
    from pathlib import Path

    code_rx = re.compile(r"^(FAB|ORDER)_[A-Z_]+$")
    tree = ast.parse(Path(fab.__file__).read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "Finding"):
            continue
        arg = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "code"), None)
        # A computed code would be invisible to this gate — keep them literal.
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"fab.py:{node.lineno}: Finding code must be a string literal")
        assert code_rx.match(arg.value), (
            f"fab.py:{node.lineno}: unexpected finding code {arg.value!r}")
        codes.add(arg.value)
    return codes


def test_every_emitted_fab_code_has_an_explain_row():
    emitted = _emitted_fab_codes()
    assert len(emitted) >= 18                       # the scan found the module
    missing = sorted(emitted - set(fab._EXPLAIN))
    assert not missing, (
        f"fab.py emits {missing} but `akcli fab explain` has no row for it — "
        "add one to fab._EXPLAIN (rule + fix direction)")
    orphans = sorted(set(fab._EXPLAIN) - emitted)
    assert not orphans, (
        f"fab._EXPLAIN explains {orphans}, which fab.py never emits — "
        "remove the row or restore the finding")


def test_every_explain_row_renders(capsys):
    for code, (section, text) in fab._EXPLAIN.items():
        assert section and text.strip().endswith("."), code
        rendered = fab.explain(code)
        assert rendered is not None and rendered.splitlines()[0] == code
        assert f"[{section}]" in rendered
    # through the CLI, for a code the table used to lack
    assert main(["fab", "explain", "FAB_NO_OUTLINE"]) == 0
    out = capsys.readouterr().out
    assert "Edge.Cuts" in out and "NOT evaluated" in out
    assert main(["fab", "explain", "FAB_NOT_A_CODE"]) == 2
    assert "unknown finding code" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# tenting: the cap binds vias KNOWN to be tented, or all of them when the
# order manifest says so
# --------------------------------------------------------------------------- #
_VIA_TENTING = {
    "tented": " (tenting (front yes) (back yes))",
    "untented": " (tenting (front no) (back no))",
    "unknown": "",                  # the test board carries no tenting at all
}


@pytest.mark.parametrize("board_says", ["tented", "untented", "unknown"])
@pytest.mark.parametrize("order_says", ["tented", "untented", None])
def test_tenting_matrix(tmp_path, profile, board_says, order_says):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.6) (drill 0.45) '
                           '(layers "F.Cu" "B.Cu") (net 1)'
                           f'{_VIA_TENTING[board_says]})')
    order = None if order_says is None else {"via_covering": order_says}
    got = sorted((f.code, f.severity.value) for f in fab.check(pcb, profile, order)
                 if "TENT" in f.code)

    if board_says == "tented" or order_says == "tented":
        assert got == [("FAB_VIA_TENTED_TOO_BIG", "error")]
    elif board_says == "unknown" and order_says is None:
        assert got == [("FAB_TENTING_UNKNOWN", "note")]
    else:
        # known untented, or the order declares a covering that is not tented
        assert got == []


def test_tenting_basis_is_named_in_the_message(tmp_path, profile):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.6) (drill 0.45) '
                           '(layers "F.Cu" "B.Cu") (net 1) '
                           '(tenting (front no) (back no)))')
    (hit,) = [f for f in fab.check(pcb, profile, {"via_covering": "tented"})
              if f.code == "FAB_VIA_TENTED_TOO_BIG"]
    assert 'the order declares via_covering "tented"' in hit.message


def test_many_unknown_vias_still_raise_one_note(tmp_path, profile):
    vias = "\n".join(
        f'(via (at {x} 10) (size 0.7) (drill {d}) (layers "F.Cu" "B.Cu") (net 1))'
        for x, d in ((5, 0.45), (10, 0.6), (15, 0.5), (20, 0.4)))
    findings = fab.check(_board(tmp_path, vias), profile)
    (note,) = [f for f in findings if f.code == "FAB_TENTING_UNKNOWN"]
    assert note.message.startswith("3 via(s)")            # 0.4 is ON the cap
    assert "largest 0.6mm at (10,10)" in note.message
    assert note.pos == (10.0, 10.0)


def test_unknown_tenting_is_silent_when_no_via_exceeds_the_cap(tmp_path, profile):
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.45) (drill 0.3) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    assert "FAB_TENTING_UNKNOWN" not in _codes(fab.check(pcb, profile))


def test_one_tented_side_is_enough_and_only_reached_sides_count(tmp_path, profile):
    body = """
  (via (at 5 10) (size 0.7) (drill 0.5) (layers "F.Cu" "B.Cu") (net 1)
    (tenting (front yes) (back no)))
  (via blind (at 10 10) (size 0.7) (drill 0.5) (layers "In2.Cu" "B.Cu") (net 1)
    (tenting (front yes) (back no)))
  (via buried (at 15 10) (size 0.7) (drill 0.5) (layers "In1.Cu" "In2.Cu") (net 1)
    (tenting (front yes) (back yes)))"""
    permissive = {**profile, "via": {**profile["via"],
                                     "forbid_blind_buried": False}}
    pcb = _board(tmp_path, body)
    findings, facts = fab.evaluate(pcb, permissive, {"via_covering": "tented"})
    hits = sorted(f.pos for f in findings if f.code == "FAB_VIA_TENTED_TOO_BIG")
    # the through via (front tented) and — by the order — the blind via; the
    # buried via reaches no outer layer, so there is nothing to tent
    assert hits == [(5.0, 10.0), (10.0, 10.0)]
    assert facts["tenting"]["vias"] == {
        "tented": 1, "untented": 1, "unknown": 0, "not_exposed": 1}

    # without the order only the board-tented one is flagged: the blind via's
    # one reached side (back) is open
    hits = [f.pos for f in fab.check(pcb, permissive)
            if f.code == "FAB_VIA_TENTED_TOO_BIG"]
    assert hits == [(5.0, 10.0)]


def test_tenting_read_from_a_real_kicad10_board(profile):
    """``fab/vias_tenting.kicad_pcb``: board tenting front yes / back no, two
    0.5 mm vias overriding it (one open, one tented) and a 0.5 mm blind via
    from the back, which inherits "back no"."""
    pcb = kicad.read_pcb(str(FAB_FIX / "vias_tenting.kicad_pcb"))
    permissive = {**profile, "stackup": {},
                  "via": {**profile["via"], "forbid_blind_buried": False}}
    findings, facts = fab.evaluate(pcb, permissive)
    assert [f.pos for f in findings if f.code == "FAB_VIA_TENTED_TOO_BIG"] == [
        (11.0, 5.0)]
    assert "FAB_TENTING_UNKNOWN" not in _codes(findings)
    assert facts["tenting"]["board"] == {
        "front": True, "back": False, "source": "setup.tenting"}
    assert facts["tenting"]["vias"] == {
        "tented": 4, "untented": 2, "unknown": 0, "not_exposed": 2}

    by_order = [f.pos for f in fab.check(pcb, permissive,
                                         {"via_covering": "tented"})
                if f.code == "FAB_VIA_TENTED_TOO_BIG"]
    assert sorted(by_order) == [(8.0, 5.0), (11.0, 5.0), (26.0, 5.0)]


# --------------------------------------------------------------------------- #
# via type — buried included
# --------------------------------------------------------------------------- #
def test_buried_vias_are_forbidden_too(profile):
    pcb = kicad.read_pcb(str(FAB_FIX / "vias_tenting.kicad_pcb"))
    findings, facts = fab.evaluate(pcb, profile)
    hits = {f.pos[0]: f.message for f in findings
            if f.code == "FAB_VIA_TYPE_FORBIDDEN"}
    assert sorted(hits) == [14.0, 17.0, 20.0, 23.0, 26.0]
    assert "blind via (F.Cu-In1.Cu)" in hits[14.0]
    assert "buried via (In1.Cu-In2.Cu)" in hits[17.0]     # KiCad 10 token
    assert "buried via (In1.Cu-In2.Cu)" in hits[20.0]     # `blind`, inner pair
    assert "micro via" in hits[23.0]
    assert facts["via_types"] == {"through": 3, "blind": 2, "buried": 2,
                                  "micro": 1}


def test_legacy_blind_token_on_an_inner_pair_is_buried(tmp_path, profile):
    pcb = _board(tmp_path, '(via blind (at 10 10) (size 0.45) (drill 0.3) '
                           '(layers "In1.Cu" "In2.Cu") (net 1))')
    (hit,) = [f for f in fab.check(pcb, profile)
              if f.code == "FAB_VIA_TYPE_FORBIDDEN"]
    assert "buried via" in hit.message


# --------------------------------------------------------------------------- #
# outline: true area, cutouts, and the bbox fallback that says what it is
# --------------------------------------------------------------------------- #
def _with_cost(profile: dict, table: str = "warn_if", **keys) -> dict:
    return {**profile, "stackup": {}, "cost": {table: keys}}


def test_area_is_the_true_outline_not_the_bounding_box(profile):
    """40 x 30 mm rounded board with three cutouts: 11.44 cm² of board inside
    a 12.00 cm² bounding box. A threshold between the two only trips if the
    bbox is (wrongly) used."""
    pcb = kicad.read_pcb(str(FAB_FIX / "outline_cutouts.kicad_pcb"))
    findings, facts = fab.evaluate(
        pcb, _with_cost(profile, board_area_cm2_gt=11.7))
    assert "FAB_COST_BOARD_AREA" not in _codes(findings)
    assert facts["outline"]["method"] == "outline"
    assert facts["outline"]["closed"] is True
    assert facts["outline"]["area_mm2"] == pytest.approx(1144.292, abs=1e-3)
    assert facts["outline"]["cutouts"] == 3

    (hit,) = [f for f in fab.check(pcb, _with_cost(profile,
                                                   board_area_cm2_gt=11.0))
              if f.code == "FAB_COST_BOARD_AREA"]
    assert hit.severity.value == "warning"
    assert "board area 11.4cm²" in hit.message
    assert "Edge.Cuts outline, 3 cutout(s) subtracted" in hit.message
    assert "bbox" not in hit.message and "bounding" not in hit.message


def test_unclosed_outline_falls_back_to_the_bbox_and_says_so(profile):
    pcb = kicad.read_pcb(str(FAB_FIX / "outline_open.kicad_pcb"))
    findings, facts = fab.evaluate(
        pcb, _with_cost(profile, board_area_cm2_gt=11.7))
    (hit,) = [f for f in findings if f.code == "FAB_COST_BOARD_AREA"]
    assert "board bbox area 12.0cm²" in hit.message
    assert "bounding-box approximation" in hit.message
    assert "did not close" in hit.message
    assert "2 segment end(s) meet nothing" in hit.message      # ...and why
    assert facts["outline"] == {
        "method": "bbox", "closed": False, "area_mm2": 1200.0,
        "bbox": ((10.0, 10.0), (50.0, 40.0)), "cutouts": 0,
        "problems": ["2 segment end(s) meet nothing within 0.01 mm"]}
    assert "FAB_NO_OUTLINE" not in _codes(findings)


def test_a_circular_board_has_an_outline_now(profile):
    # gr_circle only: before, this was FAB_NO_OUTLINE
    pcb = kicad.read_pcb(str(FAB_FIX / "outline_circle.kicad_pcb"))
    findings, facts = fab.evaluate(
        pcb, _with_cost(profile, board_area_cm2_gt=7.0,
                        board_length_mm_gte=30.0))
    codes = _codes(findings)
    assert "FAB_NO_OUTLINE" not in codes
    assert {"FAB_COST_BOARD_AREA", "FAB_COST_BOARD_LENGTH"} <= codes
    assert facts["outline"]["area_mm2"] == pytest.approx(706.858, abs=1e-3)


def test_drill_density_uses_the_true_area(tmp_path, profile):
    """A 10 x 10 mm frame with an 8 x 8 mm window: 0.36 cm² of board. Four
    vias are 111k/m² over the board but only 40k/m² over its bounding box."""
    body = """
  (gr_rect (start 100 100) (end 110 110) (layer "Edge.Cuts"))
  (gr_rect (start 101 101) (end 109 109) (layer "Edge.Cuts"))
""" + "\n".join(f'  (via (at {x} 100.5) (size 0.6) (drill 0.3) '
                 '(layers "F.Cu" "B.Cu") (net 1))' for x in (102, 104, 106, 108))
    p = tmp_path / "frame.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108)\n  (layers (0 "F.Cu" signal) '
                 '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n  (net 0 "")\n'
                 '  (net 1 "GND")' + body + "\n)", encoding="utf-8", newline="\n")
    pcb = kicad.read_pcb(str(p))
    (hit,) = [f for f in fab.check(pcb, _with_cost(
        profile, drill_density_per_m2_gt=100000))
        if f.code == "FAB_COST_DRILL_DENSITY"]
    assert "111,111/m²" in hit.message
    assert "4 holes over 0.4cm²" in hit.message
    assert "1 cutout(s) subtracted" in hit.message


def test_no_outline_at_all_is_still_a_warning(tmp_path, profile):
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108)\n  (layers (0 "F.Cu" signal) '
                 '(31 "B.Cu" signal))\n  (net 0 "")\n)', encoding="utf-8",
                 newline="\n")
    pcb = kicad.read_pcb(str(p))
    for table in ("warn_if", "error_if"):
        findings, facts = fab.evaluate(
            pcb, _with_cost(profile, table, board_area_cm2_gt=1))
        (hit,) = findings
        assert (hit.code, hit.severity.value) == ("FAB_NO_OUTLINE", "warning")
        assert facts["outline"]["method"] == "none"
    # nothing area-dependent configured: nothing to say
    assert fab.check(pcb, _with_cost(profile, trace_width_mm_lte=0.09)) == []


# --------------------------------------------------------------------------- #
# [cost.error_if] — the same finding, promoted
# --------------------------------------------------------------------------- #
def _cutouts_board():
    return kicad.read_pcb(str(FAB_FIX / "outline_cutouts.kicad_pcb"))


def test_error_if_promotes_the_same_code_to_error(profile):
    pcb = _cutouts_board()                    # 11.44 cm², 40 mm long
    base = {**profile, "stackup": {}}

    warn_only = {**base, "cost": {"warn_if": {"board_area_cm2_gt": 5}}}
    (hit,) = fab.check(pcb, warn_only)
    assert (hit.code, hit.severity.value) == ("FAB_COST_BOARD_AREA", "warning")
    assert "vendor surcharge threshold" in hit.message

    both = {**base, "cost": {"warn_if": {"board_area_cm2_gt": 5},
                             "error_if": {"board_area_cm2_gt": 10}}}
    (hit,) = fab.check(pcb, both)             # ONE finding, not two
    assert (hit.code, hit.severity.value) == ("FAB_COST_BOARD_AREA", "error")
    assert "> 10cm²" in hit.message and "[cost.error_if]" in hit.message

    # the error threshold not reached: still the warning
    below = {**base, "cost": {"warn_if": {"board_area_cm2_gt": 5},
                              "error_if": {"board_area_cm2_gt": 20}}}
    (hit,) = fab.check(pcb, below)
    assert (hit.code, hit.severity.value) == ("FAB_COST_BOARD_AREA", "warning")

    # error_if alone works without a warn_if table
    alone = {**base, "cost": {"error_if": {"board_length_mm_gte": 40}}}
    (hit,) = fab.check(pcb, alone)
    assert (hit.code, hit.severity.value) == ("FAB_COST_BOARD_LENGTH", "error")


def test_error_if_covers_every_cost_key(tmp_path, profile):
    body = "\n".join([
        '(gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts"))',
        '(via (at 5 5) (size 0.45) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))',
        '(segment (start 1 1) (end 2 1) (width 0.08) (layer "In1.Cu") (net 1))',
    ])
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108)\n  (net 0 "")\n  (net 1 "GND")\n'
                 + body + "\n)", encoding="utf-8", newline="\n")
    pcb = kicad.read_pcb(str(p))
    keys = {"board_length_mm_gte": 10, "board_area_cm2_gt": 0.5,
            "drill_density_per_m2_gt": 5000, "trace_width_mm_lte": 0.09}
    expected = {"FAB_COST_BOARD_LENGTH", "FAB_COST_BOARD_AREA",
                "FAB_COST_DRILL_DENSITY", "FAB_COST_TRACE_WIDTH"}
    for table, severity in (("warn_if", "warning"), ("error_if", "error")):
        got = {(f.code, f.severity.value)
               for f in fab.check(pcb, _with_cost(profile, table, **keys))
               if f.code.startswith("FAB_COST_")}
        assert got == {(code, severity) for code in expected}


@pytest.mark.parametrize("extra, needle", [
    ("[cost.error_if]\nboard_area_cm2_gte = 5\n", "unknown key(s) in [cost.error_if]"),
    ("[cost.error_if]\nboard_area_cm2_gt = \"big\"\n", "must be a non-negative number"),
    ("[cost.error_if]\nboard_area_cm2_gt = true\n", "must be a non-negative number"),
    ("[cost.error_if]\nboard_area_cm2_gt = -1\n", "must be a non-negative number"),
    ("[cost.error_if]\nboard_area_cm2_gt = nan\n", "must be a non-negative number"),
    ("[cost]\nerror_if = 3\n", "[cost.error_if] must be a table"),
    ("[slot]\nmin_widht_mm = 0.5\n", "unknown key(s) in [slot]"),
    ("[rules]\nmin_clearance = 0.1\n", "unknown key(s) in [rules]"),
])
def test_new_profile_tables_are_validated_on_load(tmp_path, extra, needle):
    head = 'id = "x"\n[source]\nurls = ["https://example.com"]\n'
    p = tmp_path / "p.toml"
    p.write_text(head + extra, encoding="utf-8", newline="\n")
    with pytest.raises(AkcliError) as ei:
        fab.load_profile(p)
    assert ei.value.code == "BAD_CONFIG"
    assert needle in ei.value.message


def test_valid_new_profile_tables_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(
        'id = "x"\n[source]\nurls = ["https://example.com"]\n'
        '[cost.error_if]\nboard_length_mm_gte = 600\ntrace_width_mm_lte = 0.07\n'
        '[slot]\nmin_width_mm = 0.5\nmin_plated_width_mm = 0.35\n'
        'min_non_plated_width_mm = 1\n'
        '[rules]\nmin_clearance_mm = 0.1\nmin_hole_to_hole_mm = 0.45\n',
        encoding="utf-8", newline="\n")
    doc = fab.load_profile(p)
    assert doc["cost"]["error_if"]["board_length_mm_gte"] == 600
    assert doc["slot"]["min_non_plated_width_mm"] == 1


# --------------------------------------------------------------------------- #
# slots
# --------------------------------------------------------------------------- #
def _slot_findings(slot: dict) -> list[tuple[str, str]]:
    pcb = kicad.read_pcb(str(FAB_FIX / "slots.kicad_pcb"))
    profile = {"id": "slot-test", "slot": slot}
    return sorted((f.message.split(":")[0], f.severity.value)
                  for f in fab.check(pcb, profile)
                  if f.code == "FAB_SLOT_BELOW_MIN")


def test_slot_below_minimum_by_plating():
    """``fab/slots.kicad_pcb`` (drill sizes as KiCad's own statistics list
    them): plated 0.8x2.2, 0.3x1.0, 2.2x0.8; NPTH 0.6x2.5, 1.0x2.5; plus a
    0.9x0.9 "oval" and a round 0.8 that are not slots."""
    assert _slot_findings({"min_plated_width_mm": 0.35,
                           "min_non_plated_width_mm": 1.0}) == [
        ("J1 (unnumbered pad)", "error"),        # NPTH 0.6 < 1.0
        ("J1.2", "error"),                       # plated 0.3 < 0.35
    ]
    # exactly ON the minimum passes (NPTH 1.0, plated 0.8)
    assert _slot_findings({"min_plated_width_mm": 0.8}) == [("J1.2", "error")]


def test_slot_generic_minimum_and_specific_override():
    # min_width_mm applies to both kinds...
    assert _slot_findings({"min_width_mm": 0.7}) == [
        ("J1 (unnumbered pad)", "error"), ("J1.2", "error")]
    # ...unless the specific key is present
    assert _slot_findings({"min_width_mm": 0.7,
                           "min_non_plated_width_mm": 0.5}) == [
        ("J1.2", "error")]
    # the width is the SMALLER drill size whichever axis it is on (pad 4)
    assert ("J1.4", "error") in _slot_findings({"min_plated_width_mm": 0.85})
    # an "oval" with equal sizes is a round hole, never a slot (pad 3: 0.9)
    assert ("J1.3", "error") not in _slot_findings({"min_width_mm": 5.0})


def test_slot_message_names_the_rule(profile):
    pcb = kicad.read_pcb(str(FAB_FIX / "slots.kicad_pcb"))
    (hit,) = [f for f in fab.check(pcb, {"id": "p", "slot": {
        "min_plated_width_mm": 0.35}}) if f.code == "FAB_SLOT_BELOW_MIN"]
    assert hit.message == (
        "J1.2: plated slot 0.3x1mm is narrower than the profile minimum "
        "0.35mm ([slot] min_plated_width_mm)")
    assert hit.pos == (10.0, 10.0)
    # no [slot] table: the rule does not exist
    assert "FAB_SLOT_BELOW_MIN" not in _codes(fab.check(pcb, profile))


# --------------------------------------------------------------------------- #
# project design rules vs the profile's minimums
# --------------------------------------------------------------------------- #
_RULES_PRO = FAB_FIX / "rules" / "rules_demo.kicad_pro"


def test_project_rules_looser_than_the_profile_warn_once(tmp_path, profile):
    pcb = _board(tmp_path, "")
    rules = {"min_via_diameter": 0.3, "min_through_hole_diameter": 0.2,
             "min_clearance": 0.0}
    findings, facts = fab.evaluate(pcb, profile, rules=rules)
    (hit,) = [f for f in findings if f.code == "FAB_RULES_LOOSER_THAN_PROFILE"]
    assert hit.severity.value == "warning"
    assert "min_via_diameter 0.3mm < 0.4mm (via.min_pad_mm)" in hit.message
    assert ("min_through_hole_diameter 0.2mm < 0.3mm (via.min_drill_mm)"
            in hit.message)
    assert "min_clearance" not in hit.message      # the profile has no floor
    assert facts["project_rules"] == [
        {"rule": "min_via_diameter", "project_mm": 0.3,
         "profile_key": "via.min_pad_mm", "profile_min_mm": 0.4,
         "looser": True},
        {"rule": "min_through_hole_diameter", "project_mm": 0.2,
         "profile_key": "via.min_drill_mm", "profile_min_mm": 0.3,
         "looser": True},
    ]


def test_project_rules_at_or_above_the_minimums_are_quiet(tmp_path, profile):
    pcb = _board(tmp_path, "")
    rules = fab.read_project_rules(_RULES_PRO).rules     # 0.4 via / 0.3 hole
    findings, facts = fab.evaluate(pcb, profile, rules=rules)
    assert "FAB_RULES_LOOSER_THAN_PROFILE" not in _codes(findings)
    assert [(r["rule"], r["project_mm"], r["looser"])
            for r in facts["project_rules"]] == [
        ("min_via_diameter", 0.4, False),
        ("min_through_hole_diameter", 0.3, False)]


def test_explicit_rules_table_extends_and_overrides_the_via_minimums(
        tmp_path, profile):
    pcb = _board(tmp_path, "")
    strict = {**profile, "rules": {"min_clearance_mm": 0.1,
                                   "min_track_width_mm": 0.127,
                                   "min_via_diameter_mm": 0.45,
                                   "min_hole_to_hole_mm": 0.2}}
    rules = fab.read_project_rules(_RULES_PRO).rules
    findings, facts = fab.evaluate(pcb, strict, rules=rules)
    (hit,) = [f for f in findings if f.code == "FAB_RULES_LOOSER_THAN_PROFILE"]
    assert "min_clearance 0mm < 0.1mm (rules.min_clearance_mm)" in hit.message
    # [rules] wins over the [via] stand-in for the same rule
    assert ("min_via_diameter 0.4mm < 0.45mm (rules.min_via_diameter_mm)"
            in hit.message)
    assert "min_track_width" not in hit.message          # 0.2 >= 0.127
    assert "min_hole_to_hole" not in hit.message         # 0.25 >= 0.2
    assert [r["rule"] for r in facts["project_rules"]] == [
        "min_clearance", "min_track_width", "min_via_diameter",
        "min_through_hole_diameter", "min_hole_to_hole"]


def test_a_rule_the_project_does_not_state_is_not_compared(tmp_path, profile):
    pcb = _board(tmp_path, "")
    findings, facts = fab.evaluate(pcb, profile, rules={})     # "rules": {}
    assert "FAB_RULES_LOOSER_THAN_PROFILE" not in _codes(findings)
    assert [(r["project_mm"], r["looser"]) for r in facts["project_rules"]] == [
        (None, False), (None, False)]
    # and with no project rules at all the comparison did not run
    assert fab.evaluate(pcb, profile)[1]["project_rules"] is None


def test_find_project_prefers_the_boards_own_stem(tmp_path):
    board = tmp_path / "main.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8", newline="\n")
    assert fab.find_project(board) is None
    assert fab.read_project_rules(None) == fab.ProjectRules(None, None, None)

    other = tmp_path / "aaa.kicad_pro"
    shutil.copyfile(_RULES_PRO, other)
    assert fab.find_project(board) == other          # the directory's project
    own = tmp_path / "main.kicad_pro"
    shutil.copyfile(_RULES_PRO, own)
    assert fab.find_project(board) == own            # same stem wins

    got = fab.read_project_rules(own)
    assert got.path == own.as_posix() and got.error is None
    assert got.rules["min_via_diameter"] == 0.4

    own.write_text("{ not json", encoding="utf-8", newline="\n")
    bad = fab.read_project_rules(own)
    assert bad.rules is None and "not valid JSON" in bad.error


# --------------------------------------------------------------------------- #
# CLI: facts in --json, --project, the no-project skip
# --------------------------------------------------------------------------- #
def _cli_project(tmp_path, *, board: str = "vias_tenting", pro: bool = True):
    shutil.copyfile(FAB_FIX / f"{board}.kicad_pcb", tmp_path / "b.kicad_pcb")
    (tmp_path / "profile.toml").write_text(_PROFILE, encoding="utf-8",
                                           newline="\n")
    if pro:
        doc = json.loads(_RULES_PRO.read_text(encoding="utf-8"))
        doc["board"]["design_settings"]["rules"]["min_via_diameter"] = 0.3
        (tmp_path / "b.kicad_pro").write_text(json.dumps(doc), encoding="utf-8",
                                              newline="\n")
    return [str(tmp_path / "b.kicad_pcb"), "--profile",
            str(tmp_path / "profile.toml")]


def test_cli_json_reports_the_facts(tmp_path):
    argv = _cli_project(tmp_path)
    doc = json_of(["fab", "check", *argv, "--json"], expect=1)
    assert doc["schema_version"]                       # still version-stamped
    assert doc["vias"] == 8                            # pre-existing key kept
    assert doc["via_types"] == {"through": 3, "blind": 2, "buried": 2,
                                "micro": 1}
    assert doc["outline"] == {
        "method": "outline", "closed": True, "area_mm2": 600.0,
        "bbox": [[0.0, 0.0], [30.0, 20.0]], "cutouts": 0, "problems": []}
    assert doc["tenting"] == {
        "board": {"front": True, "back": False, "source": "setup.tenting"},
        "vias": {"tented": 4, "untented": 2, "unknown": 0, "not_exposed": 2},
        "order_via_covering": None,
        "cap_applies_to": "vias the board file marks tented",
    }
    project = doc["project"]
    assert project["path"] == (tmp_path / "b.kicad_pro").as_posix()
    assert project["status"] == "read" and project["error"] is None
    assert {r["rule"]: r["looser"] for r in project["rules"]} == {
        "min_via_diameter": True, "min_through_hole_diameter": False}
    assert any(f["code"] == "FAB_RULES_LOOSER_THAN_PROFILE"
               and f["severity"] == "warning" for f in doc["findings"])


def test_cli_order_decides_the_tenting_cap(tmp_path):
    argv = _cli_project(tmp_path, pro=False)
    (tmp_path / "order.toml").write_text(_ORDER, encoding="utf-8", newline="\n")
    doc = json_of(["fab", "check", *argv, "--order",
                   str(tmp_path / "order.toml"), "--json"], expect=1)
    assert doc["tenting"]["order_via_covering"] == "tented"
    assert doc["tenting"]["cap_applies_to"].startswith("every via reaching")
    hits = [f for f in doc["findings"] if f["code"] == "FAB_VIA_TENTED_TOO_BIG"]
    assert len(hits) == 3


def test_cli_no_project_file_is_skipped_silently_in_text(tmp_path):
    argv = _cli_project(tmp_path, pro=False)
    rc, text, err = run_cli(["fab", "check", *argv])
    assert rc == 1 and err == ""
    assert "project" not in text                       # nothing to say
    assert "FAB_RULES_LOOSER_THAN_PROFILE" not in text
    assert "outline: 6.00 cm² (Edge.Cuts closed)" in text
    assert "via types: through 3, blind 2, buried 2, micro 1" in text
    assert "tenting: front tented, back open (setup.tenting)" in text

    doc = json_of(["fab", "check", *argv, "--json"], expect=1)   # ...but JSON says
    assert doc["project"] == {"path": None, "status": "absent", "error": None,
                              "rules": None}


def test_cli_project_flag_overrides_discovery(tmp_path):
    argv = _cli_project(tmp_path)                      # b.kicad_pro is loose
    strict = tmp_path / "elsewhere" / "strict.kicad_pro"
    strict.parent.mkdir()
    shutil.copyfile(_RULES_PRO, strict)                # 0.4: not looser
    doc = json_of(["fab", "check", *argv, "--project", str(strict), "--json"],
                  expect=1)
    assert doc["project"]["path"] == strict.as_posix()
    assert not any(r["looser"] for r in doc["project"]["rules"])
    assert not any(f["code"] == "FAB_RULES_LOOSER_THAN_PROFILE"
                   for f in doc["findings"])

    rc, text, _err = run_cli(["fab", "check", *argv])  # text mode, discovered
    assert "project rules: " in text and "b.kicad_pro" in text
    assert "2 of 2 profile minimum(s) compared" in text
    assert "WARNING  FAB_RULES_LOOSER_THAN_PROFILE" in text


def test_cli_an_unreadable_discovered_project_is_stated_not_fatal(tmp_path):
    argv = _cli_project(tmp_path, pro=False)
    (tmp_path / "b.kicad_pro").write_text("{ nope", encoding="utf-8",
                                          newline="\n")
    doc = json_of(["fab", "check", *argv, "--json"], expect=1)
    assert doc["project"]["status"] == "unreadable"
    assert "not valid JSON" in doc["project"]["error"]
    assert doc["project"]["rules"] is None
    rc, text, _err = run_cli(["fab", "check", *argv])
    assert "project rules: NOT compared" in text


def test_cli_a_named_project_that_cannot_be_used_is_an_error(tmp_path):
    argv = _cli_project(tmp_path, pro=False)
    rc, out, err = run_cli(["fab", "check", *argv, "--project",
                            str(tmp_path / "missing.kicad_pro")])
    assert rc != 0 and out == "" and "no such project file" in err

    bad = tmp_path / "bad.kicad_pro"
    bad.write_text("[]", encoding="utf-8", newline="\n")
    rc, out, err = run_cli(["fab", "check", *argv, "--project", str(bad)])
    assert rc == 2 and out == ""
    assert "BAD_CONFIG" in err and "bad.kicad_pro" in err


def test_cli_text_states_a_bbox_fallback_even_with_no_finding(tmp_path):
    shutil.copyfile(FAB_FIX / "outline_open.kicad_pcb", tmp_path / "b.kicad_pcb")
    p = tmp_path / "p.toml"
    p.write_text('id = "x"\n[source]\nurls = ["https://example.com"]\n'
                 '[cost.warn_if]\nboard_area_cm2_gt = 650\n',
                 encoding="utf-8", newline="\n")
    rc, text, _err = run_cli(["fab", "check", str(tmp_path / "b.kicad_pcb"),
                              "--profile", str(p)])
    assert rc == 0 and "no findings" in text
    assert "outline: 12.00 cm² is the BOUNDING BOX" in text
    assert "Edge.Cuts did not close" in text


def test_release_preflight_fab_gate_sees_the_order_and_project_rules(tmp_path):
    """The `fab` gate of `release preflight` runs the same check: the order's
    via_covering reaches it, and so do the rules of the project file the
    release already hashes."""
    kicad_fix = FAB_FIX.parent
    shutil.copyfile(kicad_fix / "board_v8.kicad_sch", tmp_path / "x.kicad_sch")
    (tmp_path / "x.kicad_pcb").write_text(
        _BOARD_HEAD + '(via (at 10 10) (size 0.6) (drill 0.45) '
        '(layers "F.Cu" "B.Cu") (net 1))\n)', encoding="utf-8", newline="\n")
    (tmp_path / "profile.toml").write_text(_PROFILE, encoding="utf-8",
                                           newline="\n")
    (tmp_path / "order.toml").write_text(_ORDER, encoding="utf-8", newline="\n")
    argv = ["release", "preflight", "--sch", str(tmp_path / "x.kicad_sch"),
            "--pcb", str(tmp_path / "x.kicad_pcb"),
            "--fab-profile", str(tmp_path / "profile.toml"), "--json"]

    def fab_gate(extra: list[str]) -> list[tuple[str, str]]:
        doc = json_of([*argv, *extra])
        gate = next(g for g in doc["gates"] if g["gate"] == "fab")
        return [(f["code"], f["severity"]) for f in gate["findings"]]

    assert fab_gate([]) == [("FAB_TENTING_UNKNOWN", "note")]
    assert fab_gate(["--order", str(tmp_path / "order.toml")]) == [
        ("FAB_VIA_TENTED_TOO_BIG", "error")]

    doc = json.loads(_RULES_PRO.read_text(encoding="utf-8"))
    doc["board"]["design_settings"]["rules"]["min_through_hole_diameter"] = 0.2
    (tmp_path / "x.kicad_pro").write_text(json.dumps(doc), encoding="utf-8",
                                          newline="\n")
    assert ("FAB_RULES_LOOSER_THAN_PROFILE", "warning") in fab_gate([])


# --------------------------------------------------------------------------- #
# what is NOT computed is said plainly
# --------------------------------------------------------------------------- #
def test_order_review_findings_say_what_is_not_computed():
    order = {"surface_finish": "ENIG", "delivery_format": "panel",
             "design_count": 2}
    messages = [f.message for f in fab.check_order(order)
                if f.code == "ORDER_REVIEW_REQUIRED"]
    assert len(messages) == 3
    assert all("NOT computed" in m for m in messages)
    enig, panel, multi = messages
    assert "mask-open copper area" in enig and "polygon booleans" in enig
    assert "V-cut" in panel and "not in the board file" in panel
    explained = fab.explain("ORDER_REVIEW_REQUIRED")
    assert "does NOT compute" in explained and "Nothing was measured" in explained


def test_new_codes_are_explained(capsys):
    for code, needle in (
            ("FAB_TENTING_UNKNOWN", "NOT applied"),
            ("FAB_SLOT_BELOW_MIN", "min_non_plated_width_mm"),
            ("FAB_RULES_LOOSER_THAN_PROFILE", "board.design_settings.rules")):
        assert main(["fab", "explain", code]) == 0
        assert needle in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# bundled profile revisions
# --------------------------------------------------------------------------- #
# A PUBLISHED profile revision is never edited in place — a changed rule set is
# a new id in a new file. Pin every published file's bytes here when it ships.
_PUBLISHED = {
    "jlc-4l-1oz.toml":
        "e0715b2407f337c436c5e972edcd819df72522d8f348623532cf929f96e318bc",
}


def test_published_profile_revisions_are_byte_identical():
    for name, digest in _PUBLISHED.items():
        data = (ROOT / "examples" / "fab" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, (
            f"examples/fab/{name} changed — a published profile revision is "
            "immutable; cut a new file with a new id instead")


def test_revision_2_adds_only_the_slot_rule_to_the_2026_07_snapshot():
    old, _ = fab.resolve_profile("jlc-4l-1oz-free-vias-2026-07")
    new, origin = fab.resolve_profile("jlc-4l-1oz-free-vias-2026-07-r2")
    assert origin.bundled and origin.name == "jlc-4l-1oz-r2.toml"
    assert new["slot"] == {"min_plated_width_mm": 0.35,
                           "min_non_plated_width_mm": 1.0}
    # every pre-existing rule is carried over number for number
    for table in ("stackup", "via", "cost"):
        assert new[table] == old[table], table
    assert set(old) | {"slot"} == set(new)
    # evidence: the old sources and date, plus the page the slot rows cite
    assert new["source"]["urls"][:2] == old["source"]["urls"]
    assert fab.retrieved_at(new) == fab.retrieved_at(old)
    assert len(new["source"]["urls"]) == 3


def test_revision_2_flags_the_narrow_slots_of_the_real_fixture():
    profile, origin = fab.resolve_profile("jlc-4l-1oz-free-vias-2026-07-r2")
    pcb = kicad.read_pcb(str(FAB_FIX / "slots.kicad_pcb"))
    hits = sorted(f.message.split(":")[0]
                  for f in fab.check(pcb, profile, origin=origin,
                                     today=dt.date(2026, 10, 1))
                  if f.code == "FAB_SLOT_BELOW_MIN")
    assert hits == ["J1 (unnumbered pad)", "J1.2"]
