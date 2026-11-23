"""Tests for :mod:`akcli.drivers.kicad_cli` — optional ERC wrapper (SPEC §3.7).

``kicad-cli`` is an **optional** secondary verifier; the primary gate is the pure
Python :mod:`..writers.connectivity`. The behavioural tests therefore ``skipif`` the
tool is not installed (the dev/CI mac has no KiCad), while the *graceful-degradation*
tests — that absence is non-fatal — run everywhere.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from akcli import safety
from akcli.drivers import kicad_cli
from akcli.errors import AkcliError

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"
# DRC ground truth: a minimal board + project file and the REAL report
# kicad-cli 10.0.4 wrote for it (see tests/fixtures/kicad/README.md).
DRC_FIX = FIX / "drc_demo"
DRC_BOARD = DRC_FIX / "drc_demo.kicad_pcb"
DRC_REPORT = DRC_FIX / "drc_demo.drc.kicad10.json"

_HAVE = kicad_cli.find() is not None
needs_cli = pytest.mark.skipif(not _HAVE, reason="kicad-cli not installed")


def _force_absent(monkeypatch):
    """Blind every rung of the discovery ladder."""
    monkeypatch.delenv("KICAD_CLI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(kicad_cli, "_FALLBACKS", ())
    monkeypatch.setattr(kicad_cli, "_WIN_GLOB", "/nonexistent/*/kicad-cli")


# --------------------------------------------------------------------------- #
# graceful degradation (runs with or without kicad-cli)
# --------------------------------------------------------------------------- #
def test_available_matches_find():
    assert isinstance(kicad_cli.available(), bool)
    assert kicad_cli.available() == (kicad_cli.find() is not None)


def test_find_env_override_wins(monkeypatch, tmp_path):
    stub = tmp_path / "kicad-cli"
    stub.write_text("#!/bin/sh\n")
    monkeypatch.setenv("KICAD_CLI", str(stub))
    assert kicad_cli.find() == str(stub)
    # env set but pointing nowhere: explicit None, never a silent fallback
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing"))
    assert kicad_cli.find() is None


def test_find_fallback_rung(monkeypatch, tmp_path):
    _force_absent(monkeypatch)
    bundle = tmp_path / "bundled-kicad-cli"
    bundle.write_text("")
    monkeypatch.setattr(kicad_cli, "_FALLBACKS", (str(bundle),))
    assert kicad_cli.find() == str(bundle)


def test_windows_glob_prefers_newest_version():
    key = kicad_cli._version_key
    paths = [r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
             r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"]
    assert max(paths, key=key).startswith(r"C:\Program Files\KiCad\10.0")


def test_absent_tool_is_non_fatal(monkeypatch):
    # Force "tool absent" and assert every entry point degrades to None (no raise).
    _force_absent(monkeypatch)
    assert kicad_cli.available() is False
    assert kicad_cli.version() is None
    assert kicad_cli.version_text() is None
    assert kicad_cli.erc(str(V8)) is None
    assert kicad_cli.netlist(str(V8)) is None
    assert kicad_cli.drc(str(DRC_BOARD)) is None


def test_parse_version_helper():
    assert kicad_cli._parse_version("8.0.4") == (8, 0, 4)
    assert kicad_cli._parse_version("kicad-cli 7.0.11+foo") == (7, 0, 11)
    assert kicad_cli._parse_version("no version here") is None


# --------------------------------------------------------------------------- #
# real kicad-cli (skipped when not installed)
# --------------------------------------------------------------------------- #
@needs_cli
def test_version_when_installed():
    v = kicad_cli.version()
    assert v is not None and isinstance(v[0], int)


@needs_cli
def test_erc_when_installed():
    rep = kicad_cli.erc(str(V8))
    assert rep is None or isinstance(rep, dict)
    if isinstance(rep, dict):
        assert "exit_code" in rep


@needs_cli
def test_netlist_when_installed():
    rep = kicad_cli.netlist(str(V8))
    assert rep is None or isinstance(rep, dict)


# --------------------------------------------------------------------------- #
# pcb drc — staging (pure Python; runs everywhere, Windows included)
# --------------------------------------------------------------------------- #
def _tree(root: Path) -> dict[str, str]:
    """``{relative posix path: sha256}`` of every file under ``root``."""
    return {p.relative_to(root).as_posix():
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _drc_project(tmp_path: Path, name: str = "proj") -> Path:
    """A throwaway copy of the DRC fixture project; returns its board."""
    d = tmp_path / name
    d.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copyfile(DRC_FIX / f"drc_demo{suffix}", d / f"drc_demo{suffix}")
    return d / "drc_demo.kicad_pcb"


def test_stage_board_copies_exactly_what_drc_reads(tmp_path):
    board = _drc_project(tmp_path)
    proj = board.parent
    (proj / "drc_demo.kicad_dru").write_bytes(b"(version 1)\n")
    (proj / "fp-lib-table").write_bytes(
        b'(fp_lib_table\r\n  (lib (name "demo")(type "KiCad")'
        b'(uri "${KIPRJMOD}/libs/demo.pretty")(options "")(descr ""))\r\n)\r\n')
    # present beside the board, deliberately NOT staged:
    (proj / "drc_demo.kicad_prl").write_bytes(b"{}")
    (proj / "drc_demo.kicad_sch").write_bytes(b"(kicad_sch)")
    (proj / "sym-lib-table").write_bytes(b"(sym_lib_table)")
    before = _tree(proj)

    work = tmp_path / "work"
    work.mkdir()
    staged = kicad_cli._stage_board(board, work)

    assert staged == work / "drc_demo.kicad_pcb"       # same file name
    assert sorted(p.name for p in work.iterdir()) == [
        "drc_demo.kicad_dru", "drc_demo.kicad_pcb", "drc_demo.kicad_pro",
        "fp-lib-table"]
    for name in ("drc_demo.kicad_pcb", "drc_demo.kicad_pro",
                 "drc_demo.kicad_dru"):
        assert (work / name).read_bytes() == (proj / name).read_bytes()
    # ${KIPRJMOD} now names the ORIGINAL project dir (forward slashes on every
    # platform), and nothing else in the table changed — CRLF bytes included.
    table = (work / "fp-lib-table").read_bytes()
    assert b"${KIPRJMOD}" not in table
    assert _staged_uris(table) == [f"{proj.as_posix()}/libs/demo.pretty"]
    assert table.count(b"\r\n") == 3
    assert table.startswith(b'(fp_lib_table\r\n  (lib (name "demo")')
    assert _tree(proj) == before                       # source untouched


def _staged_uris(table: bytes) -> list[str]:
    from akcli import sexpr

    root = sexpr.parse(table.decode("utf-8"))
    return [lib.find("uri").children[1].value for lib in root.find_all("lib")]


def test_stage_board_rehomes_kiprjmod_as_a_well_formed_atom(tmp_path):
    # A project directory with a space, and one uri the user left unquoted:
    # after the rewrite each uri must still be ONE string atom naming the
    # original directory — naive text substitution would split the bare one.
    board = _drc_project(tmp_path, "my proj")
    proj = board.parent
    (proj / "fp-lib-table").write_bytes(
        b'(fp_lib_table\n  (version 7)\n'
        b'  (lib (name "a")(type "KiCad")(uri "${KIPRJMOD}/a.pretty")(options "")(descr ""))\n'
        b'  (lib (name "b")(type "KiCad")(uri ${KIPRJMOD}/b.pretty)(options "")(descr ""))\n'
        b'  (lib (name "c")(type "KiCad")(uri "${KICAD10_FOOTPRINT_DIR}/c.pretty")(options "")(descr ""))\n'
        b')\n')
    work = tmp_path / "work"
    work.mkdir()
    kicad_cli._stage_board(board, work)
    assert _staged_uris((work / "fp-lib-table").read_bytes()) == [
        f"{proj.as_posix()}/a.pretty",
        f"{proj.as_posix()}/b.pretty",
        "${KICAD10_FOOTPRINT_DIR}/c.pretty",       # other variables: untouched
    ]


@pytest.mark.parametrize("table", [
    b"\xff\xfe not utf-8 ${KIPRJMOD}",
    b'(fp_lib_table (lib (uri "${KIPRJMOD}/x.pretty")',     # unbalanced
    b'(fp_lib_table\r\n  (lib (name "g")(uri "/abs/g.pretty"))\r\n)\r\n',
])
def test_stage_board_copies_a_table_it_cannot_or_need_not_rewrite(tmp_path,
                                                                 table):
    # Not ours to repair (KiCad reports it as it would in place), and a table
    # with nothing to re-home is not re-serialized at all.
    board = _drc_project(tmp_path)
    (board.parent / "fp-lib-table").write_bytes(table)
    work = tmp_path / "work"
    work.mkdir()
    kicad_cli._stage_board(board, work)
    assert (work / "fp-lib-table").read_bytes() == table


def test_stage_board_needs_only_the_board(tmp_path):
    board = tmp_path / "lonely.kicad_pcb"
    board.write_bytes(DRC_BOARD.read_bytes())
    work = tmp_path / "work"
    work.mkdir()
    kicad_cli._stage_board(board, work)
    assert [p.name for p in work.iterdir()] == ["lonely.kicad_pcb"]


# --------------------------------------------------------------------------- #
# pcb drc — the driver contract, with the spawn replaced IN-PROCESS (no stub
# executable: only real binaries can be spawned on Windows)
# --------------------------------------------------------------------------- #
class _FakeRun:
    """Stands in for ``safety.run_subprocess``; records argv, plays a script."""

    def __init__(self, *, returncode=0, report=b"", stderr=b"", raises=None):
        self.returncode, self.report = returncode, report
        self.stderr, self.raises = stderr, raises
        self.argv: list[str] | None = None
        self.staged: dict[str, bytes] = {}

    def __call__(self, argv, timeout, maxout=0):
        self.argv = list(argv)
        board = Path(argv[-1])
        self.staged = {p.name: p.read_bytes() for p in board.parent.iterdir()}
        if self.raises is not None:
            raise self.raises
        if self.report:
            Path(argv[argv.index("--output") + 1]).write_bytes(self.report)
        return subprocess.CompletedProcess(argv, self.returncode, b"",
                                           self.stderr)


def _fake_kicad(monkeypatch, run: _FakeRun, version=(10, 0, 4)) -> None:
    monkeypatch.setattr(kicad_cli, "find", lambda: "/fake/kicad-cli")
    monkeypatch.setattr(kicad_cli, "version", lambda: version)
    monkeypatch.setattr(kicad_cli, "run_subprocess", run)


def test_drc_runs_on_a_staged_copy_with_only_read_only_flags(tmp_path,
                                                            monkeypatch):
    board = _drc_project(tmp_path)
    before = _tree(board.parent)
    run = _FakeRun(report=DRC_REPORT.read_bytes())
    _fake_kicad(monkeypatch, run)

    out = kicad_cli.drc(str(board))

    assert out["report"] == json.loads(DRC_REPORT.read_text(encoding="utf-8"))
    assert out["exit_code"] == 0 and out["error"] is None
    assert run.argv[:3] == ["/fake/kicad-cli", "pcb", "drc"]
    assert run.argv[3:5] == ["--format", "json"]
    assert {"--severity-error", "--severity-warning"} <= set(run.argv)
    # The three flags that would make KiCad rewrite the board, recompute what
    # is being judged, or turn design violations into a "tool failed" exit:
    for banned in ("--save-board", "--refill-zones", "--exit-code-violations",
                   "--"):
        assert banned not in run.argv
    # KiCad was handed a COPY outside the project, never the user's board...
    handed = Path(run.argv[-1])
    assert handed.is_absolute() and handed.name == board.name
    assert board.parent.resolve() not in handed.resolve().parents
    assert handed.resolve() != board.resolve()
    assert run.staged["drc_demo.kicad_pcb"] == board.read_bytes()
    assert "drc_demo.kicad_pro" in run.staged
    # ...the staging dir is gone afterwards and the project is untouched.
    assert not handed.exists()
    assert _tree(board.parent) == before


def test_drc_on_kicad_older_than_8_reports_why(tmp_path, monkeypatch):
    run = _FakeRun()
    _fake_kicad(monkeypatch, run, version=(7, 0, 11))
    out = kicad_cli.drc(str(_drc_project(tmp_path)))
    assert out["report"] is None
    assert "7.0.11" in out["error"] and "pcb drc" in out["error"]
    assert run.argv is None                    # never spawned


def test_drc_nonzero_exit_is_an_error_even_if_a_report_exists(tmp_path,
                                                             monkeypatch):
    # exit != 0 without --exit-code-violations means the TOOL failed (board
    # would not load); a stale/partial report must not be read as a verdict.
    run = _FakeRun(returncode=3, report=DRC_REPORT.read_bytes(),
                   stderr=b"Failed to load board")
    _fake_kicad(monkeypatch, run)
    out = kicad_cli.drc(str(_drc_project(tmp_path)))
    assert out["report"] is None and out["exit_code"] == 3
    assert "exited 3" in out["error"] and "Failed to load board" in out["error"]


@pytest.mark.parametrize("payload", [b"", b"{not json", b"\xff\xfe", b"[1, 2]"])
def test_drc_unreadable_report_is_an_error(tmp_path, monkeypatch, payload):
    _fake_kicad(monkeypatch, _FakeRun(report=payload))
    out = kicad_cli.drc(str(_drc_project(tmp_path)))
    assert out["report"] is None and out["exit_code"] == 0
    assert "no readable JSON report" in out["error"]


def test_drc_timeout_keeps_its_code_and_does_not_raise(tmp_path, monkeypatch):
    _fake_kicad(monkeypatch, _FakeRun(
        raises=AkcliError("KICAD_CLI_TIMEOUT", "kicad-cli timed out after 600s")))
    out = kicad_cli.drc(str(_drc_project(tmp_path)))
    assert out["report"] is None
    assert out["error_code"] == "KICAD_CLI_TIMEOUT"
    assert "timed out" in out["error"]


def test_drc_missing_board_is_an_error_not_a_crash(tmp_path, monkeypatch):
    run = _FakeRun()
    _fake_kicad(monkeypatch, run)
    out = kicad_cli.drc(str(tmp_path / "nope.kicad_pcb"))
    assert out["report"] is None and "nope.kicad_pcb" in out["error"]
    assert run.argv is None
    assert list(tmp_path.iterdir()) == []      # nothing was created beside it


# --------------------------------------------------------------------------- #
# pcb drc — real kicad-cli (skipped when not installed)
# --------------------------------------------------------------------------- #
def _drc_shape(report: dict) -> dict:
    """The locale- and clock-independent content of a DRC report.

    Descriptions are localized by KiCad, ``date`` is wall-clock: neither is
    part of what the gate keys on, so neither is part of the comparison.
    """
    def rows(section: str) -> list:
        return sorted(
            (v["type"], v["severity"],
             sorted((i["uuid"], i["pos"]["x"], i["pos"]["y"])
                    for i in v["items"]))
            for v in report[section])
    return {"source": report["source"],
            "coordinate_units": report["coordinate_units"],
            "violations": rows("violations"),
            "unconnected_items": rows("unconnected_items")}


def _drc_in_place(board: Path, out: Path) -> dict:
    """``kicad-cli pcb drc`` pointed straight at ``board`` (a tmp copy)."""
    proc = safety.run_subprocess(
        [kicad_cli.find(), "pcb", "drc", "--format", "json", "--output",
         str(out), "--severity-error", "--severity-warning", str(board)], 180.0)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(out.read_text(encoding="utf-8"))


@needs_cli
def test_drc_fixture_report_is_what_kicad_really_writes():
    """Fixture-first: the committed report is re-derived from the real tool.

    Also the isolation proof on the one directory that matters most — the
    fixture lives in the repo, and a `.kicad_prl` appearing beside it would be
    KiCad writing into the user's project.
    """
    before = _tree(DRC_FIX)
    out = kicad_cli.drc(str(DRC_BOARD))
    assert out is not None and out["error"] is None, out
    assert out["exit_code"] == 0
    live = _drc_shape(out["report"])
    committed = _drc_shape(json.loads(DRC_REPORT.read_text(encoding="utf-8")))
    if (kicad_cli.version() or (0,))[0] == 10:
        assert live == committed
    else:   # another major may add checks; it must still report these
        for section in ("violations", "unconnected_items"):
            assert all(row in live[section] for row in committed[section])
    assert _tree(DRC_FIX) == before


@needs_cli
def test_localized_fixture_report_carries_the_same_facts():
    # Same board, KiCad set to another language: every field the gate keys on
    # is identical; only the human `description` strings differ.
    en = json.loads(DRC_REPORT.read_text(encoding="utf-8"))
    zh = json.loads((DRC_FIX / "drc_demo.drc.kicad10.zh_TW.json").read_text(
        encoding="utf-8"))
    assert _drc_shape(en) == _drc_shape(zh)
    assert en != zh


@needs_cli
def test_drc_leaves_the_project_directory_untouched(tmp_path):
    board = _drc_project(tmp_path)
    before = _tree(board.parent)
    out = kicad_cli.drc(str(board))
    assert out["error"] is None, out
    assert _tree(board.parent) == before, (
        "kicad-cli wrote into the project directory")
    # ...which is not what an in-place run does — the reason for staging:
    twin = _drc_project(tmp_path, "twin")
    _drc_in_place(twin, tmp_path / "twin.json")
    assert (twin.parent / "drc_demo.kicad_prl").exists(), (
        "kicad-cli no longer writes .kicad_prl in place — the staging "
        "rationale in drivers/kicad_cli.py needs re-measuring")


@needs_cli
def test_drc_honours_the_project_rule_severities(tmp_path):
    # The fixture's .kicad_pro switches the two library-dependent checks off.
    # Without it KiCad falls back to defaults and reports them — so their
    # absence proves the project file travelled into the staging copy.
    out = kicad_cli.drc(str(_drc_project(tmp_path)))
    kinds = {v["type"] for v in out["report"]["violations"]}
    assert not kinds & {"lib_footprint_issues", "lib_footprint_mismatch"}

    bare = tmp_path / "bare"
    bare.mkdir()
    shutil.copyfile(DRC_BOARD, bare / "drc_demo.kicad_pcb")
    out = kicad_cli.drc(str(bare / "drc_demo.kicad_pcb"))
    kinds = {v["type"] for v in out["report"]["violations"]}
    assert kinds & {"lib_footprint_issues", "lib_footprint_mismatch"}


@needs_cli
def test_drc_honours_a_custom_rules_file(tmp_path):
    board = _drc_project(tmp_path)
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "akcli wide tracks"\n'
        '\t(constraint track_width (min 0.5mm)))\n',
        encoding="utf-8", newline="\n")
    out = kicad_cli.drc(str(board))
    kinds = [v["type"] for v in out["report"]["violations"]]
    assert kinds.count("track_width") == 4      # every 0.2 mm segment


@needs_cli
def test_drc_staging_matches_in_place_for_a_kiprjmod_library(tmp_path):
    """A project-local footprint library reached through ``${KIPRJMOD}``.

    Staged naively, ``${KIPRJMOD}`` would resolve to the temp dir and R1's
    footprint would raise a ``lib_footprint_issues`` warning that the user's
    own in-place DRC does not have — a gate failing on its own isolation.
    """
    def build(name: str) -> Path:
        d = tmp_path / name
        (d / "libs" / "demo.pretty").mkdir(parents=True)
        (d / "p.kicad_pcb").write_text(
            DRC_BOARD.read_text(encoding="utf-8").replace(
                '"Resistor_SMD:R_0402_1005Metric"', '"demo:R_local"'),
            encoding="utf-8", newline="\n")
        pro = json.loads((DRC_FIX / "drc_demo.kicad_pro").read_text(
            encoding="utf-8"))
        pro["meta"]["filename"] = "p.kicad_pro"
        pro["board"]["design_settings"]["rule_severities"] = {}   # libs checked
        (d / "p.kicad_pro").write_text(json.dumps(pro, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
        (d / "fp-lib-table").write_text(
            '(fp_lib_table\n  (version 7)\n  (lib (name "demo")(type "KiCad")'
            '(uri "${KIPRJMOD}/libs/demo.pretty")(options "")(descr ""))\n)\n',
            encoding="utf-8", newline="\n")
        (d / "libs" / "demo.pretty" / "R_local.kicad_mod").write_text(
            '(footprint "R_local"\n\t(version 20241229)\n\t(generator "pcbnew")\n'
            '\t(generator_version "9.0")\n\t(layer "F.Cu")\n'
            '\t(property "Reference" "REF**" (at 0 -1.5 0) (layer "F.SilkS")'
            ' (effects (font (size 1 1) (thickness 0.15))))\n'
            '\t(property "Value" "R_local" (at 0 1.5 0) (layer "F.Fab")'
            ' (effects (font (size 1 1) (thickness 0.15))))\n\t(attr smd)\n'
            '\t(pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)'
            ' (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))\n'
            '\t(pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)'
            ' (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))\n)\n',
            encoding="utf-8", newline="\n")
        return d / "p.kicad_pcb"

    staged = kicad_cli.drc(str(build("staged")))
    assert staged["error"] is None, staged
    in_place = _drc_in_place(build("inplace"), tmp_path / "inplace.json")

    assert _drc_shape(staged["report"]) == _drc_shape(in_place)
    r1 = "00000000-0000-4000-8000-000000000101"          # R1's footprint
    assert not [v for v in staged["report"]["violations"]
                if v["type"] == "lib_footprint_issues"
                and any(i["uuid"] == r1 for i in v["items"])]


@needs_cli
def test_drc_unloadable_board_is_an_error_not_a_clean_report(tmp_path):
    bad = tmp_path / "bad.kicad_pcb"
    bad.write_bytes(b'(kicad_pcb (version 20240108) (generator "x") (oops')
    out = kicad_cli.drc(str(bad))
    assert out is not None and out["report"] is None
    assert out["exit_code"] not in (0, None)
    assert "exited" in out["error"]
    assert [p.name for p in tmp_path.iterdir()] == ["bad.kicad_pcb"]


@needs_cli
def test_version_text_when_installed():
    text = kicad_cli.version_text()
    assert text and "\n" not in text
    assert kicad_cli._parse_version(text) == kicad_cli.version()
