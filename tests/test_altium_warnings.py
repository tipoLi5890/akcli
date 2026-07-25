"""Altium import-fidelity warnings: nothing is dropped silently.

Two silent-failure modes are covered here:

* a ``.SchDoc`` RECORD id the reader does not model (buses are the real-world
  case) — counted into a skipped-record histogram and surfaced as a reader
  warning on stderr AND in ``read --json``'s ``warnings`` array;
* a ``.PrjPcb`` whose extra top-level sheets are ignored, or whose child
  schematic cannot be scanned for sheet symbols (which skews top-sheet
  detection) — both used to pass unnoticed.

Fixtures are generated at runtime with the in-repo toolkit
(``tests/fixtures/_gen``), so no new binaries are committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "_gen"))
from altium_fixture import SchDocBuilder, write_schdoc  # noqa: E402

from akcli.readers import altium_prj, altium_sch  # noqa: E402

from tests._cli_harness import json_of, run_cli  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def _write(tmp_path: Path, name: str, builder: SchDocBuilder) -> Path:
    p = tmp_path / name
    write_schdoc(str(p), builder, emit_records_txt=False)
    return p


def _wired_pair() -> SchDocBuilder:
    """A minimal, fully SUPPORTED sheet: U1.1 -- wire -- U2.1."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000)
    b.pin(u1, "1", "A", 1000, 1000)
    u2 = b.component("U2", 2100, 1000)
    b.pin(u2, "1", "B", 2000, 1000)
    b.wire((1000, 1000), (2000, 1000))
    return b


def _with_buses() -> SchDocBuilder:
    """The same sheet plus 3 Bus (26), 2 BusEntry (37) and 1 unknown (91) record."""
    b = _wired_pair()
    for i in range(3):
        b.add(26, **{"LocationCount": 2, "X1": 3000, "Y1": 1000 + i * 100,
                     "X2": 4000, "Y2": 1000 + i * 100})
    for i in range(2):
        b.add(37, **{"Location.X": 3000, "Location.Y": 1000 + i * 100})
    b.add(91, **{"Text": "a record id this reader has never heard of"})
    return b


# --------------------------------------------------------------------------- #
# skipped-record histogram
# --------------------------------------------------------------------------- #
def test_supported_sheet_reads_without_warnings(tmp_path):
    sch = altium_sch.read(_write(tmp_path, "clean.SchDoc", _wired_pair()))
    assert sch.warnings == []


def test_committed_net_fixtures_stay_warning_free():
    """The committed corpus uses only modelled records — no warning drift."""
    for name in ("shared_name_label", "junction_cross", "t_junction",
                 "no_erc", "two_gnd_ports", "ole_minifat"):
        sch = altium_sch.read(FIX / f"{name}.SchDoc")
        assert sch.warnings == [], f"{name} unexpectedly warns: {sch.warnings}"


def test_unsupported_records_are_counted_not_dropped(tmp_path):
    sch = altium_sch.read(_write(tmp_path, "buses.SchDoc", _with_buses()))
    assert sch.warnings == [
        "altium: skipped 6 unsupported record(s): "
        "26 (Bus) ×3, 37 (BusEntry) ×2, 91 ×1"
    ]
    # the sheet itself still reads (the histogram is a warning, not a failure)
    assert {c.designator for c in sch.components} == {"U1", "U2"}


def test_histogram_counts_records_from_every_sheet(tmp_path):
    """Hierarchical read: the child's skipped records are counted too."""
    child = _with_buses()
    _write(tmp_path, "child.SchDoc", child)
    parent = _wired_pair()
    ss = parent.sheet_symbol(1500, 3200, 800, 600)
    parent.sheet_file(ss, "child.SchDoc")
    parent.add(26, **{"LocationCount": 2, "X1": 0, "Y1": 0, "X2": 10, "Y2": 0})
    sch = altium_sch.read(_write(tmp_path, "parent.SchDoc", parent))
    assert sch.warnings == [
        "altium: skipped 7 unsupported record(s): "
        "26 (Bus) ×4, 37 (BusEntry) ×2, 91 ×1"
    ]


