"""Scaffolding engine + blank-document emitters (``akcli.scaffold``, ``writers.blank``).

Three layers of claim are pinned here:

* **Fixture equality** — the emitters must reproduce, byte for byte, the
  committed ground truth under ``tests/fixtures/kicad/blank_project/`` (which
  ``test_e2e_project.py`` proves against a real ``kicad-cli``). An emitter
  edit that changes a byte fails HERE, where the diff is readable, instead of
  in a KiCad job.
* **Plan/apply semantics** — created/kept/overwritten/conflict, the pro↔sch
  UUID binding, idempotence of ``init``.
* **All-or-nothing** — a commit that fails partway must leave the directory
  exactly as it started.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import libtable, scaffold
from akcli.errors import AkcliError
from akcli.readers import kicad as kicad_reader
from akcli.sexpr import parse as sexpr_parse
from akcli.sexpr import query as sq
from akcli.writers import atomic, blank

FIX = Path(__file__).parent / "fixtures" / "kicad" / "blank_project"
# The UUID baked into the committed .kicad_pro fixture (see the regen note in
# tests/fixtures/kicad/README.md); tests substitute their own.
FIXTURE_UUID = "00000000-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# emitters — byte-equal to the committed ground truth
# --------------------------------------------------------------------------- #
def test_blank_pro_matches_fixture():
    assert blank.blank_pro("blank.kicad_pro", FIXTURE_UUID) == \
        FIX.joinpath("blank.kicad_pro").read_text(encoding="utf-8")


def test_blank_pcb_matches_fixture():
    assert blank.blank_pcb("A4") == \
        FIX.joinpath("blank.kicad_pcb").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind,name", [("sym", "sym-lib-table"),
                                       ("fp", "fp-lib-table")])
def test_blank_lib_table_matches_fixture(kind, name):
    assert blank.blank_lib_table(kind) == \
        FIX.joinpath(name).read_text(encoding="utf-8")


def test_blank_pro_binds_the_root_sheet_uuid():
    doc = json.loads(blank.blank_pro("demo.kicad_pro", "u-1"))
    assert doc["meta"]["filename"] == "demo.kicad_pro"
    assert doc["sheets"] == [["u-1", "Root"]]
    # KiCad materialises its own Default netclass: we must not invent one.
    assert doc["net_settings"]["classes"] == []


def test_blank_sch_uuid_kwarg_is_honoured():
    text = blank.blank_sch("A4", None, uuid="u-2")
    assert sq.root_uuid(sexpr_parse(text)) == "u-2"
    # and without it, every sheet still gets a distinct identity
    assert blank.blank_sch("A4", None) != blank.blank_sch("A4", None)


def test_blank_pcb_is_a_container_not_a_layout(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(blank.blank_pcb(), encoding="utf-8", newline="\n")
    pcb = kicad_reader.read_pcb(board)
    assert pcb.footprints == []
    assert pcb.tracks == [] and pcb.vias == [] and pcb.zones == []


def test_gitignore_covers_kicad_and_akcli_churn():
    body = blank.kicad_gitignore()
    for entry in ("*-backups/", "*.kicad_prl", "fp-info-cache", "~*.lck",
                  "_autosave-*", "*-save.kicad_*", "*.kicad_pcb-bak",
                  "*.kicad_sch-bak", ".akcli/"):
        assert entry in body.splitlines()


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_new_creates_the_whole_set(tmp_path):
    p = scaffold.plan(tmp_path / "demo", mode="new")
    assert [f.action for f in p.files] == ["created"] * 6
    assert p.created == ["demo.kicad_pro", "demo.kicad_sch", "demo.kicad_pcb",
                         "sym-lib-table", "fp-lib-table", ".gitignore"]
    assert p.name == "demo"
    # planning writes nothing at all
    assert not (tmp_path / "demo").exists()


def test_plan_new_opt_outs(tmp_path):
    p = scaffold.plan(tmp_path / "d", mode="new", no_pcb=True,
                      no_lib_tables=True, no_gitignore=True)
    assert [f.kind for f in p.files] == ["pro", "sch"]


def test_plan_new_conflicts_then_force_overwrites(tmp_path):
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d"))
    again = scaffold.plan(tmp_path, mode="new", name="d")
    assert set(again.conflicts) == {"d.kicad_pro", "d.kicad_sch", "d.kicad_pcb",
                                    "sym-lib-table", "fp-lib-table"}
    # .gitignore is user-managed content: never a conflict, never overwritten
    assert again.kept == [".gitignore"]
    forced = scaffold.plan(tmp_path, mode="new", name="d", force=True)
    assert not forced.conflicts
    assert ".gitignore" not in forced.overwritten
    assert forced.kept == [".gitignore"]


def test_plan_sheets_add_blank_children(tmp_path):
    p = scaffold.plan(tmp_path / "d", mode="new", sheets=["power", "mcu"])
    kinds = {f.name: f.kind for f in p.files}
    assert kinds["power.kicad_sch"] == "sheet"
    assert kinds["mcu.kicad_sch"] == "sheet"


@pytest.mark.parametrize("raw,want", [
    ("a,b", ["a", "b"]), (" a , ,b ", ["a", "b"]), ("a,a", ["a"]),
    ("x.kicad_sch", ["x"]), (None, []), ("", []),
])
def test_sheet_names_parsing(raw, want):
    assert scaffold.sheet_names(raw) == want


def test_plan_json_has_no_backslashes(tmp_path):
    p = scaffold.plan(tmp_path / "demo", mode="new")
    # as_posix() everywhere: a Windows separator would JSON-escape to \\ and
    # break every byte-stable snapshot downstream.
    assert "\\" not in json.dumps(p.to_json())


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def test_apply_binds_pro_to_the_schematic_uuid(tmp_path):
    p = scaffold.plan(tmp_path / "demo", mode="new", title="Demo")
    scaffold.apply(p)
    d = tmp_path / "demo"
    pro = json.loads((d / "demo.kicad_pro").read_text(encoding="utf-8"))
    sch_uuid = sq.root_uuid(sexpr_parse((d / "demo.kicad_sch").read_text(
        encoding="utf-8")))
    assert pro["sheets"][0][0] == sch_uuid == p.root_uuid
    assert pro["meta"]["filename"] == "demo.kicad_pro"


def test_apply_leaves_no_temp_files(tmp_path):
    scaffold.apply(scaffold.plan(tmp_path / "demo", mode="new"))
    strays = [f.name for f in (tmp_path / "demo").iterdir()
              if f.name.endswith(".tmp")]
    assert strays == []


def test_apply_refuses_conflicts_before_touching_disk(tmp_path):
    target = tmp_path / "demo"
    scaffold.apply(scaffold.plan(target, mode="new"))
    before = {f.name: f.read_bytes() for f in target.iterdir() if f.is_file()}
    with pytest.raises(AkcliError) as exc:
        scaffold.apply(scaffold.plan(target, mode="new"))
    assert exc.value.code == "PROJECT_EXISTS"
    assert {f.name: f.read_bytes() for f in target.iterdir()
            if f.is_file()} == before


def test_apply_force_backs_up_the_previous_bytes(tmp_path):
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d"))
    old = (tmp_path / "d.kicad_sch").read_bytes()
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d", force=True))
    bak = tmp_path / ".akcli" / "backups" / "d.kicad_sch.bak"
    assert bak.is_file() and bak.read_bytes() == old


def test_apply_force_keeps_the_root_uuid(tmp_path):
    """--force rewrites the sheet; re-minting its UUID would orphan the design."""
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d"))
    before = sq.root_uuid(sexpr_parse(
        (tmp_path / "d.kicad_sch").read_text(encoding="utf-8")))
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d", force=True))
    after = sq.root_uuid(sexpr_parse(
        (tmp_path / "d.kicad_sch").read_text(encoding="utf-8")))
    assert before == after


def test_apply_rolls_back_a_partial_commit(tmp_path, monkeypatch):
    """Commit #3 explodes: the two files already promoted must disappear."""
    target = tmp_path / "demo"
    p = scaffold.plan(target, mode="new")
    real_commit = atomic.commit
    calls = {"n": 0}

    def boom(pw, backup_dir=None, backup_depth=atomic.DEFAULT_BACKUP_DEPTH):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        real_commit(pw, backup_dir, backup_depth)

    monkeypatch.setattr(scaffold.atomic, "commit", boom)
    with pytest.raises(OSError):
        scaffold.apply(p)
    survivors = sorted(f.name for f in target.iterdir()) if target.exists() else []
    assert survivors == [], f"rollback left files behind: {survivors}"


