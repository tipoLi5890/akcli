"""``add_sheet {"create": true}`` — the child file the transaction writes.

Hierarchical authoring used to need two shell round-trips (``akcli new child``
then ``draw``). With ``create`` the SAME op-list brings the child into
existence, and the identity rules do not bend for it: the child's ROOT uuid is
content-seeded from its parent's root uuid plus the ``Sheetfile`` string
(``identity.seed_sheet_file``), so plan and apply agree, a re-apply is
byte-identical, and an existing child is never touched.

Default ``create: false``/absent keeps every current refusal
(``SHEET_FILE_MISSING`` from the handler, ``SHEET_UNKNOWN`` from the envelope).
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT
from akcli.sexpr import parser as sexpr
from akcli.writers import identity, instances

REPO = Path(__file__).parent.parent
SYMBOLS = [str(REPO / "tests/fixtures/kicad/symbols/Device.kicad_sym")]


def _sym_flags() -> list[str]:
    return [a for s in SYMBOLS for a in ("--symbols", s)]


def _root_uuid(path: Path) -> str:
    return instances.root_uuid(sexpr.parse(path.read_text(encoding="utf-8")))


def _seed(tmp_path: Path, capsys) -> Path:
    main_sch = tmp_path / "main.kicad_sch"
    assert main(["new", str(main_sch)]) == EXIT["OK"]
    capsys.readouterr()
    return main_sch


def _ops_file(tmp_path: Path, doc: dict, name: str = "ops.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _draw(main_sch: Path, ops: str, *extra: str) -> int:
    return main(["draw", str(main_sch), "--ops", ops, "--json", "--no-erc",
                 *_sym_flags(), *extra])


ADD_SHEET = {
    "op": "add_sheet", "name": "Amp", "file": "amp.kicad_sch",
    "at": [5000, 2000], "size": [1000, 800], "create": True,
    "pins": [{"name": "OUT", "type": "output", "side": "left",
              "offset_mil": 200}],
}


def _flat(create: bool = True) -> dict:
    """Single-file op-list (no `sheets` envelope) — `create` alone routes it."""
    op = dict(ADD_SHEET)
    if create:
        op["create"] = True
    else:
        op.pop("create")
    return {"protocol_version": 2, "target_format": "kicad", "ops": [op]}


def _routed() -> dict:
    """Envelope-routed: the child is DECLARED and populated in the same pass."""
    return {
        "protocol_version": 2, "target_format": "kicad",
        "sheets": {"amp": {"file": "amp.kicad_sch"}},
        "ops": [
            dict(ADD_SHEET),
            {"op": "place_component", "sheet": "amp", "lib_id": "Device:R",
             "designator": "R101", "x_mil": 2000, "y_mil": 2000,
             "value": "10k"},
            {"op": "add_net_label", "sheet": "amp", "at": "R101.1",
             "name": "OUT", "scope": "hierarchical"},
        ],
    }


def _files(out: str) -> dict[str, dict]:
    return {f["file"].rsplit("/", 1)[-1]: f
            for f in json.loads(out)["files"]}


# --------------------------------------------------------------------------- #
# plan vs apply
# --------------------------------------------------------------------------- #
def test_plan_reports_the_creation_without_writing_anything(tmp_path, capsys):
    """`draw` with no --apply must leave the directory exactly as it was, and
    still SAY that the child would be created (an agent plans on this)."""
    main_sch = _seed(tmp_path, capsys)
    before = main_sch.read_bytes()
    ops = _ops_file(tmp_path, _flat())

    assert _draw(main_sch, ops) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "dry-run"
    # no SHEET_FILE_MISSING for the child this op-list would create
    assert doc["connectivity"] == []
    child = _files(json.dumps(doc))["amp.kicad_sch"]
    assert child["created"] is True and child["written"] is False

    assert not (tmp_path / "amp.kicad_sch").exists()
    assert main_sch.read_bytes() == before
    # no stray temp/stub next to the target (the journal's .akcli/ aside)
    assert sorted(f.name for f in tmp_path.glob("*.kicad_sch*")) == [
        "main.kicad_sch"]


def test_apply_creates_the_child_and_reapply_is_byte_identical(tmp_path, capsys):
    main_sch = _seed(tmp_path, capsys)
    ops = _ops_file(tmp_path, _flat())
    child = tmp_path / "amp.kicad_sch"

    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "applied"
    assert _files(json.dumps(doc))["amp.kicad_sch"]["created"] is True
    assert child.is_file()
    first = (main_sch.read_bytes(), child.read_bytes())

    # second apply: the child EXISTS now, so nothing is created — and the
    # whole tree stays byte-identical (the invariant every op must hold)
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    # nothing to create and no ops routed to it: the child is not part of the
    # transaction at all any more
    assert "amp.kicad_sch" not in _files(json.dumps(doc))
    assert (main_sch.read_bytes(), child.read_bytes()) == first


def test_child_root_uuid_is_content_seeded_from_the_parent(tmp_path, capsys):
    """plan -> apply -> delete -> apply must all agree on the child's identity:
    the root uuid is UUIDv5(parent root, "sheetfile:<file>"), never random."""
    main_sch = _seed(tmp_path, capsys)
    parent_root = _root_uuid(main_sch)
    expected = identity.node_uuid(
        parent_root, identity.seed_sheet_file("amp.kicad_sch"))
    ops = _ops_file(tmp_path, _flat())
    child = tmp_path / "amp.kicad_sch"

    assert _draw(main_sch, ops) == EXIT["OK"]          # plan first
    capsys.readouterr()
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    assert _root_uuid(child) == expected

    # re-created from scratch after a delete: same uuid, same bytes
    was = child.read_bytes()
    child.unlink()
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    assert child.read_bytes() == was
    assert _root_uuid(child) == expected


def test_an_existing_child_is_never_touched(tmp_path, capsys):
    main_sch = _seed(tmp_path, capsys)
    child = tmp_path / "amp.kicad_sch"
    assert main(["new", str(child), "--paper", "A3",
                 "--title", "Hand written"]) == EXIT["OK"]
    capsys.readouterr()
    before = child.read_bytes()

    ops = _ops_file(tmp_path, _flat())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert "amp.kicad_sch" not in _files(json.dumps(doc))
    assert child.read_bytes() == before                # paper, title, uuid kept


# --------------------------------------------------------------------------- #
# the routed transaction (child declared AND populated in one pass)
# --------------------------------------------------------------------------- #
def test_strict_nets_gate_runs_over_the_created_child(tmp_path, capsys):
    """--strict-nets dry-applies into a shadow TREE; the stub has to exist in
    there too, or the net diff cannot be computed and the gate fails closed."""
    main_sch = _seed(tmp_path, capsys)
    ops = _ops_file(tmp_path, _routed())

    assert _draw(main_sch, ops, "--apply", "--strict-nets") == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is True
    assert doc["net_diff"]["equivalent"] is False      # OUT appeared
    written = {n for n, f in _files(json.dumps(doc)).items() if f["written"]}
    assert written == {"main.kicad_sch", "amp.kicad_sch"}

    # the cross-sheet net is real when read back through the root
    assert main(["nets", str(main_sch), "--json"]) == EXIT["OK"]
    nets = {n["name"]: frozenset(n["members"])
            for n in json.loads(capsys.readouterr().out)["nets"]}
    assert nets["OUT"] == {"R101.1"}


def test_strict_nets_plan_writes_nothing(tmp_path, capsys):
    """The shadow tree is scratch space — a --strict-nets PLAN must not leave
    the stub (or anything else) in the user's directory."""
    main_sch = _seed(tmp_path, capsys)
    before = main_sch.read_bytes()
    ops = _ops_file(tmp_path, _routed())

    assert _draw(main_sch, ops, "--strict-nets") == EXIT["OK"]
    capsys.readouterr()
    assert not (tmp_path / "amp.kicad_sch").exists()
    assert main_sch.read_bytes() == before


