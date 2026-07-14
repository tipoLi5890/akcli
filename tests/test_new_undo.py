"""`akcli new` blank-sheet bootstrap + multi-level `akcli undo` rotation.

Two features that remove dogfooding friction from the write side:

* ``new <file.kicad_sch>`` writes the smallest valid document the reader and
  ``draw`` accept — no more hand-written blank sheets. Refuses an existing file
  without ``--force``; validates ``--paper``; ``--json`` envelope.
* ``draw --apply`` now rotates backups ``<name>.bak, .bak2 … .bak{depth}``
  (default depth 3). ``undo`` keeps its single-step swap (undo twice = redo),
  adds ``--list`` (show the stack) and ``--steps N`` (walk back N snapshots while
  leaving a one-step redo intact).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import kicad as kreader

FIXTURES = Path(__file__).parent / "fixtures"
V8 = FIXTURES / "kicad" / "board_v8.kicad_sch"


def _copy_v8(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    tgt = tmp_path / name
    shutil.copy(V8, tgt)
    return tgt


def _mpn_ops(tmp_path: Path, mpn: str, name: str = "ops.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "target_file": "board.kicad_sch",
        "ops": [{"op": "set_component_parameters", "designator": "R1",
                 "parameters": {"MPN": mpn}}],
    }), encoding="utf-8")
    return p


def _apply_mpn(tmp_path: Path, target: Path, mpn: str) -> None:
    ops = _mpn_ops(tmp_path, mpn)
    assert main(["draw", str(target), "--ops", str(ops), "--apply"]) == EXIT["OK"]


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #
def test_new_writes_a_readable_blank_sheet(tmp_path, capsys):
    tgt = tmp_path / "fresh.kicad_sch"
    assert main(["new", str(tgt)]) == EXIT["OK"]
    assert "CREATED" in capsys.readouterr().out
    assert tgt.exists()
    # the reader accepts it immediately (empty design, no crash)
    sch = kreader.read_sch(str(tgt))
    assert sch.components == [] and sch.nets == []
    text = tgt.read_text(encoding="utf-8")
    assert '(paper "A4")' in text and "(version 20231120)" in text


def test_new_draw_can_append_to_the_blank_sheet(tmp_path):
    tgt = tmp_path / "fresh.kicad_sch"
    assert main(["new", str(tgt)]) == EXIT["OK"]
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "target_file": "fresh.kicad_sch",
        "ops": [{"op": "place_decoupling", "x_mil": 4000, "y_mil": 2000,
                 "power_net": "+3V3", "designator": "C1"}],
    }), encoding="utf-8")
    # a fresh sheet has no embedded symbols, so point draw at a symbol source
    assert main(["draw", str(tgt), "--ops", str(ops),
                 "--symbols", str(V8), "--apply"]) == EXIT["OK"]
    assert any(c.designator == "C1" for c in kreader.read_sch(str(tgt)).components)


def test_new_title_and_paper(tmp_path):
    tgt = tmp_path / "t.kicad_sch"
    assert main(["new", str(tgt), "--paper", "A3", "--title", "My Board"]) == EXIT["OK"]
    text = tgt.read_text(encoding="utf-8")
    assert '(paper "A3")' in text
    assert '(title "My Board")' in text


def test_new_refuses_existing_without_force(tmp_path, capsys):
    tgt = tmp_path / "t.kicad_sch"
    assert main(["new", str(tgt)]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["new", str(tgt)]) == EXIT["USAGE"]
    assert "exists" in capsys.readouterr().err
    # --force overwrites
    assert main(["new", str(tgt), "--force"]) == EXIT["OK"]


def test_new_rejects_bad_paper_and_extension(tmp_path, capsys):
    assert main(["new", str(tmp_path / "t.kicad_sch"), "--paper", "Z9"]) == EXIT["USAGE"]
    assert "unknown paper" in capsys.readouterr().err
    assert main(["new", str(tmp_path / "t.txt")]) == EXIT["USAGE"]
    assert ".kicad_sch" in capsys.readouterr().err


def test_new_json_envelope(tmp_path, capsys):
    tgt = tmp_path / "t.kicad_sch"
    assert main(["new", str(tgt), "--paper", "A3", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["created"] is True and doc["paper"] == "A3"


# --------------------------------------------------------------------------- #
# backup rotation
# --------------------------------------------------------------------------- #
def test_draw_rotates_backups_up_to_depth(tmp_path):
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B", "MPN-C", "MPN-D"):
        _apply_mpn(tmp_path, tgt, mpn)
    # depth 3: .bak/.bak2/.bak3 exist, the 4th-oldest snapshot dropped
    assert (tmp_path / "board.kicad_sch.bak").exists()
    assert (tmp_path / "board.kicad_sch.bak2").exists()
    assert (tmp_path / "board.kicad_sch.bak3").exists()
    assert not (tmp_path / "board.kicad_sch.bak4").exists()
    # newest-first ordering: file=D, .bak=C, .bak2=B, .bak3=A
    assert '"MPN-D"' in tgt.read_text()
    assert '"MPN-C"' in (tmp_path / "board.kicad_sch.bak").read_text()
    assert '"MPN-B"' in (tmp_path / "board.kicad_sch.bak2").read_text()
    assert '"MPN-A"' in (tmp_path / "board.kicad_sch.bak3").read_text()


# --------------------------------------------------------------------------- #
# undo --list
# --------------------------------------------------------------------------- #
def test_undo_list_text_and_json(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B"):
        _apply_mpn(tmp_path, tgt, mpn)
    capsys.readouterr()
    assert main(["undo", str(tgt), "--list"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "board.kicad_sch.bak" in out and "[1]" in out
    # --list never writes
    before = tgt.read_bytes()
    assert main(["undo", str(tgt), "--list", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["depth"] == 2
    assert [b["level"] for b in doc["backups"]] == [1, 2]
    assert tgt.read_bytes() == before


def test_undo_list_no_backups(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    assert main(["undo", str(tgt), "--list"]) == EXIT["OK"]
    assert "no backups" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# undo default swap (unchanged: undo twice = redo)
# --------------------------------------------------------------------------- #
def test_undo_swap_still_redoes(tmp_path):
    tgt = _copy_v8(tmp_path)
    _apply_mpn(tmp_path, tgt, "MPN-A")
    _apply_mpn(tmp_path, tgt, "MPN-B")
    assert '"MPN-B"' in tgt.read_text()
    assert main(["undo", str(tgt), "--apply"]) == EXIT["OK"]
    assert '"MPN-A"' in tgt.read_text()   # one step back
    assert main(["undo", str(tgt), "--apply"]) == EXIT["OK"]
    assert '"MPN-B"' in tgt.read_text()   # undo twice = redo


# --------------------------------------------------------------------------- #
# undo --steps (walk back N, redo still works for the last step)
# --------------------------------------------------------------------------- #
def test_undo_steps_walks_back_and_keeps_redo(tmp_path):
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B", "MPN-C", "MPN-D"):
        _apply_mpn(tmp_path, tgt, mpn)
    # file=D, .bak=C, .bak2=B, .bak3=A
    assert main(["undo", str(tgt), "--steps", "2", "--apply"]) == EXIT["OK"]
    assert '"MPN-B"' in tgt.read_text()   # two snapshots back (D->C->B)
    # a single undo afterwards redoes only the LAST walked step (B->C)
    assert main(["undo", str(tgt), "--apply"]) == EXIT["OK"]
    assert '"MPN-C"' in tgt.read_text()


def test_undo_steps_dry_run_writes_nothing(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B", "MPN-C"):
        _apply_mpn(tmp_path, tgt, mpn)
    before = tgt.read_bytes()
    capsys.readouterr()
    assert main(["undo", str(tgt), "--steps", "2"]) == EXIT["OK"]
    assert "dry-run" in capsys.readouterr().out
    assert tgt.read_bytes() == before


def test_undo_steps_clamps_to_available_stack(tmp_path):
    tgt = _copy_v8(tmp_path)
    _apply_mpn(tmp_path, tgt, "MPN-A")   # only one backup exists
    # ask for more steps than snapshots: walk back as far as it goes (1)
    assert main(["undo", str(tgt), "--steps", "9", "--apply"]) == EXIT["OK"]
    assert '"MPN-A"' not in tgt.read_text()   # restored to the pre-edit original


def test_undo_steps_zero_is_usage_error(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    _apply_mpn(tmp_path, tgt, "MPN-A")
    assert main(["undo", str(tgt), "--steps", "0"]) == EXIT["USAGE"]
    assert "steps" in capsys.readouterr().err


def test_undo_steps_no_backup_is_not_found(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    assert main(["undo", str(tgt), "--steps", "2"]) == EXIT["NOT_FOUND"]
    assert "no backup" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# review-round regressions: gap-tolerant stack, config backup_depth
# --------------------------------------------------------------------------- #
def test_backup_stack_survives_a_level_gap(tmp_path, capsys):
    # A crash between rotation and the fresh .bak copy can leave level 1
    # missing while deeper snapshots exist — they must stay visible/usable.
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B", "MPN-C"):
        _apply_mpn(tmp_path, tgt, mpn)
    bak1 = tmp_path / (tgt.name + ".bak")
    assert bak1.exists()
    bak1.unlink()                                    # simulate the gap
    assert main(["undo", str(tgt), "--list"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert ".bak2" in out and "no backups" not in out
    assert main(["undo", str(tgt), "--apply"]) == EXIT["OK"]
    assert '"MPN-A"' in tgt.read_text()              # restored from .bak2


def test_config_backup_depth_key_is_accepted(tmp_path):
    # The documented [project] backup_depth key must not brick config load.
    from akcli.config import load_config
    cfgf = tmp_path / "akcli.toml"
    cfgf.write_text("[project]\nbackup_depth = 5\n")
    cfg = load_config(str(cfgf))
    assert cfg.backup_depth == 5
    cfgf.write_text("[project]\nbackup_depth = 0\n")
    import pytest
    from akcli.errors import AkcliError
    with pytest.raises(AkcliError):
        load_config(str(cfgf))


def test_undo_walk_leaves_no_tmp_files(tmp_path):
    tgt = _copy_v8(tmp_path)
    for mpn in ("MPN-A", "MPN-B", "MPN-C"):
        _apply_mpn(tmp_path, tgt, mpn)
    assert main(["undo", str(tgt), "--steps", "2", "--apply"]) == EXIT["OK"]
    assert '"MPN-A"' in tgt.read_text()
    assert not list(tmp_path.glob("*.undo-tmp"))     # disk-safe swap cleans up
