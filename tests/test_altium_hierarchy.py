"""Altium multi-sheet reading (RECORD 15/16/32/33 + .PrjPcb).

Fixtures are generated AT RUNTIME with the in-repo fixture toolkit
(``tests/fixtures/_gen``) — parent/child ``.SchDoc`` containers written into
``tmp_path`` — so the corpus needs no new committed binaries. Scale of
``DistanceFromTop`` (1/10 Location units) follows the documented Altium
convention and is exercised end-to-end here; validation against a real AD
hierarchical design remains flagged in the skill.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "_gen"))
import altium_fixture  # noqa: E402
from altium_fixture import SchDocBuilder, write_schdoc  # noqa: E402

from akcli.errors import AkcliError
from akcli.readers import altium_prj, altium_sch


def _child_sheet(port_name: str = "IN") -> SchDocBuilder:
    """A child sheet: U2.1 wired to a PORT named ``port_name``."""
    b = SchDocBuilder()
    u2 = b.component("U2", 900, 500)
    b.pin(u2, "1", "A", 1000, 500)
    b.wire((1000, 500), (1500, 500))
    b.port(1500, 500, port_name)
    return b


def _parent_sheet(child_file: str, entry_name: str = "IN") -> SchDocBuilder:
    """Parent: U1.1 wired to a sheet entry on the child sheet symbol's left edge.

    Sheet symbol top-left at (1500, 1200); entry on the left side with
    DistanceFromTop=20 -> 20 * 10 = 200 units below the top => (1500, 1000),
    exactly where the wire from U1.1 ends.
    """
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000)
    b.pin(u1, "1", "A", 1000, 1000)
    b.wire((1000, 1000), (1500, 1000))
    ss = b.sheet_symbol(1500, 1200, 800, 600)
    b.sheet_name(ss, "CHILD")
    b.sheet_file(ss, child_file)
    b.sheet_entry(ss, entry_name, side=0, distance=20)
    return b


def _write(tmp_path: Path, name: str, builder: SchDocBuilder) -> Path:
    p = tmp_path / name
    write_schdoc(str(p), builder, emit_records_txt=False)
    return p


def test_child_sheet_components_and_names(tmp_path):
    _write(tmp_path, "child.SchDoc", _child_sheet())
    parent = _write(tmp_path, "parent.SchDoc", _parent_sheet("child.SchDoc"))
    sch = altium_sch.read(parent)
    assert {c.designator for c in sch.components} == {"U1", "U2"}
    assert sch.sheets == ["CHILD"]
    # namespaces: U1 on the root, U2 on the child instance
    ns = {c.designator: c.sheet for c in sch.components}
    assert ns["U1"] == "" and ns["U2"].startswith("/s")


def test_sheet_entry_connects_to_child_port(tmp_path):
    _write(tmp_path, "child.SchDoc", _child_sheet())
    parent = _write(tmp_path, "parent.SchDoc", _parent_sheet("child.SchDoc"))
    sch = altium_sch.read(parent)
    net = next(n for n in sch.nets if ("U1", "1") in n.members)
    assert ("U2", "1") in net.members, "entry<->port pairing failed"
    # the entry/port name rides along as the (local) net name
    assert "IN" in ([net.name] + net.aliases + net.source_names)


def test_same_port_name_in_two_children_stays_separate(tmp_path):
    """Hierarchical scope: two different child sheets both exposing port 'IN'
    must NOT merge globally (that is the flat-design behavior only)."""
    _write(tmp_path, "childA.SchDoc", _child_sheet("IN"))
    b = _child_sheet("IN")
    # rename its component so membership distinguishes the two children
    for rec in b._recs:
        if rec.get("Text") == "U2":
            rec["Text"] = "U3"
    _write(tmp_path, "childB.SchDoc", b)

    p = SchDocBuilder()
    u1 = p.component("U1", 900, 1000)
    p.pin(u1, "1", "A", 1000, 1000)
    p.wire((1000, 1000), (1500, 1000))
    ssa = p.sheet_symbol(1500, 1200, 800, 600)
    p.sheet_file(ssa, "childA.SchDoc")
    p.sheet_entry(ssa, "IN", side=0, distance=20)
    ssb = p.sheet_symbol(1500, 3200, 800, 600)   # second child, NOT wired
    p.sheet_file(ssb, "childB.SchDoc")
    p.sheet_entry(ssb, "IN", side=0, distance=20)
    parent = _write(tmp_path, "parent.SchDoc", p)

    sch = altium_sch.read(parent)
    net = next(n for n in sch.nets if ("U1", "1") in n.members)
    assert ("U2", "1") in net.members
    assert ("U3", "1") not in net.members, (
        "same-named ports of two different children merged globally"
    )


def test_flat_design_ports_stay_global(tmp_path):
    """No sheet symbols anywhere -> historical global-port behavior."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000)
    b.pin(u1, "1", "A", 1000, 1000)
    b.wire((1000, 1000), (1200, 1000))
    b.port(1200, 1000, "BUS")
    u2 = b.component("U2", 900, 2000)
    b.pin(u2, "1", "B", 1000, 2000)
    b.wire((1000, 2000), (1200, 2000))
    b.port(1200, 2000, "BUS")
    doc = _write(tmp_path, "flat.SchDoc", b)
    sch = altium_sch.read(doc)
    net = next(n for n in sch.nets if ("U1", "1") in n.members)
    assert ("U2", "1") in net.members  # same-name ports merge in a flat design


def test_missing_child_fails_loudly(tmp_path):
    parent = _write(tmp_path, "parent.SchDoc", _parent_sheet("nope.SchDoc"))
    with pytest.raises(FileNotFoundError):
        altium_sch.read(parent)


def test_sheet_recursion_is_refused(tmp_path):
    parent = _write(tmp_path, "loop.SchDoc", _parent_sheet("loop.SchDoc"))
    with pytest.raises(AkcliError):
        altium_sch.read(parent)


# --------------------------------------------------------------------------- #
# .PrjPcb
# --------------------------------------------------------------------------- #
def _write_prj(tmp_path: Path, docs: list[str], extra: str = "") -> Path:
    lines = ["[Design]", "Version=1.0", extra] if extra else ["[Design]", "Version=1.0"]
    for i, d in enumerate(docs, 1):
        lines += [f"[Document{i}]", f"DocumentPath={d}"]
    p = tmp_path / "proj.PrjPcb"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_prjpcb_reads_top_sheet(tmp_path):
    _write(tmp_path, "child.SchDoc", _child_sheet())
    _write(tmp_path, "parent.SchDoc", _parent_sheet("child.SchDoc"))
    prj = _write_prj(tmp_path, ["child.SchDoc", "parent.SchDoc"])
    sch = altium_prj.read(prj)
    # top-sheet detection picked parent (child is referenced by a sheet symbol)
    assert {c.designator for c in sch.components} == {"U1", "U2"}
    assert str(prj) == sch.source_path


def test_prjpcb_power_priority_flag(tmp_path):
    _write(tmp_path, "child.SchDoc", _child_sheet())
    _write(tmp_path, "parent.SchDoc", _parent_sheet("child.SchDoc"))
    prj = _write_prj(tmp_path, ["parent.SchDoc", "child.SchDoc"],
                     extra="PowerPortNamesTakePriority=1")
    info = altium_prj.read_project(prj)
    assert info["power_priority"] is True
    assert len(info["schematics"]) == 2


def test_prjpcb_without_schematics_fails(tmp_path):
    prj = _write_prj(tmp_path, ["board.PcbDoc"])
    with pytest.raises(AkcliError):
        altium_prj.read(prj)
