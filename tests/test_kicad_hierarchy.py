"""Hierarchical-sheet reading for the KiCad reader.

The reader recurses into ``(sheet ...)`` children (paths relative to the parent
file), keeps every sheet INSTANCE in its own geometric namespace, resolves
designators from the matching ``(instances (path ...))`` entry, and connects
across sheets ONLY via sheet-pin<->hierarchical-label pairs (plus global
labels / power ports, which netbuild already merges by name).
"""

from __future__ import annotations

import pytest

from akcli.errors import AkcliError
from akcli.readers import kicad as kreader

ROOT_UUID = "aaaaaaaa-0000-4000-8000-000000000000"
SHEET1_UUID = "bbbbbbbb-0000-4000-8000-000000000001"
SHEET2_UUID = "bbbbbbbb-0000-4000-8000-000000000002"

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


def _child_sch(refs_by_path: dict[str, str]) -> str:
    """A child sheet: one R wired to a hierarchical label 'OUT' at pin 1."""
    paths = "".join(
        f'\n\t\t\t\t(path "{p}" (reference "{r}") (unit 1))'
        for p, r in refs_by_path.items()
    )
    return (
        '(kicad_sch (version 20231120) (generator "test")\n'
        '\t(uuid "cccccccc-0000-4000-8000-00000000cccc")\n'
        '\t(paper "A4")\n' + _LIB_R +
        '\t(symbol (lib_id "Device:R") (at 50.8 50.8 0) (unit 1)\n'
        '\t\t(uuid "dddddddd-0000-4000-8000-00000000dddd")\n'
        '\t\t(property "Reference" "R?" (at 0 0 0))\n'
        '\t\t(pin "1" (uuid "dddddddd-0000-4000-8000-00000000d001"))\n'
        '\t\t(pin "2" (uuid "dddddddd-0000-4000-8000-00000000d002"))\n'
        f'\t\t(instances (project "t"{paths})))\n'
        # wire from R.1 tip (50.8, 46.99 — pin (at) IS the electrical tip) up
        '\t(wire (pts (xy 50.8 46.99) (xy 50.8 40.64)) (stroke (width 0) (type default))\n'
        '\t\t(uuid "eeeeeeee-0000-4000-8000-00000000e001"))\n'
        '\t(hierarchical_label "OUT" (shape output) (at 50.8 40.64 90)\n'
        '\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t(uuid "eeeeeeee-0000-4000-8000-00000000e002"))\n'
        ')\n'
    )


def _root_sch(sheets: list[tuple[str, str, float]]) -> str:
    """Root with N sheet instances of child.kicad_sch + a wire off each pin."""
    body = []
    for suuid, pin_net, y in sheets:
        body.append(
            f'\t(sheet (at 25.4 {y}) (size 25.4 12.7)\n'
            f'\t\t(uuid "{suuid}")\n'
            f'\t\t(property "Sheetname" "S{suuid[-1]}" (at 25.4 {y} 0))\n'
            f'\t\t(property "Sheetfile" "child.kicad_sch" (at 25.4 {y} 0))\n'
            f'\t\t(pin "OUT" output (at 50.8 {y + 6.35} 0)\n'
            f'\t\t\t(effects (font (size 1.27 1.27)))\n'
            f'\t\t\t(uuid "{suuid[:-1]}f"))\n'
            f'\t)\n'
            # wire from the sheet pin to a global label naming the parent net
            f'\t(wire (pts (xy 50.8 {y + 6.35}) (xy 63.5 {y + 6.35})) '
            f'(stroke (width 0) (type default))\n'
            f'\t\t(uuid "{suuid[:-1]}e"))\n'
            f'\t(global_label "{pin_net}" (shape input) (at 63.5 {y + 6.35} 0)\n'
            f'\t\t(effects (font (size 1.27 1.27)))\n'
            f'\t\t(uuid "{suuid[:-1]}d"))\n'
        )
    return (
        '(kicad_sch (version 20231120) (generator "test")\n'
        f'\t(uuid "{ROOT_UUID}")\n'
        '\t(paper "A4")\n'
        + "".join(body) + ')\n'
    )


def _write_design(tmp_path, n_sheets: int):
    sheets = [(SHEET1_UUID, "NET_A", 25.4)]
    refs = {f"/{ROOT_UUID}/{SHEET1_UUID}": "R101"}
    if n_sheets == 2:
        sheets.append((SHEET2_UUID, "NET_B", 76.2))
        refs[f"/{ROOT_UUID}/{SHEET2_UUID}"] = "R201"
    (tmp_path / "root.kicad_sch").write_text(_root_sch(sheets))
    (tmp_path / "child.kicad_sch").write_text(_child_sch(refs))
    return tmp_path / "root.kicad_sch"


