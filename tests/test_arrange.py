"""`akcli arrange` — overlap-resolving nudges for FREE components only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from akcli import arrange, cli
from akcli.readers import kicad as kreader

FIXTURE = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"


def _pile(tmp_path, extra_ops=()):
    """Fixture copy + four free parts stacked on one spot."""
    work = tmp_path / "t.kicad_sch"
    shutil.copy(FIXTURE, work)
    ops = [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R10",
         "x_mil": 5000, "y_mil": 3000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R11",
         "x_mil": 5050, "y_mil": 3000, "value": "2k"},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C10",
         "x_mil": 5000, "y_mil": 3050, "value": "1n"},
        *extra_ops,
    ]
    opsfile = tmp_path / "ops.json"
    opsfile.write_text(json.dumps({"protocol_version": 1,
                                   "target_format": "kicad",
                                   "target_file": "x", "ops": ops}))
    assert cli.main(["draw", str(work), "--ops", str(opsfile), "--apply"]) == 0
    return work


def test_clean_sheet_plans_nothing():
    result = arrange.plan(FIXTURE)
    assert result["clean"] and result["moves"] == []
    assert result["symbols"] == 5


def test_plan_resolves_pile_without_touching_wired_parts(tmp_path, capsys):
    work = _pile(tmp_path)
    capsys.readouterr()
    result = arrange.plan(work)
    moved = {m.ref for m in result["moves"]}
    assert moved and moved <= {"R10", "R11", "C10"}   # only the free pile
    assert result["anchored_overlaps"] == []

    # apply through the CLI and prove the sheet is now overlap-free
    assert cli.main(["arrange", str(work), "--apply"]) == 0
    capsys.readouterr()
    assert arrange.plan(work)["clean"]

    # wired fixture components never moved
    pos = {c.designator: (c.x_mil, c.y_mil)
           for c in kreader.read_sch(str(work)).components}
    assert pos["R1"] == (2000.0, 2000.0)
    assert pos["R2"] == (2000.0, 2500.0)

    # the write went through the draw pipeline -> undo reverts it
    assert cli.main(["undo", str(work), "--apply"]) == 0
    capsys.readouterr()
    assert not arrange.plan(work)["clean"]


def test_anchored_components_are_never_moved(tmp_path, capsys):
    # a labeled (pin-anchored) part overlapping another labeled part is
    # reported, not silently relocated
    work = _pile(tmp_path, extra_ops=[
        {"op": "place_decoupling", "x_mil": 5050, "y_mil": 3100,
         "power_net": "+3V3", "designator": "C11"},
    ])
    capsys.readouterr()
    result = arrange.plan(work)
    assert "C11" not in {m.ref for m in result["moves"]}


def test_cli_dry_run_and_json(tmp_path, capsys):
    work = _pile(tmp_path)
    capsys.readouterr()
    assert cli.main(["arrange", str(work)]) == 0            # dry-run
    out = capsys.readouterr().out
    assert "dry-run" in out and "move " in out
    assert cli.main(["arrange", str(work), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["symbols"] == 8 and not doc["clean"]
    assert all(m["to"] != m["from"] for m in doc["moves"])
    # dry runs never write
    assert not arrange.plan(work)["clean"]


def test_cli_rejects_non_kicad(tmp_path, capsys):
    f = tmp_path / "x.SchDoc"
    f.write_text("nope")
    assert cli.main(["arrange", str(f)]) == 2