def test_inserting_an_op_and_reapplying_duplicates_nothing(tmp_path, capsys):
    main_sch = _seed(tmp_path, capsys)
    doc = _routed()
    ops = _ops_file(tmp_path, doc)
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    child = tmp_path / "amp.kicad_sch"
    child_uuid = _root_uuid(child)

    doc["ops"].insert(1, {"op": "place_component", "sheet": "amp",
                          "lib_id": "Device:R", "designator": "R102",
                          "x_mil": 4000, "y_mil": 2000, "value": "22k"})
    ops2 = _ops_file(tmp_path, doc, "ops2.json")
    assert _draw(main_sch, ops2, "--apply") == EXIT["OK"]
    out = json.loads(capsys.readouterr().out)
    assert out["connectivity"] == []                   # no DUPLICATE_UUID
    assert _files(json.dumps(out))["amp.kicad_sch"]["created"] is False

    root_doc = sexpr.parse(main_sch.read_text(encoding="utf-8"))
    assert len(list(root_doc.find_all("sheet"))) == 1  # one sheet node, not two
    assert _root_uuid(child) == child_uuid             # child identity survived
    child_text = child.read_text(encoding="utf-8")
    assert child_text.count('"Reference" "R101"') == 1  # placed once, not twice
    assert child_text.count('"Reference" "R102"') == 1