def test_apply_rollback_restores_overwritten_bytes(tmp_path, monkeypatch):
    scaffold.apply(scaffold.plan(tmp_path, mode="new", name="d"))
    before = {f.name: f.read_bytes() for f in tmp_path.iterdir() if f.is_file()}
    p = scaffold.plan(tmp_path, mode="new", name="d", force=True)
    real_commit = atomic.commit
    calls = {"n": 0}

    def boom(pw, backup_dir=None, backup_depth=atomic.DEFAULT_BACKUP_DEPTH):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        real_commit(pw, backup_dir, backup_depth)

    monkeypatch.setattr(scaffold.atomic, "commit", boom)
    with pytest.raises(OSError):
        scaffold.apply(p)
    after = {f.name: f.read_bytes() for f in tmp_path.iterdir() if f.is_file()}
    assert after == before


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def _lone_sheet(tmp_path: Path, name: str = "board") -> Path:
    sch = tmp_path / f"{name}.kicad_sch"
    sch.write_text(blank.blank_sch("A4", None), encoding="utf-8", newline="\n")
    return sch


def test_init_adopts_the_existing_root_uuid(tmp_path):
    sch = _lone_sheet(tmp_path)
    want = sq.root_uuid(sexpr_parse(sch.read_text(encoding="utf-8")))
    before = sch.read_bytes()
    p = scaffold.plan(tmp_path, mode="init")
    scaffold.apply(p)
    assert p.root_uuid == want
    assert sch.read_bytes() == before, "init must never rewrite an existing sheet"
    pro = json.loads((tmp_path / "board.kicad_pro").read_text(encoding="utf-8"))
    assert pro["sheets"][0][0] == want


