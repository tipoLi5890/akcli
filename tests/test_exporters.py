"""Tests for the netlist emitters (``exporters.py``)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from akcli import exporters
from akcli.errors import AkcliError
from akcli.readers import altium_sch
from akcli.readers import kicad as kreader
from akcli.readers import sexpr

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus"


def _read(name: str):
    return altium_sch.read(str(FIXTURES / name))


def _read_kicad(name: str = "power_entry.kicad_sch"):
    return kreader.read_sch(str(CORPUS / name))


def test_csv_has_header_and_rows():
    sch = _read("two_gnd_ports.SchDoc")
    text = exporters.to_csv(sch)
    lines = text.strip().splitlines()
    assert lines[0] == "net,ref,pin"
    # 4 components, each one pin, all on GND -> 4 membership rows
    assert lines[1:] == ["GND,U1,1", "GND,U2,1", "GND,U3,1", "GND,U4,1"]


def test_protel_has_component_and_net_blocks():
    sch = _read("shared_name_label.SchDoc")
    text = exporters.to_protel(sch)
    # one [ ] block per component (4) and one ( ) net block
    assert text.count("[\n") == 4
    assert text.count("]\n") == 4
    assert "(\nSTAT\n" in text
    # every member appears as DESIGNATOR-PIN
    for token in ("U2-1", "U3-2", "R7-1", "R12-1"):
        assert token in text
    assert text.endswith("\n")


def test_kicad_netlist_is_parseable_shape():
    sch = _read("two_gnd_ports.SchDoc")
    text = exporters.to_kicad_netlist(sch)
    assert text.startswith('(export (version "E")')
    assert "(components" in text
    assert "(nets" in text
    assert '(net (code "1") (name "GND")' in text
    assert '(node (ref "U1") (pin "1"))' in text
    # balanced parens
    assert text.count("(") == text.count(")")


def test_unnamed_net_uses_stable_id():
    # junction_cross has a single UNNAMED net -> emitted under its stable_id
    sch = _read("junction_cross.SchDoc")
    assert all(n.name is None for n in sch.nets)
    sid = sch.nets[0].stable_id
    assert sid.startswith("net_")
    for emitter in (exporters.to_csv, exporters.to_protel, exporters.to_kicad_netlist):
        assert sid in emitter(sch)


def test_export_netlist_dispatch():
    sch = _read("two_gnd_ports.SchDoc")
    assert exporters.export_netlist(sch, "csv") == exporters.to_csv(sch)
    assert exporters.export_netlist(sch, "protel") == exporters.to_protel(sch)
    assert exporters.export_netlist(sch, "kicad") == exporters.to_kicad_netlist(sch)


def test_export_netlist_bad_format_raises():
    sch = _read("two_gnd_ports.SchDoc")
    with pytest.raises(AkcliError) as exc:
        exporters.export_netlist(sch, "nonsense")
    assert exc.value.code == "BAD_CONFIG"


def test_deterministic_output():
    sch = _read("shared_name_label.SchDoc")
    assert exporters.to_csv(sch) == exporters.to_csv(_read("shared_name_label.SchDoc"))
    assert exporters.to_kicad_netlist(sch) == exporters.to_kicad_netlist(
        _read("shared_name_label.SchDoc")
    )


# --------------------------------------------------------------------------- #
# KiCad netlist fidelity: design / UUID identity / libparts / fields
# (semantic agreement with a real kicad-cli export lives in test_e2e_netlist.py)
# --------------------------------------------------------------------------- #
def _comps(text: str) -> dict[str, sexpr.SNode]:
    """``ref -> (comp ...)`` node, parsed with the repo's own S-expression reader."""
    root = sexpr.parse(text)
    comps = root.find("components")
    assert comps is not None
    out = {}
    for comp in comps.find_all("comp"):
        ref = comp.find("ref")
        assert ref is not None
        out[ref[1].value] = comp
    return out


def test_kicad_netlist_has_design_block_without_wall_clock_date():
    """``(design)`` carries source/tool/sheets — and an EMPTY date (determinism)."""
    sch = _read_kicad()
    text = exporters.to_kicad_netlist(sch)
    design = sexpr.parse(text).find("design")
    assert design is not None
    # source is posix-normalized (backslashes would break byte-stable output)
    source = design.find("source")[1].value
    assert source.endswith("tests/fixtures/corpus/power_entry.kicad_sch")
    assert "\\" not in source
    # date is present-but-empty on purpose; no digits anywhere in it
    assert design.find("date")[1].value == ""
    assert '(date "")' in text
    assert design.find("tool")[1].value.startswith("akcli ")
    # flat board -> exactly the root sheet
    sheets = design.find_all("sheet")
    assert [(s.find("number")[1].value, s.find("tstamps")[1].value) for s in sheets] == [("1", "/")]


def test_kicad_netlist_comp_carries_uuid_path():
    """Every comp gets ``(sheetpath ...)`` + ``(tstamps <symbol uuid>)``.

    This is the association KiCad 6+ keys footprint<->symbol by; without it a
    re-import can only match by refdes and loses every association on renumber.
    """
    sch = _read_kicad()
    comps = _comps(exporters.to_kicad_netlist(sch))
    by_ref = {c.designator: c for c in sch.components}
    assert set(comps) == set(by_ref)
    for ref, node in comps.items():
        sheetpath = node.find("sheetpath")
        assert sheetpath is not None
        assert sheetpath.find("names")[1].value == "/"
        assert sheetpath.find("tstamps")[1].value == "/"
        # the uuid is the model's, verbatim (never re-derived)
        assert node.find("tstamps")[1].value == by_ref[ref].unique_id