def test_histogram_collapses_a_long_tail(tmp_path):
    b = _wired_pair()
    ids = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 28, 30]   # 14 distinct ids
    for rid in ids:
        b.add(rid, **{"Location.X": 0, "Location.Y": 0})
    sch = altium_sch.read(_write(tmp_path, "tail.SchDoc", b))
    (warn,) = sch.warnings
    assert warn.startswith(f"altium: skipped {len(ids)} unsupported record(s): ")
    assert warn.endswith("+2 more id(s)")


def test_bytes_input_warns_too(tmp_path):
    p = _write(tmp_path, "buses.SchDoc", _with_buses())
    sch = altium_sch.read(p.read_bytes())
    assert sch.warnings and "26 (Bus) ×3" in sch.warnings[0]


# --------------------------------------------------------------------------- #
# CLI surface: stderr log + --json warnings array
# --------------------------------------------------------------------------- #
def test_read_cli_reports_the_histogram(tmp_path):
    p = _write(tmp_path, "buses.SchDoc", _with_buses())
    rc, _out, err = run_cli(["read", str(p)])
    assert rc == 0
    assert "warning: altium: skipped 6 unsupported record(s): 26 (Bus) ×3" in err

    doc = json_of(["read", str(p), "--json"], expect=0)
    assert any("26 (Bus) ×3" in w for w in doc["warnings"])
    summary = json_of(["read", str(p), "--json", "--summary"], expect=0)
    assert any("26 (Bus) ×3" in w for w in summary["warnings"])


# --------------------------------------------------------------------------- #
# .PrjPcb warnings
# --------------------------------------------------------------------------- #
def _write_prj(tmp_path: Path, docs: list[str]) -> Path:
    lines = ["[Design]", "Version=1.0"]
    for i, d in enumerate(docs, 1):
        lines += [f"[Document{i}]", f"DocumentPath={d}"]
    p = tmp_path / "proj.PrjPcb"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return p


def test_multi_top_project_names_the_ignored_sheets(tmp_path):
    _write(tmp_path, "boardA.SchDoc", _wired_pair())
    _write(tmp_path, "boardB.SchDoc", _wired_pair())
    prj = _write_prj(tmp_path, ["boardA.SchDoc", "boardB.SchDoc"])
    sch = altium_prj.read(prj)
    assert sch.warnings == [
        "altium: project lists 2 top-level schematic(s); reading boardA.SchDoc "
        "only, ignoring: boardB.SchDoc"
    ]


def test_single_top_project_stays_quiet(tmp_path):
    _write(tmp_path, "child.SchDoc", _wired_pair())
    parent = _wired_pair()
    ss = parent.sheet_symbol(1500, 3200, 800, 600)
    parent.sheet_file(ss, "child.SchDoc")
    _write(tmp_path, "parent.SchDoc", parent)
    prj = _write_prj(tmp_path, ["parent.SchDoc", "child.SchDoc"])
    assert altium_prj.read(prj).warnings == []


def test_unscannable_child_schematic_warns(tmp_path):
    _write(tmp_path, "parent.SchDoc", _wired_pair())
    (tmp_path / "broken.SchDoc").write_bytes(b"not an OLE2 container at all")
    prj = _write_prj(tmp_path, ["parent.SchDoc", "broken.SchDoc"])
    sch = altium_prj.read(prj)
    scan = [w for w in sch.warnings if "could not scan" in w]
    assert len(scan) == 1
    assert "broken.SchDoc" in scan[0]
    assert "AkcliError" in scan[0]              # error class is named
    assert "top-sheet detection may be wrong" in scan[0]
    # the readable sheet still parsed
    assert {c.designator for c in sch.components} == {"U1", "U2"}