def test_init_is_idempotent(tmp_path):
    _lone_sheet(tmp_path)
    scaffold.apply(scaffold.plan(tmp_path, mode="init"))
    stamp = {f.name: (f.read_bytes(), f.stat().st_mtime_ns)
             for f in tmp_path.iterdir() if f.is_file()}
    second = scaffold.plan(tmp_path, mode="init")
    assert second.created == [] and second.conflicts == []
    assert scaffold.status_of(second) == "unchanged"
    scaffold.apply(second)
    # only the project files are compared: `.akcli/` legitimately grows a
    # journal line per invocation.
    assert {f.name: (f.read_bytes(), f.stat().st_mtime_ns)
            for f in tmp_path.iterdir() if f.is_file()} == stamp


def test_init_names_itself_after_the_existing_project(tmp_path):
    _lone_sheet(tmp_path, "board")
    (tmp_path / "legacy.kicad_pro").write_text(
        blank.blank_pro("legacy.kicad_pro", "u-9"), encoding="utf-8",
        newline="\n")
    p = scaffold.plan(tmp_path, mode="init")
    assert p.name == "legacy"
    assert p.kept == ["legacy.kicad_pro", "board.kicad_sch"]


def test_init_refuses_a_malformed_project_file(tmp_path):
    _lone_sheet(tmp_path)
    (tmp_path / "board.kicad_pro").write_text("{ not json",
                                              encoding="utf-8", newline="\n")
    with pytest.raises(AkcliError) as exc:
        scaffold.plan(tmp_path, mode="init")
    assert exc.value.code == "PROJECT_FILE_INVALID"


