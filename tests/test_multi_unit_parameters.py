"""`set_component_parameters` on a multi-unit part edits EVERY placed unit.

Each placed unit is its own symbol node, while Value / Footprint / fields
belong to the part: writing only the first unit left the others on the old
value with no check to flag it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT

SYMBOLS = (Path(__file__).parent.parent / "tools" / "agent_eval" / "symbols"
           / "EvalParts.kicad_sym")

_PLACE = {"protocol_version": 2, "target_format": "kicad", "ops": [
    {"op": "place_component", "lib_id": "EvalParts:OPAMP_DUAL",
     "designator": "U1", "unit": 1, "x_mil": 3000, "y_mil": 2000,
     "value": "OPAMP_DUAL"},
    {"op": "place_component", "lib_id": "EvalParts:OPAMP_DUAL",
     "designator": "U1", "unit": 2, "x_mil": 5000, "y_mil": 2000,
     "value": "OPAMP_DUAL"},
]}
_EDIT = {"protocol_version": 2, "target_format": "kicad", "ops": [
    {"op": "set_component_parameters", "designator": "U1", "value": "LM358",
     "footprint": "Package_SO:SOIC-8", "parameters": {"MPN": "LM358DR"}},
]}


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("AKCLI_JOURNAL", "off")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "cfg"))
    sch = tmp_path / "t.kicad_sch"
    assert main(["new", str(sch)]) == EXIT["OK"]
    _draw(sch, _PLACE, tmp_path)
    return sch


def _draw(sch: Path, ops: dict, tmp_path: Path) -> None:
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(ops), encoding="utf-8", newline="\n")
    assert main(["draw", str(sch), "--ops", str(f), "--symbols",
                 str(SYMBOLS), "--apply"]) == EXIT["OK"]


def test_parameters_reach_every_unit(board, tmp_path):
    _draw(board, _EDIT, tmp_path)
    text = board.read_text(encoding="utf-8")
    assert text.count('"Value" "LM358"') == 2
    assert text.count('"Footprint" "Package_SO:SOIC-8"') == 2
    assert text.count('"MPN" "LM358DR"') == 2


def test_reapply_is_byte_identical(board, tmp_path):
    _draw(board, _EDIT, tmp_path)
    first = board.read_bytes()
    _draw(board, _EDIT, tmp_path)
    assert board.read_bytes() == first


def test_rename_keeps_the_units_one_part(board, tmp_path):
    _draw(board, {**_EDIT, "ops": [
        {"op": "set_component_parameters", "designator": "U1",
         "new_designator": "U7"}]}, tmp_path)
    text = board.read_text(encoding="utf-8")
    assert text.count('(reference "U7")') == 2
    assert '(reference "U1")' not in text
