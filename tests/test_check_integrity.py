"""``akcli check --integrity`` — the write gate's document checks, read-only.

Previously :func:`akcli.writers.connectivity.verify` ran only post-write inside
``draw --apply``; files akcli did not just write (imports, hand edits in
eeschema) never saw DANGLING_ENDPOINT / DUPLICATE_UUID / UNRESOLVED_LIB_ID /
INVALID_INSTANCES_PATH / NO_CONNECT_CONFLICT / DUPLICATE_DESIGNATOR at all.

The check is TREE-scoped (:mod:`akcli.checks.integrity`): the file given and
every sheet below it. The second half of this module covers what a
single-document pass over the root could not see — a missing child or
grandchild reported as a finding instead of killing the command, and duplicate
designators that live in or among child sheets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.checks import integrity
from akcli.cli import main
from akcli.errors import EXIT

CORPUS = Path(__file__).parent / "fixtures" / "corpus"


def _dangling(tmp_path: Path) -> Path:
    p = tmp_path / "d.kicad_sch"
    p.write_text(
        '(kicad_sch (version 20231120) (generator "t")\n'
        '\t(uuid "aaaaaaaa-0000-4000-8000-000000000000")\n'
        '\t(wire (pts (xy 10 10) (xy 20 10)) (stroke (width 0) (type default))\n'
        '\t\t(uuid "bbbbbbbb-0000-4000-8000-000000000001"))\n'
        ')\n', encoding="utf-8")
    return p


def test_integrity_flags_dangling_wire(tmp_path, capsys):
    rc = main(["check", str(_dangling(tmp_path)), "--integrity", "--json"])
    doc = json.loads(capsys.readouterr().out)
    codes = {f["code"] for f in doc["findings"]}
    assert "DANGLING_ENDPOINT" in codes
    assert rc != 0


def test_integrity_in_default_check_family(tmp_path, capsys):
    """Bare ``check`` on a .kicad_sch includes the integrity family."""
    rc = main(["check", str(_dangling(tmp_path)), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert "DANGLING_ENDPOINT" in {f["code"] for f in doc["findings"]}
    assert rc != 0


def test_integrity_clean_on_corpus(capsys):
    rc = main(["check", str(CORPUS / "power_entry.kicad_sch"),
               "--integrity", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["findings"] == []
    assert rc == 0


# --------------------------------------------------------------------------- #
# fixture census: every committed KiCad schematic passes the document gate
# --------------------------------------------------------------------------- #
KICAD_FIX = Path(__file__).parent / "fixtures" / "kicad"

# {path relative to tests/fixtures/kicad (posix): why the verifier is EXPECTED
# to report on it}. An entry is a claim that the file is deliberately not a
# document KiCad would write (or legitimately trips a check); keep it minimal
# and say why. It is empty today: the hand-authored `hier/` sheets used to
# repeat pin uuids across symbols (DUPLICATE_UUID — KiCad never writes that)
# and were repaired rather than excused. A stale entry — the file is clean
# again, or gone — fails the census, so the list can only shrink honestly.
_INTEGRITY_ALLOWLIST: dict[str, str] = {}


# The same claim for the TREE walk — what ``check --integrity`` runs — started
# from every file (a child is a legitimate place to start: the walk goes
# downward from whatever it is given).
_TREE_ALLOWLIST: dict[str, str] = {
    "hier_partial/root.kicad_sch":
        "deliberate: one resistor of the twice-instantiated child has no "
        "(path ...) entry for the second sheet instance, so eeschema — and the "
        "tree walk — resolve R102 on both (kicad-cli reports annotation errors; "
        "ground truth in netlist_hier_partial_root.kicad10.net)",
}


def _verify_document(p: Path) -> list:
    """The single-document verifier — the write gate's own pass."""
    from akcli import sexpr
    from akcli.writers import connectivity

    return connectivity.verify(
        sexpr.parse(p.read_text(encoding="utf-8")), path=str(p))