def test_kicad_netlist_hierarchical_sheetpath_is_the_instance_path():
    """A component on a child sheet reports the sheet-instance UUID path."""
    from akcli.model import Component, Schematic

    comp = Component(
        designator="R1", library_ref="Device:R", x_mil=0, y_mil=0,
        unique_id="dddddddd-0000-4000-8000-00000000dddd",
        sheet="/bbbbbbbb-0000-4000-8000-000000000001",
    )
    sch = Schematic(source_path="root.kicad_sch", source_format="kicad",
                    components=[comp], nets=[])
    text = exporters.to_kicad_netlist(sch)
    sheetpath = _comps(text)["R1"].find("sheetpath")
    # KiCad writes the path with a trailing slash: "/<sheet-uuid>/"
    assert sheetpath.find("tstamps")[1].value == "/bbbbbbbb-0000-4000-8000-000000000001/"
    design = sexpr.parse(text).find("design")
    tstamps = [s.find("tstamps")[1].value for s in design.find_all("sheet")]
    assert tstamps == ["/", "/bbbbbbbb-0000-4000-8000-000000000001/"]


def test_kicad_netlist_libparts_one_per_lib_id():
    """``(libparts)``: one entry per distinct ``lib_id``, with fields + typed pins."""
    sch = _read_kicad()
    root = sexpr.parse(exporters.to_kicad_netlist(sch))
    libparts = root.find("libparts")
    assert libparts is not None
    entries = {
        (lp.find("lib")[1].value, lp.find("part")[1].value): lp
        for lp in libparts.find_all("libpart")
    }
    lib_ids = {c.library_ref for c in sch.components if c.library_ref}
    assert {f"{lib}:{part}" for lib, part in entries} == lib_ids
    # libsource is split into lib/part the same way (not one "Device:C" blob)
    libsource = _comps(exporters.to_kicad_netlist(sch))["C2"].find("libsource")
    assert (libsource.find("lib")[1].value, libsource.find("part")[1].value) == ("Device", "C")
    # a 2-pin passive: Value field + both pins with num/name/type
    cap = entries[("Device", "C")]
    fields = {f.find("name")[1].value: f[2].value for f in cap.find("fields").find_all("field")}
    assert fields["Value"] == "C"
    pins = [
        (p.find("num")[1].value, p.find("name")[1].value, p.find("type")[1].value)
        for p in cap.find("pins").find_all("pin")
    ]
    # `~` (KiCad's "no name" placeholder) is emitted as an empty name, as KiCad does
    assert pins == [("1", "", "passive"), ("2", "", "passive")]


def test_kicad_netlist_comp_fields_are_extra_properties_sorted():
    """Extra model properties (LCSC/MPN/...) land in ``(fields)``, deterministically."""
    from akcli.model import Component, Net, Schematic

    comp = Component(
        designator="U1", library_ref="MCU:ESP32", x_mil=0, y_mil=0, value="ESP32",
        footprint="Package:QFN-48", unique_id="11111111-2222-4333-8444-555555555555",
        parameters={
            "Reference": "U1", "Value": "ESP32", "Footprint": "Package:QFN-48",
            "MPN": "ESP32-WROOM", "LCSC": "C701342", "Datasheet": "http://x/y.pdf",
            "Empty": "",
        },
    )
    sch = Schematic(source_path="b.kicad_sch", source_format="kicad",
                    components=[comp], nets=[Net(name="GND", members=[("U1", "1")])])
    text = exporters.to_kicad_netlist(sch)
    fields = _comps(text)["U1"].find("fields")
    got = [(f.find("name")[1].value, f[2].value) for f in fields.find_all("field")]
    # sorted by name; ref/value/footprint are NOT duplicated; empty values dropped
    assert got == [
        ("Datasheet", "http://x/y.pdf"), ("LCSC", "C701342"), ("MPN", "ESP32-WROOM"),
    ]


def test_kicad_netlist_stays_balanced_and_parses():
    """Every corpus board: the emitted netlist parses and has all five sections."""
    for board in sorted(CORPUS.glob("*.kicad_sch")):
        text = exporters.to_kicad_netlist(kreader.read_sch(str(board)))
        root = sexpr.parse(text)
        assert root.tag == "export"
        for tag in ("design", "components", "libparts", "nets"):
            assert root.find(tag) is not None, f"{board.name}: missing ({tag}"
        assert text.count("(") == text.count(")")
        assert text.endswith("\n")


def test_kicad_netlist_byte_stable_across_runs():
    """Same input bytes -> same output bytes (two independent reads)."""
    first = exporters.to_kicad_netlist(_read_kicad())
    second = exporters.to_kicad_netlist(_read_kicad())
    assert first.encode("utf-8") == second.encode("utf-8")
    # and nothing date-like leaked into the output
    assert not re.search(r"\d{4}-\d\d-\d\d", first)
