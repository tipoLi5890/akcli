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
    "protocol_version": 2,
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


# --------------------------------------------------------------------------- #
# the write paths beyond draw/arrange/undo: new --force, relink
# (library repair / import-altium and jlc add are covered next to their own
#  fixtures: tests/test_library_workspace.py, tests/test_jlc_add_cli.py)
# --------------------------------------------------------------------------- #
def _locked_envelope(capsys, argv: list[str]) -> dict:
    """Run ``argv --json`` expecting the TARGET_LOCKED refusal; return its
    ``error`` object."""
    code = main([*argv, "--json"])
    io = capsys.readouterr()
    assert code == 6, io.err
    err = json.loads(io.out)["error"]
    assert err["code"] == "TARGET_LOCKED" and err["exit"] == 6
    assert "--allow-open" in err["remediation"]
    assert "TARGET_LOCKED" in io.err                  # humans still get the line
    return err


def test_new_force_refuses_a_gui_locked_sheet(sch, capsys):
    # `new --force` REPLACES the sheet: under an open GUI that is the same
    # losing race as a draw (the GUI's later save restores the old sheet).
    lck = _lock(sch)
    before = sch.read_bytes()
    err = _locked_envelope(capsys, ["new", str(sch), "--force"])
    assert lck.name in err["message"]
    assert sch.read_bytes() == before                 # nothing written


def test_new_force_allow_open_overwrites(sch, capsys):
    _lock(sch)
    before = sch.read_bytes()
    assert main(["new", str(sch), "--force", "--allow-open", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    assert sch.read_bytes() != before


def test_new_without_force_reports_the_existing_file_first(sch, capsys):
    # no --force = no write: the lock is not the reason, and not the message
    _lock(sch)
    assert main(["new", str(sch)]) == 2
    err = capsys.readouterr().err
    assert "--force" in err and "TARGET_LOCKED" not in err


def test_new_ignores_a_stray_lock_when_the_sheet_does_not_exist(tmp_path, capsys):
    # Only an EXISTING file can be open in KiCad.
    target = tmp_path / "fresh.kicad_sch"
    _lock(target)
    assert main(["new", str(target)]) == 0
    assert target.is_file()


@pytest.fixture()
def stale_cache(tmp_path) -> tuple[Path, Path]:
    """(schematic with one stale lib_symbols entry, fresh library dir)."""
    from tests.test_relink import _lib_text, _sch_text

    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / "Fake.kicad_sym").write_text(_lib_text(), encoding="utf-8",
                                           newline="\n")
    target = tmp_path / "board.kicad_sch"
    target.write_text(_sch_text(), encoding="utf-8", newline="\n")
    return target, libdir


@pytest.mark.parametrize("verb", [["relink-symbols"], ["library", "relink"]],
                         ids=["relink-symbols", "library-relink"])
def test_relink_cli_refuses_then_allow_open_applies(stale_cache, capsys, verb):
    # relink.apply always honoured allow_open; the CLI never passed it, so a
    # locked target was a refusal with no documented way through.
    target, libdir = stale_cache
    lck = _lock(target)
    before = target.read_bytes()
    argv = [*verb, str(target), "--libs", str(libdir), "--only", "Fake",
            "--apply"]

    err = _locked_envelope(capsys, argv)
    assert lck.name in err["message"]
    assert target.read_bytes() == before              # nothing written

    # the dry-run preview is never blocked
    assert main(argv[:-1]) == 0
    assert "re-run with --apply" in capsys.readouterr().out

    assert main([*argv, "--allow-open", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is True and doc["replaced"] == ["Fake:R2"]
    assert target.read_bytes() != before


@pytest.mark.parametrize("argv", [
    ["draw", "b.kicad_sch"], ["arrange", "b.kicad_sch"], ["undo", "b.kicad_sch"],
    ["groups", "b.kicad_sch"], ["new", "b.kicad_sch", "--force"],
    ["relink-symbols", "b.kicad_sch"], ["library", "relink", "b.kicad_sch"],
    ["library", "repair", "."], ["library", "import-altium", "p.PcbLib"],
    ["jlc", "add", "C25804"],
], ids=lambda a: " ".join(a[:2] if a[0] in ("library", "jlc") else a[:1]))
def test_gui_gated_write_commands_offer_allow_open(argv):
    # A refusal the user cannot override is a dead end: these commands gate a
    # write on the KiCad lock and all take the same explicit-acceptance flag.
    # (`library new-symbol` and `project new/init --force` refuse too, with no
    # override yet — add them here when they grow the flag.)
    from akcli.cli import build_parser

    args = build_parser().parse_args([*argv, "--allow-open"])
    assert args.allow_open is True
    assert build_parser().parse_args(argv).allow_open is False
