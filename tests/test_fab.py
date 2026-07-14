"""Fab profile checks (`akcli fab check`) — plan acceptance criteria:

* 0.20/0.40 mm via  -> cost ERROR (paid small-via process)
* 0.30/0.40 mm via  -> passes, minimum-margin NOTE + below-preferred-annular NOTE
* 0.30/0.45 mm via  -> passes clean (the recommended geometry)
* any tented drill > 0.40 mm -> ERROR
* via inside an SMD pad -> ERROR; a registered thermal_via exception -> NOTE
* profile stackup vs board setup drift -> ERROR
* order manifest: missing fields ERROR; ENIG/panel raise review findings
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from akcli import fab
from akcli.cli import main
from akcli.errors import AkcliError
from akcli.readers import kicad

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
    pcb = _board(tmp_path, '(via (at 10 10) (size 0.6) (drill 0.45) '
                           '(layers "F.Cu" "B.Cu") (net 1))')
    assert "FAB_VIA_TENTED_TOO_BIG" in _codes(fab.check(pcb, profile))


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