def _integrity_census(root: Path, allowlist: dict[str, str],
                      findings_of=_verify_document):
    """Run ``findings_of`` (default: the single-document verifier; the tree
    census passes :func:`akcli.checks.integrity.run_tree`) over every
    ``*.kicad_sch`` under ``root``.

    Returns ``(unexpected, stale)``: files with findings that the allowlist
    does not excuse (``{rel: [codes]}``), and allowlist entries that no longer
    earn their place (the file verifies clean, or does not exist).
    """
    dirty: dict[str, list[str]] = {}
    for p in sorted(root.rglob("*.kicad_sch")):
        findings = findings_of(p)
        if findings:
            dirty[p.relative_to(root).as_posix()] = sorted(
                {f.code for f in findings})
    unexpected = {k: v for k, v in dirty.items() if k not in allowlist}
    stale = sorted(k for k in allowlist if k not in dirty)
    return unexpected, stale


def test_every_kicad_fixture_passes_the_integrity_verifier():
    assert sorted(KICAD_FIX.rglob("*.kicad_sch")), "fixture glob matched nothing"
    unexpected, stale = _integrity_census(KICAD_FIX, _INTEGRITY_ALLOWLIST)
    assert not unexpected, (
        "KiCad fixtures fail the integrity verifier (fix the fixture; only a "
        f"deliberately broken file belongs in _INTEGRITY_ALLOWLIST): {unexpected}")
    assert not stale, (
        f"stale _INTEGRITY_ALLOWLIST entries (file is clean or missing): {stale}")
    assert all(reason.strip() for reason in _INTEGRITY_ALLOWLIST.values())


def test_every_kicad_fixture_passes_the_tree_walk():
    """The tree-scoped walk is as quiet on the committed fixtures as the
    single-document pass — every sheet instance of ``hier/`` resolves its own
    reference — except where a fixture is broken on purpose."""
    unexpected, stale = _integrity_census(
        KICAD_FIX, _TREE_ALLOWLIST, integrity.run_tree)
    assert not unexpected, (
        "KiCad fixtures fail the tree-scoped integrity walk (fix the fixture; "
        f"only a deliberately broken tree belongs in _TREE_ALLOWLIST): {unexpected}")
    assert not stale, (
        f"stale _TREE_ALLOWLIST entries (tree is clean or missing): {stale}")
    assert all(reason.strip() for reason in _TREE_ALLOWLIST.values())


def test_integrity_census_catches_dirt_and_stale_excuses(tmp_path):
    """The census itself: a duplicate-uuid sheet is reported unless excused,
    and an excuse for a clean or missing file is reported as stale."""
    clean = (KICAD_FIX / "hier" / "root.kicad_sch").read_text(encoding="utf-8")
    (tmp_path / "sub").mkdir()
    for name in ("amp.kicad_sch", "ok.kicad_sch"):       # root's child + a copy
        (tmp_path / name).write_text(clean, encoding="utf-8", newline="\n")
    # the hand-authoring mistake the hier fixtures used to carry: one pin uuid
    # reused by a second symbol
    dup = clean.replace("000000000bf1", "000000000af1")
    assert dup != clean
    (tmp_path / "sub" / "dup.kicad_sch").write_text(
        dup, encoding="utf-8", newline="\n")
    (tmp_path / "sub" / "amp.kicad_sch").write_text(
        clean, encoding="utf-8", newline="\n")

    unexpected, stale = _integrity_census(tmp_path, {})
    assert unexpected == {"sub/dup.kicad_sch": ["DUPLICATE_UUID"]}
    assert stale == []

    unexpected, stale = _integrity_census(tmp_path, {
        "sub/dup.kicad_sch": "deliberate duplicate",
        "ok.kicad_sch": "was broken once",              # clean now
        "gone.kicad_sch": "deleted fixture",            # missing
    })
    assert unexpected == {}
    assert stale == ["gone.kicad_sch", "ok.kicad_sch"]


