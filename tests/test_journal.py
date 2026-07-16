"""Workspace write journal (`.akcli/journal.jsonl`) + `akcli log`.

Every write-path command appends an entry (plan/draw record their dry-run vs
applied vs refused status + the op-list sha256), `akcli log` reads it back,
and journaling can never fail the parent command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import journal
from akcli.cli import main
from akcli.errors import EXIT

OPS = {
    "protocol_version": 1, "target_format": "kicad",
    "target_file": "board.kicad_sch",
    "ops": [{"op": "add_text", "at": [1000, 1000], "text": "hello"}],
}


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    target = tmp_path / "board.kicad_sch"
    assert main(["new", str(target)]) == EXIT["OK"]
    (tmp_path / "ops.json").write_text(json.dumps(OPS), encoding="utf-8")
    return target


def test_plan_and_draw_record_entries(board: Path, capsys):
    ops = str(board.parent / "ops.json")
    assert main(["plan", str(board), "--ops", ops]) == EXIT["OK"]
    assert main(["draw", str(board), "--ops", ops, "--apply"]) == EXIT["OK"]
    capsys.readouterr()

    entries = journal.read_entries(board)
    cmds = [(e["cmd"], e["status"]) for e in entries]
    assert ("plan", "dry-run") in cmds
    assert ("draw", "applied") in cmds
    applied = next(e for e in entries if e["status"] == "applied")
    assert applied["ops_sha256"] and applied["op_count"] == 1
    assert applied["backup"] == "board.kicad_sch.bak"
    assert applied["net_diff"] == {"equivalent": True, "risk": False}


def test_undo_records_entry(board: Path, capsys):
    ops = str(board.parent / "ops.json")
    assert main(["draw", str(board), "--ops", ops, "--apply"]) == EXIT["OK"]
    assert main(["undo", str(board), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    entries = journal.read_entries(board, target="board.kicad_sch")
    assert entries[-1]["cmd"] == "undo" and entries[-1]["status"] == "applied"


def test_log_text_and_json(board: Path, capsys):
    ops = str(board.parent / "ops.json")
    main(["plan", str(board), "--ops", ops])
    capsys.readouterr()

    assert main(["log", str(board.parent)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "plan" in out and "dry-run" in out

    assert main(["log", str(board.parent), "--json", "--limit", "1"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["journal_version"] == journal.JOURNAL_VERSION
    assert doc["returned"] == 1
    assert doc["entries"][0]["cmd"] == "plan"


def test_log_filters_by_file_and_cmd(board: Path, capsys):
    ops = str(board.parent / "ops.json")
    main(["plan", str(board), "--ops", ops])
    main(["draw", str(board), "--ops", ops, "--apply"])
    capsys.readouterr()

    assert main(["log", str(board), "--cmd", "draw", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert {e["cmd"] for e in doc["entries"]} == {"draw"}


def test_journal_env_off(board: Path, monkeypatch, capsys):
    monkeypatch.setenv("AKCLI_JOURNAL", "off")
    ops = str(board.parent / "ops.json")
    main(["plan", str(board), "--ops", ops])
    capsys.readouterr()
    assert journal.read_entries(board) == []


def test_journal_corrupt_lines_skipped(tmp_path: Path):
    jdir = tmp_path / journal.DIR_NAME
    jdir.mkdir()
    (jdir / journal.FILE_NAME).write_text(
        'not json\n{"cmd": "draw", "target": "x", "status": "applied"}\n',
        encoding="utf-8")
    entries = journal.read_entries(tmp_path)
    assert len(entries) == 1 and entries[0]["cmd"] == "draw"


def test_journal_write_failure_never_raises(tmp_path: Path, capsys, monkeypatch):
    # a file where the .akcli DIRECTORY should be -> mkdir fails -> stderr note
    (tmp_path / journal.DIR_NAME).write_text("in the way", encoding="utf-8")
    journal.record(tmp_path / "board.kicad_sch", "draw", "applied")
    assert "journal write skipped" in capsys.readouterr().err


def test_journal_rotation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(journal, "_MAX_BYTES", 64)
    target = tmp_path / "board.kicad_sch"
    for _ in range(4):
        journal.record(target, "draw", "applied")
    assert (tmp_path / journal.DIR_NAME / "journal.jsonl.1").exists()
