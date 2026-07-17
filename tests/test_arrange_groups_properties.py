"""arrange --groups (bare) + --frames, and the functional-group layout lints.

Bare ``--groups`` derives the module map from the sheet's hidden ``Group``
properties (no file needed); ``--frames`` redraws each group's border after
packing. ``check --layout`` gains two advisory findings: overlapping group
extents and stale frames.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import arrange, cli, netdiff, ops
from akcli.checks import layout
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = str(Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym")


def _seed_grouped(tmp_path: Path, *, interleaved: bool = True) -> Path:
    """Two labeled divider blocks tagged via group ops (A: R1-R2, B: R3-R4)."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    coords = ({"R1": (2000, 1000), "R3": (2050, 1000),
               "R2": (2000, 1400), "R4": (2050, 1400)}
              if interleaved else
              {"R1": (1000, 1000), "R2": (1000, 1400),
               "R3": (6000, 1000), "R4": (6000, 1400)})
    group_of = {"R1": "block_a", "R2": "block_a", "R3": "block_b", "R4": "block_b"}
    ops_list = [
        {"op": "place_component", "lib_id": "Device:R", "designator": ref,
         "x_mil": x, "y_mil": y, "group": group_of[ref], "value": "1k"}
        for ref, (x, y) in coords.items()
    ] + [
        {"op": "add_net_label", "name": "VINA", "at": "R1.1"},
        {"op": "add_net_label", "name": "MIDA", "at": "R1.2"},
        {"op": "add_net_label", "name": "MIDA", "at": "R2.1"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
        {"op": "add_net_label", "name": "VINB", "at": "R3.1"},
        {"op": "add_net_label", "name": "MIDB", "at": "R3.2"},
        {"op": "add_net_label", "name": "MIDB", "at": "R4.1"},
        {"op": "add_net_label", "name": "GND", "at": "R4.2"},
    ]
    d = {"protocol_version": 1, "target_format": "kicad",
         "groups": {"block_a": {"origin": [0, 0]}, "block_b": {"origin": [0, 0]}},
         "ops": ops_list}
    rs = kw.apply(ops.resolve_groups(d), str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def test_groups_from_properties():
    pass  # covered inline below via arrange


def test_arrange_bare_groups_uses_properties(tmp_path, capsys):
    tgt = _seed_grouped(tmp_path)
    groups = arrange.groups_from_properties(tgt)
    assert groups == {"block_a": ["R1", "R2"], "block_b": ["R3", "R4"]}

    before = kreader.read_sch(str(tgt)).nets
    assert cli.main(["arrange", str(tgt), "--groups", "--apply",
                     "--symbols", DEVICE]) == 0
    d = netdiff.diff(before, kreader.read_sch(str(tgt)).nets)
    assert d.equivalent, netdiff.format_summary(d)
    # blocks are separated now
    pos = {c.designator: (c.x_mil, c.y_mil)
           for c in kreader.read_sch(str(tgt)).components}
    a_bottom = max(pos["R1"][1], pos["R2"][1])
    b_top = min(pos["R3"][1], pos["R4"][1])
    assert b_top > a_bottom


def test_arrange_bare_groups_without_properties_is_usage_error(tmp_path):
    tgt = tmp_path / "plain.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    assert cli.main(["arrange", str(tgt), "--groups", "--apply"]) == 2


def test_arrange_groups_frames_refreshes_borders(tmp_path, capsys):
    tgt = _seed_grouped(tmp_path)
    assert cli.main(["arrange", str(tgt), "--groups", "--frames", "--apply",
                     "--symbols", DEVICE, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["frames_refreshed"] == 2
    from akcli.groupframe import group_report
    assert all(r["has_frame"] for r in group_report(tgt))


def test_layout_lint_group_overlap_fires_and_clears(tmp_path):
    tgt = _seed_grouped(tmp_path, interleaved=True)
    codes = {f.code for f in layout.run(tgt)}
    assert layout.LAYOUT_GROUP_OVERLAP in codes

    assert cli.main(["arrange", str(tgt), "--groups", "--apply",
                     "--symbols", DEVICE]) == 0
    codes = {f.code for f in layout.run(tgt)}
    assert layout.LAYOUT_GROUP_OVERLAP not in codes


def test_layout_lint_frame_stale_fires_and_clears(tmp_path):
    tgt = _seed_grouped(tmp_path, interleaved=False)
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    assert layout.LAYOUT_FRAME_STALE not in {f.code for f in layout.run(tgt)}

    # drag one member out of its frame
    mv = {"protocol_version": 1, "target_format": "kicad", "ops": [
        {"op": "move_component", "designator": "R2",
         "x_mil": 3500, "y_mil": 3500, "carry_labels": True}]}
    rs = kw.apply(mv, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    assert layout.LAYOUT_FRAME_STALE in {f.code for f in layout.run(tgt)}

    # refresh clears it
    assert cli.main(["groups", str(tgt), "--frame", "--apply",
                     "--symbols", DEVICE]) == 0
    assert layout.LAYOUT_FRAME_STALE not in {f.code for f in layout.run(tgt)}


def test_layout_lint_textbox_over_symbol(tmp_path):
    tgt = _seed_grouped(tmp_path, interleaved=False)
    box = {"protocol_version": 1, "target_format": "kicad", "ops": [
        {"op": "add_text_box", "text": "note", "at": [900, 900],
         "size": [400, 400]}]}
    rs = kw.apply(box, str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    codes = {f.code for f in layout.run(tgt)}
    assert "LAYOUT_TEXTBOX_OVER_SYMBOL" in codes  # covers R1 at (1000,1000)

    # move it clear -> lint gone
    rs = kw.apply({"protocol_version": 1, "target_format": "kicad", "ops": [
        {"op": "delete_object", "match": {"kind": "text_box"}},
        {"op": "add_text_box", "text": "note", "at": [8000, 8000],
         "size": [400, 400]}]}, str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert "LAYOUT_TEXTBOX_OVER_SYMBOL" not in {f.code for f in layout.run(tgt)}