# --------------------------------------------------------------------------- #
# tree scope: a missing sheet is a finding, duplicates are judged across sheets
# --------------------------------------------------------------------------- #
HIER = KICAD_FIX / "hier"
# The families bare `check` selects for a .kicad_sch, minus integrity — what is
# left unrun when the tree cannot be loaded.
_NEED_A_SCHEMATIC = ["erc", "power", "bom", "nets", "layout", "pairs"]


def _hier(tmp_path: Path, *names: str) -> Path:
    """Copy the named ``hier/`` sheets (``root``, ``amp``, ``deep``) and return
    the root's path. A sheet left out is a sheet that is missing on disk."""
    for name in names:
        (tmp_path / f"{name}.kicad_sch").write_text(
            (HIER / f"{name}.kicad_sch").read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n")
    return tmp_path / "root.kicad_sch"


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} lost {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def _check_json(capsys, *argv: str) -> tuple[int, dict, str]:
    rc = main(["check", *argv, "--json"])
    cap = capsys.readouterr()
    return rc, json.loads(cap.out), cap.err


def test_missing_child_is_a_finding_not_a_dead_command(tmp_path, capsys):
    """Used to be FILE_NOT_FOUND (exit 4) with no finding at all: the reader
    ran first and died on the child before integrity could name it."""
    root = _hier(tmp_path, "root")                       # amp.kicad_sch absent
    rc, doc, err = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["FINDINGS"]
    assert "error" not in doc                # a findings report, not an envelope
    assert [(f["code"], f["refs"]) for f in doc["findings"]] == [
        ("SHEET_FILE_MISSING", ["Amp", "amp.kicad_sch"]),
        ("SHEET_FILE_MISSING", ["Amp Two", "amp.kicad_sch"]),
    ]
    assert doc["metadata"]["skipped_checks"] == []       # integrity was all we asked
    assert "could not be loaded" in err

    rc = main(["check", str(root), "--integrity"])       # text
    cap = capsys.readouterr()
    assert rc == EXIT["FINDINGS"]
    assert cap.out.count("ERROR [SHEET_FILE_MISSING]") == 2
    assert "# findings (2)" in cap.out


def test_missing_child_records_the_families_that_could_not_run(tmp_path, capsys):
    root = _hier(tmp_path, "root")
    rc, doc, _ = _check_json(capsys, str(root))          # default families
    assert rc == EXIT["FINDINGS"]
    assert {f["code"] for f in doc["findings"]} == {"SHEET_FILE_MISSING"}
    assert doc["metadata"]["skipped_checks"] == _NEED_A_SCHEMATIC
    # nothing was read into a model: the caveats are unknown, never "0"
    assert doc["metadata"]["unnamed_net_count"] == "n/a"
    assert doc["metadata"]["passive_pin_ratio"] == "n/a"

    rc = main(["check", str(root)])                      # text: said on stderr
    cap = capsys.readouterr()
    assert rc == EXIT["FINDINGS"]
    assert "skipped: " + ", ".join(_NEED_A_SCHEMATIC) in cap.err
    assert "unnamed nets: n/a" in cap.out

    # an assertion family that needs the model is recorded too (and its file
    # is never opened: there is nothing to assert against)
    rc, doc, _ = _check_json(capsys, str(root), "--integrity",
                             "--contract", str(tmp_path / "absent.toml"))
    assert rc == EXIT["FINDINGS"]
    assert doc["metadata"]["skipped_checks"] == ["contract"]

    # --fail-on keeps its meaning: the findings are reported, the exit is tuned
    assert main(["check", str(root), "--integrity", "--fail-on", "never"]) \
        == EXIT["OK"]
    capsys.readouterr()


def test_missing_child_without_integrity_is_still_file_not_found(tmp_path, capsys):
    """Only integrity can speak for a tree that cannot be loaded; a selection
    without it has nothing to report and fails the way it always did."""
    root = _hier(tmp_path, "root")
    rc, doc, _ = _check_json(capsys, str(root), "--erc")
    assert rc == EXIT["NOT_FOUND"]
    assert doc["error"]["code"] == "FILE_NOT_FOUND"
    assert "amp.kicad_sch" in doc["error"]["message"]


def test_missing_root_is_still_file_not_found(tmp_path, capsys):
    rc, doc, _ = _check_json(capsys, str(tmp_path / "nope.kicad_sch"),
                             "--integrity")
    assert rc == EXIT["NOT_FOUND"]
    assert doc["error"]["code"] == "FILE_NOT_FOUND"
    assert "findings" not in doc


def test_missing_grandchild_is_reported_once_under_its_parent(tmp_path, capsys):
    root = _hier(tmp_path, "root", "amp")                # deep.kicad_sch absent
    rc, doc, _ = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["FINDINGS"]
    # amp.kicad_sch is instantiated twice and verified once
    (missing,) = doc["findings"]
    assert missing["code"] == "SHEET_FILE_MISSING"
    assert missing["refs"] == ["Deep", "deep.kicad_sch"]
    assert missing["message"].startswith("amp.kicad_sch: sheet 'Deep' ")


def test_walk_starts_at_the_file_given(tmp_path):
    """No root discovery: a child checked on its own is its own tree, and the
    file given is the one whose messages carry no prefix."""
    _hier(tmp_path, "root", "amp")                       # deep.kicad_sch absent
    amp = tmp_path / "amp.kicad_sch"
    (missing,) = integrity.run_tree(amp)
    assert missing.code == "SHEET_FILE_MISSING"
    assert missing.message.startswith("sheet 'Deep' ")
    (labelled,) = integrity.run_tree(amp, label_root=True)
    assert labelled.message.startswith("amp.kicad_sch: sheet 'Deep' ")
    assert integrity.run_tree(HIER / "amp.kicad_sch") == []


def test_cross_sheet_duplicate_designator_is_seen_from_the_root(tmp_path, capsys):
    root = _hier(tmp_path, "root", "amp", "deep")
    _edit(root, '"R1"', '"R101"')            # the root's R1 takes the child's R101
    # no single document is at fault — which is why the root-only pass missed it
    for name in ("root", "amp", "deep"):
        assert _verify_document(tmp_path / f"{name}.kicad_sch") == []
    rc, doc, _ = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["FINDINGS"]
    (dup,) = doc["findings"]
    assert dup["code"] == "DUPLICATE_DESIGNATOR" and dup["severity"] == "error"
    assert dup["refs"] == ["R101"]
    assert "2 placements" in dup["message"]
    assert "/ (root.kicad_sch), /Amp/ (amp.kicad_sch)" in dup["message"]


def test_duplicate_designator_among_child_sheets_is_seen_from_the_root(
        tmp_path, capsys):
    root = _hier(tmp_path, "root", "amp", "deep")
    # the grandchild's first instance takes a designator its parent sheet uses
    _edit(tmp_path / "deep.kicad_sch", '"R301"', '"R102"')
    for name in ("root", "amp", "deep"):
        assert _verify_document(tmp_path / f"{name}.kicad_sch") == []
    rc, doc, _ = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["FINDINGS"]
    (dup,) = doc["findings"]
    assert dup["code"] == "DUPLICATE_DESIGNATOR" and dup["refs"] == ["R102"]
    assert "/Amp/ (amp.kicad_sch), /Amp/Deep/ (deep.kicad_sch)" in dup["message"]


def test_twice_instantiated_child_with_per_instance_references_is_clean(capsys):
    """Each sheet instance resolves its OWN reference (R101/R201, R301/R401):
    one file contributing four placements is not four copies of one part."""
    rc, doc, err = _check_json(capsys, str(HIER / "root.kicad_sch"), "--integrity")
    assert rc == EXIT["OK"]
    assert doc["findings"] == []
    assert "skipped_checks" not in doc["metadata"]
    assert err == ""


def test_scalar_reference_on_a_reused_sheet_is_reported_once(tmp_path):
    """One reference written on both instance paths is the per-file verifier's
    finding (prefixed with the sheet file); the tree pass must not echo it."""
    root = _hier(tmp_path, "root", "amp", "deep")
    _edit(tmp_path / "amp.kicad_sch", '(reference "R201")', '(reference "R101")')
    (dup,) = integrity.run_tree(root)
    assert dup.code == "DUPLICATE_DESIGNATOR" and dup.refs == ["R101"]
    assert dup.message.startswith(
        "amp.kicad_sch: designator 'R101' unit 1 appears on 2 placement(s)")


def test_instance_without_a_path_entry_duplicates_as_in_eeschema(capsys):
    """``hier_partial``: KiCad's own export puts R102 on both sheet instances
    (netlist_hier_partial_root.kicad10.net), so integrity reports it — and
    agrees with the reader's warning about the same tree."""
    root = KICAD_FIX / "hier_partial" / "root.kicad_sch"
    rc, doc, err = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["FINDINGS"]
    (dup,) = doc["findings"]
    assert dup["code"] == "DUPLICATE_DESIGNATOR" and dup["refs"] == ["R102"]
    assert "/Amp/ (amp.kicad_sch), /Amp Two/ (amp.kicad_sch)" in dup["message"]
    assert err.count("duplicate designator 'R102'") == 1
    # the child on its own is one instance: nothing to collide with
    assert integrity.run_tree(root.parent / "amp.kicad_sch") == []


_FOREIGN = ('(project "other" (path "/ffff0000-0000-4000-8000-000000000000/'
            'eeee0000-0000-4000-8000-000000000009" (reference "R1") (unit 1)))')
_R101_HEAD = ('(instances (project "hier"\n\t\t\t(path "/aaaa0000-0000-4000-8000-'
              '000000000000/bbbb0000-0000-4000-8000-000000000001" '
              '(reference "R101") (unit 1))')
_R101_TAIL = '(reference "R201") (unit 1)))))'


@pytest.mark.parametrize("listed", ["first", "last"])
def test_stale_foreign_project_entry_is_not_counted(tmp_path, capsys, listed):
    """KiCad keeps a symbol's entries for OTHER projects' trees. One that says
    ``R1`` — the root's own designator — names no live sheet instance of this
    tree, so it is not a second R1, wherever it sits in the block."""
    from akcli.readers import kicad as kreader

    root = _hier(tmp_path, "root", "amp", "deep")
    amp = tmp_path / "amp.kicad_sch"
    if listed == "first":
        _edit(amp, _R101_HEAD, _R101_HEAD.replace(
            '(instances ', f'(instances {_FOREIGN} ', 1))
    else:
        _edit(amp, _R101_TAIL, f'(reference "R201") (unit 1))) {_FOREIGN}))')
    text = amp.read_text(encoding="utf-8")
    assert text.count('(reference "R1")') == 1           # the stale entry is there
    assert text.count('(project "other"') == 1

    assert integrity.run_tree(root) == []
    rc, doc, err = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["OK"] and doc["findings"] == []
    # ...and the reader resolves the same designators with or without it
    assert kreader.read_sch(str(root)).warnings == []
    assert err == ""


def test_power_references_repeated_across_sheets_are_not_reported(tmp_path):
    """``#PWR0<n>`` is allocated per FILE, so every multi-sheet design akcli
    writes repeats it across sheets, and a virtual reference never reaches a
    netlist or a BOM — the tree pass leaves them alone. A repeat INSIDE one
    file is still the per-file verifier's finding."""
    root = _hier(tmp_path, "root", "amp", "deep")
    amp = tmp_path / "amp.kicad_sch"
    _edit(amp, '"#PWR011"', '"#PWR001"')                 # the root's own #PWR001
    assert integrity.run_tree(root) == []
    _edit(amp, '"#PWR012"', '"#PWR001"')                 # now twice in amp itself
    (dup,) = integrity.run_tree(root)
    assert dup.code == "DUPLICATE_DESIGNATOR" and dup.refs == ["#PWR001"]
    assert dup.message.startswith("amp.kicad_sch: ")


def test_run_check_entry_point_is_tree_scoped(tmp_path):
    """``gate`` — and ``release preflight``, once it selects the family —
    reach integrity through ``_run_check`` with a bare namespace, after loading
    the schematic themselves: the same tree walk, and still KiCad-only."""
    import argparse

    from akcli.commands.checks import _run_check

    root = _hier(tmp_path, "root", "amp", "deep")
    _edit(tmp_path / "deep.kicad_sch", '"R301"', '"R102"')
    ns = argparse.Namespace(path=str(root), symbols=None)
    (dup,) = _run_check("integrity", None, None, ns)
    assert dup.code == "DUPLICATE_DESIGNATOR" and dup.refs == ["R102"]
    assert _run_check("integrity", None, None,
                      argparse.Namespace(path="board.SchDoc", symbols=None)) == []


def test_unparseable_child_is_still_the_readers_error(tmp_path, capsys):
    """The walk skips a child it cannot parse — the reader names the file and
    the fault the moment the tree is loaded, exactly as before."""
    root = _hier(tmp_path, "root", "deep")
    (tmp_path / "amp.kicad_sch").write_text(
        '(kicad_sch (version 20231120)\n', encoding="utf-8", newline="\n")
    assert integrity.run_tree(root) == []
    rc, doc, _ = _check_json(capsys, str(root), "--integrity")
    assert rc == EXIT["PARSE"]
    assert "error" in doc and "findings" not in doc


def test_sheet_cycle_does_not_loop(tmp_path):
    """A sheet that instantiates its own file is refused by the reader on
    load; the walk only has to terminate."""
    root = _hier(tmp_path, "root", "amp", "deep")
    _edit(tmp_path / "deep.kicad_sch", "\n)\n", (
        '\n\t(sheet (at 25.4 25.4) (size 25.4 12.7)\n'
        '\t\t(uuid "cccc0000-0000-4000-8000-0000000000aa")\n'
        '\t\t(property "Sheetname" "Loop" (at 0 0 0)'
        ' (effects (font (size 1.27 1.27))))\n'
        '\t\t(property "Sheetfile" "amp.kicad_sch" (at 0 0 0)'
        ' (effects (font (size 1.27 1.27)))))\n)\n'))
    assert [f.code for f in integrity.run_tree(root)] == []


def test_same_ref_under_another_projects_root_is_not_a_duplicate(tmp_path):
    # A sheet shared between two projects keeps one (path ...) entry per
    # project; each project annotates on its own. The same reference under a
    # DIFFERENT root uuid is one part seen twice, not a duplicate — while the
    # same reference twice under ONE root still is.
    from akcli.sexpr import parse
    from akcli.writers import connectivity

    src = (Path(__file__).parent / "fixtures" / "kicad" / "hier"
           / "amp.kicad_sch").read_text(encoding="utf-8")
    live = ('(path "/aaaa0000-0000-4000-8000-000000000000/'
            'bbbb0000-0000-4000-8000-000000000001" (reference "R101") (unit 1))')
    assert live in src
    foreign = ('(path "/ffff0000-0000-4000-8000-000000000000/'
               'bbbb0000-0000-4000-8000-000000000009" (reference "R101") (unit 1))')
    shared = tmp_path / "amp.kicad_sch"
    shared.write_text(src.replace(live, live + "\n\t\t\t" + foreign),
                      encoding="utf-8", newline="\n")
    codes = [f.code for f in connectivity.verify(
        parse(shared.read_text(encoding="utf-8")), path=str(shared))]
    assert connectivity.DUPLICATE_DESIGNATOR not in codes

    same_root = foreign.replace("ffff0000", "aaaa0000")
    shared.write_text(src.replace(live, live + "\n\t\t\t" + same_root),
                      encoding="utf-8", newline="\n")
    codes = [f.code for f in connectivity.verify(
        parse(shared.read_text(encoding="utf-8")), path=str(shared))]
    assert connectivity.DUPLICATE_DESIGNATOR in codes