def test_child_sheet_components_are_read(tmp_path):
    sch = kreader.read_sch(_write_design(tmp_path, 1))
    assert {c.designator for c in sch.components} == {"R101"}
    assert sch.sheets == ["S1"]


def test_sheet_pin_connects_to_child_hier_label(tmp_path):
    sch = kreader.read_sch(_write_design(tmp_path, 1))
    # The child's R101.1 must reach the parent's NET_A through OUT.
    net = next(n for n in sch.nets if n.name == "NET_A")
    assert ("R101", "1") in net.members
    # ...and the child-local name OUT rides along as an alias/source name.
    assert "OUT" in (net.aliases + [net.name] + net.source_names)


def test_twice_instantiated_sheet_stays_separate(tmp_path):
    """One file, two instances: distinct refs, and NO cross-instance merge."""
    sch = kreader.read_sch(_write_design(tmp_path, 2))
    assert {c.designator for c in sch.components} == {"R101", "R201"}
    net_a = next(n for n in sch.nets if n.name == "NET_A")
    net_b = next(n for n in sch.nets if n.name == "NET_B")
    assert ("R101", "1") in net_a.members and ("R201", "1") not in net_a.members
    assert ("R201", "1") in net_b.members and ("R101", "1") not in net_b.members
    # the synthetic hier connectors never leak into names/aliases
    for n in sch.nets:
        for name in [n.name or ""] + n.aliases + n.source_names:
            assert "\x02" not in name


def test_missing_sheet_file_fails_loudly(tmp_path):
    (tmp_path / "root.kicad_sch").write_text(_root_sch([(SHEET1_UUID, "NET_A", 25.4)]))
    with pytest.raises(FileNotFoundError):
        kreader.read_sch(tmp_path / "root.kicad_sch")


def test_sheet_recursion_is_refused(tmp_path):
    """A file that instantiates itself must fail, not loop forever."""
    (tmp_path / "root.kicad_sch").write_text(
        '(kicad_sch (version 20231120) (generator "test")\n'
        f'\t(uuid "{ROOT_UUID}")\n'
        '\t(paper "A4")\n'
        '\t(sheet (at 25.4 25.4) (size 25.4 12.7)\n'
        f'\t\t(uuid "{SHEET1_UUID}")\n'
        '\t\t(property "Sheetname" "loop" (at 0 0 0))\n'
        '\t\t(property "Sheetfile" "root.kicad_sch" (at 0 0 0)))\n'
        ')\n'
    )
    with pytest.raises(AkcliError):
        kreader.read_sch(tmp_path / "root.kicad_sch")


def test_child_sheet_is_writable(tmp_path, capsys):
    """Ops apply to a CHILD sheet of a hierarchy (audit: previously every op
    was refused with INVALID_INSTANCES_PATH because the child's symbols carry
    /<top-root>/<sheet> paths, not this file's own root uuid)."""
    import json

    from akcli.cli import main

    _write_design(tmp_path, 1)
    child = tmp_path / "child.kicad_sch"
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 2, "target_format": "kicad",
        "ops": [{"op": "add_text", "text": "reviewed", "at": [4000, 4000]}],
    }), encoding="utf-8")
    rc = main(["draw", str(child), "--ops", str(ops), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "reviewed" in child.read_text(encoding="utf-8")


def _instance_paths(sym) -> list[str]:
    out = []
    inst = sym.find("instances")
    for proj in (inst.find_all("project") if inst is not None else []):
        for path in proj.find_all("path"):
            out.append(path.children[1].value)
    return out


def _placed(doc, ref: str):
    """The placed symbol whose instances (or Reference property) name ``ref``."""
    for sym in doc.find_all("symbol"):
        if sym.find("lib_id") is None:
            continue
        refs = []
        inst = sym.find("instances")
        for proj in (inst.find_all("project") if inst is not None else []):
            for path in proj.find_all("path"):
                r = path.find("reference")
                if r is not None:
                    refs.append(r.children[1].value)
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3 and kids[1].value == "Reference":
                refs.append(kids[2].value)
        if ref in refs:
            return sym
    return None


def _place_into_child(tmp_path, n_sheets: int, extra_fields: dict | None = None,
                      expect_rc: int = 0):
    """Place R9 into the CHILD file of an n-instance hierarchy; return it."""
    import json

    from akcli.cli import main

    _write_design(tmp_path, n_sheets)
    lib = tmp_path / "lib.kicad_sym"
    lib.write_text(
        '(kicad_symbol_lib (version 20231120) (generator t)\n'
        '  (symbol "RR" (pin_numbers (hide yes)) (in_bom yes) (on_board yes)\n'
        '    (property "Reference" "R" (at 0 0 0))\n'
        '    (symbol "RR_1_1"\n'
        '      (pin passive line (at 0 3.81 270) (length 1.27)\n'
        '        (name "~") (number "1"))\n'
        '      (pin passive line (at 0 -3.81 90) (length 1.27)\n'
        '        (name "~") (number "2")))))\n', encoding="utf-8")
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 2, "target_format": "kicad",
        "ops": [{"op": "place_component", "lib_id": "RR", "designator": "R9",
                 "x_mil": 3000, "y_mil": 3000, **(extra_fields or {})}],
    }), encoding="utf-8")
    child = tmp_path / "child.kicad_sch"
    rc = main(["draw", str(child), "--ops", str(ops), "--apply",
               "--symbols", str(lib)])
    assert rc == expect_rc
    return child