def test_init_refuses_a_non_schematic_root(tmp_path):
    (tmp_path / "board.kicad_sch").write_text(
        "(kicad_symbol_lib (version 20231120))", encoding="utf-8", newline="\n")
    with pytest.raises(AkcliError) as exc:
        scaffold.plan(tmp_path, mode="init")
    assert exc.value.code == "PROJECT_FILE_INVALID"


def test_init_without_a_schematic_is_not_found(tmp_path):
    with pytest.raises(AkcliError) as exc:
        scaffold.plan(tmp_path, mode="init")
    assert exc.value.code == "PROJECT_FILE_MISSING"


def test_init_explicit_root_wins(tmp_path):
    _lone_sheet(tmp_path, "a")
    _lone_sheet(tmp_path, "b")
    p = scaffold.plan(tmp_path, mode="init", root=Path("b.kicad_sch"))
    assert p.root_sch.name == "b.kicad_sch"
    assert p.name == "b"


def test_init_missing_explicit_root(tmp_path):
    _lone_sheet(tmp_path)
    with pytest.raises(AkcliError) as exc:
        scaffold.plan(tmp_path, mode="init", root=Path("nope.kicad_sch"))
    assert exc.value.code == "PROJECT_FILE_MISSING"


# --------------------------------------------------------------------------- #
# the point of it all: the scaffolded directory reads as a real project
# --------------------------------------------------------------------------- #
def test_scaffolded_project_has_no_lib_table_findings(tmp_path):
    d = tmp_path / "demo"
    scaffold.apply(scaffold.plan(d, mode="new"))
    ws = libtable.discover(d)
    codes = {f.code for f in libtable.audit(ws, [d / "demo.kicad_sch"])}
    assert "LIB_TABLE_MISSING" not in codes
    # the tables really are empty, not merely present
    assert ws.sym_table is not None and not ws.sym_table.entries
    assert ws.fp_table is not None and not ws.fp_table.entries


def test_scaffolded_project_journal_records_the_run(tmp_path):
    from akcli import journal

    d = tmp_path / "demo"
    scaffold.apply(scaffold.plan(d, mode="new"))
    entries = journal.read_entries(d)
    assert entries and entries[-1]["cmd"] == "project new"
    assert entries[-1]["status"] == "applied"
    assert entries[-1]["root_uuid"]


def test_plan_never_stages_one_path_twice(tmp_path):
    """`--sheets demo` on project `demo` names the root sheet, not a child."""
    p = scaffold.plan(tmp_path / "demo", mode="new", sheets=["demo", "power"])
    names = [f.name for f in p.files]
    assert len(names) == len(set(names))
    # the root sheet keeps its own kind, and the real child is still planned
    kinds = {f.name: f.kind for f in p.files}
    assert kinds["demo.kicad_sch"] == "sch"
    assert kinds["power.kicad_sch"] == "sheet"
    scaffold.apply(p)          # and it commits cleanly


def test_plan_sheets_children_are_content_seeded(tmp_path):
    """`--sheets` children get the same root uuid `add_sheet create:true` would mint."""
    from akcli.writers import identity

    p = scaffold.plan(tmp_path / "d", mode="new", sheets=["power"])
    child = next(f for f in p.files if f.kind == "sheet")
    assert child.content is not None
    want = identity.node_uuid(p.root_uuid, identity.seed_sheet_file("power.kicad_sch"))
    assert f'(uuid "{want}")' in child.content
    again = scaffold.plan(tmp_path / "d", mode="new", sheets=["power"])
    child2 = next(f for f in again.files if f.kind == "sheet")
    # Different plans mint different root uuids, but the child is a pure
    # function of (root uuid, file name) — never random.
    assert (child2.content == child.content) == (again.root_uuid == p.root_uuid)
