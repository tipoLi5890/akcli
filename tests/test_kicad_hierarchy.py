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