def test_child_sheet_placement_joins_the_sibling_sheet_paths(tmp_path, capsys):
    """A part placed INTO a child sheet must sit on the sheet path(s) its
    siblings do — the flat ``/<own-root>`` form annotated as R? in KiCad."""
    from akcli.sexpr import parser as sx

    child = _place_into_child(tmp_path, 1)
    capsys.readouterr()
    doc = sx.parse(child.read_text(encoding="utf-8"))
    assert _instance_paths(_placed(doc, "R9")) == \
        _instance_paths(_placed(doc, "R101"))


def test_twice_instantiated_scalar_ref_refuses(tmp_path, capsys):
    """0.20: a sheet instantiated twice REFUSES a scalar designator — writing
    R9 onto both (path ...) entries annotates two instances with one refdes
    (the duplicate-refdes design the per-instance map exists to prevent). The
    refusal lists the instance paths the map must cover."""
    _place_into_child(tmp_path, 2, expect_rc=6)
    out = capsys.readouterr().out
    assert "INSTANCE_REFS_REQUIRED" in out
    assert f"/{ROOT_UUID}/{SHEET1_UUID}" in out    # the fix is copy-pasteable


def test_twice_instantiated_child_placement_with_instance_map(tmp_path, capsys):
    """With instance_designators, the new symbol carries BOTH sibling paths —
    each with its OWN reference — and the write replays byte-identically."""
    from akcli.sexpr import parser as sx

    imap = {f"/{ROOT_UUID}/{SHEET1_UUID}": "R9",
            f"/{ROOT_UUID}/{SHEET2_UUID}": "R209"}
    child = _place_into_child(tmp_path, 2,
                              extra_fields={"instance_designators": imap})
    capsys.readouterr()
    doc = sx.parse(child.read_text(encoding="utf-8"))
    sym = _placed(doc, "R9")
    assert _instance_paths(sym) == _instance_paths(_placed(doc, "R101"))

    from akcli.writers import instances as winst
    assert winst.symbol_references(sym) == imap
    # the SAME symbol answers to its second instance's refdes
    assert _placed(doc, "R209") is not None

    # ...and the write replays byte-identically
    from akcli.cli import main
    once = child.read_bytes()
    assert main(["draw", str(child), "--ops", str(tmp_path / "ops.json"),
                 "--apply", "--symbols", str(tmp_path / "lib.kicad_sym")]) == 0
    capsys.readouterr()
    assert child.read_bytes() == once


def test_unannotated_child_sheet_is_writable(tmp_path, capsys):
    """The child fixture's symbols carry the placeholder ``R?`` reference in
    their property; a doc full of ``R?`` used to count as N duplicates and
    refuse EVERY write (even an add_text)."""
    import json

    from akcli.cli import main

    _write_design(tmp_path, 1)
    child = tmp_path / "child.kicad_sch"
    # strip the (instances) refs so R? is the only reference in the file
    child.write_text(
        child.read_text(encoding="utf-8").replace('"R101"', '"R?"'),
        encoding="utf-8")
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 2, "target_format": "kicad",
        "ops": [{"op": "add_text", "text": "note", "at": [4000, 4000]}],
    }), encoding="utf-8")
    assert main(["draw", str(child), "--ops", str(ops), "--apply"]) == 0
    capsys.readouterr()
    assert "note" in child.read_text(encoding="utf-8")


def test_flat_file_bad_instances_path_still_refused(tmp_path):
    """The relaxed rule still rejects a single-segment path naming a WRONG
    root — flat-file corruption stays caught."""
    from akcli.sexpr import parser as sx
    from akcli.writers import connectivity

    bad = _child_sch({"/99999999-0000-4000-8000-000000000099": "R1"})
    findings = connectivity.verify(sx.parse(bad))
    assert [f for f in findings if f.code == "INVALID_INSTANCES_PATH"]
