"""GUI-open write guard: refuse to write under a KiCad ``~<name>.lck`` file.

A write while the KiCad GUI holds the document is a losing race — the GUI's
later save overwrites the edit from memory. ``draw/arrange/undo --apply``
refuse by default; ``--allow-open`` is explicit risk acceptance (and the CLI
then reminds the user to File>Revert).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import AkcliError
from akcli.writers import kicad as kwriter

_BLANK = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid 11111111-1111-1111-1111-111111111111)
  (paper "A4")
  (lib_symbols)
)"""

_OPS = {
    "protocol_version": 1,
    "target_format": "kicad",
    "ops": [{"op": "add_text", "text": "hello", "at": [1000, 1000]}],
}


@pytest.fixture()
def sch(tmp_path) -> Path:
    p = tmp_path / "board.kicad_sch"
    p.write_text(_BLANK)
    return p


def _lock(p: Path) -> Path:
    lck = kwriter.gui_lock_path(p)
    lck.write_text("{}")
    return lck


def test_apply_refuses_when_lock_present(sch):
    _lock(sch)
    before = sch.read_text()
    with pytest.raises(AkcliError) as ei:
        kwriter.apply(_OPS, str(sch), apply=True)
    assert ei.value.code == "TARGET_LOCKED"
    assert sch.read_text() == before          # nothing written


def test_dry_run_is_not_blocked_by_lock(sch):
    _lock(sch)
    results = kwriter.apply(_OPS, str(sch), apply=False)
    assert all(r.status != "error" for r in results)


def test_allow_open_overrides_lock(sch):
    _lock(sch)
    results = kwriter.apply(_OPS, str(sch), apply=True, allow_open=True)
    assert all(r.status != "error" for r in results)
    assert "hello" in sch.read_text()


def test_cli_draw_apply_exits_6_and_warns(sch, tmp_path, capsys):
    _lock(sch)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_OPS))
    code = main(["draw", str(sch), "--ops", str(ops), "--apply"])
    err = capsys.readouterr().err
    assert code == 6
    assert "TARGET_LOCKED" in err


def test_cli_draw_allow_open_applies_and_hints_revert(sch, tmp_path, capsys):
    _lock(sch)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_OPS))
    code = main(["draw", str(sch), "--ops", str(ops), "--apply", "--allow-open"])
    err = capsys.readouterr().err
    assert code == 0
    assert "Revert" in err


def test_undo_refuses_when_lock_present(sch, tmp_path, capsys):
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_OPS))
    assert main(["draw", str(sch), "--ops", str(ops), "--apply"]) == 0
    _lock(sch)
    capsys.readouterr()
    code = main(["undo", str(sch), "--apply"])
    assert code == 6
    assert "TARGET_LOCKED" in capsys.readouterr().err
