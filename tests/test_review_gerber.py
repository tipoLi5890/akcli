"""Gerber reader + fab-package review (M9).

Fixtures are synthesized minimal RS-274X / Excellon files — enough to
exercise X2 role detection, filename fallback, unit handling, bbox math,
and the honesty rule (ambiguous coordinates yield warnings, never guesses).
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli
from akcli.model import Pcb, Schematic
from akcli.readers import gerber as greader
from akcli.review import engine, topo
from akcli.review.detectors import gerber as gdet

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _gbr(function: str | None, points: list[tuple[float, float]],
         units: str = "mm") -> str:
    """Minimal RS-274X body: X2 function attr, mm/in units, 4.6 format."""
    head = []
    if function:
        head.append(f"%TF.FileFunction,{function}*%")
    head.append("%MOMM*%" if units == "mm" else "%MOIN*%")
    head.append("%FSLAX46Y46*%")
    head.append("%ADD10C,0.100000*%")
    body = ["G01*", "D10*"]
    for i, (x, y) in enumerate(points):
        op = "D02" if i == 0 else "D01"
        body.append(f"X{round(x * 1e6)}Y{round(y * 1e6)}{op}*")
    body.append("M02*")
    return "\n".join(head + body) + "\n"


def _drl(holes: list[tuple[float, float]], metric: bool = True,
         dotted: bool = True) -> str:
    lines = ["M48", "METRIC" if metric else "INCH", "T1C0.800", "%",
             "G05", "T1"]
    for x, y in holes:
        if dotted:
            lines.append(f"X{x:.2f}Y{y:.2f}")
        else:
            lines.append(f"X{int(x * 100)}Y{int(y * 100)}")
    lines.append("M30")
    return "\n".join(lines) + "\n"


def _outline_pts(w=50.0, h=40.0, ox=0.0, oy=0.0):
    return [(ox, oy), (ox + w, oy), (ox + w, oy + h), (ox, oy + h),
            (ox, oy)]


def _full_set(root: Path, *, copper_offset=0.0, outline_w=50.0,
              with_silk=True):
    root.mkdir(parents=True, exist_ok=True)
    pts = _outline_pts(w=outline_w)
    cu = _outline_pts(w=outline_w, ox=copper_offset)
    (root / "b-F_Cu.gbr").write_text(_gbr("Copper,L1,Top", cu))
    (root / "b-B_Cu.gbr").write_text(_gbr("Copper,L2,Bot", cu))
    (root / "b-F_Mask.gbr").write_text(_gbr("Soldermask,Top", pts))
    (root / "b-B_Mask.gbr").write_text(_gbr("Soldermask,Bot", pts))
    if with_silk:
        (root / "b-F_Silkscreen.gbr").write_text(_gbr("Legend,Top", pts))
    (root / "b-Edge_Cuts.gbr").write_text(_gbr("Profile,NP", pts))
    (root / "b-PTH.drl").write_text(_drl([(10, 10), (40, 30)]))
    return root


def _ctx(gerbers, pcb=None):
    sch = Schematic(source_path="<none>", source_format="kicad",
                    components=[], nets=[])
    return topo.build_ctx(sch, pcb, None, gerbers)


# --------------------------------------------------------------------------- #
# reader
# --------------------------------------------------------------------------- #
def test_reader_x2_roles_units_bbox(tmp_path):
    gs = greader.read_gerber_dir(_full_set(tmp_path / "fab"))
    kinds = {f.kind for f in gs.files}
    assert {"copper_top", "copper_bottom", "mask_top", "mask_bottom",
            "silk_top", "outline", "drill"} <= kinds
    outline = gs.by_kind("outline")[0]
    assert outline.units == "mm"
    x0, y0, x1, y1 = outline.bbox_mm
    assert (round(x1 - x0, 3), round(y1 - y0, 3)) == (50.0, 40.0)
    drill = gs.by_kind("drill")[0]
    assert drill.tools == 1 and drill.holes == 2
    assert drill.bbox_mm == (10.0, 10.0, 40.0, 30.0)


def test_reader_filename_fallback_and_inches(tmp_path):
    d = tmp_path / "fab"
    d.mkdir()
    (d / "board.gtl").write_text(_gbr(None, _outline_pts(w=1.0, h=1.0),
                                      units="in"))
    gs = greader.read_gerber_dir(d)
    f = gs.files[0]
    assert f.kind == "copper_top" and f.units == "in"
    x0, y0, x1, y1 = f.bbox_mm
    assert round(x1 - x0, 2) == 25.4                    # 1 inch → mm


def test_reader_ambiguous_excellon_never_guesses(tmp_path):
    d = tmp_path / "fab"
    d.mkdir()
    (d / "b.drl").write_text(_drl([(10, 10)], dotted=False))
    gs = greader.read_gerber_dir(d)
    f = gs.files[0]
    assert f.holes == 1 and f.bbox_mm is None
    assert any("bbox skipped" in w for w in f.warnings)


def test_reader_empty_dir_warns(tmp_path):
    gs = greader.read_gerber_dir(tmp_path)
    assert gs.files == [] and gs.warnings


# --------------------------------------------------------------------------- #
# detector
# --------------------------------------------------------------------------- #
def test_complete_package_is_quiet(tmp_path):
    gs = greader.read_gerber_dir(_full_set(tmp_path / "fab"))
    assert gdet.run(_ctx(gs)) == []


def test_missing_mask_and_silk(tmp_path):
    d = tmp_path / "fab"
    _full_set(d, with_silk=False)
    (d / "b-B_Mask.gbr").unlink()
    fs = gdet.run(_ctx(greader.read_gerber_dir(d)))
    by_sev = {f.severity.value: f for f in fs}
    assert "mask_bottom" in by_sev["warning"].refs
    assert "silk_top" in by_sev["note"].refs


def test_misaligned_copper_flagged(tmp_path):
    gs = greader.read_gerber_dir(
        _full_set(tmp_path / "fab", copper_offset=10.0))
    fs = gdet.run(_ctx(gs))
    assert [f.code for f in fs] == ["REVIEW_GERBER_ALIGNMENT"]


def test_stale_outline_vs_board(tmp_path):
    gs = greader.read_gerber_dir(_full_set(tmp_path / "fab", outline_w=45.0))
    pcb = Pcb(source_path="<t>", source_format="kicad", nets=[],
              footprints=[],
              board={"units": "mm", "copper_layers": ["F.Cu", "B.Cu"],
                     "outline_bbox": ((0, 0), (50, 40))})
    fs = gdet.run(_ctx(gs, pcb))
    assert [f.code for f in fs] == ["REVIEW_GERBER_STALE"]
    assert fs[0].evidence["calc"]["gerber_mm"] == [45.0, 40.0]
    assert fs[0].evidence["calc"]["board_mm"] == [50.0, 40.0]


def test_layer_count_mismatch(tmp_path):
    gs = greader.read_gerber_dir(_full_set(tmp_path / "fab"))
    pcb = Pcb(source_path="<t>", source_format="kicad", nets=[],
              footprints=[],
              board={"units": "mm",
                     "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                     "outline_bbox": ((0, 0), (50, 40))})
    fs = gdet.run(_ctx(gs, pcb))
    assert "REVIEW_GERBER_LAYER_MISMATCH" in {f.code for f in fs}


def test_mixed_units_note(tmp_path):
    d = _full_set(tmp_path / "fab")
    (d / "extra.gtp").write_text(
        _gbr("Paste,Top", _outline_pts(w=2.0, h=1.6), units="in"))
    fs = gdet.run(_ctx(greader.read_gerber_dir(d)))
    assert "REVIEW_GERBER_UNITS_MIXED" in {f.code for f in fs}


# --------------------------------------------------------------------------- #
# engine + CLI + preflight
# --------------------------------------------------------------------------- #
def test_engine_skips_gerber_family_without_dir():
    sch = Schematic(source_path="<t>", source_format="kicad",
                    components=[], nets=[])
    _fs, meta = engine.analyze(sch, profile="standard")
    assert "gerber.package" in meta["detectors_skipped"]


def _seed_sch(tmp_path):
    from akcli.writers import kicad as kw
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "99999999-aaaa-bbbb-cccc-dddddddddddd") (paper "A4"))\n')
    rs = kw.apply({"protocol_version": 2, "target_format": "kicad", "ops": [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "10k"},
        {"op": "add_net_label", "name": "A", "at": "R1.1"},
        {"op": "add_net_label", "name": "B", "at": "R1.2"},
    ]}, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    return tgt


def test_cli_analyze_with_gerbers(tmp_path, capsys):
    tgt = _seed_sch(tmp_path)
    fab = _full_set(tmp_path / "fab", with_silk=False)
    assert cli.main(["review", "analyze", str(tgt), "--gerbers", str(fab),
                     "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "gerber.package" in doc["metadata"]["detectors_run"]
    assert "REVIEW_GERBER_INCOMPLETE" in {f["code"] for f in doc["findings"]}


def test_preflight_gerber_gate(tmp_path, capsys):
    tgt = _seed_sch(tmp_path)
    fab = _full_set(tmp_path / "fab")
    (fab / "b-Edge_Cuts.gbr").unlink()          # outline missing → gate fails
    code = cli.main(["release", "preflight", "--sch", str(tgt),
                     "--gerbers", str(fab), "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    gate = next(g for g in doc["gates"] if g["gate"] == "gerber")
    assert gate["status"] == "fail"
    assert any("outline" in f["message"] for f in gate["findings"])
    assert sorted(doc["inputs"]["gerbers"]["files"])
    assert code == 1

    capsys.readouterr()
    code = cli.main(["release", "preflight", "--sch", str(tgt),
                     "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    gate = next(g for g in doc["gates"] if g["gate"] == "gerber")
    assert gate["status"] == "skipped"