# --------------------------------------------------------------------------- #
# the refusals `create` does NOT weaken
# --------------------------------------------------------------------------- #
def test_without_create_the_missing_child_is_still_refused(tmp_path, capsys):
    main_sch = _seed(tmp_path, capsys)
    before = main_sch.read_bytes()
    ops = _ops_file(tmp_path, _flat(create=False))

    assert _draw(main_sch, ops, "--apply") == EXIT["OPLIST"]
    doc = json.loads(capsys.readouterr().out)
    assert [r["error_code"] for r in doc["ops"]] == ["SHEET_FILE_MISSING"]
    assert "create: true" in (doc["ops"][0]["remediation"] or "")
    assert not (tmp_path / "amp.kicad_sch").exists()
    assert main_sch.read_bytes() == before


def test_declared_but_uncreatable_child_is_still_sheet_unknown(tmp_path, capsys):
    """The envelope's own gate: a declared file nobody creates is refused."""
    main_sch = _seed(tmp_path, capsys)
    doc = _routed()
    doc["ops"][0].pop("create")
    ops = _ops_file(tmp_path, doc)

    assert _draw(main_sch, ops, "--apply") == EXIT["OPLIST"]
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "SHEET_UNKNOWN"
    assert "create: true" in (err["error"].get("remediation") or "")
    assert not (tmp_path / "amp.kicad_sch").exists()


def test_ops_validate_types_the_create_field(tmp_path, capsys):
    good = _ops_file(tmp_path, _flat(), "good.json")
    assert main(["ops", "validate", good]) == EXIT["OK"]
    capsys.readouterr()

    doc = _flat()
    doc["ops"][0]["create"] = "yes"
    bad = _ops_file(tmp_path, doc, "bad.json")
    assert main(["ops", "validate", bad, "--json"]) != EXIT["OK"]
    out = capsys.readouterr().out
    assert "create" in out and "true or false" in out


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #
def test_undo_run_restores_content_and_keeps_the_created_child(tmp_path, capsys):
    """A rollback restores CONTENT; it never unlinks. The created child comes
    back as the blank stub it started as (and undo again is a redo)."""
    main_sch = _seed(tmp_path, capsys)
    root_before = main_sch.read_bytes()
    ops = _ops_file(tmp_path, _routed())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    out = json.loads(capsys.readouterr().out)
    rid, child = out["run_id"], tmp_path / "amp.kicad_sch"
    applied = (main_sch.read_bytes(), child.read_bytes())

    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert main_sch.read_bytes() == root_before
    assert child.is_file()                             # never deleted
    stub = child.read_bytes()
    assert b"place_component" not in stub and b"R101" not in stub
    assert _root_uuid(child) == identity.node_uuid(
        _root_uuid(main_sch), identity.seed_sheet_file("amp.kicad_sch"))

    # same command again = redo
    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert (main_sch.read_bytes(), child.read_bytes()) == applied


def test_reapply_after_undo_is_byte_identical(tmp_path, capsys):
    """The stub an undo leaves behind is adopted, not re-created: replaying the
    op-list onto it reproduces the applied bytes exactly."""
    main_sch = _seed(tmp_path, capsys)
    ops = _ops_file(tmp_path, _routed())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    rid = json.loads(capsys.readouterr().out)["run_id"]
    child = tmp_path / "amp.kicad_sch"
    applied = (main_sch.read_bytes(), child.read_bytes())

    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    capsys.readouterr()
    assert (main_sch.read_bytes(), child.read_bytes()) == applied


def test_create_puts_a_flat_oplist_on_the_transaction_path(tmp_path, capsys):
    """`create` alone makes a single-file op-list multi-FILE, so it commits as a
    transaction: the recovery unit is the run backup (`undo --run <id>`), not
    the rotated `<name>.bak` stack a plain single-file draw leaves."""
    main_sch = _seed(tmp_path, capsys)
    root_before = main_sch.read_bytes()
    ops = _ops_file(tmp_path, _flat())
    assert _draw(main_sch, ops, "--apply") == EXIT["OK"]
    rid = json.loads(capsys.readouterr().out)["run_id"]

    runs = tmp_path / ".akcli" / "backups" / "runs" / rid
    assert (runs / "manifest.json").is_file()
    assert not (tmp_path / ".akcli" / "backups" / "main.kicad_sch.bak").exists()

    assert main(["undo", str(main_sch), "--run", rid, "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert main_sch.read_bytes() == root_before
