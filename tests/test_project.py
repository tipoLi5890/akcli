"""Sheet-tree root discovery + tree-scoped net gate (``akcli.project``).

The 0.20 correctness fix: a child ``.kicad_sch`` read standalone is a TRUNCATED
graph — its global labels lose every member contributed by sibling sheets — so
plan/draw's net-diff must resolve the tree's root and evaluate the WHOLE tree.
These tests pin root discovery (flat / child / explicit / ambiguous), the
staging layout, and the end-to-end gate scope difference (tree vs ``--flat``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import project
from akcli.cli import main
from akcli.errors import EXIT, AkcliError

ROOT_UUID = "aaaa0000-0000-4000-8000-00000000000a"
ROOT2_UUID = "aaaa0000-0000-4000-8000-00000000000b"
SHEET1_UUID = "bbbb0000-0000-4000-8000-000000000001"

_LIB_R = """
\t(lib_symbols
\t\t(symbol "Device:R" (pin_numbers (hide yes)) (in_bom yes) (on_board yes)
\t\t\t(property "Reference" "R" (at 0 0 0))
\t\t\t(symbol "R_1_1"
\t\t\t\t(pin passive line (at 0 3.81 270) (length 1.27)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))
\t)
"""


def _child_sch() -> str:
    """One R (ref R101 via instances); pin 1 wired to global label SHARED."""
    return (
        '(kicad_sch (version 20231120) (generator "test")\n'
        '\t(uuid "cccc0000-0000-4000-8000-00000000000c")\n'
        '\t(paper "A4")\n' + _LIB_R +
        '\t(symbol (lib_id "Device:R") (at 50.8 50.8 0) (unit 1)\n'
        '\t\t(uuid "dddd0000-0000-4000-8000-00000000000d")\n'
        '\t\t(property "Reference" "R?" (at 0 0 0))\n'
        '\t\t(pin "1" (uuid "dddd0000-0000-4000-8000-0000000000d1"))\n'
        '\t\t(pin "2" (uuid "dddd0000-0000-4000-8000-0000000000d2"))\n'
        f'\t\t(instances (project "t"\n'
        f'\t\t\t\t(path "/{ROOT_UUID}/{SHEET1_UUID}" (reference "R101") (unit 1)))))\n'
        '\t(wire (pts (xy 50.8 46.99) (xy 50.8 40.64)) (stroke (width 0) (type default))\n'
        '\t\t(uuid "eeee0000-0000-4000-8000-0000000000e1"))\n'
        '\t(global_label "SHARED" (shape input) (at 50.8 40.64 90)\n'
        '\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t(uuid "eeee0000-0000-4000-8000-0000000000e2"))\n'
        ')\n'
    )


def _root_sch(child_file: str, root_uuid: str = ROOT_UUID) -> str:
    """Instantiates the child once + its OWN R2 on the same global SHARED."""
    return (
        '(kicad_sch (version 20231120) (generator "test")\n'
        f'\t(uuid "{root_uuid}")\n'
        '\t(paper "A4")\n' + _LIB_R +
        '\t(sheet (at 25.4 25.4) (size 25.4 12.7)\n'
        f'\t\t(uuid "{SHEET1_UUID}")\n'
        '\t\t(property "Sheetname" "S1" (at 25.4 25.4 0))\n'
        f'\t\t(property "Sheetfile" "{child_file}" (at 25.4 25.4 0))\n'
        '\t)\n'
        '\t(symbol (lib_id "Device:R") (at 101.6 50.8 0) (unit 1)\n'
        '\t\t(uuid "ffff0000-0000-4000-8000-00000000000f")\n'
        '\t\t(property "Reference" "R2" (at 0 0 0))\n'
        '\t\t(pin "1" (uuid "ffff0000-0000-4000-8000-0000000000f1"))\n'
        '\t\t(pin "2" (uuid "ffff0000-0000-4000-8000-0000000000f2"))\n'
        f'\t\t(instances (project "t"\n'
        f'\t\t\t\t(path "/{root_uuid}" (reference "R2") (unit 1)))))\n'
        '\t(wire (pts (xy 101.6 46.99) (xy 101.6 40.64)) (stroke (width 0) (type default))\n'
        '\t\t(uuid "ffff0000-0000-4000-8000-0000000000e1"))\n'
        '\t(global_label "SHARED" (shape input) (at 101.6 40.64 90)\n'
        '\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t(uuid "ffff0000-0000-4000-8000-0000000000e2"))\n'
        ')\n'
    )


def _write_tree(tmp_path: Path, child_dir: str = "") -> tuple[Path, Path]:
    """root + child (child optionally in a subdirectory); returns (root, child)."""
    cdir = tmp_path / child_dir if child_dir else tmp_path
    cdir.mkdir(parents=True, exist_ok=True)
    child = cdir / "child.kicad_sch"
    child.write_text(_child_sch(), encoding="utf-8")
    rel = f"{child_dir}/child.kicad_sch" if child_dir else "child.kicad_sch"
    root = tmp_path / "root.kicad_sch"
    root.write_text(_root_sch(rel), encoding="utf-8")
    return root, child


# The edit under test: a NEW resistor whose pin 1 joins global SHARED. A new
# pin is an addition (never a merge), so the summary carries the member count:
# flat view "SHARED: 1->2 pins"; tree view "2->3 pins" — root's R2.1 is the
# member the flat view cannot see. Device:R resolves from the child's own
# embedded lib_symbols (--symbols <child>), so no KiCad install is needed.
_EDIT_OPS = {
    "protocol_version": 2,
    "target_format": "kicad",
    "ops": [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R3",
         "x_mil": 3000, "y_mil": 3000, "value": "1k"},
        {"op": "add_net_label", "at": "R3.1", "name": "SHARED",
         "scope": "global"},
    ],
}


def _ops_file(tmp_path: Path) -> str:
    p = tmp_path / "edit.json"
    p.write_text(json.dumps(_EDIT_OPS), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- #
# unit: topology
# --------------------------------------------------------------------------- #
def test_tree_files_walks_root_first(tmp_path):
    root, child = _write_tree(tmp_path)
    assert project.tree_files(root) == [root.resolve(), child.resolve()]


def test_resolve_root_flat_file_is_its_own_root(tmp_path):
    lone = tmp_path / "lone.kicad_sch"
    lone.write_text(_child_sch(), encoding="utf-8")
    assert project.resolve_root(lone) == lone.resolve()


def test_resolve_root_child_finds_root(tmp_path):
    root, child = _write_tree(tmp_path)
    assert project.resolve_root(child) == root.resolve()


def test_resolve_root_root_is_itself(tmp_path):
    root, _child = _write_tree(tmp_path)
    assert project.resolve_root(root) == root.resolve()


def test_resolve_root_cross_directory_child(tmp_path):
    root, child = _write_tree(tmp_path, child_dir="sub")
    assert project.resolve_root(child) == root.resolve()


def test_resolve_root_explicit_mismatch_refuses(tmp_path):
    root, child = _write_tree(tmp_path)
    other = tmp_path / "other.kicad_sch"
    other.write_text(_child_sch(), encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        project.resolve_root(child, explicit=other)
    assert ei.value.code == "PROJECT_ROOT_MISMATCH"


def test_resolve_root_ambiguous_refuses(tmp_path):
    root, child = _write_tree(tmp_path)
    (tmp_path / "root2.kicad_sch").write_text(
        _root_sch("child.kicad_sch", root_uuid=ROOT2_UUID), encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        project.resolve_root(child)
    assert ei.value.code == "PROJECT_ROOT_AMBIGUOUS"


def test_resolve_root_kicad_pro_breaks_the_tie(tmp_path):
    root, child = _write_tree(tmp_path)
    (tmp_path / "root2.kicad_sch").write_text(
        _root_sch("child.kicad_sch", root_uuid=ROOT2_UUID), encoding="utf-8")
    (tmp_path / "root.kicad_pro").write_text("{}", encoding="utf-8")
    assert project.resolve_root(child) == root.resolve()


def test_stage_tree_preserves_relative_layout(tmp_path):
    root, child = _write_tree(tmp_path, child_dir="sub")
    base = tmp_path / "stage"
    mapping = project.stage_tree(project.tree_files(root), base)
    staged_root = mapping[root.resolve()]
    staged_child = mapping[child.resolve()]
    assert staged_root.read_bytes() == root.read_bytes()
    # the child keeps its position RELATIVE to the root, so Sheetfile resolves
    assert staged_child == staged_root.parent / "sub" / "child.kicad_sch"


# --------------------------------------------------------------------------- #
# end-to-end: the gate evaluates the tree (the 0.20 correctness fix)
# --------------------------------------------------------------------------- #
def _plan_json(capsys, target: Path, ops: str, *extra: str) -> dict:
    assert main(["plan", str(target), "--ops", ops, "--symbols", str(target),
                 "--json", *extra]) == EXIT["OK"]
    return json.loads(capsys.readouterr().out)


def test_child_edit_net_diff_sees_the_whole_tree(tmp_path, capsys):
    root, child = _write_tree(tmp_path)
    d = _plan_json(capsys, child, _ops_file(tmp_path))
    assert d["project_root"] == root.resolve().as_posix()
    assert d["evaluation_scope"] == "tree"
    # SHARED gains R101.2 on top of {R101.1 (child), R2.1 (root)}: 2 -> 3 pins.
    assert any("(2->3 pins)" in ln for ln in d["net_diff"]["lines"])


def test_child_edit_flat_flag_keeps_single_file_scope(tmp_path, capsys):
    _root, child = _write_tree(tmp_path)
    d = _plan_json(capsys, child, _ops_file(tmp_path), "--flat")
    assert "project_root" not in d          # absent, not null
    assert "evaluation_scope" not in d
    assert any("(1->2 pins)" in ln for ln in d["net_diff"]["lines"])


def test_flat_target_payload_has_no_scope_fields(tmp_path, capsys):
    lone = tmp_path / "lone.kicad_sch"
    lone.write_text(_child_sch(), encoding="utf-8")
    d = _plan_json(capsys, lone, _ops_file(tmp_path))
    assert "project_root" not in d
    assert "evaluation_scope" not in d


def test_cross_directory_child_edit_stages_the_tree(tmp_path, capsys):
    root, child = _write_tree(tmp_path, child_dir="sub")
    d = _plan_json(capsys, child, _ops_file(tmp_path))
    assert d["project_root"] == root.resolve().as_posix()
    assert any("(2->3 pins)" in ln for ln in d["net_diff"]["lines"])


def test_ambiguous_root_degrades_honestly_in_advisory_mode(tmp_path, capsys):
    _root, child = _write_tree(tmp_path)
    (tmp_path / "root2.kicad_sch").write_text(
        _root_sch("child.kicad_sch", root_uuid=ROOT2_UUID), encoding="utf-8")
    assert main(["plan", str(child), "--ops", _ops_file(tmp_path),
                 "--symbols", str(child), "--json"]) == EXIT["OK"]
    cap = capsys.readouterr()
    d = json.loads(cap.out)
    # NO diff at all beats a truncated one presented as the net effect.
    assert d["net_diff"] is None
    assert "PROJECT_ROOT_AMBIGUOUS" in cap.err


def test_ambiguous_root_fails_closed_under_strict_nets(tmp_path, capsys):
    _root, child = _write_tree(tmp_path)
    (tmp_path / "root2.kicad_sch").write_text(
        _root_sch("child.kicad_sch", root_uuid=ROOT2_UUID), encoding="utf-8")
    before = child.read_bytes()
    assert main(["draw", str(child), "--ops", _ops_file(tmp_path),
                 "--symbols", str(child), "--apply",
                 "--strict-nets", "--no-erc", "--json"]) == EXIT["OPLIST"]
    assert child.read_bytes() == before      # nothing written
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "PROJECT_ROOT_AMBIGUOUS"


def test_explicit_root_mismatch_is_a_usage_error(tmp_path, capsys):
    _root, child = _write_tree(tmp_path)
    other = tmp_path / "other.kicad_sch"
    other.write_text(_child_sch(), encoding="utf-8")
    assert main(["plan", str(child), "--ops", _ops_file(tmp_path),
                 "--symbols", str(child), "--root", str(other),
                 "--json"]) == EXIT["USAGE"]
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "PROJECT_ROOT_MISMATCH"


def test_explicit_root_overrides_discovery(tmp_path, capsys):
    root, child = _write_tree(tmp_path)
    (tmp_path / "root2.kicad_sch").write_text(
        _root_sch("child.kicad_sch", root_uuid=ROOT2_UUID), encoding="utf-8")
    d = _plan_json(capsys, child, _ops_file(tmp_path), "--root", str(root))
    assert d["project_root"] == root.resolve().as_posix()
    assert any("(2->3 pins)" in ln for ln in d["net_diff"]["lines"])


def test_stage_directory_is_cleaned_up(tmp_path, capsys):
    root, child = _write_tree(tmp_path)
    _plan_json(capsys, child, _ops_file(tmp_path))
    stage_root = root.parent / ".akcli" / "stage"
    leftovers = list(stage_root.glob("*")) if stage_root.exists() else []
    assert leftovers == []


def test_draw_apply_on_child_writes_only_the_child(tmp_path, capsys):
    root, child = _write_tree(tmp_path)
    root_before = root.read_bytes()
    assert main(["draw", str(child), "--ops", _ops_file(tmp_path),
                 "--symbols", str(child), "--apply",
                 "--strict-nets", "--no-erc", "--json"]) == EXIT["OK"]
    d = json.loads(capsys.readouterr().out)
    assert d["applied"] is True
    assert d["evaluation_scope"] == "tree"
    assert root.read_bytes() == root_before   # P1 writes stay single-file
    # the applied edit is visible from the ROOT view
    capsys.readouterr()
    assert main(["nets", str(root), "--match", "SHARED", "--json"]) == EXIT["OK"]
    nets = json.loads(capsys.readouterr().out)
    shared = next(n for n in nets["nets"] if n["name"] == "SHARED")
    assert "R3.1" in shared["members"] and "R2.1" in shared["members"]
