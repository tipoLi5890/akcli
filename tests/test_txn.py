"""Multi-sheet op-list transaction (``writers/txn`` + the `sheets` envelope).

One op-list, several files, one commit: ops route onto declared child files
via per-op ``sheet`` tags; every dirty file is staged (temp + fsync + verify)
before the first ``os.replace``; the pre-write bytes land in
``.akcli/backups/runs/<run_id>/`` and ``undo --run`` restores all-or-nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT, AkcliError
from akcli.writers import atomic, txn

REPO = Path(__file__).parent.parent
SYMBOLS = [str(REPO / "tests/fixtures/kicad/symbols/Device.kicad_sym")]


def _sym_flags() -> list[str]:
    return [a for s in SYMBOLS for a in ("--symbols", s)]


ROUTED_OPS = {
    "protocol_version": 1,
    "target_format": "kicad",
    "sheets": {"amp": {"file": "amp.kicad_sch"}},
    "ops": [
        {"op": "add_sheet", "name": "Amp", "file": "amp.kicad_sch",
         "at": [5000, 2000], "size": [1000, 800],
         "pins": [{"name": "OUT", "type": "output", "side": "left",
                   "offset_mil": 200}]},
        {"op": "place_component", "sheet": "amp", "lib_id": "Device:R",
         "designator": "R101", "x_mil": 2000, "y_mil": 2000, "value": "10k"},
        {"op": "add_net_label", "sheet": "amp", "at": "R101.1", "name": "OUT",
         "scope": "hierarchical"},
        {"op": "add_net_label", "sheet": "amp", "at": "R101.2",
         "name": "SHARED", "scope": "global"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 8000, "y_mil": 2000, "value": "1k"},
        {"op": "add_net_label", "at": "R2.1", "name": "SHARED",
         "scope": "global"},
    ],
}


def _setup(tmp_path: Path, capsys) -> tuple[Path, Path, str]:
    main_sch = tmp_path / "main.kicad_sch"
    amp = tmp_path / "amp.kicad_sch"
    assert main(["new", str(main_sch)]) == EXIT["OK"]
    assert main(["new", str(amp)]) == EXIT["OK"]
    capsys.readouterr()
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(ROUTED_OPS), encoding="utf-8")
    return main_sch, amp, str(ops)


def _draw(main_sch: Path, ops: str, *extra: str) -> int:
    return main(["draw", str(main_sch), "--ops", ops, "--json", "--no-erc",
                 *_sym_flags(), *extra])


# --------------------------------------------------------------------------- #
# envelope validation
# --------------------------------------------------------------------------- #
def test_undeclared_sheet_tag_is_refused(tmp_path, capsys):
    main_sch, _amp, _ = _setup(tmp_path, capsys)
    ops = tmp_path / "bad.json"
    ops.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "ops": [{"op": "add_text", "sheet": "nope", "at": [100, 100],
                 "text": "x"}]}), encoding="utf-8")
    assert _draw(main_sch, str(ops)) == EXIT["OPLIST"]
    d = json.loads(capsys.readouterr().out)
    assert d["status"] == "refused"
    assert any(r["error_code"] == "SHEET_UNKNOWN" for r in d["ops"])


def test_declared_sheet_file_must_exist(tmp_path, capsys):
    main_sch, amp, ops = _setup(tmp_path, capsys)
    amp.unlink()
    assert _draw(main_sch, ops, "--apply") == EXIT["OPLIST"]
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "SHEET_UNKNOWN"


# --------------------------------------------------------------------------- #
# the transaction
# --------------------------------------------------------------------------- #
def test_one_oplist_edits_root_and_child_atomically(tmp_path, capsys):
    main_sch, amp, ops = _setup(tmp_path, capsys)
    assert _draw(main_sch, ops, "--apply", "--strict-nets") == EXIT["OK"]
    d = json.loads(capsys.readouterr().out)
    assert d["applied"] is True
    assert d["run_id"]
    written = {f["file"].rsplit("/", 1)[-1] for f in d["files"] if f["written"]}
    assert written == {"main.kicad_sch", "amp.kicad_sch"}

    # cross-file nets are real when read from the root
    assert main(["nets", str(main_sch), "--json"]) == EXIT["OK"]
    nets = {n["name"]: frozenset(n["members"])
            for n in json.loads(capsys.readouterr().out)["nets"]}
    assert nets["SHARED"] == {"R101.2", "R2.1"}
    assert nets["OUT"] == {"R101.1"}

    # the run backup + manifest exist
    run_dir = (main_sch.parent / ".akcli" / "backups" / "runs" / d["run_id"])
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    assert {m["file"].rsplit("/", 1)[-1] for m in manifest["files"]} == written


def test_routed_apply_twice_is_byte_identical(tmp_path, capsys):
    main_sch, amp, ops = _setup(tmp_path, capsys)
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    first = (main_sch.read_bytes(), amp.read_bytes())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    assert (main_sch.read_bytes(), amp.read_bytes()) == first


def test_dry_run_touches_nothing(tmp_path, capsys):
    main_sch, amp, ops = _setup(tmp_path, capsys)
    before = (main_sch.read_bytes(), amp.read_bytes())
    assert _draw(main_sch, ops) == EXIT["OK"]
    d = json.loads(capsys.readouterr().out)
    assert d["status"] == "dry-run"
    assert all(not f["written"] for f in d["files"])
    # the net diff was still computed over the WOULD-BE tree
    assert any("SHARED" in ln for ln in d["net_diff"]["lines"])
    assert (main_sch.read_bytes(), amp.read_bytes()) == before


def test_commit_failure_rolls_every_file_back(tmp_path, capsys, monkeypatch):
    main_sch, amp, ops_file = _setup(tmp_path, capsys)
    before = (main_sch.read_bytes(), amp.read_bytes())

    real_commit = atomic.commit
    calls = {"n": 0}

    def flaky_commit(pw, backup_dir=None, backup_depth=atomic.DEFAULT_BACKUP_DEPTH):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated AV lock on the second replace")
        return real_commit(pw, backup_dir, backup_depth)

    monkeypatch.setattr(atomic, "commit", flaky_commit)
    with pytest.raises(AkcliError) as ei:
        txn.apply_project(json.loads(Path(ops_file).read_text(encoding="utf-8")),
                          main_sch, apply=True,
                          sources=[SYMBOLS[0]])
    assert ei.value.code == "VERIFY_FAILED"
    assert "rolled back" in ei.value.message
    # NOTHING changed: the first file's promotion was reverted from the run backup
    assert (main_sch.read_bytes(), amp.read_bytes()) == before


# --------------------------------------------------------------------------- #
# undo --run
# --------------------------------------------------------------------------- #
def _applied_run(tmp_path, capsys):
    main_sch, amp, ops = _setup(tmp_path, capsys)
    before = (main_sch.read_bytes(), amp.read_bytes())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    rid = json.loads(capsys.readouterr().out)["run_id"]
    return main_sch, amp, rid, before


def test_undo_run_restores_every_file_and_redoes(tmp_path, capsys):
    main_sch, amp, rid, before = _applied_run(tmp_path, capsys)
    after = (main_sch.read_bytes(), amp.read_bytes())
    assert after != before

    # dry-run lists, touches nothing
    assert main(["undo", str(main_sch), "--run", rid, "--json"]) == EXIT["OK"]
    d = json.loads(capsys.readouterr().out)
    assert d["applied"] is False
    assert (main_sch.read_bytes(), amp.read_bytes()) == after

    # apply restores BOTH files to their exact pre-transaction bytes
    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert (main_sch.read_bytes(), amp.read_bytes()) == before

    # same command again = redo
    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert (main_sch.read_bytes(), amp.read_bytes()) == after


def test_undo_run_refuses_after_a_later_edit(tmp_path, capsys):
    main_sch, amp, rid, _before = _applied_run(tmp_path, capsys)
    # a later single-file edit changes amp: the swap would discard it
    later = tmp_path / "later.json"
    later.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "ops": [{"op": "add_text", "at": [400, 400], "text": "later edit"}],
    }), encoding="utf-8")
    assert main(["draw", str(amp), "--ops", str(later), "--apply", "--no-erc",
                 *_sym_flags()]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["undo", str(main_sch), "--run", rid,
                 "--apply"]) == EXIT["OPLIST"]


def test_undo_run_unknown_id_is_not_found(tmp_path, capsys):
    main_sch, _amp, _rid, _before = _applied_run(tmp_path, capsys)
    assert main(["undo", str(main_sch), "--run", "feedbeef0000"]) == \
        EXIT["NOT_FOUND"]
