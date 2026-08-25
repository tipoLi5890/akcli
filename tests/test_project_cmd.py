"""``akcli project new`` / ``akcli project init`` — the CLI surface.

The engine's semantics live in ``test_scaffold.py``; this file pins the
*contract an agent sees*: exit codes, the stdout(data)/stderr(logs) split, the
stamped ``--json`` envelope, and the promise that ``--dry-run`` exits 0 while
writing nothing — including when it is reporting conflicts, which is exactly
the case a "preview" is most likely to get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.writers import blank
from tests._cli_harness import json_of, run_cli


def _lone_sheet(tmp_path: Path, name: str = "board") -> Path:
    sch = tmp_path / f"{name}.kicad_sch"
    sch.write_text(blank.blank_sch("A4", None), encoding="utf-8", newline="\n")
    return sch


def _files(directory: Path) -> set[str]:
    return {f.name for f in directory.iterdir()} if directory.exists() else set()


_REPO = Path(__file__).resolve().parent.parent


def _sym_flags() -> list[str]:
    """The committed Device/power libraries, as ``--symbols`` flags.

    ``tests/conftest.py`` pins ``KICAD_CONFIG_HOME`` at a synthetic global
    lib-table whose URIs resolve to nothing, so a hermetic run must name the
    libraries explicitly (same idiom as ``tests/test_examples.py``).
    """
    return [a for s in ("Device.kicad_sym", "power.kicad_sym")
            for a in ("--symbols",
                      str(_REPO / "tests/fixtures/kicad/symbols" / s))]


# --------------------------------------------------------------------------- #
# project new
# --------------------------------------------------------------------------- #
def test_new_creates_a_project(tmp_path):
    d = tmp_path / "demo"
    rc, out, err = run_cli(["project", "new", str(d), "--title", "Demo"])
    assert rc == 0
    assert _files(d) >= {"demo.kicad_pro", "demo.kicad_sch", "demo.kicad_pcb",
                         "sym-lib-table", "fp-lib-table", ".gitignore"}
    # stdout is the data (one line per file + a status line); stderr silent
    assert "created   demo.kicad_pro" in out
    assert out.strip().splitlines()[-1].startswith("status: applied")
    assert err == ""


def test_new_json_is_stamped_and_posix(tmp_path):
    d = tmp_path / "demo"
    doc = json_of(["project", "new", str(d), "--json"], expect=0)
    assert doc["schema_version"] == "1"
    assert doc["command"] == "project new" and doc["mode"] == "new"
    assert doc["applied"] is True and doc["dry_run"] is False
    assert doc["status"] == "applied"
    assert doc["directory"] == d.as_posix()
    assert doc["project_file"].endswith("/demo.kicad_pro")
    assert [f["action"] for f in doc["files"]] == ["created"] * 6
    assert doc["root_uuid"]
    # every path is posix — no backslash can reach an agent's parser
    assert "\\" not in json.dumps(doc)


def test_new_dry_run_writes_nothing_and_exits_zero(tmp_path):
    d = tmp_path / "demo"
    doc = json_of(["project", "new", str(d), "--dry-run", "--json"], expect=0)
    assert doc["applied"] is False and doc["status"] == "dry-run"
    assert [f["action"] for f in doc["files"]] == ["created"] * 6
    assert not d.exists(), "dry-run must not even create the directory"


def test_new_refuses_an_existing_project(tmp_path):
    d = tmp_path / "demo"
    assert run_cli(["project", "new", str(d)])[0] == 0
    rc, out, err = run_cli(["project", "new", str(d)])
    assert rc == 2
    assert "PROJECT_EXISTS" in err
    assert "project init" in err          # names the verb that WOULD work
    assert out == ""


def test_new_conflict_json_envelope(tmp_path):
    d = tmp_path / "demo"
    run_cli(["project", "new", str(d)])
    doc = json_of(["project", "new", str(d), "--json"], expect=2)
    assert doc["error"]["code"] == "PROJECT_EXISTS"
    assert doc["error"]["exit"] == 2
    assert doc["error"]["remediation"]


def test_new_dry_run_reports_conflicts_but_still_exits_zero(tmp_path):
    d = tmp_path / "demo"
    run_cli(["project", "new", str(d)])
    doc = json_of(["project", "new", str(d), "--dry-run", "--json"], expect=0)
    assert set(doc["conflicts"]) >= {"demo.kicad_pro", "demo.kicad_sch"}
    assert doc["applied"] is False


def test_new_force_overwrites_with_a_backup(tmp_path):
    d = tmp_path / "demo"
    run_cli(["project", "new", str(d)])
    before = (d / "demo.kicad_sch").read_bytes()
    doc = json_of(["project", "new", str(d), "--force", "--json"], expect=0)
    assert "demo.kicad_sch" in doc["overwritten"]
    # .gitignore is user content: --force does not touch it
    assert doc["kept"] == [".gitignore"]
    assert (d / ".akcli" / "backups" / "demo.kicad_sch.bak").read_bytes() == before


def test_new_opt_out_flags(tmp_path):
    d = tmp_path / "demo"
    doc = json_of(["project", "new", str(d), "--no-pcb", "--no-lib-tables",
                   "--no-gitignore", "--json"], expect=0)
    assert [f["kind"] for f in doc["files"]] == ["pro", "sch"]
    assert _files(d) == {"demo.kicad_pro", "demo.kicad_sch", ".akcli"}


def test_new_sheets_creates_children_and_hints(tmp_path):
    d = tmp_path / "demo"
    rc, out, err = run_cli(["project", "new", str(d), "--sheets", "power,mcu"])
    assert rc == 0
    assert (d / "power.kicad_sch").is_file() and (d / "mcu.kicad_sch").is_file()
    # the hint is a LOG (stderr): the children exist but nothing instantiates
    # them yet — that is an op-list's job.
    assert "add_sheet" in err and "add_sheet" not in out


def test_new_sheets_hint_is_suppressed_under_json(tmp_path):
    d = tmp_path / "demo"
    rc, _out, err = run_cli(["project", "new", str(d), "--sheets", "power",
                             "--json"])
    assert rc == 0 and err == ""


def test_new_rejects_an_unknown_paper(tmp_path):
    rc, out, err = run_cli(["project", "new", str(tmp_path / "d"),
                            "--paper", "bogus"])
    assert rc == 2
    assert "unknown paper" in err and out == ""


def test_new_name_overrides_the_directory(tmp_path):
    d = tmp_path / "some-dir"
    doc = json_of(["project", "new", str(d), "--name", "widget", "--json"],
                  expect=0)
    assert doc["name"] == "widget"
    assert (d / "widget.kicad_pro").is_file()


def test_new_title_reaches_the_root_sheet(tmp_path):
    d = tmp_path / "demo"
    run_cli(["project", "new", str(d), "--title", "Hello Board"])
    assert '(title "Hello Board")' in \
        (d / "demo.kicad_sch").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# project init
# --------------------------------------------------------------------------- #
def test_init_completes_a_lone_sheet(tmp_path):
    sch = _lone_sheet(tmp_path)
    before = sch.read_bytes()
    doc = json_of(["project", "init", str(tmp_path), "--json"], expect=0)
    assert doc["mode"] == "init" and doc["status"] == "applied"
    assert doc["kept"] == ["board.kicad_sch"]
    assert set(doc["created"]) == {"board.kicad_pro", "board.kicad_pcb",
                                   "sym-lib-table", "fp-lib-table",
                                   ".gitignore"}
    assert sch.read_bytes() == before


def test_init_is_idempotent(tmp_path):
    _lone_sheet(tmp_path)
    assert run_cli(["project", "init", str(tmp_path)])[0] == 0
    doc = json_of(["project", "init", str(tmp_path), "--json"], expect=0)
    assert doc["status"] == "unchanged"
    assert doc["created"] == [] and doc["overwritten"] == []
    assert doc["applied"] is False


def test_init_has_no_force_flag(tmp_path):
    # argparse rejects it before dispatch: `init` is additive BY CONSTRUCTION,
    # not by a runtime check somebody could relax later.
    with pytest.raises(SystemExit) as exc:
        run_cli(["project", "init", str(tmp_path), "--force"])
    assert exc.value.code == 2


def test_init_without_a_schematic_is_exit_4(tmp_path):
    rc, out, err = run_cli(["project", "init", str(tmp_path)])
    assert rc == 4
    assert "PROJECT_FILE_MISSING" in err and out == ""


def test_init_ambiguous_roots_then_root_flag(tmp_path):
    _lone_sheet(tmp_path, "a")
    _lone_sheet(tmp_path, "b")
    rc, _out, err = run_cli(["project", "init", str(tmp_path)])
    assert rc == 6 and "PROJECT_ROOT_AMBIGUOUS" in err
    doc = json_of(["project", "init", str(tmp_path), "--root",
                   str(tmp_path / "b.kicad_sch"), "--json"], expect=0)
    assert doc["root_sch"].endswith("/b.kicad_sch")
    assert doc["name"] == "b"


def test_init_refuses_a_malformed_project_file(tmp_path):
    _lone_sheet(tmp_path)
    (tmp_path / "board.kicad_pro").write_text("{ nope", encoding="utf-8",
                                              newline="\n")
    rc, _out, err = run_cli(["project", "init", str(tmp_path)])
    assert rc == 3 and "PROJECT_FILE_INVALID" in err


def test_init_dry_run_writes_nothing(tmp_path):
    _lone_sheet(tmp_path)
    before = _files(tmp_path)
    doc = json_of(["project", "init", str(tmp_path), "--dry-run", "--json"],
                  expect=0)
    assert doc["status"] == "dry-run" and doc["applied"] is False
    assert _files(tmp_path) - before <= {".akcli"}   # journal only


# --------------------------------------------------------------------------- #
# discoverability
# --------------------------------------------------------------------------- #
def test_bare_project_asks_for_a_subcommand():
    rc, out, err = run_cli(["project"])
    assert rc == 2
    assert "new" in err and "init" in err and out == ""
    # the read verbs are advertised in the same breath as the write ones
    assert "info" in err and "check" in err


def test_capabilities_lists_the_project_verbs():
    from akcli.commands.capabilities import build_manifest

    by_name = {c["name"]: c for c in build_manifest()["commands"]}
    subs = {s["name"] for s in by_name["project"]["subcommands"]}
    assert {"new", "init", "info", "check"} <= subs


def test_new_hints_when_a_sheet_has_no_project(tmp_path):
    """`akcli new` on a bare directory nudges toward `project init`."""
    rc, out, err = run_cli(["new", str(tmp_path / "x.kicad_sch")])
    assert rc == 0
    assert "project init" in err
    assert "project init" not in out
    # ...and stays quiet once the directory IS a project
    run_cli(["project", "init", str(tmp_path)])
    rc, _out, err = run_cli(["new", str(tmp_path / "y.kicad_sch")])
    assert rc == 0 and "project init" not in err


def test_new_hint_is_suppressed_under_json(tmp_path):
    rc, _out, err = run_cli(["new", str(tmp_path / "x.kicad_sch"), "--json"])
    assert rc == 0 and err == ""


# --------------------------------------------------------------------------- #
# project info
# --------------------------------------------------------------------------- #
def _scaffold(tmp_path: Path, *extra: str) -> Path:
    d = tmp_path / "demo"
    rc, _out, err = run_cli(["project", "new", str(d), *extra])
    assert rc == 0, err
    return d


def test_info_reports_the_binding(tmp_path):
    d = _scaffold(tmp_path)
    rc, out, err = run_cli(["project", "info", str(d)])
    assert rc == 0 and err == ""
    assert "demo.kicad_pro" in out and "[ok]" in out
    assert "demo.kicad_sch" in out


def test_info_json_shape_is_stamped_and_posix(tmp_path):
    d = _scaffold(tmp_path)
    doc = json_of(["project", "info", str(d), "--json"], expect=0)
    assert doc["schema_version"] == "1"
    assert doc["name"] == "demo" and doc["ok"] is True and doc["error"] is None
    assert doc["project_file"] == (d / "demo.kicad_pro").as_posix()
    # the pro's recorded root uuid IS the root schematic's own uuid
    assert doc["root_sheet"]["file"] == (d / "demo.kicad_sch").as_posix()
    assert doc["root_sheet"]["uuid"]
    assert doc["sheet_tree"] == [(d / "demo.kicad_sch").as_posix()]
    assert doc["boards"] == [(d / "demo.kicad_pcb").as_posix()]
    assert doc["lib_tables"]["sym"].endswith("/sym-lib-table")
    assert doc["net_classes"] == [] and doc["text_variables"] == {}
    assert doc["pinned_libs"] == {"symbol": [], "footprint": []}
    assert "\\" not in json.dumps(doc)


def test_info_accepts_the_project_file_itself(tmp_path):
    d = _scaffold(tmp_path)
    doc = json_of(["project", "info", str(d / "demo.kicad_pro"), "--json"],
                  expect=0)
    assert doc["name"] == "demo"


def test_info_without_a_project_is_exit_4(tmp_path):
    rc, out, err = run_cli(["project", "info", str(tmp_path)])
    assert rc == 4 and "PROJECT_FILE_MISSING" in err and out == ""


def test_info_on_a_schematic_path_is_a_missing_project(tmp_path):
    sch = _lone_sheet(tmp_path)
    rc, _out, err = run_cli(["project", "info", str(sch)])
    assert rc == 4 and "PROJECT_FILE_MISSING" in err


# --------------------------------------------------------------------------- #
# project check
# --------------------------------------------------------------------------- #
def _codes(doc: dict) -> list[str]:
    return [f["code"] for f in doc["findings"]]


def test_check_on_a_fresh_scaffold_is_clean(tmp_path):
    """The gate is calibrated against what `project new` itself produces."""
    d = _scaffold(tmp_path)
    rc, out, err = run_cli(["project", "check", str(d)])
    assert rc == 0 and err == ""
    assert "no findings" in out


def test_check_json_is_stamped_and_empty_on_a_scaffold(tmp_path):
    d = _scaffold(tmp_path)
    doc = json_of(["project", "check", str(d), "--json"], expect=0)
    assert doc["schema_version"] == "1"
    assert doc["findings"] == []
    assert doc["project_file"] == (d / "demo.kicad_pro").as_posix()


def test_check_missing_board_is_a_note_that_does_not_gate(tmp_path):
    d = _scaffold(tmp_path)
    (d / "demo.kicad_pcb").unlink()
    doc = json_of(["project", "check", str(d), "--json"], expect=0)
    assert _codes(doc) == ["PROJECT_PCB_MISSING"]
    assert doc["findings"][0]["severity"] == "note"


def test_check_no_gitignore_is_a_note_that_does_not_gate(tmp_path):
    d = _scaffold(tmp_path, "--no-gitignore")
    doc = json_of(["project", "check", str(d), "--json"], expect=0)
    assert "PROJECT_STATE_UNIGNORED" in _codes(doc)
    assert {f["severity"] for f in doc["findings"]} == {"note"}


def test_check_regenerated_root_is_a_mismatch_error(tmp_path):
    """`akcli new --force` mints a fresh root uuid; the pro still names the old
    one — the project would open a design this file no longer is."""
    d = _scaffold(tmp_path)
    assert run_cli(["new", str(d / "demo.kicad_sch"), "--force"])[0] == 0
    doc = json_of(["project", "check", str(d), "--json"], expect=1)
    assert "PROJECT_ROOT_SHEET_MISMATCH" in _codes(doc)
    assert [f["severity"] for f in doc["findings"]
            if f["code"] == "PROJECT_ROOT_SHEET_MISMATCH"] == ["error"]


def test_check_fail_on_never_always_exits_zero(tmp_path):
    d = _scaffold(tmp_path)
    assert run_cli(["new", str(d / "demo.kicad_sch"), "--force"])[0] == 0
    assert run_cli(["project", "check", str(d)])[0] == 1
    assert run_cli(["project", "check", str(d), "--fail-on", "never"])[0] == 0


def test_check_missing_root_schematic_is_an_error(tmp_path):
    d = _scaffold(tmp_path)
    (d / "demo.kicad_sch").unlink()
    doc = json_of(["project", "check", str(d), "--json"], expect=1)
    assert "PROJECT_ROOT_SHEET_MISSING" in _codes(doc)


def test_check_empty_sheets_list_is_a_missing_root_binding(tmp_path):
    d = _scaffold(tmp_path)
    (d / "demo.kicad_pro").write_text(json.dumps({"sheets": []}),
                                      encoding="utf-8", newline="\n")
    doc = json_of(["project", "check", str(d), "--json"], expect=1)
    assert "PROJECT_ROOT_SHEET_MISSING" in _codes(doc)


def test_check_stale_meta_filename_is_a_note(tmp_path):
    d = _scaffold(tmp_path)
    pro = d / "demo.kicad_pro"
    doc_json = json.loads(pro.read_text(encoding="utf-8"))
    doc_json["meta"]["filename"] = "old-name.kicad_pro"
    pro.write_text(json.dumps(doc_json), encoding="utf-8", newline="\n")
    doc = json_of(["project", "check", str(d), "--json"], expect=0)
    assert "PROJECT_META_FILENAME_STALE" in _codes(doc)


def test_check_truncated_pro_is_malformed_but_read_still_works(tmp_path):
    """The degrade contract: a broken .kicad_pro is REPORTED, never fatal.

    `akcli read` on the root schematic must be untouched by it — the project
    file is not on any read path, which is exactly why `check` has to exist.
    """
    d = _scaffold(tmp_path)
    (d / "demo.kicad_pro").write_text('{"sheets": [["abc"',
                                      encoding="utf-8", newline="\n")
    doc = json_of(["project", "check", str(d), "--json"], expect=1)
    assert "PROJECT_FILE_MALFORMED" in _codes(doc)
    assert [f["severity"] for f in doc["findings"]
            if f["code"] == "PROJECT_FILE_MALFORMED"] == ["warning"]
    # the root binding is NOT re-reported: one defect, one finding
    assert "PROJECT_ROOT_SHEET_MISSING" not in _codes(doc)
    assert run_cli(["read", str(d / "demo.kicad_sch"), "--json"])[0] == 0
    # ...and so does `info`, which reports the damage instead of raising
    info = json_of(["project", "info", str(d), "--json"], expect=0)
    assert info["ok"] is False and info["error"]


def test_check_missing_child_sheet_is_a_sheet_file_missing(tmp_path):
    d = _scaffold(tmp_path, "--sheets", "power")
    ops = {"protocol_version": 2, "target_format": "kicad",
           "ops": [{"op": "add_sheet", "file": "power.kicad_sch",
                    "name": "Power", "at": [1000, 1000],
                    "size": [1000, 800]}]}
    ops_file = d / "sheet-ops.json"
    ops_file.write_text(json.dumps(ops), encoding="utf-8", newline="\n")
    assert run_cli(["draw", str(d / "demo.kicad_sch"), "--ops", str(ops_file),
                    "--apply", "--no-erc"])[0] == 0
    assert json_of(["project", "check", str(d), "--json"], expect=0)["findings"] == []
    (d / "power.kicad_sch").unlink()
    doc = json_of(["project", "check", str(d), "--json"], expect=1)
    assert "SHEET_FILE_MISSING" in _codes(doc)


def test_check_without_a_project_is_exit_4(tmp_path):
    rc, out, err = run_cli(["project", "check", str(tmp_path)])
    assert rc == 4 and "PROJECT_FILE_MISSING" in err and out == ""


# --------------------------------------------------------------------------- #
# project new --example
# --------------------------------------------------------------------------- #
def test_new_example_drops_in_a_runnable_op_list(tmp_path):
    d = tmp_path / "demo"
    rc, out, err = run_cli(["project", "new", str(d), "--example",
                            "voltage-divider"])
    assert rc == 0
    assert "created   ops.json" in out
    assert "akcli draw demo.kicad_sch --ops ops.json --apply" in err
    ops = d / "ops.json"
    assert ops.is_file()
    # it is a real op-list, not a placeholder: the validator accepts it...
    assert run_cli(["ops", "validate", str(ops)])[0] == 0
    # ...and it plans against the root schematic the same command just wrote.
    # (`--symbols` because the suite runs against a hermetic KICAD_CONFIG_HOME
    # whose nicknames resolve to nothing; a real install needs no flag.)
    rc, _out, err = run_cli(["plan", str(d / "demo.kicad_sch"), "--ops",
                             str(ops), "--json", *_sym_flags()])
    assert rc == 0, err


def test_new_example_matches_the_repo_corpus(tmp_path):
    d = tmp_path / "demo"
    assert run_cli(["project", "new", str(d), "--example",
                    "voltage-divider"])[0] == 0
    repo = Path(__file__).resolve().parent.parent
    assert (d / "ops.json").read_bytes() == (
        repo / "examples" / "circuits" / "voltage-divider" / "ops.json"
    ).read_bytes()


def test_new_example_json_lists_the_file(tmp_path):
    d = tmp_path / "demo"
    doc = json_of(["project", "new", str(d), "--example", "voltage-divider",
                   "--json"], expect=0)
    kinds = {f["name"]: f["kind"] for f in doc["files"]}
    assert kinds["ops.json"] == "ops"
    assert "ops.json" in doc["created"]


def test_new_example_never_clobbers_an_existing_ops_json(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "ops.json").write_text("mine", encoding="utf-8", newline="\n")
    rc, _out, err = run_cli(["project", "new", str(d), "--example",
                             "voltage-divider"])
    assert rc == 2 and "PROJECT_EXISTS" in err
    assert (d / "ops.json").read_text(encoding="utf-8") == "mine"


def test_new_unknown_example_is_a_usage_error(tmp_path):
    rc, out, err = run_cli(["project", "new", str(tmp_path / "d"),
                            "--example", "nope"])
    assert rc == 2 and "unknown example" in err and out == ""
    assert "voltage-divider" in err          # names what IS available
    assert not (tmp_path / "d").exists()


def test_init_has_no_example_flag(tmp_path):
    # `--example` seeds a NEW design; dropping an op-list into somebody's
    # existing project is not "add what is missing".
    with pytest.raises(SystemExit) as exc:
        run_cli(["project", "init", str(tmp_path), "--example",
                 "voltage-divider"])
    assert exc.value.code == 2
