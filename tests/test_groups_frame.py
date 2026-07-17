"""``akcli groups`` + ``--frame`` — group inspection and self-refreshing frames.

Frames are ops with a stable ``key`` (``group_frame:<name>``), so their uuids
are coordinate-independent: move the parts, re-frame, and the border is
replaced in place — never accumulated. All writes ride the standard pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli, netdiff
from akcli.groupframe import group_report, plan_frames
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = str(Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym")


def _seed(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    d = {"protocol_version": 1, "target_format": "kicad",
         "groups": {"PWR": {"origin": [1000, 1000]},
                    "LOAD": {"origin": [4000, 1000]}},
         "ops": [
             {"op": "place_component", "group": "PWR", "lib_id": "Device:R",
              "designator": "R1", "x_mil": 0, "y_mil": 0},
             {"op": "place_component", "group": "PWR", "lib_id": "Device:C",
              "designator": "C1", "x_mil": 500, "y_mil": 0},
             {"op": "place_component", "group": "LOAD", "lib_id": "Device:R",
              "designator": "R2", "x_mil": 0, "y_mil": 0},
             {"op": "add_net_label", "name": "A", "at": "R1.1"},
             {"op": "add_net_label", "name": "B", "at": "R1.2"},
         ]}
    from akcli import ops as opsmod
    rs = kw.apply(opsmod.resolve_groups(d), str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def _rect_count(tgt: Path) -> int:
    """TOP-LEVEL rectangles only (symbol bodies in lib_symbols don't count)."""
    from akcli.readers import sexpr
    root = sexpr.parse(tgt.read_text(encoding="utf-8"))
    return sum(1 for c in root.children or []
               if c.is_list and c.children and c.children[0].value == "rectangle")


def test_group_report_members_and_boxes(tmp_path):
    tgt = _seed(tmp_path)
    rows = group_report(tgt)
    by_name = {r["name"]: r for r in rows}
    assert by_name["PWR"]["members"] == ["C1", "R1"]
    assert by_name["LOAD"]["members"] == ["R2"]
    assert not by_name["PWR"]["has_frame"]
    x0, y0, x1, y1 = by_name["PWR"]["box_mil"]
    assert x0 < 1000 < x1 and y0 < 1000 < y1


def test_cli_groups_list_and_json(tmp_path, capsys):
    tgt = _seed(tmp_path)
    assert cli.main(["groups", str(tgt), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"]
    assert {g["name"] for g in payload["groups"]} == {"PWR", "LOAD"}
    assert cli.main(["groups", str(tgt)]) == 0
    assert "PWR" in capsys.readouterr().out


def test_frame_draws_and_reports(tmp_path, capsys):
    tgt = _seed(tmp_path)
    # dry-run first: no write
    assert cli.main(["groups", str(tgt), "--frame",
                     "--symbols", DEVICE]) == 0
    assert _rect_count(tgt) == 0
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    assert _rect_count(tgt) == 2
    rows = group_report(tgt)
    assert all(r["has_frame"] for r in rows)
    # frames are annotations: netlist untouched
    capsys.readouterr()


def test_frame_contains_members_with_margin(tmp_path):
    tgt = _seed(tmp_path)
    ops_list = plan_frames(tgt, margin_mil=200)
    frames = {o["key"]: o for o in ops_list if o["op"] == "add_rectangle"}
    rows = {r["name"]: r for r in group_report(tgt)}
    for name, row in rows.items():
        f = frames[f"group_frame:{name}"]
        x0, y0, x1, y1 = row["box_mil"]
        assert f["start"][0] <= x0 - 150 and f["start"][1] <= y0 - 150
        assert f["end"][0] >= x1 + 150 and f["end"][1] >= y1 + 150
        assert all(v % 50 == 0 for v in f["start"] + f["end"])
    titles = [o for o in ops_list if o["op"] == "add_text"]
    assert {t["text"] for t in titles} == {"PWR", "LOAD"}


def test_frame_refresh_replaces_in_place(tmp_path):
    """Move a part, re-frame: same uuid, new geometry, no accumulation."""
    tgt = _seed(tmp_path)
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    before_nets = kreader.read_sch(str(tgt)).nets
    first = tgt.read_text(encoding="utf-8")
    assert _rect_count(tgt) == 2

    # idempotent: re-apply with nothing moved is byte-identical
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    assert tgt.read_text(encoding="utf-8") == first

    # move a LOAD part far away, then refresh
    mv = {"protocol_version": 1, "target_format": "kicad", "ops": [
        {"op": "move_component", "designator": "R2",
         "x_mil": 7000, "y_mil": 3000, "carry_labels": True}]}
    rs = kw.apply(mv, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    assert _rect_count(tgt) == 2                      # replaced, not added
    rows = {r["name"]: r for r in group_report(tgt)}
    assert rows["LOAD"]["has_frame"]
    x0, y0, x1, y1 = rows["LOAD"]["box_mil"]
    assert x0 > 6000 and y0 > 2000                     # box followed the part

    # the whole frame lifecycle never touched a net
    d = netdiff.diff(before_nets, kreader.read_sch(str(tgt)).nets)
    assert d.equivalent, netdiff.format_summary(d)


def test_frame_title_from_groups_meta(tmp_path):
    tgt = _seed(tmp_path)
    ops_list = plan_frames(tgt, groups_meta={"PWR": {"title": "電源模組"}})
    titles = {o["key"]: o["text"] for o in ops_list if o["op"] == "add_text"}
    assert titles["group_title:PWR"] == "電源模組"
    assert titles["group_title:LOAD"] == "LOAD"


def test_groups_on_plain_sheet(tmp_path, capsys):
    tgt = tmp_path / "plain.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    assert cli.main(["groups", str(tgt)]) == 0
    assert "no functional groups" in capsys.readouterr().out
    assert cli.main(["groups", str(tgt), "--frame"]) == 0
    assert "no functional groups" in capsys.readouterr().out
